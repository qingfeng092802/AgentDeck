"""验收卡 / 打包卡的判据：报告说什么、不说什么、卡号落在哪。

`delivery_markdown()` 是纯函数，可以直接钉。
`acceptance_markdown()` 要读 EN-02 / EN-07 / EN-08 三张表，用 `.wbtmp/verify_gate.py`
在真实库上验（跑完 rollback），不塞进这组 —— 这一组要保持零外部依赖，任何人在任何机器上都能跑。
"""

from __future__ import annotations

from types import SimpleNamespace

from acp.reporting.gate import ACCEPTANCE_CARD_ID, DELIVERY_CARD_ID, delivery_markdown


def _report(**over: object) -> SimpleNamespace:
    base = {
        "entry_path": "web/index.html",
        "manifest_count": 9,
        "cards_delivered": 7,
        "package_path": "results/delivery_package.json",
        "package_bytes": 2048,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_delivery_summary_only_repeats_what_was_measured():
    """摘要里的每个数都来自 `DeliveryReport`，一个都不现算。"""
    body = delivery_markdown(_report())
    assert "web/index.html" in body
    assert "9 份" in body
    assert "7 张" in body
    assert "results/delivery_package.json" in body
    assert "2048 字节" in body


def test_delivery_summary_says_the_package_is_not_copied_in():
    """必须写明"本卡只记读数，不复制包内容" —— 否则读的人会以为这份 md 就是交付包。"""
    body = delivery_markdown(_report())
    assert "单点代笔" in body
    assert "不复制包内容" in body


def test_a_missing_field_degrades_to_a_dash_instead_of_a_crash():
    """报告对象换了形状不该让整次交付挂掉：缺字段显示 `—`，不是 `None` 也不是异常。"""
    body = delivery_markdown(SimpleNamespace(entry_path="index.html"))
    assert "—" in body
    assert "None" not in body


def test_the_two_cards_sit_after_the_three_checks_and_do_not_collide():
    """卡号：T90–T92 文档、T93–T95 检查、T96 验收、T97 打包。四段互不相交。"""
    assert ACCEPTANCE_CARD_ID == "T96"
    assert DELIVERY_CARD_ID == "T97"
    docs = {"T90", "T91", "T92"}
    checks = {"T93", "T94", "T95"}
    assert not ({ACCEPTANCE_CARD_ID, DELIVERY_CARD_ID} & docs)
    assert not ({ACCEPTANCE_CARD_ID, DELIVERY_CARD_ID} & checks)
