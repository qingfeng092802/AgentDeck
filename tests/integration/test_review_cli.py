"""`python -m acp.review` 的判据：与看板同一个端点的文本通道（M2-W8）。

**不起真端口、不连库**：注入 `httpx.MockTransport`，量的是命令行把什么发出去、退出码怎么分档。
端点真正落库那一半在 `test_review_api.py`；两个文件各管一段，别把这里读成"双通道已在库面上验过"。
"""

from __future__ import annotations

import contextlib
import io
import json

import httpx

from acp.review.__main__ import main

REQUEST = "0a049b48-3e5a-4cee-ad0a-e5718371e59a"
CARD = {
    "task_id": "T01",
    "feature_request_id": REQUEST,
    "title": "全站样式",
    "status": "REVIEWING",
    "reviewer_agent_id": "doubao",
    "acceptor_agent_id": "workbuddy",
    "acceptance_criteria": ["首屏有公司名", "样式文件被引用"],
    "valid_artifacts": 1,
    "requires_open": False,
}


def run_cli(handler, *argv):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record), base_url="http://test")
    command = list(argv) if argv[0] == "pending" else [*argv, "--principal", "owner-1"]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(command, client=client)
    return code, buffer.getvalue().strip(), seen


def pending_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/tasks/reviewing", request.url
    return httpx.Response(200, json=[CARD])


def test_a_pass_verdict_is_sent_without_findings_and_the_card_id_addresses_the_endpoint():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/reviewing":
            return pending_handler(request)
        posted.append(json.loads(request.content))
        assert request.headers["x-acp-principal"] == "owner-1"
        return httpx.Response(
            200,
            json={
                "card_status": "DELIVERED",
                "verdict": "APPROVE",
                "accepted": True,
                "delivery": {"package_path": "results/delivery_package.json"},
                "next": "交付包已组装",
            },
        )

    code, out, seen = run_cli(handler, "submit", "T01", "--verdict", "pass", "--feature-request", REQUEST)
    assert code == 0, out
    report = json.loads(out)
    assert report["card_status"] == "DELIVERED" and report["accepted"] is True
    assert posted == [
        {"feature_request_id": REQUEST, "verdict": "pass", "evidence": ""}
    ], posted
    assert [request.url.path for request in seen] == ["/tasks/reviewing", "/tasks/T01/review"]


def test_changes_requested_names_the_failed_criteria_and_marks_the_rest_passed():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks/reviewing":
            return pending_handler(request)
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"card_status": "CHANGES_REQUESTED", "accepted": False})

    code, out, _ = run_cli(
        handler,
        "submit",
        "T01",
        "--verdict",
        "changes_requested",
        "--criterion",
        "首屏有公司名",
        "--evidence",
        "首屏为空",
    )
    assert code == 0, out
    findings = posted[0]["findings"]
    assert [(row["criterion"], row["met"]) for row in findings] == [
        ("首屏有公司名", False),
        ("样式文件被引用", True),
    ], findings
    assert all(row["evidence"] == "首屏为空" for row in findings)


def test_a_card_that_is_not_pending_is_refused_without_posting():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    code, out, seen = run_cli(handler, "submit", "T99", "--verdict", "pass")
    assert code == 2, out
    assert "不在待评审列表" in out
    assert [request.method for request in seen] == ["GET"]


def test_an_unknown_verdict_word_is_a_usage_error_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调到
        raise AssertionError("命令行不成立时不许发请求")

    code, out, seen = run_cli(handler, "submit", "T01", "--verdict", "maybe")
    assert code == 2 and "不认识结论" in out
    assert seen == []


def test_pending_prints_the_rows_it_reads():
    code, out, seen = run_cli(pending_handler, "pending", "--feature-request", REQUEST)
    assert code == 0, out
    assert json.loads(out)[0]["task_id"] == "T01"
    assert seen[0].url.params["feature_request_id"] == REQUEST
