"""文档节点的判据：**修得回来的修，修不回来的整份拒**。

这一层产出的三份文档是拆单的契约输入、验收的对照物、界面三格的内容源。
所以判据量的不是"能不能出字"，而是"出了错字会不会被收下"：

- 值层面的错（文件名大小写、颜色写法、页数超了、判据多一条）→ **修 + 留痕**，
  拒掉它换不来正确性，只换来一次 16–45 秒的模型调用白等；
- 结构层面的错（三份缺一份、pages 空、页面说不清是什么）→ **整份拒**，
  收下来等于拿"半份文档"当"文档已定稿"。

零网络、零库：模型客户端是假的（与 `test_meeting_service.py` 同一条纪律）。
"""

from __future__ import annotations

import json

import pytest

from acp.docs import (
    ARCH_SECTION,
    PRD_SECTION,
    TOKENS_SECTION,
    DocumentUnavailable,
    run_documents,
    to_docs,
)

GOOD = {
    "prd": {
        "title": "手冲咖啡官网",
        "pages": [
            {"label": "首页", "file": "index.html", "duty": "品牌介绍与主推品"},
            {"label": "商品", "file": "products.html", "duty": "列出三种豆子"},
            {"label": "联系", "file": "contact.html", "duty": "门店地址与留言表单"},
        ],
        "acceptance": ["首屏能看到主推品与购物车按钮", "375px 宽下导航不换行"],
        "out_of_scope": ["在线支付"],
    },
    "architecture": {
        "stack": "原生 HTML/CSS/JS 静态站",
        "why": "三页内容固定，不需要框架",
        "risk": "购物车只做 UI，容易让人误以为能下单",
    },
    "tokens": {
        "name": "烘焙棕 / 米白",
        "bg": "#FBF7F0",
        "surface": "#FFFFFF",
        "ink": "#2B1D14",
        "muted": "#7A6552",
        "accent": "#6B4226",
        "accent2": "#C0763C",
        "line": "#E8DCCB",
    },
}


class FakeChat:
    def __init__(self, reply: str, finish: str | None = "stop") -> None:
        self.reply = reply
        self.last_finish_reason = finish
        self.seen: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        return self.reply


def reply_of(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def broken(mutate) -> dict:
    payload = json.loads(json.dumps(GOOD))
    mutate(payload)
    return payload


# ---------------------------------------------------------------- 一、好回语


def test_a_good_reply_becomes_three_sections_that_can_be_read_back():
    docs = run_documents("做一个卖手冲咖啡的静态官网", ["产品：三个页面"], client=FakeChat(reply_of(GOOD)))

    digest = docs.digest()
    assert set(digest) == {PRD_SECTION, ARCH_SECTION, TOKENS_SECTION}
    for section in (PRD_SECTION, ARCH_SECTION, TOKENS_SECTION):
        assert len(digest[section]) == 1, f"{section} 段应当是一整份 JSON 的一行，实际 {len(digest[section])} 行"
        assert isinstance(json.loads(digest[section][0]), dict), f"{section} 那一段反解不出对象"

    prd = json.loads(digest[PRD_SECTION][0])
    assert prd["title"] == "手冲咖啡官网"
    assert [page["file"] for page in prd["pages"]] == ["index.html", "products.html", "contact.html"]
    assert prd["out_of_scope"] == ["在线支付"]


def test_the_prompt_carries_the_raw_words_and_what_the_meeting_said():
    client = FakeChat(reply_of(GOOD))
    run_documents("做一个卖手冲咖啡的静态官网", ["产品：三个页面"], client=client)
    _, user = client.seen[0]
    assert "做一个卖手冲咖啡的静态官网" in user
    assert "产品：三个页面" in user


def test_the_three_files_are_the_projection_and_keep_the_business_words():
    docs = run_documents("做一个卖手冲咖啡的静态官网", [], client=FakeChat(reply_of(GOOD)))
    files = docs.files()
    assert set(files) == {"prd.md", "architecture.md", "tokens.json"}
    assert "手冲咖啡官网" in files["prd.md"]
    assert "products.html" in files["prd.md"]
    assert "原生 HTML/CSS/JS 静态站" in files["architecture.md"]
    assert json.loads(files["tokens.json"])["accent"] == "#6B4226"


def test_a_fenced_reply_is_still_read():
    fenced = "```json\n" + reply_of(GOOD) + "\n```"
    docs = run_documents("咖啡站", [], client=FakeChat(fenced))
    assert docs.prd["title"] == "手冲咖啡官网"


# ---------------------------------------------------------------- 二、坏回语


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p["prd"].pop("pages"), "docs_no_pages"),
        (lambda p: p["prd"].update(acceptance=[]), "docs_no_acceptance"),
        (lambda p: p["prd"]["pages"].__setitem__(0, "首页"), "docs_bad_page"),
        (lambda p: p["prd"]["pages"][0].update(duty=""), "docs_missing_field"),
        (lambda p: p["tokens"].update(accent="深咖色"), "docs_bad_color"),
        (lambda p: p["tokens"].pop("ink"), "docs_bad_color"),
        (lambda p: p["architecture"].update(stack=""), "docs_missing_field"),
        (lambda p: p.pop("tokens"), "docs_missing_field"),
    ],
)
def test_a_broken_reply_is_refused_whole(mutate, code):
    with pytest.raises(DocumentUnavailable) as caught:
        to_docs(broken(mutate))
    assert caught.value.code == code


# ---------------------------------------------------------------- 三、修得回来的


def test_a_page_list_that_runs_long_is_cut_not_refused():
    """七页不是"结构跑偏"，是模型把导航项当成了页面。前五页仍然是对的。"""
    payload = broken(lambda p: p["prd"]["pages"].extend([p["prd"]["pages"][0]] * 4))

    docs = to_docs(payload)

    assert len(docs.prd["pages"]) == 5
    assert "pages_truncated:7→5" in docs.extras


def test_a_file_name_is_normalised_instead_of_refusing_the_whole_doc():
    """大写、带目录、少个 l、中文名 —— 都是同一个意思的另一种写法。"""
    payload = broken(lambda p: None)
    payload["prd"]["pages"][0]["file"] = "Index.HTML"
    payload["prd"]["pages"][1]["file"] = "pages/products.html"
    payload["prd"]["pages"][2]["file"] = "联系.htm"

    docs = to_docs(payload)

    assert [page["file"] for page in docs.prd["pages"]] == ["index.html", "products.html", "page-3.html"]
    assert "file_repaired:Index.HTML→index.html" in docs.extras


def test_two_pages_landing_on_the_same_file_are_split_apart():
    """同名 = 两张卡写同一份产物，组包时后一份静默盖掉前一份。"""
    payload = broken(lambda p: None)
    payload["prd"]["pages"][1]["file"] = "index.html"

    docs = to_docs(payload)

    assert [page["file"] for page in docs.prd["pages"]] == ["index.html", "index-2.html", "contact.html"]
    assert "file_duplicate:index.html→index-2.html" in docs.extras


def test_one_acceptance_line_is_kept_with_a_note():
    """少写一条不会让剩下的那条失效；多出来的那条也没法凭空补。"""
    docs = to_docs(broken(lambda p: p["prd"].update(acceptance=["首屏能看到主推品"])))

    assert docs.prd["acceptance"] == ["首屏能看到主推品"]
    assert "acceptance_short:1<2" in docs.extras


def test_acceptance_that_runs_long_is_cut_not_refused():
    docs = to_docs(broken(lambda p: p["prd"].update(acceptance=[f"判据{i}" for i in range(9)])))

    assert len(docs.prd["acceptance"]) == 6
    assert "acceptance_truncated:9→6" in docs.extras


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("#abc", "#AABBCC"),
        ("abc", "#AABBCC"),
        ("#aabbccdd", "#AABBCC"),
        ("rgb(107, 66, 38)", "#6B4226"),
        ("rgba(107,66,38,0.9)", "#6B4226"),
        ("#6b4226", "#6B4226"),
        ("brown", "#A52A2A"),
    ],
)
def test_a_colour_written_any_other_way_becomes_hex(written, expected):
    docs = to_docs(broken(lambda p: p["tokens"].update(accent=written)))

    assert docs.tokens["accent"] == expected
    assert f"colour_repaired:accent={written}→{expected}" in docs.extras


def test_a_colour_under_another_name_is_read_from_that_name():
    """模型把 `ink` 写成 `text` —— 颜色还在，只是键名换了。只认无歧义的别名。"""
    payload = broken(lambda p: (p["tokens"].pop("ink"), p["tokens"].update(text="#2B1D14")))

    docs = to_docs(payload)

    assert docs.tokens["ink"] == "#2B1D14"
    assert "tokens.ink←text" in docs.extras


def test_an_extra_field_is_recorded_not_refused():
    """模型多吐一个字段**不该整份拒**（2026-10-28 改）。

    实测撞上的是 `architecture 多了字段：['理由']` —— 模型把 `why` 用中文又写了一遍。
    原来那一条会整份丢掉：44.7 秒白等、PRD 永远空、屏上一直说"文档节点正在写"。
    多出来的键不改变我们读的那几个键，拒掉它换不来任何正确性。
    """
    payload = broken(lambda p: p["architecture"].update(理由="因为要快"))
    payload["prd"]["pages"][0]["colour"] = "#fff"
    payload["notes"] = "模型的旁白"

    docs = to_docs(payload)

    assert docs.arch["why"], "该读的字段还在"
    assert set(docs.extras) == {"architecture.理由", "page.colour", "顶层.notes"}, docs.extras


def test_a_missing_required_field_is_still_refused():
    """宽容给"多"和"换个写法"，不给"缺" —— 缺 `architecture.why` 仍然是结构跑偏。"""
    with pytest.raises(DocumentUnavailable) as caught:
        to_docs(broken(lambda p: p["architecture"].pop("why")))
    assert caught.value.code == "docs_missing_field"


def test_no_json_at_all_is_named_as_such():
    with pytest.raises(DocumentUnavailable) as caught:
        run_documents("咖啡站", [], client=FakeChat("我先想想……"))
    assert caught.value.code == "docs_not_json"


def test_a_truncated_reply_is_not_mistaken_for_a_bad_json():
    """截断有专门的码：它要改的是 `max_tokens`，不是提示词。两者混在一起会查错方向。"""
    with pytest.raises(DocumentUnavailable) as caught:
        run_documents("咖啡站", [], client=FakeChat(reply_of(GOOD)[:80], finish="length"))
    assert caught.value.code == "docs_truncated"


def test_colours_are_normalised_to_upper_case():
    docs = run_documents("咖啡站", [], client=FakeChat(reply_of(GOOD)))
    assert docs.tokens["accent"] == "#6B4226"
    payload = json.loads(json.dumps(GOOD))
    payload["tokens"]["accent"] = "#6b4226"
    assert run_documents("咖啡站", [], client=FakeChat(reply_of(payload))).tokens["accent"] == "#6B4226"


def test_a_client_failure_is_renamed_into_the_docs_namespace():
    """客户端那侧的错在 `meeting` 模块里叫 `meeting_*`，这里必须改写成 `docs_*`。

    2026-10-28 实测踩到：文档节点被 120 秒读超时掐掉，留痕写的却是 `meeting_timeout` ——
    读日志的人第一反应是去查那场会，而会议明明是成功的。归因词挂在谁名下，就要说谁的事。
    """
    from acp.meeting import MeetingUnavailable

    class Boom:
        last_finish_reason = None

        def chat(self, system: str, user: str) -> str:
            raise MeetingUnavailable("meeting_timeout", "模型在 300 秒内没有返回")

    with pytest.raises(DocumentUnavailable) as caught:
        run_documents("咖啡站", [], client=Boom())
    assert caught.value.code == "docs_timeout"
    assert "300" in str(caught.value)
