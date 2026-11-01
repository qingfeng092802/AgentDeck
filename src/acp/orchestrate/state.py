"""T036：编排图的**过程态**形状（`plan.md` 的 "Source Code" 树里 `orchestrate/state.py` 那一项）。

**这个文件里放的不是事实态**。宪法 原则 I 定的是"唯一事实态 = PostgreSQL"，而 LangGraph 的 state
只活在一次运行里：进程崩了就没了。所以本文件的每个键都必须满足下面两条之一 ——
① 它是**已经在 EN-01 / EN-02 里有的东西**（这里是搬运，不是第二份权威）；
② 它是**本轮节点的中间产物**，且同一轮内就会被写进库（`drafts` / `card_order` / `snapshot_id`）。
判定不许以 state 为最终依据：落库之后一律以库为准（否则恢复路径就会读到一份账的两个版本）。

**为什么 `drafts` 是 `list[CardDraft]` 而不是 dict**：`split.py` 的形状判据（`extra="forbid"`）
只作用在模型对象上，一旦在某个节点里把它降级成 dict，下游就再也拿不到"多余字段被拒"这层保护。
这份代价在 Phase 4 由 `persistence/checkpointer.py::allowed_modules_for()` 付掉：白名单
**从本类的类型标注派生**而不是手写 —— 手写的风险是"往 state 里加了个新模型而忘了登记"，
那会在恢复路径上才炸。

**序列化行为的两档（2026-10-13 实测更正，推翻此前写在这里的那句"会发告警、将来变拒绝"）**：
`JsonPlusSerializer` 对未注册的自定义类，行为取决于**有没有显式给白名单**，而不是环境变量：
- `allowed_msgpack_modules=True`（默认，即"没给"）：**放行，但走 `logger.warning`**
  （原文 `Deserializing unregistered type … CardDraft. This will be blocked in a future version.`）；
- **显式给一个列表且值不在其中**：当场**拒绝并降级**（`Blocked deserialization of … not in allowed_msgpack_modules`），
  取回来的值不再是原对象。
两档都**只走 logging，不走 `warnings` 模块** ⇒ 断"零告警"（`warnings.catch_warnings`）是一条恒绿的死判据，
所以本文件的判据断的是**保真**：`test_a_checkpoint_written_with_the_deried_allowlist_comes_back_as_the_same_objects`
+ 反向对照 `test_an_empty_allowlist_blocks_the_same_payload_and_degrades_it`
（都在 `tests/guards/test_approval_resume.py`，跑真实 PG 挂起 / 恢复）。
`state.py` 里那句"要么把草稿做成可序列化形状、要么在 state 里只放卡号"是 Phase 2 给 Phase 4 留的题，
**本轮取的是第三条**：形状不动、卡号也留（`card_ids` 本来就在），代价换成"白名单必须跟着标注走"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import TypedDict, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft
from acp.protocol.messages import TaskResult


class IntakeState(TypedDict, total=False):
    """一次"已入库的需求单 → 拆单完成"的运行所经过的键。

    `total=False`：LangGraph 按节点返回的增量合并 state，入口只给 `feature_request_id`，
    其余由各节点逐段填进来。
    """

    # 入口给的那一个键就是全部输入：需求单已在 EN-01（由 `POST /requests` 写入）。
    feature_request_id: uuid.UUID
    # 条件①：从 EN-01 搬来的两列，图内不重新补全（一次需求只补全一次，见 `graph.py` 的 `_plan`）。
    summary_digest: dict[str, list[str]]
    clarification_waived: bool
    # 条件②：本轮中间产物，`_normalize` 通过校验后随即写库。
    # `drafts` → EN-02、`links` → EN-07（FR-007 要求与拆单同步生成，所以两者同批产出、同批落库）。
    drafts: list[CardDraft]
    links: list[AcceptanceLink]
    card_order: list[str]
    card_ids: list[str]
    snapshot_id: uuid.UUID
    acceptance_map_rows: int
    # 运行结束时的 EN-01 状态读数（供调用方核对，权威值仍在库里）。
    request_status: str
    # T048：A 类卡点的产出。四键全部是**条件②**（本轮中间产物，节点里当场就写进 EN-09 / EN-02）：
    # 单据号与结论落 EN-09 / EN-10，卡的新状态落 EN-02，所以判定一律以库为准（模块头那句）。
    # 这四键同时是 checkpointer 要序列化出去的形状 ⇒ 只放 `uuid` / `str` / `bool`，
    # 不放自造的数据类：白名单派生自标注，多一个自定义类就多一处"恢复时才发现炸"的地方。
    approval_ticket_id: uuid.UUID
    gate_decision: str
    card_status_after: str
    gate_asked_again: bool


class AssignmentState(TypedDict, total=False):
    """T086 / T073：一次"**已拆好的卡** → 三个岗都填上"的运行所经过的键。

    与 `ExecutionState` 分开的理由和它自己不与 `IntakeState` 合并同一个：**生命周期不同**。
    这一段跑在 `Dispatch` 之前（`plan.md:251`），产物还不存在；那一段跑在结果回投之后。
    合成一份就会让门禁读到"这一轮根本没有的键"，也让"三岗是否已齐"这种判定失去唯一载体。

    四个键都是模块头那条**条件①**（EN-02 里已有的东西，这里只搬运）：
    `feature_request_id` / `task_id` 是复合主键的两半，三个 `*_agent_id` 是 EN-02 的三列本身。
    这里 MUST NOT 出现 `SlotHandle` 一类的瞬态值 —— 契约 §6 禁它进任何可序列化载体，
    而 state 是图上唯一可能被 checkpointer 搬走的东西（见 `graph.build_assignment_graph` 那段）。
    """

    feature_request_id: uuid.UUID
    task_id: str
    executor_agent_id: str | None
    reviewer_agent_id: str | None
    acceptor_agent_id: str | None


class ExecutionState(TypedDict, total=False):
    """一次"**已派发的卡** → 产物门禁判完"的运行所经过的键（T076 那半条链）。

    与 `IntakeState` 分开的理由是**生命周期不同**：前者一跑管一个需求单（拆单到 A 类卡点），
    这一份管一张卡（回收 → 门禁 → 出边）。合成一份 state 的话，`card_ids`（复数）与
    `task_id`（单数）会同时可读，而门禁只该看后者 —— 那是"一张卡的判定读到一批卡"的入口。

    每个键仍受模块头那两条约束之一管：
    - `feature_request_id` / `task_id`：条件①，EN-02 复合主键的两半，图内不重新发明卡号；
    - `result`：条件②，适配器回收到的那份 `TaskResult`，`ResultIngest` 当场把它写成 EN-06 行；
      它是 pydantic 模型 ⇒ checkpointer 白名单靠 `allowed_modules_for()` 从本标注派生，不手写；
    - `artifact_ids`：条件②，本次回收写进去的 EN-06 主键。门禁**只按这一批 id** 判定 ——
      换人重试会在同一张卡上再攒一批新行，读"这张卡的全部产物"就是让第二次尝试拿第一次的交付过关；
    - `validated_status` / `invalid_reason`：条件①的搬运，两列都在 EN-06，由 `ValidateArtifact` 覆写。
      路由读它，但**权威值在库里**（`test_the_gate_writes_the_verdict_it_routes_on`）。

    没有 `next_hop` 这一类键：LangGraph 的条件边直接吃路由器返回值，把它再存进 state 就是
    给"结论"造第二个载体（宪法原则 I）。
    """

    feature_request_id: uuid.UUID
    task_id: str
    result: TaskResult
    artifact_ids: list[uuid.UUID]
    validated_status: str
    invalid_reason: str


# ============================================================================
# P1-2：运行时校验（2026-10-30）
# ============================================================================
"""上面那三个 `TypedDict` 只是**类型提示**：Python 运行时不会因为它们拒绝任何东西。

一条真正脏的 state（`feature_request_id` 是个字符串、混进一个没人认领的键、`result` 是
半个 dict）进到图里之后，症状不是"报错"，而是**在某一步里以别的样子炸**：`session.get()`
拿到一个非 UUID 去查库，PG 回一句 `invalid input syntax for type uuid`；或者某个键悄悄
不参与合并，图跑到 END 而 state 里少了半截 —— 那是最贵的一类，因为它看起来跑完了。

所以入口节点先过一道 pydantic：形状不对就在这里抛，且**只报键名与原因，不回显值**
（state 里有需求原文，异常会被打进日志与留痕）。

**为什么是"入口那一格"而不是包一层 `invoke`**：包 `invoke` 只能校验调用方塞进来的那一份，
而 LangGraph 会把上一版 checkpoint 里的 state 一起并进来 —— 真正进到节点里的是合并后的
结果。校验摆在节点里，量到的就是节点实际拿到的那一份。

**为什么必需键按图分开**：三个图的入口契约本来就不同（拆单入口只需要需求单号；回投入口
需要需求单号 + 卡号 + 结果；选人入口需要需求单号 + 卡号）。写成三份必需、其余可选，
"缺了必需键"才是一句明确的拒绝，而不是让节点在 `state["task_id"]` 上抛 `KeyError`。
"""


class StateValidationError(RuntimeError):
    """图 state 不合形状。**在节点开跑之前**抛，消息里只有键名与原因，没有值。"""

    code = "state_invalid"

    def __init__(self, schema: str, problems: Sequence[str]) -> None:
        self.schema = schema
        self.problems = tuple(problems)
        super().__init__(f"{schema} 形状不合法（{len(self.problems)} 处）：" + "；".join(self.problems))


class _StateModel(BaseModel):
    """三个 state 模型的共同配置。

    `extra="forbid"`：多出来的键必须报错。LangGraph 的通道是**按标注建的**，一个没人认领的
    键既不会被合并也不会被读到 —— 它是一份"我以为它在"的假现场，越早拒越好。

    `strict=True`：不做隐式转换。`feature_request_id="…"`（字符串）在宽松模式下会被悄悄
    转成 UUID，于是"上游本该给 UUID 却给了字符串"这件事永远查不出来；而严格模式下它是一句
    `Input should be a valid UUID`，指到具体键。

    `frozen=True`：校验过的那一份只读。节点拿到它之后不会误改，改动只能通过返回增量。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class IntakeStateModel(_StateModel):
    """`IntakeState` 的入口契约：只认需求单号，其余键要么还没生成、要么必须是已知形状。"""

    feature_request_id: uuid.UUID
    summary_digest: dict[str, list[str]] | None = None
    clarification_waived: bool | None = None
    drafts: list[CardDraft] | None = None
    links: list[AcceptanceLink] | None = None
    card_order: list[str] | None = None
    card_ids: list[str] | None = None
    snapshot_id: uuid.UUID | None = None
    acceptance_map_rows: int | None = None
    request_status: str | None = None
    approval_ticket_id: uuid.UUID | None = None
    gate_decision: str | None = None
    card_status_after: str | None = None
    gate_asked_again: bool | None = None


class ExecutionStateModel(_StateModel):
    """`ExecutionState` 的入口契约：回收那一跳必须自带需求单号、卡号与结果。"""

    feature_request_id: uuid.UUID
    task_id: str
    result: TaskResult
    artifact_ids: list[uuid.UUID] | None = None
    validated_status: str | None = None
    invalid_reason: str | None = None


class AssignmentStateModel(_StateModel):
    """`AssignmentState` 的入口契约：选人那一跳只认需求单号 + 卡号。"""

    feature_request_id: uuid.UUID
    task_id: str
    executor_agent_id: str | None = None
    reviewer_agent_id: str | None = None
    acceptor_agent_id: str | None = None


ModelT = TypeVar("ModelT", bound=_StateModel)


def ensure_state(schema: type[ModelT], state: Mapping[str, object], *, at: str) -> ModelT:
    """把节点拿到的那一份 state 过一遍 `schema`；不合形状就抛 `StateValidationError`。

    `at` 是节点名，只为了在消息里指明"哪一跳收到的脏数据" —— 同一个 state 形状在三张图里
    的入口契约不同，不点名就分不清是谁拒的。
    """
    try:
        return schema.model_validate(dict(state))
    except ValidationError as exc:
        problems = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
            problems.append(f"{location}（{item.get('msg', '')}）")
        raise StateValidationError(f"{schema.__name__}@{at}", problems) from None
