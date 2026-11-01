"""跨 Agent 消息里的引用型类型（T010，闭合 `data-model.md §5` 的 T-5 前两项）。

这里定死三样取值域（`ArtifactFormat` / `UsageKind` / `BillingBasis`）与两个复合引用
（`AgentRoleRef` / `Handle`）。PG 侧（EN-02 / EN-06 / EN-13）与适配器侧 MUST 引用同一份口径，
不再各写一遍字面量——分开写就会出现"契约改了三值、库里还认四值"这类看不见的不一致。

用 `Literal` 而非 `str` 混入枚举：混入枚举的成员按**值**哈希（MRO 里 `str` 在 `Enum` 前），
纯枚举按**成员名**哈希，两种写法对同一键的查找结果不同。取值域需要被测试直接遍历断言，
`get_args()` 对 `Literal` 是稳定的，故这一层不引入枚举类型；PG 侧枚举归 1.3 的 T025 / T026，
届时 MUST 从本文件的常量生成而非另起一份。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

# FR-017 第三项下限的取值域。定死的理由：`format` 若留自由字符串，"格式不符"这一支就不可证伪
# （任何输入都"符合"一个未声明的域），T-1 / T-5 因此把它列为本任务的关闭条件。
#
# `js` 是 owner 2026-10-29 的裁决（第十一轮）。此前只有五值，而**页面卡写一支 `.js` 是常态**：
# 2026-10-28 的真跑里 T02 交了 `cart.js`，`gateway.format_of()` 判 `unsupported_format`，
# 那次写入整件被拒 ⇒ 一张卡直接跑砸。根因不在执行器"乱写后缀"，在这个域少了一个
# 静态站本来就有的格式：一份能被双击打开的站点，样式、脚本、页面是三件平级的东西。
#
# **新值 MUST 追加在末尾**：PG 的 `ALTER TYPE … ADD VALUE` 只能追加到末尾（`BEFORE`/`AFTER`
# 要 PG 12+ 且要重建排序），迁移 `0010` 正是这么加的。把 `js` 插在中间会让
# `pg_enum(...).enums` 与 `values_of(...)` 的顺序对不上，而
# `tests/contract/test_states.py` 的顺序断言会红 —— 这条约束写在取值域旁边，不写在迁移里。
ArtifactFormat = Literal["html", "css", "md", "json", "py", "js"]

# G1 的三类形态。CLI / GUI 本期不接：拒绝发生在**构造期**，而不是靠 `if form == ...` 的运行时分支
# （宪法 II：形态差异只在 transport 内消化，不许长进编排层）。
AgentForm = Literal["IN_PLATFORM", "SDK", "API"]

CostBand = Literal["CHEAP", "MID", "EXPENSIVE"]

SelfReportedStatus = Literal["success", "failed"]

UsageKind = Literal["INPUT", "OUTPUT", "CALL", "SEAT_DAY"]

BillingBasis = Literal["TOKEN", "SEAT_CREDIT", "BYOK"]


class StrictModel(BaseModel):
    """跨 Agent 消息的统一基类：多余字段一律拒。

    `extra="forbid"` 是 FR-009「无私有旁路」的**结构**强制点。适配器想塞厂商专有字段或
    自己判定的 `validated_status`，会在构造期失败，而不是等到下游发现语义已被污染。
    厂商字段的合法落点只有 `adapters/<vendor>/transport.py`（契约 §4）。
    """

    model_config = ConfigDict(extra="forbid")


class AgentRoleRef(StrictModel):
    """`工具 + 岗位` 复合定位（FR-010 / Q-5）。三岗互斥的判定单位是 `agent_id`（FR-031）。"""

    agent_id: str
    role_id: str


class UsageNotice(StrictModel):
    """用量通告，`cost_record` 的唯一入口形状（EN-13 / FR-059）。

    刻意**不带** `cost_amount` 与 `currency`：金额由 `cost/ledger.py` 依 `params-ref §6` 的单价换算。
    让工具自报金额等于把 FR-061 的口径分列交回报价方，与 FR-019「以产物实况而非工具自述为准」
    是同一条理由。

    `model_ref` 允许来自 `Task.dev_model`，但 EN-13 已钉它不参与把关与选人。
    """

    usage_kind: UsageKind
    quantity: Decimal
    unit: str
    billing_basis: BillingBasis
    model_ref: str | None = None
    occurred_at: datetime


class Handle(StrictModel):
    """`submit()` 的回执，`collect()` / `cancel()` 的入参（契约 §4）。

    只装厂商侧可重查的标识：`native_ref` 由各 transport 自己解释，协议层不理解它的内容。
    MUST NOT 混进瞬态层的 `SlotHandle`（契约 §6 的 N-11 消歧）——后者是进程内对象，
    一旦随本结构落进任务卡或日志就等价于自造第二层持久化（宪法 I）。
    """

    task_id: str
    agent_id: str
    native_ref: str
    submitted_at: datetime
