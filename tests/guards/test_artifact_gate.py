"""T075 / T076 / T077 / T083：产物有效性门禁（FR-017 ~ FR-019｜R-02，宪法「产物有效性底线」）。

档位：`tests/guards/`（fast 一档）里的**混合**文件 —— `assess()` 与路由器那批是纯函数，
碰 PG 的那批各挂 `skipif`：没注入 `ACP_DB_DSN` 时它们整条 skip 而不是红。
读数口径与 `test_select_worker.py` 同一条：**每条 PG 读数都必须注明"带 `.env`"**。

本档问的是三件事，分开测才不至于互相掩盖：
1. **判定只看产物**（FR-019）—— 自评既不能救一张不达标的卡，也不该害一张达标的卡；
2. **不达标就不进审查**（FR-018）—— 这是拓扑性质，不是"当前数据恰好没走到"，
   所以它由 `build_execution_graph` 装出来的**真图**跑一遍来证，而不是单测路由函数返回值；
3. **`validated_status` 是被覆写出来的**（FR-019 / T077）—— 回收段留 NULL，门禁段盖章，
   一个 NULL 都不该有第二个人写。

两条最要紧的反向对照（缺了就等于判据恒红或恒绿）：
- T083 的三样本（0 字节 / 行数不足 / 格式不符）之外必须有一条**三项全达标 ⇒ `valid` 且审查被调用**，
  否则"审查永不被调用"可以是靠"谁都进不了审查"实现的 —— 那才是真出事。
- 聚合口径（多份产物怎么算）文档没写，本文件用
  `test_a_companion_file_in_another_format_neither_saves_nor_sinks_the_card` 把**当前取的那一口**钉住；
  将来改口径时只需要翻这一条，其余用例的期望值不变。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import graph as g
from acp.orchestrate.nodes import result_ingest as ri
from acp.orchestrate.nodes import validate_artifact as va
from acp.persistence.enums import values_of
from acp.persistence.models_core import Artifact, AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.protocol.messages import ArtifactRef, TaskResult

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 3, 20, tzinfo=dt.timezone.utc)
EXECUTOR = "qoder"
FLOOR_BYTES = 512
FLOOR_LINES = 12
FLOOR_FORMAT = "html"


def card_need(**overrides):
    """一张最小可用的卡（门禁只看它的三项下限）。"""
    values = {
        "id": "T01",
        "feature_request_id": uuid.uuid4(),
        "title": "卡 T01",
        "difficulty": 2,
        "skill_tags": ["html"],
        "depends_on": [],
        "artifact_floor_min_bytes": FLOOR_BYTES,
        "artifact_floor_min_lines": FLOOR_LINES,
        "artifact_floor_format": FLOOR_FORMAT,
        "acceptance_criteria": ["首屏有标题"],
        "handoff_count": 0,
        "review_round": 0,
        "status": "RUNNING",
        "snapshot_id": uuid.uuid4(),
        "executor_agent_id": EXECUTOR,
    }
    values.update(overrides)
    return TaskCard(**values)


def ref(rel_path: str, *, size: int, lines: int, fmt: str = FLOOR_FORMAT) -> ArtifactRef:
    return ArtifactRef(rel_path=rel_path, bytes=size, lines=lines, format=fmt)


def raises_code(exc_type, code: str, call) -> str:
    """按 `exc.code` 断言拒绝原因，而不是按消息里的中文措辞。

    `code` 是这两个自造异常带在身上的机器可读值（API 层要按它映射 HTTP 码），
    措辞随时可以改、代号改了就是契约变了 —— 所以判据钉后者。
    （`pytest.raises(..., code=…)` 那种写法会被 pytest 当场拒掉：它不认识这个关键字。）
    """
    with pytest.raises(exc_type) as caught:
        call()
    assert caught.value.code == code, f"拒了，但理由代号是 {caught.value.code!r}，不是 {code!r}"
    return str(caught.value)


def result_of(*refs_: ArtifactRef, self_reported: str = "success") -> TaskResult:
    return TaskResult(
        task_id="T01",
        self_reported_status=self_reported,
        artifacts=list(refs_),
        log_excerpt="built 1 file",
        self_assessment="已完成",
        issues=[],
        usage=[],
    )


# —————————————————————————————— 一、判定只看产物（FR-019）


def test_an_artifact_meeting_all_three_floors_is_valid():
    verdict = va.assess(card_need(), [_row(ref("index.html", size=600, lines=30))])
    assert verdict.validated_status == va.VALID, verdict.invalid_reason
    assert verdict.invalid_reason is None, "valid 却带理由 ⇒ 结论与理由的形状不互斥"


@pytest.mark.parametrize(
    ("refs_", "names"),
    [
        ((ref("index.html", size=0, lines=30),), "体积"),
        ((ref("index.html", size=600, lines=5),), "行数"),
        ((ref("notes.md", size=600, lines=30, fmt="md"),), "格式"),
        ((), f"{FLOOR_BYTES}"),
    ],
    ids=["zero-bytes", "few-lines", "wrong-format", "no-artifact"],
)
def test_each_t083_sample_is_invalid_and_names_the_floor_that_failed(refs_, names):
    """T083 的三样本（再加"空产物"这条 FR-019 原文形状）都要 `invalid`，而且**理由点名是哪一项**。

    前三条的匹配词是三项下限**自己的名字**（"体积 / 行数 / 格式"），不是理由里的散文：
    第四条目原用"没有任何产物"这五个字，结果一轮"只改措辞"的变异对照把它读成了红
    （MU-V7，见 `evidence/phase-06.md`）⇒ 判据改成"理由里必须带上卡上的下限数值"，
    那是结构事实，改写句子不该让它翻脸。
    """
    rows = [_row(r) for r in refs_]
    verdict = va.assess(card_need(), rows)
    assert verdict.validated_status == va.INVALID, verdict
    assert verdict.invalid_reason is not None and names in verdict.invalid_reason, verdict.invalid_reason


def test_a_self_report_of_success_does_not_save_an_underweight_artifact():
    """FR-019 的正身：自评 success + 不达标 ⇒ 判 invalid。自评这一路**进不了判定**。"""
    verdict = va.assess(card_need(), [_row(ref("index.html", size=10, lines=2), reported="success")])
    assert verdict.validated_status == va.INVALID


def test_a_self_report_of_failure_does_not_sink_a_meeting_artifact():
    """反方向也钉住：自评 failed + 产物达标 ⇒ 仍然 `valid`。

    只测"success 救不了"的话，把自评当**减分项**的实现体也能全绿，而 FR-019 要的是"不看自评"，
    不是"只看负面自评"。
    """
    verdict = va.assess(card_need(), [_row(ref("index.html", size=600, lines=30), reported="failed")])
    assert verdict.validated_status == va.VALID, verdict.invalid_reason


def test_a_companion_file_in_another_format_neither_saves_nor_sinks_the_card():
    """**口径分叉点**（文档未定义多产物怎么聚合，此处择一）。

    取的是"在同一格式里至少有一份同时过体积与行数"：同卡顺手落的 `styles.css`（6 行 / 30B）
    不该把一张已达标 html 判死，也不该由它去凑 html 的下限。
    另两种读法各自会怎么变：①"每一份都必须达标" ⇒ 本条期望值翻成 invalid；②"按求和算下限" ⇒
    css 的字节数会算进 html 的体积下限。改口径时**只需要动这一条**，其余用例的期望值不变 ——
    这就是把它单独钉住的原因。
    """
    card = card_need()
    rows = [
        _row(ref("index.html", size=600, lines=30)),
        _row(ref("styles.css", size=30, lines=6, fmt="css")),
    ]
    assert va.assess(card, rows).validated_status == va.VALID

    only_companion = [_row(ref("styles.css", size=9999, lines=999, fmt="css"))]
    assert va.assess(card, only_companion).validated_status == va.INVALID, "把 css 算进 html 的下限就是求和读法"


def test_a_js_companion_is_a_first_class_format_not_a_stray_file():
    """`js` 进域之后（owner 2026-10-29），同卡落的 `cart.js` 与 `styles.css` 同档。

    这条钉两件事，缺一件就等于"新格式被当成了域外产物"：

    1. **不害**：html 卡顺手交一份 `cart.js`，不把已达标的卡判死；
    2. **不救**：只有 `cart.js`（哪怕 9999 字节 / 999 行）也不算这张 html 卡达标 ——
       口径没被"多一个格式"顺手放宽，`artifact_floor_format` 仍是单值、仍是那张卡的真相。

    2026-10-28 的现场正是这条的反面：`cart.js` 当时不在域里，`gateway.format_of()` 直接抛
    `unsupported_format`，**那次写入整件**被拒 —— 卡不是"没达标"，是"连产物都没落成"。
    """
    card = card_need()
    rows = [
        _row(ref("index.html", size=600, lines=30)),
        _row(ref("cart.js", size=400, lines=10, fmt="js")),
    ]
    assert va.assess(card, rows).validated_status == va.VALID

    only_script = [_row(ref("cart.js", size=9999, lines=999, fmt="js"))]
    assert va.assess(card, only_script).validated_status == va.INVALID, (
        "js 产物替 html 卡凑下限就是放宽了口径"
    )


def test_the_two_halves_of_the_floor_on_two_files_do_not_make_a_deliverable():
    """同一格式拆成两份"各有一半"的文件 ⇒ `invalid`（求和读法在这里会给出相反的结论）。"""
    rows = [
        _row(ref("part-a.html", size=600, lines=6)),
        _row(ref("part-b.html", size=200, lines=8)),
    ]
    verdict = va.assess(card_need(), rows)
    assert verdict.validated_status == va.INVALID, verdict


def test_an_invalid_verdict_always_carries_a_readable_reason():
    verdict = va.assess(card_need(), [_row(ref("index.html", size=0, lines=0))])
    assert verdict.validated_status == va.INVALID
    assert verdict.invalid_reason, "invalid 却给不出理由 ⇒ 换人时 `status.md` 里只能写'不知道为什么不达标'"
    assert f"{FLOOR_BYTES}" in verdict.invalid_reason and f"{FLOOR_LINES}" in verdict.invalid_reason, (
        "理由必须带上卡上的下限，否则读数的人无法复核： " + str(verdict.invalid_reason)
    )


# —————————————————————————————— 二、出边只有两条（T076 / FR-018）


def test_the_exits_are_exactly_the_two_values_of_the_verdict_domain():
    """"出边仅两条"写成关系式：`OUT_EDGES` 的键集 MUST 等于 PG 域 `validated_status` 的取值全集。

    不写 `len(OUT_EDGES) == 2` 那种常量断言 —— 域里若将来加第三个值（比如 `unknown`），
    常量断言会红得没有信息量，而这条会直接点名"有一个结论没有出边"。
    """
    assert set(va.OUT_EDGES) == set(values_of("validated_status")), (
        f"结论域 {sorted(values_of('validated_status'))} 与出边表 {sorted(va.OUT_EDGES)} 不齐"
    )
    assert sorted(va.OUT_EDGES.values()) == ["handoff", "review"], va.OUT_EDGES


@pytest.mark.parametrize(
    ("state", "expected"),
    [({"validated_status": "valid"}, "review"), ({"validated_status": "invalid"}, "handoff")],
)
def test_the_router_maps_each_verdict_to_its_one_exit(state, expected):
    assert va.route_after_validation(state) == expected


@pytest.mark.parametrize("state", [{}, {"validated_status": None}, {"validated_status": "success"}])
def test_the_router_refuses_to_guess_when_the_verdict_is_unreadable(state):
    """读不到结论就抛，不"默认放行到审查"。

    兜底成 `review` 的表现正是 FR-018 禁的那件事：门禁没跑（或跑了但没写回）也进了审查。
    """
    raises_code(va.ValidationMisuse, "route_source_missing", lambda: va.route_after_validation(state))


# —————————————————————————————— 三、真 PG：回收留 NULL、门禁覆写、图上拓扑


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


@pytest.fixture
def seeded(session) -> tuple[FeatureRequest, TaskCard]:
    """一支在册工具 + 一条需求单 + 一张 `RUNNING` 且已填执行者的卡（门禁的三项下限就在它身上）。"""
    session.add(
        AgentUnit(
            agent_id=EXECUTOR,
            form="SDK",
            tier=2,
            cost_band="MID",
            max_parallel=2,
            current_load=1,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{EXECUTOR}-workspace",
            enabled=True,
        )
    )
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(request)
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T01"],
            ref=f"local:probe-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    # 这道 flush 不是顺手：两张表之间没有 `relationship()`，SQLAlchemy 的 UoW **不按表间外键排 INSERT**，
    # 一次 flush 里 `task_card` 会排在 `snapshot` 前面 ⇒ PG 当场回 FK 违反。
    # 同一个坑在 `orchestrate/graph.py::_normalize` 里踩过一次，判据是 `evidence/phase-04.md`。
    session.flush()
    card = card_need(feature_request_id=request.id, snapshot_id=snapshot_id)
    session.add(card)
    session.flush()
    return request, card


def rows_of(session, feature_request_id, task_id="T01") -> list[Artifact]:
    return list(
        session.scalars(
            select(Artifact).where(
                Artifact.feature_request_id == feature_request_id, Artifact.task_card_id == task_id
            )
        ).all()
    )


@needs_pg
def test_result_ingest_records_the_measured_values_and_leaves_the_verdict_null(session, seeded):
    """FR-019 的前件：回收段只搬实测值，`validated_status` 留 NULL（覆写权归门禁）。"""
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(ref("index.html", size=600, lines=30), self_reported="success"),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    assert ingested.count == 1
    stored = rows_of(session, request.id)
    assert [row.id for row in stored] == list(ingested.artifact_ids)
    row = stored[0]
    assert (row.bytes, row.lines, row.format, row.rel_path) == (600, 30, FLOOR_FORMAT, "index.html")
    assert row.self_reported_status == "success"
    assert row.validated_status is None, "回收段就盖章 ⇒ 门禁形同虚设"
    assert row.invalid_reason is None


@needs_pg
def test_result_ingest_accepts_a_js_artifact(session, seeded):
    """`format='js'` 走**生产回收函数**能落成 EN-06 行。

    2026-10-28 的现场不是"卡没达标"，是"连产物都没落成"：`cart.js` 在
    `gateway.format_of()` 就被判 `unsupported_format` 抛了 `BusError`，`record_result` 一次都没被调到。
    所以这条要钉的正是那一步之后的事 —— 域里有 `js` 时，回收段照常写行、`validated_status` 照常留 NULL。
    """
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(ref("cart.js", size=400, lines=10, fmt="js")),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    assert ingested.count == 1
    row = rows_of(session, request.id)[0]
    assert (row.rel_path, row.format) == ("cart.js", "js")
    assert row.validated_status is None, "回收段不盖章（覆写权归门禁），与格式是哪一个无关"
    assert "js" in values_of("artifact_format")


@needs_pg
def test_result_ingest_writes_the_executor_as_writer_not_the_caller_claim(session, seeded):
    """EN-06 `writer_agent_id` "等值于该任务 executor"（FR-033）⇒ 记账人不是调用方随口报的那一个。"""
    request, card = seeded
    raises_code(
        ri.IngestMisuse,
        "writer_not_executor",
        lambda: ri.record_result(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            result=result_of(ref("index.html", size=600, lines=30)),
            actor_agent_id="doubao",
            created_at=NOW,
        ),
    )
    assert rows_of(session, request.id) == [], "拒收之前就该一行都不写：写了再抛，库里就留下一条没人认领的产物"


@needs_pg
def test_result_ingest_refuses_a_card_that_is_not_running(session, seeded):
    request, card = seeded
    card.status = "READY"
    session.flush()
    raises_code(
        ri.IngestMisuse,
        "task_not_running",
        lambda: ri.record_result(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            result=result_of(ref("index.html", size=600, lines=30)),
            actor_agent_id=EXECUTOR,
            created_at=NOW,
        ),
    )


@needs_pg
def test_result_ingest_refuses_a_result_meant_for_another_card(session, seeded):
    """结果里的 `task_id` 与要挂的卡不一致 ⇒ 拒。靠调用方参数撮合两样东西是最省事的路。"""
    request, card = seeded
    mismatched = result_of(ref("index.html", size=600, lines=30)).model_copy(update={"task_id": "T99"})
    with pytest.raises(ri.IngestMisuse):
        ri.record_result(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            result=mismatched,
            actor_agent_id=EXECUTOR,
            created_at=NOW,
        )


@needs_pg
@pytest.mark.parametrize(
    ("refs_", "expected_status"),
    [
        ((ref("index.html", size=0, lines=30),), va.INVALID),
        ((ref("index.html", size=600, lines=5),), va.INVALID),
        ((ref("page.md", size=600, lines=30, fmt="md"),), va.INVALID),
        ((), va.INVALID),
        ((ref("index.html", size=600, lines=30),), va.VALID),
    ],
    ids=["zero-bytes", "few-lines", "wrong-format", "no-artifact", "meets-all-three"],
)
def test_the_gate_overwrites_every_row_of_this_batch(session, seeded, refs_, expected_status):
    """T077：`validated_status` 由门禁覆写，且**这一批每一行都被写到**（不许只写第一行）。"""
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(*refs_),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
    )
    assert verdict.validated_status == expected_status
    stored = rows_of(session, request.id)
    if stored:
        assert {row.validated_status for row in stored} == {expected_status}, [
            (row.rel_path, row.validated_status) for row in stored
        ]
        assert all((row.invalid_reason is None) == (expected_status == va.VALID) for row in stored)
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.status == (va.INVALID_STATE if expected_status == va.INVALID else "RUNNING"), fresh.status


@needs_pg
def test_the_verdict_is_stamped_on_every_row_of_the_batch(session, seeded):
    """一批三行 ⇒ 三行都被覆写。只盖第一行的话，另外两行的实测值永远没有判定，
    而 `validated_status` 可空正是"还没人盖章"的形状，看不出是被漏掉的。

    两条方向都查：`valid` 那批里连"不达标的第二行"也要盖上 `valid`（判定是**卡级**的，
    行级 stamped 值记录的是这次门禁的结论，不是这一行自己过没过），
    `invalid` 那批则三行都带同一句理由。
    """
    request, card = seeded
    mixed = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(
            ref("index.html", size=600, lines=30),
            ref("part.html", size=10, lines=2),
            ref("styles.css", size=30, lines=6, fmt="css"),
        ),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    assert mixed.count == 3
    valid_verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=mixed.artifact_ids,
        created_at=NOW,
    )
    assert valid_verdict.validated_status == va.VALID
    stamped = {row.id: (row.validated_status, row.invalid_reason) for row in rows_of(session, request.id)}
    assert len(stamped) == 3, stamped
    assert {status for _id, (status, _r) in stamped.items()} == {va.VALID}, stamped

    failing = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(
            ref("a.html", size=1, lines=1),
            ref("b.html", size=2, lines=2),
            ref("c.md", size=9999, lines=999, fmt="md"),
        ),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    invalid_verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=failing.artifact_ids,
        created_at=NOW,
    )
    assert invalid_verdict.validated_status == va.INVALID
    second_batch = session.scalars(
        select(Artifact).where(Artifact.id.in_(list(failing.artifact_ids)))
    ).all()
    assert len(second_batch) == 3
    assert {row.validated_status for row in second_batch} == {va.INVALID}, [
        (row.rel_path, row.validated_status) for row in second_batch
    ]
    assert len({row.invalid_reason for row in second_batch}) == 1, "同一批给出两句不同理由"


@needs_pg
def test_the_gate_records_exactly_one_audit_row_naming_what_it_saw(session, seeded):
    """留痕（FR-051 四要素）：一条 `EN-14(kind=ARTIFACT)`，payload 带结论、理由、自评与三项下限。"""
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(ref("index.html", size=10, lines=2), self_reported="success"),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
    )
    audit = list(
        session.scalars(
            select(EventLog).where(
                EventLog.entity == "task_card", EventLog.entity_id == card.id, EventLog.kind == "ARTIFACT"
            )
        ).all()
    )
    assert len(audit) == 1, [row.payload for row in audit]
    payload = audit[0].payload
    assert payload["validated_status"] == va.INVALID
    assert payload["self_reported_status"] == "success", "留痕要把'工具当时说了什么'一起留下"
    assert payload["floors"] == {"min_bytes": FLOOR_BYTES, "min_lines": FLOOR_LINES, "format": FLOOR_FORMAT}
    assert payload["card_status_after"] == va.INVALID_STATE
    assert audit[0].actor_agent_id == va.AUTOMATED_ACTOR, audit[0].actor_agent_id


@needs_pg
def test_the_gate_refuses_to_judge_the_same_batch_twice(session, seeded):
    """同一批实测值过两次门禁 ⇒ 拒。第二次结论没人负责，而库里已经有一行被改过两遍。"""
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(ref("index.html", size=600, lines=30)),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
    )
    with pytest.raises(va.ValidationMisuse):
        va.validate_card(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            artifact_ids=ingested.artifact_ids,
            created_at=NOW,
        )


@needs_pg
def test_the_gate_refuses_rows_that_belong_to_another_card(session, seeded):
    """换人重试会在同一张卡上再攒一批新行 ⇒ 门禁只认调用方给的**这一批** id，不给"读全卡"的默认路径。"""
    request, card = seeded
    other_snapshot = uuid.uuid4()
    session.add(
        Snapshot(
            id=other_snapshot,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T02"],
            ref=f"local:probe-{other_snapshot}",
            rolled_back_at=None,
        )
    )
    session.flush()
    session.add(card_need(id="T02", feature_request_id=request.id, snapshot_id=other_snapshot))
    sibling = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id="T02",
        result=TaskResult(
            task_id="T02",
            self_reported_status="success",
            artifacts=[ref("index.html", size=600, lines=30)],
            log_excerpt="x",
            self_assessment="y",
        ),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    with pytest.raises(va.ValidationMisuse, match="不属于"):
        va.validate_card(
            session,
            feature_request_id=request.id,
            task_id="T01",
            artifact_ids=sibling.artifact_ids,
            created_at=NOW,
        )


@needs_pg
def test_an_empty_return_is_judged_invalid_instead_of_rejected_as_misuse(session, seeded):
    """FR-019 的正文形状：工具报 success 但**一份产物都没交回来**。

    这张卡不是"调用方漏了参数"，所以门禁不许把它当误用抛掉 —— 抛掉的表现是空交付的任务
    既不过审查也不换人，图上直接炸，而 spec 要的恰恰是它走 `HandOff`。
    `artifact_ids` 是必填关键字参数，"省略参数"这条路在 Python 层面就不存在（TypeError），
    因此这里不再需要一条"空集即误用"的守卫。
    """
    request, card = seeded
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    assert ingested.artifact_ids == ()
    verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
    )
    assert verdict.validated_status == va.INVALID
    assert verdict.examined == 0
    assert session.get(TaskCard, (request.id, card.id)).status == va.INVALID_STATE


# —————————————————————————————— 四、图上的拓扑（T083：审查节点永不被调用）


class Counter:
    """两个下游的计数桩：`review` 一旦被调用，T083 的 `reviewer_invoked == 0` 就成立不了。"""

    def __init__(self) -> None:
        self.reviewed: list[str] = []
        self.handed_off: list[str] = []

    def review(self, state):
        self.reviewed.append(state["task_id"])
        return {"validated_status": state.get("validated_status")}

    def handoff(self, state):
        self.handed_off.append(state["task_id"])
        return {"validated_status": state.get("validated_status")}


def run_card(session, request_id, counter: Counter, result: TaskResult) -> dict:
    graph = g.build_execution_graph(
        g.ExecutionDeps(session=session, created_at=NOW, actor_agent_id=EXECUTOR),
        review=counter.review,
        handoff=counter.handoff,
    )
    return graph.invoke(
        {"feature_request_id": request_id, "task_id": "T01", "result": result},
    )


@needs_pg
@pytest.mark.parametrize(
    ("refs_", "why"),
    [
        ((ref("index.html", size=0, lines=30),), "0 字节"),
        ((ref("index.html", size=600, lines=5),), "行数不足"),
        ((ref("page.md", size=600, lines=30, fmt="md"),), "格式不符"),
    ],
    ids=["zero-bytes", "few-lines", "wrong-format"],
)
def test_an_unweighted_artifact_routes_to_handoff_and_the_review_is_never_called(session, seeded, refs_, why):
    """T083 的正文：三样本一律 `ARTIFACT_INVALID` + `reviewer_invoked == 0` + handOff 计数 +1。"""
    request, card = seeded
    counter = Counter()
    state = run_card(session, request.id, counter, result_of(*refs_))
    assert state["validated_status"] == va.INVALID, f"{why} 应当判 invalid：{state}"
    assert counter.reviewed == [], f"{why} 却进了审查 ⇒ FR-018 破防"
    assert counter.handed_off == ["T01"], f"{why} 却没换人：{counter.handed_off}"
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.status == va.INVALID_STATE, fresh.status
    assert rows_of(session, request.id) and all(row.validated_status == va.INVALID for row in rows_of(session, request.id))


@needs_pg
def test_a_meeting_artifact_reaches_review_and_never_the_handoff(session, seeded):
    """上一条的反向对照（缺了它，上面三条可以是"谁都进不了审查"那种假绿）。"""
    request, _card = seeded
    counter = Counter()
    state = run_card(session, request.id, counter, result_of(ref("index.html", size=600, lines=30)))
    assert state["validated_status"] == va.VALID
    assert counter.reviewed == ["T01"], counter.reviewed
    assert counter.handed_off == [], counter.handed_off


@needs_pg
def test_the_execution_segment_writes_exactly_the_documented_four_nodes_and_two_exits(session, seeded):
    """T076 的"出边仅两条"是拓扑判据：从图对象上读，而不是从我写的字面串上读。"""
    counter = Counter()
    graph = g.build_execution_graph(
        g.ExecutionDeps(session=session, created_at=NOW, actor_agent_id=EXECUTOR),
        review=counter.review,
        handoff=counter.handoff,
    )
    drawable = graph.get_graph()
    outgoing = sorted(
        (edge.source, edge.target) for edge in drawable.edges if edge.source == g.VALIDATE_ARTIFACT
    )
    assert outgoing == [(g.VALIDATE_ARTIFACT, g.HANDOFF), (g.VALIDATE_ARTIFACT, g.REVIEW)], outgoing
    nodes = {name for name in drawable.nodes}
    assert nodes == {g.RESULT_INGEST, g.VALIDATE_ARTIFACT, g.REVIEW, g.HANDOFF, "__start__", "__end__"}, nodes


@needs_pg
def test_the_graph_needs_both_downstreams_and_does_not_invent_a_default(session, seeded):
    """缺任一下游就装配期报错，而不是"图上少一条边"——后者会让不达标的卡有一条通向终点的路。"""
    with pytest.raises(g.MissingDownstream):
        g.build_execution_graph(
            g.ExecutionDeps(session=session, created_at=NOW, actor_agent_id=EXECUTOR),
            review=None,
            handoff=Counter().handoff,
        )


@needs_pg
def test_the_verdict_in_the_state_is_the_verdict_in_the_database(session, seeded):
    """一个结论一个载体：路由读的那份 MUST 等于库里那一份（否则图与库各说一套）。"""
    request, _card = seeded
    counter = Counter()
    state = run_card(session, request.id, counter, result_of(ref("index.html", size=10, lines=1)))
    stored = {row.validated_status for row in rows_of(session, request.id)}
    assert stored == {state["validated_status"]} == {va.INVALID}, (stored, state)


def _row(ref_: ArtifactRef, *, reported: str = "success") -> Artifact:
    """**未入库**的 EN-06 行：给纯函数 `assess()` 当输入用（构造时不 add、不 flush）。

    故意用生产形态的 mapper 而不是自造一个假对象：`assess()` 读的列名要是改了，
    用 SimpleNamespace 的用例会跟着一起错下去，用真 mapper 的会红。
    """
    return Artifact(
        id=uuid.uuid4(),
        feature_request_id=uuid.uuid4(),
        task_card_id="T01",
        writer_agent_id=EXECUTOR,
        rel_path=ref_.rel_path,
        bytes=ref_.bytes,
        lines=ref_.lines,
        format=ref_.format,
        self_reported_status=reported,
        validated_status=None,
        invalid_reason=None,
        created_at=NOW,
    )


# —————————————————————————————— 五、内容有效性（P1-16）


def _write_html(root, rel_path: str, body: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_content_check_accepts_a_complete_page(tmp_path):
    _write_html(tmp_path, "qoder-workspace/index.html", "<html><body><p>你好</p></body></html>")
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    assert va._content_rejection(row, tmp_path) is None


def test_content_check_rejects_missing_closing_tag(tmp_path):
    _write_html(tmp_path, "qoder-workspace/index.html", "<html><body><p>半个页面</p>")
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "</html>" in reason


def test_content_check_rejects_a_lorem_ipsum_page(tmp_path):
    lines = ["<html><body>"] + ["<p>Lorem ipsum dolor sit amet.</p>"] * 12 + ["</body></html>"]
    _write_html(tmp_path, "qoder-workspace/index.html", "\n".join(lines))
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "lorem ipsum" in reason


def test_content_check_reports_a_missing_file(tmp_path):
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "读不到" in reason


def test_content_check_does_not_apply_to_non_html(tmp_path):
    """自由文本文档不判截断：一篇说明在任何一句后面停下来都是合法的（文件在盘上）。"""
    _write_html(tmp_path, "qoder-workspace/notes.md", "# 说明\n\n写到一半也没关系")
    row = _row(ref("qoder-workspace/notes.md", size=600, lines=30, fmt="md"))
    assert va._content_rejection(row, tmp_path) is None


# —— 五之二、`overflow-x:hidden` 必须配一个宽度约束（2026-10-30 三发真模型直跑带出来的）
#
# 三发的统筹模型都把需求里的"375px 下不横向滚动"写成「无横向滚动条」，而 `overflow-x:hidden`
# 恰好满足那句话 —— 滚动条没了，内容是被裁掉还是适配了没人判。这条判据钉的是"藏了就得有约束"。


def _page(css: str, *, link: str = "") -> str:
    return f"<html><head>{link}<style>{css}</style></head><body>正文</body></html>"


def test_content_check_rejects_a_bare_overflow_hide(tmp_path):
    _write_html(tmp_path, "qoder-workspace/index.html", _page("html,body{overflow-x:hidden}"))
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "overflow-x:hidden" in reason and "宽度约束" in reason


def test_content_check_accepts_hide_paired_with_a_full_width_rule(tmp_path):
    _write_html(tmp_path, "qoder-workspace/index.html", _page("html,body{overflow-x:hidden;width:100%}"))
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    assert va._content_rejection(row, tmp_path) is None


def test_content_check_reads_the_linked_stylesheet(tmp_path):
    """约束在 `style.css` 里也算数 —— 只看 HTML 本体会把三份真实产物全误杀。"""
    _write_html(tmp_path, "qoder-workspace/style.css", ".wrap{max-width:1120px;box-sizing:border-box}")
    _write_html(
        tmp_path,
        "qoder-workspace/index.html",
        _page("html,body{overflow-x:hidden}", link='<link rel="stylesheet" href="style.css">'),
    )
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    assert va._content_rejection(row, tmp_path) is None


def test_content_check_ignores_a_remote_stylesheet_and_still_rejects(tmp_path):
    """外链与越界路径**不算**证据：读不到就没有约束，判它违规而不是放过。"""
    _write_html(
        tmp_path,
        "qoder-workspace/index.html",
        _page("html,body{overflow-x:hidden}", link='<link rel="stylesheet" href="https://cdn.example/x.css">'),
    )
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "overflow-x:hidden" in reason


def test_content_check_does_not_fire_without_the_hide(tmp_path):
    """反向对照：没有 `overflow-x:hidden` 的页面不许被这条判据牵连（三发的正常产物都从这里过）。"""
    _write_html(tmp_path, "qoder-workspace/index.html", _page(".card{overflow:auto}"))
    row = _row(ref("qoder-workspace/index.html", size=600, lines=30))
    assert va._content_rejection(row, tmp_path) is None


@needs_pg
def test_the_gate_turns_a_placeholder_page_into_artifact_invalid(session, seeded, tmp_path):
    """P1-16 的端到端半边：数值过线 + `bus_root` 给了，内容不过 ⇒ 卡落 `ARTIFACT_INVALID`。"""
    request, card = seeded
    rel_path = "qoder-workspace/index.html"
    body = "<html><body>\n" + "\n".join(["<p>Lorem ipsum dolor sit amet.</p>"] * 12) + "\n</body></html>\n"
    _write_html(tmp_path, rel_path, body)
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(ref(rel_path, size=600, lines=30)),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
        bus_root=tmp_path,
    )
    assert verdict.validated_status == va.INVALID
    assert verdict.invalid_reason and "内容有效性" in verdict.invalid_reason
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.status == va.INVALID_STATE


# —— 六、截断零容忍（2026-10-31 四发真模型矩阵：`done` 的四发里三发产出语法破文件）
#
# 现场形状原样搬进来：`app.js` 末行停在 `if (!`、`style.css` 末行停在 `.hero-image {`。
# 判据必须是纯字符串的 —— 运行段镜像里没有 node，"括号配对"是能复算的那一档。


def test_completeness_rejects_the_two_real_truncated_shapes():
    js = "const cart = [];\nfunction renderCart() {\n  const panel = getCartPanel();\n  if (!"
    css = ".hero-title {\n  font-size: 2rem;\n}\n.hero-image {"
    assert va.artifact_completeness("js", js), "截断的 js 必须被拒"
    assert va.artifact_completeness("css", css), "截断的 css 必须被拒"


def test_completeness_accepts_braces_inside_strings_and_comments():
    """反向对照（防误杀）：字符串与注释里的花括号不参与配对 —— 真实 CSS 里就有 `content:"{"`。"""
    css = '/* .x { color: red } */\n.card::after { content: "{" ; }\n.btn { color: red; }\n'
    assert va.artifact_completeness("css", css) is None
    js = "const tpl = `{ ${name} }`; // { 注释\nfunction f() { return tpl; }\n"
    assert va.artifact_completeness("js", js) is None


@pytest.mark.parametrize(
    ("fmt", "text", "word"),
    [
        ("html", "<html><body>x", "</html>"),
        ("py", "def f(:\n  pass\n", "Python 语法错误"),
        ("json", '{"a": 1,', "JSON 解析失败"),
        ("md", "# 写到一半也没关系", None),
    ],
    ids=["html", "py", "json", "md-free"],
)
def test_completeness_per_format(fmt, text, word):
    reason = va.artifact_completeness(fmt, text)
    if word is None:
        assert reason is None, f"自由文本文档不该判截断，却得到 {reason!r}"
    else:
        assert reason and word in reason, reason


def test_content_check_catches_a_truncated_stylesheet(tmp_path):
    """旧口径只读 HTML ⇒ 一份 css companion 写到一半也一路绿；现在它自己就会被拒。"""
    _write_html(tmp_path, "qoder-workspace/style.css", ".a { color: red; }\n.b {")
    row = _row(ref("qoder-workspace/style.css", size=600, lines=30, fmt="css"))
    reason = va._content_rejection(row, tmp_path)
    assert reason and "不完整" in reason


@needs_pg
def test_a_truncated_companion_file_sinks_the_card(session, seeded, tmp_path):
    """端到端半边：主产物（html）三项全达标、内容也干净，但它旁边那份 css 是半截的 ⇒ 整张卡 invalid。

    这一格在旧口径下**必然绿**：质量那一半只看"同格式的达标产物全坏"，而这份 css 根本不参与；
    截断那一档改成看全部产物，正是为了这个现场。
    """
    request, card = seeded
    _write_html(tmp_path, "qoder-workspace/index.html", "<html><body>完整页面</body></html>")
    _write_html(tmp_path, "qoder-workspace/style.css", ".a { color: red; }\n.b {")
    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        result=result_of(
            ref("qoder-workspace/index.html", size=600, lines=30),
            ref("qoder-workspace/style.css", size=600, lines=30, fmt="css"),
        ),
        actor_agent_id=EXECUTOR,
        created_at=NOW,
    )
    verdict = va.validate_card(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        artifact_ids=ingested.artifact_ids,
        created_at=NOW,
        bus_root=tmp_path,
    )
    assert verdict.validated_status == va.INVALID
    assert verdict.invalid_reason and "产物不完整" in verdict.invalid_reason
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.status == va.INVALID_STATE
