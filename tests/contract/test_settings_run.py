"""R-2（2026-10-29）：设置页四席直跑的纯管线。

不连网、不连库：`chat` 注入位返回假回话，钉的是“四席顺序、文件落盘、报告形状、密钥缺失拒绝”。
真模型调用归 owner 的终端/接口那一次。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from acp import settings_run
from acp.settings_run import (
    SeatModel,
    SettingsRunError,
    _json_object,
    _normalize_plan,
    _plan_prompt,
    _safe_name,
    resolve_seat,
    run_settings,
)


def _seat(name: str) -> SeatModel:
    return SeatModel(
        vendor="深度求索 DeepSeek",
        model="deepseek-v4.1-flash",
        api_key=f"key-{name}",
        base_url="https://api.deepseek.com/v1",
    )


SEATS = {
    "orchestrator": _seat("orchestrator"),
    "executor": _seat("executor"),
    "reviewer": _seat("reviewer"),
    "acceptor": _seat("acceptor"),
}


def _fake_chat(calls: list[dict[str, object]]):
    def chat(seat: SeatModel, *, system: str, user: str, max_tokens: int, **kwargs):
        calls.append({"seat": seat.model, "system": system, "max_tokens": max_tokens})
        if "连接测试" in system:
            return ("ok", {})
        if "项目总管" in system:
            return (
                json.dumps(
                    {
                        "title": "咖啡店官网",
                        "pages": [
                            {"file": "index.html", "label": "首页", "duty": "展示商品"},
                            {"file": "about.html", "label": "关于", "duty": "介绍品牌"},
                        ],
                        "acceptance": ["首页可打开", "移动端不横向溢出"],
                        "files": [
                            {"name": "index.html", "purpose": "首页"},
                            {"name": "about.html", "purpose": "关于页"},
                            {"name": "style.css", "purpose": "样式"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                {"total_tokens": 100},
            )
        if "执行工程师" in system:
            return ("<html><link rel=\"stylesheet\" href=\"style.css\"><body>ok</body></html>", {})
        if "独立审查员" in system:
            return (json.dumps({"verdict": "PASS", "findings": [{"criterion": "首页可打开", "met": True, "evidence": "有 index.html"}]}), {})
        if "验收员" in system:
            return (json.dumps({"decision": "ACCEPT", "reason": "审查通过"}), {})
        raise AssertionError(system)

    return chat


def test_four_seats_run_and_write_a_report(tmp_path):
    calls: list[dict[str, object]] = []
    report = run_settings(
        project_key="settings-demo",
        requirement="做一个咖啡店官网，要有购物车（仅前端交互）、移动端适配，并提供本地预览脚本",
        seats=SEATS,
        output_root=tmp_path,
        chat=_fake_chat(calls),
    )

    names = {item["name"] for item in report["files"]}
    assert {"index.html", "about.html", "style.css", "app.js", "serve.py", "README.md"} <= names
    assert report["review"]["verdict"] == "PASS"
    assert report["acceptance"]["decision"] == "ACCEPT"
    assert report["repair_attempts"] == 0
    assert report["repair_error"] is None
    assert report["models"]["executor"]["model"] == "deepseek-v4.1-flash"
    assert report["timings_s"]["orchestrator"] >= 0
    output = tmp_path / "settings-demo" / f"settings-run-{report['run_id']}"
    assert (output / "index.html").is_file()
    assert (output / "run-report.json").is_file()
    # 总管 → 执行（每文件一次）→ 审查 → 验收：至少 1 + 3 + 1 + 1 次调用
    assert len(calls) >= 6


def test_bad_plan_json_is_a_named_stage_failure(tmp_path):
    def chat(seat, *, system, user, max_tokens, **kwargs):
        return ("这不是 JSON", {})

    with pytest.raises(SettingsRunError) as caught:
        run_settings(
            project_key="settings-bad",
            requirement="随便",
            seats=SEATS,
            output_root=tmp_path,
            chat=chat,
        )
    assert caught.value.stage == "plan"
    assert caught.value.code == "bad_json"


def test_preflight_reports_a_bad_model_before_generation(tmp_path):
    calls: list[str] = []

    def chat(seat, *, system, user, max_tokens, **kwargs):
        calls.append(seat.model)
        raise SettingsRunError("model", "http_404", "model or endpoint not found")

    with pytest.raises(SettingsRunError) as caught:
        run_settings(
            project_key="settings-preflight",
            requirement="随便",
            seats=SEATS,
            output_root=tmp_path,
            chat=chat,
        )
    assert caught.value.stage == "preflight"
    assert caught.value.code == "model_unavailable:orchestrator"
    # 404 说的是"这个名字不行"，重打只是浪费配额 ⇒ 只许打一发就停下
    assert len(calls) == 1, f"名字不存在的探针重打了 {len(calls)} 次"


def test_preflight_retries_a_slow_probe_but_not_a_wrong_model(tmp_path):
    """2026-10-31 现场：同一个 ping 两发分别 2.1s 与 20.7s，30s 上限一发慢响应就把整单判死。

    探针可以换一发重打（连通性），型号名/密钥不行（重打也还是 404/401）。
    """
    pings: list[float] = []
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, timeout=300.0, **kwargs):
        if "连接测试" in system:
            pings.append(timeout)
            if len(pings) == 1:
                raise SettingsRunError("model", "timeout", "深度求索 DeepSeek 在 45s 内没有返回（ReadTimeout）")
            return ("ok", {})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, timeout=timeout, **kwargs)

    report = run_settings(
        project_key="settings-preflight-retry",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
    )
    assert len(pings) == 5, "四席各一发 + 慢的那一发重打一次"
    assert set(pings) == {settings_run.PREFLIGHT_TIMEOUT_S}, "探针走自己的墙钟，不吃正文那一发的 300s"
    assert report["acceptance"]["decision"] == "ACCEPT"


def _reasoning_like_chat(calls: list[dict[str, object]]):
    """把 2026-10-30 对 `deepseek-flash` 的实测形状做成假型号。

    那一发的原始读数：`max_tokens=16` ⇒ `reasoning_tokens=16`、`finish_reason=length`、
    `content` 是空串；同一发放到 128 ⇒ 推理 86 字 + 正文 `ok`。老探针按"正文为空"判死，
    于是**一个完全能用的型号在规划之前被判 `model_unavailable`**。这两发判据钉的就是那条实测线。
    """
    inner = _fake_chat(calls)

    def chat(seat: SeatModel, *, system: str, user: str, max_tokens: int, **kwargs):
        if "连接测试" in system:
            calls.append({"seat": seat.model, "system": system, "max_tokens": max_tokens})
            if max_tokens < 128:
                raise SettingsRunError(
                    "model",
                    "empty_response",
                    "返回了空正文（推理段 43 字｜finish_reason=length）⇒ 预算被推理吃光，不是型号不可用",
                )
            return ("ok", {"total_tokens": 64})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    return chat


def test_preflight_budget_survives_a_reasoning_model(tmp_path):
    calls: list[dict[str, object]] = []
    report = run_settings(
        project_key="settings-reasoning-preflight",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=_reasoning_like_chat(calls),
    )
    pings = [item for item in calls if "连接测试" in str(item["system"])]
    assert len(pings) == 4, "四席各打一发探针"
    assert all(item["max_tokens"] >= 128 for item in pings), (
        f"探针预算 {sorted({item['max_tokens'] for item in pings})} ⇒ 回到 16 那一档，"
        "推理型型号会被误判成不可用（deepseek-flash 2026-10-30 实测）"
    )
    assert report["review"]["verdict"] == "PASS"
    assert report["acceptance"]["decision"] == "ACCEPT"


def test_preflight_budget_below_the_line_is_rejected_by_the_same_model(tmp_path, monkeypatch):
    """反向对照：把预算压回 16 ⇒ 同一个假型号必须拒绝，并且拒绝停在 preflight。

    没有这一发，上面那条只证明"假型号恰好接受了当前值"，量不出那条预算线在哪儿。
    """
    monkeypatch.setattr(settings_run, "PREFLIGHT_MAX_TOKENS", 16)
    with pytest.raises(SettingsRunError) as caught:
        run_settings(
            project_key="settings-reasoning-preflight-16",
            requirement="做一个页面",
            seats=SEATS,
            output_root=tmp_path,
            chat=_reasoning_like_chat([]),
        )
    assert caught.value.stage == "preflight"
    assert "empty_response" in caught.value.detail


def test_missing_key_is_refused_before_any_call():
    with pytest.raises(SettingsRunError) as caught:
        resolve_seat(vendor="深度求索 DeepSeek", model="deepseek-v4.1-flash", api_key="")
    assert caught.value.stage == "config"
    assert caught.value.code == "missing_key"


def test_stage_callback_reports_each_phase_and_cancel_stops_before_work(tmp_path):
    stages: list[tuple[str, str]] = []
    calls: list[str] = []

    def chat(seat, *, system, user, max_tokens, **kwargs):
        calls.append(system)
        if "连接测试" in system:
            return ("ok", {})
        if "项目总管" in system:
            return (
                json.dumps(
                    {
                        "title": "X",
                        "pages": [{"file": "index.html", "label": "首页", "duty": "首页"}],
                        "acceptance": ["可打开"],
                        "files": [{"name": "index.html", "purpose": "首页"}, {"name": "style.css", "purpose": "样式"}],
                    }
                ),
                {},
            )
        if "执行工程师" in system:
            return ('<html><body>ok</body></html>' if "`index.html`" in user else "body{color:red}", {})
        if "独立审查员" in system:
            return (json.dumps({"verdict": "PASS", "findings": []}), {})
        if "验收员" in system:
            return (json.dumps({"decision": "ACCEPT", "reason": "ok"}), {})
        raise AssertionError(system)

    run_settings(
        project_key="settings-stages",
        requirement="做页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
        on_stage=lambda stage, detail="": stages.append((stage, detail)),
    )
    assert [stage for stage, _ in stages][:2] == ["plan", "execute"]
    assert any(stage == "review" for stage, _ in stages)
    assert any(stage == "accept" for stage, _ in stages)

    with pytest.raises(SettingsRunError) as caught:
        run_settings(
            project_key="settings-cancel",
            requirement="做页面",
            seats=SEATS,
            output_root=tmp_path,
            chat=chat,
            preflight=False,
            should_stop=lambda: True,
        )
    assert caught.value.code == "cancelled"


def _two_file_plan() -> str:
    return json.dumps(
        {
            "title": "X",
            "pages": [{"file": "index.html", "label": "首页", "duty": "首页"}],
            "acceptance": ["样式表与首页类名一致"],
            "files": [
                {"name": "index.html", "purpose": "首页"},
                {"name": "style.css", "purpose": "样式"},
            ],
        },
        ensure_ascii=False,
    )


def test_failed_review_triggers_one_repair_and_a_second_review(tmp_path):
    stages: list[tuple[str, str]] = []
    reviews = {"count": 0}
    repair_calls: list[str] = []

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "项目总管" in system:
            return (_two_file_plan(), {})
        if "执行工程师" in system:
            if "`style.css`" in user:
                return ("body{color:red}", {})
            return ('<html><link rel="stylesheet" href="style.css"><h1 class="hero">x</h1></html>', {})
        if "修复工程师" in system:
            repair_calls.append(user)
            return (json.dumps({"files": [{"name": "style.css", "content": "body{color:blue}"}]}), {})
        if "独立审查员" in system:
            reviews["count"] += 1
            if reviews["count"] == 1:
                return (
                    json.dumps(
                        {"verdict": "FAIL", "findings": [{"criterion": "样式表与首页类名一致", "met": False, "evidence": "style.css 缺 .hero"}]},
                        ensure_ascii=False,
                    ),
                    {},
                )
            return (json.dumps({"verdict": "PASS", "findings": []}), {})
        if "验收员" in system:
            return (json.dumps({"decision": "ACCEPT", "reason": "修复后通过"}), {})
        raise AssertionError(system)

    report = run_settings(
        project_key="settings-repair",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
        on_stage=lambda stage, detail="": stages.append((stage, detail)),
    )

    output = tmp_path / "settings-repair" / f"settings-run-{report['run_id']}"
    assert (output / "style.css").read_text(encoding="utf-8") == "body{color:blue}"
    assert reviews["count"] == 2
    assert report["review"]["verdict"] == "PASS"
    assert report["repair_attempts"] == 1
    assert report["repair_error"] is None
    assert any(stage == "repair" for stage, _ in stages)
    record = next(item for item in report["files"] if item["name"] == "style.css")
    assert record["lines"] == 1
    assert "style.css 缺 .hero" in repair_calls[0]


def test_broken_repair_reply_is_recorded_and_does_not_fake_a_pass(tmp_path):
    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "项目总管" in system:
            return (_two_file_plan(), {})
        if "执行工程师" in system:
            return (
                '<html><link rel="stylesheet" href="style.css"><body>ok</body></html>'
                if "`index.html`" in user
                else "body{color:red}",
                {},
            )
        if "修复工程师" in system:
            return ("我修好了", {})
        if "独立审查员" in system:
            return (json.dumps({"verdict": "FAIL", "findings": []}), {})
        if "验收员" in system:
            return (json.dumps({"decision": "REJECT", "reason": "审查未通过"}), {})
        raise AssertionError(system)

    report = run_settings(
        project_key="settings-broken-repair",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
    )

    assert report["review"]["verdict"] == "FAIL"
    assert report["acceptance"]["decision"] == "REJECT"
    assert report["repair_attempts"] == 1
    assert report["repair_error"] is not None and report["repair_error"].startswith("repair:bad_json")


# —————————————————— 思考强度、传输重试、自适应预算（2026-10-30 三发真模型直跑带出来的）


def _seat_with(vendor: str, level: str) -> SeatModel:
    return SeatModel(vendor=vendor, model="probe-model", api_key="k", base_url="https://x.example/v1", thinking=level)


def test_thinking_maps_to_the_field_each_vendor_actually_reads():
    """档位 → 字段：只映射**实测会改变行为**的那两种写法。

    2026-10-30 用 owner 的三把 key 各量两发：豆包 `thinking.type` 是开关（disabled ⇒ 推理段 0 字）；
    千问 `enable_thinking` + `thinking_budget` 是真预算（budget=128 那发 reasoning_tokens 正好 128 封顶）；
    DeepSeek 三种写法全被 200 接受而推理段照旧 91—184 token ⇒ **一个字段都不发**。
    """
    assert settings_run._thinking_fields(_seat_with("字节跳动豆包", "off")) == {"thinking": {"type": "disabled"}}
    for level in ("low", "medium", "high"):
        assert settings_run._thinking_fields(_seat_with("字节跳动豆包", level)) == {"thinking": {"type": "enabled"}}
    assert settings_run._thinking_fields(_seat_with("阿里云通义千问", "off")) == {"enable_thinking": False}
    assert settings_run._thinking_fields(_seat_with("阿里云通义千问", "low")) == {
        "enable_thinking": True,
        "thinking_budget": 1024,
    }
    assert settings_run._thinking_fields(_seat_with("阿里云通义千问", "high")) == {
        "enable_thinking": True,
        "thinking_budget": 8192,
    }
    for vendor in ("深度求索 DeepSeek", "月之暗面 Kimi", "智谱 AI GLM", "MiniMax 稀宇科技"):
        for level in ("off", "low", "medium", "high"):
            assert settings_run._thinking_fields(_seat_with(vendor, level)) == {}, f"{vendor}/{level} 未实测 ⇒ 不发字段"


def test_bad_thinking_level_is_refused_before_any_call():
    with pytest.raises(SettingsRunError) as caught:
        resolve_seat(vendor="深度求索 DeepSeek", model="deepseek-flash", api_key="k", thinking="ultra")
    assert caught.value.code == "bad_thinking"


class _FakeResponse:
    def __init__(self, *, content="ok", finish="stop", reasoning="", status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self._message = {"content": content, "reasoning_content": reasoning}
        self._finish = finish

    def json(self):
        return {"choices": [{"message": self._message, "finish_reason": self._finish}], "usage": {"total_tokens": 7}}


class _FakeClient:
    """替掉 `httpx.Client`：收下每一发的请求体，按脚本返回或抛错。"""

    def __init__(self, script):
        self._script = list(script)
        self.bodies: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.bodies.append(dict(json))
        item = self._script.pop(0) if self._script else _FakeResponse()
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(settings_run.httpx, "Client", lambda **kwargs: fake)


def test_chat_puts_the_thinking_field_into_the_request_body(monkeypatch):
    fake = _FakeClient([_FakeResponse()])
    _patch_client(monkeypatch, fake)
    content, usage = settings_run._chat(
        _seat_with("阿里云通义千问", "low"), system="s", user="u", max_tokens=4096
    )
    assert content == "ok"
    assert usage["finish_reason"] == "stop"
    sent = fake.bodies[0]
    assert sent["enable_thinking"] is True and sent["thinking_budget"] == 1024
    assert sent["max_tokens"] == 4096 and sent["model"] == "probe-model"


def test_chat_sends_nothing_for_a_vendor_whose_field_is_ignored(monkeypatch):
    fake = _FakeClient([_FakeResponse()])
    _patch_client(monkeypatch, fake)
    settings_run._chat(_seat_with("深度求索 DeepSeek", "high"), system="s", user="u", max_tokens=4096)
    assert not {"thinking", "enable_thinking", "thinking_budget"} & set(fake.bodies[0])


def test_unsupported_thinking_field_is_stripped_and_reposted(monkeypatch):
    """400 里点名了这个字段 ⇒ 摘掉再发一发，不把"端点不认"报成"模型坏了"。"""
    fake = _FakeClient(
        [
            _FakeResponse(status_code=400, text='unknown parameter: "enable_thinking"', content=""),
            _FakeResponse(content="ok"),
        ]
    )
    _patch_client(monkeypatch, fake)
    content, _ = settings_run._chat(_seat_with("阿里云通义千问", "medium"), system="s", user="u", max_tokens=128)
    assert content == "ok"
    assert "enable_thinking" in fake.bodies[0] and "enable_thinking" not in fake.bodies[1]


def test_transport_failure_retries_once_with_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(settings_run.time, "sleep", lambda seconds: slept.append(seconds))
    fake = _FakeClient([httpx.RemoteProtocolError("peer closed connection"), _FakeResponse(content="补回来了")])
    _patch_client(monkeypatch, fake)
    content, _ = settings_run._chat(_seat_with("阿里云通义千问", "off"), system="s", user="u", max_tokens=8192)
    assert content == "补回来了"
    assert len(fake.bodies) == 2 and slept == [2.0]


def test_transport_attempts_are_readable_from_the_usage_dict(monkeypatch):
    """救回一发必须留下读数：没有它，真实链上"退避到底生效没有"只能靠猜（2026-10-31 千问独跑）。"""
    monkeypatch.setattr(settings_run.time, "sleep", lambda seconds: None)
    fake = _FakeClient([httpx.RemoteProtocolError("peer closed"), _FakeResponse(content="ok")])
    _patch_client(monkeypatch, fake)
    _, usage = settings_run._chat(_seat_with("阿里云通义千问", "off"), system="s", user="u", max_tokens=8192)
    assert usage["transport_attempts"] == 2
    fake_clean = _FakeClient([_FakeResponse(content="ok")])
    _patch_client(monkeypatch, fake_clean)
    _, clean = settings_run._chat(_seat_with("阿里云通义千问", "off"), system="s", user="u", max_tokens=8192)
    assert clean["transport_attempts"] == 1


def test_report_names_the_stage_that_needed_a_transport_retry(tmp_path):
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        text, usage = inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)
        if "执行工程师" in system:
            return text, {**usage, "transport_attempts": 3}
        return text, usage

    report = run_settings(
        project_key="settings-transport-retry",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
    )
    assert report["transport_retries"] == {
        "execute:index.html": 3,
        "execute:about.html": 3,
        "execute:style.css": 3,
    }


def test_second_transport_failure_names_the_attempt_count(monkeypatch):
    monkeypatch.setattr(settings_run.time, "sleep", lambda seconds: None)
    fake = _FakeClient([httpx.ReadError("a"), httpx.ReadError("b")])
    _patch_client(monkeypatch, fake)
    with pytest.raises(SettingsRunError) as caught:
        settings_run._chat(_seat_with("阿里云通义千问", "off"), system="s", user="u", max_tokens=8192)
    assert caught.value.code == "transport"
    assert "已试 2 发" in caught.value.detail


def test_timeout_is_not_retried(monkeypatch):
    """反向对照：`TimeoutException` 也是 `TransportError` 的子类，但不许跟着翻倍 ⇒
    600s 那一发翻倍成一整单两倍的墙钟，是比断流更贵的失效。"""
    fake = _FakeClient([httpx.ConnectTimeout("t"), _FakeResponse(content="不该走到这里")])
    _patch_client(monkeypatch, fake)
    with pytest.raises(SettingsRunError) as caught:
        settings_run._chat(_seat_with("阿里云通义千问", "off"), system="s", user="u", max_tokens=128, timeout=1.0)
    assert caught.value.code == "timeout"
    assert len(fake.bodies) == 1


def test_empty_reply_reports_the_reasoning_chars(monkeypatch):
    """正文空 + 推理段很长 ⇒ 消息必须说"预算被推理吃光"，否则下一次又要归因成型号坏了。"""
    fake = _FakeClient([_FakeResponse(content="", finish="length", reasoning="想" * 4000)])
    _patch_client(monkeypatch, fake)
    with pytest.raises(SettingsRunError) as caught:
        settings_run._chat(_seat_with("深度求索 DeepSeek", "medium"), system="s", user="u", max_tokens=4096)
    assert caught.value.code == "empty_response"
    assert "推理段 4000 字" in caught.value.detail and "不是型号不可用" in caught.value.detail


def _review_that_needs_a_bigger_budget(seen: list[int]):
    """审查席：4096 那一发正文空（推理吃满），8192 那一发才给结论 —— 复刻 2026-10-30 的现场。"""
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "独立审查员" in system:
            seen.append(max_tokens)
            if max_tokens < 8192:
                raise SettingsRunError(
                    "model", "empty_response", "返回了空正文（推理段 10931 字｜finish_reason=length）"
                )
            return (json.dumps({"verdict": "PASS", "findings": []}), {"finish_reason": "stop"})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    return chat


def test_review_budget_doubles_once_when_the_reply_is_reasoning_only(tmp_path):
    seen: list[int] = []
    report = run_settings(
        project_key="settings-budget-review",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=_review_that_needs_a_bigger_budget(seen),
        preflight=False,
    )
    assert seen == [4096, 8192], "首发按配置预算，翻倍只一次"
    assert report["review"]["verdict"] == "PASS"
    assert report["budget_doublings"] == {"review": 1, "repair": 0}


def test_review_budget_doubles_when_the_reply_is_truncated(tmp_path):
    """`finish_reason=length` 这一族：正文非空但被截断，也要给它一次翻倍的机会。"""
    seen: list[int] = []
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "独立审查员" in system:
            seen.append(max_tokens)
            if max_tokens < 8192:
                return (json.dumps({"verdict": "PASS", "findings": []})[:40], {"finish_reason": "length"})
            return (json.dumps({"verdict": "PASS", "findings": []}), {"finish_reason": "stop"})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    report = run_settings(
        project_key="settings-budget-truncated",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
    )
    assert seen == [4096, 8192]
    assert report["budget_doublings"]["review"] == 1


def test_a_budget_that_fits_records_zero_doublings(tmp_path):
    """反向对照：正常那一发不许记成"翻过倍" —— 否则这个读数永远分不清是型号还是预算的问题。"""
    report = run_settings(
        project_key="settings-budget-ok",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=_fake_chat([]),
        preflight=False,
    )
    assert report["budget_doublings"] == {"review": 0, "repair": 0}


def test_loose_overflow_criterion_is_rewritten_but_others_are_not():
    """「无横向滚动条」是假通过的源头：`overflow-x:hidden` 就满足它。"""
    plan, _files = _normalize_plan(
        {
            "title": "t",
            "pages": [],
            "acceptance": ["375px 视口下页面无横向滚动条", "首页可双击打开", "购物车可加可减"],
            "files": [],
        },
        "做一个页面",
    )
    assert plan["acceptance"][0] == "在 375px 下内容不被截断且无横向溢出"
    assert plan["acceptance"][1:] == ["首页可双击打开", "购物车可加可减"]
    # 已经写对的那条一个字都不许改（反向对照：不许把整份名单重写一遍）
    again, _ = _normalize_plan({"title": "t", "pages": [], "acceptance": [plan["acceptance"][0]], "files": []}, "x")
    assert again["acceptance"] == [plan["acceptance"][0]]


def test_the_plan_prompt_names_the_tight_criterion_verbatim():
    prompt = _plan_prompt("做一个页面")
    assert "在 375px 下内容不被截断且无横向溢出" in prompt
    assert "不许写成「无横向滚动条」" in prompt


def test_backend_levels_match_the_frontend_selector():
    """跨文件对读：设置页那四档与后端认识的档位必须同源，漂了就是"UI 选了个后端不认的值"。"""
    source = (Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "thinking.ts").read_text(encoding="utf-8")
    declared = tuple(re.findall(r'\{ value: "([a-z]+)", label: "[^"]+" \}', source))
    assert declared == tuple(settings_run.THINKING_LEVELS), f"{declared} vs {settings_run.THINKING_LEVELS}"


# ———————————————————————————— 每一发都吃自适应预算 + 回话取形（2026-10-31 两发独跑带出来的）


def _run(tmp_path, chat, project_key: str):
    return run_settings(
        project_key=project_key,
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
    )


def _starve_once(marker: str, seen: list[int], inner):
    """让带 `marker` 的那一席首发拿不到正文、翻倍之后才正常 —— 五个阶段共用一个形状，只换识别词。"""

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if marker in system:
            seen.append(max_tokens)
            if len(seen) == 1:
                raise SettingsRunError(
                    "model", "empty_response", "返回了空正文（推理段 12044 字｜finish_reason=length）"
                )
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    return chat


@pytest.mark.parametrize(
    ("marker", "key"),
    [("项目总管", "plan"), ("独立审查员", "review"), ("验收员", "accept")],
    ids=["plan", "review", "accept"],
)
def test_every_model_stage_gets_exactly_one_doubling(tmp_path, marker, key):
    """「每一发都吃预算」是覆盖性主张，所以三席都得站在这条判据里 —— 只测审查等于没改其余两格。"""
    seen: list[int] = []
    report = _run(tmp_path, _starve_once(marker, seen, _fake_chat([])), f"settings-budget-{key}")
    assert len(seen) == 2 and seen[1] == seen[0] * 2, f"翻倍只该一次：{seen}"
    assert report["budget_doublings"][key] == 1


def _starved_executor(name: str, seen: list[int], *, truncated: bool):
    """执行席里只有 `name` 那一发拿不到完整正文；`truncated` 决定是空正文还是被截断。"""
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "执行工程师" in system and f"`{name}`" in user:
            seen.append(max_tokens)
            if len(seen) == 1:
                if truncated:
                    return ("<html><body>半截", {"finish_reason": "length"})
                raise SettingsRunError(
                    "model", "empty_response", "返回了空正文（推理段 12044 字｜finish_reason=length）"
                )
            return ("<html><body>完整</body></html>", {"finish_reason": "stop"})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    return chat


@pytest.mark.parametrize("truncated", [False, True], ids=["empty_body", "truncated_body"])
def test_execute_budget_doubles_once_and_is_recorded_per_file(tmp_path, truncated):
    """执行席也吃预算：2026-10-31 DeepSeek 独跑死在 `execute`（8192 被推理吃满、正文为空）。

    记账 MUST 按文件 —— 六份文件各记一个 0 会把"到底哪一份不够"淹掉。盘上留的那一份也 MUST 是
    翻倍后的完整正文，不是被截断的半截。
    """
    seen: list[int] = []
    report = _run(
        tmp_path, _starved_executor("style.css", seen, truncated=truncated), "settings-budget-execute"
    )
    assert len(seen) == 2 and seen[1] == seen[0] * 2, f"翻倍只该一次：{seen}"
    assert report["budget_doublings"] == {"review": 0, "repair": 0, "execute:style.css": 1}
    body = (
        tmp_path / "settings-budget-execute" / f"settings-run-{report['run_id']}" / "style.css"
    ).read_text(encoding="utf-8")
    assert "完整" in body and "半截" not in body


def test_json_object_recovers_the_self_closed_object_from_glued_replies():
    """`Expecting ',' delimiter` 的现场：对象后面又吐了一个对象，整串和"首{尾}"两刀都切歪。"""
    assert _json_object("accept", '{"decision":"ACCEPT"} {"note":"尾巴碎话"}') == {"decision": "ACCEPT"}


def test_json_object_drops_trailing_prose_that_carries_a_brace():
    """尾巴上的散文自带花括号 ⇒ "最后一个 }" 落在对象外面，只有末级扫描救得回。"""
    assert _json_object("accept", '结论：{"decision":"ACCEPT"}（附注 } 没闭）') == {"decision": "ACCEPT"}


def test_json_object_keeps_the_cheap_shapes_on_the_earlier_paths():
    assert _json_object("plan", '```json\n{"title":"咖啡店"}\n```') == {"title": "咖啡店"}
    assert _json_object("plan", '规划如下 {"title":"咖啡店","pages":[]} 以上') == {"title": "咖啡店", "pages": []}


def test_json_object_refuses_a_top_level_array_instead_of_rescuing_its_first_item():
    """末级扫描排在"顶层不是对象"之后：形状错了就明说，悄悄取数组第一个对象会把"少了几条结论"藏起来。"""
    with pytest.raises(SettingsRunError) as caught:
        _json_object("review", '[{"verdict":"PASS","findings":[]}]')
    assert caught.value.code == "bad_json"
    assert "顶层不是对象" in caught.value.detail


def test_json_object_names_the_stage_when_nothing_is_parseable():
    with pytest.raises(SettingsRunError) as caught:
        _json_object("accept", "验收员只回了一句话，没有花括号")
    assert caught.value.stage == "accept" and caught.value.code == "bad_json"
    assert "没有可解析的 JSON 对象" in caught.value.detail


def test_accept_stage_survives_a_glued_verdict(tmp_path):
    """端到端半边：容错长在 helper 里不算数，验收席那一发 MUST 真的走它。"""
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "验收员" in system:
            return ('{"decision":"ACCEPT","reason":"可用"} {"note":"多吐的一小段"}', {"finish_reason": "stop"})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    report = _run(tmp_path, chat, "settings-accept-glued")
    assert report["acceptance"] == {"decision": "ACCEPT", "reason": "可用"}


def test_readme_is_the_one_name_the_plan_pins_to_upper_case():
    """2026-10-31 混席那一发：总管把"文件名只用小写"写成验收标准，审查员判平台自己落盘的 `README.md` 违例 ⇒ 整单 REJECT。

    大写 README 是 GitHub 仓库主页的渲染约定 ⇒ 动的是那句绝对化约束，不是文件名。
    """
    assert [_safe_name(x) for x in ("readme.md", "README.md", "Readme.MD")] == ["README.md"] * 3
    _plan, files = _normalize_plan(
        {
            "title": "t",
            "pages": [],
            "acceptance": [],
            "files": [{"name": "readme.md", "purpose": "说明"}, {"name": "README.md", "purpose": "说明"}],
        },
        "做一个页面",
    )
    names = [item["name"] for item in files]
    assert names.count("README.md") == 1, "大小写各写一份 = 产物名单自相矛盾"


def test_the_plan_prompt_exempts_readme_from_the_lowercase_rule():
    prompt = _plan_prompt("做一个页面")
    rule = prompt.find("文件名只用小写字母")
    exemption = prompt.find("唯一例外是说明文件 `README.md`")
    assert 0 < rule < exemption, "例外句必须排在命名约束之后 —— 排前面会被当成另一条独立约束"
    assert "GitHub" in prompt[exemption:] and "不许把它判成违例" in prompt


# ———————————————————————————— 截断零容忍（2026-10-31 四发真模型矩阵：三发产出语法破文件）


def test_a_truncated_file_fails_the_stage_instead_of_landing_on_disk(tmp_path):
    """预算用尽仍被截断的回复以前会被原样写盘、job 还报 `done`。现在这一发必须失败，盘上不留半份。"""
    inner = _fake_chat([])

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "执行工程师" in system and "`style.css`" in user:
            return (".hero-title {\n  font-size: 2rem;\n}\n.hero-image {", {"finish_reason": "length"})
        return inner(seat, system=system, user=user, max_tokens=max_tokens, **kwargs)

    with pytest.raises(SettingsRunError) as caught:
        _run(tmp_path, chat, "settings-truncated")
    assert caught.value.stage == "execute"
    assert caught.value.code == "truncated_file"
    assert "style.css" in caught.value.detail
    leftovers = list((tmp_path / "settings-truncated").glob("settings-run-*/style.css"))
    assert leftovers == [], f"半份文件不许落盘：{leftovers}"


def test_a_truncated_repair_patch_is_refused_as_a_whole(tmp_path):
    """修复补丁里只要有一份是半截的，整批都不落盘 —— 修一半的目录比不修更难查。"""
    reviews = {"count": 0}

    def chat(seat, *, system, user, max_tokens, **kwargs):
        if "项目总管" in system:
            return (_two_file_plan(), {})
        if "执行工程师" in system:
            if "`style.css`" in user:
                return ("body{color:red}", {})
            return ('<html><link rel="stylesheet" href="style.css"><h1 class="hero">x</h1></html>', {})
        if "修复工程师" in system:
            return (
                json.dumps(
                    {
                        "files": [
                            {"name": "index.html", "content": '<html><link rel="stylesheet" href="style.css"><h1 class="hero">修好了</h1></html>'},
                            {"name": "style.css", "content": "body{color:blue}\n.hero {"},
                        ]
                    }
                ),
                {},
            )
        if "独立审查员" in system:
            reviews["count"] += 1
            return (json.dumps({"verdict": "FAIL", "findings": [{"criterion": "样式表与首页类名一致", "met": False, "evidence": "缺 .hero"}]}), {})
        if "验收员" in system:
            return (json.dumps({"decision": "REJECT", "reason": "仍有问题"}), {})
        raise AssertionError(system)

    report = run_settings(
        project_key="settings-repair-truncated",
        requirement="做一个页面",
        seats=SEATS,
        output_root=tmp_path,
        chat=chat,
        preflight=False,
    )
    assert report["repair_error"] and report["repair_error"].startswith("repair:truncated_file")
    output = tmp_path / "settings-repair-truncated" / f"settings-run-{report['run_id']}"
    assert (output / "style.css").read_text(encoding="utf-8") == "body{color:red}", "整批补丁都不许落盘"
    assert "修好了" not in (output / "index.html").read_text(encoding="utf-8"), "好补丁也不许单独落盘"
    assert report["acceptance"]["decision"] == "REJECT"
