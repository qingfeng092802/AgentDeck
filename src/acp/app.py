"""T123：HTTP 面的装配点（`api/` 那六支 router 到 `app.state` 之间的唯一接线处）。

`api/__init__.py` 的模块头从 Phase 2 起就把这一格空着写"装配归 T123"，本文件就是那一块。
MVP-2 起这里还多管一件事：`static_dir` 给了就把看板产物挂到 `/`（挂载点唯一 = 本函数最后那一步，
`include_router` 循环之后 ⇒ 公开 API 路径永远优先于静态兜底）。

三条刻意的写法，都在下面那批判据里各钉了一条：

1. **`internal_router` 不在挂载面上，而且没有参数能把它挂进来**。
   `contracts/approval-api.md §1` 末行是"由编排图内部建单（外部不直调）"，
   `api/approvals.py` 的模块头把这条做成了结构（建单挂在一个专门的 router 上）。
   只要这里给出一个 `expose_internal=True` 之类的开关，那条"外部不直调"就从**结构**降级成**默认值** ——
   而默认值是会被人改的，判据也就跟着变成"在测那个开关没被按下"。所以本工厂的签名里**没有这一项**，
   要建单请走编排（`orchestrate/nodes/approval_gate.py` → `approval/service.py`）。

2. **注入面不设默认值**：`session_factory` / `completer` / `approvers` / `graph_resumer` /
   `machine_principals` / `danger_list` 一个都不猜。给了才往 `app.state` 上放，没给就**不放** ——
   各端点自带的 503 fail-closed 才是那条"没装配就不假装能跑"规则的唯一强制点
   （`api/requests.py` 模块头那两条 503、`api/approvals.py` 的 `approvers_not_configured`）。
   在这里补一个"缺省给个空集合"等于把 503 变成 200，那是宪法 II 禁的降级路径。

3. **它不碰 DSN、不建引擎**。会话从 `persistence/session.py` 来（`make_engine` / `new_session`），
   由调用方（CLI 或测试）建好后注进来。本模块因此可以在没有 `.env` 的环境里被 import 与判读 ——
   口径同 `selfcheck.py` 只用标准库那条边界。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from acp.api import approvals, autonomy, growth, requests, tasks, web

#: 对外挂载面：**恰好**这六支，名字 = 模块名 = 判据里点名的名字（一事实一载体）。
#: 那支 `internal_router` 刻意不在这个集合里，理由见模块头第 1 条。
#: 第五、六支是 MVP-2 加的（owner 2026-10-19 第二步 / 第三步指令）：看板的只读面与 SSE 推流、
#: 进化档案的读写面。它们只提供"看得见"与"改档案"，不提供第二条能改审批结论或任务状态的通路。
PUBLIC_ROUTERS: tuple[tuple[str, Any], ...] = (
    ("requests", requests.router),
    ("tasks", tasks.router),
    ("approvals", approvals.router),
    ("autonomy", autonomy.router),
    ("web", web.router),
    ("growth", growth.router),
)

#: 可注入项的名字。判据拿它核"签名里没有别的东西"，也拿它核"给了才落到 app.state 上"。
INJECTED_KEYS: tuple[str, ...] = (
    "session_factory",
    "completer",
    "approvers",
    "graph_resumer",
    "intake_runner",
    "machine_principals",
    "danger_list",
    "static_dir",
    "bus_root",
    "review_entry_name",
    "review_mode",
)


def create_app(
    *,
    session_factory: Callable[[], Any] | None = None,
    completer: Any | None = None,
    approvers: Any | None = None,
    graph_resumer: Callable[..., Any] | None = None,
    intake_runner: Callable[..., Any] | None = None,
    machine_principals: Any | None = None,
    danger_list: Mapping[str, Any] | None = None,
    static_dir: Path | None = None,
    bus_root: Path | None = None,
    review_entry_name: str | None = None,
    review_mode: str | None = None,
) -> FastAPI:
    """建一个对外的 app：挂六支公开 router，按给到的注入项填 `app.state`，给了 `static_dir` 才挂静态面。

    返回值直接交给 `uvicorn` 或 `TestClient`。本函数**不校验注入是否齐全** ——
    齐全与否的表现形式是端点自己那两条 503，不是装配期报错：平台上"服务起来了但存储没接"
    是一个真实要跑的形态（自检报 C2 红、请求报 503），把它改成"启动即炸"会吃掉那条可读的失败。

    `static_dir` 是这一条唯一的例外，而且是刻意的：挂错目录不是"这一个端点不能用"，而是
    **整个 app 起不来或把别人的目录当成本站根**（挂在 `/` 上的 StaticFiles 会兜住所有未被路由
    命中的路径）。所以它不进 503 那一档 —— 给了就必须是个真目录，否则装配期就抛 `BoardAssetsMissing`，
    不服务一条"看板能打开但全是 404"的假成功。
    """
    app = FastAPI(title="acp", version="0.1.0")
    for _, router in PUBLIC_ROUTERS:
        app.include_router(router)
    supplied = {
        "session_factory": session_factory,
        "completer": completer,
        "approvers": approvers,
        "graph_resumer": graph_resumer,
        "intake_runner": intake_runner,
        "machine_principals": machine_principals,
        "danger_list": danger_list,
        "static_dir": static_dir,
        "bus_root": bus_root,
        "review_entry_name": review_entry_name,
        "review_mode": review_mode,
    }
    for key in INJECTED_KEYS:
        if supplied[key] is not None:
            setattr(app.state, key, supplied[key])
    if static_dir is not None:
        app.mount("/", _BoardFiles(directory=_checked_static_dir(static_dir), html=True), name="board")
    return app


class _BoardFiles(StaticFiles):
    """看板静态面。**只加一件事**：把"这个路径在文件系统上不合法"从 500 降成 404。

    2026-10-27 实测撞到的形状：浏览器请求 `/**（已在预览面板打开）`（链接被 markdown 吞掉的产物），
    Windows 上 `os.stat` 抛 `OSError: [WinError 123] 文件名、目录名或卷标语法不正确`，
    Starlette 的 `StaticFiles` 不接这一类 ⇒ 不是 404 而是 500，还把整段调用栈打进日志。
    那不是"服务坏了"，是"这个 URL 不可能对应任何文件"，按 404 说话才是诚实的读数。

    捕获面刻意是 `OSError` 而不是某一串非法字符：盘掉线、权限不足同样落这一档 ——
    静态面没有"可恢复错误"这一级，5xx 在这里只会训练人忽略日志。真要排障，访问行仍然在。
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        try:
            return await super().get_response(path, scope)
        except OSError:
            return PlainTextResponse("Not Found", status_code=404)


class BoardAssetsMissing(ValueError):
    """`static_dir` 给了却不是一个目录。装配期就拒，不留"看板打开了但里面是空的"那种形态。"""


def _checked_static_dir(static_dir: Path) -> Path:
    target = Path(static_dir)
    if not target.is_dir():
        raise BoardAssetsMissing(
            f"看板产物目录不存在或不是目录：{target}（先跑 `npm --prefix web run build`，"
            "或把 --dist 指到一个真目录；这里不给'没有就跳过挂载'的回退，那会把缺产物读成站点正常但全 404）"
        )
    return target
