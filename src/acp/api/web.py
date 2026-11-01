"""M2-001 / M2-002：看板面 —— 活动流分页读 + 同进程 SSE 推流 + 运行时参数读写（FR-049 / FR-050｜R-13）。

这一支 router 只做两件事：**看得见**（活动流）与**改得动那几个数**（运行时参数）。
它刻意不碰任务状态与审批结论 —— 那些各有自己的唯一入口（`api/tasks.py` / `api/approvals.py`）。
把写侧在这里再开一遍就会出现两条能改同一张单的路径，而 FR-042 那句"看板与 CLI 打的是同一个端点"
的判据就失去了唯一被测对象。

三处刻意的写法：

1. **SSE 的数据源是权威表，不是进程内广播**。流里每一帧都是 `persistence/events.read_since()`
   从 EN-14 现读的。进程内广播能做到"零轮询"，但它只能广播**本进程写下的**事件 —— 而原则 V 要的
   是"CLI 与看板双通道操作同一张单"，CLI 是另一个进程：它写进去的留痕在广播里根本不存在，
   看板就会"不刷新而不变"，FR-050 反过来变成假绿。读权威表则两个进程的写入一样看得见，
   Redis 那条豁免（`plan.md` 的 Constitution Check）也因此仍然成立 —— 这里没有引入跨进程的
   pub/sub 消费方，只有一个按间隔读 PG 的循环。
2. **推流按 `id` 游标推进，不按时刻**。口径与 `read_since()` 一致（本表除 PK 外无二级索引，
   自增主键才是插入序）。浏览器 `EventSource` 断线重连时自带 `Last-Event-ID` 头，这里接住它当游标，
   所以"重连不重不漏"这件事不需要前端再写一份游标。
3. **合并等待有上界，而且它不在这里写死**。两次读库之间的间隔取 `config/runtime.yaml` 的
   `fragment_merge_ms`（MVP-2 第三步 ② 的那个面板项），它就是 SC-013 那个"看到变化的延迟"在本实现里的
   上界；"不大于 spec 里那个数"这条由 `tests/guards/test_board_stream.py` 现读 `spec.md` 与现跑加载器来比。
   本模块因此**没有**那个毫秒数的第二份副本 —— 面板改了值而流还是老的间隔，是这里最容易出现的假动作。
"""

from __future__ import annotations

import ast
import json
import time
from collections.abc import Iterator
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from acp.api.approvals import PRINCIPAL_HEADER
from acp.config import runtime
from acp.persistence import events

router = APIRouter()

#: AST 扫描的根（口径同 `tests/guards/test_assembly.py::SRC_ROOT`）：数"谁真的读了这个键"要看生产树。
_SRC_ROOT = Path(__file__).resolve().parents[1]

#: 一活动流分页的两档界限：默认页长与上限。上限的存在是为了让 `?limit=` 写错不至于全表扫。
PAGE_DEFAULT = 50
PAGE_MAX = 200

#: 活动流一帧的事件名。前端与判据都按它筛，改名要两边同笔。
STREAM_EVENT = "acp-activity"

#: 帧合并等待（毫秒）**没有常量**：它来自 `config/runtime.yaml` 的 `fragment_merge_ms`。
#: 这里留一个默认值就等于允许"面板改了而流没改"，而那正是本面板要消灭的假动作。

#: 单次请求最多推这么多帧；`None` = 推到客户端断开为止（浏览器 `EventSource` 的正常形态）。
STREAM_FRAMES_MAX = 500


class ActivityRow(BaseModel):
    """EN-14 的一行在 HTTP 面上的形状（字段名 = 列名，不做第二层重命名）。"""

    id: int
    entity: str
    entity_id: str
    kind: str
    actor_agent_id: str
    payload: dict[str, Any]
    created_at: str


class ActivityPage(BaseModel):
    """一页活动流 + 下一页的游标。

    `next_cursor` 回的是**本页最后一行的 id**，不是"页码"：页码在插入序前进时会漂，
    而按 id 的游标让"翻下一页"与"续流"是同一件事。
    """

    rows: list[ActivityRow]
    next_cursor: int
    has_more: bool


def _session(request: Request):
    """取会话工厂并调一次。缺注入 ⇒ 503 并点名键，口径同 `api/approvals.py::_session`。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：活动流的读需要注入 app.state.session_factory"
        )
    return factory()


def _row(activity: events.Activity) -> ActivityRow:
    return ActivityRow(
        id=activity.id,
        entity=activity.entity,
        entity_id=activity.entity_id,
        kind=activity.kind,
        actor_agent_id=activity.actor_agent_id,
        payload=activity.payload,
        created_at=activity.created_at.isoformat(),
    )


def _checked_limit(limit: int) -> int:
    if not 1 <= limit <= PAGE_MAX:
        raise HTTPException(status_code=422, detail=f"limit 只接受 1..{PAGE_MAX}（收到 {limit}）")
    return limit


def _frame(activity: events.Activity) -> str:
    """一帧 SSE 报文：`id:` 给浏览器当重连游标，`data:` 是整行的 JSON。

    JSON 用 `ensure_ascii=False`：留痕正文多为中文，转义成 `\\uXXXX` 会让"看板上看到的"与
    "库里那行"两种形状，判据要比就得先解码 —— 少一层换算少一处能错的地方。
    """
    body = json.dumps(_row(activity).model_dump(), ensure_ascii=False, default=str)
    return f"id: {activity.id}\nevent: {STREAM_EVENT}\ndata: {body}\n\n"


class RuntimeView(BaseModel):
    """面板读数：当前值、来源指纹，以及**这个键今天到底有没有活消费者**。

    `consumers` 不是注释也不是我口述的结论，是从生产树里 AST 数出来的：只数"调了
    `load_runtime_params()` 又在同一个函数里取了这个键"的模块。所以它能说真话 ——
    旧读数（2026-10-19 笔 E~G）里 `max_tokens` 这一栏是空的，那就是"改了没人读"；
    笔 H 之后它指着 `adapters/spec_factory.py`，说的是"构造厂商报文时在这里被取走"。
    **两格都不等于"真跑已经在用它"**：真跑那一发的构造点还没接上（待裁 W-8）。
    """

    params: dict[str, Any]
    source_sha256: str
    consumers: dict[str, list[str]]


class RuntimeWrite(BaseModel):
    """面板保存的请求体：**只有那两个数字键**，多一个都不收（`extra="forbid"`）。

    2026-10-28 起 `RUNTIME_KEYS` 多了一格 `auto_accept_low_risk`，但它**不在这里** ——
    "谁可以代签"有自己的端点（`PATCH /autonomy/auto-approval`），那条要身份头、要留痕。
    所以这里保存时用 `dataclass_replace` 把没提到的那一格**原样带过去**，不是重抄一份。
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = None
    fragment_merge_ms: int = Field(ge=runtime.MIN_FRAGMENT_MERGE_MS)


def _runtime_view(params: runtime.RuntimeParams) -> RuntimeView:
    return RuntimeView(
        params=params.as_dict(),
        source_sha256=params.source_sha256,
        consumers={key: _runtime_consumers(key) for key in runtime.RUNTIME_KEYS},
    )


def _runtime_consumers(key: str) -> list[str]:
    """数出"真的读了这个键"的生产模块（相对 POSIX 路径，升序）。

    判据口径：必须是 `load_runtime_params()` 的**调用方**，且在同一个函数体里出现
    `.<key>` 这一次属性取用。只 grep 键名会把定义处、注释与文档都算成消费者 —— 那正是
    "参数写了但没生效"能被读成"已经生效"的那条路。
    """
    found: set[str] = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.name == "runtime.py" and path.parent.name == "config":
            continue  # 定义处不算消费者
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            loads = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "load_runtime_params"
                for inner in ast.walk(node)
            )
            reads = any(
                isinstance(inner, ast.Attribute) and inner.attr == key for inner in ast.walk(node)
            )
            if loads and reads:
                found.add(path.relative_to(_SRC_ROOT).as_posix())
    return sorted(found)


@router.get("/runtime", response_model=RuntimeView)
def read_runtime() -> RuntimeView:
    """面板打开时读这一条：当前生效值 + 来源指纹 + 每个键的活消费者。"""
    return _runtime_view(_current_params())


def _current_params() -> runtime.RuntimeParams:
    """读当前生效的那一份参数。读不出就 503（不回落默认）。

    **单独收成一个函数是有原因的**，不是为复用：`_runtime_consumers` 的口径是"同一个函数里既调
    `load_runtime_params()` 又取某个键"。`write_runtime` 取的是**请求体**上的 `body.max_tokens`
    —— 和参数文件里的 `max_tokens` 同名但完全不是一回事，扫描器分不出这两者。
    把"读盘"挪进这个**不取任何键**的小函数，那个读数就还是"谁真的读了参数"。
    """
    try:
        return runtime.load_runtime_params()
    except runtime.RuntimeParamsError as exc:
        raise HTTPException(status_code=503, detail=f"runtime_params_unavailable：{exc}") from None


@router.put("/runtime", response_model=RuntimeView)
def write_runtime(body: RuntimeWrite, request: Request) -> RuntimeView:
    """面板保存。写的是 `config/runtime.yaml` 那一份，写完**回读校验**才回 200。

    与档案面同一条规则：必须带 `x-acp-principal`（403 挡住披错身份，挡不住冒充 —— 本仓无鉴权，
    这条限制在 `api/approvals.py` 模块头就写着，不在这里重复一遍理由）。
    """
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        raise HTTPException(status_code=403, detail=f"principal_not_allowed：请求头 {PRINCIPAL_HEADER} 缺失")
    current = _current_params()
    # 只改这两个键，别的（`auto_accept_low_risk`）**原样带过去**：逐键重抄会在加第四个键那天
    # 悄悄把面板上那个"谁可以代签"的开关清回默认，而且不报错。
    params = dataclass_replace(
        current,
        max_tokens=body.max_tokens,
        fragment_merge_ms=body.fragment_merge_ms,
        source_sha256="",
    )
    try:
        written = runtime.write_runtime_params(params)
    except runtime.RuntimeParamsError as exc:
        raise HTTPException(status_code=422, detail=f"runtime_params_rejected：{exc}") from None
    return _runtime_view(runtime.RuntimeParams(**params.as_dict(), source_sha256=written))


@router.get("/events", response_model=ActivityPage)
def list_events(
    request: Request,
    cursor: int = 0,
    limit: int = PAGE_DEFAULT,
    entity: str | None = None,
    entity_id: str | None = None,
) -> ActivityPage:
    """读 `id > cursor` 的活动流一页（FR-049 的"活动流"与 FR-051 的"按任务查"共用这一条）。"""
    if cursor < 0:
        raise HTTPException(status_code=422, detail=f"cursor 不许为负（收到 {cursor}）")
    _checked_limit(limit)
    session = _session(request)
    try:
        # `limit + 1` 读、只回 `limit` 行：`has_more` 于是来自"确实还有一行"，而不是靠行数凑一个猜测。
        fetched = events.read_since(
            session,
            after_id=cursor,
            limit=limit + 1,
            entity=entity,
            entity_id=entity_id,
        )
    except events.EventMisuse as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    finally:
        # 2026-10-27：这一支过去只借不还 —— 每读一页活动流泄漏一个池连接，
        # 刷几次面板就把连接池抽干。读面没有要提交的东西，`close()` 就是全部收尾。
        session.close()
    has_more = len(fetched) > limit
    page = fetched[:limit]
    return ActivityPage(
        rows=[_row(activity) for activity in page],
        next_cursor=page[-1].id if page else cursor,
        has_more=has_more,
    )


@router.get("/events/stream")
def stream_events(
    request: Request,
    after: int | None = None,
    limit: int | None = None,
    poll_ms: int | None = None,
) -> StreamingResponse:
    """同进程 SSE：按游标续读 EN-14，每帧一行，直到推满 `limit` 或客户端断开（FR-050）。

    `after` 不给时用请求头 `Last-Event-ID`（浏览器重连自动带上），再不给就从 0 起。
    `limit` 数的是**数据帧**：给了 limit 而库里还没那么多行时，这一条流会一边发 keepalive 一边等，
    等够才结束 —— 浏览器侧本来就不给 limit（它要的是"一直看着"），给 limit 的是判据与排障脚本。
    503 在这里、不在生成器里抛：流一旦开始，HTTP 状态码就已经发出去了，那时才发现没装配
    就只能把一条 200 的空流留给客户端。
    """
    if poll_ms is None:
        # 没显式给间隔 ⇒ 取面板那一份（`config/runtime.yaml`）。读不出来就 503 并点名原因，
        # **不许"当成默认值继续"**：那会让"参数文件坏了"表现成"看板好好的但间隔不是我设的"。
        try:
            poll_ms = runtime.load_runtime_params().fragment_merge_ms
        except runtime.RuntimeParamsError as exc:
            raise HTTPException(status_code=503, detail=f"runtime_params_unavailable：{exc}") from None
    elif poll_ms < 1:
        raise HTTPException(status_code=422, detail=f"poll_ms 必须 ≥ 1（收到 {poll_ms}）")
    if limit is not None and not 1 <= limit <= STREAM_FRAMES_MAX:
        raise HTTPException(
            status_code=422, detail=f"limit 只接受 1..{STREAM_FRAMES_MAX}（收到 {limit}）"
        )
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：SSE 推流需要注入 app.state.session_factory"
        )
    resume = request.headers.get("last-event-id")
    start = after if after is not None else (int(resume) if (resume or "").isdigit() else 0)
    if start < 0:
        raise HTTPException(status_code=422, detail=f"after 不许为负（收到 {start}）")
    return StreamingResponse(
        _tail(factory, start=start, stop_after=limit, interval=poll_ms / 1000),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _tail(factory: Any, *, start: int, stop_after: int | None, interval: float) -> Iterator[str]:
    """推流循环。每轮**自己开、自己关**一个会话：这条流可以活很久，
    借请求级的那个"不关"的会话就会把连接池占死（`api/approvals.py` 的一次性读没有这个问题）。
    """
    cursor = start
    sent = 0
    while stop_after is None or sent < stop_after:
        session = factory()
        try:
            fetched = events.read_since(session, after_id=cursor, limit=PAGE_MAX)
        finally:
            session.close()
        if not fetched:
            # 空闲也要发一帧注释：它让"连接还活着"这件事在浏览器侧看得见，也是判据能断言的形态。
            yield ": keepalive\n\n"
        for activity in fetched:
            cursor = activity.id
            yield _frame(activity)
            sent += 1
            if stop_after is not None and sent >= stop_after:
                return
        time.sleep(interval)
