"""T113~T117 这一组的判据文件（FR-059 / FR-060 / FR-061｜R-25，`tasks.md` Phase 10）。

**六节按任务长**（文件名是 T117 那行点名的，不另开第二份，免得"成本记账有没有判据"出现两个出处）：

- 节 一 = T113 的**落库半边不连库也成立**的那部分（前置拒、键的唯一拼法、冲突分流、AST 面）；
- 节 二 = T113 的**真库里**才量得出的部分（幂等、四个键各挡一次、唯一索引在不在且列序对不对）；
- 节 三 = T114 的**写入点**（`ResultIngest` 那一发）与**双维查询**；
- 节 四 = T115 的读价（三口径三样算法、行为式绑定 §6）与"跨口径求和那道墙"；
- 节 五 = T116 的预算比对与 C 类建单路径（**含 owner 裁定那条 `skip`**）；
- 节 六 = T117 的 SC-011 组判据（整链：重复投递、两维同时查得、三口径分列）。

**档位**：节 一与 节 四、五、六里不连库的条目碰不到 PG；其余各条缺 `ACP_DB_DSN` 就 skip（不是绿）。
判据口径四条取自本轮的实测教训：
① **拒发生在一个都没写之前** ⇒ 节 一与 节 五 传进去的 session 是一个"任何属性访问都报错"的哨兵，
   传错对象不是"绕过了库"，而是"根本没碰会话"；
② **两侧要归一** ⇒ 键的拼法只有一处定义，写者与读者各自比对同一份 `DEDUP_COLUMNS`，
   并且这份名单还与迁移 0003、mapper、`data-model.md` 逐字对齐（改一处不改其余就红）；
③ **反向对照不许少** ⇒ "唯一索引报的另一种冲突"（PK 那一发）MUST 原样抛出，
   这条是节 二里唯一一条"不许变绿"的断言：把它放宽成只看 SQLSTATE，节 二立刻红；
④ **期望值不取自被测实现** ⇒ 节 三/节 四 的金额由本文件自己从 §6 那几行读一遍再乘
   （`_documented_rate` 等三个 helper），节 六 的分列形状由 `usage_shape()` 从库里的原始行搭出，
   拿 `pricing.price_of` 或 `ledger` 的返回值当期望值就等于让实现自己批自己的卷。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from decimal import Decimal
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.approval import service
from acp.approval.timeouts import params_ref_default
from acp.bus import acl
from acp.config.loader import DSN_ENV, db_url
from acp.cost import budget, ledger, pricing
from acp.orchestrate.nodes import result_ingest as ri
from acp.persistence import costs, enums
from acp.persistence import models_core, models_registry
from acp.persistence.base import Base
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import CostRecord, RoleSpec, Snapshot
from acp.protocol import messages, refs

SRC_ROOT = Path(costs.__file__).resolve().parent
COSTS_PATH = Path(costs.__file__)
MIGRATIONS = SRC_ROOT / "migrations" / "versions" / "0003_t025_registry.py"
MODELS_REGISTRY = SRC_ROOT / "models_registry.py"
PROJECT = "acp-self-host"
CARD = "T10"
OTHER_CARD = "T11"
AGENT = "qoder"
ROLE = "executor"
NOW = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone.utc)
PRICE = Decimal("0.001200")


def _requires_dsn() -> None:
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG（幂等与索引形态只在库里读得出），整条 skip（不是绿）")


@pytest.fixture
def engine():
    _requires_dsn()
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """外层事务 + 结束回滚（口径同 T105 的拦截判据）：本文件一行都不该留在库里。"""
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


@pytest.fixture
def row_count(session):
    """`cost_record` 的总行数（起点由本文件自己数，不借被测函数）。"""
    return lambda: int(session.scalar(select(func.count()).select_from(CostRecord)) or 0)


def notice(
    *,
    moment: dt.datetime = NOW,
    usage_kind: str = "INPUT",
    quantity: Decimal = Decimal("1200"),
    billing_basis: str = "TOKEN",
    unit: str = "token",
    model_ref: str | None = "dev-model",
) -> refs.UsageNotice:
    return refs.UsageNotice(
        usage_kind=usage_kind,
        quantity=quantity,
        unit=unit,
        billing_basis=billing_basis,
        model_ref=model_ref,
        occurred_at=moment,
    )


def insert(session, **kwargs) -> costs.Write:
    """一条写量的默认形状（少写一个参数就红，所以默认值都放在这一处）。"""
    payload = {
        "project_key": PROJECT,
        "task_card_id": CARD,
        "agent_id": AGENT,
        "role_id": ROLE,
        "notice": notice(),
        "cost_amount": PRICE,
        "currency": "CNY",
    }
    payload.update(kwargs)
    return costs.insert_usage(session, **payload)


class _ExplodingSession:
    """任何属性访问都报错的 session：**证明拒发生在碰会话之前**。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"前置拒不该碰会话，却访问了 {name}()")


# ---------------------------------------------------------------- 一、落库半边的形状与判定（不连库）


def test_every_refusal_happens_before_the_session_is_touched() -> None:
    """协议层管不着的那三样（没时区的时间、`float` 金额、空定位键）一律在碰会话之前拒。

    本模块**不管取值域**（`usage_kind` / `billing_basis` 由 `refs.UsageNotice` 的 `Literal` 挡，
    见 `test_the_domain_gate_is_in_the_protocol_not_beside_it`）⇒ 这里也不留一份重复的域校验。
    """
    bad: list[tuple[str, dict]] = [
        ("occurred_at_naive", {"notice": notice(moment=dt.datetime(2026, 10, 16, 9, 0))}),
        ("amount_shape", {"cost_amount": 0.0012}),
        ("shape", {"task_card_id": "  "}),
        ("shape", {"currency": None}),
        ("shape", {"project_key": ""}),
    ]
    for code, kwargs in bad:
        with pytest.raises(costs.CostMisuse) as caught:
            insert(_ExplodingSession(), **kwargs)
        assert caught.value.code == code, f"{kwargs} 应拒成 {code}，实为 {caught.value.code}"


def test_the_domain_gate_is_in_the_protocol_not_beside_it() -> None:
    """坏取值在**构造通告时**就进不来 ⇒ 成本层不再挡一次（原则 I：一处决定权）。

    这一条替代"成本层自己也校验一遍域"的写法：域值有权威出处 = `refs.UsageKind` / `refs.BillingBasis`
    （`persistence/enums.py` 从同一份 `Literal` 派生 PG 类型），加一道本地校验只会让"改了域而忘了改这里"
    变成一种可能的失效，而不是防住它。
    """
    for kwargs in ({"usage_kind": "TOKENS"}, {"billing_basis": "MIXED"}):
        with pytest.raises(Exception) as caught:
            notice(**kwargs)
        assert caught.type.__name__ in {"ValidationError"}, f"协议层没挡住 {kwargs}：{caught.type}"


def test_the_refusal_messages_carry_no_vendor_free_text_and_no_dsn() -> None:
    """错误消息只带字段名 / 取值 / 域清单（宪法「二」的凭据纪律延伸到异常文本）。"""
    with pytest.raises(costs.CostMisuse) as caught:
        insert(_ExplodingSession(), cost_amount=1.0)
    message = str(caught.value)
    assert DSN_ENV not in message and "://" not in message and "float" in message


def test_dedup_key_treats_two_spellings_of_the_same_instant_as_one_key() -> None:
    """键串要能代表唯一索引的判定 ⇒ 同一瞬间的 `+08:00` 与 `UTC` 写法必须拼出同一个键。

    不归一的后果不是"报错"而是**读起来像两条账**：`dedup_key()` 给出两个串，而 PG 认成一个瞬间。
    """
    east = costs.dedup_key(
        task_card_id=CARD,
        agent_id=AGENT,
        occurred_at=dt.datetime(2026, 10, 16, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        usage_kind="INPUT",
    )
    utc = costs.dedup_key(task_card_id=CARD, agent_id=AGENT, occurred_at=NOW, usage_kind="INPUT")

    assert east == utc, f"同一瞬间拼出两个键：{east} vs {utc}"
    assert len(east) == len(costs.DEDUP_COLUMNS), f"键的元数与名单不符：{east} / {costs.DEDUP_COLUMNS}"
    assert east[0] == CARD and east[1] == AGENT and east[3] == "INPUT"
    assert list(costs.DEDUP_COLUMNS) == ["task_card_id", "agent_id", "occurred_at", "usage_kind"]


def test_the_dedup_column_order_matches_the_migration_verbatim() -> None:
    """`data-model.md:188` 那四列的顺序，与迁移 0003 里 `create_index` 的实参**逐字同序**。

    键顺序变了，索引就变成另一件事（`(agent_id, task_card_id, …)` 挡不住"同卡同刻同种"的重复投递
    被另一个 agent 的名下记账这种形态）⇒ 这条不许靠人比对文档。
    """
    tree = ast.parse(MIGRATIONS.read_text(encoding="utf-8"))
    found: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_index":
            strings = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            if "uq_cost_record_dedup" in strings:
                for arg in node.args:
                    if isinstance(arg, ast.List):
                        found.append([str(item.value) for item in arg.elts])

    assert found == [list(costs.DEDUP_COLUMNS)], f"迁移里的索引形状 = {found}"


def test_the_mapper_and_the_writer_agree_on_the_same_index_name_and_columns() -> None:
    """mapper 的 `Index(...)` 与 `costs.DEDUP_INDEX` / `DEDUP_COLUMNS` 是同一件事的两个说法。

    两侧归一：只改 mapper（或只改本模块常量）都会让"权威是那条唯一索引"这句变成注释。
    """
    tree = ast.parse(MODELS_REGISTRY.read_text(encoding="utf-8"))
    shapes: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Index":
            args = [arg for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            columns = [str(item.value) for item in node.args if isinstance(item, ast.Constant)]
            if args:
                shapes.append((args[0].value, columns[1:]))

    assert (costs.DEDUP_INDEX, list(costs.DEDUP_COLUMNS)) in shapes, f"mapper 里的索引形状 = {shapes}"


def test_the_reader_filters_on_exactly_the_documented_four_columns_in_order() -> None:
    """`find_by_dedup_key()` 比的那四列 = `DEDUP_COLUMNS`，一个不多一个不少。

    读者与写者不同源的话，"读不回那一行"会变成一条永远命中不了的查询。
    """
    tree = ast.parse(COSTS_PATH.read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    compared: list[str] = []
    for node in ast.walk(functions["find_by_dedup_key"]):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
            compared.append(node.left.attr)
    compared = [item for item in compared if item != "id"]

    assert compared == list(costs.DEDUP_COLUMNS), f"读者比的是 {compared}"


def test_the_dedup_branch_never_swallows_a_different_unique_violation() -> None:
    """冲突分流的四种输入各自判一次：只有 `23505` + 我们的索引名才算重复。

    放宽成"只看 SQLSTATE"会把 PK 冲突（同一个 id 投两次 = 调用方缺陷）读成"记过账了"。
    """
    def error(sqlstate: str | None, constraint: str | None, text: str = "") -> IntegrityError:
        orig = SimpleNamespace(sqlstate=sqlstate, diag=SimpleNamespace(constraint_name=constraint), args=(text,))
        return IntegrityError("stmt", {}, orig)

    assert costs._is_dedup_violation(error("23505", costs.DEDUP_INDEX)) is True
    assert costs._is_dedup_violation(error("23505", "pk_cost_record")) is False
    assert costs._is_dedup_violation(error("23502", None)) is False
    assert costs._is_dedup_violation(error(None, None)) is False
    # `diag` 读不到时退回原文，仍然只认我们的索引名：
    assert costs._is_dedup_violation(error("23505", None, text=f'duplicate key value violates unique constraint "{costs.DEDUP_INDEX}"')) is True
    assert costs._is_dedup_violation(error("23505", None, text="duplicate key value violates unique constraint \"pk_cost_record\"")) is False


def test_the_writer_touches_only_the_declared_columns_of_one_table() -> None:
    """AST 两连：`cost_record` 的构造只写名单里的列；本模块**不构造别的表**。

    `COST_WRITABLE_COLUMNS` 是 EN-13 那份列名单的成文处，加列必须一起进名单，
    否则"写者只碰这几列"又是一句没人核对的注释（口径同 T108 的 `SNAPSHOT_OWNED_COLUMNS`）。
    """
    tree = ast.parse(COSTS_PATH.read_text(encoding="utf-8"))
    mappers = {
        name
        for module in (models_core, models_registry)
        for name in dir(module)
        if isinstance(getattr(module, name), type) and issubclass(getattr(module, name), Base)
        and name != "Base"
    }
    constructed: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.keywords:
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name in mappers:
                constructed.setdefault(name, set()).update(kw.arg for kw in node.keywords if kw.arg)

    assert set(constructed) == {"CostRecord"}, f"本模块构造的表不止 EN-13：{sorted(constructed)}"
    columns = constructed["CostRecord"]
    assert columns == set(costs.COST_WRITABLE_COLUMNS), (
        f"构造列与名单不符：多 {columns - set(costs.COST_WRITABLE_COLUMNS)} / "
        f"少 {set(costs.COST_WRITABLE_COLUMNS) - columns}"
    )


def test_the_persistence_layer_issues_no_commit_and_no_second_domain_list() -> None:
    """只 `flush` 不 `commit`（事务边界归调用方）；域取值一律现取 `enums.values_of()`。

    第二条防的是"顺手在成本层抄一份 `INPUT/OUTPUT/…`"：抄的那一份不会跟着 `refs.UsageKind` 变，
    于是域变了而成本层还在按旧域拒（原则 I 的老形态）。
    """
    tree = ast.parse(COSTS_PATH.read_text(encoding="utf-8"))
    calls = [getattr(node.func, "attr", "") for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert "commit" not in calls, f"持久层自己提交了：{calls}"

    values = {"INPUT", "OUTPUT", "CALL", "SEAT_DAY", "TOKEN", "SEAT_CREDIT", "BYOK"}
    copied = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in values
    }
    assert copied == set(), f"成本层抄了一份域：{copied}"


def test_the_usage_parameter_is_the_protocol_shape_not_a_local_copy() -> None:
    """`notice` 参数的标注必须是 `refs.UsageNotice`（EN-13 的唯一入口形状）。

    协议侧刻意不给它 `cost_amount` / `currency`（金额由 ledger 依 params-ref §6 算），
    所以"入口形状"与"落库列"天然差两列 —— 这条钉的是差的那两列**只能从参数进来**，不许工具自报。
    """
    tree = ast.parse(COSTS_PATH.read_text(encoding="utf-8"))
    target = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "insert_usage")
    annotations = {
        arg.arg: ast.unparse(arg.annotation) for arg in target.args.kwonlyargs if arg.annotation
    }

    assert annotations.get("notice") == "refs.UsageNotice", f"实际标注 = {annotations}"
    assert {"cost_amount", "currency"} <= set(annotations), "金额与币种必须是显式参数"


# ---------------------------------------------------------------- 二、真库里的幂等与索引形态


def test_the_documented_unique_index_is_really_there(engine) -> None:
    """`uq_cost_record_dedup` 在库里存在、是 unique、列序 = 文档那一行。

    读的是 **PG 自己的目录**（`sa.inspect`），不是 mapper 的声明：mapper 写了 `unique=True`
    而库里那条不是唯一索引，这件事只有库能回答（判据两侧归一：本模块的常量与库的对象各读一次）。
    """
    _requires_dsn()
    from sqlalchemy import inspect as sa_inspect

    indexes = sa_inspect(engine).get_indexes("cost_record")
    dedup = [item for item in indexes if item["name"] == costs.DEDUP_INDEX]

    assert len(dedup) == 1, f"唯一索引不在库里或不止一条：{indexes}"
    assert dedup[0]["unique"] is True, f"那条索引不是 unique：{dedup[0]}"
    assert list(dedup[0]["column_names"]) == list(costs.DEDUP_COLUMNS), (
        f"库里的列序 = {dedup[0]['column_names']}，与本模块的名单不同源"
    )


def test_first_notice_lands_exactly_one_row_with_every_documented_value(session, row_count) -> None:
    _requires_dsn()
    outcome = insert(session)

    assert outcome.added is True and row_count() == 1
    row = outcome.row
    assert row.project_key == PROJECT and row.task_card_id == CARD and row.agent_id == AGENT
    assert row.role_id == ROLE and row.usage_kind == "INPUT" and row.billing_basis == "TOKEN"
    assert Decimal(str(row.quantity)) == Decimal("1200") and Decimal(str(row.cost_amount)) == PRICE
    assert row.unit == "token" and row.currency == "CNY" and row.model_ref == "dev-model"
    assert outcome.to_line().startswith("COST ADD ::")


def test_an_identical_redelivery_is_dropped_and_does_not_move_the_first_row(session, row_count) -> None:
    _requires_dsn()
    first = insert(session, cost_amount=PRICE, notice=notice(quantity=Decimal("1200")))
    second = insert(session, cost_amount=Decimal("9.99"), notice=notice(quantity=Decimal("99999")))

    assert second.added is False and first.added is True
    assert second.row.id == first.row.id, "重复那一发读回的不是同一行"
    assert row_count() == 1, f"重复投递多落了一行：{row_count()}"
    assert Decimal(str(second.row.cost_amount)) == PRICE and Decimal(str(second.row.quantity)) == Decimal("1200"), (
        "已存在的流水被后到的投递改写了 ⇒ 这不是幂等，是覆盖"
    )
    assert second.to_line().startswith("COST DUP ::")


def test_each_of_the_four_keys_alone_makes_the_next_redelivery_a_new_row(session, row_count) -> None:
    """四个键**各自**都挡一次：任何一个变了就是另一笔账，任何一个不变都不许多落一行。

    只断"整条重投是重复"分不出键的组成 —— 少一个键的索引照样能让这一条绿。
    """
    _requires_dsn()
    variants = {
        "task_card_id": {"task_card_id": OTHER_CARD},
        "agent_id": {"agent_id": "doubao"},
        "occurred_at": {"notice": notice(moment=NOW + dt.timedelta(microseconds=1))},
        "usage_kind": {"notice": notice(usage_kind="OUTPUT", unit="token")},
    }

    for name, kwargs in variants.items():
        assert insert(session).added is True, f"{name} 那一发的基线没落进去"
        assert insert(session, **kwargs).added is True, f"{name} 变了却没被当成新账（键少了它）"
        assert row_count() == 2, f"{name} 这一对没数出两行：{row_count()}"
        session.query(CostRecord).delete()
        session.flush()


def test_two_time_zone_spellings_of_one_instant_land_one_row_in_the_database(session, row_count) -> None:
    """PG 侧也认成一个瞬间：`+08:00` 与 `UTC` 两种写法投递两次 ⇒ 库里只有一行。

    这一条是节 一那条键归一用例的**对侧**：Python 侧相等而库里不等，说明落库把时区丢了；
    反之库里相等而键串不等，说明"是不是重复"有两个答案。
    """
    _requires_dsn()
    east = dt.datetime(2026, 10, 16, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert insert(session, notice=notice(moment=NOW)).added is True

    assert insert(session, notice=notice(moment=east)).added is False
    assert row_count() == 1, f"同一瞬间被记成两笔：{row_count()}"


def test_a_different_unique_violation_propagates_instead_of_becoming_a_duplicate(session, row_count) -> None:
    """同一个 `record_id` 投两张卡 ⇒ PK 冲突原样抛出，**不降级成"这一发是重复"**。

    这条是节 二里唯一"必须红"的断言：把 `_is_dedup_violation()` 放宽成只看 SQLSTATE，它第一个红。
    """
    _requires_dsn()
    shared = uuid.uuid4()
    assert insert(session, record_id=shared).added is True

    with pytest.raises(IntegrityError):
        insert(session, task_card_id=OTHER_CARD, record_id=shared)
    assert row_count() == 1, "PK 冲突那一发不该留下第二行"


def test_the_session_is_still_usable_after_a_swallowed_duplicate(session, row_count) -> None:
    """吞掉重复靠 SAVEPOINT，不是回滚整笔：吞完之后同一会话还能写第二张卡的账。

    口径同 `persistence/snapshots.py` 的"两笔写、一笔事务" —— 外层事务里可能挂着别人的写入，
    一次重复投递赔上整笔事务是错的。
    """
    _requires_dsn()
    assert insert(session).added is True
    assert insert(session).added is False
    assert insert(session, task_card_id=OTHER_CARD).added is True
    assert row_count() == 2


def test_the_read_back_agrees_with_the_writer_and_reports_unknown_keys_as_none(session) -> None:
    _requires_dsn()
    outcome = insert(session)
    found = costs.find_by_dedup_key(
        session, task_card_id=CARD, agent_id=AGENT, occurred_at=NOW, usage_kind="INPUT"
    )
    missing = costs.find_by_dedup_key(
        session, task_card_id="T99", agent_id=AGENT, occurred_at=NOW, usage_kind="INPUT"
    )

    assert found is not None and found.id == outcome.row.id
    assert missing is None, "读面把不存在的键读成了行 ⇒ 四元组比对不成立"


def test_rows_for_task_orders_by_moment_then_the_declared_domain_order(session) -> None:
    """按任务列流水的顺序 = `(瞬间 ↑, usage_kind 按 `refs.UsageKind` 声明顺序 ↑)`。

    **同刻三行才分得出这一条**：只断"查得到"的话，SQL 侧按什么排根本读不出来。
    期望值现取 `enums.values_of("usage_kind")` 的下标而不是在本文件抄一份顺序 —— 与本模块同一个出处，
    改了协议 `Literal` 两侧一起动（口径同 `snapshots.list_points()` 用域下标排拍名）。
    """
    _requires_dsn()
    assert insert(session, notice=notice(usage_kind="CALL", unit="call")).added is True
    assert insert(session, notice=notice(usage_kind="OUTPUT", unit="token")).added is True
    assert insert(session, notice=notice(usage_kind="INPUT", quantity=Decimal("7"), unit="token")).added is True
    assert insert(session, task_card_id=OTHER_CARD).added is True

    listed = costs.rows_for_task(session, task_card_id=CARD)
    declared = list(enums.values_of("usage_kind"))
    expected = sorted(["CALL", "OUTPUT", "INPUT"], key=declared.index)

    assert [str(row.usage_kind) for row in listed] == expected, (
        f"同刻未按声明顺序排：{[str(r.usage_kind) for r in listed]} vs {expected}"
    )
    assert all(row.task_card_id == CARD for row in listed), "别的卡的行混进来了"


def test_an_earlier_moment_sorts_first_even_when_its_kind_declares_later(session) -> None:
    """时间这一维**压过**域名这一维：`SEAT_DAY @ 08:00` 要排在 `INPUT @ 09:00` 前面。

    只测同刻排不出这一条（两维同向时两种实现都给同一个答案）；反向摆一次才分得开"按时间排"与"按域排"。
    """
    _requires_dsn()
    early = NOW - dt.timedelta(hours=1)
    assert insert(session, notice=notice(moment=early, usage_kind="SEAT_DAY", unit="seat-day")).added is True
    assert insert(session, notice=notice(usage_kind="INPUT", unit="token")).added is True

    listed = costs.rows_for_task(session, task_card_id=CARD)

    assert [str(row.usage_kind) for row in listed] == ["SEAT_DAY", "INPUT"], (
        f"时间没压过域名：{[str(r.usage_kind) for r in listed]}"
    )


# ---------------------------------------------------------------- 三、写入点与双维查询（T114）

ROLE_SPEC_ID = "cost-probe-role"
OTHER_AGENT = "workbuddy"
UNPRICED = "本章没有这个模型"


def cheapest_model() -> str:
    """§6 的 Token 表里输入单价最低的那一行 —— 模型名**不写死在判据里**。

    两个理由：① `params-ref §7` 把模型名的点号写法列进了禁止清单（连字符形态才是官方 ID），
    判据里照 §6 的显示名抄一份就会被那道静态扫描命中（本轮实测：门禁确实红了，这条就是它的产物）；
    ② 模型名是**外部市场事实**，§6 改了判据要跟着改，而"跟着文档变"本来就是 T115 要证的性质 ⇒ 从表里现取。
    """
    table = pricing.parse_rates()
    return min(table.token, key=lambda label: table.token[label]["INPUT"])


def priced(**kwargs) -> refs.UsageNotice:
    """一条 §6 读得出单价的通告（默认 `TOKEN` / 最低单价那一行 / 1000 INPUT）。"""
    values: dict[str, object] = {"model_ref": cheapest_model(), "quantity": Decimal("1000")}
    values.update(kwargs)
    return notice(**values)


def result_of(items: list[refs.UsageNotice], *, task_id: str = CARD) -> messages.TaskResult:
    return messages.TaskResult(
        task_id=task_id,
        self_reported_status="success",
        artifacts=[messages.ArtifactRef(rel_path="index.html", bytes=600, lines=30, format="html")],
        log_excerpt="built 1 file",
        self_assessment="已完成",
        issues=[],
        usage=items,
    )


@pytest.fixture
def seeded(session) -> tuple[FeatureRequest, TaskCard]:
    """一支在册工具 + 一个岗位 + 一条需求单 + 一张 `RUNNING` 且已填执行者与执行岗的卡。

    形状照 `test_artifact_gate.py` 的同名 fixture（`task_card` 与 `snapshot` 之间没有
    `relationship()`，UoW 不按表间外键排 INSERT ⇒ 中间那次 `flush()` 不是顺手写的）。
    """
    session.add(
        RoleSpec(
            role_id=ROLE_SPEC_ID,
            name="成本探针岗",
            charter="只为本文件的写入点存在",
            read_only=False,
            builtin=False,
            mounted_agents=[AGENT],
        )
    )
    session.add(
        AgentUnit(
            agent_id=AGENT,
            form="SDK",
            tier=2,
            cost_band="MID",
            max_parallel=2,
            current_load=1,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{AGENT}-workspace",
            enabled=True,
        )
    )
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key=PROJECT,
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
            task_card_ids=[CARD],
            ref=f"local:cost-probe-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    card = TaskCard(
        id=CARD,
        feature_request_id=request.id,
        title="卡 T10",
        difficulty=2,
        skill_tags=["html"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="RUNNING",
        snapshot_id=snapshot_id,
        executor_agent_id=AGENT,
        executor_role_id=ROLE_SPEC_ID,
    )
    session.add(card)
    session.flush()
    return request, card


def _documented_rate(label: str, column: int) -> Decimal:
    """本测试**自己**从 §6 取"每百万"单价（`column`：1 = 输入列、2 = 输出列）。

    刻意不复用 `pricing` 的正则与 `_cells()`：拿被测实现的常量去验它自己，红了也不知道是谁错。
    """
    for _, line in pricing.section_six_lines(params_ref_default().read_text(encoding="utf-8")):
        cells = [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == label:
            return Decimal(cells[column].split("元")[0].strip())
    raise AssertionError(f"§6 里读不到 {label!r} 这一行")


def test_the_write_point_lands_one_row_per_notice_in_the_callers_transaction(session, row_count, seeded) -> None:
    """FR-059「每次调用 MUST 记账」在**回收这一发**上的读法：几条通告 ⇒ 几行 EN-13，一条不漏。"""
    _requires_dsn()
    request, card = seeded
    items = [priced(usage_kind="INPUT"), priced(usage_kind="OUTPUT", quantity=Decimal("500"))]

    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of(items),
        actor_agent_id=AGENT,
        created_at=NOW,
    )
    rows = session.scalars(select(CostRecord).where(CostRecord.task_card_id == CARD)).all()

    assert ingested.usage is not None and ingested.usage.rows_added == 2, ingested
    assert ingested.count == 1, "产物那半边被记账这一发带跑了"
    assert len(rows) == 2 and row_count() == 2, f"库里行数与申报不符：{len(rows)} / {row_count()}"
    for row in rows:
        assert (row.project_key, row.task_card_id, row.agent_id, row.role_id) == (
            request.project_key,
            CARD,
            card.executor_agent_id,
            card.executor_role_id,
        ), f"定位四列没有全部从库里那两行推出来：{row}"
        assert row.currency == pricing.CURRENCY and Decimal(str(row.quantity)) in (
            Decimal("1000"),
            Decimal("500"),
        ), row


def test_the_recorded_amount_is_the_documented_unit_price_not_a_reported_one(session, seeded) -> None:
    """金额是**按 §6 算出来的**：期望值由本测试独立从 §6 那一行取单价再乘数量。

    协议侧的 `UsageNotice` 没有 `cost_amount` 可报（`extra="forbid"`），这条把"工具自报金额"这条路
    从结构上钉死；对照的是 `params-ref §6` 的原文，不是 `pricing` 的返回值。
    """
    _requires_dsn()
    request, _ = seeded
    model = cheapest_model()
    input_notice = priced(usage_kind="INPUT", quantity=Decimal("1000"))
    output_notice = priced(usage_kind="OUTPUT", quantity=Decimal("500"))
    expected_input = Decimal("1000") / Decimal(1_000_000) * _documented_rate(model, 1)
    expected_output = Decimal("500") / Decimal(1_000_000) * _documented_rate(model, 2)
    assert expected_input == expected_input.quantize(pricing.AMOUNT_EXPONENT), (
        "这一批数量落在舍入规则上了 ⇒ 本条的期望算法要换成与 §6 同规则的写法"
    )
    assert expected_output == expected_output.quantize(pricing.AMOUNT_EXPONENT), (
        "这一批数量落在舍入规则上了 ⇒ 本条的期望算法要换成与 §6 同规则的写法"
    )

    ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of([input_notice, output_notice]),
        actor_agent_id=AGENT,
        created_at=NOW,
    )
    rows = {str(row.usage_kind): row for row in session.scalars(select(CostRecord)).all()}

    assert set(rows) == {"INPUT", "OUTPUT"}
    assert Decimal(str(rows["INPUT"].cost_amount)) == expected_input, rows["INPUT"].cost_amount
    assert Decimal(str(rows["OUTPUT"].cost_amount)) == expected_output, rows["OUTPUT"].cost_amount
    assert {"cost_amount", "currency"}.isdisjoint(refs.UsageNotice.model_fields), "通告里出现了金额列"


def test_a_redelivery_of_the_same_result_adds_no_second_cost_row(session, row_count, seeded) -> None:
    """**核心路径**：同一发结果投两次，EN-06 会长第二份（既有行为），但 EN-13 一行都不多。

    这条是 T117 点名的第一条组判据接到写入点上的版本 —— 幂等不在 ledger 里实现，它靠的是
    `uq_cost_record_dedup`，所以这里 MUST 用真库真索引读，而不是单测 `insert_usage` 的返回值。
    """
    _requires_dsn()
    request, _ = seeded
    items = [priced(usage_kind="INPUT"), priced(usage_kind="OUTPUT", quantity=Decimal("500"))]
    call = lambda: ri.record_result(  # noqa: E731
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of(items),
        actor_agent_id=AGENT,
        created_at=NOW,
    )

    first = call()
    baseline = row_count()
    second = call()
    artifacts = session.scalars(select(Artifact)).all()

    assert baseline == 2 and first.usage.rows_added == 2, first.usage.to_line()
    assert second.usage.rows_added == 0 and second.usage.duplicates == 2, second.usage.to_line()
    assert row_count() == baseline, "重复投递多入账了"
    assert len(artifacts) == 2, f"产物那半边的既有语义被改动：{len(artifacts)} 行"


def test_the_write_point_takes_its_locating_columns_from_the_database_not_the_caller(seeded) -> None:
    """`record_result()` 的参数表里没有 `project_key` / `role_id` / `cost_amount` —— 调用方交不出这些。

    形状对照：定位列来自 `feature_request` 与 `task_card` 两行，金额来自 §6。
    """
    parameters = set(signature(ri.record_result).parameters)
    assert {"project_key", "role_id", "cost_amount", "currency", "billing_basis"}.isdisjoint(parameters), (
        f"写入点开了口子让调用方自己报账：{sorted(parameters)}"
    )


def test_a_card_without_an_executor_role_records_the_declared_placeholder(session, row_count, seeded) -> None:
    """`executor_role_id` 为 NULL（T-14 裁决后的合法形态）时，账上写的是既有的"未指派"代号。

    反向对照：这里不写代号而是留 NULL，`cost_record.role_id` 的 NOT NULL 会当场把这一发挡死 ——
    所以本条同时在钉"回收不能因为岗位没定就不记账"。
    """
    _requires_dsn()
    request, card = seeded
    card.executor_role_id = None
    session.flush()

    ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of([priced()]),
        actor_agent_id=AGENT,
        created_at=NOW,
    )
    row = session.scalars(select(CostRecord)).one()

    assert row.role_id == acl.NO_ROLE_DECLARED, row.role_id
    assert row_count() == 1


def test_an_empty_usage_report_stores_nothing_and_reports_none(session, row_count, seeded) -> None:
    """`usage=[]` ⇒ 不调用记账、`Ingested.usage` 为 `None`。

    这条是"没用量"与"记了 0 行"的分界：把 `None` 放宽成空 `LedgerRun`，报表就把"工具没报用量"
    读成"这次真的没花钱"（Qoder 体验版全 0 那一发正是这个形状，见 `research.md` D-1 第九轮）。
    """
    _requires_dsn()
    request, _ = seeded

    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of([]),
        actor_agent_id=AGENT,
        created_at=NOW,
    )

    assert ingested.usage is None
    assert ingested.count == 1 and row_count() == 0


def test_an_unpriced_notice_fails_the_ingest_and_leaves_the_earlier_rows_in_the_transaction(session, row_count, seeded) -> None:
    """§6 里没有的模型名 ⇒ `PriceUndetermined` 原样抛出，**不降级成"跳过这一笔"**。

    后半句钉的是现状：这一发之前已经写的行留在**调用方的事务**里（本模块不自开事务边界），
    调用方回滚就一起没了。将来要改成"整发原子"得先动这条，不是悄悄改实现。
    """
    _requires_dsn()
    request, _ = seeded
    with pytest.raises(pricing.PriceUndetermined) as caught:
        ri.record_result(
            session,
            feature_request_id=request.id,
            task_id=CARD,
            result=result_of([priced(), priced(model_ref=UNPRICED)]),
            actor_agent_id=AGENT,
            created_at=NOW,
        )

    assert caught.value.code == "model_row_missing", caught.value.code
    assert row_count() == 1, "要么全记要么全不记，但现在钉的是「留在调用方事务里」这一口"


def test_the_write_point_opens_no_transaction_of_its_own() -> None:
    """AST：`result_ingest.py` 里不许出现 `commit` / `rollback` / `begin`（owner 对 T114 的预先裁决 2、3）。

    只 `flush` 是既有语义，`begin_nested` 属 `costs.insert_usage` 的 SAVEPOINT，不该出现在这里 ——
    出现了就等于写入点自己划事务边界，与裁决 1"在现有事务内调"冲突。
    """
    tree = ast.parse(Path(ri.__file__).read_text(encoding="utf-8"))
    calls = [getattr(node.func, "attr", "") for node in ast.walk(tree) if isinstance(node, ast.Call)]
    forbidden = {"commit", "rollback", "begin", "begin_nested"}

    assert forbidden.isdisjoint(calls), f"写入点自己开了事务边界：{sorted(forbidden & set(calls))}"
    assert "flush" in calls, "连既有的 flush 都没了 ⇒ 本文件的对照基准变了"


def test_by_agent_and_by_task_are_two_views_that_keep_the_bases_apart(session, row_count) -> None:
    """FR-059 的双维查询 + FR-061 的分列：同一批行，按工具查与按任务查**行数对得上、口径不合并**。

    两份小计故意做成不同数量级（TOKEN 0.006 元 / SEAT_CREDIT 13.2 元）：两数相等时"少减一个维度"
    这种错读不出来，所以两侧的值必须互不相同。
    """
    _requires_dsn()
    session.add(
        AgentUnit(
            agent_id=OTHER_AGENT,
            form="API",
            tier=1,
            cost_band="CHEAP",
            max_parallel=1,
            current_load=0,
            historical_success_rate=None,
            success_rate_sample=0,
            workspace_dir=f"/tmp/{OTHER_AGENT}-workspace",
            enabled=True,
        )
    )
    run = ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(usage_kind="INPUT")],
    )
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=OTHER_AGENT,
        role_id=ROLE,
        notices=[
            priced(
                usage_kind="SEAT_DAY",
                unit="seat-day",
                quantity=Decimal("2"),
                billing_basis="SEAT_CREDIT",
                model_ref=None,
            )
        ],
    )
    assert run.rows_added == 1

    by_agent = [ledger.by_agent(session, agent_id=name) for name in (AGENT, OTHER_AGENT)]
    by_task = ledger.by_task(session, task_card_id=CARD)
    rows_of = lambda view: sum(item.rows for item in view.subtotals.values())  # noqa: E731

    assert [rows_of(view) for view in by_agent] == [1, 1], [view.to_line() for view in by_agent]
    assert rows_of(by_task) == 2 == row_count(), "两维读到的行数与库里对不上"
    assert by_agent[0].bases() == ("TOKEN",) and by_agent[1].bases() == ("SEAT_CREDIT",)
    assert by_task.bases() == ("TOKEN", "SEAT_CREDIT"), f"同一张卡的两口径被并了：{by_task.to_line()}"
    assert {name: item.rows for name, item in by_task.subtotals.items()} == {"TOKEN": 1, "SEAT_CREDIT": 1}
    assert by_task.subtotal("TOKEN").cost_amount == by_agent[0].subtotal("TOKEN").cost_amount, (
        "两维读到的是同一批行，小计却不一样"
    )
    assert str(by_task.subtotal("TOKEN").cost_amount) != str(by_task.subtotal("SEAT_CREDIT").cost_amount), (
        "两口径金额相等 ⇒ 少减一维这类错读不出来，本条失去区分力"
    )
    assert by_agent[1].subtotal("TOKEN").rows == 0, "这一维没有行却读出了行"
    assert "不可相加" in by_agent[0].to_line(), by_agent[0].to_line()
    assert {view.dimension for view in by_agent} == {"agent"} and by_task.dimension == "task"


# ---------------------------------------------------------------- 四、读价、三口径与"合计"那道墙（T115）

MERGE_SENTENCE = "三者不可合并为单一数字"


def ref_text() -> str:
    return params_ref_default().read_text(encoding="utf-8")


def _row_line(label: str) -> str:
    """§6 里那个标签所在行的**原文**（当作定点替换的锚点：整行唯一，不会撞到别处）。"""
    for _, line in pricing.section_six_lines(ref_text()):
        cells = [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == label:
            return line
    raise AssertionError(f"§6 里读不到 {label!r} 那一行 ⇒ 判据的锚点与文档不符")


def raise_input_price(label: str, to: str) -> str:
    """把那一行的**输入单价**改成 `to`，返回改过的 §6 全文（其余行一字不动）。"""
    line = _row_line(label)
    bumped = line.replace(f"{_documented_rate(label, 1)} 元", f"{to} 元", 1)
    assert bumped != line, f"{label!r} 那一行的输入单价没被改动：{line}"
    return patched((line, bumped))


def patched(*edits: tuple[str, str]) -> str:
    """在 §6 原文上做**定点**替换（每处只换第一次出现，其余照旧）。

    先断言注入点还在：§6 被改写过形而替换静失配时，本文件的行为式绑定会退化成"什么都没改"的假绿。
    """
    text = ref_text()
    for old, new in edits:
        assert text.count(old) >= 1, f"§6 里找不到注入点 {old!r} ⇒ 本条判据的锚点过期，先修判据"
        text = text.replace(old, new, 1)
    return text


def _documented_seat_rate() -> Decimal:
    """本测试自己从"日均坐席成本"那一行取第一个数。"""
    for _, line in pricing.section_six_lines(ref_text()):
        cells = [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]
        if cells and "日均坐席成本" in cells[0]:
            digits = [part for part in cells[1].replace("，", " ").split() if any(ch.isdigit() for ch in part)]
            return Decimal(digits[0])
    raise AssertionError("§6 里没有「日均坐席成本」那一行")


def _documented_credit_rate() -> Decimal:
    """本测试自己从"加量包"那一行的第一组 `N Credits = M 元` 取每 Credit 单价。"""
    for _, line in pricing.section_six_lines(ref_text()):
        cells = [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "加量包":
            group = cells[1].split("；")[0]
            left, right = group.split("=")
            credits = Decimal(left.replace(",", "").split()[0])
            money = Decimal(right.replace(",", "").split()[0])
            return money / credits
    raise AssertionError("§6 里没有「加量包」那一行")


def test_every_read_number_points_at_the_document_line_it_claims() -> None:
    """`source` 不是装饰：它报出的行号那一行，必须真的写着这个数所在的标签。

    反向对照在最后两条 —— 行号指向的**不是** BYOK / 加量包那一行时报错也拦不住，所以这里核对的是
    "那一行的首列长什么样"，而不是"行号在文件范围内"。
    """
    table = pricing.parse_rates()
    lines = dict(pricing.section_six_lines(ref_text()))

    for label, source in table.token_source.items():
        number = int(source.rsplit(":", 1)[1])
        assert label in lines[number], f"{label!r} 的出处 {source} 指向的不是它那一行"
    seat_number = int(table.seat_day[1].rsplit(":", 1)[1])
    assert "日均坐席成本" in lines[seat_number], lines[seat_number]
    credit_number = int(table.credit[1].rsplit(":", 1)[1])
    assert lines[credit_number].strip().strip("|").split("|")[0].strip() == "加量包", lines[credit_number]
    byok_number = int(table.byok[1].rsplit(":", 1)[1])
    assert lines[byok_number].strip().strip("|").split("|")[0].strip() == "BYOK", lines[byok_number]


def test_each_basis_has_its_own_rule_and_the_three_never_share_a_formula() -> None:
    """三口径三样算法，且**各自**带着自己的出处（`source` 四行互不相同）。

    期望值不取自 `pricing`：Token 用本文件的 `_documented_rate`，坐席与 Credit 用
    `_documented_seat_rate` / `_documented_credit_rate` 各自独立地从 §6 读一遍再乘。
    """
    model = cheapest_model()
    token = pricing.price_of(priced(usage_kind="INPUT", quantity=Decimal("1000000")))
    seat = pricing.price_of(
        priced(usage_kind="SEAT_DAY", unit="seat-day", quantity=Decimal("2"), billing_basis="SEAT_CREDIT", model_ref=None)
    )
    credit = pricing.price_of(
        priced(usage_kind="CALL", unit="credit", quantity=Decimal("37"), billing_basis="SEAT_CREDIT", model_ref=None)
    )
    byok = pricing.price_of(priced(usage_kind="INPUT", quantity=Decimal("1000000"), billing_basis="BYOK"))

    assert token.cost_amount == (_documented_rate(model, 1) * Decimal("1")).quantize(pricing.AMOUNT_EXPONENT), token
    assert seat.cost_amount == (Decimal("2") * _documented_seat_rate()).quantize(pricing.AMOUNT_EXPONENT), seat
    assert credit.cost_amount == (Decimal("37") * _documented_credit_rate()).quantize(pricing.AMOUNT_EXPONENT), credit
    assert byok.cost_amount == Decimal("0.000000"), "BYOK 按 §6 的定义不进平台账 ⇒ 恒 0，且必须带出处"
    assert len({token.source, seat.source, credit.source, byok.source}) == 4, (token, seat, credit, byok)
    assert {token.basis, seat.basis, credit.basis, byok.basis} == set(enums.values_of("billing_basis"))
    assert {row.currency for row in (token, seat, credit, byok)} == {pricing.CURRENCY}


def test_a_price_changed_in_the_document_changes_the_number_computed_here() -> None:
    """**行为式绑定**（口径同 T109 给 §10 的那条）：把那一行的输入单价改成 7 ⇒ 算出的金额跟着变。

    这一条是"金额不进代码"的真正证明：抄了一份价目的实现不会因为它读的那份文档变了而变。
    对侧同测：同一份原文再算一次仍是原来那个数 —— 变的只有注入过的那一份。
    """
    model = cheapest_model()
    base = pricing.price_of(priced(usage_kind="INPUT", quantity=Decimal("1000000")))
    table = pricing.parse_rates(text=raise_input_price(model, "7"))
    moved = pricing.price_of(priced(usage_kind="INPUT", quantity=Decimal("1000000")), table=table)
    again = pricing.parse_rates(text=ref_text())

    assert moved.cost_amount == Decimal("7.000000"), moved
    assert moved.cost_amount != base.cost_amount, "改了文档里的价，算出的金额没动 ⇒ 价目被抄进了代码"
    assert pricing.price_of(
        priced(usage_kind="INPUT", quantity=Decimal("1000000")), table=again
    ).cost_amount == base.cost_amount, "原文那份被污染了"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"unit": "seat-day"}, "unit_basis_mismatch"),
        ({"usage_kind": "CALL"}, "no_rate_for_kind"),
        ({"model_ref": None}, "model_ref_missing"),
        ({"model_ref": UNPRICED}, "model_row_missing"),
    ],
    ids=["token_basis_with_seat_unit", "token_basis_with_call_kind", "token_basis_without_model_ref", "unknown_model_ref"],
)
def test_an_unpriceable_notice_refuses_instead_of_guessing(kwargs: dict, code: str) -> None:
    """四条"读不出单价"的形状各自报对代号 —— 全都发生在写库之前（本条根本不碰 session）。

    `SEAT_CREDIT` 走错形状那一支由最后一条独立测：把口径换成它、形状留给 `TOKEN`，代号也是
    `unit_basis_mismatch`，但**理由不是同一个**（一个缺单价、一个单位与口径不配）。
    """
    with pytest.raises(pricing.PriceUndetermined) as caught:
        pricing.price_of(priced(**kwargs))

    assert caught.value.code == code, caught.value.code


def test_the_seat_basis_rejects_the_token_shaped_notice_too() -> None:
    """反向对照的另一半：`SEAT_CREDIT` 只认两种 (kind, unit) 形状，第三种报错而不是退化成 0。"""
    with pytest.raises(pricing.PriceUndetermined) as caught:
        pricing.price_of(priced(billing_basis="SEAT_CREDIT", model_ref=None))

    assert caught.value.code == "unit_basis_mismatch", caught.value.code


def test_the_document_s_own_prohibition_is_part_of_the_reading() -> None:
    """§6 里那句「不可合并」被改写 ⇒ 整个读价拒绝，而不是"照算但少一句提示"。

    这条把 FR-061 的**权威句**接进了代码：谁把那一句从文档里删掉或改成"可以合计"，价目就读不出来，
    成本账当场停（`code = merge_rule_missing`）。锚点被改名同理（`section_missing`）。
    """
    with pytest.raises(pricing.PriceUndetermined) as stripped:
        pricing.parse_rates(text=patched((MERGE_SENTENCE, "三者合计成平台成本")))
    with pytest.raises(pricing.PriceUndetermined) as renamed:
        pricing.parse_rates(text=ref_text().replace(pricing.SECTION_ANCHOR, "## 6. 价目", 1))

    assert stripped.value.code == "merge_rule_missing", stripped.value.code
    assert renamed.value.code == "section_missing", renamed.value.code


def test_the_rounding_rule_is_half_up_at_six_decimal_places() -> None:
    """§6 只给单价不给精度 ⇒ 舍入是本模块**唯一**自定的规则，单独钉住它。

    价目用一份**手搭的探针表**（不读 §6）把形状凑准：`0.2 元/百万 × 2.5 token = 0.0000005` 恰好落在第 7 位，
    `ROUND_HALF_UP` ⇒ `0.000001`，而 `Decimal` 的默认档（HALF_EVEN）⇒ `0.000000`。这一发分得开两种写法；
    哪天改成默认档，同一批发第二次就会差一分，幂等那半边被"值不同但键相同"的噪声掩盖。
    """
    probe = pricing.RateTable(
        token={"成本探针价目": {"INPUT": Decimal("0.2"), "OUTPUT": Decimal("30")}},
        token_source={"成本探针价目": "手搭探针（不读 §6，本条只验舍入）"},
        seat_day=(Decimal("6.6"), "手搭探针"),
        credit=(Decimal("0.05"), "手搭探针"),
        byok=(Decimal("0"), "手搭探针"),
    )
    first = pricing.price_of(priced(model_ref="成本探针价目", usage_kind="INPUT", quantity=Decimal("2.5")), table=probe)
    second = pricing.price_of(priced(model_ref="成本探针价目", usage_kind="INPUT", quantity=Decimal("2.5")), table=probe)

    assert first.cost_amount == Decimal("0.000001"), first.cost_amount
    assert first.cost_amount == second.cost_amount, "同一批用量算两次给出不同金额 ⇒ 幂等键之外多了一份噪声"
    assert first.cost_amount.as_tuple().exponent == -6
    wide = pricing.price_of(priced(usage_kind="OUTPUT", quantity=Decimal("1")))
    assert wide.cost_amount.as_tuple().exponent == -6, wide.cost_amount


def test_pricing_branches_only_on_declared_domain_values() -> None:
    """AST：`pricing.py` 里出现的域字面量，必须**正好**是 `refs` 那两域的全体（不多不少）。

    多了 = 自造口径；少了 = 域扩了而算法没跟上，这一支会掉进"没有算法"的拒里而没人发现。
    期望值现取 `enums.values_of()`，不写死。
    """
    tree = ast.parse(Path(pricing.__file__).read_text(encoding="utf-8"))
    kinds = set(enums.values_of("usage_kind"))
    bases = set(enums.values_of("billing_basis"))
    literals = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert literals & kinds == kinds, f"usage_kind 的分支不齐：缺 {kinds - literals}"
    assert literals & bases == bases, f"billing_basis 的分支不齐：缺 {bases - literals}"


def test_a_cross_basis_sum_refuses_and_there_is_no_second_door_to_a_number() -> None:
    """**核心路径**：`total()` 报错且不返回任何数字；分列对象上除白名单外没有别的公开入口。

    单口径那份也照样报错 —— 否则"先只记一种口径、以后再加"会养成"total 是能用的"这个习惯。
    白名单那条断言防的是：以后有人给 `Breakdown` 加一个 `grand_total()` 把这道墙绕过去。
    """
    peek = {
        basis: ledger.Subtotal(basis=basis, cost_amount=Decimal(value), currency=pricing.CURRENCY, rows=1, usage={})
        for basis, value in (("TOKEN", "0.006000"), ("SEAT_CREDIT", "13.200000"))
    }
    view = ledger.Breakdown(dimension="task", subject=CARD, subtotals=peek)
    single = ledger.Breakdown(dimension="task", subject=CARD, subtotals={"TOKEN": peek["TOKEN"]})

    with pytest.raises(ledger.CrossBasisSumRefused) as caught:
        view.total()
    with pytest.raises(ledger.CrossBasisSumRefused):
        single.total()

    assert caught.value.bases == ("TOKEN", "SEAT_CREDIT"), caught.value.bases
    assert "不可合并" in str(caught.value), str(caught.value)
    doors = {name for name in dir(view) if not name.startswith("_")}
    assert doors == {"bases", "subtotal", "to_line", "total", "dimension", "subject", "subtotals"}, (
        f"分列对象上多出了一个能读出合计数的入口：{sorted(doors)}"
    )


def test_the_ledger_refuses_a_basis_name_that_is_not_in_the_domain() -> None:
    """`subtotal()` 对域外的口径名报错，不返回零小计 —— 拼错名字不该读成"这一口径没花钱"。"""
    view = ledger.Breakdown(dimension="task", subject=CARD, subtotals={})

    with pytest.raises(ledger.LedgerMisuse) as caught:
        view.subtotal("TOKENS")

    assert caught.value.code == "basis_domain"
    assert view.subtotal("TOKEN").rows == 0, "域内的空口径应当读成 0 行，而不是报错"


# ---------------------------------------------------------------- 五、预算阈值与 C 类建单路径（T116）

PLAN = Path(__file__).resolve().parents[2] / "specs" / "001-multi-agent-collab-platform" / "plan.md"
C_SKIP_REASON = "MVP-1 默认不启用 C 类卡点，启用归 Phase 12"


def budget_check(session, request, card) -> budget.BudgetVerdict:
    return budget.check(session, feature_request_id=request.id, task_card_id=card.id)


def add_card(session, request, *, card_id: str, snapshot_id: uuid.UUID, budget_value) -> TaskCard:
    card = TaskCard(
        id=card_id,
        feature_request_id=request.id,
        title=f"卡 {card_id}",
        difficulty=2,
        skill_tags=["html"],
        depends_on=[],
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="RUNNING",
        snapshot_id=snapshot_id,
        executor_agent_id=AGENT,
        executor_role_id=ROLE_SPEC_ID,
        token_budget=budget_value,
    )
    session.add(card)
    session.flush()
    return card


def test_the_threshold_is_the_documented_upper_bound_itself(session, seeded) -> None:
    """`used >= token_budget` ⇒ 达阈值：等于上限那一刻就算到达，不等"超过"。

    两侧各测一次（1400 vs 1500 未达、1600 vs 1500 已达），因为只测一侧时"写成 `>`"或"写成 `<=`"
    都能绿一条。第二发用晚一分钟的瞬间：同刻同 `usage_kind` 会被幂等键吃掉，那就量不到阈值了。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("1500")
    session.flush()
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(usage_kind="INPUT", quantity=Decimal("1000")), priced(usage_kind="OUTPUT", quantity=Decimal("400"))],
    )

    below = budget_check(session, request, card)
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(usage_kind="INPUT", quantity=Decimal("200"), moment=NOW + dt.timedelta(minutes=1))],
    )
    reached = budget_check(session, request, card)

    assert (below.used, below.reached) == (Decimal("1400"), False), below.to_line()
    assert (reached.used, reached.reached) == (Decimal("1600"), True), reached.to_line()
    assert reached.budget == Decimal("1500")


def test_the_compared_quantity_is_a_token_sum_never_an_amount_sum(session, seeded) -> None:
    """**反向对照**：一张预算 10 token 的卡，记一笔 13.2 元的坐席用量 ⇒ 未达阈值。

    这条专门用来区分"按数量比"与"按金额比"：读了 `cost_amount` 的实现在这里必然红。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("10")
    session.flush()
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[
            priced(
                usage_kind="SEAT_DAY",
                unit="seat-day",
                quantity=Decimal("2"),
                billing_basis="SEAT_CREDIT",
                model_ref=None,
            )
        ],
    )

    verdict = budget_check(session, request, card)

    assert verdict.used == Decimal("0"), verdict.to_line()
    assert verdict.reached is False, "把金额当用量比了 ⇒ 13.2 元 > 10 token"
    assert str(session.scalars(select(CostRecord).where(CostRecord.task_card_id == CARD)).one().cost_amount) == "13.200000"


def test_a_null_budget_is_no_limit_and_a_positive_one_is_a_limit(session, seeded) -> None:
    """裁决 3 的两面：`NULL` = 无预算（不是 0、不是"已超"），非 NULL 才比。

    `CHECK (token_budget > 0)` 由库里那条约束负责，本条不重复验它，只验读法。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = None
    session.flush()
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(quantity=Decimal("1000000"))],
    )

    unlimited = budget_check(session, request, card)
    card.token_budget = Decimal("1")
    session.flush()
    limited = budget_check(session, request, card)

    assert (unlimited.budget, unlimited.reached) == (None, False), unlimited.to_line()
    assert "裁决 3" in unlimited.note, unlimited.note
    assert (limited.budget, limited.reached) == (Decimal("1"), True), limited.to_line()


def test_the_card_is_located_by_the_composite_key_not_by_its_id_alone(session, seeded) -> None:
    """同名卡分属两支需求单时，预算各读各的那一张（owner 裁决 2 的复合主键在成本面同样生效）。

    **已登记的限制**：`used` 这一侧读不出来 —— EN-13 没有 `feature_request_id` 列，两张同名卡的
    token 数会被加在一起。所以这里只断言"上限来自各自那张卡"，并把 `used` 的现读值一并写进断言，
    等 owner 裁完那半边的修法语（动列 = 动迁移 = 硬停项）再翻这一条。
    """
    _requires_dsn()
    first_request, first_card = seeded
    first_card.token_budget = Decimal("1000")
    second_request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="other-project",
        raw_input="另一支需求单",
        summary_digest={"自行补全项": [], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(second_request)
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=2,
            phase="PRE_WORK",
            task_card_ids=[CARD],
            ref=f"local:cost-probe-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    add_card(session, second_request, card_id=CARD, snapshot_id=snapshot_id, budget_value=Decimal("9000"))
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(quantity=Decimal("1200"))],
    )

    on_first = budget.check(session, feature_request_id=first_request.id, task_card_id=CARD)
    on_second = budget.check(session, feature_request_id=second_request.id, task_card_id=CARD)

    assert (on_first.budget, on_second.budget) == (Decimal("1000"), Decimal("9000"))
    assert (on_first.reached, on_second.reached) == (True, False)
    assert on_first.used == on_second.used == Decimal("1200"), "两支需求单的同名卡被读成两个数 ⇒ 上面那条限制说明过期了"


def test_a_missing_card_is_refused_instead_of_read_as_unlimited(session, seeded) -> None:
    """比对没有对象 ⇒ 报错，不返回"未达阈值"。"""
    _requires_dsn()
    request, _ = seeded

    with pytest.raises(budget.BudgetMisuse) as caught:
        budget.check(session, feature_request_id=request.id, task_card_id="T99")

    assert caught.value.code == "card_missing", caught.value.code


def test_the_over_budget_payload_is_five_elements_with_the_three_disposals(session, seeded) -> None:
    """payload 过 `service.validate_payload`（与 A/D 同一个校验入口），三处置项逐字来自 `plan.md` 设计项 7。

    文档那侧也钉一次：`plan.md` 里那句被改写时本条红 —— 处置项的权威出处只有一处。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("100")
    session.flush()
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(quantity=Decimal("1200"))],
    )
    verdict = budget_check(session, request, card)

    payload = budget.build_over_budget_payload(task_card_id=CARD, verdict=verdict)
    table, _limit = service.load_policies()
    normalized = service.validate_payload(payload, kind=budget.BUDGET_KIND, table=table)
    plan_line = next(line for line in PLAN.read_text(encoding="utf-8").split("\n") if "三个处置项" in line)

    assert set(normalized) == set(service._FIVE), sorted(normalized)
    assert all(option in payload["pending_item"] for option in budget.DISPOSAL_OPTIONS), payload["pending_item"]
    assert " / ".join(budget.DISPOSAL_OPTIONS) in plan_line, plan_line
    assert payload["timeout_policy"] == service.render_timeout_policy(budget.BUDGET_KIND, table=table)
    assert "FR-061" in payload["context_digest"], payload["context_digest"]


def test_the_estimated_extra_usage_is_a_zero_claim_not_an_invented_estimate() -> None:
    """三处置项给人的是**选项**，`estimated_extra_usage` 交的是各口径 0 —— 平台不替人估。

    键必须正好是三口径（这是 FR-061 的分列形状进了 payload 的地方）；值全为 0。
    `downstream_task_ids` 缺省交这张卡自己，因为 `Impact` 不许空列表而超预算直接影响的就是它。
    """
    verdict = budget.BudgetVerdict(
        task_card_id=CARD, budget=Decimal("1000"), used=Decimal("1200"), reached=True, note="probe"
    )

    payload = budget.build_over_budget_payload(task_card_id=CARD, verdict=verdict)

    assert set(payload["impact"]["estimated_extra_usage"]) == set(enums.values_of("billing_basis"))
    assert set(payload["impact"]["estimated_extra_usage"].values()) == {0}
    assert payload["impact"]["downstream_task_ids"] == [CARD], payload["impact"]
    named = budget.build_over_budget_payload(task_card_id=CARD, verdict=verdict, downstream_task_ids=["T11"])
    assert named["impact"]["downstream_task_ids"] == ["T11"]


def test_the_c_class_ticket_id_is_derivable_from_the_same_pair_as_the_gate() -> None:
    """主键可推导（口径同 A 类）：重放回到同一行，不靠 FR-045 的合并兜底。"""
    fr = uuid.uuid4()
    first = service.ticket_id_for(kind=budget.BUDGET_KIND, feature_request_id=fr, task_card_id=CARD)
    again = service.ticket_id_for(kind=budget.BUDGET_KIND, feature_request_id=fr, task_card_id=CARD)
    other_kind = service.ticket_id_for(
        kind=service.KIND_BY_LETTER["A"], feature_request_id=fr, task_card_id=CARD
    )

    assert first == again != other_kind
    assert budget.BUDGET_KIND not in service.ACTIVE_KINDS, "C 类被启用了 ⇒ 下面那条 skip 与这条是同一件事"


def test_mvp_one_refuses_to_open_the_ticket_before_touching_the_database() -> None:
    """建单路径**存在**且接到 `service.create_approval` 这个唯一出口上；MVP-1 的拒绝发生在碰会话之前。

    传进来的 session 是"任何属性访问都报错"的哨兵：读到 `KindNotEnabled` 而不是 `AssertionError`，
    就同时证明了两件事 —— 这条路真的走到那个出口，而且没有先写任何东西。
    """
    verdict = budget.BudgetVerdict(
        task_card_id=CARD, budget=Decimal("1000"), used=Decimal("1200"), reached=True, note="probe"
    )

    with pytest.raises(service.KindNotEnabled) as caught:
        budget.open_over_budget_ticket(
            _ExplodingSession(),
            feature_request_id=uuid.uuid4(),
            task_card_id=CARD,
            verdict=verdict,
        )

    assert budget.BUDGET_KIND in str(caught.value), str(caught.value)


def test_the_ledger_reports_the_verdict_but_opens_nothing_by_default(session, row_count, seeded) -> None:
    """MVP-1 的真实行为：达阈值 ⇒ 读数照报，`approval_ticket` 一行都不多（`over_budget_action` 默认 `None`）。

    这条是"结构预留、默认不启用"的可执行读法 —— 它不是靠 `skip` 蒙过去的：这里用真库数了单据行数。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("1000")
    session.flush()

    run = ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(quantity=Decimal("1200"))],
        feature_request_id=request.id,
    )
    tickets = int(session.scalar(select(func.count()).select_from(ApprovalTicket)) or 0)

    assert run.budget is not None and run.budget.reached is True, run.to_line()
    assert "REACHED" in run.to_line(), run.to_line()
    assert tickets == 0, f"默认档把单据建出来了：{tickets} 行"


def test_an_injected_action_runs_only_when_the_threshold_is_reached(session, seeded) -> None:
    """装配点注入动作 ⇒ **只有达阈值那一发被调用**，未达时一次都不调。

    这条把"启用 C 之后会发生什么"变成可测的形状，而不必真启用它：注入的是一个记录器，
    收到的参数就是 `open_over_budget_ticket` 需要的三样（session / 需求单号 / 卡号 + verdict）。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("1000")
    session.flush()
    calls: list[tuple[str, bool]] = []

    def record(_session, *, feature_request_id, task_card_id, verdict):
        calls.append((str(feature_request_id), verdict.reached))

    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(quantity=Decimal("900"))],
        feature_request_id=request.id,
        over_budget_action=record,
    )
    ledger.record_usage(
        session,
        project_key=PROJECT,
        task_card_id=CARD,
        agent_id=AGENT,
        role_id=ROLE,
        notices=[priced(usage_kind="OUTPUT", quantity=Decimal("200"))],
        feature_request_id=request.id,
        over_budget_action=record,
    )

    assert calls == [(str(request.id), True)], f"动作被调了 {len(calls)} 次：{calls}"


@pytest.mark.skip(reason=C_SKIP_REASON)
def test_reaching_the_threshold_opens_a_c_class_ticket() -> None:
    """**这条是 Phase 12 的启用判据，不是本轮的成果**（owner 2026-10-16 对 T116 的预先裁决）。

    owner 原话要求：MVP-1 不启用 C 类卡点，本条 `skip` 并写明原因，MUST NOT 让它假绿。
    启用那一笔动的是 `service.ACTIVE_KINDS`（与 Phase 4 裁决 6 同处）；届时把 `skip` 摘掉、
    填上真库形状：`seeded` 卡 + `token_budget` + 达阈值的 `record_usage(..., over_budget_action=budget.open_over_budget_ticket)`
    ⇒ 断言 `approval_ticket` 恰好多一行、`kind == BUDGET_KIND`、`ticket_id == service.ticket_id_for(...)`。
    现在它一行代码都没跑 —— 上面两条（默认不建单、注入才动）才是 MVP-1 的实测读数。
    """
    raise AssertionError("这条不该被执行：C 类仍未启用")


# ---------------------------------------------------------------- 六、SC-011 组判据（T117）

SECOND_CARD = "T12"


def notice_set(moment: dt.datetime) -> list[refs.UsageNotice]:
    """三口径各一条，瞬间彼此错开 1 秒。

    错开不是洁癖：幂等键是 `(卡, 工具, 瞬间, 用量种类)`，**口径不在键里** ⇒ 同刻同种类的 `TOKEN`
    与 `BYOK` 会撞成一行、后到的那条被静默丢弃。这一形状已登记为待裁项（见 `tasks.md` T117 行的追记），
    本文件先把"会撞"钉成读数（下面 `test_the_dedup_key_ignores_the_billing_basis...`）。
    """
    return [
        priced(usage_kind="INPUT", quantity=Decimal("1000"), moment=moment),
        priced(
            model_ref=None,
            usage_kind="SEAT_DAY",
            unit="seat-day",
            quantity=Decimal("1"),
            billing_basis="SEAT_CREDIT",
            moment=moment + dt.timedelta(seconds=1),
        ),
        priced(
            model_ref="byok-vendor-model",
            usage_kind="OUTPUT",
            quantity=Decimal("500"),
            billing_basis="BYOK",
            moment=moment + dt.timedelta(seconds=2),
        ),
    ]


def usage_shape(rows: list[CostRecord], key: str) -> dict[str, dict[str, dict[str, Decimal]]]:
    """从**库里的原始行**独立搭出期望形状（不借 `ledger` 的聚合），口径 → 用量种类 → 数量。"""
    out: dict[str, dict[str, dict[str, Decimal]]] = {}
    for row in rows:
        by_basis = out.setdefault(str(getattr(row, key)), {})
        by_kind = by_basis.setdefault(str(row.billing_basis), {})
        kind = str(row.usage_kind)
        by_kind[kind] = by_kind.get(kind, Decimal(0)) + Decimal(str(row.quantity))
    return out


def test_the_dedup_key_ignores_the_billing_basis_so_two_bases_at_one_instant_collide(session, row_count) -> None:
    """**钉现状**：同一 (卡, 工具, 瞬间, 用量种类) 下的第二口径被当成重复丢掉，库里只有一行。

    这不是我们想要的语义（`BYOK` 那条的金额 0 于是也没了），但唯一索引的四列是
    `plan.md` 设计项 7 与 `data-model.md` EN-13 逐字给的 ⇒ 修它要动索引 = 动迁移 = 硬停项，交 owner 裁。
    裁完之后这条要翻成"两行都在"。
    """
    _requires_dsn()
    collision = [
        priced(usage_kind="INPUT", quantity=Decimal("1000"), moment=NOW),
        priced(
            model_ref="byok-vendor-model",
            usage_kind="INPUT",
            quantity=Decimal("999"),
            billing_basis="BYOK",
            moment=NOW,
        ),
    ]

    run = ledger.record_usage(
        session, project_key=PROJECT, task_card_id=CARD, agent_id=AGENT, role_id=ROLE, notices=collision
    )

    assert (run.rows_added, run.duplicates) == (1, 1), run.to_line()
    assert row_count() == 1
    survivors = session.scalars(select(CostRecord)).all()
    assert {str(row.billing_basis) for row in survivors} == {"TOKEN"}, "丢掉的方向也钉一下：先到者留"


def _split_mismatch_note(
    key: str, subject: str, got: dict[str, object], expected: dict[str, object]
) -> str:
    """把"分列数量对不上"那条断言消息**收进一个函数**，并且只点名真正不符的那几格口径。

    为什么不是就地写那句 f-string（owner 2026-10-19 令修的 W92）：原先消息里写的是 `{basis}`，
    而 `basis` 只是上一行字典推导的**内层临时名** —— 推导作用域在推导外不存在 ⇒ 断言一旦真失败，
    抛出来的是 `NameError: name 'basis' is not defined`，那条消息永远读不到。
    失败消息本身也是会被执行的代码，这里靠"值由调用方传进来"把它挪回作用域内。
    """
    names = sorted(set(got) | set(expected))
    bad = {name: (got.get(name), expected.get(name)) for name in names if got.get(name) != expected.get(name)}
    return f"{key}={subject} 的分列数量对不上，实际 vs 期望：{bad}"


def test_every_recorded_call_is_findable_by_agent_and_by_task_at_once(session) -> None:
    """SC-011 第一句的**全链**读法：库里的每一行，两个维度都必须同时查得到，且数量对得上。

    期望形状由 `usage_shape()` 从原始行独立搭出，与 `ledger` 的聚合各走各的路 —— 两侧不符就红，
    这比"数一下行数"能多挡住的错是：某一口径整列被漏读、某一维的过滤条件少写一列。
    """
    _requires_dsn()
    agents = {AGENT, OTHER_AGENT}
    for index, agent in enumerate(sorted(agents)):
        ledger.record_usage(
            session,
            project_key=PROJECT,
            task_card_id=CARD,
            agent_id=agent,
            role_id=ROLE,
            notices=notice_set(NOW + dt.timedelta(hours=index)),
        )
    rows = list(session.scalars(select(CostRecord).order_by(CostRecord.id)).all())
    assert len(rows) == 6, f"三口径 × 两工具应当 6 行，实际 {len(rows)}"

    for key, view_of in (
        ("agent_id", lambda subject: ledger.by_agent(session, agent_id=subject)),
        ("task_card_id", lambda subject: ledger.by_task(session, task_card_id=subject)),
    ):
        expected = usage_shape(rows, key)
        for subject, by_basis in expected.items():
            view = view_of(subject)
            assert view.bases() == tuple(sorted(by_basis, key=list(ledger.BASIS_ORDER).index)), view.to_line()
            got = {basis: view.subtotal(basis).usage for basis in view.bases()}
            assert got == by_basis, _split_mismatch_note(key, subject, got, by_basis)


def test_the_failure_note_names_only_the_mismatching_basis() -> None:
    """W92 的**失败那一支**判据：数对不上时消息要报得出是哪一格，而且它自己不许先炸。

    这条不连库（纯字符串构造）⇒ 住在 fast 档。理由不是"补个覆盖率"：`assert` 真失败的路径平时永远跑不到，
    原缺陷正是藏在这条谁都进不去的支里（消息引用了推导内的临时名 ⇒ 真失败时先抛 `NameError`）。
    两侧都钉：相等的口径**不该**出现在消息里，不符的口径**必须**被点名。
    """
    same = {"CACHE_READ": Decimal("7")}
    note = _split_mismatch_note("agent_id", AGENT, {"CACHE_READ": Decimal("7"), "TOKEN": Decimal("1")}, same)
    assert "TOKEN" in note, note
    assert "CACHE_READ" not in note, f"两相等的口径不该进失败消息：{note}"
    assert _split_mismatch_note("task_card_id", CARD, same, dict(same)).endswith("{}"), "全相等时不许点名任何口径"


def test_the_same_card_read_from_two_views_splits_the_same_three_columns(session) -> None:
    """SC-011 第三句：三口径 **100% 分列** ⇒ 从两个维度读，分列结果一致，且 `total()` 报错不给数。"""
    _requires_dsn()
    ledger.record_usage(
        session, project_key=PROJECT, task_card_id=CARD, agent_id=AGENT, role_id=ROLE, notices=notice_set(NOW)
    )

    view = ledger.by_task(session, task_card_id=CARD)
    per_agent = [ledger.by_agent(session, agent_id=AGENT)]

    assert view.bases() == ("TOKEN", "SEAT_CREDIT", "BYOK"), view.to_line()
    assert {item.rows for item in view.subtotals.values()} == {1}, view.to_line()
    assert [item.cost_amount for item in per_agent[0].subtotals.values()] == [
        view.subtotal(basis).cost_amount for basis in view.bases()
    ], "两维读出的分列金额不一致"
    assert "BYOK" in view.to_line() and "不可相加" in view.to_line(), view.to_line()
    with pytest.raises(ledger.CrossBasisSumRefused):
        view.total()


def test_a_replayed_report_changes_no_number_anywhere_on_the_chain(session, row_count, seeded) -> None:
    """**核心路径**（重复入账）在 SC-011 那一整句上的读法：整发重放三次，两维读数一次都没变。

    与 节 三那条的分工：那条量"库里不多行"，这条量"报表侧的分列与预算读数也不动" ——
    幂等若只在写者侧生效而查询侧漏了 `dedup`（比如有人改用 `sum` 去重），这条会红而那条不会。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("100000")
    session.flush()
    items = notice_set(NOW)
    replay = lambda: ri.record_result(  # noqa: E731
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of(items),
        actor_agent_id=AGENT,
        created_at=NOW,
    )

    replay()
    baseline_rows = row_count()
    baseline_task = ledger.by_task(session, task_card_id=CARD).to_line()
    baseline_agent = ledger.by_agent(session, agent_id=AGENT).to_line()
    last = replay()
    last = replay()

    assert baseline_rows == 3 == row_count(), "重放多入账了"
    assert ledger.by_task(session, task_card_id=CARD).to_line() == baseline_task
    assert ledger.by_agent(session, agent_id=AGENT).to_line() == baseline_agent
    assert (last.usage.rows_added, last.usage.duplicates) == (0, 3), last.usage.to_line()


def test_the_real_write_path_also_compares_the_budget(session, seeded) -> None:
    """SC-011 第二句里 MVP-1 **能闭合的那一半**：比对确实跟着写入点跑，读数随回收结果一起交回。

    另一半（"先暂停并请示"）不在本轮范围：见上面那条 `skip` 与本文件 节 五 的默认不建单。
    这一条之所以值得单开：装配方以后把 `record_usage` 的 `feature_request_id` 漏传，
    比对会静默不跑（`LedgerRun.budget` 变成 `None`），而库里一切正常 —— 只有这条读得出。
    """
    _requires_dsn()
    request, card = seeded
    card.token_budget = Decimal("1000")
    session.flush()

    ingested = ri.record_result(
        session,
        feature_request_id=request.id,
        task_id=CARD,
        result=result_of([priced(quantity=Decimal("1200"))]),
        actor_agent_id=AGENT,
        created_at=NOW,
    )

    assert ingested.usage.budget is not None and ingested.usage.budget.reached is True, ingested.usage.to_line()
    assert "REACHED" in ingested.usage.to_line(), ingested.usage.to_line()


@pytest.mark.skip(reason=C_SKIP_REASON)
def test_sc_011_second_clause_pauses_before_the_next_call() -> None:
    """SC-011 第二句「达预算阈值的任务 100% 先暂停并请示」**整句未闭合**，按 owner 裁定 `skip`。

    owner 2026-10-16 原话：这一条 MUST NOT 假绿。摘掉 `skip` 的位置在 Phase 12（启用 C 类），
    届时这条要在**真图**里量：达阈值 ⇒ 图停在 `ApprovalGate(kind=C)`、`approval_ticket` 多一行 C 类、
    且后续那一次工具调用**没有发生**（用 Mock transport 的调用计数读，不许只看单据存在）。
    """
    raise AssertionError("这条不该被执行：暂停与请示的启用归 Phase 12")
