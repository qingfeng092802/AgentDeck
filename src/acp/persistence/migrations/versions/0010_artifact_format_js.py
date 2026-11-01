"""迁移第十批：产物格式域加 `js`（owner 2026-10-29 裁决，第十一轮）。

Revision ID: artifact_format_js
Revises: phase17_review_report
Create Date: 2026-10-29

为什么加
: `ArtifactFormat` 此前只有 `html / css / md / json / py` 五值，而**页面卡写一支 `.js` 是常态**。
: 2026-10-28 的真跑里 T02 交了 `cart.js`，`bus/gateway.py::format_of()` 判 `unsupported_format`
: 把**那次写入整件**拒掉 ⇒ 一张卡直接跑砸，后面还能跑的卡也一起停。
:
: 当时的处置是把它记成"执行器乱写后缀"，那是把域缺一个值读成了模型的错：一份能被双击打开的
: 静态站，样式（css）、脚本（js）、页面（html）是三件平级的东西，少了中间那件，
: "加个购物车交互"这类需求在平台上**根本无处安放** —— 这正是 FR-017 说的"格式校验要可证伪"的
: 反面：域太窄时，"格式不符"这一支不是在拦错，是在拦对。
:
: 域一旦放宽，配套三处 MUST 一起跟上（都在本批的同一笔提交里，不在迁移里）：
: ① `prompts.ROLE_BY_FORMAT["js"] = "frontend"`（卡上没有岗位列，格式是唯一能区分的事实）；
: ② 两张 `_FLOORS` 补 `js`（`llm_split` / `reporting.cards`）；
: ③ `checks.service.scan_security` 的后缀白名单补 `.js` —— **这条最重要**：放进来一个可执行的
: 格式却不让它过静态扫描，等于凭空多一类没人看的代码。
:
: `js` **不进** `config/auto_accept.yaml`：那份清单管"哪些卡不用人签字"，而 js 会执行，
: 口径与 `py` 同档。判据 `test_js_is_never_auto_accepted`。

为什么 `ADD VALUE` 在这里是安全的
: 理由同 `0006`：PG 12+ 允许在事务块里 `ALTER TYPE … ADD VALUE`，但**同一事务内不许使用新值**。
: 本批只加值、不写 `'js'` 的行（写它的是应用层，在下一个事务里），所以能跟在 `env.py` 的
: `engine.begin()` 里跑完。把一条 `INSERT … format='js'` 塞进同一批就会炸
: （`unsafe use of new value of enum type`）。

为什么 `js` 必须追加在末尾
: `ALTER TYPE … ADD VALUE` 不带 `BEFORE`/`AFTER` 时追加到末尾，而
: `pg_enum(...).enums` 与 `values_of(...)` 的顺序在 `tests/contract/test_states.py` 里被逐位比对。
: `refs.py::ArtifactFormat` 因此也把 `js` 写在最后 —— 两处的顺序不是巧合，是同一条约束的两端。

**downgrade 为什么故意可能失败**（口径同 `0004` / `0006`）：
: 收回 `js` 只能重建类型（PG 没有 `DROP VALUE`），而任何已按 `js` 落盘的 `artifact` 行都会让最后那句
: `USING format::artifact_format_type` 报 `invalid input value for enum`。这里**不做数据改写**
: （不把 js 悄悄改成 py）：那等于把一份脚本伪造成别的格式，交付包里会多一个后缀与内容不符的文件。
: 降级要回到"没有 js"的世界，就该由人决定那批产物怎么处置。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "artifact_format_js"
down_revision: str | None = "phase17_review_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FORMAT_TYPE = "artifact_format_type"

#: 本批**之前**的标签集。downgrade 重建时用它们，顺序与 `0001` 当年一致（`html` 打头）。
FORMAT_OLD_LABELS: tuple[str, ...] = ("html", "css", "md", "json", "py")

#: 用到这个域的两列。降级时要**两列都**先挪到 text —— 只挪一列的话 `DROP TYPE` 会报
#: "cannot drop type because other objects depend on it"，卡在链中间。
#: `task_card.artifact_floor_format` 是卡自己声明的下限（EN-02），`artifact.format` 是实测（EN-06），
#: 两者同一个域是 FR-017 的立身之本（见 `refs.py` 顶部那段）。
FORMAT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_card", "artifact_floor_format"),
    ("artifact", "format"),
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
    """加值，但库里已经有它时跳过。

    守卫防的是"半程状态"：`ALTER` 落成了、`alembic_version` 没落（进程被杀、连接断）——
    没有守卫时那种库重放本批会直接报 `DuplicateObject` 卡死在链中间。判据见
    `tests/integration/test_schema_invariants.py` 里那组"手工把值加进去再 `upgrade head`"。
    """
    if value not in _labels_of(bind, type_name):
        op.execute(f"ALTER TYPE {type_name} ADD VALUE '{value}'")


def _recreate_without(type_name: str, labels: tuple[str, ...]) -> None:
    """把类型重建为 `labels`；新值仍有行在用时由最后一句 `USING` 抛错（不静默改写）。"""
    quoted = ", ".join(f"'{label}'" for label in labels)
    for table, column in FORMAT_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text")
    op.execute(f"DROP TYPE {type_name}")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
    for table, column in FORMAT_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE "
            f"{type_name} USING {column}::{type_name}"
        )


def upgrade() -> None:
    _add_value(op.get_bind(), FORMAT_TYPE, "js")


def downgrade() -> None:
    _recreate_without(FORMAT_TYPE, FORMAT_OLD_LABELS)
