"""T085：三岗互斥的**唯一**强制点（FR-031 / FR-034｜R-09，宪法原则 III）。

**宪法原话的形状**（`.specify/memory/constitution.md` 原则 III「验证方式」段）：
"编排引擎在下发审查/验收任务**之前**做静态断言三 `agent_id` 互不相同，断言不成立即触发 R-26 卡点并拒绝下发"。
⇒ 这一模块只做那一件事：**判定一份指派合不合法**。它不选人、不排序、不填坑。

**判定单位（宪法原则 III v1.0.4，2026-10-31）**：比对单位是**席位实例身份** —— 默认 `agent_id`；
模型席位由 `assign_guards` 传入实例指纹 `vendor:model:key_fp`（同模型同 Key 视为同一实例）。
不变量没变（三个席位两两不同），只换了"实例"的表示。条文原文：宪法写的是"比对单位是**席位实例身份**…"
（v1.0.4 前的措辞是 `agent_id`，历史引用见 `git show 5f1d968:.specify/memory/constitution.md`）。
不是角色"（原文用 `skill_id` 一词，`tasks.md` T085 用 `role_id` —— 这笔术语未统一，属 C7，登记在宪法自己的
Sync Report 里；两词指同一件事，本模块两者都不读）。`SeatOccupant.role_id` 这个字段**存在是为了让
"同工具换岗位"这个形状可以被构造出来**，判据 MUST NOT 读它 —— 读了就等于把"同一支工具自己审自己"
洗成"两个岗位"，而那正是把关独立性唯一被绕过的路子。所以本文件的判据配了一条专门的变异：
把比较对象换成 `(agent_id, role_id)` 之后，"同工具换岗"那条用例必须翻红（见 `evidence/phase-07.md`）。

**这里没有 fail-open 分支**，三种"信息不全"的形态各自是一条明确的拒绝而不是一句放行：
1. 某个岗是 `None` ⇒ `seat_missing`。`models_core.py:99-100` 明写"可空只表示指派尚未发生"，
   并且**指定本模块必须自己处理 NULL 这一态** —— 拿 `None != "qoder"` 当"两两不同"成立，
   就是把"还没指派"读成"指派得很安全"。
2. 三支里有重复 ⇒ `not_independent`，`violations` 逐对点名是谁撞了谁。
3. 岗位数与本模块声明的席位数不等 ⇒ `seat_count_mismatch`（这条防的是"有人加了第四个岗而这里只判三个"：
   判据写成关系式 `len({agent_id}) == len(SEATS)`，席位数变了它自动跟着变，不需要有人记得改常数）。

**与相邻三家的分工**（各管一件事，任何一处抄第二份就是原则 I 违例）：
- `assign_guards.py`（T062）判**存在性**："凑不凑得出一个合法三元组"，凑不出抛 `guard_not_closable`；
- `select_worker.py`（Phase 5）判**排序**："哪个候选更好"；
- 本模块判**合法性**："这份已填好的指派是不是三个不同的人"；
- **调用点在 `assign_guards.assign()`**：`assign_guards.py:306` 调 `assert_independent()`，先于同文件 `:325`
  的 `session.flush()` ⇒ "指派落库之前必过这道判定"这一半是**已生效**的。
  **"下发之前"那一格现在也有强制边了**：`orchestrate/graph.py` 的指派段上是
  `SelectWorker → AssignGuards → Dispatch → END`，进 `DISPATCH` 的入边**恰一条**且来自 `ASSIGN_GUARDS`
  ⇒ "没过三岗就派发"在那张图上不可表示（owner 2026-10-16 裁 甲 落地，判据 `tests/guards/test_dispatch_edge.py`
  节 一；欠账登记在 `tasks.md` 的 **T123** 行下，WorkBuddy `phase7-review-v1.0.1` 的 P1-1）。
  行号基准 = 本笔提交，写之前请复跑 `grep -n "add_edge(ASSIGN_GUARDS, DISPATCH)" src/acp/orchestrate/graph.py`。
  **仍未闭的那一半**如实记着：主管图仍停在 `ApprovalGate → END`（同文件 `:289`），所以"全链路真跑一次"
  不归本模块，归 T123 的装配点。本模块不复写第二份断言（原则 I），也不替那条边之外的东西背书。
`api/tasks.py:412-429` 那两处（自审禁令 + 可闭合存在性）是**覆盖路径**上的既有实现，
owner 2026-10-14 裁决"随 T085 落地改调用点、不改判据" ⇒ 本文件落笔后它改调这里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: 三岗的名字与**顺序**。顺序是留痕里的呈现顺序，也是"缺岗"那条理由的点名顺序；
#: 判据不依赖顺序（比较的是集合大小），改它不会让合法指派变成非法。
SEATS = ("executor", "reviewer", "acceptor")

#: 判定单位。写成常量是为了让"用了别的单位"这件事在源码里显眼，判据直接读它比对。
#: 2026-10-31 路线 C：自由路由下由 `assign_guards` 传入**席位实例身份**
#: （模型席位 = `model:<vendor>:<model>:<key_fp>`，否则 = `agent_id`）。
#: 不变量本身没松：三个席位的实例仍必须两两不同。
UNIT = "seat_identity"


class TripleViolation(ValueError):
    """这份指派不构成独立把关。`code` 是给端点与日志的稳定键名。"""

    code = "triple_violation"

    def __init__(self, message: str, *, code: str | None = None, violations: tuple[str, ...] = ()) -> None:
        if code is not None:
            self.code = code
        self.violations = violations
        super().__init__(message)


@dataclass(frozen=True)
class SeatOccupant:
    """一个席位上的 occupant。

    `role_id` **只携带、不参与判定**（模块头那段理由）。它留在这里还有第二个用途：
    审计行里要能看出"这支工具当时挂的是哪个岗"，否则"同工具换岗"这件事在库里就没有形态。
    """

    agent_id: str | None
    role_id: str | None = None

    def describe(self) -> str:
        return f"{self.agent_id!r}" + (f"（岗 {self.role_id}）" if self.role_id else "")


@dataclass(frozen=True)
class Assignment:
    """一份待下发的三岗指派。`card_id` 只出现在错误消息里，便于把理由对上对象。"""

    card_id: str
    occupants: Mapping[str, SeatOccupant]

    def seat(self, name: str) -> SeatOccupant:
        return self.occupants.get(name) or SeatOccupant(None, None)


def triple_of(assignment: Assignment) -> dict[str, str | None]:
    """审计用的三元组 `{seat: agent_id}`（每轮落盘可查，FR-031 的后半句）。

    键序 = `SEATS`，取值**只取 `agent_id`** ⇒ 这份 dict 里没有 `role_id`，
    因为三元组要被"两两不同"的审计复查（T097 第④条）读，混进岗名就会让复查第二次判到别的对象上。
    """
    return {seat: assignment.seat(seat).agent_id for seat in SEATS}


def violations_of(assignment: Assignment) -> tuple[str, ...]:
    """返回逐条理由；空元组 = 合法。判定只读 `agent_id`。"""
    reasons: list[str] = []
    missing = tuple(seat for seat in SEATS if assignment.seat(seat).agent_id is None)
    if missing:
        reasons.append(
            f"以下席位还没有 occupant：{sorted(missing)} ⇒ 可空只表示'指派尚未发生'，"
            "不许当作'两两不同'天然成立（`models_core.py:99-100`）"
        )
    present = {seat: assignment.seat(seat).agent_id for seat in SEATS}
    filled = {seat: agent for seat, agent in present.items() if agent is not None}
    if len(filled) >= 2:
        seen: dict[str, list[str]] = {}
        for seat, agent in filled.items():
            seen.setdefault(agent, []).append(seat)
        for agent, seats in sorted(seen.items()):
            if len(seats) > 1:
                reasons.append(
                    f"{agent!r} 同时占了 {'/'.join(sorted(seats))} 两个岗 ⇒ 判定单位是 {UNIT}（席位实例），"
                    "同一支工具换岗位不构成独立把关（FR-031）"
                )
    if len(filled) != len(set(filled.values())) and not reasons:
        reasons.append("去重后的工具数与席位数不相等 ⇒ 有两岗共用了同一支工具")
    if len(assignment.occupants) != len(SEATS):
        reasons.append(
            f"这份指派里的席位是 {sorted(assignment.occupants)}，与本模块声明的 {list(SEATS)} 不一致"
            " ⇒ 判据按席位数求distinct，多出来的岗没人判、少了的岗判不到"
        )
    return tuple(reasons)


def distinctness_violations(assignment: Assignment) -> tuple[str, ...]:
    """**只看已填席位**的两两不同 —— 完整性不在这里判。

    为什么要有第二个入口而不是让调用方都走 `assert_independent`：
    "下发前必须三岗齐"与"改派时不许撞人"是**两条不同要求**（前者来自 FR-031 的断言时机，
    后者来自 T071 覆盖端点那条"MUST 比 PATCH 更严"），但"什么叫做同人"这一条只有一份 ——
    两份各写就会漂。`api/tasks.py` 的覆盖路径用的就是这个入口。
    """
    reasons = [
        reason
        for reason in violations_of(assignment)
        if "还没有 occupant" not in reason and "与本模块声明的席位" not in reason
    ]
    return tuple(reasons)


def assert_distinct_so_far(assignment: Assignment) -> dict[str, str | None]:
    """部分指派下的断言：已填的岗 MUST 两两不同，未填的岗不管。合法 ⇒ 返回三元组。"""
    reasons = distinctness_violations(assignment)
    if reasons:
        raise TripleViolation(
            f"三岗互斥不成立（卡 {assignment.card_id}）：{'；'.join(reasons)}",
            code="not_independent",
            violations=reasons,
        )
    return triple_of(assignment)


def assert_independent(assignment: Assignment) -> dict[str, str | None]:
    """下发前的静态断言。合法 ⇒ 返回三元组（给留痕用）；非法 ⇒ 抛 `TripleViolation`。

    返回值就是 `triple_of()`：调用方拿到的是"已经判过的那份"，不需要在判完之后自己再拼一遍
    —— 拼第二遍就会造出第二个判定载体（原则 I），那是本模块最容易被误用的地方。
    """
    reasons = violations_of(assignment)
    if reasons:
        code = "seat_missing" if any("还没有 occupant" in reason for reason in reasons) else "not_independent"
        if len(assignment.occupants) != len(SEATS):
            code = "seat_count_mismatch"
        raise TripleViolation(
            f"三岗互斥不成立（卡 {assignment.card_id}）：{'；'.join(reasons)}",
            code=code,
            violations=reasons,
        )
    return triple_of(assignment)


def assert_batch_independent(assignments: Sequence[Assignment]) -> dict[str, dict[str, str | None]]:
    """整批判定：任一卡不合法 ⇒ 整批拒绝，返回的是**已经全批判过**的三元组。

    整批而不逐卡，与 `assign_guards.assert_closable` 同一个理由：逐卡填到一半再抛，
    库里留下一份"部分指派"，而三岗互斥在**尚未指派**的那些卡上是看不见的。
    """
    triples: dict[str, dict[str, str | None]] = {}
    blocked: list[str] = []
    for assignment in assignments:
        try:
            triples[assignment.card_id] = assert_independent(assignment)
        except TripleViolation as exc:
            blocked.extend(f"{assignment.card_id}: {reason}" for reason in exc.violations)
    if blocked:
        raise TripleViolation(
            f"{len(blocked)} 条理由，涉及 {len({line.split(':', 1)[0] for line in blocked})} 张卡 ⇒ 整批不下发",
            code="not_independent",
            violations=tuple(blocked),
        )
    return triples
