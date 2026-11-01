"""T013：三类跨 Agent 消息的正反例（FR-009 / R-02，`tests/contract/` ⇒ fast 档）。

分档由目录决定（`tests/conftest.py`），本文件不需要装饰器。

一条口径先说清楚，免得把这里的绿读成"扩展位已验"：`dev_model` 不参与判定，本文件只验到
**形状**（它是 `Task` 里唯一可改而不影响其余字段的位），真正验"选人与把关结果不变"的是
`tests/guards/test_select_worker.py`（quickstart §3 表内那条，Phase 5 落地）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

import acp.protocol as protocol
from acp.protocol import (
    ArtifactFloor,
    ArtifactRef,
    CapabilityManifest,
    Handle,
    Task,
    TaskResult,
    UsageNotice,
)
from acp.protocol.refs import (
    AgentForm,
    ArtifactFormat,
    BillingBasis,
    CostBand,
    SelfReportedStatus,
    UsageKind,
)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def task_payload(**override):
    payload = {
        "task_id": "T01",
        "title": "落地静态首页",
        "difficulty": 2,
        "skill_tags": ["html", "css"],
        "depends_on": [],
        "project_key": "demo",
        "assignee": {"agent_id": "qoder", "role_id": "frontend"},
        "objective": "产出 index.html",
        "inputs": ["AI项目台/demo/tasks/T01.md"],
        "expected_output": "index.html",
        "artifact_floor": {"min_bytes": 200, "min_lines": 8, "format": "html"},
        "acceptance_criteria": ["含 h1 标题", "首屏无外链脚本"],
        "token_budget": Decimal("1000"),
        "deadline_minutes": 30,
        "constraints": ["不引第三方框架"],
        "dev_model": None,
    }
    payload.update(override)
    return payload


def result_payload(**override):
    payload = {
        "task_id": "T01",
        "self_reported_status": "success",
        "artifacts": [
            {"rel_path": "index.html", "bytes": 512, "lines": 18, "format": "html"}
        ],
        "log_excerpt": "wrote index.html",
        "self_assessment": "两条标准均满足",
        "issues": [],
        "usage": [
            {
                "usage_kind": "CALL",
                "quantity": Decimal("1"),
                "unit": "call",
                "billing_basis": "SEAT_CREDIT",
                "occurred_at": NOW,
            }
        ],
    }
    payload.update(override)
    return payload


def manifest_payload(**override):
    payload = {
        "agent_id": "qoder",
        "form": "IN_PLATFORM",
        "capabilities": ["html", "css", "md"],
        "tier": 3,
        "cost_band": "MID",
        "max_parallel": 1,
        "role_mounts": ["frontend", "reviewer"],
    }
    payload.update(override)
    return payload


def required_names(model):
    return {name for name, field in model.model_fields.items() if field.is_required()}


# --- 正对照：基准载荷必须建得出来（缺了它，下面每一条"拒绝"都可能是恒真） -------------


def test_three_messages_roundtrip():
    for model, payload in (
        (Task, task_payload()),
        (TaskResult, result_payload()),
        (CapabilityManifest, manifest_payload()),
    ):
        built = model(**payload)
        assert model.model_validate(built.model_dump()) == built


def test_artifact_floor_and_refs_share_format_domain():
    floor = ArtifactFloor(min_bytes=1, min_lines=1, format="md")
    ref = ArtifactRef(rel_path="a.md", bytes=1, lines=1, format="md")
    assert floor.format == ref.format


# --- 必填项：缺任一项即拒，且"必填集合"本身被钉住（防止日后有人顺手加默认值） ---------

TASK_REQUIRED = [
    "task_id",
    "title",
    "difficulty",
    "skill_tags",
    "depends_on",
    "project_key",
    "assignee",
    "objective",
    "inputs",
    "expected_output",
    "artifact_floor",
    "acceptance_criteria",
    "token_budget",
]


@pytest.mark.parametrize("field", TASK_REQUIRED)
def test_task_missing_required_rejected(field):
    payload = task_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        Task(**payload)


def test_task_required_set_matches_declaration():
    assert required_names(Task) == set(TASK_REQUIRED)
    # 扩展位与可选项必须留在"非必填"里，否则 dev_model 会变成一个下发即失败的字段。
    assert {"deadline_minutes", "constraints", "dev_model"} <= set(Task.model_fields) - set(
        TASK_REQUIRED
    )


@pytest.mark.parametrize(
    "field", ["agent_id", "form", "capabilities", "tier", "cost_band", "max_parallel", "role_mounts"]
)
def test_manifest_missing_any_of_seven_rejected(field):
    """FR-011 的四项加 `form` / `role_mounts`：缺任一项 ⇒ 注册失败（契约 §3）。"""
    payload = manifest_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        CapabilityManifest(**payload)


def test_manifest_required_set_matches_declaration():
    assert required_names(CapabilityManifest) == {
        "agent_id",
        "form",
        "capabilities",
        "tier",
        "cost_band",
        "max_parallel",
        "role_mounts",
    }


def test_task_result_required_set_matches_declaration():
    assert required_names(TaskResult) == {
        "task_id",
        "self_reported_status",
        "log_excerpt",
        "self_assessment",
    }


# --- 有效性下限（FR-017）：三项各给一个"越界值"与一个"缺失" --------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"min_bytes": 0},
        {"min_bytes": -1},
        {"min_lines": 0},
        {"min_lines": -1},
        {"format": "txt"},
        {"format": ""},
        {"min_lines": None},
        {"min_bytes": None},
        {"format": None},
    ],
)
def test_artifact_floor_rejects_missing_and_nonpositive(patch):
    floor = task_payload()["artifact_floor"]
    for key, value in patch.items():
        if value is None:
            floor.pop(key, None)
        else:
            floor[key] = value
    with pytest.raises(ValidationError):
        Task(**task_payload(artifact_floor=floor))


def test_skill_tags_must_be_nonempty():
    """`plan.md` 设计项 8：空 `skill_tags` 让 `skill_fit` 除零，MUST 在构造期拦下。

    只验"非空集合"这一条：`[""]`（含空串标签）在本模型里合法——它不会让 skill_fit 除零，
    只会永远匹配不上，属 Phase 5 的 `skill_fit` 取值域问题，文档未定，故此处不判。
    """
    with pytest.raises(ValidationError):
        Task(**task_payload(skill_tags=[]))


def test_acceptance_criteria_must_be_nonempty():
    """FR-029：审查逐条比对的唯一标准来源。空标准 ⇒ 审查无从比对 ⇒ 判据恒真。"""
    with pytest.raises(ValidationError):
        Task(**task_payload(acceptance_criteria=[]))


@pytest.mark.parametrize("budget", [Decimal("-1"), Decimal("0"), Decimal("0.00"), "abc"])
def test_token_budget_rejected_when_not_positive(budget):
    """`token_budget` MUST > 0 ⇒ 0 与负数都在构造期拒（owner 2026-10-12 裁决 3，两轮同号）。

    "无预算"现在有了表达位：**显式 null**（同一条裁决的后半句："nullable 是 SQL 的原生语义，
    不另造标志字段"）。MUST NOT 用 0 冒充它——0 在本模型里是非法值，拿它当"无上限"
    会让 `cost/budget.py` 的阈值比较永远超限。
    """
    with pytest.raises(ValidationError):
        Task(**task_payload(token_budget=budget))


def test_token_budget_null_means_no_budget_and_is_accepted():
    """裁决 3 的表达位：键**必须在**（缺键仍非法，见 `test_token_budget_is_required`），值可为 null。"""
    assert Task(**task_payload(token_budget=None)).token_budget is None


def test_token_budget_is_required():
    payload = task_payload()
    del payload["token_budget"]
    with pytest.raises(ValidationError):
        Task(**payload)


@pytest.mark.parametrize("budget", [Decimal("0.01"), Decimal("1"), Decimal("1000")])
def test_token_budget_accepts_positive_values(budget):
    assert Task(**task_payload(token_budget=budget)).token_budget == budget


# --- 无私有旁路（FR-009 / D-7）：多余字段在三个结构上一律拒 --------------------------


@pytest.mark.parametrize(
    "model, factory",
    [
        (Task, task_payload),
        (TaskResult, result_payload),
        (CapabilityManifest, manifest_payload),
    ],
)
def test_extra_field_rejected(model, factory):
    with pytest.raises(ValidationError):
        model(**factory(vendor_native_id="x"))


def test_task_result_cannot_self_declare_validity():
    """FR-019 / 契约 §2.2：最终态由 `ValidateArtifact` 覆写，适配器不得自带 `validated_status`。"""
    with pytest.raises(ValidationError):
        TaskResult(**result_payload(validated_status="valid"))


# --- 空产物必须构造得出来：契约 §5 的 "success + 空产物" 用例靠它才走得到门禁 ---------


def test_task_result_empty_artifacts_is_constructible():
    result = TaskResult(**result_payload(artifacts=[]))
    assert result.artifacts == []
    assert result.self_reported_status == "success"


def test_task_result_empty_usage_is_constructible():
    """记账尚未接线时（T114 在 Phase 10）用量可为空，但不得因此让回收整体失败。"""
    assert TaskResult(**result_payload(usage=[])).usage == []


# --- 形态域（G1）：本期只接三类，CLI / GUI 在构造期即拒 ------------------------------


@pytest.mark.parametrize("form", ["CLI", "GUI", "cli", "IN_PLATFORM ", ""])
def test_manifest_rejects_unsupported_forms(form):
    with pytest.raises(ValidationError):
        CapabilityManifest(**manifest_payload(form=form))


@pytest.mark.parametrize("band", ["CHEAP", "MID", "EXPENSIVE"])
def test_manifest_accepts_three_cost_bands(band):
    assert CapabilityManifest(**manifest_payload(cost_band=band)).cost_band == band


@pytest.mark.parametrize("band", ["CHEAPEST", "cheap", ""])
def test_manifest_rejects_unknown_cost_band(band):
    with pytest.raises(ValidationError):
        CapabilityManifest(**manifest_payload(cost_band=band))


@pytest.mark.parametrize("parallel", [0, -1])
def test_max_parallel_must_be_positive(parallel):
    """EN-03：`current_load >= max_parallel` ⇒ 不可选；申报 0 或负数是一个永远选不中的单元，
    属注册期就该拒的无效申报，而不是运行时的合法状态。"""
    with pytest.raises(ValidationError):
        CapabilityManifest(**manifest_payload(max_parallel=parallel))


# --- 取值域与文档口径对齐：`Literal` 的域就是 data-model / 契约里那几行 -------------


@pytest.mark.parametrize(
    "alias, expected",
    [
        # EN-02 / EN-06 / T-1。`js` 是 owner 2026-10-29 的裁决（第十一轮）：静态站的样式/脚本/页面
        # 是三件平级的东西，少了脚本那件，"加个购物车"这类需求无处安放 —— 2026-10-28 的 T02
        # 正是交了 `cart.js` 被判 `unsupported_format` 而跑砸的。
        (ArtifactFormat, {"html", "css", "md", "json", "py", "js"}),
        (AgentForm, {"IN_PLATFORM", "SDK", "API"}),  # EN-03 / G1
        (CostBand, {"CHEAP", "MID", "EXPENSIVE"}),  # EN-03 / FR-062
        (SelfReportedStatus, {"success", "failed"}),  # EN-06
        (UsageKind, {"INPUT", "OUTPUT", "CALL", "SEAT_DAY"}),  # EN-13
        (BillingBasis, {"TOKEN", "SEAT_CREDIT", "BYOK"}),  # EN-13 / FR-061
    ],
)
def test_literal_domains_match_documented_sets(alias, expected):
    assert set(get_args(alias)) == expected


# --- 扩展位形状守卫（T009）+ 记账入口形状守卫（EN-13 / FR-061）-----------------------


@pytest.mark.parametrize("dev_model", [None, "some-dev-model-x", "另一个开发期标记"])
def test_dev_model_is_the_only_difference(dev_model):
    """除 `dev_model` 外，其余字段与 `dev_model=None` 的载荷完全一致 ⇒ 它没有连带影响任何判定输入。

    真判据（选人与把关结果不变）在 `tests/guards/test_select_worker.py`，Phase 5。
    """
    base = Task(**task_payload()).model_dump(exclude={"dev_model"})
    changed = Task(**task_payload(dev_model=dev_model)).model_dump(exclude={"dev_model"})
    assert changed == base
    assert Task(**task_payload(dev_model=dev_model)).dev_model == dev_model


def test_usage_notice_carries_no_amount():
    """金额由 ledger 依 `params-ref §6` 换算，工具不得自报（FR-061 口径分列的前提）。"""
    forbidden = {"cost_amount", "currency", "price", "usd"}
    assert not forbidden & set(UsageNotice.model_fields)


def test_usage_notice_covers_en13_input_columns():
    assert {
        "usage_kind",
        "quantity",
        "unit",
        "billing_basis",
        "model_ref",
        "occurred_at",
    } == set(UsageNotice.model_fields)


# --- `Handle` 与瞬态层划界（契约 §6 的 N-11）----------------------------------------


def test_handle_has_no_transient_slot_field():
    assert required_names(Handle) == {"task_id", "agent_id", "native_ref", "submitted_at"}
    assert not [name for name in Handle.model_fields if "slot" in name.lower()]


def test_handle_roundtrip():
    handle = Handle(
        task_id="T01", agent_id="qoder", native_ref="cli-2578-session-1", submitted_at=NOW
    )
    assert Handle.model_validate(handle.model_dump()) == handle


# --- 导出面：协议层不外泄 transport / 厂商类型（宪法 II 的结构保证）------------------


def test_protocol_export_surface_is_exactly_the_contract_shapes():
    assert set(protocol.__all__) == {
        "AgentForm",
        "AgentRoleRef",
        "ArtifactFloor",
        "ArtifactFormat",
        "ArtifactRef",
        "BillingBasis",
        "CapabilityManifest",
        "CostBand",
        "Handle",
        "SelfReportedStatus",
        "Task",
        "TaskResult",
        "UsageKind",
        "UsageNotice",
    }
