"""人工评审的写入口（M2-W8）：结论入库、卡进验收、全交付后组包。

这一格此前是空的：`ReviewVerdict` 全仓唯一构造点在 `nodes/review.py`，而 `realrun` 跑到门禁就停、
不留任何"收结论"的通道 ⇒ 卡永远停在 `RUNNING` + `validated_status=valid`。本模块补的三件事各只有一处：

1. **打开评审**：`RUNNING` + 至少一行 valid 产物的卡必须先到 `REVIEWING` 才收结论。新真跑由
   `realrun` 的门禁停点直接推到 `REVIEWING`；本模块对旧现场（代码升级前跑出的 `RUNNING`）做一次补开，
   两处共用 `review.ACTION_OPENED` 这一个动作名。
2. **收结论**：结论的完整性与两类取值仍由 `review.check_findings()` 判，本模块不重写第二份。
   人点"通过"时按卡上声明的 `acceptance_criteria` 逐条展开为 `met=True`（判定来自人工，不是平台自拟）；
   "打回"则要求显式交回逐条判定，至少一条 `met=False`。
3. **过审之后的自动流转**：`REVIEWING --APPROVE--> ACCEPTING` 由 `record_verdict` 写；接着用
   `HumanPassAcceptance` 把 EN-07 的验收条目逐条交回 `run_accept`（全 `met=True`，`MARK` 明写
   `human-review-pass:<principal>`），于是 `accept()` 落 `DELIVERED` 并留下 `acceptance_items_proxy` 留痕。
   **这一支不是 FR-051 / FR-052 的实证**：验收条目是按"人已点过通过"这一事实展开的，
   与产物内容的因果关系由人工评审本身承担。全需求单的卡都到终态时再调 `run_deliver` 组唯一一份交付包。

诚实边界：自动验收用的是**验收席身份 + 代填留痕**（与 `run.StubAcceptanceProvider` 同一格审计形状），
不是让平台替验收岗做独立判断。要独立的验收结论，得另开一条验收入口；本模块不把这条说成已经有了。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from acp import run
from acp.archive import root as archive
from acp.docs import PRD_SECTION
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import assemble as assemble_node
from acp.orchestrate.nodes import review as review_node
from acp.persistence import events
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Deliverable
from acp.reporting.checks import run_checks
from acp.reporting.gate import write_acceptance_card, write_delivery_card

#: 请求侧的短词 → EN-08 的域值。域值本体仍只从 `review.VERDICTS` 来，这里只做别名翻译。
VERDICT_WORDS: dict[str, str] = {
    "pass": review_node.APPROVE,
    "approve": review_node.APPROVE,
    "APPROVE": review_node.APPROVE,
    "changes_requested": review_node.CHANGES_REQUESTED,
    "changes": review_node.CHANGES_REQUESTED,
    "reject": review_node.CHANGES_REQUESTED,
    "CHANGES_REQUESTED": review_node.CHANGES_REQUESTED,
}

#: 旧现场补开评审时门禁已经写进库的终态（`validate_artifact.VALID` 的字面值）。
RUNNING = "RUNNING"
VALID = "valid"

#: 入口页名的唯一载体在 `run.py`（装配点与端点都从这里取，不各写一份字面量）。
DEFAULT_ENTRY_NAME = run.DEFAULT_ENTRY_NAME


def entry_name_from_digest(digest: object) -> str | None:
    """从 `summary_digest["PRD"]` 的第一页推入口名。读不到 / 坏了都返回 `None`。

    为什么要有这条线（U2，2026-10-28 真跑）：入口名原来只有两个来源 —— 请求体里的
    `entry_name` 和启动参数 `--review-entry`（默认 `index.html`）。而 PRD 说页面是
    `dashboard.html`。于是"入口名"和"需求原文"各说各话，组包 `entry_absent`，
    交付包永远出不来。**PRD 的第一页就是入口**，这是需求自己写的，不是我们猜的 ——
    所以读得到就用它，读不到才轮到启动参数那档兜底。

    只认第一页，不做"哪个像首页"的启发式：R-07 的口径是"入口由调用方点名，装配点不猜"，
    按 PRD 的页序取第一页是**点名**，挑一个"看起来最像首页的"才是猜。
    """
    if not isinstance(digest, Mapping):
        return None
    values = digest.get(PRD_SECTION) or ()
    if not isinstance(values, (list, tuple)) or not values:
        return None
    try:
        parsed = json.loads(str(values[0]))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    pages = parsed.get("pages")
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], Mapping):
        return None
    return str(pages[0].get("file") or "").strip() or None


def entry_name_for(session, feature_request_id: uuid.UUID) -> str | None:
    """读需求单的摘要，取 PRD 第一页的文件名。单子不存在 / 没有 PRD 段都返回 `None`。

    **不抛异常**：入口名是"能推就推"的便利，推不出来还有 `--review-entry` 与
    `DEFAULT_ENTRY_NAME` 两档兜底；为了推不出一个名字而让"通过"这颗按钮 500，
    是拿一个可选优化挡住主流程。
    """
    row = session.get(FeatureRequest, feature_request_id)
    return entry_name_from_digest(getattr(row, "summary_digest", None) if row is not None else None)


#: 评审面板预览产物的上限：单文件 256 KiB、每卡最多 8 份。超限只回 `content_error`，不截半份源码 ——
#: 一份被截断的 CSS 读起来像"模型生成坏了"，而真正发生的是"这一份不该在网页里整段传"。
MAX_PREVIEW_BYTES = 256 * 1024
MAX_PREVIEW_FILES = 8


class ReviewFlowError(ValueError):
    """评审入口的用法错。`status_code` 是 HTTP 面要回的那一档，`code` 是稳定键名。"""

    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class PendingArtifact:
    """一张待审卡的一份已验证产物。`content` 读不出来时为空，原因落 `content_error`。"""

    task_id: str
    writer_agent_id: str
    rel_path: str
    format: str
    bytes: int
    lines: int
    content: str | None
    content_error: str | None


@dataclass(frozen=True)
class PendingReview:
    task_id: str
    feature_request_id: uuid.UUID
    title: str
    status: str
    reviewer_agent_id: str | None
    acceptor_agent_id: str | None
    acceptance_criteria: tuple[str, ...]
    artifact_count: int
    requires_open: bool
    artifacts: tuple[PendingArtifact, ...] = ()


@dataclass(frozen=True)
class DeliveryView:
    deliverable_id: uuid.UUID
    entry_path: str
    manifest_count: int
    cards_delivered: int
    package_path: str
    package_bytes: int


@dataclass(frozen=True)
class ReviewOutcome:
    task_id: str
    feature_request_id: uuid.UUID
    project_key: str
    verdict: str
    card_status: str
    round_no: int
    findings: int
    accepted: bool
    acceptance_proxy: str | None
    accept_error: str | None
    delivery: DeliveryView | None
    delivery_skipped: str | None
    delivery_error: str | None
    next_hint: str
    #: 这次结论写入的 EN-08 行号（Phase 17 P3：报告用 `verdict_id` 指向它，XOR 的一侧）。
    verdict_id: uuid.UUID


class HumanPassAcceptance:
    """把"人已点过通过"这件事按 EN-07 的条目逐条展开，交回 `run_accept`。

    `MARK` 进 `acceptance_items_proxy` 留痕的 `provider_mark`：查库时能一眼分出"这一轮验收是人工过审
    代填的"，而不是一次独立的验收判断。证据串里带 principal，四个人同时开看板时也分得清是谁点的。
    """

    def __init__(self, principal: str, evidence: str = "") -> None:
        self.principal = principal
        self.evidence = evidence.strip()
        self.MARK = f"human-review-pass:{principal}"

    def verdicts_for(self, card, items: Sequence[str]) -> tuple[accept_node.ItemVerdict, ...]:
        note = self.evidence or "（未附说明）"
        prefix = f"{self.MARK}：人工评审通过，卡 {card.id} 的验收条目逐条代填"
        return tuple(
            accept_node.ItemVerdict(
                acceptance_item=str(item),
                met=True,
                evidence=f"{prefix}：{note}",
            )
            for item in items
        )


class GatePassAcceptance:
    """`ACP_REVIEW_MODE=auto` 的验收来源：**门禁判产物达标**，没有独立审查、也没有人。

    `MARK` 与另外两个来源刻意三者互不相同，查库时一眼分得清这一轮验收是谁放行的：

    | 来源 | mark |
    |---|---|
    | 人工过审 | `human-review-pass:<principal>` |
    | 模型审查通过 | `review-auto-pass:deepseek-reviewer:<model>` |
    | **门禁代签** | `gate-review-pass:artifact-gate` |

    为什么要单列一格而不是复用上面那条：`review-auto-pass` 的证据串写的是"由模型审查结论代填"，
    而这条路**根本没有那份结论** —— 借那条 mark 会在审计里凭空长出一个没发生过的模型调用。

    证据串里明写"不是独立验收判断"：这张卡的 `acceptance_criteria` 逐条 `met=True` 是**代填**的，
    真实判据只有门禁那三条（格式 / 字节下限 / 行数下限），两者不是同一件事，不能让人读混。
    """

    MARK = "gate-review-pass:artifact-gate"
    #: 写进 `acceptance_items_proxy` 留痕的身份（`run.py` 读这一格）。
    AGENT_ID = "artifact-gate"

    def verdicts_for(self, card, items: Sequence[str]) -> tuple[accept_node.ItemVerdict, ...]:
        note = (
            f"{self.MARK}：门禁判产物达标即视为通过（ACP_REVIEW_MODE=auto），"
            f"卡 {card.id} 的验收条目由门禁代填，不是独立验收判断"
        )
        return tuple(
            accept_node.ItemVerdict(acceptance_item=str(item), met=True, evidence=note)
            for item in items
        )


def _card(session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise ReviewFlowError(
            f"需求单 {feature_request_id} 下没有卡 {task_id}",
            code="task_not_found",
            status_code=404,
        )
    return card


def _valid_artifacts(session, feature_request_id: uuid.UUID, task_id: str) -> int:
    return (
        session.query(Artifact)
        .filter(
            Artifact.feature_request_id == feature_request_id,
            Artifact.task_card_id == task_id,
            Artifact.validated_status == VALID,
        )
        .count()
    )


def open_review_if_needed(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    principal: str,
    now: dt.datetime,
) -> TaskCard:
    """把卡放到 `REVIEWING`：新现场已经在那一格，旧现场（`RUNNING` + valid 产物）补开一次。

    补开的条件只认"门禁真的写过 valid 产物行"，不认"卡看起来跑完了" —— 后者会把一张门禁没过的卡
    拖进评审。状态不是这两种就拒，且拒之前一个字都不写。
    """
    card = _card(session, feature_request_id, task_id)
    status = str(card.status)
    if status == review_node.REVIEWING:
        return card
    if status != RUNNING:
        raise ReviewFlowError(
            f"卡 {task_id} 现在是 {status}，评审结论只在 {review_node.REVIEWING} 上收"
            "（旧现场可由 RUNNING + valid 产物补开）",
            code="state_not_reviewing",
            status_code=409,
        )
    if _valid_artifacts(session, feature_request_id, task_id) < 1:
        raise ReviewFlowError(
            f"卡 {task_id} 停在 {RUNNING} 但没有任何 valid 产物行 ⇒ 门禁没判过，不能进评审",
            code="artifact_not_valid",
            status_code=409,
        )
    if not card.reviewer_agent_id:
        raise ReviewFlowError(
            f"卡 {task_id} 没有审查岗 ⇒ 结论没有可归属的席位",
            code="reviewer_not_assigned",
            status_code=409,
        )
    card.status = review_node.REVIEWING
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="REVIEW",
        actor_agent_id=principal,
        payload={
            "node": review_node.NODE,
            "action": review_node.ACTION_OPENED,
            "prior_status": status,
            "card_status_after": review_node.REVIEWING,
        },
        created_at=now,
    )
    session.flush()
    return card


def _requested_verdict(word: str) -> str:
    try:
        return VERDICT_WORDS[word]
    except KeyError:
        raise ReviewFlowError(
            f"不认识评审结论 {word!r}，可写的只有 {sorted(VERDICT_WORDS)}",
            code="verdict_unknown",
        ) from None


def _findings_of(
    card: TaskCard,
    *,
    requested: str,
    supplied: Sequence[tuple[str, bool, str]] | None,
    evidence: str,
    principal: str,
) -> tuple[review_node.ReviewFinding, ...]:
    criteria = tuple(str(item) for item in (card.acceptance_criteria or ()))
    if not criteria:
        raise ReviewFlowError(
            f"卡 {card.id} 的 `acceptance_criteria` 为空 ⇒ 没有可逐条比对的标准",
            code="criteria_empty",
        )
    note = evidence.strip() or f"{principal} 人工评审"
    if requested == review_node.APPROVE:
        if supplied is None:
            return tuple(
                review_node.ReviewFinding(criterion=item, met=True, evidence=note) for item in criteria
            )
        findings = tuple(
            review_node.ReviewFinding(criterion=criterion, met=met, evidence=text or note)
            for criterion, met, text in supplied
        )
        if not all(item.met for item in findings):
            raise ReviewFlowError(
                "请求写的是通过，却交了 met=false 的判定 ⇒ 先改结论词，别让响应体替判定说话",
                code="verdict_findings_conflict",
            )
        return findings
    if not supplied:
        raise ReviewFlowError(
            "打回结论必须交回逐条判定（至少一条 met=false）⇒ 不服务端猜哪一条没过",
            code="findings_required",
        )
    findings = tuple(
        review_node.ReviewFinding(criterion=criterion, met=met, evidence=text or note)
        for criterion, met, text in supplied
    )
    if all(item.met for item in findings):
        raise ReviewFlowError(
            "请求写的是打回，却所有条目都 met=true ⇒ 先改结论词",
            code="verdict_findings_conflict",
        )
    return findings


def _delivery_view(session, row: Deliverable, *, feature_request_id: uuid.UUID, project_root: Path) -> DeliveryView:
    delivered = (
        session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status.in_(assemble_node.DELIVERED_STATES),
        )
        .count()
    )
    package = project_root / assemble_node.delivery_path()
    return DeliveryView(
        deliverable_id=row.id,
        entry_path=str(row.entry_path),
        manifest_count=len(list(row.manifest or ())),
        cards_delivered=delivered,
        package_path=assemble_node.delivery_path(),
        package_bytes=package.stat().st_size if package.is_file() else 0,
    )


def _deliver_if_complete(
    session,
    *,
    feature_request_id: uuid.UUID,
    project_key: str,
    bus_root: Path | None,
    entry_name: str,
    now: dt.datetime,
) -> tuple[DeliveryView | None, str | None, str | None]:
    """全需求单的卡都到终态才组包；任何一步失败都只回读数，不回滚已落库的评审结论。"""
    cards = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .all()
    )
    delivered = [card for card in cards if str(card.status) in assemble_node.DELIVERED_STATES]
    if not cards:
        return None, "cards_absent", None
    if len(delivered) < len(cards):
        return None, f"cards_pending:{len(cards) - len(delivered)}", None
    # P1-6：卡全到终态就先把需求单状态对齐。组包还可能失败（artifact_stale / 无 bus_root），
    # 但"卡都交付了，单子还在 SPLIT"是另一个假读数 —— 它会让看板一直说"还在跑"。
    # 局部 import 避免 `realrun -> review.service` 的反向依赖成环。
    from acp import realrun as realrun_module

    realrun_module.sync_request_status(session, feature_request_id, now=now)
    project_root = archive.archive_root(project_key, bus_root=bus_root) if bus_root is not None else None
    existing = (
        session.query(Deliverable)
        .filter(Deliverable.feature_request_id == feature_request_id)
        .one_or_none()
    )
    if existing is not None:
        if project_root is not None:
            return (
                _delivery_view(
                    session, existing, feature_request_id=feature_request_id, project_root=project_root
                ),
                "already_assembled",
                None,
            )
        return None, "already_assembled", None
    if bus_root is None:
        return None, "bus_root_not_configured", None
    # 三个确定性检查器（E2E / 安全 / 门禁）：报告**要进交付包**，所以必须在组包之前落库并判完。
    # `validated_manifest()` 只收已判 `valid` 的产物行 —— 放到 `run_deliver` 之后，这一包就永远不含它们。
    # 位置也在 `bus_root` 那道检查之后：没有存档根就没有产物目录，检查器无从读文件。
    request_row = session.get(FeatureRequest, feature_request_id)
    run_checks(
        session,
        feature_request_id=feature_request_id,
        project_root=project_root,
        summary_digest=(request_row.summary_digest or {}) if request_row is not None else {},
    )
    # 验收汇总卡：能走到这里就说明所有卡都已到终态，验收结论已经全在 EN-07 / EN-08 里。
    # 它与三张检查卡同在组包之前 —— 都是"交付前必须已经发生、但之前板上看不见"的事。
    write_acceptance_card(session, feature_request_id=feature_request_id, project_root=project_root)
    try:
        report = run.run_deliver(
            feature_request_id,
            transport="stub",
            bus_root=bus_root,
            session=session,
            now=now,
            entry_name=entry_name,
        )
    except (run.RunFailure, assemble_node.AssembleMisuse) as exc:
        code = getattr(exc, "code", "assemble_misuse")
        return None, None, f"{code}: {exc}"
    # 打包卡：包已经写完、读数已经从盘上量过，这里只记读数。
    # 它**不可能**出现在自己描述的那个包里（`assemble()` 读的是组包那一刻的清单），
    # 这是"清单不能包含它自己"，不是缺陷。
    write_delivery_card(
        session,
        feature_request_id=feature_request_id,
        project_root=project_root,
        report=report,
    )
    return (
        DeliveryView(
            deliverable_id=report.deliverable_id,
            entry_path=report.entry_path,
            manifest_count=report.manifest_count,
            cards_delivered=report.cards_delivered,
            package_path=report.package_path,
            package_bytes=report.package_bytes,
        ),
        None,
        None,
    )


def submit_review(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    principal: str,
    verdict: str,
    findings: Sequence[tuple[str, bool, str]] | None = None,
    evidence: str = "",
    bus_root: Path | None = None,
    entry_name: str = "index.html",
    now: dt.datetime | None = None,
    accept_for: object | None = None,
) -> ReviewOutcome:
    """收一条人工结论；通过则自动验收、全交付后组包。返回的读数全部来自库里的现值。

    `accept_for` 是验收来源的注入位：缺省仍是 `HumanPassAcceptance`（Phase 16 逐字不变）；
    `ReviewRunner` 传 `AutoReviewPassAcceptance`，把"模型审查通过"的审计 mark 与人工路径分开。
    """
    stamp = now or dt.datetime.now(dt.timezone.utc)
    requested = _requested_verdict(verdict)
    card = open_review_if_needed(
        session,
        feature_request_id=feature_request_id,
        task_id=task_id,
        principal=principal,
        now=stamp,
    )
    prepared = _findings_of(
        card,
        requested=requested,
        supplied=findings,
        evidence=evidence,
        principal=principal,
    )
    derived, reasons = review_node.check_findings(card, prepared)
    if derived is None:
        raise ReviewFlowError(
            f"卡 {task_id} 的审查结论不入库：{'；'.join(reasons)}",
            code="findings_not_compliant",
        )
    if derived != requested:
        raise ReviewFlowError(
            f"结论词是 {requested}，而逐条判定推出 {derived} ⇒ 两者必须一致",
            code="verdict_findings_conflict",
        )
    try:
        record = review_node.record_verdict(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            reviewer_agent_id=str(card.reviewer_agent_id),
            findings=prepared,
            created_at=stamp,
            actor_agent_id=principal,
        )
    except review_node.ReviewMisuse as exc:
        status_code = 409 if exc.code in {"state_not_reviewing", "reviewer_mismatch", "card_not_found"} else 422
        raise ReviewFlowError(str(exc), code=exc.code, status_code=status_code) from exc

    accepted = False
    accept_error: str | None = None
    proxy: str | None = None
    if record.verdict == review_node.APPROVE:
        provider = accept_for if accept_for is not None else HumanPassAcceptance(principal, evidence)
        proxy = str(getattr(provider, "MARK", "")) or None
        try:
            run.run_accept(
                feature_request_id,
                task_id,
                transport="stub",
                session=session,
                now=stamp,
                accept_for=provider,
            )
            accepted = True
        except (run.RunFailure, accept_node.AcceptMisuse) as exc:
            accept_error = f"{getattr(exc, 'code', 'accept_misuse')}: {exc}"

    request_row = session.get(FeatureRequest, feature_request_id)
    if request_row is None:
        raise ReviewFlowError(
            f"需求单 {feature_request_id} 不存在 ⇒ 评审读数没有对象",
            code="request_not_found",
            status_code=404,
        )
    project_key = str(request_row.project_key)
    delivery, skipped, delivery_error = _deliver_if_complete(
        session,
        feature_request_id=feature_request_id,
        project_key=project_key,
        bus_root=bus_root,
        entry_name=entry_name,
        now=stamp,
    )
    session.flush()
    row = session.query(TaskCard).filter(
        TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id
    ).one()
    if delivery is not None:
        hint = f"交付包已组装：{delivery.package_path}（{delivery.manifest_count} 条）"
    elif delivery_error:
        hint = f"评审与验收已入库；组包失败：{delivery_error}"
    elif accepted:
        hint = f"python -m acp.realrun --project {project_key} --resume {feature_request_id}"
    else:
        hint = "卡已打回（CHANGES_REQUESTED）；返工后重新提审"
    return ReviewOutcome(
        task_id=task_id,
        feature_request_id=feature_request_id,
        project_key=project_key,
        verdict=str(record.verdict),
        card_status=str(row.status),
        round_no=record.round_no,
        findings=record.findings,
        accepted=accepted,
        acceptance_proxy=proxy,
        accept_error=accept_error,
        delivery=delivery,
        delivery_skipped=skipped,
        delivery_error=delivery_error,
        next_hint=hint,
        verdict_id=record.verdict_id,
    )


def _preview_content(target: Path) -> tuple[str | None, str | None]:
    """读一份产物给面板预览。读不出来的原因落第二格，不把异常带进 HTTP 面。"""
    try:
        if not target.is_file():
            return None, "file_absent"
        if target.stat().st_size > MAX_PREVIEW_BYTES:
            return None, "too_large"
        return target.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return None, "read_failed"


def _archive_root(project_key: str | None, bus_root: Path | None) -> Path | None:
    """存档根的唯一解析位：两个入参缺任何一个都回 `None`（= 这一趟不读盘，只回元数据）。"""
    if project_key is None or bus_root is None:
        return None
    return archive.archive_root(project_key, bus_root=bus_root).resolve()


def artifacts_by_request(
    session, feature_request_id: uuid.UUID, *, project_key: str | None, bus_root: Path | None
) -> dict[str, tuple[PendingArtifact, ...]]:
    """这张需求单下**门禁判为 valid** 的产物，按卡号分组。

    "哪些行算这张卡的产物"这件事只有这一个载体：查 valid、按 `(task_card_id, rel_path)` 排、
    每卡截 `MAX_PREVIEW_FILES` 份、路径越界只回原因不读盘。`GET /tasks`（列表）与
    `GET /tasks/{id}`（详情）与待评审列表都从这里取数 —— 两处各写一份 SELECT 就会在两处各自漂，
    而漂开的那一半表现是"列表里有这张卡、详情里读不到它的产物"。
    """
    rows = (
        session.query(Artifact)
        .filter(
            Artifact.feature_request_id == feature_request_id,
            Artifact.validated_status == VALID,
        )
        .order_by(Artifact.task_card_id, Artifact.rel_path)
        .all()
    )
    root = _archive_root(project_key, bus_root)
    grouped: dict[str, list[PendingArtifact]] = {}
    for row in rows:
        bucket = grouped.setdefault(str(row.task_card_id), [])
        if len(bucket) >= MAX_PREVIEW_FILES:
            continue
        content: str | None = None
        error: str | None = None
        if root is None:
            error = "bus_root_not_configured"
        else:
            try:
                target = (root / str(row.rel_path)).resolve()
                target.relative_to(root)
            except ValueError:
                error = "path_outside_archive"
            else:
                content, error = _preview_content(target)
        bucket.append(
            PendingArtifact(
                task_id=str(row.task_card_id),
                writer_agent_id=str(row.writer_agent_id),
                rel_path=str(row.rel_path),
                format=str(row.format),
                bytes=int(row.bytes),
                lines=int(row.lines),
                content=content,
                content_error=error,
            )
        )
    return {card_id: tuple(items) for card_id, items in grouped.items()}


def artifact_previews(
    session, card: TaskCard, *, project_key: str | None, bus_root: Path | None
) -> tuple[PendingArtifact, ...]:
    """一张卡的已验证产物（元数据 + 源码）。取数规则在 `artifacts_by_request`，这里不重写。"""
    return artifacts_by_request(
        session, card.feature_request_id, project_key=project_key, bus_root=bus_root
    ).get(card.id, ())


def pending_reviews(
    session,
    feature_request_id: uuid.UUID | None = None,
    *,
    bus_root: Path | None = None,
) -> tuple[PendingReview, ...]:
    """待评审/可补开的卡。`RUNNING` 只收那些门禁真的写过 valid 产物行的（旧现场）。

    `bus_root` 给到时附带已验证产物的源码（面板的代码预览）；没给时只回元数据与原因词。
    """
    query = session.query(TaskCard).filter(TaskCard.status.in_((review_node.REVIEWING, RUNNING)))
    if feature_request_id is not None:
        query = query.filter(TaskCard.feature_request_id == feature_request_id)
    cards = query.order_by(TaskCard.feature_request_id, TaskCard.id).all()
    request_ids = {card.feature_request_id for card in cards}
    keys = {
        row.id: str(row.project_key)
        for row in session.query(FeatureRequest).filter(FeatureRequest.id.in_(request_ids)).all()
    } if request_ids else {}
    rows: list[PendingReview] = []
    for card in cards:
        valid = _valid_artifacts(session, card.feature_request_id, card.id)
        if str(card.status) == RUNNING and valid < 1:
            continue
        rows.append(
            PendingReview(
                task_id=card.id,
                feature_request_id=card.feature_request_id,
                title=card.title,
                status=str(card.status),
                reviewer_agent_id=card.reviewer_agent_id,
                acceptor_agent_id=card.acceptor_agent_id,
                acceptance_criteria=tuple(str(item) for item in (card.acceptance_criteria or ())),
                artifact_count=valid,
                requires_open=str(card.status) == RUNNING,
                artifacts=artifact_previews(
                    session,
                    card,
                    project_key=keys.get(card.feature_request_id),
                    bus_root=bus_root,
                ),
            )
        )
    return tuple(rows)
