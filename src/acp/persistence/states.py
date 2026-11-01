"""任务卡状态机的落库表达（T026，FR-019 / FR-040｜R-02）。

**顺序说明**：本文件在迁移之前写，因为 EN-02 `task_card.status` 的 PG 枚举类型由它生成——
状态集是 `data-model.md §2` 定义的，代码只是它的落库形态，不是第二定义处。

对齐方式不是"抄 15 个字符串然后祝它们一直一致"：`states_declared_in_data_model()` 直接读
`data-model.md` 的状态全集行与迁移图区间，按 §2 给的**同两条复算命令**的语义解析，
用例 `tests/contract/test_states.py` 断言 Python 枚举与文档集合**双向差集为空**。
文档改一个态名而代码没跟 ⇒ 用例立刻红；反之亦然。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

# 与 `data-model.md §2` 的状态全集逐一对应（15 个，C8 修正后把 `CHANGES_REQUESTED` 计入）。
class TaskCardState(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    READY = "READY"
    RUNNING = "RUNNING"
    NEED_INFO = "NEED_INFO"
    REJECTED = "REJECTED"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REWORK = "REWORK"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    HANDED_OFF = "HANDED_OFF"
    ESCALATED = "ESCALATED"
    ACCEPTING = "ACCEPTING"
    DELIVERED = "DELIVERED"
    DELIVERED_WITH_ISSUES = "DELIVERED_WITH_ISSUES"


TERMINAL_STATES = frozenset({TaskCardState.DELIVERED, TaskCardState.DELIVERED_WITH_ISSUES})

# §2 判据②的白名单：迁移图区间里合法出现的非态名大写词只有这两个
# （`MUST` 是规则句的连接词，`APPROVE` 是结论枚举值不是态名）。
# 这份排除表写在 §2 的说明里，改动必须两边同批——用例 `test_exclusion_list_matches_doc` 钉它。
NON_STATE_TOKENS = frozenset({"MUST", "APPROVE"})

DIAGRAM_ANCHOR = "<!-- state-diagram-start -->"
UPPER_RE = re.compile(r"[A-Z][A-Z_]{3,}")


def data_model_default() -> Path:
    """从仓库根定位 `data-model.md`（本模块在 `src/acp/persistence/` 下，往上三层是根）。"""
    return Path(__file__).resolve().parents[3] / "specs" / "001-multi-agent-collab-platform" / "data-model.md"


def states_declared_in_data_model(path: Path | None = None) -> set[str]:
    """解析 §2 的「状态全集」行（文档里的唯一权威枚举）。"""
    text = (path or data_model_default()).read_text(encoding="utf-8")
    for line in text.split("\n"):
        if line.startswith("**状态全集"):
            return set(UPPER_RE.findall(line))
    raise ValueError("data-model.md 里找不到以「**状态全集」开头的枚举行")


def state_count_claimed(path: Path | None = None) -> int:
    """解析 §2 自述的态数（文档原文是 `本表 **15 态**`，粗体覆盖"15 态"两字），
    用于判据①的两侧对照。找不到就抛，不返回 0 —— 返回 0 会让"解析失灵"读起来像通过。
    """
    text = (path or data_model_default()).read_text(encoding="utf-8")
    hit = re.search(r"本表 \*\*(\d+) 态\*\*", text)
    if hit is None:
        raise ValueError("data-model.md 里找不到「本表 **N 态**」的自述行")
    return int(hit.group(1))


def tokens_in_transition_region(path: Path | None = None) -> set[str]:
    """只扫迁移图与迁移规则区间（从 HTML 注释锚点起，到 `## 3.` 止）。

    区间必须按**整行锚点**取，不能只匹配锚点名——点名在说明文字里也出现过，
    从说明行开扫会把说明里的技术名词一并扫进来（§2 记过一次假阴）。
    """
    lines = (path or data_model_default()).read_text(encoding="utf-8").split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == DIAGRAM_ANCHOR)
    except StopIteration as exc:
        raise ValueError("data-model.md 里找不到 state-diagram-start 锚点行") from exc
    region: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## 3."):
            break
        region.append(line)
    tokens: set[str] = set()
    for line in region:
        tokens.update(UPPER_RE.findall(line))
    return tokens
