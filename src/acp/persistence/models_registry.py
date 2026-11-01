"""迁移第三批（T025）：EN-04 `role_spec` / EN-12 `snapshot` / EN-13 `cost_record` /
EN-14 `event_log` / EN-15 `meeting_note` / EN-16 `autonomy_state` / EN-17 `deliverable`。

本批还负责补上前两批声明、但因表尚未建立而无法下发的两条跨批次外键
（`task_card.executor_role_id` → `role_spec`、`task_card.snapshot_id` → `snapshot`），
见 revision `t025_registry`。

`meeting_note` 的 `conflict_sides` 是否含裁决对象自身 ⇒ 升级人工（FR-037），是应用层判定，
不在 DB 写 CHECK；`autonomy_state.updated_by` 只能由使用者改（FR-055），强制点在
`api/autonomy.py`（§4），DB 只存值。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import mapped_column

from acp.persistence.base import Base
from acp.persistence.enums import pg_enum


class RoleSpec(Base):
    """EN-04 岗位说明。

    `mounted_agents` 与协议 `CapabilityManifest.role_mounts` 的结构冗余已在
    `contracts/agent-protocol.md §3.1` 定口径：**生效口径 = 本列**，申报超出生效集合 ⇒
    `GUARD_VIOLATION` + 拒注册。那条比对的落点在 Phase 3 注册路径（`tests/contract/
    test_all_adapters.py`），本轮只把生效口径这张表建出来，MUST NOT 读成"冗余已治理"。
    """

    __tablename__ = "role_spec"

    role_id = mapped_column(Text, primary_key=True)
    name = mapped_column(Text, nullable=False)
    charter = mapped_column(Text, nullable=False)
    # 审查员 / 验收员 = true（FR-033）；网关的只读门禁读它，见 bus/acl.py。
    read_only = mapped_column(Boolean, nullable=False)
    builtin = mapped_column(Boolean, nullable=False)
    mounted_agents = mapped_column(ARRAY(Text), nullable=False)


class Snapshot(Base):
    """EN-12 存档点（FR-053 / FR-054）。`ref` 是外部存档机制的句柄，本表只登记不实现（T-4）。"""

    __tablename__ = "snapshot"

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    round_no = mapped_column(Integer, nullable=False)
    phase = mapped_column(pg_enum("snapshot_phase"), nullable=False)
    task_card_ids = mapped_column(ARRAY(Text), nullable=False)
    ref = mapped_column(Text, nullable=False)
    rolled_back_at = mapped_column(DateTime(timezone=True), nullable=True)


class CostRecord(Base):
    """EN-13 用量记录（FR-059 ~ FR-061）。

    双维查询键（project / task / agent / role）在文档里就是普通 text，**未标 FK** ⇒ 按字面不建外键：
    用量流水要能在任务卡已被清理后仍可查，收紧成 FK 是我发明规则。
    跨 `billing_basis` 求和 MUST 报错属 `cost/ledger.py` 查询层（§4），不在 DB。
    """

    __tablename__ = "cost_record"
    __table_args__ = (
        # 防重复入账的唯一索引，键顺序逐字照文档 (task_card_id, agent_id, occurred_at, usage_kind)。
        Index(
            "uq_cost_record_dedup",
            "task_card_id",
            "agent_id",
            "occurred_at",
            "usage_kind",
            unique=True,
        ),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    occurred_at = mapped_column(DateTime(timezone=True), nullable=False)
    project_key = mapped_column(Text, nullable=False)
    task_card_id = mapped_column(Text, nullable=False)
    agent_id = mapped_column(Text, nullable=False)
    role_id = mapped_column(Text, nullable=False)
    # 不参与把关与选人（文档明写）。
    model_ref = mapped_column(Text, nullable=True)
    usage_kind = mapped_column(pg_enum("usage_kind"), nullable=False)
    quantity = mapped_column(Numeric, nullable=False)
    unit = mapped_column(Text, nullable=False)
    # 单价来源 params-ref §6；本列只存结果，不存推导。
    cost_amount = mapped_column(Numeric, nullable=False)
    currency = mapped_column(Text, nullable=False)
    billing_basis = mapped_column(pg_enum("billing_basis"), nullable=False)


class EventLog(Base):
    """EN-14 过程留痕（FR-051 四要素）。

    `payload` 落库前 MUST 过 `config/loader.py` 的 `secrets` 脱敏过滤器（宪法「二」）——
    强制点在 T027，本表只提供列；`kind` 里的 `AUTONOMY_INTERCEPT` / `SELECTION` 是 C5 / FR-023 新增。

    本表除 PK 外**没有二级索引**（owner 2026-10-15 裁定：T105 不给"按 `(agent_id, 清单指纹)` 回溯"加索引）。
    ⇒ 那句回溯走的是全表扫，代价落在读速上而不是落在一条迁移上；要加就另开一轮，别当成已有。
    """

    __tablename__ = "event_log"

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity = mapped_column(Text, nullable=False)
    entity_id = mapped_column(Text, nullable=False)
    kind = mapped_column(pg_enum("event_log_kind"), nullable=False)
    actor_agent_id = mapped_column(Text, nullable=False)
    payload = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class MeetingNote(Base):
    """EN-15 会议纪要（FR-037）。

    文档把 `id / project_key` 合成一行写为 text，未标 PK ⇒ 取 `id` 作 PK（登记 T-11）。
    """

    __tablename__ = "meeting_note"

    id = mapped_column(Text, primary_key=True)
    project_key = mapped_column(Text, nullable=False)
    # = 兼任架构师岗位的总管工具（FR-037）。
    authored_by_agent_id = mapped_column(Text, nullable=False)
    decision = mapped_column(Text, nullable=False)
    conflict_sides = mapped_column(ARRAY(Text), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class AutonomyState(Base):
    """EN-16 自主度与高危清单当前态（FR-055 ~ FR-058）。

    只存**当前态**（每项目恰一行，PK 即 project_key）；拦截与请示的发生史在 EN-14，
    靠 `danger_list_version` 回溯当时生效的是哪版清单。
    """

    __tablename__ = "autonomy_state"

    project_key = mapped_column(Text, primary_key=True)
    # 宪法「三」明写只有两档，不存在第三档取值 ⇒ 由枚举类型承担，不另写 CHECK。
    mode = mapped_column(pg_enum("autonomy_mode"), nullable=False)
    danger_list_version = mapped_column(Text, nullable=False)
    updated_by = mapped_column(Text, nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)
    last_intercept_at = mapped_column(DateTime(timezone=True), nullable=True)


class Deliverable(Base):
    """EN-17 交付包（FR-036）。

    **索引不是副本**：产物留在各 `<agent>-workspace/`，本表只存 manifest 指向它们；
    "唯一一份"由 `UNIQUE(feature_request_id)` 钉住（文档明写用唯一索引），第二包入库即冲突报错。
    """

    __tablename__ = "deliverable"

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False, unique=True
    )
    manifest = mapped_column(JSONB, nullable=False)
    entry_path = mapped_column(Text, nullable=False)
    open_issues = mapped_column(JSONB, nullable=False)
    assembled_by_agent_id = mapped_column(Text, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
