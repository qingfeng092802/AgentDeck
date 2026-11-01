"""M2-001 / M2-002 的看板只读面判据（`api/web.py` + `app.create_app` 的静态面 + `serve.py` 的绑址）。

**这一文件为什么不连库**：它要证的三类事都在形状层 —— 帧怎么写、游标怎么推进、会话开几个关几个、
静态面挂在哪、绑址拒不拒。全部用注入的假会话与 monkeypatch 的读函数就量得出来，
而"从真 EN-14 里读得出一行"那一条归 `tests/integration/test_board_activity.py`（真连 PG）。

六节分工：

- 节 一 = 帧的形状与游标：一帧三行（`id:` / `event:` / `data:`），`id:` 就是行主键。
- 节 二 = 推流循环：游标只前进、每轮会话开几个就关几个、`limit` 到了就结束、空轮发 keepalive。
- 节 三 = 重连游标来自 `Last-Event-ID`（前端不必自己再存一份）。
- 节 四 = 分页读的两处拒：`entity` 不在主体清单、`limit < 1`，都在碰库之前。
- 节 五 = 静态面：不给就不挂、给了必须是个目录、挂上之后 API 路径仍然优先。
- 节 六 = 启动入口：非环回绑址在 `uvicorn.run` **之前**拒，且拒的时候一次都没跑。

③ 反向对照：把 `api/web.py` 与 `serve.py` 的**注释措辞**改掉不许红（下面没有任何一条断言读正文措辞，
读的都是调用结果与 AST 计数）；变异账写在本笔的台账格里。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from acp import app as app_module
from acp import serve
from acp.api import web
from acp.config import runtime
from acp.persistence import events

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "specs" / "001-multi-agent-collab-platform" / "spec.md"
SRC = REPO / "src" / "acp"


def activity(row_id: int, *, kind: str = "TASK", actor: str = "a1") -> events.Activity:
    """造一行读侧形状。`kind` 用域里真实存在的值（替身档不练假靶子），
    `payload` 里放中文，顺带证 `ensure_ascii=False` 那条口径（节 一 第二条）。
    """
    return events.Activity(
        id=row_id,
        entity="task_card",
        entity_id="T01",
        kind=kind,
        actor_agent_id=actor,
        payload={"note": "拆单第 2 轮"},
        created_at=dt.datetime(2026, 10, 19, 12, 0, row_id % 60, tzinfo=dt.timezone.utc),
    )


class FakeSession:
    """只回答两件事的会话替身：`close()` 被叫过没有、`scalars()` 有没有被用上。

    它**不**执行 select —— 真执行归集成那一档。这里要量的恰好是"循环自己借还会话"，
    所以计数就是判据本体。
    """

    opened = 0
    closed_sessions: list[FakeSession] = []

    def __init__(self) -> None:
        FakeSession.opened += 1
        self.closed = False

    def close(self) -> None:
        self.closed = True
        FakeSession.closed_sessions.append(self)


@pytest.fixture(autouse=True)
def _reset_fake_sessions():
    FakeSession.opened = 0
    FakeSession.closed_sessions = []
    yield


# ---------------------------------------------------------------- 一、帧的形状

# 下面这些判据把"帧"这件事钉成一个可复算的串，而不是"看起来像 SSE"。


def test_a_frame_is_three_lines_terminated_by_a_blank_one():
    frame = web._frame(activity(42))
    lines = frame.split("\n")
    assert lines[0] == "id: 42", frame
    assert lines[1] == f"event: {web.STREAM_EVENT}", frame
    assert lines[2].startswith("data: "), frame
    assert frame.endswith("\n\n"), "SSE 的帧分隔符是空行，少一个换行浏览器就拼不出事件"
    assert lines[3] == ""


def test_the_data_line_is_the_whole_row_with_the_payload_verbatim():
    frame = web._frame(activity(7))
    # 先看**线上那几个字节**，再解码比对象：`json.loads` 会把 `拆单` 解回同一个 Python 串，
    # 所以只比解码值的话，把 `ensure_ascii=False` 改成 True 也不会红（变异 M-F6 第一次就是这么绿的）。
    assert "拆单第 2 轮" in frame, f"data: 行里必须是原字面：{frame!r}"
    body = json.loads(frame.split("data: ", 1)[1].strip())
    assert body["id"] == 7
    assert body["entity"] == "task_card" and body["entity_id"] == "T01"
    assert body["kind"] == "TASK" and body["actor_agent_id"] == "a1"
    assert body["payload"] == {"note": "拆单第 2 轮"}, "载荷映射必须原样一层，读侧不替它摊平"
    assert body["created_at"] == "2026-10-19T12:00:07+00:00"


# ---------------------------------------------------------------- 二、推流循环


def _script(monkeypatch, pages: list[list[events.Activity]]):
    """把 `web` 里那个读函数换成按脚本回页的替身，并记下每轮拿到的游标。"""
    calls: list[int] = []
    index = {"i": 0}

    def fake_read(session, *, after_id=0, limit=50, entity=None, entity_id=None):
        calls.append(after_id)
        page = pages[min(index["i"], len(pages) - 1)]
        index["i"] += 1
        return tuple(page)

    monkeypatch.setattr(web.events, "read_since", fake_read)
    return calls


def test_the_cursor_advances_and_the_loop_terminates_at_the_limit(monkeypatch):
    pages = [[activity(10), activity(11)], [activity(12)]]
    calls = _script(monkeypatch, pages)
    frames = list(web._tail(FakeSession, start=0, stop_after=3, interval=0))
    assert [int(f.split("\n")[0].split(": ")[1]) for f in frames] == [10, 11, 12], frames
    assert calls == [0, 11], f"第二轮的游标必须是已发的最后一行 id：{calls}"
    assert len(frames) == 3


def test_each_poll_borrows_and_returns_a_session(monkeypatch):
    """长驻的流每轮自己开会话自己关。借了不还就是拿一条连接池占死一台库。

    **必须跑够两轮**，这是本发第一版的一个真缺口：那时 `stop_after=1` 且脚本第一轮就有数据，
    整条流只发生一轮 ⇒ "整条流复用同一个会话"那种改法（变异 M-F2）在只有一轮时
    `opened == closed == 1` 照样成立，于是它绿着过了。现在脚本第一轮故意回空页，
    于是"一轮"与"每轮"这两个说法第一次有了不同的读数：开 2 次、关 2 次、而且是 2 个不同对象。
    """
    _script(monkeypatch, [[], [activity(1)]])
    frames = list(web._tail(FakeSession, start=0, stop_after=1, interval=0))
    assert len(frames) == 2, f"第一轮空页要发 keepalive：{frames}"
    assert FakeSession.opened == len(FakeSession.closed_sessions) == 2, (
        f"两轮应当各借各还：opened={FakeSession.opened} closed={len(FakeSession.closed_sessions)}"
    )
    assert len({id(session) for session in FakeSession.closed_sessions}) == 2, "两轮拿到的其实是同一个会话"
    assert all(session.closed for session in FakeSession.closed_sessions)


def test_an_idle_round_still_emits_a_keepalive(monkeypatch):
    """空轮发一条注释帧：它既是"连接还活着"的可见证据，也让判据能断"没数据时不等于卡死"。"""
    _script(monkeypatch, [[], [activity(5)]])
    frames = list(web._tail(FakeSession, start=0, stop_after=1, interval=0))
    assert frames[0] == ": keepalive\n\n", frames
    assert frames[1].startswith("id: 5"), frames


def test_the_sc_013_bound_is_measured_from_the_spec_not_from_a_copy():
    """合并等待（= 本实现的延迟上界）MUST 不大于 spec 里那个数，而且这个数**现读加载器**。

    两头都是现读：spec 里那个秒数从 `spec.md` 取，实现里的毫秒数从 `config/runtime.yaml` 的加载器取。
    代码里再抄一份 3000、或再留一个 500 的默认常量，都会造出第二个载体 —— 那样"面板改了而流没改"
    这件事就没有任何判据能看见（`test_the_endpoint_has_no_interval_of_its_own` 钉的就是这一头）。
    """
    text = SPEC.read_text(encoding="utf-8")
    # 定锚在**定义行**（`- **SC-013（…）**：…`）而不是"正文里第一个出现 SC-013 的地方"：
    # spec 里那条编号还在覆盖矩阵里被引用过一次，抓第一条就会抓到没有秒数的那一行，于是这条判据恒红。
    line = next((entry for entry in text.splitlines() if entry.startswith("- **SC-013")), "")
    match = re.search(r"≤\s*(\d+(?:\.\d+)?)\s*秒", line)
    assert match, f"spec.md 的 SC-013 定义行取不到秒数：{line[:80]!r}"
    bound_ms = float(match.group(1)) * 1000
    interval = runtime.load_runtime_params().fragment_merge_ms
    assert 0 < interval <= bound_ms, f"合并等待 {interval} ms 超出 SC-013 的 {bound_ms} ms ⇒ 不刷新也能变这条会失守"


def test_the_endpoint_has_no_interval_of_its_own():
    """`/events/stream` 的 `poll_ms` 默认必须是 `None`（= 交给加载器），不许是任何一个数。

    留一个数字默认值，"面板改了而流还是老间隔"就永远不会被看见 —— 而这条规则用行为量不出来时
    （计时判据在 CI 上不可靠），签名这一层就是唯一还能钉住的地方。
    """
    default = inspect.signature(web.stream_events).parameters["poll_ms"].default
    assert default is None, f"端点自己带了默认间隔 {default!r} ⇒ 面板与流成了两个口径"
    assert not hasattr(web, "STREAM_POLL_MS_DEFAULT"), "模块里不该再有那份间隔的副本"


def test_the_stream_takes_its_interval_from_the_loader(monkeypatch):
    """签名默认是 `None` 只证了一半，这一发证另一半：**没给参数时那个数确实来自 `runtime.yaml`**。

    两条半边各挡一种假动作：只有上一条时，"默认 None 但函数体里写死 500"仍然合法；
    只有这一条时，模块里再留一份常量副本看不见。反向半边（读不出就 503、不许当默认值继续）
    也在这里，因为它与正向那半共用同一个注入点，拆开发量就看不出是同一件事的两面。
    """
    seen: dict[str, float] = {}
    _script(monkeypatch, [[activity(3)]])

    def recording_tail(factory, *, start, stop_after, interval):
        seen["interval"] = interval
        return iter(())

    monkeypatch.setattr(web, "_tail", recording_tail)
    monkeypatch.setattr(
        web.runtime,
        "load_runtime_params",
        lambda *a, **k: runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=1234, auto_accept_low_risk=None, source_sha256="x"),
    )
    client = TestClient(app_module.create_app(session_factory=FakeSession))
    assert client.get("/events/stream", params={"limit": 1}).status_code == 200
    assert seen == {"interval": 1.234}, f"流用的不是面板那个数：{seen}"

    def boom(*a, **k):
        raise runtime.RuntimeParamsError("这一发故意让参数读不出来")

    monkeypatch.setattr(web.runtime, "load_runtime_params", boom)
    refused = client.get("/events/stream", params={"limit": 1})
    assert refused.status_code == 503, refused.text[:160]
    assert "runtime_params_unavailable" in refused.text, "读不出参数要明说，不许'当成默认值继续'"


# ---------------------------------------------------------------- 三、重连游标


def test_the_last_event_id_header_becomes_the_cursor(tmp_path, monkeypatch):
    """浏览器重连时自动带 `Last-Event-ID`，服务端接住它 ⇒ 前端不需要再存一份游标。"""
    calls = _script(monkeypatch, [[activity(88)]])
    app = app_module.create_app(session_factory=FakeSession)
    client = TestClient(app)
    with client.stream(
        "GET", "/events/stream", params={"limit": 1, "poll_ms": 1}, headers={"Last-Event-ID": "87"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        first = next(line for line in response.iter_lines() if line.startswith("data: "))
    assert calls[0] == 87, f"没接住重连游标：{calls}"
    assert json.loads(first[len("data: ") :])["id"] == 88


def test_an_explicit_after_wins_over_the_header(tmp_path, monkeypatch):
    """`?after=` 是调用方显式给的，`Last-Event-ID` 是浏览器猜的 ⇒ 显式优先。"""
    calls = _script(monkeypatch, [[activity(90)]])
    app = app_module.create_app(session_factory=FakeSession)
    with TestClient(app).stream(
        "GET", "/events/stream", params={"after": 5, "limit": 1, "poll_ms": 1}, headers={"Last-Event-ID": "87"}
    ) as response:
        assert response.status_code == 200
        next(response.iter_lines())
    assert calls[0] == 5


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 0},
        {"limit": web.STREAM_FRAMES_MAX + 1},
        {"poll_ms": 0, "limit": 1},
        {"after": -1, "limit": 1},
    ],
)
def test_the_stream_refuses_out_of_range_arguments_before_opening(args, monkeypatch):
    """四格都断 422，但每一格带的东西不一样：两格只带 `limit`（那条界限先拒），
    另两格把 `limit: 1` 也带上 —— 这一条是量出来的，不是顺手写的。

    量法：把 `elif poll_ms < 1` 那道界限单独拆掉（变异 M-H10b）跑这一发，前几版都在这里挂住而不是红：
    `TestClient.stream()` 退出上下文时也要等 ASGI 那侧的任务结束，而 `interval=0` + 不给 limit 的循环
    永远不会结束（`_script` 的替身会一直回同一页，但 `stop_after=None` 时它不数帧）。
    **判据失守的样子不该是"整趟测试挂住"** —— 那比假绿更难查。带上 `limit: 1` 之后：
    界限在 ⇒ 422；界限被拆 ⇒ 一条一帧就结束的 200 ⇒ 这一发红。
    """
    _script(monkeypatch, [[activity(1)]])
    client = TestClient(app_module.create_app(session_factory=FakeSession))
    with client.stream("GET", "/events/stream", params=args) as response:
        # 失败信息也不抽干正文：一旦"没拒"，`response.text` 会去读那条流，这一发就从"红"变回"挂"。
        assert response.status_code == 422, f"content-type={response.headers.get('content-type')!r}"


# ---------------------------------------------------------------- 四、分页读的两处拒


def test_read_since_refuses_a_bad_entity_and_a_zero_limit_before_touching_the_store():
    """两处在**建 stmt 之前**拒：`session=None` 也能跑，就证明它确实没碰库。

    `limit=0` 单独钉是因为本表除 PK 外没有二级索引（`models_registry.EventLog` 的 docstring），
    把 0 读成"不限"就是一次全表扫 —— 而调用方写的明明是"我要 0 行"。
    """
    with pytest.raises(events.EventMisuse, match="entity"):
        events.read_since(None, entity="not_a_domain")  # type: ignore[arg-type]
    with pytest.raises(events.EventMisuse, match="limit"):
        events.read_since(None, limit=0)  # type: ignore[arg-type]


def test_an_unknown_entity_on_the_endpoint_is_422_not_500():
    """这一发**不换**读函数：要走真 `read_since()` 的前置校验，才能证 422 是"校验拒的"而不是"替身没数据"。

    `session_factory=FakeSession` 在这里只是让端点能过注入那一格 —— 校验在建 stmt 之前就抛，
    假会话一次也不会被用到（正因如此它不需要会执行 select）。
    """
    client = TestClient(app_module.create_app(session_factory=FakeSession))
    response = client.get("/events", params={"entity": "made_up"})
    assert response.status_code == 422, response.text[:160]
    assert "不在留痕主体清单里" in response.text


# ---------------------------------------------------------------- 五、静态面


def test_no_static_face_is_mounted_when_none_is_given():
    app = app_module.create_app()
    assert [r.name for r in app.routes if getattr(r, "name", None) == "board"] == []


def test_a_given_static_dir_serves_the_board_and_stays_behind_the_api(tmp_path):
    """挂上之后两件事必须同时成立：`/` 给的是看板，`/approvals` 给的仍是 API 的 503。

    后半句是这条判据的一半分量：`app.mount("/", …)` 排在路由之后才可能成立，
    谁把它挪到 `include_router` 之前，公开端点就全被静态兜底吃成 index.html —— 那时"看板能打开"
    而"接口悄悄没了"，是最难看出来的一种破坏。
    """
    (tmp_path / "index.html").write_text("<html><body>ACP-BOARD-MARKER</body></html>", encoding="utf-8")
    client = TestClient(app_module.create_app(static_dir=tmp_path))
    assert "ACP-BOARD-MARKER" in client.get("/").text
    assert client.get("/approvals").status_code == 503


def test_a_static_dir_that_is_not_a_directory_refuses_at_assembly(tmp_path):
    with pytest.raises(app_module.BoardAssetsMissing, match="npm"):
        app_module.create_app(static_dir=tmp_path / "nope")


def test_the_mount_still_happens_in_exactly_one_place_in_the_production_tree():
    """`create_app()` 里那个 `app.mount` 是**唯一**一处（挂载点多一处 = 对外面有两个答案）。

    判据走 AST 而不是 grep 正文：注释里提一句"挂载"不该红（反向对照），而换行写法也不会漏。
    """
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mount"
            ):
                hits.append(path.relative_to(SRC).as_posix())
    assert hits == ["app.py"], f"静态挂载点不止一处（或装配点不再挂载）：{hits}"


# ---------------------------------------------------------------- 六、启动入口


@pytest.mark.parametrize("raw", ["0.0.0.0", "192.168.1.8", "*", "::", "localhost"])
def test_anything_but_loopback_is_refused(raw):
    with pytest.raises(serve.HostRefused):
        serve.resolve_host(raw)


def test_the_loopback_forms_that_are_accepted():
    assert serve.resolve_host("127.0.0.1") == "127.0.0.1"
    assert serve.resolve_host("::1") == "::1"


def test_a_refused_host_never_reaches_the_server(monkeypatch):
    """拒必须发生在 `uvicorn.run` 之前 —— 光断异常类型证不了"没起服务"。"""
    ran: list[str] = []
    monkeypatch.setattr(serve, "make_session_factory", lambda: FakeSession)
    with pytest.raises(serve.HostRefused):
        serve.main(["--host", "0.0.0.0"], runner=lambda *a, **k: ran.append("ran"))
    assert ran == []


def test_the_default_bind_recorded_by_the_runner_is_loopback(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    monkeypatch.setattr(serve, "make_session_factory", lambda: FakeSession)
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    code = serve.main(
        ["--dist", str(tmp_path)], runner=lambda app, host, port, log_level: seen.update(host=host, port=port)
    )
    assert code == 0
    assert seen == {"host": "127.0.0.1", "port": serve.DEFAULT_PORT}


def test_the_approver_list_reaches_the_app_state_and_absent_means_none(monkeypatch, tmp_path):
    """看板要能批单，那把钥匙只能从启动参数来：给两个 ⇒ `app.state.approvers` 就是那两个；一个都不给 ⇒ `None`。

    这一发钉的是**注入通道通不通**，不是"给了就能批"（那是 `api/approvals.py::_approvers` 与仓储层的语义，
    已有档在量）。也不许在这里给个默认名单：把"没配"读成"不限制"正是 `_approvers` 那条方向性理由要防的事，
    所以第二半必须钉住"启动面自己不下发审批者"。
    """
    MISSING = object()
    seen: dict[str, object] = {}
    monkeypatch.setattr(serve, "make_session_factory", lambda: FakeSession)
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    def runner(app, **kwargs):
        seen["with"] = sorted(app.state.approvers)
        seen.update(kwargs)

    serve.main(
        ["--dist", str(tmp_path), "--approver", "owner-1", "--approver", "owner-2"], runner=runner
    )
    assert seen["with"] == ["owner-1", "owner-2"], "启动参数没走到装配点 ⇒ 看板上的提交会一直 503"

    bare: dict[str, object] = {}
    serve.main(
        ["--dist", str(tmp_path)],
        runner=lambda app, **k: bare.update(value=getattr(app.state, "approvers", MISSING)),
    )
    # `create_app()` 只把**非 None** 的注入项写进 `app.state`（`app.py` 的那个 `if supplied[key] is not None`），
    # 而 Starlette 的 `State` 是**字典背书**的：`dir(state)` 与 `vars(state)` 两个都看不见已注入的键
    # （实测：注入 `frozenset({'x'})` 后两者仍是 False），所以"有没有"只能用 `getattr` 配哨兵问。
    # 直接写 `app.state.approvers is None` 会先抛 AttributeError（第一跑就是这么炸的），
    # 而写 `'approvers' in dir(...)` 会变成一条恒真的判据 —— 两侧都得按 `State` 的真实形状来。
    assert bare["value"] is MISSING, "启动面不许给默认审批者"


def test_api_only_leaves_the_board_unmounted(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    def runner(app, **kwargs):
        seen["mounts"] = [r.name for r in app.routes if getattr(r, "name", None) == "board"]
        seen.update(kwargs)

    monkeypatch.setattr(serve, "make_session_factory", lambda: FakeSession)
    serve.main(["--api-only"], runner=runner)
    assert seen["mounts"] == []
    assert seen["host"] == "127.0.0.1"
