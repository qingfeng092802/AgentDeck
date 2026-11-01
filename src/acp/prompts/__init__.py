"""岗位提示词的唯一加载点：`src/acp/prompts/roles/<role_id>.md` → 注入用的一段文本。

## 为什么要有这一层，而不是在各处 `Path(...).read_text()`

`roles/` 下那十二份是**岗位说明**，它们的内容会随岗位调整而改；调用点有四处（会议四轮、
文档三份、拆单、执行卡下发）。若每处各拼一次路径与包装语，改一次格式就要改四处，
而"漏改一处"的表现是**某一路悄悄没带上岗位约束** —— 它不会报错，只会让那一格的产出
变得像通用助手写的。所以路径、包装语、格式约束标题，全部只在这里出现一次。

## 三套命名不要混（这一层负责把它们接起来）

- 提示词文件用**工作室岗位名**：`orchestrator / pm / cto / ui / frontend / backend / writer /
  qa / security / reviewer / acceptor / fullstack`
- 代码里还在用**旧名**：会议层叫 `designer`，前端引擎叫 `fe` / `be` / `sec` / `rescue`
- 库里 `role_spec` 只有**三个值**：`architect` / `reviewer` / `acceptor`（与十二岗不是一一对应）

`ROLE_ALIASES` 收前两者的映射；`role_spec` 那三个值**不映射** —— 它们不是岗位名，
硬映射会把"审查席"错认成"独立代码审查员"这个岗位（同名不同物，正是最容易出错的一处）。

## 卡上没有岗位 id

`task_card.executor_role_id` 存在，但实测**全为 NULL**（EN-04 的 `role_spec` 只登记了三个把关岗）。
所以"从卡上取 role_id"取不到东西。执行卡唯一能区分三个执行岗的事实是**产物格式**：
`css/html` → `frontend`、`py` → `backend`、`md` → `writer`。`role_of_card()` 就按它判，
判不出来返回 `None`（不猜 —— 猜错比不注入更糟）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: 十二个岗位的封闭集合。顺序与工作室名册一致（总管打头）。
ROLE_IDS: tuple[str, ...] = (
    "orchestrator",
    "pm",
    "cto",
    "ui",
    "frontend",
    "backend",
    "writer",
    "qa",
    "security",
    "reviewer",
    "acceptor",
    "fullstack",
)

#: 旧名 → 岗位名。只收"同一个岗位换了名字"的，不收"同名不同物"的。
ROLE_ALIASES: Mapping[str, str] = {
    "designer": "ui",
    "fe": "frontend",
    "be": "backend",
    "sec": "security",
    "rescue": "fullstack",
}

#: 产物格式 → 执行岗。卡上没有岗位列，这是唯一能区分它们的事实。
#:
#: `js` 归 `frontend`（owner 2026-10-29 裁决）：它和 `html` / `css` 是同一件东西的三层，
#: 归到后端会让"页面里加个购物车"变成跨岗协作，而实际写它的人就是写那张页面卡的人。
ROLE_BY_FORMAT: Mapping[str, str] = {
    "css": "frontend",
    "html": "frontend",
    "js": "frontend",
    "py": "backend",
    "md": "writer",
    "json": "ui",
}

#: 提示词目录。`Path(__file__).parent` 而不是 `REPO_ROOT`：本模块与 `roles/` 同处一个包内，
#: 用仓库根拼路径会让"把包整体挪走"变成一次静默失效（读不到文件 → 抛错，但错在启动期才好查）。
ROLE_DIR = Path(__file__).resolve().parent / "roles"

#: 允许的 `role_id` 形态。**先验形态再拼路径**：`../../etc/passwd` 这类入参不是"找不到"，
#: 是"不该去找" —— 两者都抛 `PromptNotFoundError`，但 `reason` 不同，排障时能分开。
_SAFE_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: 注入段的固定开场语。改它等于改所有调用点看到的包装，故写成常量而不是各调用点各写一句。
INJECT_HEADER = "你当前的岗位职责如下："

#: 格式约束那一节的标题。**每个调用点都必须给一段 `output_contract`** —— 不给就等于
#: "岗位说了要做什么，但没说交成什么形状"，而这正是 2026-10-28 那次两张卡 `ARTIFACT_INVALID` 的形状。
CONTRACT_HEADER = "## 输出格式约束"


class PromptNotFoundError(LookupError):
    """取不到岗位提示词。`reason` 是稳定键名，`role_id` 是原样入参（不是解析后的）。"""

    def __init__(self, role_id: object, *, reason: str, path: Path | None = None, detail: str = "") -> None:
        self.role_id = str(role_id)
        self.reason = reason
        self.path = path
        where = f"（{path}）" if path is not None else ""
        extra = f"：{detail}" if detail else ""
        super().__init__(f"岗位提示词取不到：{self.role_id!r}{where} [{reason}]{extra}")


def resolve_role_id(role_id: object) -> str:
    """旧名 → 岗位名。不认识的原样返回（由 `load_role_prompt` 判存在性）。

    空值 / 非字符串一律返回空串 —— 让下游的"形态校验"给出唯一那个原因码，
    而不是在这里再分一档"None 算不算空"。
    """
    text = str(role_id or "").strip().lower()
    return ROLE_ALIASES.get(text, text)


def _role_path(role_id: object) -> tuple[str, Path]:
    canonical = resolve_role_id(role_id)
    if not canonical:
        raise PromptNotFoundError(role_id, reason="empty_role_id", detail="岗位名是空的")
    if not _SAFE_ROLE.match(canonical):
        raise PromptNotFoundError(
            role_id,
            reason="invalid_role_id",
            detail="只接受小写字母开头的 [a-z0-9_-]，不接受路径分隔符",
        )
    return canonical, ROLE_DIR / f"{canonical}.md"


def load_role_prompt(role_id: str) -> str:
    """读一份岗位提示词。**没有缓存**：文件小（1–4 KiB），而缓存会让"改了 md 不生效"变成一次排障。

    三条失败都是 `PromptNotFoundError`，`reason` 分别是
    `empty_role_id` / `invalid_role_id` / `missing_file`（文件不存在）或 `empty_file`（存在但空）。
    """
    canonical, path = _role_path(role_id)
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptNotFoundError(canonical, reason="missing_file", path=path) from None
    except OSError as exc:  # 权限 / 编码 / 被占用：不吞，但要带上路径
        raise PromptNotFoundError(
            canonical, reason="unreadable", path=path, detail=type(exc).__name__
        ) from None
    if not body.strip():
        raise PromptNotFoundError(canonical, reason="empty_file", path=path)
    return body


def load_role_section(role_id: str, heading: str) -> str | None:
    """从岗位稿里取一节（`heading` 按前缀匹配，`##` 与 `###` 都认）。

    为什么要能取节：会议第 N 轮只需要「立项会第 N 轮台词」那一节。整份塞进去，产品经理
    会把它自己的 `PRD 结构` 也当成这一轮的产出 —— 于是会议里冒出一个 JSON，
    而会议层的契约是"只说话，不输出 JSON"。
    """
    body = load_role_prompt(role_id)
    lines = body.splitlines()
    # `heading` 允许带 `##` / `###` 前缀：调用点写"### 一、立项会台词"或"一、立项会台词"都行。
    # 两种写法都收，是因为调用点关心的是"哪一节"，不是"几级标题"；让它记住标题级数，
    # 等于把 md 的排版细节泄漏成调用方的负担。
    wanted = str(heading or "").strip().lstrip("#").strip()
    if not wanted:
        return None
    start = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("##") and stripped.lstrip("#").strip().startswith(wanted):
            start = index
            break
    if start < 0:
        return None
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    collected = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped) - len(stripped.lstrip("#")) <= level:
            break
        collected.append(line)
    text = "\n".join(collected).strip()
    return text or None


def _assemble(base: str, bodies: list[tuple[str, str]], output_contract: str) -> str:
    """三段的固定排版：基础 system → 岗位职责 → 输出格式约束。

    排版只在这里出现一次：`## 输出格式约束` 这个标题被调用点与判据同时引用，
    各调用点各写一遍就等于有两份标题，改一份漏一份时判据会绿着而模型看不见约束。
    """
    parts = [str(base).strip(), "", INJECT_HEADER, ""]
    for label, body in bodies:
        if label:
            parts.extend([f"### {label}", ""])
        parts.extend([body.strip(), ""])
    parts.extend([CONTRACT_HEADER, "", str(output_contract).strip(), ""])
    return "\n".join(parts)


def compose_system_prompt(
    base: str,
    role_id: str,
    *,
    output_contract: str,
    section: str | None = None,
) -> str:
    """基础 system + 岗位约束 + 格式约束。**`output_contract` 必填**，见 `CONTRACT_HEADER` 的理由。

    `section` 给了就只取那一节（取不到则退回整份）：宁可多带一点上下文，
    也不要因为标题改了一个字就静默退化成"没有岗位约束"。
    """
    body = load_role_prompt(role_id)
    if section:
        body = load_role_section(role_id, section) or body
    return _assemble(str(base), [("", body)], output_contract)


def compose_multi_role_prompt(
    base: str,
    roles: Sequence[tuple[str, str | None]],
    *,
    output_contract: str,
) -> str:
    """一次调用里有**多个岗位**时用这个（立项文档那一次调用同时交 PRD / 架构 / 令牌）。

    为什么不拆成三次调用：那三份文档共用同一份结论，拆开就是三次独立发挥，很容易出现
    "PRD 说三页、架构说四个文件"。这里保持一次调用，但把三个岗位的约束**分节并列**，
    让模型知道"这三段是同一次产出里的三件事"，而不是三个各说各话的人。
    """
    bodies: list[tuple[str, str]] = []
    for role_id, section in roles:
        body = load_role_prompt(role_id)
        if section:
            body = load_role_section(role_id, section) or body
        bodies.append((resolve_role_id(role_id), body))
    return _assemble(str(base), bodies, output_contract)


def role_of_card(card: Any) -> str | None:
    """从 EN-02 的现值判这一格由哪个执行岗负责。判不出来返回 `None`。

    只读 `artifact_floor_format` 这一列：卡上没有岗位列（`executor_role_id` 实测全 NULL），
    而产物格式是三个执行岗之间唯一稳定的区别。**不按标题猜** —— 标题是中文自由文本。
    """
    fmt = getattr(card, "artifact_floor_format", None)
    return ROLE_BY_FORMAT.get(str(fmt or "").strip().lower())


def role_brief_for_card(card: Any) -> str | None:
    """一张执行卡的岗位约束（拼好的一整段），给 `Task.constraints` 用。

    取不到岗位（格式不在映射里）就返回 `None`，让调用方保持 `constraints` 为空 ——
    这是"不猜"的落点：宁可这一张卡没有岗位约束，也不要把前端工程师的约束发给一张未知格式的卡。
    """
    role_id = role_of_card(card)
    if role_id is None:
        return None
    floor = getattr(card, "artifact_floor_format", "")
    criteria = [str(item) for item in (getattr(card, "acceptance_criteria", None) or ())]
    name_hint = ""
    for item in criteria:
        match = re.search(r"产物文件名必须是\s*(\S+)", item)
        if match:
            name_hint = match.group(1)
            break
    contract_lines = [
        f"- 产物格式：`{floor}`；文件名{'必须是 `' + name_hint + '`' if name_hint else '见判据'}。",
        "- 只交产物本身，不交解释；产物落在 `*-workspace/` 下。",
    ]
    return compose_system_prompt(
        base=f"这一格由「{role_id}」负责。",
        role_id=role_id,
        output_contract="\n".join(contract_lines),
        section="岗位守则",
    )
