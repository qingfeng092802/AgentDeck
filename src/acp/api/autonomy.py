"""T103：自主度档位的 HTTP 写入面（FR-055｜R-15，`data-model.md` EN-16 + §4 强制点表）。

一条端点：`PATCH /autonomy`。**只有改档这一个动作**，读档面（GET）不在这里 ——
它不属于本任务（`tasks.md` 的 T103 行只写"改档端点 + 403 拦截"），而且档位的使用者侧呈现
归看板（R-13，MVP-2）与 CLI（T106 的 `python -m acp.autonomy`），在 HTTP 面上先开一个 GET
就会多一条"谁都能读到当前档位"的读侧契约，而那份契约 owner 还没写。

**这个端点只做翻译，不做判定**（原则 I）：三类拒绝（值不合法 / 主体不是使用者 / 无行建行缺指纹）
全部在 `persistence/autonomy.py` 里成文，这里只把 `AutonomyWriteRejected.code` 换成状态码 ——
那张映射表也在仓储（`HTTP_STATUS`，owner 2026-10-15 裁决 1 逐条给值）。
在端点里再写一遍判定的话，就会出现"端点放行了、仓储还在拒"这种两处各说一套的形状，
而那正是 `tasks.md` T102 行下第 ② 项登记的洞。

**两个 fail-closed 的注入项**（口径同 `api/requests.py` 的"补全器与会话工厂都必须注入"）：

- `app.state.machine_principals`：**哪些主体是机器**。缺这一项就一律 503，不当成"没有机器"。
  这是 T102 偏离 ③ 的闭环半边 —— 自动主体的名字（`orchestrate/nodes/select_worker.py::AUTOMATED_ACTOR`）
  MUST 由装配点（T123）传进来，本模块一个字都不写死：仓储反过来 import 编排层就是向上成环，
  而那个常量自己的注释写着"这是一条临时约定，不是一个已成立的身份" ⇒ 抄第二份必漂。
  **空集合也算没配**：这个平台一定有总管工具，读到空集合只会是漏配置，
  把它读成"没有机器"就让一次漏配置变成一道敞开的门（FR-055 唯一要挡的那扇门）。
- `app.state.danger_list`：当前生效的清单（`config/loader.load_danger_list()` 的结果）。
  只在**无行建行**那一支用得上（`danger_list_version` 是 NOT NULL）。
  端点自己不读盘：T101 的口径是"重载走 loader，不是靠重启进程碰运气"，
  而重载的触发点是装配/启动路径；一个 HTTP 请求顺手读一次文件就会出现第二个"生效版本"的来源。

**改档的审计载体是 EN-16 本身**（`updated_by` / `updated_at` 两列 + 当前 `mode`），
这里不往 EN-14 写事件行：`event_log_kind` 域里没有 `MODE_CHANGE` 这个取值
（`persistence/enums.py:59`），要记就得改域 + 加迁移，而 owner 未裁 ⇒ 宁可不记也不造一个域外的行。

**提交边界在本端点**（2026-10-27 收口）：此前这里与其余三个写面一样只 `flush()`，理由是
"提交归装配点 T123"，而装配点 `serve.build_app` 从来没有提供请求级提交钩子 —— 结果是
`python -m acp.autonomy set` 稳定回 200 而库里档位不变，且会话不 `close()`、连接只出不还。
现在与 `api/approvals.py` / `api/requests.py` 同形：`with _store(request)` 保证一定收尾，
成功路径显式 `commit()`。判据仍"发一次 HTTP，再另起连接回库里读"，不凭响应体自述。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from acp.api.approvals import PRINCIPAL_HEADER
from acp.config import auto_approval
from acp.config import runtime
from acp.persistence import autonomy
from acp.persistence import events
from acp.review import policy as review_policy

router = APIRouter()


class ModePatch(BaseModel):
    """请求体：`{project_key, mode}`，多余字段 422（`extra="forbid"`，口径同 `tasks.py::TaskPatch`）。

    **`mode` 故意写成裸 `str` 而不是 `Literal["FULL", "SAFE"]`**：域值的唯一载体是
    `persistence/enums.py` 的 `autonomy_mode`（`data-model.md` EN-16 那一行只引用不重述）。
    写 `Literal` 就是给同一个域造第二个载体，而且 pydantic 会抢在仓储之前 422，
    响应体里就没有 `mode_not_allowed` 这个 code 了 —— owner 裁决 1 给这条 code 定了 422，
    要的是"值不合法"这件事由域来说，而不是由某个模型的字段类型来说。
    """

    model_config = ConfigDict(extra="forbid")

    project_key: str = Field(min_length=1)
    mode: str


class AutonomyResponse(BaseModel):
    """改档后的当前态。六个字段一一对应 EN-16 的六列，一个都不折。"""

    project_key: str
    mode: str
    danger_list_version: str
    updated_by: str
    updated_at: str
    last_intercept_at: str | None


def _session(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="store_not_configured：改档要读写 EN-16 ⇒ 装配方 MUST 注入 app.state.session_factory",
        )
    return factory()


@contextmanager
def _store(request: Request) -> Iterator[Any]:
    """一次请求一个会话：出去一定 `close()`，落地由端点显式 `commit()`。

    形状与 `api/approvals.py::_store` 同一口径：不套 `new_session`（它的 `rollback()`
    会把测试夹具的外层事务一起回滚），也不在异常分支里替调用方回滚 ——
    未提交的改档意图由 `close()` 丢弃，而不是靠下一发请求替我们收拾。
    """
    session = _session(request)
    try:
        yield session
    finally:
        session.close()


def _principal(request: Request) -> str:
    """改档主体取自 `X-ACP-Principal`（头名的唯一载体在 `api/approvals.py`，不在这里再写一遍）。

    缺头 ⇒ 403。这一条与审批面同形，也同它共享那条披露：**MVP-1 没有认证**，
    所以这道门挡的是"披错身份误操作"，挡不住冒充（同 `api/approvals.py` 模块头第 2 条）。
    """
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        raise HTTPException(
            status_code=autonomy.HTTP_STATUS[autonomy.PRINCIPAL_NOT_ALLOWED],
            detail=f"{autonomy.PRINCIPAL_NOT_ALLOWED}：请求头 {PRINCIPAL_HEADER} 缺失 ⇒ 没有主体就没有改档",
        )
    return principal


def _machine_principals(request: Request) -> frozenset[str]:
    """自动主体的集合。**没注入或注入空集合 ⇒ 503**，不读成"这个部署没有机器"。

    方向不能反：这一项漏配的后果是"总管工具可以自己把档位从 SAFE 切到 FULL"，
    而 FR-055 唯一的强制点就是这条。宁可整条端点不可用，也不开门。
    """
    declared = getattr(request.app.state, "machine_principals", None)
    if not declared:
        raise HTTPException(
            status_code=503,
            detail=(
                "machine_principals_not_configured：改档端点 MUST NOT 自己猜哪些主体是机器 ⇒ "
                "装配点（T123）注入 {select_worker.AUTOMATED_ACTOR}（空集合也算没配）"
            ),
        )
    return frozenset(declared)


def _danger_list(request: Request):
    """当前生效的清单；没注入就给 `None`。

    这里**不给 503**：改档的既有行分支用不上清单（`danger_list_version` 不在它的写者集合里），
    把它变成整条端点的前置条件就会出现"档位明明能改，却因为一项没配而全拒"的过度失败。
    缺它真正要命的场合只有无行建行那一支 —— 那里由仓储抛 `danger_list_required`，映射成 400。
    """
    return getattr(request.app.state, "danger_list", None)


@router.patch("/autonomy", response_model=AutonomyResponse)
def patch_autonomy(body: ModePatch, request: Request) -> AutonomyResponse:
    """切换自主度档位：使用者可切，平台可自切这件事在这里被拒（FR-055）。"""
    with _store(request) as session:
        try:
            row = autonomy.set_autonomy_mode(
                session,
                body.mode,
                _principal(request),
                project_key=body.project_key,
                danger_list=_danger_list(request),
                machine_principals=_machine_principals(request),
            )
        except autonomy.AutonomyWriteRejected as exc:
            # `from None`：不把 pydantic/SQLAlchemy 的栈话带进日志，那条链里可能夹着连接串。
            raise HTTPException(status_code=exc.http_status, detail=f"{exc.code}：{exc}") from None
        session.commit()  # 落库：档位切换必须提交，否则 200 而库里不变
        return AutonomyResponse(
            project_key=row.project_key,
            mode=str(row.mode),
            danger_list_version=row.danger_list_version,
            updated_by=row.updated_by,
            updated_at=row.updated_at.isoformat(),
            last_intercept_at=row.last_intercept_at.isoformat() if row.last_intercept_at else None,
        )


# ---------------------------------------------------------------- 低风险自动验收开关（2026-10-28）


class AutoApprovalPatch(BaseModel):
    """胶囊按钮写的那一格。`enabled=None` 表示"恢复成跟随部署期环境变量"。

    `StrictBool` 不是洁癖：pydantic 默认的宽松模式会把 `"true"` / `"1"` / `"yes"` 都收成 `True`，
    而这一格决定"谁可以代签"。宽松模式的后果是"客户端拼错一个字符串，机器就开始替人签字了"，
    且没有任何一条日志会说这件事发生过 —— 与 `config/runtime.py::_checked` 里"字符串不猜"
    同一条理由。`null` 仍然合法（那是"恢复跟随环境变量"）。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None


class AutoApprovalView(BaseModel):
    """胶囊按钮读的那一格。**`source` 是承重的**：它说清这个"开"是谁开的。

    没有 `source` 的界面会撒谎：部署时设了 `ACP_AUTO_ACCEPT_LOW_RISK=true`、而面板上没人按过，
    按钮亮着而人以为自己没开过 —— 反过来（按了却被环境变量盖掉）更糟，按一下毫无反应。
    """

    enabled: bool
    #: `runtime`（面板按的）/ `env`（部署期变量）/ `default`（都没设 ⇒ 关）
    source: str
    #: 哪几种产物后缀算低风险。界面把它显示出来，人才知道"低风险"的边界在哪。
    low_risk_formats: list[str]
    #: 审查员这一侧准备好了没有（`ACP_REVIEW_MODE=deepseek`）。没准备好时打开开关**不会有任何效果**，
    #: 因为代签的前提是"审查员给出 PASS"，而那一侧根本没跑。界面据此给出解释，而不是让按钮看起来失灵。
    reviewer_ready: bool
    #: 部署期变量的名字（显示在界面上，用户要改部署时得知道改哪个）。
    env_flag: str
    #: 当前审查模式（`manual` / `deepseek` / `auto`，2026-10-28 加）。界面靠它说清"为什么这一单
    #: 没有待评审的卡"：`auto` 下门禁自己签，这个胶囊按钮**不参与**那条判定。
    review_mode: str


def _review_mode() -> str:
    """当前进程看到的审查模式。不认识的值按 `manual` 报（启动期已经拒过一次，走到这里说明
    有人中途改了环境变量 —— 那种情况下如实报"这个进程此刻没认出它"比猜一个更安全）。"""
    raw = str(os.environ.get(review_policy.REVIEW_MODE_ENV, review_policy.REVIEW_MODE_MANUAL))
    normalized = raw.strip().lower()
    return normalized if normalized in review_policy.REVIEW_MODES else review_policy.REVIEW_MODE_MANUAL


def _reviewer_ready() -> bool:
    """审查员那一侧开了没有。**读的是同一份环境变量**，不是第二份配置。

    `serve.build_app` 在启动期解析一次 `ACP_REVIEW_MODE`（运行期不热更新）。这里再读一次环境变量，
    与那一次一致；不一致只可能发生在有人中途改了环境变量 —— 那种情况下重启才是正确操作，
    而这里如实把"当前进程看到的值"报出来，好过报一个界面自己猜的值。

    `auto` **不算"审查员就绪"**：那条路上没有审查员，门禁判产物达标就过，
    所以这个胶囊按钮在 `auto` 下确实不起作用 —— 如实报 `False`，由界面说明原因，
    而不是报 `True` 让人以为"开关开着、只是今天恰好没触发"。
    """
    return _review_mode() == review_policy.REVIEW_MODE_DEEPSEEK


def _auto_approval_view() -> AutoApprovalView:
    enabled, source = auto_approval.effective_switch()
    return AutoApprovalView(
        enabled=enabled,
        source=source,
        low_risk_formats=sorted(auto_approval.low_risk_formats()),
        reviewer_ready=_reviewer_ready(),
        env_flag=auto_approval.AUTO_ACCEPT_ENV,
        review_mode=_review_mode(),
    )


@router.get("/autonomy/auto-approval", response_model=AutoApprovalView)
def read_auto_approval() -> AutoApprovalView:
    """读当前生效值与来源。**不碰库、不要身份** —— 与 `GET /runtime` 同一条口径：
    读一个配置不该需要先证明自己是谁。"""
    return _auto_approval_view()


@router.patch("/autonomy/auto-approval", response_model=AutoApprovalView)
def write_auto_approval(body: AutoApprovalPatch, request: Request) -> AutoApprovalView:
    """打开 / 关闭低风险自动验收。写 `config/runtime.yaml` 的那一格，**写完回读**才回 200。

    为什么写文件而不是写库：这一格与 `max_tokens` / `fragment_merge_ms` 同类 —— 它是**部署参数**，
    不是某一张单的状态。放同一份文件里，"改完即生效、来源可指纹"这两件事就是现成的（加载器不缓存）。
    写库会多出一张只有一列的表，还要迁移。

    为什么要求身份头：这是一个**改变谁签字**的动作，与批单同级，不是读配置。缺头 403。
    """
    principal = _principal(request)
    current = runtime.load_runtime_params()
    try:
        # `replace` 而不是逐键重抄：这一格只改一个键，别的键必须**原样**带过去。
        # 逐键重抄的下场是"以后加了第四个键，忘了在这里补一行，于是面板一保存就把它清成默认" ——
        # 而那种丢失不会报错，只会在某天发现"参数明明写了却没生效"。
        written = runtime.write_runtime_params(
            dataclass_replace(current, auto_accept_low_risk=body.enabled)
        )
    except runtime.RuntimeParamsError as exc:
        raise HTTPException(status_code=409, detail=f"runtime_params_not_writable：{exc}") from None
    view = _auto_approval_view()
    # 落一条 EN-14：谁在什么时候把"谁能代签"这件事改了。不写这条，审计里就只剩文件里那个值，
    # 而文件不带"谁改的"。库不可用时不阻塞（这一格的价值是留痕，不是门禁），但**失败要说出来**：
    # 静默 `pass` 的下场是本轮实测过的 —— `entity` 写了个域外取值，异常被吞掉，
    # 端点照样 200，而"留痕"一行都没有。那种沉默比报错更难发现。
    try:
        with _store(request) as session:
            events.record(
                session,
                # EN-16 的表名，但这一格的实体是**部署参数文件**，所以 `entity_id` 是文件名。
                entity="autonomy_state",
                entity_id="runtime.yaml",
                kind="TASK",
                actor_agent_id=principal,
                payload={
                    "node": "auto_approval",
                    "action": "auto_approval_switch_written",
                    "enabled": body.enabled,
                    "effective": view.enabled,
                    "source": view.source,
                    "source_sha256": written,
                },
                created_at=dt.datetime.now(dt.timezone.utc),
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - 留痕失败不改写成功事实（文件已经原子写回了）
        print(
            f"AUTO-APPROVAL 留痕失败 :: {type(exc).__name__}: {exc}"
            " ⇒ 文件已写回，但审计里没有'谁改的'这一行",
            file=sys.stderr,
            flush=True,
        )
    return view
