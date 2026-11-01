"""EN-14 `event_log` 的**单点写入**（Phase 5 的 T070 是这张表的第一处生产写者）。

为什么现在建一个共享入口，而不是在 `select_worker.py` 里就地 INSERT：`event_log_kind` 有九个取值
（`persistence/enums.py`），本轮的 `SELECTION`、Phase 8 的 `AUTONOMY_INTERCEPT`、网关侧的
`GUARD_VIOLATION` / `READONLY_VIOLATION` 都要往这张表写。各处各写一份 INSERT 的代价不是重复代码，
而是**脱敏只在一处生效** —— EN-14 的 guardrail 明写 `payload` 落库前 MUST 过 `config/loader.py` 的
`secrets` 过滤器（`models_registry.py:106` 把这句话指给了本表），所以洗法必须与写入点绑死，
不给"绕过入口自己插一行"留出第二道路径。`kind` 同理由本入口把关：不在域里的 kind 直接拒，
不等 PG 的枚举类型报错，否则错因会被读成"数据库拒绝"而看不出是调用方写错了留痕类型。

不在这里做的事：
- **不自动提交**。调用方（仓储层 / 节点）决定事务边界，留痕行跟着业务写一起走 ——
  留痕单独提交就会出现"事件已入库而指派没入库"的半程态。
- **不代写 `created_at` 的业务语义**。时刻由调用方给（与 `approval/repository.py::_event` 同一口径），
  这样测试可以钉时刻，而"选人依据的时刻"与"审批流水的时刻"两处不会一个走注入一个走系统时间。

MVP-2 起本模块**也承载读侧**（`read_since()` + `Activity`）：写的单点与读的单点分居同一张表的
同一个模块，为的是"游标口径"这件事只有一个地方能写错 —— 看板的活动流与 CLI 的 trace 读的是同一个
`read_since()`，两处各写一份 `select` 就会出现"刷新看到的顺序与不刷新看到的不一样"那一类无法归因的分歧。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.config.loader import redact_bus_text
from acp.persistence.enums import values_of
from acp.persistence.models_registry import EventLog

#: `kind` 的唯一来源是域定义，不在此重述取值（原则 I：一个事实一个载体）。
KINDS = frozenset(values_of("event_log_kind"))

#: 留痕行的"关于谁"这一列的取值。`entity` 是表名的语义位，不是自由文本 ——
#: 散着写会让 FR-051 的四要素查询变成"按字符串猜"。
#:
#: `autonomy_state`（2026-10-28 加）是 EN-16 的表名。它承载的是**部署级自主状态的改动**，
#: 例如"低风险自动验收的开关被谁打开了"。这一格的值存在 `config/runtime.yaml` 而不是 EN-16 表里
#: （它是部署参数，不是某个项目的档位），所以那一类留痕的 `entity_id` 是**文件名**而不是项目键 ——
#: 这一点在写入处（`api/autonomy.py`）写着。把它硬挂在 `feature_request` 或 `task_card` 上，
#: 会让"这张卡上发生过什么"里多出一件与它无关的事，那种脏比多一个取值更难查。
ENTITIES = (
    "task_card",
    "feature_request",
    "approval_ticket",
    "artifact",
    "snapshot",
    "autonomy_state",
)


class EventMisuse(ValueError):
    """调用方把留痕写错了形态（kind 不在域里、payload 不是映射、时刻缺失）。失败关闭，不落半行。"""


def wash(value: Any) -> Any:
    """递归过 `redact_bus_text`：JSONB 里任何深度、任何键的字符串都在落库前洗。

    只洗顶层等于没洗 —— 选人依据里最可能带出凭据的恰恰是嵌在 `candidates[].agent_id` 一层的值，
    而 guardrail 说的是"`payload` 过过滤器"，不是"payload 的第一层过过滤器"。
    """
    if isinstance(value, str):
        return redact_bus_text(value)
    if isinstance(value, Mapping):
        return {str(key): wash(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wash(item) for item in value]
    return value


def record(
    session: Session,
    *,
    entity: str,
    entity_id: str,
    kind: str,
    actor_agent_id: str,
    payload: Mapping[str, Any],
    created_at: datetime,
) -> EventLog:
    """写一行 EN-14 并 `flush()`（不 commit）。返回已带主键的实体。"""
    if kind not in KINDS:
        raise EventMisuse(f"kind {kind!r} 不在 event_log_kind 域里：{sorted(KINDS)}")
    if entity not in ENTITIES:
        raise EventMisuse(f"entity {entity!r} 不在留痕主体清单里：{ENTITIES}")
    row = EventLog(
        entity=entity,
        entity_id=entity_id,
        kind=kind,
        actor_agent_id=actor_agent_id,
        payload=wash(dict(payload)),
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class Activity:
    """EN-14 一行在**读侧**的形状（看板活动流与 SSE 推流共用，字段与列一一对应）。

    留着 `payload` 原样而不展开：留痕的载荷是逐 kind 各有形状的，读侧替它猜一层就等于
    给每种 kind 再造一个载体（原则 I）。脱敏已经在写入点做完，这里不回洗也不重洗。
    """

    id: int
    entity: str
    entity_id: str
    kind: str
    actor_agent_id: str
    payload: dict[str, Any]
    created_at: datetime


def read_since(
    session: Session,
    *,
    after_id: int = 0,
    limit: int = 50,
    entity: str | None = None,
    entity_id: str | None = None,
) -> tuple[Activity, ...]:
    """取 `id > after_id` 的最多 `limit` 行，按主键升序。游标就是主键本身，不是时刻。

    为什么游标用 `id` 而不用 `created_at`：`id` 是插入序（自增主键），而 `created_at` 是调用方
    给的业务时刻（见 `record()` 的第三条口径"时刻由调用方给"）⇒ 同一毫秒落两行时按时刻排序不稳，
    翻页就会重读或漏读一行。`run.py::_acceptance_audit` 取"最后一轮"用的也是 `order_by(EventLog.id)`，
    两侧同口径，看板与 CLI 不会读成两份顺序。

    `limit < 1` 按**误用**拒而不是当成"没限制"：把 0 读成"全部"会让一次写错的调用变成全表扫，
    而本表除 PK 外没有二级索引（`models_registry.EventLog` 的 docstring 记着这条裁定）。
    """
    if limit < 1:
        raise EventMisuse(f"limit 必须 ≥ 1（收到 {limit}）：0 不等于'不限'，本表没有二级索引")
    if entity is not None and entity not in ENTITIES:
        raise EventMisuse(f"entity {entity!r} 不在留痕主体清单里：{ENTITIES}")
    stmt = select(EventLog).where(EventLog.id > after_id).order_by(EventLog.id).limit(limit)
    if entity is not None:
        stmt = stmt.where(EventLog.entity == entity)
    if entity_id is not None:
        stmt = stmt.where(EventLog.entity_id == entity_id)
    return tuple(
        Activity(
            id=row.id,
            entity=row.entity,
            entity_id=row.entity_id,
            kind=str(row.kind),
            actor_agent_id=row.actor_agent_id,
            payload=dict(row.payload or {}),
            created_at=row.created_at,
        )
        for row in session.scalars(stmt).all()
    )
