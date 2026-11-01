"""Qoder（SDK / CLI 子进程形态）的 transport（T041｜FR-012，R-01）。

`AgentAdapter` 四支 + 三条 T041 行里 owner 已接受的硬约束（2026-10-11 第八轮日志取证）：
① **监视子进程退出并带 `exit_code` 与 `reason` 立即失败** —— 实测那次 CLI 在启动后 1.48 秒以
   `exit_code=41 reason="auth_error"` 退出，SDK 不向调用方报、`stderr` 为空，调用侧只看见 121 秒后的
   `Control request timeout: initialize`。⇒ 本文件的失败判定读的是**退出码 + 日志里的 `process.exiting` 行**，
   不是"等 SDK 说超时"。
② **`~/.qoder/logs/…` 与 `~/.qoder/projects/<project_id>/<session_id>.jsonl` 纳入失败取证**（那是当时唯一
   有诊断信息的面；第八轮之后这条被加强为"日志 + transcript"）⇒ runner 按 `pid` 定位日志、按 `session_id`
   （= 我们生成的 `run_key`）定位 transcript，进异常消息的只有**路径、`exit_code` 与 reason 词**，
   两份正文一个字都不带（里面有令牌形态的东西，transcript 还是完整对话）。
   （2026-10-24 CN 切换：取证根按解析出的品牌走 —— CN = `~/.qoder-cn/{logs,projects}`，INTL 仍是 `~/.qoder`。）
③ **`access_token_invalid` / `auth_error` 归不可重试**，与"可重试的超时"分列 ⇒
   `QoderTransportError.retryable` 是机器可读的位（`True` / `False` / `None`＝认不出来），
   卡点与成本记账（FR-059 / FR-060）按它分档。
④ **argv[0] 是命令名，不是可执行文件**（owner 2026-10-20 第二发点火的实测）：把 `qodercli` 这个裸名
   直接交给 `Popen`，在"SDK 装了但 CLI 只躺在包内 `_bundled`、PATH 上什么都没有"的机器上当场
   `FileNotFoundError`（WinError 2）。⇒ `resolve_cli()` 把它换成一个真能起的路径，换不动就抛带"试过哪些"
   的拒绝；`SubprocessRunner` 之外的旗标载体仍只有 `QoderCliSpec` 那一个（本文件不改写 argv 的**内容**，
   只回答"这个名字在这台机器上是谁"）。**2026-10-24 owner 裁决**：顺序改成 CN 优先 ——
   ① `QODERCLI_PATH` → ② CN 默认安装位 `~/.qoder-cn/bin/qoderclicn/qoderclicn(.exe)` → ③ PATH `qoderclicn`
   → ④ PATH `qodercn`（`.cmd` dispatcher，最后手段，用上了要 warning）→ ⑤ INTL 包内 `_bundled` →
   ⑥ PATH `qodercli`/`qoder`；CN 档全空而 INTL 命中 ⇒ `fallback_from="cn"`，runner 打一条 `logging.warning`
   （只走日志，绝不 print），且**不因启动/鉴权失败再回退重试**。
⑤ **子进程必须有一个被点名的家**（owner 2026-10-20 第四发点火的实测 + 裁决 1）：那一次的 manifest 里
   `cwd` = 平台仓库根，因为 `Popen` 不接收 `cwd` 时**继承调用方的目录**，而真跑入口是在平台源码根里敲的
   ⇒ 厂商 agent 拿着只读权限把平台自己的源码当成了工作区。现在 `CliRunner.run` 收一个**没有默认值**的
   `cwd`，`confine_cwd()` 在起进程之前拒"不是目录"和"等于或高于平台根"两种形状。
   生产交的那一个家是**存档根下面、执行席那一格的 workspace 目录**（第五发点火的补：家若停在存档根顶层，
   厂商写的文件就落在 `<agent>-workspace/` 外面，collect 侧那条 FR-013 归属判定会把一次成功的执行整批拒收）。
⑥ **撞超时那条消息要能分出"在干活"与"僵住"**（裁决 3）：`tools_in_flight()` 从取证日志里只取
   `enqueued tool_use block "<工具名>"` 那一族的**标识符**，正文一个字不进消息（口径同约束 ②）。

Mock 方式按 owner 2026-10-13 裁决 2：**接口注入 + `FakeRunner`**（与 T040 的 `httpx.MockTransport` 同一条思路，
都是依赖注入）。SDK 是 CLI 子进程封装、没有 HTTP 层可挂，所以注入点是 `CliRunner` 这个 Protocol：
生产用 `SubprocessRunner`（真起子进程），测试用 `FakeRunner`（脚本化响应）⇒ **不装 qodercli、不花 API 费**。

**证据分三档（照 `research.md` D-1 的范围口径，不外推）**：
- **甲 · 真机观测**（第八轮那次真实运行）：最小往返 + initialize 握手 + 结果报文形状（`messages_seen=6`、
  `ResultMessage.subtype=success`、`is_error=False`、CLI `exit_code=0 reason="success"`），
  以及约束 ①②③ 那三条失败面。
- **乙 · 本机安装的 SDK 源码**（`qoder-agent-sdk==1.0.15`，`_cli_version__=1.1.61`，装在 `.venv` 里）：
  argv 旗标、user/result 报文字段名、`usage` 的键、`subtype` 已知取值 —— 逐条在下面 `QoderCliSpec` 里带
  `文件:行` 引用。**这一档只证明"SDK 是这么和 CLI 说话的"，不证明"我们的单向一次跑法能被 CLI 接受"**。
- **丙 · 仍未验**（`NEEDS VERIFICATION`）：产物是怎么被报出来的（CLI 没有"产物清单"这个字段，只能从
  `tool_use` 块里的 `file_path` 反推 ⇒ 哪些工具算写、路径是绝对还是相对都未定）；`--permission-mode
  accept_edits` 在真机上是否真把 **cwd 内**的写放行、同时仍拒 cwd 之外的访问；`query()` 多轮、
  `priority`、`interrupt()`、`StreamEvent` 增量与工具/MCP 报文**既未实现也未验** ⇒ 别把本笔读成
  "SDK 面全通了"。真凭据冒烟那轮要回答的就是丙这一档。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, runtime_checkable

from pydantic import ValidationError

from acp.bus.acl import ADAPTER_OWNER_PREFIX, NO_ROLE_DECLARED, Actor, Capability
from acp.bus.gateway import CARD_LABELS, BusGateway, format_of, measure_artifact
from acp.bus.layout import normalize_rel_path, workspace_dir_name
from acp.growth import store as growth_store
from acp.config.loader import REPO_ROOT, env_value, redact_bus_text
from acp.protocol import CapabilityManifest, Handle, Task, TaskResult
from acp.protocol.messages import (
    ArtifactRef,
    CUSTOMER_INPUT_CLOSE,
    CUSTOMER_INPUT_OPEN,
    UNTRUSTED_INPUT_NOTE,
)
from acp.protocol.refs import UsageNotice
from acp.registry.loader import load_registrations

logger = logging.getLogger(__name__)

AGENT_ID = "qoder"

#: 包名 / 导入名 = `config/params-ref.md §4` 那两行（§4 是人读的清单，这里是被跑的那份）；
#: 与本机安装的 `qoder-agent-sdk==1.0.15` 的 `METADATA` / `CLI_COMMANDS` 对得上（证据乙档）。
PACKAGE_NAME = "qoder-agent-sdk"
IMPORT_NAME = "qoder_agent_sdk"

#: 两个品牌各自的 CLI 命令名（`_brand.py:9` 的两个分支，证据乙档）。
#: - global 分支：`("qodercli", "qoder")`；
#: - cn 分支：`("qoderclicn", "qodercn")` —— 本机实测 `qoderclicn` 是原生 exe，
#:   `qodercn` 是 `.cmd` dispatcher 包装器（最后手段，用上了要 warning）。
#: 两份名单与本机装的那份 SDK 的符合性由 `tests/guards/test_qoder_cli_lookup.py` 钉住（改包名/改品牌就红）。
INTL_CLI_BINARY = "qodercli"
INTL_CLI_COMMAND_NAMES = (INTL_CLI_BINARY, "qoder")
CN_CLI_BINARY = "qoderclicn"
CN_CLI_WRAPPER = "qodercn"
CN_CLI_COMMAND_NAMES = (CN_CLI_BINARY, CN_CLI_WRAPPER)

#: `QoderCliSpec.argv[0]` 用的占位命令名。它**不是**最终启动的可执行文件：
#: `SubprocessRunner` 在起进程前会把它换成解析出的绝对路径（见 `_launch_argv`）。
CLI_BINARY = INTL_CLI_BINARY

#: 两个品牌各自的 CLI 版本（`qodercli --version` / `qoderclicn --version` 的实测读数）。
INTL_CLI_VERSION_EXPECTED = "1.1.61"
CN_CLI_VERSION_EXPECTED = "1.1.78"

# --- 可执行文件的查找（owner 2026-10-20 第二发点火撞到的 `FileNotFoundError`；2026-10-24 加 CN 档）---

#: 显式指定可执行文件的环境变量名（`subprocess_cli.py:186`）。它装的是**路径**不是凭据 ⇒ 可以进消息。
CLI_PATH_ENV = "QODERCLI_PATH"
#: SDK 包内自带 CLI 的那个目录名（`subprocess_cli.py:224`）。
BUNDLED_DIR_NAME = "_bundled"
#: Windows 上要带 `.exe` 才找得到（`subprocess_cli.py:226` 那个三元表达式的两个分支）。
WINDOWS_SYSTEM_NAME = "Windows"
WINDOWS_EXECUTABLE_SUFFIX = ".exe"

#: 两个品牌各自的用户配置目录名（`_brand.py:10` 的两个分支）。
#: 日志与 transcript 都挂在它下面：CN 写 `~/.qoder-cn`，INTL 写 `~/.qoder`。
INTL_CONFIG_DIR_NAME = ".qoder"
CN_CONFIG_DIR_NAME = ".qoder-cn"

#: 品牌标签（`ResolvedCli.flavor` 与 `RunOutcome.cli_flavor` 的取值域）。
Flavor = Literal["cn", "intl"]
CN_FLAVOR: Flavor = "cn"
INTL_FLAVOR: Flavor = "intl"

#: 凭据变量名（乙档）。§4 写"Service Account（推荐）/ PAT（备选）/ 本机 qodercli 登录态（仅本地开发）"。
#: 本机登录态不在支持范围：平台要的是可追踪的注入，不是继承某个人的 IDE 会话。
#: - CN 主变量 = `QODERCN_PERSONAL_ACCESS_TOKEN`（owner 2026-10-24 裁决 1）；
#: - INTL 变量 = `QODER_PERSONAL_ACCESS_TOKEN` —— 它同时是 CN CLI 二进制内嵌文档里写的那个名字，
#:   所以 CN 运行时子环境会**同时**带上两个名字（同一个值），见 `SubprocessRunner._child_env`。
SERVICE_ACCOUNT_ENV = "QODER_SERVICE_ACCOUNT_KEY"
CN_PAT_ENV = "QODERCN_PERSONAL_ACCESS_TOKEN"
INTL_PAT_ENV = "QODER_PERSONAL_ACCESS_TOKEN"
CREDENTIAL_ENV_NAMES = (CN_PAT_ENV, INTL_PAT_ENV, SERVICE_ACCOUNT_ENV)

#: "这是 SDK 在起 CLI"的身份标记（厂商 `subprocess_cli.py:724-749` 自己设的那一组）。
#: 本适配器是**直起 CLI**：这些标记一旦从父环境继承进来，CLI 会判定 `mode="sdk"`、
#: 硬要 `QODER_SDK_AUTH_PAYLOAD_FILE` 指向的 payload 文件（IDE 注入的那个 temp 文件常常已失效），
#: 我们注入的 PAT 根本没机会被读。2026-10-24 冒烟实测（同一套旗标、同一行 stdin）：
#: 继承该组 = `exit 41 / auth_payload_invalid|missing`；摘掉 = `result subtype=success`。
#: 子环境一律摘掉 —— 这一组只说明"父进程是 SDK/IDE"，不说明这台机器该怎么认证。
SDK_IDENTITY_ENV_NAMES = (
    "QODER_SDK_AUTH_PAYLOAD_FILE",
    "QODER_AGENT_SDK_ENTRYPOINT",
    "QODER_ENTRYPOINT",
    "QODER_AGENT_SDK_VERSION",
    "QODER_SDK_VERSION",
    "QODER_AGENT_SDK_DAEMON",
)

#: 2026-10-29 P0-1：子进程**只**拿这些系统变量。此前这里做的是"父环境整份复制 − SDK 标记"，
#: 实测把 `ACP_DB_DSN` / `ACP_PG_PASSWORD` / `ARK_API_KEY` / `WORKBUDDY_*` 一起交给了外部 CLI，
#: 而 CLI 自己的 logs/memory/file-history 会把它们记成明文。白名单是唯一不让凭据跟着环境走的口径：
#: 加一个名字要同时说明"为什么执行器需要它"，不是"以前有所以现在也得有"。
CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

#: 子进程的 HOME 必须落在一次运行自己的临时目录里：全局 `~/.qoder[-cn]` 下既有厂商登录态
#: `~/.qoder/.auth/user`，也有会回灌口令的 `logs/`、`memory/`、`file-history/`。
#: 只改 env 不够 —— `HOME` / `USERPROFILE` 本身也要指过去，CLI 才会把家目录当成这一格。
CHILD_HOME_PREFIX = "acp-qoder-home-"

#: P1-13：超时/失败时杀**整棵**进程树。`Popen.kill()` 只杀直接子进程，CLI 自己起的
#: 孙进程（qoderclicn 的 worker）会留下来锁住 workspace 里的文件，下一次跑就撞"文件被占用"。
#: Windows 用 `taskkill /T`（Job Object 的等价物，且不依赖 pywin32）；POSIX 用进程组。
IS_WINDOWS = os.name == "nt"


def _kill_process_tree(
    process,
    *,
    is_windows: bool = IS_WINDOWS,
    runner=subprocess.run,
) -> None:
    """尽力杀整棵树，最后仍兜底 `process.kill()`；任何一步失败都不掩盖原始超时/失败。"""
    pid = getattr(process, "pid", None)
    if pid:
        try:
            if is_windows:
                runner(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
            else:
                # POSIX 分支。`os.killpg` / `os.getpgid` / `signal.SIGKILL` 在 Windows 的
                # typeshed 里**根本不存在**，直接写三行就是三条 `Module has no attribute`
                # —— 而这条分支恰恰只在 POSIX 跑。用 `getattr` 取并显式标注，既让类型检查
                # 看得到"这是平台能力"，也不把 `Any` 带进来。
                killpg: Callable[[int, int], None] | None = getattr(os, "killpg", None)
                getpgid: Callable[[int], int] | None = getattr(os, "getpgid", None)
                sigkill: int | None = getattr(signal, "SIGKILL", None)
                if killpg is not None and getpgid is not None and sigkill is not None:
                    killpg(getpgid(pid), sigkill)
        except Exception:  # noqa: BLE001 - 杀树失败不改变"这次已经超时"的事实
            pass
    try:
        process.kill()
    except Exception:  # noqa: BLE001 - 进程可能已经退出
        pass

#: 日志面（约束 ②）。实测形态：`logs/latest` → `logs/runs/<时间戳>-p<pid>/{qodercli.log, manifest.json}`。
#: **不跟 `latest`**：它是符号链接，并发跑两张卡时会拿到"别人的那一次"⇒ 一律按 `pid` 定位。
RUNS_DIR_NAME = "runs"
LOG_FILE_NAME = "qodercli.log"

#: 约束 ② 的第二面（第八轮之后加强为"日志 + transcript"）：
#: `~/.qoder[-cn]/projects/<project_id>/<session_id>.jsonl`。`<project_id>` 我们不掌握，但
#: `session_id` 就是我们发下去的 `run_key`（`_stdin_line()` 那么写的理由在这里兑现）⇒ 可以按会话反查，
#: 不需要猜目录名。**同样只取路径进错误消息，正文一个字不带**（transcript 里是完整对话，比日志更敏感）。
PROJECTS_DIR_NAME = "projects"
TRANSCRIPT_SUFFIX = ".jsonl"

EXIT_LINE = re.compile(r'process\.exiting exit_code=(?P<code>-?\d+) reason="(?P<reason>[^"]*)"')

#: "最后一轮在跑什么工具"的取证读点（owner 2026-10-20 裁决 3）。字符类**故意收到最窄**：
#: 只吃 `[A-Za-z0-9_-]` 的工具名，所以引号里若混进正文（路径、对话片段、令牌形态的串）一律不匹配 ⇒
#: 结构行进消息，正文进不了消息。锚点串取真机那份日志的原文（甲档：2026-10-20 第四发点火的
#: `qodercli.log`，形如 `[AgentLoop] Streaming: enqueued tool_use block "Bash" (id=call_…)`）。
TOOL_LINE = re.compile(r'enqueued tool_use block "(?P<tool>[A-Za-z0-9_-]{1,40})"')

#: 约束 ③ 的两档。**确定性失败**重试只会更慢（23:12 那轮已证：抬高超时收益为零）。
NON_RETRYABLE_REASONS = frozenset({"auth_error", "access_token_invalid", "invalid_request_error"})
RETRYABLE_REASONS = frozenset({"timeout", "control_request_timeout"})

#: 厂商 stdout 原样落到**自己的 workspace**，`collect()` 从盘上重查。
#: 为什么落盘而不是留在内存：`collect()` 只拿得到 `Handle`（进程重启后内存里什么都没有），
#: 而契约 §4.1 说 `native_ref` 是"厂商侧可重查标识" ⇒ 可重查的必须是盘上的东西。
RUNS_SUBDIR = ".acp-runs"

Retryable = Literal[True, False, None]


class QoderTransportError(RuntimeError):
    """transport 层失败。消息里只有 `exit_code` / reason 词 / 日志**路径** / 字段名，MUST NOT 有凭据值或日志正文。"""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        reason: str | None = None,
        retryable: Retryable = None,
        log_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.reason = reason
        self.retryable = retryable
        self.log_path = log_path


@dataclass(frozen=True)
class RunOutcome:
    """一次子进程运行的实况。`reason` 取日志里那行 `process.exiting`（实测那次 `stderr` 是空的）。

    `log_path` / `transcript_path` 是约束 ② 的两面取证**路径**（正文不进本对象，更不进异常消息）。
    `cli_flavor` / `cli_fallback_from` 记的是"这一次真的起的是哪一支"（2026-10-24 CN 切换）：
    失败消息与运行记录据此分得开 CN / INTL，不靠猜。
    """

    exit_code: int
    reason: str | None
    stdout: str
    stderr: str
    log_path: str | None
    timed_out: bool = False
    transcript_path: str | None = None
    cli_flavor: Flavor | None = None
    cli_fallback_from: Flavor | None = None


@runtime_checkable
class CliRunner(Protocol):
    """注入点（owner 裁决 2）。两个方法都按 `run_key` 说话，因为 `cancel()` 手上只有 `Handle`。

    `cwd` **没有默认值**（owner 2026-10-20 裁决：选甲）。它是厂商子进程的家。缺省不是"没有工作目录"，
    而是"`Popen` 继承调用方的 cwd"—— 而真跑入口 `python -m acp.realrun` 恰恰是在平台源码根里敲的，
    第四发点火的 manifest 因此把 `cwd` 记成了平台仓库根：厂商 agent 拿着只读权限把**平台自己的源码**
    当成了工作区。所以这一格必须被点名交出：生产那一发的值 = `archive.require_archive_root(project_key)`
    （总线项目目录，权威出处 = `config/params-ref.md §10`），落在 `SubprocessRunner.run` 里再验一次形状。
    """

    def run(
        self,
        *,
        argv: tuple[str, ...],
        stdin_text: str,
        run_key: str,
        timeout_seconds: float,
        cwd: Path | str,
    ) -> RunOutcome: ...

    def abort(self, run_key: str) -> bool: ...


@dataclass(frozen=True)
class QoderCliSpec:
    """CLI 线协议字面量的集中处。每个字段后面标的是它的**证据档**：

    - **乙** = 读自本机安装的 `qoder-agent-sdk==1.0.15` 源码（`文件:行` 已注明）。它证明"SDK 是这么和
      CLI 说话的"，**不**证明"我们的单向一次跑法能被 CLI 接受"。
    - **丙** = 未验（`NEEDS VERIFICATION`），真凭据冒烟那轮要回答的就是这些。

    集中成可注入对象不为"将来换厂商"，而是用例必须能换一份 spec 来证明实现真在按它解析，
    而不是把字符串焊在逻辑里（T040 的同一条纪律）。
    """

    #: 乙：`_internal/transport/subprocess_cli.py:405-420` 的 `_build_command()` 前六项就是这个序列
    #: （`--print` + 两个 `*-format stream-json`）。真实旗标一个都不发明。
    #:
    #: `--permission-mode accept_edits` 是 owner 2026-10-21 那发真实点火之后的裁决（`qodercli.log` 原话：
    #: `This action needs approval, and the "Don't ask" permission mode does not prompt.`）：原值 `dontAsk`
    #: 是"从不提问、需要审批的一律拒"⇒ `Write` 在落盘前就被 CLI 拦掉，模型只把 CSS 写在了回复正文里。
    #: 现取值是 CLI `--help` 里的规范拼写（`default, accept_edits, bypass_permissions, dont_ask, auto`；
    #: SDK `subprocess_cli.py:98-110` 也把它映射成 `accept_edits`，驼峰 `acceptEdits` 只是别名）：
    #: **只自动放行工作目录内的安全文件编辑**，工作目录之外的访问与敏感路径仍走原来的检查。
    #:
    #: 为什么不是另外三支（同一条"只许在 cwd 里自由读写、不许越权到整个系统"的红线）：
    #: `bypass_permissions` / `yolo` / `--dangerously-skip-permissions` 是**全系统**跳过审批面 ⇒ 拒；
    #: `auto` 多一层 AI 分类器、放行面上界说不清 ⇒ 不取；`--add-dir` 会把信任面扩到 cwd 之外 ⇒ 不加。
    #: 单次跑法里仍有工具会因"无人可问"被拒（如 cwd 外的 `Glob`、需要确认的 shell），这是有意的：
    #: 要的是产物能落进 `qoder-workspace`，不是把审批面整体拆掉。
    argv: tuple[str, ...] = (
        CLI_BINARY,
        "--print",
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--permission-mode",
        "accept_edits",
    )

    # --- 报文信封（乙：`_internal/message_parser.py:57` 用 `data.get("type")` 做判别）---
    event_type_field: str = "type"
    result_type: str = "result"  # message_parser.py:286
    assistant_type: str = "assistant"  # message_parser.py:110
    user_type: str = "user"  # client.py:298
    control_request_type: str = "control_request"  # query.py:974

    # --- user 报文（乙：`client.py:296-301` 与 `_internal/client.py:222-228` 的三件套）---
    message_field: str = "message"
    content_field: str = "content"
    session_id_field: str = "session_id"
    parent_tool_use_field: str = "parent_tool_use_id"
    text_block_type: str = "text"  # message_parser.py:112-114 的 `{"type":"text","text":…}`
    text_field: str = "text"

    # --- 结果报文（乙：`types.py:2211-2232` 的 `ResultMessage`）---
    result_subtype_field: str = "subtype"
    result_text_field: str = "result"
    error_flag_field: str = "is_error"
    success_subtype: str = "success"
    #: 乙档里唯一见到的失败子型（`_internal/cloud_agent.py:762`）。列它不是为了"认出所有失败"，
    #: 而是给 `_verdict()` 第二个独立信号；除此之外的子型一律**抛**，不猜。
    failure_subtypes: tuple[str, ...] = ("error_during_execution",)

    # --- 产物发现（丙档①：`ResultMessage` 的 14 个字段里没有一个列文件 ⇒ 只能从 `tool_use` 块反推）---
    tool_use_block_type: str = "tool_use"  # 块形状是乙档（message_parser.py:74-78 的 id/name/input）
    tool_name_field: str = "name"
    tool_input_field: str = "input"
    #: **未验**：哪些工具名算"写文件"。白名单方向是刻意选的 —— 名单漏了会**少报**产物，
    #: 少报会被 `ValidateArtifact` 的产物下限判成 `invalid`（响）；反过来"任何带路径的工具都算写"会
    #: 把 `Read` 过的文件登记成交付物（不响）。宁要响，不要静默虚报。
    writing_tools: tuple[str, ...] = ("Write", "Edit", "MultiEdit")
    artifact_path_field: str = "file_path"

    # --- 用量（乙：键名 = `types.py:1636-1657` 的 `NonNullableUsage`）---
    usage_field: str = "usage"  # message_parser.py:297
    usage_input_field: str = "input_tokens"
    usage_output_field: str = "output_tokens"
    #: `research.md` D-1 的挂账：真机那次 `usage` 的 input/output tokens 全为 0、`total_cost_usd=0`
    #: ⇒ `ResultMessage.usage` **不可当可信计量源**。默认 `False` 的含义是"不落 EN-13，但把厂商给的数
    #: 原样写进 `TaskResult.issues` 留证"。替代计量点定了（候选 (a) 读 CLI 本地日志 / (b) 例外程序豁免）
    #: 之后再翻这个位 —— 翻它是一次有主人的改动，不是顺手。
    trust_usage: bool = False

    #: 一次外部执行的等待上限。**owner 2026-10-20 裁决 = 900 s（15 分钟）**，载体落在
    #: `config/params-ref.md §1` 的"非卡点执行超时"那一行，两侧读数由
    #: `tests/contract/test_qoder_transport.py::test_the_run_timeout_has_one_number_on_both_sides` 对读。
    #: 为什么原来那个 120 s 是错的：第四发点火的真机日志显示 CLI 在**正常多轮干活**（19 轮 / 29 个工具块，
    #: 约 70 s 时还在推进），120 s 这道闸把一个在跑的会话当成僵死杀掉了 —— 它量的是"initialize 握手要多久"
    #: （第八轮那次的 121 s），不是"一张卡跑完要多久"。
    #: 仍要如实说的是另一半：这一格的值在代码里、不在 `config/runtime.yaml` 那个能读的面里 ⇒ 改它要动代码，
    #: 与 §1 末句"MUST 可配置"相冲的那半条账还挂着（原文见 `params-ref §1` 的 2026-10-13 挂账块）。
    timeout_seconds: float = 900.0


def _sdk_package_dir() -> Path | None:
    """装着 `qoder_agent_sdk` 的那个目录。包没装 ⇒ `None`（在这里抛就没人能读出"没装"那一句话）。"""
    spec = importlib.util.find_spec(IMPORT_NAME)
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def bundled_cli_dir(package_dir: Path | PurePosixPath | None = None) -> Path | None:
    """SDK 包内自带 CLI 的那个目录（`subprocess_cli.py:224`）。包没装 ⇒ `None`。

    这里**不看它在不在**：调用方要么拿它去逐个试文件，要么把它原样抄进拒绝消息里当"查过哪里"的读数。
    """
    root = Path(package_dir) if package_dir is not None else _sdk_package_dir()
    return None if root is None else root / BUNDLED_DIR_NAME


@dataclass(frozen=True)
class ResolvedCli:
    """一次 CLI 解析的结果：可执行文件 + 品牌 + "是否是从 CN 降级来的"。

    `fallback_from="cn"` 是 `SubprocessRunner` 打回退 warning 的唯一触发条件（owner 2026-10-24 裁决 3）：
    `QODERCLI_PATH` / 注入的 `cli_path` 显式命中时它是 `None` —— 那是人工选择，不是降级。
    """

    executable: str
    flavor: Flavor
    fallback_from: Flavor | None = None


def _platform_suffix(system_name: str | None = None) -> str:
    name = platform.system() if system_name is None else system_name
    return WINDOWS_EXECUTABLE_SUFFIX if name == WINDOWS_SYSTEM_NAME else ""


def _flavor_of_path(path: str | Path) -> Flavor:
    """按文件名认品牌。`QODERCLI_PATH` 指到别的名字时只能按这条判；重命名过的 exe 会被当成 INTL。"""
    stem = Path(str(path)).stem.casefold()
    if stem.startswith(CN_CLI_BINARY) or stem.startswith(CN_CLI_WRAPPER):
        return CN_FLAVOR
    return INTL_FLAVOR


def cn_install_path(home: Path | PurePosixPath, suffix: str) -> Path:
    """CN 默认安装位：`<home>/.qoder-cn/bin/qoderclicn/qoderclicn(.exe)`（本机实测的形状）。"""
    return Path(home) / CN_CONFIG_DIR_NAME / "bin" / CN_CLI_BINARY / f"{CN_CLI_BINARY}{suffix}"


def cn_probe_points(home: Path | PurePosixPath, suffix: str) -> tuple[str, ...]:
    """CN 档的探测点（拒绝消息与回退 warning 共用一处，不各写一份名单）。"""
    return (
        str(cn_install_path(home, suffix)),
        *(f"PATH 上的 {name}" for name in CN_CLI_COMMAND_NAMES),
    )


def flavor_log_root(flavor: Flavor, home: Path | PurePosixPath) -> Path:
    """该品牌的日志根：`<home>/.qoder[-cn]/logs`（约束 ② 的第一面）。"""
    name = CN_CONFIG_DIR_NAME if flavor == CN_FLAVOR else INTL_CONFIG_DIR_NAME
    return Path(home) / name / "logs"


def flavor_projects_root(flavor: Flavor, home: Path | PurePosixPath) -> Path:
    """该品牌的 transcript 根：`<home>/.qoder[-cn]/projects`（约束 ② 的第二面）。"""
    name = CN_CONFIG_DIR_NAME if flavor == CN_FLAVOR else INTL_CONFIG_DIR_NAME
    return Path(home) / name / "projects"


def _select_pat(source: Mapping[str, str], flavor: Flavor) -> tuple[str | None, str | None]:
    """按品牌挑 PAT：CN 先 `QODERCN_...`，INTL 先 `QODER_...`；都空 ⇒ `(None, None)`。"""
    order = (CN_PAT_ENV, INTL_PAT_ENV) if flavor == CN_FLAVOR else (INTL_PAT_ENV, CN_PAT_ENV)
    for name in order:
        value = source.get(name, "")
        if value.strip():
            return name, value
    return None, None


def resolve_cli(
    *,
    env: Mapping[str, str] | None = None,
    package_dir: Path | PurePosixPath | None = None,
    which: Callable[[str], str | None] = shutil.which,
    system_name: str | None = None,
    home: Path | PurePosixPath | None = None,
) -> ResolvedCli:
    """找出"这台机器上真能起的那个 CLI"，交回可执行文件 + 品牌 + 是否从 CN 降级。

    为什么要我们自己走一遍：`QoderCliSpec.argv[0]` 是**命令名**（`qodercli`），裸名只有在它被装到
    PATH 上的机器才起得来。owner 2026-10-20 那一发在这台 Windows 上的实况 = PATH 上两个名字都查不到，
    而 SDK 包里躺着 `_bundled/qodercli.exe` ⇒ `Popen(['qodercli', …])` 当场 `FileNotFoundError`
    （WinError 2）。那一抛是 `OSError` 的后裔，既不在 `SWALLOWABLE` 也不在任何拒绝分支里 ⇒
    终端上是一段 traceback 顶掉了 `RUN FAIL`，而厂商侧一个字都没发生（它根本没被起来）。

    2026-10-24 owner 裁决后的顺序（CN 优先；①~④ 全空才轮到国际版）：
    ① `QODERCLI_PATH` 有值就**原样**交回（不校验存在性 —— 与厂商 `subprocess_cli.py:186` 同口径；
       品牌按文件名认）；
    ② CN 默认安装位 `<home>/.qoder-cn/bin/qoderclicn/qoderclicn(.exe)`；
    ③ PATH 上的 `qoderclicn`（PATHEXT 下原生 `.exe` 优先）；
    ④ PATH 上的 `qodercn`（本机是 `.cmd` dispatcher 包装器，最后手段）；
    ⑤ 包内 `_bundled`（`:221-231`，Windows 上带 `.exe`）—— 厂商自己的第二档，仅 INTL；
    ⑥ `shutil.which(command)` 按 `INTL_CLI_COMMAND_NAMES` 逐个试（`:195-196`）。
    ③④ 全空而 ⑤⑥ 命中 ⇒ `fallback_from=CN_FLAVOR`（runner 据此打一条 warning）。
    厂商在 ⑥ 之后还有"五个固定目录挨个 `exists()`"那一段（`:199-212`：npm-global / `/usr/local/bin` /
    `.local/bin` / `node_modules/.bin` / `.yarn/bin`）。**本实现不搬那一档**：那五条路径在本机（Windows
    与 WSL 两侧）一条都不存在，搬进来等于加一段今天无法证伪的代码 ⇒ 作为未闭登记，不当"已全对齐"。

    全空 ⇒ 抛 `QoderTransportError`（`reason` = `cli_not_found`）。消息里点名**两个品牌**试过的命令名、
    环境变量名、查过的目录和一条人能直接执行的下一步；不回显厂商那句原文，也不替谁下载安装。
    """
    source = os.environ if env is None else env
    home_dir = Path.home() if home is None else Path(home)
    suffix = _platform_suffix(system_name)

    explicit = source.get(CLI_PATH_ENV, "")
    if explicit:
        return ResolvedCli(str(explicit), _flavor_of_path(explicit), None)

    install = cn_install_path(home_dir, suffix)
    if install.is_file():
        return ResolvedCli(str(install), CN_FLAVOR, None)

    for command in CN_CLI_COMMAND_NAMES:
        found = which(command)
        if found:
            return ResolvedCli(found, CN_FLAVOR, None)

    bundled = bundled_cli_dir(package_dir)
    if bundled is not None:
        for command in INTL_CLI_COMMAND_NAMES:
            candidate = bundled / f"{command}{suffix}"
            if candidate.is_file():
                return ResolvedCli(str(candidate), INTL_FLAVOR, CN_FLAVOR)

    for command in INTL_CLI_COMMAND_NAMES:
        found = which(command)
        if found:
            return ResolvedCli(found, INTL_FLAVOR, CN_FLAVOR)

    raise QoderTransportError(
        f"这台机器上找不到 Qoder CLI（国内版优先）：环境变量 {CLI_PATH_ENV} 没有值，"
        f"CN 默认安装位 {install} 不存在，"
        f"PATH 上按 {'、'.join(CN_CLI_COMMAND_NAMES)} 查过也没有；"
        + (
            f"包内目录 {str(bundled)} 里也没有 "
            f"{'、'.join(f'{item}{suffix}' for item in INTL_CLI_COMMAND_NAMES)}"
            if bundled is not None
            else f"包 {PACKAGE_NAME} 没装 ⇒ 没有 {BUNDLED_DIR_NAME} 目录可查"
        )
        + f"；PATH 上按 {'、'.join(INTL_CLI_COMMAND_NAMES)} 查过也没有"
        f"（Windows 上文件名要带 {WINDOWS_EXECUTABLE_SUFFIX}）。下一步二选一："
        f"把国内版 CLI 的绝对路径写进环境变量 {CLI_PATH_ENV}（默认安装位 = {install}），"
        f"或在本机装/启用带 {BUNDLED_DIR_NAME} 的 {PACKAGE_NAME}。本入口不替你下载，"
        f"也不降级成仿真（owner 2026-10-19：禁止静默降级为 stub）",
        reason="cli_not_found",
        retryable=False,
    )


def resolve_cli_executable(
    *,
    env: Mapping[str, str] | None = None,
    package_dir: Path | PurePosixPath | None = None,
    which: Callable[[str], str | None] = shutil.which,
    system_name: str | None = None,
    home: Path | PurePosixPath | None = None,
) -> str:
    """`resolve_cli()` 的字符串门面：只关心"哪个文件"的调用方与旧判据继续用这一支。"""
    return resolve_cli(
        env=env, package_dir=package_dir, which=which, system_name=system_name, home=home
    ).executable


def confine_cwd(cwd: Path | str) -> Path:
    """起进程之前把"厂商子进程的家"判掉。两种坏法各一个原因词，都不许降级成"那就换一个目录"。

    ① **不是已存在的目录** ⇒ `cwd_not_a_directory`。坏 cwd 交给 `Popen` 也是 `OSError`，但那一条会被
        下面那个 `except OSError` 换成 `cli_launch_failed` ⇒ 把"配置没给对目录"报成"这个名字不是能起的
        程序"，归因错一层，人照着消息去查 CLI，永远查不到。先在这里判，是为了让原因词落在错的层上。
    ② **等于平台源码根，或高在它上面（祖先）** ⇒ `cwd_is_platform_repo`。这一格防的就是第四发点火的现场：
        那一次的 manifest 把 `cwd` 记成平台仓库根，于是厂商 agent 拿着只读权限把**平台自己的源码**当成工作区。
        判的是**解析后的路径**而不是目录名 —— 拿名字猜会被 `..` 那种写法绕过去（口径同
        `realrun._refuse_if_platform_repo`：那一发拦在"建根之前"，这一发拦在"起进程之前"）。

    存档根**嵌在**平台仓库里（默认的 `results/<project>` 就是这种）是**合法的**：这里判的是"等不等于或高于根"，
    不是"在不在根里面"。真把厂商关在外面的是另一头 —— `archive.require_archive_root()` 要求那个目录自己是
    **仓库顶层**，于是它执行的 `git` 打不到平台那棵树。两道各管一段，谁都不替代谁。
    """
    target = Path(str(cwd))
    if not target.is_dir():
        raise QoderTransportError(
            f"厂商子进程的工作目录 {str(target)!r} 不是一个已存在的目录 ⇒ 拒发。这一格没有默认值："
            "`Popen` 缺省继承调用方的 cwd，而真跑入口恰恰是在平台仓库根里敲的（owner 2026-10-20 裁决：把 AI"
            " 关在它自己的 workspace 里）。生产那一发的值 = `archive.require_archive_root(project_key)`，"
            "解析优先级与权威出处见 `config/params-ref.md §10`",
            reason="cwd_not_a_directory",
            retryable=False,
        )
    resolved = target.resolve()
    repo = REPO_ROOT.resolve()
    if resolved == repo or resolved in repo.parents:
        raise QoderTransportError(
            f"厂商子进程的工作目录解析成 {str(resolved)!r}，它等于或高于平台源码根 "
            f"{str(repo)!r} ⇒ 拒发：那等于把平台自己的源码交给一次外部执行去读写"
            "（第四发点火撞的就是这一格）。该交的是存档根 = `<bus_root>/<project_key>`，"
            "见 `config/params-ref.md §10`",
            reason="cwd_is_platform_repo",
            retryable=False,
        )
    return resolved


class SubprocessRunner:
    """生产实现：真起子进程。**不用 `shell=True`**（argv 传列表 ⇒ 没有命令注入面，也不吃 PATH 解析差异）。

    argv 的**内容**仍由 `QoderCliSpec` 一个载体说了算（本类不改旗标），只在交给 `Popen` 之前把
    第一参数"命令名 → 这台机器上真能起的那个可执行文件"换掉 —— 见 `cli_path`（2026-10-24 后换的是
    `resolve_cli()` 解析出的 CN/INTL 路径 + 品牌）。
    argv 之外的第二格是**家**：`cwd` 在交给 `Popen` 之前由 `confine_cwd()` 判一遍，这一层是最后一道，
    因为它是唯一一个真的起进程的地方（上游谁注了什么 runner 都一样）。
    第三格是**子环境**：`env=` 给的是父环境副本（**摘掉 SDK 身份标记**，否则 CLI 会改走 payload 文件
    认证而完全不看我们注入的 PAT）＋ 把该品牌的 PAT 映射到两个名字下（同值，`_child_env()`）——
    只做环境 → 环境的键值映射，不落文件、不写日志。
    """

    def __init__(
        self,
        *,
        log_root: Path | PurePosixPath | None = None,
        projects_root: Path | PurePosixPath | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        cli_path: str | None = None,
        env: Mapping[str, str] | None = None,
        home: Path | PurePosixPath | None = None,
    ) -> None:
        #: `None` = 按解析出的 flavor 落到 `<home>/.qoder[-cn]/{logs,projects}`；显式给了就只认它。
        self._log_root = None if log_root is None else Path(log_root)
        self._projects_root = None if projects_root is None else Path(projects_root)
        self._popen = popen
        #: `None` = 到 `run()` 那一刻才去查（`resolve_cli()`）；给了值就原样用（品牌按文件名认）。
        #: 查这件事**故意不放在 `__init__`**：构造一个 runner 不该读盘 —— 工厂造 transport 是每发都做的
        #: 动作，也把用例常用的"造一个生产 runner 验形状"变成了依赖真机的动作。
        self._cli_path = cli_path
        #: 子环境与 token 的来源。`None` = 运行时现取 `os.environ`（不在构造点拷一份快照）。
        self._env_source = env
        #: 取证根的 home（默认 `Path.home()`）；判据注入 `tmp_path`，不读用户主目录。
        self._home = None if home is None else Path(home)
        self._live: dict[str, subprocess.Popen[str]] = {}

    def run(
        self,
        *,
        argv: tuple[str, ...],
        stdin_text: str,
        run_key: str,
        timeout_seconds: float,
        cwd: Path | str,
    ) -> RunOutcome:
        home = confine_cwd(cwd)
        resolved = self._resolve_cli()
        self._warn_on_resolution(resolved)
        launch = self._launch_argv(resolved, argv)
        # P0-1：日志/transcript 的取证根跟着**这一次运行的临时 HOME**走。
        # 例外：`home=` 是判据注入的取证根（测试用），显式给了就只认它；子进程的 HOME
        # 仍然是 `child_home`，两者不要混成同一个变量。
        child_home = self._new_child_home(run_key)
        home_dir = self._home_dir() if self._home is not None else child_home
        log_root = self._log_root or flavor_log_root(resolved.flavor, home_dir)
        projects_root = self._projects_root or flavor_projects_root(resolved.flavor, home_dir)
        child_env = self._child_env(resolved.flavor, home=child_home)
        try:
            process = self._popen(
                launch,
                cwd=str(home),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                # P1-13：POSIX 下把 CLI 放进自己的进程组，超时才能按组杀干净；
                # Windows 用 taskkill /T，不依赖这个参数。
                start_new_session=not IS_WINDOWS,
            )
        except OSError as exc:
            raise QoderTransportError(
                f"`Popen` 起不了 {launch[0]!r}：{type(exc).__name__}"
                f"（winerror={getattr(exc, 'winerror', None)}）"
                f"⇒ 这个名字/路径在这台机器上不是一个能起的程序。它来自注入的 `cli_path` 或环境变量 "
                f"{CLI_PATH_ENV}（那一档原样交回、不校验存在性，与 SDK 同口径）"
                f"；要换就改这两处之一，本 runner 不自己另找一个候选顶上"
                f"（品牌={resolved.flavor}，没有回退到另一个品牌重试）",
                reason="cli_launch_failed",
                retryable=False,
            ) from exc
        self._live[run_key] = process
        timed_out = False
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            # P1-13：Popen.kill() 只杀直接子进程，CLI 的孙进程会留下来锁文件。
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
        finally:
            self._live.pop(run_key, None)
        log_path = self.log_path_for(getattr(process, "pid", None), log_root=log_root)
        return RunOutcome(
            exit_code=int(getattr(process, "returncode", -1)),
            reason=_reason_from(log_path, stderr, stdout),
            stdout=stdout or "",
            stderr=stderr or "",
            log_path=str(log_path) if log_path else None,
            timed_out=timed_out,
            transcript_path=self.transcript_path_for(run_key, projects_root=projects_root),
            cli_flavor=resolved.flavor,
            cli_fallback_from=resolved.fallback_from,
        )

    # --- 解析 / 环境 / 警告 -------------------------------------------------------

    def _resolve_cli(self) -> ResolvedCli:
        """解析这一次要起的 CLI。`cli_path` 显式给了就原样用（品牌按文件名认，不算降级）。"""
        if self._cli_path is not None:
            return ResolvedCli(self._cli_path, _flavor_of_path(self._cli_path), None)
        return resolve_cli(env=self._env_source, home=self._home)

    def _home_dir(self) -> Path:
        return Path.home() if self._home is None else self._home

    def _warn_on_resolution(self, resolved: ResolvedCli) -> None:
        """两条明确 warning（owner 2026-10-24 裁决 3：只走 logging，绝不 print）。

        ① CN 找不到、回退 INTL ⇒ 告诉人"现在用的是哪一支、凭据要对哪边有效"；
        ② CN 命中的是 `.cmd` dispatcher 包装器 ⇒ 告诉人首选（原生 exe）没找到。
        """
        if resolved.fallback_from is not None:
            logger.warning(
                "Qoder CN CLI 未找到（查过 %s）⇒ 回退到国际版 CLI %s。当前凭据必须对国际版账号有效；"
                "否则会以 auth_error / access_token_invalid 失败（本 runner 不会换另一个品牌重试）",
                "、".join(cn_probe_points(self._home_dir(), _platform_suffix())),
                resolved.executable,
            )
            return
        if resolved.flavor == CN_FLAVOR and Path(resolved.executable).suffix.casefold() in {".cmd", ".bat"}:
            logger.warning(
                "本次 CN CLI 走的是 dispatcher 包装器 %s（不是原生 %s）⇒ 多一层 PowerShell；"
                "建议把 %s 指到原生 exe",
                resolved.executable,
                CN_CLI_BINARY,
                CLI_PATH_ENV,
            )

    def _new_child_home(self, run_key: str) -> Path:
        """为这一次运行造一个临时 HOME；run_key 只用于让目录名可读，不进任何凭据。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(run_key))[:32].strip("-") or "run"
        path = Path(tempfile.mkdtemp(prefix=f"{CHILD_HOME_PREFIX}{safe}-"))
        for child in ("tmp", ".config", ".cache", ".local"):
            (path / child).mkdir(parents=True, exist_ok=True)
        return path

    def _child_env(self, flavor: Flavor, *, home: Path | None = None) -> dict[str, str]:
        """**严格白名单** + 该品牌 PAT + 一次性临时 HOME。

        2026-10-29 P0-1 之前这里是"父环境整份复制 − SDK 身份标记"。那条口径把
        `ACP_DB_DSN`（含 PG 口令）、`ARK_API_KEY`、`WORKBUDDY_SECRET_*` 全交给了外部 CLI；
        实测这些值已经出现在 `~/.qoder-cn/logs`、`memory/`、`file-history/` 的明文里。
        现在只保留执行器真正需要的系统变量（`CHILD_ENV_ALLOWLIST`），其余一律不传。

        `HOME` / `USERPROFILE` / `APPDATA` / `LOCALAPPDATA` / `XDG_*` 全部指向 `home`（缺省新建
        一个临时目录）。这挡住的是**不经过 env 的凭据**：`~/.qoder/.auth/user` 是厂商签发的登录态，
        改 `.env` 作废不了它，只有让 CLI 的家不再是真实家目录才读不到。

        为什么仍要把 PAT 映射到两个名字下：CN CLI 二进制内嵌文档写的是
        `QODER_PERSONAL_ACCESS_TOKEN`，SDK 的 CN 分支写的是 `QODERCN_PERSONAL_ACCESS_TOKEN`；
        两个名字同值 ⇒ 无论它读哪个都拿到同一把该品牌凭据，也不会因父环境里还有一把旧值而歧义。
        只做环境 → 环境的键值映射：不落文件、不写日志、不进异常消息。
        """
        source: Mapping[str, str] = os.environ if self._env_source is None else self._env_source
        temp_home = Path(home) if home is not None else Path(tempfile.mkdtemp(prefix=CHILD_HOME_PREFIX))
        temp_home.mkdir(parents=True, exist_ok=True)
        temp_tmp = temp_home / "tmp"
        temp_tmp.mkdir(parents=True, exist_ok=True)

        child: dict[str, str] = {}
        for name in CHILD_ENV_ALLOWLIST:
            value = source.get(name)
            if value:
                child[name] = value

        # 家目录与缓存面：CLI 的 logs / memory / file-history / auth 都从这些键派生。
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            child[name] = str(temp_home)
        child["TMP"] = str(temp_tmp)
        child["TEMP"] = str(temp_tmp)
        child["TMPDIR"] = str(temp_tmp)
        drive, tail = os.path.splitdrive(str(temp_home))
        if drive:
            child["HOMEDRIVE"] = drive
            child["HOMEPATH"] = tail or "\\"

        token_var, token = _select_pat(source, flavor)
        if token is None:
            return child
        child[CN_PAT_ENV] = token
        child[INTL_PAT_ENV] = token
        primary = CN_PAT_ENV if flavor == CN_FLAVOR else INTL_PAT_ENV
        if token_var != primary:
            logger.warning(
                "%s 未配置 ⇒ 本次 %s 运行使用 %s；请确认这把值属于该品牌账号",
                primary,
                flavor,
                token_var,
            )
        return child

    def _launch_argv(self, resolved: ResolvedCli, argv: tuple[str, ...]) -> list[str]:
        """把 argv[0] 换成"这台机器上真能起的可执行文件"，其余一项都不动。

        换的是**第一参数**而不是整条：旗标的唯一载体仍是 `QoderCliSpec`（`test_submit_passes_argv_and_timeout_verbatim`
        钉的就是 transport 不改写 argv），本方法只回答"这个名字在这台机器上到底是谁"。
        解析失败与 `Popen` 的 `OSError` 都换成 `QoderTransportError` —— 它是 `RuntimeError` 的后裔，
        因此在 `realrun` 的 `SWALLOWABLE` 里 ⇒ 终端上是一条 `RUN FAIL` 加一段留痕，不是 traceback。
        """
        return [resolved.executable, *[str(item) for item in argv[1:]]]

    def abort(self, run_key: str) -> bool:
        process = self._live.get(run_key)
        if process is None:
            return False
        # 先走正常终止，再兜底杀整棵树（孙进程可能不响应 terminate）。
        process.terminate()
        _kill_process_tree(process)
        return True

    def log_path_for(
        self, pid: int | None, *, log_root: Path | PurePosixPath | None = None
    ) -> Path | None:
        """按 `pid` 找那一次 run 的日志（约束 ②）。找不到就 `None` —— 不猜、不跟 `latest`。

        `log_root` 缺省 = 构造时注入的那个；两者都没有 ⇒ `None`（**不在方法里回落到用户主目录**：
        回落会让"没注入"的判据偷偷读真机的 `.qoder[-cn]`）。
        """
        root = self._log_root if log_root is None else Path(log_root)
        if pid is None or root is None:
            return None
        directory = root / RUNS_DIR_NAME
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob(f"*-p{pid}")):
            logfile = candidate / LOG_FILE_NAME
            if logfile.is_file():
                return logfile
        return None

    def transcript_path_for(
        self, session_id: str, *, projects_root: Path | PurePosixPath | None = None
    ) -> str | None:
        """按 `session_id`（= 我们生成的 `run_key`）反查 transcript（约束 ② 的第二面）。

        `run_key` 里含随机后缀 ⇒ 不可能撞上别人的会话；找不到就是 `None`，不去猜 `project_id`、
        也不取"最近的那个文件"（那在多张卡并行时必然拿错）。
        用 `iterdir` + 名字相等而不是 `glob`：会话名是外部可控的一截（任务 ID 的形状），
        拼进 glob 模式就要赌它不含 `[`/`*` —— 不赌为强。
        `projects_root` 缺省口径同 `log_path_for`：不注入、不回落。
        """
        root = self._projects_root if projects_root is None else Path(projects_root)
        if not session_id or root is None or not root.is_dir():
            return None
        wanted = f"{session_id}{TRANSCRIPT_SUFFIX}"
        for project_dir in sorted(root.iterdir()):
            candidate = project_dir / wanted
            if candidate.is_file():
                return str(candidate)
        return None


class QoderTransport:
    """SDK 形态的适配器（`form = SDK`，取值域在 `protocol/refs.AgentForm`）。"""

    #: 契约 §4 的围栏写着 `agent_id: str`，而形状比对读 `cls.__annotations__` ⇒ 这行注解必须在类层。
    agent_id: str

    def __init__(
        self,
        *,
        runner: CliRunner,
        gateway: BusGateway,
        cwd: Path | str,
        spec: QoderCliSpec | None = None,
        env: Mapping[str, str] | None = None,
        growth_root: Path | None = None,
    ) -> None:
        self.agent_id = AGENT_ID
        self.runner = runner
        self.gateway = gateway
        #: 厂商子进程的家（owner 2026-10-20 裁决 1，选甲）。**没有默认值**就是这一格的设计：给了默认
        #: 等于默认继承调用方 cwd，而真跑入口是在平台仓库根里敲的 ⇒ 默认值本身就是那次事故的成因。
        #: 生产交的是**执行席那一格的 workspace 目录**（存档根下面那一层，装配见 `realrun._confine` 与
        #: `spec_factory.build_real_transport`）；本类不校验它，校验在真起进程那一层（`confine_cwd`），
        #: 否则注入 `FakeRunner` 的用例就永远读不到那道拒。
        self.cwd = cwd
        self.spec = spec or QoderCliSpec()
        self._env = env
        #: 进化档案的根。`None` = 走 `params-ref §11` 登记的那个默认根（解析优先级见 `growth.store.growth_root`）。
        #: 它是公开属性而不是私有名字：注入这件事要能被判据指到别处验证，而不只能靠构造参数。
        self.growth_root = growth_root

    # --- describe ---------------------------------------------------------------

    def describe(self) -> CapabilityManifest:
        """交回注册表里那份申报单（与 workbuddy 同一做法：本方法不生产任何新事实）。"""
        for registration in load_registrations():
            if registration.agent_id == self.agent_id:
                return registration.manifest
        raise QoderTransportError(
            f"注册表里没有 `{self.agent_id}` 这一条 ⇒ `describe()` 拿不到申报单"
            "（唯一来源 = `src/acp/registry/agents.yaml`，档位取值域 = `params-ref §9`）"
        )

    # --- submit -----------------------------------------------------------------

    def submit(self, task: Task) -> Handle:
        """下发 = 起子进程、跑完、把它的 stdout 流水落到自己 workspace，返回可重查的回执。

        报文面 = `_prompt_text()` 的渲染：三面 + 交付下限 + **逐条 `acceptance_criteria`**
        （owner 2026-10-13 裁决 2，覆盖 T041 那轮"细则不发"的纪律 —— 理由与 FR-029 的挂钩写在那里）。
        三支 transport 的这条共同口径由 `test_all_adapters.py::test_every_submit_carries_the_criteria` 守住，
        单支的可跑判据是 `test_prompt_carries_the_visible_face_and_the_criteria`。
        """
        if task.assignee.agent_id != self.agent_id:
            raise QoderTransportError(
                f"任务卡指派给 {task.assignee.agent_id!r}，本 transport 是 {self.agent_id!r}"
                " ⇒ 拒发（拿别人的身份下发会让 workspace 归属比对失去意义）"
            )
        self._require_credential()
        run_key = f"{task.task_id}-{uuid.uuid4().hex[:8]}"
        outcome = self.runner.run(
            argv=self.spec.argv,
            stdin_text=self._stdin_line(task, run_key),
            run_key=run_key,
            timeout_seconds=self.spec.timeout_seconds,
            cwd=self.cwd,
        )
        self._fail_loud_on_exit(outcome, run_key)
        self._persist_stream(task.task_id, run_key, outcome.stdout)
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=run_key,
            submitted_at=datetime.now(timezone.utc),
        )

    # --- collect ----------------------------------------------------------------

    def collect(self, handle: Handle) -> TaskResult:
        """回收 = 从盘上重查那份流水，再按实况登记产物（FR-019：盘上没有就不算写了）。"""
        if handle.agent_id != self.agent_id:
            raise QoderTransportError(
                f"回执的 agent_id 是 {handle.agent_id!r}，不是本 transport 的 {self.agent_id!r}"
            )
        events = parse_events(self._read_stream(handle.native_ref))
        self._refuse_control_plane(events)
        result = self._result_event(events)
        found = self._artifact_candidates(events)
        allowed = [path for path, names in found.items() if names & set(self.spec.writing_tools)]
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status=self._verdict(result),
            artifacts=[self._register(handle, self._bus_path(path)) for path in allowed],
            log_excerpt=self._log_excerpt(events, result),
            self_assessment=str(result.get(self.spec.result_text_field) or ""),
            issues=self._usage_note(result) + self._discovery_note(found, allowed),
            usage=self._usage_notices(result),
        )

    def cancel(self, handle: Handle) -> None:
        """撤单。本笔的 `submit()` 是同步跑完的 ⇒ 正常情况下这里已经**没有活着的子进程**可撤。

        `abort()` 返回 `False` 时这里是无害的空操作：本地子进程已经退出，撤销的目标不存在。
        这不是"cancel 没实现"，是"同步下发让它没有对象"。要它真能撤，`submit()` 得挪成非阻塞
        （返回后子进程仍在跑），而那一步与编排层的并发（Phase 6 的并发上限与卡点）是同一件事 ⇒ 登记为未闭。
        """
        self.runner.abort(handle.native_ref)

    # --- 内部 --------------------------------------------------------------------

    def _require_credential(self) -> None:
        """两个名字都没值 ⇒ 抛。消息里只有变量名；**值一个都不读进来**（子进程自己从环境里拾取）。"""
        for name in CREDENTIAL_ENV_NAMES:
            try:
                env_value(name, self._env)
                return
            except Exception:
                continue
        raise QoderTransportError(
            f"Qoder 凭据未注入：{' 或 '.join(CREDENTIAL_ENV_NAMES)} 至少要有一个有值"
            "（凭据只从环境来，宪法「二」）"
        )

    def _stdin_line(self, task: Task, run_key: str) -> str:
        """一行 JSON 的 user 报文：**结构照乙档抄来，值是我们的渲染**。

        外层三件套（`type` / `message` / `session_id` / `parent_tool_use_id`）与 `message.content` 是
        字符串这两点，都取自 SDK 自己的写法（`client.py:296-301`）；`content` 里放什么由我们定 ——
        它就是"Agent 应该看见的那份任务"，所以用与任务卡正文同一组语义词（目标/输入/期望产物）渲染，
        并且**渲染 `strip_frontmatter` 之后那三面 + 交付下限 + 逐条验收标准**：frontmatter 仍不进场，
        标准进场（owner 2026-10-13 裁决 2）。
        `session_id = run_key` ⇒ 一次下发一个会话，厂商侧的会话标识与我们侧的可重查标识对得上
        （`contracts/agent-protocol.md §4.1` 说 `native_ref` 由各 transport 自己解释）。
        """
        payload = {
            self.spec.event_type_field: self.spec.user_type,
            self.spec.message_field: {
                "role": "user",
                # 进化档案全文排在任务面**之前**（owner 2026-10-19 MVP-2 第三步 ①：无条件、全量、不截断）。
                # 这一支的报文面只有一个 user content 槽，没有 role=system，所以"进 System Prompt"
                # 这句在这里的落地形状就是"这段前缀每次都进场"；形状差已登记为待裁（不假装等价）。
                self.spec.content_field: growth_store.render_block(root=self.growth_root)
                + self._prompt_text(task),
            },
            self.spec.parent_tool_use_field: None,
            self.spec.session_id_field: run_key,
        }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @staticmethod
    def _prompt_text(task: Task) -> str:
        """三行取自 `gateway.CARD_LABELS`（同一支常量、同一顺序），不在这个文件里再抄一份中文标签。

        `CARD_LABELS` 的顺序与 `Task` 的 `objective / inputs / expected_output` 是同源的
        （`gateway.TaskCardBody` 的注释就写着"与 `protocol.Task` 的三个语义位一一对应"）。

        **`acceptance_criteria` 随任务下发**（owner 2026-10-13 裁决 2，覆盖 T041 那一轮"细则不发"的纪律）：
        FR-029 的"审查 MUST 逐条比对"要求审查方手里有那份逐条标准，而一支工具挂哪个岗是**任务级**的决定
        （FR-033 的只读也按任务算，见 `contracts/agent-protocol.md §4`）⇒ 在 transport 里预先筛掉细则，
        等于让同一个 agent 以审查者身份收到任务时手里没有标准可比。
        文件面（`gateway.render_for_agent` 剥 frontmatter）与消息面因此**故意不一致**，
        这一处差额登记在 `tasks.md` 的收口段，等 owner 定文件面要不要一起放出来。
        """
        body = zip(CARD_LABELS, (task.objective, "; ".join(task.inputs), task.expected_output))
        floor = task.artifact_floor
        # P1-10：任务正文（目标 / 输入 / 期望产物 / 验收标准）都可能带着客户原话，
        # 包括从拆单器原样搬过来的注入串。围栏 + 声明放在最前面，交付下限留在标签外 ——
        # 下限是平台口径，不是客户说的话。
        lines = [
            UNTRUSTED_INPUT_NOTE,
            CUSTOMER_INPUT_OPEN,
            *(f"{label}: {value}" for label, value in body),
            "验收标准:",
            *(f"{index}. {item}" for index, item in enumerate(task.acceptance_criteria, 1)),
            CUSTOMER_INPUT_CLOSE,
            f"交付下限: 格式 {floor.format}、不少于 {floor.min_bytes} 字节 / {floor.min_lines} 行",
        ]
        return "\n".join(lines)

    def _fail_loud_on_exit(self, outcome: RunOutcome, run_key: str) -> None:
        """约束 ①：退出码非 0、撞超时、或 reason 不是成功 ⇒ 立即失败，并把 `exit_code` / reason / 日志路径全带上。"""
        reason = outcome.reason
        if outcome.exit_code == 0 and not outcome.timed_out and reason in (None, "success"):
            return
        retryable: Retryable = self._classify(outcome, reason)
        if outcome.timed_out:
            tool, blocks = tools_in_flight(outcome.log_path)
            detail = (
                f"撞 {self.spec.timeout_seconds} s 上限后被 kill"
                f"｜最后发起的工具={tool or '（日志里没读到 tool_use 结构行）'}"
                f"｜累计工具块={blocks}（>0 ⇒ 它是在干活时被杀的，不是启动就僵住）"
            )
        elif reason is not None:
            detail = f'reason="{reason}"'
        else:
            detail = "日志里没有 process.exiting 行"
        raise QoderTransportError(
            f"{run_key} 的子进程失败：exit_code={outcome.exit_code} {detail}"
            f"｜取证日志={outcome.log_path or '（没找到）'}"
            f"｜取证transcript={outcome.transcript_path or '（没找到）'}"
            f"｜flavor={outcome.cli_flavor or '（没记下）'}"
            f"｜降级自={outcome.cli_fallback_from or '无'}"
            f"｜retryable={retryable}"
            f"｜不可重试类={sorted(NON_RETRYABLE_REASONS)}"
            f"｜stderr 长度={len(outcome.stderr)}（实测那次 stderr 是空的 ⇒ 别只看它）",
            exit_code=outcome.exit_code,
            reason=reason,
            retryable=retryable,
            log_path=outcome.log_path,
        )

    @staticmethod
    def _classify(outcome: RunOutcome, reason: str | None) -> Retryable:
        """约束 ③ 的分档：超时＝可重试，认证类＝不可重试，**认不出来就留 `None`**（不默认"可重试"，
        那等于给一次确定性失败发了重试券，成本与卡点都会跟着错）。"""
        if outcome.timed_out or reason in RETRYABLE_REASONS:
            return True
        if reason in NON_RETRYABLE_REASONS:
            return False
        return None

    def _persist_stream(self, task_id: str, run_key: str, stdout: str) -> None:
        """流水经网关落盘 ⇒ 归属判定与脱敏都只走那一道（外部进程写的东西不该绕过它）。"""
        self.gateway.write_artifact(
            self._actor(),
            f"{self._workspace_dir()}/{RUNS_SUBDIR}/{run_key}.jsonl",
            stdout,
            Capability(agent_id=self.agent_id, task_id=task_id),
        )

    def _read_stream(self, run_key: str) -> str:
        rel = f"{self._workspace_dir()}/{RUNS_SUBDIR}/{run_key}.jsonl"
        target = self._target_of(rel)
        if not target.is_file():
            raise QoderTransportError(
                f"回收找不到运行流水 {rel!r} ⇒ 无法重查。可能是 workspace 被清过，"
                "也可能这个 `native_ref` 不属于本机"
            )
        return target.read_text(encoding="utf-8")

    def _refuse_control_plane(self, events: list[dict]) -> None:
        """流水里出现 `control_request` ⇒ 明写"本实现不应答双向控制面"，并且**不归入可重试**。

        单向跑法（写完 stdin 就等 EOF）遇上要等回话的 CLI 会僵住，而僵住的表征是撞超时 ⇒
        `_classify()` 会把它判成"可重试"，那是错的诊断（重试一次只是再僵 120 秒，成本还翻倍）。
        本方法钉的是"读得到流"那一半；"读不到流（真僵住）"那一半仍未闭，登记在 `tasks.md` 的 T041 申报里。
        """
        hit = [
            str(event.get(self.spec.result_subtype_field, ""))
            for event in events
            if event.get(self.spec.event_type_field) == self.spec.control_request_type
        ]
        if not hit:
            return
        raise QoderTransportError(
            f"流水里有 {len(hit)} 条 control_request（子型 = {sorted(set(hit))}）⇒ 本 transport 是单向一次"
            "跑法，不实现控制面应答（`can_use_tool` / hooks / MCP 双向协商都不在本笔范围，见 `research.md` "
            "D-1 的范围口径）。这条 MUST NOT 被重试：重试的是同一条不兼容的路径"
            f"（argv={list(self.spec.argv)}）",
            retryable=False,
        )

    def _result_event(self, events: list[dict]) -> dict:
        for event in events:
            if event.get(self.spec.event_type_field) == self.spec.result_type:
                return event
        seen = sorted({str(item.get(self.spec.event_type_field)) for item in events})
        raise QoderTransportError(
            f"流水里没有 {self.spec.event_type_field}={self.spec.result_type!r} 的报文"
            f"（读到的类型 = {seen}）⇒ 无法判定结果。结果报文形状的证据档=乙"
            "（`_internal/message_parser.py:286-305`），但『真 CLI 在这套旗标下确实会发它』未验（丙档）"
        )

    def _verdict(self, result: dict) -> Literal["success", "failed"]:
        """两个独立信号（`is_error` 与 `subtype`）任一指向失败 ⇒ `failed`；两个都不认 ⇒ 抛，不猜。

        这里给的是**自报状态**，最终态由 `ValidateArtifact` 覆写（FR-019 / 契约 §2.2）⇒
        本方法没有任何"达标"的判定权，也不许加。
        """
        subtype = str(result.get(self.spec.result_subtype_field, ""))
        flagged = bool(result.get(self.spec.error_flag_field, False))
        if flagged or subtype in self.spec.failure_subtypes:
            return "failed"
        if subtype == self.spec.success_subtype:
            return "success"
        raise QoderTransportError(
            f"结果报文 {self.spec.result_subtype_field}={subtype!r} 不在已认的取值里"
            f"（认的 = [{self.spec.success_subtype!r}, {list(self.spec.failure_subtypes)}]，"
            f"`{self.spec.error_flag_field}`={flagged}）⇒ 不猜，也不按『默认失败』回收"
        )

    def _artifact_candidates(self, events: list[dict]) -> dict[str, set[str]]:
        """路径 → 报过它的那些工具名（按首次出现排序）。

        三条刻意的选择：
        - **先不看工具名**，把所有带路径的 `tool_use` 块都收进来；
        - 去重按**路径**而不是按 (工具名, 路径)：同一个文件被 `Write` 再 `Edit` 是两次动作、一个产物，
          登记成两条会让 FR-017 的产物计数虚高；
        - 名字收成**集合**：`Read` 在前、`Write` 在后的顺序不该让一个真写过的文件漏报（先出现的是
          `Read` 时，只要集合里有任何一个放行名就登记）。
        """
        found: dict[str, set[str]] = {}
        for event in events:
            for block in self._content_blocks(event):
                if block.get(self.spec.event_type_field) != self.spec.tool_use_block_type:
                    continue
                tool_input = block.get(self.spec.tool_input_field)
                if not isinstance(tool_input, dict):
                    continue
                raw = tool_input.get(self.spec.artifact_path_field)
                if not isinstance(raw, str) or not raw:
                    continue
                found.setdefault(raw, set()).add(str(block.get(self.spec.tool_name_field, "")))
        return found

    def _content_blocks(self, event: dict) -> list[dict]:
        """取一条报文里的内容块；字符串内容与没有块的报文（result / system）都返回空表。"""
        message = event.get(self.spec.message_field)
        if not isinstance(message, dict):
            return []
        content = message.get(self.spec.content_field)
        if isinstance(content, list):
            return [block for block in content if isinstance(block, dict)]
        return []

    def _discovery_note(self, found: dict[str, set[str]], allowed: list[str]) -> list[str]:
        """有带路径的工具但白名单一个没放行 ⇒ 明写进 `issues`。

        为什么不抛：判定"达标"是 `ValidateArtifact` 的权（FR-019 / 契约 §2.2），适配器越权抛错会把
        "少报产物"从一次可见的 invalid 变成一次 transport 崩溃。也不静默返回空：那等于把丙档的
        不确定性藏进零值里，FR-017 的下限比对会跟着错。
        """
        if allowed or not found:
            return []
        unseen = sorted({n for names in found.values() for n in names} - set(self.spec.writing_tools))
        return [
            f"artifact_discovery_suspect: 流水里有 {len(found)} 个带 {self.spec.artifact_path_field!r} 的 "
            f"tool_use 块，但都不在放行名单 {list(self.spec.writing_tools)} 里（读到的工具名 = {unseen}）"
            "⇒ 产物按 0 条登记。名单是丙档未验项，冒烟那轮要么证实要么补齐，不能就这么留着"
        ]

    def _register(self, handle: Handle, rel_path: str) -> ArtifactRef:
        """归属、格式、存在性、脱敏、实测五步，一步都不省。

        与 T040 的差别是实质性的：Qoder 是**外部进程自己写盘**，产物不经我们的写入口 ⇒
        ① 先验归属（越出 `qoder-workspace/` 就拒，FR-013）；② 再验后缀在 `ArtifactFormat` 域内
        （**必须在读与写之前**：域外文件是外部进程留下的，它已经在盘上了，我们唯一能做的是不把它登记成产物，
        而不是"先洗一遍再拒" —— 洗它等于承认了它是产物）；③ 再验它真在盘上（报了但文件不存在 ⇒ 拒，
        FR-019 的"以实况为准"）；④ 读出来过一遍脱敏，**有差异就经网关写回去** —— 外部写入方绕过唯一写入口
        这件事由这一步补上，而不是假装它不存在；⑤ 量的就是洗过的那份 ⇒ 三个数与盘上一字不差。
        """
        own = self._workspace_dir() + "/"
        if not rel_path.startswith(own):
            raise QoderTransportError(
                f"产物 {rel_path!r} 不在本 agent 的 workspace（{own}）内 ⇒ 拒收（FR-013）："
                "适配器 MUST NOT 持有除自身 workspace 外的写句柄（契约 §4）"
                f"（总线根 = {str(self.gateway.root)!r}；厂商报的是绝对路径而本根若是相对写法，"
                "上面那条换算不会发生 —— 这一格 2026-10-20 第五发烧过一次，读数点在这里是为了"
                "下一次不用再把消息反推成根因）"
            )
        format_of(rel_path)
        target = self._target_of(rel_path)
        if not target.is_file():
            raise QoderTransportError(
                f"结果报文声称写了 {rel_path!r}，但盘上没有 ⇒ 不登记"
                "（实测值由网关读工作区得出，厂商自报不算数，FR-019）"
            )
        on_disk = target.read_text(encoding="utf-8")
        washed = redact_bus_text(on_disk)
        if washed != on_disk:
            self.gateway.write_artifact(
                self._actor(),
                rel_path,
                washed,
                Capability(agent_id=self.agent_id, task_id=handle.task_id),
            )
        return measure_artifact(target, rel_path=rel_path)

    def _usage_note(self, result: dict) -> list[str]:
        """不可信计量：**不落账但留证**。静默丢掉会少一笔用量（FR-059 失真），全量落账又是在造假数。"""
        if self.spec.trust_usage:
            return []
        usage = result.get(self.spec.usage_field)
        return [
            f"usage_untrusted: 厂商报的 usage={json.dumps(usage, ensure_ascii=False)} 未落 EN-13"
            "（`research.md` D-1：那次 tokens 全 0 / `total_cost_usd=0`，不可作计量源；"
            "替代计量点是挂账待办，定了之后翻 `QoderCliSpec.trust_usage`）"
        ]

    def _usage_notices(self, result: dict) -> list[UsageNotice]:
        """只有显式翻了 `trust_usage` 才落账。口径 = `BYOK`（§4：费用由所配模型厂商结算，不占 Qoder Credits）。"""
        if not self.spec.trust_usage:
            return []
        usage = result.get(self.spec.usage_field)
        if not isinstance(usage, dict):
            raise QoderTransportError(
                f"字段 {self.spec.usage_field!r} 不是映射，收到 {type(usage).__name__}"
                " ⇒ 已翻 `trust_usage` 却没读数可落，这是配置与实况的矛盾，不能当 0 处理"
            )
        now = datetime.now(timezone.utc)
        notices: list[UsageNotice] = []
        dimensions: tuple[tuple[Literal["INPUT", "OUTPUT"], str], ...] = (
            ("INPUT", self.spec.usage_input_field),
            ("OUTPUT", self.spec.usage_output_field),
        )
        for kind, field in dimensions:
            if field not in usage:
                raise QoderTransportError(
                    f"usage 里没有 {field!r}（现有键 = {sorted(usage)}）⇒ 该维度不落账，但要明写"
                )
            # `Decimal("n/a")` 抛的是 `InvalidOperation`（不是 pydantic 的 `ValidationError`）：
            # 单独接住再翻成本层的错，消息里才带得到**字段名**。
            try:
                quantity = Decimal(str(usage[field]))
            except InvalidOperation as exc:
                raise QoderTransportError(
                    f"usage 的 {field!r} 读到的值不是数字（类型 = {type(usage[field]).__name__}）"
                ) from exc
            try:
                notices.append(
                    UsageNotice(
                        usage_kind=kind,
                        quantity=quantity,
                        unit="token",
                        billing_basis="BYOK",
                        occurred_at=now,
                    )
                )
            except ValidationError as exc:
                # 厂商给个 `"n/a"` 之类的量：转成本层的错，带上**字段名**与读到的类型。
                # 让它以 pydantic 的形态冒出去，下游就只能猜是哪一维脏了（T040 的 N11 钉的是同一件事）。
                raise QoderTransportError(
                    f"usage 的 {field!r} 读到的值不是数字（类型 = {type(usage[field]).__name__}）"
                    f"⇒ 不落 EN-13，也不按 0 处理（{exc.errors()[0]['type']}）"
                ) from exc
        return notices

    def _log_excerpt(self, events: list[dict], result: dict) -> str:
        """节选 = 助手消息里的 text 块（乙：`message_parser.py:112-114` 的 `{"type":"text","text":…}`），
        取末尾 400 字；一个都没有时退回结果报文的 `result`（乙：`ResultMessage.result`）。

        `content` 写成字符串的助手消息也收（user 报文那一面 SDK 两种形态都处理，见
        `message_parser.py:81` 的 `isinstance(..., list)` 分支），所以这里同样两条路都走。
        """
        pieces: list[str] = []
        for event in events:
            if event.get(self.spec.event_type_field) != self.spec.assistant_type:
                continue
            message = event.get(self.spec.message_field)
            if not isinstance(message, dict):
                continue
            content = message.get(self.spec.content_field)
            if isinstance(content, str):
                pieces.append(content)
                continue
            for block in self._content_blocks(event):
                if block.get(self.spec.event_type_field) == self.spec.text_block_type:
                    pieces.append(str(block.get(self.spec.text_field, "")))
        joined = "\n".join(piece for piece in pieces if piece)
        return joined[-400:] or str(result.get(self.spec.result_text_field) or "")

    def _workspace_dir(self) -> str:
        """本 agent 的 workspace 目录名 = `layout.workspace_dir_name()`，**不在这里再拼一次后缀**。

        驱动侧 `_confine()` 拿同一个函数决定厂商子进程的家。两处各拼一份 `-workspace` 时，漂开的症状不是
        "路径不对"而是"厂商跑成功、产物一条都不登记"（2026-10-20 第五发点火的 FR-013 就是这个形状）。
        """
        return workspace_dir_name(self.agent_id)

    def _bus_path(self, raw_path: str) -> str:
        """把厂商报的路径收成总线相对形态。**只承认"绝对且确在本总线根下"这一种换算** ⇒
        换算不会成为绕过归属判定的后门：别的绝对路径、别的 workspace、含 `..` 的路径都原样交给
        `_register`，在那里被拒并留下"为什么被拒"的消息。

        比的是**同一个目录的几种拼法**而不是一个字符串：总线根可以是相对写法（`--bus-root` 的默认值
        `results` 就是，解析在 `realrun.main()` 已经收窄成绝对，但适配器不该依赖调用方那一步做对了），
        而外部进程报回来的是绝对写法。只比原样那一份会把**合法产物读成越界** —— 第五发实测到的正是这个：
        一次成功的执行，八张卡的产物被自己的安全防线整批拒收。盘符大小写在 Windows 上由 `os.path.normcase`
        洗掉（它在 POSIX 上是恒等），切出来的那一串仍从原串取 ⇒ 换算不改写文件名本身的大小写。
        """
        posix = raw_path.replace("\\", "/")
        for form in self._root_spellings():
            if os.path.normcase(posix).startswith(os.path.normcase(form + "/")):
                return posix[len(form) + 1 :]
        return posix

    def _root_spellings(self) -> tuple[str, ...]:
        """`gateway.root` 的**同目录几种拼法**（原样 + 绝对），长的排前面。

        只有拼法，没有新事实：总线根是谁仍然只有一个载体（`gateway.root`）。空串被滤掉，否则一个相对根
        在 cwd 上算出的绝对写法与它自己相等时，`"" + "/"` 会把"任何路径都在根下"变成真。
        """
        root = self.gateway.root
        forms = {
            root.as_posix().rstrip("/"),
            Path(os.path.abspath(str(root))).as_posix().rstrip("/"),
        }
        return tuple(sorted((form for form in forms if form), key=len, reverse=True))

    def _target_of(self, rel_path: str) -> Path:
        """相对路径 → 磁盘位置。`normalize_rel_path` 在这里是**必需的**，不是可选的 tidy：
        厂商报的路径我们没验过就 joinpath，`..` 会直接指到总线根外面去。
        用它而不是 `gateway._resolve()`：后者是私有方法，而它内部调的就是这一支 ⇒ 拿同一支函数，不复制第三份。
        """
        try:
            normalized = normalize_rel_path(rel_path)
        except ValueError as exc:
            raise QoderTransportError(f"路径 {rel_path!r} 不是合法的总线相对路径：{exc}") from exc
        return self.gateway.root.joinpath(*normalized.parts)

    def _actor(self) -> Actor:
        """`Handle` 不带 `role_id`（契约 §4.1）⇒ 写盘身份用 `acl.NO_ROLE_DECLARED`，不编一个岗位。
        三家适配器共用那一个载体（owner 2026-10-13 待裁② 的处置），本目录里不留第二份字面量。"""
        return Actor(actor_id=f"{ADAPTER_OWNER_PREFIX}{self.agent_id}", role_id=NO_ROLE_DECLARED)


def parse_events(stdout: str) -> list[dict]:
    """按行拆报文。**读不懂的行跳过而不是整包拒**：CLI 会把人类可读的日志混在同一个流里吐出来
    （实测那次 `stderr` 为空、诊断在日志文件里），一个非 JSON 行不该让一次成功运行变成失败。
    跳过的行数不进产物也不进账，但它仍在落盘的流水里（`_persist_stream` 存的是原文）⇒ 可复查。
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        piece = line.strip()
        if not piece:
            continue
        try:
            decoded = json.loads(piece)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return events


def _reason_from(log_path: Path | None, stderr: str, stdout: str) -> str | None:
    """取 `process.exiting … reason="…"`。**顺序是刻意的：日志 → stdout → stderr** ——
    约束 ① 的实测事实就是"`stderr` 为空"，只看它就读不到 reason。"""
    for text in (_read_log(log_path), stdout, stderr):
        if not text:
            continue
        hit = EXIT_LINE.search(text)
        if hit:
            return hit.group("reason")
    return None


def _read_log(log_path: Path | str | None) -> str:
    """读那一份取证日志。`None` / 读不到 ⇒ 空串（调用方据此回"没读到"，不抛、不猜）。

    参数收 `str` 是有原因的：`RunOutcome.log_path` 是**字符串**（它要进异常消息、也要被判据比对），
    而 runner 内部手里拿的是 `Path` —— 两个形态都得吃，否则 transport 这一侧一发就 `AttributeError`。
    """
    if log_path is None:
        return ""
    try:
        return Path(str(log_path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def tools_in_flight(log_path: Path | str | None) -> tuple[str | None, int]:
    """撞超时那一刻"最后一轮在跑什么工具"：回 `(最后一个工具名, 一共排过几个工具块)`。

    owner 2026-10-20 裁决 3 要的就是这一格 —— 第四发点火那条消息只说"撞 120 s 上限后被 kill"，
    而真机日志里 CLI 一直在正常推进（`Bash` / `Read` 一轮接一轮），人从那条消息看不出它是僵死还是在干活。

    只取 `TOOL_LINE` 吃得到的那个标识符：日志正文（对话片段、路径、命令回显）一个字都不进返回值，
    因此这条诊断不违反 `QoderTransportError` 类文档那句"MUST NOT 有凭据值或日志正文"。
    读不到日志或没有匹配行 ⇒ `(None, 0)`，不猜"大概是某个工具"。
    """
    names = TOOL_LINE.findall(_read_log(log_path))
    return (names[-1] if names else None), len(names)


#: 注册表入口名（`registry/agents.yaml` 写的是 `acp.adapters.qoder:Transport`）。
#: 别名而不是改名：栈跟踪里 `QoderTransport` 更好读，而入口形状由 `loader.ENTRY_RE` 钉住。
Transport = QoderTransport

#: 真装配面（B 计划 第一步）约定的属性名：SDK 形态的厂商模块报出"生产用哪个 runner"，
#: 工厂按这个名字取而不 import 本模块的私有细节。用例注入的是 `FakeRunner`，不是这一行。
#: 工厂那侧 `runner_class()` 造出来的这一个**默认就是"到 run() 才解析 CLI"**（`cli_path=None`）：
#: 构造不读盘 ⇒ 工厂与用例都能安全造它，而真跑那一发一定会拿到这台机器上真能起的那个可执行文件。
PRODUCTION_RUNNER = SubprocessRunner
