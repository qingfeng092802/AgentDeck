"""T089 + T090 + T088：审查节点 —— 每轮一条结论，逐条比对，且不给出可写的许诺。

三件事各自只有一种坏法能撞红，所以分三段写：

**① 结论有三类**（FR-028 / EN-08 `verdict`；Phase 17 P1 owner 2026-10-23 把两值扩成三值）。
域值不在这儿重述，取自 PG 枚举（`persistence.enums.values_of("review_verdict")`）—— 写第二份
字面量就是 `data-model.md §2` 那条"域值只引用不重述"规矩点名的形状。

`NEED_HUMAN` 不是"第三个通过/打回"，它是**人工升级**：一条 `met=None`（主观/不可判定）就足以
让整份结论落在这里。平台 MUST NOT 替模型把 `null` 猜成 `true` / `false` —— 那会把"判不了"
写成"判过了"或"判不过"，而两者都是假事实。**逐条覆盖仍然严格**：`met=None` 只改变结论种类，
不豁免 FR-029 的"少一条 / 多一条 / 重复一条"三拒。

**② 逐条比对**（FR-029）。审查依据**只有**任务卡上声明的 `acceptance_criteria`，
且每一条都要有一行 `item_findings`：
- 少一条 = 那条标准没被判过 ⇒ 拒入库（"整体尚可"不是一行判定）；
- 多一条 = 引入了卡上没有的标准 ⇒ `extra_criteria_used` 为真 ⇒ 拒入库（FR-029 原文"不得加个人偏好"）。
库里那条 `CHECK (extra_criteria_used = false)` 是第二道（`models_approval.py:70`）：
应用层被人改坏了时，它把"加个人偏好"变成一次 `IntegrityError` 而不是一次静默入库。
**两道都在，但出口不同**：应用层这一道给的是可复核的理由（哪几条多出来），库那一道只给约束名，
所以本模块 MUST 先在应用层拒，MUST NOT 靠 PG 兜 —— 这与 `handoff.py` 那两条上限的双道是同一个模式。

**③ 只读岗照样能出结论**（FR-033 / FR-057，T088 的适配器半边）。
`build_request()` 是本模块与"下发"唯一相关的产物：它给审查者的可见面是**产物正文**，
里面 MUST NOT 出现任何可写路径 —— 不给路径不是体贴，是把"顺手改一下格式"这个诱惑从请求里删掉；
拒写这件事本身仍只发生在 `bus/gateway.py` 一处（`acl.can_write`），这里不重复实现第二道拒。

**④ "每轮一条结论"不靠计数器，靠状态迁移本身**（FR-028 的"每轮产结论"读法）。
`record_verdict()` 只在 `REVIEWING` 上收结论；`APPROVE` / `CHANGES_REQUESTED` 收下即把卡移出
`REVIEWING` ⇒ 同一轮递第二次撞的是 `state_not_reviewing`，不需要也不该再有一列"本轮已收过几条"。
写成计数器的话，它就是一份独立于状态机的第二份事实（原则 I），而且会漂：计数器归零的时机没人管得着。

**`NEED_HUMAN` 是这条规则的唯一例外，且这个例外是刻意的**：它是"平台判不了、请人判"的升级记录，
不是"这一轮已经判完"。因此它**不移动卡态**（卡留在 `REVIEWING`），人工随后那条决定性结论是同一张卡
的第二条 EN-08，`round_no` 递增。若这里把卡推进 `ACCEPTING` 或 `CHANGES_REQUESTED`，人工下一次
提交会撞状态门 —— 那等于用一条"不确定"替人做了选择。重复的 `NEED_HUMAN` 仍会各留一行（它们各自
是一次审查尝试）；"不要建第二张 E 单"归 P2 的 `escalation_open`，本模块不越界做第二份去重规则。

**⑤ 本模块 MUST NOT 碰 EN-02 的两个计数列**（owner 2026-10-14 裁决 2，= WorkBuddy 的 P1-5）。
待裁 ⑬ 把轮次拆成两列之后，`review_round` 的定义**只有**"验收轮次"（`data-model.md` EN-02，
受宪法原则 III 那个硬值约束），写者唯一 = `accept()`；`rework_count` 写者唯一 = `mark_rework()`。
此前这里读那张卡的验收轮次、把结论序号记成它的 +1 再写回去 —— 那是拿审查动作去消费验收轮次的额度，
表现是"审两轮 + 返一次工之后，卡还没验收过就撞上第 3 轮上限进了带未达标项交付"。
现在第几轮审查从 EN-08 已有的结论行数数出来（事实本身就是一个计数载体），只写进 EN-14 那条留痕的
`round_no` 字段。判据两条：`test_review_accept.py` 的"review 路径不碰两列"（行为侧）+
同一文件里那条读源码的静态守卫（防止有人日后把这行加回来）。

**⑥ 本模块还持有 `FindingProvider` 这个来源形状**（owner 2026-10-17 裁 Q6 = 甲）。
放这里而不是放装配点的理由与 ① 的域值口径同一件事：`ReviewFinding` 与 `check_findings` 都在本模块，
"谁负责交出逐条判定"这件事的形状就该与"谁负责判它合不合规"在同一处；放进 `run.py` 就会有两份
"一条判定长什么样"的载体，而装配点改了其中一份时，判据看不出来。
**仿真那一支（`run.StubFindingProvider`）固定返回 pass，因此它不构成 FR-029 的实证** ——
这句话同时登记在 `evidence/phase-12.md` 与 `quickstart.md §6`，不许被"审查跑通了"这种读法替代。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from acp.persistence import events
from acp.persistence.enums import values_of
from acp.persistence.models_approval import ReviewVerdict
from acp.persistence.models_core import TaskCard

NODE = "review"

#: 结论域值：从 PG 枚举取，不在这里重述（`data-model.md §2` 的域值引用规矩）。
VERDICTS: tuple[str, ...] = tuple(values_of("review_verdict"))
APPROVE = "APPROVE"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
#: 平台判不了 ⇒ 交人。它的**唯一来源**是某条判定为 `None`（见 `check_findings`），
#: 不是调用方可以自述的第三个结论词。
NEED_HUMAN = "NEED_HUMAN"

#: 收结论的唯一入口态（`data-model.md §2`：`REVIEWING ──APPROVE──▶ ACCEPTING`）。
REVIEWING = "REVIEWING"

#: 人工评审入口打开那一笔的 EN-14 动作名。写侧分两处（`realrun` 的门禁停点、HTTP 端点对旧现场的补开），
#: 所以名字归本模块 —— 两处各写字面量就会在改名时让另一处静默读不到。
ACTION_OPENED = "review_opened"

#: 一条结论入库那一笔的 EN-14 动作名。Phase 15 起它有了**读侧**（`GET /tasks/{id}` 从留痕里
#: 还原审查轮次），所以名字也必须归本模块：写侧一个字面量、读侧另抄一个字面量，
#: 改名时读侧不会报错，只会静默读出一份空历史 —— 那正是"看起来没审过"这种最难归因的形状。
ACTION_VERDICT = "verdict_recorded"

#: 一行 `item_findings` 的最低要求。名字与 FR-029 的说法对齐：条目 + 判定 + 证据落点。
FINDING_KEYS = ("criterion", "met", "evidence")


class ReviewMisuse(ValueError):
    """审查结论不合法。`code` 是给端点与审计行的稳定键名。"""

    code = "review_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ReviewFinding:
    """一条标准的一行判定。`evidence` 是"在哪份产物的哪个位置"，MUST NOT 是产物正文里的凭据。

    `met=None` = **判不了**（标准本身主观/不可验证，或证据不足以判）。它不是 false：
    false 是"看了、不达标"，null 是"看了、不敢下判定"，两者在 `check_findings` 里
    分别通向 `CHANGES_REQUESTED` 与 `NEED_HUMAN`。
    """

    criterion: str
    met: bool | None
    evidence: str

    def to_payload(self) -> dict[str, object]:
        return {"criterion": self.criterion, "met": self.met, "evidence": self.evidence}


@dataclass(frozen=True)
class ReviewRequest:
    """下发给审查者的可见面（T088）。

    **只有正文与标准**：`artifacts` 是 `{rel_path -> 正文}` 里的正文部分拼成的段落，
    本类没有 `writable_path` / `workspace_dir` 这类字段 —— 让"能写哪儿"这件事在类型上不可表示，
    比在实现里记得不打它更可靠。
    """

    task_id: str
    acceptance_criteria: tuple[str, ...]
    artifact_bodies: tuple[tuple[str, str], ...]

    def render(self) -> str:
        lines = [f"任务卡 {self.task_id} 的待审产物正文：", ""]
        for label, body in self.artifact_bodies:
            lines += [f"--- {label} ---", body, ""]
        lines += ["按下列标准逐条判定，一条一行："] + [f"{n}. {criterion}" for n, criterion in enumerate(self.acceptance_criteria, 1)]
        return "\n".join(lines)


@dataclass(frozen=True)
class VerdictRecord:
    verdict_id: uuid.UUID
    card_id: str
    round_no: int
    verdict: str
    findings: int


class FindingProvider(Protocol):
    """审查结论的**来源形状**（owner 2026-10-17 裁 Q6 = 甲）。

    为什么要有这一个协议而不是"直接把 findings 当参数传"：`TaskResult` 里没有逐条判定的载体
    （契约 §2 那七个字段，`protocol/messages.py:101-107`），所以结论必须由**另一侧**交进来。传一个裸列表
    就没有"谁交的"这一格，而这一格正是要显式的：调用方必须点明它用的是哪一个来源（今天只有
    `run.StubFindingProvider` 那一个仿真实现），装配面缺它就该响亮拒绝，而不是拿空列表往下走 ——
    `check_findings()` 对空判定的答案是"这些标准没有对应行"，那就是一次拒绝，不是"通过"。

    入参给的是**卡 + 卡上声明的标准**而不是 `ReviewRequest`：可见面里已经包含标准，再传一次就是两个来源；
    而"审查者看到了哪些正文"不该改变它给出的判定形状（FR-029 只认卡上声明的标准）。
    """

    def findings_for(
        self, card: TaskCard, criteria: Sequence[str]
    ) -> Sequence["ReviewFinding"]: ...


def _card(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise ReviewMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在", code="card_not_found")
    return card


def build_request(
    card: TaskCard,
    bodies: Sequence[tuple[str, str]],
    *,
    reviewer_agent_id: str,
) -> ReviewRequest:
    """把"这张卡 + 这几份正文 + 这几条标准"折成审查者的可见面。

    `bodies` 由调用方从总线读好递进来（`gateway.BusGateway.read` 是唯一读口），本函数不再去碰磁盘：
    一旦这里出现第二处读路径，"Agent 可见面恰为正文"这句话就要在两处各守一遍。
    """
    if reviewer_agent_id != card.reviewer_agent_id:
        raise ReviewMisuse(
            f"请求方声称是 {reviewer_agent_id!r}，而这张卡的审查岗是 {card.reviewer_agent_id!r}"
            " ⇒ 不是同人就不给可见面（FR-031 在读取面上同样生效，读了不属于自己的卡是第二种自审入口）",
            code="reviewer_mismatch",
        )
    criteria = tuple(str(item) for item in (card.acceptance_criteria or ()))
    if not criteria:
        raise ReviewMisuse(
            f"卡 {card.id} 的 `acceptance_criteria` 为空 ⇒ 没有可逐条比对的标准，审查无从进行",
            code="criteria_empty",
        )
    return ReviewRequest(
        task_id=card.id,
        acceptance_criteria=criteria,
        artifact_bodies=tuple((str(label), str(body)) for label, body in bodies),
    )


def check_findings(card: TaskCard, findings: Sequence[ReviewFinding]) -> tuple[str | None, tuple[str, ...]]:
    """返回 `(结论, 拒因)`。没判全 / 多判了 ⇒ 结论为 None 并给出逐条理由。

    结论由 findings 推出而不是由调用方自述：任一条 `met=None` ⇒ `NEED_HUMAN`；否则全真才是
    `APPROVE`，有一条假就是 `CHANGES_REQUESTED` —— 所以"审者自称通过但有一行判不过"这种形状
    在这里就出不去，而"有一行判不了"也不会被冒充成通过或打回。

    **逐条覆盖先于结论种类**：缺一条、多一条、重复一条都先返回 None（不合规），即使某一行的
    `met` 是 None。`NEED_HUMAN` 不是绕过 FR-029 的后门 —— 它只换结论词，不换比对口径。
    """
    declared = [str(item) for item in (card.acceptance_criteria or ())]
    seen = [str(finding.criterion) for finding in findings]
    missing = [criterion for criterion in declared if criterion not in seen]
    extra = [criterion for criterion in seen if criterion not in declared]
    reasons: list[str] = []
    if missing:
        reasons.append(f"这些卡上声明的标准没有对应行：{missing} ⇒ 没判过就不算判完（FR-029）")
    if extra:
        reasons.append(f"这些标准不在卡上：{extra} ⇒ 引入额外标准或个人偏好，MUST NOT 入库（FR-029）")
    if len(seen) != len(set(seen)):
        reasons.append("同一条标准被记了不止一行 ⇒ 一份结论里一个条目只能有一个判定")
    if reasons:
        return None, tuple(reasons)
    if any(finding.met is None for finding in findings):
        return NEED_HUMAN, ()
    return (APPROVE if all(finding.met for finding in findings) else CHANGES_REQUESTED), ()


def record_verdict(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    reviewer_agent_id: str,
    findings: Sequence[ReviewFinding],
    created_at: dt.datetime,
    actor_agent_id: str | None = None,
) -> VerdictRecord:
    """入库一条结论（不 commit，提交点归装配方）。

    动的东西只有三样：EN-08 一行、卡的 `status`（`NEED_HUMAN` 时不写，见下）、EN-14 一条留痕
    （留痕里带 `round_no`）。**两个计数列都不碰** —— 依据见模块头第 ⑤ 段（owner 2026-10-14 裁决 2）。
    """
    card = _card(session, feature_request_id, task_id)
    if reviewer_agent_id != card.reviewer_agent_id:
        raise ReviewMisuse(
            f"结论来自 {reviewer_agent_id!r}，而这张卡的审查岗是 {card.reviewer_agent_id!r}",
            code="reviewer_mismatch",
        )
    if str(card.status) != REVIEWING:
        # 状态机上只有 `REVIEWING` 有到审查的两条入边（`RUNNING ──▶ REVIEWING`、`REWORK ──▶ … ──▶ REVIEWING`）。
        # 不拦这一步，一张还在 `RUNNING` 的卡能被一份结论直接拖到 `ACCEPTING`，而 `accept()` 那一边
        # 是有这道判定的 —— 半边有、半边没有，就等于"审查可以插队"。
        raise ReviewMisuse(
            f"卡 {card.id} 现在是 {card.status}，审查结论只在 {REVIEWING} 上收（先把卡推进到待审）",
            code="state_not_reviewing",
        )
    verdict, reasons = check_findings(card, findings)
    if verdict is None:
        raise ReviewMisuse(
            f"卡 {card.id} 的审查结论不入库：{'；'.join(reasons)}", code="findings_not_compliant"
        )
    # 第几轮审查 = EN-08 里这张卡已有的结论行数 + 1，**从事实里数出来，不占 EN-02 的列**。
    # 必须在 `session.add(row)` 之前算：`Session` 默认 autoflush，先加后数会把这一条新行也数进去（读成 2）。
    #
    # **必须同时按 `feature_request_id` 圈定**：EN-02 的主键是复合的 `(feature_request_id, id)`，
    # 卡号跨单重名是常态（每张单都从 `T01` 起号）。只按 `task_card_id` 数，数的是"全库所有单的
    # T01 各判过几次" —— 2026-10-28 真跑实测：本单 T01 第一次评审，读出来的 `round_no` 是 **6**
    # （库里另外五张单的 T01 各贡献 1 行）。这不只是读数难看：`round_no` 会喂给 B 类卡点的
    # 轮次上限判定（`approval/timeouts.py::decide`），虚高等于让一张刚出生的卡一上来就被判超限。
    round_no = 1 + int(
        session.scalar(
            select(func.count())
            .select_from(ReviewVerdict)
            .where(
                ReviewVerdict.feature_request_id == feature_request_id,
                ReviewVerdict.task_card_id == card.id,
            )
        )
        or 0
    )
    row = ReviewVerdict(
        id=uuid.uuid4(),
        feature_request_id=feature_request_id,
        task_card_id=card.id,
        reviewer_agent_id=reviewer_agent_id,
        verdict=verdict,
        item_findings=[finding.to_payload() for finding in findings],
        extra_criteria_used=False,
    )
    session.add(row)

    # 状态机的两条出边（`data-model.md §2`）：`REVIEWING ──APPROVE──▶ ACCEPTING`、
    # `CHANGES_REQUESTED ──▶ REWORK`。这里落在 `CHANGES_REQUESTED` 上，返工那一格归 T092 ——
    # 把"审查意见"与"返工动作"合成一步就会丢掉"谁决定返工"这个可审计的中间态。
    # `data-model.md` 那条"进入 `ACCEPTING` 前三岗互斥 MUST 已通过"由 `AssignGuards`（T085/T086）守，
    # 本模块不复写第二份断言（原则 I）。
    #
    # `NEED_HUMAN` **不移动卡态**：卡留在 `REVIEWING`，人工随后那条决定性结论才把它推出去。
    # 这里不写 `card.status` 而不是写成同一值，是为了让"升级不替人做选择"在代码面上就是一句空操作，
    # 而不是一次看起来像状态迁移的赋值（谁把 REVIEWING 改错都会在这一格显形）。
    if verdict != NEED_HUMAN:
        card.status = "ACCEPTING" if verdict == APPROVE else CHANGES_REQUESTED
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="REVIEW",
        actor_agent_id=actor_agent_id or reviewer_agent_id,
        payload={
            "node": NODE,
            "action": ACTION_VERDICT,
            "round_no": round_no,
            "verdict": verdict,
            "findings": len(findings),
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return VerdictRecord(
        verdict_id=row.id, card_id=card.id, round_no=round_no, verdict=verdict, findings=len(findings)
    )
