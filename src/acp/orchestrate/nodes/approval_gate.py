"""T048 / T049 / T051 / T054：`ApprovalGate` 节点 —— 图里唯一的那一个等待点（FR-038 ~ FR-041｜R-26）。

**卡点 = `interrupt(payload)`**（宪法 V：MUST NOT 改成轮询或重启恢复，D-9 / `plan.md` 设计项 3）。
payload 就是 FR-039 的五要素，由 `approval/service.py` 组装与校验 —— 本节点不自己拼那份 dict，
否则"缺项 MUST NOT 呈交"就有了第二条绕过路径（原则 I）。

四件事，按执行顺序：

1. **建单（或合并）**：主键由 `service.ticket_id_for()` 推导，因为**这个节点会被完整重跑一遍**。
   实测读数（`evidence/phase-04.md §T048`）：`interrupt` 之后的节点在恢复时从头再执行，
   上游节点不重跑。若主键是 `uuid4()`，重放就会插出第二张待决单，而 EN-09 那条部分唯一索引
   只认 `state='PENDING'` —— 人已批过的那张不掉进查询里，第二张就建得出来。
2. **卡进入待批态**：`DRAFT → PENDING_APPROVAL`（`data-model.md §2` 的那条边，"拆单"这一步的执行者）。
   这一步在建单之后、挂起之前：看板要能看见"有一张单在等人"，而不是等 checkpoint 里的一份过程态。
3. **挂起 / 免确认分流**：只有"这一次刚被规则表批掉"的单才跳过 `interrupt`。
   人在带外批过的单（重放时 `state != PENDING`）**照样走 `interrupt`** —— 恢复值必须被消费掉，
   否则 LangGraph 的 resume 值会悬在那儿，而这里的判定顺序也才和第一次执行对得上。
4. **结论回写状态机**（T051）：批准 → `READY`；驳回 → `REJECTED`；需补充信息 → 不改待决态、
   原 payload 一字不改地再问一次（FR-040 三）。

**为什么节点自己不做 CAS**：`approval/repository.py` 是 EN-09 状态的唯一写者（FR-040 的或签在那儿）。
节点只核对"我收到的恢复值在库里确实有对应决定"，对不上就 `GateMisuse` 报错 ——
那是这条链上最要紧的一道反向判据：**有人 resume 却没批过单，图不许往前走**。
`contracts/approval-api.md §4` 的"恢复动作"写的就是这条链。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.registry.risk_engine import RequestFacts
from acp.reporting.cards import is_system_card

#: MVP-1 在图里挂的是 A 类（拆单确认）。D 类的挂点归 Phase 8（同一个出口，`tasks.md` 的依赖表）。
KIND = KIND_BY_LETTER["A"]

#: 节点名。**图与 API 共用这一个常量**：`contracts/approval-api.md §3` 的 `resumed_from` 字段
#: 写的就是"从哪个节点续跑"，两处各写一份字符串就会漂成"响应里说恢复了而图上没有那个节点名"。
GATE = "approval_gate"

APPROVE = "approve"
REJECT = "reject"
NEED_INFO = "need_info"

#: 结论 → 卡的目标态（`data-model.md §2` 的三条出边：批准 → READY，驳回 → REJECTED，
#: 追问不改待决态所以留在 PENDING_APPROVAL）。
CARD_STATUS: dict[str, str] = {APPROVE: "READY", REJECT: "REJECTED", NEED_INFO: "PENDING_APPROVAL"}

#: §2 的**合法入边**：这个卡点只许动"刚拆完的卡"（DRAFT）与"已经挂在本卡点上的卡"（PENDING_APPROVAL，
#: 重放时会再写一次同一个值，所以它必须在允许集里 —— 少了这一条，恢复路径就会撞自己的守卫）。
#: 别的态（`RUNNING` / `REVIEWING` / `DELIVERED` …）落到这里都说明有人在派发之后又回头过这个卡点，
#: 那是 FR-041"不得重跑整条链路"的反面 ⇒ 报错，不静默改写。
ALLOWED_FROM: dict[str, frozenset[str]] = {
    "PENDING_APPROVAL": frozenset({"DRAFT", "PENDING_APPROVAL"}),
    "READY": frozenset({"PENDING_APPROVAL", "READY"}),
    "REJECTED": frozenset({"PENDING_APPROVAL", "REJECTED"}),
}


class GateMisuse(ValueError):
    """恢复值与库里的决定对不上、或图缺了跑卡点要的东西。失败关闭，不放行也不静默重问。"""


class CheckpointerNotConfigured(GateMisuse):
    """`interrupt` 必须有 checkpointer（D-9）。缺它不许把图编译成"看起来能跑但没有挂起点"的形状。"""


@dataclass(frozen=True)
class GateResult:
    """节点的一次执行结果（只给 state 用，不是第二份事实 —— 权威始终在 EN-02 / EN-09）。"""

    ticket_id: uuid.UUID
    decision: str
    card_status: str
    asked_again: bool


def cards_of(session: Session, feature_request_id: uuid.UUID) -> list[TaskCard]:
    return list(
        session.scalars(
            select(TaskCard)
            .where(TaskCard.feature_request_id == feature_request_id)
            .order_by(TaskCard.id)
        )
    )


def build_request_payload(session: Session, request: FeatureRequest, cards: list[TaskCard]) -> dict:
    """五要素的前四项从**已入库的事实**拼出来（第五项 `timeout_policy` 由 `params-ref §1` 算）。

    `suggested_verdict` 是**规则**而不是模型输出：走到这一步意味着 `normalize` 没抛
    （FR-005 的结构校验已过），所以建议批准。把它做成"让总管模型再说一遍"就是 FR-047
    禁的那类自裁入口，虽然那条原文管的是低风险判定 —— 同一条理由搬到这里，因为形状一样。

    `impact.estimated_extra_usage` 在 MVP-1 是**空映射**：它的数值载体是 EN-13 的账本，
    而写者 `cost/ledger.py` 归 T114（Phase 10）。空映射在这里读作"无口径可报"，
    MUST NOT 被读成"零用量" —— 这条缺口登记在 `evidence/phase-04.md §T049` 的未闭段。
    """
    digest = dict(request.summary_digest or {})
    completed = "; ".join(digest.get("自行补全项", []) or [])
    questions = "; ".join(digest.get("澄清问题", []) or [])
    return service.build_payload(
        kind=KIND,
        pending_item=f"确认这批任务清单：{', '.join(card.id for card in cards)}",
        context_digest=(
            f"需求单 {request.id}；原始输入 {request.raw_input[:80]}"
            f"；自行补全 {completed or '（无）'}；澄清问题 {questions or '（无）'}"
        ),
        suggested_verdict=APPROVE,
        impact={
            "downstream_task_ids": [card.id for card in cards],
            "estimated_extra_usage": {},
        },
    )


def _set_card_status(session: Session, cards: list[TaskCard], status: str) -> None:
    """按 §2 的边写卡的态；**入态不在允许集里就整批不写**（FR-041 的反面形态在这里拦）。

    检查放在写之前、且先全批核一遍再落笔，理由是半途而废的状态迁移比拒绝更坏：
    三张卡改了两张、第三张撞守卫，那两张就成了"挂在卡点上却没有单据对应决定"的孤儿。
    """
    allowed = ALLOWED_FROM.get(status)
    if allowed is None:
        raise GateMisuse(f"卡点不知道该把卡写到 {status!r}：这个目标态不在 §2 的三条出边里")
    illegal = [(card.id, str(card.status)) for card in cards if str(card.status) not in allowed]
    if illegal:
        raise GateMisuse(
            f"这批卡里有 {len(illegal)} 张不在合法入态上（样例 {illegal[:3]}）⇒ "
            "A 类卡点只允许动刚拆完的卡或本卡点上待批的卡"
        )
    for card in cards:
        card.status = status
    session.flush()


def run_gate(
    session: Session,
    *,
    request: FeatureRequest,
    cards: list[TaskCard],
    facts: RequestFacts | None = None,
    now: dt.datetime | None = None,
) -> GateResult:
    """卡点节点的全部判定，抽成函数是为了让用例能在**没有图**的情况下驱动它（T063 的负例走这里）。

    **审批对象集 = 本单的非系统卡**（`is_system_card` 判）。为什么必须在唯一判定点这里排除：

    文档 / 检查 / 验收 / 打包四类卡是"系统节点已经发生的事实"落成的行，建出来就是 `DELIVERED`，
    它们没有经过"派单→执行→评审"，也不该有人来批。而 `_set_card_status` 的入态守卫要求
    整批都在 `{DRAFT, PENDING_APPROVAL}` 里 —— 不排除它们，这批卡里只要有哪怕一张系统卡，
    整批就被判"不在合法入态上"。2026-10-28 真跑实测正是这样：T90–T92 把整个 A 类卡点顶翻，
    `GateMisuse` 上抛 ⇒ 拆单事务整体回滚 ⇒ 卡一张不剩、需求单永久停在 `INTAKE`。

    排除放在**过滤**而不是放宽守卫：守卫要挡的是"派发之后又回头过卡点"（FR-041 的反面），
    那条判断对执行卡仍然成立；系统卡压根不在它的适用范围内，放宽等于连执行卡一起放过去。
    """
    cards = [card for card in cards if not is_system_card(card)]
    if not cards:
        raise GateMisuse(
            f"需求单 {request.id} 名下没有任何卡 ⇒ A 类卡点没有可确认的对象，"
            "空清单呈交上去只会让审批者批一个看不见的东西"
        )
    ticket_id = service.ticket_id_for(kind=KIND, feature_request_id=request.id)
    payload = build_request_payload(session, request, cards)
    ticket = service.create_approval(
        session,
        kind=KIND,
        payload=payload,
        feature_request_id=request.id,
        ticket_id=ticket_id,
        facts=facts,
        now=now,
    )
    if ticket.kind_conflict:
        # 同任务已挂着**别的一类**卡点：FR-045 后半句禁第二行，所以这里不建、也不假装自己的卡点生效了。
        raise GateMisuse(
            f"需求单 {request.id} 已有一张 {ticket.kind} 类未决单 ⇒ A 类卡点挂不上（FR-045 只允许一张）"
        )
    _set_card_status(session, cards, "PENDING_APPROVAL")
    if ticket.auto_passed:
        _set_card_status(session, cards, CARD_STATUS[APPROVE])
        return GateResult(ticket_id=ticket.ticket_id, decision=APPROVE, card_status=CARD_STATUS[APPROVE], asked_again=False)

    asked_again = False
    while True:
        conclusion = interrupt(payload)
        conclusion = str(conclusion)
        if conclusion not in CARD_STATUS:
            raise GateMisuse(f"结论 {conclusion!r} 不在 resolution_verdict 域里：{sorted(CARD_STATUS)}")
        row = session.get(repo.ApprovalTicket, ticket.ticket_id)
        if row is None:
            # 建单与恢复之间那行被人删了（运维脚本 / 手动清库）。这时没有任何"库里批的是什么"
            # 可核对 ⇒ 拒绝恢复，而不是拿一个默认结论往前走。
            raise GateMisuse(
                f"单据 {ticket.ticket_id} 不在 EN-09 里 ⇒ 恢复值 {conclusion!r} 无处核对，"
                "拒绝用猜的结论推进这张卡"
            )
        stored = str(row.state)
        if conclusion == NEED_INFO:
            # 追问不结束待决（FR-040 三）：库里仍是 PENDING、payload 一字不改，再问一次。
            if stored != repo.PENDING:
                raise GateMisuse(f"收到追问但单据已是 {stored} ⇒ 追问不能落在一批过的单上")
            asked_again = True
            continue
        decisive_state = repo.APPROVED if conclusion == APPROVE else repo.REJECTED
        if stored != decisive_state:
            raise GateMisuse(
                f"恢复值是 {conclusion}，而 EN-09 那行是 {stored}（版本 {row.version}）⇒ "
                "有人 resume 却没有人批过单，或库里批的是另一个结论"
            )
        _set_card_status(session, cards, CARD_STATUS[conclusion])
        return GateResult(
            ticket_id=ticket.ticket_id, decision=conclusion, card_status=CARD_STATUS[conclusion],
            asked_again=asked_again,
        )


def approval_gate(session: Session) -> Callable[[dict], dict]:
    """图节点适配器：从 state 取需求单号，其余交给 `run_gate`。

    **这里恒不传 `facts`** ⇒ A 类卡点在 MVP-1 永远走人工确认，不是漏接：
    FR-046 的匹配键是"工具 + 岗位"，而 A 类单挂在**指派之前**（EN-02 那四列此时还是 NULL，
    `graph.py` 的建行注释与 T-14 ① 同一条事实），三段键里"工具/岗位"两段根本还不存在。
    拿需求单级规则去凑一个 agent_id 就是替 owner 决定免确认的粒度 ⇒ 已登记为待裁
    （`evidence/phase-04.md §T049` 未闭段），本轮保守取"必问"。
    """

    def node(state: dict) -> dict:
        request = session.get(FeatureRequest, state["feature_request_id"])
        if request is None:
            raise GateMisuse(f"EN-01 里没有需求单 {state['feature_request_id']} ⇒ 先走 POST /requests")
        result = run_gate(session, request=request, cards=cards_of(session, request.id))
        return {
            "approval_ticket_id": result.ticket_id,
            "gate_decision": result.decision,
            "card_status_after": result.card_status,
            "gate_asked_again": result.asked_again,
        }

    return node
