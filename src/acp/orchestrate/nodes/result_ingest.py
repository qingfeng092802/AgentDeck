"""T076 的上游节点：`ResultIngest` 把回收到的 `TaskResult` 落成 EN-06 行（FR-017 的"结果回传后"那一瞬）。

这一层只干一件事，而且**故意干得很少**：把网关实测的三个值（`bytes` / `lines` / `format`）
与工具的自评（`self_reported_status`）**并排记下**，然后就走。它不做判定 ——
`validated_status` 一律留 `NULL`，因为 FR-019 要的是"以产物实况为准"，而实况的判定权在
下一节点 `ValidateArtifact`。若这里顺手把 `valid` 填上，门禁就变成"回收方自己盖章"，
`tests/guards/test_artifact_gate.py` 那条"自评 success + 空产物 ⇒ `invalid`"会永远读不到门禁。
表列的可空性也是同一口径（`models_core.py:172`：建成 NOT NULL 就等于允许写入方先填一个 `valid` 蒙过门禁）。

三条形状上的取舍，点名在这里免得被读成"随便写的"：

1. **`writer_agent_id` 取的是卡上的 `executor_agent_id`，不是调用方报的那一个**
   （`data-model.md` EN-06 那行："等值于该任务 executor；只读岗写它 = 违规"，FR-033）。
   调用方如果传了一个不等于执行者的 `actor_agent_id`，那是有人替别人记账 ⇒ 直接拒，
   不"以卡为准"悄悄改成执行者 —— 后者会把冒充读成正常。
2. **前置状态要 `RUNNING`**（"结果回传"这个动作只在任务跑着的时候成立）。
   别的状态来回传就是第二次回收：EN-06 没有"每卡只一行"的约束，放任它 = 同一张卡攒一串来历不明的产物。
3. **一卡多产物照单全收**，不在这里挑"主产物"。EN-06 没有 `is_primary` 这类列，
   挑一个就是发明一列；"多项产物怎么聚合判定"的口径在 `validate_artifact.py`，那里写清了选择与代价。

`log_excerpt` 的脱敏**不在本模块**：过滤器在 `bus/gateway.py`（产物面）与 `logging` 的 `secrets` 过滤器（日志面），
协议层是裸模型（`contracts/agent-protocol.md §2` 那段"回传面 1 洗 / 2 未洗"仍未收口，归 owner 裁决）。
本模块因此**不写 `log_excerpt` 到任何地方** —— EN-06 没有那一列，落库面一通就多了第二条未经洗的路径。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from acp.bus import acl
from acp.cost import ledger
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.protocol.messages import TaskResult

NODE = "result_ingest"

#: 唯一允许被本节点回收的卡态（"任务正在跑"）。
INGESTABLE = "RUNNING"


class IngestMisuse(ValueError):
    """回收这件事的形状不成立（卡不存在 / 没执行者 / 状态不对 / 有人替别人记账 / 结果对不上卡）。"""

    def __init__(self, message: str, *, code: str = "ingest_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Ingested:
    """本次回收录了几行产物、记了几笔账，以及入账后的预算读数。

    判定结果不在这里（本节点无权判定），调用方要拿判定就得走 `validate_artifact`。
    `usage` 为 `None` 表示**这一发没有用量可记**（`result.usage` 是空列表），不是"记失败了下发 None"。
    """

    task_id: str
    artifact_ids: tuple[uuid.UUID, ...]
    usage: ledger.LedgerRun | None = None

    @property
    def count(self) -> int:
        return len(self.artifact_ids)


def _card_of(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    """按**复合主键**取卡（`(feature_request_id, id)`，owner 裁决 2）——只按卡号取会串到别的需求单上。"""
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise IngestMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 产物无处可挂")
    return card


def _project_key_of(session: Session, feature_request_id: uuid.UUID) -> str:
    """EN-13 的 `project_key` 取需求单上那一个（账要能按项目查，第二份项目归属=自造）。"""
    key = session.query(FeatureRequest.project_key).filter(FeatureRequest.id == feature_request_id).one_or_none()
    if key is None:
        raise IngestMisuse(
            f"需求单 {feature_request_id} 读不出来 ⇒ 用量没有项目归属，记成哪一项目都是猜",
            code="project_missing",
        )
    return str(key[0])


def record_result(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    result: TaskResult,
    actor_agent_id: str,
    created_at: dt.datetime,
) -> Ingested:
    """把一次回收写成 EN-06 行，并在**同一事务**里把 `result.usage` 记进 EN-13；**只 flush 不 commit**
    （工作单元归调用方，同 `select_worker.apply_selection`）。"""
    if result.task_id != task_id:
        raise IngestMisuse(
            f"结果写的是任务 {result.task_id}，却要挂到卡 {task_id} 上 ⇒ 回收面与卡对不上（不许靠调用方参数撮合）"
        )
    card = _card_of(session, feature_request_id, task_id)
    if card.status != INGESTABLE:
        raise IngestMisuse(
            f"卡 {task_id} 现在是 {card.status}，不是 {INGESTABLE} ⇒ 这一次回收没有对应的在跑任务",
            code="task_not_running",
        )
    executor = card.executor_agent_id
    if executor is None:
        raise IngestMisuse(
            f"卡 {task_id} 还没有执行者（`SelectWorker` 未填 `executor_agent_id`）⇒ 产物无从归属",
            code="executor_missing",
        )
    if actor_agent_id != executor:
        raise IngestMisuse(
            f"回收方报的是 {actor_agent_id}，而 {task_id} 的执行者是 {executor} ⇒ 记账人必须等于执行者（FR-033）",
            code="writer_not_executor",
        )

    ids: list[uuid.UUID] = []
    for ref in result.artifacts:
        row = Artifact(
            id=uuid.uuid4(),
            feature_request_id=feature_request_id,
            task_card_id=task_id,
            writer_agent_id=executor,
            rel_path=ref.rel_path,
            bytes=ref.bytes,
            lines=ref.lines,
            format=ref.format,
            self_reported_status=result.self_reported_status,
            validated_status=None,
            invalid_reason=None,
            created_at=created_at,
        )
        session.add(row)
        ids.append(row.id)
    session.flush()
    # T114：产物落库之后，在**同一个事务、同一次 flush 语义**里把用量记进 EN-13（FR-059「每次调用 MUST 记账」）。
    # 空 `usage` 不调用 ⇒ `Ingested.usage` 留 `None`，与"调了但记了 0 行"是两种读数，别让报表把它们读成同一件事。
    run: ledger.LedgerRun | None = None
    if result.usage:
        run = ledger.record_usage(
            session,
            project_key=_project_key_of(session, feature_request_id),
            task_card_id=task_id,
            agent_id=executor,
            role_id=card.executor_role_id or acl.NO_ROLE_DECLARED,
            notices=result.usage,
            feature_request_id=feature_request_id,
        )
    return Ingested(task_id=task_id, artifact_ids=tuple(ids), usage=run)
