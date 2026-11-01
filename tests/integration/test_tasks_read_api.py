"""Phase 15 后端第 1、2 格：`GET /tasks` 与 `GET /tasks/{task_id}` 的读面判据。

**M2-W20 在这一格关闭**。它的重开条件写成的是关系式："出现任一条 `GET /tasks*` ⇒ 看板图那一栏
MUST 与该端点同笔落"。在此之前公开面上 `/tasks` 那一族**三个操作全是写**（`PATCH` / `worker` /
`escalation`），唯一的拓扑读数 `card_order` 挂在 `PATCH` 的响应里 ⇒ 打开页面拿不到任何一张卡，
空 DAG 会被读成"今天没有任务"。本文件先钉"端点自己读得对不对"，同笔的前端那一栏紧随其后。

**这一档用生产会话工厂 + 真提交**（形状取自 `test_approval_commit_boundary.py`，不是照搬
`test_request_intake.py` 那条"外层事务回滚"的隔离）：读端点唯一有意义的失效形态是
"读不到别人提交过的行"，而同一条连接上的会话读得到未提交的行 —— 拿那种夹具跑读端点，
"没读到"这一格永远没有形状。代价两条都认：
① 写进库的行是真提交的 ⇒ 收尾按 `feature_request_id` / `agent_id` 圈定删干净，并自证删空了，
   否则下一发全量的 PG 档入口守卫（`tests/conftest.py` 的十张表）会红；
② 用例之间不共享现场 ⇒ 每一发自己建自己要读的那几张卡。

**本文件证明不到的**（明写）：
- 不证明 `PATCH` / `worker` / `escalation` 那三支也把连接还回去了 —— 那是登记不修的 **M2-W13**
  （它们至今走 `_session()`，不 `close()`）。本文件只钉**新加的两条 GET**
  （`test_the_two_new_reads_return_the_connection_they_borrow` + 末尾那条形状看守）。
- 不画 DAG、不量前端：节点与边的形状判据在那一笔的 vitest / guards 里。
  本文件只钉"节点序与边由端点给出"这件事（`_topo_order` 那三条）。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from acp.api import tasks as tasks_api
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import review as review_node
from acp.persistence import events
from acp.persistence.models_approval import ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.persistence.session import make_session_factory
from acp.review import service as review_service

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG"
)

NOW = dt.datetime(2026, 10, 22, 9, 0, 0, tzinfo=dt.timezone.utc)
TAG = "acp-test-tasks-read"
EXECUTOR, REVIEWER, ACCEPTOR = "acp-p15-exec", "acp-p15-rev", "acp-p15-acc"
CRITERIA = ["首屏有公司名", "样式文件被引用"]


@pytest.fixture(scope="session")
def engines() -> Iterator[tuple[Engine, Engine]]:
    """应用与夹具共用一个 engine（池 2 / 不许溢出），读断言与 `pg_stat_activity` 走另一个。

    池做小的理由与笔 U 同一句：默认"池 5 + 溢出 10"会把"每发漏一条连接"推到第 16 发才炸。
    """
    app_engine = create_engine(
        db_url(),
        future=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={"application_name": f"{TAG}-app"},
    )
    read_engine = create_engine(db_url(), future=True, connect_args={"application_name": f"{TAG}-read"})
    try:
        yield app_engine, read_engine
    finally:
        app_engine.dispose()
        read_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def agents(engines):
    """三岗各一个 EN-03 单元（会话级，跑完删）。`executor_agent_id` 那三列有 FK，绕不开。"""
    engine, _ = engines
    with Session(engine) as session:
        for agent_id in (EXECUTOR, REVIEWER, ACCEPTOR):
            session.add(
                AgentUnit(
                    agent_id=agent_id,
                    form="SDK",
                    tier=2,
                    cost_band="MID",
                    max_parallel=2,
                    current_load=0,
                    historical_success_rate=Decimal("0.5"),
                    success_rate_sample=4,
                    workspace_dir=f"{agent_id}-workspace",
                    enabled=True,
                )
            )
        session.commit()
    yield (EXECUTOR, REVIEWER, ACCEPTOR)
    # 删除走**读侧那个 engine**（理由同 `world` 里那一条：应用侧的池正是被验对象，它坏时清理必须还能跑）。
    _app_engine, read_engine = engines
    with Session(read_engine) as session:
        for agent_id in (EXECUTOR, REVIEWER, ACCEPTOR):
            session.execute(text("delete from agent_unit where agent_id = :i"), {"i": agent_id})
        session.commit()


@pytest.fixture
def world(engines):
    """本用例建过的一切都记在这里，收尾按 id 圈定删（删除顺序按外键从叶子到根）。

    逐条 `= :id` 而不是 `= any(:ids)`：本仓的 PG 档要同时跑在 Windows 与 WSL 两套 psycopg 上，
    数组绑参的形态在两处不一致，而清理条件写错的代价是"下一发全量在入口守卫上红"——
    那种红根因离现场很远，不值得为省几行 SQL 去换。

    **删与自证都挂在读端那个 engine 上，不挂应用那个**。这不是风格问题，是变异实测踩出来的：
    M9（把 `_store` 的收尾拿掉）让读档整片红，而那时清理若走应用侧的池（2 条、不许溢出），
    teardown 自己就卡在借连接上 ⇒ 行留在库里 ⇒ 下一发在 `conftest` 的入口守卫上集体红，
    症状离现场极远（口径同 `test_approval_commit_boundary.py::seeded` 里那条"清理不能依赖被测对象的健康度"）。
    建单走应用侧（那是被验路径的现场），删除走读侧。
    """
    app_engine, read_engine = engines
    created = {"requests": [], "snapshots": [], "events": []}

    yield created

    factory = make_session_factory(read_engine)
    with factory() as session:
        for request_id in created["requests"]:
            for table in ("artifact", "review_verdict", "task_card"):
                session.execute(
                    text(f"delete from {table} where feature_request_id = :r"), {"r": request_id}
                )
        for event_id in created["events"]:
            session.execute(text("delete from event_log where id = :i"), {"i": event_id})
        for snapshot_id in created["snapshots"]:
            session.execute(text("delete from snapshot where id = :s"), {"s": snapshot_id})
        for request_id in created["requests"]:
            session.execute(text("delete from feature_request where id = :r"), {"r": request_id})
        session.commit()
    with read_engine.connect() as conn:
        left = sum(
            int(
                conn.execute(
                    text("select count(*) from feature_request where id = :r"), {"r": request_id}
                ).scalar()
            )
            for request_id in created["requests"]
        )
    assert left == 0, f"本用例留下了 {left} 行需求单 ⇒ 收尾的圈定条件自己漏了一张表"


def make_app(app_engine: Engine, *, bus_root: Path | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.state.session_factory = lambda: Session(bind=app_engine)
    if bus_root is not None:
        app.state.bus_root = bus_root
    return TestClient(app)


def seed_request(
    world, engine, *, project_key: str, created_at=NOW, cards=1, with_artifact=True, depends=None
):
    """建一张**已提交**的需求单 + 存档点 + N 张卡（`T01..`），可选一份 valid 产物。

    返回 `(request_id, [卡号...])`。卡号跨单重名是真的（每张单都从 `T01` 起号），
    所以这条助手也是"409 那两发"的现场构造器。

    `depends` 给的是 `{卡号: [它依赖的卡号]}`，不给就沿用默认的**链**（`T02` 依赖 `T01`、…）。
    有这个入口是因为"卡号升序"与"拓扑序"在默认那条链上是**同一个答案** —— 要区分这两种坏，
    现场必须造得出"后号卡是前号卡的前提"那种形状。
    """
    request_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    world["requests"].append(request_id)
    world["snapshots"].append(snapshot_id)
    ids = [f"T{index:02d}" for index in range(1, cards + 1)]
    edges = depends if depends is not None else {
        card_id: [ids[position - 1]] for position, card_id in enumerate(ids) if position
    }
    with Session(engine, expire_on_commit=False) as session:
        session.add(
            FeatureRequest(
                id=request_id,
                project_key=project_key,
                raw_input="做个介绍公司产品的静态网站",
                summary_digest={"自行补全项": [], "澄清问题": []},
                clarification_waived=False,
                status="SPLIT",
                created_at=created_at,
            )
        )
        session.add(
            Snapshot(
                id=snapshot_id,
                round_no=1,
                phase="PRE_WORK",
                task_card_ids=ids,
                ref=f"local:probe-{snapshot_id}",
                rolled_back_at=None,
            )
        )
        session.flush()
        for position, card_id in enumerate(ids):
            session.add(
                TaskCard(
                    id=card_id,
                    feature_request_id=request_id,
                    title=f"卡 {card_id}",
                    difficulty=2 + position,
                    skill_tags=["frontend_coding"],
                    depends_on=list(edges.get(card_id, [])),
                    executor_agent_id=EXECUTOR,
                    reviewer_agent_id=REVIEWER,
                    acceptor_agent_id=ACCEPTOR,
                    artifact_floor_min_bytes=512,
                    artifact_floor_min_lines=12,
                    artifact_floor_format="html",
                    acceptance_criteria=list(CRITERIA),
                    token_budget=Decimal("1200") if position == 0 else None,
                    deadline_minutes=30 if position == 0 else None,
                    handoff_count=1 if position else 0,
                    review_round=0,
                    rework_count=0,
                    status="REVIEWING",
                    snapshot_id=snapshot_id,
                )
            )
        # 卡的 INSERT MUST 先发到 PG 再插产物：`Artifact` 与 `TaskCard` 之间没有 `relationship()`，
        # SQLAlchemy 的 UoW 不会按表间外键排这两个 mapper 的顺序（实测就是撞在这里：
        # 不加这一发 flush 时产物行先到 ⇒ `fk_artifact_...` 外键违例）。
        session.flush()
        if with_artifact:
            session.add(
                Artifact(
                    id=uuid.uuid4(),
                    feature_request_id=request_id,
                    task_card_id="T01",
                    writer_agent_id=EXECUTOR,
                    rel_path=f"{EXECUTOR}-workspace/index.html",
                    bytes=240,
                    lines=9,
                    format="html",
                    self_reported_status="success",
                    validated_status="valid",
                    invalid_reason=None,
                    created_at=created_at,
                )
            )
        session.commit()
    return request_id, ids


def bus_for(tmp_path: Path, project_key: str, *, rel_path: str, text_body: str) -> Path:
    """`<bus_root>/<project_key>/<rel_path>` —— 与 `archive.archive_root()` 的拼法同形。"""
    root = tmp_path / "results"
    target = root / project_key / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text_body, encoding="utf-8")
    return root


# ---------------------------------------------------------------- 一、列表：看板要的字段都在


def test_the_list_carries_every_field_the_board_asks_for(engines, world):
    """owner 点名的七样一个都不许缺：卡号、标题、状态、依赖、执行者、审查者、验收者。

    写成一串精确值而不是 `in body` 的抽查：`depends_on` 少一项、三岗读成同一个名字，
    这两种坏都长得像"字段存在"。断言取的是**值**，不是键的存在性。
    """
    app_engine, _ = engines
    request_id, ids = seed_request(world, app_engine, project_key="p15-fields", cards=2)

    body = make_app(app_engine).get("/tasks").json()
    assert len(body) == 2, body
    first, second = body[0], body[1]
    assert (first["id"], first["title"], first["status"]) == ("T01", "卡 T01", "REVIEWING")
    assert first["depends_on"] == []
    assert second["depends_on"] == ["T01"], "边丢了 ⇒ DAG 会画成两个孤岛"
    assert (
        first["executor_agent_id"],
        first["reviewer_agent_id"],
        first["acceptor_agent_id"],
    ) == (EXECUTOR, REVIEWER, ACCEPTOR), "三岗必须是三个不同的承载方（FR-031 的读数面）"
    assert first["feature_request_id"] == str(request_id)
    assert first["project_key"] == "p15-fields"
    assert first["difficulty"] == 2 and second["difficulty"] == 3
    assert first["handoff_count"] == 0 and second["handoff_count"] == 1
    assert first["skill_tags"] == ["frontend_coding"]


def test_rows_expose_the_split_source_and_a_null_stays_null(engines, world):
    """2026-10-31：`/tasks` 必须带 `split_source` —— 看板靠它打"兜底拆单 · 未接入真模型"的标识。

    `None` 那一格是**历史行**（本列上线前没人记过来源）：它不许被省略成缺键，也不许被读成 `llm`。
    反向对照在同一发里：先断言读到的是 `None`，再把列改成 `fallback_stub`，两读必须不同。
    """
    app_engine, _ = engines
    request_id, _ids = seed_request(world, app_engine, project_key="p15-split-source")

    before = make_app(app_engine).get("/tasks", params={"feature_request_id": str(request_id)}).json()
    assert before and all(item["split_source"] is None for item in before), before

    with Session(app_engine) as session:
        row = session.get(FeatureRequest, request_id)
        assert row is not None
        row.split_source = "fallback_stub"
        session.commit()

    after = make_app(app_engine).get("/tasks", params={"feature_request_id": str(request_id)}).json()
    assert {item["split_source"] for item in after} == {"fallback_stub"}
    detail = make_app(app_engine).get(
        "/tasks/T01", params={"feature_request_id": str(request_id)}
    ).json()
    assert detail["split_source"] == "fallback_stub", "详情与列表是同一组列（TaskFields），不许只在列表里给"


def test_the_list_orders_newest_request_first(engines, world):
    """跨需求单的排序在端点上定："最新那一单"这件事只该有一个答案。

    反向对照在同一发里：先建旧单再建新单，而库里两单都有 `T01` —— 如果排序只按卡号，
    两行的相对位置不会随建单顺序变，这条就恒绿。所以断的是**新单的两行在前**。
    单内那一维由下面两条接管（口径不同，各挡一种坏，合成一条会互相掩盖）。
    """
    app_engine, _ = engines
    older, _ = seed_request(
        world, app_engine, project_key="p15-old", created_at=NOW - dt.timedelta(days=1)
    )
    newer, _ = seed_request(world, app_engine, project_key="p15-new")

    body = make_app(app_engine).get("/tasks").json()
    order = [(item["feature_request_id"], item["id"]) for item in body]
    assert order == [
        (str(newer), "T01"),
        (str(older), "T01"),
    ], f"排出来的次序读起来像「哪个 id 小」而不是「哪一单新」：{order}"


def test_the_order_within_a_request_is_the_topology_not_the_card_number(engines, world):
    """`T01` 依赖 `T02` 时数组必须回 `[T02, T01]` —— 这一发专门把"卡号升序"与"拓扑序"分开。

    为什么必须单独钉：本文件其余现场用的都是**默认那条链**（`T02` 依赖 `T01`），在那种形状下
    "按卡号升序"和"按拓扑序"给出**同一个答案** ⇒ 只测链的话，把 `_topo_order` 改回
    `sorted(cards, key=lambda c: c.id)` 照样全绿，而前端会拿到一张箭头从后往前指的图。
    这里把依赖**反着摆**，就是为了让那一发改回去立刻红（M10）。

    这也是 M2-W20 重开条件里"图那一栏 MUST 与该端点同笔"的实质落点：节点序与边都出自这一发，
    前端只摆位连线、不再求一次拓扑。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(
        world, app_engine, project_key="p15-topo", cards=2, depends={"T01": ["T02"]}
    )

    body = make_app(app_engine).get("/tasks", params={"feature_request_id": request_id}).json()
    assert [item["id"] for item in body] == ["T02", "T01"], body
    assert [item["depends_on"] for item in body] == [[], ["T02"]], "边要跟着节点一起给，否则次序对了还是画不出图"


def test_a_filtered_out_prerequisite_is_not_reported_as_a_broken_structure(engines, world):
    """筛掉"前置那张卡"是看板的常态（`?status=REVIEWING`），但它不该被读成"库坏了"。

    现场：`T02` 依赖 `T01`，把 `T01` 改成 `READY` 再按 `status=REVIEWING` 筛 ⇒ 可见集只剩
    `T02`。归一化器如果只看得到这张筛过的子集，那条边就成了悬空引用。坏实现
    （把筛过的子集直接喂 `normalize()`）的症状是 **409 `dependency_structure_invalid`** ——
    一次完全合法的筛选被报成数据损坏，比排序错更难归因（M11 钉的就是这一发）。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-filter-topo", cards=2)
    with Session(app_engine) as session:
        first = session.query(TaskCard).filter_by(feature_request_id=request_id, id="T01").one()
        first.status = "READY"
        session.commit()

    client = make_app(app_engine)
    filtered = client.get(
        "/tasks", params={"feature_request_id": request_id, "status": "REVIEWING"}
    )
    assert filtered.status_code == 200, filtered.json()
    assert [item["id"] for item in filtered.json()] == ["T02"], "筛掉的行不许混进返回值"
    whole = client.get("/tasks", params={"feature_request_id": request_id}).json()
    assert [item["id"] for item in whole] == ["T01", "T02"], "不筛的时候整单都要在"


def test_a_dangling_dependency_is_a_409_instead_of_a_misleading_graph(engines, world):
    """库里真出现悬空引用（绕过端点直接改行）⇒ 409 并点名哪一单、哪条边，而不是退一张顺序不明的图。

    这一发是上一条的**反面**：上一条要求"合法的筛选不算坏"，这一条要求"真的坏要响亮"。
    反向对照 = 上一条（合法结构照样 200）—— 只测这一发的话，"一律退 409"那种实现也能绿。
    409 而不是 200 空数组的理由写在 `tasks.py::_topo_order` 的 docstring 里：一张空的或排错的
    DAG 都会被读成"今天没有任务"。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-dangling", cards=2)
    with Session(app_engine) as session:
        second = session.query(TaskCard).filter_by(feature_request_id=request_id, id="T02").one()
        second.depends_on = ["T99"]
        session.commit()

    response = make_app(app_engine).get("/tasks", params={"feature_request_id": request_id})
    assert response.status_code == 409, response.json()
    detail = response.json()["detail"]
    assert detail.startswith("dependency_structure_invalid"), detail
    assert "T99" in detail, "拒因里要点名那条悬空的依赖，否则调用方只能再去查一遍库"
    assert str(request_id) in detail


def test_the_list_carries_artifact_metadata_but_never_reads_the_disk(engines, world, tmp_path):
    """列表的产物那一格 = **目录形状**，源码这一列在响应的字段表里根本不存在。

    三格一起钉，缺一格就放过一种坏：
    ① `content` / `content_error` 两个键都不许出现 —— 强制点是 `TaskArtifactRef` 那张字段表，
       所以这里把 app 配上了 `bus_root`：一旦有人让列表改吃详情那个带源码的视图，
       这两格立刻现形（不配 bus_root 的话"读不读盘"在响应面上读不出来，那条断言就成了恒真）；
    ② 盘上**没有**这个文件也照样列出这一行 ⇒ 元数据来自 EN-06 的行，不是来自扫盘；
    ③ 列出的字段是实测数值（bytes/lines/format），文件树分组靠的就是这几样。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-meta", with_artifact=True)
    bus_root = tmp_path / "results"  # 建出来了，但里面一个产物文件都没有
    bus_root.mkdir()

    item = make_app(app_engine, bus_root=bus_root).get(
        "/tasks", params={"feature_request_id": request_id}
    ).json()[0]
    assert len(item["artifacts"]) == 1, item
    artifact = item["artifacts"][0]
    assert artifact["rel_path"] == f"{EXECUTOR}-workspace/index.html"
    assert (artifact["bytes"], artifact["lines"], artifact["format"]) == (240, 9, "html")
    assert "content" not in artifact, "列表里出现了 content ⇒ 一次列表请求读走了整个工作区"
    assert "content_error" not in artifact


# ---------------------------------------------------------------- 二、过滤与拒绝路径


def test_the_three_filters_narrow_the_same_result_set(engines, world):
    """`project_key` / `status` / `feature_request_id` 三个过滤项叠加而不是互相顶掉。

    每张需求单的卡号都从 `T01` 起，所以"按项目过滤"必须真的落到母单那一层：
    只看返回条数不够（两个项目的卡数可能一样），这里比的是 `project_key` 那一列。
    """
    app_engine, _ = engines
    mine, _ = seed_request(world, app_engine, project_key="p15-filter-a", cards=2)
    seed_request(world, app_engine, project_key="p15-filter-b", cards=3)
    client = make_app(app_engine)

    assert {item["project_key"] for item in client.get("/tasks", params={"project_key": "p15-filter-a"}).json()} == {
        "p15-filter-a"
    }
    by_card = client.get("/tasks", params={"feature_request_id": mine}).json()
    assert [item["id"] for item in by_card] == ["T01", "T02"], by_card
    assert len(client.get("/tasks", params={"status": "REVIEWING"}).json()) == 5
    assert client.get("/tasks", params={"project_key": "p15-filter-b", "status": "DELIVERED"}).json() == []


def test_an_unknown_status_is_refused_instead_of_reading_as_empty(engines, world):
    """拼错的状态词 MUST 422，不许回一个合法的 200 空数组。

    这是这里最容易被放过的一格：空数组在面板上长得和"没有这种状态的卡"一模一样，
    而真相是那次过滤条件根本没生效。反向对照同在一发里：域里的取值照样查得到行。
    """
    app_engine, _ = engines
    seed_request(world, app_engine, project_key="p15-status", cards=1)
    client = make_app(app_engine)

    response = client.get("/tasks", params={"status": "DOING"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("status_unknown")
    assert client.get("/tasks", params={"status": "REVIEWING"}).json(), "域里的合法取值应当查得到"


def test_reading_tasks_without_a_store_still_fails_closed(engines, world):
    """读端点也没绕开 fail-closed：没注入工厂 ⇒ 503，而不是"空清单"。

    "库里没接上"与"库里没有卡"必须是两个不同的读数 —— 后者在面板上是合法的现状，前者是事故。
    """
    app = FastAPI()
    app.include_router(tasks_api.router)
    client = TestClient(app)
    assert client.get("/tasks").status_code == 503
    assert client.get("/tasks/T01").status_code == 503
    assert client.get("/tasks").json()["detail"].startswith("store_not_configured")


# ---------------------------------------------------------------- 三、路由顺序（这条端点特有的坑）


def test_the_reviewing_list_is_not_swallowed_by_the_card_route(engines, world):
    """`GET /tasks/reviewing` MUST 不被 `GET /tasks/{task_id}` 吃掉。

    FastAPI 按**声明顺序**匹配：把动态段排在前面，`reviewing` 就被当成一张卡号叫 `reviewing` 的卡
    ⇒ 404 `task_not_found`。症状与"今天没有待评审的卡"完全一样（都是面板空），是本仓最难归因的一类坏，
    所以它需要的不是注释而是一条会红的用例。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-order", cards=1)

    client = make_app(app_engine)
    reviewing = client.get("/tasks/reviewing")
    assert reviewing.status_code == 200, reviewing.text
    assert [item["task_id"] for item in reviewing.json()] == ["T01"], reviewing.json()

    # 反向对照：同一个前缀下按卡号读的详情是**另一种形状**（`id` 而不是 `task_id`）。
    # 少了这一格，"两条路由互换了实现"这种改法还能靠上一条的 200 蒙过去。
    detail = client.get("/tasks/T01", params={"feature_request_id": request_id}).json()
    assert detail["id"] == "T01" and "task_id" not in detail


# ---------------------------------------------------------------- 四、详情：形状、源码、清单


def test_detail_carries_criteria_floor_and_the_request_status(engines, world):
    """详情 = 列表那七样 + 验收标准 + 产物门槛 + 预算/时限 + 母单的态。

    `request_status` 从 EN-01 读而不是从卡上推：原则 I 那条"卡上不复述需求单的状态"在读面上
    同样成立 —— 端点替前端"猜一个需求单状态"就是造了第二个事实源。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-shape", cards=1)

    body = make_app(app_engine).get("/tasks/T01", params={"feature_request_id": request_id}).json()
    assert body["acceptance_criteria"] == list(CRITERIA)
    assert body["artifact_floor"] == {"min_bytes": 512, "min_lines": 12, "format": "html"}
    assert Decimal(str(body["token_budget"])) == Decimal("1200")
    assert body["deadline_minutes"] == 30
    assert body["request_status"] == "SPLIT"
    assert str(body["request_created_at"]).startswith("2026-10-22T09:00")
    assert (body["review_round"], body["rework_count"]) == (0, 0)
    assert body["snapshot_id"]


def test_detail_carries_the_source_of_each_validated_artifact(engines, world, tmp_path):
    """抽屉里那段代码高亮的唯一数据源：详情带源码，路径落在存档根内才读。

    同一条请求里配一发越界对照：把 `rel_path` 改成 `../secret.txt` 后必须
    `content=None / content_error="path_outside_archive"`，且**那份内容不许出现在响应里** ——
    只断"读不到"而不断"响应里没那串字"，就会放过"把越界内容也回出去"那种更坏的修法。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-source", cards=1)
    source = "<html><h1>公司介绍</h1></html>\n"
    bus_root = bus_for(tmp_path, "p15-source", rel_path=f"{EXECUTOR}-workspace/index.html", text_body=source)

    body = make_app(app_engine, bus_root=bus_root).get(
        "/tasks/T01", params={"feature_request_id": request_id}
    ).json()
    artifact = body["artifacts"][0]
    assert artifact["content"] == source
    assert artifact["content_error"] is None

    with Session(app_engine) as session:
        row = session.query(Artifact).filter_by(feature_request_id=request_id).one()
        row.rel_path = "../outside.html"
        session.commit()
    (tmp_path / "outside.html").write_text("不该被读到", encoding="utf-8")

    escaped = make_app(app_engine, bus_root=bus_root).get(
        "/tasks/T01", params={"feature_request_id": request_id}
    ).json()
    assert escaped["artifacts"][0]["content"] is None
    assert escaped["artifacts"][0]["content_error"] == "path_outside_archive"
    assert "不该被读到" not in escaped["artifacts"][0]["rel_path"]


def test_detail_without_bus_root_reports_the_reason_instead_of_an_empty_source(engines, world):
    """没配 `bus_root` ⇒ `content_error="bus_root_not_configured"`，不是"这份产物是空的"。

    空源码与读不到源码在面板上是两件事：前者该画"这个文件 0 字节"，后者该画"没接文件总线"。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-nobus", cards=1)

    body = make_app(app_engine).get("/tasks/T01", params={"feature_request_id": request_id}).json()
    assert body["artifacts"][0]["content"] is None
    assert body["artifacts"][0]["content_error"] == "bus_root_not_configured"


def test_detail_lists_the_request_manifest_and_empty_is_not_an_error(engines, world):
    """详情带整单的 valid 清单（交付前的那份成员表）；一张 valid 产物都没有时是**空清单**不是 4xx。

    `assemble.validated_manifest` 对空集合抛 `nothing_to_deliver` —— 那个语义属于"组包"那一支
    （宁可不交，不交一份空的）。详情端点把它照直冒出去就会让一张刚建好的卡读成 4xx。
    """
    app_engine, _ = engines
    with_source, _ = seed_request(world, app_engine, project_key="p15-manifest", cards=1)
    empty, _ = seed_request(world, app_engine, project_key="p15-empty", cards=1, with_artifact=False)
    client = make_app(app_engine)

    entry = client.get("/tasks/T01", params={"feature_request_id": with_source}).json()["manifest"][0]
    assert (entry["rel_path"], entry["producer_agent_id"], entry["task_card_id"]) == (
        f"{EXECUTOR}-workspace/index.html",
        EXECUTOR,
        "T01",
    )
    nothing = client.get("/tasks/T01", params={"feature_request_id": empty}).json()
    assert nothing["manifest"] == [] and nothing["artifacts"] == [], nothing


# ---------------------------------------------------------------- 五、审查史：顺序只能来自留痕


def test_detail_restores_review_rounds_in_audit_order(engines, world):
    """两轮审查按 `round_no` 从 1 到 2 还原，逐条判定从 EN-08 配对回来。

    为什么顺序只能来自 EN-14：`review_verdict` **没有时间列也没有轮次列**，主键还是随机 UUID
    ⇒ 按它排出来的是 PG 的偶然顺序，不是审查的次序。这里故意把两轮**倒着插**（先插第 2 轮的
    结论行、再插第 1 轮），如果端点按结论表返回，两轮会互换而这条红。
    `review_opened` 那一笔也在场 —— 它是"评审被打开"，不是一轮结论：动作过滤器得有牙。
    """
    app_engine, _ = engines
    request_id, ids = seed_request(world, app_engine, project_key="p15-history", cards=1)
    second_findings = [{"criterion": CRITERIA[0], "met": False, "evidence": "第二轮还没过"}]
    first_findings = [{"criterion": CRITERIA[0], "met": False, "evidence": "第一轮缺引用"}] + [
        {"criterion": CRITERIA[1], "met": False, "evidence": "第一轮缺引用"}
    ]
    with Session(app_engine) as session:
        session.add(
            ReviewVerdict(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                task_card_id="T01",
                reviewer_agent_id=REVIEWER,
                verdict="CHANGES_REQUESTED",
                item_findings=second_findings,
                extra_criteria_used=False,
            )
        )
        session.add(
            ReviewVerdict(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                task_card_id="T01",
                reviewer_agent_id=REVIEWER,
                verdict="CHANGES_REQUESTED",
                item_findings=first_findings,
                extra_criteria_used=False,
            )
        )
        world["events"].append(
            events.record(
                session,
                entity="task_card",
                entity_id="T01",
                kind="REVIEW",
                actor_agent_id="owner-1",
                payload={
                    "node": review_node.NODE,
                    "action": review_node.ACTION_OPENED,
                    "prior_status": "RUNNING",
                },
                created_at=NOW,
            ).id
        )
        for round_no, findings, stamp in (
            (1, first_findings, NOW + dt.timedelta(minutes=1)),
            (2, second_findings, NOW + dt.timedelta(minutes=5)),
        ):
            world["events"].append(
                events.record(
                    session,
                    entity="task_card",
                    entity_id="T01",
                    kind="REVIEW",
                    actor_agent_id="owner-1",
                    payload={
                        "node": review_node.NODE,
                        "action": review_node.ACTION_VERDICT,
                        "round_no": round_no,
                        "verdict": "CHANGES_REQUESTED",
                        "findings": len(findings),
                        "card_status_after": "CHANGES_REQUESTED",
                    },
                    created_at=stamp,
                ).id
            )
        session.commit()

    body = make_app(app_engine).get("/tasks/T01", params={"feature_request_id": request_id}).json()
    history = body["review_history"]
    assert [item["round_no"] for item in history] == [1, 2], history
    assert [len(item["item_findings"]) for item in history] == [2, 1], "逐条判定配错了轮次"
    assert history[0]["item_findings"][0]["evidence"] == "第一轮缺引用"
    assert history[1]["verdict"] == "CHANGES_REQUESTED"
    assert all(item["verdict_id"] for item in history), "有轮次却没配上结论行 = EN-08 与留痕漂开了"
    assert ids == ["T01"]


# ---------------------------------------------------------------- 六、寻址与借还


def test_a_reused_card_number_without_a_request_is_ambiguous(engines, world):
    """同一个卡号出现在两张需求单下 ⇒ 不带母单号就是 409，并把候选列出来。

    三格一起钉：① 不猜"最新那一张"（那是把另一个项目的产物源码交出去）；
    ② 带了母单号就精确命中（409 不是死路）；③ 卡号谁都没有时是 404 而不是 409 ——
    把"没有"与"不止一个"混成同一个码，调用方就没法知道自己的下一步是换号还是查拼写。
    """
    app_engine, _ = engines
    first, _ = seed_request(world, app_engine, project_key="p15-twin-a", cards=1)
    second, _ = seed_request(world, app_engine, project_key="p15-twin-b", cards=1)
    client = make_app(app_engine)

    ambiguous = client.get("/tasks/T01")
    assert ambiguous.status_code == 409, ambiguous.text
    detail = ambiguous.json()["detail"]
    assert detail.startswith("task_ambiguous")
    assert str(first) in detail and str(second) in detail, "报了歧义却不给候选 = 让调用方自己猜"

    assert client.get("/tasks/T01", params={"feature_request_id": second}).status_code == 200
    missing = client.get("/tasks/T99")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"].startswith("task_not_found")
    scoped_missing = client.get("/tasks/T01", params={"feature_request_id": uuid.uuid4()})
    assert scoped_missing.status_code == 404, "母单号不对时应当说'没有这张卡'而不是'有歧义'"


def test_the_two_new_reads_return_the_connection_they_borrow(engines, world):
    """12 发读请求走在 `pool_size=2 / max_overflow=0` 的池上 ⇒ 每一发都得把连接还回去，且不留未结束事务。

    这条钉的是**新加的两条 GET**（外加它们共用的 `_store`）。
    **两个站点的实测读数不一样，都留着（差额未归因，别把它说成"一处能推另一处"）**：
    把 `api/tasks.py::_store` 的收尾拿掉（M9）时，这一发**红了** —— 整个读档跟着倒（池子 2 条耗尽）；
    而把 `api/requests.py::_persist` 的收尾拿掉（M2）时，同形的那一发**没红**
    （读数与归因尝试见 `test_request_commit_boundary.py::test_the_endpoint_returns_the_connection_it_borrowed`
    的限度段）。所以"摘掉 close 一定能量到"这句不能当成常量：本档能，入口那一档不能。
    形状看守 `test_the_two_new_reads_never_commit_and_the_store_always_closes` 是两边共同的兜底那一格。
    **它也不覆盖 `PATCH` / `worker` / `escalation`** —— 那三支至今仍走 `_session()` 不 `close()`，
    是登记不修的 M2-W13，别把这一发的绿读成整个 `api/tasks.py` 都会还连接。
    """
    app_engine, read_engine = engines
    seed_request(world, app_engine, project_key="p15-pool", cards=1)
    client = make_app(app_engine)

    codes = []
    for _ in range(4):
        codes.append(client.get("/tasks").status_code)
        codes.append(client.get("/tasks/T01").status_code)
        codes.append(client.get("/tasks/reviewing").status_code)
    assert codes == [200] * 12, f"2 条连接撑不住 12 发读请求 ⇒ 有连接没还：{codes}"

    with read_engine.connect() as conn:
        hanging = [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                text(
                    "select application_name, state from pg_stat_activity "
                    "where application_name like :p and state = 'idle in transaction'"
                ),
                {"p": f"{TAG}%"},
            )
        ]
    assert hanging == [], f"读端点返回后仍有未结束的事务挂在池子里：{hanging}"


def test_detail_and_the_reviewing_list_report_the_same_artifact_reading(engines, world, tmp_path):
    """两条读端点对同一份产物必须给**同一个读数**（过大时两边都是同一个原因词）。

    这是一条跨实现符合性判据（纪律："给公共件写第二份实现时，必须立一条两份值相等的对读用例"）：
    `GET /tasks/{id}` 与 `GET /tasks/reviewing` 共用 `review_service.artifact_previews`，
    但两边各自换算一遍就会漂开 —— 那时坏的是"待评审面板能预览、任务详情不能"这种
    两个面板各说一套的形状，而它不会在任何一边的单侧判据里红。
    """
    app_engine, _ = engines
    request_id, _ = seed_request(world, app_engine, project_key="p15-limit", cards=1)
    body_text = "x" * (review_service.MAX_PREVIEW_BYTES + 10)
    bus_root = bus_for(
        tmp_path, "p15-limit", rel_path=f"{EXECUTOR}-workspace/index.html", text_body=body_text
    )

    client = make_app(app_engine, bus_root=bus_root)
    from_detail = client.get("/tasks/T01", params={"feature_request_id": request_id}).json()["artifacts"][0]
    from_list = next(
        item
        for row in client.get("/tasks/reviewing").json()
        for item in row["artifacts"]
        if item["task_id"] == "T01"
    )
    assert from_detail["content"] is None and from_list["content"] is None
    assert from_detail["content_error"] == from_list["content_error"] == "too_large", (
        f"两边对同一份产物的读数分叉了：详情 {from_detail['content_error']} / 列表 {from_list['content_error']}"
    )


# ---------------------------------------------------------------- 七、形状看守（行为档在本机量不到的那一格）

TASKS_SOURCE = Path(__file__).resolve().parents[2] / "src" / "acp" / "api" / "tasks.py"


def _commit_calls(tree: ast.Module, function: str) -> int:
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
    return sum(
        1
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "commit"
    )


def _finally_calls(tree: ast.Module, function: str) -> list[str]:
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
    called: list[str] = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Try) or not item.finalbody:
            continue
        for stmt in item.finalbody:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    called.append(inner.func.attr)
    return called


def test_the_two_new_reads_never_commit_and_the_store_always_closes():
    """两条 GET 里 `commit` 出现 0 次，而共用的 `_store` 出去一定 `close()`。

    两半各挡一种坏，方向相反：
    ① 给 GET 加一次 `commit()` = 把只读请求变成写事务的开头 —— owner 撞过的
      `idle in transaction` 的另一半（口径同 `test_approval_commit_boundary.py` 那条同名看守）；
    ② 把 `_store` 的收尾挪走 = 会话借了不还。
    **②在两处代码上的实测读数不一样，这条看守是为读数弱的那一处兜底的**：
    注入 M9（`tasks.py::_store` 不收尾）时行为档**红了**（读档整片倒，池子 2 条耗尽）；
    注入 M2（`requests.py::_persist` 不收尾）时同形的那一发**没红** —— 那一档只能靠形状看守与
    fast 档的假会话计数挡。两份实测都留着，差额未完全归因；别说成"一处能推另一处"。
    """
    tree = ast.parse(TASKS_SOURCE.read_text(encoding="utf-8"))
    assert _commit_calls(tree, "list_tasks") == 0, "只读端点里出现了 commit()"
    assert _commit_calls(tree, "get_task") == 0, "只读端点里出现了 commit()"
    assert _finally_calls(tree, "_store") == ["close"], (
        f"`_store` 的 finally 里只该有 close() 这一句：{_finally_calls(tree, '_store')}"
    )
