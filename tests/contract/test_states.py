"""T026：15 态状态机的落库表达（FR-019 / FR-040｜R-02）。

判据形态照 `data-model.md §2` 的两条**关系式**，不写"15"这个常数：
① 枚举行条目数 = 自述态数 = Python 枚举成员数；② 迁移图与规则区里的大写态名 MUST 都在枚举内
（差集为空）。两条都用**双向**差集，单向写会漏掉"文档删了一个态而代码留着"。

每条判据都配了**开火对照**（把文档改坏一份，看差集是否真的非空）：不配对照的用例
在解析函数整体失灵时会一起变绿，那正是 §2 记过的"判据自破"。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from acp.persistence.enums import domains, enum_type_name, pg_enum, values_of
from acp.persistence.states import (
    DIAGRAM_ANCHOR,
    NON_STATE_TOKENS,
    TaskCardState,
    data_model_default,
    state_count_claimed,
    states_declared_in_data_model,
    tokens_in_transition_region,
)
from acp.protocol.refs import (
    AgentForm,
    ArtifactFormat,
    BillingBasis,
    CostBand,
    SelfReportedStatus,
    UsageKind,
)

DOC = data_model_default()


def fake_doc(states: tuple[str, ...], *, extra_region_tokens: str = "", prose: str = "") -> str:
    """构造一份最小可用的 data-model §2，用于开火对照（不碰真文档）。"""
    return (
        "## 2. 任务卡状态机\n\n"
        f"本表 **{len(states)} 态**。\n"
        f"**状态全集（唯一权威枚举，{len(states)} 个）**：{', '.join(states)}\n\n"
        f"说明文字里出现 {prose} 这类技术名词，MUST NOT 被当成态名。\n\n"
        f"{DIAGRAM_ANCHOR}\n\n"
        "```text\nDRAFT ──拆单──▶ PENDING_APPROVAL\n```\n\n"
        "迁移合法性规则（入库断言）：\n"
        "- 进入 REVIEWING 前，三岗互斥断言 MUST 已通过；结论取 APPROVE。\n"
        f"{extra_region_tokens}\n"
        "## 3. 关系与基数\n"
    )


def test_python_enum_and_doc_enumeration_have_empty_difference():
    declared = states_declared_in_data_model(DOC)
    implemented = {state.value for state in TaskCardState}
    assert implemented - declared == set()
    assert declared - implemented == set()


def test_enum_size_is_the_documents_own_claimed_size():
    """判据①：自述态数 = 枚举行条目数 = Python 成员数（三者任一处改了都会红）。"""
    claimed = state_count_claimed(DOC)
    declared = states_declared_in_data_model(DOC)
    assert claimed == len(declared) == len(TaskCardState)
    assert claimed > 0  # 解析失败会得到 0，与"文档确实写了 0"不可分 ⇒ 显式钉住非空


def test_transition_region_tokens_are_all_declared_states():
    """判据②：图与规则区的大写词，减去枚举与两项排除 ⇒ 空集。"""
    declared = states_declared_in_data_model(DOC)
    unknown = tokens_in_transition_region(DOC) - NON_STATE_TOKENS - declared
    assert unknown == set()


def test_renamed_state_in_doc_fires_the_gate(tmp_path: Path):
    """开火对照：文档改一个态名而代码没跟 ⇒ 差集非空（不是"两边都是 0"的假绿）。"""
    renamed = tuple(
        "HANDED_OVER" if value == "HANDED_OFF" else value
        for value in (state.value for state in TaskCardState)
    )
    broken = tmp_path / "data-model.md"
    broken.write_text(fake_doc(renamed), encoding="utf-8")
    declared = states_declared_in_data_model(broken)
    implemented = {state.value for state in TaskCardState}
    assert declared - implemented == {"HANDED_OVER"}
    assert implemented - declared == {"HANDED_OFF"}


def test_undeclared_token_in_diagram_fires_the_gate(tmp_path: Path):
    broken = tmp_path / "data-model.md"
    broken.write_text(
        fake_doc(tuple(state.value for state in TaskCardState), extra_region_tokens="QUEUED"),
        encoding="utf-8",
    )
    declared = states_declared_in_data_model(broken)
    assert tokens_in_transition_region(broken) - NON_STATE_TOKENS - declared == {"QUEUED"}


def test_region_scan_is_anchored_on_the_whole_comment_line(tmp_path: Path):
    """N-4 回归：区间 MUST 从**整行**锚点起扫。

    锚点名在说明文字里也出现过（真文档就是这样），只匹配名字会让区间从说明行就开始，
    把说明里的技术名词一并扫进差集 —— 那是 §2 记过的假阴。
    """
    doc = tmp_path / "data-model.md"
    text = fake_doc(
        tuple(state.value for state in TaskCardState),
        prose="ARCHITECTURE 与 state-diagram-start 锚点名",
    )
    doc.write_text(text, encoding="utf-8")
    tokens = tokens_in_transition_region(doc)
    assert "ARCHITECTURE" not in tokens
    assert "DRAFT" in tokens  # 区间确实开火了，不是整段没扫


def test_pg_enum_type_carries_exactly_the_state_machine():
    enum_type = pg_enum("task_card_state")
    assert set(enum_type.enums) == {state.value for state in TaskCardState}
    assert enum_type.name == enum_type_name("task_card_state") == "task_card_state_type"


def test_review_verdict_domain_is_exactly_the_three_value_contract():
    """Phase 17 P1：EN-08 的结论域 = `APPROVE / CHANGES_REQUESTED / NEED_HUMAN`。

    三值不是"把不确定塞进通过/打回"：`NEED_HUMAN` 是**人工升级**的载体，由
    `review.check_findings()` 在任一条 `met=null` 时推出，MUST NOT 由模型自述。
    """
    assert values_of("review_verdict") == ("APPROVE", "CHANGES_REQUESTED", "NEED_HUMAN")


def test_approval_kind_domain_is_exactly_the_five_kinds():
    """Phase 17 P1：第五类卡点 `E_REVIEW_UNCERTAIN` 与 A–D 共用同一张 EN-09 表。"""
    assert values_of("approval_kind") == (
        "A_SPLIT",
        "B_ACCEPTANCE",
        "C_BUDGET",
        "D_DANGER",
        "E_REVIEW_UNCERTAIN",
    )


def test_the_two_changed_pg_enums_carry_the_live_domain_in_order():
    """PG 枚举对象的标签集与顺序 = 域元组；迁移 `0008` 的 ADD VALUE 顺序必须与它一致。"""
    assert tuple(pg_enum("review_verdict").enums) == values_of("review_verdict")
    assert tuple(pg_enum("approval_kind").enums) == values_of("approval_kind")


def test_pg_enum_object_is_cached_per_domain():
    """同域复用同一个 `Enum` 对象：`cost_band` 用在 EN-03 与 EN-05 两张表。

    每次新建对象的后果是 `CREATE TYPE` 下发两次 ⇒ `type already exists`，
    这条是本轮真跑迁移时撞出来的，所以钉成用例而不只写在注释里。
    """
    assert pg_enum("cost_band") is pg_enum("cost_band")
    assert values_of("cost_band") == values_of("cost_band")


@pytest.mark.parametrize(
    ("domain", "literal"),
    [
        ("artifact_format", ArtifactFormat),
        ("agent_form", AgentForm),
        ("cost_band", CostBand),
        ("usage_kind", UsageKind),
        ("billing_basis", BillingBasis),
        ("self_reported_status", SelfReportedStatus),
    ],
)
def test_derived_domains_are_read_from_protocol_literals(domain, literal):
    """`data-model.md §5` T-1：PG 枚举 MUST 引用 `refs.py` 的 `Literal`，不再各写一份字面量。

    断言的是**值集合相等**：在这里抄一份字面量而漏抄一个值，本用例红；
    只改 `Literal` 不改这里，也红（同一来源的两端被钉住）。
    """
    assert values_of(domain) == tuple(get_args(literal))


def test_doc_only_domains_are_the_documented_set():
    """协议派生 6 域之外的域只能来自 data-model，多一个域或少一个域都要回到那张表改。"""
    derived = {
        "artifact_format",
        "agent_form",
        "cost_band",
        "usage_kind",
        "billing_basis",
        "self_reported_status",
    }
    assert set(domains()) - derived == {
        "task_card_state",
        "feature_request_status",
        "validated_status",
        "review_verdict",
        "approval_kind",
        "approval_state",
        "resolution_channel",
        "resolution_verdict",
        "snapshot_phase",
        "event_log_kind",
        "autonomy_mode",
    }


def test_t6_closed_duplicated_is_out_of_the_domain():
    """owner 2026-10-12 裁决 4：**删掉 `DUPLICATED`** ⇒ T-6 闭合。

    理由是"后到结论"已由 `resolution_event.applied = false` 表达，同一个事实不留第二个载体。
    本用例断言减法之后的形态：域里没有 DUPLICATED，且文档 §5 的 T-6 已划掉——
    两条一起看才说明删掉的是"态"而不是"表达能力"。
    """
    assert "DUPLICATED" not in values_of("approval_state")
    assert set(values_of("approval_state")) == {
        "PENDING",
        "APPROVED",
        "REJECTED",
        "NEED_INFO",
        "TIMED_OUT_CONVERGED",
    }
    doc = DOC.read_text(encoding="utf-8")
    assert "**T-6**（P2：C16）" not in doc  # §5 里已划掉，不留第二份待裁清单
    assert "applied" in doc  # 替代表达式仍在文档里


def test_ruling_4_location_is_en_09_not_the_state_machine():
    """裁决 4 原文写"data-model.md §2 的 15 态列表去掉 DUPLICATED"——**位置不对**，按事实执行并留证。

    `DUPLICATED` 从来不在 §2 的状态全集里，它在 **EN-09 `approval_ticket.state`**
    （`data-model.md:126`）。15 态列表一个字节都不该动——动了就破 §2 判据①
    （自述态数 = 枚举行条目数 = Python 成员数）。这条断言就是那条更正的证据，不是走流程。
    """
    declared = states_declared_in_data_model(DOC)
    assert "DUPLICATED" not in declared
    assert len(declared) == state_count_claimed(DOC) == len(TaskCardState)


def test_terminal_states_are_a_subset_of_the_enum():
    from acp.persistence.states import TERMINAL_STATES

    assert TERMINAL_STATES <= {state for state in TaskCardState}
    assert {state.value for state in TERMINAL_STATES} <= states_declared_in_data_model(DOC)
