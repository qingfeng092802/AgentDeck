"""迁移第三批：EN-04 / EN-12 / EN-13 / EN-14 / EN-15 / EN-16 / EN-17（T025）
+ 补发第一批里声明、当时无法下发的两条 `task_card` 外键。

Revision ID: t025_registry
Revises: t024_approval
Create Date: 2026-10-12

**FK 为什么单独补发**：`task_card.executor_role_id → role_spec` 与
`task_card.snapshot_id → snapshot` 指向本批才建的表；在 `t023_core` 里建列时目标表还不存在，
PG 会直接报错。把约束留到目标表出现之后下发，模型侧则一次声明到位——
这样 `alembic check` 比对的是"三批全部落地后"的终局形态，不会长期残留 ORM↔DB 差异。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from acp.persistence.enums import enum_type_name, pg_enum

revision: str = "t025_registry"
down_revision: str | None = "t024_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 见 `0001_t023_core.py` 的同名说明：降级 MUST 显式 DROP TYPE，否则迁移链不可重放。
ENUM_DOMAINS: tuple[str, ...] = (
    "snapshot_phase",
    "usage_kind",
    "billing_basis",
    "event_log_kind",
    "autonomy_mode",
)


def upgrade() -> None:
    op.create_table(
        "role_spec",
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("charter", sa.Text(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("mounted_agents", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("role_id", name="pk_role_spec"),
    )
    op.create_table(
        "snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("phase", pg_enum("snapshot_phase"), nullable=False),
        sa.Column("task_card_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot"),
    )
    op.create_foreign_key(
        "fk_task_card_executor_role_id_role_spec",
        "task_card",
        "role_spec",
        ["executor_role_id"],
        ["role_id"],
    )
    op.create_foreign_key(
        "fk_task_card_snapshot_id_snapshot",
        "task_card",
        "snapshot",
        ["snapshot_id"],
        ["id"],
    )
    op.create_table(
        "cost_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("model_ref", sa.Text(), nullable=True),
        sa.Column("usage_kind", pg_enum("usage_kind"), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("billing_basis", pg_enum("billing_basis"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cost_record"),
    )
    # FR-059 防重复入账：四元组唯一索引，键顺序逐字照 data-model EN-13。
    op.create_index(
        "uq_cost_record_dedup",
        "cost_record",
        ["task_card_id", "agent_id", "occurred_at", "usage_kind"],
        unique=True,
    )
    op.create_table(
        "event_log",
        # 文档写 `bigserial`：BigInteger PK + autoincrement ⇒ PG 建 BIGSERIAL。
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("kind", pg_enum("event_log_kind"), nullable=False),
        sa.Column("actor_agent_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_event_log"),
    )
    op.create_table(
        "meeting_note",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("authored_by_agent_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("conflict_sides", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_meeting_note"),
    )
    op.create_table(
        "autonomy_state",
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("mode", pg_enum("autonomy_mode"), nullable=False),
        sa.Column("danger_list_version", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_intercept_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("project_key", name="pk_autonomy_state"),
    )
    op.create_table(
        "deliverable",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("entry_path", sa.Text(), nullable=False),
        sa.Column("open_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assembled_by_agent_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_deliverable"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_deliverable_feature_request_id_feature_request",
        ),
        # FR-036"唯一一份"用唯一索引钉住：第二包入库即冲突报错，不静默覆盖。
        sa.UniqueConstraint(
            "feature_request_id", name="uq_deliverable_feature_request_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("deliverable")
    op.drop_table("autonomy_state")
    op.drop_table("meeting_note")
    op.drop_table("event_log")
    op.drop_index("uq_cost_record_dedup", table_name="cost_record")
    op.drop_table("cost_record")
    op.drop_constraint("fk_task_card_snapshot_id_snapshot", "task_card", type_="foreignkey")
    op.drop_constraint(
        "fk_task_card_executor_role_id_role_spec", "task_card", type_="foreignkey"
    )
    op.drop_table("snapshot")
    op.drop_table("role_spec")
    for domain in ENUM_DOMAINS:
        op.execute(f"DROP TYPE IF EXISTS {enum_type_name(domain)}")
