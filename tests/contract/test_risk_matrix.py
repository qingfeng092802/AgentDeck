"""T060 / T061 的判据：EN-11 低风险免确认规则表（FR-046 / FR-047 / FR-048｜R-26 ①）。

`contract` 一档，不碰 PG —— 这里验的是**判定**，规则灌进 EN-11 表那一半在
`tests/integration/test_approval_ticket.py`（它要连库）。

为什么这份判据值得单独一个文件：FR-046 / FR-047 是宪法级的"防自判低风险规避审批"，
它最容易出的两种坏法都是**静默**的 ——
① 清单读不出来时当成"没有规则命中"（表现：全都走人工，看起来保守，实际上免确认整条路失灵）；
② 空键被当通配（表现：一条漏写 `match_role_id` 的规则把整个岗位面放开）。
两条都必须红，所以两侧各有一组专门的用例。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from acp.approval.timeouts import KIND_BY_LETTER
from acp.persistence.enums import values_of
from acp.registry import risk_engine as R

REAL_RULES = R.RULES_PATH
REAL_PARAMS_REF = R.PARAMS_REF_PATH


def rules_from(tmp_path: Path, text: str) -> list[R.Rule]:
    path = tmp_path / "risk_matrix.yaml"
    path.write_text(text, encoding="utf-8")
    return R.load_rules(path)


def patched(old: str, new: str, tmp_path: Path) -> list[R.Rule]:
    text = REAL_RULES.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"注入点不唯一（{text.count(old)} 处）：{old!r}"
    return rules_from(tmp_path, text.replace(old, new))


def facts(**over) -> R.RequestFacts:
    base = dict(
        agent_id="workbuddy",
        role_id="architect",
        skill_tags=frozenset({"planning"}),
        fs_mutation=False,
        paid_external_call=False,
        estimated_tokens=1000,
    )
    base.update(over)
    return R.RequestFacts(**base)


# ---------------------------------------------------------------- 名单唯一来源

def test_the_rule_list_has_only_one_source():
    """在册工具名不许出现在判定源码里：抄进代码就有第二份清单，改 yaml 不红。"""
    source = Path(R.__file__).read_text(encoding="utf-8")
    for agent in ("workbuddy", "qoder", "doubao"):
        assert agent not in source, f"risk_engine.py 里出现了 {agent!r} ⇒ 规则被抄进代码"


def test_shipped_rules_are_the_ones_the_doc_points_at():
    """清单存在且**只有那四条在册**（条数写成关系式而不是定值：读文件行数派生）。"""
    text = REAL_RULES.read_text(encoding="utf-8")
    declared = sum(1 for line in text.split("\n") if line.startswith("- match_agent_id:"))
    assert len(R.load_rules()) == declared >= 1


def test_threshold_is_read_from_params_ref_section_two(tmp_path):
    """阈值 5 万来自 §2 那张表：换文本就换读数，读不出就报错，绝不返回默认阈值。"""
    assert R.budget_threshold() == 50_000
    text = REAL_PARAMS_REF.read_text(encoding="utf-8")
    old = "| 预估 token 预算阈值 | **5 万 Tokens** |"
    assert text.count(old) == 1
    variant = tmp_path / "params-ref.md"
    variant.write_text(text.replace(old, "| 预估 token 预算阈值 | **12 万 Tokens** |", 1), encoding="utf-8")
    assert R.budget_threshold(variant) == 120_000
    variant.write_text(text.replace(old, "| 预估 token 预算阈值 | 看情况 |", 1), encoding="utf-8")
    with pytest.raises(R.RiskRulesError, match="读不出数"):
        R.budget_threshold(variant)
    variant.write_text(
        text.replace("## 2. 低风险免确认阈值", "## 2. 改名了", 1), encoding="utf-8"
    )
    with pytest.raises(R.RiskRulesError, match="找不到锚点行"):
        R.budget_threshold(variant)


# ---------------------------------------------------------------- 清单形状：全部拒在内存里

def test_a_null_or_blank_match_key_is_refused_not_read_as_wildcard(tmp_path):
    """三段键写成 null / 空串都不当通配（`data-model.md:162`），载入即拒。"""
    for old, new in (
        ("match_role_id: architect", "match_role_id: null"),
        ("match_role_id: architect", 'match_role_id: ""'),
        ("match_agent_id: workbuddy", "match_agent_id: null"),
    ):
        text = REAL_RULES.read_text(encoding="utf-8").replace(old, new, 1)
        assert new in text
        with pytest.raises(R.RiskRulesError, match="非空字符串"):
            rules_from(tmp_path, text)


def test_capability_tags_must_be_a_non_empty_list(tmp_path):
    for broken in ("match_capability_tags: []", "match_capability_tags: null", "match_capability_tags: [\"\"]"):
        text = REAL_RULES.read_text(encoding="utf-8").replace(
            "match_capability_tags:\n    - planning\n    - architecture_review", broken, 1
        )
        assert broken in text
        with pytest.raises(R.RiskRulesError, match="非空字符串列表"):
            rules_from(tmp_path, text)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("auto_pass_flag", "auto_pass_needs_all_three"),
        ("unknown_key", "表外键"),
        ("dropped_key", "缺键"),
        ("not_a_list", "顶层必须是非空清单"),
        ("duplicate_key", "三段键重复"),
        ("bool_as_string", "必须是布尔"),
    ],
)
def test_bad_shapes_are_refused(tmp_path, mutation, reason):
    text = REAL_RULES.read_text(encoding="utf-8")
    if mutation == "auto_pass_flag":
        text = text.replace("  no_paid_external_call: true\n", "  no_paid_external_call: false\n", 1)
    elif mutation == "unknown_key":
        text = text.replace("  auto_pass: true\n", "  auto_pass: true\n  approver: boss\n", 1)
    elif mutation == "dropped_key":
        text = text.replace("  no_fs_mutation: true\n", "", 1)
    elif mutation == "not_a_list":
        text = "not_a_list: true\n"
    elif mutation == "duplicate_key":
        text = text + text[text.index("- match_agent_id: workbuddy") : text.index("# ── 三条登记在册")]
    elif mutation == "bool_as_string":
        text = text.replace("  no_fs_mutation: true\n", '  no_fs_mutation: "true"\n', 1)
    with pytest.raises(R.RiskRulesError, match=reason):
        rules_from(tmp_path, text)


def test_the_restored_real_list_still_loads(tmp_path):
    """反向对照：把原清单原样写进临时目录再载入 ⇒ 不报错（上面那些红来自坏形状，不来自临时目录）。"""
    rules = rules_from(tmp_path, REAL_RULES.read_text(encoding="utf-8"))
    assert rules == R.load_rules()


# ---------------------------------------------------------------- 命中口径：三段 AND 子集

@pytest.mark.parametrize(
    ("agent", "role", "tags", "matched"),
    [
        ("workbuddy", "architect", frozenset({"planning"}), True),
        ("workbuddy", "architect", frozenset({"architecture_review"}), True),
        # 子集方向只认一个：task ⊆ rule 才命中；task 比 rule 宽就不命中（超集里含未申报的能力）。
        ("workbuddy", "architect", frozenset({"planning", "document_drafting"}), False),
        ("workbuddy", "reviewer", frozenset({"planning"}), False),
        # 命中 ≠ 免确认：doubao 这条命中了，但它自己申报 `auto_pass: false`（下一条用例管这个）。
        ("doubao", "reviewer", frozenset({"summarization"}), True),
        ("qoder", "architect", frozenset({"frontend_coding"}), True),
    ],
)
def test_matching_is_three_keys_anded_with_a_subset_test(agent, role, tags, matched):
    hit = any(rule.matches(agent, role, tags) for rule in R.load_rules())
    assert hit is matched


def test_empty_task_tags_never_match_anything():
    """空 `skill_tags` 的请求侧也不当通配：空集是任何集合的子集，拿它当命中就是给
    "没申报能力的卡"发免确认（FR-046 的方向反了）。"""
    verdict = R.evaluate(facts(skill_tags=frozenset()))
    assert verdict.exempt is False
    assert verdict.reasons == ("task_skill_tags_empty",)


# ---------------------------------------------------------------- 免确认要两侧都对得上

def test_exempt_needs_the_rule_and_the_request_both_on_the_low_risk_side():
    assert R.evaluate(facts()).exempt is True, "正向对照：规则声明与三条事实同时成立才免确认"
    flips = [
        ({"fs_mutation": True}, "rule0_vs_fs_mutation"),
        ({"paid_external_call": True}, "rule0_vs_paid_external_call"),
        ({"estimated_tokens": 50_001}, "over_budget_threshold"),
    ]
    for over, reason in flips:
        verdict = R.evaluate(facts(**over))
        assert verdict.exempt is False, f"{over} 还免确认就是拿一条声明换掉一次确认"
        assert any(reason in item for item in verdict.reasons), verdict.reasons


def test_threshold_is_compared_against_the_doc_value_not_a_constant(tmp_path):
    """阈值来自 §2：把阈值注成 500 之后同一份请求就不再免确认（证明比较用的是那个数）。"""
    assert R.evaluate(facts(estimated_tokens=1000), threshold=500).exempt is False
    assert R.evaluate(facts(estimated_tokens=1000), threshold=1000).exempt is True


def test_non_auto_pass_rules_are_reported_as_matched_but_not_exempt():
    verdict = R.evaluate(facts(agent_id="doubao", role_id="reviewer", skill_tags=frozenset({"summarization"})))
    assert verdict.exempt is False
    assert verdict.reasons == ("rule2_declares_no_auto_pass",), verdict.reasons


# ---------------------------------------------------------------- 作用域与覆盖入口

@pytest.mark.parametrize("kind", sorted(values_of("approval_kind")))
def test_exemption_only_lifts_kind_A(kind):
    """FR-048：免确认只免除卡点 A；审查 / 验收 / 超预算 / 危险操作一律照走。"""
    verdict = R.evaluate(facts())
    assert verdict.exempt is True
    assert R.exemption_applies(kind, verdict) is (kind == KIND_BY_LETTER["A"])


def test_a_non_exempt_verdict_lifts_nothing():
    verdict = R.evaluate(facts(fs_mutation=True))
    for kind in sorted(values_of("approval_kind")):
        assert R.exemption_applies(kind, verdict) is False


def test_evaluate_has_no_override_entry():
    """FR-047 的形状判据：判定只有"事实 + 规则 + 阈值"三种输入，没有任何一个能设结果的参数。"""
    params = inspect.signature(R.evaluate).parameters
    assert list(params) == ["facts", "rules", "threshold"], list(params)
    assert list(inspect.signature(R.exemption_applies).parameters) == ["kind", "verdict"]
    for name in ("exempt", "auto_pass", "override", "force"):
        assert not any(name in parameter for parameter in params), name
