"""迁移第十二批：EN-01 追加 `split_source` + `event_log_kind` 追加 `split_fallback`（2026-10-31）。

两处都是**可空 / 只追加**，不动既有行：

- `feature_request.split_source`（Text，可空）：这批卡是怎么来的。`NULL` = 本列上线前的历史行
  —— 那些行确实没人记过来源，**不许把它读成 `llm`**。取值域的唯一载体是
  `orchestrate/graph.py::SPLIT_SOURCES`。
- `event_log_kind` 追加 `split_fallback`：拆单回落兜底时留痕。现场是 2026-10-31 的空库真跑 ——
  `.env` 里那把豆包 key 已失效，系统静默用了 Stub 拆单（卡上判据是模板句、`meeting_note` 0 行），
  而库里与读面上都读不出"这单没接真模型"。

`ADD VALUE` 只加值、不写任何 `split_fallback` 行（写它的是应用层，在下一个事务里），
所以能跟在 `env.py` 的 `engine.begin()` 里跑完 —— 与 `0008` 同一条口径。
本批带半程守卫：库里已有该标签就跳过 ADD（救"ALTER 落成了、alembic_version 没落"的残局）。

downgrade：删列 + 把类型重建为不含新值的旧标签集（PG 没有 `DROP VALUE`）。
若库里已有 `split_fallback` 行，最后那句 `USING` 会**响亮失败**而不是悄悄改写它们。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

#: `alembic_version.version_num` 是 `varchar(32)` ⇒ revision id 必须 ≤ 32 字符。
revision: str = "split_source_event_kind"
down_revision: str | None = "approval_ticket_created_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_KIND_TYPE = "event_log_kind_type"
EVENT_KIND_COLUMN = ("event_log", "kind")
NEW_KIND = "split_fallback"

#: 本批**之前**的标签集（顺序与 `0002` 当年一致）。downgrade 重建时用它。
EVENT_KIND_OLD_LABELS: tuple[str, ...] = (
    "TASK",
    "APPROVAL",
    "ARTIFACT",
    "REVIEW",
    "SNAPSHOT",
    "GUARD_VIOLATION",
    "READONLY_VIOLATION",
    "AUTONOMY_INTERCEPT",
    "SELECTION",
)

#: 查库里那个类型**当前**实际有哪些标签（只读系统表，命名空间取 `current_schema()`，理由同 `0008`）。
_LABELS_SQL = (
    "select e.enumlabel from pg_type t "
    "join pg_enum e on e.enumtypid = t.oid "
    "join pg_namespace n on n.oid = t.typnamespace "
    "where t.typname = :type_name and n.nspname = current_schema()"
)


def _labels_of(bind, type_name: str) -> set[str]:
    return {row[0] for row in bind.execute(sa.text(_LABELS_SQL), {"type_name": type_name})}


def upgrade() -> None:
    op.add_column("feature_request", sa.Column("split_source", sa.Text(), nullable=True))
    bind = op.get_bind()
    if NEW_KIND not in _labels_of(bind, EVENT_KIND_TYPE):
        op.execute(f"ALTER TYPE {EVENT_KIND_TYPE} ADD VALUE '{NEW_KIND}'")


def downgrade() -> None:
    op.drop_column("feature_request", "split_source")
    table, column = EVENT_KIND_COLUMN
    quoted = ", ".join(f"'{label}'" for label in EVENT_KIND_OLD_LABELS)
    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text")
    op.execute(f"DROP TYPE {EVENT_KIND_TYPE}")
    op.execute(f"CREATE TYPE {EVENT_KIND_TYPE} AS ENUM ({quoted})")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {EVENT_KIND_TYPE} USING {column}::{EVENT_KIND_TYPE}")
