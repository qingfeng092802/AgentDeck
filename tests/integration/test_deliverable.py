"""T099（+ T094 / T095 / T096 的判据半边）：唯一交付包 —— 行数恰为 1、入口真存在、每条可反查。

档位：`tests/integration/`（沿 `plan.md` 的目录分档，**不进 `-m fast` 先跑集**）。
本文件混合两种副作用：PG 行（唯一索引、manifest 落库）与临时总线目录（入口存在性、公共区代笔）。
两边都用 `tmp_path`，**不碰工作树里的 `AI项目台/`**。

owner 给 T099 的四条逐字对着写：
① `deliverable` 行数恰为 1；
② 再插必失败；
③ `entry_path` 指向文件真实存在；
④ `manifest` 每条 `rel_path` 可反查产出者。

**②有两道，两道各自量一次**：应用层 `PackageAlreadyAssembled`（给得出理由）与库面那条
`UNIQUE(feature_request_id)`（应用层被改坏时的最后一道）。只测前者就等于"唯一性写在代码里"，
删掉那段判据就静默放行第二包 —— 所以 ③ 那支绕过 `assemble()` 直接插 ORM 行。

**反向对照在本文件承担两件事**（owner 的纪律：不许拿"谁都拒"当唯一性）：
- 唯一键是**需求单级**而不是全局级 ⇒ 第二张需求单必须照样能组包（`..._per_request_not_globally`）；
- 只读岗不能写公共区，但编排引擎必须能 ⇒ 两支都在，"单点代笔"才既不是"没人能写"也不是"谁都能写"。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.bus import acl, layout
from acp.bus.gateway import BusGateway
from acp.bus.orchestrator_writer import OrchestratorWriter
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import assemble as asm
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Deliverable, MeetingNote, Snapshot
from acp.registry import roles as role_registry

FLEET = ("qoder", "doubao", "workbuddy")
NOW = dt.datetime(2026, 10, 14, 10, 30, tzinfo=dt.timezone.utc)
ENTRY = "qoder-workspace/index.html"
SECOND_ARTIFACT = "doubao-workspace/app.css"

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)


# ---------------------------------------------------------------- 临时总线


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    """一份最小总线：项目子目录 + 三支 workspace + 两条真产物文件。

    两条产物都落盘（`ENTRY` 与 `SECOND_ARTIFACT`），因为"入口在 manifest 里"与"入口在盘上"
    是两个独立的条件 —— 第二包那支要用**另一个**合法入口去撞唯一索引，换一个不存在的入口
    就先被 `entry_missing` 拦掉，测不到要测的那一道。
    """
    project = tmp_path / layout.BUS_ROOT_NAME / "demo"
    for agent_id in FLEET:
        (project / f"{agent_id}{layout.WORKSPACE_SUFFIX}").mkdir(parents=True)
    # P0-4：`_seed_request` 的 `Artifact.bytes` 是 `len(rel_path) * 32`。这里把文件写成同样的
    # 字节数，否则组包前的 `stat()` 重算会（正确地）判 `artifact_stale`，这些用例就测不到
    # 它们本来要测的唯一性/入口/门禁语义了。
    for rel_path, head in ((ENTRY, "<html><h1>公司介绍</h1></html>\n"), (SECOND_ARTIFACT, "body{margin:0}\n")):
        size = len(rel_path) * 32
        body = (head.encode("utf-8") + b" " * size)[:size]
        assert len(body) == size
        (project / rel_path).write_bytes(body)
    return project


# ---------------------------------------------------------------- PG 装配


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """一支事务 = 一个用例，teardown 一律回滚。

    **三支工具与前三岗在同一个事务里种**：`Artifact.writer_agent_id` 有指向 EN-03 的外键，
    而用例自己开的事务看不见别的事务里的行 —— 把 fleet 种在 session 级夹具里，
    用例读到的就永远是"fleet 为空"（第一版就是这么错的：外键当场炸）。
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        for agent_id in FLEET:
            s.add(
                AgentUnit(
                    agent_id=agent_id,
                    form="SDK",
                    tier=2,
                    cost_band="MID",
                    max_parallel=2,
                    current_load=0,
                    historical_success_rate=Decimal("0.5"),
                    success_rate_sample=4,
                    workspace_dir=f"/tmp/{agent_id}-workspace",
                    enabled=True,
                )
            )
        role_registry.seed_builtin_roles(s)
        s.flush()
        yield s
        s.close()
        transaction.rollback()


def _seed_request(
    session: Session,
    *,
    status: str = "DELIVERED",
    artifacts: tuple[tuple[str, str, str | None], ...] = ((ENTRY, "qoder", "valid"),),
) -> uuid.UUID:
    """一张需求单 + 一张处于 `status` 的卡 + 若干产物行（`validated_status` 由调用方给）。

    `artifacts` 的第三项刻意允许 `None` / `"invalid"`：FR-019 的门禁判定发生在 `ValidateArtifact`，
    覆写前那列就是空的，而"没判过的产物能不能进最终交付"正是 T094 要量的一件事。
    """
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="RUNNING",
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
    session.flush()
    card = TaskCard(
        id="T01",
        feature_request_id=request.id,
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有公司名"],
        handoff_count=0,
        review_round=1,
        status=status,
        snapshot_id=snapshot_id,
        executor_agent_id="qoder",
        reviewer_agent_id="doubao",
        acceptor_agent_id="workbuddy",
    )
    session.add(card)
    # 卡先落，产物行后落：两表之间没有 `relationship()`，同一次 flush 里工作单元按 mapper 名排序
    # （`artifact` 会排在 `task_card` 前）⇒ 复合外键撞响。同 `test_review_accept.py` 的装配坑一处。
    session.flush()
    for rel_path, writer, validated in artifacts:
        session.add(
            Artifact(
                id=uuid.uuid4(),
                feature_request_id=request.id,
                task_card_id=card.id,
                writer_agent_id=writer,
                rel_path=rel_path,
                bytes=len(rel_path) * 32,
                lines=14,
                format=rel_path.rsplit(".", 1)[-1],
                self_reported_status="success",
                validated_status=validated,
                invalid_reason="行数不足" if validated == "invalid" else None,
                created_at=NOW,
            )
        )
    session.flush()
    return request.id


def _package_rows(session: Session, feature_request_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Deliverable).where(Deliverable.feature_request_id == feature_request_id)
        )
        or 0
    )


# ---------------------------------------------------------------- ① 行数恰为 1


@needs_pg
def test_a_delivered_batch_lands_exactly_one_package_row(session, bus_root):
    """T099 ①：`deliverable` 对这张需求单**恰有一行**，且 manifest 的成员数等于 valid 产物数。

    两条断言各管一种坏法：行数不管"组了两次"，成员数不管"漏了一条"。
    """
    request_id = _seed_request(session)
    package = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        bus_root=bus_root,
    )
    assert _package_rows(session, request_id) == 1
    assert len(package.manifest) == 1
    assert package.entry_path == ENTRY


# ---------------------------------------------------------------- ② 再插必失败（两道）


@needs_pg
def test_a_second_package_for_the_same_request_is_refused_without_touching_the_first(
    session, bus_root
):
    """T099 ② 的应用层那道：第二次组装报错，**且第一包的内容没被覆盖**。

    "不静默覆盖"是 owner 在 T094 那行明写的（`tasks.md:2741`）。只断"抛异常"不够 ——
    先覆盖再抛的写法在这条上照样绿，而那正是最坏的一种：交付已发出又被改写。
    """
    request_id = _seed_request(
        session,
        artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid")),
    )
    first = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        bus_root=bus_root,
    )
    with pytest.raises(asm.PackageAlreadyAssembled) as caught:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=SECOND_ARTIFACT,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )
    assert caught.value.code == "delivery_package_exists"
    row = session.scalars(select(Deliverable).where(Deliverable.feature_request_id == request_id)).one()
    assert row.entry_path == first.entry_path, "第二次组装把第一包的入口改写了"
    assert _package_rows(session, request_id) == 1


@needs_pg
def test_the_database_itself_refuses_a_second_row_when_the_app_layer_is_bypassed(session):
    """T099 ② 的库面那道：不走 `assemble()`，直插第二行也撞唯一索引。

    这一条**证到的比上一条多**：上一条里"被拒"是应用层那段 `try` 的功劳，删掉它照样绿；
    这里绕开应用层，唯一索引是唯一的判据。两支都在，"唯一"才不是一段可以随时删掉的代码。
    反向对照（另一张需求单能插）在下一条，两条合起来才说清"唯一"的键是哪一列。
    """
    request_id = _seed_request(session)

    def _raw() -> None:
        session.add(
            Deliverable(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                manifest=[],
                entry_path=ENTRY,
                open_issues=[],
                assembled_by_agent_id="workbuddy",
                created_at=NOW,
            )
        )

    with session.begin_nested():
        _raw()
        session.flush()
    with pytest.raises(IntegrityError) as caught, session.begin_nested():
        _raw()
        session.flush()
    assert "feature_request_id" in str(caught.value), str(caught.value)
    assert _package_rows(session, request_id) == 1


@needs_pg
def test_the_uniqueness_is_per_request_not_global(session, bus_root):
    """反向对照（②的另一半）：唯一键是 `feature_request_id`，不是"全平台只许一个包"。

    少了这条，`UNIQUE((feature_request_id))` 被写成 `UNIQUE(id)` 之外还有个更坏的改法 ——
    给整张表加一条"只允许一行"的约束，前两条判据全绿而平台只能交付一次。
    """
    first = _seed_request(session)
    second = _seed_request(session)
    for request_id in (first, second):
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )
    assert (_package_rows(session, first), _package_rows(session, second)) == (1, 1)
    assert int(session.scalar(select(func.count()).select_from(Deliverable)) or 0) == 2


# ---------------------------------------------------------------- ③ 入口真存在


@needs_pg
def test_an_entry_that_is_not_in_the_manifest_is_refused(session, bus_root):
    """T099 ③ 的前半：入口 MUST 是本批交付的产物之一（文件在盘上也不够）。

    `SECOND_ARTIFACT` 这条在 `bus_root` 里**不存在**，但故意让它也不在 manifest 里 ——
    两个条件各查各的，先查 manifest 才轮得到存在性（`assemble.py:207` 的顺序）。
    """
    request_id = _seed_request(session)
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=SECOND_ARTIFACT,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )
    assert caught.value.code == "entry_not_in_manifest"


@needs_pg
def test_an_entry_named_by_the_manifest_but_absent_from_disk_is_refused(session, tmp_path):
    """T099 ③ 的后半：manifest 里有这条记录，但盘上没有那份文件 ⇒ 拒。

    这条是 R-07 第①条"读取入口"的真身：**产物行是数据库里的一条记录，记录不会自己变成文件**。
    对照 = 上一条用真 `bus_root`（文件在）能组包成功 —— 同一个入口串，唯一差别就是盘上有没有。
    """
    request_id = _seed_request(session)
    empty_root = tmp_path / "没有产物的总线"
    empty_root.mkdir()
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=empty_root,
        )
    assert caught.value.code == "entry_missing"


# ---------------------------------------------------------------- ④ 每条可反查产出者


@needs_pg
def test_every_manifest_entry_resolves_to_the_artifact_row_that_produced_it(session, bus_root):
    """T099 ④：`rel_path` → 产物行 → `writer_agent_id`，逐条对上，且**实测值不是自评值**。

    两支产物由**不同**的 agent 产出：如果实现把产出者写成常量（或写需求单下第一支），
    第二条就对不上。`bytes` / `lines` 同样按行比对 —— 交付包里那两个数是给门禁复算用的。
    """
    request_id = _seed_request(
        session,
        artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid")),
    )
    package = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        bus_root=bus_root,
    )
    assert len(package.manifest) == 2
    for entry in package.manifest:
        row = session.scalars(
            select(Artifact).where(Artifact.feature_request_id == request_id, Artifact.rel_path == entry.rel_path)
        ).one()
        assert entry.producer_agent_id == row.writer_agent_id, entry
        assert (entry.bytes, entry.lines, entry.format) == (row.bytes, row.lines, str(row.format)), entry
        assert entry.task_card_id == row.task_card_id
    assert {entry.producer_agent_id for entry in package.manifest} == {"qoder", "doubao"}


@needs_pg
def test_only_gate_validated_artifacts_enter_the_package(session, bus_root):
    """自述成功但门禁没过 / 还没判过的产物 MUST NOT 进最终交付（FR-019 之后才有资格，SC-007）。

    三条产物：`valid` / `invalid` / `NULL`（还没覆写）。manifest 只许含第一条 ⇒
    双向断言：既不许含坏的那两条，也不许把好的那条也漏掉（只断前者会放过"全都不进"的写法）。
    """
    request_id = _seed_request(
        session,
        artifacts=(
            (ENTRY, "qoder", "valid"),
            (SECOND_ARTIFACT, "doubao", "invalid"),
            ("qoder-workspace/extra.md", "qoder", None),
        ),
    )
    package = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        bus_root=bus_root,
    )
    assert {entry.rel_path for entry in package.manifest} == {ENTRY}


@needs_pg
def test_a_batch_with_no_validated_artifact_delivers_nothing_rather_than_an_empty_package(session):
    """全批都没过门禁 ⇒ 抛，而不是交一份空 manifest 的包。

    空包在计数上也是"一行 deliverable"，所以它会把"行数恰为 1"这条骗过去 —— 这条判据必须在场。
    """
    request_id = _seed_request(session, artifacts=((ENTRY, "qoder", "invalid"),))
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.validated_manifest(session, request_id)
    assert caught.value.code == "nothing_to_deliver"
    assert _package_rows(session, request_id) == 0


@needs_pg
def test_nothing_is_assembled_before_a_card_reaches_delivery(session, bus_root):
    """卡还在 `RUNNING` ⇒ 没有交付可言，组包抛 `nothing_delivered`。

    反向对照同一条里做完：把同一张卡推到 `DELIVERED` 后同一批产物就能组包 ——
    否则"拒"可能只是"谁都拒"。
    """
    request_id = _seed_request(session, status="RUNNING")
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )
    assert caught.value.code == "nothing_delivered"

    card = session.scalars(select(TaskCard).where(TaskCard.feature_request_id == request_id)).one()
    card.status = "DELIVERED"
    session.flush()
    package = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        bus_root=bus_root,
    )
    assert package.manifest


# ---------------------------------------------------------------- T093 的交付面：清单必填非空


@needs_pg
def test_a_delivery_with_issues_must_carry_a_non_empty_issue_list(session, bus_root):
    """`DELIVERED_WITH_ISSUES` 的卡 + 空 `open_issues` ⇒ 拒（FR-035 的"显式列出"落到 EN-17）。

    这一条把 T093 的状态与 T094 的包连起来：状态说"有问题"，包里却写着没问题，
    读交付包的人就被骗了第二次，而这次没有下一轮可审。
    """
    request_id = _seed_request(session, status="DELIVERED_WITH_ISSUES")
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )
    assert caught.value.code == "open_issues_required"

    package = asm.assemble(
        session,
        feature_request_id=request_id,
        entry_path=ENTRY,
        assembled_by="workbuddy",
        created_at=NOW,
        open_issues=[{"item": "首屏有公司名", "gap": "标题里没有", "suggestion": "补 <h1>"}],
        bus_root=bus_root,
    )
    assert len(package.open_issues) == 1
    row = session.scalars(select(Deliverable).where(Deliverable.feature_request_id == request_id)).one()
    assert row.open_issues and row.open_issues[0]["item"] == "首屏有公司名"


# ---------------------------------------------------------------- T095：公共区单点代笔


def test_the_public_copy_carries_the_index_and_nothing_else(tmp_path: Path):
    """T095：`results/delivery_package.json` 由编排引擎写，**写的是索引不是副本**。

    三条各挡一种坏法：
    - 路径 = `assemble.delivery_path()`（两处引用同一个常量，不各写一遍）；
    - JSON 里 MUST NOT 出现产物正文（复制文件树 = 违原则 I）；
    - 中文不转义（R-07 的"打开就能看懂"）。
    不碰 PG，纯临时目录。
    """
    project = tmp_path / layout.BUS_ROOT_NAME / "demo"
    for agent_id in FLEET:
        (project / f"{agent_id}{layout.WORKSPACE_SUFFIX}").mkdir(parents=True)
    gateway = BusGateway(project)
    payload = {
        "deliverable_id": str(uuid.uuid4()),
        "feature_request_id": str(uuid.uuid4()),
        "entry_path": ENTRY,
        "manifest": [{"rel_path": ENTRY, "producer": "qoder", "bytes": 640, "lines": 14, "format": "html"}],
        "open_issues": [{"item": "首屏有公司名", "gap": "标题缺", "suggestion": "补 <h1>"}],
        "assembled_by": "workbuddy",
    }
    with OrchestratorWriter(gateway) as writer:
        written = writer.write_delivery_package(payload)
    assert written == asm.delivery_path() == layout.DELIVERY_PACKAGE_JSON
    text = (project / written).read_text(encoding="utf-8")
    assert json.loads(text) == payload
    assert "首屏有公司名" in text, "中文被转义成 \\uXXXX，交付说明打开读不出"
    assert "<html>" not in text, "交付包里出现了产物正文 ⇒ 那是第二份文件树"


def test_the_delivery_copy_is_writable_only_by_the_orchestrator_identity():
    """T095 的"单点"两半：编排引擎放行，只读审查岗拒。

    两支都要 —— 只断"拒"就把代笔点写成了"没人能写"（公共区永远空着，交付包等于没发出去），
    只断"放行"就把公共区写成了谁都能改。ACL 的判据只有一个载体（`acl.can_write`），这里不复写。
    """
    reviewer = acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}doubao", "reviewer", read_only=True)
    allowed = acl.can_write(acl.Actor(acl.PUBLIC_OWNER, "orchestrator"), layout.DELIVERY_PACKAGE_JSON, None)
    denied = acl.can_write(reviewer, layout.DELIVERY_PACKAGE_JSON, acl.Capability("doubao", "T01"))
    assert allowed.allowed is True, allowed
    assert denied.allowed is False and denied.code == acl.READ_ONLY_VIOLATION, denied


# ---------------------------------------------------------------- T096：冲突裁决


def conflict(item: str = "首页标题", sides=("qoder", "doubao"), candidates=(ENTRY, SECOND_ARTIFACT),
             chosen: str = ENTRY, rationale: str = "对照原始需求：首页只许一个标题") -> asm.Conflict:
    return asm.Conflict(item=item, sides=sides, candidates=candidates, chosen_rel_path=chosen, rationale=rationale)


def _statuses(session: Session, feature_request_id: uuid.UUID) -> list[str]:
    return [
        str(row.status)
        for row in session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .order_by(TaskCard.id)
        .all()
    ]


def _notes(session: Session) -> int:
    """EN-15 现有行数。按 `project_key` 数而不是按需求单数 —— `MeetingNote` 上没有需求单外键，
    本文件全部夹具共用 `"demo"`，所以这个数就是"这个键下总共记了几条纪要"，别把它读成"这张单的纪要数"。
    """
    return int(
        session.scalar(
            select(func.count()).select_from(MeetingNote).where(MeetingNote.project_key == "demo")
        )
        or 0
    )


@needs_pg
def test_a_self_judged_conflict_moves_the_involved_cards_into_escalation(session):
    """待裁 ⑪（owner 2026-10-14）：裁决人就是冲突一方 ⇒ 涉事卡进 `ESCALATED`，纪要一行都不写。

    三条一起才叫"升人工"落地，缺任一条都只是抛了个错：
    ① 稳定键还是 `adjudicator_is_a_side`（调用方照旧被拦住）；
    ② 卡的状态**确实**变了，而且异常文本里点名是哪张卡（不让人猜升级有没有落）；
    ③ EN-15 一行没多（写了纪要就等于把一次没资格的裁决装订成"开过会"）。
    涉事卡是从**候选产物行反查**的，不是调用方申报的 —— 申报就等于让想绕过门禁的一方自己填门禁输入。
    """
    request_id = _seed_request(
        session, status="REVIEWING", artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid"))
    )
    assert _statuses(session, request_id) == ["REVIEWING"], _statuses(session, request_id)
    before_notes = _notes(session)

    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.adjudicate(
            session,
            feature_request_id=request_id,
            project_key="demo",
            conflicts=[conflict(sides=("qoder", "workbuddy"))],
            decided_by="workbuddy",
            created_at=NOW,
        )
    assert caught.value.code == "adjudicator_is_a_side"
    assert "已推进 ESCALATED 的卡：['T01']" in str(caught.value), str(caught.value)

    session.expire_all()
    assert _statuses(session, request_id) == ["ESCALATED"], _statuses(session, request_id)
    assert _notes(session) == before_notes, "被拒的自裁还是留下了纪要"


@needs_pg
def test_a_card_that_already_delivered_is_not_recalled_by_a_later_conflict(session):
    """反向那一半：已定论（`DELIVERED`）的卡**不被后续冲突追回**，但这次裁决照旧被拒。

    这一条不是"顺手多写一条"：`data-model.md §2` 那条"已完成任务不得回到 `READY` 重跑（FR-041）"
    说的是同一个理由。没有它，"升级"就变成"任何人事后可以把手伸进已交付的卡"。
    """
    request_id = _seed_request(
        session, status="DELIVERED", artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid"))
    )
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.adjudicate(
            session,
            feature_request_id=request_id,
            project_key="demo",
            conflicts=[conflict(sides=("qoder", "workbuddy"))],
            decided_by="workbuddy",
            created_at=NOW,
        )
    assert caught.value.code == "adjudicator_is_a_side"
    assert "已推进 ESCALATED 的卡：（无）" in str(caught.value), str(caught.value)
    assert "已定论不予追回的卡：['T01']" in str(caught.value), str(caught.value)
    session.expire_all()
    assert _statuses(session, request_id) == ["DELIVERED"], _statuses(session, request_id)


@needs_pg
def test_a_self_judged_conflict_that_resolves_to_no_card_is_still_named(session):
    """涉事卡反查为空（候选路径在库里没有产物行）⇒ 不许静默：异常照抛，且明写"升级没落点"。

    这是一条**防御分支**用例，不是装饰：那个分支今天走不到产品路径，但"candidates 是编的"正是
    申报式接口最容易长出来的形状。少了它，这条分支改成 `pass` 也不会有任何判据变红。
    """
    request_id = _seed_request(session, status="REVIEWING")
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.adjudicate(
            session,
            feature_request_id=request_id,
            project_key="demo",
            conflicts=[
                conflict(
                    item="凭空的冲突",
                    sides=("qoder", "workbuddy"),
                    candidates=("qoder-workspace/库里没有的那份.html",),
                    chosen="qoder-workspace/库里没有的那份.html",
                )
            ],
            decided_by="workbuddy",
            created_at=NOW,
        )
    assert caught.value.code == "adjudicator_is_a_side"
    assert "升级没落点" in str(caught.value), str(caught.value)
    session.expire_all()
    assert _statuses(session, request_id) == ["REVIEWING"], "反查不到卡却还是把卡动了"


@needs_pg
def test_a_third_party_ruling_moves_no_card_and_does_leave_notes(session):
    """反向对照（必须绿的那一支）：裁决人不在参与方里 ⇒ 卡不动、纪要照落。

    上面三条测的都是"被拒"，只有这一条测"放行"。少了它，`adjudicate` 改成"谁来裁都升级"也全绿。
    """
    request_id = _seed_request(
        session, status="REVIEWING", artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid"))
    )
    before_notes = _notes(session)
    ids = asm.adjudicate(
        session,
        feature_request_id=request_id,
        project_key="demo",
        conflicts=[conflict(sides=("qoder", "doubao"))],
        decided_by="workbuddy",
        created_at=NOW,
    )
    assert len(ids) == 1
    session.expire_all()
    assert _statuses(session, request_id) == ["REVIEWING"], "第三方裁决也把卡推进了升级态"
    assert _notes(session) == before_notes + 1


def test_a_ruling_must_pick_one_of_the_candidates_and_say_why():
    """T096 的硬规则②③：选中项不在候选里 / 理由空白 ⇒ 各自拒。

    两条坏法分开量（一个 match 词判两种形状是本轮点名过的失效形态）：
    凭空造第三份产物是"新增"，只写"就这么定了"是"没记"。
    """
    for bad, code in (
        (conflict(chosen="qoder-workspace/第三个方案.html"), "chosen_not_a_candidate"),
        (conflict(rationale="   "), "rationale_required"),
    ):
        with pytest.raises(asm.AssembleMisuse) as caught:
            asm.adjudicate(
                None,  # type: ignore[arg-type]
                feature_request_id=uuid.uuid4(),
                project_key="demo",
                conflicts=[bad],
                decided_by="workbuddy",
                created_at=NOW,
            )
        assert caught.value.code == code, bad


@needs_pg
def test_each_adjudicated_conflict_leaves_exactly_one_note(session):
    """T096 的落地面：N 处冲突 ⇒ EN-15 恰 N 行，且参与方名单与裁决人都可反查。

    写成关系式（`行数 == len(conflicts)`）而不是常量 1：两处冲突时它照样成立。
    第二处刻意换一组 `sides` —— 只有一处冲突的读数分不清 `conflict_sides` 是不是被写成了常量。
    """
    request_id = _seed_request(session)
    conflicts = [
        conflict(),
        conflict(item="配色", sides=("doubao",), candidates=(SECOND_ARTIFACT,), chosen=SECOND_ARTIFACT,
                 rationale="原始需求没提配色，取执行者那一版"),
    ]
    ids = asm.adjudicate(
        session,
        feature_request_id=request_id,
        project_key="demo",
        conflicts=conflicts,
        decided_by="workbuddy",
        created_at=NOW,
    )
    assert len(ids) == len(conflicts) == 2
    rows = session.scalars(select(MeetingNote).where(MeetingNote.id.in_(list(ids)))).all()
    assert len(rows) == len(conflicts)
    assert {tuple(row.conflict_sides) for row in rows} == {("qoder", "doubao"), ("doubao",)}
    assert all(row.authored_by_agent_id == "workbuddy" for row in rows)
    assert all("原始需求" in row.decision or "需求" in row.decision for row in rows)


@needs_pg
def test_a_refused_ruling_leaves_no_note_at_all(session):
    """⑬ 的形状：断言在写库之前 ⇒ 被裁掉的冲突一行都不留。

    这一条与上一条是同一处判据的两半 —— 只有上一条的话，"先写后判"的写法也是绿的
    （纪要落了库，事后抛异常，读起来像"裁过又撤销"）。
    """
    request_id = _seed_request(session)
    before = int(session.scalar(select(func.count()).select_from(MeetingNote)) or 0)
    with pytest.raises(asm.AssembleMisuse) as caught:
        asm.adjudicate(
            session,
            feature_request_id=request_id,
            project_key="demo",
            conflicts=[conflict(), conflict(chosen="qoder-workspace/第三个方案.html")],
            decided_by="workbuddy",
            created_at=NOW,
        )
    assert caught.value.code == "chosen_not_a_candidate"
    assert int(session.scalar(select(func.count()).select_from(MeetingNote)) or 0) == before, "被拒的那一批留下了纪要"


def test_the_payload_shape_is_the_six_keys_the_contract_names():
    """交付包 JSON 的键集合 = `Package.to_payload()` 的六个键，MUST NOT 多也必须齐。

    多一个键（比如把产物正文塞进 `body`）就是第二份事实态；少一个键（比如丢了 `open_issues`）
    就是 FR-035 的清单在出口处消失。两支合起来才把"索引"这个形状钉住。
    """
    package = asm.Package(
        deliverable_id=uuid.uuid4(),
        feature_request_id=uuid.uuid4(),
        entry_path=ENTRY,
        manifest=(asm.ManifestEntry(ENTRY, "qoder", "T01", 640, 14, "html"),),
        open_issues=(),
        assembled_by="workbuddy",
    )
    assert set(asm.payload_of(package)) == {
        "deliverable_id",
        "feature_request_id",
        "entry_path",
        "manifest",
        "open_issues",
        "assembled_by",
    }
    assert set(asm.payload_of(package)["manifest"][0]) == {  # type: ignore[index]
        "rel_path",
        "producer",
        "task_card_id",
        "bytes",
        "lines",
        "format",
    }


# ---------------------------------------------------------------- P0-4 字节重算


def test_a_manifest_member_whose_size_changed_is_refused_as_stale(session, bus_root):
    """磁盘上的文件被改小/改大 ⇒ `artifact_stale`，不许拿库里的旧字节数组包。"""
    request_id = _seed_request(session)
    target = bus_root / ENTRY
    target.write_bytes(target.read_bytes() + b"x")

    with pytest.raises(asm.AssembleMisuse) as exc:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )

    assert exc.value.code == "artifact_stale"


def test_a_manifest_member_missing_from_disk_is_refused_as_stale(session, bus_root):
    """清单里有、盘上没有 ⇒ `artifact_stale`；入口还在，所以不会先撞 `entry_missing`。"""
    request_id = _seed_request(
        session,
        artifacts=((ENTRY, "qoder", "valid"), (SECOND_ARTIFACT, "doubao", "valid")),
    )
    (bus_root / SECOND_ARTIFACT).unlink()

    with pytest.raises(asm.AssembleMisuse) as exc:
        asm.assemble(
            session,
            feature_request_id=request_id,
            entry_path=ENTRY,
            assembled_by="workbuddy",
            created_at=NOW,
            bus_root=bus_root,
        )

    assert exc.value.code == "artifact_stale"
