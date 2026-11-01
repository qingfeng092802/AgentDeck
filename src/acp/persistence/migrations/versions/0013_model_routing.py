"""迁移第十三批：`feature_request.model_routing`（路线 B，2026-10-31）。

为什么加
: 用户要求"自己指定谁负责执行/把关"，而这件选择**必须跟着需求单走**：同一次部署里，
: 上一单可能让 Qoder CLI 写代码、下一单换成纯 API 模型。存进程内存或前端 localStorage
: 都会让"这张单当时是怎么指派"在重启/换浏览器之后读不出来 —— 路由是这一单的事实，落行。
: 形状与口径的唯一载体是 `orchestrate/routing.py` 的模块说明。

为什么是 JSONB 而不是三列
: 三个席的形状不一样（执行席有 kind/vendor/model/thinking，把关席只有一个 agent_id），
: 拆成列会把"哪几列组合才合法"写成库约束，而校验已经在 API 层（pydantic）。
: 这一列只被两个读点消费：`select_worker`/`assign_guards` 的偏好与 `doubao/exec.py` 的 spec。
: **不存密钥**：它随读面回给浏览器。

NULL 的口径
: 老请求没有这一格 ⇒ 执行者照旧走四要素排序、把关席照旧从 EN-04 候选池填。
: **不把 NULL 读成"qoder 强制"** —— 那会让历史行为被一次迁移改写。

downgrade 只删列：这是一列纯偏好，删掉不改变任何状态机的走向。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "model_routing"
down_revision: str | None = "split_source_event_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "feature_request"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("model_routing", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "model_routing")
