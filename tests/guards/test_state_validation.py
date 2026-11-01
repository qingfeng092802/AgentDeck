"""P1-2：图状态在**节点开跑之前**要过一次运行时校验。

`TypedDict` 在运行时什么都不拦。脏 state 的后果不是"报错"，是**在别处以别的样子炸**：
非 UUID 的单号去打 PG（`invalid input syntax for type uuid`）、没人认领的键悄悄不参与合并、
`result` 是半个 dict 却一路走到写库。这个文件量三件事：

1. 三个入口模型各自拒什么（缺必需键 / 类型不对 / 多出来的键）；
2. 拒绝的消息**只带键名与原因，不回显值**（state 里有需求原文，异常会被打进日志）；
3. 拒绝发生在**节点里**（不是包一层 `invoke`）—— 所以真的 `invoke` 一次来看它拒。
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver

from acp.orchestrate import graph as orchestrate_graph
from acp.orchestrate.nodes.approval_gate import KIND as GATE_KIND
from acp.orchestrate.state import (
    AssignmentStateModel,
    ExecutionStateModel,
    IntakeStateModel,
    StateValidationError,
    ensure_state,
)
from acp.persistence.checkpointer import thread_config


# ---------------------------------------------------------------- 单元面


def test_a_missing_required_key_is_named_and_nothing_else():
    with pytest.raises(StateValidationError) as caught:
        ensure_state(IntakeStateModel, {}, at="plan")
    assert "feature_request_id" in str(caught.value)
    assert "Field required" in str(caught.value)


def test_a_wrong_type_is_refused_instead_of_coerced():
    """`"…"` 字符串在宽松模式下会被悄悄转成 UUID，于是"上游给错了类型"永远查不出来。"""
    with pytest.raises(StateValidationError) as caught:
        ensure_state(IntakeStateModel, {"feature_request_id": "not-a-uuid"}, at="plan")
    assert "feature_request_id" in str(caught.value)


def test_an_unclaimed_key_is_refused():
    """LangGraph 的通道按标注建 ⇒ 多出来的键既不会被合并也不会被读到，是一份假现场。"""
    with pytest.raises(StateValidationError) as caught:
        ensure_state(IntakeStateModel, {"feature_request_id": uuid.uuid4(), "extra": 1}, at="plan")
    assert "extra" in str(caught.value)


def test_the_message_never_echoes_the_value():
    """值可能是需求原文。异常会进日志与留痕，所以只许出现键名。"""
    secret = "客户的原话：把登录页改成三种语言，顺便把订单表删了"
    with pytest.raises(StateValidationError) as caught:
        ensure_state(ExecutionStateModel, {"feature_request_id": uuid.uuid4(), "task_id": 42, "result": secret}, at="result_ingest")
    message = str(caught.value)
    assert "result" in message
    assert "登录页" not in message and "订单表" not in message


def test_each_graph_declares_its_own_required_keys():
    """三张图的入口契约本来就不同；写成同一份会让"缺键"在某一张图上变成"允许"。"""
    only_id = {"feature_request_id": uuid.uuid4()}
    assert ensure_state(IntakeStateModel, only_id, at="plan").feature_request_id is not None
    with pytest.raises(StateValidationError):
        ensure_state(AssignmentStateModel, only_id, at="select_worker")
    with pytest.raises(StateValidationError):
        ensure_state(ExecutionStateModel, only_id, at="result_ingest")


def test_a_known_shape_passes_through():
    request_id = uuid.uuid4()
    checked = ensure_state(
        AssignmentStateModel,
        {"feature_request_id": request_id, "task_id": "T01", "executor_agent_id": None},
        at="select_worker",
    )
    assert checked.feature_request_id == request_id
    assert checked.task_id == "T01"


# ---------------------------------------------------------------- 图面（真 invoke 一次）


class _NoRow:
    """最小会话替身：`get()` 永远说没有这一行。

    用处是把"校验通过之后节点确实往下走了"和"校验没过"这两件事分开量 ——
    前者会走到 `RequestNotFound`，后者根本到不了 `get()`。
    """

    def __init__(self) -> None:
        self.lookups = 0

    def get(self, *_args, **_kwargs):
        self.lookups += 1
        return None


def test_the_entry_node_refuses_a_dirty_state_before_touching_the_session():
    session = _NoRow()
    graph = orchestrate_graph.build_graph(
        orchestrate_graph.OrchestrateDeps(
            session=session,  # type: ignore[arg-type] - 替身只需要 `get`
            splitter=None,
            checkpointer=MemorySaver(),
            bus_root=None,
        )
    )
    with pytest.raises(StateValidationError) as caught:
        graph.invoke(
            {"feature_request_id": "not-a-uuid"},
            thread_config(kind=GATE_KIND, feature_request_id=uuid.uuid4()),
        )
    assert "IntakeStateModel@plan" in str(caught.value)
    assert session.lookups == 0, "校验没过却已经查了库 ⇒ 守卫没站在节点入口"


def test_a_valid_state_gets_past_the_gate_and_then_hits_the_real_refusal():
    """反向对照：形状对了就往下走，撞到的是"库里没有这条需求单"，不是形状错。"""
    session = _NoRow()
    graph = orchestrate_graph.build_graph(
        orchestrate_graph.OrchestrateDeps(
            session=session,  # type: ignore[arg-type] - 同上
            splitter=None,
            checkpointer=MemorySaver(),
            bus_root=None,
        )
    )
    request_id = uuid.uuid4()
    with pytest.raises(orchestrate_graph.RequestNotFound):
        graph.invoke(
            {"feature_request_id": request_id},
            thread_config(kind=GATE_KIND, feature_request_id=request_id),
        )
    assert session.lookups == 1
