"""T031 + T036 + T048：把 `Plan → Split → Normalize → ApprovalGate` 接进图并跑出第一次真实状态迁移。

节点集合是四个，逐字取自 `plan.md` 的 "Source Code" 树（`Plan/Split/Normalize/AssignGuards/…/ApprovalGate`）。
**Phase 3 收口时这里是三个节点**（"`ApprovalGate` 属 Phase 4"写在当时那版的模块头里），
Phase 4 把第四个接上 ⇒ 本段改写为现状；`test_graph_has_exactly_the_documented_nodes_and_plain_edges`
跟着改成四元素，那条判据仍然会拦住"多加一个没人认领的节点"这件事。
写库这件事放在 `_normalize` 里而不是另起一个 `record` 节点，理由与
`data-model.md §4` 把 FR-005 的后果写成"退回重排，不入 `READY`"是同一句：**校验不过就不建行**，
所以"通过校验"与"建行"必须发生在同一个判定之后、同一个工作单元之内，拆成两个节点反而会让
中间那个状态（已校验、未入库）成为一个可被观察却无人为它负责的窗口。

`Plan → Split` 这条边（T031）**不设等待人**：`add_edge` 而不是条件边，也不是 `interrupt`。
FR-003 的原文是"在使用者不回答任何澄清问题的情况下仍能继续推进，**不得阻塞等待答复**"，
所以这条边上任何形式的"等一等"都是违例。唯一的推进条件是"摘要合法"，而它写在 `_plan` 节点里
（不合法即 `IncompleteDigest` 抛出）—— 写成条件边就是把同一条规则放在两处（原则 I）。
判据侧对应两条：`test_zero_answers_still_advances_to_split`（一条澄清没答也要走到 `SPLIT`，
且返回的 state 里**没有** `__interrupt__`）与变异对照（把这条边换成 `interrupt` 必然翻红）。

**为什么 `_plan` 不重新调用补全器**：摘要已由入口（T029 / T030）算好并写进 EN-01。图里再跑一次
补全 = 同一份需求两次模型调用（FR-059 / FR-060 的用量是要记账的，不是免费的），
而且 `summary_digest` 这一列会出现两个写者。所以 `_plan` 在本 MVP-1 路径上的职责是
"**取回并核形状**"：从 EN-01 读摘要、按 FR-002 的同一判据再核一次（`validate_summary_digest`
是那个形状的唯一强制点，两处共用它而不是各写一份），然后把摘要交给 Split。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import cast
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy.orm import Session

from acp.archive import cadence
from acp.archive.root import require_archive_root
from acp.orchestrate.nodes.approval_gate import GATE
from acp.orchestrate.nodes.approval_gate import KIND as GATE_KIND
from acp.orchestrate.nodes.approval_gate import CheckpointerNotConfigured, approval_gate
from acp.orchestrate.nodes import assign_guards, dispatch as dispatch_node, select_worker
from acp.orchestrate.scheduler import LeaseLedger
from acp.bus import layout as bus_layout
from acp import routing as routing_lib
from acp.orchestrate.nodes.normalize import normalize
from acp.orchestrate.nodes.plan import validate_summary_digest
from acp.orchestrate.nodes.prefilled import DOC_WRITER_AGENT_ID, doc_cards
from acp.orchestrate.nodes.result_ingest import record_result
from acp.orchestrate.nodes.split import CardSplitter, split_with_acceptance_map
from acp.orchestrate.nodes.validate_artifact import (
    INVALID,
    OUT_EDGES,
    VALID,
    route_after_validation,
    validate_card,
)
from acp.orchestrate.state import (
    AssignmentState,
    AssignmentStateModel,
    ExecutionState,
    ExecutionStateModel,
    IntakeState,
    IntakeStateModel,
    ensure_state,
)
from acp.persistence import events
from acp.persistence import snapshots as snapshot_store
from acp.reporting.cards import FAMILY_DOC, add_done_card, verify_card
from acp.persistence.checkpointer import thread_config
from acp.persistence.models_approval import AcceptanceMap
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.review.policy import REVIEW_MODE_MANUAL

#: `validate_artifact.OUT_EDGES` 的值（下一跳的名字）与上面的节点名是同一对口径，
#: 所以这里**从那张表取值**而不是再写两个字符串字面量 —— 表与图各写一份时，
#: "改了出边名字忘了改图"就是 T076 那条判据要挡的东西。
#: 取法按"结论 → 跳"的方向点名，不用 `sorted(OUT_EDGES.values())`：排序后是
#: `["handoff", "review"]`，那两个名字会正好互换，而图照样能跑（两条边都还在）⇒ 这是个静默错。
VA_REVIEW = OUT_EDGES[VALID]
VA_HANDOFF = OUT_EDGES[INVALID]

PLAN = "plan"
SPLIT = "split"
NORMALIZE = "normalize"
APPROVAL_GATE = GATE

#: 这批卡是怎么来的（EN-01 `split_source` 的唯一取值域）。
#: - `llm` = 真模型拆的；`prd` = 按 PRD 确定性投影（不调模型）；
#: - `fallback_stub` = 模型几次都没过契约、回落通用清单；`stub` = 直接注入的确定性拆单器。
#: `fallback_stub` / `stub` 是**降级**：屏上要显眼标出来，库里的 EN-14 要留 `split_fallback`。
#: 2026-10-31 空库真跑：`.env` 那把豆包 key 已失效（`/models` 401），系统静默用了 Stub ——
#: 卡上判据是模板句、`meeting_note` 0 行，而读面上读不出"这单没接真模型"。
SPLIT_SOURCES = ("llm", "prd", "fallback_stub", "stub")
DEGRADED_SPLIT_SOURCES = frozenset({"fallback_stub", "stub"})

# EN-01 上"可以被这条链推进"的前置状态。别的状态（SPLIT 及以后）再跑一次就是重复拆单。
ADVANCEABLE = "INTAKE"


class RequestNotFound(ValueError):
    """图要推进的需求单不在 EN-01 里。"""


class AlreadyAdvanced(ValueError):
    """需求单已经走过拆单。消息里只带状态名，不带需求原文。"""


class MissingSession(ValueError):
    """整条链必须共用一个工作单元；拿到 None 说明装配方没给会话。"""


@dataclass(frozen=True)
class OrchestrateDeps:
    """一次运行的注入面：会话 + 拆单器 + checkpointer。

    **`session` 是单个会话而不是工厂**，这是刻意的：一条链的四个节点必须在同一个工作单元里写，
    工厂（每次调用给一个新会话）会让每个节点各自开一份 unit of work，于是 `_normalize` 写的行
    与 `_plan` 读的行不在同一份未提交状态里，而**提交点又归装配方**（T123）—— 结果就是
    "跑完什么都没落库"这种最难查的绿。会话的 `commit` / `close` 一律由调用方做，节点只 `flush`。
    **拆单器与入口的补全器同一条口径：没有默认实现**（实体 = Phase 3 的适配器），缺它即
    `SplitNotConfigured`，不降级成"看起来像拆单结果的固定清单"。

    `checkpointer` 在 Phase 4 起是**必需**的（D-9：卡点 = `interrupt` + PG checkpointer）。
    为什么不是"可以不给"：实测 `compile()` 时不接 checkpointer，`interrupt()` **不报错**，
    只是返回一个带 `__interrupt__` 的结果而没有任何可恢复的现场 ——
    那条链看起来"卡在人工审批"，实际上下一次 `Command(resume=…)` 无处可续，
    是宪法 V 禁的那种"重启恢复"的伪装形态。所以缺它就在装配期抛 `CheckpointerNotConfigured`。
    生产用 `persistence/checkpointer.py` 的 `PostgresSaver`；用例可以注入内存版，
    但**验"真实挂起 / 恢复"的那几条判据 MUST 用 PG 版**（`tests/guards/test_approval_resume.py`）。

    `bus_root` 是**总线根**（T109 / T-4 定稿：存档根 = `<bus_root>/<project_key>`，解析只走
    `archive/root.py`，权威出处 `config/params-ref.md §10`）。Phase 2 ~ Phase 8 没有这个参数，
    因为那时 `_normalize` 写的是指向登记行自身的临时句柄，用不着知道存档根在哪儿。换成真实句柄之后
    拆单这一步**必须**有一个真仓库可指：这个参数不给、`ACP_BUS_ROOT` 又没设时，`_normalize` 抛
    `ArchiveRootMisuse` 而整条 `invoke` 失败（调用方的事务不提交 ⇒ 库里一行新东西都没有），
    而不是静默退回那句自指假话 —— "看起来跑完了、而库里那行指不到任何东西"比一次明确的失败难发现得多。
    """

    session: Session
    splitter: CardSplitter | None
    checkpointer: BaseCheckpointSaver | None = None
    bus_root: Path | None = None


def _require_session(deps: OrchestrateDeps) -> Session:
    if deps.session is None:
        raise MissingSession("编排链需要一个共享会话；装配方（T123）按运行创建后传进来")
    return deps.session


def _request_row(session: Session, feature_request_id: uuid.UUID) -> FeatureRequest:
    """按需求单号取 EN-01 那一行；没有就抛 `RequestNotFound`。

    单独成函数是为了让"取不到"只有一种写法：`session.get()` 的返回是 `FeatureRequest | None`，
    拆单链上有四个节点各自取过一次，四处各写一遍 `if row is None` 就会漂成四种消息，
    而其中一处忘了写就是一次 `AttributeError: 'NoneType' object has no attribute 'project_key'` ——
    那条异常既看不出是哪个单号，也看不出该去查什么。
    """
    row = session.get(FeatureRequest, feature_request_id)
    if row is None:
        raise RequestNotFound(f"EN-01 里没有需求单 {feature_request_id} ⇒ 先走 POST /requests")
    return row


def _plan(deps: OrchestrateDeps) -> Callable[[IntakeState], IntakeState]:
    def node(state: IntakeState) -> IntakeState:
        # P1-2：入口先过运行时校验。`TypedDict` 在运行时什么都不拦，脏数据会一路走到底、
        # 在别处以别的样子炸（详见 `state.py` 那段）。
        checked = ensure_state(IntakeStateModel, state, at=PLAN)
        session = _require_session(deps)
        row = _request_row(session, checked.feature_request_id)
        if str(row.status) != ADVANCEABLE:
            raise AlreadyAdvanced(
                f"需求单当前状态是 {row.status}，不是 {ADVANCEABLE} ⇒ 这条链只推进尚未拆单的需求单"
            )
        return {
            "summary_digest": validate_summary_digest(dict(row.summary_digest)),
            "clarification_waived": bool(row.clarification_waived),
        }

    return node


def _split_source_of(splitter: object) -> str:
    """拆单器自报的来源。没有 `last_source` 这一格的对象 = 直接注入的确定性拆单器 ⇒ `stub`。"""
    raw = str(getattr(splitter, "last_source", "") or "").strip().lower()
    return raw if raw in SPLIT_SOURCES else "stub"


def _split(deps: OrchestrateDeps) -> Callable[[IntakeState], IntakeState]:
    def node(state: IntakeState) -> IntakeState:
        # FR-004 与 FR-007 出自同一次拆单：一批卡 **与** 它们的验收映射（T034）。
        drafts, links = split_with_acceptance_map(state["summary_digest"], splitter=deps.splitter)
        # 降级必须留痕（EN-01 `split_source` + EN-14 `split_fallback`）：卡片是 Stub 给的通用清单时，
        # 用户与排障者都该一眼看见"这单没接真模型"，而不是从模板句反推。
        source = _split_source_of(deps.splitter)
        session = _require_session(deps)
        request = _request_row(session, state["feature_request_id"])
        request.split_source = source
        if source in DEGRADED_SPLIT_SOURCES:
            events.record(
                session,
                entity="feature_request",
                entity_id=str(request.id),
                kind="split_fallback",
                actor_agent_id=select_worker.AUTOMATED_ACTOR,
                payload={
                    "node": SPLIT,
                    "source": source,
                    "reason": str(getattr(deps.splitter, "last_error", "") or ""),
                },
                created_at=dt.datetime.now(dt.timezone.utc),
            )
        return {"drafts": list(drafts), "links": list(links)}

    return node


def _persist_doc_cards(
    session: Session,
    request: FeatureRequest,
    deps: OrchestrateDeps,
) -> int:
    """把三份定稿文档落成「已完成卡」（T90–T92），返回落了几张。

    **为什么不给它们派单、不给它们排审批**：这三张卡的产物在立项节点就已经产出了
    （`docs/service.py::ProjectDocs`），执行器对它们无事可做。让它们走一遍
    `DRAFT → 审批 → READY → 派单 → 执行`，只会得到三张"执行器把现成文件再抄一遍"的卡，
    而抄出来的那一份还要再被门禁比一次 —— 那是把同一件事做三遍。

    **建卡走 `reporting.cards.add_done_card`，不在这里另写一套**：那一格已经是"把已经发生的事实
    落成一张已交付卡"的唯一入口（文档 / 检查 / 验收 / 打包四类共用），它自己管三件事——
    卡号高位段、存档点复用、`validated_status` 留给门禁。这里只负责"把内容写到工作区"和"判一次"。
    两份建卡入口就是这个仓库一直在付代价的那种"五代并存"。

    **判定为什么调 `assess` 而不是 `validate_card`**：后者第一条就要求卡处于 `RUNNING`
    （"这次判定有对应的在跑任务"），而系统节点卡是 `DELIVERED`。判据本身仍是同一份
    （`assess` 就是 `validate_card` 的纯判定核），只是跳过了那条"必须先跑过"的前置。
    不判这一步的后果很具体：`assemble.validated_manifest()` 只收 `validated_status == "valid"`
    的产物行 ⇒ 文档卡的产物永远进不了交付包。
    """
    cards = doc_cards(request.summary_digest or {})
    if not cards:
        return 0
    workspace = require_archive_root(request.project_key, bus_root=deps.bus_root) / bus_layout.workspace_dir_name(
        DOC_WRITER_AGENT_ID
    )
    workspace.mkdir(parents=True, exist_ok=True)
    written = 0
    #: 这一跑新写出来的文件（原来不存在的那些）。失败时要按它清回去。
    fresh: list[Path] = []

    try:
        for card in cards:
            path = workspace / card.filename
            existed = path.exists()
            # 先写盘：`add_done_card` 的 `rel_path` 必须指向一个真的存在的文件（`assemble` 打包时按它去读）。
            # P0-4：必须写 bytes，不能用 `write_text` —— Windows 上 `write_text` 会把 `\n` 翻成
            # `\r\n`，于是盘上比 `content.encode("utf-8")` 每条换行多 1 字节，manifest 的 bytes
            # 从落盘那一刻起就与磁盘不符（实测 12 行的报告差 12B）。
            path.write_bytes(card.body.encode("utf-8"))
            if not existed:
                fresh.append(path)
            add_done_card(
                session,
                feature_request_id=request.id,
                card_id=card.task_id,
                title=card.title,
                fmt=card.format,
                rel_path=f"{workspace.name}/{card.filename}",
                content=card.body,
                acceptance=card.acceptance,
                writer_agent_id=DOC_WRITER_AGENT_ID,
                floor=(card.min_bytes, card.min_lines),
                # 文档卡是流水线的第一段。显式写出来而不是吃默认值：
                # 默认值是"没标就是 doc"的兜底，而这里是一个决定（看板按它分行）。
                family=FAMILY_DOC,
            )
            # 判一次，让文档卡的产物进得了交付包。判定逻辑在 `reporting.cards.verify_card` 一处，
            # 检查卡（T93–T95）走的是同一个函数 —— 两处各写一遍就是两份判据。
            verify_card(session, feature_request_id=request.id, card_id=card.task_id)
            written += 1
    except BaseException:
        # 写盘不参与事务（U7，2026-10-28 真跑）：`session.rollback()` 只撤库里的行，盘上的文件留着。
        # 实测现场是 `results/realrun_1028/workbuddy-workspace/` 里三份文件都在、库里一张卡都没有 ——
        # 下一次重跑会撞主键或读到"文件在但没人认"的半程态，比一次干净的失败难查得多。
        #
        # 只删**这一跑新写出来的**那些：原来就存在的文件属于上一次（可能是一次成功的）落盘，
        # 删掉它等于用一次失败去毁掉一次成功。
        for path in fresh:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return written

def _normalize(deps: OrchestrateDeps) -> Callable[[IntakeState], IntakeState]:
    def node(state: IntakeState) -> IntakeState:
        session = _require_session(deps)
        drafts = state["drafts"]
        # FR-005：非法结构在这里抛出，下面的建行一行都不会发生（用例
        # `test_illegal_dependency_structure_creates_no_card_rows` 钉的就是这个先后）。
        order = normalize(drafts)

        request = _request_row(session, state["feature_request_id"])
        cards = [str(item) for item in order]
        # T109（T-4 接口定稿 + owner 2026-10-16 裁 ㉝ 归 T109）：这一行原来是 T-15 裁 ① 的那句自指
        # —— `round_no=1` 加一个指向登记行自身的临时 `ref`。换成真实句柄只有一个诚实做法：
        # **先真打一拍**，再把它回读出来的对象名登记进库。
        # 三步各走各的唯一入口：存档根由 `archive.root` 解析（`<bus_root>/<project_key>`，
        # 权威出处 `config/params-ref.md §10`），拍点由 `archive.cadence` 落盘，库里那一行连同
        # EN-14 的 `SNAPSHOT` 留痕由 `persistence/snapshots.record_point()` 写。
        # ⇒ `graph` 从此不再 `Snapshot(...)`：EN-12 的写者收成一处（原则 I），轮次也不再是硬编码的 1
        # （`next_round_no` 按项目数轮 —— 同一个项目的第二次拆单登记成第 1 轮会让"可枚举"变成"永远是第一轮"）。
        round_no = snapshot_store.next_round_no(session, project_key=request.project_key)
        point = cadence.commit_archive_point(
            require_archive_root(request.project_key, bus_root=deps.bus_root),
            phase=cadence.OPEN_PHASE,
            round_no=round_no,
            project_key=request.project_key,
            task_card_ids=cards,
        )
        snapshot = snapshot_store.record_point(
            session,
            project_key=request.project_key,
            round_no=round_no,
            phase=cadence.OPEN_PHASE,
            task_card_ids=cards,
            ref=point.ref,
        )
        # 下面这道 flush 在 T108 之前是"人工保序"，现在保序由 `record_point()` 内部那次 flush 提供。
        # 留着它不是因为还需要，而是因为**这个先后本身是一条实测到的 SQLAlchemy 行为的对策**：
        # 没有 `relationship()` 的两个 mapper，UoW 不按表间外键排 INSERT 顺序 —— 三种写法
        # （直接 import 两个模型模块 / 先 `import acp.persistence` / 显式 `configure_mappers()`）
        # 在同一次 flush 里全部以同一形态失败：`INSERT INTO feature_request` → `INSERT INTO task_card`
        # （`snapshot` 被排在卡之后），PG 回 `fk_task_card_snapshot_id_snapshot`。
        # 库里那条 FK 是真的，ORM 却不知道要排序 ⇒ 谁写存档点，谁就得先把它发出去。
        # `tests/integration/test_schema_invariants.py` 当初没暴露这一条，因为它的 `add()` 助手
        # 每个对象都开一个 savepoint 并单独 flush —— 恰好等于人工保序。
        session.flush()
        for draft in drafts:
            session.add(
                TaskCard(
                    id=draft.task_id,
                    feature_request_id=request.id,
                    title=draft.title,
                    difficulty=draft.difficulty,
                    skill_tags=list(draft.skill_tags),
                    depends_on=list(draft.depends_on),
                    # 这四列 NULL = "指派与三岗尚未发生"（T-14 ① / AssignGuards 之前），
                    # 不是占位值：外键在非空时才生效，所以填了假 id 一样会被拦。
                    executor_agent_id=None,
                    executor_role_id=None,
                    reviewer_agent_id=None,
                    acceptor_agent_id=None,
                    artifact_floor_min_bytes=draft.artifact_floor.min_bytes,
                    artifact_floor_min_lines=draft.artifact_floor.min_lines,
                    artifact_floor_format=draft.artifact_floor.format,
                    acceptance_criteria=list(draft.acceptance_criteria),
                    # 文档没给这两列的初值（EN-02 只给了上限）：0 = "尚未发生移交 / 尚未起算轮次"，
                    # 与 FR-027 的"上限 1"和宪法 原则 III 的"第 3 轮"同向，不是自造的边界。
                    handoff_count=0,
                    review_round=0,
                    status="DRAFT",
                    snapshot_id=snapshot.id,
                )
            )
        # FR-007（T034）：映射表与卡同步落库。这里的第二次 flush 与上面那道同一条理由 ——
        # `acceptance_map` 与 `task_card` 两个 mapper 之间没有 `relationship()`，
        # UoW 不按那条复合外键排 INSERT 顺序 ⇒ 卡必须已经发出去，映射才能指得过去。
        session.flush()
        for link in state["links"]:
            session.add(
                AcceptanceMap(
                    id=uuid.uuid4(),
                    feature_request_id=request.id,
                    requirement_item=link.requirement_item,
                    acceptance_item=link.acceptance_item,
                    task_card_id=link.task_id,
                    produced_by=link.produced_by,
                )
            )
        # 文档卡（T90–T92）：立项节点已把三份文档定稿，这里落成"已完成卡"。
        # 位置在普通卡与验收映射之后、`request.status = "SPLIT"` 之前：
        # 它们不依赖任何卡、也没有卡依赖它们（DAG 里的孤立输入节点），所以落库顺序不影响拓扑；
        # 放在"校验已过"之后是为了保住既有性质 —— `normalize(drafts)` 不过就一行都不建，
        # 文档卡不能成为"校验没覆盖却先入库"的口子。
        _persist_doc_cards(session, request, deps)
        request.status = "SPLIT"
        session.flush()
        return {
            "card_order": list(order),
            "card_ids": list(order),
            "snapshot_id": snapshot.id,
            "acceptance_map_rows": len(state["links"]),
            "request_status": str(request.status),
        }

    return node


def build_graph(deps: OrchestrateDeps):
    """按 `plan.md` 的节点与边装配并 compile。

    **T049 的挂点**：`ApprovalGate(kind=A)` 接在 `Normalize` 之后、`END` 之前 —— 也就是
    "首次 `Dispatch` 之前"这条链上目前唯一可能的等待点（Dispatch 节点归 Phase 6）。
    把它做成 `Normalize` 的**唯一出边**是刻意的：图上没有第二条从拆单通向终点的路线，
    所以"绕过卡点直接派发"在拓扑上不可表示；`test_graph_has_exactly_the_documented_nodes_and_plain_edges`
    钉的就是这条边集。等 Phase 6 接 `Dispatch` 时，它 MUST 接在 `ApprovalGate` 之后 ——
    那时这条判据会被有意改写，而不是在这里预埋一个开关。

    **`interrupt` 要求 checkpointer**（D-9）：缺它就在装配期抛 `CheckpointerNotConfigured`。
    这一处与 Phase 3 那版模块头的"这条链上没有等待点，所以不接 checkpointer"正好相反，
    因为从本笔起链上**有**等待点了；那句话作为时点读数留在 `tasks.md` 的 Phase 3 段落里，不改写。
    """
    if deps.checkpointer is None:
        raise CheckpointerNotConfigured(
            "图上有 ApprovalGate 这一个挂起点，compile 时必须带 checkpointer（D-9 / 宪法 V）"
        )
    builder = StateGraph(IntakeState)
    builder.add_node(PLAN, _plan(deps))
    builder.add_node(SPLIT, _split(deps))
    builder.add_node(NORMALIZE, _normalize(deps))
    builder.add_node(APPROVAL_GATE, approval_gate(_require_session(deps)))
    builder.add_edge(START, PLAN)
    # T031：这条边不设等待人，也不做条件判断（推进条件已在 `_plan` 内拦）。
    builder.add_edge(PLAN, SPLIT)
    builder.add_edge(SPLIT, NORMALIZE)
    # T049：拆单之后必须过 A 类卡点，再谈派发。
    builder.add_edge(NORMALIZE, APPROVAL_GATE)
    builder.add_edge(APPROVAL_GATE, END)
    return builder.compile(checkpointer=deps.checkpointer)


def _refuse_if_parked_at_the_gate(graph, feature_request_id: uuid.UUID) -> None:
    """已经停在人工卡点的 thread ⇒ 拒绝再推进。**必须挡在图开跑之前**（U1，2026-10-28 真跑）。

    为什么挡在这里、而不是让 `_plan` 去拦：`graph.invoke(dict)` 不是"从上次停的地方继续"，
    而是把新输入并进已有 state、从入口再走一遍 —— 而 LangGraph 在**节点开跑之前**就落了
    一版 checkpoint（`next=('plan',)`）。所以原来停在 `approval_gate` 的等待点被这一版覆盖，
    随后 `_plan` 撞 `AlreadyAdvanced` 抛错。后果不是"这次失败"，而是**这张单永久废掉**：
    checkpoint 现在指向 `plan`，`run_resume` 读到 `next` 非空（绕过 `no_waiting_point` 那道守卫）
    ⇒ `Command(resume=…)` 从 `plan` 续跑 ⇒ 必然再撞 `AlreadyAdvanced`。只能删 checkpoint 才救得回来。

    进了图再判断已经晚了 —— checkpoint 那时已经写下去了。所以唯一能保住现场的位置是开跑之前。

    **只拦"停在卡点"这一种**，不拦中途失败后的重试：`next=('split',)` 那种现场本来就没有
    等待点可丢，重试是正常操作（见 `U6` 的可重试口径）。把重试也一起拦掉，等于用一条运维
    事故换另一条。
    """
    snapshot = graph.get_state(thread_config(kind=GATE_KIND, feature_request_id=feature_request_id))
    waiting = tuple(str(node) for node in (snapshot.next or ()))
    if APPROVAL_GATE not in waiting:
        return
    raise AlreadyAdvanced(
        f"这条需求单已经停在人工卡点（next={waiting}）⇒ 该走的是 run_resume 那一条，"
        "再推进一次会把等待点覆盖掉，这张单就再也恢复不了"
    )


def advance_request(
    feature_request_id: uuid.UUID,
    *,
    session: Session,
    splitter: CardSplitter | None,
    checkpointer: BaseCheckpointSaver | None = None,
    bus_root: Path | None = None,
) -> IntakeState:
    """一次调用 = 一个工作单元：四个节点共用注入的这一个会话，提交点归调用方。

    `thread_id` 由 `(卡点类, 需求单号)` 推导（`persistence/checkpointer.py` 的模块头第 2 条）：
    首发与续跑算出的是同一个，所以 FR-041 的"同一 thread 恢复"不需要谁去记住它。
    """
    graph = build_graph(
        OrchestrateDeps(
            session=session, splitter=splitter, checkpointer=checkpointer, bus_root=bus_root
        )
    )
    _refuse_if_parked_at_the_gate(graph, feature_request_id)
    return graph.invoke(
        {"feature_request_id": feature_request_id},
        thread_config(kind=GATE_KIND, feature_request_id=feature_request_id),
    )


def resume_after_approval(
    feature_request_id: uuid.UUID,
    *,
    session: Session,
    splitter: CardSplitter | None,
    checkpointer: BaseCheckpointSaver,
    conclusion: str,
    bus_root: Path | None = None,
) -> IntakeState:
    """把人工结论交回图里续跑（FR-041：从被打断的那一步恢复，MUST NOT 重跑整条链路）。

    实测语义（`evidence/phase-04.md §T048` 三条读数）：`Command(resume=…)` 只重跑**卡住的那个节点**；
    而**不带** `Command` 的 `invoke(dict)` 是"从头再跑一次"（`plan` 因此会撞 `AlreadyAdvanced`）
    ⇒ 恢复只有这一条入口，用错姿势不是静默重跑而是响亮报错，这正是本函数存在的理由。
    """
    graph = build_graph(
        OrchestrateDeps(
            session=session, splitter=splitter, checkpointer=checkpointer, bus_root=bus_root
        )
    )
    return graph.invoke(
        Command(resume=conclusion),
        thread_config(kind=GATE_KIND, feature_request_id=feature_request_id),
    )


RESULT_INGEST = "result_ingest"
VALIDATE_ARTIFACT = "validate_artifact"
REVIEW = "review"
HANDOFF = "handoff"


class MissingDownstream(ValueError):
    """装配卡级图时缺了 `Review` 或 `HandOff` 之一 ⇒ 出边会只剩一条，而那正是 T076 禁的形状。"""


@dataclass(frozen=True)
class ExecutionDeps:
    """卡级那半条链的注入面：会话 + 本次回收的时刻 + 回收方是谁。

    与 `OrchestrateDeps` 分开的理由和 `IntakeState` / `ExecutionState` 分开一样：生命周期不同，
    需要的东西也不同（卡级不带拆单器，也不需要 checkpointer —— 这一段上没有 `interrupt`，
    挂起点在 A/B/C/D 四类卡点上，不产物回投这条边上）。

    **这里没有"上限 / 槽位"这一项**：占槽是 `Dispatch`（T078）的事，回投段只管把已经跑完的结果落成实态。
    把 slot manager 塞进本依赖面，就会出现"门禁自己放槽"这种谁都不认的收尾。
    """

    session: Session
    created_at: dt.datetime
    actor_agent_id: str
    #: 产物所在的项目根（`archive_root(project_key)`）。P1-16 的内容检测要按
    #: `artifact.rel_path` 读盘；缺省 `None` = 不查内容（只服务只测判定表的用例）。
    #: 生产两条路径（`realrun.run_card_segment` / `run.run_execute`）都显式交这个值。
    bus_root: Path | None = None


def _result_ingest(deps: ExecutionDeps) -> Callable[[ExecutionState], ExecutionState]:
    def node(state: ExecutionState) -> ExecutionState:
        # P1-2：回投是这张图的入口，三个必需键（单号 / 卡号 / 结果）在这里一次校验。
        # 少了任何一格，`record_result` 会往 EN-06 写一行挂不上卡的产物 —— 那行修不掉，只能删。
        checked = ensure_state(ExecutionStateModel, state, at=RESULT_INGEST)
        if deps.session is None:
            raise MissingSession("回投段需要一个共享会话")
        ingested = record_result(
            deps.session,
            feature_request_id=checked.feature_request_id,
            task_id=checked.task_id,
            result=checked.result,
            actor_agent_id=deps.actor_agent_id,
            created_at=deps.created_at,
        )
        return {"artifact_ids": list(ingested.artifact_ids)}

    return node


def _validate_artifact(deps: ExecutionDeps) -> Callable[[ExecutionState], ExecutionState]:
    def node(state: ExecutionState) -> ExecutionState:
        if deps.session is None:
            raise MissingSession("门禁段需要一个共享会话")
        verdict = validate_card(
            deps.session,
            feature_request_id=state["feature_request_id"],
            task_id=state["task_id"],
            artifact_ids=state["artifact_ids"],
            created_at=deps.created_at,
            bus_root=deps.bus_root,
        )
        # `to_state()` 的静态返回是 `dict[str, object]`（它只认识"要写回哪些键"，
        # 不认识这张图的 TypedDict）。这里用 `cast` 而不是 `type: ignore`：形状由
        # `test_the_gate_writes_the_verdict_it_routes_on` 在运行期钉住，声明只是把这件
        # 已经成立的事告诉类型检查器 —— `ignore` 会把将来真出错的同类问题一起吞掉。
        return cast(ExecutionState, verdict.to_state())

    return node


def build_execution_graph(deps: ExecutionDeps, *, review: Callable, handoff: Callable):
    """T076：`ResultIngest → ValidateArtifact → {Review | HandOff}`，出边**恰好**两条。

    `review` / `handoff` 是**注入的节点体**而不是本模块里的实现：`Review` 归 Phase 7（T089），
    `HandOff` 归 T081。不给默认实现的口径与 `splitter` 一样 —— 缺了就装配期响亮报错，
    而不是"图上悄悄少一条边"。这条拓扑是 FR-018 的结构保证：产物不达标的卡没有第三条路可走，
    而"审查节点永不被调用"（`tests/guards/test_artifact_gate.py`，T083）证的正是这件事。

    段上**不带 checkpointer**：这一段没有等待点（宪法 V 的 `interrupt` 全在卡点类上），
    带一个反而会把"回投不需要恢复现场"读成设计。恢复语义在 `ApprovalGate` 那半条链上（T051 / T054）。
    """
    if review is None or handoff is None:
        raise MissingDownstream("卡级图必须同时给出 Review 与 HandOff 两个下游节点体（T076 的出边是两条）")
    builder = StateGraph(ExecutionState)
    builder.add_node(RESULT_INGEST, _result_ingest(deps))
    builder.add_node(VALIDATE_ARTIFACT, _validate_artifact(deps))
    builder.add_node(REVIEW, review)
    builder.add_node(HANDOFF, handoff)
    builder.add_edge(START, RESULT_INGEST)
    builder.add_edge(RESULT_INGEST, VALIDATE_ARTIFACT)
    builder.add_conditional_edges(
        VALIDATE_ARTIFACT,
        route_after_validation,
        {VA_REVIEW: REVIEW, VA_HANDOFF: HANDOFF},
    )
    builder.add_edge(REVIEW, END)
    builder.add_edge(HANDOFF, END)
    return builder.compile()


SELECT_WORKER = "select_worker"
ASSIGN_GUARDS = "assign_guards"
#: 节点名**从 `dispatch.py` 取**而不是在这里再写一遍 `"dispatch"`：
#: 那个模块自己的 `NODE` 是留痕与日志里用的同一个名字，两处各写一份就会漂成"图上有节点、留痕里没节点"。
DISPATCH = dispatch_node.NODE


@dataclass(frozen=True)
class AssignmentDeps:
    """指派 + 派发那一段的注入面：会话 + 时刻 + actor + 瞬态槽 + 进程内台账。

    **为什么槽在这一段、而不在 `ExecutionDeps` 里**：`ExecutionDeps` 的模块头那条"这里没有上限/槽位
    这一项"说的是**回投段**（产物落库、门禁判定）不该知道槽的存在；派发段正是槽的借用现场，
    它不知道槽就没法占槽。两张依赖面各管一段，比一张大而全的依赖面更难被"顺手在别处也放一下槽"带坏。

    **为什么台账是参数而不是 state**：`SlotHandle` 按契约 §6 MUST NOT 被序列化进任务卡、日志、产物或 PG，
    而图 state 是唯一可能被 checkpointer 搬走的载体。`LeaseLedger` 只在进程内活着：图节点往里 `put`，
    装配点（`orchestrate/scheduler.py:run_card`）从里 `take` 并在 `finally` 里退。
    """

    session: Session
    created_at: dt.datetime
    actor_agent_id: str
    slots: object
    leases: LeaseLedger
    #: 审查模式（Phase 17 D6）。默认 manual ⇒ 直接构造本依赖的旧调用与 Phase 16 逐字同行为；
    #: 网页入口在 `deepseek` 模式下显式传值，把关席才会选到 `deepseek-reviewer`。
    review_mode: str = REVIEW_MODE_MANUAL


def _select_worker(deps: AssignmentDeps) -> Callable[[AssignmentState], AssignmentState]:
    def node(state: AssignmentState) -> AssignmentState:
        # P1-2：选人是这张图的入口。卡号缺失时 `_card` 会拿 `None` 去查库，
        # PG 报的是类型错，而真正的问题在上游少给了键。
        checked = ensure_state(AssignmentStateModel, state, at=SELECT_WORKER)
        if deps.session is None:
            raise MissingSession("选人段需要一个共享会话")
        card = _card(deps.session, checked.feature_request_id, checked.task_id)
        need = select_worker.task_need_of(card)
        candidates, dropped = select_worker.candidates_from_registry(deps.session)
        ranking = select_worker.rank_candidates(need, candidates, dropped)
        # 2026-10-31 路线 B：请求级路由点名的执行者优先（`qoder` 或纯 API 执行器）。
        # 三岗互斥与自守禁令不在这一层判 —— 那是 `AssignGuards` 的断言点，这里只决定"谁坐执行席"。
        # 点名的人不参选（停用/不在册）时**不强制**，同一条 SELECTION 留痕里写明 `routing_ignored`。
        routed = routing_lib.executor_agent(routing_lib.routing_of(_request_row(deps.session, checked.feature_request_id)))
        available = {candidate.agent_id for candidate in candidates}
        forced = routed if routed in available else None
        written = select_worker.apply_selection(
            deps.session,
            ranking,
            feature_request_id=state["feature_request_id"],
            actor_agent_id=deps.actor_agent_id,
            created_at=deps.created_at,
            forced=forced,
            routing_ignored=routed if routed and forced is None else None,
        )
        # 取**卡上那一列的现值**而不是 `ranking.ordered[0]`：候选为空时 `apply_selection` 不动列
        # （T072 那条"不静默丢弃"），返回来的三元组就必须反映"库里到底有没有执行者"，
        # 否则下一跳 `AssignGuards` 会拿着一个图上存在、库里不存在的执行者去判规则 1。
        return {"executor_agent_id": written.executor_agent_id}

    return node


def _assign_guards(deps: AssignmentDeps) -> Callable[[AssignmentState], AssignmentState]:
    def node(state: AssignmentState) -> AssignmentState:
        if deps.session is None:
            raise MissingSession("把关指派段需要一个共享会话")
        picked = assign_guards.assign(
            deps.session,
            feature_request_id=state["feature_request_id"],
            task_id=state["task_id"],
            created_at=deps.created_at,
            actor_agent_id=deps.actor_agent_id,
            mode=deps.review_mode,
        )
        return {"reviewer_agent_id": picked.reviewer, "acceptor_agent_id": picked.acceptor}

    return node


def _card(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.id == task_id,
        )
        .one_or_none()
    )
    if card is None:
        raise RequestNotFound(f"任务卡 {task_id} 不在库里 ⇒ 无卡可指派")
    return card


def _dispatch(deps: AssignmentDeps) -> Callable[[AssignmentState], AssignmentState]:
    def node(state: AssignmentState) -> AssignmentState:
        if deps.session is None:
            raise MissingSession("派发段需要一个共享会话")
        lease = dispatch_node.dispatch(
            deps.session,
            feature_request_id=state["feature_request_id"],
            task_id=state["task_id"],
            slots=deps.slots,
            created_at=deps.created_at,
            actor_agent_id=deps.actor_agent_id,
        )
        deps.leases.put(lease)
        # **返回空 dict 是有意的**：`SlotHandle` / `Lease` 都不许进 state（契约 §6 + 上面那段理由）。
        # 派发这一步的可观察效果全在库里（卡态 `RUNNING` + EN-14 一行）与台账里（一张待退的凭据），
        # 图上再往 state 放一个"派发了"的布尔值就是给同一个事实造第二个载体。
        return {}

    return node


def build_assignment_graph(deps: AssignmentDeps):
    """T086 / T073 + **T123（owner 2026-10-16 裁 甲）**：`SelectWorker → AssignGuards → Dispatch`。

    **为什么 `AssignGuards` 在 `Dispatch` 之前**：`plan.md:251` 原话，且规则 1（把关工具 MUST ≠ 执行者）
    要求执行者已知 —— 所以它必须在 `SelectWorker` **之后**（`plan.md:386` 那句原文写反了，
    owner 裁待裁 ⑨ 已定："正确顺序是 `SelectWorker` 先于 `AssignGuards`"，措辞随本笔一起改）。

    **为什么 `Dispatch` 今天进图**：WorkBuddy `phase7-review-v1.0.1` 的 P1-1 点的是"三岗互斥在下发之前
    无强制"——宪法原则 III 那句"下发**之前**做静态断言"里，"之前"在拓扑上必须可证伪。
    指派段跑完就已经过了 `guard/triple.assert_independent()`（`assign_guards.py:306`，先于它自己的
    `flush`），而派发是它的**唯一出边** ⇒ "没有过三岗就派发"在这张图上不可表示。
    代价与边界都记在 `orchestrate/scheduler.py` 的模块头：借槽从 `run_card()` 上移到这里，
    还槽留在那里，中间靠 `LeaseLedger` 这一张进程内台账交接。

    **这条边的对侧是 `dispatch()` 的生产调用点计数**：`scheduler.run_card()` 不再调 `dispatch()`
    （它调的是 `leases.take()`），所以整个 src 里 `dispatch(` 的调用点恰一处 = 本节点。
    两处各派一次就是两份就绪判定，那条判据是 `tests/guards/test_dispatch_edge.py`。

    段上不带 checkpointer（与卡级段同一理由：这一段上没有 `interrupt`）。卡点 B 的 `interrupt`
    今日仍不可用（`approval/service.py` 的 `ACTIVE_KINDS` 只有 A / D），所以矩阵填不满时本段
    抛 `GuardNotClosable` 而不是挂起 —— 与 T062 那个 422 出口同口径，登记为未闭而不是"已生效"。
    """
    builder = StateGraph(AssignmentState)
    builder.add_node(SELECT_WORKER, _select_worker(deps))
    builder.add_node(ASSIGN_GUARDS, _assign_guards(deps))
    builder.add_node(DISPATCH, _dispatch(deps))
    builder.add_edge(START, SELECT_WORKER)
    builder.add_edge(SELECT_WORKER, ASSIGN_GUARDS)
    # P1-1 那一格：派发只能是把关指派的后继，图上没有第二条路进 `DISPATCH`。
    builder.add_edge(ASSIGN_GUARDS, DISPATCH)
    builder.add_edge(DISPATCH, END)
    return builder.compile()
