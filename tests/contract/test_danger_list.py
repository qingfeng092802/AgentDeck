"""T101 的判据：`config/danger_list.yaml` 的加载、内容指纹与"平台只读"归属（FR-055 / FR-058｜R-15）。

本文件属 **fast 档**（`tests/contract/`），不碰 PG、不碰外部凭据。
SC-010 那五段（两档各拦一次、扩展项默认拦、SAFE 档请示、可按指纹回溯、反向不误拦）
归 `tests/guards/test_autonomy_danger.py`（T107），因为那要先有 `guard/autonomy.py`（T104）这个判定点。
这里只管四件事，每件事都是"清单这一层"能独立证明的：

1. **形状**（第二节）—— 写坏的清单进不了内存，且每种坏法各有各的拒收理由；
2. **匹配语义**（第三节）—— 词序列连续匹配，正向命中与**反向不误拦**两侧都钉；
3. **内容指纹**（第四节）—— "什么算换版"：增删改条目算，注释 / 空白 / 顺序不算，两个方向都有用例；
4. **归属**（第五节）—— 平台代码里没有人写这份清单（C5 的结论，AST 扫写入面，不是靠注释声明）。
   T106 之后这句话多了一个**登记出来的**例外（不是放宽出来的）：名单一项 = 使用者敲的那条命令，
   外加"名单必须是活的"与"运行期不许 import 那个入口"两条对照，限度在各自文档里点名。

**为什么第三节值得单独钉在这一层**：EN-16 那一列存在的理由是"审计时能查当时是哪版清单"
（`data-model.md:219`、`tasks.md` T105）。如果指纹对注释敏感，审计读数会天天"换版"而策略没变；
如果它对条目变更不敏感，就是"改了清单而行为不变"那个 bug 的假绿形态。两头都得钉。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import acp
from acp.config.loader import (
    DANGER_INTERCEPT,
    DANGER_LIST_PATH,
    DangerEntry,
    DangerListError,
    load_danger_list,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 扫描面 = **当前被 import 的那棵 `acp` 树**，不是按 `parents[2]` 猜出来的 `仓库根/src/acp`。
#: 理由不是整洁，是 T105 留下的实测教训：变异跑时 `PYTHONPATH` 指到 `/tmp/mutsrc`，而按字面路径扫的
#: "仓内 0 命中"读的是**那棵没被改过的树** ⇒ 变异没红、看着像通过（假绿的第二种形态，
#: 与"子进程继承了 PYTHONPATH=src"那一种是同一族）。
#: 正常跑（`PYTHONPATH=src`）时这一值就等于 `REPO_ROOT / "src" / "acp"`，读数一字不差。
SRC_ROOT = Path(acp.__file__).resolve().parent

#: 一条**形状合法**的最小清单文本，第二行之后的用例都在它上面做单点改动。
BASE_LIST = "- pattern: rm -rf\n- pattern: git push\n"

#: 假的凭据**形状**（不是任何真值）。用来验"报错回显清单原文时是否过脱敏"。
FAKE_KEY_SHAPE = "sk-test-0123456789abcdef"

FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "danger_list.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ── 一、随仓清单本体 ──────────────────────────────────────────────────────────


def test_the_shipped_list_loads_and_every_entry_is_intercept_by_default() -> None:
    """内置清单可读、非空，且**没有任何一条自带放行**（FR-056「一律拦截」的形状前提）。"""
    loaded = load_danger_list()
    assert loaded.entries, "内置清单不能是空的（空清单在加载层就是错误，见第二节）"
    assert {entry.action for entry in loaded.entries} == {DANGER_INTERCEPT}
    # 随仓那份**不该有** user_added 项：那是使用者的笔，产出方替使用者"预先扩展"就是造数（FR-058）。
    assert loaded.user_added_count == 0, f"随仓清单里出现了 user_added 项：{loaded.patterns}"


def test_the_three_actions_the_owner_named_are_all_present() -> None:
    """`tasks.md` T100 原话点名的三条（`sudo` / `rm -rf` / `git push`）逐条在位。

    **这条判据的限度要点名**：三个词是硬编码的，出处是 T100 那一行；owner 若改那一行的措辞，
    本用例不会自动跟着变。它证明的是"点名的三条没被漏"，不证明"清单覆盖面够宽"——
    后者归 T107 的 SC-010 与 Phase 13。
    """
    folded = {entry.dedupe_key for entry in load_danger_list().entries}
    for named in ("sudo", "rm -rf", "git push"):
        assert named in folded, f"T100 点名的 {named!r} 不在清单里"


def test_production_call_reads_the_config_file_itself() -> None:
    """不传 `path` 的调用 = 读 `config/danger_list.yaml`（"生产调用方 MUST 不传"的形状核对）。"""
    direct = load_danger_list()
    explicit = load_danger_list(DANGER_LIST_PATH)
    assert (direct.fingerprint, direct.entries) == (explicit.fingerprint, explicit.entries)
    assert DANGER_LIST_PATH == REPO_ROOT / "config" / "danger_list.yaml"


# ── 二、形状判据：写坏的清单进不了内存 ─────────────────────────────────────────


def test_an_empty_or_mapping_top_level_is_refused(tmp_path: Path) -> None:
    """空清单 = 一份"谁都不拦"的清单 ⇒ 按错误处理，不按默认放行处理（方向与 FR-056 一致）。"""
    for text in ("", "[]\n", "entries:\n  - pattern: rm -rf\n"):
        with pytest.raises(DangerListError, match="非空清单"):
            load_danger_list(_write(tmp_path, text))


def test_entries_outside_the_three_keys_are_refused(tmp_path: Path) -> None:
    """表外键与缺 `pattern` 都拒 —— "多一个键"不是扩充，是第二份清单的开始。"""
    with pytest.raises(DangerListError, match="表外键"):
        load_danger_list(_write(tmp_path, "- pattern: rm -rf\n  enabled: true\n"))
    with pytest.raises(DangerListError, match="缺必填键"):
        load_danger_list(_write(tmp_path, "- user_added: true\n"))
    with pytest.raises(DangerListError, match="不是映射"):
        load_danger_list(_write(tmp_path, "- rm -rf\n"))


BAD_SHAPES: tuple[tuple[str, str], ...] = (
    ("action-allow", "- pattern: rm -rf\n- pattern: git push\n  action: allow\n"),
    ("action-pass", "- pattern: rm -rf\n  action: pass\n"),
    ("action-casefold", "- pattern: rm -rf\n  action: INTERCEPT\n"),
    ("action-missing-value", "- pattern: rm -rf\n  action:\n"),
    ("user-added-string", "- pattern: rm -rf\n  user_added: 'true'\n"),
    ("user-added-number", "- pattern: rm -rf\n  user_added: 1\n"),
    ("pattern-blank", "- pattern: '   '\n"),
    ("pattern-number", "- pattern: 42\n"),
    ("pattern-sequence", "- pattern:\n    - rm\n    - rf\n"),
)


@pytest.mark.parametrize(
    ("bad_shape", "text"),
    BAD_SHAPES,
    ids=[shape for shape, _ in BAD_SHAPES],
)
def test_each_bad_shape_has_its_own_refusal(bad_shape: str, text: str, tmp_path: Path) -> None:
    """九种写坏的形态各拒一次（`bad_shape` 只当标签用，不回显清单原文）。

    逐条为什么要各钉一条，它们对应的失效各不相同：
    - `action-allow` / `action-pass`：第二条放行分支的入口（T104「不存在第二条放行分支」）；
    - `action-casefold`：动作值**不**做大小写归一。看着像笔误，实际是取舍 ——
      归一 `INTERCEPT` 就要归一 `ALLOW`，那一步一走，"清单里写放行"就又成立了；
    - `action-missing-value`：`action:` 空值读成 `None`，既不是缺省也不是合法值 ⇒ 拒；
    - `user-added-string` / `user-added-number`：`'true'` 与 `1` 在 YAML 里都是真值但不是布尔，
      靠真值猜就会让"是不是使用者加的"这个审计字段失真；
    - `pattern-*`：空 / 非字符串 / 写成列表 ⇒ 没有可比对的词序列，宁可拒也不"拼一下试试"。
    """
    with pytest.raises(DangerListError):
        load_danger_list(_write(tmp_path, text))


def test_an_explicit_intercept_action_is_accepted(tmp_path: Path) -> None:
    """反向对照：写出 `action: intercept` 是合法的（拒的是"别的动作值"，不是"写了 action 这件事"）。"""
    loaded = load_danger_list(_write(tmp_path, f"- pattern: rm -rf\n  action: {DANGER_INTERCEPT}\n"))
    assert loaded.entries[0].action == DANGER_INTERCEPT


def test_a_case_or_whitespace_twin_of_an_entry_is_refused_as_duplicate(tmp_path: Path) -> None:
    """重复项拒收，且**大小写 / 空白写法不同也算重复**（同一条策略不该有两份指纹、两份审计归因）。

    正向半边在同一处：宽条与细条并存是合法的（`git push` 与 `git push --force` 不是重复项），
    否则这条用例会退化成"清单不许有两条以 git 开头的项"那种假判据。
    """
    for text in ("- pattern: rm -rf\n- pattern: RM -RF\n", "- pattern: rm  -rf\n- pattern: rm -rf\n"):
        with pytest.raises(DangerListError, match="重复"):
            load_danger_list(_write(tmp_path, text))
    both = load_danger_list(_write(tmp_path, "- pattern: git push\n- pattern: git push --force\n"))
    assert len(both.entries) == 2


def test_a_missing_file_is_reported_not_replaced_by_a_builtin_default(tmp_path: Path) -> None:
    """文件不存在 ⇒ 报错。代码里没有"内置兜底清单"，所以这条路径不可能悄悄放宽。"""
    with pytest.raises(DangerListError, match="读不出来"):
        load_danger_list(tmp_path / "nope.yaml")


# ── 三、匹配语义：正向命中与反向不误拦 ─────────────────────────────────────────

#: 命中用例的清单**固定顺序**（匹配语义与"命中条目"的呈现顺序都在这上面断，顺序不能随机）。
MATCH_LIST = ("sudo", "rm -rf", "git push", "alembic downgrade", "docker compose down -v")

#: 每条写成 `(命令, 期望命中的 pattern 有序元组)`，而不是"命中/不命中"两堆字符串 ——
#: 只断"命中了"会让"命中错了条目"过关，而 T105 的留痕要写的正是**命中了哪条**。
HITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sudo rm -rf /tmp/x", ("sudo", "rm -rf")),
    ("rm -rf ./build", ("rm -rf",)),
    ("RM -RF ./build", ("rm -rf",)),
    ("git push origin main", ("git push",)),
    ("wsl.exe -u root sudo ls", ("sudo",)),
    ("git push --force-with-lease", ("git push",)),
    ("alembic downgrade -1", ("alembic downgrade",)),
    ("docker compose down -v", ("docker compose down -v",)),
)

#: 反向不误拦的样本：全是"不在清单内且可自动回滚 / 只读"的形态（T107 ⑤ 要的那一族）。
MISSES: tuple[str, ...] = (
    "ls -la",
    "git status",
    "git pushy --all",
    "cat /usr/local/sudo_helper/readme.md",
    "rm file.txt",
    "alembic upgrade head",
    "alembic current",
    "git clean -n",
    "docker compose down",
    "docker compose up -d db",
    "python -m pytest -q",
)


@pytest.mark.parametrize(
    ("command", "expected"), HITS, ids=[f"hit-{index}" for index in range(len(HITS))]
)
def test_a_listed_action_hits_exactly_the_named_entries(
    command: str, expected: tuple[str, ...], tmp_path: Path
) -> None:
    loaded = load_danger_list(
        _write(tmp_path, "".join(f"- pattern: {pattern}\n" for pattern in MATCH_LIST))
    )
    assert tuple(entry.pattern for entry in loaded.match(command)) == expected


@pytest.mark.parametrize("command", MISSES, ids=[f"safe-{index}" for index in range(len(MISSES))])
def test_a_read_only_or_reversible_action_is_not_listed(command: str) -> None:
    """反向半边：**随仓**清单不许拦住这些（拦住了就是用"全拦"换 100% 拦截率的读数）。"""
    assert load_danger_list().match(command) == ()


#: P0-2：包装器把命令藏进 `-c` 的内层串。每条都对应清单里一条真实 pattern。
WRAPPED_HITS: tuple[tuple[str, str], ...] = (
    ("sh -c 'rm -rf /'", "rm -rf"),
    ("bash -lc 'rm -rf /'", "rm -rf"),
    ("cmd /c \"del /s C:\\tmp\"", "del /s"),
    ("powershell -Command \"Remove-Item -Recurse C:\\tmp\"", "Remove-Item -Recurse"),
    ("psql -c 'truncate table task_card'", "truncate table"),
    ("sh -c \"bash -c 'rm -rf /'\"", "rm -rf"),
)


@pytest.mark.parametrize(
    ("command", "pattern"), WRAPPED_HITS, ids=[f"wrapped-{index}" for index in range(len(WRAPPED_HITS))]
)
def test_a_wrapped_command_is_scanned_through_its_c_payload(command: str, pattern: str) -> None:
    """P0-2：`sh -c 'rm -rf /'` 的词序列里 `rm -rf` 本来被引号拆坏了；二次切词后必须命中。"""
    hits = tuple(entry.pattern for entry in load_danger_list().match(command))
    assert pattern in hits, f"{command!r} 没有命中 {pattern!r}：{hits}"


def test_the_c_payload_scan_does_not_mistake_grep_count_for_a_command() -> None:
    """反向对照：`grep -c "rm -rf" log` 是搜索计数，不是执行 `rm -rf`。

    只在**已知包装器**后面认 `-c`；把 grep 的计数参数也当命令扫，会让一次正常搜索被拦成
    D 类卡点 —— 那正是"全拦"退化。
    """
    assert load_danger_list().match('grep -c "rm -rf" build.log') == ()


def test_an_unbalanced_quote_does_not_crash_and_still_falls_back() -> None:
    """任务文本里的中文引号/落单撇号很常见：`shlex` 会抛，拦截器不能跟着崩。"""
    assert "rm -rf" in tuple(entry.pattern for entry in load_danger_list().match('sh -c "rm -rf /'))


def test_match_returns_every_hit_in_file_order(tmp_path: Path) -> None:
    """宽条与细条同时命中时两条都返回，且按**文件顺序**（留痕记"当时是哪几条拦住了它"）。

    后半个单点是必需的：只有"返回两条"的断言，一个写死返回前两条的实现也能过。
    """
    loaded = load_danger_list(
        _write(tmp_path, "- pattern: git push\n- pattern: git push --force\n")
    )
    assert tuple(e.pattern for e in loaded.match("git push --force origin main")) == (
        "git push",
        "git push --force",
    )
    narrow = load_danger_list(_write(tmp_path, "- pattern: git push --force\n"))
    assert tuple(e.pattern for e in narrow.match("git push --force origin main")) == (
        "git push --force",
    )


def test_tokens_are_the_normalized_word_sequence_of_the_pattern() -> None:
    """`DangerEntry.tokens` 是匹配的唯一输入形状：casefold + 按空白切词（`matches` 的两个半边）。"""
    entry = DangerEntry(pattern="RM  -RF", action=DANGER_INTERCEPT, user_added=False)
    assert entry.tokens == ("rm", "-rf")
    assert entry.matches(("sudo", "rm", "-rf", "/tmp")) is True
    assert entry.matches(("sudo", "rm", "/tmp", "-rf")) is False  # 不连续 ⇒ 不命中
    assert entry.matches(("rm",)) is False  # 命令词数不足时不许误判
    assert entry.matches(()) is False  # 空命令


# ── 四、内容指纹：什么算换版 ───────────────────────────────────────────────────


def test_the_fingerprint_is_a_prefixed_digest(tmp_path: Path) -> None:
    """EN-16 那一列的形状：`sha256:<64 hex>`（与 `data-model.md:219` 的"内容指纹"同口径）。"""
    loaded = load_danger_list(_write(tmp_path, BASE_LIST))
    assert FINGERPRINT_RE.match(loaded.fingerprint), loaded.fingerprint
    assert FINGERPRINT_RE.match(loaded.source_sha256), loaded.source_sha256


def test_a_comment_or_order_or_whitespace_change_is_not_a_new_version(tmp_path: Path) -> None:
    """**指纹不动**的一侧：注释、条目顺序、空白与换行写法都不是策略变更。

    没有这一条，"当时是哪版清单"会把这些无语义变更也读成换版，审计字段就失去意义。
    """
    plain = load_danger_list(_write(tmp_path, BASE_LIST))
    commented = load_danger_list(_write(tmp_path, f"# 随手注释\n{BASE_LIST}# 尾部注释\n"))
    reordered = load_danger_list(_write(tmp_path, "- pattern: git push\n- pattern: rm -rf\n"))
    padded = load_danger_list(_write(tmp_path, "- pattern: rm  -rf\n- pattern: git   push\n"))
    split = load_danger_list(_write(tmp_path, '- pattern: "rm\\n-rf"\n- pattern: git push\n'))
    assert plain.fingerprint == commented.fingerprint == reordered.fingerprint == padded.fingerprint
    assert plain.fingerprint == split.fingerprint
    # 同一处改动在**字节面**上是看得见的：两个读数各答各的问题（策略 / 文件），不许混用。
    assert plain.source_sha256 != commented.source_sha256


def test_adding_toggling_or_removing_an_entry_is_a_new_version(tmp_path: Path) -> None:
    """**指纹必动**的一侧：增条目、翻 `user_added`、删条目三种各算换版。"""
    base = load_danger_list(_write(tmp_path, BASE_LIST))
    added = load_danger_list(_write(tmp_path, f"{BASE_LIST}- pattern: sudo\n"))
    toggled = load_danger_list(
        _write(tmp_path, "- pattern: rm -rf\n  user_added: true\n- pattern: git push\n")
    )
    removed = load_danger_list(_write(tmp_path, "- pattern: rm -rf\n"))
    assert added.fingerprint != base.fingerprint
    assert toggled.fingerprint != base.fingerprint
    assert removed.fingerprint != base.fingerprint


def test_the_list_is_reloaded_from_disk_on_every_call(tmp_path: Path) -> None:
    """FR-055 那句"重载走 loader 而非重启碰运气"的实现形态：**模块级没有缓存**。

    本节最要紧的一条 —— 缓存一份就是 `quickstart.md` 排障表里
    "改了清单但没重载 ⇒ 用的还是旧版"那一行的成因，而且它会伪装成"清单正在生效"。
    """
    path = _write(tmp_path, BASE_LIST)
    before = load_danger_list(path)
    path.write_text(f"{BASE_LIST}- pattern: sudo\n", encoding="utf-8")
    after = load_danger_list(path)
    assert len(after.entries) == len(before.entries) + 1
    assert after.fingerprint != before.fingerprint


def test_a_bom_written_by_powershell_changes_neither_the_policy_nor_the_byte_reading(tmp_path: Path) -> None:
    """清单的写入者是使用者本人，而 PowerShell 5.1 的 `Set-Content` 会给文件加 BOM。

    BOM 不是策略变更 ⇒ 条目与语义指纹都不变；同时 `source_sha256` 也必须相同 ——
    解码时就把 BOM 去掉（`utf-8-sig`），排障时才不会被"BOM 与否"读成两份文件。
    第三句断言是这条用例**唯一有判别力的部分**：本轮变异 P8-11（把 `utf-8-sig` 换成 `utf-8`）
    实测发现 PyYAML 自己就容忍了串上的 BOM，条目与语义指纹两侧照样相等 ⇒ 只断前两句的话，
    这条用例在任何一种解码下都是绿的（恒真判据）。加第三句之后 P8-11 才会红。
    """
    plain = tmp_path / "plain.yaml"
    plain.write_text(BASE_LIST, encoding="utf-8")
    with_bom = tmp_path / "bom.yaml"
    with_bom.write_text(BASE_LIST, encoding="utf-8-sig")
    assert with_bom.read_bytes()[:3] == b"\xef\xbb\xbf"  # 前置动作：BOM 确实写进去了
    loaded_plain, loaded_bom = load_danger_list(plain), load_danger_list(with_bom)
    assert loaded_plain.entries == loaded_bom.entries
    assert loaded_plain.fingerprint == loaded_bom.fingerprint
    assert loaded_plain.source_sha256 == loaded_bom.source_sha256


# ── 五、归属：平台只读这份清单（C5）───────────────────────────────────────────

#: 会改动文件内容的调用名。`open` 只按"这一调用里提到了清单"计（读写模式都算写入面）。
MUTATING_CALLS = frozenset({"write_text", "write_bytes", "open", "unlink", "replace", "remove"})

#: 扫描用的键：清单文件名与那个常量都含这一段（`DANGER_LIST_PATH` 去 casefold 后同样命中）。
LIST_TOKEN = "danger_list"


def _mutating_calls_mentioning(source: str, token: str = LIST_TOKEN) -> list[str]:
    """返回一份源码里"改动清单文件的调用"（命中即违例）。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in MUTATING_CALLS and token in ast.unparse(node).casefold():
            found.append(ast.unparse(node))
    return found


def test_the_scanner_catches_a_write_injected_at_each_position_it_claims_to_cover() -> None:
    """**先证扫描器会开火**，再拿它去断"仓内 0 命中" —— 否则那个 0 可能只是扫描面本来就是空的。

    四种注入形态对应四种真实写法：拼路径、用那个常量、走 `open`、以及删除
    （删清单与写空清单是同一个失效方向：没有清单 ⇒ 谁都不拦）。
    """
    for source in (
        '(REPO_ROOT / "config" / "danger_list.yaml").write_text("[]")',
        "DANGER_LIST_PATH.write_bytes(b'')",
        'f = open(str(DANGER_LIST_PATH), "w")',
        "Path(DANGER_LIST_PATH).unlink()",
    ):
        assert _mutating_calls_mentioning(source), source


def test_the_scanner_does_not_claim_to_catch_an_indirect_write() -> None:
    """**盲区钉住**（负面对照）：路径先赋给一个不含清单字样的变量、再在该变量上写，本扫描器抓不到。

    写这条不是"顺手"，是因为上一条用例的 0 命中很容易被读成"平台不可能写这份清单"。
    它证不到那一层。要做到的话得加名字流分析（`p = <含清单路径的表达式>` 之后 `p.write_*`），
    那不在 T101 的射程里 ⇒ 这里把盲区钉成一条用例：
    哪天有人把扫描面扩到能抓它，本用例就会红，那时要改的是**判据的措辞**（连同
    `test_no_platform_module_writes_the_list_outside_the_registered_entry` 文档里那句限度），
    而不是偷偷让它是空判据。

    **T106 落地时补的一句**（措辞在这里改，判据本体不动）：盲区的确切形状是"变量名里没有
    `LIST_TOKEN` 那一段"，不是"凡是经过变量的写都看不见" —— 因为按调用点文本认，
    一个叫 `danger_list_file` 的变量照样命中。这正是下面允许名单能用起来的前提，
    也是 `acp/danger/__init__.py` 里那句"变量名带这一段不是风格"的注释的来处。
    代价要说白：一个想绕过名单的写者只要把变量改名为 `p` 就躲开了 ⇒ 这条判据挡的是
    **"顺手写错了地方"**，挡不住**"成心藏起来"**；成心那一路留给两份便宜读数兜 ——
    清单本体随仓（`git ls-files -- config/danger_list.yaml` 有输出），所以任何一次自扩都会
    出现在 `git diff` 里；以及人工复核（宪法原则 III：产出方不自审）。
    """
    indirect = 'p = Path("config") / "danger_list.yaml"\np.write_text("[]")\n'
    assert _mutating_calls_mentioning(indirect) == []


#: 清单写入面的允许名单（唯一一项 = 使用者侧扩展入口的 CLI 模块，owner 在 T101 就登记了这一格）。
#: **口径**：路径相对 `SRC_ROOT`（= 当前被 import 的那棵 `acp` 树），所以这一项的仓内全名是
#: `src/acp/danger/__init__.py`。**为什么必须是名单而不是"整个 src/acp 都算平台"**：
#: `python -m acp.danger add` 是 FR-058 的"使用者可扩展清单"这句话在仓内的唯一落点，
#: 它当然在 `src/` 下。名单的意义不在"放行"，在下面那条用例 —— 名单里每一项都得
#: **被扫描器真的看见**，否则名单是一条死条目，而"0 处违例"就退化成了"扫描面本来就空的"那种假绿。
LIST_WRITER_ALLOWLIST = frozenset({"danger/__init__.py"})

#: 运行期那一侧不许 import 使用者入口（见 `test_no_runtime_module_imports_the_user_entry`）。
DANGER_PACKAGE = "danger"


def _relative_source_map() -> dict[str, str]:
    """被 import 的那棵树里 `相对路径 -> 源码文本`（键相对 `SRC_ROOT`，与名单同口径）。"""
    return {
        str(path.relative_to(SRC_ROOT).as_posix()): path.read_text(encoding="utf-8")
        for path in sorted(SRC_ROOT.rglob("*.py"))
    }


def _writing_modules(sources: dict[str, str]) -> set[str]:
    """在给定源码集合里"写清单"的模块（相对路径）。"""
    return {name for name, text in sources.items() if _mutating_calls_mentioning(text)}


def test_no_platform_module_writes_the_list_outside_the_registered_entry() -> None:
    """清单的写入者 = 使用者（`plan.md` 设计项 9 / C5）⇒ `src/acp/` 下除名单外 0 处写它。

    这条守的是拦截判据的**独立性**：平台若能自扩清单，被拦的一方就能把自己移出清单，
    那与"审查岗不得改验收标准"（宪法原则 III）是同一条原则的失效形态。
    **限度点名**：AST 按调用点文本认，间接写入不在射程内（上面那条用例把它钉成了读数，不是散文）。
    T101 当时给这条留的前置义务（"T106 落笔时写入面必须在使用者入口那一侧，并且要显式登记"）
    现在就落在两处：名单里那一项，以及紧跟着那条"名单不是死条目"的用例。
    """
    offenders = _writing_modules(_relative_source_map()) - LIST_WRITER_ALLOWLIST
    assert offenders == set(), sorted(offenders)


def _dead_allowlist_entries(entries: set[str], sources: dict[str, str]) -> set[str]:
    """名单里"看不见写清单调用"的那些项（缺文件或只有读没有写都算死条目）。"""
    return {
        entry
        for entry in entries
        if entry not in sources or not _mutating_calls_mentioning(sources[entry])
    }


def test_the_registered_writer_is_a_write_the_scanner_actually_sees() -> None:
    """**名单的活性对照**：允许名单每一项都得同时满足"文件在 + 扫描器在它里面看见写清单的调用"。

    为什么单独钉这一条：T106 之后 `offenders == set()` 这个读数有两种成因 —— ① 真没别人写；
    ② 唯一那个写者把变量改名成 `p = ...`（落进盲区）而名单没人复核。①②在这一条上是**不同结果**：
    ② 会让"名单里这一项看不见命中"而红。少这一条，允许名单就是一条随时可以变成谎话的常量。
    """
    assert _dead_allowlist_entries(set(LIST_WRITER_ALLOWLIST), _relative_source_map()) == set()


def test_the_liveness_control_fires_on_an_entry_that_only_reads_the_list() -> None:
    """反向对照（判据自己会开火）：把一个"只读不写"的模块塞进名单，活性对照必须把它挑出来。

    诱饵挑 `selfcheck.py` 是有理由的：它确实**读**清单（`--show-danger` 打印指纹），
    所以"名单项存在即通过"那种弱化版本会放它过关 —— 那正是"悄悄放宽判据"的形状。
    这一条与上一条走的是**同一个谓词**，不是另写一遍检查。
    """
    sources = _relative_source_map()
    assert _dead_allowlist_entries({"selfcheck.py"}, sources) == {"selfcheck.py"}



def _danger_imports(source: str) -> list[str]:
    """一份源码里"import 使用者入口 `acp.danger`"的语句（命中即违例）。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hit = any(alias.name.split(".")[:2] == ["acp", DANGER_PACKAGE] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            hit = (
                node.level == 0 and parts[:2] == ["acp", DANGER_PACKAGE]
            ) or (
                # 包内相对写法（`from .danger import main` / `from ..danger import x`）：
                # 只按"最后一节是那个包名"认，不在这里重建包路径 —— 重建就是第二份口径。
                node.level > 0 and parts[:1] == [DANGER_PACKAGE]
            ) or (
                node.level == 0 and parts == ["acp"]
                and any(alias.name == DANGER_PACKAGE for alias in node.names)
            )
        else:
            continue
        if hit:
            found.append(ast.unparse(node))
    return found


def test_the_import_scanner_catches_each_way_of_reaching_the_user_entry() -> None:
    """**先证会开火**：四种写法各注入一次（绝对 import、绝对 from、包内相对、`from acp import danger`）。

    第四种最容易漏：它的 `module` 是 `acp`，包名藏在 `names` 里。漏了它，下面那条 0 命中
    就是"三种写法扫到了、第四种随便"的空判据。
    """
    for source in (
        "import acp.danger",
        "from acp.danger import main",
        "from .danger import main",
        "from acp import danger",
    ):
        assert _danger_imports(source), source


def test_no_runtime_module_imports_the_user_entry() -> None:
    """运行期那一侧 0 处 import `acp.danger` ⇒ "写入面在使用者入口"不只是一个文件名。

    为什么名单还不够：名单挡的是"别的模块自己写清单"，但 `from acp.danger import main`
    再被 `guard/` 调一次，效果与自写**完全一样**（拦的一方拿到了扩展入口），而写入面扫描
    看不见它 —— 命中点在 `acp/danger/` 里，不在调用方。这一条把那条路也钉死：
    **能调用 `main()` 的只有敲命令的人**（`__main__.py` 那一层，在名单包内）。
    **限度点名**：本判据是**静态 import**，挡不住 `importlib.import_module("acp.danger")`
    这种动态写法（那属名字流那一层，同上面的盲区）。
    """
    sources = {
        name: text
        for name, text in _relative_source_map().items()
        if f"/{DANGER_PACKAGE}/" not in f"/{name}"
    }
    offenders = {name for name, text in sources.items() if _danger_imports(text)}
    assert offenders == set(), sorted(offenders)



def test_an_error_about_a_pattern_never_echoes_a_credential_value(tmp_path: Path) -> None:
    """报错要回显 `pattern` 原文，而清单是可被人粘贴的自由文本 ⇒ 回显必须过脱敏。

    判据只用**形状**不用凭据字面量：这里的假键只以 `sk-…` 形态出现，
    断言的是"值没进消息、脱敏标记进了消息"两侧。
    **触发路径要点名**：这一条走的是"重复项"那条校验（`_shown()` 的调用点之一）。
    变异 P8-2（关掉重复项检查）会连带把它变绿，所以它不是独立的触发点 ——
    独立的第二条路在下面那条（解析失败路径），两条各钉一个回显面。
    """
    text = f"- pattern: {FAKE_KEY_SHAPE} rm -rf\n- pattern: '{FAKE_KEY_SHAPE} rm -rf'\n"
    with pytest.raises(DangerListError) as caught:
        load_danger_list(_write(tmp_path, text))
    message = str(caught.value)
    assert FAKE_KEY_SHAPE not in message
    assert "[REDACTED]" in message


def test_a_yaml_parse_error_does_not_leak_the_line_it_failed_on(tmp_path: Path) -> None:
    """解析失败那条回显的是 **PyYAML 自己摘的出错行原文**（另一条泄漏路径，与上面那条不同源）。

    为什么单独钉一条：本轮变异对照暴露出来的。`registry/loader.py` 那类调用点写的是
    `... : {exc}` 直插异常正文，而 `MarkedYAMLError` 的正文含 `>>>` 那一行原文 ——
    使用者往清单里粘了一把键、又刚好在那行写坏 YAML 语法，键就跟着 traceback 进终端与日志。
    这一条钉两件事：**消息被洗过**，且**因果链被断掉**（`__cause__` 为 None，
    否则原始异常那份未洗的正文还会出现在 traceback 里）。
    **诱饵要排在会被摘出来的那一行上**：PyYAML 的 mark 只摘"出错那一行"的原文，
    把假键写在别的行上它根本不会带出来（本轮实测第一版就是这样写的，那条断言恒真）。
    ⇒ 这里让语法错与假键**同一行**：未闭合的流序列 `[`。
    """
    text = f"- pattern: rm -rf\n- pattern: [unclosed {FAKE_KEY_SHAPE}\n"
    with pytest.raises(DangerListError) as caught:
        load_danger_list(_write(tmp_path, text))
    message = str(caught.value)
    assert FAKE_KEY_SHAPE not in message
    assert "[REDACTED]" in message
    assert "解析失败" in message
    assert caught.value.__cause__ is None
