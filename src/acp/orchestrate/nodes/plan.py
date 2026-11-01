"""T030：`Plan` 节点 —— 需求确认摘要（FR-002 / FR-003｜R-03）。

**这个节点只负责"形状"**：补全什么由注入的补全器决定，本模块钉的是"摘要必须把自行补全项
**显式列出**"这条 FR-002 的判据（`data-model.md` EN-01 的 `summary_digest` 说明列同一句）。
形状判据写在节点里而不是写在调用方，是为了让它只有一个强制点：`api/requests.py` 与将来任何
直接驱动图的入口都走这里，绕过节点的写法会拿不到合法摘要。

**补全器为什么是注入的、且没有默认实现**（这是本轮最重要的一个决定，点名如下）：
FR-002 的主语是"总管"，而总管的实体是适配器（`adapters/<vendor>/transport.py`），
适配器归 **Phase 3（T038 ~ T048）**。本阶段没有任何可用模型通道，于是这里只钉对外形状
（`RequirementCompleter`），把实现留给 Phase 3 —— 与 `plan.md §"两处待外部核验的第三方事实"` 里
"接口先行 + 调用点占位"那条处置同形。
**MUST NOT 在这里放一个"规则版补全器"当默认值**：那会让 `POST /requests` 看起来能跑，
而它给出的"自行补全项"其实是硬编码话术 —— 那是把 FR-002 冒充成已实现（宪法级的假绿）。
缺补全器时的表现是 503 `completer_not_configured`，见 `api/requests.py`。

**摘要里不放 `raw_input`**：EN-01 已有 `raw_input` 列，再往 jsonb 里抄一份就是同一事实两个载体
（原则 I）。摘要只放"补全了什么"与"想问什么"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

SELF_COMPLETED_KEY = "自行补全项"
CLARIFICATION_KEY = "澄清问题"


class IncompleteDigest(ValueError):
    """摘要不满足 FR-002 的形状要求。消息里只带字段名，不带需求原文（防把用户文本回显进日志）。"""


class BlankRequirement(ValueError):
    """`raw_input` 去掉空白后为空 —— FR-001 说"不要求表单"，不等于"可以没有需求"。"""


@runtime_checkable
class RequirementCompleter(Protocol):
    """总管补全能力的对外形状（实现体 = Phase 3 的适配器）。"""

    def complete(self, raw_input: str) -> "CompleterOutcome": ...


@dataclass(frozen=True)
class CompleterOutcome:
    """补全器的返回值：它自己补了什么、以及它还想问什么。"""

    self_completed: Sequence[str] = ()
    clarifications: Sequence[str] = ()


@dataclass(frozen=True)
class PlanOutcome:
    summary_digest: dict[str, list[str]] = field(default_factory=dict)

    @property
    def self_completed(self) -> list[str]:
        return list(self.summary_digest[SELF_COMPLETED_KEY])

    @property
    def clarifications(self) -> list[str]:
        return list(self.summary_digest[CLARIFICATION_KEY])


def validate_summary_digest(digest: object) -> dict[str, list[str]]:
    """FR-002 的形状判据（唯一强制点）。

    要求三件事，缺任一条即 `IncompleteDigest`：
    ① `自行补全项` 这一项**必须在**——"未显式列出"就是不合格，哪怕补全器其实补了东西；
    ② 两项都必须是**字符串列表**（不是把列表塞进一个字符串里，那读不出条目数）；
    ③ 每个条目去掉空白后非空、且不重复（空白条目是凑数，重复条目会让"列了几条"这个读数失真）。
    空列表是**允许**的，含义 = 总管判定这条需求没有需要补全的模糊点；把它当非法会逼调用方
    编造补全项来过关，那是更坏的一种假绿。

    **只校验那两项，其余段原样带过去**（2026-10-28 真跑后改）。原来这里 `return checked`，
    等于把摘要当成"只有这两个键"的白名单：立项会写的「会议」、文档节点写的「PRD / 架构 / 令牌」
    全被丢掉，而这个返回值就是图 state 里 `summary_digest` 的全部内容。后果是**一条根因、四个下游症状**：

    - `llm_split._prd_of()` 永远读不到 PRD ⇒ 用户设计的"PRD 定死后确定性投影"**从未生效**，
      每次都回落模型拆单（实测拆出来的卡标题与投影完全不��）；
    - 拆单提示词里也没有 PRD ⇒ 执行器不知道页面文件该叫 `dashboard.html`、也不知道逐字文案，
      自己写了 `kanban.html`、「确 定」；
    - E2E 检查按 PRD 的页面清单找 `dashboard.html` ⇒ 判"不通过"；
    - 组包要 `index.html` 当入口 ⇒ `entry_absent`，交付包永远组不出来。

    非 FR-002 的段**不做形状校验**（它们的形状由写它们的那一步负责：会议是 `role｜text` 行，
    PRD 是一行 JSON）。这里只保证"是字符串列表"，形状不对就跳过 —— 校验别人的契约是越界，
    而静默丢掉它们是撒谎。
    """
    if not isinstance(digest, dict):
        raise IncompleteDigest(f"摘要必须是对象，实际是 {type(digest).__name__}")
    if SELF_COMPLETED_KEY not in digest:
        raise IncompleteDigest(f"摘要缺少 {SELF_COMPLETED_KEY} 这一项（FR-002 要求显式列出）")

    checked: dict[str, list[str]] = {}
    for key in (SELF_COMPLETED_KEY, CLARIFICATION_KEY):
        items = digest.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise IncompleteDigest(f"{key} 必须是字符串列表")
        stripped = [item.strip() for item in items]
        if any(not item for item in stripped):
            raise IncompleteDigest(f"{key} 里有空白条目")
        if len(set(stripped)) != len(stripped):
            raise IncompleteDigest(f"{key} 里有重复条目")
        checked[key] = stripped
    # 其余段原样带过去（见上面那段"一条根因、四个下游症状"）。只收字符串列表，形状不对就跳过：
    # 这里不是它们的强制点，丢掉才是错。
    for key, value in digest.items():
        if key in checked:
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            checked[key] = list(value)
    return checked


def build_summary_digest(outcome: CompleterOutcome) -> dict[str, list[str]]:
    """把补全器的产出装配成合法摘要；装配与校验同处，产出方没有"绕过校验"的路径。"""
    return validate_summary_digest(
        {
            SELF_COMPLETED_KEY: list(outcome.self_completed),
            CLARIFICATION_KEY: list(outcome.clarifications),
        }
    )


def plan_requirement(raw_input: str, *, completer: RequirementCompleter) -> PlanOutcome:
    """`Plan` 节点本体：原文 → 摘要。不写库、不建卡（拆单是 T032）。"""
    if not raw_input or not raw_input.strip():
        raise BlankRequirement("需求原文为空")
    return PlanOutcome(summary_digest=build_summary_digest(completer.complete(raw_input)))


def is_clarification_waived(digest: dict[str, list[str]], answers: Sequence[str]) -> bool:
    """FR-003 的落点：`clarification_waived` 的口径（EN-01 的说明列"使用者未回答澄清仍继续 = true"）。

    三种情形分别是：
    - 总管提出了澄清问题、使用者一条没答 ⇒ **True**（这就是"未回答仍推进"）；
    - 总管没提问题 ⇒ **False**（没东西可答，不是"放弃了回答"）；
    - 答了至少一条 ⇒ **False**（正常路径）。
    把"没提问题"也记成 True 会让这个字段失去区分力 —— 它就该只表达"被放弃的澄清"这一件事。
    """
    asked = digest.get(CLARIFICATION_KEY) or []
    return bool(asked) and not any(answer.strip() for answer in answers)
