"""Phase 15 第三步的判据（owner 2026-10-22 裁决 ③：网页点批准要真的把大模型跑起来）。

**本文件零库、零出网、零厂商**：`WebResumer` 的三条下游（`ensure_archive_root` / `run_resume` /
`run_real`）全部换成记账替身，会话工厂换成假会话。真跑归 owner 的终端，这里量的是**接线与次序**。

四组各量一件，缺一组就会出现一种"看起来接上了"的假绿：

1. **装配面**（`serve.build_app`）：默认注入了、可被显式覆盖、总线根的缺省值只有一个出处。
2. **调用面**（`api/approvals.py`）：续跑者进的是 `BackgroundTasks`，不是内联调用 ——
   内联的话看板点一下要转 900 秒（厂商默认超时），而 HTTP 面上一点异常都看不到。
3. **执行面**（`serve_resumer.WebResumer`）：批准走"恢复 → 跑卡"，驳回只恢复；失败写留痕且**不抛**；
   同一张需求单的第二发是跳过而不是并跑。
4. **提交面**（`realrun.run_real`）：派发之后、厂商之前的那一拍提交点是**调用方给的**
   （CLI 不传 ⇒ 整发一个事务，口径一字未变）；看板传 `session.commit` 才看得见"卡变青"。
"""

from __future__ import annotations

import ast
import datetime as dt
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp import serve, serve_resumer as sr

NOW = dt.datetime(2026, 10, 22, 15, 0, tzinfo=dt.timezone.utc)
SRC = Path(__file__).resolve().parents[2] / "src" / "acp"


# ---------------------------------------------------------------- 假件


class FakeSession:
    """只回答 `get`（需求单）与记账的四件事；`events.record` 要的 `add/flush` 也在这里。"""

    def __init__(self, request: object | None) -> None:
        self.request = request
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(self, model, key):
        return self.request

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def fake_report() -> SimpleNamespace:
    return SimpleNamespace(ticket_state="APPROVED", waiting_after=())


@pytest.fixture
def wired(monkeypatch):
    """把 `WebResumer` 的三条下游换成记账替身，返回 (resumer, calls, sessions, lines)。"""

    calls: list[tuple] = []
    sessions: list[FakeSession] = []
    lines: list[str] = []
    script: dict[str, object] = {}

    def factory() -> FakeSession:
        session = FakeSession(SimpleNamespace(project_key="demo"))
        sessions.append(session)
        return session

    def ensure(project_key, bus_root, **kwargs):
        calls.append(("ensure", project_key, str(bus_root)))

    def resume(feature_request_id, **kwargs):
        calls.append(("resume", feature_request_id, kwargs["transport"]))
        hook = script.get("resume")
        if hook is not None:
            return hook(feature_request_id, **kwargs)
        return fake_report()

    def run_cards(session, **kwargs):
        calls.append(("cards", kwargs["feature_request_id"], kwargs["project_key"]))
        calls.append(("commit_hook", kwargs.get("commit_after_dispatch")))
        hook = script.get("cards")
        if hook is not None:
            return hook(session, **kwargs)
        return [SimpleNamespace(card_id="T01", validated_status="VALID", error=None)]

    monkeypatch.setattr(sr.realrun, "ensure_archive_root", ensure)
    monkeypatch.setattr(sr.run, "run_resume", resume)
    monkeypatch.setattr(sr.realrun, "run_real", run_cards)

    monkeypatch.setattr(sr.WebResumer, "_waiting_nodes", lambda self, *args, **kwargs: ())
    resumer = sr.WebResumer(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lines.append,
    )
    return resumer, calls, sessions, lines, script


def trace_rows(sessions: list[FakeSession]) -> list:
    return [row for session in sessions for row in session.added]


def actions_of(sessions: list[FakeSession]) -> list[str]:
    return [str(getattr(row, "payload", {}).get("action")) for row in trace_rows(sessions)]


def reset(resumer: sr.WebResumer) -> None:
    resumer._locks.clear()


# ---------------------------------------------------------------- 一、装配面


@pytest.fixture
def served(tmp_path, monkeypatch):
    """`serve.main` 走真参数解析，只把会话工厂换成假工厂（口径同 `test_realrun_and_machine_principal.py`）。"""

    monkeypatch.setattr(serve, "make_session_factory", lambda: (lambda: FakeSession(None)))

    def launch(*flags: str):
        captured: dict[str, object] = {}

        def runner(app, **kwargs):
            captured["app"] = app

        code = serve.main(["--api-only", "--dist", str(tmp_path), "--port", "8799", *flags], runner=runner)
        assert code == 0 and "app" in captured
        return captured["app"]

    return launch


def test_serve_injects_graph_resumer(served):
    """owner 点名的这一条：`serve.build_app()` 装配出来的 app 上**必须**有一个可调用的续跑者。

    这条红的表现就是 owner 撞到的那个"停摆"：端点读到 `None` ⇒ 如实回 `resumed_from=null`，
    结论落库了、图还挂着。断言不止"不是 None"：类型也是它 —— 一个被误注成别的可调用对象的
    `graph_resumer` 会让端点照常调度，然后在后台默默失败。
    """
    app = served()
    resumer = getattr(app.state, "graph_resumer", None)
    assert resumer is not None, "启动面没注 graph_resumer ⇒ 点批准只改库、不起工人"
    assert isinstance(resumer, sr.WebResumer), f"注进来的不是生产续跑者：{type(resumer)!r}"
    assert callable(resumer)
    assert Path(resumer.bus_root) == serve.REPO_ROOT / "results", (
        f"总线根的缺省值应该是 <仓库根>/results（与 acp.realrun 同拼法），读到 {resumer.bus_root}"
    )

    explicit = served("--bus-root", str(Path("C:/tmp/explicit-bus")))
    assert Path(explicit.state.graph_resumer.bus_root) == Path("C:/tmp/explicit-bus")


def test_an_explicit_resumer_overrides_the_default(tmp_path, monkeypatch):
    """判据要能换替身（否则每一条都得起真厂商）；覆盖位存在且生效。"""
    monkeypatch.setattr(serve, "make_session_factory", lambda: (lambda: FakeSession(None)))
    marker = lambda **kwargs: None  # noqa: E731 - 只当身份用
    app = serve.build_app(dist=None, resumer=marker)
    assert app.state.graph_resumer is marker


# ---------------------------------------------------------------- 二、调用面


def test_the_endpoint_schedules_the_resumer_in_a_background_task():
    """`_resume` 里 MUST 是 `background.add_task(resumer, ...)`，MUST NOT 直接调 `resumer(...)`。

    这条是"HTTP 不被续跑拖住"的结构面：厂商默认 900 秒，内联调用会让看板那一下转到超时，
    而响应体里还会写着"已续跑"。行为面那一半在 `tests/integration/test_approval_api.py`
    的 `test_the_response_is_sent_before_the_resumer_finishes`（真驱动 ASGI，量"响应先出、续跑还在跑"）。
    """
    tree = ast.parse((SRC / "api" / "approvals.py").read_text(encoding="utf-8"))
    fn = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_resume"),
        None,
    )
    assert fn is not None, "取不到 _resume"
    direct = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "resumer"
    ]
    assert not direct, "续跑者被内联调用了（HTTP 会等它跑完厂商）"
    scheduled = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_task"
    ]
    assert scheduled, "没找到 background.add_task(...) ⇒ 这一发根本没安排续跑"
    assert any(isinstance(arg, ast.Name) and arg.id == "resumer" for call in scheduled for arg in call.args), (
        "add_task 的第一个实参不是 resumer"
    )


# ---------------------------------------------------------------- 三、执行面


def test_the_web_resumer_resumes_then_runs_the_cards(wired):
    """批准的次序 = 建现场 → 恢复图 → 跑卡，且跑卡那一发拿到**提交点**、会话正常提交并关掉。"""
    resumer, calls, sessions, _, script = wired
    script["resume"] = None
    reset(resumer)
    resumer(feature_request_id="0d1f8a54-1111-4c1e-9a3b-000000000001", conclusion="approve")

    verbs = [item[0] for item in calls if item[0] != "commit_hook"]
    assert verbs == ["ensure", "resume", "cards"], f"次序不对：{calls}"
    hook = next(item[1] for item in calls if item[0] == "commit_hook")
    assert hook is not None, "run_real 没收到提交点 ⇒ 派发要等厂商跑完才可见（看板会像停摆）"

    work = [session for session in sessions if session.commits and session.added == []]
    assert work, "主工作单元没有提交"
    assert all(session.closed for session in sessions), "有会话没还"
    assert not any(session.rollbacks for session in sessions), "成功路径不该回滚"


def test_a_uuid_identifier_goes_through_as_is(wired):
    """生产形态是 **UUID 对象**（`ApprovalTicket.feature_request_id` 是 `UUID(as_uuid=True)`）。

    这一条是缺陷回归：`uuid.UUID(x)` 在 x 本身是 UUID 时会去找 `.replace` ⇒ 后台线程炸掉，
    看板上的表现与 owner 报的停摆同形（"点批准后什么都没发生"）。判据只喂字符串时这一格是绿的。
    """
    resumer, calls, sessions, _, _ = wired
    reset(resumer)
    fid = uuid.uuid4()
    resumer(feature_request_id=fid, conclusion="reject")
    assert [item[0] for item in calls] == ["ensure", "resume"], calls
    assert calls[1][1] == fid, "交给 run_resume 的不是原对象"
    rows = trace_rows(sessions)
    assert rows and all(row.payload["feature_request_id"] == str(fid) for row in rows), (
        f"留痕里的需求单号不对：{[row.payload for row in rows]}"
    )


def test_a_reject_resumes_but_never_runs_the_cards(wired):
    """驳回也要恢复图（让请求落 REJECTED 的终态），但**不跑卡** —— 跑卡是"批准之后"的事。"""
    resumer, calls, _, _, _ = wired
    reset(resumer)
    resumer(feature_request_id="0d1f8a54-2222-4c1e-9a3b-000000000002", conclusion="reject")
    assert [item[0] for item in calls] == ["ensure", "resume"], f"驳回也跑了卡：{calls}"


def test_a_failed_resume_is_recorded_in_the_activity_log_and_never_escapes(wired):
    """后台任务抛出去只剩 uvicorn 一条日志；失败 MUST 落成一行 EN-14（看板读得到），且不往外抛。"""
    resumer, _, sessions, lines, script = wired

    def boom(feature_request_id, **kwargs):
        raise RuntimeError("厂商把连接掐了")

    script["resume"] = boom
    reset(resumer)
    resumer(feature_request_id="0d1f8a54-3333-4c1e-9a3b-000000000003", conclusion="approve")

    rows = trace_rows(sessions)
    actions = [row.payload.get("action") for row in rows]
    assert "resume_started" in actions and "resume_failed" in actions, f"留痕不全：{actions}"
    failed = next(row for row in rows if row.payload["action"] == "resume_failed")
    assert failed.kind == "APPROVAL" and failed.entity == "feature_request"
    assert failed.payload["code"] == "RuntimeError"
    assert failed.actor_agent_id == sr.RESUME_ACTOR, "留痕的主体列不是编排器（acl 那一份）"
    assert any("RESUME fail" in line for line in lines), "诊断侧没留一行"


def test_a_missing_waiting_point_is_skipped_not_failed(wired):
    """第二次之后的批准没有等待点可恢复 —— 那是常态（评审通过让下游卡变 READY），记 skip 不记 fail。"""
    resumer, _, sessions, _, script = wired

    def no_waiting(feature_request_id, **kwargs):
        raise sr.run.RunFailure("thread 上没有等待点", code=sr.run.NO_WAITING_POINT)

    script["resume"] = no_waiting
    reset(resumer)
    resumer(feature_request_id="0d1f8a54-4444-4c1e-9a3b-000000000004", conclusion="approve")

    actions = [row.payload.get("action") for row in trace_rows(sessions)]
    assert actions[-1] == "resume_skipped", f"没有等待点被记成了失败：{actions}"


def test_a_second_trigger_for_the_same_request_waits_its_turn_instead_of_running_twice(wired):
    """同一张需求单的第二个触发不并跑。并跑的表现是两条链各自派发同一批卡，归因起来两边都不像。"""
    resumer, calls, _, _, script = wired
    started = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    def slow(feature_request_id, **kwargs):
        seen.append("enter")
        started.set()
        assert release.wait(5), "测试没放行"
        seen.append("leave")
        return fake_report()

    script["resume"] = slow
    reset(resumer)
    first = threading.Thread(
        target=lambda: resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000005", conclusion="approve")
    )
    first.start()
    assert started.wait(5), "第一条链没起来"
    resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000005", conclusion="approve")
    release.set()
    first.join(10)
    assert not first.is_alive()
    assert seen == ["enter", "leave"], f"两条链并跑了：{seen}"
    assert [item[0] for item in calls].count("resume") == 1, f"同一张需求单恢复了两遍：{calls}"


# ---------------------------------------------------------------- 四、提交面


def test_continue_mode_skips_the_consumed_waiting_point_and_only_runs_cards(wired):
    """评审通过之后的续跑：图上的等待点已经用掉，再恢复只会得到 `no_waiting_point`。

    这一段 MUST 只跑 `run_real`（已解锁的 READY 卡），且仍然走同一个提交点与同一个锁。
    """
    resumer, calls, sessions, _lines, _script = wired
    resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000006", conclusion="continue")
    names = [item[0] for item in calls]
    assert "resume" not in names, f"continue 模式又调了恢复段：{calls}"
    assert names.count("cards") == 1, f"continue 模式没有跑卡：{calls}"
    assert actions_of(sessions).count("resume_finished") == 1


def test_continue_resumes_when_the_gate_is_still_waiting(wired):
    """P1-3：`continue` 不能无条件跳过恢复段。

    现场：单据已经 APPROVED，但图还停在 `approval_gate`（resume 上一次失败/进程被杀）。
    这时 `resolve` 已无单可批、`continue` 若继续跳过 `run_resume`，卡就永久停在
    `PENDING_APPROVAL`。判据：`next` 里还有卡点 ⇒ MUST 先恢复，再跑卡。
    """
    resumer, calls, sessions, _lines, _script = wired
    resumer._waiting_nodes = lambda *args, **kwargs: ("approval_gate",)

    resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000007", conclusion="continue")

    names = [item[0] for item in calls]
    assert names.count("resume") == 1, f"还有卡点却没恢复：{calls}"
    assert names.count("cards") == 1, f"恢复之后没有跑卡：{calls}"


def test_run_real_asks_the_caller_for_the_commit_point_between_dispatch_and_the_vendor():
    """`commit_after_dispatch` 的三条口径：缺省不提交（CLI 不变）、位置在派发后/跑卡前、CLI 两处都不传。"""
    source = (SRC / "realrun.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run_real"),
        None,
    )
    assert fn is not None, "取不到 run_real"
    names = [arg.arg for arg in fn.args.kwonlyargs]
    assert "commit_after_dispatch" in names, "run_real 没有这个注入位"
    default = fn.args.kw_defaults[names.index("commit_after_dispatch")]
    assert isinstance(default, ast.Constant) and default.value is None, "缺省值不是你给的那个 None"

    body = ast.get_source_segment(source, fn) or ""
    dispatch_at = body.index("run.run_assign(")
    hook_at = body.index("commit_after_dispatch()")
    cards_at = body.index("run_card_segment(")
    assert dispatch_at < hook_at < cards_at, "提交点不在「派发完、厂商还没开始」那一拍"

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "run_real")
            or (isinstance(node.func, ast.Name) and node.func.id == "run_real")
        )
        and node.lineno > fn.lineno
    ]
    passed = [
        keyword.arg
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "commit_after_dispatch"
    ]
    assert calls, "没找到任何 run_real 的调用点 ⇒ 这条判据在空转"
    assert passed == [], "CLI 调用点不该传提交点（口径：整发一个事务）"


def test_the_resumer_reads_the_request_through_the_shared_session_factory(wired):
    """会话工厂必须是与 app 共用的那一个（`serve.build_app` 只建一次引擎）—— 这里量"是同一个对象"。"""
    sentinel = object()

    def factory():
        return sentinel

    resumer = sr.WebResumer(session_factory=factory, bus_root=Path("C:/tmp/x"))
    assert resumer.session_factory is factory

    source = (SRC / "serve.py").read_text(encoding="utf-8")
    assert source.count("make_session_factory()") == 1, (
        "`build_app` 里建了两个工厂/引擎 ⇒ 续跑与端点不共用池子（且 `graph_resumer` 拿到的不是同一个）"
    )


# ---------------------------------------------------------------- 五、Phase 17 P2：审查跑者的模式门与触发位


def test_manual_mode_injects_no_review_runner(served, monkeypatch):
    """`ACP_REVIEW_MODE=manual`（缺省）时 Phase 16 行为逐字不变：连跑者对象都不存在。

    这条是 owner P2 判据 "manual 不触发 ReviewRunner" 的装配面那一半；运行面那一半由
    `test_the_review_runner_runs_after_the_session_commit_and_after_the_lock_is_released` 钉。
    """
    from acp.review.runner import ReviewRunner

    monkeypatch.setenv("ACP_REVIEW_MODE", "manual")
    app = served()
    resumer = app.state.graph_resumer
    assert not isinstance(resumer, ReviewRunner)
    assert getattr(resumer, "review_runner", None) is None, (
        "manual 模式注入了审查跑者 ⇒ 切模式之前就停在 REVIEWING 的卡会被追溯"
    )


def test_an_unknown_review_mode_is_refused_before_any_runner_is_built():
    with pytest.raises(serve.ReviewModeInvalid, match="sometimes"):
        serve.resolve_review_mode("sometimes")


def test_deepseek_mode_requires_the_key_and_names_the_variable(monkeypatch):
    from acp.adapters.deepseek import review as deepseek_review

    monkeypatch.delenv(deepseek_review.API_KEY_ENV, raising=False)
    with pytest.raises(deepseek_review.ReviewConfigError) as exc:
        serve.resolve_review_runner(
            "deepseek", session_factory=lambda: None, bus_root=Path("C:/tmp/archives")
        )
    assert deepseek_review.API_KEY_ENV in str(exc.value)
    assert "unit-test-placeholder" not in str(exc.value)


def test_deepseek_mode_builds_a_review_runner_with_the_injected_client():
    from acp.review.runner import ReviewRunner

    def client(request):
        raise AssertionError("构造期不该调用模型")

    runner = serve.resolve_review_runner(
        "deepseek",
        session_factory=lambda: None,
        bus_root=Path("C:/tmp/archives"),
        client=client,
    )
    assert isinstance(runner, ReviewRunner)
    assert runner.bus_root == Path("C:/tmp/archives")


def test_the_review_runner_runs_after_the_session_commit_and_after_the_lock_is_released(wired):
    """触发位两件事一起量：**提交之后**（否则 runner 用另一条连接看不到 REVIEWING）
    且 **per-feature 锁已释放**（否则 PASS 之后的续跑会自己撞 `already_running`）。
    """
    resumer, _calls, sessions, _lines, script = wired
    script["cards"] = lambda session, **kwargs: [
        SimpleNamespace(card_id="T01", validated_status="VALID", card_status="REVIEWING", error=None)
    ]
    seen: dict[str, object] = {}

    def fake_review(*, feature_request_id, card_ids):
        seen["lock_held"] = resumer._lock_for(str(feature_request_id)).locked()
        seen["committed"] = any(item.commits for item in sessions)
        seen["card_ids"] = card_ids
        return SimpleNamespace(continue_feature_ids=())

    resumer.review_runner = fake_review
    resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000007", conclusion="approve")

    assert seen == {"lock_held": False, "committed": True, "card_ids": ("T01",)}, seen


def test_a_review_followup_runs_continue_only_after_the_lock_is_released(wired):
    """PASS 且还有下游卡时，`WebResumer` MUST 在锁释放之后递归调用 `conclusion='continue'`。

    递归那一发要重新拿锁；锁不是可重入的 ⇒ 在持锁时调用只会得到 `resume_skipped/already_running`，
    而"看起来跑完了"和"续跑被自己跳过"在活动流里长得完全一样。
    """
    resumer, calls, _sessions, _lines, script = wired
    fid = "0d1f8a54-5555-4c1e-9a3b-000000000008"
    script["cards"] = lambda session, **kwargs: [
        SimpleNamespace(card_id="T01", validated_status="VALID", card_status="REVIEWING", error=None)
    ]
    seen: list[bool] = []

    def fake_review(*, feature_request_id, card_ids):
        seen.append(resumer._lock_for(str(feature_request_id)).locked())
        return SimpleNamespace(continue_feature_ids=(uuid.UUID(str(feature_request_id)),) if len(seen) == 1 else ())

    resumer.review_runner = fake_review
    resumer(feature_request_id=fid, conclusion="approve")

    assert seen == [False, False], f"审查跑者在持锁时被调用了：{seen}"
    assert [item[0] for item in calls].count("cards") == 2, f"续跑没有发生：{calls}"


def test_manual_mode_never_enters_the_review_path(wired, monkeypatch):
    """Running-level companion to the injection test: with no runner, `_review` must not even be entered.

    An empty runner object is not the same as "the path is never taken"; a future refactor could call
    `_review` and leave the guard inside it. This test pins the call site itself.
    """
    resumer, _calls, _sessions, _lines, script = wired
    script["cards"] = lambda session, **kwargs: [
        SimpleNamespace(card_id="T01", validated_status="VALID", card_status="REVIEWING", error=None)
    ]
    entered: list[bool] = []
    monkeypatch.setattr(resumer, "_review", lambda *args, **kwargs: entered.append(True) or ())

    assert resumer.review_runner is None
    resumer(feature_request_id="0d1f8a54-5555-4c1e-9a3b-000000000009", conclusion="approve")
    assert entered == [], "manual 模式仍进了审查路径 ⇒ 切模式前停在 REVIEWING 的卡会被追溯"
