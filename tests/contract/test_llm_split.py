"""真拆单的判据：**零网络**，模型用假客户端替。

四条必须钉住：
1. 模型输出的卡要过既有规则（成环、引用、判据条数）；
2. 跑偏一律回落 Stub，且回落之后 `acceptance_links` 走的是回落那条路（不然映射与卡对不上）；
3. 标题里必须有业务主语 —— 那是执行器唯一能看到的"这是什么站"；
4. 多写字段就拒（协议之外的东西不许静默生效）。
"""

from __future__ import annotations

import json

import pytest

from acp.orchestrate.llm_split import LlmSplitter, SplitUnavailable, _prompt, links_of, to_drafts
from acp.run import StubOrchestrator

GOOD = {
    "cards": [
        {
            "task_id": "T01",
            "title": "手冲咖啡 · 全站样式表",
            "difficulty": 1,
            "depends_on": [],
            "format": "css",
            "acceptance": ["styles.css 存在且非空", "含 375px 断点"],
        },
        {
            "task_id": "T02",
            "title": "手冲咖啡 · 首页（导航 + 主推品）",
            "difficulty": 2,
            "depends_on": ["T01"],
            "format": "html",
            "acceptance": ["首页出现 3 款手冲单品与价格", "导航可跳到商品页"],
        },
        {
            "task_id": "T03",
            "title": "手冲咖啡 · 联系我们",
            "difficulty": 2,
            "depends_on": ["T01"],
            "format": "html",
            "acceptance": ["门店地址与营业时间可见", "手机号可点击拨打"],
        },
    ]
}


class FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self.reply


def _ok_cards():
    return to_drafts(GOOD)


def test_good_json_becomes_valid_cards_with_business_subject() -> None:
    cards = _ok_cards()
    assert [card.task_id for card in cards] == ["T01", "T02", "T03"]
    assert cards[0].artifact_floor.format == "css"
    assert cards[0].depends_on == []
    assert cards[1].depends_on == ["T01"]
    # 业务主语是这一层的产出：执行器的 objective 就是这张卡的标题
    assert all("手冲咖啡" in card.title for card in cards)
    assert len(cards[1].acceptance_criteria) == 2


def test_links_cover_every_criterion_exactly_once() -> None:
    cards = _ok_cards()
    links = links_of(cards)
    assert len(links) == sum(len(card.acceptance_criteria) for card in cards)
    for card in cards:
        for criterion in card.acceptance_criteria:
            hits = [link for link in links if link.task_id == card.task_id and link.acceptance_item == criterion]
            assert len(hits) == 1


def test_unknown_field_is_rejected() -> None:
    payload = {"cards": [dict(GOOD["cards"][0], owner="pm")]}
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts(payload)
    assert caught.value.code == "split_extra_field"


def test_cycle_is_rejected_by_the_existing_rules() -> None:
    payload = json.loads(json.dumps(GOOD))
    payload["cards"][0]["depends_on"] = ["T02"]  # T01 ← T02 ← T01
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts(payload)
    assert caught.value.code == "split_rules_rejected"


def test_too_few_criteria_is_rejected() -> None:
    payload = json.loads(json.dumps(GOOD))
    payload["cards"][0]["acceptance"] = ["只有一条"]
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts(payload)
    assert caught.value.code == "split_criteria_count"


def test_bad_json_falls_back_to_stub_and_links_follow_the_fallback() -> None:
    fallback = StubOrchestrator()
    splitter = LlmSplitter(client=FakeChat("我拆不出来"), raw_input="做一个手冲咖啡官网", fallback=fallback)
    cards = splitter.split({"会议": ["orchestrator｜先定验收"]})

    assert cards == fallback.split({"会议": ["orchestrator｜先定验收"]})
    assert splitter.last_error == "split_not_json"
    assert splitter.acceptance_links({}) == fallback.acceptance_links({})


def test_rules_rejection_falls_back_and_reports_the_code() -> None:
    payload = json.loads(json.dumps(GOOD))
    payload["cards"][0]["depends_on"] = ["T99"]  # 引用不存在的卡
    splitter = LlmSplitter(
        client=FakeChat(json.dumps(payload, ensure_ascii=False)),
        raw_input="x",
        fallback=StubOrchestrator(),
    )
    cards = splitter.split({})
    assert splitter.last_error == "split_rules_rejected"
    assert len(cards) == 4  # 回落成 Stub 的四张


def test_prompt_carries_requirement_and_meeting_lines() -> None:
    chat = FakeChat(json.dumps(GOOD, ensure_ascii=False))
    splitter = LlmSplitter(client=chat, raw_input="做一个卖手冲咖啡的静态官网")
    splitter.split({"会议": ["pm｜共 3 页"], "自行补全项": ["联系方式取邮箱"]})
    prompt = chat.prompts[0]
    assert "做一个卖手冲咖啡的静态官网" in prompt
    assert "pm｜共 3 页" in prompt
    assert "联系方式取邮箱" in prompt


def test_without_fallback_a_failure_is_loud() -> None:
    splitter = LlmSplitter(client=FakeChat("not json"), raw_input="x", fallback=None)
    with pytest.raises(SplitUnavailable) as caught:
        splitter.split({})
    assert caught.value.code == "split_not_json"


# ---------------------------------------------------------------- 六、PRD 优先


def _digest_with_prd() -> dict[str, list[str]]:
    return {
        "PRD": [
            json.dumps(
                {
                    "title": "手冲咖啡官网",
                    "pages": [{"label": "首页", "file": "index.html", "duty": "品牌与主推品"}],
                    "acceptance": ["首屏能看到主推品"],
                    "out_of_scope": ["在线支付"],
                },
                ensure_ascii=False,
            )
        ],
        "会议": ["产品：三个页面"],
    }


def test_the_prd_outranks_the_meeting_lines():
    """PRD 是这一步的**契约输入**，台词只是讨论过程。

    两者冲突时按 PRD 拆 —— 否则"会后落文档"那一步等于白跑。
    """
    text = _prompt("做一个手冲咖啡的静态官网", _digest_with_prd())
    assert "产品已定稿的 PRD" in text
    assert "首页（index.html）：品牌与主推品" in text
    assert "首屏能看到主推品" in text
    assert "本期不做：在线支付" in text
    assert text.index("产品已定稿的 PRD") < text.index("立项会结论"), (
        "PRD 段排在了会议段之后 —— 模型按先读到的为准，顺序就是优先级"
    )


def test_without_a_prd_it_falls_back_to_the_meeting_only():
    text = _prompt("做一个咖啡站", {"会议": ["产品：三个页面"]})
    assert "产品已定稿的 PRD" not in text
    assert "产品：三个页面" in text


def test_a_broken_prd_section_does_not_take_the_split_down():
    """老单没有「PRD」段、或者那一段坏掉，都只该退回台词，不该抛。"""
    text = _prompt("做一个咖啡站", {"PRD": ["{这不是 JSON"], "会议": ["产品：三个页面"]})
    assert "产品已定稿的 PRD" not in text
    assert "产品：三个页面" in text


# ---------------------------------------------------------------- 摘要过 plan 节点之后 PRD 还在不在

def test_the_prd_survives_the_plan_node_shape_check():
    """`plan.validate_summary_digest` 只该校验 FR-002 那两项，其余段必须原样带过去。

    2026-10-28 真跑实测的根因：这个函数原来 `return checked`（只有「自行补全项」「澄清问题」），
    而它的返回值就是图 state 里 `summary_digest` 的全部内容 ⇒ `_prd_of()` 永远读到 None ⇒
    **"PRD 定死后确定性投影"从未生效**，每次回落模型拆单；连拆单提示词里也没有 PRD，
    于是执行器自己给页面起名（`kanban.html`，PRD 要的是 `dashboard.html`），
    E2E 检查判"不通过"、组包 `entry_absent` —— 一条根因、四个下游症状。

    这条用例从"摘要进 plan 节点"一直断到"投影真的按 PRD 拆出卡"，中间不许断链。
    """
    from acp.orchestrate.llm_split import _prd_of, skeleton_from_prd
    from acp.orchestrate.nodes.plan import validate_summary_digest

    digest = dict(_digest_with_prd())
    digest["自行补全项"] = []
    digest["澄清问题"] = []

    passed = validate_summary_digest(digest)

    assert passed["自行补全项"] == []
    assert "PRD" in passed, "PRD 段在 plan 节点被丢掉了 ⇒ 确定性投影永远不生效"
    assert passed["会议"] == ["产品：三个页面"], "会议段被丢掉了"

    prd = _prd_of(passed)
    assert prd is not None, "过完 plan 节点就读不出 PRD"
    drafts = skeleton_from_prd(prd)
    assert [draft.task_id for draft in drafts] == ["T01", "T02", "T03", "T04"]
    # 页面卡必须按 PRD 的 `file` 命名 —— 这是"执行器该写哪个文件"的唯一来源
    assert drafts[1].title.endswith("首页"), drafts[1].title


def test_the_plan_shape_check_still_refuses_a_digest_without_the_self_completed_key():
    """放行其余段不等于放宽 FR-002：缺「自行补全项」照样拒。"""
    from acp.orchestrate.nodes.plan import IncompleteDigest, validate_summary_digest

    with pytest.raises(IncompleteDigest, match="自行补全项"):
        validate_summary_digest({"PRD": ["{}"], "澄清问题": []})


def test_non_string_list_sections_are_skipped_not_crashed():
    """别的段形状不对（不是字符串列表）就跳过 —— 校验别人的契约是越界。"""
    from acp.orchestrate.nodes.plan import validate_summary_digest

    passed = validate_summary_digest({"自行补全项": [], "澄清问题": [], "怪段": [1, 2, 3], "对象段": {"a": 1}})
    assert "怪段" not in passed
    assert "对象段" not in passed


# ---------------------------------------------------------------- 页面卡的判据必须点名文件（U3）

def test_every_page_card_names_the_file_it_must_write():
    """每张页面卡的第二句判据都是"产物文件名必须是 `<PRD 的 file>`"（U3，2026-10-28 真跑）。

    为什么这条值得单独钉：`file` 原来只出现在**第 2 页起**的那句"可通过站内导航到达"里，
    第一页的卡完全不带文件名 ⇒ 执行器自己起名 `kanban.html`，而 PRD 要的是 `dashboard.html`
    ⇒ E2E 判"不通过"、组包 `entry_absent`，交付包永远出不来。

    文件名是这条链上唯一不能漂的锚点：卡说写哪个文件，执行器就写哪个文件。
    """
    from acp.orchestrate.llm_split import skeleton_from_prd

    prd = {
        "title": "看板",
        "pages": [
            {"label": "总览", "file": "dashboard.html", "duty": "一屏看完指标"},
            {"label": "明细", "file": "detail.html", "duty": "逐条下钻"},
        ],
        "acceptance": ["首屏能看到三个指标卡"],
        "out_of_scope": [],
    }

    drafts = skeleton_from_prd(prd)
    pages = [draft for draft in drafts if draft.title.endswith(("总览", "明细"))]

    assert len(pages) == 2
    for draft, filename in zip(pages, ("dashboard.html", "detail.html")):
        assert f"产物文件名必须是 {filename}" in draft.acceptance_criteria, draft.acceptance_criteria

    # 第一页仍然要带上 PRD 的验收判据 —— 加了文件名不能把原来那句挤掉。
    assert "首屏能看到三个指标卡" in pages[0].acceptance_criteria, pages[0].acceptance_criteria


def test_the_split_prompt_forbids_artifacts_the_bus_would_refuse() -> None:
    """拆单提示词必须写明"产物只有静态文件"（2026-10-28 的真实事故）。

    现场：客户原话只说"要有咨询表单"，模型把验收标准扩成"提交数据存入 MySQL + 通过 SMTP 发信 +
    邮件失败触发漏单告警"。执行器只能写一支 `submit-contact.php` 去凑，而总线只收
    html / css / md / json / py ⇒ `unsupported_format`，这张卡**永远**过不了，
    整单停在 `SPLIT`（需求单状态）与 `ESCALATED`（卡态）上等人。

    这条判据钉的是"提示词里那几句不许被删"：删了它，下一次拆单还会写出一条平台做不到的判据，
    而表现是"执行器很笨"，不是"拆单越界"。
    """
    from acp.orchestrate.llm_split import SYSTEM

    for suffix in ("html", "css", "md", "json", "py"):
        assert suffix in SYSTEM, f"提示词里没有点名产物后缀 {suffix}"
    # 三类"平台没有的东西"必须被点名拒掉，否则模型会换个说法接着写。
    for forbidden in ("数据库", "邮件", "后端接口"):
        assert forbidden in SYSTEM, f"提示词没有拦住 {forbidden}"
    # 表单类需求要给一个**做得到**的替代表述，不然模型只知道"不许"，不知道"改成什么"。
    assert "成功提示" in SYSTEM
