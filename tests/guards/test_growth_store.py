"""MVP-2 第三步 ①：个人进化档案的载体、读写与注入块判据（owner 2026-10-19 指令）。

**这一文件不连库、不出网**：档案是两份 `.md`，它的事实态就在盘上。
本文件管三节，各钉一件事：

- 节 一 = **权威源绑定**（`config/params-ref.md §11`）。绑定写成**行为式**而不是字符串相等：
  把从本节读出的变量名写进环境，根目录必须跟着走；只改文档或只改代码，各有一头红（口径同 §10）。
- 节 二 = 读写形状的四个"不许"：不许把"文件没了"读成空串、不许半份档案、不许超界的静默截断、
  不许把使用者的字洗成 `[REDACTED]` 再存。
- 节 三 = 注入块的两段标题与"全文、空文件也在场"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from acp.config.loader import REPO_ROOT, REDACTED
from acp.growth import store

SPEC = REPO_ROOT / "config" / "params-ref.md"


def fence_values() -> dict[str, str]:
    """取 §11 那个 ```text 围栏里的 `KEY=value`。解析失败必须红 —— 取不到数这条就是死判据。"""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## 11. ", 1)
    assert len(section) == 2, "params-ref 里没有 §11 ⇒ 档案根在本仓没有权威出处"
    block = re.search(r"```text\n(.*?)```", section[1], re.S)
    assert block, "§11 里没有 ```text 围栏"
    values = {}
    for line in block.group(1).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    assert values, "§11 的围栏是空的"
    return values


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "growth"
    root.mkdir()
    for name in store.ARCHIVES:
        (root / name).write_text("", encoding="utf-8")
    return root


# ---------------------------------------------------------------- 一、权威源绑定


def test_the_env_var_named_in_section_11_moves_the_root():
    """§11 那一行是 `ACP_GROWTH_ROOT=growth`：**左边是变量名，右边是默认目录名**，两侧都要绑。

    第一版我把右边那个值当变量名去设环境，于是"根没跟着动"，这条判据红得正是地方 ——
    红在测试自己写错，而不是红在产品代码：变量名与默认值是两个不同的事实，不能互换。
    """
    values = fence_values()
    var_name = next((key for key in values if key.endswith("_ROOT")), "")
    assert var_name, f"§11 的围栏里没有以 `_ROOT` 起头的键：{sorted(values)}"
    assert store.GROWTH_ROOT_ENV == var_name, (
        f"§11 登记的变量名是 {var_name!r}，代码读的是 {store.GROWTH_ROOT_ENV!r} ⇒ 只改了一侧"
    )
    default_dir = values[var_name]
    assert default_dir == store.DEFAULT_ROOT_NAME, (
        f"§11 写的默认目录名是 {default_dir!r}，代码里是 {store.DEFAULT_ROOT_NAME!r} ⇒ 只改了一侧"
    )
    # 行为式绑定：把**从文档读出的那个变量名**写进环境，解析结果必须跟着走。
    resolved = store.growth_root(env={var_name: "somewhere-else"})
    assert resolved == REPO_ROOT / "somewhere-else", f"代码没在读文档里那个变量名：{resolved}"
    assert store.growth_root(env={var_name: "   "}) == REPO_ROOT / default_dir, "空白值要按没设处理"
    assert store.growth_root(env={}) == REPO_ROOT / default_dir


def test_the_closed_sets_and_bounds_in_section_11_match_the_code():
    values = fence_values()
    assert tuple(values["GROWTH_FILES"].split(",")) == store.ARCHIVES, "档案封闭集合两侧不同集"
    assert int(values["GROWTH_MAX_ARCHIVE_BYTES"]) == store.MAX_ARCHIVE_BYTES, "上界两侧不同值"
    # `GROWTH_INJECT=unconditional-full-text` 在代码侧的形状就是那两行标题：标题里写的是"全文注入"，
    # 哪天有人把它改成"摘要"或"要点"，这一句会先红 —— 绑的是字面量与承诺，不是再抄一遍枚举值。
    assert values["GROWTH_INJECT"] == "unconditional-full-text"
    assert "全文注入" in store.PROFILE_HEADER and "全文注入" in store.MISTAKES_HEADER


# ---------------------------------------------------------------- 二、读写形状


def test_a_missing_archive_raises_instead_of_reading_as_empty(archive: Path):
    (archive / store.PROFILE_FILE).unlink()
    with pytest.raises(store.GrowthError, match="不返回空串"):
        store.read(store.PROFILE_FILE, root=archive)


def test_a_name_outside_the_closed_set_is_refused(archive: Path):
    """第三个名字一开，"读档案"就变成"读仓库里任意文件"（`.env` 也算文件）。"""
    with pytest.raises(store.GrowthError, match="封闭|只接受"):
        store.read(".env", root=archive)
    with pytest.raises(store.GrowthError, match="只接受"):
        store.write("agents.md", "x", root=archive)


def test_write_lands_whole_or_not_at_all(archive: Path):
    content = "第一次\n第二段\n"
    digest = store.write(store.PROFILE_FILE, content, root=archive)
    assert digest == store.digest_of(content)
    assert store.read(store.PROFILE_FILE, root=archive) == content
    # 原子性：写盘路径上不许留下临时文件（半途失败留下 `.my_profile.*.tmp` 就是给仓库添垃圾）。
    leftovers = [p.name for p in archive.iterdir() if p.name != store.PROFILE_FILE and p.name != store.MISTAKES_FILE]
    assert leftovers == [], f"临时文件没清掉：{leftovers}"


def test_an_oversized_archive_is_refused_without_touching_the_file(archive: Path):
    old = (archive / store.PROFILE_FILE).read_text(encoding="utf-8")
    too_long = "长" * (store.MAX_ARCHIVE_BYTES + 1)
    with pytest.raises(store.GrowthError, match="拒写"):
        store.write(store.PROFILE_FILE, too_long, root=archive)
    assert (archive / store.PROFILE_FILE).read_text(encoding="utf-8") == old, "拒写之后原文件必须一格未动"


def test_a_credential_shaped_archive_is_refused_not_washed(archive: Path):
    """命中凭据形态 ⇒ 拒写。**不许**出现"存下的是洗过的版本"：那既丢了字又抹平了事实。

    那一行必须**顶格**写：`NAME=值` 这条形态判据的锚是行首（`^[ \\t]*`），
    把它塞进句子中间就不算命中 —— 第一版我就是这么写的，于是这条判据"绿"得毫无意义。
    """
    secret_line = "ARK_API_KEY=" + "a" * 24
    with pytest.raises(store.GrowthError, match="凭据形态"):
        store.write(store.MISTAKES_FILE, f"笔记\n{secret_line}\n", root=archive)
    written = (archive / store.MISTAKES_FILE).read_text(encoding="utf-8")
    assert REDACTED not in written, "洗过再存就是这条规则被反着实现了"
    assert written == "", "拒写时不该留下任何内容"


def test_a_bom_on_disk_is_not_content(archive: Path):
    """使用者可能用 PowerShell / 记事本存过：BOM 是编码痕迹，不是档案的第一个字（口径同 danger_list）。"""
    (archive / store.PROFILE_FILE).write_bytes(b"\xef\xbb\xbf" + "第一行\n".encode("utf-8"))
    assert store.read(store.PROFILE_FILE, root=archive).startswith("第一行")


def test_a_missing_root_is_reported_before_any_write_is_attempted(tmp_path: Path):
    with pytest.raises(store.GrowthError, match="根不存在"):
        store.write(store.PROFILE_FILE, "内容", root=tmp_path / "nope")


# ---------------------------------------------------------------- 三、注入块


def test_the_block_carries_both_headers_in_order(archive: Path):
    block = store.render_block(root=archive)
    assert block.index(store.PROFILE_HEADER) < block.index(store.MISTAKES_HEADER)
    assert block.count("\n" + store.PROFILE_HEADER) == 0 and block.startswith(store.PROFILE_HEADER)


def test_empty_archives_still_produce_the_block(archive: Path):
    """"无条件"的可证形态：档案为空时标题仍在。

    只在有内容时断言"内容在场"，就会允许"有内容才注入"这一条实现偷偷生效。
    """
    block = store.render_block(root=archive)
    assert store.PROFILE_HEADER in block and store.MISTAKES_HEADER in block


def test_a_long_archive_is_not_cut(archive: Path):
    """一份接近上界的档案必须**整段**出现在块里：判据按"最后一行也在场"量，而不是按长度近似。

    尺寸要卡在 `(200 KB, 上界)` 之间：低于 200 KB 证不了"长"，高于上界就先撞上"拒写"那条规则，
    于是这条判据测的会变成节 二 那一发而不是截断（第一版我就写超了，红在拒写上）。
    """
    tail = "这是最后一行，截断就会丢掉它"
    body = "\n".join(f"第 {n} 行 " + "错" * 20 for n in range(3000))
    store.write(store.PROFILE_FILE, f"{body}\n{tail}\n", root=archive)
    size = len(f"{body}\n{tail}\n".encode("utf-8"))
    assert 200_000 < size < store.MAX_ARCHIVE_BYTES, f"这一发没落在预期的尺寸窗口里：{size}"
    block = store.render_block(root=archive)
    assert tail in block
    assert body.splitlines()[0] in block and body.splitlines()[-1] in block
    assert len(block.encode("utf-8")) >= size, "注入块比档案本身还短 ⇒ 中间被裁过"
