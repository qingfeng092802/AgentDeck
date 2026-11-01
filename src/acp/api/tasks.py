"""T035：`PATCH /tasks/{id}` —— 下发前调整字段与依赖（FR-006｜R-03），并闭合 `data-model.md §5` 的 T-8。

三件由 owner 2026-10-12 三项决策定稿的事，都在这里落地：

1. **白名单 + `status=DRAFT` 门禁**（决策 1，含他加的那条收紧）。FR-006 的原文是"在任务清单**下发前**
   预览并调整"，所以下发之后改字段会让已经跑出去的状态迁移与依赖关系错位 ⇒ 只允许 `DRAFT`。
   白名单的**强制点只有一个** = `TaskPatch` 的 `extra="forbid"`：不可改的列根本不声明，传了就在 pydantic
   层 422 并点名那个键。没有第二道"收下但忽略"——那道会把使用者的拼错静默变成"改了但没生效"，
   是这里最坏的一种假成功。
2. **改了 `acceptance_criteria` 必须重算 EN-07**（决策 2 选 A + 强约束）：申报缺失 ⇒
   `422 acceptance_map_not_configured`，**不存在"改成功、映射为空"这种中间态**。
3. **改后 MUST 重跑 `Normalize`**（决策 3）：同一个事务内对**该需求单下这批卡改后的依赖集合**跑
   `normalize()`；非法 ⇒ `422 dependency_invalid`，而库里那一行**不动**（原子性由"先全部校验、
   后全部写"这个顺序保证，不是靠回滚兜）。**不把整张图重跑一遍**：那会重新调补全器与拆单器
   = 同一需求第二次记模型账（FR-059 / FR-060），还会撞出"卡该不该重新建"这种本不存在的问题。

`validate_draft`（FR-004 单卡形状）/ `normalize`（FR-005 引用与成环）/ `validate_links`（FR-007 覆盖）
是既有判据，本模块**一条都不重写**：把库里的行折回 `CardDraft` 喂给它们，只把它们的异常映射成 HTTP 语义。
重写一遍的同一条规则会有第二个逃逸口 —— 而 PATCH 正是最容易被当作旁路的那条路。

**路径里只有一个 `{id}`，而 EN-02 的主键是复合的 `(feature_request_id, id)`（裁决 2）** ⇒
`feature_request_id` 必须在请求体里给。这不是把主键泄漏给调用方：卡号的作用域本来就只在需求单内
（`data-model.md` EN-02 的 `depends_on` 行同此口径），调用方手里从来就是"哪张需求单的 T01"这一对。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from acp.api.approvals import PRINCIPAL_HEADER
from acp.guard import triple as gp
from acp.orchestrate.nodes import (
    artifact_retry,
    assemble as assemble_node,
    assign_guards,
    escalation,
    review as review_node,
    select_worker,
)
from acp.reporting import cards
from acp.review import reports as review_reports
from acp.review import service as review_service
from acp.orchestrate.nodes.normalize import DependencyViolation, normalize
from acp.orchestrate.nodes.split import (
    AcceptanceLink,
    CardDraft,
    MappingViolation,
    SplitViolation,
    validate_draft,
    validate_links,
)
from acp.persistence.enums import values_of
from acp.persistence.models_approval import AcceptanceMap, ReviewVerdict
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog
from acp.protocol import ArtifactFloor
from acp.review.policy import REVIEW_MODE_MANUAL

router = APIRouter()

# 只有这两列可空：显式传 null 是"把可空列改成无约束"，与"没传这个字段"是两件事；
# 反过来对 NOT NULL 列传 null 是清空，必须拒（见 handler 里的 `null_not_allowed`）。
NULLABLE_FIELDS: frozenset[str] = frozenset({"token_budget", "deadline_minutes"})
ADJUSTABLE_GATE_STATE = "DRAFT"


class AcceptanceLinkPayload(BaseModel):
    """改判据时随请求申报的映射行（EN-07 的一行，形状与 `AcceptanceLink` 同）。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    requirement_item: str
    acceptance_item: str
    produced_by: str


class TaskPatch(BaseModel):
    """请求体：`feature_request_id`（复合主键的另一半）+ 若干**可改字段**。

    白名单就是这张字段表，其强制点见模块头第 1 条，此处不复述理由。
    两个例外都写在 `contracts/agent-protocol.md §1.2`：`feature_request_id` 被声明但**不是**可改列
    （它是寻址的一半，路径里只有 `{id}` 而 EN-02 主键是复合的），`acceptance_links` 不是 EN-02 的列
    （它是决策 2 要求随批申报的 EN-07 映射）。

    本类**靠** `exclude_unset` 区分"没传"与"传了 null"：`token_budget: null` = 改成无预算，
    `deadline_minutes` 不传 = 不动。这两件事在 FR-006 的"调整"语义里必须可区分。
    """

    model_config = ConfigDict(extra="forbid")

    feature_request_id: uuid.UUID
    title: str | None = None
    difficulty: int | None = None
    skill_tags: list[str] | None = None
    depends_on: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    # 与协议层 `Task.token_budget` 同一条口径（裁决 3）：非 null 时必须 > 0，"无预算"用 null 表达。
    token_budget: Decimal | None = Field(default=None, gt=0)
    deadline_minutes: int | None = None
    acceptance_links: list[AcceptanceLinkPayload] | None = None

    def adjusted_fields(self) -> dict[str, Any]:
        """只回真正传了的可改字段（`exclude_unset` 是关键：没传的列一律不动）。"""
        sent = self.model_dump(exclude_unset=True)
        sent.pop("feature_request_id", None)
        sent.pop("acceptance_links", None)
        return sent


# 白名单的**声明处**就是上面这张字段表（强制点 = `extra="forbid"`：不可改的列根本没声明，
# 传了就 422 并点名键）。下面两个集合是从它**推导**出来的，不是第二份清单 ——
# `contracts/agent-protocol.md §1.2` 写的是同一份，`tests/integration/test_task_patch.py`
# 有一条用例逐字比对两边（漂移即红）。
NON_COLUMN_FIELDS: frozenset[str] = frozenset({"feature_request_id", "acceptance_links"})
ADJUSTABLE_FIELDS: frozenset[str] = frozenset(TaskPatch.model_fields) - NON_COLUMN_FIELDS
# `token_budget` / `deadline_minutes` 是 EN-02 的列，但不参与 FR-004 的单卡形状（`CardDraft` 里没有它们），
# 所以构造"改后的卡"时要把它们隔出去 —— 反过来 `artifact_floor` 三列按决策 1 不可改，压根不在白名单里。
CARD_SHAPE_FIELDS: frozenset[str] = frozenset(CardDraft.model_fields) - {"task_id", "artifact_floor"}


class TaskPatchResponse(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    status: str
    request_status: str
    adjusted: list[str]
    depends_on: list[str]
    acceptance_criteria: list[str]
    # 这两个数是决策 2 那条"不许改成功、映射为空"的可核对面：写进去多少行、替换掉多少行。
    acceptance_map_rows: int
    acceptance_map_rows_replaced: int
    # 改后**整批**卡的拓扑序（决策 3 的 normalize 结果）——看板据此重画依赖图，不必再问一次模型。
    card_order: list[str]


def _session(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：EN-02 的调整需要注入 app.state.session_factory"
        )
    return factory()


@contextmanager
def _store(request: Request) -> Iterator[Any]:
    """评审端点的一次请求一个会话：出去一定 `close()`，落地由端点显式 `commit()`。

    形状与 `api/approvals.py::_store` 同一口径：不套 `new_session`（它的 `rollback()` 会把测试夹具的
    外层事务一起回滚），也不在异常分支里替调用方回滚 —— 未提交的评审结论由 `close()` 丢弃。
    """
    session = _session(request)
    try:
        yield session
    finally:
        session.close()


def _principal(request: Request) -> str:
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        raise HTTPException(
            status_code=403, detail=f"principal_not_allowed：请求头 {PRINCIPAL_HEADER} 缺失"
        )
    return principal


def _draft_of(card: TaskCard) -> CardDraft:
    """把库里的行折成 `CardDraft`，好让 FR-004 的判据有**唯一**一个入口（本模块不重写形状规则）。"""
    return CardDraft(
        task_id=card.id,
        title=card.title,
        difficulty=card.difficulty,
        skill_tags=list(card.skill_tags),
        depends_on=list(card.depends_on),
        artifact_floor=ArtifactFloor(
            min_bytes=card.artifact_floor_min_bytes,
            min_lines=card.artifact_floor_min_lines,
            format=card.artifact_floor_format,
        ),
        acceptance_criteria=list(card.acceptance_criteria),
    )


def _load_card(session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.id == task_id,
        )
        .one_or_none()
    )
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"task_not_found：需求单 {feature_request_id} 下没有卡 {task_id}",
        )
    return card


@router.patch("/tasks/{task_id}", response_model=TaskPatchResponse)
def patch_task(task_id: str, payload: TaskPatch, request: Request) -> TaskPatchResponse:
    with _store(request) as session:

        card = _load_card(session, payload.feature_request_id, task_id)
        # 决策 1 的收紧：下发前才可调整。`READY` 及之后拒改**任何**字段。
        if str(card.status) != ADJUSTABLE_GATE_STATE:
            raise HTTPException(
                status_code=409,
                detail=f"task_not_adjustable：当前状态是 {card.status}，FR-006 只允许在 {ADJUSTABLE_GATE_STATE} 下调整",
            )

        adjusted = payload.adjusted_fields()
        if not adjusted:
            raise HTTPException(
                status_code=422,
                detail="no_field_adjusted：请求体里没有一个可改字段，这条 PATCH 什么都不做",
            )
        for name, value in adjusted.items():
            if value is None and name not in NULLABLE_FIELDS:
                raise HTTPException(
                    status_code=422,
                    detail=f"null_not_allowed：{name} 是 EN-02 的 NOT NULL 列，能改但不能清空"
                    f"（可空的只有 {sorted(NULLABLE_FIELDS)}）",
                )

        criteria_changed = "acceptance_criteria" in adjusted
        if payload.acceptance_links is not None and not criteria_changed:
            # 判据没改而映射改了，落库的结果是 EN-07 里出现卡上没有的条目 ——
            # 与 FR-029"MUST NOT 引入额外标准"同一件事，所以拒在这里，而不是"收下但不写"。
            raise HTTPException(
                status_code=422,
                detail="acceptance_map_without_criteria：acceptance_criteria 未被改动，不重申报 EN-07"
                "（映射的键是卡上的判据原文，判据未变而映射变了 = 往卡上塞它没写过的标准）",
            )

        siblings = (
            session.query(TaskCard)
            .filter(TaskCard.feature_request_id == payload.feature_request_id)
            .all()
        )
        # 先全部校验、后全部写：原子性靠这个顺序，不靠回滚。
        # 被改的那张卡以"改后的值"参与校验，其余卡原样参与 —— `depends_on` 的作用域就是这批卡。
        drafts: list[CardDraft] = []
        for other in siblings:
            base = _draft_of(other)
            if other.id != task_id:
                drafts.append(base)
                continue
            drafts.append(
                base.model_copy(
                    update={k: v for k, v in adjusted.items() if k in CARD_SHAPE_FIELDS}
                )
            )
        target = next(d for d in drafts if d.task_id == task_id)
        try:
            # FR-004 的单卡形状（含 `depends_on` 里无空白 / 无重复）→ FR-005 的引用合法与不成环：
            # 两条都是既有判据，本模块只做 HTTP 语义映射，不重写任何一条。
            for draft in drafts:
                validate_draft(draft)
            order = normalize(drafts)
        except SplitViolation as exc:
            raise HTTPException(status_code=422, detail=f"task_shape_invalid：{exc}") from exc
        except DependencyViolation as exc:
            raise HTTPException(status_code=422, detail=f"dependency_invalid：{exc}") from exc

        if criteria_changed:
            if not payload.acceptance_links:
                # `None`（没带这个键）与 `[]`（带了但是空的）在这里是同一件事：**没有申报**。
                # 决策 2 拒的就是"改成功、映射为空"，而空数组正是它的一个写法。
                raise HTTPException(
                    status_code=422,
                    detail="acceptance_map_not_configured：改了 acceptance_criteria 就必须同批重申报 EN-07 映射"
                    "（决策 2：不许出现'改成功、映射为空'）",
                )
            links = [
                AcceptanceLink(
                    task_id=link.task_id,
                    requirement_item=link.requirement_item,
                    acceptance_item=link.acceptance_item,
                    produced_by=link.produced_by,
                )
                for link in payload.acceptance_links
            ]
            # 覆盖判据只按**这张卡**算：其余卡的 EN-07 行本轮不动，把它们一起纳进来会误报"未覆盖"。
            # 而 `validate_links` 的规则 ② 正好反过来保证了申报里不许出现别的卡的卡号。
            try:
                validate_links([target], links)
            except MappingViolation as exc:
                raise HTTPException(status_code=422, detail=f"acceptance_map_invalid：{exc}") from exc

        # —— 到这里才写。——
        for name, value in adjusted.items():
            setattr(card, name, value)
        session.flush()

        rows_replaced = 0
        if criteria_changed:
            # 旧映射不能留着：判据改了而映射还是旧条目 = EN-07 从此指向一条卡上已经不存在的判据（脏数据）。
            rows_replaced = (
                session.query(AcceptanceMap)
                .filter(
                    AcceptanceMap.feature_request_id == payload.feature_request_id,
                    AcceptanceMap.task_card_id == task_id,
                )
                .delete(synchronize_session=False)
            )
            session.flush()  # 删除先发出去，再插新的：同表内 delete→insert 的顺序不该交给 UoW 猜
            for link in links:
                session.add(
                    AcceptanceMap(
                        id=uuid.uuid4(),
                        feature_request_id=payload.feature_request_id,
                        requirement_item=link.requirement_item,
                        acceptance_item=link.acceptance_item,
                        task_card_id=link.task_id,
                        produced_by=link.produced_by,
                    )
                )
        session.flush()

        session.commit()  # 落库：提交点在本端点（与 approvals/requests 同形）；close 在 _store 的 finally
        return TaskPatchResponse(
            task_id=card.id,
            feature_request_id=payload.feature_request_id,
            status=str(card.status),
            # EN-01 的状态列权威在需求单行本身（原则 I：不在卡上复述需求单的状态）。
            request_status=str(session.get(FeatureRequest, payload.feature_request_id).status),
            adjusted=sorted(adjusted),
            depends_on=list(card.depends_on),
            acceptance_criteria=list(card.acceptance_criteria),
            acceptance_map_rows=_map_row_count(session, payload.feature_request_id, task_id),
            acceptance_map_rows_replaced=rows_replaced,
            card_order=list(order),
        )


def _map_row_count(session, feature_request_id: uuid.UUID, task_id: str) -> int:
    """读回**库里**这张卡的 EN-07 行数，而不是"我准备写几行"的意图数。"""
    return (
        session.query(AcceptanceMap)
        .filter(
            AcceptanceMap.feature_request_id == feature_request_id,
            AcceptanceMap.task_card_id == task_id,
        )
        .count()
    )


# ------------------------------------------------------------------ FR-024 人工覆盖（T071）
#
# **形态定稿**（T071 行下登记的"路径名与请求/响应契约"由本轮定）：
# `POST /tasks/{task_id}/worker`，体 = `{feature_request_id, agent_id, operator}`。
# 为什么单独开一条而不往 PATCH 加字段：owner 2026-10-12 决策 1 把四个指派列排除在
# `contracts/agent-protocol.md §1.2` 的白名单之外，原话"给使用者开'直接改派'会绕过三岗互斥和选人评分"。
# 于是 FR-024（M1 硬需求）只能另开路，**且必须比 PATCH 更严** —— 下面那三条 MUST 就是"更严"的内容。
#
# 覆盖只写 `executor_agent_id`（依据是 `data-model.md:34` 那句"可空至 SelectWorker 填充"），**不动 status**；
# 与 PATCH 一样只 `flush()`，提交边界留给调用方的 UoW（`persistence/session.py::new_session`）。
ASSIGNABLE_STATES: frozenset[str] = frozenset({"DRAFT", "PENDING_APPROVAL", "READY"})


class WorkerOverrideBody(BaseModel):
    """`operator` 必填：留痕里"谁覆盖的"是 FR-051 四要素之一，缺它这行审计就成了无主记录。"""

    model_config = ConfigDict(extra="forbid")

    feature_request_id: uuid.UUID
    agent_id: str = Field(min_length=1)
    operator: str = Field(min_length=1)


class WorkerOverrideResponse(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    executor_agent_id: str
    #: 自动算出来的原首位。响应里直接带它，FR-024 的"覆盖可查"就不必去翻 EN-14。
    auto_selected: str | None
    overridden: bool
    keys: list[str]
    skipped: list[str]


def _guard_pool(session, request: Request) -> list[assign_guards.Candidate]:
    """三岗候选池的唯一取数口在 `assign_guards.pool_from_role_specs()`（T086 把它从本文件搬了过去）。

    搬的理由与 `guard/triple.py` 同一条：覆盖路径与自动路径 MUST 读**同一份** EN-04 生效口径，
    两处各解析一遍 `mounted_agents` 就会在两处各自漂（原则 I）。这里只剩一行转发，判据不变。

    `review_mode` 由 `create_app` 的装配注入（缺省 = manual）：`deepseek` 模式下
    `deepseek-reviewer` 才能作为审查候选；直接 `create_app()` 不注入的测试现场保持 Phase 16 口径。
    """
    mode = getattr(request.app.state, "review_mode", REVIEW_MODE_MANUAL)
    return assign_guards.pool_from_role_specs(session, mode=mode)


@router.post("/tasks/{task_id}/worker", response_model=WorkerOverrideResponse)
def override_worker(task_id: str, body: WorkerOverrideBody, request: Request) -> WorkerOverrideResponse:
    """T071：使用者人工指定执行者，覆盖自动选择并留痕（FR-024｜R-12）。

    三条 MUST（owner 2026-10-12 裁决"必须比 PATCH 更严"）逐条对应下面的三道检查，顺序是定的：
    **先资格、再自审、后可闭合** —— 反过来会把"根本没资格的人"送进闭合判定，报出来的理由是误导。
    """
    with _store(request) as session:
        card = _load_card(session, body.feature_request_id, task_id)

        if str(card.status) not in ASSIGNABLE_STATES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"not_assignable：当前状态是 {card.status}，改派只在派发前有效"
                    f"（允许 {sorted(ASSIGNABLE_STATES)}）；执行中换人走 FR-026 的 handoff（T081），不是这条端点"
                ),
            )

        try:
            task_need = select_worker.task_need_of(card)
        except select_worker.SkillTagsEmpty as exc:
            # 键①分母为 0 ⇒ 响亮失败。文档给的形态是"GUARD_VIOLATION + 卡点 B"，而 B 类在 MVP-1 是结构预留
            # （`approval/service.py` 的 `ACTIVE_KINDS` 只有 A / D）⇒ 与 `guard_not_closable` 同口径先报 422，
            # B 启用时改的是出口，不是判据。
            raise HTTPException(status_code=422, detail=f"guard_violation：{exc}") from exc

        candidates, dropped = select_worker.candidates_from_registry(session)
        ranking = select_worker.rank_candidates(task_need, candidates, dropped)

        # MUST ①：走选人判据 —— 覆盖的是排序结果的**选择权**，不是**资格**。满载 / 停用 / 无申报的人指不了。
        if body.agent_id not in {ranked.agent_id for ranked in ranking.ordered}:
            reason = next((detail for agent_id, detail in ranking.excluded if agent_id == body.agent_id), None)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"override_not_eligible：{body.agent_id!r} 本轮不可参选"
                    + (f"（{reason}）" if reason else "（它不在候选集里：未注册、已停用或缺少能力申报）")
                ),
            )

        # MUST ②：三岗互斥。判定本体在 `guard/triple.py`（T085，owner 2026-10-14 裁"改调用点、不改判据"）——
        # 这里给的是**改派之后的三元组**（把新执行者放进 executor 位，两个把关位取库里现值），
        # 走 `assert_distinct_so_far` 而不是 `assert_independent`：这条端点跑在 `AssignGuards` **之前**，
        # 那时两个把关位通常还是空的，"完整性"是下发那道断言的要求，不是这道改派的要求。
        prospective = gp.Assignment(
            card.id,
            {
                "executor": gp.SeatOccupant(body.agent_id, None),
                "reviewer": gp.SeatOccupant(getattr(card, "reviewer_agent_id"), None),
                "acceptor": gp.SeatOccupant(getattr(card, "acceptor_agent_id"), None),
            },
        )
        try:
            gp.assert_distinct_so_far(prospective)
        except gp.TripleViolation as exc:
            raise HTTPException(status_code=422, detail=f"self_review_forbidden：{exc}") from exc
        try:
            assign_guards.assert_closable(
                [assign_guards.CardNeed(card.id, produced_by=body.agent_id)], _guard_pool(session, request)
            )
        except assign_guards.GuardNotClosable as exc:
            raise HTTPException(status_code=422, detail=f"guard_not_closable：{exc}") from exc

        # MUST ③：留痕。写入点仍只有 `apply_selection()`（T070）一处 ⇒ 覆盖与自动共用同一份审计形状，
        # 不会出现"自动选人记了键序、人工覆盖只记了个名字"那种两套口径。
        overridden = ranking.with_override(body.agent_id)
        select_worker.apply_selection(
            session,
            overridden,
            feature_request_id=body.feature_request_id,
            actor_agent_id=body.operator,
            created_at=dt.datetime.now(dt.timezone.utc),
            overridden=True,
            by=body.operator,
        )

        session.commit()  # 落库：提交点在本端点（与 approvals/requests 同形）；close 在 _store 的 finally
        return WorkerOverrideResponse(
            task_id=card.id,
            feature_request_id=body.feature_request_id,
            executor_agent_id=body.agent_id,
            auto_selected=overridden.auto_selected,
            overridden=True,
            keys=list(overridden.keys),
            skipped=list(overridden.skipped),
        )


# ------------------------------------------------------------------ 人工出口（T093 / 待裁 ⑫）
#
# **独立一条路由，不并入 PATCH**（owner 2026-10-14 待裁 ⑫ 的裁定，原话："escalation.py 已做，
# 与 PATCH 的语义不同"）。不同在哪三处，逐条写清以免后来人被"看起来差不多"说服而合并它：
# ① PATCH 的入口态是 `DRAFT`（下发**前**调字段），这里的入口态是 `ESCALATED`（升级**后**接手）；
# ② PATCH 改的是可改字段白名单，这里只改 `status` 一列，而 `status` 在 §1.2 那份不可改清单里；
# ③ PATCH 的主语可以是系统，这里的主语 MUST 是人（见下面 `principal` 那道门）。
# 决策本体不在本文件 —— 取值合法性、状态门禁、留痕形状全在 `orchestrate/nodes/escalation.py`，
# 这里只做 HTTP 形状与状态码映射（原则 I：出口只有一处判，端点不重判一遍）。
ESCALATION_STATUS_BY_CODE: dict[str, int] = {
    "card_not_found": 404,
    "state_not_escalated": 409,
    "exit_not_allowed": 422,
    "principal_required": 422,
}


class EscalationDecisionBody(BaseModel):
    """`principal` 是**做这个决定的人**，与 T071 的 `operator` 同一语义、不是同一个字。

    `decision` 不用 `Literal["READY", "REJECTED"]` 写死：那两个值是状态机文档里的一条边，
    写进类型就等于把权威抄成第二份（`data-model.md §2` 新立的"域值只引用不重述"）。
    合法性由 `escalation.exit_targets_in_doc()` 在运行时从原文解析，文档加第三个出口而这里没跟，
    红的是那条判据，不是这个类型。
    """

    model_config = ConfigDict(extra="forbid")

    feature_request_id: uuid.UUID
    decision: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    reason: str = ""


class EscalationDecisionResponse(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    decision: str
    status_after: str
    principal: str
    executor_agent_id: str | None
    #: 响应里带这两个计数，是因为 FR-036 之后"人工接手有没有顺手改了别的列"必须一眼可见：
    #: 这条路由只动 `status`，所以这两个数在 200 响应里必须与进来时一样。
    handoff_count: int
    rework_count: int


@router.post("/tasks/{task_id}/escalation", response_model=EscalationDecisionResponse)
def resolve_escalation(task_id: str, body: EscalationDecisionBody, request: Request) -> EscalationDecisionResponse:
    """待裁 ⑫：停在 `ESCALATED` 的卡交回人工决定的两个出口之一（`data-model.md:303`）。"""
    with _store(request) as session:
        card = _load_card(session, body.feature_request_id, task_id)

        # 自动主体不许当"人"。这条不在 escalation.py 里判：那里收的是"谁做的决定"这个事实，
        # 而 `orchestrator` 是系统自己的名字 —— 系统把自己签成人工出口，就是全流程唯一那条死路的假通过。
        if body.principal.strip() == select_worker.AUTOMATED_ACTOR:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"human_principal_required：`principal` 不能是自动主体 {select_worker.AUTOMATED_ACTOR!r}"
                    " ⇒ 这条边的全部意义是'这里有人做过判断'，机器自述成人等于把升级又还回机器"
                ),
            )

        try:
            outcome = escalation.resolve_escalation(
                session,
                feature_request_id=body.feature_request_id,
                task_id=task_id,
                decision=body.decision,
                principal=body.principal,
                reason=body.reason,
                created_at=dt.datetime.now(dt.timezone.utc),
            )
        except escalation.EscalationMisuse as exc:
            status = ESCALATION_STATUS_BY_CODE.get(exc.code, 500)
            raise HTTPException(status_code=status, detail=f"{exc.code}：{exc}") from exc

        # 与 PATCH / worker 同一口径：只 flush，不 commit —— 提交边界在调用方的 UoW 里。
        session.flush()
        session.refresh(card)
        session.commit()  # 落库：提交点在本端点（与 approvals/requests 同形）；close 在 _store 的 finally
        return EscalationDecisionResponse(
            task_id=outcome.card_id,
            feature_request_id=body.feature_request_id,
            decision=outcome.decision,
            status_after=outcome.status_after,
            principal=outcome.principal,
            executor_agent_id=outcome.executor_agent_id,
            handoff_count=int(card.handoff_count),
            rework_count=int(card.rework_count),
        )


# ------------------------------------------------------------------ `ARTIFACT_INVALID` 的人工出口（2026-10-28）
#
# **为什么又是一条独立路由**：与 escalation 同一条理由（待裁 ⑫ 的裁定：`status` 在 §1.2 的不可改清单里，
# 所以它不能走 PATCH 的可改字段白名单）；而且这两条边的**入口态不同、下家不同** ——
# `ESCALATED` 的下家是 `READY / REJECTED`（人决定要不要继续做），
# `ARTIFACT_INVALID` 的下家只有 `HANDED_OFF`（人决定"这张卡再跑一次"），
# 合进一条路由就得在同一个 body 里塞两套取值域，那正是"一个端点两种语义"。
# 判据全在 `orchestrate/nodes/artifact_retry.py`（原则 I：出口只有一处判，端点不重判一遍）。
ARTIFACT_RETRY_STATUS_BY_CODE: dict[str, int] = {
    "card_not_found": 404,
    "state_not_artifact_invalid": 409,
    "principal_required": 422,
    "executor_required": 422,
    # 文档锚点读不出来是**部署形态**坏了（`data-model.md` 被改过），不是调用方的错 ⇒ 500。
    "doc_anchor_ambiguous": 500,
    "doc_anchor_unparsed": 500,
}


class ArtifactRetryBody(BaseModel):
    """`principal` 与 escalation 那条同一个语义：**做这个决定的人**，不是自动主体。

    不带 `decision`：这条边只有一个合法下家，取值由 `artifact_retry.exit_target_in_doc()` 从状态机原文解析。
    让人去选一个只有一个选项的东西，等于把"文档说去哪"变成"调用方猜去哪"。
    """

    model_config = ConfigDict(extra="forbid")

    feature_request_id: uuid.UUID
    principal: str = Field(min_length=1)
    reason: str = ""


class ArtifactRetryResponse(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    prior_status: str
    status_after: str
    principal: str
    executor_agent_id: str | None
    #: 这两个计数在响应里，是为了让"重跑有没有顺手改了别的列"一眼可见：
    #: 这条路由只动 `status`，所以它们必须与进来时一样（`handoff_count` 计的是换人，原地重跑不是换人）。
    handoff_count: int
    rework_count: int


@router.post("/tasks/{task_id}/artifact-retry", response_model=ArtifactRetryResponse)
def retry_artifact(task_id: str, body: ArtifactRetryBody, request: Request) -> ArtifactRetryResponse:
    """把被门禁判为产物不达标、且**没有出边**的卡放回可派发态（`ARTIFACT_INVALID ──▶ HANDED_OFF`）。

    现场（2026-10-28，单 `9b715923` 的 T03）：执行器返回零产物 ⇒ `examined: 0` ⇒ `ARTIFACT_INVALID`；
    这一格不在 `dispatch.DISPATCHABLE` 里，于是「继续执行」推不动它、评审列表里也没有它 ——
    人只能手改库。这条路由就是那一格的出口。
    """
    with _store(request) as session:
        card = _load_card(session, body.feature_request_id, task_id)

        # 自动主体不许当"人"：理由与 escalation 那条逐字相同 —— 机器自述成人，
        # 这条人工出口留下的痕就没有认领者，"这里有人判过"这句话是假的。
        if body.principal.strip() == select_worker.AUTOMATED_ACTOR:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"human_principal_required：`principal` 不能是自动主体 {select_worker.AUTOMATED_ACTOR!r}"
                    " ⇒ 重跑是人工决定，不是系统给自己发一张通行证"
                ),
            )

        try:
            outcome = artifact_retry.resolve_artifact_retry(
                session,
                feature_request_id=body.feature_request_id,
                task_id=task_id,
                principal=body.principal,
                reason=body.reason,
                created_at=dt.datetime.now(dt.timezone.utc),
            )
        except artifact_retry.ArtifactRetryMisuse as exc:
            status = ARTIFACT_RETRY_STATUS_BY_CODE.get(exc.code, 500)
            raise HTTPException(status_code=status, detail=f"{exc.code}：{exc}") from exc

        session.flush()
        session.refresh(card)
        session.commit()  # 落库：提交点在本端点（与 approvals/requests/escalation 同形）
        return ArtifactRetryResponse(
            task_id=outcome.card_id,
            feature_request_id=body.feature_request_id,
            prior_status=outcome.prior_status,
            status_after=outcome.status_after,
            principal=outcome.principal,
            executor_agent_id=outcome.executor_agent_id,
            handoff_count=int(card.handoff_count),
            rework_count=int(card.rework_count),
        )


# ------------------------------------------------------------------ 人工评审入口（M2-W8）

#: 请求侧的两个结论词到域值的翻译只在 `review.service.VERDICT_WORDS` 一处；这里不抄第二份。


class ReviewFindingBody(BaseModel):
    """一条标准的人工判定。`extra="forbid"` 与 `TaskPatch` 同一口径：多写的键 422 而不是被忽略。"""

    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(min_length=1)
    met: bool
    evidence: str = ""


class ReviewSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_request_id: uuid.UUID
    verdict: str
    findings: list[ReviewFindingBody] | None = None
    evidence: str = ""
    #: 只在"最后一张卡过审、全部交付到位"那一发用得上；缺省取装配点的 `review_entry_name`。
    entry_name: str | None = None


class DeliveryView(BaseModel):
    deliverable_id: uuid.UUID
    entry_path: str
    manifest_count: int
    cards_delivered: int
    package_path: str
    package_bytes: int


class ReviewResponse(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    project_key: str
    #: EN-08 的三值（`APPROVE / CHANGES_REQUESTED / NEED_HUMAN`）。保持 `str` 是为了不在此抄第二份
    #: 域字面量；三值由 `values_of("review_verdict")` 与 `tests/contract/test_review_three_value.py` 钉住。
    verdict: str
    card_status: str
    round_no: int
    findings: int
    accepted: bool
    acceptance_proxy: str | None
    accept_error: str | None
    delivery: DeliveryView | None
    delivery_skipped: str | None
    delivery_error: str | None
    next: str


class ReviewArtifactView(BaseModel):
    """一份已验证产物。`content` 只在 `bus_root` 配好且文件读得出来时非空；原因落 `content_error`。"""

    task_id: str
    writer_agent_id: str
    rel_path: str
    format: str
    bytes: int
    lines: int
    content: str | None
    content_error: str | None


class ReviewCardView(BaseModel):
    task_id: str
    feature_request_id: uuid.UUID
    title: str
    status: str
    reviewer_agent_id: str | None
    acceptor_agent_id: str | None
    acceptance_criteria: list[str]
    valid_artifacts: int
    requires_open: bool
    artifacts: list[ReviewArtifactView]


class ReviewReportView(BaseModel):
    """一次自动审查尝试的只读视图（EN-18）。

    **不返回原始 prompt / 原始响应正文**：产品表只存结构化 criteria/issues 与 sha256、
    长度、延迟、token；原始正文只在 POC 证据目录（plan 附录 A）。
    """

    report_id: uuid.UUID
    task_id: str
    feature_request_id: uuid.UUID
    attempt_no: int
    decision: str
    model_decision: str | None
    reason: str | None
    reason_detail: str | None
    criteria: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    reviewer_agent_id: str
    model_id: str | None
    prompt_sha256: str | None
    raw_sha256: str | None
    raw_chars: int
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    verdict_id: uuid.UUID | None
    verdict_round_no: int | None
    approval_ticket_id: uuid.UUID | None
    created_at: dt.datetime


def _review_bus_root(request: Request) -> Path | None:
    raw = getattr(request.app.state, "bus_root", None)
    return Path(raw) if raw is not None else None


@router.get("/tasks/reviewing", response_model=list[ReviewCardView])
def list_pending_reviews(request: Request, feature_request_id: uuid.UUID | None = None) -> list[ReviewCardView]:
    """看板/CLI 的待评审列表。`RUNNING` 那一档是旧现场的补开入口（门禁必须已写过 valid 产物）。"""
    with _store(request) as session:
        rows = review_service.pending_reviews(
            session, feature_request_id, bus_root=_review_bus_root(request)
        )
    return [
        ReviewCardView(
            task_id=row.task_id,
            feature_request_id=row.feature_request_id,
            title=row.title,
            status=row.status,
            reviewer_agent_id=row.reviewer_agent_id,
            acceptor_agent_id=row.acceptor_agent_id,
            acceptance_criteria=list(row.acceptance_criteria),
            valid_artifacts=row.artifact_count,
            requires_open=row.requires_open,
            artifacts=_artifact_views(row.artifacts),
        )
        for row in rows
    ]


# ------------------------------------------------------------------ 读侧：任务清单与单卡详情（Phase 15）
#
# M2-W20 那一格的关闭条件写成的是关系式："出现任一条 `GET /tasks*` ⇒ 图那一栏 MUST 与该端点同笔落"。
# 本笔就是那条端点，看板 DAG 与任务卡列表紧随其后（前端另起一笔）。
# 在此之前，公开面上 `/tasks` 那一族**三个操作全是写**，唯一的拓扑读数 `card_order` 挂在
# `PATCH` 的响应里 —— 打开页面拿不到任何一张卡，空 DAG 会被读成"今天没有任务"。
#
# **拓扑序的载体只有一处**：`_topo_order` 调的就是写侧那一个 `normalize()`（FR-005 的唯一强制点），
# 让 `GET /tasks` 的**数组次序本身**就是节点序、边在每行的 `depends_on` 里。`PATCH` 响应里那个
# `card_order` 字段照旧存在（它是"改完这张卡之后"的即时读数），但看板不读它 —— 两边不是两份排序
# 算法，是同一个函数的两个调用点，所以这里不需要"两份实现的关键字相等"那种符合性判据。
#
# **路由顺序是结构性的**：`GET /tasks/reviewing`（上面那一发）MUST 声明在本节任何
# `GET /tasks/{task_id}` **之前**。FastAPI 按声明顺序匹配路径，把动态段排前面就会让
# `/tasks/reviewing` 被当成"一张卡号叫 `reviewing` 的请求" ⇒ 404 `task_not_found`，
# 而待评审面板读到空 —— 症状与"没有待评审的卡"完全一样，是这里最难归因的一种坏。
# 判据 `test_the_reviewing_list_is_not_swallowed_by_the_card_route` 钉住这一格。
#
# 两条读端点**都不写库**，所以走 `_store`（出去一定 `close()`）而一次 `commit` 都不发：
# 读请求提交 = 把只读请求变成写事务的开头，那是 owner 撞过的 `idle in transaction` 的另一半
# （口径同 `tests/integration/test_approval_commit_boundary.py` 那条形状看守）。
#
# `GET /tasks` 的 `feature_request_id` / `project_key` / `status` 三个过滤项**都是可选**：
# 看板一次进页面要的是"这台机器上现在有哪些卡"，逼调用方先知道自己不知道的需求单号就成了死循环。
# 三者叠加（AND），全空就是全量。

#: 状态过滤的合法取值唯一来源是域定义（原则 I：不在端点里重述一遍取值清单）。
TASK_STATE_DOMAIN: tuple[str, ...] = tuple(values_of("task_card_state"))


class ArtifactFloorView(BaseModel):
    """产物门禁的三个实测下限（FR-019 的判据本体在 `nodes/validate_artifact.py`，这里只报现值）。"""

    min_bytes: int
    min_lines: int
    format: str


class TaskArtifactRef(BaseModel):
    """列表里的一份产物 = **目录形状**（路径 + 实测数值），不带源码。

    源码归详情：一次列表请求把整棵工作区读进内存不是"看板快一点"，而是把 N 个文件的内容
    塞进每一发的列表响应里。面板点开某一份才去读，本来就是它该有的那一次额外请求。
    """

    task_id: str
    writer_agent_id: str
    rel_path: str
    format: str
    bytes: int
    lines: int


class TaskManifestEntryView(BaseModel):
    """交付包成员索引的一行（`assemble.validated_manifest` 的形状，字段名照 `ManifestEntry`）。"""

    rel_path: str
    producer_agent_id: str
    task_card_id: str
    bytes: int
    lines: int
    format: str


class TaskReviewRoundView(BaseModel):
    """一轮审查。轮次、结论、时刻都取自 EN-14 那一行留痕 —— 它是这张表里**唯一有时序**的载体。

    `item_findings` 是从 EN-08 配对来的（见 `_review_history` 里那条限度说明）：配不上时
    `verdict_id=None` 且条目为空，端点不编一份"看起来完整"的历史。
    """

    round_no: int
    #: 同 `ReviewResponse.verdict`：三值都可能出现在历史里（`NEED_HUMAN` 行由自动审查写入）。
    verdict: str
    findings: int
    actor_agent_id: str
    created_at: dt.datetime
    card_status_after: str | None
    verdict_id: uuid.UUID | None
    item_findings: list[dict[str, Any]]


class TaskFields(BaseModel):
    """两条读端点**共用**的那一组列：一张卡"是谁、在哪张单下、现在什么态、三岗坐着谁"。

    共用一个基类不是省事，是防漂移：列表与详情对同一个列各读各的，就会有一处 `str()` 一处不 `str()`，
    那时坏的是前端"两边状态词不一样"这种没法归因的形状。
    """

    id: str
    feature_request_id: uuid.UUID
    project_key: str
    #: EN-01 的状态（原则 I：需求单的态只从需求单行读，卡上不复述）。
    request_status: str
    #: 看板拿它挑"最新那一张需求单"，否则公开面上没有任何一处能排出请求的新旧。
    request_created_at: dt.datetime
    #: 这批卡是怎么来的（`llm` / `prd` / `fallback_stub` / `stub`；`None` = 本列上线前的历史行）。
    #: `fallback_stub` / `stub` 是降级 —— 看板据此打"未接入真模型"的标识。
    split_source: str | None
    title: str
    status: str
    depends_on: list[str]
    executor_agent_id: str | None
    reviewer_agent_id: str | None
    acceptor_agent_id: str | None
    difficulty: int
    skill_tags: list[str]
    #: FR-026 的换人上限（≤1）就在这一个读数上；徽章要能一眼看出"这卡换过手"。
    handoff_count: int
    acceptance_criteria: list[str]
    #: 这张卡属于流水线的哪一段：`doc` / `build` / `gate` / `summary`。
    #: 前端执行看板按它分行（owner 2026-10-28：「不同任务竖向排列，同类任务横向排列」）。
    #: 判据在 `reporting/cards.py::family_of` —— **不按卡号段猜**，读的是建卡那一刻写下的标签。
    family: str


class TaskSummaryView(TaskFields):
    """`GET /tasks` 的一行。"""

    artifacts: list[TaskArtifactRef]
    #: 这张卡的验收是**机器代签**的吗（`review/runner.py` 在低风险自动验收时写的 EN-14）。
    #: 前端据此在流水线节点上打 `Auto-Approved` 徽章 —— 那是"这不是人签的字"的唯一可见证据。
    auto_approved: bool = False
    #: 机器审查给过的建议结论（`APPROVE` / `None`）。非空而 `auto_approved=False` 的意思是
    #: "机器审过了、但这份得人签"，与"机器根本没审"是两件事，界面上不该长得一样。
    auto_review: str | None = None


class TaskDetailView(TaskFields):
    """`GET /tasks/{task_id}` 的一整张卡：形状 + 产物源码 + 整单清单 + 审查史。"""

    artifact_floor: ArtifactFloorView
    token_budget: Decimal | None
    deadline_minutes: int | None
    #: 验收轮次（裁决 ⑬ 之后这一列只计验收轮，不计审查轮 —— 审查轮在 `review_history` 里数）。
    review_round: int
    rework_count: int
    snapshot_id: uuid.UUID
    artifacts: list[ReviewArtifactView]
    manifest: list[TaskManifestEntryView]
    review_history: list[TaskReviewRoundView]


def _requests_by_id(session, cards: list[TaskCard]) -> dict[uuid.UUID, FeatureRequest]:
    """按卡集合**一次**取回 EN-01。逐卡 `session.get` 会把列表请求变成 N 次往返。"""
    ids = {card.feature_request_id for card in cards}
    if not ids:
        return {}
    rows = session.query(FeatureRequest).filter(FeatureRequest.id.in_(ids)).all()
    return {row.id: row for row in rows}


def _common_fields(card: TaskCard, request_row: FeatureRequest) -> dict[str, Any]:
    """把一张卡 + 它的母单折成共用字段。枚举与数组在这里统一成 JSON 形状，别处不再各折一次。"""
    return {
        "id": str(card.id),
        "feature_request_id": card.feature_request_id,
        "project_key": str(request_row.project_key),
        "request_status": str(request_row.status),
        "request_created_at": request_row.created_at,
        "split_source": None if request_row.split_source is None else str(request_row.split_source),
        "title": str(card.title),
        "status": str(card.status),
        "depends_on": [str(item) for item in (card.depends_on or ())],
        "executor_agent_id": card.executor_agent_id,
        "reviewer_agent_id": card.reviewer_agent_id,
        "acceptor_agent_id": card.acceptor_agent_id,
        "difficulty": int(card.difficulty),
        "skill_tags": [str(item) for item in (card.skill_tags or ())],
        "handoff_count": int(card.handoff_count),
        "acceptance_criteria": [str(item) for item in (card.acceptance_criteria or ())],
        "family": cards.family_of(card),
    }


def _artifact_refs(items: Sequence[review_service.PendingArtifact]) -> list[TaskArtifactRef]:
    return [
        TaskArtifactRef(
            task_id=item.task_id,
            writer_agent_id=item.writer_agent_id,
            rel_path=item.rel_path,
            format=item.format,
            bytes=item.bytes,
            lines=item.lines,
        )
        for item in items
    ]


def _artifact_views(items: Sequence[review_service.PendingArtifact]) -> list[ReviewArtifactView]:
    return [
        ReviewArtifactView(
            task_id=item.task_id,
            writer_agent_id=item.writer_agent_id,
            rel_path=item.rel_path,
            format=item.format,
            bytes=item.bytes,
            lines=item.lines,
            content=item.content,
            content_error=item.content_error,
        )
        for item in items
    ]


def _review_history(session, card: TaskCard) -> list[TaskReviewRoundView]:
    """从 EN-14 的 `REVIEW` 留痕还原审查轮次，并把 EN-08 的逐条判定配回去。

    为什么顺序只能来自留痕：`review_verdict` **没有时间列也没有轮次列**（`data-model.md` EN-08 就给了
    这七个字段），主键还是随机 UUID ⇒ 按它排出来的顺序是 PG 的偶然，不是审查的次序。而
    `record_verdict` 每一轮都写一条带 `round_no` / `verdict` / `findings` / `card_status_after` 的留痕，
    `event_log.id` 是插入序 —— 时序在那里，就从那里读。

    **配对的那一格限度必须点名**：留痕与结论行之间没有外键，这里按 `(结论词, 条目数)` 认领。
    两轮结论不同或条目数不同 ⇒ 认领是唯一的；两轮**同为 APPROVE 且条目数相同**时，两条结论行
    可以互换而库里没有任何读数能分出谁属于哪一轮 —— 那种情况下 `verdict_id` 仍会给出（给的是
    按 PG 返回顺序的第一条），但"这一轮的逐条证据就是这一份"这句话在那个交点上不成立。
    补这一格的代价是给 EN-08 加一列（迁移 + 冻结的域表），不在本笔范围。
    """
    events = (
        session.query(EventLog)
        .filter(
            EventLog.entity == "task_card",
            EventLog.entity_id == card.id,
            EventLog.kind == "REVIEW",
        )
        .order_by(EventLog.id)
        .all()
    )
    pool = list(
        session.query(ReviewVerdict)
        .filter(
            ReviewVerdict.feature_request_id == card.feature_request_id,
            ReviewVerdict.task_card_id == card.id,
        )
        .all()
    )
    history: list[TaskReviewRoundView] = []
    for event in events:
        payload = dict(event.payload or {})
        if payload.get("action") != review_node.ACTION_VERDICT:
            continue
        verdict = str(payload.get("verdict") or "")
        findings = int(payload.get("findings") or 0)
        match = next(
            (
                row
                for row in pool
                if str(row.verdict) == verdict and len(row.item_findings or ()) == findings
            ),
            None,
        )
        if match is not None:
            pool.remove(match)
        after = payload.get("card_status_after")
        history.append(
            TaskReviewRoundView(
                round_no=int(payload.get("round_no") or 0),
                verdict=verdict,
                findings=findings,
                actor_agent_id=str(event.actor_agent_id),
                created_at=event.created_at,
                card_status_after=str(after) if after is not None else None,
                verdict_id=match.id if match is not None else None,
                item_findings=[dict(item) for item in (match.item_findings or ())]
                if match is not None
                else [],
            )
        )
    return history


def _topo_order(
    session, cards: list[TaskCard], requests_by_id: dict[uuid.UUID, FeatureRequest]
) -> list[TaskCard]:
    """需求单从新到旧；同一张单内 = `normalize()` 的拓扑序（FR-005 的唯一强制点）。

    为什么在端点上排而不是让前端排：`plan-web.md` 明令前端不许再算一遍拓扑，而 M2-W20 那条
    重开条件要的形状是"图的**节点序与边由同一条端点给出**"。这里让**数组次序本身**就是节点序
    ⇒ 前端按数组摆位、按 `depends_on` 连线就够，不必自己求一次拓扑（多加一个 `card_order` 字段
    会把同一份顺序在每一行里复述一遍，那是两个载体）。

    喂给归一化器的是**整单**的卡，不是筛过的子集：一张卡依赖的那张正好被 `status` 滤掉时，
    `normalize()` 看到的是悬空引用 ⇒ 端点会把"你筛掉了它"报成"库里的结构坏了"。发射时再按
    可见集裁掉筛掉的行，顺序仍取自整单的拓扑序。

    坏结构退 409、不退一张顺序错的图：看板每画一条边都在宣称「先做这个、再做那个」，一张
    排错的 DAG 比空白更骗人（口径同 M2-W20 那句"空骨架会被读成今天没有任务"）。
    """
    if not cards:
        return []
    grouped: dict[uuid.UUID, dict[str, TaskCard]] = {}
    for card in cards:
        grouped.setdefault(card.feature_request_id, {})[card.id] = card
    keys = list(grouped)
    whole: dict[uuid.UUID, dict[str, TaskCard]] = {key: {} for key in keys}
    for row in session.query(TaskCard).filter(TaskCard.feature_request_id.in_(keys)).all():
        whole[row.feature_request_id][row.id] = row

    ordered: list[TaskCard] = []
    for key in sorted(keys, key=lambda item: requests_by_id[item].created_at, reverse=True):
        by_id = whole[key]
        try:
            sequence = normalize([_draft_of(by_id[card_id]) for card_id in sorted(by_id)])
        except DependencyViolation as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"dependency_structure_invalid：需求单 {key} 下的依赖结构过不了 FR-005 的"
                    f"唯一强制点（{exc}）⇒ 这一屏宁可不画，也不画一张顺序错乱的图"
                ),
            ) from exc
        visible = grouped[key]
        ordered.extend(by_id[card_id] for card_id in sequence if card_id in visible)
    return ordered


@router.get("/tasks", response_model=list[TaskSummaryView])
def list_tasks(
    request: Request,
    feature_request_id: uuid.UUID | None = None,
    project_key: str | None = None,
    status: str | None = None,
) -> list[TaskSummaryView]:
    """任务清单：DAG 的节点与边、任务卡列表、文件树都从这一发出。

    排序 = **需求单从新到旧，同一张单内 = `normalize()` 的拓扑序**（见 `_topo_order`：那一步
    就是"M2-W20 要求节点序由端点给出"的落点）。数组次序即节点序，边在每行的 `depends_on` 里，
    前端拿这一发就够画 DAG —— 它不需要、也不许自己再算一遍拓扑（`plan-web.md` 那条禁令）。
    """
    wanted = (status or "").strip()
    if wanted and wanted not in TASK_STATE_DOMAIN:
        raise HTTPException(
            status_code=422,
            detail=(
                f"status_unknown：{status!r} 不是任务卡状态（可过滤的取值来自 `task_card_state` 域）。"
                "把拼错的状态读成'没有这种卡'会返回一个合法的 200 空数组，那是假成功"
            ),
        )
    key = (project_key or "").strip()
    with _store(request) as session:
        query = session.query(TaskCard)
        if feature_request_id is not None:
            query = query.filter(TaskCard.feature_request_id == feature_request_id)
        if key:
            query = query.join(
                FeatureRequest, FeatureRequest.id == TaskCard.feature_request_id
            ).filter(FeatureRequest.project_key == key)
        if wanted:
            query = query.filter(TaskCard.status == wanted)
        cards = query.all()
        requests_by_id = _requests_by_id(session, cards)
        ordered = _topo_order(session, cards, requests_by_id)
        # 代签标记**一次查完**（不是每张卡一条 SQL）：列表一屏几十张卡，逐卡查会把一次读变成 N 次往返。
        marks = _auto_marks(session, ordered)
        # 产物取数按**需求单**归组，一张单一次查询；`bus_root=None` ⇒ 列表一律不读盘。
        grouped: dict[uuid.UUID, dict[str, tuple[review_service.PendingArtifact, ...]]] = {}
        views: list[TaskSummaryView] = []
        for card in ordered:
            request_row = requests_by_id[card.feature_request_id]
            if card.feature_request_id not in grouped:
                grouped[card.feature_request_id] = review_service.artifacts_by_request(
                    session,
                    card.feature_request_id,
                    project_key=str(request_row.project_key),
                    bus_root=None,
                )
            views.append(
                TaskSummaryView(
                    **_common_fields(card, request_row),
                    artifacts=_artifact_refs(grouped[card.feature_request_id].get(card.id, ())),
                    auto_approved=marks.get(card.id, (False, None))[0],
                    auto_review=marks.get(card.id, (False, None))[1],
                )
            )
    return views


#: 机器代签 / 机器建议这两条留痕的动作名。**与 `review/runner.py` 那两份常量是同一对字符串**，
#: 两边各写一份就会漂成"后端写了、前端读不到"，而那件事不会报错，只会让徽章永远不出现。
AUTO_ACCEPT_ACTION = "review_auto_accepted"
ADVISORY_ACTION = "review_advisory_pass"


def _auto_marks(session, cards: list[TaskCard]) -> dict[str, tuple[bool, str | None]]:
    """`{卡号: (是否机器代签, 机器建议的结论)}`。**只认这两条动作名**，别的一律不看。

    读 EN-14 而不是给 `task_card` 加列：代签不是卡的一个状态（卡还是 `DELIVERED`），
    是**一次发生过的动作**；把它做成列就等于说"这张卡天生是机器签的"，而它随时可能被人重开。
    取每张卡**最新**的那一条：同一张卡返工后再审，前一次的代签记录不该再让徽章亮着。
    """
    ids = [str(card.id) for card in cards]
    if not ids:
        return {}
    rows = session.scalars(
        select(EventLog)
        .where(EventLog.entity == "task_card", EventLog.entity_id.in_(ids))
        .order_by(EventLog.id.desc())
    ).all()
    marks: dict[str, tuple[bool, str | None]] = {}
    for row in rows:
        if row.entity_id in marks:
            continue
        payload = row.payload if isinstance(row.payload, dict) else {}
        action = str(payload.get("action") or "")
        if action == AUTO_ACCEPT_ACTION:
            marks[row.entity_id] = (True, str(payload.get("verdict") or "APPROVE"))
        elif action == ADVISORY_ACTION:
            marks[row.entity_id] = (False, str(payload.get("verdict") or "APPROVE"))
    return marks


@router.get("/tasks/{task_id}", response_model=TaskDetailView)
def get_task(
    task_id: str, request: Request, feature_request_id: uuid.UUID | None = None
) -> TaskDetailView:
    """单卡详情：验收标准、产物门槛、产物源码、整单交付清单、审查史。

    **寻址**：EN-02 的主键是复合的 `(feature_request_id, id)`，而路径里只有 `{task_id}`
    （与 `PATCH` 那条同一件事，理由见模块头）。带 `?feature_request_id=` 就精确取；不带就按卡号找，
    命中 0 张 ⇒ 404，命中多张 ⇒ **409 `task_ambiguous` 并把候选需求单号列出来**。
    这里不选"最新那一张"：卡号跨单重名是常态（每张单都从 `T01` 起号），替调用方猜一张就是
    把另一个项目的产物源码交出去。
    """
    with _store(request) as session:
        query = session.query(TaskCard).filter(TaskCard.id == task_id)
        if feature_request_id is not None:
            query = query.filter(TaskCard.feature_request_id == feature_request_id)
        cards = query.all()
        if not cards:
            scope = f"（需求单 {feature_request_id} 下）" if feature_request_id is not None else ""
            raise HTTPException(status_code=404, detail=f"task_not_found：{scope}没有卡 {task_id}")
        if len(cards) > 1:
            candidates = sorted(str(card.feature_request_id) for card in cards)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task_ambiguous：卡号 {task_id} 同时出现在 {len(cards)} 张需求单下 {candidates}"
                    " ⇒ EN-02 的主键是复合的，请带 ?feature_request_id=；这里不替调用方挑一张"
                ),
            )
        card = cards[0]
        request_row = session.get(FeatureRequest, card.feature_request_id)
        previews = review_service.artifact_previews(
            session,
            card,
            project_key=str(request_row.project_key),
            bus_root=_review_bus_root(request),
        )
        try:
            manifest = assemble_node.validated_manifest(session, card.feature_request_id)
        except assemble_node.AssembleMisuse:
            # `nothing_to_deliver` = 这张单还没有一份 valid 产物。空清单是**真值**，
            # 不是错误：详情端点不该因为"还没交付"而 4xx（那是组包那一支的语义）。
            manifest = ()
        return TaskDetailView(
            **_common_fields(card, request_row),
            artifact_floor=ArtifactFloorView(
                min_bytes=int(card.artifact_floor_min_bytes),
                min_lines=int(card.artifact_floor_min_lines),
                format=str(card.artifact_floor_format),
            ),
            token_budget=card.token_budget,
            deadline_minutes=card.deadline_minutes,
            review_round=int(card.review_round),
            rework_count=int(card.rework_count),
            snapshot_id=card.snapshot_id,
            artifacts=_artifact_views(previews),
            manifest=[
                TaskManifestEntryView(
                    rel_path=entry.rel_path,
                    producer_agent_id=entry.producer_agent_id,
                    task_card_id=entry.task_card_id,
                    bytes=entry.bytes,
                    lines=entry.lines,
                    format=entry.format,
                )
                for entry in manifest
            ],
            review_history=_review_history(session, card),
        )


@router.get("/tasks/{task_id}/review-report", response_model=list[ReviewReportView])
def get_review_report(
    task_id: str,
    request: Request,
    feature_request_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[ReviewReportView]:
    """一张卡的自动审查报告（EN-18，只读；Phase 17 P3 D5）。

    **寻址与 `GET /tasks/{task_id}` 同一口径**：EN-02 的主键是复合的，带
    `?feature_request_id=` 精确取；不带就按卡号找，0 张 404、多张 409 并列候选。
    返回按 `created_at DESC, attempt_no DESC`；`limit` 默认 20、上限 100（越界 422，不静默截断）。

    **路径用单数 `review-report`**（owner 2026-10-23 的 P3 指令）；返回的是该卡的**全部**尝试
    （数组），所以路径名读作"这张卡的审查报告"而不是"第 N 条"。不返回原始 prompt/response 正文。
    """
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=422,
            detail=f"limit_out_of_range：{limit}，允许 1..100（不静默截成上限）",
        )
    with _store(request) as session:
        query = session.query(TaskCard).filter(TaskCard.id == task_id)
        if feature_request_id is not None:
            query = query.filter(TaskCard.feature_request_id == feature_request_id)
        cards = query.all()
        if not cards:
            scope = f"（需求单 {feature_request_id} 下）" if feature_request_id is not None else ""
            raise HTTPException(status_code=404, detail=f"task_not_found：{scope}没有卡 {task_id}")
        if len(cards) > 1:
            candidates = sorted(str(card.feature_request_id) for card in cards)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task_ambiguous：卡号 {task_id} 同时出现在 {len(cards)} 张需求单下 {candidates}"
                    " ⇒ EN-02 的主键是复合的，请带 ?feature_request_id=；这里不替调用方挑一张"
                ),
            )
        rows = review_reports.for_card(
            session,
            task_id,
            feature_request_id=feature_request_id,
            limit=limit,
        )
        return [
            ReviewReportView(
                report_id=row.id,
                task_id=row.task_card_id,
                feature_request_id=row.feature_request_id,
                attempt_no=row.attempt_no,
                decision=row.decision,
                model_decision=row.model_decision,
                reason=row.reason,
                reason_detail=row.reason_detail,
                criteria=list(row.criteria or ()),
                issues=list(row.issues or ()),
                reviewer_agent_id=row.reviewer_agent_id,
                model_id=row.model_id,
                prompt_sha256=row.prompt_sha256,
                raw_sha256=row.raw_sha256,
                raw_chars=row.raw_chars,
                latency_ms=row.latency_ms,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                verdict_id=row.verdict_id,
                verdict_round_no=row.verdict_round_no,
                approval_ticket_id=row.approval_ticket_id,
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.post("/tasks/{task_id}/review", response_model=ReviewResponse)
def submit_task_review(
    task_id: str, body: ReviewSubmitBody, request: Request, background: BackgroundTasks
) -> ReviewResponse:
    """人工/看板提交一条审查结论（FR-028 / FR-029）。

    通过 ⇒ `record_verdict` 把卡推到 `ACCEPTING`，随后按 `human-review-pass` 代填验收条目、
    卡到 `DELIVERED`；全需求单的卡都到终态时再组唯一一份交付包。打回 ⇒ `CHANGES_REQUESTED`，
    不碰验收与交付包。逐条判定的完整性仍由 `review.check_findings()` 判，端点只做 HTTP 翻译。
    """
    principal = _principal(request)
    # 入口名四档，前两档是"有人点了名"，后两档是"推出来的"：
    #   ① 请求体 `entry_name`（这一单临时指定）
    #   ② 启动参数 `--review-entry`（运维显式压过推断；不传时是 None，不再用 `index.html` 盖住 PRD —— 那正是 U2）
    #   ③ PRD 第一页的 `file`（需求自己写的入口，见 `review_service.entry_name_from_digest`）
    #   ④ `DEFAULT_ENTRY_NAME`
    # 原来只有 ①②④，而 ② 的默认值是 `index.html`、PRD 说 `dashboard.html` ⇒ 组包 `entry_absent`，
    # 交付包永远出不来。空串一律当"没点名"，不再 422 —— 空不是一个决定。
    explicit = (body.entry_name or getattr(request.app.state, "review_entry_name", None) or "").strip()
    findings = (
        [(item.criterion, item.met, item.evidence) for item in body.findings]
        if body.findings is not None
        else None
    )
    with _store(request) as session:
        entry_name = (
            explicit
            or review_service.entry_name_for(session, body.feature_request_id)
            or review_service.DEFAULT_ENTRY_NAME
        )
        try:
            outcome = review_service.submit_review(
                session,
                feature_request_id=body.feature_request_id,
                task_id=task_id,
                principal=principal,
                verdict=body.verdict,
                findings=findings,
                evidence=body.evidence,
                bus_root=_review_bus_root(request),
                entry_name=entry_name,
                now=dt.datetime.now(dt.timezone.utc),
            )
        except review_service.ReviewFlowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=f"{exc.code}：{exc}") from exc
        # 提交点在端点这一侧：评审结论与自动验收在同一个事务里，不能一个落了另一个还在会话里。
        session.commit()
    # 通过之后下游卡才会变成 READY。续跑放在响应之后：厂商每张卡可能跑几百秒，
    # 内联会把「通过」这颗按钮拖到超时，而页面上还看不到任何进度。装配面没注续跑者就不排。
    resumer = getattr(request.app.state, "graph_resumer", None)
    if resumer is not None and outcome.verdict == "APPROVE" and outcome.accepted and outcome.delivery is None:
        background.add_task(resumer, feature_request_id=outcome.feature_request_id, conclusion="continue")
    delivery = (
        DeliveryView(
            deliverable_id=outcome.delivery.deliverable_id,
            entry_path=outcome.delivery.entry_path,
            manifest_count=outcome.delivery.manifest_count,
            cards_delivered=outcome.delivery.cards_delivered,
            package_path=outcome.delivery.package_path,
            package_bytes=outcome.delivery.package_bytes,
        )
        if outcome.delivery is not None
        else None
    )
    return ReviewResponse(
        task_id=outcome.task_id,
        feature_request_id=outcome.feature_request_id,
        project_key=outcome.project_key,
        verdict=outcome.verdict,
        card_status=outcome.card_status,
        round_no=outcome.round_no,
        findings=outcome.findings,
        accepted=outcome.accepted,
        acceptance_proxy=outcome.acceptance_proxy,
        accept_error=outcome.accept_error,
        delivery=delivery,
        delivery_skipped=outcome.delivery_skipped,
        delivery_error=outcome.delivery_error,
        next=outcome.next_hint,
    )
