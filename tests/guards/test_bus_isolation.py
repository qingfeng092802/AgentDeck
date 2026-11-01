"""T021：文件总线的隔离判据（FR-013 / FR-014｜R-24），含宪法 v1.0.3 原则 II 的 frontmatter 四断言。

覆盖 `contracts/file-bus.md` §7 那张表的六行、§3 的分层四条、§2 的三条规则与 §6 的关系式。
全部用 `tmp_path`，**不碰工作树里的 `AI项目台/`**——总线是运行期数据，测试写进工作树等于
让判据自己污染被检对象。
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest

from acp import recover
from acp.bus import acl, layout
from acp.bus.gateway import (
    ALLOWED_FRONTMATTER_KEYS,
    CARD_LABELS,
    BusError,
    BusGateway,
    GuardEvent,
    TaskCardBody,
    render_card,
    split_card,
    strip_frontmatter,
    validate_card,
)
from acp.bus.orchestrator_writer import ORCHESTRATOR, OrchestratorWriter, StatusRow

AGENTS = ("workbuddy", "qoder", "doubao")

BODY = TaskCardBody(
    objective="产出静态首页",
    inputs="AI项目台/demo/tasks/T01-home.md",
    expected_output="qoder-workspace/index.html",
)

FIVE_CLASSES = {
    "artifact_floor": "min_bytes=200, min_lines=8, format=html",
    "acceptance_criteria": "[含 h1 标题, 首屏无外链脚本]",
    "token_budget": "1000",
    "executor_agent_id": "qoder",
    "reviewer_agent_id": "workbuddy",
    "acceptor_agent_id": "doubao",
    "review_round": "1",
    "handoff_count": "0",
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / layout.BUS_ROOT_NAME / "demo"
    project.mkdir(parents=True)
    return project


@pytest.fixture
def gateway(root: Path):
    events: list[GuardEvent] = []
    return BusGateway(root, audit_sink=events.append), events


@pytest.fixture
def bus(root: Path):
    gw = BusGateway(root)
    writer = OrchestratorWriter(gw)
    yield gw, writer
    writer.stop()


def adapter(agent_id: str) -> acl.Actor:
    """生产形态的适配器身份：三支 transport 交出的都是 `adapter:<agent_id>`（`workbuddy:249` 等）。

    这里刻意不写裸 `agent_id`：ACL 的只读放行判据比的就是这个串（`acl.py:159`），
    用例用裸名构造会让"只读岗写自己 workspace 被拒"因为名字不匹配而**假绿**。
    """
    return acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}{agent_id}", "frontend")


def cap(agent_id: str, task_id: str = "T01") -> acl.Capability:
    return acl.Capability(agent_id, task_id)


def make_row(task_id: str, status: str = "EXECUTING") -> StatusRow:
    return StatusRow(
        task_id=task_id,
        status=status,
        executor="qoder",
        reviewer="workbuddy",
        acceptor="doubao",
        artifact_path=f"qoder-workspace/{task_id}.html",
        usage={"TOKEN": Decimal("0"), "SEAT_CREDIT": Decimal("3"), "BYOK": Decimal("0")},
    )


# --- 契约 §7 前三行：三种写路径全被拒，且落审计 --------------------------------------


def test_adapter_cannot_write_public_tasks_dir(gateway):
    gw, events = gateway
    with pytest.raises(BusError) as caught:
        gw.write_artifact(adapter("qoder"), "tasks/T02-next.md", "x", cap("qoder"))
    assert caught.value.code == acl.WRITE_FORBIDDEN
    assert caught.value.http_status == 403
    assert [(e.kind, e.code) for e in events] == [("GUARD_VIOLATION", acl.WRITE_FORBIDDEN)]
    assert not (gw.root / "tasks" / "T02-next.md").exists()


def test_adapter_cannot_write_other_workspace(gateway):
    gw, events = gateway
    with pytest.raises(BusError) as caught:
        gw.write_artifact(adapter("qoder"), "doubao-workspace/x.md", "x", cap("qoder"))
    assert caught.value.code == acl.WRITE_FORBIDDEN
    assert len(events) == 1


def test_readonly_actor_denied_on_public_paths(gateway):
    """FR-033 / owner 2026-10-13 裁决 1：只读身份写**公共区**一律拒，并落 `READONLY_VIOLATION` 审计。

    前身 `test_readonly_role_denied_on_every_path` 把"含其自身 workspace"也圈在拒绝里；
    裁决 1 把只读定为**任务级** ⇒ "自身 workspace"那一格移到正向用例
    （`test_readonly_actor_can_write_its_own_workspace`），这条只留"不是它的路径"。
    """
    gw, events = gateway
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}workbuddy", "reviewer", read_only=True)
    for path in ("tasks/T02-next.md", "status.md", "docs/meetings/2026-10-13.md"):
        with pytest.raises(BusError) as caught:
            gw.write_artifact(reviewer, path, "x", cap("workbuddy"))
        assert caught.value.code == acl.READ_ONLY_VIOLATION, path
        assert caught.value.guard_kind == "READONLY_VIOLATION", path
    assert {event.kind for event in events} == {"READONLY_VIOLATION"}
    assert len(events) == 3
    assert not (gw.root / "tasks" / "T02-next.md").exists()


def test_readonly_actor_cannot_write_other_workspace(gateway):
    """反向（裁决 1 的另一半）：只读身份写**别人**的 workspace 仍拒，且拒因是 `read_only_violation`。

    这条与 `test_adapter_cannot_write_other_workspace` 成对才成立：同一个路径，非只读身份拿到
    `write_forbidden`（归属不符），只读身份拿到 `read_only_violation`。少了这一条，
    "把只读那整个分支删掉"这种变异会退化成"归属检查顺手拦了"⇒ 看着像没坏。
    """
    gw, events = gateway
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}workbuddy", "reviewer", read_only=True)
    with pytest.raises(BusError) as caught:
        gw.write_artifact(reviewer, "qoder-workspace/inside-others.md", "x", cap("workbuddy"))
    assert caught.value.code == acl.READ_ONLY_VIOLATION
    assert caught.value.http_status == 403
    assert len(events) == 1
    assert not (gw.root / "qoder-workspace" / "inside-others.md").exists()


def test_readonly_actor_can_write_its_own_workspace(gateway):
    """正向（裁决 1，此前**没有任何用例钉住**）：只读身份写自己的 workspace 放行。

    放行只放到"不再被第 2 条拒"这一步：第 4 条照旧要一份指向自己的任务级能力凭证 ⇒
    缺凭证时拿到的是 `write_forbidden`，不是 ALLOW。这一半是这条用例不被写成"直接放行"的理由。
    """
    gw, events = gateway
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}workbuddy", "reviewer", read_only=True)
    written = gw.write_artifact(reviewer, "workbuddy-workspace/notes.md", "reviewed", cap("workbuddy"))
    assert written.name == "notes.md"
    assert (gw.root / "workbuddy-workspace" / "notes.md").read_text(encoding="utf-8") == "reviewed"
    with pytest.raises(BusError) as caught:
        gw.write_artifact(reviewer, "workbuddy-workspace/second.md", "x", None)
    assert caught.value.code == acl.WRITE_FORBIDDEN, "缺凭证仍是第 4 条的活，第 2 条放行不等于直接 ALLOW"
    assert len(events) == 1, "第一次正常写不该落违规行，只有缺凭证那次落"


def test_readonly_role_reads_but_writes_nothing(gateway):
    """跨 workspace **读通**、被拒的那次写**不许留下半个产物** —— 两件事必须同时成立。

    本行的历史（保留，不改写）：首版这里断言的是"reviewer 不能把自己的 verdict 写进自己的 workspace"，
    那时契约 §2 规则三写的是"含其自身 workspace 都无写权限"，我一度把期望写反过（判"实现错了"）；
    2026-10-13 owner 裁决 1 把只读定为**任务级** ⇒ 同一件事现在允许了，正向证据在
    `test_readonly_actor_can_write_its_own_workspace`。这条保留的理由换成"读通 + 拒写不落盘"。
    """
    gw, events = gateway
    gw.write_artifact(adapter("qoder"), "qoder-workspace/index.html", "<h1>ok</h1>", cap("qoder"))
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}workbuddy", "reviewer", read_only=True)
    assert "ok" in gw.read(reviewer, "qoder-workspace/index.html")
    with pytest.raises(BusError) as caught:
        gw.write_artifact(reviewer, "qoder-workspace/index.html", "<h1>tampered</h1>", cap("workbuddy"))
    assert caught.value.code == acl.READ_ONLY_VIOLATION
    assert gw.read(adapter("qoder"), "qoder-workspace/index.html") == "<h1>ok</h1>", "被拒的写不许留下半个产物"
    assert len(events) == 1, "读与拒写之外的动作不该被记成违规"


def test_readonly_gate_is_not_a_blanket_deny(gateway):
    """防"全拦冒充安全"：只读岗的读、执行者的写，两条正常路径必须照常通。"""
    gw, events = gateway
    executor = adapter("qoder")
    gw.write_artifact(executor, "qoder-workspace/a.html", "<p>x</p>", cap("qoder"))
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}workbuddy", "reviewer", read_only=True)
    assert gw.read(reviewer, "qoder-workspace/a.html") == "<p>x</p>"
    assert events == []


def test_cross_workspace_read_is_allowed(gateway):
    """FR-013 的正面：跨 workspace **读允许**、写拒绝——只测拒会漏掉"把读也禁了"的实现。"""
    gw, _ = gateway
    gw.write_artifact(adapter("doubao"), "doubao-workspace/note.md", "content", cap("doubao"))
    assert gw.read(adapter("qoder"), "doubao-workspace/note.md") == "content"


# --- §2 规则三与 fail-closed 边界 -----------------------------------------------------


def test_unmapped_path_denied_not_defaulted_open(gateway):
    gw, events = gateway
    with pytest.raises(BusError) as caught:
        gw.write_artifact(ORCHESTRATOR, "notes/random.md", "x", None)
    assert caught.value.code == acl.UNMAPPED_PATH
    assert len(events) == 1


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside.md", "qoder-workspace/../../escape.md", "C:/Windows/x.md"],
)
def test_escaping_paths_rejected(gateway, path):
    gw, _ = gateway
    with pytest.raises(BusError) as caught:
        gw.write_artifact(adapter("qoder"), path, "x", cap("qoder"))
    assert caught.value.code in {"bad_path", "path_escape"}


def test_workspace_write_without_capability_denied(gateway):
    gw, _ = gateway
    with pytest.raises(BusError) as caught:
        gw.write_artifact(adapter("qoder"), "qoder-workspace/x.md", "x", None)
    assert caught.value.code == acl.WRITE_FORBIDDEN


def test_adapter_own_workspace_write_allowed(gateway):
    """ACL 的正对照：正当组合必须写成功，否则上面所有的"拒"都可能只是恒真。"""
    gw, _ = gateway
    written = gw.write_artifact(
        adapter("qoder"), "qoder-workspace/index.html", "<h1>hi</h1>", cap("qoder")
    )
    assert written.read_text(encoding="utf-8") == "<h1>hi</h1>"


# --- 静态表本身可遍历断言（D-4：违规变写入即失败的前提是表能被遍历） --------------------


def test_acl_patterns_are_unique():
    patterns = [rule.pattern for rule in acl.RULES]
    assert len(patterns) == len(set(patterns))


@pytest.mark.parametrize(
    "path, expected_owner",
    [
        ("tasks/T01-home.md", acl.PUBLIC_OWNER),
        ("results/T01.md", acl.PUBLIC_OWNER),
        ("status.md", acl.PUBLIC_OWNER),
        ("daily-log.md", acl.PUBLIC_OWNER),
        ("AGENTS.md", acl.PUBLIC_OWNER),
        ("docs/meetings/2026-10-12.md", acl.PUBLIC_OWNER),
        ("qoder-workspace/index.html", "adapter:qoder"),
        ("workbuddy-workspace/n.md", "adapter:workbuddy"),
    ],
)
def test_declared_owner_is_single_valued_per_path(path, expected_owner):
    assert acl.declared_owner(path) == expected_owner
    hits = [rule for rule in acl.RULES if acl._pattern_hit(rule.pattern, path)]
    assert len(hits) == 1, f"{path} 命中 {len(hits)} 条模式，归属不唯一"


def test_windows_separators_map_to_the_same_owner():
    assert acl.declared_owner("qoder-workspace\\sub\\x.md") == "adapter:qoder"


# --- §3 任务卡：四行拒写 + 五类封闭（断言②③）+ 无 frontmatter 仍可解析（断言④）---------


def test_four_line_body_rejected(gateway):
    gw, _ = gateway
    text = render_card(BODY, {}) + "备注: 第四行不属于协议\n"
    with pytest.raises(BusError) as caught:
        validate_card(text)
    assert "三行封顶" in caught.value.detail
    with pytest.raises(BusError):
        gw.write_artifact(ORCHESTRATOR, "tasks/T01-home.md", text, None)
    assert not (gw.root / "tasks" / "T01-home.md").exists()


@pytest.mark.parametrize("drop", CARD_LABELS)
def test_missing_line_rejected(gateway, drop):
    """缺行与超行数同一条判据的两面：只判超行数，缺 `期望产物` 的卡片会拿到绿灯。"""
    lines = [f"{label}: 内容" for label in CARD_LABELS if label != drop]
    with pytest.raises(BusError) as caught:
        validate_card("\n".join(lines) + "\n")
    assert "缺行" in caught.value.detail


def test_duplicate_label_rejected():
    """构造刻意留在三行内：四行的话先撞"三行封顶"，这条判据就永远轮不到开火。"""
    with pytest.raises(BusError) as caught:
        validate_card("目标: a\n目标: b\n输入: c\n")
    assert "标签重复" in caught.value.detail


def test_free_text_line_rejected():
    with pytest.raises(BusError) as caught:
        validate_card("目标: a\n随手记一句\n期望产物: c\n")
    assert "正文行不符合" in caught.value.detail


def test_sixth_frontmatter_class_rejected_on_render():
    with pytest.raises(BusError) as caught:
        render_card(BODY, {"web_search_enabled": "true"})
    assert "第六类" in caught.value.detail


def test_sixth_frontmatter_class_rejected_on_write(gateway):
    """渲染面拒了还不够，落盘面也必须拒（否则绕过 `render_card` 直接写文本就能塞字段）。"""
    gw, _ = gateway
    text = (
        "---\nartifact_floor: min_bytes=200\nrole_grant: write_anywhere\n---\n"
        "目标: a\n输入: b\n期望产物: c\n"
    )
    with pytest.raises(BusError) as caught:
        gw.write_artifact(ORCHESTRATOR, "tasks/T01-home.md", text, None)
    assert caught.value.code == acl.WRITE_FORBIDDEN
    assert "超五类" in caught.value.detail


def test_all_five_classes_accepted():
    """正对照：五类全给也建得出来——不然"拒第六类"可能其实是"拒所有"。"""
    text = render_card(BODY, FIVE_CLASSES)
    meta, body = split_card(text)
    assert set(meta) == set(FIVE_CLASSES) <= set(ALLOWED_FRONTMATTER_KEYS)
    assert validate_card(text) == BODY


def test_rendered_frontmatter_never_reaches_the_agent(gateway):
    """分层条件②/断言①：Agent 可见面恰为三行，且**不含任何 frontmatter 键与值**。"""
    gw, _ = gateway
    rel = "tasks/T01-home.md"
    content = render_card(BODY, FIVE_CLASSES)
    gw.write_artifact(ORCHESTRATOR, rel, content, None)
    rendered = gw.render_for_agent(rel)
    assert rendered.strip().split("\n") == [
        f"目标: {BODY.objective}",
        f"输入: {BODY.inputs}",
        f"期望产物: {BODY.expected_output}",
    ]
    for key in FIVE_CLASSES:
        assert key not in rendered, f"frontmatter 键 {key!r} 漏进了 Agent 可见面"
    assert "---" not in rendered


def test_card_without_frontmatter_still_parses():
    """分层条件③/断言④：无 frontmatter 时三行仍可独立解析（回退路径的唯一输入）。"""
    plain = "目标: a\n输入: b\n期望产物: c\n"
    assert split_card(plain) == ({}, plain)
    assert validate_card(plain) == TaskCardBody("a", "b", "c")
    assert strip_frontmatter(plain) == plain


# --- §4 + §7 行 5：单点串行写，无半行 --------------------------------------------------


def test_public_writes_run_on_one_worker_thread(bus):
    _, writer = bus
    caller = threading.get_ident()
    executed_on = writer._submit(lambda: threading.get_ident())
    assert executed_on == writer.worker_thread_id
    assert executed_on != caller, "公共区写请求没走串行队列"


def test_concurrent_status_updates_leave_no_half_rows(bus):
    """§7 行 5：并发 10 次 `status.md` 更新 ⇒ 串行单点写、无 interleaving 半行。

    首版把"读现有行"留在调用线程上，十个线程各自读到旧表再整表写回 ⇒ 丢更新，
    这张表只剩一两行；本用例把那个缺陷拦了下来（修复见 orchestrator_writer 的注释）。
    """
    _, writer = bus
    threads = [
        threading.Thread(target=writer.upsert_status, args=(make_row(f"T{index:02d}"),))
        for index in range(1, 11)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = (writer.gateway.root / layout.STATUS_MD).read_text(encoding="utf-8")
    data = [line for line in text.split("\n") if line.startswith("|") and "---" not in line]
    header, *rows = data
    assert len(rows) == 10, f"期望 10 行，实到 {len(rows)}（丢更新或半行）"
    assert {StatusRow.parse(line).task_id for line in rows} == {
        f"T{index:02d}" for index in range(1, 11)
    }
    assert "\r" not in text


def test_concurrent_event_appends_keep_every_line(bus):
    _, writer = bus
    threads = [
        threading.Thread(
            target=writer.append_event,
            kwargs={"entity": f"task=T{index:02d}", "action": "DISPATCH", "actor": "qoder",
                    "summary": f"第 {index} 次下发"},
        )
        for index in range(1, 11)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = [
        line
        for line in (writer.gateway.root / layout.DAILY_LOG_MD)
        .read_text(encoding="utf-8")
        .split("\n")
        if line
    ]
    assert len(lines) == 10
    assert len({line.split("|")[4].strip() for line in lines}) == 10, "事件行互相覆盖"
    for line in lines:
        assert line.startswith("- ") and line.count("|") == 4, f"半行或格式不符：{line!r}"


def test_status_upsert_replaces_not_appends(bus):
    """§6 关系式"每张任务恰有一行"由构造成立：同一任务两次迁移只留最后一行。"""
    _, writer = bus
    writer.upsert_status(make_row("T01", "EXECUTING"))
    writer.upsert_status(make_row("T01", "ARTIFACT_INVALID"))
    text = (writer.gateway.root / layout.STATUS_MD).read_text(encoding="utf-8")
    rows = [line for line in text.split("\n") if line.startswith("| T01 ")]
    assert len(rows) == 1 and "ARTIFACT_INVALID" in rows[0]


def test_usage_columns_are_never_summed(bus):
    """FR-061：三口径分列，MUST NOT 出现一个合并后的总数。"""
    _, writer = bus
    writer.upsert_status(make_row("T01"))
    row = StatusRow.parse((writer.gateway.root / layout.STATUS_MD).read_text(encoding="utf-8")
                          .split("\n")[-2])
    assert sorted(row.usage) == ["BYOK", "SEAT_CREDIT", "TOKEN"]
    assert row.usage["SEAT_CREDIT"] == Decimal("3")


# --- T019：AGENTS.md 由代码生成 --------------------------------------------------------


def test_agents_md_is_generated_from_the_static_table(bus):
    _, writer = bus
    rel = writer.write_agents_md("demo", AGENTS)
    text = (writer.gateway.root / rel).read_text(encoding="utf-8")
    for rule in acl.RULES:
        assert rule.pattern in text and rule.owner in text
    for name in AGENTS:
        assert f"{name}-workspace/" in text
    for label in CARD_LABELS:
        assert f"{label}:" in text


def test_agents_md_denied_for_adapters(gateway):
    gw, _ = gateway
    with pytest.raises(BusError):
        gw.write_artifact(adapter("qoder"), layout.AGENTS_MD, "x", cap("qoder"))


def test_ensure_layout_creates_contract_dirs(bus):
    """T014 的逐字判据：契约 §1 的**目录**条目一个不多一个不少，三个 .md 不在这里建。"""
    _, writer = bus
    made = writer.ensure_layout(AGENTS)
    assert set(made) == set(layout.PUBLIC_DIRS) | {f"{name}-workspace" for name in AGENTS}
    for entry in layout.PUBLIC_DIRS:
        assert (writer.gateway.root / entry).is_dir()
    for name in AGENTS:
        assert (writer.gateway.root / f"{name}-workspace").is_dir()
    assert (writer.gateway.root / "docs" / "meetings").is_dir()
    # PUBLIC_ENTRIES 是契约 §1 的全清单；文件条目若被 mkdir，这条就会红（首版踩过）。
    assert not (writer.gateway.root / layout.STATUS_MD).is_dir()
    assert not (writer.gateway.root / layout.AGENTS_MD).is_dir()


# --- §7 行 6 + T022：停库后按 §4 重建三元组 --------------------------------------------


def build_project(writer: OrchestratorWriter, gw: BusGateway) -> None:
    writer.ensure_layout(AGENTS)
    writer.write_agents_md("demo", AGENTS)
    writer.write_task_card("T01", "home", BODY, FIVE_CLASSES)
    writer.upsert_status(make_row("T01", "DELIVERED"))
    writer.write_result_index("T01", [("qoder-workspace/index.html", 512, 18, "html")])
    gw.write_artifact(adapter("qoder"), "qoder-workspace/index.html", "<h1>hi</h1>", cap("qoder"))


def test_recover_rebuilds_the_triple_without_pg(bus):
    gw, writer = bus
    build_project(writer, gw)
    result = recover.rebuild(gw.root.parent, "demo")
    assert result.card_count == result.status_row_count == 1
    triple = result.triples[0]
    assert (triple.task_id, triple.status) == ("T01", "DELIVERED")
    assert triple.objective == BODY.objective
    assert triple.artifacts == (("qoder-workspace/index.html", 512, 18, "html"),)
    assert result.pg_side_checked is False, "PG 侧那半条关系式不能由总线侧自证"


def test_recover_reads_body_when_frontmatter_is_absent(bus, root):
    """条件③的实跑面：把 frontmatter 整段抹掉（模拟 PG 没了、元数据随之内存态丢失），
    三元组仍然重建得出来——这是"回退路径不只存在于文档"的那条判据。"""
    gw, writer = bus
    build_project(writer, gw)
    card = root / layout.TASKS_DIR / "T01-home.md"
    stripped = strip_frontmatter(card.read_text(encoding="utf-8")) + "\n"
    card.write_bytes(stripped.encode("utf-8"))
    result = recover.rebuild(root.parent, "demo")
    assert result.triples[0].objective == BODY.objective


def test_recover_fails_when_status_row_is_missing(bus):
    gw, writer = bus
    build_project(writer, gw)
    (gw.root / layout.STATUS_MD).write_bytes(
        b"| task_id | a | b | c | d | e | f |\n|---|---|---|---|---|---|---|\n"
    )
    with pytest.raises(recover.ConsistencyError) as caught:
        recover.rebuild(gw.root.parent, "demo")
    assert "只在 tasks/ 出现" in str(caught.value)


def test_recover_fails_on_malformed_task_filename(bus):
    gw, writer = bus
    build_project(writer, gw)
    (gw.root / layout.TASKS_DIR / "T1-bad.md").write_bytes(
        "目标: a\n输入: b\n期望产物: c\n".encode("utf-8")
    )
    with pytest.raises(recover.ConsistencyError):
        recover.rebuild(gw.root.parent, "demo")


def test_recover_cli_reports_counts_and_exit_codes(bus, monkeypatch):
    """T022 的调用形态判据：`python -m acp.recover --from-file-bus <project>`。

    `--bus-root` 默认取相对路径 `AI项目台`，所以工作目录要落在**总线根的父级**；
    首版 chdir 到根本身 ⇒ 拼出 `AI项目台/AI项目台/demo`，CLI 报"目录不存在"退 1。
    """
    gw, writer = bus
    build_project(writer, gw)
    monkeypatch.chdir(gw.root.parent.parent)
    assert recover.main(["acp.recover", "--from-file-bus", "demo"]) == 0
    assert recover.main(["acp.recover", "--from-file-bus", "nope"]) == 1
    assert recover.main(["acp.recover", "--from-file-bus", "demo", "--bus-root", "AI项目台"]) == 0
