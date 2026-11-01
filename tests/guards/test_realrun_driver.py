"""MVP-2 笔 P 的判据：真入口 `acp.realrun` 的驱动形状（owner 2026-10-19 B 计划第二步 + 三条决定）。

**分工**（三处各量一件，免得同一件事被量三次、每处都以为自己量全了）：

- `test_transport_spec_factory.py` 量**工厂自己**：缺凭据拒、不降级、造出来的是真 transport、超时数从厂商模块来；
- `test_realrun_and_machine_principal.py` 量**启动面那把旗标**：`--machine-principal` 落到 `app.state`、
  没配时保持"没这个属性"那种形状（不是 `None`、不是空集合）；真跑入口的命令行形态、`git init` 的成败
  与退出码都不在它那里，在本文件节 二；
- 本文件量**驱动本身**：厂商构造只有一个入口、停点写不出东西、失败政策的先后、
  以及真 PG 上一次卡跑完之后的库内读数到底落在哪一格。
  节 二 从笔 T（2026-10-20 第一发点火之后）还量两道**动手之前**的拒绝 —— 路径重复（`--bus-root` 末段
  等于 `--project`）与 git 环境变量穿透（`GIT_DIR` 那三个），以及"`init` 退了 0 但顶层仍不成立"那条
  消息到底有没有说清是哪一格坏了；这三件事的共同点是**拒之前盘上必须一个字节都没动**，
  所以每一发都同时断"没建目录"与"没发 git"。

**跨档借用点名**：节 三那三发要一个"已批准并恢复完"的现场，那份搭建件（`to_resumed` / `make_all_ready` /
`drop_thread`）在 `tests/guards/test_run_chain.py` 里，按文件路径 load 过来用，**不抄第二份**。
理由与 `test_transport_spec_factory.py` 借 transport 契约档那份 fixture 同一条：抄第二份现场搭建
就会有两份孤儿 thread 清理逻辑，而 `checkpoints` 里留一个孤儿就是 Q4 那条守卫要拦的事。

**全程零出网、零真凭据**：节 一与节 二都不连库（节 二 那几发全在 `tmp_path` 上做）；
节 三连的是本机 PG（缺 `ACP_DB_DSN` 整节 skip，不是绿），并且用一支只写真文件的假厂商
（`FakeVendorTransport`）顶掉真构造 —— 注入位就是为这一格留的。
本文件里出现的模型 ID 与凭据值全是 `fixture-…` 形态的假值。
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from acp import realrun, run
from acp.adapters.stub import StubAdapter, artifact_path, resolve_transport_flag, StubMisuse
from acp.archive import root as archive
from acp.bus import acl
from acp.bus import layout
from acp.bus.gateway import BusGateway
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import review as review_node
from acp.orchestrate.nodes import validate_artifact
from acp.persistence.models_approval import ReviewVerdict
from acp.persistence.models_core import Artifact, TaskCard
from acp.persistence.models_registry import Deliverable, EventLog
from acp.review import service as review_service
from acp.protocol.messages import Task, TaskResult
from acp.protocol.refs import Handle, StrictModel

SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "acp"
DRIVER = SRC_ROOT / "realrun.py"

NOW = dt.datetime(2026, 10, 19, 9, 30, tzinfo=dt.timezone.utc)
PROJECT = "acp-realrun"
FAKE_MODEL = "fixture-realrun-model"

#: 下限的真值不在这里抄：那是内置拆单那张卡的三列（512B / 12 行 / `html`|`css`），
#: 由 `card.artifact_floor_*` 读回来比对。所以下面这两支"正文"是按**卡声明的那个格式**长出来的，
#: 而不是测试自己挑一种后缀 —— 假厂商也要交一份格式对得上、只是内容是我编的东西。
def _good_body(fmt: str) -> str:
    if fmt == "css":
        return "/* fixture */\n" + "".join(f".line-{i} {{ color: #336699; padding: {i}px; }}\n" for i in range(20))
    return "<!DOCTYPE html>\n" + "".join(f"<p>line {i} 的内容，够长够一行。</p>\n" for i in range(20)) + "</html>\n"


def _tiny_body(fmt: str) -> str:
    return "/* t */\n" if fmt == "css" else "<p>短</p>\n"


def _load_chain_harness():
    path = pathlib.Path(__file__).with_name("test_run_chain.py")
    spec = importlib.util.spec_from_file_location("realrun_chain_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_chain = _load_chain_harness()


# ------------------------------------------------------------------ 小工具（AST 面）


def calls_named(tree: ast.AST, *names: str) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id in names:
                hits.append(target.id)
            elif isinstance(target, ast.Attribute) and target.attr in names:
                hits.append(target.attr)
    return hits


def source_modules(path: pathlib.Path) -> set[str]:
    """一份源文件 import 到的模块全名（三种写法都吃到，口径与 `test_all_adapters._import_edges` 同）。"""
    edges: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            edges |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            head = node.module or ""
            edges.add(head)
            edges |= {f"{head}.{alias.name}" for alias in node.names if not head.startswith("acp")}
    return {edge for edge in edges if edge}


def func_source(name: str) -> str:
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(DRIVER.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"`realrun.py` 里没有函数 {name!r} ⇒ 这条判据盯的是一个已不存在的形状")


def call_sites(name: str, root: pathlib.Path) -> list[str]:
    """`src/acp` 下调了这个名字的文件（相对路径），**逐个文件单独 parse**。

    口径同 `test_transport_spec_factory._call_sites`：用 AST 而不是 grep —— grep 的命中数不等于调用点数
    （散文与错误消息文本一样会命中那个名字）。
    """
    hits = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if calls_named(tree, name):
            hits.append(path.relative_to(root).as_posix())
    return hits


class FakeSession:
    """只回答"这一发有没有真去开会话"的替身。它不回答任何数据，回答了就等于在 guards 档伪造库里的事实。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rolled_back = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1

    def get(self, model, key):  # noqa: ANN001, ANN201 - 路线 B 的路由读取只问"有没有这一行"
        """`None` = 库里没有这一行 ⇒ 这一档**没有** `model_routing`，工厂保持原样（不伪造路由）。"""
        return None


class Boom(RuntimeError):
    """注入位被调到就说明这一发走到了它不该走的地方。"""


def _sink() -> tuple[list[str], Callable[[str], object]]:
    """一个记账列表加它的 `append` —— 判据要的是"这句话确实说出口了"，不是只断返回值。"""
    lines: list[str] = []
    return lines, lines.append


def _call_keywords(source: str, func_name: str, attr: str) -> dict[str, object]:
    """取出 `source` 里 `func_name` 那一次 `.<attr>(...)` 调用的关键字，收成 `{名字: 字面量值}`。

    非字面量的值收成 `None`：本判据只比 `encoding` / `errors` / `check` 这三个字面量，
    拿不到字面量就等于那一发不是写死的口径，比较结果要能说出口。
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == attr:
                    return {
                        kw.arg: (kw.value.value if isinstance(kw.value, ast.Constant) else None)
                        for kw in inner.keywords
                        if kw.arg is not None
                    }
            raise AssertionError(f"{func_name} 里没有 .{attr}(...) 这一发 ⇒ 这条判据盯的形状已经不在了")
    raise AssertionError(f"源文件里没有函数 {func_name!r}")


# ------------------------------------------------------------------ 一、构造面：厂商只能从工厂进来


def test_the_driver_constructs_vendor_transports_only_through_the_factory(tmp_path):
    """`realrun.py` 里不许出现第二份厂商构造：只调 `build_real_transport()`，不 import `httpx`、不引 transport 目录。

    挡的错有两种，形状相反：
    ① 驱动自己 `DoubaoTransport(api=..., http=httpx.Client(...))` —— 那就绕开了工厂那道"缺凭据即拒"，
       也重新引入了"spec 的第二处构造"（`test_no_production_module_constructs_a_vendor_spec_bypassing_the_factory`
       量过的那一格）；
    ② 驱动顺手 `StubAdapter(...)` —— 那会把"真跑"读成"跑通了但一个厂商都没碰"。
    两条不许只靠"扫不到"绿：`tmp_path` 那份诱饵同时证明了扫描器看得见这两种写法。
    """
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    edges = source_modules(DRIVER)

    assert calls_named(tree, "build_real_transport") == ["build_real_transport"], (
        "驱动里的真装配调用点变了：应当恰一处（`real_transport()` 那一发）"
    )
    forbidden_calls = (
        "StubAdapter",
        "DoubaoTransport",
        "QoderTransport",
        "WorkBuddyTransport",
        "DoubaoApiSpec",
        "QoderCliSpec",
        "WorkBuddyApiSpec",
        "resolve_transport_flag",
    )
    assert calls_named(tree, *forbidden_calls) == [], f"驱动绕过工厂自己构造：{calls_named(tree, *forbidden_calls)}"

    assert "httpx" not in {edge.split(".")[0] for edge in edges}, "驱动不许直接出网：客户端归工厂与 transport"
    assert not [edge for edge in edges if edge.startswith("acp.adapters.") and edge != "acp.adapters.spec_factory"], (
        f"驱动引了厂商目录：{edges}"
    )
    assert "acp.adapters.stub" not in edges, "驱动不许把仿真侧拉进真跑入口"

    decoy = tmp_path / "bypass.py"
    decoy.write_text(
        "import httpx\n\n"
        "from acp.adapters.doubao.transport import DoubaoTransport\n"
        "from acp.adapters.stub import StubAdapter\n\n"
        "def f():\n"
        "    return DoubaoTransport(http=httpx.Client()), StubAdapter(), resolve_transport_flag('stub')\n",
        encoding="utf-8",
        newline="\n",
    )
    decoy_tree = ast.parse(decoy.read_text(encoding="utf-8"))
    assert calls_named(decoy_tree, *forbidden_calls) != [], "扫描器看不见真构造 ⇒ 上面那三条是空对空"
    assert "httpx" in {edge.split(".")[0] for edge in source_modules(decoy)}, "导入扫描吃不到真出网"


def test_the_factory_has_exactly_one_production_caller_and_it_is_the_driver():
    """接线之后读数该怎么改：`build_real_transport()` 的生产调用点**恰一处 = `realrun.py`**，
    而 `build_transport_spec()` 仍然只在工厂自己那一个文件里被调。

    这两格一起才说明"接了线但没接出第二处构造点"：只断前一条会放过"驱动自己又拼了一份 spec"；
    只断后一条会放过"没人调工厂、真跑是假的"。
    """
    defining = (SRC_ROOT / "adapters" / "spec_factory.py").relative_to(SRC_ROOT).as_posix()
    wired = [hit for hit in call_sites("build_real_transport", SRC_ROOT) if hit != defining]
    assert wired == ["realrun.py"], f"真装配的调用点应当恰是驱动一处：{wired}"

    specs = [hit for hit in call_sites("build_transport_spec", SRC_ROOT) if hit != defining]
    assert specs == [], f"有人绕开工厂自己构造厂商 spec：{specs}"


def test_the_driver_borrows_the_two_single_carriers(tmp_path):
    """驱动借用 `run._task_of()` 与 `run._slots_from_registry()` 这两个私有件，各恰一处。

    这不是"懒得重写"，是**不许有第二份**：`Task` 那 13 项必填与"上限从 EN-03 现值读"各只有一个载体，
    在驱动里另写一份就会漂成"下发面读的列与指派段写的列不是同一份"。代价点名：跨模块调私有函数 ⇒
    **改名要两侧同笔**，所以这一格把名字钉死，而不是让重命名静默把一个私有件变成第二个读数。
    """
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    assert calls_named(tree, "_task_of") == ["_task_of"], calls_named(tree, "_task_of")
    assert calls_named(tree, "_slots_from_registry") == ["_slots_from_registry"]

    decoy = tmp_path / "second_carrier.py"
    decoy.write_text(
        "from acp import run\n\n\ndef f(session, cards):\n"
        "    return run._task_of(session, 'f', 'T01', project_key='p', executor_role_id='r'), "
        "run._slots_from_registry(session)\n",
        encoding="utf-8",
        newline="\n",
    )
    assert calls_named(ast.parse(decoy.read_text(encoding="utf-8")), "_task_of") == ["_task_of"], (
        "扫描器看不见第二处借用 ⇒ 上面那两条断言没有约束力"
    )


def test_the_frozen_segments_still_pass_the_door_and_the_driver_leaves_it_closed():
    """`FROZEN_SEGMENT_GATE` 必须是那道门唯一认的值，而门本身一格没放宽（决定 3）。

    两个方向各挡一种错：
    ① 有人把常量改成 `"doubao"` ⇒ `resolve_transport_flag()` 当场 `StubMisuse`，这一发红（入口不再能进冻结段，
       而不是偷偷放宽门，因为门在 `adapters/stub.py`，本文件改不到它）；
    ② 有人把门放宽了 ⇒ 下面那一圈真厂商名的拒绝断言红。

    顺带量到的一格（本轮现读，不是先验）：那道门把**"没给"与"给了但不认"分成两个代号**
    （`transport_flag_missing` / `transport_flag_unknown`）。这里把两个都钉上，是因为如果哪天并成一个，
    "忘传旗标"与"传了个真厂商名"在 CLI 上就读成同一件事了 —— 而后者才是 R-07 要挡的那一发。
    """
    assert realrun.FROZEN_SEGMENT_GATE == "stub"
    assert resolve_transport_flag(realrun.FROZEN_SEGMENT_GATE) == "stub"
    for rejected in ("doubao", "qoder", "workbuddy", "real", "true"):
        with pytest.raises(StubMisuse) as caught:
            resolve_transport_flag(rejected)
        assert caught.value.code == "transport_flag_unknown", rejected
    for absent in ("", None):
        with pytest.raises(StubMisuse) as caught:
            resolve_transport_flag(absent)  # type: ignore[arg-type]
        assert caught.value.code == "transport_flag_missing", absent


def test_the_stop_nodes_structurally_cannot_write(tmp_path):
    """人工停点的边界（M2-W8 之后）：**只允许开评审那一笔，不许产结论、不许碰验收**。

    旧版这格断的是"闭包里连 `session` 都拿不到"；评审入口落地后 review 边必须把卡推到 `REVIEWING`
    （`validate_artifact.py:27` 那句的写者），所以那条禁词断言按新事实改准，但红线一个字没松：
    - 函数体里不出现 `record_verdict` / `ReviewVerdict` / `accept(` / `ACCEPTING` ⇒ 门禁停点不代评审、
      更不代验收；
    - 开评审只走 `review_node.ACTION_OPENED` 与 `review_node.REVIEWING` 两个常量，不写状态机字面量；
    - handoff 边那一条分支不碰 `session`（测试用一个访问即报错的哨兵证明它在 invalid 状态上不落库）。
    """
    body = func_source("_human_stop")
    for banned in ("record_verdict", "ReviewVerdict", "accept(", "ACCEPTING", "CHANGES_REQUESTED"):
        assert banned not in body, f"停点里出现了 {banned!r} ⇒ 它不再只负责开评审"
    assert "review_node.ACTION_OPENED" in body and "review_node.REVIEWING" in body, (
        "开评审那一笔没有走实体层的常量 ⇒ 状态名/动作名被抄成了第二份"
    )

    class _ExplodingSession:
        def __getattribute__(self, name):
            raise AssertionError(f"handoff 停点碰了会话属性 {name!r} ⇒ 它写了库")

    lines: list[str] = []
    node = realrun._human_stop(
        realrun.EDGE_HANDOFF,
        session=_ExplodingSession(),
        feature_request_id=uuid.uuid4(),
        task_id="T01",
        now=dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc),
        emit_to=lines.append,
    )
    assert node({"validated_status": validate_artifact.INVALID}) == {}
    assert len(lines) == 1 and "RUN AWAIT" in lines[0] and "next=handoff" in lines[0], lines
    assert "validated=invalid" in lines[0] and realrun.STOP_HINT in lines[0], lines

    decoy = tmp_path / "writing.py"
    decoy.write_text(
        "def node(state):\n    review_node = object()\n    review_node.record_verdict(session)\n"
        "    card.status = 'ACCEPTING'\n    return {}\n",
        encoding="utf-8",
        newline="\n",
    )
    text = decoy.read_text(encoding="utf-8")
    assert "record_verdict" in text and "ACCEPTING" in text, (
        "这套禁词吃不到真写法 ⇒ 上面那条禁词断言是空的"
    )


def test_the_two_outgoing_edge_names_are_taken_from_the_gate_table():
    """两条出边的名字**从 `OUT_EDGES` 取**，驱动里不留 `"review"` / `"handoff"` 这两个字面量。

    口径与 `graph.py:64-70` 那段注释同一件事：表与消费者各写一份时，改了出边名字忘了改这里就是一次静默错，
    而 `sorted(OUT_EDGES.values())` 那种取法会让两条边正好互换、图照样能跑 —— 所以这里连取法都钉死。
    """
    assert realrun.EDGE_REVIEW == validate_artifact.OUT_EDGES[validate_artifact.VALID]
    assert realrun.EDGE_HANDOFF == validate_artifact.OUT_EDGES[validate_artifact.INVALID]
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    assigned: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value
    for name in ("EDGE_REVIEW", "EDGE_HANDOFF"):
        value = assigned[name]
        assert not isinstance(value, ast.Constant), f"{name} 被改成了字面量 ⇒ 表与消费者又各一份"
        assert isinstance(value, ast.Subscript), f"{name} 不是从表里取的：{ast.dump(value)}"


def test_the_failure_policy_keeps_the_two_assembly_defects_loud():
    """失败政策（owner 第三步 2）：厂商与协议层的错收住继续跑，**三类该响的必须原样抛**。

    这张真值表里的每一格都是会真实发生的形状，不是把分支凑满：

    - 收：`RuntimeError`（三支 transport 的错误类都从它派生）、`ValueError`、pydantic 的
      `ValidationError`（协议建模失败 —— owner 那句"JSON 不符合 Pydantic 协议"就落在这一格）；
    - 不收：`scheduler.MissingLease`（没派发就跑执行段 = 驱动自己的顺序错了）与
      `scheduler.SlotLeak`（借还不成对，吞掉的代价是"跑几次把并发上限吃光后静默阻塞"）；
    - 不收：`RealRunRefused`（缺凭据 / 不在册 / 形态不可装 / 存档根开不出来）—— 那是**进程级**事实，
      一张卡撞上就说明后面每一张都会同样撞上，逐张重复只会给出一个"跑了 4 张、每张都失败"的假读数
      （owner 第一步那句"必须抛出明确的系统错误并退出"落在这一格）；
    - 不收：库错（`SQLAlchemyError`）—— 库里的事实坏了再往下跑只会多写几行脏的。

    最后那条关系是这条判据的存在理由：**那三支"不收"的也都从 `RuntimeError` 派生**，所以"收不收"完全取决于先后。
    如果有人把 `ASSEMBLY_DEFECTS` / `CONFIG_REFUSALS` 清空，或把 `_swallowable()` 退回成一行
    `isinstance(exc, SWALLOWABLE)`，这条守卫就会红，而不是安静地绿。
    """
    for family, members in (
        ("ASSEMBLY_DEFECTS", realrun.ASSEMBLY_DEFECTS),
        ("CONFIG_REFUSALS", realrun.CONFIG_REFUSALS),
    ):
        assert members, f"{family} 被清空 ⇒ 那份'先后'不再有人兑现"
        for item in members:
            assert any(issubclass(item, base) for base in realrun.SWALLOWABLE), (
                f"{family} 里的 {item.__name__} 不再派生自 RuntimeError/ValueError ⇒ "
                "这份政策的先后不再重要，本判据要跟着改写而不是留着当装饰"
            )

    class _Shape(StrictModel):
        number: int

    with pytest.raises(Exception) as caught:  # pydantic 的 ValidationError
        _Shape.model_validate({"number": "不是一個整数"})
    from pydantic import ValidationError  # 只在需要比对类型时引，不在模块顶引

    assert isinstance(caught.value, ValidationError)
    assert realrun._swallowable(caught.value), "协议建模失败被当成装配缺陷 ⇒ 一发坏报文就能把整条链打死"

    assert realrun._swallowable(RuntimeError("对端 120 秒没回"))
    assert realrun._swallowable(ValueError("报文形状不对"))
    for loud in (
        scheduler.SlotLeak("退了但没退干净"),
        scheduler.MissingLease("台账里没有这张卡"),
        realrun.RealRunRefused("缺凭据", code="real_wiring_refused"),
        KeyError("native_ref"),
        SQLAlchemyError("库挂了"),
        KeyboardInterrupt(),
    ):
        assert not realrun._swallowable(loud), f"{type(loud).__name__} 被吞掉了 ⇒ 这是装配/配置缺陷，不是厂商失败"


def test_the_summary_counts_four_shapes_and_an_empty_reading_is_not_success():
    """`RUN SUMMARY` 那四个数：空读数不许被数成成功。

    `DONE_UNKNOWN` 那一格挡的是最容易出现的假绿：段跑完了但 `validated_status` 读不出（state 里没有那一键）——
    数成 `valid` 就是"门禁过了"的假读数，数成 `error` 又掩盖了"门禁没给结论"这个不同的根因。
    """
    def outcome(card_id: str, **over) -> realrun.CardOutcome:
        base = {
            "card_id": card_id,
            "agent_id": "qoder",
            "validated_status": validate_artifact.VALID,
            "card_status": "RUNNING",
            "artifacts": (("qoder-workspace/T.html", 600, 20, "html"),),
        }
        base.update(over)
        return realrun.CardOutcome(**base)

    counts = realrun._count(
        [
            outcome("T01"),
            outcome("T02", validated_status=validate_artifact.INVALID, card_status="ARTIFACT_INVALID"),
            outcome("T03", validated_status=None, artifacts=(), error="RuntimeError: 超时"),
            outcome("T04", validated_status=None),
        ]
    )
    assert counts == {
        realrun.DONE_VALID: 1,
        realrun.DONE_INVALID: 1,
        realrun.DONE_ERROR: 1,
        realrun.DONE_UNKNOWN: 1,
    }, counts
    assert sum(counts.values()) == 4, "四个名字不覆盖全部读数 ⇒ 有一张卡从摘要里消失"


def test_the_exit_codes_are_borrowed_not_invented():
    """入口的三档退出码全部借 `run.py` 那几个常量，本文件不自造数。

    理由是 owner 2026-10-16 裁决 1 那条形状：**碰任何东西之前就拒**与用法错误同档（2），
    跑起来之后失败才是 1。骨架那一版自己写过 `EXIT_NOT_WIRED = 2`，接线之后那个名字必须消失 ——
    留着它就是在同一族命令里养第二种退出码口径。
    """
    assert not hasattr(realrun, "EXIT_NOT_WIRED"), "骨架那个自造的退出码还在 ⇒ 与 `run` 那三档并行了"
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_numbers = {
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
    }
    assert module_numbers == set(), f"模块顶层自己写了数：{sorted(module_numbers)}"
    assert "run.EXIT_OK" in source and "run.EXIT_RUNTIME" in source and "run.EXIT_USAGE" in source


def test_a_missing_credential_is_refused_by_the_driver_and_never_degrades(tmp_path, monkeypatch):
    """owner 点名那一发（"重点验证：没配 Key 时是否真报错"）在**驱动这一侧**的形状。

    工厂那侧已有判据（`test_transport_spec_factory.py` 节 三 / 节 五），这一格量的是驱动有没有把那个拒绝
    **换成自己的代号并原样说话**，以及反向那一半：凭据齐了它交回来的必须是真 transport 而不是仿真。
    四格各挡一种会发生的错：
    ① 缺 Key ⇒ `RealRunRefused(code="real_wiring_refused")`，消息点名缺的那一支变量名（不说成"库没配"，
       也不说成"厂商没响应"）；缺模型 ID 是**另一格**，同样点名自己的变量名；
    ② 消息里不许带值（哨兵那一份一次都不出现，口径同那两条防泄漏判据）；
    ③ 齐了 ⇒ 类名是 `DoubaoTransport`、模块在 `acp.adapters.doubao`，且不是 `StubAdapter` 的实例；
    ④ 子进程那一支（qoder）同样造得出来，而且驱动递给工厂的第三格**原样**落在 `transport.cwd` 上
       （owner 2026-10-20 裁决 1；驱动不改写它、也不给它补默认）。
    """
    for name in ("ARK_API_KEY", "QODER_PERSONAL_ACCESS_TOKEN", "QODER_SERVICE_ACCOUNT_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(realrun.spec_factory.MODEL_ENV, FAKE_MODEL)

    for agent_id, env_name in (("doubao", "ARK_API_KEY"), ("qoder", "QODER_PERSONAL_ACCESS_TOKEN")):
        with pytest.raises(realrun.RealRunRefused) as caught:
            realrun.real_transport(agent_id, None, tmp_path)  # 工厂在凭据那一关就拒，网关根本不会被读
        assert caught.value.code == "real_wiring_refused", caught.value.code
        assert env_name in str(caught.value), f"消息没点名缺的那一支变量：{caught.value}"

    sentinel = "fixture-should-never-appear-in-any-message"
    monkeypatch.setenv("ARK_API_KEY", sentinel)
    monkeypatch.delenv(realrun.spec_factory.MODEL_ENV, raising=False)
    with pytest.raises(realrun.RealRunRefused) as caught:
        realrun.real_transport("doubao", None, tmp_path)
    assert realrun.spec_factory.MODEL_ENV in str(caught.value), caught.value
    assert "ARK_API_KEY" not in str(caught.value), "缺的是模型 ID，消息却在说凭据 ⇒ 归因换了对象"

    monkeypatch.setenv(realrun.spec_factory.MODEL_ENV, FAKE_MODEL)
    built = realrun.real_transport("doubao", BusGateway(tmp_path), tmp_path)
    assert type(built).__name__ == "DoubaoTransport", type(built).__name__
    assert str(type(built).__module__).startswith("acp.adapters.doubao")
    assert not isinstance(built, StubAdapter), "真装配回落成了仿真"
    assert sentinel not in repr(built) and "Bearer" not in repr(built), "构造品的读数里带着凭据形态的东西"

    # 子进程那一支走同一条路：第三格必须**原样**落到 transport 手里（驱动不改写、不补默认）。
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", sentinel)
    jail = tmp_path / "AI项目台" / "confined"
    jail.mkdir(parents=True)
    qoder_built = realrun.real_transport("qoder", BusGateway(jail), jail)
    assert type(qoder_built).__name__ == "QoderTransport", type(qoder_built).__name__
    assert qoder_built.cwd == jail, f"驱动递给工厂的家没有落到 transport 上：{qoder_built.cwd!r}"
    assert sentinel not in repr(qoder_built), "构造品的读数里带着凭据形态的东西"


def test_the_confined_home_is_the_executors_workspace_dir_inside_the_checked_root(tmp_path):
    """裁决 1 在驱动这一侧的四格现场：`_confine()` 交出的家 = 存档根**里面**那一格的 workspace 目录。

    ① 真 `ensure_archive_root` 跑过（真 git、临时目录、零出网）⇒ 交回 `<那个根>/<executor>-workspace`，
       两张函数不是两套口径（存档根的拼法只有 `archive.archive_root()` 一处，目录名的拼法只有
       `layout.workspace_dir_name()` 一处，本条两侧都按现值比而不是抄字面量）；
    ② 那一个目录在交出去之前**已经被建出来**（外部进程只往它的 cwd 里写，目录不存在它不会替我们建）；
    ③ 目录不存在 ⇒ 拒，代号 `archive_root_missing`（借来的值，见上一条判据里新增的那两格名单）；
    ④ 目录存在但**不是自己的仓库顶层** ⇒ 拒，代号 `archive_root_not_toplevel`。④ 就是"把平台源码交给厂商"
       那条路的入口：总线目录恰好嵌在别人的仓库里时，`rev-parse` 会向上认到那一个，家就不是家。

    这一发同时钉住两件事：**不降级**（坏值绝不会被换成 `archive_root()` 那种"只拼路径不检查"的读法交出去，
    那两条都停在 `_confine` 里抛，一个路径也不会返回）与**不在被拒的家上动手**（③④ 两发的盘上都必须
    没有多出一个 workspace 目录 —— 先建再拒等于把"配置不成立"变成"已经在别人树里写过东西"）。
    """
    bus = tmp_path / "results"
    made = realrun.ensure_archive_root("real_demo", bus, emit_to=lambda line: None)
    home = realrun._confine("real_demo", bus, "qoder")
    assert home == made / layout.workspace_dir_name("qoder"), (
        f"家不是存档根里那一格 workspace：{home} vs {made}"
    )
    assert home.is_dir(), f"_confine 交了一个盘上不存在的家：{home}"

    with pytest.raises(realrun.RealRunRefused) as missing:
        realrun._confine("nope", tmp_path / "nowhere", "qoder")
    assert missing.value.code == "archive_root_missing", missing.value.code
    assert not (tmp_path / "nowhere").exists(), "存档根不存在那一发在盘上建了东西"

    plain = tmp_path / "plain" / "proj"
    plain.mkdir(parents=True)
    with pytest.raises(realrun.RealRunRefused) as not_top:
        realrun._confine("proj", tmp_path / "plain", "qoder")
    assert not_top.value.code == "archive_root_not_toplevel", not_top.value.code
    assert not (tmp_path / "plain" / "proj" / layout.workspace_dir_name("qoder")).exists(), (
        "存档根没被认下来就先把 workspace 建了出来 ⇒ 顺序倒了"
    )


def test_the_home_the_driver_names_is_the_one_the_transport_writes_into(tmp_path, monkeypatch):
    """驱动点的目录名与 transport 自己算的归属目录名**必须是同一个值**（两侧对读，第五发点火的根因）。

    这两处各有各的载体：驱动交家（`realrun._confine` 取 `layout.workspace_dir_name`）、适配器认产物
    （`QoderTransport._workspace_dir()`，同一支函数）。它们一旦漂开，症状不是"路径不对"而是
    **"厂商跑成功、产物一条都不登记"**——2026-10-20 第五发就是这个形状：家交在存档根顶层，
    `collect()` 按 `qoder-workspace/` 判归属，`styles.css` 被 FR-013 拒收。

    所以这一发不走字面量：它把**真构造品**（凭据注入替身、零出网）交出来的 `cwd` 与它自己那条
    `startswith` 判据要用的那一个目录名对读。变异 = 把任一侧的名字改掉 ⇒ 红。
    """
    bus = tmp_path / "results"
    realrun.ensure_archive_root("real_demo", bus, emit_to=lambda line: None)
    jail = realrun._confine("real_demo", bus, "qoder")
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "sentinel-for-shape-only")
    built = realrun.real_transport("qoder", BusGateway(jail.parent), jail)
    assert type(built).__name__ == "QoderTransport", type(built).__name__
    assert built.cwd == jail
    assert pathlib.Path(built.cwd).name + "/" == built._workspace_dir() + "/", (
        "驱动交的那一格与适配器认的那一格不是同一个目录名 ⇒ FR-013 会把成功读成越界"
    )
    assert pathlib.Path(built.cwd).is_dir(), "构造用的家在盘上不存在（真起进程时这一格就是 FileNotFoundError）"


def test_the_entry_canonicalizes_the_default_relative_bus_root(tmp_path, monkeypatch):
    """命令行上省掉 `--bus-root`（默认值是**相对**的 `results`）⇒ 交下游的那一格必须是绝对路径。

    这一格不是整洁问题，是判定问题：`_bus_path` 只在"厂商报的绝对串落在总线根下面"时做换算，
    而总线根留着相对写法时那个前缀**永远不相等** ⇒ 每一次换算都失败 ⇒ 一次成功的执行整批被判越界。
    第五发点火的读数就是这条链（消息里那条路径以 `D:/…` 开头，正是"没换算"的指纹）。
    顺带钉住入口说给人那一条 `NEXT` 命令：它带的 `--bus-root` 现在是绝对的，从任何目录敲回去都成立。

    受理段仍被 mock（它要 checkpointer，归 `test_run_chain.py` 那批），厂商工厂注入的是记账假件 ⇒ 零出网。
    """
    monkeypatch.chdir(tmp_path)
    seen: list[pathlib.Path] = []

    def fake_intake(raw_input, **kwargs):
        seen.append(kwargs["bus_root"])
        return run.IntakeReport(
            feature_request_id=uuid.uuid4(),
            ticket_id=uuid.uuid5(uuid.NAMESPACE_DNS, "ticket-pen-z"),
            card_ids=("T01",),
            request_status="SPLIT",
        )

    monkeypatch.setattr(run, "run_intake", fake_intake)
    lines: list[str] = []
    code = realrun.main(
        ["--project", "real_demo", "--request", "做个静态网站"],
        emit_to=lines.append,
        session_factory=lambda: (lambda: FakeSession()),
        transport_factory=lambda agent_id, gateway, cwd: object(),
    )
    assert code == run.EXIT_OK, lines
    assert seen and seen[0].is_absolute(), f"入口把相对总线根原样交下去了：{seen}"
    assert seen[0] == (tmp_path / "results").resolve()
    joined = "\n".join(lines)
    assert f"--bus-root {tmp_path / 'results'}" in joined, lines


def test_the_driver_invents_exactly_eight_codes_and_borrows_the_rest(tmp_path):
    """真入口自造的拒绝代号**只有八个**，其余一律从 `run.py` 那一份现值借。

    为什么这份名单写在判据里而不是 `realrun.py` 顶部：那种"只给判据读的常量"在 src 里是第二个载体
    （代码用的是字面量，常量只是它的一份副本），而本仓对这种形状的态度写在 `run.py` 那段
    "这里只声明**本模块自己抛**的那三个"的注释里 —— 那里那三个常量是**被 raise 用的**，不是目录。
    所以这里读 AST，名单只在测试这一侧：新加一个自造代号 ⇒ 这一格红，逼写的人回来登记一条。
    诱饵那一发证明这套读法吃得到一个没登记的代号（不是"扫不到所以绿"）。

    名单从五个到七个的那两格 = 2026-10-20 第一发点火之后加的两道闸。
    **函数名此前一直写着 "three" 而名单里已经是五个** —— 名字是过期的，这一次连同数量一起改准，
    记在 `evidence/phase-15.md` §十二（判据文件自己的散文也会说谎，这一格是又一发实测）。

    第八个 `orphaned_running`（2026-10-28）：停在 `RUNNING` 却没人推的卡被收回时用的代号
    （`reclaim_orphaned_cards`）。它与上面七个不同类 —— 那七个是**入口拒绝**，这个是**一张卡的处境**；
    放同一份名单里是因为这一格数的是"`realrun.py` 自造的字面量"，不分类别。
    """
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "code" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    }
    assert used == {
        "archive_root_is_platform_repo",
        "bus_root_project_duplicated",
        "credential_missing",
        "git_env_overrides_set",
        "git_init_did_not_take",
        "git_init_failed",
        "orphaned_running",
        "real_wiring_refused",
    }, sorted(used)
    for borrowed in (run.USAGE_MISSING, run.CARDS_MISSING, run.CHAIN_ROUND_LIMIT):
        assert borrowed not in used, f"驱动把从 `run` 借来的代号又抄了一遍字面量：{borrowed}"
    # 家（`cwd`）那一格借的是 `archive.ArchiveRootMisuse.code` 的两个值 ⇒ 驱动里不许出现这两串字面量，
    # 否则"存档根不成立"在两个模块里各有一个名字，改一处就会让另一处说谎。名单只在判据这一侧，同上一条。
    for borrowed in ("archive_root_missing", "archive_root_not_toplevel"):
        assert borrowed not in used, f"驱动重述了 `archive` 的代号：{borrowed}"

    decoy = tmp_path / "typo.py"
    decoy.write_text('raise RealRunRefused("x", code="archive_root_typo")\n', encoding="utf-8", newline="\n")
    decoy_used = {
        kw.value.value
        for node in ast.walk(ast.parse(decoy.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "code" and isinstance(kw.value, ast.Constant)
    }
    assert decoy_used == {"archive_root_typo"}, "读不到 `code=` 的字面量 ⇒ 上面那条名单断言是空对空"


# ------------------------------------------------------------------ 二、决定 2：自动开根


def test_ensure_archive_root_creates_a_root_the_frozen_intake_segment_accepts(tmp_path):
    """本入口建的那个根，**必须能被冻结段自己的检查放行**（`require_archive_root` 不抛）。

    这条是把决定 2 与冻结代码接起来的那一发：只断"目录建了 + git init 退 0"不够，
    因为 `require_archive_root()` 要的是"这个目录自己是仓库顶层"。跑真 git，临时目录，零出网。
    """
    lines: list[str] = []
    bus_root = tmp_path / "results"
    target = realrun.ensure_archive_root("real_demo", bus_root, emit_to=lines.append)

    assert target == bus_root / "real_demo", target
    assert (target / ".git").is_dir(), "`.git` 不在 ⇒ init 没真发生"
    resolved = archive.require_archive_root("real_demo", bus_root=str(bus_root))
    assert resolved.resolve() == target.resolve(), resolved
    assert "RUN INIT" in lines[0] and "git=init" in lines[0], lines

    subprocess.run(
        ["git", "status", "--short"], cwd=str(target), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def test_ensure_archive_root_is_reentrant_and_keeps_what_is_on_disk(tmp_path):
    """同一个根跑第二次：不重复 init、不抹已有文件、报的是"已有仓库"。

    挡的错是真会发生的：owner 第二次点火时如果入口把存档根重建一遍，第一次那批产物与存档历史就没了，
    而看起来"这一发跑得比上一发干净"。
    """
    bus_root = tmp_path / "results"
    _, sink = _sink()
    target = realrun.ensure_archive_root("p1", bus_root, emit_to=sink)
    (target / "keep.txt").write_text("第一次的产物\n", encoding="utf-8")

    lines: list[str] = []
    again = realrun.ensure_archive_root("p1", bus_root, emit_to=lines.append)
    assert again == target
    assert "已有仓库" in lines[0] and "made_dir=False" in lines[0], lines
    assert (target / "keep.txt").is_file(), "第二次进来把盘上的东西清掉了"


def test_a_git_init_that_fails_or_lies_is_refused_not_guessed(tmp_path):
    """`git init` 退非零 ⇒ 拒；退 0 但顶层没变 ⇒ 也拒。两格各挡一种"看起来成功了"。

    第二格是本笔最重要的防御：`init` 退 0 不代表那个目录成了顶层（外面套着一层仓库时它仍然不是）。
    放过去的下场是 `cadence` 的 `add -A` 打在别人的工作树上 —— 那正是 `archive/root.py` 模块头点名的
    "最坏的一次误伤，而且它一路都是退出码 0"。所以这里**要求再读一次顶层**，不是信退出码。
    """
    _, sink = _sink()
    with pytest.raises(realrun.RealRunRefused) as caught:
        realrun.ensure_archive_root("boom", tmp_path / "r1", emit_to=sink, git=lambda argv, cwd: 128)
    assert caught.value.code == "git_init_failed", caught.value.code
    assert "128" in str(caught.value)

    not_taken = tmp_path / "r2"
    with pytest.raises(realrun.RealRunRefused) as again:
        realrun.ensure_archive_root("still", not_taken, emit_to=sink, git=lambda argv, cwd: 0)
    assert again.value.code == "git_init_did_not_take", again.value.code

    # 诱饵必须**真去 init**（只记 argv 不干活的那一支会撞上上面那条 `git_init_did_not_take`），
    # 于是这里包一层真执行器：量的是"发出去的 argv 恰是那一条"，不是"随便编一个都会被收下"。
    argvs: list[list[str]] = []

    def capturing(argv: list[str], cwd: pathlib.Path) -> int:
        argvs.append(list(argv))
        return realrun._run_git(argv, cwd)

    realrun.ensure_archive_root("p3", tmp_path / "r3", emit_to=sink, git=capturing)
    assert argvs == [["init", "-q", "."]], f"本入口的 git 面不止 init：{argvs}"


def test_the_platform_repository_itself_is_refused_as_an_archive_root(tmp_path):
    """存档根解析成平台仓库根（或它的祖先）⇒ 拒，而且是在 `mkdir` 之前。

    反向也要量：默认那个"嵌在平台仓库里"的 `results/<project>` 是**允许**的 —— 否则这条防护会把正常用法
    一起拦死，而那就变成"为了能跑而放宽防护"的前一步。
    """
    repo = realrun.platform_repo_root()
    assert repo.is_dir(), "读不到平台仓库根 ⇒ 下面两条断言都在比空气"
    with pytest.raises(realrun.RealRunRefused) as caught:
        realrun._refuse_if_platform_repo(repo)
    assert caught.value.code == "archive_root_is_platform_repo"
    with pytest.raises(realrun.RealRunRefused):
        realrun._refuse_if_platform_repo(repo.parent)

    realrun._refuse_if_platform_repo(repo / "results" / "real_demo")
    realrun._refuse_if_platform_repo(tmp_path / "outside")


def test_a_usage_error_leaves_nothing_on_disk(tmp_path):
    """命令行不成立 ⇒ 一个目录都不建、一句 `git` 都不跑。

    这条钉的是 `main()` 里"形态先于路径"那个顺序。反过来的写法（先 `ensure_archive_root` 再判形态）
    也能返回 2，但会在盘上留下一个空仓库，而那个仓库之后会被 `--project` 复用 —— 现场就这么脏的。
    """
    bus_root = tmp_path / "results"
    calls: list = []
    for argv in (["--project", "p", "--bus-root", str(bus_root)], ["--project", "p", "--request", "   "]):
        lines: list[str] = []
        assert realrun.main(argv, emit_to=lines.append, git=lambda a, c: calls.append(a) or 0) == run.EXIT_USAGE
        assert "arguments_missing" in lines[0], lines
    assert not bus_root.exists(), "用法错误那一发已经动了盘"
    assert calls == []


def test_the_missing_dsn_is_named_and_no_vendor_is_blamed(tmp_path, monkeypatch):
    """没有 `ACP_DB_DSN` ⇒ `credential_missing` 点名变量名，**不说成厂商的事、也不降级**。

    两个方向：
    ① 消息里必须出现变量名（否则运维会去查厂商）；
    ② 消息里不许出现厂商凭据名、`transport_flag_unknown` 或"用仿真继续"（那是把"库没配"读成"厂商没配"，
       或干脆给自己找一个静默降级的出口）。
    这里是**抽走**变量（`monkeypatch` 会还回去），不是往里注入 —— 口径同那条"注入会掩盖运行时缺 `.env` 起不来"。
    """
    monkeypatch.delenv(DSN_ENV, raising=False)
    lines: list[str] = []
    code = realrun.main(
        ["--project", "p9", "--request", "做个静态网站", "--bus-root", str(tmp_path / "results")],
        emit_to=lines.append,
    )
    assert code == run.EXIT_USAGE, code
    assert any("credential_missing" in line and DSN_ENV in line for line in lines), lines
    joined = " ".join(lines)
    for wrong_blame in ("ARK_API_KEY", "QODER_PERSONAL_ACCESS_TOKEN", "transport_flag_unknown", "Doubao"):
        assert wrong_blame not in joined, f"{wrong_blame} 出现在缺库的读数里 ⇒ 归因换了对象"


def test_a_second_resume_continues_with_the_remaining_ready_cards(tmp_path, monkeypatch):
    """评审通过后卡解锁，但受理图的等待点已经消费掉 ⇒ 第二次 `--resume` 必须能接着跑。

    旧行为是 `no_waiting_point` 直接拒，于是"评审通过 → 下游卡变 READY"之后没有任何 CLI 入口能把
    它们跑下去。现在这一发只把恢复段的拒绝换成 `RUN CONTINUE`，执行段照旧走 `run_real` ——
    不是绕过恢复，而是"恢复无事可做时继续推进剩余可派发的卡"。
    """
    request_id = uuid.uuid4()
    called: list[uuid.UUID] = []

    def no_waiting(*_args, **_kwargs):
        raise run.RunFailure("thread 上没有等待点", code=run.NO_WAITING_POINT)

    def fake_real(session, *, feature_request_id, **_kwargs):
        called.append(feature_request_id)
        return []

    monkeypatch.setattr(run, "run_resume", no_waiting)
    monkeypatch.setattr(realrun, "run_real", fake_real)
    lines: list[str] = []
    code = realrun.main(
        ["--project", "real_demo", "--resume", str(request_id), "--bus-root", str(tmp_path / "results")],
        emit_to=lines.append,
        session_factory=lambda: (lambda: FakeSession()),
    )
    assert code == run.EXIT_OK, lines
    assert called == [request_id]
    joined = "\n".join(lines)
    assert "RUN CONTINUE" in joined and f"request={request_id}" in joined and run.NO_WAITING_POINT in joined
    assert "RUN SUMMARY" in joined and "cards=0" in joined


def test_the_request_mode_hands_over_the_ticket_and_calls_no_vendor(tmp_path, monkeypatch):
    """`--request` 那一发的交付契约：打印单据号 + 一条**可以再敲回去**的续跑命令，且一个厂商都不碰。

    这里 mock 掉的是**冻结的受理段本身**（它要 checkpointer，归 `test_run_chain.py` 那批判据量），
    量的是入口对人说的那三行 —— 那三行是 owner 在看板/CLI 手动批准时的唯一输入，所以它得可解析。
    注入位 `transport_factory` 装一个记账的假件：被调过就是这条的红。
    """
    request_id = uuid.uuid4()
    ticket_id = uuid.uuid5(uuid.NAMESPACE_DNS, "ticket-under-test")
    made: list[FakeSession] = []

    def fake_intake(raw_input, **kwargs):
        assert kwargs["transport"] == realrun.FROZEN_SEGMENT_GATE, "入口递给冻结段的旗标变了"
        assert kwargs["bus_root"] == (tmp_path / "results").resolve(), (
            "入口换了一次根 ⇒ 产物落点不再是那条命令说的地方"
        )
        return run.IntakeReport(
            feature_request_id=request_id,
            ticket_id=ticket_id,
            card_ids=("T01", "T02", "T03", "T04"),
            request_status="SPLIT",
        )

    monkeypatch.setattr(run, "run_intake", fake_intake)
    built: list[str] = []

    def session_factory():
        """形状对齐 `make_session_factory()`：这个函数交回的是**工厂**，工厂每次调用给一个新会话。"""

        def make():
            session = FakeSession()
            made.append(session)
            return session

        return make

    lines: list[str] = []
    code = realrun.main(
        ["--project", "real_demo", "--request", "做个静态网站", "--bus-root", str(tmp_path / "results")],
        emit_to=lines.append,
        session_factory=session_factory,
        transport_factory=lambda agent_id, gateway, cwd: built.append(agent_id) or object(),
    )
    assert code == run.EXIT_OK, lines
    assert len(made) == 1 and made[0].commits == 1, "工作单元的提交点不在装配方这里 ⇒ 那一发没落在一个会话里"
    assert built == [], "受理段就去碰厂商了"
    joined = "\n".join(lines)
    assert f"ticket={ticket_id}" in joined and "RUN AWAIT :: stage=A_SPLIT" in joined, lines
    assert f"--resume {request_id}" in joined and "python -m acp.approve approve" in joined, lines
    assert (tmp_path / "results" / "real_demo").is_dir(), "受理段之前没把存档根建出来"


def test_the_two_git_executors_decode_the_same_way():
    """本入口的 `_run_git` 与冻结的 `cadence.run_git` 必须是**同一张**解码口径。

    为什么要有这一条：`git init` 失败时 git 会把路径原样写进 stderr，而本机区域编码是 cp936、
    git 写的是 UTF-8 ⇒ 不钉 `encoding` 的 `text=True` 在读流线程里当场抛 `UnicodeDecodeError`
    （下面那发行为档记的就是这条实测）。这一抛发生在 `main()` 开会话**之前**，不在
    `SWALLOWABLE` 的任何拒绝分支覆盖范围内 ⇒ owner 看到的是一段 traceback 顶掉
    `RUN FAIL :: code=git_init_failed`。

    **比的是两份源码相等，不是比一个写死的字面量**：所以这条钉的是"两份不许各解各的"，
    不钉"必须是 utf-8"（那半由 `cadence` 自己的判据管）。两份一起改 ⇒ 这条仍绿，是刻意的 ——
    口径只有一个，执行器有两个。
    """
    ours = _call_keywords(func_source("_run_git"), "_run_git", "run")
    frozen = _call_keywords(
        (SRC_ROOT / "archive" / "cadence.py").read_text(encoding="utf-8"), "run_git", "run"
    )
    for name in ("encoding", "errors", "check"):
        assert name in ours, f"`_run_git` 少了关键字 {name} ⇒ 与 `cadence.run_git` 分叉成两份口径"
        assert ours[name] == frozen[name], f"{name}：本入口 {ours[name]!r} 对不上冻结侧 {frozen[name]!r}"


def test_a_git_failure_that_breaks_the_locale_codec_still_returns_a_code(tmp_path):
    """中文存档根 + git 失败消息 ⇒ 执行器交回的是退出码，不是把解码异常抛给调用方。

    实测形态 = `-C <一个中文目录名>` 打一条不存在的目录（128），本机修复前那一条会牵动读流线程抛
    `UnicodeDecodeError`（`evidence/phase-15.md` §七 有六种形态的开火/不开火对照）。

    **这一格证明到哪要点名**：`returncode` 修复前后都是 128 ⇒ 它不是缺陷的开火对照，
    它是"修完没把执行器弄坏、也没顺手改掉退出码"的回归档；开火对照是上面那条口径一致用例
    （变异 = 删 `encoding=` ⇒ 那条红）。
    """
    missing = tmp_path / "编码会撞车的一个目录名"
    assert realrun._run_git(["-C", str(missing), "rev-parse", "--show-toplevel"], tmp_path) == 128


# ---- 2026-10-20 第一发点火烧到的两个形状（落点与逐条实测 = `evidence/phase-15.md` §十二）----


def test_a_bus_root_that_repeats_the_project_key_is_refused_before_the_disk_moves(tmp_path):
    """`--bus-root` 的末段与 `--project` 同名 ⇒ 拒，而且拒之前盘上一个字节都没动。

    这一格挡的是 owner 2026-10-20 那一发的**命令形状**（`--bus-root results/real_demo --project real_demo`
    ⇒ 存档根 `results/real_demo/real_demo`）。为什么不只改文档：那条命令的产物会落进一个谁都没点名的目录，
    而 `recover --from-file-bus` 与交付包核验读的是外层那一个 ⇒ 出现"跑通了但找不到产物"，归因还要错一层。
    静默把末段剥掉也不做：那条拼法的载体只许是 `archive.archive_root()`（`params-ref §10`），
    在入口里纠偏就是写第二份拼法。

    反向对照在同一发里：`results/other` 那种"外层根带别的名字"必须放行 —— 只判逐字相等那一种形状，
    拿宽度当强度会把合法用法一起拦死。
    """
    calls: list = []
    doubled = tmp_path / "results" / "real_demo"
    with pytest.raises(realrun.RealRunRefused) as caught:
        realrun.ensure_archive_root(
            "real_demo", doubled, emit_to=lambda s: None,
            git=lambda a, c: calls.append(a) or 0, env={},
        )
    assert caught.value.code == "bus_root_project_duplicated", caught.value.code
    assert calls == [], "拒之前已经发了 git ⇒ 那道防护铺在了 init 后面"
    assert not doubled.exists() and not (tmp_path / "results").exists(), "拒了但目录已经建出来了"
    message = str(caught.value)
    assert f"{str(doubled / 'real_demo')!r}" in message, message
    assert str(doubled.parent) in message and "params-ref.md §10" in message, message

    realrun.ensure_archive_root(
        "real_demo", tmp_path / "results", emit_to=lambda s: None,
        git=lambda a, c: calls.append(a) or 0, env={}, probe=lambda p: (p, 0),
    )
    assert (tmp_path / "results" / "real_demo").is_dir(), "合法那一侧被一起拦掉了"

    lines, sink = _sink()
    assert realrun.main(
        ["--project", "real_demo", "--request", "做个静态网站", "--bus-root", str(doubled)],
        emit_to=sink, git=lambda a, c: calls.append(a) or 0,
    ) == run.EXIT_USAGE
    assert "code=bus_root_project_duplicated" in lines[0], lines
    assert calls == [], "入口这一发也没碰 git"

    # `--bus-root` 的那句 help 是人最先读的东西：拼法与权威出处都要在里面，不能只写在拒绝消息里。
    help_source = func_source("build_parser")
    assert "<bus_root>/<project_key>" in help_source and "params-ref.md §10" in help_source, help_source


def test_a_git_env_override_is_refused_by_name_and_never_by_value(tmp_path):
    """`GIT_DIR` / `GIT_WORK_TREE` / `GIT_CEILING_DIRECTORIES` 任一在位 ⇒ 拒，消息里只有变量名。

    这一族是"防 git 穿透"那一格：三种覆盖都会让 `git init` 与顶层探测打到**别的**仓库上，
    而本入口要的形状是"存档根自己就是顶层"。拒在 `mkdir` 之前，理由同上一条：拒在之后就留了脏目录。

    值一律不进消息 —— 那是别处的路径，会被日志文件接住（口径同宪法「二」对凭据的那一条，
    路径串这里也按同一规矩办）。所以下面那发用哨兵串专门验"注入发生了但没抄出去"。
    两个反向对照各挡一种恒真写法：**空白值按没设处理**（否则 unset 不彻底的 shell 会被永久拦死）、
    **名单外的变量在位不影响点火**（否则这道闸就成了"环境里有任何变量都不许"）。
    """
    sentinel = str(tmp_path / "SENTINEL-别处的仓库/.git")
    calls: list = []
    for name in realrun.GIT_OVERRIDE_ENV_NAMES:
        with pytest.raises(realrun.RealRunRefused) as caught:
            realrun.ensure_archive_root(
                "p", tmp_path / f"bus-{name}", emit_to=lambda s: None,
                git=lambda a, c: calls.append(a) or 0, env={name: sentinel},
            )
        assert caught.value.code == "git_env_overrides_set", (name, caught.value.code)
        assert name in str(caught.value), name
        assert sentinel not in str(caught.value), f"{name} 的**值**被抄进了消息：别处的路径进了日志"
    assert calls == [], "覆盖变量在位时仍然发了 git"
    assert not (tmp_path / f"bus-{realrun.GIT_OVERRIDE_ENV_NAMES[0]}").exists(), "拒在 mkdir 之后"

    realrun.ensure_archive_root(
        "ok", tmp_path / "blank", emit_to=lambda s: None,
        git=lambda a, c: calls.append(a) or 0, env={"GIT_DIR": "   "}, probe=lambda p: (p, 0),
    )
    realrun.ensure_archive_root(
        "ok2", tmp_path / "outside-vars", emit_to=lambda s: None,
        git=lambda a, c: calls.append(a) or 0,
        env={"HOME": sentinel, "GIT_CONFIG_GLOBAL": sentinel}, probe=lambda p: (p, 0),
    )


def test_a_lying_init_now_names_which_way_it_failed(tmp_path):
    """`git init` 退 0 而顶层仍不成立 ⇒ 那条消息要能**归因**，不是"读到 None"让人猜。

    五格原因词各挡一种独立坏法，全部用注入的探测读数打 —— 这是 owner 那一发的直接后果：
    `init` 退 0、`.git` 明明在盘上、而 `rev-parse` 退 128（属主检查），当时那一条只说"读到 None"，
    没有任何一个字指向成因。默认探测那一格（真 git、真退出码）在下一条用例里单开。

    两条共同的口径：消息里**不许出现 git 的原文**（`fatal:` 那种），只许出现原因词、变量名、
    路径与退出码 —— 与 `archive/root.py` 模块头那条消息口径同向。
    """
    outer = tmp_path / "一个外层的仓库"
    cases = (
        ((None, None), "probe_did_not_run", False),
        ((None, 128), "git_dir_not_at_target", False),
        ((None, 128), "toplevel_probe_refused", True),
        ((None, 0), "toplevel_printed_nothing", True),
        ((outer, 0), "toplevel_is_another_repo", True),
    )
    seen_causes: set[str] = set()
    for index, (reading, cause, has_git_dir) in enumerate(cases):
        target = tmp_path / f"cause-{index}"
        if has_git_dir:
            target.mkdir(parents=True)
            (target / ".git").mkdir()
        with pytest.raises(realrun.RealRunRefused) as caught:
            realrun.ensure_archive_root(
                target.name, target.parent, emit_to=lambda s: None,
                git=lambda argv, cwd: 0, probe=lambda _p, r=reading: r, env={},
            )
        assert caught.value.code == "git_init_did_not_take", (cause, caught.value.code)
        message = str(caught.value)
        assert f"原因词={cause}" in message, (cause, message)
        assert "fatal:" not in message, "git 的原文被抄进了消息 ⇒ 换成路径以外的东西进日志了"
        assert "本入口不删" in message, (cause, message)
        seen_causes.add(cause)
    assert len(seen_causes) == len(cases), "五格读出来是同一格 ⇒ 原因词没有鉴别力"
    assert str(outer) in str(caught.value), "读到别的外层顶层时要点名那一个"


def test_the_default_probe_carries_a_real_exit_code(tmp_path):
    """默认探测那一格必须**真吃 git 的退出码**：注入位永远验不到这一发，所以单开真 git。

    ① 一个真仓库 ⇒ `(顶层路径, 0)`；
    ② 一个 `.git` 是坏 gitfile 的目录 ⇒ `(None, 非零)`，而且它**不会**退回祖先仓库（git 认下了那个
       `.git` 才失败的）⇒ 这一发同时把 `toplevel_probe_refused` 与 `git_dir_not_at_target` 分开了；
    ③ 拿 ② 那个目录走一遍 `ensure_archive_root`（init 用桩交回 0）⇒ 消息里出现的是**真**退出码；
    ④ 源码面：argv 仍然出自 `archive.repository_toplevel()`，本入口不许出现第二个 `rev-parse` 载体。
    """
    repo = tmp_path / "in" / "side"
    realrun.ensure_archive_root("side", tmp_path / "in", emit_to=lambda s: None, env={})
    top, code = realrun._read_toplevel(repo)
    assert code == 0 and top is not None and top.resolve() == repo.resolve(), (top, code)

    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / ".git").write_bytes(b"invalid gitfile, not a repository\n")
    junk_top, junk_code = realrun._read_toplevel(junk)
    assert junk_top is None and junk_code not in (None, 0), (junk_top, junk_code)

    with pytest.raises(realrun.RealRunRefused) as caught:
        realrun.ensure_archive_root(
            "junk", tmp_path, emit_to=lambda s: None, git=lambda argv, cwd: 0, env={}
        )
    assert caught.value.code == "git_init_did_not_take", caught.value.code
    message = str(caught.value)
    assert "原因词=toplevel_probe_refused" in message, message
    assert f"探测退出码={junk_code}" in message and f"退 {junk_code}" in message, message

    source = func_source("_read_toplevel")
    assert "runner=" in source, source
    # **按 AST 判，不按字符串判**：`rev-parse` 这个词在本函数的说明里就要复述一次（"不自己拼 rev-parse"），
    # 拿 `"rev-parse" not in source` 去判会挡住自己的散文而放过真的第二载体 —— 口径同那条
    # "grep 命中数≠调用点数"。第二载体的形状是"这里自己起了一发 subprocess"，那个才是要拦的。
    hits = set(calls_named(ast.parse(source), "run", "Popen", "check_output",
                           "repository_toplevel", "run_git"))
    assert {"repository_toplevel", "run_git"} <= hits, hits
    assert not hits & {"run", "Popen", "check_output"}, f"探测里自己起了 subprocess ⇒ 那条 argv 有了第二个载体：{hits}"


# ------------------------------------------------------------------ 三、真 PG：驱动的库内读数


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整节 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


class FakeVendorTransport:
    """一支只干"把一份文件经同一个网关落盘、交回实测值"的假厂商。

    它**不是** transport 协议的合格实现（没有 `describe()` / `cancel()`）—— 这是刻意的：
    驱动多调一个入口就 `AttributeError`，于是"驱动以为厂商还有别的能力"这种猜想自动被否掉。
    落盘走 `gateway.write_and_measure()`（与真 transport 同一支函数）⇒ EN-06 那三个数是盘上量出来的，
    不是我报的（FR-019 的口径在这一发里也得保持）。
    """

    def __init__(
        self, agent_id: str, gateway, *, failing: tuple[str, ...] = (), tiny: tuple[str, ...] = ()
    ) -> None:
        self.agent_id = agent_id
        self.gateway = gateway
        self.failing = failing
        self.tiny = tiny
        self.seen: list[Task] = []

    def submit(self, task: Task) -> Handle:
        self.seen.append(task)
        if task.task_id in self.failing:
            raise RuntimeError(f"假厂商在 {task.task_id} 这一发报失败（fixture-only）")
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=f"fixture-{task.task_id}",
            submitted_at=NOW,
        )

    def collect(self, handle: Handle) -> TaskResult:
        task = self.seen[-1]
        fmt = str(task.artifact_floor.format)
        body = _tiny_body(fmt) if handle.task_id in self.tiny else _good_body(fmt)
        rel = artifact_path(self.agent_id, f"{handle.task_id}.{fmt}")
        actor = acl.Actor(
            actor_id=f"{acl.ADAPTER_OWNER_PREFIX}{self.agent_id}", role_id=acl.NO_ROLE_DECLARED
        )
        capability = acl.Capability(agent_id=self.agent_id, task_id=handle.task_id)
        measured = self.gateway.write_and_measure(actor, rel, body, capability)
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status="success",
            artifacts=[measured],
            log_excerpt="fixture",
            self_assessment="fixture 自评，不参与判定",
        )


def _cards_of(session, request_id: uuid.UUID) -> dict[str, str]:
    return {
        str(row.id): str(row.status)
        for row in session.query(TaskCard).filter(TaskCard.feature_request_id == request_id).all()
    }


def test_the_real_segment_stops_at_the_gate_and_writes_no_verdict(bus, session):
    """M2-W8 之后的停点：门禁判 valid ⇒ 卡进 `REVIEWING`，而 `review_verdict` 仍零行。

    这一发是评审入口的前件，六格读数各挡一种说谎：
    ① 卡态是 `REVIEWING`（开评审那一笔由驱动写）；**不是** `ACCEPTING` —— 结论还没人交；
    ② EN-06 那一行的三个数是实测值（`bytes` / `lines` 与盘上那份一致），且 `writer_agent_id` = 执行岗；
    ③ `review_verdict` 表**一行都没有** ⇒ "等待人工评审"不是"替评审岗签了个字"；
    ④ 门禁的结论进了库（`validated_status=valid`），`invalid_reason` 为空；
    ⑤ 槽与台账收尾归零（借还成对），日志里有 `SUBMIT` / `COLLECT` / `GATE` / `AWAIT` 四行；
    ⑥ EN-14 上有且只有一条 `action=review_opened` 的开门留痕。
    """
    request_id = _chain.to_resumed(bus, session)
    built: list[FakeVendorTransport] = []
    seen_cwd: list[pathlib.Path] = []

    def factory(agent_id, gateway, cwd):
        transport = FakeVendorTransport(agent_id, gateway)
        built.append(transport)
        seen_cwd.append(cwd)
        return transport

    lines: list[str] = []
    try:
        outcomes = realrun.run_real(
            session,
            feature_request_id=request_id,
            project_key=_chain.PROJECT,
            bus_root=bus.path,
            now=NOW,
            emit_to=lines.append,
            transport_factory=factory,
        )
        assert [item.card_id for item in outcomes] == ["T01"], outcomes
        first = outcomes[0]
        assert first.error is None and first.validated_status == validate_artifact.VALID, first
        assert first.card_status == review_node.REVIEWING, (
            f"卡态读成 {first.card_status} ⇒ 评审入口的前件没有落下"
        )
        assert _cards_of(session, request_id)["T01"] == review_node.REVIEWING

        rows = list(session.query(Artifact).filter(Artifact.task_card_id == "T01").all())
        assert len(rows) == 1, rows
        on_disk = (bus.path / _chain.PROJECT / rows[0].rel_path).read_text(encoding="utf-8")
        assert rows[0].bytes == len(on_disk.encode("utf-8")), "EN-06 那个字节数不是盘上的那份"
        assert rows[0].lines == len(on_disk.splitlines()), rows[0].lines
        assert str(rows[0].validated_status) == validate_artifact.VALID and rows[0].invalid_reason is None
        assert rows[0].writer_agent_id == built[0].agent_id

        assert session.query(ReviewVerdict).filter(ReviewVerdict.feature_request_id == request_id).count() == 0, (
            "驱动替审查岗写了结论 ⇒ 决定 1 被破坏"
        )
        task = built[0].seen[0]
        assert task.assignee.agent_id == built[0].agent_id and task.acceptance_criteria, task
        # 递给工厂的第三格 = 子进程的家（owner 2026-10-20 裁决 1）。它必须是**那个项目自己的**存档根，
        # 不是平台仓库根、也不是调用方的 cwd —— 这一格在驱动侧只有这一个来源：`require_archive_root()`。
        root = bus.path / _chain.PROJECT
        assert seen_cwd == [root / layout.workspace_dir_name(t.agent_id) for t in built], (
            f"驱动交出的家不是存档根里那一格 workspace：{seen_cwd}"
        )
        assert seen_cwd[0].is_dir() and seen_cwd[0] != realrun.platform_repo_root(), seen_cwd
        joined = "\n".join(lines)
        for expect in ("RUN SUBMIT", "RUN COLLECT", "RUN GATE", "RUN AWAIT", "next=review"):
            assert expect in joined, expect
        assert "ACCEPTING" not in joined, "停点自己宣布了一个没发生过的态"
        opened = [
            event
            for event in session.query(EventLog)
            .filter(EventLog.entity == "task_card", EventLog.entity_id == "T01")
            .all()
            if (event.payload or {}).get("action") == review_node.ACTION_OPENED
        ]
        assert len(opened) == 1, f"开评审留痕不是恰好一条：{[event.payload for event in opened]}"
        assert first.artifacts and first.artifacts[0][0].endswith(f".{task.artifact_floor.format}"), (
            f"产物后缀不跟卡声明的格式走：{first.artifacts}"
        )
    finally:
        _chain.drop_thread(session, request_id)


def test_four_passes_unlock_the_chain_and_the_last_one_assembles_the_package(bus, session):
    """完整闭环：真跑停评审 → 人点通过 → 验收 → 下一张卡解锁；最后一张过审时组出唯一交付包。

    这一发把四轮的"执行 / 开评审 / 收结论 / 验收"走完，断言**库里的终态**而不是响应体：
    四张卡全 `DELIVERED`、四行 EN-08、一行 EN-17，包文件在盘上。
    """
    request_id = _chain.to_resumed(bus, session)
    built: list[FakeVendorTransport] = []

    def factory(agent_id, gateway, cwd):
        transport = FakeVendorTransport(agent_id, gateway)
        built.append(transport)
        return transport

    try:
        rounds = 0
        while any(status != "DELIVERED" for status in _cards_of(session, request_id).values()):
            assert rounds < 5, _cards_of(session, request_id)
            rounds += 1
            outcomes = realrun.run_real(
                session,
                feature_request_id=request_id,
                project_key=_chain.PROJECT,
                bus_root=bus.path,
                now=NOW,
                emit_to=lambda line: None,
                transport_factory=factory,
            )
            assert outcomes, "还有卡没交付，但这一轮没有可派发的卡 ⇒ 链路断了"
            for outcome in outcomes:
                assert outcome.validated_status == validate_artifact.VALID, outcome
                assert _cards_of(session, request_id)[outcome.card_id] == review_node.REVIEWING
                review_service.submit_review(
                    session,
                    feature_request_id=request_id,
                    task_id=outcome.card_id,
                    principal="owner-test",
                    verdict="pass",
                    evidence="fixture 人工评审",
                    bus_root=bus.path,
                    entry_name="T02.html",
                    now=NOW,
                )
            session.flush()

        # 四张执行卡全到终态。
        cards = _cards_of(session, request_id)
        assert {card_id: status for card_id, status in cards.items() if not card_id.startswith("T9")} == {
            "T01": "DELIVERED",
            "T02": "DELIVERED",
            "T03": "DELIVERED",
            "T04": "DELIVERED",
        }, cards
        # 交付前那五张系统节点卡（检查 3 + 验收 1 + 打包 1）也在，且都是终态。
        # 它们是"交付前必须已经发生、但之前板上看不见"的事，2026-10-28 起落成卡进交付包。
        assert {card_id: cards.get(card_id) for card_id in ("T93", "T94", "T95", "T96", "T97")} == {
            "T93": "DELIVERED",
            "T94": "DELIVERED",
            "T95": "DELIVERED",
            "T96": "DELIVERED",
            "T97": "DELIVERED",
        }, cards
        assert session.query(ReviewVerdict).filter_by(feature_request_id=request_id).count() == 4
        assert session.query(Deliverable).filter_by(feature_request_id=request_id).count() == 1
        package = bus.path / _chain.PROJECT / "results" / "delivery_package.json"
        assert package.is_file(), package
        manifest = json.loads(package.read_text(encoding="utf-8"))["manifest"]
        names = {str(item.get("rel_path") or item.get("path") or "") for item in manifest}
        # 三份检查报告 + 验收汇总进包（组包前落库并判完，`validated_manifest` 只收 valid 的）。
        for report in ("e2e-report.md", "security-report.md", "gate-report.md", "acceptance.md"):
            assert any(name.endswith(report) for name in names), (report, sorted(names))
        # 打包卡自己的 `delivery-summary.md` **不在**包里 —— 清单不能包含它自己。
        assert not any(name.endswith("delivery-summary.md") for name in names), sorted(names)
    finally:
        _chain.drop_thread(session, request_id)


def test_an_under_floor_artifact_lands_on_the_handoff_edge_without_a_handoff(bus, session):
    """不达标那一支：路由走 `handoff` 那条边，但**换人没有发生**（决定 1 的停点在两条边上都成立）。

    这一格挡的是"图上那条边被当成已经执行了的"那种读法：`ARTIFACT_INVALID` 是门禁自己写的（状态机那一支），
    而 `handoff_count` 仍是 0、卡没进 `HANDED_OFF` ⇒ 出边上的节点体确实一个字都没写。
    """
    request_id = _chain.to_resumed(bus, session)
    _chain.make_all_ready(session, request_id)
    lines: list[str] = []
    try:
        outcomes = realrun.run_real(
            session,
            feature_request_id=request_id,
            project_key=_chain.PROJECT,
            bus_root=bus.path,
            now=NOW,
            emit_to=lines.append,
            transport_factory=lambda agent_id, gateway, cwd: FakeVendorTransport(
                agent_id, gateway, tiny=("T02",)
            ),
        )
        by_card = {item.card_id: item for item in outcomes}
        invalid = by_card["T02"]
        assert invalid.validated_status == validate_artifact.INVALID, invalid
        assert invalid.card_status == validate_artifact.INVALID_STATE, invalid
        row = (
            session.query(TaskCard)
            .filter(TaskCard.feature_request_id == request_id, TaskCard.id == "T02")
            .one()
        )
        assert int(row.handoff_count) == 0, "停点在 `handoff` 那条边上真的换了人"
        joined = "\n".join(lines)
        assert "next=handoff" in joined, joined
        assert "RUN HANDOFF ::" not in joined and "handed_off" not in joined
    finally:
        _chain.drop_thread(session, request_id)


def test_a_vendor_failure_is_recorded_and_the_rest_of_the_chain_keeps_running(bus, session):
    """厂商失败（owner 第三步 1 与 2 的驱动半边）：一张卡倒下、留一行痕、其余三张照样跑完，进程不崩。

    四格：
    ① `run_real` 返回而不是抛 ⇒ "不允许崩溃"；
    ② 失败那张卡的 EN-14 里有且只有本入口那一行 `realrun_produce_failed`，`detail` 带着异常原文；
    ③ 那张卡**没有**产物行、也**没有**被门禁盖章，并且**不再停在 `RUNNING`** —— 它落到 `ESCALATED`
       （2026-10-28 改）。原来这一格断言的是 `RUNNING`，理由是"停下、等人处理"；但 `RUNNING` 不在
       `DISPATCHABLE` 里，也没有任何节点会再碰它，所以"等人处理"是句空话：现场 `real_demo / 7800b3dd`
       的 T05 就这样停了六小时，之后每次续跑都读到"没有可派发的卡"（`cards=0`），界面上一个能点的东西都没有。
       `ESCALATED` 才是文档里"停下、交人工、且有出边"的那一格（`ESCALATED ──人工──▶ READY / REJECTED`）。
    ④ 其余三张照旧 `valid`，收尾槽全零 ⇒ 那一发的槽被 `run_card()` 的 `finally` 还掉了。
    """
    request_id = _chain.to_resumed(bus, session)
    _chain.make_all_ready(session, request_id)
    lines: list[str] = []
    try:
        outcomes = realrun.run_real(
            session,
            feature_request_id=request_id,
            project_key=_chain.PROJECT,
            bus_root=bus.path,
            now=NOW,
            emit_to=lines.append,
            transport_factory=lambda agent_id, gateway, cwd: FakeVendorTransport(
                agent_id, gateway, failing=("T01",)
            ),
        )
        counts = realrun._count(outcomes)
        assert counts == {
            realrun.DONE_ERROR: 1,
            realrun.DONE_VALID: 3,
            realrun.DONE_INVALID: 0,
            realrun.DONE_UNKNOWN: 0,
        }, (counts, outcomes)
        failed = [item for item in outcomes if item.card_id == "T01"][0]
        assert failed.artifacts == () and failed.validated_status is None, failed
        assert "假厂商在 T01 这一发报失败" in failed.error, failed.error
        # ③ 不再停在 RUNNING：那是死路（没有入口会再派发它）。落到 ESCALATED 才能被人放回待跑。
        assert _cards_of(session, request_id)["T01"] == realrun.FAILED_CARD_STATE == "ESCALATED"

        rows = list(
            session.query(Artifact).filter(
                Artifact.feature_request_id == request_id, Artifact.task_card_id == "T01"
            ).all()
        )
        assert rows == [], f"失败那一发却留下了产物行：{rows}"

        from acp.persistence.models_registry import EventLog

        notes = [
            row
            for row in session.query(EventLog)
            .filter(EventLog.entity_id == "T01")
            .all()
            if (row.payload or {}).get("action") == realrun.FAILURE_EVENT_ACTION
        ]
        assert len(notes) == 1, notes
        assert "RuntimeError" in notes[0].payload["detail"], notes[0].payload
        assert any("RUN FAIL" in line for line in lines), lines
    finally:
        _chain.drop_thread(session, request_id)


def test_a_config_refusal_aborts_the_chain_instead_of_repeating_itself(bus, session):
    """缺凭据那一类（`CONFIG_REFUSALS`）在真链上的形状：**一次就停、不逐张重复、不伪装成一次卡失败**。

    与上一发（厂商失败被收住、其余照跑）配对，这两发合起来才是那份三段名单的可证形状：
    ① 异常从 `run_real` 抛出来（不是 `CardOutcome.error`）；
    ② 工厂只被调**一次** —— 后面三张卡根本没被试着跑，所以读数不会是"跑了 4 张、每张都失败"；
    ③ 那一发**不**留 `realrun_produce_failed` 那行留痕：被收住才写它，而这一发没被收住 ⇒
       如果哪天有人把 `RealRunRefused` 从名单里摘掉，它会变成四条"看起来像厂商失败"的行，这一格先红；
    ④ 槽被 `run_card()` 的 `finally` 还掉了（借与还在失败路径上仍成对）。
    """
    request_id = _chain.to_resumed(bus, session)
    _chain.make_all_ready(session, request_id)
    attempts: list[str] = []

    def refusing(agent_id: str, gateway, cwd) -> object:
        attempts.append(agent_id)
        raise realrun.RealRunRefused(f"假环境：{agent_id} 这一发缺凭据（fixture-only）", code="real_wiring_refused")

    from acp.persistence.models_registry import EventLog

    lines: list[str] = []
    try:
        with pytest.raises(realrun.RealRunRefused):
            realrun.run_real(
                session,
                feature_request_id=request_id,
                project_key=_chain.PROJECT,
                bus_root=bus.path,
                now=NOW,
                emit_to=lines.append,
                transport_factory=refusing,
            )
        assert len(attempts) == 1, f"同一句拒绝被重复了 {len(attempts)} 次 ⇒ 它被当成一次卡失败在处理"
        assert not any("RUN FAIL" in line for line in lines), lines
        notes = [
            row
            for row in session.query(EventLog).filter(EventLog.entity_id == "T01").all()
            if (row.payload or {}).get("action") == realrun.FAILURE_EVENT_ACTION
        ]
        assert notes == [], f"配置级拒绝被写成了一次卡失败：{notes}"
        # 配置级拒绝**不**走"这张卡失败"那条路（它连 FAIL 行都不许写），所以卡就停在被派发的那一刻。
        # 与上一条用例的区别值得写清：`ESCALATED` 是"这张卡自己跑砸了、交人处置"；
        # 这里是"这个进程根本不会成功"，整条链已经中止，卡态由下一次续跑前的现场处置决定
        # （网页那条路径上由 `serve_resumer._release_stuck_cards` 兜）。
        assert _cards_of(session, request_id)["T01"] == "RUNNING", "那一发该停在被派发的那一刻，不多不少"
        rows = list(session.query(Artifact).filter(Artifact.feature_request_id == request_id).all())
        assert rows == [], "装配失败那一发却落了产物行"
    finally:
        _chain.drop_thread(session, request_id)


def test_the_driver_never_touches_a_real_vendor_construction(bus, session, monkeypatch):
    """反向对照（重点那条变异）：真 PG 上跑驱动时，`build_real_transport()` **一次都没被调**。

    这一格与节 一那条静态判据是同一个事实的两面：静态那条说"代码里只有工厂那一个调用点"，
    这一发说的是"这一轮真跑起来时确实没走到它"。少了这一发，前者只证明形状、不证明这一轮没出网；
    少了前者，这一发只证明一次运行、下次有人加一处直接构造照样绿。
    做法：把 `realrun.real_transport` 换成一个会记账的假件之外**再**给工厂本身下绊子。
    """
    request_id = _chain.to_resumed(bus, session)
    calls: list = []
    monkeypatch.setattr(
        realrun.spec_factory,
        "build_real_transport",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(),
    )
    lines: list[str] = []
    try:
        outcomes = realrun.run_real(
            session,
            feature_request_id=request_id,
            project_key=_chain.PROJECT,
            bus_root=bus.path,
            now=NOW,
            emit_to=lines.append,
            transport_factory=lambda agent_id, gateway, cwd: FakeVendorTransport(agent_id, gateway),
        )
        assert calls == [], f"注入位之外还有一条真构造路径：{calls}"
        assert outcomes and outcomes[0].error is None, outcomes
    finally:
        _chain.drop_thread(session, request_id)
