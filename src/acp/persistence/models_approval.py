"""迁移第二批（T024）：EN-07 `acceptance_map` / EN-08 `review_verdict` /
EN-09 `approval_ticket` / EN-10 `resolution_event` / EN-11 `risk_rule`。

字段照 `data-model.md §1`。本批唯一由文档明确指派给数据库的约束是 EN-09 的
**未决态部分唯一索引**（FR-045）与 EN-08 / EN-11 的两条布尔不变量；或签 CAS 的
"后到者不二次迁移"指派给 `approval/repository.py`（§4），所以 DB 只留 `version` 列，
不写触发器。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import mapped_column

from acp.persistence.base import Base
from acp.persistence.enums import pg_enum


class AcceptanceMap(Base):
    """EN-07 需求→验收条目映射表。

    文档**没给本表任何主键**（只列了 5 个业务列）。这里加 `id uuid PK` 是 SQLAlchemy 映射的
    硬要求，属本轮新增，已登记为 `data-model.md §5` T-11 的文档缺口；不加代理键而把
    `(feature_request_id, task_card_id, acceptance_item)` 当复合键同样是我发明规则，不取。
    """

    __tablename__ = "acceptance_map"
    __table_args__ = (
        # 裁决 2 的连带改动：卡的 PK 是 `(feature_request_id, id)` ⇒ 外键必须引用这一对列。
        # 本表已有 `feature_request_id`（EN-07 文档列），直接组成复合外键，不新增列。
        ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
        ),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False
    )
    requirement_item = mapped_column(Text, nullable=False)
    acceptance_item = mapped_column(Text, nullable=False)
    task_card_id = mapped_column(Text, nullable=False)
    # = 总管 agent_id（FR-007）。文档此处未标 FK，按字面存 text，不自行收紧成外键。
    produced_by = mapped_column(Text, nullable=False)


class ReviewVerdict(Base):
    """EN-08 审查结论。

    `reviewer_agent_id` 的说明写"MUST ≠ executor（入库即断言）"，但 §4 把三岗互斥的强制点
    指派给 `guard/triple.py` ⇒ DB 侧不再写第二份 CHECK；"入库即断言"由写库前的守卫节点承担，
    该节点属 Phase 2（T045 之后），本轮只落表结构。`produced_by` 类同：文档未标 FK 就不加。
    """

    __tablename__ = "review_verdict"
    __table_args__ = (
        # FR-029：引入额外标准即拒绝入库——文档把"恒为 false"写成列级事实 ⇒ 落 DB CHECK。
        CheckConstraint("extra_criteria_used = false", name="no_extra_criteria"),
        ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
        ),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    # 裁决 2 的连带列（同 EN-06 的理由：单列外键在复合主键上没有可引用的唯一约束）。
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False
    )
    task_card_id = mapped_column(Text, nullable=False)
    reviewer_agent_id = mapped_column(Text, nullable=False)
    verdict = mapped_column(pg_enum("review_verdict"), nullable=False)
    item_findings = mapped_column(JSONB, nullable=False)
    extra_criteria_used = mapped_column(Boolean, nullable=False)


class ApprovalTicket(Base):
    """EN-09 审批单（卡点 A/B/C/D/E，FR-038 ~ FR-045 + Phase 17 P1 D3）。

    两处按文档内部一致性作出的判定，均已登记 T-11：
    `decided_by_principal` / `decided_at` 在字段表里**没有** NULL 标记，但同一张表的部分唯一索引
    以 `state='PENDING'` 为条件 ⇒ 未决行必然存在，而未决行不可能已有决意人与决定时刻。
    按字面建成 NOT NULL 会让 `PENDING` 无处可存（门禁自锁），故这两列可空，其余列按字面不可空。
    """

    __tablename__ = "approval_ticket"
    __table_args__ = (
        # FR-045 未决态部分唯一索引，逐字照文档的键与条件。
        # 已知残余（登记不扩大）：PG 里 NULL 彼此不相等 ⇒ `kind=A_SPLIT`（挂需求单、
        # task_card_id 为 NULL）不受本索引去重；文档给的键只有这一种写法，不自行补第二个索引。
        Index(
            "uq_approval_ticket_pending_card_kind",
            "task_card_id",
            "kind",
            unique=True,
            postgresql_where=text("state = 'PENDING'"),
        ),
        # 裁决 2 的连带改动。用**默认 MATCH SIMPLE**，不是 MATCH FULL：
        # 后者要求"要么两列都空、要么两列都非空"，会把文档明写的 A 类单（挂需求单、
        # `task_card_id` 为 NULL）直接拒掉 —— 那是拿参照完整性换一个合法业务形态，不取。
        # **代价如实登记**：MATCH SIMPLE 下只要有一列为 NULL 就不校验，因此
        # "(挂了卡却没挂需求单)" 这种半拉子行能过；这条参照完整性由建单路径兜
        # （`approval/service.py`，Phase 4），库侧留一条特征化用例钉住现状。
        ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
        ),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    task_card_id = mapped_column(Text, nullable=True)
    feature_request_id = mapped_column(UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=True)
    kind = mapped_column(pg_enum("approval_kind"), nullable=False)
    state = mapped_column(pg_enum("approval_state"), nullable=False)
    # 五要素必填的建单校验指派给 `approval/service.py`（§4），DB 只保证它是 JSON 对象。
    payload = mapped_column(JSONB, nullable=False)
    risk_exempt_eligible = mapped_column(Boolean, nullable=False)
    # 乐观锁：CAS 的比较与"后到者落 duplicate"在仓储层（§4），DB 只提供这一列。
    version = mapped_column(Integer, nullable=False)
    # P1-4（2026-10-30）：这一行**建出来的时刻**。它存在的唯一理由是让"这张单挂了多久"可算 ——
    # A / C / D 三类的 `timeout_at` 是 NULL（"没有可算的时刻"），没有这一列就没有任何起点。
    # `server_default=now()`：时刻是 DB 侧的事实，不该要求每个写入点都记得传；迁移期历史行
    # 按需求单创建时间回填（见 `0011`）。
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    decided_by_principal = mapped_column(Text, nullable=True)
    decided_at = mapped_column(DateTime(timezone=True), nullable=True)
    # timeout_at 由 params-ref §1 的默认值算出后写入，不入代码常量（引用型参数条款）。
    # 裁决 ②（owner 2026-10-13）改**可空**：§1 有三行写"无超时"，第一版把它落成 timestamptz 上界哨兵，
    # owner 否掉（"哨兵值是反模式；nullable 是 SQL 原生语义"，先例 = `0004_t014_nullable_executor`）。
    # NULL 的语义是"没有可算的时刻"，不是"一个极远的时刻"：超时扫描 `WHERE timeout_at <= now()`
    # 在 NULL 上恒不命中（SQL 三值逻辑），所以 A / C / D 不需要任何魔法数就天然不会被放行。
    timeout_at = mapped_column(DateTime(timezone=True), nullable=True)


class ResolutionEvent(Base):
    """EN-10 结论到达流水。`applied=false` 的行即 duplicate 留痕（FR-040）。"""

    __tablename__ = "resolution_event"

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    approval_ticket_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_ticket.id"), nullable=False
    )
    arrived_at = mapped_column(DateTime(timezone=True), nullable=False)
    channel = mapped_column(pg_enum("resolution_channel"), nullable=False)
    verdict = mapped_column(pg_enum("resolution_verdict"), nullable=False)
    applied = mapped_column(Boolean, nullable=False)
    reason_text = mapped_column(Text, nullable=False)


class RiskRule(Base):
    """EN-11 低风险免确认规则表（由 `config/risk_matrix.yaml` 载入）。

    三段匹配键（agent / role / capability_tags）的**命中口径**是应用层规则（等值 AND 等值 AND
    子集，任一列 NULL/空集 ⇒ 不命中），落在 `registry/risk_engine.py`；DB 侧只落文档明写的
    那条布尔不变量：三条条件同时成立才可为 `auto_pass`。
    """

    __tablename__ = "risk_rule"
    __table_args__ = (
        CheckConstraint(
            "NOT auto_pass OR (no_fs_mutation AND no_paid_external_call AND budget_within_threshold)",
            name="auto_pass_needs_all_three",
        ),
    )

    rule_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_agent_id = mapped_column(Text, nullable=False)
    match_role_id = mapped_column(Text, nullable=True)
    match_capability_tags = mapped_column(ARRAY(Text), nullable=False)
    no_fs_mutation = mapped_column(Boolean, nullable=False)
    no_paid_external_call = mapped_column(Boolean, nullable=False)
    budget_within_threshold = mapped_column(Boolean, nullable=False)
    auto_pass = mapped_column(Boolean, nullable=False)
