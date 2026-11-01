"""T111（FR-064, FR-065｜R-14）：两个场景注入入口的判据（`python -m acp.chaos`）。

档位：`tests/guards/`，混合文件。第一节的 ①②（射程与顺序）要真 PG + 真 checkpointer；
其余都是桩 / 源码面，不连库。

**为什么这一批用例不真停库、也不真删业务表**：
`--stop-postgres` 的影响面是这台机上连 `acp-db` 的**所有**进程，包括 owner 正在跑的看板与后端
⇒ 真执行那一发在本轮**没有被任何用例跑过**（点名登记，不装作已验）。替代它的是三件：
`--yes` 这道确认门、argv 的形状断言、非零退出码的归类。这三件都能证"拼出来的命令对不对、
没确认会不会动手"，证不到"docker 真把容器停了" —— 差额在 `evidence/phase-09.md §四` 那条未闭清单里。

| 原文 / 义务 | 节 |
|---|---|
| "两个演练入口（场景注入用，不得只在文档里存在）" | 一、二、三 |
| 注入不许扩大成长链路（只删一个 thread 的三张表） | 一 |
| 破坏性动作要有确认门 | 二、三 |
| 凭据面：argv 与消息里不许带出口令 / DSN | 二、三 |
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import acp
from acp import chaos, recover
from acp.bus import layout
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import checkpointer as cp
from acp.persistence.models_core import FeatureRequest

SRC_ROOT = Path(acp.__file__).resolve().parent
#: `src/acp` 往上**两级**才是仓库根（写成 `.parent` 会得到 `src/`，于是 compose 那条判据
#: 红在"文件不存在"上，看起来像 compose 没建过 —— 它建过，是判据自己拼错了路）。
REPO_ROOT = SRC_ROOT.parents[1]
PROJECT = "acp-chaos"
NOW = dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


@pytest.fixture
def checkpointer_conn():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 checkpointer，整条 skip（不是绿）")
    connection = psycopg.connect(cp.conninfo(), autocommit=True)
    cp.ensure_checkpoint_schema(connection)
    connection.threads_used = []
    try:
        yield connection
    finally:
        for thread in connection.threads_used:
            with connection.cursor() as cursor:
                for table in chaos.THREAD_TABLES:
                    cursor.execute(f'DELETE FROM "{table}" WHERE thread_id = %s', (thread,))
        connection.close()


def _intake_state():
    from acp.orchestrate.state import IntakeState

    return IntakeState


def _seed_thread(session: Session, connection, project_dir: Path, request_id: uuid.UUID) -> str:
    """给某个需求单造一个**有现场**的 thread（走恢复那条写回路径，1 个 checkpoint + 若干 blob）。"""
    from acp.orchestrate import graph

    saver = cp.build_checkpointer(connection, state_schema=_intake_state())
    target = recover._build_recovery_graph(session, bus_root=project_dir.parent, saver=saver)
    config = cp.thread_config(kind=graph.GATE_KIND, feature_request_id=request_id)
    target.update_state(config, {"feature_request_id": request_id}, as_node=graph.NORMALIZE)
    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    connection.threads_used.append(thread)
    return thread


def _request(session: Session, request_id: uuid.UUID) -> None:
    session.add(
        FeatureRequest(
            id=request_id,
            project_key=PROJECT,
            raw_input="演练入口的用例底座",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    session.flush()


class StubRunner:
    """compose argv 的记账桩：只记录、只回一个预设退出码。"""

    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.code = code

    def __call__(self, argv) -> int:
        self.calls.append(tuple(str(item) for item in argv))
        return self.code


# ---------------------------------------------------------------- 一、场景 1 的射程与顺序

def test_the_thread_table_list_equals_the_library_declared_one() -> None:
    """`THREAD_TABLES` MUST 等于"库声明的、含 `thread_id` 列的那几张表"—— 名单不靠人记。

    这条是注入面唯一的"少删一张"防线：升 `langgraph-checkpoint-postgres` 时如果多出一张带
    `thread_id` 的表，本模块的删法就只清掉一部分，而"state 损坏"这个场景从此变成"半损坏"——
    那比全损坏更难诊断（恢复会读到一份混合现场）。
    """
    tables, _ = cp.library_declared_schema()
    with_thread = {name for name, columns in tables.items() if "thread_id" in columns}

    assert set(chaos.THREAD_TABLES) == with_thread, f"差集：{with_thread ^ set(chaos.THREAD_TABLES)}"
    assert chaos.THREAD_TABLES[-1] == "checkpoints", (
        "主表必须最后删：另两张按 `(thread_id, checkpoint_id)` 挂在它上面，库上没有真外键，"
        "先删主表就把它们变成孤儿行"
    )


def test_the_kill_removes_only_the_named_thread(session: Session, checkpointer_conn, bus) -> None:
    """删一个 thread 不许碰到另一个 thread —— **这一条是"演练不会升级成事故"的那道界**。

    两个 thread 都用真 checkpointer 写出来（一个假设为空就直接红在 `count` 上，不算通过）。
    """
    project_dir = bus.with_project(PROJECT)
    first, second = uuid.uuid4(), uuid.uuid4()
    _request(session, first)
    _request(session, second)
    thread_a = _seed_thread(session, checkpointer_conn, project_dir, first)
    thread_b = _seed_thread(session, checkpointer_conn, project_dir, second)
    assert _rows(checkpointer_conn, thread_a) > 0, "前置：现场真要有行"
    before_b = _rows(checkpointer_conn, thread_b)

    killed = chaos.kill_graph_state(checkpointer_conn, thread_a)

    assert killed.thread_id == thread_a and killed.total > 0
    assert _rows(checkpointer_conn, thread_a) == 0
    assert _rows(checkpointer_conn, thread_b) == before_b, "别人的现场一行不许少"


def _rows(connection, thread: str) -> int:
    """某个 thread 在三张表上的总行数（`chaos.read_thread_rows` 的和，不另开一条 SQL）。"""
    return chaos.read_thread_rows(connection, thread).total


def test_the_injection_leaves_the_migrations_table_alone(
    session: Session, checkpointer_conn, bus
) -> None:
    """`checkpoint_migrations` 没有 `thread_id` ⇒ 它**不在射程内**，注入前后计数必须相等。

    这条看着多余，其实是"删错东西"最便宜的探测：那张表记的是 schema 版本，被删一次就会让
    下一次 `ensure_checkpoint_schema()` 重放建表语句，把一次演练变成 schema 层的事故。
    """
    project_dir = bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    thread = _seed_thread(session, checkpointer_conn, project_dir, request_id)
    before = int(checkpointer_conn.execute("SELECT count(*) FROM checkpoint_migrations").fetchone()[0])

    chaos.kill_graph_state(checkpointer_conn, thread)

    assert int(checkpointer_conn.execute("SELECT count(*) FROM checkpoint_migrations").fetchone()[0]) == before
    assert "checkpoint_migrations" not in chaos.THREAD_TABLES


def test_a_dry_run_reads_rows_and_deletes_nothing(session: Session, checkpointer_conn, bus) -> None:
    """不带 `--yes` 那一发 MUST 只交出读数。这条与下一条是一对：一条证"看了"，一条证"没动"。"""
    project_dir = bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    thread = _seed_thread(session, checkpointer_conn, project_dir, request_id)
    before = _rows(checkpointer_conn, thread)

    rows = chaos.read_thread_rows(checkpointer_conn, thread)

    assert rows.thread_id == thread and rows.total == before and rows.total > 0
    assert [name for name, _ in rows.counts] == list(chaos.THREAD_TABLES)
    assert _rows(checkpointer_conn, thread) == before


def test_an_empty_thread_id_is_refused_before_the_cursor_is_used() -> None:
    """空 thread 号 ⇒ `thread_required`，且**连接根本没被碰**（传进去的是个不能用的对象也不会 TypeError）。

    空串会拼成 `WHERE thread_id = ''`：删不掉任何行，却打出一行 `deleted=0` 看起来像注入成功。
    演练工具自己造假读数，比不注入坏得多。
    """
    with pytest.raises(chaos.ChaosMisuse) as caught:
        chaos.kill_graph_state(object(), "")  # 不是 psycopg 连接：走到第二行就会 AttributeError

    assert caught.value.code == "thread_required"


def test_the_thread_id_comes_from_the_same_derivation_as_recovery() -> None:
    """注入算的 thread 号 MUST 等于恢复侧 `checkpointer.thread_id_for()` 算出来的那个。

    两个口径一旦分叉，就是"删了一个没人会读的名字、留了一个恢复路径正在读的名字"——
    那种现场看起来像"注入成功而恢复无效"，排查会指向错的一侧。
    """
    request_id = uuid.uuid4()
    from acp.orchestrate import graph

    assert chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id) == (
        cp.thread_id_for(kind=graph.GATE_KIND, feature_request_id=request_id)
    )
    assert "acp:" not in (SRC_ROOT / "chaos.py").read_text(encoding="utf-8"), (
        "本模块不许自己拼 thread 号：第二份推导迟早和恢复侧分叉，而分叉的样子是"
        "「注入删了一个没人会读的名字，恢复读的那个还在」"
    )


# ---------------------------------------------------------------- 二、场景 2 的确认门

def test_stop_postgres_refuses_without_confirmation_and_calls_nothing() -> None:
    """没有 `--yes` ⇒ 拒在执行之前，runner 一次都没被调到（`calls == []` 才是这条的用意）。"""
    runner = StubRunner()

    with pytest.raises(chaos.ChaosMisuse) as caught:
        chaos.stop_postgres(confirmed=False, runner=runner)

    assert caught.value.code == "confirmation_required"
    assert runner.calls == []


def test_the_argv_names_no_credential_and_only_the_compose_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argv 是封闭的六个 token，里面没有口令、没有主机、也没有端口。

    口令那条用**注入一个已知值**来断，不读环境里真实的那一个：读法在没配口令的机器上会退化成
    `assert not secret`（恒真），而注入法在任何机器上都是同一条判据（凭据面口径：判据用形态，
    比对用值，两边都不回显）。
    """
    monkeypatch.setenv("ACP_PG_PASSWORD", "s3cr3t-value-for-shape-probe")
    argv = chaos.compose_stop_argv()
    joined = " ".join(argv)

    assert argv == ("docker", "compose", "-f", chaos.COMPOSE_FILE, "stop", chaos.COMPOSE_SERVICE)
    assert "://" not in joined and "--env-file" not in joined
    assert os.environ["ACP_PG_PASSWORD"] not in joined, "argv 里出现了口令 ⇒ 它会进 shell 历史与 ps 输出"
    assert len(argv) == 6


def test_the_referenced_compose_file_really_declares_that_service() -> None:
    """这一条把 `COMPOSE_SERVICE` 与**仓里那份 compose 文件**绑起来（口径同 T107 的环境变量名绑定）。

    只断常量等于常量是死的。compose 改服务名、或有人把 `docker-compose.yml` 换成别的文件名，
    这条会红 —— 那正是"演练命令拼得出来而 docker 说没这个服务"的那次事故形状。
    """
    path = REPO_ROOT / chaos.COMPOSE_FILE
    assert path.is_file(), f"compose 文件不在：{path}"
    text = path.read_text(encoding="utf-8")
    services = set(re.findall(r"^  (\w[\w-]*):\s*$", text, re.M))

    assert chaos.COMPOSE_SERVICE in services, f"compose 的 services 是 {sorted(services)}"
    assert "password" not in chaos.compose_stop_argv(), "argv 不该带任何配置项，更不该带口令"


def test_a_nonzero_compose_exit_becomes_a_failure_not_a_silent_ok() -> None:
    """退出码非零 ⇒ `run_compose` 把码交回上层，由 CLI 归类成 FAIL（125 是本机真实见过的形态）。"""
    runner = StubRunner(code=125)

    command, code = chaos.stop_postgres(confirmed=True, runner=runner)

    assert code == 125 and runner.calls == [command]
    assert chaos.run_compose.__doc__ and "不经 shell" in chaos.run_compose.__doc__, (
        "argv 必须是列表直调：拼成 shell 串会让 compose 文件名里的空格变成分词符"
    )


# ---------------------------------------------------------------- 三、CLI 面

def test_the_two_injections_are_mutually_exclusive_and_one_is_required() -> None:
    for argv in (
        ["chaos", "--kill-graph-state", "--request", str(uuid.uuid4()), "--stop-postgres"],
        ["chaos", "--stop-postgres", "--kill-graph-state"],
        ["chaos"],
    ):
        with pytest.raises(SystemExit) as caught:
            chaos.main(argv)
        assert caught.value.code == 2


def test_kill_requires_an_explicit_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    """不给 `--request` ⇒ 退出码 2 且**一条 SQL 都没发**。

    这一条挡的是"默认删全部 thread"那种看起来更方便的实现：一个演练入口拿到"全删"的默认值，
    就等于平台自带一个不可撤销的批量删除命令，而它连 `--yes` 都不需要。
    """
    assert chaos.main(["chaos", "--kill-graph-state"]) == 2
    out = capsys.readouterr().out
    assert "--request" in out and "CHAOS OK" not in out and "CHAOS DRY" not in out


def test_kill_rejects_a_non_uuid_request_without_connecting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert chaos.main(["chaos", "--kill-graph-state", "--request", "T01"]) == 2
    assert "uuid" in capsys.readouterr().out


def test_dry_run_without_dsn_reports_the_variable_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    code = chaos.main(["chaos", "--kill-graph-state", "--request", str(uuid.uuid4())])
    out = capsys.readouterr().out

    assert code == 3
    assert DSN_ENV in out and "://" not in out and "CHAOS OK" not in out


def test_the_cli_goes_through_the_injected_runner_only_when_confirmed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--yes` 才走 `run_compose`，且走的是**模块属性**（不是被 import 时绑死的本地引用）。

    这条同时是两件：一是"确认门在 CLI 层也有效"，二是"用例能把执行器换掉"。
    换掉它不是图省事 —— 真停库会动到 owner 正在用的那个 PG（模块头那条明写的差额）。
    """
    calls: list[tuple[str, ...]] = []

    def fake_run_compose(argv) -> int:
        calls.append(tuple(argv))
        return 0

    monkeypatch.setattr(chaos, "run_compose", fake_run_compose)

    assert chaos.main(["chaos", "--stop-postgres"]) == 2 and calls == []
    assert "CHAOS DRY" in capsys.readouterr().out
    assert chaos.main(["chaos", "--stop-postgres", "--yes"]) == 0
    assert calls == [chaos.compose_stop_argv()]
    assert "CHAOS OK" in capsys.readouterr().out


def test_there_is_deliberately_no_start_flag() -> None:
    """本模块**不给** `--start-postgres`：少一个入口比多一个假出口好。

    配一个对称旗标会让人以为"chaos 能收尾"，而它只管弄坏；起库那句归操作者
    （`docker compose start db`）。这条钉的是"这个缺失是有决定的"，不是忘了 ——
    下一轮想加的人要先读模块头那一段。
    """
    tree = ast.parse((SRC_ROOT / "chaos.py").read_text(encoding="utf-8"))
    flags = {
        value.value if isinstance(value, ast.Constant) else value
        for node in ast.walk(tree)
        for keyword in getattr(node, "keywords", [])
        for attr, value in ((getattr(keyword, "arg", None), getattr(keyword, "value", None)),)
        if attr == "name" and isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    assert not any("start" in flag for flag in flags), f"出现了启动旗标：{sorted(flags)}"
    assert "docker compose start" in (SRC_ROOT / "chaos.py").read_text(encoding="utf-8"), (
        "不加旗标的那句理由要写在模块里，否则下一轮会被读成漏做"
    )


def test_the_default_kind_is_the_gate_kind_bound_to_the_approval_module() -> None:
    """`--kind` 的默认值 MUST 推出与 `advance_request` 同一个 thread 号，而 `chaos` 不模块级 import 编排层。

    两头都要钉：不 import 的理由写在模块头（演练工具不该因为编排栈装不上就连现场都造不出来）；
    而"等于"这一半必须有 —— **第一版这里写的是 `"A"`，实际域值是 `"A_SPLIT"`**：名字分叉之后
    注入删掉的是 `acp:A:<uuid>`（一个不存在的 thread，`deleted=0` 看起来像成功），
    而恢复路径读的 `acp:A_SPLIT:<uuid>` 那份现场还原封不动躺着。这条就是那次实测的落点。
    """
    from acp.orchestrate import graph
    from acp.orchestrate.nodes import approval_gate

    request_id = uuid.uuid4()
    assert chaos.DEFAULT_KIND == approval_gate.KIND == graph.GATE_KIND
    assert chaos.thread_id(kind=chaos.DEFAULT_KIND, feature_request_id=request_id) == (
        cp.thread_config(kind=graph.GATE_KIND, feature_request_id=request_id)["configurable"]["thread_id"]
    ), "默认 kind 推出来的 thread 必须就是推进那条链用的那一个"
    tree = ast.parse((SRC_ROOT / "chaos.py").read_text(encoding="utf-8"))
    top = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0 and node.module
    }
    assert not any(name.startswith("acp.orchestrate") for name in top)


def test_kill_never_touches_a_business_table() -> None:
    """注入的射程只有 checkpoint 三表：`task_card` / `snapshot` / `event_log` 一次都不出现在源码里。

    场景 1 的定义就是"业务事实态没坏、坏的是编排现场"。把 EN-02 / EN-12 / EN-14 一起删掉，
    场景 1 就变成场景 2，而"三元组能不能从文件总线重建"这句话会失去它的对照物。
    """
    text = (SRC_ROOT / "chaos.py").read_text(encoding="utf-8")
    for table in ("task_card", "snapshot", "event_log", "feature_request", "artifact"):
        assert f'FROM "{table}"' not in text and f'"{table}" WHERE' not in text
        assert f"DELETE FROM {table}" not in text
    assert layout.BUS_ROOT_NAME not in text, (
        "注入不该顺手删总线目录：那是另一件事，而且不可撤销（checkpoint 行能重建，产物文件不能）"
    )
