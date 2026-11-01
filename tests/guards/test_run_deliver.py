"""T123 装配点（第 2 步下半段第 5 项）：交付包段 `run_deliver()` 的判据。

**这一笔接的是哪一段**：验收段通了之后链路能收到 `DELIVERED`，但公共区那三样
（`results/delivery_package.json`、`status.md`、`results/<卡号>.md`）在 `src` 里**还没有生产写者**
（现读 2026-10-17 10:0x：`OrchestratorWriter(` 在 `src` 里构造 **1** 处 = 本笔刚接上的 `run_deliver`，
`upsert_status` / `write_result_index` / `ensure_layout` / `write_agents_md` 的生产调用方仍各 **0** 处 ——
那半条是步骤 4）。owner 2026-10-14 的修订把 R-06 提前进 MVP-1，理由原话是"R-07 的'跑通'判据需要唯一交付包作为终点"
（`plan.md:505`）—— 所以 T124 要端到端跑通，这一格是前件，不是可选装饰。

本文件钉六件事，每件各挡一种会发生的错：

① **入口页不许装配点猜**：名字由调用方给（`entry_name` 是无默认值的关键参数），在实体那份 manifest 里选，
   选中 0 条 ⇒ `entry_absent`，选中 ≥2 条 ⇒ `entry_ambiguous`。两种都是拒绝，不是"取第一个"。
   挡的错：manifest 里多一份同名产物时交付包指向谁都行，"唯一一份"就没了。
② **唯一性不在装配点重判**：组包、上限、已有包的拒绝全在 `assemble.assemble()` 里
   （`delivery_package_exists` 是实体的代号），本函数不查"是不是已经有包了"。挡的错：两处各判一次，
   第二处一旦写松（比如"有包就覆盖"），FR-036 的 MUST 就只剩注释。
③ **公共区只走 `OrchestratorWriter` 那一个代笔点**，不拿平台身份直接 `gateway.write_artifact` 写
   `results/`。挡的错：`acl.PUBLIC_OWNER` 那份身份一旦旁路，T095 的"单点代笔"就成了一句口号。
④ **入口只从 `validated_manifest` 里选**（实体会滤掉门禁判 `invalid` 的行），装配不自读第二份 EN-06。
   挡的错：自读未筛的 EN-06 会把"自述成功但不达标"那份当入口 —— SC-007 的虚报形状最后一次出现的地方。
⑤ **项目根只有一个载体 = `archive.archive_root()`**（owner 2026-10-17 裁决 2 的合并，笔 B）：
   `assemble(bus_root=…)` 收到的必须是它的结果，而 `run.py` 里不再自己养一份拼法。挡的错有两层，一层已经真踩过：
   把一路传下来的外层 `bus_root` 递给实体，交付段会指着 `<外层>/qoder-workspace/…` 说不存在（差的正是
   `project_key` 那一层）；另一层是两份口径迟早分叉，连 `project_key` 的形态校验也会变成"一边拒、
   一边静默拼出一个目录"—— 所以本文件除了静态那格，还钉一发行为量：装配段拿到的目录必须**等于**
   存档段那个函数算出来的目录，非法键名在装配段也要一起 `ValueError`。
⑥ **未达标项清单只有一个来源 = 库**（owner 2026-10-17 裁决 3 第 ① 项）：`run_deliver` 不收外部清单，
   它经 `open_issues_of()` 从 EN-14 那一轮验收的载荷里读原文；读不到就拒（`open_issues_unreadable`），
   **不**降级成空清单。挡的错：装配点手里那份与验收岗判的那份可以不同，而库里看不出来 —— 交付包就替
   装配点说了话。EN-14 载荷的扫描者也必须只有一个（`_acceptance_audit`），两处各写一遍循环就会有一份
   在键名改了之后读到空。

诚实性（与执行段/验收段同规）：本笔只到**一张卡交付**那一形 —— 四张卡同时 ready 会撞 Q10
（指派段在同一支 `max_parallel=1` 的工具上第二次占槽会挂起，owner 2026-10-17 裁甲，修复在下一笔），
所以这里断的是交付包段的机制，**不是** R-07 的"4 个文件"构成。那句在 `evidence/phase-12.md` §十五 明写。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import subprocess
import uuid
from contextlib import closing
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp import chaos
from acp import run as run_module
from acp.adapters.stub import StubMisuse
from acp.approval import repository as repo
from acp.archive import root as archive
from acp.bus import layout
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import assemble as assemble_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Deliverable, EventLog

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 17, 9, 10, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-deliver"
RAW = "做个介绍公司产品的静态网站"
FINDINGS = run_module.StubFindingProvider()
ACCEPT = run_module.StubAcceptanceProvider()


# ------------------------------------------------------------------ 小工具


def func_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`run.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def called_names(func: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                out.append(name)
    return out


def keyword_of(call_owner: ast.AST, kind: str, keyword: str):
    for node in ast.walk(call_owner):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == kind:
                for kw in node.keywords:
                    if kw.arg == keyword:
                        return kw.value
    return None


def entry(rel_path: str) -> SimpleNamespace:
    """manifest 条目的假面 —— `_entry_path_of` 只读 `rel_path` 一列，不需要真 `ManifestEntry`。"""
    return SimpleNamespace(rel_path=rel_path)


def bus_root_of(bus) -> pathlib.Path:
    project = bus.path / PROJECT
    project.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "."], ["config", "user.email", "test@invalid"], ["config", "user.name", "test"]):
        subprocess.run(
            ["git", *args], cwd=str(project), check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    return bus.path


def drop_thread(session, feature_request_id: uuid.UUID) -> None:
    thread = cp.thread_id_for(kind="A_SPLIT", feature_request_id=feature_request_id)
    with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
        chaos.kill_graph_state(conn, thread)


def to_delivered(bus, session) -> tuple[uuid.UUID, str, str]:
    """真跑到**一张卡 `DELIVERED`**：受理 → 人批 → 恢复 → 指派 → 执行段 → 验收段。

    返回 `(需求单号, 卡号, 那份产物的总线内相对路径)`。第三格从 EN-06 现值读，不拼字符串 ——
    拼出来的路径与网关实际落盘的那份一旦分叉，交付包就会指向一个不存在的文件。
    """
    bus_root_of(bus)
    intake = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=bus.path, session=session, now=NOW)
    request_id = intake.feature_request_id
    ticket = session.get(ApprovalTicket, intake.ticket_id)
    repo.resolve(
        session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
        principal="owner-test", approvers=frozenset({"owner-test"}), now=NOW,
    )
    run_module.run_resume(request_id, transport="stub", bus_root=bus.path, session=session)
    slots = run_module._slots_from_registry(session)
    leases = scheduler.LeaseLedger()
    assigned = run_module.run_assign(request_id, transport="stub", session=session, now=NOW, slots=slots, leases=leases)
    card_id = assigned.dispatched[0][0]
    run_module.run_execute(request_id, card_id, transport="stub", bus_root=bus.path, session=session,
                           now=NOW, findings_for=FINDINGS, slots=slots, leases=leases)
    acc = run_module.run_accept(request_id, card_id, transport="stub", session=session, now=NOW, accept_for=ACCEPT)
    assert acc.card_status_after == accept_node.DELIVERED, acc
    rel = str(session.scalars(
        select(Artifact.rel_path).where(Artifact.feature_request_id == request_id, Artifact.task_card_id == card_id)
    ).one())
    return request_id, card_id, rel


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


# ---------------------------------------------------------------- 一、静态形状


def test_the_entry_selection_refuses_absent_and_ambiguous_rather_than_guessing():
    """`_entry_path_of` 的三种落点：恰好一条 ⇒ 那条；0 条 ⇒ `entry_absent`；两条 ⇒ `entry_ambiguous`。

    这一发不连库（纯函数），所以它在 fast 档里就能守住"不猜"这一格。
    反向读法也钉：命中两条时**不许**退化成"取排序后的第一个" —— 那是把冲突裁决（T096）的输出
    换成装配点的手工挑选。
    """
    rid = uuid.uuid4()
    picked = run_module._entry_path_of([entry("qoder-workspace/styles.css")], "styles.css", rid)
    assert picked == "qoder-workspace/styles.css", picked

    with pytest.raises(run_module.RunFailure) as absent:
        run_module._entry_path_of([entry("qoder-workspace/styles.css")], "index.html", rid)
    assert absent.value.code == run_module.ENTRY_MISSING, absent.value.code

    with pytest.raises(run_module.RunFailure) as ambiguous:
        run_module._entry_path_of(
            [entry("qoder-workspace/index.html"), entry("doubao-workspace/index.html")], "index.html", rid
        )
    assert ambiguous.value.code == run_module.ENTRY_AMBIGUOUS, ambiguous.value.code


def test_run_deliver_refuses_an_empty_entry_name_before_touching_io():
    """空入口名不是默认值，是"没决定" ⇒ 在任何一个 IO 动作之前拒（`session=None` 就是这一发的证人）。

    同一发还钉签名：`entry_name` MUST 是**关键参数且无默认值**。挡的错是"给它一个 `DEFAULT_ENTRY_NAME`" ——
    那样 `run_deliver(rid, transport=…, bus_root=…, session=…, now=…)` 会带着一个没人点过的名字跑下去，
    空名拒绝那一格就永远走不到，而"入口页由调用方点名"这句只剩注释。
    """
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_deliver(uuid.uuid4(), transport="stub", bus_root=pathlib.Path("/tmp/unused"),
                               session=None, now=NOW, entry_name="   ")
    assert caught.value.code == run_module.ENTRY_NAME_EMPTY, caught.value.code

    fn = func_body("run_deliver")
    names = [arg.arg for arg in fn.args.kwonlyargs]
    padded = [None] * (len(names) - len(fn.args.kw_defaults)) + list(fn.args.kw_defaults)
    with_defaults = {name for name, node in zip(names, padded) if node is not None}
    assert "entry_name" in names and "entry_name" not in with_defaults, (
        f"`entry_name` 不在关键无默认参数里（现有默认值：{sorted(with_defaults)}）"
    )


def test_the_transport_gate_speaks_first_in_the_delivery_segment():
    """门先说话：`--transport` 没显式给 ⇒ 撞仿真门，轮不到入口名那一步（顺序反了本发就读到别的代号）。"""
    with pytest.raises(StubMisuse):
        run_module.run_deliver(uuid.uuid4(), transport="", bus_root=pathlib.Path("/tmp/unused"),
                               session=None, now=NOW, entry_name="index.html")


def test_the_assembly_point_never_builds_a_deliverable_row_or_bypasses_the_writer():
    """交付包那一行只能由实体建；公共区那个文件只能由 `OrchestratorWriter` 写。

    四格各挡一种错：
    ① `run.py` 里零处构造 `Deliverable(` —— 有就是 EN-17 的第二载体；
    ② `run_deliver()` 里 `assemble` 恰好一次（不多判一次唯一性、不少到不组包）；
    ③ `write_delivery_package` 恰好一次，且**没有** `write_artifact` 直接写公共区（绕开 `PUBLIC_OWNER`）；
    ④ `validated_manifest` 只在 `run_deliver()` 里出现一次，`_entry_path_of` 里零次 ——
       manifest 只读一次，选入口这一步不再去 EN-06 里另捞一份。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "Deliverable"], "装配点在构造 EN-17 的行"
    body = func_body("run_deliver")
    names = called_names(body)
    assert names.count("assemble") == 1, names
    assert names.count("write_delivery_package") == 1, names
    assert "write_artifact" not in names, "直接走网关写公共区 = 绕过 `OrchestratorWriter` 那单点代笔"
    assert names.count("validated_manifest") == 1, names
    assert not any(name in called_names(func_body("_entry_path_of")) for name in
                   ("validated_manifest", "write_delivery_package")), "选入口的那一步在第二次读 manifest"


def test_the_entry_passed_to_assemble_is_the_chosen_one():
    """`assemble(entry_path=…)` 收到的是 `_entry_path_of` 挑出来的那一个，不是调用方原样传进来的名字。

    这条与上面那条同族但挡的是另一种错：把 `entry_name`（`index.html`）直接递给 `assemble()`，
    实体那句 `entry_not_in_manifest` 会拒 —— 但更糟的写法是装配点自己拼一个 `<workspace>/index.html`
    递进去，那就绕过了 manifest 的选择与"0 条/2 条"的拒绝。所以钉名字链，不钉内容。
    """
    body = func_body("run_deliver")
    chosen = None
    for node in body.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if "entry_path" in called_names(node.value) or "_entry_path_of" in called_names(node.value):
                chosen = node.targets[0].id
    assert chosen, "没有一处赋值把 `_entry_path_of()` 的结果接住 ⇒ 入口不是从 manifest 选出来的"
    handed = keyword_of(body, "assemble", "entry_path")
    assert isinstance(handed, ast.Name) and handed.id == chosen, (
        f"`assemble(entry_path=…)` 收的不是选出来那份（期望 {chosen}，实得 "
        f"{ast.dump(handed) if handed else None}）"
    )


def root_division_sites(tree: ast.Module) -> list[str]:
    """`run.py` 里所有"外层总线根 ÷ 项目键"形状的除法，按所在函数名报出来（合并后 MUST 为空）。

    按形状找而不是按名字找：有人重新加一份私有拼法时大概率会换个函数名，但"把两个名字一除"这个形状跑不掉。
    `Path(bus_root) / project_key` 也算 —— 左边的 `Call` 摊开看里面的名字。
    """

    def names(node: ast.expr) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Call):
            return {n for arg in node.args for n in names(arg)}
        return set()

    hits: list[str] = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if any("bus_root" in n for n in names(node.left)) and any(
                    "project_key" in n for n in names(node.right)
                ):
                    hits.append(fn.name)
    return hits


def archive_root_calls(func: ast.AST) -> list[ast.Call]:
    """某个函数体里所有 `archive.archive_root(...)` 调用（属性基名必须是那个 import 别名）。"""
    return [
        node for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "archive_root" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "archive"
    ]


def test_the_project_root_has_one_carrier_and_the_entity_gets_that_one():
    """`assemble(bus_root=…)` 收到的是**项目根**，而项目根全仓只剩 `archive.archive_root()` 一处载体。

    这条判据的形状在 owner 2026-10-17 裁决 2 前后各钉一次，钉的是同一个关系、不一样的量法：

    · **合并前**（原话）"全 `run.py` 里'外层根套一层项目名'的表达式 MUST 只有一份（在私有函数内）" ——
      那时 `run.py` 自己养一份，核验段另用 `archive.archive_root()`，两件事读起来都对，但迟早分叉；
    · **合并后** 同一格翻成"**0 份私有拼法 + 消费点全走那一个载体**"，因为"只有一份"这件事现在由
      `archive/root.py` 那处成立，`run.py` 里再留一份就是第二份。

    两格都留着，各挡一种真发生过的错：① 递给实体的根差 `project_key` 那一层（本仓第一次真跑就被
    `assemble.py:344`"入口在总线根目录下不存在"拦下的那一发）；② 两份口径分叉后，`project_key` 的
    形态校验只剩一边，非法键名会变成"一边拒、一边静默拼出目录"。③ 再加一格关系量：四个消费点
    （网关、交付、投影、整链活动流）各**恰好**调载体一次，`bus_root=` 递的是自己的形参、项目键排在第一位
    （段函数里那个键还必须就是从 `_request_row()` 取的那一个）—— 位置换一下就悄悄换根。
    """
    text = SRC.read_text(encoding="utf-8")
    tree = ast.parse(text)

    body = func_body("run_deliver")
    made = None
    for node in body.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(
            node.value.func, ast.Attribute
        ) and node.value.func.attr == "archive_root" and isinstance(node.targets[0], ast.Name):
            made = node.targets[0].id
    assert made, "`run_deliver()` 里没有一处 `archive.archive_root()` 赋值 ⇒ 项目根不在这里定"
    handed = keyword_of(body, "assemble", "bus_root")
    assert isinstance(handed, ast.Name) and handed.id == made, (
        f"`assemble(bus_root=…)` 收到的是 {ast.dump(handed) if handed else None} 而不是 {made} ⇒ 交付段会差一层"
    )

    assert not [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_project_root"], (
        "`run.py` 还养着一份私有项目根承载点函数 ⇒ 与存档段那份是两件事，迟早分叉"
    )
    assert root_division_sites(tree) == [], "私有拼法又出现了（除法形状）⇒ 载体不再是唯一一处"
    assert text.count("Path(bus_root) / project_key") == 0, "合并前那份拼法的字面量还留在 `run.py`"

    for name in ("_gateway_for", "run_deliver", "run_publish", "run_chain"):
        fbody = func_body(name)
        calls = archive_root_calls(fbody)
        assert len(calls) == 1, (
            f"`{name}()` 里 `archive.archive_root()` 是 {len(calls)} 处（应恰好 1）"
            "⇒ 0 处 = 那一层的根不由载体决定，2 处 = 中途换了一次根"
        )
        call = calls[0]
        assert [a.id for a in call.args] == ["project_key"], (
            f"`{name}()` 递给载体的位置参数不是项目键 ⇒ 换位置就换根"
        )
        assert {k.arg for k in call.keywords} == {"bus_root"}, (
            f"`{name}()` 没用关键字把 `bus_root=` 递给载体 ⇒ 总线根那一层会退化成默认值"
        )
        params = {p.arg for p in (*fbody.args.posonlyargs, *fbody.args.args, *fbody.args.kwonlyargs)}
        for k in call.keywords:
            assert isinstance(k.value, ast.Name) and k.value.id == k.arg, (
                f"`{name}()` 的 `{k.arg}=` 递进去的是 {ast.dump(k.value)} 而不是自己的形参 ⇒ 两处不是同一个根"
            )
        assert "bus_root" in params, f"`{name}()` 的 `bus_root` 不是自己的形参 ⇒ 那一层是别人塞进来的"
        if "project_key" not in params:  # 段函数里项目键不是形参，只能来自受理段那一行
            owner_assign = [
                node for node in ast.walk(fbody) if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "project_key" for t in node.targets)
            ]
            assert len(owner_assign) == 1 and "_request_row" in called_names(owner_assign[0].value), (
                f"`{name}()` 的项目键不是从 `_request_row()` 那行取的 ⇒ 载体算出的目录可能不是这个请求的目录"
            )

    imported = [
        node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "acp.archive"
    ]
    assert any(
        node.level == 0 and any(a.asname == "archive" for a in node.names) for node in imported
    ), "`run.py` 没把 `acp.archive.root` 以 `archive` 别名引进来 ⇒ 上面那些调用指的是别的东西"


def test_the_assembled_root_is_the_one_the_archive_segment_computes(tmp_path) -> None:
    """行为那一格：合并后装配段的根**等于**存档/核验段那个函数算出来的目录，非法键名也一起拒。

    静态那格只能证明"写的是同一个调用"，证不了"量出来是同一个目录"；这一发直接比对象。第二格是合并
    带来的**新**可观测面：`run.py` 那份私有拼法不校验 `project_key`，`archive_root()` 经
    `layout.project_dir()` 校验 —— 两份并存时同一支非法键会"一边拒、一边静默拼出目录"，合并后必须
    两边一起 `ValueError`。生产路径上键名在受理段就过了校验，所以这一发是防分叉的绊线，不是补功能。
    """
    expected = archive.archive_root(PROJECT, bus_root=tmp_path)
    assert run_module._gateway_for(tmp_path, PROJECT).root == expected
    assert expected == pathlib.Path(str(tmp_path / PROJECT))

    with pytest.raises(ValueError):
        run_module._gateway_for(tmp_path, "Bad Key")



# ------------------------------------------------------------------ 二、真 PG


def test_the_package_lands_once_and_the_public_file_points_at_the_entry(bus, session):
    """一张卡交付后组包：EN-17 一行、`results/delivery_package.json` 在盘上、入口指回那条 manifest。

    五格各挡一种错：
    ① `report.entry_path` / `manifest_count` / `cards_delivered` 全部从 **EN-17 那行**与库里现值读回，
       不是 `Package` 返回值的转抄（原则 I）；
    ② 公共区那个路径是 `assemble.delivery_path()` 给出的那一个，装配点不另拼（`layout` 只有一份）；
    ③ 盘上的 JSON 里 `entry_path` 等于 EN-17 里那一份 —— 两份一旦分叉，"打开就能预览"就是假的；
    ④ `package_bytes > 0` 是从盘上量的（写完没落地，这里就见光）；
    ⑤ 入口名用的是**这张卡真交了的那个文件名**：本笔只到"一张卡交付"那一形，
       所以断的是机制，不是 owner 裁 Q9 那句"4 个文件"的构成 —— 那半条要等 Q10 修完。
    """
    request_id, card_id, rel = to_delivered(bus, session)
    name = rel.rsplit("/", 1)[-1]
    try:
        report = run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                        now=NOW, entry_name=name)
        assert report.entry_path == rel, (report.entry_path, rel)
        assert report.manifest_count == 1 and report.cards_delivered == 1, report
        assert report.package_path == assemble_node.delivery_path() == layout.DELIVERY_PACKAGE_JSON, report
        on_disk = bus.path / PROJECT / report.package_path
        assert on_disk.is_file(), on_disk
        assert report.package_bytes == on_disk.stat().st_size > 0, report
        payload = json.loads(on_disk.read_text(encoding="utf-8"))
        row = session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).one()
        assert payload["entry_path"] == str(row.entry_path), (payload["entry_path"], row.entry_path)
        assert payload["deliverable_id"] == str(row.id), payload
        assert len(payload["manifest"]) == 1 and payload["manifest"][0]["rel_path"] == rel, payload["manifest"]
        assert payload["open_issues"] == [], payload
    finally:
        drop_thread(session, request_id)


def test_assembling_the_package_is_what_turns_the_request_delivered(bus, session):
    """包写成的那一刻 ⇒ EN-01 终态，且留痕记着它是从哪一档过来的。

    为什么这一格非得在真 PG 上量：`run.mark_request_delivered` 读的是 `session.get(FeatureRequest, …)`
    的现值，写的是同一行的 `status`，并在同一个事务里落一条 EN-14。三件事都在库里，假件只能证形状。

    现场（`real_demo / 7800b3dd`，2026-10-28 08:05）：包已经组出来、十份产物都进了 manifest，
    而 EN-01 还写着 `RUNNING`。原因是最后一张卡由**人工评审**推到终态，那条路径不经过跑卡循环，
    而跑卡循环是此前唯一写这一列的地方 —— 于是最该被写下的那一档（交付了）反而没人写。
    """
    request_id, card_id, rel = to_delivered(bus, session)
    name = rel.rsplit("/", 1)[-1]
    try:
        before = str(session.get(FeatureRequest, request_id).status)
        assert before != run_module.REQUEST_DELIVERED, before

        report = run_module.run_deliver(
            request_id, transport="stub", bus_root=bus.path, session=session, now=NOW, entry_name=name
        )

        assert str(session.get(FeatureRequest, request_id).status) == run_module.REQUEST_DELIVERED
        event = session.scalars(
            select(EventLog)
            .where(
                EventLog.entity == "feature_request",
                EventLog.entity_id == str(request_id),
                EventLog.payload["action"].astext == run_module.REQUEST_STATUS_SYNCED,
            )
            .order_by(EventLog.id.desc())
        ).first()
        assert event is not None, "组包点没有留下状态迁移的痕"
        assert event.payload["prior_status"] == before, event.payload
        assert event.payload["status_after"] == run_module.REQUEST_DELIVERED, event.payload
        assert event.payload["deliverable_id"] == str(report.deliverable_id), event.payload
    finally:
        drop_thread(session, request_id)


def test_a_missing_entry_refuses_before_any_package_row_or_file(bus, session):
    """入口名点了本批没交的文件 ⇒ `entry_absent`，而且**一行库都不写、一个文件都不落**。

    挡的错是"先组包再发现入口不对"：那时 EN-17 已经有一行指向不存在入口的包，
    而 `FR-036` 的"唯一一份"会让第二次尝试直接被 `delivery_package_exists` 拒 ——
    坏包就永久留在那里了。所以选择 MUST 在组包之前。
    """
    request_id, _card_id, _rel = to_delivered(bus, session)
    try:
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                   now=NOW, entry_name="index.html")
        assert caught.value.code == run_module.ENTRY_MISSING, caught.value.code
        assert session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).count() == 0
        assert not (bus.path / PROJECT / layout.DELIVERY_PACKAGE_JSON).exists()
    finally:
        drop_thread(session, request_id)


def test_an_invalid_artifact_row_is_not_eligible_to_be_the_entry(bus, session):
    """门禁判为 `invalid` 的那一行**进不了 manifest** ⇒ 按名字选入口时它不算一条命中。

    挡的错很具体：装配点如果自读 EN-06（不经过实体的 `validated_manifest`）来选入口，就会把
    "自述成功但门禁判不过"那份当成可预览的入口 —— 那正是 SC-007 要防的虚报形状最后一次出现的地方。
    这一发在场面上多造一条同名但 `invalid` 的行：valid 那行是 css，所以 `index.html` 在 manifest 里
    必须 0 命中 ⇒ `entry_absent`。若实现改成"自读未筛的 EN-06"，那一行会被选中 ⇒ 本发立刻红。
    """
    request_id, card_id, _rel = to_delivered(bus, session)
    try:
        executor = str(session.get(TaskCard, (request_id, card_id)).executor_agent_id)
        session.add(
            Artifact(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                task_card_id=card_id,
                writer_agent_id=executor,
                rel_path="doubao-workspace/index.html",
                bytes=128,
                lines=4,
                format="html",
                self_reported_status="success",
                validated_status="invalid",
                invalid_reason="guard_min_threshold",
                created_at=NOW,
            )
        )
        session.flush()
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                   now=NOW, entry_name="index.html")
        assert caught.value.code == run_module.ENTRY_MISSING, caught.value.code
        assert session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).count() == 0
    finally:
        drop_thread(session, request_id)


def test_a_second_package_is_refused_and_the_first_file_is_untouched(bus, session):
    """FR-036 的"唯一一份"在链路上是真的：第二发被实体拒，第一个包的内容一个字节都没变。

    这一发同时是 ② 那条静态判据的行为证人 —— 如果装配点哪天自己查"有没有包了"再决定覆盖，
    `deliverable_id` 就会变，这里的断言立刻红。
    """
    request_id, _card_id, rel = to_delivered(bus, session)
    name = rel.rsplit("/", 1)[-1]
    try:
        first = run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                       now=NOW, entry_name=name)
        text_before = (bus.path / PROJECT / first.package_path).read_text(encoding="utf-8")
        with pytest.raises(assemble_node.AssembleMisuse) as caught:
            run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                   now=NOW, entry_name=name)
        assert caught.value.code == "delivery_package_exists", caught.value.code
        assert session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).count() == 1
        assert (bus.path / PROJECT / first.package_path).read_text(encoding="utf-8") == text_before
    finally:
        drop_thread(session, request_id)


# -------------------------------------------- 三、未达标项清单的载体（owner 2026-10-17 裁决 3 第 ① 项）


class UnmetProvider:
    """验收来源交回**全部未达标** —— 只为把卡推进 `DELIVERED_WITH_ISSUES` 那一形，内容不参与判据。

    为什么仍走 `run_accept` 而不是直接调 `accept_node.accept()`：绕开 `run_accept` 就绕开了
    "验收岗身份取自 EN-02 + 代填留痕"那两格，交付段读到的载荷就不是链路上会产生的形状。
    """

    MARK = "stub-unmet-provider"

    def verdicts_for(self, card, items):
        return tuple(
            accept_node.ItemVerdict(
                acceptance_item=str(item),
                met=False,
                evidence=f"{self.MARK}：产物里没有 {str(item)!r} 那一节",
                gap=f"缺 {str(item)}",
            )
            for item in items
        )


UNMET = UnmetProvider()


def to_delivered_with_issues(bus, session) -> tuple[uuid.UUID, str, str]:
    """跑到一张卡 **`DELIVERED_WITH_ISSUES`**：与 `to_delivered` 同一条链，只把轮次摆到上限前一格、判定交回未达标。

    为什么直接写 `review_round`：跑满上限的另一条路是"不通过 → 返工 → 再指派 → 再执行 → 再验收"三轮，
    那是 `test_review_accept.py` 第八节的现场。本节的对象是交付包段的**读侧**，绕开的代价照实写：
    这一发不证明"三轮之后一定收敛"，只证明"卡一旦带问题，包里的清单来自库里"。
    """
    bus_root_of(bus)
    intake = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=bus.path,
                                   session=session, now=NOW)
    request_id = intake.feature_request_id
    ticket = session.get(ApprovalTicket, intake.ticket_id)
    repo.resolve(
        session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
        principal="owner-test", approvers=frozenset({"owner-test"}), now=NOW,
    )
    run_module.run_resume(request_id, transport="stub", bus_root=bus.path, session=session)
    slots = run_module._slots_from_registry(session)
    leases = scheduler.LeaseLedger()
    assigned = run_module.run_assign(request_id, transport="stub", session=session, now=NOW,
                                     slots=slots, leases=leases)
    card_id = assigned.dispatched[0][0]
    run_module.run_execute(request_id, card_id, transport="stub", bus_root=bus.path, session=session,
                           now=NOW, findings_for=FINDINGS, slots=slots, leases=leases)
    card = session.get(TaskCard, (request_id, card_id))
    card.review_round = accept_node.round_limit() - 1
    session.flush()
    acc = run_module.run_accept(request_id, card_id, transport="stub", session=session, now=NOW,
                                accept_for=UNMET)
    assert acc.card_status_after == accept_node.DELIVERED_WITH_ISSUES, acc
    assert acc.unmet == len(acc.items), acc
    rel = str(session.scalars(
        select(Artifact.rel_path).where(Artifact.feature_request_id == request_id, Artifact.task_card_id == card_id)
    ).one())
    return request_id, card_id, rel


def erase_issue_list(session, card_id: str) -> None:
    """把 EN-14 那一轮验收载荷里的清单格抹成空 —— 现场是**造**的：真链路上 `accept()` 一定会写它。

    这一发挡的是"载荷形状漂了之后静默交空清单"：到了 `assemble()` 那一侧，空清单与"这轮全过了"
    长得一模一样（那里的判据是"有带问题的卡而清单为空"），两种成因分不开就得靠读侧先拒。
    """
    rows = [
        row for row in session.query(EventLog)
        .filter(EventLog.entity == "task_card", EventLog.entity_id == card_id).all()
        if (row.payload or {}).get("action") == accept_node.ACTION_ROUND
    ]
    assert rows, "库里没有那一轮验收的载荷 ⇒ 现场没造出来"
    for row in rows:
        row.payload = {**(row.payload or {}), accept_node.OPEN_ISSUES_ITEMS: []}
    session.flush()


def test_the_delivery_segment_has_no_channel_to_hand_the_list_in():
    """清单只有一个来源：`run_deliver` 的签名里不再有 `open_issues`，递给实体的那一格是 `open_issues_of()` 的结果。

    四格各挡一种错：
    ① 签名里没有 `open_issues` ⇒ 留一个可申报的参数，就是给同一份清单留第二个来源（装配点交的与验收岗
       判的可以不同，而库里看不出来）；
    ② 递给 `assemble(open_issues=…)` 的是 `open_issues_of()` 这一发调用，不是字面量 `()` 也不是变量转抄
       （写死 `()` 会让带问题交付照样出包，只是清单是空的）；
    ③ EN-14 载荷的扫描函数**只有一个**（`_acceptance_audit`）：`run_accept` 与 `open_issues_of` 都调它，
       两处各写一遍循环，改了键名就会有一份读到空；
    ④ 拒绝代号在 `run.py` 里只出现两处（定义 + 那一发 `raise`）⇒ 没有第二处把同一个现场判成别的形状。
    """
    body = func_body("run_deliver")
    args = [node.arg for node in list(body.args.posonlyargs) + list(body.args.args) + list(body.args.kwonlyargs)]
    assert "open_issues" not in args, f"`run_deliver` 还能收外部清单：{args}"
    assert called_names(body).count("open_issues_of") == 1, called_names(body)
    handed = keyword_of(body, "assemble", "open_issues")
    assert isinstance(handed, ast.Call), handed
    assert (getattr(handed.func, "id", None) or getattr(handed.func, "attr", None)) == "open_issues_of", handed

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    scanners = sorted({
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "EventLog" in ast.dump(node)
    })
    assert scanners == ["_acceptance_audit"], f"EN-14 载荷出现了第二个扫描者：{scanners}"
    for reader in ("run_accept", "open_issues_of"):
        assert "_acceptance_audit" in called_names(func_body(reader)), reader
    text = SRC.read_text(encoding="utf-8")
    assert text.count('"open_issues_unreadable"') == 1, "同一个现场在 `run.py` 里有两处拒绝形状"


def test_a_card_delivered_with_issues_puts_the_librarys_list_into_the_package(bus, session):
    """带未达标项交付那一发：EN-17 里的清单**逐字等于**验收段落在 EN-14 的那一份，盘上那个 JSON 也带着它。

    四格：
    ① `assemble()` 的 `open_issues_required` 在这一发没有拦 ⇒ 清单真的读得到了（此前那一形跑不通）；
    ② 库里 `Deliverable.open_issues` == 载荷里那份清单（不是重新拼的）；
    ③ 条数与清单长度相等 ⇒ 同一轮验收的两个口径不漂；
    ④ 盘上包 JSON 的 `open_issues` 与库里那行相等 ⇒ R-07 那份"打开就能看见"的说明里看得见问题清单。
    """
    request_id, card_id, rel = to_delivered_with_issues(bus, session)
    name = rel.rsplit("/", 1)[-1]
    try:
        audit = run_module._acceptance_audit(session, card_id)
        report = run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                        now=NOW, entry_name=name)
        row = session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).one()
        issues = [dict(item) for item in (row.open_issues or ())]
        assert issues == list(audit[accept_node.OPEN_ISSUES_ITEMS]), (issues, audit)
        assert len(issues) == int(audit["open_issues"]) == audit_count(audit) > 0, (issues, audit)
        assert report.cards_delivered == 1, report
        on_disk = json.loads((bus.path / PROJECT / report.package_path).read_text(encoding="utf-8"))
        assert on_disk["open_issues"] == issues, on_disk
        assert all(set(item) == {"item", "gap", "suggestion"} for item in issues), issues
    finally:
        drop_thread(session, request_id)


def audit_count(audit: dict) -> int:
    """载荷里那条 `unmet` —— 用来把"清单条数"钉成关系式，而不是往判据里塞一个数字。"""
    return int(audit["unmet"])


def test_an_unreadable_issue_list_refuses_before_any_package_row_or_file(bus, session):
    """有卡带问题、而库里那份清单读不出来 ⇒ `open_issues_unreadable`，**一行的库都不写、一个文件都不落**。

    为什么不降到"交一份空清单让 `assemble()` 去拒"：那一挡的语义是"清单为空 ⇒ 不许组包"（判定面），
    这里的语义是"清单本该在库里、却不在"（载体面）—— 混成同一个代号，复核者就分不清该补判定还是该查载荷。
    现场是造的（见 `erase_issue_list`），真实链路里 `accept()` 一定会写那一格。
    """
    request_id, card_id, rel = to_delivered_with_issues(bus, session)
    name = rel.rsplit("/", 1)[-1]
    erase_issue_list(session, card_id)
    try:
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session,
                                   now=NOW, entry_name=name)
        assert caught.value.code == run_module.OPEN_ISSUES_UNREADABLE, caught.value.code
        assert session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).count() == 0
        assert not (bus.path / PROJECT / layout.DELIVERY_PACKAGE_JSON).exists()
    finally:
        drop_thread(session, request_id)
