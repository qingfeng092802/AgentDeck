"""Phase 17 P2：DeepSeek 审查客户端的外部契约（零真实出网、零凭据值）。

三组判据各堵一种"看起来接到了模型"的假绿：

1. **输入面**：`rawInput / acceptanceCriteria / artifactContent` 缺一不可。少一项就在构造期拒，
   不是"发出去让模型猜"——P2 的 `ReviewRunner` 会把构造失败折成 `NEED_HUMAN`，绝不放行。
2. **结构化输出**：坏 JSON、多一个顶层键、模型自述 PASS 却有一条 met=false，都 MUST NOT 变成 PASS。
   解析失败重试一次；第二次仍坏 ⇒ `NEED_HUMAN`。这条是 owner 点名的"第二次失败降级 NEED_HUMAN"。
3. **可见面**：POC 的 system prompt 逐字复用；user JSON 保持 POC 的
   `task_id/title/criteria/artifacts` 形状，并带上 owner 要求的 `raw_input`；
   请求体不发 `max_tokens` / `response_format`。

**本文件证明不到什么**：它不证明真实 DeepSeek 端点可用（P0 已经证过），也不证明
`ReviewRunner` 会把结果写进 EN-08（那在 `tests/integration/test_review_runner.py`）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from acp.adapters.deepseek import review as dr


def _input(**over: object) -> dr.ReviewInput:
    values: dict[str, object] = {
        "task_id": "T01",
        "title": "卡 T01",
        "raw_input": "做一个待办清单前端",
        "acceptance_criteria": ("页面有输入框", "刷新后列表仍在"),
        "artifacts": (
            dr.ReviewArtifact(
                rel_path="qoder-workspace/index.html",
                filename="index.html",
                format="html",
                content="<input id='new-item'><button id='add'>新增</button>",
            ),
        ),
    }
    values.update(over)
    return dr.ReviewInput(**values)  # type: ignore[arg-type]


def _payload(
    decision: str = "PASS",
    *,
    criteria: list[dict[str, object]] | None = None,
    issues: list[dict[str, object]] | None = None,
    uncertainty_reason: str | None = None,
) -> str:
    body: dict[str, object] = {
        "decision": decision,
        "criteria": criteria
        if criteria is not None
        else [
            {"criterion": "页面有输入框", "met": True, "evidence": "<input id='new-item'>"},
            {"criterion": "刷新后列表仍在", "met": True, "evidence": "localStorage.setItem"},
        ],
        "issues": issues if issues is not None else [],
    }
    if uncertainty_reason is not None:
        body["uncertainty_reason"] = uncertainty_reason
    return json.dumps(body, ensure_ascii=False)


def _completion(content: str, *, status: int = 200, model: str = "deepseek-flash") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def _client(responses: list[httpx.Response], calls: list[httpx.Request], *, api_key: str = "unit-test-placeholder"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    return dr.DeepSeekReviewClient(
        api_key=api_key,
        transport=httpx.MockTransport(handler),
        clock=lambda: 0.0,
    )


# ---------------------------------------------------------------- 一、输入面


def test_missing_raw_input_is_rejected_before_any_request():
    with pytest.raises(dr.ReviewInputError, match="raw_input"):
        _input(raw_input="   ")


def test_missing_acceptance_criteria_is_rejected_before_any_request():
    with pytest.raises(dr.ReviewInputError, match="acceptance_criteria"):
        _input(acceptance_criteria=())


def test_missing_artifact_content_is_rejected_before_any_request():
    with pytest.raises(dr.ReviewInputError, match="artifacts"):
        _input(artifacts=())
    with pytest.raises(dr.ReviewInputError, match="content"):
        _input(
            artifacts=(
                dr.ReviewArtifact(
                    rel_path="qoder-workspace/index.html",
                    filename="index.html",
                    format="html",
                    content="  ",
                ),
            )
        )


def test_filename_must_be_the_last_segment_of_rel_path():
    """两个名字字段必须同源：不一致时拒在构造期，不给模型两个互相矛盾的文件名。"""
    with pytest.raises(dr.ReviewInputError, match="不一致"):
        dr.ReviewArtifact(
            rel_path="qoder-workspace/index.html",
            filename="styles.css",
            format="html",
            content="<html></html>",
        )
    with pytest.raises(dr.ReviewInputError, match="filename"):
        dr.ReviewArtifact(
            rel_path="qoder-workspace/index.html",
            filename="  ",
            format="html",
            content="<html></html>",
        )


def test_missing_api_key_reports_the_variable_name_not_a_value():
    with pytest.raises(dr.ReviewConfigError) as exc:
        dr.DeepSeekReviewClient.from_env(env={})
    assert dr.API_KEY_ENV in str(exc.value)
    assert "unit-test-placeholder" not in str(exc.value)


# ---------------------------------------------------------------- 二、POC 可见面


def test_the_system_prompt_is_the_poc_prompt_plus_the_path_clause():
    """POC 原文 + **一段工作区路径说明**（owner 2026-10-31 裁甲方案）。

    为什么必须加这一句：真机那一发里审查员把 `doubao-workspace/styles.css` 判成"文件名不匹配
    卡上的 styles.css" ⇒ 整张卡 CHANGES_REQUESTED。加句之后"文件名以 filename 为准"是明文契约。
    """
    assert len(dr.SYSTEM_PROMPT) == 876
    assert dr.SYSTEM_PROMPT.startswith("你是独立代码审查员。")
    assert dr.SYSTEM_PROMPT.endswith("不得编造。\n")
    assert "只输出一个 JSON 对象" in dr.SYSTEM_PROMPT
    assert "`rel_path` 首段是工作区目录" in dr.SYSTEM_PROMPT
    assert "以 `filename` 字段为准" in dr.SYSTEM_PROMPT


def test_the_user_json_keeps_the_poc_shape_and_carries_raw_input():
    calls: list[httpx.Request] = []
    client = _client([_completion(_payload())], calls)
    result = client.review(_input())

    assert result.decision == dr.PASS
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["model"] == dr.REQUEST_MODEL == "deepseek-chat"
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert "max_tokens" not in body
    assert "response_format" not in body
    assert body["messages"][0] == {"role": "system", "content": dr.SYSTEM_PROMPT}
    user = json.loads(body["messages"][1]["content"])
    assert set(user) == {"task_id", "title", "raw_input", "criteria", "artifacts"}
    assert user["raw_input"] == "做一个待办清单前端"
    assert user["criteria"] == ["页面有输入框", "刷新后列表仍在"]
    assert user["artifacts"] == [
        {
            "rel_path": "qoder-workspace/index.html",
            "filename": "index.html",
            "format": "html",
            "content": "<input id='new-item'><button id='add'>新增</button>",
        }
    ]
    assert calls[0].headers["authorization"].startswith("Bearer ")


# ---------------------------------------------------------------- 三、结构化输出


def test_bad_json_is_retried_once_then_degrades_to_need_human():
    calls: list[httpx.Request] = []
    client = _client([_completion("这不是 JSON"), _completion("还是不是 JSON")], calls)
    result = client.review(_input())

    assert len(calls) == 2, "解析失败必须重试一次，而不是一次就放弃或一次就放行"
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "contract_violation"
    assert result.attempts == 2
    assert result.decision != dr.PASS, "第二次失败 MUST NOT 静默 PASS"
    assert {finding.criterion for finding in result.findings} == {
        "页面有输入框",
        "刷新后列表仍在",
    }
    assert all(finding.met is None for finding in result.findings)


def test_a_bad_first_response_can_recover_on_the_second_attempt():
    calls: list[httpx.Request] = []
    client = _client([_completion("坏"), _completion(_payload())], calls)
    result = client.review(_input())

    assert len(calls) == 2
    assert result.decision == dr.PASS
    assert result.attempts == 2


def test_model_self_reported_pass_with_a_failed_criterion_is_never_pass():
    calls: list[httpx.Request] = []
    content = _payload(
        "PASS",
        criteria=[
            {"criterion": "页面有输入框", "met": True, "evidence": "有"},
            {"criterion": "刷新后列表仍在", "met": False, "evidence": "没有 localStorage"},
        ],
    )
    client = _client([_completion(content), _completion(content)], calls)
    result = client.review(_input())

    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "contract_violation"
    assert result.decision != dr.PASS


def test_model_self_reported_fail_with_all_true_is_never_fail():
    calls: list[httpx.Request] = []
    content = _payload("FAIL")
    client = _client([_completion(content), _completion(content)], calls)
    result = client.review(_input())

    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "contract_violation"


def test_a_missing_criterion_is_a_contract_violation_unless_the_model_says_need_human():
    calls: list[httpx.Request] = []
    missing = _payload(
        "PASS",
        criteria=[{"criterion": "页面有输入框", "met": True, "evidence": "有"}],
    )
    client = _client([_completion(missing), _completion(missing)], calls)
    result = client.review(_input())
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "contract_violation"

    uncertain = _payload(
        "NEED_HUMAN",
        criteria=[{"criterion": "页面有输入框", "met": True, "evidence": "有"}],
        uncertainty_reason="第二条标准缺少可验证依据",
    )
    calls.clear()
    client = _client([_completion(uncertain)], calls)
    result = client.review(_input())
    assert result.decision == dr.NEED_HUMAN
    assert result.findings[1].criterion == "刷新后列表仍在"
    assert result.findings[1].met is None


def test_an_explicit_need_human_preserves_null_and_reason():
    calls: list[httpx.Request] = []
    content = _payload(
        "NEED_HUMAN",
        criteria=[
            {"criterion": "页面有输入框", "met": True, "evidence": "有"},
            {"criterion": "刷新后列表仍在", "met": None, "evidence": "看不到 localStorage"},
        ],
        uncertainty_reason="第二条标准在产物里找不到可验证依据",
    )
    client = _client([_completion(content)], calls)
    result = client.review(_input())

    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "model_uncertain"
    assert result.findings[1].met is None
    assert "第二条" in result.reason_detail


def test_need_human_with_all_true_criteria_is_not_left_as_an_approve_shape():
    """`record_verdict` 从 findings 推结论：NEED_HUMAN 若全是 true，会被推成 APPROVE。

    这是"第二次失败降级 NEED_HUMAN"之外的第二道：模型可能对**整体**没把握但逐条都写了 true。
    平台不采纳那批判定 ⇒ 全部折成 `None`，而不是把"不确定"伪装成"全过"。
    """
    calls: list[httpx.Request] = []
    content = _payload("NEED_HUMAN", uncertainty_reason="整体无法确认，但不指向单条标准")
    client = _client([_completion(content)], calls)
    result = client.review(_input())

    assert result.decision == dr.NEED_HUMAN
    assert all(finding.met is None for finding in result.findings), result.findings
    assert "未采纳" in result.findings[0].evidence


def test_a_json_fence_around_one_object_is_accepted():
    calls: list[httpx.Request] = []
    content = "```json\n" + _payload() + "\n```"
    client = _client([_completion(content)], calls)
    assert client.review(_input()).decision == dr.PASS


def test_an_extra_top_level_key_is_rejected_as_a_contract_violation():
    calls: list[httpx.Request] = []
    body = json.loads(_payload())
    body["score"] = 9
    content = json.dumps(body, ensure_ascii=False)
    client = _client([_completion(content), _completion(content)], calls)
    result = client.review(_input())
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "contract_violation"


def test_a_transport_failure_twice_is_need_human_not_pass():
    calls: list[httpx.Request] = []
    client = _client([_completion("upstream", status=500), _completion("upstream", status=500)], calls)
    result = client.review(_input())

    assert len(calls) == 2
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "transport_error"
    assert result.decision != dr.PASS


def test_a_transport_exception_twice_is_need_human_not_pass():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("unit-test connection refused")

    client = dr.DeepSeekReviewClient(
        api_key="unit-test-placeholder",
        transport=httpx.MockTransport(handler),
        clock=lambda: 0.0,
    )
    result = client.review(_input())

    assert len(calls) == 2
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "transport_error"


# ---------------------------------------------------------------- 四、不发模型的降级


def test_credential_suspect_does_not_call_the_model():
    calls: list[httpx.Request] = []
    client = _client([_completion(_payload())], calls)
    result = client.review(
        _input(
            artifacts=(
                dr.ReviewArtifact(
                    rel_path="qoder-workspace/index.html",
                    filename="index.html",
                    format="html",
                    content='api_key = "abcdefghijklmnop1234"',
                ),
            )
        )
    )

    assert calls == [], "命中凭据形态 MUST NOT 发送模型"
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "credential_suspect"
    assert all(finding.met is None for finding in result.findings)


def test_prompt_too_large_does_not_call_the_model():
    calls: list[httpx.Request] = []
    client = _client([_completion(_payload())], calls)
    result = client.review(
        _input(
            artifacts=(
                dr.ReviewArtifact(
                    rel_path="qoder-workspace/index.html",
                    filename="index.html",
                    format="html",
                    content="x" * (dr.MAX_PROMPT_BYTES + 1),
                ),
            )
        )
    )

    assert calls == []
    assert result.decision == dr.NEED_HUMAN
    assert result.reason == "prompt_too_large"


def test_the_response_model_and_token_counts_are_recorded_for_audit():
    calls: list[httpx.Request] = []
    client = _client([_completion(_payload(), model="deepseek-flash")], calls)
    result = client.review(_input())

    assert result.model_id == "deepseek-flash"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.total_tokens == 15
    assert result.prompt_sha256 and result.raw_sha256
    assert result.raw_chars > 0
