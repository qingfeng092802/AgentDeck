"""PG 事实态的落库映射（Phase 1.3 / T023 ~ T025）。

导入本包即完成 **全部 17 张表** 的映射注册：Alembic 的 `env.py` 只需 `import acp.persistence`
就能拿到完整的 `target_metadata`，不必在多处维护"哪些表属于本模型"的第二份清单
（实体清单的唯一口径 = `data-model.md §1` 的 `### EN-` 小节，用例按那条复算命令的语义核对表名集合）。
"""

from acp.persistence.base import Base, NAMING_CONVENTION, target_metadata
from acp.persistence.enums import domains, pg_enum, values_of
from acp.persistence.states import TaskCardState

# 侧效导入：三批实体都要进同一份 metadata（缺一个 import 就会少一批表，且不会报错）。
from acp.persistence import models_approval  # noqa: F401  (EN-07 ~ EN-11)
from acp.persistence import models_core  # noqa: F401  (EN-01 / EN-02 / EN-03 / EN-05 / EN-06)
from acp.persistence import models_registry  # noqa: F401  (EN-04 / EN-12 ~ EN-17)
from acp.persistence import models_review  # noqa: F401  (EN-18 review_report)

__all__ = [
    "Base",
    "NAMING_CONVENTION",
    "TaskCardState",
    "domains",
    "pg_enum",
    "target_metadata",
    "values_of",
]
