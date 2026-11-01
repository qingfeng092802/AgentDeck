"""T062：把关矩阵的闭合判据（FR-034 / FR-031｜R-09）。

**这个模块只回答一个问题**："这三张把关席位填得满吗" —— 填不满就抛 `GuardNotClosable`
（HTTP 侧 `422 guard_not_closable`），MUST NOT 降级成"让产出方自己审"（FR-034 原文）。
选人的**排序**不归这里（那是 Phase 5 的 `SelectWorker` 四要素全序），三岗互斥的**断言**
也不归这里（那是 Phase 7 的 `guard/triple.py`）；这里管的是"连一个合法三元组都不存在"这种
更早的失败 —— 两条判据在同一个出口汇合（`contracts/approval-api.md §7` 的 `guard_not_closable`
一行写的就是"三岗矩阵无法闭合而试图放行"）。

**MVP-1 的落地形态只到"响亮失败"为止**，这点必须说清免得被读成已生效：
`data-model.md §2` 给这条边的终点写的是 `PENDING_APPROVAL(kind=B) ──▶ ESCALATED`，
而 B 类卡在 MVP-1 是结构预留（`approval/service.py` 的 `ACTIVE_KINDS` 只有 A / D），
所以矩阵无法闭合时这里抛错而**不是一张 B 类单**。"升人工裁决"要到 B/C 启用才有产品形态
—— 登记在 `evidence/phase-04.md §T062` 的未闭段。

两处刻意不收的东西：
- **不写第二份三岗互斥判据**：`distinctness` 在这里只用来判"填得满填不满"（存在性），
  真出现一份填好的三元组要入库时，权威断言在 Phase 7 那一处（原则 I）。
- **不做技能契合**：那是排序键，不是可行性条件；在这里做等于把 Phase 5 的判据抄第二份。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from sqlalchemy import select

from acp.guard import triple as gp
from acp.routing import EXECUTOR_API_AGENT, routing_of, seat_identity, seat_model, seat_preference
from acp.persistence import events
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import RoleSpec
from acp.review.policy import (
    DEEPSEEK_REVIEWER,
    REVIEW_MODE_DEEPSEEK,
    REVIEW_MODE_MANUAL,
)

ROLES = ("executor", "reviewer", "acceptor")
NODE = "assign_guards"

#: 自审禁令（FR-034）：卡的产出方不许占这张卡的审查岗或验收岗。
#: 执行岗**不在禁令里** —— 产出方本来就是执行方，把它从执行岗排除等于没人能改这份产物。
SELF_REVIEW_FORBIDDEN = ("reviewer", "acceptor")


class GuardNotClosable(ValueError):
    """矩阵无法闭合。`reasons` 逐条给依据，`code` 是契约 §7 里的那个键名。"""

    code = "guard_not_closable"

    def __init__(self, reasons: tuple[str, ...], card_id: str | None = None) -> None:
        self.reasons = reasons
        self.card_id = card_id
        super().__init__(
            f"guard_not_closable：{'；'.join(reasons)}（卡 {card_id}）"
            if card_id
            else f"guard_not_closable：{'；'.join(reasons)}"
        )


@dataclass(frozen=True)
class Candidate:
    """一个可挂的候选：`mounted_roles` 是 **EN-04 生效集合**，不是 `agents.yaml` 的申报值。

    口径来自 `contracts/agent-protocol.md §3.1`（申报超出生效集合 ⇒ 注册节点就拒），
    所以到了这里不该再有"申报算不算数"的二次判断。
    """

    agent_id: str
    mounted_roles: frozenset[str]

    def can(self, role: str) -> bool:
        return role in self.mounted_roles


@dataclass(frozen=True)
class CardNeed:
    """一张卡的把关需求。`produced_by` 是这张卡的产出方（FR-007 的 `acceptance_map.produced_by`
    或 EN-02 的执行方 —— 两者此刻都可能是 None，None 时自审禁令自然不触发，不是绕过）。"""

    card_id: str
    produced_by: str | None = None


@dataclass(frozen=True)
class Closability:
    """一张卡的可行性结论。`assignments` 为空即不可闭合。"""

    card_id: str
    assignments: list[tuple[str, str, str]] = field(default_factory=list)
    reasons: tuple[str, ...] = ()

    @property
    def closable(self) -> bool:
        return bool(self.assignments)


def _eligible(role: str, pool: list[Candidate], produced_by: str | None) -> list[str]:
    banned = produced_by if role in SELF_REVIEW_FORBIDDEN else None
    return sorted(candidate.agent_id for candidate in pool if candidate.can(role) and candidate.agent_id != banned)


def closability(card: CardNeed, pool: list[Candidate]) -> Closability:
    """存在性判定：在候选池里能不能挑出三个**互不相同**且各自有岗的 agent。

    穷举而不是贪心：贪心会在"唯一能当审查的人也唯一能当执行"这类形状上给出假阴/假阳，
    而 MVP-1 的池子是 3 个量级，穷举的代价是常数。
    """
    by_role = {role: _eligible(role, pool, card.produced_by) for role in ROLES}
    if len(pool) < len(ROLES):
        return Closability(card.card_id, [], (f"候选池只有 {len(pool)} 支工具，凑不满 {len(ROLES)} 个岗（FR-031）",))
    missing = [role for role in ROLES if not by_role[role]]
    if missing:
        return Closability(
            card.card_id,
            [],
            tuple(f"{role} 岗没有可挂的候选（在岗集合与自审禁令之后为空）" for role in missing),
        )
    # 三个岗各一层循环，而不是 `itertools.product(*(...))`：后者的静态类型是
    # `tuple[str, ...]`（长度未知），`Closability.assignments` 要的是三元组，于是要么
    # cast 要么 ignore。三层循环写出来的就是 `tuple[str, str, str]`，一个类型声明都不用改，
    # 而"互不相同"的判据仍是同一句 `len(set(...)) == len(ROLES)`。
    triples: list[tuple[str, str, str]] = []
    for executor in by_role["executor"]:
        for reviewer in by_role["reviewer"]:
            for acceptor in by_role["acceptor"]:
                if len({executor, reviewer, acceptor}) == len(ROLES):
                    triples.append((executor, reviewer, acceptor))
    if not triples:
        return Closability(
            card.card_id,
            [],
            (
                "三岗候选都存在但凑不出两两不同的三元组 —— 同工具换 role 不算两个岗"
                "（FR-031 的判定单位是 agent_id，不含 role_id）",
            ),
        )
    return Closability(card.card_id, sorted(triples))


def assert_closable(cards: list[CardNeed], pool: list[Candidate]) -> dict[str, tuple[str, str, str]]:
    """全批可行 ⇒ 返回每张卡的**第一个**合法三元组；任一卡不可行 ⇒ 整批抛错、一岗都不指。

    "整批不指"是刻意的：逐卡填到一半再抛，库里就留下一份"部分指派"的卡集，
    而三岗互斥在部分指派上是**看不见的**（未指派的那些卡还没进断言）。
    """
    picked: dict[str, tuple[str, str, str]] = {}
    blocked: list[str] = []
    for card in cards:
        result = closability(card, pool)
        if not result.closable:
            blocked.extend(f"{card.card_id}: {reason}" for reason in result.reasons)
            continue
        picked[card.card_id] = result.assignments[0]
    if blocked:
        raise GuardNotClosable(tuple(blocked))
    return picked


# 本模块**不导出图节点函数**：T062 交的是"422 的出口"这一条判据，节点真正接进图的位置在
# `plan.md:251`（`AssignGuards` 在 `Dispatch` 之前）与 `plan.md:386`（`SelectWorker` 先于它，
# owner 2026-10-14 裁待裁 ⑨ 已把这两句的顺序定下来）⇒ 接线归 T086，本节下面就是 T086 的节点体。
# 留一个只抛错的空节点等于留一处"看起来接好了"的假象，所以 `route()` / `assign()` 之前的那批
# 只判存在性的函数（`closability` / `assert_closable`）保持不变 —— 它们是 T062 的 422 出口，
# 与 T086 的"填坑并落库"是两件事，混在一起就会让 422 那条判据失去单一触发点。

# ------------------------------------------------------------------ T086：矩阵闭合的路由与落库

#: 把关两个席位各自要求的**岗位**（EN-04 的 `role_id`）。
#: 执行岗不在这里 —— 执行者由 `SelectWorker` 按能力与负载选（FR-022），它的岗位是"前端 / 文档"这类工作岗，
#: 而这里两个席位要的岗是 `registry/roles/` 种下的那两个只读岗（T087）。
SEAT_ROLES: dict[str, str] = {"reviewer": "reviewer", "acceptor": "acceptor"}

#: 宪法原则 III 规则 2 点名的总管工具（含其兼任的架构师岗）。
#: 这一串是**判定输入**而不是显示名：它出现在"执行者是谁"的比较里，所以必须与注册表的 `agent_id` 同值。
ORCHESTRATOR_AGENT = "workbuddy"

#: 执行者是总管时，两个把关席位**只能**落在这两支里（规则 2 的白名单）。
#: 它比"排除执行者"更严：将来接入第四支工具后，总管执行的任务仍然不许把把关交给那第四支。
ORCHESTRATOR_GUARD_WHITELIST = frozenset({"qoder", "doubao"})


@dataclass(frozen=True)
class GuardRoute:
    """一张卡的把关路由结论。

    `narrowed` 表示这一张走过规则 2 的白名单（不是"顺手记个 bool"：判据要能分开
    "因为白名单才闭合"和"本来就能闭合"，否则规则 2 被删掉时只有端到端那条会红，定位不回来）。
    """

    card_id: str
    executor: str
    reviewer: str
    acceptor: str
    narrowed: bool
    eligible: dict[str, tuple[str, ...]]
    #: 请求级路由被采纳的偏好（2026-10-31 路线 B）；空字典 = 这一单没点关。
    applied: dict[str, str] = field(default_factory=dict)
    #: 点了但**不在当席候选里**的偏好：不采纳，但进留痕（`routing_ignored`），不静默。
    ignored: dict[str, str] = field(default_factory=dict)

    def triple(self) -> dict[str, str]:
        return {"executor": self.executor, "reviewer": self.reviewer, "acceptor": self.acceptor}


def pool_from_role_specs(session, *, mode: str = REVIEW_MODE_MANUAL) -> list[Candidate]:
    """三岗候选池：从 `role_spec.mounted_agents`（EN-04 **生效口径**）反推每支工具能挂哪些岗。

    读 EN-04 而不是 `agents.yaml` 的申报值，口径来自 `contracts/agent-protocol.md §3.1`：
    申报超出生效集合在注册节点就被拒，到这里不该再有第二次判断。

    这个函数从 `api/tasks.py::_guard_pool` **搬过来**（不是复制）：覆盖路径与自动路径 MUST 读同一份
    生效口径，两处各写一遍就会在两处各自漂（原则 I）。搬动之后 API 侧只剩一行转发，判据不变。

    **`mode` 是运行模式契约，不是评分结果**（Phase 17 D6）：
    - `manual`（缺省）：`deepseek-reviewer` 整支从池里剔除 —— Phase 16 的三岗候选逐字不变；
    - `deepseek`：`deepseek-reviewer` 进入池（它只挂 reviewer 岗），其余工具的挂载原样保留；
      审查席的收窄在 `eligible_guards` 里按同一模式做（总管执行的卡仍受宪法规则 2 的白名单约束）。
    默认值取 `manual` 是刻意的：任何没有显式传模式的旧调用（CLI、覆盖端点、历史用例）
    都落在 Phase 16 的行为上，而不是悄悄把模型身份拉进候选。
    """
    mounts: dict[str, set[str]] = {}
    for spec in session.scalars(select(RoleSpec)):
        for agent_id in spec.mounted_agents:
            identity = str(agent_id)
            if identity == DEEPSEEK_REVIEWER and mode != REVIEW_MODE_DEEPSEEK:
                continue
            mounts.setdefault(identity, set()).add(str(spec.role_id))
    return [
        Candidate(agent_id=agent_id, mounted_roles=frozenset(roles))
        for agent_id, roles in sorted(mounts.items())
    ]


def eligible_guards(
    executor: str, pool: list[Candidate], *, mode: str = REVIEW_MODE_MANUAL
) -> tuple[dict[str, list[str]], bool]:
    """规则 1（不等于执行者）+ 岗位挂载要求 + 规则 2（总管白名单）之后的各席候选。

    返回 `(候选表, 是否走过白名单)`。白名单只在"执行者 = 总管"这一支生效，其余情况原样放行 ——
    顺序是定的：**先排除自审、再收窄**，反过来会把"本来就有资格的独立工具"报成"被白名单挡了"，
    那种理由写在审计行里会误导人工裁决。

    `deepseek` 模式把审查席收窄到 `deepseek-reviewer`；唯一的例外是**执行者 = 总管**的卡：
    宪法规则 2 要求这类卡的两个把关席只能是 `qoder` / `doubao`，所以那一支仍按白名单走人工审查 ——
    这不是模式失效，是宪法约束优先，且这类卡不会被 `ReviewRunner` 误当成可自动审查的卡。
    """
    narrowed = executor == ORCHESTRATOR_AGENT
    out: dict[str, list[str]] = {}
    for seat, role in SEAT_ROLES.items():
        keep = [
            candidate.agent_id
            for candidate in pool
            if candidate.can(role) and candidate.agent_id != executor
        ]
        if narrowed:
            keep = [agent_id for agent_id in keep if agent_id in ORCHESTRATOR_GUARD_WHITELIST]
        elif seat == "reviewer" and mode == REVIEW_MODE_DEEPSEEK:
            keep = [agent_id for agent_id in keep if agent_id == DEEPSEEK_REVIEWER]
        out[seat] = sorted(set(keep))
    return out, narrowed


def route(
    card_id: str,
    executor: str | None,
    pool: list[Candidate],
    *,
    mode: str = REVIEW_MODE_MANUAL,
    preference: Mapping[str, str | None] | None = None,
) -> GuardRoute:
    """把两个把关席位填上。填不满 ⇒ `GuardNotClosable`（这是"升人工"那条边的应用层出口）。

    配对取**字典序最小的合法配对**，MVP-1 不给把关岗排序：把关人的"谁更合适"属 Phase 5 的判据，
    在这里再排一遍就是抄第二份口径（原则 I）。要按契合度挑把关人，归 MVP-2 的看板那一批。

    `preference`（2026-10-31 路线 B）是**偏好**不是命令：点了名的工具只有在该席候选里才会被排到最前；
    不在候选里的进 `ignored`（留痕），其余规则（自审禁令、白名单、三岗互斥）一条都不放松。
    """
    if executor is None:
        raise GuardNotClosable(
            (f"卡 {card_id} 还没有执行者 ⇒ 规则 1（把关工具 MUST ≠ 执行者）无从判定",),
            card_id,
        )
    candidates, narrowed = eligible_guards(str(executor), pool, mode=mode)
    distinct = {agent_id for seat in SEAT_ROLES for agent_id in candidates[seat]}
    if len(distinct) < len(SEAT_ROLES):
        raise GuardNotClosable(
            (
                f"可承担把关岗的去重工具只有 {sorted(distinct)}（各席候选={candidates}）"
                f"⇒ 少于 {len(SEAT_ROLES)} 支，两个席位填不满",
                "处置=升卡点 B 人工裁决（FR-034），MUST NOT 降级为产出方自审放行",
            ),
            card_id,
        )
    requested = {seat: (preference or {}).get(seat) for seat in SEAT_ROLES}
    ordered: dict[str, list[str]] = {}
    for seat in SEAT_ROLES:
        values = list(candidates[seat])
        preferred = requested[seat]
        if preferred and preferred in values:
            values.remove(preferred)
            values.insert(0, preferred)
        ordered[seat] = values
    for reviewer in ordered["reviewer"]:
        for acceptor in ordered["acceptor"]:
            if reviewer != acceptor:
                chosen = {"reviewer": reviewer, "acceptor": acceptor}
                # 每一条偏好**恰好**落进 applied 或 ignored 之一：落选可能因为不在候选里，
                # 也可能因为把那个席位让给了互斥（例如执行者自己）。只记"被排到最前"会把
                # "点了却没坐上"读成"生效了"。
                applied = {seat: pref for seat, pref in requested.items() if pref and chosen.get(seat) == pref}
                ignored = {seat: pref for seat, pref in requested.items() if pref and chosen.get(seat) != pref}
                return GuardRoute(
                    card_id=card_id,
                    executor=str(executor),
                    reviewer=reviewer,
                    acceptor=acceptor,
                    narrowed=narrowed,
                    eligible={seat: tuple(values) for seat, values in candidates.items()},
                    applied=applied,
                    ignored=ignored,
                )
    raise GuardNotClosable(
        (f"两支候选都存在但凑不出两个不同的把关人（各席候选={candidates}）⇒ 同工具换岗不算两个人",),
        card_id,
    )


def assign(
    session,
    *,
    feature_request_id,
    task_id: str,
    created_at,
    actor_agent_id: str,
    pool: list[Candidate] | None = None,
    mode: str = REVIEW_MODE_MANUAL,
) -> GuardRoute:
    """T086 的节点体：**下发前**把两个把关席位填上、断言三岗互斥、落库并留一条三元组审计行。

    顺序是判据的一部分：`断言 → 落库`，不是 `落库 → 断言`。反过来的话，一次失败的指派会在库里
    留下一行"指派已填但把关不独立"的卡，而那张卡下一步就会被派发出去。
    """
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise GuardNotClosable((f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在",), task_id)
    # 请求级路由（2026-10-31 路线 B）：把关席的偏好从 EN-01 的 `model_routing` 读。
    # 读了不采纳时 `route()` 会把原因放进 `ignored`，下面一并落进留痕 —— 不让"点了没生效"静默。
    request = session.get(FeatureRequest, feature_request_id)
    routing = routing_of(request) if request is not None else None
    preferences = {seat: seat_preference(routing, seat) for seat in SEAT_ROLES}
    # 路线 C（2026-10-31）：被点名成"模型实例"的把关席不再走 EN-04 候选池 —— 它们由通用
    # 审查/验收 Runner 调用；载体制用已有的 API agent（外键要一条真在册的 agent_id）。
    routed_reviewer = seat_model(routing, "reviewer") is not None
    routed_acceptor = seat_model(routing, "acceptor") is not None
    if routed_reviewer and routed_acceptor:
        picked = GuardRoute(
            card_id=card.id,
            executor=str(card.executor_agent_id),
            # 审查席的载体必须写 `deepseek-reviewer`：`ReviewRunner` 按这一列认"这张卡归我审"，
            # 写成通用 API 载体会让它整批 skip（2026-10-31 真机实测：reviewer_not_deepseek）。
            reviewer=DEEPSEEK_REVIEWER,
            acceptor=EXECUTOR_API_AGENT,
            narrowed=False,
            eligible={},
        )
    else:
        picked = route(
            card.id,
            card.executor_agent_id,
            pool if pool is not None else pool_from_role_specs(session, mode=mode),
            mode=mode,
            preference=preferences,
        )
        if routed_reviewer or routed_acceptor:
            picked = replace(
                picked,
                reviewer=DEEPSEEK_REVIEWER if routed_reviewer else picked.reviewer,
                acceptor=EXECUTOR_API_AGENT if routed_acceptor else picked.acceptor,
            )

    # 三岗互斥的**唯一**断言点在这里（T085）。`route()` 已经按规则 1 / 2 排过一遍，
    # 这里仍然要判：两处各判一次不是重复，而是"路由的规则"与"宪法不许同人"这两条约束来源不同 ——
    # 将来有人改窄了候选池（比如加了一支工具），路由会跟着变，而这条断言不该跟着变松。
    # 判定单位 = **席位实例身份**：模型席位是 `model:<vendor>:<model>:<key_fp>`，否则是 agent_id。
    # 2026-10-31 owner 裁决（路线 C）：自由路由下三席可能共用同一个 API 载体，互斥底线不变 ——
    # 同模型同 key 出现在两个席位仍被这一行拦下（判据见 test_assign_guards 的 collapse 组）。
    assignment = gp.Assignment(
        card.id,
        {
            "executor": gp.SeatOccupant(seat_identity(routing, "executor", picked.executor), None),
            "reviewer": gp.SeatOccupant(seat_identity(routing, "reviewer", picked.reviewer), SEAT_ROLES["reviewer"]),
            "acceptor": gp.SeatOccupant(seat_identity(routing, "acceptor", picked.acceptor), SEAT_ROLES["acceptor"]),
        },
    )
    audited = gp.assert_independent(assignment)

    card.reviewer_agent_id = picked.reviewer
    card.acceptor_agent_id = picked.acceptor
    payload = {
        "node": NODE,
        "action": "guards_assigned",
        "triple": audited,
        "narrowed": picked.narrowed,
        "eligible": {seat: list(values) for seat, values in picked.eligible.items()},
    }
    if picked.applied:
        payload["routing_applied"] = picked.applied
    if picked.ignored:
        payload["routing_ignored"] = picked.ignored
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=actor_agent_id,
        payload=payload,
        created_at=created_at,
    )
    session.flush()
    return picked

