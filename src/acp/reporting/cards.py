"""把"已经发生的事实"落成一张已交付的任务卡（文档 / 检查 / 验收 / 打包）。

为什么需要这一格：看板画的是 EN-02 的行。文档节点与检查器产出的东西**确实发生了**
（文件在盘上、结论在留痕里），但没有行 ⇒ 屏上就是没有它们。于是"13 个阶段"永远只是
演示里的一张图，而不是这一单真走过的路。

四条纪律：

1. **不伪造执行**：`status` 直接写 `DELIVERED`，`review_round=0`、`handoff_count=0` ——
   这几张卡没有经过"派单→执行→评审"，它们本来就不是执行卡。写一个假的 1 反而会让
   门禁统计把"系统节点"算成"一次人工评审"。
2. **存档点复用同一单的**：`snapshot_id` 是 NOT NULL，而这几张卡与拆单卡属于同一次开工 ——
   拿同一张需求单已有的存档点，而不是再造一个（再造会在 EN-12 里多出一条并不存在的"开工前"）。
   没有存档点就**不写**：宁可这一格缺席，也不要编一个存档点。
3. **产物带实测值**：`bytes` / `lines` 由内容算出，`validated_status` 留给门禁覆写；
   自己填 `valid` 等于自称过了门禁。
4. **卡号落在高位段**（T90+）：拆单卡的 T01..T06 是执行链的编号，这两类混在一起排序会乱。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.config.runtime import DEFAULT_ARTIFACT_FLOORS
from acp.persistence.models_approval import AcceptanceMap
from acp.persistence.models_core import Artifact, TaskCard

#: 系统节点卡的号段起点。拆单卡从 T01 起、上限 6 张（`llm_split.MAX_CARDS`），
#: 90 起的这一段落差足够，且仍然是 `T\d+` 形态 —— 不引入第二套编号语法。
SYSTEM_CARD_BASE = 90

#: 每种格式的判据下限（字节 / 行）。**不再另写一份字面量**：与拆单侧共用
#: `runtime.DEFAULT_ARTIFACT_FLOORS`，系统卡不跟随 `runtime.yaml` 的运行期改动
#: （它们是"文档/检查/验收"的固定下限，不是客户页面卡）。
_FLOORS: dict[str, tuple[int, int]] = dict(DEFAULT_ARTIFACT_FLOORS)


class CardNotWritten(RuntimeError):
    """这一张写不进去。`code` 进留痕，不吞。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _snapshot_of(session: Session, feature_request_id: uuid.UUID) -> uuid.UUID | None:
    return session.scalar(
        select(TaskCard.snapshot_id).where(TaskCard.feature_request_id == feature_request_id).limit(1)
    )


#: 系统节点卡在 `skill_tags` 里打的标记。**唯一的可判谓词** ——
#: 不按卡号段猜（T90+ 是分配结果，不是性质），也不按 `status == DELIVERED` 猜
#: （执行卡跑完也是 DELIVERED，那会把真正跑完的执行卡排除在审批之外）。
SYSTEM_TAG = "system"

#: 一张卡属于流水线的哪一段。前端执行看板按它分行（owner 2026-10-28：
#: 「不同任务的卡片竖向排列，同类任务横向排列」）——
#: 文档一行、执行卡按依赖逐层向下、门禁一行、汇总一行。
#:
#: **为什么写进 `skill_tags` 而不是新开一列**：这一格是"这张卡是谁落成的"的延伸事实，
#: 而 `skill_tags` 本来就是那张卡的标签集合（`is_system_card` 已经在读它）。
#: 新开一列要一次迁移，而这一格的取值完全由**创建点**决定 —— 创建点知道自己是哪一段，
#: 没有"事后猜"的成分。这也是 `is_system_card` 那条注释的同一句话：不按卡号段猜。
FAMILY_DOC = "doc"
FAMILY_BUILD = "build"
FAMILY_GATE = "gate"
FAMILY_SUMMARY = "summary"
FAMILIES = (FAMILY_DOC, FAMILY_BUILD, FAMILY_GATE, FAMILY_SUMMARY)


def family_of(card) -> str:
    """这张卡属于哪一段。判据只有一条：标签里有没有那四个词之一。

    没有就是 `build` —— 拆单拆出来的执行卡带的是能力标签（`frontend_coding` 之类），
    它们一个都不在这四个词里，所以"没标"与"执行卡"是同一件事，不需要第二个默认分支。
    """
    tags = getattr(card, "skill_tags", None) or ()
    for name in (FAMILY_DOC, FAMILY_GATE, FAMILY_SUMMARY, FAMILY_BUILD):
        if name in tags:
            return name
    return FAMILY_BUILD


def is_system_card(card: TaskCard) -> bool:
    """这张卡是不是"系统节点落成的卡"（文档 / 检查 / 验收 / 打包）。

    为什么要能判：这些卡**建出来就是 `DELIVERED`**，它们没有经过"派单→执行→评审"，
    因此也不该进 A 类卡点的审批对象集。2026-10-28 真跑实测：不排除它们，
    `run_gate._set_card_status` 的入态守卫会把 T90–T92 判成"不在合法入态上"，
    整条受理以 `GateMisuse` 结束、卡全部回滚、需求单永久停在 `INTAKE`。
    """
    return SYSTEM_TAG in (getattr(card, "skill_tags", None) or ())


def next_system_ids(session: Session, feature_request_id: uuid.UUID, count: int) -> list[str]:
    """接着这一单已有的高位号往后排。重跑（重新立项除外）不会撞号。"""
    taken = set(
        session.scalars(
            select(TaskCard.id).where(TaskCard.feature_request_id == feature_request_id)
        )
    )
    out: list[str] = []
    cursor = SYSTEM_CARD_BASE
    while len(out) < count:
        candidate = f"T{cursor:02d}"
        if candidate not in taken:
            out.append(candidate)
        cursor += 1
    return out


def add_done_card(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    card_id: str,
    title: str,
    fmt: str,
    rel_path: str,
    content: str,
    acceptance: Sequence[str],
    depends_on: Sequence[str] = (),
    writer_agent_id: str,
    floor: tuple[int, int] | None = None,
    now: datetime | None = None,
    family: str = FAMILY_DOC,
) -> None:
    """写"一张卡 + 一份产物"。调用方负责先 `flush()` 存档点（与拆单节点同一条保序规则）。

    `floor` 是**调用方给这一张卡声明的门槛**（字节 / 行），不给才按格式取默认值。
    为什么门槛要能由调用方给：`_FLOORS` 只能按格式一刀切，而同样是 md，
    一份 README 是十几行、一份架构只有五行（"选型/理由/风险"三句话）。
    2026-10-28 实测：只有一页的 PRD 渲染出来 127B，拿 md 那档 200B 去卡它，
    这张卡会在门禁里被判 invalid —— 门槛按"典型内容"拍数字，就会在内容合法但简短时误伤。
    """
    snapshot_id = _snapshot_of(session, feature_request_id)
    if snapshot_id is None:
        raise CardNotWritten(
            "system_card_without_snapshot",
            "这一单还没有存档点 ⇒ 不写卡：编一个存档点会让 EN-12 多出一条并不存在的'开工前'",
        )
    if not acceptance:
        raise CardNotWritten("system_card_without_acceptance", f"{card_id} 没有判据 ⇒ 门禁无从比对")

    low_bytes, low_lines = floor if floor is not None else _FLOORS.get(fmt, (512, 12))
    body = content.encode("utf-8")
    session.add(
        TaskCard(
            id=card_id,
            feature_request_id=feature_request_id,
            title=title,
            difficulty=1,
            skill_tags=[SYSTEM_TAG, family],
            depends_on=list(depends_on),
            # 这三岗对系统节点不成立：没有"谁执行 / 谁审查 / 谁验收"三件事。
            executor_agent_id=None,
            executor_role_id=None,
            reviewer_agent_id=None,
            acceptor_agent_id=None,
            artifact_floor_min_bytes=low_bytes,
            artifact_floor_min_lines=low_lines,
            artifact_floor_format=fmt,
            acceptance_criteria=list(acceptance),
            handoff_count=0,
            review_round=0,
            status="DELIVERED",
            snapshot_id=snapshot_id,
        )
    )
    # 与 `graph.py` 里那两道 flush 同一条理由：两个 mapper 之间没有 relationship，
    # UoW 不按复合外键排 INSERT 顺序 ⇒ 卡必须先发出去，产物才指得过去。
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=feature_request_id,
            task_card_id=card_id,
            writer_agent_id=writer_agent_id,
            rel_path=rel_path,
            bytes=len(body),
            lines=max(1, content.count("\n") + (0 if content.endswith("\n") else 1)),
            format=fmt,
            self_reported_status="success",
            # 留给门禁：写卡的人不自称"已验过"。
            validated_status=None,
            invalid_reason=None,
            created_at=now or datetime.now(timezone.utc),
        )
    )
    # 判据同时落 EN-07（U4，2026-10-28 真跑）。原来只写 `task_card.acceptance_criteria`，
    # 于是验收汇总里 T90–T95 全是「判据 0 条」，而 `acceptance.md` 顶部又按 EN-07 数出
    # 「判据条目：10 条」—— 同一个文件里两处口径对不上，读的人没法判断到底哪句是真的。
    #
    # `requirement_item` 取卡标题而不是需求原文：EN-07 是"需求条目 → 验收条目"的映射，
    # 系统节点卡对应的需求条目就是"这三份文档/这几份报告"，标题是它唯一的书面表达。
    for item in acceptance:
        session.add(
            AcceptanceMap(
                id=uuid.uuid4(),
                feature_request_id=feature_request_id,
                requirement_item=title,
                acceptance_item=str(item),
                task_card_id=card_id,
                produced_by=writer_agent_id,
            )
        )
    session.flush()


def verify_card(session: Session, *, feature_request_id: uuid.UUID, card_id: str) -> str:
    """给系统节点卡判一次，把结论写回 `validated_status`，返回 `"valid"` / `"invalid"`。

    **为什么不调 `validate_card`**：那一条的第一句就是"卡现在不是 `RUNNING` ⇒ 这次判定没有对应的
    在跑任务"，而系统节点卡（文档 / 检查 / 打包）建出来就是 `DELIVERED` —— 它们本来就没有"在跑"这一刻。
    判定规则本身仍是同一份：`assess()` 就是 `validate_card` 的纯判定核，本函数只是跳过那条前置。

    **为什么必须判**：`assemble.validated_manifest()` 只收 `validated_status == "valid"` 的产物行。
    不判 ⇒ 系统节点卡的产物永远进不了交付包，而板上却显示"已完成" —— 那是本仓库最不能接受的那种形状：
    看着完成了，交付包里没有。

    `assess` 用局部 import：`reporting` 是被 `orchestrate` 依赖的一层，模块级反向 import 会让
    "谁能依赖谁"这件事变得靠运气（此刻不成环，但下一次改动就可能成环）。判定的这一刻才需要它。
    """
    from acp.orchestrate.nodes.validate_artifact import assess

    card = session.get(TaskCard, {"feature_request_id": feature_request_id, "id": card_id})
    if card is None:
        raise CardNotWritten("system_card_absent", f"{card_id} 不在库里 ⇒ 没有可判的对象")
    rows = list(
        session.query(Artifact)
        .filter(
            Artifact.feature_request_id == feature_request_id,
            Artifact.task_card_id == card_id,
        )
        .all()
    )
    verdict = assess(card, rows)
    for row in rows:
        row.validated_status = verdict.validated_status
        row.invalid_reason = verdict.invalid_reason
    session.flush()
    return str(verdict.validated_status)
