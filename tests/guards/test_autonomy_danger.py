"""T107 的判据：SC-010 的五段 + owner 2026-10-15 两条裁决（⑳ 状态码一致性 / ㉔ 清单损坏必须红）。

档位：`tests/guards/`（fast 档），**混合文件**。第一到第三节要真 PG（档位读数与 D 类单只在库里读得出），
PG 那条 skip 挂在 `engine` fixture 上 ⇒ 凡请求 `session` 的用例自动 skip；第四节总线那一半、
第五到第七节不碰库，没有 `.env` 时仍然照跑（现跑读数：**带 PG 35 passed / 不带 PG 18 passed + 17 skipped**，
而不是整档消失 —— 少的只是"档位与单据"那 17 格）。

**这一档判据与 T104 / T105 的分工（为什么要新文件而不是往旧文件里加）**
`tests/guards/test_autonomy_guard.py` 判的是**判定点本身**（`decide()` 的顺序、唯一放行出口、
两条路径汇到同一个建单入口、三层锁）；`test_autonomy_intercept.py` 判的是**留痕的形状**。
本文件判的是 **SC-010 那三句可数的事实**（`spec.md:553`）：拦截率 100%、扩展项同样 100%、
审查/验收阶段写动作次数 0 —— 这三句的量词与"抽样三条命令"不同：**全体条目 × 两档** 才是 100% 的意思。
一处编号差要点名（不是抄错）：`plan.md` 设计项 9 的判据 ④ 是"按 `(agent_id, 清单指纹)` 回溯"，
那条已由 **T105** 钉住（`test_autonomy_intercept.py` 第四节）；owner 给 T107 的第 4 条对应的是
SC-010 第二句"审查与验收阶段发生写动作的次数 0" ⇒ 本文件第四节判的是后者。

**七组断言与失效形状**

| 节 | 判的 | 少了它会漏的失效形状 |
|---|---|---|
| 一 | 清单**每一条**在两档下各被拦一次（比率是关系式，不是抽样） | "三条抽样的样本全拦"冒充"清单 100% 生效"；`FULL` 被读成"免清单" |
| 二 | 使用者扩展项同样拦，且**扩展走的是 `acp.danger` 那条真实路径** | 扩展项落进"只写文件不参与判定"的空档；两份清单读法（loader vs 手搓） |
| 三 | 保守档下"非只读且不可自动回滚" ⇒ D 类单真建出来 | 判定红了而库里没单（"拦了但没人被叫来"）；两档实际同行为 |
| 四 | 审查/验收岗位写动作 = 0 次，**且拒写只发生在唯一那一处**、与档位互不替代 | "全拒"冒充 0 次；autonomy 变成第二处拒写；bus 变成第二个拦截点 |
| 五 | 不在清单内且只读/可回滚的动作**不被误拦** | 用"全拦"凑满 100% 拦截率（SC-010 的分母造假） |
| 六 | 契约 §1.5 的状态码表 ⇄ `HTTP_STATUS` 逐条一致（双向） | 契约改了而代码没改（或反之），使用者看到的码与文档两样 |
| 七 | 清单损坏三种形态下 `--show-danger` **红**，健康时不红 | "只呈现不校验"（裁决 ㉔ 点名的那条缺口） |
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import acp
from acp import selfcheck
from acp.approval import repository as approval_repo
from acp.bus import acl, layout
from acp.bus.gateway import BusError, BusGateway, GuardEvent
from acp.config import loader as config_loader
from acp.config.loader import DANGER_INTERCEPT, DSN_ENV, db_url, load_danger_list
from acp.danger import candidate_text
from acp.guard import autonomy as guard
from acp.persistence import autonomy as store
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot

#: PG 那条理由挂在 **fixture 上而不是逐条 marker 上**：凡要用档位的用例都请求 `session` ⇒ 自动 skip，
#: 不碰库的第六、七节照跑（把它们一起 skip 会让 fast 档在没 PG 的机器上少掉 8 条真判据）。
#: 为什么不用一排 `@needs_pg`：新加一条要库的用例而忘了挂 marker，就会在没有 PG 的机器上
#: 红成一条假缺陷（那种红会被读成"判据有问题"）；挂在 fixture 上做不到"漏"。
PG_SKIP_REASON = f"缺 {DSN_ENV} ⇒ 本条要真 PG（档位与 D 类单只在库里读得出），整条 skip（不是绿）"

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 六节里三处静态扫描的根 = **当前被 import 的那棵 `acp` 树**（口径同 T106 那次修正：
#: 按字面路径拼出来的 `仓库根/src/acp` 在变异跑里读的是没被改过的那棵树，AST 变异就进不了账）。
SRC_ROOT = Path(acp.__file__).resolve().parent
CONTRACT = REPO_ROOT / "specs" / "001-multi-agent-collab-platform" / "contracts" / "agent-protocol.md"

PROJECT = "acp-self-host"
AGENT = "qoder"
CARD = "T01"
CARD2 = "T02"
NOW = dt.datetime(2026, 10, 15, 16, 0, tzinfo=dt.timezone.utc)
TIERS = ("FULL", "SAFE")

#: 第四节的三个把关身份与三条被拒路径（路径表与 `test_readonly.py` 的 `DENIED_PATHS` 同一组，
#: 但这里**不 import 那个文件**：跨文件 import 判据会让两处的读数互相顶替，改一处另一处跟着变）。
EXECUTOR = "qoder"
REVIEWER = "doubao"
ACCEPTOR = "workbuddy"
GUARD_ACTORS = ("reviewer", "acceptor")
DENIED_PATHS = (
    layout.STATUS_MD,
    layout.DELIVERY_PACKAGE_JSON,
    f"{EXECUTOR}-workspace/index.html",
)

#: 第五节那批"应当放行"的动作（**每条都要当场证明它不在清单里**，否则这一节是空判据）。
UNLISTED_READ_ONLY = ("cat README.md", "git status", "ls -la", "python -m pytest -q")
#: 不可自动回滚但**不在清单内**的动作 —— 它在 SAFE 档要被第三节拦住，是反向对照的另一侧。
UNLISTED_WRITABLE = "deploy.sh --staging"


@pytest.fixture(scope="session")
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(PG_SKIP_REASON)
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
def scope(session: Session) -> uuid.UUID:
    """一张需求单 + 一条 snapshot + 两张卡（形状与 `test_autonomy_guard.py::scope` 一致：
    两张卡才分得出"两条路径各建一张单"，同卡第二次拦截会被 FR-045 合并掉）。"""
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
                 ref=f"local:t107-{snapshot_id}", rolled_back_at=None)
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
    """写 EN-16 的档位（有行就改、没行就建）。

    与 `test_autonomy_guard.py::set_mode` 同一取向：**不借被测路径的任何写者**
    （不碰 `set_autonomy_mode`、也不碰 `ensure_autonomy_state`）—— 起点若由被测函数生成，
    "读到哪一档"就变成自证。那两条函数自己的判据在 `test_autonomy_state.py`。
    """
    row = session.get(store.AutonomyState, PROJECT)
    if row is None:
        row = store.AutonomyState(
            project_key=PROJECT,
            danger_list_version=load_danger_list().fingerprint,
            updated_by="user-1",
            updated_at=NOW,
        )
        session.add(row)
    row.mode = mode
    session.flush()


def planned(command: str, *, read_only: bool = False, auto_rollback: bool = False, **overrides):
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


def held(session: Session, command: str, danger_list) -> bool:
    """这一发有没有被拦（两档都调用它，档位由 `set_mode` 事先定好）。"""
    return guard.decide(session, planned(command), danger_list).outcome == guard.DANGER_HIT


# ── 一、SC-010 第一句：清单里每一条动作，两档各被拦一次（拦截率 100%）───────────


@pytest.mark.parametrize("tier", TIERS)
def test_every_entry_on_the_shipped_list_is_held_in_this_tier(session: Session, tier: str) -> None:
    """**逐条**而不是抽样：`spec.md:553` 那句"拦截率 100%"的量词是"清单中的动作"全体。

    T104 那份判据抽的是 owner 点名的三条（`sudo` / `rm -rf` / `git push`），它证的是判定顺序；
    这一条证的是**清单整体生效** —— 少任一条（比如有人把某条 `pattern` 写成带前导 Tab 的形状，
    加载进来了却匹配不上）在抽样读数是绿的、在这一条上是红的。
    命令取 `pattern` 原文：匹配是"归一化后的连续词序列"，原文必然自匹配 ⇒ 这一条判的是"拦不拦"，
    不判"怎么写得像"。
    """
    set_mode(session, tier)
    loaded = load_danger_list()
    patterns = loaded.patterns
    assert patterns, "随仓清单为空 ⇒ 这一条会变成空判据（空清单在加载层就报错，这里再钉一次）"

    missed = [pattern for pattern in patterns if not held(session, pattern, loaded)]
    assert missed == [], f"{tier} 档下这些清单条目没被拦：{missed}"


def test_the_full_tier_waives_asking_not_the_list(session: Session) -> None:
    """两档各拦一次的"各"要可分：`FULL` 免的是**逐笔请示**，不免清单（FR-056 / FR-057）。

    做法是把同一发清单内动作在两档各判一次，要求两边都是 `DANGER_HIT`；
    真正的分档差在第三节（保守档多出一条请示）。少了这一条，"`FULL` 就全放行"这种实现
    会在第一条用例上红、但没人说得清到底是哪一档错了。
    """
    loaded = load_danger_list()
    command = loaded.patterns[0]

    set_mode(session, "FULL")
    full = guard.decide(session, planned(command), loaded)
    set_mode(session, "SAFE")
    safe = guard.decide(session, planned(command), loaded)

    assert (full.outcome, safe.outcome) == (guard.DANGER_HIT, guard.DANGER_HIT)
    assert (full.mode, safe.mode) == ("FULL", "SAFE"), "读数里的档位必须是库里那一档，不是猜的"
    assert full.hits and safe.hits


def test_the_rate_is_a_relation_and_the_sample_is_not_empty(session: Session) -> None:
    """**关系式而不是定值**：拦住数 == 清单条数，且条数 > 0。

    为什么不只写"三条命令被拦"：定值读数会被无关改动凑满（清单加了第 19 条而判据没动，
    那条就没人判过）。这里把"分母"直接取自身，加条目不需要改判据、漏条目一定红。
    """
    set_mode(session, "SAFE")
    loaded = load_danger_list()
    held_count = sum(1 for pattern in loaded.patterns if held(session, pattern, loaded))

    assert held_count == len(loaded.patterns) > 0


# ── 二、SC-010 第一句的扩展半边：使用者自己加的那条同样拦 ──────────────────────


def test_a_user_added_entry_is_held_in_both_tiers(session: Session, tmp_path: Path) -> None:
    """`user_added: true` 不是"备注"，它进的是同一张判定表（FR-058 那句"新增项 MUST 默认拦截"）。

    构造方式是**真的走一遍扩展入口**：取 `acp.danger.candidate_text()` 生成的那份文本（T106 的
    使用者侧写者产出的形状），落进临时文件、用生产读法 `load_danger_list(tmp)` 读回来。
    为什么不直接在用例里手搓 YAML：手搓的那份不是扩展入口会写出来的东西 ——
    两个形状里判据绿的可能是"扩展入口写了一条永远匹配不上的条目"，而这里能抓住它。
    """
    list_file = tmp_path / "danger_list.yaml"
    base = "- pattern: sudo\n"
    list_file.write_bytes(candidate_text(base, "acme-wipe --all").encode("utf-8"))
    extended = load_danger_list(list_file)

    assert extended.user_added_count == 1
    matched = extended.match("acme-wipe --all /data")
    assert [entry.pattern for entry in matched] == ["acme-wipe --all"]
    assert matched[0].user_added is True and matched[0].action == DANGER_INTERCEPT

    for tier in TIERS:
        set_mode(session, tier)
        assert held(session, "acme-wipe --all /data", extended), tier


def test_extending_the_list_moves_the_fingerprint_the_trace_records(
    session: Session, tmp_path: Path
) -> None:
    """加一条 ⇒ 指纹换版 ⇒ "当时是哪版清单"这句才有**两种可区分的读数**（回溯的另一把钥匙）。

    方向要说清：这条不是重复 `test_danger_list.py` 的指纹判据，而是接上判定那一侧 ——
    `Decision.danger_list_version` 取的是**手里这份**清单的指纹。两份清单都用生产读法
    （`load_danger_list`）从盘上读出来，不手搓 `DangerList` 对象：手搓一个"看起来不同的指纹"
    证的只是我改了那个字段，而不是"清单加了条目 ⇒ 指纹跟着变"。
    """
    narrow_file = tmp_path / "narrow.yaml"
    wide_file = tmp_path / "wide.yaml"
    narrow_file.write_bytes(b"- pattern: sudo rm -rf\n")
    wide_file.write_bytes(b"- pattern: sudo rm -rf\n- pattern: acme-wipe\n  user_added: true\n")
    narrow, wide = load_danger_list(narrow_file), load_danger_list(wide_file)
    assert narrow.fingerprint != wide.fingerprint

    set_mode(session, "SAFE")
    command = "sudo rm -rf /tmp/x"
    left = guard.decide(session, planned(command), narrow)
    right = guard.decide(session, planned(command), wide)

    assert (left.danger_list_version, right.danger_list_version) == (narrow.fingerprint, wide.fingerprint)
    assert left.danger_list_version != right.danger_list_version
    assert left.hits and right.hits

    # 再加一发"只有新版才盖得住"的命令。少了这两行，本节测的就只是"指纹跟着条目数变"，
    # 而不是"多出来的那一条**参与了判定**"—— P8-T107-4（`match()` 跳过 `user_added` 项）
    # 在第一版读数里是绿的，就是这个缺口。
    only_new = "acme-wipe /data"
    from_narrow = guard.decide(session, planned(only_new), narrow)
    from_wide = guard.decide(session, planned(only_new), wide)
    assert from_narrow.hits == () and list(from_wide.hits) == ["acme-wipe"], (
        from_narrow.hits,
        from_wide.hits,
    )
    assert from_wide.danger_list_version == wide.fingerprint


# ── 三、SC-010 第二句的前半：保守档下"非只读且不可自动回滚"触发人工请示 ─────────


def test_the_safe_tier_pages_a_human_and_the_ticket_lands_in_the_database(
    session: Session, scope: uuid.UUID
) -> None:
    """**请示不是"返回一个字符串"**：库里要有一张 D 类单，读得回、挂得住卡（"拦了但没人被叫来"红）。"""
    loaded = load_danger_list()
    assert loaded.match(UNLISTED_WRITABLE) == (), f"{UNLISTED_WRITABLE!r} 已在清单里 ⇒ 这一节判的就不再是保守档"
    set_mode(session, "SAFE")
    decision = guard.decide(
        session,
        planned(UNLISTED_WRITABLE, feature_request_id=scope),
        loaded,
    )
    assert decision.outcome == guard.SAFE_REVIEW
    assert decision.kind == guard.KIND_D

    ticket = guard.hold_ticket(session, decision, now=NOW)
    row = session.get(approval_repo.ApprovalTicket, ticket.ticket_id)
    assert row is not None and str(row.kind) == guard.KIND_D
    assert row.task_card_id == CARD and row.feature_request_id == scope
    assert "保守档" in str(row.payload) or "请示" in str(row.payload), str(row.payload)


def test_the_same_writable_action_is_released_under_full(session: Session) -> None:
    """**两档确实有差**：同一个"非只读且不可自动回滚"的动作，`FULL` 放行、`SAFE` 请示。

    这一条是第三节的存在理由 —— 如果实现把两档做成同一个行为，第一、二节仍然全绿
    （清单内动作本来就两档都拦），只有这一条会红。
    """
    loaded = load_danger_list()
    set_mode(session, "SAFE")
    safe = guard.decide(session, planned(UNLISTED_WRITABLE), loaded)
    set_mode(session, "FULL")
    full = guard.decide(session, planned(UNLISTED_WRITABLE), loaded)

    assert safe.outcome == guard.SAFE_REVIEW
    assert full.outcome == guard.ALLOW and full.allowed is True


@pytest.mark.parametrize(
    ("command", "read_only", "auto_rollback"),
    [
        (UNLISTED_WRITABLE, True, False),
        (UNLISTED_WRITABLE, False, True),
        (UNLISTED_WRITABLE, True, True),
    ],
)
def test_the_conservative_tier_asks_only_for_what_is_both_writable_and_hard_to_undo(
    session: Session, command: str, read_only: bool, auto_rollback: bool
) -> None:
    """请示的条件是两个**且**关系：只读或可自动回滚 ⇒ 不在 SAFE 档叫人（FR-056 的原话形状）。

    三种组合各一次：少任一半都该放行。为什么参数化而不是三条：这三个格子漏一条，
    "把 and 写成 or"的实现仍能在其余格绿 —— 穷举到 2×2 去掉已判的两格才闭合。
    """
    loaded = load_danger_list()
    set_mode(session, "SAFE")
    decision = guard.decide(session, planned(command, read_only=read_only, auto_rollback=auto_rollback), loaded)
    assert decision.outcome == guard.ALLOW, (read_only, auto_rollback, decision.outcome)


# ── 四、SC-010 第二句的后半：审查/验收阶段写动作次数 = 0（同一强制点，双向印证）──


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    for directory in (layout.RESULTS_DIR, f"{EXECUTOR}-workspace", f"{REVIEWER}-workspace",
                      f"{ACCEPTOR}-workspace"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    return tmp_path


def guard_actor(role_id: str, agent_id: str) -> acl.Actor:
    """把关岗身份：`adapter:<agent_id>` + `read_only=True`（值的唯一来源是 EN-04 读数，这里按生产形态构造）。"""
    return acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}{agent_id}", role_id, read_only=True)


def executor_actor() -> acl.Actor:
    return acl.Actor(f"{acl.ADAPTER_OWNER_PREFIX}{EXECUTOR}", "executor", read_only=False)


def test_a_guard_seat_lands_zero_writes_across_all_three_denied_paths(bus_root: Path) -> None:
    """**次数 = 0 的那个数要可数**：两个把关岗 × 三条路径 = 6 次尝试，落了 0 个文件、投了 6 条守卫事件。

    为什么在这里重写一遍 `test_readonly.py` 已经判过的三条路径：那条判据数的是"每条都被拒"，
    SC-010 这句数的是"写动作次数 0" —— 同一个事实的两种量词，而后者要求**尝试数非零**才不是空读数
    （一次都没试过也是 0）。两侧都断：`attempted == 6` 与 `landed == 0`。
    """
    events: list[GuardEvent] = []
    gateway = BusGateway(bus_root, audit_sink=events.append)
    attempted = 0

    for role, agent_id in zip(GUARD_ACTORS, (REVIEWER, ACCEPTOR)):
        for path in DENIED_PATHS:
            attempted += 1
            with pytest.raises(BusError) as caught:
                gateway.write_artifact(guard_actor(role, agent_id), path, "| x |\n", capability=None)
            assert caught.value.code == acl.READ_ONLY_VIOLATION, (role, path, caught.value.code)

    landed = [item for item in bus_root.rglob("*") if item.is_file()]
    assert attempted == len(GUARD_ACTORS) * len(DENIED_PATHS)
    assert landed == [], landed
    assert len(events) == attempted
    assert {event.code for event in events} == {acl.READ_ONLY_VIOLATION}, [event.code for event in events]


def _can_write_call_sites() -> list[str]:
    """`src/acp` 里"调用 `can_write`"的位置（定义处与导入语句不算调用）。"""
    found: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "can_write":
                    found.append(path.relative_to(SRC_ROOT).as_posix())
    return found


def test_the_refusal_is_written_at_exactly_one_place_in_the_runtime() -> None:
    """**双向印证的第一半**：拒写只有一个强制点（`bus/gateway.py`），SC-010 那句"次数 0"才有一个可查的落点。

    口径同 T104/T105 那两条 AST 判据：先证扫描器会开火（下面那条注入对照），再拿它去断"恰一处"。
    两处各拒一次 = 两条"同一个事实"的解释，那时"0 次"读的是哪一个就不确定了。
    """
    sites = _can_write_call_sites()
    assert sites == ["bus/gateway.py"], sites


def test_the_write_scan_fires_when_a_second_refusal_point_appears() -> None:
    """正对照（判据会开火）：在别处多调一次 `can_write`，"恰一处"这条必须红。

    注入形态就是最可能真实发生的那种：节点里"顺手再判一次"（`orchestrate/nodes/review.py` 的模块头
    明写它**不**重复实现第二道拒 —— 这条判据就是把那句话钉成读数）。
    """
    def sites_of(source: str) -> list[str]:
        return [
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", "")) == "can_write"
        ]

    assert sites_of("if not acl.can_write(actor, path, capability).allowed: raise BusError()") == [
        "acl.can_write"
    ]
    assert sites_of("def can_write(actor, path, capability):\n    return True\n") == [], "定义不该被算成调用"


def test_the_autonomy_guard_is_not_a_second_write_refusal_and_the_bus_is_not_a_second_interception(
    session: Session, bus_root: Path
) -> None:
    """**双向印证的第二半**：两条门互不替代，也互不顶替。

    两个方向各验一次，因为"同一强制点"这句话有两个可失效面：
    ① 拦截点若去判写权限 ⇒ 出现第二处拒写（同一个 `read_only` 事实两处成文）；
    ② 拒写点若去判档位 ⇒ 出现第二个拦截点（那时"清单内动作 100% 被拦"的分子就不只由 `decide()` 决定）。
    ①用静态读数（`guard/autonomy.py` 不 import `acp.bus`），②用真读数：`FULL` 档 + 不在清单 + 只读
    ⇒ `decide()` 放行，而同一个审查岗经网关写公共区**仍然被拒** ⇒ 拒写与档位无关。
    """
    guard_source = (SRC_ROOT / "guard" / "autonomy.py").read_text(encoding="utf-8")
    imported = {
        (node.module or "")
        for node in ast.walk(ast.parse(guard_source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(guard_source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [name for name in imported if name.split(".")[0] == "bus" or name.startswith("acp.bus")], imported

    loaded = load_danger_list()
    set_mode(session, "FULL")
    decision = guard.decide(
        session, planned("cat notes.md", read_only=True, auto_rollback=True), loaded
    )
    assert decision.allowed is True

    events: list[GuardEvent] = []
    gateway = BusGateway(bus_root, audit_sink=events.append)
    with pytest.raises(BusError) as caught:
        gateway.write_artifact(guard_actor("reviewer", REVIEWER), layout.STATUS_MD, "| x |\n", capability=None)
    assert caught.value.code == acl.READ_ONLY_VIOLATION
    assert [event.code for event in events] == [acl.READ_ONLY_VIOLATION]


def test_a_writable_seat_still_lands_its_own_write(bus_root: Path) -> None:
    """反向对照（不许把"谁都写不了"读成 0 次）：执行岗写自己 workspace ⇒ 落 1 个文件。

    少了这一条，"把所有写都拒掉"的实现第四节全绿 —— 而那种实现下把关与执行一起消失，
    SC-010 的"次数 0"就变成"总线不可用"的另一个名字（口径同 `test_readonly.py` 那条反向对照）。
    """
    events: list[GuardEvent] = []
    gateway = BusGateway(bus_root, audit_sink=events.append)
    target = f"{EXECUTOR}-workspace/index.html"
    written = gateway.write_artifact(executor_actor(), target, "<html><body>ok</body></html>\n",
                                     capability=acl.Capability(EXECUTOR, CARD))
    assert written.is_file and written.read_text(encoding="utf-8").startswith("<html>")
    assert events == []


# ── 五、SC-010 的反向半边：不在清单内且可自动回滚的只读动作不被误拦 ─────────────


@pytest.mark.parametrize("command", UNLISTED_READ_ONLY)
def test_an_unlisted_read_only_action_is_released_in_both_tiers(
    session: Session, command: str
) -> None:
    """**防"全拦"冒充 100%**：这四条在 `FULL` 与 `SAFE` 下都必须放行（只读 + 可自动回滚）。

    每条先自证"确实不在清单里"（`match()` 返回空）再断放行 ——
    顺序是刻意的：如果清单被改到把这些词也收进去了，这条用例要红在"清单形状变了"这一格，
    而不是安静地变成一条恒真的判据。
    """
    loaded = load_danger_list()
    assert loaded.match(command) == (), f"{command!r} 已经在清单里 ⇒ 这一节的反向对照失效了，先看清单"

    for tier in TIERS:
        set_mode(session, tier)
        decision = guard.decide(
            session, planned(command, read_only=True, auto_rollback=True), loaded
        )
        assert decision.allowed is True, (tier, decision.outcome)


def test_the_reverse_side_is_not_vacuous(session: Session) -> None:
    """**这一节真的判过东西**：放行数 > 0 且拦住数 > 0 同时成立（"全拦"和"全放"两种退化都当场红）。

    为什么不满足于参数化用例本身：参数化的格子如果因为清单变动全部变成"被拦"，
    上面那条会红，但那时读数只说"某条反向用例失败"；这条把两个方向各数一次，
    红的时候直接给出是哪一侧退化成了常数。
    """
    loaded = load_danger_list()
    set_mode(session, "SAFE")
    released = sum(
        1
        for command in UNLISTED_READ_ONLY
        if guard.decide(
            session, planned(command, read_only=True, auto_rollback=True), loaded
        ).allowed
    )
    held_count = sum(1 for pattern in loaded.patterns if held(session, pattern, loaded))

    assert released == len(UNLISTED_READ_ONLY) > 0
    assert held_count == len(loaded.patterns) > 0


# ── 六、裁决 ⑳：契约 §1.5 的状态码表与 HTTP_STATUS 逐条一致（正反双向）─────────


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


#: §1.5 里**有两张**"状态 / 稳定键"表：状态码映射（业务拒法，载体 = `HTTP_STATUS`）与注入项表
#: （fail-closed 的装配缺失，全是 503，**刻意不在 `HTTP_STATUS` 里**）。两张表的前两列表头一模一样，
#: 区分点只剩第三列 —— 所以这里按整行表头定锚而不是按前两列，并把"另一张不许混进来"钉成读数。
#: （这一格不是防御性写作：解析器第一版就是按前两列抓的，结果把 503 那两条抓了进来，
#: 判据当场红了两次 —— 红得对，但那两次红的是我的取法，不是契约与代码不一致。）
STATUS_TABLE_HEADER = ("状态", "稳定键", "什么时候")
INJECTION_TABLE_HEADER = ("状态", "稳定键", "装配方（T123）MUST 交什么")


def _section_15(text: str) -> str:
    """取出契约里 §1.5 那一节（锚点 = 标题那一行；**锚点不唯一就显式报错**，不静默取第一个）。"""
    heads = [index for index, line in enumerate(text.splitlines()) if line.startswith("### 1.5 ")]
    if len(heads) != 1:
        raise AssertionError(f"§1.5 的标题锚点读了 {len(heads)} 处（需要恰 1 处）⇒ 定锚漂了，先修锚点")
    body = text.splitlines()[heads[0] + 1 :]
    stop = next(
        (index for index, line in enumerate(body) if line.startswith("### ") or line.startswith("## ")),
        len(body),
    )
    return "\n".join(body[:stop])


def _table_rows(text: str, header: tuple[str, ...]) -> list[list[str]]:
    """按整行表头定位一张 markdown 表，返回它的数据行（分隔行与表尾之后的不算）。"""
    lines = _section_15(text).splitlines()
    anchors = [index for index, line in enumerate(lines) if _cells(line) == list(header)]
    if len(anchors) != 1:
        raise AssertionError(f"表头 {header} 在 §1.5 里读了 {len(anchors)} 处（需要恰 1 处）⇒ 契约的表头措辞改了")
    rows: list[list[str]] = []
    for line in lines[anchors[0] + 1 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if set("".join(cells)) <= {"-", ":", " "}:
            continue
        rows.append(cells)
    if not rows:
        raise AssertionError(f"表头 {header} 底下解析不出任何行 ⇒ 这条判据成了空判据")
    return rows


def _status_table(text: str) -> dict[str, int]:
    """解析 §1.5 那张"状态 / 稳定键 / 什么时候"的表（读的是**发出去的那份文档**）。"""
    table: dict[str, int] = {}
    for cells in _table_rows(text, STATUS_TABLE_HEADER):
        assert len(cells) == 3 and re.fullmatch(r"\d{3}", cells[0]) and cells[1].startswith("`"), cells
        table[cells[1].strip("`")] = int(cells[0])
    return table


def _injection_table(text: str) -> dict[str, str]:
    """§1.5 的另一张表：fail-closed 注入项。**它的载体不是 `HTTP_STATUS`**，
    所以它进判据的方式只能是"确认它没被混进比对"，见 `test_the_two_tables_stay_two_carriers`。

    状态列按**原文**留着而不是 `int()`：这一张表里有一格是 `—`（第三个注入项明写"不拦"、不给 503），
    把它折算成数字就等于把契约里那句"不过度失败"改掉。
    """
    return {cells[1].strip("`"): cells[0] for cells in _table_rows(text, INJECTION_TABLE_HEADER)}


def _status_drift(table: dict[str, int], mapping: dict[str, int]) -> list[str]:
    """双向差集：只在表里 / 只在常量里 / 两边都有但值不同。**两个方向共用这一个谓词**。"""
    drift: list[str] = []
    for code in sorted(set(table) | set(mapping)):
        if code not in mapping:
            drift.append(f"missing_in_constant:{code}")
        elif code not in table:
            drift.append(f"missing_in_table:{code}")
        elif table[code] != mapping[code]:
            drift.append(f"value:{code}:table={table[code]}≠constant={mapping[code]}")
    return drift


def test_the_contract_status_table_and_the_constant_agree_row_by_row() -> None:
    """逐条一致（owner 裁决 ⑳ 的处置原话）。**表成文的载体是文档，常量的载体是代码**，
    两边都比"码 + 稳定键"这一对，比一个方向就只有一半效力。

    解析的是契约文件本体（会被读者看到的那份文本），不是从 `HTTP_STATUS` 反向生成的表。
    """
    table = _status_table(CONTRACT.read_text(encoding="utf-8"))
    assert _status_drift(table, store.HTTP_STATUS) == []


def test_the_two_tables_stay_two_carriers() -> None:
    """锚点判据（两个方向）：§1.5 的状态码表只那三条，注入项表那两条 503 **不许进 `HTTP_STATUS`**。

    三条各钉一次：
    ① 比对取的表里不许混进 §1.4 / §1.3 的稳定键（`card_not_found`、`human_principal_required`
       是离得最近的两条，取范围一路读到文件末尾就会漏进来）；
    ② 注入项表确实解析得出（否则 ③ 是空断言）；
    ③ 注入项那几个 code 不在 `HTTP_STATUS` 里 —— 这是**要它不在**：503 说的是"装配方没交注入项"，
       不是"仓储拒了这个主体的写法"，把 503 塞进 `HTTP_STATUS` 等于让 fail-closed 缺失读成业务拒绝
       （而 T103 那条"缺注入项 ⇒ 503"的判据会跟着失去指向）。
    """
    text = CONTRACT.read_text(encoding="utf-8")
    table = _status_table(text)

    assert "card_not_found" not in table and "state_not_escalated" not in table
    assert "human_principal_required" not in table and "principal_required" not in table
    injected = _injection_table(text)
    codes_503 = {code for code, status in injected.items() if status == "503"}
    assert codes_503, injected
    assert codes_503 & set(store.HTTP_STATUS) == set(), sorted(codes_503 & set(store.HTTP_STATUS))
    assert any(status != "503" for status in injected.values()), "第三格『不给 503』那一行没解析出来"


@pytest.mark.parametrize(
    ("table", "mapping", "expected"),
    [
        # 改表不改常量（他的原话："改常量不改变契约表 / 改表不改常量，两种都该被拦"）
        (
            {"principal_not_allowed": 409, "mode_not_allowed": 422, "danger_list_required": 400},
            {"principal_not_allowed": 403, "mode_not_allowed": 422, "danger_list_required": 400},
            "value:principal_not_allowed:table=409≠constant=403",
        ),
        # 改常量不改表（同一个谓词的反方向）
        (
            {"principal_not_allowed": 403, "mode_not_allowed": 422, "danger_list_required": 400},
            {"principal_not_allowed": 403, "mode_not_allowed": 418, "danger_list_required": 400},
            "value:mode_not_allowed:table=422≠constant=418",
        ),
        # 文档多写一条代码没有的
        (
            {"principal_not_allowed": 403, "mode_not_allowed": 422, "danger_list_required": 400,
             "mode_locked": 423},
            {"principal_not_allowed": 403, "mode_not_allowed": 422, "danger_list_required": 400},
            "missing_in_constant:mode_locked",
        ),
        # 代码新增一个 code 而契约没跟上
        (
            {"principal_not_allowed": 403, "mode_not_allowed": 422},
            {"principal_not_allowed": 403, "mode_not_allowed": 422, "danger_list_required": 400},
            "missing_in_table:danger_list_required",
        ),
    ],
)
def test_the_drift_predicate_fires_in_both_directions(
    table: dict[str, int], mapping: dict[str, int], expected: str
) -> None:
    """**先证判据会开火**，再拿它去断契约与代码一致 —— 四个格子两两对应"文档改了/代码改了"两侧。

    这里的输入是字面串而不是从 `store.HTTP_STATUS` 取的值：拿实现常量验实现的话，
    常量本身变了这些格子会跟着变，判据就永不会红（这条口径写在 `evidence/phase-08.md` 的变异教训里）。
    """
    assert _status_drift(table, mapping) == [expected]


# ── 七、裁决 ㉔：清单损坏时 `--show-danger` 必须红（不是只呈现）──────────────────

#: 四种"这份清单不能用"的形态。①②③ 是 owner 逐字点的三形态，④ 是 loader 已有的"没有清单"那一格，
#: 一并钉住是因为它的失效方向与①②相同（读不到清单 ⇒ 谁都不拦）。
BROKEN_LISTS = (
    ("yaml 语法错", "- pattern: [unclosed\n  bogus: |\n"),
    ("清单为空（顶层空序列）", "[]\n"),
    ("清单为空（只有注释）", "# 一条都没有\n"),
    ("条目形状不合法（缺 pattern）", "- action: intercept\n"),
)


@pytest.mark.parametrize(("label", "text"), BROKEN_LISTS)
def test_a_broken_list_makes_show_danger_red(label: str, text: str, tmp_path: Path) -> None:
    """**裁决 ㉔ 的正半**：清单损坏 ⇒ `show_danger` 报 FAIL，而不是把坏清单照打出来。

    注入方式与限度要点名：这里走的是 `show_danger(path=…)` 那个判据注入位
    （生效清单的来源仍是 `loader.DANGER_LIST_PATH`，口径同 T106 里 `acp.danger` 的预检）。
    "生产路径上这一发到底会不会被跑到"由下面那条 `main` 那一格的用例钉，两件事分开写。
    """
    target = tmp_path / "danger_list.yaml"
    target.write_bytes(text.encode("utf-8"))

    status, lines = selfcheck.show_danger(path=target)

    assert status == selfcheck.FAIL, (label, status, lines)
    assert any("清单读不出" in line for line in lines), lines
    assert not any(line.startswith("entry 1 ::") for line in lines), "坏清单不许被逐条打出来"


def test_the_fingerprint_mismatch_form_is_red_too(tmp_path: Path) -> None:
    """第三形态"**指纹对不上**"：清单健康，而 EN-16 记的是另一版 ⇒ 必须红。

    这是三种里唯一需要两份载体的一条（前一形态靠 loader 自己就能判）。
    `recorded` 是注入的 EN-16 读数 `{project_key: 当时记的指纹}` ——
    **本函数不连库**，把真读数注进来是装配点（T123）的活，这条限度钉在下面那条"未注入"的用例里。
    消息里只出现 project_key 与两个摘要（指纹是摘要不是凭据），不出现清单条目以外的自由文本。
    """
    target = tmp_path / "danger_list.yaml"
    target.write_bytes(b"- pattern: sudo rm -rf\n")
    current = load_danger_list(target).fingerprint

    status, lines = selfcheck.show_danger(path=target, recorded={PROJECT: "sha256:" + "f" * 64})
    assert status == selfcheck.FAIL, lines
    joined = "\n".join(lines)
    assert "en16=MISMATCH" in joined and PROJECT in joined and current in joined

    agreed = selfcheck.show_danger(path=target, recorded={PROJECT: current})
    assert agreed[0] == selfcheck.PASS, agreed
    assert any("en16=一致" in line for line in agreed[1]), agreed[1]


def test_no_injection_says_so_instead_of_passing_quietly() -> None:
    """**限制度读数**：不注入 `recorded` ⇒ 打一行"未比对"，第③形态**不在本命令射程内**。

    为什么不顺手在这里连库：`import acp.selfcheck` 只用标准库这条边界（模块头写着，C1/C5 靠它），
    而 EN-16 的读法住在 `persistence/autonomy.py`。 ⇒ 生产调用（下一条用例那个 `main`）今天只挡得住
    ①②（loader 自己就报），③要等装配点把读数注进来。这条不是"留空当"，是把空当钉成读数：
    **谁把这一行读成"比对通过"，这一条就红**。
    """
    status, lines = selfcheck.show_danger()
    assert status == selfcheck.PASS
    assert any("未比对" in line for line in lines), lines
    assert not any("en16=一致" in line for line in lines), lines


def test_an_empty_en16_reading_is_blocked_not_passed() -> None:
    """`recorded={}` ⇒ **BLOCKED**："没有行可比对"不是"比对通过"（口径同 C3 对未初始化总线、C4 对缺 registry）。

    这一格很容易被写成 PASS（"没有过期项 ⇒ 一切正常"），而那正是把"库里还没建行"读成"策略已生效"。
    """
    status, lines = selfcheck.show_danger(recorded={})
    assert status == selfcheck.BLOCKED, lines
    assert any("没有任何行可比对" in line for line in lines), lines


def test_the_shipped_entry_point_exits_1_when_the_effective_list_is_broken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**入口那一格**：`python -m acp.selfcheck --show-danger` 在生效清单坏掉时退出码 1 并报 `DANGER FAIL`。

    上面几条判的是 `show_danger()` 这个函数，这条判的是**使用者实际敲的那一条命令**：
    把 `loader.DANGER_LIST_PATH` 换成一份坏文件（loader 在调用时读那个模块量 ⇒ 这是真入口的路径，
    不是给判据开的口子），然后跑 `selfcheck.main(["--show-danger"])`。
    顺带钉一条口径：**"没有清单"从来不是通过** —— loader 明写它不退回内置默认。
    """
    broken = tmp_path / "danger_list.yaml"
    broken.write_bytes(BROKEN_LISTS[0][1].encode("utf-8"))
    monkeypatch.setattr(config_loader, "DANGER_LIST_PATH", broken)

    assert selfcheck.main([selfcheck.SHOW_DANGER_FLAG]) == 1
