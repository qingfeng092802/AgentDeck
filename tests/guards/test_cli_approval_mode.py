"""P1-8：CLI 也要吃 `ACP_APPROVAL_MODE=auto`，不能让同一条 `.env` 在两个入口下有不同表现。

网页那条路在 `serve.build_app` 启动期解析一次、由 `serve_intake._auto_pass_split` 执行；
CLI 这条路的判据量的是**同一件事**：档位读的是同一个变量、免确认走的是同一个仓储入口、
署名的是同一个规则名。差别只有"谁来读环境"。

这里不打库：`repo.apply_auto_pass` 换成记账替身。真正的落库语义由
`tests/integration/test_approval_ticket.py` 的 `apply_auto_pass` 那几条管着，不重复。
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from acp import run as cli
from acp.approval import policy as approval_policy


class _Outcome:
    def __init__(self, applied: bool) -> None:
        self.applied = applied


def _report() -> cli.IntakeReport:
    return cli.IntakeReport(
        feature_request_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        card_ids=("T01", "T02"),
        request_status="INTAKE",
    )


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake(session, *, ticket_id, decided_by, now=None):  # noqa: ANN001 - 替身签名照被测函数
        calls.append({"session": session, "ticket_id": ticket_id, "decided_by": decided_by, "now": now})
        return _Outcome(True)

    monkeypatch.setattr(cli.repo, "apply_auto_pass", fake)
    return calls


def test_without_the_variable_the_cli_stays_on_the_manual_path(
    monkeypatch: pytest.MonkeyPatch, applied: list[dict]
) -> None:
    monkeypatch.delenv(approval_policy.APPROVAL_MODE_ENV, raising=False)
    assert cli._auto_pass_split(object(), _report(), now=dt.datetime.now(dt.timezone.utc)) is False
    assert applied == [], "没设开关却动了审批状态"


def test_manual_says_no_and_touches_nothing(monkeypatch: pytest.MonkeyPatch, applied: list[dict]) -> None:
    monkeypatch.setenv(approval_policy.APPROVAL_MODE_ENV, "manual")
    assert cli._auto_pass_split(object(), _report(), now=dt.datetime.now(dt.timezone.utc)) is False
    assert applied == []


def test_auto_signs_with_the_same_rule_name_as_the_web(
    monkeypatch: pytest.MonkeyPatch, applied: list[dict]
) -> None:
    """规则名与网页侧**逐字相同**：`AUTO` 通道的 `reason_text` 靠它区分"哪条规则免的"。"""
    monkeypatch.setenv(approval_policy.APPROVAL_MODE_ENV, "auto")
    report = _report()
    now = dt.datetime.now(dt.timezone.utc)
    assert cli._auto_pass_split(object(), report, now=now) is True
    assert len(applied) == 1
    assert applied[0]["ticket_id"] == report.ticket_id
    assert applied[0]["decided_by"] == approval_policy.SPLIT_AUTO_RULE
    assert applied[0]["now"] == now


@pytest.mark.parametrize("raw", ["AUTO", " auto ", "Auto"])
def test_the_value_is_normalised_like_the_web_side(
    monkeypatch: pytest.MonkeyPatch, applied: list[dict], raw: str
) -> None:
    monkeypatch.setenv(approval_policy.APPROVAL_MODE_ENV, raw)
    assert cli._auto_pass_split(object(), _report(), now=dt.datetime.now(dt.timezone.utc)) is True


def test_an_unknown_value_is_a_loud_failure_not_a_silent_manual(
    monkeypatch: pytest.MonkeyPatch, applied: list[dict]
) -> None:
    """认不出的取值不降级：那会让"我以为它开着"和"它其实关着"变成同一屏。"""
    monkeypatch.setenv(approval_policy.APPROVAL_MODE_ENV, "aotu")
    with pytest.raises(cli.RunFailure) as caught:
        cli._auto_pass_split(object(), _report(), now=dt.datetime.now(dt.timezone.utc))
    assert caught.value.code == "approval_mode_invalid"
    assert applied == []


def test_a_ticket_someone_else_already_decided_falls_back_to_waiting(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """免确认没成 != 整单失败：单据可能已被批过，那一发回到"等人批"的老路。"""
    monkeypatch.setenv(approval_policy.APPROVAL_MODE_ENV, "auto")
    monkeypatch.setattr(cli.repo, "apply_auto_pass", lambda *a, **k: _Outcome(False))
    assert cli._auto_pass_split(object(), _report(), now=dt.datetime.now(dt.timezone.utc)) is False


def test_the_intake_branch_actually_calls_it() -> None:
    """接线判据：`main()` 的受理分支必须**真的**调用它，并且签成之后接着续跑。

    这一条用源码而不是打库：`main()` 要真跑到那一格需要一次完整受理（建单 + 拆单 + 建卡），
    那是 integration 档的事；这里只要钉住"接线没有在某次重构里被摘掉"。
    """
    import inspect

    source = inspect.getsource(cli.main)
    assert "if _auto_pass_split(session, report, now=now):" in source, "受理分支没接免确认"
    assert '"AUTO"' in source, "免确认之后没有那行读数 ⇒ 用户看不出它是自动过的"
    assert "report = run_resume(" in source, "签成之后没有续跑 ⇒ 与网页那条路表现不一致"
