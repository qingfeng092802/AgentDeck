"""M2-001 / M2-002 的集成判据：活动流的读与 SSE 推流**真连 PG**（FR-049 / FR-050｜R-13）。

**为什么这一档不能被 `tests/guards/test_board_stream.py` 替掉**：那一档用替身量的是"循环与帧的形状"，
它一次也不会把 `select` 发给库。这里要量的恰好是替身给不了的五件事：

1. 游标分页在**真插入序**上成立（`id` 单调，翻页不重不漏）；
2. `kind` 那一列取回来是**纯标签串**而不是 `Enum.MEMBER`（`pg_enum()` 用字面量构造 `SAEnum`，
   没有 Python 枚举类参与 ⇒ `str(row.kind)` 就等于值本身。哪天有人把域换成枚举类，
   看板上就会出现 `EventLogKind.TASK` 这种形状，而替身档永远看不见）；
3. `created_at` 带时区往返（列是 `DateTime(timezone=True)`，序列化出去的 ISO 串 MUST 带偏移）；
4. 载荷里那串中文在 `data:` 行里**逐字**出现，没被 `\\uXXXX` 化，也没被脱敏洗掉
   （洗只发生在写入点 `events.wash()`，读侧不再洗 —— 再洗一遍就会把已经洗过的痕迹当成新命中）；
5. **一条已经打开的流自己等到后来的提交** —— FR-050 那句"无需手动刷新即反映变化"的本体，
   替身档与"提交后再新开一条流"那种读法都证不到它（见最后那发的注释）。

**每一发都种自己的行、删自己的行**：`event_log` 是全表共享的（别的用例、别的轮次都往里写），
所以判据一律按 `entity_id=<本发独有的 scope>` 筛，收尾按本发拿到的 `id` 差集删。
这既不是"清库"也不是"全表断言"，是把这一发的取证范围收到自己能负责的那几行上。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from acp.app import create_app
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import events
from acp.persistence.models_registry import EventLog

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG",
)

NOW = dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def plant(engine):
    """种 EN-14 的行并保证收尾删干净。

    这里**必须真提交**：SSE 那条流的每一轮都自己开会话、关会话，读的是另一条连接上的已提交事实。
    用 fixture 级"回滚式"会话种的话，流里永远读不到 ⇒ 那条判据会以"没数据"为由恒绿或恒红，
    两种都不是我们要的"看见了才绿"。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    created: list[int] = []

    def add(*, kind: str = "TASK", note: str, entity_id: str, at: dt.datetime = NOW) -> int:
        with factory() as session:
            # 走 `events.record()` 而不是手搓 INSERT：写侧那道"kind 必须在域里、payload 必须过洗"
            # 是本判据的前提之一，绕过它就等于用另一条没被审过的路径造数据。
            row = events.record(
                session,
                entity="task_card",
                entity_id=entity_id,
                kind=kind,
                actor_agent_id="agent-1",
                payload={"note": note},
                created_at=at,
            )
            session.commit()
            created.append(row.id)
            return row.id

    yield add
    with factory() as session:
        session.execute(delete(EventLog).where(EventLog.id.in_(created)))
        session.commit()
    assert created, "本发没种任何行 ⇒ 下面那些'读得出来'的断言其实一次都没跑"


def client_with_store(engine) -> TestClient:
    return TestClient(create_app(session_factory=lambda: Session(bind=engine)))


def read_ids(client: TestClient, scope: str) -> list[int]:
    page = client.get("/events", params={"entity_id": scope, "limit": 50}).json()
    return [row["id"] for row in page["rows"]]


def test_the_page_read_returns_the_planted_rows_in_insertion_order(engine, plant):
    scope = f"board-{uuid.uuid4().hex[:12]}"
    first = plant(note="拆单", entity_id=scope)
    second = plant(note="指派", entity_id=scope)
    client = client_with_store(engine)
    ids = read_ids(client, scope)
    assert ids == sorted([first, second]) == [first, second], f"插入序不是单调的：{ids}"
    assert first < second, "自增主键给出了顺序，游标分页的前提就是它"


def test_the_cursor_page_advances_and_reports_has_more_honestly(engine, plant):
    scope = f"board-{uuid.uuid4().hex[:12]}"
    ids = [plant(note=f"第 {n} 步", entity_id=scope) for n in range(3)]
    client = client_with_store(engine)
    page1 = client.get("/events", params={"entity_id": scope, "limit": 2}).json()
    assert [row["id"] for row in page1["rows"]] == ids[:2]
    assert page1["has_more"] is True and page1["next_cursor"] == ids[1]
    page2 = client.get(
        "/events", params={"entity_id": scope, "limit": 2, "cursor": page1["next_cursor"]}
    ).json()
    assert [row["id"] for row in page2["rows"]] == ids[2:]
    # 最后一页的 has_more 必须是 False —— 它是"多读一行"换来的事实，不是按行数猜的。
    assert page2["has_more"] is False and page2["next_cursor"] == ids[2]


def test_the_page_orders_by_insertion_not_by_the_business_clock(engine, plant):
    """游标按 `id` 排，不按 `created_at` 排 —— 这一条只有真库能量，且必须把时刻**反着摆**。

    摆法是决定这条有没有鉴别力的唯一因素：**后插入的那行带更早的业务时刻**。
    这样按 `id` 排得到 `[先插的, 后插的]`，按 `created_at` 排得到相反序 ⇒ 读侧一旦被改成按时刻排，
    这一发才红。第一版我把两行摆成"先插入的那行时刻更早"，两序恰好同向，于是 M-F10
    （把 `order_by` 换成 `created_at`）**整档全绿** —— 反着摆摆反了，等于没做对照。

    口径与 `run.py::_acceptance_audit` 同源：它取"最后落库的那一行"用的也是 `order_by(EventLog.id)`，
    而不是载荷里那个自报的轮次（被检对象不许给自己排座次）。
    """
    scope = f"board-{uuid.uuid4().hex[:12]}"
    first = plant(note="先插入", entity_id=scope, at=NOW)
    second = plant(note="后插入、但业务时刻更早", entity_id=scope, at=NOW - dt.timedelta(minutes=5))
    assert first < second, "自增主键给了反常形状，这一发没构成对照"
    ids = read_ids(client_with_store(engine), scope)
    assert ids == [first, second], f"读侧改成按时刻排了（期望按 id 的 {first, second}）：{ids}"


def test_a_row_read_from_pg_keeps_its_shapes(engine, plant):
    """四件事一起量：`kind` 是纯标签、时刻带偏移、中文逐字、载荷是当初那份映射。

    `kind` 取 `SELECTION` 而不是随便编一个：`events.record()` 只放行 `event_log_kind` 域里的值，
    这一发第一次跑就用了域外的 `DISPATCH`，于是它红在**写侧**、并让 `plant` 的收尾断言报了
    "本发没种任何行"（那条断言就是为这种"看着跑了其实没跑"准备的）。
    """
    scope = f"board-{uuid.uuid4().hex[:12]}"
    plant(kind="SELECTION", note="指派给前端岗", entity_id=scope)
    row = client_with_store(engine).get("/events", params={"entity_id": scope}).json()["rows"][0]
    assert row["kind"] == "SELECTION", f"枚举列取回来的形状变了：{row['kind']!r}"
    assert row["created_at"].endswith("+00:00"), row["created_at"]
    assert row["payload"] == {"note": "指派给前端岗"}, "载荷被改了形：读侧不该再洗一遍"
    assert row["actor_agent_id"] == "agent-1" and row["entity_id"] == scope


def test_one_open_stream_carries_a_row_committed_after_it_opened(engine, plant):
    """FR-050 那句"无需手动刷新即反映变化"的**唯一可证形态**：一条已经打开的流，自己等到后来落库的行。

    上面那两条流式用例每次都是"提交之后**再开一条**流"——那种读法证的是游标续读，
    证不了"不重开也能变"：把 `_tail` 改成"每轮不重新读库、只在开始时读一次快照"，
    那两条照样全绿（它们本来就在新开连接）。所以这里线程与主线程**共用同一条生成器**：
    线程先跑起来、读到打开前那一行，主线程在那之后才提交第二行，第二行必须由**同一条**流发出来。

    每轮自己开、自己关会话（见 `web._tail` 的注释），所以这里真的换了连接去读 PG ——
    读的隔离级别也是被量的一部分：要是循环复用了第一轮那个会话，`READ COMMITTED` 之下的新行
    会被同一事务的旧快照挡住，这一发就会等到超时而不是等到帧。

    时限是**存在性**的（等不到就红），不是延迟 SLO —— SC-013 那个秒数的关系判据在
    `tests/guards/test_board_stream.py` 现读 `spec.md`，那里才是不许写死数字的地方。
    """
    from acp.api import web

    scope = f"board-{uuid.uuid4().hex[:12]}"
    anchor = plant(note="流打开前就有的一行", entity_id=scope)
    interval = 0.05
    frames: list[str] = []

    def tail() -> None:
        for frame in web._tail(
            lambda: Session(bind=engine), start=anchor - 1, stop_after=2, interval=interval
        ):
            frames.append(frame)

    worker = threading.Thread(target=tail, daemon=True)
    opened_at = time.monotonic()
    worker.start()
    # 先等第一轮真的发过一帧，否则"等到了第二帧"可能只是启动顺序凑巧（前置动作不做，等于没断言）。
    deadline = opened_at + 10.0
    # 一帧是**三行**（`id:` / `event:` / `data:`），不是只有 `data:` 那一行：
    # 用 `startswith("data: ")` 去筛数据帧会一个也筛不到，而这里的第一处断言恰好是按 `id:` 行匹配的，
    # 于是"没筛出来"与"流没发"两种情况在旧写法里长得一样（第一跑就是这么空的）。
    while time.monotonic() < deadline and not any(f.startswith("id: ") for f in frames):
        time.sleep(0.02)
    assert any(f"id: {anchor}\n" in f for f in frames), f"第一条流连打开前那行都没发出来：{frames}"

    # 空转几轮**再**提交第二行：这一觉不是等稳定，是刻意制造"空闲轮"。
    # 不睡就直接提交的话，两轮就把两行发完，下面那条 keepalive 断言会变成掷硬币（第一跑就是这么红的：
    # `assert 0 >= 1`， ids 两个都对，只有空闲轮一次没出现）。
    time.sleep(interval * 4)
    later = plant(note="流打开之后才提交的一行", entity_id=scope)
    worker.join(timeout=interval * 40 + 5.0)
    assert not worker.is_alive(), "生成器没在第二行到达后结束 ⇒ 它可能根本没在每轮重新读库"

    ids = [json.loads(f.split("data: ", 1)[1])["id"] for f in frames if "data: " in f]
    assert ids == [anchor, later], f"同一条流应当按插入序发出这两行：{ids}"
    assert sum(1 for f in frames if f.startswith(": keepalive")) >= 1, (
        "空闲一轮都没发过 keepalive：等不到第二行时，客户端与判据都无法区分'活着'与'卡住'"
    )
    assert any("流打开之后才提交的一行" in f for f in frames), "中文在 data: 行里变了形"


def test_the_stream_tails_only_rows_after_the_cursor(engine, plant):
    """FR-050 的最小可证形态：先推已有的，再按游标续推**新落库的那一行**，不回头重发。

    最后那半句同时证了中文逐字：`payload` 从 `data:` 行解码回来仍是那句中文，
    转义成 `\\uXXXX` 或读侧再洗一遍都会在这里红 —— 所以本文件不再单开一条只查中文的流式用例，
    那条会不得不依赖"表里此刻只有我这行"这种不成立的前提（`event_log` 是全表共享的）。
    """
    scope = f"board-{uuid.uuid4().hex[:12]}"
    first = plant(note="已有的第一步", entity_id=scope)
    client = client_with_store(engine)
    # 游标取 `first - 1` 而不是 0：`first` 是刚种下的那一行，它的 id 是本表此刻的最大值，
    # 所以"从 first-1 起读一帧"必然读到我自己那行；从 0 起就会读到库里别人的历史行，
    # 那条断言就变成了"依赖表里只有我这行"这种不成立的前提。
    with client.stream(
        "GET", "/events/stream", params={"after": first - 1, "limit": 1, "poll_ms": 1}
    ) as response:
        assert response.status_code == 200
        frames = [line for line in response.iter_lines() if line.startswith("data: ")]
    assert len(frames) == 1, frames
    stream_id = json.loads(frames[0][len("data: ") :])["id"]
    assert stream_id == first

    later = plant(note="后来落库的一步", entity_id=scope)
    with client.stream(
        "GET", "/events/stream", params={"after": stream_id, "limit": 1, "poll_ms": 1}
    ) as response:
        frames = [line for line in response.iter_lines() if line.startswith("data: ")]
    payloads = [json.loads(f[len("data: ") :]) for f in frames]
    assert [p["id"] for p in payloads] == [later], f"续流把旧行又发了一遍：{payloads}"
    # 这一句量的是**线上那一行的字节**，不是解码后的对象：解码会把 `\\uXXXX` 变回原字，
    # 只比对象就看不见转义（替身档的变异 M-F6 第一次就是这么绿的，两档都补了同一条）。
    assert "后来落库的一步" in frames[0], frames[0]
    assert payloads[0]["payload"] == {"note": "后来落库的一步"}
