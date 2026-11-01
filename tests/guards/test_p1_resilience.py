"""P1-13：网络抖动与子进程树的闭环。"""

from __future__ import annotations

import httpx
import pytest

from acp import realrun
from acp.adapters.workbuddy import transport as wb
from acp.adapters.qoder import transport as qt


def test_workbuddy_retries_connect_error_twice_then_returns_the_response() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"Response": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        response = wb._post_with_retry(
            client,
            "https://workbuddy.example.invalid",
            content=b"{}",
            headers={"content-type": "application/json"},
            timeout=1.0,
            backoff=(0.0, 0.0),
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert len(calls) == 3, f"应重试 2 次（共 3 发），实际 {len(calls)}"


def test_workbuddy_reraises_after_three_failed_attempts() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("still down", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.ConnectError):
            wb._post_with_retry(
                client,
                "https://workbuddy.example.invalid",
                content=b"{}",
                headers={},
                timeout=1.0,
                backoff=(0.0, 0.0),
            )
    finally:
        client.close()

    assert len(calls) == 3


def test_connect_error_is_swallowable_per_card() -> None:
    """一次网络抖动只该坏一张卡，不该把整条需求单打断。"""
    assert realrun._swallowable(httpx.ConnectError("reset"))


def test_kill_process_tree_uses_taskkill_on_windows() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))

    process = type("P", (), {"pid": 4321, "killed": False, "kill": lambda self: setattr(self, "killed", True)})()
    qt._kill_process_tree(
        process,
        is_windows=True,
        runner=fake_run,
    )

    assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]], calls
    assert process.killed is True, "taskkill 之后仍要兜底 kill 一次"
