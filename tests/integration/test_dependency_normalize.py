"""T033 的判据：依赖结构校验（FR-005）。

**本文件不碰 PG**，放在 `tests/integration/` 是按 `plan.md` 的目录分档（图相关用例归这一档），
副作用是不进 `-m fast` 先跑集 —— 与 `test_task_split.py` 同一条如实说明。

一条写法上的刻意选择：**判据全部写成关系，不写成"等于某个手写序列"**。
"返回 `(T01, T02, T03)`" 这种断言在实现换了另一个合法拓扑序时会假红，
而"每条依赖的前件都排在它后面"才是真正的 FR-005。同时输入顺序故意给成**乱的**，
否则实现哪怕原样返回输入顺序也能过（上一轮就踩过这种死判据：摘掉 `flush()` 仍全绿，
因为查询自己会 autoflush —— 同一类毛病在这里的形态就是"输入恰好已经是拓扑序"）。
"""

from __future__ import annotations

import pytest

from acp.orchestrate.nodes.normalize import (
    CYCLE,
    DANGLING,
    SELF_DEPENDENCY,
    DependencyViolation,
    normalize,
)
from acp.orchestrate.nodes.split import CardDraft
from acp.protocol import ArtifactFloor

FLOOR = ArtifactFloor(min_bytes=200, min_lines=10, format="html")


def card(task_id: str, depends_on: list[str] | None = None) -> CardDraft:
    return CardDraft(
        task_id=task_id,
        title=f"卡 {task_id}",
        difficulty=1,
        skill_tags=["html"],
        depends_on=depends_on or [],
        artifact_floor=FLOOR,
        acceptance_criteria=["有内容"],
    )


def order_is_respected(order: tuple[str, ...], deps: dict[str, list[str]]) -> None:
    """真正的 FR-005：每条边都必须在顺序里"前件在前"。"""
    position = {task_id: index for index, task_id in enumerate(order)}
    for task_id, listed in deps.items():
        for dep in listed:
            assert position[dep] < position[task_id], f"{dep} 必须排在 {task_id} 前面"


def test_a_valid_dag_is_ordered_with_every_predecessor_first():
    deps = {"T01": [], "T02": ["T01"], "T03": ["T01"], "T04": ["T02", "T03"]}
    # 输入顺序刻意不是拓扑序：T04 先出现，但它有两个前件。
    drafts = [card("T04", deps["T04"]), card("T03", deps["T03"]),
              card("T01"), card("T02", deps["T02"])]
    order = normalize(drafts)
    assert set(order) == set(deps)
    order_is_respected(order, deps)


def test_order_does_not_depend_on_the_input_order():
    deps = {"T01": [], "T02": ["T01"], "T03": ["T02"]}
    forward = normalize([card("T01"), card("T02", deps["T02"]), card("T03", deps["T03"])])
    backward = normalize([card("T03", deps["T03"]), card("T02", deps["T02"]), card("T01")])
    assert forward == backward, "同一份计划只因输入顺序不同就给出不同顺序 ⇒ 不可复现"


def test_diamond_is_not_mistaken_for_a_cycle():
    deps = {"T01": [], "T02": ["T01"], "T03": ["T01"], "T04": ["T02", "T03"]}
    order = normalize([card(k, deps[k]) for k in ("T04", "T02", "T03", "T01")])
    order_is_respected(order, deps)


def test_dangling_reference_is_rejected_and_named():
    with pytest.raises(DependencyViolation) as caught:
        normalize([card("T01"), card("T02", ["T99"])])
    reasons = caught.value.reasons
    assert len(reasons) == 1
    assert DANGLING in reasons[0] and "T99" in reasons[0] and "T02" in reasons[0]


def test_self_dependency_is_rejected_and_named():
    with pytest.raises(DependencyViolation) as caught:
        normalize([card("T01", ["T01"])])
    assert SELF_DEPENDENCY in caught.value.reasons[0]


def test_two_node_cycle_reports_a_closed_path():
    with pytest.raises(DependencyViolation) as caught:
        normalize([card("T01", ["T02"]), card("T02", ["T01"])])
    reason = caught.value.reasons[0]
    assert CYCLE in reason
    path = reason.split(": ", 1)[1].split(" → ")
    assert path[0] == path[-1], f"环必须是闭合路径，实际 {path}"
    assert set(path) == {"T01", "T02"}


def test_three_node_cycle_is_found_even_when_it_is_not_the_prefix():
    drafts = [card("T01"), card("T02", ["T03"]), card("T03", ["T04"]), card("T04", ["T02"])]
    with pytest.raises(DependencyViolation) as caught:
        normalize(drafts)
    assert {"T02", "T03", "T04"} <= set(caught.value.reasons[0].split(" → "))


def test_all_broken_edges_are_reported_in_one_pass():
    """同时有自依赖与悬空引用 ⇒ 两条原因一起给。

    只报第一条的话，"退回重排"要多跑一轮才知道还剩什么坏，而这一轮的等待成本是使用者付。
    """
    drafts = [card("T01", ["T01"]), card("T02", ["T77"])]
    with pytest.raises(DependencyViolation) as caught:
        normalize(drafts)
    assert len(caught.value.reasons) == 2
    assert any(SELF_DEPENDENCY in r for r in caught.value.reasons)
    assert any(DANGLING in r for r in caught.value.reasons)


def test_cycle_is_not_reported_as_a_bunch_of_dangling_edges():
    """成环时原因只该是"循环依赖"一条：把环摊成 N 条边原因，读者得自己重组成环。"""
    with pytest.raises(DependencyViolation) as caught:
        normalize([card("T01", ["T03"]), card("T02", ["T01"]), card("T03", ["T02"])])
    assert len(caught.value.reasons) == 1
    assert caught.value.reasons[0].startswith(CYCLE)


def test_single_card_with_no_dependencies_is_a_valid_plan():
    assert normalize([card("T01")]) == ("T01",)


def test_empty_input_is_not_this_nodes_decision():
    """空清单在这里**不报错**：FR-004 的"清单必须非空"归 `split.validate_split`。

    这条不是走过场 —— 它在钉"同一规则只有一个强制点"。哪天有人把非空检查也加到 normalize，
    两处都会拒，而报错文案不一致，使用者会以为换了个错误。
    """
    assert normalize([]) == ()


def test_violation_message_is_the_reasons_joined():
    """`str(exc)` 要能直接读给使用者（日志与卡点都取的是消息而不是属性）。"""
    with pytest.raises(DependencyViolation) as caught:
        normalize([card("T01", ["T99"])])
    assert caught.value.reasons[0] == str(caught.value)
