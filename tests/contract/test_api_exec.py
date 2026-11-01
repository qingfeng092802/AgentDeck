"""路线 B：纯 API 执行器（2026-10-31）。

钉四件事：
1. **路由化 spec**：没有 `model_routing` / 厂商不认识 ⇒ 拒绝装配，不猜默认厂商；
2. **围栏剥离**：```html … ``` 写成文件时是干净正文；
3. **截断零产物**：`artifact_completeness` 不过 **或** `finish_reason=length` ⇒ 不写盘、
   交回零产物（门禁据此判 `ARTIFACT_INVALID`）—— 这是 P0-1 在执行器这一侧的入口；
4. **凭据**：缺厂商 key 在发请求之前就失败，消息里只有变量名。
"""

from __future__ import annotations

import pytest

from acp.adapters.doubao import exec as api_transport
from acp.routing import RoutingSpecError
from acp.bus.acl import NO_ROLE_DECLARED
from acp.bus.gateway import BusGateway
from acp.config.loader import CredentialMissing
from acp.protocol import ArtifactFloor
from acp.protocol.messages import Task


def _task(**overrides) -> Task:
    values = dict(
        task_id="T02",
        title="首页",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        project_key="demo",
        assignee={"agent_id": "doubao", "role_id": NO_ROLE_DECLARED},
        objective="做首页",
        inputs=[],
        expected_output="",
        artifact_floor=ArtifactFloor(min_bytes=1, min_lines=1, format="html"),
        acceptance_criteria=["产物 index.html 存在且非空"],
        token_budget=None,
        deadline_minutes=None,
        constraints=[],
    )
    values.update(overrides)
    return Task(**values)


def _spec() -> api_transport.ApiExecSpec:
    return api_transport.ApiExecSpec(
        vendor="深度求索 DeepSeek",
        vendor_id="deepseek",
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
    )


def _patch_chat(monkeypatch, text: str, finish: str = "stop") -> list[dict]:
    calls: list[dict] = []

    def fake_chat(seat, *, system, user, max_tokens, timeout=None, **kwargs):
        calls.append({"model": seat.model, "max_tokens": max_tokens, "user": user})
        return text, {"finish_reason": finish, "prompt_tokens": 10, "completion_tokens": 20}

    monkeypatch.setattr(api_transport.settings_run, "_chat", fake_chat)
    return calls


# —— 路由化 spec ——


def test_without_routing_the_api_executor_refuses_to_be_wired():
    with pytest.raises(RoutingSpecError) as caught:
        api_transport.spec_from_route(None)
    assert "model_routing" in str(caught.value)


def test_an_unknown_vendor_is_refused_not_defaulted():
    routing = {"executor": {"kind": "api", "vendor": "某家不存在的厂商", "model": "x"}}
    with pytest.raises(RoutingSpecError) as caught:
        api_transport.spec_from_route(routing)
    assert "不存在的厂商" in str(caught.value)


def test_a_valid_route_carries_the_vendor_endpoint_and_thinking():
    routing = {
        "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash", "thinking": "high"}
    }
    spec = api_transport.spec_from_route(routing, env={"DEEPSEEK_API_KEY": "k-test"})
    assert (spec.vendor_id, spec.model, spec.thinking) == ("deepseek", "deepseek-flash", "high")
    assert spec.key_env == "DEEPSEEK_API_KEY", "厂商 → 变量名的映射在 spec 里，工厂不再抄一份"
    assert spec.base_url.startswith("https://api.deepseek.com")


# —— 围栏与目标文件名 ——


def test_strip_fence_unwraps_a_full_markdown_block():
    assert api_transport.strip_fence("```html\n<p>x</p>\n```") == "<p>x</p>"
    assert api_transport.strip_fence("  <p>x</p>  ") == "<p>x</p>"


def test_target_name_prefers_the_name_matching_the_floor_format():
    task = _task(acceptance_criteria=["产物 index.html 存在且非空", "style.css 被引用"])
    assert api_transport.target_name(task) == "index.html", "只认后缀等于卡的格式那个名字"
    bare = _task(acceptance_criteria=["产物存在且非空"])
    assert api_transport.target_name(bare) == "T02.html", "找不到名字才退 <卡号>.<格式>"


# —— 落盘 ——


def test_a_fenced_reply_lands_as_a_clean_file(monkeypatch, tmp_path):
    calls = _patch_chat(monkeypatch, "```html\n<html><body>首页</body></html>\n```")
    transport = api_transport.ApiExecTransport(
        api=_spec(), gateway=BusGateway(tmp_path), env={"DEEPSEEK_API_KEY": "k-test"}
    )
    result = transport.collect(transport.submit(_task()))

    assert result.self_reported_status == "success"
    (ref,) = result.artifacts
    assert ref.rel_path == "doubao-workspace/index.html"
    on_disk = (tmp_path / ref.rel_path).read_text(encoding="utf-8")
    assert on_disk == "<html><body>首页</body></html>", "围栏不许进文件"
    assert ref.bytes == len(on_disk.encode("utf-8"))
    assert calls and calls[0]["model"] == "deepseek-flash"
    assert {notice.usage_kind for notice in result.usage} == {"INPUT", "OUTPUT"}


def test_a_structurally_truncated_reply_never_reaches_the_disk(monkeypatch, tmp_path):
    _patch_chat(monkeypatch, "<html><body>写了一半", finish="length")
    transport = api_transport.ApiExecTransport(
        api=_spec(), gateway=BusGateway(tmp_path), env={"DEEPSEEK_API_KEY": "k-test"}
    )
    result = transport.collect(transport.submit(_task()))

    assert result.artifacts == [], "截断的产物不许落盘（门禁据此判 ARTIFACT_INVALID）"
    assert result.self_reported_status == "failed"
    assert any("truncated" in issue for issue in result.issues), result.issues
    assert not (tmp_path / "doubao-workspace/index.html").exists()


def test_finish_reason_length_alone_blocks_a_balanced_reply(monkeypatch, tmp_path):
    """反向补充：括号配平也挡不住"厂商自己说被截了"——只在完整块之后停住的那种截断。"""
    _patch_chat(monkeypatch, "<html><body>x</body></html>", finish="length")
    transport = api_transport.ApiExecTransport(
        api=_spec(), gateway=BusGateway(tmp_path), env={"DEEPSEEK_API_KEY": "k-test"}
    )
    result = transport.collect(transport.submit(_task()))
    assert result.artifacts == []
    assert not (tmp_path / "doubao-workspace/index.html").exists()


def test_a_missing_vendor_key_fails_before_any_request(monkeypatch, tmp_path):
    calls = _patch_chat(monkeypatch, "<html><body>x</body></html>")
    transport = api_transport.ApiExecTransport(api=_spec(), gateway=BusGateway(tmp_path), env={})
    with pytest.raises(CredentialMissing) as caught:
        transport.submit(_task())
    assert "DEEPSEEK_API_KEY" in str(caught.value)
    assert calls == [], "缺凭据时不许发出任何请求"


# —— 注册面：默认执行者仍是 qoder ——


def test_the_api_executor_reuses_the_existing_api_agent_and_stays_out_of_the_default_ranking():
    """路线 B 的实现口径：复用注册表里的 `doubao`（API 形态），**不新增注册条目**。

    三条一起钉：
    ① `routing.EXECUTOR_API_AGENT` 就是 `doubao` —— 注册表条数被 `params-ref §3` 的并发关系式钉着，
       加一条就得改 owner 定的平台并发值；
    ② `doubao` 的静态能力里没有 `frontend_coding` ⇒ 没有路由的老路径里它不会被排序选中，
       qoder 仍是默认执行者；
    ③ 带路由装配时，工厂从**这个包**取到纯 API 执行适配器（`RoutedTransport`）与路由 spec。
    """
    from acp.adapters import spec_factory
    from acp.adapters.doubao import RoutedTransport, spec_from_route
    from acp.registry.loader import load_registrations
    from acp.routing import EXECUTOR_API_AGENT

    assert EXECUTOR_API_AGENT == "doubao"
    registration = next(item for item in load_registrations() if item.agent_id == "doubao")
    assert registration.manifest.form == "API"
    assert "frontend_coding" not in registration.manifest.capabilities
    assert RoutedTransport is api_transport.ApiExecTransport
    routing = {"executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash"}}
    spec = spec_from_route(routing, env={"DEEPSEEK_API_KEY": "k-test"})
    assert spec.vendor_id == "deepseek"
    assert spec_factory.ROUTED_SPEC_ATTR == "spec_from_route"
    assert spec_factory.ROUTED_TRANSPORT_ATTR == "RoutedTransport"


def test_usage_notices_are_byok_and_the_ledger_can_price_them(monkeypatch, tmp_path):
    """`TOKEN` 口径对 DeepSeek / 千问会 `model_row_missing`（§6 只有豆包四行）⇒ 这一支记 BYOK。

    2026-10-31 真机那一发就是这么死在回收段的：文件写成了，账却对不上价目表。断言两件事：
    口径是 BYOK，且定价器真算得出来（0 元 + 出处指向 §6）。
    """
    from acp.cost import pricing

    _patch_chat(monkeypatch, "<html><body>x</body></html>")
    transport = api_transport.ApiExecTransport(
        api=_spec(), gateway=BusGateway(tmp_path), env={"DEEPSEEK_API_KEY": "k-test"}
    )
    result = transport.collect(transport.submit(_task()))
    assert result.usage, "有 token 读数就该落账，不能因为换了口径就整条不报"
    for notice in result.usage:
        assert notice.billing_basis == "BYOK"
        assert notice.model_ref == "deepseek-flash"
        amount = pricing.price_of(notice)
        assert amount.cost_amount == 0, "BYOK 由厂商结算 ⇒ 平台账记 0（这是口径，不是缺价）"
        assert "§6" in amount.source
