"""T103 的 HTTP 面判据：`PATCH /autonomy`（FR-055｜R-15，`tasks.md` 的 T103 行 + owner 2026-10-15 裁决 1）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_approval_api.py`）：
端点只是翻译层，"这一次改档到底生不生效"是数据库给的读数；拿内存替身跑就会把"应用层以为自己拦住了"
当成"接口拦住了"。**一律不 commit**：外层事务 + 结束回滚。

要回答的七件事，按 owner 五条关键约束对齐：

1. **使用者改得动**（约束 1 的反面半边：门不是焊死的）——200 之后**回库里读**，不看响应体自述；
2. **`orchestrator` 发起必须 403**（约束 3 + 5，这一条就是 T102 偏离 ③ 的闭环半边）；
3. **EN-03 注册过的 `agent_id` 403**（约束 1）；
4. **`machine_principals` 由装配点注入**（约束 2）：没注入 ⇒ 503，注入**空集合**也算没配；
   同一个请求在注入之后必须 200（这一对是反向对照，缺了就分不清"门挡人"和"门坏了"）；
5. **三个 code 的状态码按裁决 1 落地**（约束 4）：403 / 422 / 400，且响应体点名 code；
6. **端点不写自己不该写的列、也不碰 ORM**（EN-16 的写者分工，裁决 3 的 HTTP 半边）；
7. **同一对状态码在 `create_app()` 上成立**（owner 2026-10-19 裁 W-9 选甲 ⇒ `serve.py --machine-principal`）：
   上面 1~4 用的都是本文件手搓的 `FastAPI()`，那一档证明的是"端点认这个注入项"；
   节 五 换成生产装配点，证明的是"启动面真的会给它"。两档差的正是笔 I 实测到的那个空档。

两处刻意的写法：
- **"是仓储拒的还是 pydantic 抢答的"用 `detail` 的类型分辨**：pydantic 的 422 给的是**列表**，
  而 `mode_not_allowed` 这条走的是端点里的字符串 `detail`。只看 422 这个数字分不出这两者，
  而把域写进模型字段（`Literal`）恰好就会退化成前者 —— 那正是本端点刻意不做的事。
- **默认档不被写入这件事也断**：无行建行时用 `FULL` 请求，库里必须就是 `FULL`；
  若端点/仓储先建默认档再改，`updated_at` 会多写一次而读数看不出来，所以这里断的是最终列值而不是行数。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp.api import autonomy as api
from acp.app import create_app
from acp.approval import repository as approval_repo
from acp.config.loader import DSN_ENV, db_url, load_danger_list
from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.persistence import autonomy
from acp.persistence.models_core import AgentUnit
from acp.persistence.models_registry import AutonomyState

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（改档的读回只能在库里做）",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SOURCE = REPO_ROOT / "src" / "acp" / "api" / "autonomy.py"

PROJECT = "acp-self-host"
HUMAN = "owner-1"
#: 装配点该注入的那一项（本文件用真常量而不是抄一个 "orchestrator" 字面串 ⇒
#: 那个名字一旦改名，这里的读数会跟着变；抄字面串就会出现"用例还在绿、接缝已经断了"）。
MACHINES = frozenset({AUTOMATED_ACTOR})

T_SEED = dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.timezone.utc)
T_INTERCEPT = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)
#: 建行时写进指纹列的"旧版"读数：与 `load_danger_list().fingerprint` 明显不同 ⇒
#: "改档顺手把清单指纹刷成当前版"这种写法能被读出来（裁决 3 的 HTTP 半边）。
VERSION_BEFORE = "sha256:" + "0" * 64

_UNSET = object()


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


def make_client(
    session: Session,
    *,
    principals: object = _UNSET,
    danger_list: object = _UNSET,
    with_store: bool = True,
) -> TestClient:
    """装配点替身（真正的装配归 T123，`api/__init__.py` 明写本轮不建 `app.py`）。

    三个注入项都用 `_UNSET` 表达"没配"，而不是给一个产品代码里的默认值 ——
    默认值会让"漏配置"这件事在读数里消失（口径同 `api/requests.py` 的两个 fail-closed 503）。
    """
    app = FastAPI()
    app.include_router(api.router)
    if with_store:
        app.state.session_factory = lambda: session
    if principals is not _UNSET:
        app.state.machine_principals = principals
    if danger_list is not _UNSET:
        app.state.danger_list = danger_list
    return TestClient(app)


def _patch(client: TestClient, mode: str, principal: str | None = HUMAN) -> object:
    headers = {api.PRINCIPAL_HEADER: principal} if principal is not None else {}
    return client.patch("/autonomy", json={"project_key": PROJECT, "mode": mode}, headers=headers)


def _count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(AutonomyState)) or 0)


def _row(session: Session) -> AutonomyState:
    session.expire_all()
    row = session.get(AutonomyState, PROJECT)
    assert row is not None
    return row


def _seed_row(session: Session, mode: str = "SAFE") -> AutonomyState:
    """测试侧直接建行（不借端点，也不借 `ensure_autonomy_state`）⇒ 起始态与被测行为无关。"""
    row = AutonomyState(
        project_key=PROJECT,
        mode=mode,
        danger_list_version=VERSION_BEFORE,
        updated_by="user-1",
        updated_at=T_SEED,
        last_intercept_at=T_INTERCEPT,
    )
    session.add(row)
    session.flush()
    return row


def _register_agent(session: Session, agent_id: str) -> None:
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=1,
            cost_band="MID",
            max_parallel=1,
            current_load=0,
            historical_success_rate=None,
            success_rate_sample=0,
            workspace_dir=f"{agent_id}-workspace",
            enabled=True,
        )
    )
    session.flush()


# ── 一、使用者改得动（约束 1 的反面半边）─────────────────────────────────────


def test_a_human_principal_moves_the_stored_tier(session: Session) -> None:
    """200 之后回库里读：档位、改档人跟着变，而**清单指纹列与拦截时刻一个字都不动**。

    注入的 `danger_list` 是当前随仓清单（指纹与 `VERSION_BEFORE` 不同），所以"端点把指纹顺手刷成
    当前版"这种写法会在这里红 —— 那一列的写者是重载路径（owner 裁决 3），不是改档。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "FULL")

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "FULL"
    row = _row(session)
    assert str(row.mode) == "FULL"
    assert row.updated_by == HUMAN
    assert row.updated_at > T_SEED
    assert row.danger_list_version == VERSION_BEFORE
    assert row.last_intercept_at == T_INTERCEPT


def test_the_response_reports_the_stored_row_not_the_request(session: Session) -> None:
    """响应体的 `danger_list_version` 取的是库里那一行，不是请求方递进来的那份清单。

    写法上这两种读数长得很像（都是 `sha256:` 串），但含义相反：前者是"当时生效的是哪版"，
    后者会变成"调用方自称生效的是哪版"。断言两侧（响应 + 库）都等于 `VERSION_BEFORE`。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    body = _patch(client, "SAFE").json()

    assert body["danger_list_version"] == VERSION_BEFORE
    assert body["danger_list_version"] != load_danger_list().fingerprint
    assert body["last_intercept_at"] == T_INTERCEPT.isoformat()
    assert _row(session).danger_list_version == VERSION_BEFORE


def test_a_mode_change_on_a_bootstrapped_project_needs_no_danger_list(session: Session) -> None:
    """反向对照（约束 2 的那道门不是焊死的另一半）：既有行改档**不需要**注入清单。

    若把清单变成整条端点的前置条件（一律 503），这条就会红 —— 那种"过度失败"的形态是：
    明明能改档，却因为一项没配而全拒，使用者看到的是平台坏了而不是自己漏了参数。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES)

    assert _patch(client, "FULL").status_code == 200
    assert str(_row(session).mode) == "FULL"


# ── 二、机器主体一律 403（约束 1 / 3 / 5）─────────────────────────────────────


def test_the_automated_actor_is_refused_with_403(session: Session) -> None:
    """**owner 点名要的那一条**：`agent_id=orchestrator` 发起的改档必须 403。

    这一条同时是 T102 偏离 ③ 的闭环半边：`orchestrator` 不在 EN-03（它是自动选人的留痕主语，
    `select_worker.AUTOMATED_ACTOR` 自己的注释写着"不是一个已成立的身份"），
    所以库侧名单拦不到它 —— 只有装配点把它申报进 `machine_principals` 才拦得到。
    断三件：状态码、响应体点名 `principal_not_allowed`、**库里档位没动**。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "FULL", principal=AUTOMATED_ACTOR)

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith(autonomy.PRINCIPAL_NOT_ALLOWED)
    row = _row(session)
    assert str(row.mode) == "SAFE"
    assert row.updated_by == "user-1"
    assert row.updated_at == T_SEED


def test_a_registered_agent_is_refused_with_403(session: Session) -> None:
    """EN-03 注册过的工具同样 403（这一支不需要装配点申报，库侧名单就是唯一载体）。"""
    _register_agent(session, "qoder")
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "FULL", principal="qoder")

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith(autonomy.PRINCIPAL_NOT_ALLOWED)
    assert str(_row(session).mode) == "SAFE"


def test_a_missing_principal_header_is_403_and_writes_nothing(session: Session) -> None:
    """没有主体就没有改档：缺请求头 ⇒ 403，且未建档的项目一行都不建。

    这一条与 `api/approvals.py` 的 `_principal` 同形，也共享那条披露：MVP-1 没有认证，
    403 挡的是"披错身份误操作"，**挡不住冒充**（一支工具填一个使用者名，这一层分不出来）。
    """
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "FULL", principal=None)

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith(autonomy.PRINCIPAL_NOT_ALLOWED)
    # 断"缺的是哪一个头"而不是只断 403：端点那道 `_principal` 与仓储那道空白主体检查给出的
    # 状态码与 code 完全一样，只断这两者的话，把 `_principal` 整段删掉这条也照样绿
    # （本轮变异跑里实测到过这一条，故补此断言 —— 那时缺头请求会退化成"空白主体"那条路）。
    assert api.PRINCIPAL_HEADER in detail
    assert _count(session) == 0


def test_an_empty_machine_set_counts_as_unconfigured(session: Session) -> None:
    """注入**空集合**当"没配"处理 ⇒ 503，不当成"这个部署里没有机器"。

    为什么不是 403 也不是 200：这是部署缺陷，不是这次请求的错；而把它读成"没有机器"
    就会让一次漏配置变成 FR-055 唯一要挡的那扇门（总管自切档位）。反向对照在下一条
    （同一条请求在申报之后必须 403 而不是 503），两条一起才说明这道门是"没装好"而不是"焊死了"。
    """
    _seed_row(session)
    client = make_client(session, principals=frozenset(), danger_list=load_danger_list())

    response = _patch(client, "FULL", principal=AUTOMATED_ACTOR)

    assert response.status_code == 503, response.text
    assert "machine_principals_not_configured" in response.json()["detail"]
    assert str(_row(session).mode) == "SAFE"


def test_an_uninjected_machine_set_refuses_every_change(session: Session) -> None:
    """`machine_principals` 整项没注入 ⇒ 503（连人类主体也不放行）。

    与上一条同向：缺的是"哪些主体是机器"这份知识，而不是"这次是谁"。
    反向对照放在一节的第三条（注入之后人类主体 200），所以这里不是恒红的形状。
    """
    _seed_row(session)
    client = make_client(session, danger_list=load_danger_list())

    response = _patch(client, "FULL", principal=HUMAN)

    assert response.status_code == 503, response.text
    assert "machine_principals_not_configured" in response.json()["detail"]
    assert str(_row(session).mode) == "SAFE"


# ── 三、裁决 1 的三个状态码（约束 4）─────────────────────────────────────────


def test_a_third_tier_is_422_and_the_rejection_comes_from_the_domain(session: Session) -> None:
    """第三值 ⇒ 422 + 点名 `mode_not_allowed`，而且**必须是仓储拒的，不是 pydantic 抢答的**。

    分辨方法：pydantic 的校验失败把 `detail` 给成**列表**（每条是一个 dict），
    而仓储那条走端点里的字符串 `detail`。域值一旦被人写进模型字段（`Literal["FULL","SAFE"]`），
    这条就会变成列表 ⇒ 红。那时该改的是把域还给 `persistence/enums.py`，不是把断言改成列表。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "PARANOID")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), f"这是 pydantic 的 422 而不是域的 422：{detail!r}"
    assert detail.startswith(autonomy.MODE_NOT_ALLOWED)
    assert str(_row(session).mode) == "SAFE"


def test_an_automated_actor_with_a_bad_tier_hears_about_the_tier_first(session: Session) -> None:
    """钉住一个**判定优先级**，免得日后被读成"403 漏了"：非法档位 + 机器主体 ⇒ 422。

    仓储先核值再核主体（`set_autonomy_mode` 里档位判定在 `_guard_updated_by` 之前）。
    两条都是拒、都不写库，所以这个次序不放松任何一道门；把它钉成用例是因为不钉的话，
    下一轮看到"orchestrator 拿到了 422 而不是 403"会以为约束 5 破了而"顺手修反"。
    """
    _seed_row(session)
    client = make_client(session, principals=MACHINES, danger_list=load_danger_list())

    response = _patch(client, "PARANOID", principal=AUTOMATED_ACTOR)

    assert response.status_code == 422, response.text
    assert str(_row(session).mode) == "SAFE"


def test_an_unbootstrapped_project_is_created_at_the_requested_tier(session: Session) -> None:
    """无行 + 注入了清单 ⇒ upsert 建行，档位 = **请求值**（不是先建默认档再改）。"""
    loaded = load_danger_list()
    client = make_client(session, principals=MACHINES, danger_list=loaded)

    response = _patch(client, "FULL")

    assert response.status_code == 200, response.text
    row = _row(session)
    assert str(row.mode) == "FULL"
    assert row.updated_by == HUMAN
    assert row.danger_list_version == loaded.fingerprint
    assert _count(session) == 1


def test_an_unbootstrapped_project_without_a_list_is_400_and_writes_no_row(session: Session) -> None:
    """无行 + 没清单 ⇒ 400 `danger_list_required`（owner 裁决 1："与 payload_incomplete 同类：缺必填"），
    且**库里 0 行** —— 不拿占位指纹凑一行审计记录。
    """
    client = make_client(session, principals=MACHINES)

    response = _patch(client, "FULL")

    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith(autonomy.DANGER_LIST_REQUIRED)
    assert _count(session) == 0


def test_a_missing_store_is_503_rather_than_a_memory_only_success(session: Session) -> None:
    """会话工厂没配 ⇒ 503。端点不许在"写不进库"的情况下回一个 200。"""
    client = make_client(session, principals=MACHINES, with_store=False)

    response = _patch(client, "FULL")

    assert response.status_code == 503, response.text
    assert "store_not_configured" in response.json()["detail"]


def test_the_status_map_is_load_bearing_and_covers_exactly_the_three_codes() -> None:
    """裁决 1 的三个数钉成读数，并断这张表**不是装饰物**。

    三件事一起断才成立：① 表里就是三个 code、值是 403 / 422 / 400；
    ② `AutonomyWriteRejected` 构造时查表 ⇒ 新加一个 code 而没给状态，当场 `KeyError`
    （这一条是那条注入用例，红绿都是判据）；③ 端点用的是 `exc.http_status` 而不是自己 if-else
    （由上一条 + 本文件的三个状态码读数共同兜住）。
    """
    assert autonomy.HTTP_STATUS == {
        autonomy.PRINCIPAL_NOT_ALLOWED: 403,
        autonomy.MODE_NOT_ALLOWED: 422,
        autonomy.DANGER_LIST_REQUIRED: 400,
    }
    with pytest.raises(KeyError):
        autonomy.AutonomyWriteRejected("未映射的 code", code="not_in_the_map")


def test_the_principal_code_is_the_same_string_the_approval_face_uses() -> None:
    """两个 feature 各有一个 `principal_not_allowed` 的载体，所以它们的**串值**必须一致。

    `data-model.md` §4 的强制点表把改档那条也指到审批面那个错误码表，"同一个键名"是文档里的承诺；
    两处各自成文而没有一致性断言的话，漂移的形式是文档说 403 `principal_not_allowed`、
    实际返回 403 `principal_required`，而状态码没错、只有串错了。
    """
    assert autonomy.PRINCIPAL_NOT_ALLOWED == approval_repo.PRINCIPAL_NOT_ALLOWED


# ── 四、HTTP 层不碰 ORM（EN-16 写者分工的端点半边）───────────────────────────


def _attr_stores(source: str) -> set[str]:
    """模块里"写到某个对象属性上"的那些属性名（`x.y = …`）。

    与本仓的 AST 判据同款（见 `test_autonomy_state.py` 的 `_written_attributes`）。
    这里扫的是**整份端点源码**而不是单个函数：HTTP 层合法地一个属性都不写，
    所以空集就是它该有的形状；一旦出现赋值，形式就是"端点自己改 EN-16 的列"。
    """
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }


def test_the_http_layer_writes_no_columns_itself() -> None:
    """端点里 MUST NOT 出现"往对象属性上写值"的语句 ⇒ 所有写入都穿过仓储那一个口子。

    范围声明：这条只断"端点不自己写列"，**不**断"仓储写哪些列"（后者由
    `test_autonomy_state.py` 的 `test_each_new_writer_owns_exactly_its_own_column_set` 钉）。
    正对照在下面一条，注入三种"端点自己改库"的写法，扫描器必须都抓得到。
    """
    assert _attr_stores(API_SOURCE.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize(
    "injected",
    [
        "def f(row):\n    row.mode = 'FULL'\n",
        "def f(row):\n    row.updated_by = 'owner-1'\n",
        "def f(state):\n    state.machine_principals = frozenset()\n",
    ],
)
def test_the_endpoint_scanner_actually_catches_a_self_writing_handler(injected: str) -> None:
    """上面那条的**正对照**：注入的三种"端点自己写"的形态都被抓到（否则空集只是扫描面为空）。"""
    assert _attr_stores(injected) != set(), injected


# ── 五、同一对状态码落在生产装配点上（owner 2026-10-19 裁 W-9 选甲）────────────
#
# 为什么这一节必须在真库档、而不在 `tests/guards/test_realrun_and_machine_principal.py`：
# `_guard_updated_by` 的判定顺序是 `mode` 域检 → **先读 EN-03 的 agent 名单** → 才轮到
# `machine_principals`。所以"配了名单 ⇒ 403"这一发天然带一次 `select`，
# 会话替身只能读到替身自曝（本轮实测）。那边量的是旗标落不落 `app.state`、以及"拒在库读之前"这件事；
# 这一节量的是名单落到生产 app 上之后，那一条禁令**真的开始拦机器身份**。


@pytest.fixture
def assembled_factory(session: Session):
    """把生产装配点绑到本用例那枚回滚会话上（`session_factory` 每次返回同一个会话 ⇒ 看得见未提交的行）。

    与上面的 `make_client()` 差两处，而这两处正是要量的东西：那一份是 `FastAPI()` 手工挂 router、
    手工 `app.state.machine_principals = …`；这一份必须由 `serve.py`/`create_app()` 那条"给了才放"
    的循环把值送进去。`danger_list` 不给是有意的 —— 403 与 503 两档都发生在读清单之前，
    给了反而会把"拒在哪一步"糊掉。
    """

    def build(principals: object | None) -> TestClient:
        app = create_app(session_factory=lambda: session, machine_principals=principals)
        return TestClient(app)

    return build


def test_the_production_app_refuses_a_machine_principal_with_403(
    session: Session, assembled_factory
) -> None:
    """owner 点名的那一发：**带**该参数 ⇒ 那个身份被 403，且拒它的是"名单"而不是"注册表"。

    三条断言各自成立才有意义：
    - 状态码 403、`detail` 点名 `principal_not_allowed`；
    - `detail` 里必须出现"调用方申报的自动主体"那一句 —— 这是 `_guard_updated_by` 的**机器名单**那一支。
      本用例不往 EN-03 注册任何 `agent_id`，所以读到另一支"是 EN-03 注册过的 agent_id"就等于
      这一发是被别的东西拦下的，旗标没起作用；
    - 库里没有多出档位行（禁令是拒，不是"先落一行再告诉你是机器"）。
    """
    client = assembled_factory(MACHINES)
    response = _patch(client, "FULL", principal=AUTOMATED_ACTOR)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), f"detail 是列表 ⇒ 这一发被 pydantic 抢答了，域根本没跑：{detail!r}"
    assert autonomy.PRINCIPAL_NOT_ALLOWED in detail, detail
    assert "调用方申报的自动主体" in detail, f"拒因不是机器名单那一支：{detail}"
    assert _count(session) == 0, "403 之前已经把行落进库了"


def test_the_same_request_without_the_list_is_503_on_the_production_app(
    session: Session, assembled_factory
) -> None:
    """owner 点名的另一半：**不带**该参数 ⇒ 同一发、同一个身份、同一个库，读数变成 503。

    这一条与上一条是**同一个请求的两个形状**，只差 `app.state` 上那一项 —— 所以这一对合起来
    才把"旗标"单独定因。少了这一发，上一发读到的 403 也可以来自别的配置。
    这条就是 W-9 登记的那个事实的可复算版：`serve.py` 没有注入位时，`--as-agent` 打到真服务只读得到 503。
    """
    client = assembled_factory(None)
    response = _patch(client, "FULL", principal=AUTOMATED_ACTOR)
    assert response.status_code == 503, response.text
    assert "machine_principals_not_configured" in response.json()["detail"]
    assert _count(session) == 0


def test_a_human_still_gets_through_on_the_production_app(
    session: Session, assembled_factory
) -> None:
    """反向对照：生产装配点配了名单 ≠ 谁都被 403。

    先埋一行（`_seed_row`，不借端点也不借仓储），再拿同一个 app 打一个人类身份 ⇒ 必须走的是 200。
    这一发同时把 `danger_list` 那个前置条件摘掉了：无建行那一支要清单，而本节故意不注入它，
    因为"拒在哪一步"是这一节要量的东西，多给一项配置会把归因糊掉。
    少了这一发，上面那两条就只是"这条端点对谁都拒"的同义反复 —— 与本文件节 一 同一条道理，
    只是对象从手搓 app 换成了 `create_app()`。
    """
    _seed_row(session)
    client = assembled_factory(MACHINES)
    response = client.patch(
        "/autonomy",
        json={"project_key": PROJECT, "mode": "FULL"},
        headers={api.PRINCIPAL_HEADER: HUMAN},
    )
    assert response.status_code == 200, response.text
    assert _count(session) == 1, "人类那一发没落成行 ⇒ 它没走过名单门，反向对照不成立"
    assert str(_row(session).mode) == "FULL"
