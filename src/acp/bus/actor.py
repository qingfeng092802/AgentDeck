"""T088：只读隔离的**身份生产者** —— 把"这支工具在这张卡上担哪个岗"换成一个带 `read_only` 的 Actor。

**这里补的是哪一个缺口**：`acl.Actor.read_only` 的列注释写着"来自 `role_spec.read_only`（EN-04），
不是请求方自述"，而 Phase 3 落地时它的**生产写者并不存在** —— 三支 transport 构造的都是
`read_only=False` 的执行者身份，网关那道只读判定今天只在测试里被喂过真值。
EN-04 那一行与 `contracts/agent-protocol.md §4` 都把这条记为"仍开的缺口"，本模块是它的闭合点。

**为什么按"卡 + 席位"取，不按"工具"取**：owner 2026-10-13 裁决 1 把 FR-033 的只读定成**任务级**——
同一支工具在**这张卡**上担审查/验收岗时只读，它在别的卡上作执行者照写。
所以身份不是工具的属性，而是"工具 × 卡 × 席位"这三元的属性，本模块的入参就是这个三元，缺一个都不对。

**拒写这件事仍然只在 `bus/gateway.py` 一处**（`acl.can_write`）。本模块只**给身份**，不判路径、不返回布尔，
所以不会出现"两处各判一次只读、两处口径各自漂"的那种形状（原则 I）。
`tests/guards/test_readonly.py` 的三条拒 + 一条放行量的都是网关，这里只是把 `read_only` 填对。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.bus import acl
from acp.guard import triple as gp
from acp.persistence.models_core import Artifact, TaskCard
from acp.persistence.models_registry import RoleSpec

#: 每个席位对应的岗位名。**审查/验收两席直接取 EN-04 的那两个只读岗**（`registry/roles/` 种的），
#: 执行席的岗在卡上（`executor_role_id`，可空）⇒ 不在这里重述，见 `actor_for_seat()`。
SEAT_ROLE = {seat: seat for seat in gp.SEATS if seat != "executor"}


class ActorMisuse(ValueError):
    """身份取不出来。`code` 是稳定键名；消息里只带 id 与席位名，不带产物内容。"""

    code = "actor_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


def _card(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise ActorMisuse(
            f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 没有可取的身份",
            code="card_not_found",
        )
    return card


def read_only_role_ids(session: Session) -> frozenset[str]:
    """EN-04 里标了只读的那些岗。**唯一来源是库，不是 `BUILTIN_ROLES` 那份声明** ——
    声明是种子输入，库里那行才是生效口径（与 `contracts/agent-protocol.md §3.1` 同一句）。
    """
    return frozenset(
        str(row.role_id) for row in session.scalars(select(RoleSpec).where(RoleSpec.read_only.is_(True)))
    )


def actor_for_seat(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    seat: str,
) -> acl.Actor:
    """取出这张卡上这个席位的 Actor。

    三条口径：
    - `agent_id` 从卡上那一列读（指派没做过就是 `None` ⇒ 抛，不发"身份不明"的下发请求）；
    - `role_id` 执行席取 `executor_role_id`（可空 = 未挂岗），把关两席取 EN-04 的那个岗名；
    - `read_only` 只从 EN-04 的 `read_only` 列来，MUST NOT 由调用方传参决定 ——
      能传参就等于请求方自述，那条正是 `acl.Actor` 注释里点名要避免的形状。
    """
    if seat not in gp.SEATS:
        raise ActorMisuse(f"席位 {seat!r} 不在三岗里（{list(gp.SEATS)}）", code="seat_unknown")
    card = _card(session, feature_request_id, task_id)
    agent_id = getattr(card, f"{seat}_agent_id", None)
    if agent_id is None:
        raise ActorMisuse(
            f"卡 {card.id} 的 {seat} 席还没有指派 ⇒ 没有身份可下发（先走 `AssignGuards`）",
            code="seat_unassigned",
        )
    if seat == "executor":
        role_id = card.executor_role_id or acl.NO_ROLE_DECLARED
    else:
        role_id = SEAT_ROLE[seat]
    return acl.Actor(
        actor_id=f"{acl.ADAPTER_OWNER_PREFIX}{agent_id}",
        role_id=str(role_id),
        read_only=role_id in read_only_role_ids(session),
    )


def artifact_paths_for_review(session: Session, *, feature_request_id: uuid.UUID, task_id: str) -> tuple[str, ...]:
    """审查者要读的产物路径（**只读用途**，MUST NOT 被拿去当写位）。

    单列出来是为了让"读什么"与"能写什么"在类型上分开：T088 的下发请求只带产物**正文**
    （`review.build_request()`），路径留在服务端做读取用。给审查者回传可写路径就是邀请它动手。
    """
    rows = session.scalars(
        select(Artifact.rel_path)
        .where(Artifact.feature_request_id == feature_request_id, Artifact.task_card_id == task_id)
        .order_by(Artifact.rel_path)
    ).all()
    return tuple(str(path) for path in rows)
