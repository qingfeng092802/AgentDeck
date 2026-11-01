"""T081 / T082：`HandOff` 换人与上限（FR-026 / FR-027｜R-11，宪法「三」的验收轮次与换人上限）。

档位：`tests/guards/`（fast 一档）里的混合文件 —— 与上限有关的两条关系是纯读表，
其余每条都要真 PG（换人次数那条 CHECK 在库里，不在库里读不到它），各挂 `skipif`。
**每条 PG 读数都必须注明"带 `.env`"**。

本文件问的四件事，各自只有一种坏法能把它撞红：
1. **换人真的换了**：新执行者 ≠ 失败者，且次数 +1、状态机走一格（`ARTIFACT_INVALID → HANDED_OFF`）。
2. **上限有两条，不是一条**：`handoff_count ≤ 1` 是动作数，"涉及工具 ≤ 2"是去重后的身份数。
   两者在"正常换人"的轨迹上同时到界，但**各自都有只到自己那条界的形状**，两个方向各一条用例：
   `..._agents_bound_can_fire_while_the_count_bound_is_still_open`（人工覆盖两次 ⇒ 工具数到界、次数为 0）与
   `..._count_bound_alone_can_fire_while_the_agents_bound_is_still_open`（账本上次数已到界、历史面只剩一支）。
   只实现一条的话，对应那条就红 —— 前一条红在"换成了第三支工具"，后一条红在被库面 CHECK 顶成一次崩溃。
3. **超限是一条业务出口，不是一次崩溃**：`ESCALATED` + 留痕里带原因；
   执行者与次数都不许被顺手改动（人工出口是"READY / REJECTED"，不是"由我再挑一个"）。
4. **库面 CHECK 也真的在**：应用层那道判据之外，直接把 `handoff_count` 写成 2 必须被 PG 拒
   —— 只测应用层的写法，将来有人删掉那段判断，库面那一道还活着却没人知道它活着。

反向对照（缺了就等于判据恒红）：
- "第 2 次必升人工"配一条"第 1 次正常换人"；
- "候选里没有可替代的 ⇒ 升级"配一条"给一支可用的替补 ⇒ 换人成功"；
- "返工（`CHANGES_REQUESTED` / `REWORK`）不算换人"配一条"同一次失败连着调用两次 ⇒ 第二次升人工"
  （次数被第一次吃掉，界在两条上限上，而不是"同一件事被记了两次账"）。
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import re
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.api import tasks as tasks_api
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import dispatch as dp
from acp.orchestrate.nodes import escalation as ep
from acp.orchestrate.nodes import handoff as hp
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.runtime.slot_manager import InProcessSlotManager

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 3, 55, tzinfo=dt.timezone.utc)
FAILED = "qoder"
BACKUP = "workbuddy"
THIRD = "doubao"


def candidate(
    agent_id: str,
    *,
    capabilities=("frontend_coding", "repository_editing", "test_execution"),
    tier: int = 2,
    load: int = 0,
    max_parallel: int = 3,
) -> sw.WorkerCandidate:
    """默认 `tier=2`：卡上的 `difficulty` 也是 2 ⇒ 同档 = 键②最优，谁都不靠档次先出局。"""
    return sw.WorkerCandidate(
        agent_id=agent_id,
        capabilities=frozenset(capabilities),
        tier=tier,
        cost_band="MID",
        current_load=load,
        max_parallel=max_parallel,
        historical_success_rate=Decimal("0.5"),
        success_rate_sample=4,
    )


def pool(*, backup_busy: bool = False, only_failed_and_backup: bool = False) -> list[sw.WorkerCandidate]:
    """三支候选：失败者、替补、第三支。

    第三支刻意给 `tier=1`（偏离难度 2 更远）⇒ 只要它在场，排序就必然选替补而不是它。
    这样"选到谁"这件事在本文件里只有一个来源（排序规则），不会因为我改了候选顺序而变。
    替补可以"忙到接满"，用来逼出"无可替代"那一支路。
    """
    out = [
        candidate(FAILED),
        candidate(BACKUP, load=3 if backup_busy else 0),
    ]
    if not only_failed_and_backup:
        out.append(candidate(THIRD, tier=1))
    return out


def raises_code(exc_type, code: str, call) -> str:
    with pytest.raises(exc_type) as caught:
        call()
    assert caught.value.code == code, f"代号是 {caught.value.code!r}，期望 {code!r}：{caught.value}"
    return str(caught.value)


# —————————————————————————— 一、两条上限的关系是读出来的，不是记下来的


def test_the_two_limits_are_related_by_a_relationship_not_two_constants():
    """"换人次数 ≤ 1"与"涉及工具 ≤ 2"是同一条界的两个面：**工具数 = 次数上限 + 1**（首次执行占一支）。

    写成 `MAX_AGENTS_INVOLVED == MAX_HANDOFF_COUNT + 1` 这条关系，改一侧忘改另一侧就会红；
    写成两个各自 `== 1` / `== 2` 的常量断言，则永远是绿的（它只是在复述我抄的数）。
    """
    assert hp.MAX_AGENTS_INVOLVED == hp.MAX_HANDOFF_COUNT + 1, (
        hp.MAX_HANDOFF_COUNT,
        hp.MAX_AGENTS_INVOLVED,
    )


def test_the_application_limit_agrees_with_the_database_check():
    """应用层的 `MAX_HANDOFF_COUNT` 必须等于 EN-02 上那条 CHECK 里的数 —— 两处现在是两个载体。

    读的是表上的 CHECK 原文，数值靠正则现取；库里那条被人改成 `<= 2` 而代码没跟（或反过来），
    这条立刻点名差额。写成两个常量互相比对就没有这个作用：它只复述我抄的数。

    取法是 `__table__.constraints` + `isinstance(CheckConstraint)`，不是 `check_constraints`：
    SQLAlchemy 2.1.4 的 `Table` **没有** `check_constraints` 这个属性（实测 `AttributeError`，
    它是 `Inspector` 那一侧的方法名）。那条 CHECK 登记在 `models_core.py:79`。
    """
    checks = [
        c
        for c in TaskCard.__table__.constraints
        if isinstance(c, CheckConstraint) and "handoff_count" in str(c.sqltext)
    ]
    assert len(checks) == 1, [str(c.sqltext) for c in checks]
    sqltext = str(checks[0].sqltext)
    digits = re.findall(r"\d+", sqltext)
    assert len(digits) == 1, f"这条 CHECK 里不止一个数，正则取哪个都不对：{sqltext}"
    assert int(digits[0]) == hp.MAX_HANDOFF_COUNT, (
        f"库面写的是 {sqltext}，应用层常量是 {hp.MAX_HANDOFF_COUNT}"
    )


def test_only_the_failure_state_triggers_a_handoff_rework_is_a_different_path():
    """`CHANGES_REQUESTED` / `REWORK` 是同一执行者返工，走换人路径会把审查意见计成换人次数。"""
    assert hp.TRIGGER_STATES == frozenset({"ARTIFACT_INVALID"}), hp.TRIGGER_STATES
    assert "REWORK" not in hp.TRIGGER_STATES and "CHANGES_REQUESTED" not in hp.TRIGGER_STATES


# —————————————————————————— 二、真 PG：换人、上限、升级


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


def seed_agent(session, agent_id: str) -> None:
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=3,
            cost_band="MID",
            max_parallel=3,
            current_load=0,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=True,
        )
    )


def select_for(session, request_id, card, agents: tuple[str, ...]) -> None:
    """走生产写者给这张卡定下首位执行者（并落一行 `SELECTION`）—— 换人判定的历史面就在那些行里。

    这里**只给一支**候选时结果就是那支；给两支以上时结果由排序规则定（本文件的形状让替补必然赢，
    免得"选到谁"变成用例里隐含的第二个排序实现）。
    """
    ranking = sw.rank_candidates(
        sw.task_need_of(card), [candidate(agent_id) for agent_id in agents]
    )
    sw.apply_selection(
        session,
        ranking,
        feature_request_id=request_id,
        actor_agent_id=sw.AUTOMATED_ACTOR,
        created_at=NOW,
    )


@pytest.fixture
def failed_card(session) -> tuple[FeatureRequest, TaskCard]:
    """一张已经跑过、被门禁判为 `ARTIFACT_INVALID` 的卡，执行者是 `qoder`。"""
    for agent_id in (FAILED, BACKUP, THIRD):
        seed_agent(session, agent_id)
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
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="ARTIFACT_INVALID",
        snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    # 首位执行者只给一支候选：`select_for` 三支同分时末位 `agent_id` 会赢（键⑤是"小者优先"），
    # 那会让这张卡的"失败者"变成 doubao，与下面所有用例里的 `FAILED` 对不上。
    # 形状不变、只是把它定死：本文件要测的是换人，不是再测一遍排序。
    select_for(session, request.id, card, (FAILED,))
    session.refresh(card)
    assert card.executor_agent_id == FAILED, card.executor_agent_id
    return request, card


def audits(session, task_id: str, kind: str) -> list[EventLog]:
    return list(
        session.scalars(
            select(EventLog)
            .where(EventLog.entity_id == task_id, EventLog.kind == kind)
            .order_by(EventLog.id)
        ).all()
    )


@needs_pg
def test_the_first_handoff_moves_to_an_alternative_agent_and_counts_once(session, failed_card):
    request, card = failed_card
    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="产物 0 字节，门禁判 invalid",
        candidates=pool(),
        created_at=NOW,
    )
    assert outcome.escalated is False
    assert outcome.from_agent == FAILED and outcome.to_agent == BACKUP, outcome
    assert outcome.handoff_count_after == 1
    # 这里写状态机原文而不是 `hp.LANDED`：拿常量验常量是恒真的（把 LANDED 改成 "READY"
    # 或 "DONE" 时，`test_the_landed_card_can_be_dispatched_again` 会因为 READY 恰好可派而照绿，
    # "换过人的卡停在派发看不见的态"那个缺陷就漏了）。常量与原文两处都钉。
    assert outcome.status_after == "HANDED_OFF" == hp.LANDED, outcome.status_after
    assert outcome.agents_involved == (FAILED, BACKUP), outcome.agents_involved
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.executor_agent_id == BACKUP and fresh.handoff_count == 1
    assert fresh.status == "HANDED_OFF"

    handed = audits(session, card.id, "TASK")
    assert len(handed) == 1, [row.payload for row in handed]
    payload = handed[0].payload
    assert payload["action"] == "handed_off"
    assert payload["reason"] == "产物 0 字节，门禁判 invalid", "FR-026 要的'记录换人原因'——理由必须原文可查"
    assert (payload["from_agent"], payload["to_agent"]) == (FAILED, BACKUP)
    assert payload["handoff_count"] == 1


@needs_pg
def test_the_second_handoff_escalates_instead_of_bringing_in_a_third_agent(session, failed_card):
    """FR-027 + "MUST NOT 三工具循环"：第二次换人一律升人工，且**不再选人**。"""
    request, card = failed_card
    first = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="第一次：产物不达标",
        candidates=pool(),
        created_at=NOW,
    )
    assert first.to_agent == BACKUP
    session.get(TaskCard, (request.id, card.id)).status = "ARTIFACT_INVALID"
    session.flush()

    second = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="第二次：替补也没过",
        candidates=pool(),
        created_at=NOW,
    )
    assert second.escalated is True and second.to_agent is None, second
    assert second.status_after == hp.ESCALATED
    assert second.handoff_count_after == 1, "升级不许把次数写成 2（库里那条 CHECK 也会拒）"
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.executor_agent_id == BACKUP, "升级时不许顺手再换一次人：人工出口是 READY / REJECTED"

    rows = audits(session, card.id, "TASK")
    assert [row.payload["action"] for row in rows] == ["handed_off", "escalated"], rows
    assert "替补也没过" in rows[1].payload["reason"]
    assert rows[1].payload["handoff_count"] == 1


@needs_pg
def test_a_third_handoff_is_refused_before_any_candidate_is_ranked(session, failed_card):
    """超限那次**连排序都不该发生**：`SELECTION` 行数不变。

    排序发生过的表现是"留痕里多了一条'为什么选它'，而它根本没被采用"——
    给用户看的理由与库里的决定不一致，比不排更糟。
    """
    request, card = failed_card
    for reason in ("第一次", "第二次"):
        hp.handoff(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reason=reason,
            candidates=pool(),
            created_at=NOW,
        )
        session.get(TaskCard, (request.id, card.id)).status = "ARTIFACT_INVALID"
        session.flush()
    before = len(audits(session, card.id, "SELECTION"))
    third = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="第三次",
        candidates=pool(),
        created_at=NOW,
    )
    assert third.escalated is True
    assert len(audits(session, card.id, "SELECTION")) == before, "超限那次仍然产生了一条选人留痕"


@needs_pg
def test_the_agents_bound_can_fire_while_the_count_bound_is_still_open(session, failed_card):
    """两条上限真的不等价：这张卡**一次人都没换过**（`handoff_count = 0`），但历史上已经涉及两支工具。

    形状是"人工覆盖过两次执行者"留下的：两次 `SELECTION` 行、零次换人动作。
    此时换人应当被**工具数**那条界挡住，理由里点名"涉及"，而不是"次数"也不是"没有替补"。
    """
    request, card = failed_card
    select_for(session, request.id, card, (BACKUP,))
    session.refresh(card)
    assert card.handoff_count == 0
    assert hp.agent_history(session, feature_request_id=request.id, task_id=card.id) == (FAILED, BACKUP)

    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="第三次身份，但一次换人动作都没记",
        candidates=pool(),
        created_at=NOW,
    )
    assert outcome.escalated is True, outcome
    assert "涉及" in outcome.reason and "2" in outcome.reason, outcome.reason
    assert outcome.handoff_count_after == 0


@needs_pg
def test_the_count_bound_alone_can_fire_while_the_agents_bound_is_still_open(session, failed_card):
    """上一条的另一半：只到**次数**那条界的形状 —— 删掉应用层那道次数判断，这条必须红。

    两条上限的载体不同（`handoff_count` 是 EN-02 上的列，"涉及工具"是 EN-14 的 `SELECTION` 行），
    所以库允许"次数已用到界、历史面上却只剩一支工具"这种账本（首次换人那行留痕缺失 / 旧数据导入）。
    此时工具数 = 1 < 2 挡不住，只有次数那道界挡得住；
    它没挡的话后面会发生什么本用例一并量出来：换人成立 → 次数写成 2 → 库面 CHECK 把业务出口
    变成一次 `IntegrityError`（`handoff.py` 模块头那句"库面拦的表现不是升级人工"）。
    """
    request, card = failed_card
    card.handoff_count = 1
    session.flush()
    assert hp.agent_history(session, feature_request_id=request.id, task_id=card.id) == (FAILED,)

    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="账本上次数已用到界，历史面只剩一支工具",
        candidates=pool(),
        created_at=NOW,
    )
    assert outcome.escalated is True, outcome
    assert outcome.to_agent is None
    assert len(outcome.agents_involved) == 1, (
        "界是从**次数**上到的，不是从工具数上到的：工具数此刻还是 1，"
        "所以这条用例区分得开两条上限（工具数那条一旦先响，这里就变成 2）"
    )
    assert "账本上次数已用到界" in outcome.reason, "FR-026：本次原因必须原文可查"
    assert outcome.handoff_count_after == 1
    fresh = session.get(TaskCard, (request.id, card.id))
    assert (fresh.handoff_count, fresh.status, fresh.executor_agent_id) == (1, hp.ESCALATED, FAILED)
    escalated = audits(session, card.id, "TASK")[-1].payload
    assert escalated["agents_involved"] == [FAILED] and escalated["handoff_count"] == 1


@needs_pg
def test_no_replacement_available_escalates_with_the_exclusion_list_rather_than_vanishing(session, failed_card):
    """FR-025 的换人面：没有可用替补时升级，并把"为什么每个人都出局"写进理由。

    形状：替补接满（`current_load >= max_parallel`）+ 第三支根本不在候选里 ⇒ 排序结果空。
    静默丢弃的表现是"卡停在 `ARTIFACT_INVALID`，谁也没被叫来"。
    """
    request, card = failed_card
    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="门禁不过，而替补都接满了",
        candidates=pool(backup_busy=True, only_failed_and_backup=True),
        created_at=NOW,
    )
    assert outcome.escalated is True and outcome.to_agent is None
    assert "没有可替代" in outcome.reason, outcome.reason
    # Q7 乙（owner 2026-10-17 裁）那句"席位占用"是**有条件**的：这一形没人被席位挡（候选是自己接满/缺席），
    # 所以理由里不许出现那句 —— 它是"措辞写死成恒真"的反向证人（对照 `test_run_execute.py` 的 ②c）。
    assert "席位占用" not in outcome.reason, f"没有撞岗却写着席位占用：{outcome.reason!r}"
    assert any(
        row.payload["action"] == "escalated" and "max_parallel" in str(row.payload)
        for row in audits(session, card.id, "TASK")
    ), [row.payload for row in audits(session, card.id, "TASK")]


@needs_pg
def test_a_usable_backup_makes_the_same_shape_go_through(session, failed_card):
    """上一条的 GREEN 一侧：同一形状只把替补从"接满"改成"空闲" ⇒ 换人成立，不升级。"""
    request, card = failed_card
    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="门禁不过",
        candidates=pool(backup_busy=False, only_failed_and_backup=True),
        created_at=NOW,
    )
    assert outcome.escalated is False and outcome.to_agent == BACKUP, outcome


@needs_pg
def test_handoff_refuses_a_rework_state_and_leaves_the_count_alone(session, failed_card):
    """审查提了修改意见（`CHANGES_REQUESTED`）不算换人：状态与次数都不许动。"""
    request, card = failed_card
    card.status = "CHANGES_REQUESTED"
    session.flush()
    raises_code(
        hp.HandoffMisuse,
        "state_not_handed_off",
        lambda: hp.handoff(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reason="审查要求返工",
            candidates=pool(),
            created_at=NOW,
        ),
    )
    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.handoff_count == 0 and fresh.status == "CHANGES_REQUESTED"


@needs_pg
def test_handoff_refuses_an_empty_reason_because_the_audit_would_be_uncheckable(session, failed_card):
    request, card = failed_card
    raises_code(
        hp.HandoffMisuse,
        "reason_required",
        lambda: hp.handoff(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reason="   ",
            candidates=pool(),
            created_at=NOW,
        ),
    )


@needs_pg
def test_handoff_refuses_a_card_that_never_had_an_executor(session, failed_card):
    """没执行者 = 首次派发就失败，那是另一件事（不该被计成"换人 1 次"）。"""
    request, card = failed_card
    card.executor_agent_id = None
    session.flush()
    raises_code(
        hp.HandoffMisuse,
        "no_executor_to_replace",
        lambda: hp.handoff(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reason="没人跑过",
            candidates=pool(),
            created_at=NOW,
        ),
    )


@needs_pg
def test_the_database_still_refuses_a_count_of_two_even_if_the_node_is_edited(session, failed_card):
    """库面 CHECK 是第二道：绕过节点直接写 2 必须被 PG 拒。

    绕开 `handoff()` 用 `update()` 写 —— 正是"将来有人把应用层那段判断删了"的形状。
    """
    request, card = failed_card
    # `update()` 走的是 Core 语句，约束违反在 execute 那一刻就回来（不是等 flush）⇒ 两条都包在raises 里。
    with pytest.raises(IntegrityError, match="handoff_count_within_limit"):
        session.execute(
            update(TaskCard)
            .where(TaskCard.feature_request_id == request.id, TaskCard.id == card.id)
            .values(handoff_count=2)
        )
        session.flush()


@needs_pg
def test_the_landed_card_can_be_dispatched_again(session, failed_card):
    """换人之后那张卡**必须有回来的入口**：状态机写的是 `HANDED_OFF ──▶ RUNNING（换人≤1）`。

    只认 `READY` 的派发会把换过人的卡永久停在 `HANDED_OFF` —— 这条用例就是把那条边接起来读一遍。
    """
    request, card = failed_card
    hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="门禁不过",
        candidates=pool(),
        created_at=NOW,
    )
    slots = InProcessSlotManager(
        per_agent_max_parallel={agent_id: 3 for agent_id in (FAILED, BACKUP, THIRD)}, platform_limit=3
    )
    lease = dp.dispatch(
        session, feature_request_id=request.id, task_id=card.id, slots=slots, created_at=NOW
    )
    assert lease.agent_id == BACKUP and lease.card_id == card.id
    assert session.get(TaskCard, (request.id, card.id)).status == "RUNNING"


# ---------------------------------------------------------------- 六、ESCALATED 之后的人工出口（T086 前置）
#
# 上面那些用例证到"卡升进了 `ESCALATED`"为止。`data-model.md:303` 给它写的下一跳是
# `ESCALATED ──人工──▶ READY / REJECTED`，Phase 6 结束时那条边**没有任何实现**，卡升上去就是死路
# （owner 2026-10-14 裁待裁 6 ⇒ Phase 7 第一条）。这一节测的出口 = `orchestrate/nodes/escalation.py`。

HUMAN = "owner-1"


@pytest.fixture
def escalated_card(session, failed_card) -> tuple[FeatureRequest, TaskCard]:
    """用**真生产者**把卡送进 `ESCALATED`（次数已到界，再换人必升人工），不手改状态列。

    为什么不直接 `card.status = "ESCALATED"`：那样这条边是否可达就永远不进判据，
    出口用例会跟着"进入这一态的迁移"一起坏掉而无人报警。
    """
    request, card = failed_card
    session.execute(
        update(TaskCard)
        .where(TaskCard.feature_request_id == request.id, TaskCard.id == card.id)
        .values(handoff_count=hp.MAX_HANDOFF_COUNT)
    )
    session.flush()
    session.refresh(card)
    assert card.handoff_count == hp.MAX_HANDOFF_COUNT, card.handoff_count
    outcome = hp.handoff(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reason="第二次失败",
        candidates=pool(),
        created_at=NOW,
    )
    assert outcome.escalated is True, outcome
    session.refresh(card)
    assert card.status == "ESCALATED", card.status
    return request, card


@needs_pg
def test_the_human_exit_to_ready_keeps_the_executor_and_spends_no_handoff(session, escalated_card):
    """人选了 `READY`：卡回到可派发态，**执行者不动**、**换人次数不涨**（那是两个不同的决定）。"""
    request, card = escalated_card
    before = (card.executor_agent_id, card.handoff_count)
    outcome = ep.resolve_escalation(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        decision="READY",
        principal=HUMAN,
        created_at=NOW,
    )
    session.refresh(card)
    assert (outcome.status_after, card.status) == ("READY", "READY")
    assert (card.executor_agent_id, card.handoff_count) == before, (
        f"人工出口顺手改了执行者或次数：{before} → {(card.executor_agent_id, card.handoff_count)}"
    )


@needs_pg
def test_the_other_human_exit_is_rejected_and_is_a_real_transition(session, escalated_card):
    """两个出口都得真的能走（只实现一个的话，另一条人工边仍然是死路）。"""
    request, card = escalated_card
    outcome = ep.resolve_escalation(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        decision="REJECTED",
        principal=HUMAN,
        created_at=NOW,
    )
    session.refresh(card)
    assert outcome.decision == "REJECTED" == card.status


@needs_pg
def test_a_card_that_is_not_escalated_cannot_use_this_exit(session, failed_card):
    """反向：卡还在 `ARTIFACT_INVALID`（没升过级）⇒ 出口拒。每条状态机边只有一个来源态，从别处进去就没人判过合法性。"""
    request, card = failed_card
    message = raises_code(
        ep.EscalationMisuse,
        "state_not_escalated",
        lambda: ep.resolve_escalation(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            decision="READY",
            principal=HUMAN,
            created_at=NOW,
        ),
    )
    assert "ARTIFACT_INVALID" in message, message


@needs_pg
def test_a_decision_outside_the_state_machines_own_line_is_refused(session, escalated_card):
    """出口取值来自状态机原文，不是模块里那份字面量 ⇒ 拿一个"态名合法但不是出口"的值必须被拒。"""
    request, card = escalated_card
    raises_code(
        ep.EscalationMisuse,
        "exit_not_allowed",
        lambda: ep.resolve_escalation(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            decision="REWORK",
            principal=HUMAN,
            created_at=NOW,
        ),
    )


def test_the_exit_targets_are_read_from_the_document_line_both_ways(tmp_path):
    """判据②式的**双向**差集：文档那条边的右侧 ↔ 模块那份兜底字面量，谁多谁少都红。

    配一条开火对照（不动真文档，改 tmp 副本）：把 `REJECTED` 从那一行删掉 ⇒ 解析结果只剩 `READY`。
    不配这条的话，"解析"整段失灵（返回写死的两个值）时上面的相等断言也照样绿。
    """
    from acp.persistence.states import data_model_default

    declared = ep.exit_targets_in_doc()
    assert set(declared) == set(ep.EXIT_TARGETS), (declared, ep.EXIT_TARGETS)
    assert len(declared) == len(ep.EXIT_TARGETS)

    text = data_model_default().read_text(encoding="utf-8")
    line = ep.exit_line_in_doc()
    assert "REJECTED" in line, line
    # 开火对照先证明**这一枪真的打出去了**：`replace` 没命中时副本与原文逐字相同，
    # 那种"改坏了的文档"其实没改坏，下面的断言就会拿着真文档的值通过 —— 恒真的对照比没有对照更糟。
    broken_text = text.replace(line, line.replace("REJECTED", "").rstrip(" /"), 1)
    assert broken_text != text, "这一处替换没命中 ⇒ 开火对照是空的"
    broken = tmp_path / "data-model.md"
    broken.write_text(broken_text, encoding="utf-8")
    assert ep.exit_targets_in_doc(broken) == ("READY",), ep.exit_targets_in_doc(broken)


@needs_pg
def test_the_decision_path_consults_the_parsed_line_not_the_module_constant(session, escalated_card, monkeypatch):
    """把"允许的出口"这一份权威换成只含 `READY` 的解析结果 ⇒ `REJECTED` 必须当场被拒。

    这一条存在是因为上一条比的是两个集合相等：如果 `resolve_escalation()` 实际读的是模块里那份
    `EXIT_TARGETS` 而不是解析结果，集合相等照样成立 —— 那就是"拿自己的常量比自己"的恒等式形状。
    """
    request, card = escalated_card
    monkeypatch.setattr(ep, "exit_targets_in_doc", lambda path=None: ("READY",))
    raises_code(
        ep.EscalationMisuse,
        "exit_not_allowed",
        lambda: ep.resolve_escalation(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            decision="REJECTED",
            principal=HUMAN,
            created_at=NOW,
        ),
    )


@needs_pg
def test_the_audit_line_names_the_human_as_its_subject(session, escalated_card):
    """留痕的主语必须是**那个人**，不是 `orchestrator`：这条边的全部意义是"有人做过判断"，
    写成自动主体就把人工决定伪装成系统行为（与 `dispatch` / `handoff` 用 `AUTOMATED_ACTOR` 相反）。"""
    request, card = escalated_card
    ep.resolve_escalation(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        decision="READY",
        principal=HUMAN,
        created_at=NOW,
        reason="补齐依赖后重试",
    )
    rows = [
        row
        for row in audits(session, card.id, "TASK")
        if (row.payload or {}).get("node") == ep.NODE
    ]
    assert len(rows) == 1, [row.payload for row in rows]
    row = rows[0]
    assert row.actor_agent_id == HUMAN, row.actor_agent_id
    assert row.payload["action"] == "human_exit"
    assert row.payload["prior_status"] == "ESCALATED"
    assert row.payload["decision"] == "READY"
    assert row.payload["reason"] == "补齐依赖后重试"


@needs_pg
def test_an_exit_without_a_subject_is_refused(session, escalated_card):
    """缺 `principal` ⇒ 拒。留下的痕没人认领，比不处理更糟（它看起来像处理过了）。"""
    request, card = escalated_card
    raises_code(
        ep.EscalationMisuse,
        "principal_required",
        lambda: ep.resolve_escalation(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            decision="READY",
            principal="  ",
            created_at=NOW,
        ),
    )
    session.refresh(card)
    assert card.status == "ESCALATED", "拒了却还是把卡移出了升级态"


# ---------------------------------------------------------------- 待裁 ⑫：人工出口的独立路由
# （owner 2026-10-14 原话："escalation.py 已做，与 PATCH 的语义不同" ⇒ 公开面 = `POST /tasks/{id}/escalation`，
# 契约在 `contracts/agent-protocol.md §1.4`。上面那几节测的是**决定本体**，这一节测的是**公开面**：
# 决定对了但路由没开，使用者仍然走不出 `ESCALATED` —— 那正是 owner 点名的"全流程唯一死路"。）

ESCALATION_BODY_KEYS = {"feature_request_id", "decision", "principal", "reason"}


def build_app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_api.router)
    # 每次请求一个新会话，绑在**同一条连接**上（2026-10-28）。
    # 端点现在一定会 `close()` 它借到的会话（`api/tasks.py::_store` 的 finally），
    # 把夹具自己那个交出去，第一发请求之后本文件的回读判据就全哑了：
    # `session.refresh(card)` 会报 "Instance is not persistent within this Session"。
    # 同连接上的新会话照样读得到本夹具未提交的行，隔离仍由夹具结束那一次外层 rollback 提供。
    app.state.session_factory = lambda: Session(bind=session.get_bind())
    return app


def app_for(session) -> TestClient:
    return TestClient(build_app(session))


def exit_call(client: TestClient, card, **over) -> object:
    body = {
        "feature_request_id": str(card.feature_request_id),
        "decision": "READY",
        "principal": HUMAN,
        "reason": "补齐依赖后重试",
    }
    body.update(over)
    return client.post(f"/tasks/{card.id}/escalation", json=body)


ESCALATION_ROUTE = "/tasks/{task_id}/escalation"


@needs_pg
def test_the_escalation_route_is_public_and_is_a_post(session):
    """路由形状本身也要钉：方法或路径写错，下面那几条照样绿（它们打的是同一个错处）。

    读 OpenAPI 的 `paths`、不打请求 ⇒ 这一条与那几条是两种失效面：这条红 = 路由没挂或方法不是 POST，
    那几条红 = 挂上了但行为不对。
    **键取模板形态** `{task_id}` 而不是某张真卡的号：`card.id = "T01"` 在 OpenAPI 里永远查不到，
    我第一次写这条时就是拿真卡号去查，红得像是"路由没挂"（现跑撞出来的，所以写进注释）。
    """
    paths = app_for(session).get("/openapi.json").json()["paths"]
    assert ESCALATION_ROUTE in paths, sorted(paths)
    assert "post" in paths[ESCALATION_ROUTE], sorted(paths[ESCALATION_ROUTE])


@needs_pg
def test_the_route_moves_the_card_and_claims_nothing_else_changed(session, escalated_card):
    """200 之外还要读回两个计数：这条边只动 `status`，`handoff_count` / `rework_count` 必须原样。

    `rework_count` 出现在响应里不是装饰 —— 它是迁移 `0007` 那一条新列**在公开面上可读**的唯一证据；
    少了这一句，"加了一列但没人能读"和"没加"在产品行为上长得一模一样。
    """
    request, card = escalated_card
    response = exit_call(app_for(session), card)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"] == "READY" and payload["status_after"] == "READY", payload
    assert payload["principal"] == HUMAN, payload
    assert payload["handoff_count"] == card.handoff_count, payload
    assert payload["rework_count"] == 0, payload
    session.refresh(card)
    assert str(card.status) == "READY"


@needs_pg
def test_the_route_refuses_a_decision_that_is_not_on_the_documented_edge(session, escalated_card):
    """`RUNNING` 不是那条人工边的目标 ⇒ 422 `exit_not_allowed`，卡仍停在 `ESCALATED`。

    取值权威在 `data-model.md` 那条边的箭头右侧，不在这里也不在端点里各写一份：
    这条用例故意发一个"看起来合理但文档没写"的态（卡最后确实会去 `RUNNING`，只是不能从这一格过去）。
    """
    request, card = escalated_card
    response = exit_call(app_for(session), card, decision="RUNNING")
    assert response.status_code == 422, response.text
    assert "exit_not_allowed" in response.text, response.text
    session.refresh(card)
    assert str(card.status) == "ESCALATED"


@needs_pg
def test_the_route_refuses_the_automated_actor_as_the_human_subject(session, escalated_card):
    """`principal = "orchestrator"` ⇒ 422。系统把自己签成人，这条死路就"看起来走通了"。

    反向对照是同一条请求换一个真人名字（上一条用例就是它），不是这里再发一次：
    两支持平才说明拦下来的是"主语是自动主体"这一件事，而不是"这条路由谁都拒"。
    """
    request, card = escalated_card
    response = exit_call(app_for(session), card, principal=sw.AUTOMATED_ACTOR)
    assert response.status_code == 422, response.text
    assert "human_principal_required" in response.text, response.text
    session.refresh(card)
    assert str(card.status) == "ESCALATED"


@needs_pg
def test_the_route_refuses_a_card_that_never_escalated(session, failed_card):
    """不在 `ESCALATED` 上 ⇒ 409（不是 422）：使用者的参数没错，是卡的相位不对。

    404 那一支由下一条覆盖，两支各有自己的失效面 —— 把"没这张卡"和"这张卡不该走这条边"合成一个码，
    看板就没法分辨"链接错了"和"时机错了"。
    """
    request, card = failed_card
    response = exit_call(app_for(session), card)
    assert response.status_code == 409, response.text
    assert "state_not_escalated" in response.text, response.text


@needs_pg
def test_the_route_reports_a_missing_card_as_404(session, escalated_card):
    """寻址失败 = 404。`escalation.py` 自己那个 `card_not_found` 也在映射表里，但端点先查了一次卡，
    所以这一支走的是 `_load_card` —— 两条路都得归到 404，否则同一个错两种码会被读成两种原因。
    """
    request, card = escalated_card
    body = {
        "feature_request_id": str(request.id),
        "decision": "READY",
        "principal": HUMAN,
    }
    response = app_for(session).post("/tasks/T99/escalation", json=body)
    assert response.status_code == 404, response.text
    assert "task_not_found" in response.text, response.text


def test_the_escalation_status_map_covers_every_code_the_node_can_raise():
    """`EscalationMisuse.code` 的取值与端点那张映射表 MUST 对齐（不含端点自己那一道 422）。

    写成关系式而不是列常量：以后节点多抛一种码而映射表没跟上，它会落到 500 那一支 —— 那不算崩，
    但"该是 409 的相位错"被读成"服务端坏了"是会被误修的。这条把它钉在前面。
    读的是**函数源码**而不是模块里的某个常量表：那个表今天不存在，造一个出来就变成拿实现比自己。
    """
    raised = set(re.findall(r'code="([a-z_]+)"', inspect.getsource(ep)))
    assert raised, "从 escalation.py 里一个 code 都没读到 ⇒ 这条判据已经是空判据"
    unmapped = raised - set(tasks_api.ESCALATION_STATUS_BY_CODE)
    assert unmapped <= {"doc_anchor_ambiguous", "doc_anchor_unparsed"}, (
        f"这些码没进映射表，会被当 500 报出去：{sorted(unmapped)}"
    )
