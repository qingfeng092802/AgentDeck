"""T060 / T061：EN-11 低风险免确认规则表的判定（FR-046 / FR-047 / FR-048｜R-26 ①）。

**规则的唯一来源是 `config/risk_matrix.yaml`**，代码里没有任何兜底规则：读不出清单、清单形状不对
⇒ 直接报错，而不是"当没命中"。把"清单没读到"当成"没有一条规则命中"是最要紧的一种假绿 ——
它的表现是"所有任务都要人工确认"，看起来保守，实际上免确认这条路整条失灵而没人喊。
（同一条口径的先例：`guard/naming.py` 之于 `params-ref §7`、`registry/loader.py` 之于 `§9`。）

**FR-047「判定 MUST NOT 由 AI 自由裁量或覆盖」在代码形状上的落点**：本模块对外只有一种输入 ——
`RequestFacts`（平台侧算出来的事实）+ 规则表，返回一个 `Verdict`。
- `evaluate()` 没有 `exempt=` / `override=` / `auto_pass=` 这类参数；
- 免确认的**作用域**（只有卡点 A）不在规则表里，它在 `exemption_applies()` 里，是结构约束：
  改它要动代码并过一遍用例，比改一份 yaml 难得多 —— 这正是 FR-048 要的摩擦
  （`contracts/approval-api.md §5` 末句把这类改动作废走宪法修订）；
- `rules=` / `threshold=` 两个注入位**只为单点变异而存在**（换一份清单才能证明判定读的是清单而不是
  硬编码），生产调用方 MUST NOT 传。

三段匹配键的口径逐字取 `data-model.md:161-164`：
`match_agent_id` 等值 **AND** `match_role_id` 等值 **AND** `task.skill_tags ⊆ match_capability_tags`；
任一列 NULL / 空 ⇒ 该规则不命中，**空键不当通配**。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from acp.persistence.enums import values_of
from acp.persistence.models_approval import RiskRule
from acp.registry import loader

RULES_PATH = loader.REPO_ROOT / "config" / "risk_matrix.yaml"
PARAMS_REF_PATH = loader.REPO_ROOT / "config" / "params-ref.md"
THRESHOLD_ANCHOR = "## 2. 低风险免确认阈值"
THRESHOLD_ROW_KEY = "预估 token 预算阈值"

_MATCH_KEYS = ("match_agent_id", "match_role_id", "match_capability_tags")
_CONDITION_KEYS = ("no_fs_mutation", "no_paid_external_call", "budget_within_threshold")
ALLOWED_KEYS = frozenset((*_MATCH_KEYS, *_CONDITION_KEYS, "auto_pass"))

_WAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万")
_PLAIN_RE = re.compile(r"^(\d[\d_]*(?:\.\d+)?)")


def _only(domain: str, letter: str) -> str:
    """从域里按首字母取唯一取值；两个以上或零个都报错，不"取第一个"。"""
    hits = [value for value in values_of(domain) if value[0] == letter]
    if len(hits) != 1:
        raise RiskRulesError(f"{domain} 域里首字母为 {letter} 的取值不是恰好一个：{hits}")
    return hits[0]


class RiskRulesError(ValueError):
    """规则表读不出 / 形状不合 / 三段键写了空值 / 域与代码的对应关系断了。"""


@dataclass(frozen=True)
class Rule:
    """一条 EN-11 规则（`rule_id` 是库里的 serial 代理键，不参与匹配，所以这里不带它）。"""

    match_agent_id: str
    match_role_id: str
    match_capability_tags: frozenset[str]
    no_fs_mutation: bool
    no_paid_external_call: bool
    budget_within_threshold: bool
    auto_pass: bool
    #: 本清单里的序号（从 0 起，来源顺序 = 文件顺序），只用于把报错指回具体那一条。
    ordinal: int = 0

    @property
    def key(self) -> str:
        """留痕用的规则名：**按申报的三段键命名而不是用 serial**。
        拿 `rule_id` 当凭证主体会让"重排一次清单"改变历史行的读数，而三段键是清单自己写死的。
        """
        return f"risk_rule:{self.match_agent_id}:{self.match_role_id}"

    def matches(self, agent_id: str, role_id: str, skill_tags: frozenset[str]) -> bool:
        return (
            self.match_agent_id == agent_id
            and self.match_role_id == role_id
            and bool(skill_tags)
            and skill_tags <= self.match_capability_tags
        )


@dataclass(frozen=True)
class RequestFacts:
    """平台侧算出的三项事实（FR-046 的三条低风险条件）。

    **MUST NOT 由工具自报**：这三项来自任务卡与账本（EN-02 的产物下限 / EN-13 的用量口径）。
    `estimated_tokens` 目前还没有生产写者（`cost/ledger.py` 归 T114，Phase 10）⇒ 它**没有默认值**，
    缺它即 `TypeError`；给它 0 是调用方的决定，而 0 在判定里就是一个真数（条件③过），
    所以"没有账本时能不能免确认"这件事被留在调用方，登记在 `evidence/phase-04.md §T060` 的未闭段。
    """

    agent_id: str
    role_id: str
    skill_tags: frozenset[str]
    fs_mutation: bool
    paid_external_call: bool
    estimated_tokens: int

    def __post_init__(self) -> None:
        if self.estimated_tokens < 0:
            raise RiskRulesError(f"estimated_tokens 不能是负数：{self.estimated_tokens}")


@dataclass(frozen=True)
class Verdict:
    """一次判定的全部可读结论。`exempt` 只在"命中的规则 auto_pass 且请求侧三条同时成立"时才为真。"""

    exempt: bool
    rule: Rule | None
    #: 不命中 / 命中的规则本身不免确认 / 请求侧条件与规则声明冲突 / 超阈值 —— 分开列，
    #: 取证时才分得出"这次为什么没免"是哪一种。
    reasons: tuple[str, ...] = field(default=())

    @property
    def decided_by(self) -> str | None:
        return self.rule.key if self.exempt and self.rule is not None else None


def _cells(line: str) -> list[str]:
    return [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]


def budget_threshold(params_ref: Path | None = None) -> int:
    """FR-046 条件③的阈值，唯一载体 = `params-ref §2` 那张表的"预估 token 预算阈值"行。

    本模块**不内联那个数**：§2 是引用型参数（改它属 PATCH），抄进代码就等于配置改了而行为不变。
    读不出行 / 读不出数一律报错，不返回默认阈值。
    """
    text = (params_ref or PARAMS_REF_PATH).read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(THRESHOLD_ANCHOR))
    except StopIteration as exc:
        raise RiskRulesError(f"params-ref.md 里找不到锚点行 {THRESHOLD_ANCHOR!r}") from exc
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) < 2 or cells[0] != THRESHOLD_ROW_KEY:
            continue
        wan = _WAN_RE.search(cells[1])
        if wan:
            return int(float(wan.group(1)) * 10_000)
        plain = _PLAIN_RE.match(cells[1])
        if plain:
            return int(plain.group(1).replace("_", ""))
        raise RiskRulesError(f"§2 的阈值单元格读不出数：{cells[1]!r}")
    raise RiskRulesError(f"{THRESHOLD_ANCHOR} 的表里找不到 {THRESHOLD_ROW_KEY!r} 那一行")


def load_rules(path: Path | None = None) -> list[Rule]:
    """解析 `config/risk_matrix.yaml`。形状判据全在这里，且每条都拒在内存里、不落半行。"""
    raw = yaml.safe_load((path or RULES_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise RiskRulesError("risk_matrix.yaml 顶层必须是非空清单（一条规则一项）")
    rules: list[Rule] = []
    for ordinal, item in enumerate(raw):
        where = f"第 {ordinal + 1} 条规则"
        if not isinstance(item, dict):
            raise RiskRulesError(f"{where} 不是映射：{type(item).__name__}")
        unknown = sorted(set(item) - ALLOWED_KEYS)
        if unknown:
            raise RiskRulesError(f"{where} 有表外键：{unknown}（表外键是第二份清单的开始，不是扩充）")
        missing = sorted(ALLOWED_KEYS - set(item))
        if missing:
            raise RiskRulesError(f"{where} 缺键：{missing}")
        for key in ("match_agent_id", "match_role_id"):
            value = item[key]
            if not isinstance(value, str) or not value.strip():
                raise RiskRulesError(
                    f"{where} 的 {key} 必须是非空字符串（写成 null 或空串时本模块按"
                    "「空键不当通配」整条拒收，口径见 data-model.md:161-164）"
                )
        tags = item["match_capability_tags"]
        if not isinstance(tags, list) or not tags or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise RiskRulesError(f"{where} 的 match_capability_tags 必须是非空字符串列表")
        for key in (*_CONDITION_KEYS, "auto_pass"):
            if not isinstance(item[key], bool):
                raise RiskRulesError(f"{where} 的 {key} 必须是布尔：{item[key]!r}")
        if item["auto_pass"] and not all(item[key] for key in _CONDITION_KEYS):
            raise RiskRulesError(
                f"{where} auto_pass: true 但三条条件没同时成立 ⇒ 与库里那条 auto_pass_needs_all_three "
                "的 CHECK 同一口径，这里先拒（清单进不了内存比落半行再被 DB 拒好查）"
            )
        rules.append(
            Rule(
                match_agent_id=item["match_agent_id"],
                match_role_id=item["match_role_id"],
                match_capability_tags=frozenset(item["match_capability_tags"]),
                no_fs_mutation=item["no_fs_mutation"],
                no_paid_external_call=item["no_paid_external_call"],
                budget_within_threshold=item["budget_within_threshold"],
                auto_pass=item["auto_pass"],
                ordinal=ordinal,
            )
        )
    duplicates = sorted(_duplicate_keys(rules))
    if duplicates:
        raise RiskRulesError(f"三段键重复的规则：{duplicates}（重复命中会让「哪条生效」取决于文件顺序）")
    return rules


def _duplicate_keys(rules: list[Rule]) -> set[tuple[str, str, frozenset[str]]]:
    seen: set[tuple[str, str, frozenset[str]]] = set()
    dupes: set[tuple[str, str, frozenset[str]]] = set()
    for rule in rules:
        key = (rule.match_agent_id, rule.match_role_id, rule.match_capability_tags)
        if key in seen:
            dupes.add(key)
        seen.add(key)
    return dupes


def evaluate(
    facts: RequestFacts,
    *,
    rules: list[Rule] | None = None,
    threshold: int | None = None,
) -> Verdict:
    """三段键命中 **且** 该规则 `auto_pass` **且** 请求侧三条事实同时低风险 ⇒ 免确认（FR-046）。

    命中口径与"规则声明"要**两侧都对得上**：规则说它不碰文件，请求侧就必须真的不碰文件；
    任何一方为假都不免确认。这不是重复判定 —— 规则表是**人对这类组合的申报**，
    `RequestFacts` 是**这一次任务的事实**，两个来源、两个时刻，合起来才是 FR-046 的三条。
    """
    if not facts.skill_tags:
        # 空标签集合按"不命中"处理（`data-model.md:162` 那句"任一列空 ⇒ 不命中"同样管请求侧：
        # 空集是任何集合的子集，拿它当命中就是给"没申报能力的卡"发免确认）。
        return Verdict(False, None, ("task_skill_tags_empty",))
    table = load_rules() if rules is None else rules
    limit = budget_threshold() if threshold is None else threshold
    matched = [r for r in table if r.matches(facts.agent_id, facts.role_id, facts.skill_tags)]
    if not matched:
        return Verdict(False, None, ("no_rule_matched",))
    reasons: list[str] = []
    for rule in matched:
        if not rule.auto_pass:
            reasons.append(f"rule{rule.ordinal}_declares_no_auto_pass")
            continue
        if rule.no_fs_mutation != (not facts.fs_mutation):
            reasons.append(f"rule{rule.ordinal}_vs_fs_mutation")
            continue
        if rule.no_paid_external_call != (not facts.paid_external_call):
            reasons.append(f"rule{rule.ordinal}_vs_paid_external_call")
            continue
        if facts.estimated_tokens > limit:
            reasons.append(f"rule{rule.ordinal}_over_budget_threshold_{limit}")
            continue
        return Verdict(True, rule, ())
    return Verdict(False, None, tuple(reasons))


def exemption_applies(kind: str, verdict: Verdict) -> bool:
    """FR-048：免确认**只**免除卡点 A（拆单确认），审查 / 验收 / 超预算 / 危险操作一律照走。"""
    return verdict.exempt and kind == _only("approval_kind", "A")


def persist_rules(session: Session, rules: list[Rule] | None = None) -> int:
    """把清单灌进 EN-11（T060 的「+ EN-11 `risk_rule`」）。`rule_id` 是 serial，由库给，清单里不写。"""
    table = load_rules() if rules is None else rules
    for rule in table:
        session.add(
            RiskRule(
                match_agent_id=rule.match_agent_id,
                match_role_id=rule.match_role_id,
                match_capability_tags=sorted(rule.match_capability_tags),
                no_fs_mutation=rule.no_fs_mutation,
                no_paid_external_call=rule.no_paid_external_call,
                budget_within_threshold=rule.budget_within_threshold,
                auto_pass=rule.auto_pass,
            )
        )
    session.flush()
    return len(table)


def now_or(moment: dt.datetime | None) -> dt.datetime:
    """留痕用的时间戳；单独成一个函数是为了让用例能把"现在"钉住（同 `repository._now`）。"""
    return moment if moment is not None else dt.datetime.now(dt.timezone.utc)
