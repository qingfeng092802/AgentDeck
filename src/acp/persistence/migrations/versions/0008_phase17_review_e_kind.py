"""迁移第八批：Phase 17 P1 —— EN-08 三值化 + 第五类卡点 E_REVIEW_UNCERTAIN。

Revision ID: phase17_review_e_kind
Revises: ruling_10_13_rework_escalation
Create Date: 2026-10-23

两处都是**向已有 PG 枚举追加标签**，不新增表、不改列：

- `review_verdict_type`：`APPROVE / CHANGES_REQUESTED` → 追加 `NEED_HUMAN`（owner 2026-10-23）。
- `approval_kind_type`：`A_SPLIT / B_ACCEPTANCE / C_BUDGET / D_DANGER` → 追加
  `E_REVIEW_UNCERTAIN`（Phase 17 D3，第五类卡点 = 审查不确定的人工裁决单）。

**为什么 `0002` 必须冻结标签集**：`0002` 建这两个类型时若取 `enums.py` 的活值，空库重放会在
本批的 `ADD VALUE` 处报 `DuplicateObject`（与 A-5 的 `resolution_channel/AUTO` 同形）。
本批因此与 `0006` 一样带**半程守卫**：库里已有该标签就跳过那句 ADD（救的是
"ALTER 落成了、alembic_version 没落"的中断残局）；正常空库重放在 `0002` 拿到的是当年标签集，
ADD 会真的执行。判据 = `test_upgrade_head_replays_from_an_empty_database` 与
`test_a_half_applied_auto_label_still_replays` 里新增的两段标签断言。

**`ADD VALUE` 不能与"使用新值"同事务**：本批只加值、不写任何 `NEED_HUMAN` / `E_REVIEW_UNCERTAIN`
行（写它们的是应用层，在下一个事务里），所以能跟在 `env.py` 的 `engine.begin()` 里跑完。
与 `0006` 同一条口径，不是随手挑的。

**downgrade 故意可能失败**：PG 没有 `DROP VALUE`，收回标签只能重建类型。若库里已有
`NEED_HUMAN` 的结论行或 E 类单据，最后那句 `USING …::<type>` 会报 `invalid input value for enum`
—— 这是**想要的**响亮失败，不把新值悄悄改写成别的值（那等于伪造审查结论/审批类别）。
降级前由人决定那批行怎么处置。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

#: `alembic_version.version_num` 是 `varchar(32)` ⇒ revision id 必须 ≤ 32 字符。
#: 文件名可以长，但这一格不能（实测超长时的报错是 `value too long for type character varying(32)`，
#: 且它发生在所有 DDL 之后 ⇒ 整条迁移回滚，标签一个都没留下）。
revision: str = "phase17_review_e_kind"
down_revision: str | None = "ruling_10_13_rework_escalation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEW_VERDICT_TYPE = "review_verdict_type"
APPROVAL_KIND_TYPE = "approval_kind_type"
REVIEW_VERDICT_COLUMN = ("review_verdict", "verdict")
APPROVAL_KIND_COLUMN = ("approval_ticket", "kind")

#: 各类型在本批**之前**的标签集。downgrade 重建时用它们，顺序与 `0002` 当年一致。
REVIEW_VERDICT_OLD_LABELS: tuple[str, ...] = ("APPROVE", "CHANGES_REQUESTED")
APPROVAL_KIND_OLD_LABELS: tuple[str, ...] = (
    "A_SPLIT",
    "B_ACCEPTANCE",
    "C_BUDGET",
    "D_DANGER",
)

#: 查库里那个类型**当前**实际有哪些标签。只读 `pg_type` / `pg_enum` 这一对系统表，
#: 命名空间取 `current_schema()`（理由同 `0006`：别的 schema 里同名类型会把守卫判错）。
_LABELS_SQL = (
    "select e.enumlabel from pg_type t "
    "join pg_enum e on e.enumtypid = t.oid "
    "join pg_namespace n on n.oid = t.typnamespace "
    "where t.typname = :type_name and n.nspname = current_schema()"
)


def _labels_of(bind, type_name: str) -> set[str]:
    return {row[0] for row in bind.execute(sa.text(_LABELS_SQL), {"type_name": type_name})}


def _add_value(bind, type_name: str, value: str) -> None:
    if value not in _labels_of(bind, type_name):
        op.execute(f"ALTER TYPE {type_name} ADD VALUE '{value}'")


def _recreate_without(type_name: str, table: str, column: str, labels: tuple[str, ...]) -> None:
    """把类型重建为 `labels`；新值仍有行在用时由最后一句 `USING` 抛错（不静默改写）。"""
    quoted = ", ".join(f"'{label}'" for label in labels)
    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text")
    op.execute(f"DROP TYPE {type_name}")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE "
        f"{type_name} USING {column}::{type_name}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    _add_value(bind, REVIEW_VERDICT_TYPE, "NEED_HUMAN")
    _add_value(bind, APPROVAL_KIND_TYPE, "E_REVIEW_UNCERTAIN")


def downgrade() -> None:
    _recreate_without(
        REVIEW_VERDICT_TYPE,
        *REVIEW_VERDICT_COLUMN,
        REVIEW_VERDICT_OLD_LABELS,
    )
    _recreate_without(
        APPROVAL_KIND_TYPE,
        *APPROVAL_KIND_COLUMN,
        APPROVAL_KIND_OLD_LABELS,
    )
