"""T057：卡点超时的**数值**从 `config/params-ref.md §1` 读，**结构约束**写进代码（FR-043｜R-26，宪法 V）。

分家的理由是宪法把两件事放在两个地方：
- `params-ref §1` 明写"本表只承载时长与次数"，改这些值属引用型参数变更（PATCH）；
- 两条结构约束（"A / C / D / E 不得自动放行""B 仅在跑满验收轮次上限后收敛"）§1 的注释句
  （`params-ref.md:30`）点名"留在宪法正文，不在此表"，`contracts/approval-api.md §5` 的末句
  更进一步："上表四条结构约束改动需走宪法修订"。
⇒ 把结构约束做成可配置的数，等于让一次 PATCH 悄悄改掉一条 NON-NEGOTIABLE；所以这里
**只有时长 / 延长步长 / 延长次数从表里读**，能不能自动放行这件事**恒由代码判定**。

**轮次上限的来源不是本表**：`data-model.md:46`（EN-02 `review_round` 行，C9 修正）明写
"上限取自 `constitution.md` **原则 III** 的硬值……**本值不是引用型参数，MUST NOT 下沉到 `params-ref`**；
本表不复述数字以免三处漂移"。所以 `review_round_limit()` 读 `.specify/memory/constitution.md`。
但 §1 的 B 行**确实复述了一次**那个数字（"收敛仅在第 3 轮结束后生效"）——那是文档自己说的
"不复述"与文档实际写法之间的既有矛盾，本轮不改文档语义，改为**加一道对照判据**：
两处数字不等即报错。漂移由程序喊，而不是靠 §1 那句注释的自律。

**读不出就抛，不返回默认值**（同 `guard/naming.py` 与 `registry/loader.py` 的口径）：
"表里没读到"如果被当成通过，表现就是超时行为静默变成代码里的兜底值，而那正是本节要禁的第二份事实。

一处文档没写完的形态，取保守侧并点名（登记在 `evidence/phase-04.md §T057` 的未闭段）：
B 类单**延长次数用尽而轮次仍未跑满**时该怎么办，`params-ref §1` 与 `contracts/approval-api.md §5`
都没给答案。本实现取"继续挂起、不收敛"，理由是两条约束里"MUST NOT 提前收敛"是结构约束、
"最多 3 次"是数值约束，冲突时结构约束优先 —— 反过来的话 FR-043 的那句 MUST NOT 就成了可被耗尽的。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from acp.persistence.enums import values_of

#: `approval_kind` 域取值的首字母就是 `params-ref §1` 表里"卡点"列的字母前缀
#: （`A_SPLIT / B_ACCEPTANCE / C_BUDGET / D_DANGER / E_REVIEW_UNCERTAIN`）。从域派生而不是
#: 抄一份映射，是为了让"新增一类卡点"必须改域定义而不能只改本模块。
KIND_BY_LETTER: dict[str, str] = {value[0]: value for value in values_of("approval_kind")}
LETTER_BY_KIND: dict[str, str] = {letter: kind for kind, letter in KIND_BY_LETTER.items()}

#: 结构约束①（FR-043 / `approval-api.md §5`）：这四类 MUST NOT 因超时自动放行。
#: E（审查不确定）由 Phase 17 P1 加入：它本来就是"交人定论"，没有可自动收敛的形态。
AUTO_PASS_ON_TIMEOUT_FORBIDDEN: frozenset[str] = frozenset(
    KIND_BY_LETTER[letter] for letter in "ACDE"
)
#: 结构约束②（FR-043 / FR-035）：只有 B 类可在**跑满轮次上限后**收敛。
CONVERGE_KINDS: frozenset[str] = frozenset({KIND_BY_LETTER["B"]})

#: `decide()` 的三种出口。**不是**单据的态（态的唯一载体是 EN-09 `state`），
#: 而是"到点了该做什么"的判定结果，供卡点节点与超时扫描消费。
WAIT = "wait"
EXTEND = "extend"
CONVERGE = "converge"

#: **前缀**而不是整行：文档里那行后面还挂着「（对应宪法原则 V）」。按整行等值匹配的第一版
#: 直接把九条判据全打成「找不到锚点行」——红得响亮，但红的原因是判据自己写岔了，不是文档变了。
SECTION_ANCHOR = "## 1. 卡点超时默认值"
_NO_TIMEOUT = "无超时"
_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*小时")
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分钟")
_EXTENSION_RE = re.compile(r"每次\s*(\d+(?:\.\d+)?)\s*小时、最多\s*(\d+)\s*次")
_ROUND_STATED_RE = re.compile(r"第\s*(\d+)\s*轮")
_CONSTITUTION_LIMIT_RE = re.compile(r"验收轮次上限\s*(\d+)")


class ParamsRefError(ValueError):
    """§1 读不出、形状不认识、或两处轮次数不一致。消息里只带键名与行号，不带凭据。"""


@dataclass(frozen=True)
class TimeoutPolicy:
    """一类卡点的超时数值（全部来自 §1 的表）+ 该类受哪条结构约束管。"""

    kind: str
    #: `None` = §1 写的"无超时"（A / C / D / E 四行）。
    wait: dt.timedelta | None
    #: §1"超时后行为"列的原文（`挂起等待` / `按默认策略继续`），留着是为了让单据可自证。
    on_timeout: str
    extension_length: dt.timedelta | None
    max_extensions: int | None
    #: §1 的 B 行复述的那个轮次数（`None` = 该行没写）。与宪法的对照判据用它。
    round_stated_in_params_ref: int | None

    @property
    def never_times_out(self) -> bool:
        return self.wait is None

    def expires_at(self, created_at: dt.datetime) -> dt.datetime | None:
        """§1 的"无超时"返回 `None`。

        裁决 ②（owner 2026-10-13）之后这个 `None` **一路到底**：仓储层直接落 SQL `NULL`
        （`migration 0006` 把 `approval_ticket.timeout_at` 改成可空），
        不再折成一个"永远到不了的时刻"。理由与代价都记在迁移文件头与 `evidence/phase-04.md §T050`。
        """
        return None if self.wait is None else created_at + self.wait


def params_ref_default() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "params-ref.md"


def constitution_default() -> Path:
    return Path(__file__).resolve().parents[3] / ".specify" / "memory" / "constitution.md"


def _cells(line: str) -> list[str]:
    return [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]


def _duration(cell: str, *, where: str) -> dt.timedelta | None:
    if cell == _NO_TIMEOUT:
        return None
    hours = _HOURS_RE.search(cell)
    if hours:
        return dt.timedelta(hours=float(hours.group(1)))
    minutes = _MINUTES_RE.search(cell)
    if minutes:
        return dt.timedelta(minutes=float(minutes.group(1)))
    raise ParamsRefError(f"{where} 的默认超时列读不出时长：{cell!r}（只认识 {_NO_TIMEOUT!r} / `N 小时` / `N 分钟`）")


def section_one_lines(text: str) -> list[str]:
    """只取 §1 的表行（到下一个 `## ` 为止），别节的表格一律不参与。"""
    lines = text.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(SECTION_ANCHOR))
    except StopIteration as exc:
        raise ParamsRefError(f"params-ref.md 里找不到锚点行 {SECTION_ANCHOR!r}") from exc
    region: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            region.append(line)
    rows = [line for line in region if not set(line.replace("|", "")) <= {"-", " ", ":"}]
    # 表头 + 分隔行的特征是"首列叫 卡点"；按内容判而不是按"第 1、2 行"判，
    # 免得日后本节上面多一张小表就把判据指错地方。
    data = [line for line in rows if _cells(line)[0] != "卡点"]
    if not data:
        raise ParamsRefError(f"{SECTION_ANCHOR} 下没解析出任何数据行（读到 {len(rows)} 行表体）")
    return data


def parse_timeout_table(params_ref_text: str | None = None, path: Path | None = None) -> dict[str, TimeoutPolicy]:
    """§1 的表 → `{approval_kind: TimeoutPolicy}`。**五类必须齐全**（Phase 17 起含 E）：缺一行就报错。

    "齐全"这条判据挡住的是"某行被改写后字母前缀变了"这一类静默降级 —— 少一类卡点的超时值
    不会让建单失败，只会让那一类卡点拿不到策略，读起来像"它没有超时"。
    """
    text = params_ref_text if params_ref_text is not None else (path or params_ref_default()).read_text(
        encoding="utf-8"
    )
    table: dict[str, TimeoutPolicy] = {}
    for line in section_one_lines(text):
        cells = _cells(line)
        if len(cells) < 4:
            raise ParamsRefError(f"§1 的行少了列（读到 {len(cells)} 列）：{cells[0]!r}")
        letter = cells[0].strip()[:1]
        kind = KIND_BY_LETTER.get(letter)
        if kind is None:
            raise ParamsRefError(f"§1 的卡点列首字母 {letter!r} 不在 approval_kind 域里：{cells[0]!r}")
        extension = _EXTENSION_RE.search(cells[3])
        stated = _ROUND_STATED_RE.search(cells[3])
        table[kind] = TimeoutPolicy(
            kind=kind,
            wait=_duration(cells[1], where=f"§1 的 {letter} 行"),
            on_timeout=cells[2],
            extension_length=(
                dt.timedelta(hours=float(extension.group(1))) if extension else None
            ),
            max_extensions=int(extension.group(2)) if extension else None,
            round_stated_in_params_ref=int(stated.group(1)) if stated else None,
        )
    missing = sorted(set(KIND_BY_LETTER.values()) - set(table))
    if missing:
        raise ParamsRefError(f"§1 的表里缺这几类卡点的行：{missing}")
    for kind in sorted(CONVERGE_KINDS):
        policy = table[kind]
        if policy.extension_length is None or policy.max_extensions is None:
            raise ParamsRefError(
                f"{kind} 行的附加结构约束列读不出「每次 N 小时、最多 M 次」⇒ FR-043 的延长档位没有数值载体"
            )
    return table


def review_round_limit(path: Path | None = None) -> int:
    """宪法 原则 III 的验收轮次上限（`constitution.md` 的"验收轮次上限 N"那行）。

    **本仓唯一解析这句话的地方**：T091 的验收节点（`orchestrate/nodes/accept.py`）也调它，
    不再自己写第二份正则 —— 两个解析器对同一句权威话有两种失败方式，正是原则 I 要禁的第二份事实。

    命中数 MUST 恰为 1，0 处与 2 处都抛：0 处是"宪法里没有这条"（那就没有任何上限可言），
    2 处是"宪法在两处定义同一件事"。取第一处等于替宪法回答"哪一处算数"，不取。
    """
    text = (path or constitution_default()).read_text(encoding="utf-8")
    hits = _CONSTITUTION_LIMIT_RE.findall(text)
    if len(hits) != 1:
        raise ParamsRefError(
            f"constitution.md 里「验收轮次上限」命中 {len(hits)} 处（值={hits}）⇒ 唯一权威处不成立："
            "0 处 = 宪法没立过这条，≥2 处 = 同一件事有两处定义，两种都不许由代码猜哪处算数"
        )
    return int(hits[0])


def _cross_check_rounds(table: dict[str, TimeoutPolicy], limit: int) -> int:
    """§1 复述的轮次数与宪法的硬值不一致即报错（两处读数漂移在这里喊，而不是等到算错）。"""
    for kind, policy in table.items():
        stated = policy.round_stated_in_params_ref
        if stated is not None and stated != limit:
            raise ParamsRefError(
                f"卡点 {kind}：params-ref §1 复述的轮次上限是 {stated}，而宪法 原则 III 写的是 {limit}"
                "⇒ 两处必须同批改（data-model.md:46 的 C9 修正就是为这个洞立的）"
            )
    return limit


def load_policies(
    params_ref: Path | None = None, constitution: Path | None = None
) -> tuple[dict[str, TimeoutPolicy], int]:
    table = parse_timeout_table(path=params_ref)
    return table, _cross_check_rounds(table, review_round_limit(constitution))


def policy_for(kind: str, *, table: dict[str, TimeoutPolicy] | None = None) -> TimeoutPolicy:
    if table is None:
        table, _ = load_policies()
    try:
        return table[kind]
    except KeyError as exc:
        raise ParamsRefError(f"卡点类型 {kind!r} 不在 approval_kind 域里") from exc


def decide(
    kind: str,
    *,
    review_round: int,
    extensions_used: int = 0,
    table: dict[str, TimeoutPolicy] | None = None,
    params_ref: Path | None = None,
    constitution: Path | None = None,
) -> str:
    """到点了做什么：`WAIT` / `EXTEND` / `CONVERGE` 三取一（FR-043 + FR-035 的唯一判定处）。

    未归类的卡点类型（第五类）抛错而不是回落到 `WAIT` —— 回落会让"新增一类而没写它受哪条约束"
    读起来像安全默认，实际上是把结构约束的归属变成了隐式决定。
    """
    if kind not in AUTO_PASS_ON_TIMEOUT_FORBIDDEN and kind not in CONVERGE_KINDS:
        raise ParamsRefError(f"卡点 {kind!r} 既不在 AUTO_PASS_ON_TIMEOUT_FORBIDDEN 也不在 CONVERGE_KINDS ⇒ 结构约束未归类")
    if kind in AUTO_PASS_ON_TIMEOUT_FORBIDDEN:
        return WAIT
    if table is None:
        table, limit = load_policies(params_ref, constitution)
    else:
        limit = review_round_limit(constitution)
    policy = policy_for(kind, table=table)
    if review_round >= limit:
        return CONVERGE
    if policy.max_extensions is not None and extensions_used >= policy.max_extensions:
        return WAIT
    return EXTEND
