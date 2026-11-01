"""迁移第二批：EN-07 / EN-08 / EN-09 / EN-10 / EN-11（T024）

Revision ID: t024_approval
Revises: t023_core
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from acp.persistence.enums import enum_type_name, pg_enum, pg_enum_frozen

revision: str = "t024_approval"
down_revision: str | None = "t023_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 见 `0001_t023_core.py` 的同名说明：降级 MUST 显式 DROP TYPE，否则迁移链不可重放。
ENUM_DOMAINS: tuple[str, ...] = (
    "review_verdict",
    "approval_kind",
    "approval_state",
    "resolution_channel",
    "resolution_verdict",
)

#: `resolution_channel` 在**本批下发时**（2026-10-12）只有这两个值。第三个值 `AUTO` 是
#: owner 2026-10-13 的裁决 ④ 经 `0006` 那句 `ALTER TYPE … ADD VALUE 'AUTO'` 加进来的。
#: 这里为什么不用 `pg_enum("resolution_channel")`（它取 `enums.py` 的活标签集）：
#: 空库重放时 `CREATE TABLE resolution_event` 会用**今天**的三值集合内联发 `CREATE TYPE`，
#: 于是 `0006` 那句 ADD VALUE 报 `DuplicateObject` ⇒ 整条链起不来（A-5，owner 2026-10-14 裁决 1 选 ①）。
#:
#: **Phase 17 P1 起 `review_verdict` 与 `approval_kind` 也冻结**：`0008` 会分别往这两个类型
#: ADD VALUE（`NEED_HUMAN` / `E_REVIEW_UNCERTAIN`）。继续用 `pg_enum()` 的活标签集建类型时，
#: 空库重放到 0008 会撞同一个 `DuplicateObject`，表现与 A-5 完全相同 —— 增量库看不见，
#: 只有从空库重放才红。冻结的是**本批下发时**的标签集，不是今天的。
#: 剩下两个域（`approval_state` / `resolution_verdict`）不冻结：它们的集合自本批下发以来
#: **没有前向增值**（`approval_state` 少掉 `DUPLICATED` 是 owner 2026-10-12 裁决 4 的**同日**更正、
#: 且没有任何迁移往那个类型 ADD VALUE，冻结它反而会造出一个库里从未有过的值），所以取活值与取当年值
#: 是同一个集合。判据在 `tests/integration/test_schema_invariants.py`（空库 `upgrade head` 必须 rc=0）。
RESOLUTION_CHANNEL_LABELS_AT_T024: tuple[str, ...] = ("DASHBOARD", "CLI")
REVIEW_VERDICT_LABELS_AT_T024: tuple[str, ...] = ("APPROVE", "CHANGES_REQUESTED")
APPROVAL_KIND_LABELS_AT_T024: tuple[str, ...] = (
    "A_SPLIT",
    "B_ACCEPTANCE",
    "C_BUDGET",
    "D_DANGER",
)


def upgrade() -> None:
    op.create_table(
        "acceptance_map",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requirement_item", sa.Text(), nullable=False),
        sa.Column("acceptance_item", sa.Text(), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=False),
        sa.Column("produced_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_acceptance_map"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_acceptance_map_feature_request_id_feature_request",
        ),
        # 裁决 2：卡的 PK 是 (feature_request_id, id) ⇒ 外键引用这一对列；本表已有该列，不新增。
        sa.ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
            name="fk_acceptance_map_feature_request_id_task_card",
        ),
    )
    op.create_table(
        "review_verdict",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # 裁决 2 的连带列（同 EN-06 的理由）。
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=False),
        sa.Column("reviewer_agent_id", sa.Text(), nullable=False),
        sa.Column(
            "verdict",
            pg_enum_frozen("review_verdict", REVIEW_VERDICT_LABELS_AT_T024),
            nullable=False,
        ),
        sa.Column("item_findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("extra_criteria_used", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_review_verdict"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_review_verdict_feature_request_id_feature_request",
        ),
        sa.ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
            name="fk_review_verdict_feature_request_id_task_card",
        ),
        # FR-029：引入额外标准即拒绝入库。
        sa.CheckConstraint(
            "extra_criteria_used = false", name="no_extra_criteria"
        ),
    )
    op.create_table(
        "approval_ticket",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=True),
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "kind",
            pg_enum_frozen("approval_kind", APPROVAL_KIND_LABELS_AT_T024),
            nullable=False,
        ),
        sa.Column("state", pg_enum("approval_state"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_exempt_eligible", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decided_by_principal", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approval_ticket"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_approval_ticket_feature_request_id_feature_request",
        ),
        # 裁决 2 的连带改动，用默认 MATCH SIMPLE（**不是** MATCH FULL）：MATCH FULL 会把文档
        # 明写的 A 类单（挂需求单、task_card_id 为 NULL）拒掉。代价是"挂了卡却没挂需求单"
        # 的半拉子行绕过参照校验 ⇒ 由建单路径兜（`approval/service.py`，Phase 4），已登记。
        sa.ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
            name="fk_approval_ticket_feature_request_id_task_card",
        ),
    )
    # FR-045 未决态部分唯一索引：同一 (task_card_id, kind) 至多一张 PENDING。
    op.create_index(
        "uq_approval_ticket_pending_card_kind",
        "approval_ticket",
        ["task_card_id", "kind"],
        unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_table(
        "resolution_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "channel", pg_enum_frozen("resolution_channel", RESOLUTION_CHANNEL_LABELS_AT_T024), nullable=False
        ),
        sa.Column("verdict", pg_enum("resolution_verdict"), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_resolution_event"),
        sa.ForeignKeyConstraint(
            ["approval_ticket_id"],
            ["approval_ticket.id"],
            name="fk_resolution_event_approval_ticket_id_approval_ticket",
        ),
    )
    op.create_table(
        "risk_rule",
        # 文档写 `serial PK`：Integer PK + autoincrement ⇒ PG 建 SERIAL（自序列由 PG 命名，
        # 不在此写 nextval('<不存在的序列>')，那会在建表时报"sequence does not exist"）。
        sa.Column("rule_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_agent_id", sa.Text(), nullable=False),
        sa.Column("match_role_id", sa.Text(), nullable=True),
        sa.Column("match_capability_tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("no_fs_mutation", sa.Boolean(), nullable=False),
        sa.Column("no_paid_external_call", sa.Boolean(), nullable=False),
        sa.Column("budget_within_threshold", sa.Boolean(), nullable=False),
        sa.Column("auto_pass", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("rule_id", name="pk_risk_rule"),
        # 三条条件同时成立才可为 auto_pass（免确认路径只有一条）。
        sa.CheckConstraint(
            "NOT auto_pass OR (no_fs_mutation AND no_paid_external_call AND budget_within_threshold)",
            name="auto_pass_needs_all_three",
        ),
    )


def downgrade() -> None:
    op.drop_table("risk_rule")
    op.drop_index("uq_approval_ticket_pending_card_kind", table_name="approval_ticket")
    op.drop_table("resolution_event")
    op.drop_table("approval_ticket")
    op.drop_table("review_verdict")
    op.drop_table("acceptance_map")
    for domain in ENUM_DOMAINS:
        op.execute(f"DROP TYPE IF EXISTS {enum_type_name(domain)}")
