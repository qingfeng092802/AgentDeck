"""「增加需求」落进 digest 的那三条规则（owner 2026-10-28 第 2 条）。

**这一档不连 PG**：`extend_digest` 是纯函数，量的是"digest 长什么样"，
不是"端点有没有写库"（那条归 `tests/integration/test_request_extend.py`）。
分开的理由和会议那一层同源：形状错了会安静地骗人，而形状的判据不该被 Docker 的启停左右。

三条规则，每条都对应屏上一种假话：
1. 追加的话只进 `追加`，**`raw_input` 一个字不改** —— 改了"这一单是怎么来的"就查不回来；
2. `会议` 清空 —— 不清的话前端一见到够数的台词就判"会议已结束"，新的讨论反而看不见；
3. 三份文档挪进 `上一版` —— 不挪的话屏上把**上一版** PRD 当这一版；
   挪了但删掉的话，这一趟就没有修订底稿（`_effective_input` 要拿 PRD 拼提示词）。
"""

from __future__ import annotations

from acp.docs import ARCH_SECTION, PRD_SECTION, TOKENS_SECTION
from acp.meeting import MEETING_SECTION, ROUND_SECTION
from acp.serve_intake import (
    ADD_SECTION,
    PREV_DOCS_SECTION,
    WebIntakeRunner,
    extend_digest,
)

PRD_JSON = '{"title":"手冲咖啡官网","pages":["首页","商品","联系"]}'


def _digest() -> dict[str, list[str]]:
    """一条已经跑完的单：2 轮会议 8 句、三份文档、还有补全项。"""
    return {
        "自行补全项": ["页面数量取 3 个"],
        MEETING_SECTION: [f"{role}｜第 {i} 句" for i, role in enumerate(["orchestrator", "pm", "cto", "designer"] * 2)],
        ROUND_SECTION: ["2"],
        PRD_SECTION: [PRD_JSON],
        ARCH_SECTION: ['{"stack":"原生三件套"}'],
        TOKENS_SECTION: ['{"colors":{}}'],
    }


def test_the_original_requirement_is_not_touched_and_the_addition_gets_its_own_line() -> None:
    """`raw_input` 是"客户最初说的那句话"，追加**不改它**。

    存储上分开（`追加` 一格）、使用时合起来（`_effective_input`）——
    改掉原文的代价是"这一单最初要什么"永远查不回来了。
    """
    merged = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")
    assert merged[ADD_SECTION] == ["10:24｜再加一个改价页面"]
    assert merged["自行补全项"] == ["页面数量取 3 个"]


def test_the_meeting_is_cleared_so_the_new_discussion_is_visible() -> None:
    """不清台词的话，前端 `stageOf()` 一见到"轮数 × 4"条就判会议结束。

    表现：点完"增加需求"，屏上立刻显示"会议已锁定方案 / 正在落文档"，
    而真正在发生的是四岗位重开了一场会 —— 那一整场讨论一个字都看不见。
    """
    merged = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")
    assert merged[MEETING_SECTION] == []


def test_the_three_documents_move_aside_but_the_prd_is_kept_as_the_draft() -> None:
    """三份文档从读面消失，PRD 挪进 `上一版`。

    两件事同时要成立：
    · 读面（前端 `parseDocs`）只认 `PRD` / `架构` / `令牌`，三格空了 ⇒ 屏上那三格回到"正在写"；
    · 修订底稿不能丢 ⇒ `上一版` 里留着上一版 PRD，`_effective_input` 拼提示词要用它。
    """
    merged = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")
    assert PRD_SECTION not in merged
    assert ARCH_SECTION not in merged
    assert TOKENS_SECTION not in merged
    assert merged[PREV_DOCS_SECTION] == [PRD_JSON]


def test_the_round_count_survives_so_the_page_still_knows_the_denominator() -> None:
    """轮数不是"这一场会的台词"，是**这一单的配置** —— 追加不重置它。"""
    merged = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")
    assert merged[ROUND_SECTION] == ["2"]


def test_the_input_digest_is_not_mutated_in_place() -> None:
    """入参是从 ORM 读出来的对象：就地改会让"到底写没写"变成一件靠猜的事。"""
    original = _digest()
    snapshot = {key: list(value) for key, value in original.items()}
    extend_digest(original, "再加一个改价页面", stamp="10:24")
    assert original == snapshot


def test_two_additions_accumulate_in_order() -> None:
    """连加两次是两行，不是覆盖 —— 客户会一次补一句，第一次补的那句不该消失。"""
    once = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")
    twice = extend_digest(once, "手机端也要能用", stamp="10:31")
    assert twice[ADD_SECTION] == ["10:24｜再加一个改价页面", "10:31｜手机端也要能用"]


def test_the_effective_input_carries_both_the_additions_and_the_previous_prd() -> None:
    """喂给会议 / 文档的输入 = 原话 + 追加的话 + 上一版 PRD。

    缺最后那一段，四岗位会把追加需求当成**一条新需求**从零讨论，
    第一版里已经谈定的东西全部消失 —— 而 owner 要的正是"在这一版基础上增加"。
    """
    class _Row:
        raw_input = "做一个手冲咖啡官网"
        summary_digest = extend_digest(_digest(), "再加一个改价页面", stamp="10:24")

    text = WebIntakeRunner._effective_input(None, _Row())  # type: ignore[arg-type]
    assert text.startswith("做一个手冲咖啡官网")
    assert "再加一个改价页面" in text
    assert "上一版 PRD" in text
    assert PRD_JSON in text


def test_a_request_without_additions_feeds_the_raw_input_untouched() -> None:
    """没追加过的单，喂进去的就是原文 —— 不多一段提示词，也不多一次读 digest。"""

    class _Row:
        raw_input = "做一个手冲咖啡官网"
        summary_digest = {PRD_SECTION: [PRD_JSON]}

    assert WebIntakeRunner._effective_input(None, _Row()) == "做一个手冲咖啡官网"  # type: ignore[arg-type]


# ---------------------------------------------------------------- 「还在跑就别改 digest」

# 真链路（2026-10-28 第十轮）踩到的那个半应用状态，本段盯的就是它。
#
# 现场：执行 14 张卡的那十分钟里点了「增加需求」。`extend_request` 只挡 `status == "INTAKE"`，
# 而那一刻 `status` 早就不是 `INTAKE` 了 ⇒ 放行；digest 改完（追加写了、会议清了、文档挪进
# `上一版`）；`WebIntakeRunner.__call__` 那边 `acquire(blocking=False)` 拿到 `False`，
# 留痕写 `split_skipped / already_running` 就返回了 —— **会议一次都没开**。
# 屏上从此停在「会议无产出」，而库里那份 digest 说"正在重开一场会"。
#
# 为什么锁而不是 `status` 是真相：续跑者（`resumer`）是在 `_run` 里**同步**跑的，
# 所以"执行卡"的那十分钟里锁也 held 着。

import threading  # noqa: E402
import uuid  # noqa: E402

from pathlib import Path  # noqa: E402

from acp.api import requests as api  # noqa: E402
from acp.serve_intake import WebIntakeRunner  # noqa: E402


class _Runner:
    """只带一个锁表的替身：判据只关心"问得出来吗"，不关心它怎么跑。"""

    def __init__(self) -> None:
        self.locks: dict[str, threading.Lock] = {}

    def is_running(self, feature_request_id: object) -> bool:
        lock = self.locks.get(str(feature_request_id))
        return bool(lock is not None and lock.locked())


def test_busy_is_true_only_while_the_lock_is_held():
    runner = _Runner()
    fid = uuid.uuid4()
    assert api.runner_busy(runner, fid) is False

    lock = threading.Lock()
    lock.acquire()
    runner.locks[str(fid)] = lock
    try:
        assert api.runner_busy(runner, fid) is True
    finally:
        lock.release()
    assert api.runner_busy(runner, fid) is False


def test_a_runner_without_the_probe_falls_back_to_the_old_behaviour():
    """判据里的记账替身没有 `is_running` —— 问不出来就**不拒**，不能因此把追加全挡死。"""
    assert api.runner_busy(object(), uuid.uuid4()) is False


def test_a_probe_that_raises_does_not_turn_the_call_into_a_500():
    class Boom:
        def is_running(self, feature_request_id: object) -> bool:
            raise RuntimeError("锁表坏了")

    assert api.runner_busy(Boom(), uuid.uuid4()) is False


def test_the_real_runner_answers_from_its_own_lock_table():
    """`WebIntakeRunner.is_running` 与 `__call__` 用的是**同一张锁表**。

    这条钉的是"两边各拿一把锁"这种坏法：那样 `is_running` 永远答 False，
    而 `__call__` 那边照样 `acquire` 失败 —— 半应用状态原样复现，判据却是绿的。
    """
    runner = WebIntakeRunner(session_factory=lambda: None, bus_root=Path("."))
    fid = uuid.uuid4()
    assert runner.is_running(fid) is False

    lock = runner._lock_for(str(fid))
    lock.acquire()
    try:
        assert runner.is_running(fid) is True
    finally:
        lock.release()
    assert runner.is_running(fid) is False


def test_the_refusal_says_what_is_actually_going_on():
    """409 的正文要能直接念给人听：说清"还在跑"，而不是一句 `intake_running`。

    前端把 `detail` 原样贴在输入框下面（`OutFoot` 的 `err`），所以这句话就是用户看到的全部解释。
    """
    assert "还在跑" in api.BUSY_DETAIL
    assert "等它跑完" in api.BUSY_DETAIL
    # 码仍留在最前面：调用方要按码分支，人读的是码后面那半句
    assert api.BUSY_DETAIL.startswith("intake_running")
