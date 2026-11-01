"""T123 装配点（第 2 步下半段第 4 项）：验收段 `run_accept()` 的判据。

**这一笔接的是哪一段**：执行段把卡推到 `ACCEPTING` 就结束了，而 `ACCEPTING` 之后那一格
（`accept.accept()` ⇒ `DELIVERED`）**今天之前一个生产调用方都没有**（现读：`src/acp` 里除本判据外 0 处）。
这个缺口不是"少一个函数"那么轻：拆单出来的 T02/T03/T04 三条卡的就绪前件是"T01 = DELIVERED"，
所以链路的第二发指派直接抛 —— 探针原文记在 `evidence/phase-12.md` §十五。验收段接上，R-07 才有四份文件。

**owner 2026-10-17 裁 Q8 = 甲**（装配点替验收岗出结论）⇒ 本文件同时钉住"甲 ≠ 造数"那三格：

① **条目集合不是编的**：条目由实体层从 EN-07 映射表读（`expected_items()`），完整性由实体层判
   （`check_items()` 双向差集）⇒ 装配点自己列条目、自己数条目，都是这一文件要拦住的红；
② **代填不静默**：仿真来源交判定后，装配点另落一行 `action=acceptance_items_proxy`，
   带来源的机读标记与逐条条目名；真实来源不声明标记 ⇒ 那一行**不写**（这一形有专门一条反向证人）；
③ **代填的是判定，不是身份**：`accept()` 那句"不是同人就不收"照旧生效 ⇒ 库里那条
   `acceptance_round` 的 actor 仍是这张卡的验收岗，而不是平台身份。

诚实性（同一句也写在 `quickstart.md §4` 与本节的台账里）：**R-07 这一发的验收结论由装配点按 EN-07
映射表逐条代填（全 `met=True`），不构成 FR-051 / FR-052 的实证。**

辅助函数是本文件自用的副本（`drop_thread` / `bus_root_of` 在两笔里各有一份）—— 这几份辅助函数每笔
各自成立，改一处不会让另一处跟着漂；口径与 `test_run_execute.py` 同，但**不共享代码**。
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
from acp.adapters.stub import StubMisuse
from acp.approval import repository as repo
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import accept as accept_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import TaskCard
from acp.persistence.models_registry import EventLog

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 17, 8, 30, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-accept"
RAW = "做个介绍公司产品的静态网站"
PROVIDER = run_module.StubAcceptanceProvider()
FINDINGS = run_module.StubFindingProvider()


# ------------------------------------------------------------------ 小工具


def func_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`run.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def real_statements(body: ast.FunctionDef) -> list[ast.stmt]:
    return [
        node
        for node in body.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    ]


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


def to_accepting(bus, session) -> tuple[uuid.UUID, str, str]:
    """真把一条链跑到**一张卡在 `ACCEPTING`**：受理 → 人批 → 恢复 → 指派 → 执行段。

    返回 `(需求单号, 卡号, 这张卡的验收岗)`。验收岗从库里读，不从指派段的返回值抄 —— 后面两条判据
    要断的就是"库里那个席位人收不收结论"。
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
    report = run_module.run_execute(
        request_id, card_id, transport="stub", bus_root=bus.path, session=session, now=NOW,
        findings_for=FINDINGS, slots=slots, leases=leases,
    )
    assert report.card_status_after == "ACCEPTING", f"执行段没收在 `ACCEPTING`，验收段的前件就假了：{report}"
    acceptor = str(session.get(TaskCard, (request_id, card_id)).acceptor_agent_id)
    return request_id, card_id, acceptor


def accept_actions(session, card_id: str) -> list[tuple[str, str]]:
    """这张卡上 `node=accept` 的留痕，`(action, actor_agent_id)`，按 `EventLog.id` 升序。"""
    rows = session.scalars(
        select(EventLog).where(EventLog.entity == "task_card", EventLog.entity_id == card_id).order_by(EventLog.id)
    ).all()
    return [
        (str((row.payload or {}).get("action")), str(row.actor_agent_id))
        for row in rows
        if (row.payload or {}).get("node") == accept_node.NODE
    ]


def proxy_row(session, card_id: str) -> dict:
    for row in session.scalars(
        select(EventLog).where(EventLog.entity == "task_card", EventLog.entity_id == card_id).order_by(EventLog.id)
    ).all():
        payload = row.payload or {}
        if payload.get("action") == run_module.ACCEPT_PROXY_ACTION:
            return payload
    return {}


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


class QuietProvider:
    """**真实来源的形状**：交回判定，但不声明 `MARK`。

    它存在的理由只有一格：让"代填留痕"这条判据有反向证人 —— 仿真来源写那一行、真实来源不写，
    那一行才真的证明"这一发是代填的"。它不判 `met=False`，因为本轮要测的是留痕的条件性，
    未达标那一支（`CHANGES_REQUESTED` / 带未达标项交付）已由 `tests/guards/test_review_accept.py` 覆盖。
    """

    def verdicts_for(self, card, items):
        return tuple(
            accept_node.ItemVerdict(acceptance_item=str(item), met=True, evidence=f"真实来源：{card.id} 的 {item}")
            for item in items
        )


# ---------------------------------------------------------------- 一、静态形状


def test_run_accept_never_writes_the_card_status_itself():
    """状态机只有一个载体：`run_accept()` 里不许出现任何对 `card.status` 的写入，也不许自己算轮次。

    钉两格：① 没有 `xxx.status = …` 形式的赋值；② `accept()` 恰好调一次，且 `verdicts=` 那个实参
    就是本函数从注入来源拿到的那份（不是现编的第二份）。把状态写回装配点，`ACCEPTING → DELIVERED`
    就有两处定义，实体那句 `state_not_accepting` 的守卫会被绕开。
    """
    body = func_body("run_accept")
    writes = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "status" for target in node.targets
        )
    ]
    assert not writes, f"装配点在写状态（状态机的第二个载体）：{writes}"
    names = called_names(body)
    assert names.count("accept") == 1, names
    assert names.count("expected_items") == 1, f"条目集合应当只从实体层取一次：{names}"
    assert names.count("record") == 1, f"代填留痕只有一处：{names}"


def test_the_verdicts_handed_to_accept_come_from_the_injected_source():
    """`accept(verdicts=…)` 收到的是**来源交回来的那份**，且来源收到的条目是实体层读出来的那份。

    这一条是 Q8 甲 的关键一格：如果装配点把来源的返回值丢掉、自己再造一份判定，
    "判据基础真实"这句就假了。所以静态钉名字链：`expected_items()` 的返回值 ⇒ 传进 `verdicts_for`
    ⇒ `verdicts_for` 的返回值 ⇒ 传进 `accept(verdicts=…)`。名字必须是同一条赋值链上的，不是同名巧合。
    """
    body = func_body("run_accept")
    statements = real_statements(body)

    def assigned_from_call(target_call: str) -> str | None:
        """找那条"把 `target_call()` 的结果接住"的赋值 —— 看整棵子树，不看最外层调用。

        第一版这里只读 `node.value.func`，于是 `verdicts = tuple(accept_for.verdicts_for(…))` 被读成
        `tuple` 而判据假红（实现是对的，判据在盯一个不存在的写法）。看子树才是"这份值来自那一步"。
        """
        for node in statements:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if target_call in called_names(node.value):
                    return node.targets[0].id
        return None

    items_name = assigned_from_call("expected_items")
    verdicts_name = assigned_from_call("verdicts_for")
    assert items_name, "没有一处赋值把 `expected_items()` 的结果接住 ⇒ 条目集合不在装配点的流动路径上"
    assert verdicts_name, "没有一处赋值把 `verdicts_for()` 的结果接住 ⇒ 来源交回来的判定没人接"
    positional = None
    for node in ast.walk(body):
        if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "verdicts_for":
            positional = node.args[1] if len(node.args) > 1 else keyword_of(node, "verdicts_for", "items")
    handed = positional
    assert isinstance(handed, ast.Name) and handed.id == items_name, (
        f"`verdicts_for` 收到的条目不是 `expected_items()` 那份（期望 {items_name}，实得 {ast.dump(handed) if handed else None}）"
    )
    accepted = keyword_of(body, "accept", "verdicts")
    assert isinstance(accepted, ast.Name) and accepted.id == verdicts_name, (
        f"`accept(verdicts=…)` 收的不是来源交回的那份（期望 {verdicts_name}，实得 "
        f"{ast.dump(accepted) if accepted else None}）⇒ 装配点在替验收岗再造判定"
    )


def test_run_accept_refuses_before_touching_the_database():
    """没有结论来源 ⇒ 在**任何碰库动作之前**停，代号 `acceptance_source_missing`。

    `session=None` 是这一发的证人：顺序写反（先读卡再检查来源）就会以 `AttributeError` 红，
    而不是读到这个 `RunFailure`。这一形不需要真库，所以它进 fast 档。
    """
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_accept(uuid.uuid4(), "T01", transport="stub", session=None, now=NOW, accept_for=None)
    assert caught.value.code == run_module.ACCEPTANCE_MISSING, caught.value.code


def test_the_transport_gate_speaks_before_the_source_check():
    """两道门各管一件事，顺序也要钉：`--transport` 没显式给 ⇒ 先撞仿真门，轮不到"缺来源"。

    反过来说：如果这一发读到 `acceptance_source_missing`，就说明有人在 transport 那一步之前先动了
    验收段的逻辑（那一步今天该做的只是"不许默认调厂商"）。
    """
    with pytest.raises(StubMisuse):
        run_module.run_accept(uuid.uuid4(), "T01", transport="", session=None, now=NOW, accept_for=None)


def test_the_stub_provider_fills_exactly_the_items_it_was_given():
    """仿真来源**不发明条目**：给它几条就交几条，顺序一致，每条 `met=True` 且证据串带着仿真身份。

    这一发同时钉住诚实声明的两个半边：`MARK` 前缀（机读）+ "不是 FR-051 / FR-052 的实证"（人读）。
    少了后半句，查 EN-14 的人只看得到"代填过"，看不出"代填不构成实证"。
    """
    card = SimpleNamespace(id="T01")
    items = ("产物 index.html 存在且非空", "首页 的正文可读")
    verdicts = tuple(PROVIDER.verdicts_for(card, items))
    assert tuple(verdict.acceptance_item for verdict in verdicts) == items, verdicts
    assert all(verdict.met is True for verdict in verdicts), verdicts
    for verdict in verdicts:
        assert verdict.evidence.startswith(PROVIDER.MARK), verdict.evidence
        assert "不是 FR-051 / FR-052 的实证" in verdict.evidence, verdict.evidence


def test_the_assembly_point_never_constructs_an_acceptance_verdict():
    """`run.py` 里构造 `ItemVerdict` 的地方**只许有一处**，而且在 `StubAcceptanceProvider` 内。

    这是"甲 ≠ 造数"的实现面证人：判定只能由被点名的那一支来源交，装配点别处再构造一份，
    就等于"没有来源也照样能过验收" —— `ACCEPTANCE_MISSING` 那道门会被自己绕开。
    与 `test_run_execute.py` 里 `ReviewFinding` 那条同规（那条也承认 `StubFindingProvider` 是唯一构造点）。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "ItemVerdict"]
    assert len(sites) == 1, f"`ItemVerdict` 的构造点应当只有一处，实得 {len(sites)}"
    in_provider = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StubAcceptanceProvider":
            in_provider = any(child is sites[0] for child in ast.walk(node))
    assert in_provider, "唯一的 `ItemVerdict` 构造点不在 `StubAcceptanceProvider` 里 ⇒ 装配点在别处代填"
    body = func_body("run_accept")
    assert not any(child is sites[0] for child in ast.walk(body)), "`run_accept()` 自己构造判定"


# ------------------------------------------------------------------ 二、真 PG


def test_the_chain_now_reaches_delivered_and_names_the_acceptor(bus, session):
    """整链收到 `DELIVERED`，而"这一轮判了几条、过几条"落在**实体自己那条**留痕上，actor 是验收岗。

    五格各挡一种错：
    ① 卡态与轮次从**库里**读回（`DELIVERED` / `review_round == 1`），不读 `AcceptanceOutcome`；
    ② `acceptance_round` 那行的 actor == 卡上的验收岗 ⇒ 代填的是判定，不是身份（换成平台身份的话，
       `accept()` 那句"不是同人就不收"本该红，所以这一格同时证明身份没有被代掉）；
    ③ 代填那行的 actor 是平台自动化身份（**是它在代填**，与 ② 分得开才叫两条事实）；
    ④ `met=2 / unmet=0`、条目两条都来自 EN-07（映射表两条 = 这张卡的两条判据）；
    ⑤ 留痕顺序是 `acceptance_items_proxy` 在前、`acceptance_round` 在后：代填发生在结论被收下之前，
       不是在结论落下之后补一句"我代填了"。
    """
    request_id, card_id, acceptor = to_accepting(bus, session)
    try:
        report = run_module.run_accept(
            request_id, card_id, transport="stub", session=session, now=NOW, accept_for=PROVIDER
        )
        row = session.get(TaskCard, (request_id, card_id))
        assert (str(row.status), int(row.review_round)) == (accept_node.DELIVERED, 1), (row.status, row.review_round)
        assert report.card_status_after == accept_node.DELIVERED, report
        actions = accept_actions(session, card_id)
        assert [action for action, _actor in actions] == [
            run_module.ACCEPT_PROXY_ACTION,
            "acceptance_round",
        ], actions
        round_actor = [actor for action, actor in actions if action == "acceptance_round"][0]
        proxy_actor = [actor for action, actor in actions if action == run_module.ACCEPT_PROXY_ACTION][0]
        assert round_actor == acceptor, f"结论的 actor 不是卡上的验收岗（{round_actor} / {acceptor}）⇒ 身份被代填了"
        assert proxy_actor == run_module.ACTOR_AGENT_ID, f"代留痕的 actor 应当是平台身份：{proxy_actor}"
        payload = proxy_row(session, card_id)
        assert payload["provider_mark"] == PROVIDER.MARK, payload
        assert tuple(payload["items"]) == report.items and len(report.items) == 2, (payload, report)
        assert (report.met, report.unmet, report.round_no) == (2, 0, 1), report
        assert report.acceptor_agent_id == acceptor, report
    finally:
        drop_thread(session, request_id)


def test_a_source_that_declares_no_mark_leaves_no_proxy_row(bus, session):
    """反向证人：来源不声明 `MARK` ⇒ 库里那行"代填"一个字都不该出现（owner 裁 Q8 的补条）。

    少了这一发，`acceptance_items_proxy` 那条就可能是"每发都写"的恒常噪音 —— 那它就再也不证明
    "这一发的结论是代填的"这件事了。这里用一个不留标记的来源（真实来源的形状），
    卡照样收到 `DELIVERED`，但验收段只剩实体自己那一行。
    """
    request_id, card_id, _acceptor = to_accepting(bus, session)
    try:
        report = run_module.run_accept(
            request_id, card_id, transport="stub", session=session, now=NOW, accept_for=QuietProvider()
        )
        assert report.card_status_after == accept_node.DELIVERED, report
        assert report.proxy_mark == "", report
        assert [action for action, _actor in accept_actions(session, card_id)] == ["acceptance_round"], (
            accept_actions(session, card_id)
        )
    finally:
        drop_thread(session, request_id)


def test_acceptance_without_a_source_writes_nothing(bus, session):
    """缺来源那一发**不收任何东西**：卡还在 `ACCEPTING`、轮次没涨、验收段一行留痕都没有。

    这一发与静态那条（`test_run_accept_refuses_before_touching_the_database`）是两格：静态那条证
    "顺序在碰库之前"，这一发证"跑在真链上时同样一行都没多"。写反顺序的表现是轮次或状态先动了，
    那时这张卡既进不了 `DELIVERED` 也退不回 `ACCEPTING`，谁也接不上。
    """
    request_id, card_id, _acceptor = to_accepting(bus, session)
    try:
        before = (str(session.get(TaskCard, (request_id, card_id)).status),
                  int(session.get(TaskCard, (request_id, card_id)).review_round))
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_accept(request_id, card_id, transport="stub", session=session, now=NOW, accept_for=None)
        assert caught.value.code == run_module.ACCEPTANCE_MISSING, caught.value.code
        row = session.get(TaskCard, (request_id, card_id))
        assert (str(row.status), int(row.review_round)) == before == (accept_node.ACCEPTING, 0), (row.status, row.review_round)
        assert accept_actions(session, card_id) == [], accept_actions(session, card_id)
    finally:
        drop_thread(session, request_id)
