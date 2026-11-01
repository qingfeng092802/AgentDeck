"""立项文档节点（2026-10-28 owner 选 B）：会后产出 PRD / 架构 / 设计令牌三份结构化文档。

为什么单独一层：会议只产出"人话"（四段 ≤140 字台词），拆单只产出"卡"，
中间那份**能被人读、也能被机器读的需求文档**此前不存在 —— 于是拆单靠猜、验收自己判。

这一层只做"落文档"，不碰卡、不碰规则。入口在 `acp.serve_intake.WebIntakeRunner`：
开会 → **落文档** → 拆单；文档失败不挡拆单（`DocumentUnavailable` 由调用方捕获并留痕）。
"""

from acp.docs.service import (
    ARCH_SECTION,
    PRD_SECTION,
    SECTIONS,
    TOKENS_SECTION,
    DocumentUnavailable,
    ProjectDocs,
    run_documents,
    to_docs,
)

__all__ = [
    "ARCH_SECTION",
    "PRD_SECTION",
    "SECTIONS",
    "TOKENS_SECTION",
    "DocumentUnavailable",
    "ProjectDocs",
    "run_documents",
    "to_docs",
]
