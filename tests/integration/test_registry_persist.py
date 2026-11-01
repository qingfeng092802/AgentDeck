"""T043 的判据：注册表的申报落到 EN-03 / EN-05，岗位比对读 EN-04 的生效口径（FR-011 / FR-012，契约 §3.1）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_orchestrate_graph.py`），
且**一律不 commit**：每个用例套一层外层事务、结束回滚 ⇒ 库保持可重复跑，`quickstart` 里"四表为 0"那条读数
不会因为跑收口用例而漂。

要回答的问题按 `src/acp/registry/persist.py` 的四条规则排：
R1 先全验后写（一支超出 ⇒ 整批不落）、R2 只写申报列（派生列不许被抹回 0）、
R3 同一时刻两份不同申报要抛、R4 比对读 EN-04 而不是读 yaml。
另有两条边界：空批次与裸 `datetime` 属**用法错**（`RegistryMisuse`），不与"申报被拒"混成一类。

第 1 条用例（`test_empty_en04_refuses_every_real_declaration`）刻意把"今天真跑必然全拒"钉成判据：
EN-04 还没有写者（内置岗位说明归 T087）⇒ 生效集合为空 ⇒ 任何申报都超出。**这是正确行为**，
所以它红的时候意味着有人给 `persist` 加了"表空就放行"的兜底 —— 那才是事故。
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.persistence.models_core import AgentUnit, CapabilityManifest
from acp.persistence.models_registry import RoleSpec
from acp.protocol.messages import CapabilityManifest as Declaration
from acp.registry import persist
from acp.registry.loader import Registration, load_registrations

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（落库与生效集合的比对只能在库里读）",
)

T1 = dt.datetime(2026, 10, 13, 8, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.timezone.utc)
FAKE_SECRET = "ark-fake-0123456789ABCDEF"


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
        transaction.rollback()  # 用例里的行一律不落库，库保持可重复跑


def declaration(**override) -> Declaration:
    base = {
        "agent_id": "doubao",
        "form": "API",
        "capabilities": ["text_generation", "summarization"],
        "tier": 2,
        "cost_band": "CHEAP",
        "max_parallel": 4,
        "role_mounts": ["reviewer"],
    }
    return Declaration(**{**base, **override})


def registration(manifest: Declaration | None = None, **override) -> Registration:
    return Registration(
        manifest=manifest or declaration(),
        adapter_entry=override.pop("adapter_entry", "acp.adapters.doubao:Transport"),
        enabled=override.pop("enabled", True),
    )


def seed_role(session: Session, role_id: str, agents: list[str], **override) -> None:
    """直接种 EN-04。生效口径的**写者**今天还不存在（内置岗位说明归 T087），所以用例自己造它。"""
    row = {
        "role_id": role_id,
        "name": role_id,
        "charter": f"{role_id} 岗位守则",
        "read_only": False,
        "builtin": True,
        "mounted_agents": agents,
    }
    session.add(RoleSpec(**{**row, **override}))
    session.flush()


def agent_rows(session: Session) -> dict[str, AgentUnit]:
    return {row.agent_id: row for row in session.scalars(select(AgentUnit)).all()}


def manifest_rows(session: Session, agent_id: str | None = None) -> list[CapabilityManifest]:
    stmt = select(CapabilityManifest).order_by(CapabilityManifest.declared_at)
    if agent_id is not None:
        stmt = stmt.where(CapabilityManifest.agent_id == agent_id)
    return list(session.scalars(stmt).all())


def count_of(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


# —— 一、生产路径今天的真状态：生效集合为空 ⇒ 全拒，且一行不落 ——


def test_empty_en04_refuses_every_real_declaration(session: Session):
    """没有批准就没有生效集合 ⇒ 四支真申报全部被拒；这是**正确行为**，不是缺陷。

    红法只有一种：有人给 `effective_mounts` 加了"表为空就放行"的兜底 ——
    那等于把"还没批准"读成"批准全部"，也正是契约 §3.1 要避免的那类静默降级。
    """
    with pytest.raises(persist.RegistrationRefused) as caught:
        persist.persist_registrations(session, None, declared_at=T1)
    assert "EN-04 无此行" in str(caught.value)
    assert count_of(session, AgentUnit) == 0
    assert count_of(session, CapabilityManifest) == 0


def test_all_real_declarations_land_once_en04_is_seeded(session: Session):
    """FR-012 / SC-001 的**库侧**证据：三形态各一支 + 审查身份真的进了 EN-03（不是只在 yaml 里声明过）。"""
    # owner 2026-10-13 裁决 2：`developer` 不作为岗存在 ⇒ qoder 的申报改成三个前置岗。
    # 岗的只读位按 FR-033 摆（审查员 / 验收员只读），本模块不读它，但 seed 要摆得跟真岗位一致。
    seed_role(session, "architect", ["workbuddy", "qoder"])
    seed_role(session, "reviewer", ["doubao", "qoder", "deepseek-reviewer"], read_only=True)
    seed_role(session, "acceptor", ["qoder"], read_only=True)

    report = persist.persist_registrations(session, load_registrations(), declared_at=T1)

    assert report.agents == ("workbuddy", "qoder", "doubao", "deepseek-reviewer")
    assert report.en03_created == ("workbuddy", "qoder", "doubao", "deepseek-reviewer")
    assert report.en03_updated == () and report.en05_inserted == 4 and report.en05_reused == 0
    rows = agent_rows(session)
    assert {row.form for row in rows.values()} == {"IN_PLATFORM", "SDK", "API"}
    for agent_id, unit in rows.items():
        assert unit.workspace_dir == f"{agent_id}-workspace", "目录名取 `layout.workspace_dir_name`，不在这里另拼一份"
        assert (unit.current_load, unit.success_rate_sample, unit.historical_success_rate) == (0, 0, None)
        assert unit.enabled is True


# —— 二、R4：比对读 EN-04，不读申报自己 ——


def test_guard_reads_en04_and_not_the_declaration(session: Session):
    """同一份输入，只动 EN-04 就从不通过变通过 ⇒ 生效口径的来源是库，不是 yaml。

    两次调用之间 `Registration` 与 `declared_at` **完全没变**，变的只有 `role_spec.mounted_agents`
    那一列 ⇒ 如果比对读的是申报自己（或 yaml），第二次不会过。"只把 yaml 的岗改多一个"那个方向
    本用例没测（它要靠 `test_registry_loader.py` 那侧证明申报会变），这里钉的是"生效集合在库里"。
    """
    seed_role(session, "reviewer", ["workbuddy"])
    item = registration()
    with pytest.raises(persist.RegistrationRefused) as caught:
        persist.persist_registrations(session, [item], declared_at=T1)
    assert "岗位 'reviewer' 的生效集合不含 'doubao'" in str(caught.value)
    assert count_of(session, AgentUnit) == 0, "被拒的那一支不许留下半行"

    # 只动 EN-04（赋一份新数组：ARRAY 列原地 append 不会触发变更检测，得整体换掉）
    row = session.get(RoleSpec, "reviewer")
    row.mounted_agents = ["workbuddy", "doubao"]
    session.flush()
    report = persist.persist_registrations(session, [item], declared_at=T1)
    assert report.en03_created == ("doubao",), "换了生效集合就能落 ⇒ 判据读的是 EN-04"


def test_over_mount_refuses_the_whole_batch_not_just_that_agent(session: Session):
    """R1：一支超出 ⇒ 整批不落。半支队伍进库比全拒更难查（三岗互斥与选人按"库里有谁"判定）。"""
    seed_role(session, "reviewer", ["doubao"])
    seed_role(session, "architect", ["workbuddy"])
    # qoder 在裁决 2 之后申报三个前置岗 ⇒ 它卡在**第一个** `architect`（该行只批了 workbuddy），
    # 断言的是"哪一支闯祸"，与它缺几个岗无关。
    batch = list(load_registrations())
    assert len(batch) == 4

    with pytest.raises(persist.RegistrationRefused) as caught:
        persist.persist_registrations(session, batch, declared_at=T1)
    assert caught.value.agent_id == "qoder", "要点名是哪一支闯的祸"
    assert count_of(session, AgentUnit) == 0 and count_of(session, CapabilityManifest) == 0


def test_union_is_not_taken(session: Session):
    """申报挂两岗、只有一岗生效 ⇒ 拒，而不是"把两岗并起来当已批准"（契约 §3.1 原话）。"""
    seed_role(session, "architect", ["doubao"])
    seed_role(session, "reviewer", ["workbuddy"])
    item = registration(manifest=declaration(role_mounts=["architect", "reviewer"]))
    with pytest.raises(persist.RegistrationRefused) as caught:
        persist.persist_registrations(session, [item], declared_at=T1)
    assert "reviewer" in str(caught.value) and "architect" not in caught.value.reason
    assert count_of(session, AgentUnit) == 0


# —— 三、R2 / R3：申报列与派生列、同一时刻的两份申报 ——


def test_redeclaring_updates_declaration_columns_only(session: Session):
    """R2：`current_load` / `historical_success_rate` / `success_rate_sample` 的权威源不在本模块，
    重新申报把它们抹回 0 = 覆盖别人的事实态。"""
    seed_role(session, "reviewer", ["doubao"])
    persist.persist_registrations(session, [registration()], declared_at=T1)
    unit = agent_rows(session)["doubao"]
    unit.current_load = 7
    unit.success_rate_sample = 5
    unit.historical_success_rate = Decimal("0.80")
    session.flush()

    persist.persist_registrations(session, [registration(manifest=declaration(tier=3, max_parallel=9))], declared_at=T2)

    again = agent_rows(session)["doubao"]
    assert (again.tier, again.max_parallel) == (3, 9), "申报列跟着变"
    assert (again.current_load, again.success_rate_sample, str(again.historical_success_rate)) == (
        7,
        5,
        "0.80",
    ), "派生列一个都不许被重新申报覆盖"
    assert count_of(session, AgentUnit) == 1, "EN-03 是生效快照，一支 agent 恰一行"


def test_same_timestamp_same_content_is_idempotent(session: Session):
    seed_role(session, "reviewer", ["doubao"])
    item = registration()
    first = persist.persist_registrations(session, [item], declared_at=T1)
    second = persist.persist_registrations(session, [item], declared_at=T1)
    assert (first.en05_inserted, first.en05_reused) == (1, 0)
    assert (second.en05_inserted, second.en05_reused) == (0, 1)
    assert second.en03_updated == ("doubao",)
    assert len(manifest_rows(session, "doubao")) == 1, "重跑注册不该留两行同键历史"


def test_same_timestamp_different_content_raises_timeline_conflict(session: Session):
    """R3：同键不同内容 ⇒ 抛，而且**旧行不许被覆盖**（覆盖就是给时间线编两个版本）。"""
    seed_role(session, "reviewer", ["doubao"])
    persist.persist_registrations(session, [registration()], declared_at=T1)
    with pytest.raises(persist.DeclarationTimelineConflict) as caught:
        persist.persist_registrations(session, [registration(manifest=declaration(tier=4))], declared_at=T1)
    assert "两份申报挂同一时刻" in str(caught.value)
    assert "tier=2" in str(caught.value) and "tier=4" in str(caught.value), "消息要能定位是哪两个值在打架"
    assert [row.tier for row in manifest_rows(session, "doubao")] == [2]


def test_new_timestamp_appends_history_row(session: Session):
    seed_role(session, "reviewer", ["doubao"])
    persist.persist_registrations(session, [registration()], declared_at=T1)
    persist.persist_registrations(session, [registration(manifest=declaration(tier=3))], declared_at=T2)
    assert [row.tier for row in manifest_rows(session, "doubao")] == [2, 3]
    assert manifest_rows(session, "doubao")[0].capabilities == ["text_generation", "summarization"]
    assert count_of(session, AgentUnit) == 1


# —— 四、用法错与边界 ——


def test_empty_batch_is_a_misuse_not_a_zero_registration(session: Session):
    """空批次报"成功注册 0 支"会把装配点缺件读成 fleet 为空 ⇒ 单独一档、且必须红。"""
    with pytest.raises(persist.RegistryMisuse) as caught:
        persist.persist_registrations(session, [], declared_at=T1)
    assert "成功注册 0 支" in str(caught.value)


def test_naive_declared_at_is_a_misuse(session: Session):
    """EN-05 的 `declared_at` 是 `timestamptz` 且是 PK 的一列：裸 datetime 会按会话时区解释。"""
    seed_role(session, "reviewer", ["doubao"])
    with pytest.raises(persist.RegistryMisuse) as caught:
        persist.persist_registrations(session, [registration()], declared_at=dt.datetime(2026, 10, 13, 8, 0))
    assert "timestamptz" in str(caught.value)


def test_disabled_registration_still_lands_with_enabled_false(session: Session):
    """`enabled` 是申报的一部分：注册即接入（FR-012）不等于"不能用"，停用位要能在库里读出来。"""
    seed_role(session, "reviewer", ["doubao"])
    persist.persist_registrations(session, [registration(enabled=False)], declared_at=T1)
    assert agent_rows(session)["doubao"].enabled is False


def test_refusal_message_does_not_echo_capabilities_or_credentials(session: Session):
    """拒注册的消息只放 agent_id 与两个集合：capabilities 里塞一个凭据形态的串，也不该被回显。

    点名一处刻意的不同：`DeclarationTimelineConflict` 会把两边的 capabilities 打出来，
    那是**申报内容**（不是凭据、也不是厂商自由文本），要定位冲突就得打；这条用例只管拒注册那一面。
    """
    seed_role(session, "reviewer", ["workbuddy"])
    item = registration(manifest=declaration(capabilities=[FAKE_SECRET]))
    with pytest.raises(persist.RegistrationRefused) as caught:
        persist.persist_registrations(session, [item], declared_at=T1)
    assert FAKE_SECRET not in str(caught.value)
    assert "doubao" in str(caught.value)


def test_the_module_never_commits(session: Session, monkeypatch):
    """事务边界归调用方（`persistence.session.new_session`）：本模块只 flush。

    做法是给会话的 `commit` 装一根绊线 —— 正向断言（"提交没发生"）在本 fixture 里读不出来
    （外层事务回滚了也照样看不出错），所以必须**主动装线**，否则这条等于没断言。
    """

    def boom(*args, **kwargs):
        raise AssertionError("persist 不许自己 commit（装配方负责工作单元的收尾）")

    monkeypatch.setattr(session, "commit", boom)
    seed_role(session, "reviewer", ["doubao"])
    report = persist.persist_registrations(session, [registration()], declared_at=T1)
    assert report.en05_inserted == 1
