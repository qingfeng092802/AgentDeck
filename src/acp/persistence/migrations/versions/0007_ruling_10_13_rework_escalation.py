"""迁移第七批：owner 2026-10-14 Phase 7 收口裁决的**待裁 ⑩⑪⑫⑬ 四条合并成一笔**。

Revision ID: ruling_10_13_rework_escalation
Revises: ruling_2_4_timeout_auto
Create Date: 2026-10-14

**四条里只有 ⑬ 带 schema，其余三条为什么也写进本批而不是各走各的**：owner 的原话是
"把这 4 条合并成一笔迁移 0007 + 状态机同步"。合并的理由不是省事 —— 这四条改的是**同一张状态机的
四个不同边**，分开落会出现"库里已有 `rework_count` 而 `REWORK` 还不可派发"这种半套世界：
那种中间态既不违反任何一条判据，又和签批文档不一致，正是最难查的那类漂移。

- **⑩ `REWORK ──▶ RUNNING` 开**（owner 理由逐字："返工后要能回到执行态，否则状态机断头"）。
  `data-model.md:301` 那一条边**一直就写着**，缺的是应用层 `dispatch.DISPATCHABLE` 里没有 `REWORK`。
  无 schema：`task_card_state_type` 的 15 个标签里本来就有 `REWORK`（`test_schema_invariants.py` 钉的
  就是"库里标签与 §2 全集逐字相同"），加边不增态。
- **⑪ 冲突裁决者含自身 ⇒ 相关卡进 `ESCALATED`**（沿用宪法原则 III"相关方不得自裁"）。
  无 schema：`ESCALATED` 也在那 15 个标签里。改的是**入边集合** —— 此前它只有一条入边（`handoff` 的两条
  上限超限），现在多一条"冲突裁决人就是冲突任一方"。这条新边必须同步进 `data-model.md §2` 的图，
  否则 `escalation.py` 的出口锚点判据与状态机文档就不是同一份事实了。
  **同步那一行刻意不含"人工"二字**：`escalation.exit_line_in_doc()` 要求"带「人工」的 `ESCALATED` 边
  恰有一条"，新边若也带这两个字，锚点立刻变成 2 条命中 ⇒ 判据红（这不是巧合，是那条判据在替文档守
  "一条边只有一个来源"这件事）。
- **⑫ 人工出口走独立路由**（owner 理由逐字："escalation.py 已做，与 PATCH 的语义不同"）。
  无 schema：这是 HTTP 面 + 契约面（`contracts/approval-api.md` 的公开路由清单加一条）。
- **⑬ `review_round` 拆两列** —— 本批唯一的 schema 动作：
  `review_round` = **验收轮次**（受宪法原则 III 硬值约束），`rework_count` = **返工次数**（无上限，
  也不计成换人）。此前一列两用登记在 `evidence/phase-07.md` 未闭 ⑤ 与 `accept.py` 模块头。

**`rework_count` 为什么带 `server_default '0'`**：`0` 不是占位符，它就是"这张卡还没返过工"这个事实的
值；不带默认的话这一列在旧行上无处安放，只能再补一条 `UPDATE` —— 而那条 UPDATE 写的还是同一个 0。
与 `handoff_count` / `review_round`（无默认、由建行方显式给）不同，那两列在拆单时就被赋了值，
而本列是**给已有表加历史**，二者形态不一样，不必强行对齐。

**downgrade 是有损的，这里明写不假装可逆**：`DROP COLUMN rework_count` 会把已经计过的返工次数一并带走，
且没有任何地方能把它恢复回来（返工这件事在 `event_log` 里留了痕，但那是流水不是计数列）。
所以降级读到的世界是"返工次数从没被计过"。口径与 `0004` / `0006` 那两批不同：它们故意让降级在
有数据时**响亮失败**逼人做决定，本批的降级不会失败 —— 因为库里这一列只有整数、没有枚举，
删掉它不撞任何约束。**代价点名**：这意味着"降级成功"在本批**不等于**"无损"，读的人要看这一句而不是看退出码。

本 revision 不引入新的枚举域，所以没有 `ENUM_DOMAINS` 与 `DROP TYPE` 那一段（口径同 `0004`）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ruling_10_13_rework_escalation"
down_revision: str | None = "ruling_2_4_timeout_auto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ⑬ 唯一一条 schema 动作。NOT NULL + server_default '0' ⇒ 已有行（若有）自动落在"没返过工"上。
    op.add_column(
        "task_card",
        sa.Column("rework_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # 反向就一句。它一定成功，也一定丢东西 —— 见模块头"downgrade 是有损的"那一段。
    op.drop_column("task_card", "rework_count")
