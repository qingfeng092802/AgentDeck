"""T035 的判据：`PATCH /tasks/{task_id}` —— 下发前调整（FR-006），并闭合 T-8。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip，口径同 `test_orchestrate_graph.py`）。
它要回答的不是"端点返回了什么"，而是"**库里那两张表现在长什么样**"：EN-02 的列、EN-07 的行、
以及"被拒的那次 PATCH 到底写没写进去"。所以断言一律读库（`session.get` / 裸 SQL），
响应体只在"回显必须与库一致"时才用。

四条刻意选择，写下来免得被照抄成偶然：

1. **白名单的负例来自契约文档，不来自本文件的手抄清单**。`§1.2` 里那两个 ```text 围栏是机读的，
   用例逐字比对代码侧 `ADJUSTABLE_FIELDS` 并**逐项真的发一次请求**去验"不可改"。
   文档与代码谁漂了都红，而不是"文档写着、测试也写着、两边一起错"。
2. **原子性判据要求"被拒时一行未动"**，且 EN-02 与 EN-07 分别核 ——只核前者的话，
   后者半写（删了旧行、没插新行）看起来仍是"这次请求被拒了"。
3. **"没传"与"传了 null"必须分得开**（`test_null_clears_...` 一条里同时断两件事）：
   FR-006 的调整语义里"改成无预算"和"没动预算"是两个不同的决定。
4. **端点提交并收尾自己的会话**（`test_the_patch_commits_and_closes_...`）：2026-10-27 之前
   这条判据断的是相反的形状（"不 commit、不 close"，理由"提交归装配方 T123"），而装配方
   `serve.build_app` 从来没有请求级提交钩子 ⇒ 生产面上 PATCH 回 200、库里没有那一行、连接只借不还。
   现在与 `api/approvals.py` / `api/requests.py` 同形：端点显式 `commit()`，`_store` 的 `finally` 收尾。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from acp.api.tasks import (
    ADJUSTABLE_FIELDS,
    NON_COLUMN_FIELDS,
    TaskPatch,
    router as tasks_router,
)
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.graph import advance_request
from acp.orchestrate.nodes.plan import CompleterOutcome
from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft
from acp.persistence.models_approval import AcceptanceMap
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.persistence.states import TaskCardState
from acp.protocol import ArtifactFloor

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（调整与原子性只能在库里读）",
)

NOW = dt.datetime(2026, 10, 12, 18, 30, 0, tzinfo=dt.timezone.utc)
FLOOR = ArtifactFloor(min_bytes=512, min_lines=12, format="html")
DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": ["联系方式用邮箱还是表单？"]}
PRODUCER = "stub-orchestrator"

# 卡号 / 依赖 / 判据：三张卡各自判据不重名，好让 EN-07 的行数能按卡单独数。
DEFAULT_CARDS: tuple[tuple[str, list[str], tuple[str, ...]], ...] = (
    ("T01", [], ("首屏有标题", "联系方式可见")),
    ("T02", ["T01"], ("列表可滚动",)),
    ("T03", ["T01", "T02"], ("页脚有备案号",)),
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "001-multi-agent-collab-platform"
    / "contracts"
    / "agent-protocol.md"
)


def _contract_whitelist() -> tuple[frozenset[str], frozenset[str]]:
    """读 `contracts/agent-protocol.md §1.2` 的两个 ```text 围栏 ⇒（可改, 不可改）。

    按竖线切；文档里写的是字母序，本函数**不**排序（排序是断言那边的事，这里只负责如实读出）。
    """
    body = CONTRACT_PATH.read_text(encoding="utf-8")
    start = body.index("### 1.2 ")
    section = body[start : body.index("\n## 2. ", start)]
    blocks = re.findall(r"```text\n(.*?)\n```", section, re.S)
    assert len(blocks) == 2, f"§1.2 MUST 恰好有两个白名单围栏，实际读出 {len(blocks)} 个"
    return tuple(  # type: ignore[return-value]
        frozenset(part.strip() for part in block.split("|")) for block in blocks
    )


DOC_ADJUSTABLE, DOC_NOT_ADJUSTABLE = _contract_whitelist()


# ---------------------------------------------------------------- 造事实态


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()  # 口径同 `test_schema_invariants.py`：不 dispose 会按 GC 时序抖 ResourceWarning


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


def seed(
    session,
    *,
    cards: tuple[tuple[str, list[str], tuple[str, ...]], ...] = DEFAULT_CARDS,
    statuses: dict[str, str] | None = None,
) -> uuid.UUID:
    """1 行 EN-01 + 1 行 EN-12 + N 行 EN-02 + 每卡每判据一行 EN-07。

    插入顺序是 snapshot → cards → maps，**不能交给 UoW 猜**：这些 mapper 之间没有
    `relationship()`，SQLAlchemy 不会按外键排 INSERT（`test_orchestrate_graph.py` 里两次被抓到，
    第 15 号事实已记进 memory）。
    """
    request_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id,
            project_key="demo",
            raw_input="做个介绍公司产品的静态网站",
            summary_digest=DIGEST,
            clarification_waived=True,
            status="SPLIT",
            created_at=NOW,
        )
    )
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=[c[0] for c in cards],
            ref=f"local:registry/snapshot-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    for task_id, depends_on, criteria in cards:
        session.add(
            TaskCard(
                id=task_id,
                feature_request_id=request_id,
                title=f"卡 {task_id}",
                difficulty=2,
                skill_tags=["html", "css"],
                depends_on=list(depends_on),
                executor_agent_id=None,
                executor_role_id=None,
                reviewer_agent_id=None,
                acceptor_agent_id=None,
                artifact_floor_min_bytes=FLOOR.min_bytes,
                artifact_floor_min_lines=FLOOR.min_lines,
                artifact_floor_format=FLOOR.format,
                token_budget=None,
                deadline_minutes=30,
                acceptance_criteria=list(criteria),
                review_standard_locked=True,
                handoff_count=0,
                review_round=0,
                status=(statuses or {}).get(task_id, "DRAFT"),
                snapshot_id=snapshot_id,
            )
        )
    session.flush()
    for task_id, _, criteria in cards:
        for item in criteria:
            session.add(
                AcceptanceMap(
                    id=uuid.uuid4(),
                    feature_request_id=request_id,
                    requirement_item=f"需求条目：{task_id}",
                    acceptance_item=item,
                    task_card_id=task_id,
                    produced_by=PRODUCER,
                )
            )
    session.flush()
    return request_id


def app_with(session, *, with_store: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_router)
    if with_store:
        app.state.session_factory = lambda: session
    return TestClient(app)


def patch_task(client, request_id, task_id: str, body: dict):
    return client.patch(f"/tasks/{task_id}", json={"feature_request_id": str(request_id), **body})


def card_row(session, request_id, task_id: str) -> TaskCard:
    row = session.get(TaskCard, (request_id, task_id))
    assert row is not None, f"EN-02 里 {request_id}/{task_id} 这行不见了 —— 断言的前提塌了"
    return row


def map_items(session, request_id, task_id: str) -> list[str]:
    """库里这张卡当前的 EN-07 判据条目（排序后，好让"替换了什么"是个可比的读数）。"""
    return sorted(
        row.acceptance_item
        for row in session.query(AcceptanceMap)
        .filter(
            AcceptanceMap.feature_request_id == request_id,
            AcceptanceMap.task_card_id == task_id,
        )
        .all()
    )


def links_for(task_id: str, items: list[str], requirement: str = "需求条目：改后") -> list[dict]:
    return [
        {
            "task_id": task_id,
            "requirement_item": requirement,
            "acceptance_item": item,
            "produced_by": PRODUCER,
        }
        for item in items
    ]


def independent_counts() -> tuple[int, int, int]:
    """换一个连接数三张表的行数 —— "端点没越权提交"的唯一外部可见证据。"""
    eng = create_engine(db_url(), future=True)
    try:
        with eng.connect() as conn:
            return tuple(  # type: ignore[return-value]
                conn.execute(text(f"select count(*) from {t}")).scalar()
                for t in ("task_card", "acceptance_map", "feature_request")
            )
    finally:
        eng.dispose()


# ---------------------------------------------------------------- 决策 1：白名单


def test_adjustable_whitelist_is_the_contract_s_first_fence():
    """代码侧集合（从 `TaskPatch` 字段表推导）与契约 §1.2 的第一个围栏**逐字相等**。

    两个方向都断：文档多写了代码没有 ⇒ 红；代码多写了文档没有 ⇒ 也红。
    """
    assert ADJUSTABLE_FIELDS == DOC_ADJUSTABLE, (
        f"只在文档 = {sorted(DOC_ADJUSTABLE - ADJUSTABLE_FIELDS)}，"
        f"只在代码 = {sorted(ADJUSTABLE_FIELDS - DOC_ADJUSTABLE)}"
    )
    assert ADJUSTABLE_FIELDS | NON_COLUMN_FIELDS == frozenset(TaskPatch.model_fields)


def test_not_adjustable_fence_names_real_columns_and_never_overlaps():
    columns = frozenset(TaskCard.__table__.columns.keys())
    assert DOC_NOT_ADJUSTABLE & ADJUSTABLE_FIELDS == frozenset(), "两份清单有交集 = 白名单自相矛盾"
    assert DOC_NOT_ADJUSTABLE - columns == frozenset(), (
        f"契约点名了 EN-02 上不存在的列：{sorted(DOC_NOT_ADJUSTABLE - columns)}"
    )


def test_the_two_fences_together_name_every_column():
    """两张围栏加起来 MUST 覆盖 EN-02 的**每一列**，唯一例外是寻址用的 `feature_request_id`。

    这一条是迁移 `0007` 加 `rework_count` 时补的，之前只有反方向（"点名了不存在的列"会红），
    于是**新加一列而两张围栏都不提它，判据全绿** —— 那一列就同时是"可改"（没人拦）和"不可改"（没人申报），
    读文档的人与读代码的人会各拿一半事实。现在加列必须同时改围栏，改不动就得先裁口径。
    例外那一项写在明处而不是从集合里悄悄减：`§1.2` 第 1 条讲的是"它在请求体里是定位不是改写"。
    """
    columns = frozenset(TaskCard.__table__.columns.keys())
    unlisted = columns - DOC_ADJUSTABLE - DOC_NOT_ADJUSTABLE - {"feature_request_id"}
    assert unlisted == frozenset(), f"这些列既不在可改也不在不可改围栏里：{sorted(unlisted)}"


@pytest.mark.parametrize("name", sorted(DOC_NOT_ADJUSTABLE))
def test_every_not_adjustable_column_is_rejected_by_name(session, name):
    """决策 1 的"不可改"逐项发一次请求：422 + 错误体点名这个键，且库里一行未动。

    逐项而不是整批断一次：白名单少拦一列（比如 `status` 被放开）就会在这一列自己的用例上红，
    而不是笼统地"某处不对"。
    """
    request_id = seed(session)
    before_map = map_items(session, request_id, "T02")

    response = patch_task(app_with(session), request_id, "T02", {name: "改一下"})

    assert response.status_code == 422, response.text
    assert "extra_forbidden" in response.text, f"422 不是来自 `extra=forbid`，而是别的原因：{response.text}"
    assert f'"{name}"' in response.text, f"错误体没点名 {name}：{response.text}"
    session.expire_all()
    row = card_row(session, request_id, "T02")
    assert row.title == "卡 T02" and row.depends_on == ["T01"], f"{name} 被收下并写进了 EN-02"
    assert map_items(session, request_id, "T02") == before_map


def test_feature_request_id_addresses_but_never_rewrites(session):
    """契约 §1.2 明写的那个例外：`feature_request_id` 在请求体里是**寻址**，不是可改列。

    三条一起才成立：① 它不触发 `extra_forbidden`（它是声明过的寻址字段，不是被拒的键）；
    ② 卡号在**别的**需求单下不存在 ⇒ 404，而不是"把这行的这列改成那个号"；
    ③ 同号卡存在于另一个需求单时也不会被这次 PATCH 命中（复合主键的作用域是真的）。
    """
    mine = seed(session)  # A 单：T01 / T02 / T03
    other = seed(session, cards=(("T01", [], ("首屏有标题",)),))  # B 单：只有 T01
    client = app_with(session)

    response = patch_task(client, other, "T02", {"title": "改标题"})

    assert response.status_code == 404, response.text
    assert "task_not_found" in response.text
    assert "extra_forbidden" not in response.text, "寻址列被当成不可改键拒掉 = 这条 PATCH 根本发不出来"
    session.expire_all()
    assert card_row(session, mine, "T02").title == "卡 T02", "A 单的同号卡被 B 单的请求改写了"
    assert card_row(session, other, "T01").feature_request_id == other


# ---------------------------------------------------------------- 决策 1 的收紧：DRAFT 门禁


@pytest.mark.parametrize(
    "state",
    [
        TaskCardState.PENDING_APPROVAL,
        TaskCardState.READY,
        TaskCardState.RUNNING,
        TaskCardState.REVIEWING,
        TaskCardState.DELIVERED,
    ],
)
def test_any_state_after_draft_refuses_every_adjustable_field(session, state):
    """`READY` 及之后**任何**字段都拒（owner 决策 1 的收紧）。

    态取五个而不是一个：门禁写成"只放 DRAFT"才顶得住"哪天有人把 READY 也当成可调整"，
    而多态参数化是为了暴露"实现其实是 `!= PENDING_APPROVAL`"这类歪掉的等价写法。
    **Phase 4 加了 `PENDING_APPROVAL` 这一档**：A 类卡点接进图之后，这个态不再是纸面态，
    而是端到端路径上的末态（`test_us02_...` 现在读到的就是它），所以它 MUST 在拒调整的集合里。
    """
    request_id = seed(session, statuses={"T02": state.value})
    response = patch_task(app_with(session), request_id, "T02", {"title": "改标题"})

    assert response.status_code == 409, response.text
    assert "task_not_adjustable" in response.text
    assert card_row(session, request_id, "T02").title == "卡 T02"
    assert str(card_row(session, request_id, "T02").status) == state.value, "门禁顺手改了态"


def test_draft_card_is_accepted(session):
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T02", {"title": "列表页要能滚动"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["adjusted"] == ["title"]
    assert body["status"] == "DRAFT"
    assert body["request_status"] == "SPLIT", "需求单状态 MUST 从 EN-01 读，不是卡态的第二个名字"
    assert card_row(session, request_id, "T02").title == "列表页要能滚动"


# ---------------------------------------------------------------- 写哪些、不写哪些


def test_patch_writes_only_the_columns_it_was_given(session):
    """`exclude_unset` 的正面：没传的列一律不动（这里一次改 2 列，核 5 列）。"""
    request_id = seed(session)
    target = card_row(session, request_id, "T02")
    snapshot_id = target.snapshot_id

    response = patch_task(
        app_with(session), request_id, "T02", {"title": "新标题", "token_budget": 1200}
    )

    assert response.status_code == 200, response.text
    assert response.json()["adjusted"] == ["title", "token_budget"]
    row = card_row(session, request_id, "T02")
    assert row.title == "新标题"
    assert row.token_budget == Decimal("1200")
    assert row.difficulty == 2 and row.skill_tags == ["html", "css"], "没传的列被写成了默认值"
    assert row.deadline_minutes == 30
    assert row.snapshot_id == snapshot_id, "snapshot_id 不是可改列，PATCH 不该换掉它指向的存档点"


def test_null_clears_the_nullable_columns_and_omission_keeps_them(session):
    """**"传了 null"与"没传"必须是两个不同的结果**（裁决 3 的 null 语义 + FR-006 的调整语义）。

    `token_budget: null` ⇒ 库里真变成 NULL；`deadline_minutes` 不传 ⇒ 原值 30 还在。
    实现若把 `exclude_unset` 换成 `exclude_none`，这同一条用例的两半会一起红 ——
    前者把"没传"当成"传了 null"，后者则永远清不掉列。
    """
    request_id = seed(session)
    patch_task(app_with(session), request_id, "T02", {"token_budget": 900})
    assert card_row(session, request_id, "T02").token_budget == Decimal("900")

    response = patch_task(app_with(session), request_id, "T02", {"token_budget": None})

    assert response.status_code == 200, response.text
    row = card_row(session, request_id, "T02")
    assert row.token_budget is None, "显式 null 没落到库里 = '改成无预算'这句话是假的"
    assert row.deadline_minutes == 30, "没传的列被动了 —— exclude_unset 被换成了 exclude_none"


@pytest.mark.parametrize(
    "name", ["title", "difficulty", "skill_tags", "depends_on", "acceptance_criteria"]
)
def test_explicit_null_on_not_null_column_is_rejected(session, name):
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T02", {name: None})

    assert response.status_code == 422, response.text
    assert "null_not_allowed" in response.text
    assert card_row(session, request_id, "T02").title == "卡 T02"


def test_locator_only_body_is_refused(session):
    """一条字段都没有的 PATCH 不是"成功改了零个字段"，而是没意义的请求 ⇒ 422。"""
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T02", {})

    assert response.status_code == 422, response.text
    assert "no_field_adjusted" in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "   "),
        ("skill_tags", []),
        ("skill_tags", ["  "]),
        ("acceptance_criteria", []),
        ("depends_on", [""]),
        ("depends_on", ["T01", "T01"]),
    ],
)
def test_fr_004_shape_still_gates_a_patched_card(session, field, value):
    """FR-004 的单卡形状在 PATCH 这条路上**照旧**成立（判据同 `validate_draft`，没重写）。"""
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T02", {field: value})

    assert response.status_code == 422, f"{field}={value!r} 竟然被收下：{response.text}"
    assert "task_shape_invalid" in response.text
    assert card_row(session, request_id, "T02").depends_on == ["T01"]


def test_unknown_card_is_404(session):
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T99", {"title": "随便"})

    assert response.status_code == 404, response.text
    assert "task_not_found" in response.text


def test_missing_store_is_503(session):
    """会话工厂由装配方注入（T123）；缺它直接 503，不开一条"内存版调整"的旁路。"""
    request_id = seed(session)
    client = app_with(session, with_store=False)

    response = patch_task(client, request_id, "T02", {"title": "新标题"})

    assert response.status_code == 503, response.text
    assert "store_not_configured" in response.text


# ---------------------------------------------------------------- 决策 3：重跑 Normalize + 原子性


def test_dangling_dependency_is_refused_and_nothing_is_written(session):
    """`depends_on` 指向不存在的卡 ⇒ 422，且**两张表**都保持原样（决策 3 的原子性）。

    原子性靠"先全部校验、后全部写"的顺序，不靠回滚：端点里没有 commit 可回滚。
    这里同时核 EN-07，因为"卡没改但映射被删了"是半写最容易长的形状。
    """
    request_id = seed(session)
    before = card_row(session, request_id, "T02")
    before_map = map_items(session, request_id, "T02")

    response = patch_task(app_with(session), request_id, "T02", {"depends_on": ["T99"]})

    assert response.status_code == 422, response.text
    assert "dependency_invalid" in response.text
    assert before.depends_on == ["T01"], "同一个对象被读出了新值 = 校验之前就已经 setattr 了"
    session.expire_all()
    assert card_row(session, request_id, "T02").depends_on == ["T01"]
    assert card_row(session, request_id, "T02").status == "DRAFT"
    assert map_items(session, request_id, "T02") == before_map


def test_a_cycle_created_by_the_patch_is_refused(session):
    """种子是合法 DAG，成环是这次 PATCH **造出来**的：T01←T02 变成 T01→T02→T01。

    FR-005 的判据点在入库前，而 PATCH 是最容易被当成旁路的那条路，所以这条必须存在。
    两半各钉一件事，缺一不可（M9 变异实测出来的）：
    ① 422 之后**会话里那个对象**还是原值 —— 挡住"先 setattr 再校验"（这种写法不会立刻进 PG，
       但脏状态留在装配方的事务里，下一次 flush 就写出去了）；
    ② `expire_all()` 后重新读 PG 仍是原值 —— 挡住"校验之前已经 flush"。
    只写 ② 是抓不到 ① 的：`Session.get()` 不 autoflush，脏值根本不会被发出去。
    """
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T01", {"depends_on": ["T03"]})

    assert response.status_code == 422, response.text
    assert "dependency_invalid" in response.text
    assert card_row(session, request_id, "T01").depends_on == [], "422 前已经 setattr，脏状态留在事务里"
    session.expire_all()
    assert card_row(session, request_id, "T01").depends_on == []


def test_card_order_in_the_response_is_the_post_patch_topological_order(session):
    """响应里的 `card_order` MUST 是**改后**这批卡的拓扑序（关系判据，不是手抄的常量序）。

    改法：把 T03 的依赖砍成空 ⇒ 它不再必须排在 T02 之后，而 T02 仍必须在 T01 之后。
    """
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T03", {"depends_on": []})

    assert response.status_code == 200, response.text
    order = response.json()["card_order"]
    assert sorted(order) == ["T01", "T02", "T03"]
    assert order.index("T01") < order.index("T02"), "依赖没被重算：T02 仍排在它的前件之前"


def test_a_legal_dependency_change_is_persisted_for_the_whole_batch(session):
    request_id = seed(session)
    response = patch_task(app_with(session), request_id, "T03", {"depends_on": ["T02"]})

    assert response.status_code == 200, response.text
    session.expire_all()
    assert card_row(session, request_id, "T03").depends_on == ["T02"]
    assert card_row(session, request_id, "T02").depends_on == ["T01"], "同批别的卡被动过"


# ---------------------------------------------------------------- 决策 2：改判据必须重申报映射


@pytest.mark.parametrize("declaration", [{}, {"acceptance_links": []}], ids=["没带键", "空数组"])
def test_criteria_change_without_redeclaration_is_refused(session, declaration):
    """决策 2 的强约束：**不许出现"改成功、映射为空"**。`None` 与 `[]` 同判"没有申报"。"""
    request_id = seed(session)
    response = patch_task(
        app_with(session),
        request_id,
        "T02",
        {"acceptance_criteria": ["列表可滚动", "支持键盘导航"], **declaration},
    )

    assert response.status_code == 422, response.text
    assert "acceptance_map_not_configured" in response.text
    assert card_row(session, request_id, "T02").acceptance_criteria == ["列表可滚动"]
    assert map_items(session, request_id, "T02") == ["列表可滚动"]


def test_criteria_change_replaces_this_card_s_rows_only(session):
    """整批替换的边界：本卡旧 1 行删掉、新 2 行插入，别的卡的行数一根毛都不动。"""
    request_id = seed(session)
    before_other = map_items(session, request_id, "T01")
    new_items = ["列表可滚动", "支持键盘导航"]

    response = patch_task(
        app_with(session),
        request_id,
        "T02",
        {
            "acceptance_criteria": new_items,
            "acceptance_links": links_for("T02", new_items),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["acceptance_map_rows_replaced"] == 1, "旧行没被替换掉 = 库里出现两套判据"
    assert body["acceptance_map_rows"] == 2
    session.expire_all()
    assert map_items(session, request_id, "T02") == sorted(new_items)
    assert map_items(session, request_id, "T01") == before_other
    assert card_row(session, request_id, "T02").acceptance_criteria == new_items
    rows = (
        session.query(AcceptanceMap)
        .filter(
            AcceptanceMap.feature_request_id == request_id, AcceptanceMap.task_card_id == "T02"
        )
        .all()
    )
    assert {r.produced_by for r in rows} == {PRODUCER}
    assert all(r.requirement_item.strip() for r in rows)


def test_redeclarations_are_checked_against_fr_007(session):
    """申报的形状判据仍是 `validate_links` 那四条，本端点不降低标准（也一条都不重写）。

    三种坏形状各自点名：漏一条判据 / 同一判据映射两次 / 引用别的卡的卡号。
    """
    new_items = ["列表可滚动", "支持键盘导航"]
    cases = [
        (links_for("T02", new_items[:1]), "漏了一条"),
        (links_for("T02", new_items + new_items[:1]), "同一判据映射两次"),
        (links_for("T01", new_items), "卡号不属于本卡"),
        (links_for("T02", new_items + ["卡上没写的标准"]), "引入卡外判据"),
    ]
    for links, why in cases:
        request_id = seed(session)
        response = patch_task(
            app_with(session),
            request_id,
            "T02",
            {"acceptance_criteria": new_items, "acceptance_links": links},
        )
        assert response.status_code == 422, f"{why} 竟然被收下：{response.text}"
        assert "acceptance_map_invalid" in response.text, response.text
        session.expire_all()
        assert card_row(session, request_id, "T02").acceptance_criteria == ["列表可滚动"], (
            f"{why}：映射被拒了，卡却已经改了 = 半写"
        )
        assert map_items(session, request_id, "T02") == ["列表可滚动"]


def test_declaring_map_without_touching_criteria_is_refused(session):
    """反向也拦：判据没改而申报映射，落库就是往卡上塞它没写过的标准（FR-029 同一件事）。"""
    request_id = seed(session)
    response = patch_task(
        app_with(session),
        request_id,
        "T02",
        {"title": "新标题", "acceptance_links": links_for("T02", ["列表可滚动"])},
    )

    assert response.status_code == 422, response.text
    assert "acceptance_map_without_criteria" in response.text
    assert card_row(session, request_id, "T02").title == "卡 T02"


def test_title_change_keeps_the_existing_map_untouched(session):
    """没改判据 ⇒ EN-07 不动（不许"顺手重算"，那会把没申报的东西写成申报过）。"""
    request_id = seed(session)
    before = map_items(session, request_id, "T02")

    response = patch_task(app_with(session), request_id, "T02", {"title": "新标题"})

    assert response.status_code == 200, response.text
    assert response.json()["acceptance_map_rows_replaced"] == 0
    assert response.json()["acceptance_map_rows"] == 1
    assert map_items(session, request_id, "T02") == before


# ---------------------------------------------------------------- 会话纪律


def test_the_patch_commits_and_closes_the_session_it_owns(session, monkeypatch):
    """提交点在本端点：`commit()` 一次、`close()` 一次、顺序不能反。绊线 + 会话内可见，两样一起。

    绊线不换连接数行，是因为夹具把会话绑在外部事务上 —— "另开连接看不见"在这种写法下恒成立，
    会变成一条永远绿的假判据（成因见 `test_orchestrate_graph.py` 的同名用例）。
    """
    request_id = seed(session)
    calls: list[str] = []
    monkeypatch.setattr(session, "commit", lambda *a, **k: calls.append("commit"))
    monkeypatch.setattr(session, "close", lambda *a, **k: calls.append("close"))

    response = patch_task(app_with(session), request_id, "T02", {"title": "新标题"})

    assert response.status_code == 200, response.text
    assert calls == ["commit", "close"], f"端点没有提交并收尾自己的会话：{calls}"
    assert card_row(session, request_id, "T02").title == "新标题", "会话内读不到 = 根本没写"
    assert independent_counts() == (0, 0, 0), "测试库留下了业务行，这条文件不再可重复跑"


# ---------------------------------------------------------------- US-02 全链


def test_us02_end_to_end_requirement_to_list_to_adjustment(session, bus):
    """Checkpoint 2 的收口判据之一：**需求 → 任务清单 → 可调整**，整条路不依赖看板与成本。

    前段走产品代码（`POST /requests` → 编排图），后段走本端点，读数以**裸 SQL**从 PG 拿：
    链上每一段都由它自己那一环的真实写入作证，中间没有一处是测试替身伪造的。

    **Phase 4 之后本用例分两段**（原话里的"不依赖人工卡点"不再成立，因为 FR-008 把 A 类卡点
    接进了这条链）：第一段读到的是"拆单完成 + 挂在 A 类卡点 + PATCH 被拒 409"，
    第二段在 DRAFT 清单上验"可调整"那两半（EN-02 改判据、EN-07 换代）。
    两段合起来才是原判据，缺一段就漏一种坏法；而"同一条链上拆完还能改"这件事
    现在是一条**钉住的 409 读数**，不是一句没人管的注释 —— 相撞的三条候选处置写在 Phase 4 申报里等裁。
    """
    from acp.api.requests import router as requests_router

    class StubCompleter:
        def complete(self, raw_input):
            return CompleterOutcome(
                self_completed=("页面数量取 1 个首页",),
                clarifications=("联系方式用邮箱还是表单？",),
            )

    class StubSplitter:
        def split(self, summary_digest):
            return [
                CardDraft(
                    task_id="T01",
                    title="搭首页骨架",
                    difficulty=2,
                    skill_tags=["html", "css"],
                    depends_on=[],
                    artifact_floor=FLOOR,
                    acceptance_criteria=["首屏有标题"],
                ),
                CardDraft(
                    task_id="T02",
                    title="填联系方式",
                    difficulty=3,
                    skill_tags=["html"],
                    depends_on=["T01"],
                    artifact_floor=FLOOR,
                    acceptance_criteria=["联系方式可见"],
                ),
            ]

        def acceptance_links(self, summary_digest):
            return [
                AcceptanceLink(
                    task_id="T01",
                    requirement_item="需求条目：展示公司产品信息",
                    acceptance_item="首屏有标题",
                    produced_by=PRODUCER,
                ),
                AcceptanceLink(
                    task_id="T02",
                    requirement_item="需求条目：留联系方式",
                    acceptance_item="联系方式可见",
                    produced_by=PRODUCER,
                ),
            ]

    app = FastAPI()
    app.include_router(requests_router)
    app.include_router(tasks_router)
    app.state.completer = StubCompleter()
    app.state.session_factory = lambda: session
    client = TestClient(app)

    intake = client.post(
        "/requests", json={"project_key": "demo", "raw_input": "做个介绍公司产品的静态网站"}
    )
    assert intake.status_code == 201, intake.text
    request_id = uuid.UUID(intake.json()["request_id"])

    # T109 起 `_normalize` 要真打一拍 ⇒ 这里显式给总线根。本文件只有这一条链走完 `Normalize`，
    # 逐个传参比 autouse 注入诚实（`test_orchestrate_graph.py` 那边是每条用例都走，才用 autouse）。
    bus.with_project("demo")
    advance_request(
        request_id,
        session=session,
        splitter=StubSplitter(),
        checkpointer=InMemorySaver(),
        bus_root=bus.path,
    )

    new_items = ["联系方式可见", "邮箱可点击"]
    adjustment = {
        "acceptance_criteria": new_items,
        "acceptance_links": links_for("T02", new_items, "需求条目：留联系方式"),
    }
    probe = session.execute
    en01 = probe(text("select status from feature_request where id=:i"), {"i": request_id}).scalar()
    cards = probe(
        text("select id, title, status, acceptance_criteria from task_card "
             "where feature_request_id=:i order by id"),
        {"i": request_id},
    ).all()
    gate = probe(
        text("select kind, state, version from approval_ticket where feature_request_id=:i"),
        {"i": request_id},
    ).all()

    # ── 第一段：Phase 4 之后这条链的末态是"挂在 A 类卡点上"，于是同一条链上的 PATCH 被拒。
    # **这是 FR-008（首次派发前必过卡点）与 owner 决策 1（PATCH 只放 DRAFT）的真实相撞点**，
    # 本轮既不放开门禁也不撤掉挂点，而是把相撞读数钉成断言等裁（三条候选处置写在 Phase 4 申报里）。
    gated = patch_task(client, request_id, "T02", adjustment)
    assert gated.status_code == 409, gated.text
    assert "task_not_adjustable" in gated.text

    # ── 第二段：FR-006 的"下发前可调整"仍然要能被端到端地证明，所以走 DRAFT 清单那一段
    # （本文件既有夹具 `seed`，卡的初态就是 DRAFT）。两段合起来才是原判据的两半，缺一半就漏一种坏法。
    draft_request = seed(
        session,
        cards=(("T01", [], ("首屏有标题",)), ("T02", ["T01"], ("联系方式可见",))),
    )
    patched = patch_task(client, draft_request, "T02", adjustment)
    assert patched.status_code == 200, patched.text

    draft_cards = probe(
        text("select id, status, acceptance_criteria from task_card "
             "where feature_request_id=:i order by id"),
        {"i": draft_request},
    ).all()
    maps = probe(
        text("select task_card_id, acceptance_item, requirement_item, produced_by "
             "from acceptance_map where feature_request_id=:i "
             "order by task_card_id, acceptance_item"),
        {"i": draft_request},
    ).all()
    print(
        "\nUS02-READINGS "
        f"post=201 graph_en01={en01} "
        f"cards={[(c[0], c[1], c[2], c[3]) for c in cards]} gate={list(gate)} "
        f"patch_on_gated_chain={gated.status_code} patch_on_draft=200 "
        f"draft_cards={[(c[0], c[1], c[2]) for c in draft_cards]} en07_rows={len(maps)} "
        f"en07={[(m[0], m[1]) for m in maps]} "
        f"order={patched.json()['card_order']} replaced={patched.json()['acceptance_map_rows_replaced']}"
    )

    assert en01 == "SPLIT"
    assert [c[0] for c in cards] == ["T01", "T02"]
    assert [c[2] for c in cards] == ["PENDING_APPROVAL", "PENDING_APPROVAL"], (
        "卡的末态必须是待批：A 类卡点没挂上就说明 FR-008 的挂点被绕过了"
    )
    assert list(gate) == [("A_SPLIT", "PENDING", 0)], f"EN-09 上应当恰有一张未决 A 类单，读到 {list(gate)}"
    assert cards[1][3] == ["联系方式可见"], "被拒的那次 PATCH MUST 一字不写"

    assert [c[0] for c in draft_cards] == ["T01", "T02"]
    assert draft_cards[1][2] == new_items, "卡上的判据没落到库里"
    # 在 Python 侧排序而不是靠 `order by acceptance_item`：中文条目在 PG 里的 Collation 不归本用例管。
    assert sorted((m[0], m[1]) for m in maps) == sorted(
        [("T01", "首屏有标题"), ("T02", "邮箱可点击"), ("T02", "联系方式可见")]
    ), "EN-07 必须与卡当前声明的判据严格一致（决策 2 要的就是这条不变式）"
    assert independent_counts() == (0, 0, 0)
