"""检查器落卡的判据：路径归一化 + PRD 页面清单。

钉两件事，两件都是**第一次接线就会踩**的：

1. `relative_name()` 必须把 `qoder-workspace/index.html` 剥成 `index.html`。
   不剥的话，`scan_e2e` 拿 `href="products.html"` 去比 `files` 里那些带前缀的 key，
   **每一页的每个站内链接都会被判成断链** —— 报告全红，而产物其实没问题。
2. `_prd_pages()` 只从 PRD 段取 `file`，取不到就空 —— 空清单意味着"不要求产出任何页面"，
   而不是"要求产出 0 页"。这两件事在 `scan_e2e` 的 `expect_links` 里是同一个形状，
   所以取不到时必须返回空列表（少判一条），不能返回一个假的页面名（多判一条）。
"""

from __future__ import annotations

import json

import pytest

from acp.checks.service import scan_e2e
from acp.docs.service import PRD_SECTION
from acp.reporting.checks import E2E_CARD_ID, GATE_CARD_ID, SECURITY_CARD_ID, _prd_pages, relative_name


def test_a_workspace_path_is_stripped_to_its_relative_name():
    assert relative_name("qoder-workspace/index.html") == "index.html"
    assert relative_name("qoder-workspace/assets/site.css") == "assets/site.css"
    # 不是 workspace 形态的原样返回：`results/` 这类公共区路径不该被误剥。
    assert relative_name("results/delivery_package.json") == "results/delivery_package.json"
    assert relative_name("index.html") == "index.html"


def test_stripping_is_what_keeps_same_site_links_from_all_being_reported_broken():
    """把归一化去掉，同一份产物就会从"零断链"变成"全是断链" —— 这条就是那个对照。"""
    files = {
        "index.html": '<html><head><title>首页</title><meta name="viewport" content="width=device-width"></head>'
        '<body><h1>首页</h1><a href="products.html">产品</a></body></html>',
        "products.html": '<html><head><title>产品</title><meta name="viewport" content="width=device-width"></head>'
        "<body><h1>产品</h1><a href=\"index.html\">回首页</a></body></html>",
        "site.css": ":root{--bg:#fff}@media (max-width:600px){body{font-size:14px}}",
    }
    good = scan_e2e(files)
    assert not [item for item in good.findings if item.code == "e2e_broken_link"]

    prefixed = {f"qoder-workspace/{name}": body for name, body in files.items()}
    bad = scan_e2e(prefixed)
    assert [item for item in bad.findings if item.code == "e2e_broken_link"], (
        "带前缀的 key 必须被判成断链，否则说明 scan_e2e 的判据变了，归一化这一层的理由要重写"
    )


def test_prd_pages_only_reads_the_file_column():
    prd = {
        "title": "站",
        "pages": [
            {"label": "首页", "file": "index.html", "duty": "主推"},
            {"label": "产品", "file": "products.html", "duty": "分类"},
        ],
        "acceptance": ["a", "b"],
        "out_of_scope": [],
    }
    digest = {PRD_SECTION: [json.dumps(prd, ensure_ascii=False)]}
    assert _prd_pages(digest) == ["index.html", "products.html"]


@pytest.mark.parametrize(
    "digest",
    [
        {},
        {PRD_SECTION: []},
        {PRD_SECTION: ["不是 JSON"]},
        {PRD_SECTION: ["[]"]},
        {PRD_SECTION: [json.dumps({"title": "站"})]},
    ],
)
def test_no_prd_means_no_expected_pages_instead_of_a_crash(digest):
    """老单 / 坏 JSON / 没有 pages 一律空清单：少判一条，不编一条。"""
    assert _prd_pages(digest) == []


def test_the_three_check_cards_sit_in_the_system_range():
    """卡号落在 T90+ 的系统段，且互不相同 —— 与文档卡 T90–T92 不撞。"""
    ids = {E2E_CARD_ID, SECURITY_CARD_ID, GATE_CARD_ID}
    assert len(ids) == 3
    for card_id in ids:
        assert card_id.startswith("T9"), f"{card_id} 不在系统段"
        assert card_id not in {"T90", "T91", "T92"}, f"{card_id} 与文档卡撞号"


# ---------------------------------------------------------------- 门禁汇总必须读上游（U5）

def test_the_gate_summary_reads_the_two_upstream_reports():
    """门禁汇总要把 E2E / 安全的结论算进去（U5，2026-10-28 真跑）。

    实测撞到的自相矛盾：同一轮里 `e2e-report` 判**不通过**，`gate-report` 判
    **通过，没有发现任何问题** —— 检查器写了一份报告，却没有任何东西读它做决定，
    那两张卡就只是"板上多了两行字"。汇总的职责本来就是汇总。
    """
    from acp.checks.service import Finding, Report, scan_gate

    cards = [
        {
            "id": "T01",
            "title": "卡 T01",
            "format": "html",
            "bytes": 900,
            "lines": 20,
            "min_bytes": 512,
            "min_lines": 12,
            "criteria": ["首屏有公司名"],
        }
    ]
    e2e = Report("e2e-report.md", (Finding("fail", "e2e_no_css", "交付目录里没有 css 文件"),))
    security = Report("security-report.md", (Finding("warn", "sec_clean", "没命中任何规则"),))

    alone = scan_gate(cards)
    together = scan_gate(cards, upstream=(e2e, security))

    assert alone.passed, "只看产物下限时这一单是通过的 —— 这正是原来那句自相矛盾的来源"
    assert not together.passed
    assert [item.code for item in together.failed] == ["gate_e2e_no_css"]
    assert "e2e-report.md" in together.failed[0].text, "没说是哪一份报告判的"
    # 上游只有提示时不当成失败，但也不吞掉：吞掉的话"读过了"和"没读"看不出区别。
    assert any(item.code == "gate_upstream_warn" for item in together.findings), together.findings
