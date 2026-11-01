"""从文件总线重建"任务 → 状态 → 产物"三元组（T022，FR-065｜R-24），
以及从最近存档点恢复执行态（T110，FR-064｜R-14）。

宪法「三」的崩溃回退要求这条路**可执行**，不是只写在文档里：PG 不可用时，
`status.md` + `tasks/**` + `results/**` 必须足以推出三元组（契约 §4 / §6，D-9）。
T110 补的是**另一侧**：文件总线没坏、坏的是 LangGraph 的 state ⇒ 从 EN-12 那行存档点把 thread
写回去。两条路合在一个入口（`python -m acp.recover`）是刻意的：宪法「三」列的两条回退路径
在操作者眼里就是同一件事 —— "出问题了，把它捞回来"，分成两个命令只会让人猜该敲哪个。

三条刻意的事：
1. **只读正文三行**，frontmatter 一律剥掉——这正是契约 §3 条件③的用处（PG 没了，
   frontmatter 里的元数据也就没了，回退路径 MUST NOT 依赖它）。
2. **不重跑任何链路**：`--from-file-bus` 那一侧只读文件、只做一致性比对，没有编排、没有模型调用。
   "恢复 = 重跑一遍"是实现者最容易混过去的那条，它会把已完成任务再执行一次。
   `--from-snapshot` 这一侧把这句话做成了**结构性**的而不是承诺性的：它构造
   `OrchestrateDeps(splitter=None)`，而 `_split` 缺拆单器就抛 `SplitNotConfigured`
   ⇒ 这条链只要真的从头跑起来就会响亮失败，"没有整链路重跑"不需要靠读注释来信。
   另一半天然是形状给的：恢复走 `update_state(as_node=normalize)`，它**写一个 checkpoint**
   而不执行任何节点（2026-10-16 在真 checkpointer 上实测：被删空的 thread 用这一句复活后
   `values` 回来了、`next` 指向后继节点，而节点体一次都没进）。
3. 契约 §6 的第一条关系式（`results/**` 条目数 == `artifact` 记录数）要拿 PG 侧的 EN-06 计数
   才能算 ⇒ 总线侧只校验它能自证的那两条，缺口在输出里明写成 `pg_side_checked=False`，
   MUST NOT 让它以"已通过"的面貌出现。**这一侧不会因为 T110 而变**：两个模式各报各的读数，
   `--from-snapshot` 不替总线侧作证，反之也一样。

**为什么恢复要认存档点而不认"最新的那个 checkpoint"**：thread 里那份 state 恰恰是可能已损坏的东西，
拿损坏现场恢复损坏现场就是 FR-064 要禁的循环。EN-12 那行 + 盘上那个 commit 是**损坏面之外**的见证，
所以 `--from-snapshot last` 里的 "last" 排的是 `snapshots.list_points()` 的序（轮次↑拍名↑id↑），
不是 checkpoints 表的时间序。

**凭据面**：本命令的两条路径都不打印 DSN、不打印 git 的 stderr 原文；失败消息只带变量名
（`ACP_DB_DSN` / `ACP_BUS_ROOT`）、字段名、退出码与原因词（宪法「二」，口径同 `acp.approve`）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.archive import cadence, restore, root
from acp.bus import layout
from acp.bus.gateway import CARD_LABELS, BusError, split_card
from acp.bus.orchestrator_writer import STATUS_HEADER, StatusRow
from acp.config.loader import CredentialMissing
from acp.persistence.models_core import Artifact, FeatureRequest

TASK_FILE_RE = re.compile(r"^(T\d{2,})-[a-z0-9][a-z0-9_-]*\.md$")
RESULT_ROW_RE = re.compile(r"^\|\s*(\S+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\S+)\s*\|$")


class ConsistencyError(Exception):
    """契约 §6 的关系式不成立。恢复流程在这里停，而不是拼一个看起来完整的假三元组。"""


@dataclass(frozen=True)
class Triple:
    task_id: str
    status: str
    objective: str
    inputs: str
    expected_output: str
    artifacts: tuple[tuple[str, int, int, str], ...]


@dataclass(frozen=True)
class Reconstruction:
    triples: tuple[Triple, ...]
    card_count: int
    status_row_count: int
    result_index_count: int

    @property
    def pg_side_checked(self) -> bool:
        return False


def read_cards(root: Path) -> dict[str, Triple]:
    cards: dict[str, Triple] = {}
    directory = root / layout.TASKS_DIR
    if not directory.exists():
        return cards
    for path in sorted(directory.glob("*.md")):
        hit = TASK_FILE_RE.match(path.name)
        if hit is None:
            raise ConsistencyError(
                f"任务卡文件名不符 `T<NN>-<slug>.md`：{path.name!r}（契约 §3）"
            )
        task_id = hit.group(1)
        meta, body = split_card(path.read_text(encoding="utf-8"))
        lines = [line for line in body.split("\n") if line.strip()]
        parsed = _parse_body(lines, path.name)
        cards[task_id] = Triple(
            task_id=task_id,
            status="",
            objective=parsed["目标"],
            inputs=parsed["输入"],
            expected_output=parsed["期望产物"],
            artifacts=(),
        )
    return cards


def read_status(root: Path) -> dict[str, StatusRow]:
    path = root / layout.STATUS_MD
    if not path.exists():
        return {}
    rows: dict[str, StatusRow] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(STATUS_HEADER) or cells[0] == "task_id" or set(cells[0]) <= {"-"}:
            continue
        rows[cells[0]] = StatusRow.parse(line)
    return rows


def read_result_index(root: Path, task_id: str) -> tuple[tuple[str, int, int, str], ...]:
    path = root / layout.RESULTS_DIR / f"{task_id}.md"
    if not path.exists():
        return ()
    entries: list[tuple[str, int, int, str]] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        hit = RESULT_ROW_RE.match(line.strip())
        if hit is None or hit.group(1) == "rel_path":
            continue
        entries.append((hit.group(1), int(hit.group(2)), int(hit.group(3)), hit.group(4)))
    return tuple(entries)


def rebuild(bus_root: Path, project_key: str) -> Reconstruction:
    root = Path(bus_root) / project_key
    if not root.exists():
        raise ConsistencyError(f"总线项目目录不存在：{root}")

    cards = read_cards(root)
    status_rows = read_status(root)

    # 契约 §6 第二式：每张 tasks/T** 在 status.md 必有对应行，反之亦然。
    only_cards = sorted(set(cards) - set(status_rows))
    only_status = sorted(set(status_rows) - set(cards))
    if only_cards or only_status:
        raise ConsistencyError(
            f"任务卡与 status.md 不成对：只在 tasks/ 出现 {only_cards}；"
            f"只在 status.md 出现 {only_status}（缺行 = 状态未落盘）"
        )

    triples: list[Triple] = []
    result_index_count = 0
    for task_id, card in cards.items():
        row = status_rows[task_id]
        artifacts = read_result_index(root, task_id)
        result_index_count += 1 if artifacts else 0
        triples.append(
            Triple(
                task_id=task_id,
                status=row.status,
                objective=card.objective,
                inputs=card.inputs,
                expected_output=card.expected_output,
                artifacts=artifacts,
            )
        )
    triples.sort(key=lambda item: item.task_id)
    return Reconstruction(
        triples=tuple(triples),
        card_count=len(cards),
        status_row_count=len(status_rows),
        result_index_count=result_index_count,
    )


def _parse_body(lines: list[str], filename: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in lines:
        for label in CARD_LABELS:
            if line.strip().startswith(f"{label}: "):
                parsed[label] = line.strip()[len(label) + 2 :]
    missing = [label for label in CARD_LABELS if label not in parsed]
    if missing:
        raise ConsistencyError(f"{filename} 的正文缺行 {missing}（回退路径只依赖这三行）")
    return parsed


@dataclass(frozen=True)
class Consistency:
    """契约 §6 关系式一的两边读数 —— EN-06 那一侧**第一次被真的数过**。

    比契约那句"条目数 == 记录数"多钉一层：按 `(卡号, rel_path)` 的**成对差集**比，计数只比总数。
    两侧同源（都来自 `TaskResult.artifacts` 的 `ref.rel_path`），所以成对比较是成立的；而只比总数会
    放过"一边少了 A 的某条、另一边多了 B 的某条"这种抵消掉的样子 —— 那正是漂移最典型的形状。
    """

    pairs: int
    bus_only: tuple[tuple[str, str], ...]
    pg_only: tuple[tuple[str, str], ...]

    @property
    def pg_side_checked(self) -> bool:
        """这一格从 `False` 变成 `True` 的位置只在 `--verify-consistency`：总线侧那条路径仍然如实报
        `pg_side_checked=False`（`Reconstruction` 上那个属性没动），两个模式各说各的、不互相作证。"""
        return True


def reconcile(bus_root: Path | str, project_key: str, session: Session) -> Consistency:
    """把 `results/**` 的条目与 EN-06 的行**成对**比一次；不一致就 `ConsistencyError`（契约 §6："报错"）。

    **这一函数不写库**。宪法「三」那句"PG 恢复后按一致性规则回填"里的"回填"两字有写的一侧，
    而这里故意只交差额不交写入：把 `status.md` 的值写回 EN-02 就是给 `task_card.status` 造第二个写者
    （状态机的唯一写者在 `persistence/states.py` + 各节点），那会推翻 Phase 4 起的整条状态迁移口径。
    ⇒ 校验落在这里，写入归 T123 的装配点，且 MUST 由 owner 先裁"哪一列允许被回填"
    （登记在 `tasks.md` 的 T112 追记里，不新增任务编号）。
    """
    reconstruction = rebuild(Path(bus_root), project_key)
    bus_pairs = {
        (triple.task_id, entry[0]) for triple in reconstruction.triples for entry in triple.artifacts
    }
    request_ids = select(FeatureRequest.id).where(FeatureRequest.project_key == project_key)
    pg_pairs = {
        (str(row[0]), str(row[1]))
        for row in session.execute(
            select(Artifact.task_card_id, Artifact.rel_path).where(
                Artifact.feature_request_id.in_(request_ids)
            )
        ).all()
    }
    bus_only = tuple(sorted(bus_pairs - pg_pairs))
    pg_only = tuple(sorted(pg_pairs - bus_pairs))
    if bus_only or pg_only:
        raise ConsistencyError(
            f"契约 §6 关系式一不成立：只在总线侧 {len(bus_only)} 条 {list(bus_only)[:5]}；"
            f"只在 EN-06 {len(pg_only)} 条 {list(pg_only)[:5]}（本行只报差额，不回填）"
        )
    return Consistency(pairs=len(bus_pairs), bus_only=bus_only, pg_only=pg_only)


def _build_recovery_graph(session: Session, *, bus_root: Path | str, saver: Any) -> Any:
    """装配**恢复用**的那张图。`splitter=None` 不是"忘了给"，它本身就是那条 MUST NOT 的形状。

    FR-064 那句"恢复到可继续调度的最小状态；MUST NOT 整链路重跑"通常只能写成注释。这里它能写成
    一个缺失的依赖：`_split` 没有拆单器就抛 `SplitNotConfigured`，所以这条路径上**只要真的从头跑起来
    就会响亮失败**，而不是"跑成功了但没人发现它重跑了"。反过来，正常推进的 `advance_request` MUST 带
    拆单器 —— 两个入口的差别就是这一格。

    编排栈的 import 放在函数里：`--from-file-bus` 是 PG 不可用时那条路，模块级依赖编排栈会让
    "数据库没了连文件都捞不出来"成为新故障面（判据 `test_the_bus_side_does_not_import_the_orchestration_stack`）。
    """
    from acp.orchestrate import graph

    return graph.build_graph(
        graph.OrchestrateDeps(
            session=session, splitter=None, checkpointer=saver, bus_root=Path(bus_root)
        )
    )


@dataclass(frozen=True)
class Recovery:
    """一次 `--from-snapshot` 的读数 = 存档侧事实 + 图侧现状 + 还能动的那几张卡。"""

    restoration: restore.Restoration
    #: `update_state` 之后 thread 的 `next`（那正是"可继续调度"在图上的可读形态）。
    next_nodes: tuple[str, ...]
    resumable: tuple[str, ...]
    #: 写回之前这个 thread 是不是空的。**`True` 才是这条路径预期的形态** ——
    #: 恢复的前提就是"state 损坏/没了"；`False` 说明有人在一份活现场上又按了一次回退。
    thread_was_empty: bool

    def to_line(self) -> str:
        nxt = ",".join(self.next_nodes) or "-"
        resumable = ",".join(self.resumable) or "-"
        return (
            f"{self.restoration.to_line()} next={nxt} resumable={len(self.resumable)}[{resumable}] "
            f"thread_was_empty={str(self.thread_was_empty).lower()}"
        )


def restore_execution_state(
    session: Session,
    *,
    project_key: str,
    snapshot: str = restore.LAST,
    bus_root: Path | str = layout.BUS_ROOT_NAME,
    saver: Any = None,
    graph: Any = None,
    moment: dt.datetime | None = None,
) -> Recovery:
    """FR-064 的三步：读存档点并校验 → 把执行态写回 thread → 落回退留痕。**本函数不 commit**。

    顺序是定过的，不是顺手写的：
    ① `reconstruct()` 全程只读，四道拒绝都在这里发生 ⇒ 拒了之后现场与进来时一模一样；
    ② `update_state(as_node=normalize)` 只**写一个 checkpoint**，不执行任何节点（实测见模块头）；
    ③ `mark_rolled_back()` 才第一次碰库，且只 `flush`。
    ⇒ ①③ 在调用方的同一个工作单元里（回滚 = 库里什么都不剩），而 ② 走的是 checkpointer 那条
    **独立的 autocommit 连接**（`persistence/checkpointer.py` 模块头第 1 条）。这条差额点名在册：
    MVP-1 里"checkpoint 已写回、EN-12 的回退时刻没落库"是一个可达的半程态，跨两种连接的原子性
    不在本轮射程内（同一个族：`tasks.md` T123 的 ⑭ 提交边界）。这里选"先图后库"而不是反过来，
    是因为反过来的那份半程态会留下**一次没发生过的回退记录** —— 那是造数，比缺一条留痕更坏。
    """
    restoration = restore.reconstruct(
        session, project_key=project_key, snapshot=snapshot, bus_root=bus_root
    )
    from acp.orchestrate import graph as graph_module
    from acp.orchestrate.nodes.dispatch import DISPATCHABLE
    from acp.persistence import checkpointer

    target = graph if graph is not None else _build_recovery_graph(session, bus_root=bus_root, saver=saver)
    config = checkpointer.thread_config(
        kind=graph_module.GATE_KIND, feature_request_id=restoration.feature_request_id
    )
    before = target.get_state(config)
    target.update_state(config, restoration.thread_values(), as_node=graph_module.NORMALIZE)
    after = target.get_state(config)
    restore.mark_rolled_back(
        session, restoration, now=moment if moment is not None else dt.datetime.now(dt.timezone.utc)
    )
    return Recovery(
        restoration=restoration,
        next_nodes=tuple(str(node) for node in (after.next or ())),
        resumable=restoration.resumable(dispatchable=DISPATCHABLE),
        thread_was_empty=not bool(before.values),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="崩溃回退的三条路（FR-065 从文件总线重建 / FR-064 从存档点恢复执行态 / PG 恢复后的一致性校验）"
    )
    #: **三条路做成互斥的三选一而不是旗标组合**：它们读的两侧本来就不同（文件 / 文件+checkpoint /
    #: 文件+EN-06），能同时给出的只有"用哪条来判定"这种猜出来的优先级。互斥组让"两个模式一起跑"
    #: 在 argparse 层就退 2，不需要产品代码里再写一段 if 去决定谁说了算。
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--from-file-bus",
        metavar="PROJECT_KEY",
        help="PG 不可用时从文件总线重建任务—状态—产物三元组（T022 / FR-065）",
    )
    group.add_argument(
        "--from-snapshot",
        metavar="LAST_OR_SNAPSHOT_ID",
        help=f"从最近存档点恢复执行态（T110 / FR-064）；接受 {restore.LAST} 或一个 snapshot id",
    )
    group.add_argument(
        "--verify-consistency",
        metavar="PROJECT_KEY",
        help="PG 恢复后把 results/** 与 EN-06 成对比一次（契约 §6 关系式一）；只报差额，不回填",
    )
    #: `--from-file-bus` 的项目号在旗标值里，`--from-snapshot` 的那个位置给的是**点**不是项目
    #: ⇒ 恢复模式必须另给 `--project`（存档点按项目枚举，没有项目就没有"最近"这件事，
    #: 而"跨项目挑最后一个"会把 B 工程的现场盖到 A 工程上）。缺它是一条 usage error，不是默认值。
    parser.add_argument("--project", metavar="PROJECT_KEY", default=None)
    parser.add_argument("--bus-root", default=layout.BUS_ROOT_NAME)
    return parser


def _run_from_file_bus(args: argparse.Namespace) -> int:
    try:
        result = rebuild(Path(args.bus_root), args.from_file_bus)
    except (ConsistencyError, BusError) as error:
        print(f"RECOVER FAIL :: {error}")
        return 1

    for triple in result.triples:
        paths = ";".join(entry[0] for entry in triple.artifacts) or "-"
        print(
            f"triple task={triple.task_id} status={triple.status} "
            f"artifacts={len(triple.artifacts)}[{paths}] 期望产物={triple.expected_output}"
        )
    print(
        f"RECOVER OK :: triples={len(result.triples)} cards={result.card_count} "
        f"status_rows={result.status_row_count} result_indexes={result.result_index_count} "
        f"pg_side_checked={result.pg_side_checked}"
    )
    return 0


def _run_from_snapshot(args: argparse.Namespace) -> int:
    if not args.project:
        print(
            "usage error: --from-snapshot 必须与 --project 同给"
            "（存档点按项目枚举，不给项目就没法知道是哪一个工程的 last）"
        )
        return 2

    from acp.orchestrate.state import IntakeState
    from acp.persistence import checkpointer
    from acp.persistence.session import make_session_factory, new_session

    try:
        connection = checkpointer.open_connection()
    except (checkpointer.CheckpointerUnavailable, CredentialMissing) as error:
        print(f"RECOVER FAIL :: {error}")
        return 3

    try:
        try:
            saver = checkpointer.build_checkpointer(connection, state_schema=IntakeState)
            with new_session(make_session_factory()) as session:
                recovery = restore_execution_state(
                    session,
                    project_key=args.project,
                    snapshot=args.from_snapshot,
                    bus_root=Path(args.bus_root),
                    saver=saver,
                )
        except (
            ConsistencyError,
            restore.RestoreMisuse,
            root.ArchiveRootMisuse,
            cadence.ArchiveMisuse,
            cadence.ArchiveFailure,
        ) as error:
            print(f"RECOVER FAIL :: {error}")
            return 1
    except Exception as error:  # noqa: BLE001 — 只报类名：驱动与库的原文可能带连接串参数
        print(f"RECOVER FAIL :: 非预期异常（{type(error).__name__}），本行不回显驱动原文")
        return 1
    finally:
        connection.close()

    print(f"RECOVER OK :: mode=from_snapshot {recovery.to_line()}")
    return 0


def _run_verify_consistency(args: argparse.Namespace) -> int:
    from acp.persistence.session import make_session_factory, new_session

    try:
        with new_session(make_session_factory()) as session:
            consistency = reconcile(Path(args.bus_root), args.verify_consistency, session)
    except (ConsistencyError, BusError) as error:
        print(f"RECOVER FAIL :: {error}")
        return 1
    except CredentialMissing as error:
        print(f"RECOVER FAIL :: {error}")
        return 3
    except Exception as error:  # noqa: BLE001 — 只报类名：驱动原文可能带连接串参数
        print(f"RECOVER FAIL :: 非预期异常（{type(error).__name__}），本行不回显驱动原文")
        return 1

    print(
        f"RECOVER OK :: mode=verify_consistency pairs={consistency.pairs} "
        f"bus_only={len(consistency.bus_only)} pg_only={len(consistency.pg_only)} "
        f"pg_side_checked={str(consistency.pg_side_checked).lower()}"
    )
    return 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv[1:])
    if args.from_snapshot:
        return _run_from_snapshot(args)
    if args.verify_consistency:
        return _run_verify_consistency(args)
    return _run_from_file_bus(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
