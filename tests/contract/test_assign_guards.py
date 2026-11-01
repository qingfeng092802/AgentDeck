"""T062 的判据：把关矩阵的闭合性（FR-034 / FR-031｜R-09）。

`contract` 一档、不碰 PG —— 这里问的是"**存不存在**一个合法三元组"，是一个纯可行性判定，
与 Phase 5 的"哪个候选更好"（排序）和 Phase 7 的"这份指派合不合法"（断言）是三件不同的事，
各归各的载体。分不开的话就会出现"排序函数顺手把互斥放宽了"这种坏法。

两条最要紧的反向对照（缺了就等于判据恒红）：
- 每条"不可闭合"的负例后面都跟着一条"只补一个候选就变可闭合"的对照 ⇒ 红的原因是**形状**，
  不是因为我的夹具本来就没打算让它通过。
- 自审禁令那条同时查两侧：审查/验收岗排除产出方，执行岗**不**排除 ——
  只测前一半会把"把产出方从所有岗里踢掉"读成绿，而那等于没人能改自己的产物。
"""

from __future__ import annotations

import pytest

from acp.orchestrate.nodes import assign_guards as ag

THREE = [
    ag.Candidate("workbuddy", frozenset({"executor", "reviewer"})),
    ag.Candidate("qoder", frozenset({"executor", "acceptor"})),
    ag.Candidate("doubao", frozenset({"reviewer", "acceptor"})),
]


def test_a_legal_triple_exists_and_the_pick_is_deterministic():
    picked = ag.assert_closable([ag.CardNeed("T01")], THREE)
    assert set(picked) == {"T01"}
    triple = picked["T01"]
    assert len(set(triple)) == 3, f"三岗必须两两不同（原则 III），读到 {triple}"
    # 同一批输入连跑两次给出同一个三元组：`_eligible` 里排过序，所以"能不能闭合"不掺顺序噪声。
    assert ag.assert_closable([ag.CardNeed("T01")], list(reversed(THREE))) == picked


def test_producer_is_banned_from_review_and_accept_only():
    """FR-034：产出方不得自审。禁令管审查 / 验收两岗，执行岗照旧可以是它。"""
    result = ag.closability(ag.CardNeed("T01", produced_by="workbuddy"), THREE)
    assert result.closable, result.reasons
    for executor, reviewer, acceptor in result.assignments:
        assert reviewer != "workbuddy" and acceptor != "workbuddy"
    assert any(executor == "workbuddy" for executor, _r, _a in result.assignments), (
        "执行岗也被自审禁令踢掉 ⇒ 那不是防自审，那是没人能改这份产物"
    )


def test_two_agents_can_not_fill_three_seats():
    """候选池不足三支 ⇒ 直接不可闭合，理由要点名"凑不满三个岗"而不是"没有候选"。"""
    result = ag.closability(ag.CardNeed("T01"), THREE[:2])
    assert result.closable is False
    assert any("凑不满" in reason for reason in result.reasons), result.reasons


def test_a_role_with_no_candidate_is_named_in_the_reason():
    pool = [
        ag.Candidate("a0", frozenset({"executor"})),
        ag.Candidate("a1", frozenset({"executor"})),
        ag.Candidate("a2", frozenset({"executor"})),
    ]
    result = ag.closability(ag.CardNeed("T01"), pool)
    assert result.closable is False
    assert {"reviewer", "acceptor"} <= {reason.split(" 岗")[0] for reason in result.reasons}, result.reasons


def test_same_agent_under_two_roles_is_not_two_seats():
    """三支工具、三岗都有候选，但审查与验收只有同一支能干 ⇒ 凑不出两两不同的三元组。

    这条负例钉的是 FR-031 的判定单位 = `agent_id`（不含 `role_id`）：
    写成"`len(set)==3` 就放行"而没测这一支，就会漏掉"同一支工具换两个岗"这种假闭合。
    """
    pool = [
        ag.Candidate("a0", frozenset({"executor", "reviewer", "acceptor"})),
        ag.Candidate("a1", frozenset({"executor"})),
        ag.Candidate("a2", frozenset({"executor"})),
    ]
    result = ag.closability(ag.CardNeed("T01"), pool)
    assert result.closable is False
    assert any("同工具换 role" in reason for reason in result.reasons), result.reasons
    # 反向对照：只把验收岗多开一支 ⇒ 立刻可闭合。上一条的红来自形状，不来自恒红。
    widened = pool + [ag.Candidate("a3", frozenset({"acceptor"}))]
    assert ag.closability(ag.CardNeed("T01"), widened).closable


def test_whole_batch_is_refused_when_any_card_cannot_close():
    """任一卡填不满 ⇒ 整批抛错，一张都不指（防"半指派"状态里的三岗互斥看不见）。"""
    with pytest.raises(ag.GuardNotClosable) as caught:
        ag.assert_closable([ag.CardNeed("T01"), ag.CardNeed("T02", produced_by="doubao")], THREE)
    assert caught.value.code == "guard_not_closable"
    assert any(reason.startswith("T02:") for reason in caught.value.reasons), caught.value.reasons


def test_the_self_review_ban_can_be_the_thing_that_breaks_closability():
    """审查与验收只剩同一支工具，而它正是产出方 ⇒ 禁令之后为空，不可闭合。"""
    pool = [
        ag.Candidate("a0", frozenset({"executor", "reviewer", "acceptor"})),
        ag.Candidate("a1", frozenset({"executor"})),
        ag.Candidate("a2", frozenset({"executor"})),
    ]
    result = ag.closability(ag.CardNeed("T01", produced_by="a0"), pool)
    assert result.closable is False


def test_the_node_is_deliberately_not_wired_into_the_graph():
    """本模块不导出节点函数：接线归 Phase 5 / 7（模块尾那段说明的唯一机器可读形式）。

    没有这条判据，那段说明就只能等人读到；有了它，"顺手接进图"之前必须先处理这里与 `graph.py`
    的节点集合判据 —— 两处红比一句注释可靠。
    """
    assert not hasattr(ag, "assign_guards"), "AssignGuards 节点仍不该存在（T073 / T097 才接）"
    assert ag.SELF_REVIEW_FORBIDDEN == ("reviewer", "acceptor")


# —— 请求级路由的偏好（2026-10-31 路线 B）：偏好可以改选，但不许改规则


def _pool(*entries):
    return [ag.Candidate(agent_id, frozenset(roles)) for agent_id, roles in entries]


def test_a_routed_reviewer_beats_the_lexicographic_order():
    """点名的审查员在候选里 ⇒ 它被选中（而不是字典序最小的那个）。"""
    pool = _pool(("aaa", {"reviewer"}), ("qoder", {"reviewer"}), ("bbb", {"acceptor"}))
    picked = ag.route("T01", "api-executor", pool, preference={"reviewer": "qoder"})
    assert picked.reviewer == "qoder"
    assert picked.applied == {"reviewer": "qoder"}
    assert picked.ignored == {}


def test_an_ineligible_routing_preference_is_recorded_not_used():
    """点了名但不在当席候选里 ⇒ 不采纳，**进留痕**（不静默）。"""
    pool = _pool(("aaa", {"reviewer"}), ("bbb", {"acceptor"}))
    picked = ag.route("T01", "api-executor", pool, preference={"reviewer": "not-mine"})
    assert picked.reviewer == "aaa"
    assert picked.ignored == {"reviewer": "not-mine"}


def test_a_preference_cannot_put_the_executor_on_a_guard_seat():
    """偏好不能破规则 1：点名执行者去把关 ⇒ 当席候选里没有它 ⇒ 忽略并留痕。"""
    pool = _pool(
        ("api-executor", {"reviewer", "acceptor"}),
        ("aaa", {"reviewer"}),
        ("bbb", {"acceptor"}),
    )
    picked = ag.route("T01", "api-executor", pool, preference={"reviewer": "api-executor"})
    assert picked.reviewer == "aaa"
    assert picked.ignored["reviewer"] == "api-executor"


def test_routing_preferences_never_collapse_the_three_seats():
    """偏好可以改选，但**永远**留下三个两两不同的席位（FR-031 的判定单位是 agent_id）。"""
    pool = _pool(
        ("aaa", {"reviewer"}),
        ("bbb", {"acceptor"}),
        ("ccc", {"reviewer", "acceptor"}),
        ("ddd", {"reviewer", "acceptor"}),
    )
    for preference in (
        {"reviewer": "aaa", "acceptor": "aaa"},  # 两个席位点同一个（且其中一席没有它）
        {"reviewer": "ccc", "acceptor": "ccc"},  # 两席都能挂它
        {"reviewer": "ccc", "acceptor": "ddd"},
        {"reviewer": "not-in-pool"},
    ):
        picked = ag.route("T01", "executor-x", pool, preference=preference)
        triple = (picked.executor, picked.reviewer, picked.acceptor)
        assert len(set(triple)) == 3, f"偏好 {preference} 压塌了互斥：{triple}"
