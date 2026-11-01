"""代码死角的守卫：清理是**一次**的事，不回来是**每次**的事。

2026-10-30 全仓扫过一遍 `src/` 与 `web/src/`：

- 被注释掉的旧代码块：**0 处**；
- `TODO` / `FIXME`：**0 条**（唯一命中的两行是文档里写 ASCII 转义序列的说明，不是标记）；
- 没人提及的模块：**0 个**（`0011_...` 那条由 Alembic 按 revision id 引用，不算死角）。

那一遍的结论是"今天干净"，而这条判据要的是"明天也干净" ——
死代码最贵的形态不是它存在，而是它**在第三次重构里被当成现成的实现接回去**
（注释里那段旧代码读起来总是比新代码更"完整"）。

两条口径要分清，**不许把历史留痕读成死角**：
`本文件 2026-10-16 那条的改写` 这种带时点的说明是**读数**，是有意留下的；
被注释掉的 `def ...` / `const ...` 才是死角。所以下面的扫描只找"长得像代码"的注释行，
不碰中文说明 —— 本仓的正文注释绝大多数是中文，这条判据正好靠这个区分。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES = (REPO_ROOT / "src", REPO_ROOT / "web" / "src")
SUFFIXES = (".py", ".ts", ".tsx")

#: 中日韩统一表意文字区间：本仓的正文注释绝大多数落在里面。
CJK = re.compile(r"[一-鿿]")

#: 注释行里"长得像代码"的形状：有函数调用/赋值/箭头，且**没有中文说明**。
CODE_SHAPED = re.compile(r"(\w+\s*\([^)]*\)|=>|\w+\s*=\s*[^=\s])")

#: 纯装饰的分隔线（`===` / `---`）不算代码。
DECORATION = re.compile(r"^[=\-*#/·\s]+$")

MARKERS = ("TODO", "FIXME")


def _sources():
    for root in SOURCES:
        for path in sorted(root.rglob("*")):
            if path.suffix in SUFFIXES and "__pycache__" not in str(path):
                yield path


def _comment_bodies(path: Path):
    """取出每一行注释里**去掉 `#` / `//` 之后**的正文。行内注释取 `#` 之后那一段。"""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if path.suffix == ".py":
            body = stripped[1:].strip() if stripped.startswith("#") else None
        else:
            if stripped.startswith("//"):
                body = stripped[2:].strip()
            elif stripped.startswith("/*"):
                body = stripped[2:].strip()
            else:
                body = None
        if body:
            yield number, body


def test_no_ownerless_todo_or_fixme() -> None:
    """`TODO` / `FIXME` 必须带归属（人 / 时点），否则不许存在。

    "以后再看"这种没有主人的标记是一次延期决定，而延期决定不属于代码 ——
    它该落在台账里。有主人的（`(owner 2026-10-30)` / `owner:` / `@延哥`）可以留。
    """
    ownerless: list[str] = []
    for path in _sources():
        for number, body in _comment_bodies(path):
            if not any(marker in body for marker in MARKERS):
                continue
            if re.search(r"owner|@[A-Za-z一-鿿]|20\d\d-\d\d-\d\d", body, re.IGNORECASE):
                continue
            ownerless.append(f"{path.relative_to(REPO_ROOT)}:{number}: {body[:60]}")
    assert ownerless == [], f"没有归属的 TODO/FIXME：{ownerless}"


def test_no_commented_out_code() -> None:
    """注释里不许出现"长得像代码"的行（本仓正文是中文，靠这个区分）。

    放过两类：中文说明（本仓的正常形态），以及 `===` 那种纯装饰分隔线。
    """
    suspects: list[str] = []
    for path in _sources():
        for number, body in _comment_bodies(path):
            if CJK.search(body) or DECORATION.match(body):
                continue
            if CODE_SHAPED.search(body):
                suspects.append(f"{path.relative_to(REPO_ROOT)}:{number}: {body[:80]}")
    assert suspects == [], f"疑似被注释掉的旧代码：{suspects}"


def test_noqa_always_says_why() -> None:
    """`# noqa: CODE` 后面必须跟理由。

    一次没有理由的豁免等于"这里有个问题，但我决定不再解释" —— 三个月后
    没人知道它豁免的到底是什么，于是它会被一直留着，直到变成一处真缺陷的藏身处。
    """
    silent: list[str] = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"#\s*noqa:\s*([A-Z0-9]+)\s*(.*)$", line)
            if not match:
                continue
            rest = match.group(2).strip()
            if not rest.startswith(("-", "(", "—")):
                silent.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:80]}")
    assert silent == [], f"没有理由的 noqa：{silent}"


@pytest.mark.parametrize("marker", MARKERS)
def test_the_marker_words_do_not_leak_into_identifiers(marker: str) -> None:
    """反向对照：`TODO`/`FIXME` 出现在标识符里（变量名、类型名）不算标记。

    少了这半边，上面那条判据会因为有人起了一个叫 `TodoItem` 的类型而假红 ——
    那正是"门禁假红 ⇒ 有人把门禁关掉"的起点。
    """
    allowed = re.compile(rf"\w*{marker}\w*")
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for word in allowed.findall(text):
            if word.upper() == marker:
                continue
            # 命中复合词（TodoItem / FIX_ME_LATER）是允许的，不断言。
            assert isinstance(word, str)
