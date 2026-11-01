"""PG 侧枚举类型的唯一定义处（T023 ~ T026 共用）。

**从协议层派生，不另写一份**：`ArtifactFormat` / `AgentForm` / `CostBand` / `UsageKind` /
`BillingBasis` / `SelfReportedStatus` 六个域直接取 `acp.protocol.refs` 里 `Literal` 的参数——
`refs.py` 的注释早就写了"PG 与适配器引用同一份"，这里就是兑现那句话的地方。
新增一个取值只改 `Literal`，PG 枚举跟着变；反过来若有人在这里抄一份字面量，
`tests/contract/test_states.py` 的双向差集断言会红。

其余的域只在 `data-model.md` 有定义（EN-01 / EN-08 / EN-09 / EN-10 / EN-12 / EN-14 / EN-16），
所以在这里成文并注明出处行。
"""

from __future__ import annotations

from typing import get_args

from sqlalchemy import Enum as SAEnum

from acp.protocol.refs import (
    AgentForm,
    ArtifactFormat,
    BillingBasis,
    CostBand,
    SelfReportedStatus,
    UsageKind,
)
from acp.persistence.states import TaskCardState

_DOMAINS: dict[str, tuple[str, ...]] = {
    # 顺序 = `refs.py` 里 `Literal` 的顺序（`pg_enum(...).enums` 与 `values_of(...)` 被逐位比对）。
    # `js`（owner 2026-10-29）**不在这里手写**：它随 `ArtifactFormat` 一起进来，PG 侧由迁移
    # `0010_artifact_format_js` 用 `ALTER TYPE … ADD VALUE` 追加到末尾 —— 两处都是"末尾"，
    # 因为它们描述的是同一个域的两个投影。
    "artifact_format": tuple(get_args(ArtifactFormat)),
    "agent_form": tuple(get_args(AgentForm)),
    "cost_band": tuple(get_args(CostBand)),
    "usage_kind": tuple(get_args(UsageKind)),
    "billing_basis": tuple(get_args(BillingBasis)),
    "self_reported_status": tuple(get_args(SelfReportedStatus)),
    "task_card_state": tuple(member.value for member in TaskCardState),
    # 以下各行的出处 = data-model.md 的小节号（改域必须先改那张表，再来这里加值）
    "feature_request_status": ("INTAKE", "SPLIT", "RUNNING", "DELIVERED", "FAILED"),  # EN-01
    "validated_status": ("valid", "invalid"),  # EN-06（由 ValidateArtifact 覆写，FR-019）
    # Phase 17 P1（owner 2026-10-23）：EN-08 从两值扩成三值。`NEED_HUMAN` 是**人工升级**的载体，
    # 由 `review.check_findings()` 在任一条 `met=null` 时推出；它 MUST NOT 由模型自述，
    # 也 MUST NOT 被折成 CHANGES_REQUESTED（那会把"平台判不了"写成"人已判不过"）。
    "review_verdict": ("APPROVE", "CHANGES_REQUESTED", "NEED_HUMAN"),  # EN-08（FR-028 + Phase 17）
    # Phase 17 P1（owner 2026-10-23）：第五类卡点 = 审查不确定。它与 A–D 共用 EN-09 表，
    # 但**不在图上产生新的 interrupt**：建单后卡留在 `REVIEWING`，等人工用 E 单定论。
    "approval_kind": (
        "A_SPLIT",
        "B_ACCEPTANCE",
        "C_BUDGET",
        "D_DANGER",
        "E_REVIEW_UNCERTAIN",
    ),  # EN-09（FR-038 + Phase 17 D3）
    # owner 2026-10-12（裁决 4）**删掉 `DUPLICATED`**：`contracts/approval-api.md` 规定后到者不二次迁移，
    # 那个态**到达不了**；"后到结论"由 `resolution_event.applied = false` 表达（同一事实只有一个载体，
    # 不留第二份）。T-6 由此闭合。
    "approval_state": (
        "PENDING",
        "APPROVED",
        "REJECTED",
        "NEED_INFO",
        "TIMED_OUT_CONVERGED",
    ),
    # owner 2026-10-13（裁决 ④）加第三个值 `AUTO`：免确认（FR-046/FR-047）也是一种 resolution，
    # 审计要能区分"人工批准"与"自动免确认"。此前它不写 EN-10，理由是域里只有两个通道、硬塞就是造假。
    # **`AUTO` 不由人提交**：它是"没有人在敲命令"的载体，所以 `repository.resolve()` 只放
    # `DASHBOARD / CLI`（判据 `test_a_human_caller_cannot_submit_the_auto_channel`）。
    "resolution_channel": ("DASHBOARD", "CLI", "AUTO"),  # EN-10（FR-042 同一张单 + 裁决 ④）
    "resolution_verdict": ("approve", "reject", "need_info"),  # EN-10
    "snapshot_phase": ("PRE_WORK", "POST_WORK"),  # EN-12（FR-053）
    "event_log_kind": (
        "TASK",
        "APPROVAL",
        "ARTIFACT",
        "REVIEW",
        "SNAPSHOT",
        "GUARD_VIOLATION",
        "READONLY_VIOLATION",
        "AUTONOMY_INTERCEPT",
        "SELECTION",
        # 2026-10-31：拆单回落兜底必须留痕 —— `.env` 里那把 key 失效时，系统静默用了 Stub，
        # 屏上和库里都读不出"这单没接真模型"（实测：卡上判据是模板句、`meeting_note` 0 行）。
        "split_fallback",
    ),  # EN-14
    "autonomy_mode": ("FULL", "SAFE"),  # EN-16（FR-055；宪法「三」明写只有两档）
}


_TYPES: dict[str, SAEnum] = {}


def values_of(domain: str) -> tuple[str, ...]:
    return _DOMAINS[domain]


def domains() -> tuple[str, ...]:
    return tuple(_DOMAINS)


def enum_type_name(domain: str) -> str:
    """PG 里的类型名。**与 `pg_enum()` 同一处推导**，降级脚本据此发 `DROP TYPE`，
    两处不可能写岔（写岔的表现是"降级后重放迁移报 type already exists"，本轮实测过）。
    """
    return f"{domain}_type"


def pg_enum(domain: str) -> SAEnum:
    """PG 原生枚举类型。类型名 = `<domain>_type`，迁移与降级都按这个名字操作。

    **按域缓存同一个对象**：`Enum` 是 `SchemaType`，`CREATE TABLE` 会为它内联发出一条
    `CREATE TYPE`。若每次调用都新建对象，跨表复用同一域（`cost_band` 用在 EN-03 与 EN-05，
    `artifact_format` 用在 EN-02 与 EN-06）就会发出两次 `CREATE TYPE` 而报"type already exists"。
    """
    cached = _TYPES.get(domain)
    if cached is None:
        cached = SAEnum(
            *_DOMAINS[domain],
            name=enum_type_name(domain),
            native_enum=True,
            create_constraint=False,
            inherit_cache=False,
        )
        _TYPES[domain] = cached
    return cached


_FROZEN: dict[tuple[str, tuple[str, ...]], SAEnum] = {}


def pg_enum_frozen(domain: str, values: tuple[str, ...]) -> SAEnum:
    """迁移专用的**标签集冻结**版（owner 2026-10-14 裁决 1，修 A-5）。

    类型名仍取 `enum_type_name(domain)`，与 `pg_enum()` 同一处推导，两处不可能写岔；
    但标签集**按那次迁移下发时的集合写死**，不取 `_DOMAINS` 的活值。

    **为什么必须分成两个函数**：`pg_enum()` 的活标签集回答的是"现在的模型长什么样"，
    而迁移要回答的是"那一刻库里该长什么样"。用活值建当年的类型，后来才加进去的值
    （`AUTO`，owner 2026-10-13 裁决 ④）会在第一次空库建库时就被写进类型，
    于是那条 `ALTER TYPE ... ADD VALUE 'AUTO'` 必然报 `DuplicateObject`
    ⇒ 从空库重放整条迁移链起不来（本轮 14:2x 实测）。增量库看不到这个洞，
    因为它的类型是当年那份两值集合。

    同一 `(域, 标签集)` 复用同一个对象 —— 与 `pg_enum()` 的缓存理由是同一条（`SchemaType` 会内联发
    `CREATE TYPE`）。**这里不改 `_TYPES`**：模型侧继续取活值，两侧唯一的不同就是标签集合。
    """
    key = (domain, values)
    cached = _FROZEN.get(key)
    if cached is None:
        cached = SAEnum(
            *values,
            name=enum_type_name(domain),
            native_enum=True,
            create_constraint=False,
            inherit_cache=False,
        )
        _FROZEN[key] = cached
    return cached

