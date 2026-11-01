"""T074：`SelectWorker` 的判据（FR-022 ~ FR-025 / FR-062｜R-12）。

**形状与行为分开钉**：排序键是纯函数，全部不碰库；`SELECTION` 留痕、`executor_agent_id` 落列、
覆盖端点那三条 MUST 只能在真 PG 上验（同一行里"写了列没写留痕"这种半程态，用假 session 永远读不出来）。

**本文件有两条档位**（这是 owner 2026-10-13 裁决 ⑤ 那个 `tests/guards/` 触 PG 冲突的延续，
不是我又开了一种口径）：`conftest.py` 把 `guards` 定成 fast 档 = 不碰 PG，而 T074 的判据⑤
（人工覆盖且留痕可查）按 `tasks.md` 的原文就写在这一档里。本轮的处置与 Phase 4 一致 ——
**按 spec 给的路径放文件，PG 那几条各自挂 `skipif`**，没注入 `ACP_DB_DSN` 时它们整条 skip 而
**其余纯函数判据照样跑**。为什么不给整张文件挂 `pytestmark`：那会让"确定性"这条最该常跑的判据
在没有 PG 的机器上一起消失。移档还是改 `FAST_DIRS`，读数已在 `evidence/phase-05.md` 备好等 owner 定。

**降档模式（Phase 5 起）**：变异对照只钉核心路径 = `rank_candidates` 的排序键，见
`evidence/phase-05.md` 的变异表；其余判据不做逐条变异。

**第六节是 Phase 5 欠的一笔**：`contracts/agent-protocol.md §5` 那张契约表里"`dev_model` 置为某值
⇒ 选人与把关结果不变"那一行，Phase 5 只落了形状守卫（`tests/contract/test_protocol.py`），
同一段正文明写"语义守卫归 Phase 5 的 `tests/guards/test_select_worker.py`"⇒ 它落在**本文件**，
Phase 6 补上（见 `evidence/phase-06.md` 的 T081/T082 之后那节）。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.api import tasks as tasks_api
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import assign_guards
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence.models_core import AgentUnit, CapabilityManifest, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, RoleSpec, Snapshot

NOW = dt.datetime(2026, 10, 14, 9, 0, 0, tzinfo=dt.timezone.utc)
needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 这几条要真 PG（留痕行与 `executor_agent_id` 只能读库）",
)

TAGS = ("html", "css")
#: 三条岗名与 `assign_guards.ROLES` 同源，但这里是 **EN-04 的 role_id**：岗位说明本身归 Phase 7（T087），
#: 本轮只为"可闭合存在性"那一道检查种最小可用的三行。
ROLE_IDS = ("executor", "reviewer", "acceptor")


def candidate(
    agent_id: str,
    capabilities: tuple[str, ...],
    tier: int,
    load: int,
    rate: Decimal | None,
    sample: int,
    *,
    max_parallel: int = 3,
    cost_band: str = "MID",
) -> sw.WorkerCandidate:
    return sw.WorkerCandidate(
        agent_id=agent_id,
        capabilities=frozenset(capabilities),
        tier=tier,
        cost_band=cost_band,
        current_load=load,
        max_parallel=max_parallel,
        historical_success_rate=rate,
        success_rate_sample=sample,
    )


def task(difficulty: int = 2, tags: tuple[str, ...] = TAGS) -> sw.TaskNeed:
    return sw.TaskNeed(card_id="T01", skill_tags=tags, difficulty=difficulty)


# ------------------------------------------------------------------ 纯函数：确定性（判据①）

def test_the_same_input_ranked_three_times_gives_the_same_agent():
    """判据①：同一输入连跑三次，选出的 `agent_id` 完全一致。

    连跑三次不是仪式：这条断言要防的是"实现里混进了时间项或哈希扰动"，那种东西**单跑一次看不出来**。
    三次都从**新的** `TaskNeed` / 新的列表对象出发，免得哪天状态挂在实例上而这条成了自证。
    """
    picks = []
    for _ in range(3):
        pool = [
            candidate("adapter:b", ("html",), 2, 0, Decimal("0.9"), 10),
            candidate("adapter:a", TAGS, 3, 2, Decimal("0.1"), 4),
            candidate("adapter:c", TAGS, 2, 1, Decimal("0.5"), 8),
        ]
        picks.append(sw.rank_candidates(task(), pool).selected.agent_id)
    assert picks == ["adapter:c", "adapter:c", "adapter:c"], picks


def test_the_input_order_does_not_change_the_resulting_sequence():
    """全序的含义：把候选列表倒过来喂，得到的**序列**必须逐位相同。

    只断首位会漏掉一种真实缺陷：首位对了但后几位依赖输入顺序（`sorted` 的稳定性会掩盖它）。
    """
    pool = [
        candidate("adapter:a", TAGS, 2, 0, Decimal("0.9"), 10),
        candidate("adapter:b", TAGS, 2, 0, Decimal("0.4"), 10),
        candidate("adapter:c", ("html",), 1, 0, Decimal("1.0"), 10),
    ]
    forward = [ranked.agent_id for ranked in sw.rank_candidates(task(), pool).ordered]
    backward = [ranked.agent_id for ranked in sw.rank_candidates(task(), list(reversed(pool))).ordered]
    assert forward == backward == ["adapter:a", "adapter:b", "adapter:c"], (forward, backward)


def test_the_documented_five_keys_appear_in_order_when_all_are_comparable():
    """`keys` 的取值与位序 = plan 设计项 8 写死的那五位，一字不多一字不少。

    键名会进留痕（可审计面），所以这里钉的是**字面串 + 顺序**，不是"有五个元素"。
    """
    ranking = sw.rank_candidates(
        task(),
        [candidate("adapter:a", TAGS, 2, 0, Decimal("0.9"), 10), candidate("adapter:b", TAGS, 2, 1, Decimal("0.4"), 10)],
    )
    assert ranking.keys == sw.DOCUMENTED_KEYS == (
        "skill_fit",
        "tier_match",
        "historical_success_rate",
        "current_load",
        "agent_id",
    )


# ------------------------------------------------------------------ 键①②：契合优先（判据②）

def test_a_busy_but_skilled_candidate_beats_an_idle_but_unskilled_one():
    """判据②：契合高但正忙 **胜** 契合低但空闲 ⇒ 负载确实排在第四位。

    两位候选在键①上就分开了（2/2 对 1/2），键②（同档）与键③（同战绩）故意打平，
    否则这条测的是别的键。空闲那一支的 `current_load=0` 是诱饵：负载真被排到前面就会选中它。
    """
    ranking = sw.rank_candidates(
        task(difficulty=2),
        [
            candidate("adapter:busy-skilled", TAGS, 2, 2, Decimal("0.8"), 10),
            candidate("adapter:idle-unskilled", ("html",), 2, 0, Decimal("0.8"), 10),
        ],
    )
    assert ranking.selected.agent_id == "adapter:busy-skilled"
    assert [ranked.agent_id for ranked in ranking.ordered] == [
        "adapter:busy-skilled",
        "adapter:idle-unskilled",
    ]


def test_the_tier_key_prefers_the_agent_whose_tier_matches_the_difficulty():
    """键②：技能契合打平时，**同档**的候选胜；偏离量大小决定次序（口径见 `select_worker` 模块头）。

    反向对照一起做：难度换成 3 时名次**必须跟着换人** —— 这证明比较的是"对难度的匹配度"，
    既不是"高档永远优先"，也不是"低档永远优先"。三档各放一支，两种难度下赢的不是同一支。
    """
    pool = [
        candidate("adapter:far", TAGS, 1, 0, Decimal("0.8"), 10),
        candidate("adapter:near", TAGS, 2, 0, Decimal("0.8"), 10),
        candidate("adapter:exact", TAGS, 3, 0, Decimal("0.8"), 10),
    ]
    assert sw.rank_candidates(task(difficulty=2), pool).selected.agent_id == "adapter:near"
    assert sw.rank_candidates(task(difficulty=3), pool).selected.agent_id == "adapter:exact"
    ordered = [ranked.agent_id for ranked in sw.rank_candidates(task(difficulty=1), pool).ordered]
    assert ordered == ["adapter:far", "adapter:near", "adapter:exact"]


def test_all_else_equal_the_less_loaded_candidate_wins():
    """键④：前四位全打平时，**闲的胜忙的**（`current_load` 是升序位）。

    两支除了负载全部相同（同契合、同档、同战绩、同样本）⇒ 这条唯一能证的就是负载那一位的朝向。
    反过来的写法（忙者优先）会让这条红，而"键④存在"这种断言不会 —— 所以断的是**次序**不是键名。
    """
    ranking = sw.rank_candidates(
        task(),
        [
            candidate("adapter:a", TAGS, 2, 2, Decimal("0.8"), 10),
            candidate("adapter:b", TAGS, 2, 0, Decimal("0.8"), 10),
        ],
    )
    assert [ranked.agent_id for ranked in ranking.ordered] == ["adapter:b", "adapter:a"]


def test_the_agent_id_breaker_makes_the_last_key_total():
    """键⑤：四要素全同 ⇒ 由 `agent_id` 升序兜底，**不存在平票** ⇒ 不存在"随便选一个"。

    输入顺序故意摆成"会被错选的那一侧"（`z` 在前）：拿掉兜底位时这条会因两次输入给出不同序列而红，
    只断"选出了某个人"则永远绿 —— 那正是 FR-022 想要的"顺序"与"一次结果"的区别。
    """
    twins = [
        candidate("adapter:z", TAGS, 2, 1, Decimal("0.8"), 10),
        candidate("adapter:a", TAGS, 2, 1, Decimal("0.8"), 10),
    ]
    forward = [ranked.agent_id for ranked in sw.rank_candidates(task(), twins).ordered]
    backward = [ranked.agent_id for ranked in sw.rank_candidates(task(), list(reversed(twins))).ordered]
    assert forward == backward == ["adapter:a", "adapter:z"], (forward, backward)


# ------------------------------------------------------------------ 键③：无战绩不是 0%（判据④）

def test_a_candidate_without_history_is_disclosed_and_not_scored_as_zero():
    """判据④的前半：无战绩让键③**整批跳过**，被选中的人要在 `selection_basis` 里显式披露。

    构造是关键：两支候选在键①②④上都打平（同为 2/2 契合、同档、同为 0 负载），
    于是"有战绩的 0.3"与"无战绩的"若按 0% 处理就会分出胜负 ⇒ 现在它们打平，由 `agent_id` 兜底定序。
    这条断的是**没有按 0% 处理**，不是断"无战绩方赢了"。
    """
    ranking = sw.rank_candidates(
        task(),
        [
            candidate("adapter:x", TAGS, 2, 0, Decimal("0.3"), 9),
            candidate("adapter:y", TAGS, 2, 0, None, 0),
        ],
    )
    assert "historical_success_rate" not in ranking.keys
    assert ranking.skipped, "键③被跳过却没披露 ⇒ 留痕会读成'这一位比过且打平'"
    basis = ranking.selection_basis()
    assert "无历史战绩" in str(basis), basis["skipped"]
    assert [ranked.agent_id for ranked in ranking.ordered] == ["adapter:x", "adapter:y"]


def test_the_value_object_refuses_a_rate_without_a_sample():
    """与库里 `no_rate_without_sample` 那条 CHECK 同口径，但**在构造期就拒**。

    为什么不等 PG 报错：`success_rate_sample = 0` 而比率非空是调用方算错了（分母为 0 的占比不存在），
    把它带到比较里会得到一个看起来合理的名次；等入库才发现就晚了半个链路。
    """
    with pytest.raises(sw.SelectionMisuse):
        candidate("adapter:broken", TAGS, 2, 0, Decimal("0.5"), 0)


# ------------------------------------------------------------------ 键①的分母：响亮失败（判据③）

def test_empty_skill_tags_fail_loudly_instead_of_selecting_someone():
    """判据③：`skill_tags` 为空 ⇒ 抛错，**不选任何人**（分母 0 不许用相似度补，见 D-13）。"""
    with pytest.raises(sw.SkillTagsEmpty):
        sw.TaskNeed(card_id="T01", skill_tags=(), difficulty=2)


def test_a_saturated_candidate_is_excluded_with_a_readable_reason():
    """FR-025 的"全忙"：`current_load >= max_parallel` 出局，原因进留痕，首位顺延。

    上限本身是逐条申报值（`params-ref §9` 明写"默认 3 属运行时槽位侧，`agents.yaml` 不许按默认过关"），
    所以这里用 `max_parallel=1` 的一支来证明判定读的是申报值，不是那个默认数。
    """
    ranking = sw.rank_candidates(
        task(),
        [
            candidate("adapter:full", TAGS, 2, 1, Decimal("0.9"), 10, max_parallel=1),
            candidate("adapter:free", ("html",), 2, 0, Decimal("0.2"), 10),
        ],
    )
    assert ranking.selected.agent_id == "adapter:free"
    assert [agent_id for agent_id, _ in ranking.excluded] == ["adapter:full"]
    assert "max_parallel" in ranking.excluded[0][1]


def test_a_candidate_dropped_before_ranking_is_still_recorded_as_excluded():
    """`extra_excluded` 的由来：候选集构造阶段被拿掉的人**不许静默消失**。

    "没人可选"要能读出是被谁挡的、为什么挡（FR-025 的"MUST NOT 静默丢弃"同样管候选集本身）。
    """
    ranking = sw.rank_candidates(
        task(),
        [candidate("adapter:a", TAGS, 2, 0, Decimal("0.5"), 4)],
        extra_excluded=[("adapter:ghost", "已启用但没有能力申报")],
    )
    assert [ranked.agent_id for ranked in ranking.ordered] == ["adapter:a"]
    assert ("adapter:ghost", "已启用但没有能力申报") in ranking.excluded
    assert ranking.selection_basis()["excluded"] == [
        {"agent_id": "adapter:ghost", "reason": "已启用但没有能力申报"}
    ]


def test_no_eligible_candidate_leaves_a_notice_and_no_selection():
    """T072：全忙 ⇒ `selected is None` + 一句可读提示，而不是异常，也不是"随便选一个"。"""
    ranking = sw.rank_candidates(
        task(), [candidate("adapter:full", TAGS, 2, 3, Decimal("0.9"), 10, max_parallel=3)]
    )
    assert ranking.selected is None
    assert ranking.ordered == ()
    assert "READY" in ranking.waiting_notice and "不静默丢弃" in ranking.waiting_notice


def test_duplicate_candidates_are_refused_instead_of_an_unexplainable_order():
    """同一名 `agent_id` 出现两次 ⇒ 全序里会有两个并列首位，比较结果不可解释 ⇒ 当场拒。"""
    pool = [candidate("adapter:a", TAGS, 2, 0, Decimal("0.5"), 4)] * 2
    with pytest.raises(sw.SelectionMisuse):
        sw.rank_candidates(task(), pool)


def test_an_override_only_reorders_and_keeps_the_auto_choice_visible():
    """`with_override` 的边界：只重排，`keys` / `skipped` 不变，且原首位留在 `auto_selected` 里。

    这是 FR-024"覆盖行为留痕"的可核对形状：只存最终执行人就永久丢掉了"推翻了谁"。
    """
    ranking = sw.rank_candidates(
        task(),
        [
            candidate("adapter:a", TAGS, 2, 0, Decimal("0.9"), 10),
            candidate("adapter:b", TAGS, 2, 0, Decimal("0.4"), 10),
        ],
    )
    overridden = ranking.with_override("adapter:b")
    assert overridden.selected.agent_id == "adapter:b"
    assert overridden.auto_selected == "adapter:a"
    assert overridden.keys == ranking.keys and overridden.skipped == ranking.skipped
    basis = overridden.selection_basis(overridden=True, by="user:owner")
    assert basis["overridden"] is True and basis["auto_selected"] == "adapter:a"
    assert basis["decided_by"] == "user:owner"


def test_an_override_of_an_ineligible_agent_is_refused_at_the_value_object_level():
    """资格判据不属于覆盖：没进过比较的人（满载 / 未启用）摆不到首位 ⇒ 抛错。"""
    ranking = sw.rank_candidates(task(), [candidate("adapter:a", TAGS, 2, 0, Decimal("0.9"), 10)])
    with pytest.raises(sw.SelectionMisuse):
        ranking.with_override("adapter:never-registered")


# ------------------------------------------------------------------ PG：留痕与端点（判据⑤ + 三条 MUST）

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
def request_row(session) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def card_row(session, request_row) -> TaskCard:
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
        feature_request_id=request_row.id,
        title="卡 T01",
        difficulty=2,
        skill_tags=["html", "css"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="READY",
        snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    return card


def seed_unit(
    session,
    agent_id: str,
    capabilities: tuple[str, ...],
    *,
    tier: int = 2,
    load: int = 0,
    rate: Decimal | None = Decimal("0.5"),
    sample: int = 10,
    max_parallel: int = 3,
    enabled: bool = True,
    declared_at: dt.datetime = NOW,
    with_manifest: bool = True,
) -> None:
    """种一支 EN-03 单元（可选配一份 EN-05 申报）。`with_manifest=False` 用来测"无申报"那一支路。"""
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=tier,
            cost_band="MID",
            max_parallel=max_parallel,
            current_load=load,
            historical_success_rate=rate,
            success_rate_sample=sample,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=enabled,
        )
    )
    if with_manifest:
        session.add(
            CapabilityManifest(
                agent_id=agent_id,
                capabilities=list(capabilities),
                tier=tier,
                cost_band="MID",
                max_parallel=max_parallel,
                declared_at=declared_at,
            )
        )
    session.flush()


def seed_roles(session, agent_ids: tuple[str, ...]) -> None:
    """三岗都挂满这三个人 ⇒ "可闭合存在性"这一道在覆盖路径上是通的（自审禁令仍单独判）。"""
    for role_id in ROLE_IDS:
        session.add(
            RoleSpec(
                role_id=role_id,
                name=f"岗 {role_id}",
                charter="最小可用岗位说明（岗位正文归 Phase 7 的 T087）",
                read_only=role_id != "executor",
                builtin=True,
                mounted_agents=list(agent_ids),
            )
        )
    session.flush()


def app_for(session) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_api.router)
    # 每次请求一个新会话，绑在**同一条连接**上（2026-10-28）。
    # 端点现在一定会 `close()` 它借到的会话（`api/tasks.py::_store` 的 finally），
    # 把夹具自己那个交出去，第一发请求之后本文件的回读判据就全哑了：
    # `session.refresh(card)` 会报 "Instance is not persistent within this Session"。
    # 同连接上的新会话照样读得到本夹具未提交的行，隔离仍由夹具结束那一次外层 rollback 提供。
    app.state.session_factory = lambda: Session(bind=session.get_bind())
    return TestClient(app)


@needs_pg
def test_the_auto_selection_writes_the_executor_column_and_exactly_one_row(session, card_row):
    """T070 + `data-model.md:34`：选人结果落 `executor_agent_id`，依据落**一行** EN-14 `kind=SELECTION`。

    "恰一行"是这条的主体：留痕写两行就会让 FR-023 的展示面出现两个互相矛盾的"为什么"。
    """
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_unit(session, "adapter:b", ("html",), rate=Decimal("0.4"))
    candidates, dropped = sw.candidates_from_registry(session)
    ranking = sw.rank_candidates(sw.task_need_of(card_row), candidates, dropped)
    sw.apply_selection(
        session, ranking, feature_request_id=card_row.feature_request_id, actor_agent_id=sw.AUTOMATED_ACTOR, created_at=NOW
    )

    rows = list(session.scalars(select(EventLog).where(EventLog.kind == "SELECTION")))
    assert len(rows) == 1
    assert card_row.executor_agent_id == "adapter:a"
    payload = rows[0].payload
    assert payload["selected"] == "adapter:a" and payload["overridden"] is False
    assert rows[0].actor_agent_id == sw.AUTOMATED_ACTOR, (
        "自动路径的留痕主语必须是那个常量：owner 裁决 6 要求它成常量，"
        "而 Phase 7 若要引入'系统 actor'概念，改的应当是一个常量 + 这一条断言"
    )
    assert rows[0].entity == "task_card" and rows[0].entity_id == card_row.id


@needs_pg
def test_the_newest_capability_declaration_is_the_one_used(session, card_row):
    """EN-05 一个 agent 可以有多行 ⇒ 生效的是 `declared_at` 最新那份。

    这条钉的是一个真实会错的点：拿"第一行"会让过期技能表继续起作用，而它**不会报错**，
    只会让键①的名次悄悄变掉。故意把过期那份摆在**更容易赢**的一侧（多一个标签）。
    """
    seed_unit(session, "adapter:a", ("html", "css"), rate=Decimal("0.9"))
    seed_unit(session, "adapter:b", (), rate=Decimal("0.9"), with_manifest=False)
    session.add(
        CapabilityManifest(
            agent_id="adapter:a",
            capabilities=["html"],
            tier=2,
            cost_band="MID",
            max_parallel=3,
            declared_at=NOW - dt.timedelta(days=30),
        )
    )
    session.add(
        CapabilityManifest(
            agent_id="adapter:b",
            capabilities=["html", "css", "figma"],
            tier=2,
            cost_band="MID",
            max_parallel=3,
            declared_at=NOW + dt.timedelta(minutes=1),
        )
    )
    session.flush()

    candidates, dropped = sw.candidates_from_registry(session)
    by_id = {c.agent_id: c.capabilities for c in candidates}
    assert by_id["adapter:a"] == frozenset({"html", "css"}), by_id
    assert "figma" in by_id["adapter:b"]
    assert dropped == []


@needs_pg
def test_an_enabled_unit_without_a_declaration_is_reported_not_dropped(session, card_row):
    """已启用但没有 EN-05 申报 ⇒ 进 `excluded` 而**不是**从候选集里蒸发。"""
    seed_unit(session, "adapter:a", TAGS)
    seed_unit(session, "adapter:ghost", (), with_manifest=False)
    candidates, dropped = sw.candidates_from_registry(session)
    assert [c.agent_id for c in candidates] == ["adapter:a"]
    assert [agent_id for agent_id, _ in dropped] == ["adapter:ghost"]


@needs_pg
def test_the_override_endpoint_flips_the_executor_and_leaves_an_auditable_basis(session, card_row):
    """判据⑤ + T071 MUST③：人工覆盖后**库里**执行人变了，`SELECTION` 行标 `overridden=true` 且留住原首位。

    走真端点而不是直接调函数：三条 MUST 是端点上的约束，函数级断言证明不了路由与校验的顺序。
    """
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_unit(session, "adapter:b", TAGS, rate=Decimal("0.4"))
    seed_roles(session, ("adapter:a", "adapter:b", "adapter:c"))

    client = app_for(session)
    result = client.post(
        f"/tasks/{card_row.id}/worker",
        json={
            "feature_request_id": str(card_row.feature_request_id),
            "agent_id": "adapter:b",
            "operator": "user:owner",
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["executor_agent_id"] == "adapter:b" and body["auto_selected"] == "adapter:a"
    assert body["overridden"] is True and body["keys"] == list(sw.DOCUMENTED_KEYS)

    row = session.scalars(select(EventLog).where(EventLog.kind == "SELECTION")).one()
    assert row.payload["overridden"] is True
    assert row.payload["auto_selected"] == "adapter:a"
    assert row.payload["decided_by"] == "user:owner"
    # 端点在**另一个会话**里写（见 `app_for` 的注释）⇒ 本会话 identity map 里那张卡还是旧值。
    # 不 expire 就读，量到的是"我缓存里的执行人"，不是库里那一个 —— 这条断言会变成恒真。
    session.expire_all()
    assert session.get(TaskCard, (card_row.feature_request_id, card_row.id)).executor_agent_id == "adapter:b"


@needs_pg
def test_the_override_endpoint_refuses_an_agent_that_is_over_its_own_limit(session, card_row):
    """T071 MUST①：覆盖不能把"自己接满了"的人指上去（FR-011 的 `max_parallel` 是逐条申报值）。"""
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_unit(session, "adapter:full", TAGS, load=2, max_parallel=2)
    seed_roles(session, ("adapter:a", "adapter:full", "adapter:c"))

    result = app_for(session).post(
        f"/tasks/{card_row.id}/worker",
        json={
            "feature_request_id": str(card_row.feature_request_id),
            "agent_id": "adapter:full",
            "operator": "user:owner",
        },
    )
    assert result.status_code == 422
    detail = result.json()["detail"]
    assert detail.startswith("override_not_eligible") and "max_parallel" in detail


@needs_pg
def test_the_override_endpoint_refuses_to_make_a_reviewer_review_himself(session, card_row):
    """新增条（owner 2026-10-14 登记）：覆盖路径**自己**要有一条三岗判据，不与 `AssignGuards` 那条合并。

    `adapter:b` 已是这张卡的 reviewer，再把它指成 executor ⇒ 同一支工具自审（FR-034）。
    这条与 Phase 7 的 T097 是两条路径上的两件事：T097 判 `AssignGuards` 填岗，本条判人工改派。
    """
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_unit(session, "adapter:b", TAGS, rate=Decimal("0.4"))
    seed_roles(session, ("adapter:a", "adapter:b", "adapter:c"))
    card_row.reviewer_agent_id = "adapter:b"
    session.flush()

    result = app_for(session).post(
        f"/tasks/{card_row.id}/worker",
        json={
            "feature_request_id": str(card_row.feature_request_id),
            "agent_id": "adapter:b",
            "operator": "user:owner",
        },
    )
    assert result.status_code == 422
    assert result.json()["detail"].startswith("self_review_forbidden")


@needs_pg
def test_the_override_endpoint_refuses_an_agent_that_makes_the_matrix_unclosable(session, card_row):
    """T071 MUST② 的后半：把执行者换成一支**只剩两个人能填满三岗**的池子里的人 ⇒ 拒。

    这里 role_spec 只挂两个人，`assert_closable` 的池子 < 3 ⇒ `guard_not_closable`。
    故意让被指的 `adapter:a` 本身完全合格（契合、同档、不忙）：证明拦住它的不是资格而是矩阵。
    """
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_unit(session, "adapter:b", TAGS, rate=Decimal("0.4"))
    seed_roles(session, ("adapter:a", "adapter:b"))
    session.add(
        RoleSpec(
            role_id="acceptor-only-for-c",
            name="只给 C 的验收岗",
            charter="让候选池退化成两个人",
            read_only=True,
            builtin=True,
            mounted_agents=["adapter:c"],
        )
    )
    session.flush()

    result = app_for(session).post(
        f"/tasks/{card_row.id}/worker",
        json={
            "feature_request_id": str(card_row.feature_request_id),
            "agent_id": "adapter:a",
            "operator": "user:owner",
        },
    )
    assert result.status_code == 422
    assert result.json()["detail"].startswith("guard_not_closable")


@needs_pg
def test_the_selection_payload_is_redacted_before_it_lands(session, card_row):
    """EN-14 的 guardrail：payload 落库前 MUST 过脱敏，**嵌套层也要洗**（宪法「二」）。

    凭据形态在运行时拼出来（`sk-` 串不写死在源码里），这样工作树的凭据形态扫描读数仍是 0；
    载体选 `excluded` 那一条，是因为它是列表里的字典 —— 只洗顶层的写法会从这里漏出去。
    """
    probe = "sk-" + "redacted-notreal-0123456789"
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    ranking = sw.rank_candidates(
        sw.task_need_of(card_row),
        [candidate("adapter:a", TAGS, 2, 0, Decimal("0.9"), 10)],
        extra_excluded=[("adapter:ghost", probe)],
    )
    sw.apply_selection(
        session, ranking, feature_request_id=card_row.feature_request_id, actor_agent_id=sw.AUTOMATED_ACTOR, created_at=NOW
    )

    row = session.scalars(select(EventLog).where(EventLog.kind == "SELECTION")).one()
    assert probe not in str(row.payload), "凭据形态原样落库 ⇒ 脱敏只在顶层生效"
    assert "[REDACTED]" in str(row.payload)


@needs_pg
def test_a_card_with_no_skill_tags_cannot_be_assigned_through_the_endpoint(session, card_row):
    """判据③的端点侧：`skill_tags` 为空 ⇒ 422 且文案是 `guard_violation`，**不返回 200 也不改列**。

    MVP-1 的 B 类卡点是结构预留（`service.ACTIVE_KINDS` 只有 A / D），所以本轮形态是响亮失败；
    B 启用时改的是出口，这条断言的文案前缀也要跟着改 —— 登记在 `evidence/phase-05.md` 的未闭段。
    """
    seed_unit(session, "adapter:a", TAGS, rate=Decimal("0.9"))
    seed_roles(session, ("adapter:a", "adapter:b", "adapter:c"))
    card_row.skill_tags = ["html", "css"]
    session.flush()

    client = app_for(session)
    ok = client.post(
        f"/tasks/{card_row.id}/worker",
        json={"feature_request_id": str(card_row.feature_request_id), "agent_id": "adapter:a", "operator": "u"},
    )
    assert ok.status_code == 200, ok.text

    card_row.skill_tags = []
    session.flush()
    result = client.post(
        f"/tasks/{card_row.id}/worker",
        json={"feature_request_id": str(card_row.feature_request_id), "agent_id": "adapter:a", "operator": "u"},
    )
    assert result.status_code == 422
    assert result.json()["detail"].startswith("guard_violation")


# —————————————————————————— 六、`dev_model` 不许进任何判定（契约 §5 那行的语义半段）

#: `contracts/agent-protocol.md §1` 把 `dev_model` 定成"开发期扩展位，MUST NOT 参与选人、把关、成本判定"。
#: §5 的契约表里那一行（"`dev_model` 置为某值 ⇒ 选人与把关结果不变"）到 Phase 5 只落了**形状守卫**
#: （`tests/contract/test_protocol.py::test_dev_model_is_the_only_difference`），语义守卫按原文归本文件 ——
#: 这一段就是那笔欠账。
DECISION_SOURCES = (
    "src/acp/orchestrate/nodes/select_worker.py",  # 选人（FR-022 ~ FR-025）
    "src/acp/orchestrate/nodes/handoff.py",  # 换人复用同一条全序，它是选人的第二个调用方
    "src/acp/orchestrate/nodes/assign_guards.py",  # 把关（FR-031 / FR-032 的可闭合性）
    "src/acp/api/tasks.py",  # 覆盖端点：人工决定也要走同一把尺
    "src/acp/approval/service.py",  # 五要素里的预算/成本那一面
)
EXTENSION_SLOTS = frozenset({"dev_model", "model_ref"})
REPO_ROOT = Path(__file__).resolve().parents[2]


def extension_slot_reads(source: str, *, filename: str) -> list[str]:
    """AST 里"被代码读到"的扩展位名字：属性名 / 变量名 / 关键字参数 / 字符串键。

    只判 AST 能确定的那四种位置。文档字符串**整段跳过**（它是给人读的话，不是读值的代码）——
    少了这一步，这条判据会退化成一次 `grep`：本仓库有 5 处正当的散文提到 `dev_model`
    （`protocol/refs.py:63`、`messages.py:75`、契约 §1/§5、`data-model.md:182`），
    拿 grep 当判据的结果是"要么一进来就红，要么把散文全删掉"。
    """
    tree = ast.parse(source, filename=filename)
    prose: set[int] = set()
    for holder in ast.walk(tree):
        if not isinstance(holder, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(holder, "body", ())
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                prose.add(id(body[0].value))
    hits: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.keyword):
            name = node.arg
        elif isinstance(node, ast.Constant) and id(node) not in prose and isinstance(node.value, str):
            name = node.value
        if name in EXTENSION_SLOTS:
            hits.append(f"{filename}:{getattr(node, 'lineno', 0)} 读到 {name}")
    return hits


def test_no_decision_module_reads_the_extension_slot():
    """五个判定模块对 `dev_model` / `model_ref` 的**代码读取**必须为零。

    这条是 §5 那一行的权威判据，比"跑两遍看结果一样"强：行为对照只在今天这一版里成立
    （选人读的是 EN-02 的列，扩展位根本不在输入面上），将来有人在 `TaskNeed` 上加一个字段，
    跑两遍仍然一样 —— 因为那两遍用的是同一张卡。摘掉这个前提的写法就是把判据写成恒真。
    """
    for rel in DECISION_SOURCES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"判据的覆盖面会悄悄缩水：{rel} 不在了"
        hits = extension_slot_reads(path.read_text(encoding="utf-8"), filename=rel)
        assert hits == [], f"{rel} 把扩展位读进了判定：{hits}"


def test_the_scanner_flags_a_read_in_every_module_it_claims_to_cover():
    """每个被点名的文件各自注入一次违例 ⇒ 都必须被抓到（不是"整体抓到一个就算"）。

    这一条把上一条从"扫了 5 个文件里的 1 个"抬成"5 个位置各自可证伪"。注入的是**内存里的源码串**，
    不往工作树写任何东西。
    """
    for rel in DECISION_SOURCES:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        probe = "\n\ndef _qoder_probe(card):\n    return card.dev_model, getattr(card, 'model_ref')\n"
        hits = extension_slot_reads(source + probe, filename=rel)
        assert len(hits) >= 2, f"{rel} 注入了两处读取却没被抓到：{hits}"
        assert {hit.rsplit(" ", 1)[-1] for hit in hits} == {"dev_model", "model_ref"}, hits


def test_prose_and_longer_identifiers_are_not_violations():
    """两条反向（必须**不**红）：只在文档字符串里提、以及名字只是"以扩展位为前缀"。

    少了这一条，判据的坏法就只剩"放宽"一种：把散文也算违例的话，改注释要红；
    用子串匹配的话 `dev_model_alias` 这种合法命名也要红。
    """
    docstringed = '"""契约 §1 说 dev_model 是扩展位，model_ref 不许参与判定。"""\n\nVALUE = 1\n'
    assert extension_slot_reads(docstringed, filename="probe_doc.py") == []
    prefixed = "def f(cfg):\n    return cfg.dev_model_alias, cfg.model_reference\n"
    assert extension_slot_reads(prefixed, filename="probe_prefix.py") == []


def test_the_three_decision_inputs_have_no_field_to_put_it_in():
    """输入面上没有格子 ⇒ 结构上进不来。三张表 = 选人 / 把关 / 覆盖端点的请求体。

    与上一条扫描互补：扫描管"有人绕过去直接读"，这条管"有人把它声明成判定输入" ——
    后者一旦发生，`test_no_decision_module_reads...` 也要红，但这条会先点名是**哪张表**变了。
    """
    surfaces = {
        "select_worker.TaskNeed": {f.name for f in dataclasses.fields(sw.TaskNeed)},
        "select_worker.WorkerCandidate": {f.name for f in dataclasses.fields(sw.WorkerCandidate)},
        "assign_guards.CardNeed": {f.name for f in dataclasses.fields(assign_guards.CardNeed)},
        "api.tasks.WorkerOverrideBody": set(tasks_api.WorkerOverrideBody.model_fields),
    }
    for name, fields in surfaces.items():
        assert not (fields & EXTENSION_SLOTS), f"{name} 把扩展位声明成了判定输入：{sorted(fields & EXTENSION_SLOTS)}"
