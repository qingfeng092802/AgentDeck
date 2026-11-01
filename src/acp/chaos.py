"""T111（FR-064, FR-065｜R-14）：两个崩溃回退场景的**注入入口**，不是文档里的两段 bash。

quickstart §6 那四条命令的成立条件是"前两条能把现场弄坏、后两条能把现场捞回来"。T022 与 T110 交了
后两条，本模块交前两条。**入口存在的理由是判据要能跑**：`contracts/file-bus.md §7` 那行
"停 PG 后按 §4 重建三元组"、`tasks.md` T112 那行"两场景恢复后均无重复任务卡"，在没有注入入口的
时候只能靠人手工破坏一次现场，而手工现场写不进用例、也就无法复跑。

两个入口的破坏面是**分别定过的**，不是"都叫 chaos 所以一样危险"：

- `--kill-graph-state` 删的是 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` 里
  **一个 `thread_id`** 的行。范围由 `thread_id_for()` 推导，推导需要 `(卡点类, 需求单号)` 两个输入
  ⇒ 它不可能"顺手删掉别人的现场"。这一份不碰 EN-01 ~ EN-16 的任何业务表：演练要证的是
  "state 坏了能从存档点重建"，把业务事实一起删掉就变成另一个场景了。
- `--stop-postgres` 停的是本机 compose 里的 `db` 服务，影响面 = 这台机上**所有**连它的进程，
  不只是本平台 ⇒ 这是本模块唯一一条会影响共享环境的动作。

**两个入口都默认不执行**：不带 `--yes` 时一个只报"现场有多少行、会删哪些表"，一个只报"会拼出哪条
argv"。理由不是客气 —— 演练脚本被误当成执行脚本跑，是这个工具自己最可能造成事故的地方，而它偏偏
长得像"平台提供的运维命令"。**恢复 PG 不在本模块**（`docker compose start db` 归操作者）：给它配一个
对称的 `--start-postgres` 会让人以为"chaos 能收尾"，而它其实只管弄坏 —— 少一个入口比多一个假出口好。

**两个 dry-run 的退出码不一样，这不是笔误**：`--kill-graph-state` 那一发**交出了读数**（三张表各几行），
所以它是 0；`--stop-postgres` 那一发什么都没读也没做，只把 argv 交给人看一眼，所以它是 2
（"这一发没执行" MUST NOT 被读成一次成功的演练）。两者的 stdout 都以 `CHAOS DRY ::` 起头，
`CHAOS OK ::` 只在现场真被动过之后出现。

**凭据面**（宪法「二」）：DSN 只从 `ACP_DB_DSN` 取；失败与拒绝的消息里只有变量名、表名、行数、
退出码与异常类名。psycopg 的原始消息可能带连接串参数，一律 `from None` 掐断（口径同
`persistence/checkpointer.open_connection`）。compose 的 argv 里不含任何值：口令在 `.env` 与
compose 文件里，不在命令行上。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psycopg

from acp.config.loader import CredentialMissing
from acp.persistence import checkpointer

#: 带 `thread_id` 列的那三张表 = 一次注入的全部射程。**顺序是定过的**：`checkpoint_writes` 与
#: `checkpoint_blobs` 先走，`checkpoints` 最后 —— 前两张按 `(thread_id, checkpoint_id)` 挂在第一张上，
#: 反过来先删主表会把它们变成没有父行的孤儿（库上没有真外键，所以这一步靠顺序而不是靠数据库报错）。
#: 这份名单的**符合性**由用例钉：它 MUST 等于从 `checkpointer.library_declared_schema()` 里
#: 解析出的"含 `thread_id` 列的表集"（库换了表结构，这张名单会红，而不是默默少删一张）。
THREAD_TABLES: tuple[str, ...] = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

#: 表名会被拼进 SQL，所以它必须是标识符形态。**这条放在 import 期而不是每次调用**：名单是常量，
#: 一个常量形状不对却在每次注入时才知道，等于让"删错表"这种事故晚一步发生。
if any(not name.replace("_", "").isalnum() or not name.islower() for name in THREAD_TABLES):
    raise RuntimeError("THREAD_TABLES 里有不是小写标识符的表名 ⇒ 它不该被拼进 SQL")

#: 停库用的 argv：`docker compose -f <file> stop <service>`。**值全部来自仓内文件形态**，
#: 命令行上没有口令、没有主机、也没有端口（这三样都在 `.env` 与 compose 文件里）。
COMPOSE_FILE = "docker-compose.yml"
COMPOSE_SERVICE = "db"

#: A 类卡点是 MVP-1 这条链上唯一的 thread 种类，`thread_id` 里那一段就是它的**域值**不是字母
#: （实测：`approval_gate.KIND == "A_SPLIT"`，而 `advance_request` 用它当 `kind` 推 thread 号 ——
#: 第一版这里写成 `"A"`，于是"演练删的 thread"与"恢复读的 thread"是两个名字，注入看着成功而现场还在）。
#: 但**不在模块级 import `orchestrate`**：chaos 是演练工具，它不该因为编排栈导入失败就连现场都造不出来。
#: 这个字面量由用例与 `approval_gate.KIND` 双向绑一次（`test_the_default_kind_is_the_gate_kind…`）。
DEFAULT_KIND = "A_SPLIT"

#: 注入动作的执行器注入位：`argv -> 退出码`。用例把它换成记账桩，于是"argv 拼得对不对"这件事
#: 不需要真有一个 docker，也不需要真停掉谁的库。
ArgvRunner = Callable[[Sequence[str]], int]


class ChaosMisuse(ValueError):
    """注入不成立（缺确认旗标、需求单号形状不对、名单外的表）。全部发生在动手之前。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


class ChaosFailure(RuntimeError):
    """注入动作真跑了而外部程序不退零。消息里只有子命令名与退出码，没有 stderr 原文。"""

    def __init__(self, action: str, code: int) -> None:
        self.action = action
        self.code = code
        super().__init__(f"演练动作失败：动作={action!r} 退出码={code}")


@dataclass(frozen=True)
class ThreadRows:
    """一个 thread 在 checkpoint 三张表上各有多少行。**这是 dry-run 交出的全部东西**。"""

    thread_id: str
    counts: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)

    def to_line(self) -> str:
        return f"thread={self.thread_id} " + " ".join(f"{name}={count}" for name, count in self.counts)


def thread_id(*, kind: str, feature_request_id: uuid.UUID) -> str:
    """把 CLI 收到的 `(旗标 → thread 号)` 这一步单独留出：`--kill-graph-state` 需要操作者**先看见**
    自己要动的是哪个 thread，再决定加不加 `--yes`。推导式仍是 `checkpointer.thread_id_for()`，
    这里不写第二份 f-string（那会变成"恢复算出的 thread"与"注入算出的 thread"两个口径）。"""
    return checkpointer.thread_id_for(kind=kind, feature_request_id=feature_request_id)


def read_thread_rows(connection: psycopg.Connection, tid: str) -> ThreadRows:
    """数一个 thread 的现场行数（只读，`--kill-graph-state` 的 dry-run 走这一条）。"""
    counts: list[tuple[str, int]] = []
    for table in THREAD_TABLES:
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT count(*) FROM "{table}" WHERE thread_id = %s', (tid,))
            counts.append((table, int(cursor.fetchone()[0])))
    return ThreadRows(thread_id=tid, counts=tuple(counts))


def kill_graph_state(connection: psycopg.Connection, tid: str) -> ThreadRows:
    """删掉那个 thread 的 checkpoint 现场，回删掉的行数。**这就是场景 1 本身**。

    表名进 SQL 这件事只有在这里是安全的：取值来自本模块的封闭常量 `THREAD_TABLES`，而它在 import 期
    就被形态断言钉过（下面那一段）；操作者能给的只有 `thread_id`（一个**值**，走参数位），给不了表名。
    """
    if not tid:
        raise ChaosMisuse(
            "thread 号为空 ⇒ 空串会被拼成 `WHERE thread_id = ''`：删不掉任何行，却报出一行 "
            "`deleted=0` 看起来像注入成功 ⇒ 演练的读数和现场无关了",
            code="thread_required",
        )
    with connection.cursor() as cursor:
        deleted: list[tuple[str, int]] = []
        for table in THREAD_TABLES:
            cursor.execute(f'DELETE FROM "{table}" WHERE thread_id = %s', (tid,))
            deleted.append((table, int(cursor.rowcount)))
    if not connection.autocommit:
        # `open_connection()` 给的是 autocommit 连接（PostgresSaver 要求），那时这句 commit 是多余的；
        # 用例传进来的可能是事务连接 ⇒ 按连接自己的模式收尾，而不是无条件 commit（那会在 autocommit
        # 连接上抛 "no transaction in progress"，把一个成功的注入读成失败）。
        connection.commit()
    return ThreadRows(thread_id=tid, counts=tuple(deleted))


def compose_stop_argv(*, service: str = COMPOSE_SERVICE, compose_file: str = COMPOSE_FILE) -> tuple[str, ...]:
    """拼出"停那个服务"的 argv。**拼出来不等于执行**：执行要 `--yes`，且由 `run_compose` 那条路走。"""
    return ("docker", "compose", "-f", compose_file, "stop", service)


def run_compose(argv: Sequence[str]) -> int:
    """默认执行器：`subprocess` 直调，**不经 shell**（argv 是元组 ⇒ 路径里的空格不会被当分隔符）。

    这两个关键字今天是惰性的（不捕获流 ⇒ 父进程不解码，实测钉与不钉退码都是 0），但 `encoding=` 一给
    `Popen.text_mode` 就开着 ⇒ 谁以后在这里加 `capture_output=True`，中文 compose 输出就不会把 `stdout`
    静默变成 `None`。逐条读数 = `evidence/phase-15.md` §十六。
    """
    return subprocess.run(list(argv), check=False, encoding="utf-8", errors="replace").returncode


def stop_postgres(
    *, confirmed: bool, runner: ArgvRunner | None = None
) -> tuple[tuple[str, ...], int | None]:
    """场景 2：停掉 compose 里的 `db`。回 `(argv, 退出码)`；**未确认时抛出且一条命令都没发**。

    `confirmed=False` 抛 `confirmation_required` 而不是返回一个"没执行"的读数：这条路径的语义是
    "操作者还没决定要停"，把它读成一次成功的演练，就等于让 `CHAOS OK` 出现在一个什么都没发生的现场上。

    `runner` 的默认值**故意写成 `None` 而不是 `run_compose`**：参数默认值在 `def` 求值时就绑死了，
    写函数名会让用例无法把执行器换成记账桩（`monkeypatch.setattr(chaos, "run_compose", …)` 打不动它）。
    而真停库这一发影响的是这台机上所有连它的进程 ⇒ 用例 MUST 能拦住那一次真调用，见
    `test_the_cli_goes_through_the_injected_runner_only_when_confirmed`。
    """
    argv = compose_stop_argv()
    if not confirmed:
        raise ChaosMisuse(
            f"停库的影响面是这台机上连它的**所有**进程，不止本平台 ⇒ 加 --yes 明写确认"
            f"（本行只交出会拼出的 argv：{list(argv)}）",
            code="confirmation_required",
        )
    return argv, (runner if runner is not None else run_compose)(argv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="两个崩溃回退场景的注入入口（FR-064 / FR-065｜R-14）；默认 dry-run"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--kill-graph-state",
        action="store_true",
        help="场景 1：删掉某个需求的编排现场（checkpoints 三表上该 thread 的行）",
    )
    group.add_argument(
        "--stop-postgres", action="store_true", help="场景 2：停掉 compose 里的 db 服务（影响全机）"
    )
    parser.add_argument("--request", metavar="FEATURE_REQUEST_ID", default=None)
    parser.add_argument("--kind", default=DEFAULT_KIND)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行。不带它时两个入口都只报读数，不动现场、不发命令",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv[1:])
    if args.stop_postgres:
        try:
            command, code = stop_postgres(confirmed=args.yes)
        except ChaosMisuse as error:
            print(f"CHAOS DRY :: {error}")
            return 2
        if code == 0:
            print(f"CHAOS OK :: action=stop_postgres argv={list(command)} exit=0")
            return 0
        print(f"CHAOS FAIL :: action=stop_postgres argv={list(command)} exit={code}")
        return 1

    if not args.request:
        print(
            "usage error: --kill-graph-state 必须带 --request <feature_request_id>"
            "（thread 号由 `(卡点类, 需求单号)` 推导；不给就不知道该动哪一个现场 ⇒ 不猜、不全删）"
        )
        return 2
    try:
        request_id = uuid.UUID(args.request)
    except (ValueError, AttributeError) as error:
        print(f"usage error: --request 不是 uuid（{type(error).__name__}）")
        return 2

    tid = thread_id(kind=args.kind, feature_request_id=request_id)
    try:
        connection = checkpointer.open_connection()
    except (checkpointer.CheckpointerUnavailable, CredentialMissing) as error:
        print(f"CHAOS FAIL :: {error}")
        return 3
    try:
        if not args.yes:
            rows = read_thread_rows(connection, tid)
            print(f"CHAOS DRY :: {rows.to_line()} total={rows.total} deleted=0（加 --yes 才真删）")
            return 0
        rows = kill_graph_state(connection, tid)
    except psycopg.Error as error:
        # `from None`：psycopg 的原文可能带连接串参数（凭据面口径，见模块头）。
        print(f"CHAOS FAIL :: 现场注入失败（{type(error).__name__}），本行不回显驱动原文")
        return 1
    finally:
        connection.close()
    print(f"CHAOS OK :: action=kill_graph_state {rows.to_line()} deleted={rows.total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
