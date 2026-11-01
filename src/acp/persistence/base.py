"""声明式基类与约束命名约定（T023 ~ T025 共用）。

**为什么把命名约定写成表**：PG 对未命名的 CHECK / 索引自动起名（形如
`task_card_artifact_floor_min_bytes_check`），列一改名约束名就跟着变，迁移与用例里的引用会
在某个时刻莫名其妙失效。这里固定成 `ck_<表>_<短名>` 形态，用例与迁移引用的是**稳定名字**，
失败时能直接归因到"哪一条入库级不变量被破"。

约定里 `ck` 带 `%(constraint_name)s` ⇒ 模型侧只写短名（`floor_bytes_positive`），
最终名 = `ck_task_card_floor_bytes_positive`。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

target_metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = target_metadata
