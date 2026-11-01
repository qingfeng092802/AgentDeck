"""T032 + T034：`Split` 节点 —— 拆单产出结构化任务清单（FR-004｜R-03）**与**验收映射表（FR-007｜R-10）。

FR-004 的四项必填（任务名 / 难度 / 所需技能 / 依赖）在这里第一次成为**可拒绝的形状**：
拆单器给不出合法形状就是 `SplitViolation`，而不是"先收下，等下游崩"。
FR-007 要求映射表**在拆单时同步生成**，所以它的形状判据（`validate_links`）与卡的形状判据放在同一个模块里，
但仍是两个各管一头的单点：`validate_split` 管卡，`validate_links` 管映射（见下面第四条边界）。

**为什么这里产出的是草稿（`CardDraft`）而不是 EN-02 行**：本节点的回答是"清单形状 + 集合规则"，
把建行留在下游，是因为**建行需要一份合法的依赖结构**，而结构校验归 `normalize.py`（FR-005）——
先建后改会把一张卡推过两次状态迁移。EN-02 的列在 T036 之前还不全可满足：
`executor_agent_id` / `executor_role_id` 当时是 NOT NULL 带外键，而拆单时刻还没有执行人
（指派归 Phase 5 的 `SelectWorker`），这个缺口当时登记为 `data-model.md §5` 的 **T-14**。
**owner 2026-10-12 决策 2 取候选 ①**（两列改可空）⇒ 现在"拆出来还没有人"是合法行而不是假事实，
建行落在 `orchestrate/graph.py` 的 `_normalize`（校验通过才建行，`status=DRAFT`）。
本节点仍**不写库**：形状判据与写库时点分处两地，各自的用例才好归因。

**拆单器为什么没有默认实现**：与 T030 的补全器同一条理由（owner 2026-10-12 裁决 2 已确认那个形态）——
FR-004 的主语也是总管，实体归 Phase 3 的适配器。缺实现 ⇒ 抛 `SplitNotConfigured`，
不返回一份"看起来像拆单结果"的固定清单。

**与其他节点的边界**（四个单点，各写一遍就成两处强制）：
① 单张卡的形状、卡号形态 / 唯一 / 清单非空 = 本模块（`validate_draft` / `validate_split`）；
② "依赖引用了不存在的卡"与"成环" = `normalize.py`（FR-005）；
③ 映射表与这批卡的覆盖关系 = 本模块的 `validate_links`（FR-007），它读卡的 `acceptance_criteria`
   但**不**改卡，也不校验依赖；
④ 建行与落库时点 = `orchestrate/graph.py`（T036）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from acp.orchestrate.nodes.plan import validate_summary_digest
from acp.protocol import ArtifactFloor
# StrictModel 是协议层的 `extra="forbid"` 基类。CardDraft 不是跨 Agent 消息（它是编排阶段的中间形状），
# 但"多余字段一律拒"这条对它同样成立 —— 复用基类而不是再抄一份 model_config，是为了少一个漂移点。
from acp.protocol.refs import StrictModel

# EN-02 的说明列："形如 T01" ⇒ 前缀 T + 至少两位数字。放宽到任意字符串会让 `depends_on`
# 的引用比对失去意义（"T1" 与 "T01" 会被读成两张不同的卡）。
CARD_ID_PATTERN = re.compile(r"^T\d{2,}$")


class SplitViolation(ValueError):
    """拆单结果不成立。消息里只带字段名与卡号，不带需求原文。"""


class SplitNotConfigured(RuntimeError):
    """没有可用的拆单器（实体 = Phase 3 的适配器）。"""


class AcceptanceMapNotConfigured(RuntimeError):
    """有拆单器但它不申报验收映射（FR-007 的那半件缺实现）。"""


class CardDraft(StrictModel):
    """一张任务卡在拆单阶段的形状。

    字段面按 **EN-02 的列**取，不多不少：四项必填是 FR-004 点名的，`artifact_floor` 与
    `acceptance_criteria` 则因为 EN-02 把它们建成 NOT NULL（FR-017 的三项下限"由任务卡声明"、
    FR-029 的逐条比对依据），拆单时不给就等于让下游临时想验收标准（Story 1 AC6 反对的正是这个）。
    `token_budget` / `deadline_minutes` 不在这里：前者属预算与卡点 C（Phase 10），后者可空。
    """

    task_id: str
    title: str
    difficulty: int
    skill_tags: list[str]
    depends_on: list[str]
    artifact_floor: ArtifactFloor
    acceptance_criteria: list[str]


@runtime_checkable
class CardSplitter(Protocol):
    """拆单能力的对外形状（实现体 = Phase 3 的总管适配器）。

    两个方法是**同一次拆单的两件产物**（FR-004 与 FR-007），不是两次决策：
    `acceptance_links` 的入参与 `split` 相同，所以适配器只能在已经看过同一份摘要的前提下
    同时给出"卡"和"需求 → 验收条目"的对应关系。
    """

    def split(self, summary_digest: Mapping[str, list[str]]) -> Sequence[CardDraft]: ...

    def acceptance_links(self, summary_digest: Mapping[str, list[str]]) -> Sequence["AcceptanceLink"]: ...


class AcceptanceLink(StrictModel):
    """一条"需求条目 → 验收判据"对应，落在哪张卡上（EN-07 的一行）。

    **为什么三样内容都由总管申报，而不是平台自己拼**：
    ① `requirement_item` 要的是"原始需求里的哪一条"。FR-001 让入口只有一段自由文本，
       EN-01 已经逐字存着它 ⇒ 平台若把整句 `raw_input` 抄进每一行，既造出原文副本
       （原则 I，`summary_digest` 那条已经立过这个先例），又让 FR-030 的"对照原始需求与映射表
       **逐条**判定"落空——每一行都指向同一整句，等于没有对应关系。谁能指出"哪一条"？只有做
       补全与拆单的那个总管适配器（FR-007 的主语也正是它）。
    ② `produced_by` = 总管自己的 `agent_id`。EN-07 文档明写这列**不加外键**（不自行收紧），
       所以它的权威来源只能是申报方；平台编一个 id 就是往唯一事实态写假事实。
    """

    task_id: str
    requirement_item: str
    acceptance_item: str
    produced_by: str


class MappingViolation(ValueError):
    """验收映射不成立。消息里只带字段名与卡号 / 条目数，不带需求原文。"""


def _items(value: Sequence[str], *, label: str, allow_empty: bool) -> list[str]:
    if not value and not allow_empty:
        raise SplitViolation(f"{label} 为空")
    stripped = []
    for raw in value:
        if not isinstance(raw, str):
            raise SplitViolation(f"{label} 里有非字符串条目")
        item = raw.strip()
        if not item:
            raise SplitViolation(f"{label} 里有空白条目")
        stripped.append(item)
    if len(set(stripped)) != len(stripped):
        raise SplitViolation(f"{label} 里有重复条目")
    return stripped


def validate_draft(draft: CardDraft) -> CardDraft:
    """FR-004 的单卡形状判据（唯一强制点）。

    `skill_tags` 非空这一条不是我加的偏好：`plan.md` 设计项 8 把 `skill_fit` 定义为
    `|tags ∩ capabilities| / |tags|`，空集合直接除零，而那里明写"`|skill_tags| = 0` ⇒ 断言失败"。
    拦在拆单比让选人在运行时崩好。`difficulty` 只查类型不查区间 —— 文档与库里都没有给它上界下界，
    自造一个区间就是把实现者的猜测写成需求。
    """
    if not draft.title.strip():
        raise SplitViolation("title 为空白（FR-004 的任务名）")
    _items(draft.skill_tags, label="skill_tags", allow_empty=False)
    _items(draft.acceptance_criteria, label="acceptance_criteria", allow_empty=False)
    _items(draft.depends_on, label="depends_on 里的引用", allow_empty=True)
    return draft


def validate_split(drafts: Sequence[CardDraft]) -> tuple[CardDraft, ...]:
    """集合级规则：清单非空、卡号形态合法、卡号在本清单内唯一。

    唯一性这条与裁决 2 的复合主键是同一件事的两个强制点吗？不是 —— 这里管的是**一次拆单内部**
    不能出现两张同号牌（那连"依赖指向谁"都读不出来），DB 管的是**跨批次插入**时同单不得重名。
    两者规则同源、时点不同：本处拒在内存里（早、免费），DB 拒在写入时（兜住任何绕过本节点的旁路）。
    """
    if not drafts:
        raise SplitViolation("拆出 0 张卡：FR-004 要求产出结构化任务清单")
    ids: list[str] = []
    for draft in drafts:
        validate_draft(draft)
        if not CARD_ID_PATTERN.fullmatch(draft.task_id):
            raise SplitViolation(f"卡号 {draft.task_id!r} 不符合 EN-02 的形态（形如 T01）")
        ids.append(draft.task_id)
    if len(set(ids)) != len(ids):
        raise SplitViolation(f"卡号重复：{sorted(i for i in set(ids) if ids.count(i) > 1)}")
    return tuple(drafts)


def split_cards(
    summary_digest: Mapping[str, list[str]], *, splitter: CardSplitter | None
) -> tuple[CardDraft, ...]:
    """`Split` 节点本体。入参必须是 `plan` 节点认可的摘要 —— 判据复用，不在这里重写一遍。"""
    if splitter is None:
        raise SplitNotConfigured(
            "拆单器未注入：总管实体是适配器，归 Phase 3（T038 ~ T048）；此处不给规则版默认值"
        )
    validate_summary_digest(dict(summary_digest))
    return validate_split(splitter.split(summary_digest))


def validate_links(
    drafts: Sequence[CardDraft], links: Sequence[AcceptanceLink]
) -> tuple[AcceptanceLink, ...]:
    """FR-007 的形状判据（唯一强制点）：映射必须**不多不少地**覆盖这批卡。

    四条都是"验收阶段还能不能逐条判定"的直接后果，不是风格：
    ① 每条 link 的四个字段非空白（空白条目是凑数）；
    ② `task_id` 必须在这批卡里 —— 库里那条复合外键也会拦（`fk_acceptance_map_*`），
       但这里拒在内存里更早、报错更可读（同一规则同源、时点不同，与 `validate_split` 的
       卡号唯一性是同一个处置）；
    ③ 每张卡的**每一条** `acceptance_criteria` 恰有一条 link 覆盖：少一条，FR-030 的
       "对照映射表逐条判定"就漏判那条；多一条（同一判据被映射两次），轮次上限
       （宪法 原则 III 的硬值）就会被重复计数偷走 —— 上限 3 的语义是"三轮判定"，
       不是"三轮 × 每轮判两遍"；
    ④ link 里的 `acceptance_item` 必须是该卡声明过的判据：借映射表塞进卡上没写的标准，
       与 FR-029"MUST NOT 引入额外标准"是同一件事，只是发生在生成侧而不是审查侧
       （EN-02 的 `review_standard_locked` 默认 true 也指着这条）。

    一次收齐全部原因（同 `normalize.DependencyViolation`）：退回给总管重做时，
    只说第一条要多撞一轮，而这一轮的等待成本由使用者付。
    """
    reasons: list[str] = []
    by_card: dict[str, CardDraft] = {}
    for draft in drafts:
        by_card[draft.task_id] = draft

    for index, link in enumerate(links):
        for field in ("task_id", "requirement_item", "acceptance_item", "produced_by"):
            value = getattr(link, field)
            if not isinstance(value, str) or not value.strip():
                reasons.append(f"第 {index + 1} 条映射的 {field} 缺失或为空白")
        if link.task_id not in by_card:
            reasons.append(f"第 {index + 1} 条映射指向 {link.task_id}，而这张卡不在这批清单里")
            continue
        declared = by_card[link.task_id].acceptance_criteria
        if link.acceptance_item not in declared:
            reasons.append(
                f"{link.task_id} 的映射写了卡上没有的判据：{link.acceptance_item!r}"
            )

    covered: dict[tuple[str, str], int] = {}
    for link in links:
        key = (link.task_id, link.acceptance_item)
        covered[key] = covered.get(key, 0) + 1
    for task_id, draft in by_card.items():
        for item in draft.acceptance_criteria:
            count = covered.get((task_id, item), 0)
            if count == 0:
                reasons.append(f"{task_id} 的判据「{item}」没有对应映射（FR-030 会漏判）")
            elif count > 1:
                reasons.append(f"{task_id} 的判据「{item}」被映射 {count} 次（重复计轮次）")

    if reasons:
        raise MappingViolation("；".join(reasons))
    return tuple(links)


def split_with_acceptance_map(
    summary_digest: Mapping[str, list[str]], *, splitter: CardSplitter | None
) -> tuple[tuple[CardDraft, ...], tuple[AcceptanceLink, ...]]:
    """FR-004 + FR-007 的同步出口：一批卡 **与** 它们的验收映射，出自同一次拆单。

    形状判据仍然只有一个强制点：卡走 `validate_split`，映射走 `validate_links`，
    本函数只是把两者按文档要求的"同步生成"串起来，不重复实现任何一条规则。

    **`acceptance_links` 缺失时 fail closed**：Phase 3 的适配器可能在补上 FR-007 之前先注册进来，
    那种情况下"拆单成功但没有映射表"读起来像成功 —— 而 FR-030 的逐条验收会在运行时才发现没依据。
    所以这里点名报错，与补全器 / 拆单器的"没有默认实现"是同一条处置。
    """
    drafts = split_cards(summary_digest, splitter=splitter)
    assert splitter is not None  # split_cards 已在 None 时抛出，这里只为让下面的取用可静态读通
    links_fn = getattr(splitter, "acceptance_links", None)
    if links_fn is None:
        raise AcceptanceMapNotConfigured(
            "拆单器没有申报 acceptance_links：FR-007 的映射表只能由总管给出，平台不代拼"
        )
    return drafts, validate_links(drafts, list(links_fn(dict(summary_digest))))
