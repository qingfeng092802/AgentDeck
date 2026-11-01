"""把「立项阶段已经定稿的三份文档」落成三张**已完成卡**（板上看得见的那三格）。

## 为什么这三张卡不该交给执行器

执行器只会写文件，不会做判断。PRD / 架构 / 令牌是立项阶段**已经定稿的结论**，
再让模型"执行"一遍等于把同一件事做两遍，而且第二遍会漂：2026-10-28 那次拆单回落后，
模型把卡标题写成了通用词「首页 / 产品说明页 / 联系页」，执行器据此做出通用页，两张卡直接 `ARTIFACT_INVALID`。

所以这里只做**登记**，不做生成：
- 内容来自 `summary_digest` 的三段（文档节点的产物，`docs/service.py::ProjectDocs`）
- 落盘、建卡、建产物行，状态直接 `DELIVERED`
- **不调模型**，因此可复算：同一份 digest 永远落出同一组卡

## 卡号为什么是 T90–T92

页面卡与外壳卡由 `skeleton_from_prd()` 按 PRD 的页数展开，占 `T01..T0N`。文档卡是**输入**
而不是产出：它们不依赖任何卡，也没有卡依赖它们，在 DAG 里是孤立节点。
排到 90 段是为了"页数从 1 涨到 5 时卡号不会撞"，而不是为了在板上排在最后
（前端按 `depends_on` 算层级，孤立节点自然落在第 0 层）。

## 三个文件名的来历

`ProjectDocs.files()` 已经定义了这三份的渲染形状（`prd.md` / `architecture.md` / `tokens.json`），
本模块**不另写一套渲染** —— 两份渲染就是两份口径的开始。这里只做"取出来、写到工作区、登记"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from acp.docs.service import ARCH_SECTION, PRD_SECTION, TOKENS_SECTION, ProjectDocs

#: 文档卡的写者。库里 `agent_unit` 现存的四个 id 里，只有 `workbuddy` 是"平台这一侧"的身份
#: （另三个分别是执行岗 doubao / qoder 与审查岗 deepseek-reviewer）。文档由立项节点产出，
#: 记在平台名下才是事实；记在某个执行岗名下会让"这文件谁写的"在留痕里指向一个没参与的人。
DOC_WRITER_AGENT_ID = "workbuddy"

#: 三张卡号。写死在模块级是为了让判据能逐字钉住它们，而不是在函数里拼字符串。
PRD_CARD_ID = "T90"
ARCH_CARD_ID = "T91"
TOKENS_CARD_ID = "T92"

#: 产物门槛（字节 / 行）。`ArtifactFloor` 三个字段都必须 > 0（协议层就是这么定的），
#: 所以即便文档是我们自己写的，也得给一组真实下限。
#:
#: **这组数字只用来挡"渲染坏掉产出空文件"，不用来判"文档够不够好"** —— 后者由
#: `to_docs()` 的结构校验保证（pages 1–5、acceptance 1–6、七个颜色都是 6 位 hex）。
#: 注意 acceptance 的下界是 1 不是 2：`to_docs()` 对"少写一条判据"是**收下并留痕**，
#: 只有一条都没有才拒（口径 5）。这里若还按 2 条写，就会在合法文档上误判。
#: 2026-10-28 第一版把 md 设成 200B/6 行，结果**只有一个页面的 PRD 渲染出来是 127B**，
#: 直接让 `_persist_doc_cards` 抛异常、整个拆单挂掉 —— 门槛按"典型内容"拍数字，
#: 就会在内容合法但简短时误伤。这里取"能容纳最小合法文档、且挡住空文件"的那一档。
#:
#: 一行都没有（0B）才该报错；三五行是这三份文档的正常形状，不是缺陷。
_MIN_BYTES = 64
_MIN_LINES = 3


@dataclass(frozen=True)
class PrefilledCard:
    """一张"已经完成"的卡：卡号、标题、一个文件、以及这份文件为什么算完成。

    **一卡一文件**不是偷懒：`artifact` 表一行对应一份产物，而 `collect()` 的归属判定
    （FR-013）认的是 `<agent>-workspace/**` 下的具体路径。把三份塞进一张卡会让
    "这张卡的产物是哪一份"在门禁里失去唯一答案。
    """

    task_id: str
    title: str
    filename: str
    body: str
    format: str
    min_bytes: int
    min_lines: int
    acceptance: tuple[str, ...]


def _section(digest: Mapping[str, Sequence[str]], key: str) -> dict[str, object] | None:
    """从摘要里取回一段 JSON 对象。取不到 / 坏了都返回 None —— 老单没有这一段不是错误。"""
    values = digest.get(key) or ()
    if not values:
        return None
    try:
        parsed = json.loads(values[0])
    except (json.JSONDecodeError, TypeError, IndexError):
        return None
    return parsed if isinstance(parsed, dict) else None


def doc_cards(digest: Mapping[str, Sequence[str]]) -> tuple[PrefilledCard, ...]:
    """摘要 → 三张已完成卡。三段缺任何一段就整组不产出。

    **为什么是"整组"而不是"有几段出几张"**：这三份是立项节点一次性产出的同一批结论
    （`ProjectDocs` 是 frozen dataclass，构造时三份都在）。只落出一两份会让板上的
    "文档已定稿"这个判断变成半真 —— 而半真的状态比缺席更难排障：看到 PRD 卡的人会以为
    架构也定了，于是去等一张永远不会出现的卡。

    返回空元组是合法结果：老单（2026-10-28 之前）的摘要里只有「会议」段，没有这三段。
    """
    prd = _section(digest, PRD_SECTION)
    arch = _section(digest, ARCH_SECTION)
    tokens = _section(digest, TOKENS_SECTION)
    if not (prd and arch and tokens):
        return ()

    # 渲染交给 `ProjectDocs`：它是这三份文件形状的唯一产地，本模块只消费。
    files = ProjectDocs(prd=prd, arch=arch, tokens=tokens).files()
    title = str(prd.get("title") or "").strip() or "本站"

    return (
        PrefilledCard(
            task_id=PRD_CARD_ID,
            title=f"{title} · PRD",
            filename="prd.md",
            body=files["prd.md"],
            format="md",
            min_bytes=_MIN_BYTES,
            min_lines=_MIN_LINES,
            acceptance=(
                "列出每个页面的文件名与职责",
                "验收标准可拿文件内容或浏览器行为对照",
            ),
        ),
        PrefilledCard(
            task_id=ARCH_CARD_ID,
            title=f"{title} · 架构",
            filename="architecture.md",
            body=files["architecture.md"],
            format="md",
            min_bytes=_MIN_BYTES,
            min_lines=_MIN_LINES,
            acceptance=(
                "写明选型、理由与一处真实风险",
                "选型与 PRD 的页面形态一致（静态站不写服务端依赖）",
            ),
        ),
        PrefilledCard(
            task_id=TOKENS_CARD_ID,
            title=f"{title} · 令牌",
            filename="tokens.json",
            body=files["tokens.json"],
            format="json",
            min_bytes=_MIN_BYTES,
            min_lines=_MIN_LINES,
            acceptance=(
                "七个颜色都是 6 位 hex",
                "前后景对比可读（正文与背景不是同一档明度）",
            ),
        ),
    )
