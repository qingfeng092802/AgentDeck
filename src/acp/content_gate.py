"""产物"写完了没有"的机械判据（P0-1，2026-10-31）。

**为什么单独一个模块**：这条判据有两个消费者 —— 门禁（`orchestrate/nodes/validate_artifact.py`）
与纯 API 执行器（`adapters/doubao/exec.py`，落盘前先判）。它住在门禁里时，适配器为了复用
就得反向 import `acp.orchestrate`，而 `tests/guards/test_assembly.py` 明令禁止那个方向
（"删掉一支适配器目录，编排图仍可构建"靠的就是零耦合）。所以判据本身下沉到这一层，
两个消费者都向上取。

**判据的边界**：只认结构性收尾（`html` 要 `</html>`；`js`/`css` 括号配平且顺序正确；
`json` 能 `loads`；`py` 能 `compile`；`md` 等自由文本不判）。不假装做语法分析 —— 运行段镜像里
没有 node，判据也必须能在任何机器上用纯 Python 复算。
"""

from __future__ import annotations

import json


#: 代码类产物的完整性判据（2026-10-31 四发真模型矩阵）：`app.js` 末行停在 `if (!`、
#: `style.css` 末行停在 `.hero-image {`，而两发都以 `done` 收工 —— 字节数与行数门槛对"写了一半"
#: 完全无感。判据必须是**纯字符串**的：运行段镜像里没有 node，复算也不该依赖本机装了什么。
CODE_FORMATS = frozenset({"js", "css"})


def _code_skeleton(text: str) -> str:
    """把字符串字面量与注释换成空格（保留换行），只留结构字符。

    直接数 `{` 会把 `content: "{"`、模板字符串里的花括号、注释里的示例算进去 —— 一份**完整**
    的样式表被自己的字符串判成截断，比放过一次截断更伤（`_linked_style_text` 那段立过同一条）。
    所以先做一次最小扫描：引号内与注释内一律不参与配对。
    """
    out: list[str] = []
    index, size = 0, len(text)
    quote = ""
    while index < size:
        char = text[index]
        if quote:
            if char == "\\" and index + 1 < size:
                out.append(" ")
                out.append("\n" if text[index + 1] == "\n" else " ")
                index += 2
                continue
            if char == quote:
                quote = ""
                out.append(" ")
                index += 1
                continue
            out.append("\n" if char == "\n" else " ")
            index += 1
            continue
        if char in "'\"`":
            quote = char
            out.append(" ")
            index += 1
            continue
        if char == "/" and index + 1 < size and text[index + 1] == "/":
            while index < size and text[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and index + 1 < size and text[index + 1] == "*":
            out.append("  ")
            index += 2
            while index < size and not (text[index] == "*" and index + 1 < size and text[index + 1] == "/"):
                out.append("\n" if text[index] == "\n" else " ")
                index += 1
            out.append("  ")
            index += 2 if index + 1 < size else 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


_PAIRS = (("{", "}"), ("(", ")"), ("[", "]"))
_CLOSERS = {closing: opening for opening, closing in _PAIRS}


def _imbalance(skeleton: str) -> str | None:
    """结构符配对：先比总数（报差额），再比顺序（报提前闭合 / 结尾未闭合）。"""
    for opening, closing in _PAIRS:
        delta = skeleton.count(opening) - skeleton.count(closing)
        if delta:
            return f"{opening}{closing} 差 {delta} 个"
    stack: list[str] = []
    for char in skeleton:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != _CLOSERS[char]:
                return f"结构符 {char} 提前闭合"
    if stack:
        return f"结尾还留着未闭合的 {stack[-1]}"
    return None


def artifact_completeness(fmt: str, text: str) -> str | None:
    """「这份产物写完了没有」的机械判据。返回原因，`None` = 看不出截断。

    只认**结构性收尾**，不假装能做语法分析：`html` 要 `</html>`；`js`/`css` 要括号配平且
    闭合顺序正确；`json` 要能 `loads`；`py` 要能 `compile`；`md` 等自由文本文档不判
    （一篇说明在任何一句后面停下来都是合法的）。
    """
    normalized = str(fmt or "").strip().lower()
    if normalized == "html":
        return None if "</html>" in text.casefold() else "缺少 </html> 收尾"
    if normalized in CODE_FORMATS:
        problem = _imbalance(_code_skeleton(text))
        return f"代码在结尾处不完整（{problem}）" if problem else None
    if normalized == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"JSON 解析失败（{exc.msg}，第 {exc.lineno} 行第 {exc.colno} 列）"
        return None
    if normalized == "py":
        try:
            compile(text, "<artifact>", "exec")
        except SyntaxError as exc:
            return f"Python 语法错误（第 {exc.lineno} 行：{exc.msg}）"
        return None
    return None
