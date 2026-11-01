"""T110（FR-064｜R-14）：`python -m acp.recover --from-snapshot last` 的机制与装配判据。

档位：`tests/guards/`，混合文件。一 ~ 四节要真 PG（skip 挂在 `engine` 上，口径同 T107 / T108 ——
不逐条挂 marker，免得新加一条要库的用例忘了挂就在无 PG 机器上红成假缺陷）；第五节要真 checkpointer
（PG + LangGraph）；第六节的 CLI 那五条不连库（只在缺 `ACP_DB_DSN` 时判"环境档拒绝"那一格）。

**这六节各挡什么**（逐条对位 owner 给 T110 的那句原文）：

| 原文 / 义务 | 节 |
|---|---|
| "LangGraph state 损坏时从最近存档点重建执行态" | 一（哪一个点）、五（写回 thread） |
| "重放该存档点之后的任务事件" | 二（锚、序、范围） |
| "恢复到可继续调度的最小状态" | 三（交集、`thread_values` 的键集） |
| D-12 "回退动作本身落审计" | 四（`rolled_back_at` 收成一处写者） |
| "MUST NOT 整链路重跑" | 五（缺拆单器 ⇒ 从头跑必然响亮失败） |
| 凭据面（宪法「二」）与"三条路互斥"、git 面不放宽 | 六 |
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import acp
from acp import chaos, recover
from acp.archive import cadence, restore
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import checkpointer as cp
from acp.persistence import events, snapshots as snap
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from conftest import CHILD_STDIO_ENV

SRC_ROOT = Path(acp.__file__).resolve().parent
PROJECT = "acp-self-host"
CARD_A = "T01"
CARD_B = "T02"
NOW = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone.utc)
#: 一个**形状合法但盘上不存在**的对象名：第三道拒绝拿它当诱饵（形状 ≠ 存在，这一对必须分开测）。
ABSENT_SHA = "f" * 40
#: 子进程 env 的哨兵变量名：`test_the_child_sees_the_parent_environment` 靠它量"有没有完整继承"。
ENV_PROBE = "ACP_TEST_ENV_PROBE"


def _child_env() -> dict[str, str]:
    """起子进程用的 env = **完整继承父进程** + 覆写两格（`PYTHONPATH` 与被量子进程读的那棵树、`PYTHONIOENCODING`）。

    原来这里手写的是极简 env（只给 `PYTHONPATH` 与 `PATH`）。它在 POSIX 上恰好能用，
    但那条判据要量的事实是"import 之后重模块数 = 0"，而极简 env 让子进程**可能根本走不到那一步**：
    `acp.recover → sqlalchemy.util.concurrency → asyncio`，Windows 上 `asyncio` 会拉
    `windows_events` / `_overlapped`，缺 `SYSTEMROOT` 时这一步直接抛 `WinError 10106`
    ⇒ 判据**恒红、且红在 import 阶段**（WorkBuddy 的 `phase9-review-v1.0.0.md` §0.3 三档对照实测：
    原样 rc=1 / 补 `SYSTEMROOT` rc=0 且 stdout=`0` / 完整继承 rc=0；本机同一发在 Anaconda 3.13.9 上
    不红 ⇒ 是**机器相关的环境耦合**，不是"这条判据错了"，更不是我这边能拿来当"已验"的证据）。
    继承整份 env 之后，"显式钉住 `src` 这一棵树"的意图保留，环境面不再由判据自己造。
    例外只有 `PYTHONIOENCODING` 那一格：它不是"再造一份环境"，而是把**子进程的写入口径**钉到与
    父进程读入口径（`encoding="utf-8", errors="replace"`）同一次读数上 —— 本仓路径自带中文，
    不钉这一格时那发判据读回的 `PYTHONPATH` 是 `\ufffd`，而红因与要证的事（env 有没有继承）无关。
    """
    return {**os.environ, **CHILD_STDIO_ENV, "PYTHONPATH": str(SRC_ROOT.parent)}


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG（存档点行与流水只在库里读得出），整条 skip（不是绿）")
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
    """独立 autocommit 连接（`persistence/checkpointer.py` 模块头第 1 条），清场口径同 T048。

    它写下的 checkpoint 行不在业务事务里，不会被 `session` 的回滚带走 ⇒ 用过哪个 thread
    就登记哪个，收尾按 thread 删，否则这批行成为下一次跑的脏现场。
    """
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


def claim_thread(connection, request_id: uuid.UUID) -> str:
    from acp.orchestrate import graph

    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    connection.threads_used.append(thread)
    return thread


def _request(session: Session, *, project_key: str = PROJECT) -> uuid.UUID:
    request_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id,
            project_key=project_key,
            raw_input="恢复路径的用例底座",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    session.flush()
    return request_id


def _cards(
    session: Session, request_id: uuid.UUID, card_ids: list[str], *, snapshot_id: uuid.UUID, status: str = "READY"
) -> None:
    for card_id in card_ids:
        session.add(
            TaskCard(
                id=card_id,
                feature_request_id=request_id,
                title=f"卡 {card_id}",
                difficulty=2,
                skill_tags=["frontend_coding"],
                depends_on=[],
                executor_agent_id=None,
                executor_role_id=None,
                reviewer_agent_id=None,
                acceptor_agent_id=None,
                artifact_floor_min_bytes=512,
                artifact_floor_min_lines=12,
                artifact_floor_format="html",
                acceptance_criteria=["首屏有标题"],
                handoff_count=0,
                review_round=0,
                status=status,
                snapshot_id=snapshot_id,
            )
        )
    session.flush()


def _event(session: Session, *, card: str, kind: str, at: dt.datetime) -> int:
    row = events.record(
        session,
        entity="task_card",
        entity_id=card,
        kind=kind,
        actor_agent_id="platform",
        payload={"probe": kind},
        created_at=at,
    )
    return int(row.id)


def _raw_point_row(session: Session, *, ref: str, cards: list[str], trace: bool = True) -> Snapshot:
    """直接插一行存档点（可带可不带 EN-14 留痕），只为造出仓储那条链**造不出**的形态。

    `snap.record_point()` 拒临时句柄、也必带留痕，所以第一节的 ②④ 两道只能从这儿搭。
    这不违反"EN-12 写者一处"：那条判据扫的是 `src/`（生产代码），测试搭现场是搭现场。
    """
    row = Snapshot(
        id=uuid.uuid4(),
        round_no=1,
        phase=cadence.OPEN_PHASE,
        task_card_ids=cards,
        ref=ref,
        rolled_back_at=None,
    )
    session.add(row)
    session.flush()
    if trace:
        events.record(
            session,
            entity="snapshot",
            entity_id=str(row.id),
            kind=snap.SNAPSHOT_KIND,
            actor_agent_id="platform",
            payload={"phase": cadence.OPEN_PHASE, "round_no": 1, "ref": ref},
            created_at=NOW,
        )
    return row


def _real_point(
    session: Session, project_dir: Path, *, cards: list[str], round_no: int = 1
) -> Snapshot:
    """打一拍**真**存档（git 对象在盘上）再落库：走 T109 定稿的那条链，不是手拼行。"""
    point = cadence.commit_archive_point(
        project_dir,
        phase=cadence.OPEN_PHASE,
        round_no=round_no,
        project_key=PROJECT,
        task_card_ids=cards,
    )
    return snap.record_point(
        session,
        project_key=PROJECT,
        round_no=round_no,
        phase=cadence.OPEN_PHASE,
        task_card_ids=cards,
        ref=point.ref,
        now=NOW,
    )


def _restoration_stub(**overrides) -> restore.Restoration:
    base = dict(
        snapshot_id=uuid.uuid4(),
        project_key=PROJECT,
        round_no=1,
        phase=cadence.OPEN_PHASE,
        ref=f"{cadence.GIT_REF_PREFIX}{'a' * 40}",
        sha="a" * 40,
        archive_root=Path("/unused"),
        feature_request_id=uuid.uuid4(),
        anchor_at=NOW,
        cards_at_point=(CARD_A, CARD_B),
        card_statuses=((CARD_A, "READY"), (CARD_B, "DELIVERED")),
        replayed=(),
        request_status="INTAKE",
    )
    base.update(overrides)
    return restore.Restoration(**base)


# ---------------------------------------------------------------- 一、四个拒绝全部发生在写之前

def test_a_project_without_any_point_refuses_rather_than_inventing_a_start(
    session: Session,
) -> None:
    """一个存档点都没有 ⇒ `no_points`。这条挡的是"没有点就退回空 state 从头跑"那种看似宽容的实现。"""
    _request(session)
    before = int(session.scalar(select(func.count()).select_from(Snapshot)) or 0)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=Path("/unused"))

    assert caught.value.code == "no_points"
    assert int(session.scalar(select(func.count()).select_from(Snapshot)) or 0) == before


def test_a_temporary_handle_row_cannot_be_a_restore_point(session: Session) -> None:
    """T-15 那类 `local:` 自指行**如果还在库里**，不能当恢复起点。

    与 T108 的"新写入不许再用它"是一对：那一侧拦写入，这一侧拦读取。少了这一侧，库里那行历史
    占位就会被读成"曾经有一份存档"，而 FR-064 的全部前提就是"有一份真存档可回"。

    卡要先挂在这行上（`task_card.snapshot_id` 是 NOT NULL 的 FK），否则 `list_points()` 的
    "按项目的卡反查"根本不会把这行端出来 ⇒ 读到的是 `no_points` 而不是 `ref_not_real`。
    这个顺序本身就是第一条读数：**恢复只看得到"卡真在库里"的那些点**。
    """
    row = _raw_point_row(
        session, ref=f"{cadence.TEMPORARY_REF_PREFIX}registry/snapshot-{uuid.uuid4()}", cards=[CARD_A], trace=False
    )
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=Path("/unused"))

    assert caught.value.code == "ref_not_real"
    assert str(session.get(Snapshot, row.id).ref).startswith(cadence.TEMPORARY_REF_PREFIX), (
        "拒绝的一侧不许顺手改写历史行：抹平它就等于假装 T-15 没存在过"
    )


def test_a_shaped_handle_whose_object_is_absent_is_refused(session: Session, bus) -> None:
    """第三道：形状对、盘上没有 ⇒ `archive_object_missing`。**这一道走真 git，不用桩**。

    为什么不用桩：`is_real_handle()` 已经管住形状，这一道的全部内容就是"去仓库里问一次"；
    拿桩问 = 用实现自己的回答证明实现是对的（原则 I 那一类空判据）。
    """
    project_dir = bus.with_project(PROJECT)
    _cards(session, _request(session), [CARD_A], snapshot_id=_raw_point_row(
        session, ref=f"{cadence.GIT_REF_PREFIX}{ABSENT_SHA}", cards=[CARD_A]
    ).id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert caught.value.code == "archive_object_missing"
    assert ABSENT_SHA in str(caught.value), "对象名不是凭据，可以进消息；git 的 stderr 原文才不行"


def test_a_point_without_its_snapshot_trace_is_refused(session: Session, bus) -> None:
    """第四道：没有 EN-14 的 `SNAPSHOT` 留痕 ⇒ `trace_missing`（没有时间锚就不知道"其后"从哪算起）。"""
    project_dir = bus.with_project(PROJECT)
    row = _raw_point_row(
        session,
        ref=cadence.commit_archive_point(
            project_dir,
            phase=cadence.OPEN_PHASE,
            round_no=1,
            project_key=PROJECT,
            task_card_ids=[CARD_A],
        ).ref,
        cards=[CARD_A],
        trace=False,
    )
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert caught.value.code == "trace_missing"


def test_a_card_absent_from_en02_is_refused_with_the_missing_id_named(session: Session, bus) -> None:
    """存档点说两张卡、库里只有一张 ⇒ `cards_missing` 且**点名缺的那张**。

    半套事实不能拿去重建执行态：`card_order` 里留一张没有 EN-02 行的卡，下一次 `Dispatch`
    就在一个不存在的号上调度。
    """
    project_dir = bus.with_project(PROJECT)
    phantom = "T99"
    row = _real_point(session, project_dir, cards=[CARD_A, phantom])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert caught.value.code == "cards_missing"
    assert phantom in str(caught.value)


def test_a_point_whose_cards_span_two_requests_is_refused(session: Session, bus) -> None:
    """卡跨两张需求单 ⇒ `thread_id` 推不出来，不猜（MVP-1 的 thread 号是 `(卡点类, 需求单)` 的函数）。"""
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A, CARD_B])
    for card_id in (CARD_A, CARD_B):
        _cards(session, _request(session), [card_id], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert caught.value.code == "ambiguous_request"


def test_an_explicitly_named_unknown_point_is_refused_rather_than_falling_back_to_last(
    session: Session, bus
) -> None:
    """点名一个不在清单里的 id ⇒ `snapshot_unknown`，**不是**退回 `last`。

    "找不到就挑最后一个"在恢复这种动作上是越权：操作者要回第 3 轮，系统把他带到第 7 轮。
    """
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, snapshot=str(uuid.uuid4()), archive_root=project_dir)

    assert caught.value.code == "snapshot_unknown"


def test_a_non_uuid_snapshot_argument_is_refused_on_shape(session: Session, bus) -> None:
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)

    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.reconstruct(session, project_key=PROJECT, snapshot="the-latest-one", archive_root=project_dir)

    assert caught.value.code == "snapshot_shape"


# ---------------------------------------------------------------- 二、锚 · 序 · 重放范围

def test_last_picks_the_highest_round_not_the_last_row_inserted(session: Session, bus) -> None:
    """`last` 用的是 `list_points()` 的序（轮次↑拍名按域声明序↑），不是插入序、不是 id 序。

    两张卡挂在**同一张需求单**上：分成两张就会先在 `ambiguous_request` 那道拒掉，
    这一条要测的是"选哪个点"，不是"点能不能用"。
    """
    project_dir = bus.with_project(PROJECT)
    request_id = _request(session)
    first = _real_point(session, project_dir, cards=[CARD_A], round_no=1)
    _cards(session, request_id, [CARD_A], snapshot_id=first.id)
    second = _real_point(session, project_dir, cards=[CARD_A, CARD_B], round_no=2)
    _cards(session, request_id, [CARD_B], snapshot_id=second.id)

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert result.snapshot_id == second.id and result.round_no == 2
    assert result.cards_at_point == (CARD_A, CARD_B)


def test_events_at_or_before_the_anchor_are_not_replayed(session: Session, bus) -> None:
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="TASK", at=NOW - dt.timedelta(minutes=5))

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert result.replayed == ()


def test_events_after_the_anchor_are_replayed_in_stored_order(session: Session, bus) -> None:
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="REVIEW", at=NOW + dt.timedelta(minutes=3))
    _event(session, card=CARD_A, kind="TASK", at=NOW + dt.timedelta(minutes=1))

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert [item.kind for item in result.replayed] == ["TASK", "REVIEW"], (
        "序 = `(created_at, id)` 升序：写入的先后与时刻的先后不一致时，恢复报告不能跟着抖"
    )


def test_the_same_moment_is_broken_by_the_event_id_not_included_by_accident(
    session: Session, bus
) -> None:
    """同刻的两条流水：id 比锚小的不算"其后"、id 比锚大的才算。**这一条钉的就是 `>` 与 `>=` 那一档**。

    现场按真实顺序搭：先有卡上的一次动作（第 1 轮期间），再打第 2 轮开工前那一拍（锚），
    然后又一次动作。三条共享同一时刻（`NOW`），于是唯一能分开它们的只有主键序 ——
    条件若写成 `created_at >= anchor.created_at`（丢掉 id 这一半），第一条就会混进重放清单，
    而按 `kind` 还筛不掉它（筛的维度是 `entity`）。
    """
    project_dir = bus.with_project(PROJECT)
    request_id = _request(session)
    holder = _raw_point_row(
        session,
        ref=cadence.commit_archive_point(
            project_dir,
            phase=cadence.OPEN_PHASE,
            round_no=1,
            project_key=PROJECT,
            task_card_ids=[CARD_A],
        ).ref,
        cards=[CARD_A],
        trace=False,
    )
    _cards(session, request_id, [CARD_A], snapshot_id=holder.id)
    earlier = _event(session, card=CARD_A, kind="TASK", at=NOW)
    anchor = _real_point(session, project_dir, cards=[CARD_A], round_no=2)
    later = _event(session, card=CARD_A, kind="REVIEW", at=NOW)

    trace_id = int(
        session.scalar(
            select(EventLog.id).where(
                EventLog.entity == "snapshot", EventLog.entity_id == str(anchor.id)
            )
        )
        or 0
    )
    assert trace_id != 0 and earlier < trace_id < later, (
        "前置：三条得共享同一时刻、且 id 把锚夹在中间，否则这条用例等于没断"
    )

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert result.snapshot_id == anchor.id
    assert [item.event_id for item in result.replayed] == [later]


def test_an_intercept_on_the_card_is_replayed_because_the_filter_is_the_entity_not_a_kind_list(
    session: Session, bus
) -> None:
    """`AUTONOMY_INTERCEPT` 也在"这张卡动过"之列 —— **这一条就是"别再抄一份 kind 名单"的证据**。

    第一版这里写的是 `kind in ("TASK","APPROVAL","ARTIFACT","REVIEW","SELECTION")`：拦截被筛掉
    （读的人以为那一轮什么都没发生），而 `APPROVAL` 是个死值（它的 `entity` 是 `approval_ticket`，
    按卡筛根本不会命中）。两个错都不是"少写一个 kind"，是**筛的维度选错了**。
    """
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="AUTONOMY_INTERCEPT", at=NOW + dt.timedelta(minutes=1))
    _event(session, card=CARD_A, kind="ARTIFACT", at=NOW + dt.timedelta(minutes=2))

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert [item.kind for item in result.replayed] == ["AUTONOMY_INTERCEPT", "ARTIFACT"]
    assert not hasattr(restore, "TASK_EVENT_KINDS"), (
        "kind 名单一旦回来就是第二份事实：域里新加一个 kind，那份抄件不会跟着变"
    )


def test_a_card_outside_the_point_is_out_of_the_replay_scope(session: Session, bus) -> None:
    """重放范围 = **这一轮那几张卡**，不是"这个项目之后的所有留痕"。

    诱饵放在同一张需求单上（另一张卡、同一时刻之后）：只有"按卡筛"与"按项目筛"能分开，
    这一条才不是白跑。库里两张卡都在、两条流水都在，读数必须只认存档点提名那一张。
    """
    project_dir = bus.with_project(PROJECT)
    request_id = _request(session)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, request_id, [CARD_A, CARD_B], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="TASK", at=NOW + dt.timedelta(minutes=1))
    _event(session, card=CARD_B, kind="TASK", at=NOW + dt.timedelta(minutes=2))

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert [item.entity_id for item in result.replayed] == [CARD_A]
    assert len(result.cards_at_point) == 1


def test_reconstruct_writes_not_a_single_row(session: Session, bus) -> None:
    """读面就是读面：`reconstruct()` 之后三张表的行数一张没多（"不写"这件事只有数得出来才算）。"""
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="TASK", at=NOW + dt.timedelta(minutes=1))
    before = (
        int(session.scalar(select(func.count()).select_from(Snapshot)) or 0),
        int(session.scalar(select(func.count()).select_from(EventLog)) or 0),
        int(session.scalar(select(func.count()).select_from(TaskCard)) or 0),
    )
    statuses_before = list(session.scalars(select(TaskCard.status).order_by(TaskCard.id)))

    restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert (
        int(session.scalar(select(func.count()).select_from(Snapshot)) or 0),
        int(session.scalar(select(func.count()).select_from(EventLog)) or 0),
        int(session.scalar(select(func.count()).select_from(TaskCard)) or 0),
    ) == before
    assert list(session.scalars(select(TaskCard.status).order_by(TaskCard.id))) == statuses_before


# ---------------------------------------------------------------- 三、可继续调度的最小状态

def test_resumable_is_the_intersection_with_the_set_the_caller_passed(session: Session, bus) -> None:
    from acp.orchestrate.nodes.dispatch import DISPATCHABLE

    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A, CARD_B])
    request_id = _request(session)
    _cards(session, request_id, [CARD_A], snapshot_id=row.id)
    _cards(session, request_id, [CARD_B], snapshot_id=row.id, status="DELIVERED")

    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    assert result.resumable(dispatchable=DISPATCHABLE) == (CARD_A,)
    assert result.resumable(dispatchable=frozenset({"READY", "HANDED_OFF", "REWORK"})) == (CARD_A,), (
        "借来的集合与抄来的集合同值时结果必须一致；这一格变红就说明有人把 `DISPATCHABLE` 抄了一份"
    )
    assert result.resumable(dispatchable=frozenset()) == (), "反向对照：空集合不许捞出卡来"


def test_the_restore_layer_reaches_neither_the_graph_nor_the_guard_layer() -> None:
    """`archive/restore.py` 一次都不 import 编排 / 把关 / 审批 / API / 适配器层 ⇒ 它**物理上**跑不了图。

    方向反着钉：不写"恢复代码里没有 invoke 语句"（一句 `getattr` 就绕过去了），而是掐依赖边。
    口径同 T108 的 `test_the_archive_layer_cannot_reach_the_guard_layer`。
    """
    tree = ast.parse((SRC_ROOT / "archive" / "restore.py").read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            parts = name.split(".")
            roots.add(parts[1] if parts[0] == "acp" and len(parts) > 1 else name)
    banned = {"guard", "approval", "orchestrate", "api", "adapters"} & roots
    assert not banned, f"restore.py 依赖了 {sorted(banned)} ⇒ MUST NOT 整链路重跑只剩注释在守"


def test_thread_values_uses_only_declared_intake_keys_and_never_the_split_products() -> None:
    """`thread_values()` 的键集 MUST ⊆ `IntakeState` 声明的键，且**不含 `drafts` / `links`**。

    那两个键是"某一次拆单的中间产物"（`state.py` 模块头的条件②），已落成 EN-02 / EN-07 两表；
    把它们塞回去 = 让恢复路径重新产出一遍拆单结果，那才是 FR-064 禁的整链路重跑。
    """
    from acp.orchestrate.state import IntakeState

    item = _restoration_stub()
    values = item.thread_values()

    assert set(values) <= set(IntakeState.__annotations__), (
        f"多出的键：{sorted(set(values) - set(IntakeState.__annotations__))} ⇒ state 形状之外凭空造了一个"
    )
    assert {"drafts", "links"}.isdisjoint(values)
    assert values["card_ids"] == [CARD_A, CARD_B] == values["card_order"]
    assert values["snapshot_id"] == item.snapshot_id and values["request_status"] == "INTAKE"


# ---------------------------------------------------------------- 四、回退动作本身落审计

def test_mark_rolled_back_is_the_only_writer_of_that_column_in_the_source_tree() -> None:
    """`rolled_back_at` 的写点恰一处：`archive/restore.py`。

    改前读数是**零处**：这一列自迁移 0003 就在，却从没被写过（T108 还专门把它写成 `None` 并留了
    一句"本轮写它 = 造一个到不了的态"）。T110 兑现它，同时把它收成一处。
    """
    sites: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "rolled_back_at":
                        sites.append(str(path.relative_to(SRC_ROOT).as_posix()))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "values" and "rolled_back_at" in ast.dump(node):
                    sites.append(f"{path.relative_to(SRC_ROOT).as_posix()}(update)")
    assert sorted(set(sites)) == ["archive/restore.py"], f"写点：{sorted(sites)}"


def test_mark_rolled_back_writes_the_timestamp_and_nothing_else(session: Session, bus) -> None:
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)
    events_before = int(session.scalar(select(func.count()).select_from(EventLog)) or 0)
    moment = NOW + dt.timedelta(minutes=30)

    written = restore.mark_rolled_back(session, result, now=moment)

    assert written.rolled_back_at == moment
    assert int(written.round_no) == 1 and written.ref == row.ref and list(written.task_card_ids) == [CARD_A]
    assert int(session.scalar(select(func.count()).select_from(EventLog)) or 0) == events_before, (
        "回退留痕的载体就是这一列；给 `event_log_kind` 加 `ROLLBACK` 是迁移单头改动，不在本轮"
    )


def test_a_second_rollback_overwrites_the_single_timestamp_and_says_so(session: Session, bus) -> None:
    """特征化用例：列只容一个时间戳 ⇒ 第二次回退**覆盖**第一次；"回退过几次"在 MVP-1 没有载体。

    这条不是"证明设计正确"，是把限制钉成可读的：哪天要留次数，就得加表或加 `event_log_kind` 域值
    （迁移单头改动 ⇒ 按硬停项先问 owner），届时这条会被有意改写而不是静默失真。
    """
    project_dir = bus.with_project(PROJECT)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, _request(session), [CARD_A], snapshot_id=row.id)
    result = restore.reconstruct(session, project_key=PROJECT, archive_root=project_dir)

    restore.mark_rolled_back(session, result, now=NOW + dt.timedelta(minutes=1))
    second = restore.mark_rolled_back(session, result, now=NOW + dt.timedelta(minutes=2))

    assert second.rolled_back_at == NOW + dt.timedelta(minutes=2)


def test_losing_the_row_between_read_and_write_is_refused_not_silently_skipped(session: Session) -> None:
    """`row_lost`：刚读到的点在同一会话里读不回来 ⇒ 拒，不"当没这回事"。"""
    with pytest.raises(restore.RestoreMisuse) as caught:
        restore.mark_rolled_back(session, _restoration_stub(), now=NOW)

    assert caught.value.code == "row_lost"


# ---------------------------------------------------------------- 五、装配侧：把执行态写回 thread

def test_a_wiped_thread_comes_back_at_the_gate_without_running_any_node(
    session: Session, checkpointer_conn, bus
) -> None:
    """场景 1 的正身：现场被 `chaos` 删空 → 恢复写回 → `next` 指向 `approval_gate`、卡一行都不多。

    一条用例同时钉三件：① 恢复走 `update_state`，**不执行节点**（实测读数在 `evidence/phase-09 §三`）；
    ② 回来的 `values` 是存档点那一轮的卡集合；③ `thread_was_empty=True` —— 在一份**活**现场上按回退
    键是另一回事，这条把两种情形分开了。
    """
    from acp.orchestrate import graph

    project_dir = bus.with_project(PROJECT)
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    request_id = _request(session)
    row = _real_point(session, project_dir, cards=[CARD_A, CARD_B])
    _cards(session, request_id, [CARD_A, CARD_B], snapshot_id=row.id)
    _event(session, card=CARD_A, kind="TASK", at=NOW + dt.timedelta(minutes=1))
    thread = claim_thread(checkpointer_conn, request_id)
    chaos.kill_graph_state(checkpointer_conn, thread)
    assert chaos.read_thread_rows(checkpointer_conn, thread).total == 0, "前置：现场真没了才算注入了"
    cards_before = int(session.scalar(select(func.count()).select_from(TaskCard)) or 0)

    recovery = recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=project_dir.parent, saver=saver, moment=NOW
    )

    assert recovery.next_nodes == (graph.APPROVAL_GATE,)
    assert recovery.thread_was_empty is True
    assert recovery.restoration.snapshot_id == row.id
    assert len(recovery.restoration.replayed) == 1
    assert int(session.scalar(select(func.count()).select_from(TaskCard)) or 0) == cards_before, (
        "恢复不产生新卡：FR-064 的那句 MUST NOT，在这一格是可数的"
    )
    assert session.get(Snapshot, row.id).rolled_back_at == NOW


def test_a_full_chain_rerun_on_the_recovery_path_fails_loudly(
    session: Session, checkpointer_conn, bus
) -> None:
    """把"MUST NOT 整链路重跑"从注释变成可跑的断言：**这条路上从头 invoke 会炸**。

    恢复用的图是 `OrchestrateDeps(splitter=None)` 装配的，而 `_split` 缺拆单器就抛
    `SplitNotConfigured` ⇒ 只要有人把恢复写成"重新跑一遍"，这条立刻红。
    正常推进那条链 MUST 带拆单器，由 T037 那批用例守，本文件不重复。
    """
    from acp.orchestrate import graph
    from acp.orchestrate.nodes.split import SplitNotConfigured

    project_dir = bus.with_project(PROJECT)
    request_id = _request(session)
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    recovery_graph = recover._build_recovery_graph(session, bus_root=project_dir.parent, saver=saver)
    config = cp.thread_config(kind=graph.GATE_KIND, feature_request_id=request_id)
    claim_thread(checkpointer_conn, request_id)

    with pytest.raises(SplitNotConfigured):
        recovery_graph.invoke({"feature_request_id": request_id}, config)


def test_the_restore_writes_exactly_one_checkpoint_row(session: Session, checkpointer_conn, bus) -> None:
    """一次恢复 = **一个** checkpoint；再恢复一次 = 两个点，而卡与留痕的行数都不再变。

    "幂等"完整两场景在 `test_recovery_idempotent.py`（T112）；这一条只管"写回这一步不多写"。
    """
    project_dir = bus.with_project(PROJECT)
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    request_id = _request(session)
    row = _real_point(session, project_dir, cards=[CARD_A])
    _cards(session, request_id, [CARD_A], snapshot_id=row.id)
    thread = claim_thread(checkpointer_conn, request_id)
    events_before = int(session.scalar(select(func.count()).select_from(EventLog)) or 0)

    first = recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=project_dir.parent, saver=saver, moment=NOW
    )
    assert chaos.read_thread_rows(checkpointer_conn, thread).counts[2][1] == 1
    assert first.next_nodes and first.resumable == (CARD_A,)

    recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=project_dir.parent, saver=saver, moment=NOW
    )
    assert chaos.read_thread_rows(checkpointer_conn, thread).counts[2][1] == 2
    assert int(session.scalar(select(func.count()).select_from(EventLog)) or 0) == events_before, (
        "恢复不写 EN-14：`rolled_back_at` 一列就是回退的载体，多写一条流水会顶掉「一次动作一个载体」"
    )


def _intake_state():
    from acp.orchestrate.state import IntakeState

    return IntakeState


# ---------------------------------------------------------------- 六、CLI 面

def test_the_three_paths_are_mutually_exclusive() -> None:
    for argv in (
        ["recover", "--from-file-bus", PROJECT, "--from-snapshot", restore.LAST],
        ["recover", "--from-snapshot", restore.LAST, "--verify-consistency", PROJECT],
        ["recover", "--from-file-bus", PROJECT, "--verify-consistency", PROJECT],
    ):
        with pytest.raises(SystemExit) as caught:
            recover.main(argv)
        assert caught.value.code == 2

    with pytest.raises(SystemExit) as none:
        recover.main(["recover"])
    assert none.value.code == 2, "一个旗标都不给也不行：默认走哪条就是产品在替操作者猜"


def test_snapshot_mode_refuses_without_a_project_before_touching_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert recover.main(["recover", "--from-snapshot", restore.LAST]) == 2
    out = capsys.readouterr().out
    assert "--project" in out and "RECOVER" not in out, "usage 档不该出现任何一条现场读数"


def test_snapshot_mode_without_dsn_reports_the_variable_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    code = recover.main(["recover", "--from-snapshot", restore.LAST, "--project", PROJECT])
    out = capsys.readouterr().out

    assert code == 3, "环境缺位不是「恢复不成立」：1 说的是现场对不上，3 说的才是连不上"
    assert DSN_ENV in out and "://" not in out and "RECOVER OK" not in out


def test_the_bus_side_does_not_import_the_orchestration_stack() -> None:
    """`import acp.recover` 之后 `sys.modules` 里 MUST NOT 有 langgraph / psycopg。

    这条看着像洁癖，其实是 FR-065 的定义性约束：那条路径**就是**"PG 不可用时还能捞回三元组"。
    模块级依赖编排栈，等于把"数据库没了连文件都读不出来"做成新的故障面。代价点名：
    `restore_execution_state` 缺依赖时抛 `ImportError`，由 CLI 按类名报出去，不影响总线侧。

    **env 走 `_child_env()`**（完整继承 + 只覆写 `PYTHONPATH`）：这条要量的是"重模块数 = 0"，
    极简 env 会让子进程在 Windows 上崩在 import 阶段，于是红因与要证的事无关（P2-1；
    为什么不能图省事手写 env，见 `_child_env()` 的 docstring）。
    """
    script = (
        "import sys, acp.recover;"
        "print(sum(1 for m in sys.modules if m.split('.')[0] in {'psycopg','langgraph','langchain'}))"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_child_env(),
    )
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.strip() == "0", f"总线侧拉起了编排栈：{done.stdout.strip()} 个模块"


def test_the_child_sees_the_parent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉住 `_child_env()` 这件事本身：子进程 MUST 看得见父进程的环境，`PYTHONPATH` 由判据覆写。

    为什么单开一条而不是在上面那条里顺手断言：上面那条的 `returncode == 0` 只说明"这次没崩"，
    而"崩"的因是 env 少了一格 —— 少一格这件事在 POSIX 上不会暴露（本机实测：Anaconda 3.13.9 的
    `import asyncio` 在无 `SYSTEMROOT` 下照样 rc=0），所以必须用**与平台无关的哨兵**把它钉成一条判据：
    谁把 `_child_env()` 换回手写极简 env，这条当场红（POSIX 靠哨兵变量红，Windows 上两条都红）。
    """
    monkeypatch.setenv(ENV_PROBE, "keep-me")
    script = (
        "import os, sys;"
        f"sys.stdout.write('|'.join([os.environ.get({ENV_PROBE!r}, ''), "
        "os.environ.get('SYSTEMROOT', ''), os.environ.get('PYTHONPATH', '')]))"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_child_env(),
    )

    assert done.returncode == 0, done.stderr[-300:]
    parts = done.stdout.strip().split("|")
    assert len(parts) == 3, f"子进程输出的形状不对（拿不到三格读数）：{done.stdout!r}"
    probe, systemroot, pythonpath = parts
    assert probe == "keep-me", "子进程丢了父进程的环境 ⇒ 这是手写极简 env 的形态，Windows 上会恒红"
    assert pythonpath == str(SRC_ROOT.parent), "判据覆写的那一格必须指向被量的这棵树（否则量的不是它）"
    if os.name == "nt":
        assert systemroot, "Windows 上没有 SYSTEMROOT ⇒ Winsock 初始化会失败（P2-1 的那一发）"
    else:
        assert systemroot == "", "POSIX 上这一格本来就该是空的：不许把它当成通过条件"


def test_the_object_check_adds_no_new_git_subcommand() -> None:
    """`object_exists()` 用的是名单里已有的 `rev-parse` ⇒ 恢复路径**没有**放宽 git 面。

    与 T109 那条"边界自检不扩面"是一对：恢复是第二个想调用 git 的地方，最容易顺手来一句
    `git cat-file` / `git log`。
    """
    assert cadence.ALLOWED_SUBCOMMANDS == ("add", "commit", "rev-parse")
    assert cadence.VERIFY_COMMIT_ARGV[0] in cadence.ALLOWED_SUBCOMMANDS
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], cwd: Path) -> cadence.GitResult:
        calls.append(argv)
        return cadence.GitResult(code=0, stdout="anything")

    assert cadence.object_exists(Path("/unused"), "a" * 40, runner=runner) is True
    assert calls == [(*cadence.VERIFY_COMMIT_ARGV, f"{'a' * 40}^{{commit}}")]


def test_a_malformed_object_name_is_refused_before_it_reaches_git() -> None:
    """`object_exists` 先看形状再问 git。诱饵选 `"a"` 与带空格的串：前者会让 git 报一个**看似
    "存档丢了"**的非零码，后者会把 argv 拆歪 —— 两种都必须红在形状这一道，而不是红在退出码上。
    """
    for bad in ("a", "a" * 41, "dead beef", "", "A" * 40, "f"*40 + "; rm -rf /"):
        with pytest.raises(cadence.ArchiveMisuse) as caught:
            cadence.object_exists(
                Path("/unused"), bad, runner=lambda argv, cwd: cadence.GitResult(code=0)
            )
        assert caught.value.code == "sha_shape"


def test_a_nonexistent_object_reports_absent_rather_than_raising(session: Session, bus) -> None:
    """`object_exists` 对"没有"回 `False` 而不是抛：这里问的是有没有，不是 git 坏没坏。

    反向对照在同一条里：同一个桩在 `code=0` 时必须回 `True`（见
    `test_the_object_check_adds_no_new_git_subcommand`），否则"恒 False"也能让第五节那几条绿着。
    """
    project_dir = bus.with_project(PROJECT)
    assert cadence.object_exists(project_dir, ABSENT_SHA) is False
    point = cadence.commit_archive_point(
        project_dir,
        phase=cadence.OPEN_PHASE,
        round_no=1,
        project_key=PROJECT,
        task_card_ids=[CARD_A],
    )
    assert cadence.object_exists(project_dir, point.sha) is True
