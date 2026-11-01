"""迁移第六批：owner 2026-10-13 Phase 4 收口的**裁决 ② + 裁决 ④**（两条一起走一笔）。

Revision ID: ruling_2_4_timeout_auto
Revises: t050_checkpoints
Create Date: 2026-10-13

裁决 ②（`approval_ticket.timeout_at` 改可空）
: Phase 4 的第一版把 §1 的"无超时"落成 `timestamptz` 的**上界哨兵** `9999-12-31`，
: 因为那一列当时是 NOT NULL。owner 明确否掉："哨兵值是反模式；nullable 是 SQL 原生语义；
: Phase 1.3 已有 nullable 先例（就是 `0004_t014_nullable_executor`）。
: 本批把列放宽，并**把已经按哨兵写进去的行改回 NULL** —— 不改这一步的话，旧库里会留下一批
: "看起来是一个极远的时刻、意思是根本没有时刻"的行，那是把一次口径变更藏进数据里。
:
: **换来的语义**：超时扫描的 `WHERE timeout_at <= now()` 在 NULL 上**恒不命中**（SQL 三值逻辑），
: 于是"无超时"由数据库原生表达，不再依赖一个需要人记住的魔法数。判据是
: `tests/integration/test_approval_ticket.py::test_a_no_timeout_ticket_stores_sql_null`，
: 它断的正是 `timeout_at <= now()` 读回 `NULL` 而不是 `false` —— 这两种形状在"扫描会不会捞到它"上等价，
: 但在"这份缺席是不是一等事实"上不等价：`false` 是"算过了没到期"，`NULL` 是"没有可算的时刻"。

裁决 ④（`resolution_channel` 加第三个值 `AUTO`）
: 免确认（FR-046/FR-047）此前**不写 EN-10 到达流水**，理由是这个域只有 `DASHBOARD / CLI`，
: 硬塞一个就是拿假事实填留痕面（登记在 `evidence/phase-04.md §T060` 未闭段）。
: owner 裁："免确认也是一种 resolution；审计需要区分'人工批准'和'自动免确认'"。
: 自此 EN-10 有 `AUTO` 这一通道，`apply_auto_pass()` MUST 写一行 `applied=true` 的流水。
:
: **`AUTO` 不是人可以提交的通道**：它是"没有人在敲命令"这件事的载体。若 `resolve()` 接受它，
: 任何拿到 `ticket_id` 与身份的调用者都能把自己的结论标成"规则表自动批的"，
: 那是把审计面变成可伪造的。拦它的是 `repository.resolve()` 的通道白名单（只放 `DASHBOARD / CLI`），
: 判据 `test_a_human_caller_cannot_submit_the_auto_channel`。

**为什么 `ADD VALUE` 在这里是安全的**：PG 12+ 允许在事务块里执行 `ALTER TYPE ... ADD VALUE`，
但**同一个事务内不许使用新值**。本批只加值、不写 `'AUTO'` 的行（写它的是应用层，在下一个事务里），
所以能跟在 `env.py:37` 的 `engine.begin()` 里跑完。这不是随手挑的：把它和一条
`INSERT ... channel='AUTO'` 放同一批就会炸（`unsafe use of new value of enum type`）。

**downgrade 为什么故意可能失败**（口径同 `0004` 的"downgrade 为什么故意会失败"）：
收回 `AUTO` 只能重建类型（PG 没有 `DROP VALUE`），而任何已按 `AUTO` 落盘的流水都会让最后那句
`USING channel::resolution_channel_type` 报"invalid input value for enum"。
这里**不做数据改写**（不把 AUTO 悄悄改成 DASHBOARD）—— 那等于伪造审计来源；
降级要回到"免确认不留痕"的世界，就该由人决定这批流水怎么处置。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ruling_2_4_timeout_auto"
down_revision: str | None = "t050_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Phase 4 第一版写进库的那个上界哨兵。只在本批的 UPDATE 里作为"要被改回 NULL 的那批行"的判据出现，
#: 产品代码里已经没有它（`timeouts.NEVER_TIMES_OUT` 随裁决 ② 删除）。
SENTINEL = "9999-12-31T23:59:59.999999+00:00"

CHANNEL_TYPE = "resolution_channel_type"
HUMAN_VALUES = ("DASHBOARD", "CLI")

#: 查库里那个类型**当前**实际有哪些标签。只读 `pg_type` / `pg_enum` 这一对系统表，
#: 不依赖任何驱动专有 API；命名空间取 `current_schema()`，与本批其它语句同一目标 schema
#: （不这么写的表现是：别的 schema 里同名类型会把守卫判成"已有 AUTO"而悄悄跳过加值）。
_LABELS_SQL = (
    "select e.enumlabel from pg_type t "
    "join pg_enum e on e.enumtypid = t.oid "
    "join pg_namespace n on n.oid = t.typnamespace "
    "where t.typname = :type_name and n.nspname = current_schema()"
)


def _labels_of(bind, type_name: str) -> set[str]:
    return {row[0] for row in bind.execute(sa.text(_LABELS_SQL), {"type_name": type_name})}


def upgrade() -> None:
    # 1) 加值（不在此事务内使用它，见模块头）。
    #    **守卫（owner 2026-10-14 裁决 1 的动作 3"两者都做更稳"）**：库里已经有 `AUTO` 时跳过这句。
    #    它防的是"半程状态"：`0002` 已按当年两值冻结之后，正常空库重放走不到这一支（那时标签集里没 AUTO，
    #    守卫恒为真、照旧 ADD）；真正会被它救回来的是"0006 的 ALTER 落成了、alembic_version 没落"
    #    那类中断残局 —— 没有守卫时那种库重放本批会直接报 `DuplicateObject` 而卡死在链中间。
    #    判据：`test_a_half_applied_auto_label_still_replays`（把库摆成那个半程态再 `upgrade head`）。
    if "AUTO" not in _labels_of(op.get_bind(), CHANNEL_TYPE):
        op.execute(f"ALTER TYPE {CHANNEL_TYPE} ADD VALUE 'AUTO'")


    # 2) 放宽 NOT NULL。
    op.alter_column(
        "approval_ticket",
        "timeout_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        nullable=True,
    )

    # 3) 把哨兵改回 NULL：口径变更必须落到数据上，否则旧库里留下一批"魔法数"。
    op.execute(
        f"UPDATE approval_ticket SET timeout_at = NULL WHERE timeout_at >= '{SENTINEL}'::timestamptz"
    )


def downgrade() -> None:
    # 3') 反向：NULL 收回成哨兵（这是本批之前的形状，不假装它是"真时刻"）。
    op.execute(
        f"UPDATE approval_ticket SET timeout_at = '{SENTINEL}'::timestamptz "
        "WHERE timeout_at IS NULL"
    )
    op.alter_column(
        "approval_ticket",
        "timeout_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        nullable=False,
    )

    # 1') 重建类型去掉 AUTO：PG 不能 DROP VALUE。库里若已有 AUTO 流水，最后一句会响亮地失败。
    quoted = ", ".join(f"'{value}'" for value in HUMAN_VALUES)
    op.execute("ALTER TABLE resolution_event ALTER COLUMN channel TYPE text")
    op.execute(f"DROP TYPE {CHANNEL_TYPE}")
    op.execute(f"CREATE TYPE {CHANNEL_TYPE} AS ENUM ({quoted})")
    op.execute(
        "ALTER TABLE resolution_event ALTER COLUMN channel TYPE "
        f"{CHANNEL_TYPE} USING channel::{CHANNEL_TYPE}"
    )
