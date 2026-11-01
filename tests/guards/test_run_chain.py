"""Q10（owner 2026-10-17 裁甲）：每轮派发上限 `max_cards_per_round` 与整链驱动 `run_chain()` 的判据。

**这一笔修的是什么**：四张卡同时 ready、三工具都把这四张排到 `qoder` 第一，而 `qoder` 的
`max_parallel` 是 1。指派段原来的循环是"把 ready 的一次推完"，第二张起要占的是**同一格还没还回来的槽** ——
`InProcessSlotManager.acquire()` 走进 `condition.wait()`（`slot_manager.py:129-135`），等的是同一个进程里
永远不会有人还的那格（还槽在执行段的 `finally`，而执行段要等指派段返回才走得到）。2026-10-17 那一发
`run_assign` 就是这么挂住的，`pg_stat_activity` 里没有等待事件 —— 挂在线程条件上，不是库上。

owner 的三条约束逐条对应到判据：

① 参数名与语义（`max_cards_per_round` + 那句 docstring 原文）⇒ `test_the_parameter_name_and_the_ruling_text_are_exact`。
② 生产驱动 MUST 显式传值、`None` 只给测试 ⇒ `test_the_production_driver_passes_a_real_number_not_none`
   （驱动本体是 `run_chain()`，本轮新加，见台账的"形状选择"一段）。
③ "死锁复现"反向判据 ⇒ 两发合起来钉：
   `test_a_second_acquire_on_a_saturated_agent_waits`（真槽层：占满之后第二次 `acquire` 不返回，
   还掉之后它返回 ⇒ 挡住的是容量，不是别的东西）+
   `test_the_chain_dispatches_all_four_cards_one_per_round`（真 PG：同一份现场传 1 就能一轮一张跑完四张）。
   **取证偏差照实登记**：owner 原话是"不传参数（或传 4）⇒ timeout 挂死"。让判据真的挂在那一发上需要
   跨线程共用一个 `Session`（SQLAlchemy 不保证安全）或跨连接看未提交行（看不见），两种都要么不稳要么
   会留脏连接。所以挂死那一格拆成"槽层真的等" + "整链传 1 真的跑得完"两半，中间那座桥由
   `test_the_round_slice_takes_everything_when_unset` 钉死：不传上限就是"把 ready 的一次全喂进循环"，
   而循环里的第二次占槽就是上面那一发真的在等的东西。

本文件还钉一条与本轮形状有关的：驱动**不自己判状态机** —— 什么时候停轮只认实体那句
`nothing_dispatchable`，装配点不再写第二份"这张卡现在能不能派"。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import subprocess
import threading
import uuid
from contextlib import closing
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp import chaos
from acp import recover
from acp import run as run_module
from acp.adapters.stub import StubMisuse
from acp.approval import repository as repo
from acp.bus import layout
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import assemble as assemble_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import Artifact, TaskCard
from acp.persistence.models_registry import Deliverable
from acp.runtime.slot_manager import InProcessSlotManager

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 17, 11, 20, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-chain"
RAW = "做个介绍公司产品的静态网站"
FINDINGS = run_module.StubFindingProvider()
ACCEPT = run_module.StubAcceptanceProvider()
#: owner 2026-10-17 给的那句 docstring 原文（约束 1 要求"docstring 必须写"这句）。
RULING_TEXT = "每轮最多尝试派发几张卡；容量满时下一轮再试，不在本轮阻塞"


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


def keyword_value(owner: ast.AST, kind: str, keyword: str):
    for node in ast.walk(owner):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == kind:
                for kw in node.keywords:
                    if kw.arg == keyword:
                        return kw.value
    return None


def need(card_id: str) -> SimpleNamespace:
    """`dispatch.CardNeed` 的假面 —— `_round_slice` 只按位置切，读不到卡上任何一列。"""
    return SimpleNamespace(card_id=card_id)


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
    for kind in ("A_SPLIT", "B_ACCEPTANCE"):
        thread = cp.thread_id_for(kind=kind, feature_request_id=feature_request_id)
        with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
            chaos.kill_graph_state(conn, thread)


def to_resumed(bus, session) -> uuid.UUID:
    """跑到"人批 + 恢复已完成"那一形：四张卡在库里，只有 T01 是 ready。"""
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
    return request_id


def make_all_ready(session, request_id: uuid.UUID) -> tuple[str, ...]:
    """把四张卡的依赖清空并置 `READY` —— Q10 那一形（**四张同时 ready**）是造出来的现场。

    为什么必须造：内置拆单是三张页面都依赖那张样式卡，正常链上永远只有一张 ready，撞不到那一形。
    直接写 `card.status` 在本仓是有先例的（状态机列的写者是被绕开的测试夹具），绕开的代价在这一发里
    只有一件事：这一发**不**证明状态机迁移合法，它只证明"四张 ready 时指派段的占槽行为"。
    """
    cards = list(session.scalars(select(TaskCard).where(TaskCard.feature_request_id == request_id)
                                .order_by(TaskCard.id)).all())
    for card in cards:
        card.depends_on = []
        card.status = "READY"
    session.flush()
    return tuple(str(card.id) for card in cards)


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


# ---------------------------------------------------------------- 一、不连库


def test_the_round_slice_takes_everything_when_unset_and_refuses_a_zero_round():
    """`_round_slice` 的三形：不给上限 ⇒ 全部；给 1 ⇒ 只第一张；给 0 / 负 ⇒ 拒，不是"这轮不干活"。

    0 那一格单独值得一条断言：`ready[:0]` 是空列表，实现上"什么都不派发"是**能跑通**的，
    而它的表现是四张 ready 卡原地消失 —— 那是 FR-025 禁的静默丢弃，所以它必须是拒绝而不是空转。
    """
    four = [need("T01"), need("T02"), need("T03"), need("T04")]
    assert [item.card_id for item in run_module._round_slice(four, None)] == ["T01", "T02", "T03", "T04"]
    assert [item.card_id for item in run_module._round_slice(four, 1)] == ["T01"]
    assert [item.card_id for item in run_module._round_slice(four, 2)] == ["T01", "T02"]
    for bad in (0, -1):
        with pytest.raises(run_module.RunFailure) as caught:
            run_module._round_slice(four, bad)
        assert caught.value.code == run_module.ROUND_LIMIT_INVALID, bad


def test_a_second_acquire_on_a_saturated_agent_waits_and_comes_back_after_release():
    """Q10 的机理证人（真槽层，不连库）：容量满时第二次 `acquire` **不返回**，还掉之后它才返回。

    这一发是 owner 约束 3 前半句"timeout 挂死"能稳定钉住的那半：拿真的 `InProcessSlotManager`，
    把 `qoder` 的唯一额度占住，再要第二格 —— 看门狗线程 1 秒后仍在等，就是真的在等；
    主线程退槽之后它必须完成（否则这一发分不清"等容量"与"死锁在别处"）。
    1 秒不是随机数：`acquire` 在该形下**永远不会自己返回**，所以任何有限超时都是同一句证词。
    """
    slots = InProcessSlotManager(per_agent_max_parallel={"qoder": 1, "doubao": 4, "workbuddy": 2},
                                 platform_limit=3)
    held = slots.acquire("T01", "qoder")
    done: list[str] = []

    def worker() -> None:
        second = slots.acquire("T02", "qoder")
        done.append(second.agent_id)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(1.0)
    assert thread.is_alive(), "第二次占槽竟然返回了 ⇒ `acquire` 不再是'容量满就等'，本轮的归因要整段重写"
    slots.release(held)
    thread.join(10.0)
    assert not thread.is_alive(), "退了那一格还回不来 ⇒ 卡住的不是容量"
    assert done == ["qoder"], done


def test_the_parameter_name_and_the_ruling_text_are_exact():
    """约束 1：名字必须是 `max_cards_per_round`，而且 docstring 里必须有 owner 那句原文。

    名字与那句原文都是 owner 给的字面 ⇒ 判据按字面钉。语义漂了没人报错的形状是"改名叫
    `round_batch_size` 而 docstring 还写着旧语义"（或反过来），下一轮读代码的人会照名字去用。
    """
    fn = func_body("run_assign")
    names = [arg.arg for arg in fn.args.kwonlyargs]
    assert "max_cards_per_round" in names, names
    text = ast.get_docstring(fn) or ""
    assert RULING_TEXT in text, "owner 那句语义原文不在 `run_assign` 的 docstring 里"
    helper = ast.get_docstring(func_body("_round_slice")) or ""
    assert "不限" in helper and "拒" in helper, helper


def test_the_production_driver_passes_a_real_number_not_none():
    """约束 2：`run.py` 的生产驱动调 `run_assign` 时 MUST 传一个**非 None 的整数**。

    `None` 留在签名上是给测试与向后兼容的；生产那一路一旦漏传，形状就回到"一轮把 ready 全喂进去"，
    也就是那一发挂死的形状。所以这里断的是字面量本身：必须是 `Constant(int)`，不接受变量、
    不接受 `None`、也不接受"看起来像 1 的表达式"。
    同时反向钉一句：驱动里那次调用不许省略这个关键字（省略 = 用默认 = None）。
    """
    driver = func_body("run_chain")
    handed = keyword_value(driver, "run_assign", "max_cards_per_round")
    assert handed is not None, "`run_chain` 调 `run_assign` 时没给 `max_cards_per_round` ⇒ 生产驱动用了默认值"
    assert isinstance(handed, ast.Constant) and isinstance(handed.value, int) and handed.value > 0, handed
    assert "_round_slice" in called_names(func_body("run_assign")), "指派段没走那份切片 ⇒ 上限是装饰"


def test_the_driver_does_not_decide_the_state_machine_itself():
    """停轮只认实体那句 `nothing_dispatchable`，装配点不再写第二份"这张卡能不能派"。

    挡的错很具体：驱动最容易顺手长成 `while any(card.status == "READY" ...)` —— 那就是把
    `dispatch.readiness()` 的判定在装配点重抄一遍，两处一旦漂（比如忘了 `HANDED_OFF` / `REWORK`
    也是可派发态），驱动会在还有一张可派的卡时停下，读数看起来却是"链跑完了"。
    """
    body = func_body("run_chain")
    names = called_names(body)
    assert "run_assign" in names and "readiness" not in names and "cards_of" not in names, names
    text = ast.dump(body)
    assert "'READY'" not in text and '"READY"' not in text, "驱动里出现了自判的可派发态"
    source = SRC.read_text(encoding="utf-8")
    start = source.index("def run_chain(")
    assert "NOTHING_DISPATCHABLE" in source[start:start + 4000], "停轮没有认实体那句代号"


def _execution(**over) -> run_module.ExecutionReport:
    """`ExecutionReport` 的**生产形态**假实例（用真 dataclass 构造器，不用 `SimpleNamespace`）。

    为什么不用假面：`_activity_of()` 读的是这格的**字段名**，假面会把字段名当成自由字典键混过去，
    真构造器则在名字漂的那一刻直接 `TypeError`。
    """
    base = dict(
        feature_request_id=uuid.uuid4(),
        card_id="T01",
        agent_id="qoder",
        validated_status="valid",
        artifact_count=1,
        card_status_after=accept_node.ACCEPTING,
        verdict_rows=1,
        verdicts=("pass",),
        handoff_count_after=0,
        seats_after=("qoder", "workbuddy", "doubao"),
        usage_after=(("qoder", 0),),
        pending_after=(),
    )
    base.update(over)
    return run_module.ExecutionReport(**base)


def _acceptance(**over) -> run_module.AcceptanceReport:
    base = dict(
        feature_request_id=uuid.uuid4(),
        card_id="T01",
        acceptor_agent_id="doubao",
        items=("有入口页",),
        round_no=1,
        met=2,
        unmet=0,
        limit=3,
        card_status_after=accept_node.DELIVERED,
        proxy_mark=run_module.StubAcceptanceProvider.MARK,
    )
    base.update(over)
    return run_module.AcceptanceReport(**base)


def test_the_activity_line_answers_who_and_what_and_only_adds_the_verdict_cells_when_it_ran():
    """活动流那一行的两格内容：`actor` = 执行岗，摘要里有三岗与计数，`met=` 只在真的过了验收时出现。

    五格各挡一种错：
    ① `actor` 取 `seats_after[0]` 而不是编排身份 ⇒ FR-052 那句"执行者"要有读数（"单点代笔"这件事
       已由 `acl.RULES` 表达，不需要每行再署一次编排器的名）；
    ② 两种形状（过了验收 / 门禁没过）共用语义相同的前缀格，不会出现一侧少一列；
    ③ `met=` / `round=` 那一组**只**在 `accepted` 非空时出现 —— 挡的错是"卡停在 `ESCALATED`，
       活动流里却写着 `met=0`，读起来像验收岗说过话"；
    ④ 摘要里不出现 `|`：契约 §4 那行是五列，摘要自己带分隔符就把列数撑成六列（读侧会静默错位）；
    ⑤ 没有厂商自由文本：摘要只由 id、状态名与计数拼成（宪法「二」与契约 §5 的红线）。
    """
    actor, bare = run_module._activity_of(_execution(), None)
    assert actor == "qoder", actor
    for cell in ("task=T01", "status=ACCEPTING", "executor=qoder", "reviewer=workbuddy", "acceptor=doubao",
                 "verdict_rows=1", "artifacts=1", "handoff=0"):
        assert cell in bare, (cell, bare)
    for absent in ("met=", "unmet=", "round=", "limit="):
        assert absent not in bare, f"没过验收的那一发里出现了 {absent} ⇒ 活动流替验收岗说了话"

    _again, full = run_module._activity_of(_execution(card_status_after=accept_node.DELIVERED), _acceptance())
    assert "met=2 unmet=0" in full and "round=1" in full and "limit=3" in full, full
    prefix = bare.replace(f"status={accept_node.ACCEPTING}", f"status={accept_node.DELIVERED}")
    assert full.startswith(prefix), (bare, full)
    assert "|" not in bare and "|" not in full, "摘要里带了列分隔符 ⇒ 契约 §4 的五列会被撑成六列"


def test_the_chain_is_the_only_producer_of_the_activity_stream():
    """追行只在驱动里，投影段一次都不追 —— 这就是"活动流以链路为准、状态迁移以 EN-14 为准"那句裁定的形状。

    为什么钉静态的一发：`run_publish` 是整表重投影（可以反复跑），逐卡追行会让同一次交互在每次重投影里
    重复出现；而"哪天有人把追行搬进投影段"在读数上和"链跑了两次"长得一模一样，只有代码形状能分。
    """
    names = called_names(func_body("run_chain"))
    assert names.count("append_event") == 1, names
    assert "OrchestratorWriter" in names, "驱动没有自己的代笔点 ⇒ 追行只能绕开 `PUBLIC_OWNER` 直接写盘"
    for other in ("run_publish", "run_deliver", "run_execute", "run_accept", "run_assign"):
        assert "append_event" not in called_names(func_body(other)), other


# ------------------------------------------------------------------ 二、真 PG


def test_the_chain_dispatches_all_four_cards_one_per_round(bus, session):
    """owner 约束 3 的后半句：同一份"四张同时 ready"的现场，传 1 就一轮一张，四张全跑完。

    四格读数都是库侧/盘侧的现值，不是函数返回值：
    ① 一轮一张 ⇒ 第一发 `dispatched` 恰一张、`deferred` 恰三张（剩下的不是丢了，是下一轮）；
    ② `run_chain` 之后四张卡都到 `DELIVERED`（EN-02 现值）；
    ③ 交付包构成 = **4 个文件**（3 html + 1 css），入口指回那份 `index.html` —— 这一格是 Q9 那个数字
       第一次被**实证**（此前只有内置拆单的纸面构成）；
    ④ 槽与台账收尾归零：`held` 空、每支工具的占用 0（一轮一槽的意义就在这儿）。
    """
    request_id = to_resumed(bus, session)
    try:
        slots = run_module._slots_from_registry(session)
        leases = scheduler.LeaseLedger()
        make_all_ready(session, request_id)
        one = run_module.run_assign(request_id, transport="stub", session=session, now=NOW,
                                    slots=slots, leases=leases, max_cards_per_round=1)
        assert len(one.dispatched) == 1 and one.deferred == ("T02", "T03", "T04"), (one.dispatched, one.deferred)
        assert one.usage == (("doubao", 0), ("qoder", 1), ("workbuddy", 0)), one.usage
        scheduler_run = run_module.run_execute(request_id, one.dispatched[0][0], transport="stub",
                                               bus_root=bus.path, session=session, now=NOW,
                                               findings_for=FINDINGS, slots=slots, leases=leases)
        assert scheduler_run.card_status_after == accept_node.ACCEPTING, scheduler_run
        run_module.run_accept(request_id, one.dispatched[0][0], transport="stub", session=session,
                              now=NOW, accept_for=ACCEPT)
        assert leases.pending() == () and dict(slots.usage())["qoder"] == 0, (leases.pending(), slots.usage())

        report = run_module.run_chain(request_id, transport="stub", bus_root=bus.path, session=session,
                                      now=NOW, findings_for=FINDINGS, accept_for=ACCEPT,
                                      entry_name="index.html")
        statuses = {task_id: str(card.status) for task_id, card in
                    ((str(card.id), card) for card in session.query(TaskCard)
                     .filter(TaskCard.feature_request_id == request_id).all())}
        assert set(statuses.values()) == {accept_node.DELIVERED}, statuses
        assert len(report.dispatched) + 1 == 4, report.dispatched
        row = session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).one()
        entries = sorted(str(item["rel_path"]) for item in (row.manifest or ()))
        assert len(entries) == 4, entries
        assert sum(1 for item in entries if item.endswith(".html")) == 3, entries
        assert sum(1 for item in entries if item.endswith(".css")) == 1, entries
        assert str(row.entry_path).endswith("index.html"), row.entry_path
        assert report.publish.status_rows == 4, report.publish
        assert report.publish.index_entries == report.publish.artifact_rows == 4, report.publish
    finally:
        drop_thread(session, request_id)


def test_a_zero_round_limit_is_refused_before_any_card_leaves_the_queue(bus, session):
    """约束 2 的另一半：`0` 在生产里是**决定错了**，不是"这轮休息" —— 拒在占槽之前，一张卡都不动。

    这一发专门挡"`ready[:0]` 正好也是合法切片"那种写法：切片不报错、循环空转、函数正常返回一个
    `dispatched=()` 的报告 —— 调用方读到的是"这一轮没活干"，而真相是四张 ready 卡被一个 0 抹掉了。
    """
    request_id = to_resumed(bus, session)
    try:
        slots = run_module._slots_from_registry(session)
        leases = scheduler.LeaseLedger()
        make_all_ready(session, request_id)
        before = {str(card.id): str(card.status) for card in
                  session.query(TaskCard).filter(TaskCard.feature_request_id == request_id).all()}
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_assign(request_id, transport="stub", session=session, now=NOW,
                                  slots=slots, leases=leases, max_cards_per_round=0)
        assert caught.value.code == run_module.ROUND_LIMIT_INVALID, caught.value.code
        after = {str(card.id): str(card.status) for card in
                 session.query(TaskCard).filter(TaskCard.feature_request_id == request_id).all()}
        assert after == before == {card_id: "READY" for card_id in before}, (before, after)
        assert leases.pending() == () and sum(slots.usage().values()) == 0, (leases.pending(), slots.usage())
    finally:
        drop_thread(session, request_id)


def test_the_serial_chain_lands_the_public_area_and_the_package_for_all_four_cards(bus, session):
    """串行跑完的现场，公共区与交付包必须是**四张卡**的形状 —— 步骤 3/4 那两格在这一发里合起来验。

    ① `results/**` 条目数 == EN-06 行数（契约 §6 第一式，读的是 `recover.reconcile`）；
    ② `tasks/**` 与 `status.md` 成对（第二式，四行四张）；
    ③ 盘上那个 JSON 的 manifest 与 EN-17 那行逐条相等（两份口径一旦分叉，"打开就能预览"是假的）；
    ④ 交付包字节 > 0 是从盘上量的。
    这一发同时是 T124 端到端那一发的**预演**：`verify_mvp1` 要读的入口与四份文件就在这儿。
    """
    request_id = to_resumed(bus, session)
    root = bus.path / PROJECT
    try:
        report = run_module.run_chain(request_id, transport="stub", bus_root=bus.path, session=session,
                                      now=NOW, findings_for=FINDINGS, accept_for=ACCEPT,
                                      entry_name="index.html")
        assert report.rounds == 4, report.rounds
        consistency = recover.reconcile(bus.path, PROJECT, session)
        assert consistency.pairs == 4 and not consistency.bus_only and not consistency.pg_only, consistency
        assert report.publish.status_rows == 4 and report.publish.cards_published == 4, report.publish
        assert len(list((root / layout.TASKS_DIR).glob("*.md"))) == 4
        payload = json.loads((root / report.delivery.package_path).read_text(encoding="utf-8"))
        row = session.query(Deliverable).filter(Deliverable.feature_request_id == request_id).one()
        assert sorted(item["rel_path"] for item in payload["manifest"]) == sorted(
            str(item.rel_path) for item in assemble_node.validated_manifest(session, request_id)
        )
        assert payload["entry_path"] == str(row.entry_path), payload
        assert report.delivery.package_bytes == (root / report.delivery.package_path).stat().st_size > 0, report.delivery
    finally:
        drop_thread(session, request_id)


def daily_log(root: pathlib.Path) -> list[list[str]]:
    """`daily-log.md` 拆成五列（契约 §4 那行 `时间 | 实体 | 动作 | actor | 摘要`）。

    读侧在 `src` 里没有对应的读者（`recover` 不读这份文件，它不是三元组重建的输入），所以这里自己拆。
    空行不算事件 —— 与 `run._daily_log_lines` 同一口径，两处不一致就会有一处读数偏高。
    """
    text = (root / layout.DAILY_LOG_MD).read_text(encoding="utf-8")
    return [[cell.strip() for cell in line[2:].split(" | ")] for line in text.splitlines() if line.strip()]


def test_every_card_that_ran_leaves_exactly_one_activity_line(bus, session):
    """链上每跑完一张卡，`daily-log.md` 恰多一行；投影段重跑一次都不追行（裁决 3 第 ③ 项 + 那条分工）。

    六格各挡一种错：
    ① 行数 == 跑完的卡数 ⇒ `append_event` 真的落了盘（写侧计数读不出"调用了但没落"）；
    ② 每行的 `actor` 是那一张卡的**执行岗**，且这五个 actor 与 `status.md` 那一列逐张相等 ⇒
       活动流与投影说的是同一批人，没有第二份名单；
    ③ 卡号逐张出现且只出现一次 ⇒ 没有把 T02 的两拍拢成一拍、也没有把一张卡写两遍；
    ④ 摘要列里 `status=` 与 `met=` 都在 ⇒ "做了什么 / 结果"这一格有读数（FR-052 的后半句）；
    ⑤ `run_publish` 再跑一次 ⇒ 行数不变 ⇒ 投影不冒充交互（那条分工现在有了证人，不只是注释）；
    ⑥ `report.activity_lines` 是从盘上数回来的，与文件里的行数相等 ⇒ 报告自己不是第二份事实。
    """
    request_id = to_resumed(bus, session)
    root = bus.path / PROJECT
    try:
        report = run_module.run_chain(request_id, transport="stub", bus_root=bus.path, session=session,
                                      now=NOW, findings_for=FINDINGS, accept_for=ACCEPT,
                                      entry_name="index.html")
        lines = daily_log(root)
        assert len(lines) == report.activity_lines == len(report.dispatched) == 4, (
            len(lines), report.activity_lines, report.dispatched
        )
        ran = {entry[0] for entry in report.dispatched}
        assert sorted(line[4].split(" ")[0] for line in lines) == sorted(f"task={card_id}" for card_id in ran), lines
        status = recover.read_status(root)
        for line in lines:
            assert len(line) == 5, line
            assert line[1] == run_module.ACTIVITY_ENTITY and line[2] == run_module.ACTIVITY_ACTION, line
            card_id = line[4].split(" ")[0].split("=", 1)[1]
            assert line[3] == status[card_id].executor, (line, "actor 列与投影那张卡的执行岗不是同一个人")
            assert f"task={card_id}" in line[4] and "met=" in line[4] and "status=" in line[4], line
            assert line[0].startswith("2026-"), line
        again = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert len(daily_log(root)) == 4, "投影段重跑一次就往活动流里追了一行 ⇒ 交互数被投影次数污染"
        assert again.agents_path == layout.AGENTS_MD and (root / layout.AGENTS_MD).is_file(), again
    finally:
        drop_thread(session, request_id)
