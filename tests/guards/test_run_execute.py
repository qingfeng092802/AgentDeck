"""T123 装配点（第 2 步下半段第 3 项 · 甲 → 乙）：执行段 `run_execute()` 的判据。

**这一笔接的是哪一段**：`run_assign()` 把一张卡推到 `RUNNING` 并在台账里留下一格占着的槽。
那格槽在甲 那一轮之前**没有人还** —— `scheduler.run_card()` 的 `finally` 早就写好了（`3c5ca6d`），
但它一个生产调用方都没有（甲 那轮的现读：`src/acp` 里 `scheduler.run_card(` 的调用点 0）。
本文件钉的就是那个调用点：领槽 → **在借与还之间**产结果 → 跑卡级那半条链 → 无论成败都退槽。

**2026-10-17 乙 一轮把甲 那轮"刻意不做的三件事"做了两件半**（owner 裁 Q6 = 甲）：

① `Review` / `HandOff` 两个**节点体**在 `run.py` 里了（`_review_node_body` / `_handoff_node_body`），
   两个入参不再是外部给的假面 ⇒ `MissingDownstream` 在真链上不再可达。本文件把甲 那轮那一条
   "缺一个下游"的拒绝面换成**"结论来源缺失"**那一形（同一格拒绝位、同一条 `finally`）。
② `TaskResult` 由装配点**现产**：`_task_of()` 把 EN-01 / EN-02 的现值投影成 `Task` → `submit()` → `collect()`。
   这一步会抛（仿真侧认不出交付物名就拒），所以它必须落在借与还之间 ⇒ `run_card()` 的入参从 `result`
   换成 `produce`。`test_a_producer_that_raises_still_returns_the_slot` 是这次换签名的**唯一证人**：
   把 `produce()` 挪回 `run_card()` 之前，那一发就会读到仍占着的槽。
③ **命令行入口仍不开**：审查结论的来源是显式传进来的 `findings_for`（协议 `review.FindingProvider`），
   今天唯一的实现是 `run.StubFindingProvider` —— 它按卡上声明的标准**固定交回 `met=True`**，
   所以**这几条判据不构成 FR-029 的实证**（owner 2026-10-17 的原话要求，同一句也写在
   `quickstart.md §6` 与 `evidence/phase-12.md §十四`）。缺来源 ⇒ `findings_source_missing`。
   交付包与 R-07 那四份文件也不在本笔。
④ **换人的候选先过席位过滤**（新待裁项 Q7，owner 2026-10-17 **裁 落甲 + 补三条**）：FR-031 的两个断言点
   都不在 `ARTIFACT_INVALID → HANDED_OFF → RUNNING` 这条边上（`data-model.md:342`），而 `handoff()`
   的出局集合来自 EN-14 的 `SELECTION` 行 = "谁执行过"，不含席位 ⇒ 探针实测把执行岗换成了**这张卡的审查岗**
   （`seats_after=('doubao','doubao','workbuddy')`，读数在 `evidence/phase-12.md` §十四 发 3）。
   装配点因此用 `gp.distinctness_violations()`（同一个载体，不写第二份判定）把会撞席位的候选剔掉再递。
   他补的三条落成：**第 1 条 = 那对判据**（三支下 MUST 进 `ESCALATED` 不静默 / 第四支在册 MUST 有合法候选）；
   **第 2 条 = 被挡的支必须可归因** ⇒ 装配点自己落一行 `seat_blocked_candidates` 留痕，理由是 `triple.py` 的原文；
   **第 3 条 = P1 待办**（MVP-2 加第四支工具之后 `handoff` 才有合法候选）记在 `tasks.md` 的 T123 行下。
   仍差的一格如实记着：`ESCALATED` 那一行**自己的** `reason` 字段仍由 `handoff._escalate` 拼，不含席位原因 ⇒
   那一格要动实体（Q7 的 乙 支，三行），owner 未扩权 ⇒ 登记待裁，本轮没动实体。

三节：

- 节 一 = 静态形状（不连库）：装配点不自己退槽；碰库的读数全在闭包里；记账人取 EN-02 现值；
  `produce` 交的是**函数**而不是预构建的结果；**装配点不构造 `ReviewFinding`**；换人理由不现编；
  候选集只有一个载体、且递给 `handoff()` 之前先过席位过滤（过滤本身只问 `gp` 那一个入口，
  函数体里一次比较都不许有）；审查读的是本轮那一批产物；仿真来源在每条证据里留下自己的名字。
- 节 二 = 拒绝面（不连库，假槽层 + 假会话）两条：台账里没有凭据 ⇒ 一次库都不碰；`--transport` 没显式
  选中 ⇒ 在碰任何东西之前先拒（`resolve_transport_flag` 必须是函数体第一条非文档语句）。
  为什么只剩这两条：卡级段头两个节点（`ResultIngest` / `ValidateArtifact`）都要真库，其余拒绝形状在
  假会话上根本进不到那一步（甲 那轮的 W21 就是撞在这格上）⇒ 它们都在节 三。
- 节 三 = 真 PG 十三条：一条链真跑到底（受理 → 人批 → 恢复 → 指派 → 真下发真回收 → 门禁 → 审查）
  并读到 `ACCEPTING` + EN-08 那一行 + 库侧每条 `evidence` 带着仿真标记；同一张卡上留着上一轮那行
  （文件不在总线上）时链子照样到 `ACCEPTING`（"只读本轮那一批"的行为侧证人）；没有来源那一发停在 `RUNNING`
  且 EN-08 零行；结论不合规那一发同理；不达产物下限那张卡走**换人**那条出边 —— 这一形是**一对**：
  内置三支下进 `ESCALATED` 且三岗仍互不相同，补一支第四工具才读得到 `HANDED_OFF` + 次数 1 + 真换人
  （两发合起来才把"按席位剔人"与"见谁剔谁"分开）；
  `produce` 抛错仍退槽且库里零行；回收面对不上卡时段内拒；记账人等于现值执行者（与凭据的差额被量出来）；
  第二发拒绝且库一行都不许多出来；来源抛错仍退槽；退不干净报 `SlotLeak` 且原异常不丢；
  跑完一张不许退别人那一格（`pending_after` / `usage_after` 两格的非空证人）。
  **缺 `ACP_DB_DSN` 时只有这一节 skip**。

反向对照在变异账里（`evidence/phase-12.md` §十四）：纯改文档措辞、等价的闭包写法、语义相同的另一种
投影都不许红；每一条新判据各自配一发单点注入。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import pathlib
import subprocess
import uuid
from contextlib import closing
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp import chaos
from acp import run as run_module
from acp.adapters import stub
from acp.adapters.stub import StubMisuse
from acp.approval import repository as repo
from acp.config.loader import DSN_ENV, db_url
from acp.bus import layout
from acp.guard import triple as gp
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.orchestrate.nodes import review as rv
from acp.orchestrate.nodes.result_ingest import IngestMisuse
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, CapabilityManifest, TaskCard
from acp.persistence.models_registry import EventLog

SRC = pathlib.Path(run_module.__file__)
NOW = dt.datetime(2026, 10, 17, 2, 0, tzinfo=dt.timezone.utc)
PROJECT = "acp-run-execute"
RAW = "做一个介绍公司产品的静态网站，配色你看着办"
PROVIDER = run_module.StubFindingProvider()
ZERO = {"doubao": 0, "qoder": 0, "workbuddy": 0}


# ------------------------------------------------------------------ 小工具


def func_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`run.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def nested_func(name: str, inner: str) -> ast.FunctionDef:
    """取 `name()` 里面那层叫 `inner` 的闭包。找不到就直接红 —— 闭包位置本身就是被测面。"""
    body = func_body(name)
    for node in ast.walk(body):
        if isinstance(node, ast.FunctionDef) and node.name == inner:
            return node
    raise AssertionError(f"`{name}()` 里没有嵌套函数 `{inner}()` ⇒ 碰库的那一步不在闭包里，台账空时就会先读库")


def called_names(func: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name:
                out.append(name)
    return out


def keyword_node(call_owner: ast.AST, kind: str, keyword: str):
    """找到 `kind(**{keyword: 值})` 那一个实参节点（找不到返回 None，由调用方决定怎么红）。"""
    for node in ast.walk(call_owner):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == kind:
                for kw in node.keywords:
                    if kw.arg == keyword:
                        return kw.value
    return None


class Handle:
    """假槽句柄：瞬态协议里 `release()` 只读这两个属性（真 `SlotHandle` 也是这两个）。"""

    def __init__(self, task_id: str, agent_id: str) -> None:
        self.task_id = task_id
        self.agent_id = agent_id


class Slots:
    """进程内槽层的假面：能占、能退、能让"退不干净"这一形**可注入**（`leaky=True`）。

    刻意不复用 `InProcessSlotManager`：这一节要的那两种形状（`release()` 静默 no-op、段内抛错后仍归零）
    在真实现里要么不可达、要么得改真实现，都不该为一判据动。
    """

    def __init__(self, agents: tuple[str, ...] = ("qoder", "doubao", "workbuddy"), *, leaky: bool = False) -> None:
        self._holds = {agent: 0 for agent in agents}
        self.leaky = leaky
        self.acquires: list[tuple[str, str]] = []
        self.releases: list[str] = []

    def acquire(self, task_id: str, agent_id: str) -> Handle:
        self.acquires.append((task_id, agent_id))
        self._holds[agent_id] += 1
        return Handle(task_id, agent_id)

    def release(self, handle: Handle) -> None:
        self.releases.append(handle.task_id)
        if self.leaky:
            return
        self._holds[handle.agent_id] -= 1

    def usage(self) -> dict[str, int]:
        return dict(self._holds)


class Session_:
    """只够 `_seats()` 那一条读数链的假会话；`armed=False` 时任何一次碰库都直接红。"""

    def __init__(self, *, armed: bool = True, seats: tuple[str, str, str] = ("qoder", "doubao", "workbuddy")) -> None:
        self.armed = armed
        self.seats = seats
        self.touches = 0

    def _guard(self, what: str) -> None:
        self.touches += 1
        if not self.armed:
            raise AssertionError(f"台账里还没有凭据就碰了库（{what}）⇒ 段体没留在闭包里")

    def query(self, model):
        self._guard(f"query({getattr(model, '__name__', model)})")
        assert model is TaskCard, f"三岗读数读的不是 EN-02：{model}"
        return self

    def filter(self, *args, **kwargs):
        return self

    def one(self):
        executor, reviewer, acceptor = self.seats
        return SimpleNamespace(executor_agent_id=executor, reviewer_agent_id=reviewer, acceptor_agent_id=acceptor)

    def get(self, model, key):
        self._guard(f"get({getattr(model, '__name__', model)})")
        return object()


class DropsOneCriterion:
    """少判一条的结论来源：`check_findings` 该把它拒在入库之前。

    刻意不判最后一条而不是第一条：判第一条会连"哪条没判"的样例位置都固定住，换一条标准顺序就变形状。
    """

    def findings_for(self, card, criteria):
        kept = [str(item) for item in criteria][: max(0, len(criteria) - 1)]
        return [rv.ReviewFinding(item, True, f"test：故意少判一条（{card.id}）") for item in kept]


class ExplodingProvider:
    """结论来源自己抛 —— 注入点在"审查已经要开口"那一步，所以 EN-06 已经写过行。"""

    def findings_for(self, card, criteria):
        raise RuntimeError("来源炸一次：这条判据要看的是槽照样回来")


def lease_of(task_id: str = "T01", agent_id: str = "qoder", *, slots: Slots | None = None) -> dispatch_node.Lease:
    """照真链路的形状造一张凭据：**先占再交**，句柄来自槽层的 `acquire()`，不是手搓的结构。"""
    layer = slots if slots is not None else Slots()
    handle = layer.acquire(task_id, agent_id)
    return dispatch_node.Lease(card_id=task_id, agent_id=agent_id, handle=handle)


def execute(bus, session, request_id, card_id, *, slots, ledger, findings_for=PROVIDER):
    """本文件唯一的 `run_execute` 调用形状：仿真运输 + 仿真来源，**两个都要显式**。"""
    return run_module.run_execute(
        request_id,
        card_id,
        transport="stub",
        bus_root=bus.path,
        session=session,
        now=NOW,
        findings_for=findings_for,
        slots=slots,
        leases=ledger,
    )


# ---------------------------------------------------------------- 一、静态形状


def test_run_execute_never_borrows_or_returns_a_slot_itself():
    """装配点只调 `run_card`；`acquire` / `release` / `dispatch` 一次都不许出现。

    钉这一格的理由不是"整洁"：`scheduler.run_card()` 的 `finally` 是全仓唯一的收尾点，
    装配点若顺手再退一次，轻则被瞬态层拒（它不认第二次退同一句柄），重则两边都以为对方退了 ——
    后者会让平台上限慢慢吃光，表现为"系统卡住"而不是"哪里报错"。计数走 AST：grep 会把
    `def release(` 与文档串里的词一起算进来。
    """
    calls = called_names(func_body("run_execute"))
    assert calls.count("run_card") == 1, f"生命周期装配点必须恰一处调 run_card：{calls.count('run_card')}"
    for forbidden in ("release", "acquire", "dispatch"):
        assert forbidden not in calls, f"装配点自己碰了 {forbidden}() ⇒ 借还与就绪判定出现第二个载体"


def test_nothing_touches_the_database_until_a_closure_asks_for_it():
    """`run_card()` **之前**一次库都不许碰；之后的收尾读数才允许出现在函数本体里。

    位置本身就是被测面：台账里没有凭据时这一段**一次库都不该碰**（与受理段、恢复段、指派段"门先说话"
    是同一条纪律）。写在函数开头的话，一张没派发的卡会先被读一遍三岗、先投影、先编译一张图，
    然后才撞到 `MissingLease` —— 顺序倒了，归因也跟着倒：人看到的会是库里的读数而不是缺凭据。

    为什么不是"函数本体一次都不许碰库"：`run_execute` 末尾那几格报告读数（卡态、结论行数、三岗现值）
    本来就该在段跑完之后再从库里取，那一时在 `run_card()` 之后，凭据已经领到了。所以这条钉的是
    **时机**（早于 `run_card` 的调用行就红），不是"本体里有没有出现这些名字"。
    """
    body = func_body("run_execute")
    in_segment = called_names(nested_func("run_execute", "segment"))
    in_produce = called_names(nested_func("run_execute", "produce"))
    for required in ("build_execution_graph", "invoke", "_review_node_body", "_handoff_node_body"):
        assert required in in_segment, f"`{required}` 没在 `segment` 闭包里 ⇒ 编译时机跑到领槽之前（{in_segment}）"
    for required in ("_task_of", "StubAdapter", "submit", "collect"):
        assert required in in_produce, f"`{required}` 没在 `produce` 闭包里 ⇒ 下发/回收跑到了借还窗口之外（{in_produce}）"

    run_card_line = min(
        node.lineno for node in ast.walk(body) if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "run_card"
    )
    closure_lines = {
        node.lineno
        for func in (nested_func("run_execute", "segment"), nested_func("run_execute", "produce"))
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
    }
    early = [
        (getattr(node.func, "attr", None) or getattr(node.func, "id", None), node.lineno)
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and node.lineno < run_card_line
        and node.lineno not in closure_lines
    ]
    offenders = [
        item for item in early if item[0] in ("_seats", "_task_of", "_request_row", "_card_row",
                                              "build_execution_graph", "StubAdapter", "_slots_from_registry")
    ]
    assert not offenders, (
        f"这 {len(offenders)} 处碰库发生在 `run_card()`（第 {run_card_line} 行）之前又在闭包之外：{offenders} "
        "⇒ 台账空时它们会先说话"
    )


def test_the_bookkeeper_comes_from_the_card_not_from_the_platform_identity():
    """`ExecutionDeps.actor_agent_id` 必须是卡上现值的执行者，不是 `ACTOR_AGENT_ID`。

    `record_result` 那句拒绝写得很直白（FR-033）："回收方报的是 X，而卡 Y 的执行者是 Z ⇒ 记账人必须
    等于执行者"。装配点如果图省事填平台身份，第一段真跑就会红 —— 所以这一格在 AST 上先钉住：
    实参必须是个变量名，且这个名字在闭包里由一次 `_seats()` 的读数绑出来；`ACTOR_AGENT_ID` 出现在
    本函数就红。**绑法不管**（解包、下标、中间变量都算）：那是写法，不是这条判据要约束的面。
    """
    inner = nested_func("run_execute", "segment")
    actor = keyword_node(inner, "ExecutionDeps", "actor_agent_id")
    assert isinstance(actor, ast.Name), f"actor_agent_id 实参必须是从库里读出来的变量：{type(actor).__name__}"
    assert actor.id != "ACTOR_AGENT_ID", "装配点把平台身份当成了记账人 ⇒ 与 FR-033 相撞"
    bound = [
        node
        for node in ast.walk(inner)
        if isinstance(node, ast.Assign) and actor.id in {getattr(t, "id", None) for t in ast.walk(node.targets[0])}
    ]
    assert bound and all("_seats" in ast.dump(node.value) for node in bound), (
        f"{actor.id} 不是从 `_seats()` 的读数里绑出来的 ⇒ 记账人另有来源（{[ast.dump(n.value)[:60] for n in bound]}）"
    )
    assert "ACTOR_AGENT_ID" not in ast.dump(func_body("run_execute")), "本函数里出现平台身份当记账人"


def test_run_card_is_handed_a_producer_not_a_prebuilt_result():
    """`run_card(produce=…)` 收的是**函数**，而且没有 `result=` 这个入参了。

    这一格是 2026-10-17 那次换签名的形状证人。为什么值得单独立一条：把"产结果"挪到 `run_card()` 之前
    看起来只是移动三行代码，但它把失败面移出了借还窗口 —— 那一发如果抛（仿真侧认不出交付物名就要抛），
    槽已经借出去、`finally` 还没开始，而装配点被明令不许自己退槽（owner 2026-10-16 裁 甲）。
    行为侧的证人是 `test_a_producer_that_raises_still_returns_the_slot`；这一条钉的是"不许改回预构建"。
    """
    body = func_body("run_execute")
    produce = keyword_node(body, "run_card", "produce")
    assert isinstance(produce, ast.Name) and produce.id == "produce", (
        f"交给 run_card 的必须是那个 produce 闭包，实参读到 {ast.dump(produce) if produce else '（没有 produce= 这一项）'}"
    )
    assert keyword_node(body, "run_card", "result") is None, "又交回预构建的 result ⇒ 产结果这一步跑到借还窗口之外"


def test_the_assembly_point_never_constructs_a_review_finding():
    """两个节点体与 `run_execute` 里不许出现 `ReviewFinding(...)`；全模块只许在 `StubFindingProvider` 里构造一次。

    逐条判定只能由**结论来源**交出（`review.FindingProvider`）。节点体一旦自己构造，表现就是
    "平台替审查岗说了话"：`check_findings` 拿到的东西全是从实现里现编的，`met` 想填几就填几，
    而 FR-029 与 FR-031 要的"审查岗必须是那个岗"这件事就从链路上消失了。这一条也是 Q6 裁 甲
    （不做"Review 节点体直读 `PAGE_BODIES` 造 findings"那一支）在代码面上的钉子。
    扫描面按**位置**分两半，而不是整模块一刀切：owner 裁的仿真那一支（`run.StubFindingProvider`）
    按定义就是 findings 的构造点，一刀切会把它一起钉红 —— 那是判据写错，不是实现写错。
    类型标注里的 `review_node.ReviewFinding` 不是构造调用，所以本判据只看 `ast.Call`。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))

    def sites(owner: ast.AST) -> list[int]:
        return [
            node.lineno
            for node in ast.walk(owner)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "ReviewFinding"
        ]

    for name in ("_review_node_body", "_handoff_node_body", "run_execute"):
        found = sites(func_body(name))
        assert not found, f"`{name}()` 自己构造了 `ReviewFinding` ⇒ 审查结论的来源跑到平台身上（行号 {found}）"

    provider = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "StubFindingProvider"]
    assert len(provider) == 1, f"仿真那一支应当恰一个类定义，读到 {len(provider)} 个"
    found = sites(provider[0])
    assert len(found) == 1, (
        f"`StubFindingProvider` 里的构造点应当恰一处（真表达到每一标准只过一遍），读到行号 {found}"
    )
    every = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "ReviewFinding"
    ]
    assert every == found, f"模块里还有别的 `ReviewFinding` 构造点没被这条钉住：{every}"


def test_the_handoff_reason_is_the_gates_own_words_not_an_invented_one():
    """`handoff(...)` 的 `reason` 只能是门禁写在 state 上的那句，不许有非空兜底。

    `state["invalid_reason"]` 的来源是 EN-06 上那次判定的 `invalid_reason`（`validate_artifact` 覆写的列）。
    写一个 `or "产物不达标"` 的兜底，留痕里就会出现一句**没人判定过的原因**，FR-026 要的"为什么换"就废了；
    而空理由本来会撞 `handoff()` 自己的 `reason_required`，那是它该报的错，不该被装配点替它填平。
    所以这一条钉的是：`reason` 实参里出现的字符串字面量只能是键名 `"invalid_reason"` 与空串。
    """
    inner = nested_func("_handoff_node_body", "node")
    reason = keyword_node(inner, "handoff", "reason")
    assert reason is not None, "handoff() 没带 reason ⇒ FR-026 要的换人原因没进留痕"
    literals = [node.value for node in ast.walk(reason) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    forbidden = [item for item in literals if item not in ("invalid_reason", "")]
    assert not forbidden, f"reason 里出现了装配点现编的句子：{forbidden}"
    assert "invalid_reason" in literals, f"reason 不是从门禁结论里读的：{literals}"


def test_the_candidate_set_has_exactly_one_carrier():
    """换人的候选集只能来自 `candidates_from_registry()`，装配点不自己拼、也不自己排。

    理由与图上的 `SelectWorker` 同一个：候选集若有两份"谁在册、谁停用"，漂了的表现是换回一个已经失败过
    的工具（`handoff()` 里那句"排除在场者"读的是入参，入参本身漂了它无从发现）。排序也不在装配点做：
    `handoff()` 自己调 `rank_candidates`，装配点再排一次就是第二个判定载体。
    """
    inner = nested_func("_handoff_node_body", "node")
    calls = called_names(inner)
    assert calls.count("candidates_from_registry") == 1, f"候选集载体应当恰一处：{calls}"
    assert "rank_candidates" not in calls, "装配点自己排序 ⇒ 判定出现第二个载体"
    assert "WorkerCandidate" not in calls, "装配点自己构造候选 ⇒ 在册口径写了两份"
    assert calls.count("handoff") == 1, f"`handoff()` 调用次数应当恰一处：{calls}"


def test_the_candidates_handed_to_handoff_pass_the_seat_filter():
    """`handoff(candidates=…)` 交出去的必须是过滤之后的那个名字，且**剔掉的支必须留下一行留痕**。

    两半各钉一种错：
    ① 把 `candidates=safe` 改回 `candidates=candidates`（取消过滤）⇒ Q7 那条读数回来（执行岗 = 审查岗），
       这里红；② 只剔人不留痕 ⇒ owner 2026-10-17 裁 Q7 补的第 2 条（"原因不许只写成'换人失败'"）落空，
       库里只剩 `handoff()` 那句"没有可替代的工具可换"，看不见真正原因是撞岗 ⇒ 这里也红。
    留痕走 `events.record` 那唯一的写入者、`kind` 沿用 `handoff` 已在用的那个，只换 `action` 名字 ⇒
    这条判据钉的是"有这一行、且它是过滤那一步的产物"，不是"留痕写了两份判定"。
    ③ **名字的来源要跟着那次解包推**（W39 就是这么来的）：第一版只断 `candidates=` 是个 `ast.Name`，
    于是 M65（交回裸的 `candidates`）在实现面**没红**、只有两条 PG 发红了 —— 单侧覆盖不算覆盖，
    现在从解包目标的第一个元素推出该交哪个名字，两条侧一起红。
    """
    inner = nested_func("_handoff_node_body", "node")
    calls = called_names(inner)
    assert calls.count("_seat_safe_candidates") == 1, f"席位过滤没接进 handoff 那一步：{calls}"
    unpack = [
        node
        for node in ast.walk(inner)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "_seat_safe_candidates"
        and isinstance(node.targets[0], ast.Tuple)
    ]
    assert len(unpack) == 1, f"过滤的结果没有被拆成（合法候选, 被挡的支）⇒ 被挡的那一半丢了：{calls}"
    arg = keyword_node(inner, "handoff", "candidates")
    assert isinstance(arg, ast.Name), (
        f"`candidates=` 交的不是过滤结果里那个名字："
        f"{'（没有 candidates= 这一项）' if arg is None else ast.dump(arg)[:90]}"
    )
    first_target = str(unpack[0].targets[0].elts[0].id)  # 不写死变量名：从那次解包的第一个目标推
    assert arg.id == first_target, (
        f"`candidates=` 交的是 `{arg.id}` 而不是过滤结果里的 `{first_target}` ⇒ 过滤算了但没用上"
    )
    assert calls.count("record") == 1, f"被挡的支应当经 `events.record` 恰一行留痕：{calls}"
    payload = keyword_node(inner, "record", "payload")
    assert payload is not None and "SEAT_BLOCKED_ACTION" in ast.dump(payload), (
        "留痕的 `action` 没引那个具名常量 ⇒ 它和 `handoff()` 自己那两条出口（`handed_off` / `escalated`）"
        "要么同名要么写了第三份字面量，读的人分不开"
    )
    joined = [node for node in ast.walk(inner) if isinstance(node, ast.JoinedStr)]
    assert not joined, (
        f"节点体里编了句子（f-string 在行号 {[n.lineno for n in joined]}）⇒ 留痕的理由出现第二份口径，"
        "而它的正本在 `guard/triple.py`（owner 裁 Q7 补第 2 条要的是**原文**）"
    )
    dumped = ast.dump(payload) if payload is not None else ""
    reason_values = [
        value
        for dict_node in ast.walk(payload)
        if isinstance(dict_node, ast.Dict)
        for key, value in zip(dict_node.keys, dict_node.values)
        if isinstance(key, ast.Constant) and key.value == "reasons"
    ]
    assert len(reason_values) == 1, f"留痕里 `reasons` 那一栏应当恰一处，读到 {len(reason_values)}：{dumped[:80]}"
    carried = reason_values[0]
    assert (
        isinstance(carried, ast.Call)
        and getattr(carried.func, "id", None) == "list"
        and len(carried.args) == 1
        and isinstance(carried.args[0], ast.Name)
        and carried.args[0].id == "reasons"
    ), f"留痕里的 reasons 不是 `gp` 那句原子的搬运（要的是 `list(reasons)`）：{ast.dump(carried)[:90]}"


def test_the_seat_filter_asks_the_existing_carrier_instead_of_writing_a_second_one():
    """`_seat_safe_candidates()` 判"撞不撞岗"只能问 `gp.distinctness_violations`，函数体里一次比较都不许有。

    为什么这条要钉在实现面而不是结果面：把它写成 `if c.agent_id != card.reviewer_agent_id: 留下`
    是最省事的实现，而它**恰好也能让本轮两条 PG 读数变绿**（三支注册面下剔掉审查岗、四支下留下第四支）。
    差别要到下一轮才暴露：那种写法漏掉验收岗，`role_id` 那一栏在构造里没有形状，而 `guard/triple.py:130-146`
    的"同人"口径（判定单位 = 工具、NULL 席位不参与、多出来的岗名要报错）在装配点被复写了一遍 —— 两份必漂。
    所以四条一起看：① 恰一次调用 `distinctness_violations`；② 入参用 `gp.Assignment` / `gp.SeatOccupant`
    这两个载体形状构造（不是自造 dict）；③ 三个席位名都出现，少哪一个就有一岗没人挡；
    ④ 函数体里 MUST NOT 有 `ast.Compare` —— 任何 `==` / `!=` 都是第二份判定的形态，
    包括看起来很无害的 `agent_id != card.executor_agent_id`（那一格本来由 `handoff()` 按在场者排除）。
    """
    body = func_body("_seat_safe_candidates")
    calls = called_names(body)
    assert calls.count("distinctness_violations") == 1, f"席位判定应当恰一次问那个唯一载体：{calls}"
    assert "Assignment" in calls and "SeatOccupant" in calls, f"入参不是 triple.py 的形状：{calls}"
    dumped = ast.dump(body)
    for seat in ("executor", "reviewer", "acceptor"):
        assert f"'{seat}'" in dumped, f"席位 {seat!r} 没进这份构造 ⇒ 那一岗没人替它挡"
    comparisons = [node for node in ast.walk(body) if isinstance(node, ast.Compare)]
    assert not comparisons, (
        f"`_seat_safe_candidates()` 里出现了 {len(comparisons)} 处比较（行号 {[c.lineno for c in comparisons]}）"
        " ⇒ 三岗互斥的判定被写成了第二份"
    )
    joined = [node for node in ast.walk(body) if isinstance(node, ast.JoinedStr)]
    assert not joined, (
        f"被挡的理由被装配点自己编了一句（f-string 在行号 {[n.lineno for n in joined]}）⇒ EN-14 里会出现"
        "第二份'什么叫做同人'的口径，而它的正本在 `guard/triple.py`（owner 裁 Q7 补第 2 条要的是**原文**）"
    )


def test_the_reviewer_reads_this_rounds_batch_not_the_whole_card():
    """审查读的正文必须是 `state["artifact_ids"]` 那一批，不是"这张卡的全部产物"。

    这条与 `validate_artifact.py:165-167` 是同一条理由的两侧：换人重试会在同一张卡上再攒一批新行，
    拿整卡的产物去审，就是让**上一轮的交付**被这一轮的审查员签字。仓里有一个现成的
    `bus.actor.artifact_paths_for_review()`，但它给的是整卡口径 ⇒ 本节点**刻意不调它**。

    【2026-10-17 02:1x 本条改写过，原文的缺陷如实记着】第一版只断 `"artifact_ids" in ast.dump(...)`，
    于是 M59（把查询条件换成 `Artifact.task_card_id == task_id`）**当场全绿 69 passed** —— 因为那句
    `ids = list(state.get("artifact_ids"))` 还留在原地，只是查询不再用它。"键被读过"不等于"查询用它"，
    这是 [[regression-guards]] 里"死判据拆开算期望值"那一条形。改完钉三件事：① `Artifact.id.in_(...)`
    恰一次；② 节点体里 MUST NOT 出现 `Artifact.task_card_id`（整卡口径的那一形）；③ 整卡口径的 helper
    没被调。行为侧另有一发独立证人（`test_a_stale_artifact_row_from_the_previous_round_is_not_reviewed`），
    它比 AST 更硬：上一轮那行的文件不在总线上，读整卡就会炸。
    """
    inner = nested_func("_review_node_body", "node")
    in_calls = [
        node
        for node in ast.walk(inner)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "in_"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "id"
        and getattr(node.func.value.value, "id", None) == "Artifact"
    ]
    assert len(in_calls) == 1, f"`Artifact.id.in_(…)` 应当恰一次（本轮那一批），读到 {len(in_calls)} 次"
    whole_card = [
        node.lineno
        for node in ast.walk(inner)
        if isinstance(node, ast.Attribute)
        and node.attr == "task_card_id"
        and getattr(node.value, "id", None) == "Artifact"
    ]
    assert not whole_card, (
        f"审查按 `Artifact.task_card_id` 查 ⇒ 读的是整卡口径（行号 {whole_card}），上一轮交付会被这一轮签字"
    )
    assert "artifact_paths_for_review" not in called_names(inner), (
        "审查改读整卡产物列表 ⇒ 换人重试时上一轮的交付会被这一轮签字"
    )


def test_the_review_chain_calls_the_three_entity_functions_once_each():
    """审查链在节点体里恰好各调一次 `build_request` / `check_findings` / `record_verdict`，且都早于写状态。

    三条各挡一种错：少一次 `build_request` ⇒ 审查者没看到正文就被判；两次 `check_findings` ⇒
    同一份判定被两个地方各判一次（结论可以不一样）；`record_verdict` 之前没推进 `REVIEWING` ⇒
    撞它自己的 `state_not_reviewing`。顺序也钉：`card.status = REVIEWING` 这次赋值必须出现在
    `record_verdict` 调用之前（`validate_artifact.py:27` 明写这一步归 `Review` 节点）。
    """
    inner = nested_func("_review_node_body", "node")
    calls = called_names(inner)
    for name in ("build_request", "check_findings", "record_verdict"):
        assert calls.count(name) == 1, f"`{name}` 应当恰一处调用，读到 {calls.count(name)}：{calls}"
    verdict_line = next(node.lineno for node in ast.walk(inner) if isinstance(node, ast.Call)
                        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "record_verdict")
    reviewing = [
        node.lineno
        for node in ast.walk(inner)
        if isinstance(node, ast.Assign) and "status" in ast.dump(node.targets[0]) and "REVIEWING" in ast.dump(node.value)
    ]
    assert reviewing and min(reviewing) < verdict_line, (
        f"没有先把卡推进 `REVIEWING`（赋值行 {reviewing}，写结论在第 {verdict_line} 行）⇒ 撞 `state_not_reviewing`"
    )


def test_the_stub_provider_marks_every_finding_as_simulation():
    """`StubFindingProvider` 交回的每条证据都自带仿真标记，且它不读正文。

    owner 裁 Q6 = 甲 时要求的诚实性在代码面上的钉子：**"R-07 的 findings 由 StubFindingProvider 固定返回
    pass，不构成 FR-029 的实证"** 这句话不能只写在纸上 —— 它必须落在 EN-08 那行的 `item_findings` 里，
    否则半年后查库的人会把一条仿真结论当成"审查岗真判过"。两格：① 每条 `evidence` 都以 `MARK` 开头；
    ② 条数与条目**原样等于卡上声明的标准**（不多不少）—— 这一支连正文都不在入参里
    （`FindingProvider.findings_for(card, criteria)` 没有 bodies 这一项），所以"它看了产物内容才判"
    这句话在结构上就说不成立。
    """
    card = SimpleNamespace(id="T01")
    findings = PROVIDER.findings_for(card, ["首屏要有标题", "样式表要被引用"])
    assert len(findings) == 2, findings
    for finding in findings:
        assert finding.met is True, f"这一支的定义就是固定 pass：{finding}"
        assert finding.evidence.startswith(PROVIDER.MARK), f"证据串没留仿真身份：{finding.evidence}"
    assert [f.criterion for f in findings] == ["首屏要有标题", "样式表要被引用"], "改了条目就与卡上声明脱钩（FR-029）"


# ---------------------------------------------------------------- 二、拒绝面


def test_a_card_without_a_lease_never_touches_the_database():
    """台账里没有这张卡 ⇒ `lease_missing`，且假会话一次调用都没被记上。

    这一发挡的是"执行段可以单独跑"这种想法：槽是指派段借的，凭据只在进程内的台账里活着，
    没有凭据还硬跑段，就是让一张没占槽的卡占着平台上限之外的额度跑起来（FR-021 的上限成装饰）。
    """
    session = Session_(armed=False)
    with pytest.raises(run_module.RunFailure) as caught:
        run_module.run_execute(
            uuid.uuid4(),
            "T01",
            transport="stub",
            bus_root=pathlib.Path("/tmp/never-touched"),
            session=session,
            now=NOW,
            findings_for=PROVIDER,
            slots=Slots(),
            leases=scheduler.LeaseLedger(),
        )
    assert caught.value.code == run_module.LEASE_MISSING, caught.value.args[0]
    assert session.touches == 0, f"拒绝之前碰了 {session.touches} 次库 ⇒ 段体没留在闭包里"


def test_an_unregistered_transport_is_refused_before_anything_else():
    """`--transport` 的门先说话：没显式选中就碰台账，表现会是"槽没有"而不是"运输方式没选"。

    门的本体在 `adapters/stub.py::resolve_transport_flag()`，本判据钉的是它在 `run_execute` 里确实是
    **函数本体的第一条语句**（M 系列：把它挪到 `run_card` 之后就红 —— 那一发台账还得先被 `take` 掉一次）。
    「第一条」按**去文档字符串之后**的第一条算：`ast.parse` 把 docstring 也摆成一条 `Expr`，
    不排除它，任何写了 docstring 的函数体都会让这条恒红 —— 那是判据写错，不是顺序写错。
    """
    body = func_body("run_execute")
    statements = [
        node
        for node in body.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    ]
    assert statements, "run_execute 的函数体除了 docstring 什么都没有"
    first = statements[0]
    assert isinstance(first, (ast.Expr, ast.Assign)) and isinstance(
        getattr(first, "value", first), ast.Call
    ), f"run_execute 的第一条语句不是调用，而是 {type(first).__name__} ⇒ 那道门被挪到后面去了"
    call = first if isinstance(first, ast.Call) else first.value
    assert getattr(call.func, "id", None) == "resolve_transport_flag", (
        f"第一条语句是 {ast.dump(call.func)[:80]} 而不是 resolve_transport_flag"
    )


# ---------------------------------------------------------------- 三、真 PG


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整节 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


def bus_root_of(bus) -> pathlib.Path:
    """`<根>/<项目>` 得是 git 顶层（T109 之后 `_normalize` 要真打一拍）—— 前两笔踩过的那格。"""
    project = bus.path / PROJECT
    project.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "."], ["config", "user.email", "probe@invalid"], ["config", "user.name", "probe"]):
        subprocess.run(
            ["git", *args], cwd=str(project), check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    return bus.path


def to_running(
    bus,
    session,
    *,
    slots: object | None = None,
    ledger: scheduler.LeaseLedger | None = None,
) -> tuple[uuid.UUID, str, str, scheduler.LeaseLedger, object]:
    """真把一条需求单推到**一张卡 RUNNING 且槽被占着**：受理 → 人批 → 恢复 → 指派。

    不手搓 `RUNNING` 的卡：那会把"派发真的占了槽、真的写了三岗"从被测面里挪走，
    而这两件事正是执行段的入场券。返回 (需求单号, 卡号, 执行者, 台账, 槽层)。

    `slots` / `ledger` 可注入（默认走 EN-03 现值装出来的真槽层）：要把"退了但没退干净"这种形状
    演出来，只能换一个 `release()` 静默 no-op 的槽层 —— 真实现里那一形不可达，为一条判据去改真槽层
    不划算，也不该。注入的是**同一条链上的同一个对象**，指派段占的槽就是执行段要还的那一格。
    """
    root = bus_root_of(bus)
    report = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=root, session=session, now=NOW)
    ticket = session.get(ApprovalTicket, report.ticket_id)
    repo.resolve(
        session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
        principal="owner-execute", approvers=frozenset({"owner-execute"}), now=NOW,
    )
    resumed = run_module.run_resume(report.feature_request_id, transport="stub", bus_root=root, session=session)
    assert resumed.waiting_after == (), resumed.waiting_after
    layer = slots if slots is not None else run_module._slots_from_registry(session)
    entries = ledger if ledger is not None else scheduler.LeaseLedger()
    assigned = run_module.run_assign(
        report.feature_request_id, transport="stub", session=session, now=NOW, slots=layer, leases=entries
    )
    assert len(assigned.dispatched) == 1 and assigned.held == ("T01",), assigned
    card_id, executor = assigned.dispatched[0][0], assigned.dispatched[0][1]
    return report.feature_request_id, card_id, executor, entries, layer


def drop_thread(session, feature_request_id: uuid.UUID) -> None:
    thread = cp.thread_id_for(kind="A_SPLIT", feature_request_id=feature_request_id)
    with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
        chaos.kill_graph_state(conn, thread)


def artifact_rows(session, request_id: uuid.UUID, card_id: str) -> list[Artifact]:
    return list(
        session.scalars(
            select(Artifact).where(Artifact.feature_request_id == request_id, Artifact.task_card_id == card_id)
        ).all()
    )


def verdict_rows(session, request_id: uuid.UUID, card_id: str) -> list[ReviewVerdict]:
    return list(
        session.scalars(
            select(ReviewVerdict).where(
                ReviewVerdict.feature_request_id == request_id, ReviewVerdict.task_card_id == card_id
            )
        ).all()
    )


def seat_blocked(session, card_id: str, *, since: int) -> dict[str, list[str]]:
    """`since` 之后那行 `seat_blocked_candidates` 留痕里的 `(被挡的工具 → 原文理由)`。

    它是 owner 裁 Q7 补第 2 条的落点（"原因不许只写成'换人失败'"）：库里必须能读出**哪两支根本没进候选集、
    为什么**。理由文本是 `guard/triple.py` 的原文，所以这一发断的是"含 `同时占了` 那句"，不是断我抄的一句。
    """
    rows = session.scalars(
        select(EventLog)
        .where(
            EventLog.entity == "task_card",
            EventLog.entity_id == card_id,
            EventLog.id > since,
        )
        .order_by(EventLog.id)
    ).all()
    out: dict[str, list[str]] = {}
    for row in rows:
        payload = row.payload or {}
        if payload.get("action") != run_module.SEAT_BLOCKED_ACTION:
            continue
        for item in payload.get("blocked") or ():
            out[str(item["agent_id"])] = [str(reason) for reason in item["reasons"]]
    return out


def escalated_reason(session, card_id: str, *, since: int) -> str:
    """`since` 之后那行 `escalated` 留痕**自己的** `reason` 字段（owner 2026-10-17 裁 Q7 乙要看的就是这一格）。

    它与 `seat_blocked()` 读的是两条不同事实：那条是"装配点记得谁被挡、为什么"，这一条是
    "`handoff()` 写下的升级理由里有没有写明为什么升级"。所以判据 MUST 读库里的 `payload["reason"]` ——
    读 `HandoffOutcome.reason` 是同一个字符串的另一个出口，读错那个等于没判。
    """
    rows = session.scalars(
        select(EventLog)
        .where(
            EventLog.entity == "task_card",
            EventLog.entity_id == card_id,
            EventLog.id > since,
        )
        .order_by(EventLog.id)
    ).all()
    for row in rows:
        payload = row.payload or {}
        if payload.get("action") == "escalated":
            return str(payload.get("reason") or "")
    return ""


def register_fourth(session) -> str:
    """往 EN-03/EN-05 克隆一支第四工具（本发事务内，收尾回滚）—— 让"换人成功"那一形**可达**。

    为什么必须补这一支：甲 的席位过滤在内置 3 支注册面上的读数是"合法候选恒为空"，
    而这个读数本身分不开两种实现 —— "按席位剔人"与"见谁剔谁"。补上一支不撞任何席位的工具，
    同一条链就必须真的换成人，过滤器才不是一枚恒关的开关。
    克隆而不是手搓：`form / cost_band / tier` 的合法取值由注册面的枚举定，抄现成的行比抄我的记忆稳。
    刻意在 `to_running()` **之后**调用：先让三岗被内置三支占满，第四支才是唯一合法候选。
    """
    fourth = "probe_fourth"
    unit = session.scalars(select(AgentUnit).where(AgentUnit.enabled.is_(True)).limit(1)).one()
    session.add(
        AgentUnit(
            agent_id=fourth,
            form=unit.form,
            tier=unit.tier,
            cost_band=unit.cost_band,
            max_parallel=unit.max_parallel,
            current_load=0,
            historical_success_rate=unit.historical_success_rate,
            success_rate_sample=unit.success_rate_sample,
            workspace_dir=layout.workspace_dir_name(fourth),
            enabled=True,
        )
    )
    declared = session.scalars(select(CapabilityManifest).limit(1)).one()
    session.add(
        CapabilityManifest(
            agent_id=fourth,
            capabilities=list(declared.capabilities),
            tier=declared.tier,
            cost_band=declared.cost_band,
            max_parallel=declared.max_parallel,
            declared_at=declared.declared_at + dt.timedelta(hours=1),
        )
    )
    session.flush()
    return fourth


def event_high_water(session, card_id: str) -> int:
    """这张卡在 EN-14 上当前的最大 `id`（没有行就 0）—— 给下一条函数当"起点"。"""
    return int(session.scalar(select(func.max(EventLog.id)).where(
        EventLog.entity == "task_card", EventLog.entity_id == card_id
    )) or 0)


def handoff_actions(session, card_id: str, *, since: int) -> list[str]:
    """`since` 之后由 `HandOff` 节点写出的动作名（那两个词是实体自己的两条出口：`handed_off` / `escalated`）。

    为什么读这一格而不是只读卡态：`ESCALATED` 与"这个节点根本没跑到"在卡态列上可能长得一样
    （都停在 `ARTIFACT_INVALID`），留痕里有没有那一行才把两者分开 —— 读动作名 = 读"走进了哪条出口"。
    为什么要 `since` 这个起点：EN-14 的作用域只有 `(entity, entity_id)`（`models_registry.py:117-118`），
    卡号 `(feature_request_id, id)` 的两段式主键在这张表上落不下 ⇒ 只按 `T01` 查会捞进**别的需求单**的
    同名卡（库里若有历史提交行就是脏读数）。按自增 `id` 的起点筛，是本发在不给这张表加 scoping 列之前
    唯一能把"这一次写的"与"库里原来有的"分开的办法；那一条 scoping 缺口登记在台账（不在本轮）。
    """
    rows = session.scalars(
        select(EventLog)
        .where(
            EventLog.entity == "task_card",
            EventLog.entity_id == card_id,
            EventLog.id > since,
        )
        .order_by(EventLog.id)
    ).all()
    return [str((row.payload or {}).get("action")) for row in rows if (row.payload or {}).get("node") == "handoff"]


def test_the_chain_runs_a_card_to_accepting_and_returns_the_slot(bus, session):
    """正读一整发：真下发 → 真回收 → 门禁 `valid` → 审查收结论 → 卡进 `ACCEPTING`，槽退干净。

    这一发是乙 的"机制成立"证人，七格各挡一种错：① `validated_status` / `artifact_count` 与 EN-06 行数
    三者相等（报告与库不许各说一份）；② `card_status_after == "ACCEPTING"`（`RUNNING → REVIEWING → ACCEPTING`
    那两跳真的走了，而不是停在 `RUNNING` 的甲 那轮形状）；③ `verdict_rows == 1` 且 `verdicts == ("APPROVE",)`；
    ④ 结论的**写者是卡上现值的审查岗**（不是平台身份、也不是执行者自己）；⑤ EN-08 那行的每条 `evidence`
    都带仿真标记（诚实性落到库里，不只在台账里）；⑥ `usage_after` 全零 + `pending_after` 空；
    ⑦ `seats_after` 三岗仍互不相同（审查链没顺手改岗）。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        rows = artifact_rows(session, request_id, card_id)
        assert (report.card_id, report.agent_id) == (card_id, executor), report
        assert report.validated_status == "valid", f"门禁在真链上没判达标：{report}"
        assert report.artifact_count == len(rows) and rows, (report.artifact_count, rows)
        assert report.card_status_after == "ACCEPTING", f"审查链没把卡推进验收态：{report.card_status_after}"
        assert (report.verdict_rows, report.verdicts) == (1, ("APPROVE",)), report
        verdict = verdict_rows(session, request_id, card_id)[0]
        assert verdict.reviewer_agent_id == report.seats_after[1], (
            f"结论写者不是卡上现值的审查岗：{verdict.reviewer_agent_id} vs {report.seats_after[1]}"
        )
        assert verdict.extra_criteria_used is False, verdict
        marked = [item["evidence"] for item in verdict.item_findings]
        assert marked and all(str(text).startswith(run_module.StubFindingProvider.MARK) for text in marked), marked
        declared = list(session.get(TaskCard, (request_id, card_id)).acceptance_criteria)
        assert [item["criterion"] for item in verdict.item_findings] == declared, (
            f"逐条判定的条目与卡上声明的标准不一致：{declared} vs {verdict.item_findings}"
        )
        assert report.usage_after == (("doubao", 0), ("qoder", 0), ("workbuddy", 0)), report.usage_after
        assert report.pending_after == (), report.pending_after
        assert len(set(report.seats_after)) == 3, report.seats_after
    finally:
        drop_thread(session, request_id)


def test_a_stale_artifact_row_from_the_previous_round_is_not_reviewed(bus, session):
    """同一张卡上留着上一轮那一行（文件已不在总线上）⇒ 本轮审查只读本轮那一批，链子照样跑到 `ACCEPTING`。

    这一发是上一条判据的**行为侧证人**，因为 M59 第一次打过来的时候 AST 那条没红（改写理由记在
    `test_the_reviewer_reads_this_rounds_batch_not_the_whole_card` 里）。形状选"上一轮的文件已经没了"：
    `gateway.read()` 对不存在的路径抛的是 `FileNotFoundError`（`bus/gateway.py:278`，它只要求路径存在，
    不返回空串），所以"读整卡"这一形在这一发上是**会炸的**，而"只读本轮 id"这一形必须稳稳过。
    另外三格读数：① 卡进 `ACCEPTING`；② EN-06 这一发结束时是**两行**（本轮一行 + 上一轮那一行还在），
    证明我没有顺手把旧行删掉来让链路好看；③ EN-08 那行的逐条判定条目数等于卡上声明的标准数
    （正文只来自本轮那一份，标准却一条没少）。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        session.add(
            Artifact(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                task_card_id=card_id,
                writer_agent_id=executor,
                rel_path=f"{layout.workspace_dir_name(executor)}/previous-round.css",
                bytes=4_096,
                lines=40,
                format="css",
                self_reported_status="success",
                validated_status="valid",
                created_at=NOW,
            )
        )
        session.flush()
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert report.card_status_after == "ACCEPTING", (
            f"审查把上一轮那行也读了（那文件不在总线上）：{report.card_status_after}"
        )
        rows = artifact_rows(session, request_id, card_id)
        assert len(rows) == 2, f"本轮跑完应当是两行（旧行不许被顺手删）：{len(rows)}"
        verdict = verdict_rows(session, request_id, card_id)[0]
        declared = list(session.get(TaskCard, (request_id, card_id)).acceptance_criteria)
        assert len(verdict.item_findings) == len(declared), (verdict.item_findings, declared)
        assert report.validated_status == "valid" and report.verdicts == ("APPROVE",), report
    finally:
        drop_thread(session, request_id)


def test_a_card_without_a_findings_source_stops_before_writing_a_verdict(bus, session):
    """没有结论来源 ⇒ `findings_source_missing`；产物与门禁结论照落，但**卡态仍 `RUNNING`、EN-08 零行**、槽回来。

    这一发钉三件事：① 缺来源的拒绝发生在**写任何结论之前**（卡不会被推进到 `REVIEWING` 再停在那儿 ——
    那个中间态谁也接不上，且 `REVIEWING` 有第二条入边，停在那里的卡看起来像"审查正在做"）；
    ② 它不吞掉前面的真工作：门禁判过了、EN-06 有行，所以这一发的读数是"干到哪儿停住了"而不是"什么都没干"；
    ③ 段内拒绝走的是同一条 `finally`。这一形是甲 那轮"缺一个下游"那一条的替代证人。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        with pytest.raises(run_module.RunFailure) as caught:
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger, findings_for=None)
        assert caught.value.code == run_module.FINDINGS_MISSING, caught.value.args[0]
        assert session.get(TaskCard, (request_id, card_id)).status == "RUNNING", (
            "缺结论来源那一发把卡推进了 REVIEWING ⇒ 留下一个没人接的中间态"
        )
        assert verdict_rows(session, request_id, card_id) == [], "没有来源却写出了结论"
        assert artifact_rows(session, request_id, card_id), "前面的真工作不该被这一发抹掉"
        assert slots.usage() == ZERO and ledger.pending() == (), (slots.usage(), ledger.pending())
    finally:
        drop_thread(session, request_id)


def test_a_verdict_that_dropped_a_criterion_is_refused_and_leaves_the_card_running(bus, session):
    """审查少判一条 ⇒ 由 `check_findings` 拒在入库前（`findings_not_compliant`），卡态仍 `RUNNING`。

    甲 那轮的报告里 `validated_status` 全绿过一次是**硬填**的（M41 那次），这一发的作用是：结论面的
    "少一条"必须让整发停住，而不是"写了个结论但少一行 findings"。它同时是 `check_findings` 在链路上
    真的被调到的证人（现算：把那一处调用摘掉，`record_verdict` 内部会抛 `findings_not_compliant` 的
    `ReviewMisuse` 而不是 `RunFailure` ⇒ 这一发红在异常类型上）。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        with pytest.raises(run_module.RunFailure) as caught:
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger, findings_for=DropsOneCriterion())
        assert caught.value.code == "findings_not_compliant", caught.value.args[0]
        assert "没有对应行" in caught.value.args[0], caught.value.args[0]
        assert session.get(TaskCard, (request_id, card_id)).status == "RUNNING"
        assert verdict_rows(session, request_id, card_id) == [], "不合规的结论入库了（FR-029 + 库面 CHECK 都该拦）"
        assert slots.usage() == ZERO, slots.usage()
    finally:
        drop_thread(session, request_id)


def test_a_card_below_the_floor_takes_the_handoff_exit_and_escalates_when_no_seat_safe_tool_exists(bus, session):
    """不达产物下限那张卡走**另一条**出边；内置 3 支注册面下没有合法候选 ⇒ 进 `ESCALATED`，且三岗仍两两不同。

    这一发读的是 Q7（FR-031 × 换人）按 甲 落地后的形状 —— **owner 2026-10-17 裁 Q7 补第 1 条的"正"半边**
    （内置 3 支下 `ARTIFACT_INVALID` 那条出边 MUST 进 `ESCALATED`，不许静默失败）与**第 2 条**（被挡的原因必须可查），
    六格各挡一种错：
    ① `validated_status == "invalid"`（这一格的反向证人，甲 那轮的 M41 就钉在这）；
    ② 真的走进了 `handoff()` 那一条出口 —— 用 EN-14 的 `action == "escalated"` 证，而不是只看卡态字符串
       （"什么都没发生"与"发生了但被拦下"在卡态上都可能长得像）；
    ②b **被挡的两支有名字**：同一条时间线上先有一行 `seat_blocked_candidates`，逐支带上 `guard/triple.py`
       的原文理由（含"同时占了"那句）⇒ "没有可替代的工具"这件事在库里是可归因的，不是一句泛化的失败；
    ②c **升级那一行自己的 `reason` 也写着席位原因**（owner 2026-10-17 裁 Q7 乙，授权动 `handoff.py`）：
       库里 `action=escalated` 那行的 `payload["reason"]` MUST 含"席位占用"，且带 `triple.py` 的原文理由 ——
       审计只看这一行时也该知道"为什么没人可换"，不是只看到一句泛化的换人失败；
    ③ **执行岗没被人替换**：`_escalate()` 的口径是"不动执行者、不动次数"（`handoff.py:125-129`），
       所以 `seats_after[0]` 必须还是派发时那个人、`handoff_count_after` 必须是 0；
    ④ FR-031 仍然成立 —— 这一格是 甲 存在的全部理由。判"撞不撞岗"用的是 triple.py 那个入口本身，
       不是我另写一份比较（`test_the_seat_filter_asks_the_existing_carrier...` 钉的是实现面，这一发钉的是结果面）；
    ⑤ 槽照样退干净、结论一条不写（审查那条出边没走）。

    诚实性：内置注册面恰 3 支、三岗又被这 3 支占满 ⇒ 合法候选恒为空，所以**这一形今天换不成**。
    "换人成功"那一格由 `test_a_card_below_the_floor_really_switches_when_a_fourth_tool_is_registered` 补
    （= 第 1 条的"反"半边），两格合起来才是 甲 的完整读数（台账 §十四 明写这一对）。
    曾经差的那一格已在本轮补上：`ESCALATED` 那一行**自己的** `reason` 现在含席位原因（= 上面 ②c，
    owner 裁 Q7 乙 授权动 `handoff.py`）；它的反向证人不在这里，在
    `test_handoff.py::test_no_replacement_available_escalates_with_the_exclusion_list_rather_than_vanishing`
    —— 那一形没有候选被席位挡掉，所以那句"席位占用"一个字都不许出现。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        card = session.get(TaskCard, (request_id, card_id))
        card.artifact_floor_min_bytes = 10_000_000
        session.flush()
        since = event_high_water(session, card_id)
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert report.validated_status == "invalid", f"门禁没判出'不达标'：{report}"
        assert handoff_actions(session, card_id, since=since) == ["seat_blocked_candidates", "escalated"], (
            f"没走到 `handoff()` 的升级出口：{report.card_status_after}"
        )
        blocked = seat_blocked(session, card_id, since=since)
        assert sorted(blocked) == sorted({report.seats_after[1], report.seats_after[2]}), (
            f"被挡的应当正是占着审查/验收岗的那两支：{blocked} / seats={report.seats_after}"
        )
        assert all(any("同时占了" in reason for reason in reasons) for reasons in blocked.values()), blocked
        reason_text = escalated_reason(session, card_id, since=since)
        assert "席位占用" in reason_text, f"升级那一行自己的 reason 没写席位原因（owner 裁 Q7 乙）：{reason_text!r}"
        assert "同时占了" in reason_text, f"reason 里没带 `triple.py` 的原文理由：{reason_text!r}"
        assert "没有可替代的工具可换" in reason_text, f"把旧那句挤掉了：{reason_text!r}"
        assert str(gp.SEATS) in reason_text, f"岗名不是从 `triple.py` 那份 SEATS 渲染的：{reason_text!r}"
        assert report.card_status_after == "ESCALATED", report.card_status_after
        assert report.handoff_count_after == 0, f"升级那一发不该计换人次数：{report.handoff_count_after}"
        assert report.seats_after[0] == executor, f"升级把执行者也换了：{report.seats_after}"
        assert len(set(report.seats_after)) == 3, f"三岗相撞（FR-031）：{report.seats_after}"
        row = session.get(TaskCard, (request_id, card_id))
        assert gp.distinctness_violations(
            gp.Assignment(
                card_id=card_id,
                occupants={
                    "executor": gp.SeatOccupant(row.executor_agent_id, None),
                    "reviewer": gp.SeatOccupant(row.reviewer_agent_id, None),
                    "acceptor": gp.SeatOccupant(row.acceptor_agent_id, None),
                },
            )
        ) == (), f"库里的三元组过不了 FR-031 那一道：{report.seats_after}"
        assert (report.verdict_rows, report.verdicts) == (0, ()), report
        assert report.usage_after == (("doubao", 0), ("qoder", 0), ("workbuddy", 0)), report.usage_after
        assert report.pending_after == (), report.pending_after
    finally:
        drop_thread(session, request_id)


def test_a_card_below_the_floor_really_switches_when_a_fourth_tool_is_registered(bus, session):
    """补一支不撞席位的第四工具 ⇒ 同一条链必须真的换成人：`HANDED_OFF` + 次数 1 + 三岗仍互不相同。

    这一发是上一发的**反向对照**，也是 甲 的过滤器不是恒关开关的唯一证人：内置 3 支下"合法候选为空"
    这个读数，把 `_seat_safe_candidates()` 写成"一律返回空列表"同样能过 —— 只有第四支在场时换成成功，
    才把两者分开。四格读数：① `HANDED_OFF`（`handoff.LANDED`）；② `handoff_count_after == 1`；
    ③ 新执行者就是第四支（不是内置那三支里的任何一支）；④ 三岗仍两两不同 —— 这一格就是探针里
    那个 `('doubao','doubao','workbuddy')` 的形状，它现在必须是可红的。

    顺带把两重排除的分工也钉住：`qoder` 是被 `handoff()` 自己按"在场者"排除的（`handoff.py:214`），
    我这层只按席位排除 —— 所以第四支是**唯一**活下来的候选，选择没有歧义。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        fourth = register_fourth(session)
        card = session.get(TaskCard, (request_id, card_id))
        card.artifact_floor_min_bytes = 10_000_000
        session.flush()
        since = event_high_water(session, card_id)
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert report.card_status_after == "HANDED_OFF", report.card_status_after
        assert report.handoff_count_after == 1, report.handoff_count_after
        assert report.seats_after[0] == fourth, f"换人换到的不是那支唯一合法的候选：{report.seats_after}"
        assert report.seats_after[0] != executor and len(set(report.seats_after)) == 3, report.seats_after
        assert handoff_actions(session, card_id, since=since) == ["seat_blocked_candidates", "handed_off"], (
            "第四支在册还走升级出口 ⇒ 席位过滤器被写成了恒空"
        )
        assert sorted(seat_blocked(session, card_id, since=since)) == ["doubao", "workbuddy"], (
            "被挡的两支应当正是那一对席位占用者（`qoder` 由 `handoff()` 按在场者排除，不在这一行里）"
        )
        assert (report.verdict_rows, report.verdicts) == (0, ()), report
        # 槽层是 `to_running()` 里按当时的 EN-03 现值装出来的（第四支在那之后才进册），
        # 所以这里只有内置三支的键 —— 这一格要读的是"全零"，不是"键集等于注册面"。
        assert report.usage_after == (("doubao", 0), ("qoder", 0), ("workbuddy", 0)), report.usage_after
    finally:
        drop_thread(session, request_id)


def test_a_producer_that_raises_still_returns_the_slot(bus, session):
    """下发这一步抛（仿真侧认不出交付物名）⇒ 槽照样回来，而且**库里一行产物都没有**。

    这一发是 `run_card()` 从 `result=` 换成 `produce=` 的**唯一证人**：产结果被挪回 `run_card()` 之前，
    这里就会在 `StubMisuse` 之后读到仍占着的那一格（`{"qoder": 1}`）与台账里那条凭据 —— 而装配点被
    owner 明令不许自己退槽，于是这一发就是那种"没人会看见的泄漏"唯一的显形机会。
    形状选在"卡上声明的标准里没有文件名"：那是真会发生的输入错误（拆单时没点名交付物）。
    错误码引 `stub` 模块的常量而不写字符串字面量：仿真侧有两个可混淆的拒法（`UNNAMED_DELIVERABLE` /
    `BODY_MISSING`），这一发要钉的是"走的是哪一个"，写死第一版的 `"unnamed_deliverable"` 反而是我照抄自己
    的错名 —— 真常量是 `transport_deliverable_unnamed`（`stub.py:49`）。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        card = session.get(TaskCard, (request_id, card_id))
        card.acceptance_criteria = ["配色要克制", "文字要短"]
        session.flush()
        with pytest.raises(StubMisuse) as caught:
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert caught.value.code == stub.UNNAMED_DELIVERABLE, caught.value.args[0]
        assert card_id in caught.value.args[0], f"错误里没点名是哪张卡：{caught.value.args[0]}"
        assert slots.usage() == ZERO, f"产结果抛错却没退槽：{slots.usage()}"
        assert ledger.pending() == (), ledger.pending()
        assert artifact_rows(session, request_id, card_id) == [], "下发都还没成就写了产物行"
        assert session.get(TaskCard, (request_id, card_id)).status == "RUNNING"
    finally:
        drop_thread(session, request_id)


def test_a_recycled_result_naming_another_card_is_refused_inside_the_segment(bus, session, monkeypatch):
    """回收面上写的是别的卡号 ⇒ 段内拒（`IngestMisuse`），**槽照样回来、台账照样空、库里零行**。

    乙 之后结果由装配点现产，所以"对不上卡"这一形只能从适配器那一侧演：把 `run.py` 名字空间里的
    `StubAdapter` 换成一支会谎报 `task_id` 的壳（真厂商会这么错）。两条口径同时钉住：① "结果对不上卡"
    由 `record_result` 拒，装配点不另写一份撮合逻辑（`run.py` 里出现 `if result.task_id != task_id`
    就是第二份判定）；② 段内拒绝走同一条 `finally`。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    real = run_module.StubAdapter

    class Lying:
        def __init__(self, *, agent_id, gateway, clock=None) -> None:
            self._inner = real(agent_id=agent_id, gateway=gateway, clock=clock)

        def submit(self, task):
            return self._inner.submit(task)

        def collect(self, handle):
            return self._inner.collect(handle).model_copy(update={"task_id": "T99"})

    monkeypatch.setattr(run_module, "StubAdapter", Lying)
    try:
        with pytest.raises(IngestMisuse) as caught:
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert card_id in caught.value.args[0] and "T99" in caught.value.args[0], caught.value.args[0]
        assert slots.usage() == ZERO, slots.usage()
        assert ledger.pending() == () and artifact_rows(session, request_id, card_id) == []
    finally:
        drop_thread(session, request_id)


def test_the_bookkeeper_is_the_cards_current_executor_even_when_the_lease_names_another(bus, session):
    """派发之后把卡上的执行者改掉 ⇒ 段用的是**库里现值**，而报告里的 `agent_id` 仍是台账那一刻的。

    这一发故意制造一个分叉，为的是把两件事钉住：① 记账人取现值（改成 `lease.agent_id` 就会被
    `record_result` 拒成 `writer_not_executor`，FR-033）；② 报告里的 `agent_id` 来自凭据而不是库 ——
    **这两个数在有换人的那一发上就是不一样**。差额本身是㉖/㉘ 那一族要处置的形状（换人时那格槽
    还挂在旧工具名下），这里只把它量出来并写进台账，不在本笔顺手"修"掉。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    other = next(name for name in ("workbuddy", "doubao") if name != executor)
    try:
        card = session.get(TaskCard, (request_id, card_id))
        card.executor_agent_id = other
        session.flush()
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert report.agent_id == executor, f"报告把凭据里的执行者换了：{report.agent_id} != {executor}"
        assert {row.writer_agent_id for row in artifact_rows(session, request_id, card_id)} == {other}, (
            "EN-06 的记账人不是库里现值的那个执行者 ⇒ 装配点读了凭据而不是卡"
        )
        assert [count for _agent, count in report.usage_after] == [0, 0, 0], report.usage_after
    finally:
        drop_thread(session, request_id)


def test_a_second_run_execute_is_refused_and_leaves_the_library_untouched(bus, session):
    """同一张卡第二发 ⇒ `lease_missing`（凭据已被 `take`），且 EN-06 与 EN-08 一行都不许多出来。

    这一条是"回收只能一次"的形状证人：台账 `take` 是**取走**而不是查一下，所以执行段天然不可重放。
    如果哪天有人把 `take` 改成 `peek`，这一发就红 —— 而库里多出来的那几行产物与那条重复的结论
    会先脏在读数里（乙 之后连结论都可能被写第二次，所以两张贴数一起对）。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        before = (len(artifact_rows(session, request_id, card_id)), len(verdict_rows(session, request_id, card_id)))
        with pytest.raises(run_module.RunFailure) as caught:
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert caught.value.code == run_module.LEASE_MISSING, caught.value.args[0]
        after = (len(artifact_rows(session, request_id, card_id)), len(verdict_rows(session, request_id, card_id)))
        assert after == before and before[1] == 1, f"第二发把段跑起来了：{before} → {after}"
        assert slots.usage() == ZERO, slots.usage()
    finally:
        drop_thread(session, request_id)


def test_a_findings_source_that_raises_still_returns_the_slot(bus, session):
    """结论来源抛 ⇒ 原异常照抛、槽照退（真槽层）。这就是 `finally` 而不是"成功路径末尾"的理由。

    owner 点名要防的那件事（一次失败泄漏一格槽，跑几次把并发上限吃光）在这一发上是**可红**的：
    把退槽挪回成功路径，这里就会在 `RuntimeError` 之后读到仍占着的 `{"qoder": 1}`。
    抛点选在审查那一步：此时 EN-06 已经写过行，所以这条同时说明"写了一半"不影响收尾。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        with pytest.raises(RuntimeError):
            execute(bus, session, request_id, card_id, slots=slots, ledger=ledger, findings_for=ExplodingProvider())
        assert sum(slots.usage().values()) == 0, f"段内抛错却没退槽：{slots.usage()}"
        assert ledger.pending() == (), ledger.pending()
    finally:
        drop_thread(session, request_id)


def test_an_unreturned_slot_is_reported_as_a_leak_and_keeps_the_original_error(bus, session):
    """段内抛错**且**槽没退干净 ⇒ 报的是 `SlotLeak`，原异常挂在 `__context__` 上不丢。

    两种错同时发生时，泄漏更该被看到：前者是一次任务失败，后者是"后面所有卡都会静默阻塞"的故障源。
    注入的是 `release()` 静默 no-op 的槽层（真实现那一形不可达）；整条链都用它，所以指派段占的那一格
    就是执行段退不掉的那一格。这条也是"哨兵写在 `finally` 里"的反证 —— 哨兵挪到成功路径，这里只剩 RuntimeError。
    """
    leaky = Slots(leaky=True)
    request_id, card_id, executor, ledger, _layer = to_running(bus, session, slots=leaky)
    try:
        assert leaky.usage()[executor] == 1, f"指派段没在假槽层上留下占用：{leaky.usage()}"
        with pytest.raises(scheduler.SlotLeak) as caught:
            execute(bus, session, request_id, card_id, slots=leaky, ledger=ledger, findings_for=ExplodingProvider())
        assert card_id in caught.value.args[0], caught.value.args[0]
        assert isinstance(caught.value.__context__, RuntimeError), (
            f"原异常没挂在 __context__ 上 ⇒ traceback 里只剩泄漏一段：{caught.value.__context__!r}"
        )
        assert ledger.pending() == (), f"退槽之前凭据没被取走：{ledger.pending()}"
    finally:
        drop_thread(session, request_id)


def test_finishing_one_card_does_not_return_another_cards_slot(bus, session):
    """台账里另有一张卡的凭据时，跑完 T01 只许退 T01 那一格 —— `pending_after` 必须还剩 T02。

    这条是 `pending_after` / `usage_after` 两格的**非空证人**：前面几发跑完两张读数都正好是空，
    把读数写死成 `()` / `{}` 也照样绿（我第一轮就是这个形状，发现于写 M37 之前）。这里往同一个
    台账里再放一张 T02 的凭据（真槽层真占一格），于是：① `pending_after` 必须是 `("T02",)`；
    ② `usage_after` 里另一支工具必须是 1、T01 那一支必须是 0；③ 台账本身也还认这张凭据。
    任何"收尾时把整层清空"的写法都会在这里红，而那正是平台上限被悄悄多还一格的样子。
    """
    request_id, card_id, executor, ledger, slots = to_running(bus, session)
    try:
        other_agent = "doubao" if executor != "doubao" else "workbuddy"
        ledger.put(lease_of("T02", other_agent, slots=slots))
        assert slots.usage()[other_agent] == 1, slots.usage()
        report = execute(bus, session, request_id, card_id, slots=slots, ledger=ledger)
        assert report.pending_after == ("T02",), f"T01 收尾把别人的凭据也交了：{report.pending_after}"
        assert dict(report.usage_after) == {
            "doubao": 1 if other_agent == "doubao" else 0,
            "qoder": 0 if executor == "qoder" else 1,
            "workbuddy": 1 if other_agent == "workbuddy" else 0,
        }, report.usage_after
        assert ledger.pending() == ("T02",), ledger.pending()
    finally:
        drop_thread(session, request_id)
