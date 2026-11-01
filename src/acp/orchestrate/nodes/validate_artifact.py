"""T075 / T076 / T077：产物有效性门禁 —— 独立节点 + 两条出边 + `validated_status` 覆写（FR-017 ~ FR-019｜R-02）。

**为什么它是一个节点而不是一段边条件**：`plan.md` 设计项 5 定的是"校验做成图上独立节点"，
而 `data-model.md` 的状态机把 `RUNNING → REVIEWING` 的**唯一前件**写成 `artifact.validated_status = valid` ——
那个前件是一次**覆写**（要写库、要留痕），不是纯粹的读判定。边条件里写库就等于把"谁盖章"交给了路由，
而路由在 LangGraph 里可能被重放。所以：判定与覆写都在这里，路由只读这里写出去的结论。

**门禁只认产物实况，不认自评**（FR-019 的"防虚报"）。三件输入信号里，`self_reported_status`
在本节点**完全不参与判定** —— 它只被原样读出来放进留痕，好让人看见"工具当时说了什么"。
判据的全部依据是 `ResultIngest` 记下的那三个实测值。这条边界最容易在一处"顺手相信自评"的代码里被破，
所以 `assess()` 的入参里根本没有那个字段。

**多项产物怎么聚合（文档没写，此处择一，等你过目）**：
卡声明的下限是"这份交付至少多大"，且 `artifact_floor_format` 是**单值** ⇒ 本节点按
"**在同一格式的产物里，至少有一份同时过体积与行数两项**"判定；不在这个格式里的产物
（比如同一张卡顺手落的 `styles.css`）**既不救卡也不害卡**。
- 取这个口径的理由：`min_bytes` / `min_lines` 加在不同格式的文件上没有意义（html 的 12 行与 png 的 12 行
  不是同一个底线），而"每张卡只有一份交付物"在本层既没有列也没有约束支撑。
- 被它放过的形状要点名：一张卡里有一份达标交付物 + 一堆空文件 ⇒ 判 `valid`。
  这条由 `test_a_companion_file_in_another_format_neither_saves_nor_sinks_the_card` 钉住，
  它同时也是"若将来改口径为『每一份都必须达标』"时**唯一需要翻**的用例。
- 没取的另外两种：①每一份都必须独立达标（会把多文件交付的卡全判死，R-07 那个静态网站样例第一步就撞）；
  ②按求和算下限（把不同格式的行数/字节相加，量的是目录不是交付物）。

**状态侧的分工**（`data-model.md` 状态机原文）：
`invalid` ⇒ 本节点把卡置 `ARTIFACT_INVALID`（下一跳是 `HandOff`，T081）；
`valid` ⇒ 本节点**不改卡状态**，仍是 `RUNNING` —— 进 `REVIEWING` 是 `Review` 节点（Phase 7）的动作，
在这里提前写就会把"审查真的开始了"读成"门禁过了"，而这两件事的失败方向不同。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy.orm import Session

from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.persistence import events
from acp.persistence.models_core import Artifact, TaskCard

NODE = "validate_artifact"

#: `validated_status` 的取值域来源是 PG 枚举（`enums.py`），这里只引用不重述。
VALID = "valid"
INVALID = "invalid"

#: 出边只有两条（T076）：结论 → 下一跳的名字。这张表是"出边仅两条"这条判据的唯一读法。
OUT_EDGES: dict[str, str] = {VALID: "review", INVALID: "handoff"}

#: 不达标时卡的新状态（`data-model.md` 状态机里 `有效性门禁` 那一支）。
INVALID_STATE = "ARTIFACT_INVALID"
#: 门禁只在任务跑着的时候判它；别的状态来说明这次回投没有对应的在跑任务。
VALIDATABLE = "RUNNING"


class ValidationMisuse(ValueError):
    """门禁的输入形状不成立（卡不存在 / 产物不是这张卡这一批的 / 同一批被判了两次 / 结论读不出）。"""

    def __init__(self, message: str, *, code: str = "validation_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Verdict:
    """一次门禁判定。`invalid_reason` 与 `validated_status` 互斥，形状上就不允许"invalid 但没理由"。"""

    task_id: str
    validated_status: str
    invalid_reason: str | None
    examined: int
    self_reported_status: str | None

    @property
    def valid(self) -> bool:
        return self.validated_status == VALID

    def to_state(self) -> dict:
        """给图用的增量：只有结论，没有产物内容 —— 瞬态与实态各归各处（原则 I）。"""
        return {"validated_status": self.validated_status, "invalid_reason": self.invalid_reason}


def _worst_of(rows: Sequence[Artifact]) -> str:
    """把同格式的实测值按路径排序列出来 —— 不排名、不选"最差的那一份"，只把数全摆出来。

    排序是为了**可重放**：`invalid_reason` 是要落库的字符串，取哪一份当代表就会让同一批产物
    在两次运行里给出两串不同的话。
    """
    return "、".join(f"{row.rel_path}({row.bytes}B/{row.lines}行)" for row in sorted(rows, key=lambda r: r.rel_path))


def assess(card: TaskCard, rows: Sequence[Artifact]) -> Verdict:
    """纯判定：卡上的三项下限 ⇄ 这一批产物的实测值。不读自评，不写库。"""
    declared = [row for row in rows if row.format == card.artifact_floor_format]
    if not rows:
        return Verdict(
            task_id=card.id,
            validated_status=INVALID,
            invalid_reason=f"这张卡没有任何产物行可判（自评再高也是空交付）；下限={card.artifact_floor_min_bytes}B"
            f"/{card.artifact_floor_min_lines}行/{card.artifact_floor_format}",
            examined=0,
            self_reported_status=None,
        )
    if not declared:
        got = sorted({row.format for row in rows})
        return Verdict(
            task_id=card.id,
            validated_status=INVALID,
            invalid_reason=f"格式下限 {card.artifact_floor_format}：这批 {len(rows)} 份产物里没有一份是它（实测 {got}）",
            examined=len(rows),
            self_reported_status=None,
        )
    passing = [
        row
        for row in declared
        if row.bytes >= card.artifact_floor_min_bytes and row.lines >= card.artifact_floor_min_lines
    ]
    if passing:
        return Verdict(
            task_id=card.id,
            validated_status=VALID,
            invalid_reason=None,
            examined=len(rows),
            self_reported_status=passing[0].self_reported_status,
        )
    shortfall = _worst_of(declared)
    return Verdict(
        task_id=card.id,
        validated_status=INVALID,
        invalid_reason=(
            f"体积/行数下限 {card.artifact_floor_min_bytes}B/{card.artifact_floor_min_lines}行："
            f"格式 {card.artifact_floor_format} 的产物里没一份同时过两项（实测 {shortfall}）"
        ),
        examined=len(rows),
        self_reported_status=declared[0].self_reported_status,
    )


def _card_of(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise ValidationMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 没有下限可比")
    return card


_PLACEHOLDER = "lorem ipsum"

#: 藏横向溢出的写法与"至少还得有一个宽度约束"的四种形态。
#: 这条判据的由来（2026-10-30 三发真模型直跑）：统筹模型把需求写成「375px 视口下页面无横向滚动条」，
#: 而 `overflow-x:hidden` 恰好**满足这句话**——滚动条没了，内容是被裁掉还是适配了没人知道。
#: 三发真实产物（A/B/C）都同时带着 `width:100%`、`max-width:`、`box-sizing:border-box` ⇒ 一条都不该红；
#: 它抓的是"只藏不约束"那种裸形态，判据里那份合成产物就是它的正对照。
_OVERFLOW_HIDE = re.compile(r"overflow-x\s*:\s*hidden", re.I)
_WIDTH_CONSTRAINTS = (
    re.compile(r"width\s*:\s*100%", re.I),
    re.compile(r"max-width\s*:", re.I),
    re.compile(r"box-sizing\s*:\s*border-box", re.I),
    re.compile(r"@media", re.I),
)
_LINKED_CSS = re.compile(r"""<link[^>]*href\s*=\s*["']([^"']+?\.css)["']""", re.I)


def _linked_style_text(html: str, page: Path) -> str:
    """把页面引的**同目录本地**样式表正文并进来。外链、越界路径、读不到的都跳过。

    跳过是"这一格判不了就不判"，不是"判它违规"：门禁误杀一份正常交付的代价，
    比放过一份裸 `overflow-x:hidden` 的代价高（后者还有审查席与人工验收两道）。
    """
    root = page.parent.resolve()
    parts: list[str] = []
    for href in _LINKED_CSS.findall(html):
        if href.startswith(("http://", "https://", "//", "data:")):
            continue
        candidate = (root / href.split("?")[0].split("#")[0]).resolve()
        if candidate.parent != root or not candidate.is_file():
            continue
        try:
            parts.append(candidate.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


from acp.content_gate import artifact_completeness


def _artifact_text(row: Artifact, bus_root: Path) -> tuple[str | None, str | None]:
    """读盘：`(正文, 拒绝理由)`。读不到时正文为 `None`、理由非空。"""
    target = Path(bus_root) / str(row.rel_path)
    try:
        return target.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"{row.rel_path} 读不到（{type(exc).__name__}）⇒ 无法确认产物内容"


def _completeness_rejection(row: Artifact, bus_root: Path) -> str | None:
    """**全部格式**的完整性判定（截断零容忍）。

    与内容质量的聚合口径不同：质量那一半只在"同一格式的达标产物全坏"时才判死（一份坏
    companion 不该拖死一张好卡），而**截断**任何一份都判死 —— 一个写到一半的文件进了交付包，
    下游看到的是"文件在、内容错"，比一次干净失败难查一个量级。
    """
    text, missing = _artifact_text(row, bus_root)
    if missing:
        return missing
    reason = artifact_completeness(str(row.format), str(text or ""))
    return f"{row.rel_path} {reason}" if reason else None


def _quality_rejection(row: Artifact, bus_root: Path) -> str | None:
    """HTML 的内容质量（占位文案 / 只藏不约束的横向溢出）。非 HTML 一律 `None`。

    只看 HTML：占位判定盯 `lorem ipsum`，出现 ≥3 行且占非空行的一半以上才算"全是占位"
    —— 一条示例文案不会误伤一份正常页面。
    """
    if str(row.format) != "html":
        return None
    text, missing = _artifact_text(row, bus_root)
    if missing:
        return missing
    assert text is not None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lorem = [line for line in lines if _PLACEHOLDER in line.casefold()]
    if len(lorem) >= 3 and len(lorem) * 2 >= len(lines):
        return f"{row.rel_path} 有 {len(lorem)}/{len(lines)} 行是 lorem ipsum 占位内容"
    target = Path(bus_root) / str(row.rel_path)
    combined = f"{text}\n{_linked_style_text(text, target)}"
    if _OVERFLOW_HIDE.search(combined) and not any(pattern.search(combined) for pattern in _WIDTH_CONSTRAINTS):
        return (
            f"{row.rel_path} 用 overflow-x:hidden 藏掉横向溢出，"
            "但整页与它引的样式表里没有任何宽度约束（width:100% / max-width / box-sizing:border-box / @media）"
            "⇒ 内容是被裁掉还是适配无从判定"
        )
    return None


def _content_rejection(row: Artifact, bus_root: Path) -> str | None:
    """内容闸的单点入口：先判完整性（全部格式），再过 HTML 质量。返回原因，`None` = 可用。"""
    return _completeness_rejection(row, bus_root) or _quality_rejection(row, bus_root)


def validate_card(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    artifact_ids: Sequence[uuid.UUID],
    created_at: dt.datetime,
    actor_agent_id: str = AUTOMATED_ACTOR,
    bus_root: Path | None = None,
) -> Verdict:
    """判定 + **覆写** `validated_status` + 不达标时改卡态 + 一条 EN-14 留痕；只 flush 不 commit。

    `artifact_ids` 必填且必须是这一次回收写进去的那一批（`ResultIngest.record_result()` 的返回值）。
    不给"读这张卡的全部产物行"的默认路径，是因为换人重试会在同一张卡上再攒一批新行：
    默认读全部 = 第二次尝试可以拿第一次的产物过门禁。

    `bus_root` 给了就多做一次**内容有效性**检测（P1-16）：字节/行数达标但 HTML 缺 `</html>`
    或整页是 lorem ipsum 占位 ⇒ 判 `invalid`。缺省 `None` = 只做数值判定（测试/旧调用面）；
    生产两条执行路径都显式交项目根。
    """
    card = _card_of(session, feature_request_id, task_id)
    if card.status != VALIDATABLE:
        raise ValidationMisuse(
            f"卡 {task_id} 现在是 {card.status}，不是 {VALIDATABLE} ⇒ 这次判定没有对应的在跑任务",
            code="task_not_running",
        )
    # `artifact_ids` 是**必填关键字参数**，所以"调用方省略它"这条路径在 Python 层面就已经不存在（TypeError）。
    # 这里因此**不**把空批次当误用：一张卡本次什么都没交回来，正是 FR-019 那句"自评 success + 空产物"的形状，
    # 它必须由 `assess()` 判成 `invalid` 并让路由把卡送去 `HandOff` —— 在这一层抛误用 = 空交付绕过了门禁。
    ids = list(artifact_ids)
    rows = list(session.query(Artifact).filter(Artifact.id.in_(ids)).all()) if ids else []
    missing = sorted({str(i) for i in artifact_ids} - {str(row.id) for row in rows})
    if missing:
        raise ValidationMisuse(f"这些产物行不在库里：{missing} ⇒ 门禁拒绝凭不存在的实测值下结论")
    foreign = sorted(
        {row.rel_path for row in rows if row.task_card_id != task_id or row.feature_request_id != feature_request_id}
    )
    if foreign:
        raise ValidationMisuse(
            f"这批 id 里有不属于卡 {task_id} 的产物：{foreign} ⇒ 拿别的卡的交付过本卡门禁是造假"
        )
    already = sorted(row.rel_path for row in rows if row.validated_status is not None)
    if already:
        raise ValidationMisuse(
            f"这些产物行已经被判过（{already}）⇒ 同一批实测值过两次门禁，后一次的结论没人负责"
        )

    verdict = assess(card, rows)
    if verdict.valid and bus_root is not None:
        # 两档口径，故意不同（2026-10-31 四发真模型矩阵后定的）：
        # ① **截断**看全部产物，任何一份写了一半就判死 —— 半份文件进了交付包，下游见到的是
        #    "文件在、内容错"，比一次干净失败难查一个量级；
        # ② **内容质量**只在"同一格式的达标产物全坏"时才判死 —— 一份坏的 companion 文件
        #    不能把一张好卡拖死，与 `assess()` 的聚合口径保持一致。
        truncated = [reason for reason in (_completeness_rejection(row, bus_root) for row in rows) if reason]
        if truncated:
            verdict = Verdict(
                task_id=verdict.task_id,
                validated_status=INVALID,
                invalid_reason="产物不完整：" + "；".join(truncated),
                examined=verdict.examined,
                self_reported_status=verdict.self_reported_status,
            )
        else:
            passing = [
                row
                for row in rows
                if row.format == card.artifact_floor_format
                and row.bytes >= card.artifact_floor_min_bytes
                and row.lines >= card.artifact_floor_min_lines
            ]
            rejections = [(row, _quality_rejection(row, bus_root)) for row in passing]
            if passing and all(reason for _row, reason in rejections):
                verdict = Verdict(
                    task_id=verdict.task_id,
                    validated_status=INVALID,
                    invalid_reason="内容有效性：" + "；".join(str(reason) for _row, reason in rejections),
                    examined=verdict.examined,
                    self_reported_status=verdict.self_reported_status,
                )
    for row in rows:
        row.validated_status = verdict.validated_status
        row.invalid_reason = verdict.invalid_reason
    if not verdict.valid:
        card.status = INVALID_STATE
    events.record(
        session,
        entity="task_card",
        entity_id=task_id,
        kind="ARTIFACT",
        actor_agent_id=actor_agent_id,
        payload={
            "node": NODE,
            "validated_status": verdict.validated_status,
            "invalid_reason": verdict.invalid_reason,
            "self_reported_status": verdict.self_reported_status,
            "examined": verdict.examined,
            "card_status_after": card.status,
            "floors": {
                "min_bytes": card.artifact_floor_min_bytes,
                "min_lines": card.artifact_floor_min_lines,
                "format": card.artifact_floor_format,
            },
        },
        created_at=created_at,
    )
    session.flush()
    return verdict


def route_after_validation(state: Mapping) -> str:
    """T076 的出边：只读结论，不重新判定，也不给"读不到就按 valid 放行"的兜底。

    两个取值之外一律抛：把缺失的 `validated_status` 默认成某一条边，表现就是"门禁没跑也往下走"，
    而那正是 FR-018 要挡的那类事。
    """
    status = state.get("validated_status")
    if status not in OUT_EDGES:
        raise ValidationMisuse(
            f"路由读到的结论是 {status!r}，不在 {sorted(OUT_EDGES)} 里 ⇒ 门禁没给结论或给了个域外的值",
            code="route_source_missing",
        )
    return OUT_EDGES[status]
