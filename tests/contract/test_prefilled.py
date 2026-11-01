"""文档卡的判据：三份定稿文档 → 三张已完成卡。

钉四件事：
1. 三段齐全时**恰好**三张，卡号是 T90/T91/T92（前端与交付包都按卡号认，改了就是换协议）；
2. 缺任何一段就**整组不产出** —— 半真的"文档已定稿"比缺席更难排障；
3. 每张卡一个文件，格式分别是 md / md / json（`ArtifactFormat` 只认这五个值，没有 js）；
4. 内容取自 `ProjectDocs` 的同一套渲染，本模块不另写一份 —— 两份渲染就是两份口径。

这四条都是"能证伪"的：删一段、改一个卡号、把 json 写成 md，下面都会红。
"""

from __future__ import annotations

import json

import pytest

from acp.docs.service import ARCH_SECTION, PRD_SECTION, TOKENS_SECTION
from acp.orchestrate.nodes.prefilled import (
    ARCH_CARD_ID,
    DOC_WRITER_AGENT_ID,
    PRD_CARD_ID,
    TOKENS_CARD_ID,
    doc_cards,
)

PRD = {
    "title": "手冲咖啡站",
    "pages": [
        {"label": "首页", "file": "index.html", "duty": "主推豆单与门店信息"},
        {"label": "产品说明", "file": "products.html", "duty": "按产地与烘焙度分类介绍"},
        {"label": "联系", "file": "contact.html", "duty": "门店地址与营业时间"},
    ],
    "acceptance": ["首页首屏能看到豆单入口", "三个页面都能从导航到达"],
    "out_of_scope": ["在线支付"],
}

ARCH = {
    "stack": "静态 HTML + 一份全站 CSS",
    "why": "三页以内的展示站不需要构建链，直接开 index.html 就能看",
    "risk": "页面间导航靠相对路径，目录挪一层就会断链",
}

TOKENS = {
    "name": "米白 / 焦糖",
    "bg": "#faf7f2",
    "surface": "#ffffff",
    "ink": "#2b2118",
    "muted": "#7a6a58",
    "accent": "#a8622a",
    "accent2": "#5b7a5b",
    "line": "#e2d8c9",
}


def _digest(**override: object) -> dict[str, list[str]]:
    """按 `ProjectDocs.digest()` 的同一形状造摘要；传 `override` 可以整段拿掉或写坏。"""
    sections: dict[str, object] = {PRD_SECTION: PRD, ARCH_SECTION: ARCH, TOKENS_SECTION: TOKENS}
    sections.update(override)
    return {
        key: [json.dumps(value, ensure_ascii=False, sort_keys=True)]
        for key, value in sections.items()
        if value is not None
    }


def test_the_three_documents_become_exactly_three_finished_cards():
    """三段齐 → 恰好三张，卡号与格式逐字固定。

    卡号是协议的一部分：`assemble` 按卡号收产物、前端按卡号选卡，改一个字符就是换协议。
    """
    cards = doc_cards(_digest())
    assert [card.task_id for card in cards] == [PRD_CARD_ID, ARCH_CARD_ID, TOKENS_CARD_ID]
    assert [card.format for card in cards] == ["md", "md", "json"]
    assert [card.filename for card in cards] == ["prd.md", "architecture.md", "tokens.json"]
    # 写者必须是库里真有的 agent（`artifact.writer_agent_id` 有外键）。它不进 `PrefilledCard`，
    # 因为三张卡共用同一个写者 —— 把它做成每张卡的字段，就等于允许三张卡各写各的。
    assert DOC_WRITER_AGENT_ID in {"workbuddy", "doubao", "qoder", "deepseek-reviewer"}


def test_a_missing_section_drops_the_whole_group_instead_of_half_of_it():
    """缺任何一段 → 空元组。**不产出"有几段出几张"**。

    只落 PRD 卡会让板上出现"文档已定稿"的假象：看到 PRD 卡的人会以为架构也定了，
    于是去等一张永远不会出现的卡。半真的状态比缺席更难排障。
    """
    for missing in (PRD_SECTION, ARCH_SECTION, TOKENS_SECTION):
        assert doc_cards(_digest(**{missing: None})) == (), f"缺 {missing} 时不该落出任何卡"
    assert doc_cards({}) == (), "老单（2026-10-28 之前）没有这三段，不该落卡"


def test_a_broken_section_is_treated_as_missing_not_as_a_crash():
    """坏 JSON 与缺段同一格：立项节点没给出可用的结论，就不是"有文档"。"""
    broken = {PRD_SECTION: ["{ 这不是 JSON"], ARCH_SECTION: _digest()[ARCH_SECTION], TOKENS_SECTION: _digest()[TOKENS_SECTION]}
    assert doc_cards(broken) == ()


def test_the_card_body_is_the_same_rendering_the_docs_node_already_produces():
    """卡里的正文 = `ProjectDocs.files()` 的那一份，不另写一套渲染。

    两份渲染就是两份口径的开始：docs 节点写进 `results/docs/` 的与卡里挂的必须一致，
    否则"板上看到的那份"和"交付包里那份"会在某次改渲染时悄悄分叉。

    **一处已知差异要点名**：`tokens.json` 按**语义**比而不是逐字比。
    摘要里那三段是 `sort_keys=True` 序列化进去的，解析回来再渲染，键序与 `docs/` 目录那份不同；
    JSON 对象本来就无序，所以内容等价。md 两份是逐字比的 —— 它们没有这个问题，
    不该因为 json 那一处放宽就把 md 也放宽。
    """
    from acp.docs.service import ProjectDocs

    cards = doc_cards(_digest())
    expected = ProjectDocs(prd=PRD, arch=ARCH, tokens=TOKENS).files()
    for card in cards:
        if card.format == "json":
            assert json.loads(card.body) == json.loads(expected[card.filename])
        else:
            assert card.body == expected[card.filename], f"{card.filename} 的渲染与 docs 节点不一致"


@pytest.mark.parametrize("card_id", [PRD_CARD_ID, ARCH_CARD_ID, TOKENS_CARD_ID])
def test_every_card_has_a_positive_artifact_floor(card_id: str):
    """门槛三项都必须 > 0（协议层就是这么定的），否则门禁会退化成恒真判据。"""
    card = next(item for item in doc_cards(_digest()) if item.task_id == card_id)
    assert card.min_bytes > 0
    assert card.min_lines > 0
    assert len(card.body.encode("utf-8")) >= card.min_bytes, "自己渲染的东西也必须过自己的门槛"
    assert len(card.body.splitlines()) >= card.min_lines


# ---------------------------------------------------------------- 落盘不参与事务（U7）

def test_a_failed_card_write_does_not_leave_orphan_files(tmp_path, monkeypatch):
    """建卡失败时，这一跑新写出来的文件要清回去（U7，2026-10-28 真跑）。

    `session.rollback()` 只撤库里的行，盘上的文件留着 —— 实测现场是
    `results/realrun_1028/workbuddy-workspace/` 里三份文件都在、库里一张卡都没有。
    下一次重跑会撞主键或读到"文件在但没人认"的半程态，比一次干净的失败难查得多。

    **只删这一跑新写的那些**：原来就存在的文件属于上一次（可能是一次成功的）落盘，
    删掉它等于用一次失败去毁掉一次成功。
    """
    import uuid
    from types import SimpleNamespace

    from acp.orchestrate import graph as graph_module
    from acp.reporting.cards import CardNotWritten

    workspace_root = tmp_path / "demo"
    workspace_root.mkdir()
    monkeypatch.setattr(graph_module, "require_archive_root", lambda project_key, bus_root: workspace_root)

    request = SimpleNamespace(id=uuid.uuid4(), project_key="demo", summary_digest=_digest())

    class NoSnapshotSession:
        """任何查询都返回"没有存档点" ⇒ `add_done_card` 抛 `system_card_without_snapshot`。"""

        def scalar(self, *_args, **_kwargs):
            return None

        def scalars(self, *_args, **_kwargs):
            return SimpleNamespace(first=lambda: None)

        def add(self, _row):  # pragma: no cover - 走不到这里
            raise AssertionError("没有存档点就不该写任何行")

        def flush(self):  # pragma: no cover
            return None

    with pytest.raises(CardNotWritten):
        graph_module._persist_doc_cards(NoSnapshotSession(), request, SimpleNamespace(bus_root=None))

    written = sorted(path.name for path in workspace_root.rglob("*") if path.is_file())
    assert written == [], f"建卡失败却留下了孤儿文件：{written}"
