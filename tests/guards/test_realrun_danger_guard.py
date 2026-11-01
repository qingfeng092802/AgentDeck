"""P0-2：`realrun._refuse_dangerous_task()` 在下发前拦住危险动作。

这里不碰 PG：`guard.autonomy.effective_mode()` 被替换成固定档位，`hold_ticket()` 被替换成
一个记账替身。判据只问三件事：

1. 任务文本里出现 `rm -rf` / `drop table` ⇒ 抛 `DangerActionBlocked`，且 code 稳定；
2. 拦下之前 MUST 建 D 类单（调用 `hold_ticket`），不是只抛一个异常；
3. 普通任务不拦 —— 否则每张卡都停在门口，等于把执行链关死。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from acp import realrun
from acp.guard import autonomy as guard_autonomy


def _task(objective: str, criteria: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        objective=objective,
        inputs=[],
        acceptance_criteria=criteria,
        constraints=[],
    )


def _call(task: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(guard_autonomy, "effective_mode", lambda *args, **kwargs: "FULL")
    realrun._refuse_dangerous_task(
        object(),
        task=task,
        task_id="T01",
        feature_request_id=uuid.uuid4(),
        project_key="demo",
        executor="qoder",
    )


def test_a_dangerous_task_is_refused_and_creates_a_d_ticket(monkeypatch) -> None:
    held: list[object] = []
    monkeypatch.setattr(
        guard_autonomy,
        "hold_ticket",
        lambda session, decision: held.append(decision) or SimpleNamespace(ticket_id="ticket-1"),
    )

    with pytest.raises(realrun.DangerActionBlocked) as exc:
        _call(_task("执行 rm -rf /tmp/build", ["drop table task_card"]), monkeypatch)

    assert exc.value.code == "danger_action_blocked"
    assert exc.value.ticket_id == "ticket-1"
    assert exc.value.hits, "命中条目必须点名，不能只说'危险'"
    assert held, "拦下之前 MUST 走 hold_ticket 建 D 类单，否则人没有可批的东西"


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("执行 sh -c 'rm -rf /'", "rm -rf"),
        ("执行 psql -c 'truncate table task_card'", "truncate table"),
    ],
    ids=["shell-c", "psql-c"],
)
def test_a_wrapped_command_still_creates_a_d_ticket(monkeypatch, text: str, pattern: str) -> None:
    """P0-2：包装器里的命令也要走同一个 D 类出口，不是只抛一个异常。"""
    held: list[object] = []
    monkeypatch.setattr(
        guard_autonomy,
        "hold_ticket",
        lambda session, decision: held.append(decision) or SimpleNamespace(ticket_id="ticket-wrapped"),
    )

    with pytest.raises(realrun.DangerActionBlocked) as exc:
        _call(_task(text, []), monkeypatch)

    assert pattern in exc.value.hits
    assert held, "包装器命中时同样必须先建 D 类单"


def test_a_benign_task_is_not_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        guard_autonomy,
        "hold_ticket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不该建单")),
    )

    _call(_task("做一个三页面的产品介绍站", ["首页有公司名", "产品页可跳转"]), monkeypatch)
