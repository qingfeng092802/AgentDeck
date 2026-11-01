"""凭据注入路径、`secrets` 脱敏过滤器（T027，FR-016｜R-03，宪法「二」）与高危清单加载（T101，FR-055 / FR-058｜R-15）。

三件事，且只做这三件事：

1. **注入**：凭据只从环境变量取（`env_value` / `db_url`）。本模块没有"从文件读凭据"的回退
   ——留一个回退就等于承认凭据可以落文件，而宪法「二」的红线正是"不落文件"。
   异常消息里 MUST NOT 出现凭据值，只出现变量名。
   **不在这里枚举变量名**：哪些环境变量算凭据，唯一口径在 `config/params-ref.md §4 / §5`
   （`plan.md` 的 Constitution Check 表规定计划与代码都不复述包名 / 域名 / 环境变量名）。
   本模块用**名字形态**（`…KEY|…TOKEN|…SECRET|…PASSWORD|…DSN`）来判定要不要洗，而不是抄名单。

2. **脱敏**：`SecretsRedactor` 按**凭据形态**（内嵌口令的连接串、`sk-` 前缀键、
   `NAME=value` 赋值、PEM 私钥块、JWT 三段）与**当前环境里那些凭据变量的实际值**两路清洗。
   判据只用形态而不内联任何凭据字面量：把真值抄进源码就是最直接的泄漏。
   环境值这一路是补形态盲区的（换一把不合规整形态的键，形态正则看不见，但它必然还在那个
   变量里）。

3. **高危清单加载**（T101）：`load_danger_list()` 读 `config/danger_list.yaml`、逐条校形、
   算**内容指纹**。三件事在本模块里彼此独立 —— 加载路径**不读环境变量、不碰数据库**（口径同
   `registry/loader.py`：配置加载器不依赖落库层，否则 `bus/gateway.py` 那条只需要脱敏的路径会被迫
   拖进 SQLAlchemy）。指纹写进 EN-16 那一步在 `persistence/autonomy.py`，不在这里。
   **模块级不留缓存**：每次调用都重新读盘、重新算指纹。这正是 FR-055 那句"重载走 loader 而非重启碰运气"
   的实现形态，也是 `quickstart.md` 排障表里"改了清单但没重载 ⇒ 用的还是旧版"那一行的成因 ——
   缓存一份就是把这个失效做成默认行为。
   报错消息回显清单原文时**过一遍本模块自己的 `redact_bus_text()`**：清单是可被人扩展的自由文本，
   谁往里粘了一把键，异常里就会带出它，而异常会进终端与日志。两条回显路径都算 ——
   校验类回显 `pattern`，解析类回显 PyYAML 摘的出错行（后者还断掉 `__cause__`，见 `DangerListError`）。

**挂载点**（契约 `file-bus.md §5` / `agent-protocol.md §2.2` 要求同时作用于日志与产物两面）：
- 日志侧 = `install_logging_filter()`（logging 的 `secrets` 过滤器）；
- 产物侧 = `acp.bus.gateway.write_artifact` 与 `acp.bus.orchestrator_writer.append_event`
  在落盘前调用 `redact_bus_text()`。只在调用点各自洗一遍会漏掉新加的写入面，所以挂在两个入口上。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

DSN_ENV = "ACP_DB_DSN"

#: 脱敏标记。**固定短串**，用例按它断言"洗过"，产物里出现它就等于出现过去敏痕迹。
REDACTED = "[REDACTED]"

#: 名字形态：以此结尾的环境变量视为凭据载体（名字本身不是秘密，值才是）。
SECRET_NAME_RE = re.compile(r"(API_?KEY|ACCESS_KEY|ACCESS_TOKEN|SECRET_?\w*|TOKEN|PASSWORD|DSN)$")

#: 短于此长度的环境值不参与整体替换——`PATH=C:\...` 之类会把正文洗花。
MIN_VALUE_LEN = 8

# 凭据形态（都是"形状"，不内联任何真实值）。
FORM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 内嵌口令的连接串：user:pass@host。没有 @ 的普通 URL 不命中。
    re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@:]+:[^\s@/]+@\S+"),
    # sk- 系 API key（方舟 / OpenAI 兼容端口的常见形态）。
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    # P1-20：Qoder PAT、火山方舟 key、WorkBuddy 变量/取值前缀。
    # 这三类此前只靠"值恰好在 env 里被整体替换"兜底；值一旦离开 env（子进程、粘贴进正文），
    # 脱敏就失效。按形态认前缀是第二道，和 env 值替换各管一面。
    re.compile(r"\bpt-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bark-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bWORKBUDDY_[A-Za-z0-9_]{12,}"),
    # NAME=值 的赋值行（.env 写法与 JSON 里的 "password": "…" 都算）：保留名字，只洗值。
    # 名字 MUST **以**凭据词结尾，所以 `tokenizer: cl100k` 这类正常内容不会被误洗——
    # 放宽到"含 token 即洗"会让产物被洗花，那是另一种破坏。
    re.compile(
        r'(?im)^([ \t]*["\']?[A-Za-z0-9_.\-]*'
        r"(?:API_KEY|ACCESS_KEY|ACCESS_TOKEN|SECRET_KEY|SECRET_ID|CLIENT_SECRET|SECRET|TOKEN|"
        r'PASSWORD|PASSWD|DSN)["\']?[ \t]*[:=][ \t]*)(\S+)(["\']?.*)$',
    ),
    # PEM 私钥块。
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # JWT 三段式。
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
)


class CredentialMissing(RuntimeError):
    """凭据变量缺失或为空。消息里只有变量名，不可能带出值。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"环境变量 {name} 未注入或为空（凭据只从环境来，MUST NOT 写进文件）")
        self.name = name


def env_value(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name, "")
    if not value:
        raise CredentialMissing(name)
    return value


def db_url(env: Mapping[str, str] | None = None) -> str:
    """`ACP_DB_DSN` → SQLAlchemy URL，固定走 psycopg 3。

    清单里装的是 `psycopg[binary]`（psycopg 3），而 SQLAlchemy 对 `postgresql://` 的默认驱动是
    psycopg2（本机没装）⇒ 不显式指定 `+psycopg` 就会以"缺 psycopg2"的假象失败。
    迁移（`migrations/env.py`）与用例共用这一条，两处不会走成两个口径。
    """
    dsn = env_value(DSN_ENV, env)
    for scheme in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme) :]
    return dsn


class SecretsRedactor:
    """把凭据形态与环境凭据值统一替换成 `REDACTED`。"""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        collected = {
            value
            for name, value in source.items()
            if SECRET_NAME_RE.search(name) and len(value) >= MIN_VALUE_LEN
        }
        # 先长后短：短值常是长值的子串（同一把键被拼进不同形态），先替短的会把长的留下半截。
        self._values = tuple(sorted(collected, key=len, reverse=True))
        self.redactions = 0

    @property
    def tracked_env_values(self) -> int:
        """参与整体替换的环境值**条数**（不暴露值本身）。"""
        return len(self._values)

    def __repr__(self) -> str:  # 防止对象被打进日志时把值带出去
        return f"SecretsRedactor(tracked_env_values={self.tracked_env_values})"

    def hits(self, text: str) -> bool:
        probe = text
        for value in self._values:
            if value in probe:
                return True
        return any(pattern.search(probe) for pattern in FORM_PATTERNS)

    def redact(self, text: str) -> str:
        out = text
        for value in self._values:
            out = out.replace(value, REDACTED)
        for pattern in FORM_PATTERNS:
            out = pattern.sub(_substitute, out)
        if out != text:
            self.redactions += 1
        return out


def _substitute(match: re.Match[str]) -> str:
    """赋值行保留左侧的键名（键名不是秘密，且抹掉它会让日志失去可读性），其余整段替换。"""
    if match.lastindex == 3:
        return match.group(1) + REDACTED + match.group(3)
    return REDACTED


#: 进程级默认实例：环境变量在进程生命周期内不变，重复构造只是重复扫一遍 os.environ。
_DEFAULT = SecretsRedactor()


def redact_bus_text(text: str) -> str:
    """产物 / 日志两面共用的洗法（`SecretsRedactor` 的模块级门面）。"""
    return _DEFAULT.redact(text)


def redaction_hits(text: str) -> bool:
    return _DEFAULT.hits(text)


class SecretsFilter(logging.Filter):
    """logging 侧的 `secrets` 过滤器（T027 要求的名字就是 `secrets`）。

    改写 `record.msg` 而不是丢弃记录：拦截一条就等于把"曾出现过凭据"这件事本身抹掉了，
    留痕面反过来缺了证据。
    """

    def __init__(self, redactor: SecretsRedactor | None = None) -> None:
        super().__init__(name="secrets")
        self.redactor = redactor or _DEFAULT

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(str(record.getMessage()))
        record.args = ()
        return True


def install_logging_filter(logger: logging.Logger | None = None) -> SecretsFilter:
    target = logger or logging.getLogger()
    existing = next(
        (f for f in target.filters if isinstance(f, SecretsFilter)),
        None,
    )
    if existing is not None:
        return existing
    handler = SecretsFilter()
    target.addFilter(handler)
    return handler


# ── 高危清单（T101｜FR-055 / FR-058，R-15）───────────────────────────────────────

#: 仓库根 = `src/acp/config/loader.py` 往上三层（口径同 `registry/loader.py`）。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 清单的唯一位置。**没有"读不到就退回内置默认"的回退**：内置一份第二清单就是两处口径，
#: 而那份"默认"永远拦得比文件少 —— 文件被误删时的正确表现是报错，不是悄悄放宽。
DANGER_LIST_PATH = REPO_ROOT / "config" / "danger_list.yaml"

#: 清单里唯一合法的动作值（FR-056「一律拦截」+ FR-058「新增项 MUST 默认拦截」）。
#: 它写成常量而不是散落字面量，是因为报错消息要原样引用它 —— 值本身仍只有一个来源。
DANGER_INTERCEPT = "intercept"

#: 三条允许的键。表外键一律拒（同 `registry/loader.py` 的理由：多出来的键没人解析，
#: 那份「清单改了而行为不变」比缺键更难发现）。
DANGER_KEYS = frozenset({"pattern", "action", "user_added"})
DANGER_REQUIRED_KEYS = frozenset({"pattern"})

#: 报错里回显的清单文本最多取这么长（一条被粘进整段脚本的 `pattern` 不该刷满终端）。
_SHOWN_LIMIT = 120


class DangerListError(ValueError):
    """高危清单不合法。失败关闭，不返回半份清单。

    两条回显路径都过脱敏，因为两条都会带出清单原文：
    ① 校验类报错回显 `pattern` 原文（经 `redact_bus_text()` + 截长度）；
    ② 解析类报错带出 PyYAML 摘的**出错那一行**原文（消息洗过，且 `from None` 断掉因果链，
       否则原始异常那份未洗的正文还会出现在 traceback 里）。
    消息里不出现任何环境变量值 —— 清单是可被人扩展的自由文本，直接回显就是给泄漏多开一条路。
    """


def _shown(value: object) -> str:
    """把清单里的原文安全地放进报错消息：先过本模块自己的脱敏，再截长度。"""
    return redact_bus_text(repr(value))[:_SHOWN_LIMIT]


@dataclass(frozen=True)
class DangerEntry:
    """一条高危项。`pattern` 是**归一化后**的词序列原文（连续空白折成单个空格，保留书写大小写）。"""

    pattern: str
    action: str
    user_added: bool

    @property
    def tokens(self) -> tuple[str, ...]:
        """匹配用的词序列（casefold 后）。大小写不敏感是 Windows 侧的要求，见清单头部第 2 条。"""
        return tuple(token.casefold() for token in self.pattern.split())

    @property
    def dedupe_key(self) -> str:
        """重复项判定用的键：词序列相同（忽略大小写与空白写法）即同一条策略。"""
        return " ".join(self.tokens)

    def matches(self, command_tokens: Sequence[str]) -> bool:
        """词序列**连续出现**即命中。

        刻意不做子串匹配：裸子串会让 `sudo` 命中 `/usr/local/sudo_helper/readme.md` 这类路径，
        也会让 `git push` 命中 `git pushy`（那不是同一支命令）。
        也刻意不做"通配 / 正则"：`pattern` 的表达能力一旦放开，一份写坏的清单就能把全部动作判成命中，
        而 `tasks.md` T107 ⑤ 要的正是"不在清单内且可自动回滚的只读动作不被误拦"。
        """
        tokens = self.tokens
        if not tokens or len(tokens) > len(command_tokens):
            return False
        for index in range(len(command_tokens) - len(tokens) + 1):
            if tuple(command_tokens[index : index + len(tokens)]) == tokens:
                return True
        return False


#: P0-2：这些程序把 `-c` 后面的参数当**命令串**执行。匹配前要把内层串重新切词，
#: 否则 `sh -c 'rm -rf /'` 的词序列是 `("sh", "-c", "'rm", "-rf", "/'")`，清单里那条
#: `rm -rf` 永远看不到。`cmd` 用 `/c`，PowerShell 用 `-Command`。
_SHELL_WRAPPERS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "cmd", "powershell", "pwsh"})

#: `psql -c` 的内层是 SQL；报告点名的第二种真实形态（`truncate table` 在清单里）。
_SQL_WRAPPERS = frozenset({"psql"})

#: 内层串最多再拆几层（`sh -c "bash -c 'rm -rf /'"`）。设上限是为了不让构造串把匹配拖成无限递归。
_MAX_WRAPPER_DEPTH = 3


def _split_words(text: str) -> tuple[str, ...]:
    """按 shell 规则切词；引号不平衡时退回空白切分，并把两侧引号剥掉。

    **不用 `str.split()` 是 P0-2 的核心**：`sh -c 'rm -rf /'` 用空白切会得到带引号的
    `"'rm"` / `"/'"`，清单里的 `rm -rf` 就永远匹配不上。`shlex` 去掉引号后内层才是
    `rm -rf /`。反过来，任务文本里常见中文引号与不配对撇号，`shlex` 会抛 `ValueError`
    ⇒ 必须保留一条不抛的退路，否则一条普通需求就能让拦截器自己崩掉。
    """
    try:
        parts = shlex.split(text, posix=True)
    except ValueError:
        parts = [part.strip("'\"") for part in re.findall(r"\S+", text)]
    return tuple(part for part in parts if part)


def _command_name(token: str) -> str:
    """`/bin/sh` / `C:\\Windows\\System32\\cmd.exe` → `sh` / `cmd`（只用于认包装器）。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name[:-4] if name.endswith(".exe") else name


def _inner_commands(tokens: Sequence[str]) -> tuple[str, ...]:
    """从已切好的词里取出 `-c` / `/c` / `-Command` 后面的内层命令串。

    只在**已知包装器**后面认这个旗标：`grep -c "rm -rf" log` 不是命令执行，不能把 grep 的
    计数参数当命令扫 —— 那会把一次正常搜索误拦成 D 类卡点。
    """
    found: list[str] = []
    for index, token in enumerate(tokens):
        name = _command_name(token)
        if name not in _SHELL_WRAPPERS and name not in _SQL_WRAPPERS:
            continue
        for offset in range(index + 1, len(tokens)):
            option = tokens[offset]
            lowered = option.casefold()
            inner: str | None = None
            if lowered in ("-c", "/c", "-command", "--command"):
                if offset + 1 < len(tokens):
                    inner = tokens[offset + 1]
            elif lowered.startswith("--command="):
                inner = option.split("=", 1)[1]
            elif (
                name in _SHELL_WRAPPERS
                and lowered.startswith("-")
                and not lowered.startswith("--")
                and "c" in lowered[1:]
            ):
                # `bash -lc '...'` / `sh -ec '...'`：短旗标合写时 `-c` 仍是执行命令。
                if offset + 1 < len(tokens):
                    inner = tokens[offset + 1]
            elif name == "cmd" and lowered.startswith("/") and "c" in lowered[1:]:
                if offset + 1 < len(tokens):
                    inner = tokens[offset + 1]
            if inner is not None:
                found.append(inner)
                break
    return tuple(found)


def _token_sequences(command: str, *, depth: int = 0) -> tuple[tuple[str, ...], ...]:
    """一条命令 → 一份或多份词序列（外层 + `-c` 内层，最多三层）。

    匹配仍然是**词序列连续出现**（`DangerEntry.matches` 的唯一口径）；这里只负责把
    "被包装器藏起来的那一层命令"也变成可比的词序列，不引入正则或子串匹配。
    """
    words = _split_words(command)
    sequences: list[tuple[str, ...]] = [tuple(word.casefold() for word in words)]
    if depth >= _MAX_WRAPPER_DEPTH:
        return tuple(sequences)
    for inner in _inner_commands(words):
        sequences.extend(_token_sequences(inner, depth=depth + 1))
    return tuple(sequences)


@dataclass(frozen=True)
class DangerList:
    """加载结果。`entries` 保持**文件顺序**（呈现与逐条打印要能对着文件读），
    指纹与匹配都与顺序无关（匹配本来就是逐条独立）。"""

    entries: tuple[DangerEntry, ...]
    fingerprint: str
    source_sha256: str

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(entry.pattern for entry in self.entries)

    @property
    def user_added_count(self) -> int:
        return sum(1 for entry in self.entries if entry.user_added)

    def match(self, command: str) -> tuple[DangerEntry, ...]:
        """返回命中的**全部**条目；空元组 = 不在清单内。

        为什么返回多条而不是第一条：使用者可以加一条更具体的（`git push` 与 `git push --force` 并列），
        此时"命中了哪条"这件事本身就有两个真值。EN-14 的留痕要记的是"当时是哪几条拦住了它"
        （`data-model.md:225`），只报第一条就是丢信息。顺序 = 文件顺序。

        P0-2（2026-10-30）：除了整条命令的词序列，还检查 `sh/bash/zsh/cmd/powershell -c`
        与 `psql -c` 的内层串。清单仍是策略核对而不是完整 shell 解析器 —— 变量拼接
        （`X=rm; $X -rf`）与"写进脚本再执行脚本"仍不在射程，边界写在 `danger_list.yaml` 头部。
        """
        sequences = _token_sequences(command)
        return tuple(entry for entry in self.entries if any(entry.matches(tokens) for tokens in sequences))


def _fingerprint_of(entries: Sequence[DangerEntry]) -> str:
    """内容指纹 = 归一化条目**集合**的哈希（排序后串接 ⇒ 与文件顺序无关；不含注释与空白写法）。

    口径写在 `config/danger_list.yaml` 头部"内容指纹"那一段；改注释 / 调顺序 ⇒ 指纹不变，
    增删改任一条目 ⇒ 指纹必变。两条都由 `tests/contract/test_danger_list.py` 钉着。
    """
    lines = sorted(
        f"{entry.dedupe_key}\t{entry.action}\t{'1' if entry.user_added else '0'}"
        for entry in entries
    )
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_danger_list(path: Path | None = None) -> DangerList:
    """读 `config/danger_list.yaml` → 逐条校形 → 交出带指纹的清单。**每次调用都重新读盘**（无缓存）。

    `path` 只为判据服务（用例要构造"加一条自定义项"与"写坏一处形状"两种文件）；
    生产调用方 MUST 不传，否则就多一个"清单可以指向别处"的可配置面，而那个面一旦被用上，
    EN-16 里的指纹就不再对应 `config/danger_list.yaml` 了。
    """
    target = DANGER_LIST_PATH if path is None else path
    try:
        # `utf-8-sig` 而不是 `utf-8`：PowerShell 5.1 的 `Set-Content` 会给文件加 BOM，
        # 而这份清单的写入者正是使用者本人。BOM 不是语义变更，为一个编码痕迹拒绝加载安全清单
        # 会把"编辑器行为"变成"平台起不来"；用例钉的是**加了 BOM 指纹不变**。
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DangerListError(
            f"高危清单读不出来：{target}（reason={type(exc).__name__}）。"
            "清单是拦截判据的唯一来源，读不到就是「没有清单」⇒ 报错，不退回内置默认"
        ) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # `MarkedYAMLError` 的正文会带上出错那一行的**原文**（含 `>>>` 标记与行列号）。
        # 清单是可被人粘贴的自由文本 ⇒ 这一路也必须过脱敏，且 `from None` 断掉因果链：
        # 留着 `from exc` 时，原始异常自己那份未洗的正文还会出现在 traceback 里。
        # 诊断信息不丢（类型名 + 行列号 + 被洗过的原文仍在消息里）。
        raise DangerListError(
            redact_bus_text(f"{target.name} 解析失败 {type(exc).__name__}: {exc}")
        ) from None
    if not isinstance(raw, list) or not raw:
        raise DangerListError(
            f"{target.name} 顶层必须是非空清单（一条动作一项）。"
            "空清单 = 一份「谁都不拦」的清单，与 FR-056「MUST 一律拦截」的方向相反 ⇒ "
            "空文件按错误处理，不按「默认放行」处理"
        )

    entries: list[DangerEntry] = []
    seen: dict[str, int] = {}
    for ordinal, item in enumerate(raw):
        where = f"第 {ordinal + 1} 条"
        if not isinstance(item, dict):
            raise DangerListError(f"{where} 不是映射：{type(item).__name__}")
        unknown = sorted(set(item) - DANGER_KEYS)
        if unknown:
            raise DangerListError(
                f"{where} 有表外键：{unknown}（表外键是第二份清单的开始，不是扩充）"
            )
        missing = sorted(DANGER_REQUIRED_KEYS - set(item))
        if missing:
            raise DangerListError(f"{where} 缺必填键：{missing}")
        declared = item["pattern"]
        if not isinstance(declared, str) or not declared.strip():
            raise DangerListError(
                f"{where} 的 `pattern` 必须是非空字符串（收到 {_shown(declared)}）"
            )
        pattern = " ".join(declared.split())
        action = item.get("action", DANGER_INTERCEPT)
        if action != DANGER_INTERCEPT:
            raise DangerListError(
                f"{where} 的 `action` 只接受 {DANGER_INTERCEPT!r}（收到 {_shown(action)}）⇒ "
                "清单里没有「放行」这个取值：能在清单里写放行 = 第二条放行分支，"
                "而 FR-056 那句 MUST 就会变成可撤销的承诺（见 tasks.md T104「不存在第二条放行分支」）。"
                "要放行一条被误列的动作，正确的动作是把那条从清单里删掉"
            )
        user_added = item.get("user_added", False)
        if not isinstance(user_added, bool):
            raise DangerListError(
                f"{where} 的 `user_added` 必须是布尔（收到 {_shown(user_added)}）"
                "—— 写成字符串 \"true\" 在 YAML 里是 truthy 但不是布尔，"
                "而「是不是使用者加的」这件事不能靠真值猜"
            )
        entry = DangerEntry(pattern=pattern, action=action, user_added=user_added)
        clash = seen.get(entry.dedupe_key)
        if clash is not None:
            raise DangerListError(
                f"{where} 的 `pattern` 与第 {clash + 1} 条重复：{_shown(entry.pattern)} ⇒ "
                "同一条策略会有两份（大小写或空白写法不同也算）。重复项让「命中条目」这个审计字段"
                "有两种解释，所以拒收而不是合并"
            )
        seen[entry.dedupe_key] = ordinal
        entries.append(entry)

    return DangerList(
        entries=tuple(entries),
        fingerprint=_fingerprint_of(entries),
        # 字节面：解码后正文的哈希（注释与空白写法**算在内**）。EN-16 存的是 `fingerprint`，
        # 这一值只在排障 / CLI 打印时用，它回答的是"当时是那版**文件**"。
        source_sha256="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
