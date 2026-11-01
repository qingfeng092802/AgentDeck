"""T089 + T090 + T091 + T092 + T093：审查结论与验收轮次。

档位：`tests/guards/`，混合文件。前四节是纯函数 + 源码扫描（fast，不碰 PG）；
第五节之后要真 PG（各挂 `skipif`，读数注明带 `.env`）。

**为什么本文件有一半在扫源码**。这三条任务里有两条的判据不是"某个输入给某个输出"，
而是"这个值只有一个来源"：
- T089 的结论域值 MUST 取自 PG 枚举（`data-model.md §2` 的域值引用规矩是 owner 2026-10-14
  在 Phase 6 收口轮立的，本文件是它第一个消费方）；
- T091 的轮次上限 MUST 取自宪法原则 III，且**本仓 MUST NOT 有第二个解析它的正则**
  （Phase 4 已有 `approval/timeouts.py` 在读这句话；`accept.py` 第一版自己写了一份，
  两份对同一句话有两种失败方式 —— 一份取第一处、一份要求恰一处，那是原则 I 的违例，
  已在收口轮合成一份）。
这种"只有一个来源"的断言，用值比对是量不出来的：把 `VERDICTS` 写成字面量元组，
它照样等于枚举的读数；把上限写死成 `3`，它照样等于宪法当前的读数。所以第四节用 AST 扫源码，
并且**每条扫描都配一条注入对照**（往临时目录放一份真违例），否则"扫出来是空"可能只是扫描器坏了。

反向对照在本文件的职责（owner 的纪律：每条新判据至少一条"坏法不红"的反证）：
临时目录里那份**只用散文提到这句话**的文件 MUST NOT 被报出来 —— 少了这一条，
"只有一处解析"绿的原因可能只是"任何提到这句话的文件都被报"，那它就不是在量解析器数量。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.approval import timeouts as T
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import accept as ac
from acp.orchestrate.nodes import review as rv
from acp.persistence.enums import values_of
from acp.persistence.models_approval import AcceptanceMap, ReviewVerdict
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.registry import roles as role_registry

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 9, 5, tzinfo=dt.timezone.utc)
EXECUTOR = "qoder"
REVIEWER = "doubao"
ACCEPTOR = "workbuddy"

#: 卡上声明的审查标准（FR-029 的比对口径）。
CARD_CRITERIA = ["首屏有标题", "样式表被引用"]
#: EN-07 映射表里的验收条目（FR-030 的比对口径）。**故意与上面那组不同名**：
#: 两处同名的话，"验收到底读哪张表"这个区别就量不出来了（可互换的取值来源必须是不同值）。
MAP_ITEMS = ["页面能打开", "标题出现"]

SRC_ROOT = Path(ac.__file__).resolve().parents[2]
LIMIT_PHRASE = "验收轮次上限"


def _findings(met_by: dict[str, bool], order: tuple[str, ...] | None = None) -> list[rv.ReviewFinding]:
    keys = order if order is not None else tuple(met_by)
    return [
        rv.ReviewFinding(key, met_by.get(key, False), f"qoder-workspace/index.html:{key}") for key in keys
    ]


def _stub_card(**over: object) -> TaskCard:
    values: dict[str, object] = {
        "id": "T01",
        "feature_request_id": uuid.uuid4(),
        "title": "卡 T01",
        "difficulty": 2,
        "skill_tags": ["frontend_coding"],
        "depends_on": [],
        "artifact_floor_min_bytes": 512,
        "artifact_floor_min_lines": 12,
        "artifact_floor_format": "html",
        "acceptance_criteria": list(CARD_CRITERIA),
        "handoff_count": 0,
        "review_round": 0,
        "status": "REVIEWING",
        "snapshot_id": uuid.uuid4(),
        "executor_agent_id": EXECUTOR,
        "reviewer_agent_id": REVIEWER,
        "acceptor_agent_id": ACCEPTOR,
    }
    values.update(over)
    return TaskCard(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 一、结论域值只有一个来源（T089，fast）


def _assignment_shape(source: str, name: str) -> str:
    """模块源码里 `name = <表达式>` 右值节点的类型名（`Call` / `Tuple` / `absent`）。"""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return type(node.value).__name__ if node.value is not None else "none"
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return type(node.value).__name__
    return "absent"


def test_the_verdict_domain_is_read_from_the_enum_and_is_exactly_three():
    """Phase 17 P1：EN-08 的三值 + `data-model.md §2` 的引用规矩，两条一起量。"""
    assert rv.VERDICTS == tuple(values_of("review_verdict"))
    assert set(rv.VERDICTS) == {rv.APPROVE, rv.CHANGES_REQUESTED, rv.NEED_HUMAN}, rv.VERDICTS


def test_the_verdict_domain_is_not_a_literal_in_the_module_source():
    """`VERDICTS` 的右值 MUST 是一次调用（`values_of(...)`），不是抄下来的元组。

    注入对照（这条自己也会坏）：把右值换成字面量元组，扫描器要报出 `Tuple` —— 报不出来
    说明这个判据恒真，绿是因为它没在看任何东西。
    """
    source = Path(rv.__file__).read_text(encoding="utf-8")
    assert _assignment_shape(source, "VERDICTS") == "Call"
    assert (
        _assignment_shape(
            'VERDICTS: tuple[str, ...] = ("APPROVE", "CHANGES_REQUESTED", "NEED_HUMAN")',
            "VERDICTS",
        )
        == "Tuple"
    )
    assert _assignment_shape("def f():\n    return 1\n", "VERDICTS") == "absent"


# ---------------------------------------------------------------- 二、结论由 findings 推出（T089，fast）


def test_the_verdict_is_derived_and_the_caller_has_noway_to_state_it():
    """调用方**没有**可以写"我判它通过"的参数：结论只从 findings 的 `met` 算出来。

    这条不是风格问题。`record_verdict(..., verdict=...)` 这种签名一旦出现，
    "自称通过但有一行判不过"就能入库，而 FR-028 的"每轮一条结论"读起来仍然是满足的。
    参数名扫描配一份合成函数做注入对照，免得"没有 verdict 参数"绿是因为函数压根没参数。
    """
    names = set(inspect.signature(rv.record_verdict).parameters)
    assert not [name for name in names if "verdict" in name or "approve" in name], names

    def _synthetic(session, *, verdict, approved_by):  # noqa: ANN001, ARG001
        return verdict

    planted = set(inspect.signature(_synthetic).parameters)
    assert [name for name in planted if "verdict" in name or "approve" in name], "扫描器失效：连明显的违例都没抓到"


def test_all_met_is_approve_and_a_single_unmet_is_changes_requested():
    """`met` 全真 ⇒ `APPROVE`；有一条假 ⇒ `CHANGES_REQUESTED`。两个方向都断（反向对照在后半）。"""
    card = _stub_card()
    assert rv.check_findings(card, _findings({c: True for c in CARD_CRITERIA}))[0] == rv.APPROVE
    assert rv.check_findings(card, _findings({CARD_CRITERIA[0]: True, CARD_CRITERIA[1]: False}))[0] == (
        rv.CHANGES_REQUESTED
    )


def test_a_null_item_escalates_to_need_human_without_inventing_a_boolean():
    """一条 `met=None` ⇒ `NEED_HUMAN`；判定里 MUST NOT 出现被伪造的 true/false。

    这是 Phase 17 的核心口径：主观/不可判定的标准由模型交 `null`，平台把它读成"人来定"，
    而不是替模型猜一个布尔再写进 EN-08。
    """
    card = _stub_card()
    findings = [
        rv.ReviewFinding(CARD_CRITERIA[0], True, "qoder-workspace/index.html:1"),
        rv.ReviewFinding(CARD_CRITERIA[1], None, "「好看」不可验证 ⇒ 交人工"),
    ]
    verdict, reasons = rv.check_findings(card, findings)
    assert verdict == rv.NEED_HUMAN, verdict
    assert reasons == (), reasons
    payload = [finding.to_payload() for finding in findings]
    assert [row["met"] for row in payload] == [True, None], payload


@pytest.mark.parametrize(
    ("given", "expect_fragment"),
    [
        ([("首屏有标题", None)], "没有对应行"),
        ([(c, None) for c in CARD_CRITERIA] + [("配色更克制", None)], "不在卡上"),
        ([("首屏有标题", None), ("首屏有标题", None), ("样式表被引用", None)], "不止一行"),
    ],
    ids=["missing-one", "personal-preference", "duplicated-row"],
)
def test_need_human_does_not_skip_the_strict_per_item_comparison(given, expect_fragment):
    """`met=None` 只改变**结论的种类**，不改变逐条覆盖的严格性。

    三种坏法都带 `None`：少一条 / 多一条 / 重复一条 —— 每一条都必须先被判成"不合规"，
    而不是因为有不确定项就被放行。少了这组，`NEED_HUMAN` 会成为绕过 FR-029 的后门。
    """
    card = _stub_card()
    findings = [rv.ReviewFinding(criterion, met, "qoder-workspace/index.html:1") for criterion, met in given]
    verdict, reasons = rv.check_findings(card, findings)
    assert verdict is None, verdict
    assert expect_fragment in "；".join(reasons), reasons


# ---------------------------------------------------------------- 三、逐条比对（T090，fast）


@pytest.mark.parametrize(
    ("given", "expect_fragment"),
    [
        (["首屏有标题"], "没有对应行"),
        (["首屏有标题", "样式表被引用", "配色更克制"], "不在卡上"),
        (["首屏有标题", "首屏有标题"], "不止一行"),
    ],
    ids=["missing-one", "personal-preference", "duplicated-row"],
)
def test_the_three_ways_a_finding_set_fails_to_be_compliant(given, expect_fragment):
    """三种坏法各一条，且每条都给出**可读的拒因**（不是笼统一句"不合规"）。

    `配色更克制` 那条就是 FR-029 说的"个人偏好"：卡上没声明，来了就拒。
    """
    card = _stub_card()
    findings = [rv.ReviewFinding(name, True, "qoder-workspace/index.html:1") for name in given]
    verdict, reasons = rv.check_findings(card, findings)
    assert verdict is None
    assert expect_fragment in "；".join(reasons), reasons


def test_a_complete_set_of_findings_is_accepted_without_reason():
    """反向对照：判全了 MUST NOT 有拒因。少了这条，上面三条红的原因可能是"永远拒"。"""
    card = _stub_card()
    verdict, reasons = rv.check_findings(card, _findings({c: True for c in CARD_CRITERIA}))
    assert verdict == rv.APPROVE
    assert reasons == ()


def test_an_empty_criteria_card_refuses_the_review_rather_than_inventing_one():
    """卡上没标准 ⇒ 不给可见面，而不是让审查者自拟（原则 III 禁的那件事）。"""
    card = _stub_card(acceptance_criteria=[])
    with pytest.raises(rv.ReviewMisuse) as caught:
        rv.build_request(card, [("qoder-workspace/index.html", "<html/>")], reviewer_agent_id=REVIEWER)
    assert caught.value.code == "criteria_empty"


@pytest.mark.parametrize(
    ("given", "expect_fragment"),
    [
        ((MAP_ITEMS[0],), "没有对应判定"),
        (tuple(MAP_ITEMS) + ("顺手加一条",), "不在映射表里"),
        ((MAP_ITEMS[0], MAP_ITEMS[0], MAP_ITEMS[1]), "不止一行"),
    ],
    ids=["missing-one", "self-invented-item", "duplicated-row"],
)
def test_the_three_ways_an_acceptance_set_fails_to_cover_the_map(given, expect_fragment):
    """验收侧的三种坏法与审查侧同形（FR-030），但**判据各自要有**：
    两边用的是两个函数（`check_findings` / `check_items`），一边绿不能替另一边说话。

    `self-invented-item` 那条就是原则 III 的"验收口径 MUST NOT 由验收方自拟"。
    反向对照在第五节第一条（判全了不报错），这里不重复。
    """
    reasons = ac.check_items(
        tuple(MAP_ITEMS), [ac.ItemVerdict(item, True, "qoder-workspace/index.html:1") for item in given]
    )
    assert reasons, given
    assert expect_fragment in "；".join(reasons), reasons


def test_a_complete_acceptance_set_covers_the_map_without_reason():
    """上一条的反向对照：集合相等 ⇒ 零拒因。少了这条，上面三条可能只是"永远拒"。"""
    reasons = ac.check_items(
        tuple(MAP_ITEMS), [ac.ItemVerdict(item, True, "index.html:1") for item in MAP_ITEMS]
    )
    assert reasons == ()


# ---------------------------------------------------------------- 四、轮次上限只有一个解析者（T091，fast）


def _regex_authors(root: Path, phrase: str) -> set[str]:
    """root 下**用正则解析这句话**的文件名集合。只认 `re.compile("...")` 的字面量，散文提到不算。"""
    hits: set[str] = set()
    for py in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "compile" or not (
                isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
            ):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and phrase in arg.value:
                    hits.add(py.name)
    return hits


def test_exactly_one_module_in_the_source_tree_parses_the_limit():
    """T091 的"不在此复述数值"落到代码面上：全仓只有一处在解析宪法那句话。

    读数必须是**非空且恰一个文件** —— 空集说明扫描器没工作，两个文件说明有两份事实。
    """
    found = _regex_authors(SRC_ROOT, LIMIT_PHRASE)
    assert found == {"timeouts.py"}, f"{LIMIT_PHRASE} 的解析者应为 1 个，读到 {sorted(found)}"


def test_the_scanner_finds_a_second_parser_and_ignores_prose(tmp_path: Path):
    """上一条的两支对照：真违例必须被抓到（注入），散文提到必须不被抓（反向）。

    少了后一支，`found == {"timeouts.py"}` 可能只是"谁提到这句话都算违例"的另一种写法，
    那它对"加一句注释"都会红，判的就不是解析器数量了。
    """
    (tmp_path / "a.py").write_text(
        f'import re\nP = re.compile(r"{LIMIT_PHRASE}\\s*(\\d+)")\n', encoding="utf-8"
    )
    (tmp_path / "b.py").write_text(f'T = "{LIMIT_PHRASE} 3 是宪法写的"\n', encoding="utf-8")
    (tmp_path / "c.py").write_text("import re\nP = re.compile(r'别的句子\\\\s*(\\\\d+)')\n", encoding="utf-8")
    assert _regex_authors(tmp_path, LIMIT_PHRASE) == {"a.py"}


def test_the_two_readers_of_the_cap_agree_and_move_with_the_document(tmp_path: Path):
    """跨实现符合性：`accept.round_limit()` 与 `timeouts.review_round_limit()` 永远是同一个数，
    且这个数随宪法文本移动（写死成 3 的实现在这里就红了）。"""
    real = ac.round_limit()
    assert real == T.review_round_limit() == 3
    patched = tmp_path / "constitution.md"
    patched.write_text(
        T.constitution_default().read_text(encoding="utf-8").replace(f"{LIMIT_PHRASE} 3", f"{LIMIT_PHRASE} 5"),
        encoding="utf-8",
    )
    assert ac.round_limit(patched) == T.review_round_limit(patched) == 5


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("原则 III 里没有那一句\n", 0),
        (f"{LIMIT_PHRASE} 3\n另一处又写 {LIMIT_PHRASE} 7\n", 2),
    ],
    ids=["no-definition", "two-definitions"],
)
def test_a_constitution_that_does_not_define_the_cap_exactly_once_is_refused(
    tmp_path: Path, text: str, hits: int
):
    """0 处与 ≥2 处都抛。第一版这里取"命中就取第一处"，那是替宪法回答"哪一处算数"。

    两种坏法的后果不同但都不能静默：0 处会被读成"宪法没立过上限"（那 `>=` 判定就没有依据），
    2 处会被读成"任一处都行"（两处数值可以互相矛盾，如上面那对的 3 与 7）。
    """
    path = tmp_path / "constitution.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(T.ParamsRefError) as caught:
        T.review_round_limit(path)
    assert f"命中 {hits} 处" in str(caught.value), str(caught.value)


# ---------------------------------------------------------------- PG 侧的公共装配


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


def _seed(session: Session, *, status: str, review_round: int = 0) -> tuple[FeatureRequest, TaskCard]:
    """三支 Agent + 前三岗 + 一张卡（状态与轮次由调用方给）+ EN-07 映射行。

    岗的挂载走真生产者（`role_registry.seed_builtin_roles`），映射行走 `AcceptanceMap` 直写
    —— 本文件要判的是"验收口径从 EN-07 取"，所以那张表 MUST 是它自己种的。
    """
    for agent_id in (EXECUTOR, REVIEWER, ACCEPTOR):
        if session.get(AgentUnit, agent_id) is None:
            session.add(
                AgentUnit(
                    agent_id=agent_id,
                    form="SDK",
                    tier=2,
                    cost_band="MID",
                    max_parallel=2,
                    current_load=0,
                    historical_success_rate=Decimal("0.5"),
                    success_rate_sample=4,
                    workspace_dir=f"/tmp/{agent_id}-workspace",
                    enabled=True,
                )
            )
    role_registry.seed_builtin_roles(session)
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(request)
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T01"],
            ref=f"local:probe-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    card = TaskCard(
        id="T01",
        feature_request_id=request.id,
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=list(CARD_CRITERIA),
        handoff_count=0,
        review_round=review_round,
        status=status,
        snapshot_id=snapshot_id,
        executor_agent_id=EXECUTOR,
        reviewer_agent_id=REVIEWER,
        acceptor_agent_id=ACCEPTOR,
    )
    session.add(card)
    # **两张表之间没有 `relationship()`** ⇒ 工作单元不按表间外键排 INSERT 顺序，实测按 mapper 名排序：
    # `acceptance_map` 排在 `task_card` 前面，同一次 flush 里插就撞复合外键（`persist_registrations`
    # 的 EN-03/EN-05 是同一个坑）。这里显式把卡先落地，不留"顺序靠运气"的装配。
    session.flush()
    for item in MAP_ITEMS:
        session.add(
            AcceptanceMap(
                id=uuid.uuid4(),
                feature_request_id=request.id,
                requirement_item="R-01",
                acceptance_item=item,
                task_card_id=card.id,
                produced_by="workbuddy",
            )
        )
    session.flush()
    return request, card


@pytest.fixture
def reviewing(session) -> tuple[FeatureRequest, TaskCard]:
    return _seed(session, status="REVIEWING")


@pytest.fixture
def accepting(session) -> tuple[FeatureRequest, TaskCard]:
    return _seed(session, status="ACCEPTING")


def _verdict_rows(session: Session, card: TaskCard) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ReviewVerdict).where(ReviewVerdict.task_card_id == card.id)
        )
        or 0
    )


def _event_rows(session: Session, card: TaskCard, action: str) -> int:
    return len(
        [
            row
            for row in session.scalars(select(EventLog).where(EventLog.entity_id == card.id)).all()
            if isinstance(row.payload, dict) and row.payload.get("action") == action
        ]
    )


def _round_payload(session: Session, card_id: str) -> dict:
    """那张卡**最后一轮**验收落在 EN-14 上的载荷（写侧的形状只能从库里读，不能从返回值抄）。

    取 `id` 最大的一条：与 `run._acceptance_audit` 同一口径（插入序），不按 `round_no` 排 ——
    让被检对象给自己排座次，判据就会跟着它一起漂。
    """
    rows = [
        row
        for row in session.scalars(select(EventLog).where(EventLog.entity_id == card_id)).all()
        if isinstance(row.payload, dict) and row.payload.get("action") == ac.ACTION_ROUND
    ]
    assert rows, f"卡 {card_id} 的库里没有那一轮验收的载荷 ⇒ 这一发没有可核的对象"
    return max(rows, key=lambda row: row.id).payload


def _items(met_by: dict[str, bool]) -> list[ac.ItemVerdict]:
    return [
        ac.ItemVerdict(item, met_by[item], f"qoder-workspace/index.html:{item}", f"差在 {item}")
        for item in MAP_ITEMS
        if item in met_by
    ]


# ---------------------------------------------------------------- 五、审查结论入库（T089/T090，PG）


@needs_pg
def test_a_round_records_exactly_one_verdict_row_and_moves_the_card(session, reviewing):
    """T089 的"每轮产结论"：一轮 = EN-08 一行 + 卡推进一格 + EN-14 一条留痕。

    第三条断言在 owner 2026-10-14 裁决 2（P1-5）之后**反了过来**：这里 MUST 是 0。
    `review_round` 是验收轮次、写者唯一 = `accept()`；审查结论去动它就是拿审查消费验收额度。
    """
    request, card = reviewing
    recorded = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    assert recorded.verdict == rv.APPROVE
    assert _verdict_rows(session, card) == 1
    assert card.status == "ACCEPTING"
    assert card.review_round == 0, f"审查结论动了验收轮次：{card.review_round}"
    assert recorded.round_no == 1, "第几轮审查要从 EN-08 已有的行数数，不是从那一列数"
    assert _event_rows(session, card, "verdict_recorded") == 1


@needs_pg
def test_the_second_verdict_in_the_same_round_is_refused_by_the_state(session, reviewing):
    """同一轮递两次 ⇒ 第二次被 `REVIEWING` 这道门拦住，且**一行都不多**。

    拦它的是状态迁移本身而不是"本轮已收过"的计数器：计数器是独立于状态机的第二份事实，
    而"归零时机"没人管得着。这条断言把两种实现区分开 —— 加了计数器、没加状态门的写法在此红。
    """
    request, card = reviewing
    rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    with pytest.raises(rv.ReviewMisuse) as caught:
        rv.record_verdict(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reviewer_agent_id=REVIEWER,
            findings=_findings({CARD_CRITERIA[0]: True, CARD_CRITERIA[1]: False}),
            created_at=NOW,
        )
    assert caught.value.code == "state_not_reviewing"
    assert _verdict_rows(session, card) == 1, "被拒的那一次留下了行"
    assert card.review_round == 0, "审查那两条路径都不许碰验收轮次（owner 2026-10-14 裁决 2）"


@needs_pg
def test_need_human_is_recorded_without_faking_booleans_and_leaves_the_card_open(session, reviewing):
    """`met=None` 入库成 `NEED_HUMAN`：EN-08 行保留 `null`，卡**留在 `REVIEWING`** 等人工定论。

    为什么卡不能像 `APPROVE` / `CHANGES_REQUESTED` 那样移出去：`NEED_HUMAN` 不是"这一轮判完了"，
    而是"平台判不了、请人判"。把人还没判的卡推进 `ACCEPTING` 或 `CHANGES_REQUESTED`，都会让
    下一次人工结论撞上状态门；而留在 `REVIEWING` 时，人工那条决定性结论是同一张卡的第二条 EN-08
    （`round_no` 递增）。这条同时钉住"`null` 没被折成 false"——折了的话库里读到的是
    `CHANGES_REQUESTED`，`item_findings` 里也不会有 `null`。
    """
    request, card = reviewing
    findings = [
        rv.ReviewFinding(CARD_CRITERIA[0], True, "qoder-workspace/index.html:1"),
        rv.ReviewFinding(CARD_CRITERIA[1], None, "「好看」不可验证 ⇒ 交人工"),
    ]
    recorded = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=findings,
        created_at=NOW,
    )
    session.flush()
    assert recorded.verdict == rv.NEED_HUMAN
    assert card.status == rv.REVIEWING, f"NEED_HUMAN 把卡推出了 REVIEWING：{card.status}"
    rows = session.scalars(
        select(ReviewVerdict).where(ReviewVerdict.task_card_id == card.id)
    ).all()
    assert len(rows) == 1
    assert str(rows[0].verdict) == rv.NEED_HUMAN
    assert [item["met"] for item in rows[0].item_findings] == [True, None], rows[0].item_findings
    assert _event_rows(session, card, "verdict_recorded") == 1
    assert card.review_round == 0, "NEED_HUMAN 不许碰验收轮次（裁决 2）"

    # 人工在同一张卡上给出决定性结论 ⇒ 第二条 EN-08，round_no = 2，卡才移动。
    second = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    assert (second.verdict, second.round_no) == (rv.APPROVE, 2)
    assert card.status == "ACCEPTING"
    assert _verdict_rows(session, card) == 2


@needs_pg
def test_an_extra_criterion_is_refused_before_it_reaches_the_table(session, reviewing):
    """FR-029 的应用层那道：多一条卡上没写的标准 ⇒ 拒，且 EN-08 零行、留痕零条。"""
    request, card = reviewing
    findings = _findings({c: True for c in CARD_CRITERIA}) + [
        rv.ReviewFinding("配色更克制", True, "qoder-workspace/index.html:1")
    ]
    with pytest.raises(rv.ReviewMisuse) as caught:
        rv.record_verdict(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            reviewer_agent_id=REVIEWER,
            findings=findings,
            created_at=NOW,
        )
    assert caught.value.code == "findings_not_compliant"
    assert "配色更克制" in str(caught.value)
    assert _verdict_rows(session, card) == 0
    assert _event_rows(session, card, "verdict_recorded") == 0


@needs_pg
def test_the_database_check_is_the_second_net_under_the_app_layer(session, reviewing):
    """绕过应用层直写 `extra_criteria_used = true` ⇒ PG 的 `no_extra_criteria` 兜住。

    **这条证到什么、证不到什么**要写清楚：证到"库面那道约束真的在场"（生产路径永远不会走到它，
    所以只能这样直写）；证不到"应用层没坏"—— 那是上一条判据的活。两支都在，才叫双道。
    正向对照（同一张卡写 `false` 能落）排在后面：少了它，"插入失败"的原因可能是 FK 或 NOT NULL。
    """
    request, card = reviewing
    with pytest.raises(IntegrityError) as caught, session.begin_nested():
        session.add(
            ReviewVerdict(
                id=uuid.uuid4(),
                feature_request_id=request.id,
                task_card_id=card.id,
                reviewer_agent_id=REVIEWER,
                verdict=rv.CHANGES_REQUESTED,
                item_findings=[{"criterion": "配色更克制", "met": True, "evidence": "个人偏好"}],
                extra_criteria_used=True,
            )
        )
        session.flush()
    assert "no_extra_criteria" in str(caught.value), str(caught.value)

    with session.begin_nested():
        session.add(
            ReviewVerdict(
                id=uuid.uuid4(),
                feature_request_id=request.id,
                task_card_id=card.id,
                reviewer_agent_id=REVIEWER,
                verdict=rv.CHANGES_REQUESTED,
                item_findings=[{"criterion": c, "met": False, "evidence": "缺 <link>"} for c in CARD_CRITERIA],
                extra_criteria_used=False,
            )
        )
        session.flush()
        # 断言在 savepoint **内**：出了那一格，读数就把"这次插入成功过"忘掉，判据退化成恒真。
        assert _verdict_rows(session, card) == 1


# ---------------------------------------------------------------- 六、验收口径来自 EN-07（T091，PG）


@needs_pg
def test_the_acceptance_items_come_from_en_07_not_from_the_card(session, accepting):
    """判据用两组**不同名**的条目：卡上的 `acceptance_criteria` 与映射表里的条目互不通用。

    按卡上那组去验收必须被拒（否则"对照映射表逐条判定"是空话 —— 两处同名时谁都看不出读的是哪张表）。
    """
    request, card = accepting
    assert ac.expected_items(session, request.id, card.id) == tuple(sorted(MAP_ITEMS))
    with pytest.raises(ac.AcceptMisuse) as caught:
        ac.accept(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            verdicts=[ac.ItemVerdict(c, True, "index.html:1") for c in CARD_CRITERIA],
            created_at=NOW,
            actor_agent_id=ACCEPTOR,
        )
    assert caught.value.code == "items_not_compliant"
    assert card.status == ac.ACCEPTING and card.review_round == 0, "被拒的那一次动了卡"


@needs_pg
def test_a_card_without_a_mapping_row_has_no_acceptance_criteria(session, accepting):
    """映射表空 ⇒ 抛 `map_empty`，而不是让验收方自带一套口径。"""
    request, card = accepting
    session.query(AcceptanceMap).filter(AcceptanceMap.task_card_id == card.id).delete()
    session.flush()
    with pytest.raises(ac.AcceptMisuse) as caught:
        ac.expected_items(session, request.id, card.id)
    assert caught.value.code == "map_empty"


@needs_pg
def test_only_the_acceptor_seat_can_put_a_card_into_delivery(session, accepting):
    """执行者自报"我做完了、判我通过"必须被拒 —— 三岗互斥在验收那一格才算闭合。

    正向对照（验收岗自己递的同样内容照样入库）在同一支的后半：少了它，红的原因
    可能是"谁都拒"，那这道门就成了假门禁。
    """
    request, card = accepting
    verdicts = _items({item: True for item in MAP_ITEMS})
    with pytest.raises(ac.AcceptMisuse) as caught:
        ac.accept(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            verdicts=verdicts,
            created_at=NOW,
            actor_agent_id=EXECUTOR,
        )
    assert caught.value.code == "acceptor_mismatch"
    assert card.status == ac.ACCEPTING

    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=verdicts,
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert outcome.status_after == ac.DELIVERED, outcome


# ---------------------------------------------------------------- 七、返工与轮次（T092，PG）


@needs_pg
def test_an_unmet_item_goes_back_for_rework_without_counting_as_a_handoff(session, accepting):
    """`ACCEPTING ──未过──▶ CHANGES_REQUESTED ──▶ REWORK`，返工 NOT 计成换人。

    三条断言各管一个坏法：状态、`handoff_count`、三个岗的指派列。
    `handoff.py:53` 那句分工如果被人合并（返工也 +1），上限 1 的换人额度会被返工吃光。
    """
    request, card = accepting
    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert outcome.status_after == ac.CHANGES_REQUESTED
    assert outcome.round_no == 1 and card.review_round == 1
    assert ac.mark_rework(
        session, feature_request_id=request.id, task_id=card.id, created_at=NOW, actor_agent_id=ACCEPTOR
    ) == ac.REWORK
    assert (card.handoff_count, card.executor_agent_id) == (0, EXECUTOR), "返工被动了换人计数或岗的指派"
    assert _event_rows(session, card, "rework_marked") == 1


@needs_pg
def test_rework_is_refused_when_the_card_is_not_awaiting_it(session, accepting):
    """反向：`ACCEPTING` 上直接喊返工 ⇒ 拒。这条把上一条的"顺序"钉住 ——
    `CHANGES_REQUESTED` 是唯一的入口，跳过它就把"谁判的不通过"那一格抹掉了。
    """
    request, card = accepting
    with pytest.raises(ac.AcceptMisuse) as caught:
        ac.mark_rework(
            session, feature_request_id=request.id, task_id=card.id, created_at=NOW, actor_agent_id=ACCEPTOR
        )
    assert caught.value.code == "state_not_changes_requested"
    assert card.status == ac.ACCEPTING


@needs_pg
def test_a_rework_counts_itself_and_never_borrows_the_acceptance_round(session, reviewing):
    """待裁 ⑬（owner 2026-10-14）：返工计 `rework_count`，验收轮次计 `review_round`，两列不互相顶替。

    **为什么这条是行为断言而不是"列存在"断言**：拆列这件事光看 schema 看不出对错 ——
    真正的口径是"返工两次之后再来一轮验收，`review_round` 必须恰好 +1，不是 +3"。
    宪法那条"跑满上限 MUST 进 `DELIVERED_WITH_ISSUES`"判的是验收轮次；一列两用时，
    返工次数会替一张从未验收过的卡把上限吃光（这条在拆列前是 `test_the_review_and_the_acceptance_share_one_round_column`
    钉着的"两列不许出现"，裁定之后它反过来了）。
    """
    request, card = reviewing
    for expected in (1, 2):
        card.status = ac.CHANGES_REQUESTED  # 夹具脚手架：把卡摆回返工的唯一入口态
        ac.mark_rework(
            session, feature_request_id=request.id, task_id=card.id, created_at=NOW, actor_agent_id=ACCEPTOR
        )
        assert card.rework_count == expected, (expected, card.rework_count)
        assert card.review_round == 0, f"返工串到了验收轮次那一列：{card.review_round}"
        assert card.handoff_count == 0, "返工被计成了换人（FR-027 的 ≤1 与它无关）"

    card.status = ac.ACCEPTING
    ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({item: True for item in MAP_ITEMS}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert (card.review_round, card.rework_count, card.status) == (1, 2, ac.DELIVERED), (
        card.review_round,
        card.rework_count,
        card.status,
    )


def test_the_two_round_carriers_are_named_and_no_third_appears():
    """EN-02 上的轮次载体恰是 `review_round` + `rework_count` 两列，第三份一出现就红。

    谓词按**语义形态**取（名字里含 `round` 或 `rework`），不是把两个名字列出来再比对自己：
    谁再加一列 `acceptance_round` / `rework_rounds`，这条都会红 —— 而"点名两列比两列"那种写法
    对第三列完全无感（上一条测试从"只许一列"翻成"只许这两列"时，改的就是这个谓词，不是常量）。
    """
    carriers = sorted(
        column.name for column in TaskCard.__table__.columns if "round" in column.name or "rework" in column.name
    )
    assert carriers == ["review_round", "rework_count"], f"轮次载体列变成 {carriers} ⇒ 先改文档与口径，再来改这条"


@needs_pg
def test_rework_count_defaults_to_zero_without_the_caller_declaring_it(session, reviewing):
    """`server_default 0` 表达的是"这张卡还没返过工"，不是"没人记得传值"。

    这条刻意不看 `TaskCard(**values)` 那种没落库的对象（那个读出来是 `None`，属未插行的正常形态），
    看的是**库里那一行**：夹具从头到尾没传 `rework_count`，读回来必须是 0。
    把它和上一条合在一起会掩盖真正的差别 —— 上一条测的是"谁在写"，这一条测的是"没写时是什么"。
    """
    request, card = reviewing
    assert card.rework_count == 0, card.rework_count


# ---------------------------------------------------------------- 八、带未达标项交付（T093，PG）


@needs_pg
def test_the_last_round_delivers_with_each_unmet_item_named(session, accepting):
    """跑满上限仍未过 ⇒ `DELIVERED_WITH_ISSUES`，且未达标项**逐条列出**（FR-035）。

    写成关系式而不是条数常量：`len(open_issues) == outcome.unmet` 且集合与未过的那几条相等。
    """
    request, card = accepting
    card.review_round = ac.round_limit() - 1
    session.flush()
    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert outcome.status_after == ac.DELIVERED_WITH_ISSUES
    assert outcome.round_no == ac.round_limit()
    assert len(outcome.open_issues) == outcome.unmet == 1
    assert {issue["item"] for issue in outcome.open_issues} == {MAP_ITEMS[1]}
    assert all(set(issue) == {"item", "gap", "suggestion"} for issue in outcome.open_issues), outcome.open_issues


@needs_pg
def test_a_clean_round_delivers_with_no_issue_list(session, accepting):
    """全过 ⇒ `DELIVERED` 且 `open_issues` 为空。**与上一条成对**：两条合起来才说清"什么时候必须非空"。"""
    request, card = accepting
    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({item: True for item in MAP_ITEMS}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert (outcome.status_after, outcome.open_issues, outcome.unmet) == (ac.DELIVERED, (), 0)
    assert _round_payload(session, card.id)[ac.OPEN_ISSUES_ITEMS] == [], "全过那一轮的清单格必须是**空列表**而不是缺键"


@needs_pg
def test_the_round_row_carries_the_list_and_not_only_the_count(session, accepting):
    """EN-14 那一行同时带**条数**与**清单原文**，两格必须说同一个数（owner 2026-10-17 裁决 3 第 ① 项）。

    为什么单独钉：装配点、T125 的"四要素可查"与 T131 的"未达标项清单"要问的是"哪一条、差在哪"，
    只有条数的那一份答不了。而这一格被摘掉之后读侧拿到的是**空清单** —— 那正是 FR-035 最贵的形状
    （把"有问题"交成"没问题"），所以它必须有一条红得起来的判据，不能只靠 `run.open_issues_of` 的拒绝兜着。

    四格：
    ① 清单原文与 `outcome.open_issues` 逐条相等 ⇒ 载荷里那份就是验收岗交回来的那份，没有"再编一次"；
    ② 条数格 == 清单长度 ⇒ 同一行里的两个口径不许漂（漂了就是读哪一格有两种答案）；
    ③ 每一条的三个键与 `to_issue()` 同名 ⇒ 下游按 `item` / `gap` / `suggestion` 取，不必猜形状；
    ④ 全过那一轮是**空列表**而不是缺键（上一发钉的那格）⇒ 读侧分得开"没问题"与"载体没了"。
    """
    request, card = accepting
    card.review_round = ac.round_limit() - 1
    session.flush()
    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    payload = _round_payload(session, card.id)
    assert payload[ac.OPEN_ISSUES_ITEMS] == list(outcome.open_issues), payload
    assert payload["open_issues"] == len(payload[ac.OPEN_ISSUES_ITEMS]) == outcome.unmet, payload
    assert all(set(issue) == {"item", "gap", "suggestion"} for issue in payload[ac.OPEN_ISSUES_ITEMS]), payload
    assert payload["card_status_after"] == ac.DELIVERED_WITH_ISSUES, payload


@needs_pg
def test_the_cap_choosing_the_exit_is_the_parsed_one_not_the_number_three(session, accepting, monkeypatch):
    """把宪法读数换成 5 ⇒ 第 3 轮不许收敛；换成 1 ⇒ 第 1 轮就收敛。

    这一条是"上限取自宪法硬值"唯一的实量法：上限写死成 3 的实现，在 5 的那一支会红
    （它第 3 轮就收敛了），在 1 的那一支也会红（它第 1 轮不收敛）。两支反向各红一次。
    """
    request, card = accepting
    monkeypatch.setattr(ac, "round_limit", lambda path=None: 5)
    card.review_round = 2
    outcome = ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert (outcome.limit, outcome.status_after) == (5, ac.CHANGES_REQUESTED), outcome

    fresh = _seed(session, status="ACCEPTING")
    monkeypatch.setattr(ac, "round_limit", lambda path=None: 1)
    early = ac.accept(
        session,
        feature_request_id=fresh[0].id,
        task_id=fresh[1].id,
        verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert (early.limit, early.status_after) == (1, ac.DELIVERED_WITH_ISSUES), early


@needs_pg
def test_a_delivered_card_cannot_be_accepted_a_second_time(session, accepting):
    """`DELIVERED` 之后再验收 ⇒ `state_not_accepting`。交付格是单向的：
    不拦第二次，"带未达标项交付"就能被反复改写成"全部达标"，而 FR-035 的显式清单就没了。
    """
    request, card = accepting
    ac.accept(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        verdicts=_items({item: True for item in MAP_ITEMS}),
        created_at=NOW,
        actor_agent_id=ACCEPTOR,
    )
    assert card.status == ac.DELIVERED
    with pytest.raises(ac.AcceptMisuse) as caught:
        ac.accept(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            verdicts=_items({MAP_ITEMS[0]: True, MAP_ITEMS[1]: False}),
            created_at=NOW,
            actor_agent_id=ACCEPTOR,
        )
    assert caught.value.code == "state_not_accepting"
    assert (card.status, card.review_round) == (ac.DELIVERED, 1), "被拒的那一次把卡拖回了轮次里"


# ------------------------------------------------ 九、审查不占验收轮次（P1-5｜owner 2026-10-14 裁决 2）


def _written_attributes(source: str) -> set[str]:
    """这段源码里**作为赋值目标出现**的属性名集合（`x.review_round = …` 这种）。

    用 AST 而不是搜字符串：搜 `".review_round"` 会被注释与 docstring 里的词骗到（假红），
    也会被 `setattr(card, "review_" + "round", …)` 那种写法放过（假绿）。AST 这一版只认赋值位形。
    """
    written: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                written.add(target.attr)
    return written


def test_the_review_module_writes_neither_counter():
    """裁决 2 的静态那道：`review.py` 里 MUST NOT 出现对两个计数列的赋值。

    三条断言各管一种"判据自己失效"的坏法：
    ① 真源码零命中（现在的样子）；
    ② **注入对照** —— 把那行赋值塞回一段等价源码里，扫描器必须报出 `review_round`；
       报不出来就说明这条恒真，绿是因为它什么都没看（这一类我们在本轮踩过两次）；
    ③ 只读不算写：同一份源码里 `x = card.review_round` 这种读法 MUST NOT 命中，
       否则将来任何"读一下验收轮次"的正当改动都会被它拦。
    """
    source = Path(rv.__file__).read_text(encoding="utf-8")
    assert _written_attributes(source) & {"review_round", "rework_count"} == set(), (
        f"审查模块动了计数列：{sorted(_written_attributes(source))}"
    )
    assert _written_attributes("card.review_round = 3\n") == {"review_round"}
    assert _written_attributes("card.rework_count += 1\n") == {"rework_count"}
    assert _written_attributes("round_no = int(card.review_round) + 1\n") == set()


@needs_pg
def test_a_verdict_leaves_both_counters_and_reports_its_own_round(session, reviewing):
    """裁决 2 的行为那道：一次结论只动 `status` + EN-08 行 + EN-14 留痕，两列一个都不动。

    种子里把 `review_round` 摆成 **2** 而不是 0 —— 这是关键：旧写法 `card.review_round = 计数 + 1`
    在 0 起步的种子上会写成 1（与"没动"读成两样），而在 2 起步的种子上它会把 2 改写成 1，
    这条才分得开"没碰"和"碰坏了"。`rework_count` 同样先写成 3。
    """
    request, card = reviewing
    card.review_round = 2
    card.rework_count = 3
    session.flush()
    recorded = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    assert (card.review_round, card.rework_count) == (2, 3), "审查结论改了计数列（裁决 2 / P1-5 回来了）"
    assert recorded.round_no == 1, "第几轮审查要从 EN-08 数，第一轮就是 1"


@needs_pg
def test_the_review_round_number_is_counted_from_the_verdict_rows(session, reviewing):
    """同一张卡的第二次审查 ⇒ `round_no` 是 2，而 `review_round` 仍是种子里的 5（两件事不共享载体）。

    种子取 **5** 是有讲究的：旧写法是"读那一列 + 1 再写回"，在 0 或 2 起步的种子上它写出的数
    恰好可能与"没动"看起来一样（2 → 3 与 3 → 4 那种错位）；5 起步时它必然写成 6、7，
    而 `(1, 2)` 与"仍是 5"两处同时红 —— 变异不红的判据默认是判据的缺陷，所以这里故意把种子摆到
    两种口径都糊不过去的位置。
    状态门先把卡挪出 `REVIEWING`（一条结论一移），所以这里显式把卡放回 `REVIEWING` 再收第二条 ——
    放的这个动作是测试装配，不是产品路径，因此在断言之后才核对"放回去没有动计数列"。
    """
    request, card = reviewing
    card.review_round = 5
    session.flush()
    first = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    card.status = "REVIEWING"
    session.flush()
    second = rv.record_verdict(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        reviewer_agent_id=REVIEWER,
        findings=_findings({c: True for c in CARD_CRITERIA}),
        created_at=NOW,
    )
    session.flush()
    assert (first.round_no, second.round_no) == (1, 2), (first.round_no, second.round_no)
    assert _verdict_rows(session, card) == 2
    assert card.review_round == 5, "从 EN-08 数轮次，不许把数出来的值写回验收轮次那一列"
    rows = session.scalars(select(EventLog).where(EventLog.entity_id == card.id)).all()
    rounds = sorted(
        row.payload["round_no"] for row in rows if isinstance(row.payload, dict) and "round_no" in row.payload
    )
    assert rounds == [1, 2], f"审查轮次要能在 EN-14 里读回来（不占列就靠它）：{rounds}"

