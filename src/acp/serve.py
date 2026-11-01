"""M2-001：本机 HTTP 启动入口（FR-049 的"在本机提供网页看板"、宪法原则 V 的 `127.0.0.1` 看板形态）。

MVP-1 全程没有这一支（`quickstart.md` 在 2026-10-19 之前那一格逐字写着"本仓没有 HTTP 启动入口"，
复算串是 `grep -rn "uvicorn" src/acp --include=*.py`，当时读 1 处、且那一处是 `app.py` 的文档串）。
本笔把那读数变成"有调用点"，所以那一格的**旧读数按日期留着不改写**，新读数在下面补注。

三件事，且只做这三件：

1. **绑址只认环回**。`--host` 只接受 `127.0.0.1` / `::1`，别的一律在**起服务之前**拒
   （`resolve_host()` 先跑，`uvicorn.run` 后跑）。理由不是洁癖：MVP-1 与 MVP-2 现阶段都**没有认证**
   （`api/approvals.py` 模块头第 2 条：`X-ACP-Principal` 挡披错身份，挡不住冒充），
   而看板要能 `POST /approvals/{id}/resolve` —— 绑到 `0.0.0.0` 等于把一张能改审批结论的写端点
   摊到局域网里每一个能连到这台机器的人。
2. **装配只有 `create_app()` 一处**。这里不新建 app、不挂 `internal_router`，只装一个
   `TrustedHostMiddleware`（P1-12，环回 Host 白名单）并注六样：`persistence/session.py` 的真会话工厂、`--approver` 那批审批者身份、
   `--machine-principal` 那批机器身份（owner 2026-10-19 裁 W-9 选甲；缺它 `FR-055` 的 403 在生产面上永远读不到），
   按 `--dist` 决定要不要挂看板产物，以及**需求入口的补全器**。前四样**都不给默认值**：给了才落 `app.state`，
   没给就不落（`create_app` 的 `INJECTED_KEYS` 语义），漏配的表现形式是端点自己那两条 503。
   第二处装配点就会出现"对外挂了哪几支"有两个答案（`tests/guards/test_assembly.py` 钉的是恰一处）。
   第五样是**唯一的例外，而且是 owner 明示的**（2026-10-22 Phase 15："修复之前报 503 的问题，
   确保 completer 注入"）：默认注 `orchestrate/local_completer.py` 那条空补全的旁路，
   它一件都不补、响应体用 `completer_id` 自报身份 —— 所以"看板能提需求"不等于"FR-002 已实现"，
   那句话的证据仍在适配器那一支。要回到 503 形态就别走 `build_app`（直接 `create_app()` 不注）。
   **第六样**是 owner 同一天的第二条裁决（2026-10-22 Phase 15"让网页审批自动唤醒大模型"）：
   默认注 `serve_resumer.WebResumer`，它在**后台**跑 `run_resume → run_real`（HTTP 先回、续跑后跑），
   失败只写活动流不往外抛；`build_app(resumer=...)` 传对象就用传的那个 —— 判据靠这一格换替身，
   真厂商一次都不出现。
3. **缺产物就报错，不静默跳过挂载**。给了 `--dist`（默认 `web/dist`）而那个目录不存在 ⇒
   `app.BoardAssetsMissing` 起不来并提示先构建；要看纯 API 面就显式写 `--api-only`。
   "没有产物就不挂、照样把 API 起起来"是一种假成功：使用者会以为看板坏了，而真相是根本没挂静态面。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from acp.adapters.deepseek import review as deepseek_review
from acp.app import create_app
from acp.config import install_logging_filter
from acp.docs import run_documents
from acp.meeting import (
    DEFAULT_ROUNDS,
    ROUNDS_ENV,
    MeetingUnavailable,
    RoundsInvalid,
    default_client,
    resolve_rounds,
    run_meeting,
)
from acp.orchestrate.llm_split import LlmSplitter
from acp.run import StubOrchestrator
from acp.orchestrate.local_completer import LocalPassthroughCompleter
from acp.persistence.session import make_session_factory
from acp.approval.policy import (
    APPROVAL_MODE_AUTO as APPROVAL_MODE_AUTO,
    APPROVAL_MODE_ENV,
    APPROVAL_MODES as APPROVAL_MODES,
    ApprovalModeInvalid as ApprovalModeInvalid,
    resolve_approval_mode,
)
from acp.review.policy import (
    REVIEW_MODE_AUTO as REVIEW_MODE_AUTO,
    REVIEW_MODE_DEEPSEEK as REVIEW_MODE_DEEPSEEK,
    REVIEW_MODE_ENV,
    REVIEW_MODE_MANUAL,
    REVIEW_MODES as REVIEW_MODES,
    ReviewModeInvalid as ReviewModeInvalid,
    resolve_review_mode,
)
from acp.review.runner import ReviewRunner
from acp.review.service import DEFAULT_ENTRY_NAME
from acp.serve_intake import WebIntakeRunner
from acp.serve_resumer import WebResumer

#: 仓库根 = `src/acp/serve.py` 往上三层（口径同 `config/loader.py::REPO_ROOT`）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 看板产物的默认位置（owner 2026-10-19 第二步指令定的外层 `web/`，构建产物在 `web/dist`）。
DEFAULT_DIST = REPO_ROOT / "web" / "dist"

#: 允许绑的地址。**只有环回**，而且这是个封闭集合，不是"以 127. 开头"那种前缀判断：
#: 前缀判断会把 `127.0.0.2` 之外的形态也放进来，而 `0.0.0.0` 恰恰是这里唯一必须挡住的那一个。
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1")

#: 2026-10-29（P0-5 Docker 一键）：容器里的进程**必须**绑 `0.0.0.0`，否则端口映射打不进来 ——
#: `127.0.0.1` 在容器网络命名空间里只指容器自己，`-p 127.0.0.1:8765:8765` 也连不上。
#: 放行它是**有条件的**：只有显式设了这个变量才认，且只认 `0.0.0.0` 这一个值。
#:
#: 为什么不直接把 `0.0.0.0` 加进 `LOOPBACK_HOSTS`：那条集合保护的是"对外可写、无鉴权的审批
#: 决议端点不许离开这台机器"。容器里绑 `0.0.0.0` 并不违反这条 —— 只要发布侧仍然只发布到宿主
#: 环回（`docker-compose.yml` 写的是 `127.0.0.1:${ACP_APP_PORT}:8765`），能连到它的仍然只有本机。
#: 反过来，把默认值放宽就等于让"忘了 docker 这回事也照跑"变成一次敞开的门，所以默认必须还是拒。
NON_LOOPBACK_ENV = "ACP_ALLOW_NON_LOOPBACK"
CONTAINER_BIND = "0.0.0.0"
#: 与 `config/auto_approval.py::_TRUTHY` 同一份词表，但**语义不同**：那边是严格模式
#: （认不出的写法直接报错，因为它要放行自动验收），这边是宽松模式（认不出就当关）——
#: 这是一道"放宽"的开关，错拼必须落在"关"那一侧，不能因为写错而悄悄放行。
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: 默认端口。**不是** 8000：`plan-web.md §一` 的 dev proxy 示例写的是 8000，
#: 而这台机器上 8000 / 8001 是 owner 另一个项目在用的服务（撞端口不是本仓的测试失败，
#: 是别人的服务被打断）⇒ 换到 8765，并把这条写进 Vite 的 proxy 配置，两侧同笔。
DEFAULT_PORT = 8765


class HostRefused(ValueError):
    """绑了非环回地址。失败关闭在起服务之前，消息里不出现任何凭据或 DSN。"""


#: 审查模式只认这两个值；默认 `manual` 保住 Phase 16 的逐字行为（D6）。
#: 取值域与判错类的唯一载体在 `acp.review.policy`，本模块只做转出（`serve.ReviewModeInvalid` 仍是同一个类）。


def resolve_review_runner(
    mode: str,
    *,
    session_factory: Callable[[], object],
    bus_root: Path,
    client: Callable[[deepseek_review.ReviewInput], deepseek_review.ReviewResult] | None = None,
    transport: Any | None = None,
) -> ReviewRunner | None:
    """按模式造审查跑者：`manual` / `auto` ⇒ `None`；`deepseek` ⇒ 真客户端 + 跑者。

    `auto` 为什么也返回 `None`：它根本不在"审查跑者"这一层动手 —— 门禁判 valid 的那一刻
    就在 `realrun.run_card_segment()` 里签掉了（`review_mode=auto` 那一段），
    所以这里既没有模型客户端要造，也没有 `REVIEWING` 的卡要追。
    给它也塞一个 runner 会让同一件事有两个触发点（一个在跑卡事务里、一个在锁释放之后），
    而那正是"同一张卡被签两次"的形状。

    缺凭据时在这里失败（`DeepSeekReviewClient.from_env` 只报变量名），不把服务降级成 manual ——
    "配了 deepseek 但没 key"如果静默变人工，等于把一次部署错误伪装成一切正常。
    """
    normalized = resolve_review_mode(mode)
    if normalized in (REVIEW_MODE_MANUAL, REVIEW_MODE_AUTO):
        return None
    caller = client
    if caller is None:
        caller = deepseek_review.DeepSeekReviewClient.from_env(transport=transport).review
    return ReviewRunner(session_factory=session_factory, client=caller, bus_root=bus_root)


def _llm_callables():
    """造"会议 + 文档 + 真拆单"三个调用面。

    **三条链各一个客户端实例**（2026-10-28 改）。原来会议与拆单共用一个，
    理由是"各造一个会出现会议能用、拆单报缺键这种半死状态" —— 但三个实例读的是
    **同一组**环境变量、同一个模型名，那个半死状态根本不会出现；而共用带来的实际代价是
    "想给会议关掉深度思考，就只能连拆单一起关"。

    实测（`doubao-seed-2-1-pro`，同一 prompt）：开思考 63.8 秒 / 2573 个 reasoning token，
    关思考 2.8 秒 / 0 个，输出都是 65 字。四轮串行 ⇒ 这一项就是 133 秒与 12 秒的差别。
    会议与文档要的是"照着约束把人话说明白"，不是推理；拆单要吐结构化 JSON，保持不干预。

    缺凭据 ⇒ 三者都返回 None，并把原因说在启动日志里。为什么不是"没配就假装有、跑起来再失败"：
    这几件是这一版产品的卖点，没接上时启动那一行必须能看见 —— 否则页面上只会少四句话、
    卡片退回固定四张、三份文档一栏空白，而没人知道是没配还是模型挂了。
    """
    # 讨论轮数（owner 2026-10-28 第 1 条）。**启动期读一次、拒非法值**，与 `ACP_REVIEW_MODE`
    # 同一条口径：一个手滑写成 `0` 的环境变量静默退回默认值的话，现场只会觉得"怎么又是两轮"。
    try:
        rounds = resolve_rounds(os.environ.get(ROUNDS_ENV, str(DEFAULT_ROUNDS)))
    except RoundsInvalid as exc:
        raise SystemExit(f"[serve] 会议轮数非法：{exc}") from None

    try:
        client = default_client()
        meeting_client = default_client(thinking=False)
    except MeetingUnavailable as exc:
        print(f"[serve] 模型未接：{exc.code} · {exc}", file=sys.stderr, flush=True)
        return None, None, None, rounds

    try:
        # 文档节点单开一个客户端：三份 JSON 比一段台词长得多，实测 1200 tokens / 120 秒都不够。
        docs_client = default_client(max_tokens=2400, timeout=300.0, thinking=False)
    except MeetingUnavailable as exc:
        print(f"[serve] 文档节点未接：{exc.code} · {exc}", file=sys.stderr, flush=True)
        docs_client = None

    def meeting(raw_input: str, on_turn=None):
        return run_meeting(raw_input, client=meeting_client, on_turn=on_turn, rounds=rounds)

    def documents(raw_input: str, said: list[str]):
        if docs_client is None:
            raise MeetingUnavailable("docs_client_missing", "文档客户端没造出来（凭据同会议，见启动日志）")
        return run_documents(raw_input, said, client=docs_client)

    def splitter(raw_input: str):
        # 两次都过不了契约就**不回落**（`escalate_on_failure=True`）：Stub 会给出一份通用卡，
        # 而通用卡正是 2026-10-28 两张卡 `ARTIFACT_INVALID` 的起点。回落等于把一次"模型跑偏"
        # 变成一次"看起来成功、下游全死"。
        # 要退回旧行为（回落 Stub）就把这个开关改回 False —— 它是这一处的常量，不是配置项。
        return LlmSplitter(
            client=client,
            raw_input=raw_input,
            fallback=StubOrchestrator(),
            escalate_on_failure=True,
        )

    return meeting, documents, splitter, rounds


def container_bind_allowed(flag: bool | None = None) -> bool:
    """容器绑 `0.0.0.0` 的开关：`flag` 显式给了就用它，否则读环境变量。默认关。"""
    if flag is not None:
        return flag
    return os.environ.get(NON_LOOPBACK_ENV, "").strip().lower() in _TRUTHY


def resolve_host(raw: str, *, allow_non_loopback: bool | None = None) -> str:
    """把 `--host` 解析成 uvicorn 能用的绑址；不合法就在**起服务之前**拒。

    两档放行：环回无条件；`0.0.0.0` 只在 `ACP_ALLOW_NON_LOOPBACK` 显式打开时认（容器场景）。
    其余一律拒 —— 包括 `192.168.x.x`、`*`、`::`，开了那个变量也不认：本仓需要的只有"容器内
    全网卡"这一种，别的非环回形态都不该有正当理由。
    """
    if raw in LOOPBACK_HOSTS:
        return raw
    if raw == CONTAINER_BIND and container_bind_allowed(allow_non_loopback):
        print(
            f"[serve] 已按 {NON_LOOPBACK_ENV}=1 绑 {CONTAINER_BIND}（容器口径）："
            "发布侧 MUST 只发布到宿主环回（127.0.0.1:8765:8765），"
            "否则一个无鉴权的写端点就上了局域网",
            file=sys.stderr,
            flush=True,
        )
        return raw
    raise HostRefused(
        f"只允许绑 {list(LOOPBACK_HOSTS)}（收到 {raw!r}）⇒ 本仓无鉴权，"
        "对外可写的审批决议端点不能离开这台机器；"
        f"在容器里需要 {CONTAINER_BIND} 时显式设 {NON_LOOPBACK_ENV}=1，并把发布端口留在环回"
    )


def build_app(
    *,
    dist: Path | None = DEFAULT_DIST,
    approvers: Any | None = None,
    machines: Any | None = None,
    bus_root: Path | None = None,
    review_entry_name: str | None = None,
    completer: Any | None = None,
    resumer: Any | None = None,
    intake_runner: Any | None = None,
    review_mode: str | None = None,
    review_client: Any | None = None,
    approval_mode: str | None = None,
) -> FastAPI:
    """真装配：会话工厂走 `persistence/session.py`（DSN 只从环境变量来），静态面按 `dist` 决定。

    `dist=None` 是"这一趟不挂静态面"的唯一写法；给了路径但不是目录 ⇒ `BoardAssetsMissing`，
    理由见模块头第 3 条。

    `approvers` 是**审批决议那一支的钥匙**：`api/approvals.py::_approvers` 没配就 503
    （方向不能反 —— 把"集合为空"读成"谁都能批"会让一次漏配置变成一道敞开的门）。
    本入口刻意不给默认名单：看板上的"提交结论"要不要能用，是由启动的人显式说出来的，不是由代码猜的。

    `machines` 是**"哪些主体算机器"那份名单**（owner 2026-10-19 裁 W-9 选甲）：
    `api/autonomy.py::_machine_principals` 没配就 503、配了才谈得上把机器身份 403 掉。
    这一项在笔 I 之前根本没有注入位，所以 `--as-agent` 那一发在真服务上只读得到 503 ——
    加了它，"工具不能自己改自己的档位"（FR-055）那条禁令第一次在生产面上可验。同样刻意不给默认值。

    `completer` 是**唯一给默认值的注入项**，缺省落到 `LocalPassthroughCompleter`（一条什么都不补的
    旁路，理由与限度写在模块头第 2 条和 `orchestrate/local_completer.py`）。传对象就用传的那个 ——
    Phase 3 的适配器接进来时改的是这里的调用，不是 `plan.py` 里的默认值。

    `resumer` 与 `completer` 同一天、同一轮的第二格（owner 裁决 ③）：不传就注 `WebResumer`，
    它的会话工厂**与本 app 共用同一个**（同一个引擎，不是第二个池子），总线根缺省取
    `<仓库根>/results`（与 `acp.realrun` 从仓库根起跑时的默认值同一拼法；`main()` 会把显式值 resolve 后交进来）。

    `intake_runner` 是受理那一侧的同形注入位：不传就注 `WebIntakeRunner`，于是网页提交需求后
    **不需要再跑一遍 CLI** 也能长出任务卡与 A 类单据。它与 `resumer` 共用一个会话工厂与总线根；
    拆单器仍是仿真那一支（固定四张卡、不读需求正文），这条边界写在 `serve_intake.py` 的模块头。

    `review_mode` 缺省读 `ACP_REVIEW_MODE`（默认 `manual`）；**启动时读一次**，运行期不热更新。
    `deepseek` 模式缺凭据 ⇒ 构造 `DeepSeekReviewClient` 时直接失败（只报变量名），不降级成 manual。
    `review_client` 是判据用的注入位：给了就用它，不再读环境变量。

    `approval_mode` 缺省读 `ACP_APPROVAL_MODE`；**2026-10-29 P0-6 起缺省值改为 `auto`**：
    live 档已经没有审批按钮，缺省 manual 会让新装环境在拆单那一步永久停住。显式设 `manual`
    仍按人工门走。`auto` 下受理完成即用规则名把 A 类单据免确认（`approval.policy.SPLIT_AUTO_RULE`），
    然后**把这一单直接交给 `graph_resumer`** —— 两格共用同一个续跑者对象，所以"免确认之后怎么跑"
    与「批准」那条路是同一段驱动，不是第二份。认不出的取值在启动期就抛 `ApprovalModeInvalid`，
    不降级成 manual：那会让"我以为它开着"与"它其实关着"变成同一屏。
    """
    mode = resolve_review_mode(
        review_mode if review_mode is not None else os.environ.get(REVIEW_MODE_ENV, REVIEW_MODE_MANUAL)
    )
    # 审批门那一格与审查那一格**各自独立解析**：`review` 管"卡交上来谁签验收"，
    # `approval` 管"拆单方案要不要人点头"。由前者推导后者，会让"只免验收、拆单要看一眼"
    # 这个组合没有表达方式。
    raw_approval_mode = os.environ.get(APPROVAL_MODE_ENV)
    if approval_mode is None and raw_approval_mode is None:
        print(
            "[serve] ACP_APPROVAL_MODE 未设置 ⇒ 默认 auto（拆单免确认）；要人工批请显式设 manual",
            file=sys.stderr,
            flush=True,
        )
    gate = resolve_approval_mode(
        approval_mode
        if approval_mode is not None
        else (raw_approval_mode if raw_approval_mode is not None else APPROVAL_MODE_AUTO)
    )
    factory = make_session_factory()
    resume_root = bus_root if bus_root is not None else REPO_ROOT / "results"
    if resumer is not None:
        graph_resumer = resumer
    else:
        graph_resumer = WebResumer(
            session_factory=factory,
            bus_root=resume_root,
            review_runner=resolve_review_runner(
                mode,
                session_factory=factory,
                bus_root=resume_root,
                client=review_client,
            ),
            review_mode=mode,
        )
    # 三个调用面只造一次：`_llm_callables()` 每调一次都会新建一个客户端，
    # 之前写成 `[0]` 与 `[1]` 两次调用，等于会议与拆单各握一把钥匙、各建一条连接池。
    meeting_callable, documents_callable, splitter_callable, meeting_rounds = _llm_callables()
    split_runner = (
        intake_runner
        if intake_runner is not None
        else WebIntakeRunner(
            session_factory=factory,
            bus_root=resume_root,
            meeting=meeting_callable,
            documents=documents_callable,
            splitter=splitter_callable,
            approval_mode=gate,
            # 与「批准」那条路**同一个对象**：免确认之后由它续跑，不另造一份驱动。
            resumer=graph_resumer,
            # 只用来写「会议轮数」那一格与打印进度；真正开几轮由上面的 `meeting` 调用面执行。
            meeting_rounds=meeting_rounds,
        )
    )
    return create_app(
        session_factory=factory,
        static_dir=dist,
        approvers=approvers,
        machine_principals=machines,
        bus_root=bus_root,
        review_entry_name=review_entry_name,
        completer=completer if completer is not None else LocalPassthroughCompleter(),
        graph_resumer=graph_resumer,
        intake_runner=split_runner,
        review_mode=mode,
    )


#: P1-12：只接受环回 Host。绑址只挡"连不上"，挡不住 DNS rebinding —— 恶意页面可以把
#: 自己的域名解析到 127.0.0.1，再用浏览器带着 `Host: evil.example` 打本机写端点。
ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


def install_host_guard(app: FastAPI) -> None:
    """给生产启动的 app 装 Host 校验；单独成函数是为了判据能在一个小 app 上量它。"""
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., object] | None = None,
) -> int:
    """解析参数 → 先验绑址 → 再装配 → 最后才把 app 交给 `uvicorn.run`。

    `runner` 是给判据留的注入位：不传就是真的 `uvicorn.run`。测试传一个记账的替身，
    于是"被拒的那一发根本没碰服务器"这件事量得出来，而不是只断一条异常类型。
    """
    install_logging_filter()
    parser = argparse.ArgumentParser(prog="acp.serve", description="本机看板与只读 API 的启动入口")
    parser.add_argument("--host", default="127.0.0.1", help=f"只接受 {list(LOOPBACK_HOSTS)}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST, help="看板产物目录（StaticFiles 挂载点）")
    parser.add_argument("--api-only", action="store_true", help="不挂静态面，只提供 JSON / SSE 面")
    parser.add_argument(
        "--approver",
        action="append",
        default=[],
        dest="approvers",
        metavar="PRINCIPAL",
        help="允许提交审批结论的身份，可重复；一个都不给时看板能看不能批（503 approvers_not_configured）",
    )
    parser.add_argument(
        "--machine-principal",
        action="append",
        default=[],
        dest="machines",
        metavar="PRINCIPAL",
        help="登记为机器的身份（工具的 `agent_id` 之类），可重复；一个都不给时"
        " `PATCH /autonomy` 一律 503 `machine_principals_not_configured`，给了才谈得上把那一格 403 掉",
    )
    parser.add_argument(
        "--bus-root",
        type=Path,
        default=Path("results"),
        help="文件总线的外层根（默认 results；与 acp.realrun 的默认值同一拼法）。"
        "评审通过且全部卡交付后，端点从这里组交付包",
    )
    parser.add_argument(
        "--review-entry",
        default=None,
        help="组包时认定的入口页文件名。**不传就按 PRD 第一页推**（U2，2026-10-28）；"
        f"PRD 也读不到才退回 {DEFAULT_ENTRY_NAME}。传了就以传的为准 —— 显式压过推断",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(list(argv) if argv is not None else None)

    host = resolve_host(args.host)
    app = build_app(
        dist=None if args.api_only else args.dist,
        approvers=frozenset(args.approvers) if args.approvers else None,
        machines=frozenset(args.machines) if args.machines else None,
        bus_root=args.bus_root.resolve(),
        review_entry_name=args.review_entry,
    )
    # P1-12：生产启动时装 Host 守卫；测试直接用 `build_app()` 时不会经过这里，
    # 所以既有 TestClient（Host: testserver）不受影响。
    install_host_guard(app)
    run = uvicorn.run if runner is None else runner
    run(app, host=host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口形状由 main() 的判据覆盖
    raise SystemExit(main(sys.argv[1:]))
