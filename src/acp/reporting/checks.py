"""把三个**确定性检查器**的结论落成三张卡（E2E / 安全 / 门禁）。

## 为什么是代码而不是"让模型写一份报告"

演示里这三格是模型写报告。那是最假的一格：报告长得像结论，但没有人真的看过产物。
`checks/service.py` 里三个扫描器读的是**产物文件本身**，输出是"哪一条不成立、在哪一行"，
可复算、可复现。本模块只负责把它们接到链路上：喂文件、收报告、落卡。

## 什么时候跑

`review/service.py` 在"所有卡都到终态、准备组交付包"那一刻调本模块（`run_deliver` 之前）。
放在这里而不是 `assemble` 里面，是因为**报告本身要进交付包**：
组包读的是 `validated_manifest()`，只收已判 `valid` 的产物行 ⇒ 检查报告必须在组包**之前**落库并判完。

## 路径归一化（这一格不做就会全红）

产物行的 `rel_path` 形如 `qoder-workspace/index.html`，而页面里的链接写的是 `products.html`。
拿带前缀的 key 去比 `scan_e2e` 的 `target not in files`，**每一页的每一个站内链接都会被判成断链** ——
2026-10-28 第一次接线就是这么红的。所以喂进去之前先按 workspace 去掉前缀，变成同目录相对名。
多 workspace 合并时同名文件会撞：撞了保留先读到的那份，并记一条 `warn`（不静默）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.bus import layout as bus_layout
from acp.checks.service import Report, scan_e2e, scan_gate, scan_security
from acp.docs.service import PRD_SECTION
from acp.persistence.models_core import Artifact, TaskCard
from acp.reporting.cards import FAMILY_GATE, FAMILY_SUMMARY, add_done_card, verify_card

#: 三张检查卡的卡号。固定值而不是 `next_system_ids()`：这三张是**每次交付都同号**的系统节点，
#: 让它们随运行次数漂移，前端与报告里的引用就没法稳定指向。
E2E_CARD_ID = "T93"
SECURITY_CARD_ID = "T94"
GATE_CARD_ID = "T95"

#: 报告落盘的目录名（在工作区里）。与文档卡同一个 workspace，理由同 `prefilled.DOC_WRITER_AGENT_ID`。
WRITER_AGENT_ID = "workbuddy"

#: 报告这类 md 的门槛。同 `prefilled` 那条教训：门槛只挡"空文件"，不判"报告写得好不好"。
_MIN_BYTES = 64
_MIN_LINES = 3


@dataclass(frozen=True)
class CheckOutcome:
    """一次检查的结论：报告正文 + 它自己的判据 + 通过与否。"""

    card_id: str
    title: str
    filename: str
    report: Report
    #: 流水线哪一段。E2E 与安全扫描是"门禁"，门禁汇总读它们、属于"收口"。
    #: 看板按它分行（`reporting/cards.py::FAMILIES`），所以它是数据不是装饰。
    family: str = FAMILY_GATE

    @property
    def body(self) -> str:
        return self.report.markdown()


def _prd_pages(digest: Mapping[str, Sequence[str]]) -> list[str]:
    """PRD 里的页面文件名清单，喂给 `scan_e2e` 的 `expect_links`（"要求产出的页有没有产出"）。"""
    values = digest.get(PRD_SECTION) or ()
    if not values:
        return []
    try:
        prd = json.loads(values[0])
    except (json.JSONDecodeError, TypeError, IndexError):
        return []
    if not isinstance(prd, dict):
        return []
    return [
        str(page.get("file"))
        for page in (prd.get("pages") or ())
        if isinstance(page, dict) and page.get("file")
    ]


def relative_name(rel_path: str) -> str:
    """产物行的 `rel_path` → 同 workspace 的相对名。

    `qoder-workspace/index.html` → `index.html`；不是 workspace 形态的路径原样返回。
    为什么必须归一化：页面里的链接写的是 `products.html`，而 `scan_e2e` 用
    `target not in files` 判断链 —— 拿带前缀的 key 去比，**每一页的每个站内链接都会红**。
    """
    parts = str(rel_path).split("/", 1)
    if len(parts) == 2 and bus_layout.workspace_agent_id(parts[0]):
        return parts[1]
    return str(rel_path)


def _files_of(session: Session, feature_request_id: uuid.UUID, project_root) -> dict[str, str]:
    """已入库产物 → `{同 workspace 相对名: 内容}`。

    只读**已入库**的产物行，不扫盘：盘上可能有执行器留下的临时文件、上一轮的残留、
    或者根本没被 `collect()` 认领的东西。拿它们去判"这一单交付了什么"，判的就不是这一单。
    """
    rows = (
        session.query(Artifact)
        .filter(Artifact.feature_request_id == feature_request_id)
        .order_by(Artifact.rel_path)
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        path = project_root / str(row.rel_path)
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.setdefault(relative_name(str(row.rel_path)), body)
    return out


def _gate_rows(session: Session, feature_request_id: uuid.UUID) -> list[dict[str, object]]:
    """`scan_gate` 的入参：每张卡的声明下限 ⇄ 它已有产物的实测值。"""
    cards = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .order_by(TaskCard.id)
        .all()
    )
    rows = session.query(Artifact).filter(Artifact.feature_request_id == feature_request_id).all()
    by_card: dict[str, list[Artifact]] = {}
    for row in rows:
        by_card.setdefault(str(row.task_card_id), []).append(row)

    out: list[dict[str, object]] = []
    for card in cards:
        own = by_card.get(str(card.id), [])
        same = [row for row in own if str(row.format) == str(card.artifact_floor_format)]
        picked = same[0] if same else (own[0] if own else None)
        out.append(
            {
                "id": str(card.id),
                "title": str(card.title),
                "format": str(card.artifact_floor_format),
                "bytes": int(picked.bytes) if picked else 0,
                "lines": int(picked.lines) if picked else 0,
                "min_bytes": int(card.artifact_floor_min_bytes),
                "min_lines": int(card.artifact_floor_min_lines),
                "criteria": list(card.acceptance_criteria or ()),
            }
        )
    return out


def run_checks(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    project_root,
    summary_digest: Mapping[str, Sequence[str]] | None = None,
) -> tuple[CheckOutcome, ...]:
    """跑三个检查器并把报告落成三张卡。返回本次的结论（调用方据此报读数）。

    **只 flush 不 commit**：与 `_normalize` 里那几处同一条纪律 —— 事务边界归调用方。
    """
    files = _files_of(session, feature_request_id, project_root)
    # 先算上游两份，再让门禁汇总读它们（U5）。三份都只是纯函数调用，顺序在这里没有副作用；
    # 但"汇总必须能看见上游"这件事只能靠这个顺序表达 —— 并列写在元组里就传不进去了。
    e2e = scan_e2e(files, expect_links=_prd_pages(summary_digest or {}))
    security = scan_security(files)
    outcomes = (
        CheckOutcome(
            card_id=E2E_CARD_ID,
            title="E2E 结构检查",
            filename="e2e-report.md",
            report=e2e,
        ),
        CheckOutcome(
            card_id=SECURITY_CARD_ID,
            title="安全静态扫描",
            filename="security-report.md",
            report=security,
        ),
        CheckOutcome(
            card_id=GATE_CARD_ID,
            title="门禁汇总",
            filename="gate-report.md",
            report=scan_gate(_gate_rows(session, feature_request_id), upstream=(e2e, security)),
            # 汇总读上游两份，它自己不再被别的门禁读 ⇒ 归"收口"那一段。
            family=FAMILY_SUMMARY,
        ),
    )

    workspace = project_root / bus_layout.workspace_dir_name(WRITER_AGENT_ID)
    workspace.mkdir(parents=True, exist_ok=True)
    # 幂等：交付那一步失败后重试时，这三张卡可能已经在了。主键是复合 `(需求单, 卡号)`，
    # 重复 `add_done_card` 会撞主键并让整次交付挂掉 —— 而"上次检查过了"不该让这次交付失败。
    # 只补缺的那几张：报告是按当前产物算的，已存在的那张留着不动（它的结论对应当次产物）。
    #
    # 用 `scalars` 取纯值，**不要 `query(TaskCard.id)` 再 `str(row)`**：单列 query 交回的是
    # `Row` 元组，`str(row)` 是 `"('T93',)"` 而不是 `"T93"` ⇒ 集合里永远比不中 ⇒ 幂等从未生效。
    # 2026-10-28 真跑实测：第二次组包直接撞 `pk_task_card`（T93 已存在），整次交付挂掉。
    present = set(
        session.scalars(
            select(TaskCard.id).where(
                TaskCard.feature_request_id == feature_request_id,
                TaskCard.id.in_((E2E_CARD_ID, SECURITY_CARD_ID, GATE_CARD_ID)),
            )
        )
    )
    for outcome in outcomes:
        if outcome.card_id in present:
            continue
        (workspace / outcome.filename).write_bytes(outcome.body.encode("utf-8"))
        add_done_card(
            session,
            feature_request_id=feature_request_id,
            card_id=outcome.card_id,
            title=outcome.title,
            fmt="md",
            rel_path=f"{workspace.name}/{outcome.filename}",
            content=outcome.body,
            acceptance=outcome.report.acceptance(),
            writer_agent_id=WRITER_AGENT_ID,
            floor=(_MIN_BYTES, _MIN_LINES),
            family=outcome.family,
        )
        # 判一次，让报告产物进得了交付包（`validated_manifest` 只收 `valid`）。
        # 报告里写"不通过"不影响这一步：产物是"一份说明检查没过"的报告，它本身是有效的。
        verify_card(session, feature_request_id=feature_request_id, card_id=outcome.card_id)
    return outcomes
