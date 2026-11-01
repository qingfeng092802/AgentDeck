"""Q4 裁决的判据：PG 档"入口断言干净 / 出口清理干净"这两半真的在做事。

**为什么这一组判据存在**：checkpointer 走的是另一条 autocommit 连接（`persistence/checkpointer.py`
的 `open_connection()`），会话夹具的 `trans.rollback()` 发不到它 ⇒ 跑过受理图的用例每发都会在
`checkpoints` 里留一个 thread，而它对应的那行 EN-01 早随事务消失。本轮实测到的基线是
**13 个孤儿 / 51+126+67 行**（读数与一次性清理记在 `evidence/phase-12.md §十三`）。owner 裁"现在删 +
夹具层加清理"，处置对应我给的那三档里的**第 ① 档**（每次跑完按 thread 精确清）+ 一次性清，不是第 ② 档
（把入口守卫扩到 checkpointer 表 —— 那只会让守卫变宽，不会让现场变干净）。

三节：

- 节 一 = 形状（不连库）：删除只走 `chaos.kill_graph_state` 那一条路径；圈定条件两条都在
  （`acp:` 前缀 + `feature_request` 里没有对应行）；入口与出口挂在同一个夹具上；入口那个读数
  必须来自 SQL 而不是常量。
- 节 二 = 活性对照（真 PG）：**真造一个孤儿** ⇒ 入口读数看得见它 ⇒ 出口清理之后它没了，
  而一个**不属于本栈的 thread（非 `acp:` 前缀）一行都没少**。后半句是防"整表清空"那种省事写法。
- 节 三 = 库级不变量（真 PG，两发）：① 跑到这一发时孤儿读数必须是 0（前面那些套件没漏）；
  ② **入口读数**必须是空 —— 它证的正是"上一发的出口清理真的跑过"（M46 的证人）。

缺 `ACP_DB_DSN` 时只有节 一 能跑；节 二 / 节 三 整条 skip（不是绿）。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine

from acp import chaos
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import checkpointer as cp
from conftest import pg_orphan_thread_ids, pg_purge_orphan_threads

CONFTEST = pathlib.Path(__file__).resolve().parent.parent / "conftest.py"
FORGED_NS = ""


def top_level_block(text: str, name: str) -> str:
    """取出 conftest 里那个顶层 `def name(...)` 的整块源码（到下一个顶格行为止）。

    用行扫而不是正则：`\\ndef ` 那种 lookahead 会把"函数后面还有空行再接装饰器"的形态判成没匹配，
    于是判据退化成"找不到就红"—— 那是判据自己在骗自己（本轮第一次跑就红在这里，不是代码红）。
    """
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith(f"def {name}(")), None
    )
    assert start is not None, f"conftest 里没有顶层 `def {name}(` ⇒ Q4 那一半没落地"
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip() and not lines[index].startswith((" ", "\t", "#"))
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def sql_pieces(text: str, name: str) -> str:
    """把 `NAME = ( "…" "…" )` 那种跨行拼接的 SQL 还原成一整句（只剥外层双引号）。"""
    match = re.search(rf"{name} = \(\n(.*?)\n\)", text, re.S)
    assert match, f"conftest 里没有 `{name}` 这条跨行拼接的定义"
    return " ".join(line.strip().strip('"') for line in match.group(1).splitlines() if line.strip())


def block_without_docstring(text: str, name: str) -> str:
    """取那个函数的**代码**部分，文档串摘掉。

    为什么要摘：本判据要找的是"代码里有没有第二条删除路径"，而文档串里有责任写明"刻意不是整表
    DELETE"这句话 —— 不摘的话这条判据会被自己的说明文字绊倒（本轮第一次跑就是这样，红的不是代码）。
    同族的旧账见 §十三 W29（文档层判据要防自指）。
    """
    code = top_level_block(text, name)
    tree = ast.parse(code)
    doc = ast.get_docstring(tree.body[0], clean=False)
    return code.replace(doc, "") if doc else code


# ---------------------------------------------------------------- 一、形状


def test_the_fixture_deletes_only_through_the_one_known_path():
    """出口清理的删除动作必须是 `chaos.kill_graph_state`，其代码里不许出现第二条 DELETE。

    钉这一格有两个具体原因：① `kill_graph_state` 带着"子表在前"的表名单（`THREAD_TABLES`），
    库里没有真外键，顺序写错就把三类行变成没有父行的孤儿；② 它要求一个明确的 `thread_id`，
    空串会被那道 `thread_required` 拒绝 —— 而整表清空那种写法**既能删掉不该删的现场，又报不出删了什么**。
    同一份"怎么删"的知识只留一处（宪法原则 I）。证人：M49 把那一行换成整表 DELETE ⇒ 这条红。
    """
    code = block_without_docstring(CONFTEST.read_text(encoding="utf-8"), "pg_purge_orphan_threads")
    assert "kill_graph_state" in code, "出口清理没走唯一那条删除路径 ⇒ 表名单与顺序会各漂一份"
    assert not re.search(r"(?i)\bdelete\s+from\b", code), "清理代码里出现了第二条删除路径"


def test_the_orphan_predicate_carries_both_qualifiers():
    """孤儿定义必须同时带两个限定：`acp:` 前缀 + `feature_request` 里没有对应行。

    少前缀 ⇒ 清理会把不属于本测试栈的 thread 一起抹掉（库是共享的开发库，不是沙盒）；
    少 `not exists` ⇒ "清理"退化成套件跑完把 `acp:` 现场全删，那会把**真发跑出来并还要复查**的
    现场也抹掉（比如同一个会话里前一发建了单、后一发要恢复它）。这一条是 M48 的证人。
    """
    text = CONFTEST.read_text(encoding="utf-8")
    joined = sql_pieces(text, "PG_ORPHAN_THREAD_SQL")
    assert "like 'acp:%'" in joined, f"少了前缀限定 ⇒ 会删到别人的 thread：{joined}"
    assert "not exists" in joined and "feature_request" in joined, f"少了 EN-01 对照：{joined}"
    assert "split_part" in joined, f"需求单号那一半要从 thread_id 里取出来：{joined}"


def test_entry_assertion_and_exit_cleanup_hang_on_the_same_fixture():
    """入口那半与出口那半必须在同一个 session 夹具里（`yield` 把两半缝在一起）。

    分开挂会有一种缝：夹具 A 断言干净、夹具 B 负责清理，而 B 被 skip / 被改顺序 ⇒ A 报的"脏"
    没人负责，B 清的又不是 A 判的那批。这一发不测行为，测的是"两半没有分家"。
    """
    code = top_level_block(CONFTEST.read_text(encoding="utf-8"), "pg_tables_empty_at_entry")
    assert "pg_orphan_thread_ids(" in code, "入口那一半没查孤儿 thread ⇒ Q4 的对称只落了一半"
    assert "yield" in code and "pg_purge_orphan_threads(" in code, "出口那一半没挂在这个夹具上"


def orphan_query_names(tree: ast.AST) -> set[str]:
    """哪些变量的值直接来自那条孤儿读数函数（用来允许"先存进另一个名字再交给 `at_entry`"的等价写法）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        called = getattr(func, "id", "") or getattr(func, "attr", "")
        if "orphan_thread_ids" in called:
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def test_the_entry_reading_is_a_database_read_not_a_default():
    """`at_entry` 必须来自那条 SQL，不能是写死的空值。

    为什么单独钉这一格：下面节 三 那条入口证人读的就是 `orphans_at_entry`，而它断言的是"等于空" ——
    一旦 `at_entry` 被写成常量 `()`，这条证人就**恒真**（fixture 的默认值等于缺陷值，判据自己失效）。
    M47 就是这一发的证人：把 `at_entry = pg_orphan_thread_ids(thread_engine)` 改成 `at_entry = ()`
    ⇒ 这条红。反向 R11 走"先存进 `reading` 再交给 `at_entry`"的等价写法 ⇒ 必须绿，
    所以钉的是**来源**，不是某一行的写法。
    """
    code = block_without_docstring(CONFTEST.read_text(encoding="utf-8"), "pg_tables_empty_at_entry")
    tree = ast.parse(code)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "at_entry" for t in node.targets)
    ]
    assert len(assigns) == 1, f"入口读数 `at_entry` 的赋值应当恰一条，实际 {len(assigns)} 条 ⇒ 谁改写它说不清"
    value = assigns[0].value
    called = getattr(value.func, "id", "") or getattr(value.func, "attr", "") if isinstance(value, ast.Call) else ""
    from_query = (isinstance(value, ast.Call) and "orphan_thread_ids" in called) or (
        isinstance(value, ast.Name) and value.id in orphan_query_names(tree)
    )
    assert from_query, (
        f"入口读数不是那条 SQL 的返回值（写成常量就让 hygiene 的入口证人恒真）：{ast.dump(value)[:120]}"
    )


# ---------------------------------------------------------------- 二、活性对照


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整节 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


def forge(conn, thread_id: str) -> None:
    """往 `checkpoints` 里塞一行最小的现场（列取自库的实际 NOT NULL 集合，不猜）。"""
    with conn.cursor() as cursor:
        cursor.execute(
            "insert into checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "values (%s, %s, %s, '{}'::jsonb, '{}'::jsonb)",
            (thread_id, FORGED_NS, "forged-by-judgment"),
        )


def test_a_forged_orphan_is_seen_at_entry_and_gone_after_the_exit_half(engine):
    """活性对照（owner 的原文："真造一个孤儿 ⇒ teardown 后应删干净"）。

    三步都要有读数：① 造完之后**入口那条 SQL 看得见它**（否则这一发可以是恒真）；
    ② 出口清理返回的名单里有它，且清完读数里没有它了；③ 一个非 `acp:` 前缀的 thread 一行不少 ——
    这一句防的正是"整表清空"那种写法，它是本发的反向半段。
    """
    orphan = cp.thread_id_for(kind="A_SPLIT", feature_request_id=uuid.uuid4())
    outsider = f"notACP:{uuid.uuid4()}"
    with psycopg.connect(cp.conninfo(), autocommit=True) as conn:
        forge(conn, orphan)
        forge(conn, outsider)
        try:
            seen = pg_orphan_thread_ids(engine)
            assert orphan in seen, f"造出来的孤儿入口读不到 ⇒ 这条 SQL 圈定的不是它：{seen}"
            removed = pg_purge_orphan_threads(engine)
            assert orphan in removed, removed
            assert orphan not in pg_orphan_thread_ids(engine), "清完还在 ⇒ 出口那一半是空的"
            left = conn.execute(
                "select count(*) from checkpoints where thread_id = %s", (outsider,)
            ).fetchone()[0]
            assert left == 1, f"非 acp 前缀的现场被删了 {1 - left} 行 ⇒ 清理越界"
        finally:
            for tid in (orphan, outsider):
                chaos.kill_graph_state(conn, tid)
            assert conn.execute(
                "select count(*) from checkpoints where thread_id in (%s, %s)", (orphan, outsider)
            ).fetchone()[0] == 0


def test_the_exit_half_is_idempotent_when_there_is_nothing_to_clean(engine):
    """没有孤儿时出口那一半必须**不碰库**地返回空名单（而不是"删了 0 行也算成功"）。

    这条看着像重复，实际挡的是：把清理写成无条件 `delete ... where true` 的实现，在空库上照样"成功"，
    而在有别人现场的库上就是一次越界删除 —— 与 `kill_graph_state` 那句 `thread_required` 的拒绝同一条理由。
    """
    assert pg_orphan_thread_ids(engine) == (), "这一发要在干净库上跑，否则它证的不是幂等"
    assert pg_purge_orphan_threads(engine) == (), "没东西可清时不该返回一个非空名单"


# ---------------------------------------------------------------- 三、库级不变量


def test_no_orphan_thread_has_been_left_by_the_suites_that_ran_before_this_one(engine):
    """跑到这一发时孤儿读数必须是 0 —— 谁在前面漏了一个 thread，这里就点名是谁。

    这条不是重复节 二（那一发只证机制）。它证的是库级事实：排在本发之前的那些套件（按当前收集顺序 =
    `contract` + `e2e` + `guards` 的前半，2026-10-16 实测）每一个跑过受理图的用例都自己收尾过。
    **它盖不到本发之后的套件** —— 那半边由两处兜住：下一发跑之前的**入口读数**（下面那一发用例就是读它），
    以及"整套跑完三张表读 0"这条复算（读数与命令见 `evidence/phase-12.md §十三`）。
    把这条写成"整套都不许留"是要撒谎，所以写成它能证的那一句。
    """
    leftovers = pg_orphan_thread_ids(engine)
    assert leftovers == (), f"前面的套件留下了 {len(leftovers)} 个孤儿 thread：{list(leftovers[:5])}"


def test_the_previous_run_handed_this_one_a_clean_checkpointer(engine, pg_tables_empty_at_entry):
    """上一发交过来的时候就该是干净的：入口读数必须为空（它证的正是"出口那一半真的在跑"）。

    为什么单独立一条而不是靠入口 `pytest.fail`：孤儿 thread 不让任何用例出错（thread 号由需求单 UUID
    推，撞不上），拿它把整轮 1685 条挡在门外是把"打扫"升级成"停机" —— 所以入口只做**自疗 + 记录**，
    把"上一发漏了"这件事交给一条会红的用例来报。红的是用例，不是整轮，且现场仍被清干净。
    这一发也是 M46（把出口那一半摘掉）的证人：那一发让 thread 累积 ⇒ 下一轮的这里就红。
    """
    assert pg_tables_empty_at_entry.orphans_at_entry == (), (
        f"开跑时 checkpointer 里已有 {len(pg_tables_empty_at_entry.orphans_at_entry)} 个孤儿 thread"
        f" ⇒ 上一发的出口清理没跑：{list(pg_tables_empty_at_entry.orphans_at_entry[:3])}"
    )
