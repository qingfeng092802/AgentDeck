"""T115 的读价半边：`config/params-ref.md §6` 的价目 → 可算的单价（FR-061｜R-25）。

**为什么这里一份数字都不写**：`plan.md` 设计项 7 那句「单价与套餐数值一律读 `config/params-ref.md` §6」
是硬约束，宪法「二」也把 §6 定性为**外部市场事实**（随厂商调整 ⇒ PATCH）。代码里抄一份的后果不是「重复」
这么简单：厂商调价时文档改了、代码没改，账就按旧价算，而**没有任何一条用例会发现**（EN-13 存的本来就是
算完的结果）。⇒ 本模块把 §6 读出来，读不出就报错，宁可停住也不要「按上周的价继续记」。
判据里有一发**行为式绑定**（改 §6 里的数字 ⇒ 读价跟着变），口径同 T109 给 `params-ref §10` 的那条。

**三类口径各自有各自的算法**（FR-061 的「分列」到这里才真正有了内容）：

| 口径 | 算法 | §6 里的载体 |
|---|---|---|
| `TOKEN`（按用量计价） | `quantity / 1e6 × 每百万单价`，单价按 `model_ref` 与 `usage_kind` 取 | 豆包那张 4 行表（输入 / 输出 两列） |
| `SEAT_CREDIT`（按坐席 + 额度） | `SEAT_DAY × 日均坐席成本`，或 `CALL × 加量包每 Credit 单价` | 「日均坐席成本」那一行 + 「加量包」那一行的第一组 `N Credits = M 元` |
| `BYOK`（由所配厂商结算） | **恒 0 元**，且必须带出处 | BYOK 那一行写的就是「费用由模型厂商结算，不占 Credits」 |

BYOK 的 0 **不是**「免费」也不是「成本未知」：按 §6 的定义，这笔钱根本不进平台账（进的是厂商账单）。
它之所以能这么记，全靠「分列」这条约束还在 —— 一旦有人把三个小计加成一个数，这个 0 就变成
「平台这次只花了豆包那点钱」的假账。所以报错的那道接口（`ledger.Breakdown.total()`）与本模块是同一件事的两半。

**读不出价一律报错，不猜**（`PriceUndetermined`，`code` 逐条点名）：§6 缺锚点 / 表体空、
某一行读不出数字、`model_ref` 不在表里、`TOKEN` 口径遇到没有单价的 `usage_kind`（`CALL` / `SEAT_DAY` ——
那张表只有输入 / 输出两列）、单位与口径对不上。猜的后果是 EN-13 里躺着一串看起来像钱的数，
而那正是 R-25 这条需求线要避免的事。

**舍入是这里唯一一处我们自己定的规则**：§6 只给单价不给精度，本模块把金额落到 `0.000001` 元、
`ROUND_HALF_UP`。这一条被钉成用例（不是「顺手写的」），因为它决定「同一批用量记两次会不会差一分钱」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from acp.approval.timeouts import params_ref_default
from acp.protocol.refs import UsageNotice

#: §6 的锚点行前缀（与 `approval/timeouts.py` 取 §1 的做法同形：按标题定位，到下一个 `## ` 为止）。
SECTION_ANCHOR = "## 6. 价目快照"

#: §6 整表以「元」计价 ⇒ 币种由文档载体决定，不是这里挑的。
CURRENCY = "CNY"

#: 单价是「每百万 Tokens」，`quantity` 记的是实际 token 数 ⇒ 除数。
PER_MILLION = Decimal(1_000_000)

#: 金额精度（模块头那条：§6 不给，本模块定并钉成用例）。
AMOUNT_EXPONENT = Decimal("0.000001")

#: 单位与口径的对应（读 §6 的列名，不另造词表）。
TOKEN_UNIT = "token"
SEAT_DAY_UNIT = "seat-day"
CREDIT_UNIT = "credit"

#: 三种口径各自的算法入口，取值 = `billing_basis` 域（从 `protocol/refs` 派生，不抄字面量）。
MERGE_FORBIDDEN_HINT = "不可合并"

_MILLION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元/百万")
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_CREDIT_PACK_RE = re.compile(r"([\d,]+)\s*Credits\s*=\s*([\d,]+)\s*元")


class PriceUndetermined(ValueError):
    """§6 读不出这一发该用的单价，或这一发压根没有单价可算。全部发生在写库之前。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Amount:
    """一条通告算出来的金额 + 它是**按 §6 哪一行**算出来的。

    `source` 形如 `params-ref §6:123`。写进 EN-13 的只有 `cost_amount` 与 `currency`；`source` 跟着
    返回值走（判据与日志用它点名出处），**不落库** —— EN-13 没有那一列，加一列属迁移，不顺手做。
    """

    cost_amount: Decimal
    currency: str
    basis: str
    source: str


@dataclass(frozen=True)
class RateTable:
    """§6 的可算快照。每个数都带自己的行号出处，包括 `TOKEN` 那张表。"""

    token: dict[str, dict[str, Decimal]]
    token_source: dict[str, str]
    seat_day: tuple[Decimal, str]
    credit: tuple[Decimal, str]
    byok: tuple[Decimal, str]

    def token_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self.token))


def _cells(line: str) -> list[str]:
    return [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]


def section_six_lines(text: str) -> list[tuple[int, str]]:
    """只取 §6 那一段的表行，返回 `(文件里的 1 基行号, 原文)`；别的节一律不参与。

    行号要跟着读数走：报错时说清「§6 第 123 行读不出单价」，比「价目表有问题」能查得多。
    """
    lines = text.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(SECTION_ANCHOR))
    except StopIteration as exc:
        raise PriceUndetermined(
            f"params-ref.md 里找不到锚点行 {SECTION_ANCHOR!r} ⇒ 价目一节被改名或删掉，先修文档",
            code="section_missing",
        ) from exc
    region: list[tuple[int, str]] = []
    for offset, line in enumerate(lines[start + 1 :], start=start + 2):
        if line.startswith("## "):
            break
        if line.startswith("|"):
            region.append((offset, line))
    data = [(number, line) for number, line in region if not set(line.replace("|", "")) <= {"-", " ", ":"}]
    if not data:
        raise PriceUndetermined(
            f"{SECTION_ANCHOR} 下没读到任何表行（表体 {len(region)} 行全是分隔行）", code="section_empty"
        )
    return data


def parse_rates(path: Path | None = None, *, text: str | None = None) -> RateTable:
    """§6 → `RateTable`。缺哪一行就报哪一行，**不用默认值补**。

    注入 `path` / `text` 是判据与测试用的（行为式绑定那一发要能喂一份改过数字的副本），
    生产路径两个都不给 ⇒ 读的就是仓库里那一份。
    """
    body = text if text is not None else (path or params_ref_default()).read_text(encoding="utf-8")
    rows = section_six_lines(body)

    token: dict[str, dict[str, Decimal]] = {}
    token_source: dict[str, str] = {}
    seat_day: tuple[Decimal, str] | None = None
    credit: tuple[Decimal, str] | None = None
    byok: tuple[Decimal, str] | None = None
    merge_rule_found = False

    for number, line in rows:
        cells = _cells(line)
        if len(cells) < 2:
            continue
        label, first = cells[0], cells[1]
        rates = [_MILLION_RE.search(cell) for cell in cells[1:3]]
        if all(rates) and label and label != "模型":
            token[label] = {"INPUT": Decimal(rates[0].group(1)), "OUTPUT": Decimal(rates[1].group(1))}
            token_source[label] = f"params-ref §6:{number}"
            continue
        if "日均坐席成本" in label:
            found = _NUMBER_RE.search(first)
            if found is None:
                raise PriceUndetermined(
                    f"§6 第 {number} 行的坐席日成本读不出数字：{first!r}", code="seat_rate_missing"
                )
            seat_day = (Decimal(found.group(1)), f"params-ref §6:{number}")
            continue
        if label == "加量包" and credit is None:
            pack = _CREDIT_PACK_RE.search(first)
            if pack is None:
                raise PriceUndetermined(
                    f"§6 第 {number} 行读不出 `N Credits = M 元` 这一组：{first!r}", code="credit_rate_missing"
                )
            credits = Decimal(pack.group(1).replace(",", ""))
            money = Decimal(pack.group(2).replace(",", ""))
            if credits <= 0:
                raise PriceUndetermined(
                    f"§6 第 {number} 行的 Credits 数不是正数：{pack.group(1)!r}", code="credit_rate_missing"
                )
            credit = (money / credits, f"params-ref §6:{number}")
            continue
        if label == "BYOK":
            if "不占 Credits" not in first:
                raise PriceUndetermined(
                    f"§6 第 {number} 行的 BYOK 说明里没有「不占 Credits」这句话 ⇒ 0 元这一口径读不出来",
                    code="byok_rule_missing",
                )
            byok = (Decimal(0), f"params-ref §6:{number}")
            continue
        if MERGE_FORBIDDEN_HINT in line:
            merge_rule_found = True

    if not token:
        raise PriceUndetermined("§6 里没有一行读得出「每百万元」的 Token 单价表", code="token_table_missing")
    if seat_day is None:
        raise PriceUndetermined("§6 里读不出日均坐席成本", code="seat_rate_missing")
    if credit is None:
        raise PriceUndetermined("§6 里读不出加量包的 Credits 单价", code="credit_rate_missing")
    if byok is None:
        raise PriceUndetermined("§6 里读不出 BYOK 那一行", code="byok_rule_missing")
    if not merge_rule_found:
        raise PriceUndetermined(
            f"§6 里找不到含 {MERGE_FORBIDDEN_HINT!r} 的那句口径约束 ⇒ FR-061 的权威句被改写了",
            code="merge_rule_missing",
        )
    return RateTable(token=token, token_source=token_source, seat_day=seat_day, credit=credit, byok=byok)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_EXPONENT, rounding=ROUND_HALF_UP)


def price_of(notice: UsageNotice, *, table: RateTable | None = None) -> Amount:
    """一条 `UsageNotice` → 金额 + 币种 + 出处。算不出就抛，**永不返回估算值**。"""
    rates = table if table is not None else parse_rates()
    basis = str(notice.billing_basis)
    kind = str(notice.usage_kind)
    unit = str(notice.unit).strip().lower()
    quantity = Decimal(str(notice.quantity))

    if basis == "TOKEN":
        if unit != TOKEN_UNIT:
            raise PriceUndetermined(
                f"TOKEN 口径的单位收到 {unit!r}，只认 {TOKEN_UNIT!r} ⇒ §6 的单价按「每百万 token」给，"
                "换个单位就是换个算法",
                code="unit_basis_mismatch",
            )
        if kind not in {"INPUT", "OUTPUT"}:
            raise PriceUndetermined(
                f"§6 那张表只有 输入 / 输出 两列，{kind} 这一支没有单价可取 ⇒ 不猜",
                code="no_rate_for_kind",
            )
        if notice.model_ref is None:
            raise PriceUndetermined(
                "TOKEN 口径必须带 model_ref 才能对上 §6 的那一行 ⇒ 缺它就只报缺，不取第一行凑数",
                code="model_ref_missing",
            )
        label = str(notice.model_ref)
        row = rates.token.get(label)
        if row is None:
            raise PriceUndetermined(
                f"model_ref={label!r} 不在 §6 的价目行里（可读出的标签：{rates.token_labels()}）"
                " ⇒ 要么文档补这一行，要么调用方交一个文档里有的模型名",
                code="model_row_missing",
            )
        return Amount(
            cost_amount=_quantize(quantity / PER_MILLION * row[kind]),
            currency=CURRENCY,
            basis=basis,
            source=rates.token_source[label],
        )

    if basis == "SEAT_CREDIT":
        if kind == "SEAT_DAY" and unit == SEAT_DAY_UNIT:
            value, source = rates.seat_day
            return Amount(cost_amount=_quantize(quantity * value), currency=CURRENCY, basis=basis, source=source)
        if kind == "CALL" and unit == CREDIT_UNIT:
            value, source = rates.credit
            return Amount(cost_amount=_quantize(quantity * value), currency=CURRENCY, basis=basis, source=source)
        raise PriceUndetermined(
            f"SEAT_CREDIT 口径只认两种形状：(SEAT_DAY, {SEAT_DAY_UNIT!r}) 与 (CALL, {CREDIT_UNIT!r})，"
            f"收到 (kind={kind!r}, unit={unit!r})",
            code="unit_basis_mismatch",
        )

    if basis == "BYOK":
        value, source = rates.byok
        return Amount(cost_amount=_quantize(quantity * value), currency=CURRENCY, basis=basis, source=source)

    raise PriceUndetermined(
        f"billing_basis={basis!r} 没有算法（三口径之外的值进不来：协议层与 PG 枚举都会先拒）",
        code="basis_unsupported",
    )
