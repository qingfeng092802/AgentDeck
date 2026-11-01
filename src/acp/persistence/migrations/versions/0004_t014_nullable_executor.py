"""迁移第四批：T-14 候选 ① —— `task_card.executor_agent_id` / `executor_role_id` 改可空。

Revision ID: t014_nullable_executor
Revises: t025_registry
Create Date: 2026-10-12

**为什么是新 revision 而不是就地改 `0001`**：owner 2026-10-12 的口径是"链已出库就加新 revision"。
`t023_core` 已随 `9c2b195` 提交；就地改写会把一段**已经存在过**的历史抹成"从来没错过"，而这次改动
本身是裁决的产物（有明确的前后两态），留一份显式 revision 才是可审计的。

**upgrade 为什么安全**：放宽 NOT NULL 不会让任何既存行变得非法。
**downgrade 为什么故意会失败**：收回 NOT NULL 时，任何 `executor_agent_id IS NULL` 的既存卡都会撞约束。
这里**不做数据补全**（不填占位工具、不删行）——"有未指派的卡"正是本次裁决买来的一种合法状态，
降级要回到没有这种状态的世界，就该由人决定怎么处置这些卡，而不是由迁移脚本替人编一个 executor。

**命名按 `data-model.md §5` 的待办号 T-14，不按任务号**：`tasks.md` 里没有任何任务拥有"EN-02 建行"
这一步（那正是 T-14 登记的空缺），拿一个不存在的任务号命名会被读成"有这条任务"。

本 revision 不引入新的枚举域，所以没有 `ENUM_DOMAINS` 与 `DROP TYPE` 那一段（见 `0001` 的同名说明）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t014_nullable_executor"
down_revision: str | None = "t025_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "task_card", "executor_agent_id", existing_type=sa.Text(), nullable=True
    )
    op.alter_column(
        "task_card", "executor_role_id", existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    op.alter_column(
        "task_card", "executor_agent_id", existing_type=sa.Text(), nullable=False
    )
    op.alter_column(
        "task_card", "executor_role_id", existing_type=sa.Text(), nullable=False
    )
