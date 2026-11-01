"""审批门的运行模式：`ACP_APPROVAL_MODE` 的唯一载体（2026-10-28）。

**为什么要有这一格**：`ACP_REVIEW_MODE=auto` 把"逐张卡的人工评审"去掉了，但**拆单确认**
（A 类单据）还挂在那儿 —— 它是图上的中断点（`orchestrate/nodes/approval_gate.py`），
不批就不往下走，而 `/requests/{id}/continue` 见到待批单直接 409。于是现场仍然是
"每单都要人点一次"，且那一颗按钮长在右下那块浮层面板里（2026-10-28 已被 owner 要求整块删掉）。
面板没了，这道门就必须由机器自己过，否则整条链在拆单那一步永久停住。

**为什么复用 `apply_auto_pass()` 而不是自己改状态**：那条路是本仓为"规则表免确认"
（FR-046 / FR-048）专门留的唯一写者 —— 它只允许 A 类、必须由一条规则署名、并且写一行
`channel=AUTO` 的 EN-10 到达流水。审计面因此仍然分得清"人批的（DASHBOARD/CLI）"与
"规则免确认的（AUTO）"，`resolution_channel` 的 `AUTO` 值**只有它能写**。
自己 UPDATE 一行就是把那条区分抹掉。

**与 `ACP_REVIEW_MODE` 的关系：两件事，互不推导。**
`review` 管的是"卡交上来之后谁签验收"，`approval` 管的是"拆单方案要不要人点头"。
把前者设成 `auto` 不自动意味着后者也免 —— 那样一来"我只想免验收、但拆单我要看一眼"
这个组合就没有表达方式了。所以两格各自独立，各自一个环境变量。

**默认值仍是 `manual`**：这一格默认免确认等于替所有部署做了一次治理决定。
现场要"零人工"，在 `.env` 里显式写 `ACP_APPROVAL_MODE=auto` —— 和 `ACP_REVIEW_MODE` 同一种开关方式。

本模块**不 import 任何东西**：`serve_intake`（后台线程）与 `serve`（装配）都要读它，
保持零依赖才能不被卷进循环 import。
"""

from __future__ import annotations

#: 环境变量名。
APPROVAL_MODE_ENV = "ACP_APPROVAL_MODE"

#: 人工批（默认）。A 类单据停在 `PENDING`，等 CLI 或看板上的动作。
APPROVAL_MODE_MANUAL = "manual"

#: 拆单完成即免确认：`apply_auto_pass()` 用下面那个规则名署名，然后自己续跑。
APPROVAL_MODE_AUTO = "auto"

APPROVAL_MODES = (APPROVAL_MODE_MANUAL, APPROVAL_MODE_AUTO)

#: 免确认的**规则名**，写进 `approval_ticket.decided_by_principal` 与 EN-10 的 `reason_text`。
#:
#: 形状照 `tests/integration/test_approval_ticket.py` 里那条 `risk_rule:workbuddy:architect`：
#: `来源:取值`。来源写清楚是"哪个开关让它免的"，取值写清楚是"开关的哪一档" ——
#: 三个月后有人问"这单为什么没人批就过了"，从这一格能一路查到 `.env` 的那一行。
#: **不许**写成 `system` / `auto` 这类没有出处的词：那是把"谁决定的"退化成"某个东西决定的"。
SPLIT_AUTO_RULE = f"auto-split-confirm:{APPROVAL_MODE_ENV}={APPROVAL_MODE_AUTO}"


class ApprovalModeInvalid(ValueError):
    """`ACP_APPROVAL_MODE` 不在封闭集合里。启动期拒，不降级成 manual。

    与 `review/policy.py::ReviewModeInvalid` 同一条口径：认不出来的值当成 `manual` 会让
    "我以为它开着"与"它其实关着"变成同一屏，而这两件事的后果差一整道人工门。
    """

    def __init__(self, raw: str) -> None:
        self.raw = raw
        super().__init__(f"不认识的 {APPROVAL_MODE_ENV}={raw!r} ⇒ 只接受 {list(APPROVAL_MODES)}（不降级）")


def resolve_approval_mode(raw: str) -> str:
    normalized = str(raw).strip().lower()
    if normalized not in APPROVAL_MODES:
        raise ApprovalModeInvalid(raw)
    return normalized


def auto_split_confirm(mode: str) -> bool:
    """这一档要不要在拆单完成时自动免确认。

    只认 `auto` 一个值，其余一律 `False` —— 判据写成 `== APPROVAL_MODE_AUTO` 而不是
    `!= APPROVAL_MODE_MANUAL`：将来加第三档（比如"只免文档类拆单"）时，新档不该默默继承
    "全免"这个后果，得自己在这里写一行。
    """
    return mode == APPROVAL_MODE_AUTO
