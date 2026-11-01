"""T098 + T088：只读隔离 —— 三种写路径全被拒，**且审查岗仍能出具结论**。

档位：`tests/guards/`，混合文件。ACL 那三条是纯函数 + 临时总线根（fast，不碰 PG）；
身份生产（`bus/actor.py` 读 EN-04）与"结论真能入库"那几条要真 PG（各挂 `skipif`，读数注明带 `.env`）。

**"三种写路径"取的是哪三种**（owner 的 T098 原文只写"三种写路径全被拒"，没列清单；这里逐条点名，
免得下一轮把这数当成随便三个）：

| # | 路径 | 归谁 | 拒因（`contracts/file-bus.md §2.1`） |
|---|---|---|---|
| ① | 被审产物所在路径（`<executor>-workspace/…`） | 别的工具 | `read_only_violation`（只读身份写别人的 workspace） |
| ② | `status.md` | 公共区 | `read_only_violation`（公共区对全体 Agent 只读，写归编排引擎单点） |
| ③ | `results/delivery_package.json` | 公共区（T095 的落点） | `read_only_violation` |

反向对照 MUST 在场：**只读身份写它自己那一份 workspace 仍放行**（owner 2026-10-13 裁决 1 把只读定成
任务级；Phase 3 已按此收窄 `acl.py:157-166`）。少了这一条，"任何写都拒"的实现也全绿，
而那种实现会让审查岗连自己的结论文件都写不下去 —— 那是把只读做成了禁用。

**"仍能出具结论"是这条判据的重点**（防假门禁）：只读如果同时把结论能力一起削掉，
表面上"把关很严"，实际是把关消失了。所以有一行结论入库的断言在同一文件里，
而不是只断"写被拒"。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.bus import acl, layout
from acp.bus import actor as actor_module
from acp.bus.gateway import BusGateway, GuardEvent
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import assign_guards as ag
from acp.orchestrate.nodes import review as rv
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.registry import roles as role_registry

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 8, 20, tzinfo=dt.timezone.utc)
EXECUTOR = "qoder"
REVIEWER = "doubao"
ACCEPTOR = "workbuddy"
ARTIFACT = f"{EXECUTOR}-workspace/index.html"

#: 三种被拒的写路径（模块头那张表的左列）。
DENIED_PATHS = (ARTIFACT, layout.STATUS_MD, layout.DELIVERY_PACKAGE_JSON)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / layout.RESULTS_DIR).mkdir()
    Path(tmp_path, f"{EXECUTOR}-workspace").mkdir()
    Path(tmp_path, f"{REVIEWER}-workspace").mkdir()
    Path(tmp_path, f"{ACCEPTOR}-workspace").mkdir()
    return tmp_path


def reviewer_actor() -> acl.Actor:
    """生产形态的把关身份：`adapter:<agent_id>` + `read_only=True`（值来自 EN-04，见 PG 那半）。"""
    return acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}{REVIEWER}", "reviewer", read_only=True)


# ---------------------------------------------------------------- 一、三条被拒 + 一条放行（fast）

@pytest.mark.parametrize("path", DENIED_PATHS)
def test_a_read_only_guard_cannot_write_any_of_the_three_paths(path: str):
    """①②③：三种写路径逐个被拒，且拒因都是 `read_only_violation` 而不是别的。

    分开点拒因是必须的：`write_forbidden`（归属不符）也会拒掉 ①，
    但如果只读那一半根本没生效，靠归属判据就能"看起来对"—— 那时删掉 `acl.py:157` 那段判断，
    ①仍绿而②③会红（②③不是任何人的 workspace）。所以三条都在，且都断**同一个** code。
    """
    decision = acl.can_write(reviewer_actor(), path, acl.Capability(REVIEWER, "T01"))
    assert decision.allowed is False, decision
    assert decision.code == acl.READ_ONLY_VIOLATION, decision
    assert decision.http_status == 403, decision


def test_a_read_only_guard_can_still_write_its_own_workspace(root):
    """反向对照（裁决 1 的另一半）：只读放行的正是"它自己那一份 workspace"。

    少了这条，"任何写都拒"的实现也全绿 —— 而那种实现把审查岗的结论落点一起削掉了。
    """
    own = f"{REVIEWER}-workspace/findings.md"
    decision = acl.can_write(reviewer_actor(), own, acl.Capability(REVIEWER, "T01"))
    assert decision.allowed is True, decision


def test_the_denial_is_recorded_in_the_bus_audit(root):
    """拒写不是"悄悄返回 False"：总线的守卫事件里有一条可查的 `read_only_violation`。

    这条与上一条配对存在的原因：如果审计不落，"把关被绕过了"与"把关没启用"在事后读起来一模一样。
    """
    events: list[GuardEvent] = []
    gateway = BusGateway(root, audit_sink=events.append)
    with pytest.raises(Exception) as caught:
        gateway.write_artifact(reviewer_actor(), layout.STATUS_MD, "| x |\n", capability=None)
    assert "read_only" in str(caught.value).lower() or any(
        event.code == acl.READ_ONLY_VIOLATION for event in events
    ), (str(caught.value), [event.code for event in events])


def test_a_guard_without_a_capability_still_cannot_write_anything(root):
    """任务级凭证缺失时也不放行：只读身份 + `capability=None` ⇒ 仍拒。

    这条防的是"把只读写成'只有带凭证才拦'"那种倒置实现：`acl.py` 的顺序是只读判定在归属比对之前，
    所以没有凭证时先被只读拦下，走不到凭证那一步。
    """
    decision = acl.can_write(reviewer_actor(), f"{REVIEWER}-workspace/findings.md", None)
    assert decision.allowed is False, decision


# ---------------------------------------------------------------- 二、下发请求不给可写位置（fast）

def _card_stub() -> TaskCard:
    return TaskCard(
        id="T01",
        feature_request_id=uuid.uuid4(),
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有标题", "样式表被引用"],
        handoff_count=0,
        review_round=0,
        status="REVIEWING",
        snapshot_id=uuid.uuid4(),
        executor_agent_id=EXECUTOR,
        reviewer_agent_id=REVIEWER,
        acceptor_agent_id=ACCEPTOR,
    )


def test_the_review_request_contains_no_path_the_reviewer_could_write():
    """T088 的核心形状：请求里出现的**每一条路径**，审查者都写不了。

    这条不是"看起来没写路径"就完事：判据把渲染出来的文本逐行找路径形状，再拿 `acl.can_write`
    现判一次。于是"顺手把审查者自己的 workspace 落点也告诉它"这种改法会当场红 ——
    而那恰恰是"邀请它改一下格式"的起点（FR-033 要防的动作）。
    """
    card = _card_stub()
    request = rv.build_request(
        card, [(ARTIFACT, "<html><h1>标题</h1></html>\n" * 12), (f"{EXECUTOR}-workspace/app.css", "body{}\n" * 20)],
        reviewer_agent_id=REVIEWER,
    )
    text = request.render()
    paths = [line for line in text.splitlines() if "/" in line and not line.startswith("按下列")]
    assert paths, "请求里连一条产物路径都没有 ⇒ 这条判据在量空气"
    for candidate in paths:
        rel = candidate.strip("- ").strip()
        decision = acl.can_write(reviewer_actor(), rel, acl.Capability(REVIEWER, "T01"))
        assert decision.allowed is False, f"审查者在这份请求里拿到了一个它能写的路径：{rel}"
    assert f"{REVIEWER}-workspace" not in text


def test_the_review_request_carries_bodies_not_references():
    """请求的可见面是**正文**（"只把产物内容送进请求"那半句），且标准逐条列出。

    反向：标准集合与卡上 `acceptance_criteria` **相等**（多一条少一条都不对 ——
    少一条审查者就少判一条，FR-029 的"逐条比对"由这里成立）。
    """
    card = _card_stub()
    request = rv.build_request(card, [(ARTIFACT, "<html>正文</html>")], reviewer_agent_id=REVIEWER)
    assert request.acceptance_criteria == tuple(card.acceptance_criteria)
    assert "<html>正文</html>" in request.render()
    with pytest.raises(rv.ReviewMisuse) as caught:
        rv.build_request(card, [(ARTIFACT, "<html>正文</html>")], reviewer_agent_id=ACCEPTOR)
    assert caught.value.code == "reviewer_mismatch"


# ---------------------------------------------------------------- 三、身份来自 EN-04（PG）

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
def guarded(session) -> tuple[FeatureRequest, TaskCard]:
    """一张三个岗都填好的卡 + 种过前三岗的 EN-04。

    岗的挂载与指派都走**真生产者**（`role_registry.seed_builtin_roles` / 卡列直接写），
    因为本文件要判的正是"`read_only` 到底从哪儿来"。
    """
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
                workspace_dir=f"/tmp/{agent_id}-workspace",
                enabled=True,
            )
        )
    role_registry.seed_builtin_roles(session)
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
        acceptance_criteria=["首屏有标题", "样式表被引用"],
        handoff_count=0,
        review_round=0,
        status="REVIEWING",
        snapshot_id=snapshot_id,
        executor_agent_id=EXECUTOR,
        reviewer_agent_id=REVIEWER,
        acceptor_agent_id=ACCEPTOR,
    )
    session.add(card)
    session.flush()
    return request, card


@needs_pg
def test_the_guard_actor_identity_comes_from_en_04_not_from_the_caller(session, guarded):
    """T088 补缺口的那一句：`Actor.read_only` 的唯一来源是 EN-04 的列，不是请求方自述。

    三个岗各取一次身份：审查 / 验收 ⇒ `read_only=True`；执行 ⇒ `False`。
    这三条读的是同一份 `role_spec.read_only`，所以岗名一旦被换掉（比如把 reviewer 标成非只读），
    本条会红而不是继续绿 —— 它没有把"True"写死在自己的期望集合里。
    """
    request, card = guarded
    reviewer = actor_module.actor_for_seat(session, feature_request_id=request.id, task_id=card.id, seat="reviewer")
    acceptor = actor_module.actor_for_seat(session, feature_request_id=request.id, task_id=card.id, seat="acceptor")
    executor = actor_module.actor_for_seat(session, feature_request_id=request.id, task_id=card.id, seat="executor")
    assert (reviewer.read_only, acceptor.read_only) == (True, True)
    assert executor.read_only is False
    assert reviewer.actor_id == f"{acl.ADAPTER_OWNER_PREFIX}{REVIEWER}"
    assert {reviewer.role_id, acceptor.role_id} == set(ag.SEAT_ROLES.values())


@needs_pg
def test_an_unassigned_seat_has_no_identity_rather_than_a_default_one(session, guarded):
    """反向：指派没做过的席位取不出身份 ⇒ 抛，而不是发一个"看起来能用"的默认 Actor。

    默认的 `read_only=False` 是最坏的一种默认：它会把一次未指派的把关变成可写下发。
    """
    request, card = guarded
    card.reviewer_agent_id = None
    session.flush()
    with pytest.raises(actor_module.ActorMisuse) as caught:
        actor_module.actor_for_seat(session, feature_request_id=request.id, task_id=card.id, seat="reviewer")
    assert caught.value.code == "seat_unassigned"


@needs_pg
def test_the_read_only_reviewer_can_still_record_a_verdict(session, guarded, root):
    """**防假门禁的那一半**：三条写路径全被拒的同时，结论真能入库（FR-033 / FR-057）。

    顺序刻意是"先写被拒、后判结论"：只读岗如果连结论都出不了，看起来严格，实际把关消失了。
    结论入库走 PG（`review_verdict` 一行 + 卡状态一格），全程不写任何一个文件。
    """
    request, card = guarded
    actor = actor_module.actor_for_seat(
        session, feature_request_id=request.id, task_id=card.id, seat="reviewer"
    )
    for path in DENIED_PATHS:
        decision = acl.can_write(actor, path, acl.Capability(REVIEWER, "T01"))
        assert decision.allowed is False, (path, decision)

    findings = [
        rv.ReviewFinding("首屏有标题", True, f"{ARTIFACT}:1 有 <h1>"),
        rv.ReviewFinding("样式表被引用", False, f"{ARTIFACT} 里没有 <link> 引用"),
    ]
    recorded = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=findings,
        created_at=NOW,
    )
    session.refresh(card)
    assert recorded.verdict == rv.CHANGES_REQUESTED
    assert card.status == rv.CHANGES_REQUESTED
    assert card.review_round == 0, "审查结论不许占验收轮次那一列（owner 2026-10-14 裁决 2）"
    assert recorded.round_no == 1, "轮次号从 EN-08 数，并随留痕走，不占列"
    assert (root / layout.STATUS_MD).exists() is False, "审查岗把公共区写下去了"
