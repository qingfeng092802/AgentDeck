"""T108 的机制半边：一轮任务的两个存档拍点（FR-053｜R-14）。

宪法「三」的原话是"每轮**开工前 / 完工后** MUST 自动 git commit；存档点可枚举，revert 可复原"
（`.specify/memory/constitution.md:457`），D-12 补了第二条边界："**存档不替代三岗把关**（宪法「三」明文），
回退动作本身落审计"。本模块只管第一句里"怎么留下一个存档点"，DB 侧的登记归
`persistence/snapshots.py`（一个事实一个载体：git 对象在盘上，句柄在库里，两边各说各的活）。

**为什么机制与登记分两个模块**：合在一起的话，"commit 成功而建行失败"这种半程态会在同一个函数里
被吞掉（要么整笔回滚说"没存档"，要么落一行指向不存在的存档的假句柄）。拆开之后调用方看得见两步：
`commit_archive_point()` 交回一个 sha，`record_point()` 只认这个 sha。

**这一层刻意不 import 会话、不写库**：存档机制要能在"PG 断了"的场景下仍然被讨论
（D-12 的第二条回退路径 = 从文件总线重建三元组），把 git 与 SQL 焊在一个模块里，那条路径就只能整体缺席。

**只允许三个 git 子命令**（`ALLOWED_SUBCOMMANDS`）：`add` / `commit` / `rev-parse`。这一份名单是安全边界
而不是便利清单 —— 存档机制一旦能拼任意 argv，它就成为"平台自己执行破坏性 git 动作"的第二条通道
（`config/danger_list.yaml` 拦的是**适配器出站动作**，拦不住这里）。`reset` / `clean` / `checkout` / `push`
在本模块里不是"没列进名单"，而是**点名拒在执行之前**（判据钉"名单外子命令 ⇒ runner 一次都没被调到"）。

**空轮也要留点**：`commit` 带 `--allow-empty`。一轮里可能一个文件都没改（例如整轮都停在人工卡点等人），
不带这个旗标 git 会退 1 ⇒ 那一轮就没有存档点 ⇒ "每轮开工前 / 完工后各一个"这句话就变成
"有改动的那些轮各一个"。宁可留一个内容等于上一轮的点，也不要"数出来少一个"。

**不回显厂商文本**：git 的 stdout / stderr 里可能出现绝对路径、用户名、hook 输出，而本项目的错误口径
一直是"只带变量名、字段名、退出码、原因词"。⇒ `ArchiveFailure` 只带**子命令名 + 退出码**，
`rev-parse` 的输出只取第一行且必须匹配十六进制形状才用（形状不过 = 抛错，不把原文塞进消息）。

**身份从哪来**：平台不传 `-c user.*`、不写任何 git 配置 —— 存档 commit 用的是**被存档那个仓库自己的身份**。
缺身份就退 128 ⇒ 抛 `ArchiveFailure` 让装配方去看仓库配置，而不是由平台编一个"acp-bot"的作者名落进历史
（那是给提交记录造假主体）。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from acp.persistence.enums import values_of

#: 两拍打点的取值**从 `snapshot_phase` 域取**，不在这里重述（口径同 `guard/autonomy.py` 的档位别名）：
#: 域里删掉 `POST_WORK`，本模块 import 期就报错，而不是把"完工后那一拍"静默读成"没有这一拍"。
PHASES = values_of("snapshot_phase")
OPEN_PHASE = "PRE_WORK"
CLOSE_PHASE = "POST_WORK"
_missing = sorted({OPEN_PHASE, CLOSE_PHASE} - set(PHASES))
if _missing:
    raise RuntimeError(f"snapshot_phase 域里没有 {_missing} ⇒ 本模块的拍点名与文档脱钩了")
del _missing

#: 句柄的形态位。**T109 定稿定的是这一个函数，不是散在代码里的字符串拼接**：本模块只允许
#: `snapshot_ref()` 一处拼句柄（判据 = AST 数 `GIT_REF_PREFIX` 的使用点）。接口定稿没有改这个形态 ——
#: 它改的是**另一半**："这个对象住在哪一个仓库里"由 `archive/root.py` 解析（T-15 那句"换成真实句柄"
#: 只换前一半会变成"一个形状对但指不到东西的字符串"，那与 `local:` 自指是同一种假）。
GIT_REF_PREFIX = "git:"
#: T-15 裁 ① 那个"指向登记行自身"的临时句柄的前缀。新的写入不许用它（拦在仓储那一侧，判据钉）。
TEMPORARY_REF_PREFIX = "local:"
#: 对象名的形状：**40 位（SHA-1）或 64 位（SHA-256），全小写十六进制**。
#: 不写成 `{40,64}` 的区间：那个写法会把 41 到 63 位也认成合法对象名，而 git 从不产生那种长度 ⇒
#: 形状判据松一档，`rev-parse` 读出个半截串就会被拼进 `ref` 落进库里（第六节钉的就是这一档）。
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

#: 允许交给 git 的子命令。**只有这三个**，理由写在模块头。
ALLOWED_SUBCOMMANDS = ("add", "commit", "rev-parse")

#: 本模块自己会用的 argv（`add` 用 `-A` 覆盖整棵存档根，`commit` 带 `--allow-empty`）。
ADD_ARGV: tuple[str, ...] = ("add", "-A")
COMMIT_FLAGS: tuple[str, ...] = ("commit", "--allow-empty")
READ_BACK_ARGV: tuple[str, ...] = ("rev-parse", "HEAD")
#: T110 的"这个对象真在盘上吗"用的 argv。**用的还是名单里已有的 `rev-parse` ⇒ 恢复路径不扩大 git 面**
#: （这条检查必须由存档根这个仓库自己回答，不能让 `restore.py` 去 `os.listdir` 猜 `.git` 在哪）。
#: `--verify --quiet` 的组合是刻意选的：对象不存在时 git 退 1 且**不写 stderr**，
#: 于是"不存在"是一个退出码读数而不是一段会被带进消息的原文（模块头的凭据口径）。
#: `^{commit}` 而不是裸对象名：存进去的是 commit，若同名对象其实是 blob，那也不能当作存档点。
VERIFY_COMMIT_ARGV: tuple[str, ...] = ("rev-parse", "--verify", "--quiet")

#: T109 的边界自检用的 argv。**它用的是名单里已有的 `rev-parse` ⇒ 这一道检查不扩大 git 面**。
#: 调用它的是 `archive/root.require_archive_root()`（存档根的唯一解析点），不是本模块 ——
#: 本模块按上面的约定"根由调用方交出、不猜默认值"，而"猜来的根其实是别人仓库的一个子目录"这个
#: 真实风险恰好发生在有默认值的那一层。
TOPLEVEL_ARGV: tuple[str, ...] = ("rev-parse", "--show-toplevel")


class ArchiveMisuse(ValueError):
    """调用方交来的形状不成立（拍点名不在域里、轮次 < 1、任务集合空或含空白项、名单外的子命令）。

    这类错误**发生在执行之前** ⇒ 一条 git 命令都没跑，现场没被动过，可以安全重试。
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


class ArchiveFailure(RuntimeError):
    """git 退出码非零。消息里只有子命令名与退出码 —— 没有 stderr 原文（模块头那条口径）。"""

    def __init__(self, subcommand: str, code: int) -> None:
        self.subcommand = subcommand
        self.code = code
        super().__init__(f"存档的 git 动作失败：子命令={subcommand!r} 退出码={code}")


@dataclass(frozen=True)
class GitResult:
    """一次 git 调用的读数。`stdout` 只在成功时被读；失败时本模块不碰它，也不落进任何消息。"""

    code: int
    stdout: str = ""


GitRunner = Callable[[tuple[str, ...], Path], GitResult]


def run_git(argv: Sequence[str], cwd: Path) -> GitResult:
    """默认执行器：`subprocess` 直调 git，**不经 shell**（argv 是列表 ⇒ 路径里的空格不会被当成分隔符）。

    单独抽出来是为了判据能用桩替身：所有"argv 形状对不对"的断言都在拼好的 argv 上做，
    不需要真有一个 git 仓库；而"现场真被 commit 了"那一发用临时仓库走这条真实现。
    """
    completed = subprocess.run(
        ["git", *(str(item) for item in argv)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return GitResult(code=completed.returncode, stdout=completed.stdout or "")


@dataclass(frozen=True)
class ArchivePoint:
    """一个已落到盘上的存档点。本模块只产出它，落库由 `persistence/snapshots.py` 负责。"""

    sha: str
    ref: str
    phase: str
    round_no: int
    message: str


def snapshot_ref(sha: str) -> str:
    """把 commit 对象名换成 EN-12 的 `ref`。**句柄形态唯一的拼装点**（T109 定稿就改这一个函数）。"""
    if not _SHA.match(sha):
        raise ArchiveMisuse(
            "commit 回读到的对象名不是十六进制形状 ⇒ 不把它拼成句柄"
            "（宁可不落，也不落一个指不到东西的 ref）",
            code="sha_shape",
        )
    return f"{GIT_REF_PREFIX}{sha}"


def is_real_handle(ref: str) -> bool:
    """这个句柄是不是"指向一个真存档"的形态。临时句柄（`local:` 起头）与空串都不算。"""
    return ref.startswith(GIT_REF_PREFIX) and bool(_SHA.match(ref[len(GIT_REF_PREFIX) :]))


def object_exists(root: Path | str, name: str, *, runner: GitRunner = run_git) -> bool:
    """存档根里那个 commit 对象查不查得出来 —— T109 那句"真实句柄"在这里被**盘上**核对一次。

    `is_real_handle()` 只证形状（一个 40 位十六进制串可以是任何人手敲的），这一函数证存在。
    两者都要过才有 FR-064 那个"从最近一次 git 存档点重建"的起点：形状对而对象不在，
    拿它恢复就是在一个不存在的现场上继续调度。

    **退非零回 `False` 而不是抛**：这里问的是"有没有"，不是"git 坏没坏"。把"没有"表达成异常，
    调用方就得区分两种非零退出码，而 `ArchiveFailure` 恰好不带 stderr 原文 ⇒ 区分不了。
    消息/异常面因此不出现，只剩一个布尔位给 `restore.py` 决定它的第四个拒绝码。
    """
    if not _SHA.match(name):
        raise ArchiveMisuse(
            "要核对的对象名不是十六进制形状 ⇒ 不去问 git（把一个明知非法的形状递进 argv，"
            "读出来的非零码会被误当成'存档丢了'）",
            code="sha_shape",
        )
    argv = (*VERIFY_COMMIT_ARGV, f"{name}^{{commit}}")
    assert_allowed(argv)
    return runner(argv, Path(root)).code == 0


def commit_message(
    *, phase: str, round_no: int, project_key: str, task_card_ids: Sequence[str]
) -> str:
    """存档 commit 的正文：**把"哪一轮、哪一拍、涉及哪些卡、哪个项目"写进对象本身**。

    为什么不是只写一句 `snapshot`：`revert 可复原`（宪法「三」）要求人只读 `git log` 就能认出
    "这个点是第 2 轮的开工前现场"；而这一行同时是"库里那一行说的是同一件事"的独立见证 ——
    两边各写一份比两边都不写更容易漂，因为漂了能 diff 出来（判据第五节钉这一对）。
    """
    return (
        f"archive: {phase} round={round_no} project={project_key} "
        f"cards={','.join(task_card_ids)}"
    )


def _validate_shape(
    phase: str, round_no: int, task_card_ids: Sequence[str], project_key: str
) -> None:
    if phase not in PHASES:
        raise ArchiveMisuse(
            f"拍点名 {phase!r} 不在 snapshot_phase 域里：{list(PHASES)}",
            code="phase_domain",
        )
    if round_no < 1:
        raise ArchiveMisuse(f"轮次序号从 1 起（收到 {round_no}）", code="round_no")
    if not project_key.strip():
        raise ArchiveMisuse(
            "存档点必须点名项目，否则「存档点可枚举」这句话在第二个项目上就断了（会读出别的项目的点）",
            code="project_key",
        )
    if not task_card_ids:
        raise ArchiveMisuse(
            "本轮任务集合为空 ⇒ 不建存档点。EN-12 的 `task_card_ids` 是这一行「说的是哪一轮」的唯一依据，"
            "空集合那行只能被读成「整个项目在这一刻停了」，而那不是它想说的意思",
            code="cards_required",
        )
    cleaned = [item.strip() for item in task_card_ids]
    if any(not item for item in cleaned):
        raise ArchiveMisuse("本轮任务集合含空白卡号 ⇒ 先修调用方", code="cards_shape")
    if len(set(cleaned)) != len(cleaned):
        raise ArchiveMisuse("本轮任务集合含重复卡号 ⇒ 先修调用方", code="cards_shape")


def commit_archive_point(
    root: Path | str,
    *,
    phase: str,
    round_no: int,
    project_key: str,
    task_card_ids: Sequence[str],
    runner: GitRunner = run_git,
) -> ArchivePoint:
    """在 `root` 这个仓库里留下一个存档点，回 `ArchivePoint`（**不碰数据库**）。

    三步固定：`add -A` → `commit --allow-empty -m <正文>` → `rev-parse HEAD` 回读对象名。
    第三步不是多余：`commit` 的 stdout 是给人看的摘要行（`2 files changed` 那种），版本与语言环境都会变；
    要一个能进 `ref` 的东西只能回读 `HEAD`。

    `root` 由调用方交出且 MUST 是那个要被存档的仓库根；本模块**不猜默认值** —— 给一个"看起来像仓库根"
    的默认（比如当前工作目录）就等于允许有人在没有仓库的目录里调用它，那一步会退 128 并把现场搞成半程态。
    """
    _validate_shape(phase, round_no, task_card_ids, project_key)
    target = Path(root)
    if not target.is_dir():
        raise ArchiveMisuse(f"存档根不存在：{str(target)!r}", code="root_missing")
    cards = [item.strip() for item in task_card_ids]
    message = commit_message(
        phase=phase, round_no=round_no, project_key=project_key, task_card_ids=cards
    )

    steps: tuple[tuple[str, ...], ...] = (
        ADD_ARGV,
        (*COMMIT_FLAGS, "-m", message),
        READ_BACK_ARGV,
    )
    sha = ""
    for argv in steps:
        assert_allowed(argv)
        result = runner(argv, target)
        if result.code != 0:
            raise ArchiveFailure(argv[0], result.code)
        if argv[0] == "rev-parse":
            lines = result.stdout.splitlines()
            sha = lines[0].strip() if lines else ""
    return ArchivePoint(
        sha=sha, ref=snapshot_ref(sha), phase=phase, round_no=round_no, message=message
    )


def assert_allowed(argv: Sequence[str]) -> None:
    """名单外的子命令**在执行之前**拒掉。消息里只有那个子命令名。"""
    head = str(argv[0])
    if head not in ALLOWED_SUBCOMMANDS:
        raise ArchiveMisuse(
            f"存档机制不许调用 git 子命令 {head!r}；允许的名单只有 {list(ALLOWED_SUBCOMMANDS)}"
            "（破坏性 git 动作不在存档射程内，改远端更不是存档该做的事）",
            code="git_subcommand_forbidden",
        )
