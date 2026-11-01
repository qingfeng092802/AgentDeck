"""T125（US-06）：过程性事实抽查 —— 抽查 ≥10 张**真跑出来**的任务卡，三岗与四要素逐格从库里读回。

**抽的是什么数据**：三张需求单各走完整条链（受理段 → 人批 → 恢复 → `run_chain()` 一轮一张卡串行跑完
→ 交付包 → 公共区投影）= **12 张卡**。全部是链上数据，本文件不手写任何一行 `task_card`（那一格由
`test_the_sampled_cards_came_off_a_real_chain_not_a_hand_written_row` 反着钉）。
owner 2026-10-17 授权序列第 3 条（"再笔 C：T125 过程性事实抽查"）钉的就是这一发。

**四要素 ↔ 载体**（一格一个载体，不许拿投影文件冒充事实源）：

| 要素 | 载体 | 为什么是它 |
| --- | --- | --- |
| 谁做的 | EN-02 `task_card.executor_agent_id`（+ `reviewer_agent_id` / `acceptor_agent_id`） | 三岗的写者是 `SelectWorker` / `AssignGuards`，"两两不同"的强制点在 `guard/triple.py` |
| 做了什么 | EN-06 `artifact` 行（`rel_path` / `bytes` / `lines` / `format` / `self_reported_status`） | 回收点 `result_ingest.py:128` 是产物行唯一写者，且 `writer_agent_id` 被钉成"必须等于执行者"（FR-033） |
| 结果 | EN-02 `status` | 状态迁移以 EN-14 为准，但"现在是什么态"只有 EN-02 这一份 |
| 审查意见 | EN-08 `review_verdict.item_findings` | `record_verdict()` 是唯一入口，每条 finding 带 条目 + 判定 + 依据（FR-029） |

**为什么还要钉"归属"**：EN-14 `event_log` **没有** `feature_request_id` 列，`entity_id` 存的是卡号（`T01`…），
三张需求单一起跑就有三个 `T01`。所以验收留痕那一格按 **EN-14 主键区间**归属：每条链开跑前记一次
`max(id)`、跑完再记一次，只有落在这段区间里的行才许算到这条链的卡上。区间归区间 —— 别读成"库里能按需求单
反查留痕"，那一格今天仍不存在（owner 已裁"不加列"，见下段与诚实边界 ⑤）。

**owner 2026-10-17 裁 Q11 = 甲（零迁移 + 区间归属），并要一条区间边界判据** ⇒ 本文件末尾两发就是那条边界，
取的是**甲-B**（列上已有事实键就按事实查）：EN-08 `review_verdict` 挂卡用的是
`(feature_request_id, task_card_id)` 那条复合外键，"这条结论属于哪张卡"在 schema 里就是事实，
区间只是"够用"。两发钉的关系是：**推断必须与事实同数同署名**，且**区间外必须真的有别链的行**
（否则区间就退化成了全表扫，前面所有按区间筛出来的读数都不是这张卡在这一条链上的行）。
**两个选项的实测前提**（按列名读数选形，不按措辞）：他写的甲-B 条件句"若有 `task_id` 字段"——仓里那一列实名
`task_card_id`，另有 `feature_request_id` 与它组成外键，所以甲-B 成立；甲-A 要的两个时间戳列
（`task_card.created_at` / `review_verdict.decided_at`）**两边都不存在** ⇒ 走甲-A 就是加列 = 开迁移
= 他明令的"不做"。

诚实边界（这一发证明不到的）：① 数据来自 `StubAdapter` 的仿真链，不是真实工具的真实产出；
② 12 张卡形状相同（内置拆单固定四张：一张样式 + 三张页面），它证明的是"链上每张卡都能被问出这四格"，
不是"任意形状的卡都能"；③ 今天抽不到 `CHANGES_REQUESTED` / `REWORK` / `HANDED_OFF` / `ESCALATED` 那几形
（链上没走过返工与换人），所以"四要素在每个态都可查"这句只覆盖了交付那两个态；
④ 抽查不含 `daily-log.md` 与 `AGENTS.md` —— 那两份是可读影子与运行时副本，不是事实源，拿它们算"可查"
就是把投影当库；⑤ EN-14 无需求单列 ⇒ 跨需求单的留痕归属靠插入区间 —— 这是 **owner 2026-10-17 对 Q11 的裁定（甲）**，
不是临时妥协；"库里能按 `feature_request_id` 直接反查留痕"那一格今天仍不存在，要它就得加列 = 开迁移 = 硬停项，
裁定明写不做。末了两发钉的是**边界**（推断与事实同数同署名 + 区间外确有别链行），不是把这一格关掉；⑥ 本文件不勾 `tasks.md` 的 T125，勾选权归复核者。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import pathlib
import re
import subprocess
import uuid
from contextlib import closing

import psycopg
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp import chaos
from acp import run as run_module
from acp.approval import repository as repo
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import accept as accept_node
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import Artifact, TaskCard
from acp.persistence.models_registry import EventLog

SELF = pathlib.Path(__file__)
SPEC = SELF.parents[2] / "specs" / "001-multi-agent-collab-platform" / "tasks.md"
NOW = dt.datetime(2026, 10, 17, 13, 40, tzinfo=dt.timezone.utc)
RAW = "做个介绍公司产品的静态网站"
#: 抽查下限。**这个数字不是本文件挑的** —— 第一条静态判据拿它去比 `tasks.md` T125 那行的原文数字。
SAMPLE_MIN = 10
#: 三张需求单 × 内置拆单四张 = 12 张：**大于**下限，不刚好等于（等于下限会让"少抽几张"看不出来）。
REQUESTS = 3
CARDS_PER_REQUEST = 4
ENTRY = "index.html"
FINDINGS = run_module.StubFindingProvider()
ACCEPT = run_module.StubAcceptanceProvider()
#: 链跑完之后卡可能停的态（仿真链今天只有交付那一形，其余态抽不到 ⇒ 诚实边界 ③）。
LANDED = {accept_node.DELIVERED, accept_node.DELIVERED_WITH_ISSUES}
#: FR-029 那一行 findings 的三格（`review.Finding.to_payload()` 的键，条目 + 判定 + 依据）。
FINDING_KEYS = {"criterion", "met", "evidence"}


# ------------------------------------------------------------------ 小工具


class Sample:
    """一张被抽到的卡 + 它所属那条链的 EN-14 归属窗口。"""

    def __init__(self, request_id: uuid.UUID, card: TaskCard, window: tuple[int, int]) -> None:
        self.request_id = request_id
        self.card = card
        self.window = window

    @property
    def card_id(self) -> str:
        return str(self.card.id)


def project_key_of(index: int) -> str:
    return f"acp-t125-sample-{index}"


def bus_root_of(bus, project_key: str) -> None:
    """链上的存档点要求 `<bus>/<project_key>` 自己就是仓库顶层 ⇒ 每个项目键各起一个临时仓库。"""
    project = pathlib.Path(bus.path) / project_key
    project.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "."], ["config", "user.email", "test@invalid"], ["config", "user.name", "test"]):
        subprocess.run(
            ["git", *args], cwd=str(project), check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )


def drop_thread(session, feature_request_id: uuid.UUID) -> None:
    for kind in ("A_SPLIT", "B_ACCEPTANCE"):
        thread = cp.thread_id_for(kind=kind, feature_request_id=feature_request_id)
        with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
            chaos.kill_graph_state(conn, thread)


def event_high_water(session) -> int:
    """EN-14 当前的最大主键 —— 归属窗口的那条"之前"线。"""
    return int(session.scalar(select(func.max(EventLog.id))) or 0)


def run_one_chain(bus, session, index: int) -> tuple[uuid.UUID, int, int]:
    """一张需求单从受理跑到交付 + 投影，返回 `(需求单 id, 链前高水位, 链后高水位)`。"""
    project_key = project_key_of(index)
    bus_root_of(bus, project_key)
    intake = run_module.run_intake(RAW, project_key=project_key, transport="stub", bus_root=bus.path,
                                   session=session, now=NOW)
    request_id = intake.feature_request_id
    ticket = session.get(ApprovalTicket, intake.ticket_id)
    repo.resolve(
        session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
        principal="owner-test", approvers=frozenset({"owner-test"}), now=NOW,
    )
    run_module.run_resume(request_id, transport="stub", bus_root=bus.path, session=session)
    before = event_high_water(session)
    run_module.run_chain(request_id, transport="stub", bus_root=bus.path, session=session,
                         now=NOW, findings_for=FINDINGS, accept_for=ACCEPT, entry_name=ENTRY)
    return request_id, before, event_high_water(session)


def require_sample(cards, floor: int = SAMPLE_MIN) -> None:
    """样本不足就是**抽查没发生**，不是"少抽几张也算过" ⇒ 拒，而不是让后面的断言在小集合上恒真。"""
    if len(cards) < floor:
        raise AssertionError(
            f"这一发只抽到 {len(cards)} 张卡（下限 {floor}）⇒ 抽查不成立；要么链没跑完，要么样本被改小了"
        )


def verdicts_of(session, item: Sample) -> list[ReviewVerdict]:
    return list(session.scalars(
        select(ReviewVerdict).where(ReviewVerdict.feature_request_id == item.request_id,
                                   ReviewVerdict.task_card_id == item.card_id)
    ).all())


def artifacts_of(session, item: Sample) -> list[Artifact]:
    return list(session.scalars(
        select(Artifact).where(Artifact.feature_request_id == item.request_id,
                              Artifact.task_card_id == item.card_id)
    ).all())


def window_rows(session, item: Sample) -> list[EventLog]:
    """落在这一条链的 EN-14 区间里、且挂在这张卡号上的留痕（按主键升序）。"""
    return list(session.scalars(
        select(EventLog).where(EventLog.entity == "task_card", EventLog.entity_id == item.card_id,
                               EventLog.id > item.window[0], EventLog.id <= item.window[1])
        .order_by(EventLog.id)
    ).all())


def acceptance_rows(session, item: Sample) -> list[EventLog]:
    """区间里的那些行中，验收轮那一格（键名引用写侧的常量，不在这里再拼一遍字符串）。"""
    return [row for row in window_rows(session, item)
            if (row.payload or {}).get("action") == accept_node.ACTION_ROUND]


def review_events_in_window(session, item: Sample) -> list[EventLog]:
    """区间里"审查说过话"那些行。`kind=REVIEW` 的写者全仓只有一个（`review.py:272` 那一发），
    所以"一条结论 ↔ 一条留痕"这个 1:1 是可以拿来当边界用的：数不上就是有人在中间动过手脚。"""
    return [row for row in window_rows(session, item) if str(row.kind) == "REVIEW"]


def verdicts_by_card_number_only(session, card_id: str) -> list[ReviewVerdict]:
    """**故意**只用卡号查（丢掉需求单号那一半）—— 用来证明直接键不是装饰：
    三张单一起跑，卡号 `T01` 在这种查法下必然串号。"""
    return list(session.scalars(
        select(ReviewVerdict).where(ReviewVerdict.task_card_id == card_id)
    ).all())


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整节 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


@pytest.fixture
def sample(bus, session):
    """跑三条链，返回 12 个 `Sample`；抽查的结论留在各条判据里，夹具只保证样本下限。"""
    chains = [run_one_chain(bus, session, index) for index in range(REQUESTS)]
    try:
        rows: list[Sample] = []
        for request_id, before, after in chains:
            for card in session.scalars(
                select(TaskCard).where(TaskCard.feature_request_id == request_id).order_by(TaskCard.id)
            ).all():
                rows.append(Sample(request_id=request_id, card=card, window=(before, after)))
        require_sample(rows)
        yield rows
    finally:
        for request_id, _before, _after in chains:
            drop_thread(session, request_id)


# ------------------------------------------------------------------ 一、静态（不连库）


def test_the_sample_floor_is_the_number_the_task_row_asks_for():
    """下限必须等于 `tasks.md` T125 那行写的数字，而且每条抽查判据都得真的过这道门。

    为什么钉这一发：T125 的"10"是**规格**给的，不是实现者挑的。下一轮要是样本构造被改小（或者有人把
    `SAMPLE_MIN` 顺手改成 3 让现场跑得快），`require_sample` 就不再拦任何东西，而三格判据在 4 张卡上照样全绿
    —— 那是"抽查"两个字最静默的死法。三格各挡一半：
    ① 数字与规格那行一致（规格改了这里就红，逼着下一笔同步）；
    ② 每个连库判据的函数体里都调了 `require_sample`（漏一格 = 那一格能在小样本上蒙过）；
    ③ `require_sample` 自己真的会拒（9 拒 10 过）—— 不测这一发，它可能只是个 `pass`。
    """
    line = next(row for row in SPEC.read_text(encoding="utf-8").splitlines() if "T125 [US-06]" in row)
    asked = int(re.search(r"抽查 (\d+) 个任务", line).group(1))
    assert SAMPLE_MIN == asked, f"规格要 {asked} 张，本文件只保证 {SAMPLE_MIN} 张 ⇒ 抽查缩水"
    assert REQUESTS * CARDS_PER_REQUEST > SAMPLE_MIN, "样本刚好等于下限会让少抽几张看不出来 ⇒ 构造面要留余量"

    tree = ast.parse(SELF.read_text(encoding="utf-8"))
    audits = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        and any(arg.arg == "sample" for arg in node.args.args)
    ]
    assert len(audits) >= 3, f"连库的抽查判据只剩 {len(audits)} 发 ⇒ 三格（三岗 / 四要素 / 审查轮次）没抽齐"
    for node in audits:
        called = [
            getattr(call.func, "id", None) for call in ast.walk(node) if isinstance(call, ast.Call)
        ]
        assert "require_sample" in called, f"{node.name} 没过样本门 ⇒ 它能在小样本上蒙过去"

    with pytest.raises(AssertionError):
        require_sample(list(range(SAMPLE_MIN - 1)))
    require_sample(list(range(SAMPLE_MIN)))


# ------------------------------------------------------------------ 二、真 PG（抽 12 张）


def test_the_sampled_cards_came_off_a_real_chain_not_a_hand_written_row(session, sample) -> None:
    """抽查的第一问不是"格子里有没有值"，而是**这些卡是不是链上跑出来的** —— 手写一行就能骗过后三发。

    四格各是一个只有真跑才会有的痕迹：① 状态停在交付那一形；② EN-06 有产物行（回收点写得出，手插的卡没有）；
    ③ 这张卡的验收轮留痕落在**它所属那条链的 EN-14 区间**里（区间外的行不算）；④ 产物记账人 == 执行岗
    （FR-033 那条在链上是被 `result_ingest` 拒掉的，所以能同时读到两列一致 = 走的是回收点而不是 insert）。
    """
    require_sample(sample)
    assert len({item.request_id for item in sample}) == REQUESTS, "三张需求单没抽齐 ⇒ 这是单张计划的四个影子"
    for item in sample:
        assert str(item.card.status) in LANDED, item.card
        assert artifacts_of(session, item), f"卡 {item.card_id} 没有 EN-06 产物行 ⇒ 这一张没经过回收点"
        assert acceptance_rows(session, item), f"卡 {item.card_id} 在本链区间内没有验收轮留痕 ⇒ 不是链上跑完的"
        writers = {row.writer_agent_id for row in artifacts_of(session, item)}
        assert writers == {item.card.executor_agent_id}, (
            f"卡 {item.card_id} 的产物记账人 {writers} 不等于执行岗 ⇒ 这行不是 `result_ingest` 写的"
        )


def test_every_sampled_card_answers_the_triple_and_the_three_seats_are_pairwise_distinct(
    session, sample
) -> None:
    """三岗可查 **且** 两两不同，逐卡量；再把两列交叉核一次，挡"列填了但填的是别人的名字"。

    ① 三列都非空且两两不同（FR-031 的强制点在 `guard/triple.py`，这里读的是它守过之后的现值）；
    ② EN-08 那行的 `reviewer_agent_id` 必须等于卡上的 `reviewer_agent_id` —— 审查意见的作者就是那一岗，
      两处读起来不一样就说明有一列是事后补的；
    ③ 验收轮留痕的 `actor_agent_id` 必须等于卡上的 `acceptor_agent_id`（同上，验收岗那一格）。
    ④ NULL 不当"天然不相等"用：三列里任何一列是 NULL 就直接红（`TaskCard` 那三列都可空，
      可空只表示"指派尚未发生"，而链跑完之后它不该再是 NULL）。
    """
    require_sample(sample)
    for item in sample:
        seats = (item.card.executor_agent_id, item.card.reviewer_agent_id, item.card.acceptor_agent_id)
        assert all(seats), f"卡 {item.card_id} 的三岗里还有空位：{seats}"
        assert len(set(seats)) == 3, f"卡 {item.card_id} 三岗不是两两不同：{seats}"
        rows = verdicts_of(session, item)
        assert rows, item.card_id
        assert {row.reviewer_agent_id for row in rows} == {seats[1]}, (
            f"卡 {item.card_id} 的 EN-08 作者 {sorted({row.reviewer_agent_id for row in rows})}"
            f" 不等于审查岗 {seats[1]}"
        )
        rounds = acceptance_rows(session, item)
        assert {row.actor_agent_id for row in rounds} == {seats[2]}, (
            f"卡 {item.card_id} 的验收留痕署名 {sorted({row.actor_agent_id for row in rounds})}"
            f" 不等于验收岗 {seats[2]}"
        )


def test_every_sampled_card_answers_the_four_elements_and_the_opinion_is_not_an_empty_shell(
    session, sample
) -> None:
    """四要素逐卡读回，每格都问出**内容**而不是问出"这一列存在"。

    · 谁做的 = EN-02 执行岗（上面那发已钉非空，这里当 key 用）；
    · 做了什么 = EN-06 那几行的 `rel_path` / `bytes` / `lines` / `format` / `self_reported_status`
      —— 尺寸与格式都是回收点从工具回执里带的，全 0 或空路径就是"做了但说不出做了什么"；
    · 结果 = EN-02 `status`，并与本链验收轮载荷里的 `card_status_after` **交叉核**（留痕说的现值
      与列上的现值分叉 = 有人绕过状态机改列）；
    · 审查意见 = EN-08 `item_findings` 非空、每条带 条目 + 判定 + 依据三格（FR-029），且
      `extra_criteria_used` 恒 `False`（引了未登记的判据就是"审查意见"掺了私货）。
    """
    require_sample(sample)
    for item in sample:
        assert item.card.executor_agent_id, item.card_id
        for row in artifacts_of(session, item):
            assert str(row.rel_path).strip(), f"卡 {item.card_id} 有产物行的路径是空的"
            assert row.bytes > 0 and row.lines > 0, (row.rel_path, row.bytes, row.lines)
            assert row.format is not None and row.self_reported_status is not None, row.rel_path
        rounds = acceptance_rows(session, item)
        assert rounds, f"卡 {item.card_id} 在本链区间内没有验收轮留痕 ⇒ 结果那一格没有对手可供交叉核"
        assert str(item.card.status) == str(rounds[-1].payload["card_status_after"]), (
            f"卡 {item.card_id} 的现值 {item.card.status} 与验收留痕 {rounds[-1].payload['card_status_after']}"
            " 分叉 ⇒ 状态列被人改过"
        )
        rows = verdicts_of(session, item)
        for verdict in rows:
            findings = list(verdict.item_findings or [])
            assert findings, f"卡 {item.card_id} 的审查意见是空壳 ⇒ 四要素里那一格只有行没有内容"
            for finding in findings:
                assert FINDING_KEYS <= set(finding), finding
                assert str(finding["criterion"]).strip() and str(finding["evidence"]).strip(), finding
            assert verdict.extra_criteria_used is False, verdict.id


def test_every_completed_card_has_at_least_one_verdict_row_and_walked_an_acceptance_round(
    session, sample
) -> None:
    """`review_verdict ≥ 1` **且** `ACCEPTING` 真的走过一轮 —— 两个证据一个在 EN-08、一个在 EN-14 + EN-02。

    "走过一轮"不只看有没有验收行，还核三处对得上：① 区间内最大 `round_no` 等于卡上的 `review_round`
    （P1-5 之后只有验收段会推进这个计数器，所以它俩必须相等）；② 载荷里的 `limit` 等于实体那个上限的现值
    （`accept.round_limit()`，宪法硬值，不在这里抄数字）；③ 同区间里有一条 `kind=REVIEW` 的行排在验收行
    **之前** —— 这就是"三岗互斥已通过、审查说过话之后才进 `ACCEPTING`"那句话在数据上的形状。
    干净交付那一形还要求 `unmet == 0` 且清单原文是空列表（`DELIVERED_WITH_ISSUES` 反过来要求非空）。
    """
    require_sample(sample)
    for item in sample:
        assert len(verdicts_of(session, item)) >= 1, f"卡 {item.card_id} 一条审查结论都没有"
        rounds = acceptance_rows(session, item)
        assert rounds, item.card_id
        assert item.card.review_round >= 1, f"卡 {item.card_id} 的验收轮次还是 0 ⇒ 没过 ACCEPTING 那一轮"
        last = rounds[-1].payload
        assert int(last["round_no"]) == item.card.review_round, (last["round_no"], item.card.review_round)
        assert int(last["limit"]) == accept_node.round_limit(), last["limit"]
        kinds = [str(row.kind) for row in window_rows(session, item) if row.id < rounds[0].id]
        assert "REVIEW" in kinds, f"卡 {item.card_id} 的验收行之前没有 REVIEW 留痕 ⇒ 顺序不对"
        if str(item.card.status) == accept_node.DELIVERED:
            assert int(last["unmet"]) == 0 and last[accept_node.OPEN_ISSUES_ITEMS] == [], last
        else:
            assert int(last["unmet"]) >= 1 and last[accept_node.OPEN_ISSUES_ITEMS], last


def test_the_verdict_to_card_link_is_a_direct_composite_key_not_a_card_number_alone(
    session, sample
) -> None:
    """Q11 裁甲之后的**边界判据（甲-B）**：审查结论挂在"这张卡"上靠的是列上的复合键，不是推断。

    EN-08 `review_verdict` 与 EN-02 `task_card` 之间是 `(feature_request_id, task_card_id)` 那条复合外键
    （`models_core.py` 的裁决 2 落下来的形状）⇒ "这条结论属于哪张卡"在 schema 里就是**事实**。
    与"不采信自述"同源：能按事实查的就不许用推断查。三格各挡一种错：
    ① 每卡按两列筛都读得到行，且行上的 `feature_request_id` 就是我抽的这张单；
    ② **只用卡号查必然串号**（三张单都有 `T01`）⇒ 这一格把"直接键"从装饰变成必要条件：
      裸查的行数必须**严格大于**按复合键查的，等号成立就意味着有人把单号那一半悄悄丢了；
    ③ 汇总数等于卡的张数 ⇒ 本轮仿真链每卡恰好一轮审查，多出来的是重复判定、少了就是漏判。
    """
    require_sample(sample)
    total = 0
    for item in sample:
        rows = verdicts_of(session, item)
        assert rows, f"卡 {item.card_id} 按 (需求单, 卡号) 读不到审查结论"
        assert {str(row.feature_request_id) for row in rows} == {str(item.request_id)}, item.card_id
        bare = verdicts_by_card_number_only(session, item.card_id)
        assert len(bare) > len(rows), (
            f"卡号 {item.card_id} 裸查 {len(bare)} 行、按复合键查 {len(rows)} 行 ⇒ 两数相等就是有人把"
            "需求单那一半丢了（跨单串号在这种现场是必然的：三张单都有 T01）"
        )
        total += len(rows)
    assert total == len(sample), (total, len(sample))


def test_the_interval_attribution_and_the_direct_key_agree_on_every_card(session, sample) -> None:
    """区间归属只是"够用"，它必须与列上的事实**同数同署名** —— 不一致就以事实那侧为准并红。

    owner 2026-10-17 裁 Q11 = 甲（零迁移 + 区间归属），并要一条区间边界判据。这一发就是那条边界：
    ① 一条结论配一条 `kind=REVIEW` 留痕（写者全仓只有一个 ⇒ 1:1 可当边界）；
    ② 留痕的 `actor_agent_id` 与结论行的 `reviewer_agent_id` 都必须等于卡上的审查岗 ——
      三份读法（列、留痕、EN-02 岗）说出同一个名字才叫"可查"；
    ③ **区间外必须有别的链的行**：按卡号裸查 EN-14 的总数要严格大于区间内的行数。
      这一格是防"区间其实是全表扫"那种假归属 —— 如果三张链的留痕全都落进同一个区间，
      前面那几条判据就都跑在被污染的行集上，绿是真的绿但量的不是这张卡。
    """
    require_sample(sample)
    for item in sample:
        rows = verdicts_of(session, item)
        events = review_events_in_window(session, item)
        assert len(events) == len(rows), (item.card_id, len(events), len(rows))
        assert {str(row.actor_agent_id) for row in events} == {item.card.reviewer_agent_id}, item.card_id
        assert {row.reviewer_agent_id for row in rows} == {item.card.reviewer_agent_id}, item.card_id
        inside = len(window_rows(session, item))
        outside = session.query(EventLog).filter(
            EventLog.entity == "task_card", EventLog.entity_id == item.card_id
        ).count()
        assert outside > inside, (
            f"卡 {item.card_id} 的区间外没有别的链的留痕（{outside} ≤ {inside}）⇒ 区间退化成了全表扫，"
            "上面那些按区间筛出来的行不再是这张卡在这一条链上的行"
        )
