"""T065 ~ T072：`SelectWorker` 的确定性匹配与四要素全序（FR-022 ~ FR-025 / FR-062；plan 设计项 8、D-13）。

**三条禁令撑起整条需求的可证伪性**（FR-022）：`rank_candidates` 不含模型调用、不含时间相关项、
不含随机种子。少任何一条，"按顺序选择"就退化成"每次都可能不一样的一次结果"，而判据①
（同一输入连跑三次同一个 `agent_id`）只会在**它恰好没漂**的那天绿。

**排序键②的口径是一条决定，不是引用**（owner 2026-10-14 本轮择一）：`difficulty` 在全仓只有类型、
没有域（`data-model.md:32` 只写 `int`，`split.py:134` 自陈"只查类型不查区间 —— 文档与库里都没有给它
上界下界"），文档从未给 `difficulty ↔ tier` 的对应表。所以本轮取**同档即匹配**：比较值 =
`abs(tier - difficulty)` 升序。它不引入任何新数字、不发明阈值；将来 owner 若在 `params-ref §9`
补一张带宽表，**只替换 `_tier_deviation()` 一个纯函数**，函数签名与 `selection_basis.keys` 都不动。

**FR-062 不另开比较位**（owner 同轮裁"靠键②隐含"）：`cost_band` 只作为**取值**进 `selection_basis`
（可审计、可展示），MUST NOT 进比较 —— 依据是 `params-ref §9` 正文那句"两轴各管一件事"：
`tier` 是能力档位，`cost_band` 是记账侧标签，把后者塞成全序的第 6 位等于改契约而不只是改实现。

**键③的"跳过本位"是整批跳过，不是逐候选跳过**（`data-model.md:60` 那句话的主语是"选人"）：
候选里只要有一支 `success_rate_sample = 0`，本位在**这一批比较**里整体不参与 —— `keys` 序列里
不出现 `historical_success_rate`，`skipped` 里逐条写明是谁无战绩。两种看着更"精细"的写法都不成立：
- "无战绩方与有战绩方在这一位算平手"得到的关系**不可传递**（A=.9、B=无、C=.5 时
  A>B、B>C 而 A>C），而字典序要求每一位都是全序 ⇒ Python 的 `sorted` 会给出**依赖输入顺序**的结果，
  直接毁掉判据①。
- 把无战绩折成 `0%` 是文档明令禁止的造假（"「无战绩」与「总是失败」必须可区分"）。
T074④ 要求被选中的无战绩候选在 `selection_basis` 里显式带"无历史战绩"字样 —— 那是 `skipped` 那一段
的文本，不是给某一支候选打的标签，两者是同一条事实的两个读法，载体仍只有 `skipped` 这一处。

**并发上限有两轴，本模块只碰后一根**：`params-ref §3` 的默认 3 是"平台同时最多跑几张卡"（运行时槽位，
归 Phase 6 的 T079/T080）；`current_load ≥ max_parallel` 是"这一支工具自己接满了"（FR-025），
在本模块里作为**出局**而不是作为一个排序值。`current_load` 的权威口径在瞬态层
（`runtime/slot_manager.py::usage()`，Phase 6 的 T079），本模块收的是**入参形状** ——
调用方从哪一列/哪次读取拿到它不归这里，改源不改签名。

这里 MUST NOT 做的三件事：
- **不写三岗互斥断言**：那是 `guard/triple.py`（T085，Phase 7）的唯一载体；这里判的是"谁来做"，
  不是"谁能审"（`assign_guards.py:5` 已把这条边界写过一次，两边各守各的）。
- **不写 `reviewer_agent_id` / `acceptor_agent_id`**：那两个把关岗归 `AssignGuards`（T086，Phase 7）。
  反过来，写 `executor_agent_id` 是**有文档依据的** —— `data-model.md:34` 原话是"可空至 `SelectWorker`
  填充"。所以这一列的写者实现只有 `apply_selection()` 一个函数，自动选人（T073）与人工覆盖（T071）
  都走它 ⇒ 两条路径不可能各写一半；而"为什么是他"这个事实的载体仍只有 EN-14 那一行。
- **候选为空时不报错**：FR-025 要的是"保持 `READY` + 进等待表 / 明确提示，MUST NOT 静默丢弃" ⇒
  这里返回 `selected=None`、`executor_agent_id` 不动，但**照样落一行 `SELECTION`**
  （"没人可选"也是一次要能查的决策），调用方把 `waiting_notice` 呈给使用者。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.persistence import events
from acp.persistence.models_core import AgentUnit, CapabilityManifest, TaskCard
from acp.review.policy import REVIEW_ONLY_AGENT_IDS

#: 五个比较位的**规范名**。这些字符串会原样进 `selection_basis.keys`，所以它们是可审计面的一部分：
#: 改了名字就等于改了历史留痕的读法（判据 `test_the_key_names_are_the_documented_five_in_order` 钉这一条）。
KEY_SKILL_FIT = "skill_fit"
KEY_TIER_MATCH = "tier_match"
KEY_SUCCESS_RATE = "historical_success_rate"
KEY_CURRENT_LOAD = "current_load"
KEY_AGENT_ID = "agent_id"

#: plan.md 设计项 8 写定的全序位序（① → ⑤）。本模块按它逐位比较，前一位分不出才看下一位。
DOCUMENTED_KEYS: tuple[str, ...] = (
    KEY_SKILL_FIT,
    KEY_TIER_MATCH,
    KEY_SUCCESS_RATE,
    KEY_CURRENT_LOAD,
    KEY_AGENT_ID,
)

#: 无战绩的披露用语。T074④ 断的就是这个字面串 —— 它是一条**契约级**措辞（使用者看得懂的那句），
#: 不是实现细节，所以集中成常量而不是散在拼接里。
NO_HISTORY_NOTICE = "无历史战绩"

#: 自动选人的留痕主体（owner 2026-10-14 裁决 6 要求成常量）。
#: **这是一条临时约定，不是一个已成立的身份**：EN-03 里没有"系统自己"这支工具，
#: 而 `event_log.actor_agent_id` 是 NOT NULL ⇒ 自动路径必须有个主语。是否在 Phase 7 引入
#: "系统 actor"概念、或把它归进 `role_spec` 的一种特殊角色，已登记在 `tasks.md` 的 Phase 5 申报段；
#: 定下来之前，MUST NOT 拿任何注册工具的 `agent_id` 冒充（那会让审计读成"某支工具做了平台级决定"）。
AUTOMATED_ACTOR = "orchestrator"


class SelectionMisuse(ValueError):
    """调用方交来的形状不对（重复候选、`max_parallel <= 0`、有比率却没样本）。构造期就拒。"""


class SkillTagsEmpty(ValueError):
    """任务没声明所需技能 ⇒ 键①的分母为 0。

    文档给这条的形态是"**断言失败**，落 `GUARD_VIOLATION` + 卡点 B"（plan 设计项 8、D-13），
    而卡点 B 在 MVP-1 是结构预留（`approval/service.py` 的 `ACTIVE_KINDS` 只有 A / D）⇒ 本轮同样是
    **响亮失败而不是一张 B 类单**，与 `assign_guards.py` 那条"矩阵不闭合就抛错"同一口径。
    MUST NOT 用相似度或模型打分补这一档。
    """

    code = "guard_violation"

    def __init__(self, card_id: str) -> None:
        self.card_id = card_id
        super().__init__(
            f"skill_tags 为空 ⇒ skill_fit 分母为 0（卡 {card_id}）；"
            "该项本该在 Normalize 的 FR-004 四项必填就被拦住，不许在选人时用相似度兜底"
        )


@dataclass(frozen=True)
class TaskNeed:
    """一张卡在选人这一刻需要的三个值。

    刻意**不**带 `session`、不带卡的全列：纯函数的输入只到"比较要用什么"为止，
    多带一个字段就会有人顺手在这里读库，那时"不含时间相关项"这条禁令就守不住了。
    """

    card_id: str
    skill_tags: tuple[str, ...]
    difficulty: int

    def __post_init__(self) -> None:
        if not self.skill_tags:
            raise SkillTagsEmpty(self.card_id)


@dataclass(frozen=True)
class WorkerCandidate:
    """一支候选工具的比较取值。

    `capabilities` 是 **EN-05 生效口径**（技能契合只有这一处来源，EN-03 注脚已声明不存该字段）；
    `tier / cost_band / current_load / max_parallel / historical_success_rate / success_rate_sample`
    全取自 EN-03。比率列可空是**有意的**：库里那条 CHECK 就是"无样本必须无比率"，
    本类在同一条口径上构造期就拒（把"默认值等于缺陷值"挡在进入比较之前，而不是等 PG 报错）。
    """

    agent_id: str
    capabilities: frozenset[str]
    tier: int
    cost_band: str
    current_load: int
    max_parallel: int
    historical_success_rate: Decimal | None
    success_rate_sample: int

    def __post_init__(self) -> None:
        if self.max_parallel <= 0:
            raise SelectionMisuse(f"{self.agent_id} 的 max_parallel={self.max_parallel} 必须 > 0（FR-011 逐条必填）")
        if self.success_rate_sample <= 0 and self.historical_success_rate is not None:
            raise SelectionMisuse(
                f"{self.agent_id} 的 success_rate_sample={self.success_rate_sample} 却带了比率 "
                f"{self.historical_success_rate} ⇒ 与库里 no_rate_without_sample 那条 CHECK 同口径拒绝"
            )

    @property
    def has_history(self) -> bool:
        return self.success_rate_sample > 0


@dataclass(frozen=True)
class RankedWorker:
    """一次比较里某支候选的**全部可见取值**（FR-023 展示面的原料，M1 只落留痕不建展示面）。"""

    agent_id: str
    skill_matches: int
    skill_fit: Decimal
    tier: int
    tier_deviation: int
    cost_band: str
    historical_success_rate: Decimal | None
    success_rate_sample: int
    current_load: int
    max_parallel: int

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "skill_matches": self.skill_matches,
            "skill_fit": f"{self.skill_fit}",
            "tier": self.tier,
            "tier_deviation": self.tier_deviation,
            "cost_band": self.cost_band,
            "historical_success_rate": (
                None if self.historical_success_rate is None else f"{self.historical_success_rate}"
            ),
            "success_rate_sample": self.success_rate_sample,
            "current_load": self.current_load,
            "max_parallel": self.max_parallel,
        }


@dataclass(frozen=True)
class Ranking:
    """`rank_candidates` 的输出：全序表 + 这条顺序是**怎么来的**（键序列 / 跳过位 / 出局者）。

    `keys` 与 `skipped` 是分开的两件事：前者说"这一批比了哪几位、按什么序"，后者说"哪一位没参与、
    为什么"。合成一个列表就会让读的人分不清"跳过了"与"忘了比"。
    """

    card_id: str
    difficulty: int
    skill_tags: tuple[str, ...]
    ordered: tuple[RankedWorker, ...]
    keys: tuple[str, ...]
    skipped: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]
    #: 自动算出来的首位。**只有走过 `with_override()` 才会有值**，它是"覆盖了什么"的唯一载体
    #: （FR-024 要可查的是"人工推翻了哪一个"，只留最终执行人就把这条信息永久丢掉了）。
    auto_selected: str | None = None

    def with_override(self, agent_id: str) -> Ranking:
        """把一支**已获资格**的候选摆到首位（FR-024 的"以人工指定为准"）。

        只重排 `ordered` 与首位，`keys` / `skipped` / `excluded` 一律不动 —— 依据仍然是自动算出来的
        那一条序；覆盖是在它之上做的一次决定，不是另起一套比较。资格判据由调用方先把关
        （T071 的三条 MUST 之一："覆盖的是排序结果的选择权，不是资格判据"）。
        """
        chosen = [ranked for ranked in self.ordered if ranked.agent_id == agent_id]
        if not chosen:
            raise SelectionMisuse(
                f"覆盖目标 {agent_id!r} 不在可参选集合里 ⇒ 不许把它摆到首位；"
                "本函数只重排已获资格的候选，资格判据在调用方"
            )
        rest = [ranked for ranked in self.ordered if ranked.agent_id != agent_id]
        return replace(
            self,
            ordered=tuple(chosen + rest),
            auto_selected=self.ordered[0].agent_id if self.ordered else None,
        )

    @property
    def selected(self) -> RankedWorker | None:
        return self.ordered[0] if self.ordered else None

    @property
    def waiting_notice(self) -> str:
        """候选为空时给使用者的那句话（FR-025 的"明确提示"，不是异常）。"""
        if self.ordered:
            return ""
        if not self.excluded:
            return f"卡 {self.card_id}：没有可参选的协作单元（候选集为空）⇒ 任务保持 READY，不静默丢弃"
        reasons = "；".join(f"{agent_id}：{reason}" for agent_id, reason in self.excluded)
        return f"卡 {self.card_id}：全部候选本轮不可选 ⇒ 任务保持 READY，不静默丢弃（{reasons}）"

    def selection_basis(self, *, overridden: bool = False, by: str | None = None) -> dict[str, object]:
        """FR-023 的可核对形态：**键序列**而不是叙述，配各候选的四要素取值。

        `overridden` 属 FR-024，写入点仍只有 T070 这一处（覆盖端点不另开第二条写 `SELECTION` 的路径）。
        `by` 是留痕主体（自动选人时为编排引擎自身的 agent_id，覆盖时为使用者侧标识）。
        """
        return {
            "card_id": self.card_id,
            "difficulty": self.difficulty,
            "skill_tags": sorted(self.skill_tags),
            "keys": list(self.keys),
            "skipped": list(self.skipped),
            "candidates": [ranked.to_dict() for ranked in self.ordered],
            "excluded": [{"agent_id": agent_id, "reason": reason} for agent_id, reason in self.excluded],
            "selected": None if self.selected is None else self.selected.agent_id,
            "auto_selected": self.auto_selected,
            "overridden": overridden,
            "decided_by": by,
        }


def _tier_deviation(candidate: WorkerCandidate, difficulty: int) -> int:
    """键②的比较值：**同档即匹配**，偏离量越小越靠前（口径见模块头，换带宽表只改这里）。"""
    return abs(candidate.tier - difficulty)


def rank_candidates(
    task: TaskNeed,
    candidates: Sequence[WorkerCandidate],
    extra_excluded: Sequence[tuple[str, str]] = (),
) -> Ranking:
    """纯函数：算出全序候选表并解释这个顺序（FR-022 / FR-023，D-13）。

    **键③"无战绩 ⇒ 跳过本位"是整批跳过**（owner 2026-10-14 裁决 1 认可）：
    逐候选算平手得到的关系**不可传递**（A=.9 / B=无 / C=.5 时 A>B、B>C 而 A>C），
    `sorted` 的结果就会依赖输入顺序 ⇒ 毁掉判据①要的确定性；而按 0% 处理是文档明令禁止的造假
    （"无战绩"与"总是失败"必须可区分，`data-model.md:60`）。

    `extra_excluded` 是给**调用方**的口子：候选集构造阶段就被拿掉的工具（例如已启用但没有能力申报）
    不归本函数判，但必须跟着进 `selection_basis.excluded` —— 否则"没人可选"这件事在留痕里
    只剩下一个总数，读不出是被谁挡的（FR-025 的"MUST NOT 静默丢弃"同样适用于候选集本身）。
    """
    tags = frozenset(task.skill_tags)
    ids = [candidate.agent_id for candidate in candidates]
    duplicated = sorted({agent_id for agent_id in ids if ids.count(agent_id) > 1})
    if duplicated:
        raise SelectionMisuse(f"候选集里 {duplicated} 重复 ⇒ 全序会出现两个同名单元，比较结果不可解释")

    active: list[WorkerCandidate] = []
    excluded: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate.current_load >= candidate.max_parallel:
            excluded.append(
                (candidate.agent_id, f"current_load={candidate.current_load} ≥ max_parallel={candidate.max_parallel}")
            )
            continue
        active.append(candidate)

    # 键③整批跳过与否，取决于"这一批里有没有任一支无战绩"（见模块头那条不可传递的论证）。
    rate_active = all(candidate.has_history for candidate in active)
    skipped: list[str] = []
    if not rate_active:
        no_history = sorted(candidate.agent_id for candidate in active if not candidate.has_history)
        skipped.append(
            f"{KEY_SUCCESS_RATE} 本位跳过：{NO_HISTORY_NOTICE} —— {no_history} 的 success_rate_sample = 0"
            "（不按 0% 处理，也不对有战绩的候选计分）"
        )

    def comparison(candidate: WorkerCandidate) -> tuple[int, int, Decimal | int, int, str]:
        matches = len(tags & candidate.capabilities)
        rate: Decimal | int = 0
        if rate_active:
            # 走到这里 rate_active 为真 ⇒ 每支候选的比率都非空，Decimal 之间可直接比较。
            rate = -Decimal(f"{candidate.historical_success_rate}")
        return (
            -matches,
            _tier_deviation(candidate, task.difficulty),
            rate,
            candidate.current_load,
            candidate.agent_id,
        )

    ordered = tuple(
        RankedWorker(
            agent_id=candidate.agent_id,
            skill_matches=len(tags & candidate.capabilities),
            skill_fit=Decimal(len(tags & candidate.capabilities)) / Decimal(len(tags)),
            tier=candidate.tier,
            tier_deviation=_tier_deviation(candidate, task.difficulty),
            cost_band=candidate.cost_band,
            historical_success_rate=candidate.historical_success_rate,
            success_rate_sample=candidate.success_rate_sample,
            current_load=candidate.current_load,
            max_parallel=candidate.max_parallel,
        )
        for candidate in sorted(active, key=comparison)
    )
    keys = tuple(
        key
        for key in DOCUMENTED_KEYS
        if not (key == KEY_SUCCESS_RATE and not rate_active)
    )
    return Ranking(
        card_id=task.card_id,
        difficulty=task.difficulty,
        skill_tags=task.skill_tags,
        ordered=ordered,
        keys=keys,
        skipped=tuple(skipped),
        excluded=tuple(excluded) + tuple(extra_excluded),
    )


def apply_selection(
    session: Session,
    ranking: Ranking,
    *,
    feature_request_id: uuid.UUID,
    actor_agent_id: str,
    created_at: datetime,
    overridden: bool = False,
    by: str | None = None,
    forced: str | None = None,
    routing_ignored: str | None = None,
) -> TaskCard:
    """T070 的唯一输出副作用 + `executor_agent_id` 的唯一写者实现（自动与覆盖两条路径都走这里）。

    做两件事、不 commit、不刷新卡状态：
    1. `executor_agent_id`：`forced` 给了就写它（请求级路由点名的执行者，2026-10-31 路线 B），
       否则写首位候选（候选为空就**不动列**）；
    2. 落一行 EN-14 `kind=SELECTION`，payload 是 `selection_basis()`（`overridden` 走同一个入口，
       所以"人工覆盖"永远不会出现第二条写 `SELECTION` 的路径 —— T071 的三条 MUST 里那条"留痕"就这么守）。

    `forced` 的留痕口径：`overridden=True` + `decided_by="model_routing"`，并在 payload 里多一格
    `routed_executor`。**不改 `selected` 那一格** —— 它说的是"排序第一是谁"，路由覆盖不改变排序事实。

    卡的作用域是 `(feature_request_id, id)`：`pending_for()` 在 Phase 4 栽过同一件事
    （只按卡号查会捞进别的需求单的同名卡），这里从一开始就两列一起比。
    """
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == ranking.card_id)
        .one_or_none()
    )
    if card is None:
        raise SelectionMisuse(
            f"需求单 {feature_request_id} 下没有卡 {ranking.card_id} ⇒ 选人结果无处可落，"
            "不许把留痕写在一张不存在的卡上"
        )
    if forced:
        card.executor_agent_id = forced
    elif ranking.selected is not None:
        card.executor_agent_id = ranking.selected.agent_id
    payload = ranking.selection_basis(overridden=overridden or bool(forced), by=by or ("model_routing" if forced else None))
    if forced:
        payload["routed_executor"] = forced
    if routing_ignored:
        # 路由点名了一个不参选的执行者（停用/不在册）⇒ 不强制，但**不静默**：留痕里写明点了谁。
        payload["routing_ignored"] = routing_ignored
    events.record(
        session,
        entity="task_card",
        entity_id=ranking.card_id,
        kind="SELECTION",
        actor_agent_id=actor_agent_id,
        payload=payload,
        created_at=created_at,
    )
    session.flush()
    return card


def task_need_of(card: TaskCard) -> TaskNeed:
    """把 EN-02 的行折成比较输入。刻意只取三列 —— 多给一列就会有人顺手在纯函数里读库。"""
    return TaskNeed(
        card_id=card.id,
        skill_tags=tuple(card.skill_tags),
        difficulty=card.difficulty,
    )


def candidates_from_registry(session: Session) -> tuple[list[WorkerCandidate], list[tuple[str, str]]]:
    """读出候选：可用的 EN-03 行 + 各自**最新一份** EN-05 申报。返回 `(候选, 被拿掉的人和原因)`。

    三条读取口径必须先说清，否则这段会被改错：

    - **`capabilities` 只能从 EN-05 读**（`protocol/messages.py:117` 明写"EN-05 主字段，与 EN-03 不重复"），
      而一个 agent 在 EN-05 里可以有多行（PK 是 `(agent_id, declared_at)`）⇒ 生效的是 `declared_at`
      最新那一份；取"第一行"会在有历史申报的库上拿到过期技能表，而且**看不出来**。
    - **`tier / cost_band / max_parallel / current_load / 战绩两列`取 EN-03**，即注册生效口径，
      不是申报值 —— 申报超出生效集合在注册节点就被拒（`contracts/agent-protocol.md §3.1`），
      到这里不该再有"申报算不算数"的二次判断。
    - **`enabled` 是可用性，不是比较位**：停用的工具不进候选集。把它做成"键⑥"会让停用的工具
      以低分形式继续被选中，那正是 FR-011 把 `enabled` 与排序分开的原因。

    没有能力申报的启用工具**不静默消失**：它作为 `(agent_id, 原因)` 进第二个返回值，
    由调用方传给 `rank_candidates(extra_excluded=...)` 落进留痕。
    """
    units = list(session.scalars(select(AgentUnit).where(AgentUnit.enabled.is_(True))))
    latest: dict[str, CapabilityManifest] = {}
    for manifest in session.scalars(select(CapabilityManifest)):
        current = latest.get(manifest.agent_id)
        if current is None or manifest.declared_at > current.declared_at:
            latest[manifest.agent_id] = manifest

    candidates: list[WorkerCandidate] = []
    dropped: list[tuple[str, str]] = []
    for unit in units:
        if unit.agent_id in REVIEW_ONLY_AGENT_IDS:
            # 审查身份只挂审查岗：它在 EN-03 里是启用的，但执行选人 MUST NOT 选中它
            # （Phase 17 C-P1-6）。这里不靠"能力集合恰好不重合"来保证 —— 卡上的 skill_tags
            # 由拆单器给，任何人加一个 `code_review` 标签就能把它拉去执行。
            dropped.append((unit.agent_id, "review-only 身份 ⇒ 只挂审查岗，不参加执行选人"))
            continue
        # 变量名不叫 `manifest`：上面那个 for 已经用同名变量遍历过 EN-04 的整批行，
        # 复用一个"非 Optional"的名字会让这次取到的 Optional 变成一次静默的类型谎。
        declared = latest.get(unit.agent_id)
        if declared is None:
            dropped.append((unit.agent_id, "已启用但没有能力申报 ⇒ 无 capabilities，键①无法计算"))
            continue
        candidates.append(
            WorkerCandidate(
                agent_id=unit.agent_id,
                capabilities=frozenset(declared.capabilities),
                tier=unit.tier,
                cost_band=str(unit.cost_band),
                current_load=unit.current_load,
                max_parallel=unit.max_parallel,
                historical_success_rate=unit.historical_success_rate,
                success_rate_sample=unit.success_rate_sample,
            )
        )
    return candidates, dropped
