"""Phase 4 的库侧判据：或签 CAS、到达流水、未决单合并与排序、免确认落库、EN-11 灌表、checkpoint schema。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_schema_invariants.py`）。
为什么 CAS 这组判据必须在真库里跑：`UPDATE ... WHERE version = :v AND state = 'PENDING'` 的
命中/未命中是**数据库给的答案**，用内存替身去验就等于拿我自己对 SQL 的理解来给我的实现作证，
那条判据在 SQLite / dict / 任何替身上都会恒绿。

三条刻意的写法：
1. **每个负例都用 savepoint**（`session.begin_nested()`）：PG 里语句一失败整个事务就 aborted，
   后面任何查询都报 "current transaction is aborted" ⇒ 不用 savepoint 的话，
   第一个负例之后的所有用例都会以另一种错误形态失败，读起来像"环境坏了"而不是"这条拒了"。
2. **`后到不二次迁移` 断的是三件事**：单据的 `state` 没动、`version` 没动、`decided_*` 没被改写，
   外加流水里多了一行 `applied=false`。只断"返回 409"是不够的 —— 返回码对而库里改写了，
   是更坏的那种缺陷。
3. **"库里若有两行未决单"这一支用直接建行造出来**：那条部分唯一索引管不到"卡列为 NULL 的 A 类单"
   （PG 里 NULL 彼此不相等，`models_approval.py:101-109` 已把这一条登记为已知残余），
   所以索引不会替我拦，`pending_for()` 必须自己喊。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER, TimeoutPolicy, parse_timeout_table
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.checkpointer import library_declared_schema
from acp.persistence.enums import values_of
from acp.persistence.models_approval import ApprovalTicket, ResolutionEvent, RiskRule
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.registry import risk_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（CAS 与部分唯一索引只能在库里验）",
)

NOW = dt.datetime(2026, 10, 13, 18, 0, 0, tzinfo=dt.timezone.utc)
APPROVERS = frozenset({"owner-1"})
TABLE = parse_timeout_table()


def policy(kind_letter: str) -> TimeoutPolicy:
    return TABLE[KIND_BY_LETTER[kind_letter]]


@pytest.fixture(scope="session")
def engine():
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


@pytest.fixture
def request_row(session) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def card_row(session, request_row) -> TaskCard:
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=["T01"],
            ref=f"local:probe-{snapshot_id}", rolled_back_at=None,
        )
    )
    session.flush()
    return new_card(session, request_row.id, snapshot_id, "T01")


def new_card(session, request_id, snapshot_id, card_id: str) -> TaskCard:
    """插一张最小可用的卡。排序用例需要 4 张**不同**的卡：共用卡号会被 FR-045 合并成一行，
    那条"排序"断言就会变成"一行"，读起来永远是绿的。"""
    card = TaskCard(
        id=card_id, feature_request_id=request_id, title=f"卡 {card_id}", difficulty=2,
        skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12, artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
        status="PENDING_APPROVAL", snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    return card


def extra_card(session, card_row, card_id: str) -> TaskCard:
    """再加一张卡，并把它登记进同一条 snapshot 的 `task_card_ids` —— 不登记就是拿 fixture
    造出"卡挂着存档点、存档点却不认它"的假形状，那种形状真出现在库里时这里的判据就看不见。"""
    card = new_card(session, card_row.feature_request_id, card_row.snapshot_id, card_id)
    snapshot = session.get(Snapshot, card_row.snapshot_id)
    snapshot.task_card_ids = [*snapshot.task_card_ids, card_id]
    session.flush()
    return card


def payload(kind_letter: str) -> dict:
    return service.build_payload(
        kind=KIND_BY_LETTER[kind_letter],
        pending_item=f"{kind_letter} 类待批内容",
        context_digest="上下文摘要",
        suggested_verdict="approve",
        impact={"downstream_task_ids": ["T01"], "estimated_extra_usage": {}},
    )


def resolve(session, ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD, principal="owner-1", **kw):
    kw.setdefault("now", NOW)
    return repo.resolve(
        session,
        ticket_id=ticket_id,
        verdict=verdict,
        version=version,
        channel=channel,
        principal=principal,
        approvers=APPROVERS,
        **kw,
    )


def event_rows(session, ticket_id) -> list[ResolutionEvent]:
    return repo.events(session, ticket_id)


# ---------------------------------------------------------------- 建单形状

def test_created_ticket_is_pending_version_zero_and_never_expires_for_A(session, request_row, card_row):
    """A 类：§1 写"无超时" ⇒ `timeout_at` 落 timestamptz 上界；B 类才落一个真实时刻。"""
    created = repo.create_or_merge(
        session,
        kind=KIND_BY_LETTER["A"],
        payload=payload("A"),
        risk_exempt_eligible=False,
        timeout_policy=policy("A"),
        feature_request_id=request_row.id,
        now=NOW,
    )
    row = session.get(ApprovalTicket, created.ticket_id)
    assert (str(row.state), row.version, created.created) == ("PENDING", 0, True)
    assert row.timeout_at is None, "A 类不许有一个会到期的时刻（裁决 ②：落 SQL NULL）"
    assert row.decided_by_principal is None and row.decided_at is None

    on_card = repo.create_or_merge(
        session,
        kind=KIND_BY_LETTER["B"],
        payload=payload("B"),
        risk_exempt_eligible=False,
        timeout_policy=policy("B"),
        task_card_id="T01",
        feature_request_id=request_row.id,
        now=NOW,
    )
    assert session.get(ApprovalTicket, on_card.ticket_id).timeout_at == NOW + dt.timedelta(hours=2)


def test_an_E_ticket_is_built_by_the_factory_with_an_empty_downstream_impact(
    session, request_row, card_row
):
    """Phase 17 P1 的 E 类端到端：工厂建单 + 五要素落库 + 空 `downstream_task_ids` + 无超时。

    这条走的是**产品路径**（`service.create_approval`），不是直接写仓储 —— contract 档那条
    `validate_payload` 只证明形状，这一条才证明"E 真的能建出一张 PENDING 单，且库里读得到"。
    """
    kind = KIND_BY_LETTER["E"]
    built = service.build_payload(
        kind=kind,
        pending_item="审查拿不准：标准「好看」不可验证",
        context_digest="产物已审，主观标准无法判定",
        suggested_verdict="need_info",
        impact={"downstream_task_ids": [], "estimated_extra_usage": {"TOKEN": 0}},
    )
    created = service.create_approval(
        session,
        kind=kind,
        payload=built,
        task_card_id=card_row.id,
        feature_request_id=request_row.id,
        now=NOW,
    )
    row = session.get(ApprovalTicket, created.ticket_id)
    assert (str(row.kind), str(row.state), created.merged) == ("E_REVIEW_UNCERTAIN", "PENDING", False)
    assert row.payload["impact"]["downstream_task_ids"] == []
    assert row.timeout_at is None, "§1 的 E 行是「无超时」⇒ 落 SQL NULL，不给一个会到期的时刻"
    assert row.risk_exempt_eligible is False, "E 不属于 FR-048 的免确认作用域"


def test_a_no_timeout_ticket_stores_sql_null(session, request_row, card_row):
    """裁决 ② 的库侧形态：`None` 落的是 **NULL**，不是一个极远的时刻，也不报错（列已放宽）。

    断三件事：
    1. 读回来是 `None`（不是 datetime）；
    2. SQL 里 `information_schema` 说这一列 `IS_NULLABLE = YES`（列真的放宽了，不是 ORM 单方面以为）；
    3. `timeout_at <= now()` 在 SQL 侧读出 **NULL** 而不是 `false`。
       第 3 点是这条用例存在的主要理由：两种形状在"扫描会不会捞到它"上等价，
       但 `false` 的含义是"算过了、没到期"，`NULL` 的含义是"没有可算的时刻"——
       §1 写的"无超时"是后者，用 `false` 表达就等于把一次口径变更藏进了三值逻辑。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    read_back = session.execute(
        text("select timeout_at, (timeout_at <= now()) is null from approval_ticket where id=:i"),
        {"i": ticket_id},
    ).one()
    assert read_back[0] is None, "落成了时刻（哨兵复活）而不是 NULL"
    assert read_back[1] is True, "`timeout_at <= now()` 必须走三值逻辑的 NULL 分支"

    nullable = session.execute(
        text("select is_nullable from information_schema.columns "
             "where table_name='approval_ticket' and column_name='timeout_at'")
    ).scalar()
    assert nullable == "YES", f"列还是 NOT NULL（迁移 0006 没生效）：{nullable}"

    # B 类对照：有超时的那一类必须落一个真时刻，否则上面那条 NULL 就成了"全都不写"。
    on_card = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["B"], payload=payload("B"), risk_exempt_eligible=False,
        timeout_policy=policy("B"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    assert session.get(ApprovalTicket, on_card.ticket_id).timeout_at == NOW + dt.timedelta(hours=2)


def test_the_timeout_scan_predicate_skips_no_timeout_tickets(session, request_row, card_row):
    """把"扫描不会放行 A/C/D"写成一条 SQL 层的读数，而不是靠推理。

    三类各插一行是**故意绕开 `create_or_merge`**：同一需求单下它会按 FR-045 合并成一行，
    那样"三类都不被捞到"就只剩一类在受测。这里直插三行，每类一行。
    """
    for letter, card_id in (("C", "T02"), ("D", "T03")):
        extra_card(session, card_row, card_id)
    for letter, card_id in (("C", "T02"), ("D", "T03")):
        session.add(
            ApprovalTicket(
                id=uuid.uuid4(), task_card_id=card_id, feature_request_id=request_row.id,
                kind=KIND_BY_LETTER[letter], state="PENDING", payload=payload(letter),
                risk_exempt_eligible=False, version=0, timeout_at=None,
            )
        )
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW,
    )
    session.flush()

    kinds_present = {
        str(row[0]) for row in session.execute(text("select kind from approval_ticket"))
    }
    assert kinds_present == {"A_SPLIT", "C_BUDGET", "D_DANGER"}, f"三类没都落库：{kinds_present}"
    assert session.scalar(
        text("select count(*) from approval_ticket where timeout_at <= now()")
    ) == 0, "无超时的单被扫描谓词捞到 ⇒ A/C/D 会被自动放行"
    assert session.scalar(
        text("select count(*) from approval_ticket where timeout_at is null")
    ) == 3


def test_a_card_without_a_request_is_accepted_by_the_database_not_by_the_service(session, request_row):
    """特征化那条 MATCH SIMPLE 的残余：PG 放行"挂了卡却没挂需求单"的半拉子行，责任在建单路径。

    `models_approval.py:110-119` 当年写"由建单路径兜 + 库侧留一条特征化用例钉住现状"，
    这一条就是那一句的落地：这里**故意**用 ORM 直插，证明数据库确实不拦。
    """
    with pytest.raises(repo.ApprovalMisuse, match="没挂需求单"):
        service.create_approval(
            session, kind=KIND_BY_LETTER["A"], payload=payload("A"), task_card_id="T99"
        )
    with session.begin_nested():
        session.add(
            ApprovalTicket(
                id=uuid.uuid4(), task_card_id="T99", feature_request_id=None,
                kind=KIND_BY_LETTER["A"], state="PENDING", payload=payload("A"),
                risk_exempt_eligible=False, version=0, timeout_at=None,
            )
        )
    session.flush()
    assert session.scalar(
        text("select count(*) from approval_ticket where task_card_id='T99' and feature_request_id is null")
    ) == 1, "数据库如果拦住了，就说明外键被换成了 MATCH FULL —— 那会连合法的 A 类单一起拒掉"


# ---------------------------------------------------------------- FR-045 合并与索引

def test_same_card_same_kind_merges_into_one_row(session, card_row, request_row):
    first = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    second = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    assert (first.created, second.created, second.merged) == (True, False, True)
    assert first.ticket_id == second.ticket_id
    assert session.scalar(text("select count(*) from approval_ticket")) == 1


def test_two_pending_gates_on_one_card_are_refused_even_across_kinds(session, card_row, request_row):
    """FR-045 后半句：同一任务同时只允许一个未决卡点，**换类型也不许建第二行**，并把冲突报回去。"""
    a = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    d = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    assert d.merged is True and d.kind_conflict is True
    assert d.kind == str(a.kind), "合并回来的是那张已挂着的单，不是新来的那一类"
    assert session.scalar(text("select count(*) from approval_ticket")) == 1


def test_the_partial_unique_index_is_real_and_rejects_a_direct_duplicate(session, card_row, request_row):
    """T059：绕过应用层直插第二行同 (卡, 类) 的未决单 ⇒ 那条部分唯一索引必须拦。"""
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ApprovalTicket(
                    id=uuid.uuid4(), task_card_id="T01", feature_request_id=request_row.id,
                    kind=KIND_BY_LETTER["D"], state="PENDING", payload=payload("D"),
                    risk_exempt_eligible=False, version=0, timeout_at=None,
                )
            )
    # 反向对照：把状态换成非未决 ⇒ 索引条件不再覆盖这一行，插得进去（证明索引是部分的而不是全表唯一）。
    with session.begin_nested():
        session.add(
            ApprovalTicket(
                id=uuid.uuid4(), task_card_id="T01", feature_request_id=request_row.id,
                kind=KIND_BY_LETTER["D"], state="REJECTED", payload=payload("D"),
                risk_exempt_eligible=False, version=0, timeout_at=None,
            )
        )
    assert session.scalar(
        text("select count(*) from approval_ticket where state='REJECTED'")
    ) == 1


def test_a_request_level_lookup_ignores_card_level_tickets_of_the_same_request(
    session, card_row, request_row
):
    """作用域修正的一条：查 A 类（需求单级）未决单时，**不许**把挂在卡上的未决单捞回来。

    这条是本轮新写的"扫描谓词跳过无超时单"那次用例撞出来的真缺陷：`pending_for` 原先只按
    `feature_request_id` 筛，于是同一需求单下的 C/D 卡级单也算"该需求单的未决单" ——
    后果两种都不轻：`create_or_merge` 会把 A 类单 merge 到一张 D 类卡级单上（卡点挂错对象），
    或者因为一次捞回多行而喊"部分唯一索引没生效"，**而索引其实好得很**（那就是误报把脏数据藏起来）。
    """
    on_card = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    # 需求单级那一支：既不该 merge 到上面那张卡级单，也不该报错。
    on_request = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW,
    )
    assert on_request.merged is False and on_request.kind_conflict is False
    assert on_request.ticket_id != on_card.ticket_id, "A 类单挂到了卡级 D 单上 = 卡点作用域串了"
    assert session.scalar(text("select count(*) from approval_ticket")) == 2

    # 反向对照：同一个作用域再来一次才叫 merge（证明上面的"不 merge"不是因为压根没查）。
    again = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW,
    )
    assert (again.merged, again.ticket_id) == (True, on_request.ticket_id)


def test_a_card_level_lookup_cannot_reach_another_requests_card_of_the_same_id(session):
    """作用域修正的另一条：卡号只在需求单内唯一（`data-model.md §3` 裁决 2 的复合主键），
    所以卡级查询 MUST 两列一起比 —— 否则 R2 的 `T01` 会被当成 R1 的 `T01` merge 掉。

    这一条比上一条更靠近"跨单越权"：两张不同需求单的同名卡本来互不相干。
    两个作用域都在本用例里自建，不借用别的夹具的行（借来的话，夹具一变这条就空转）。
    """
    scopes = []
    for label in ("R1", "R2"):
        request_id = uuid.uuid4()
        session.add(
            FeatureRequest(
                id=request_id, project_key="demo", raw_input=f"两张需求单之一：{label}",
                summary_digest={"自行补全项": [], "澄清问题": []}, clarification_waived=True,
                status="SPLIT", created_at=NOW,
            )
        )
        snapshot_id = uuid.uuid4()
        session.add(
            Snapshot(id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=["T01"],
                     ref=f"local:probe-{label}-{snapshot_id}", rolled_back_at=None)
        )
        session.flush()
        session.add(
            TaskCard(
                id="T01", feature_request_id=request_id, title=f"{label} 的 T01", difficulty=2,
                skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
                artifact_floor_min_lines=12, artifact_floor_format="html",
                acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
                status="PENDING_APPROVAL", snapshot_id=snapshot_id,
            )
        )
        session.flush()
        scopes.append(request_id)

    first, second = scopes
    r1_ticket = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=first, now=NOW,
    )
    # 第二张故意用**另一类**卡点：本用例量的是"查找作用域"，而 EN-09 那条部分唯一索引
    # 不含 `feature_request_id`（见下面那条特征化用例），同 kinds 会先撞索引、把要测的东西盖住。
    r2_ticket = repo.create_or_merge(
        session, kind=KIND_BY_LETTER["B"], payload=payload("B"), risk_exempt_eligible=False,
        timeout_policy=policy("B"), task_card_id="T01", feature_request_id=second, now=NOW,
    )
    assert r2_ticket.merged is False, "R2 的 T01 merge 到了 R1 的 T01 那张单上 ⇒ 作用域只比了一列"
    assert r1_ticket.ticket_id != r2_ticket.ticket_id

    # 只给卡号不给需求单 ⇒ 当场拒，不许"猜一个作用域"。
    with pytest.raises(repo.ApprovalMisuse, match="没带 feature_request_id"):
        repo.pending_for(session, task_card_id="T01")


def test_the_pending_index_collides_across_requests_on_the_same_card_id(session, card_row, request_row):
    """**特征化用例（钉现状，不是认可）**：EN-09 那条部分唯一索引的键里没有 `feature_request_id`，
    于是两张**不同需求单**上的同名卡 `T01` 会互相顶位。

    为什么与 `data-model.md §3` 冲突：那一节写着"卡号的作用域就在这条边上 —— `(feature_request_id, id)`
    复合主键 ⇒ 同一需求单内 `T01` 唯一，**跨需求单可重名**（裁决 2）"。而 EN-09 的
    `unresolved_unique` 给的键是 `(task_card_id, kind) WHERE state='PENDING'` —— 少了那一列，
    索引就把"跨需求单可重名"的两个作用域当成了同一个：R1 的 T01 挂着未决 D 单时，
    R2 的 T01 建一张同类型卡点会被数据库直接拒。

    这条**不写成"应该成功"**：改索引键属数据模型变更，要 owner 裁（是加列进键，还是接受"卡号在
    未决队列里全局唯一"这个更强口径）。本轮按现状钉住并登记在 `evidence/phase-04.md §T059`。
    """
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id, now=NOW,
    )
    other_request = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=other_request, project_key="demo", raw_input="另一张需求单的 T01",
            summary_digest={"自行补全项": [], "澄清问题": []}, clarification_waived=True,
            status="SPLIT", created_at=NOW,
        )
    )
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=["T01"],
                 ref=f"local:probe-collide-{snapshot_id}", rolled_back_at=None)
    )
    session.flush()
    session.add(
        TaskCard(
            id="T01", feature_request_id=other_request, title="另一张需求单的 T01", difficulty=2,
            skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
            artifact_floor_min_lines=12, artifact_floor_format="html",
            acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
            status="PENDING_APPROVAL", snapshot_id=snapshot_id,
        )
    )
    session.flush()

    with pytest.raises(IntegrityError, match="uq_approval_ticket_pending_card_kind"):
        with session.begin_nested():
            session.add(
                ApprovalTicket(
                    id=uuid.uuid4(), task_card_id="T01", feature_request_id=other_request,
                    kind=KIND_BY_LETTER["D"], state="PENDING", payload=payload("D"),
                    risk_exempt_eligible=False, version=0, timeout_at=None,
                )
            )
    # 反向对照：换成**别的卡号**就插得进去 ⇒ 顶位的原因确实是卡号相同，不是"一个需求单只能一张未决单"。
    extra_card(session, card_row, "T02")
    with session.begin_nested():
        session.add(
            ApprovalTicket(
                id=uuid.uuid4(), task_card_id="T02", feature_request_id=request_row.id,
                kind=KIND_BY_LETTER["D"], state="PENDING", payload=payload("D"),
                risk_exempt_eligible=False, version=0, timeout_at=None,
            )
        )
    assert session.scalar(text("select count(*) from approval_ticket")) == 2


def test_a_mixed_timeout_bucket_sorts_without_crashing(session, card_row, request_row):
    """裁决 ② 的连带面：同一优先级档里**既有 NULL 又有真时刻**时，排序不许炸。

    为什么这条不是多余的：`sorted()` 的键里如果直接放 `timeout_at`，`None < datetime` 会
    `TypeError`。今天 §1 下 C/D 都是"无超时"、B/A 各有档，所以混不到一起 ——
    但"混不到"是**当前数值的巧合**，不是规则。这里直插一行带真时刻的 D 类单把混合形状造出来，
    它同时也是 `GET /approvals` 会不会 500 的前置条件。
    """
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["C"], payload=payload("C"), risk_exempt_eligible=False,
        timeout_policy=policy("C"), feature_request_id=request_row.id, now=NOW,
    )
    extra_card(session, card_row, "T02")
    session.add(
        ApprovalTicket(
            id=uuid.uuid4(), task_card_id="T02", feature_request_id=request_row.id,
            kind=KIND_BY_LETTER["D"], state="PENDING", payload=payload("D"),
            risk_exempt_eligible=False, version=0,
            timeout_at=NOW + dt.timedelta(minutes=5),
        )
    )
    session.flush()

    rows = repo.list_pending(session)
    assert [str(row.kind) for row in rows] == ["C_BUDGET", "D_DANGER"], (
        f"无超时的没排在同档最前：{[str(r.kind) for r in rows]}"
    )
    assert [row.timeout_at for row in rows] == [None, NOW + dt.timedelta(minutes=5)]


def test_two_pending_request_level_tickets_make_the_reader_shout(session, request_row):
    """库侧残余：A 类单的卡列是 NULL，而 PG 里 NULL 彼此不相等 ⇒ 索引管不到，只能由读侧喊。

    这一条不是给"读侧有防呆"发奖状，是把那处残余**钉成读数**：直插两行之后 `pending_for()`
    报错而不是随便返回一行 —— 后者会让"同一任务只有一个未决卡点"这条看起来一直成立。
    """
    for _ in range(2):
        with session.begin_nested():
            session.add(
                ApprovalTicket(
                    id=uuid.uuid4(), task_card_id=None, feature_request_id=request_row.id,
                    kind=KIND_BY_LETTER["A"], state="PENDING", payload=payload("A"),
                    risk_exempt_eligible=False, version=0, timeout_at=None,
                )
            )
    session.flush()
    with pytest.raises(repo.ApprovalMisuse, match="部分唯一索引"):
        repo.pending_for(session, feature_request_id=request_row.id)


# ---------------------------------------------------------------- 或签 CAS

def test_first_decisive_conclusion_applies_and_second_only_logs(session, request_row):
    """两条结论用两个不同的到达时刻，这不是随手写的：`events()` 的次序键是 `(arrived_at, id)`，
    同一时刻落进来的两行会退化成按 UUID 定序 —— 那份"到达流水"就再也不是到达顺序了。
    生产里两次 HTTP 落在同一微秒的概率可以忽略，所以这里不改读侧规则，只把这条残余钉在用例名下方，
    并登记进 Checkpoint 4 的未闭清单。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    first = resolve(session, ticket_id, "approve", 0)
    assert (first.applied, first.state, first.version, first.code) == (True, "APPROVED", 1, None)

    second = resolve(
        session, ticket_id, "reject", 0,
        channel=repo.CLI, reason_or_ask="晚到的驳回", now=NOW + dt.timedelta(minutes=5),
    )
    assert (second.applied, second.code, second.logged_as) == (False, "already_decided", "duplicate")
    assert second.status_code == 409

    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert (str(row.state), row.version) == ("APPROVED", 1), "后到的结论改写了状态 = 二次迁移"
    assert row.decided_by_principal == "owner-1" and row.decided_at == NOW
    events = event_rows(session, ticket_id)
    assert [(e.applied, str(e.verdict), str(e.channel)) for e in events] == [
        (True, "approve", "DASHBOARD"),
        (False, "reject", "CLI"),
    ]


def test_a_conclusion_carrying_the_current_version_still_cannot_re_migrate(session, request_row):
    """CAS 的两道谓词（`version` 与 `state='PENDING'`）**各自**都要有独立用例。

    这一条是变异跑出来的：把 `state = 'PENDING'` 从 WHERE 里删掉，上面那条"后到记 duplicate"
    仍然红不了 —— 因为它带的是过期版本号，两道门任意一道都能拦住它。缺的正是"版本号对得上、
    但单据已经落定"这一支：那才是"看板刷新后另一个人拿着最新版本去驳回"的真实形状。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    assert resolve(session, ticket_id, "approve", 0).applied is True

    fresh_version = session.get(ApprovalTicket, ticket_id).version
    session.expire_all()
    assert fresh_version == 1
    late = resolve(
        session, ticket_id, "reject", 1, channel=repo.CLI, reason_or_ask="版本没过期也不许二次迁移",
        now=NOW + dt.timedelta(minutes=1),
    )
    assert (late.applied, late.code, late.logged_as, late.state) == (
        False, "already_decided", "duplicate", "APPROVED"
    )
    session.expire_all()
    row = session.get(ApprovalTicket, ticket_id)
    assert (str(row.state), row.version) == ("APPROVED", 1), "版本号对得上就放行 = 一道门当两道用"
    # 两次到达 MUST 用两个不同的时刻：`events()` 的次序键是 `(arrived_at, id)`，同一时刻落进来的
    # 两行退化成按 UUID 定序 —— 反向对照 MU-Q（纯改名）第一次就把这条断言打翻过，读数是
    # `[False, True]`，不是变异生效而是本用例自带的不确定性。
    assert [e.applied for e in repo.events(session, ticket_id)] == [True, False]


def test_a_decided_ticket_cannot_be_re_migrated_even_with_a_matching_version(session, request_row):
    """FR-040 的"已落定就不许再迁"：**版本号对得上也不行**。

    这条是变异 MU-A 逼出来的。跑之前只有"版本过期"与"后到 duplicate"两支，把 CAS 的
    `state = 'PENDING'` 从 WHERE 里删掉**两支都照样绿** —— 那说明"态"这道谓词没人量过。
    补上本条之后 MU-A 仍然绿，探针读数给了原因（`evidence/phase-04.md §MU-A`）：
    同 session 里裸 SQL 改态之后 `session.get()` 会**把身份映射刷新成新值**，于是
    `resolve()` 的前置检查（`state != PENDING → already_decided`）先拦下，走不到 WHERE。
    所以本条量的是"**前置检查 + 态**"这条真实行为路径，它**不**为 WHERE 里那道谓词作证；
    那道谓词在当前所有写路径下不可单独观测（每个写者都同时推 `version`），已登记待裁。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"
    session.execute(text("update approval_ticket set state = 'REJECTED' where id = :i"), {"i": ticket_id})
    session.flush()

    outcome = resolve(session, ticket_id, "approve", 0)
    assert outcome.applied is False, "已经落定的单不许被一份晚到的结论二次迁移"
    assert (outcome.code, outcome.state) == ("already_decided", "REJECTED")

    session.expire_all()
    row = session.get(ApprovalTicket, ticket_id)
    assert (str(row.state), row.version) == ("REJECTED", 0), "落定的单被晚到的结论改写=二次迁移"


def test_stale_version_is_reachable_and_keeps_the_ticket_pending(session, request_row):
    """`stale_version` 那一支不是死代码：版本号被别的写者抬过而单据仍是待决时走它。

    今天生产路径造不出这个形状（`need_info` 不 bump 版本，见 `repository` 模块头第 2 条），
    所以这里**直接抬版本**把分支打开 —— 防御分支零用例是这轮反复在补的那类漏口。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    session.execute(
        text("update approval_ticket set version = 7 where id=:i"), {"i": ticket_id}
    )
    session.flush()
    outcome = resolve(session, ticket_id, "approve", 0)
    assert (outcome.applied, outcome.code, outcome.logged_as) == (False, "stale_version", None)
    assert outcome.status_code == 409
    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert str(row.state) == "PENDING" and row.version == 7
    assert [e.applied for e in event_rows(session, ticket_id)] == [False], "到达要有流水，但不许算生效"


def test_principal_outside_the_approver_set_is_refused_without_writing_anything(session, request_row):
    """403 那一支：非审批者既不迁移也不留流水（越权的尝试归审计面 EN-14，不归 EN-10）。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    outcome = resolve(session, ticket_id, "approve", 0, principal="someone-else")
    assert (outcome.applied, outcome.code) == (False, "principal_not_allowed")
    assert outcome.status_code == 403
    assert session.get(ApprovalTicket, ticket_id).version == 0
    assert event_rows(session, ticket_id) == []


def test_an_empty_approver_set_denies_everyone(session, request_row):
    """审批者集合为空 ⇒ 谁都拒。把它读成"不限制"就是一次漏配置变成一道敞开的门。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    outcome = repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD,
        principal="owner-1", approvers=frozenset(),
    )
    assert (outcome.applied, outcome.code) == (False, "principal_not_allowed")


def test_need_info_leaves_state_version_and_payload_untouched(session, request_row):
    """FR-040 三：追问不结束待决、不动 CAS 锁、原五要素一字不改（三条各一个断言）。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    before = dict(session.get(ApprovalTicket, ticket_id).payload)
    asked = resolve(session, ticket_id, "need_info", 0, reason_or_ask="联系方式用邮箱还是表单？")
    assert (asked.applied, asked.state, asked.version, asked.code) == (True, "PENDING", 0, None)
    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert str(row.state) == "PENDING" and row.version == 0
    assert dict(row.payload) == before, "追问把五要素改写了 ⇒ 审批者看到的就不是原来那份"
    events = event_rows(session, ticket_id)
    assert [(e.applied, str(e.verdict), e.reason_text) for e in events] == [
        (True, "need_info", "联系方式用邮箱还是表单？")
    ]
    # 追问之后再批仍然生效：证明追问没把 CAS 锁死（模块头那条"version 是决定性结论的锁"）。
    later = resolve(session, ticket_id, "approve", 0)
    assert (later.applied, later.state) == (True, "APPROVED")


def test_resolve_on_a_missing_ticket_reports_not_found(session):
    outcome = resolve(session, uuid.uuid4(), "approve", 0)
    assert (outcome.applied, outcome.code, outcome.status_code) == (False, "ticket_not_found", 404)


def test_a_verdict_outside_the_domain_is_a_caller_error_not_a_business_refusal(session, request_row):
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    with pytest.raises(repo.ApprovalMisuse, match="resolution_verdict"):
        resolve(session, ticket_id, "LGTM", 0)


def test_a_human_caller_cannot_submit_the_auto_channel(session, request_row):
    """裁决 ④ 的反面：`AUTO` 进了 `resolution_channel` 域，但**人不许用它提交结论**。

    这一条是加值之后必须同时存在的另一半：只加值不划白名单，任何拿到 `ticket_id` 与审批身份的人
    都能把自己的结论写成"规则表自动批的"，审计面就从"谁批的"退化成"自称谁批的"。
    断的是"既不迁移、也不留流水"——留一行 AUTO 就已经把假事实写进来了。
    """
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    outcome = repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.AUTO,
        principal="owner-1", approvers=APPROVERS, now=NOW,
    )
    assert (outcome.applied, outcome.code) == (False, "channel_not_allowed")
    assert outcome.status_code == 422
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"
    assert repo.events(session, ticket_id) == [], "被拒的提交连一行流水都不该留下"

    # 反向对照：同一张单换成人可提交的通道就过（证明拒的是通道，不是这张单）。
    assert resolve(session, ticket_id, "approve", 0).applied is True


def test_the_pg_enum_labels_are_exactly_the_documented_domain(session):
    """迁移 0006 之后，库里的类型取值必须与 `enums.py` 一字不差（多一个少一个都算事故）。

    与 `test_schema_invariants.py::test_task_card_state_type_matches_the_doc_enumeration` 同族，
    但那条管 `task_card_state`：`resolution_channel` 是本轮**刚刚被扩宽**的那个域。
    `alembic check` 看不见枚举取值的变化（它不对 PG 原生枚举做 autogenerate），
    所以"改了 enums.py 没发迁移"与"发了迁移没改 enums.py"这两种漂移都得由这一条抓。两个方向都断。
    """
    labels = {
        row[0]
        for row in session.execute(
            text(
                "select e.enumlabel from pg_type t join pg_enum e on e.enumtypid = t.oid "
                "where t.typname = 'resolution_channel_type' order by e.enumsortorder"
            )
        )
    }
    declared = set(values_of("resolution_channel"))
    assert labels - declared == set(), f"库里有而域里没有：{sorted(labels - declared)}"
    assert declared - labels == set(), f"域里登记了而库里没有（缺迁移）：{sorted(declared - labels)}"


def test_pending_tickets_sort_c_d_e_over_b_over_a(session, card_row, request_row):
    """FR-044 的读侧次序：`C/D/E > B > A`（E 是 Phase 17 的第五类，与 C/D 同档）。

    C / D / E 同档且都"无超时"，三者之间的先后由 `str(id)`（UUID）定序 —— 那是不可预测的，
    所以这里**不许**把名字写成一个精确列表（上一版断 `kinds[:2] == sorted(kinds[:2])`，
    等于把"按字母序"当成规则，它这轮碰巧绿，换一个 UUID 就会红）。断的是关系。
    """
    for letter in ("A", "B", "C", "D", "E"):
        repo.create_or_merge(
            session, kind=KIND_BY_LETTER[letter], payload=payload(letter), risk_exempt_eligible=False,
            timeout_policy=policy(letter),
            task_card_id=None if letter == "A" else extra_card(session, card_row, f"T0{ord(letter) - 63}").id,
            feature_request_id=request_row.id, now=NOW,
        )
    rows = repo.list_pending(session)
    kinds = [str(row.kind) for row in rows]
    priorities = [repo.priority_key(kind) for kind in kinds]
    assert priorities == [0, 0, 0, 1, 2], (
        f"优先级序列不是 C/D/E→B→A：{list(zip(kinds, priorities))}"
    )
    assert set(kinds[:3]) == {"C_BUDGET", "D_DANGER", "E_REVIEW_UNCERTAIN"}
    assert kinds[3:] == ["B_ACCEPTANCE", "A_SPLIT"]
    assert [(str(r.kind), r.timeout_at) for r in rows[3:]] == [
        ("B_ACCEPTANCE", NOW + dt.timedelta(hours=2)),
        ("A_SPLIT", None),
    ]
    assert set(kinds) == set(values_of("approval_kind"))
    assert repo.priority_key(KIND_BY_LETTER["C"]) < repo.priority_key(KIND_BY_LETTER["A"])


def test_the_priority_map_refuses_an_unregistered_kind():
    with pytest.raises(repo.ApprovalMisuse, match="不在优先级表里"):
        repo.priority_key("E_FUTURE")


# ---------------------------------------------------------------- 免确认与规则表

def test_auto_pass_signs_the_ticket_with_the_rule_name_not_a_human(session, request_row):
    """FR-046 / FR-047：规则表判定直接批准 A 类单，署名是 `risk_rule:<工具>:<岗>`。"""
    facts = risk_engine.RequestFacts(
        agent_id="workbuddy", role_id="architect", skill_tags=frozenset({"planning"}),
        fs_mutation=False, paid_external_call=False, estimated_tokens=1000,
    )
    ticket = service.create_approval(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"),
        feature_request_id=request_row.id, facts=facts, now=NOW,
    )
    assert (ticket.exempt, ticket.auto_passed, ticket.state) == (True, True, "APPROVED")
    row = session.get(ApprovalTicket, ticket.ticket_id)
    assert row.decided_by_principal == "risk_rule:workbuddy:architect"
    assert row.version == 1 and row.risk_exempt_eligible is True
    # 裁决 ④（owner 2026-10-13）：免确认**要**写 EN-10 流水，通道 `AUTO`、verdict approve、applied true，
    # `reason_text` 带规则名（EN-10 没有 principal 列，不写就只知道"是自动的"而不知道"是哪条规则"）。
    events = event_rows(session, ticket.ticket_id)
    assert [(e.applied, str(e.verdict), str(e.channel), e.reason_text) for e in events] == [
        (True, "approve", "AUTO", "risk_rule:workbuddy:architect")
    ]


def test_a_non_A_kind_cannot_be_auto_passed_even_with_eligible_facts(session, card_row, request_row):
    """FR-048 的写侧一道：拿 D 类单走免确认 ⇒ 仓储直接拒，不看上游怎么判。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=True,
        timeout_policy=policy("D"), task_card_id="T01", feature_request_id=request_row.id,
        now=NOW, ticket_id=ticket_id,
    )
    with pytest.raises(repo.ApprovalMisuse, match="FR-048"):
        repo.apply_auto_pass(session, ticket_id=ticket_id, decided_by="risk_rule:workbuddy:architect")
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"
    assert session.get(ApprovalTicket, ticket_id).version == 0
    # 拒了的免确认不许留下一行 AUTO：那一行本身就是"这条规则批过这张单"的陈述。
    assert repo.events(session, ticket_id) == []


def test_missing_signature_on_auto_pass_is_refused(session, request_row):
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=True,
        timeout_policy=policy("A"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    with pytest.raises(repo.ApprovalMisuse, match="decided_by"):
        repo.apply_auto_pass(session, ticket_id=ticket_id, decided_by=None)


def test_risk_rules_load_into_en_11_and_the_db_check_is_real(session):
    """T060：清单灌进 EN-11；库里那条 `auto_pass_needs_all_three` 不是纸面约束。"""
    count = risk_engine.persist_rules(session)
    assert count == len(risk_engine.load_rules()) == session.scalar(select(func.count()).select_from(RiskRule))
    assert session.scalar(
        text("select count(*) from risk_rule where auto_pass and not no_paid_external_call")
    ) == 0
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                RiskRule(
                    match_agent_id="x", match_role_id="y", match_capability_tags=["z"],
                    no_fs_mutation=True, no_paid_external_call=False,
                    budget_within_threshold=True, auto_pass=True,
                )
            )


# ---------------------------------------------------------------- 超时收敛（FR-043 的 B 类支）

def test_timeout_convergence_refuses_until_the_rounds_are_full(session, card_row, request_row):
    """T063 第四条的仓储侧：B 类未跑满轮次时超时**不改状态**；跑满才进收敛态。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["B"], payload=payload("B"), risk_exempt_eligible=False,
        timeout_policy=policy("B"), task_card_id="T01", feature_request_id=request_row.id,
        now=NOW, ticket_id=ticket_id,
    )
    assert repo.converge_on_timeout(
        session, ticket_id=ticket_id, review_round=2, policy=policy("B")
    ) is None
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"

    outcome = repo.converge_on_timeout(
        session, ticket_id=ticket_id, review_round=3, policy=policy("B"), now=NOW
    )
    assert (outcome.applied, outcome.state) == (True, "TIMED_OUT_CONVERGED")
    assert str(session.get(ApprovalTicket, ticket_id).state) == "TIMED_OUT_CONVERGED"


def test_timeout_convergence_cannot_be_borrowed_for_A_C_D(session, request_row):
    """拿 B 类策略去收敛一张 D 类单 ⇒ 形状错位当场报错（结构约束不许被数值顶掉）。"""
    ticket_id = uuid.uuid4()
    repo.create_or_merge(
        session, kind=KIND_BY_LETTER["D"], payload=payload("D"), risk_exempt_eligible=False,
        timeout_policy=policy("D"), feature_request_id=request_row.id, now=NOW, ticket_id=ticket_id,
    )
    with pytest.raises(repo.ApprovalMisuse, match="超时策略属于"):
        repo.converge_on_timeout(
            session, ticket_id=ticket_id, review_round=9, policy=policy("B")
        )


# ---------------------------------------------------------------- checkpoint schema（T050）

def test_the_migrated_schema_matches_what_the_library_declares(engine):
    """迁移 0005 建的东西必须与依赖那份列表逐一对得上（列与索引两个面）。

    权威在 `langgraph...base.MIGRATIONS`：依赖一升级、列表多一条而旧库没重放 0005，
    这里就会红 —— 那正是"跑起来才发现缺列"的替代物。
    """
    tables, indexes = library_declared_schema()
    with engine.connect() as conn:
        actual_columns: dict[str, set[str]] = {}
        for table, column in conn.execute(
            text("select table_name, column_name from information_schema.columns "
                 "where table_name like 'checkpoint%'")
        ):
            actual_columns.setdefault(table, set()).add(column)
        actual_indexes = {
            (table, name)
            for table, name in conn.execute(
                text("select tablename, indexname from pg_indexes where tablename like 'checkpoint%'")
            )
        }
    assert set(actual_columns) == set(tables), "表集合与依赖声明的不一致"
    for table, expected in tables.items():
        missing = expected - actual_columns[table]
        assert not missing, f"{table} 缺列 {sorted(missing)} ⇒ 旧库需要跑 ensure_checkpoint_schema()"
    missing_indexes = indexes - actual_indexes
    assert not missing_indexes, f"缺索引 {sorted(missing_indexes)}"


def test_the_checkpointer_round_trips_a_checkpoint_into_those_tables(engine):
    """真实 PG 上写一次 checkpoint 再读回来：列与索引不只是"存在"，而是"够用"。

    断的是**值**不是"读到了对象"：`put()` 把非原始类型的 channel 值拆进 `checkpoint_blobs`，
    读侧按 `channel_versions` 去 join。少了任何一列/索引，这里会以"值变空"的形态红 ——
    那比 `get_tuple() is not None` 更接近生产里真正会坏的东西。

    checkpoint 骨架取自依赖自己的 `empty_checkpoint()`：手抄那份 dict 等于把我的理解当权威，
    格式版本一升（`v` 现在是 4，还有 `updated_channels` 这个键）我的用例只会红得莫名其妙。
    """
    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    from acp.persistence.checkpointer import conninfo

    connection = psycopg.connect(conninfo(), autocommit=True)
    try:
        saver = PostgresSaver(connection)
        thread = "roundtrip-" + uuid.uuid4().hex[:8]
        config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        version = "00000000000000000000000000000001.0.1"
        drafts = [{"card_id": "T01", "title": "首屏"}]
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"drafts": drafts}
        checkpoint["channel_versions"] = {"drafts": version}
        saved = saver.put(
            config,
            checkpoint,
            {"source": "loop", "step": 1, "writes": None, "parents": {}},
            {"drafts": version},
        )
        assert saved["configurable"]["checkpoint_id"] == checkpoint["id"]
        got = saver.get_tuple(saved)
        assert got is not None and got.checkpoint["id"] == checkpoint["id"]
        assert got.checkpoint["channel_values"] == {"drafts": drafts}, "blob 没读回来 ⇒ 列或索引不够用"
        with connection.cursor() as cur:
            cur.execute("select count(*) from checkpoint_blobs where thread_id=%s", (thread,))
            assert cur.fetchone()[0] == 1, "blob 行没落表，那读回来的值是从哪来的？"
            cur.execute("delete from checkpoint_writes where thread_id=%s", (thread,))
            cur.execute("delete from checkpoint_blobs where thread_id=%s", (thread,))
            cur.execute("delete from checkpoints where thread_id=%s", (thread,))
    finally:
        connection.close()
