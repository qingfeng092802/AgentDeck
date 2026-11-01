"""T094 + T096：唯一交付包 —— 索引，不是第二份文件树；冲突按原始需求裁，裁不动就升人工。

**"唯一"由谁保证**：EN-17 上那条 `UNIQUE(feature_request_id)`（`models_registry.py:166-168`）。
本模块**不自己数行数** —— 那样"唯一"就变成应用层的一个变量，删掉它就静默放行第二包。
这里的做法与 `handoff.py` 那两条上限同模式：应用层先给一次可读的理由（`PackageAlreadyAssembled`
带"第几次组装、谁组的"），库面那条唯一索引是"应用层被人改坏了"时的最后一道；
判据里两道都测（`tests/integration/test_deliverable.py` 的 ②③）。

**入口文件必须真实存在**（FR-036 / R-07 的第①条读取入口）。`entry_path` 不是一个字符串字段：
它在装配时就对着总线根目录查过一次存在性，并且 MUST 是 manifest 里的一条 ——
"交付包说入口是 `index.html`，打开却没有这个文件"是使用者第一眼就能看到的假。

**冲突裁决（T096 / FR-037）**：以"对照原始需求"为准，裁决人与理由落 EN-15 `meeting_note`；
**裁决对象含自身 ⇒ 升人工**（宪法原则 III 那句"裁决者若为冲突任一方，MUST 升级人工而非自裁"）。
这一条与三岗互斥是同一个理由的两半：自己判自己在本平台不给任何出口，连"总管兼架构师"也不行。
Phase 7 前半段这里**只抛错、没把卡推进 `ESCALATED`**（登记为待裁 ⑪："冲突是需求单级的，`ESCALATED`
是任务卡态，谁进去要先裁"）⇒ owner 2026-10-14 裁定：涉事卡进 `ESCALATED`，判定条件就是"裁决人为冲突任一方"。
落点在 `adjudicate` 的校验趟之后、抛错之前，涉事卡由**候选产物行反查**得到（见 `cards_of_conflict` 为何不收申报）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.bus import layout
from acp.persistence import events
from acp.persistence.models_core import Artifact, TaskCard
from acp.persistence.models_registry import Deliverable, MeetingNote

NODE = "assemble"

DELIVERED_STATES = ("DELIVERED", "DELIVERED_WITH_ISSUES")

#: 已定论的卡态。⑪ 的升级**不追回**这些卡 —— 理由与 `data-model.md §2` 那条
#: "已完成任务不得回到 `READY` 重跑（FR-041）" 同源：后面的冲突不改变"那张卡当时确实交付了"这件事。
TERMINAL_STATES = ("DELIVERED", "DELIVERED_WITH_ISSUES", "REJECTED")

ESCALATED = "ESCALATED"


class AssembleMisuse(ValueError):
    """装配用法错。`code` 是给端点与审计行的稳定键名。"""

    code = "assemble_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


class PackageAlreadyAssembled(AssembleMisuse):
    """这个需求单已经有交付包了。FR-036 要"唯一一份"，第二包 MUST 报错而不是静默覆盖。"""

    code = "delivery_package_exists"


@dataclass(frozen=True)
class ManifestEntry:
    """一条产物索引。`rel_path` 是**总线内相对路径**，MUST NOT 是绝对路径 ——
    绝对路径把机器形状写进交付包，换一台机器这份包就不可读（FR-063 的子目录隔离同理）。
    """

    rel_path: str
    producer_agent_id: str
    task_card_id: str
    bytes: int
    lines: int
    format: str

    def to_payload(self) -> dict[str, object]:
        return {
            "rel_path": self.rel_path,
            "producer": self.producer_agent_id,
            "task_card_id": self.task_card_id,
            "bytes": self.bytes,
            "lines": self.lines,
            "format": self.format,
        }


@dataclass(frozen=True)
class Conflict:
    """一处多份产物互冲。`sides` 是冲突涉及的 `agent_id`（谁产的），`chosen_rel_path` 是裁决结果。"""

    item: str
    sides: tuple[str, ...]
    candidates: tuple[str, ...]
    chosen_rel_path: str
    rationale: str


@dataclass(frozen=True)
class Package:
    deliverable_id: uuid.UUID
    feature_request_id: uuid.UUID
    entry_path: str
    manifest: tuple[ManifestEntry, ...]
    open_issues: tuple[dict[str, str], ...]
    assembled_by: str

    def to_payload(self) -> dict[str, object]:
        return {
            "deliverable_id": str(self.deliverable_id),
            "feature_request_id": str(self.feature_request_id),
            "entry_path": self.entry_path,
            "manifest": [entry.to_payload() for entry in self.manifest],
            "open_issues": list(self.open_issues),
            "assembled_by": self.assembled_by,
        }


def validated_manifest(session: Session, feature_request_id: uuid.UUID) -> tuple[ManifestEntry, ...]:
    """交付包的成员 = 这张需求单下**门禁判为 valid** 的产物行（FR-019 之后才有资格进包）。

    不查 `validated_status` 就会把"自述成功但产物不达标"的那些行也组进最终交付 ——
    那正是 SC-007 要防的虚报形状，而交付包是它最后一次能被看见的地方。

    **同一个 `rel_path` 只算一份**（2026-10-28 真跑补）：清单是**文件**清单，不是行清单。
    实测现场 —— 一张"总览页"的卡跑完，执行器把整套站都写了一遍（`index.html` / `members.html` /
    `tasks.html` 全在），于是同一个路径在 `artifact` 里留下了两三行（T02 与 T04 都写了 `index.html`），
    而盘上只有一个文件（后写覆盖先写）。不去重的两个后果都是硬的：

    - 组包按行数报成员数 ⇒ 交付包说自己有 19 份，盘上只有 12 份；
    - 入口判定读到两份同名 `index.html` ⇒ `entry_ambiguous` ⇒ **交付包永远组不出来**。

    留哪一行：`created_at` 最新的那一行 —— 它是盘上那份内容的实际写者（后写覆盖先写），
    `bytes` / `lines` 也只有它说的是真的。同一时刻再按卡号取大的，保证结果与查询顺序无关。
    """
    rows = session.scalars(
        select(Artifact)
        .where(Artifact.feature_request_id == feature_request_id, Artifact.validated_status == "valid")
        .order_by(Artifact.rel_path, Artifact.task_card_id)
    ).all()
    if not rows:
        raise AssembleMisuse(
            f"需求单 {feature_request_id} 下没有任何 valid 产物行 ⇒ 空交付不组包（宁可不交，不交一份空的）",
            code="nothing_to_deliver",
        )
    latest: dict[str, Artifact] = {}
    for row in rows:
        key = str(row.rel_path)
        current = latest.get(key)
        if current is None or (row.created_at, str(row.task_card_id)) > (
            current.created_at,
            str(current.task_card_id),
        ):
            latest[key] = row
    return tuple(
        ManifestEntry(
            rel_path=str(row.rel_path),
            producer_agent_id=str(row.writer_agent_id),
            task_card_id=str(row.task_card_id),
            bytes=int(row.bytes),
            lines=int(row.lines),
            format=str(row.format),
        )
        for _, row in sorted(latest.items())
    )


def cards_of_conflict(
    session: Session, feature_request_id: uuid.UUID, conflict: Conflict
) -> tuple[str, ...]:
    """这处冲突**涉及的卡** = 那些候选产物行所在的 `task_card_id`（库里反查，不由调用方申报）。

    为什么从库取而不是让调用方传：⑪ 的效果是"把卡推进 `ESCALATED`"，那是一张写别的表的行；
    调用方申报"这几张卡 involved"就等于让想绕过门禁的一方自己填门禁的输入 —— 与
    `bus/actor.py` 那条"`read_only` MUST NOT 由调用方传参决定"是同一条理由。
    候选路径对不上任何产物行时返回空元组，调用处 MUST 响亮处理（见 `adjudicate` 里那一段）。
    """
    if not conflict.candidates:
        return ()
    rows = session.scalars(
        select(Artifact.task_card_id)
        .where(
            Artifact.feature_request_id == feature_request_id,
            Artifact.rel_path.in_(tuple(conflict.candidates)),
        )
        .distinct()
    ).all()
    return tuple(sorted({str(card_id) for card_id in rows}))


def _escalate_self_adjudicated(
    session: Session,
    feature_request_id: uuid.UUID,
    conflict: Conflict,
    *,
    decided_by: str,
    created_at: dt.datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """待裁 ⑪：涉事卡进 `ESCALATED`。返回 `(升级的卡号…, 因已定论而没动的卡号…)`。

    只改状态 + 留痕，**不动执行者、不动指派、不动两个计数** —— 形状逐字对齐 `handoff._escalate`
    （那里那句"保留原执行者是刻意的"在这里同样成立：升级之后走 ⑫ 的人工出口，人选 READY 还是
    REJECTED 是那个决定的内容，不是这里的）。
    """
    escalated: list[str] = []
    held: list[str] = []
    for card_id in cards_of_conflict(session, feature_request_id, conflict):
        card = (
            session.query(TaskCard)
            .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == card_id)
            .one_or_none()
        )
        if card is None:  # 产物行在、卡行不在 = 引用完整性坏了，点名而不是静默跳过
            held.append(f"{card_id}(卡行缺失)")
            continue
        if str(card.status) in TERMINAL_STATES:
            held.append(card_id)
            continue
        before = str(card.status)
        card.status = ESCALATED
        events.record(
            session,
            entity="task_card",
            entity_id=card.id,
            kind="TASK",
            actor_agent_id=decided_by,
            payload={
                "node": NODE,
                "action": "escalated_self_adjudication",
                "conflict_item": conflict.item,
                "reason": "裁决人就是冲突任一方 ⇒ 相关方不得自裁（宪法原则 III）",
                "adjudicator": decided_by,
                "conflict_sides": sorted(set(conflict.sides)),
                "prior_status": before,
                "card_status_after": card.status,
            },
            created_at=created_at,
        )
        escalated.append(card.id)
    session.flush()
    return tuple(escalated), tuple(held)


def adjudicate(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    project_key: str,
    conflicts: Sequence[Conflict],
    decided_by: str,
    created_at: dt.datetime,
) -> tuple[str, ...]:
    """T096：逐处冲突落一条 EN-15 纪要，返回纪要 id 列表。

    三条硬规则在这里判，不在调用方判：
    - **裁决者不能是冲突任一方**（含自身 ⇒ 升人工，抛 `code="adjudicator_is_a_side"`，并且
      owner 2026-10-14 待裁 ⑪ 之后**真的把涉事卡推进 `ESCALATED`**，不再只抛错）；
    - **选中的那一份必须在这处冲突的候选里**（不在就是凭空造了第三个答案，FR-037 的"对照原始需求"
      是选择依据，不是新增产物的许可）；
    - **理由非空**（只写"就这么定了"等于没记）。

    **两趟：先整批判完，再整批落库**（与 `registry/persist.py::persist_registrations` 同一手法）。
    第一版是一趟到底的循环，于是"三处冲突里第三处不合法"会留下前两处的纪要再抛 ——
    读起来像"裁了两条、第三条被拒"，而事实是这次裁决整批没成立。判据 =
    `tests/integration/test_deliverable.py::test_a_refused_ruling_leaves_no_note_at_all`。

    改成两趟之后这里多了一个顺序事实，写清以免被误读：**校验趟会把所有冲突都判一遍**（而不是撞到第一条
    就退），因为 ⑪ 的升级是**这次提交里所有自裁冲突**的后果，只升第一条会让"另外两处也同样是自裁"这件事
    不留痕。抛出的仍是**批内第一处**问题的稳定键（保持既有判据的读法不变）。

    **升级不落纪要**：被拒的这次裁决一条 `meeting_note` 都不写 —— 写了就等于把一次没有资格的裁决
    装订成"开过会"。卡的状态变更是**门禁自己的产出**，不属于被拒绝的那一批，所以它先于异常落库；
    代价如实点名：本模块不 own 事务（节点只 `flush`，与 `events.py` / `persist.py` 同一口径），
    调用方若在捕获异常后回滚整笔事务，这条升级会一起没了 ⇒ **端点/装配段那侧 MUST 提交这一笔**，
    今天还没有生产调用方（`evidence/phase-07.md` 未闭 ① 那条），这一句就归那一笔的判据。
    """
    rulings = list(conflicts)
    problems: list[tuple[str, str]] = []
    self_adjudicated: list[Conflict] = []
    for conflict in rulings:
        if decided_by in conflict.sides:
            self_adjudicated.append(conflict)
            problems.append((
                "adjudicator_is_a_side",
                f"冲突「{conflict.item}」的参与方含 {decided_by}，而裁决人也是它 ⇒ 升人工裁决（FR-037），"
                "MUST NOT 自裁",
            ))
        if conflict.chosen_rel_path not in conflict.candidates:
            problems.append((
                "chosen_not_a_candidate",
                f"冲突「{conflict.item}」选中的 {conflict.chosen_rel_path!r} 不在候选 {list(conflict.candidates)} 里",
            ))
        if not conflict.rationale.strip():
            problems.append((
                "rationale_required",
                f"冲突「{conflict.item}」没有裁决理由 ⇒ 纪要里只写'就这么定了'等于没记",
            ))

    if problems:
        escalated: list[str] = []
        held: list[str] = []
        untraceable: list[str] = []
        for conflict in self_adjudicated:
            moved, not_moved = _escalate_self_adjudicated(
                session, feature_request_id, conflict, decided_by=decided_by, created_at=created_at
            )
            escalated.extend(moved)
            held.extend(not_moved)
            if not moved and not not_moved:
                # 涉事候选在库里反查不到任何卡 ⇒ 升级没有落点。这不许静默：抛出去的键还是
                # `adjudicator_is_a_side`（裁决确实被拒了），但消息 MUST 点名"没能升人工"这件事。
                untraceable.append(conflict.item)
        code, message = problems[0]
        if self_adjudicated:
            detail = (
                f"已推进 {ESCALATED} 的卡：{list(escalated) or '（无）'}；"
                f"已定论不予追回的卡：{list(held) or '（无）'}"
            )
            if untraceable:
                detail += f"；这些冲突反查不到涉事卡，升级没落点：{untraceable}"
            message = f"{message} ⇒ {detail}"
        raise AssembleMisuse(message, code=code)

    note_ids: list[str] = []
    for conflict in rulings:
        note_id = str(uuid.uuid4())
        session.add(
            MeetingNote(
                id=note_id,
                project_key=project_key,
                authored_by_agent_id=decided_by,
                decision=f"{conflict.item} → {conflict.chosen_rel_path}：{conflict.rationale}",
                conflict_sides=list(conflict.sides),
                created_at=created_at,
            )
        )
        note_ids.append(note_id)
    session.flush()
    return tuple(note_ids)


def _assert_manifest_matches_disk(manifest: Sequence[ManifestEntry], bus_root: Path) -> None:
    """P0-4：组包前对每一条 manifest 重新 `stat()`，不信库里的旧快照。

    `bytes` 是写入瞬间由 `measure_artifact()` 量的，之后文件可能被后一次运行覆盖、被手工改小、
    或者整份消失。交付包是给人下载的，**它自带的清单必须与磁盘同一时刻一致**；只读快照就组包，
    等于把"上次运行的真"写成"这一次的交付物"。

    缺失与字节不符都判 `artifact_stale`（`AssembleMisuse` 的稳定 code），不静默跳过：
    跳过会让交付包少几条却仍然"成功"，比直接失败更难发现。
    """
    stale: list[str] = []
    for entry in manifest:
        target = bus_root / entry.rel_path
        if not target.is_file():
            stale.append(f"{entry.rel_path}（缺失）")
            continue
        actual = target.stat().st_size
        if actual != entry.bytes:
            stale.append(f"{entry.rel_path}（声明 {entry.bytes}B，盘上 {actual}B）")
    if stale:
        preview = "；".join(stale[:5])
        more = f"；另有 {len(stale) - 5} 条" if len(stale) > 5 else ""
        raise AssembleMisuse(
            f"交付清单与磁盘不一致 ⇒ 拒绝组包（artifact_stale）：{preview}{more}",
            code="artifact_stale",
        )


def assemble(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    entry_path: str,
    assembled_by: str,
    created_at: dt.datetime,
    open_issues: Sequence[dict[str, str]] = (),
    bus_root: Path | None = None,
) -> Package:
    """T094：把 valid 产物组成**唯一一份**交付包并落 EN-17。

    `bus_root` 给了就做两件事：① 查入口文件真实存在；② 对 manifest **每一条**重新 `stat()`，
    缺失或字节不符即 `artifact_stale`（P0-4，2026-10-29）。不给 = 不查，但判据那条 MUST 给。
    """
    manifest = validated_manifest(session, feature_request_id)
    if entry_path not in {entry.rel_path for entry in manifest}:
        raise AssembleMisuse(
            f"入口 {entry_path!r} 不在 manifest 里 ⇒ 交付包不能指向一个本批没交的东西",
            code="entry_not_in_manifest",
        )
    if bus_root is not None and not (bus_root / entry_path).is_file():
        raise AssembleMisuse(
            f"入口 {entry_path!r} 在总线根目录下不存在 ⇒ 这是「打开就能看见的假」"
            "（R-07 第①条的读取入口）",
            code="entry_missing",
        )
    cards = session.scalars(
        select(TaskCard).where(
            TaskCard.feature_request_id == feature_request_id,
            TaskCard.status.in_(DELIVERED_STATES),
        )
    ).all()
    if not cards:
        raise AssembleMisuse(
            f"需求单 {feature_request_id} 下没有一张处于 {list(DELIVERED_STATES)} 的卡 ⇒ 还没交付就组包",
            code="nothing_delivered",
        )
    if any(str(card.status) == "DELIVERED_WITH_ISSUES" for card in cards) and not open_issues:
        raise AssembleMisuse(
            "有卡带未达标项交付，而 `open_issues` 是空的 ⇒ FR-035 要求显式列出未达标项清单，"
            "空清单就是把'有问题'写成了'没问题'",
            code="open_issues_required",
        )

    if bus_root is not None:
        _assert_manifest_matches_disk(manifest, bus_root)

    row = Deliverable(
        id=uuid.uuid4(),
        feature_request_id=feature_request_id,
        manifest=[entry.to_payload() for entry in manifest],
        entry_path=entry_path,
        open_issues=list(open_issues),
        assembled_by_agent_id=assembled_by,
        created_at=created_at,
    )
    try:
        # **savepoint 而不是 `session.rollback()`**：事务边界归调用方（与 `persistence/events.py`、
        # `registry/persist.py` 同一口径 —— 节点只 `flush`，不 own 事务）。第一版这里写的是
        # `session.rollback()`，它会把调用方那一整笔事务一起撤掉：表现是"第二包被拒了，
        # 而第一包连同卡、留痕一起没了"，正好违了本行要证的那句"不静默覆盖"。
        # `begin_nested()` 只回滚这一条 INSERT，外层事务里的东西都还在。
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        raise PackageAlreadyAssembled(
            f"需求单 {feature_request_id} 已有交付包 ⇒ 第二包被唯一索引拒（不静默覆盖第一包的内容）"
            f"（库面约束 {type(exc).__name__}）",
            code="delivery_package_exists",
        ) from exc
    events.record(
        session,
        entity="feature_request",
        entity_id=str(feature_request_id),
        kind="TASK",
        actor_agent_id=assembled_by,
        payload={
            "node": NODE,
            "action": "delivery_assembled",
            "deliverable_id": str(row.id),
            "entries": len(manifest),
            "entry_path": entry_path,
            "open_issues": len(open_issues),
        },
        created_at=created_at,
    )
    session.flush()
    return Package(
        deliverable_id=row.id,
        feature_request_id=feature_request_id,
        entry_path=entry_path,
        manifest=manifest,
        open_issues=tuple(open_issues),
        assembled_by=assembled_by,
    )


def payload_of(package: Package) -> dict[str, object]:
    """公共区那份 JSON 的形状（写入归 `bus/orchestrator_writer.py` 单点，本模块只给内容）。"""
    return package.to_payload()


def delivery_path() -> str:
    """交付包在总线里的路径 —— 唯一载体仍是 `bus/layout.py`，这里只是转发给它自己的消费者。"""
    return layout.DELIVERY_PACKAGE_JSON
