"""路线 B 的词汇与请求体（2026-10-31）：`model_routing` 的两支执行者、把关偏好、形状校验。

纯函数一档，不连网不连库。这里钉的是**形状与拒绝面**；"选人/把关真的按它走"在
`test_orchestrate_graph.py`（PG）与 `test_request_intake.py`（PG）里各钉一半。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from acp.api.requests import IntakeRequest, ModelRoutingBody, _routing_payload
from acp import routing


def test_executor_has_exactly_two_kinds_and_a_missing_default():
    assert routing.executor_agent({"executor": {"kind": "qoder"}}) == "qoder"
    assert routing.executor_agent({"executor": {"kind": "api", "vendor": "v", "model": "m"}}) == "doubao", (
        "纯 API 执行席复用已有 API 形态 agent（不新增注册条目，见 routing.py 的说明）"
    )
    assert routing.executor_agent(None) is None
    assert routing.executor_agent({}) is None
    assert routing.executor_agent({"executor": {"kind": "openai"}}) is None, "不认识的 kind 不许猜成某一支"


def test_executor_target_and_thinking():
    body = {
        "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash", "thinking": "high"}
    }
    assert routing.executor_target(body) == ("深度求索 DeepSeek", "deepseek-flash")
    assert routing.thinking_of(body) == "high"
    assert routing.thinking_of({"executor": {"kind": "api"}}) == "medium", "缺省与设置页四席同值"
    assert routing.executor_target({"executor": {"kind": "api", "vendor": "v"}}) is None, "缺一即不算点名"


def test_seat_preference_only_reads_the_agent_slot():
    body = {"reviewer": {"agent_id": "doubao"}, "acceptor": {"vendor": "x"}}
    assert routing.seat_preference(body, "reviewer") == "doubao"
    assert routing.seat_preference(body, "acceptor") is None, "把关席只认 agent_id，不认 vendor/model"
    assert routing.seat_preference(None, "reviewer") is None


@pytest.mark.parametrize(
    "body",
    [
        {"executor": {"kind": "nope"}},
        {"executor": {"kind": "qoder", "surprise": "x"}},
        {"surprise": {}},
    ],
    ids=["bad-kind", "extra-key", "unknown-role"],
)
def test_bad_routing_bodies_are_refused(body):
    """`extra=forbid` 仍然生效：未知键在入口就 422（key 是已知字段，见下一条）。"""
    with pytest.raises(ValidationError):
        IntakeRequest(project_key="p", raw_input="r", model_routing=body)


def test_a_seat_must_name_an_agent_or_a_model_instance():
    """关席二选一：`agent_id` 或 `vendor+model`；两者都空 / 同时给 ⇒ 422。"""
    for body in ({"reviewer": {"agent_id": ""}}, {"reviewer": {"agent_id": "qoder", "vendor": "x", "model": "y"}}):
        payload = IntakeRequest(project_key="p", raw_input="r", model_routing=body)
        with pytest.raises(HTTPException) as caught:
            _routing_payload(payload.model_routing)
        assert caught.value.status_code == 422


def test_api_keys_travel_in_the_body_but_never_into_the_stored_routing():
    """路线 C：key 随请求体进内存仓；落库那份只有 `key_fp`（短指纹），没有 key。"""
    body = IntakeRequest(
        project_key="p",
        raw_input="r",
        model_routing={
            "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash", "api_key": "sk-test-123456"},
            "reviewer": {"vendor": "阿里云通义千问", "model": "qwen3.8-flash", "api_key": "sk-test-123456"},
        },
    )
    stored, keys = _routing_payload(body.model_routing)
    assert stored is not None
    assert "api_key" not in stored["executor"] and "api_key" not in stored["reviewer"]
    assert stored["executor"]["key_fp"] == stored["reviewer"]["key_fp"], "同一把 key 的指纹必须相同（互斥靠它）"
    assert keys == {"executor": "sk-test-123456", "reviewer": "sk-test-123456"}


def test_an_api_executor_must_name_vendor_and_model():
    with pytest.raises(HTTPException) as caught:
        _routing_payload(ModelRoutingBody(executor={"kind": "api", "vendor": "", "model": ""}))
    assert caught.value.status_code == 422
    assert "model_routing.executor" in caught.value.detail


def test_a_qoder_route_needs_nothing_else():
    """反向对照：`kind=qoder` 不要求 vendor/model —— 老路径的请求不该被新校验误伤。"""
    stored, keys = _routing_payload(ModelRoutingBody(executor={"kind": "qoder"}))
    assert stored is not None and stored["executor"]["kind"] == "qoder" and keys == {}
