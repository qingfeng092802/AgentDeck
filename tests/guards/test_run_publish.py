"""T123 装配点（T124 步骤 4）：公共区投影段 `run_publish()` 的判据。

**这一笔接的是哪一段**：交付包段通了之后，`results/delivery_package.json` 有了，但契约 §4 那两件
（`status.md` 每卡一行、`results/<卡号>.md` 产物索引）与 §1 的目录在 `src` 里**一个生产写者都没有** ——
`phase6-review-v1.0.0.md` 第 5 行早就把这一格登记为待办，原话是"公共投影在整个生产路径上还没有任何写点"。
不接这一格，两件事立不住：契约 §6 那两条关系式在真实链路上算不出数，宪法「三」那条"停 PG 后按 §4
重建三元组"也没有输入可重建。

本文件钉六件事，每件各挡一种会发生的错：

① **五件写面恰好那五个**：`ensure_layout` / `write_agents_md` / `write_task_card` / `upsert_status` /
   `write_result_index` 各一次，且装配点一次都不直接 `gateway.write_artifact`（`acl.PUBLIC_OWNER` 那单点代笔）。
   这一条在 2026-10-17 之前钉的是"**四件** + 零次 `write_agents_md`"（那句"本笔明写不接"的理由记在
   `evidence/phase-12.md` §十四/§十五，不删）；owner 裁决 3 第 ② 项点名补 `AGENTS.md` 的写者 ⇒ **改口不是删**。
   `append_event` 仍钉在本段之外（活动流的写者归 `run_chain`，那条分工由 `test_run_chain.py` 钉）。
② **可见面只投影一次**：下发给 Agent 的 `Task` 三行与公共区那张 `tasks/<卡号>-<slug>.md` 三行，
   必须走同一个 `_card_face_of()`。挡的错是哪一侧改了分隔符或换了列，Agent 当时看到的与事后可查的就分叉。
③ **三岗与状态只取 EN-02 现值**，没指派就是空串。挡的错是把"还没决定"渲染成一个像决定的词。
④ **用量那一格分列、口径名不抄第二份**：口径清单取自 `orchestrator_writer.USAGE_BASIS`，数字取自
   `cost/ledger.by_task` 的**金额小计**。挡的错有两个方向：自己再列一遍 `TOKEN/SEAT_CREDIT/BYOK`
   （两份清单迟早漂），以及把三口径加成一个数（FR-061 明禁，`ledger.Breakdown.total()` 就是为它抛的）。
⑤ **索引与落点读同一次 EN-06，且一次都不筛 `validated_status`**：`results/<卡号>.md` 的条目与 `status.md`
   的"产物落点"出自同一次读取、同一份排序；契约 §6 关系式一比的是"条目数 == `artifact` 记录数"，而
   读侧（`recover.reconcile`）没有 valid 条件 ⇒ 投影自己加一道过滤，那条式子就变成单边差集。
   挡的错有两个：落点写一个索引里没有的路径（两份事实态），以及"顺手只投影达标的那几行"。
⑥ **读数从盘上用读侧的读者读回**：`status_rows` / `index_entries` 走 `recover.read_status` /
   `read_result_index`，不是写入次数的转抄。挡的错是"写了但没落"这一形 —— 用写侧计数永远读不出来。

诚实性（与本链其余四段同规）：仿真运输不产用量回执（`adapters/stub.py` 边界 ③"没有真实调用就没有用量"）
⇒ 端到端那一发的用量三格恒为 0，本笔里**非零**的那一发是直接塞 `cost_record` 行进事务的，
它证明的是"分列的通道通了"，不是"真调用的账算对了"。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import pathlib
import re
import subprocess
import uuid
from contextlib import closing
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp import chaos
from acp import recover
from acp import run as run_module
from acp.adapters.stub import StubMisuse
from acp.approval import repository as repo
from acp.bus import acl, layout
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import assemble as assemble_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import AcceptanceMap, ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, TaskCard
from acp.persistence.models_registry import CostRecord


SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 17, 10, 40, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-publish"
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


def attr_reads(func: ast.AST, attribute: str, owners: tuple[str, ...] = ("row", "card")) -> int:
    """函数体里对 `<row|card>.<attribute>` 这种**读列**的次数（钉"某一列只在一处被读"用）。

    只数挂在行对象上的读取，不数 `face.depends_on` 那种读回投影结果的 —— 判据要挡的是"第二个投影点"，
    而 `_card_face_of` 的结果被两处共用正是本笔要的形状。
    """
    return sum(1 for node in ast.walk(func)
               if isinstance(node, ast.Attribute) and node.attr == attribute
               and isinstance(node.value, ast.Name) and node.value.id in owners)


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


def to_delivered(bus, session) -> uuid.UUID:
    """真跑到**一张卡 `DELIVERED` 并组好包**，返回需求单号（另外三张仍是未指派的 DRAFT 形）。"""
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
    run_module.run_accept(request_id, card_id, transport="stub", session=session, now=NOW, accept_for=ACCEPT)
    rel = str(session.query(Artifact.rel_path).filter(Artifact.task_card_id == card_id).one()[0])
    run_module.run_deliver(request_id, transport="stub", bus_root=bus.path, session=session, now=NOW,
                           entry_name=rel.rsplit("/", 1)[-1])
    return request_id


def card_count(session, feature_request_id: uuid.UUID) -> int:
    return session.query(TaskCard).filter(TaskCard.feature_request_id == feature_request_id).count()


def to_intaked(bus, session) -> uuid.UUID:
    """只跑受理段：卡有、单据还是 PENDING、**没有结论到达行**。

    本笔"没卡"那一发要从这里删起 —— 走完整条链的话，`approval_ticket` 底下还挂着 `resolution_event`
    （它只外键到 `approval_ticket.id`，没有需求单列可筛），拆它要在测试里多写一层拆除工，
    而这一发要证明的是"没卡就拒"，不是"我能把库拆干净"。
    """
    bus_root_of(bus)
    intake = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=bus.path,
                                   session=session, now=NOW)
    return intake.feature_request_id


def strip_cards(session, feature_request_id: uuid.UUID) -> None:
    """在**测试事务里**把这个需求单的卡抹掉，造出"有单没卡"那一形。

    删除顺序不是随手排的：`acceptance_map` / `review_verdict` / `approval_ticket` / `artifact` 四张表都按
    复合外键 `(feature_request_id, task_card_id)` 指向 `task_card`（owner 裁决 2 把卡主键改成复合键的
    连带后果，见 `models_core.py::TaskCard` 那段说明），先删卡会撞 `ForeignKeyViolation` —— 本发第一版
    就是这么炸的，留在这里免得下一轮又排一次。`approval_ticket` 底下还挂着 `resolution_event`，所以本发
    的现场只跑到受理段（没批单、没有结论到达行），把那条链留在场景外，而不是在测试里补一层拆除工。
    """
    for model in (ReviewVerdict, AcceptanceMap, ApprovalTicket, Artifact, TaskCard):
        session.query(model).filter(model.feature_request_id == feature_request_id).delete()
    session.flush()


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


def test_the_transport_gate_speaks_first_in_the_publish_segment():
    """门先说话，与受理/恢复/指派/执行/验收/交付包同一条纪律：`transport=""` 撞仿真门，轮不到读库。"""
    with pytest.raises(StubMisuse):
        run_module.run_publish(uuid.uuid4(), transport="", bus_root=pathlib.Path("/tmp/unused"), session=None)


def test_the_public_area_has_exactly_the_five_producers_this_segment_declares():
    """本段的公共区写面**恰好五个动作、一个代笔点**，而且不多写一件、不少写一件。

    五格各挡一种错：
    ① 五个动作各恰好一次（`ensure_layout` 与 `write_agents_md` 各一发、每卡三个动作各一处调用点）——
       少一个就是五件里有一件没接；
    ② 只有一个 `OrchestratorWriter` 构造点：两条队列 = 两个"唯一写入者"，`worker_thread_id` 那格就作废了；
    ③ 零次 `write_artifact`：绕开代笔就是绕开 `acl.PUBLIC_OWNER` 与网关那道脱敏；
    ④ 零次 `append_event`：活动流那件归 `run_chain`（owner 裁决 3 第 ③ 项的落点选择），本段是**重投影**，
       逐卡追行会让同一次交互在每次重投影里重复出现；
    ⑤ 零次 `write_delivery_package`：交付包那件另有写者（`run_deliver`），两段各写各的。
    """
    body = func_body("run_publish")
    names = called_names(body)
    for action in ("ensure_layout", "write_agents_md", "write_task_card", "upsert_status", "write_result_index"):
        assert names.count(action) == 1, (action, names)
    assert names.count("OrchestratorWriter") == 1, names
    assert "write_artifact" not in names, "直接走网关写公共区 = 绕过单点代笔"
    for deferred in ("append_event", "write_delivery_package"):
        assert deferred not in names, f"`{deferred}` 不属于本段（交付包与活动流另有写者）"


def test_the_card_face_is_projected_once_and_both_sides_take_that_one():
    """下发面与公共区卡文件走**同一个** `_card_face_of()`，且那三列的读取只发生在它内部。

    这条是本轮把 `_task_of` 里那份投影搬出来之后钉的：`_task_of` 与 `run_publish` 都调它（两路都必须过），
    而 `run_publish` 自己一次都不读 `depends_on` / `acceptance_criteria` / `title` 那三列 —— 一旦两处各拼，
    分隔符或取列在哪一侧改动都会让"Agent 看到的任务"与"事后能查的任务"分叉，而读侧不会炸。
    """
    assign = called_names(func_body("_task_of"))
    publish = called_names(func_body("run_publish"))
    assert "_card_face_of" in assign, "下发载体没走那份投影"
    assert "_card_face_of" in publish, "公共区卡文件没走那份投影"
    face = func_body("_card_face_of")
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for column in ("title", "depends_on", "acceptance_criteria"):
        assert attr_reads(face, column) >= 1, column
        assert attr_reads(tree, column) == attr_reads(face, column), (
            f"`.{column}` 在 `_card_face_of` 之外还有第二处读列 ⇒ 那份投影不是唯一口径"
        )


def test_the_usage_column_never_relists_the_bases_and_never_sums_them():
    """用量那一格：口径名只有一份（`USAGE_BASIS`），数字只从 `ledger.by_task` 的分列小计里取。

    两个方向各挡一种错：
    ① `run.py` 全文出现 `"TOKEN"` / `"SEAT_CREDIT"` / `"BYOK"` 三个字面量的次数为 **0** ——
       自己再列一遍就是给 FR-061 那三口径造第二个载体，两份清单迟早漂；
    ② 全 `run.py` 里没有任何一处 `cost_amount` 参与加法 —— 把三口径（或任意几笔金额）加成一个数
       正是 `ledger.Breakdown.total()` 抛 `CrossBasisSumRefused` 禁掉的那件事，装配点不许自己绕过它。
    """
    text = SRC.read_text(encoding="utf-8")
    for basis in ("TOKEN", "SEAT_CREDIT", "BYOK"):
        assert f'"{basis}"' not in text, f"口径名 {basis} 在 `run.py` 里被抄了第二份"
    assert "USAGE_BASIS" in text and "USAGE_BASIS" in called_names(func_body("_usage_by_basis")) + [
        node.id for node in ast.walk(func_body("_usage_by_basis")) if isinstance(node, ast.Name)
    ], "那一格没从 `USAGE_BASIS` 取口径名"
    body = func_body("_usage_by_basis")
    assert [name for name in called_names(body) if name in ("sum", "total")] == [], (
        f"分列小计被加了：{called_names(body)}"
    )
    assert "by_task" in called_names(body), "那一格不是从 EN-13 的按任务分列里读的"


def test_the_two_public_readings_come_from_the_reader_side_not_the_write_count():
    """报告里的两个计数用**读侧的读者**从盘上读回，不转抄写了几次。

    挡的错是"写了但没落"：`write_*` 的返回的是相对路径，装配点若拿循环次数当读数，文件没落地也照样"对"。
    这里钉 `recover.read_status` / `recover.read_result_index` 各出现一次，且 `run_publish` 不 `stat()` 任何
    文件尺寸（尺寸那一格归交付包段，两段互抄就变成两份读数口径）。
    """
    body = func_body("run_publish")
    names = called_names(body)
    assert names.count("read_status") == 1, names
    assert names.count("read_result_index") == 1, names
    assert "stat" not in names, "尺寸读数属交付包段那一份口径，本段不另起一处"


def test_the_card_file_name_carrier_stays_parseable_and_the_empty_cells_are_written_empty():
    """`CARD_SLUG` 必须过契约 §3 的文件名规范，未指派那一格渲染成**空串**而不是一个像决定的词。

    ① `layout.is_task_file()` 是读侧 `TASK_FILE_RE` 的那一份规则，这里用同一个判据而不是再写一个正则；
    ② 三行正文允许为空吗？不允许 —— `recover.CARD_LINE_RE` 的 `(.+)` 会拒空值，所以空依赖那一格
       必须有内容。本笔的答复是两个明写的空串常量（`NO_INPUTS` / `NO_CRITERIA`），断言它们非空、
       且**不含** `": "`（否则 `_parse_body` 按标签切完之后会多出一个假标签）。
    """
    assert layout.is_task_file(f"T01-{run_module.CARD_SLUG}.md"), run_module.CARD_SLUG
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", run_module.CARD_SLUG), run_module.CARD_SLUG
    for text in (run_module.NO_INPUTS, run_module.NO_CRITERIA):
        assert text and ": " not in text, text
    body = SRC.read_text(encoding="utf-8")
    assert body.count("executor_agent_id or \"\"") >= 1, "三岗那一格不是空串形态 ⇒ 未指派会被渲染成一个词"


# ------------------------------------------------------------------ 二、真 PG


def test_the_projection_makes_both_contract_six_relations_hold_on_a_real_chain(bus, session):
    """真链跑到交付之后投影 ⇒ 契约 §6 那两条关系式在**读侧**真的算得出数。

    这一发是本笔的主证人，它断的全是读侧的数（不是装配点自己报了几个）：
    ① `recover.rebuild` 不抛 ⇒ `tasks/**` 与 `status.md` 成对（第二式），且每张卡三行都解析得开；
    ② `recover.reconcile` 不抛且 `pairs == EN-06 行数` ⇒ `results/**` 条目数等于产物行数（第一式）；
    ③ `report.cards_published == 4` 而 `artifact_rows == 1` ⇒ 投影的是**这个需求单的全部四张卡**，
       产物只有一张卡有（另三张还是未指派的 DRAFT 形）—— 这一格同时是"不许只投影跑过的那一张"的证人；
    ④ `status.md` 里那张交付卡的"产物落点"确实出现在它的 `results/T0x.md` 条目里（两份不各写一份）。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        report = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert report.project_key == PROJECT, report
        assert report.cards_published == card_count(session, request_id) == 4, report
        assert report.status_rows == 4, report
        assert report.artifact_rows == report.index_entries == 1, report
        reconstruction = recover.rebuild(bus.path, PROJECT)
        assert reconstruction.card_count == reconstruction.status_row_count == 4, reconstruction
        consistency = recover.reconcile(bus.path, PROJECT, session)
        assert consistency.pairs == 1 and not consistency.bus_only and not consistency.pg_only, consistency
        rows = recover.read_status(root)
        indexes = {task_id: recover.read_result_index(root, task_id) for task_id in rows}
        delivered = [row for row in rows.values() if row.artifact_path]
        assert len(delivered) == 1, delivered
        assert delivered[0].artifact_path in indexes[delivered[0].task_id][0], delivered
    finally:
        drop_thread(session, request_id)


def test_unassigned_seats_are_written_as_empty_and_the_status_is_the_database_value(bus, session):
    """没指派的那三岗在盘上是**空串**，状态列等于 EN-02 现值 —— 一个词都不替库替。

    挡的错有两个方向：把 `None` 渲染成 `"None"`（读侧会把它当成一个 agent 名字，`recover` 的三元组
    里就出现一个不存在的工具），以及拿 `run_assign` 的返回值填状态（那时图上刚迁完，库里若是别的值
    就露馅 —— 原则 I 要的是读库，不是读返回值）。
    """
    bus_root_of(bus)
    intake = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=bus.path,
                                   session=session, now=NOW)
    request_id = intake.feature_request_id
    try:
        run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        rows = recover.read_status(bus.path / PROJECT)
        assert len(rows) == 4, rows
        for task_id, row in rows.items():
            card = session.get(TaskCard, (request_id, task_id))
            assert row.status == str(card.status), (task_id, row.status, card.status)
            assert (row.executor, row.reviewer, row.acceptor) == (
                str(card.executor_agent_id or ""), str(card.reviewer_agent_id or ""),
                str(card.acceptor_agent_id or ""),
            ), (task_id, row)
            assert "None" not in (row.executor + row.reviewer + row.acceptor), row
    finally:
        drop_thread(session, request_id)


def test_publishing_twice_leaves_one_row_per_card_and_identical_bytes(bus, session):
    """投影是**幂等**的：连发两次之后 `status.md` 仍每卡一行、`tasks/**` 仍四张，文件字节一模一样。

    这一发挡的是"追加而不是重写"：`upsert_status` 本体是整表重写（契约 §4），但如果装配点哪天改成
    直接 `write_artifact` 追加，第二发就会得到八行 —— 而"每张任务在 status.md 恰有一行"是 §6 第二式，
    多出来的那几行不会报错，只会在重建时挑到旧状态。字节级对照同时钉住"文件名不随发的次数漂"。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        first = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        status_before = (root / layout.STATUS_MD).read_text(encoding="utf-8")
        cards_before = sorted(path.read_bytes() for path in (root / layout.TASKS_DIR).glob("*.md"))
        agents_before = (root / layout.AGENTS_MD).read_bytes()
        again = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert again.status_rows == first.status_rows == 4, (first, again)
        assert again.index_entries == first.index_entries == 1, (first, again)
        assert (root / layout.STATUS_MD).read_text(encoding="utf-8") == status_before
        assert sorted(path.read_bytes() for path in (root / layout.TASKS_DIR).glob("*.md")) == cards_before
        assert len(list((root / layout.TASKS_DIR).glob("*.md"))) == 4
        assert (root / layout.AGENTS_MD).read_bytes() == agents_before, "AGENTS.md 每一发重投影都长一点"
    finally:
        drop_thread(session, request_id)


def test_the_agents_md_is_written_by_the_projection_from_the_registration_table(bus, session):
    """`AGENTS.md` 由投影段真的写出来，名单是 **EN-03 现值**而不是写死的那三支（owner 裁决 3 第 ② 项）。

    四格各挡一种错：
    ① 文件在盘上、字节 > 0，且报告里那一格给的是**相对路径**（尺寸由本发自己从盘上量 ——
       投影段那条"不 `stat()` 尺寸"的口径归交付包段，这里不破它）⇒ "调用过写者"不等于"落盘了"；
    ② 每一条 `acl.RULES` 的归属行都在文件里 ⇒ 那份运行时副本真的从 `acl` 生成，不是一份手抄的空壳
       （方向要说清：`acl` 是权威，文件必须跟上它，不是拿文件反过来定 `acl`）；
    ③ **在册名单取自 EN-03 现值** —— 这一格是真注入：往事务里加一支本批次从没派发过的工具，重投影之后
       它的 workspace 名字必须出现在文件里。挡的错是把三支名字写死（写死了 ② 照样成立，读数看不出来）；
    ④ 删掉再投影一次能长回来 ⇒ 写者是这一段时间本身，不是别的段顺手留下的那份。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        first = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        text = (root / layout.AGENTS_MD).read_text(encoding="utf-8")
        assert first.agents_path == layout.AGENTS_MD, first
        assert (root / layout.AGENTS_MD).stat().st_size > 0, first
        for rule in acl.RULES:
            assert f"| `{rule.pattern}` | `{rule.owner}` |" in text, rule.pattern
        for name in first.agents:
            assert layout.workspace_dir_name(name) in text, name

        session.add(
            AgentUnit(
                agent_id="latecomer", form="SDK", tier=2, cost_band="MID", max_parallel=1,
                current_load=0, success_rate_sample=0, workspace_dir="latecomer-workspace", enabled=True,
            )
        )
        session.flush()
        second = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert "latecomer" in second.agents, second.agents
        assert layout.workspace_dir_name("latecomer") in (root / layout.AGENTS_MD).read_text(encoding="utf-8"), (
            "新在册的工具没进 AGENTS.md ⇒ 名单是写死的三支"
        )
        (root / layout.AGENTS_MD).unlink()
        third = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert third.agents_path == layout.AGENTS_MD, third
        assert (root / layout.AGENTS_MD).stat().st_size > 0, "删掉之后投影段没有把它长回来 ⇒ 写者不是这一段"
    finally:
        drop_thread(session, request_id)


def test_the_usage_column_is_per_basis_and_never_a_merged_number(bus, session):
    """给一张卡塞一笔 EN-13 金额 ⇒ 那一格只有它所属口径非零，另两口径仍是 0。

    这是 ④ 那条静态判据的行为证人（静态那格只能挡住"抄了第二份口径名"）：
    · TOKEN 格读到 `0.5`（`pricing` 之外的现值，直接落 `cost_record`，不经任何换算）；
    · `SEAT_CREDIT` / `BYOK` 仍是 `0` ⇒ 装配点没有把三口径拢成一个数往每一格里填；
    · 另一张没有账的卡三格全 `0` ⇒ 也不存在"把上一张的账串到这一张"。
    仿真链本身产不出这行（`StubAdapter` 边界 ③）⇒ 本发证明的是**通道分列**，不是"真调用的账算对了"。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        cards = session.query(TaskCard).filter(TaskCard.feature_request_id == request_id).all()
        billed = next(str(card.id) for card in cards if str(card.status) == accept_node.DELIVERED)
        other = next(str(card.id) for card in cards if str(card.id) != billed)
        session.add(
            CostRecord(
                id=uuid.uuid4(),
                occurred_at=NOW,
                project_key=PROJECT,
                task_card_id=billed,
                agent_id=str(session.get(TaskCard, (request_id, billed)).executor_agent_id),
                role_id="role-test",
                model_ref=None,
                usage_kind="INPUT",
                quantity=Decimal("1000"),
                unit="token",
                cost_amount=Decimal("0.5"),
                currency="CNY",
                billing_basis="TOKEN",
            )
        )
        session.flush()
        run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        rows = recover.read_status(root)
        assert rows[billed].usage == {"TOKEN": Decimal("0.5"), "SEAT_CREDIT": Decimal("0"),
                                      "BYOK": Decimal("0")}, rows[billed].usage
        assert rows[other].usage == {"TOKEN": Decimal("0"), "SEAT_CREDIT": Decimal("0"),
                                     "BYOK": Decimal("0")}, rows[other].usage
    finally:
        drop_thread(session, request_id)


def test_an_invalid_artifact_row_is_still_indexed_and_the_relations_still_hold(bus, session):
    """门禁判 `invalid` 的那一行**照样进索引** —— 投影段不筛 valid，筛的是交付包段。

    这一发挡的是"顺手把不达标的那行滤掉"：契约 §6 关系式一比的是"`results/**` 条目数 == 该任务
    `artifact` 记录数"，而 `recover.reconcile` 那侧的 EN-06 查询**没有** `validated_status` 条件
    （`recover.py:230`）。投影一旦自己筛，那条关系式就变成"只在 EN-06 侧多一条"的差集 ⇒ 一致性检查
    在一条真链上永久报错。SC-007 要防的虚报形状另有拦法（`assemble.validated_manifest` 那一道），
    不在公共区这一格。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        card_id = next(str(card.id) for card in
                       session.query(TaskCard).filter(TaskCard.feature_request_id == request_id).all()
                       if str(card.status) == accept_node.DELIVERED)
        session.add(
            Artifact(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                task_card_id=card_id,
                writer_agent_id=str(session.get(TaskCard, (request_id, card_id)).executor_agent_id),
                rel_path="doubao-workspace/draft.html",
                bytes=64,
                lines=3,
                format="html",
                self_reported_status="success",
                validated_status="invalid",
                invalid_reason="guard_min_threshold",
                created_at=NOW,
            )
        )
        session.flush()
        report = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert report.artifact_rows == report.index_entries == 2, report
        consistency = recover.reconcile(bus.path, PROJECT, session)
        assert consistency.pairs == 2 and not consistency.bus_only and not consistency.pg_only, consistency
        row = recover.read_status(root)[card_id]
        assert row.artifact_path in [entry[0] for entry in recover.read_result_index(root, card_id)], row
    finally:
        drop_thread(session, request_id)


def test_a_request_without_cards_refuses_instead_of_publishing_an_empty_status(bus, session):
    """一张卡都没有 ⇒ `cards_absent`，四件一个都不写。挡的错是把"空表"当成"这个项目就是没活"。

    现场是**造**出来的（在事务里删掉这个需求单的卡行），不是自然形态：受理段跑完就一定有卡。
    之所以还要这一发：调用方拿错需求单号（或将来 `--publish` 接上 CLI 之后敲错 UUID）时，
    静默写出一张空 `status.md` 会覆盖掉上一次的真投影 —— 那才是它最贵的表现。
    """
    request_id = to_intaked(bus, session)
    root = bus.path / PROJECT
    try:
        first = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert first.status_rows == 4, first
        before = (root / layout.STATUS_MD).read_text(encoding="utf-8")
        strip_cards(session, request_id)
        assert card_count(session, request_id) == 0, "现场没造出来（还有卡）"
        with pytest.raises(run_module.RunFailure) as caught:
            run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert caught.value.code == run_module.CARDS_EMPTY, caught.value.code
        assert (root / layout.STATUS_MD).read_text(encoding="utf-8") == before, "拒不掉就把上一次的真投影覆盖了"
    finally:
        drop_thread(session, request_id)


def test_the_package_row_and_the_projection_do_not_read_each_sides_files(bus, session):
    """交付包段与投影段各读各的：本段不碰 `delivery_package.json`，也不因为"包已存在"而少写一行。

    这一发是上一发（幂等）之外另一种"两段互抄"的形状：先组包再投影，四件应当**全部**落齐；
    如果哪天有人把 `run_publish` 改成"有包了才写 status.md"或者拿包里的 manifest 当索引来源，
    这里就会看到 `results/**` 少一张、或 `status.md` 少了那三张 DRAFT 卡。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        package_before = (root / assemble_node.delivery_path()).read_bytes()
        report = run_module.run_publish(request_id, transport="stub", bus_root=bus.path, session=session)
        assert (root / assemble_node.delivery_path()).read_bytes() == package_before, "投影段动了交付包"
        assert sorted(path.name for path in (root / layout.TASKS_DIR).glob("*.md")) == [
            f"T0{index}-{run_module.CARD_SLUG}.md" for index in range(1, 5)
        ], sorted(path.name for path in (root / layout.TASKS_DIR).glob("*.md"))
        assert report.status_rows == 4
        assert sorted(path.name for path in (root / layout.RESULTS_DIR).glob("T*.md")) == [
            f"T0{index}.md" for index in range(1, 5)
        ], sorted(path.name for path in (root / layout.RESULTS_DIR).glob("T*.md"))
        assert sum(len(recover.read_result_index(root, f"T0{index}")) for index in range(1, 5)) == 1
    finally:
        drop_thread(session, request_id)
