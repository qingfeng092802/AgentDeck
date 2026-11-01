"""迁移第五批：checkpoint schema —— D-9 的 PG checkpointer 落点（T050）

Revision ID: t050_checkpoints
Revises: t014_nullable_executor
Create Date: 2026-10-13

**为什么这一批不写 `CREATE TABLE` 的字面量**：这四张表的结构归 `langgraph-checkpoint-postgres`
自己（它是 writer，我们只是宿主），抄一份 DDL 进迁移就制造了"升级依赖而 schema 不跟着变"的
静默漂移。所以本文件遍历 `acp.persistence.checkpointer.SCHEMA_STATEMENTS`，
而那个名字**就是** `langgraph.checkpoint.postgres.base.MIGRATIONS` 的别名（同一个对象、同一份列表）。

**唯一一处改写：`CREATE INDEX CONCURRENTLY` → `CREATE INDEX`**。原因是环境的硬约束而不是偏好：
`migrations/env.py:37` 用 `engine.begin()` 把整个 revision 包在一条事务里，PG 不允许
`CREATE INDEX CONCURRENTLY` 在事务块内跑（实测：alembic 先报 `AssertionError` 在
`autocommit_block()` 上，因为事务已经归 env.py 持有）。为这一条去改 `env.py` 的事务模型，
blast radius 是整条迁移链，不在本笔的范围。
收益侧也是零：这三张表**就在这一次迁移里刚建出来**，没有并发读者，CONCURRENTLY 买不到任何东西。
代价点名：改写的是语句文本，所以配一道"索引名集合必须与库那份列表声明的一致"的判据
（`tests/integration/test_approval_ticket.py::test_the_migrated_schema_matches_what_the_library_declares`）
—— 漏建一个索引要红，而不是悄悄少一个。

**与 `ensure_checkpoint_schema()` 的分工**（两遍同一列表不是重复劳动）：
- 本迁移管"从空库能建起来"：Checkpoint 1 那句"17 实体迁移可跑"的含义是可重放，
  如果 checkpoint 表只能靠运行期补，那 `alembic upgrade head` 之后的库就是个半拉子世界。
- `ensure_checkpoint_schema()` 管"库已在 head、依赖版本涨了"的那一刻：列表里新增一条时
  旧库不会自动重放 0005，那条就永远缺 —— 同一文件里那条 schema 判据正是为这一刻准备的
  （它按库的当前列表逐表逐列核对 `information_schema`）。

**downgrade 是 DROP TABLE**：这四张表里没有业务事实态（宪法 原则 I 的"唯一事实态"是 EN-01 ~ EN-17），
挂起现场属于过程态，降级把它清掉不丢任何可恢复的事实；这与 `0004` 那种"降级故意会失败"的表不同。
本批不引入新的枚举域，所以没有 `ENUM_DOMAINS` 与 `DROP TYPE` 那一段（见 `0001` 的同名说明）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from acp.persistence.checkpointer import SCHEMA_STATEMENTS

revision: str = "t050_checkpoints"
down_revision: str | None = "t014_nullable_executor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 降级要撤的表（顺序 = 依赖反向）。写死在这里是因为降级必须可预测：
#: 列表涨了而这份没跟上 ⇒ 由上面点名的那条判据红，而不是留下一张没人认领的表。
DROP_ORDER: tuple[str, ...] = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


def upgrade() -> None:
    if not SCHEMA_STATEMENTS:
        raise RuntimeError("langgraph 的 MIGRATIONS 列表是空的 ⇒ 无从建表，别把'没有语句'当成通过")
    for statement in SCHEMA_STATEMENTS:
        op.execute(statement.replace("CONCURRENTLY ", ""))


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
