"""迁移第九批：Phase 17 P3 —— EN-18 `review_report` + `deepseek-reviewer` 的审查挂载。

Revision ID: phase17_review_report
Revises: phase17_review_e_kind
Create Date: 2026-10-23

两件事：

1. **新表 `review_report`**（规划期称 EN-17，`data-model.md` 的 EN-17 已被 `deliverable` 占用
   ⇒ 在文档里编为 EN-18）。一行 = 一次自动审查尝试；`verdict_id` / `approval_ticket_id`
   恰好一个非空（PASS/FAIL 走 EN-08，NEED_HUMAN 走 EN-09）。不存原始 prompt/response 正文。
2. **把 `deepseek-reviewer` 追加进 `reviewer` 的生效挂载集**。这是**数据迁移**而不是种子变更：
   `role_spec` 在已运行的库里已有那一行，`seed_builtin_roles()` 对"内容不同"是响亮拒绝的
   （FR-071 的批准面不允许被种子静默覆盖）⇒ 老库 MUST 由迁移补这一格，否则下一次受理
   会在种子那里撞 `RoleSpecConflict`。空库上这一句是 no-op，随后种子按新 `MOUNTS` 插入四元组。

downgrade 先撤挂载、再删表：表是新的、无既有数据；挂载撤回不影响已经派出去的历史卡。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: `alembic_version.version_num` 是 `varchar(32)` ⇒ revision id 必须 ≤ 32 字符。
revision: str = "phase17_review_report"
down_revision: str | None = "phase17_review_e_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEWER = "reviewer"
REVIEW_IDENTITY = "deepseek-reviewer"


def upgrade() -> None:
    op.create_table(
        "review_report",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_card_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("reviewer_agent_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("model_decision", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("prompt_sha256", sa.Text(), nullable=True),
        sa.Column("raw_sha256", sa.Text(), nullable=True),
        sa.Column("raw_chars", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("verdict_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verdict_round_no", sa.Integer(), nullable=True),
        sa.Column("approval_ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_request_id"],
            ["feature_request.id"],
            name="fk_review_report_feature_request_id_feature_request",
        ),
        sa.ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
            name="fk_review_report_feature_request_id_task_card",
        ),
        sa.ForeignKeyConstraint(
            ["verdict_id"],
            ["review_verdict.id"],
            name="fk_review_report_verdict_id_review_verdict",
        ),
        sa.ForeignKeyConstraint(
            ["approval_ticket_id"],
            ["approval_ticket.id"],
            name="fk_review_report_approval_ticket_id_approval_ticket",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_report"),
        sa.UniqueConstraint(
            "feature_request_id",
            "task_card_id",
            "attempt_no",
            name="uq_review_report_attempt",
        ),
        sa.CheckConstraint("attempt_no > 0", name="ck_review_report_attempt_positive"),
        sa.CheckConstraint(
            "decision IN ('PASS', 'FAIL', 'NEED_HUMAN')",
            name="ck_review_report_decision_three_values",
        ),
        sa.CheckConstraint(
            "(verdict_id IS NULL) <> (approval_ticket_id IS NULL)",
            name="ck_review_report_result_exit_xor",
        ),
        sa.CheckConstraint(
            "(decision = 'NEED_HUMAN') = (reason IS NOT NULL)",
            name="ck_review_report_reason_iff_need_human",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(criteria) = 'array'", name="ck_review_report_criteria_is_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(issues) = 'array'", name="ck_review_report_issues_is_array"
        ),
        sa.CheckConstraint(
            "prompt_sha256 IS NULL OR length(prompt_sha256) = 64",
            name="ck_review_report_prompt_sha256_shape",
        ),
        sa.CheckConstraint(
            "raw_sha256 IS NULL OR length(raw_sha256) = 64",
            name="ck_review_report_raw_sha256_shape",
        ),
        sa.CheckConstraint("raw_chars >= 0", name="ck_review_report_raw_chars_non_negative"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_review_report_latency_ms_non_negative"),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_review_report_prompt_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_review_report_completion_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_review_report_total_tokens_non_negative",
        ),
    )
    op.create_index(
        "ix_review_report_card_created",
        "review_report",
        ["task_card_id", "created_at"],
        unique=False,
    )

    # 老库的 reviewer 行可能已经存在；只追加、不覆盖（申报/批准面的其余内容一字不动）。
    op.execute(
        sa.text(
            "UPDATE role_spec SET mounted_agents = array_append(mounted_agents, :identity) "
            "WHERE role_id = :role AND NOT (:identity = ANY(mounted_agents))"
        ).bindparams(identity=REVIEW_IDENTITY, role=REVIEWER)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE role_spec SET mounted_agents = array_remove(mounted_agents, :identity) "
            "WHERE role_id = :role"
        ).bindparams(identity=REVIEW_IDENTITY, role=REVIEWER)
    )
    op.drop_index("ix_review_report_card_created", table_name="review_report")
    op.drop_table("review_report")
