"""P1-17：整链 E2E —— `intake → approve → run_chain → verify`，**真库、真盘、真字节**。

这一档存在的理由只有一句：**单元档与契约档都可以在"每一段各自正确"的前提下全绿，
而整条链仍然是假的**。具体地说，交付包的 `manifest[].bytes` 是写入那一刻量出来的；
只要组包与落盘之间有任何一次覆盖 / 截断 / 编码漂移，那份清单就会与磁盘不符 ——
而 `run_deliver` 自己在组包那一刻验过一次（`assemble._assert_manifest_matches_disk`），
**组包之后发生的事它管不着**。所以本文件在链跑完之后再 stat 一遍，量的是最终态。

三条刻意的选择：
① **真库而不是会话替身**：checkpointer 走另一条连接、不参与外层事务 ⇒ 替身会让
   "图上有等待点"这个读数变成上一个用例留下的现场（守卫的现场可复用 = 守卫不存在）。
② **真盘而不是内存文件系统**：要验的正是 `stat().st_size`，换成内存 FS 就只剩自证。
③ **不复用 `assemble` 自己的校验函数**：那是被验的那一半，用验对象的函数去验它自己
   得到的永远是真；而且它只覆盖"组包那一刻"，覆盖不到本文件要验的"交付之后的最终态"。

第 3 条还带来一份责任：**证明这条断言有牙**（`test_the_byte_check_has_teeth`）。
一条从不失败的断言不是门禁，是一句装饰。

跑法：`pytest tests/e2e`（e2e 档**不在** fast 档里，需要 `ACP_DB_DSN` 指向一个能建库的 PG）。
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import select

from acp import run as run_module
from acp.approval import repository as approval_repo
from acp.archive import root as archive
from acp.persistence import session as session_module
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import Deliverable

PROJECT_KEY = "e2e_probe"
RAW_INPUT = "给商品页加一个能改数量的输入框，样式沿用 tokens.json"
PRINCIPAL = "e2e-owner"

#: 与 `run.DEFAULT_ENTRY_NAME` 同一个字面量：入口页由调用方点名，e2e 也自己点一次。
ENTRY_NAME = "index.html"


@pytest.fixture
def e2e_env() -> str:
    """本档要真 PG：没有 `ACP_DB_DSN` 就 skip，绝不假装跑过。

    **不要拿工作库跑**：本档会真提交需求单、卡与交付包；调用方应指向一个空的
    scratch 库（库已迁移到 head）。skip 而不是红，是为了让 `pytest tests/` 在没配库的
    机器上仍能给出"哪些档没跑"的读数，而不是整片 error。
    """
    dsn = os.environ.get("ACP_DB_DSN", "").strip()
    if not dsn:
        pytest.skip("缺 ACP_DB_DSN ⇒ 整链 E2E 不跑（不是绿）")
    return dsn


# ------------------------------------------------------------------ 共用现场：真临时库

#: **这里刻意没有 `tests/e2e/conftest.py`**：`tests/guards`、`tests/integration` 里有三个文件
#: 写的是 `from conftest import CHILD_STDIO_ENV`（按 rootdir 解析的裸模块名），一旦本目录多出
#: 一个 conftest，pytest 的 sys.path 里就会出现两个同名 `conftest`，那三处 import 会解析到
#: 本目录这份上 ⇒ 全量收集当场炸。所以 E2E 的夹具放在测试模块里，不给目录加第二份 conftest。
REPO_ROOT = Path(__file__).resolve().parents[2]


def _swap_database(dsn: str, database: str) -> str:
    """把 DSN 里的库名换成另一个（`/acp` → `/postgres`）。**不解析口令**：只动最后那一段。"""
    head, _, _tail = dsn.rpartition("/")
    return f"{head}/{database}"


@pytest.fixture
def e2e_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """建一个真库 → `alembic upgrade head` → 指向它 → 用完 DROP，并把环境还原。

    为什么必须自备一个库而不是复用 `ACP_DB_DSN` 那个：整链会**真提交**，开发库里留一批
    `e2e` 行，下一次全量档就会停在"进入时十张表必须为空"那条守卫上（离根因 53 个用例远，
    2026-10-16 实测过一次）。而**不能靠会话回滚**：checkpointer 走另一条连接、不参与外层
    事务 ⇒ 回滚撤不掉它，"图上有等待点"就成了上一个用例留下的现场。

    作用域是**函数级**并且用完还原 `ACP_DB_DSN`：本档按目录序排在 `guards` / `integration`
    之前，把环境留在自己的临时库上会让后面那两档连到一个刚被写脏的库里去。
    """
    base = os.environ.get("ACP_DB_DSN", "").strip()
    if not base:
        pytest.skip("E2E 档需要 `ACP_DB_DSN`（没有可连的 PG 就整档 skip，不假绿）")

    moment = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    database = f"acp_e2e_{moment}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_swap_database(base, "postgres"), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database}"')
    target = _swap_database(base, database)
    try:
        # 迁移走子进程：`python -m alembic` 就是生产用的那一条命令，E2E 该验的是它。
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "ACP_DB_DSN": target, "PYTHONIOENCODING": "utf-8"},
            timeout=300,
        )
        if done.returncode != 0:
            pytest.fail(f"临时库没迁到 head：{done.stdout}\n{done.stderr}")
        # `monkeypatch` 负责用例结束后还原 ⇒ 不会污染同会话里其它档的用例。
        monkeypatch.setenv("ACP_DB_DSN", target)
        yield target
    finally:
        # `WITH (FORCE)`：checkpointer 那条连接可能还挂着，不带 FORCE 删不掉。
        # 删是清理不是判据 ⇒ 失败只报一行，不把用例判红。
        try:
            with psycopg.connect(_swap_database(base, "postgres"), autocommit=True) as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        except Exception as exc:  # pragma: no cover - 清理路径
            print(f"[e2e] 临时库 {database} 没删掉（可手动 DROP）：{exc}")


@pytest.fixture
def e2e_env(e2e_dsn: str) -> str:
    """把已经切好的那个 DSN 递给用例（切换与还原都由 `e2e_dsn` 负责）。"""
    return e2e_dsn



def _manifest_drift(manifest, project_root: Path) -> tuple[list[str], list[str]]:
    """对**磁盘**重新 stat 一遍，返回 `(缺失, 字节不符)`。

    单独成函数并让下面两条用例共用：正例与变异对照必须走**同一份**比较逻辑 ——
    各写一遍的话，改了其中一处而另一处没改，正例会继续绿着，而它绿的原因不再是被验的那一件事。
    """
    missing: list[str] = []
    drifted: list[str] = []
    for item in manifest:
        rel_path = str(item["rel_path"])
        target = project_root / rel_path
        if not target.is_file():
            missing.append(rel_path)
            continue
        actual = target.stat().st_size
        if actual != int(item["bytes"]):
            drifted.append(f"{rel_path}（清单 {item['bytes']}B / 盘上 {actual}B）")
    return missing, drifted


def _run_whole_chain(session_factory, bus_root: Path, now: dt.datetime) -> run_module.ChainReport:
    """整链跑一遍（受理 → 批单 → 恢复 → 指派/执行/门禁/审查/验收 → 组包 → 投影）。

    三条用例共用一个现场构造器：**同一份现场 ⇒ 同一份结论**。各写一遍的话，
    某一条因为现场搭得不同而过，那它就不再是在验同一件事。
    """
    with session_module.new_session(session_factory) as session:
        intake = run_module.run_intake(
            RAW_INPUT, project_key=PROJECT_KEY, transport="stub",
            bus_root=bus_root, session=session, now=now,
        )
        request_id = intake.feature_request_id
        # 受理段走完的样子：挂起在 A 类卡点，有一张单据等人。
        assert intake.card_ids and intake.ticket_id is not None
        # 批单走**唯一那个**写者（`repository.resolve`），不自己 UPDATE 状态。
        outcome = approval_repo.resolve(
            session, ticket_id=intake.ticket_id, verdict="approve", version=0,
            channel=approval_repo.CLI, principal=PRINCIPAL,
            approvers=frozenset({PRINCIPAL}), reason_or_ask="e2e 整链用例自批", now=now,
        )
        assert outcome.applied, f"批准没生效：{outcome.code} ⇒ 恢复段拿不到决定态"

    with session_module.new_session(session_factory) as session:
        resumed = run_module.run_resume(
            request_id, transport="stub", bus_root=bus_root, session=session
        )
        assert resumed.conclusion == "approve"
        assert not resumed.waiting_after, (
            f"恢复之后图上还停着 {resumed.waiting_after} ⇒ 卡没被放行，整链会空转"
        )

    with session_module.new_session(session_factory) as session:
        return run_module.run_chain(
            request_id, transport="stub", bus_root=bus_root, session=session, now=now,
            findings_for=run_module.StubFindingProvider(),
            accept_for=run_module.StubAcceptanceProvider(),
            entry_name=ENTRY_NAME,
        )


def test_the_whole_chain_delivers_a_package_whose_manifest_matches_the_disk(
    e2e_env: str, bus
) -> None:
    """一条需求从受理走到交付包；交付包自带的清单与磁盘**逐条**一致。

    断言的顺序就是责任的顺序：先证链路真走到了交付（否则"清单正确"只是空谈），
    再证清单每一条都指向盘上一个真实存在、且字节数相同的文件。
    """
    bus_root = bus.path
    bus.with_project(PROJECT_KEY)
    now = dt.datetime(2026, 10, 30, 1, 0, tzinfo=dt.timezone.utc)

    factory = session_module.make_session_factory()
    chain = _run_whole_chain(factory, bus_root, now)
    assert chain.dispatched, "整链一张卡都没派出去 ⇒ 下面验的只是个空包"
    assert chain.delivery.manifest_count > 0, "交付包清单是空的 ⇒ 组包这一格没真落"

    with session_module.new_session(factory) as session:
        row = session.scalar(
            select(Deliverable).where(Deliverable.feature_request_id == chain.feature_request_id)
        )
        assert row is not None, "EN-17 里没有交付包 ⇒ 链路没走到组包"
        request = session.get(FeatureRequest, chain.feature_request_id)

        # 清单里的 `rel_path` 与 `package_path` 都相对**存档根**（`<bus_root>/<project_key>`，
        # `archive/root.py` 是全仓唯一拼这一层的地方），不是相对外层总线根。
        project_root = archive.archive_root(PROJECT_KEY, bus_root=bus_root)
        entry_on_disk = project_root / row.entry_path
        assert entry_on_disk.is_file(), (
            f"入口 {row.entry_path!r} 不在盘上 ⇒ 交付包指向一个打不开的东西（R-07 第①条）"
        )

        missing, drifted = _manifest_drift(row.manifest, project_root)
        assert not missing, f"清单里有盘上不存在的文件：{missing}"
        assert not drifted, f"交付清单与磁盘字节不符（write_bytes 那一档退化了）：{drifted}"
        # 反向对照：清单非空且每一条都被真的量过 —— 空的比较结果会让上面两句恒绿。
        assert len(row.manifest) >= 1

        # 交付包自己那份 JSON：包是给人下载的，"写了多大"只有量过才算数。
        package = project_root / chain.delivery.package_path
        assert package.is_file()
        assert package.stat().st_size == chain.delivery.package_bytes, (
            "交付包文件的实际大小与读数不一致 ⇒ `package_bytes` 又变成了一个自述值"
        )
        assert str(request.status) == "DELIVERED", (
            f"需求单状态是 {request.status} ⇒ 交出去了却没把单子收口（P1-6 那一格）"
        )


def test_the_byte_check_has_teeth(e2e_env: str, bus) -> None:
    """变异对照：把盘上某个文件**改多一个字节**，同一份比较逻辑必须立刻报出不符。

    没有这条，上一条只是"这次恰好没漂移" —— 而"恰好没漂移"和"验出来了"是两件事。
    改动落在**磁盘**这一侧（追加一个字节）而不是改库里的清单：真实世界里漂移发生的
    方向正是这一侧（后一次运行覆盖、手工改动、截断）。
    """
    bus_root = bus.path
    bus.with_project(PROJECT_KEY)
    now = dt.datetime(2026, 10, 30, 1, 2, tzinfo=dt.timezone.utc)
    factory = session_module.make_session_factory()
    chain = _run_whole_chain(factory, bus_root, now)
    project_root = archive.archive_root(PROJECT_KEY, bus_root=bus_root)

    with session_module.new_session(factory) as session:
        row = session.scalar(
            select(Deliverable).where(Deliverable.feature_request_id == chain.feature_request_id)
        )
        assert row is not None and row.manifest
        victim = project_root / str(row.manifest[0]["rel_path"])
        declared = int(row.manifest[0]["bytes"])
        assert victim.stat().st_size == declared, "变异之前就应该是一致的，否则对照不成立"

    # 追加一个字节 —— 交付包已经组完、清单已经落库，这正是"只有重新 stat 才看得见"的那一类漂移。
    with victim.open("ab") as handle:
        handle.write(b"\n")

    with session_module.new_session(factory) as session:
        row = session.scalar(
            select(Deliverable).where(Deliverable.feature_request_id == chain.feature_request_id)
        )
        assert row is not None
        _missing, drifted = _manifest_drift(row.manifest, project_root)
        assert len(drifted) == 1, f"改了一个字节却读成 {len(drifted)} 处不符：{drifted}"
        assert str(row.manifest[0]["rel_path"]) in drifted[0], "报的不是被改的那一条"
        assert f"{declared}B" in drifted[0] and f"{declared + 1}B" in drifted[0], (
            "消息里必须同时给出声明值与盘上值 —— 只说不一致就还得自己再去量一次"
        )


def test_a_second_assemble_is_refused_so_the_first_package_is_the_only_one(
    e2e_env: str, bus
) -> None:
    """反向对照：`FR-036` 的"唯一一份"在真库上也成立。

    少了这条，上面那条"清单与磁盘一致"只能证明**某一次**组对了，
    证明不了"不会被第二次组包悄悄覆盖成另一份" —— 那正是"第二份清单也对，但内容已经换了"的形态。
    """
    from acp.orchestrate.nodes import assemble as assemble_node

    bus_root = bus.path
    bus.with_project(PROJECT_KEY)
    now = dt.datetime(2026, 10, 30, 1, 4, tzinfo=dt.timezone.utc)
    factory = session_module.make_session_factory()
    chain = _run_whole_chain(factory, bus_root, now)
    project_root = archive.archive_root(PROJECT_KEY, bus_root=bus_root)

    with session_module.new_session(factory) as session:
        first = session.scalar(
            select(Deliverable).where(Deliverable.feature_request_id == chain.feature_request_id)
        )
        assert first is not None
        with pytest.raises(Exception) as caught:
            assemble_node.assemble(
                session,
                feature_request_id=chain.feature_request_id,
                entry_path=first.entry_path,
                assembled_by=PRINCIPAL,
                created_at=now,
                # 与 `run_deliver` 同一口径：`assemble()` 的 `bus_root` 是"清单里 rel_path
                # 相对的那个根"= 存档根，不是外层总线根。
                bus_root=project_root,
            )
        assert "PackageAlreadyAssembled" in type(caught.value).__name__, (
            f"第二包没有被拒（{type(caught.value).__name__}）⇒ 唯一一份那条约束在真库上不成立"
        )
        rows = session.scalars(
            select(Deliverable).where(Deliverable.feature_request_id == chain.feature_request_id)
        ).all()
        assert len(rows) == 1 and rows[0].id == first.id, "第二包把第一包的内容换掉了"
