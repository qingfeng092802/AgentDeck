"""拆单的**机器契约**判据：严格形状 + 重试一次 + 不回落（R-26 卡点）。

## 这一组要钉住的三件事，以及为什么不是别的三件

1. **形状**：`{"cards":[…]}`，每卡六个键，`extra="forbid"`。多写一个键整单被拒 ——
   收下它就等于让"协议之外的东西"静默生效（FR-009）。
2. **重试是带着原因重问**，不是重采样：同一份 prompt 再问一遍，模型大概率再错一次。
3. **两次都失败时不回落**：Stub 会给出一份通用卡，那正是 2026-10-28 两张卡
   `ARTIFACT_INVALID` 的起点。回落把"模型跑偏"变成"看起来成功、下游全死"。
   抛出的码**恒为** `split_needs_human`（2026-10-28 方案二裁决），最后一次的具体原因进
   `cause_code` 与消息 —— 事件码要稳定、诊断码要具体，两者不是一件事。

## 一处必须写明的口径纠正

原始需求里写的是"大模型会返回 JSON 数组……要求它返回严格的 JSON 结构（包含 `guards` 和 `bus_gate`）"。
这与本仓的契约**正好相反**，实测两条都会被拒：
- 顶层数组：`_json_object` 取第一个 `{` 到最后一个 `}`，数组在那一步就不是"JSON 对象"了；
- `guards` / `bus_gate`：它们是编排层的概念（守卫、总线闸门），不是 EN-02 的列，
  `extra="forbid"` 会把**整单**拒掉。

所以这里的判据钉的是**真实契约**：数组被拒、`guards` 被拒。`test_guards_and_bus_gate_are_rejected`
就是那一行纠正的落点 —— 它是绿的，才说明"不许脏数据过关"这句话在代码里成立。
"""

from __future__ import annotations

import json

import pytest

from acp.orchestrate.llm_split import (
    NEEDS_HUMAN_CODE,
    LlmSplitter,
    SplitNeedsHuman,
    SplitUnavailable,
    to_drafts,
)

CARD = {
    "task_id": "T01",
    "title": "手冲咖啡 · 全站样式表",
    "difficulty": 1,
    "depends_on": [],
    "format": "css",
    "acceptance": ["含 :root 变量", "含 375px 断点"],
}

GOOD = {
    "cards": [
        CARD,
        {
            "task_id": "T02",
            "title": "手冲咖啡 · 首页",
            "difficulty": 2,
            "depends_on": ["T01"],
            "format": "html",
            "acceptance": ["首屏有 3 款主推豆", "导航可跳到其余两页"],
        },
    ]
}


class ScriptedChat:
    """按顺序回话的假客户端。用完之后重复最后一条 —— 测试不该因为多问一次而崩。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self.replies[min(len(self.prompts), len(self.replies)) - 1]


class StubFallback:
    """最小回落体：只要被调用就返回一张标记卡，好让"有没有回落"可断言。"""

    def split(self, digest):  # noqa: ANN001, ANN201 - 判据只看它有没有被调
        raise AssertionError("不该回落到 Stub")

    def acceptance_links(self, digest):  # noqa: ANN001, ANN201
        return ()


# ---------------------------------------------------------------- 形状


def test_the_top_level_array_is_rejected_with_a_clean_code() -> None:
    """顶层数组曾是 `TypeError: unhashable type: 'dict'` —— 一个未捕获的异常。

    它落进 `split()` 的 `except Exception` 后变成 `split_client_failed:TypeError`，
    排障时看不出是"模型回了数组"。
    """
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts([CARD])  # type: ignore[arg-type]
    assert caught.value.code == "split_bad_json"


def test_guards_and_bus_gate_are_rejected() -> None:
    """这两个键是**编排层**的字段，不是卡的列。带上它们整单被拒，不是只丢那两个键。"""
    for extra in ({"guards": ["triple"]}, {"bus_gate": "on"}, {"layer": 2}):
        payload = {"cards": [{**CARD, **extra}]}
        with pytest.raises(SplitUnavailable) as caught:
            to_drafts(payload)
        assert caught.value.code == "split_extra_field", f"{extra} 没有被拒"


def test_a_non_numeric_difficulty_is_rejected_instead_of_crashing() -> None:
    """`int("2a")` 原来抛 `ValueError`，同样不是 `SplitUnavailable`。"""
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts({"cards": [{**CARD, "difficulty": "2a"}]})
    assert caught.value.code == "split_bad_card"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"cards": []}, "split_no_cards"),
        ({"cards": {}}, "split_no_cards"),
        ({"cards": [], "extra": 1}, "split_extra_field"),
        ({"cards": [{"task_id": "T01", "title": "x", "depends_on": [], "format": "css"}]}, "split_missing_field"),
        ({"cards": [{**CARD, "format": "tsx"}]}, "split_bad_format"),
        ({"cards": [{**CARD, "depends_on": "T01"}]}, "split_bad_deps"),
        ({"cards": [{**CARD, "acceptance": ["只有一条"]}]}, "split_criteria_count"),
    ],
)
def test_the_existing_error_codes_are_unchanged(payload, code: str) -> None:
    """错误码是留痕与判据共用的键，换一套码等于让两边各说各话。"""
    with pytest.raises(SplitUnavailable) as caught:
        to_drafts(payload)
    assert caught.value.code == code


# ---------------------------------------------------------------- 重试


def test_the_retry_repeats_the_question_with_the_failure_reason() -> None:
    """第二次问的是**带着原因**的问题：只说"再来一次"，模型会照原样再错一遍。"""
    chat = ScriptedChat("我拆不出来", json.dumps(GOOD, ensure_ascii=False))
    splitter = LlmSplitter(client=chat, raw_input="做一个手冲咖啡站")

    drafts = splitter.split({"会议": ["产品：三页"]})

    assert [draft.task_id for draft in drafts] == ["T01", "T02"]
    assert len(chat.prompts) == 2, "重试恰好一次"
    assert "上一次的回话被拒（split_not_json）" in chat.prompts[1]
    assert splitter.last_error is None
    assert splitter.last_source == "llm"


def test_a_single_bad_reply_does_not_fall_back_any_more() -> None:
    """第一次错、第二次对 ⇒ 不回落到 Stub（旧行为是第一次错就回落）。"""
    chat = ScriptedChat(json.dumps({"cards": [{**CARD, "guards": []}]}), json.dumps(GOOD))
    splitter = LlmSplitter(client=chat, raw_input="做一个手冲咖啡站", fallback=StubFallback())

    drafts = splitter.split({"会议": ["产品：三页"]})

    assert len(drafts) == 2


# ---------------------------------------------------------------- 不回落（R-26）


def test_two_failures_escalate_to_a_human_instead_of_falling_back() -> None:
    """`escalate_on_failure=True`：两次都错就抛 `SplitNeedsHuman`，**不调回落体**。

    回落体故意写成"一调就断言失败"：这条判据同时证明"抛了"和"没回落"。
    """
    chat = ScriptedChat("not json at all")
    splitter = LlmSplitter(
        client=chat,
        raw_input="做一个手冲咖啡站",
        fallback=StubFallback(),
        escalate_on_failure=True,
    )

    with pytest.raises(SplitNeedsHuman) as caught:
        splitter.split({"会议": ["产品：三页"]})

    assert caught.value.code == NEEDS_HUMAN_CODE == "split_needs_human"
    assert caught.value.cause_code == "split_not_json", "诊断码丢了 ⇒ 排障只剩一句「要人处理」"
    assert caught.value.attempts == 2
    assert isinstance(caught.value, SplitUnavailable), "老代码里 except SplitUnavailable 要能接住它"
    assert len(chat.prompts) == 2
    assert splitter.last_error == "split_not_json"
    assert "split_not_json" in str(caught.value), "原因码要进消息：留痕的 error 就是这一句"


def test_the_human_code_does_not_change_with_the_cause() -> None:
    """**这条是方案二的全部要害**：不管最后一次错在哪，对外码恒为 `split_needs_human`。

    为什么值得单钉一条：前端与读面（`RequestView.last_error_code`）靠这个码判"这一单在等人"。
    若抛的是最后一次的 `split_extra_field` / `split_bad_deps`，前端就得维护一张越来越长的枚举 ——
    漏一个的表现是"该显示琥珀色的单显示成了红色失败"，而那正是这次要修的东西。
    诊断码不是没了，它进 `cause_code` 与消息。
    """
    causes = [
        ("not json at all", "split_not_json"),
        (json.dumps({"cards": [{**CARD, "guards": []}]}), "split_extra_field"),
        # 花括号配对、中间坏掉 ⇒ 走的是 `split_bad_json` 那一条，不是 `split_no_cards`
        ('{"cards": [}', "split_bad_json"),
    ]
    seen: list[str] = []
    for reply, expected_cause in causes:
        splitter = LlmSplitter(client=ScriptedChat(reply), raw_input="x", escalate_on_failure=True)
        with pytest.raises(SplitNeedsHuman) as caught:
            splitter.split({"会议": ["产品：三页"]})
        assert caught.value.code == "split_needs_human"
        assert caught.value.cause_code == expected_cause
        seen.append(caught.value.code)

    assert seen == ["split_needs_human"] * len(causes), f"对外码跟着原因变了：{seen}"


def test_the_fallback_still_works_when_escalation_is_off() -> None:
    """默认仍是旧行为：回落 Stub。改默认值会静默改掉一整条生产路径。"""

    class CountingFallback(StubFallback):
        calls = 0

        def split(self, digest):  # noqa: ANN001, ANN201
            CountingFallback.calls += 1
            return to_drafts(GOOD)

    chat = ScriptedChat("not json")
    splitter = LlmSplitter(client=chat, raw_input="x", fallback=CountingFallback())

    drafts = splitter.split({"会议": ["产品：三页"]})

    assert CountingFallback.calls == 1
    assert len(drafts) == 2
    assert splitter.last_error == "split_not_json"
    # 2026-10-31 起降级必须**自报来源**（P0-2）：`graph._split` 靠这一格写 EN-01 的
    # `split_source` 与 EN-14 的 `split_fallback`。回退成 `None` 会让屏上与库里都读不出
    # "这单没接真模型" —— 那正是本轮真跑里发生的事（key 失效，静默用 Stub）。
    assert splitter.last_source == "fallback_stub"
