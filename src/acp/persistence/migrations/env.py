"""Alembic 运行环境（T023）。

**DSN 只从环境变量取**（宪法「二」）：`alembic.ini` 里没有 `sqlalchemy.url`，读取与驱动归一化
统一在 `acp/config/loader.py::db_url`（T027）——迁移与用例共用一条路径，两处走成两个口径的
表现是"测试连得上、迁移报缺 psycopg2"。这里**不提供**"读不到就从文件里找"的回退：
有回退就等于承认凭据可以落文件。
复跑方式见 `quickstart.md`（`set -a; . ./.env; set +a` 后跑 `alembic upgrade head`；
MUST NOT 开 `set -x`，那会把整条 DSN 打进终端）。

不做 `fileConfig()` 日志初始化：迁移只需要一条连接，日志面打开就多一处凭据回显面。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from acp.config.loader import db_url
from acp.persistence import target_metadata
from acp.persistence.checkpointer import library_declared_schema

#: checkpoint 那四张表 / 三个索引的结构权威在依赖包里（`langgraph...base.MIGRATIONS`），
#: 不在 `target_metadata` 里 ⇒ autogenerate 会把它们读成"库里多出来的东西"并建议 `remove_table`，
#: 于是 `alembic check` 报红。这里按**名字集合**把它们排除掉，而那个名字集合是从同一份列表解析来的
#: —— 不是手敲四个表名：手敲等于抄第二份 schema，依赖升级后列表变了而这里没跟，
#: 表现就是 `alembic check` 又开始建议删表（红在错的地方）。
#: 排除只作用于 autogenerate 的比较面，`upgrade` / `downgrade` 的显式 revision 一字不动。
_CHECKPOINT_TABLES, CHECKPOINT_INDEXES = library_declared_schema()
_EXCLUDED_TABLES = set(_CHECKPOINT_TABLES)
# `library_declared_schema()` 的索引项形状是 `(表名, 索引名)`；这里只要索引名。
_EXCLUDED_INDEXES = {index_name for _table, index_name in CHECKPOINT_INDEXES}


def include_object(object, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and name in _EXCLUDED_TABLES:
        return False
    if type_ == "index" and name in _EXCLUDED_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(db_url())
    try:
        with engine.begin() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=include_object,
            )
            context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
