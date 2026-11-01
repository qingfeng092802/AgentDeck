"""验收与打包两张卡：让"最后两步"在板上留下痕迹。

## 为什么这两格也需要卡

演示里"验收"和"打包"是流程图上最后两个方框，看起来必然发生。真链路里它们确实发生了
（`accept()` 改了卡状态、`assemble()` 写了 `delivery_package.json`），但**板上没有行** ——
于是看板讲到门禁就断了，看的人不知道这单到底交没交。

## 两张卡各说什么，不说什么

- **T96 验收**：只说库里读得到的三件事 —— 每张卡的状态、它声明了几条判据、它的评审结论。
  **不说"第几条 met"**：验收条目挂在 EN-07，逐条判定挂在 EN-08 的 `item_findings` 里，
  这里不做二次解释，要细节去看那两张表。
- **T97 打包**：只说 `DeliveryReport` 里已经量过的事实（入口页、成员数、包大小）。
  `package_bytes` 是从盘上量的 —— 写没写成、写成多大，只有量过一次才算量过。

## 一个顺序上的必然

T97 的产物**不可能**出现在它自己描述的那个包里：`assemble()` 组包读的是组包那一刻的
`validated_manifest()`，而 T97 是组包之后才建的。这不是缺陷，是"清单不能包含它自己"。
所以 T97 的产物是 `delivery-summary.md`（给人看的摘要），包本体仍在公共区由
`OrchestratorWriter` 单点代笔写。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from acp.bus import layout as bus_layout
from acp.persistence.models_approval import AcceptanceMap, ReviewVerdict
from acp.persistence.models_core import TaskCard
from acp.reporting.cards import FAMILY_SUMMARY, add_done_card, verify_card

ACCEPTANCE_CARD_ID = "T96"
DELIVERY_CARD_ID = "T97"

WRITER_AGENT_ID = "workbuddy"

#: 报告门槛：只挡空文件（同 `prefilled` / `checks` 那两处口径）。
_MIN_BYTES = 64
_MIN_LINES = 3

#: 卡到终态的那几个值。与 `assemble.DELIVERED_STATES` 同口径，但这里不 import 它 ——
#: `reporting` 是被 `orchestrate` 依赖的一层，反向 import 会让依赖方向靠运气。
_TERMINAL = ("DELIVERED", "DELIVERED_WITH_ISSUES")


def acceptance_markdown(session: Session, *, feature_request_id: uuid.UUID) -> str:
    """验收汇总正文：状态 + 判据条数 + 评审结论，逐卡一行。"""
    cards = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .order_by(TaskCard.id)
        .all()
    )
    links = session.query(AcceptanceMap).filter(AcceptanceMap.feature_request_id == feature_request_id).all()
    verdicts = session.query(ReviewVerdict).filter(ReviewVerdict.feature_request_id == feature_request_id).all()

    per_card_items: dict[str, int] = {}
    for link in links:
        key = str(link.task_card_id)
        per_card_items[key] = per_card_items.get(key, 0) + 1
    per_card_verdict: dict[str, str] = {str(v.task_card_id): str(v.verdict) for v in verdicts}

    done = [card for card in cards if str(card.status) in _TERMINAL]
    lines = [
        "# 验收汇总",
        "",
        f"- 卡：{len(done)} / {len(cards)} 张到终态",
        f"- 判据条目：{len(links)} 条（EN-07）",
        f"- 评审结论：{len(verdicts)} 份（EN-08）",
        "",
        "## 逐卡",
        "",
    ]
    for card in cards:
        items = per_card_items.get(str(card.id), 0)
        verdict = per_card_verdict.get(str(card.id), "未评审")
        lines.append(f"- `{card.id}` {card.title} — {card.status}，判据 {items} 条，评审 {verdict}")
    if not cards:
        lines.append("（这一单没有任务卡）")
    return "\n".join(lines) + "\n"


def delivery_markdown(report: object) -> str:
    """交付摘要正文：只说 `DeliveryReport` 里量过的那几格。"""
    def field(name: str) -> str:
        return str(getattr(report, name, "—"))

    return "\n".join(
        [
            "# 交付包",
            "",
            f"- 入口页：{field('entry_path')}",
            f"- 成员：{field('manifest_count')} 份",
            f"- 已交付卡：{field('cards_delivered')} 张",
            f"- 包文件：{field('package_path')}（{field('package_bytes')} 字节）",
            "",
            "包本体由公共区单点代笔写出；本卡只记读数，不复制包内容。",
        ]
    ) + "\n"


def _write(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    project_root,
    card_id: str,
    title: str,
    filename: str,
    body: str,
) -> bool:
    """落一张 md 卡。已存在就跳过（重试不撞主键），返回是否真的新建。"""
    present = session.query(TaskCard.id).filter(
        TaskCard.feature_request_id == feature_request_id,
        TaskCard.id == card_id,
    ).scalar()
    if present is not None:
        return False
    workspace = project_root / bus_layout.workspace_dir_name(WRITER_AGENT_ID)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / filename).write_bytes(body.encode("utf-8"))
    add_done_card(
        session,
        feature_request_id=feature_request_id,
        card_id=card_id,
        title=title,
        fmt="md",
        rel_path=f"{workspace.name}/{filename}",
        content=body,
        acceptance=[f"{title} 的报告在 {filename}", "报告里的每个读数都能在库里或盘上复算"],
        writer_agent_id=WRITER_AGENT_ID,
        floor=(_MIN_BYTES, _MIN_LINES),
        # 验收汇总与交付打包都属于"收口那一段"，看板上排在门禁之后。
        family=FAMILY_SUMMARY,
    )
    verify_card(session, feature_request_id=feature_request_id, card_id=card_id)
    return True


def write_acceptance_card(session: Session, *, feature_request_id: uuid.UUID, project_root) -> bool:
    """落 T96 验收汇总卡。调用点：所有卡到终态之后、组包之前。"""
    return _write(
        session,
        feature_request_id=feature_request_id,
        project_root=project_root,
        card_id=ACCEPTANCE_CARD_ID,
        title="验收汇总",
        filename="acceptance.md",
        body=acceptance_markdown(session, feature_request_id=feature_request_id),
    )


def write_delivery_card(session: Session, *, feature_request_id: uuid.UUID, project_root, report: object) -> bool:
    """落 T97 打包卡。调用点：`run_deliver` 返回之后（包已经写完、读数已经量过）。"""
    return _write(
        session,
        feature_request_id=feature_request_id,
        project_root=project_root,
        card_id=DELIVERY_CARD_ID,
        title="交付打包",
        filename="delivery-summary.md",
        body=delivery_markdown(report),
    )
