"""T052 / T058 / T059：EN-09 审批单的写侧（或签 CAS + 到达流水 + 未决单排序）。

**为什么 CAS 在代码里而库里只留 `version` 列**：`data-model.md §4` 把"后到者不二次迁移"指派给
`approval/repository.py`，EN-09 的注释（`models_approval.py:130`）同一条口径 —— DB 只提供乐观锁列，
不写触发器。所以决定性结论的写入 MUST 是**一条带 `version` 与 `state='PENDING'` 双条件的 UPDATE**：
读-改-写分两步等于把 or-sign 的"先到者生效"变成"谁都以为自己先到"。

**`state='PENDING'` 这一半条件今天量不到**（原话写的是"不是冗余：两人同时读到的 version 可能相同"，
本轮变异 MU-A 证伪了那句推理）：并发时赢家那条 UPDATE 把 `state` 与 `version` **在同一条语句里一起改**，
PG 的行锁让败者的 UPDATE 重算谓词时 `version` 已经不等 ⇒ 只比 `version` 也拦得住。
所以它现在是**防御性编程**（owner 2026-10-13 裁决 ⑥ 定"保留，不补判据"）：
它成为唯一防线的那一刻，是将来有人新增一条"改态但不推 `version`"的写路径。
已登记待办；本模块不假装它被哪条用例证过 —— 断"落定不许迁移"的那几条量的都是前置检查那一道门。

**`need_info` 不动 `state`、不动 `version`、不动 `payload`**（FR-040 三 + T052 的"需补充信息不改待决态"）。
三条理由，每条都有用例钉着：
1. EN-09 的部分唯一索引条件是 `WHERE state = 'PENDING'`（`models_approval.py:103-109`，FR-045）。
   把追问写成 `state='NEED_INFO'` 会让这一行**掉出索引**，同一张卡的第二个未决卡点就能插进来 ——
   "追问"反而打开了 FR-045 的口子，那是拿语义换一个漏。
2. `version` 是**决定性结论**的锁。追问一 bump 版本，先到的追问就能把后到的批准打成
   `stale_version`，等于让非决定性结论否决决定性结论，与 FR-040 的"以先到达的决定性结论为准"相反。
3. 追问这件事的唯一载体是 EN-10 的 `resolution_event(verdict='need_info', applied=true)` 行，
   `reason_text` 存追问内容；单据的待批上下文（payload 五要素）一字不改 ⇒ "保留原请求上下文"
   由"我们根本不写 payload"保证，而不是由"写了但写回原值"保证。

**排序键 `C/D > B > A`（FR-044）写在代码里而不是 SQL 的 CASE 里**：`contracts/approval-api.md §6`
那行 `priority = C/D(0) > B(1) > A(2)` 与本模块的 `priority_key()` 是同一规则的两侧，两处各写一份就会
漂移；取 Python 侧为唯一载体，读全量未决单后按它排。未决单的量被 FR-045 压成"每任务至多一张"，
MVP-1 的规模下这个取舍成立；库里若出现成千上万张未决单，本函数就是第一个要改的地方（登记在
`evidence/phase-04.md §T058` 的未闭段，不假装它永远不会成为问题）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from acp.approval.timeouts import CONVERGE, KIND_BY_LETTER, TimeoutPolicy, decide
from acp.persistence.enums import values_of
from acp.persistence.models_approval import ApprovalTicket, ResolutionEvent

#: 域取值是权威，本模块的**名字**只是它的别名：改名 / 删值会在导入期报错，
#: 而不是让某个字符串悄悄变成"库里没有的态"（位置绑定 `values_of(...)[0]` 的写法第一版就这么错着）。
class ApprovalMisuse(ValueError):
    """调用形态本身就不对（结论不在域里、会话没给、域与代码的别名脱钩）。业务拒绝不走这里，走 `Outcome`。"""


def _named(domain: str, *names: str) -> dict[str, str]:
    values = set(values_of(domain))
    for name in names:
        if name not in values:
            raise ApprovalMisuse(f"{domain} 域里没有 {name!r} ⇒ 代码里的别名与文档脱钩了")
    return {name: name for name in names}


PENDING = _named("approval_state", "PENDING")["PENDING"]
APPROVED = _named("approval_state", "APPROVED")["APPROVED"]
REJECTED = _named("approval_state", "REJECTED")["REJECTED"]
TIMED_OUT = _named("approval_state", "TIMED_OUT_CONVERGED")["TIMED_OUT_CONVERGED"]

#: EN-10 的到达渠道。`DASHBOARD / CLI` 是 FR-042 的双通道；`AUTO` 是 owner 2026-10-13 裁决 ④ 加的
#: 第三个值，**只有 `apply_auto_pass()` 能写它** —— 见下面 `HUMAN_CHANNELS` 那条注释。
DASHBOARD = _named("resolution_channel", "DASHBOARD")["DASHBOARD"]
CLI = _named("resolution_channel", "CLI")["CLI"]
AUTO = _named("resolution_channel", "AUTO")["AUTO"]

#: 人可以提交的通道。**这份名单存在的理由**：`AUTO` 是"没有人在敲命令"这件事的载体，
#: 若 `resolve()` 收它，任何拿到 `ticket_id` 与身份的调用者都能把自己的结论标成"规则表自动批的"，
#: 审计面就从"谁批的"退化成"自称谁批的"。所以通道白名单与域不是同一件事，两处 MUST NOT 合一。
HUMAN_CHANNELS = frozenset({DASHBOARD, CLI})

VERDICTS = frozenset(values_of("resolution_verdict"))
#: "决定性"是结论的属性而不是域里的字段，所以它在代码里成文；与域的对应关系由下一行核对。
DECISIVE = frozenset({"approve", "reject"})
if not DECISIVE <= VERDICTS:
    raise ApprovalMisuse(f"DECISIVE {sorted(DECISIVE)} 不在 resolution_verdict 域里")

#: 结构约束（FR-044）：优先级只由卡点类型决定，不是引用型参数，MUST NOT 下沉 `params-ref`。
#: E（审查不确定）与 C/D 同档：它同样阻塞一条正在走的审查链，且没有超时能替人收口。
#: **值集保持 {0, 1, 2} 不变** —— 前端审批牌的档位数与它同数（`test_board_frontend.py` 节 七），
#: 新加一档就会让 E 落进中性色、看着像"没分级"。
_PRIORITY: dict[str, int] = {
    KIND_BY_LETTER["C"]: 0,
    KIND_BY_LETTER["D"]: 0,
    KIND_BY_LETTER["E"]: 0,
    KIND_BY_LETTER["B"]: 1,
    KIND_BY_LETTER["A"]: 2,
}

#: `code` 的取值 = `contracts/approval-api.md §7` 错误码表里的键名（去掉 HTTP 状态）。
#: 唯一载体在本模块，HTTP 层只把它映进响应体，不在别处再抄一份字符串。
ALREADY_DECIDED = "already_decided"
STALE_VERSION = "stale_version"
PRINCIPAL_NOT_ALLOWED = "principal_not_allowed"
#: 下面两条不在 §7 那张表里（表只列了四条"触发方=建单/resolve"的码），取什么 HTTP 状态是本轮决定，
#: 已登记在 `evidence/phase-04.md §T055` 的未闭段等 owner 裁。
TICKET_NOT_FOUND = "ticket_not_found"
NOT_PENDING = "not_pending"
#: 裁决 ④ 的连带项：`AUTO` 在域里但不在人的可提交集合里。它不是"业务上被拒"而是"调用形态不对"，
#: 但它也不该走 `ApprovalMisuse`（那是编程错误，不该由 HTTP 请求触发），所以给一条自己的码。
CHANNEL_NOT_ALLOWED = "channel_not_allowed"

#: `code` → HTTP 状态码（`contracts/approval-api.md §7` + 上面那三条的临时归属）。
HTTP_STATUS: dict[str, int] = {
    ALREADY_DECIDED: 409,
    STALE_VERSION: 409,
    PRINCIPAL_NOT_ALLOWED: 403,
    TICKET_NOT_FOUND: 404,
    NOT_PENDING: 409,
    CHANNEL_NOT_ALLOWED: 422,
}


@dataclass(frozen=True)
class Creation:
    """建单结果。`merged` 与 `kind_conflict` 是两个布尔，因为 FR-045 的两句话管两件不同的事。"""

    ticket_id: uuid.UUID
    merged: bool
    #: 同任务已有未决卡点、但**类型不同** ⇒ 不新建第二行（FR-045 后半句），冲突交调用方处置。
    #: 本轮生产路径取不到 `True`（A 类挂需求单、D 类挂卡，两个作用域不重叠），所以它有用例钉住
    #: 形态而没有生产调用方 —— 登记为未闭，不写成"已生效"。
    kind_conflict: bool
    #: 合并回来的那一行的实际类型（冲突时与来单不同，调用方据此判断自己的卡点有没有挂上）。
    kind: str

    @property
    def created(self) -> bool:
        return not self.merged


@dataclass(frozen=True)
class Outcome:
    """一次 `resolve` / 超时收敛的结果，形状照 `contracts/approval-api.md §3` 的响应体。"""

    ticket_id: uuid.UUID
    applied: bool
    state: str
    version: int
    code: str | None = None
    logged_as: str | None = None

    @property
    def status_code(self) -> int:
        """`code is None` ⇒ 这次操作按契约成功（200 一族，具体状态归 HTTP 层）。有码必须能查表，
        查不到就报错：新增一个 `code` 而忘了给它 HTTP 归属，表现必须是响不了而不是静默 200。
        """
        if self.code is None:
            raise ApprovalMisuse("成功的 Outcome 没有 HTTP 错误码可取")
        return HTTP_STATUS[self.code]


def priority_key(kind: str) -> int:
    try:
        return _PRIORITY[kind]
    except KeyError as exc:
        raise ApprovalMisuse(f"卡点类型 {kind!r} 不在优先级表里 ⇒ FR-044 的排序键缺一条") from exc


def _now(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else dt.datetime.now(dt.timezone.utc)


def _event(
    session: Session,
    *,
    ticket_id: uuid.UUID,
    arrived_at: dt.datetime,
    channel: str,
    verdict: str,
    applied: bool,
    reason_text: str,
) -> None:
    session.add(
        ResolutionEvent(
            id=uuid.uuid4(),
            approval_ticket_id=ticket_id,
            arrived_at=arrived_at,
            channel=channel,
            verdict=verdict,
            applied=applied,
            reason_text=reason_text,
        )
    )


def pending_for(
    session: Session,
    *,
    task_card_id: str | None = None,
    feature_request_id: uuid.UUID | None = None,
) -> ApprovalTicket | None:
    """FR-045 后半句的读侧：**按作用域查**，不是"按其中一列查"。

    这里 MUST NOT "取第一行"了事：库里本该至多一行，读出不止一行说明那条部分唯一索引没生效，
    那种情况下返回任意一行都会把重复掩盖掉 ⇒ 直接报错，让索引失效看得见。

    **两处作用域限定是修出来的，不是设计时想全的**（本轮新写的"一张需求单上同时挂 A 类与卡级单"
    那条用例把第一处撞了出来）：
    1. 查**需求单级**（A 类，`task_card_id is None`）时必须加 `task_card_id IS NULL`。
       只按 `feature_request_id` 筛会把同一需求单下**挂在某张卡上**的未决单一起捞回来 ——
       后果不止是"多捞几行"：轻则 `create_or_merge` 把 A 类单 merge 到一张 D 类卡级单上
       （卡点挂错对象、`kind_conflict` 假报），重则 `pending_for` 因为捞到多行而喊"索引没生效"，
       而索引其实好得很。FR-045 那句"同一任务只允许一个未决卡点"里的"任务"是**那张卡或那张需求单**，
       不是"那批卡所在的同一个需求"。
    2. 查**卡级**时必须 `(feature_request_id, task_card_id)` **两列一起比**。
       卡号的作用域就在 EN-02 那条复合主键上（`data-model.md §3` 裁决 2）：`T01` 在两个需求单里
       可以各存在一张，只按 `task_card_id` 筛会把**别人家的 T01** 的未决单捞过来 merge。
    """
    if task_card_id is None and feature_request_id is None:
        raise ApprovalMisuse("查未决单必须给 task_card_id 或 feature_request_id 之一")
    stmt = select(ApprovalTicket).where(ApprovalTicket.state == PENDING)
    if task_card_id is not None:
        if feature_request_id is None:
            # 不接受"只给卡号"：那在复合主键的作用域下是两个坐标缺了一个，查出来的东西不可解释。
            raise ApprovalMisuse(
                f"卡 {task_card_id} 的未决单查询没带 feature_request_id ⇒ "
                "卡号只在需求单内唯一，少了这一列就会撞进别的需求单的同名卡"
            )
        stmt = stmt.where(
            ApprovalTicket.task_card_id == task_card_id,
            ApprovalTicket.feature_request_id == feature_request_id,
        )
    else:
        stmt = stmt.where(
            ApprovalTicket.feature_request_id == feature_request_id,
            ApprovalTicket.task_card_id.is_(None),
        )
    rows = list(session.scalars(stmt))
    if len(rows) > 1:
        raise ApprovalMisuse(
            f"同一作用域查出 {len(rows)} 张未决卡点 ⇒ EN-09 的部分唯一索引（FR-045）没生效"
        )
    return rows[0] if rows else None


def create_or_merge(
    session: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    risk_exempt_eligible: bool,
    timeout_policy: TimeoutPolicy,
    task_card_id: str | None = None,
    feature_request_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
    ticket_id: uuid.UUID | None = None,
) -> Creation:
    """建一张未决单，或合并到已有未决单（FR-045）。

    `timeout_policy` 由 `timeouts.py` 给出、`risk_exempt_eligible` 由 EN-11 规则表给出 ——
    两者都不是调用方能自由填的字段：把超时策略做成入参就等于允许"这一单请 5 分钟后自动放行"，
    而 FR-043 的结构约束禁的正是这件事。

    `ticket_id` 是**重放**用的：LangGraph 的 `interrupt` 在恢复时会把节点函数从头再执行一遍
    （实测读数见 `evidence/phase-04.md §T048`），所以卡点节点第二次进来时必须拿回自己那张单，
    而不是再插一行 —— 顺序也因此是"先按单据号认，再按任务作用域认"，反过来的话重放会先建出第二行。
    """
    moment = _now(now)
    if ticket_id is not None:
        row = session.get(ApprovalTicket, ticket_id)
        if row is not None:
            return Creation(
                ticket_id=row.id,
                merged=True,
                kind_conflict=str(row.kind) != str(kind),
                kind=str(row.kind),
            )
    existing = pending_for(
        session, task_card_id=task_card_id, feature_request_id=feature_request_id
    )
    if existing is not None:
        return Creation(
            ticket_id=existing.id,
            merged=True,
            kind_conflict=str(existing.kind) != str(kind),
            kind=str(existing.kind),
        )
    expires = timeout_policy.expires_at(moment)
    row = ApprovalTicket(
        id=ticket_id or uuid.uuid4(),
        task_card_id=task_card_id,
        feature_request_id=feature_request_id,
        kind=kind,
        state=PENDING,
        payload=payload,
        risk_exempt_eligible=risk_exempt_eligible,
        version=0,
        decided_by_principal=None,
        decided_at=None,
        # P1-4：这一行建出来的时刻。显式传 `moment` 而不是靠 DB 默认 —— 建单与超时策略
        # 用的是同一个"现在"，两处取两个时钟会让"刚好挂着还没到期"的单在读数上自相矛盾。
        created_at=moment,
        # 裁决 ②（owner 2026-10-13）：§1 写"无超时"的三类（A / C / D）落 SQL `NULL`。
        # 不是"一个很大的时刻"而是"没有可算的时刻" —— 超时扫描 `timeout_at <= now()` 在 NULL 上
        # 恒不命中（三值逻辑），所以不需要魔法数也不会放行这三类。迁移 0006 顺带把旧哨兵行改回 NULL。
        timeout_at=expires,
    )
    session.add(row)
    session.flush()
    return Creation(ticket_id=row.id, merged=False, kind_conflict=False, kind=str(row.kind))


def list_tickets(session: Session, state: str | None = None) -> list[ApprovalTicket]:
    """未决单排队（FR-044）：`C/D/E > B > A`；同档先"无超时"、再按 `timeout_at`、再按 `id` 定序（不留并列）。

    排序键只有一份实现（`priority_key()`）。`list_pending()` 是它的一个筛选而不是第二条规则 ——
    两处各排一次就会漂成"看板上未决单的顺序和列表端点不一样"。

    **"无超时"排在同档最前**是裁决 ② 之后新出现的一档（FR-044 没写这种情况，所以这是一条决定而不是引用）：
    永不到期的单只有人能推，而会到期的那一张系统早晚自己收口，把前者排在后面等于让它一直压着。
    要反过来就只改这一行 —— 别在调用方各排一次。
    键里的 `is not None` 旗标保证 `None` 与时刻**永不互比**（那样会 TypeError），
    同旗标内部才轮到真值比较，两边都是 `None` 时 Python 走相等分支直接落到 `id`。
    """
    stmt = select(ApprovalTicket)
    if state is not None:
        stmt = stmt.where(ApprovalTicket.state == state)
    rows = list(session.scalars(stmt))
    return sorted(
        rows,
        key=lambda row: (
            priority_key(str(row.kind)),
            row.timeout_at is not None,
            row.timeout_at,
            str(row.id),
        ),
    )


def list_pending(session: Session) -> list[ApprovalTicket]:
    return list_tickets(session, PENDING)


def events(session: Session, ticket_id: uuid.UUID) -> list[ResolutionEvent]:
    return list(
        session.scalars(
            select(ResolutionEvent)
            .where(ResolutionEvent.approval_ticket_id == ticket_id)
            .order_by(ResolutionEvent.arrived_at, ResolutionEvent.id)
        )
    )


def resolve(
    session: Session,
    *,
    ticket_id: uuid.UUID,
    verdict: str,
    version: int,
    channel: str,
    principal: str,
    approvers: frozenset[str],
    reason_or_ask: str = "",
    now: dt.datetime | None = None,
) -> Outcome:
    """或签：先到达的**决定性结论**以 CAS 生效，后到的记 duplicate 且不二次迁移（FR-040）。"""
    if verdict not in VERDICTS:
        raise ApprovalMisuse(f"结论 {verdict!r} 不在 resolution_verdict 域里：{sorted(VERDICTS)}")
    if channel not in HUMAN_CHANNELS:
        # `AUTO` 在域里，但它是"没有人在敲命令"的载体（裁决 ④）。让人提交它就等于
        # 把审计面从"谁批的"退化成"自称谁批的" —— 白名单与域是两件事，这里不收。
        return Outcome(
            ticket_id=ticket_id, applied=False, state="", version=version,
            code=CHANNEL_NOT_ALLOWED,
        )
    moment = _now(now)
    row = session.get(ApprovalTicket, ticket_id)
    if row is None:
        return Outcome(
            ticket_id=ticket_id, applied=False, state="", version=version, code=TICKET_NOT_FOUND
        )
    if not approvers or principal not in approvers:
        # 失败关闭：审批者集合没配 ⇒ 谁都不得过。反过来（空集合当"不限制"）是最典型的假开放。
        return Outcome(
            ticket_id=row.id, applied=False, state=str(row.state), version=row.version,
            code=PRINCIPAL_NOT_ALLOWED,
        )

    if verdict not in DECISIVE:
        # 追问：只写流水；state / version / payload 三条都不动（理由见模块头）。
        _event(
            session, ticket_id=row.id, arrived_at=moment, channel=channel, verdict=verdict,
            applied=True, reason_text=reason_or_ask,
        )
        session.flush()
        return Outcome(ticket_id=row.id, applied=True, state=str(row.state), version=row.version)

    if str(row.state) != PENDING:
        _event(
            session, ticket_id=row.id, arrived_at=moment, channel=channel, verdict=verdict,
            applied=False, reason_text=reason_or_ask,
        )
        session.flush()
        return Outcome(
            ticket_id=row.id, applied=False, state=str(row.state), version=row.version,
            code=ALREADY_DECIDED, logged_as="duplicate",
        )

    new_state = APPROVED if verdict == "approve" else REJECTED
    result = session.execute(
        update(ApprovalTicket)
        .where(
            ApprovalTicket.id == row.id,
            ApprovalTicket.version == version,
            ApprovalTicket.state == PENDING,
        )
        .values(
            state=new_state, version=version + 1, decided_by_principal=principal, decided_at=moment
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # CAS 没命中而进来时是待决 ⇒ 有人在我之后、我之前写过一次（超时收敛就是这种时刻）。
        _event(
            session, ticket_id=row.id, arrived_at=moment, channel=channel, verdict=verdict,
            applied=False, reason_text=reason_or_ask,
        )
        session.flush()
        session.expire(row)
        fresh = session.get(ApprovalTicket, row.id)
        code = ALREADY_DECIDED if str(fresh.state) != PENDING else STALE_VERSION
        return Outcome(
            ticket_id=row.id, applied=False, state=str(fresh.state), version=fresh.version,
            code=code, logged_as="duplicate" if code == ALREADY_DECIDED else None,
        )

    _event(
        session, ticket_id=row.id, arrived_at=moment, channel=channel, verdict=verdict,
        applied=True, reason_text=reason_or_ask,
    )
    session.flush()
    session.expire(row)
    return Outcome(ticket_id=row.id, applied=True, state=new_state, version=version + 1)


def apply_auto_pass(
    session: Session,
    *,
    ticket_id: uuid.UUID,
    decided_by: str | None,
    now: dt.datetime | None = None,
) -> Outcome:
    """规则表免确认的落库（FR-046 + FR-048）：单据直接进 `APPROVED`，`decided_by_principal` 记规则名。

    **"只有 A 类能免"在这里再拦一次**不是重复代码：`risk_engine.exemption_applies()` 是判据，
    本函数是唯一能把单据写成"无人批过却已批准"的写者。两处指向同一条结构约束、各拦一侧
    （判据侧拦"算错了"，写者侧拦"绕过服务层直调仓储"），先例是 `registry/persist.py` 的申报比对
    与库里那条 CHECK 成对出现。少任何一侧，EN-11 注释里那句"组合不出第二条免确认路径"就不成立。

    **写一行 EN-10 到达流水（owner 2026-10-13 裁决 ④）**：通道取 `AUTO`，`verdict='approve'`，
    `applied=true`，`reason_text` 存规则名。此前这一支不写流水，理由是域里只有 `DASHBOARD / CLI`，
    硬塞就是造假；owner 裁"免确认也是一种 resolution，审计要区分人工批准与自动免确认"，于是加值而不是造假。
    `reason_text` 带规则名不是与 `decided_by_principal` 重复：EN-10 **没有 principal 列**，
    不写进去这份流水就只知道"是自动的"而不知道"是哪条规则自动的"。

    `AUTO` 只能由本函数写 —— `resolve()` 的通道白名单不收它（见 `HUMAN_CHANNELS`）。
    """
    if not decided_by:
        raise ApprovalMisuse("免确认必须由一条规则署名，decided_by 为空就是没有来源的批准")
    row = session.get(ApprovalTicket, ticket_id)
    if row is None:
        return Outcome(
            ticket_id=ticket_id, applied=False, state="", version=0, code=TICKET_NOT_FOUND
        )
    if str(row.kind) != KIND_BY_LETTER["A"]:
        raise ApprovalMisuse(
            f"卡点 {row.kind} 试图免确认 ⇒ FR-048 只允许 A 类（拆单确认）免除"
        )
    if str(row.state) != PENDING:
        return Outcome(
            ticket_id=row.id, applied=False, state=str(row.state), version=row.version,
            code=NOT_PENDING,
        )
    moment = _now(now)
    result = session.execute(
        update(ApprovalTicket)
        .where(ApprovalTicket.id == row.id, ApprovalTicket.state == PENDING)
        .values(state=APPROVED, version=row.version + 1, decided_by_principal=decided_by, decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire(row)
    if result.rowcount == 1:
        # 只在真的批下来那一支写流水：`rowcount == 0` 说明有人在我之前把它定案了，
        # 那种时刻给 EN-10 塞一行 AUTO 就是伪造一次"自动批准"发生过。
        _event(
            session, ticket_id=row.id, arrived_at=moment, channel=AUTO, verdict="approve",
            applied=True, reason_text=decided_by,
        )
        session.flush()
    return Outcome(
        ticket_id=row.id, applied=result.rowcount == 1, state=APPROVED, version=row.version + 1
    )


def converge_on_timeout(
    session: Session,
    *,
    ticket_id: uuid.UUID,
    review_round: int,
    policy: TimeoutPolicy,
    extensions_used: int = 0,
    now: dt.datetime | None = None,
) -> Outcome | None:
    """超时扫描的唯一出口（FR-043 + FR-035）：**判定在 `timeouts.decide()`，这里只落它的结果**。

    返回 `None` = 这一轮不该动它（未跑满轮次 / A、C、D 三类恒不自动放行），调用方什么都不做。
    本函数不接受"要不要收敛"这个入参 —— 谁能在调用处把 `CONVERGE` 说成 `True`，FR-043 那句
    "MUST NOT 提前收敛"就成了一句话。
    """
    row = session.get(ApprovalTicket, ticket_id)
    if row is None:
        return Outcome(
            ticket_id=ticket_id, applied=False, state="", version=0, code=TICKET_NOT_FOUND
        )
    if policy.kind != str(row.kind):
        # 挡住"拿 B 类的策略去收敛一张 D 类单"：判定读的是**单据**的类型，数值读的是**传进来**的策略，
        # 两者错位就会让一条结构约束被另一类的数值顶掉。
        raise ApprovalMisuse(f"传入的超时策略属于 {policy.kind}，而单据的类型是 {row.kind}")
    action = decide(
        str(row.kind),
        review_round=review_round,
        extensions_used=extensions_used,
        table={policy.kind: policy},
    )
    if action != CONVERGE:
        return None
    if str(row.state) != PENDING:
        return Outcome(
            ticket_id=row.id, applied=False, state=str(row.state), version=row.version,
            code=NOT_PENDING,
        )
    moment = _now(now)
    result = session.execute(
        update(ApprovalTicket)
        .where(ApprovalTicket.id == row.id, ApprovalTicket.state == PENDING)
        .values(state=TIMED_OUT, version=row.version + 1, decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire(row)
    return Outcome(
        ticket_id=row.id, applied=result.rowcount == 1, state=TIMED_OUT, version=row.version + 1
    )
