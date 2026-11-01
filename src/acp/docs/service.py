"""立项文档节点：把"会上说的"落成三份**结构化**文档 —— PRD / 架构 / 设计令牌。

为什么要有这一格：会议那一步的产物是一串 **≤140 字**的台词，产品那一轮把"几个页面、
每页职责、验收判据"全压进一句话里（提示词写的就是「不超过 120 字」）。于是：

- 拆单器只能读那句摘要，靠它猜"这站有几个页面、每页干什么"；
- 验收时逐条勾的是**卡自己写的** `acceptance_criteria`，而卡是同一个模型拆的 ——
  自己出题自己判；
- 交付包里没有任何一处写着"客户当时要什么、验收怎么算"。

这一格把三份文档做成 **JSON 结构**而不是三段散文：结构是事实，文本只是渲染。
拆单读结构、界面三格读结构 —— 两处不再是两份口径。

五条口径：

1. **模型是注入的**。判据喂假客户端，一次网络都不发（与 `meeting` 同一条纪律）。
2. **失败不挡拆单**。`DocumentUnavailable` 由调用方捕获，只留痕；拆单退回"只读台词"。
3. **尺寸有上限**。文档要能在一屏里读完：页 1–5、判据 2–6 条、每条 ≤80 字。
   超了**截断并留痕**，不整份拒 —— 多一页不代表结构跑偏。
4. **颜色必须是 hex**。令牌那一段下游要直接进 CSS 变量，写"深咖色"等于没给。
   但"没给成 hex"和"没给"是两件事：3 位、8 位带 alpha、`rgb()`、漏了 `#`、CSS 颜色名
   —— 这些都是**同一种意思的另一种写法**，归一就完了。
5. **值层面的毛病修，结构层面的毛病拒**（2026-10-28 第二次放宽，理由见 `ProjectDocs.extras`）。
   判据只有一条：**修完之后还能不能还原模型的意思**。能 —— 文件名大小写、颜色写法、
   页数超了、判据多了一条，全都修完留痕。不能 —— 三份里缺一份、pages 一条没有、
   页面既没 label 也没 duty，那才是"结构跑偏"，整份拒。
   为什么这条值得单独立一条口径：**一次拒单 = 一次 16–45 秒的模型调用白等 + PRD 永远空**，
   而宽容的代价只是 `extras` 里多一行可查的账。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from acp.prompts import PromptNotFoundError, compose_multi_role_prompt

#: `summary_digest` 里的三个段名。前端按它们取，后端按它们写。
PRD_SECTION = "PRD"
ARCH_SECTION = "架构"
TOKENS_SECTION = "令牌"
SECTIONS = (PRD_SECTION, ARCH_SECTION, TOKENS_SECTION)

#: 页数上限：超过五个页面的"静态站"通常是模型把导航项当成了独立页面。
MAX_PAGES = 5
#: 判据条数：1 条太松，7 条以上没人逐条验。
MIN_ACCEPTANCE = 2
MAX_ACCEPTANCE = 6
MAX_OUT_OF_SCOPE = 4
#: 单条文字上限（字符）。超了截断而不是拒 —— 文档是给人读的，不是协议字段。
MAX_TEXT = 80

_FILE = re.compile(r"^[a-z][a-z0-9_-]*\.html$")
#: `rgb(107, 66, 38)` / `rgba(107,66,38,0.9)` —— alpha 丢掉，CSS 变量只吃 6 位。
_RGB = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)$",
    re.IGNORECASE,
)

_TOKEN_KEYS = ("bg", "surface", "ink", "muted", "accent", "accent2", "line")

#: 模型换个名字写同一个颜色的情况。**只收无歧义的别名**：`secondary` 到底指 `muted`
#: 还是 `accent2` 说不清，那种就不猜 —— 猜错比缺一个键更难查。
_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "bg": ("background", "canvas", "base"),
    "surface": ("card", "panel"),
    "ink": ("text", "fg", "foreground", "color", "colour"),
    "muted": ("dim", "sub"),
    "accent": ("primary", "brand"),
    "line": ("border", "divider", "stroke"),
}

#: CSS 颜色名 → hex。只列会出现在"暖色官网"里的那批；不在表里的照旧拒，
#: 因为"深咖色"这种**中文描述**确实没有唯一答案，硬猜等于替客户配色。
_NAMED = {
    "white": "#FFFFFF",
    "black": "#000000",
    "red": "#FF0000",
    "green": "#008000",
    "blue": "#0000FF",
    "gray": "#808080",
    "grey": "#808080",
    "silver": "#C0C0C0",
    "orange": "#FFA500",
    "brown": "#A52A2A",
    "navy": "#000080",
    "teal": "#008080",
    "olive": "#808000",
    "maroon": "#800000",
    "purple": "#800080",
    "pink": "#FFC0CB",
    "gold": "#FFD700",
    "beige": "#F5F5DC",
    "ivory": "#FFFFF0",
    "wheat": "#F5DEB3",
    "tan": "#D2B48C",
    "coral": "#FF7F50",
    "salmon": "#FA8072",
    "cream": "#FFFDD0",
}



class DocumentUnavailable(RuntimeError):
    """模型没给出能用的三份文档。`code` 进留痕，不带正文（正文可能有客户原话）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChatClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class ProjectDocs:
    """三份文档的**结构**。`digest()` 是唯一出口，文本渲染只是它的一种投影。"""

    prd: Mapping[str, Any]
    arch: Mapping[str, Any]
    tokens: Mapping[str, Any]
    #: 模型吐出形状不标准、但**修得回来**的地方，一行一条账：
    #: `architecture.理由`（多字段）、`file_repaired:Index.HTML→index.html`、
    #: `pages_truncated:7→5`、`colour_repaired:accent=#abc→#AABBCC`、`tokens.ink←text` 之类。
    #:
    #: 2026-10-28 起两次放宽，理由同一条：原来"多一个字段就整份拒"，实测撞上
    #: `architecture 多了字段：['理由']` —— 模型把 `why` 用中文又写了一遍，44.7 秒白等，
    #: PRD 永远空。多字段 / 换个写法不改变我们读的那几个键，拒掉它换不来任何正确性，
    #: 只换来一次白等。**修得回来就修，修完留下这一行账**；修完就是猜的（"深咖色"）
    #: 才拒 —— 那种收下来等于替客户配色。
    extras: tuple[str, ...] = ()

    def digest(self) -> dict[str, list[str]]:
        """→ `summary_digest` 的三段。每段一行 = 一整份 JSON。

        为什么不是"一段一行文本"：这三份要被人（前端）和机器（拆单）同时读，
        按行拆开就没法还原结构了；而多存一份结构化的第二副本就是两份口径的开始。
        """
        return {
            PRD_SECTION: [json.dumps(self.prd, ensure_ascii=False, sort_keys=True)],
            ARCH_SECTION: [json.dumps(self.arch, ensure_ascii=False, sort_keys=True)],
            TOKENS_SECTION: [json.dumps(self.tokens, ensure_ascii=False, sort_keys=True)],
        }

    def files(self) -> dict[str, str]:
        """→ `{文件名: 内容}`。给人看的投影，事实仍是 `digest()`。"""
        return {
            "prd.md": _prd_markdown(self.prd),
            "architecture.md": _arch_markdown(self.arch),
            "tokens.json": json.dumps(self.tokens, ensure_ascii=False, indent=2) + "\n",
        }


def _clip(value: object) -> str:
    text = str(value).strip()
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _prd_markdown(prd: Mapping[str, Any]) -> str:
    lines = [f"# {_clip(prd.get('title'))} · PRD", "", "## 页面结构", ""]
    for page in prd.get("pages") or ():
        lines.append(f"- **{_clip(page.get('label'))}**（`{_clip(page.get('file'))}`）：{_clip(page.get('duty'))}")
    lines += ["", "## 验收标准", ""]
    for index, item in enumerate(prd.get("acceptance") or (), 1):
        lines.append(f"{index}. {_clip(item)}")
    out = prd.get("out_of_scope") or ()
    if out:
        lines += ["", "## 本期不做", ""]
        lines += [f"- {_clip(item)}" for item in out]
    return "\n".join(lines) + "\n"


def _arch_markdown(arch: Mapping[str, Any]) -> str:
    lines = [
        "# 架构",
        "",
        f"- **选型**：{_clip(arch.get('stack'))}",
        f"- **理由**：{_clip(arch.get('why'))}",
        f"- **风险**：{_clip(arch.get('risk'))}",
    ]
    return "\n".join(lines) + "\n"


SYSTEM = """你是立项文档节点。读客户原话与立项会结论，产出三份**结构化**文档。

只输出一个 JSON 对象，形如：
{"prd":{"title":"...","pages":[{"label":"首页","file":"index.html","duty":"..."}],"acceptance":["..."],"out_of_scope":["..."]},
 "architecture":{"stack":"...","why":"...","risk":"..."},
 "tokens":{"name":"...","bg":"#RRGGBB","surface":"#RRGGBB","ink":"#RRGGBB","muted":"#RRGGBB","accent":"#RRGGBB","accent2":"#RRGGBB","line":"#RRGGBB"}}

硬规则：
1. pages 1-5 个，file 必须是小写、以 .html 结尾；duty 一句话说清这页干什么。
2. acceptance 2-6 条，每条**可验证**（能拿文件内容或浏览器行为对照），并用客户原话里的词。
3. out_of_scope 0-4 条：这一次明确不做的事。
4. architecture 三句话：选型、理由、一处真实风险（不要"可能有不兼容"这种空话）。
5. tokens 七个颜色都是 6 位 hex，前后景要读得清；name 写成"主色 / 辅色"。
6. 不要输出解释、不要 markdown 代码块、不要多余字段。"""


#: 这一次调用同时交三份文档 ⇒ 三个岗位的约束**并列**注入，各取它自己那一节。
#: 取 `### 二、…` 而不是整份：整份塞进去会把"立项会台词"也带进来，
#: 而这一步只产文档、不说话，台词那一段在这里是噪音。
DOCS_ROLES: tuple[tuple[str, str], ...] = (
    ("pm", "二、PRD 结构"),
    ("cto", "二、架构三句话"),
    ("ui", "二、设计令牌"),
)

#: 三份文档的形状契约。与上面 `SYSTEM` 的示例同义，但只讲形状、不讲内容 ——
#: 分工是：`SYSTEM` 管"写什么"，这一段管"回话长什么样"。
DOCS_CONTRACT = """只输出一个 JSON 对象，顶层三个键：`prd` / `architecture` / `tokens`，一个都不能少。
- `pages` 里每一页只保留 `label` / `file` / `duty` 三个键（多写的键会被丢弃，不进卡）。
- `file` 必须是 `^[a-z][a-z0-9_-]*\\.html$` 形态，第一页写 `index.html`。
- `tokens` 七个颜色键一个都不能少，值必须能归一成 6 位 hex（写 `深咖色` 会整份被拒）。
- 不要 markdown 围栏、不要解释文字、不要多余字段。"""

#: 岗位稿取不到时的原因码（`None` = 三份都读到了）。**不抛异常**：`SYSTEM` 本身自足。
docs_role_prompt_error: str | None = None


def system_of() -> str:
    """文档节点的 system：基础 SYSTEM + pm / cto / ui 三段岗位约束 + 形状契约。"""
    global docs_role_prompt_error
    try:
        return compose_multi_role_prompt(SYSTEM, DOCS_ROLES, output_contract=DOCS_CONTRACT)
    except PromptNotFoundError as exc:
        docs_role_prompt_error = exc.reason
        return SYSTEM


def _prompt(raw_input: str, said: Sequence[str]) -> str:
    lines = [f"客户原话：{raw_input.strip()}", ""]
    if said:
        lines.append("立项会结论：")
        lines.extend(f"- {item}" for item in said)
    return "\n".join(lines)


def _json_object(text: str) -> dict[str, Any]:
    """从回话里取出那个 JSON 对象。允许它手贱包了 ```json 围栏。"""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise DocumentUnavailable("docs_not_json", "回话里没有 JSON 对象")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        raise DocumentUnavailable("docs_bad_json", "JSON 解析失败") from None
    if not isinstance(parsed, dict):
        raise DocumentUnavailable("docs_bad_json", "顶层不是对象")
    return parsed


def _repair_file(raw: object, index: int, extras: list[str]) -> str:
    """把模型写的文件名归一成 `<小写字母开头>.html`。**归一不出来就按页序给一个**。

    实测会撞到的写法：`Index.HTML`（大写）、`pages/products.html`（带目录）、
    `contact.htm`（少个 l）、`首页.html`（中文）。这四种都是**同一个意思**，
    拒掉它换不来正确性，只换来一次白等。

    第一页兜底成 `index.html` 而不是 `page-1.html`：组包那一步要拿 `index.html` 当入口
    （`orchestrate/nodes/assemble.py`），入口名一漂，整个交付包都组不出来。
    """
    text = str(raw or "").strip()
    original = text
    # 目录前缀：`pages/index.html` → `index.html`。Windows 反斜杠也认。
    text = re.split(r"[\\/]", text)[-1].strip()
    lowered = text.lower()
    if lowered.endswith(".htm"):
        lowered += "l"
    if _FILE.match(lowered):
        if lowered != original:
            extras.append(f"file_repaired:{original}→{lowered}")
        return lowered
    derived = "index.html" if index == 0 else f"page-{index + 1}.html"
    extras.append(f"file_repaired:{original or '(空)'}→{derived}")
    return derived


def _pages_of(prd: Mapping[str, Any], extras: list[str]) -> list[dict[str, str]]:
    raw = prd.get("pages")
    if not isinstance(raw, list) or not raw:
        raise DocumentUnavailable("docs_no_pages", "prd.pages 为空")
    if len(raw) > MAX_PAGES:
        # 超过五个页面通常是模型把导航项当成了独立页面。**截断不拒**：
        # 前五页仍然是对的，拒掉等于把对的那部分也扔了。
        extras.append(f"pages_truncated:{len(raw)}→{MAX_PAGES}")
        raw = raw[:MAX_PAGES]
    pages: list[dict[str, str]] = []
    used: set[str] = set()
    for index, page in enumerate(raw):
        if not isinstance(page, dict):
            raise DocumentUnavailable("docs_bad_page", "page 不是对象")
        # 多余字段**只记不拒**（理由见 `ProjectDocs.extras`）。缺字段照样拒 ——
        # 缺才是"这页干什么"没人说，多一个 `id` 不是。
        extras.extend(f"page.{key}" for key in sorted(set(page) - {"label", "file", "duty"}))
        label = _clip(page.get("label", ""))
        duty = _clip(page.get("duty", ""))
        if not label or not duty:
            raise DocumentUnavailable("docs_missing_field", "page 缺 label 或 duty")
        file = _repair_file(page.get("file"), index, extras)
        # 两个页面落到同一个文件名 = 两张卡写同一份产物，组包时后一份盖掉前一份。
        # 这不是"模型多写了"，是**下游会静默丢内容**，必须在这里拆开。
        if file in used:
            stem = file[: -len(".html")]
            suffix = 2
            while f"{stem}-{suffix}.html" in used:
                suffix += 1
            extras.append(f"file_duplicate:{file}→{stem}-{suffix}.html")
            file = f"{stem}-{suffix}.html"
        used.add(file)
        pages.append({"label": label, "file": file, "duty": duty})
    return pages


def _list_of(
    prd: Mapping[str, Any],
    key: str,
    *,
    low: int,
    high: int,
    extras: list[str],
) -> list[str]:
    raw = prd.get(key) or []
    if not isinstance(raw, list):
        raise DocumentUnavailable("docs_bad_list", f"prd.{key} 不是数组")
    items = [_clip(item) for item in raw if str(item).strip()]
    if len(items) > high:
        extras.append(f"{key}_truncated:{len(items)}→{high}")
        items = items[:high]
    if not items and low > 0:
        raise DocumentUnavailable(f"docs_no_{key}", f"prd.{key} 一条都没有")
    if len(items) < low:
        # 少写一条不等于写错。多出来的那一条没法凭空补，少的那条也不会让剩下的失效。
        extras.append(f"{key}_short:{len(items)}<{low}")
    return items


def _repair_hex(raw: object) -> str | None:
    """任意写法 → `#RRGGBB`。认不出来返回 None（由调用方决定拒还是兜）。

    认的写法：`#aabbcc` / `aabbcc`（漏 `#`）/ `#abc`（3 位缩写）/ `#aabbccdd`
    （带 alpha，丢掉）/ `rgb(107,66,38)` / `rgba(...)` / CSS 颜色名。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    matched = _RGB.match(text)
    if matched:
        return "#" + "".join(f"{min(255, int(part)):02X}" for part in matched.groups())
    body = text.lstrip("#").strip()
    if re.fullmatch(r"[0-9a-fA-F]{3}", body):
        body = "".join(ch * 2 for ch in body)
    elif re.fullmatch(r"[0-9a-fA-F]{8}", body):
        body = body[:6]
    if re.fullmatch(r"[0-9a-fA-F]{6}", body):
        return "#" + body.upper()
    return _NAMED.get(text.lower())


def to_docs(payload: dict[str, Any]) -> ProjectDocs:
    """JSON → 三份文档，逐字段判形状。

    **值层面的毛病修，结构层面的毛病拒**（口径 5，2026-10-28 第二次放宽）：

    - 修 + 留痕：多字段、文件名不合规、两个页面同名、页数超 5、判据超 6 / 少 1、
      颜色写成 3 位 / 8 位 / `rgb()` / 漏 `#` / CSS 颜色名 / 换个键名。
    - 整份拒：三份缺一份、`pages` 为空、页面既无 label 也无 duty、
      `prd.title` 空、`architecture.stack|why` 空、颜色归一不出来（如"深咖色"）。

    分界线是"修完之后还能不能还原模型的意思"。修完还能 —— 收；修完就是猜 —— 拒。
    """
    extras: list[str] = []
    extras.extend(f"顶层.{key}" for key in sorted(set(payload) - {"prd", "architecture", "tokens"}))
    prd = payload.get("prd")
    arch = payload.get("architecture")
    tokens = payload.get("tokens")
    for name, value in (("prd", prd), ("architecture", arch), ("tokens", tokens)):
        if not isinstance(value, dict):
            raise DocumentUnavailable("docs_missing_field", f"缺 {name} 或缺成非对象")

    pages = _pages_of(prd, extras)
    acceptance = _list_of(prd, "acceptance", low=MIN_ACCEPTANCE, high=MAX_ACCEPTANCE, extras=extras)
    out = _list_of(prd, "out_of_scope", low=0, high=MAX_OUT_OF_SCOPE, extras=extras)
    title = _clip(prd.get("title", ""))
    if not title:
        raise DocumentUnavailable("docs_missing_field", "prd.title 为空")

    extras.extend(f"architecture.{key}" for key in sorted(set(arch) - {"stack", "why", "risk"}))
    arch_clean = {key: _clip(arch.get(key, "")) for key in ("stack", "why", "risk")}
    if not arch_clean["stack"] or not arch_clean["why"]:
        raise DocumentUnavailable("docs_missing_field", "architecture 缺 stack 或 why")

    extras.extend(f"tokens.{key}" for key in sorted(set(tokens) - {"name", *_TOKEN_KEYS}))
    name = _clip(tokens.get("name", ""))
    if not name:
        raise DocumentUnavailable("docs_missing_field", "tokens.name 为空")
    colours: dict[str, str] = {}
    for key in _TOKEN_KEYS:
        raw = tokens.get(key)
        source = key
        if not str(raw or "").strip():
            # 键名换了但颜色还在（`text` / `primary` / `border`…）。只认无歧义的别名。
            for alias in _TOKEN_ALIASES.get(key, ()):
                if str(tokens.get(alias) or "").strip():
                    raw, source = tokens.get(alias), alias
                    break
        value = _repair_hex(raw)
        if value is None:
            raise DocumentUnavailable("docs_bad_color", f"tokens.{key} 不是 6 位 hex：{str(raw)[:24]!r}")
        if source != key:
            extras.append(f"tokens.{key}←{source}")
        elif str(raw).strip() != value:
            extras.append(f"colour_repaired:{key}={str(raw).strip()}→{value}")
        colours[key] = value

    return ProjectDocs(
        prd={"title": title, "pages": pages, "acceptance": acceptance, "out_of_scope": out},
        arch=arch_clean,
        tokens={"name": name, **colours},
        extras=tuple(extras),
    )


def run_documents(
    raw_input: str,
    said: Sequence[str],
    *,
    client: ChatClient,
) -> ProjectDocs:
    """真调一次模型，产三份文档。失败抛 `DocumentUnavailable`（码进留痕）。

    客户端那侧的错（超时 / 传输 / HTTP）在 `meeting` 模块里叫 `meeting_*`，这里统一改写成
    `docs_*`：留着 `meeting_timeout` 去记一次**文档**失败，读日志的人会去查会议那一段 ——
    2026-10-28 就这样绕过一次弯路。
    """
    try:
        reply = client.chat(system_of(), _prompt(raw_input, said))
    except Exception as exc:  # noqa: BLE001 - 客户端异常形状不归这一层猜
        code = str(getattr(exc, "code", type(exc).__name__))
        if code.startswith("meeting_"):
            code = "docs_" + code[len("meeting_") :]
        raise DocumentUnavailable(code, str(exc)[:200]) from None
    if getattr(client, "last_finish_reason", None) == "length":
        raise DocumentUnavailable("docs_truncated", "模型回话被 max_tokens 截断")
    return to_docs(_json_object(reply))
