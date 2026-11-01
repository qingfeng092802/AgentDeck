"""T037：`POST /requests` 到确认摘要的最小闭环（FR-001 / FR-002 / FR-003 的正反例）。

**这一档要 PG，所以不在 fast 集**（分档口径与 `test_schema_invariants.py` 同源：fast 不碰 PG 与
外部凭据，否则 Docker 一停先跑集就跟着红，把环境抖动算成代码缺陷）。缺 `ACP_DB_DSN` 时整档 skip
并在收口读数里写明 skip 条数。

**端点写库为什么还能用例内回滚**：注入的 `session_factory` 每次返回一个**绑在同一条外层事务连接上**的
新会话。端点现在自己 `commit()` + `close()`（Phase 15 那一笔改的就是这个边界，见 `api/requests.py`），
而 SQLAlchemy 2.1.4 在"会话绑到已 begin 的连接"这个形状下 `commit()` 发不到 PostgreSQL、
`close()` 也不动外层事务 —— 于是写入真实打到 PG（不是 mock、不是内存方言），跑完随外层事务一起消失，
库里仍不留业务数据，可重复跑。

**这里"回库读"读的是同一条连接**，所以本文件量的是"端点有没有把这一笔发到它自己的会话"，
**量不到"这一笔有没有发到 PostgreSQL"**：摘掉 `_persist` 里那句 `commit()`，本文件照样全绿
（口径同 `test_approval_api.py` 第 4 条，那条坑是笔 U 撞出来的）。实锤"另一条连接读得到 EN-01"
落在 `test_request_commit_boundary.py`，两个文件各管一段，不许互相冒充。

**补全器是注入的桩，且明写它是桩**：FR-002 的主语"总管"实体是适配器，归 Phase 3（T038 ~ T048）。
本文件验的是**形状与拒绝路径**（摘要必须显式列出补全项、缺依赖时 503 而不是假成功），
不是模型补全得准不准 —— 后者要等适配器，届时另有判据，MUST NOT 拿本文件的绿当那条的绿。
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from acp.api.requests import router
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.local_completer import COMPLETER_ID, LocalPassthroughCompleter
from acp.orchestrate.nodes.plan import (
    CLARIFICATION_KEY,
    SELF_COMPLETED_KEY,
    CompleterOutcome,
    IncompleteDigest,
    build_summary_digest,
    is_clarification_waived,
    plan_requirement,
    validate_summary_digest,
)
from acp.persistence.models_core import FeatureRequest, TaskCard

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（缺凭据时 skip 而不是假绿）",
)

RAW = "做个介绍公司产品的静态网站，配色你看着办，最好下周一能看\n要有联系方式"
COMPLETIONS = ("页面数量取 1 个首页", "配色按“简洁商务”取，不追问")
CLARIFICATIONS = ("联系方式用邮箱还是表单？")


class StubCompleter:
    """占位补全器：把给定的补全项与澄清问题原样返回，并记下收到的原文。

    它存在的全部理由是"没有真总管可注入"，不是"规则版补全已经够用"。真的那条路在 Phase 3。
    """

    def __init__(self, outcome: CompleterOutcome | None = None) -> None:
        self.outcome = outcome or CompleterOutcome(COMPLETIONS, CLARIFICATIONS)
        self.seen: list[str] = []

    def complete(self, raw_input: str) -> CompleterOutcome:
        self.seen.append(raw_input)
        return self.outcome


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    # 见 `test_schema_invariants.py` 的同名说明：池里的连接不显式归还就会在解释器退出时
    # 触发 psycopg 的 `__del__` ResourceWarning，表现为按 GC 时序抖动的偶发 warning。
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema_must_be_applied(engine):
    """入口这三张表没建齐 ⇒ 全红而不是 skip（"没建表所以没测"读起来像通过，是最要防的绿）。"""
    present = set(inspect(engine).get_table_names())
    missing = {"feature_request", "role_spec", "snapshot"} - present
    if missing:
        pytest.fail(f"缺表 {sorted(missing)} ⇒ 先跑 `alembic upgrade head`（真跑，不是只写脚本）")


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


def read_table(session, model):
    """在**另一个会话**里读这条连接上的现值。

    端点借走的会话它自己 `close()` 了（Phase 15 之后提交边界归端点），夹具那个 `session` 因此
    在被借出过一次之后就与外层事务解绑了 —— 拿它继续 `query` 会读不到前一步写下的行
    （`test_approval_api.py` 的 `app_for` 记过同一发实测）。所以写走工厂、读走这里。
    """
    with Session(bind=session.get_bind()) as item:
        return item.query(model).all()


def read_back(session):
    return read_table(session, FeatureRequest)


def same_connection_factory(session):
    """生产形状（一次请求一个**新**会话），但绑在夹具那条外层事务的连接上。

    为什么不把夹具那个会话本身交出去：端点现在会 `close()` 它借到的会话（提交边界归端点），
    交出去第一发之后夹具就读不到自己前一步建的行了；同连接上的新会话照样读得到未提交到 PG 的行，
    隔离仍由夹具结束时那一次外层 `rollback()` 提供。
    """
    return lambda: Session(bind=session.get_bind())


def build_client(session, *, completer=..., factory=...) -> TestClient:
    """`...` = **不注入**该项（用来测 fail-closed 的那两条 503），传对象才注入。

    这里刻意没有"缺省给个工厂"的分支：`factory=...` 的含义就是"这一发要验的就是没装配"，
    给它补一个默认值会把 503 那一格静默变成 201（= 判据自己把要防的形状抹掉）。
    """
    app = FastAPI()
    app.include_router(router)
    if completer is not ...:
        app.state.completer = completer
    if factory is not ...:
        app.state.session_factory = factory
    return TestClient(app)


def client_for(session, *, completer) -> TestClient:
    """注入补全器 + 生产形状的会话工厂（绝大多数用例要的那一挡）。"""
    return build_client(session, completer=completer, factory=same_connection_factory(session))


@pytest.fixture
def client(session):
    return client_for(session, completer=StubCompleter())


def post(client, **overrides):
    body = {"project_key": "demo", "raw_input": RAW}
    body.update(overrides)
    return client.post("/requests", json=body)


def stored(session) -> FeatureRequest:
    rows = read_back(session)
    assert len(rows) == 1, f"入口应当恰写一行 EN-01，实际 {len(rows)}"
    return rows[0]


def test_httpx_is_importable_for_the_test_client():
    """`TestClient` 依赖 `httpx`，它现在是 `pyproject` 的**运行时**直接依赖。

    口径变更（owner 2026-10-13 裁决，T042 待裁①）：此前它只是随 `openai` 传递进来、并显式列在 dev 组，
    而 `doubao` / `workbuddy` 两支 transport 在模块层 `import httpx` ⇒ 声明面与运行时事实不符，已挪进
    `[project].dependencies`。本文件与将来的 api 用例都靠它跑，所以这条绊线照旧留着：它红 = 依赖没了，
    整套 HTTP 用例会以"导入错误"的形态消失，而不是以"测试失败"的形态出现（后者看得见，前者会被读成"没有用例"）。
    """
    assert importlib.util.find_spec("httpx") is not None, (
        "httpx 不可导入 ⇒ fastapi.testclient 用不了；请确认它在 pyproject 的 [project].dependencies 里"
    )


def test_one_free_sentence_is_enough(client, session):
    """FR-001 的正面对照：只有一个自由文本字段的请求就能建单，且原文**逐字**入库。

    逐字这条不是凑数：入口若顺手做了"归一化"（去换行、改标点），FR-001 的"不必遵循模板"就成了空话，
    因为存下来的已经不是使用者说的那句话。
    """
    response = post(client)
    assert response.status_code == 201, response.text
    row = stored(session)
    assert row.raw_input == RAW
    assert row.project_key == "demo"


def test_the_write_reaches_a_session_the_endpoint_never_borrowed(session):
    """摘掉 `_persist` 的 flush/commit 那一发，这条必须红 —— 它不是"看着像在写"。

    首版这条靠的是"关掉 autoflush 再查"：那时端点与夹具**共用一个会话**，而 `stored()` 走 ORM 查询
    会触发 autoflush，替端点把 INSERT 补发了一遍 ⇒ 摘掉 `session.flush()` 照样 24 passed。
    判据看着在验"落库"，实际验的是"session 里挂着过" —— 最阴的一种绿（用例是活的，但被别人的动作喂饱）。

    现在工厂**一次请求一个新会话**（与 `serve` 给的生产形状同形），写法换成更强的一格：
    写入必须由端点自己发到连接上，一个它从没见过的会话才读得到。
    **它量不到 commit**：同一条连接上的新会话读得到"发了 INSERT 但没提交"的行，摘掉 `commit()`
    这一发照样绿。实锤"另一条连接读得到 EN-01"在 `test_request_commit_boundary.py`，两档不许互相冒充。
    """
    assert post(client_for(session, completer=StubCompleter())).status_code == 201
    rows = read_back(session)
    assert len(rows) == 1, "写入还挂在端点那个会话的 pending 里，没发到连接上"
    assert rows[0].raw_input == RAW


def test_completer_sees_the_raw_input_verbatim(session):
    stub = StubCompleter()
    assert post(client_for(session, completer=stub)).status_code == 201
    assert stub.seen == [RAW], "节点改写了送去补全的原文"


def test_the_response_names_which_completer_produced_the_digest(session):
    """`completer_id` 是"这份摘要是谁补的"唯一读数位：桩顶上来时必须看得见是桩。

    这条不是凑数。Phase 15 之后 `serve` 默认注了一条空补全的本机旁路，于是"摘要里为什么是空的"
    有两个真值完全不同的解释（总管判定没有模糊点 / 根本没接总管）。少了这个字段，
    看板只能靠猜，而猜错的那一半就是 FR-002 的假绿。
    """
    body = post(client_for(session, completer=StubCompleter())).json()
    assert body["completer_id"] == "StubCompleter", body
    live = post(client_for(session, completer=LocalPassthroughCompleter())).json()
    assert live["completer_id"] == COMPLETER_ID == "local-passthrough", live
    assert live["summary_digest"][SELF_COMPLETED_KEY] == [], "本机旁路不该补出任何东西"


def test_summary_digest_lists_completions_item_by_item(client, session):
    """FR-002：补全内容必须是**逐条**的列表，不是拼成一段话的字符串。"""
    digest = post(client).json()["summary_digest"]
    assert digest[SELF_COMPLETED_KEY] == list(COMPLETIONS)
    assert isinstance(digest[SELF_COMPLETED_KEY], list)


def test_digest_carries_only_the_two_documented_keys(client, session):
    """原则 I 的形状面：摘要里不出现 `raw_input` 的副本，也不多出文档没给的第二份事实。"""
    digest = post(client).json()["summary_digest"]
    assert set(digest) == {SELF_COMPLETED_KEY, CLARIFICATION_KEY}
    assert RAW not in repr(digest)


@pytest.mark.parametrize(
    ("digest", "reason"),
    [
        ({}, "缺 `自行补全项` 这一项 = 未显式列出（T037 点名的那条不合格）"),
        ({CLARIFICATION_KEY: []}, "只有澄清问题、没有补全项 ⇒ 同样不合格"),
        ({"自行补全项": "配色随便"}, "补全项是字符串而不是列表 ⇒ 读不出条目数"),
        ({"自行补全项": ["配色", "  "]}, "空白条目是凑数"),
        ({"自行补全项": ["配色", "配色"]}, "重复条目会让「列了几条」这个读数失真"),
        ({"自行补全项": ["配色"], "澄清问题": [None]}, "条目必须都是字符串"),
    ],
    ids=[
        "missing-self-completed",
        "only-clarifications",
        "value-not-a-list",
        "blank-item",
        "duplicated-item",
        "non-string-item",
    ],
)
def test_incomplete_digests_are_rejected(digest, reason):
    assert reason
    with pytest.raises(IncompleteDigest):
        validate_summary_digest(digest)


def test_digest_must_be_an_object():
    with pytest.raises(IncompleteDigest):
        validate_summary_digest(["自行补全项"])


def test_no_ambiguity_is_legal_and_not_read_as_waived():
    """空补全列表 = "总管判定没有模糊点"，是合法值；把它当非法会逼调用方编造补全项来过关。"""
    digest = build_summary_digest(CompleterOutcome())
    assert digest == {SELF_COMPLETED_KEY: [], CLARIFICATION_KEY: []}
    assert is_clarification_waived(digest, []) is False, "没问题时不该记成「放弃了回答」"


def test_waived_only_when_questions_were_dropped(session):
    """FR-003 的三态口径各来一次：提了问题没答 = True；没提问题 = False；答了 = False。

    写成三条而不是"一个布尔取反"是因为 `clarification_waived` 只在第一态才是真的"放弃"；
    把"没问"也算进去，这个字段就再也读不出"使用者跳过了澄清"这件事。
    """
    asked = build_summary_digest(CompleterOutcome(COMPLETIONS, CLARIFICATIONS))
    silent = build_summary_digest(CompleterOutcome(COMPLETIONS))
    assert is_clarification_waived(asked, []) is True
    assert is_clarification_waived(asked, ["用邮箱"]) is False
    assert is_clarification_waived(asked, ["   "]) is True, "空白回答不等于回答了"
    assert is_clarification_waived(silent, []) is False


def test_intake_does_not_block_on_clarifications(client, session):
    """FR-003 的正面对照：总管提了澄清问题、使用者一个字没答，请求仍 201 且不等待。"""
    response = post(client)
    assert response.status_code == 201
    body = response.json()
    assert body["summary_digest"][CLARIFICATION_KEY] == list(CLARIFICATIONS)
    assert body["clarification_waived"] is True
    assert stored(session).clarification_waived is True


def test_intake_stays_at_intake_and_creates_no_card(client, session):
    """拆单属 T032：入口只写 EN-01。这张需求单下必须一张卡都没有（负面对照）。"""
    body = post(client).json()
    assert body["status"] == "INTAKE"
    assert len(read_back(session)) == 1
    assert read_table(session, TaskCard) == []


def test_missing_completer_fails_closed(session):
    """没注入补全器 ⇒ 503，而不是"规则版补全器"顶上 —— 后者会让 FR-002 读起来像已实现。

    `serve` 现在默认注了一条本机旁路，所以这一格在生产面上不再被触发；它钉的是**装配面自己**：
    直接 `create_app()` 而不给补全器时入口必须响亮地拒绝，而不是悄悄找一条兜底。
    """
    client = build_client(session, completer=..., factory=same_connection_factory(session))
    response = post(client)
    assert response.status_code == 503
    assert response.json()["detail"].startswith("completer_not_configured")
    assert read_back(session) == [], "503 之前不该已经落库"


def test_missing_store_fails_closed():
    """没注入会话工厂 ⇒ 503 `store_not_configured`；此时也不该有 EN-01 行可查。"""
    client = build_client(None, completer=StubCompleter(), factory=...)
    assert post(client).status_code == 503
    assert post(client).json()["detail"].startswith("store_not_configured")


def test_blank_requirement_is_rejected(client):
    """FR-001 不要求模板，但"没有需求"不是需求：空白原文走 pydantic 的 min_length 会放行，
    所以节点侧那条 `BlankRequirement` 才是这一格的强制点 ⇒ 期望 422。
    """
    assert post(client, raw_input="   ").status_code == 422


@pytest.mark.parametrize("body", [
    {"project_key": "demo"},
    {"raw_input": ""},
    {"project_key": "", "raw_input": RAW},
])
def test_envelope_shape_is_enforced(body, client):
    """入口自述能力的反面：缺字段、空字段一律 422。这与"要填表单"是两件事 —— 这里拒的是信封形状。"""
    assert client.post("/requests", json=body).status_code == 422


def test_unknown_field_is_rejected_without_a_digest(client):
    """`extra="forbid"`：拼错的字段被拒而不是静默当"没传"。"""
    response = post(client, raw_imput=RAW)
    assert response.status_code == 422


def test_plan_node_rejects_blank_before_calling_completer():
    stub = StubCompleter()
    with pytest.raises(ValueError):
        plan_requirement("  \n ", completer=stub)
    assert stub.seen == [], "空白原文也去打了补全器"


# —— 请求级路由（2026-10-31 路线 B）


ROUTING = {
    "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash", "thinking": "medium"},
    "reviewer": {"agent_id": "doubao"},
    "acceptor": {"agent_id": "qoder"},
}


def test_model_routing_is_stored_and_read_back(client, session):
    """路由随需求单落库、随读面回来；**没给路由的请求必须保持 NULL**（不许被写成"默认 qoder"）。

    最后那条是本条的鉴别力所在：NULL 是"这一单没点名"，它要能继续走自动选人；
    若入口把缺失补成 `{"executor": {"kind": "qoder"}}`，历史行为就被一次提交悄悄改写了。
    """
    plain = post(client, project_key="plain")
    assert plain.status_code == 201, plain.text
    routed = post(client, project_key="routed", model_routing=ROUTING)
    assert routed.status_code == 201, routed.text

    rows = {row.project_key: row for row in read_back(session)}
    assert rows["plain"].model_routing is None, "没给路由的老请求必须保持 NULL"
    assert rows["routed"].model_routing == ROUTING

    view = client.get(f"/requests/{rows['routed'].id}")
    assert view.status_code == 200, view.text
    assert view.json()["model_routing"] == ROUTING, "读面没把路由带回来 ⇒ 页面无从显示/复核"


def test_an_api_executor_without_vendor_or_model_is_refused(client):
    """`kind=api` 缺厂商/模型 ⇒ 422 点名缺哪一格，而不是落库后在适配器里报"缺凭据"。"""
    response = post(client, model_routing={"executor": {"kind": "api", "vendor": "", "model": ""}})
    assert response.status_code == 422, response.text
    assert "model_routing.executor" in response.text


def test_unknown_routing_keys_are_refused_and_keys_never_land_in_the_row(client, session):
    """路线 C：`api_key` 是**合法字段**，但落库那份必须没有它；未知键仍然 422。"""
    bad = post(client, model_routing={"executor": {"kind": "qoder", "api_keyx": "x"}})
    assert bad.status_code == 422, bad.text
    assert "api_keyx" in bad.text

    ok = post(
        client,
        project_key="withkey",
        model_routing={
            "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash", "api_key": "sk-unit-test"}
        },
    )
    assert ok.status_code == 201, ok.text
    row = next(item for item in read_back(session) if item.project_key == "withkey")
    assert "api_key" not in json.dumps(row.model_routing), "key 不许落库"
    assert row.model_routing["executor"]["key_fp"], "落库的是短指纹，用来判同模型不同 key"
