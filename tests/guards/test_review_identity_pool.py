"""Phase 17 P3：`deepseek-reviewer` 身份注册与模式化四选人池。

本文件钉四件事，每件都有一种**会静默坏掉**的写法：

1. **身份只挂审查**：注册表里它存在、EN-04 里只在 reviewer 一行出现。漏掉挂载 ⇒ 注册节点整批拒；
   多挂一个执行/验收岗 ⇒ 它可能被派去干活。
2. **manual 模式逐字不变**：缺省池 = Phase 16 的三支 + 相同三元组。把 `deepseek-reviewer`
   漏进默认池时，`route()` 的字典序会把它排到审查席最前面 —— 人工模式会被悄悄换成模型审查。
3. **deepseek 模式纳入它**：非总管执行的卡，审查席收窄到它；总管执行的卡受宪法规则 2 约束，
   仍走 qoder/doubao 的人工审查（这不是模式失效，是宪法优先）。
4. **执行选人永远排除它**：它即使被 EN-03/EN-05 完整登记，也不进 `candidates_from_registry`；
   否则一张带 `code_review` 标签的卡会把审查身份拉去执行。

档位：注册面与前两条纯读表（fast）；池子与选人那几条要真 PG（各挂 `skipif`，读数注明带 `.env`）。
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import assign_guards as ag
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence.models_core import AgentUnit, CapabilityManifest
from acp.registry import roles as role_registry
from acp.registry.loader import load_registrations
from acp.review.policy import DEEPSEEK_REVIEWER, REVIEW_MODE_DEEPSEEK, REVIEW_MODE_MANUAL

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
THREE = ("workbuddy", "qoder", "doubao")


# ---------------------------------------------------------------- 一、注册面（fast）


def test_registry_declares_the_review_identity_as_review_only():
    registrations = {item.agent_id: item for item in load_registrations()}
    assert sorted(registrations) == ["deepseek-reviewer", "doubao", "qoder", "workbuddy"]

    item = registrations[DEEPSEEK_REVIEWER]
    assert item.manifest.form == "API"
    assert list(item.manifest.role_mounts) == ["reviewer"]
    assert list(item.manifest.capabilities) == ["code_review", "criteria_evaluation", "test_review"]
    assert item.manifest.max_parallel == 1
    assert item.adapter_entry == "acp.adapters.deepseek:ReviewEntry"
    assert item.resolve().__name__ == "ReviewEntry"


def test_effective_mounts_put_the_identity_on_the_review_seat_only():
    assert DEEPSEEK_REVIEWER in role_registry.MOUNTS["reviewer"]
    for role in ("architect", "acceptor"):
        assert DEEPSEEK_REVIEWER not in role_registry.MOUNTS[role], role
    declared = {
        (registration.manifest.agent_id, role_id)
        for registration in load_registrations()
        for role_id in registration.manifest.role_mounts
    }
    effective = {
        (agent_id, role_id) for role_id, agents in role_registry.MOUNTS.items() for agent_id in agents
    }
    assert declared <= effective


# ---------------------------------------------------------------- 二、池子与路由（PG）


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


def _seed_roles(session: Session) -> None:
    role_registry.seed_builtin_roles(session)


def _seed_units_and_manifests(session: Session) -> None:
    """四支工具都完整登记（EN-03 + EN-05），让"审查身份会不会被执行选人挑走"可构造。"""
    for agent_id in (*THREE, DEEPSEEK_REVIEWER):
        session.add(
            AgentUnit(
                agent_id=agent_id,
                form="API",
                tier=2,
                cost_band="CHEAP",
                max_parallel=2,
                current_load=0,
                historical_success_rate=Decimal("0.5"),
                success_rate_sample=4,
                workspace_dir=f"{agent_id}-workspace",
                enabled=True,
            )
        )
        session.add(
            CapabilityManifest(
                agent_id=agent_id,
                declared_at=NOW,
                capabilities=["frontend_coding", "code_review"],
                tier=2,
                cost_band="CHEAP",
                max_parallel=2,
            )
        )
    session.flush()


@needs_pg
def test_manual_pool_is_phase_16_verbatim(session):
    _seed_roles(session)
    pool = ag.pool_from_role_specs(session)  # 缺省 = manual
    assert [(item.agent_id, set(item.mounted_roles)) for item in pool] == [
        ("doubao", {"reviewer"}),
        ("qoder", {"acceptor", "architect", "reviewer"}),
        ("workbuddy", {"acceptor", "architect", "reviewer"}),
    ]
    assert DEEPSEEK_REVIEWER not in {item.agent_id for item in pool}

    # Phase 16 的三种执行者各自的三元组（字典序最小合法配对）逐字不变。
    expected = {
        "workbuddy": ("doubao", "qoder"),
        "qoder": ("doubao", "workbuddy"),
        "doubao": ("qoder", "workbuddy"),
    }
    for executor, (reviewer, acceptor) in expected.items():
        picked = ag.route("T01", executor, pool)
        assert (picked.reviewer, picked.acceptor) == (reviewer, acceptor), executor


@needs_pg
def test_deepseek_pool_includes_the_review_identity(session):
    _seed_roles(session)
    pool = ag.pool_from_role_specs(session, mode=REVIEW_MODE_DEEPSEEK)
    by_id = {item.agent_id: set(item.mounted_roles) for item in pool}
    assert by_id[DEEPSEEK_REVIEWER] == {"reviewer"}
    assert set(THREE) <= set(by_id), "其余三支仍要在池里（执行/验收席靠它们）"

    picked = ag.route("T01", "qoder", pool, mode=REVIEW_MODE_DEEPSEEK)
    assert picked.reviewer == DEEPSEEK_REVIEWER
    assert picked.acceptor == "workbuddy", "验收席不许被审查身份占走"


@needs_pg
def test_deepseek_mode_keeps_the_constitutional_whitelist_for_the_orchestrator(session):
    """执行者 = 总管时规则 2 优先：审查只能是 qoder / doubao，不能是模型身份。"""
    _seed_roles(session)
    pool = ag.pool_from_role_specs(session, mode=REVIEW_MODE_DEEPSEEK)
    picked = ag.route("T01", "workbuddy", pool, mode=REVIEW_MODE_DEEPSEEK)
    assert picked.reviewer in {"qoder", "doubao"}, picked.triple()
    assert picked.reviewer != DEEPSEEK_REVIEWER
    assert picked.acceptor in {"qoder", "doubao"}


@needs_pg
def test_review_identity_never_lands_on_the_executor_or_acceptor_seat(session):
    _seed_roles(session)
    pool = ag.pool_from_role_specs(session, mode=REVIEW_MODE_DEEPSEEK)
    for executor in THREE:
        picked = ag.route("T01", executor, pool, mode=REVIEW_MODE_DEEPSEEK)
        assert picked.executor == executor
        assert picked.acceptor != DEEPSEEK_REVIEWER, (executor, picked.triple())
    assert {item.agent_id for item in pool if item.can("acceptor")} == {"qoder", "workbuddy"}


@needs_pg
def test_execution_candidates_exclude_the_review_identity(session):
    _seed_units_and_manifests(session)
    candidates, dropped = sw.candidates_from_registry(session)
    assert DEEPSEEK_REVIEWER not in {item.agent_id for item in candidates}
    assert any(
        agent_id == DEEPSEEK_REVIEWER and "review-only" in reason for agent_id, reason in dropped
    ), dropped


@needs_pg
def test_mode_filter_is_not_a_license_to_skip_the_seat_roles(session):
    """审查身份在 deepseek 池里也只有 reviewer 一个岗：执行/验收候选集合与 Phase 16 相同。"""
    _seed_roles(session)
    manual = ag.pool_from_role_specs(session, mode=REVIEW_MODE_MANUAL)
    deepseek = ag.pool_from_role_specs(session, mode=REVIEW_MODE_DEEPSEEK)
    for role in ("architect", "acceptor"):
        assert {item.agent_id for item in manual if item.can(role)} == {
            item.agent_id for item in deepseek if item.can(role)
        }, role
