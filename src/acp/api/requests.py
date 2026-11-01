"""T029：`POST /requests` 需求入口（FR-001 ~ FR-003｜R-03）。

FR-001 的"不要求先填表单或遵循模板"落在**请求体的形状**上：整棵树里只有三个字段，其中承载需求的
只有一个自由文本 `raw_input`；没有小节、没有关键词表、没有必须按某种顺序写的字段。
`project_key` 是路由标识（EN-01 该列 NOT NULL，`AI项目台/<project>` 的子目录名），不是需求模板的一部分，
所以保留它而**不自造默认值** —— 拿"当前只有一个项目"猜一个键，等于把用户没说的当成用户说过的。

`extra="forbid"` 与 FR-001 不矛盾：多写的字段被 422 拒掉是**入口的自述能力**，
表现是"这个字段我不认识"，而不是"请按规定模板填"。放开多余字段会让拼错的字段静默变成
"没传"，那才是真的把使用者推进猜模板的境地。

**两个 fail-closed 的 503**：补全器与会话工厂都必须由装配方注入，
缺任何一个都直接 503，不降级、不假装成功。理由写在 `orchestrate/nodes/plan.py` 的模块头：
这里最容易犯的假绿是"塞一个规则版补全器把流程走通"，那会让 FR-002 看起来已实现而它没有。

`serve` 默认注的是 `orchestrate/local_completer.py` 那条**空补全的旁路**（owner 2026-10-22
Phase 15 指令"修复之前报 503 的问题，确保 completer 注入"）。禁令管的是"节点实现体里长出第二个
事实源"，没管装配点；装配点必须自己决定注什么，否则看板连"提一句话需求"这一格都做不出来。
那条旁路如实交空、不编话术，响应体再带 `completer_id` 自报身份 —— 于是 503 那一档现在只表示
"根本没人装配"，而"补全是模型给的还是字符串凑的"这件事在读数上可分。

**提交边界归本端点**（同一句指令的后半："落库 commit"）：首版只有 `add` + `flush`，注释写着
"提交点归装配方（T123 用 scoped_session 按请求收尾）"，而 T123 交付的装配面**从没兑现那句** ——
`serve.build_app` 给的是 `make_session_factory()`，一次请求一个新会话，端点既不 commit 也不 close。
表现就是 owner 撞到的那一格：201 回了 `request_id`，PG 里 0 行，需求单在库里不存在，
看板与 `realrun` 之后全都读不到它。现在这里 `commit` → 失败 `rollback` → 一定 `close`，
与 `api/approvals.py`（笔 U）、`api/tasks.py`（M2-W8）同一口径：**HTTP 载体 = 意图点显式提交 +
一定收尾，绝不套 `persistence/session.new_session`**（它的 `rollback()` 会连测试夹具的外层事务一起回滚）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from acp.api.approvals import PRINCIPAL_HEADER
from acp.config.loader import REPO_ROOT
from acp import routing_vault
from acp.orchestrate import intake
from acp.orchestrate.nodes.plan import (
    BlankRequirement,
    RequirementCompleter,
    plan_requirement,
)
from acp import settings_run
from acp.persistence.models_approval import ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.serve_intake import extend_digest
from acp.persistence.models_registry import Deliverable, EventLog

#: 读"受理为什么失败"时往前翻多少行留痕。取 80 而不是 1：`split_started`、四轮 `meeting_turn`、
#: `docs_*`、`split_llm` 都在同一个实体上，一次失败的现场通常有十几二十行。
_ERROR_SCAN = 80

router = APIRouter()


class ExecutorRouteBody(BaseModel):
    """执行席路由：`qoder`（CLI，默认）或 `api`（纯 API 执行器，模型由 vendor/model 点名）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["qoder", "api"]
    #: 只有 `kind=api` 时才必填（校验在 handler 里做：见 `_routing_payload`）。
    vendor: str = ""
    model: str = ""
    #: 自定义模型池给的兼容端点；空 ⇒ 按 `vendor` 查目录（`settings_run.BASE_URLS`）。
    base_url: str = ""
    #: **只进进程内存**（`routing_vault`），不落库、不回读。
    api_key: SecretStr | None = None
    thinking: str = "medium"


class SeatRouteBody(BaseModel):
    """把关席路由：要么点名一个在册 agent，要么点名一个模型实例（路线 C）。

    模型席位（`vendor` + `model` 齐）会由通用审查/验收 Runner 调用；`agent_id` 形态保持旧语义
    （当席候选里的偏好）。两形态互斥，由 `_routing_payload` 校验。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = ""
    vendor: str = ""
    model: str = ""
    base_url: str = ""
    api_key: SecretStr | None = None
    thinking: str = "medium"


class ModelRoutingBody(BaseModel):
    """`model_routing` 的请求体（2026-10-31 路线 B）。

    形状与三条口径的唯一载体是 `orchestrate/routing.py` 的模块说明；这里只做**形状**校验
    （`extra=forbid`、`kind` 二值），"这支 agent 在不在册/在不在候选池"归编排层判。
    """

    model_config = ConfigDict(extra="forbid")

    executor: ExecutorRouteBody | None = None
    reviewer: SeatRouteBody | None = None
    acceptor: SeatRouteBody | None = None


class IntakeRequest(BaseModel):
    """需求入口的请求体：两个必填 + 一个可选。"""

    model_config = ConfigDict(extra="forbid")

    project_key: str = Field(min_length=1)
    raw_input: str = Field(min_length=1)
    # 使用者对上一轮澄清问题的回答；MVP-1 的入口是"一句话先来"，所以默认空（FR-003 的不阻塞路径）。
    clarification_answers: list[str] = Field(default_factory=list)
    #: 请求级路由（2026-10-31 路线 B）。缺省 = 老行为：执行者排序、把关席从 EN-04 池里填。
    model_routing: ModelRoutingBody | None = None


class RequestView(BaseModel):
    """一条已落库需求的读面（2026-10-27 会议接线时补）。

    为什么需要它：网页提交之后，四岗位会议的台词写在 `summary_digest` 的「会议」段里，
    而 `POST /requests` 只回一次体、没有回读面 —— 页面刷新或后台写完之后，前端就再也拿不到那四句话。
    这个端点回的就是那一行本身（含 `raw_input`：前端此前只能把它存在 localStorage 里）。

    不做的两件事：不回 `feature_request` 之外的行（卡与留痕各有自己的读面），
    也不回任何凭据/模型 id。
    """

    request_id: uuid.UUID
    project_key: str
    raw_input: str
    status: str
    #: 这批卡是怎么来的（2026-10-31）。`fallback_stub` / `stub` = 没接真模型，屏上要显眼标出来。
    split_source: str | None
    #: 这一单的请求级路由（路线 B）。`None` = 老请求：执行者与把关席都走自动路径。
    model_routing: dict[str, Any] | None = None
    summary_digest: dict[str, list[str]]
    clarification_waived: bool
    created_at: dt.datetime
    #: 受理失败的原因（U6，2026-10-28）。`None` = 没失败过。
    #: 取的是最近一行 `split_failed` / `intake_failed` 留痕的 `error`；不新加列，因为
    #: "这次为什么失败"是一次的事实，不是这条需求单的属性 —— 为它加列就要为"重试成功后清空"
    #: 再写一条规则，而留痕里本来就有一行。
    last_error: str | None = None
    #: 失败的那一次归因码（`docs_timeout` / `split_prd_no_pages` 之类），与 `last_error` 同源。
    last_error_code: str | None = None


class IntakeResponse(BaseModel):
    request_id: uuid.UUID
    project_key: str
    status: str
    summary_digest: dict[str, list[str]]
    clarification_waived: bool
    #: 这一份摘要**是谁**补的。`local-passthrough` = 本机旁路（什么都没补），不是 FR-002 的实证；
    #: 真适配器注进来时这里是它自己的标识。看板 MUST 把它显示出来，别把旁路读成总管。
    completer_id: str
    #: 后台拆解是否真的排上了（装配方注入了 `intake_runner`）。缺省 false 时页面不许说"正在拆解"。
    intake_scheduled: bool


@dataclass(frozen=True)
class StoredRequest:
    """落库之后**再读出来**的那一份读数。

    为什么不直接把 ORM 对象交回给 `create_request` 拼响应：`_persist` 现在会 `close()` 它借到的会话，
    而会话工厂的 `expire_on_commit` 不由本端点决定（生产工厂设了 `False`，测试工厂未必）。
    先取快照再收尾，"响应里的字段来自库里那一行"这句才有明确的时点。
    """

    request_id: uuid.UUID
    project_key: str
    status: str
    clarification_waived: bool


def _completer(request: Request) -> RequirementCompleter:
    completer = getattr(request.app.state, "completer", None)
    if completer is None:
        raise HTTPException(
            status_code=503,
            detail="completer_not_configured：总管补全能力由 Phase 3 的适配器提供，装配时注入 app.state.completer",
        )
    return completer


def _completer_id(completer: Any) -> str:
    """补全器的自报身份。没有 `completer_id` 的东西就退回它的类名 —— 那也是一个可归因的读数。"""
    return str(getattr(completer, "completer_id", None) or type(completer).__name__)


def _persist(request: Request, row: FeatureRequest) -> StoredRequest:
    """把 EN-01 真发到 PG：`add` → `commit` → 取快照 → 一定 `close`。

    - `commit` 是这一格的存在理由：没有它，写入只停在这条连接的事务里，进程一退就没，
      而响应已经回了 201 与 `request_id`（owner 在生产面上撞到的正是这一形状）。
    - 失败必须 `rollback` 再冒出去：半途失败的会话若脏着还回池子，下一发请求会在那团脏状态上继续写。
    - `close` 放在 `finally`：未提交的意图由收尾丢弃，而不是靠下一发请求替我们收拾。
    - 提供方给的是**工厂**（一次请求一个会话），所以本函数拥有这个会话的整个生命周期；
      集成用例把会话绑在外层事务的连接上时，`commit()` 发不到 PG 而 `close()` 不动外层 ——
      隔离仍由夹具那次 `rollback()` 提供，这是 `test_request_intake.py` 的夹具形状。
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：EN-01 的写入需要注入 app.state.session_factory"
        )
    session = factory()
    try:
        session.add(row)
        session.commit()
        stored = StoredRequest(
            request_id=row.id,
            project_key=row.project_key,
            status=str(row.status),
            clarification_waived=bool(row.clarification_waived),
        )
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return stored


@router.get("/requests/{request_id}", response_model=RequestView)
def get_request(request_id: uuid.UUID, request: Request) -> RequestView:
    """读回一条需求（含摘要里各段，会议台词就在「会议」那一段）。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：需求读面需要注入 app.state.session_factory"
        )
    session = factory()
    try:
        row = session.get(FeatureRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        digest = {
            str(key): [str(item) for item in value]
            for key, value in (row.summary_digest or {}).items()
            if isinstance(value, list)
        }
        code, error = _last_failure(session, request_id)
        return RequestView(
            request_id=row.id,
            project_key=str(row.project_key),
            raw_input=str(row.raw_input),
            status=str(row.status),
            split_source=None if row.split_source is None else str(row.split_source),
            model_routing=dict(row.model_routing) if isinstance(row.model_routing, dict) else None,
            summary_digest=digest,
            clarification_waived=bool(row.clarification_waived),
            created_at=row.created_at,
            last_error=error,
            last_error_code=code,
        )
    finally:
        session.close()


class DeliveryManifestEntryView(BaseModel):
    rel_path: str
    producer: str
    task_card_id: str
    bytes: int
    lines: int
    format: str


class DeliveryPackageView(BaseModel):
    """EN-17 交付包的读面：索引，不是文件内容本身。

    `manifest` 里的 `rel_path` 是总线内相对路径；文件正文留在各 `<agent>-workspace/`。
    实时看板此前只有 `studio.deliver`（推演档才有），已交付的单在「交付」页仍是占位文案；
    这个端点把 EN-17 的既成事实交出来，页面不再需要猜。
    """

    deliverable_id: uuid.UUID
    feature_request_id: uuid.UUID
    entry_path: str
    manifest: list[DeliveryManifestEntryView]
    open_issues: list[dict[str, Any]]
    assembled_by_agent_id: str
    created_at: dt.datetime


@router.get("/requests/{request_id}/delivery", response_model=DeliveryPackageView)
def get_delivery(request_id: uuid.UUID, request: Request) -> DeliveryPackageView:
    """读这一单的交付包（EN-17）。没有组包 ⇒ 404 `delivery_not_assembled`。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：交付读面需要注入 app.state.session_factory"
        )
    session = factory()
    try:
        if session.get(FeatureRequest, request_id) is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        row = (
            session.query(Deliverable)
            .filter(Deliverable.feature_request_id == request_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="delivery_not_assembled：这一单还没有交付包")
        manifest = [
            DeliveryManifestEntryView(
                rel_path=str(item.get("rel_path") or ""),
                producer=str(item.get("producer") or ""),
                task_card_id=str(item.get("task_card_id") or ""),
                bytes=int(item.get("bytes") or 0),
                lines=int(item.get("lines") or 0),
                format=str(item.get("format") or ""),
            )
            for item in (row.manifest or [])
            if isinstance(item, dict)
        ]
        return DeliveryPackageView(
            deliverable_id=row.id,
            feature_request_id=row.feature_request_id,
            entry_path=str(row.entry_path),
            manifest=manifest,
            open_issues=[item for item in (row.open_issues or []) if isinstance(item, dict)],
            assembled_by_agent_id=str(row.assembled_by_agent_id),
            created_at=row.created_at,
        )
    finally:
        session.close()


def _fingerprint(secret: SecretStr | None) -> str | None:
    """key → 短指纹（sha256 前 10 位）。**不是 key**：用它判"同模型不同 key"，落库只落它。"""
    if secret is None:
        return None
    value = secret.get_secret_value()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _routing_payload(body: ModelRoutingBody | None) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """请求体路由 → `(落库的 JSONB, 按席位的 key)`。key **只走第二个返回值**（进内存仓）。

    形状规则（422 点名缺哪一格）：`executor.kind=api` 要 vendor+model；把关席二选一
    （`agent_id` 或 vendor+model）；模型席位用自定义厂商时 `base_url` 必填
    （否则运行时不知道该打哪个端点，会在后台失败——那比入口 422 晚一个数量级）。
    """
    if body is None:
        return None, {}
    keys: dict[str, str] = {}
    stored = body.model_dump(exclude_none=True)
    for seat in ("executor", "reviewer", "acceptor"):
        value = stored.get(seat)
        if not isinstance(value, dict):
            continue
        secret = getattr(getattr(body, seat), "api_key", None) if seat != "executor" else body.executor.api_key if body.executor else None
        fp = _fingerprint(secret)
        if fp:
            value["key_fp"] = fp
            keys[seat] = secret.get_secret_value()  # type: ignore[union-attr]
        value.pop("api_key", None)
        if seat == "executor" and value.get("kind") == "qoder":
            continue
        agent_id = str(value.get("agent_id") or "").strip()
        vendor = str(value.get("vendor") or "").strip()
        model = str(value.get("model") or "").strip()
        if agent_id and (vendor or model):
            raise HTTPException(
                status_code=422,
                detail=f"model_routing.{seat} 同时给了 agent_id 与 vendor/model ⇒ 只能二选一",
            )
        if not agent_id and not (vendor and model):
            raise HTTPException(
                status_code=422,
                detail=f"model_routing.{seat} 缺 agent_id，或 vendor+model 不全",
            )
        if vendor and model and not str(value.get("base_url") or "").strip():
            if vendor not in settings_run.VENDOR_IDS:
                raise HTTPException(
                    status_code=422,
                    detail=f"model_routing.{seat}.vendor={vendor!r} 不是内置厂商 ⇒ 必须给 base_url",
                )
        # 落库只留真值：空串是"这一格没给"，落进 JSONB 会让 `GET /requests` 的回读多一堆噪声；
        # agent 形态的席位不存 thinking（那是模型席位才用的字段）。
        if agent_id:
            value.pop("thinking", None)
        stored[seat] = {k: v for k, v in value.items() if v != ""}
    return stored, keys


@router.post("/requests", status_code=201, response_model=IntakeResponse)
def create_request(payload: IntakeRequest, request: Request, background: BackgroundTasks) -> IntakeResponse:
    completer = _completer(request)
    try:
        outcome = plan_requirement(payload.raw_input, completer=completer)
    except BlankRequirement as exc:
        raise HTTPException(status_code=422, detail="raw_input 不能是空白") from exc

    routing_payload, routing_keys = _routing_payload(payload.model_routing)
    request_id = uuid.uuid4()
    row = intake.new_request(
        request_id=request_id,
        project_key=payload.project_key,
        raw_input=payload.raw_input,
        summary_digest=outcome.summary_digest,
        clarification_answers=payload.clarification_answers,
        model_routing=routing_payload,
    )
    stored = _persist(request, row)
    # 凭据只进进程内存（`routing_vault`），落库的那份路由里只有 key_fp。
    routing_vault.remember(stored.request_id, routing_keys)
    # 受理回执先回，拆解在后台跑：网页不需要再跑一遍 CLI 才看得到任务卡。
    # 装配面没注跑者（判据里的裸 app）就什么都不排 —— 201 只代表"那一行落了库"，
    # 页面上"正在拆解"这句话只在真注入了 `intake_runner` 时才有可能成真。
    runner = getattr(request.app.state, "intake_runner", None)
    if runner is not None:
        background.add_task(runner, feature_request_id=stored.request_id)
    return IntakeResponse(
        request_id=stored.request_id,
        project_key=stored.project_key,
        status=stored.status,
        summary_digest=outcome.summary_digest,
        clarification_waived=stored.clarification_waived,
        completer_id=_completer_id(completer),
        intake_scheduled=runner is not None,
    )


class RetryResponse(BaseModel):
    """重试回执。只说"排上了"，不说"成功了" —— 结果要靠读面去看。"""

    request_id: uuid.UUID
    status: str
    intake_scheduled: bool


@router.post("/requests/{request_id}/retry", status_code=202, response_model=RetryResponse)
def retry_request(request_id: uuid.UUID, request: Request, background: BackgroundTasks) -> RetryResponse:
    """把一条 `FAILED`（或已停住的 `INTAKE`）需求单重新排进受理（U6，2026-10-28；P1-7 2026-10-29）。

    为什么需要它：受理失败之后需求单原来是"永远停在 `INTAKE`"，既看不出失败、也没有第二次机会 ——
    唯一的出路是重新提一遍需求，而那会换一个新 id，之前那场的会议台词与文档全部丢掉。
    重试把**同一条单**推回 `INTAKE` 再跑一遍受理链，现场是同一个。

    `INTAKE` 只在**没有跑者持锁、且一张卡都没拆出来**时才接：前者是"正在跑"（重排会让两个跑者
    抢同一张单），后者说明拆单已经落过卡（重跑会撞 `AlreadyAdvanced` 或拆出第二套卡）。
    两种情况各回各的话，不让调用方去猜"为什么没反应"。
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：重试需要注入 app.state.session_factory"
        )
    runner = getattr(request.app.state, "intake_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="intake_runner_missing：没有注入受理跑者 ⇒ 排不出这一次重试",
        )
    session = factory()
    try:
        row = session.get(FeatureRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        current = str(row.status)
        if current == "INTAKE":
            is_running = getattr(runner, "is_running", None)
            if callable(is_running) and is_running(request_id):
                raise HTTPException(
                    status_code=409,
                    detail="retry_running：这一单的受理跑者还在跑，重排会让两个跑者抢同一张单",
                )
            has_cards = (
                session.query(TaskCard)
                .filter(TaskCard.feature_request_id == request_id)
                .first()
                is not None
            )
            if has_cards:
                raise HTTPException(
                    status_code=409,
                    detail=f"not_failed：当前状态是 {current}，且已经拆出任务卡 ⇒ 重跑会拆出第二套卡",
                )
        elif current != "FAILED":
            raise HTTPException(
                status_code=409,
                detail=f"not_failed：当前状态是 {current}，只有 FAILED 或已停住的 INTAKE 可以重试",
            )
        row.status = "INTAKE"
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    background.add_task(runner, feature_request_id=request_id)
    return RetryResponse(request_id=request_id, status="INTAKE", intake_scheduled=True)


class ContinueResponse(BaseModel):
    """继续执行的回执。只说"排上了"，跑成什么样要去读卡与留痕。"""

    request_id: uuid.UUID
    status: str
    resume_scheduled: bool


@router.post("/requests/{request_id}/continue", status_code=202, response_model=ContinueResponse)
def continue_request(request_id: uuid.UUID, request: Request, background: BackgroundTasks) -> ContinueResponse:
    """把这一单里**还没跑完的卡**接着跑（2026-10-28）。

    为什么需要这个入口：续跑此前只由「批准」那一下触发（`api/approvals.py::_resume`）。而批单只有一次 ——
    工单一旦 `APPROVED`，它就从 `?state=PENDING` 里消失，待办条里也就没有它了。于是中途一张卡失败之后
    （现场：`real_demo / 7800b3dd` 的 T05 写了 `submit-contact.php` 被总线拒），**人手上一个能点的东西都没有**：
    批单已经批过了，评审没有可评的卡，待办是空的。这一格补的就是那个"再推一次"。

    四道门各有各的话，不让调用方去猜"为什么没反应"：
    - 没有这条单 ⇒ 404；
    - 还有 `PENDING` 的 A 类单 ⇒ 409 `pending_approval`（该批的还没批，先批，别绕过门禁）；
    - 状态不是 `SPLIT` / `RUNNING` ⇒ 409 `not_runnable`（`INTAKE` 在受理、`DELIVERED` 已交付、`FAILED` 该走 retry）；
    - 装配方没注 resumer ⇒ 503（"排上了"这句话不能凭空说）。

    续跑本身走 `background.add_task`，与批单那一侧同形：它会一直跑到厂商返回，内联调就是让页面点一下转几分钟。
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：继续执行需要注入 app.state.session_factory"
        )
    resumer = getattr(request.app.state, "graph_resumer", None)
    if resumer is None:
        raise HTTPException(
            status_code=503,
            detail="graph_resumer_missing：没有注入续跑者 ⇒ 排不出这一次继续执行",
        )
    session = factory()
    try:
        row = session.get(FeatureRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        status = str(row.status)
        pending = (
            session.query(ApprovalTicket)
            .filter(
                ApprovalTicket.feature_request_id == request_id,
                ApprovalTicket.state == "PENDING",
            )
            .count()
        )
        if pending:
            raise HTTPException(
                status_code=409,
                detail=f"pending_approval：这一单还有 {pending} 张待批的工单，先在待办里批完再继续",
            )
        if status not in ("SPLIT", "RUNNING"):
            raise HTTPException(
                status_code=409,
                detail=f"not_runnable：当前状态是 {status}，只有 SPLIT / RUNNING 的需求单可以继续执行",
            )
    finally:
        session.close()
    # 谁按的这一下要带给续跑者：只有它允许在开跑前把停在 `RUNNING` 的孤儿卡收回 `READY`
    # （`realrun.reclaim_orphaned_cards`）。缺头时不拦 —— 这一发不改卡态，只把能跑的卡接着跑，
    # 与「批准」那一发同一条口径；带上就多一层"有人认领"的留痕主语。
    principal = (request.headers.get(PRINCIPAL_HEADER) or "").strip() or None
    background.add_task(resumer, feature_request_id=request_id, conclusion="continue", principal=principal)
    return ContinueResponse(request_id=request_id, status=status, resume_scheduled=True)


class ExtendBody(BaseModel):
    """追加需求的请求体：只有一句话。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="这一次要追加的需求（一句话即可）")


#: 「这一趟还在跑」那一发的原话。**写死成常量**：它同时是 409 的 detail 与判据要找的那句话，
#: 两处各写一份的话，改了文案判据就静默失效（它断的是"说了什么"，不是"状态码是几"）。
BUSY_DETAIL = (
    "intake_running：这一单此刻还在跑（开会 / 出文档 / 执行任务卡），等它跑完再追加。"
    "追加要重开一场会并重出 PRD，和正在跑的那一趟会抢同一份 digest"
)


def runner_busy(runner: object, request_id: uuid.UUID) -> bool:
    """问受理跑者：这一单此刻有人在跑吗？

    为什么必须问**跑者**而不是读 `status`（2026-10-28 第十轮真链路踩到的那个坑）：
    `status` 只在受理段是 `INTAKE`，而锁在**执行段**也held着（续跑者 `resumer` 是在
    `_run` 里同步跑的，跑完 14 张卡才松手）。于是"执行中点增加需求"这一发：
    ① `status` 不是 `INTAKE` ⇒ 旧判断放行；② digest 改完（追加写了、会议清了、文档挪进上一版）；
    ③ `__call__` 那边 `acquire(blocking=False)` 拿到 `False`，留痕写 `split_skipped/already_running`
    就返回 —— 会议一次都没开。屏上从此停在「会议无产出」，而库里的 digest 说"正在重开一场会"。

    所以先问、先拒，把"改了一半"挡在门外。跑者没有 `is_running`（判据里的记账替身）时
    退回旧口径 —— 不因为问不出来就一律拒。
    """
    probe = getattr(runner, "is_running", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(request_id))
    except Exception:  # noqa: BLE001 - 探测失败不该把一发追加变成 500
        return False


class ExtendResponse(BaseModel):
    """追加需求的回执。`cards` 是**已有的卡数** —— 前端据此说清"卡不会重拆"。"""

    request_id: uuid.UUID
    status: str
    extend_scheduled: bool
    cards: int


@router.post("/requests/{request_id}/extend", status_code=202, response_model=ExtendResponse)
def extend_request(
    request_id: uuid.UUID,
    payload: ExtendBody,
    request: Request,
    background: BackgroundTasks,
) -> ExtendResponse:
    """在**已产生的 PRD 基础上**追加一条需求，四岗位带着这一版 PRD 重开一次会（2026-10-28 owner 第 2 条）。

    与「修改需求」的区别：那个是**离开这一单**、拿原文回受理台重开一单；这个是**在这一单上**追加，
    旧 PRD 作为修订底稿，会议与文档重跑一遍。

    做三件事，都不动已有卡：
    1. 追加的话写进 `summary_digest["追加"]`（一行一条，带时刻）。**`raw_input` 一个字不改** ——
       它是"客户最初说的那句话"，改掉就查不回这一单是怎么来的了；
    2. 清空 `summary_digest["会议"]`、把三份文档挪进 `上一版`：不清台词的话屏上会立刻显示上一场的，
       `stageOf()` 见到"轮数 × 4"条台词就判"会议已结束"，新的讨论反而看不见；
       不动文档的话前端 `parseDocs` 会读到**上一版 PRD**，屏上把旧版当这一版，
       而且 `stageOf()` 一见到文档就判"正在拆卡"—— 真正在发生的是会议重开。
       挪走而不是删掉：那三份正是这一趟的修订底稿（`serve_intake._effective_input` 要读 PRD）；
    3. 排一次受理（`background.add_task`）。受理链跑完文档那一步时发现库里已经有卡，
       就**只刷新 PRD、不重拆**（见 `serve_intake._run` 里那段分叉）。

    为什么不改 `status`：这一趟不推进状态机（不拆单、不交付），把 `DELIVERED` 改成 `INTAKE`
    再改回来，中间那几秒屏上会变成"正在拆解"，而实际上卡一张都没动。

    **这一趟还有人在跑就不许追加**（409，见 `BUSY_DETAIL` / `runner_busy`）：那说明另一趟正在
    写同一份 digest，两条链同时改一个 JSONB 字段，谁覆盖谁全靠运气。判据是**跑者手里的锁**，
    不是 `status` —— 锁在执行段也held着，而 `status` 那时候早就不是 `INTAKE` 了。
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：追加需求需要注入 app.state.session_factory"
        )
    runner = getattr(request.app.state, "intake_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="intake_runner_missing：没有注入受理跑者 ⇒ 排不出这一次追加",
        )
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty_text：追加的需求不能是空白")

    # 这一趟有没有人在跑 —— **改 digest 之前**就要问清（见 `runner_busy` 那段说明）。
    if runner_busy(runner, request_id):
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)

    session = factory()
    try:
        row = session.get(FeatureRequest, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        status = str(row.status)
        # 这里**不再**单看 `status == "INTAKE"`：那一格只在受理段成立，而锁在执行段也held着。
        # 忙不忙已经由上面那道 `runner_busy` 统一判过（见 `BUSY_DETAIL` 的说明）——
        # 留着这条会让"受理跑挂了、状态卡在 INTAKE、锁已松"的单永远追加不了。
        cards = (
            session.query(TaskCard)
            .filter(TaskCard.feature_request_id == request_id)
            .count()
        )
        digest = extend_digest(row.summary_digest, text, stamp=f"{dt.datetime.now():%H:%M}")
        row.summary_digest = digest
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()

    background.add_task(runner, feature_request_id=request_id)
    return ExtendResponse(request_id=request_id, status=status, extend_scheduled=True, cards=int(cards))


class EvidenceArtifactView(BaseModel):
    """文件总线上的一行 = **一个文件**（不是一次写入）。

    `rel_path` 是产物在项目目录里的相对路径，前端只取末段当文件名显示。
    `validated` 三态压成布尔：`None`（校验节点还没跑）与 `invalid` 都是"不能算已验证"，
    但它们在屏上是两件事 —— 所以另给 `state` 原样透出，不让前端猜。
    """

    rel_path: str
    task_card_id: str
    writer_agent_id: str
    format: str
    bytes: int
    lines: int
    state: str


class EvidenceFindingView(BaseModel):
    """一条判据的判定结果（`review_verdict.item_findings` 的一行）。"""

    criterion: str
    met: bool
    evidence: str


class EvidenceVerdictView(BaseModel):
    """一张卡的审查结论。`findings` 是逐条判据，前端拿它填「门禁比对」那张表。"""

    task_card_id: str
    reviewer_agent_id: str
    verdict: str
    findings: list[EvidenceFindingView]


class EvidenceView(BaseModel):
    """右栏两张"总是空的"面板的读面（2026-10-29）。

    为什么单独一个端点而不是塞进 `GET /requests/{id}`：那一格在受理期间每 1.5 秒被轮询一次，
    而这两张表只在卡落库之后才有内容 —— 塞进去等于让"开会"那三分钟每次都多查两张表。
    """

    request_id: uuid.UUID
    artifacts: list[EvidenceArtifactView]
    verdicts: list[EvidenceVerdictView]


def latest_per_path(rows: Iterable[Artifact]) -> list[Artifact]:
    """一个文件一行：同一 `rel_path` 被几张卡先后写过时，**后写的覆盖先写的**。

    为什么这条规则要单独拎出来当一个具名函数：它是**产品规则**，不是这一屏的排版偏好。
    流水线上每张卡各写自己那一段、最后合成一个文件，所以库里同一个 `about.html` 会有五行；
    照原样铺到「文件总线」上，读起来就是"产出了五份 about.html"。这条规则错了不会报错，
    只会让屏上的产物计数虚高 —— 正是需要有判据盯住的那类东西。

    **入参必须已按 `created_at` 升序**（端点用 `order_by` 保证）。这里不再排一次：
    函数不该猜"哪一次写得晚"，排序是调用方对"时间"这件事的唯一主张。
    """
    latest: dict[str, Artifact] = {}
    for row in rows:
        latest[str(row.rel_path)] = row
    return sorted(latest.values(), key=lambda item: str(item.rel_path))


@router.get("/requests/{request_id}/evidence", response_model=EvidenceView)
def request_evidence(request_id: uuid.UUID, request: Request) -> EvidenceView:
    """这一单的**产物清单**与**审查结论**（owner 2026-10-29：「任务结束了还是空的」）。

    缺陷的形状：右栏「文件总线」与「审查」两页只读推演档的 `artifacts` / `reviews`，
    而真链路从来没往那两格写过东西 —— 于是 14 张卡全部 `DELIVERED`、交付包都生成了，
    这两页仍然是"文件总线还是空的"/"门禁还没开始"。

    读的两张表都是**既成事实**，不做任何推断：

    - `artifact`（EN-06）：一个文件一行。同一 `rel_path` 被多张卡先后写过（流水线上每张卡
      各写自己那一段，最后合成一个文件），按 `created_at` 取**最后一次**写的那个 ——
      文件总线上"一行一个文件"是产品规则，不是这一屏的排版偏好；
    - `review_verdict`（EN-08）：一张卡一条结论，`item_findings` 里是逐条判据的 `met` 与证据。
      `ACP_REVIEW_MODE=auto` 下这些结论由门禁代签（证据那行会写明），但**它们是真的**，
      所以照原样上屏 —— 不能因为"不是人签的"就把整页留空。

    空是合法结果（还没派单的单就是空的），但它必须与"读面根本没接"区分开：
    现在空只会出现在真的还没有卡的时候。
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：证据面需要注入 app.state.session_factory"
        )
    session = factory()
    try:
        if session.get(FeatureRequest, request_id) is None:
            raise HTTPException(status_code=404, detail=f"request_not_found：没有需求单 {request_id}")
        rows = (
            session.query(Artifact)
            .filter(Artifact.feature_request_id == request_id)
            .order_by(Artifact.created_at)
            .all()
        )
        artifacts = [
            EvidenceArtifactView(
                rel_path=str(row.rel_path),
                task_card_id=str(row.task_card_id),
                writer_agent_id=str(row.writer_agent_id),
                format=str(row.format),
                bytes=int(row.bytes or 0),
                lines=int(row.lines or 0),
                state=str(row.validated_status or "pending"),
            )
            for row in latest_per_path(rows)
        ]

        verdicts = []
        for row in (
            session.query(ReviewVerdict)
            .filter(ReviewVerdict.feature_request_id == request_id)
            .order_by(ReviewVerdict.task_card_id)
            .all()
        ):
            findings = []
            for item in row.item_findings or []:
                if not isinstance(item, dict):
                    continue
                findings.append(
                    EvidenceFindingView(
                        criterion=str(item.get("criterion") or ""),
                        met=bool(item.get("met")),
                        evidence=str(item.get("evidence") or ""),
                    )
                )
            verdicts.append(
                EvidenceVerdictView(
                    task_card_id=str(row.task_card_id),
                    reviewer_agent_id=str(row.reviewer_agent_id),
                    verdict=str(row.verdict),
                    findings=findings,
                )
            )
    finally:
        session.close()

    return EvidenceView(request_id=request_id, artifacts=artifacts, verdicts=verdicts)


def _last_failure(session, request_id: uuid.UUID) -> tuple[str | None, str | None]:
    """这条需求单最近一次受理失败 `(归因码, 原因)`。没失败过返回 `(None, None)`。

    读的是留痕而不是新列（理由见 `RequestView.last_error`）。**在 Python 里筛而不是在 SQL 里
    筛 JSONB**：`payload["action"]` 的 `astext` 只在 PG 上成立，而这个读面也被判据拿 SQLite 跑过；
    一张单的留痕本来就只有几十行，翻 80 行比多一条方言依赖便宜。
    """
    rows = (
        session.query(EventLog)
        .filter(EventLog.entity == "feature_request", EventLog.entity_id == str(request_id))
        .order_by(EventLog.created_at.desc())
        .limit(_ERROR_SCAN)
        .all()
    )
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if str(payload.get("action") or "") in ("split_failed", "intake_failed"):
            code = str(payload.get("code") or "").strip() or None
            error = str(payload.get("error") or "").strip() or None
            return code, error
    return None, None


class SettingsSeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor: str
    model: str
    #: 设置页那一列「思考强度」。2026-10-30 之前前端不发、后端不收 ⇒ 页面上写的档位是装饰；
    #: 现在它一路传到 `settings_run._thinking_fields`，按各家实测有效的字段发出去。
    thinking: str = "medium"


class SettingsRunBody(BaseModel):
    """设置页四席直跑的请求体。**密钥只在这次请求的内存里**，不落库、不回显。"""

    model_config = ConfigDict(extra="forbid")

    project_key: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    seats: dict[str, SettingsSeatBody]
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    openai_compat_base_url: str = ""


@router.post("/requests/settings-run", status_code=202)
def settings_run_request(body: SettingsRunBody) -> dict[str, Any]:
    """按设置页四席直跑一条需求；立即返回 run_id，执行在后台线程。"""
    if set(body.seats) != set(settings_run.SEAT_NAMES):
        raise HTTPException(
            status_code=422,
            detail=f"settings_run:seats_incomplete：需要 {list(settings_run.SEAT_NAMES)}",
        )
    seats: dict[str, settings_run.SeatModel] = {}
    try:
        for name in settings_run.SEAT_NAMES:
            selected = body.seats[name]
            vendor_id = settings_run.VENDOR_IDS.get(selected.vendor)
            if vendor_id is None:
                raise HTTPException(status_code=422, detail=f"settings_run:unknown_vendor：{selected.vendor!r}")
            secret = body.api_keys.get(vendor_id)
            api_key = secret.get_secret_value() if secret is not None else ""
            seats[name] = settings_run.resolve_seat(
                vendor=selected.vendor,
                model=selected.model,
                api_key=api_key,
                openai_compat_base_url=body.openai_compat_base_url,
                thinking=selected.thinking,
            )
        run_id = settings_run.start_run(
            project_key=body.project_key,
            requirement=body.requirement,
            seats=seats,
            output_root=REPO_ROOT / "results",
        )
        return {"run_id": run_id, "status": "running"}
    except settings_run.SettingsRunError as exc:
        status = 422 if exc.stage == "config" else 502
        raise HTTPException(status_code=status, detail=f"{exc.stage}:{exc.code}: {exc.detail}") from None


@router.get("/requests/settings-run/{run_id}")
def get_settings_run(run_id: str) -> dict[str, Any]:
    """读一次后台直跑的状态；完成后带 `report`。"""
    job = settings_run.get_run(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"settings_run_not_found：{run_id}")
    return job


@router.post("/requests/settings-run/{run_id}/cancel")
def cancel_settings_run(run_id: str) -> dict[str, Any]:
    """请求取消；当前模型调用结束后工作线程在下个检查点退出。"""
    job = settings_run.cancel_run(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"settings_run_not_found：{run_id}")
    return job

