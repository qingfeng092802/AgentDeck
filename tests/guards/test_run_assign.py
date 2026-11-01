"""T123 装配点（第 2 步下半段第 2 项）：指派段 `run_assign()` 的判据。

**这一笔接的是哪一段**：`--resume` 把卡推到 `READY` 之后，一张卡要经过
`SelectWorker → AssignGuards → Dispatch` 才有人真的开工。这一段今天**没有命令行入口**（理由写在
`run.py::run_assign` 的模块头：单跑它会把卡留在 `RUNNING` 而台账里那张凭据随进程消失），
所以这里的判据钉的是装配函数本体的形状与读数来源，命令行那一面归第 3 项同笔落。

三节：

- 节 一 = 静态形状（不连库）：这一笔**没有把 `dispatch()` 变成第二个生产调用点**、
  瞬态层的上限从 EN-03 读而不是在装配点写死、门在会话之前说话。
  （整个 src 的 `dispatch()` 调用点计数已由 `test_dispatch_edge.py:234` 钉住，这里只补 run.py 这一侧，
  同一个清单不留两处。）
- 节 二 = 拒绝面（不连库）：四种"这一段跑不起来"各说哪一个代号，且一次会话都没打开。
- 节 三 = 真 PG：三岗填上且两两不同、只有过了就绪判定的那张卡动、被挡的每一张都点名、
  台账与瞬态层的占用对得上、第二次跑不再派发一张槽。**缺 `ACP_DB_DSN` 时只有这一节 skip**。

反向对照写在变异账里（`evidence/phase-12.md` §十一）：纯改文档措辞与纯改循环写法都不许红。
"""

from __future__ import annotations

import ast
import datetime as dt
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
from acp.approval import repository as repo
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import AgentUnit, TaskCard
from acp.review import policy as review_policy

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 16, 13, 0, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-assign"
RAW = "做一个介绍公司产品的静态网站，配色你看着办"
READYABLE = ("T01", "T02", "T03", "T04")


# ------------------------------------------------------------------ 小工具


def func_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`run.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def called_names(func: ast.AST) -> list[str]:
    """函数体内所有调用的名字（裸名与 `a.b()` 的末段都算）。"""
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name:
                out.append(name)
    return out


def keyword_value(kind: ast.AST, keyword: str):
    """找到 `Kind(**{keyword: 值})` 那一个实参节点。"""
    for node in ast.walk(kind):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "InProcessSlotManager":
                for kw in node.keywords:
                    if kw.arg == keyword:
                        return kw.value
    return None


def fake_card(card_id: str, status: str, depends_on: tuple[str, ...], executor: str | None = None):
    """按 EN-02 的**读法**造一张卡：`need_of()` 只碰这四列。"""
    return SimpleNamespace(id=card_id, status=status, depends_on=list(depends_on), executor_agent_id=executor)


class _FakeCards:
    """最小假会话：`query(TaskCard).filter(...).all()` 与 `get(FeatureRequest, …)`。"""

    def __init__(self, rows: list, request: object = object()) -> None:
        self._rows = rows
        self._request = request
        self.gets: list[str] = []

    def get(self, model, key):
        self.gets.append(getattr(model, "__name__", str(model)))
        return self._request

    def query(self, model):
        assert model is TaskCard, f"这道读数读的不是 EN-02：{model}"
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        # `cards_of()` 会排序（判据要可复现），假会话照一条链把它接下来：排序本身不是被测面。
        return self

    def all(self):
        return list(self._rows)


# ---------------------------------------------------------------- 一、静态形状


def test_run_assign_goes_through_the_graph_and_never_calls_dispatch_itself():
    """装配点进派发只有一条路：`build_assignment_graph` 一处，`dispatch(` 零处。

    owner 对本项那条约束的原文是"dispatch() 只能有一个生产调用点"。整个 src 的计数已由
    `test_dispatch_edge.py:234` 钉住（那一处 = 图上的 `DISPATCH` 节点）；这条补的是**本笔新引入的风险**：
    装配点最顺手的写法就是"循环里直接调 `dispatch_node.dispatch(...)`"，那会造出第二份就绪判定
    （原则 I 违例），而这条判据在 AST 上当场红。计数用 AST 不用 grep，因为 grep 会把
    `def dispatch(` 与文档串里的词一起算进来。
    """
    body = func_body("run_assign")
    calls = called_names(body)
    assert calls.count("build_assignment_graph") == 1, f"指派图该且只该建一次：{calls.count('build_assignment_graph')}"
    assert "dispatch" not in calls, "装配点自己调了 dispatch() ⇒ src 里出现第二份就绪判定"


def test_the_slot_layer_is_assembled_from_en03_not_from_a_literal_in_the_code():
    """瞬态层的每工具上限**来自 EN-03 现值**，平台上限来自 `config/concurrency.yaml`。

    钉的是实参的**类型**：`per_agent_max_parallel=` 后面必须是一个变量（从库里读出来的那份），
    写字面量字典就红 —— 因为"注册面改了额度而装配点还按老数占槽"是这条链上最安静的一种错。
    另两格：`load_platform_limit()` 必须被调（那个数按引用型参数条款不许下沉成代码常量），
    以及读数必须过 `enabled`（停用的工具不该有额度可占）。
    """
    body = func_body("_slots_from_registry")
    value = keyword_value(body, "per_agent_max_parallel")
    assert isinstance(value, ast.Name), f"上限实参必须是读出来的变量，收到的是 {type(value).__name__}"
    assert "load_platform_limit" in called_names(body), "平台上限没从 concurrency.yaml 读 ⇒ 它成了第二处常量"
    assert "AgentUnit" in ast.dump(body), "这道读数不读 EN-03 ⇒ 装配面与注册面脱钩"
    assert "enabled" in ast.dump(body), "没过滤 `enabled` ⇒ 停用的工具也会被占槽"


def test_the_gate_speaks_before_the_session_is_touched_in_run_assign():
    """`run_assign()` 里门 MUST 早于第一次会话使用 —— 与受理段、恢复段同一条纪律（裁决 1 那档退出码）。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = func_body("run_assign")
    gate_lines = sorted(node.lineno for node in ast.walk(body) if isinstance(node, ast.Call) and
                        (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "resolve_transport_flag")
    assert gate_lines, "`run_assign()` 里没有 `resolve_transport_flag()` 调用 ⇒ 这条判据会退化成恒真"
    session_lines = [node.lineno for node in ast.walk(body) if isinstance(node, ast.Call)
                     and getattr(node.func, "attr", None) == "get"]
    assert session_lines and gate_lines[0] < min(session_lines), (
        f"行序读成 门{gate_lines[0]} / 会话{min(session_lines)} ⇒ 打错旗标的人会被归因成配置问题"
    )


# ---------------------------------------------------------------- 二、拒绝面


def test_run_assign_refuses_a_bad_transport_before_it_touches_the_session():
    """`--transport` 打错 ⇒ `StubMisuse(transport_flag_unknown)`，且会话一次属性访问都没发生。"""
    calls: list[str] = []

    class _Counting:
        def get(self, *args, **kwargs):
            calls.append("get")
            raise AssertionError("门还没说话就查了库")

    with pytest.raises(run_module.StubMisuse) as caught:
        run_module.run_assign(uuid.uuid4(), transport="qoder", session=_Counting(), now=NOW)
    assert caught.value.code == "transport_flag_unknown" and calls == [], (caught.value.code, calls)


def test_a_missing_request_row_is_refused_before_the_card_face_is_read():
    session = _FakeCards(rows=[fake_card("T01", "READY", ())], request=None)
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_assign(uuid.uuid4(), transport="stub", session=session, now=NOW)
    assert caught.value.code == run_module.REQUEST_MISSING, caught.value.args[0]


def test_a_request_without_cards_is_refused_by_the_same_name_the_resume_side_uses():
    """没有卡 ⇒ `cards_missing`：恢复段与指派段说的是同一件事（EN-02 空），不该有两个代号。"""
    session = _FakeCards(rows=[])
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_assign(uuid.uuid4(), transport="stub", session=session, now=NOW)
    assert caught.value.code == run_module.CARDS_MISSING, caught.value.args[0]


def test_no_dispatchable_card_at_all_is_refused_and_names_the_request():
    """四张卡全在卡点态 ⇒ `nothing_dispatchable`，正文要点名是哪一条需求单。

    这一格与下面那一格是**两种不同的"跑不动"**：这里 `readiness()` 的 ready 与 blocked 都为空
    （不在可派发态的卡根本不进分类），所以归因只能是"这条单子还没到派发的时候"。
    合成一格就会把"还没批完"与"批完了但依赖没齐"说成同一句话，人接着查的方向正好相反。
    """
    request_id = uuid.uuid4()
    waiting = [fake_card(card_id, run_module.CARD_AWAITING, ()) for card_id in READYABLE]
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_assign(request_id, transport="stub", session=_FakeCards(rows=waiting), now=NOW)
    assert caught.value.code == run_module.NOTHING_DISPATCHABLE, caught.value.args[0]
    assert str(request_id) in caught.value.args[0], f"拒绝正文没说是哪条需求单：{caught.value.args[0]}"


def test_a_ready_card_blocked_by_an_unmet_dependency_is_named_with_its_blocker():
    """`READY` 卡被未交付的依赖挡住 ⇒ 代号相同，但正文必须带出"哪张卡、被谁、以什么态挡着"。

    这是 FR-025 那句"不静默丢弃"在装配面上的形态。断言只认三个**读数**（被挡卡号、依赖卡号、
    依赖那一行的态），不认措辞 ⇒ 改文案不红，把 `blocked` 丢掉才红（M5 注的就是这一发）。
    """
    request_id = uuid.uuid4()
    cards = [
        fake_card("T01", "REJECTED", ()),
        fake_card("T02", "READY", ("T01",), executor="qoder"),
    ]
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_assign(request_id, transport="stub", session=_FakeCards(rows=cards), now=NOW)
    message = caught.value.args[0]
    assert caught.value.code == run_module.NOTHING_DISPATCHABLE, message
    assert "T02" in message and "T01" in message and "REJECTED" in message, message


# ---------------------------------------------------------------- 三、真 PG


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
    """`<根>/<项目>` 得是 git 顶层（T109 之后 `_normalize` 要真打一拍）—— 同上一笔踩过的那格。"""
    project = bus.path / PROJECT
    project.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "."], ["config", "user.email", "probe@invalid"], ["config", "user.name", "probe"]):
        subprocess.run(
            ["git", *args], cwd=str(project), check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    return bus.path


def to_ready(bus, session) -> uuid.UUID:
    """把一条需求单真推到**四张卡 READY**：受理 → CLI 通道人批 → 恢复。返回需求单号。

    走完整条前段而不是手搓四张卡：指派段的输入形状（哪些卡 READY、依赖怎么挂）本身就是被恢复
    那一步决定的，手搓会把"恢复真的把四张卡都放行"这件事从被测面里挪走。
    """
    report = run_module.run_intake(
        RAW, project_key=PROJECT, transport="stub", bus_root=bus_root_of(bus), session=session, now=NOW
    )
    ticket = session.get(ApprovalTicket, report.ticket_id)
    outcome = repo.resolve(
        session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
        principal="owner-assign", approvers=frozenset({"owner-assign"}), now=NOW,
    )
    assert (outcome.applied, outcome.state) == (True, "APPROVED"), outcome.code
    resumed = run_module.run_resume(
        report.feature_request_id, transport="stub", bus_root=bus_root_of(bus), session=session
    )
    assert resumed.waiting_after == (), resumed.waiting_after
    assert {card_id: status for card_id, status in resumed.card_statuses} == {card_id: "READY" for card_id in READYABLE}, (
        resumed.card_statuses
    )
    # thread 由调用方在用完后清（清早了会让"图上还有等待点"这类读数变得不可解释）。
    return report.feature_request_id


def drop_thread(session, feature_request_id: uuid.UUID) -> None:
    thread = cp.thread_id_for(kind="A_SPLIT", feature_request_id=feature_request_id)
    with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
        chaos.kill_graph_state(conn, thread)


def test_the_three_seats_get_filled_and_are_pairwise_different(bus, session):
    """指派段的正读：一张卡被派发 ⇒ 三岗都填上且两两不同，卡进 `RUNNING`，台账与瞬态层各看得见它。

    四件事各挡一种会发生的错：
    ① 三岗两两不同（FR-031）且都不是空串/`None` —— 断言写在**库里读回的三列**上，不是图上那份；
    ② 恰好派发了 1 张：四张卡里只有 T01 过就绪判定（其余三张依赖它），多派发一张就是占了两格槽；
    ③ `held` 与 `usage` 的关系式：台账里有几张，瞬态层的占用求和就是几 —— 两面必须同时非零/归零；
    ④ 被占的那一格记在**执行者那支工具**名下（`usage` 里唯一非零的键 == 库里的 `executor_agent_id`）。
    """
    request_id = to_ready(bus, session)
    try:
        report = run_module.run_assign(request_id, transport="stub", session=session, now=NOW)
        assert len(report.dispatched) == 1, report.dispatched
        card_id, executor, reviewer, acceptor = report.dispatched[0]
        assert card_id == "T01", report.dispatched
        assert "" not in (executor, reviewer, acceptor) and "None" not in (executor, reviewer, acceptor), (
            report.dispatched
        )
        assert len({executor, reviewer, acceptor}) == 3, f"三岗两两不同破了：{report.dispatched[0]}"
        assert dict(report.statuses) == {"T01": dispatch_node.STARTED, "T02": "READY", "T03": "READY",
                                        "T04": "READY"}, report.statuses
        assert report.held == (card_id,), report.held
        busy = {agent: used for agent, used in report.usage if used}
        assert sum(used for _, used in report.usage) == len(report.held), (report.usage, report.held)
        assert set(busy) == {executor}, f"占槽记在别的工具名下：占用 {busy}，执行者 {executor}"
    finally:
        drop_thread(session, request_id)


def test_every_blocked_card_is_named_with_the_dependency_that_blocks_it(bus, session):
    """三张被挡的卡 MUST 逐张点名并带上"被谁挡着"（FR-025 的不静默丢弃在装配面上的形态）。"""
    request_id = to_ready(bus, session)
    try:
        report = run_module.run_assign(request_id, transport="stub", session=session, now=NOW)
        assert sorted(card_id for card_id, _reason in report.blocked) == ["T02", "T03", "T04"], report.blocked
        for card_id, reason in report.blocked:
            assert "T01" in reason, f"{card_id} 的阻塞原因没点名那条依赖：{reason}"
    finally:
        drop_thread(session, request_id)


def test_a_second_run_assign_borrows_no_second_slot(bus, session):
    """同一条需求单再跑一次指派段：退 `nothing_dispatchable`，且**没有第二格槽被占**。

    这一发是"重复点提交"的看守。第一跑之后唯一的 READY 卡已经 `RUNNING`（不在可派发态里），
    所以 `ready` 为空 ⇒ 走拒绝面。断言盯三处：代号、台账里仍只有一张、瞬态层求和仍为 1。
    """
    request_id = to_ready(bus, session)
    slots = run_module._slots_from_registry(session)
    ledger = scheduler.LeaseLedger()
    try:
        first = run_module.run_assign(
            request_id, transport="stub", session=session, now=NOW, slots=slots, leases=ledger
        )
        with pytest.raises(run_module.RunFailure) as refused:
            run_module.run_assign(request_id, transport="stub", session=session, now=NOW, slots=slots, leases=ledger)
        assert refused.value.code == run_module.NOTHING_DISPATCHABLE, refused.value.args[0]
        assert ledger.pending() == first.held == ("T01",), (ledger.pending(), first.held)
        assert sum(slots.usage().values()) == 1, slots.usage()
    finally:
        drop_thread(session, request_id)


def test_the_slot_caps_follow_en03_including_the_disabled_case(bus, session):
    """瞬态层的键 = EN-03 里 `enabled` 的**执行**工具；审查专用身份不占槽；全停用就退 `agents_not_registered`。

    这条是"上限从库里读"那条静态判据的**行为**半边：静态那条看实参类型，这条看换个值真的跟着变。
    `deepseek-reviewer` 在 EN-03 里也是 enabled，但它没有执行适配器、永不参与派发 ⇒
    不属于槽表（`run._slots_from_registry` 的筛选口径 = `review.policy.REVIEW_ONLY_AGENT_IDS`）。
    """
    request_id = to_ready(bus, session)
    try:
        enabled = {
            str(agent_id) for (agent_id,) in session.execute(select(AgentUnit.agent_id).where(AgentUnit.enabled.is_(True)))
        }
        review_only = enabled & review_policy.REVIEW_ONLY_AGENT_IDS
        assert review_only, "前置就不成立：审查专用身份没在册，这条判据分不出'排除'与'恰好没有'"
        registered = enabled - review_only
        assert registered, "前置就不成立：EN-03 里没有可执行的工具，这条判据会退化成恒真"
        slots = run_module._slots_from_registry(session)
        assert set(slots.usage()) == registered, (slots.usage(), registered)
        assert review_only & set(slots.usage()) == set(), slots.usage()

        victim = sorted(registered)[0]
        row = session.get(AgentUnit, victim)
        row.enabled = False
        session.flush()
        after = run_module._slots_from_registry(session)
        assert set(after.usage()) == registered - {victim}, (after.usage(), victim)

        for agent_id in sorted(registered):
            session.get(AgentUnit, agent_id).enabled = False
        session.flush()
        with pytest.raises(run_module.RunFailure) as refused:
            run_module._slots_from_registry(session)
        assert refused.value.code == run_module.AGENTS_MISSING, refused.value.args[0]
    finally:
        drop_thread(session, request_id)
