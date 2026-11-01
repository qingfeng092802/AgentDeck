"""T105 的判据：一次拦截要留下**流水**（EN-14 `kind=AUTONOMY_INTERCEPT`）与**当前态**（EN-16
`last_intercept_at`），而流水要能按 `(agent_id, 清单指纹)` 回溯（FR-056｜R-15，`plan.md` 设计项 9 判据 ④）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿）：流水落 EN-14、当前态落 EN-16、
索引是 PG 的原生对象，三件事都只能在库里读出来；一律不 commit（外层事务 + 结束回滚）。

要回答的六件事：

1. **记了哪四样**（约束 1）：`plan.md` 那句"记动作、命中条目、当时档位、清单指纹"逐样对位断一次，
   再加 `data-model.md` EN-16 分工段的"是否已转 D 类单"；
2. **回溯**（判据 ④）：两把钥匙 = EN-14 的 `actor_agent_id` 列 + `payload` 里的 `danger_list_version` 键。
   ⇒ 同 agent 两版指纹要分得开、两个 agent 同一版指纹也要分得开（只断"查得到"是分不出来的）。
   **这一节不测查询性能**：owner 2026-10-15 裁定不给这一对加索引（`pg_indexes` 里 `event_log` 只有
   `pk_event_log`）⇒ 回溯走全表扫，判据钉"查得到且分得开"，不钉"查得快"；
3. **放行不许进这张表**：进了就把 SC-010 那句"清单内动作 100% 被拦"的分母读成"判了几次"；
4. **只写自己那一列**：`last_intercept_at` 是 EN-16 的第三个写者，与"改档那三列"互不相交
   （AST 半边在 `tests/integration/test_autonomy_state.py` 的第四节，本文件钉的是读数侧）；
5. **单据去重、留痕逐次**（FR-045 的后果）：两次拦截 ⇒ 一张单 + 两行流水，缺一读不出"等过几次"；
6. **一条不落库的失败形状**：EN-16 缺行时整笔抛错，已经 `flush()` 的流水跟着回滚（SAVEPOINT 那条用例）。

两处刻意的写法：
- **起点不借被测函数**：EN-16 那一行由本文件的 `seed_tier()` 直接构造（口径同
  `test_autonomy_guard.py` 的 `set_mode()`），否则"当前态有没有被动过"这件事是它自己写的again；
- **脱敏断的是"洗过"而不是"某个占位串出现几次"**（`redaction_hits` 为假 + 原文不在），
  与 T104 同一选择；`hits` 是策略文本，原样留 —— 把它洗掉就读不出命中了哪条。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from acp.approval import repository as approval_repo
from acp.config.loader import (
    DSN_ENV,
    DangerList,
    db_url,
    load_danger_list,
    redaction_hits,
)
from acp.guard import autonomy as guard
from acp.persistence import autonomy as store
from acp.persistence import events
from acp.persistence.enums import enum_type_name, values_of
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import AutonomyState, EventLog, Snapshot

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（流水、当前态与索引都只在库里读得出）",
)

#: 同 `test_autonomy_guard.py`：静态扫描跟**被 import 的那棵树**走，否则变异跑里读的是没被改过的文件，
#: 本文件那两条 AST 判据（`events.record()` 调用点数 = 1、payload 键集合）就进不了变异账（P2-1）。
#: 原先的 `REPO_ROOT` 在本文件里只为拼这一行而存在 ⇒ 改完一并删掉，不留死常量。
GUARD_SOURCE = Path(guard.__file__).resolve()

PROJECT = "acp-self-host"
AGENT = "qoder"
OTHER_AGENT = "doubao"
CARD = "T01"
CARD2 = "T02"
NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(minutes=5)
#: 假凭据串：形状命中 `loader.FORM_PATTERNS` 的 `sk-` 那条，只用来证明"命令原文进 payload 前被洗"。
FAKE_KEY = "sk-FAKEfakeFAKEfake0"


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


@pytest.fixture
def scope(session) -> uuid.UUID:
    """一张需求单 + 一条 snapshot + 两张卡（两张才分得出"两次拦截各挂各的卡"，同卡会被 FR-045 合并）。"""
    request_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id, project_key=PROJECT, raw_input="做个介绍公司产品的静态网站",
            summary_digest={"自行补全项": [], "澄清问题": []}, clarification_waived=True,
            status="RUNNING", created_at=NOW,
        )
    )
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=[CARD, CARD2],
                 ref=f"local:probe-{snapshot_id}", rolled_back_at=None)
    )
    session.flush()
    for card_id in (CARD, CARD2):
        session.add(
            TaskCard(
                id=card_id, feature_request_id=request_id, title=f"卡 {card_id}", difficulty=2,
                skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
                artifact_floor_min_lines=12, artifact_floor_format="html",
                acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
                status="RUNNING", snapshot_id=snapshot_id,
            )
        )
    session.flush()
    return request_id


def seed_tier(session: Session, mode: str, *, fingerprint: str) -> AutonomyState:
    """种 EN-16 那一行（拦截路径要求行已存在）。**直接构造，不借 `store` 的任何写者**：
    "当前态被动了哪一列"这个问题必须由与被测行为无关的起点来答。

    一行是 PK（`project_key`）决定的 ⇒ 第二次进来只调起点，不重插，且**从不写
    `last_intercept_at`**：那一列正是被测的落点，起点碰它等于把判据的读数自己造出来。
    """
    row = session.get(AutonomyState, PROJECT)
    if row is not None:
        row.mode = mode
        row.danger_list_version = fingerprint
        session.flush()
        return row
    row = AutonomyState(
        project_key=PROJECT,
        mode=mode,
        danger_list_version=fingerprint,
        updated_by="user-1",
        updated_at=NOW,
        last_intercept_at=None,
    )
    session.add(row)
    session.flush()
    return row


def action(
    command: str, *, read_only: bool = False, auto_rollback: bool = False, **overrides
) -> guard.PlannedAction:
    fields = {
        "project_key": PROJECT,
        "agent_id": AGENT,
        "command": command,
        "read_only": read_only,
        "auto_rollback": auto_rollback,
        "task_card_id": CARD,
        "feature_request_id": None,
    }
    fields.update(overrides)
    return guard.PlannedAction(**fields)


def traces(
    session: Session, *, agent: str | None = None, fingerprint: str | None = None
) -> list[EventLog]:
    """按回溯的那两把钥匙读 EN-14。`as_string()` 渲染成 `payload ->> 'danger_list_version'`
    —— 这就是"按指纹回溯"在生产里要写成的形状（本轮不加索引，owner 裁定；读速代价记在
    `models_registry.EventLog` 的模块头与 `evidence/phase-08.md §十一`）。
    """
    stmt = select(EventLog).where(EventLog.kind == guard.INTERCEPT_KIND)
    if agent is not None:
        stmt = stmt.where(EventLog.actor_agent_id == agent)
    if fingerprint is not None:
        stmt = stmt.where(EventLog.payload["danger_list_version"].as_string() == fingerprint)
    return list(session.scalars(stmt))


def intercept(
    session: Session, command: str, *, mode: str = "SAFE", fingerprint: str | None = None,
    request_id: uuid.UUID | None = None, card: str = CARD, agent_id: str = AGENT,
    now: dt.datetime | None = None, **overrides,
) -> tuple[guard.Decision, EventLog]:
    """跑完整的一条链：判定 → 建单 → 留痕。返回 `(判定, 流水行)`。"""
    listed = load_danger_list()
    danger = listed if fingerprint is None else swapped_fingerprint(listed, fingerprint)
    seed_tier(session, mode, fingerprint=danger.fingerprint)
    decision = guard.decide(
        session,
        action(command, agent_id=agent_id, task_card_id=card, feature_request_id=request_id, **overrides),
        danger,
    )
    ticket = guard.hold_ticket(session, decision, now=now or NOW)
    return decision, guard.record_interception(session, decision, ticket, now=now or NOW)


def swapped_fingerprint(loaded: DangerList, fingerprint: str) -> DangerList:
    """同一份条目、换一个指纹。

    只用来造"清单换版而策略没变/变了"的两次拦截 —— 指纹是**留痕的键**而不是判定输入，
    所以这里改它不改变命中结果（`match()` 看的是 `entries`）。
    """
    return DangerList(entries=loaded.entries, fingerprint=fingerprint, source_sha256="0" * 64)


# ── 一、流水记了 plan.md 点名的那四样 ────────────────────────────────────────


def test_one_interception_leaves_exactly_one_trace_row(session: Session, scope: uuid.UUID) -> None:
    """拦一次 = 一行流水，且这张表上**只**多这一行（不顺手写别的 kind）。"""
    seed_tier(session, "SAFE", fingerprint=load_danger_list().fingerprint)
    before = session.scalar(select(func.count()).select_from(EventLog))

    decision = guard.decide(
        session, action("sudo rm -rf /tmp/scratch", feature_request_id=scope), load_danger_list()
    )
    ticket = guard.hold_ticket(session, decision, now=NOW)
    row = guard.record_interception(session, decision, ticket, now=NOW)

    assert session.scalar(select(func.count()).select_from(EventLog)) == before + 1
    assert row.kind == guard.INTERCEPT_KIND == "AUTONOMY_INTERCEPT"
    assert row.entity == "task_card" and row.entity_id == CARD
    assert row.actor_agent_id == AGENT
    assert row.created_at == NOW


def test_the_payload_records_the_four_things_the_plan_names(
    session: Session, scope: uuid.UUID
) -> None:
    """`plan.md` 设计项 9 的失败路径那句："记动作、命中条目、当时档位、清单指纹"。

    四样各断一次，且**值来自判定而不是测试自己**：动作是去空白后的原文（洗过），命中条目是
    `{"sudo","rm -rf"}`，档位取当时 EN-16 那一行（这里种 FULL —— 清单命中在任一档位都拦，
    而流水要能说出"当时是哪一档"，种 SAFE 就永远读不出 FULL 这一支）。
    """
    loaded = load_danger_list()
    _, row = intercept(session, "sudo rm -rf /tmp/scratch", mode="FULL", request_id=scope)

    payload = row.payload
    assert payload["action"] == "sudo rm -rf /tmp/scratch"
    assert set(payload["hits"]) == {"sudo", "rm -rf"}
    assert payload["mode"] == "FULL"
    assert payload["danger_list_version"] == loaded.fingerprint


def test_the_trace_also_says_which_ticket_it_escalated_to(
    session: Session, scope: uuid.UUID
) -> None:
    """`data-model.md` EN-16 分工段的"是否已转 D 类单"：流水要能指着那张单。

    `ticket_merged` 是同一件事的另一半 —— 并进已有单（FR-045）也是"已转"，只是转到了别人那张。
    """
    _, row = intercept(session, "sudo rm -rf /tmp/scratch", request_id=scope)
    listed = session.scalars(
        select(approval_repo.ApprovalTicket.id).where(
            approval_repo.ApprovalTicket.kind == guard.KIND_D
        )
    ).all()

    assert uuid.UUID(row.payload["ticket_id"]) in listed
    assert row.payload["ticket_merged"] is False


def test_the_safe_tier_review_path_leaves_the_same_kind_with_a_different_outcome(
    session: Session, scope: uuid.UUID
) -> None:
    """两条拦截路径在库里靠 `outcome` 分得开、靠 `kind` 归成一类。

    方向要说清：同一张表 = "拦截"这件事只有一个载体（不给两条路径各开一种 kind，
    那会让"拦了几次"变成两次求和）；`hits` 为空 = 这条是保守档请示而不是清单命中。
    """
    _, row = intercept(session, "deploy.sh --prod", request_id=scope)

    assert row.kind == guard.INTERCEPT_KIND
    assert row.payload["outcome"] == guard.SAFE_REVIEW
    assert row.payload["hits"] == []
    assert row.payload["read_only"] is False and row.payload["auto_rollback"] is False


def test_the_command_is_washed_but_the_policy_text_is_not(
    session: Session, scope: uuid.UUID
) -> None:
    """凭据形态不落 EN-14（宪法「二」+ EN-14 的 guardrail），策略文本照原样留。

    断的是"洗过"（`redaction_hits` 为假、原文不在），不是某个占位串出现几次 —— 后者会把洗法本身
    钉成形状，换一种洗法就红（与 T104 同一选择）。`hits` 里的 `rm -rf` 是被拦的原因，
    把它洗掉这行流水就再也读不出"命中了哪条"。
    """
    _, row = intercept(session, f"sudo rm -rf /tmp/{FAKE_KEY}", request_id=scope)

    assert FAKE_KEY not in row.payload["action"]
    assert not redaction_hits(str(row.payload))
    assert "rm -rf" in row.payload["hits"]


# ── 二、放行与坏形状：这张表不收 ─────────────────────────────────────────────


def test_a_released_action_leaves_no_trace_and_moves_no_column(
    session: Session, scope: uuid.UUID
) -> None:
    """**反向半边**（防"全拦"冒充 100%）：放行的动作不许进拦截流水。

    进来的话会同时坏两处读数 —— 按 `kind` 数出来的"拦了几次"变成"判了几次"，
    而 EN-16 的 `last_intercept_at` 会被一次根本没发生的拦截推进。
    """
    seed_tier(session, "FULL", fingerprint=load_danger_list().fingerprint)
    decision = guard.decide(
        session, action("cat readme.md", feature_request_id=scope), load_danger_list()
    )
    assert decision.allowed

    with pytest.raises(guard.AutonomyGuardMisuse) as exc:
        guard.record_interception(session, decision, None)
    assert exc.value.code == "nothing_to_log"
    assert traces(session) == []
    assert store.read_autonomy_state(session, project_key=PROJECT).last_intercept_at is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_actor_is_refused(session: Session, scope: uuid.UUID, blank: str) -> None:
    """主体列空 ⇒ 拒。这一条不是参数校验顺手加的：回溯按 `(agent_id, 指纹)` 点查，
    空主体的那一行**永远查不到**，它就是一条写在审计表里的谎。
    """
    seed_tier(session, "SAFE", fingerprint=load_danger_list().fingerprint)
    decision = guard.decide(
        session, action("sudo ls", agent_id=blank, feature_request_id=scope), load_danger_list()
    )
    ticket = guard.hold_ticket(session, decision, now=NOW)

    with pytest.raises(guard.AutonomyGuardMisuse) as exc:
        guard.record_interception(session, decision, ticket, now=NOW)
    assert exc.value.code == "actor_required"
    assert traces(session) == []


def test_a_cardless_interception_is_refused(session: Session, scope: uuid.UUID) -> None:
    """没有卡号就不留痕，也不改记成 `feature_request`：同一件事两种记法 ⇒ 按卡回溯会漏掉一半。

    与 `impact()`／`hold_ticket()` 的要求同向（一次出站动作必然属于一张卡）。
    """
    seed_tier(session, "SAFE", fingerprint=load_danger_list().fingerprint)
    decision = guard.decide(
        session, action("sudo ls", task_card_id=None, feature_request_id=scope), load_danger_list()
    )

    with pytest.raises(guard.AutonomyGuardMisuse) as exc:
        guard.hold_ticket(session, decision, now=NOW)
    assert exc.value.code == "card_required"
    assert traces(session) == []


def test_a_missing_tier_row_fails_closed_and_the_trace_rolls_back_with_it(
    session: Session, scope: uuid.UUID
) -> None:
    """EN-16 没行 ⇒ 整笔抛错，**而且已经 flush 的那行流水跟着回滚**。

    这条判据的形状：留痕与当前态在同一笔事务里（`events.record()` 与本模块都不 commit），
    所以用 SAVEPOINT 包住这一动、回滚它，然后要求流水读不出来。
    ⇒ "有流水而当前态没动"这种半程态不是靠注释避免的。方向也钉住：宁可这次拦截报错，
    也不留一条"当前态读不到、流水却说自己拦过"的账。
    """
    decision = guard.decide(
        session, action("sudo rm -rf /tmp/scratch", feature_request_id=scope), load_danger_list()
    )
    ticket = guard.hold_ticket(session, decision, now=NOW)
    savepoint = session.begin_nested()

    with pytest.raises(store.AutonomyStateMissing):
        guard.record_interception(session, decision, ticket, now=NOW)
    savepoint.rollback()

    assert traces(session) == []
    assert store.read_autonomy_state(session, project_key=PROJECT) is None


# ── 三、EN-16 的第三个写者：只动 last_intercept_at ───────────────────────────


def test_the_current_tier_row_moves_only_its_intercept_timestamp(
    session: Session, scope: uuid.UUID
) -> None:
    """拦一次之后，那一行只有 `last_intercept_at` 变了。

    另外四列各断一次原值（`mode` / `updated_by` / `updated_at` / `danger_list_version`）：
    这条判据挡的是模块头点名的那两个失效形状 —— "记一次拦截就把改档人覆盖成拦截时刻的操作者"、
    "重载清单的列被拦截顺手改掉"。AST 那半边在 `test_autonomy_state.py`，这里是读数侧。
    """
    loaded = load_danger_list()
    seed_tier(session, "FULL", fingerprint=loaded.fingerprint)
    decision = guard.decide(
        session, action("sudo ls", feature_request_id=scope), loaded
    )
    ticket = guard.hold_ticket(session, decision, now=LATER)
    guard.record_interception(session, decision, ticket, now=LATER)

    row = store.read_autonomy_state(session, project_key=PROJECT)
    assert row.last_intercept_at == LATER
    assert str(row.mode) == "FULL"
    assert row.updated_by == "user-1" and row.updated_at == NOW
    assert row.danger_list_version == loaded.fingerprint


def test_the_intercept_timestamp_is_a_reading_not_a_counter(session: Session, scope: uuid.UUID) -> None:
    """两次拦截 ⇒ `last_intercept_at` 推进到最后一次，流水两行。

    这一对读数把两件事分开了：当前态那一列回答"最近一次是什么时候"（一格的容量），
    "一共拦了几次"由 EN-14 的行数回答。合成一列计数器就会出现在改档时被清零那种事。
    """
    _, first = intercept(session, "sudo ls", request_id=scope, card=CARD, now=NOW)
    decision2 = guard.decide(
        session,
        action("git push origin main", task_card_id=CARD2, feature_request_id=scope),
        load_danger_list(),
    )
    ticket2 = guard.hold_ticket(session, decision2, now=LATER)
    second = guard.record_interception(session, decision2, ticket2, now=LATER)

    assert first.created_at == NOW and second.created_at == LATER
    assert len(traces(session)) == 2
    assert store.read_autonomy_state(session, project_key=PROJECT).last_intercept_at == LATER


# ── 四、回溯：判据 ④ 的那两把钥匙 ────────────────────────────────────────────


def test_two_fingerprints_are_separable_for_the_same_agent(
    session: Session, scope: uuid.UUID
) -> None:
    """同一支工具、两版清单 ⇒ 两条查询各数到一行，互不串。

    只断"查得到"是分不出来的：把 `danger_list_version` 键漏掉，两版就都数到同一行。
    ⇒ 这一条同时是"当时是哪版清单"这句话的可执行形式（FR-056 的留痕要求）。
    """
    loaded = load_danger_list()
    other = "sha256:" + "1" * 64
    assert other != loaded.fingerprint

    intercept(session, "sudo ls", request_id=scope, card=CARD, fingerprint=other)
    decision = guard.decide(
        session, action("git push origin main", task_card_id=CARD2, feature_request_id=scope), loaded
    )
    ticket = guard.hold_ticket(session, decision, now=LATER)
    guard.record_interception(session, decision, ticket, now=LATER)

    assert [row.payload["danger_list_version"] for row in traces(session, fingerprint=other)] == [other]
    assert [
        row.payload["danger_list_version"] for row in traces(session, fingerprint=loaded.fingerprint)
    ] == [loaded.fingerprint]


def test_two_agents_are_separable_for_the_same_fingerprint(
    session: Session, scope: uuid.UUID
) -> None:
    """同一版清单、两支工具 ⇒ 按 `actor_agent_id` 分得开。

    两把钥匙要**各**能切开一次：只测这一条会漏掉"指纹没写进 payload"那一半，
    只测上一条会漏掉"主体列写成固定串"那一半（`agent_id` 在 payload 里也有一份，
    但**回溯走的是列**，索引就是按列排的）。
    """
    loaded = load_danger_list()
    intercept(session, "sudo ls", request_id=scope, card=CARD, agent_id=AGENT)
    intercept(session, "git push origin main", request_id=scope, card=CARD2, agent_id=OTHER_AGENT)

    assert [row.actor_agent_id for row in traces(session, agent=AGENT)] == [AGENT]
    assert [row.actor_agent_id for row in traces(session, agent=OTHER_AGENT)] == [OTHER_AGENT]
    assert len(traces(session, agent=AGENT, fingerprint=loaded.fingerprint)) == 1


def test_a_different_kind_is_not_counted_as_an_interception(
    session: Session, scope: uuid.UUID
) -> None:
    """别的 `kind` 不许混进这个计数（九类共用一张表的代价，就在这里钉住）。

    种一行 `SELECTION`（同卡同主体）再数拦截流水 ⇒ 那条必须读不到。
    失效形状："漏了 `kind` 过滤" 会把选人流水算成拦截次数，SC-010 的分母就此虚高。
    这一条在没有索引的世界里**更重要**而不是更不重要：全表扫时唯一把两类流水分开的就是 `kind`。
    """
    seed_tier(session, "SAFE", fingerprint=load_danger_list().fingerprint)
    events.record(
        session,
        entity="task_card",
        entity_id=CARD,
        kind="SELECTION",
        actor_agent_id=AGENT,
        payload={"danger_list_version": "sha256:" + "2" * 64},
        created_at=NOW,
    )

    assert traces(session) == []
    assert traces(session, agent=AGENT) == []


def test_the_kind_landed_in_the_trace_is_a_domain_value(session: Session) -> None:
    """域对照：`INTERCEPT_KIND` 必须是 `event_log_kind` 的既有取值。

    这条不是"常量等于字面串"的自证（那种断言恒真）—— 它核的是**域里删掉这个标签时本模块当场红**，
    而不是等到 PG 在建索引或插行时报一句"数据库拒绝"。
    """
    assert guard.INTERCEPT_KIND in set(values_of("event_log_kind"))


# ── 五、零迁移那一侧：留痕需要的东西库里已经有了 ─────────────────────────────


def test_the_trace_needs_no_new_column_and_no_new_kind(session: Session) -> None:
    """T105 **不落任何 schema 改动**，这一条钉的是那两句前提。

    起因：启动指令里有一句"EN-16 加 `last_intercept_at`（需新迁移 0008）"。现跑核对不成立 ——
    那一列自 `0003` 就在（`migrations/versions/0003_t025_registry.py:127`，`nullable=True`），
    `AUTONOMY_INTERCEPT` 也早在 `event_log_kind` 域里（`persistence/enums.py:67`）⇒ 本轮不新增迁移。
    登记这一条是为了下一轮：照那句去建 0008 就是给同一个语义加第二份载体，
    而"加列"这一动在已有列上会直接报 `DuplicateColumn`（响亮的那种），"加域值"反而静默。

    判据形状是**关系式**而不是快照：只断"留痕需要的 ⊆ 库里已有的"。
    不断"库里只有这几列"，也不断"`event_log` 只有 PK 一个索引"—— 后者会把别人合法的加索引动作
    判成违规，而 owner 这一轮的裁定是"不加索引、代价落在读速上"（写在 `models_registry.EventLog`
    的模块头），那条决定由文档与全表扫的读速承担，不由一条断言承担。
    """
    columns = set(
        session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'autonomy_state'"
            )
        ).scalars()
    )
    labels = set(
        session.execute(
            text(
                "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                f"WHERE t.typname = '{enum_type_name('event_log_kind')}'"
            )
        ).scalars()
    )

    assert {"last_intercept_at", "danger_list_version", "mode"} <= columns
    assert {guard.INTERCEPT_KIND} <= labels


# ── 六、AST 半边：流水只有一个写点，payload 键集合钉死 ────────────────────────

#: 与 `guard.INTERCEPT_PAYLOAD_KEYS` 同集合的"缺一行"注入（正对照用）。
_INJECT_ONE_KEY_SHORT = (
    "def record_interception(s, d, t, **k):\n"
    "    return events.record(s, entity='task_card', entity_id='x',\n"
    "        kind='AUTONOMY_INTERCEPT', actor_agent_id='a', created_at=1,\n"
    "        payload={'action': 'x', 'mode': 'FULL', 'hits': [], 'ticket_id': 't',\n"
    "                 'ticket_merged': False, 'project_key': 'p', 'read_only': True,\n"
    "                 'auto_rollback': False})\n"
)


def _events_record_calls(source: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "events"
    ]


def _payload_keys_of(source: str) -> set[str]:
    calls = _events_record_calls(source)
    assert len(calls) == 1, f"流水写点有 {len(calls)} 处，扫描器不知道该认哪一个"
    payload = next(
        (kw.value for kw in calls[0].keywords if kw.arg == "payload"),
        None,
    )
    assert isinstance(payload, ast.Dict), "payload 必须是字面字典，否则键集合读不出来"
    return {key.value for key in payload.keys}


def test_the_trace_has_a_single_write_point_in_the_guard() -> None:
    """`guard/autonomy.py` 里对 `events.record()` 的调用**恰有一处**。

    这是 T104 那条"唯一出口"的留痕版：有了第二处，两张脸（流水与当前态）就会在不同函数里各写一套，
    "拦了但没留痕"这种形状就再也数不出来。范围声明：AST 只看源码里的调用点，
    不看运行期是否真的调到（那由第一节的读数用例管），也不看别的模块自己 INSERT EN-14
    （`persistence/events.py` 的"单点写入"那道由它自己的判据负责）。
    """
    assert len(_events_record_calls(GUARD_SOURCE.read_text(encoding="utf-8"))) == 1


def test_the_payload_key_set_is_exactly_what_the_doc_claims() -> None:
    """payload 的键集合 = `INTERCEPT_PAYLOAD_KEYS`（多一个少一个都红）。

    为什么值得钉：`plan.md` 与 `data-model.md` 两处各列了一份"留痕要记什么"，而库里那一列是
    自由形状的 jsonb ⇒ 没有 schema 会替我拦。少写 `danger_list_version` 这一格，
    第四节的回溯用例只会红在"查不到"，而这里能红在"根本没写"。
    """
    source = GUARD_SOURCE.read_text(encoding="utf-8")

    assert _payload_keys_of(source) == set(guard.INTERCEPT_PAYLOAD_KEYS)
    assert len(guard.INTERCEPT_PAYLOAD_KEYS) == len(set(guard.INTERCEPT_PAYLOAD_KEYS))


def test_the_scanner_actually_sees_a_missing_payload_key() -> None:
    """上面那条的**正对照**：少写 `outcome` 这一键，扫描器必须报出来。

    没有这一条，`== set(INTERCEPT_PAYLOAD_KEYS)` 这个绿点可能只是"扫描面本来就是空的"
    （同一个教训见 `test_autonomy_guard.py` 的 AST 一节）。
    """
    injected = _payload_keys_of(_INJECT_ONE_KEY_SHORT)

    assert "outcome" not in injected
    assert injected != set(guard.INTERCEPT_PAYLOAD_KEYS)


# ── 七、FR-045 合并下的逐次：一张单、两行流水 ────────────────────────────────


def test_two_interceptions_on_one_card_make_one_ticket_and_two_traces(
    session: Session, scope: uuid.UUID
) -> None:
    """同一张卡拦两次 ⇒ 单据被合并（FR-045），流水仍逐次。

    模块头那句"单据负责有人在等，流水负责等过几次"在这里成为可判的：
    第二次的 `ticket_id` 等于第一次的、`ticket_merged` 为真，而流水是两行。
    漏掉流水这一侧的话，第二次拦的那个动作在库里就**没有任何痕迹**了。
    """
    loaded = load_danger_list()
    _, first = intercept(session, "sudo ls", request_id=scope)
    decision2 = guard.decide(session, action("rm -rf /tmp/scratch", feature_request_id=scope), loaded)
    ticket2 = guard.hold_ticket(session, decision2, now=LATER)
    second = guard.record_interception(session, decision2, ticket2, now=LATER)

    rows = traces(session)
    assert len(rows) == 2
    assert first.payload["ticket_id"] == second.payload["ticket_id"]
    assert first.payload["ticket_merged"] is False and second.payload["ticket_merged"] is True
    assert session.scalar(
        select(func.count()).select_from(approval_repo.ApprovalTicket)
    ) == 1


def test_the_merged_trace_still_names_its_own_action(
    session: Session, scope: uuid.UUID
) -> None:
    """被并进已有单的那一次，流水仍写自己的命令。

    与上一条相邻但不是同一件事：单据的 payload **不追加**第二条命令（`create_or_merge` 的行为），
    所以"这一张单拦了哪些动作"这个问题只能从 EN-14 回答 —— 那一侧少一行，事实就永久丢了。
    """
    loaded = load_danger_list()
    intercept(session, "sudo ls", request_id=scope)
    decision2 = guard.decide(session, action("git push origin main", feature_request_id=scope), loaded)
    ticket2 = guard.hold_ticket(session, decision2, now=LATER)
    guard.record_interception(session, decision2, ticket2, now=LATER)

    actions = sorted(row.payload["action"] for row in traces(session))
    assert actions == ["git push origin main", "sudo ls"]
