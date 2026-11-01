"""审查身份与运行模式的唯一载体（Phase 17 P3）。

为什么单独一个模块：`deepseek-reviewer` 这个名字同时被三处读 —— 适配器（谁在发请求）、
选人池（manual 模式不许它进来）与执行选人（它永远不进执行候选）。三处各写一份字面量，
改名时必然漏一处，而漏掉的那一处不会报错：要么人工模式里多出一个审查候选，要么模型身份
被选成执行者。模式词同理（`serve` 读环境、`assign_guards` 过滤、`ReviewRunner` 装配）。

本模块**不 import 任何东西**：适配器与编排层都能安全地引用它，不会拉出一圈循环依赖。
"""

from __future__ import annotations

#: 审查席的模型身份。注册表、适配器、ReviewRunner 与选人池都从这一格取。
DEEPSEEK_REVIEWER = "deepseek-reviewer"

#: 只服务审查、MUST NOT 进入执行选人的身份。今天是单元素集合；将来接第二家审查模型时在这里加。
REVIEW_ONLY_AGENT_IDS = frozenset({DEEPSEEK_REVIEWER})

#: 门禁代签的身份名（`auto` 模式）。**它不是一支 agent**：不进注册表、不进选人池、不发任何请求，
#: 只作为 `review_verdict.actor_agent_id` 与留痕的主语出现 —— 审计时要能一眼分清
#: "模型审过"（`deepseek-reviewer`）、"人签的"（`owner-1`）与"门禁自己放行的"（这一个）。
GATE_REVIEWER = "gate-reviewer"

#: `ACP_REVIEW_MODE` 的取值域。默认 `manual` 保住 Phase 16 的逐字行为。
REVIEW_MODE_ENV = "ACP_REVIEW_MODE"
REVIEW_MODE_MANUAL = "manual"
REVIEW_MODE_DEEPSEEK = "deepseek"
#: `auto` = **门禁代签**（2026-10-28）：产物过门禁即视为通过，卡不进人工评审、也不调模型。
#: 与 `deepseek` 的差别只有一条，但那条很关键：`deepseek` 是"独立审查员给 PASS，再按风险档决定谁签"，
#: `auto` 是"没有人审，门禁判产物达标就过"。所以 `auto` 下 `auto_accept_low_risk` 那个开关**不参与判定**
#: （它管的是"机器能不能替人签一份被审过的结论"，而这里根本没有那份结论）。
#: 它存在的原因是现场口径："每张卡都要点一次，一单六张卡就是六次点击" —— 那是把 owner 变成盖章机器。
REVIEW_MODE_AUTO = "auto"
REVIEW_MODES = (REVIEW_MODE_MANUAL, REVIEW_MODE_DEEPSEEK, REVIEW_MODE_AUTO)


class ReviewModeInvalid(ValueError):
    """`ACP_REVIEW_MODE` 不在封闭集合里。启动期拒，不降级成 manual。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        super().__init__(f"不认识的 {REVIEW_MODE_ENV}={raw!r} ⇒ 只接受 {list(REVIEW_MODES)}（不降级）")


def resolve_review_mode(raw: str) -> str:
    normalized = str(raw).strip().lower()
    if normalized not in REVIEW_MODES:
        raise ReviewModeInvalid(raw)
    return normalized
