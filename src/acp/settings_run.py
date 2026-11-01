"""按设置页的四席模型直跑一条需求（最小接线，2026-10-29 owner 指令）。

这不是第二条编排链，也不碰 EN-01/EN-02 的状态机：它只回答一个此前没有答案的问题 ——
“设置页里选的四个模型，真的能按四席分工跑完一条复杂需求吗？” 产物落
`results/<project>/settings-run-<run_id>/`，报告同目录 `run-report.json`。

四席的调用面：
- `orchestrator`：规划（页面 / 验收 / 文件清单）；
- `executor`：逐文件写正文；
- `reviewer`：按验收标准逐条审查；
- `acceptor`：根据审查结论做最终接受/拒绝。

**密钥只在本进程内存里**：入参 `SeatModel.api_key` 的 `repr=False`，报告不写；HTTP 错误消息
过 `redact_bus_text`。本模块不写 DB、不写 EN-14，避免把一次“设置连通性验证”伪装成平台事实。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from acp.config.loader import redact_bus_text
from acp.content_gate import artifact_completeness

logger = logging.getLogger(__name__)

#: 设置页的厂商显示名 → 厂商 id（与 `web/src/lib/modelCatalog.ts` 的 `id` 同值）。
VENDOR_IDS: Mapping[str, str] = {
    "深度求索 DeepSeek": "deepseek",
    "阿里云通义千问": "qwen",
    "字节跳动豆包": "doubao",
    "月之暗面 Kimi": "kimi",
    "智谱 AI GLM": "zhipu",
    "MiniMax 稀宇科技": "minimax",
    "OpenAI 兼容": "openai-compat",
}

#: OpenAI 兼容端点。模型服务自己的 base_url；请求路径统一追加 `/chat/completions`。
BASE_URLS: Mapping[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "kimi": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.chat/v1",
}

SEAT_NAMES = ("orchestrator", "executor", "reviewer", "acceptor")
#: preflight 那一发的正文预算。**不许按" ping 只有两个字符"来定**：2026-10-30 实测
#: `deepseek-flash` 在 `max_tokens=16` 下把 16 全部花在推理段（`reasoning_tokens=16`、
#: `finish_reason=length`、`content` 空），探针就报 `empty_response` ⇒ 一个完全能用的型号
#: 被判"不可用"，整单在规划之前死掉。同一发放到 128 就正常回 `ok`（推理 86 字 / 正文 2 字）。
#: 256 留的是"推理先吃满预算"这一族的余量，代价是每次探针多几百 token，比误判一整单便宜。
PREFLIGHT_MAX_TOKENS = 256
#: 探针的墙钟与容错。**探针可以重打，正文那一发不行**：2026-10-31 实测同一个 ping
#: 两发分别 2.1s 与 20.7s —— dashscope 的尾巴本来就贴着老的 30s 上限，一发慢响应就把整单
#: 判死在 `preflight:model_unavailable`。而 `_chat` 里"超时不重试"是给 600s 的正文那一发定的，
#: 套在一发 45s 的连通性探针上只会误伤。
PREFLIGHT_TIMEOUT_S = 45.0
PREFLIGHT_RETRIES = 1
#: 只有这两类失败可以换一发重打：它们说的是"这一次没连上"。
#: `http_404` / `http_401` 说的是"这个名字/这把 key 不行"，重打只是浪费一次配额。
PREFLIGHT_RETRYABLE = frozenset({"timeout", "transport"})
#: 传输层重试次数（不含首发）。千问 `qwen3.8-flash` 写 8192 长正文那一发 2026-10-30 两次
#: 都在同一位置 `RemoteProtocolError` 断流；审查报告 P1-13 登记的就是"全仓没有重试消费者"。
TRANSPORT_RETRIES = 1
TRANSPORT_BACKOFF_S = (2.0, 4.0)
#: 预算自适应（统筹/执行/审查/修复/验收每一发都走）：`finish_reason=length` 或正文空 ⇒ 翻倍再试一次。
#: 实测那一发：DeepSeek 独跑时审查给 4096，推理段 10931 字吃到 `length` 而正文为 0 ⇒ 整单死。
BUDGET_RETRIES = 1
#: 自适应允许的预算上限：8192 翻倍到 16384 是最后一档，再往上就超出端点单回复的上限。
BUDGET_CEILING = 16384
ALLOWED_SUFFIXES = frozenset({".html", ".css", ".js", ".md", ".json", ".py"})
INTERACTION_WORDS = ("购物车", "表单", "校验", "点击", "交互", "弹窗", "搜索", "筛选")

#: 跨文件契约：每个文件独立生成时，最容易漂的就是选择器。把它写死成一份，所有执行调用原样带上。
CONTRACT: Mapping[str, str] = {
    "cartBadge": "data-cart-badge",
    "addToCart": "data-add-to-cart",
    "sortSelect": "data-sort-select",
    "productList": "data-product-list",
    "contactForm": "data-contact-form",
    "nav": "main-nav",
}

PREVIEW_SCRIPT = '''"""本地预览：只依赖标准库，双击或 `python serve.py` 后打开 http://127.0.0.1:8000/。"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os

os.chdir(Path(__file__).resolve().parent)
print("本地预览：http://127.0.0.1:8000/（Ctrl+C 停止）", flush=True)
ThreadingHTTPServer(("127.0.0.1", 8000), SimpleHTTPRequestHandler).serve_forever()
'''


@dataclass(frozen=True)
class SeatModel:
    vendor: str
    model: str
    api_key: str = field(repr=False)
    base_url: str
    #: 设置页那一列「思考强度」的真值（`off/low/medium/high`）。2026-10-30 之前它根本不下发，
    #: 页面上写着"中"而请求体里什么都没有 —— 这一格补的是"UI 说的话算不算数"。
    thinking: str = "medium"

    @property
    def vendor_id(self) -> str:
        # 路线 C：自定义模型池里的厂商是个自由标签 ⇒ 不在目录里时给一个中性 id，
        # 让 `_thinking_fields` 落到"不发思考字段"（没有实测过的那一家不猜字段名）。
        return VENDOR_IDS.get(self.vendor, "custom")

    def public(self) -> dict[str, str]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "base_url": self.base_url,
            "thinking": self.thinking,
        }


class SettingsRunError(RuntimeError):
    """一次设置直跑的失败。`stage` 与 `code` 是给前端与日志的稳定键名；消息里不含密钥。"""

    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(f"{stage}:{code}: {message}")
        self.stage = stage
        self.code = code
        self.detail = message


@dataclass
class RunJob:
    """一次后台直跑的内存状态。`seats` 只在工作线程里，不放进这里，避免报告/接口持有密钥。"""

    run_id: str
    started_at: str
    status: str = "running"
    stage: str = "queued"
    detail: str = ""
    report: dict[str, Any] | None = None
    error: str | None = None
    finished_at: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "detail": self.detail,
            "report": self.report,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_JOBS: dict[str, RunJob] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 50


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _set_stage(run_id: str, stage: str, detail: str = "") -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(run_id)
        if job is not None and job.status == "running":
            job.stage = stage
            job.detail = detail


def _finish(run_id: str, status: str, *, report: dict[str, Any] | None = None, error: str | None = None) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(run_id)
        if job is None:
            return
        job.status = status
        job.stage = "done" if status == "done" else "cancelled" if status == "cancelled" else "error"
        job.report = report
        job.error = error
        job.finished_at = _now_iso()


def get_run(run_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(run_id)
        return job.public() if job is not None else None


def cancel_run(run_id: str) -> dict[str, Any] | None:
    """请求取消。当前模型调用不能中断，工作线程在下一次检查点退出。"""
    with _JOBS_LOCK:
        job = _JOBS.get(run_id)
        if job is None:
            return None
        job.cancel.set()
        if job.status == "running":
            job.status = "cancelling"
        return job.public()


def start_run(
    *,
    project_key: str,
    requirement: str,
    seats: Mapping[str, SeatModel],
    output_root: Path,
    chat: Callable[..., tuple[str, dict[str, Any]]] | None = None,
) -> str:
    """启动后台直跑，立即返回 `run_id`。实际执行在 daemon 线程里。"""
    run_id = uuid.uuid4().hex[:12]
    job = RunJob(run_id=run_id, started_at=_now_iso())
    with _JOBS_LOCK:
        _JOBS[run_id] = job
        if len(_JOBS) > _MAX_JOBS:
            finished = [item for item in _JOBS.values() if item.status not in {"running", "cancelling"}]
            for item in sorted(finished, key=lambda value: value.started_at)[: len(_JOBS) - _MAX_JOBS]:
                _JOBS.pop(item.run_id, None)

    def worker() -> None:
        try:
            report = run_settings(
                project_key=project_key,
                requirement=requirement,
                seats=seats,
                output_root=output_root,
                run_id=run_id,
                chat=chat,
                on_stage=lambda stage, detail="": _set_stage(run_id, stage, detail),
                should_stop=job.cancel.is_set,
            )
            _finish(run_id, "done", report=report)
        except SettingsRunError as exc:
            status = "cancelled" if exc.code == "cancelled" else "error"
            _finish(run_id, status, error=f"{exc.stage}:{exc.code}: {exc.detail}")
        except BaseException as exc:  # noqa: BLE001 - 工作线程不能静默死；把类型与消息还给接口
            _finish(run_id, "error", error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, name=f"settings-run-{run_id}", daemon=True).start()
    return run_id



def resolve_seat(
    *,
    vendor: str,
    model: str,
    api_key: str,
    openai_compat_base_url: str = "",
    thinking: str = "medium",
) -> SeatModel:
    vendor = vendor.strip()
    model = model.strip()
    key = api_key.strip()
    level = thinking.strip().casefold()
    if vendor not in VENDOR_IDS:
        raise SettingsRunError("config", "unknown_vendor", f"不认识的厂商 {vendor!r}")
    vendor_id = VENDOR_IDS[vendor]
    if not model:
        raise SettingsRunError("config", "missing_model", f"{vendor} 没有选型号")
    if not key:
        raise SettingsRunError("config", "missing_key", f"{vendor} 没有 API Key")
    if level not in THINKING_LEVELS:
        raise SettingsRunError("config", "bad_thinking", f"思考强度 {thinking!r} 不在 {list(THINKING_LEVELS)}")
    if vendor_id == "openai-compat":
        base = openai_compat_base_url.strip().rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise SettingsRunError("config", "missing_base_url", "OpenAI 兼容端点没有可用的接入地址")
    else:
        base = BASE_URLS[vendor_id]
    return SeatModel(vendor=vendor, model=model, api_key=key, base_url=base, thinking=level)


#: 设置页「思考强度」的四档，与 `web/src/lib/thinking.ts` 的 `THINKING_LEVELS` 同值（跨文件对读判据钉）。
THINKING_LEVELS = ("off", "low", "medium", "high")
#: 三档"开"的推理预算（只对有预算旋钮的厂商有意义）。
THINKING_BUDGETS: Mapping[str, int] = {"low": 1024, "medium": 4096, "high": 8192}
#: 会被 `_thinking_fields` 写进请求体的扩展字段名 —— 400 时按这张表摘除重试。
THINKING_FIELD_KEYS = ("thinking", "enable_thinking", "thinking_budget")


def _thinking_fields(seat: SeatModel) -> dict[str, Any]:
    """把「思考强度」映射成各家**实测有效**的请求字段（2026-10-30，owner 的三把 key，每档两发）。

    - 豆包 ARK：`thinking={"type":"disabled"}` ⇒ 推理段 0 字 / `reasoning_tokens=0`（两发一致）；
      `enabled` 或不带 ⇒ 推理段 ~590 字。这是个开关，ARK 不给预算旋钮 ⇒ `low/medium/high` 同一形态。
    - 千问 dashscope：`enable_thinking=false` ⇒ 推理段 0 字；`true` + `thinking_budget` 是**真预算**
      （budget=128 那一发 `reasoning_tokens` 正好 128 封顶；给到 1024/8192 时模型自己想到 537/562 就收）。
    - DeepSeek：`thinking` / `enable_thinking` / `reasoning_effort` 三种写法都被 200 接受，
      但推理段始终 91—184 token、**行为不变** ⇒ 一个字段都不发。发一个不起作用的字段
      比不发更糟：它让页面上那列下拉看起来已经生效了。
    - kimi / 智谱 / MiniMax / OpenAI 兼容：本轮没有 key，未实测 ⇒ 不发。带 key 的人按同一口径补表，
      补之前先量"档位变了没有"，不要照抄别家字段名。
    """
    level = seat.thinking if seat.thinking in THINKING_LEVELS else "medium"
    vendor_id = seat.vendor_id
    if vendor_id == "doubao":
        return {"thinking": {"type": "disabled" if level == "off" else "enabled"}}
    if vendor_id == "qwen":
        if level == "off":
            return {"enable_thinking": False}
        return {"enable_thinking": True, "thinking_budget": THINKING_BUDGETS[level]}
    return {}


def _strip_unsupported_fields(body: dict[str, Any], text: str) -> list[str]:
    """400 里点名了某个思考字段 ⇒ 摘掉它重试一发，不把"端点不认这个字段"误报成模型失败。

    只摘错误文本真点到的那些键：`model` 写错也是 400，那时候不许顺手摘掉思考字段把真因盖掉。
    """
    lowered = text.casefold()
    dropped = [key for key in THINKING_FIELD_KEYS if key in body and key in lowered]
    for key in dropped:
        body.pop(key, None)
    return dropped


def _chat(
    seat: SeatModel,
    *,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float = 300.0,
) -> tuple[str, dict[str, Any]]:
    """一次 OpenAI 兼容 chat completions。返回 `(content, usage)`；失败消息不含密钥。

    `usage` 里带两个**不是厂商字段**的本模块读数：`finish_reason` 与 `reasoning_chars`。
    自适应预算那一发要看它们，而调用方与判据之间只有这个二元组可传（多返回值会打散
    `chat` 注入位的形状，那份形状被 `tests/contract/test_settings_run.py` 的假 chat 钉着）。
    """
    url = f"{seat.base_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
        "model": seat.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    body.update(_thinking_fields(seat))
    headers = {"Authorization": f"Bearer {seat.api_key}", "Content-Type": "application/json"}
    response: httpx.Response | None = None
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=body)
                if response.status_code == 400 and _strip_unsupported_fields(body, response.text):
                    response = client.post(url, headers=headers, json=body)
            break
        except httpx.TimeoutException as exc:
            # 超时**不重试**：`TimeoutException` 是 `TransportError` 的子类，这一支必须排在前面，
            # 否则 600s 那一发会被悄悄翻倍成一整单两倍的墙钟。
            raise SettingsRunError(
                "model", "timeout", f"{seat.vendor} 在 {timeout:.0f}s 内没有返回（{type(exc).__name__}）"
            ) from None
        except httpx.TransportError as exc:
            if attempt < TRANSPORT_RETRIES:
                backoff = TRANSPORT_BACKOFF_S[min(attempt, len(TRANSPORT_BACKOFF_S) - 1)]
                logger.warning(
                    "四席直跑 %s / %s 传输失败（%s）⇒ 等 %.0fs 后重打第 %d 发",
                    seat.vendor_id,
                    seat.model,
                    type(exc).__name__,
                    backoff,
                    attempt + 2,
                )
                time.sleep(backoff)
                continue
            raise SettingsRunError(
                "model",
                "transport",
                f"{seat.vendor} 传输失败（{type(exc).__name__}｜已试 {attempt + 1} 发，退避 "
                f"{'、'.join(f'{value:g}s' for value in TRANSPORT_BACKOFF_S[:attempt])}）",
            ) from None
    assert response is not None  # 循环要么 break 要么抛；这一行是给类型检查器的
    if response.status_code != 200:
        excerpt = redact_bus_text(response.text[:500])
        raise SettingsRunError(
            "model",
            f"http_{response.status_code}",
            f"{seat.vendor} 返回 HTTP {response.status_code}：{excerpt}",
        )
    try:
        payload = response.json()
        choice = payload["choices"][0]
        message = choice["message"]
        content = message["content"]
        usage = dict(payload.get("usage")) if isinstance(payload.get("usage"), dict) else {}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SettingsRunError("model", "bad_response", f"{seat.vendor} 响应形状不对（{type(exc).__name__}）") from None
    reasoning = message.get("reasoning_content")
    reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
    usage["finish_reason"] = str(choice.get("finish_reason") or "")
    usage["reasoning_chars"] = reasoning_chars
    # 第几发才连上：没有这个读数，"退避重试到底救没救回一发"在真实链上不可观察（2026-10-31 千问独跑就是这一格）。
    usage["transport_attempts"] = attempt + 1
    if not isinstance(content, str) or not content.strip():
        # 推理型型号会把预算花在 `reasoning_content` 上，正文留空。把这两个读数带进消息，
        # 否则"空内容"看起来像厂商挂了，而真因是这一发的预算不够（见 PREFLIGHT_MAX_TOKENS 那一段实测）。
        raise SettingsRunError(
            "model",
            "empty_response",
            f"{seat.vendor} 返回了空正文（推理段 {reasoning_chars} 字｜finish_reason={usage['finish_reason']}）"
            f"{' ⇒ 预算被推理吃光，不是型号不可用' if reasoning_chars else ''}",
        )
    return content, usage


def _preflight_probe(
    call: Callable[..., tuple[str, dict[str, Any]]], seat: SeatModel
) -> None:
    """连通性探针：慢响应/断流可以换一发重打，名字或密钥不行。"""
    attempts = PREFLIGHT_RETRIES + 1
    for index in range(attempts):
        try:
            call(
                seat,
                system="连接测试：只回复 ok，不要解释。",
                user="ping",
                max_tokens=PREFLIGHT_MAX_TOKENS,
                timeout=PREFLIGHT_TIMEOUT_S,
            )
            return
        except SettingsRunError as exc:
            if exc.code not in PREFLIGHT_RETRYABLE or index == attempts - 1:
                raise


def _call_within_budget(
    call: Callable[..., tuple[str, dict[str, Any]]],
    seat: SeatModel,
    *,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float = 300.0,
    retries: int = BUDGET_RETRIES,
    ceiling: int = BUDGET_CEILING,
) -> tuple[str, dict[str, Any], int]:
    """每一发的自适应预算：正文空 或 `finish_reason=length` ⇒ 翻倍再试一次。

    实测那一发（2026-10-30，DeepSeek 独跑）：审查给 4096，推理段 10931 字吃到 `length`、正文 0
    ⇒ 整单在 102.9s 死在 `model:empty_response`。第三个返回值 = 实际翻倍次数，
    它 MUST 进报告 —— 否则页面上写的预算和真发出去的预算是两个数，下一次归因又得从头量。
    """
    budget = max_tokens
    attempt = 0
    while True:
        try:
            text, usage = call(seat, system=system, user=user, max_tokens=budget, timeout=timeout)
        except SettingsRunError as exc:
            if exc.code == "empty_response" and attempt < retries and budget * 2 <= ceiling:
                budget *= 2
                attempt += 1
                continue
            raise
        if (
            str(usage.get("finish_reason") or "").casefold() == "length"
            and attempt < retries
            and budget * 2 <= ceiling
        ):
            budget *= 2
            attempt += 1
            continue
        return text, usage, attempt


def _json_object(stage: str, text: str) -> dict[str, Any]:
    """从模型回话里取一个 JSON 对象：整串 → 第一个 `{` 到最后一个 `}` → 逐个 `{` 取自身闭合的对象，
    三级依次试，全失败才报 `bad_json`。

    为什么要有第二发取法：2026-10-31 千问独跑在 317s（文件全生成、审查与修复都过了）死在
    `accept:bad_json`（`Expecting ',' delimiter`）—— 老取法只有"第一个 `{` 到最后一个 `}`"，
    验收席只要在对象后面多吐一个花括号或一段带引号的话，那一刀就切进对象里去了。
    现在多一道 `raw_decode` 扫描：从每个 `{` 起找一个**自身闭合**的对象，尾部的碎话直接丢掉。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    candidates: list[str] = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            raise SettingsRunError(stage, "bad_json", "模型回话的顶层不是对象")
        return value
    # 末级取法：前面两刀都切歪了才走这里。顶层是数组那一形态在上面就报了，不许被这一发救回来
    # —— 静默取数组里的第一个对象等于把"回话形状错了"改成"少了几条结论"，后者在页面上读不出来。
    decoder = json.JSONDecoder()
    for index in (position for position in range(len(cleaned)) if cleaned[position] == "{"):
        try:
            value, _consumed = decoder.raw_decode(cleaned[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise SettingsRunError(stage, "bad_json", "模型回话里没有可解析的 JSON 对象")


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def _safe_name(name: object) -> str | None:
    text = str(name or "").strip().replace("\\", "/")
    if not text or "/" in text or text.startswith("."):
        return None
    suffix = Path(text).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        return None
    # 同一份说明/预览脚本不许因为大小写各写一份（冒烟实测：readme.md + README.md 并存）。
    if text.lower() == "readme.md":
        return "README.md"
    if text.lower() == "serve.py":
        return "serve.py"
    return text


def _plan_prompt(requirement: str) -> str:
    return (
        f"客户需求：{requirement}\n\n"
        "请输出一个严格 JSON 对象，不要 Markdown 代码围栏，不要解释：\n"
        '{"title":"项目标题","pages":[{"file":"index.html","label":"首页","duty":"这一页的职责"}],'
        '"acceptance":["可验收的条目"],"files":[{"name":"index.html","purpose":"用途"}]}\n\n'
        "要求：至少 3 个页面；必须有 index.html 与 style.css；"
        "如果需求包含交互（购物车、表单、校验、弹窗、筛选等），必须有 app.js；"
        "如果需求包含本地预览，必须有 serve.py；"
        # 移动端那条措辞单独钉一句：模型默认会写成「无横向滚动条」，而那一条 `overflow-x:hidden`
        # 就能满足 —— 判据写成"藏得住"，产物就只会藏。运行期还有一道同名单句改写兜底。
        f"验收标准里凡是管移动端宽度的那一条，必须写成「{_OVERFLOW_TIGHT}」，"
        "不许写成「无横向滚动条」（把滚动条藏起来也算满足，那是假通过）；"
        "所有页面统一使用这些 data 属性：" + json.dumps(CONTRACT, ensure_ascii=False) + "；"
        "文件名只用小写字母、数字、点、下划线和连字符，不要目录；"
        # 白名单只有一项，而且是平台自己写死的那份说明文件：2026-10-31 那一发混席里，总管把这句
        # 绝对化的命名约束写成验收标准，审查员逐条对账时判我们生成的 `README.md` 违例 ⇒ 整单 REJECT。
        # 大写 README 是 GitHub 主页渲染的约定，不动它，动的是这句约束。
        "唯一例外是说明文件 `README.md`（大写由平台落盘，GitHub 靠它在仓库主页渲染），"
        "不许把它判成违例，也不许为它写命名判据。"
    )


#: 「无横向滚动条」这一族措辞的修补。三发真模型直跑（2026-10-30）里，统筹模型每次都把需求里的
#: "375px 下不横向滚动"写成「375px 视口下页面无横向滚动条」——而 `overflow-x:hidden` 恰好满足这句话：
#: 滚动条确实没了，内容是被裁掉还是真适配没人判。这里把它统一改写成"不被截断 + 无溢出"两条同时成立，
#: 审查席拿到的判据就不再是"藏得住"而是"装得下"。
_OVERFLOW_LOOSE = re.compile(r"横向滚动|滚动条")
_OVERFLOW_TIGHT = "在 375px 下内容不被截断且无横向溢出"


def _tighten_overflow_criterion(criterion: str) -> str:
    text = criterion.strip()
    if not text or _OVERFLOW_TIGHT in text:
        return text
    if _OVERFLOW_LOOSE.search(text):
        return _OVERFLOW_TIGHT
    return text


def _normalize_plan(plan: Mapping[str, Any], requirement: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    title = str(plan.get("title") or "设置直跑").strip()
    pages = [p for p in (plan.get("pages") or []) if isinstance(p, Mapping)]
    acceptance = [_tighten_overflow_criterion(str(x)) for x in (plan.get("acceptance") or []) if str(x).strip()]
    files: dict[str, str] = {}
    for item in plan.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(item.get("name"))
        if name:
            files[name] = str(item.get("purpose") or "").strip()
    for page in pages:
        name = _safe_name(page.get("file"))
        if name and name.endswith(".html"):
            files.setdefault(name, str(page.get("duty") or page.get("label") or "").strip())
    files.setdefault("index.html", "首页")
    files.setdefault("style.css", "全站样式")
    if any(word in requirement for word in INTERACTION_WORDS):
        files.setdefault("app.js", "交互脚本")
    if "预览" in requirement or "本地" in requirement:
        files.setdefault("serve.py", "本地预览脚本")
    if not acceptance:
        acceptance = ["页面能双击打开", _OVERFLOW_TIGHT, "主要链接可达"]
    ordered = [{"name": name, "purpose": files[name]} for name in files][:10]
    return {"title": title, "pages": pages, "acceptance": acceptance, "files": ordered}, ordered


def _readme(title: str, files: list[dict[str, str]]) -> str:
    body = "\n".join(f"- `{item['name']}` — {item['purpose'] or '产物'}" for item in files)
    preview = (
        "本地预览：在目录里执行 `python serve.py`，然后打开 http://127.0.0.1:8000/。"
        if any(item["name"] == "serve.py" for item in files)
        else "直接双击 `index.html` 预览。"
    )
    return (
        f"# {title}\n\n按设置页四席模型直跑生成的静态站。\n\n"
        f"## 文件\n\n{body}\n\n## 预览\n\n{preview}\n"
    )


def run_settings(
    *,
    project_key: str,
    requirement: str,
    seats: Mapping[str, SeatModel],
    output_root: Path,
    chat: Callable[..., tuple[str, dict[str, Any]]] | None = None,
    preflight: bool = True,
    run_id: str | None = None,
    on_stage: Callable[[str, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """跑完四席并写盘；返回可 JSON 序列化的报告。`chat` 是判据注入位。"""
    call = chat or _chat
    missing = [name for name in SEAT_NAMES if name not in seats]
    if missing:
        raise SettingsRunError("config", "missing_seat", f"缺少席位：{missing}")
    if not project_key.strip() or "/" in project_key or "\\" in project_key or project_key in {".", ".."}:
        raise SettingsRunError("config", "bad_project", "项目键必须是单层目录名")

    def stage(name: str, detail: str = "") -> None:
        if on_stage is not None:
            on_stage(name, detail)

    def check() -> None:
        if should_stop is not None and should_stop():
            raise SettingsRunError("cancelled", "cancelled", "用户取消")

    # 运行前先打一拍轻量探针：型号不存在/无权限（404）在规划之前就报出来，
    # 而不是等一张 80 秒的 CSS 生成完才发现整套配置不可用。
    if preflight:
        check()
        for name in SEAT_NAMES:
            stage("preflight", f"{name}:{seats[name].model}")
            seat = seats[name]
            try:
                _preflight_probe(call, seat)
            except SettingsRunError as exc:
                raise SettingsRunError(
                    "preflight",
                    f"model_unavailable:{name}",
                    f"{seat.vendor} / {seat.model} 不可用（{exc.code}）：{exc.detail}",
                ) from None

    run_id = run_id or uuid.uuid4().hex[:12]
    output_dir = output_root / project_key / f"settings-run-{run_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    timings: dict[str, float] = {}
    #: 哪一发是靠传输层重打救回来的（值 = 第几发才连上）。只记 >1 的，正常那一发不进这张表 ——
    #: 没有它，"退避重试到底有没有生效"在真实链上读不出来（2026-10-31 千问独跑就是这一格）。
    transport_retries: dict[str, int] = {}
    #: 每一发实际翻了几次倍（0 = 配置里的预算够用）。`review` / `repair` 恒在，其余阶段
    #: （`plan` / `accept` / `execute:<文件名>`）只在**真翻过**时才进这张表 —— 六份文件各记一个 0
    #: 会把"到底哪一发不够"淹掉。
    budget_doublings: dict[str, int] = {"review": 0, "repair": 0}
    models_used: dict[str, dict[str, str]] = {name: seat.public() for name, seat in seats.items()}

    def note_attempts(key: str, usage: Mapping[str, Any]) -> None:
        value = usage.get("transport_attempts")
        if isinstance(value, int) and value > 1:
            transport_retries[key] = value

    check()
    stage("plan", "总管规划")
    plan_started = time.monotonic()
    plan_reply, plan_usage, plan_doublings = _call_within_budget(
        call,
        seats["orchestrator"],
        system="你是项目总管。只输出严格 JSON，不要 Markdown 代码围栏，不要解释。",
        user=_plan_prompt(requirement),
        max_tokens=4096,
    )
    note_attempts("plan", plan_usage)
    if plan_doublings:
        budget_doublings["plan"] = plan_doublings
    plan, files = _normalize_plan(_json_object("plan", plan_reply), requirement)
    timings["orchestrator"] = round(time.monotonic() - plan_started, 3)

    generated: list[dict[str, Any]] = []
    for index, item in enumerate(files, start=1):
        name = item["name"]
        if name.lower() in {"readme.md", "serve.py"}:
            continue
        check()
        stage("execute", f"{index}/{len(files)} {name}")
        exec_started = time.monotonic()
        purpose = item["purpose"] or name
        reply, exec_usage, exec_doublings = _call_within_budget(
            call,
            seats["executor"],
            system=(
                "你是执行工程师。只输出目标文件的完整正文，不要 Markdown 代码围栏，不要解释。"
                "内容必须完整、可运行、可直接保存。"
            ),
            user=(
                f"客户需求：{requirement}\n\n项目标题：{plan['title']}\n"
                f"文件清单：{json.dumps(files, ensure_ascii=False)}\n"
                f"跨文件契约（必须原样使用这些 data 属性）：{json.dumps(CONTRACT, ensure_ascii=False)}\n\n"
                f"现在只写 `{name}`（用途：{purpose}）。"
                "如果是 HTML，必须引用 style.css；有 app.js 时也必须引用它。不要省略任何代码。"
            ),
            max_tokens=8192,
            timeout=600.0,
        )
        if exec_doublings:
            budget_doublings[f"execute:{name}"] = exec_doublings
        body = _strip_fence(reply)
        if not body.strip():
            raise SettingsRunError("execute", "empty_file", f"{name} 生成结果为空")
        # 2026-10-31 四发真模型矩阵：预算翻倍用尽后仍被截断的回复会被原样写盘，job 还报 `done`
        # （实测三发里四份文件 `node --check` 不过）。落盘前用门禁同一份判据挡一次，
        # 让这一发以 `execute:truncated_file` 失败 —— 失败看得见，半份文件不留。
        incomplete = artifact_completeness(Path(name).suffix.lstrip(".").lower(), body)
        if incomplete:
            raise SettingsRunError("execute", "truncated_file", f"{name} {incomplete}")
        (output_dir / name).write_bytes(body.encode("utf-8"))
        generated.append(
            {
                "name": name,
                "bytes": len(body.encode("utf-8")),
                "lines": len(body.splitlines()),
                "purpose": purpose,
            }
        )
        timings[f"execute:{name}"] = round(time.monotonic() - exec_started, 3)
        note_attempts(f"execute:{name}", exec_usage)

    if any(item["name"] == "serve.py" for item in files):
        (output_dir / "serve.py").write_bytes(PREVIEW_SCRIPT.encode("utf-8"))
        generated.append(
            {
                "name": "serve.py",
                "bytes": len(PREVIEW_SCRIPT.encode("utf-8")),
                "lines": len(PREVIEW_SCRIPT.splitlines()),
                "purpose": "本地预览脚本",
            }
        )

    readme = _readme(plan["title"], files)
    (output_dir / "README.md").write_bytes(readme.encode("utf-8"))
    generated.append({"name": "README.md", "bytes": len(readme.encode("utf-8")), "lines": len(readme.splitlines()), "purpose": "使用说明"})

    def build_manifest() -> list[dict[str, Any]]:
        manifest = []
        budget = 80_000
        for item in generated:
            path = output_dir / item["name"]
            text = path.read_text(encoding="utf-8", errors="replace")
            checks: dict[str, bool] = {}
            if item["name"].lower().endswith(".html"):
                checks = {
                    "has_style_link": "style.css" in text,
                    "has_app_script": "app.js" in text,
                    "has_cart_badge": "data-cart-badge" in text,
                    "has_add_to_cart": "data-add-to-cart" in text,
                    "has_sort_select": "data-sort-select" in text,
                    "has_product_list": "data-product-list" in text,
                    "has_contact_form": "data-contact-form" in text,
                }
            if len(text) <= budget:
                content = text
                budget -= len(text)
            else:
                # 只裁超预算的大文件；头尾都留，页尾的 script/form 不会被截掉。
                content = text[:4000] + "\n…[内容过长，中段省略]…\n" + text[-1500:]
                budget = 0
            manifest.append({**item, "content": content, "checks": checks})
        return manifest

    def review_once() -> dict[str, Any]:
        check()
        stage("review", "独立审查")
        review_started = time.monotonic()
        review_reply, review_usage, doublings = _call_within_budget(
            call,
            seats["reviewer"],
            system=(
                "你是独立审查员。只输出严格 JSON，不要 Markdown 代码围栏，不要解释。"
                "`checks` 是对应文件原文的结构事实（true = 原文里确实出现），优先于你的猜测；"
                "只按给定的 acceptance 判定，不要把 acceptance 之外的新要求当成 FAIL 理由。"
            ),
            user=(
                f"客户需求：{requirement}\n\n验收标准：{json.dumps(plan['acceptance'], ensure_ascii=False)}\n\n"
                f"文件清单与原文：{json.dumps(build_manifest(), ensure_ascii=False)}\n\n"
                '请逐条判断并输出：{"verdict":"PASS 或 FAIL","findings":[{"criterion":"...","met":true,"evidence":"..."}]}'
            ),
            max_tokens=4096,
        )
        budget_doublings["review"] = max(budget_doublings["review"], doublings)
        note_attempts("review", review_usage)
        result = _json_object("review", review_reply)
        timings["reviewer"] = round(time.monotonic() - review_started, 3)
        return result

    review = review_once()
    repair_attempts = 0
    repair_error: str | None = None
    if str(review.get("verdict") or "").upper() == "FAIL":
        # 只修一轮：执行席按审查 findings 改已有文件，再复评一次；修不动不伪造 PASS。
        check()
        stage("repair", "按审查意见修复")
        repair_attempts = 1
        repair_started = time.monotonic()
        try:
            repair_reply, repair_usage, repair_doublings = _call_within_budget(
                call,
                seats["executor"],
                system=(
                    "你是修复工程师。只输出严格 JSON，不要 Markdown 代码围栏，不要解释。"
                    "只返回需要修改的已有文件，键名 name/content。"
                ),
                user=(
                    f"客户需求：{requirement}\n\n规划：{json.dumps(plan, ensure_ascii=False)}\n\n"
                    f"审查 findings：{json.dumps(review.get('findings') or [], ensure_ascii=False)}\n\n"
                    f"当前文件：{json.dumps(build_manifest(), ensure_ascii=False)}\n\n"
                    '请输出：{"files":[{"name":"style.css","content":"完整文件正文"}]}'
                    " 只改真正有问题的文件；保留已正确的结构与文案。"
                ),
                max_tokens=8192,
                timeout=600.0,
            )
            budget_doublings["repair"] = max(budget_doublings["repair"], repair_doublings)
            note_attempts("repair", repair_usage)
            patch = _json_object("repair", repair_reply)
            existing = {item["name"] for item in generated}
            # 先整体校验、再统一落盘：补丁写到一半才撞截断，会留下一份"修了一半"的目录 ——
            # 那比整批不修更难查（哪几份是新的、哪几份是旧的，报告里读不出来）。
            patches: list[tuple[str, str]] = []
            for item in patch.get("files") or []:
                if not isinstance(item, Mapping):
                    continue
                name = _safe_name(item.get("name"))
                if name is None or name not in existing:
                    continue
                body = str(item.get("content") or "")
                if not body.strip():
                    continue
                incomplete = artifact_completeness(Path(name).suffix.lstrip(".").lower(), body)
                if incomplete:
                    raise SettingsRunError("repair", "truncated_file", f"{name} {incomplete}")
                patches.append((name, body))
            for name, body in patches:
                (output_dir / name).write_bytes(body.encode("utf-8"))
                for record in generated:
                    if record["name"] == name:
                        record["bytes"] = len(body.encode("utf-8"))
                        record["lines"] = len(body.splitlines())
            review = review_once()
        except SettingsRunError as exc:
            repair_error = f"{exc.stage}:{exc.code}: {exc.detail}"
        timings["repair"] = round(time.monotonic() - repair_started, 3)

    verdict = str(review.get("verdict") or "").upper()
    if verdict not in {"PASS", "FAIL"}:
        raise SettingsRunError("review", "bad_verdict", f"审查结论不在 PASS/FAIL：{verdict!r}")

    check()
    stage("accept", "验收决定")
    accept_started = time.monotonic()
    accept_reply, accept_usage, accept_doublings = _call_within_budget(
        call,
        seats["acceptor"],
        system="你是验收员。只输出严格 JSON，不要 Markdown 代码围栏，不要解释。",
        user=(
            f"客户需求：{requirement}\n\n审查结论：{json.dumps(review, ensure_ascii=False)}\n\n"
            '请给出最终决定：{"decision":"ACCEPT 或 REJECT","reason":"一句话理由"}'
        ),
        max_tokens=1024,
    )
    note_attempts("accept", accept_usage)
    if accept_doublings:
        budget_doublings["accept"] = accept_doublings
    acceptance = _json_object("accept", accept_reply)
    decision = str(acceptance.get("decision") or "").upper()
    if decision not in {"ACCEPT", "REJECT"}:
        raise SettingsRunError("accept", "bad_decision", f"验收决定不在 ACCEPT/REJECT：{decision!r}")
    timings["acceptor"] = round(time.monotonic() - accept_started, 3)

    report = {
        "run_id": run_id,
        "project_key": project_key,
        "requirement": requirement,
        "output_dir": output_dir.as_posix(),
        "models": models_used,
        "plan": plan,
        "files": generated,
        "review": review,
        "repair_attempts": repair_attempts,
        "repair_error": repair_error,
        "budget_doublings": budget_doublings,
        "transport_retries": transport_retries,
        "acceptance": acceptance,
        "timings_s": timings,
        "total_s": round(time.monotonic() - started, 3),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (output_dir / "run-report.json").write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    stage("done", "报告已写")
    return report
