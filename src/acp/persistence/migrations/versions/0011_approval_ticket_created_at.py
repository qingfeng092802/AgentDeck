"""迁移第十一批：`approval_ticket.created_at`（P1-4，2026-10-30）。

Revision ID: approval_ticket_created_at
Revises: artifact_format_js
Create Date: 2026-10-30

为什么加
: A 类卡点按宪法是**无超时**（`timeout_at` 为 `NULL`），于是"这张单挂了多久"在库里
: **一个可算的起点都没有**。看板要能红着说"已挂起 N 天"，就必须先有一个"它什么时候挂上"。
: 没有这一列时唯一能拿来近似的是 `feature_request.created_at`，但那回答的是"需求什么时候提的"，
: 不是"这张单什么时候等人" —— 一张单可能在需求提出三天后才因为重拆产生，两者不是同一件事。
:
: 这也解释了为什么不复用 `timeout_at`：那一列对 A/C/D 三类的语义是"没有可算的时刻"，
: 拿它当起点就是把"无超时"重新写成一个哨兵值 —— 正是 owner 2026-10-13 裁决 ② 否掉的形状。

为什么不是"从 EN-10 反推"
: `resolution_event.arrived_at` 只在**有人裁过**之后才有行；未决单恰恰没有。用一个
: "结论到达时刻"去推"开始等待的时刻"，在未决单上恒空，在已决单上恒等于结论时刻 ——
: 两档都错，且错得很安静。

为什么带 `server_default=now()`
: 这是"行被建出来的时刻"，DB 自己知道，不需要每个写入点都记得传。迁移期也不必为历史行编一个
: 看似有理的假时刻 —— 下面那段 `UPDATE` 优先用需求单的创建时间回填（那是**已知**的下界），
: 实在没有才落到 `now()`。默认值留在库上，比留在某个 ORM 构造点更不容易漏。

**downgrade 只删列**：这一列是**新增的观测面**，删掉不改变任何状态机的走向（`state` 那一列
才是权威）。所以降级不需要数据改写，也不会有"降完起不来"的形状。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "approval_ticket_created_at"
down_revision: str | None = "artifact_format_js"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "approval_ticket"

#: 历史行回填：需求单的创建时间是**已知的下界**（卡点不可能早于需求单）；没有需求单的
#: （D 类挂卡、E 类挂审查）退到"最早一次结论到达"，再退到 `now()`。
#: 三档依次收窄，不用一个常量糊过去 —— 回填值会被看板读成"挂了多久"，编一个就是编一个假读数。
BACKFILL_SQL = """
UPDATE approval_ticket AS t
SET created_at = COALESCE(
    (SELECT f.created_at FROM feature_request AS f WHERE f.id = t.feature_request_id),
    (SELECT min(e.arrived_at) FROM resolution_event AS e WHERE e.approval_ticket_id = t.id),
    now()
)
WHERE t.created_at IS NULL
"""


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(BACKFILL_SQL)
    op.alter_column(
        TABLE,
        "created_at",
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.drop_column(TABLE, "created_at")
