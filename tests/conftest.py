"""pytest 分档：plan.md:48 定的 fast / guards 双档。

本文件属 **Checkpoint 0 未通过 · 基础设施准备件**，不占 Phase 1 任何任务编号，
也不产生"Phase 1 已开始"的读数——它只定义档位怎么切，`tests/` 下此刻还没有任何用例。

口径（与 `quickstart.md §3` 的先跑集一致）：

- `guards` = `tests/guards/` 一档，宪法判据用例（三岗互斥、只读岗、产物有效性、恢复幂等、
  口径分列、禁用命名）。
- `fast`   = `tests/guards/` + `tests/contract/`，即 quickstart §3 那条
  `pytest -q -v tests/guards tests/contract`，秒级、不碰 PG 与外部凭据。
- `tests/integration/`、`tests/e2e/` **不打 fast** ⇒ `-m fast` 天然不会捞到需要 PG /
  三个外部 Agent 的用例，Checkpoint 0 未闭时也不会被误跑成"全绿"。

档位由**目录**决定而非装饰器：写用例的人不需要记得加 mark，漏标不会让它跳过分档；
代价是新增顶层目录时要回来补一行映射。
"""

import subprocess
from pathlib import Path

import pytest

TIER_ROOT = Path(__file__).resolve().parent

FAST_DIRS = ("guards", "contract")
GUARDS_DIRS = ("guards",)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "fast: 秒级档 = tests/guards + tests/contract（quickstart §3 先跑集）"
    )
    config.addinivalue_line(
        "markers", "guards: 宪法判据档 = tests/guards"
    )


def pytest_collection_modifyitems(items):
    for item in items:
        try:
            rel = Path(item.path).resolve().relative_to(TIER_ROOT)
        except ValueError:
            continue
        dirs = rel.parts[:-1]
        top = dirs[0] if dirs else ""
        if top in FAST_DIRS:
            item.add_marker(pytest.mark.fast)
        if top in GUARDS_DIRS:
            item.add_marker(pytest.mark.guards)


# ------------------------------------------------------------------ 共用现场：临时总线根（T109 起）


#: 子进程那一半的**写入**口径：`PYTHONIOENCODING=<编码>:<错误处理>`。
#: 为什么调用点必须显式给：解码只发生在父进程读管道时，而本机区域编码是 cp936 ⇒ 不带这个变量的
#: 子进程用 cp936 写 stdout，父进程却按 `encoding="utf-8"` 去读 ⇒ 中文原样变成 `\ufffd`。
#: 2026-10-20 全量实测到的就是这一发：`test_recover_from_snapshot.py::test_the_child_sees_the_parent_environment`
#: 红在"仓库路径里那几个中文"上，而它读的是 `PYTHONPATH`。值的两半（编码与错误处理）由
#: `tests/guards/test_subprocess_decoding.py` 拿去与 `cadence.run_git` 钉的那两个字面量对读。
CHILD_STDIO_SPEC = "utf-8:replace"
CHILD_STDIO_ENV = {"PYTHONIOENCODING": CHILD_STDIO_SPEC}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return done.stdout


class BusRoot:
    """一个临时总线根，按需为某个 `project_key` 建出 `<bus_root>/<project_key>` 并 init 成真仓库。

    为什么这个助手放在 `conftest` 而不是各测试文件里：T109 之后 `_normalize` 要真打一拍
    （`archive/root.require_archive_root()` + `archive/cadence.commit_archive_point()`），
    于是"跑过一次拆单"这件事多了一个前置条件 —— 存档根存在且它自己是一个仓库的顶层。
    各文件各写一份 `git init` 会带来两种真实的错法：一种忘了设身份（退 128，看起来像机制坏了），
    另一种忘了 init（于是 git 向上找到**平台自己的仓库**并把整棵工作树提交掉）。
    身份写在**那个临时仓库自己的** `.git/config` 里：那是搭现场，不给产品代码补参数，
    也不碰用户任何一份 git 配置（口径同 `cadence` 模块头"身份从哪来"那一条）。
    """

    def __init__(self, root: Path) -> None:
        self.path = root

    def with_project(self, project_key: str) -> Path:
        project = self.path / project_key
        project.mkdir(parents=True, exist_ok=True)
        _git(project, "init", "-q", ".")
        _git(project, "config", "user.email", "probe@invalid")
        _git(project, "config", "user.name", "probe")
        return project


@pytest.fixture
def bus(tmp_path: Path) -> BusRoot:
    """临时总线根（每用例一个新 `tmp_path`，用例之间不共享存档历史）。"""
    root = tmp_path / "AI项目台"
    root.mkdir()
    return BusRoot(root)


# ---------------------------------------------------- 共用守卫：PG 档入口的十张表必须为空（owner 2026-10-16 裁决 3 + 裁决 Q1）

#: 第一组是**注册面**（EN-03 / EN-04 / EN-05）。它们的规范是"测试进来时为空"：
#: 各文件的 PG 夹具自己 `seed_builtin_roles()` 再靠**会话回滚**收摊，所以只要有人往库里提交了行，
#: 下一次 PG 档就会在 `pk_agent_unit` 上撞重复键 —— 2026-10-16 实测：一次 `python -m acp.run` 冒烟
#: （走 `persistence/session.new_session`，正常退出即 commit）让全量档读出 **53 failed / 103 errors**，
#: 第一条错离根因已经很远了。这条守卫把那种情况从"第 53 个用例才发现"改成"第一个用例之前就停下"。
REGISTRY_TABLES = ("agent_unit", "capability_manifest", "role_spec")
#: 第二组是裁决 Q1 新扩的：一次真 CLI 冒烟会**提交**行的那 7 张（受理段 + 卡点 + 存档点 + 留痕）。
#: 裁决理由原文："守卫目的就是进入时断言干净 ⇒ 会落行的表不查，一次意外污染 `task_card` 就静默进入"。
#: **没扩到全部 14 张**：另外那 4 张（`review_verdict / risk_rule / autonomy_state / cost_record`）
#: 今天的 CLI 冒烟不写它们，加进去只是让守卫自己变宽 ⇒ 扩表的依据是"谁会写"，不是"表存在"。
CLI_SMOKE_TABLES = (
    "acceptance_map",
    "approval_ticket",
    "event_log",
    "feature_request",
    "resolution_event",
    "snapshot",
    "task_card",
)
#: 点名时取**主键列**，不取 `select *`（各表列数不同，解包会先炸在守卫自己身上）。
#: `task_card` 的主键是 `(feature_request_id, id)` 复合键，这里取业务键 `id`（报出来是 `T01` 那种人认得的号）。
#: `event_log.id` 是自增 `BigInteger`，`str()` 之后照样能点名。
PG_ENTRY_KEY_COLUMN = {
    "acceptance_map": "id",
    "agent_unit": "agent_id",
    "approval_ticket": "id",
    "capability_manifest": "agent_id",
    "event_log": "id",
    "feature_request": "id",
    "resolution_event": "id",
    "role_spec": "role_id",
    "snapshot": "id",
    "task_card": "id",
}
#: **枚举顺序按表名字母序**（owner 裁决 Q1 的"不硬编码枚举顺序"）：两组谁在前不参与判定，
#: 报错正文里出现的顺序因此是稳定的，而书写顺序改了不会影响行为。
PG_ENTRY_TABLES = tuple(sorted(PG_ENTRY_KEY_COLUMN))

#: —————————————————————————————— Q4 裁决（owner 2026-10-16 23:0x）：入口断言干净 / 出口清理干净
#: 与上面那十张表对称的一半。**为什么需要它而不是"靠测试事务回滚"**：checkpointer 走的是另一条
#: autocommit 连接（`persistence/checkpointer.py` 的 `open_connection()`），会话夹具的
#: `trans.rollback()` 发不到它 ⇒ 每发跑过受理图的用例都会在 `checkpoints` 里留下一个 thread，
#: 而它对应的那行 EN-01 早就随事务消失了。本轮实测到的基线正是这样：13 个孤儿、51/126/67 行。
#:
#: 孤儿的定义只看一件事：`thread_id` 的第 3 段（需求单号）在 `feature_request` 里没有行
#: （拼接的产地是 `checkpointer.thread_id_for()`：`acp:<卡点类>:<需求单号>[:<卡号>]`）。
#: 这个 SQL 只用于**读**；删一律走 `chaos.kill_graph_state` —— 全仓唯一一条删 thread 现场的路径，
#: 它同时保证被删的表名单与演练工具共用同一份（`chaos.THREAD_TABLES`，顺序是子表在前）。
PG_ORPHAN_THREAD_SQL = (
    "select distinct thread_id from checkpoints "
    "where thread_id like 'acp:%' "
    "and not exists (select 1 from feature_request f "
    "where f.id::text = split_part(thread_id, ':', 3)) "
    "order by 1"
)


def pg_orphan_thread_ids(engine) -> tuple[str, ...]:
    """现在这一刻的孤儿 thread 名单（入口守卫与出口清理读的是同一条 SQL，不各写一份判定）。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        return tuple(str(row[0]) for row in conn.execute(text(PG_ORPHAN_THREAD_SQL)))


def pg_purge_orphan_threads(engine) -> tuple[str, ...]:
    """出口清理：把孤儿逐个交给 `chaos.kill_graph_state`，返回删掉的 thread 号。

    刻意**不是** `delete from checkpoints`：整表清空会把不属于本测试栈的 thread（非 `acp:` 前缀）
    和已有真 EN-01 行的现场一起抹掉，而"删掉却看不出删了什么"正是 `kill_graph_state` 那句
    `thread_required` 拒绝要防的形状。逐 thread 删还有一层好处：读数能报到 thread 粒度。
    """
    from contextlib import closing

    import psycopg

    from acp import chaos
    from acp.persistence import checkpointer as cp

    threads = pg_orphan_thread_ids(engine)
    if not threads:
        return ()
    with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as raw:
        for tid in threads:
            chaos.kill_graph_state(raw, tid)
    return threads


@pytest.fixture(scope="session", autouse=True)
def pg_tables_empty_at_entry():
    """带 `ACP_DB_DSN` 的那一档**开跑前**断言这十张表为空；缺 DSN 时本守卫不碰库（照跑）。

    为什么挂在 session 而不是每个用例上：这十张表的状态是**库级**的，逐用例查十次要乘 1600 多次往返，
    而污染只会发生一次（有人跑了真 CLI），一次查询足够。

    为什么"缺 DSN 不 skip 而是**空过**"：本守卫不是被测对象，它是环境检查；抽掉 DSN 时那些用例本来
    就各自 skip，把它变成一条 skip 会让"这一档没跑守卫"被读成"守卫跳过了 ⇒ 可能有问题"。
    注意这一支必须 `yield` 一次再返回，不能裸 `return`：本夹具从 Q4 起是生成器（有出口那一半），
    生成器夹具在 yield 之前就 return 时 pytest 会在**每一个**用例的 setup 上报
    `ValueError: … did not yield a value` —— 抽 DSN 那一档会整档炸掉（本轮就撞过一次，见 §十三 W28）。

    十张表都查、都要**计数**：读数不为零的那张进 `found`。表名或点名列写错时守卫自己会炸
    （`select count(*) from <不存在的表>` 是 ProgrammingError），所以"这张没被查到"不存在静默路径。

    **出口那一半（owner 2026-10-16 裁决 Q4）**：跑完之后把 checkpointer 里的孤儿 thread 清掉。
    这一半不是"顺手打扫"，是因为那批行**不会自己消失** —— checkpointer 用另一条 autocommit 连接，
    测试事务回滚发不到它，所以每次跑 PG 档都在累积（本轮实测基线：13 个孤儿 / 51+126+67 行）。
    入口与出口挂在同一个夹具上是有意的：守卫"进来时干净"，清理"出去时干净"，缺一半就会有一种
    "上一发留下的东西被下一当成污染"或反过来"这一发漏的没人管"的缝。
    """
    import os
    import warnings
    from types import SimpleNamespace

    from acp.config.loader import DSN_ENV

    if not os.environ.get(DSN_ENV):
        # 生成器夹具不能裸 `return`（见上面 docstring 那一段）：空过一次，让这一档照常跑完。
        yield SimpleNamespace(orphans_at_entry=())
        return
    from sqlalchemy import create_engine, text

    from acp.config.loader import db_url

    registry = set(REGISTRY_TABLES)
    smoke = set(CLI_SMOKE_TABLES)
    if registry & smoke or (registry | smoke) != set(PG_ENTRY_TABLES):
        pytest.fail(
            f"守卫自己的两张名单不自洽 ⇒ 该查哪些表这件事已经不可信："
            f"注册面 ∩ 冒烟面 = {sorted(registry & smoke)}，"
            f"并集与枚举表集合的差 = {sorted((registry | smoke) ^ set(PG_ENTRY_TABLES))}",
            pytrace=False,
        )

    engine = create_engine(db_url(), future=True)
    found: dict[str, tuple[int, list[str]]] = {}
    try:
        with engine.connect() as conn:
            for table in PG_ENTRY_TABLES:
                column = PG_ENTRY_KEY_COLUMN[table]
                count = conn.execute(text(f"select count(*) from {table}")).scalar_one()  # noqa: S608
                if not count:
                    continue
                samples = [
                    str(value)
                    for (value,) in conn.execute(
                        text(f"select distinct {column} from {table} limit 5")  # noqa: S608 名字来自上面的常量表
                    )
                ]
                found[table] = (int(count), samples)
    finally:
        engine.dispose()
    if found:
        readings = "\n".join(
            f"    {table} = {count} 行，样例 {PG_ENTRY_KEY_COLUMN[table]} ∈ {samples}"
            for table, (count, samples) in found.items()
        )
        hit_registry = sorted(set(found) & registry)
        pytest.fail(
            f"PG 档入口守卫：十张表里 {len(found)} 张非空 ⇒ 先停下而不是跑完再归因\n{readings}\n"
            f"  分组：注册面命中 {hit_registry or '（无）'}；CLI 冒烟面命中 {sorted(set(found) & smoke)}\n"
            f"  成因：`python -m acp.run` 走 `persistence/session.new_session`（正常退出即 commit）——"
            f"注册面那三张各写 3 行，业务那七张写的是需求单 / 四张卡 / A 类单据 / 结论流水 / 验收映射 /"
            f" 存档点 / 留痕\n"
            f"  复原：注册面按主键删回空态即可（任何一次 `seed_builtin_roles()` 都能原样重建，删的不是独一份的现场）；"
            f"业务七张要**按那一发的 `feature_request_id` 圈定再删**，别整表清空 —— 复算式见"
            f" `evidence/phase-12.md` §十 的 C21（14 张表逐张报数，终态全 0）",
            pytrace=False,
        )

    # —— 出口那一半（Q4）：入口断言干净 / 跑完清理干净，两张对称的半边挂在同一个夹具上。
    #
    # **入口这里为什么是"先自疗再断言清掉的那刻"，而不是直接 fail**：孤儿 thread 不会让任何用例出错
    # （thread 号由需求单 UUID 推，撞不上），而一次被 Ctrl-C 打断的运行就会留下整批现场 ——
    # 拿它把后面每一轮 1685 条挡在门外，是把"打扫"升级成"停机"。业务面那一半（上面）该 fail，
    # 因为污染会让用例**真错**（UniqueViolation 一路炸下去），两半的严重性不是一回事。
    # 停机风险换掉之后，"上一发漏了"这件事仍然有人管：入口读数交给 `orphans_at_entry` 交出去，
    # 由 `tests/guards/test_pg_checkpoint_hygiene.py` 那条用例断言它必须是空 —— 红的是用例，不是整轮。
    thread_engine = create_engine(db_url(), future=True)
    try:
        at_entry = pg_orphan_thread_ids(thread_engine)
        if at_entry:
            warnings.warn(
                f"PG 档入口：checkpointer 里有 {len(at_entry)} 个孤儿 thread ⇒ 已由入口清理（自疗），"
                f"样例 {list(at_entry[:3])}；上一发跑完没清干净这件事由 hygiene 用例报红",
                stacklevel=1,
            )
            pg_purge_orphan_threads(thread_engine)
        leftovers = pg_orphan_thread_ids(thread_engine)
        if leftovers:
            pytest.fail(
                f"PG 档入口守卫：清理之后仍有 {len(leftovers)} 个孤儿 thread ⇒ "
                f"删除路径本身坏了（不是 residue，是缺陷）：{list(leftovers[:5])}\n"
                f"  判定：thread_id 第 3 段（需求单号）在 `feature_request` 里没有行；"
                f"删除只该走 `chaos.kill_graph_state` —— 复算见 evidence/phase-12.md §十三",
                pytrace=False,
            )
        yield SimpleNamespace(orphans_at_entry=at_entry)
    finally:
        pg_purge_orphan_threads(thread_engine)
        thread_engine.dispose()



