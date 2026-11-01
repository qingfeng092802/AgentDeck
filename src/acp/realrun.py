"""MVP-2 笔 P：真跑入口从骨架变成驱动（owner 2026-10-19 B 计划第二步 + 同日三条决定）。

W-8 裁乙留下的那一支入口，本轮按 owner 三条决定接线。三条各自的落点，逐条点名免得下一轮读成"看着像"：

- **决定 1（丙：不加 CLI 传结论的旗标）**：本入口跑到 `result_ingest → validate_artifact` 就停，
  评审那一发归人。**与 owner 原话有一处口径差，必须点名而不是抹平**：他说"停在 `ACCEPTING`"，
  而库里 `ACCEPTING` 的前件是"审查岗已经交过一次 `APPROVE`"（`orchestrate/nodes/review.py:267` 那句
  `REVIEWING ──APPROVE──▶ ACCEPTING`）；决定 1 同时禁止本入口自带结论 ⇒ 它不能替人把卡推到 `ACCEPTING`。
  **M2-W8 之后**（2026-10-21）：门禁判 valid 的那一支由本入口推到 **`REVIEWING`**（`Review` 节点的动作，
  `validate_artifact.py:27`），然后停下等人从 `POST /tasks/<卡号>/review`（或 `python -m acp.review`）交结论；
  不达标那一支停在 **`ARTIFACT_INVALID`**。`RUNNING` 那一格只属于 M2-W8 之前的旧现场，
  评审端点会把它们补开到 `REVIEWING`。
- **决定 2（允许初始化 bus_root）**：存档根缺失 ⇒ `ensure_archive_root()` 自动 `mkdir` + `git init`。
  打的是**存档根**（`<bus_root>/<project_key>`，载体 = `archive.root.archive_root()`）那一层，不是外层
  `bus_root`：`require_archive_root()` 查的是"这个目录自己是不是仓库顶层"，在外层 init 治不了它。
  默认总线根 = `results`（`DEFAULT_BUS_ROOT`）⇒ `--project real_demo` 的产物路径就是 `results/real_demo/`，
  对上 owner 那句"产物路径就是 results/real_demo/..."。
  ⚠️ 一处名字巧合要点名：文件总线的公共区里也有一个 `results/`（`bus/layout.py::RESULTS_DIR`，在存档根**里面**），
  与本轮这个外层总线根 `results/` 不是同一层东西。
  **2026-10-20 第一发点火之后加的两道闸（都在 `mkdir` 之前）**：`_refuse_if_doubled`（`--bus-root` 的末段
  与 `--project` 同名 ⇒ 拒，并把正确那一行写进消息 —— 那次敲的是 `--bus-root results/real_demo --project
  real_demo`，存档根变成 `results/real_demo/real_demo`）与 `_refuse_if_git_env_overrides`（`GIT_DIR` /
  `GIT_WORK_TREE` / `GIT_CEILING_DIRECTORIES` 在位 ⇒ 拒，只点变量名）。同一发还查到一个**与本入口无关的
  环境事实**：`wsl -u root` 下 `git init` 退 0 而 `rev-parse --show-toplevel` 退 128（drvfs 把 `/mnt/d`
  的属主报成 Windows 那一侧的用户，git 的仓库属主检查不肯认刚建好的仓库）⇒ `git_init_did_not_take`
  从"读到 None"改成带原因词的一行（`_toplevel_cause` 五格），否则那条消息只能让人猜。
- **决定 3（乙：不复用 `run_execute`）**：`run.py` 一字未动（含 `resolve_transport_flag()` 与 `run_execute`
  那两条 AST 判据）。厂商调用只发生在下面的 `_produce()`，构造走 `spec_factory.build_real_transport()`，
  **没有 `StubAdapter`**。三段今天没有厂商调用的冻结链路（受理 / 恢复 / 指派）由本入口**调用**而不是重写，
  它们各自第一行就是那道门，所以必须交一个它认的值 —— 见 `FROZEN_SEGMENT_GATE` 那一格的说明。

**第四发点火之后的两条决定（owner 2026-10-20 原话，会话即凭证）**：

- **`cwd` 选甲**：`SubprocessRunner.run` 接收 `cwd`，由本入口传**厂商那一席自己的 workspace 目录**
  （解析 = `_confine()`：先 `archive.require_archive_root(project_key)` 过两道拒，再取
  `layout.workspace_dir_name(executor)` 那一层并在交出去之前建出来）。原话是"绝对不能让 AI 在平台源码根目录跑，
  必须把它关在它自己的 workspace 里"——前三发都没有撞这一格，是因为第四发之前根本没有一发真的跑到"厂商在写文件"
  那一步：那一次的 manifest 把 `cwd` 记成了平台仓库根，厂商 agent 于是拿着只读权限把**平台自己的源码**当成了工作区。
  **这一格在笔 Y 落错过一次**：当时把"workspace"实现成了存档根本身，第五发点火 ⇒ 产物写在存档根顶层、
  `collect()` 的归属判定（FR-013，认 `<agent>-workspace/**`）把它全拒了。现值 = 存档根**下面**那一层。
- **默认超时 900 s**：`QoderCliSpec.timeout_seconds` 从 120.0 抬到 900.0，载体 = `config/params-ref §1`
  新增的"非卡点执行超时"那一行（两侧读数有判据对读）。诊断半边一起落：撞超时那条消息带上
  "最后发起的工具"与累计工具块数，用来分"在干活时被杀"与"启动就僵住"。
  子进程树那格按 owner 裁**只登记不修**（`Popen.kill()` 只杀直接子进程，Windows 杀树是深坑）⇒ `tasks.md` 的 M2-W18。

**两个私有件的借用要点名**（这是本笔换来的东西与它的代价）：`run._task_of()` 是 `Task` 那 13 项必填在全仓的唯一投影，
`run._slots_from_registry()` 是"瞬态层上限从 EN-03 现值读"的唯一读法。在本文件另写一份就是给这两件造第二个载体，
而本仓已有一条同族纪律（`Task` 的必填只许有一份抄本，`tests/guards/test_transport_spec_factory.py` 借 fixture 那段）。
所以这里跨模块调私有函数，判据 `test_the_driver_borrows_the_two_single_carriers` 钉住"借的是哪两个名字" ⇒ **改名要两侧同笔**。

**日志的粒度是诚实的**：`submit()` 是同步跑完的（豆包一次补全、Qoder 一个子进程跑完才返回），
所以"Qoder 正在写 HTML"这一发能给出的最细形状是 `RUN SUBMIT` 与 `RUN COLLECT` 之间厂商在跑，
中间没有逐帧进度。要逐帧得把下发改成流式，那一格与编排层并发是同一件事，登记为未闭。

**这一发证明不到什么**（三条，写在最前面免得读成"真端到端已通"）：

1. 本文件与判据**全程零出网**：厂商构造被 `transport_factory` 这个注入位换掉，真凭据一次都不出现。
   真调用归 owner 的终端（他那句"等你汇报后，我亲自在终端点火"）。
2. 拆单今天仍是 `run.StubOrchestrator` 那份固定四张卡（R-07 的构成），**不读需求正文** ——
   这一发真起来的是"下发与回收"，不是"总管看懂了需求"。那句话的权威出处在 `quickstart.md §6` 边界 ①，
   本文件不重抄第三份。
3. 本入口**仍不产结论、不代验收**：评审走 `api/tasks.py` 的 `POST /tasks/{task_id}/review`（看板/CLI 同一端点），
   通过后的验收条目由 `review/service.py` 以 `human-review-pass` 留痕代填 —— 它是"人已点过通过"的展开，
   **不是**独立的验收判断，也不构成 FR-051 / FR-052 的实证。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from acp import run
from acp.adapters import spec_factory
from acp.archive import cadence
from acp.archive import root as archive
from acp.bus import actor as bus_actor
from acp.bus import layout
from acp.bus.gateway import BusError, BusGateway
from acp.config.loader import CredentialMissing, install_logging_filter, load_danger_list
from acp.guard import autonomy as guard_autonomy
from acp.orchestrate import graph as orchestrate_graph
from acp import routing as routing_lib
from acp.orchestrate import scheduler
from acp.orchestrate.nodes import artifact_retry
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.orchestrate.nodes import escalation
from acp.orchestrate.nodes import review as review_node
from acp.orchestrate.nodes import validate_artifact
from acp.persistence import events
from acp.persistence import session as session_module
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.protocol.messages import TaskResult
from acp.review import policy as review_policy
from acp.review import service as review_service

PROG = "python -m acp.realrun"

#: 决定 2 的默认总线根。`--project real_demo` ⇒ 存档根 `results/real_demo`
#: （"外层根套一层项目名"这条拼法只在 `archive.root.archive_root()` 那一处，本文件不重写它；
#: 权威出处 = `config/params-ref.md §10` 的 `ARCHIVE_ROOT=<bus_root>/<project_key>`）。
DEFAULT_BUS_ROOT = "results"

#: 会让 git 打到"另一个仓库"的三个环境变量。**在位就拒，且在 `mkdir` 之前拒**。
#: 只点变量名、绝不点值（值里可能是别处的路径，宪法「二」那条口径同样管得着非凭据的路径串）。
#: 为什么这三个各算一格而不是"一个 git 环境不对"：`GIT_DIR` 把仓库搬到别处、`GIT_WORK_TREE` 把**工作树**
#: 搬到别处、`GIT_CEILING_DIRECTORIES` 让向上探测在半路停下 —— 三种坏法给出的读数不同，
#: 但共同点是"本入口以为自己在存档根上操作，其实不是"。这一族与 ③ 那格属主检查是同一件事的两个方向：
#: 一个是环境把 git 指走，一个是 git 自己不肯认这个目录。
GIT_OVERRIDE_ENV_NAMES: tuple[str, ...] = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES")

#: 受理 / 恢复 / 指派这三段冻结链路各自第一行的那道门（`adapters/stub.py::resolve_transport_flag`）
#: 只放这一个值。交进去**不等于**那三段用仿真跑：那三段一次厂商请求都不发，厂商调用只在本文件的
#: `_produce()` 里发生，那一发构造的是 `build_real_transport()`。判据两侧各钉一条：
#: `test_the_frozen_segments_still_pass_the_door`（门没被放宽）与
#: `test_no_stub_adapter_is_constructed_in_the_driver`（本入口不自称仿真）。
FROZEN_SEGMENT_GATE = "stub"

#: 输出行前缀：与 `run` / `recover` / `chaos` 同族（`RUN <判定> :: key=value`）。
#: 同族同形的理由是 `run.py` 模块头那句"人和脚本都要读"，这里不另起一种好看法。
PREFIX = "RUN"

#: 门禁两条出边的名字**从 `OUT_EDGES` 取**，不在这里重写字面量（口径同 `graph.py:69-70`：
#: 表与图各写一份时，"改了出边名字忘了改这里"是静默错，而排序取法会让两条边互换）。
EDGE_REVIEW = validate_artifact.OUT_EDGES[validate_artifact.VALID]
EDGE_HANDOFF = validate_artifact.OUT_EDGES[validate_artifact.INVALID]

#: 卡跑完一段之后的四种收尾，逐条进 `RUN SUMMARY` 的计数。四个名字各对应一种可独立坏掉的形状，
#: 并成"成功 / 失败"两格就看不出"厂商跑通了但门禁没过"与"厂商根本没跑"的差别。
DONE_VALID = "valid"
DONE_INVALID = "invalid"
DONE_ERROR = "error"
DONE_UNKNOWN = "unknown"

#: 失败留痕那行的 `kind` / `entity` / `action`。前两个的取值域来自 `persistence/enums.py`
#: （`kind` 不在域里时 `events.record()` 当场拒），`action` 是本入口自己的名字，
#: 与 `run.py` 那两个留痕动作名（`acceptance_items_proxy` / `seat_blocked_candidates`）同规。
FAILURE_EVENT_KIND = "TASK"
FAILURE_EVENT_ENTITY = "task_card"
FAILURE_EVENT_ACTION = "realrun_produce_failed"

#: 可以收住继续跑的两类失败：transport 层的错（三支各自那个 `*TransportError` 全从 `RuntimeError` 派生）
#: 与协议/建模的错（pydantic 的 `ValidationError` 是 `ValueError` 的子类）。
#: **不在这里点名厂商类名**是刻意的：本入口不该 import 具体 transport，否则接第四支就要改这一段。
SWALLOWABLE: tuple[type[BaseException], ...] = (RuntimeError, ValueError, BusError)

#: 总线拒绝为什么也算"这一张卡跑砸了"（2026-10-28，现场 = `real_demo` 那单的 T05）：
#: 执行器写了一支 `submit-contact.php`，`format_of()` 判 `unsupported_format` 抛 `BusError`。
#: 它原来不在 `SWALLOWABLE` 里（`BusError` 直接派生自 `Exception`），于是**一件坏产物把整条链打断**：
#: `run_card_segment` 的 except 走 `_swallowable` 判 False ⇒ 原样抛 ⇒ `serve_resumer` 记成
#: `resume_failed`，后面还能跑的卡一张都没跑。`BusError` 说的从来是"这一次写入/这一件产物不成立"，
#: 不是"这个进程不会成功"——后者由 `RealRunRefused` / `CONFIG_REFUSALS` 表达，两份名单互不重叠。

#: 两种"看起来像一次失败、其实是装配缺陷"的错，必须原样抛出去：`MissingLease` = 没有派发过就跑执行段，
#: `SlotLeak` = 借还不成对（吞掉它的下场是"跑几次把并发上限吃光后静默阻塞"）。
#: 这一份名单与上面那份**有交集**（两支都从 `RuntimeError` 派生），所以先后由 `_swallowable()` 定，
#: 而那条判据量的正是这个先后 —— 把两份名单并成一份，泄漏就会被当"这一发的厂商失败了"吞下去。
ASSEMBLY_DEFECTS: tuple[type[BaseException], ...] = (scheduler.MissingLease, scheduler.SlotLeak)

#: 停点那一行的下一步提示。评审入口落地后（M2-W8），卡被推到 `REVIEWING`，人从这里接手。
STOP_HINT = "产物已判定，等待人工评审（POST /tasks/<卡号>/review 或 python -m acp.review submit）"

#: 用法错误的提示句（形状同 `run.USAGE_ONE_OF`：两个都不给 / 两个都给时打出去的那一句）。
USAGE_ONE_OF = "`--request <需求原话>` 与 `--resume <需求单号>` 必给且只能给一个"

#: 批准那一发的下一步：A 类单据的两个通道，逐字与 `quickstart.md §5` 那两条命令同形。
APPROVE_HINT = "python -m acp.approve approve <单据号> --principal <审批人>"


class RealRunRefused(RuntimeError):
    """到不了厂商之前就停。`code` 是稳定键名：措辞可以改，代号改了就是调用方要跟着改。

    消息里只允许出现：路径、变量**名**、代号、卡号、退出码。凭据值与厂商响应正文一个字都不带
    （口径同三支 transport 各自的错误类，也同宪法「二」）。
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


class DangerActionBlocked(RuntimeError):
    """P0-2：任务文本命中高危动作清单 ⇒ 建 D 类单、拒绝下发。

    继承 `RuntimeError` 是刻意的：它属于"这一张卡跑砸了"，不是"这个进程不会成功"。
    `run_card_segment` 会把它收进 `SWALLOWABLE`，落一条 `realrun_produce_failed` 留痕，
    再把卡从 `RUNNING` 挪到 `ESCALATED` —— 与人工升级走同一个出口。
    """

    code = "danger_action_blocked"

    def __init__(self, message: str, *, hits: tuple[str, ...], ticket_id: str | None = None) -> None:
        self.hits = hits
        self.ticket_id = ticket_id
        super().__init__(message)


#: 第三类"不许被当成一次卡失败"的错。`RealRunRefused` 也派生自 `RuntimeError`，但它说的不是
#: "这一张卡跑砸了"，而是"这个进程不会成功"——缺凭据 / 不在册 / 形态不可装 / 存档根开不出来
#: 都是进程级事实，一张卡撞上就说明后面每一张都会同样撞上。
#: owner 第一步那句"必须抛出明确的系统错误并退出"落在这一格：**原样抛出去**，由 `main()` 用
#: "碰任何东西之前就拒"那一档退出码结束，而不是逐张重复同一条拒绝、最后给出一个"跑了 4 张、每张都失败"的读数。
#: 与 `ASSEMBLY_DEFECTS` 分成两份名单是因为归因不同：那一支指向装配顺序，这一支指向环境与配置。
CONFIG_REFUSALS: tuple[type[BaseException], ...] = (RealRunRefused,)


def _is_httpx_error(exc: BaseException) -> bool:
    """P1-13：`httpx.ConnectError` 是 `HTTPError` 的后裔、不是 `RuntimeError`。

    不在模块顶层 `import httpx`：`tests/guards/test_realrun_driver.py` 有一条"驱动不许直接
    出网"的 AST 判据，顶层 import 会被读成"驱动自己带了 http 客户端"。按模块名认，不引入依赖。
    """
    return type(exc).__module__.split(".", 1)[0] == "httpx"


def _swallowable(exc: BaseException) -> bool:
    """这张卡跑不完 ≠ 整条链该停。判的是"错在哪一层"，不是"错得响不响"。"""
    return (
        (isinstance(exc, SWALLOWABLE) or _is_httpx_error(exc))
        and not isinstance(exc, ASSEMBLY_DEFECTS)
        and not isinstance(exc, CONFIG_REFUSALS)
    )


@dataclass(frozen=True)
class CardOutcome:
    """一张卡在本入口这一段跑完之后的读数。

    读数**全从库里与 `TaskResult` 的现值来**，不读图上那份 state：图 state 是"这一跳算出来的"，
    库里那份才是门禁写进去的（口径同 `run.ExecutionReport` 那段说明）。
    `error` 为 `None` 表示这一发真的跑到了门禁；非 `None` 时产物与结论都不存在，
    所以另两格保持空 / `None`，不用空字符串冒充读数。
    """

    card_id: str
    agent_id: str
    validated_status: str | None
    card_status: str | None
    artifacts: tuple[tuple[str, int, int, str], ...]
    error: str | None = None


def _row(verdict: str, *, emit_to: Callable[[str], object], **fields: object) -> None:
    """一族一行：`RUN <判定> :: key=value`。空值不进那一行（写了 `next=None` 就是噪声）。"""
    joined = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    emit_to(f"{PREFIX} {verdict} :: {joined}" if joined else f"{PREFIX} {verdict}")


def platform_repo_root() -> Path:
    """平台自己那份仓库的根（`src/acp/realrun.py` 往上三层）。只用于下面那道防护，不当配置用。"""
    return Path(__file__).resolve().parents[2]


def _run_git(argv: Sequence[str], cwd: Path) -> int:
    """默认 git 执行器：argv 是列表、不经 shell（形状同 `cadence.run_git`，但本入口只发 `init`）。

    那三个关键字（`encoding="utf-8"` / `errors="replace"` / `check=False`）逐字抄 `cadence.py:123-131`，
    不是这里的新发明：git 写的就是 UTF-8，而本机区域编码是 cp936 ⇒ 不钉 `encoding` 的话，
    一条带中文路径的失败消息会被按 cp936 解，轻则乱码、重则抛 `UnicodeDecodeError`（它是
    `ValueError` 的后裔，而这一发发生在 `main()` 开会话之前 ⇒ 不在任何拒绝分支的覆盖范围内，
    owner 看到的是一整段 traceback 而不是 `RUN FAIL :: code=git_init_failed`）。
    不复用 `cadence.run_git` 而是自己发这一发的唯一理由 = 它的 `ALLOWED_SUBCOMMANDS` 只有
    `add / commit / rev-parse`，`init` 进去就会被自己的名单拒 ⇒ 执行器另起一个，**解码口径不另起一个**。
    """
    done = subprocess.run(
        ["git", *(str(item) for item in argv)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.returncode


def _refuse_if_platform_repo(target: Path) -> None:
    """存档根不许等于平台仓库根，也不许是它的祖先。

    只防这一种形状：init 之后它是顶层 ⇒ `cadence` 会在那个仓库上 `add -A`，而那个仓库里装着平台全部源码
    （`archive/root.py` 模块头点名的"最坏的一次误伤"）。反过来"存档根**嵌在**平台仓库里"（默认那个
    `results/<project>` 就是这种）是**允许的**：init 之后它自己就是顶层，而那正是 `require_archive_root()`
    要的形状。判的是路径而不是"像不像"：拿目录名猜会被 `--bus-root ../平台名` 那种写法绕过去。
    """
    repo = platform_repo_root()
    resolved = target.resolve()
    if resolved == repo or resolved in repo.parents:
        raise RealRunRefused(
            f"存档根解析成 {str(resolved)!r}，它等于或高于平台仓库根 {str(repo)!r} ⇒ 拒。"
            "再往下一次 `git init` 会让存档机制把平台自己的源码树当成产物提交掉",
            code="archive_root_is_platform_repo",
        )


def _refuse_if_doubled(project_key: str, bus_root: Path | str, target: Path) -> None:
    """`--bus-root` 的末段与 `--project` 同名 ⇒ 拒，并把正确那一行原样写给看消息的人。

    为什么是拒而不是"顺手把多出来那一段剥掉"：`<bus_root>/<project_key>` 这条拼法只许有**一个**载体
    （`archive.archive_root()`，权威 = `config/params-ref.md §10` 的 `ARCHIVE_ROOT=<bus_root>/<project_key>`）。
    在本入口静默纠偏等于写第二份拼法，而它只在"用户敲重复"这一支生效 —— 下一次换个写法就会两条规则各漂一半。
    更糟的下场是产物落进一个谁都没点名的目录：`--bus-root results/real_demo --project real_demo` 真跑起来
    写的是 `results/real_demo/real_demo/`，而人以为看的是 `results/real_demo/`（`recover --from-file-bus`
    与交付包核验读的是后者 ⇒ 会出现"跑通了但找不到产物"那种归因到错的层的红）。

    只判末段与项目名**逐字相等**这一种形状：`--bus-root results/foo --project real_demo` 是合法用法
    （外层根本来就可以带层级），把它一起拦掉就是"为了能跑而放宽防护"的反面 —— 拿宽度当强度。
    """
    outer = Path(str(bus_root))
    if outer.name != project_key:
        return
    raise RealRunRefused(
        f"--bus-root 的末段与 --project 同名（都是 {project_key!r}）⇒ 存档根会拼成 {str(target)!r}，"
        f"比你要的那一层多套了一个项目名。正确形状 = `--bus-root` 只给外层根："
        f"`--bus-root {outer.parent} --project {project_key}`"
        f"（外层根就是默认 {DEFAULT_BUS_ROOT!r} 时把 `--bus-root` 整个省掉即可 ⇒ 存档根 "
        f"{str(Path(DEFAULT_BUS_ROOT, project_key))!r}）。"
        "拼法 `<bus_root>/<project_key>` 的权威出处 = `config/params-ref.md §10`，本入口不替你剥那一段",
        code="bus_root_project_duplicated",
    )


def _refuse_if_git_env_overrides(env: Mapping[str, str], target: Path) -> None:
    """`GIT_DIR` / `GIT_WORK_TREE` / `GIT_CEILING_DIRECTORIES` 有一个在位 ⇒ 在 `mkdir` 之前就拒。

    这一发的坏法与 `_refuse_if_platform_repo` 那一格不同：盘上的一切看起来都"照做了"，
    但 init 与顶层探测打的是**另一个仓库**。
    `GIT_DIR` 让新仓库落在变量指的地方（存档根里那个 `.git` 其实不在），`GIT_WORK_TREE` 让 `add -A`
    去覆盖别人指定的工作树，`GIT_CEILING_DIRECTORIES` 让向上探测提前停下 ⇒ 三种都能把这道"是不是顶层"
    的检查变成读数上的一种。拒在 `mkdir` 之前，是因为拒在之后就已经在他盘上留了一个空目录。

    消息里**只出现变量名**：值是别处的路径，本入口不把它抄进一条会被日志文件接住的行里。
    """
    present = tuple(name for name in GIT_OVERRIDE_ENV_NAMES if (env.get(name) or "").strip())
    if not present:
        return
    raise RealRunRefused(
        f"点火这一发所在的环境里已设 {', '.join(present)}（只点变量名，不点值）⇒ 在存档根 "
        f"{str(target)!r} 上的 `git init` 与顶层探测打的会是那个变量指的仓库，而不是"
        "「这个目录自己就是顶层」这一格要求。请先在同一 shell 里 "
        f"`unset {' '.join(present)}` 再点火；本入口不替你改环境，也不换一个目录去 init",
        code="git_env_overrides_set",
    )


def _read_toplevel(target: Path) -> tuple[Path | None, int | None]:
    """公共件那一次顶层读数，**外加它的退出码**（多取这一个数只是为了把拒绝写成能归因的一行）。

    argv 仍然出自 `archive.repository_toplevel()` ⇒ 本入口不自己拼 `rev-parse`，git 面还是 `init` 那一条；
    取码的办法是把默认执行器包一层记录，而不是另发一发探测（多发一发就得再拼一次 argv）。
    第二格是 `None` 只有一种读法：探测根本没跑（`cadence.assert_allowed` 把 argv 拒了），
    那时"退 0"和"退非 0"都无从谈起 —— 消息里会把这一格单独点出来，不混进"读到 None"那一格。
    """
    seen: list[int] = []

    def recording(argv: tuple[str, ...], cwd: Path) -> cadence.GitResult:
        result = cadence.run_git(argv, cwd)
        seen.append(result.code)
        return result

    top = archive.repository_toplevel(target, runner=recording)
    return top, (seen[0] if seen else None)


def _toplevel_cause(target: Path, top: Path | None, code: int | None) -> str:
    """顶层读不出来时**到底哪一格坏了**的一个原因词（只有词与几个读数，没有 git 的原文）。

    五个原因是五种可独立坏掉的形状，并成一句"不是顶层"就退化成 owner 2026-10-20 第一发点火烧到的那条消息：
    `git init` 退 0、盘上 `.git` 明明在了，而"读到 None"一个字都没说 —— 那一句不可归因，人只能猜。

    实测过的一发（同一条命令，`wsl -u root` 下，2026-10-20）：`git init -q .` 退 **0**，紧接着
    `git rev-parse --show-toplevel` 退 **128** ⇒ 走的是 `toplevel_probe_refused` 那一格
    （`/mnt/d` 上 drvfs 把属主报成 Windows 那一侧的用户，git 的仓库属主检查不肯认这个刚建好的仓库）。
    `toplevel_is_another_repo` 是另一头：git 认了**别的**目录是顶层（默认那条 `results/<project>` 嵌在平台
    仓库里、而它自己的 `.git` 没建起来时，读到的就是平台根）。两格给的下一步动作不一样，所以不合并。

    先后是有内容的：先认"读到了一个别的路径"（它最有指向性），再认"`.git` 不在这儿"，最后才按退出码分。
    """
    if code is None:
        return "probe_did_not_run（顶层探测一次都没发出去 ⇒ argv 被 `cadence` 的允许名单拒了，与本入口无关）"
    if top is not None:
        return (
            f"toplevel_is_another_repo（读到的是 {str(top)!r} ⇒ 这个目录是别人仓库里的一个子目录，"
            "自己不是顶层）"
        )
    if not (target / ".git").exists():
        return (
            f"git_dir_not_at_target（`git init` 退 0 但 {str(target)!r} 里没有 `.git` ⇒ init 落到了别处；"
            "这一格与 `git_env_overrides_set` 那道前置检查是同一条链的两端 —— 那道拦在动手之前，这一格是"
            "动手之后发现 `GIT_DIR` 还是把仓库搬走了）"
        )
    if code != 0:
        return (
            f"toplevel_probe_refused（`.git` 就在这个目录里，而探测退 {code} ⇒ git 自己不肯认这个仓库。"
            "已知成因：当前用户与仓库属主不一致 —— 在 WSL 里以 root 跑 `/mnt/<盘>` 上的 git 必现这一格。"
            "两条修法由你手工选其一：换一个与盘上属主一致的用户点火，或 "
            "`git config --global --add safe.directory <这个目录>`；本入口不替你写全局 git 配置"
        )
    return "toplevel_printed_nothing（探测退 0 但没吐出一个路径 ⇒ 读不出顶层，不猜）"


def ensure_archive_root(
    project_key: str,
    bus_root: Path,
    *,
    emit_to: Callable[[str], object] = print,
    git: Callable[[Sequence[str], Path], int] | None = None,
    probe: Callable[[Path], tuple[Path | None, int | None]] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """决定 2：存档根缺失 ⇒ `mkdir` + `git init`；已经是一个仓库顶层 ⇒ 一个字都不动，只报一行。

    四步的顺序是要命的：

    ① **先过路径重复那道**（`_refuse_if_doubled`）：`--bus-root` 末段与 `--project` 同名是**一次误敲的量级**
       （owner 2026-10-20 第一发就是这样），它比下面两格更常撞到，且拒在盘上什么都没发生最好读。
    ② **再过平台仓库那道防护**（`_refuse_if_platform_repo`）与 git 环境覆盖那道（`_refuse_if_git_env_overrides`），
       都在 `mkdir` 之前 —— "先把目录造出来再说"正好是把陷阱铺好。
    ③ 目录不存在才 `mkdir`（存在但里面不是仓库时只补 init，不动里面的东西：用户可能已经放了文件进去）。
    ④ "是不是仓库顶层"用**公共件** `archive.repository_toplevel()` 判，不在这里自己拼 `rev-parse`：
       那条 argv 与它的允许名单归 `archive/cadence.py`，本入口的 git 面只有 `init` 一个子命令。

    这一步是可重入的：同一个 `--project` 第二次进来不会抹掉存档历史，也不会因为"目录已经在了"而报错。
    写完还要**再读一次**顶层（`git_init_did_not_take` 那一格）：退 0 不等于真成了顶层，
    而"以为成了"的下场是下一次 `add -A` 打在别人的工作树上。拒的时候还要**带上原因词**
    （`_toplevel_cause`）：这一发的两种坏法在盘上留下的东西不一样 —— 一种是 `.git` 根本没落在这儿，
    一种是落在了但 git 不肯认 —— 只报"读到 None"就是把可归因的一条消息换成一句让人猜的话。
    """
    target = archive.archive_root(project_key, bus_root=str(bus_root))
    _refuse_if_doubled(project_key, bus_root, target)
    _refuse_if_platform_repo(target)
    _refuse_if_git_env_overrides(os.environ if env is None else env, target)
    read = probe or _read_toplevel
    made_dir = not target.is_dir()
    if made_dir:
        target.mkdir(parents=True)
    top, first_code = read(target)
    if top is not None and top.resolve() == target.resolve():
        _row("INIT", emit_to=emit_to, archive_root=str(target), git="已有仓库", made_dir=made_dir)
        return target
    code = (git or _run_git)(["init", "-q", "."], target)
    if code != 0:
        raise RealRunRefused(
            f"存档根 {str(target)!r} 的 `git init` 退回 {code} ⇒ 本入口不猜它成没成，也不换一个目录去 init",
            code="git_init_failed",
        )
    after, second_code = read(target)
    if after is None or after.resolve() != target.resolve():
        raise RealRunRefused(
            f"`git init` 退回 0 之后 {str(target)!r} 仍然不是仓库顶层（读到 "
            f"{None if after is None else str(after)}，探测退出码={second_code}）"
            "⇒ 存档点会把 `add -A` 打到别人的工作树上，这一发停在这里比继续跑好。"
            f"原因词={_toplevel_cause(target, after, second_code)}。"
            "这一发留在盘上的目录与 `.git` 本入口不删：按上面那一句修好后可以直接用同一个 `--project` 重跑，"
            f"不想留着就请自己删 {str(target)!r}（`results/` 已被 `.gitignore` 排除，删它不动平台仓库）",
            code="git_init_did_not_take",
        )
    _row("INIT", emit_to=emit_to, archive_root=str(target), git="init", made_dir=made_dir)
    return target


def _confine(project_key: str, bus_root: Path, agent_id: str) -> Path:
    """厂商子进程的家 = **查过的存档根里、属于执行席那一格的 workspace 目录**（owner 2026-10-20 裁决 1：交
    `require_archive_root`，不是 `archive_root`；第五发点火之后收窄到 workspace 那一层）。

    两段各管一件事，**顺序不许换**：

    ① `require_archive_root()` 那两道拒先跑（目录不存在 / 它不是自己的仓库顶层）。两者的差额正是这道防护的价值：
    `archive_root()` 只做拼接，谁都能拼出一个"看着像存档根、其实是别人仓库里一个子目录"的路径；
    `require_archive_root()` 还要求那个目录**自己是仓库顶层**（`archive/root.py` 模块头点名的那次"最坏误伤"——
    不然厂商 agent 在里面敲一条 `git` 就打到平台那棵树上）。② 在这一之前建目录等于"家还没被认下来就先在盘上
    动手"，所以 ① 红的时候盘上不会多出任何东西（反向对照钉这一格）。

    ② 目录名取 `layout.workspace_dir_name(agent_id)`，本文件**不自己拼 `-workspace` 后缀**：collect 侧那条
    归属判定（`_register`，FR-013）认的就是这一个名字，两处各拼一份就是第二个载体，而这两处一旦漂开，
    症状是"厂商跑成功、产物全被拒收"——2026-10-20 第五发点火的现场就是这个形状（它把 `styles.css` 写在
    存档根顶层，四张卡的产物一条都没登记上）。目录在交出去之前 `mkdir(parents=True, exist_ok=True)`：
    外部进程只会往它的 cwd 里写，那个目录不存在时它不会替我们建。

    代价点名：每张卡多一发 `git rev-parse --show-toplevel`。用的还是公共件 `archive.repository_toplevel()`
    里那条**允许名单内**的子命令 ⇒ 本入口的 git 面没有因此扩大（仍是 `init` 一个子命令）。

    `ArchiveRootMisuse` 是 `ValueError` 的后裔，本来就会被 `SWALLOWABLE` 接成"这一张卡失败"；这里换成
    `RealRunRefused` 是为了让它按**配置不成立**整条链停：家给错了，剩下三张卡的家会是同一个坏值，
    一张一张撞没有意义。代号取 `exc.code`（借来的值，本文件不重述那两个字符串字面量）。
    """
    try:
        root = archive.require_archive_root(project_key, bus_root=str(bus_root))
    except archive.ArchiveRootMisuse as exc:
        raise RealRunRefused(
            f"厂商子进程的工作目录这一格不成立：{exc}"
            f"（本入口交出去的必须是 `require_archive_root({project_key!r})` 而不是 `archive_root(...)`："
            "前者额外确认那个目录是自己的仓库顶层，后者只拼路径。这一格不降级 —— 第四发点火的 manifest"
            "里那个 `cwd` 就是「没被要求点名时，子进程继承调用方目录」的样子）",
            code=exc.code,
        ) from exc
    home = root / layout.workspace_dir_name(agent_id)
    home.mkdir(parents=True, exist_ok=True)
    return home


def real_transport(
    agent_id: str,
    gateway: BusGateway,
    cwd: Path,
    *,
    routing: Mapping[str, object] | None = None,
) -> object:
    """厂商 transport 的**唯一构造点**：只调工厂，不在这里 `DoubaoTransport(...)` / `QoderTransport(...)`。

    工厂缺凭据 / 缺模型 ID / 形态不可装 / 不在册 / 子进程形态没交出 `cwd` ⇒ 一律换成本入口的
    `real_wiring_refused` 往上抛。**没有一条路径会退回仿真**（owner 第一步那句"禁止静默降级为 stub"）。

    `routing`（2026-10-31 路线 B）只对路由驱动的 API 执行器有意义：它点名厂商与模型。
    老路径（qoder / doubao）收到 None，行为一字不变；由 `transport_factory_for()` 按请求绑定。
    """
    try:
        return spec_factory.build_real_transport(agent_id, gateway=gateway, cwd=cwd, routing=routing)
    except (spec_factory.RealWiringRefused, spec_factory.SpecRefused) as exc:
        raise RealRunRefused(
            f"{agent_id} 的真装配被拒：{exc}（凭据与模型 ID 只从环境变量来，本入口不补默认值也不降级）",
            code="real_wiring_refused",
        ) from exc


def transport_factory_for(
    session,
    feature_request_id: uuid.UUID,
    base: Callable[[str, BusGateway, Path], object] | None = None,
) -> Callable[[str, BusGateway, Path], object]:
    """把需求单上的 `model_routing` 绑进 transport 工厂（路线 B）。

    没有路由 ⇒ **原样返回** `base`（老行为：qoder 默认、工厂签名一字不改，测试注入的假工厂也照旧可用）。
    有路由 ⇒ 返回 `partial(base, routing=routing)`：工厂仍是三参形状，路由从关键字进来。
    读的是 EN-01 的现值而不是调用方传的值 —— 路由是这一单的事实，多个入口（CLI / 网页续跑）读同一处。
    """
    factory = base or real_transport
    request = session.get(FeatureRequest, feature_request_id)
    routing = routing_lib.routing_of(request) if request is not None else None
    if not routing:
        return factory
    # 路线 C：执行席 key 在进程内存仓里 ⇒ 注进**路由副本**交给工厂（这一份不落库、不回读）。
    from acp import routing_vault

    seat = routing.get("executor")
    key = routing_vault.key_for(feature_request_id, "executor")
    if key and isinstance(seat, dict):
        routing = {**routing, "executor": {**seat, "api_key": key}}
    return partial(factory, routing=routing)


def _gateway(project_key: str, bus_root: Path) -> BusGateway:
    """网关的根 = `archive.archive_root()` 算出来的那一个。本文件不自己拼 `<bus_root>/<project_key>`。"""
    return BusGateway(archive.archive_root(project_key, bus_root=str(bus_root)))


def _refuse_dangerous_task(
    session,
    *,
    task,
    task_id: str,
    feature_request_id: uuid.UUID,
    project_key: str,
    executor: str,
) -> None:
    """P0-2：在 `transport.submit()` 之前过一遍高危动作清单。

    **为什么扫任务文本而不是"executor 真跑出来的命令"**：平台不接厂商工具调用的逐条申报，
    真正会执行 `rm -rf` 的是厂商 CLI 内部；等它跑完再拦已经晚了。这里把任务的目标、输入、
    验收标准、约束拼成"申报动作串"，用 `guard.autonomy.decide()` 判一次 —— 命中即建 D 类单并拒发。
    这是 fail-closed 的上游过滤：**任何提到危险词的任务都先停**，宁可误拦一次，不把用户可控文本
    原样交给一个带完整运行时的第三方 CLI。

    只对 `DANGER_HIT` 动作：`SAFE` 档下 `decide()` 还会返回 `SAFE_REVIEW`（所有可写动作都要人批），
    那是另一条治理策略；本入口如果把它也当成拒绝，就会让每一张卡都停在门口，等于把系统关死。
    """
    declared = "\n".join(
        [
            str(getattr(task, "objective", "") or ""),
            *[str(item) for item in getattr(task, "inputs", ()) or ()],
            *[str(item) for item in getattr(task, "acceptance_criteria", ()) or ()],
            *[str(item) for item in getattr(task, "constraints", ()) or ()],
        ]
    ).strip()
    if not declared:
        return
    decision = guard_autonomy.decide(
        session,
        guard_autonomy.PlannedAction(
            project_key=project_key,
            agent_id=executor,
            command=declared,
            read_only=False,
            auto_rollback=False,
            task_card_id=task_id,
            feature_request_id=feature_request_id,
        ),
        load_danger_list(),
    )
    if decision.outcome != guard_autonomy.DANGER_HIT:
        return
    ticket = guard_autonomy.hold_ticket(session, decision)
    raise DangerActionBlocked(
        f"任务文本命中高危动作清单 {decision.hits} ⇒ 拒绝下发，已建 D 类单据 {ticket.ticket_id} 等人工审批",
        hits=tuple(decision.hits),
        ticket_id=str(ticket.ticket_id),
    )


def _produce(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    project_key: str,
    bus_root: Path,
    executor: str,
    emit_to: Callable[[str], object],
    transport_factory: Callable[[str, BusGateway, Path], object],
) -> TaskResult:
    """真干活那一发：投影任务卡 → 构造真 transport → `submit()` → `collect()`。

    两条身份口径不在这里自造：

    - `Task` 的 13 项必填由 `run._task_of()` 投（全仓唯一那份投影，理由见模块头"两个私有件的借用"）；
    - 执行席的岗位由 `bus_actor.actor_for_seat(seat="executor")` 取，读的是 EN-02 的 `executor_role_id` 现值。
      今天生产路径上**没有人写过那一列**（唯一写者是拆单时的 `None`），所以读出来是
      `acl.NO_ROLE_DECLARED` 那个"未申报"值，而不是本入口编一个岗名。
      **豆包那一支会在发出第一份报文之前拒掉"指派到未申报岗位"的任务卡**（`_refuse_unmounted_role`），
      Qoder 那一支没有这道校验 —— 这一格不对称是本轮现读注册表读出来的事实（`agents.yaml` 里豆包只挂
      `reviewer`），登记进台账，不在这里替谁圆场。

    递给工厂的**第三格是家**：`_confine(project_key, bus_root, executor)` = 查过的存档根里、执行席那一格的
    workspace 目录（owner 2026-10-20 裁决 1 + 第五发点火的收窄）。这一格不在这里猜、也不留给 transport 的
    默认值 —— 缺省继承的是本进程的 cwd，而本入口是在平台仓库根里敲的，第四发点火的 manifest 因此把平台
    源码交给了那次外部执行。
    """
    role_id = str(
        bus_actor.actor_for_seat(
            session, feature_request_id=feature_request_id, task_id=task_id, seat="executor"
        ).role_id
    )
    task = run._task_of(
        session, feature_request_id, task_id, project_key=project_key, executor_role_id=role_id
    )
    _refuse_dangerous_task(
        session,
        task=task,
        task_id=task_id,
        feature_request_id=feature_request_id,
        project_key=project_key,
        executor=executor,
    )
    transport = transport_factory(
        executor, _gateway(project_key, bus_root), _confine(project_key, bus_root, executor)
    )
    _row(
        "SUBMIT",
        emit_to=emit_to,
        card=task_id,
        agent=executor,
        role=role_id,
        model=getattr(getattr(transport, "api", None), "model", None),
    )
    handle = transport.submit(task)
    result = transport.collect(handle)
    for ref in result.artifacts:
        _row(
            "ARTIFACT",
            emit_to=emit_to,
            card=task_id,
            path=ref.rel_path,
            bytes=ref.bytes,
            lines=ref.lines,
            format=ref.format,
        )
    _row(
        "COLLECT",
        emit_to=emit_to,
        card=task_id,
        agent=executor,
        native_ref=handle.native_ref,
        self_reported=result.self_reported_status,
        artifacts=len(result.artifacts),
    )
    return result


def _human_stop(
    edge: str,
    *,
    session,
    feature_request_id: uuid.UUID,
    task_id: str,
    now: dt.datetime,
    emit_to: Callable[[str], object],
) -> Callable[[dict], dict]:
    """`Review` / `HandOff` 两条出边上的**人工停点**。

    两条边的动作不同，这一点是 M2-W8 之后收紧的：
    - `review` 边：门禁已判 valid ⇒ 把卡推进 `REVIEWING`（状态机上 `RUNNING → REVIEWING` 的写者就是
      `Review` 节点，`validate_artifact.py:27` 那句）。**不产结论、不碰 EN-08** —— 结论归人，
      由 `acp.review` 端点/CLI 收。
    - `handoff` 边：一字不写，`ARTIFACT_INVALID` 是门禁自己落的，换人仍归 `handoff()`（本入口不代做）。

    为什么还要把两条边都装上，而不是绕过图直接调 `record_result()` + `validate_card()`：
    T076 那张图"出边恰好两条"是 FR-018 的结构保证（`build_execution_graph` 缺任何一个下游节点体就在装配期
    报错）。把停点做成"节点体里只做状态迁移"而不是"图上少一条边"，本入口跑的就是生产那张图。
    """

    def node(state: dict) -> dict:
        if edge == EDGE_REVIEW and state.get("validated_status") == validate_artifact.VALID:
            card = (
                session.query(TaskCard)
                .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
                .one_or_none()
            )
            if card is not None and str(card.status) == validate_artifact.VALIDATABLE:
                prior = str(card.status)
                card.status = review_node.REVIEWING
                events.record(
                    session,
                    entity="task_card",
                    entity_id=card.id,
                    kind="REVIEW",
                    actor_agent_id=run.ACTOR_AGENT_ID,
                    payload={
                        "node": review_node.NODE,
                        "action": review_node.ACTION_OPENED,
                        "prior_status": prior,
                        "card_status_after": review_node.REVIEWING,
                    },
                    created_at=now,
                )
                session.flush()
        _row(
            "AWAIT",
            emit_to=emit_to,
            request=str(feature_request_id),
            card=task_id,
            **{"next": edge, "validated": state.get("validated_status"), "hint": STOP_HINT},
        )
        return {}

    return node


def _routed_acceptor(session, feature_request_id: uuid.UUID):
    """这一单是否点名了验收模型 ⇒ `(vendor, model, base_url, key_fp)` 或 `None`。"""
    request = session.get(FeatureRequest, feature_request_id)
    return routing_lib.seat_model(routing_lib.routing_of(request), "acceptor")


def _accept_by_model(acceptor, *, request_id, session, feature_request_id, task_id, bus_root) -> tuple[str, str]:
    """调用通用验收 Runner；任何失败折成 NEED_HUMAN（不把异常当 ACCEPT，也不把卡判死）。"""
    from acp import routing_vault
    from acp.accept.routing_client import decide
    from acp.review.routing_client import seat_for

    try:
        request = session.get(FeatureRequest, feature_request_id)
        card = (
            session.query(TaskCard)
            .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
            .one_or_none()
        )
        if request is None or card is None:
            return "NEED_HUMAN", "缺需求单或任务卡 ⇒ 验收模型无从判定"
        routing = routing_lib.routing_of(request)
        seat = seat_for(routing, "acceptor", request_id=request_id, vault_key_for=routing_vault.key_for)
        rows = (
            session.query(Artifact)
            .filter(Artifact.feature_request_id == feature_request_id, Artifact.task_card_id == task_id)
            .all()
        )
        archive_root = archive.require_archive_root(str(request.project_key), bus_root=bus_root)

        def _preview(rel_path: str) -> str:
            try:
                return (archive_root / rel_path).read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                return ""

        artifacts = [
            {
                "rel_path": str(row.rel_path),
                "filename": str(row.rel_path).replace("\\", "/").rsplit("/", 1)[-1],
                "bytes": int(row.bytes),
                "lines": int(row.lines),
                "format": str(row.format),
                "validated_status": str(row.validated_status or ""),
                "content": _preview(str(row.rel_path)),
            }
            for row in rows
        ]
        return decide(
            seat,
            requirement=str(request.raw_input),
            criteria=tuple(str(item) for item in (card.acceptance_criteria or ())),
            artifacts=artifacts,
            evidence="产物已通过门禁（valid）",
        )
    except Exception as exc:  # noqa: BLE001 - 验收失败不是交付失败，退回人工
        return "NEED_HUMAN", f"{type(exc).__name__}: {exc}"[:300]


def _sign_by_gate(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    bus_root: Path,
    now: dt.datetime,
    emit_to: Callable[[str], object],
) -> str:
    """`ACP_REVIEW_MODE=auto`：门禁判 valid 的那一刻，由门禁自己把这一轮签掉。

    **为什么签在这里、而不是等 `WebResumer` 的审查跑者**：那一段在锁释放之后、另一个会话里，
    中间那一小段时间卡是 `REVIEWING` —— 看板上它会一闪而过地出现在「评审 1」里，
    而人点进去时它已经自己过了。同一件事有两个触发点，就是"同一张卡被签两次"的形状。
    签在跑卡事务里的另一个好处：结论、验收、交付与"卡跑完了"是同一次提交，
    界面读到的要么是"还在跑"，要么是"已交付"，不会读到一个半截态。

    **失败不抛**（与 `escalate_card_after_failure` 同一条纪律）：签不掉时卡留在 `REVIEWING`，
    人工照旧接得住，同时留一条 `review_gate_failed` 说明为什么。
    抛出去会让"产物明明达标、只是验收写不进去"的一张卡整张算失败 —— 那比退回人工更糟。
    """
    evidence = "门禁代签：产物过门禁即视为通过（ACP_REVIEW_MODE=auto）"
    acceptor = _routed_acceptor(session, feature_request_id)
    if acceptor is not None:
        decision, reason = _accept_by_model(
            acceptor,
            request_id=feature_request_id,
            session=session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            bus_root=bus_root,
        )
        if decision != "ACCEPT":
            code = f"accept_{decision.lower()}"
            events.record(
                session,
                entity=FAILURE_EVENT_ENTITY,
                entity_id=task_id,
                kind=FAILURE_EVENT_KIND,
                actor_agent_id=review_policy.GATE_REVIEWER,
                payload={"node": "realrun", "action": "accept_model_refused", "code": code, "detail": reason},
                created_at=now,
            )
            _row("SIGN", emit_to=emit_to, card=task_id, ok=False, code=code, error=reason[:300])
            return code
        evidence = f"验收模型 {acceptor[0]}/{acceptor[1]} 给出 ACCEPT：{reason}"
    try:
        outcome = review_service.submit_review(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            # 主语是门禁，不是 `orchestrator`：这条结论**没有人也没有模型**给过，
            # 写 `orchestrator` 会让审计里"谁判的"这一格读成"编排器判的"，而它根本没有审查的职责。
            principal=review_policy.GATE_REVIEWER,
            verdict=review_node.APPROVE,
            evidence=evidence,
            bus_root=bus_root,
            now=now,
            accept_for=review_service.GatePassAcceptance(),
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring 第 2 段
        # `code` 单独取出来写进消息：`ReviewFlowError` 的 str 只有人话，稳定键名在属性上，
        # 而看日志的人要靠那个键名区分"签不动"的几种原因（`state_not_reviewing` /
        # `findings_not_compliant` / `reviewer_mismatch`），只读人话会以为都是同一件事。
        code = str(getattr(exc, "code", type(exc).__name__))
        note = f"{code}: {type(exc).__name__}: {exc}"
        events.record(
            session,
            entity=FAILURE_EVENT_ENTITY,
            entity_id=task_id,
            kind=FAILURE_EVENT_KIND,
            actor_agent_id=review_policy.GATE_REVIEWER,
            payload={"node": "realrun", "action": "review_gate_failed", "code": code, "detail": note},
            created_at=now,
        )
        _row("SIGN", emit_to=emit_to, card=task_id, ok=False, code=code, error=note[:300])
        return f"gate_sign_failed:{code}"
    _row(
        "SIGN",
        emit_to=emit_to,
        card=task_id,
        ok=True,
        status=outcome.card_status,
        accepted=outcome.accepted,
        proxy=outcome.acceptance_proxy,
    )
    return str(outcome.card_status)


def retry_invalid_cards(
    session,
    *,
    feature_request_id: uuid.UUID,
    now: dt.datetime,
    emit_to: Callable[[str], object] = print,
    retried: set[str] | None = None,
) -> list[str]:
    """`auto` 模式：把这张单里**已经停在 `ARTIFACT_INVALID`** 的卡放回可派发态，返回放回去的卡号。

    为什么需要这一手（与 `sign_waiting_reviews` 同一条理由）：`_retry_invalid` 只在"这一圈跑出
    一张不达标的卡"的那一刻生效，而现场更常见的是**这一圈开始之前**它就已经停在那儿了
    （上一圈跑坏、或者切模式之前留下的）。不扫一遍，人还得自己点「重跑这张卡」。

    `retried` 由调用方持有：同一张卡在同一圈里只重跑一次，扫过的卡号会被写进去，
    免得"扫一遍 + 跑坏了再重跑一次"变成两次。
    """
    done = retried if retried is not None else set()
    waiting = [
        str(row.id)
        for row in session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status == artifact_retry.TRIGGER,
        )
        .order_by(TaskCard.id)
        .all()
        if str(row.id) not in done
    ]
    retried_ids: list[str] = []
    for card_id in waiting:
        done.add(card_id)
        status = _retry_invalid(
            session,
            feature_request_id=feature_request_id,
            task_id=card_id,
            now=now,
            emit_to=emit_to,
        )
        if not status.startswith("retry_refused:"):
            retried_ids.append(card_id)
    return retried_ids


def _retry_invalid(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    now: dt.datetime,
    emit_to: Callable[[str], object],
) -> str:
    """`auto` 模式：门禁判产物不达标 ⇒ 把卡放回可派发态，让这一圈再派它一次。

    为什么这条**不是**"把不合格的东西签过去"：`ARTIFACT_INVALID` 说的是"这一趟没交出合格的产物"
    （现场两次都是执行器返回零产物 `examined: 0`），不是"产物不好但我放过它"。
    该做的决定是"再跑一次"，那是机器自己能下的判断；把它推给人，人能做也只是点一下「重跑这张卡」。

    为什么用 `artifact_retry.resolve_artifact_retry()` 而不是自己写状态：那是这条边**唯一**的实现
    （目标态从 `data-model.md §2` 原文解析、不动执行者、不动 `handoff_count`）。
    人工入口 `POST /tasks/{id}/artifact-retry` 走的是同一个函数 —— 两条路一个判定，不抄第二份。

    主语写 `gate-reviewer`：这个决定是门禁下的，不是"有人判过"。
    """
    try:
        outcome = artifact_retry.resolve_artifact_retry(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            principal=review_policy.GATE_REVIEWER,
            reason="门禁判产物不达标（ACP_REVIEW_MODE=auto）⇒ 自动原地重跑一次",
            created_at=now,
        )
    except artifact_retry.ArtifactRetryMisuse as exc:
        # 挪不动（执行者空 / 状态不是 ARTIFACT_INVALID）：如实记一条，卡留在原地等人 ——
        # 那正是这条边该交回给人的形状（例如"这张卡没有执行者"，机器没有可派的人）。
        _row("RETRY", emit_to=emit_to, card=task_id, ok=False, code=exc.code, error=str(exc)[:200])
        return f"retry_refused:{exc.code}"
    _row(
        "RETRY",
        emit_to=emit_to,
        card=task_id,
        ok=True,
        prior=outcome.prior_status,
        status=outcome.status_after,
        executor=outcome.executor_agent_id,
    )
    return str(outcome.status_after)


#: 需求单的"这一单已经结束"两态（域里只有这两个终态，`enums.py:38`）。
#: 与 `CARD_SETTLED` 不是同一份：卡收尾 ≠ 单收尾（单要等所有卡都收尾），别拿卡那份去套。
REQUEST_SETTLED = frozenset({"DELIVERED", "FAILED"})

#: 上一单产物被挪去的地方（工作区内的相对目录）。
QUARANTINE_DIR = "_prev"


def quarantine_foreign_artifacts(
    session,
    *,
    feature_request_id: uuid.UUID,
    project_key: str,
    bus_root: Path,
    now: dt.datetime,
    emit_to: Callable[[str], object] = print,
) -> list[str]:
    """把工作区里**属于别的（已结束的）需求单**的产物挪进 `_prev/<那一单>`，返回挪走的相对路径。

    **为什么必须做**（2026-10-28 现场，两单都栽在同一处）：工作区是
    `<bus_root>/<project_key>/<agent>-workspace`，**同一个 project_key 下的所有需求单共用它**。
    上一单的 `server.py` 还在，执行器一看"已经有了"就不写 —— qoder 的原话是
    *"The existing `server.py` already meets all requirements"* —— 而收集侧只登记**本趟写过的**
    文件（`QoderTransport.collect()` 读的是工具调用流水里的写操作）⇒ 零产物 ⇒
    `validate_artifact` 判 `ARTIFACT_INVALID`。重跑多少次都一样：那份文件一直在。

    三道收窄，缺一条就会误伤：

    ① **只挪"已结束"的单**（`REQUEST_SETTLED`）：正在跑的那一单的文件一格不动 ——
       否则同时跑两单时，后开的那单会把前一单的产物搬走。
    ② **只挪登记在 `artifact` 表里的路径**：表里没有的文件不碰。它可能是执行器自己放的中间物，
       也可能是人放进来的东西；"不在账上的文件"归谁不清，不清就不动。
    ③ **本单的产物一格不动**：依赖链靠它们（T02 要用 T01 的 css）。

    挪而不是删：这些文件属于上一单的交付证据，删掉等于把它的账做平。
    """
    rows = (
        session.query(Artifact.rel_path, Artifact.feature_request_id)
        .join(FeatureRequest, FeatureRequest.id == Artifact.feature_request_id)
        .filter(
            FeatureRequest.project_key == project_key,
            Artifact.feature_request_id != feature_request_id,
            FeatureRequest.status.in_(sorted(REQUEST_SETTLED)),
        )
        .all()
    )
    if not rows:
        return []
    try:
        root = archive.require_archive_root(project_key, bus_root=str(bus_root))
    except archive.ArchiveRootMisuse:
        # 家还没被认下来 ⇒ 一格不动（`_confine` 会在真正派发时把它响亮地拒掉）。
        return []
    moved: list[str] = []
    for rel_path, owner in rows:
        rel = str(rel_path).replace("\\", "/")
        source = root / rel
        if not source.is_file():
            continue
        target = root / QUARANTINE_DIR / str(owner) / rel
        if target.exists():
            target = target.with_name(f"{target.stem}.{int(now.timestamp())}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        moved.append(rel)
    if moved:
        _row("QUARANTINE", emit_to=emit_to, moved=len(moved), dir=QUARANTINE_DIR)
    return moved


def sign_waiting_reviews(
    session,
    *,
    feature_request_id: uuid.UUID,
    bus_root: Path,
    now: dt.datetime,
    emit_to: Callable[[str], object] = print,
) -> list[str]:
    """`auto` 模式：把这张单里**已经停在 `REVIEWING`** 的卡按门禁结论补签掉，返回签掉的卡号。

    为什么需要这一手：`_sign_by_gate` 只在"本次跑出一张 valid 卡"的那一刻生效，而现场最常见的形状是
    **切模式之前**已经有一批卡停在评审里（这一单就是：切到 `auto` 时 T05 / T06 正等着人点）。
    不补签，切过去之后人还是要为历史那几张各点一次 —— 与"不用守着点"正好相反。

    `deepseek` 那条路**故意不这么做**（Phase 17 D6：不追溯已在 `REVIEWING` 的卡）：那是"换一个审查员"，
    补签等于让新审查员去审一批没让它看过的产物。`auto` 不是换审查员 —— 那条路上根本没有审查员，
    卡停在那里只是因为没人点，所以补签与它自己的语义一致。

    幂等：签过的卡不再是 `REVIEWING`，下一次续跑扫不到它。
    """
    waiting = [
        str(row.id)
        for row in session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status == review_node.REVIEWING,
        )
        .order_by(TaskCard.id)
        .all()
    ]
    signed: list[str] = []
    for card_id in waiting:
        status = _sign_by_gate(
            session,
            feature_request_id=feature_request_id,
            task_id=card_id,
            bus_root=bus_root,
            now=now,
            emit_to=emit_to,
        )
        if not status.startswith("gate_sign_failed:"):
            signed.append(card_id)
    if waiting:
        _row("SIGN", emit_to=emit_to, cards=",".join(signed) or "-", waiting=len(waiting), sweep=True)
    return signed


def _artifact_rows(session, feature_request_id: uuid.UUID, artifact_ids: Sequence[uuid.UUID]) -> list[Artifact]:
    """本次回收那一批产物行的现值。

    按 **id 集合**取而不是"读这张卡的全部产物行"：后者会让换人重试时把上一轮的行也数进这一轮的读数里
    （`validate_artifact.py:165-167` 那句同一条理由）。`feature_request_id` 一起进过滤条件是防串单。
    """
    ids = list(artifact_ids)
    if not ids:
        return []
    return list(
        session.query(Artifact)
        .filter(Artifact.id.in_(ids), Artifact.feature_request_id == feature_request_id)
        .order_by(Artifact.rel_path)
        .all()
    )


def _status_of(session, feature_request_id: uuid.UUID, task_id: str) -> str:
    """卡态的**库里现值**（停在哪一格以它为准，不以图 state 的回执为准）。"""
    row = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if row is None:
        raise RealRunRefused(
            f"卡 {task_id} 在需求单 {feature_request_id} 下读不出来 ⇒ 收尾读数没有对象",
            code=run.CARDS_MISSING,
        )
    return str(row.status)


#: 一张卡"派发过、但没跑完"之后落到的态（2026-10-28）。
#: 为什么是 `ESCALATED` 而不是 `REWORK`：`data-model.md §2` 给 `RUNNING` 的出边只有两条
#: （`valid ──▶ REVIEWING` / `invalid ──▶ ARTIFACT_INVALID`），"执行器崩了/写不出东西"**没有边**。
#: 没有边不等于可以自己发明一条 —— `REWORK` 的入边是 `CHANGES_REQUESTED`（审查意见），
#: 把"崩了"记成"返工"会让 `rework_count` 说一件没发生过的事。
#: `ESCALATED` 是文档里唯一"停下、交人工、且有出边"的态（`ESCALATED ──人工──▶ READY / REJECTED`），
#: 而本文件从第一版起就在 `run_card_segment` 的 docstring 里宣称"停下、留痕、交人工"——
#: 那一句此前**没有实现**（只写了留痕就 return，卡留在 `RUNNING`），这里把它兑现。
#: **代价如实登记**：`ESCALATED` 的文档入边只有两条（换人/工具超限、冲突自裁），这是第三条，
#: 归入"待补文档项"，判据 `test_a_failed_card_leaves_running_and_can_be_handed_back` 钉住现状。
FAILED_CARD_STATE = "ESCALATED"
FAILED_CARD_ACTION = "card_escalated_after_failure"


def escalate_card_after_failure(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    code: str,
    detail: str,
    now: dt.datetime,
    actor_agent_id: str = run.ACTOR_AGENT_ID,
) -> bool:
    """把这张卡从 `RUNNING` 挪到 `ESCALATED` 并留痕。挪了返回 `True`，卡不在 `RUNNING` 就什么都不做。

    这个函数存在的原因是**死锁**：卡由 `Dispatch` 置 `RUNNING`，跑挂了之后没人把它挪走，
    而 `DISPATCHABLE = {READY, HANDED_OFF, REWORK}` 里没有 `RUNNING` ⇒ 之后每一次
    `run_real()` 都读到"没有可派发的卡"，整条链静默停住，界面上连一个能点的东西都没有。
    现场是 `real_demo / 7800b3dd` 的 T05（2026-10-28，`submit-contact.php` 被总线拒之后）。

    **调用方要保证这一次写入能活下来**：`serve_resumer` 的失败路径上主工作单元会回滚，
    那里的调用点必须另开一个会话（见 `WebResumer._release_stuck_cards`）。
    """
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None or str(card.status) != dispatch_node.STARTED:
        return False
    prior = str(card.status)
    card.status = FAILED_CARD_STATE
    events.record(
        session,
        entity=FAILURE_EVENT_ENTITY,
        entity_id=task_id,
        kind=FAILURE_EVENT_KIND,
        actor_agent_id=actor_agent_id,
        payload={
            "node": "realrun",
            "action": FAILED_CARD_ACTION,
            "prior_status": prior,
            "card_status_after": FAILED_CARD_STATE,
            "code": code,
            "detail": detail[:400],
            "exit_hint": "POST /tasks/<卡号>/escalation {decision: READY} 放回待跑，再 POST /requests/<单号>/continue",
        },
        created_at=now,
    )
    session.flush()
    return True


#: 需求单状态（EN-01 的 `feature_request_status` 域）。这里只写本文件会落到的那三个：
#: 需求单在受理段由 `INTAKE` 走到 `SPLIT`，之后**再也没人写过它** —— 2026-10-28 的读数：
#: 连跑通交付包的那一单（`fcae8033`）状态都还停在 `SPLIT`，界面因此永远显示"已立项"。
REQUEST_RUNNING = "RUNNING"
REQUEST_DELIVERED = "DELIVERED"
#: 卡到这几个态就算"这张卡不会再自己动了"。与 `persistence/states.py::TERMINAL_STATES` 同一份
#: 口径（`DELIVERED` / `DELIVERED_WITH_ISSUES`）再加 `REJECTED` —— 驳回的卡也不会再跑。
CARD_SETTLED = frozenset({"DELIVERED", "DELIVERED_WITH_ISSUES", "REJECTED"})


#: 卡停在 `RUNNING` 之后被收回重排时，第一段留痕的动作名（第二段是 `escalation.human_exit`）。
RECLAIM_ACTION = "card_reclaimed_from_orphan_running"


def reclaim_orphaned_cards(
    session,
    *,
    feature_request_id: uuid.UUID,
    principal: str,
    now: dt.datetime,
    actor_agent_id: str = run.ACTOR_AGENT_ID,
) -> tuple[str, ...]:
    """把"停在 `RUNNING`、却没有任何进程在推"的卡收回 `READY`；返回被收回的卡号。

    为什么必须补这一格：`DISPATCHABLE` 里没有 `RUNNING`，所以一张卡只要停在 `RUNNING` 就**再也没有入口**。
    执行失败那条路已经由 `escalate_card_after_failure` 兜住；**进程被杀**（Ctrl-C、机器重启、
    看板那个后台线程被回收）那条路没人兜 —— 卡留在 `RUNNING`，`readiness()` 只收 `READY`，
    于是「继续执行」点多少次都只是把别的卡跑完，这一张永远交付不了，需求单也就永远到不了 `DELIVERED`。
    2026-10-28 的库里就有两张这样的单（`9e827757` / `070eeb46`：T01 停在 `RUNNING`，T02–T04 还是 `READY`）。

    为什么敢收回：调用方此刻持有这一单的续跑锁（`serve_resumer` 每单一把，`already_running` 会直接跳过），
    而本进程一张卡都还没派过 ⇒ 库里那些 `RUNNING` 不可能由**这次**续跑持有。所以这一格**只在人显式按下
    「继续执行」时**走（`reclaim_principal` 非空）；CLI 与「批准」那两条路径一字未变 ——
    它们的现场里可能有另一个进程正握着卡，收回就是把卡从别人手里抢走。

    两步而不是一步：`RUNNING → ESCALATED` 说的是"这张卡没人推了"（机器判的，主语 orchestrator），
    `ESCALATED → READY` 说的是"人可以放它回队列"（主语是按下按钮的那个人）。合成一步就等于
    让机器替人做判断，而状态机里这两条边的语义本来就不同 —— 留痕也要能分开读。
    """
    rows = (
        session.query(TaskCard)
        .filter(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status == dispatch_node.STARTED,
        )
        .order_by(TaskCard.id)
        .all()
    )
    reclaimed: list[str] = []
    for row in rows:
        task_id = str(row.id)
        if not escalate_card_after_failure(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            code="orphaned_running",
            detail="这一单被要求继续执行时，这张卡仍停在 RUNNING，而本次续跑一张卡都还没派过 ⇒ 推它的那个进程已经不在了",
            now=now,
            actor_agent_id=actor_agent_id,
        ):
            continue
        escalation.resolve_escalation(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            decision="READY",
            principal=principal,
            created_at=now,
            reason="继续执行时收回没人推的卡",
        )
        reclaimed.append(task_id)
    return tuple(reclaimed)


def sync_request_status(session, feature_request_id: uuid.UUID, *, now: dt.datetime) -> str | None:
    """把需求单状态对齐到卡的实际进度；返回写进去的新值，没变就是 `None`。

    两条规则，都只看卡，不看图 state（图 state 是"这一跑走到哪"，卡是"事实走到哪"）：
    - 全部卡都落在 `CARD_SETTLED` 里 ⇒ `DELIVERED`；
    - 否则只要离开过 `SPLIT` 就算 `RUNNING`。

    **只在 `SPLIT` / `RUNNING` 之间推进**：`INTAKE` 是受理段还没跑完（那是 `serve_intake` 的地盘），
    `FAILED` 是受理失败（U6 的现场，不能被一次续跑悄悄抹掉），`DELIVERED` 是终态。
    写 `RUNNING` 之前先读一次现值，值没变就不写 —— 每张卡跑完都写一次库不是问题，
    每张卡都留一行"状态变了"的假留痕才是。
    """
    request = session.get(FeatureRequest, feature_request_id)
    if request is None:
        return None
    current = str(request.status)
    if current not in ("SPLIT", REQUEST_RUNNING):
        return None
    rows = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .all()
    )
    if not rows:
        return None
    target = REQUEST_DELIVERED if all(str(row.status) in CARD_SETTLED for row in rows) else REQUEST_RUNNING
    if target == current:
        return None
    request.status = target
    events.record(
        session,
        entity="feature_request",
        entity_id=str(feature_request_id),
        kind="TASK",
        actor_agent_id=run.ACTOR_AGENT_ID,
        payload={
            "node": "realrun",
            "action": "request_status_synced",
            "prior_status": current,
            "status_after": target,
            "cards": {str(row.id): str(row.status) for row in rows},
        },
        created_at=now,
    )
    session.flush()
    return target


def run_card_segment(
    session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    project_key: str,
    bus_root: Path,
    now: dt.datetime,
    slots,
    leases: scheduler.LeaseLedger,
    executor: str,
    emit_to: Callable[[str], object] = print,
    transport_factory: Callable[[str, BusGateway, Path], object] = real_transport,
    review_mode: str = review_policy.REVIEW_MODE_MANUAL,
) -> CardOutcome:
    """一张卡的精简执行流：真产出 → `result_ingest` → `validate_artifact` → 人工停点 → 无论如何退槽。

    与 `run.run_execute()` 的三处差别，逐条对应 owner 的决定：

    ① `produce` 交的是 `transport_factory` 造出来的**真** transport（决定 3：不复用 `run_execute`，
       所以本函数既不碰 `resolve_transport_flag`，也不构造 `StubAdapter`）；
    ② `review` / `handoff` 两个节点体是 `_human_stop()`（决定 1：不代填结论，跑到门禁就停）；
    ③ 失败不让整条链崩（owner 第三步 2"不允许崩溃"）。他给的出口是 `FAILED` 或 R-26 卡点：
       卡态域里**没有**给卡用的 `FAILED`（`enums.py:38` 那一支属于 EN-01 需求单那一列），
       所以本发走后半条 —— 停下、留痕、交人工。留痕只走 `events.record()` 那一个写者（它会 `wash()`
       整个 payload），本函数不另开一条落盘路径。

    两类错**故意不接**（`ASSEMBLY_DEFECTS` 那份名单）：`scheduler.MissingLease`（没派发就跑执行段 = 本入口
    自己的顺序错了）与 `scheduler.SlotLeak`（借还不成对，吞掉它就是"跑几次把并发上限吃光后静默阻塞"那条路）。
    库错（SQLAlchemy 那一支）既不在 `SWALLOWABLE` 里也不在名单里 ⇒ 照样抛出去：库里的事实坏了再往下跑
    只会多写几行脏的。
    """

    def segment(state: dict) -> dict:
        graph = orchestrate_graph.build_execution_graph(
            orchestrate_graph.ExecutionDeps(
                session=session,
                created_at=now,
                actor_agent_id=executor,
                bus_root=archive.archive_root(project_key, bus_root=bus_root),
            ),
            review=_human_stop(
                EDGE_REVIEW,
                session=session,
                feature_request_id=feature_request_id,
                task_id=task_id,
                now=now,
                emit_to=emit_to,
            ),
            handoff=_human_stop(
                EDGE_HANDOFF,
                session=session,
                feature_request_id=feature_request_id,
                task_id=task_id,
                now=now,
                emit_to=emit_to,
            ),
        )
        return dict(graph.invoke(state))

    def produce() -> TaskResult:
        return _produce(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            project_key=project_key,
            bus_root=bus_root,
            executor=executor,
            emit_to=emit_to,
            transport_factory=transport_factory,
        )

    try:
        outcome = scheduler.run_card(
            feature_request_id=feature_request_id,
            task_id=task_id,
            slots=slots,
            leases=leases,
            produce=produce,
            segment=segment,
        )
    except Exception as exc:  # noqa: BLE001 - 收不收由 `_swallowable()` 判，两种装配缺陷在这里原样抛出去
        if not _swallowable(exc):
            raise
        # 走到这里的是 transport 层与协议层的错。留痕只走 `events.record()` 那一个写者（它会 `wash()`
        # 整个 payload），本函数不另开第二条落盘路径；消息里的原文摘要不带凭据（三支 transport 的错误类
        # 各自的模块头都明写"消息里只有字段名与路径"）。
        note = f"{type(exc).__name__}: {exc}"
        events.record(
            session,
            entity=FAILURE_EVENT_ENTITY,
            entity_id=task_id,
            kind=FAILURE_EVENT_KIND,
            actor_agent_id=run.ACTOR_AGENT_ID,
            payload={"node": "realrun", "action": FAILURE_EVENT_ACTION, "detail": note},
            created_at=now,
        )
        _row("FAIL", emit_to=emit_to, card=task_id, agent=executor, error=note[:300])
        # 留痕之后必须**把卡挪出 `RUNNING`**：只留痕不挪态，这张卡就再也没有入口了（见
        # `escalate_card_after_failure` 的 docstring）。放在这里而不是 `finally`：
        # 这一段是"这一张卡失败了"，成功路径不该被它碰到。
        escalate_card_after_failure(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            code=str(getattr(exc, "code", type(exc).__name__)),
            detail=note,
            now=now,
        )
        return CardOutcome(
            card_id=task_id,
            agent_id=executor,
            validated_status=None,
            card_status=_status_of(session, feature_request_id, task_id),
            artifacts=(),
            error=note,
        )

    state = outcome.segment_result or {}
    validated = state.get("validated_status")
    rows = _artifact_rows(session, feature_request_id, list(state.get("artifact_ids") or ()))
    _row(
        "GATE",
        emit_to=emit_to,
        card=task_id,
        agent=executor,
        validated=validated,
        artifacts=len(rows),
        reason=state.get("invalid_reason"),
    )
    # `auto` 模式：门禁判 valid ⇒ 当场签掉，卡不进人工评审（`_sign_by_gate` 的 docstring 说明为什么在这里）。
    # 只对 `valid` 那一支生效：`invalid` 走的是 `ARTIFACT_INVALID`，它的出口是
    # `POST /tasks/{id}/artifact-retry`（人工决定再跑一次），不是"签个字就过去"。
    if review_mode == review_policy.REVIEW_MODE_AUTO and validated == validate_artifact.VALID:
        _sign_by_gate(
            session,
            feature_request_id=feature_request_id,
            task_id=task_id,
            bus_root=bus_root,
            now=now,
            emit_to=emit_to,
        )
    return CardOutcome(
        card_id=task_id,
        agent_id=executor,
        validated_status=validated,
        card_status=_status_of(session, feature_request_id, task_id),
        artifacts=tuple(
            (str(row.rel_path), int(row.bytes), int(row.lines), str(row.format)) for row in rows
        ),
    )


def run_real(
    session,
    *,
    feature_request_id: uuid.UUID,
    project_key: str,
    bus_root: Path,
    now: dt.datetime,
    emit_to: Callable[[str], object] = print,
    transport_factory: Callable[[str, BusGateway, Path], object] = real_transport,
    commit_after_dispatch: Callable[[], object] | None = None,
    review_mode: str = "manual",
    reclaim_principal: str | None = None,
) -> list[CardOutcome]:
    """把这张需求单里**过了就绪判定**的卡逐张跑到门禁，每轮只派发一张（Q10 甲那条生产驱动纪律）。

    `max_cards_per_round=1` 写死在这里不是保守：`run.run_chain()` 那段记的是一次真挂死 —— 一轮派两张、
    第二张在同一调用栈里等一张只有执行段跑完才会还的槽。轮数上限借 `run.MAX_CHAIN_ROUNDS` 那一个数，
    本文件不留第二份；停轮条件也借 `run.NOTHING_DISPATCHABLE` 那一个代号，不自己判"还能不能派"。

    `commit_after_dispatch` 是**看板那一侧要的提交点**，CLI 不传（= 整发一个事务，口径一字未变）。
    为什么必须在这里：派发（卡态 + `TASK dispatched` 留痕）与厂商调用在同一个工作单元里，
    而厂商那 900 秒是一整段；看板没有 stdout，不提交的话"点批准之后发生了什么"要等厂商跑完才读得到。
    提交点仍在**调用方**手里（本文件只负责在那一拍问一句），不在这里自己 commit。

    `reclaim_principal` 非空 ⇒ **这一发是人按下的「继续执行」**：开跑之前先把停在 `RUNNING` 却没人推的卡
    收回 `READY`（见 `reclaim_orphaned_cards`）。空值 ⇒ 一格不动，CLI 与「批准」两条路径逐字不变。
    """
    if reclaim_principal:
        reclaimed = reclaim_orphaned_cards(
            session,
            feature_request_id=feature_request_id,
            principal=reclaim_principal,
            now=now,
        )
        if reclaimed:
            # 说清是哪几张、为什么 —— 否则活动流里只会看到几张卡莫名从 RUNNING 变回 READY。
            _row("RECLAIM", emit_to=emit_to, cards=",".join(reclaimed), principal=reclaim_principal)
    # `auto` 模式：先把历史遗留在 `REVIEWING` 的卡补签掉（`sign_waiting_reviews` 的 docstring
    # 说明为什么 `deepseek` 那条路不这么做、而这一档必须做）。放在派发之前：否则这一轮跑完，
    # 人看到的还是"评审 N + 新交付的几张"混在一起。
    #: 本轮已经自动重跑过的卡号（每张最多一次，见下面那一支）。
    retried: set[str] = set()
    # 开跑前先把上一单遗留的产物挪走（`quarantine_foreign_artifacts` 的 docstring 说明为什么）。
    # 这一步与模式无关：`manual` 也会被同一份残留挡住 —— 那个缺陷不是 `auto` 带来的。
    quarantine_foreign_artifacts(
        session,
        feature_request_id=feature_request_id,
        project_key=project_key,
        bus_root=bus_root,
        now=now,
        emit_to=emit_to,
    )
    if review_mode == review_policy.REVIEW_MODE_AUTO:
        sign_waiting_reviews(
            session,
            feature_request_id=feature_request_id,
            bus_root=bus_root,
            now=now,
            emit_to=emit_to,
        )
        # 同一档的另外半边：历史遗留在 `ARTIFACT_INVALID` 的卡也放回可派发态，让这一圈重跑一次。
        # 两件事放在同一处，是因为它们回答的是同一个问题："切到 auto 之后，还有哪些卡在等人？"
        retry_invalid_cards(
            session,
            feature_request_id=feature_request_id,
            now=now,
            emit_to=emit_to,
            retried=retried,
        )
    slots = run._slots_from_registry(session)
    leases = scheduler.LeaseLedger()
    outcomes: list[CardOutcome] = []
    rounds = 0
    while True:
        if rounds >= run.MAX_CHAIN_ROUNDS:
            raise RealRunRefused(
                f"真跑走到 {run.MAX_CHAIN_ROUNDS} 轮还没到「没有可派发的卡」⇒ 这是原地打转，不是慢；"
                f"此前跑过的卡号是 {[item.card_id for item in outcomes]}",
                code=run.CHAIN_ROUND_LIMIT,
            )
        try:
            assigned = run.run_assign(
                feature_request_id,
                transport=FROZEN_SEGMENT_GATE,
                session=session,
                now=now,
                slots=slots,
                leases=leases,
                max_cards_per_round=1,
                review_mode=review_mode,
            )
        except run.RunFailure as exc:
            if exc.code != run.NOTHING_DISPATCHABLE:
                raise
            break
        rounds += 1
        # 派发之后、厂商之前就把需求单状态对齐（`SPLIT → RUNNING`），并且**排在提交点之前**：
        # 一次提交同时落地"卡是 RUNNING"与"单是 RUNNING"，界面不必等几分钟后卡跑完才改口。
        # 这一句也是"卡在跑、单还写着已立项"那个读数的唯一修法（现场 = `real_demo / 7800b3dd`）。
        sync_request_status(session, feature_request_id, now=now)
        if commit_after_dispatch is not None:
            commit_after_dispatch()
        for card_id, executor, reviewer, acceptor in assigned.dispatched:
            _row(
                "ASSIGN",
                emit_to=emit_to,
                round=rounds,
                card=card_id,
                executor=executor,
                reviewer=reviewer,
                acceptor=acceptor,
            )
            outcomes.append(
                run_card_segment(
                    session,
                    feature_request_id=feature_request_id,
                    task_id=card_id,
                    project_key=project_key,
                    bus_root=bus_root,
                    now=now,
                    slots=slots,
                    leases=leases,
                    executor=executor,
                    emit_to=emit_to,
                    transport_factory=transport_factory,
                    review_mode=review_mode,
                )
            )
            # `auto` 模式：门禁判产物不达标 ⇒ **原地重跑一次**（`_retry_invalid` 的 docstring 说明为什么）。
            # 每张卡每次 `run_real` 只重跑一次：不设上限时，一支一直交不出产物的执行器会让这一圈
            # 转到 `MAX_CHAIN_ROUNDS` 才停，而那时谁也说不清它是"慢"还是"坏"。
            if (
                review_mode == review_policy.REVIEW_MODE_AUTO
                and outcomes[-1].error is None
                and outcomes[-1].validated_status == validate_artifact.INVALID
                and card_id not in retried
            ):
                retried.add(card_id)
                _retry_invalid(
                    session,
                    feature_request_id=feature_request_id,
                    task_id=card_id,
                    now=now,
                    emit_to=emit_to,
                )
            # 每张卡跑完就把需求单状态对齐一次：派发那一刻它就该是 `RUNNING`，
            # 最后一张到终态那一刻它就该是 `DELIVERED`。放在循环里而不是循环后，
            # 是因为"跑完 4 张、第 5 张卡住"这一段最需要界面说真话（现场见 T05 那一单）。
            sync_request_status(session, feature_request_id, now=now)
    # 一张卡都没派发（例如全部已交付、或全部卡在不可派发态）时也要对齐一次：
    # 否则"最后一次续跑"这条路径上状态会永远停在旧值。
    sync_request_status(session, feature_request_id, now=now)
    return outcomes


def _count(outcomes: Sequence[CardOutcome]) -> dict[str, int]:
    """按那四个收尾名数一遍。数的是**本入口交出去的读数**，不另立一套判定。"""
    counts = {DONE_VALID: 0, DONE_INVALID: 0, DONE_ERROR: 0, DONE_UNKNOWN: 0}
    for item in outcomes:
        if item.error is not None:
            counts[DONE_ERROR] += 1
        elif item.validated_status == validate_artifact.VALID:
            counts[DONE_VALID] += 1
        elif item.validated_status == validate_artifact.INVALID:
            counts[DONE_INVALID] += 1
        else:
            counts[DONE_UNKNOWN] += 1
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="真厂商调用入口：受理段跑到 A 类卡点即停（人工批准），"
        "恢复段把卡跑到产物门禁即停（人工评审）",
    )
    parser.add_argument("--project", required=True, help="项目键（总线根下的目录名）")
    parser.add_argument("--request", help="需求原话，逐字入库（EN-01 的 raw_input）")
    parser.add_argument(
        "--resume",
        metavar="FEATURE_REQUEST_ID",
        help="把这张需求单接下去跑：首次 = 恢复已裁决的 A 类单据；再次 = 继续推进剩余 READY 卡"
        "（评审通过后卡才会解锁下游依赖）",
    )
    parser.add_argument(
        "--bus-root",
        default=DEFAULT_BUS_ROOT,
        help=f"总线的**外层**根目录（默认 {DEFAULT_BUS_ROOT}），不是存档根本身："
        f"存档根 = <bus_root>/<project_key>（权威出处 config/params-ref.md §10）"
        f"⇒ 不要把项目名在这里再写一遍；缺失的存档根自动 mkdir + git init",
    )
    return parser


def _mode(args) -> tuple[str, object, str]:
    """argv 收成 `(形态, 值, 用法提示)`；形态为空串 = 命令行本身不成立。

    三条口径抄 `run._mode()`（那是同族命令现有的形状，不是本文件的新发明）：空白与"没给"同一格
    （`--request "   "` 在 `if not args.request` 里是真值）；`--resume` 的值必须是 UUID
    （一个 thread 由 `(卡点类, 需求单号)` 推导，敲错一个字符就会恢复成"另一条不存在的现场"）。
    """
    text = (args.request or "").strip()
    raw = (args.resume or "").strip()
    if bool(text) == bool(raw):
        return "", None, USAGE_ONE_OF
    if text:
        return "request", text, ""
    try:
        return "resume", uuid.UUID(raw), ""
    except ValueError:
        return "", None, f"`--resume` 要一个需求单号（UUID），收到 {raw!r}"


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], dt.datetime] | None = None,
    emit_to: Callable[[str], object] = print,
    session_factory: Callable[[], object] | None = None,
    transport_factory: Callable[[str, BusGateway, Path], object] | None = None,
    git: Callable[[Sequence[str], Path], int] | None = None,
) -> int:
    """入口。退出码三档全借 `run.py` 那几个常量，本文件不自造数（`EXIT_*` 的口径在那边已经裁过）。

    两格顺序要点名：

    - **先 `ensure_archive_root` 再开会话**。决定 2 那句"不存在就自动建"必须发生在受理图之前 ——
      存档根在拆单节点里就被 `require_archive_root()` 读，晚一步那一发抛的是 `archive_root_missing`，
      而它读起来像"用户没准备好目录"，不是"入口没建目录"。
    - **形态先于路径**：用法错误（`arguments_missing`）在 `mkdir` 之前就说完，所以敲错命令行不会在盘上
      留下一个空仓库。
    """
    install_logging_filter()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or run.EXIT_USAGE)
    kind, value, hint = _mode(args)
    if not kind:
        _row("FAIL", emit_to=emit_to, code=run.USAGE_MISSING, hint=hint)
        return run.EXIT_USAGE

    now = (clock or (lambda: dt.datetime.now(dt.timezone.utc)))()
    # `resolve()` 不是美化：默认值 `results` 是**相对本进程 cwd** 的一串字面量，而厂商子进程报回来的产物路径
    # 是绝对的（它的家由 `_confine` 交出去，那一支已经 resolve 过）。总线根留着相对拼法，`_bus_path` 的
    # "绝对且确在本总线根下"那次换算就永远不成立 ⇒ 一次成功的执行会被 FR-013 整批拒收（2026-10-20 第五发）。
    bus_root = Path(args.bus_root).resolve()
    try:
        ensure_archive_root(args.project, bus_root, emit_to=emit_to, git=git)
        factory = (session_factory or session_module.make_session_factory)()
        with session_module.new_session(factory) as session:
            if kind == "request":
                report = run.run_intake(
                    str(value),
                    project_key=args.project,
                    transport=FROZEN_SEGMENT_GATE,
                    bus_root=bus_root,
                    session=session,
                    now=now,
                )
                _row(
                    "INTAKE",
                    emit_to=emit_to,
                    request=str(report.feature_request_id),
                    project=args.project,
                    cards=",".join(report.card_ids),
                    ticket=str(report.ticket_id),
                    request_status=report.request_status,
                )
                _row(
                    "AWAIT",
                    emit_to=emit_to,
                    stage="A_SPLIT",
                    request=str(report.feature_request_id),
                    **{"next": "人工批准这则 A 类单据（看板或 CLI 同一端点）", "approve": APPROVE_HINT},
                )
                _row(
                    "NEXT",
                    emit_to=emit_to,
                    command=f"{PROG} --project {args.project} --resume {report.feature_request_id}"
                    f" --bus-root {bus_root}",
                )
                return run.EXIT_OK

            try:
                resumed = run.run_resume(
                    value,  # type: ignore[arg-type]
                    transport=FROZEN_SEGMENT_GATE,
                    bus_root=bus_root,
                    session=session,
                )
            except run.RunFailure as exc:
                if exc.code != run.NO_WAITING_POINT:
                    raise
                # 第二次之后 `--resume` 仍要可用：评审通过让下游卡变 READY，但受理图的等待点已经消费掉了。
                # 这不是"恢复一张旧现场"，是"接着把剩余可派发的卡跑完" ⇒ 跳过恢复段，直接进执行段。
                request_id = value  # type: ignore[assignment]
                _row(
                    "CONTINUE",
                    emit_to=emit_to,
                    request=str(request_id),
                    reason=exc.code,
                )
            else:
                request_id = resumed.feature_request_id
                _row(
                    "RESUME",
                    emit_to=emit_to,
                    request=str(resumed.feature_request_id),
                    ticket=str(resumed.ticket_id),
                    conclusion=resumed.conclusion,
                    card_statuses=",".join(f"{card}:{status}" for card, status in resumed.card_statuses),
                )
            outcomes = run_real(
                session,
                feature_request_id=request_id,
                project_key=args.project,
                bus_root=bus_root,
                now=now,
                emit_to=emit_to,
                transport_factory=transport_factory_for(
                    session, request_id, transport_factory or real_transport
                ),
            )
            counts = _count(outcomes)
            _row(
                "SUMMARY",
                emit_to=emit_to,
                request=str(request_id),
                cards=len(outcomes),
                stop="human_review",
                **counts,
            )
            return run.EXIT_RUNTIME if counts[DONE_ERROR] or counts[DONE_UNKNOWN] else run.EXIT_OK
    except RealRunRefused as exc:
        _row("FAIL", emit_to=emit_to, code=exc.code)
        emit_to(f"{PREFIX} DETAIL :: {exc}")
        return run.EXIT_USAGE
    except run.RunFailure as exc:
        # 冻结链路自己的拒绝（单据还没批、没有可派发的卡、注册面空……）在这里换成 CLI 侧的归因。
        # 退出码取 `EXIT_USAGE` 那一档的那条理由在 `run.py:1928-1933`（owner 2026-10-16 裁决 1）：
        # **拒绝发生在这一发碰任何厂商之前**，与"旗标用法错"同档，不是"跑起来之后失败了"。
        _row("FAIL", emit_to=emit_to, code=exc.code)
        emit_to(f"{PREFIX} DETAIL :: {exc}")
        return run.EXIT_USAGE
    except CredentialMissing as exc:
        _row("FAIL", emit_to=emit_to, code="credential_missing", name=exc.name)
        emit_to(
            f"{PREFIX} DETAIL :: 变量 {exc.name} 未注入 ⇒ 凭据只从环境来（宪法「二」），"
            "本入口不读文件里的默认值，也不降级成仿真"
        )
        return run.EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - 入口形状由 main() 的判据覆盖
    raise SystemExit(main(sys.argv[1:]))
