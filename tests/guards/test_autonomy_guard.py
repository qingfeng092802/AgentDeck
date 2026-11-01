"""T104 的判据：`guard/autonomy.py` 是唯一拦截点，两条路径汇到同一个 D 类出口（FR-056 / FR-057｜R-15）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿）：档位从 EN-16 读、D 类单落 EN-09，
两件事都只能在库里读出来；一律不 commit（外层事务 + 结束回滚）。

要回答的五件事，按 owner 的五条关键约束对齐：

1. **挂在适配器出站动作之前**（约束 1）—— 本仓目前**没有**适配器调用点（`src/acp` 里 `submit(` 的
   命中只有三处 `def` 与一处协议声明），所以这一格能断的是"判定与建单都发生在动作交出去之前"这个**顺序契约**，
   加上"调用点还不存在"这条读数（登记在 `evidence/phase-08.md`，归 T123）；
2. **先判清单**（约束 2）：`FULL` 档命中一样拦 ⇒ 与保守档那一条分开各断一次，不许合并成"一条拦截"；
3. **再判保守档**（约束 3）：`SAFE` + 非只读 + 不可自动回滚才请示；三条里任一条不成立就放行
   （这一条同时是 T107 ⑤ 的防"全拦"半边）；
4. **不存在第二条放行分支**（约束 4）：三层各钉一次 —— 判定矩阵（16 格只有 7 格放行，且
   AST 数得出"构造 `ALLOW` 的地方恰有一处"）、放行不许呈交成人审、D 类单不可能被免确认或超时放掉；
5. **与 Phase 4 同出口**（约束 5）：两条拦截路径建出来的单 `kind` 相同、且都出自
   `approval/service.create_approval`（本模块没有第二条建单路径）。

两处刻意的写法：
- **矩阵的期望值是手写的**，不是拿 `decide()` 自己算一遍再对照自己（那条断言恒真）；
- **命令原文进 payload 前必须被洗**：断的是"洗过"（`redaction_hits` 为假）与"原文不在单据里"，
  而不是断某个 `[REDACTED]` 的出现次数 —— 后者会把洗法本身的形状钉死，换一种洗法就红。
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp.approval import repository as approval_repo
from acp.approval import service
from acp.approval.timeouts import AUTO_PASS_ON_TIMEOUT_FORBIDDEN, KIND_BY_LETTER
from acp.config.loader import DSN_ENV, db_url, load_danger_list, redaction_hits
from acp.guard import autonomy as guard
from acp.persistence import autonomy as store
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.registry.risk_engine import Verdict, exemption_applies

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（档位与 D 类单都只在库里读得出）",
)

#: 静态扫描的根 = **当前被 import 的那份 `acp/guard/autonomy.py`**（口径同 `test_autonomy_danger.py` 的
#: `SRC_ROOT` 与 T106 那次修正）：按字面路径拼出来的 `仓库根/src/acp/…` 在变异跑里读的是**没被改过**的那棵树，
#: 本文件那两条 AST 判据（放行出口唯一、`ALLOW` 构造点数）就永远进不了变异账 —— 红不了不是变异太弱，是取错了文件。
#: 原先那行 `REPO_ROOT = Path(__file__).resolve().parents[2]` 在本文件里**只为拼这个路径而存在**，
#: 改完就没有第二个读者 ⇒ 一并删掉，不留一个"看起来还被用"的死常量（WorkBuddy phase8-review P2-1）。
GUARD_SOURCE = Path(guard.__file__).resolve()

PROJECT = "acp-self-host"
AGENT = "qoder"
CARD = "T01"
CARD2 = "T02"
NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
#: 只用于证明"命令原文不会原样进单据"的假凭据串（形状命中 `loader.FORM_PATTERNS` 的 `sk-` 那条）。
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
    """一张需求单 + 一条 snapshot + **两张卡**（两张卡才分得出"两条路径各自的 payload"，
    同一张卡的第二次拦截会被 FR-045 合并掉，见 `test_a_second_interception...`）。"""
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


def set_mode(session: Session, mode: str) -> None:
    """测试侧写 EN-16 的档位（有行就改，没行就建 —— 一条用例里换档两次是常态）。

    不借 `set_autonomy_mode`，也不借 `ensure_autonomy_state`：起点必须与被测行为无关，
    否则"读到的是哪一档"就成了自证（那条函数自己的判据在 `test_autonomy_state.py`）。
    """
    row = session.get(store.AutonomyState, PROJECT)
    if row is None:
        row = store.AutonomyState(
            project_key=PROJECT,
            danger_list_version="sha256:" + "0" * 64,
            updated_by="user-1",
            updated_at=NOW,
        )
        session.add(row)
    row.mode = mode
    session.flush()


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


# ── 一、判定：先清单、再保守档（约束 2 / 3）─────────────────────────────────

#: 穷举矩阵的**手写**期望值。`hit` = 命令在清单内（用 `rm -rf` / `sudo` 这两条真实条目构造）。
#: 顺序 = (mode, hit, read_only, auto_rollback)。16 格里 7 格放行、8 格清单拦、1 格保守档请示。
EXPECTED: dict[tuple[str, bool, bool, bool], str] = {}
for _mode, _hit, _ro, _rb in itertools.product(("FULL", "SAFE"), (True, False), (True, False), (True, False)):
    if _hit:
        _expected = guard.DANGER_HIT
    elif _mode == guard.SAFE and not _ro and not _rb:
        _expected = guard.SAFE_REVIEW
    else:
        _expected = guard.ALLOW
    EXPECTED[(_mode, _hit, _ro, _rb)] = _expected

#: 清单内与清单外的取样命令（都用随仓那份清单，不自己造清单 ⇒ 命中判据核的是真条目）。
LISTED_COMMANDS = ("rm -rf /var/tmp/x", "sudo shutdown now")
UNLISTED_COMMANDS = ("python -m pytest -q", "cat readme.md")


@pytest.mark.parametrize(("mode", "hit", "read_only", "auto_rollback"), list(EXPECTED))
def test_the_exhaustive_matrix_matches_the_two_rules(
    session: Session, mode: str, hit: bool, read_only: bool, auto_rollback: bool
) -> None:
    """16 格逐个跑，每格的期望值写在上面那张表里（不是让 `decide()` 自己给自己出题）。"""
    set_mode(session, mode)
    command = LISTED_COMMANDS[0] if hit else UNLISTED_COMMANDS[0]

    decision = guard.decide(
        session, action(command, read_only=read_only, auto_rollback=auto_rollback), load_danger_list()
    )

    assert decision.outcome == EXPECTED[(mode, hit, read_only, auto_rollback)]
    assert decision.mode == mode


def test_the_matrix_counts_are_not_all_one_side(session: Session) -> None:
    """**防"全拦"与防"全放"各一次**：16 格里放行恰 7、清单拦恰 8、保守档请示恰 1。

    计数取自上面那张表（同一个真值来源），所以这条不会与上一条互相自证；
    它挡的是"矩阵被改成恒拦/恒放而逐格断言跟着一起改"这种整体漂移 —— 那 times 三个计数会先对不上。
    """
    counts = {guard.ALLOW: 0, guard.DANGER_HIT: 0, guard.SAFE_REVIEW: 0}
    for expected in EXPECTED.values():
        counts[expected] += 1
    assert counts == {guard.ALLOW: 7, guard.DANGER_HIT: 8, guard.SAFE_REVIEW: 1}


@pytest.mark.parametrize("command", LISTED_COMMANDS)
def test_a_listed_command_is_held_in_both_tiers(session: Session, command: str) -> None:
    """约束 2 的两半：`FULL` 档命中**也**拦（`FULL` 免的是逐笔请示，不免清单）。"""
    for mode in ("FULL", "SAFE"):
        set_mode(session, mode)
        decision = guard.decide(session, action(command), load_danger_list())
        assert decision.outcome == guard.DANGER_HIT
        assert decision.hits, f"{command!r} 在 {mode} 档下应当点名命中的条目"


def test_the_hits_are_named_and_the_fingerprint_is_the_loaded_list_one(session: Session) -> None:
    """留痕要能回答"当时是哪版清单、命中了哪几条"（T105 的入参就在这里成形）。"""
    loaded = load_danger_list()
    set_mode(session, "SAFE")

    decision = guard.decide(session, action("sudo rm -rf /tmp/scratch"), loaded)

    assert set(decision.hits) == {"sudo", "rm -rf"}
    assert decision.danger_list_version == loaded.fingerprint


def test_an_unlisted_writable_action_waits_for_a_human_only_in_safe(session: Session) -> None:
    """约束 3（FR-056 第二条 + `spec.md:327` 的兜底）：清单没列不等于放行。"""
    set_mode(session, "SAFE")
    held = guard.decide(session, action("deploy.sh --prod"), load_danger_list())
    assert held.outcome == guard.SAFE_REVIEW

    set_mode(session, "FULL")
    passed = guard.decide(session, action("deploy.sh --prod"), load_danger_list())
    assert passed.outcome == guard.ALLOW and passed.allowed


@pytest.mark.parametrize(
    ("read_only", "auto_rollback"), [(True, False), (False, True), (True, True)]
)
def test_the_safe_tier_does_not_page_a_readonly_or_rollbackable_action(
    session: Session, read_only: bool, auto_rollback: bool
) -> None:
    """T107 ⑤ 的同一形状（此处是单元半边）：三种"至少一条不成立"都不该把人叫起来。

    挡的失效形状是"保守档 = 什么都请示" —— 那条会让 SC-010 的 100% 拦截率是"全拦"冒充的。
    """
    set_mode(session, "SAFE")
    decision = guard.decide(
        session, action("cat readme.md", read_only=read_only, auto_rollback=auto_rollback),
        load_danger_list(),
    )
    assert decision.outcome == guard.ALLOW


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_an_undeclared_command_is_never_a_release(session: Session, blank: str) -> None:
    """空白申报抛错而不是放行：**"不说要执行什么"不能等于"清单里没有"」**。

    这条同时钉住一个方向：抛的不是 `Decision`，所以调用方拿不到 `.allowed`，
    也没有任何一条路径能让它变成 `ALLOW`（`pytest.raises` 里若返回了 Decision 就是红）。
    """
    set_mode(session, "FULL")
    with pytest.raises(guard.UndeclaredAction):
        guard.decide(session, action(blank), load_danger_list())


def test_a_missing_row_is_read_as_the_default_tier_and_writes_nothing(session: Session) -> None:
    """无 EN-16 行 ⇒ 按 `DEFAULT_MODE` 判，**且不建行**（owner 处置表第 ③ 条）。

    这一条同时是 T102 交接项 ④ 的闭合半边：本模块是 `read_autonomy_state()` 的第一处消费者，
    默认值取的是仓储那一个常量（`store.DEFAULT_MODE`），不是这里另抄的字面串。
    判据写成关系式而不是 `"SAFE"` 字面串，但**同时**断一次字面值：
    只断关系式的话，"默认档被改成 FULL 而拦截条件跟着松"这条变异就看不见。
    """
    listed = action(LISTED_COMMANDS[1])
    decision = guard.decide(session, listed, load_danger_list())

    assert decision.mode == store.DEFAULT_MODE == "SAFE"
    assert decision.outcome == guard.DANGER_HIT
    assert int(session.scalar(select(func.count()).select_from(store.AutonomyState)) or 0) == 0

    unlisted = action("cat readme.md")
    assert guard.decide(session, unlisted, load_danger_list()).outcome == guard.SAFE_REVIEW


def test_the_case_of_a_command_does_not_change_the_verdict(session: Session) -> None:
    """大小写与空白写法不换判定（清单匹配两边都 casefold，T100 的口径）。

    反向半边也钉住：判定读的是词序列，`RM -RF` 里那条 `rm -rf` 照样命中 ⇒ 挡的是"改写一下就过"。
    """
    set_mode(session, "FULL")
    lower = guard.decide(session, action("rm -rf /tmp/a"), load_danger_list())
    upper = guard.decide(session, action("  RM  -RF  /tmp/a "), load_danger_list())

    assert lower.outcome == upper.outcome == guard.DANGER_HIT
    assert set(lower.hits) == set(upper.hits)


# ── 二、唯一放行出口（约束 4）──────────────────────────────────────────────


def _decision_outcome_calls(source: str) -> list[str]:
    """收集本模块里每一处 `Decision(...)` 构造点交出的 `outcome`。

    与 `test_autonomy_state.py` 的 AST 判据同款（把"代码形状"当断言对象），区别在于这里数的是
    **放行出口**：`outcome=ALLOW` 出现两次以上，就意味着模块里存在第二条放行路径。
    """
    outcomes = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Decision"):
            continue
        keyword = next((item for item in node.keywords if item.arg == "outcome"), None)
        if keyword is None:
            outcomes.append("?")
            continue
        value = keyword.value
        outcomes.append(getattr(value, "id", None) or getattr(value, "value", "?"))
    return outcomes


def test_the_release_exit_is_singular() -> None:
    """AST 数得出：构造 `Decision` 的地方恰三处，其中放行恰一处（`FULL` 与 `SAFE` 共用它）。"""
    outcomes = _decision_outcome_calls(GUARD_SOURCE.read_text(encoding="utf-8"))

    assert outcomes.count("ALLOW") == 1
    assert sorted(outcomes) == ["ALLOW", "DANGER_HIT", "SAFE_REVIEW"]


_ALLOWED_SHAPE = "Decision(outcome=ALLOW, mode='m', danger_list_version='v', hits=(), action=a)"
_HELD_SHAPE = "Decision(outcome=DANGER_HIT, mode='m', danger_list_version='v', hits=(), action=a)"


@pytest.mark.parametrize(
    "injected",
    [
        # 有人在清单判定之后加了一条"没命中就早点返回"的旁路
        f"def decide(a, b, c):\n    return {_HELD_SHAPE}\n    return {_ALLOWED_SHAPE}\n    return {_ALLOWED_SHAPE}\n",
        # 有人在另一个函数里也构造了一次放行（helper 里"再给一次机会"）
        f"def decide(a, b, c):\n    return {_ALLOWED_SHAPE}\n\n\ndef helper(a):\n    return {_ALLOWED_SHAPE}\n",
    ],
)
def test_the_scanner_catches_a_second_release_exit(injected: str) -> None:
    """上面那条的正对照：注入两种"多一处放行构造"的写法，扫描器都必须数得出 **2** 处。

    断的是"能数到 2"，不是"能数到 ≥1"——后者连"模块里根本没有放行"这种形状都会放过。
    """
    assert _decision_outcome_calls(injected).count("ALLOW") == 2


def test_a_pass_cannot_be_presented_as_a_human_ticket(session: Session) -> None:
    """放行不许被"顺手呈交成人审"：那会让同一次判定有两个地方各说一遍。"""
    set_mode(session, "FULL")
    decision = guard.decide(session, action("cat readme.md"), load_danger_list())

    assert decision.allowed and decision.kind is None
    with pytest.raises(guard.AutonomyGuardMisuse) as exc:
        guard.hold_ticket(session, decision)
    assert exc.value.code == "nothing_to_hold"


# ── 三、两条路径汇到 Phase 4 那一个出口（约束 5）────────────────────────────


def test_both_hold_paths_issue_the_same_kind(session: Session, scope: uuid.UUID) -> None:
    """清单命中与保守档请示 ⇒ 同一类卡点、同一个建单入口（`plan.md` 设计项 9 那句"同一个出口"）。"""
    set_mode(session, "SAFE")
    listed = guard.decide(session, action(LISTED_COMMANDS[0], feature_request_id=scope), load_danger_list())
    unlisted = guard.decide(
        session, action("deploy.sh --prod", feature_request_id=scope), load_danger_list()
    )
    assert listed.kind == unlisted.kind == guard.KIND_D == KIND_BY_LETTER["D"]

    first = guard.hold_ticket(session, listed, now=NOW)
    second = guard.hold_ticket(session, unlisted, now=NOW)
    assert first.kind == second.kind == guard.KIND_D


def test_the_ticket_carries_the_action_the_hits_the_tier_and_the_fingerprint(
    session: Session, scope: uuid.UUID
) -> None:
    """单据要能自证"拦的是什么、当时是哪版清单"（T105 留痕的同一组事实，两个载体各说各的活）。"""
    loaded = load_danger_list()
    set_mode(session, "FULL")
    decision = guard.decide(
        session, action("sudo rm -rf /tmp/scratch", feature_request_id=scope), loaded
    )

    ticket = guard.hold_ticket(session, decision, now=NOW)
    row = session.get(approval_repo.ApprovalTicket, ticket.ticket_id)
    assert row is not None
    text = str(row.payload)

    assert "rm -rf" in text and "sudo" in text
    assert loaded.fingerprint in text
    assert str(row.kind) == guard.KIND_D
    assert row.task_card_id == CARD and row.feature_request_id == scope


def test_the_suggested_verdict_differs_by_path(session: Session, scope: uuid.UUID) -> None:
    """建议结论按路径分：清单命中 = 建议不放行；保守档 = 建议补信息。

    钉住它是因为两处都填 `reject` 会让"保守档下这件事本可以补料通过"这种处理在读数里消失；
    而 `suggested_verdict` 是**建议**（结论的唯一载体仍是 EN-10 的 resolution），填哪个都不放松一道门。
    两条各挂一张卡：同一张卡的第二次拦截会被合并，那时读到的仍是第一张单的 payload（不是判据要的形状）。
    """
    set_mode(session, "SAFE")
    listed = guard.decide(
        session, action("sudo ls", feature_request_id=scope), load_danger_list()
    )
    unlisted = guard.decide(
        session,
        action("deploy.sh --prod", task_card_id=CARD2, feature_request_id=scope),
        load_danger_list(),
    )
    assert listed.outcome == guard.DANGER_HIT and unlisted.outcome == guard.SAFE_REVIEW

    held_listed = session.get(
        approval_repo.ApprovalTicket, guard.hold_ticket(session, listed, now=NOW).ticket_id
    )
    held_unlisted = session.get(
        approval_repo.ApprovalTicket, guard.hold_ticket(session, unlisted, now=NOW).ticket_id
    )

    assert held_listed.payload["suggested_verdict"] == "reject"
    assert held_unlisted.payload["suggested_verdict"] == "need_info"


def test_the_command_reaches_the_ticket_redacted(session: Session, scope: uuid.UUID) -> None:
    """命令原文进 payload 前必须被洗：EN-09 的 payload 是给人看的。"""
    set_mode(session, "FULL")
    decision = guard.decide(
        session, action(f"rm -rf /tmp/x --key {FAKE_KEY}", feature_request_id=scope), load_danger_list()
    )

    item = guard.pending_item(decision)

    assert FAKE_KEY not in item
    assert redaction_hits(item) is False
    assert "rm -rf" in item


def test_a_second_interception_on_the_same_card_merges_into_one_pending_ticket(
    session: Session, scope: uuid.UUID
) -> None:
    """FR-045 的合并在这里的后果：拦两次只挂一张单，逐次的事实归 EN-14（T105）。

    反向半边也在这一条里：合并回来的那张单**不被第二次判定改写**（payload 里还是第一次的命令），
    否则"人在批的那张单"与"平台最后拦下的那次动作"就会不是同一件事。
    """
    set_mode(session, "FULL")
    first = guard.hold_ticket(
        session, guard.decide(session, action("rm -rf /tmp/a", feature_request_id=scope), load_danger_list()),
        now=NOW,
    )
    second = guard.hold_ticket(
        session,
        guard.decide(session, action("sudo shutdown now", feature_request_id=scope), load_danger_list()),
        now=NOW,
    )

    assert second.merged and second.ticket_id == first.ticket_id
    assert int(session.scalar(
        select(func.count()).select_from(approval_repo.ApprovalTicket).where(
            approval_repo.ApprovalTicket.state == approval_repo.PENDING
        )
    ) or 0) == 1
    assert "/tmp/a" in str(session.get(approval_repo.ApprovalTicket, first.ticket_id).payload)


# ── 四、这张单不可能被自动放掉（约束 4 的外层两道锁）────────────────────────


def test_the_d_ticket_is_never_exempt_even_when_the_rules_say_exempt(session: Session) -> None:
    """三道锁的第二道：`exemption_applies` 只免除 A 类（FR-048），D 类照走。

    这一条特意**不经过** `guard`：本模块不传 `facts` 是第一道锁，而第一道锁挡的是"这一支调用"，
    第二道挡的是"整个平台" —— 只断第一道的话，将来有人给 `hold_ticket` 加上 `facts` 就静默开了门。
    """
    verdict = Verdict(exempt=True, rule=None, reasons=("probe",))
    assert exemption_applies(guard.KIND_D, verdict) is False
    assert exemption_applies(KIND_BY_LETTER["A"], verdict) is True


def test_the_d_ticket_has_no_timeout_release(session: Session, scope: uuid.UUID) -> None:
    """第三道锁：D 类在"超时不许自动放行"的集合里，且 §1 给它的就是"无超时 / 挂起等待"。"""
    set_mode(session, "FULL")
    ticket = guard.hold_ticket(
        session,
        guard.decide(session, action("rm -rf /tmp/b", feature_request_id=scope), load_danger_list()),
        now=NOW,
    )
    row = session.get(approval_repo.ApprovalTicket, ticket.ticket_id)
    policy = row.payload["timeout_policy"]

    assert guard.KIND_D in AUTO_PASS_ON_TIMEOUT_FORBIDDEN
    assert policy["auto_pass_forbidden"] is True
    assert policy["wait_seconds"] is None
    assert row.state == approval_repo.PENDING


def test_the_first_lock_holds_no_facts_means_no_exempt(session: Session, scope: uuid.UUID) -> None:
    """第一道锁：本模块建出来的单，`exempt` 与 `auto_passed` 都是假（因为没交 `facts`）。"""
    set_mode(session, "SAFE")
    ticket = guard.hold_ticket(
        session,
        guard.decide(
            session, action("deploy.sh --prod", feature_request_id=scope), load_danger_list()
        ),
        now=NOW,
    )
    assert ticket.exempt is False
    assert ticket.auto_passed is False


def test_a_d_ticket_without_a_card_is_refused_before_it_reaches_the_service(
    session: Session,
) -> None:
    """没挂卡的出站动作不许建 D 类单（`create_approval` 只拒"挂卡没需求单"那一半，这里补另外两半）。"""
    set_mode(session, "FULL")
    decision = guard.decide(session, action("rm -rf /tmp/c", task_card_id=None), load_danger_list())

    with pytest.raises(guard.AutonomyGuardMisuse) as exc:
        guard.hold_ticket(session, decision, now=NOW)
    assert exc.value.code == "card_required"
    assert int(session.scalar(select(func.count()).select_from(approval_repo.ApprovalTicket)) or 0) == 0
