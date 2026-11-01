"""迁移第一批：EN-01 / EN-02 / EN-03 / EN-05 / EN-06（T023）

Revision ID: t023_core
Revises:
Create Date: 2026-10-12

**为什么本文件从 `acp.persistence.enums` 取枚举类型而不内联字面量**：`data-model.md §5` T-1
的要件是"PG 枚举 MUST 引用 `refs.py::ArtifactFormat`，不再各写一份字面量"。迁移内联字面量
= 在唯一事实源之外再造一份，且它落在**已下发的迁移历史**里，改了也不会回头生效。
其余标量类型（Text/Integer/JSONB/…）内联是刻意的：迁移必须能被未来独立解读，不随模型漂移。

`task_card` 的两条外键（→ `role_spec` / → `snapshot`）指向第三批的表，本 revision 只建列、
不建约束，由 `t025_registry` 补下发（约束名与该 revision 一致，`alembic check` 靠名字比对）。

**约束名在这里也只写短名**：Alembic 的 `op.create_table` 会套用 `env.py` 里 `target_metadata`
的命名约定，与模型侧同一条规则。写成全名（`ck_task_card_floor_bytes_positive`）会被再前缀一次，
实测落库为 `ck_task_card_ck_task_card_floor_bytes_positive`（本轮第一版就踩到了）。

**owner 裁决 2 / 3 / 4 就地改这三份 revision，而不是加第四份**（2026-10-12 收官轮）：改的是
主键形态、列可空性、枚举取值——三者都是**基线自身的定义**，而这条链从未离开本机开发库
（`data_rows=0`，`downgrade base → upgrade head` 已实测可逆）。留一份"先建错主键再改回来"的
revision 会让基线读起来像有历史包袱，也让 `alembic check` 比的是三次改动的净效果而非一张干净的表。
代价点名：**改完 MUST 重建库**（`downgrade base` + `upgrade head` + `alembic check`），
否则库里是旧形态、代码是新形态，而这种分叉要等第一次插数据才炸出来。
重建已于 2026-10-12 13:3x 实跑：重建前 `data_rows=0`（先量的，不是事后说的），`downgrade base` 后
`enum_types=0 / base_tables=1`，`upgrade head` 与 `alembic check` 均退出码 **0**，
终局目录 `ck_named=7 / fk=18 / approval_state_labels=5 / task_card 主键两列`（逐项读数见
`quickstart.md §2` 路径 C 的"PG 事实态初始化与迁移"裁决轮补记）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from acp.persistence.enums import enum_type_name, pg_enum

revision: str = "t023_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本 revision 引入的枚举域；`upgrade` 由建表自动发 CREATE TYPE，
# `downgrade` MUST 显式 DROP TYPE —— PG 的枚举类型不被 `DROP TABLE` 级联删除，
# 漏掉这步的表现是"降级后重放迁移报 type already exists"（本轮实测到并修掉）。
ENUM_DOMAINS: tuple[str, ...] = (
    "feature_request_status",
    "agent_form",
    "cost_band",
    "artifact_format",
    "task_card_state",
    "self_reported_status",
    "validated_status",
)


def upgrade() -> None:
    op.create_table(
        "feature_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_key", sa.Text(), nullable=False),
        sa.Column("raw_input", sa.Text(), nullable=False),
        sa.Column("summary_digest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("clarification_waived", sa.Boolean(), nullable=False),
        sa.Column("status", pg_enum("feature_request_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_feature_request"),
    )
    op.create_table(
        "agent_unit",
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("form", pg_enum("agent_form"), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("cost_band", pg_enum("cost_band"), nullable=False),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("current_load", sa.Integer(), nullable=False),
        sa.Column("historical_success_rate", sa.Numeric(), nullable=True),
        sa.Column("success_rate_sample", sa.Integer(), nullable=False),
        sa.Column("workspace_dir", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_unit"),
        sa.CheckConstraint(
            "success_rate_sample > 0 OR historical_success_rate IS NULL",
            name="no_rate_without_sample",
        ),
    )
    op.create_table(
        "task_card",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("skill_tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("depends_on", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("executor_agent_id", sa.Text(), nullable=False),
        sa.Column("executor_role_id", sa.Text(), nullable=False),
        sa.Column("reviewer_agent_id", sa.Text(), nullable=True),
        sa.Column("acceptor_agent_id", sa.Text(), nullable=True),
        sa.Column("artifact_floor_min_bytes", sa.Integer(), nullable=False),
        sa.Column("artifact_floor_min_lines", sa.Integer(), nullable=False),
        sa.Column("artifact_floor_format", pg_enum("artifact_format"), nullable=False),
        sa.Column("token_budget", sa.Numeric(), nullable=True),
        sa.Column("deadline_minutes", sa.Integer(), nullable=True),
        sa.Column("acceptance_criteria", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "review_standard_locked", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("handoff_count", sa.Integer(), nullable=False),
        sa.Column("review_round", sa.Integer(), nullable=False),
        sa.Column("status", pg_enum("task_card_state"), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        # owner 2026-10-12 裁决 2：主键 = (feature_request_id, id)，任务卡 id 只要求"同一需求单内唯一"。
        sa.PrimaryKeyConstraint("feature_request_id", "id", name="pk_task_card"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_task_card_feature_request_id_feature_request",
        ),
        sa.ForeignKeyConstraint(
            ["executor_agent_id"], ["agent_unit.agent_id"], name="fk_task_card_executor_agent_id_agent_unit"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_agent_id"], ["agent_unit.agent_id"], name="fk_task_card_reviewer_agent_id_agent_unit"
        ),
        sa.ForeignKeyConstraint(
            ["acceptor_agent_id"], ["agent_unit.agent_id"], name="fk_task_card_acceptor_agent_id_agent_unit"
        ),
        sa.CheckConstraint(
            "artifact_floor_min_bytes > 0", name="floor_bytes_positive"
        ),
        sa.CheckConstraint(
            "artifact_floor_min_lines > 0", name="floor_lines_positive"
        ),
        sa.CheckConstraint("handoff_count <= 1", name="handoff_count_within_limit"),
        # 裁决 3：NULL = 无预算（SQL 的 CHECK 对 NULL 恒不违反），非空时 MUST > 0。
        sa.CheckConstraint("token_budget > 0", name="token_budget_positive"),
    )
    op.create_table(
        "capability_manifest",
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("cost_band", pg_enum("cost_band"), nullable=False),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "declared_at", name="pk_capability_manifest"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_unit.agent_id"], name="fk_capability_manifest_agent_id_agent_unit"
        ),
    )
    op.create_table(
        "artifact",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # 裁决 2 的连带列：卡的 PK 变成 (feature_request_id, id) 后，单列 task_card_id 外键
        # 没有可引用的唯一约束了 ⇒ 产物必须一并带上所属需求单，才能组成复合外键。
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=False),
        sa.Column("writer_agent_id", sa.Text(), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("lines", sa.Integer(), nullable=False),
        sa.Column("format", pg_enum("artifact_format"), nullable=False),
        sa.Column("self_reported_status", pg_enum("self_reported_status"), nullable=False),
        sa.Column("validated_status", pg_enum("validated_status"), nullable=True),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_artifact"),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_artifact_feature_request_id_feature_request",
        ),
        sa.ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
            name="fk_artifact_feature_request_id_task_card",
        ),
        sa.ForeignKeyConstraint(
            ["writer_agent_id"], ["agent_unit.agent_id"], name="fk_artifact_writer_agent_id_agent_unit"
        ),
    )


def downgrade() -> None:
    # 先删表（按建表的反序：先删依赖者再删被依赖者），再删本批引入的枚举类型。
    op.drop_table("artifact")
    op.drop_table("capability_manifest")
    op.drop_table("task_card")
    op.drop_table("agent_unit")
    op.drop_table("feature_request")
    for domain in ENUM_DOMAINS:
        op.execute(f"DROP TYPE IF EXISTS {enum_type_name(domain)}")
