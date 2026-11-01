"""笔 X（owner 2026-10-20 裁决 M2-W17）：全仓 `subprocess` 的解码口径只许一个载体，而且两头都不许漏。

**为什么要这一档**（owner 原话："这必须修！在 Windows (cp936) 下，不钉 encoding 随时会炸流"）：
本机区域编码是 cp936，而 `git` / `qodercli` / `wsl.exe` 这些外部程序写的不是 cp936（后一个是 UTF-16）。
`subprocess` 在**文本模式**下（`text=True`、`universal_newlines=True`，或给了 `encoding`）会用
`locale.getpreferredencoding()` 去解码读流线程；撞上不认识的字节时抛的不是 `CalledProcessError`
而是 `UnicodeDecodeError`，而且**位置在读流线程里**，于是有三种下场（三条都是本机实测，不是推的）：
- `stdout`/`stderr` 那一支变 `None`，诊断整条丢掉。2026-10-20 实测形状：`test_naming_guard.py` 那一发
  判据照旧绿，代价是它声称要对照的那个载体的 stderr 永远读不到，只留一条
  `PytestUnhandledThreadExceptionWarning`（探针读数 = `evidence/phase-15.md` §十六）；
- 在生产路径上直接冒出去，把一条本该是 `RUN FAIL :: code=…` 的拒绝顶成一段 traceback
  （2026-10-19 在 `realrun._run_git` 上实测过，落位见 `evidence/phase-15.md` §七）；
- 反过来，**父侧钉了 utf-8 而子进程按 cp936 写** ⇒ 读回来是 `\ufffd` 这样的替换串。这一发是本轮全量实测
  烧出来的（`test_recover_from_snapshot.py::test_the_child_sees_the_parent_environment` 红在带中文的
  仓库路径上），所以本文件有节 四专门管子进程那一头。

**这条判据量的是"漏"，也量"值"**：值不另写一份，权威载体 = `archive/cadence.py::run_git` 里那两个
字面量（一事实一载体；同族先例 = `test_realrun_driver.py::test_the_two_git_executors_decode_the_same_way`）；
子进程那一头的串由 `tests/conftest.py::CHILD_STDIO_SPEC` 承载，并由节 四拿去与同一份权威对读。

**证明到哪要点名**：扫描是 AST 级的，只保证"源码里没有漏"；运行时真按这个口径解码，由各执行器自己的
行为档管（git 那一发在 `test_realrun_driver.py`，CLI 那一发在 `test_qoder_cli_lookup.py`）。
**字节模式（没有文本化）的调用不在射程内** —— 给它们加 `encoding` 会把 `bytes` 变成 `str`、连带改掉
调用方的断言，那不是修漏而是引入缺陷（这一条下面有反面对照钉着）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "acp"
TESTS_ROOT = Path(__file__).resolve().parents[1]
CADENCE_FILE = SRC_ROOT / "archive" / "cadence.py"

#: `subprocess` 里会开子进程、且能传 `encoding` / `errors` 的那几个入口。
#: 不列 `getoutput` / `getstatusoutput`：它们返回 str 但**不接受** `encoding` 参数，
#: 把它们算进射程等于要求一件不可能的事（今天仓内零使用，复算见本文件末那条）。
SUBPROCESS_ENTRIES = frozenset({"run", "Popen", "check_output", "check_call", "call"})

#: 文本模式的三种写法（`encoding=` 单独出现也算：CPython 里它把 `text_mode` 一并打开）。
TEXT_MODE_KWARGS = ("text", "universal_newlines", "encoding")


def _imported_subprocess_names(source: str) -> set[str]:
    """这份源码里**直接**从 `subprocess` 引进来的函数名（`from subprocess import run` 那种写法）。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "subprocess":
            names |= {alias.name for alias in node.names}
    return names


def _entry_name(node: ast.Call, imported: set[str]) -> str | None:
    """这个调用节点是不是 `subprocess` 的入口，是的话报出入口名（两种写法都认）。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess":
        return func.attr if func.attr in SUBPROCESS_ENTRIES else None
    if isinstance(func, ast.Name) and func.id in imported:
        return func.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.keyword | None:
    return next((kw for kw in node.keywords if kw.arg == name), None)


def _literal_of(kw: ast.keyword) -> object:
    """只认字面量；非字面量交回这个哨兵 —— 口径要写死，`text=某个变量` 同样算漏。"""
    return kw.value.value if isinstance(kw.value, ast.Constant) else "=非字面量"


def _wants_text(node: ast.Call) -> bool:
    """三个写法里任意一个被打开就算文本模式；只有把 `text` 明写成 `False` 才算不。"""
    for name in TEXT_MODE_KWARGS:
        kw = _keyword(node, name)
        if kw is not None and _literal_of(kw) is not False:
            return True
    return False


def _authority_values() -> tuple[str, str]:
    """权威口径 = `archive/cadence.py::run_git` 里钉的那两个字面量，AST 现取，不复制。

    为什么只许有一份：写死 `"utf-8"` / `"replace"` 就是第二份事实 —— `cadence` 哪天换口径，本文件这些
    判据照样绿（"两边都还是 utf-8"而权威那份已经走了 ⇒ 假绿）。找不到、或找到的不是字面量，都算这条红。
    """
    authority: tuple[object, object] | None = None
    for node in ast.walk(ast.parse(CADENCE_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "run_git":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "run":
                    authority = (
                        _literal_of(_keyword(inner, "encoding")),
                        _literal_of(_keyword(inner, "errors")),
                    )
    assert authority is not None, "`cadence.run_git` 里没有 `subprocess.run(...)` ⇒ 权威载体挪了地方，这条要跟着改准"
    assert all(isinstance(item, str) for item in authority), f"权威载体不是字面量：{authority}"
    return authority  # type: ignore[return-value]


def _python_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts]


def _calls_of(path: Path) -> list[tuple[Path, int, str, ast.Call]]:
    source = path.read_text(encoding="utf-8")
    imported = _imported_subprocess_names(source)
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            entry = _entry_name(node, imported)
            if entry is not None:
                out.append((path, node.lineno, entry, node))
    return out


def _calls(root: Path) -> list[tuple[Path, int, str, ast.Call]]:
    return [item for path in _python_files(root) for item in _calls_of(path)]


def text_mode_violations(root: Path, *, require: tuple[str, ...] = ("encoding", "errors")) -> list[str]:
    """`root` 下所有**文本模式**却没钉全 `require` 那几个关键字的调用，报 `文件:行:入口:缺什么`。"""
    return violations_in_files(_python_files(root), root, require=require)


def violations_in_files(
    files: list[Path], anchor: Path, *, require: tuple[str, ...] = ("encoding", "errors")
) -> list[str]:
    """同一套判定，但把"扫哪些文件"交回调用方 —— 用于扫描根**之外**那份补集。"""
    found: list[str] = []
    for path in files:
        for _, lineno, entry, node in _calls_of(path):
            if not _wants_text(node):
                continue
            missing = [name for name in require if _keyword(node, name) is None]
            if missing:
                try:
                    label = path.relative_to(anchor).as_posix()
                except ValueError:
                    label = path.name
                found.append(f"{label}:{lineno}:{entry}:缺 {'+'.join(missing)}")
    return found


def decode_settings(root: Path) -> dict[str, tuple[object, object]]:
    """每个钉了 `encoding` 的调用实际钉上的 `(encoding, errors)`（非字面量原样报出来，不粉饰）。"""
    settings: dict[str, tuple[object, object]] = {}
    for path, lineno, entry, node in _calls(root):
        encoding_kw = _keyword(node, "encoding")
        if encoding_kw is None:
            continue
        errors_kw = _keyword(node, "errors")
        settings[f"{path.relative_to(root).as_posix()}:{lineno}:{entry}"] = (
            _literal_of(encoding_kw),
            _literal_of(errors_kw) if errors_kw is not None else None,
        )
    return settings


# ------------------------------------------------ 一、控制件：先证扫描器会开火，再谈它报的那个 0


DECOYS = {
    "a_plain.py": "import subprocess\nsubprocess.run(['x'], text=True, capture_output=True)\n",
    "b_univ.py": "import subprocess\nsubprocess.Popen(['x'], universal_newlines=True)\n",
    "c_bare_import.py": "from subprocess import run\nrun(['x'], text=True)\n",
    "d_encoding_only.py": "import subprocess\nsubprocess.check_output(['x'], encoding='utf-8')\n",
    "e_nonliteral_flag.py": "import subprocess\nMODE = True\nsubprocess.run(['x'], text=MODE)\n",
}
#: 反面对照：字节模式（没文本化）⇒ 不解码 ⇒ 不在射程内。
BYTES_MODE = "import subprocess\nsubprocess.run(['x'], capture_output=True, check=False)\n"
#: 正面对照：两个都钉上 ⇒ 不该被报。
ALREADY_PINNED = "import subprocess\nsubprocess.run(['x'], text=True, encoding='utf-8', errors='replace')\n"


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_the_scanner_sees_a_violation_in_every_shape_it_claims_to_cover(tmp_path):
    """五种"文本模式但没钉全"的写法逐条必须被抓到，而钉全的那一种与字节模式那一种不许被抓。

    扫描面类判据最常见的失效形态是"它什么都看不见却报 0" ⇒ 这里把每一种写法真的写进临时目录再读差集，
    而不是只断一个总数。`c_bare_import.py` 那一发挡的是"只认 `subprocess.run` 这种带模块前缀的写法"，
    `e_nonliteral_flag.py` 那一发挡的是"把 `text=变量` 当没看见"。
    """
    for name, body in DECOYS.items():
        _write(tmp_path, name, body)
    hits = text_mode_violations(tmp_path)
    assert {hit.split(":")[0] for hit in hits} == set(DECOYS), f"该抓的没抓全：{hits}"
    assert all("缺 " in hit for hit in hits), hits

    _write(tmp_path, "f_ok.py", ALREADY_PINNED)
    _write(tmp_path, "g_bytes/h.py", BYTES_MODE)
    hits = text_mode_violations(tmp_path)
    assert not any("f_ok.py" in hit for hit in hits), f"钉全的那一发被误报：{hits}"
    assert not any("h.py" in hit for hit in hits), f"字节模式被误报 ⇒ 判据写宽了，将来会逼人去给二进制调用加编码：{hits}"


def test_the_scanner_walks_a_nested_tree_and_skips_bytecode(tmp_path):
    """目录要能递归（真仓的 `src/acp/**` 与 `tests/**` 都是嵌套的），而 `__pycache__` 不算源码。

    诱饵放在**两层深的子目录**里：只扫顶层的实现在真仓上会一片绿却是假的。`.pyc` 那一支是反向的：
    缓存里那份过期源码不许变成一条违例（它会让人以为"修了文件但没修"，而实际只需重跑）。
    """
    _write(tmp_path, "pkg/sub/deep.py", DECOYS["a_plain.py"])
    assert [hit.split(":")[0] for hit in text_mode_violations(tmp_path)] == ["pkg/sub/deep.py"]

    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "stale.py").write_text(DECOYS["b_univ.py"], encoding="utf-8")
    assert all("__pycache__" not in hit for hit in text_mode_violations(tmp_path))


# --------------------------------------------------------------------- 二、正题：本仓现在一个漏都不许有


def test_no_text_mode_subprocess_call_in_the_repo_lacks_the_decode_carrier():
    """全仓（`src/acp/**` + `tests/**`）文本模式的 `subprocess` 调用 MUST 同时钉 `encoding` 与 `errors`。

    这一条就是 owner 那句"补一条静态全仓扫描判据，防再漏"的落点：**任何人**今后新写一发
    `subprocess.run(..., text=True)` 忘了钉编码，这条当场红，不用再等一次 cp936 上的偶然炸流。
    失败消息逐条点名"哪个文件哪一行的哪个入口缺哪个关键字"，直接照着补就行。
    """
    violations = [*text_mode_violations(SRC_ROOT), *text_mode_violations(TESTS_ROOT)]
    assert violations == [], (
        "这些文本模式的 subprocess 调用没钉解码口径（cp936 机器上会炸读流线程）：\n" + "\n".join(violations)
    )


def test_the_pinned_values_are_the_one_carrier_in_archive_cadence():
    """钉上的**值**必须等于 `archive/cadence.py::run_git` 那两个 —— 口径只有一个，执行器有多个。

    为什么不在这条里写死 `"utf-8"` / `"replace"`：写死就是第二份事实，`cadence` 哪天换口径这边照样绿
    （"两边都还是 utf-8"而权威那份已经走了 ⇒ 假绿）。权威那份由 `_authority_values()` 现取，本文件只用它一次。
    非字面量（哨兵）与缺 `errors`（`None`）都算不合格。
    """
    authority = _authority_values()

    for label, root in (("src", SRC_ROOT), ("tests", TESTS_ROOT)):
        settings = decode_settings(root)
        assert settings, f"{label} 里一个钉了 encoding 的 subprocess 调用都没有 ⇒ 扫描器或写法变了"
        for site, values in settings.items():
            assert values == authority, f"{label} 的 {site} 钉的是 {values}，对不上 `cadence.run_git` 的 {authority}"


def test_no_call_uses_an_entry_that_cannot_take_the_carrier():
    """`getoutput` / `getstatusoutput` 之所以被排除在射程外，是因为它们**不接受** `encoding` 参数。

    这是一条"排除项也要有理由、也要有人守着"的对照：今天全仓零使用（本条现跑 = 0）；哪天真出现一发，
    这条当场红，逼着改的人回来把口径定清楚（要么换成 `run(..., text=True, encoding=…)`，要么明写
    那一支不在防漏范围内并改这条），而不是让扫描器长期放过一个它自己抓不住的面。
    """
    forbidden = ("getoutput", "getstatusoutput")
    hits: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and (node.module or "") == "subprocess"
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                        and func.value.id == "subprocess" and func.attr in forbidden) or (
                        isinstance(func, ast.Name) and func.id in imported):
                    hits.append(f"{path.relative_to(root).as_posix()}:{node.lineno}:{func.attr if isinstance(func, ast.Attribute) else func.id}")
    assert hits == [], f"出现了不接受 encoding 入口的用法，得先定口径再谈防漏：{hits}"


# --------------------------------------------------------------- 三、射程边界：两个根就是"全仓"这件事也要有发


def _python_files_outside(root: Path, roots: tuple[Path, ...]) -> list[Path]:
    """`root` 下、不落在 `roots` 里、也不落在缓存与产物目录里的 `*.py`。

    跳过名单是**点名**的（`.git*`、`__pycache__`、`node_modules`、`results`）：`results/` 是存档根，
    里面是别人跑出来的产物仓库，不是本仓源码；把它算进扫描面就会让门禁去扫库外的东西（同
    `config/params-ref.md §7` 那条 `--exclude-dir` 的口径）。
    """
    inside = {path.resolve() for target in roots for path in _python_files(target)}
    skipped = ("node_modules", "__pycache__", "results")
    out: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts[:-1]
        if any(part in skipped or part.startswith(".") for part in parts):
            continue
        if path.resolve() not in inside:
            out.append(path)
    return out


def test_the_outside_walker_is_not_blind(tmp_path):
    """先把这个"根之外"的枚举器自己验一遍活性：它说"没有"必须是因为真没有，不是因为没走到。

    没有这一发，下面那条"补集里零 subprocess"就成了一个恒真的空断言（补集算错成空集 ⇒ 永远绿）。
    诱饵分五处放：根外顶层、根外两层深的子目录、根**内**、`results/` 产物目录、`__pycache__/` ——
    只有前两处该出现在补集里。
    """
    _write(tmp_path, "a_top.py", DECOYS["a_plain.py"])
    _write(tmp_path, "pkg/sub/b_deep.py", DECOYS["b_univ.py"])
    _write(tmp_path, "src/acp/c_inside.py", DECOYS["a_plain.py"])
    _write(tmp_path, "results/d_artifact.py", DECOYS["a_plain.py"])
    _write(tmp_path, "__pycache__/e_cache.py", DECOYS["a_plain.py"])

    names = [
        path.relative_to(tmp_path).as_posix()
        for path in _python_files_outside(tmp_path, (tmp_path / "src" / "acp",))
    ]
    assert names == ["a_top.py", "pkg/sub/b_deep.py"], names


def test_the_two_roots_are_the_whole_python_surface():
    """本仓的 python 面 = `src/acp/**` + `tests/**`，根外那几份（现读只 1 份，见下）MUST 一个 subprocess 入口都不用。

    为什么要有这一条：owner 那句是"全仓扫描"，而扫描器只吃两个根 ⇒ "全仓"这两个字得由这条补上另一半，
    否则 `scripts/` 里哪天出现一发不钉编码的调用，这条防线就是**看起来有、实际没有**。
    活性由上面那条管；这条报的是补集里到底有什么，失败消息直接把文件点名。
    """
    repo = Path(__file__).resolve().parents[2]
    outside = _python_files_outside(repo, (SRC_ROOT, TESTS_ROOT))
    offenders = violations_in_files(outside, repo)
    assert offenders == [], f"这两个根之外出现了没钉解码口径的 subprocess 调用，扫描面要一起扩：{offenders}"
    assert len(outside) <= 4, (
        f"根外的 python 文件已经涨到 {len(outside)} 份：{[p.relative_to(repo).as_posix() for p in outside]} "
        "⇒ 要么把它们纳入扫描根，要么在这里点名为什么不管"
    )


# --------------------------------------------- 四、口径的另一半天：子进程那一头也得按同一个编码**写**

#: 子进程侧的载体：环境变量名，加上"通过它带进来"允许的两个名字（conftest 的常量、恢复档那个助手）。
#: 允许名字不等于放过它们 —— 下面 `test_the_child_env_helper_really_carries_the_spec` 专门核 `_child_env()`
#: 的 return 里真的并了那份常量，否则"用一个函数名"就成了第二个口径的藏身处。
CHILD_STDIO_ENV_VAR = "PYTHONIOENCODING"
CHILD_STDIO_CARRIERS = ("PYTHONIOENCODING", "CHILD_STDIO_ENV", "_child_env")
CONFTEST_FILE = TESTS_ROOT / "conftest.py"


def _spawns_local_interpreter(node: ast.Call) -> bool:
    """argv 第一格点到 `sys.executable` ⇒ 这个子进程是**本机另一个 Python 解释器**。

    只有它有"写侧编码跟着区域编码走"这件事：`git` / `bash` / `node` / `qodercli` 这些外部程序自己
    写 UTF-8（或干脆不写文本），给它们塞 `PYTHONIOENCODING` 是无效动作，所以它们不在这一节的射程里。
    """
    return bool(node.args) and "sys.executable" in ast.unparse(node.args[0])


def child_side_gaps(root: Path) -> list[str]:
    """父进程按文本读、子进程是本机解释器、却没钉子进程写入口径的那些调用。"""
    found: list[str] = []
    for path, lineno, entry, node in _calls(root):
        if _keyword(node, "encoding") is None or not _spawns_local_interpreter(node):
            continue
        env_kw = _keyword(node, "env")
        blob = ast.unparse(env_kw.value) if env_kw is not None else ""
        if not any(carrier in blob for carrier in CHILD_STDIO_CARRIERS):
            label = path.relative_to(root).as_posix()
            found.append(f"{label}:{lineno}:{entry}:子进程没钉 {CHILD_STDIO_ENV_VAR}")
    return found


CHILD_DECOYS = {
    "a_gap_no_env.py": (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'x'], capture_output=True, text=True,\n"
        "                 encoding='utf-8', errors='replace')\n"
    ),
    "b_gap_plain_env.py": (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'x'], capture_output=True, text=True,\n"
        "                 encoding='utf-8', errors='replace', env={'PATH': '/bin'})\n"
    ),
    "c_ok_literal_env.py": (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'x'], capture_output=True, text=True,\n"
        "                 encoding='utf-8', errors='replace',\n"
        "                 env={'PYTHONIOENCODING': 'utf-8:replace'})\n"
    ),
    "d_ok_carrier_env.py": (
        "import subprocess, sys\n"
        "from conftest import CHILD_STDIO_ENV\n"
        "subprocess.run([sys.executable, '-c', 'x'], capture_output=True, text=True,\n"
        "                 encoding='utf-8', errors='replace', env={**os.environ, **CHILD_STDIO_ENV})\n"
    ),
    "e_not_a_python_child.py": (
        "import subprocess\n"
        "subprocess.run(['git', 'status'], capture_output=True, text=True,\n"
        "                 encoding='utf-8', errors='replace')\n"
    ),
    "f_bytes_child.py": (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'x'], capture_output=True, env={'PATH': '/bin'})\n"
    ),
}


def test_the_child_side_rule_fires_only_on_the_shape_it_claims(tmp_path):
    """六种形态逐条点名：两种该红、四种不该红，而每条"不该红"各有各的理由。

    `e_not_a_python_child.py` 是**反向对照**：子进程是 `git` 时这条规则必须闭嘴 —— 允许名单类判据最常见的
    过度收紧就是把不适用的一侧也拦下来，逼人往 `git` 调用里塞一个作用都不起的键。
    `f_bytes_child.py` 是另一半：父进程不按文本读，就没有"读回中文"这件事可钉，也不许报。
    """
    for name, body in CHILD_DECOYS.items():
        _write(tmp_path, name, body)
    hits = child_side_gaps(tmp_path)
    assert [hit.split(":")[0] for hit in hits] == ["a_gap_no_env.py", "b_gap_plain_env.py"], hits
    assert all(CHILD_STDIO_ENV_VAR in hit for hit in hits), hits


def test_no_interpreter_child_in_the_repo_writes_with_a_different_codec():
    """本仓每一发"起本机解释器再按文本读回来"的调用，MUST 同时把子进程那侧的写入口径钉上。

    为什么父侧钉了还不够（这一发是 2026-10-20 全量实测烧出来的，不是推的）：本机区域编码 cp936，
    子进程写中文时按 cp936 进管道，父进程按 `encoding="utf-8"` 读 ⇒ 中文原样变成 `\\ufffd`。
    撞上的那一发是 `test_recover_from_snapshot.py::test_the_child_sees_the_parent_environment`：
    它比的正是**带中文的仓库路径**，于是红在"路径读回来是乱码"上，而红因与要证的事（env 有没有继承）无关。
    """
    gaps = [*child_side_gaps(SRC_ROOT), *child_side_gaps(TESTS_ROOT)]
    assert gaps == [], (
        "这些调用起了本机解释器并按 utf-8 读，却没钉子进程那侧的写入口径：\n" + "\n".join(gaps)
    )


def test_the_helper_accepted_as_a_carrier_really_carries_the_spec():
    """`_child_env()` 被允许当载体 ⇒ 就必须核它的 return 里真的并了 `CHILD_STDIO_ENV`，而且不自己落一份串。

    允许名单类判据最省事也最坏的形态就是"认名字不认内容"：名单里放一个函数名，函数哪天把那一格删掉，
    调用点照样"合格"。这一发就是那条漏的封口 —— 它同时是反向对照：把 `_child_env` 里那份合并改掉，
    这条红（而上一节那条"全仓零 gap"仍绿，两发各管一头）。
    """
    source = (TESTS_ROOT / "guards" / "test_recover_from_snapshot.py").read_text(encoding="utf-8")
    helper = next(
        (
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_child_env"
        ),
        None,
    )
    assert helper is not None, "`_child_env()` 不在了 ⇒ 这一族的载体换了地方，这条要跟着改准"
    # 只看 return 那一行：docstring 里本来就该解释这个变量名，把它当"又落了一份口径"是误报。
    returns = [
        ast.get_source_segment(source, node) or "" for node in helper.body if isinstance(node, ast.Return)
    ]
    assert len(returns) == 1, f"`_child_env()` 的 return 形状变了（现读 {len(returns)} 条）⇒ 这条要跟着改准"
    assert CHILD_STDIO_CARRIERS[1] in returns[0], f"return 里没有合并 conftest 那份常量：{returns[0]!r}"
    assert CHILD_STDIO_ENV_VAR not in returns[0], (
        f"`_child_env()` 自己又写了一遍环境变量名 ⇒ 口径落成第二份，值漂了没人管：{returns[0]!r}"
    )


def _conftest_assign(name: str) -> ast.expr:
    """取 `conftest.py` 里那个模块级常量的**值节点**（不 import 它 —— 那份文件是 pytest 插件面）。"""
    tree = ast.parse(CONFTEST_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    raise AssertionError(f"`conftest.py` 里找不到模块级 {name} ⇒ 载体挪了地方，这条要跟着改准")


def test_the_child_side_spec_is_the_same_reading_as_the_parent_side_carrier():
    """`conftest.CHILD_STDIO_SPEC` 的两半必须等于 `cadence.run_git` 钉的那两个字面量 —— 口径还是一个。

    为什么不在这条里写死 `"utf-8:replace"`：那份串在这里再落一份就是第二份事实，父侧哪天换口径这条照样绿。
    父侧的值从 `run_git` 现取（与本文件节 二 共用同一个权威），子侧的值从 `conftest` 现取，比的是**值**；
    `CHILD_STDIO_ENV` 那一格也必须指向同一个串（它的值是 `CHILD_STDIO_SPEC` 这个名字，不是第二份字面量 ——
    所以这里核的是"键名 + 指向的名字"，而不是把 dict 求值成对象，那样会把间接层看丢）。
    """
    spec_node = _conftest_assign("CHILD_STDIO_SPEC")
    assert isinstance(spec_node, ast.Constant) and isinstance(spec_node.value, str), ast.dump(spec_node)
    spec = spec_node.value
    env_node = _conftest_assign("CHILD_STDIO_ENV")
    assert isinstance(env_node, ast.Dict) and len(env_node.keys) == 1, ast.dump(env_node)
    key, value = env_node.keys[0], env_node.values[0]
    assert isinstance(key, ast.Constant) and key.value == CHILD_STDIO_ENV_VAR, ast.dump(key)
    assert isinstance(value, ast.Name) and value.id == "CHILD_STDIO_SPEC", (
        f"`CHILD_STDIO_ENV` 里那一格没有指向 `CHILD_STDIO_SPEC` 而是 {ast.dump(value)} ⇒ 又落了一份口径"
    )
    encoding, _, errors = spec.partition(":")
    authority = _authority_values()
    assert (encoding, errors) == authority, (
        f"子进程写入口径 {encoding!r}/{errors!r} 对不上父进程权威载体 {authority[0]!r}/{authority[1]!r}"
        " ⇒ 两侧各解各的，带中文的路径这类读数会漂"
    )

