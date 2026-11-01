"""笔 W + 2026-10-24 CN 切换：`qodercli` / `qoderclicn` 这两个**名字**在这台机器上到底是谁。

为什么这一档要单独立一个文件：`tests/contract/test_qoder_transport.py` 的口径是"全程 `FakeRunner`、
不起真进程、与真机的安装状态无关"，而这里要量的**恰恰是真机的安装状态**（owner 2026-10-20 那一发的
根因就是"包装了、CLI 也装了，但只躺在包内 `_bundled`，PATH 上什么都没有 ⇒ `Popen(['qodercli', …])`
当场 `FileNotFoundError`（WinError 2），抛的位置在 `realrun.SWALLOWABLE` 之外 ⇒ 一屏 traceback 顶掉了
`RUN FAIL`，而厂商侧一个字都没发生"）。把两档混在一个文件里，前一档的"与真机无关"就先被弄破了。

**2026-10-24 owner 裁决后的顺序（本文件量的就是它）**：
① `QODERCLI_PATH`（原样交回）→ ② CN 默认安装位 `~/.qoder-cn/bin/qoderclicn/qoderclicn(.exe)` →
③ PATH 上的 `qoderclicn`（原生优先）→ ④ PATH 上的 `qodercn`（本机是 `.cmd` dispatcher，最后手段，
用上了要 warning）→ ⑤ INTL 包内 `_bundled/qodercli` → ⑥ PATH 上的 `qodercli` / `qoder`。
CN 档全空而 INTL 档命中 ⇒ `ResolvedCli.fallback_from == "cn"`，runner 打一条 `logging.warning`
（owner 裁决 3：只走日志，绝不 print）。

**这一档证明到哪，逐条点名**：
- 顺序（①~⑥）与"找不到时那条消息写了什么"= 注入位上可全验，不靠真机；**每条都要注入空的临时 home**，
  否则本机真装的 CN 会抢先命中，判据就变成"看机器脸色"；
- 与厂商 SDK 的**符合性**（两个品牌的 `CLI_COMMANDS`、`.exe` 规则、INTL 三档的先后）= 读本机装的
  这份包的文件来比，它证的是"我们抄的顺序与它一致"，不证"它的顺序是对的"；
- **活性档**（`test_the_real_cli_on_this_machine_starts…`）= 不 mock 任何一环，用生产 `Popen` 真起一发
  `--version`，按解析出的 flavor 对读版本号（CN 1.1.78 / INTL 1.1.61）。它不出网（只打印版本号），
  所以不违反成本纪律 5；除它之外本文件没有第二发起进程的判据。
- 仍未验（丙档，冒烟轮次的事）：CN CLI 接受我们那套单向 `--print` + `stream-json` 旗标、CN 侧的鉴权名
  到底是 `QODERCN_PERSONAL_ACCESS_TOKEN` 还是 `QODER_PERSONAL_ACCESS_TOKEN` —— `--version` 只证明
  "这个文件是个能起的程序"。
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from acp.adapters.qoder import transport as qoder_transport
from acp.adapters.qoder.transport import (
    BUNDLED_DIR_NAME,
    CLI_BINARY,
    CLI_PATH_ENV,
    CN_CLI_BINARY,
    CN_CLI_COMMAND_NAMES,
    CN_CLI_VERSION_EXPECTED,
    CN_CLI_WRAPPER,
    CN_CONFIG_DIR_NAME,
    CN_FLAVOR,
    IMPORT_NAME,
    INTL_CLI_BINARY,
    INTL_CLI_COMMAND_NAMES,
    INTL_CLI_VERSION_EXPECTED,
    INTL_FLAVOR,
    PACKAGE_NAME,
    QoderTransportError,
    ResolvedCli,
    SubprocessRunner,
    bundled_cli_dir,
    resolve_cli,
    resolve_cli_executable,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "acp"
TRANSPORT_FILE = SRC_ROOT / "adapters" / "qoder" / "transport.py"
CADENCE_FILE = SRC_ROOT / "archive" / "cadence.py"

#: 消息里出现这些就算泄漏面：值形态的东西一律不许进拒绝消息（口径同 `QoderTransportError` 的类文档）。
FORBIDDEN_IN_REFUSAL = (
    "QODERCN_PERSONAL_ACCESS_TOKEN=",
    "QODER_PERSONAL_ACCESS_TOKEN=",
    "QODER_SERVICE_ACCOUNT_KEY=",
    "sk-",
)


def _call_keywords(source: str, func_name: str, attr: str) -> dict[str, object]:
    """取 `func_name` 里那一次 `.<attr>(…)` 调用的关键字面量。口径同 `test_realrun_driver._call_keywords`。

    单独 parse 而不是 grep：散文与消息文本也会命中 `encoding` 这个词，命中数不等于调用点。
    同名函数可以有几个（`CliRunner.run` 那份是 Protocol 的声明，里没有调用）⇒ 逐个试，
    一个都没有才抛：那等于这条判据盯的形状已经不在了。
    非字面量交回 `None` 哨兵，**不是把值吞成某个假值**：所以"两边都变成变量"这种改法会让两个 `None` 相等而
    骗过比较 —— 下面那条符合性判据因此除比值之外还核"两侧都是字符串字面量"（`encoding` 换成变量就等于
    回到各机的区域编码，那正是这一族判据要挡的东西）。
    """
    tried = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            tried += 1
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == attr:
                    return {
                        kw.arg: (kw.value.value if isinstance(kw.value, ast.Constant) else None)
                        for kw in inner.keywords
                        if kw.arg is not None
                    }
    raise AssertionError(
        f"源文件里叫 {func_name!r} 的函数有 {tried} 个，没有一个含 .{attr}(...) 这一发 ⇒ 形状已改，判据跟着改准"
    )


def _marker_order(source: str, func_name: str, markers: tuple[str, ...]) -> list[int]:
    """`func_name` 那段**代码**（不含 docstring）里几个 marker 各自第一次出现的偏移。

    用来量"查找的先后"这种东西：顺序在源码文本上是可数的，不需要真去跑一遍才知道谁先谁后。
    剥 docstring 是必须的：两边的说明段都会提到 `QODERCLI_PATH` 这些名字，不剥就是把散文当代码。
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = (ast.get_source_segment(source, node) or "").splitlines()
            skip = 1  # `def` 那一行
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                skip = first.end_lineno - node.lineno + 1
            code = "\n".join(lines[skip:])
            hits = []
            for marker in markers:
                offset = code.find(marker)
                assert offset >= 0, f"`{func_name}` 的代码里找不到 {marker!r} ⇒ 这一档的形状变了，判据要跟着改准"
                hits.append(offset)
            return hits
    raise AssertionError(f"源文件里没有函数 {func_name!r}")


class FakeWhich:
    """`shutil.which` 的替身：记下被问的名字，按名单外不给值。"""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table
        self.asked: list[str] = []

    def __call__(self, command: str) -> str | None:
        self.asked.append(command)
        return self.table.get(command)


def bundled(tmp_path: Path, *names: str) -> Path:
    """在 `tmp_path` 下摆出一个"包目录 + `_bundled` + 这些文件名"的形状，交回那个包目录。"""
    root = tmp_path / "site-packages" / IMPORT_NAME
    directory = root / BUNDLED_DIR_NAME
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_bytes(b"MZ")
    return root


def cn_install(tmp_path: Path, *, home_name: str = "home") -> tuple[Path, Path]:
    """摆出"CN 默认安装位 + 原生 exe"：`<home>/.qoder-cn/bin/qoderclicn/qoderclicn.exe`。"""
    home = tmp_path / home_name
    directory = home / CN_CONFIG_DIR_NAME / "bin" / CN_CLI_BINARY
    directory.mkdir(parents=True)
    exe = directory / f"{CN_CLI_BINARY}.exe"
    exe.write_bytes(b"MZ")
    return home, exe


def empty_home(tmp_path: Path, *, home_name: str = "empty-home") -> Path:
    """一个**没有 CN 安装位**的临时 home。旧判据都要注入它，否则会被本机真装的 CN 抢先命中。"""
    home = tmp_path / home_name
    home.mkdir(parents=True)
    return home


# --------------------------------------------------- 一、顺序（注入位上验；CN 优先）


def test_the_env_tier_wins_and_is_handed_over_verbatim(tmp_path):
    """① `QODERCLI_PATH` 有值 ⇒ 就交回那一个值，**原样**、不校验存在性、不看后面几档。

    诱饵是这里的关键：CN 默认安装位与 INTL `_bundled` 里都摆一个真存在的文件。实现要是
    "先验一下存在性，不在就往下找"，返回值会变成其中一个 ⇒ 这条红。厂商 `subprocess_cli.py:186`
    就是原样交回，我们跟着同形（不同形就等于自造第二套查找规则，两处会漂）。
    """
    cn_home, _ = cn_install(tmp_path)
    package = bundled(tmp_path, f"{INTL_CLI_BINARY}.exe")
    which = FakeWhich({})
    got = resolve_cli_executable(
        env={CLI_PATH_ENV: "D:/somewhere/没有这个文件"},
        package_dir=package,
        which=which,
        system_name="Windows",
        home=cn_home,
    )
    assert got == "D:/somewhere/没有这个文件", got
    assert which.asked == [], "① 命中了还去查 PATH ⇒ 顺序不是那一张顺序"


def test_the_cn_install_dir_beats_the_intl_bundled_cli(tmp_path):
    """② CN 默认安装位命中 ⇒ 交回那个原生 exe，不看 INTL 包内、不问 PATH。

    这是 owner 2026-10-24 裁决的主目标：本机把国际版 SDK 装着、PATH 上什么都没有时，
    CN 仍然必须先被找到（否则整个切换在"SDK 还在"的机器上根本不生效）。
    """
    cn_home, cn_exe = cn_install(tmp_path)
    package = bundled(tmp_path, f"{INTL_CLI_BINARY}.exe")
    which = FakeWhich(
        {INTL_CLI_BINARY: "D:/on-path/qodercli.exe", CN_CLI_BINARY: "D:/on-path/qoderclicn.exe"}
    )
    got = resolve_cli(env={}, package_dir=package, which=which, system_name="Windows", home=cn_home)
    assert got.executable == str(cn_exe), got
    assert got.flavor == CN_FLAVOR and got.fallback_from is None, got
    assert which.asked == [], "CN 安装位命中后还问 PATH = 同一发两个答案，谁覆盖谁说不清"


def test_cn_path_is_tried_after_the_install_dir(tmp_path):
    """安装位为空 ⇒ 按 `CN_CLI_COMMAND_NAMES` 问 PATH；命中就是 CN，不问 INTL。"""
    home = empty_home(tmp_path)
    package = bundled(tmp_path, f"{INTL_CLI_BINARY}.exe")
    which = FakeWhich(
        {CN_CLI_BINARY: "D:/on-path/qoderclicn.exe", INTL_CLI_BINARY: "D:/on-path/qodercli.exe"}
    )
    got = resolve_cli(env={}, package_dir=package, which=which, system_name="Windows", home=home)
    assert got.executable == "D:/on-path/qoderclicn.exe", got
    assert got.flavor == CN_FLAVOR and got.fallback_from is None, got
    assert which.asked == [CN_CLI_BINARY], which.asked


def test_the_cn_wrapper_is_only_the_last_cn_candidate(tmp_path):
    """原生 `qoderclicn` 不在 PATH 上、`qodercn` 在 ⇒ 用包装器；原生在 ⇒ 包装器不许被选中。

    本机实测 `qodercn` 是 `.cmd`（PowerShell dispatcher）⇒ 它是**最后手段**，不是首选
    （owner 2026-10-24 裁决 2）。第二条对照是"首选不许被降级"：原生在 PATH 上时问都不许问包装器。
    """
    home = empty_home(tmp_path)
    package = bundled(tmp_path, f"{INTL_CLI_BINARY}.exe")
    only_wrapper = FakeWhich({CN_CLI_WRAPPER: "D:/on-path/qodercn.cmd"})
    got = resolve_cli(env={}, package_dir=package, which=only_wrapper, system_name="Windows", home=home)
    assert got.executable == "D:/on-path/qodercn.cmd", got
    assert got.flavor == CN_FLAVOR, got
    assert only_wrapper.asked == [CN_CLI_BINARY, CN_CLI_WRAPPER], only_wrapper.asked

    both = FakeWhich(
        {CN_CLI_BINARY: "D:/on-path/qoderclicn.exe", CN_CLI_WRAPPER: "D:/on-path/qodercn.cmd"}
    )
    got2 = resolve_cli(env={}, package_dir=package, which=both, system_name="Windows", home=home)
    assert got2.executable == "D:/on-path/qoderclicn.exe", got2
    assert both.asked == [CN_CLI_BINARY], "原生在 PATH 上时又去看包装器 ⇒ 首选被降级"


def test_intl_bundled_is_the_fallback_when_cn_is_absent(tmp_path):
    """CN 档全空 ⇒ 回退 INTL `_bundled`，且 `fallback_from` 记下"是从 CN 降来的"。"""
    home = empty_home(tmp_path)
    package = bundled(tmp_path, f"{INTL_CLI_BINARY}.exe")
    which = FakeWhich({INTL_CLI_BINARY: "D:/on-path/qodercli.exe"})
    got = resolve_cli(env={}, package_dir=package, which=which, system_name="Windows", home=home)
    assert got.executable == str(package / BUNDLED_DIR_NAME / f"{INTL_CLI_BINARY}.exe"), got
    assert got.flavor == INTL_FLAVOR and got.fallback_from == CN_FLAVOR, got
    assert which.asked == [CN_CLI_BINARY, CN_CLI_WRAPPER], "CN 两档都问过才轮到 INTL"


def test_intl_path_is_the_last_tier_and_tries_every_command_name(tmp_path):
    """前四档都空 ⇒ 按 `INTL_CLI_COMMAND_NAMES` **逐个**问 PATH。

    诱饵排在第二个名字上：实现只认第一个（`qodercli`）就会返回 None 那一支、当场抛 ⇒ 这条红。
    这条同时是"名单不是单名"的反证，所以断言里连问的顺序一起断（CN 两档 + INTL 两档）。
    """
    home = empty_home(tmp_path)
    which = FakeWhich({"qoder": "/usr/local/bin/qoder"})
    got = resolve_cli(env={}, package_dir=bundled(tmp_path), which=which, system_name="Linux", home=home)
    assert got.executable == "/usr/local/bin/qoder", got
    assert got.flavor == INTL_FLAVOR and got.fallback_from == CN_FLAVOR, got
    assert which.asked == [*CN_CLI_COMMAND_NAMES, *INTL_CLI_COMMAND_NAMES], which.asked


def test_the_bundled_name_carries_the_windows_suffix_and_the_other_platform_does_not(tmp_path):
    """`.exe` 这一格两个分支都要有发：只测一个平台等于把另一支的拼法留成未验。

    Windows 那一支找 `qodercli.exe`；非 Windows 那一支**不许**带后缀 —— 后半个对照是"目录里只有
    `qodercli.exe`"，实现要是无条件补 `.exe`，那一支就查不到；无条件不补则前一发查不到。两发互相卡死。
    """
    hit = bundled(tmp_path / "win", f"{INTL_CLI_BINARY}.exe")
    assert resolve_cli_executable(
        env={},
        package_dir=hit,
        which=FakeWhich({}),
        system_name="Windows",
        home=empty_home(tmp_path / "win"),
    ) == str(hit / BUNDLED_DIR_NAME / f"{INTL_CLI_BINARY}.exe")

    only_exe = bundled(tmp_path / "nix", f"{INTL_CLI_BINARY}.exe")
    with pytest.raises(QoderTransportError):
        resolve_cli_executable(
            env={},
            package_dir=only_exe,
            which=FakeWhich({}),
            system_name="Linux",
            home=empty_home(tmp_path / "nix"),
        )
    plain = bundled(tmp_path / "nix2", INTL_CLI_BINARY)
    assert resolve_cli_executable(
        env={},
        package_dir=plain,
        which=FakeWhich({}),
        system_name="Linux",
        home=empty_home(tmp_path / "nix2"),
    ) == str(plain / BUNDLED_DIR_NAME / INTL_CLI_BINARY)


def test_the_cn_install_path_uses_the_platform_suffix(tmp_path):
    """CN 默认安装位也要按平台拼后缀：POSIX 认裸名、Windows 认 `.exe`，两边不许互相冒充。"""
    home = tmp_path / "nix-home"
    plain = home / CN_CONFIG_DIR_NAME / "bin" / CN_CLI_BINARY / CN_CLI_BINARY
    plain.parent.mkdir(parents=True)
    plain.write_bytes(b"MZ")
    got = resolve_cli(
        env={}, package_dir=bundled(tmp_path / "nix"), which=FakeWhich({}), system_name="Linux", home=home
    )
    assert got.executable == str(plain), got

    with pytest.raises(QoderTransportError):
        resolve_cli(
            env={}, package_dir=bundled(tmp_path / "w32"), which=FakeWhich({}), system_name="Windows", home=home
        )


def test_a_machine_without_any_cli_is_refused_naming_every_place_it_looked(tmp_path):
    """五档全空 ⇒ 抛的是带"试过哪些"的拒绝，不是 `FileNotFoundError`，也不是空话一句。

    这条替代了 owner 那一屏 traceback：`QoderTransportError` 派生自 `RuntimeError`，因此落在
    `realrun.SWALLOWABLE` 里（下面那发单独断这个继承关系，防止有人把它改成 `OSError` 的后裔）。
    消息的最低要求 = **两个品牌**的命令名、CN 默认安装位、那个环境变量名、查过的包内目录，
    加一条人能直接执行的下一步；同时**不许**出现凭据赋值形态，也不许把厂商那句原文抄过来。
    """
    package = bundled(tmp_path)
    home = empty_home(tmp_path)
    with pytest.raises(QoderTransportError) as caught:
        resolve_cli_executable(env={}, package_dir=package, which=FakeWhich({}), system_name="Windows", home=home)
    message = str(caught.value)
    for expected in (
        *CN_CLI_COMMAND_NAMES,
        *INTL_CLI_COMMAND_NAMES,
        CLI_PATH_ENV,
        str(package / BUNDLED_DIR_NAME),
        PACKAGE_NAME,
    ):
        assert expected in message, f"消息里点不到 {expected!r} ⇒ 拿到这句话的人下一步不知道该改哪：{message}"
    assert CN_CLI_BINARY in message and CN_CONFIG_DIR_NAME in message, (
        f"CN 默认安装位与二进制名都要点到：{message}"
    )
    assert caught.value.reason == "cli_not_found" and caught.value.retryable is False
    assert not any(token in message for token in FORBIDDEN_IN_REFUSAL), message
    assert "Qoder CLI not found" not in message, "厂商那句原文不该抄进我们的消息里"

    missing_package = tmp_path / "没有这个包"
    with pytest.raises(QoderTransportError) as bare:
        resolve_cli_executable(
            env={},
            package_dir=missing_package,
            which=FakeWhich({}),
            home=empty_home(tmp_path, home_name="bare-home"),
        )
    assert BUNDLED_DIR_NAME in str(bare.value), str(bare.value)


def test_the_refusal_is_a_runtime_error_so_the_driver_can_swallow_it():
    """拒绝必须留在 `realrun` 那张"可以收住继续跑"的名单里 —— 这一格就是 traceback 与 `RUN FAIL` 的分界。

    只断类型，不去 import `realrun`：那张名单的内容（`RuntimeError` / `ValueError`）由驱动自己的判据管，
    这里管的是"transport 抛的东西够不够格进那张名单"。反过来说，把它换成 `OSError` 的后裔当场红。
    """
    assert issubclass(QoderTransportError, RuntimeError)
    assert not issubclass(QoderTransportError, OSError)


# --------------------------------------------------------- 二、runner 那一侧：什么时候才查、查了怎么用


def test_the_production_runner_resolves_only_when_it_runs(monkeypatch, tmp_path):
    """构造 `PRODUCTION_RUNNER()` 一个字节都不读盘；到 `run()` 才查，查到的就是 argv[0]。

    为什么这是承重的一格而不是实现细节：工厂那一侧的调用形状就是 `runner_class()`（零参构造），
    而 `tests/guards/test_transport_spec_factory.py` 与本文件的形状档都会造它。放在构造时查 =
    这两处都变成"取决于这台机器装没装 CLI"，一台没装的机器上整片用例直接 error。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        qoder_transport,
        "resolve_cli",
        lambda **kw: calls.append("查了") or ResolvedCli("D:/x/qoderclicn.exe", CN_FLAVOR, None),
    )
    runner = qoder_transport.PRODUCTION_RUNNER()
    assert calls == [], "构造阶段就去查 CLI ⇒ 工厂与形状档都被绑到真机的安装状态上"
    assert runner._cli_path is None

    seen: list[list[str]] = []

    def fake_popen(args, **kwargs):
        seen.append(list(args))

        class Done:
            pid = 1
            returncode = 0

            def communicate(self, input=None, timeout=None):  # noqa: A002 - 名字由 CliRunner 那一侧定
                return "", ""

        return Done()

    runner._popen = fake_popen
    # 取证根交给临时目录：这一发解析出的 flavor 是 CN，默认根会指向 `~/.qoder-cn`（用户主目录）。
    runner._home = tmp_path
    runner.run(argv=(CLI_BINARY, "--print"), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert calls == ["查了"] and seen == [["D:/x/qoderclicn.exe", "--print"]], (calls, seen)


def test_the_bundled_dir_of_the_installed_package_is_reported_not_guessed():
    """`bundled_cli_dir()` 不注参数时说的是"本机这份包装在哪"，那个路径的形状要能核对。

    这一格量的是"路径由 `importlib` 现取而不是写死"：名字必须是 `_bundled`、父目录必须是包本身。
    包没装（比如某个只跑 fast 档的环境）⇒ 交回 `None`，也明写出来，不抛。
    """
    directory = bundled_cli_dir()
    if directory is None:
        pytest.skip(f"这台机器的解释器里没有 {PACKAGE_NAME} ⇒ 没有 `_bundled` 可报，这一档没有对象")
    assert directory.name == BUNDLED_DIR_NAME, str(directory)
    assert directory.parent.name == IMPORT_NAME, str(directory)


# ------------------------------------------------- 三、与本机装的这份 SDK 的符合性（乙档，读它自己的文件）


def _sdk_file(relative: str) -> Path:
    """装着 SDK 的那个目录里的一份文件。包没装 ⇒ 明写 SKIP（对照面不在，不是"两边都对得上"）。"""
    spec = importlib.util.find_spec(IMPORT_NAME)
    if spec is None or not spec.submodule_search_locations:
        pytest.skip(f"这份解释器里没有 {PACKAGE_NAME} ⇒ 乙档的对照面不在，这一条没有对象可比")
    return Path(next(iter(spec.submodule_search_locations))) / relative


def _brand_command_names() -> tuple[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """从 `_brand.py` 的**文本**里读出两个分支各自的 `CLI_COMMANDS`，外加 `SDK_BRAND` 那个字面量。

    为什么不 `import qoder_agent_sdk`：本机实测那一发要 1.70 秒、往 `sys.modules` 里加 628 个模块。
    把它放进 fast 档既拖慢也不干净（guards 与 contract 共用同一个解释器，而 contract 那一档自己会拔模块）。
    形状只认这一种：`X = (…A…) if IS_CN_SDK else (…B…)`；认不出来就抛，让改的人回来把它读一遍。
    """
    source = _sdk_file("_brand.py").read_text(encoding="utf-8")
    brand: str | None = None
    commands: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "SDK_BRAND" and isinstance(node.value, ast.Constant):
            brand = str(node.value.value)
        if target.id == "CLI_COMMANDS" and isinstance(node.value, ast.IfExp):
            commands = (
                tuple(ast.literal_eval(node.value.body)),
                tuple(ast.literal_eval(node.value.orelse)),
            )
    assert brand is not None and commands is not None, (
        "`_brand.py` 里 `SDK_BRAND` / `CLI_COMMANDS` 的形状不是这条判据认的那种 ⇒ 去把它读一遍再改准这里"
    )
    return brand, commands


def test_both_brand_command_lists_match_the_sdk_brand_branches():
    """两个品牌的命令名名单都必须等于本机 SDK `_brand.py` 两个分支各自那一组 —— 不是抄来的。

    判据从厂商文件的**文本**里读两个分支（`CLI_COMMANDS = (…cn…) if IS_CN_SDK else (…global…)`），
    本文件一个字都不写名单内容；厂商改名/换品牌 ⇒ 两条当场红，逼着改的人回来对一遍。
    """
    _brand, (cn_names, global_names) = _brand_command_names()
    assert INTL_CLI_COMMAND_NAMES == global_names, (
        f"INTL 名单 {INTL_CLI_COMMAND_NAMES} 对不上厂商 global 分支 {global_names}"
    )
    assert CN_CLI_COMMAND_NAMES == cn_names, (
        f"CN 名单 {CN_CLI_COMMAND_NAMES} 对不上厂商 cn 分支 {cn_names}"
    )
    assert CLI_BINARY == INTL_CLI_COMMAND_NAMES[0], "argv[0] 那个占位名 = INTL 名单里排第一的那个"


def test_the_intl_tiers_and_the_suffix_rule_match_the_vendor_lookup():
    """INTL 三档的**先后**、环境变量名、`_bundled` 目录名与 `.exe` 规则逐条对上厂商那份源码。

    比的是"两边都有这些字面量，而且顺序一致"，不比消息文本（那半故意不同形：它的 `CLINotFoundError`
    还带着"用 `QoderAgentOptions(cli_path=…)`"这句我们不接受的用法建议 —— 我们的入口不是它的客户端）。
    顺序这一格用偏移量量：只看"都有"的话，把 PATH 排到第一位照样绿。CN 那一档是我们自己的顺序
    （owner 2026-10-24 裁决），不拿厂商源码比，但**CN 必须排在 INTL 之前**由末尾两行单独断。
    """
    vendor = _sdk_file("_internal/transport/subprocess_cli.py").read_text(encoding="utf-8")
    assert f'"{CLI_PATH_ENV}"' in vendor, f"厂商那份里不是这个环境变量名：{CLI_PATH_ENV}"
    assert f'"{BUNDLED_DIR_NAME}"' in vendor
    assert 'f"{command}.exe"' in vendor and '"Windows"' in vendor, "`.exe` 那条规则的形状变了"

    ours = TRANSPORT_FILE.read_text(encoding="utf-8")
    for source, func, markers in (
        (
            vendor,
            "_find_cli",
            (f'os.environ.get("{CLI_PATH_ENV}")', "_find_bundled_cli()", "shutil.which("),
        ),
        (
            ours,
            "resolve_cli",
            (".get(CLI_PATH_ENV", "cn_install_path(", "which(command)", "bundled_cli_dir("),
        ),
    ):
        offsets = _marker_order(source, func, markers)
        assert offsets == sorted(offsets), f"{func} 里几档的顺序不是那一张顺序：{offsets}"

    cn_loop = ours.find("for command in CN_CLI_COMMAND_NAMES")
    intl_loop = ours.find("for command in INTL_CLI_COMMAND_NAMES")
    assert 0 <= cn_loop < intl_loop, "CN 的问 PATH 循环必须排在 INTL 之前"


def test_the_two_subprocess_executors_decode_the_same_way():
    """CLI 执行器与 `cadence.run_git` 必须是**同一张**解码口径（两份执行器、一个口径）。

    本机区域编码 cp936 而 CLI 写的是 UTF-8：不钉 `encoding` 的 `text=True` 会在读流线程里当场抛
    `UnicodeDecodeError` —— 2026-10-19 在 git 那一侧实测过同一形态（`evidence/phase-15.md` §七）。
    CLI 一旦真被起来（本笔就是让它第一次真被起来），中文任务正文与路径都会经过这条读流。
    比两份源码的字面量而不是各写各的默认值；两份一起改仍绿是刻意的（口径只有一个，执行器有两个）。
    """
    ours = _call_keywords(TRANSPORT_FILE.read_text(encoding="utf-8"), "run", "_popen")
    frozen = _call_keywords(CADENCE_FILE.read_text(encoding="utf-8"), "run_git", "run")
    for name in ("encoding", "errors"):
        assert name in ours, f"`SubprocessRunner.run` 少了关键字 {name} ⇒ 与 `cadence.run_git` 分叉成两份口径"
        assert isinstance(ours[name], str) and isinstance(frozen[name], str), (
            f"{name} 两侧必须是字面量：现读 CLI 侧 {ours[name]!r} / git 侧 {frozen[name]!r}"
            " ⇒ 换成变量就是「各自运行时的区域编码」，这条判据也就管不住了"
        )
        assert ours[name] == frozen[name], f"{name}：CLI 侧 {ours[name]!r} 对不上 git 侧 {frozen[name]!r}"


# --------------------------------------------------------------------- 四、活性档（不 mock，真起一发）


def test_the_real_cli_on_this_machine_starts_and_reports_the_version_of_its_flavor(tmp_path):
    """生产路径不注任何替身：解析 → `subprocess.Popen` 真起一发 `--version`，rc 0 且版本号按 flavor 对上。

    本机现在解析到 CN（默认安装位）⇒ 期望 `1.1.78`；一台没装 CN 但装着 INTL SDK 的机器解析到 INTL
    ⇒ 期望 `1.1.61`。两档全空 ⇒ 明写 SKIP（拒绝面由上面那两条管，没有对象可起不叫失败）。
    它**不出网**（`--version` 只打印版本号），所以不违反成本纪律。`log_root` / `projects_root` 注临时目录：
    取证那两面归约束 ② 的判据管，这一发要的是"这个文件是个能起的程序"。
    `home` **不注**：解析要按真机走，这一发量的就是"这台机器上真能起哪个"—— 注一个假 home 等于把
    runner 自己的解析改成"CN 全空 ⇒ 回退 INTL"，与外面这句 `resolve_cli()` 打架（2026-10-24 实测过：
    注 `home=tmp_path` 时 runner 起的是 INTL bundled，版本 `1.1.61` ≠ CN 档 `1.1.78` 当场红）。

    `cwd` 交的是 `tmp_path` 而不是留空：这一发是本仓**唯一真起厂商进程**的判据，它自己就得是那句裁决的
    现场 —— 厂商子进程的家由调用方点名交出，缺省继承调用方目录（= 平台源码根）不是可用形状。
    """
    try:
        resolved = resolve_cli()
    except QoderTransportError as exc:
        pytest.skip(f"这台机器上没有可起的 CLI ⇒ 活性档没有对象可验；拒绝面读的是 {str(exc)[:120]}")
    assert Path(resolved.executable).is_file(), resolved.executable
    assert "qoder" in Path(resolved.executable).name.lower(), resolved.executable
    expected = {CN_FLAVOR: CN_CLI_VERSION_EXPECTED, INTL_FLAVOR: INTL_CLI_VERSION_EXPECTED}[resolved.flavor]

    outcome = SubprocessRunner(log_root=tmp_path, projects_root=tmp_path).run(
        argv=(CLI_BINARY, "--version"),
        stdin_text="",
        run_key="probe",
        timeout_seconds=30.0,
        cwd=tmp_path,
    )
    assert outcome.cli_flavor == resolved.flavor, (
        f"runner 自己解析到 {outcome.cli_flavor!r} 而外面这句说 {resolved.flavor!r}"
        f"（降级自={outcome.cli_fallback_from!r}）⇒ 两条解析路径不指向同一支"
    )
    assert outcome.timed_out is False, f"30 秒没回来：{outcome.exit_code}"
    assert outcome.exit_code == 0, (resolved.executable, outcome.exit_code, outcome.stderr[:200])
    assert outcome.stdout.strip() == expected, (
        f"真机版本号 {outcome.stdout.strip()!r} 对不上 {resolved.flavor!r} 档的 {expected!r} ⇒ "
        "旗标口径（乙档）读的是另一个版本的 CLI，那一整份要重读"
    )
