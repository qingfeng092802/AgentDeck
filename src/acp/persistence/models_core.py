"""迁移第一批（T023）：EN-01 `feature_request` / EN-02 `task_card` / EN-03 `agent_unit` /
EN-05 `capability_manifest` / EN-06 `artifact`。

**逐行照 `data-model.md §1`，不加文档没写的东西**：文档给 NULL 标记的列才可空，其余 `nullable=False`；
默认值只在文档明写"默认"处出现（EN-02 `review_standard_locked` 的"默认 true"）；易变数值
（阈值、时长、并发默认、单价）一律运行时读 `config/params-ref.md`，不进字段定义（宪法 v1.0.3
引用型参数条款）。

**入库级 CHECK 只落文档把规则指派给数据库的那些**：`§4 校验归属` 把三岗互斥指派给
`guard/triple.py`、把产物三项下限的**覆写**指派给 `orchestrate/nodes/validate_artifact.py`、
把审批单五要素指派给 `approval/service.py` ⇒ 这些不在 DB 再写一份，否则同一条规则出现两个
强制点，违反宪法原则 I（不留第二份事实源）。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import mapped_column

from acp.persistence.base import Base
from acp.persistence.enums import pg_enum


class FeatureRequest(Base):
    """EN-01 需求单（`data-model.md §1` EN-01）。"""

    __tablename__ = "feature_request"

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_key = mapped_column(Text, nullable=False)
    raw_input = mapped_column(Text, nullable=False)
    # 含"自行补全项"显式列表（FR-002）：结构由协议层摘要对象给出，库里只按 jsonb 存。
    summary_digest = mapped_column(JSONB, nullable=False)
    clarification_waived = mapped_column(Boolean, nullable=False)
    status = mapped_column(pg_enum("feature_request_status"), nullable=False)
    #: 这批卡是怎么来的（2026-10-31）。取值域见 `orchestrate.graph.SPLIT_SOURCES`：
    #: `llm` = 真模型拆的；`prd` = 按 PRD 确定性投影；`fallback_stub` = 模型拆不动、回落通用清单；
    #: `stub` = 直接注入的确定性拆单器。**NULL 只代表"本列上线前的历史行"**，
    #: 不许把 NULL 读成 `llm` —— 那些行确实没人记过来源。
    split_source = mapped_column(Text, nullable=True)
    #: 请求级的"谁来执行、谁来把关"（2026-10-31 路线 B）。形状的唯一载体是
    #: `orchestrate/routing.py` 的模块说明；**不存密钥**（列会随读面回给浏览器）。
    #: NULL = 老请求：执行者照旧由 `select_worker` 排序，把关席照旧由 EN-04 候选池填。
    model_routing = mapped_column(JSONB, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class TaskCard(Base):
    """EN-02 任务卡（核心，`data-model.md §1` EN-02）。

    三处**曾按字面实现并登记待裁**的决定，现由 owner 2026-10-12（Phase 1.3 收官轮）裁定并落地：

    1. **裁决 2**：主键改成**复合** `(feature_request_id, id)`。原文档写 "text PK" 而说明列写
       "同一需求单内唯一"，两者不等价：卡 id 形如 `T01`，两张需求单各自拆出 `T01` 时全局主键会撞。
       **连带后果（同批改到位）**：所有指向任务卡的外键都必须引用这对列，否则 PG 报
       "no unique constraint matching given keys" ⇒ `artifact` / `review_verdict` 各补一列
       `feature_request_id`（值可从其所属卡推出，是为参照完整性做的冗余，不是第二份业务事实），
       `acceptance_map` / `approval_ticket` 用它们已有的那一列组成复合外键。
    2. **裁决 3**：`token_budget` 可空，`NULL = 无预算`（SQL 原生语义，不另造标志字段）；
       非空时 MUST > 0 ⇒ 落 DB CHECK。此前"只在协议层拦 0"的口径到此入库。
    3. `artifact_floor_format`：字段表原写 `text`，取 §5 T-1 的指令落为原生枚举
       `artifact_format_type`（该条明写"1.3 的 PG 枚举 MUST 引用 `refs.py::ArtifactFormat`"）。
    4. `snapshot_id` 文档未给 NULL 标记 ⇒ 按字面 NOT NULL，即"插入任何卡之前必须已有存档点行"，
       与 FR-053 的"本轮开工前存档点"同向。本轮不动，如实登记在 §5 T-11 之外。
    5. **T-14 由 owner 2026-10-12 裁候选 ①**：`executor_agent_id` / `executor_role_id` 改可空，
       使"拆单后立即建行（`status=DRAFT`，还没指派）"成为合法形态。`snapshot_id` **仍是 NOT NULL**
       —— 它不是同一处缺口：FR-053 要求开工前就有 `PRE_WORK` 存档点，所以建行前该由编排引擎先落 EN-12 行，
       这条路是**可满足**的，不需要第二条裁决。
    """

    __tablename__ = "task_card"
    __table_args__ = (
        PrimaryKeyConstraint("feature_request_id", "id"),
        CheckConstraint("artifact_floor_min_bytes > 0", name="floor_bytes_positive"),
        CheckConstraint("artifact_floor_min_lines > 0", name="floor_lines_positive"),
        CheckConstraint("handoff_count <= 1", name="handoff_count_within_limit"),
        # 裁决 3：NULL 不参与 `> 0` 判定（SQL 的 CHECK 对 NULL 恒不违反）⇒ 一个约束同时表达
        # "非空必须为正"与"NULL = 不限"两件事，不需要第二个标志列。
        CheckConstraint("token_budget > 0", name="token_budget_positive"),
    )

    id = mapped_column(Text, nullable=False)
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False
    )
    title = mapped_column(Text, nullable=False)
    difficulty = mapped_column(Integer, nullable=False)
    skill_tags = mapped_column(ARRAY(Text), nullable=False)
    # 入库前经 DAG 校验（FR-005），强制点在 `orchestrate/nodes/normalize.py`，不在 DB。
    depends_on = mapped_column(ARRAY(Text), nullable=False)
    # **owner 2026-10-12（T-14 取候选 ①）**：两列改可空。理由由 owner 给出：
    # `reviewer_agent_id` / `acceptor_agent_id` 早就是同一种可空形态（"可空至 AssignGuards 填充"），
    # 拆单阶段"还没有执行人"是事实而不是占位 —— 候选 ②（清单只存图状态）违反 PG 是唯一事实态，
    # 候选 ③（另立拆单产物表）违反原则 I 的分层（同一张卡两个载体）。
    # 可空**只表示"指派尚未发生"**：非空时外键照常生效（库里 `fk_task_card_executor_*` 两条仍在），
    # 而"三岗两两不同"（FR-031）的强制点在 `guard/triple.py`，它必须自己处理"列还是 NULL"这一态，
    # 不许拿 NULL 当"天然不相等"混过断言。
    executor_agent_id = mapped_column(Text, ForeignKey("agent_unit.agent_id"), nullable=True)
    # 跨批次外键：role_spec 属第三批（T025），本列在此声明、由 revision t025_registry 补 FK。
    executor_role_id = mapped_column(Text, ForeignKey("role_spec.role_id"), nullable=True)
    # 可空至 AssignGuards 填充（FR-031）；"两两不同"的强制点 = guard/triple.py，故 DB 不写 CHECK。
    reviewer_agent_id = mapped_column(Text, ForeignKey("agent_unit.agent_id"), nullable=True)
    acceptor_agent_id = mapped_column(Text, ForeignKey("agent_unit.agent_id"), nullable=True)
    artifact_floor_min_bytes = mapped_column(Integer, nullable=False)
    artifact_floor_min_lines = mapped_column(Integer, nullable=False)
    artifact_floor_format = mapped_column(pg_enum("artifact_format"), nullable=False)
    # 裁决 3：可空 = 无预算；非空时由下面的 CHECK 保证 > 0（协议层同一条口径，两道都对齐）。
    token_budget = mapped_column(Numeric, nullable=True)
    deadline_minutes = mapped_column(Integer, nullable=True)
    acceptance_criteria = mapped_column(ARRAY(Text), nullable=False)
    review_standard_locked = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    handoff_count = mapped_column(Integer, nullable=False)
    # 上限取自宪法原则 III 的硬值，MUST NOT 下沉 params-ref（C9 修正）；本表不复述数字。
    review_round = mapped_column(Integer, nullable=False)
    # owner 2026-10-14 裁决 ⑬：轮次拆成两列 —— `review_round` 只计**验收轮次**（受宪法硬值约束），
    # `rework_count` 只计**返工次数**（无上限，也不计成换人，`handoff_count` 那条 CHECK 不被它触碰）。
    # 之前一列两用是登记在 `evidence/phase-07.md` 未闭 ⑤ 的待裁项，现由裁决定死。
    # `server_default` 不是占位：0 就是"这张卡还没返过工"这个事实的值，也正是旧行回填时唯一正确的数。
    rework_count = mapped_column(Integer, nullable=False, server_default=text("0"))
    status = mapped_column(pg_enum("task_card_state"), nullable=False)
    snapshot_id = mapped_column(UUID(as_uuid=True), ForeignKey("snapshot.id"), nullable=False)


class AgentUnit(Base):
    """EN-03 协作单元（工具）。`agent_id` 是三岗互斥的唯一判定单位（FR-031）。

    `current_load` 是**上一次已知占用**的可查快照，权威口径在瞬态层（原则 I），本列不是事实源，
    故不给它任何 DB 级默认或触发器——写者 = 编排引擎单点。
    """

    __tablename__ = "agent_unit"
    __table_args__ = (
        # "无战绩"与"总是失败"必须可区分：分母为 0 时比率只能是 NULL（文档 EN-03 明写）。
        CheckConstraint(
            "success_rate_sample > 0 OR historical_success_rate IS NULL",
            name="no_rate_without_sample",
        ),
    )

    agent_id = mapped_column(Text, primary_key=True)
    form = mapped_column(pg_enum("agent_form"), nullable=False)
    tier = mapped_column(Integer, nullable=False)
    cost_band = mapped_column(pg_enum("cost_band"), nullable=False)
    max_parallel = mapped_column(Integer, nullable=False)
    current_load = mapped_column(Integer, nullable=False)
    historical_success_rate = mapped_column(Numeric, nullable=True)
    success_rate_sample = mapped_column(Integer, nullable=False)
    workspace_dir = mapped_column(Text, nullable=False)
    enabled = mapped_column(Boolean, nullable=False)


class CapabilityManifest(Base):
    """EN-05 能力申报单。

    文档未给本表 PK，只给了"复合唯一：(agent_id, declared_at)"⇒ 直接把该唯一对作 PK，
    **不新增一个文档里没有的 `id` 列**（登记为 `data-model.md §5` T-11 的一处文档缺口）。
    """

    __tablename__ = "capability_manifest"

    agent_id = mapped_column(Text, ForeignKey("agent_unit.agent_id"), primary_key=True)
    capabilities = mapped_column(ARRAY(Text), nullable=False)
    tier = mapped_column(Integer, nullable=False)
    cost_band = mapped_column(pg_enum("cost_band"), nullable=False)
    max_parallel = mapped_column(Integer, nullable=False)
    declared_at = mapped_column(DateTime(timezone=True), primary_key=True)


class Artifact(Base):
    """EN-06 产物（实测值，非自评）。

    `validated_status` 可空是**有意的**：文档说它"由 `ValidateArtifact` 覆写"（FR-019），
    覆写前不存在判定结果；把它建成 NOT NULL 就等于允许写入方先填一个 `valid` 蒙过门禁。

    `feature_request_id` 是**裁决 2 的连带列**：任务卡主键改成 `(feature_request_id, id)` 后，
    单列外键 `task_card_id → task_card.id` 不再合法（被引用列集上没有唯一约束）。补这一列组成
    复合外键是保住"产物必须挂在真实卡上"这条参照完整性的最小改动；它的值可从所属卡推出，
    属参照完整性冗余，不是第二份业务事实。
    """

    __tablename__ = "artifact"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feature_request_id", "task_card_id"],
            ["task_card.feature_request_id", "task_card.id"],
        ),
    )

    id = mapped_column(UUID(as_uuid=True), primary_key=True)
    feature_request_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_request.id"), nullable=False
    )
    task_card_id = mapped_column(Text, nullable=False)
    writer_agent_id = mapped_column(Text, ForeignKey("agent_unit.agent_id"), nullable=False)
    rel_path = mapped_column(Text, nullable=False)
    bytes = mapped_column(Integer, nullable=False)
    lines = mapped_column(Integer, nullable=False)
    format = mapped_column(pg_enum("artifact_format"), nullable=False)
    self_reported_status = mapped_column(pg_enum("self_reported_status"), nullable=False)
    validated_status = mapped_column(pg_enum("validated_status"), nullable=True)
    invalid_reason = mapped_column(Text, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
