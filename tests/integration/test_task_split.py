"""T032 的判据：拆单产出的清单形状（FR-004 四项必填 + EN-02 的拆单阶段字段）。

**本文件不碰 PG**，但它放在 `tests/integration/` —— 理由是按 `plan.md` 的 Source Code 分档，
"图"相关的用例归 integration 档（`guards` = 宪法判据、`contract` = 协议 fixture 双向测试，
两个名字都比"编排节点"更不合适）。放在 integration 档的副作用是它不会进 `-m fast` 的秒级先跑集，
这条如实写在这里而不是假装没发生。

拆单器同样是**注入的桩**（真实现体 = Phase 3 适配器，与 T029 / T030 同一条处置，
owner 2026-10-12 裁决 2 已确认那个形态）。本文件验的是形状与拒绝路径，不是拆得好不好。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acp.orchestrate.nodes.plan import CompleterOutcome, build_summary_digest
from acp.orchestrate.nodes.split import (
    CardDraft,
    SplitNotConfigured,
    SplitViolation,
    split_cards,
    validate_draft,
    validate_split,
)
from acp.protocol import ArtifactFloor

DIGEST = build_summary_digest(CompleterOutcome(("配色按简洁商务取",), ("联系方式用邮箱还是表单？",)))
FLOOR = {"min_bytes": 200, "min_lines": 10, "format": "html"}


def draft(task_id="T01", **overrides) -> CardDraft:
    fields = dict(
        task_id=task_id,
        title="首页骨架",
        difficulty=2,
        skill_tags=["html", "css"],
        depends_on=[],
        artifact_floor=ArtifactFloor(**FLOOR),
        acceptance_criteria=["首屏有公司名"],
    )
    fields.update(overrides)
    return CardDraft(**fields)


class StubSplitter:
    def __init__(self, drafts):
        self.drafts = drafts
        self.seen: list = []

    def split(self, summary_digest):
        self.seen.append(dict(summary_digest))
        return self.drafts


def test_clean_split_returns_the_drafts_in_order():
    drafts = [draft(), draft("T02", depends_on=["T01"])]
    out = split_cards(DIGEST, splitter=StubSplitter(drafts))
    assert [c.task_id for c in out] == ["T01", "T02"]


def test_splitter_receives_the_validated_digest():
    """拆单的输入面就是 `plan` 节点那张摘要 —— 不重算、不补全，也不许绕过摘要判据。"""
    stub = StubSplitter([draft()])
    split_cards(DIGEST, splitter=stub)
    assert stub.seen == [DIGEST]


def test_digest_must_still_pass_the_plan_gate():
    """摘要缺 `自行补全项` ⇒ 拆单不开火：FR-002 的判据不会因为下游是 Split 就放宽。"""
    with pytest.raises(ValueError):
        split_cards({}, splitter=StubSplitter([draft()]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"title": "   "},
        {"skill_tags": []},
        {"skill_tags": ["  "]},
        {"skill_tags": ["html", "html"]},
        {"acceptance_criteria": []},
        {"depends_on": ["T01", "T01"]},
        {"depends_on": ["   "]},
    ],
    ids=[
        "title-empty",
        "title-blank",
        "skill-tags-empty",
        "skill-tags-blank",
        "skill-tags-duplicated",
        "criteria-empty",
        "depends-duplicated",
        "depends-blank",
    ],
)
def test_bad_card_shape_is_rejected(overrides):
    """FR-004 的四项必填在这里第一次变成"给不出就拒"，而不是"缺了等下游崩"。"""
    with pytest.raises((SplitViolation, ValidationError)):
        validate_draft(draft(**overrides))


def test_non_string_tag_is_rejected_by_the_validator():
    """`skill_tags: [1]` 会被 pydantic 转成 "1"（宽松转换），所以这条由 `_items` 的类型检查兜。"""
    d = draft()
    object.__setattr__(d, "skill_tags", ["html", 1])  # 绕开构造，直接造出运行时畸形
    with pytest.raises(SplitViolation):
        validate_draft(d)


def test_skill_tags_must_be_non_empty_because_skill_fit_divides_by_it():
    """`plan.md` 设计项 8：`skill_fit = |tags ∩ capabilities| / |tags|` ⇒ 空集合是除零。

    这条单独钉一次，是因为它是"必填"之外更强的理由：空 tags 不只是不完整，而是会让选人崩。
    """
    with pytest.raises(SplitViolation):
        validate_draft(draft(skill_tags=[]))


@pytest.mark.parametrize(
    "task_id",
    ["t01", "T1", "TASK-01", "", "T01 "],
    ids=["lowercase", "one-digit", "other-form", "empty", "trailing-space"],
)
def test_card_id_must_match_the_documented_shape(task_id):
    """EN-02 说明列"形如 T01"。放宽到任意串会让 `depends_on` 的引用比对失去意义。"""
    with pytest.raises(SplitViolation):
        validate_split([draft(task_id)])


def test_duplicate_card_ids_in_one_split_are_rejected():
    with pytest.raises(SplitViolation):
        validate_split([draft("T01"), draft("T01", title="另一张")])


def test_empty_task_list_is_not_a_split():
    with pytest.raises(SplitViolation):
        validate_split([])


def test_no_default_splitter_is_offered():
    """缺拆单器 ⇒ 报错，而不是回落到一份固定清单（那会把 FR-004 冒充成已实现）。"""
    with pytest.raises(SplitNotConfigured):
        split_cards(DIGEST, splitter=None)


def test_draft_carries_the_two_fields_en02_needs_before_dispatch():
    """正面对照：`artifact_floor` 与 `acceptance_criteria` 必须在拆单阶段就有。

    理由不是"字段好看"：EN-02 把这两列建成 NOT NULL，而 FR-017 的下限与 FR-029 的逐条比对
    都要求"由任务卡声明"。Story 1 AC6 反对的正是"等交付前才临时想验收标准"。
    """
    card = validate_split([draft()])[0]
    assert card.artifact_floor.min_bytes == 200
    assert card.acceptance_criteria == ["首屏有公司名"]


def test_draft_forbids_extra_fields():
    """`StrictModel` 的 `extra="forbid"` 对草稿同样成立：适配器/总管不许夹带私有字段。"""
    with pytest.raises(ValidationError):
        draft(vendor_task_handle="native-123")


def test_difficulty_has_no_invented_bounds():
    """文档与库里都没给 `difficulty` 上界下界 ⇒ 本节点只查类型，不查区间。

    这条是**负面对照**：写成 `difficulty=0` 或 `99` 都应当通过本节点。
    如果哪天要加区间，那属改 EN-02 与 `contracts/`，不是在这里偷偷定。
    """
    assert validate_draft(draft(difficulty=0)).difficulty == 0
    assert validate_draft(draft(difficulty=99)).difficulty == 99
    with pytest.raises(ValidationError):
        draft(difficulty="hard")
