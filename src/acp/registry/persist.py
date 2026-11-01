"""注册表 → 事实态的落库与校验（T043｜FR-011 / FR-012，契约 §3.1）。

`describe()` 交回的那份申报单，到这里才第一次变成库里可读的行：

| 载体 | 它是什么 | 谁写 |
|---|---|---|
| `registry/agents.yaml` | **申报**（工具自报） | 人（改档位只改这里） |
| EN-05 `capability_manifest` | 申报的**历史投影**（带 `declared_at`） | 本模块 |
| EN-03 `agent_unit` | 申报的**生效快照**（选人读它） | 本模块 |
| EN-04 `role_spec.mounted_agents` | **生效口径**（owner 批准后的集合） | **本模块不写**（写它 = 工具给自己挂牌） |

四条刻意的规则，每条都有对应用例：

**R1 先全验、后写。** 一批注册里有一个岗超出生效集合 ⇒ 整批不落库。半支队伍进库比全拒更难查：
三岗互斥（FR-031）与后续选人（FR-022）都按"库里有谁"判定，缺一支会静默改变结果，
而不是报错。事务边界归调用方的 `persistence.session.new_session`，本模块只 `flush`。

**R2 只写申报列。** EN-03 的 `current_load` / `historical_success_rate` / `success_rate_sample`
是派生值（权威源在 `runtime/slot_manager.py` 与产物校验历史，宪法原则 I）⇒ 重新申报**不许**把它们抹回 0。

**R3 同一时刻的两份不同申报 = 抛。** EN-05 的 PK 是 `(agent_id, declared_at)`：同键同内容 ⇒ 幂等 no-op
（重跑注册不该留两行）；同键**不同**内容 ⇒ 拒，因为那等于给时间线编两个版本。

**R4 比对读 EN-04，不读 yaml。** 申报里多写一个岗 ⇒ 行为不变（照样拒）；EN-04 里加上这个岗 ⇒ 通过。
`MUST NOT 取并集`（契约 §3.1 原话），所以这里没有任何"两边都有就算"的写法。

**生产路径眼下必然全拒，这一点不粉饰**：EN-04 还**没有写者** —— 内置岗位说明（FR-071 的九岗，
M1 只交付前三岗）是 **T087**，本轮之下 `role_spec` 是空表 ⇒ 生效集合为空 ⇒ 任何申报都超出。
那是**正确**的行为（没有批准就没有生效），不是缺陷；所以下面所有正向用例都自己 seed `role_spec`。

曾经预见的岗名冲突已由 owner 2026-10-13 裁决 2 处置（`developer` 不作为岗存在，qoder 的申报改成
`architect` / `reviewer` / `acceptor` 三个前置岗）⇒ 三支申报现在都落在 M1 要种的三岗之内。
**那条裁决当时换来的两处口径张力，同日已裁并落地**（原来登记的两个问题保留在此作出处，不再是待裁项）：
① FR-033 的只读是**任务级**（owner 裁决 1）：同一支工具在**这个任务**上担审查/验收岗时，该任务的产物路径
对它只读；它作为别的任务的执行者照写（ACL 已按此收窄，`bus/acl.py:160-166`）。
本模块只看岗名、不看 `read_only`（那是网关的活），
口径全文与**现状缺口**（`Actor.read_only` 尚无生产写者）写在 `contracts/agent-protocol.md §4`。
② `acceptance_criteria` **三支统一随任务下发**（owner 裁决 2）：T040 / T041 那两轮"细则不发给被考核方"的
方向纪律被覆盖，判据 = `tests/contract/test_all_adapters.py::test_every_submit_carries_the_criteria`
⇒ 本模块两侧不再有"doubao 发、qoder 不发"的两套口径。

证据档：本模块的判据全部来自仓内可读的代码与库（甲/乙档），**不含任何厂商调用**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.bus.layout import workspace_dir_name
from acp.persistence.models_core import AgentUnit
from acp.persistence.models_core import CapabilityManifest as CapabilityManifestRow
from acp.persistence.models_registry import RoleSpec
from acp.protocol.messages import CapabilityManifest
from acp.registry.loader import Registration, load_registrations

#: EN-03 里属于"申报"的那几列 —— R2 的白名单，派生列一个都不在里头。
DECLARATION_FIELDS = ("form", "tier", "cost_band", "max_parallel")


class RegistrationRefused(RuntimeError):
    """申报与生效口径不一致 ⇒ 拒注册。

    消息里只有 `agent_id`、岗位名与两个集合；MUST NOT 有凭据值，也不写"已注册"这类结论词
    （拒注册时它是假的）。
    """

    def __init__(self, agent_id: str, *, reason: str, declared: tuple[str, ...], effective: tuple[str, ...]) -> None:
        super().__init__(
            f"{agent_id} 拒注册：{reason}"
            f"｜申报 role_mounts={list(declared)}"
            f"｜生效口径 EN-04 mounted_agents 的键={list(effective)}"
            "｜处置=整批不落库（R1），MUST NOT 取并集（契约 §3.1）"
        )
        self.agent_id = agent_id
        self.reason = reason
        self.declared = declared
        self.effective = effective


class RegistryMisuse(ValueError):
    """调用方式不对（空批次、不带时区的 `declared_at`）。

    与"申报被拒"分开放：那一类是**数据**冲突、由装配方按结果决定要不要继续；这一类是**用法**错，
    混进同一个异常里会让调用方把一个编程错误读成"这批 agent 没批准"。
    """


class DeclarationTimelineConflict(RuntimeError):
    """同一个 `(agent_id, declared_at)` 上已经有一份**内容不同**的申报（R3）。

    单独一档的理由：它不是权限问题，改 `role_mounts` 或 EN-04 都消不掉它，只有换
    `declared_at`（承认这是新一次申报）或撤回改写才是解。
    """


@dataclass(frozen=True)
class RegistrationReport:
    """这一批写了什么。调用方要能把"新建 / 更新 / 复用"三种分开报，别只报一个总数。"""

    declared_at: datetime
    agents: tuple[str, ...]
    en03_created: tuple[str, ...]
    en03_updated: tuple[str, ...]
    en05_inserted: int
    en05_reused: int


def _now_utc() -> datetime:
    return datetime.now(UTC)


def effective_mounts(session: Session) -> dict[str, frozenset[str]]:
    """EN-04 的生效口径：`{role_id: 被批准挂载的 agent_id 集合}`。

    空表返回空映射 ⇒ 所有申报都会被判"超出"。这里**不加**"表为空就放行"的兜底：
    放行等于把"还没批准"读成"批准全部"，那正是契约要避免的那类静默降级。
    """
    rows = session.execute(select(RoleSpec.role_id, RoleSpec.mounted_agents)).all()
    return {role_id: frozenset(agents or ()) for role_id, agents in rows}


def check_role_mounts(manifest: CapabilityManifest, mounts: Mapping[str, frozenset[str]]) -> None:
    """申报的每个岗都 MUST 在 EN-04 里、且该岗的 `mounted_agents` 含本 agent。"""
    for role_id in manifest.role_mounts:
        allowed = mounts.get(role_id)
        if allowed is None:
            raise RegistrationRefused(
                manifest.agent_id,
                reason=f"生效口径里没有岗位 {role_id!r}（EN-04 无此行）",
                declared=tuple(manifest.role_mounts),
                effective=tuple(sorted(mounts)),
            )
        if manifest.agent_id not in allowed:
            raise RegistrationRefused(
                manifest.agent_id,
                reason=f"岗位 {role_id!r} 的生效集合不含 {manifest.agent_id!r}",
                declared=tuple(manifest.role_mounts),
                effective=tuple(sorted(allowed)),
            )


def _write_agent_unit(session: Session, registration: Registration) -> bool:
    """写 EN-03 的申报列。返回 True = 新建，False = 更新既有行（R2：派生列不碰）。"""
    manifest = registration.manifest
    existing = session.get(AgentUnit, manifest.agent_id)
    if existing is None:
        session.add(
            AgentUnit(
                agent_id=manifest.agent_id,
                form=manifest.form,
                tier=manifest.tier,
                cost_band=manifest.cost_band,
                max_parallel=manifest.max_parallel,
                current_load=0,
                historical_success_rate=None,
                success_rate_sample=0,
                workspace_dir=workspace_dir_name(manifest.agent_id),
                enabled=registration.enabled,
            )
        )
        return True
    for field in DECLARATION_FIELDS:
        setattr(existing, field, getattr(manifest, field))
    existing.workspace_dir = workspace_dir_name(manifest.agent_id)
    existing.enabled = registration.enabled
    return False


def _write_manifest_row(
    session: Session, registration: Registration, declared_at: datetime
) -> bool:
    """写 EN-05 一行。返回 True = 新插，False = 同键同内容复用（R3）。"""
    manifest = registration.manifest
    row = session.get(CapabilityManifestRow, (manifest.agent_id, declared_at))
    if row is None:
        session.add(
            CapabilityManifestRow(
                agent_id=manifest.agent_id,
                declared_at=declared_at,
                capabilities=list(manifest.capabilities),
                tier=manifest.tier,
                cost_band=manifest.cost_band,
                max_parallel=manifest.max_parallel,
            )
        )
        return True
    same = (
        list(row.capabilities) == list(manifest.capabilities)
        and row.tier == manifest.tier
        and row.cost_band == manifest.cost_band
        and row.max_parallel == manifest.max_parallel
    )
    if not same:
        raise DeclarationTimelineConflict(
            f"{manifest.agent_id} 在 `declared_at`={declared_at.isoformat()} 上已有一份**内容不同**的申报"
            f"｜库里那份 capabilities={list(row.capabilities)} tier={row.tier} "
            f"cost_band={row.cost_band} max_parallel={row.max_parallel}"
            f"｜这次这份 capabilities={list(manifest.capabilities)} tier={manifest.tier} "
            f"cost_band={manifest.cost_band} max_parallel={manifest.max_parallel}"
            "⇒ 拒（两份申报挂同一时刻 = 给时间线编两个版本，R3）；"
            "解法只有换 `declared_at`（承认是新一次申报）或整批回滚，不是覆盖旧行"
        )
    return False


def persist_registrations(
    session: Session,
    registrations: Iterable[Registration] | None = None,
    *,
    declared_at: datetime | None = None,
) -> RegistrationReport:
    """把一批申报落进 EN-03 / EN-05。`declared_at` 由调用方给（测试要可复现，不读时钟）。

    抛 `RegistrationRefused` 时**一行都不落**：先整批验岗，再逐支写。
    **两趟之间显式 `flush`**：EN-05.agent_id 有指向 EN-03 的外键，而这两个 mapper 之间没有
    `relationship()` ⇒ 工作单元不按表间外键排 INSERT 顺序。父行没落地就插子行会撞 `IntegrityError`，
    所以这里不让 SQLAlchemy 去猜顺序。事务边界仍在调用方（`persistence.session.new_session`），
    本模块只 `flush` 不 `commit`。
    """
    batch = list(registrations if registrations is not None else load_registrations())
    if not batch:
        raise RegistryMisuse(
            "注册表里一条都没有 ⇒ 没有可落库的申报。这**不是**'成功注册 0 支'："
            "空批次通常意味着 `agents.yaml` 路径给错了或装载被静默跳过，报成成功会把装配点的缺失读成 fleet 为空"
        )
    stamp = declared_at if declared_at is not None else _now_utc()
    if stamp.tzinfo is None:
        raise RegistryMisuse(
            "`declared_at` MUST 带时区：EN-05 的 `declared_at` 是 `timestamptz` 且是 PK 的一列，"
            f"收到裸 datetime（{stamp.isoformat()}）会被按会话时区解释 ⇒ 同一时刻在两套时区下变成两行"
        )

    mounts = effective_mounts(session)
    for registration in batch:
        check_role_mounts(registration.manifest, mounts)

    created: list[str] = []
    updated: list[str] = []
    for registration in batch:
        if _write_agent_unit(session, registration):
            created.append(registration.manifest.agent_id)
        else:
            updated.append(registration.manifest.agent_id)
    session.flush()

    inserted = 0
    reused = 0
    for registration in batch:
        if _write_manifest_row(session, registration, stamp):
            inserted += 1
        else:
            reused += 1
    session.flush()

    return RegistrationReport(
        declared_at=stamp,
        agents=tuple(r.manifest.agent_id for r in batch),
        en03_created=tuple(created),
        en03_updated=tuple(updated),
        en05_inserted=inserted,
        en05_reused=reused,
    )
