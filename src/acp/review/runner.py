"""Phase 17 P2：自动审查的后台跑者（PASS / FAIL / NEED_HUMAN 三条写路径）。

**触发面**：`WebResumer._run()` 在 `with new_session` 退出（= 提交）之后、且 per-feature 锁
释放之后调用本模块。传入的卡号是**本轮 `run_real` 产出的、`error is None` 且仍为 `REVIEWING`**
的那些（由 `WebResumer` 先筛一遍，本模块再逐个回库核一次）—— 不扫描"全部 REVIEWING"，
切模式之前就停在 `REVIEWING` 的卡不会被追溯（D6）。

**三条路径各有唯一出口**：

- `PASS`：`review_service.submit_review(verdict=APPROVE, accept_for=AutoReviewPassAcceptance(...))`
  —— `record_verdict` 写 EN-08 `APPROVE`，`run_accept` 用自动来源代填 EN-07 验收条目，
  留痕 mark 是 `review-auto-pass:deepseek-reviewer:<model_id>`，与人工的 `human-review-pass:*` 分开。
  但 `PASS` 之后还有一道**风险闸**（2026-10-28）：开关开 + 低风险 + 三岗互斥都成立才真的代签，
  否则退化成"建议"（见下一条）。
- `FAIL`：`submit_review(verdict=CHANGES_REQUESTED, findings=...)` —— 写 EN-08 并把卡打回，
  不验收、不组包。
- `NEED_HUMAN`：`review.record_verdict()` 写 EN-08 `NEED_HUMAN`（`met=null` 原样保留），
  再 `create_approval(kind=E_REVIEW_UNCERTAIN)` 建人工裁决单；卡留在 `REVIEWING`。
  已有 `PENDING`/`NEED_INFO` 的 E 单 ⇒ 跳过（`escalation_open`），不建第二张、也不再调模型。
- **建议**（`review_advisory_pass`，不是第四条结论出口）：模型给了 `PASS`、但风险闸不放行。
  **EN-08 / EN-18 一行都不写**（建议不是结论），只在 EN-14 里留"机器审过了、为什么没签"。
  卡留在 `REVIEWING`，等人。

**一条纪律**：本模块**永不向 `WebResumer` 抛异常**。每张卡一个自己的会话；单卡失败写一行
`review_auto_failed` 并把卡留在 `REVIEWING`，批量继续。失败不吞成 PASS。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.adapters.deepseek import review as deepseek_review
from acp.approval import service as approval_service
from acp.config import auto_approval
from acp.config import runtime as runtime_params
from acp.guard import triple
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import review as review_node
from acp.persistence import events
from acp.persistence import session as session_module
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.review import reports as review_reports
from acp.review import service as review_service

#: 审查席身份名的唯一来源 = 适配器模块。两处各写一份会在改名时让 runner 只处理空气。
DEEPSEEK_REVIEWER = deepseek_review.AGENT_ID

#: E 类的域值从审批服务那一份映射取，不在这里抄字面量。
ESCALATION_KIND = approval_service.KIND_BY_LETTER["E"]

#: 自动验收的 mark 前缀（HR-1 附一）。完整 mark = `review-auto-pass:<agent>:<model>`。
AUTO_MARK_PREFIX = "review-auto-pass"

#: 机器代签的留痕动作名（EN-14）。前端据此在流水线节点上打 `Auto-Approved` 徽章 ——
#: 这个名字是**界面与后端之间唯一的约定**，改它要同时改 `api/tasks.py::AUTO_MARKS` 与 `web/src`。
AUTO_ACCEPT_ACTION = "review_auto_accepted"

#: 机器审过、但这张卡不归机器签（风险档不够低 / 开关没开 / 三岗互斥不成立 / 配置坏了）。
#: 它**不写 EN-08 结论**：机器给的是建议，不是这一轮的结论。
ADVISORY_ACTION = "review_advisory_pass"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class AutoReviewPassAcceptance:
    """自动 PASS 的验收来源：条目全 `met=True`，但 mark 与人工来源 MUST 可区分。

    `AGENT_ID` 进 `run_accept` 的 `acceptance_items_proxy` 留痕（`run.py` 读这一格）；
    `MARK` 进 EN-07 的代填来源与 EN-14。两者都不使用 `human-review-pass:*`。
    """

    AGENT_ID = DEEPSEEK_REVIEWER

    def __init__(self, model_id: str, *, digest: str = "") -> None:
        self.model_id = model_id or "unknown"
        self.digest = digest
        self.MARK = f"{AUTO_MARK_PREFIX}:{self.AGENT_ID}:{self.model_id}"

    def verdicts_for(self, card, items: Sequence[str]) -> tuple[accept_node.ItemVerdict, ...]:
        note = f"自动审查通过（model={self.model_id}"
        if self.digest:
            note += f"，prompt_sha256={self.digest}"
        note += "）"
        return tuple(
            accept_node.ItemVerdict(
                acceptance_item=str(item),
                met=True,
                evidence=f"{note}：{card.id} 的验收条目由模型审查结论代填，不是独立验收判断",
            )
            for item in items
        )


@dataclass(frozen=True)
class ReviewBatch:
    """一次批量审查的读数。`continue_feature_ids` 只去重、不排序（顺序不影响续跑）。"""

    processed: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    escalated: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    continue_feature_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class _CardResult:
    kind: str
    reason: str = ""
    continue_feature_id: uuid.UUID | None = None


@dataclass
class ReviewRunner:
    """`WebResumer.review_runner` 的生产实现。`client` 是 `ReviewInput -> ReviewResult` 的可调用面。"""

    session_factory: Callable[[], Session]
    client: Callable[[deepseek_review.ReviewInput], deepseek_review.ReviewResult]
    bus_root: Path
    clock: Callable[[], dt.datetime] = utcnow
    emit_to: Callable[[str], object] = _stderr
    reviewer_agent_id: str = DEEPSEEK_REVIEWER

    def __call__(self, *, feature_request_id: Any, card_ids: Sequence[str]) -> ReviewBatch:
        fid = self._uuid(feature_request_id)
        processed: list[str] = []
        skipped: list[tuple[str, str]] = []
        escalated: list[str] = []
        failed: list[tuple[str, str]] = []
        followups: list[uuid.UUID] = []
        seen_cards: set[str] = set()

        for raw_id in card_ids:
            card_id = str(raw_id)
            if card_id in seen_cards:
                continue
            seen_cards.add(card_id)
            try:
                result = self._one(fid, card_id)
            except Exception as exc:  # noqa: BLE001 - 单卡失败不许拖垮整批
                failed.append((card_id, type(exc).__name__))
                self._emit(f"REVIEW fail :: card={card_id} :: {type(exc).__name__}: {exc}")
                self._trace(
                    fid,
                    card_id=card_id,
                    action="review_auto_failed",
                    code=type(exc).__name__,
                    error=str(exc),
                )
                continue
            if result.kind == "processed":
                processed.append(card_id)
            elif result.kind == "escalated":
                escalated.append(card_id)
            elif result.kind == "failed":
                failed.append((card_id, result.reason))
            else:
                skipped.append((card_id, result.reason))
                self._trace(
                    fid,
                    card_id=card_id,
                    action="review_auto_skipped",
                    reason=result.reason,
                )
            if result.continue_feature_id is not None and result.continue_feature_id not in followups:
                followups.append(result.continue_feature_id)

        return ReviewBatch(
            processed=tuple(processed),
            skipped=tuple(skipped),
            escalated=tuple(escalated),
            failed=tuple(failed),
            continue_feature_ids=tuple(followups),
        )

    # ------------------------------------------------------------------ 单卡

    def _one(self, fid: uuid.UUID, card_id: str) -> _CardResult:
        with session_module.new_session(self.session_factory) as session:
            card = session.get(TaskCard, (fid, card_id))
            if card is None:
                return _CardResult("skipped", "card_missing")
            if str(card.status) != review_node.REVIEWING:
                return _CardResult("skipped", f"status_{card.status}")
            if str(card.reviewer_agent_id or "") != self.reviewer_agent_id:
                return _CardResult("skipped", "reviewer_not_deepseek")
            existing = self._open_escalation(session, fid, card.id)
            if existing is not None:
                return _CardResult("skipped", "escalation_open")

            request = session.get(FeatureRequest, fid)
            if request is None:
                return _CardResult("skipped", "feature_request_missing")
            previews = review_service.artifact_previews(
                session,
                card,
                project_key=str(request.project_key),
                bus_root=self.bus_root,
            )
            if not previews:
                return self._escalate(
                    session,
                    fid,
                    card,
                    result=None,
                    reason="artifact_absent",
                    detail="卡上没有任何 valid 产物行 ⇒ 没有可审的正文",
                )
            unreadable = next((item for item in previews if item.content is None), None)
            if unreadable is not None:
                return self._escalate(
                    session,
                    fid,
                    card,
                    result=None,
                    reason="artifact_unreadable",
                    detail=f"{unreadable.rel_path}: {unreadable.content_error or 'read_failed'}",
                )

            try:
                review_input = deepseek_review.ReviewInput(
                    task_id=card.id,
                    title=str(card.title),
                    raw_input=str(request.raw_input),
                    acceptance_criteria=tuple(str(item) for item in (card.acceptance_criteria or ())),
                    artifacts=tuple(
                        deepseek_review.ReviewArtifact(
                            rel_path=str(item.rel_path),
                            filename=str(item.rel_path).replace("\\", "/").rsplit("/", 1)[-1],
                            format=str(item.format),
                            content=str(item.content),
                        )
                        for item in previews
                    ),
                )
            except deepseek_review.ReviewInputError as exc:
                reason = "criteria_empty" if "acceptance_criteria" in str(exc) else "artifact_unreadable"
                return self._escalate(
                    session, fid, card, result=None, reason=reason, detail=str(exc)
                )

            self._event(
                session,
                card,
                action="review_auto_started",
                extra={
                    "attempt": 1,
                    "artifact_count": len(previews),
                    "prompt_bytes": review_input.visible_bytes(),
                },
            )
            try:
                result = self.client(review_input)
            except Exception as exc:  # noqa: BLE001 - 模型客户端失败折成 failed，由人工接手
                self._event(
                    session,
                    card,
                    action="review_auto_failed",
                    extra={"code": type(exc).__name__, "error": str(exc)},
                )
                self._emit(f"REVIEW fail :: card={card.id} :: {type(exc).__name__}: {exc}")
                return _CardResult("failed", type(exc).__name__)
            if not isinstance(result, deepseek_review.ReviewResult):
                raise TypeError(
                    "审查客户端必须返回 ReviewResult；"
                    f"读到 {type(result).__name__} ⇒ 不把未知形状当结论"
                )
            if result.decision == deepseek_review.PASS:
                return self._accept(session, fid, card, result)
            if result.decision == deepseek_review.FAIL:
                return self._reject(session, fid, card, result)
            return self._escalate(
                session,
                fid,
                card,
                result=result,
                reason=result.reason or "contract_violation",
                detail=result.reason_detail,
            )

    # ------------------------------------------------------------------ 三条出口

    def _assignment(self, card: TaskCard) -> triple.Assignment:
        """把这张卡的三岗读成一份 `Assignment`，交给 `guard/triple.py` 那一份口径去判。

        **不自己写"三个 id 两两不同"**：那正是 FR-031 唯一的那一份判定。这里只负责把库里的列
        搬成它的入参（`role_id` 只携带、不参与判定，与那个模块的口径一致）。
        """
        return triple.Assignment(
            card_id=str(card.id),
            occupants={
                "executor": triple.SeatOccupant(card.executor_agent_id, card.executor_role_id),
                "reviewer": triple.SeatOccupant(card.reviewer_agent_id),
                "acceptor": triple.SeatOccupant(card.acceptor_agent_id),
            },
        )

    def _auto_accept_decision(self, card: TaskCard) -> auto_approval.Decision:
        """四条闸的判定。**任何一类配置错都朝"不许代签"倒**，不朝"当没配"倒。"""
        try:
            return auto_approval.decide(
                card,
                reviewer_pass=True,
                triple_reasons=triple.distinctness_violations(self._assignment(card)),
            )
        except (auto_approval.AutoApprovalConfigError, runtime_params.RuntimeParamsError) as exc:
            # 清单读不出 / 开关值不成形 / runtime.yaml 坏了：这一发**不代签**，但要说清为什么。
            # 反过来（当成"没开"静默继续）会让一次配置事故看起来像"今天恰好都要人签"。
            return auto_approval.Decision(
                allowed=False,
                code="config_error",
                risk_class=auto_approval.NEEDS_HUMAN,
                source=auto_approval.SOURCE_DEFAULT,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _advisory(
        self,
        session: Session,
        card: TaskCard,
        result: deepseek_review.ReviewResult,
        decision: auto_approval.Decision,
    ) -> _CardResult:
        """审查员 PASS，但这份不归机器签：留一条"机器审过了"的痕，**不写任何结论行**。

        为什么不写 EN-08（`review_verdict`）：那是"这一轮审查的结论"，一旦落库就占掉一轮
        （`review_round` 与 `check_findings` 都读它）。机器给的是一份**建议**，不是结论 ——
        写进去等于替人把结论定了、只是没替他签字，那种半截状态比不写更难解释。

        为什么连 EN-18（`review_report`）也不写：`reports.record()` 的准入条件是
        **`verdict_id` / `approval_ticket_id` 恰好一个非空**（一次尝试一个结果出口，附录 C 的 XOR）。
        建议这条路两个出口都没有 —— 没有 EN-08 可挂，也不该借一张 E 单来挂（那会凭空造出一张
        待办，等于把"机器审过了"变成"机器又把球踢回给人"，与设计相反）。
        硬要写就得放宽那条 XOR，而它守的正是"每行报告都能追到它是哪一次尝试的哪个结果"。
        ⇒ 这一格的唯一载体是 EN-14 `review_advisory_pass`：模型、摘要、成本、为什么没代签，全在里面。
        """
        self._event(
            session,
            card,
            action=ADVISORY_ACTION,
            extra={
                **decision.payload(),
                **self._result_fields(
                    result, verdict=review_node.APPROVE, card_status=str(card.status)
                ),
                # 这一发**花了钱但没进 EN-18**：成本账只能从这里查，所以整块搬过来，
                # 不挑字段（挑字段的下场是半年后没人说得清"那次建议到底调没调模型"）。
                # `report_skipped` 是一个显式的"没有报告行"，免得读日志的人以为报告写丢了。
                "report_skipped": "advisory_is_not_a_verdict",
            },
        )
        self._emit(
            f"REVIEW advise :: card={card.id} 机器审查通过但不代签（{decision.code}）"
            f" risk={decision.risk_class} switch={decision.source}"
        )
        return _CardResult("skipped", f"auto_accept_{decision.code}")

    def _accept(self, session: Session, fid: uuid.UUID, card: TaskCard, result: deepseek_review.ReviewResult) -> _CardResult:
        # **风险闸（2026-10-28）**：审查员给了 PASS，不等于机器可以替他签字。
        # 只有"开关开 + 这张卡属于低风险 + 三岗互斥成立"三条同时成立才往下走；
        # 否则只留一条"机器审过了，但这份得人签"的痕，卡留在 `REVIEWING`。
        decision = self._auto_accept_decision(card)
        if not decision.allowed:
            return self._advisory(session, card, result, decision)
        findings = self._findings_for(card, result)
        provider = AutoReviewPassAcceptance(
            result.model_id or "unknown", digest=result.prompt_sha256[:16]
        )
        outcome = review_service.submit_review(
            session,
            feature_request_id=fid,
            task_id=card.id,
            principal=self.reviewer_agent_id,
            verdict=review_node.APPROVE,
            findings=findings,
            evidence=f"自动审查通过（model={result.model_id or 'unknown'}）",
            bus_root=self.bus_root,
            now=self.clock(),
            accept_for=provider,
        )
        # 报告与 EN-08/验收同一事务：报告写失败 ⇒ 整个工作单元回滚（HR-3）。
        self._record_report(
            session,
            fid,
            card,
            result=result,
            decision=deepseek_review.PASS,
            verdict_id=outcome.verdict_id,
            verdict_round_no=outcome.round_no,
        )
        self._event(
            session,
            card,
            action="review_auto_finished",
            extra=self._result_fields(result, verdict=outcome.verdict, card_status=outcome.card_status),
        )
        followup = fid if outcome.accepted and outcome.delivery is None else None
        # 机器代签的**唯一可审计落点**：这张卡的验收不是人点出来的，前端据此打 `Auto-Approved` 徽章。
        # 四格（风险档 / 开关来源 / 审查模型 / 三岗）都写进去 —— 徽章要能回答"谁替谁签的、凭什么"。
        self._event(
            session,
            card,
            action=AUTO_ACCEPT_ACTION,
            extra={
                **decision.payload(),
                "verdict": outcome.verdict,
                "card_status": outcome.card_status,
                "model_id": result.model_id or "unknown",
                "prompt_sha256": result.prompt_sha256[:16],
                "triple": triple.triple_of(self._assignment(card)),
            },
        )
        self._emit(
            f"REVIEW pass :: card={card.id} verdict={outcome.verdict} "
            f"accepted={outcome.accepted} delivery={outcome.delivery is not None}"
        )
        return _CardResult("processed", "pass", followup)

    def _reject(self, session: Session, fid: uuid.UUID, card: TaskCard, result: deepseek_review.ReviewResult) -> _CardResult:
        findings = self._findings_for(card, result)
        outcome = review_service.submit_review(
            session,
            feature_request_id=fid,
            task_id=card.id,
            principal=self.reviewer_agent_id,
            verdict=review_node.CHANGES_REQUESTED,
            findings=findings,
            evidence=f"自动审查打回（model={result.model_id or 'unknown'}）",
            bus_root=self.bus_root,
            now=self.clock(),
        )
        self._record_report(
            session,
            fid,
            card,
            result=result,
            decision=deepseek_review.FAIL,
            verdict_id=outcome.verdict_id,
            verdict_round_no=outcome.round_no,
        )
        self._event(
            session,
            card,
            action="review_auto_finished",
            extra=self._result_fields(result, verdict=outcome.verdict, card_status=outcome.card_status),
        )
        self._emit(f"REVIEW fail :: card={card.id} verdict={outcome.verdict} 已打回返工")
        return _CardResult("processed", "fail")

    def _escalate(
        self,
        session: Session,
        fid: uuid.UUID,
        card: TaskCard,
        *,
        result: deepseek_review.ReviewResult | None,
        reason: str,
        detail: str,
    ) -> _CardResult:
        findings = self._escalation_findings(card, result, detail)
        record = review_node.record_verdict(
            session,
            feature_request_id=fid,
            task_id=card.id,
            reviewer_agent_id=self.reviewer_agent_id,
            findings=findings,
            created_at=self.clock(),
            actor_agent_id=self.reviewer_agent_id,
        )
        payload = approval_service.build_payload(
            kind=ESCALATION_KIND,
            pending_item=f"审查不确定：{card.title}",
            context_digest=hashlib.sha256(
                f"{fid}:{card.id}:{reason}:{detail}".encode("utf-8")
            ).hexdigest()[:16],
            suggested_verdict="need_info",
            impact={"downstream_task_ids": [], "estimated_extra_usage": {}},
        )
        ticket = approval_service.create_approval(
            session,
            kind=ESCALATION_KIND,
            payload=payload,
            feature_request_id=fid,
            task_card_id=card.id,
            now=self.clock(),
        )
        self._record_report(
            session,
            fid,
            card,
            result=result,
            decision=deepseek_review.NEED_HUMAN,
            reason=reason,
            reason_detail=detail,
            criteria=[
                {"criterion": item.criterion, "met": item.met, "evidence": item.evidence}
                for item in findings
            ],
            issues=[dict(item) for item in (result.issues if result is not None else ())],
            # `verdict_id` 只留给自动 PASS/FAIL 那条出口（附录 C 的 XOR）：NEED_HUMAN 的 EN-08 行
            # 用 `verdict_round_no` + E 单关联，不把两列同时填上。
            verdict_id=None,
            verdict_round_no=record.round_no,
            approval_ticket_id=ticket.ticket_id,
        )
        extra: dict[str, Any] = {
            "verdict": record.verdict,
            "card_status": str(card.status),
            "reason": reason,
            "reason_detail": detail,
            "ticket_id": str(ticket.ticket_id),
            "ticket_state": ticket.state,
            "merged": ticket.merged,
            "attempt": 1,
        }
        if result is not None:
            extra.update(self._result_fields(result, verdict=record.verdict, card_status=str(card.status)))
        self._event(session, card, action="review_escalated", extra=extra)
        self._emit(
            f"REVIEW escalate :: card={card.id} reason={reason} ticket={ticket.ticket_id} "
            f"state={ticket.state}"
        )
        return _CardResult("escalated", reason)

    # ------------------------------------------------------------------ 取数与留痕

    def _record_report(
        self,
        session: Session,
        fid: uuid.UUID,
        card: TaskCard,
        *,
        result: deepseek_review.ReviewResult | None,
        decision: str,
        verdict_id: uuid.UUID | None = None,
        verdict_round_no: int | None = None,
        approval_ticket_id: uuid.UUID | None = None,
        reason: str | None = None,
        reason_detail: str | None = None,
        criteria: Sequence[dict[str, Any]] | None = None,
        issues: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """在**当前工作单元**里写 EN-18。本模块不 commit；回滚由 `new_session` 统一做。"""
        review_reports.record(
            session,
            feature_request_id=fid,
            task_card_id=card.id,
            reviewer_agent_id=self.reviewer_agent_id,
            decision=decision,
            result=result,
            created_at=self.clock(),
            reason=reason,
            reason_detail=reason_detail,
            criteria=criteria,
            issues=issues,
            verdict_id=verdict_id,
            verdict_round_no=verdict_round_no,
            approval_ticket_id=approval_ticket_id,
        )

    def _findings_for(
        self, card: TaskCard, result: deepseek_review.ReviewResult
    ) -> tuple[tuple[str, bool, str], ...]:
        by_criterion = {item.criterion: item for item in result.findings}
        out: list[tuple[str, bool, str]] = []
        for criterion in (str(item) for item in (card.acceptance_criteria or ())):
            item = by_criterion.get(criterion)
            if item is not None:
                out.append((criterion, item.met, item.evidence))
        return tuple(out)

    def _escalation_findings(
        self,
        card: TaskCard,
        result: deepseek_review.ReviewResult | None,
        detail: str,
    ) -> tuple[review_node.ReviewFinding, ...]:
        """NEED_HUMAN 的逐条判定：覆盖卡上全部标准，且至少一条 `met=None`。

        `record_verdict` 从 findings 推结论：全 true 会推出 APPROVE。模型宣布不确定但每条都写
        true 时，平台不采纳那批判定（`ensure_uncertain` 统一折成 null），否则"不确定"会被静默放行。
        """
        declared = [str(item) for item in (card.acceptance_criteria or ())]
        by_criterion = {item.criterion: item for item in (result.findings if result else ())}
        provisional = [
            deepseek_review.Finding(
                criterion,
                by_criterion[criterion].met if criterion in by_criterion else None,
                by_criterion[criterion].evidence
                if criterion in by_criterion
                else f"平台未获得该标准的可判定依据：{detail}",
            )
            for criterion in declared
        ]
        if not provisional:
            raise review_node.ReviewMisuse(
                f"卡 {card.id} 的 `acceptance_criteria` 为空 ⇒ E 单没有可挂的审查标准",
                code="criteria_empty",
            )
        normalized = deepseek_review.ensure_uncertain(provisional, detail or "模型未给出可判定结论")
        return tuple(
            review_node.ReviewFinding(item.criterion, item.met, item.evidence) for item in normalized
        )

    def _open_escalation(self, session: Session, fid: uuid.UUID, card_id: str) -> ApprovalTicket | None:
        return session.scalars(
            select(ApprovalTicket).where(
                ApprovalTicket.feature_request_id == fid,
                ApprovalTicket.task_card_id == card_id,
                ApprovalTicket.kind == ESCALATION_KIND,
                ApprovalTicket.state.in_(("PENDING", "NEED_INFO")),
            )
        ).first()

    def _result_fields(
        self, result: deepseek_review.ReviewResult, *, verdict: str, card_status: str
    ) -> dict[str, Any]:
        return {
            "verdict": verdict,
            "card_status": card_status,
            "model_decision": result.model_decision,
            "model_id": result.model_id,
            "attempts": result.attempts,
            "http_status": result.http_status,
            "latency_ms": result.latency_ms,
            "prompt_sha256": result.prompt_sha256,
            "raw_sha256": result.raw_sha256,
            "raw_chars": result.raw_chars,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "reason": result.reason,
            "reason_detail": result.reason_detail,
        }

    def _event(
        self, session: Session, card: TaskCard, *, action: str, extra: dict[str, Any] | None = None
    ) -> None:
        events.record(
            session,
            entity="task_card",
            entity_id=card.id,
            kind="REVIEW",
            actor_agent_id=self.reviewer_agent_id,
            payload={"action": action, "feature_request_id": str(card.feature_request_id), **(extra or {})},
            created_at=self.clock(),
        )

    def _trace(self, fid: uuid.UUID, *, card_id: str | None, action: str, **fields: Any) -> None:
        """失败/跳过留痕单独开会话：主工作单元回滚之后这一行仍要留下。"""
        try:
            with session_module.new_session(self.session_factory) as session:
                events.record(
                    session,
                    entity="task_card" if card_id else "feature_request",
                    entity_id=card_id or str(fid),
                    kind="REVIEW",
                    actor_agent_id=self.reviewer_agent_id,
                    payload={
                        "action": action,
                        "feature_request_id": str(fid),
                        **({"card_id": card_id} if card_id else {}),
                        **fields,
                    },
                    created_at=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 留痕写不进去不能反过来把审查判成失败
            self._emit(f"REVIEW trace-failed :: {action} :: {type(exc).__name__}: {exc}")

    @staticmethod
    def _uuid(value: Any) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    def _emit(self, line: str) -> None:
        try:
            self.emit_to(f"[deepseek-review] {line}")
        except Exception:  # noqa: BLE001 - 诊断输出不许影响审查
            pass
