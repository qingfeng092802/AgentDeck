"""T109：`snapshot.ref` 的接口定稿 —— 句柄指向的那份仓库在哪儿、由谁说了算（FR-054｜R-14，T-4 闭合）。

档位：`tests/guards/`，混合文件。一~三 + 五是纯函数 / 临时 git 仓库 / 源码 AST（fast，不碰 PG）；
四要真 PG（skip 挂在 `engine` fixture 上，口径同 T108 —— 不逐条挂 marker，免得新加一条要库的用例
忘了挂就在无 PG 机器上红成"机制坏了"那种假缺陷）。

**这六节各挡什么**（逐条对位 owner 2026-10-16 给 T109 的四条关键约束）：

| 约束 / 义务 | 节 |
|---|---|
| ① "哪个目录是存档根"要有权威出处（`params-ref.md §10`） | 一 |
| ③ 存档根约定 MUST 与 `record_point()` 拒 `local:` 同向 | 一、二、六 |
| 根的解析优先级与形态（不给就拒，不静默回退） | 二 |
| 失败必须发生在执行之前、库里零新行 | 三 |
| ② `orchestrate/graph.py` 的 `ref=` 已换成真实句柄（关闭判据那一半） | 四、五 |
| 原则 I：一个语义一个载体（变量名、前缀、拼接规则、调用点各一处） | 一、五 |
| 两边同向（不许只验"该出现的出现"，不验"不该出现的不出现"） | 六 |

**为什么第一节不是简单地"读文档然后和常量比字符串"**：那是拿实现读实现。这里的绑定是行为式的 ——
用例把**从围栏里读出的那个变量名**写进 `os.environ`，再看 `resolve_bus_root()` 跟不跟着走；
改名只改一侧，这一条就红。§9 用同一程序，区别在 §10 明写了"本节由判据消费，不由运行时消费"。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import acp
from acp.archive import cadence, root
from acp.bus import layout
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.graph import OrchestrateDeps, advance_request, build_graph
from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft
from acp.persistence import snapshots as snap
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import EventLog, Snapshot
from acp.protocol import ArtifactFloor

#: 扫描根跟着**被 import 的那棵树**走（口径同 `evidence/phase-08.md §十五` 的 P2-1 修正）：
#: 变异跑改的是副本，按字面路径拼出来的仓库根读不到副本 ⇒ 这批判据就永远进不了账。
PKG_ROOT = Path(acp.__file__).resolve().parent
SRC_ROOT = PKG_ROOT
REPO_ROOT = PKG_ROOT.parents[1]
PARAMS_REF = REPO_ROOT / "config" / "params-ref.md"

#: 本节围栏的锚点。**按标题定位而不是"文件里第一个 ```text 围栏"** —— 后者会随着 params-ref
#: 以后新增小节而悄悄指到别人的清单上（判据漂到隔壁那一类）。
SECTION_ANCHOR = "## 10. 存档根与"

#: 围栏里 MUST 恰有这五个键。多一个 = 一份没人解析的第二清单；少一个 = 权威源缺一角。
SPEC_KEYS = frozenset(
    {"ACP_BUS_ROOT", "ARCHIVE_ROOT", "SNAPSHOT_REF", "OBJECT_NAME", "TEMPORARY_REF_PREFIX"}
)

PROJECT = "demo"
OTHER_PROJECT = "demo-other"
CARD = "T01"
CARD2 = "T02"
NOW = dt.datetime(2026, 10, 16, 9, 30, tzinfo=dt.timezone.utc)
FLOOR = ArtifactFloor(min_bytes=512, min_lines=12, format="html")
DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": ["联系方式用邮箱还是表单？"]}


class SpecError(RuntimeError):
    """`params-ref.md §10` 读不出 / 键集不等于名单 ⇒ 抛。

    "读不出"从来不许被当成通过：那正是 §9 那条程序沿用下来的理由。
    """


# ------------------------------------------------------------------ 围栏解析（本文件的"权威源"入口）


def parse_spec(text_body: str) -> dict[str, str]:
    """从 §10 的 ```text 围栏里读出那几行 `键=值`。

    与 `registry/loader.py` 读 §9 的做法同一条程序：**键集必须恰等于 `SPEC_KEYS`**，
    重复键拒，围栏里出现 `#` 注释行以外的一切非 `k=v` 行也拒（注释允许，因为围栏同时是文档）。
    """
    start = text_body.find(SECTION_ANCHOR)
    if start < 0:
        raise SpecError("params-ref 里没有 §10 那一节 ⇒ 存档根的权威出处不见了")
    body = text_body[start:]
    fence = re.search(r"```text\n(.*?)\n```", body, re.DOTALL)
    if fence is None:
        raise SpecError("§10 里读不出 ```text 围栏 ⇒ 权威源换了形态，代码侧的常量没人核对")
    spec: dict[str, str] = {}
    for line in fence.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SpecError(f"§10 围栏里有一行不是 `键=值`：{stripped!r}")
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in spec:
            raise SpecError(f"§10 围栏里的键 {key!r} 出现了两次 ⇒ 哪一条作数读不出来")
        spec[key] = value.strip()
    if frozenset(spec) != SPEC_KEYS:
        raise SpecError(
            f"§10 围栏的键集不等于名单：多 {sorted(frozenset(spec) - SPEC_KEYS)} "
            f"缺 {sorted(SPEC_KEYS - frozenset(spec))}"
        )
    return spec


def spec() -> dict[str, str]:
    return parse_spec(PARAMS_REF.read_text(encoding="utf-8"))


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return done.stdout


def recorder(argv_log: list[tuple[str, ...]], *, code: int = 0, stdout: str = ""):
    def runner(argv: tuple[str, ...], cwd: Path) -> cadence.GitResult:
        argv_log.append(argv)
        return cadence.GitResult(code=code, stdout=stdout)

    return runner


def card(task_id: str, depends_on: list[str] | None = None) -> CardDraft:
    return CardDraft(
        task_id=task_id,
        title=f"卡 {task_id}",
        difficulty=2,
        skill_tags=["html", "css"],
        depends_on=depends_on or [],
        artifact_floor=FLOOR,
        acceptance_criteria=["首屏有标题"],
    )


class StubSplitter:
    def __init__(self, drafts: list[CardDraft]) -> None:
        self.drafts = drafts

    def split(self, summary_digest):
        return list(self.drafts)

    def acceptance_links(self, summary_digest):
        return [
            AcceptanceLink(
                task_id=draft.task_id,
                requirement_item=f"需求条目：{draft.title}",
                acceptance_item=item,
                produced_by="stub-orchestrator",
            )
            for draft in self.drafts
            for item in draft.acceptance_criteria
        ]


# ------------------------------------------------------------------ 夹具

@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG（拆单后的 `ref` 与轮次只在库里读得出），整条 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


def seed_request(session: Session, *, project_key: str = PROJECT) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(),
        project_key=project_key,
        raw_input="做个介绍公司产品的静态网站",
        summary_digest=DIGEST,
        clarification_waived=True,
        status="INTAKE",
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def split(session: Session, request_id: uuid.UUID, bus_root: Path | None, *drafts: CardDraft):
    """走一次真拆单（`advance_request` ⇒ `Plan → Split → Normalize`）。"""
    return advance_request(
        request_id,
        session=session,
        splitter=StubSplitter(list(drafts) or [card(CARD)]),
        checkpointer=InMemorySaver(),
        bus_root=bus_root,
    )


# ---------------------------------------------------------------- 一、权威出处 = params-ref §10


def test_the_section_declares_exactly_the_five_keys_it_should() -> None:
    """围栏能读出来，且键集恰等于名单 —— 这一条同时是其余四条的"读得出"前提。"""
    assert set(spec()) == set(SPEC_KEYS)


def test_the_env_var_name_the_code_reads_is_the_one_the_section_declares(monkeypatch, tmp_path) -> None:
    """**行为式绑定**：把围栏里那个变量名写进进程环境，`resolve_bus_root()` 必须跟着走。

    两侧各改一次都会红在这里：只改文档的名字 ⇒ 代码读的还是旧名 ⇒ 取到默认值；
    只改代码的名字 ⇒ 上一行那个等式先红（同一个关系，两个方向）。
    名字相同但**没人读它**的那种错法（代码里另写一个变量名）也被这一对钉住。
    """
    declared = [name for name in spec() if name.startswith("ACP_")]
    assert len(declared) == 1, f"本节里 `ACP_` 起头的变量名不是恰好一个：{declared}"
    name = declared[0]
    assert name == root.BUS_ROOT_ENV, (
        f"文档声明的变量名 {name!r} 与代码读的 {root.BUS_ROOT_ENV!r} 不是一回事"
    )

    marker = str(tmp_path / "from-the-doc")
    monkeypatch.setenv(name, marker)
    assert str(root.resolve_bus_root()) == marker, "环境变量按文档那个名字设了而代码不认 ⇒ 权威源是假的"


def test_the_default_bus_root_name_has_only_one_carrier() -> None:
    """默认目录名：文档、`root.py`、`bus/layout.py`（契约 §1 的载体）三处必须是同一个值。

    两个载体就能漂：总线建在 A 名下、存档根去找 B 名，两边都自称对齐了契约。
    """
    declared = spec()["ACP_BUS_ROOT"]
    assert declared == root.DEFAULT_BUS_ROOT_NAME == layout.BUS_ROOT_NAME


def test_the_ref_shapes_come_from_the_section_not_from_the_code() -> None:
    """`SNAPSHOT_REF` / `TEMPORARY_REF_PREFIX` 两个前缀与 `cadence` 的常量同值。

    这一条不是形式主义：`record_point()` 拒的是 `cadence.TEMPORARY_REF_PREFIX` 起头的句柄，
    而文档说退役的是"`local:` 那个形态"。两者一旦分叉，"两侧同向"就只是一句注释。
    """
    declared_ref = spec()["SNAPSHOT_REF"]
    assert declared_ref.startswith(cadence.GIT_REF_PREFIX), (
        f"文档里的句柄形态 {declared_ref!r} 不是以代码那个前缀 {cadence.GIT_REF_PREFIX!r} 起头"
    )
    assert spec()["TEMPORARY_REF_PREFIX"] == cadence.TEMPORARY_REF_PREFIX


@pytest.mark.parametrize(
    "name",
    ["a" * 40, "f" * 64, "A" * 40, "z" * 40, "a" * 39, "a" * 41, "", "git:" + "a" * 40],
)
def test_the_object_name_rule_in_the_section_agrees_with_the_code(name: str) -> None:
    """同一条形状规则，两侧归一后逐个取值必须同判（含"文档松一档 / 代码松一档"两个方向）。

    代码那侧用 `snapshot_ref()` 的真实行为读（抛 = 拒），文档那侧用围栏里的正则现编译 ——
    抄一份正则进代码或反过来，改一边就红，而不是让两边各自漂。
    """
    declared = re.compile(spec()["OBJECT_NAME"])
    doc_accepts = bool(declared.match(name))
    try:
        cadence.snapshot_ref(name)
        code_accepts = True
    except cadence.ArchiveMisuse:
        code_accepts = False
    assert doc_accepts == code_accepts, f"{name!r}：文档判 {doc_accepts} 而代码判 {code_accepts}"


@pytest.mark.parametrize(
    "body, reason",
    [
        ("没有那一节", "§10 不见了 ⇒ 权威出处没了，判据红而不是改用代码里的常量继续跑"),
        ("## 10. 存档根与\n正文里没有围栏\n", "围栏换成别的形态（表格 / yaml）也一样红：没人解析的那份清单不算出处"),
        ("## 10. 存档根与\n```text\nACP_BUS_ROOT=AI项目台\n```\n", "键集缺四项"),
        (
            "## 10. 存档根与\n```text\nACP_BUS_ROOT=AI项目台\nARCHIVE_ROOT=x\nSNAPSHOT_REF=x\n"
            "OBJECT_NAME=x\nTEMPORARY_REF_PREFIX=x\nACP_UNUSED=y\n```\n",
            "多一个没人消费的键",
        ),
    ],
)
def test_a_section_that_cannot_be_read_is_an_error_not_a_pass(body: str, reason: str) -> None:
    """四种"读不出"全部抛（口径同 §9 的 loader：读不出当通过是最要的假绿）。"""
    with pytest.raises(SpecError):
        parse_spec(body)
    assert reason  # 这条注释是给读的人的：四种形态各自为什么算"读不出"写在参数里


def test_the_section_anchor_appears_exactly_once() -> None:
    """`## 10.` 这个锚点在 params-ref 里命中 MUST = **1**。

    两处就会出"改了隔壁那一处而判据还在读第一处"那种漂（`§9` 那条正文指针用的是同一个程序）。
    """
    body = PARAMS_REF.read_text(encoding="utf-8")
    assert body.count(SECTION_ANCHOR) == 1, "存档根的声明出现了两处 ⇒ 哪一条作数读不出来"


# ---------------------------------------------------------------- 二、根的解析与拒绝


def test_the_precedence_is_explicit_then_env_then_default(tmp_path) -> None:
    """显式参数 > `ACP_BUS_ROOT` > 默认目录名 —— 三层各量一次，顺序不许反。

    反过来的代价很具体：装配方（T123）传了 `bus_root=` 却被进程里一个陈旧的环境变量盖掉，
    存档就打到别人的目录里去了，而所有退出码都是 0。
    """
    explicit = tmp_path / "explicit"
    env_dir = tmp_path / "from-env"
    assert root.resolve_bus_root(bus_root=explicit, env={"ACP_BUS_ROOT": str(env_dir)}) == explicit
    assert root.resolve_bus_root(env={"ACP_BUS_ROOT": str(env_dir)}) == env_dir
    assert root.resolve_bus_root(env={}) == Path(layout.BUS_ROOT_NAME)


@pytest.mark.parametrize("blank", ["", " ", "\t"], ids=["empty", "space", "tab"])
def test_a_blank_env_value_is_treated_as_unset(blank: str) -> None:
    """空白值按"没设"处理：`Path("")` resolve 出来是文件系统根，那会往下走很远才失败。"""
    assert root.resolve_bus_root(env={"ACP_BUS_ROOT": blank}) == Path(layout.BUS_ROOT_NAME)


def test_the_archive_root_rule_is_the_bus_project_directory_itself(tmp_path) -> None:
    """`ARCHIVE_ROOT=<bus_root>/<project_key>`：拼接规则借 `layout.project_dir()`，不在这里另写一份。"""
    assert root.archive_root("p1", bus_root=tmp_path) == tmp_path / "p1"
    declared = spec()["ARCHIVE_ROOT"]
    assert declared == "<bus_root>/<project_key>", f"文档那条规则改了而代码没跟：{declared!r}"


@pytest.mark.parametrize("bad", ["", "DEMO", "p 1", "-p", "p/x", "p/../x"], ids=lambda v: repr(v))
def test_an_illegal_project_key_never_becomes_a_directory(bad: str) -> None:
    """`project_key` 形态非法 ⇒ 抛，不拼出目录。

    放过去会出两件坏事：`p/../x` 那种能把存档根挪到总线外面去，空串能把存档根拼成总线根本身。
    """
    with pytest.raises(ValueError):
        root.archive_root(bad, bus_root=Path("/tmp"))


def test_the_boundary_check_uses_no_new_git_subcommand() -> None:
    """这道顶层自检用的是允许名单里**已有**的 `rev-parse` ⇒ 接口定稿没有把 git 面扩大。"""
    cadence.assert_allowed(cadence.TOPLEVEL_ARGV)
    assert tuple(cadence.ALLOWED_SUBCOMMANDS) == ("add", "commit", "rev-parse"), (
        "允许名单被改动过：T108 那笔的封闭集合是安全边界，不因为 T109 需要读顶层就放宽"
    )


def test_a_root_that_is_not_a_repository_toplevel_is_refused(bus) -> None:
    """目录存在、但它只是**别人仓库里的一个子目录** ⇒ 拒，且一条 `add` 都没跑。

    这是本文件最要紧的一条负例：总线目录常常就嵌在某个仓库里（把 `AI项目台/` 放在平台仓库根下就是这样）。
    不加这道检查，`add -A` + `commit` 会向上找到那个仓库、把它的整棵工作树提交掉，
    而退出码一路是 0 —— 那种"存档成功"比崩溃更难发现。

    构造方式：让 `<bus>/<project>` 是一个仓库，然后把存档根指到它的**父**目录 `<bus>` 的一个子项目上
    —— `bus.with_project()` 建的是 `<bus>/<project>`，所以 `<bus>/demo` 是顶层（正向），
    而 `<bus>/demo/nested` 存在、向上找到的顶层却是 `<bus>/demo`（负向）。
    """
    project = bus.with_project(PROJECT)
    nested = project / "nested"
    nested.mkdir()

    calls: list[tuple[str, ...]] = []
    # 存档根解析 = <bus>/nested（不存在那一档在下一条用例里，这里用 `bus_root=project` 让解析落到 nested）
    with pytest.raises(root.ArchiveRootMisuse) as caught:
        root.require_archive_root(
            "nested", bus_root=project, env={}, runner=recorder(calls, stdout=str(project) + "\n")
        )
    assert caught.value.code == "archive_root_not_toplevel", "子目录冒充存档根 ⇒ 必须拒"
    assert [argv[0] for argv in calls] == ["rev-parse"], (
        "拒之前只该跑那一次只读检查，`add` 一次都不许跑"
    )
    assert "fatal" not in str(caught.value), "错误消息不回显 git 的原文"


def test_the_boundary_check_runs_against_a_real_repository(bus) -> None:
    """正向那一半用**真 git** 量一次：桩只能证明"代码相信了什么"，不能证明"git 真怎么说"。

    这一条是上一条的对照物 —— 上一条里 `--show-toplevel` 的读数是我给的桩，这一条里它是 git 自己写的。
    """
    project = bus.with_project(PROJECT)
    nested = project / "nested"
    nested.mkdir()
    assert root.repository_toplevel(project) == project
    assert root.repository_toplevel(nested) == project, "向上找顶层这件事 git 真会做 ⇒ 那道检查不是摆设"


def test_a_real_repository_top_level_is_accepted(bus) -> None:
    """目录存在且它自己就是顶层 ⇒ 原样返回，供 `commit_archive_point()` 用。"""
    project = bus.with_project(PROJECT)
    assert root.require_archive_root(PROJECT, bus_root=bus.path, env={}) == project


def test_a_missing_root_refuses_before_any_git_runs(tmp_path) -> None:
    """根不存在 ⇒ `archive_root_missing`，且 runner 一次都没被调到（失败关闭的证据是"没跑过"，不是"抛了"）。"""
    calls: list[tuple[str, ...]] = []
    with pytest.raises(root.ArchiveRootMisuse) as caught:
        root.require_archive_root("nope", bus_root=tmp_path / "nowhere", env={}, runner=recorder(calls))
    assert caught.value.code == "archive_root_missing"
    assert calls == []


# ---------------------------------------------------------------- 三、口径收成一处（AST）


def _call_sites(name: str) -> list[str]:
    """src 树里 `name(...)` 的调用点所在文件（相对 `src/acp` 的 POSIX 路径，可重复）。"""
    sites: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if getattr(func, "id", "") == name or getattr(func, "attr", "") == name:
                    sites.append(path.relative_to(SRC_ROOT).as_posix())
    return sites


def test_the_pre_work_beat_has_exactly_one_production_call_site() -> None:
    """`commit_archive_point()` 在 `src/` 里的调用点 = **只有 `orchestrate/graph.py`**。

    这一条同时是两件事的证据：㉝ 要的那次换句柄真的接进了产品路径（不是只有判据在驱动），
    和 T123 的 ㉜ 已经缩窄为"只剩完工后那一拍"（开工前这一拍不再是零调用方）。
    """
    assert _call_sites("commit_archive_point") == ["orchestrate/graph.py"]


def test_the_archive_root_has_exactly_one_resolver_and_three_known_callers() -> None:
    """解析器仍只有一个（`resolve_bus_root()` 只在 `root.py` 内被调），调用者从两处变成**三处已登记的**。

    **这一条的期望值被 T110 改过一次（改前一处），被笔 Y 改第二次（改前两处 ⇒ 三处）**。
    T110 那一发：`archive/restore.py` 落地后，恢复路径也要问一次"存档根在哪儿"。
    笔 Y 那一发（owner 2026-10-20 裁决 1）：真跑入口 `realrun.py` 在把子进程的家交给 transport 之前，
    问的正是**同一个** `require_archive_root()` —— 要的就是它那道"这个目录是不是自己的仓库顶层"的边界自检，
    所以这里 MUST NOT 出现第二个解析函数，也不许在 `realrun.py` 里重拼 `<bus_root>/<project_key>`。
    这是有意改写而不是解析面失守：失守的形状是**有人在自己文件里重拼那条拼接规则**，
    而三个调用者走的是同一个函数、同一份优先级、同一道 `--show-toplevel` 边界自检
    （判据 = 第二条断言 `resolve_bus_root` 仍只在 `root.py` 内 +
    `test_recovery_idempotent.py::test_recover_does_not_re_derive_the_archive_root` +
    `test_realrun_driver.py::test_the_confined_home_is_the_executors_workspace_dir_inside_the_checked_root`）。
    ⇒ 名单按 `(文件)` 钉且**排序比**：第四处调用者出现就红，哪怕它只在注释里像个调用。
    """
    assert sorted(set(_call_sites("require_archive_root"))) == [
        "archive/restore.py",
        "orchestrate/graph.py",
        "realrun.py",
    ], "存档根的调用者超出这三处 ⇒ 要么又开了一条解析路径，要么该把新调用点登记进来"
    assert sorted(set(_call_sites("resolve_bus_root"))) == ["archive/root.py"]


def test_no_source_file_builds_the_temporary_handle_anymore() -> None:
    """`"local:..."` 这个字符串常量在 `src/` 里只许出现在**它的定义处**（`cadence.py` 那一行常量）。

    按 AST 取字符串常量而不是 grep 正文：注释与文档里提到"临时形态"是允许的（要禁的话就成了
    判据被自己正文复述那种漂），而"有人又拼了一句自指句柄"必须红。
    """
    hits: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(cadence.TEMPORARY_REF_PREFIX):
                    hits.append(path.relative_to(SRC_ROOT).as_posix())
    assert hits == ["archive/cadence.py"], f"临时形态的字面量出现在多处：{hits}"


def test_the_graph_no_longer_imports_the_snapshot_model() -> None:
    """`orchestrate/graph.py` 不再从 `models_registry` import 任何东西 ⇒ EN-12 的写者移走了（T-15 兑现的那一半）。"""
    tree = ast.parse((SRC_ROOT / "orchestrate" / "graph.py").read_text(encoding="utf-8"))
    imported = {
        f"{node.module}:{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    registry = [item for item in imported if item.startswith("acp.persistence.models_registry")]
    assert registry == [], f"graph 还在直接碰注册表模型：{registry}"


def test_the_closure_reading_still_holds() -> None:
    """owner 给的关闭判据本身落成用例：`local:registry` 在 `orchestrate/graph.py` 里命中 **0**。

    把它写成用例而不是只在台账里记一次，是因为这一条会被"注释里顺手复述一遍原文"打破 ——
    那种红是好事：判据的字面串只能有一个出处，历史那句话该写在 `evidence/` 与 `data-model.md` 里。
    """
    body = (SRC_ROOT / "orchestrate" / "graph.py").read_text(encoding="utf-8")
    assert body.count("local:registry") == 0, "关闭判据那三个字面串又出现在 graph 里了"


# ---------------------------------------------------------------- 四、端到端：真实句柄（真 PG + 真 git）


def test_a_split_registers_a_handle_that_resolves_in_the_archive_root(session, bus) -> None:
    """拆一次单 ⇒ 库里那行的 `ref` = 文档那个前缀 + 一个 `cat-file -e` 查得出来的对象名。

    这就是"真实句柄"与 T-15 那句自指的分界：**这一行说的东西在盘上验得出来**。
    `git:` 形状对了但对象不存在那种半程态（commit 失败还继续写）也一并被这一条挡住。
    """
    project = bus.with_project(PROJECT)
    request = seed_request(session)
    state = split(session, request.id, bus.path, card(CARD))

    row = session.get(Snapshot, state["snapshot_id"])
    assert row is not None
    prefix = re.compile(spec()["OBJECT_NAME"])
    assert row.ref.startswith(cadence.GIT_REF_PREFIX)
    name = row.ref[len(cadence.GIT_REF_PREFIX) :]
    assert prefix.match(name), f"句柄尾巴不是本节那个形状：{row.ref!r}"
    git(project, "cat-file", "-e", f"{name}^{{commit}}")  # check=True ⇒ 查不出来就直接红在这里


def test_the_registered_object_is_the_archive_head_and_the_commit_body_says_the_same_round(session, bus) -> None:
    """库里那一行与存档根里那个提交说的是**同一轮**：HEAD 相等 + 正文点名拍次 / 轮 / 项目 / 卡。

    两边各写一份比两边都不写更容易漂 —— 漂了还能 diff 出来，所以这一对必须一起量。
    """
    project = bus.with_project(PROJECT)
    request = seed_request(session)
    state = split(session, request.id, bus.path, card(CARD), card(CARD2, [CARD]))
    row = session.get(Snapshot, state["snapshot_id"])

    head = git(project, "rev-parse", "HEAD").strip()
    assert row.ref == f"{cadence.GIT_REF_PREFIX}{head}", "登记的句柄不等于存档根的 HEAD ⇒ 指到了一个没打过的点"
    message = git(project, "log", "-1", "--pretty=%B").strip()
    assert message == cadence.commit_message(
        phase=cadence.OPEN_PHASE,
        round_no=int(row.round_no),
        project_key=PROJECT,
        task_card_ids=[str(item) for item in state["card_order"]],
    ), f"提交正文与库里那行分叉了：{message!r}"


def test_rounds_count_per_project_and_do_not_stay_at_one(session, bus) -> None:
    """同项目第二次拆单 = 第 2 轮；另一个项目仍从 1 起。

    这一条钉的是被 T108 留下的另一半：那时 `round_no` 硬编码 1，"每轮一个点"在数轮这件事上读不出来
    （同一个项目永远第 1 轮，`revert` 挑点也就无从挑起）。
    """
    bus.with_project(PROJECT)
    bus.with_project(OTHER_PROJECT)
    first = seed_request(session)
    state = split(session, first.id, bus.path, card(CARD))
    assert int(session.get(Snapshot, state["snapshot_id"]).round_no) == 1

    second = seed_request(session)
    state2 = split(session, second.id, bus.path, card("T09"))
    assert int(session.get(Snapshot, state2["snapshot_id"]).round_no) == 2, "第二轮还登记成 1 ⇒ 轮次没数起来"

    elsewhere = seed_request(session, project_key=OTHER_PROJECT)
    state3 = split(session, elsewhere.id, bus.path, card("T77"))
    assert int(session.get(Snapshot, state3["snapshot_id"]).round_no) == 1, "别的项目被这条链的轮次污染了"


def test_the_graph_path_never_writes_a_temporary_handle_again(session, bus) -> None:
    """跑完两次拆单，库里 `local:%` 的行数 = 种下去的那一行（**新增全是真实句柄**）。

    与 T108 第六节那条的分工：那里钉"既有那行不被改写"，这里钉"新路径不再产生这种行"。
    两边都要 —— 只钉前者会放过"又开始写临时句柄"这种回退。
    """
    bus.with_project(PROJECT)
    stale_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=stale_id,
            round_no=1,
            phase=cadence.OPEN_PHASE,
            task_card_ids=["T90"],
            ref=f"{cadence.TEMPORARY_REF_PREFIX}registry/snapshot-{stale_id}",
            rolled_back_at=None,
        )
    )
    session.flush()

    for index in (1, 2):
        request = seed_request(session)
        split(session, request.id, bus.path, card(f"T{index:02d}"))

    temporary = session.scalars(select(Snapshot).where(Snapshot.ref.like("local:%"))).all()
    assert [str(row.id) for row in temporary] == [str(stale_id)], "新的自指句柄又进库了"


def test_each_registered_point_leaves_one_snapshot_event_row(session, bus) -> None:
    """每登记一个存档点，EN-14 就多一条 `SNAPSHOT` 留痕（裁决 2 收下的那一半在端到端上也成立）。"""
    bus.with_project(PROJECT)
    before = session.scalar(select(EventLog).where(EventLog.kind == snap.SNAPSHOT_KIND)) is not None
    request = seed_request(session)
    state = split(session, request.id, bus.path, card(CARD))
    rows = list(
        session.scalars(select(EventLog).where(EventLog.kind == snap.SNAPSHOT_KIND))
    )
    assert len(rows) == 1 + (1 if before else 0), f"留痕条数与登记条数分叉：{len(rows)}"
    assert rows[-1].entity_id is not None


def test_a_missing_archive_root_splits_nothing(session, tmp_path) -> None:
    """存档根不存在 ⇒ 拆单整条失败，EN-01 / EN-02 / EN-12 一行都没动（失败在写之前）。

    这一条是"没有真仓库就别装成有"的另一半：拒绝要**贵到调用方不可能忽略**，
    否则"缺配置"会退化成一堆指向空气的存档行。
    """
    cards_before = session.scalar(text("select count(*) from task_card"))
    snapshots_before = session.scalar(text("select count(*) from snapshot"))
    request = seed_request(session)

    with pytest.raises(root.ArchiveRootMisuse) as caught:
        split(session, request.id, tmp_path / "no-bus-here", card(CARD))

    assert caught.value.code == "archive_root_missing"
    assert str(session.get(FeatureRequest, request.id).status) == "INTAKE"
    assert session.scalar(text("select count(*) from task_card")) == cards_before
    assert session.scalar(text("select count(*) from snapshot")) == snapshots_before


def test_the_refusal_message_names_the_variable_and_carries_no_credential(session, tmp_path) -> None:
    """消息点名变量名与那个目录（配置问题要能照着改），但不带 git 原文、更不带 DSN 里的口令。

    凭据面口径沿用 T105 / T108：只带变量名、字段名、退出码、原因词。这里不打印 DSN，
    只把它的口令取出来断"不在消息里"。
    """
    request = seed_request(session)
    with pytest.raises(root.ArchiveRootMisuse) as caught:
        split(session, request.id, tmp_path / "no-bus-here", card(CARD))
    message = str(caught.value)

    password = make_url(db_url()).password
    assert root.BUS_ROOT_ENV in message, "配置类错误必须点名该改哪个变量，否则只能靠猜"
    assert "fatal" not in message
    if password:
        assert password not in message, "错误消息里出现了 DSN 的口令"


# ---------------------------------------------------------------- 五、显式参数与默认路径同形


def test_an_explicit_bus_root_and_the_env_var_produce_the_same_row(session, bus) -> None:
    """两条入口给出**同一件事**：同一项目下，显式 `bus_root=` 与 `ACP_BUS_ROOT` 各自打一拍，轮次连着数。

    这一条防的是"env 那条路只是测试里才通"的假象：生产部署只设变量，装配方只传参数，
    两者若走两套解析，`revert` 就会在其中一个目录下找不到另一个的点。
    """
    first = bus.with_project(PROJECT)
    request = seed_request(session)
    split(session, request.id, bus.path, card(CARD))

    os.environ[root.BUS_ROOT_ENV] = str(bus.path)
    try:
        second = seed_request(session)
        state = advance_request(
            second.id,
            session=session,
            splitter=StubSplitter([card("T05")]),
            checkpointer=InMemorySaver(),
            # 显式不给 ⇒ 走环境变量那一档
        )
    finally:
        os.environ.pop(root.BUS_ROOT_ENV, None)

    row = session.get(Snapshot, state["snapshot_id"])
    assert int(row.round_no) == 2
    head = git(first, "rev-parse", "HEAD").strip()
    assert row.ref == f"{cadence.GIT_REF_PREFIX}{head}", "两条入口打到了不同的仓库：句柄与 HEAD 分叉"


def test_the_deposited_object_is_enumerable_from_the_repository_side(bus) -> None:
    """从仓库这一侧数：连打两拍 ⇒ 历史上多两个提交，且都在同一条线上。

    FR-054 的"可枚举"在盘上的那一半（库里那一半在 T108 第五节）：如果某一拍没真提交，
    这个计数就会少一个，而所有断言看起来都对。
    """
    project = bus.with_project(PROJECT)
    # `--all` 而不是 `--all HEAD`：`bus.with_project()` 建的是一个**还没有任何提交**的仓库
    # （`git init` + 身份），带 `HEAD` 会退 128。这一条同时是"空轮也留点"的另一面证据 ——
    # 第一拍就是在 unborn 分支上打出来的，`--allow-empty` 在那种状态下也得成立。
    before = len(git(project, "rev-list", "--all").splitlines())
    for index, phase in enumerate((cadence.OPEN_PHASE, cadence.CLOSE_PHASE), start=1):
        cadence.commit_archive_point(
            project,
            phase=phase,
            round_no=index,
            project_key=PROJECT,
            task_card_ids=[f"T{index:02d}"],
        )
    after = len(git(project, "rev-list", "--all").splitlines())
    assert after - before == 2, f"两拍只留下了 {after - before} 个提交"
    assert before == 0, "前置现场就不该有提交：这一条的 0 是'两拍各一个'的分母"


# ---------------------------------------------------------------- 六、两边同向（不许只验一半）


def test_the_assembler_accepts_a_real_handle_and_refuses_a_temporary_one(session) -> None:
    """同一个句柄，`is_real_handle()`（拼装那一侧）与 `record_point()`（仓储那一侧）的判断必须同向。

    这是 owner 第 3 条约束的落点：§10 说真实句柄是 `git:<对象名>`，而仓储说拒 `local:` 起头。
    任何一侧单独放宽都会让"两侧同向"变成一句注释 —— 所以这里同时量"该收的收了"与"该拒的拒了"，
    而且拒的时候**库里零新行**（`rows == 0` 才是"执行之前拒"的证据，抛错本身不是）。
    """
    real = f"{cadence.GIT_REF_PREFIX}{'a' * 40}"
    temporary = f"{cadence.TEMPORARY_REF_PREFIX}registry/snapshot-1"
    assert cadence.is_real_handle(real) is True
    assert cadence.is_real_handle(temporary) is False

    rows_before = session.scalar(text("select count(*) from snapshot"))
    with pytest.raises(snap.SnapshotMisuse) as caught:
        snap.record_point(
            session,
            project_key=PROJECT,
            round_no=1,
            phase=cadence.OPEN_PHASE,
            task_card_ids=[CARD],
            ref=temporary,
            now=NOW,
        )
    assert caught.value.code == "ref_not_a_real_handle"
    assert "T-15" in str(caught.value), "临时形态那一支的消息必须点名 T-15（两支共用同一个 code，归因靠这个串）"
    assert session.scalar(text("select count(*) from snapshot")) == rows_before

    row = snap.record_point(
        session,
        project_key=PROJECT,
        round_no=1,
        phase=cadence.OPEN_PHASE,
        task_card_ids=[CARD],
        ref=real,
        now=NOW,
    )
    assert row.ref == real, "真实形态被拒 ⇒ §10 与仓储两边反向"


def test_the_graph_still_builds_without_a_bus_root() -> None:
    """**装配期**不需要存档根 —— 缺根是"跑到 `_normalize` 那一刻"的失败，不是 compile 的失败。

    反向对照（owner 那条"变异没红先怀疑用例"的同一程序里"不许连带红"那一半）：
    `OrchestrateDeps` 新增 `bus_root` 之后，如果装配期就要根，所有只做结构检查的用例
    （节点表、边表、缺 checkpointer 那一类）都会被牵连成"配置缺失"，而那不是它们的判据。
    """
    graph = build_graph(
        OrchestrateDeps(session=object(), splitter=StubSplitter([card(CARD)]), checkpointer=InMemorySaver())
    )
    assert graph is not None
