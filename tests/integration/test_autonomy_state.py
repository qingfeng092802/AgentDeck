"""EN-16 `autonomy_state` 的读写判据（T101 的落库半边 + T102 的建行 / 读取 / 改档半边）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_registry_persist.py`），
且**一律不 commit**：外层事务 + 结束回滚 ⇒ 库保持可重复跑。
为什么只能连 PG：`mode` 是 PG 原生枚举（`autonomy_mode_type`），sqlite 上建不出这一列，
"第三档"在假库里反而会写成功 ⇒ 整档留在 integration 层，fast 档不捞它。

T101 那一半要回答四件事：

1. 重载一次，EN-16 里那一列**确实变成新指纹**（而不是"loader 算得出但没人写"）；
2. 它**只写这一列** —— 档位 / 改档人 / 改档时刻 / 最近拦截时刻四个字段都不许被顺手抹掉。
   这是 Phase 7 的 P1-5（`review.py` 顺手写 `review_round` 吃掉验收轮次预算）同一条失效形状，
   所以用了同一种钉子：AST 把"这个函数写了哪些属性"当集合断言；
3. **缺行时报错而不擅自建行**（重载不是改档，也不是建档）；
4. 改了清单文件之后**再重载一次**，库里跟着变（`quickstart.md` 排障表那一行的正向半边）。

T102 那一半（owner 2026-10-15 的"C + E 结合"处置表）要回答四件事，四条判据一条不缺：

5. **幂等**：`ensure_autonomy_state()` 连调两次只建一行，第二次**不改已有行的任何一个字段**
   （连"顺手把指纹补新"都不许 —— 那是另一个写者的活）；
6. **两档**：`FULL` / `SAFE` 之外的第三个值在碰到库之前就被拒，且拒了就没写；
7. **读取无副作用**：`read_autonomy_state()` 在无行时返回 `None` 而库里行数不变；
8. **越权**：EN-03 注册过的 `agent_id` 发起的改档抛错，且**档位保持原值**（拒而不改才是拒）。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url, load_danger_list
from acp.persistence import autonomy
from acp.persistence.enums import values_of
from acp.persistence.models_core import AgentUnit
from acp.persistence.models_registry import AutonomyState

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（EN-16 那一列的落库只能在库里读）",
)

PROJECT = "acp-self-host"

#: 建行时的"旧"读数。四个字段各给一个**互不相同**的值 ⇒ 任何一个被抹掉都能被断出来
#: （全用同一个时刻的话，"updated_at 被刷成 now"这种缺陷就看不见）。
MODE_BEFORE = "SAFE"
VERSION_BEFORE = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
UPDATED_BY_BEFORE = "user-1"
T_UPDATED = dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.timezone.utc)
T_INTERCEPT = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)

REPO_ROOT = Path(__file__).resolve().parents[2]

AUTONOMY_SOURCE = REPO_ROOT / "src" / "acp" / "persistence" / "autonomy.py"

#: 随仓清单的位置（第三节要复制一份"只改注释"的副本，读原文用同一份）。
DANGER_LIST_FILE = REPO_ROOT / "config" / "danger_list.yaml"


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


def _seed(session: Session) -> AutonomyState:
    row = AutonomyState(
        project_key=PROJECT,
        mode=MODE_BEFORE,
        danger_list_version=VERSION_BEFORE,
        updated_by=UPDATED_BY_BEFORE,
        updated_at=T_UPDATED,
        last_intercept_at=T_INTERCEPT,
    )
    session.add(row)
    session.flush()
    return row


def _reload(session: Session) -> AutonomyState:
    """从库里重读那一行（`expire_all` 保证读的是 PG 的返回而不是 identity map 里的缓存）。"""
    session.expire_all()
    row = session.get(AutonomyState, PROJECT)
    assert row is not None
    return row


# ── 一、写这一列 ─────────────────────────────────────────────────────────────


def test_reload_records_the_loaded_fingerprint(session: Session, tmp_path: Path) -> None:
    """重载一次 ⇒ EN-16 的 `danger_list_version` 变成当前清单的指纹（不是 loader 里的一个数）。"""
    _seed(session)
    path = tmp_path / "danger_list.yaml"
    path.write_text("- pattern: rm -rf\n", encoding="utf-8")
    loaded = load_danger_list(path)

    autonomy.record_danger_list_version(session, PROJECT, loaded)

    assert _reload(session).danger_list_version == loaded.fingerprint


def test_the_other_four_columns_are_untouched(session: Session) -> None:
    """**这一条是本文件的主判据**：档位、改档人、改档时刻、最近拦截时刻都不许被"顺手"改掉。

    失效形状不是假设：重载清单是一次**不是改档**的动作。如果它把 `mode` 一起写回默认，
    使用者切到 SAFE 之后一次重载就把档位刷回去了；如果它写 `updated_at`，
    "最近一次改档"这个读数就变成了"最近一次读文件"。两条都在这一条用例里断住。
    """
    _seed(session)
    autonomy.record_danger_list_version(session, PROJECT, load_danger_list())
    after = _reload(session)
    assert after.mode == MODE_BEFORE
    assert after.updated_by == UPDATED_BY_BEFORE
    assert after.updated_at == T_UPDATED
    assert after.last_intercept_at == T_INTERCEPT


def test_a_repeated_reload_of_the_same_list_is_idempotent(session: Session) -> None:
    """同一份清单重载两次，落库值不变（"重载"不是"换版"，审计侧不该看到两次变更）。"""
    _seed(session)
    first = load_danger_list()
    autonomy.record_danger_list_version(session, PROJECT, first)
    autonomy.record_danger_list_version(session, PROJECT, load_danger_list())
    assert _reload(session).danger_list_version == first.fingerprint


def test_editing_the_file_and_reloading_moves_the_recorded_version(
    session: Session, tmp_path: Path
) -> None:
    """`quickstart.md` 排障表"改了清单但没重载 ⇒ 用的还是旧版"那一行的**正向半边**：
    改文件 + 再走一次 loader ⇒ 库里跟着变，且旧值与新值不同（否则这条断言是空的）。
    """
    _seed(session)
    path = tmp_path / "danger_list.yaml"
    path.write_text("- pattern: rm -rf\n", encoding="utf-8")
    autonomy.record_danger_list_version(session, PROJECT, load_danger_list(path))
    before = _reload(session).danger_list_version

    path.write_text("- pattern: rm -rf\n- pattern: sudo\n", encoding="utf-8")
    autonomy.record_danger_list_version(session, PROJECT, load_danger_list(path))
    after = _reload(session).danger_list_version

    assert after != before
    assert after == load_danger_list(path).fingerprint


# ── 二、缺行不建（默认档位没有权威出处）──────────────────────────────────────


def test_a_missing_row_is_reported_and_no_row_is_created(session: Session) -> None:
    """缺行 ⇒ `AutonomyStateMissing`，且库里仍然是 0 行（不插一行"看着像 SAFE"的默认读数）。"""
    with pytest.raises(autonomy.AutonomyStateMissing):
        autonomy.record_danger_list_version(session, PROJECT, load_danger_list())
    assert int(session.scalar(select(func.count()).select_from(AutonomyState)) or 0) == 0


def _written_attributes(source: str, function_name: str) -> set[str]:
    """收集**指定函数**里"写到某个对象属性上"的那些属性名（`row.x = …` 这种）。

    范围必须钉到函数：整模块扫会把 `AutonomyStateMissing.__init__` 里的
    `self.project_key = …` 也算进来（本轮实测就是这样红的），而那条不是"多写一列"。
    同名函数出现两次也直接判红 —— 扫到哪一个是由源码顺序决定的，那会让断言变成假判据。
    放在测试侧而不是 `autonomy.py` 里：这是判据的实现，不是被测行为的一部分
    （同一选择见 `tests/guards/test_review_accept.py` 第九节）。
    """
    found = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    assert len(found) == 1, f"{function_name} 在源码里出现 {len(found)} 次，扫描器不知道该认哪一个"
    return {
        node.attr
        for node in ast.walk(found[0])
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }


def test_the_writer_owns_exactly_one_column() -> None:
    """AST 钉住"这个函数写了哪些属性"= 一个集合，多写一列就红。

    与 `tests/guards/test_review_accept.py` 第九节那条同源（P1-5 的教训：判据面与改动面不重叠时，
    "上一轮还是绿的"这条读数过期得最快）。**正对照在同一处**：注入三种"多写一列"的源码，
    扫描器必须都能抓出来，否则这个 0 只是扫描面本来就是空的。
    """
    source = AUTONOMY_SOURCE.read_text(encoding="utf-8")
    assert _written_attributes(source, "record_danger_list_version") == set(autonomy.OWNED_COLUMNS)
    for injected in (
        "def record_danger_list_version(a, b, c):\n    c.danger_list_version = 1\n    c.mode = 'SAFE'\n",
        "def record_danger_list_version(a, b, c):\n    c.danger_list_version = 1\n    c.updated_at = 2\n",
        "def record_danger_list_version(a, b, c):\n    row = c\n    row.danger_list_version = 1\n    row.last_intercept_at = 2\n",
    ):
        assert _written_attributes(injected, "record_danger_list_version") != {
            "danger_list_version"
        }, injected


# ── 三、落库的是哪一个读数 ───────────────────────────────────────────────────


def test_the_column_follows_the_semantic_fingerprint_not_the_byte_hash(
    session: Session, tmp_path: Path
) -> None:
    """EN-16 那一列跟的是**语义**指纹：注释改动换版了文件、没换策略。

    判据写成关系式而不是比对两个常量：把随仓清单复制一份、只加一行注释 ⇒
    两份的 `fingerprint` 相等而 `source_sha256` 不等 ⇒ 记其中一份之后，
    库里那一列必须等于"两边共同的那一个值"。这一条红 = 有人把字节哈希写进了 EN-16，
    那时的表象是"清单天天换版而策略没变"，靠肉眼从审计读数里分不出来。
    """
    shipped = load_danger_list()
    copy = tmp_path / "danger_list.yaml"
    copy.write_text(f"# 只是注释，不是策略\n{shipped_source_text()}", encoding="utf-8")
    commented = load_danger_list(copy)
    assert commented.fingerprint == shipped.fingerprint
    assert commented.source_sha256 != shipped.source_sha256

    _seed(session)
    autonomy.record_danger_list_version(session, PROJECT, commented)
    assert _reload(session).danger_list_version == shipped.fingerprint


def shipped_source_text() -> str:
    """随仓清单的原文（只在上一条里用来做"只改注释"的那份副本）。"""
    return DANGER_LIST_FILE.read_text(encoding="utf-8-sig")


# ── 四、建行：ensure 的幂等（T102 判据①）────────────────────────────────────

#: 改档时刻专用值：与 `_seed()` 里的 `T_UPDATED` 不同 ⇒ "改档有没有真的写时刻"读得出来。
T_NEW = dt.datetime(2026, 10, 15, 10, 0, tzinfo=dt.timezone.utc)

#: 使用者侧的主体名（与 `PLATFORM_PRINCIPAL` 不同类：一个是人，一个是平台自己）。
HUMAN = "user-1"


def _count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(AutonomyState)) or 0)


def _small_list(tmp_path: Path) -> object:
    """一份**与随仓清单不同**的清单（一条命中项），用来让"第二次调用顺手补写指纹"变得可见。"""
    path = tmp_path / "danger_list_other.yaml"
    path.write_text("- pattern: sudo\n", encoding="utf-8")
    return load_danger_list(path)


def _register_agent(session: Session, agent_id: str) -> None:
    """往 EN-03 插一支注册过的工具。`agent_id` 是"机器身份"这件事的唯一库侧载体，
    所以越权那组用例必须走真表，而不是在测试里编一个"看起来像 agent 的名字"。
    """
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


def test_ensure_creates_the_first_row_at_the_safe_tier(session: Session, tmp_path: Path) -> None:
    """owner 2026-10-15 裁决 2：新项目首行 `mode = SAFE`，且是**应用层**建行（没有迁移、没有 `server_default`）。

    这里把 `"SAFE"` 写成字面串而不是取 `autonomy.DEFAULT_MODE` —— 拿实现常量验实现，
    就把"有人把默认档改成 FULL"这条变异放过了（同一个理由见下一节 `test_there_are_exactly_two_tiers`）。
    """
    before = _count(session)
    loaded = _small_list(tmp_path)

    autonomy.ensure_autonomy_state(session, project_key=PROJECT, danger_list=loaded, now=T_UPDATED)

    row = _reload(session)
    assert row.mode == "SAFE"
    assert row.danger_list_version == loaded.fingerprint
    assert row.updated_by == autonomy.PLATFORM_PRINCIPAL
    assert row.updated_at == T_UPDATED
    assert _count(session) == before + 1


def test_ensure_is_idempotent_and_the_second_call_writes_nothing(
    session: Session, tmp_path: Path
) -> None:
    """判据①「连调两次只建一行」的两半：行数只 +1，**且第二次调用一个字段都不改**。

    第二次的入参故意全换：时刻换成 `T_NEW`、清单换成另一份（指纹不同）。
    这样"第二次顺手刷默认档 / 补新指纹 / 更新建档时刻"这三种写法都会被抓出来，
    而行数只 +1 这一条单独是抓不到它们的（它们都改的是同一行）。
    """
    before = _count(session)
    first = load_danger_list()
    autonomy.ensure_autonomy_state(session, project_key=PROJECT, danger_list=first, now=T_UPDATED)

    autonomy.ensure_autonomy_state(
        session, project_key=PROJECT, danger_list=_small_list(tmp_path), now=T_NEW
    )

    assert _count(session) == before + 1
    row = _reload(session)
    assert row.mode == "SAFE"
    assert row.danger_list_version == first.fingerprint
    assert row.updated_at == T_UPDATED


def test_ensure_does_not_reset_a_handed_tier(session: Session, tmp_path: Path) -> None:
    """已有行是 `FULL` 时，建档路径 MUST NOT 把它刷回默认档。

    这是上一条的**反向半边**，也是真正会伤使用者的那条：selfcheck 天天跑，
    只要 `ensure_` 里出现一次"补写默认值"，使用者切的 FULL 档就会在下一次启动检查时消失，
    而且表面上"没报错"。所以先建好行再把档位改到域里的**另一档**（测试侧直接写 ORM，
    不借 `set_autonomy_mode` —— 那是第六节要单独判的东西，两件事不互证）。
    """
    other = next(value for value in values_of("autonomy_mode") if value != "SAFE")
    _seed(session)
    seeded = _reload(session)
    seeded.mode = other
    session.flush()

    autonomy.ensure_autonomy_state(session, project_key=PROJECT, danger_list=_small_list(tmp_path))

    row = _reload(session)
    assert row.mode == other
    assert row.updated_at == T_UPDATED
    assert row.danger_list_version == VERSION_BEFORE


def test_read_is_a_pure_read_when_the_row_is_missing(session: Session) -> None:
    """判据③「读取无副作用」：无行 ⇒ `None`，库里行数一格没涨。

    owner 的处置表那行写的是"无行则返回 mode=SAFE（不建行，无副作用）"——
    "读成 SAFE"这件事归调用方用 `DEFAULT_MODE` 表达（唯一载体在 `persistence/autonomy.py`），
    本函数给的是"有没有这一行"这个事实。**这里刻意不断言 SAFE**：断它就等于把调用方的默认值
    塞进读取函数，而"不建行"这半条判据就会被"读时顺手建一行"冒充掉。
    """
    before = _count(session)
    assert autonomy.read_autonomy_state(session, project_key=PROJECT) is None
    assert _count(session) == before


def test_read_returns_the_stored_row_without_touching_it(session: Session) -> None:
    """有行时读取返回那一行本身，且读之后各列原样（读一次不该变成写一次）。"""
    _seed(session)
    row = autonomy.read_autonomy_state(session, project_key=PROJECT)
    assert row is not None
    assert row.mode == MODE_BEFORE
    assert row.updated_by == UPDATED_BY_BEFORE
    assert row.updated_at == T_UPDATED
    assert _reload(session).mode == MODE_BEFORE
    assert _count(session) == 1


# ── 五、两档：FULL / SAFE，第三值载入即拒（T102 判据②）───────────────────────


def test_there_are_exactly_two_tiers_and_the_default_is_one_of_them() -> None:
    """宪法「三」"只有两档，没有第三档"的代码侧读数 + 裁决 2 的字面默认值。

    域里一旦冒出第三个值，这条先红（而不是等某个端点把它当成合法档位放过去）；
    `DEFAULT_MODE` 单独钉成字面串，因为"默认档"是 owner 裁的，不是实现方挑的。
    """
    assert set(values_of("autonomy_mode")) == {"FULL", "SAFE"}
    assert len(values_of("autonomy_mode")) == 2
    assert autonomy.DEFAULT_MODE == "SAFE"


@pytest.mark.parametrize("third", ["SUPER", "safe", "FULL ", "", "AUTO", None])
def test_a_third_tier_is_rejected_before_it_reaches_the_database(
    session: Session, third: object
) -> None:
    """判据②：`autonomy_mode` 域外的值一律拒，且**拒了就是没写**。

    取值里有 `"safe"`（小写）和 `"FULL "`（尾随空白）两个诱饵：真库的枚举比较是按标签字面串来的，
    一旦代码里做了 `casefold()` 或 `strip()` 的"宽容归一"，就会出现"库里存的是 FULL、
    文档里说只有两档"这种读不出的漂移 —— 这两个串就是为了让那种写法当场红。
    `None` 走的是同一条判定（`in` 判据对非串同样成立），不是另一份逻辑。
    """
    _seed(session)
    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(session, third, HUMAN, project_key=PROJECT)  # type: ignore[arg-type]
    assert exc.value.code == autonomy.MODE_NOT_ALLOWED

    row = _reload(session)
    assert row.mode == MODE_BEFORE
    assert row.updated_by == UPDATED_BY_BEFORE
    assert row.updated_at == T_UPDATED


def test_a_tier_change_writes_the_three_owned_columns_and_only_those(session: Session) -> None:
    """改档确实落库（不是只在内存里改了个 ORM 对象），且只落它那一组列。

    `_seed()` 给的五列是五个**互不相同**的值，所以"没跟着变"的读数才有意义：
    `danger_list_version` 归重载写者（裁决 3），`last_intercept_at` 归拦截写者（T104 / T105）。
    """
    _seed(session)
    autonomy.set_autonomy_mode(
        session, "FULL", HUMAN, project_key=PROJECT, now=T_NEW
    )

    row = _reload(session)
    assert row.mode == "FULL"
    assert row.updated_by == HUMAN
    assert row.updated_at == T_NEW
    assert row.danger_list_version == VERSION_BEFORE
    assert row.last_intercept_at == T_INTERCEPT


def test_the_tier_writer_is_allowed_to_write_the_same_value_twice(session: Session) -> None:
    """同档再切一次**也算一次改档**：`updated_at` / `updated_by` 跟着动，行数不变。

    这条是把"记的是动作还是差值"这件事钉住：使用者真的按了一次"切到 SAFE"，
    审计就该看到那一次（否则"改过但没变"这个态在库里是隐形的，而它恰好是排查现场要看的那一格）。
    """
    _seed(session)
    autonomy.set_autonomy_mode(session, MODE_BEFORE, HUMAN, project_key=PROJECT, now=T_NEW)
    row = _reload(session)
    assert row.mode == MODE_BEFORE
    assert row.updated_at == T_NEW
    assert _count(session) == 1


def test_mode_change_creates_the_row_when_absent(session: Session, tmp_path: Path) -> None:
    """owner 处置表的"改档端点 = 有行改行，无行建行"半边：无行时 upsert 直接以**请求的档位**建行。

    这里建行时用的是 `FULL`（请求值）而不是先建 `SAFE` 再改 —— 先建默认档再改会在同一事务里
    发两次写，且 `updated_at` 会被覆盖成"改档时刻"，那正好把"这一行什么时候出生的"抹掉。
    """
    before = _count(session)
    loaded = _small_list(tmp_path)
    autonomy.set_autonomy_mode(
        session, "FULL", HUMAN, project_key=PROJECT, danger_list=loaded, now=T_NEW
    )

    row = _reload(session)
    assert row.mode == "FULL"
    assert row.updated_by == HUMAN
    assert row.danger_list_version == loaded.fingerprint
    assert _count(session) == before + 1


def test_mode_change_refuses_to_invent_a_fingerprint_when_creating(session: Session) -> None:
    """无行又没交清单 ⇒ 拒（`danger_list_required`），**不拿占位串凑一行**。

    占位指纹进了审计表就再也读不出"当时是哪版清单"，而 EN-14 的拦截留痕正是按 `(agent_id, 指纹)`
    回溯的（T105）—— 那一条会跟着变成假数据。
    """
    before = _count(session)
    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(session, "FULL", HUMAN, project_key=PROJECT)
    assert exc.value.code == autonomy.DANGER_LIST_REQUIRED
    assert _count(session) == before


# ── 六、越权：改档主体只能是使用者（T102 判据④）─────────────────────────────


def test_a_registered_agent_cannot_change_the_tier(session: Session) -> None:
    """判据④：EN-03 注册过的 `agent_id` 发起改档 ⇒ 抛错，且档位保持原值。

    走真表而不是编一个"看起来像机器"的名字：`agent_id` 这件事的唯一库侧载体就是 EN-03。
    """
    _register_agent(session, "qoder")
    _seed(session)

    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(session, "FULL", "qoder", project_key=PROJECT)
    assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED

    row = _reload(session)
    assert row.mode == MODE_BEFORE
    assert row.updated_by == UPDATED_BY_BEFORE
    assert _count(session) == 1


def test_padding_the_agent_id_does_not_slip_past_the_principal_check(session: Session) -> None:
    """同一支工具写成 `" qoder "` 也一样拒。

    归一化只做**首尾空白**这一层（`strip()`），不做大小写 —— 大小写一归一就会出现
    "库里是 `Qoder`、判定里是 `qoder`"这种两边都自洽的漂移，而首尾空白没有这个代价。
    """
    _register_agent(session, "qoder")
    _seed(session)
    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(session, "FULL", "  qoder  ", project_key=PROJECT)
    assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED
    assert _reload(session).mode == MODE_BEFORE


def test_a_blank_principal_is_rejected(session: Session) -> None:
    """空 / 全空白主体一律拒：没有主体就没有审计行，"留空当不限制"是最典型的假开放。"""
    _seed(session)
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
            autonomy.set_autonomy_mode(session, "FULL", blank, project_key=PROJECT)
        assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED
    assert _reload(session).mode == MODE_BEFORE


def test_the_platform_can_create_the_row_but_cannot_change_the_tier(
    session: Session, tmp_path: Path
) -> None:
    """`PLATFORM_PRINCIPAL` 的**方向性**：建档收它，改档拒它。

    两条必须一起看才成立：只测"建档能用 platform"就漏了"平台以 platform 名义自切档位"，
    只测"改档不能用 platform"就漏了"启动路径建不出行"。这正是 FR-055 那句话的两半 ——
    "可切换"指使用者可切，不指平台可自切；而第一行确实是平台起的头，那一行的主体就该写平台自己。
    """
    created = autonomy.ensure_autonomy_state(
        session, project_key=PROJECT, danger_list=_small_list(tmp_path)
    )
    assert created.updated_by == autonomy.PLATFORM_PRINCIPAL

    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(
            session, "FULL", autonomy.PLATFORM_PRINCIPAL, project_key=PROJECT
        )
    assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED
    assert _reload(session).mode == "SAFE"


def test_an_automated_actor_is_rejected_when_the_caller_declares_it(session: Session) -> None:
    """`machine_principals` 这个接缝的两面，一起写才诚实。

    正面：调用方把自动主体的名字申报进来 ⇒ 拒。
    反面（**这一条是绿的**）：同一个串没申报时，本函数分不出来 ——
    MVP-1 没有认证，这里挡的是"披错身份误操作"，挡不住冒充（同 `api/approvals.py` 模块头第 2 条）。
    为什么不在本模块直接写死那个名字：它住在 `orchestrate/nodes/select_worker.py`，
    那边 import 了 `persistence.events` ⇒ 反过来 import 就是向上成环，
    而在这里再抄一遍就是给一个已有唯一载体的字面串加第二份。
    ⇒ **连带义务登记给 T103**：`api/autonomy.py` MUST 把 `{select_worker.AUTOMATED_ACTOR}` 传进来
    （`evidence/phase-08.md §六` 有一条对应的未闭项，关闭判据写在那儿）。
    """
    _seed(session)
    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.set_autonomy_mode(
            session,
            "FULL",
            "orchestrator",
            project_key=PROJECT,
            machine_principals=("orchestrator",),
        )
    assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED
    assert _reload(session).mode == MODE_BEFORE

    row = autonomy.set_autonomy_mode(session, "FULL", "orchestrator", project_key=PROJECT)
    assert row.mode == "FULL"


def test_the_creation_path_also_rejects_a_registered_agent(session: Session, tmp_path: Path) -> None:
    """建档路径同样不收 `agent_id`：否则"谁都没同意"的第一行会由一支工具签出来。

    与改档那条共用同一个判定函数，但**分开写用例**：两条路径的 `allow_platform` 不一样
    （建档收、改档拒），把两者合成一条就会在其中一处漏判据。
    """
    _register_agent(session, "doubao")
    before = _count(session)
    with pytest.raises(autonomy.AutonomyWriteRejected) as exc:
        autonomy.ensure_autonomy_state(
            session, project_key=PROJECT, danger_list=_small_list(tmp_path), updated_by="doubao"
        )
    assert exc.value.code == autonomy.PRINCIPAL_NOT_ALLOWED
    assert _count(session) == before


# ── 七、AST 半边：每个写者只写自己那组列（判据②③④的结构性钉子）──────────────


def test_each_new_writer_owns_exactly_its_own_column_set() -> None:
    """T102 三个函数的"写了哪些属性"钉成三个集合，多写一列就红。
    **【T105 追加第四个写者，原三行判据不动】** `record_intercept()` 认
    `INTERCEPT_OWNED_COLUMNS`（就一列），它那一组的越权注入在 `OVERREACHING` 末尾。

    与本文件 `test_the_writer_owns_exactly_one_column`、`tests/guards/test_review_accept.py` 第九节
    同源（P1-5 的教训：判据面与改动面不重叠时，"上一轮还是绿的"这条读数过期得最快）。
    **`ensure_autonomy_state` 与 `read_autonomy_state` 认空集**：建行走的是 `AutonomyState(...)`
    构造器（INSERT 给全部列，合法），所以这两个函数里一旦出现属性赋值，就只能是"给已有行补写列"
    或"读取顺手写了一格"，那两条分别由第五节的 `test_ensure_does_not_reset_a_handed_tier`
    与 `test_read_is_a_pure_read_when_the_row_is_missing` 从读数侧挡着。

    范围声明（免得这条被读成覆盖全函数）：AST 只看**属性赋值**，看不到构造器里给了哪几列，
    也看不到 `session.execute(update(...))` 这种写法 ⇒ 建行写了什么由第四、五节的读数用例管，
    两半合起来才是"只写自己那组列"。正对照在下一节：注入的越权写法必须都能被抓出来。
    """
    source = AUTONOMY_SOURCE.read_text(encoding="utf-8")
    assert _written_attributes(source, "set_autonomy_mode") == set(autonomy.MODE_OWNED_COLUMNS)
    assert _written_attributes(source, "ensure_autonomy_state") == set()
    assert _written_attributes(source, "read_autonomy_state") == set()
    # T105 的第四个写者（拦截时刻）：同一枚钉子，另一组列。它的行为侧判据在
    # `tests/guards/test_autonomy_intercept.py`，这里只钉"它有没有顺手改别的列"。
    assert _written_attributes(source, "record_intercept") == set(autonomy.INTERCEPT_OWNED_COLUMNS)


#: (函数名, 注入的越权写法, 多写的那一列)。注入里前几行就是合法集合，所以红点只能来自多写的那一列。
OVERREACHING = [
    (
        "set_autonomy_mode",
        "def set_autonomy_mode(a, b, c, **k):\n"
        "    r = a\n    r.mode = b\n    r.updated_by = c\n    r.updated_at = 1\n"
        "    r.danger_list_version = 'x'\n",
        "danger_list_version",
    ),
    (
        "set_autonomy_mode",
        "def set_autonomy_mode(a, b, c, **k):\n"
        "    r = a\n    r.mode = b\n    r.updated_by = c\n    r.updated_at = 1\n"
        "    r.last_intercept_at = 2\n",
        "last_intercept_at",
    ),
    (
        "ensure_autonomy_state",
        "def ensure_autonomy_state(a, **k):\n    r = a\n    r.mode = 'SAFE'\n",
        "mode",
    ),
    (
        "read_autonomy_state",
        "def read_autonomy_state(a, **k):\n    r = a\n    r.updated_at = 1\n",
        "updated_at",
    ),
    (
        # T105 的第四个写者。这一条的形状是"记一次拦截就把改档时刻一起刷了"——
        # 模块头那一段点名的正是它（`last_intercept_at` 不记"谁"，也不记"什么时候改的档"）。
        "record_intercept",
        "def record_intercept(a, b, **k):\n    r = a\n    r.last_intercept_at = 1\n"
        "    r.updated_at = 2\n    r.updated_by = 'qoder'\n",
        "updated_by",
    ),
]


@pytest.mark.parametrize(("function_name", "injected", "extra"), OVERREACHING)
def test_the_scanner_actually_catches_an_overreaching_writer(
    function_name: str, injected: str, extra: str
) -> None:
    """上面那条的**正对照**：把越权写法喂给扫描器，它必须点出多写的那一列。

    没有这一组，`== set()` 与 `== MODE_OWNED_COLUMNS` 这两个绿点就只是"扫描面本来为空"的读数
    （同一个教训见 `test_the_writer_owns_exactly_one_column` 的注入段）。
    """
    assert extra in _written_attributes(injected, function_name)



