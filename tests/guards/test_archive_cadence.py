"""T108：存档节律（FR-053｜R-14，宪法「三」"每轮开工前 / 完工后 MUST 自动 git commit"）。

档位：`tests/guards/`，混合文件。一~四 + 六的最后一条 + 八是纯函数 / 临时 git 仓库 / 源码 AST
（fast，不碰 PG）；五、六前半、七要真 PG（skip 挂在 `engine` fixture 上，口径同 T107 ——
不逐条挂 marker，免得新加一条要库的用例忘了挂就在无 PG 机器上红成假缺陷）。

**这八节各挡什么**（逐条对位 owner 给 T108 的那句原文与本轮的两条兑现义务）：

| 原文 / 义务 | 节 |
|---|---|
| 每轮开工前 / 完工后各一次自动 commit | 一、二 |
| 存档点可枚举（EN-12 `snapshot.ref` + `phase`） | 五 |
| 存档不替代三岗把关，反之亦然（D-12 明写） | 七 |
| T-15 那句"T108 落地时必须换成真实句柄" | 六 |
| 本机制不许成为第二条破坏性 git 通道 | 三 |
| 错误口径（只带退出码与原因词，不回显厂商文本） | 四 |
| 原则 I：句柄拼装一处、EN-12 写者一处 | 六、八 |
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import acp
from acp.archive import cadence
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import snapshots as snap
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot

#: 扫描根跟着**被 import 的那棵树**走（口径同 T106 的修正与 `evidence §十五` 那条 P2-1）：
#: 变异跑改的是副本，按字面路径拼出来的仓库根读不到副本，那两条 AST 判据就永远进不了账。
SRC_ROOT = Path(acp.__file__).resolve().parent

PROJECT = "acp-self-host"
OTHER_PROJECT = "acp-other"
CARD = "T01"
CARD2 = "T02"
NOW = dt.datetime(2026, 10, 15, 18, 30, tzinfo=dt.timezone.utc)
SHA_A = "a" * 40
SHA_B = "b" * 40
REF_A = f"git:{SHA_A}"
#: 假凭据串：形状命中 `loader.FORM_PATTERNS` 的 `sk-` 那条，只用来证明"git 的输出不进错误消息"。
FAKE_KEY = "sk-FAKEfakeFAKEfake0"


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def engine():
    """PG 那条理由挂在 fixture 上：没库时整条 skip（不是绿），不碰库的那几节照跑。"""
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG（存档点行与留痕只在库里读得出），整条 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


@pytest.fixture
def scope(session: Session) -> uuid.UUID:
    """一张需求单 + 两张卡 + **一行库里已有的临时句柄存档点**。

    那行临时句柄（`local:registry/snapshot-<id>`）不是偷懒复用 `graph.py` 的形状，是刻意放进去的：
    第六节要分清"改写历史行"（本轮不做，抹平它就等于假装 T-15 没存在过）与
    "新写入不许再用它"（这就是 T-15 的兑现），两个方向都需要库里同时有这么一行。
    """
    request_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id,
            project_key=PROJECT,
            raw_input="存档节律的用例底座",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    stale_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=stale_id,
            round_no=1,
            phase=cadence.OPEN_PHASE,
            task_card_ids=[CARD, CARD2],
            ref=f"{cadence.TEMPORARY_REF_PREFIX}registry/snapshot-{stale_id}",
            rolled_back_at=None,
        )
    )
    # 这道 flush 不是顺手加的：`task_card.snapshot_id` 有真外键，而**没有 `relationship()` 的两个
    # mapper，SQLAlchemy 的工作单元不按表间外键排 INSERT 顺序**（`graph.py:176-183` 记过同一条实测）。
    # 不先让存档点行落库，下面两张卡会以一条 insertmany 批量插到它前面 ⇒ 本文件每一条 PG 用例
    # 都红在外键违反上，而那看起来更像"存档机制有问题"。
    session.flush()
    for card_id in (CARD, CARD2):
        session.add(
            TaskCard(
                id=card_id,
                feature_request_id=request_id,
                title=f"卡 {card_id}",
                difficulty=2,
                skill_tags=["frontend_coding"],
                depends_on=[],
                executor_agent_id=None,
                executor_role_id=None,
                reviewer_agent_id=None,
                acceptor_agent_id=None,
                artifact_floor_min_bytes=512,
                artifact_floor_min_lines=12,
                artifact_floor_format="html",
                acceptance_criteria=["首屏有标题"],
                handoff_count=0,
                review_round=0,
                status="DRAFT",
                snapshot_id=stale_id,
            )
        )
    session.flush()
    return request_id


def recorder(argv_log: list[tuple[str, ...]], *, sha: str = SHA_A, fail_on: str | None = None):
    """桩执行器：记下每一次 argv，返回一个形状合法的读数。

    `fail_on` 造"git 退非零"那一发，且它的 stdout 里塞一条假凭据形状与一个绝对路径 ——
    判据要证明失败消息里两样都没有（用形态而不是字面量当判据，凭据面口径同 T105）。
    """

    def runner(argv: tuple[str, ...], cwd: Path) -> cadence.GitResult:
        argv_log.append(argv)
        if fail_on and argv[0] == fail_on:
            return cadence.GitResult(code=128, stdout=f"fatal: bad object in {cwd} token {FAKE_KEY}")
        return cadence.GitResult(code=0, stdout=f"{sha}\n")

    return runner


def git(repo: Path, *args: str) -> str:
    """在**测试自己建的临时仓库**里跑 git（`check=True`，失败就让用例红在命令本身上）。

    临时仓库的身份写在它自己的 `.git/config` 里 —— 那是搭现场，不是给产品代码补参数，
    也不碰用户任何一份 git 配置（`cadence` 模块头那条"身份从哪来"就是为了让这一步看得见）。
    """
    done = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "archive-target"
    root.mkdir()
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "probe@invalid")
    git(root, "config", "user.name", "probe")
    (root / "status.md").write_text("| round | card |\n|---|---|\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return root


# ---------------------------------------------------------------- 一、两拍各产一个真 commit 对象

def test_both_beats_each_produce_a_real_commit_object(repo: Path) -> None:
    """开工前 + 完工后各一拍 ⇒ 两个**真存在**的 git 对象，且 `ref` 指得回去。

    "真存在"是这条用例的全部意义：只断 ref 的形状证明的是字符串拼对了，`cat-file -e` 才证明那个对象
    在库里 ⇒ 宪法那句 `revert 可复原` 才有东西可复原。
    """
    opened = cadence.commit_archive_point(
        repo, phase=cadence.OPEN_PHASE, round_no=1, project_key=PROJECT, task_card_ids=[CARD, CARD2]
    )
    (repo / "qoder-workspace").mkdir()
    (repo / "qoder-workspace" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    closed = cadence.commit_archive_point(
        repo, phase=cadence.CLOSE_PHASE, round_no=1, project_key=PROJECT, task_card_ids=[CARD, CARD2]
    )

    assert opened.ref == f"git:{opened.sha}" and closed.ref == f"git:{closed.sha}"
    assert opened.sha != closed.sha
    for point in (opened, closed):
        assert git(repo, "cat-file", "-e", point.sha) == ""
    log = git(repo, "log", "--format=%s")
    assert f"archive: {cadence.OPEN_PHASE} round=1" in log
    assert f"archive: {cadence.CLOSE_PHASE} round=1" in log


def test_archiving_leaves_the_current_working_directory_untouched(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存档 commit 只落在**被交出的那个根**里；进程当时站着的那个目录一头不动。

    这一条防的是最自然的错法：把默认根取成 cwd ⇒ 跑起来就把平台源码仓（或装配方当时所在目录）
    当存档现场连打带 commit。那种实现下本文件其它每一发照样绿。

    对照物是**第二个临时仓库**并 `chdir` 进去当替身，不是真仓：WSL 以 root 跑 git 对 `/mnt/d`
    报 `dubious ownership` 退 128（本轮实测），拿真仓当对照会红在环境上而不是红在性质上；
    替身测的是同一条性质，换机器也测得出。断的是 `HEAD` 与提交数前后相同 ——
    不拿"工作树干净"当判据（owner 与并行 agent 随时在改这份树，那种断言会在无关时刻假红）。
    """
    stand_in = tmp_path / "platform-repo"
    stand_in.mkdir()
    git(stand_in, "init", "-q", ".")
    git(stand_in, "config", "user.email", "probe@invalid")
    git(stand_in, "config", "user.name", "probe")
    (stand_in / "README.md").write_text("platform\n", encoding="utf-8")
    git(stand_in, "add", "-A")
    git(stand_in, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(stand_in)
    head_before = git(stand_in, "rev-parse", "HEAD")
    count_before = git(stand_in, "rev-list", "--count", "HEAD")

    cadence.commit_archive_point(
        repo, phase=cadence.OPEN_PHASE, round_no=1, project_key=PROJECT, task_card_ids=[CARD]
    )

    assert git(stand_in, "rev-parse", "HEAD") == head_before
    assert git(stand_in, "rev-list", "--count", "HEAD") == count_before
    assert int(git(repo, "rev-list", "--count", "HEAD").strip()) == 2, "该动的那一个才动"


# ---------------------------------------------------------------- 二、空轮也留点（--allow-empty）

def test_a_round_with_no_file_change_still_leaves_two_points(repo: Path) -> None:
    """工作树一个文件都没改的那一轮，两拍仍然各留一个点 ⇒ 这是 `--allow-empty` 的全部理由。

    少了那颗旗标，git 在空工作树上退 1 ⇒ "每轮开工前 / 完工后各一次"静默退化成
    "有改动的那些轮各一次"，而**整轮停在人工卡点等人**恰恰是最常出现"没改动"的那种轮。
    """
    first = cadence.commit_archive_point(
        repo, phase=cadence.OPEN_PHASE, round_no=2, project_key=PROJECT, task_card_ids=[CARD]
    )
    second = cadence.commit_archive_point(
        repo, phase=cadence.CLOSE_PHASE, round_no=2, project_key=PROJECT, task_card_ids=[CARD]
    )
    count = int(git(repo, "rev-list", "--count", "HEAD").strip())

    assert first.sha != second.sha
    assert count == 3, "seed + 两拍 = 3；少的那一拍就是'没改动就不算轮'那种退化"


# ---------------------------------------------------------------- 三、名单外的子命令：执行之前拒

@pytest.mark.parametrize(
    "subcommand",
    ["push", "reset", "clean", "checkout", "rebase", "rm", "filter-branch", "rev-list", "cat-file"],
)
def test_a_subcommand_outside_the_allow_list_is_refused(subcommand: str) -> None:
    """存档机制不是第二条 git 通道：名单外的子命令**在拼成可执行 argv 之前**就拒。

    `rev-list` / `cat-file` 这两个"只读但照样拒"的值是刻意的反向对照 —— 名单的意义不是
    "破坏性的不进"，而是"这一层能做的动作是一个封闭集合"。读存档点走 SQL（第五节），不走 git。
    """
    with pytest.raises(cadence.ArchiveMisuse) as caught:
        cadence.assert_allowed((subcommand, "--hard"))

    assert caught.value.code == "git_subcommand_forbidden"
    assert subcommand in str(caught.value)


def test_a_missing_archive_root_runs_no_git_at_all() -> None:
    """根不存在 ⇒ 一条 git 都不跑。`calls == []` 才是"失败关闭"的证据，抛错本身不是。

    只断"抛了 `ArchiveMisuse`"会放过一种真实的错法：先 `add -A` 再发现根不对然后抛 ——
    那时现场已被动过，而消息看起来一样干净。
    """
    calls: list[tuple[str, ...]] = []

    with pytest.raises(cadence.ArchiveMisuse) as caught:
        cadence.commit_archive_point(
            Path("/definitely/not/here/anywhere"),
            phase=cadence.OPEN_PHASE,
            round_no=1,
            project_key=PROJECT,
            task_card_ids=[CARD],
            runner=recorder(calls),
        )

    assert caught.value.code == "root_missing"
    assert calls == [], "runner 被调到过 ⇒ 不是执行之前拒，是动完现场再报错"


@pytest.mark.parametrize(
    "phase, round_no, cards, project_key, code",
    [
        ("PRE_WORK", 0, [CARD], PROJECT, "round_no"),
        ("PREWORK", 1, [CARD], PROJECT, "phase_domain"),
        ("PRE_WORK", 1, [], PROJECT, "cards_required"),
        ("PRE_WORK", 1, [CARD, "  "], PROJECT, "cards_shape"),
        ("PRE_WORK", 1, [CARD, CARD], PROJECT, "cards_shape"),
        ("PRE_WORK", 1, [CARD], "", "project_key"),
    ],
)
def test_the_shape_rules_reject_before_running_git(
    phase: str, round_no: int, cards: list[str], project_key: str, code: str
) -> None:
    """六种坏形状各一格，全部在执行之前拒。`project_key` 那一格钉的是"存档点必须点名项目"：
    没有它，"存档点可枚举"在第二个项目出现的那天就断了（会读出别的项目的点）。
    """
    calls: list[tuple[str, ...]] = []

    with pytest.raises(cadence.ArchiveMisuse) as caught:
        cadence.commit_archive_point(
            Path.cwd(),
            phase=phase,
            round_no=round_no,
            project_key=project_key,
            task_card_ids=cards,
            runner=recorder(calls),
        )

    assert caught.value.code == code
    assert calls == []


# ---------------------------------------------------------------- 四、真实 argv 与失败消息的口径

def test_the_three_allowed_commands_are_exactly_the_ones_used() -> None:
    """真实调用序列 = `add` / `commit` / `rev-parse` 各一次，且 `--allow-empty` 真在 argv 里。

    这一发把上一节的反向面补齐（名单内的三项**确实被用到**、且没有第四项）：
    `--allow-empty` 只写在常量里而没进 argv 的话，第二节的用例照样绿。
    """
    calls: list[tuple[str, ...]] = []

    cadence.commit_archive_point(
        Path.cwd(),
        phase=cadence.OPEN_PHASE,
        round_no=1,
        project_key=PROJECT,
        task_card_ids=[CARD],
        runner=recorder(calls),
    )

    assert [argv[0] for argv in calls] == list(cadence.ALLOWED_SUBCOMMANDS)
    assert calls[1][:2] == cadence.COMMIT_FLAGS
    assert calls[2] == cadence.READ_BACK_ARGV


def test_a_failed_git_action_names_the_exit_code_and_nothing_else() -> None:
    """git 退非零 ⇒ 消息只有子命令名与退出码。桩的 stdout 里那条假凭据与那个路径都不许出现。

    另一件要钉的事：失败之后**不继续往下跑** —— `add` / `commit` 两步就停，
    跑到第三步再报就是"现场已被改过两次"。
    """
    calls: list[tuple[str, ...]] = []

    with pytest.raises(cadence.ArchiveFailure) as caught:
        cadence.commit_archive_point(
            Path.cwd(),
            phase=cadence.OPEN_PHASE,
            round_no=1,
            project_key=PROJECT,
            task_card_ids=[CARD],
            runner=recorder(calls, fail_on="commit"),
        )
    message = str(caught.value)

    assert caught.value.code == 128 and caught.value.subcommand == "commit"
    assert FAKE_KEY not in message and "fatal" not in message
    assert [argv[0] for argv in calls] == ["add", "commit"], "失败后还往下跑 = 半程态"


@pytest.mark.parametrize("sha", ["", "not-a-sha", "A" * 40, "z" * 40, "a" * 39, "a" * 41])
def test_a_non_hex_sha_never_becomes_a_handle(sha: str) -> None:
    """回读到的东西不是小写十六进制的 40 或 64 位 ⇒ 不拼成句柄（大写也被拒：对象名按小写比）。"""
    with pytest.raises(cadence.ArchiveMisuse) as caught:
        cadence.snapshot_ref(sha)

    assert caught.value.code == "sha_shape"


# ---------------------------------------------------------------- 五、存档点可枚举（库里那一半）

def test_two_beats_are_enumerable_in_domain_order(session: Session, scope: uuid.UUID) -> None:
    """枚举顺序 = `(轮次 ↑, 拍名按域声明顺序, id ↑)`，不是字符串序，也不是"插入序"。

    为什么单独钉这一条：PG 枚举按 OID 排，而 `POST_WORK < PRE_WORK` 按字母排 ⇒
    "开工前在完工前"这句话如果交给 SQL 排序就会掉。声明顺序（`snapshot_phase` 域里的下标）是唯一权威。
    底座里那行 round 1 的临时句柄行也跟着排 —— 它不是噪声，它正是"库里历史与新增并读"的形状。
    """
    snap.record_point(
        session, project_key=PROJECT, round_no=2, phase=cadence.OPEN_PHASE,
        task_card_ids=[CARD, CARD2], ref=REF_A, now=NOW,
    )
    snap.record_point(
        session, project_key=PROJECT, round_no=2, phase=cadence.CLOSE_PHASE,
        task_card_ids=[CARD, CARD2], ref=f"git:{SHA_B}", now=NOW,
    )
    points = snap.list_points(session, project_key=PROJECT)

    assert [str(row.phase) for row in points] == [
        cadence.OPEN_PHASE,
        cadence.OPEN_PHASE,
        cadence.CLOSE_PHASE,
    ], [str(row.phase) for row in points]
    assert [int(row.round_no) for row in points] == [1, 2, 2]
    assert str(points[-1].ref) == f"git:{SHA_B}"


def test_the_trace_row_names_the_same_archive_point(session: Session, scope: uuid.UUID) -> None:
    """FR-052 那句"落盘留痕含存档点"：EN-14 有一行 `kind=SNAPSHOT`，说的是同一行 EN-12。

    `SNAPSHOT` 这个域值此前在全仓是**零写入**的（只有 `enums.py` 列着它）⇒ 这一发是它的第一处生产写者。
    键集合按 `SNAPSHOT_PAYLOAD_KEYS` 钉死：多一个少一个都红，免得"记了什么"这件事只有代码知道。
    """
    row = snap.record_point(
        session, project_key=PROJECT, round_no=1, phase=cadence.OPEN_PHASE,
        task_card_ids=[CARD], ref=REF_A, now=NOW,
    )
    events = list(
        session.scalars(
            select(EventLog).where(
                EventLog.entity == "snapshot", EventLog.entity_id == str(row.id)
            )
        )
    )

    assert len(events) == 1
    assert str(events[0].kind) == snap.SNAPSHOT_KIND
    assert str(events[0].actor_agent_id) == snap.PLATFORM_PRINCIPAL
    assert set(events[0].payload) == set(snap.SNAPSHOT_PAYLOAD_KEYS)
    assert events[0].payload["ref"] == REF_A
    assert events[0].payload["sha"] == SHA_A


def test_round_numbers_count_rounds_and_stay_inside_the_project(session: Session, scope: uuid.UUID) -> None:
    """`next_round_no` 数的是**轮**不是**拍**；第二个项目的点不进本项目的答案。

    按行数就每轮加二 ⇒ `round_no` 变成跳号流水，`revert` 挑"第 2 轮开工前"挑不到东西。
    后半个判据是 `snapshot` 没有项目列的唯一兑现途径（卡集合反查），所以反查本身就是判据对象。
    """
    for index, phase in enumerate((cadence.OPEN_PHASE, cadence.CLOSE_PHASE)):
        snap.record_point(
            session, project_key=PROJECT, round_no=2, phase=phase,
            task_card_ids=[CARD, CARD2], ref=f"git:{'c' * 40}" if index == 0 else f"git:{'d' * 40}", now=NOW,
        )
    other = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=other,
            project_key=OTHER_PROJECT,
            raw_input="另一个项目",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    session.flush()

    assert snap.next_round_no(session, project_key=PROJECT) == 3, "库里已有 round 1（底座）与 2（本轮两拍）"
    assert snap.next_round_no(session, project_key=OTHER_PROJECT) == 1
    assert snap.list_points(session, project_key=OTHER_PROJECT) == []


# ---------------------------------------------------------------- 六、真实句柄（T-15 的兑现）

def test_the_temporary_handle_is_refused_for_new_rows(session: Session, scope: uuid.UUID) -> None:
    """T-15 那句"T108 落地时必须换成真实句柄"落在**新写入**上：`local:` 形态一行都不落。

    两个方向都要钉，因为这里两边都能假绿：
    ① 拒绝之后库里没有新行（写了半行 = 半程态）；
    ② 底座那行历史临时句柄**没被改写**（改写它就是抹平"曾经只登记不实现"那个时点事实）。
    """
    before = int(session.scalar(select(func.count()).select_from(Snapshot)) or 0)

    with pytest.raises(snap.SnapshotMisuse) as caught:
        snap.record_point(
            session, project_key=PROJECT, round_no=1, phase=cadence.OPEN_PHASE,
            task_card_ids=[CARD], ref=f"{cadence.TEMPORARY_REF_PREFIX}registry/snapshot-x", now=NOW,
        )
    session.flush()

    assert caught.value.code == "ref_not_a_real_handle"
    assert "T-15" in str(caught.value), (
        "消息没点名『这是 T-15 的临时形态』⇒ 与『根本不是句柄』读起来一模一样："
        "调用方会去查自己的 sha 从哪来，而真正缺的是一次真 commit（两条拒绝共用一个 code 时，"
        "消息是唯一还能把两种成因分开的地方）"
    )
    assert int(session.scalar(select(func.count()).select_from(Snapshot)) or 0) == before
    stale = list(session.scalars(select(Snapshot).where(Snapshot.ref.like("local:%"))))
    assert len(stale) == 1, "历史那行被改写 ⇒ 抹平了 T-15 的时点读数"


@pytest.mark.parametrize(
    "ref",
    ["", "git:", f"git:{'a' * 39}", f"git:{'A' * 40}", "s3://bucket/key", "file:///tmp/x"],
)
def test_a_handle_that_points_at_nothing_is_refused(ref: str) -> None:
    """其它"看着像句柄"的形态一律拒，且**一条 SQL 都不发**。

    `s3://` 与 `file://` 两格是给 T109 留的：换存档机制是那一轮的活，不是靠调用方随手传一个串进来。
    这一发的 session 传 `None` —— 校验不过就连会话都不该碰，那才是"写之前拒"的最强形状。
    """
    with pytest.raises(snap.SnapshotMisuse) as caught:
        snap.record_point(
            None, project_key=PROJECT, round_no=1, phase=cadence.OPEN_PHASE,
            task_card_ids=[CARD], ref=ref, now=NOW,
        )

    assert caught.value.code == "ref_not_a_real_handle"


def test_the_handle_is_assembled_in_exactly_one_place() -> None:
    """`GIT_REF_PREFIX` 的拼装点全仓**恰一处**（`cadence.snapshot_ref` 那一个 f-string）。

    句柄形态是 T109 要定稿的东西；散成两处，下一轮改一处就会让"`ref` 指什么"有两个答案。
    `is_real_handle` 里那次切片不算拼装（它不产生新句柄），所以判据按 f-string 的插值位置数。
    """
    sources = {
        path.relative_to(SRC_ROOT): path.read_text(encoding="utf-8")
        for path in list((SRC_ROOT / "archive").rglob("*.py"))
        + [SRC_ROOT / "persistence" / "snapshots.py"]
    }
    builds: list[str] = []
    for rel, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.JoinedStr) and any(
                isinstance(part, ast.FormattedValue)
                and getattr(part.value, "id", "") == "GIT_REF_PREFIX"
                for part in node.values
            ):
                builds.append(str(rel))

    assert len(builds) == 1, f"句柄拼装点 {builds} ⇒ 形态成了两个载体的候选"


# ---------------------------------------------------------------- 七、存档不替代三岗把关（D-12）

def test_archiving_moves_no_card_status_and_opens_no_ticket(
    session: Session, scope: uuid.UUID
) -> None:
    """打一拍存档**不改卡状态、不建审批单**：宪法「三」那句 MUST NOT 替代三岗把关的库里形状。

    断的是**前后完全相同**而不是"没报错" —— 这条最容易"顺手写对"：实现若为了省事让存档把 `DRAFT`
    推进一步，全链路其它用例照绿，而把关从流程里消失了。
    """
    statuses_before = list(session.scalars(select(TaskCard.status).order_by(TaskCard.id)))
    tickets_before = int(session.scalar(select(func.count()).select_from(ApprovalTicket)) or 0)

    row = snap.record_point(
        session, project_key=PROJECT, round_no=2, phase=cadence.CLOSE_PHASE,
        task_card_ids=[CARD, CARD2], ref=REF_A, now=NOW,
    )

    assert list(session.scalars(select(TaskCard.status).order_by(TaskCard.id))) == statuses_before
    assert int(session.scalar(select(func.count()).select_from(ApprovalTicket)) or 0) == tickets_before
    assert int(row.round_no) == 2 and str(row.rolled_back_at) != ""  # 列存在但未写回退时刻
    assert row.rolled_back_at is None, "回退留痕归 T110；本轮写它 = 造一个到不了的态"


def test_the_archive_layer_cannot_reach_the_guard_layer() -> None:
    """另一半（"反之亦然"）：存档这一层**根本不 import** 把关 / 审批 / 编排层。

    方向是刻意的 —— 不写"存档代码里没有改状态的语句"那种正面断言（一句 `session.execute(update(...))`
    就绕过去了），而是把依赖边钉死：拿不到那些模块，就写不出替代把关的动作。
    名单里带上 `api` 是因为端点层也能改状态，而存档层没有理由知道端点存在。

    **扫描面按包 glob，不按文件名点名**（T110 起的改写）：原来这里是三份文件名的清单，
    于是 `archive/restore.py` 落地那一轮它根本不在射程内 —— 而恢复恰恰是最有条件去重跑链路的一层。
    口径同 T106 那条"扫描根要跟被 import 的那棵树走"。判据本身在
    `test_recovery_idempotent.py::test_the_archive_layer_scan_covers_every_module_under_archive`
    还有一条"这个 glob 还在"的看守。
    """
    banned_found: list[str] = []
    paths = sorted((SRC_ROOT / "archive").glob("*.py")) + [SRC_ROOT / "persistence" / "snapshots.py"]
    for path in paths:
        roots = {
            item.split(".")[1] if item.startswith("acp.") else item
            for item in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        }
        if banned := {"guard", "approval", "orchestrate", "api"} & roots:
            banned_found.append(f"{path.name}:{sorted(banned)}")

    assert paths, "存档包里一个模块都没有 ⇒ 这条判据已经空转"
    assert not banned_found, f"存档层依赖了把关/审批/编排/API 层 ⇒ 存档成为第二处强制点：{banned_found}"


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ---------------------------------------------------------------- 八、EN-12 的写者收成一处

def test_en12_has_exactly_one_construction_site_after_t109_closed_the_second() -> None:
    """`Snapshot(` 在源码里**恰一处**：`persistence/snapshots.py`。

    **这条的期望值被 T109 改过，改前不是零处而是两处**（T108 那一轮的原文是
    "恰两处：`orchestrate/graph.py`（T-15 的历史登记）与本仓储"）。第二处在 T109 关掉：
    owner 2026-10-16 裁 ㉝ 归 T109 ⇒ `_normalize` 不再自己 `Snapshot(ref="local:…")`，
    而是经 `archive.cadence` 打一拍 + `snapshots.record_point()` 落库。
    ⇒ 现在第三处（或任何第二处）= 有人绕开"什么算一个存档点"这个唯一裁决。
    按 `(文件, 处数)` 钉而不是只数总量：同一文件里多写一行也要红。
    """
    sites: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Snapshot":
                sites.append(str(path.relative_to(SRC_ROOT).as_posix()))

    assert len(sites) == 1, f"EN-12 的构造点数不是 1：{sorted(sites)}"
    assert sites == ["persistence/snapshots.py"], f"EN-12 的构造点不在唯一写者上：{sorted(sites)}"
