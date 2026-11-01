"""Phase 16 第一格：网页受理之后**真的把图推到 A 类卡点**。

`POST /requests` 此前只写 EN-01 那一行，网页提交完永远没有任务卡与 A_SPLIT 单据；而 CLI 的受理段
会再建一张需求单 ⇒ 网页入口不能靠重跑 CLI 续上。这一组量四件事，缺一格就会出现一种"看起来接上了"的假绿：

1. **装配面**：`serve.build_app` 默认注入了 `intake_runner`，可显式覆盖，总线根与续跑者同源。
2. **调用面**：`POST /requests` 把跑者放进 `BackgroundTasks`，不是内联 —— 内联会把 201 拖到拆解完成之后；
   并且只在装配方真的注入了跑者时才排（裸 app 的 201 只代表那一行落了库）。
3. **执行面**：跑者调 `run_intake_existing`（同一张需求单），成功写 `split_ready` 留痕，
   失败写 `split_failed` 且**不抛**；同一张单的第二次触发是跳过而不是并跑。
4. **边界面**：`run_intake_existing` 不建第二张 EN-01 行、不重跑补全 —— 它只把已落库的那一张推进去。

本文件零库、零出网、零厂商：会话工厂换成记账假件，图与厂商都在替身那一侧。
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from acp import serve, serve_intake as si
from acp.api import requests as requests_api
from acp.orchestrate import llm_split

NOW = dt.datetime(2026, 10, 22, 18, 0, tzinfo=dt.timezone.utc)
SRC = Path(__file__).resolve().parents[2] / "src" / "acp"
FID = uuid.uuid4()


class FakeSession:
    def __init__(self, request: object | None = None) -> None:
        self.request = request if request is not None else SimpleNamespace(project_key="demo")
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

    def query(self, model):  # noqa: ANN001, ANN201
        """`WebIntakeRunner._card_count()` 那一条要用 `session.query(TaskCard)…count()`。

        这一组判据跑的都是**第一趟**受理（库里还没有卡），所以恒 0 是正确读数、不是桩：
        "这张单上已经有卡"那一条分叉（追加需求：只刷新 PRD、不重拆）在这里本来就不该走到。
        真要量那条分叉，得给它一张有卡的场面 —— 而不是把这个读数改成能返回卡。
        """
        return _NoRows()


class _NoRows:
    """空结果集：链式调用照原样返回，`count()` 恒 0、`all()` 恒空。"""

    def filter(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self

    def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self

    def limit(self, count: int):  # noqa: ANN201
        return self

    def count(self) -> int:
        return 0

    def all(self):  # noqa: ANN201
        return []


def rows_of(sessions: list[FakeSession]) -> list:
    return [row for session in sessions for row in session.added]


def actions(sessions: list[FakeSession]) -> list[str]:
    return [str(getattr(row, "payload", {}).get("action")) for row in rows_of(sessions)]


class _RowsSession:
    """只喂 `_last_failure` 那一支的最小会话：它要的是"查出来的行"，不是数据库。

    为什么不用真库：这条判据量的是"码有没有被改口径"，与 SQL 方言无关；
    真库会让它从"契约判据"变成"需要一个 PG 的集成判据"（而这一组文件头写着零库）。
    `order_by` 在这里**只做倒序**：真实查询是 `created_at desc`，假件按插入顺序反过来即等价。
    """

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def query(self, model):  # noqa: ANN001, ANN201
        outer = self

        class _Q:
            def __init__(self) -> None:
                self.rows = [row for row in outer._rows if isinstance(row, model)]

            def filter(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                return self

            def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
                self.rows.reverse()
                return self

            def limit(self, count: int):  # noqa: ANN201
                self.rows = self.rows[:count]
                return self

            def all(self):  # noqa: ANN201
                return list(self.rows)

        return _Q()


@pytest.fixture
def wired(monkeypatch):
    calls: list[tuple] = []
    sessions: list[FakeSession] = []
    lines: list[str] = []
    script: dict[str, object] = {}

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    def ensure(project_key, bus_root, **kwargs):
        calls.append(("ensure", project_key, str(bus_root)))

    def advance(feature_request_id, **kwargs):
        calls.append(("advance", str(feature_request_id), str(kwargs["bus_root"])))
        hook = script.get("advance")
        if hook is not None:
            return hook(feature_request_id, **kwargs)
        return SimpleNamespace(ticket_id=uuid.uuid4(), card_ids=("T01", "T02"), request_status="SPLIT")

    monkeypatch.setattr(si.realrun, "ensure_archive_root", ensure)
    monkeypatch.setattr(si.run, "run_intake_existing", advance)
    runner = si.WebIntakeRunner(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lines.append,
    )
    return runner, calls, sessions, lines, script


# ---------------------------------------------------------------- 一、装配面


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "make_session_factory", lambda: (lambda: FakeSession()))

    def launch(*flags: str):
        captured: dict[str, object] = {}

        def runner(app, **kwargs):
            captured["app"] = app

        code = serve.main(["--api-only", "--dist", str(tmp_path), "--port", "8798", *flags], runner=runner)
        assert code == 0 and "app" in captured
        return captured["app"]

    return launch


def test_serve_injects_the_intake_runner(served):
    """`serve.build_app()` 装配出来的 app 上必须有一个可调用的拆解跑者，且类型是生产那一个。"""
    app = served()
    runner = getattr(app.state, "intake_runner", None)
    assert runner is not None, "启动面没注 intake_runner ⇒ 网页受理之后永远没有任务卡"
    assert isinstance(runner, si.WebIntakeRunner), f"注进来的不是生产拆解跑者：{type(runner)!r}"
    assert callable(runner)
    assert Path(runner.bus_root) == serve.REPO_ROOT / "results", (
        f"总线根的缺省值应与续跑者/realrun 同源，读到 {runner.bus_root}"
    )

    explicit_root = served("--bus-root", str(Path("C:/tmp/explicit-bus")))
    assert Path(explicit_root.state.intake_runner.bus_root) == Path("C:/tmp/explicit-bus")


def test_an_explicit_intake_runner_overrides_the_default(monkeypatch):
    monkeypatch.setattr(serve, "make_session_factory", lambda: (lambda: FakeSession()))
    marker = lambda **kwargs: None  # noqa: E731
    app = serve.build_app(dist=None, intake_runner=marker)
    assert app.state.intake_runner is marker


# ---------------------------------------------------------------- 二、调用面


def _calls_of(module: str, function: str, attr: str) -> list[ast.Call]:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    fn = next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function), None)
    assert fn is not None, f"取不到 {function}"
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
    ]


def test_the_scheduling_flag_reports_whether_a_runner_was_actually_scheduled(monkeypatch):
    """回体的 `intake_scheduled` 只读装配面：没注跑者时 MUST 是 False，导了才排任务并报 True。

    这一格防的是"页面按 201 自己编一句正在拆解"：裸装配（判据 / 别的宿主）只写了 EN-01，
    没有任何东西会去拆卡，回体里那个布尔值必须把这件事说穿。
    """
    from fastapi import BackgroundTasks
    from starlette.requests import Request

    from acp.api import requests as api_requests

    monkeypatch.setattr(
        api_requests,
        "plan_requirement",
        lambda raw, *, completer: SimpleNamespace(summary_digest={}),
    )
    monkeypatch.setattr(
        api_requests,
        "_persist",
        lambda request, row: api_requests.StoredRequest(
            request_id=FID,
            project_key="demo",
            status="INTAKE",
            clarification_waived=False,
        ),
    )

    def call(state):
        request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(**state))})
        tasks = BackgroundTasks()
        response = api_requests.create_request(
            api_requests.IntakeRequest(project_key="demo", raw_input="做个站"), request, tasks
        )
        return response, tasks

    bare, bare_tasks = call({"completer": object()})
    assert bare.intake_scheduled is False, "没注跑者却回了「已排拆解」"
    assert bare_tasks.tasks == []

    scheduled: list[dict] = []
    wired_response, wired_tasks = call(
        {"completer": object(), "intake_runner": lambda **kwargs: scheduled.append(kwargs)}
    )
    assert wired_response.intake_scheduled is True
    assert len(wired_tasks.tasks) == 1
    assert wired_tasks.tasks[0].kwargs == {"feature_request_id": FID}


def test_the_request_endpoint_schedules_the_runner_in_a_background_task():
    """`create_request` MUST 排 `background.add_task(runner, ...)`，MUST NOT 内联调用。"""
    tree = ast.parse((SRC / "api" / "requests.py").read_text(encoding="utf-8"))
    fn = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "create_request"),
        None,
    )
    assert fn is not None, "取不到 create_request"
    direct = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "runner"
    ]
    assert not direct, "拆解跑者被内联调用了（201 会等整条图跑完）"
    scheduled = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_task"
    ]
    assert scheduled, "没找到 background.add_task(...) ⇒ 这一发根本没安排拆解"
    assert any(isinstance(arg, ast.Name) and arg.id == "runner" for call in scheduled for arg in call.args), (
        "add_task 的第一个实参不是 runner"
    )
    assert any(
        keyword.arg == "feature_request_id" and isinstance(keyword.value, ast.Attribute)
        for call in scheduled
        for keyword in call.keywords
    ), "排进去的 feature_request_id 不是刚落库的那一张"


def test_the_review_endpoint_schedules_a_continue_after_a_pass():
    """评审通过且还有下游时，端点排的是 `conclusion="continue"`，不是再走一次恢复段。"""
    scheduled = _calls_of("api/tasks.py", "submit_task_review", "add_task")
    assert scheduled, "评审通过之后没有安排继续跑 ⇒ 下游卡停在 READY 没人派"
    conclusions = [
        keyword.value.value
        for call in scheduled
        for keyword in call.keywords
        if keyword.arg == "conclusion" and isinstance(keyword.value, ast.Constant)
    ]
    assert conclusions == ["continue"], f"续跑模式不是 continue：{conclusions}"


# ---------------------------------------------------------------- 三、执行面


def test_the_web_intake_resumes_the_same_request(wired):
    runner, calls, sessions, lines, _ = wired
    runner(feature_request_id=FID)

    assert [item[0] for item in calls] == ["ensure", "advance"], calls
    assert calls[1][1] == str(FID), "推进的不是刚受理的那一张需求单"
    assert actions(sessions).count("split_started") == 1
    assert actions(sessions).count("split_ready") == 1
    assert actions(sessions).count("split_finished") == 1
    assert any("INTAKE done" in line for line in lines)


def test_a_failure_is_traced_and_never_raised(wired):
    runner, _calls, sessions, lines, script = wired

    def boom(feature_request_id, **kwargs):
        raise RuntimeError("图停了")

    script["advance"] = boom
    runner(feature_request_id=FID)  # 不抛
    assert actions(sessions).count("split_failed") == 1
    assert any("INTAKE fail" in line for line in lines)
    failed = [row for row in rows_of(sessions) if getattr(row, "payload", {}).get("action") == "split_failed"]
    assert failed and "图停了" in str(failed[0].payload.get("error"))


def test_a_split_that_needs_a_human_keeps_its_own_code_all_the_way_to_the_read_surface(wired):
    """R-26 交人工：留痕里的码必须是 `split_needs_human`，并且**原样**流到读面。

    钉两处，因为它们是两次独立的机会把码弄丢：

    1. `_run` 的 `except` 取的是 `getattr(exc, "code", ...)` —— 拆单器抛的码若还是最后一次的
       `split_extra_field`，前端那张枚举就得跟着长；
    2. `_last_failure` 读的是 `payload["code"]` —— 它只是搬运，所以**上游码对了它就对了**。
       这一条把"搬运没被改口径"也一起量了（`RequestView.last_error_code` 就是它的返回值）。

    为什么值得单独一条：屏上靠这个码把"这单在等人"与"这单失败了"分成琥珀 / 红两档
    （`web/src/lib/live.ts` 的 `stageOf`）。码一换，那一档静默退回红色失败 —— 而这正是要修的。
    """
    runner, _calls, sessions, lines, script = wired

    def needs_human(feature_request_id, **kwargs):
        raise llm_split.SplitNeedsHuman(
            llm_split.NEEDS_HUMAN_CODE,
            "拆单 2 次都过不了契约（最后一次 split_extra_field：多写了 guards）",
            attempts=2,
            cause_code="split_extra_field",
        )

    script["advance"] = needs_human
    runner(feature_request_id=FID)  # 不抛：后台线程抛出去只剩一条 uvicorn 日志

    assert actions(sessions).count("split_failed") == 1
    assert any("INTAKE fail" in line for line in lines)
    failed = [row for row in rows_of(sessions) if getattr(row, "payload", {}).get("action") == "split_failed"]
    assert failed[0].payload["code"] == "split_needs_human", (
        f"留痕里的码是 {failed[0].payload['code']!r}：诊断码冒充了事件码"
    )
    assert "split_extra_field" in str(failed[0].payload["error"]), "诊断码要留在同一行留痕里"

    code, error = requests_api._last_failure(_RowsSession(list(rows_of(sessions))), FID)
    assert code == "split_needs_human", f"读面把码改了口径：{code!r}"
    assert error and "split_extra_field" in error


def test_the_second_trigger_on_the_same_request_is_skipped(wired):
    runner, calls, sessions, _lines, _ = wired
    lock = runner._lock_for(str(FID))
    assert lock.acquire(blocking=False)
    try:
        runner(feature_request_id=FID)
    finally:
        lock.release()
    assert calls == [], "同一张单已经在跑，第二发仍起了第二个工作单元"
    assert actions(sessions) == ["split_skipped"], actions(sessions)


# ---------------------------------------------------------------- 四、边界面


def test_run_intake_existing_never_builds_a_second_request(monkeypatch):
    """网页入口已经写过 EN-01 了；推进器只搬那一行，不再调 `intake.new_request` / `plan_requirement`。"""
    calls: list[tuple] = []
    row = SimpleNamespace(id=FID, status="SPLIT", project_key="demo")

    monkeypatch.setattr("acp.run._prepare_intake", lambda session, now: calls.append(("prepare",)))
    monkeypatch.setattr("acp.run._request_row", lambda session, fid: row)
    monkeypatch.setattr(
        "acp.run._advance", lambda fid, **kwargs: calls.append(("advance", str(fid), kwargs["splitter"]))
    )
    monkeypatch.setattr(
        "acp.run._intake_report",
        lambda session, current: SimpleNamespace(feature_request_id=current.id, ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT"),
    )
    monkeypatch.setattr("acp.run.resolve_transport_flag", lambda transport: calls.append(("transport", transport)))

    from acp import run

    report = run.run_intake_existing(
        FID,
        transport="stub",
        bus_root=None,
        session=SimpleNamespace(flush=lambda: None),
        now=NOW,
    )
    assert report.feature_request_id == FID
    assert [item[0] for item in calls] == ["transport", "prepare", "advance"], calls
    assert calls[2][1] == str(FID)


# ---------------------------------------------------------------- 六、会议可见性


def test_the_meeting_lines_are_committed_before_the_split_starts(monkeypatch):
    """台词落进 `summary_digest` 之后必须**当场提交**，不能等整条受理事务。

    2026-10-28 实测：会议 16:21:59 结束（`meeting_ready` 已写留痕），整条受理事务 16:25:25
    才提交 —— 那 3 分半里 `GET /requests/{id}` 读回 **0 条**台词（未提交的写对别的连接不可见），
    屏上就是"会议进行中"一动不动，而模型其实早说完了。

    判据不量"提交了几次"，那会把留痕会话也算进去；它量**顺序**：
    拆单开始那一刻，写过 digest 的那个会话已经提交过。
    """
    timeline: list[str] = []
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession(
            request=SimpleNamespace(project_key="demo", raw_input="做一个咖啡站", summary_digest=None)
        )
        index = len(sessions)
        inner = session.commit

        def commit() -> None:
            inner()
            timeline.append(f"commit:{index}")

        session.commit = commit  # type: ignore[method-assign]
        sessions.append(session)
        return session

    def advance(feature_request_id, **kwargs):
        timeline.append("advance")
        return SimpleNamespace(ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT")

    monkeypatch.setattr(si.realrun, "ensure_archive_root", lambda *a, **k: None)
    monkeypatch.setattr(si.run, "run_intake_existing", advance)

    runner = si.WebIntakeRunner(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lambda line: None,
        meeting=lambda raw: [SimpleNamespace(role_id="orchestrator", text="客户要做咖啡站")],
    )
    runner(feature_request_id=FID)

    work = next(i for i, s in enumerate(sessions) if getattr(s.request, "summary_digest", None))
    at = timeline.index("advance")
    assert f"commit:{work}" in timeline[:at], (
        "会议台词写进了 summary_digest，但拆单开始前主会话没提交 —— "
        "`GET /requests/{id}` 读不到台词，屏上就是「会议进行中」一动不动"
    )


def test_each_turn_reaches_the_digest_before_the_next_role_speaks(monkeypatch):
    """四个岗位**逐轮可见**，不是四轮跑完才一起出现。

    为什么这条必须单独钉：`test_the_meeting_lines_are_committed_before_the_split_starts`
    只要求"拆单前提交过一次"，一次性四段也满足它 —— 而网页会议桌要的是
    "总管说完就看得见总管、产品正在说"这件事，那要求的是**四轮之间各提交一次**。
    判据量的是每个岗位开说那一刻、库里已经能读到几条台词。
    """
    sessions: list[FakeSession] = []
    observed: list[tuple[str, int]] = []
    # 留痕（`_trace`）会自己开一批会话，它们 `get()` 回来的也是这个对象 ——
    # 所以整条链路共用**一个** request 替身，读到的才是同一份 digest（生产里也是同一行）。
    request = SimpleNamespace(project_key="demo", raw_input="做一个咖啡站", summary_digest=None)

    def factory() -> FakeSession:
        session = FakeSession(request=request)
        sessions.append(session)
        return session

    roles = ("orchestrator", "pm", "cto", "designer")

    def streaming_meeting(raw, on_turn=None):
        said = []
        for role in roles:
            # 开说之前先看一眼库里读得到几条 —— 这就是网页此刻能画出来的台词数。
            lines = list(((request.summary_digest or {}).get("会议") or ()))
            observed.append((role, len(lines)))
            turn = SimpleNamespace(role_id=role, text=f"{role} 的意见")
            said.append(turn)
            if on_turn is not None:
                on_turn(turn, list(said))
        return said

    def advance(feature_request_id, **kwargs):
        return SimpleNamespace(ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT")

    monkeypatch.setattr(si.realrun, "ensure_archive_root", lambda *a, **k: None)
    monkeypatch.setattr(si.run, "run_intake_existing", advance)

    runner = si.WebIntakeRunner(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lambda line: None,
        meeting=streaming_meeting,
    )
    runner(feature_request_id=FID)

    assert observed == [("orchestrator", 0), ("pm", 1), ("cto", 2), ("designer", 3)], (
        "每个岗位开说时，库里应该已经有前面几轮的台词；"
        f"实际读数 {observed} —— 全是 0 就是四轮跑完才一起落库（老行为），屏上四段会同时出现"
    )
    assert len(request.summary_digest["会议"]) == 4


def test_a_runner_that_ignores_on_turn_still_lands_the_lines(monkeypatch):
    """只收一个参数的老替身不许把台词丢掉 —— 那是最难查的一类失败（签名不合 ⇒ 静默零台词）。"""
    sessions: list[FakeSession] = []
    request = SimpleNamespace(project_key="demo", raw_input="做一个咖啡站", summary_digest=None)

    def factory() -> FakeSession:
        sessions.append(FakeSession(request=request))
        return sessions[-1]

    monkeypatch.setattr(si.realrun, "ensure_archive_root", lambda *a, **k: None)
    monkeypatch.setattr(
        si.run,
        "run_intake_existing",
        lambda feature_request_id, **kwargs: SimpleNamespace(
            ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT"
        ),
    )

    runner = si.WebIntakeRunner(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lambda line: None,
        meeting=lambda raw: [SimpleNamespace(role_id="orchestrator", text="客户要做咖啡站")],
    )
    runner(feature_request_id=FID)

    assert request.summary_digest["会议"] == ["orchestrator｜客户要做咖啡站"]


# ---------------------------------------------------------------- 七、文档节点

class FakeDocs:
    """三份文档的替身。`digest()` 是事实，`files()` 是投影 —— 与生产实现同一分工。"""

    prd = {
        "title": "手冲咖啡官网",
        "pages": [{"label": "首页", "file": "index.html", "duty": "品牌与主推品"}],
        "acceptance": ["首屏能看到主推品", "375px 下导航不换行"],
        "out_of_scope": ["在线支付"],
    }

    def digest(self) -> dict[str, list[str]]:
        return {"PRD": ['{"title":"手冲咖啡官网"}'], "架构": ['{"stack":"原生"}'], "令牌": ['{"accent":"#6B4226"}']}

    def files(self) -> dict[str, str]:
        return {"prd.md": "# 手冲咖啡官网 · PRD\n", "architecture.md": "# 架构\n", "tokens.json": "{}\n"}


def test_the_docs_are_committed_into_the_digest_before_the_split_starts(monkeypatch):
    """文档与会议同一条规矩：**写得进去也要读得到**。

    这一条量顺序：拆单开始那一刻，写过「PRD」段的会话已经提交过。
    只说"提交过一次"不够 —— 留痕会话也提交，那会把没提交的主会话盖住。
    """
    timeline: list[str] = []
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession(
            request=SimpleNamespace(project_key="demo", raw_input="做一个咖啡站", summary_digest=None)
        )
        index = len(sessions)
        inner = session.commit

        def commit() -> None:
            inner()
            timeline.append(f"commit:{index}")

        session.commit = commit  # type: ignore[method-assign]
        sessions.append(session)
        return session

    def advance(feature_request_id, **kwargs):
        timeline.append("advance")
        return SimpleNamespace(ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT")

    monkeypatch.setattr(si.realrun, "ensure_archive_root", lambda *a, **k: None)
    monkeypatch.setattr(si.run, "run_intake_existing", advance)

    runner = si.WebIntakeRunner(
        session_factory=factory,
        bus_root=Path("C:/tmp/archives"),
        clock=lambda: NOW,
        emit_to=lambda line: None,
        documents=lambda raw, said: FakeDocs(),
    )
    runner(feature_request_id=FID)

    work = next(i for i, s in enumerate(sessions) if getattr(s.request, "summary_digest", None))
    at = timeline.index("advance")
    assert f"commit:{work}" in timeline[:at], (
        "文档写进了 summary_digest，但拆单开始前主会话没提交 —— "
        "`GET /requests/{id}` 读不到三段，屏上那三格一直是空的"
    )
    assert set(sessions[work].request.summary_digest) >= {"PRD", "架构", "令牌"}


def test_a_failed_doc_node_only_leaves_a_trace(wired, monkeypatch):
    """文档失败**不挡拆单** —— 把"文档节点挂了"表现成"需求提交失败"是另一种假话。"""
    from acp.docs import DocumentUnavailable

    runner, calls, sessions, lines, script = wired

    def boom(raw: str, said: list[str]):
        raise DocumentUnavailable("docs_bad_json", "JSON 解析失败")

    runner.documents = boom
    runner(feature_request_id=FID)

    assert [row for row in rows_of(sessions) if getattr(row, "payload", {}).get("action") == "docs_failed"], (
        "文档失败没有留痕 ⇒ 屏上只会看到三格空着，没人知道是模型跑偏还是没接"
    )
    assert any(call[0] == "advance" for call in calls), "文档失败把拆单带走了"


def test_the_doc_files_land_under_results_docs(tmp_path, monkeypatch):
    """文件是投影：落在 `<项目目录>/results/docs/` 下，不进交付包（那要改 file-bus 契约，另定）。"""
    monkeypatch.setattr(si.realrun, "ensure_archive_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(
        si.run,
        "run_intake_existing",
        lambda fid, **kw: SimpleNamespace(ticket_id=uuid.uuid4(), card_ids=("T01",), request_status="SPLIT"),
    )
    runner = si.WebIntakeRunner(
        session_factory=lambda: FakeSession(
            request=SimpleNamespace(project_key="demo", raw_input="做一个咖啡站", summary_digest=None)
        ),
        bus_root=tmp_path,
        clock=lambda: NOW,
        emit_to=lambda line: None,
        documents=lambda raw, said: FakeDocs(),
    )
    runner(feature_request_id=FID)

    folder = tmp_path / "results" / "docs"
    assert sorted(p.name for p in folder.iterdir()) == ["architecture.md", "prd.md", "tokens.json"]
    assert "手冲咖啡官网" in (folder / "prd.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 失败要看得见（U6）

def test_a_failed_intake_marks_the_request_failed(wired):
    """受理失败要把需求单改成 `FAILED`，并把原因留在留痕里（U6，2026-10-28 真跑）。

    原来失败只写一行 `split_failed`，需求单永远停在 `INTAKE` —— 而 `INTAKE` 在界面上读作
    "正在拆解"。于是一次失败看起来像"卡住了"：会议桌说"会议已锁定方案"，文档格一直画骨架屏，
    而真正发生的是这件事已经结束了。

    `FAILED` 是 `feature_request_status` 枚举里本来就有的取值（EN-01），不需要迁移；
    前端 `stageOf()` 见到"状态不是 INTAKE 却没有文档"会判 `stalled` 并收手。
    """
    runner, _calls, sessions, _lines, script = wired

    def boom(feature_request_id, **kwargs):
        raise RuntimeError("图停了")

    script["advance"] = boom
    runner(feature_request_id=FID)

    marked = [row for row in rows_of(sessions) if getattr(row, "payload", {}).get("action") == "intake_failed"]
    assert marked, "失败没有留下可读的原因 ⇒ 页面上只剩'停住了'，而用户能做的动作和'卡住了'一模一样"
    assert "图停了" in str(marked[0].payload.get("error"))
    assert marked[0].payload.get("code") == "RuntimeError"
    assert any(
        str(getattr(session.request, "status", "")) == "FAILED" for session in sessions
    ), "需求单状态没改 ⇒ 页面上永远是'正在拆解'"


def test_a_failure_after_the_request_left_intake_does_not_roll_it_back(wired):
    """状态已经不是 `INTAKE` 就不硬写 `FAILED`：那会抹掉一个已经成立的事实。"""
    runner, _calls, sessions, lines, script = wired

    def boom(feature_request_id, **kwargs):
        raise RuntimeError("图停了")

    script["advance"] = boom
    # 主工作单元拿到的那一行已经是 SPLIT（模拟与上一跑交叠）。
    original_factory = runner.session_factory

    def factory():
        session = original_factory()
        session.request = SimpleNamespace(project_key="demo", status="SPLIT")
        return session

    runner.session_factory = factory
    runner(feature_request_id=FID)

    assert not any(
        str(getattr(session.request, "status", "")) == "FAILED" for session in sessions
    ), "把 SPLIT 退回 FAILED 了"
    assert any("fail-not-marked" in line for line in lines), lines
