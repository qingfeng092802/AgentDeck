"""入口名的推导（U2，2026-10-28 真跑）。

入口名原来只有两个来源：请求体里的 `entry_name` 和启动参数 `--review-entry`（默认 `index.html`）。
而 PRD 说页面是 `dashboard.html` —— 两者各说各话，组包 `entry_absent`，交付包永远出不来。
这里钉的是第三档：**PRD 第一页的 `file`**。读得到就用它，读不到返回 None 让调用方退回命令行值。

只认第一页，不做"哪个像首页"的启发式：R-07 的口径是"入口由调用方点名，装配点不猜"，
按 PRD 的页序取第一页是点名，挑一个"看起来最像首页的"才是猜。
"""

from __future__ import annotations

import json

import pytest

from acp.docs.service import PRD_SECTION
from acp.review.service import entry_name_from_digest


def _digest(payload: object) -> dict[str, list[str]]:
    return {PRD_SECTION: [json.dumps(payload, ensure_ascii=False)]}


def test_the_first_page_of_the_prd_is_the_entry():
    digest = _digest(
        {
            "title": "看板",
            "pages": [
                {"label": "总览", "file": "dashboard.html", "duty": "一屏看完指标"},
                {"label": "明细", "file": "detail.html", "duty": "逐条下钻"},
            ],
        }
    )
    assert entry_name_from_digest(digest) == "dashboard.html"


@pytest.mark.parametrize(
    "digest",
    [
        None,
        {},
        {PRD_SECTION: []},
        {PRD_SECTION: ["不是 JSON"]},
        {PRD_SECTION: ["[]"]},
        _digest({"title": "看板", "pages": []}),
        _digest({"title": "看板", "pages": [{"label": "总览"}]}),
        _digest({"title": "看板", "pages": [{"label": "总览", "file": "   "}]}),
    ],
)
def test_anything_unreadable_is_none_not_a_guess(digest):
    """读不出来就是 None —— 由调用方退回命令行值 / 内置默认，不在这里编一个名字。"""
    assert entry_name_from_digest(digest) is None
