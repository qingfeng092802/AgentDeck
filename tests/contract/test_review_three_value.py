"""Phase 17 P1：三值审查在协议形状层的落点（EN-08 / API 响应 / 人工写入口）。

三件事分开量，因为它们各有各的坏法：

1. **EN-08 的结论域**是 `values_of("review_verdict")`，本文件只引用、不重述。
2. **API 响应模型**必须能携带 `NEED_HUMAN`：`ReviewResponse` 与 `TaskReviewRoundView` 是
   "自动审查不确定"被看板读到的两条路，任何一条把枚举收窄回两值，不确定就会在响应序列化时
   变成 500 或被静默改写。
3. **人工写入口仍是布尔**：`ReviewFindingBody.met` MUST NOT 接受 null —— 人点"通过/打回"时
   必须给出确定判定；`null` 只属于自动审查的"我判不了"。两条路径分开，才不会有"人也能交一张
   不确定的单来绕过逐条判定"。
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from acp.api.tasks import ReviewFindingBody, ReviewResponse, TaskReviewRoundView
from acp.orchestrate.nodes import review as rv
from acp.persistence.enums import values_of


def test_review_response_accepts_all_three_domain_values():
    """响应模型对域内三值都放行；`NEED_HUMAN` 不是"未知字符串"而是可序列化的正常读数。"""
    assert set(values_of("review_verdict")) == {rv.APPROVE, rv.CHANGES_REQUESTED, rv.NEED_HUMAN}
    for verdict in values_of("review_verdict"):
        view = ReviewResponse(
            task_id="T01",
            feature_request_id=uuid.uuid4(),
            project_key="demo",
            verdict=verdict,
            card_status=rv.REVIEWING if verdict == rv.NEED_HUMAN else "ACCEPTING",
            round_no=1,
            findings=2,
            accepted=False,
            acceptance_proxy=None,
            accept_error=None,
            delivery=None,
            delivery_skipped="cards_pending:1",
            delivery_error=None,
            next="等人",
        )
        assert view.verdict == verdict


def test_review_history_round_carries_need_human_and_null_item_findings():
    """详情端点的审查历史要能显示一条 `NEED_HUMAN` 行，逐条判定里带 `met: null`。"""
    view = TaskReviewRoundView(
        round_no=1,
        verdict=rv.NEED_HUMAN,
        findings=2,
        actor_agent_id="deepseek-reviewer",
        created_at=dt.datetime.now(dt.timezone.utc),
        card_status_after=rv.REVIEWING,
        verdict_id=uuid.uuid4(),
        item_findings=[
            {"criterion": "首屏有标题", "met": True, "evidence": "index.html"},
            {"criterion": "好看", "met": None, "evidence": "主观标准"},
        ],
    )
    assert view.verdict == rv.NEED_HUMAN
    assert [item["met"] for item in view.item_findings] == [True, None]


def test_the_human_finding_stays_boolean_only():
    """人工写入口不许交 `met=null`：不确定只属于自动审查，人的"通过/打回"必须是确定判定。"""
    assert ReviewFindingBody(criterion="首屏有标题", met=True).met is True
    assert ReviewFindingBody(criterion="首屏有标题", met=False).met is False
    with pytest.raises(ValidationError):
        ReviewFindingBody(criterion="首屏有标题", met=None)


def test_the_domain_finding_keeps_null_in_its_json_payload():
    """`ReviewFinding` 是自动审查的载体：`met=None` 一路原样序列化成 JSON null，不许折成 false。"""
    payload = rv.ReviewFinding(criterion="好看", met=None, evidence="不可判定").to_payload()
    assert payload == {"criterion": "好看", "met": None, "evidence": "不可判定"}
