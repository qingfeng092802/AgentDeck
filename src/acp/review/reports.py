"""EN-18 `review_report` 的写入口与只读查询（Phase 17 D5）。

两条纪律写在这里，因为它们是本表存在的理由：

- **报告与结果同一事务**：调用方（`ReviewRunner`）在自己的会话里先写 EN-08/EN-09，再调
  `record()`；本模块只 `flush`，不 `commit`。报告写失败 ⇒ 整个工作单元回滚，EN-08 与 E 单
  一起消失，只留 runner 的 `review_auto_failed` 留痕（不出现"判决进了库、证据没进库"）。
- **不落原始正文**：只存结构化 criteria/issues 与 sha256/长度/延迟/token。原始 prompt/response
  只进 POC 证据目录（plan 附录 A），不入产品库、不进运行日志。

`attempt_no` 是**自动审查尝试序号**（`1 + 本卡已有报告数`），与 EN-08 的 `round_no`
（人工判决轮次）不是同一个计数；报告里同时记 `verdict_round_no` 以便对读。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from acp.adapters.deepseek import review as deepseek_review
from acp.persistence.models_review import DECISIONS, ReviewReport

#: 无模型调用的预检拒绝原因。它们仍写报告（否则"为什么没发模型"没有证据），但只落 reason。
NO_MODEL_REASONS = frozenset(
    {
        "artifact_absent",
        "artifact_unreadable",
        "criteria_empty",
        "prompt_too_large",
        "credential_suspect",
    }
)


class ReviewReportMisuse(ValueError):
    """报告写入的形状不合法（不是业务拒绝）。构造期就拒，避免把半行写进库。"""


def next_attempt_no(session: Session, feature_request_id: uuid.UUID, task_card_id: str) -> int:
    """下一次尝试序号。必须在 `session.add(row)` 之前调用（autoflush 会把新行数进去）。"""
    return 1 + int(
        session.scalar(
            select(func.count())
            .select_from(ReviewReport)
            .where(
                ReviewReport.feature_request_id == feature_request_id,
                ReviewReport.task_card_id == task_card_id,
            )
        )
        or 0
    )


def _criteria_of(result: deepseek_review.ReviewResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    return [
        {"criterion": item.criterion, "met": item.met, "evidence": item.evidence}
        for item in result.findings
    ]


def _issues_of(result: deepseek_review.ReviewResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    return [dict(item) for item in result.issues]


def record(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_card_id: str,
    reviewer_agent_id: str,
    decision: str,
    result: deepseek_review.ReviewResult | None,
    created_at: Any,
    reason: str | None = None,
    reason_detail: str | None = None,
    criteria: Sequence[dict[str, Any]] | None = None,
    issues: Sequence[dict[str, Any]] | None = None,
    verdict_id: uuid.UUID | None = None,
    verdict_round_no: int | None = None,
    approval_ticket_id: uuid.UUID | None = None,
) -> ReviewReport:
    """写一行报告并 `flush`。返回值是 ORM 行（调用方可以读 id / attempt_no）。

    `result=None` 表示本次**没有调用模型**（预检拒绝）：`model_id` 落 NULL，
    `criteria`/`issues` 由调用方给（通常是把卡上标准逐条折成 `met=None`），
    `prompt_sha256`/`raw_sha256` 落 NULL，`latency_ms`/`raw_chars` 落 0。
    """
    if decision not in DECISIONS:
        raise ReviewReportMisuse(f"decision={decision!r} 不在 {DECISIONS} 里")
    if (verdict_id is None) == (approval_ticket_id is None):
        raise ReviewReportMisuse(
            "verdict_id / approval_ticket_id 必须恰好一个非空（一次尝试一个结果出口）"
        )
    if (decision == "NEED_HUMAN") != (reason is not None):
        raise ReviewReportMisuse(
            f"decision={decision} 与 reason={reason!r} 不一致：只有 NEED_HUMAN 有 reason，且必须有"
        )
    if decision == "NEED_HUMAN" and not str(reason or "").strip():
        raise ReviewReportMisuse("NEED_HUMAN 的 reason 不能是空白串")
    if decision == "NEED_HUMAN" and verdict_id is not None:
        raise ReviewReportMisuse(
            "NEED_HUMAN 的报告只能挂在 E 单上；verdict_id 只用于自动 PASS/FAIL 那条出口（XOR）"
        )
    if decision != "NEED_HUMAN" and approval_ticket_id is not None:
        raise ReviewReportMisuse("PASS/FAIL 是决定性结论，不得挂 E 单")
    if result is None and decision != "NEED_HUMAN":
        raise ReviewReportMisuse("没有模型结果时只允许写 NEED_HUMAN（不得伪造 PASS/FAIL）")
    if result is not None and result.decision != decision:
        raise ReviewReportMisuse(
            f"报告 decision={decision} 与审查结果 {result.decision} 不一致"
        )

    row = ReviewReport(
        id=uuid.uuid4(),
        feature_request_id=feature_request_id,
        task_card_id=task_card_id,
        attempt_no=next_attempt_no(session, feature_request_id, task_card_id),
        reviewer_agent_id=reviewer_agent_id,
        model_id=(result.model_id if result is not None else None),
        decision=decision,
        model_decision=(result.model_decision if result is not None else None),
        reason=reason,
        reason_detail=reason_detail,
        criteria=list(criteria) if criteria is not None else _criteria_of(result),
        issues=list(issues) if issues is not None else _issues_of(result),
        prompt_sha256=(result.prompt_sha256 or None) if result is not None else None,
        raw_sha256=(result.raw_sha256 or None) if result is not None else None,
        raw_chars=(result.raw_chars if result is not None else 0),
        latency_ms=(result.latency_ms if result is not None else 0),
        prompt_tokens=(result.prompt_tokens if result is not None else None),
        completion_tokens=(result.completion_tokens if result is not None else None),
        total_tokens=(result.total_tokens if result is not None else None),
        verdict_id=verdict_id,
        verdict_round_no=verdict_round_no,
        approval_ticket_id=approval_ticket_id,
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return row


def for_card(
    session: Session,
    task_card_id: str,
    *,
    feature_request_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[ReviewReport]:
    """按 `created_at` 倒序读一张卡的报告。`limit` 的封顶由 HTTP 面负责（100）。"""
    query = select(ReviewReport).where(ReviewReport.task_card_id == task_card_id)
    if feature_request_id is not None:
        query = query.where(ReviewReport.feature_request_id == feature_request_id)
    query = query.order_by(ReviewReport.created_at.desc(), ReviewReport.attempt_no.desc()).limit(limit)
    return list(session.scalars(query).all())
