"""T123 装配点（第 2 步 · 上半）：`python -m acp.run` 受理段那一跑的判据。

**这个文件为什么值得存在**：`run.py` 是平台上第一个"从命令行把一条需求推到卡点"的入口。它最容易出
的两种错都不是"算错"，而是**话说不给对的人**：

① 门（`--transport`）没有先说话 ⇒ 打错旗标的人读到"缺 `ACP_DB_DSN`"，一次误敲被归因成配置问题。
   本轮冒烟真撞上过：第一版把 `make_session_factory()` 放在门前面，三条拒绝路径一个字都不打印、退 1；
② 打印出来的号不是库里的号 ⇒ "看起来跑通了"，而批准之后恢复时无人对得上。

所以这里的判据集中在**顺序**与**读数来源**两件事上，不重复 `test_assembly.py` 已盯住的挂载面。

五节改六节（2026-10-16 第 2 步下半段第 1 项：`--resume` 接上了）：

- 节 一 = 静态形状：`--transport` 不带 `default=`、门在建会话之前、注册面在建行之前、
  `--request` / `--resume` 的互斥四格（都不给 / 都给 / 空白 / 不是 UUID）、两支分派各有调用点。
- 节 二 = 恢复面（不连库）：结论只从 EN-09 的 `state` 推、单据面四种形状各说哪一个代号、
  `run_resume` 也遵守"门先说话"、两支读数的打印面各说哪一个事实。
- 节 三 = 拒绝面：每种拒绝说哪一个代号、哪一档退出码，且**一次会话都没打开**。
- 节 四 = 仿真总管的诚实形状：摘要与需求原文无关；四张卡点名的文件 == `StubAdapter` 真交付的那四份；
  判据与验收映射喂给**生产校验器** `split.validate_links`，而不是本文件自己写一份比对。
- 节 五 = 四道"读数不成立"的拒绝各有证人（受理段三道 + 恢复段一道，用最小假会话喂，
  不必假造整张受理图）。
- 节 六 = 真 PG 那六发：跑到挂起、注册面先于图、**批准与驳回各真推一次 `--resume`**、
  没批过的与已经推过的两发拒绝。缺 `ACP_DB_DSN` 时只有这一节 skip，前五节照跑 ——
  装配面的回归不该等数据库。

③ 反向对照：本文件的中文措辞改掉 MUST NOT 变红。节 一 只认 Call 节点的关键字名与行序，
节 二 只认代号与退出码，节 三 只认集合相等与"生产校验器不抛"。判据若开始盯正文用词，那是自指。
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import os
import pathlib
import re
import uuid
from contextlib import closing, redirect_stdout
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp import chaos
from acp import run as run_module
from acp.adapters import stub
from acp.approval import repository as repo
from acp.approval.repository import pending_for
from acp.bus.gateway import ARTIFACT_FORMATS
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import graph as orchestrate_graph
from acp.orchestrate import intake
from acp.orchestrate.nodes import approval_gate as gate
from acp.orchestrate.nodes import split as split_node
from acp.orchestrate.nodes.plan import CLARIFICATION_KEY, SELF_COMPLETED_KEY, plan_requirement
from acp.persistence import checkpointer as cp
from acp.persistence.enums import values_of
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest, TaskCard

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
PROJECT = "acp-run"
RAW = "做一个介绍公司产品的静态网站，配色你看着办"
OTHER_RAW = "给内部巡检班组做一个能离线打开的检查清单站，联系方式留邮箱"
CARD_IDS = ("T01", "T02", "T03", "T04")


# ------------------------------------------------------------------ 小工具


def tree_of(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def func_body(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`run.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def call_lines(func: ast.FunctionDef, callee: str) -> list[int]:
    """该函数里对 `callee(...)` 的所有调用点行号（裸名调用与属性调用都算）。"""
    lines = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if called == callee:
            lines.append(node.lineno)
    return sorted(lines)


def first_line(tree: ast.Module, callee: str, *, within: str) -> int:
    lines = call_lines(func_body(tree, within), callee)
    assert lines, f"`{within}()` 里没有对 {callee}() 的调用 ⇒ 这条判据会退化成恒真"
    return lines[0]


def run_cli(monkeypatch, argv: list[str], *, guard_session: bool = True) -> tuple[int, list[str], int]:
    """跑一次 CLI，返回 (退出码, stdout 非空行, 建会话被尝试的次数)。

    `guard_session=True` 把 `make_session_factory` 换成一个**会数数的炸弹**："门先说话"于是
    不是一句注释，而是一次会被踩到、也会被数到的断言。
    """
    hits: list[int] = []

    def boom():
        hits.append(1)
        raise AssertionError("门还没说话就打开了会话")

    if guard_session:
        monkeypatch.setattr(run_module.session_module, "make_session_factory", boom)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run_module.main(argv)
    return code, [line for line in buffer.getvalue().splitlines() if line.strip()], len(hits)


# ---------------------------------------------------------------- 一、静态形状


def test_the_transport_argument_carries_no_default():
    """`--transport` 这一条 MUST NOT 有 `default=`。

    给了默认值就等于装配点允许"没选运输方式"地跑起来，而下一次误敲会是一次真实外部调用。
    这条只看关键字名 ⇒ 把旗标改名（如 `--transport-mode`）也会红：门的本体仍会拒，但 argv 那一层
    已经和它脱钩了，那正是"看起来有门、其实没人守"的形状。
    """
    calls = [
        node
        for node in ast.walk(func_body(tree_of(SRC), "build_parser"))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "add_argument"
        and node.args
        and getattr(node.args[0], "value", None) == "--transport"
    ]
    assert len(calls) == 1, f"`--transport` 的定义不止一处或根本没有：{len(calls)}"
    keywords = {kw.arg for kw in calls[0].keywords}
    assert "default" not in keywords, f"旗标被发了默认值 ⇒ 可以不选就往外发请求：{sorted(keywords)}"


def test_the_gate_speaks_before_the_database_is_opened_in_main():
    """`main()` 里的行序：门 → 建会话 → 受理。**顺序换了就红**。

    行为面另有 节 二 那几条守着（它们数"会话开没开"）；这条守的是"读代码的人把顺序改回去时当场红"，
    不必等一次真敲命令行。本轮实测到的那一发：门在建会话之后 ⇒ `--transport qoder` 静默退 1。
    """
    tree = tree_of(SRC)
    gate = first_line(tree, "resolve_transport_flag", within="main")
    factory = first_line(tree, "make_session_factory", within="main")
    called = first_line(tree, "run_intake", within="main")
    assert gate < factory < called, f"行序读成 门{gate}/会话{factory}/受理{called} ⇒ 门的话会被凭据的错盖住"


def test_the_registry_is_seeded_before_the_first_card_row():
    """EN-04 种子 + 注册落库 MUST 早于建行，建行 MUST 早于受理图。

    这是 `tasks.md` T123 行上那条 MUST（WorkBuddy `phase7-review` P1-2）的**静态半边**：EN-04 空表时
    拆完卡就走不到 `assign_guards`，而那次失败会远到看不出根因是"没种岗位"。真跑那一发在 节 六。

    两条入口（CLI 的 `run_intake` 与网页后端的 `run_intake_existing`）共用 `_prepare_intake`，
    所以这里读两段源码：入口那一段的**调用位置**，和 helper 自己那一段的**内容顺序** ——
    只认 helper 名字而不读它做了什么，等于把"种了没有"换成"写没写这个词"。
    """
    tree = tree_of(SRC)
    prepare = first_line(tree, "_prepare_intake", within="run_intake")
    build = first_line(tree, "new_request", within="run_intake")
    advance = first_line(tree, "_advance", within="run_intake")
    seed = first_line(tree, "seed_builtin_roles", within="_prepare_intake")
    persist = first_line(tree, "persist_registrations", within="_prepare_intake")
    assert seed < persist, f"`_prepare_intake()` 行序读成 种子{seed}/落库{persist} ⇒ 注册面会晚于岗位表"
    assert prepare < build < advance, f"行序读成 准备{prepare}/建行{build}/图{advance}"


def test_the_cli_routes_a_resume_to_run_resume_and_not_back_to_the_intake_side():
    """`main()` 的两支分派都得有证人：`--resume` 走 `run_resume`，且**只有这一处是分派**。

    **2026-10-30 改写（P1-8）**：`run_resume()` 的调用点从 1 处变成 2 处 —— 第二处是
    "`ACP_APPROVAL_MODE=auto` 时拆单免确认之后接着跑"，它与 `--resume` 那支共用同一个函数，
    而不是另写一份驱动（那才是"批准/免确认走两条不同的路"的老问题）。
    所以判据改成"两处都在会话里、第二处由 `_auto_pass_split()` 把关"，
    而不是放宽成"有几处都行"—— 第三处没有守卫就仍然会红。

    为什么单开一条：`--resume` 接上以后，最容易出的错不是"恢复不工作"，而是**分派回落到受理段**
    （有人把 `else` 那一支删掉、或把 `kind` 判反）。那样"恢复一条已裁决的单子"会把同一条需求重新投一遍
    —— 受理那一支的第一步就是建 EN-01 新行。`first_line()` 自带活性断言：`main()` 里没有 `run_resume()`
    调用时它当场红，而不是"看起来也过了"。
    """
    tree = tree_of(SRC)
    main_body = func_body(tree, "main")
    factory = first_line(tree, "make_session_factory", within="main")
    intake_line = first_line(tree, "run_intake", within="main")
    resume_lines = call_lines(main_body, "run_resume")
    auto_lines = call_lines(main_body, "_auto_pass_split")
    assert len(resume_lines) == 2, (
        f"`main()` 里 `run_resume()` 的调用点应是两处（--resume 分派 + auto 免确认后续跑）：{resume_lines}"
    )
    assert len(auto_lines) == 1, f"免确认那一支没有惟一的守门调用：{auto_lines}"
    assert auto_lines[0] < resume_lines[-1], (
        f"行序读成 免确认{auto_lines[0]}/续跑{resume_lines[-1]} ⇒ 没签单就先续跑了"
    )
    assert factory < intake_line and factory < resume_lines[0], (
        f"行序读成 会话{factory}/受理{intake_line}/恢复{resume_lines[0]} ⇒ 有一支不在会话里跑"
        "（两支的先后不构成判据：if/else 换边写是纯风格）"
    )


def test_resume_needs_a_request_id_and_nothing_else():
    """`--resume` 已接上（owner 2026-10-16 授权第 2 步下半段第 1 项）—— **这条是 2026-10-16 那条的改写**。

    before（原判据，标题 `test_resume_is_not_accepted_yet`）：`--resume` 必须被 `parse_args` 拒掉，
    用它钉"上半笔只做受理段"这条边界；
    after：`--resume <需求单号>` 是真路径，于是这一节改钉**命令行层的四格**：
    两个都给、都不给、`--resume` 不是 UUID 三格都退 2 且各说各的提示；给对了才产出 `(mode, UUID)`。
    留这条而不是删掉的理由：`--request` 与 `--resume` 的互斥是新增的失败面
    （"猜一次就把一条已裁决的单子重新投一遍"），它需要证人，而证人就是这一发。
    """
    def mode(argv: list[str]):
        return run_module._mode(run_module.build_parser().parse_args(argv))

    base = ["--project", PROJECT, "--transport", "stub"]
    request_id = uuid.uuid4()

    kind, value, hint = mode([*base, "--resume", str(request_id)])
    assert (kind, value) == (run_module.MODE_RESUME, request_id), (kind, value, hint)

    kind, value, hint = mode([*base, "--request", RAW])
    assert (kind, value) == (run_module.MODE_REQUEST, RAW), (kind, value, hint)

    for argv, expected_hint in (
        ([*base], run_module.USAGE_ONE_OF),
        ([*base, "--request", RAW, "--resume", str(request_id)], run_module.USAGE_ONE_OF),
        ([*base, "--request", "   "], run_module.USAGE_ONE_OF),
        ([*base, "--resume", "  "], run_module.USAGE_ONE_OF),
    ):
        bad_kind, bad_value, bad_hint = mode(argv)
        assert (bad_kind, bad_value) == ("", None), f"这组该被拒却成立了：{argv} ⇒ {bad_kind}/{bad_value}"
        assert bad_hint == expected_hint, (argv, bad_hint)

    bad_kind, _value, bad_hint = mode([*base, "--resume", "not-a-uuid"])
    assert bad_kind == "" and "UUID" in bad_hint, (bad_kind, bad_hint)


# ---------------------------------------------------------------- 二、恢复面（不连库）


def test_the_conclusion_comes_from_en09_state_not_from_argv():
    """结论映射表只收两个决定态，且键必须是 `repository` 的常量。

    这条判据钉的是三件事，缺一就退化成"读一个常量"：
    ① 键 = `{repo.APPROVED, repo.REJECTED}` —— 用常量而不是字面量，`_named()` 在 import 期就把它与
       `approval_state` 域对过账；
    ② 值 ⊆ `ApprovalGate` 认的那个域（`CARD_STATUS` 的键），两边不同域的话错误会变成一次 `GateMisuse`
       而不是"装配点拒了你"；
    ③ `PENDING` 与 `TIMED_OUT_CONVERGED` **不在表里** —— 把超时收敛当批准恢复，等于让一次超时
       变成一次自动放行（FR-044 的优先级表与超时语义都不认）。
    """
    assert set(run_module.CONCLUSION_BY_STATE) == {repo.APPROVED, repo.REJECTED}, run_module.CONCLUSION_BY_STATE
    assert set(run_module.CONCLUSION_BY_STATE.values()) <= set(gate.CARD_STATUS), run_module.CONCLUSION_BY_STATE
    for not_decisive in (repo.PENDING, repo.TIMED_OUT):
        assert not_decisive not in run_module.CONCLUSION_BY_STATE, f"{not_decisive} 被当成了可恢复的结论"


class _FakeTickets:
    """最小假会话：只回答 `_decided_ticket` 那一发 `query(ApprovalTicket).filter(...).all()`。"""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def query(self, model):
        assert model is ApprovalTicket, f"这道读数读的不是 EN-09：{model}"
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


def test_the_four_ticket_shapes_the_resume_guard_has_to_name(monkeypatch):
    """单据面的四种形状各说哪一个代号：没有、多于一个、没结论、有结论。

    与受理段的 节 四 同族：这些分支写在 `run_resume` 的上游，如果只能靠"把整条链跑一遍"才能触发，
    它们就是永不红的花纹。这里同样**明写证明不到什么**：假会话绕过 SQL 谓词，证的是拒绝逻辑与读数来源。
    """
    request_id = uuid.uuid4()

    with pytest.raises(run_module.RunFailure) as none:
        run_module._decided_ticket(_FakeTickets([]), request_id)
    assert none.value.code == run_module.TICKET_MISSING, none.value.args[0]

    two = [SimpleNamespace(id=uuid.uuid4(), state=repo.APPROVED), SimpleNamespace(id=uuid.uuid4(), state=repo.APPROVED)]
    with pytest.raises(run_module.RunFailure) as many:
        run_module._decided_ticket(_FakeTickets(two), request_id)
    assert many.value.code == run_module.TICKET_MISSING and str(two[0].id) in many.value.args[0], many.value.args[0]

    with pytest.raises(run_module.RunFailure) as pending:
        run_module._decided_ticket(_FakeTickets([SimpleNamespace(id=uuid.uuid4(), state=repo.PENDING)]), request_id)
    assert pending.value.code == run_module.NOT_DECISIVE, pending.value.args[0]

    decided = SimpleNamespace(id=uuid.uuid4(), state=repo.APPROVED)
    assert run_module._decided_ticket(_FakeTickets([decided]), request_id) is decided, "有结论时不该换对象或读第二份"


def test_run_resume_refuses_before_it_touches_the_session(monkeypatch):
    """`run_resume` 的第一次会话使用发生在门之后 —— 恢复段与受理段共用同一条纪律。

    做法是把假会话的**每一次**属性访问都记下来：门若没先说话，`session.get` 就会被碰到。
    """
    calls: list[str] = []

    class _Counting:
        def get(self, *args, **kwargs):
            calls.append("get")
            raise AssertionError("门还没说话就查了库")

    with pytest.raises(run_module.StubMisuse) as caught:
        run_module.run_resume(uuid.uuid4(), transport="qoder", bus_root=None, session=_Counting())
    assert caught.value.code == "transport_flag_unknown" and calls == [], (caught.value.code, calls)


def test_a_missing_request_row_is_refused_by_name(monkeypatch):
    """EN-01 里没这条需求单 ⇒ `feature_request_missing`，而不是让图去报"找不到 thread"。"""
    session = SimpleNamespace(get=lambda model, key: None)
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_resume(uuid.uuid4(), transport="stub", bus_root=None, session=session)
    assert caught.value.code == run_module.REQUEST_MISSING, caught.value.args[0]


def test_the_two_readings_print_as_two_different_facts():
    """`_report()` 两支各说各的事实：受理段说 `WAITING`，恢复段说 `RESUMED` + `CARDS`。

    这条守的是**打印面与读数对象的对应关系**，不是措辞（改文案不许红，见 R2 那类反向对照）：

    ① 恢复那一支必须把 `conclusion` 与 `state` 打出来 —— 那是"结论来自 EN-09"在人眼里的唯一凭据；
    ② 两支的判定词不许共用：`WAITING`（还等人在批）与 `RESUMED`（已经批过并推下去了）是两件不同的事实，
       共用一个词就会让人把前者读成后者；
    ③ 受理那一支的 HINT 里必须带上**这一发**的单据号与需求单号 ⇒ 写成示例常量就是教人对着别的单子按恢复；
    ④ 恢复那一支共三行，少一行（比如把 `RUN CARDS` 漏掉）就看不出卡态。
    """
    request_id, ticket_id = uuid.uuid4(), uuid.uuid4()
    awaiting = tuple(sorted((card_id, "READY") for card_id in CARD_IDS))

    def lines_of(fn, *args, **kwargs):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = fn(*args, **kwargs)
        return code, [line for line in buffer.getvalue().splitlines() if line.strip()]

    intake_code, intake_lines = lines_of(
        run_module._report,
        run_module.IntakeReport(
            feature_request_id=request_id, ticket_id=ticket_id, card_ids=CARD_IDS, request_status="SPLIT"
        ),
    )
    resume_code, resume_lines = lines_of(
        run_module._report,
        run_module.ResumeReport(
            feature_request_id=request_id,
            ticket_id=ticket_id,
            ticket_state=repo.APPROVED,
            conclusion=gate.APPROVE,
            waiting_before=(orchestrate_graph.APPROVAL_GATE,),
            waiting_after=(),
            card_statuses=awaiting,
            request_status="SPLIT",
        ),
    )
    assert (intake_code, resume_code) == (run_module.EXIT_OK, run_module.EXIT_OK), (intake_code, resume_code)

    assert intake_lines[0].startswith("RUN WAITING ::"), intake_lines[0]
    assert f"ticket={ticket_id}" in intake_lines[0] and f"request={request_id}" in intake_lines[0], intake_lines[0]
    assert f"cards={','.join(CARD_IDS)}" in intake_lines[0], intake_lines[0]
    assert not any("RUN RESUMED" in line for line in intake_lines), intake_lines
    assert any(str(ticket_id) in line and str(request_id) in line for line in intake_lines[1:]), (
        "HINT 里没带这一发的两个号 ⇒ 人会照着示例去恢复另一条单子"
    )

    assert resume_lines[0].startswith("RUN RESUMED ::"), resume_lines[0]
    assert f"conclusion={gate.APPROVE}" in resume_lines[0], resume_lines[0]
    assert f"state={repo.APPROVED}" in resume_lines[0], resume_lines[0]
    assert f"suspended_at={orchestrate_graph.APPROVAL_GATE}" in resume_lines[0], resume_lines[0]
    assert "next=-" in resume_lines[0], f"图到头了却要人以为还有下一跳：{resume_lines[0]}"
    assert resume_lines[1].startswith("RUN CARDS ::"), resume_lines[1]
    assert f"statuses={','.join(f'{card_id}:READY' for card_id in CARD_IDS)}" in resume_lines[1], resume_lines[1]
    assert not any("RUN WAITING" in line for line in resume_lines), resume_lines
    assert len(resume_lines) == 3, f"恢复那支该是三行（RESUMED / CARDS / HINT）：{resume_lines}"


def test_the_cli_sends_each_mode_to_its_own_half(monkeypatch):
    """两支分派的**行为**证人：`--request` 只走受理段，`--resume` 只走恢复段。

    上面 节 一 那条静态判据只数调用点，判不出"把条件取反"这一形（M22 注的正是 `kind == MODE_REQUEST`
    → `kind != MODE_REQUEST`：两支调用点仍各一处，静态那条照绿）。这里把两段各换成记录器、连会话工厂
    一起假掉 ⇒ 不碰库、不真发任何外部调用，证的就是"哪一种 argv 走哪一支"这一格。
    """
    seen: list[tuple[str, object]] = []
    request_id = uuid.uuid4()

    class _SessionCtx:
        def __enter__(self):
            return SimpleNamespace(label="fake-session")

        def __exit__(self, *exc):
            return False

    def fake_intake(raw, **kwargs):
        seen.append(("intake", raw))
        return run_module.IntakeReport(uuid.uuid4(), uuid.uuid4(), (), "SPLIT")

    def fake_resume(value, **kwargs):
        seen.append(("resume", value))
        return run_module.ResumeReport(
            feature_request_id=value,
            ticket_id=uuid.uuid4(),
            ticket_state=repo.APPROVED,
            conclusion=gate.APPROVE,
            waiting_before=(orchestrate_graph.APPROVAL_GATE,),
            waiting_after=(),
            card_statuses=(("T01", "READY"),),
            request_status="SPLIT",
        )

    monkeypatch.setattr(run_module, "run_intake", fake_intake)
    monkeypatch.setattr(run_module, "run_resume", fake_resume)
    monkeypatch.setattr(run_module.session_module, "make_session_factory", lambda: object())
    monkeypatch.setattr(run_module.session_module, "new_session", lambda factory: _SessionCtx())

    argv = ["--project", PROJECT, "--transport", "stub"]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code_request = run_module.main([*argv, "--request", RAW])
        code_resume = run_module.main([*argv, "--resume", str(request_id)])

    assert [name for name, _value in seen] == ["intake", "resume"], f"分派走错了支：{seen}"
    assert seen[0][1] == RAW, seen[0]
    assert seen[1][1] == request_id, f"恢复段收到的不是那个需求单号：{seen[1][1]!r}"
    assert (code_request, code_resume) == (run_module.EXIT_OK, run_module.EXIT_OK), buffer.getvalue()


# ---------------------------------------------------------------- 三、拒绝面


@pytest.mark.parametrize(
    "raw_transport, expected_code",
    [
        (None, "transport_flag_missing"),
        ("qoder", "transport_flag_unknown"),
        ("workbuddy", "transport_flag_unknown"),
        ("doubao", "transport_flag_unknown"),
        ("STUB", "transport_flag_unknown"),
        ("stub ", "transport_flag_unknown"),
    ],
)
def test_every_transport_refusal_stops_before_the_session(monkeypatch, raw_transport, expected_code):
    """六种敲法各退 **2**、各说**门**的代号，且一次都没打开会话。

    三支真适配器的名字排在被拒的一侧是故意的：`--transport qoder` 在人的直觉里"最像能跑"（场内真有一支），
    而它恰恰是这一发 MUST 拒的那种 —— 拒它才证明门不是"认不出就回落"。`STUB` / `"stub "` 拒的是大小写与
    空白，口径同"不替用户猜意图"。
    """
    argv = ["--project", PROJECT, "--request", RAW]
    if raw_transport is not None:
        argv += ["--transport", raw_transport]
    code, lines, opened = run_cli(monkeypatch, argv)
    assert (code, opened) == (2, 0), f"退 {code}、开了 {opened} 次会话：{lines}"
    assert lines[0] == f"RUN FAIL :: code={expected_code}", lines
    assert len(lines) == 2, f"门拒一次应该说两行（判定 + 归因），读到 {len(lines)} 行：{lines}"


def test_a_blank_request_is_a_usage_error_decided_before_the_credentials(monkeypatch):
    """空白串与"没给"同档：退 2、`arguments_missing`，且同样先于凭据。

    为什么单独一条：`--request "   "` 在 `if not args.request` 里是**真值**，第一版就这么放过去了，
    然后靠 `plan_requirement()` 的 `BlankRequirement` 在库里那一段拒 ⇒ 归因成了运行时错、退出码 1，
    而它是敲错。三种空白各喂一次：`""` / `"   "` / `"\\t"`（后者只在前者都漏了时才被抓到）。
    """
    for request in ("", "   ", "\t"):
        code, lines, opened = run_cli(
            monkeypatch, ["--project", PROJECT, "--transport", "stub", "--request", request]
        )
        assert (code, opened) == (2, 0), f"需求 {request!r} 退 {code}、开会话 {opened} 次：{lines}"
        assert "arguments_missing" in lines[0], lines


def test_the_credential_refusal_names_the_variable_and_nothing_else(monkeypatch):
    """门过了、库没配 ⇒ 退 1、点名变量**名**，不打印值，也不把它说成旗标问题。

    这一条同时是"门与凭据分两档"的证据：2 归敲错、1 归现场缺东西。这里 `ACP_DB_DSN` 是**被测状态**
    而不是跳过理由 ⇒ 用 `delenv` 造出来（`loader.db_url()` 每次调用现读环境，没有缓存，实测过）。
    """
    monkeypatch.delenv(DSN_ENV, raising=False)
    code, lines, _ = run_cli(
        monkeypatch, ["--project", PROJECT, "--request", RAW, "--transport", "stub"], guard_session=False
    )
    assert code == 1, f"缺凭据应该退 1（跑得起来但到不了挂起点），读到 {code}：{lines}"
    joined = "\n".join(lines)
    assert DSN_ENV in joined, f"没点名变量：{joined}"
    assert not re.search(r"//[^\s]*@|[?&]password=", joined), f"读数里出现了凭据形态：{joined}"
    assert "transport" not in joined, f"缺凭据被说成了旗标问题：{joined}"


# ---------------------------------------------------------------- 四、仿真总管的诚实形状


def test_the_digest_does_not_depend_on_what_was_asked():
    """两条完全不同的需求 ⇒ **同一份**摘要：这就是"仿真总管不读正文"的实证。

    `plan_requirement()` 的摘要只来自 `completer.complete(raw_input)`，而 `StubOrchestrator` 交回固定的
    三份补全项 ⇒ 摘要与 `raw_input` 无关。把它钉成断言而不是文档里一句话的理由：哪天有人给 `complete()`
    加"按关键词挑补全项"，这里先红 —— 而那一改动**必须**去改 `quickstart.md §6` 的"不构成 FR-002 实证"。
    """
    stub_manager = run_module.StubOrchestrator()
    one = plan_requirement(RAW, completer=stub_manager).summary_digest
    two = plan_requirement(OTHER_RAW, completer=stub_manager).summary_digest
    assert one == two, f"摘要开始随需求内容变了 ⇒ 仿真侧被当成了 FR-002 的证据：{one} vs {two}"
    assert one[SELF_COMPLETED_KEY] and one[CLARIFICATION_KEY] == [], one


def test_the_cards_name_exactly_the_files_the_stub_delivers():
    """四张卡点名的产物 == `StubAdapter` 真会交付的那四份（跨模块同值，两处各写一定漂）。

    这是 `quickstart.md §6` 边界 ④ 的前半：**卡上没点名的产物，运输层拒收**
    （`stub.UNNAMED_DELIVERABLE`），所以"卡说交付 styles.css"与"页子里真有 styles.css"必须是同一个事实。
    格式那一列也不抄字面量：与 `gateway.ARTIFACT_FORMATS`（从 `protocol/refs` 推导的那份域）比。
    """
    names = {file_name for _id, _title, file_name, _deps in run_module._CARDS}
    assert names == set(stub.PAGE_BODIES), f"卡点名的文件与交付集合不是一套：{sorted(names)} vs {sorted(stub.PAGE_BODIES)}"
    drafts = run_module.StubOrchestrator().split({})
    assert len(drafts) == len(run_module._CARDS)
    for draft, (_card_id, _title, file_name, _deps) in zip(drafts, run_module._CARDS):
        assert file_name in " ".join(draft.acceptance_criteria), f"{draft.task_id} 的判据没点名 {file_name}"
        expected = "css" if file_name.endswith(".css") else "html"
        assert draft.artifact_floor.format == expected, f"{file_name} 的格式声明成 {draft.artifact_floor.format!r}"
        assert draft.artifact_floor.format in ARTIFACT_FORMATS, ARTIFACT_FORMATS


def test_the_production_validator_accepts_the_map_and_refuses_a_renamed_item():
    """判据 ↔ 验收映射喂给**生产校验器**：正例不抛，改掉一条就抛。

    为什么不在本文件自己比对：`split.validate_links()` 的关系式有四条（字段非空、`task_id` 在清单里、
    每条判据恰被覆盖一次、映射不许写卡上没有的判据）。自己写一份 `set(a) == set(b)` 只盯得住其中一条。
    本轮冒烟撞上的那一发正是"两侧各写一份然后漂"：四张卡各两条判据全数漏配 ⇒ `MappingViolation`。
    """
    stub_manager = run_module.StubOrchestrator()
    drafts = stub_manager.split({})
    links = stub_manager.acceptance_links({})
    assert split_node.validate_links(drafts, links) == links, "生产校验器的关系式变了，而这里还在按旧形状交"
    assert len(links) == sum(len(draft.acceptance_criteria) for draft in drafts)

    first = links[0]
    drifted = (
        split_node.AcceptanceLink(
            task_id=first.task_id,
            requirement_item=first.requirement_item,
            acceptance_item="这条判据卡上根本没写",
            produced_by=first.produced_by,
        ),
    ) + links[1:]
    with pytest.raises(split_node.MappingViolation):
        split_node.validate_links(drafts, drifted)


# ---------------------------------------------------------------- 五、四道拒绝各有证人


class _FakeCards:
    """最小假会话：只回答 `_require_cards_awaiting` 那一发 `query(TaskCard).filter(...).all()`。"""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def query(self, model):
        assert model is TaskCard, f"这道拒绝读的不是 EN-02：{model}"
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


def test_no_ticket_in_en09_is_refused_instead_of_printing_a_made_up_number(monkeypatch):
    """EN-09 里没有 `PENDING` 单据 ⇒ 拒，代号 `request_not_intake`。

    明写这一发**证明不到什么**：假会话绕过了 SQL 谓词，所以证的是拒绝逻辑本身（读到 None 就说哪个代号）。
    "库里到底有没有那一行"归 节 六 的真跑。写它的唯一理由是：这道分支若没有证人，就是一段永不红的花纹。
    """
    request_id = uuid.uuid4()
    monkeypatch.setattr(run_module, "pending_for", lambda session, *, feature_request_id: None)
    with pytest.raises(run_module.RunFailure) as caught:
        run_module._require_pending_ticket(SimpleNamespace(), request_id)
    assert caught.value.code == run_module.ALREADY_RUNNING, caught.value.args[0]


def test_a_read_ticket_is_returned_verbatim(monkeypatch):
    """有单据就把**它的** id 交出去 —— 不是重新生成一个、也不是从 state 里捞。

    与上一条配对：上一条证"没有时拒"，这一条证"有时不改"。少了这一条，`_require_pending_ticket`
    可以随便返回一个新 UUID 而不被发现有两条分支都"看起来对"。
    """
    request_id, ticket_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        run_module, "pending_for", lambda session, *, feature_request_id: SimpleNamespace(id=ticket_id)
    )
    assert run_module._require_pending_ticket(SimpleNamespace(), request_id) == ticket_id


def test_cards_that_never_entered_the_gate_are_refused():
    """卡为空、或卡没全体在 `CARD_AWAITING` ⇒ 两道拒绝；全体在 ⇒ 按字典序交号。

    三段各是一格会真发生的错：空单（图跑完但一张卡没建）、状态越界（门建了单却没动卡，恢复时按卡找
    等待点落空）、以及顺序不稳定（打印出来的 `cards=` 那一列在两发之间会跳）。
    """
    request_id = uuid.uuid4()
    with pytest.raises(run_module.RunFailure) as empty:
        run_module._require_cards_awaiting(_FakeCards([]), request_id)
    assert "空单" in empty.value.args[0] and empty.value.code == run_module.ALREADY_RUNNING, empty.value.args[0]

    with pytest.raises(run_module.RunFailure) as off:
        run_module._require_cards_awaiting(
            _FakeCards(
                [
                    SimpleNamespace(id="T01", status=run_module.CARD_AWAITING),
                    SimpleNamespace(id="T02", status="DRAFT"),
                ]
            ),
            request_id,
        )
    assert "T02" in off.value.args[0] and "DRAFT" in off.value.args[0], off.value.args[0]
    assert off.value.code == run_module.ALREADY_RUNNING

    ordered = run_module._require_cards_awaiting(
        _FakeCards(
            [
                SimpleNamespace(id="T02", status=run_module.CARD_AWAITING),
                SimpleNamespace(id="T01", status=run_module.CARD_AWAITING),
            ]
        ),
        request_id,
    )
    assert ordered == ("T01", "T02"), ordered


def test_the_resume_reading_refuses_an_empty_card_face():
    """恢复**之后**的读数也不许交出空集：空 ⇒ `cards_missing`，非空 ⇒ 按卡号排序的 `(卡号, 现态)`。

    与上面那条同族但**代号不同**：`request_not_intake` 说的是"这条单子不在卡点上"，`cards_missing`
    说的是"单子推完了却没留下可继续调度的卡"。合成一格，人读到的归因就会指错方向（该去查 EN-02 的行，
    却去查卡点）。这一发同样明写证明不到什么：假会话绕过 SQL，证的是"读到空集说哪个代号"。
    """
    request_id = uuid.uuid4()
    with pytest.raises(run_module.RunFailure) as empty:
        run_module._require_card_statuses(_FakeCards([]), request_id)
    assert empty.value.code == run_module.CARDS_MISSING, empty.value.args[0]

    mixed = run_module._require_card_statuses(
        _FakeCards(
            [SimpleNamespace(id="T02", status="READY"), SimpleNamespace(id="T01", status=run_module.CARD_AWAITING)]
        ),
        request_id,
    )
    assert mixed == (("T01", run_module.CARD_AWAITING), ("T02", "READY")), mixed


# ---------------------------------------------------------------- 六、真 PG 那六发


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整条 skip（不是绿）")
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


def bus_root_of(bus) -> pathlib.Path:
    """把 `bus` 夹具的**根**交出去，但先把 `<根>/<项目>` 那个仓库建好。

    写成 helper 是因为这里踩过一次：`BusRoot.with_project()` 返回的是 `<根>/<project_key>`，
    而 `run_intake(bus_root=…)` 要的是根（它自己再拼一次项目键）。传深一层得到的是
    `AI项目台/acp-run/acp-run` ⇒ `archive_root_missing`，看着像装配点的错，其实是现场搭错。
    """
    bus.with_project(PROJECT)
    return bus.path


def _card_statuses(session, feature_request_id: uuid.UUID) -> tuple[tuple[str, str], ...]:
    """直接从 EN-02 读卡态，**不借 `run_resume` 的读数**。

    分成两次独立读数是有意为之：只比对 `report.card_statuses` 的话，"报告说 READY"与"库里是 READY"
    就成了同一个来源，装配点改成读图 state 也能过。这里每一条恢复判据都同时读两边。
    """
    return tuple(
        sorted(
            (card.id, str(card.status))
            for card in session.query(TaskCard).filter(TaskCard.feature_request_id == feature_request_id).all()
        )
    )


def _drop_thread(feature_request_id: uuid.UUID) -> None:
    """删掉这一发留在 checkpointer 里的行。

    会话回滚管不到它们：`_advance` 用的是**另一条连接**（psycopg，autocommit），自己提交。
    不清就会给下一个用例留一个"能恢复到一个已经不存在的需求单"的等待点。
    """
    thread = cp.thread_id_for(kind="A_SPLIT", feature_request_id=feature_request_id)
    with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
        chaos.kill_graph_state(conn, thread)


def test_a_real_run_stops_at_the_gate_and_the_ticket_it_prints_exists(bus, session):
    """真跑一遍受理段：打印的号是 EN-09 的现值，卡与需求单读到的是**被真推进过**的那一行。

    这是这一笔唯一"真跑"的一条，四件事各挡一种会发生的错：

    1. `report.ticket_id` == `pending_for()` 读回的那一行 ⇒ 打印的不是猜的；
    2. 四张卡的库里状态都等于 `run.CARD_AWAITING` ⇒ 恢复时按卡找等待点不落空；
    3. `report.card_ids` 与库里那四张同号 ⇒ 命令行那一列不是从图 state 里捞的；
    4. `report.request_status` 落在 EN-01 状态域内、且不等于建行时的 `INTAKE` ⇒
       那一列读的是真被推进过的行。**EN-01 里根本没有"待批"这一值**，所以这条同时是"没把门后的
       事实写进错误的列"的看守。
    """
    report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    try:
        ticket = pending_for(session, feature_request_id=report.feature_request_id)
        assert ticket is not None and ticket.id == report.ticket_id, (
            f"打印的单据号 {report.ticket_id} 在 EN-09 里读不到同一行 ⇒ 批准之后无人对得上"
        )
        rows = session.query(TaskCard).filter(TaskCard.feature_request_id == report.feature_request_id).all()
        assert {card.id for card in rows} == set(CARD_IDS), sorted(card.id for card in rows)
        assert {card.status for card in rows} == {run_module.CARD_AWAITING}, {card.id: card.status for card in rows}
        assert report.card_ids == CARD_IDS, report.card_ids

        domain = set(values_of("feature_request_status"))
        assert report.request_status in domain, f"EN-01 的状态域里没有 {report.request_status!r}"
        assert report.request_status != intake.INITIAL_STATUS, "建行之后没人推进过这条需求单，那一列是初始态"
        stored = session.execute(
            select(FeatureRequest.status).where(FeatureRequest.id == report.feature_request_id)
        ).scalar_one()
        assert stored == report.request_status, f"报告读的是 {report.request_status!r}，库里是 {stored!r}"
    finally:
        _drop_thread(report.feature_request_id)


def test_the_registry_face_is_wired_before_the_graph_runs(bus, session, monkeypatch):
    """EN-04 种子与注册落库**真的被调了**，且早于建行 —— 不止是行序看着对。

    与 节 一 那条静态判据配对：那条防"改顺序"，这条防"接线被整段删掉"。记录方式是把三个入口各包一层
    计数再看序列 ⇒ 有人把种子挪到建行之后，序列当场反掉。
    """
    order: list[str] = []
    real_seed = run_module.role_registry.seed_builtin_roles
    real_persist = run_module.persist_registrations
    real_new = intake.new_request

    def seed(s):
        order.append("seed")
        return real_seed(s)

    def persist(*args, **kwargs):
        order.append("persist")
        return real_persist(*args, **kwargs)

    def new_request(**kwargs):
        order.append("build_row")
        return real_new(**kwargs)

    monkeypatch.setattr(run_module.role_registry, "seed_builtin_roles", seed)
    monkeypatch.setattr(run_module, "persist_registrations", persist)
    monkeypatch.setattr(intake, "new_request", new_request)
    report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    _drop_thread(report.feature_request_id)
    assert order[:3] == ["seed", "persist", "build_row"], f"注册面没有先于建行：{order}"


#: EN-09 没有 `approvers` 列（白名单归调用方给，见 `data-model.md §4` 与 `repository.resolve` 的签名），
#: 所以这里给一个"两个人里先到的那个"——与 `test_approval_resume.py` 的或签场景同形。
PRINCIPAL = "owner-cli"
APPROVERS = frozenset({PRINCIPAL, "owner-second"})


def _decide(session, ticket, *, verdict: str, now: dt.datetime = NOW):
    """把 EN-09 那张单按**人批**的方式落到决定态（CLI 通道 + 白名单内的人 + CAS 用行上的现版本）。

    这里刻意不碰 `run_resume`：结论进库与结论进图是两件事，混在一起就分不出"没人批过"与"批了没恢复"。
    """
    return repo.resolve(
        session,
        ticket_id=ticket.id,
        verdict=verdict,
        version=ticket.version,
        channel=repo.CLI,
        principal=PRINCIPAL,
        approvers=APPROVERS,
        now=now,
    )


class _CountingOrchestrator(run_module.StubOrchestrator):
    """数拆单与验收映射**真被调了几次**：FR-041 那句"MUST NOT 整链路重跑"在这一层是可数的。

    为什么值得单开一个类而不是看 `waiting_after`：`waiting_after == ()` 只说"图到头了"，它挡不住
    "从头又跑了一遍再到头"。而 `Command(resume=…)` 只重跑被中断的那一个节点 ⇒ 两根计数都必须是 0。
    """

    def __init__(self) -> None:
        self.split_calls = 0
        self.link_calls = 0

    def split(self, summary_digest):
        self.split_calls += 1
        return super().split(summary_digest)

    def acceptance_links(self, summary_digest):
        self.link_calls += 1
        return super().acceptance_links(summary_digest)


def test_an_approved_ticket_resumes_the_gate_and_lets_the_cards_through(bus, session):
    """两段式的第二段真跑一遍：恢复前卡在 `approval_gate`，恢复后卡态落定、链路一节没重跑。

    四件事各挡一种会发生的错：

    1. `waiting_before == ("approval_gate",)` 且 `waiting_after == ()` ⇒ 恢复是从**那个**等待点起来的，
       图也真走到了终点（受理图 `approval_gate → END`，后面没有节点）；
    2. `resumed.conclusion == "approve"` 而命令行上没有任何人再敲过一次结论 ⇒ 它只可能来自 EN-09 的
       `state`（这条与 节 二 那条静态判据配对：那条防表被改，这条防表没被用）；
    3. 四张卡 `PENDING_APPROVAL → READY` ⇒ 门后的事实写进了 EN-02，不是只留在图 state 里；
    4. 计数器两根都是 0 ⇒ 装配点没有偷偷改成 `advance_request`。
    """
    intake_report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    counter = _CountingOrchestrator()
    try:
        ticket = session.get(ApprovalTicket, intake_report.ticket_id)
        assert (str(ticket.state), ticket.version) == ("PENDING", 0), (ticket.state, ticket.version)

        outcome = _decide(session, ticket, verdict="approve")
        assert (outcome.applied, outcome.state) == (True, "APPROVED"), (outcome.applied, outcome.state, outcome.code)

        resumed = run_module.run_resume(
            intake_report.feature_request_id,
            transport="stub",
            bus_root=bus_root_of(bus),
            session=session,
            orchestrator=counter,
        )
        assert resumed.ticket_id == ticket.id, resumed.ticket_id
        assert resumed.conclusion == gate.APPROVE, f"库里批的是 approve，交回图的却是 {resumed.conclusion!r}"
        assert resumed.waiting_before == (orchestrate_graph.APPROVAL_GATE,), resumed.waiting_before
        assert resumed.waiting_after == (), resumed.waiting_after
        intake_nodes = {
            orchestrate_graph.APPROVAL_GATE,
            orchestrate_graph.PLAN,
            orchestrate_graph.SPLIT,
            orchestrate_graph.NORMALIZE,
        }
        assert len(intake_nodes) == 4, f"受理图四个节点名撞了 ⇒ 这一读分不出卡在谁身上：{sorted(intake_nodes)}"
        assert resumed.card_statuses == tuple(sorted((card_id, "READY") for card_id in CARD_IDS)), (
            resumed.card_statuses
        )
        assert _card_statuses(session, intake_report.feature_request_id) == resumed.card_statuses, (
            "报告与 EN-02 各说一份卡态 ⇒ 恢复的读数不是从库里读的"
        )
        assert (counter.split_calls, counter.link_calls) == (0, 0), (
            f"恢复重跑了链路上的 {counter.split_calls} 次拆单 / {counter.link_calls} 次映射 ⇒ FR-041 的 MUST NOT 破了"
        )
    finally:
        _drop_thread(intake_report.feature_request_id)


def test_a_rejected_ticket_resumes_the_same_gate_to_rejected_cards(bus, session):
    """驳回那半边真跑一次：`CONCLUSION_BY_STATE` 的第二格不是摆设。

    只测 approve 的话，把 `{APPROVED: approve, REJECTED: reject}` 写成两条都返回 `approve` —— 这条判据
    就是唯一的证人（M4 的变异正是这一发，它必须红）。
    """
    intake_report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    try:
        ticket = session.get(ApprovalTicket, intake_report.ticket_id)
        outcome = _decide(session, ticket, verdict="reject")
        assert (outcome.applied, outcome.state) == (True, "REJECTED"), (outcome.applied, outcome.state, outcome.code)

        resumed = run_module.run_resume(
            intake_report.feature_request_id, transport="stub", bus_root=bus_root_of(bus), session=session
        )
        assert resumed.conclusion == gate.REJECT, f"库里驳回了，交回图的却是 {resumed.conclusion!r}"
        assert resumed.card_statuses == tuple(sorted((card_id, "REJECTED") for card_id in CARD_IDS)), (
            resumed.card_statuses
        )
    finally:
        _drop_thread(intake_report.feature_request_id)


def test_resume_on_a_still_pending_ticket_refuses_and_mutates_nothing(bus, session):
    """真数据上的"还没人批"：退 `approval_not_decisive`，且库与线程都留在原地。

    节 二 那条用假会话喂四种形状，这条把同一格放在**真单据**上过一遍：它挡的是"库里刚建单就能恢复"
    （那等于任何拿到需求单号的人都能放行）。同时证明这次拒绝没碰任何东西：单据仍是 `PENDING`、
    版本仍是 0、卡仍在卡点态、仿真总管一次都没被调。
    """
    intake_report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    counter = _CountingOrchestrator()
    try:
        with pytest.raises(run_module.RunFailure) as refused:
            run_module.run_resume(
                intake_report.feature_request_id,
                transport="stub",
                bus_root=bus_root_of(bus),
                session=session,
                orchestrator=counter,
            )
        assert refused.value.code == run_module.NOT_DECISIVE, refused.value.args[0]

        ticket = session.get(ApprovalTicket, intake_report.ticket_id)
        assert (str(ticket.state), ticket.version) == ("PENDING", 0), (ticket.state, ticket.version)
        statuses = {card_id: status for card_id, status in _card_statuses(session, intake_report.feature_request_id)}
        assert statuses == {card_id: run_module.CARD_AWAITING for card_id in CARD_IDS}, statuses
        assert (counter.split_calls, counter.link_calls) == (0, 0), "这道拒绝连图都没该建"
    finally:
        _drop_thread(intake_report.feature_request_id)


def test_a_second_resume_is_refused_because_the_thread_has_no_waiting_point(bus, session):
    """同一条单子第二次恢复：退 `no_waiting_point`，而不是把已批过的链路再推一遍。

    这一发同时钉两件事：① `_decided_ticket` 在真数据上仍然认这张 `APPROVED` 单（它不是拒绝的理由），
    真正拦住的是图上没有等待点 ⇒ 归因说的是"这条已经推过去了"而不是"没人批过"；② 拦下之后卡态
    与单据版本都没变（重复点"恢复"按钮的那个人不会把 READY 改回 PENDING_APPROVAL）。
    """
    intake_report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    try:
        ticket = session.get(ApprovalTicket, intake_report.ticket_id)
        _decide(session, ticket, verdict="approve")
        first = run_module.run_resume(
            intake_report.feature_request_id, transport="stub", bus_root=bus_root_of(bus), session=session
        )
        assert first.waiting_after == (), first.waiting_after

        counter = _CountingOrchestrator()
        with pytest.raises(run_module.RunFailure) as refused:
            run_module.run_resume(
                intake_report.feature_request_id,
                transport="stub",
                bus_root=bus_root_of(bus),
                session=session,
                orchestrator=counter,
            )
        assert refused.value.code == run_module.NO_WAITING_POINT, refused.value.args[0]
        assert (counter.split_calls, counter.link_calls) == (0, 0), "这次拒绝之前就该退，图没该建"

        after = session.get(ApprovalTicket, intake_report.ticket_id)
        assert (str(after.state), after.version) == ("APPROVED", 1), (after.state, after.version)
        statuses = {card_id: status for card_id, status in _card_statuses(session, intake_report.feature_request_id)}
        assert statuses == {card_id: "READY" for card_id in CARD_IDS}, statuses
    finally:
        _drop_thread(intake_report.feature_request_id)
