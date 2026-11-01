"""启动自检：`plan.md` 的 Checkpoint 0 判据（C1 / C2 / C5 三项，C3 / C4 随各自 Phase 验）。

只依赖标准库——本自检的职责之一是在依赖未装全时报出真实现状，而不是自己先崩；
C5 因此用 try/except 逐个探第三方包，探到失败也只记账、不抛出。
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"

#: T106 的呈现旗标（FR-058）。成文一次：`main` 里的分支与 `tests/integration/test_autonomy_cli.py`
#: 都取这个常量，免得旗标串出现两份而改了一处另一处静默失效。
SHOW_DANGER_FLAG = "--show-danger"

BOUNDS = re.compile(r"(>=|<)\s*(\d+(?:\.\d+)*)")

# 发行包名与导入名不一致的两处：写成表而不是猜规则，错了当场看得见。
IMPORT_ALIAS = {
    "pyyaml": "yaml",
    "langgraph-checkpoint-postgres": "langgraph.checkpoint.postgres",
}


def _satisfies(version: tuple[int, ...], spec: str) -> bool:
    for op, ver in BOUNDS.findall(spec):
        bound = tuple(int(x) for x in ver.split("."))
        if op == ">=" and version < bound:
            return False
        if op == "<" and version >= bound:
            return False
    return True


def check_interpreter() -> tuple[str, str]:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return BLOCKED, "pyproject.toml 不存在"
    with pyproject.open("rb") as fh:
        spec = tomllib.load(fh)["project"]["requires-python"]
    now = sys.version_info[:3]
    status = PASS if _satisfies(now, spec) else FAIL
    return status, f"current={'.'.join(map(str, now))} requires-python={spec}"


def check_postgres() -> tuple[str, str]:
    try:
        import psycopg
    except ImportError:
        return BLOCKED, "psycopg 未安装（当前解释器缺该依赖；装法 = venv + `pip install -e .[dev]`）"
    dsn = os.environ.get("ACP_DB_DSN", "")
    if not dsn:
        return BLOCKED, "ACP_DB_DSN 未注入（凭据只从环境来，MUST NOT 写进文件）"
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            version = conn.execute("select version()").fetchone()[0]
    except Exception as exc:
        return FAIL, f"连接失败：{type(exc).__name__}"
    return PASS, f"connected, server={version.split(',')[0]}"


def _declared_imports() -> list[str]:
    """从 pyproject 的 dependencies 派生要探测的导入名。

    判据跟着清单走而不是抄一份清单：加了依赖就自动纳入自检，删了也不会留下过期项。
    """
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return []
    with pyproject.open("rb") as fh:
        deps = tomllib.load(fh)["project"].get("dependencies", [])
    out = []
    for spec in deps:
        name = re.split(r"[\[\s;<>=!~]", spec.strip(), maxsplit=1)[0].strip().lower()
        if not name:
            continue
        out.append(IMPORT_ALIAS.get(name, name.replace("-", "_")))
    return out


def check_dependency_surface() -> tuple[str, str]:
    mods = _declared_imports()
    if not mods:
        return FAIL, "pyproject 未声明任何依赖，无项可检（不是通过）"
    broken = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as exc:
            broken.append(f"{m}({type(exc).__name__})")
    if broken:
        return FAIL, f"不可加载 {len(broken)}/{len(mods)}：" + "、".join(broken)
    return PASS, f"declared={len(mods)} 全部可导入"


def check_bus_layout() -> tuple[str, str]:
    """C3：总线 ACL 比对（T015 落地后，从"两个文件在不在"升级成真比对）。

    三件可判的事：① 归属表自洽——模式不重复、四个代表路径各解出唯一 owner；
    ② 表与契约 §2 同口径——公共区解出 `orchestrator_writer`、workspace 解出 `adapter:<前缀>`；
    ③ 每个在册 `<agent>-workspace/` 的目录名能解出 agent_id（解不出就是归属声明与目录对不上）。

    没有任何项目被初始化时本项读 **BLOCKED 而不是 PASS**："没有输入可检"不能伪装成
    "检过且通过"——同 C4 对 registry 的口径。复现命令见 `quickstart.md §2` 路径 C 的
    「总线初始化」一条（它建在项目目录里，不写进工作树的持久状态）。
    """
    try:
        from acp.bus import acl as bus_acl
        from acp.bus import layout as bus_layout
    except Exception as exc:
        return BLOCKED, f"bus/acl.py 未建立或导入失败：{type(exc).__name__}"

    patterns = [rule.pattern for rule in bus_acl.RULES]
    if len(patterns) != len(set(patterns)):
        return FAIL, f"归属表有重复模式：{patterns}"

    probes = ("tasks/T01-home.md", "results/T01.md", "status.md", "qoder-workspace/a.html")
    try:
        owners = {path: bus_acl.declared_owner(path) for path in probes}
    except (KeyError, ValueError) as exc:
        return FAIL, f"代表路径解不出归属：{type(exc).__name__}: {exc}"
    if owners["qoder-workspace/a.html"] != f"{bus_acl.ADAPTER_OWNER_PREFIX}qoder":
        return FAIL, f"workspace 归属与契约 §2 不符：{owners['qoder-workspace/a.html']!r}"
    if owners["status.md"] != bus_acl.PUBLIC_OWNER or owners["tasks/T01-home.md"] != bus_acl.PUBLIC_OWNER:
        return FAIL, f"公共区归属与契约 §2 不符：{owners}"

    bus = ROOT / bus_layout.BUS_ROOT_NAME
    workspaces = sorted(
        directory.name
        for project in (bus.glob("*/") if bus.is_dir() else [])
        for directory in project.glob(f"*{bus_layout.WORKSPACE_SUFFIX}")
        if directory.is_dir()
    )
    unresolvable = [
        name for name in workspaces if bus_layout.workspace_agent_id(name) is None
    ]
    if unresolvable:
        return FAIL, f"workspace 目录名解不出 agent_id：{unresolvable}"
    if not workspaces:
        return BLOCKED, (
            f"rules={len(patterns)} 表自洽，但总线未初始化（workspaces=0）⇒ 无输入可检"
        )
    return PASS, f"rules={len(patterns)} workspaces={len(workspaces)} mismatches=0"


def check_registry() -> tuple[str, str]:
    """C4：注册表**能过 loader**，而不只是"文件在 + 形态数够"（owner 2026-10-13 裁决 2）。

    三段判据按顺序，每段各自的失效形态不同：
    ① 文件不存在 ⇒ **BLOCKED**（Phase 3 未落地是"缺输入"，不是"检过不过"，与 C3 同一口径）；
    ② loader 或 params-ref §9 读不出 / 任一条申报不合法 ⇒ **FAIL** 并带出是哪一条哪一项
       （旧版这里只数形态，"申报缺项也能过 C4"是它留的洞 ⇒ 裁决 2 补的就是这一层）；
    ③ 形态数 < 3 ⇒ **FAIL**（SC-001 的"平台内 / SDK / API 各 ≥1"）。

    **代价点名**（owner 原话"C4 变慢（走 loader 而非只读文件），但 selfcheck 手工触发，可接受"）：
    本函数现在会连带导入 pydantic 与 `acp.protocol`，并把 `config/params-ref.md` 读进来解析
    —— 所以 **`import acp.selfcheck` 本身仍只用标准库**（第三方与项目内导入全在这几个
    check 函数体内，C1 / 顶部逻辑不碰），这条边界不因为裁决 2 而挪。
    """
    path = ROOT / "src" / "acp" / "registry" / "agents.yaml"
    if not path.exists():
        return BLOCKED, "registry/agents.yaml 未建立（Phase 3 T039）"
    try:
        from acp.registry.loader import RegistryError, load_registrations
    except Exception as exc:
        return BLOCKED, f"registry.loader 不可导入：{type(exc).__name__}: {exc}"
    try:
        registrations = load_registrations(path)
    except RegistryError as exc:
        return FAIL, f"loader 拒收注册表：{exc}"
    except Exception as exc:
        return FAIL, f"loader 非预期异常：{type(exc).__name__}: {exc}"
    forms = {registration.manifest.form for registration in registrations}
    if len(forms) < 3:
        return FAIL, f"形态数={len(forms)}（SC-001 要求平台内/SDK/API 各 ≥1）"
    return PASS, (
        f"entries={len(registrations)} forms={sorted(forms)}，loader 全条通过"
        "（取值域来源 params-ref §9；每条都构造出合法 CapabilityManifest）"
    )


CHECKS = (
    ("C1", "interpreter", check_interpreter),
    ("C2", "postgres", check_postgres),
    ("C3", "bus-acl", check_bus_layout),
    ("C4", "registry", check_registry),
    ("C5", "deps-surface", check_dependency_surface),
)


def show_danger(
    *,
    path=None,
    recorded: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """`--show-danger`（T106 / FR-058）：打印当前生效清单的**语义指纹**与每条的默认动作。

    为什么这一项住在 selfcheck 而不是新开一个 `acp.danger show`：owner 给的入口就是
    `selfcheck --show-danger`（`tasks.md` 的 T106 行），而"启动前先看一眼生效的是哪版策略"
    本来就是自检的一句话。第三句理由才是省事。

    读的是 `config/loader.load_danger_list()`，也就是拦截路径用的同一份读法 ⇒ 这里打印的指纹
    与 `guard/autonomy.py` 当时用的是同一个数（另开一份"数一遍条目"的实现就是第二个载体）。
    每条动作都打出来，包括 `action` 在文件里省略时补上的默认值 —— 那句"新项默认拦截"（FR-058）
    在这里是可核对的一行，不是文档里的一句话。

    **T107 裁决 ㉔ 加的两个注入位**（生产调用一个都不传，`main` 里那条分支就是 `show_danger()`）：

    - `path` = 检查**一份**清单（语法错 / 为空 这两种损坏形态只能这样喂给判据）。
      口径同 T106 的 `acp.danger`：生效清单的来源仍是 `loader.DANGER_LIST_PATH`，
      注入只是"把校验器拿来检查一份不在那个位置的文件"。
    - `recorded` = EN-16 各项目当时记下的 `danger_list_version`（`{project_key: 指纹}`），
      用来查第三种损坏形态 —— **记的是一版、盘上算出来是另一版**（清单改了而当前态没跟上）。
      **这里刻意不连库**：`import acp.selfcheck` 只用标准库这条边界不挪（口径同 `check_registry`
      那段"C4 变慢但边界不挪"），而 EN-16 的读法住在 `persistence/autonomy.py` —— 把读数注进来
      是装配点（T123）的活。⇒ 不注入时明打"未比对"一行，**第 ③ 形态不在本命令的射程内**，
      这条限度钉在判据里而不是含糊过去。
    - `recorded` 给了但是**空集合** ⇒ 读 **BLOCKED 而不是 PASS**（"没有行可比对"不是"比对通过"，
      口径同 C3 对未初始化总线、C4 对缺 registry 的读法）。

    **本函数不写任何状态**：它只读清单、只比字符串。
    """
    try:
        from acp.config.loader import DangerListError, load_danger_list
    except Exception as exc:
        return BLOCKED, [f"config.loader 不可导入：{type(exc).__name__}"]
    try:
        loaded = load_danger_list(path)
    except DangerListError as exc:
        # 消息本身来自 loader，且 loader 的报错已经过 `_shown()` / `redact_bus_text()`（清单是自由文本）。
        return FAIL, [f"清单读不出：{exc}"]
    lines = [
        f"fingerprint={loaded.fingerprint} entries={len(loaded.entries)} "
        f"user_added={loaded.user_added_count} actions={sorted({entry.action for entry in loaded.entries})}"
    ]
    lines += [
        f"entry {ordinal} :: pattern={entry.pattern!r} action={entry.action} user_added={entry.user_added}"
        for ordinal, entry in enumerate(loaded.entries, start=1)
    ]
    if recorded is None:
        lines.append("en16=未比对（recorded 未注入 ⇒ 第③种损坏形态『指纹对不上』不在本命令射程内）")
        return PASS, lines
    if not recorded:
        return BLOCKED, lines + ["en16=BLOCKED :: EN-16 没有任何行可比对（不是通过，口径同 C3/C4 的『没有输入可检』）"]
    stale = sorted(str(key) for key, version in recorded.items() if str(version) != loaded.fingerprint)
    if stale:
        return FAIL, lines + [
            f"en16=MISMATCH :: 当前清单={loaded.fingerprint} 而 EN-16 记的另一版，项目={stale}"
            " ⇒ 要么清单改了而重载没跟上，要么这一行本来就是旧版留下的（两种都要人看，不许静默通过）"
        ]
    lines.append(f"en16=一致 projects={sorted(str(key) for key in recorded)}")
    return PASS, lines

# owner 2026-10-11 裁定 2：旧判据要 C1~C4 全绿，而 C3/C4 的输入物属 Phase 1/3 ⇒ 循环依赖。
# 新判据只把 C1 / C2 / C5 计入 Checkpoint 0；C3 随 T015 验、C4 随 T039 验。
CHECKPOINT0 = ("C1", "C2", "C5")
DEFERRED_TO = {"C3": "T015", "C4": "T039"}


def main(argv: list[str]) -> int:
    # `selfcheck` 的模块边界是"只依赖标准库"，所以这里延迟导入；入口一跑就装上全局 secrets 过滤器。
    from acp.config import install_logging_filter

    install_logging_filter()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if SHOW_DANGER_FLAG in argv:
        # **呈现入口，不是门禁项**：这一支不跑 C1~C5、也不改 `CHECKPOINT0` 的判据面
        # （把"清单能不能读"升成一条门禁是另一件事，已登记等 owner 裁；见 evidence/phase-08.md §十二）。
        # 退出码只回答这一件事：清单读得出 = 0，读不出 = 1（"没有清单"从来不是通过）。
        status, lines = show_danger()
        print(f"DANGER {status} ::")
        for line in lines:
            print(f"  {line}")
        return 0 if status == PASS else 1
    results: dict[str, tuple[str, str]] = {}
    tally = {PASS: 0, FAIL: 0, BLOCKED: 0}
    for cid, name, fn in CHECKS:
        try:
            status, evidence = fn()
        except Exception as exc:
            status, evidence = FAIL, f"自检自身异常：{type(exc).__name__}: {exc}"
        results[cid] = (status, evidence)
        tally[status] += 1
        print(f"[{cid} {name}] {status} :: {evidence}")
    print(
        f"SUMMARY pass={tally[PASS]} fail={tally[FAIL]} blocked={tally[BLOCKED]} "
        f"green_all={tally[PASS] == len(CHECKS)}"
    )
    passed = [cid for cid in CHECKPOINT0 if results[cid][0] == PASS]
    verdict = PASS if len(passed) == len(CHECKPOINT0) else FAIL
    deferred = "、".join(f"{cid}→{DEFERRED_TO[cid]}" for cid in sorted(DEFERRED_TO))
    print(
        f"CHECKPOINT0 {verdict} :: gate={'∧'.join(CHECKPOINT0)} "
        f"passed={len(passed)}/{len(CHECKPOINT0)} deferred={deferred}"
    )
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
