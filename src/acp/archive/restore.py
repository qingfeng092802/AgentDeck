"""T110（FR-064｜R-14）：编排状态损坏时，从最近存档点重建执行态并重放该点之后的任务事件。

宪法「三」的崩溃回退第一条写的是"**LangGraph state 损坏** ⇒ 从最近一次 git 存档点重建执行态，
重放该存档点之后的任务事件（任务卡 + 结果索引），恢复到可继续调度的最小状态；MUST NOT 整链路重跑"。
本模块交出的就是这句话里能证的那三件：**哪一个点**（校验过句柄的那个）、**该点之后发生过什么**
（读 EN-14，不是重跑）、**现在还能调度谁**（卡号 + 库里当前状态的交集）。

**这一层为什么在 `archive/` 而不是 `orchestrate/`**：它一次都不碰图、不碰适配器 ——
`test_the_archive_layer_cannot_reach_the_guard_layer` 扫的是整个 `src/acp/archive/` 包，
本模块因此**物理上无法**"整链路重跑"（要跑图就得 import 图，而那一条判据立刻红）。
把 thread 写回去的那一步（`update_state`）在装配侧：CLI `python -m acp.recover --from-snapshot last`
才是同时认识"存档事实"与"图形状"的那一层（`thread_values()` 交出去的就是给它用的）。

**四道拒绝全部发生在写之前**，一条不落：
① 该项目一个存档点都没有 ⇒ `no_points`（不猜一个起点，也不建一个）；
② `ref` 不是真实句柄 ⇒ `ref_not_real`（库里 T-15 那种自指行如果还活着，它**不能**当恢复起点 ——
   拿它恢复等于把"曾经只登记不实现"伪装成"有一份存档"）；
③ 句柄形状对，但那个对象在存档根里查不出来 ⇒ `archive_object_missing`（形状 ≠ 存在，
   这一道是 T109 §10 那条"真实句柄"承诺的兑现处：`rev-parse --verify` 用的是允许名单里已有的子命令）；
④ 那一行没有 EN-14 的 `SNAPSHOT` 留痕 ⇒ `trace_missing`。**重放的起点是那条留痕的 `created_at`**，
   没有锚就不知道"其后"是从哪儿算起 —— owner 2026-10-16 裁决 2 收下那半条留痕时它还是"超出字面"，
   今天它成了 FR-064 唯一的时间锚。

**这四道之外还有 `snapshot_unknown` / `snapshot_shape` / `cards_missing` / `ambiguous_request` /
`row_lost`，全部同样发生在写之前**；`reconstruct()` 一次都不 `flush`、一次都不 `add`，
所以拒绝之后调用方的事务里连脏对象都没有（判据第一节那条"行数一张没多"钉的就是这个）。

**`rolled_back_at` 的唯一写者是 `mark_rolled_back()`**：data-model 那一列写的就是"回退也留痕"，
在此之前它是全仓一条从没被写过的列。列只容一个时间戳 ⇒ 重复回退会**覆盖**上一次的时间，
"回退过几次"这件事在 MVP-1 的 schema 里没有载体（要它就得加表或加 `event_log_kind` 域值 = 迁移单头改动
⇒ 按硬停项等一句，不自己加）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.archive import cadence, root
from acp.persistence import events, snapshots as snap
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot

#: `snapshot` 参数接受的两个写法之一。**"latest" 由 `snapshots.list_points()` 的排序决定**，
#: 不在这里再排一次序（那个排序把"拍名按域声明序"钉成了唯一口径，两处排会漂）。
LAST = "last"

#: 重放的**过滤维度 = EN-14 的 `entity`，不是 `kind`**。
#: 一开始这里抄了一份 kind 名单（`TASK / APPROVAL / ARTIFACT / REVIEW / SELECTION`），两处都不对：
#: `APPROVAL` 的 `entity` 是 `approval_ticket`（`approval/repository.py::_event`），按卡筛根本不会命中，
#: 是个死值；而 `AUTONOMY_INTERCEPT`（`guard/autonomy.py:366`）明明是"这张卡在这一刻被拦下"这件事，
#: 按名单筛就被漏掉 —— 恢复报告里少一条，读的人会以为那一轮什么都没发生。
#: 更要紧的是：名单是从 `event_log_kind` 域里**手抄的子集**，域加一个值这份抄件不会跟着变。
#: 全仓现在往 `entity="task_card"` 写的只有 5 个 kind（实测 `grep -rn 'entity="task_card"' src/acp`），
#: 用 `entity` 筛正好一次覆盖它们，且这个维度是 `events.ENTITIES` 里的既有事实，不是新抄的。
TASK_EVENT_ENTITY = "task_card"
if TASK_EVENT_ENTITY not in events.ENTITIES:  # 载体不在，本模块的筛法就失去了依据 ⇒ import 期就报
    raise RuntimeError(f"events.ENTITIES 里没有 {TASK_EVENT_ENTITY!r} ⇒ 重放的过滤维度脱钩了")


class RestoreMisuse(ValueError):
    """恢复不成立（没有点 / 句柄不实 / 对象查不出来 / 没有留痕锚 / 卡集合跨多张需求单）。

    全部发生在**一行都没写之前**，所以调用方的事务可以原样回滚或原样重试。
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ReplayedEvent:
    """一条"该点之后发生过"的任务事件。只带读数，不带 payload 原文（凭据面口径同 T105）。"""

    event_id: int
    entity_id: str
    kind: str
    at: dt.datetime


@dataclass(frozen=True)
class Restoration:
    """从某个存档点重算出来的执行态事实。"""

    snapshot_id: uuid.UUID
    project_key: str
    round_no: int
    phase: str
    ref: str
    sha: str
    archive_root: Path
    feature_request_id: uuid.UUID
    anchor_at: dt.datetime
    cards_at_point: tuple[str, ...]
    card_statuses: tuple[tuple[str, str], ...]
    replayed: tuple[ReplayedEvent, ...]
    #: 恢复时读到的 EN-01 状态。图那侧的 `_normalize` 会按 `feature_request_id` 从库里重取，
    #: 这一格是为了让 CLI 打出来的那行能自证"需求单还在、状态是什么"，不是给图用的第二份事实。
    request_status: str
    #: 恒 `False`，且没有任何路径把它设成 `True` —— 本模块不 invoke 图。
    #: 这一格存在的唯一理由是让"没整链路重跑"这句话在输出里可读，而不是只在注释里；
    #: 用例钉的是"这个值来自一条真断言，不是一句自述"（见 T112 第六节）。
    whole_chain_replayed: bool = False

    def thread_values(self) -> dict[str, object]:
        """交给装配方塞回 LangGraph thread 的**最小**值。

        刻意不含 `drafts` / `links`：那是"某一次拆单的中间产物"，已落成 EN-02 / EN-07 两表
        （`orchestrate/state.py` 的模块头就是这条原则：事实态在库里，state 只是这次运行的暂存）。
        把它们塞回去等于让恢复路径重新产出一遍拆单结果 —— 那才是 FR-064 禁的"整链路重跑"。
        """
        return {
            "feature_request_id": self.feature_request_id,
            "card_order": list(self.cards_at_point),
            "card_ids": list(self.cards_at_point),
            "snapshot_id": self.snapshot_id,
            "request_status": self.request_status,
        }

    def resumable(self, *, dispatchable: Iterable[str]) -> tuple[str, ...]:
        """**可继续调度的最小状态** = 这一轮的卡 ∩ 库里当前状态在"可下发"集合里的那些。

        `dispatchable` 由调用方交出（`orchestrate/nodes/dispatch.py::DISPATCHABLE` 是唯一载体）——
        本层不 import 编排层，所以这张集合是**借来的**，不是抄来的：抄一份的话，
        "恢复出来能调度谁"就会在 `DISPATCHABLE` 改动的那天与真实下发口径分叉。
        """
        allowed = {str(item) for item in dispatchable}
        return tuple(
            card_id for card_id, status in self.card_statuses if status in allowed and card_id in self.cards_at_point
        )

    def to_line(self) -> str:
        """一行机读读数（给 CLI 打，也给用例断）。字段名 = 本模块的口径名。"""
        return (
            f"snapshot={self.snapshot_id} round={self.round_no} phase={self.phase} "
            f"ref={self.ref} cards={len(self.cards_at_point)} replayed={len(self.replayed)} "
            f"whole_chain_replayed={str(self.whole_chain_replayed).lower()}"
        )


def _point(session: Session, *, project_key: str, snapshot: str) -> Snapshot:
    """挑出要回到的那一行。`last` 用仓储那套序，不在这儿重排。"""
    points = snap.list_points(session, project_key=project_key)
    if not points:
        raise RestoreMisuse(
            f"项目 {project_key!r} 一个存档点都没有 ⇒ 没有可回到的点（不猜一个，也不建一个）",
            code="no_points",
        )
    if snapshot == LAST:
        return points[-1]
    wanted = _as_uuid(snapshot)
    for row in points:
        if row.id == wanted:
            return row
    raise RestoreMisuse(
        f"指定的存档点 {snapshot!r} 不在项目 {project_key!r} 的可枚举清单里"
        f"（现有点数 = {len(points)}）⇒ 不照着它恢复",
        code="snapshot_unknown",
    )


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RestoreMisuse(
            f"存档点号 {value!r} 不是 uuid，也不是 {LAST} ⇒ 本入口只认这两种写法",
            code="snapshot_shape",
        ) from exc


def _anchor(session: Session, snapshot_id: uuid.UUID) -> EventLog:
    """那一行的 `SNAPSHOT` 留痕 = 重放的**时间锚**。没有它就不知道"其后"从哪算起。"""
    rows = list(
        session.scalars(
            select(EventLog)
            .where(
                EventLog.entity == "snapshot",
                EventLog.entity_id == str(snapshot_id),
                EventLog.kind == snap.SNAPSHOT_KIND,
            )
            .order_by(EventLog.id)
        )
    )
    if not rows:
        raise RestoreMisuse(
            f"存档点 {snapshot_id} 没有 EN-14 的 {snap.SNAPSHOT_KIND} 留痕 ⇒ 重放没有起点，"
            "恢复在这一步停（FR-052 那句'存档点 MUST 落盘留痕'在这里是可执行的，不是口号）",
            code="trace_missing",
        )
    return rows[-1]


def _request_of(session: Session, cards: tuple[str, ...]) -> uuid.UUID:
    """这一轮的卡归属哪张需求单；跨多张就拒（MVP-1 的 thread 号由 `(卡点类, 需求单)` 推导）。"""
    ids = {
        str(row[0])
        for row in session.execute(
            select(TaskCard.feature_request_id).where(TaskCard.id.in_(cards))
        ).all()
    }
    if len(ids) != 1:
        raise RestoreMisuse(
            f"这一轮的卡跨了 {len(ids)} 张需求单 ⇒ thread 号推不出来，不猜"
            f"（卡数 = {len(cards)}，需求单数 = {len(ids)}）",
            code="ambiguous_request",
        )
    return uuid.UUID(next(iter(ids)))


def reconstruct(
    session: Session,
    *,
    project_key: str,
    snapshot: str = LAST,
    archive_root: Path | str | None = None,
    bus_root: Path | str | None = None,
    env: dict[str, str] | None = None,
    runner: cadence.GitRunner = cadence.run_git,
) -> Restoration:
    """读 + 校验 + 重放，回 `Restoration`。**不写库、不碰图、不改状态**。"""
    if archive_root is None:
        archive_root = root.require_archive_root(project_key, bus_root=bus_root, env=env, runner=runner)
    target = Path(archive_root)

    row = _point(session, project_key=project_key, snapshot=snapshot)
    if not cadence.is_real_handle(row.ref):
        raise RestoreMisuse(
            f"存档点 {row.id} 的句柄 {row.ref!r} 不是真实句柄（T-15 的临时形态）⇒ 不能当恢复起点",
            code="ref_not_real",
        )
    sha = row.ref[len(cadence.GIT_REF_PREFIX) :]
    if not cadence.object_exists(target, sha, runner=runner):
        raise RestoreMisuse(
            f"句柄形状对，但存档根 {str(target)!r} 里查不出对象 {sha!r} ⇒ 这一行说的东西不在盘上，"
            "恢复在这一步停（宁可报错，也不在一个不存在的现场上继续调度）",
            code="archive_object_missing",
        )

    trace = _anchor(session, row.id)
    cards = tuple(str(item) for item in row.task_card_ids)
    # **这里没有"卡集合为空"那一支**，不是漏了：`snap.list_points()` 用 `task_card_ids && 该项目的卡`
    # 过滤，空数组与任何集合的 overlap 都是假 ⇒ 卡集合为空的存档点**进不到这一层**（显式点名它也会
    # 在 `_point()` 以 `snapshot_unknown` 拒掉）。留一个到不了的分支 = 让"恢复有四道拒绝"这句话里
    # 混进一条永远验不到的（口径同 owner 2026-10-12 裁决 4 删掉 `approval_state.DUPLICATED`：
    # 那个态到达不了，所以它不该在域里）。
    statuses = session.execute(
        select(TaskCard.id, TaskCard.status).where(TaskCard.id.in_(cards)).order_by(TaskCard.id)
    ).all()
    found = {str(item[0]) for item in statuses}
    missing = sorted(set(cards) - found)
    if missing:
        raise RestoreMisuse(
            f"这一轮的 {len(missing)} 张卡在 EN-02 里读不出来：{missing} ⇒ 存档点说的和库里对不上，"
            "不拿半套事实去重建执行态",
            code="cards_missing",
        )

    replay_rows = session.scalars(
        select(EventLog)
        .where(
            EventLog.entity == TASK_EVENT_ENTITY,
            EventLog.entity_id.in_(cards),
            (
                (EventLog.created_at > trace.created_at)
                | ((EventLog.created_at == trace.created_at) & (EventLog.id > trace.id))
            ),
        )
        .order_by(EventLog.created_at, EventLog.id)
    ).all()

    request_id = _request_of(session, cards)
    request = session.get(FeatureRequest, request_id)
    if request is None:
        raise RestoreMisuse(
            f"卡集合反查出的需求单 {request_id} 在 EN-01 里读不回来 ⇒ 存档点说的和库里对不上，"
            "不拿半套事实去重建执行态",
            code="row_lost",
        )

    return Restoration(
        snapshot_id=row.id,
        project_key=project_key,
        round_no=int(row.round_no),
        phase=str(row.phase),
        ref=row.ref,
        sha=sha,
        archive_root=target,
        feature_request_id=request_id,
        anchor_at=trace.created_at,
        cards_at_point=cards,
        card_statuses=tuple((str(item[0]), str(item[1])) for item in statuses),
        replayed=tuple(
            ReplayedEvent(
                event_id=int(item.id), entity_id=str(item.entity_id), kind=str(item.kind), at=item.created_at
            )
            for item in replay_rows
        ),
        request_status=str(request.status),
    )


def mark_rolled_back(session: Session, restoration: Restoration, *, now: dt.datetime) -> Snapshot:
    """把"回退到了哪一点、什么时候"落在 EN-12 那一列上 —— `rolled_back_at` 的**唯一**写者。

    只 `flush()`：事务边界归调用方（口径同 `persistence/events.py` 与 `snapshots.record_point()`）。
    """
    row = session.get(Snapshot, restoration.snapshot_id)
    if row is None:
        raise RestoreMisuse(
            f"刚读到的存档点 {restoration.snapshot_id} 在同一个会话里读不回来 ⇒ 会话被换掉了",
            code="row_lost",
        )
    row.rolled_back_at = now
    session.flush()
    return row


__all__ = [
    "LAST",
    "ReplayedEvent",
    "RestoreMisuse",
    "Restoration",
    "TASK_EVENT_ENTITY",
    "mark_rolled_back",
    "reconstruct",
]
