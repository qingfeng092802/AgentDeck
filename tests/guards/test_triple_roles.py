"""T085 + T086 + T087 + T097：三岗互斥的**断言、路由与岗位前件**（FR-031 / FR-032 / FR-034 / FR-071｜R-09）。

档位：`tests/guards/`，混合文件 —— 断言本体与生效口径的三条关系式是纯读表（fast），
落库与三元组审计那几条要真 PG（各挂 `skipif`，**每条 PG 读数都注明"带 `.env`"**）。

本文件问五件事，每件只有一种坏法能把它撞红：

1. **同人就是同人**（FR-031）：判定单位是 `agent_id`，`role_id` 不参与 ——
   "同一支工具挂两个岗来自审"这个形状 MUST 可构造、且 MUST 被拒（②）。
2. **信息不全不算通过**（FR-031 的那一半人会忘）：席位为空 ⇒ 拒，而不是"`None` 与字符串不等 ⇒ 通过"（⑤）。
   `models_core.py:99-100` 把这句话直接写在了列注释里，指定由 `guard/triple.py` 处理这一态。
3. **矩阵真能闭合**（FR-032）：三种执行者下都能填出两个不同的把关人（⑧），
   且总管执行时收窄到白名单（⑨）—— 收窄是一条**独立的界**，与"排除执行者"不是同一件事的两种写法。
4. **断言在写库之前**（FR-034）：不合法 ⇒ 一列都不落（⑬），不留"已指派但不独立"的半成品给下一步去派发。
5. **生效口径是自洽的**（FR-071 + 注册路径）：EN-04 的挂载集合 ⊇ `agents.yaml` 的申报（⑦），
   否则生产路径上三支工具全注册不上，而那条失败会在很远的地方才显形。

反向对照（缺了就等于判据恒红）：
- ①与②配成一对：三个不同工具 ⇒ 通过；同一工具换岗 ⇒ 拒。只留②的话，"任何指派都拒"的实现也绿。
- ⑤的 `assert_independent`（要齐）与 `assert_distinct_so_far`（不管齐）各一条：
  只判前者会让 T071 覆盖端点在把关位还是空的时候被误拒 —— 那是"判据过严"那种红，也是缺陷。
- ⑨配一条"执行者不是总管时，白名单不生效"（⑧ 里 executor=doubao 那一支就覆盖了它）。
- ⑩⑪是**源码面**的守卫（⑩ 定的是"没有一个 `except` 体能 `return`"，不是"本模块不许出现 `try`" ——
  批量那个函数合法地用 `try` 收集逐卡理由后再整批抛；第一版按"不许有 try"写，被这条真实结构当场撞红）。
  它们的存在理由与 `test_select_worker.py` 的 `dev_model` 那组同源：行为对照抓不到的"结构性禁令"
  只能扫源码，而扫源码的判据 MUST 有开火对照，否则解析器整体失灵时它也绿。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.guard import triple as gp
from acp.orchestrate import graph as og
from acp.orchestrate import scheduler as sc
from acp.orchestrate.nodes import assign_guards as ag
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, RoleSpec, Snapshot
from acp.registry import roles as role_registry
from acp.registry.loader import load_registrations
from acp.registry.persist import persist_registrations
from acp.runtime.slot_manager import InProcessSlotManager

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 7, 50, tzinfo=dt.timezone.utc)
GUARD_SRC = Path(__file__).resolve().parents[2] / "src/acp/guard/triple.py"


def seat(executor, reviewer, acceptor, *, roles: dict[str, str] | None = None) -> gp.Assignment:
    """一份指派。`roles` 让"同工具换岗"这个形状可以被构造出来（键是席位名，值是岗名）。"""
    mounts = roles or {}
    return gp.Assignment(
        "T01",
        {
            seat_name: gp.SeatOccupant(agent, mounts.get(seat_name))
            for seat_name, agent in (
                ("executor", executor),
                ("reviewer", reviewer),
                ("acceptor", acceptor),
            )
        },
    )


def candidate(agent_id: str, *roles: str) -> ag.Candidate:
    return ag.Candidate(agent_id=agent_id, mounted_roles=frozenset(roles))


# ---------------------------------------------------------------- 一、断言本体（fast）

def test_three_distinct_tools_pass_and_the_triple_comes_back_from_the_guard():
    """①：三个不同工具 ⇒ 通过，且返回的就是那三个人的三元组（留痕用的值是**判过的**，不是调用方自备的）。"""
    triple = gp.assert_independent(seat("qoder", "workbuddy", "doubao"))
    assert triple == {"executor": "qoder", "reviewer": "workbuddy", "acceptor": "doubao"}, triple
    assert list(triple) == list(gp.SEATS), "三元组的键序 = 席位序，审计行按它呈现"


def test_the_same_tool_on_two_seats_is_refused_even_with_different_role_ids():
    """②：同一支工具挂两个岗 —— **判定单位是工具，岗名不参与**（FR-031 / 宪法原则 III）。

    `role_id` 给了两个不同的值（`reviewer` / `acceptor`）正是这个形状的关键：
    实现若比较的是 `(agent_id, role_id)` 对，这一条就会绿 —— 那变异对照专门打这一枪。
    """
    with pytest.raises(gp.TripleViolation) as caught:
        gp.assert_independent(seat("qoder", "qoder", "doubao", roles={"reviewer": "acceptor"}))
    assert caught.value.code == "not_independent"
    assert any(f"判定单位是 {gp.UNIT}" in reason for reason in caught.value.violations), caught.value.violations


def test_a_missing_seat_is_refused_rather_than_read_as_naturally_distinct():
    """⑤：可空列 = "指派尚未发生"，MUST NOT 当成"两两不同天然成立"。"""
    with pytest.raises(gp.TripleViolation) as caught:
        gp.assert_independent(seat("qoder", "workbuddy", None))
    assert caught.value.code == "seat_missing"
    assert any("acceptor" in reason for reason in caught.value.violations), caught.value.violations


def test_the_override_assertion_tolerates_empty_seats_but_still_refuses_the_same_tool():
    """⑤ 的另一半：覆盖路径（T071）跑在 `AssignGuards` 之前，把关位通常还是空的。

    `assert_distinct_so_far` 对空位 MUST 放行（否则改派端点在合法时机上被误拒），
    对同人 MUST 仍拒（否则就绕过了 FR-031）。两个方向各一次调用，缺一半都算没钉住这两个入口的分工。
    """
    assert gp.assert_distinct_so_far(seat("qoder", None, None)) == {
        "executor": "qoder",
        "reviewer": None,
        "acceptor": None,
    }
    with pytest.raises(gp.TripleViolation) as caught:
        gp.assert_distinct_so_far(seat("qoder", "qoder", None))
    assert caught.value.code == "not_independent"


def test_an_unexpected_seat_set_is_refused_not_silently_underjudged():
    """⑥：席位集合与 `SEATS` 不一致 ⇒ 拒。多出来的岗没人判、少了的岗判不到，都是"看起来判过了"的假象。"""
    assignment = gp.Assignment(
        "T01",
        {"executor": gp.SeatOccupant("qoder"), "reviewer": gp.SeatOccupant("workbuddy")},
    )
    with pytest.raises(gp.TripleViolation) as caught:
        gp.assert_independent(assignment)
    assert caught.value.code == "seat_count_mismatch"


def test_the_batch_is_refused_as_a_whole_when_one_card_is_bad():
    """整批不指（与 `assert_closable` 同一理由）：逐卡填到一半再抛，库里留下一份部分指派。"""
    good = gp.Assignment("T02", {"executor": gp.SeatOccupant("qoder"), "reviewer": gp.SeatOccupant("workbuddy"), "acceptor": gp.SeatOccupant("doubao")})
    bad = gp.Assignment("T01", {"executor": gp.SeatOccupant("qoder"), "reviewer": gp.SeatOccupant("qoder"), "acceptor": gp.SeatOccupant("doubao")})
    with pytest.raises(gp.TripleViolation) as caught:
        gp.assert_batch_independent([good, bad])
    assert len(caught.value.violations) == 1, caught.value.violations
    assert caught.value.violations[0].startswith("T01:"), caught.value.violations
    # 反向：全批合法时返回的条数 = 卡数，一条都没被吞。
    assert set(gp.assert_batch_independent([good])) == {"T02"}


def test_single_tool_registry_cannot_close_the_matrix_and_says_so():
    """③：只接入一支工具 ⇒ 不可闭合，理由是"凑不出两两不同"，不是"没找到"。"""
    pool = [candidate("qoder", "reviewer", "acceptor", "architect")]
    with pytest.raises(ag.GuardNotClosable) as caught:
        ag.route("T01", "qoder", pool)
    assert any("少于 2 支" in reason for reason in caught.value.reasons), caught.value.reasons
    assert any("升卡点 B" in reason for reason in caught.value.reasons), "FR-034 的出口要在理由里点名"


# ---------------------------------------------------------------- 二、矩阵闭合路由（fast）

def test_the_orchestrator_whitelist_is_a_separate_bound_from_the_self_review_ban():
    """⑨：执行者是总管时收窄为 `{qoder, doubao}` —— 这一条**比"排除执行者"更严**。

    构造形状：池子里有第四支工具 `cursor`，它挂着 reviewer / acceptor 岗，且不是执行者。
    只实现"不等于执行者"的话，`cursor` 会被选进把关位（那在宪法 rule 2 里是不允许的）；
    只实现白名单而不实现排除执行者的话，总管自己会进候选。两个方向都由这一条抓到。
    """
    pool = [
        candidate("workbuddy", "architect", "reviewer", "acceptor"),
        candidate("qoder", "reviewer", "acceptor"),
        candidate("doubao", "reviewer", "acceptor"),
        candidate("cursor", "reviewer", "acceptor"),
    ]
    picked = ag.route("T01", ag.ORCHESTRATOR_AGENT, pool)
    assert picked.narrowed is True
    assert {picked.reviewer, picked.acceptor} <= set(ag.ORCHESTRATOR_GUARD_WHITELIST), picked.triple()
    assert "cursor" not in (picked.reviewer, picked.acceptor)
    assert "cursor" not in picked.eligible["reviewer"], "候选表里还留着 cursor ⇒ 收窄发生在选完之后"


def test_the_whitelist_does_not_apply_when_the_executor_is_not_the_orchestrator():
    """上一条的反向：执行者是普通工具时，总管**可以**被指派为把关人（宪法 rule 3）。"""
    pool = [
        candidate("workbuddy", "architect", "reviewer", "acceptor"),
        candidate("qoder", "reviewer", "acceptor"),
        candidate("doubao", "reviewer", "acceptor"),
    ]
    picked = ag.route("T01", "doubao", pool)
    assert picked.narrowed is False
    assert ag.ORCHESTRATOR_AGENT in picked.eligible["reviewer"], picked.eligible
    assert len({picked.reviewer, picked.acceptor}) == 2


def test_route_output_always_satisfies_the_guard():
    """⑫ 符合性：路由的产物 MUST 通过 `guard/triple.py` 的断言。

    两条判据来自两个文件（路由规则 vs 宪法同人），这一条把它们钉成**同一件事的两面**：
    任一侧重写而另一侧没跟，这条就红 —— 而不是各绿各的。
    """
    pool = [
        candidate("workbuddy", "architect", "reviewer", "acceptor"),
        candidate("qoder", "reviewer", "acceptor"),
        candidate("doubao", "reviewer", "acceptor"),
    ]
    for executor in ("workbuddy", "qoder", "doubao"):
        triple = ag.route("T01", executor, pool).triple()
        assert gp.assert_independent(
            gp.Assignment("T01", {seat_name: gp.SeatOccupant(agent) for seat_name, agent in triple.items()})
        ) == triple, executor


# ---------------------------------------------------------------- 三、前三岗与生效口径（T087）

def test_the_three_builtin_roles_exist_with_the_documented_read_only_flags():
    """T087：MVP-1 交前三岗；`read_only` 是两条不同的要求 —— 审查/验收只读，架构师不是。"""
    declared = {role.role_id: role for role in role_registry.BUILTIN_ROLES}
    assert set(declared) == {"architect", "reviewer", "acceptor"}, set(declared)
    assert declared["reviewer"].read_only is True and declared["acceptor"].read_only is True
    assert declared["architect"].read_only is False, "架构师是总管岗，可写自己那一份 workspace（守则第 4 条）"


def test_every_charter_carries_both_required_sections_and_is_not_the_constant_it_claims(tmp_path, monkeypatch):
    """FR-071 ① 要求正文含"岗位定位 + 岗位守则"两节；缺任一节 ⇒ `read_charter()` 直接抛。

    开火对照在**副本目录**上做（`ROLE_DIR` 是模块级取数口，注进去就够）：
    不碰工作树里那份真说明 —— 测试改自己的 `src/` 会在并行跑时互相踩，而且"改回来"这一步本身就是个风险动作。
    """
    for role in role_registry.BUILTIN_ROLES:
        text = role_registry.read_charter(role.role_id)
        assert "## 岗位定位" in text and "## 岗位守则" in text, role.role_id
        assert len(text) > 200, f"{role.role_id} 的正文短到不可能含守则"

    mirror = tmp_path / "roles"
    mirror.mkdir()
    for role in role_registry.BUILTIN_ROLES:
        mirror.joinpath(f"{role.role_id}.md").write_text(
            role_registry.charter_path(role.role_id).read_text(encoding="utf-8"), encoding="utf-8"
        )
    monkeypatch.setattr(role_registry, "ROLE_DIR", mirror)
    assert role_registry.read_charter("reviewer"), "副本没接上 ⇒ 下面的注入打的是空气"

    mirror.joinpath("reviewer.md").write_text(
        mirror.joinpath("reviewer.md").read_text(encoding="utf-8").split("## 岗位守则")[0],
        encoding="utf-8",
    )
    with pytest.raises(role_registry.RoleSpecConflict, match="岗位守则"):
        role_registry.read_charter("reviewer")
    mirror.joinpath("acceptor.md").write_text("", encoding="utf-8")
    with pytest.raises(role_registry.RoleSpecConflict, match="是空的"):
        role_registry.read_charter("acceptor")
    mirror.joinpath("architect.md").unlink()
    with pytest.raises(role_registry.RoleSpecConflict, match="缺席"):
        role_registry.read_charter("architect")


def test_effective_mounts_cover_every_declared_role_mount():
    """⑦：EN-04 生效口径 MUST ⊇ `agents.yaml` 的申报，否则注册节点 R4 会把三支工具全拒掉。

    这条不跑注册路径也能提前红 —— 等注册来发现，报错的地点离原因已经很远（而且是"看起来像配置问题"）。
    """
    declared_pairs = {
        (registration.manifest.agent_id, role_id)
        for registration in load_registrations()
        for role_id in registration.manifest.role_mounts
    }
    effective = {
        (agent_id, role_id) for role_id, agents in role_registry.MOUNTS.items() for agent_id in agents
    }
    missing = sorted(declared_pairs - effective)
    assert missing == [], f"申报了但生效口径里没有的岗挂载：{missing}"
    assert set(role_registry.MOUNTS) == {role.role_id for role in role_registry.BUILTIN_ROLES}


def test_the_guard_seat_roles_are_the_seeded_role_ids_not_a_second_naming():
    """席位要求的那个岗名，MUST 就是 EN-04 里那一行的 `role_id`（两处各写一套名字就会互不相认）。"""
    assert set(ag.SEAT_ROLES.values()) == {"reviewer", "acceptor"}
    assert set(ag.SEAT_ROLES.values()) <= {role.role_id for role in role_registry.BUILTIN_ROLES}


# ---------------------------------------------------------------- 四、源码面的守卫（fail-open 与判定单位）

def test_no_handler_in_the_guard_can_swallow_a_violation():
    """⑩：宪法与 `plan.md:256` 都写了"代码里不写 fail-open 路径"—— 扫源码，不靠人记。

    **形状定的是"没有一个 `except` 体里能 `return`"**，不是"这个文件不许有 try"：
    `assert_batch_independent` 确实有一个 `try`，它抓住单卡的违例是为了**收集**逐卡理由，
    然后在末尾整批 `raise` —— 那是聚合不是放行。第一版我把判据写成"不许出现 try"，
    当场被这条真实结构撞红（现跑读数：`[178] != []`），所以口径按"能不能吞"收紧而不是按"有没有 try"。

    对照三发（都在内存里，不碰工作树）：
    - 处理器里 `return {}` ⇒ MUST 抓到（fail-open 的形状）；
    - 处理器里只 `raise` ⇒ MUST NOT 抓到（本模块合法用的那种）；
    - 完全没有 try ⇒ 也 MUST NOT 抓到（否则这条会变成恒红）。
    """
    def swallowed(source: str) -> list[int]:
        tree = ast.parse(source)
        return [
            handler.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if any(isinstance(inner, ast.Return) for inner in ast.walk(handler))
        ]

    tree = ast.parse(GUARD_SRC.read_text(encoding="utf-8"))
    handlers = [
        handler
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    assert handlers, "本模块的 try 结构变了 ⇒ 先确认这几处都在收集还是开始吞了"
    assert swallowed(GUARD_SRC.read_text(encoding="utf-8")) == [], (
        f"这些 except 体里有 return（行 {swallowed(GUARD_SRC.read_text(encoding='utf-8'))}）⇒ 有一处能吞掉违例"
    )

    assert swallowed("def p(a):\n    try:\n        return check(a)\n    except Exception:\n        return {}\n")
    assert swallowed("def p(a):\n    try:\n        return check(a)\n    except ValueError as e:\n        raise Bad(e)\n") == []
    assert swallowed("def p(a):\n    return check(a)\n") == []


def test_role_id_never_appears_inside_a_comparison_in_the_guard_source():
    """⑪：判定单位这条是"不含 `role_id`"，用 AST 比较运算里的名字来钉。

    行为面（②）只能抓"比较里真的用了 role_id"的实现；这条抓的是同一件事的源码形态，
    两者不重复：②会在"把比较对象换成对"时红，这条会在"新增一处读 `role_id` 的比较"时先红，
    报出来的位置就是该改的那一行。
    """
    tree = ast.parse(GUARD_SRC.read_text(encoding="utf-8"))
    reads_in_comparisons: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        names = {
            child.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Attribute)
        } | {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        if "role_id" in names:
            reads_in_comparisons.append(node.lineno)
    assert reads_in_comparisons == [], f"这些比较运算读了 role_id：{reads_in_comparisons}"

    injected = "def probe(x):\n    return (x.agent_id, x.role_id) != (y.agent_id, y.role_id)\n"
    fired = [
        node.lineno
        for node in ast.walk(ast.parse(injected))
        if isinstance(node, ast.Compare)
        and any(isinstance(child, ast.Attribute) and child.attr == "role_id" for child in ast.walk(node))
    ]
    assert fired, "对照没开火 ⇒ 上面那条断言是空的"


# ---------------------------------------------------------------- 五、落库与审计（PG）

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


def seed_unit(session, agent_id: str) -> None:
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


@pytest.fixture
def guarded_project(session) -> tuple[FeatureRequest, TaskCard]:
    """一张 READY 卡 + 执行者已定（`qoder`）+ EN-04 已种前三岗。

    执行者直接写列而不是走 `select_worker.apply_selection()`：本文件判的是把关，
    选人那一段在 `test_select_worker.py` 里已经有自己的判据，这里再跑一遍只会让两文件的失败互相纠缠。
    """
    for agent_id in ("workbuddy", "qoder", "doubao"):
        seed_unit(session, agent_id)
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
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="READY",
        snapshot_id=snapshot_id,
        executor_agent_id="qoder",
    )
    session.add(card)
    session.flush()
    return request, card


@needs_pg
def test_the_triple_is_persisted_on_the_card_and_in_one_audit_row(session, guarded_project):
    """④：三元组落盘可查（G3）—— 卡上两列 + EN-14 一行，两处读出来必须同一个值。"""
    request, card = guarded_project
    picked = ag.assign(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        created_at=NOW,
        actor_agent_id=sw.AUTOMATED_ACTOR,
    )
    session.refresh(card)
    assert (card.reviewer_agent_id, card.acceptor_agent_id) == (picked.reviewer, picked.acceptor)
    rows = list(
        session.scalars(
            select(EventLog).where(
                EventLog.entity_id == card.id, EventLog.kind == "TASK"
            )
        ).all()
    )
    guard_rows = [row for row in rows if (row.payload or {}).get("node") == ag.NODE]
    assert len(guard_rows) == 1, [row.payload for row in rows]
    assert guard_rows[0].payload["triple"] == picked.triple(), guard_rows[0].payload
    assert {card.executor_agent_id, card.reviewer_agent_id, card.acceptor_agent_id} == set(picked.triple().values())


@needs_pg
def test_the_registry_derived_pool_closes_the_matrix_for_every_possible_executor(session, guarded_project):
    """⑧：拿**真 EN-04**（不是用例里手搓的池子）把三种执行者各跑一遍。

    这一条是 `MOUNTS` 那三行取数的产品形态判据：它不问"看起来够不够"，问"每一种执行者都填得满吗"。
    """
    pool = ag.pool_from_role_specs(session)
    assert {candidate.agent_id for candidate in pool} == {"workbuddy", "qoder", "doubao"}, pool
    for executor in ("workbuddy", "qoder", "doubao"):
        picked = ag.route("T01", executor, pool)
        assert len({picked.executor, picked.reviewer, picked.acceptor}) == 3, (executor, picked.triple())


@needs_pg
def test_the_seed_is_idempotent_and_refuses_to_overwrite_the_effective_set(session, guarded_project):
    """T087 的种子：同内容再跑 = no-op；内容不同 = 抛。静默覆盖生效口径等于在不留痕的情况下改批准面。"""
    assert role_registry.seed_builtin_roles(session) == {"inserted": 0, "unchanged": 3}
    row = session.scalars(select(RoleSpec).where(RoleSpec.role_id == "reviewer")).one()
    session.execute(
        RoleSpec.__table__.update().where(RoleSpec.role_id == "reviewer").values(read_only=False)
    )
    session.flush()
    # `__table__.update()` 走的是 Core 批量语句，SQLAlchemy 不会替 ORM 同步身份映射里的对象
    # （现跑第一次就栽在这里：不 `expire_all()` 时种子读到的还是那份**旧的** `read_only=True`，
    # 于是"内容不同必须抛"这条根本没开火）。这不是种子的缺陷 —— 同一个工作单元里读到快照是 ORM 的语义，
    # 但判据必须显式制造"库里真的变了"这个状态，否则它绿的是自己的缓存。
    session.expire_all()
    with pytest.raises(role_registry.RoleSpecConflict, match="reviewer"):
        role_registry.seed_builtin_roles(session)


def seats_only_slots() -> InProcessSlotManager:
    """一个只为"段能跑完"存在的瞬态层：上限给到不会在这一发上撞界。

    本文件不测派发，所以这个数不参与任何断言 —— 它存在的唯一理由是 2026-10-16 裁 甲 之后
    `invoke()` 会真走到 `DISPATCH` 节点，没有瞬态层就抛 `MissingSession`/`AttributeError`，
    于是三岗那条判据会红在**不相干**的地方。第三份同名构造（`test_dispatch.py`、
    `test_dispatch_edge.py` 各一份）是刻意的重复：测试现场不共享池子，一个用例改坏参数
    不该把另外两档带成红。
    """
    return InProcessSlotManager(
        per_agent_max_parallel={"qoder": 3, "workbuddy": 3, "doubao": 3},
        platform_limit=3,
    )


@needs_pg
def test_the_assignment_segment_is_a_graph_and_fills_all_three_seats(session):
    """T086 / T073 的**接线形状**：`SelectWorker → AssignGuards → Dispatch` 是图上的节点，不是脚本里的顺序调用。

    为什么这条要单独钉（而不是只测 `assign()` 这个函数）：宪法原则 II 定的是"LangGraph 是唯一编排大脑"。
    把这两步写成装配点里的顺序调用，函数测试照样全绿，但图上少两个节点 —— 恢复语义、审计顺序、
    以及"卡点该挂在哪"这三件事就都没有载体了。

    三段断言各管一件事：
    ① 拓扑 = 三个节点、全段普通边、通向终点的入口只有 `dispatch`；
    ② 跑完一遍，卡上三个岗**两两不同**且 `guard/triple.py` 认这份三元组（真 EN-04 + 真 EN-05，不是手搓池子）；
    ③ 图上没有 `SlotHandle` 那种瞬态值 —— state 里只出现四个可安全重放的键。

    **【2026-10-16 裁 甲 的有意改写，原文不删】** 本条原来叫
    `test_the_assignment_segment_is_a_two_node_graph_and_fills_all_three_seats`，① 的期望值是
    "两个节点、通向终点的入口只有 `assign_guards`"（原 563 / 567 行）。owner 裁 甲 把
    `ASSIGN_GUARDS → DISPATCH` 那条边补上之后，那个"止于指派"的拓扑不再是实况 ⇒ ①跟着改、
    函数名里的 `two_node` 一起摘掉（留着就是一个说谎的名字）。
    **②③ 两句一个字没改**：三岗互斥的判定、以及"state 里只有可重放键"这两件事与这次改动无关，
    它们仍然必须红在原来那两种坏法上。名字改口的对偶是 `test_dispatch_edge.py` 节 一；
    本条只多了一件事 —— 段现在会占槽，所以现场得给它一个瞬态层（`seats_only_slots()`），
    台账用完即弃：这条测的是指派，不是借还，借还归 `test_dispatch_edge.py` 节 三。
    """
    role_registry.seed_builtin_roles(session)
    report = persist_registrations(session, declared_at=NOW)
    assert sorted(report.agents) == ["deepseek-reviewer", "doubao", "qoder", "workbuddy"], report.agents

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
    session.add(
        TaskCard(
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
            status="READY",
            snapshot_id=snapshot_id,
        )
    )
    session.flush()

    graph = og.build_assignment_graph(
        og.AssignmentDeps(
            session=session,
            created_at=NOW,
            actor_agent_id=sw.AUTOMATED_ACTOR,
            slots=seats_only_slots(),
            leases=sc.LeaseLedger(),
        )
    )
    shape = graph.get_graph()
    names = {node for node in shape.nodes if not node.startswith("__")}
    assert names == {og.SELECT_WORKER, og.ASSIGN_GUARDS, og.DISPATCH}, sorted(names)
    assert [(e.target, e.conditional) for e in shape.edges if e.source == og.SELECT_WORKER] == [
        (og.ASSIGN_GUARDS, False)
    ], "选人之后是把关指派，且不是条件边"
    assert [e.source for e in shape.edges if e.target == "__end__"] == [og.DISPATCH]

    result = graph.invoke({"feature_request_id": request.id, "task_id": "T01"})
    assert set(result) <= {"feature_request_id", "task_id", "executor_agent_id", "reviewer_agent_id", "acceptor_agent_id"}, result
    triple = {
        key: result[key]
        for key in ("executor_agent_id", "reviewer_agent_id", "acceptor_agent_id")
    }
    assert len({value for value in triple.values()}) == 3, triple
    by_seat = {seat: triple[f"{seat}_agent_id"] for seat in gp.SEATS}
    assert gp.assert_independent(
        gp.Assignment("T01", {seat: gp.SeatOccupant(agent) for seat, agent in by_seat.items()})
    ) == by_seat
    session.expire_all()
    card = session.get(TaskCard, (request.id, "T01"))
    assert (card.executor_agent_id, card.reviewer_agent_id, card.acceptor_agent_id) == (
        triple["executor_agent_id"],
        triple["reviewer_agent_id"],
        triple["acceptor_agent_id"],
    )


@needs_pg
def test_a_refused_assignment_writes_no_column_and_no_audit_row(session, guarded_project):
    """⑬：断言/判定在写库之前 —— 被拒时一列都不落，也不留痕。

    构造形状：卡还没有执行者（`executor_agent_id` 为空是 EN-02 的合法态，T-14 之后确实会出现）。
    此时规则 1（把关工具 ≠ 执行者）无从判定 ⇒ `route()` 抛，而两个把关位 MUST 仍是空的。
    反过来说：把这段顺序改成"先落库再判"，本条就会红在两列被写上。
    """
    request, card = guarded_project
    card.executor_agent_id = None
    session.flush()
    with pytest.raises(ag.GuardNotClosable, match="还没有执行者"):
        ag.assign(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            created_at=NOW,
            actor_agent_id="probe",
        )
    session.expire_all()
    fresh = session.get(TaskCard, (request.id, card.id))
    assert (fresh.executor_agent_id, fresh.reviewer_agent_id, fresh.acceptor_agent_id) == (None, None, None)
    assert session.scalars(
        select(EventLog).where(EventLog.entity_id == card.id, EventLog.kind == "TASK")
    ).first() is None, "抛过了还留了一条审计行"
