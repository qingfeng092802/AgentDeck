"""路线 C：任意模型担任审查 / 验收（2026-10-31）。

纯函数一档：传输层 `settings_run._chat` 被替换成脚本化回复，钉的是**提示词契约、JSON 容错、
重试与 NEED_HUMAN 兜底**。真机验证归 owner 的终端那一次。
"""

from __future__ import annotations

import json

import pytest

from acp import settings_run
from acp.accept import routing_client as accept_client
from acp.adapters.deepseek import review as deepseek_review
from acp.review import routing_client as review_client


def _seat() -> settings_run.SeatModel:
    return settings_run.SeatModel(
        vendor="自定义厂商", model="model-x", api_key="k-test", base_url="https://example.test/v1"
    )


def _review_input() -> deepseek_review.ReviewInput:
    return deepseek_review.ReviewInput(
        task_id="T01",
        title="卡 T01",
        raw_input="做个页面",
        acceptance_criteria=("页面可打开",),
        artifacts=(
            deepseek_review.ReviewArtifact(
                rel_path="doubao-workspace/index.html",
                filename="index.html",
                format="html",
                content="<html><body>ok</body></html>",
            ),
        ),
    )


def _scripted(monkeypatch, replies: list[object]) -> list[dict]:
    calls: list[dict] = []

    def fake_chat(seat, *, system, user, max_tokens, timeout=None, **kwargs):
        calls.append({"system": system, "user": user, "model": seat.model})
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    monkeypatch.setattr(settings_run, "_chat", fake_chat)
    return calls


def _review_json(decision: str = "PASS") -> str:
    return json.dumps(
        {
            "decision": decision,
            "criteria": [{"criterion": "页面可打开", "met": True, "evidence": "<body>ok"}],
            "issues": [],
        },
        ensure_ascii=False,
    )


def test_any_model_can_review_with_the_shared_prompt(monkeypatch):
    calls = _scripted(monkeypatch, [_review_json()])
    result = review_client.RoutingReviewClient(_seat())(_review_input())
    assert result.decision == deepseek_review.PASS
    assert calls[0]["system"] == deepseek_review.SYSTEM_PROMPT, "任意模型都必须用同一份审查提示词"
    assert "filename" in calls[0]["system"], "甲方案那句路径说明必须在提示词里"


def test_a_reviewer_that_never_returns_json_falls_back_to_need_human(monkeypatch):
    _scripted(monkeypatch, ["不是 JSON", "还是不是"])
    result = review_client.RoutingReviewClient(_seat())(_review_input())
    assert result.decision == deepseek_review.NEED_HUMAN
    assert result.reason == "contract_violation"
    assert all(finding.met is None for finding in result.findings)


def test_a_transport_failure_degrades_without_claiming_a_verdict(monkeypatch):
    _scripted(monkeypatch, [settings_run.SettingsRunError("model", "transport", "remote protocol error")])
    result = review_client.RoutingReviewClient(_seat())(_review_input())
    assert result.decision == deepseek_review.NEED_HUMAN
    assert result.reason == "transport_error"


def test_accept_retries_twice_then_accepts(monkeypatch):
    payload = json.dumps({"decision": "ACCEPT", "reason": "可用"})
    calls = _scripted(monkeypatch, ["坏格式", "还是坏", payload])
    decision, reason = accept_client.decide(
        _seat(),
        requirement="做页面",
        criteria=("页面可打开",),
        artifacts=[{"filename": "index.html", "bytes": 40, "lines": 1, "content": "<html></html>"}],
        evidence="产物已通过门禁（valid）",
    )
    assert (decision, reason) == ("ACCEPT", "可用")
    assert len(calls) == 3, "首次 + 最多 2 次重试"


def test_accept_falls_back_to_need_human_after_three_bad_replies(monkeypatch):
    calls = _scripted(monkeypatch, ["x", "y", "z"])
    decision, reason = accept_client.decide(
        _seat(),
        requirement="做页面",
        criteria=("页面可打开",),
        artifacts=[{"filename": "index.html", "bytes": 40, "lines": 1, "content": "<html></html>"}],
        evidence="产物已通过门禁（valid）",
    )
    assert decision == "NEED_HUMAN" and "已试 3 发" in reason
    assert len(calls) == 3


def test_accept_refuses_a_decision_outside_the_two_value_domain(monkeypatch):
    _scripted(monkeypatch, [json.dumps({"decision": "MAYBE", "reason": "?"})])
    decision, _reason = accept_client.decide(
        _seat(),
        requirement="做页面",
        criteria=("页面可打开",),
        artifacts=[{"filename": "index.html", "bytes": 40, "lines": 1, "content": "<html></html>"}],
        evidence="产物已通过门禁（valid）",
    )
    assert decision == "NEED_HUMAN"
