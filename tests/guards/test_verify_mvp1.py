"""T124 步骤 5：端到端核验段 `acp.verify_mvp1` 的判据。

**这一笔接的是哪一段**：`quickstart.md` §4 那张表里，四条产物判据到今天只有一条被自动化过
（构成与死链的**静态半边**，`tests/guards/test_stub_transport.py` 第 ④ 件），console 与空区块两格一直
写着"属 T124"。本文件把四条钉成一条命令的读数，并钉住"这个读数为什么可以信"。

本文件钉五件事，每件各挡一种会发生的错：

① **四条是合取**：构成、console 错误、死链、空区块 —— 任一一格非零就不许 `passed`。
   挡的错是"前三条过了就算 §4 已过"，而 §4 原话是"三条缺一即判未跑通"（第四条"无空白区块"同规）。
② **期望那两个数不在代码里自证**：`EXPECT_HTML / EXPECT_CSS` 与 §4 表第一行逐字比对，两个方向都会红。
③ **成员从包 JSON 读、不重判资格**：`validated_status` / `validated_manifest` 在本模块出现 0 次
   （筛 valid 那一道只有组包段一个落点，`assemble.py:127`）。
④ **链接按那一页自己的目录解**，外链只登记不解析，且打印面必须看得见"未核验"那一格。
   挡的错有两个方向：按 basename 放宽（四份文件哪天落到两个 workspace 就把断链读成活链），
   以及"0 死链"被读成"全部链接都能点"。
⑤ **通道不通就是拒绝，不是 0**：驱动起不来、少回一页读数、包 JSON 与库分叉、成员在盘上不存在，
   四种都是 `VerifyFailure`，一种报告都不产。挡的错是"没浏览器 ⇒ 没有报错"这种把缺输入当通过。

诚实性（三条，都得看得见）：
· 本文件**不**起真浏览器。console 那一半由注入的假 runner 给读数，证明的是"读数怎么变成判定"，
  不是"这台机器的 Edge 真的报 0 条" —— 后者由 `evidence/phase-12.md §十五` 那次真跑（Windows 侧
  Python + Node + Edge，CDP 打开的是本地 `file://`，零外部调用）单独留读数。
· 假 session 只证明"分支按形状走"，**不**证明 `project_key` 那条 join 取得到行 —— 后者在第六节真 PG。
· 空区块的语义（什么算空）在 `cdp_console.mjs` 里；本文件只钉它的**条数通道**与"少回就拒"。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import re
import uuid
from contextlib import closing
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp import chaos
from acp import run as run_module
from acp import verify_mvp1 as verify_module
from acp.approval import repository as repo
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import checkpointer as cp
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import Deliverable

SRC = pathlib.Path(verify_module.__file__)
QUICKSTART = (pathlib.Path(__file__).resolve().parents[2] / "specs"
              / "001-multi-agent-collab-platform" / "quickstart.md")
NOW = dt.datetime(2026, 10, 17, 11, 15, tzinfo=dt.timezone.utc)
PROJECT = "acp-verify-mvp1"
RAW = "做个介绍公司产品的静态网站"
WORK = "qoder-workspace"
ENTRY = f"{WORK}/index.html"
ABOUT = f"{WORK}/about.html"
CONTACT = f"{WORK}/contact.html"
STYLE = f"{WORK}/styles.css"
FINDINGS = run_module.StubFindingProvider()
ACCEPT = run_module.StubAcceptanceProvider()

#: §4 那个形状的最小复制品：三页共用一份样式，页内相对链接全部同级。
GOOD_BODIES = {
    ENTRY: '<a href="about.html">说明</a><a href="contact.html">联系</a><link rel="stylesheet" href="styles.css">',
    ABOUT: '<a href="index.html">首页</a><a href="contact.html">联系</a>',
    CONTACT: '<a href="index.html">首页</a><a href="mailto:team@example.invalid">邮件</a>',
    STYLE: "body{margin:0}",
}


# ------------------------------------------------------------------ 小工具


def func_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`verify_mvp1.py` 里没有函数 {name!r} ⇒ 这条判据在盯一个已经不存在的形状")


def called_names(func: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                out.append(name)
    return out


def module_tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def attributes(tree: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def imported_modules(tree: ast.AST) -> set[str]:
    """被 import 的**模块全名**：`import a.b` → `a.b`，`from a.b import c` → `a.b`。

    判"有没有网络出口"必须看真实导入面而不是 grep 正文：本模块的模块头就写着
    `assemble.validated_manifest()` 这个名字（散文提到 ≠ 代码调用它），扫正文会把说明自己判成违例
    —— 那种判据第一版就是这么红的，留在这里当下一次的锚。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def select_targets(tree: ast.AST) -> list[str]:
    """每个 `select(X)` 里那个 X 的名字 —— 用来点名"核验段到底查了哪几张表"。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "select" and node.args:
            first = node.args[0]
            out.append(str(getattr(first, "id", None) or getattr(first, "attr", None) or first))
    return out


def emit_keywords(func_name: str, verdict: str) -> set[str]:
    """`_emit("<verdict>", …)` 那一行的字段名集合 —— 打印面有哪些格从这里读，不去 grep 输出。"""
    for node in ast.walk(func_body(func_name)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_emit":
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == verdict:
                return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(f"{func_name} 里没有 _emit({verdict!r}) 那一行")


def row_for(entry: str = ENTRY, request_id: uuid.UUID | None = None) -> Deliverable:
    """一条 EN-17 行，用**模型构造器**造（生产形态）：判据不许靠假对象上的自由字段蒙过去。"""
    return Deliverable(
        id=uuid.uuid4(),
        feature_request_id=request_id or uuid.uuid4(),
        manifest=[],
        entry_path=entry,
        open_issues=[],
        assembled_by_agent_id="orchestrator",
        created_at=NOW,
    )


class FakeSession:
    """只回答"就这几行"的假 session —— 它证明的是分支形状，不证明那条 join（模块头第二条）。"""

    def __init__(self, rows: tuple[Deliverable, ...]) -> None:
        self.rows = list(rows)

    def scalar(self, statement):
        return self.rows[0] if self.rows else None

    def scalars(self, statement):
        return SimpleNamespace(all=lambda: self.rows)


def reading(page: str, *, errors=(), warnings=(), empty=(), links=(), subresources=(), failed=()) -> dict:
    """驱动一行的读数形状（与 `cdp_console.mjs` 的 `pages[*]` 同构）。"""
    return {
        "page": page,
        "errors": list(errors),
        "consoleWarnings": list(warnings),
        "loadingFailed": [{"errorText": text} for text in failed],
        "dom": {"links": list(links), "subresources": list(subresources), "emptyBlocks": list(empty)},
    }


def clean_runner(root: pathlib.Path, extra: dict[str, dict] | None = None):
    """"什么都不报"的驱动替身：每页回一条空读数，键 = 传进去的路径字符串（`verify` 就按这个键取）。"""

    def run(paths):
        out = {str(path): reading(str(path)) for path in paths}
        for key, item in (extra or {}).items():
            assert key in out, f"注入的读数指向了一页不在页集里的 {key!r} ⇒ 这一发没在证它以为的事"
            out[key] = item
        return out

    return run


def staged(tmp_path: pathlib.Path, bodies: dict[str, str], *, manifest: tuple[str, ...] | None = None,
           entry: str = ENTRY, package_entry: str | None = None) -> pathlib.Path:
    """在 `tmp_path/<PROJECT>` 上搭**盘上现场**：workspace 文件 + `results/delivery_package.json`。

    `package_entry` 单独给，是为了让"库里的入口"与"包 JSON 的入口"能故意不一致（分叉那一发）；
    默认两者一致不是省事 —— 不一致必须**显式**造出来才叫变异。
    """
    root = tmp_path / PROJECT
    for rel, body in bodies.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    members = tuple(bodies) if manifest is None else manifest
    (root / "results").mkdir(parents=True, exist_ok=True)
    (root / "results" / "delivery_package.json").write_text(
        json.dumps({"entry_path": package_entry or entry,
                    "manifest": [{"rel_path": rel} for rel in members]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def check(tmp_path: pathlib.Path, bodies: dict[str, str] | None = None, *, manifest=None,
          entry: str = ENTRY, package_entry=None, runner=None, use_real_channel: bool = False,
          node=verify_module.NODE_DEFAULT,
          session=None) -> verify_module.VerifyReport:
    """搭现场 + 一条 EN-17 行 + 跑 `verify()`（本文件不连库那一节都用这一发）。

    默认注假 runner（模块头第一条诚实性：判据不起真浏览器）。`use_real_channel=True` 只有一发用 ——
    那一发要的就是"缺 Node 时操作系统给的那一下"。
    """
    root = staged(tmp_path, bodies if bodies is not None else GOOD_BODIES, manifest=manifest,
                  entry=entry, package_entry=package_entry)
    if runner is not None:
        active = runner
    elif use_real_channel:
        active = None
    else:
        active = clean_runner(root)
    return verify_module.verify(
        project_key=PROJECT,
        bus_root=tmp_path,
        session=session if session is not None else FakeSession((row_for(entry),)),
        runner=active,
        node=node,
    )


def bus_root_of(bus) -> pathlib.Path:
    """把 `bus` 那个临时根下的项目目录 init 成真仓库（受理段的存档点前置，口径同 `conftest.BusRoot`）。"""
    return bus.with_project(PROJECT)


def to_delivered(bus, session) -> uuid.UUID:
    """真跑到**四张卡交付 + 组包 + 公共区投影**（`run_chain` 是唯一的整链驱动），返回需求单号。"""
    bus_root_of(bus)
    intake = run_module.run_intake(RAW, project_key=PROJECT, transport="stub", bus_root=bus.path,
                                   session=session, now=NOW)
    request_id = intake.feature_request_id
    ticket = session.get(ApprovalTicket, intake.ticket_id)
    repo.resolve(session, ticket_id=ticket.id, verdict="approve", version=ticket.version, channel=repo.CLI,
                 principal="owner-test", approvers=frozenset({"owner-test"}), now=NOW)
    run_module.run_resume(request_id, transport="stub", bus_root=bus.path, session=session)
    run_module.run_chain(request_id, transport="stub", bus_root=bus.path, session=session, now=NOW,
                         findings_for=FINDINGS, accept_for=ACCEPT, entry_name="index.html")
    return request_id


def drop_thread(session, feature_request_id: uuid.UUID) -> None:
    for kind in ("A_SPLIT", "B_ACCEPTANCE"):
        thread = cp.thread_id_for(kind=kind, feature_request_id=feature_request_id)
        with closing(psycopg.connect(cp.conninfo(), autocommit=True)) as conn:
            chaos.kill_graph_state(conn, thread)


@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 PG，整节 skip（不是绿）")
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


# ------------------------------------------------------- 一、四条判据是合取


@pytest.mark.parametrize("bucket", ["composition", "console", "dead", "empty", "subresource"])
def test_each_of_the_four_judgments_is_fatal_on_its_own(tmp_path, bucket):
    """五发注入各只改**一个**桶 ⇒ 那一格非零、其余三格仍是 0、`passed` 变 False。

    逐格点名"非零的那一格"是这条判据的全部价值：只断 `passed is False` 的话，
    五发会因为同一个原因（比如构成那条一直不成立）而红，看起来覆盖很宽其实是一格。
    这一发挡的具体形状：`console` 与 `subresource` 都来自驱动，拢成一格就会让"样式没加载出来"
    这种读数在没有 console 错误时溜过去；`composition` 走成员集合、与驱动读数无关，必须自己就能致命。
    """
    index = str(tmp_path / PROJECT / ENTRY)
    extra: dict[str, dict] = {}
    manifest = None
    if bucket == "composition":
        manifest = (ENTRY, ABOUT, STYLE)          # 少一页 ⇒ 2 页 1 样式
    elif bucket == "console":
        extra = {index: reading(index, errors=["boom"])}
    elif bucket == "dead":
        extra = {index: reading(index, links=["gone.html"])}
    elif bucket == "empty":
        extra = {index: reading(index, empty=["section"])}
    elif bucket == "subresource":
        extra = {index: reading(index, failed=["net::ERR_FILE_NOT_FOUND"])}
    report = check(tmp_path, manifest=manifest, runner=clean_runner(tmp_path / PROJECT, extra))
    counts = (int(not report.composition.matches_expectation), report.console_error_count,
              report.dead_link_count, report.empty_block_count)
    #: 四格各只有一格非零 —— 期望值逐桶写死，不是"有一格非零就行"（后者会让五发因为同一个原因红）。
    #: `subresource` 与 `dead` 落进同一格是**本模块的判定**：子资源加载失败就是一条点不开的引用，
    #: 它在 `dead_link_count` 里 +1 而 console 那格仍是 0（真浏览器就是这么分开发两类事件的，实测）。
    expected = {"composition": (1, 0, 0, 0), "console": (0, 1, 0, 0), "dead": (0, 0, 1, 0),
                "empty": (0, 0, 0, 1), "subresource": (0, 0, 1, 0)}[bucket]
    assert counts == expected, (bucket, counts, report)
    assert report.passed is False, bucket


def test_a_clean_channel_with_the_expected_composition_passes(tmp_path, capsys):
    """反向对照（本文件的 R 位）：四格全零 + 3 页 1 样式 ⇒ `passed is True` 且退出码 0。

    上一发如果有一个桶其实动不了判定，这一发就会露出来（同一份现场，只是不注入任何非零读数）。
    `capsys` 那一格钉的是"过了也要把四条的数打出来"，不是打过一句 HINT —— 判据不过时才打 HINT。
    """
    report = check(tmp_path)
    assert report.passed is True, report
    assert report.console_error_count == report.dead_link_count == report.empty_block_count == 0, report
    assert (len(report.composition.pages), len(report.composition.styles)) == (3, 1), report.composition
    assert verify_module._report(report) == verify_module.EXIT_OK, "退出码与判定脱钩 = 这条命令不可用"
    printed = capsys.readouterr().out
    assert "VERIFY PACKAGE ::" in printed and "VERIFY HINT" not in printed, printed


# --------------------------------------------- 二、来源、成员来源、通道的所有权


def test_the_expected_composition_is_pinned_to_the_quickstart_row():
    """`EXPECT_HTML / EXPECT_CSS` 必须等于 §4 表第一行写的那两个数 —— 文档是来源，代码是抄件。

    ② 那一格的证人：常量改成 2 或 5 ⇒ 红；§4 那行改了而代码没跟 ⇒ 也红。两个方向都红正是想要的。
    """
    line = next(row for row in QUICKSTART.read_text(encoding="utf-8").splitlines()
                if row.startswith("| 产出物数量 |"))
    html = int(re.search(r"(\d+) 个页面文件", line).group(1))
    css = int(re.search(r"(\d+) 个样式文件", line).group(1))
    assert (verify_module.EXPECT_HTML, verify_module.EXPECT_CSS) == (html, css), line


def test_membership_is_read_from_the_package_and_never_re_decided_here():
    """成员 = 包 JSON 里那串 `rel_path`；本模块一次都不重判"有没有资格进包"。

    第二条断的是"核验段不去读 EN-06"：一旦它自己 `select(Artifact)`，就会出现"包里有四份、
    核的只有三份"这种谁也说不清的读数 —— 那正是 ③ 要挡的口径分叉。
    """
    tree = module_tree()
    assert "validated_manifest" not in called_names(tree), "组包段那道筛被抄了第二份"
    assert "validated_status" not in attributes(tree), "核验段不许读那一列"
    assert set(select_targets(tree)) == {"Deliverable"}, select_targets(tree)
    assert "_member_paths" in called_names(func_body("composition_of"))


def test_the_console_channel_is_the_only_subprocess_and_has_no_network_exit():
    """console 那一半是唯一的外部进程点，且本模块没有任何网络出口。

    · `subprocess.run` 出现恰好 1 次（在 `run_cdp_driver` 里）⇒ 想多起一个浏览器得先过这条；
    · 禁 `httpx` / `requests` / `urllib.request` / `socket` ⇒ 这一发的边界是不联网；
      `urlsplit` 这类**解析**工具不在禁列（它不发包）；
    · 浏览器可执行文件的默认路径只写在驱动里，本模块出现 0 次 ⇒ 同一事实只有一份。
    """
    tree = module_tree()
    assert called_names(tree).count("run") == 1, called_names(tree).count("run")
    modules = imported_modules(tree)
    assert not {name.split(".")[0] for name in modules} & {"httpx", "requests", "socket"}, modules
    assert not any(name.startswith("urllib.request") for name in modules), modules
    assert "msedge.exe" not in SRC.read_text(encoding="utf-8")


def test_the_default_console_channel_is_the_real_driver_not_a_stub():
    """`runner` 不给时走 `run_cdp_driver` —— 默认值必须是真通道。

    挡的错很具体：判据里注入假 runner 用惯了，有人会把默认值也写成"返回空读数"，
    于是 `python -m acp.verify_mvp1` 在用户手里永远是绿的，而它什么都没打开过。
    """
    body = func_body("verify")
    assert "run_cdp_driver" in called_names(body), body
    assert "msedge" not in SRC.read_text(encoding="utf-8")
    driver = pathlib.Path(verify_module.DRIVER_PATH)
    assert driver.name == "cdp_console.mjs" and driver.is_file(), driver
    text = driver.read_text(encoding="utf-8")
    assert "DevToolsActivePort" in text, "端口发现只许走这条（stderr 被 libpng 警告灌满，实测抓不到）"


def test_the_page_line_prints_the_unchecked_bucket():
    """PAGE 那行必须带 `unchecked_links` 这一格 ⇒ "未核验的外链数"在输出面上看得见。

    ④ 的反面：不打印的话，一个含两条外链的站点会打印"dead_links=0"，读的人以为全部链接都能点。
    """
    fields = emit_keywords("_report", "PAGE")
    assert {"path", "console_errors", "dead_links", "empty_blocks", "unchecked_links"} <= fields, fields


# --------------------------------------------------------------- 三、链接怎么解


def test_links_resolve_against_their_own_page_directory_not_by_basename(tmp_path):
    """同名文件在**另一个** workspace 里 ⇒ 那是死链；同级存在 ⇒ 那不是。

    现场刻意摆成"盘上有这个 basename、但不在这一页旁边"：入口页链 `about.html`，而 `about.html`
    只在 `doubao-workspace/`。任何"按 basename 在成员里找"的实现都会放行它，而浏览器打开
    `file:///…/qoder-workspace/about.html` 得到的是 ERR_FILE_NOT_FOUND。
    这一发同时给出 §4"可预览"的前提读数：四份文件落在同一个目录才谈得上原位可预览。
    """
    elsewhere = staged(tmp_path / "split", {
        ENTRY: '<a href="about.html">x</a>',
        "doubao-workspace/about.html": "<p>two</p>",
    })
    assert verify_module.resolve_links(elsewhere, ENTRY, ["about.html"]) == (("about.html",), ()), \
        "按 basename 放宽了"
    together = staged(tmp_path / "same", {ENTRY: '<a href="about.html">x</a>', ABOUT: "<p>two</p>"})
    assert verify_module.resolve_links(together, ENTRY, ["about.html"]) == ((), ()), "同级存在却报死链"


def test_external_and_non_file_hrefs_are_registered_not_resolved(tmp_path):
    """外链进 `unchecked`；`#frag` 与 `mailto:` 两格都不进；空 href 与根相对路径进死链。

    三堆各有各的错法：把外链当死链 ⇒ 不联网的核验假红；外链完全不记 ⇒ "0 死链"被读成"全部可点"；
    空 href 当无害 ⇒ `href=""` 点击回到本页顶部，与写它的人的意图相反，那是缺陷不是空白。
    """
    root = staged(tmp_path, {ENTRY: "<p>x</p>"})
    dead, unchecked = verify_module.resolve_links(
        root, ENTRY,
        ["https://acp.invalid/a", "http://acp.invalid/b", "#top", "mailto:team@example.invalid",
         "", "/abs.html", "styles.css"],
    )
    assert unchecked == ("https://acp.invalid/a", "http://acp.invalid/b"), unchecked
    assert dead == ("", "/abs.html", "styles.css"), dead


# ------------------------------------------------------- 四、通道与前置的拒绝


def test_a_missing_console_channel_is_a_refusal_not_a_zero(tmp_path):
    """`node` 不在 = 没有读数来源 ⇒ 拒（`console_driver_missing`），而不是"console 错误 0 条"。

    这一发**注的是假现场、真通道**（`use_real_channel`）：要的就是 `FileNotFoundError` 由操作系统给
    的那一下。用假 runner 证它就没有力气 —— 假 runner 永远不会不回来；把 `subprocess` 换成假的
    又会变成"判据在演自己写的戏"。
    """
    with pytest.raises(verify_module.VerifyFailure) as caught:
        check(tmp_path, use_real_channel=True, node="__acp_no_such_node_binary__")
    assert caught.value.code == verify_module.CODE_DRIVER_MISSING, caught.value.code


def test_a_short_reading_set_refuses_instead_of_scoring_the_missing_page_as_clean(tmp_path):
    """驱动少回一页 ⇒ 拒。挡的是"页集与读数集各算各的"：少那一页会被当成 0 错误、0 死链。"""
    index = str(tmp_path / PROJECT / ENTRY)

    def short(paths):
        return {index: reading(index)}

    with pytest.raises(verify_module.VerifyFailure) as caught:
        check(tmp_path, runner=short)
    assert caught.value.code == verify_module.CODE_DRIVER_FAILED, caught.value.code


def test_a_package_json_that_disagrees_with_the_row_refuses_before_any_reading(tmp_path):
    """包 JSON 的入口与 EN-17 不一致 ⇒ 拒，且**一次都不启动驱动**。

    runner 里放的是炸点：驱动被叫到就是本发输 —— 要的形状是"拒绝发生在浏览器之前"，
    否则一次分叉的现场会被量出读数并打印成一条看起来像结论的东西。
    """
    def boom(paths):
        raise AssertionError("前置没成立就起了 console 通道")

    with pytest.raises(verify_module.VerifyFailure) as caught:
        check(tmp_path, package_entry=f"{WORK}/other.html", runner=boom)
    assert caught.value.code == verify_module.CODE_PACKAGE_DIVERGED, caught.value.code


def test_a_manifest_member_with_no_file_on_disk_refuses(tmp_path):
    """索引点了名、盘上没那份文件 ⇒ `package_member_missing`（这不是死链，是索引在说谎）。

    与死链分开两个代号的原因：死链是"页面里某个链接点不开"，成员缺失是"整份索引不可信"，
    后者的构成判据没有可信读数可报，只能停。
    """
    with pytest.raises(verify_module.VerifyFailure) as caught:
        check(tmp_path, bodies=GOOD_BODIES, manifest=(ENTRY, ABOUT, CONTACT, STYLE, f"{WORK}/gone.html"))
    assert caught.value.code == verify_module.CODE_MEMBER_MISSING, caught.value.code


def test_zero_or_two_packages_under_one_project_key_both_refuse(tmp_path):
    """0 张包 ⇒ `deliverable_absent`；2 张 ⇒ `deliverable_ambiguous` 并要求 `--request`；
    点名了一张不存在的需求单 ⇒ 仍是 `deliverable_absent`（不许退化成"按项目键再找一遍"）。

    "取最新那张"这一形在这里被挡死：两条需求单的产物不同，默认取新等于让一次敲错的项目键
    把另一条的交付当成这一发的读数。
    """
    with pytest.raises(verify_module.VerifyFailure) as absent:
        verify_module.deliverable_row(FakeSession(()), project_key=PROJECT, feature_request_id=None)
    assert absent.value.code == verify_module.CODE_ABSENT, absent.value.code
    with pytest.raises(verify_module.VerifyFailure) as ambiguous:
        verify_module.deliverable_row(FakeSession((row_for(), row_for())), project_key=PROJECT,
                                      feature_request_id=None)
    assert ambiguous.value.code == verify_module.CODE_AMBIGUOUS, ambiguous.value.code
    with pytest.raises(verify_module.VerifyFailure) as named_absent:
        verify_module.deliverable_row(FakeSession(()), project_key=PROJECT, feature_request_id=uuid.uuid4())
    assert named_absent.value.code == verify_module.CODE_ABSENT, named_absent.value.code


# ------------------------------------------------------------------- 五、命令行


def test_a_malformed_request_id_is_a_usage_exit_before_any_connection():
    """`--request` 不是 UUID ⇒ 退出码 2（用法档），而且排在连库之前。

    这一发不注入 `ACP_DB_DSN` 也照样跑到那一格：解析 UUID 在 `make_session_factory()` 之前，
    所以它证明的是"敲错一个字符不会去库里找另一条现场"。
    """
    assert verify_module.main(["--project", PROJECT, "--request", "not-a-uuid"]) == verify_module.EXIT_USAGE


def test_the_cli_requires_a_project_key():
    """没有 `--project` ⇒ argparse 退 2，`main()` 把它统一成 `EXIT_USAGE`（与 `acp.run` 同档）。"""
    assert verify_module.main([]) == verify_module.EXIT_USAGE


# ------------------------------------------------------------------ 六、真 PG


def test_the_project_key_lookup_finds_the_row_the_chain_actually_wrote(bus, session):
    """真链跑完后按项目键取到的 EN-17 行 = `run_deliver` 落的那一行，入口取的是库里那一列。

    假 session 证不了这条 join（模块头第二条点名）⇒ 这一发是它唯一的证人；
    再加一条 `entry_path` 归一：核验段用的入口是 EN-17 的值，不是它自己按 `index.html` 猜的。
    """
    request_id = to_delivered(bus, session)
    try:
        found = verify_module.deliverable_row(session, project_key=PROJECT, feature_request_id=None)
        assert found.feature_request_id == request_id, found
        assert found.entry_path == ENTRY, found.entry_path
    finally:
        drop_thread(session, request_id)


def test_two_real_packages_under_one_project_key_have_to_be_named(bus, session):
    """同一个项目键下真有两张包 ⇒ 按项目键取会拒，按需求单号取各取各的（不是"取最新"）。

    第二张是在事务里造的（一条 `feature_request` + 一条 `deliverable`，都用模型构造器），
    所以这一发把 `--request` 那一格从"分支存在"升级成"两条真行都取对"。
    """
    first = to_delivered(bus, session)
    other = FeatureRequest(id=uuid.uuid4(), project_key=PROJECT, raw_input="第二条",
                           summary_digest={}, clarification_waived=False, status="DELIVERED", created_at=NOW)
    session.add(other)
    session.flush()
    second = row_for(request_id=other.id)
    session.add(second)
    session.flush()
    try:
        with pytest.raises(verify_module.VerifyFailure) as caught:
            verify_module.deliverable_row(session, project_key=PROJECT, feature_request_id=None)
        assert caught.value.code == verify_module.CODE_AMBIGUOUS, caught.value.code
        assert verify_module.deliverable_row(
            session, project_key=PROJECT, feature_request_id=first).feature_request_id == first
        assert verify_module.deliverable_row(
            session, project_key=PROJECT,
            feature_request_id=second.feature_request_id).id == second.id
    finally:
        drop_thread(session, first)


def test_the_real_chain_passes_all_four_judgments_on_a_clean_channel(bus, session):
    """整链跑完的真现场 + 一条什么都不报的 console 通道 ⇒ `passed is True`。

    断的全是这份现场自己的数：① 成员 3 页 1 样式、入口 = EN-17 那列；② 四份文件都在盘上同一目录
    （"可预览"押的就是这一格）；③ **死链是从盘上量的**，不是注入出来的 —— 每页那些相对链接
    在同级目录里逐个存在才拿得到 0；④ 驱动收到的页集 = 三页（`composition.pages` 不是自己数的数）。
    它**不**证明真浏览器报 0 条，那一格由 evidence §十五 的真跑读数负责。
    """
    request_id = to_delivered(bus, session)
    root = bus.path / PROJECT
    try:
        seen: list[list[str]] = []

        def spy(paths):
            seen.append([str(path) for path in paths])
            return clean_runner(root)(paths)

        report = verify_module.verify(project_key=PROJECT, bus_root=bus.path, session=session, runner=spy)
        assert report.project_key == PROJECT and report.feature_request_id == request_id, report
        assert len(report.composition.pages) == 3 and len(report.composition.styles) == 1, report.composition
        assert report.composition.other == () and report.composition.matches_expectation, report.composition
        assert report.composition.entry_path == ENTRY, report.composition.entry_path
        assert all((root / rel).is_file() for rel in report.composition.pages + report.composition.styles)
        dirs = {str(pathlib.PurePosixPath(rel).parent)
                for rel in report.composition.pages + report.composition.styles}
        assert dirs == {WORK}, f"四份文件不在同一目录，原位预览这一格当场不成立：{dirs}"
        assert report.dead_link_count == 0 and report.empty_block_count == 0, report
        assert report.passed is True, report
        assert sorted(pathlib.Path(rel).name for rel in seen[0]) == [
            "about.html", "contact.html", "index.html"], seen
    finally:
        drop_thread(session, request_id)
