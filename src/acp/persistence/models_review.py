"""EN-18 `review_report`：一次自动审查尝试的完整审计行（Phase 17 P1/P3 D5）。

**编号说明**：`data-model.md` 的 EN-17 已被 `deliverable` 占用；`phase-17-plan.md` 附录 C
按规划期的编号称它为 EN-17。代码与文档以**表名 `review_report`** 为唯一锚点，本文件在
`data-model.md` 里编为 **EN-18**，并注明与规划编号的对应关系（不制造第二个 EN-17）。

一行 = 一次自动审查尝试（PASS / FAIL / NEED_HUMAN 都写）。两条结果出口**恰好一个**非空：
自动判决走 `verdict_id`（EN-08），不确定升级走 `approval_ticket_id`（EN-09）。MUST NOT 两列都空
（"审了但没结果"）或都非空（一次尝试两个出口）。

不存原始 prompt / 原始响应正文：只存 sha256、长度、延迟、token 计数与结构化 criteria/issues。
原始正文只进 POC 证据目录（附录 A），不入产品库、不进运行日志。
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import mapped_column

from acp.persistence.base import Base

DECISIONS = ("PASS", "FAIL", "NEED_HUMAN")


class ReviewReport(Base):
    """EN-18：自动审查报告。`attempt_no` 是自动尝试序号，不是 EN-08 的人工判决轮次。"""

    __tablename__ = "review_report"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
        ),
        UniqueConstraint(
            "feature_request_id", "task_card_id", "attempt_no", name="uq_review_report_attempt"
        ),
        CheckConstraint("attempt_no > 0", name="attempt_positive"),
        CheckConstraint(
            "decision IN ('PASS', 'FAIL', 'NEED_HUMAN')", name="decision_three_values"
        ),
        # 一条报告恰好有一个结果出口（附录 C 的 XOR）。
        CheckConstraint(
            "(verdict_id IS NULL) <> (approval_ticket_id IS NULL)", name="result_exit_xor"
        ),
        CheckConstraint(
            "(decision = 'NEED_HUMAN') = (reason IS NOT NULL)", name="reason_iff_need_human"
        ),
        CheckConstraint("jsonb_typeof(criteria) = 'array'", name="criteria_is_array"),
        CheckConstraint("jsonb_typeof(issues) = 'array'", name="issues_is_array"),
        CheckConstraint(
            "prompt_sha256 IS NULL OR length(prompt_sha256) = 64", name="prompt_sha256_shape"
        ),
        CheckConstraint("raw_sha256 IS NULL OR length(raw_sha256) = 64", name="raw_sha256_shape"),
        CheckConstraint("raw_chars >= 0", name="raw_chars_non_negative"),
        CheckConstraint("latency_ms >= 0", name="latency_ms_non_negative"),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="completion_tokens_non_negative",
        ),
        CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="total_tokens_non_negative"),
        # 端点按 created_at 倒序读；B-tree 正扫可反向走，不为此引入表达式索引。
        Index("ix_review_report_card_created", "task_card_id", "created_at"),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False
    )
    task_card_id = mapped_column(Text, nullable=False)
    attempt_no = mapped_column(Integer, nullable=False)
    reviewer_agent_id = mapped_column(Text, nullable=False)
    # NULL = 本次没有调用模型（artifact_absent / criteria_empty 等预检拒绝）。
    # 文档附录 C 写的是非空，但它同时允许"无模型调用"的报告行 —— 给一个假模型名会把
    # "没发出去"伪装成"发出去了"，所以这里取可空并让读取面把 NULL 读成"未调用"。
    model_id = mapped_column(Text, nullable=True)
    decision = mapped_column(Text, nullable=False)
    model_decision = mapped_column(Text, nullable=True)
    reason = mapped_column(Text, nullable=True)
    reason_detail = mapped_column(Text, nullable=True)
    criteria = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    issues = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    prompt_sha256 = mapped_column(Text, nullable=True)
    raw_sha256 = mapped_column(Text, nullable=True)
    raw_chars = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_ms = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_tokens = mapped_column(Integer, nullable=True)
    completion_tokens = mapped_column(Integer, nullable=True)
    total_tokens = mapped_column(Integer, nullable=True)
    verdict_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_verdict.id"), nullable=True
    )
    verdict_round_no = mapped_column(Integer, nullable=True)
    approval_ticket_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_ticket.id"), nullable=True
    )
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
