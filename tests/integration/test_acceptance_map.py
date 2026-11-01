"""T034 的判据：`Split` 同步生成的验收映射表形状（FR-007｜R-10）。

**本文件不碰 PG**（放在 integration 档是沿 `plan.md` 的目录分档，副作用与 T032 / T033 同一条：
它不进 `-m fast` 先跑集，如实写明）。落库那半段在 `test_orchestrate_graph.py` 的端到端那条里读。

一条写法上的选择：负例一律**只改一处**（漏覆盖 / 重复 / 卡外判据 / 指向不存在的卡 / 四个字段空白），
因为 `validate_links` 一次收齐全部原因，把两种坏混在一个用例里就读不出"这条红是因为哪一条"。
"""

from __future__ import annotations

import pytest

from acp.orchestrate.nodes.split import (
    AcceptanceLink,
    AcceptanceMapNotConfigured,
    CardDraft,
    MappingViolation,
    SplitNotConfigured,
    split_with_acceptance_map,
    validate_links,
)
from acp.protocol import ArtifactFloor

DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": []}
FLOOR = ArtifactFloor(min_bytes=200, min_lines=10, format="html")
PRODUCER = "orchestrator-qoder"


def card(task_id: str, criteria: list[str] | None = None) -> CardDraft:
    return CardDraft(
        task_id=task_id,
        title=f"卡 {task_id}",
        difficulty=1,
        skill_tags=["html"],
        depends_on=[],
        artifact_floor=FLOOR,
        acceptance_criteria=criteria or ["首屏有公司名"],
    )


def link(task_id="T01", item="首屏有公司名", requirement="需求：首页要能介绍公司", producer=PRODUCER):
    return AcceptanceLink(
        task_id=task_id,
        requirement_item=requirement,
        acceptance_item=item,
        produced_by=producer,
    )


class Splitter:
    """一次拆单给两件产物（卡 + 映射），并且两件都收到同一份摘要。"""

    def __init__(self, drafts, links):
        self.drafts = drafts
        self.links = links
        self.split_seen: list = []
        self.links_seen: list = []

    def split(self, summary_digest):
        self.split_seen.append(dict(summary_digest))
        return list(self.drafts)

    def acceptance_links(self, summary_digest):
        self.links_seen.append(dict(summary_digest))
        return list(self.links)


class LegacySplitter:
    """只有 `split` 的旧形状适配器（Phase 3 里真可能出现这种半成品）。"""

    def split(self, summary_digest):
        return [card("T01")]


def reasons(exc):
    return str(exc.value)


# ------------------------------------------------------------------ 覆盖关系（FR-030 的依据）


def test_every_declared_criterion_gets_exactly_one_link():
    drafts = [card("T01", ["首屏有公司名", "移动端不溢出"]), card("T02", ["配色统一"])]
    links = [
        link("T01", "首屏有公司名"),
        link("T01", "移动端不溢出"),
        link("T02", "配色统一", requirement="需求：整站配色一致"),
    ]
    out = validate_links(drafts, links)
    assert len(out) == 3


def test_uncovered_criterion_is_rejected_and_named():
    """漏一条映射 ⇒ FR-030 的"逐条判定"会漏判那条，而验收看起来仍然"全部通过"。"""
    drafts = [card("T01", ["首屏有公司名", "移动端不溢出"])]
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [link("T01", "首屏有公司名")])
    message = reasons(exc)
    assert "移动端不溢出" in message and "T01" in message


def test_the_same_criterion_mapped_twice_is_rejected():
    """重复映射把"轮次上限"偷走：上限 3 是三**轮**判定，不是三轮 × 每轮判两遍。"""
    drafts = [card("T01", ["首屏有公司名"])]
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [link("T01", "首屏有公司名"), link("T01", "首屏有公司名")])
    assert "被映射 2 次" in reasons(exc)


def test_a_link_introducing_a_criterion_the_card_never_declared_is_rejected():
    """借映射表塞进卡上没写的判据 = FR-029 反对的那件事，只是发生在生成侧。"""
    drafts = [card("T01", ["首屏有公司名"])]
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [link("T01", "首页必须有动画")])
    assert "卡上没有的判据" in reasons(exc) and "首页必须有动画" in reasons(exc)


def test_a_link_pointing_at_a_card_that_is_not_in_the_batch_is_rejected():
    drafts = [card("T01")]
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [link("T99", "首屏有公司名")])
    assert "T99" in reasons(exc)


def test_no_links_at_all_is_a_violation_when_there_are_cards():
    """`count() == 0` 在 FR-007 下不是"暂时没做"，是坏产物 —— 这里把它拒在形状层。"""
    with pytest.raises(MappingViolation):
        validate_links([card("T01")], [])


@pytest.mark.parametrize(
    "field", ["task_id", "requirement_item", "acceptance_item", "produced_by"],
    ids=["卡号空白", "需求条目空白", "判据空白", "申报方空白"],
)
def test_each_of_the_four_fields_must_be_non_blank(field):
    """四个字段各拆一刀：只测其中一个，另外三个就能悄悄写空。"""
    drafts = [card("T01")]
    kwargs = {"task_id": "T01", "requirement_item": "需求：首页", "acceptance_item": "首屏有公司名",
              "produced_by": PRODUCER}
    kwargs[field] = "   "
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [AcceptanceLink(**kwargs)])
    assert field in reasons(exc)


def test_all_broken_links_are_reported_in_one_pass():
    """两条坏（漏覆盖 + 卡外判据）要一次说完，否则总管要多撞一轮。"""
    drafts = [card("T01", ["首屏有公司名", "移动端不溢出"])]
    with pytest.raises(MappingViolation) as exc:
        validate_links(drafts, [link("T01", "首页必须有动画")])
    assert len(exc.value.args[0].split("；")) >= 2


def test_producer_is_carried_verbatim():
    """`produced_by` 是申报方给的，平台不改写、不补默认值（EN-07 文档明写这列不加外键）。"""
    drafts = [card("T01")]
    out = validate_links(drafts, [link("T01", producer="orchestrator-workbuddy")])
    assert out[0].produced_by == "orchestrator-workbuddy"


# ------------------------------------------------------------------ 同步生成的入口


def test_split_and_map_are_produced_from_the_same_digest():
    drafts = [card("T01", ["首屏有公司名"])]
    splitter = Splitter(drafts, [link("T01", "首屏有公司名")])
    out_drafts, out_links = split_with_acceptance_map(DIGEST, splitter=splitter)
    assert [d.task_id for d in out_drafts] == ["T01"]
    assert len(out_links) == 1
    assert splitter.split_seen == [DIGEST] and splitter.links_seen == [DIGEST], (
        "两件产物必须出自同一份摘要；如果 links 收到的是别的东西，映射就对不上这批卡"
    )


def test_a_splitter_without_acceptance_links_fails_closed():
    """旧形状适配器（只有 `split`）不许"拆单成功、映射为空"地跑过去。"""
    with pytest.raises(AcceptanceMapNotConfigured):
        split_with_acceptance_map(DIGEST, splitter=LegacySplitter())


def test_missing_splitter_still_fails_before_the_map_gate():
    with pytest.raises(SplitNotConfigured):
        split_with_acceptance_map(DIGEST, splitter=None)


def test_the_map_does_not_revalidate_the_card_shape():
    """分工判据：卡本身的坏形状由 `validate_split` 拒，不该在映射层冒出第二种报错文案。

    这条与 T033 的 `test_empty_input_is_not_this_nodes_decision` 同一个用途 ——
    哪天有人把卡的判据也搬进 `validate_links`，两处就会对同一种坏给出不同消息。
    """
    drafts = [card("T01", ["首屏有公司名"])]
    out_drafts, out_links = split_with_acceptance_map(
        DIGEST, splitter=Splitter(drafts, [link("T01", "首屏有公司名")])
    )
    assert len(out_drafts) == 1 and len(out_links) == 1


def test_extra_links_beyond_the_cards_are_rejected():
    """映射多于卡（多出一条指向不存在的卡）也在形状层拒掉，不等库的复合外键。"""
    drafts = [card("T01", ["首屏有公司名"])]
    with pytest.raises(MappingViolation):
        validate_links(drafts, [link("T01", "首屏有公司名"), link("T02", "配色统一")])
