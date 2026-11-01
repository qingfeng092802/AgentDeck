"""公共区**单点代笔** + 并发串行队列（T020 / T019，FR-052 / FR-013｜R-24）。

为什么要一根真队列而不是一个锁：契约说公共区"唯一写入者 = `orchestrator_writer`"，
锁只保证不重叠，队列保证**字面上只有一个线程碰过这些文件**——复核时可以只看一个线程 id。
提交方拿到"写完才返回"的语义（读己之写），顺序 = 提交顺序（FIFO），
`tests/guards/test_bus_isolation.py` 的并发用例断言的正是这两点。

权限与格式**不在这里重复实现**：所有落盘都走 `gateway.write_artifact`，
本模块只是那条唯一入口的编排侧调用者 + 串行化器。
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from acp.bus import acl, layout
from acp.bus.gateway import CARD_LABELS, BusGateway, TaskCardBody, render_card

ORCHESTRATOR = acl.Actor(acl.PUBLIC_OWNER, "orchestrator")

STATUS_HEADER = ("task_id", "状态", "执行者", "审查者", "验收者", "产物落点", "累计用量（按口径分列）")

# FR-061 的三口径。用量列**分列**呈现，MUST NOT 相加成一个数冒充总成本。
USAGE_BASIS = ("TOKEN", "SEAT_CREDIT", "BYOK")


@dataclass(frozen=True)
class StatusRow:
    task_id: str
    status: str
    executor: str
    reviewer: str
    acceptor: str
    artifact_path: str
    usage: Mapping[str, Decimal]

    def render(self) -> str:
        parts = ";".join(f"{basis}={self.usage.get(basis, Decimal(0))}" for basis in USAGE_BASIS)
        return (
            f"| {self.task_id} | {self.status} | {self.executor} | {self.reviewer} "
            f"| {self.acceptor} | {self.artifact_path} | {parts} |"
        )

    @classmethod
    def parse(cls, line: str) -> StatusRow:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(STATUS_HEADER):
            raise ValueError(
                f"status.md 行宽不匹配（期望 {len(STATUS_HEADER)} 列，读到 {len(cells)}）"
            )
        usage: dict[str, Decimal] = {}
        for chunk in cells[6].split(";"):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                usage[key.strip()] = Decimal(value.strip() or 0)
        return cls(cells[0], cells[1], cells[2], cells[3], cells[4], cells[5], usage)


@dataclass
class _Job:
    action: Callable[[], Any]
    done: threading.Event
    result: Any = None
    error: BaseException | None = None


def _is_separator_row(line: str) -> bool:
    return set(line.replace("|", "").replace("-", "").strip()) == set() and "-" in line


class OrchestratorWriter:
    def __init__(self, gateway: BusGateway) -> None:
        self.gateway = gateway
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._closing = threading.Event()
        self._worker = threading.Thread(
            target=self._loop, name="acp-orchestrator-writer", daemon=True
        )
        self._worker.start()

    @property
    def worker_thread_id(self) -> int:
        """给判据用的**身份可见性**：公共区的每次落盘都发生在这个 tid 上。"""
        return self._worker.ident or -1

    def stop(self) -> None:
        """先排空再停线程——未排空就退出会留下"提交方拿到成功但文件没落"的假象。"""
        self._queue.join()
        self._closing.set()
        self._worker.join(timeout=5)

    def __enter__(self) -> OrchestratorWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _loop(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            try:
                job.result = job.action()
            except BaseException as error:  # 单条失败要还给提交方，线程不能静默死
                job.error = error
            finally:
                job.done.set()
                self._queue.task_done()

    def _submit(self, action: Callable[[], Any]) -> Any:
        job = _Job(action, threading.Event())
        self._queue.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    # --- 公共区的五个动作（全部经网关这一入口）------------------------------------

    def write_task_card(
        self,
        task_id: str,
        slug: str,
        body: TaskCardBody,
        frontmatter: Mapping[str, object] = {},
    ) -> str:
        rel_path = f"{layout.TASKS_DIR}/{task_id}-{slug}.md"
        # 契约 §3 的文件名规范在写之前判，不留"文件名不符但内容合法"的中间态：
        # 三元组重建靠文件名解 task_id（recover 的 TASK_FILE_RE），名字错 = 恢复时读不到。
        if not layout.is_task_file(f"{task_id}-{slug}.md"):
            raise ValueError(
                f"任务卡文件名不符 `T<NN>-<slug>.md`：{task_id}-{slug}.md"
                f"（NN 至少两位、slug 小写）"
            )
        content = render_card(body, frontmatter)
        self._write(rel_path, content)
        return rel_path

    def write_result_index(self, task_id: str, entries: Sequence[tuple[str, int, int, str]]) -> str:
        """`results/<task>.md`：产物**索引**，本体留在各 workspace（D-15，不复制第二份文件树）。

        每条 = (rel_path, bytes, lines, format)，四个值都是实测值而非自评（FR-019）。
        """
        rel_path = f"{layout.RESULTS_DIR}/{task_id}.md"
        lines = [
            f"# {task_id} 产物索引",
            "",
            "| rel_path | bytes | lines | format |",
            "|---|---|---|---|",
            *(f"| {path} | {size} | {rows} | {fmt} |" for path, size, rows, fmt in entries),
            "",
        ]
        self._write(rel_path, "\n".join(lines))
        return rel_path

    def write_delivery_package(self, payload: Mapping[str, object]) -> str:
        """T095：`results/delivery_package.json`，公共区单点代笔（写入者只有本类）。

        写的是**索引的索引**（manifest 指向各 workspace 的产物），MUST NOT 是产物副本 ——
        复制一份文件树就是第二份事实态（`data-model.md` 关于 EN-17 那一段，原则 I）。
        JSON 由 `ensure_ascii=False` 落盘：这份文件是给人读的最终交付说明，转义成 `\\uXXXX` 的中文
        会让"打开就能看懂"这条 R-07 判据在字面上就不成立。
        """
        self._write(
            layout.DELIVERY_PACKAGE_JSON,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return layout.DELIVERY_PACKAGE_JSON

    def upsert_status(self, row: StatusRow) -> str:
        """每次状态迁移后由单点**重写整表**（契约 §4）。

        重写而不是追加：追加法要求读者自己取最后一行，而契约 §6 的关系式是
        "每张任务在 `status.md` 恰有一行"——重写让它由构造成立，不靠读侧纪律。

        **读—改—写整段都在队列里做**：把"读现有行"放在调用线程上，十个并发迁移会各自读到
        旧表、各自写回一张只含自己那行的表 ⇒ 丢更新。首版就是这么写的，`test_concurrent_status_updates`
        把它拦下来了。
        """
        self._submit(lambda: self._upsert_status_job(row))
        return layout.STATUS_MD

    def _upsert_status_job(self, row: StatusRow) -> None:
        rows = [item for item in self._read_rows() if item.task_id != row.task_id]
        rows.append(row)
        rows.sort(key=lambda item: item.task_id)
        self.gateway.write_artifact(
            ORCHESTRATOR, layout.STATUS_MD, status_table(rows), capability=None
        )

    def append_event(
        self,
        *,
        entity: str,
        action: str,
        actor: str,
        summary: str,
        occurred_at: datetime | None = None,
    ) -> str:
        """`daily-log.md` 的追加式事件行：`时间 | 实体 | 动作 | actor | 摘要`（契约 §4）。

        同样是整段进队列——"读全文 + 拼一行 + 写回"若跨线程执行，两个事件会互相覆盖，
        而契约 §4 要求每条交互都落盘（FR-052），丢一条就是审计断链。
        """
        stamp = (occurred_at or datetime.now().astimezone()).isoformat(timespec="seconds")
        line = f"- {stamp} | {entity} | {action} | {actor} | {summary}"
        self._submit(lambda: self._append_event_job(line))
        return layout.DAILY_LOG_MD

    def _append_event_job(self, line: str) -> None:
        path = self.gateway.root / layout.DAILY_LOG_MD
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        self.gateway.write_artifact(
            ORCHESTRATOR, layout.DAILY_LOG_MD, f"{current}{line}\n", capability=None
        )

    def write_agents_md(self, project_key: str, agents: Sequence[str]) -> str:
        """T019：AGENTS.md 由**代码生成**而非手抄。

        内容与 `acl.RULES` / `CARD_LABELS` 同源 ⇒ 改归属表不需要回来改这份文本，
        也就不会出现"协议说明说要这样、网关实际做那样"的两份口径。
        """
        self._write(layout.AGENTS_MD, agents_md(project_key, agents))
        return layout.AGENTS_MD

    def ensure_layout(self, agents: Sequence[str]) -> list[str]:
        """按 `layout.py` 建出契约 §1 的**目录**（三个 .md 不在这建：它们是文件，
        由各自的写入动作生成；把它们 mkdir 成目录会让后续写入撞 NotADirectoryError）。
        """
        made: list[str] = []
        for entry in layout.PUBLIC_DIRS:
            (self.gateway.root / entry).mkdir(parents=True, exist_ok=True)
            made.append(entry)
        for agent_id in agents:
            (self.gateway.root / layout.workspace_dir_name(agent_id)).mkdir(
                parents=True, exist_ok=True
            )
            made.append(layout.workspace_dir_name(agent_id))
        return made

    def _write(self, rel_path: str, content: str) -> None:
        self._submit(
            lambda: self.gateway.write_artifact(ORCHESTRATOR, rel_path, content, capability=None)
        )

    def _read_rows(self) -> list[StatusRow]:
        path = self.gateway.root / layout.STATUS_MD
        if not path.exists():
            return []
        return [
            StatusRow.parse(line)
            for line in path.read_text(encoding="utf-8").split("\n")
            if line.startswith("|") and not _is_separator_row(line)
            and "task_id" not in line
        ]


def status_table(rows: Sequence[StatusRow]) -> str:
    header = "| " + " | ".join(STATUS_HEADER) + " |"
    separator = "|" + "---|" * len(STATUS_HEADER)
    return "\n".join([header, separator, *(row.render() for row in rows)]) + "\n"


def agents_md(project_key: str, agents: Sequence[str]) -> str:
    rules = "\n".join(
        f"| `{rule.pattern}` | `{rule.owner}` | {rule.note} |" for rule in acl.RULES
    )
    workspaces = "\n".join(f"- `{layout.workspace_dir_name(name)}/`" for name in sorted(agents))
    card = "\n".join(f"{label}: <内容>" for label in CARD_LABELS)
    return (
        f"# AGENTS.md（项目 `{project_key}`）\n\n"
        "本文件由 `bus/orchestrator_writer.py` 从代码生成，是协议的**运行时副本**；"
        "唯一事实源仍是 `contracts/file-bus.md` 与 `src/acp/bus/acl.py`"
        "——两处与本文件不一致时，以那两处为准并重新生成。\n\n"
        "## 写入归属（静态表，运行时不推断）\n\n"
        "| 路径模式 | 唯一写入者 | 说明 |\n|---|---|---|\n"
        f"{rules}\n\n"
        "## 本项目当前在册的 workspace\n\n"
        f"{workspaces}\n\n"
        "## 任务卡的可见面（Agent 只看到这三行）\n\n"
        "```markdown\n"
        f"{card}\n"
        "```\n\n"
        "五类受控 frontmatter 不进可见面：网关渲染时剥去；PG 不可用时按这三行正文重建三元组。\n"
    )
