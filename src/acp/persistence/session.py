"""PG 会话的获取点（原则 I 的落地：事实态只有 PostgreSQL 一份，连接也只有这一条路径）。

**URL 一律来自 `config/loader.db_url()`，也就是只读环境变量**（宪法「二」）。这里不提供
"从文件里找连接串"的回退，也不接受把 DSN 当参数传进产品代码的写法 —— 测试要指向别的库，
就在测试里注入 `session_factory`，而不是在生产代码里开一条"能传字符串"的口子。

引擎与会话**不在模块级建**：导入 `acp.persistence` 时不该因为没配环境变量就炸；
迁移（`migrations/env.py`）与用例各自拿自己的连接，互不共享池子。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from acp.config.loader import db_url


def make_engine() -> Engine:
    return create_engine(db_url())


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False)


@contextmanager
def new_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """一个工作单元一个会话：正常退出提交，抛错回滚。

    `rollback()` 在异常路径上必须真做 —— 节点里半途失败时，会话里留着脏对象会让同一个
    会话的下一笔写入连带提交上去，那比"这一笔没写"坏得多。
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
