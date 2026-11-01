"""T047：审批单建单路径（五要素校验 + 卡点作用域 + 免确认接线，FR-039 / FR-038 / FR-048｜R-26）。

**"缺项 MUST NOT 呈交"在这里只有一道门**：`validate_payload()`。卡点节点、内部端点、将来的超时扫描
MUST 都从它过 —— 校验做成两处（入口一份、服务一份）就会出现"绕过某一条路径的单子带着残缺上屏"，
而审批者看不到残缺就会以为自己批的是完整的（FR-039 的动机正是这条）。

**五要素的键名唯一载体是 `contracts/approval-api.md §2` 那张表**，本模块用 `_FIVE` 那份元组把它
成文为代码；文档改键名而代码没跟 ⇒ `tests/contract/test_approval_payload.py` 的双向差集断言红
（同 `persistence/states.py` 之于 `data-model.md §2` 的做法）。

三条刻意的设计，逐条可证伪：

1. **`timeout_policy` 由本模块算，不接受调用方给的值**。它是五要素之一，所以缺它要拒；
   但它同时是 FR-043 那条 MUST NOT 的载体 —— 如果允许调用方自带一份，"这张单 5 分钟后自动放行"
   就是一个合法请求。因此口径是：**必须由调用方交出这个键**（缺 ⇒ 400），
   且**它必须逐字等于 `timeouts.py` 从 `params-ref §1` 算出的那份**（不等 ⇒ 同一条码，detail 点名键）。
   两种坏法共用建单侧唯一的 400 码，细分在 detail 的键名里 —— 这与 `contracts/file-bus.md §2.1`
   的 `write_forbidden`（三处坏法一个码、细分在 reason）是同一条口径，不是偷懒。
2. **启用的卡点类是 A / D / E**（Phase 4 的 Goal 行 + 宪法 V；E 由 Phase 17 P1 加入）。
   B / C 建单**报错而不是静默降级**：静默接受会产出一张"结构预留"的单据却在库里长得跟真单一样，
   那是最容易被读成"已启用"的形态。
3. **`risk_exempt_eligible` 没有入参**。FR-047 要的是"AI 无覆盖入口"，落到函数签名上就是
   这条路径上没有任何一个参数能把它设成 `True`；值只从 `registry/risk_engine.evaluate()` 来，
   而那份规则表只从 `config/risk_matrix.yaml` 读。

payload 里那份 `timeout_policy` 是**建单时刻的快照**（审批者要能在单上看到自己批的是哪种等待规则），
不是第二个权威：改 §1 的值不会改写历史单据，这是 FR-039 要的效果，不是漂移。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator, model_validator
from sqlalchemy.orm import Session

from acp.approval import repository as repo
from acp.approval.timeouts import (
    AUTO_PASS_ON_TIMEOUT_FORBIDDEN,
    CONVERGE_KINDS,
    KIND_BY_LETTER,
    TimeoutPolicy,
    load_policies,
    policy_for,
    review_round_limit,
)
from acp.persistence.enums import values_of
from acp.registry.risk_engine import RequestFacts, Verdict, evaluate, exemption_applies

#: 五要素的键名（`contracts/approval-api.md §2` 那张表的"键"列）。
_FIVE: tuple[str, ...] = (
    "pending_item",
    "context_digest",
    "suggested_verdict",
    "impact",
    "timeout_policy",
)

#: 启用的卡点类：A 拆单确认 + D 危险操作 + E 审查不确定（Phase 17 P1）。
#: B / C 只有域值与表结构，没有启用路径。
ACTIVE_KINDS: frozenset[str] = frozenset(
    {KIND_BY_LETTER["A"], KIND_BY_LETTER["D"], KIND_BY_LETTER["E"]}
)

#: `suggested_verdict` 的取值域 = EN-10 的 `resolution_verdict`（系统与人是同一套结论词表）。
SUGGESTIBLE = frozenset(values_of("resolution_verdict"))
#: 影响面里的用量口径键 = `billing_basis` 域（FR-061 要求三口径分列，所以字典的键就是这个域）。
USAGE_KEYS = frozenset(values_of("billing_basis"))


class PayloadIncomplete(ValueError):
    """`400 payload_incomplete`。`missing` / `malformed` / `conflicts` 三个列表进 detail，不进码名。"""

    def __init__(
        self,
        *,
        missing: tuple[str, ...] = (),
        malformed: tuple[str, ...] = (),
        conflicts: tuple[str, ...] = (),
    ) -> None:
        self.missing = missing
        self.malformed = malformed
        self.conflicts = conflicts
        parts = []
        if missing:
            parts.append(f"缺键 {sorted(missing)}")
        if malformed:
            parts.append(f"形状不符 {sorted(malformed)}")
        if conflicts:
            parts.append(f"与 params-ref 算出的值不符 {sorted(conflicts)}")
        super().__init__("payload_incomplete：" + "；".join(parts))

    @property
    def code(self) -> str:
        return "payload_incomplete"


class KindNotEnabled(ValueError):
    """B / C 类只建模不启用（Phase 4 的 Goal 行；Phase 17 起 A / D / E 启用）。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"卡点类型 {kind} 未启用：当前只启 A / D / E 三类，B / C 是结构预留"
        )


class Impact(BaseModel):
    """五要素④：波及的后续任务 id 列表 + 预估额外用量（FR-039 原文的两段）。"""

    model_config = ConfigDict(extra="forbid")

    downstream_task_ids: list[str]
    estimated_extra_usage: dict[str, int]

    @field_validator("downstream_task_ids")
    @classmethod
    def _ids_non_blank(cls, value: list[str]) -> list[str]:
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError("downstream_task_ids 必须是非空字符串列表")
        return value

    @model_validator(mode="after")
    def _ids_required_unless_review_uncertain(self, info: ValidationInfo) -> "Impact":
        """空列表只对 E（审查不确定）放开：叶子卡没有后继，硬凑一个下游 id 就是造假。

        非 E 类沿用五要素的原口径（空列表 ⇒ 拒）。`context` 由 `validate_payload` 传入
        `kind`；直接构造 `Impact` 而没带上下文时按**保守侧**处理（仍然要求非空），
        免得某个绕过建单路径的调用方拿到一个比产品路径更宽的形状。
        """
        kind = (info.context or {}).get("kind")
        if not self.downstream_task_ids and kind != KIND_BY_LETTER["E"]:
            raise ValueError("downstream_task_ids 必须是非空字符串列表（仅 E_REVIEW_UNCERTAIN 允许为空）")
        return self

    @field_validator("estimated_extra_usage")
    @classmethod
    def _usage_keys_are_billing_bases(cls, value: dict[str, int]) -> dict[str, int]:
        """用量 MUST 按口径分键存放：一个加总过的单一数字就是 FR-061 禁的那种"三口径合并"。"""
        unknown = sorted(set(value) - USAGE_KEYS)
        if unknown:
            raise ValueError(f"estimated_extra_usage 的键不在 billing_basis 域里：{unknown}")
        if any(not isinstance(qty, int) or qty < 0 for qty in value.values()):
            raise ValueError("estimated_extra_usage 的值必须是非负整数")
        return value


class _ApprovalPayload(BaseModel):
    """五要素的形状。键名与 `_FIVE` 一一对应，多一个键就拒（防"第二份规则"混进 payload）。"""

    model_config = ConfigDict(extra="forbid")

    pending_item: str
    context_digest: str
    suggested_verdict: str
    impact: Impact
    timeout_policy: dict[str, Any]

    @field_validator("pending_item", "context_digest")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("空白串等于没交")
        return value

    @field_validator("suggested_verdict")
    @classmethod
    def _verdict_in_domain(cls, value: str) -> str:
        if value not in SUGGESTIBLE:
            raise ValueError(f"suggested_verdict 不在 resolution_verdict 域里：{sorted(SUGGESTIBLE)}")
        return value

    @field_validator("timeout_policy")
    @classmethod
    def _policy_is_a_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("timeout_policy 不能是空映射")
        return value


def render_timeout_policy(
    kind: str, *, table: dict[str, TimeoutPolicy] | None = None, constitution=None
) -> dict[str, Any]:
    """把 §1 的那一行渲染成 payload 的五要素⑤（快照，不是第二权威）。

    `wait_seconds` 为 `None` 就是"无超时"的字面表达 —— 这里 MUST NOT 拿 0 顶：
    0 秒在语义上是"立刻到期"，与 §1 那三行正好相反。
    """
    policy = policy_for(kind, table=table)
    return {
        "kind": policy.kind,
        "wait_seconds": None if policy.wait is None else int(policy.wait.total_seconds()),
        "on_timeout": policy.on_timeout,
        "extension_length_seconds": (
            None
            if policy.extension_length is None
            else int(policy.extension_length.total_seconds())
        ),
        "max_extensions": policy.max_extensions,
        "auto_pass_forbidden": kind in AUTO_PASS_ON_TIMEOUT_FORBIDDEN,
        # 只有 B 类有"跑满轮次才收敛"这回事，A / C / D 给 None 而不是给一个用不上的数。
        "convergence_round_limit": review_round_limit(constitution) if kind in CONVERGE_KINDS else None,
    }


def validate_payload(
    payload: Mapping[str, Any],
    *,
    kind: str,
    table: dict[str, TimeoutPolicy] | None = None,
) -> dict[str, Any]:
    """五要素齐备 + 形状合法 + `timeout_policy` 等于 §1 算出的那份 ⇒ 返回规范化后的 dict。"""
    if not isinstance(payload, Mapping):
        raise PayloadIncomplete(missing=_FIVE)
    missing = tuple(key for key in _FIVE if key not in payload or payload[key] is None)
    if missing:
        raise PayloadIncomplete(missing=missing)
    try:
        # `kind` 只进校验上下文（不进 payload 的键集）：E 类的空 `downstream_task_ids` 例外
        # 需要知道这是哪一类单，而给 payload 加第六个键会直接破 `extra="forbid"` 与五要素判据。
        model = _ApprovalPayload.model_validate(dict(payload), context={"kind": kind})
    except ValidationError as exc:
        keys = sorted({str(err["loc"][0]) if err["loc"] else "?" for err in exc.errors()})
        raise PayloadIncomplete(malformed=tuple(keys)) from exc
    if dict(payload["timeout_policy"]) != render_timeout_policy(kind, table=table):
        raise PayloadIncomplete(conflicts=("timeout_policy",))
    return model.model_dump(mode="json")


#: 单据主键的推导命名空间：常量而不是随机值 ⇒ 同一个 `(卡点类, 需求单, 卡)` 永远得到同一个 `ticket_id`。
TICKET_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:acp:approval_ticket")


def ticket_id_for(
    *, kind: str, feature_request_id: uuid.UUID, task_card_id: str | None = None
) -> uuid.UUID:
    """**可推导的主键**：`interrupt` 的节点在恢复时会从头再执行一遍（实测读数见
    `evidence/phase-04.md §T048`），如果单据主键是 `uuid4()`，重放就会再造一张。
    拿 FR-045 的合并逻辑兜也可以（`pending_for` 会认出来），但那条路只覆盖"仍是待决"的单据 ——
    人已批过的单在重放时会掉出 `state='PENDING'` 的查询，于是第二张就插进去了。
    主键可推导 ⇒ 重放永远回到同一行，不需要新列、也不把单据号抄成第二份事实。
    """
    suffix = f":{task_card_id}" if task_card_id else ""
    return uuid.uuid5(TICKET_NAMESPACE, f"{kind}:{feature_request_id}{suffix}")


def build_payload(
    *,
    kind: str,
    pending_item: str,
    context_digest: str,
    suggested_verdict: str,
    impact: Mapping[str, Any],
    table: dict[str, TimeoutPolicy] | None = None,
) -> dict[str, Any]:
    """把调用方能给的四项 + 本模块算的那一项组成五要素（调用方拿不到 `timeout_policy` 的写入口）。"""
    return {
        "pending_item": pending_item,
        "context_digest": context_digest,
        "suggested_verdict": suggested_verdict,
        "impact": dict(impact),
        "timeout_policy": render_timeout_policy(kind, table=table),
    }


@dataclass(frozen=True)
class Ticket:
    """建单结果 + 这次判定为什么是这个结果。`verdict` 带回来是为了让留痕能说出规则名。"""

    ticket_id: uuid.UUID
    kind: str
    merged: bool
    kind_conflict: bool
    exempt: bool
    auto_passed: bool
    #: 建完之后的单据态（`approval_state` 域值）。卡点节点靠它决定"还要不要挂起等人"，
    #: 而不是靠自己的记忆 —— 重放时这份记忆就是假的。
    state: str = ""
    version: int = 0
    decided_by: str | None = None
    reasons: tuple[str, ...] = field(default=())


def create_approval(
    session: Session,
    *,
    kind: str,
    payload: Mapping[str, Any],
    feature_request_id: uuid.UUID | None = None,
    task_card_id: str | None = None,
    facts: RequestFacts | None = None,
    now: dt.datetime | None = None,
    ticket_id: uuid.UUID | None = None,
) -> Ticket:
    """校验五要素 → 判免确认 → 建单（或合并到已有未决单）。

    `facts` 是平台侧算出的三条低风险事实；**不给 facts 就是不免确认**（保守方向，与 FR-046
    "任一条不满足即 MUST 请求确认"同向），而不是"没传就默认低风险"。
    """
    if kind not in set(values_of("approval_kind")):
        raise repo.ApprovalMisuse(f"卡点类型 {kind!r} 不在 approval_kind 域里")
    if kind not in ACTIVE_KINDS:
        raise KindNotEnabled(kind)
    if task_card_id and not feature_request_id:
        # `data-model.md §4` 给建单路径的活：库里那条复合外键是 MATCH SIMPLE（一列 NULL 就不校验），
        # 所以"挂了卡却没挂需求单"这种半拉子行 PG 不拦 —— 拦它是这条路径的责任。
        # 不用 MATCH FULL 换它：FULL 会连合法的 A 类单（挂需求单、`task_card_id` 为 NULL）一起拒掉。
        raise repo.ApprovalMisuse(
            f"卡 {task_card_id} 的单没挂需求单 ⇒ 复合外键在 MATCH SIMPLE 下不校验，这里 MUST 拒"
        )
    table, _limit = load_policies()
    normalized = validate_payload(payload, kind=kind, table=table)
    policy = policy_for(kind, table=table)
    verdict = (
        Verdict(False, None, ("request_facts_not_supplied",))
        if facts is None
        else evaluate(facts)
    )
    auto = exemption_applies(kind, verdict)
    creation = repo.create_or_merge(
        session,
        kind=kind,
        payload=normalized,
        risk_exempt_eligible=verdict.exempt,
        timeout_policy=policy,
        task_card_id=task_card_id,
        feature_request_id=feature_request_id,
        now=now,
        ticket_id=ticket_id,
    )
    reasons = verdict.reasons
    auto_passed = False
    decided_by: str | None = None
    if creation.merged:
        # 合并回来的单可能是**别人**建的（同任务同类型的重复触发，FR-045 前半句）：
        # 免确认只对"这一条路径新建的单"生效，不去改一张已经挂着的单，否则两份判据谁赢取决于顺序。
        if auto:
            reasons = (*reasons, "auto_pass_skipped_because_merged")
    else:
        if auto:
            outcome = repo.apply_auto_pass(
                session, ticket_id=creation.ticket_id, decided_by=verdict.decided_by, now=now
            )
            auto_passed = outcome.applied
            decided_by = verdict.decided_by
    row = session.get(repo.ApprovalTicket, creation.ticket_id)
    return Ticket(
        ticket_id=creation.ticket_id,
        kind=creation.kind,
        merged=creation.merged,
        kind_conflict=creation.kind_conflict,
        exempt=auto,
        auto_passed=auto_passed,
        state=str(row.state),
        version=row.version,
        decided_by=decided_by or row.decided_by_principal,
        reasons=reasons,
    )
