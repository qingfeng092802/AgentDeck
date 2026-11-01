"""岗位提示词加载与注入的判据（2026-10-28）。

钉住四件事：
1. 十二份岗位稿都读得到，且**旧名能解析到新名**（会议层至今仍叫 `designer`）；
2. 取不到时抛的是 `PromptNotFoundError` 且 `reason` 分得开（空 / 越权 / 不存在）；
3. 注入后的 system **同时**带岗位正文与 `## 输出格式约束` —— 缺后半段就是"说了要做什么、没说交成什么形状"；
4. 卡上没有岗位列，`role_of_card()` 按**产物格式**判，判不出来返回 `None` 而不是猜。

第 3 条是本轮的核心：`test_injected_role_text_reaches_the_system_prompt` 用**临时目录替掉
`ROLE_DIR`**（真的走一次文件读取），证明"文件内容 → system prompt"这条路是通的，而不是
只证明模板里有个标题。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from acp import prompts
from acp.adapters.doubao.transport import DoubaoTransport
from acp.docs import service as docs_service
from acp.meeting import service as meeting_service
from acp.orchestrate.llm_split import OUTPUT_CONTRACT, LlmSplitter
from acp.prompts import (
    ROLE_IDS,
    PromptNotFoundError,
    compose_multi_role_prompt,
    compose_system_prompt,
    load_role_prompt,
    load_role_section,
    resolve_role_id,
    role_brief_for_card,
    role_of_card,
)
from acp.protocol import ArtifactFloor
from acp.protocol.messages import Task


class _Card:
    """够 `role_of_card()` / `role_brief_for_card()` 用的最小卡面。"""

    def __init__(self, fmt: str, criteria: tuple[str, ...] = ()) -> None:
        self.artifact_floor_format = fmt
        self.acceptance_criteria = list(criteria)


# ---------------------------------------------------------------- 加载器


def test_all_twelve_role_files_are_loadable() -> None:
    """十二份都要在。少一份的表现不是报错，是那一格静默没有岗位约束。"""
    for role_id in ROLE_IDS:
        body = load_role_prompt(role_id)
        assert body.strip(), f"{role_id}.md 是空的"
        assert body.startswith("# 岗位："), f"{role_id}.md 少了标题行"


def test_the_old_name_resolves_to_the_new_one() -> None:
    """会议层至今仍叫 `designer`，文件叫 `ui.md`。这条映射不写，第四轮就永远读不到岗位稿。"""
    assert resolve_role_id("designer") == "ui"
    assert resolve_role_id("fe") == "frontend"
    assert resolve_role_id("be") == "backend"
    assert resolve_role_id("sec") == "security"
    assert resolve_role_id("rescue") == "fullstack"
    # 已经是新名的不动，不认识的原样返回（由加载器判存在性）。
    assert resolve_role_id("frontend") == "frontend"
    assert resolve_role_id("architect") == "architect"
    assert load_role_prompt("designer") == load_role_prompt("ui")


@pytest.mark.parametrize(
    ("role_id", "reason"),
    [
        ("", "empty_role_id"),
        ("   ", "empty_role_id"),
        ("../../etc/passwd", "invalid_role_id"),
        ("roles/frontend", "invalid_role_id"),
        ("Frontend!", "invalid_role_id"),
        ("nope", "missing_file"),
    ],
)
def test_missing_role_raises_with_a_stable_reason(role_id: str, reason: str) -> None:
    """`reason` 是稳定键名：排障时"路径不对"与"文件没写"不能读成同一件事。"""
    with pytest.raises(PromptNotFoundError) as caught:
        load_role_prompt(role_id)
    assert caught.value.reason == reason
    assert caught.value.role_id == role_id, "`role_id` 存原样入参，排障时要看到人真正传了什么"


def test_section_extraction_stops_at_the_next_heading() -> None:
    """只取一节：整份塞进会议第四轮，产品经理会把它自己的 PRD 结构也当成本轮产出。"""
    section = load_role_section("pm", "一、立项会台词")
    assert section is not None
    assert section.startswith("### 一、立项会台词")
    assert "PRD 结构" not in section, "取节越界了：把下一节也带了进来"
    assert load_role_section("pm", "这一节不存在") is None


# ---------------------------------------------------------------- 注入


def test_injected_role_text_reaches_the_system_prompt(monkeypatch, tmp_path) -> None:
    """**Mock 读取 `frontend.md`**：临时目录里放一份假稿，验证它真的进了 system prompt。

    用临时目录而不是 monkeypatch `load_role_prompt`：前者连"路径怎么拼"一起验了，
    后者只验了"调用发生了"。这一层的 bug 恰恰更可能出在路径上。
    """
    fake = "# 岗位：前端开发专家（`frontend`）\n\n## 输出格式约束\n\n只输出 index.html 一个文件。\n"
    (tmp_path / "frontend.md").write_text(fake, encoding="utf-8")
    monkeypatch.setattr(prompts, "ROLE_DIR", tmp_path)

    composed = compose_system_prompt("基础 system", "frontend", output_contract="只交一个文件")

    assert "输出格式约束" in composed, "注入后的 system 必须带格式约束那一节"
    assert "只输出 index.html 一个文件" in composed, "岗位稿正文没有进 prompt"
    assert "你当前的岗位职责如下：" in composed
    assert composed.startswith("基础 system"), "基础 system 必须原样在最前，不能被顶掉"


def test_the_composed_prompt_carries_the_contract_and_the_role_body() -> None:
    """真实文件那条路：`## 输出格式约束` 与岗位正文同时在。"""
    composed = compose_system_prompt(
        "基础 system", "frontend", output_contract="产物文件名必须是 index.html", section="岗位守则"
    )
    assert "## 输出格式约束" in composed
    assert "产物文件名必须是 index.html" in composed
    assert "文件名是这条链上唯一不能漂的锚点" in composed, "取的是「岗位守则」那一节"


def test_multi_role_prompt_lists_every_role() -> None:
    """一次调用交三份文档 ⇒ 三个岗位并列，谁都不能缺。"""
    composed = compose_multi_role_prompt(
        "基础 system",
        [("pm", "二、PRD 结构"), ("cto", "二、架构三句话"), ("ui", "二、设计令牌")],
        output_contract="三份 JSON",
    )
    for label in ("### pm", "### cto", "### ui"):
        assert label in composed, f"少了一节：{label}"
    assert "## 输出格式约束" in composed


def test_the_meeting_turn_injects_only_its_own_section() -> None:
    """四轮会议各自只带自己那一节，且都带格式约束。"""
    for spec in meeting_service.ROLE_SPECS:
        system = meeting_service.system_of(spec)
        assert system.startswith(spec.system), "人设那句必须还在最前面"
        assert "## 输出格式约束" in system
        assert "不要 JSON" in system, "会议层契约：只说话，不输出 JSON"
    assert meeting_service.meeting_role_prompt_errors == {}


def test_the_documents_node_injects_three_roles() -> None:
    system = docs_service.system_of()
    assert system.startswith(docs_service.SYSTEM)
    for label in ("### pm", "### cto", "### ui"):
        assert label in system
    assert docs_service.docs_role_prompt_error is None


def test_the_splitter_carries_the_orchestrator_role_and_the_contract() -> None:
    splitter = LlmSplitter(client=object(), raw_input="做一个手冲咖啡站")  # type: ignore[arg-type]
    assert "## 输出格式约束" in splitter._system
    assert "guards" in splitter._system and "bus_gate" in splitter._system
    assert OUTPUT_CONTRACT.splitlines()[0] in splitter._system
    assert splitter.role_prompt_error is None


# ---------------------------------------------------------------- 卡 → 岗位


@pytest.mark.parametrize(
    ("fmt", "role_id"),
    [("css", "frontend"), ("html", "frontend"), ("py", "backend"), ("md", "writer"), ("json", "ui")],
)
def test_card_role_comes_from_the_artifact_format(fmt: str, role_id: str) -> None:
    """卡上没有岗位列（`executor_role_id` 实测全 NULL），产物格式是唯一能区分它们的事实。"""
    assert role_of_card(_Card(fmt)) == role_id


def test_unknown_format_gets_no_role_rather_than_a_guess() -> None:
    """判不出来就返回 None —— 猜错比不注入更糟：前端守则会发给一张未知格式的卡。"""
    assert role_of_card(_Card("png")) is None
    assert role_brief_for_card(_Card("png")) is None


def test_the_card_brief_names_the_file_from_the_criteria() -> None:
    """文件名从判据里取，不从标题猜（标题是中文自由文本）。"""
    brief = role_brief_for_card(_Card("html", ("产物文件名必须是 index.html", "含一条 @media 断点")))
    assert brief is not None
    assert "index.html" in brief
    assert "`html`" in brief
    assert "## 输出格式约束" in brief


def test_the_executor_prompt_renders_the_constraints() -> None:
    """`constraints` 在协议里存在，但三个适配器原先都不渲染它 ⇒ 填了也到不了模型。"""
    task = Task(
        task_id="T02",
        title="手冲咖啡 · 首页",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=["T01"],
        project_key="real_demo",
        assignee={"agent_id": "qoder", "role_id": "architect"},
        objective="手冲咖啡 · 首页",
        inputs=["手冲咖啡 · 全站样式表"],
        expected_output="产物文件名必须是 index.html",
        artifact_floor=ArtifactFloor(min_bytes=512, min_lines=12, format="html"),
        acceptance_criteria=["产物文件名必须是 index.html"],
        token_budget=Decimal(1000),
        constraints=["岗位：frontend\n- 只交产物本身"],
    )
    # `_prompt` 只读 `task`，不碰 self ⇒ 不需要造一个完整的 transport。
    content = DoubaoTransport._prompt(None, task)  # type: ignore[arg-type]
    assert "constraints:" in content
    assert "- 岗位：frontend" in content
    assert "  - 只交产物本身" in content, "多行约束要缩进续行，不能压成一句"
    assert "---" not in content
