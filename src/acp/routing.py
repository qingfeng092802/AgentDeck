"""`model_routing`：请求级别的"谁来执行、谁来把关"（2026-10-31 路线 B）。

形状存在 EN-01 的 `model_routing` 列里（JSONB），**不存任何密钥**：

    {"executor": {"kind": "qoder" | "api", "vendor": "<厂商显示名>", "model": "<模型 id>",
                  "thinking": "low|medium|high|off"},
     "reviewer": {"agent_id": "<在册 agent>"},
     "acceptor": {"agent_id": "<在册 agent>"}}

三条口径（改这里之前先读一遍）：

1. **执行者只有两支**：`kind=qoder` = 原来的 CLI 路径（默认、首选，老行为一字不改）；
   `kind=api` = 纯 API 执行器（`EXECUTOR_API_AGENT`），vendor/model 决定它打哪个厂商。
   `executor` 整格缺失 ⇒ 不干预，照旧走 `select_worker` 的四要素排序。
2. **把关席是偏好，不是命令**：给了的 agent 只有在**当席候选里**才被采纳；三岗互斥与自审禁令
   仍由 `assign_guards` 断言。没采纳的偏好进事件流（`routing_ignored`），不静默吞掉。
3. **密钥不在这里**：vendor 只决定从哪个环境变量取（见 `adapters/doubao/exec.py`）。
   `model_routing` 会随需求单落库、随读面回给浏览器 ⇒ 往里放 key 等于把凭据写进数据库。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 执行者的两支。`qoder` 是默认：CLI 读写仓库那条路，任何没写路由的旧请求都落在这里。
EXECUTOR_QODER = "qoder"
#: 纯 API 执行器**复用注册表里已有的 API 形态 agent**（`doubao`），不新增条目。
#: 为什么不是新开一支：注册表条数被 `params-ref §3` 的"并发上限 = 首批接入 Agent 数"关系式钉着
#: （`concurrency.yaml` 的 3 是 owner 定的平台值），加一条就得改并发 —— 那是产品决定，不是这次能顺手改的。
#: 复用之后：`kind=api` 时执行席写 `doubao`，真正打哪家由路由里的 vendor/model 决定
#: （`adapters.doubao` 在**有路由**时换成纯 API 执行适配器）。报告里的厂商/模型读 `model_routing`，
#: 不读 agent id。
EXECUTOR_API = "api"
EXECUTOR_API_AGENT = "doubao"

#: 路由能点名的三个席（与 `assign_guards.ROLES` 同值；执行席只能落两支之一，另一支由 table 决定）。
ROUTE_ROLES = ("executor", "reviewer", "acceptor")


class RoutingSpecError(ValueError):
    """把路由翻成执行 spec 失败（缺厂商/模型、厂商不认识、那把 key 不在）。

    为什么定义在这一层：适配器不许 import `adapters.spec_factory`（同层互相 import 有守卫），
    也不许 import `orchestrate`（反向依赖有守卫）。工厂捕获它再转成对外的 `SpecRefused`。
    消息里只有厂商名与变量**名**，MUST NOT 出现任何凭据值。
    """


def routing_of(row: object) -> dict[str, Any] | None:
    """从 EN-01 行读路由。坏形状（None / 非映射）按"没给"处理 —— 写入口已经校验过。"""
    raw = getattr(row, "model_routing", None)
    return dict(raw) if isinstance(raw, Mapping) else None


def executor_agent(routing: Mapping[str, Any] | None) -> str | None:
    """路由里点名的执行者 agent_id；没点名或不认识的 kind ⇒ `None`（不干预）。"""
    if not routing:
        return None
    executor = routing.get("executor")
    if not isinstance(executor, Mapping):
        return None
    kind = str(executor.get("kind") or "").strip().lower()
    if kind == EXECUTOR_QODER:
        return EXECUTOR_QODER
    if kind == EXECUTOR_API:
        return EXECUTOR_API_AGENT
    return None


def seat_preference(routing: Mapping[str, Any] | None, seat: str) -> str | None:
    """把关席（`reviewer` / `acceptor`）点名的 agent_id。"""
    if not routing:
        return None
    value = routing.get(seat)
    if isinstance(value, Mapping):
        agent_id = str(value.get("agent_id") or "").strip()
        return agent_id or None
    return None


def executor_target(routing: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """API 执行者要打哪家、哪个模型：`(vendor 显示名, model id)`。缺一即 `None`。"""
    if not routing:
        return None
    executor = routing.get("executor")
    if not isinstance(executor, Mapping):
        return None
    vendor = str(executor.get("vendor") or "").strip()
    model = str(executor.get("model") or "").strip()
    return (vendor, model) if vendor and model else None


def thinking_of(routing: Mapping[str, Any] | None) -> str:
    """执行席的思考强度；缺省 `medium`（与设置页四席的默认同值）。"""
    if not routing:
        return "medium"
    executor = routing.get("executor")
    if not isinstance(executor, Mapping):
        return "medium"
    return str(executor.get("thinking") or "medium").strip().lower() or "medium"

def seat_model(routing: Mapping[str, Any] | None, seat: str) -> tuple[str, str, str, str] | None:
    """模型席位：`(vendor, model, base_url, key_fp)`；不是模型席位（只点了 agent 或没点）⇒ `None`。

    路线 C（2026-10-31）：三个席都能被点名成一个"模型实例"。`key_fp` 是提交时对 key 取的
    短指纹（**不是 key**），用来区分"同模型不同 key"—— 三岗互斥的判定单位改用模型实例指纹时，
    它必须存在；没带 key 的席位按 `env` 计（同一个环境变量就是同一个实例）。
    """
    if not routing:
        return None
    value = routing.get(seat)
    if not isinstance(value, Mapping):
        return None
    vendor = str(value.get("vendor") or "").strip()
    model = str(value.get("model") or "").strip()
    if not vendor or not model:
        return None
    base_url = str(value.get("base_url") or "").strip()
    key_fp = str(value.get("key_fp") or "env").strip() or "env"
    return (vendor, model, base_url, key_fp)


def seat_identity(routing: Mapping[str, Any] | None, seat: str, fallback: str) -> str:
    """三岗互斥的判定单位：模型席位 ⇒ `model:<vendor>:<model>:<key_fp>`；否则回落 agent_id。

    2026-10-31 owner 裁决（路线 C）：自由路由下三席可能共用同一个 API 载体，互斥不能再只看
    agent_id —— 但**底线不变**：三个��位的实例指纹必须两两不同（同模型同 key 两席仍被拦）。
    """
    target = seat_model(routing, seat)
    if target is None:
        return str(fallback)
    vendor, model, _base_url, key_fp = target
    return f"model:{vendor}:{model}:{key_fp}"


def seat_thinking(routing: Mapping[str, Any] | None, seat: str, *, default: str = "medium") -> str:
    if not routing:
        return default
    value = routing.get(seat)
    if not isinstance(value, Mapping):
        return default
    level = str(value.get("thinking") or default).strip().lower()
    return level or default

#: 内置厂商显示名 → 官方凭据变量名（`params-ref §4`）。自定义模型池的厂商不在表里 ⇒ 必须带 key。
VENDOR_ENV: Mapping[str, str] = {
    "深度求索 DeepSeek": "DEEPSEEK_API_KEY",
    "阿里云通义千问": "DASHSCOPE_API_KEY",
    "字节跳动豆包": "ARK_API_KEY",
}


def vendor_env(vendor: str) -> str | None:
    return VENDOR_ENV.get(str(vendor).strip())

