"""T104：适配器出站动作之前的**唯一拦截点**（FR-056 / FR-057 的档位半边｜R-15，卡点 D 的触发方）。

`plan.md` 设计项 9 给这一层的原话是三句：三份载体各管一件事（清单文件 / EN-16 当前态 / 本模块），
本模块是**唯一拦截点**，且两条判定路径**汇到同一个出口** `ApprovalGate(kind=D)`，
"不存在第二条放行分支"。这一模块就按这三句写，判据逐条对位在 `tests/guards/test_autonomy_guard.py`。

**判定顺序（只有这一个顺序）**

1. `command` 空白 ⇒ 抛 `UndeclaredAction`。这不是"参数校验顺手加的一条"：
   "不申报要执行什么"如果走成放行，就等于给所有拦截开了一个"闭口不谈就过"的后门，
   而 `spec.md:327` 那句"不以清单未列为由放行"管的是同一种推理方向。
2. **在清单内 ⇒ 拦**（任一档位都拦；`FULL` 档免的是"逐笔请示"，不免清单）。
3. **`SAFE` 档 + 非只读 + 不可自动回滚 ⇒ 请示**（FR-056 的第二条，也是 `spec.md:327` 的兜底：
   清单没列的动作在保守档下依然要过人）。
4. 以上都不成立 ⇒ 放行。**这是本模块唯一一处放行出口**（判据：AST 数 `ALLOW` 的构造点数 = 1，
   再配一张 2×2×2×2 的穷举矩阵，让"全拦"和"全放"两种退化都当场红）。

**"同一个出口"是怎么做到的（不是靠注释说的）**

- 两条拦截路径产出的 `Decision` 里，`kind` 都是 `KIND_D`，且都只能由 `hold_ticket()` 这一条路建单；
- `hold_ticket()` 走的是 **Phase 4 已经在用的那一个建单入口** `approval/service.create_approval()`
  —— 同一个五要素校验、同一套合并逻辑、同一张 EN-09 表，本模块不开第二条建单路径；
- **挂起这件事不在这里**：`interrupt()` 的唯一载体是 `orchestrate/nodes/approval_gate.py`（宪法 V，
  MUST NOT 改成轮询或重启恢复）。本模块 import 它、也复制它的等待 —— 只保证交出去的那张单
  与那个节点建出来的是同一类东西。因此"从端点/节点到适配器 `submit()` 之前调用本模块"这一格
  **目前是空的**（仓内没有适配器调用点：`grep -rn "\\.submit(" src/acp` 只有三处 `def` 与协议声明），
  读数与关闭判据见 `evidence/phase-08.md`。

**为什么这张单不可能被免确认放掉**（三条锁，一条比一条靠外，判据各一条）：
① `hold_ticket()` 不传 `facts` ⇒ `create_approval` 里的判定是 `request_facts_not_supplied`、`exempt=False`；
② 即便有人改成传 `facts`，`risk_engine.exemption_applies()` 只免除 A 类（FR-048）；
③ 即便超时也不放行 —— `timeouts.AUTO_PASS_ON_TIMEOUT_FORBIDDEN` 收 A/C/D，§1 那行给 D 的是"无超时 / 挂起等待"。
⇒ "第二条放行分支"在这三层上都被堵住，而不是只在本模块里被堵住。

**单据去重、留痕逐次**（FR-045 的合并在这里的后果，写清楚免得被读成缺陷）：
同一张卡上第二次拦下一个新动作，会**合并**进那张还没批的 D 类单（`create_or_merge` 的
`pending_for` 认出它），第二次的命令不会追加进 payload；逐次的事实由 EN-14
`event_log(kind=AUTONOMY_INTERCEPT)` 承担（T105，落点 = 本模块的 `record_interception()`），
单据负责"有人在等"，流水负责"等过几次"。⇒ 调用方（T123 的适配器门面）把这两件事**各调一次**，
漏掉流水就读不出"拦过几次"，漏掉单据就是拦了而没人被叫来 —— 两者都不报错，所以判据在
`tests/guards/test_autonomy_intercept.py` 里钉成"两次拦截 ⇒ 一张单 + 两行流水"。

**洗什么**：`command` 进 payload 前一律过 `redact_bus_text()`（与产物、日志两面同一份洗法，
T027 的 `SecretsRedactor` 门面）。清单里的一条 `pattern`（如 `rm -rf`）不是秘密，原样留；
命令本体可能是 `aws --secret-access-key <值>` 这种形状，而 EN-09 的 payload 是要给人看的。

`read_only` 与 `auto_rollback` 由调用方交出：前者 SHOULD 取 EN-04 的岗位读数（`bus/acl.py` 的
`Actor.read_only`，不是请求方自述，同 T088），后者是动作属性 —— 本模块**不猜**。
猜不出来的形状（空白主体、空命令）一律抛错而不是选一个保守值继续，因为"保守值"在这里就是放行。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER
from acp.config.loader import DangerList, redact_bus_text
from acp.persistence import autonomy as store
from acp.persistence import events
from acp.persistence.enums import values_of
from acp.persistence.models_registry import EventLog

#: 三条判定结果。`ALLOW` 在本模块只有一处被构造出来（判据 `test_the_release_exit_is_singular`）。
ALLOW = "allow"
DANGER_HIT = "danger_list_hit"
SAFE_REVIEW = "safe_tier_review"

#: 两条拦截路径共用的卡点类。**从域派生而不是抄 `"D_DANGER"`**：`KIND_BY_LETTER` 的键是
#: `approval_kind` 域取值的首字母，域里没了 D 就 import 期报错（口径同 `approval/repository.py::_named`）。
KIND_D = KIND_BY_LETTER["D"]

#: 两档的名字同样从 `autonomy_mode` 域核。`store.DEFAULT_MODE` 是"新项目的默认档"（owner 裁决 2），
#: 与这里的"保守档这一取值"是两件事 —— 前者哪天被改成 `FULL`，后者仍然是"要请示人工"的那一档。
FULL = "FULL"
SAFE = "SAFE"
#: 下面这段核的是"代码里的档位别名与文档脱钩"，不是"值等于某个字面串"：域里删掉 SAFE，
#: 本模块 import 期就报错，而不是把"保守档"静默读成"没有这一档 ⇒ 全放行"。
_missing = sorted({FULL, SAFE} - set(values_of("autonomy_mode")))
if _missing:
    raise RuntimeError(f"autonomy_mode 域里没有 {_missing} ⇒ 本模块的档位别名与文档脱钩了")
del _missing

#: `suggested_verdict` 的两条取值：清单命中 = 建议不放行；保守档请示 = 建议补信息后再定。
#: 这是**建议**不是结论（结论的唯一载体是 EN-10 的 resolution），选值理由写在用例名里。
SUGGEST_REJECT = "reject"
SUGGEST_NEED_INFO = "need_info"

_SUGGESTED: dict[str, str] = {DANGER_HIT: SUGGEST_REJECT, SAFE_REVIEW: SUGGEST_NEED_INFO}
#: 单据标题里"这条路径为什么拦"的一句话说明（进 `pending_item`，不进判定逻辑）。
_WHY: dict[str, str] = {
    DANGER_HIT: "命中高危清单（任一档位都拦）",
    SAFE_REVIEW: "保守档下非只读且不可自动回滚，须先请示人工",
}


class AutonomyGuardMisuse(ValueError):
    """交来的形状不成立（卡号没配需求单号 / 单据挂不住）。消息里只有 id 与键名。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


class UndeclaredAction(AutonomyGuardMisuse):
    """没有可核的动作文本（空白 `command`）。

    **单独一类而不是 `ALLOW`**：调用方拿不到动作原文时，"没在清单上"这句话不成立，
    把它读成放行就是本模块自己开的第二条放行分支。
    """

    def __init__(self, agent_id: str) -> None:
        super().__init__(
            f"agent {agent_id!r} 的出站动作没有申报命令文本 ⇒ 无法核对清单，拒绝判定（不放行也不拦截成单）",
            code="action_undeclared",
        )


@dataclass(frozen=True)
class PlannedAction:
    """一次"适配器即将拿去做的事"。本模块只读它，不改它，也不从它推导出没写出来的事实。"""

    project_key: str
    agent_id: str
    command: str
    read_only: bool
    auto_rollback: bool
    task_card_id: str | None = None
    feature_request_id: uuid.UUID | None = None
    #: 波及面（五要素④）。默认空映射 = **没有申报用量**，不是"这次零成本"。
    #: 平台不替一次自己没跑过的动作编 token 数（FR-061 的三口径分列在这里就靠"不填"来诚实）。
    downstream_task_ids: tuple[str, ...] = ()
    estimated_extra_usage: Mapping[str, int] | None = None


@dataclass(frozen=True)
class Decision:
    """判定读数。`allowed` 是派生属性而不是第三个布尔字段：
    两个真值载体（`outcome` 与一个 flag）会漂成"outcome 说拦而 flag 说放"。"""

    outcome: str
    mode: str
    danger_list_version: str
    hits: tuple[str, ...]
    action: PlannedAction

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def kind(self) -> str | None:
        """拦截路径的卡点类；放行时是 `None`（没有单要建）。"""
        return None if self.allowed else KIND_D


def effective_mode(session: Session, project_key: str) -> str:
    """当前生效的档位。**无行 ⇒ 取 `store.DEFAULT_MODE`，且不建行**（owner 2026-10-15 的处置表第 ③ 条）。

    默认值只从仓储那一个常量取：这里再写一份 `"SAFE"` 就会出现"建档默认一档、拦截判定另一档"的漂法
    （`tasks.md` 的 T103 行下第 ④ 项登记的就是这件事，本模块是它的第一处消费者）。
    """
    row = store.read_autonomy_state(session, project_key=project_key)
    return str(row.mode) if row is not None else store.DEFAULT_MODE


def decide(session: Session, action: PlannedAction, danger_list: DangerList) -> Decision:
    """唯一判定入口。顺序与理由写在模块头。"""
    if not action.command.strip():
        raise UndeclaredAction(action.agent_id)
    mode = effective_mode(session, action.project_key)
    command = action.command.strip()
    hits = tuple(entry.pattern for entry in danger_list.match(command))
    if hits:
        return Decision(
            outcome=DANGER_HIT,
            mode=mode,
            danger_list_version=danger_list.fingerprint,
            hits=hits,
            action=action,
        )
    if mode == SAFE and not action.read_only and not action.auto_rollback:
        return Decision(
            outcome=SAFE_REVIEW,
            mode=mode,
            danger_list_version=danger_list.fingerprint,
            hits=(),
            action=action,
        )
    return Decision(
        outcome=ALLOW,
        mode=mode,
        danger_list_version=danger_list.fingerprint,
        hits=(),
        action=action,
    )


def pending_item(decision: Decision) -> str:
    """五要素①：这一次要人批的是什么。**命令原文先过 redaction**。"""
    action = decision.action
    parts = [
        f"{_WHY[decision.outcome]}：动作 {redact_bus_text(action.command.strip())!r}",
        f"发起方 agent_id={action.agent_id!r}",
        f"当时档位={decision.mode}",
        f"清单指纹={decision.danger_list_version}",
    ]
    if decision.hits:
        parts.append(f"命中条目={'; '.join(decision.hits)}")
    if not action.read_only:
        parts.append("该动作声明为可写")
    if not action.auto_rollback:
        parts.append("且不可自动回滚")
    return "｜".join(parts)


def context_digest(decision: Decision) -> str:
    """五要素②：为什么会有这一次判定。只放平台侧已有的读数，不抄 `raw_input`。"""
    action = decision.action
    return redact_bus_text(
        f"project_key={action.project_key}｜卡={action.task_card_id or '未挂卡'}｜"
        f"需求单={action.feature_request_id or '未挂单'}｜"
        f"只读岗={action.read_only}｜可自动回滚={action.auto_rollback}"
    )


def impact(decision: Decision) -> dict[str, object]:
    """五要素④：波及面。

    **被拦的那张卡本身必然在列表里**：它停下来了，"没有任何后续任务被影响"这个读数在 D 类这里是假的
    （`service.Impact` 的非空校验因此恰好是对的，而不是需要绕的开脱）。
    调用方申报的下游追加在后面；没申报就只有点名这一张。用量映射不填就交空映射，不编数字。
    """
    action = decision.action
    ids = [item for item in (action.task_card_id, *action.downstream_task_ids) if item]
    if not ids:
        raise AutonomyGuardMisuse(
            "D 类单必须点名至少一张被拦下的卡 ⇒ 没有卡号的出站动作请先挂卡再判",
            code="card_required",
        )
    return {
        "downstream_task_ids": ids,
        "estimated_extra_usage": dict(action.estimated_extra_usage or {}),
    }


def hold_ticket(session: Session, decision: Decision, *, now: dt.datetime | None = None) -> service.Ticket:
    """两条拦截路径的**唯一**出口：交 Phase 4 的 `create_approval()` 建 D 类单。

    - `facts` 不传 ⇒ 这张单不可能被规则表免确认（模块头那条锁 ①）；
    - 同作用域的既有未决单会被合并（FR-045），所以拦十次也只有一张挂在等人；
    - 放行路径进不来这里（`decision.kind is None` 时被拒），这一道是防"调用方把 Decision 传错"：
      把一次放行呈交成人审，同样是把判定搞成两个地方各说一遍。
    - **卡号与需求单号必须一起给**：`create_approval` 只拒"挂卡没挂需求单"那一半（复合外键在
      MATCH SIMPLE 下不校验），这里补上另一半 —— 只给需求单号不给卡号、以及两个都不给，
      在 D 类这一支都不收（一次出站动作必然属于一张卡，见 `impact()` 的理由）。
    """
    if decision.kind != KIND_D:
        raise AutonomyGuardMisuse(
            f"判定结果是 {decision.outcome!r}（放行），没有可呈交的人工单",
            code="nothing_to_hold",
        )
    action = decision.action
    if not action.task_card_id or not action.feature_request_id:
        raise AutonomyGuardMisuse(
            "D 类单要同时给出 task_card_id 与 feature_request_id ⇒ "
            "缺一就挂不住'哪张卡停下来了'这件事（EN-09 的复合外键在 MATCH SIMPLE 下不拦这种半拉子行）",
            code="card_required",
        )
    payload = service.build_payload(
        kind=KIND_D,
        pending_item=pending_item(decision),
        context_digest=context_digest(decision),
        suggested_verdict=_SUGGESTED[decision.outcome],
        impact=impact(decision),
    )
    return service.create_approval(
        session,
        kind=KIND_D,
        payload=payload,
        feature_request_id=action.feature_request_id,
        task_card_id=action.task_card_id,
        now=now,
    )


#: 拦截流水的类型（EN-14 `event_log_kind` 的取值之一）。这里写的是**调用参数**而不是第二份取值清单：
#: 载体仍在 `persistence/enums.py`，`events.record()` 会核它 —— 不在域里就抛 `EventMisuse`，
#: 不等 PG 的枚举类型报错（那样错因会被读成"数据库拒绝"而看不出是调用方写错了留痕类型）。
INTERCEPT_KIND = "AUTONOMY_INTERCEPT"

#: `record_interception()` 落进 `payload` 的键，**按集合钉**（多一个少一个都红，判据在
#: `tests/guards/test_autonomy_intercept.py`）。前四个逐字对着 `plan.md` 设计项 9 那句
#: "记动作、命中条目、当时档位、清单指纹"；`ticket_id` / `ticket_merged` 对着 `data-model.md`
#: EN-16 分工段的"是否已转 D 类单"（`merged` 说的就是"并进已有单"这件事，FR-045）；
#: 后四个是让"这一行为什么被拦"能在库里直接复核，不必回头读代码。
#: **回溯的两把钥匙**：`agent_id` 走 EN-14 的 `actor_agent_id` 列，清单指纹走本映射里的
#: `danger_list_version` 键。owner 2026-10-15 裁定这一对**不加索引**（现跑 `pg_indexes` 里
#: `event_log` 只有 `pk_event_log`）⇒ 回溯走全表扫，判据钉的是"查得到且分得开"，不是"查得快"。
INTERCEPT_PAYLOAD_KEYS = (
    "action",
    "outcome",
    "mode",
    "danger_list_version",
    "hits",
    "ticket_id",
    "ticket_merged",
    "project_key",
    "read_only",
    "auto_rollback",
)


def record_interception(
    session: Session,
    decision: Decision,
    ticket: service.Ticket,
    *,
    now: dt.datetime | None = None,
) -> EventLog:
    """一次拦截的流水（EN-14）+ 当前态的 `last_intercept_at`（EN-16）。返回流水行。

    **两笔写、一笔事务**：流水与当前态都在调用方的事务里 `flush()`，谁都不单独提交 ⇒
    "有流水而当前态没动"和"当前态动了而没流水"这两种半程态会在同一次回滚里一起消失。
    反过来，这也意味着 EN-16 缺行时本函数**整笔抛错**（`store.record_intercept()` 抛
    `AutonomyStateMissing`），而不是"流水先落下、当前态算了"—— 失败关闭的方向是"审计不完整就不算数"，
    装配点因此 MUST 先调 `ensure_autonomy_state()`（宿主 = T123，见 `tasks.md` 的 T105 行下追记）。

    **放行不许进来**（`nothing_to_log`）：这张表按 `kind` 数出来的就是"拦了几次"，SC-010 那句
    "清单内动作 100% 被拦"的分母也读自这里。把放行写成同一个 `kind`，两种行在库里就分不开了 ——
    那不是多几行噪声，是那两条读数从此不可信。

    **`entity` 只能是 `task_card`**：一次出站动作必然属于一张卡（`impact()` 与 `hold_ticket()`
    都这么要求），而 FR-051 的四要素查询按卡捞。没给卡号 ⇒ 拒（`card_required`），
    不改记成 `feature_request` —— 那样同一件事会有两种记法，按卡回溯会漏掉一半。

    **洗什么**：`action`（命令原文）过 `redact_bus_text()`，与 `pending_item()` 同一份洗法；
    `hits` 是策略文本（`rm -rf` 这种），不是凭据形态，原样留。整份 payload 还会在
    `events.record()` 里再递归过一遍 `wash()` —— 两道不是重复劳动：本模块这一道保证"进 payload 前就是
    干净的"，那一道保证"任何调用方都绕不过脱敏"（EN-14 的 guardrail 写在 `data-model.md` 那一行）。
    """
    if decision.allowed:
        raise AutonomyGuardMisuse(
            f"判定结果是 {decision.outcome!r}（放行）⇒ 拦截流水不收放行的行",
            code="nothing_to_log",
        )
    action = decision.action
    if not action.agent_id.strip():
        raise AutonomyGuardMisuse(
            "拦截流水的主体列是空的 ⇒ 按 `(agent_id, 清单指纹)` 回溯这件事会在这行上永远查不到",
            code="actor_required",
        )
    if not action.task_card_id:
        raise AutonomyGuardMisuse(
            "AUTONOMY_INTERCEPT 的留痕要挂在被拦的那张卡上 ⇒ 没有卡号的出站动作请先挂卡再判",
            code="card_required",
        )
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    row = events.record(
        session,
        entity="task_card",
        entity_id=action.task_card_id,
        kind=INTERCEPT_KIND,
        actor_agent_id=action.agent_id.strip(),
        payload={
            "action": redact_bus_text(action.command.strip()),
            "outcome": decision.outcome,
            "mode": decision.mode,
            "danger_list_version": decision.danger_list_version,
            "hits": list(decision.hits),
            "ticket_id": str(ticket.ticket_id),
            "ticket_merged": ticket.merged,
            "project_key": action.project_key,
            "read_only": action.read_only,
            "auto_rollback": action.auto_rollback,
        },
        created_at=moment,
    )
    store.record_intercept(session, action.project_key, now=moment)
    return row
