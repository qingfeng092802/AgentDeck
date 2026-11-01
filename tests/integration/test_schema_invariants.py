"""Phase 1.3 的库侧不变量（T023 ~ T025 的落地判据）。

**这一档不在 fast 集里**（`tests/conftest.py` 的分档口径：fast = `tests/guards` + `tests/contract`，
明写"不碰 PG 与外部凭据"）。把需要 PG 的用例放进 `tests/contract/` 的后果是：
Docker 一停，先跑集就跟着红——那是把环境抖动算成代码缺陷（1.2 收口时 C2 就这么翻过一次脸）。
所以这里单独一档，缺 `ACP_DB_DSN` 时**如实 skip 并在收口读数里写明 skip 条数**，不写成通过。

用例分两类，用途不同，都在文件名/用例名里点名：
- `*_is_rejected` / `*_must_be_*`：文档指派给数据库的不变量，插坏数据 MUST 报错；
- `*_yet` / `*_does_not_*`：**特征化用例**，钉住"文档把这条规则指派给了别处"这一事实。
  它们绿不代表规则不存在，只代表强制点不在 DB；将来把规则下沉到 DB 时，这条用例会红，
  逼着改的人回来同步 `data-model.md §4`——这正是它存在的理由。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.persistence import models_approval, models_core, models_registry, models_review
from acp.persistence.checkpointer import library_declared_schema
from acp.persistence.enums import values_of
from acp.persistence.states import data_model_default, states_declared_in_data_model
from conftest import CHILD_STDIO_ENV

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（不在 fast 档，缺凭据时 skip 而不是假绿）",
)

NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
REPO = Path(__file__).resolve().parents[2]
FeatureRequest = models_core.FeatureRequest
TaskCard = models_core.TaskCard
AgentUnit = models_core.AgentUnit
CapabilityManifest = models_core.CapabilityManifest
Artifact = models_core.Artifact
AcceptanceMap = models_approval.AcceptanceMap
ReviewVerdict = models_approval.ReviewVerdict
ApprovalTicket = models_approval.ApprovalTicket
ResolutionEvent = models_approval.ResolutionEvent
RiskRule = models_approval.RiskRule
RoleSpec = models_registry.RoleSpec
Snapshot = models_registry.Snapshot
CostRecord = models_registry.CostRecord
EventLog = models_registry.EventLog
MeetingNote = models_registry.MeetingNote
AutonomyState = models_registry.AutonomyState
Deliverable = models_registry.Deliverable
ReviewReport = models_review.ReviewReport


def declared_entities() -> set[str]:
    """实体清单的唯一口径 = `data-model.md §1` 里各小节标题行的表名（`### EN-NN <table>`）。

    判据跟着那张表走，加实体时不必回来改这句自述（§1 的复算命令同一条理由）。
    """
    text_ = data_model_default().read_text(encoding="utf-8")
    return set(re.findall(r"^### EN-\d+ `([a-z_]+)`", text_, re.MULTILINE))


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    # 不 dispose 的话，池里那条 psycopg 连接会活到解释器退出才被 GC 掉，
    # 于是 `Connection.__del__` 抛 ResourceWarning、被 pytest 的 unraisable 钩子记成
    # 一条 warning —— 而且只在 GC 落在 unconfigure 窗口内时才出现（实测四趟出一次），
    # 是个按 GC 时序抖的假信号。显式归还连接后它恒为 0 warning。
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema_must_be_applied(engine):
    """迁移没真跑 = 本文件全红，而不是全 skip。

    "没建表所以没测"读起来像通过，这正是要防的假绿；红会把"先跑 alembic upgrade head"
    这句话顶到眼前。
    """
    present = set(inspect(engine).get_table_names())
    missing = declared_entities() - present
    if missing:
        pytest.fail(
            f"缺表 {sorted(missing)} ⇒ 先跑 `alembic upgrade head`（真跑，不是只写脚本）"
        )


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()  # 用例里的行一律不落库，库保持可重复跑


def add(session, obj):
    with session.begin_nested():
        session.add(obj)
        session.flush()
    return obj


def try_add(session, obj) -> bool:
    """True = 插进去了；False = 数据库把这次写入拦下。

    拦下的形态不止一种，都算"被拦住"而不是分开断言：约束违反是 `IntegrityError`，
    而原生枚举收到声明外的字面量是 `DataError`（PG 22P02）。分开写会得到
    "用例报错而不是变红"的噪声，归因反而更难。
    """
    try:
        with session.begin_nested():
            session.add(obj)
            session.flush()
    except SQLAlchemyError:
        return False
    return True


def seeded(session):
    """一张最小可用的合法世界：需求单 + 存档点 + 岗位 + 工具 + 一张 RUNNING 的卡。"""
    request = add(
        session,
        FeatureRequest(
            id=uuid.uuid4(),
            project_key="demo",
            raw_input="做个介绍公司产品的静态网站",
            summary_digest={"自行补全项": ["配色"]},
            clarification_waived=False,
            status="INTAKE",
            created_at=NOW,
        ),
    )
    snapshot = add(
        session,
        Snapshot(
            id=uuid.uuid4(),
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T01"],
            ref="local:snapshot-1",
            rolled_back_at=None,
        ),
    )
    role = add(
        session,
        RoleSpec(
            role_id="frontend",
            name="前端",
            charter="按验收标准产出页面",
            read_only=False,
            builtin=True,
            mounted_agents=["qoder"],
        ),
    )
    reviewer_role = add(
        session,
        RoleSpec(
            role_id="reviewer",
            name="审查员",
            charter="逐条比对验收标准，只读",
            read_only=True,
            builtin=True,
            mounted_agents=["workbuddy"],
        ),
    )
    unit = add(
        session,
        AgentUnit(
            agent_id="qoder",
            form="SDK",
            tier=3,
            cost_band="MID",
            max_parallel=2,
            current_load=0,
            historical_success_rate=None,
            success_rate_sample=0,
            workspace_dir="qoder-workspace",
            enabled=True,
        ),
    )
    other = add(
        session,
        AgentUnit(
            agent_id="workbuddy",
            form="IN_PLATFORM",
            tier=2,
            cost_band="CHEAP",
            max_parallel=1,
            current_load=0,
            historical_success_rate=None,
            success_rate_sample=0,
            workspace_dir="workbuddy-workspace",
            enabled=True,
        ),
    )
    card = task_card(session, request, snapshot, "T01")
    return dict(
        request=request,
        snapshot=snapshot,
        role=role,
        reviewer_role=reviewer_role,
        unit=unit,
        other=other,
        card=card,
    )


def card_kwargs(request_id, snapshot_id, card_id="T01", **overrides) -> dict:
    """一张合法卡的最小字段集。单独成函数是为了让**反面用例**也能拿到对象而不是先落库。"""
    fields = dict(
        id=card_id,
        feature_request_id=request_id,
        title="首页",
        difficulty=2,
        skill_tags=["html"],
        depends_on=[],
        executor_agent_id="qoder",
        executor_role_id="frontend",
        artifact_floor_min_bytes=200,
        artifact_floor_min_lines=10,
        artifact_floor_format="html",
        token_budget=Decimal("1000"),
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="RUNNING",
        snapshot_id=snapshot_id,
    )
    fields.update(overrides)
    return fields


def task_card(session, request, snapshot, card_id="T01", **overrides):
    return add(session, TaskCard(**card_kwargs(request.id, snapshot.id, card_id, **overrides)))


def test_declared_entities_all_exist(session):
    """库里既不许少一张实体表，也不许多一张没人登记的表 —— 两个方向各一条断言。

    **Phase 4 的一处必要区分**（不是放宽这条门禁）：checkpoint 那四张表（D-9 / T050）不是本仓库的
    实体，它们的结构权威是 `langgraph.checkpoint.postgres.base.MIGRATIONS`，落库路径是迁移
    `0005_t050_checkpoints`。所以它们的"登记载体"是那份列表，取数也**从列表解析**而不是在这里
    手敲四个表名 —— 手敲的话依赖一升级、列表里多一张表，这条判据就会红在错的地方。
    剩下两条断言管住"这不是一个可以被随手塞表的口袋"：两套集合不许重叠，
    而 checkpoint 那一侧必须真的被解析出来了（非空）。
    """
    present = set(inspect(session.get_bind()).get_table_names()) - {"alembic_version"}
    declared = declared_entities()
    checkpoint, _indexes = library_declared_schema()
    reserved = set(checkpoint)
    assert reserved, "解析不出依赖自己声明的 checkpoint 表 ⇒ 这条判据的分母是空的，红在这里而不是放行"
    assert declared - present == set()
    assert present - declared - reserved == set(), (
        f"库里多出既非实体、也非 checkpoint schema 的表：{sorted(present - declared - reserved)}"
    )
    assert reserved & declared == set(), "实体表与 checkpoint 表不许重名"


def test_artifact_format_type_carries_the_live_domain(session):
    """库侧对照：`artifact_format_type` 的标签**集合与顺序** = 协议域（迁移 `0010` 的落点）。

    为什么这条必须走真库、不能在 contract 档里断言：`pg_enum()` 回答的是"模型以为库里长什么样"
    （它按 `refs.py` 现算），而这条要回答的是"**库里此刻真的**长什么样"。两者只在迁移漏跑时
    分岔 —— 而那正是最该被抓到的一刻：代码认 `js`、库里不认，写入会在
    `INSERT … format='js'` 上抛 `invalid input value for enum`，且现场是一条**已经跑完的卡**
    在最后一步落不了账。

    顺序也断言：`ALTER TYPE … ADD VALUE` 只能追加到末尾，`js` MUST 在最后一位。
    若哪天有人用 `BEFORE`/`AFTER` 重排了标签，`pg_enum(...).enums` 与 `values_of(...)` 会错位，
    而错位的表现是"格式对不对"这类判定开始偶发地判错 —— 比整条不认更难查。
    """
    labels = tuple(
        row[0]
        for row in session.execute(
            text(
                "select e.enumlabel from pg_type t join pg_enum e on e.enumtypid = t.oid "
                "join pg_namespace n on n.oid = t.typnamespace "
                "where t.typname = 'artifact_format_type' and n.nspname = current_schema() "
                "order by e.enumsortorder"
            )
        )
    )
    assert labels == values_of("artifact_format")
    assert labels[-1] == "js", "js 不在末尾 ⇒ 有人重排过标签，`values_of()` 的顺序对不上了"


def test_both_columns_accept_js(session):
    """`js` 要能在**两列**上真的写进去（EN-02 的下限 + EN-06 的实测）。

    只验类型标签不验列，会漏掉一种坏法：类型加了值、但某一列的 CHECK 或生成列还是旧口径。
    这里不建整单，只读 `information_schema` 的 `udt_name` —— 两列 MUST 都指向同一个类型，
    而那个类型里 MUST 有 `js`。
    """
    rows = session.execute(
        text(
            "select table_name, column_name, udt_name from information_schema.columns "
            "where (table_name = 'task_card' and column_name = 'artifact_floor_format') "
            "   or (table_name = 'artifact' and column_name = 'format')"
        )
    ).all()
    assert len(rows) == 2, rows
    for table, column, udt in rows:
        assert udt == "artifact_format_type", f"{table}.{column} 的底层类型是 {udt}，不是 artifact_format_type"


def test_task_card_state_type_matches_the_doc_enumeration(session):
    """T026 的库侧对照：PG 枚举标签集合 = `data-model.md §2` 的状态全集（差集双向为空）。"""
    labels = {
        row[0]
        for row in session.execute(
            text(
                "select e.enumlabel from pg_type t join pg_enum e on e.enumtypid = t.oid "
                "where t.typname = 'task_card_state_type' order by e.enumsortorder"
            )
        )
    }
    declared = states_declared_in_data_model(data_model_default())
    assert labels - declared == set()
    assert declared - labels == set()


def test_artifact_floor_must_be_positive(session):
    world = seeded(session)
    for bad in (0, -1):
        assert not try_add(
            session,
            TaskCard(
                id=f"T9{bad}",
                feature_request_id=world["request"].id,
                title="坏下限",
                difficulty=1,
                skill_tags=["md"],
                depends_on=[],
                executor_agent_id="qoder",
                executor_role_id="frontend",
                artifact_floor_min_bytes=bad,
                artifact_floor_min_lines=1,
                artifact_floor_format="md",
                token_budget=Decimal("1"),
                acceptance_criteria=["x"],
                handoff_count=0,
                review_round=0,
                status="DRAFT",
                snapshot_id=world["snapshot"].id,
            ),
        )


def test_handoff_count_limit_is_one(session):
    """FR-027 换人上限 1：`handoff_count = 1` 可插，`= 2` 被数据库拦下。"""
    world = seeded(session)
    rid, sid = world["request"].id, world["snapshot"].id
    assert try_add(session, TaskCard(**card_kwargs(rid, sid, "T02", handoff_count=1))) is True
    assert try_add(session, TaskCard(**card_kwargs(rid, sid, "T03", handoff_count=2))) is False


def test_success_rate_needs_a_nonzero_sample(session):
    """EN-03："无战绩"与"总是失败"必须可区分 ⇒ 分母为 0 时比率只能是 NULL。"""
    world = seeded(session)
    bad = dict(
        agent_id="unit-bad",
        form="API",
        tier=1,
        cost_band="CHEAP",
        max_parallel=1,
        current_load=0,
        historical_success_rate=Decimal("0.0"),
        success_rate_sample=0,
        workspace_dir="unit-bad-workspace",
        enabled=True,
    )
    assert try_add(session, AgentUnit(**bad)) is False
    assert try_add(session, AgentUnit(**{**bad, "agent_id": "unit-ok", "historical_success_rate": None, "workspace_dir": "unit-ok-workspace"})) is True


def test_review_verdict_cannot_bring_extra_criteria(session):
    world = seeded(session)
    def verdict(extra: bool) -> ReviewVerdict:
        return ReviewVerdict(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            task_card_id=world["card"].id,
            reviewer_agent_id="workbuddy",
            verdict="APPROVE",
            item_findings=[{"acceptance_item": "首屏有标题", "pass": True, "note": ""}],
            extra_criteria_used=extra,
        )

    assert try_add(session, verdict(True)) is False
    assert try_add(session, verdict(False)) is True


def test_auto_pass_needs_all_three_conditions(session):
    def rule(name: str, **flags) -> RiskRule:
        base = dict(
            match_agent_id="qoder",
            match_role_id="frontend",
            match_capability_tags=["html"],
            no_fs_mutation=True,
            no_paid_external_call=True,
            budget_within_threshold=True,
            auto_pass=True,
        )
        base.update(flags)
        return RiskRule(**base)

    assert try_add(session, rule("partial", no_fs_mutation=False)) is False
    assert try_add(session, rule("full")) is True
    # 三条不全但 auto_pass=false 是合法的（免确认路径只有一条，但规则行本身可以只是"匹配"）。
    assert try_add(session, rule("no-auto", auto_pass=False, budget_within_threshold=False)) is True


def test_only_one_pending_ticket_per_card_and_kind(session):
    world = seeded(session)
    def ticket(state: str) -> ApprovalTicket:
        return ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=world["card"].id,
            feature_request_id=world["request"].id,
            kind="B_ACCEPTANCE",
            state=state,
            payload={
                "待批内容": "x",
                "上下文摘要": "y",
                "建议结论": "批准",
                "影响面": "z",
                "超时策略": "收敛",
            },
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW + dt.timedelta(minutes=30),
        )

    assert try_add(session, ticket("PENDING")) is True
    assert try_add(session, ticket("PENDING")) is False  # FR-045 未决态至多一张
    assert try_add(session, ticket("APPROVED")) is True  # 已决的不占未决位


def test_pending_dedup_does_not_cover_feature_request_tickets(session):
    """特征化用例：§EN-09 给的键是 (task_card_id, kind)，而 A 类单 `task_card_id` 为 NULL。

    PG 里 NULL 互不相等 ⇒ 同一需求单可以堆两张 PENDING 的 A 类单，**文档给的键就是这个行为**。
    本用例钉住现状，不是认可它：要补第二个索引属改 `data-model.md`，改完这条会红。
    """
    world = seeded(session)
    def a_ticket() -> ApprovalTicket:
        return ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=None,
            feature_request_id=world["request"].id,
            kind="A_SPLIT",
            state="PENDING",
            payload={"待批内容": "拆单"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        )

    assert try_add(session, a_ticket()) is True
    assert try_add(session, a_ticket()) is True


def test_cost_record_dedup_on_the_documented_four_columns(session):
    world = seeded(session)
    def record(usage_kind: str) -> CostRecord:
        return CostRecord(
            id=uuid.uuid4(),
            occurred_at=NOW,
            project_key="demo",
            task_card_id=world["card"].id,
            agent_id="qoder",
            role_id="frontend",
            usage_kind=usage_kind,
            quantity=Decimal("10"),
            unit="token",
            cost_amount=Decimal("0.01"),
            currency="CNY",
            billing_basis="TOKEN",
        )

    assert try_add(session, record("INPUT")) is True
    assert try_add(session, record("INPUT")) is False
    assert try_add(session, record("OUTPUT")) is True


def test_second_deliverable_for_one_request_is_rejected(session):
    world = seeded(session)
    def package() -> Deliverable:
        return Deliverable(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            manifest=[{"task_card_id": "T01", "rel_path": "qoder-workspace/index.html"}],
            entry_path="qoder-workspace/index.html",
            open_issues=[],
            assembled_by_agent_id="qoder",
            created_at=NOW,
        )

    assert try_add(session, package()) is True
    assert try_add(session, package()) is False  # FR-036"唯一一份"


def test_undeclared_enum_value_is_rejected(session):
    world = seeded(session)
    assert try_add(
        session,
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            task_card_id=world["card"].id,
            writer_agent_id="qoder",
            rel_path="index.html",
            bytes=300,
            lines=12,
            format="swift",
            self_reported_status="success",
            created_at=NOW,
        ),
    ) is False


def test_artifact_validated_status_is_null_until_validation(session):
    """EN-06 的 `validated_status` 可空是有意的：覆写发生在 `ValidateArtifact`，先填 `valid` 就是自评。"""
    world = seeded(session)
    assert try_add(
        session,
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            task_card_id=world["card"].id,
            writer_agent_id="qoder",
            rel_path="index.html",
            bytes=300,
            lines=12,
            format="html",
            self_reported_status="success",
            validated_status=None,
            created_at=NOW,
        ),
    ) is True


def test_artifact_composite_fk_rejects_mismatched_pair(session):
    """裁决 2 的正面对照：产物挂的卡必须与它声明的需求单**成对**存在。

    单列外键时代这没得测（卡 id 全局唯一）；改成复合主键后，"卡存在、但不属于这张需求单"
    是一种新的、必须被拦下的畸形行 —— 拿别人的卡号配自己的需求单。
    """
    world = seeded(session)
    other_request = add(
        session,
        FeatureRequest(
            id=uuid.uuid4(),
            project_key="demo2",
            raw_input="另一个需求",
            summary_digest={},
            clarification_waived=False,
            status="INTAKE",
            created_at=NOW,
        ),
    )
    assert try_add(
        session,
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=other_request.id,  # 需求单换了
            task_card_id=world["card"].id,  # 卡还是原来那张 ⇒ 对不上
            writer_agent_id="qoder",
            rel_path="index.html",
            bytes=300,
            lines=12,
            format="html",
            self_reported_status="success",
            created_at=NOW,
        ),
    ) is False


def test_two_requests_can_share_a_card_id(session):
    """裁决 2 要买到的东西：`T01` 在两张需求单下各自存在 ⇒ 复合主键放行。"""
    world = seeded(session)
    other_request = add(
        session,
        FeatureRequest(
            id=uuid.uuid4(),
            project_key="demo2",
            raw_input="另一个需求",
            summary_digest={},
            clarification_waived=False,
            status="INTAKE",
            created_at=NOW,
        ),
    )
    assert (
        try_add(
            session,
            TaskCard(
                **card_kwargs(
                    other_request.id, world["snapshot"].id, "T01"
                )
            ),
        )
        is True
    )


def test_card_id_is_unique_within_one_request(session):
    """裁决 2 的另一半：同一需求单内 `T01` 仍不可重复（这才是"需求单内唯一"的落点）。

    `expunge_all()` 不是装饰：不清 identity map 时 SQLAlchemy 会先在内存里报"同一主键两个对象"
    （实测会顺带吐一条 SAWarning），那条**不是**数据库主键拦截 ⇒ `is False` 成立但原因错了。
    弹出后 flush 才真的发第二条 `INSERT`，红得才是复合主键。
    """
    world = seeded(session)
    session.expunge_all()
    assert (
        try_add(
            session,
            TaskCard(**card_kwargs(world["request"].id, world["snapshot"].id, "T01")),
        )
        is False
    )


def test_cross_batch_foreign_keys_are_enforced(session):
    """`executor_role_id` / `snapshot_id` 的 FK 由 revision 3 补发，这里验它真的在库里。"""
    world = seeded(session)
    assert (
        try_add(
            session,
            TaskCard(
                id="T02",
                feature_request_id=world["request"].id,
                title="挂不存在的岗位",
                difficulty=1,
                skill_tags=["md"],
                depends_on=[],
                executor_agent_id="qoder",
                executor_role_id="no-such-role",
                artifact_floor_min_bytes=1,
                artifact_floor_min_lines=1,
                artifact_floor_format="md",
                token_budget=Decimal("1"),
                acceptance_criteria=["x"],
                handoff_count=0,
                review_round=0,
                status="DRAFT",
                snapshot_id=world["snapshot"].id,
            ),
        )
        is False
    )


def test_a_card_can_be_stored_before_assignment(session):
    """T-14 候选 ①（owner 2026-10-12）：拆单后可以立即建 EN-02 行，`executor_*` 为 NULL。

    两条断言是一体的：先看没指派的行能落库，再看**非空时外键照常生效**（下一例）。
    只留前一条会变成"nullable 把参照完整性一起放宽了"这种没人想要的读法。
    """
    world = seeded(session)
    assert try_add(
        session,
        TaskCard(
            **card_kwargs(
                world["request"].id,
                world["snapshot"].id,
                "T02",
                executor_agent_id=None,
                executor_role_id=None,
                status="DRAFT",
            )
        )
    ) is True


def test_a_named_executor_still_has_to_be_a_real_tool(session):
    """上一条买的只是"可以还没有"，不是"可以指向不存在的东西"。"""
    world = seeded(session)
    assert (
        try_add(
            session,
            TaskCard(
                **card_kwargs(
                    world["request"].id,
                    world["snapshot"].id,
                    "T02",
                    executor_agent_id="ghost-unit",
                )
            )
        )
        is False
    )


def test_review_verdict_does_not_check_triple_roles_in_the_db_yet(session):
    """特征化用例：字段表写"MUST ≠ executor（入库即断言）"，§4 把强制点指派给 `guard/triple.py`。

    所以库里**不写**第二条 CHECK（写了就是同一规则两个强制点）。本用例绿 = "DB 不是那条
    规则的裁判"这一分工成立；将来若改成 DB 兜底，这里红并要求同步 §4。
    """
    world = seeded(session)
    assert try_add(
        session,
        ReviewVerdict(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            task_card_id=world["card"].id,
            reviewer_agent_id="qoder",  # 与 executor 同一工具 = 违三岗互斥，但不由 DB 判
            verdict="APPROVE",
            item_findings=[],
            extra_criteria_used=False,
        ),
    ) is True


def test_token_budget_positive_is_enforced_but_null_means_no_budget(session):
    """裁决 3：`token_budget MUST > 0` 落到 DB CHECK，而"无预算"用 NULL 表达（nullable 本身即语义）。

    三条断言各管一头：`0` 与负数被 CHECK 拦下 ⇒ "MUST > 0"不再是协议层的口头约束；
    `NULL` 放行 ⇒ 不新增标志字段也能表达"这张卡不设预算"（owner 明写"不新增标志字段"）。
    """
    world = seeded(session)
    assert try_add(
        session,
        TaskCard(**card_kwargs(world["request"].id, world["snapshot"].id, "T02",
                              token_budget=Decimal("0"))),
    ) is False
    assert try_add(
        session,
        TaskCard(**card_kwargs(world["request"].id, world["snapshot"].id, "T03",
                              token_budget=Decimal("-1"))),
    ) is False
    assert try_add(
        session,
        TaskCard(**card_kwargs(world["request"].id, world["snapshot"].id, "T04", token_budget=None)),
    ) is True


def test_serial_and_bigserial_ids_are_assigned_by_the_database(session):
    """EN-14 文档写 `bigserial`、EN-11 写 `serial PK` ⇒ 不传 id 也必须有 id。"""
    world = seeded(session)
    log = add(
        session,
        EventLog(
            entity="task_card",
            entity_id=world["card"].id,
            kind="TASK",
            actor_agent_id="qoder",
            payload={"做了什么": "开工"},
            created_at=NOW,
        ),
    )
    assert log.id is not None
    rule = add(
        session,
        RiskRule(
            match_agent_id="qoder",
            match_role_id="frontend",
            match_capability_tags=["html"],
            no_fs_mutation=True,
            no_paid_external_call=True,
            budget_within_threshold=True,
            auto_pass=True,
        ),
    )
    assert rule.rule_id is not None


def test_batch_three_tables_round_trip(session):
    """EN-15 / EN-16 不只是"表在"，而是能写能读回（第三批的可用手感）。"""
    note = add(
        session,
        MeetingNote(
            id="M01",
            project_key="demo",
            authored_by_agent_id="qoder",
            decision="首页由 qoder 出，审查由 workbuddy 做",
            conflict_sides=["qoder", "workbuddy"],
            created_at=NOW,
        ),
    )
    assert session.get(MeetingNote, "M01").decision == note.decision
    state = add(
        session,
        AutonomyState(
            project_key="demo",
            mode="SAFE",
            danger_list_version="sha256:deadbeef",
            updated_by="owner",
            updated_at=NOW,
            last_intercept_at=None,
        ),
    )
    assert session.get(AutonomyState, "demo").mode == state.mode == "SAFE"


def test_capability_manifest_composite_key_is_the_identity(session):
    """EN-05 只给了"复合唯一 (agent_id, declared_at)"，本轮把它当 PK，不新增文档没有的列。"""
    world = seeded(session)
    first = dict(agent_id="qoder", capabilities=["html"], tier=3, cost_band="MID", max_parallel=2)
    assert try_add(session, CapabilityManifest(declared_at=NOW, **first)) is True
    assert try_add(session, CapabilityManifest(declared_at=NOW, **first)) is False
    assert try_add(session, CapabilityManifest(declared_at=NOW + dt.timedelta(seconds=1), **first)) is True


def test_duplicated_is_out_of_the_type_after_ruling_4(session):
    """裁决 4：`DUPLICATED` 从 `approval_state` 删掉 ⇒ 库里插它必被类型拦下。

    取代原特征化用例（旧口径"保留在枚举域但仓储层不写"）。后半段不是附带断言而是裁决 4 的
    理由本身：删态之后 `resolution_event.applied = false` 是"后到结论"的**唯一**载体，
    它必须仍可写，否则这条裁决就只是把留痕能力删掉了。
    """
    world = seeded(session)
    ticket = add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=world["card"].id,
            feature_request_id=world["request"].id,
            kind="D_DANGER",
            state="PENDING",
            payload={"待批内容": "删库"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        ),
    )
    assert try_add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=world["card"].id,
            feature_request_id=world["request"].id,
            kind="C_BUDGET",
            state="DUPLICATED",
            payload={"待批内容": "已删的态"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        ),
    ) is False
    assert try_add(
        session,
        ResolutionEvent(
            id=uuid.uuid4(),
            approval_ticket_id=ticket.id,
            arrived_at=NOW,
            channel="CLI",
            verdict="reject",
            applied=False,
            reason_text="后到者：CAS 失败，只留这一行痕",
        ),
    ) is True


def test_acceptance_map_and_artifact_link_back_to_the_card(session):
    world = seeded(session)
    add(
        session,
        AcceptanceMap(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            requirement_item="要一个首页",
            acceptance_item="首屏有标题",
            task_card_id=world["card"].id,
            produced_by="qoder",
        ),
    )
    rows = session.query(AcceptanceMap).filter_by(task_card_id="T01").count()
    assert rows == 1


def test_match_simple_is_what_keeps_a_class_tickets_legal(session):
    """特征化用例：复合外键用 MATCH SIMPLE（不是 MATCH FULL）—— 收益与代价都钉在这条里。

    收益：A 类单（挂需求单、`task_card_id` 为 NULL）能落库，这是 `data-model.md` EN-09 与
    `contracts/approval-api.md` 明写的形态；MATCH FULL 要求"要么两列都空、要么两列都非空"，
    会当场拒掉它 —— 拿参照完整性换一个合法业务形态，不取。
    代价：有一列为 NULL 就不校验 ⇒ "(挂了卡却没挂需求单)" 的半拉子行 DB 拦不住，
    强制点指派给 `approval/service.py`（Phase 4）。第三条断言是正对照：两列都非空时外键照常生效。
    """
    world = seeded(session)
    assert try_add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=None,  # A 类单：只挂需求单
            feature_request_id=world["request"].id,
            kind="A_SPLIT",
            state="PENDING",
            payload={"待批内容": "拆分方式"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        ),
    ) is True
    # 已登记的残余：反过来的半拉子行（挂了卡却没挂需求单）在 MATCH SIMPLE 下放行。
    assert try_add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=world["card"].id,
            feature_request_id=None,
            kind="C_BUDGET",
            state="PENDING",
            payload={"待批内容": "半拉子行，由建单路径兜"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        ),
    ) is True
    assert try_add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id="T99",  # 卡不存在
            feature_request_id=world["request"].id,
            kind="B_ACCEPTANCE",
            state="PENDING",
            payload={"待批内容": "两列都非空 ⇒ 外键生效"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=NOW,
        ),
    ) is False


# ------------------------------------------------- EN-18 review_report（Phase 17 P3 D5）


def _verdict_row(session, world, verdict="APPROVE"):
    return add(
        session,
        ReviewVerdict(
            id=uuid.uuid4(),
            feature_request_id=world["request"].id,
            task_card_id=world["card"].id,
            reviewer_agent_id="deepseek-reviewer",
            verdict=verdict,
            item_findings=[{"criterion": "首屏有标题", "met": True}],
            extra_criteria_used=False,
        ),
    )


def _ticket_row(session, world):
    return add(
        session,
        ApprovalTicket(
            id=uuid.uuid4(),
            task_card_id=world["card"].id,
            feature_request_id=world["request"].id,
            kind="E_REVIEW_UNCERTAIN",
            state="PENDING",
            payload={"待批内容": "模型不确定"},
            risk_exempt_eligible=False,
            version=1,
            timeout_at=None,
        ),
    )


def report_kwargs(world, **overrides) -> dict:
    fields = dict(
        id=uuid.uuid4(),
        feature_request_id=world["request"].id,
        task_card_id=world["card"].id,
        attempt_no=1,
        reviewer_agent_id="deepseek-reviewer",
        model_id="deepseek-flash",
        decision="PASS",
        model_decision="PASS",
        reason=None,
        reason_detail=None,
        criteria=[{"criterion": "首屏有标题", "met": True}],
        issues=[],
        prompt_sha256="a" * 64,
        raw_sha256="b" * 64,
        raw_chars=10,
        latency_ms=1,
        verdict_id=None,
        verdict_round_no=1,
        approval_ticket_id=None,
        created_at=NOW,
    )
    fields.update(overrides)
    return fields


def test_review_report_needs_exactly_one_result_exit(session):
    """附录 C 的 XOR 落在 DB：两条出口两列都空 = "审了没结果"，都非空 = 一次尝试两个出口。"""
    world = seeded(session)
    verdict = _verdict_row(session, world)
    ticket = _ticket_row(session, world)
    assert try_add(session, ReviewReport(**report_kwargs(world, verdict_id=verdict.id))) is True
    assert try_add(session, ReviewReport(**report_kwargs(world, attempt_no=2))) is False
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world,
                    attempt_no=3,
                    verdict_id=verdict.id,
                    approval_ticket_id=ticket.id,
                )
            ),
        )
        is False
    )


def test_review_report_decision_is_the_three_value_enum(session):
    world = seeded(session)
    verdict = _verdict_row(session, world)
    assert (
        try_add(
            session,
            ReviewReport(**report_kwargs(world, decision="MAYBE", verdict_id=verdict.id)),
        )
        is False
    )


def test_review_report_reason_iff_need_human(session):
    """`reason` 是升级理由：只有 NEED_HUMAN 有它；反过来 NEED_HUMAN 也必须说得出理由。"""
    world = seeded(session)
    verdict = _verdict_row(session, world)
    ticket = _ticket_row(session, world)
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world,
                    decision="NEED_HUMAN",
                    model_decision="NEED_HUMAN",
                    reason="model_uncertain",
                    approval_ticket_id=ticket.id,
                    verdict_id=None,
                )
            ),
        )
        is True
    )
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world,
                    attempt_no=2,
                    decision="NEED_HUMAN",
                    reason=None,
                    approval_ticket_id=ticket.id,
                )
            ),
        )
        is False
    )
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world, attempt_no=3, decision="PASS", reason="why", verdict_id=verdict.id
                )
            ),
        )
        is False
    )


def test_review_report_attempt_no_is_unique_within_a_card(session):
    """重试是同一张卡的下一次尝试，不是覆盖上一行；序号撞车由唯一约束拦。"""
    world = seeded(session)
    verdict = _verdict_row(session, world)
    assert try_add(session, ReviewReport(**report_kwargs(world, verdict_id=verdict.id))) is True
    assert try_add(session, ReviewReport(**report_kwargs(world, verdict_id=verdict.id))) is False
    assert (
        try_add(
            session,
            ReviewReport(**report_kwargs(world, attempt_no=2, verdict_id=verdict.id)),
        )
        is True
    )


def test_review_report_without_a_model_call_is_allowed(session):
    """预检拒绝（artifact_absent / criteria_empty）没有模型可署名 ⇒ `model_id` 为 NULL 是合法行。

    给一个假模型名会把"没发出去"伪装成"发出去了"（`models_review` 模块头的取舍）。
    """
    world = seeded(session)
    verdict = _verdict_row(session, world)
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world,
                    model_id=None,
                    decision="FAIL",
                    model_decision=None,
                    reason=None,
                    prompt_sha256=None,
                    raw_sha256=None,
                    raw_chars=0,
                    latency_ms=0,
                    verdict_id=verdict.id,
                )
            ),
        )
        is True
    )


def test_review_report_structured_fields_must_be_json_arrays(session):
    world = seeded(session)
    verdict = _verdict_row(session, world)
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(world, criteria={"不是数组": True}, verdict_id=verdict.id)
            ),
        )
        is False
    )
    assert (
        try_add(
            session,
            ReviewReport(
                **report_kwargs(
                    world, attempt_no=2, issues={"不是数组": True}, verdict_id=verdict.id
                )
            ),
        )
        is False
    )


def test_alembic_cli_runs_on_the_real_engine():
    """迁移工具的**在线路径**要真跑一次：本轮实测过它静默坏掉，而用例可以全绿。

    根因已点名：`env.py` 在 T027 把取 DSN 收敛到 `loader.db_url()` 时，`run_migrations_online()`
    漏改了一处旧名 ⇒ `NameError`。离线分支（`--sql`）走另一段代码，"能生成 SQL"不等于"能连库执行"；
    而本文件的用例各自 `create_engine` 直连，**压根不经过 `env.py`**
    ⇒ "integration 档全绿 + `alembic upgrade head` 起不来"是可以同时成立的，这条用例就是来堵这个缝的。
    挑 `alembic check` 是因为它最便宜：在线连库、只比不写，顺带把"模型与已下发迁移漂移"一起钉住。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=REPO,
        env={**os.environ, **CHILD_STDIO_ENV},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    merged = (proc.stdout + proc.stderr).lower()
    assert "nameerror" not in merged, "env.py 的在线分支里有未定义名"
    assert proc.returncode == 0, (
        f"alembic check 退出码 {proc.returncode}\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    )


# ------------------------------------------------- 空库整链重放（A-5｜owner 2026-10-14 裁决 1）

#: 本轮专用探针库名。**故意不是 `acp`**：那是共享开发库，别的会话正在用，
#: 一条会 `downgrade base` 的用例把它清成空库就是拿别人的现场换自己的读数。
PROBE_DB = "acp_schema_replay_probe"
CHANNEL_TYPE_NAME = "resolution_channel_type"
REVIEW_VERDICT_TYPE_NAME = "review_verdict_type"
APPROVAL_KIND_TYPE_NAME = "approval_kind_type"

_LABELS_SQL = (
    "select e.enumlabel from pg_type t "
    "join pg_enum e on e.enumtypid = t.oid "
    "join pg_namespace n on n.oid = t.typnamespace "
    "where t.typname = :type_name and n.nspname = current_schema()"
)


def _probe_dsn(database: str) -> str:
    """把 `.env` 那份 DSN 的库名换成 `database`，其余（主机/端口/账号）原样。

    只在函数内部拼串，**MUST NOT 把它打印或断言进消息里** —— 那条串含口令。
    """
    dsn = os.environ[DSN_ENV]
    return f"{dsn.rsplit('/', 1)[0]}/{database}"


def _on_maintenance(statement: str) -> None:
    engine = create_engine(db_url({"ACP_DB_DSN": _probe_dsn("postgres")}))
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


def _probe_read(sql: str, **params: object):
    engine = create_engine(db_url({"ACP_DB_DSN": _probe_dsn(PROBE_DB)}))
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).all()
    finally:
        engine.dispose()


def _on_maintenance_in_probe(sql: str) -> None:
    """在探针库上直接发一条 DDL/维护语句（AUTOCOMMIT）。

    `ALTER TYPE … ADD VALUE` 在 PG 里不许与被使用的事务混在一起，而这里要的正是"手工把值加进去"
    这一半残局，所以单独一条自动提交，不走 alembic。
    """
    engine = create_engine(db_url({"ACP_DB_DSN": _probe_dsn(PROBE_DB)}))
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def _fresh_probe_db() -> None:
    _on_maintenance(f'DROP DATABASE IF EXISTS "{PROBE_DB}"')
    _on_maintenance(f'CREATE DATABASE "{PROBE_DB}"')


def _drop_probe_db() -> None:
    _on_maintenance(f'DROP DATABASE IF EXISTS "{PROBE_DB}"')


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """在**子进程**里跑 alembic，并把它的 `ACP_DB_DSN` 换成探针库。

    必须走子进程：`env.py` 自己调 `loader.db_url()` 取环境，本用例的进程内改动它看不见，
    而 `os.environ` 直接改会把后面同档的用例一起带进探针库（那是"读数来自另一个库"的假绿）。
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env={**os.environ, **CHILD_STDIO_ENV, DSN_ENV: _probe_dsn(PROBE_DB)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _labels_of(type_name: str) -> set[str]:
    return {row[0] for row in _probe_read(_LABELS_SQL, type_name=type_name)}


def _fail(proc: subprocess.CompletedProcess[str]) -> str:
    return f"rc={proc.returncode}\nSTDOUT:{proc.stdout[-800:]}\nSTDERR:{proc.stderr[-800:]}"


def test_upgrade_head_replays_from_an_empty_database():
    """A-5 的正面对手（owner 裁决 1 动作 1/4/5）：**空库能把整条链建起来，且 `downgrade base` 之后还能再建**。

    这条测的不是 0007 那一列，测的是迁移链的一条形状规矩：**建类型的迁移要用"当年"的标签集**
    （`enums.pg_enum_frozen()`），不能用 `enums.py` 的活值。用活值时的表现是本轮 14:2x 实测到的：
    空库走到 `0006` 报 `DuplicateObject: enum label "AUTO" already exists` ⇒ 链起不来，
    而增量库看不到这个洞（它的类型是当年那份两值集合）。
    与本文件 `test_alembic_cli_runs_on_the_real_engine` 是同一个缝的两半：那条堵"在线分支能不能连库"，
    这条堵"从空库重放能不能跑完"。**那条绿、这条红是可以同时成立的**，所以两条都要在。

    **本条不覆盖 0002 的冻结语义，那一条由半场态判据（`test_a_half_applied_auto_label_still_replays`）看守。**
    （owner 2026-10-14 P2-2 点名要写进 docstring 的一句。为什么是真缺口：`0006` 的幂等守卫看到库里已有
    `AUTO` 就跳过 ADD，所以 `0002` 万一退回活标签集，本条**仍会绿** —— 本轮 M1 变异实测过这一形状。）
    """
    _fresh_probe_db()
    try:
        up = _alembic("upgrade", "head")
        assert up.returncode == 0, _fail(up)
        # 加值这一步真的发生过（不是靠 0002 的冻结把 AUTO 提前塞进去 —— 那是另一个坏法，见下一条用例）
        assert _labels_of(CHANNEL_TYPE_NAME) == {"DASHBOARD", "CLI", "AUTO"}, _labels_of(CHANNEL_TYPE_NAME)
        # Phase 17 P1 的两个新增值同理：0002 冻结成当年标签集，0008 的 ADD VALUE 才真的执行到。
        assert _labels_of(REVIEW_VERDICT_TYPE_NAME) == {
            "APPROVE",
            "CHANGES_REQUESTED",
            "NEED_HUMAN",
        }, _labels_of(REVIEW_VERDICT_TYPE_NAME)
        assert _labels_of(APPROVAL_KIND_TYPE_NAME) == {
            "A_SPLIT",
            "B_ACCEPTANCE",
            "C_BUDGET",
            "D_DANGER",
            "E_REVIEW_UNCERTAIN",
        }, _labels_of(APPROVAL_KIND_TYPE_NAME)
        columns = {row[0] for row in _probe_read(
            "select column_name from information_schema.columns where table_name='task_card'"
        )}
        assert "rework_count" in columns, sorted(columns)

        down = _alembic("downgrade", "base")
        assert down.returncode == 0, _fail(down)
        left = {row[0] for row in _probe_read(
            "select table_name from information_schema.tables where table_schema='public'"
        )}
        # `alembic_version` 是 alembic 自己的表，`downgrade base` 只删里面的那一行、不删表本身
        # （本轮先按 `== set()` 写，红在这里 —— 那不是链不可重放，是我把工具的簿记当成了我们的表）。
        assert left <= {"alembic_version"}, f"降级 base 之后我们的表没被清干净：{sorted(left)}"
        assert _labels_of(CHANNEL_TYPE_NAME) == set(), "类型没随 base 降级拔干净（`DROP TABLE` 不删类型）"

        again = _alembic("upgrade", "head")
        assert again.returncode == 0, _fail(again)
    finally:
        _drop_probe_db()


def test_a_half_applied_auto_label_still_replays():
    """`0006` 那句 `ADD VALUE` 的守卫（owner 裁决 1 动作 3"两者都做更稳"），顺带把 0002 的冻结从反方向钉住。

    库里**已经有** `AUTO` 而 `alembic_version` 还停在 `t024_approval` —— 那是"0006 的 ALTER 落成了、
    版本号没落"那种中断残局。没有守卫时重放本批直接报 `DuplicateObject` 卡死在链中间；
    有守卫时它跳过加值、把本批的另两件（放宽 NOT NULL、哨兵改回 NULL）照常做完。

    前半段的断言是**冻结生效的证据**：`upgrade t024_approval` 之后类型里 MUST NOT 有 `AUTO`。
    把它摘掉（0002 改回 `pg_enum("resolution_channel")`）时，这一条先红在第一句上，
    而不是等 `upgrade head` 才红 —— 两条用例各自有各自的失败位置，才分得清是哪一处被改坏。
    """
    _fresh_probe_db()
    try:
        first = _alembic("upgrade", "t024_approval")
        assert first.returncode == 0, _fail(first)
        assert _labels_of(CHANNEL_TYPE_NAME) == {"DASHBOARD", "CLI"}, (
            f"0002 建类型时用了活标签集（A-5 回来了）：{sorted(_labels_of(CHANNEL_TYPE_NAME))}"
        )
        # Phase 17 P1：这两个域在 0002 下发时也各只有当年那几个值。用活值建类型的话，
        # 0008 的 ADD VALUE 会在空库重放时报 DuplicateObject（与 A-5 同形）。
        assert _labels_of(REVIEW_VERDICT_TYPE_NAME) == {"APPROVE", "CHANGES_REQUESTED"}, (
            f"0002 的 review_verdict 类型用了活标签集：{sorted(_labels_of(REVIEW_VERDICT_TYPE_NAME))}"
        )
        assert _labels_of(APPROVAL_KIND_TYPE_NAME) == {
            "A_SPLIT",
            "B_ACCEPTANCE",
            "C_BUDGET",
            "D_DANGER",
        }, f"0002 的 approval_kind 类型用了活标签集：{sorted(_labels_of(APPROVAL_KIND_TYPE_NAME))}"
        # 人工造半程状态：值加进去，版本号仍停在 0002（这正是守卫要接住的那个库）
        _on_maintenance_in_probe("ALTER TYPE resolution_channel_type ADD VALUE 'AUTO'")
        rest = _alembic("upgrade", "head")
        assert rest.returncode == 0, f"守卫没接住半程库：{_fail(rest)}"
        assert _labels_of(CHANNEL_TYPE_NAME) == {"DASHBOARD", "CLI", "AUTO"}
    finally:
        _drop_probe_db()

