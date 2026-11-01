"""T108 的登记半边：EN-12 存档点的唯一写者与可枚举读面（FR-053 / FR-054｜R-14）。

`archive/cadence.py` 交回一个真 commit 对象名，本模块把那句话落成一行 EN-12 + 一条 EN-14 留痕。
分两半的理由写在那一侧的模块头（半程态要看得见）。

**为什么写者只有这一处**：T108 之前 `orchestrate/graph.py` 的 `_normalize` 自己 `session.add(Snapshot(...))`，
`ref` 写的是一句**指向登记行自身的自指**（`local:` 起头，T-15 裁 ①；2026-10-16 由 T109 换掉 ——
现在 `_normalize` 先经 `archive.cadence` 真打一拍，再拿回读的对象名调本函数）。那句自指是个占位，
`data-model.md §4` 当时明写"不得读成'某处已有一份存档'"。如果 `graph` 继续手拼 `Snapshot(...)`，
"什么算一个存档点"就有了第二处决定权。这里把写者收成一处，并且**拒绝临时句柄进新行**
（`ref_not_a_real_handle`，消息里点名 T-15）⇒ 想留一个存档点，手上就得真有一个 commit。

**两笔写、一笔事务**（EN-12 行 + EN-14 `SNAPSHOT` 流水，都只 `flush()` 不 `commit()`）：
FR-052 那句"每次交互 MUST 落盘留痕：任务、执行者、结果、审查意见、**存档点**"到这一轮才有第一处生产写者
（`event_log_kind` 里的 `SNAPSHOT` 此前是零写入的域值）。只 `flush` 的口径同 `persistence/events.py`
与 `persistence/autonomy.py`：事务边界归调用方，否则"流水已入库而存档行没入库"这种半程态就出不了门。

**`project_key` 必须进参数**：EN-12 没有项目列（`data-model.md:167` 那张表里就是没有），
"存档点可枚举"这句话要有意义，只能按项目分开数 ⇒ 本模块用卡集合反查（`task_card_ids` ∩ 该项目的卡）。
这个反查不是顺手加的便利：不带项目过滤的 `list_points()` 在多项目库里会把 A 项目的轮次算进 B 项目，
`next_round_no()` 就会凭空跳号，而 `revert 可复原` 会挑到别的项目的现场。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.archive import cadence
from acp.persistence import events
from acp.persistence.autonomy import PLATFORM_PRINCIPAL
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot

#: 留痕类型（`event_log_kind` 的取值之一）。写的是**调用参数**而不是第二份取值清单，
#: 载体仍在 `persistence/enums.py`；不在域里 `events.record()` 自己就抛（口径同 T105 的拦截留痕）。
SNAPSHOT_KIND = "SNAPSHOT"

#: 本模块落的 `snapshot` 行的列。**这是 AST 用例的对照物**：新增列要一起进这张名单，
#: 否则"写者只碰这几列"这句话会变成一句没人核对的注释。
SNAPSHOT_OWNED_COLUMNS = ("id", "round_no", "phase", "task_card_ids", "ref", "rolled_back_at")

#: 留痕 payload 的键集合（同样按集合钉，多一个少一个都红）。
SNAPSHOT_PAYLOAD_KEYS = ("phase", "round_no", "ref", "sha", "project_key", "task_card_ids", "commit_message")


class SnapshotMisuse(ValueError):
    """调用方交来的形状不成立（句柄是临时形态、拍名不在域里、卡集合空）。全部发生在写之前。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def _card_ids_of(session: Session, project_key: str) -> list[str]:
    """该项目现有任务卡的卡号集合（`snapshot` 反查项目的唯一途径，见模块头）。"""
    rows = session.execute(
        select(TaskCard.id)
        .join(FeatureRequest, TaskCard.feature_request_id == FeatureRequest.id)
        .where(FeatureRequest.project_key == project_key)
    ).all()
    return [str(row[0]) for row in rows]


def record_point(
    session: Session,
    *,
    project_key: str,
    round_no: int,
    phase: str,
    task_card_ids: Sequence[str],
    ref: str,
    now: dt.datetime | None = None,
) -> Snapshot:
    """落一行 EN-12 存档点 + 一条 EN-14 `SNAPSHOT` 留痕。返回存档点行（未提交，归调用方）。

    - **`ref` MUST 是真实句柄**：`local:` 起头的一律拒（`ref_not_a_real_handle`）。这一条就是 T-15 那句
      "T108 落地时必须换成真实句柄"在代码里的落点 —— 它拦的是**新的写入**，不去改写既有那一行
      （`graph.py` 的临时登记行是历史时点的产物，抹平它就等于假装它没存在过）。
    - **`rolled_back_at` 一律写 `None`**：回退留痕归 T110（`--from-snapshot`），本模块不预留那个入口，
      否则"谁写过这一列"在 T110 落地之前会有一个假的第二个写者。
    """
    if phase not in cadence.PHASES:
        raise SnapshotMisuse(
            f"拍点名 {phase!r} 不在 snapshot_phase 域里：{list(cadence.PHASES)}", code="phase_domain"
        )
    if ref.startswith(cadence.TEMPORARY_REF_PREFIX):
        raise SnapshotMisuse(
            f"句柄 {ref!r} 是 T-15 的临时形态（指向登记行自身）⇒ 新写的存档点必须指向一个真 commit 对象",
            code="ref_not_a_real_handle",
        )
    if not cadence.is_real_handle(ref):
        raise SnapshotMisuse(
            f"句柄 {ref!r} 不是本机制认得的真实句柄（应是 {cadence.GIT_REF_PREFIX}<sha> 形态）"
            " ⇒ 一行都不落：宁可调用方去把 commit 补上",
            code="ref_not_a_real_handle",
        )
    if round_no < 1:
        raise SnapshotMisuse(f"轮次序号从 1 起（收到 {round_no}）", code="round_no")
    cards = [str(item).strip() for item in task_card_ids]
    if not cards or any(not item for item in cards) or len(set(cards)) != len(cards):
        raise SnapshotMisuse("本轮任务集合为空 / 含空白项 / 含重复卡号 ⇒ 先修调用方", code="cards_shape")
    message = cadence.commit_message(
        phase=phase, round_no=round_no, project_key=project_key, task_card_ids=cards
    )

    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=round_no,
            phase=phase,
            task_card_ids=cards,
            ref=ref,
            rolled_back_at=None,
        )
    )
    events.record(
        session,
        entity="snapshot",
        entity_id=str(snapshot_id),
        kind=SNAPSHOT_KIND,
        actor_agent_id=PLATFORM_PRINCIPAL,
        payload={
            "phase": phase,
            "round_no": round_no,
            "ref": ref,
            "sha": ref[len(cadence.GIT_REF_PREFIX) :],
            "project_key": project_key,
            "task_card_ids": cards,
            "commit_message": message,
        },
        created_at=moment,
    )
    # 两笔都只 `flush`（在调用方的事务里）：流水跟着存档行一起走或一起消失。
    session.flush()
    return _require(session, snapshot_id)


def _require(session: Session, snapshot_id: uuid.UUID) -> Snapshot:
    row = session.get(Snapshot, snapshot_id)
    if row is None:
        raise SnapshotMisuse(
            "刚 add 的存档点行读不回来 ⇒ 会话被换掉了（存档点的读面与写面必须在同一个工作单元里）",
            code="row_lost",
        )
    return row


def list_points(session: Session, *, project_key: str) -> list[Snapshot]:
    """按项目列出存档点，顺序 = `(round_no ↑, 拍名按域声明顺序, id ↑)`。

    为什么拍序不用 SQL 的 `ORDER BY phase`：PG 枚举按 OID 排，不保证等于 `snapshot_phase` 在代码里
    声明的顺序 ⇒ "开工前在完工前"这个语义会从排序里掉出去。这里按域下标排，与库里怎么建的枚举类型无关。
    """
    cards = _card_ids_of(session, project_key)
    if not cards:
        return []
    rows = list(session.scalars(select(Snapshot).where(Snapshot.task_card_ids.overlap(cards))))
    order = {str(name): index for index, name in enumerate(cadence.PHASES)}
    return sorted(rows, key=lambda row: (int(row.round_no), order.get(str(row.phase), len(order)), str(row.id)))


def next_round_no(session: Session, *, project_key: str) -> int:
    """该项目下一轮的序号 = 已有轮次去重计数 + 1（与 `nodes/review.py:219` 同形）。

    没有行 ⇒ 1。这里数的是**轮**不是**拍**：一轮有开工前与完工后两拍，按行数就会每轮加二。
    """
    used = {int(row.round_no) for row in list_points(session, project_key=project_key)}
    return len(used) + 1
