"""T033：`Normalize` 节点 —— 依赖结构校验（FR-005｜R-04）。

FR-005 给的两条出路在这里是**同一条代码路径**的两半：能靠重排救的就是"拓扑序"（成功返回的顺序
即"退回重排"的结果），救不了的那一类（成环、引用不存在）必须**拒**，并且给出原因 —— 因为
`data-model.md §4` 写的后果是"退回重排，不入 `READY`"，一句"失败"没有可执行性，
总管或使用者拿不到"哪条边坏"就没法重排。

**为什么环要报出路径而不是只报"有环"**：`T01 → T02 → T01` 这种形态的修法有两种（断开哪一条边），
只说"有环"等于把选择推回给调用方却不给材料。

**这里不检查的东西（刻意留给别的单点，避免同一规则两处强制）**：
① 清单非空 —— 归 `split.validate_split`（FR-004）；本节点收到空清单返回空顺序，不报错。
② 卡号形态 —— 归 `split`；本节点只把卡号当标识符比对。
③ `depends_on` 的数组本身是否合法（空白 / 重复）—— 归 `split.validate_draft`。
④ "禁环不写进 DB CHECK"是 `data-model.md §4` 的指派：跨行性质，数据库没有单条约束能表达它。
"""

from __future__ import annotations

from collections.abc import Sequence

from acp.orchestrate.nodes.split import CardDraft

DANGLING = "悬空引用"
SELF_DEPENDENCY = "自依赖"
CYCLE = "循环依赖"


class DependencyViolation(ValueError):
    """依赖结构不成立。`reasons` 是本次收集到的**全部**坏边，不是第一条。

    收集全部而不是一遇就抛：一次拆单里同时有环和悬空引用时，只报第一条会让使用者改完再撞第二次，
    而"退回重排"的成本是按轮次算的。
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__("；".join(self.reasons))


def _edges(drafts: Sequence[CardDraft]) -> dict[str, tuple[str, ...]]:
    return {draft.task_id: tuple(draft.depends_on) for draft in drafts}


def _find_cycle(edges: dict[str, tuple[str, ...]]) -> list[str] | None:
    """迭代式 DFS 找一条回边构成的环，返回环上的卡号（首尾同卡，读起来是闭合的）。

    只用"引用存在的边"来找：悬空引用是另一类原因，混进 DFS 会让环的路径里出现不存在的节点。
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in edges}
    for root in sorted(edges):
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        path: list[str] = []
        while stack:
            node, leaving = stack.pop()
            if leaving:
                path.pop()
                color[node] = BLACK
                continue
            if color[node] == GREY:
                continue
            if color[node] == BLACK:
                continue
            color[node] = GREY
            path.append(node)
            stack.append((node, True))
            for dep in sorted(edges[node]):
                if dep not in edges:
                    continue
                if color[dep] == GREY:
                    start = path.index(dep)
                    return path[start:] + [dep]
                if color[dep] == WHITE:
                    stack.append((dep, False))
    return None


def normalize(drafts: Sequence[CardDraft]) -> tuple[str, ...]:
    """FR-005 的唯一强制点：校验依赖结构并返回拓扑序。

    顺序是**确定的**：同一份输入连跑必须得到同一顺序（可复现是 `plan.md` 设计项 8 对选人的要求，
    这里同一条理由 —— 否则"退回重排"这一步本身就不可比对）。
    并列就绪的卡按卡号升序取出，作为末位 tie-breaker。
    """
    edges = _edges(drafts)
    reasons: list[str] = []

    for card_id in sorted(edges):
        for dep in edges[card_id]:
            if dep == card_id:
                reasons.append(f"{SELF_DEPENDENCY}: {card_id} 依赖自己")
            elif dep not in edges:
                reasons.append(f"{DANGLING}: {card_id} 依赖 {dep}，而 {dep} 不在这份清单里")

    if reasons:
        # 有悬空 / 自依赖时先不跑环检测：那类坏边会让 DFS 的可达集合失真，报出来的环更难读。
        raise DependencyViolation(reasons)

    cycle = _find_cycle(edges)
    if cycle is not None:
        raise DependencyViolation([f"{CYCLE}: {' → '.join(cycle)}"])

    remaining = {card_id: set(deps) for card_id, deps in edges.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(node for node, deps in remaining.items() if not deps)
        if not ready:
            # 到不了这里——环已在上一步拒掉。留着这行是因为：如果哪天有人改动上面而漏掉，
            # 这里必须炸而不是静默返回一个残缺顺序（静默的残缺顺序会被下游当成合法计划）。
            raise DependencyViolation([f"{CYCLE}: 无法归约的剩余卡集 {sorted(remaining)}"])
        first = ready[0]
        order.append(first)
        del remaining[first]
        for deps in remaining.values():
            deps.discard(first)
    return tuple(order)
