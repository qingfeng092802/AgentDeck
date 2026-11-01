"""T050：LangGraph 的 checkpointer 落 PG（D-9 / 宪法 V，FR-041）。

三件事，且只做这三件：

1. **给图一个 `PostgresSaver`**。它的连接**独立于** `OrchestrateDeps.session`：
   `graph.py` 的模块头早就点过名 —— 把编排会话与 checkpointer 会话混成一处，会让"业务事实态"
   与"图的挂起现场"共用一个工作单元，于是节点里的 `flush` 与 LangGraph 写的 checkpoint 行
   会在同一次回滚里一起消失（那是最难查的一种绿：图看起来跑完了，库里什么都没留）。
2. **`thread_id` 由已存的列推导，不新增列、不写第二份事实**。EN-09 已经有
   `kind / feature_request_id / task_card_id` 三列，`thread_id = f"acp:{kind}:{req}[:{card}]"`
   是它们的纯函数 ⇒ 恢复路径从单据本身就能算回同一个 thread（FR-041 的"同一 `thread_id` 恢复"）。
   代价点名：同一个 `(kind, 需求单, 卡)` 的**第二次**挂起点会算出同一个 thread ——
   MVP-1 里 FR-045 保证同一任务同时只有一张未决单，所以这条推导式不会被两件事抢同一个名字。
3. **checkpoint 表的建表语句不抄字面量**。schema 的唯一来源是
   `langgraph.checkpoint.postgres.base.MIGRATIONS`（本模块只 `tuple(...)` 别一下名），
   迁移 `0005_t050_checkpoints` 与本文件的 `ensure_checkpoint_schema()` 都遍历**同一份列表**。
   为什么两处都要有：迁移管"从空库能建起来"（Checkpoint 1 那句"17 实体迁移可跑"的含义），
   `ensure_` 管"升级了 `langgraph-checkpoint-postgres` 而旧库没重放 0005"的那一刻 ——
   列表里新增一条时，旧库不会自动补，`test_the_pinned_library_and_the_db_agree` 会红。

凭据纪律（宪法「二」）在本模块的落点：DSN 只从 `ACP_DB_DSN` 取；连不上时抛的消息
**只带变量名与异常类名**，`raise ... from None` 掐断异常链 —— psycopg 的原始消息里可能带出
连接串参数，留着链就是把口令递给日志（`config/loader.py` 的 `CredentialMissing` 同一条口径）。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any, get_args, get_origin, get_type_hints

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.base import MIGRATIONS
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from acp.config.loader import DSN_ENV, db_url, env_value

#: checkpoint schema 的语句列表 = 库自带的那一份，逐字遍历，不在这里增删。
SCHEMA_STATEMENTS: tuple[str, ...] = tuple(MIGRATIONS)


class CheckpointerUnavailable(RuntimeError):
    """连不上 PG 或形态不对。消息里只有变量名与异常类名，MUST NOT 带出连接串参数。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"checkpointer 不可用：{detail}")


def conninfo(env: Any = None) -> str:
    """`ACP_DB_DSN` → psycopg 连接串（去掉 SQLAlchemy 的 `+psycopg` 驱动标记）。

    `db_url()` 是给 SQLAlchemy 的（它要驱动前缀），psycopg 自己不认那个前缀 ⇒ 这里剥掉。
    两处共用同一个环境变量与同一条读取路径，不各读一遍。
    """
    url = db_url(env)
    for scheme in ("postgresql+psycopg://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql://" + url[len(scheme) :]
    return url


def open_connection(env: Any = None) -> psycopg.Connection:
    """打开一条 autocommit 连接（`PostgresSaver` 要求 autocommit：它自己管写入边界）。"""
    env_value(DSN_ENV, env)  # 先做"变量在不在"的判断，消息里只会出现变量名
    try:
        return psycopg.connect(conninfo(env), autocommit=True)
    except psycopg.Error as exc:
        # `from None`：异常链里那句原始消息可能含连接串参数，掐掉它是凭据纪律的要求。
        raise CheckpointerUnavailable(f"{DSN_ENV} 指向的 PG 连不上（{type(exc).__name__}）") from None


def apply_schema(execute: Callable[[str], Any]) -> int:
    """把 `SCHEMA_STATEMENTS` 逐条交给一个 execute  callable，返回执行条数。

    唯一一份"怎么建 checkpoint 表"的循环：迁移 `0005` 与 `ensure_checkpoint_schema()` 都调它，
    两边不可能走成两个口径（分两处写的话，升级库的那一处会先漂）。
    """
    for statement in SCHEMA_STATEMENTS:
        execute(statement)
    return len(SCHEMA_STATEMENTS)


_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(([\s\S]*?)\);", re.I)
_ADD_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.I)
_INDEX_RE = re.compile(r"CREATE INDEX (?:CONCURRENTLY )?IF NOT EXISTS (\w+) ON (\w+)", re.I)
_CONSTRAINT_PREFIXES = ("PRIMARY KEY", "CONSTRAINT", "UNIQUE", "FOREIGN KEY", "CHECK")


def library_declared_schema() -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """把 `SCHEMA_STATEMENTS` 解析成 `(表 → 列名集合)` 与 `{(表, 索引名)}`。

    这是**判据侧的载体**，形态与 `persistence/states.py` 解析 `data-model.md §2` 同族：
    权威在列表里，解析出来和库里实际有的比，差任何一个列 / 索引就红。放在 `src/` 而不是测试文件里，
    是因为迁移与用例要共用同一份解析 —— 两处各写一个正则就会漂成"迁移认为自己建全了而用例说没有"。
    """
    joined = "\n".join(SCHEMA_STATEMENTS)
    tables: dict[str, set[str]] = {}
    for name, body in _TABLE_RE.findall(joined):
        columns: set[str] = set()
        for raw in body.split("\n"):
            line = raw.strip().rstrip(",")
            if not line or line.upper().startswith(_CONSTRAINT_PREFIXES):
                continue
            columns.add(line.split()[0])
        tables.setdefault(name, set()).update(columns)
    for table, column in _ADD_COLUMN_RE.findall(joined):
        tables.setdefault(table, set()).add(column)
    indexes = {(table, name) for name, table in _INDEX_RE.findall(joined)}
    return tables, indexes


def ensure_checkpoint_schema(connection: psycopg.Connection) -> int:
    """幂等补齐 checkpoint schema（列表里的每一条都带 `IF NOT EXISTS` / `IF NOT EXISTS` 形 ALTER）。

    调用时机 = 装配点（T123）起图之前。**迁移仍是 schema 的权威**：本函数不为"跳过迁移"开路，
    它只让"库已建好 + 依赖版本涨了"这一种状态自愈。
    """
    return apply_schema(connection.execute)


def allowed_modules_for(state_schema: type) -> tuple[tuple[str, str], ...]:
    """从 state 的类型标注里**派生** msgpack 白名单，不手写清单。

    LangGraph 的 `JsonPlusSerializer` 对自定义类走"未注册即可疑"的路子：实测读到未注册的
    pydantic 模型会发 `Deserializing unregistered type …` 告警，并明说将来会变成拒绝
    （`LANGGRAPH_STRICT_MSGPACK=true` 立刻拒）。手写白名单的风险是"往 state 里加了个新模型而忘了
    登记" —— 那会在恢复路径上炸；从标注派生则永远跟着 state 的形状走。
    """
    found: list[tuple[str, str]] = []
    for hint in _hints(state_schema).values():
        for klass in _classes(hint):
            if klass is state_schema:
                continue
            item = (klass.__module__, klass.__qualname__)
            if item not in found:
                found.append(item)
    return tuple(found)


def _hints(state_schema: type) -> dict[str, Any]:
    return dict(get_type_hints(state_schema))


def _classes(hint: Any) -> Iterable[type]:
    origin = get_origin(hint)
    if origin is not None:
        for arg in get_args(hint):
            yield from _classes(arg)
        return
    if isinstance(hint, type) and hasattr(hint, "model_dump"):
        yield hint


def build_checkpointer(
    connection: psycopg.Connection, *, state_schema: type | None = None
) -> PostgresSaver:
    """给出绑好连接的 `PostgresSaver`；`state_schema` 决定 msgpack 白名单（不传就是默认序列化）。"""
    serde = None
    if state_schema is not None:
        serde = JsonPlusSerializer(allowed_msgpack_modules=list(allowed_modules_for(state_schema)))
    return PostgresSaver(connection, serde=serde) if serde else PostgresSaver(connection)


def thread_id_for(
    *,
    kind: str,
    feature_request_id: uuid.UUID,
    task_card_id: str | None = None,
) -> str:
    """确定性推导的 thread 号（见模块头第 2 条：它是三列的函数，不是第四份事实）。"""
    if not kind or not isinstance(feature_request_id, uuid.UUID):
        raise CheckpointerUnavailable("thread_id 需要卡点类型与需求单号两者")
    suffix = f":{task_card_id}" if task_card_id else ""
    return f"acp:{kind}:{feature_request_id}{suffix}"


def thread_config(
    *,
    kind: str,
    feature_request_id: uuid.UUID,
    task_card_id: str | None = None,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """LangGraph 的 `configurable` 字典；恢复与首发用同一个推导式 ⇒ 不会漂成两个 thread。"""
    configurable: dict[str, Any] = {
        "thread_id": thread_id_for(
            kind=kind, feature_request_id=feature_request_id, task_card_id=task_card_id
        )
    }
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def now(moment: dt.datetime | None = None) -> dt.datetime:
    return moment if moment is not None else dt.datetime.now(dt.timezone.utc)
