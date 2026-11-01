"""通用审查 Runner 的模型客户端（路线 C，2026-10-31）。

**为什么不是再写一套提示词与解析**：审查的可见面、判定规则与 JSON 契约已经在
`adapters/deepseek/review.py` 里定稿（POC + 甲方案）。这里只换**传输**：任何被点名的模型
都走 `settings_run._chat`（思考字段、传输退避、预算翻倍、错误消息白拿），提示词与解析原样复用。
这样"谁来做审查"不再绑定某一家。

失败口径与 `deepseek` 那一支一致：凭据形态命中 / 可见面过大 / 两次都拿不到合规 JSON ⇒ `NEED_HUMAN`，
消息里只出现原因，不把模型的原始错误当结论。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping

from acp import settings_run
from acp.adapters.deepseek import review as deepseek_review

MAX_ATTEMPTS = 2
MAX_TOKENS = 4096
TIMEOUT_S = 300.0


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RoutingReviewClient:
    """`ReviewInput -> ReviewResult`：任意 OpenAI 兼容模型都能做审查席。"""

    def __init__(self, seat: settings_run.SeatModel, *, clock: Callable[[], dt.datetime] = _utcnow) -> None:
        self.seat = seat
        self.clock = clock

    def __call__(self, request: deepseek_review.ReviewInput) -> deepseek_review.ReviewResult:
        started = self.clock()
        payload = request.payload()
        user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        prompt_sha = hashlib.sha256(
            (deepseek_review.SYSTEM_PROMPT + user).encode("utf-8")
        ).hexdigest()
        if request.credential_suspect():
            return self._degrade(request, "credential_suspect", "产物或需求里出现凭据形态，按红线不发送模型", prompt_sha)
        if request.visible_bytes() > deepseek_review.MAX_PROMPT_BYTES:
            return self._degrade(request, "prompt_too_large", "可见面超过上限", prompt_sha)

        last_detail = "没有拿到可解析的模型响应"
        for attempt in (1, MAX_ATTEMPTS):
            try:
                content, usage = settings_run._chat(
                    self.seat,
                    system=deepseek_review.SYSTEM_PROMPT,
                    user=user,
                    max_tokens=MAX_TOKENS,
                    timeout=TIMEOUT_S,
                )
            except settings_run.SettingsRunError as exc:
                return self._degrade(request, "transport_error", f"{exc.stage}:{exc.code}: {exc.detail}"[:400], prompt_sha)
            try:
                parsed = deepseek_review._parse_model_output(content)
                decision, findings, issues, reason, detail = deepseek_review._classify(
                    parsed, request.acceptance_criteria
                )
            except deepseek_review._ContractViolation as exc:
                last_detail = str(exc)
                continue
            return deepseek_review.ReviewResult(
                decision=decision,
                findings=findings,
                issues=issues,
                reason=reason,
                reason_detail=detail,
                model_decision=parsed.decision,
                model_id=self.seat.model,
                attempts=attempt,
                latency_ms=int((self.clock() - started).total_seconds() * 1000),
                prompt_sha256=prompt_sha,
                raw_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                raw_chars=len(content),
                prompt_tokens=_int(usage.get("prompt_tokens")),
                completion_tokens=_int(usage.get("completion_tokens")),
                total_tokens=_int(usage.get("total_tokens")),
            )
        return self._degrade(request, "contract_violation", last_detail[:300], prompt_sha)

    def _degrade(
        self,
        request: deepseek_review.ReviewInput,
        reason: str,
        detail: str,
        prompt_sha: str,
    ) -> deepseek_review.ReviewResult:
        findings = tuple(
            deepseek_review.Finding(item, None, detail) for item in request.acceptance_criteria
        )
        return deepseek_review.ReviewResult(
            decision=deepseek_review.NEED_HUMAN,
            findings=deepseek_review.ensure_uncertain(list(findings), detail),
            reason=reason,
            reason_detail=detail,
            model_id=self.seat.model,
            prompt_sha256=prompt_sha,
        )


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def seat_for(
    routing: Mapping[str, object] | None,
    seat_name: str,
    *,
    request_id: object,
    vault_key_for: Callable[[object, str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> settings_run.SeatModel:
    """把路由里的模型席位翻成 `SeatModel`。key 先看内存仓，再回落到该厂商的环境变量。"""
    from acp import routing as routing_lib
    from acp.config.loader import env_value

    target = routing_lib.seat_model(routing, seat_name)
    if target is None:
        raise ValueError(f"routing.{seat_name} 不是模型席位 ⇒ 不构造通用客户端")
    vendor, model, base_url, _key_fp = target
    key = (vault_key_for(request_id, seat_name) if vault_key_for is not None else None) or ""
    if not key:
        env_name = routing_lib.vendor_env(vendor)
        if env_name is None:
            raise ValueError(f"自定义厂商 {vendor!r} 没有 key ⇒ 提交时必须随 body 带 api_key（不猜环境变量）")
        key = env_value(env_name, env)
    resolved_base = base_url
    if not resolved_base:
        vendor_id = settings_run.VENDOR_IDS.get(vendor)
        if vendor_id is None:
            raise ValueError(f"自定义厂商 {vendor!r} 没给 base_url ⇒ 不猜端点（提交时就该 422）")
        resolved_base = settings_run.BASE_URLS[vendor_id]
    return settings_run.SeatModel(
        vendor=vendor,
        model=model,
        api_key=key,
        base_url=resolved_base,
        thinking=routing_lib.seat_thinking(routing, seat_name),
    )
