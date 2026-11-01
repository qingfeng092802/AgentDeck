"""T123 装配点（第 2 步）：`python -m acp.run` 把一条需求推到 A 类卡点，并把已裁决的单据恢复回去。

**这一笔覆盖到哪、没覆盖到哪**（如实划界，免得下一轮读成"全链路已通"）：
`--request` = 旗标门 → 注册面（EN-04 种子 + EN-03/EN-05 落库）→ 仿真总管补全 → EN-01 建行 →
受理图 `Plan → Split → Normalize → ApprovalGate` 跑到挂起并打印单据号。
`--resume <需求单号>` = 从 EN-09 的 `state` 推出结论 → 检查 thread 上真有等待点 → `resume_after_approval`
→ 读出卡片与需求单的现值。**两段式到这里算接上了**。
**还没接的是"交付包"，以及执行段的一处真实缺口**（`acp.run` 仍产不出 R-07 的那四份文件）：
五个入口里今天接上 3 个 —— `seed_builtin_roles`、`run_card`、`actor_for_seat`（后两个的调用点都在本文件：
`run_execute()` 走生命周期、`_review_node_body()` / `produce()` 取席位身份）。
2026-10-17 乙 一轮把 `Review` / `HandOff` 两个**节点体**接上了，也把 `Task` 组装 → `submit()` → `collect()`
那段驱动接上了；仍缺的一格是**审查结论的来源**（owner 裁 Q6 = 甲：`findings_for` 由调用方注入，
缺 ⇒ `findings_source_missing`）。因此命令行入口仍不开 —— 理由与 2026-10-16 裁 Q2 时不同了：
那时缺的是节点体（现读各 0），今天节点体在链路上，缺的是"审查岗真的说过话"这一格事实。
`assemble` 的生产调用方仍是 0；`resolve_escalation` 不是 0 —— 它有一个调用方在 HTTP 端点上
（`api/tasks.py:517`，升级的人工出口，2026-10-17 本轮复算时读出来的），但**在 `acp.run` 这条链路上仍是 0**：
升级之后要人先做决定，而这一发的装配点还不认识"人做过的升级决定"。复算命令与读数一并记在
`evidence/phase-12.md` §十四。

**为什么仿真的补全与拆单在这个文件里**（owner 2026-10-16 裁 乙）：它必须返回 `CompleterOutcome` /
`CardDraft`，那两个类型属编排层；放进 `src/acp/adapters/` 会造出**第一条** adapters → orchestrate 边，
冲击 T045 的"删掉一支目录，编排图仍可构建"。装配点是唯一同时合法 import 两侧的地方
（现存只有 `api/*` 与 `recover.py` 在 orchestrate 之外引它）。判据 = `tests/guards/test_assembly.py` 节 五。

**这一发证明不到什么**：权威表述只有一份，在 `quickstart.md §6` 末段的边界 ①（owner 补充约束 2 点名的
两处落点是 `evidence/phase-12.md` 与 `quickstart.md`）。这里**不重抄那句原话** —— 抄第三份就会有三份各自漂移的空间，
而 `scripts/check_tables.py` 与判据都只盯那两处。本文件自带的、可被 节 三 那条用例证伪的断言只有一句：
`StubOrchestrator` 的补全与拆单**不读需求正文**。

**stdout 的形状取 `recover` / `chaos` 那一族（`RUN <判定> :: key=value`），不取 `approve` 的单行 JSON**：
`approve` 是 HTTP 客户端，JSON 是给脚本消费的；这两个是直连库的 CLI，人和脚本都要读，
同族命令同形比"每种各好看一次"重要。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from acp import recover
from acp.adapters.stub import StubAdapter, StubMisuse, resolve_transport_flag
from acp.approval import policy as approval_policy
from acp.approval import repository as repo
from acp.approval.repository import pending_for
from acp.archive import root as archive
from acp.bus import OrchestratorWriter
from acp.bus import actor as bus_actor
from acp.bus import layout
from acp.bus.gateway import BusGateway, TaskCardBody
from acp.bus.orchestrator_writer import USAGE_BASIS, StatusRow
from acp.config.loader import CredentialMissing, install_logging_filter
from acp.cost import ledger
from acp.guard import triple as gp
from acp.orchestrate import graph as orchestrate_graph
from acp.orchestrate import intake
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import accept as accept_node
from acp.orchestrate.nodes import approval_gate as gate
from acp.orchestrate.nodes import assemble as assemble_node
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.orchestrate.nodes import handoff as handoff_node
from acp.orchestrate.nodes import review as review_node
from acp.orchestrate.nodes import select_worker as sw
from acp.orchestrate.nodes.plan import CompleterOutcome, plan_requirement
from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.prompts import role_brief_for_card
from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft
from acp.orchestrate.state import IntakeState
from acp.reporting.cards import is_system_card
from acp.persistence import events
from acp.persistence import session as session_module
from acp.persistence.checkpointer import (
    build_checkpointer,
    ensure_checkpoint_schema,
    open_connection,
    thread_config,
)
from acp.persistence.models_approval import ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Deliverable, EventLog
from acp.protocol.messages import ArtifactFloor, Task
from acp.registry import roles as role_registry
from acp.registry.persist import persist_registrations
from acp.review import policy as review_policy
from acp.runtime.slot_manager import InProcessSlotManager, load_platform_limit

PROG = "python -m acp.run"
ACTOR_AGENT_ID = AUTOMATED_ACTOR
CARD_AWAITING = "PENDING_APPROVAL"
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2

#: 拒绝原因的 stable 键名：措辞可以改，代号改了就是调用方要跟着改（口径同 `dispatch.SlotConfigError`）。
#: 这里只声明**本模块自己抛**的那三个。`archive_root_missing` 不在列 —— 它由 `archive/root.py` 抛、
#: 由 `main()` 原样转写；在装配点再抄一份常量就是"一事实两载体"，抄错的那一份会成为新的错源。
USAGE_MISSING = "arguments_missing"
CHECKPOINT_UNAVAILABLE = "checkpointer_unavailable"
ALREADY_RUNNING = "request_not_intake"
#: 恢复侧的三格，各自是"跑不下去"的一个不同原因，合成一格就没法告诉人缺的是哪一样。
REQUEST_MISSING = "feature_request_missing"
NO_WAITING_POINT = "no_waiting_point"
TICKET_MISSING = "approval_ticket_missing"
#: EN-09 那行还 `PENDING`（或落在 `TIMED_OUT_CONVERGED` 这类非决定态）⇒ 没有可交回图的结论。
NOT_DECISIVE = "approval_not_decisive"
#: 恢复跑完了但 EN-02 里一张卡都没有 ⇒ 读数会是空的。与受理侧的 `request_not_intake` 分开：
#: 那一格说的是"这条单子不在卡点上"，这一格说的是"单子推完了却没留下可继续调度的卡"。
CARDS_MISSING = "cards_missing"
#: 指派段的两格。各自是"这一段跑不起来"的一个不同原因，不许并成一格 `nothing_to_do`：
#: 前者是装配面缺东西（注册面没跑），后者是业务面还没到时候（依赖没满足 / 卡不在可派发态），
#: 读错方向的人会去补错那一边。
AGENTS_MISSING = "agents_not_registered"
NOTHING_DISPATCHABLE = "nothing_dispatchable"
#: Q10 的那一格：`max_cards_per_round` 给成 0 或负数。**这不是"这轮不派发"**（那由 `deferred` 报出来），
#: 而是调用方把一个不可能的值当成决定 ⇒ 拒，四张 ready 卡不许因为一个 0 就原地消失（FR-025）。
ROUND_LIMIT_INVALID = "round_limit_invalid"
#: 台账里没有这张卡的凭据 ⇒ 派发那一步没跑过（或跑在另一个进程里）。
#: 判定本体**只在** `scheduler.LeaseLedger.take()` 那一处；这里出现的只是把那句开发者视角的说明
#: 换成 CLI 侧的归因（哪一发、哪张卡、缺什么前置），不是第二份"有没有槽"的检查。
LEASE_MISSING = "lease_missing"
#: 审查结论没有来源（owner 2026-10-17 裁 Q6 = 甲：`findings_for` 由调用方注入，缺就停下）。
#: 这一格单独一个代号而不是并到 `arguments_missing`：前者是"命令行敲错了"，后者是"这条链路今天还没有
#: 真实的审查结论来源"，读错方向的人会去补 CLI，而该补的是审查席那一侧。
FINDINGS_MISSING = "findings_source_missing"
#: 验收结论没有来源（owner 2026-10-17 裁 Q8 = 甲：`accept_for` 由调用方注入，缺就停下）。
#: 与 `FINDINGS_MISSING` 分两个代号而不是并成一个"没有来源"：那一格停在审查岗说话之前，这一格停在
#: 验收岗说话之前，卡态与已写的行数都不一样，读的人要按代号找段、按段找缺的那一环。
ACCEPTANCE_MISSING = "acceptance_source_missing"
#: 装配点"代验收岗填条目"这件事自己的留痕动作名（owner 裁 Q8 的补条："必须被显式记录，不是静默代填"）。
#: 它与实体那句 `acceptance_round` 是两条事实：那句记"这一轮判了几条、过几条"（由 `accept()` 写），
#: 这一行记"这些判定不是验收岗给的，是仿真来源交的"（由装配点写）。合成一条就分不开"谁判的"。
ACCEPT_PROXY_ACTION = "acceptance_items_proxy"
#: 被席位挡掉的候选的留痕动作名（owner 2026-10-17 裁 Q7 补第 2 条："原因不许只写成'换人失败'"）。
#: 单独一个名字是因为它与 `handoff()` 自己那两条出口（`handed_off` / `escalated`）**是同一张卡上三条不同的事**：
#: 这一条说的是"哪些工具根本没进候选集、为什么"，那两条说的是"换没换成"。混用同一个 `action`，
#: 读留痕的人就没法分开"没有可选的人"与"有可选的人但换了/没换"。
SEAT_BLOCKED_ACTION = "seat_blocked_candidates"

#: **结论的唯一来源是 EN-09 那行的 `state`，不是命令行上再敲一遍的 verdict**。
#: 键用 `repository` 的常量而不是字面量：那份别名与 `approval_state` 域的对应关系由 `_named()` 在 import 期就核过。
#: 只映射两个决定态 —— `PENDING` 没有结论可交，`TIMED_OUT_CONVERGED` 是"超时收敛"而不是人的决意，
#: 把它当"批准"恢复等于让一次超时变成一次自动放行（FR-044 的优先级表与 §5 的超时语义都不认这个）。
CONCLUSION_BY_STATE: dict[str, str] = {repo.APPROVED: gate.APPROVE, repo.REJECTED: gate.REJECT}


class RunFailure(RuntimeError):
    """跑得起来但到不了挂起点。`code` 点名是哪一格。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


# ———————————————————————————————————— 仿真总管（裁 乙）

#: 四张卡的固定拆法：一张样式 + 三张页面，页面都依赖样式。
#: 这个数字**不是从 `raw_input` 推出来的** —— 真推就等于宣称仿真总管会拆单，而它不会：
#: 它只是把 R-07 要的构成（3 HTML + 1 CSS）按卡点名，让链路有一段真实形状可走。
_CARDS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("T01", "全站样式表", "styles.css", ()),
    ("T02", "首页", "index.html", ("T01",)),
    ("T03", "产品说明页", "about.html", ("T01",)),
    ("T04", "联系页", "contact.html", ("T01",)),
)
_FLOOR = (512, 12)


def _criteria(title: str, file_name: str) -> tuple[str, ...]:
    """一张卡的判据**只在这里写一次**。

    `split()` 与 `acceptance_links()` 都读这一份：两边各写一份一定会漂，而 `validate_links`
    的关系式是"每条判据恰被一条映射覆盖、映射不许写卡上没有的判据"（`split.py:211-227`），漂了就是 `MappingViolation`
    —— 2026-10-16 的命令行冒烟就是这么撞上的（四张卡各两条判据全数漏配）。
    """
    return (f"产物 {file_name} 存在且非空", f"{title} 的正文可读")


class StubOrchestrator:
    """补全 + 拆单的仿真实现（一个对象两处能力，因为仿真侧也只做一件事：交出固定内容）。

    `complete()` **不读 `raw_input` 的内容**：读它就会变成"仿真总管看懂了需求"，
    而那正是 FR-002 要真总管做到的事。原话只被数一次长度，用来拒空输入。
    """

    def complete(self, raw_input: str) -> CompleterOutcome:
        """**不读 `raw_input` 的内容**：读它就会变成"仿真总管看懂了需求"，而那正是 FR-002 要真总管做到的事。

        空输入不在这里拒 —— `plan_requirement()` 已有 `BlankRequirement`，CLI 层在开门凭据之前又拒一次
        （`main()` 的 `arguments_missing`）；这里再写第三份只会多一个没人走的分支。
        """
        return CompleterOutcome(
            self_completed=(
                "页面数量取 4 张卡：一份全站样式 + 首页 + 产品说明页 + 联系页",
                "配色按“简洁商务”取，不追问",
                "联系方式取邮箱，不做表单",
            ),
            clarifications=(),
        )

    def split(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[CardDraft, ...]:
        return tuple(
            CardDraft(
                task_id=card_id,
                title=title,
                difficulty=1 if not deps else 2,
                skill_tags=["frontend_coding"],
                depends_on=list(deps),
                artifact_floor=ArtifactFloor(
                    min_bytes=_FLOOR[0], min_lines=_FLOOR[1], format=("css" if file_name.endswith(".css") else "html")
                ),
                acceptance_criteria=list(_criteria(title, file_name)),
            )
            for card_id, title, file_name, deps in _CARDS
        )

    def acceptance_links(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[AcceptanceLink, ...]:
        """一张卡两条判据 ⇒ 两条映射（`validate_links` ③ 要"每条判据恰有一条"，多一条也算红）。

        `requirement_item` 只被要求非空白（`split.py:204-207`），所以这里按卡点名一条**粗**需求项：
        它证明的是"映射这一层接得上"，不是"仿真总管认得出需求条目" —— 后者归 FR-004，本轮不宣称。
        """
        return tuple(
            AcceptanceLink(
                task_id=card_id,
                requirement_item=f"{title} 按声明交付",
                acceptance_item=item,
                produced_by=ACTOR_AGENT_ID,
            )
            for card_id, title, file_name, _deps in _CARDS
            for item in _criteria(title, file_name)
        )


class StubFindingProvider:
    """`review.FindingProvider` 的**仿真实现**：按卡上声明的标准逐条交回 `met=True`。

    诚实性（owner 2026-10-17 裁 Q6 = 甲的原话要求，同时登记在 `quickstart.md §6` 与
    `evidence/phase-12.md §十四`）：**这一支不构成 FR-029 的实证**。FR-029 要的是"审查者按卡上声明的
    标准逐条判过"，而这里每一条的判定都是写死的 `True`，与被审对象的实际内容**没有因果关系**。
    它存在的理由只有一个：让"结论来源缺失"这一格从阻塞变成可绕过的显式选择，从而把执行段的**机制**
    （`build_request → check_findings → record_verdict → 状态迁移`）演到能复核的形状。
    所以：

    - `evidence` 那一格**把仿真身份写进结论本身**（会落进 EN-08 的 `item_findings`），
      查库的人一眼能看出这条结论是谁交的 —— 不留"看起来像真判定"的读数；
    - 它**不是默认值**：必须由调用方显式传进 `run_execute(findings_for=…)`。缺 ⇒ `findings_source_missing`；
    - `--transport stub` 那一扇门（`resolve_transport_flag`）管的是"不许真调厂商"，这一支管的是
      "不许把固定 pass 当成审查"。两道门各管一件事，合成一道就会有一种错溜过去。
    """

    #: 证据串里的这个前缀是给"这条结论是仿真交的"留的机读标记，判据钉它（M 系列），也给人查库时用。
    MARK = "stub-finding-provider"

    def findings_for(self, card, criteria):
        prefix = f"{self.MARK}：卡 {card.id} 的标准"
        return tuple(
            review_node.ReviewFinding(
                criterion=str(item),
                met=True,
                evidence=f"{prefix} {str(item)!r} 由仿真来源固定判过（不是 FR-029 的实证）",
            )
            for item in criteria
        )


#: 验收结论的来源形状（owner 2026-10-17 裁 Q8 = 甲）。
#: **协议为什么在装配点这一侧，不像 Q6 那样放进实体层**：`review.record_verdict()` 需要"逐条判定"这个
#: 载体，所以 `FindingProvider` 归 `review.py`；而 `accept.accept()` 收的就是 `ItemVerdict` 序列
#: （`accept.py:145-152`），实体层不需要第三种入口 ⇒ 把协议放进 `accept.py` 反而是给一个不读它的模块
#: 造门面。条目集合仍由实体层给（`expected_items()` 读 EN-07），完整性仍由实体层判（`check_items()`
#: 双向差集），装配点只负责"把条目的判定交进来"。
class AcceptanceProvider(Protocol):
    """按 EN-07 的映射表逐条交回验收判定 —— 不许自拟条目，也不许只交"整体通过"。"""

    def verdicts_for(self, card, items: Sequence[str]) -> Sequence[accept_node.ItemVerdict]: ...


class StubAcceptanceProvider:
    """`AcceptanceProvider` 的**仿真实现**：EN-07 表里每条交回 `met=True`。

    诚实性（owner 2026-10-17 裁 Q8 = 甲原话要求的声明）：**这一支不构成 FR-051 / FR-052 的实证**。
    判据基础是真的（条目集合从 EN-07 映射表读、`check_items()` 仍做双向差集、验收岗身份仍由
    `actor_for_seat(seat="acceptor")` 取，`accept()` 那句"不是同人就不收"照旧生效），
    但每一条的 `met` 都是写死的 `True`，与产物实际内容没有因果关系。
    owner 把它与 `StubAdapter` / `StubFindingProvider` 算同族，所以形状也照同一族规：

    - **不是默认值**：必须由调用方显式传进 `run_accept(accept_for=…)`，缺 ⇒ `acceptance_source_missing`；
    - **不静默代填**：这一支交回判定后，装配点另落一行 `action=acceptance_items_proxy` 的 EN-14，
      把"哪几条是代填的、来源的机读标记是什么"写进库里（owner 裁 Q8 的补条）。真实来源不声明 `MARK`
      ⇒ 那一行不写，于是"代填"这件事只在真的发生时留痕，不是一条恒常噪音。
    """

    #: 与 `StubFindingProvider.MARK` 同规：机读身份标记，进留痕载荷，也给人查库时用。
    MARK = "stub-acceptance-provider"

    def verdicts_for(self, card, items: Sequence[str]) -> tuple[accept_node.ItemVerdict, ...]:
        prefix = f"{self.MARK}：卡 {card.id} 的条目"
        return tuple(
            accept_node.ItemVerdict(
                acceptance_item=str(item),
                met=True,
                evidence=f"{prefix} {str(item)!r} 由仿真来源固定判过（不是 FR-051 / FR-052 的实证）",
            )
            for item in items
        )


# ———————————————————————————————————— 跑到挂起点

@dataclass(frozen=True)
class IntakeReport:
    """一次受理的读数。四列都是**库里的现值**，不是图 state 的回执（权威在库，见原则 I）。"""

    feature_request_id: uuid.UUID
    ticket_id: uuid.UUID
    card_ids: tuple[str, ...]
    request_status: str


def _prepare_intake(session, now: dt.datetime) -> None:
    """受理图之前的两步种子 + 注册（CLI 与网页入口共用这一份，不各写一遍）。"""
    role_registry.seed_builtin_roles(session)
    persist_registrations(session, declared_at=now)


def _intake_report(session, row) -> IntakeReport:
    return IntakeReport(
        feature_request_id=row.id,
        ticket_id=_require_pending_ticket(session, row.id),
        card_ids=_require_cards_awaiting(session, row.id),
        request_status=row.status,
    )


def run_intake(
    raw_input: str,
    *,
    project_key: str,
    transport: str,
    bus_root: Path | None,
    session,
    now: dt.datetime,
    orchestrator: StubOrchestrator | None = None,
) -> IntakeReport:
    """把一条需求从 EN-01 推到 A 类卡点挂起。**不 commit**（提交点归调用方，同 `_persist` 的口径）。

    顺序是有意的，三处不能换：
    ⓪ `resolve_transport_flag` 在**打开任何会话之前**：门的拒绝必须比凭据的缺失先说话，
       否则打错 `--transport` 的人读到的是"缺 DSN"，而归因应该是"这个旗标我不认"。
    ① `seed_builtin_roles` 与 `persist_registrations` 必须在受理图之前 —— 拆完卡就要选人，
       EN-04 空表时 `assign_guards` 抛 `guard_not_closable`，那次失败会远到看不出是"没种岗位"；
       这也是 `tasks.md` T123 行上那条 MUST（WorkBuddy `phase7-review` P1-2）的落点。
    ② 补全在建行之前：`summary_digest` 是 EN-01 的一列，受理图的 `_plan` **只搬不算**（一次需求只补全一次）。
    """
    resolve_transport_flag(transport)
    _prepare_intake(session, now)

    stub = orchestrator or StubOrchestrator()
    outcome = plan_requirement(raw_input, completer=stub)
    row = intake.new_request(
        project_key=project_key,
        raw_input=raw_input,
        summary_digest=outcome.summary_digest,
        created_at=now,
    )
    session.add(row)
    session.flush()

    _advance(row.id, session=session, splitter=stub, bus_root=bus_root)
    session.flush()
    return _intake_report(session, row)


def run_intake_existing(
    feature_request_id: uuid.UUID,
    *,
    transport: str,
    bus_root: Path | None,
    session,
    now: dt.datetime,
    orchestrator: StubOrchestrator | None = None,
) -> IntakeReport:
    """把**已经落库**的那张需求单推到 A 类卡点（网页受理的后台那一半）。

    与 `run_intake()` 的唯一差别是它不再建 EN-01 行：网页入口已经把受理回执与那一行一起交出去了，
    再建一行会让同一句话在库里变成两张单。补全也不在这里重跑 —— 摘要就是端点收下的那一份。
    拆单器仍取仿真那一支：它固定交四张卡、**不读 `raw_input` 的内容**（`StubOrchestrator` 的模块头
    写着这条诚实边界），网页上的"拆解"因此不能被读成模型读懂了需求。
    """
    resolve_transport_flag(transport)
    _prepare_intake(session, now)
    row = _request_row(session, feature_request_id)
    stub = orchestrator or StubOrchestrator()
    _advance(row.id, session=session, splitter=stub, bus_root=bus_root)
    session.flush()
    return _intake_report(session, row)


def _auto_pass_split(session, report: IntakeReport, *, now: dt.datetime) -> bool:
    """P1-8：CLI 也读 `ACP_APPROVAL_MODE` —— 与网页那条路**同一档语义**。

    为什么必须统一：同一条 `.env`、同一个仓库，网页起的单会自动过 A 类门，CLI 起的单却停在
    `RUN WAITING`。那是"我按文档设了 auto，换个入口还是要人点一下"的现场，而两边的判据各自
    都自洽 —— 查起来最费时间的一类分歧。

    **档位判断、免确认写法、署名规则名全部复用**，这里只多一件事：读环境。
    网页那侧在启动期解析一次并注入（`serve.build_app`），CLI 每发一次，所以读在调用点。

    返回"签成了没有"：签不掉（单据已被别人定过 / 不存在）就回到等人批的老路，
    由调用方决定要不要续跑 —— 把"没签成"表现成"整单失败"是另一种假话。
    """
    raw = os.environ.get(approval_policy.APPROVAL_MODE_ENV)
    if raw is None:
        return False
    try:
        mode = approval_policy.resolve_approval_mode(raw)
    except approval_policy.ApprovalModeInvalid as exc:
        # 认不出的取值不降级成 manual：那会让"我以为它开着"和"它其实关着"变成同一屏，
        # 而这里差的是一整道人工门（与 `serve.build_app` 的启动期拒绝同一条口径）。
        raise RunFailure(str(exc), code="approval_mode_invalid") from exc
    if not approval_policy.auto_split_confirm(mode):
        return False
    outcome = repo.apply_auto_pass(
        session,
        ticket_id=report.ticket_id,
        decided_by=approval_policy.SPLIT_AUTO_RULE,
        now=now,
    )
    return bool(getattr(outcome, "applied", False))


def _require_pending_ticket(session, feature_request_id: uuid.UUID) -> uuid.UUID:
    """挂起到底发生了没有 —— 读 EN-09，不读图 state（原则 I：一事实一载体，权威在库）。

    为什么单独成函数：这一格与下面那格都是"跑完了但没到挂起点"的拒绝。写在 `run_intake` 里
    就只能靠"把整张受理图假掉"来触发 —— 那是拿假现场证一段拒绝，证完还把真路径挤没了；
    拆出来之后两条拒绝各能用一行小场面直接喂（判据文件 节 四 的两条负面对照）。
    """
    ticket = pending_for(session, feature_request_id=feature_request_id)
    if ticket is None:
        raise RunFailure(
            f"受理图跑完了但 EN-09 里没有该需求的 `PENDING` 单据 ⇒ 打印出来的单据号会是假的："
            f"feature_request_id={feature_request_id}",
            code=ALREADY_RUNNING,
        )
    return ticket.id


def _require_cards_awaiting(session, feature_request_id: uuid.UUID) -> tuple[str, ...]:
    """卡 MUST 全体在 `PENDING_APPROVAL`，否则恢复时按卡找等待点会落空。

    **EN-01 的状态域里根本没有"待批"这一值**（`enums.py:38` 给的是 `INTAKE / SPLIT / RUNNING /
    DELIVERED / FAILED`），卡点动的是 EN-02 的卡状态（`DRAFT → PENDING_APPROVAL`）与 EN-09 的
    一行 `PENDING` 单据。所以这里判的是那两张表的现值；`state` 里的 `approval_ticket_id`
    只是同一个事实的第二视图，`.get()` 那种"取不到就当没发生"的写法一律不用。

    **只判"该进卡点的卡"**：文档 / 检查 / 验收 / 打包四类系统节点卡建出来就是 `DELIVERED`，
    它们不进 A 类卡点，自然也永远等不到 `PENDING_APPROVAL`。判全体就等于要求系统卡
    先倒退成 `DRAFT` 再被批一遍 —— 那是把已经发生的事实改成没发生。

    2026-10-28 真跑实测：这一格与 `approval_gate._set_card_status` 是同一个根因的**两个落点**。
    先修了卡点那一处，受理仍然在这里以 `request_not_intake` 结束（读到
    `{'T90': 'DELIVERED', 'T91': 'DELIVERED', 'T92': 'DELIVERED'}`）。两处都要排除，
    只改一处等于把失败往后挪了一步。
    """
    statuses = {
        card.id: card.status
        for card in session.query(TaskCard).filter(TaskCard.feature_request_id == feature_request_id).all()
        if not is_system_card(card)
    }
    if not statuses:
        raise RunFailure(
            f"受理图跑完了但 EN-02 里一张卡都没有 ⇒ 需求单成了空单："
            f"feature_request_id={feature_request_id}",
            code=ALREADY_RUNNING,
        )
    off_gate = {card_id: status for card_id, status in statuses.items() if status != CARD_AWAITING}
    if off_gate:
        raise RunFailure(
            f"卡点建了单但卡没全体进入 `{CARD_AWAITING}`：读到 {off_gate} ⇒ 恢复时会对不上",
            code=ALREADY_RUNNING,
        )
    return tuple(sorted(statuses))


def _require_card_statuses(session, feature_request_id: uuid.UUID) -> tuple[tuple[str, str], ...]:
    """恢复**之后**的卡态读数：每张卡一对 `(卡号, 现态)`，一张都没有就拒。

    为什么不复用上面那道 `_require_cards_awaiting`：那道判"是不是全体都在卡点态"（恢复之前的形状），
    这道判"卡还在不在、各落在什么态"（恢复之后的读数）。合成一个函数就得在里面分叉，而分叉出来的
    那一支恰好是需要小场面直接喂的拒绝 ⇒ 拆开，各配一名证人（`test_run_intake.py` 的
    `test_the_resume_reading_refuses_an_empty_card_face` 与 `test_cards_that_never_entered_the_gate_are_refused`）。
    """
    statuses = tuple(
        sorted(
            (card.id, str(card.status))
            for card in session.query(TaskCard).filter(TaskCard.feature_request_id == feature_request_id).all()
        )
    )
    if not statuses:
        raise RunFailure(
            f"恢复跑完了但 EN-02 里一张卡都没有 ⇒ 读数会是空的：feature_request_id={feature_request_id}",
            code=CARDS_MISSING,
        )
    return statuses


@dataclass(frozen=True)
class ResumeReport:
    """一次恢复的读数。`waiting_before` / `waiting_after` 是**同一个 thread 的两个时刻**：
    恢复成功的形状是"从 `(卡点节点,)` 变成 `()`"，只报后者就看不出它确实是从等待点起来的。
    """

    feature_request_id: uuid.UUID
    ticket_id: uuid.UUID
    ticket_state: str
    conclusion: str
    waiting_before: tuple[str, ...]
    waiting_after: tuple[str, ...]
    card_statuses: tuple[tuple[str, str], ...]
    request_status: str


def _decided_ticket(session, feature_request_id: uuid.UUID) -> ApprovalTicket:
    """从 EN-09 取这张需求单的 A 类单据，并判它**有没有一个决定性的结论**。

    为什么不收命令行上再敲一遍的 verdict：那样就有两个载体（argv 与库里那行）说同一件事，
    而 `ApprovalGate` 自己在恢复时会拿交回来的结论与库里那行比对（`approval_gate.py:196-201`，
    不一致直接 `GateMisuse`）。装配点如果允许人猜，第一次猜错得到的就是一次 `GateMisuse`
    而不是"你该先读单据"。所以这里只读、不猜：**结论是 `state` 的函数**。
    """
    rows = (
        session.query(ApprovalTicket)
        .filter(
            ApprovalTicket.feature_request_id == feature_request_id,
            ApprovalTicket.kind == gate.KIND,
        )
        .all()
    )
    if not rows:
        raise RunFailure(
            f"EN-09 里没有这条需求单的 `{gate.KIND}` 单据 ⇒ 没有可交回图的结论："
            f"feature_request_id={feature_request_id}",
            code=TICKET_MISSING,
        )
    if len(rows) > 1:
        raise RunFailure(
            f"同一需求单的 `{gate.KIND}` 单据有 {len(rows)} 张（{[str(row.id) for row in rows]}）"
            f"⇒ 该按哪一张的结论恢复不能猜",
            code=TICKET_MISSING,
        )
    ticket = rows[0]
    if ticket.state not in CONCLUSION_BY_STATE:
        raise RunFailure(
            f"单据 {ticket.id} 的当前态是 {ticket.state!r}，不在决定态 {sorted(CONCLUSION_BY_STATE)} 里 "
            f"⇒ 还没有人可以交回结论（`PENDING` 就是还没批；`TIMED_OUT_CONVERGED` 是超时收敛，"
            f"把它当批准恢复等于让一次超时变成一次自动放行）",
            code=NOT_DECISIVE,
        )
    return ticket


def run_resume(
    feature_request_id: uuid.UUID,
    *,
    transport: str,
    bus_root: Path | None,
    session,
    orchestrator: StubOrchestrator | None = None,
) -> ResumeReport:
    """把一张**已被人裁决过**的 A 类单据交回受理图续跑（FR-041：从被打断的那一步起，不重跑链路）。

    四步各挡一种会发生的错，顺序不可换：
    ⓪ 门先说话（与 `run_intake` 同一个理由：拒绝必须比凭据的缺失先归因）。
    ① 需求单要在库里：不在就是恢复了一个不存在的现场，报 `feature_request_missing` 而不是图内部的错。
    ② 结论只从 EN-09 的 `state` 推（`_decided_ticket`）。
    ③ 图上 MUST 真有等待点才谈恢复：`Command(resume=…)` 打到一条没有中断的 thread 上，
       LangGraph 给的是它自己的错，那句话不会告诉人"这条单子其实已经跑过卡点了"。
       所以这里先读一次 `next`，把归因换成平台自己的代号 `no_waiting_point`。
    """
    resolve_transport_flag(transport)
    request = session.get(FeatureRequest, feature_request_id)
    if request is None:
        raise RunFailure(
            f"EN-01 里没有这条需求单 ⇒ 无现场可恢复：feature_request_id={feature_request_id}",
            code=REQUEST_MISSING,
        )
    ticket = _decided_ticket(session, feature_request_id)
    conclusion = CONCLUSION_BY_STATE[ticket.state]
    stub = orchestrator or StubOrchestrator()

    with _saver() as saver:
        graph = orchestrate_graph.build_graph(
            orchestrate_graph.OrchestrateDeps(
                session=session, splitter=stub, checkpointer=saver, bus_root=bus_root
            )
        )
        waiting_before = _next_nodes(graph, feature_request_id)
        if not waiting_before:
            raise RunFailure(
                f"thread 上没有等待点（`next=()`）⇒ 这条需求单要么已经跑过卡点，要么从没挂起过："
                f"feature_request_id={feature_request_id}",
                code=NO_WAITING_POINT,
            )
        # "有等待点"还不够，得是**那一个**等待点（U1，2026-10-28）。历史上有过一版 checkpoint
        # 被误推进覆盖成 `next=('plan',)` 的现场：`next` 非空，于是上面那道守卫放行，
        # `Command(resume=…)` 从 `plan` 续跑，必然再撞 `AlreadyAdvanced` —— 报出来的错会指向
        # 拆单，而真正发生的是"等待点没了"。这里把归因换回它该在的地方。
        if orchestrate_graph.APPROVAL_GATE not in waiting_before:
            raise RunFailure(
                f"thread 停在 {waiting_before} 而不是人工卡点（{orchestrate_graph.APPROVAL_GATE}）⇒ "
                f"没有可续的等待点：feature_request_id={feature_request_id}",
                code=NO_WAITING_POINT,
            )
        orchestrate_graph.resume_after_approval(
            feature_request_id,
            session=session,
            splitter=stub,
            checkpointer=saver,
            conclusion=conclusion,
            bus_root=bus_root,
        )
        waiting_after = _next_nodes(graph, feature_request_id)

    card_statuses = _require_card_statuses(session, feature_request_id)
    return ResumeReport(
        feature_request_id=feature_request_id,
        ticket_id=ticket.id,
        ticket_state=ticket.state,
        conclusion=conclusion,
        waiting_before=waiting_before,
        waiting_after=waiting_after,
        card_statuses=card_statuses,
        request_status=str(request.status),
    )


@contextmanager
def _saver() -> Iterator[Any]:
    """开一条 checkpointer 连接、把 schema 备好、交出绑好的 saver；**连接的生死归这一个函数**。

    为什么提成共用件而不是在跑与恢复两处各写一遍：那四行（`open_connection` → `CredentialMissing` 归因 →
    `ensure_checkpoint_schema` → `build_checkpointer`）里任何一行只在其中一份里被改掉，就会出现
    "受理跑得起来但恢复跑不起来"这种最难归因的裂口（少了 `ensure_checkpoint_schema` 那一份，
    表现是新库上恢复报 `relation "checkpoints" does not exist`，看着像 checkpointer 坏了）。
    """
    try:
        connection = open_connection()
    except CredentialMissing as exc:
        raise RunFailure(
            f"打不开 checkpointer 的连接：变量 {exc.name!r} 未注入 ⇒ 受理图没有等待点可言",
            code=CHECKPOINT_UNAVAILABLE,
        ) from exc
    with closing(connection) as conn:
        ensure_checkpoint_schema(conn)
        yield build_checkpointer(conn, state_schema=IntakeState)


def _next_nodes(graph, feature_request_id: uuid.UUID) -> tuple[str, ...]:
    """这个 thread 现在停在哪些节点上。空元组 = **没有等待点**（不是"停在 START"）。"""
    snapshot = graph.get_state(thread_config(kind=gate.KIND, feature_request_id=feature_request_id))
    return tuple(str(node) for node in (snapshot.next or ()))


def waiting_nodes(
    feature_request_id: uuid.UUID,
    *,
    session,
    bus_root: Path | None,
) -> tuple[str, ...]:
    """P1-3：给续跑器读一次"图上还有没有等待点"的公开入口。

    只读：开一条 checkpointer 连接、按 `IntakeState` 建同一张受理图、读 `next`，不跑图、
    不写库。`continue` 分支用它判"单据已批但图还停在 approval_gate"那格 —— 这时必须走
    `run_resume`，否则 `resolve` 无单可批、`continue` 不碰图，卡会永久停在
    `PENDING_APPROVAL`。
    """
    with _saver() as saver:
        graph = orchestrate_graph.build_graph(
            orchestrate_graph.OrchestrateDeps(
                session=session,
                splitter=StubOrchestrator(),
                checkpointer=saver,
                bus_root=bus_root,
            )
        )
        return _next_nodes(graph, feature_request_id)


def _advance(feature_request_id: uuid.UUID, *, session, splitter, bus_root: Path | None) -> IntakeState:
    """受理图那一跑（走 `orchestrate_graph.advance_request` 这一条既有缝，不另起一张图）。

    `bus_root` 缺省**不猜**：`_normalize` 要打开工前一拍（`cadence.commit_archive_point`），
    那里要求"目录存在且它是一个 git 顶层"，缺任何一条都由 `archive.root` 报出带代号的错，
    这里只把它翻成 CLI 的一行判定并点名命令（装配点建现场那一格仍归 T123 的 待裁②，未闭）。
    """
    try:
        with _saver() as saver:
            return orchestrate_graph.advance_request(
                feature_request_id,
                session=session,
                splitter=splitter,
                checkpointer=saver,
                bus_root=bus_root,
            )
    except RunFailure:
        raise
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        raise RunFailure(f"受理图这一跑停在 `{code}`：{exc}", code=code) from exc


# ———————————————————————————————————— 指派段（第 2 步下半段第 2 项）

#: 瞬态层的上限**从 EN-03 现值读**，装配点不写第二份数（原则 I：一事实一载体）。
#: 平台上限那一个数例外：它按引用型参数条款只认 `config/concurrency.yaml`（`load_platform_limit()`），
#: EN-03 里没有这一列，所以这里读的是两个不同载体各自的权威，不是一份数抄两处。
#: 只认 `enabled` 的工具：停用的那支就算有额度也不该被占 —— 派发之后没人能交付。
def _slots_from_registry(session) -> InProcessSlotManager:
    """EN-03 里没有一支 `enabled` 的**执行**工具 ⇒ `agents_not_registered`，而不是让槽层去报空映射。

    `deepseek-reviewer` 这类审查专用身份不进槽表：它没有执行适配器、永不参与派发，
    把它算进来会让 `usage()` 多出一个永远为 0 的席位，并让"平台上限 = 首批执行 Agent 数"
    这条关系式平白多出一项。筛选口径取 `review.policy.REVIEW_ONLY_AGENT_IDS`（单载体）。
    """
    caps = {
        str(row.agent_id): int(row.max_parallel)
        for row in session.query(AgentUnit).filter(AgentUnit.enabled.is_(True)).all()
        if str(row.agent_id) not in review_policy.REVIEW_ONLY_AGENT_IDS
    }
    if not caps:
        raise RunFailure(
            "EN-03 里没有一支 `enabled` 的执行工具 ⇒ 瞬态层没有上限可装，派发也就没有可占的槽；"
            "注册面请先跑 `persist_registrations`",
            code=AGENTS_MISSING,
        )
    return InProcessSlotManager(per_agent_max_parallel=caps, platform_limit=load_platform_limit())


def _seats(session, feature_request_id: uuid.UUID, task_id: str) -> tuple[str, str, str]:
    """三岗的**库里现值**（`(执行者, 审查者, 验收者)`）。

    从图上那份返回值里取会更短，但图上那份是"这一跳算出来的"，库里那份是"门禁写进去的"：
    FR-031 要能被复核的是后者。`AssignGuards` 若在 `flush` 之后被别的写者改了列，这里就读得出来。
    """
    row = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one()
    )
    return str(row.executor_agent_id), str(row.reviewer_agent_id), str(row.acceptor_agent_id)


@dataclass(frozen=True)
class AssignmentReport:
    """指派段的读数。`held` 与 `usage` 是同一件事的两面：台账里有几张待退的凭据、瞬态层就占着几格。

    这两个数**必须一起读**：只看 `held` 看不出瞬态层是不是真的占着（`put` 了但 `acquire` 失败会两样都空），
    只看 `usage` 看不出占用有没有对得上一张卡号。差额不为零就是装配缺陷，判据把它钉成一条关系式。

    `deferred` 是 `max_cards_per_round` 生效时**本轮没试着派发**的那些 ready 卡号。它必须存在：
    限了每轮张数而不报出剩下的，调用方就看不出"还有活"与"活干完了"的差别 —— 那正是一次静默丢弃。
    """

    feature_request_id: uuid.UUID
    dispatched: tuple[tuple[str, str, str, str], ...]
    blocked: tuple[tuple[str, str], ...]
    deferred: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    held: tuple[str, ...]
    usage: tuple[tuple[str, int], ...]


def _round_slice(ready: Sequence[Any], max_cards_per_round: int | None) -> tuple[Any, ...]:
    """按 `max_cards_per_round` 切出这一轮要试着派发的卡；`None` = 不限（只给测试与向后兼容）。

    抽成一个小函数不是为了好看：这一格的真形是"拿全部"与"拿前 N 张"，而"拿全部"那一支在生产里
    必须永远走不到（见 `run_chain` 与 `Q10` 的台账）。纯函数 ⇒ 判据能在不连库的档位钉它。
    上限为 0 或负数是**调用方的决定错了**，不是"这轮不干活"：后者由 `deferred` 报出来，前者必须拒。
    """
    if max_cards_per_round is None:
        return tuple(ready)
    if max_cards_per_round <= 0:
        raise RunFailure(
            f"`max_cards_per_round` 读到的 {max_cards_per_round!r} 不是一张卡都不是的意思 ⇒ "
            f"要停就别调指派段；给 0 会让 {len(ready)} 张 ready 卡原地消失（FR-025 不许静默丢弃）",
            code=ROUND_LIMIT_INVALID,
        )
    return tuple(ready[:max_cards_per_round])


def run_assign(
    feature_request_id: uuid.UUID,
    *,
    transport: str,
    session,
    now: dt.datetime,
    slots: Any | None = None,
    leases: scheduler.LeaseLedger | None = None,
    max_cards_per_round: int | None = None,
    review_mode: str = review_policy.REVIEW_MODE_MANUAL,
) -> AssignmentReport:
    """把这张需求单里**过了就绪判定**的卡逐张推过 `SelectWorker → AssignGuards → Dispatch`。

    五条顺序各有理由，换了就有一种错会溜过去：
    ⓪ 门先说话（与受理段、恢复段同一条纪律）。
    ① 需求单要在库里 ⇒ `feature_request_missing`，而不是让图去报"没有卡"。
    ② **就绪判定在这里做一次，然后只跑 `ready` 那几张**。`Dispatch` 节点里自己还会再判一次
       （`dispatch.py:182-188`），这一遍不是重复劳动而是换载体：装配点若把四张 `READY` 卡无脑喂进图，
       第二张起得到的是 `DispatchMisuse(dependencies_unmet)` 异常，而 FR-025 要的是"哪几张被谁挡着"
       这句可读的话 —— 所以 `blocked` 从 `readiness()` 的读数里原样带出来，一条都不丢。
    ③ 槽与台账是**参数**，函数不自己退槽：一张卡的槽从借到还中间必须隔着执行段（`scheduler.run_card`），
       退槽归它的 `finally`（owner 2026-10-16 裁 甲）。这一发把 `held` 原样交回调用方。
    ④ `max_cards_per_round`：**每轮最多尝试派发几张卡；容量满时下一轮再试，不在本轮阻塞**。
       （Q10，owner 2026-10-17 裁甲。）这一格的来历是一次真挂死：四张卡同时 ready、三工具都把这四张
       排到 `qoder` 第一，而 `qoder` 的 `max_parallel` 是 1 —— 第二轮的 `acquire()` 走进
       `slot_manager.py:129-135` 那个 `condition.wait()`，等的是**同一进程里永远不会有人还的那格槽**
       （还槽在执行段的 `finally`，而执行段要等指派段返回才走得到）。不是死锁在库上（`pg_stat_activity`
       当时没有等待事件），是死等在线程条件上。限每轮张数把"等容量"变成"下一轮再看"。
       `None` 只给测试与向后兼容 ⇒ 生产驱动 MUST 显式传值（`run_chain` 传 1，判据钉的是那条）。

    **这一发今天没有命令行入口**（有意）：单跑指派段会把卡留在 `RUNNING`，而台账里那张凭据随进程消失
    ⇒ 下一发既没人知道这格槽还在不在，`dispatch()` 又不接受 `RUNNING` 的卡。CLI 那一面与执行段同笔落。
    """
    resolve_transport_flag(transport)
    request = session.get(FeatureRequest, feature_request_id)
    if request is None:
        raise RunFailure(
            f"EN-01 里没有这条需求单 ⇒ 无现场可指派：feature_request_id={feature_request_id}",
            code=REQUEST_MISSING,
        )
    cards = dispatch_node.cards_of(session, feature_request_id)
    if not cards:
        raise RunFailure(
            f"EN-02 里没有这张需求单的卡 ⇒ 指派段没有对象：feature_request_id={feature_request_id}",
            code=CARDS_MISSING,
        )
    verdict = dispatch_node.readiness(cards)
    if not verdict.ready:
        raise RunFailure(
            f"一张可派发的卡都没有 ⇒ 指派段不许空跑，阻塞读数为 {list(verdict.blocked)}"
            f"（先核卡态与依赖；这条不是「没人干活」，是「活还不能干」）："
            f"feature_request_id={feature_request_id}",
            code=NOTHING_DISPATCHABLE,
        )
    slot_layer = slots if slots is not None else _slots_from_registry(session)
    ledger = leases if leases is not None else scheduler.LeaseLedger()
    graph = orchestrate_graph.build_assignment_graph(
        orchestrate_graph.AssignmentDeps(
            session=session,
            created_at=now,
            actor_agent_id=ACTOR_AGENT_ID,
            slots=slot_layer,
            leases=ledger,
            review_mode=review_mode,
        )
    )
    dispatched: list[tuple[str, str, str, str]] = []
    picked = _round_slice(verdict.ready, max_cards_per_round)
    for need in picked:
        graph.invoke({"feature_request_id": feature_request_id, "task_id": need.card_id})
        dispatched.append((need.card_id, *_seats(session, feature_request_id, need.card_id)))
    tried = {item[0] for item in dispatched}
    deferred = tuple(need.card_id for need in verdict.ready if need.card_id not in tried)

    return AssignmentReport(
        feature_request_id=feature_request_id,
        dispatched=tuple(dispatched),
        blocked=verdict.blocked,
        deferred=deferred,
        statuses=tuple((card.card_id, card.status) for card in dispatch_node.cards_of(session, feature_request_id)),
        held=ledger.pending(),
        usage=tuple(sorted(slot_layer.usage().items())),
    )


# ———————————————————————————————————— 执行段（第 2 步下半段第 3 项 · 乙：实体节点体 + 真回收）

@dataclass(frozen=True)
class ExecutionReport:
    """一张卡"段跑完、槽退干净"之后的读数。

    `usage_after` 与 `pending_after` 必须一起读，口径同 `AssignmentReport` 那两个数：前者是瞬态层的
    全量占用，后者是台账里还欠着的凭据。**收尾成功时两者都该是空 / 全零**，而"该是空"这件事
    不是判据、是 `scheduler.run_card()` 的 `finally` 里那句 `SlotLeak` 哨兵（摘掉它这两格就一起骗人）。

    `card_status_after` / `verdict_rows` / `verdicts` / `handoff_count_after` / `seats_after` 这几格
    **一律从库里读现值**，不读图上那份返回值：图 state 是"这一跳算出来的"，库里那份才是门禁与结论写进去的
    （口径同 `_seats()`，也同 §十二 那条"权威值在库里"）。把它们改成从 state 抄，就等于给结论造第二个载体。

    `verdicts` 是一串而不是"最后一条"：EN-08 那张表**没有 `created_at` 列**（`models_approval.py:77-86`），
    所以"哪一条是这一次写的"在库里问不出来 —— 一次运行至多写一条，返工轮次多了就是一串。把串里的某一条
    挑出来当"本轮结论"要按 id 或按插入顺序猜，那是装配点在替库里造一个不存在的事实。
    """

    feature_request_id: uuid.UUID
    card_id: str
    agent_id: str
    validated_status: str | None
    artifact_count: int
    card_status_after: str
    verdict_rows: int
    verdicts: tuple[str, ...]
    handoff_count_after: int
    seats_after: tuple[str, str, str]
    usage_after: tuple[tuple[str, int], ...]
    pending_after: tuple[str, ...]


def _request_row(session, feature_request_id: uuid.UUID) -> FeatureRequest:
    row = session.get(FeatureRequest, feature_request_id)
    if row is None:
        raise RunFailure(
            f"EN-01 里没有这条需求单 ⇒ 执行段没有现场：feature_request_id={feature_request_id}",
            code=REQUEST_MISSING,
        )
    return row


#: 审查结论的来源形状（Q6 甲）。装配面只认这个协议，不认任何具体实现的名字。
FindingsFor = review_node.FindingProvider


def _card_row(session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    """这张卡的 ORM 行（同一个会话里读到的是同一个对象，改状态与 `record_verdict` 看到的是同一份）。"""
    row = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if row is None:
        raise RunFailure(
            f"EN-02 里没有这张卡 ⇒ 执行段没有对象：feature_request_id={feature_request_id} task_id={task_id}",
            code=CARDS_MISSING,
        )
    return row


@dataclass(frozen=True)
class CardFace:
    """任务卡可见面的三格，加上 `_task_of` 还要按原样下发的那三列（一条都不重新解释）。

    `inputs` 保持一条一个元素 —— "渲染成一行"是各一面自己的事（下发面给列表，卡文件给一行）。
    """

    title: str
    objective: str
    inputs: tuple[str, ...]
    expected_output: str
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


def _card_face_of(session, feature_request_id: uuid.UUID, row: TaskCard) -> CardFace:
    """把一张卡的 EN-02 现值投影成**可见面三行**（契约 §3 的 `目标 / 输入 / 期望产物`）—— 只有这一处投。

    为什么要有这个名字：下发载体（`Task.objective / inputs / expected_output`）与公共区那张
    `tasks/<卡号>-<slug>.md` 吃的必须是**同一份三行**。原先这两处各拼一次，分隔符或取列一旦在哪一侧改动，
    Agent 当时看到的任务与事后能查到的任务就不是同一件事（原则 I：一件事实一个载体），而这条漂移
    在测试里表现为"卡文件能解析、但与下发内容不同" —— 读侧不会炸，只有复核时会发现。

    三格各自怎么取、为什么只能这样取（三条都在本函数，`_task_of` 只指过来不复述）：

    - `objective`：取卡标题。EN-02 没有 `objective` 列，而 `Task` 与卡文件都要求它非空；取标题是
      **现有列的投影**，比现编一句"做一个页面"诚实（后者是装配点替总管编需求）。
    - `inputs`：取依赖卡的标题（按 `depends_on` 的顺序）。同样是投影：库里能查到的"这张卡吃进去什么"
      只有它的依赖。
    - `expected_output`：取这张卡 `acceptance_criteria` 的原样拼接。**这一格只搬运，不解释** ——
      "哪几个 token 算交付物"的唯一载体是 `adapters/stub.py::expected_files()`，这里再写一份正则就是
      原则 I 违例（两份解释一定会漂，而漂了的表现是"仿真侧交付了卡没点名的文件"）。
    """
    titles = {
        str(card.id): str(card.title)
        for card in session.query(TaskCard).filter(TaskCard.feature_request_id == feature_request_id).all()
    }
    depends = [str(item) for item in (row.depends_on or ())]
    criteria = [str(item) for item in (row.acceptance_criteria or ())]
    return CardFace(
        title=str(row.title),
        objective=str(row.title),
        inputs=tuple(titles.get(item, item) for item in depends),
        expected_output="；".join(criteria),
        depends_on=tuple(depends),
        acceptance_criteria=tuple(criteria),
    )


def _task_of(
    session, feature_request_id: uuid.UUID, task_id: str, *, project_key: str, executor_role_id: str
) -> Task:
    """把 EN-01 / EN-02 的现值**投影**成下发载体 —— 一件事实都不新造。

    `Task` 有而 EN-02 没有的那三格（`objective` / `inputs` / `expected_output`）**不在这里各拼一次**，
    全走 `_card_face_of()` —— 公共区那张 `tasks/<卡号>.md` 走的是同一个函数，两处看到的就是同一份三行。
    每格为什么那样取的三条理由，记在 `_card_face_of()` 的说明里（那里是唯一的口径载体，本段不重述）。

    登记为残留：这三格真正的载体应当是 EN-02 的三列（`objective` / `inputs` / `expected_output`），
    那是一次迁移，属 owner 的硬停项 ⇒ 本轮用投影 + 本段说明，不擅自加列（`evidence/phase-12.md §十四`）。
    """
    row = _card_row(session, feature_request_id, task_id)
    face = _card_face_of(session, feature_request_id, row)
    # 岗位约束走 `constraints` 这一格（契约 §1 已有的字段），不是塞进 `expected_output`：
    # 后者是**判据**的载体，门禁与审查都逐条比对它，混进岗位说明会让"这一条算不算判据"变成
    # 一个要靠读文本才能回答的问题。`constraints` 本来就是"给执行者的附加约束"。
    brief = role_brief_for_card(row)
    return Task(
        task_id=str(row.id),
        title=face.title,
        difficulty=int(row.difficulty),
        skill_tags=[str(item) for item in (row.skill_tags or ())],
        depends_on=list(face.depends_on),
        project_key=project_key,
        assignee={"agent_id": str(row.executor_agent_id), "role_id": executor_role_id},
        objective=face.objective,
        inputs=list(face.inputs),
        expected_output=face.expected_output,
        artifact_floor=ArtifactFloor(
            min_bytes=int(row.artifact_floor_min_bytes),
            min_lines=int(row.artifact_floor_min_lines),
            format=str(row.artifact_floor_format),
        ),
        acceptance_criteria=list(face.acceptance_criteria),
        token_budget=row.token_budget,
        deadline_minutes=row.deadline_minutes,
        constraints=[brief] if brief else [],
    )


def _gateway_for(bus_root: Path, project_key: str) -> BusGateway:
    """总线网关：根 = `archive.archive_root()` 算出来的那一个 —— `run.py` 自己不拼项目根。

    owner 2026-10-17 裁决 2 的合并：这里原本另养着一份私有拼法，与 `archive.root.archive_root()`
    是同一件事的两个承载点。两份口径迟早分叉，第一现场就是交付段差 `project_key` 那一层
    （`assemble.py:344` 那句"入口在总线根目录下不存在"把本仓第一次真跑拦下的）。合并后
    "外层根套一层项目名"在 `run.py` 里为 0 份，`project_key` 的形态校验也只剩 `layout.project_dir()` 一处。
    """
    return BusGateway(archive.archive_root(project_key, bus_root=bus_root))


def _review_node_body(
    session,
    gateway: BusGateway,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    now: dt.datetime,
    findings_for: FindingsFor | None,
):
    """`Review` 节点体：只读岗读正文 → 组可见面 → 逐条比对 → 收结论。

    顺序里三处是要命的：

    ① **用审查席那个身份去读，而不是平台身份**。这一格今天**不是**隔离强制点：`gateway.read()` 的
       docstring 明写"读不设跨 workspace 墙（FR-013），`actor` 目前在本方法里不参与判定"。仍然取
       `actor_for_seat(seat="reviewer")` 是为了两件事：把"谁读的"这件事留在链路上可复核（写平台身份就变成
       平台自己读了），以及读层哪天加上按角色限读，这一发不必改形状。**不把它写成"只读隔离已由这一格保证"** ——
       只读隔离的强制点在 `acl.can_write` 与 `guard/triple.py`，不在这里。
    ② **只读本轮那一批产物**。`state["artifact_ids"]` 是 `ResultIngest` 本次写进去的 EN-06 主键，
       而 `bus/actor.py::artifact_paths_for_review()` 给的是"这张卡的全部产物"——换人重试时两者不等价，
       用后者会让上一轮交付被这一轮审掉（`validate_artifact.py:165-167` 同一条理由）。
    ③ **`RUNNING → REVIEWING` 由本节点写**。`validate_artifact.py:27` 明写"进 `REVIEWING` 是 `Review`
       节点的动作"，而 `artifact.validated_status = valid` 这个唯一前件由图上的条件边保证（不达标那一支
       已经被路由送去 `HandOff`）。在这里再判一次 `validated_status` 就是给状态机造第二个载体。

    `findings_for` 是**注入的结论来源**（owner 2026-10-17 裁 Q6 = 甲）：`TaskResult` 里没有逐条判定的载体
    （`protocol/messages.py:101-107` 七字段，`adapters/stub.py:13` 自己点名这一格），所以结论必须由
    `review.FindingProvider` 那一侧交进来。今天唯一的实现是本文件的 `StubFindingProvider`，它固定交回
    `met=True` ⇒ **不构成 FR-029 的实证**（那句话写进它的 docstring，也写进每条 `evidence` 串里）。
    没有来源 ⇒ 在本节点第一次动作之前就 `findings_source_missing` 停下，**不写状态、不落库** ——
    反过来写（先推到 `REVIEWING` 再报错）会让这张卡停在谁也接不上的中间态。
    """

    def node(state: dict) -> dict:
        card = _card_row(session, feature_request_id, task_id)
        if findings_for is None:
            raise RunFailure(
                f"卡 {task_id} 的审查结论没有来源 ⇒ 仿真侧不产 `ReviewFinding`（owner 2026-10-17 裁 Q6 = 甲，"
                "丙 登记为下一轮），本节点不许替审查岗编判定",
                code=FINDINGS_MISSING,
            )
        reviewer = str(card.reviewer_agent_id)
        reviewer_actor = bus_actor.actor_for_seat(
            session, feature_request_id=feature_request_id, task_id=task_id, seat="reviewer"
        )
        ids = list(state.get("artifact_ids") or ())
        paths = sorted(
            str(row.rel_path)
            for row in session.query(Artifact).filter(Artifact.id.in_(ids)).all()
        )
        bodies = tuple((path, gateway.read(reviewer_actor, path)) for path in paths)
        request = review_node.build_request(card, bodies, reviewer_agent_id=reviewer)
        findings = tuple(findings_for.findings_for(card, request.acceptance_criteria))
        verdict, reasons = review_node.check_findings(card, findings)
        if verdict is None:
            raise RunFailure(
                f"卡 {task_id} 的审查结论不入库：{'；'.join(reasons)}", code="findings_not_compliant"
            )
        card.status = review_node.REVIEWING
        review_node.record_verdict(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            reviewer_agent_id=reviewer,
            findings=findings,
            created_at=now,
        )
        return {}

    return node


def _seat_safe_candidates(session, feature_request_id: uuid.UUID, task_id: str, candidates) -> list:
    """把"换上就撞席位"的候选从候选集里剔掉 —— 判定本体仍是 `guard/triple.py`，装配点只是去问它。

    为什么这一句要存在（owner 2026-10-17 的 Q7 没答，按 甲 落地并披露，撤销路径 = 删本函数与那一行调用）：
    FR-031"三岗两两不同"今天只有两个断言点 —— `assign_guards.py:306`（下发前）与 `api/tasks.py:405`（覆盖路径），
    而状态机 `ARTIFACT_INVALID → HANDED_OFF → RUNNING`（`data-model.md:342`）**不过 `AssignGuards`**；
    `handoff()` 的出局集合来自 `agent_history()`，唯一载体是 EN-14 的 `SELECTION` 行（`handoff.py:92-108`）
    —— 那是"谁执行过"，不含席位。探针实测（`evidence/phase-12.md` §十四 发 3）：内置 3 支注册面下换人把
    执行岗换成了**这张卡的审查岗**，`seats_after=('doubao','doubao','workbuddy')` ⇒ FR-031 在一条可达路径上被违反。

    这里**不新写判定**：构造一份"候选占执行岗"的 `gp.Assignment`，问 `gp.distinctness_violations()` ——
    T071 覆盖路径用的同一个入口，它的 docstring 明写这条入口管的就是"改派时不许撞人"。
    撞 ⇒ 少递一个候选；不撞 ⇒ 原样交给 `handoff()`。装配点管"把哪些人递进去"，"谁与谁同人"仍只有那一份。

    两格代价如实记着，不许在台账里说成"换人已跑通"：
    ① 内置注册面恰 3 支、三岗又被这 3 支占满 ⇒ 合法候选恒为空 ⇒ 这一发只能进 `ESCALATED`
       （FR-025 的人工出口）。本轮因此**没有**"换人成功"的读数，`HANDED_OFF` 那一格仍未在真链上出现过。
    ② **因撞岗被剔掉的候选由本装配点自己落一条 EN-14 留痕**（owner 2026-10-17 裁 Q7 补的第 2 条："原因不许只
       写成'换人失败'"）。`handoff()` 的 `exclusions` 由它自己按在场者拼、入参只有候选集，所以它那句
       "没有可替代的工具可换"里**天然看不见撞岗** ⇒ 剔除这一步由做它的 actor 记一行（`action=seat_blocked_candidates`，
       逐支带上 `gp.distinctness_violations` 的原文理由，不是我另写一句）。**同一份 `blocked` 还作为 `seat_blocked`
       递给 `handoff()`**（owner 2026-10-17 裁 Q7 乙，授权动 `handoff.py`）⇒ `ESCALATED` 那一行自己的 `reason`
       现在以"席位占用导致合法候选为空"开头并带上原文理由；措辞拼接在实体那一侧，判定仍只在 `triple.py` 一处。

    返回 `(合法候选, 被席位挡掉的支与其原文理由)` —— 第二个值不是日志副产品，它是留痕的输入。
    """
    card = _card_row(session, feature_request_id, task_id)
    guards = {
        "reviewer": gp.SeatOccupant(card.reviewer_agent_id, None),
        "acceptor": gp.SeatOccupant(card.acceptor_agent_id, None),
    }
    safe = []
    blocked = []
    for candidate in candidates:
        trial = gp.Assignment(
            card_id=task_id,
            occupants={**guards, "executor": gp.SeatOccupant(str(candidate.agent_id), None)},
        )
        reasons = gp.distinctness_violations(trial)
        if reasons:
            blocked.append((str(candidate.agent_id), reasons))
        else:
            safe.append(candidate)
    return safe, tuple(blocked)


def _handoff_node_body(
    session, *, feature_request_id: uuid.UUID, task_id: str, now: dt.datetime
):
    """`HandOff` 节点体：候选集从 EN-03 现值重取，判定与上限全交回 `handoff.handoff()`。

    两条不重复实现的口径：
    ① **候选集只有一个载体** = `select_worker.candidates_from_registry()`（图上的 `SelectWorker` 读的也是它）。
       在装配点自己拼候选集，就会有两份"谁在册、谁停用"，而 `handoff()` 里那句"排除在场者"读的是入参，
       两份入参漂了的表现是换回一个已经失败过的人。
    ② **理由不许现编**。`reason` 取 `state["invalid_reason"]`（门禁写在 EN-06 上的那句）原样递进去；
       为空就让它撞 `handoff()` 自己的 `reason_required`。装配点写一个 `or "产物不达标"` 的兜底，
       留痕里就会出现一句没人判定过的原因 —— FR-026 要的那句"为什么换"就废了。
    ③ **递进去的候选不许撞席位**：见 `_seat_safe_candidates()`（Q7 按 甲 落地，两格代价写在那里）。
       候选集载体仍只有一个（`candidates_from_registry`），这一句只是"少给"，不是"另判一次"。
    ④ **被剔掉的支必须留下名字与理由**（owner 2026-10-17 裁 Q7 补第 2 条）。留痕走 `events.record` 那唯一的
       写入者，`kind` 沿用 `handoff` 已经在用的 `"TASK"`，只是 `action` 换了名字 ⇒ 读的人在同一条卡的时间线上
       先看到"哪两支因为占着审查/验收岗被剔除"，再看到 `handoff()` 自己那句判定。这里**不新写判定语句**：
       理由就是 `gp.distinctness_violations` 返回的那几句原文。
    """

    def node(state: dict) -> dict:
        candidates, _dropped = sw.candidates_from_registry(session)
        safe, blocked = _seat_safe_candidates(session, feature_request_id, task_id, candidates)
        if blocked:
            events.record(
                session,
                entity="task_card",
                entity_id=task_id,
                kind="TASK",
                actor_agent_id=ACTOR_AGENT_ID,
                payload={
                    "node": handoff_node.NODE,
                    "action": SEAT_BLOCKED_ACTION,
                    "blocked": [
                        {"agent_id": agent_id, "reasons": list(reasons)} for agent_id, reasons in blocked
                    ],
                },
                created_at=now,
            )
        handoff_node.handoff(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            reason=str(state.get("invalid_reason") or ""),
            candidates=safe,
            created_at=now,
            seat_blocked=blocked,
        )
        return {}

    return node


def run_execute(
    feature_request_id: uuid.UUID,
    task_id: str,
    *,
    transport: str,
    bus_root: Path,
    session,
    now: dt.datetime,
    findings_for: FindingsFor | None = None,
    slots,
    leases: scheduler.LeaseLedger,
) -> ExecutionReport:
    """把这张**已经占着槽**的卡跑完整条执行段：组装 → 下发 → 回收 → 落库 → 门禁 → 出边，并且**无论如何**把槽退回去。

    四条顺序各有理由，换了就有一种错会溜过去：
    ① **台账里没有凭据时，一次库都不碰**。所以三岗读数、卡→`Task` 投影、卡级图编译全都放在闭包里，
       由 `run_card()` 领到凭据之后才第一次执行 —— 不是在函数开头。这与受理段/恢复段/指派段"门先说话"
       是同一条纪律。
    ② **退槽不在这里**。`scheduler.run_card()` 的 `finally` 是唯一的收尾点（owner 2026-10-16 裁 甲），
       本函数一次都不碰 `release()` / `acquire()` —— 装配点自己退一次，就会出现"段抛了而两边都退"或
       "两边都以为对方退了"。也正因如此，`submit()` / `collect()` 这一步必须交给 `run_card(produce=…)`
       在借与还**之间**去跑：把它挪到 `run_card()` 之前，抛错那一发就没人退槽（2026-10-17 乙 轮换签名的原因）。
    ③ **卡态必须 `RUNNING`，而这条不在本函数检查**：`ResultIngest` 自己拒 `task_not_running`
       （`orchestrate/nodes/result_ingest.py:110-113`），"指派段先跑、执行段后跑"这个顺序是被段内的
       拒绝钉住的；在这里再写一个 `if card.status != ...` 就是状态机的第二个载体。
    ④ **运输方式必须显式**：`resolve_transport_flag(transport)` 在碰任何东西之前先说话（门的本体在
       `adapters/stub.py`），今天这一发只有仿真总管一支可用 —— 真调厂商 API 不在本轮范围（owner 纪律）。

    `findings_for` 是审查结论的来源（Q6 甲：注入；缺 ⇒ `findings_source_missing`）。**命令行入口仍不开**：
    没有真实的审查结论来源就开入口，等于允许"审查岗没说话而卡进了 `ACCEPTING`"这种读数出现在 R-07 的验收里。
    """
    resolve_transport_flag(transport)

    def produce() -> Any:
        executor, _reviewer, _acceptor = _seats(session, feature_request_id, task_id)
        project_key = str(_request_row(session, feature_request_id).project_key)
        role_id = str(
            bus_actor.actor_for_seat(
                session, feature_request_id=feature_request_id, task_id=task_id, seat="executor"
            ).role_id
        )
        task = _task_of(
            session, feature_request_id, task_id, project_key=project_key, executor_role_id=role_id
        )
        adapter = StubAdapter(
            agent_id=executor, gateway=_gateway_for(bus_root, project_key), clock=lambda: now
        )
        handle = adapter.submit(task)
        return adapter.collect(handle)

    def segment(state: dict) -> Any:
        executor, _reviewer, _acceptor = _seats(session, feature_request_id, task_id)
        gateway = _gateway_for(bus_root, str(_request_row(session, feature_request_id).project_key))
        graph = orchestrate_graph.build_execution_graph(
            orchestrate_graph.ExecutionDeps(
                session=session,
                created_at=now,
                actor_agent_id=executor,
                bus_root=gateway.root,
            ),
            review=_review_node_body(
                session,
                gateway,
                feature_request_id=feature_request_id,
                task_id=task_id,
                now=now,
                findings_for=findings_for,
            ),
            handoff=_handoff_node_body(
                session, feature_request_id=feature_request_id, task_id=task_id, now=now
            ),
        )
        return graph.invoke(state)

    try:
        outcome = scheduler.run_card(
            feature_request_id=feature_request_id,
            task_id=task_id,
            slots=slots,
            leases=leases,
            produce=produce,
            segment=segment,
        )
    except scheduler.MissingLease as exc:
        raise RunFailure(
            f"卡 {task_id} 在台账里没有凭据 ⇒ 指派段没跑过（或跑在别的进程里），"
            f"执行段不许在没有占槽的情况下运行：feature_request_id={feature_request_id}",
            code=LEASE_MISSING,
        ) from exc
    state = outcome.segment_result or {}
    card = _card_row(session, feature_request_id, task_id)
    verdict_rows = list(
        session.query(ReviewVerdict)
        .filter(ReviewVerdict.feature_request_id == feature_request_id, ReviewVerdict.task_card_id == task_id)
        .all()
    )
    return ExecutionReport(
        feature_request_id=outcome.feature_request_id,
        card_id=outcome.card_id,
        agent_id=outcome.agent_id,
        validated_status=state.get("validated_status"),
        artifact_count=len(state.get("artifact_ids") or ()),
        card_status_after=str(card.status),
        verdict_rows=len(verdict_rows),
        verdicts=tuple(sorted(str(row.verdict) for row in verdict_rows)),
        handoff_count_after=int(card.handoff_count),
        seats_after=_seats(session, feature_request_id, task_id),
        usage_after=tuple(sorted(outcome.usage_after.items())),
        pending_after=leases.pending(),
    )


def _acceptance_audit(session, task_id: str) -> dict[str, Any]:
    """这张卡**最后一轮**验收的 EN-14 载荷（取不到就是空字典，由调用方决定怎么对待空）。

    为什么按 `EventLog.id` 取最后一条而不是按 `payload["round_no"]` 排序：`id` 是插入序（自增主键），
    而 `round_no` 是那一轮自己写的数 —— 用后者排序等于让被检对象给自己排座次。两条都是同一张卡上的行，
    取最后一条就是"最近一次落进库里的结论"。

    抽出来之前这一段是 `run_accept` 里的内联循环；`open_issues_of` 要读同一份载荷，
    留下两份循环就会有一份在"载荷键名改了"之后仍然返回旧形状。
    """
    audit: dict[str, Any] = {}
    for event in (
        session.query(EventLog)
        .filter(EventLog.entity == "task_card", EventLog.entity_id == task_id)
        .order_by(EventLog.id)
        .all()
    ):
        payload = event.payload or {}
        if payload.get("node") == accept_node.NODE and payload.get("action") == accept_node.ACTION_ROUND:
            audit = payload
    return audit


@dataclass(frozen=True)
class AcceptanceReport:
    """一张卡过完验收段之后的读数（除 `proxy_mark` 外**全部从库里读**，口径同 `ExecutionReport`）。

    `round_no` 取卡上的 `review_round` 现值、`met` / `unmet` / `limit` 取实体自己那条
    `action=acceptance_round` 的 EN-14 载荷 —— 不读 `AcceptanceOutcome`：那个返回值是"这一跳算出来的"，
    库里那两行才是收进结论之后的事实（原则 I）。把它们改成从返回值抄，等于给结论造第二个载体。

    `proxy_mark` 是**这一发的身份**，不是库里的新事实：它记的是"这次交判定的是哪一支来源"（仿真来源
    声明 `MARK`，真实来源不声明 ⇒ 空串），判据用它来分"该有代填留痕"与"不该有"两种形状。
    """

    feature_request_id: uuid.UUID
    card_id: str
    acceptor_agent_id: str
    items: tuple[str, ...]
    round_no: int
    met: int
    unmet: int
    limit: int
    card_status_after: str
    proxy_mark: str


def run_accept(
    feature_request_id: uuid.UUID,
    task_id: str,
    *,
    transport: str,
    session,
    now: dt.datetime,
    accept_for: AcceptanceProvider | None = None,
) -> AcceptanceReport:
    """把这张**已进 `ACCEPTING`** 的卡推过验收段：条目取 EN-07，判定由注入的来源交，结论交回 `accept()`。

    四条顺序里两处是要命的：

    ① **没有结论来源就在第一个动作之前停**（owner 2026-10-17 裁 Q8 = 甲）。先推再报错会让这张卡的
       `review_round` 与状态停在谁也接不上的位置；`acceptance_source_missing` 那一发库里一行都不该多。
    ② **条目集合只从实体层拿**（`accept_node.expected_items()` 读 EN-07，空表它自己抛 `map_empty`）。
       装配点在这里列条目 = 原则 III 禁的"验收方自拟口径"，也 = 给映射表造第二个载体；
       完整性同理仍由实体层的 `check_items()` 双向差集判 —— 本函数不数条目对不对，只搬运来源交的东西。
    ③ **状态、轮次、留痕全在 `accept()` 里**，本函数一次都不写 `card.status`（状态机只有一个载体），
       也不自己算轮次。验收岗身份取卡上那一列并交给 `accept()`，那句"不是同人就不收"
       （`accept.py:168-173`）照旧是强制点 —— 代填的是**判定**，不是**身份**。
    ④ 代填留痕只在来源自己声明 `MARK` 时才写：真实来源没这个属性 ⇒ 不写 ⇒ 库里那行"代填"出现的时候
       就是真的代填过的时候（owner 裁 Q8 的补条："不许静默代填"）。

    `actor_for_seat(seat="acceptor")` 这一发与审查段同一格：**不是隔离强制点**（`gateway.read()` 里 actor
    不参与判定），它的作用是把"以哪个席位的名义验收"留在链路上可复核。
    """
    resolve_transport_flag(transport)
    if accept_for is None:
        raise RunFailure(
            f"卡 {task_id} 的验收结论没有来源 ⇒ 仿真侧不交 `ItemVerdict`（owner 2026-10-17 裁 Q8 = 甲），"
            "本函数不许替验收岗编判定",
            code=ACCEPTANCE_MISSING,
        )
    card = _card_row(session, feature_request_id, task_id)
    acceptor = str(card.acceptor_agent_id)
    bus_actor.actor_for_seat(session, feature_request_id=feature_request_id, task_id=task_id, seat="acceptor")
    items = accept_node.expected_items(session, feature_request_id, task_id)
    verdicts = tuple(accept_for.verdicts_for(card, items))
    mark = str(getattr(accept_for, "MARK", ""))
    if mark:
        payload = {
            "node": accept_node.NODE,
            "action": ACCEPT_PROXY_ACTION,
            "provider_mark": mark,
            "items": [str(verdict.acceptance_item) for verdict in verdicts],
        }
        # 自动审查那一支的验收来源要能在留痕里与"人点通过"分开（owner 2026-10-23 裁决）。
        # 人工来源没有 `AGENT_ID` ⇒ 这一格不写，避免把人类审批伪装成机器身份。
        source_agent = str(getattr(accept_for, "AGENT_ID", "") or "")
        if source_agent:
            payload["agent_id"] = source_agent
        events.record(
            session,
            entity="task_card",
            entity_id=task_id,
            kind="TASK",
            actor_agent_id=ACTOR_AGENT_ID,
            payload=payload,
            created_at=now,
        )
    accept_node.accept(
        session,
        feature_request_id=feature_request_id,
        task_id=task_id,
        verdicts=verdicts,
        created_at=now,
        actor_agent_id=acceptor,
    )

    row = _card_row(session, feature_request_id, task_id)
    audit = _acceptance_audit(session, task_id)
    return AcceptanceReport(
        feature_request_id=feature_request_id,
        card_id=task_id,
        acceptor_agent_id=acceptor,
        items=tuple(str(item) for item in items),
        round_no=int(row.review_round),
        met=int(audit.get("met", 0)),
        unmet=int(audit.get("unmet", 0)),
        limit=int(audit.get("limit", 0)),
        card_status_after=str(row.status),
        proxy_mark=mark,
    )


# ———————————————————————————————————— 交付包段

#: R-07 那个"可直接预览的入口页"叫什么。**这个名字不在仓里任何 schema 上** ——
#: `data-model.md:274` 只说 EN-17 的 `entry_path` 存"相对路径（SC-004 三条判据的读取起点）"，
#: 叫什么由需求定。owner 2026-10-17 裁 Q9：钉 4 个文件（3 HTML + 1 CSS），入口 = `index.html`，
#: 不新增第 5 份。所以这个名字是**调用方的决定**：`run_deliver(entry_name=…)` 必填、没有默认值，
#: 装配点只按它去 manifest 里选，选中 0 条或 2 条都停下，不猜。
DEFAULT_ENTRY_NAME = "index.html"
ENTRY_MISSING = "entry_absent"
ENTRY_AMBIGUOUS = "entry_ambiguous"
ENTRY_NAME_EMPTY = "entry_name_empty"


def _entry_path_of(manifest, entry_name: str, feature_request_id: uuid.UUID) -> str:
    """在 `validated_manifest()` 的结果里按文件名选入口 —— **选**，不是**判**。

    manifest 那份事实的唯一载体是实体（`assemble.validated_manifest` 读 EN-06 的 valid 行），
    这里不加第二条过滤规则：只按 basename 等于调用方点名的入口页。两种落空都是拒绝而不是猜：
    0 条 ⇒ `entry_absent`（交付包不能指向一个本批没交的东西）；
    ≥2 条 ⇒ `entry_ambiguous`（同一批里两份同名入口 = 交付包指向谁都行，那不是"唯一一份"）。
    """
    hits = sorted(
        str(entry.rel_path) for entry in manifest if PurePosixPath(str(entry.rel_path)).name == entry_name
    )
    if not hits:
        raise RunFailure(
            f"manifest 里没有名为 {entry_name!r} 的产物 ⇒ 交付包没有可预览的入口，装配点不替 R-07 挑一个"
            f"：feature_request_id={feature_request_id}，manifest={len(list(manifest))} 条",
            code=ENTRY_MISSING,
        )
    if len(hits) > 1:
        raise RunFailure(
            f"manifest 里有 {len(hits)} 份 {entry_name!r}（{hits}）⇒ 入口页不唯一，"
            "这是冲突裁决（T096）的输出，不是装配点可以任选的形状",
            code=ENTRY_AMBIGUOUS,
        )
    return hits[0]


@dataclass(frozen=True)
class DeliveryReport:
    """交付包落定之后的读数。`entry_path` / `manifest_count` 从 **EN-17 那行**读回，不读 `Package`；
    `package_bytes` 是从盘上量的（写没写成、写成多大，只有量过一次才算量过 —— 文档里写不算）。
    """

    feature_request_id: uuid.UUID
    deliverable_id: uuid.UUID
    entry_path: str
    manifest_count: int
    cards_delivered: int
    package_path: str
    package_bytes: int


#: 有卡带未达标项、而库里那份清单读不出来 ⇒ 拒，不静默交空清单。
#: 为什么不让 `assemble()` 的 `open_issues_required` 一起挡这一发：那一挡的语义是"清单为空 ⇒ 不许组包"
#: （判定面，唯一判点在实体侧），这里的语义是"清单本该在库里、却不在"（载体面：载荷形状不符、
#: 或那一轮不是 `accept()` 落的）。两种现场要修的东西不同，混成一个代号就分不出来。
OPEN_ISSUES_UNREADABLE = "open_issues_unreadable"


def open_issues_of(session, feature_request_id: uuid.UUID) -> tuple[dict[str, str], ...]:
    """未达标项清单的**读侧**：把处于 `DELIVERED_WITH_ISSUES` 的那些卡，各自那一轮验收存在 EN-14 里的清单原文收回来。

    为什么要有这一格（owner 2026-10-17 裁决 3 第 ① 项）：`run_deliver` 原来靠调用方把清单交进来，
    而"调用方手里那份清单"在库里没有对应物 ⇒ 同一次交付可以有两种说法（装配点交的与验收岗判的不一样），
    且 T125 的"四要素可查"与 T131 的"未达标项清单"都问不出数。现在清单只有一个来源：库。

    三格各有挡的错：
    ① 卡集合按 `TaskCard.id` 排序 —— 这一份会整条存进 EN-17，两张卡的清单互串顺序会被读成"内容变了"。
    ② 带问题的卡读不到自己的清单 ⇒ `open_issues_unreadable`，**不**降级成空清单：空清单到了
       `assemble()` 那一侧就是"没问题"，那正是 FR-035 禁的形状。
    ③ 这里**不判"该不该有清单"**（判点在 `assemble.open_issues_required`，唯一一处），只读。
    """
    rows = (
        session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status == accept_node.DELIVERED_WITH_ISSUES,
        )
        .order_by(TaskCard.id)
        .all()
    )
    issues: list[dict[str, str]] = []
    for row in rows:
        items = _acceptance_audit(session, str(row.id)).get(accept_node.OPEN_ISSUES_ITEMS) or ()
        if not items:
            raise RunFailure(
                f"卡 {row.id} 处于 {accept_node.DELIVERED_WITH_ISSUES}，而 EN-14 上那一轮验收的载荷里没有"
                f"{accept_node.OPEN_ISSUES_ITEMS!r} 那一格 ⇒ 清单没有可读的载体；"
                "宁可停下，也不把'有问题'交成一份空清单（FR-035）",
                code=OPEN_ISSUES_UNREADABLE,
            )
        issues.extend(dict(item) for item in items)
    return tuple(issues)


#: 需求单终态（`data-model.md §EN-01` 五档之一）。**不 import `realrun` 那一侧的常量**：
#: `realrun` 反向依赖本模块（它读 `run.ACTOR_AGENT_ID`），引过来就是环。两处字面量相同，
#: 判据 `tests/guards/test_stuck_card_recovery.py` 逐字比过一次。
REQUEST_DELIVERED = "DELIVERED"
REQUEST_STATUS_SYNCED = "request_status_synced"


def mark_request_delivered(
    session, feature_request_id: uuid.UUID, *, now: dt.datetime, deliverable_id: uuid.UUID
) -> str | None:
    """唯一一份交付包写成 ⇒ 需求单 `DELIVERED`；返回写进去的值，没变就是 `None`。

    为什么这一格必须补（2026-10-28 真实事故 `real_demo / 7800b3dd`）：跑卡循环里
    `realrun.sync_request_status` 只能在"每张卡刚跑完"那一刻对齐，而**最后一张卡几乎总是人工评审**
    才到终态（`review/service.py::_assemble_if_ready`），那条路径不经过跑卡循环。于是那一单的包
    已经组出来、`results/delivery_package.json` 都在盘上了，EN-01 还写着 `RUNNING` ——
    屏上"需求单状态"读的就是这一列，读者只能理解成"还在跑，等一会儿"。

    为什么落在组包点而不是评审点：`DELIVERED` 说的不是"卡都过了"，而是"这一单交出去了"。
    卡的终态由 `review`/`accept` 自己写；"交出去了"的唯一事实是 EN-17 那一行，它就在这里生成。
    顺带覆盖了另一类调用方：`review/service.py` 之外还有人直接调 `run_deliver`（脚本、重试路径），
    把写口放在这里，它们不必各自记得补一次。

    幂等：已经是 `DELIVERED` 就不写、不记留痕。重试组包走 `already_assembled` 早退，不该每次重试
    都在留痕里多一行"又交付了一次"（留痕要能当读数用）。
    """
    request = session.get(FeatureRequest, feature_request_id)
    if request is None:
        return None
    prior = str(request.status)
    if prior == REQUEST_DELIVERED:
        return None
    request.status = REQUEST_DELIVERED
    events.record(
        session,
        entity="feature_request",
        entity_id=str(feature_request_id),
        kind="TASK",
        actor_agent_id=ACTOR_AGENT_ID,
        payload={
            "node": "deliver",
            "action": REQUEST_STATUS_SYNCED,
            "prior_status": prior,
            "status_after": REQUEST_DELIVERED,
            "deliverable_id": str(deliverable_id),
        },
        created_at=now,
    )
    session.flush()
    return REQUEST_DELIVERED


def run_deliver(
    feature_request_id: uuid.UUID,
    *,
    transport: str,
    bus_root: Path,
    session,
    now: dt.datetime,
    entry_name: str,
) -> DeliveryReport:
    """组成**唯一一份**交付包（EN-17）并由公共区单点代笔写出 `results/delivery_package.json`。

    四格顺序各有理由：

    ① 门先说话，与受理/恢复/指派/执行/验收同一条纪律。
    ② **入口页由调用方点名，装配点不猜**（见 `DEFAULT_ENTRY_NAME` 的来历）。名字空 ⇒ `entry_name_empty`，
       manifest 里选不中 ⇒ `entry_absent`，选中多份 ⇒ `entry_ambiguous`。
    ③ 组包、上限、互冲、唯一性**全在实体那侧**（`assemble.assemble()`）：本函数不数卡、不判状态、
       不查"是不是已经有包了"。`FR-036` 的"唯一一份"由实体的 `delivery_package_exists` 守着，
       装配点再判一次就是给唯一性造第二个载体。
    ④ **写公共区只走 `OrchestratorWriter`**（`acl.PUBLIC_OWNER` 那一份身份），不直接 `gateway.write_artifact`
       带平台身份写：`results/` 与 `status.md` 的合法写者是编排器代笔这一个点，绕开它 T095 那条
       "公共区单点代笔"的判据就成了一句口号。

    未达标项清单**从库里读**（`open_issues_of()`），不再由调用方交进来：那份清单的事实源是验收段自己
    落在 EN-14 上的那一轮结论，装配点收一份外部清单再交给 `assemble()`，等于允许"交付包说的"与
    "验收岗判的"是两件事（owner 2026-10-17 裁决 3 第 ① 项）。有卡带问题而库里读不到清单 ⇒ 在这一格
    就停（`open_issues_unreadable`），不把它降级成空清单交给实体去拒。
    """
    resolve_transport_flag(transport)
    if not entry_name or not entry_name.strip():
        raise RunFailure("入口页名是调用方的决定，空名字不是'默认值'而是'没决定'", code=ENTRY_NAME_EMPTY)
    project_key = str(_request_row(session, feature_request_id).project_key)
    gateway = _gateway_for(bus_root, project_key)
    project_root = archive.archive_root(project_key, bus_root=bus_root)
    entry_path = _entry_path_of(assemble_node.validated_manifest(session, feature_request_id), entry_name, feature_request_id)

    try:
        package = assemble_node.assemble(
            session,
            feature_request_id=feature_request_id,
            entry_path=entry_path,
            assembled_by=ACTOR_AGENT_ID,
            created_at=now,
            open_issues=open_issues_of(session, feature_request_id),
            bus_root=project_root,
        )
    except assemble_node.AssembleMisuse:
        # P1-6：组包失败（典型是 artifact_stale）不等于"卡还在跑"。卡已经全交付时，
        # 需求单状态必须先对齐成 DELIVERED，再把组包错误原样抛出去 —— 否则看板会一直说
        # "还在跑"，而真正该看的是"包不能交"。
        from acp import realrun as realrun_module

        realrun_module.sync_request_status(session, feature_request_id, now=now)
        raise
    with OrchestratorWriter(gateway) as writer:
        written = writer.write_delivery_package(assemble_node.payload_of(package))

    row = session.query(Deliverable).filter(Deliverable.feature_request_id == feature_request_id).one()
    # 包已经在盘上了（`written` 非空才走得到这里）⇒ 这一单的事实是"交了"，把 EN-01 那一列对齐。
    # 位置在 EN-17 那行读回之后：交付包这一事实先落定，再改状态，不会出现"状态已交付、包却没有"。
    mark_request_delivered(session, feature_request_id, now=now, deliverable_id=row.id)
    delivered = session.query(TaskCard).filter(
        TaskCard.feature_request_id == feature_request_id,
        TaskCard.status.in_(assemble_node.DELIVERED_STATES),
    ).count()
    return DeliveryReport(
        feature_request_id=feature_request_id,
        deliverable_id=row.id,
        entry_path=str(row.entry_path),
        manifest_count=len(list(row.manifest or ())),
        cards_delivered=delivered,
        package_path=written,
        package_bytes=(project_root / written).stat().st_size,
    )


# ———————————————————————————————————— 公共区投影段（T124 步骤 4）

#: `tasks/<卡号>-<slug>.md` 的后半段。刻意用一个**不承载语义**的词：文件名要能反解出卡号
#: （`recover.TASK_FILE_RE` 是宪法「三」崩溃回退第 2 条的入口），而 EN-02 里没有一列天生是英文 slug ——
#: 标题是中文，`TASK_FILE_RE` 的 slug 只收 `[a-z0-9_-]`。从标题现造一份 slug 等于把"文件名"变成第二份
#: 需求描述，还会在同标题的两张卡上撞名（`read_cards` 按卡号收进字典，撞名就是静默覆盖）。
CARD_SLUG = "card"
#: 契约 §3 那三行 MUST 非空：`recover.CARD_LINE_RE` 是 `^(目标|输入|期望产物): (.+)$`，空值匹配不上 ⇒
#: `_parse_body` 报"缺行"，整张卡在读侧解析失败，那条"停 PG 后按 §4 重建三元组"的回退路径直接断。
#: 所以空的那一格渲染成**明写的空**（下面两个常量），不留白、也不拿别的列顶上去冒充内容。
NO_INPUTS = "（无依赖）"
NO_CRITERIA = "（无验收标准）"
CARDS_EMPTY = "cards_absent"


def _artifact_rows_of(session, feature_request_id: uuid.UUID, task_id: str) -> tuple[Artifact, ...]:
    """这张卡在 EN-06 的全部行，按 `rel_path` 排序 —— 公共区那两格**只读这一次**。

    排序取 `rel_path` 而不是 `created_at`：`status.md` 的"产物落点"必须同时是 `results/<卡号>.md` 里的
    一条，而两次查询恰好给出同一个末位这件事不能靠运气 —— 一份顺序、一次读取，落点 = 末位，索引 = 全列。

    这里**不筛 `validated_status`**：契约 §6 关系式一比的是"`results/**` 条目数 == 该任务 `artifact` 记录数"
    （`recover.reconcile`），门禁判不过的那些行仍然在 EN-06 里；把它们从索引里滤掉，那条关系式就变成
    两个方向的差集。过滤是交付包那一段的职责（`assemble.validated_manifest`），不是投影这一段的。
    """
    return tuple(
        session.query(Artifact)
        .filter(Artifact.feature_request_id == feature_request_id, Artifact.task_card_id == task_id)
        .order_by(Artifact.rel_path)
        .all()
    )


def _usage_by_basis(session, task_id: str, project_key: str) -> dict[str, Decimal]:
    """`status.md` 的用量格 = EN-13 按 `billing_basis` 分列的**金额小计**（FR-061：从不互加）。

    为什么是金额而不是数量：同一口径内可能有两种单位家族（`SEAT_CREDIT` 下 `CALL` 按 Credit 计、
    `SEAT_DAY` 按坐席天计），把它们加成一个数正是 `ledger.Breakdown.total()` 故意抛异常禁掉的那件事
    （`cost/ledger.py:109`）。分列金额每口径一格、同币种、永不相加 ⇒ 这一格是唯一不会说谎的形状。
    **口径名与顺序取自 `orchestrator_writer.USAGE_BASIS`**（FR-061 三口径的那一份清单），这里不另列一份。

    登记的精度限制（不在本笔修）：`ledger.by_task` 只按 `task_card_id`(+`project_key`) 过滤，而卡号只在
    需求单内唯一（owner 裁决 2 的复合主键）⇒ 跨需求单的同名卡会被读成同一张。那条限制的原话与修法
    在 `cost/budget.py::token_used` 的说明里（同一件事，不在这里重述一遍）；要动的是 EN-13 的列与唯一
    索引形状 = 迁移 + 索引，两者都属硬停项。
    """
    breakdown = ledger.by_task(session, task_card_id=task_id, project_key=project_key)
    return {basis: breakdown.subtotal(basis).cost_amount for basis in USAGE_BASIS}


@dataclass(frozen=True)
class PublishReport:
    """投影之后的读数。`status_rows` / `index_entries` 是**从盘上用读侧的读者读回来的**，不是写入次数；
    `artifact_rows` 是 EN-06 现值计数 —— 后两个数应当相等，那条相等就是契约 §6 关系式一。

    `agents_path` 与 `status_path` 同格：都是代笔点返回的**相对路径**，不是尺寸。这一段一次都不 `stat()`
    盘上文件（判据 `test_the_two_public_readings_come_from_the_reader_side_not_the_write_count`），
    尺寸那一份口径归交付包段；`AGENTS.md` 落没落在盘上，由判据自己量。
    """

    feature_request_id: uuid.UUID
    project_key: str
    agents: tuple[str, ...]
    cards_published: int
    dirs_made: tuple[str, ...]
    status_rows: int
    index_entries: int
    artifact_rows: int
    status_path: str
    agents_path: str


def run_publish(feature_request_id: uuid.UUID, *, transport: str, bus_root: Path, session) -> PublishReport:
    """把 EN-01 / EN-02 / EN-03 / EN-06 / EN-13 的现值投影到**公共区五件**。

    为什么要有这一段：`contracts/file-bus.md §4` 的更新时机写的是"每次状态迁移后由单点写入"，而
    本仓库到今天**一个生产写者都没有**（`phase6-review-v1.0.0.md` 第 5 行把这一格登记为待办，原话是
    "公共投影在整个生产路径上还没有任何写点"）。不写这五件，两件事就立不住：契约 §6 那两条关系式
    （`results/**` 条目数 == EN-06 行数、`tasks/**` 与 `status.md` 成对）在真实链路上算不出数，
    宪法「三」那条"停 PG 后按 §4 重建三元组"也没有输入可重建。

    五件与各自挡的错：

    ① `ensure_layout(在册工具)` —— 目录按 `layout.PUBLIC_DIRS` 建，workspace 按 EN-03 现值建。
       挡的错：拿注册面之外的一份名单建目录 = 给"在册工具"造第二个载体。
    ② `write_task_card(...)` —— 三行走 `_card_face_of()` 那一份投影（与下发给 Agent 的是同一份）。
       挡的错：公共区那张卡与当时下发的卡内容不同 ⇒ "做了什么"这条过程性事实事后对不上。
    ③ `upsert_status(...)` —— 每卡一行，状态与三岗全部取自 EN-02 现值；**没指派就是空串**，不填"未指派"。
       挡的错：把"还没决定"渲染成一个看起来像决定的词。
    ④ `write_result_index(...)` —— 条目 = EN-06 该卡的全部行（实测的 bytes / lines / format，不是自述）。
       挡的错：索引里写一个盘上没有的路径，或把产物本体复制进公共区（那是第二份文件树）。
    ⑤ `write_agents_md(project_key, 在册工具)` —— `AGENTS.md` 是协议的**运行时副本**（契约 §1/§2 把它列在
       公共区里）。owner 2026-10-17 裁决 3 第 ② 项点名补这一格：此前 `write_agents_md` 的生产调用方是 0，
       只有 `test_bus_isolation.py` 里用例自己构造的那一次 ⇒ "公共区唯一写入者"这件事在生产路径上从来没被
       走过。正文不在这里手抄：由 `orchestrator_writer.agents_md()` 从 `acl.RULES` / `CARD_LABELS` 现生成，
       比那份函数与 `acl` 是否同源的判据也在 `test_bus_isolation.py`，本段只负责"有人真的调它"。

    与交付包段的分工：那一段写 `results/delivery_package.json`（入口与 manifest），这一段写"过程"。
    两段都只经 `OrchestratorWriter` 那一个代笔点落盘（`acl.PUBLIC_OWNER`），装配点自己一次
    `gateway.write_artifact` 都没有。

    诚实边界（三条，登记在 `evidence/phase-12.md`）：
    · 本段是**整表重投影**（读库现值 → 重写五件），不是"每个迁移点各挂一次"；跑一次只反映跑那一刻的状态。
      逐迁移挂点要等 CLI 串起来（T123 的入口那一格），届时这个函数按现调用即可，不必改形状。
    · `daily-log.md` 的写者**不在本段**（`append_event` 挂在 `run_chain` 每轮之后，owner 裁决 3 第 ③ 项）：
      本段是重投影，逐卡追一行会让"同一次交互"在每次重投影里都出现一遍。两面的"谁为准"就此定死 ——
      **状态迁移以 EN-14 为准**，`daily-log.md` 记的是链路上真的跑过哪一张卡、由谁跑的。
    · 用量格在仿真链上恒为 0：`StubAdapter` 不产用量的回执（`adapters/stub.py` 边界 ③"没有真实调用就没有
      用量"）⇒ EN-13 零行。这一格今天证明的是"分列的通道通了"，不是"金额算对了"。
    """
    resolve_transport_flag(transport)
    project_key = str(_request_row(session, feature_request_id).project_key)
    root = archive.archive_root(project_key, bus_root=bus_root)
    gateway = _gateway_for(bus_root, project_key)
    agents = tuple(
        sorted(str(row[0]) for row in session.query(AgentUnit.agent_id).order_by(AgentUnit.agent_id).all())
    )
    cards = tuple(
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .order_by(TaskCard.id)
        .all()
    )
    if not cards:
        raise RunFailure(
            f"需求单 {feature_request_id} 下没有任何任务卡 ⇒ 投影出来的公共区是一张空表，那不是事实而是事故"
            f"（受理段没跑？卡被拆到别的需求单下？）：project_key={project_key}",
            code=CARDS_EMPTY,
        )
    artifact_rows = 0
    status_path = ""
    with OrchestratorWriter(gateway) as writer:
        made = tuple(writer.ensure_layout(agents))
        agents_path = writer.write_agents_md(project_key, agents)
        for row in cards:
            task_id = str(row.id)
            face = _card_face_of(session, feature_request_id, row)
            artifacts = _artifact_rows_of(session, feature_request_id, task_id)
            artifact_rows += len(artifacts)
            writer.write_task_card(
                task_id,
                CARD_SLUG,
                TaskCardBody(
                    objective=face.objective,
                    inputs="；".join(face.inputs) or NO_INPUTS,
                    expected_output=face.expected_output or NO_CRITERIA,
                ),
            )
            status_path = writer.upsert_status(
                StatusRow(
                    task_id=task_id,
                    status=str(row.status),
                    executor=str(row.executor_agent_id or ""),
                    reviewer=str(row.reviewer_agent_id or ""),
                    acceptor=str(row.acceptor_agent_id or ""),
                    artifact_path=str(artifacts[-1].rel_path) if artifacts else "",
                    usage=_usage_by_basis(session, task_id, project_key),
                )
            )
            writer.write_result_index(
                task_id,
                tuple(
                    (str(item.rel_path), int(item.bytes), int(item.lines), str(item.format))
                    for item in artifacts
                ),
            )
    return PublishReport(
        feature_request_id=feature_request_id,
        project_key=project_key,
        agents=agents,
        cards_published=len(cards),
        dirs_made=made,
        status_rows=len(recover.read_status(root)),
        index_entries=sum(len(recover.read_result_index(root, str(row.id))) for row in cards),
        artifact_rows=artifact_rows,
        status_path=status_path,
        agents_path=agents_path,
    )


# ———————————————————————————————————— 整链驱动（Q10 甲的"生产驱动"那一格）

#: 死轮兜底：每轮至少推进一张卡，而一张卡最多被换人一次（`handoff.MAX_HANDOFF_COUNT`），
#: 内置四张卡 ⇒ 正常链在 8 轮内一定走到"没有可派发的卡"。超过这个数就是**装配点在原地打转**
#: （某处把已派发的卡又读回可派发态），宁可报停也不要转下去 —— 转下去的表现正是 Q10 那一发挂死。
MAX_CHAIN_ROUNDS = 16
CHAIN_ROUND_LIMIT = "chain_round_limit"

#: `daily-log.md` 那一行的"实体 / 动作"两列（契约 §4 的五列格式：`时间 | 实体 | 动作 | actor | 摘要`）。
#: 这一族只有驱动这一个写者 ⇒ 名字写在这里，读侧留给复核者肉眼（`recover` 不读这份文件，
#: 它不是宪法「三」那条"停 PG 后重建三元组"的输入 —— 那份输入是 `status.md` + `tasks/**` + `results/**`）。
ACTIVITY_ENTITY = "task_card"
ACTIVITY_ACTION = "chain_round"


def _daily_log_lines(root: Path) -> int:
    """盘上那份 `daily-log.md` 的事件行数（没有这个文件就是 0，不报错）。

    为什么数盘上不数"调了几次 `append_event`"：与投影段那条"读数从盘上用读侧的读者读回"同一口径 ——
    写侧计数读不出"调用了但没落盘"。行是什么形状不在这里再定义一遍：契约 §4 那句"追加式事件行"
    是格式的唯一出处，这里只按非空行收（`append_event` 每发恰好追加一行）。
    """
    path = root / layout.DAILY_LOG_MD
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def _activity_of(execution: ExecutionReport, accepted: AcceptanceReport | None) -> tuple[str, str]:
    """一张卡在这一轮跑完之后，活动流那一行的 `(actor, summary)` 两列。

    为什么单独一个函数：这一发有"过了验收"与"门禁没过、压根没进验收段"两种形状，两处分头拼串迟早
    有一侧漏掉 `met=` 那一格。挡的错是活动流里两种形状读起来一样 —— 那就看不出这一轮真的过了几道岗。

    `actor` 取**执行岗**而不是编排身份：FR-052 那句要的是"执行者"，而"这份文件由单点代笔写"已经由
    `acl.RULES` 表达了，不必在每一行再署一次编排器的名。
    `summary` 只放卡号、状态名与计数 —— 不放需求原文、不放厂商返回的自由文本（宪法「二」凭据管理与
    契约 §5 脱敏红线的共同要求：落盘前的 `redact_bus_text` 是兜底，不是"可以往里塞别的"的许可）。
    """
    executor, reviewer, acceptor = execution.seats_after
    parts = [
        f"task={execution.card_id}",
        f"status={execution.card_status_after}",
        f"executor={executor}",
        f"reviewer={reviewer}",
        f"acceptor={acceptor}",
        f"verdict_rows={execution.verdict_rows}",
        f"artifacts={execution.artifact_count}",
        f"handoff={execution.handoff_count_after}",
    ]
    if accepted is not None:
        parts += [
            f"round={accepted.round_no}",
            f"met={accepted.met}",
            f"unmet={accepted.unmet}",
            f"limit={accepted.limit}",
        ]
    return executor, " ".join(parts)


@dataclass(frozen=True)
class ChainReport:
    """整链跑完之后的读数。`rounds` / `deferred` / `blocked` 三格合起来回答"为什么停在这一刻"。

    `activity_lines` 是从**盘上**数回来的 `daily-log.md` 行数，不是 `append_event` 的调用次数 ——
    写侧计数读不出"调用了但没落盘"，而这一格要回答的正是"链上每跑完一张卡，活动流里确实多了一行"。
    """

    feature_request_id: uuid.UUID
    project_key: str
    rounds: int
    dispatched: tuple[tuple[str, str, str, str], ...]
    outcomes: tuple[tuple[str, str], ...]
    blocked: tuple[tuple[str, str], ...]
    delivery: DeliveryReport
    publish: PublishReport
    activity_lines: int


def run_chain(
    feature_request_id: uuid.UUID,
    *,
    transport: str,
    bus_root: Path,
    session,
    now: dt.datetime,
    findings_for: FindingsFor,
    accept_for: AcceptanceProvider,
    entry_name: str,
) -> ChainReport:
    """把一张需求单从**指派段**跑到**交付包 + 公共区投影**，每轮只派发一张卡。

    前置是受理段 + 人批 + 恢复段已经跑过（卡已在 `READY`）。这一段**故意不叫 `run_resume` 的第二次**：
    恢复段要求图上有一个等待点，跑第二次的读数是 `no_waiting_point`，把它塞进本函数就等于
    "驱动自己决定要不要重放恢复" —— 那不是驱动该决定的事（哪一发算恢复，归调用方）。

    这一段就是 Q10 里"生产驱动 MUST 显式传值"的那个调用方：`run_assign(..., max_cards_per_round=1)`
    写死在这里，判据 `test_the_production_driver_passes_a_real_round_limit` 钉的就是这一行。
    为什么是 1 而不是 2：槽是按工具算的，一轮派两张就意味着第二张要等第一张的执行段回来才可能拿到额度，
    而**在同一个调用栈里等**就是那一发挂死；1 是"每轮占一格、下一轮之前一定还掉"的那个唯一安全值。

    四条纪律：
    ① 门先说话（`transport`），再建槽层 —— 槽层要读 EN-03，门不许排在它后面。
    ② 每轮之后**立刻**把这张卡跑完（执行段 → 验收段），槽在 `run_card()` 的 `finally` 里还掉，
       所以下一轮面对的额度是干净的。这就是"容量满时下一轮再试，不在本轮阻塞"的落地形状。
    ③ 验收只在卡真的停在 `ACCEPTING` 时做（门禁没过就是 `ESCALATED` / `HANDED_OFF`，那时验收段
       的前件不成立，硬调会得到 `accept_node` 的拒绝而不是得到一条读数）。
    ④ 停轮的条件是"指派段自己说没有可派发的卡"（`nothing_dispatchable`），不是轮数；
       `MAX_CHAIN_ROUNDS` 只是原地打转的兜底，走到它就报错停下，不静默收尾。
    ⑤ 每张卡跑完**立刻**向 `daily-log.md` 追一行活动流（owner 2026-10-17 裁决 3 第 ③ 项：
       `append_event` 此前生产调用方是 0）。这一发挂在驱动而不是投影段，因为驱动才知道"哪一次算一轮"：
       `run_publish` 是整表重投影，逐卡追行会让同一次交互在每次重投影里重复出现。
       writer 按卡**短命**（每卡一个 `with`，用完即关）⇒ 任一时刻仍只有一个公共区写线程活着，
       代价是一次链上先后出现 N 个线程 id（`orchestrator_writer` 那句话里的"只看一个线程 id"
       在整条链上不再字面成立，这一点写在 `evidence/phase-12.md`）。

    **这一发仍没有命令行入口**（`main()` 不调它）：入口那一格的开关前件是"缺真实结论来源"，
    本轮的结论来源仍是 `StubFindingProvider` / `StubAcceptanceProvider`（owner 裁 Q8 = 甲，装配点代填）。
    ⇒ 它是给判据、探针与 `verify_mvp1` 用的库内驱动，不是给用户敲的旗标。
    **直接调库的人今天不进活动流**：走 `run_assign` / `run_execute` / `run_accept` 自己拼顺序的调用方
    一次 `append_event` 都不会发生（活动流记的是"链跑过"，不是"库动过"）。那一格与 `status.md` 的
    逐迁移挂点同类，随 CLI 入口归 MVP-2。
    """
    resolve_transport_flag(transport)
    project_key = str(_request_row(session, feature_request_id).project_key)
    slots = _slots_from_registry(session)
    leases = scheduler.LeaseLedger()
    rounds = 0
    dispatched: list[tuple[str, str, str, str]] = []
    outcomes: list[tuple[str, str]] = []
    blocked: tuple[tuple[str, str], ...] = ()
    while True:
        if rounds >= MAX_CHAIN_ROUNDS:
            raise RunFailure(
                f"整链在 {MAX_CHAIN_ROUNDS} 轮里没走到「没有可派发的卡」⇒ 这是原地打转，不是慢："
                f"每轮的派发读数为 {[item[0] for item in dispatched]}，先把卡号交给复核者",
                code=CHAIN_ROUND_LIMIT,
            )
        try:
            assigned = run_assign(
                feature_request_id, transport=transport, session=session, now=now,
                slots=slots, leases=leases, max_cards_per_round=1,
            )
        except RunFailure as exc:
            if exc.code != NOTHING_DISPATCHABLE:
                raise
            blocked = exc_blocked_of(session, feature_request_id)
            break
        rounds += 1
        dispatched.extend(assigned.dispatched)
        for entry in assigned.dispatched:
            card_id = entry[0]
            execution = run_execute(
                feature_request_id, card_id, transport=transport, bus_root=bus_root, session=session,
                now=now, findings_for=findings_for, slots=slots, leases=leases,
            )
            accepted = None
            if execution.card_status_after == accept_node.ACCEPTING:
                accepted = run_accept(
                    feature_request_id, card_id, transport=transport, session=session,
                    now=now, accept_for=accept_for,
                )
                outcomes.append((card_id, str(accepted.card_status_after)))
            else:
                outcomes.append((card_id, str(execution.card_status_after)))
            actor, summary = _activity_of(execution, accepted)
            with OrchestratorWriter(_gateway_for(bus_root, project_key)) as writer:
                writer.append_event(
                    entity=ACTIVITY_ENTITY, action=ACTIVITY_ACTION, actor=actor,
                    summary=summary, occurred_at=now,
                )
        blocked = assigned.blocked
    delivery = run_deliver(
        feature_request_id, transport=transport, bus_root=bus_root, session=session, now=now,
        entry_name=entry_name,
    )
    publish = run_publish(
        feature_request_id, transport=transport, bus_root=bus_root, session=session,
    )
    return ChainReport(
        feature_request_id=feature_request_id,
        project_key=project_key,
        rounds=rounds,
        dispatched=tuple(dispatched),
        outcomes=tuple(outcomes),
        blocked=blocked,
        delivery=delivery,
        publish=publish,
        activity_lines=_daily_log_lines(archive.archive_root(project_key, bus_root=bus_root)),
    )


def exc_blocked_of(session, feature_request_id: uuid.UUID) -> tuple[tuple[str, str], ...]:
    """停下来那一刻的**阻塞读数**（为什么没有可派发的卡了）—— 读实体，不自己判。"""
    return dispatch_node.readiness(dispatch_node.cards_of(session, feature_request_id)).blocked


# ———————————————————————————————————— 命令行

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="多 Agent 协作平台的最小装配点")
    parser.add_argument("--project", required=True, help="项目键（总线根下的目录名）")
    parser.add_argument("--request", help="需求原话，逐字入库（EN-01 的 raw_input）")
    #: 恢复段接上了（owner 2026-10-16 授权第 2 步下半段）。形状取"给需求单号"而不是 `--resume` 无值：
    #: 一个 thread 由 `(卡点类, 需求单号)` 推导，CLI 不让人猜"恢复的是哪一条"—— 那正是 FR-041 禁的形态。
    parser.add_argument("--resume", metavar="FEATURE_REQUEST_ID", help="把这张需求单已裁决的 A 类单据交回图里续跑")
    #: **刻意没有 `default=`**：给了默认值就等于装配点允许"没选运输方式"地跑起来，
    #: 而下一次误敲会是一次真实外部调用。门的本体在 `resolve_transport_flag()`，这条只是把它接到 argv。
    parser.add_argument("--transport", help="只有 `stub` 可用，且必须显式给")
    parser.add_argument("--bus-root", help="总线根目录；缺省时按 `ACP_BUS_ROOT` / cwd 相对路径解析")
    return parser


def _emit(verdict: str, **fields) -> None:
    joined = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    print(f"RUN {verdict} :: {joined}" if joined else f"RUN {verdict}")


MODE_REQUEST = "request"
MODE_RESUME = "resume"
#: 两个都不给 / 两个都给时打出去的那句提示。它存在的理由是 `--resume` 已经接上：
#: 现在"没给旗标"不再等于"就是想跑受理段"，猜一次就会把一条已裁决的单子重新投一遍。
USAGE_ONE_OF = "`--request` 与 `--resume <需求单号>` 必给且只能给一个"


def _mode(args) -> tuple[str, Any, str]:
    """把 argv 收成 (形态, 值, 用法提示)；形态为空串表示命令行本身不成立。

    空白与"没给"同一格：`--request "   "` 在 `if not args.request` 里是**真值**，第一版就是这么放过去的
    （判据 `test_a_blank_request_is_a_usage_error_decided_before_the_credentials` 钉的正是那一发）。
    `--resume` 的值必须是 UUID：一个 thread 由 `(卡点类, 需求单号)` 推导，敲错一个字符就会恢复成
    "另一条不存在的现场"，那不如在命令行层就拒掉。
    """
    text = (args.request or "").strip()
    raw = (args.resume or "").strip()
    if bool(text) == bool(raw):
        return "", None, USAGE_ONE_OF
    if text:
        return MODE_REQUEST, text, ""
    try:
        return MODE_RESUME, uuid.UUID(raw), ""
    except ValueError:
        return "", None, f"`--resume` 要一个需求单号（UUID），收到 {raw!r}"


def main(argv: Sequence[str] | None = None, *, clock: Callable[[], dt.datetime] | None = None) -> int:
    install_logging_filter()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse 自己已经打过用法错误，这里只统一退出码
        return int(exc.code or EXIT_USAGE)
    kind, value, hint = _mode(args)
    if not kind:
        _emit("FAIL", code=USAGE_MISSING, hint=hint)
        return EXIT_USAGE

    now = (clock or (lambda: dt.datetime.now(dt.timezone.utc)))()
    bus_root = Path(args.bus_root) if args.bus_root else None
    try:
        #: 先过门再碰凭据：门的拒绝必须说话在先（`run_intake` / `run_resume` 里各还有第二次，是给直接调库的人的）。
        resolve_transport_flag(args.transport)
    except StubMisuse as exc:
        #: **退出码取 2 而不是 1**（owner 2026-10-16 裁决 1 接受）：这一次拒绝发生在任何连接之前，
        #: 和 argparse 的用法错误同档 —— 都没碰库，且归因落在旗标本身。
        #: 跑起来之后才抛的 `StubMisuse`（如产物没点名）仍走下面那档 1。
        _emit("FAIL", code=exc.code)
        print(f"RUN DETAIL :: {exc}")
        return EXIT_USAGE
    try:
        factory = session_module.make_session_factory()
        with session_module.new_session(factory) as session:
            if kind == MODE_REQUEST:
                report = run_intake(
                    value,
                    project_key=args.project,
                    transport=args.transport,
                    bus_root=bus_root,
                    session=session,
                    now=now,
                )
                # P1-8：`ACP_APPROVAL_MODE=auto` 时 A 类单据免确认并**接着跑**，与网页同形。
                # 没有这一支，同一条 .env 在两个入口下的表现就不一样。
                if _auto_pass_split(session, report, now=now):
                    _emit(
                        "AUTO",
                        ticket=report.ticket_id,
                        rule=approval_policy.SPLIT_AUTO_RULE,
                    )
                    report = run_resume(
                        report.feature_request_id,
                        transport=args.transport,
                        bus_root=bus_root,
                        session=session,
                    )
            else:
                report = run_resume(
                    value,
                    transport=args.transport,
                    bus_root=bus_root,
                    session=session,
                )
    except RunFailure as exc:
        _emit("FAIL", code=exc.code)
        print(f"RUN DETAIL :: {exc}")
        return EXIT_RUNTIME
    except CredentialMissing as exc:
        #: 它自带缺的变量名，这里只转写成一行，不另造归因、也不回显任何值。
        _emit("FAIL", code=type(exc).__name__, name=exc.name)
        print(f"RUN DETAIL :: {exc}")
        return EXIT_RUNTIME
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        _emit("FAIL", code=code)
        print(f"RUN DETAIL :: {type(exc).__name__}: {exc}")
        return EXIT_RUNTIME

    return _report(report)


def _report(report: IntakeReport | ResumeReport) -> int:
    """两支读数的打印面。判定词各一个（`WAITING` / `RESUMED`），因为它们是两件不同的事实。"""
    if isinstance(report, IntakeReport):
        _emit(
            "WAITING",
            request=report.feature_request_id,
            ticket=report.ticket_id,
            cards=",".join(report.card_ids),
            status=report.request_status,
        )
        print(
            f"RUN HINT :: 先批这张单据（`python -m acp.approve approve {report.ticket_id} "
            f"--principal <人> --base-url <后端>`）。那一发要有后端：本仓库不带启动器，"
            f"后端得由人自己起并给 `acp.app.create_app(approvers=…)` 注入白名单（不注入 = 503 "
            f"`approvers_not_configured`，失败关闭）⇒ 没有后端时改走库侧 `acp.approval.repository.resolve`，"
            f"结论同样只落在 EN-09 那一行。批完再 `python -m acp.run --project … "
            f"--resume {report.feature_request_id} --transport stub` 续跑"
        )
        return EXIT_OK
    _emit(
        "RESUMED",
        request=report.feature_request_id,
        ticket=report.ticket_id,
        state=report.ticket_state,
        conclusion=report.conclusion,
        suspended_at=",".join(report.waiting_before) or "-",
        next=",".join(report.waiting_after) or "-",
        status=report.request_status,
    )
    _emit(
        "CARDS",
        statuses=",".join(f"{card_id}:{status}" for card_id, status in report.card_statuses),
    )
    print(
        "RUN HINT :: 卡片状态是门禁回写后的现值。往下那两格今天是**库内有、命令行没有**：整链驱动 "
        "`acp.run.run_chain(需求单号, …, max_cards_per_round=1)`（指派 → 执行 → 验收 → 交付包 → 投影，"
        "一轮一张卡），产物核验段 `python -m acp.verify_mvp1 --project <项目键>`（`quickstart.md` §4 那四条"
        "判据，它有命令行入口，但只**读**已经跑完的现场）。链条这一格仍不开入口的理由没换：结论来源还是仿真替身 "
        "—— 审查走 `StubFindingProvider`，验收按 owner 2026-10-17 裁 Q8 = 甲 由装配点按 EN-07 映射表"
        "**逐条代填**（全 met=True）⇒ 现在开入口，等于允许「把关岗没说话而卡进了 `DELIVERED`」这种读数进 "
        "R-07 的验收。`run_chain` 目前无 CLI 入口，操作入口归 MVP-2（owner 2026-10-17 裁决 1；"
        "判据面一字未动 ⇒ 这一格在库里仍是被判据钉着的那一发，不是漏接）"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
