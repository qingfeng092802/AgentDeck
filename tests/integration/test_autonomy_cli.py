"""T106 的判据：`python -m acp.autonomy` / `python -m acp.danger` / `selfcheck --show-danger`
（FR-055 / FR-058｜R-15）。

**这档真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿）：改档落 EN-16、越权读 EN-03 在册集合，
两件事都只能在库里读出来。**一处要点名的连带后果**：清单那几节（第五、六节）本身不碰库，
但它们按 owner 给的交付物清单落在同一档 ⇒ 没有 `.env` 时它们与 PG 那几节一起 skip，
读数会少 12 条而不是 0 条；复跑时先确认注入了 DSN。

owner 的约束 5 是这一轮的靶心：**CLI 与看板 / HTTP 调同一端点，不另开逻辑**。所以本文件的判据不是
"CLI 的输出好不好看"，而是三件可判的事：

1. **发出去的那一发长什么样**（方法 / 路径 / 头 / 体）—— 用一个记账的 `MockTransport`，
   它不执行任何业务，只把请求记下来 ⇒ "CLI 自己判了一遍"这种形状在这里当场露形；
2. **库里动的到底是哪几列** —— 用真端点（`TestClient` 挂 `autonomy.router`，注入项与 T103 同一套），
   发一次 HTTP 再回库里读，不凭响应体自述（口径同 `test_autonomy_api.py`）；
3. **拒的时候有没有半写** —— `--as-agent` 那一支要求"403 + 库里档位没动"两条同时成立。

两处刻意的写法：
- **身份两个旗标都不给就不许发**：这一条在本地判（退出码 2、请求 0 发），因为"没有主体"这句话
  不必占服务端一次机会；但**档位词不在本地判**（第四节），那是域的活。两者的分界就是这条判据的内容。
- **不回显地址**：传输层失败只打异常类名（`httpx` 的消息会带完整 URL），断的是"给的探测地址不出现在 stdout"。
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import io
import json
import os
import types
from pathlib import Path


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp import selfcheck
from acp import autonomy as autonomy_cli
from acp.api import autonomy as api_autonomy
from acp.autonomy import TIERS, CliUsage, body_of, principal_of
from acp.autonomy import main as autonomy_main
from acp.config.loader import (
    DANGER_INTERCEPT,
    DSN_ENV,
    REDACTED,
    DangerList,
    db_url,
    load_danger_list,
)
from acp.danger import candidate_text
from acp.danger import main as danger_main
from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.persistence import autonomy as store
from acp.persistence.enums import values_of
from acp.persistence.models_core import AgentUnit

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（档位与 EN-03 在册集合都只在库里读得出）",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 扫的是**当前被 import 的那一份** `acp.autonomy` 模块本体，不是按 `parents[2]` 拼出来的字面路径。
#: 少这一层间接，变异跑（`PYTHONPATH=/tmp/mutsrc`）时"源码里没有域值字面串"那条读的还是仓库里
#: 那棵没被改过的树 ⇒ 变异不红（T105 的假绿教训在源码扫描这一族的同一个形态）。
AUTONOMY_CLI_SOURCE = Path(autonomy_cli.__file__).resolve()

PROJECT = "acp-self-host"
HUMAN = "owner-1"
NOW = dt.datetime(2026, 10, 15, 14, 0, tzinfo=dt.timezone.utc)
FAKE_KEY = "sk-FAKEfakeFAKEfake0"
#: 只用来证明"地址不回显"的探测串，不是真端点。
LEAK_PROBE = "leak-probe.invalid"
MACHINES = frozenset({AUTOMATED_ACTOR})
_UNSET = object()


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
        try:
            yield s
        finally:
            s.close()
            transaction.rollback()


def seed_row(session: Session, mode: str = "FULL") -> store.AutonomyState:
    """起点用构造器直接写（不借被测路径的任何写者），指纹取真清单那份。"""
    row = store.AutonomyState(
        project_key=PROJECT,
        mode=mode,
        danger_list_version=load_danger_list().fingerprint,
        updated_by=HUMAN,
        updated_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def make_client(session: Session, *, principals: object = MACHINES) -> TestClient:
    """装配点替身（真装配归 T123）：与 `test_autonomy_api.py::make_client` 同一套注入项。

    这里**不给** `danger_list`：改档的既有行分支用不上它，给了反而把"缺它会怎样"这一格糊住
    （那一格由 `test_autonomy_api.py` 钉）。
    """
    app = FastAPI()
    app.include_router(api_autonomy.router)
    app.state.session_factory = lambda: session
    if principals is not _UNSET:
        app.state.machine_principals = principals
    return TestClient(app)


def run_cli(client, *argv: str) -> tuple[int, dict | None, str]:
    """跑一次改档 CLI，返回 `(退出码, 那一行 JSON, 原始 stdout)`。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = autonomy_main([*argv, "--project", PROJECT], client=client)
    out = buffer.getvalue()
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        parsed = None
    return code, parsed, out


def recorder() -> tuple[list[httpx.Request], httpx.Client]:
    """记账用的 client：把请求记下来并回一份 canned 200，**不执行任何业务**。

    为什么需要它：用真端点验"CLI 有没有自己判一遍"是验不出来的 —— 真端点会替它把判定做完，
    两边读数一样。只有"请求根本没发出去"或"发出去的体比契约多一个键"这种读数能证明路径只有一条。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "project_key": PROJECT,
                "mode": "SAFE",
                "danger_list_version": "sha256:" + "0" * 64,
                "updated_by": HUMAN,
                "updated_at": NOW.isoformat(),
                "last_intercept_at": None,
            },
        )

    return seen, httpx.Client(base_url="http://api.invalid", transport=httpx.MockTransport(handler))


def _register_agent(session: Session, agent_id: str) -> None:
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


# ── 一、发出去的那一发长什么样（约束 5：同一条路）─────────────────────────────


def test_the_cli_sends_exactly_the_patched_contract_shape() -> None:
    """方法 / 路径 / 头 / 体四样逐一对着 §1.5，**不多给一个键**。

    体里多一个 `updated_by` 之类的键会被端点的 `extra="forbid"` 挡成 422，但那时红的是服务端；
    这条在请求离开 client 之前就判，所以它钉的是"CLI 侧没有第二份请求形状"。
    """
    seen, client = recorder()

    with contextlib.redirect_stdout(io.StringIO()):
        code = autonomy_main(
            ["set", "safe", "--project", PROJECT, "--principal", HUMAN], client=client
        )

    assert code == 0
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "PATCH"
    assert request.url.path == "/autonomy"
    assert request.headers["x-acp-principal"] == HUMAN
    body = json.loads(request.content)
    assert set(body) == {"project_key", "mode"}
    assert body == {"project_key": PROJECT, "mode": "SAFE"}


def test_the_cli_reads_the_tier_from_the_database_not_from_its_own_output(
    session: Session,
) -> None:
    """`set safe` 真打下去：库里那一行变成 SAFE，而 `updated_by` / `updated_at` 是这一动写的。

    发一次 HTTP 再回库读，不凭 stdout 自述（口径同 T103 的判据形状）—— CLI 打印的是响应体，
    响应体是端点自己组的，拿它证明"落库了"是循环论证。
    """
    seed_row(session, "FULL")
    client = make_client(session)

    code, report, _ = run_cli(client, "set", "safe", "--principal", HUMAN)

    assert (code, report["http"], report["mode"]) == (0, 200, "SAFE")
    row = store.read_autonomy_state(session, project_key=PROJECT)
    assert str(row.mode) == "SAFE"
    assert row.updated_by == HUMAN and row.updated_at != NOW


def test_the_cli_touches_no_column_the_endpoint_does_not_own(session: Session) -> None:
    """改档之后 `danger_list_version` 与 `last_intercept_at` 都不动（第三个写者不在 CLI 这一侧）。

    失效形状：CLI 为了"看着完整"顺手把指纹重算一遍写进去 ⇒ 那会让清单重载的写者失去唯一性，
    而 EN-16 的读数分不出"是重载跟上了还是改档顺手动了"。
    """
    seed_row(session, "FULL")
    before = store.read_autonomy_state(session, project_key=PROJECT)
    snapshot = (before.danger_list_version, before.last_intercept_at)
    client = make_client(session)

    run_cli(client, "set", "safe", "--principal", HUMAN)

    after = store.read_autonomy_state(session, project_key=PROJECT)
    assert (after.danger_list_version, after.last_intercept_at) == snapshot


# ── 二、--as-agent 反向必 403（约束 2）───────────────────────────────────────


def test_an_automated_principal_gets_403_and_the_tier_does_not_move(session: Session) -> None:
    """总管工具发这一发 ⇒ 403 `principal_not_allowed`，**且库里档位一格没动**。

    两条同时成立才算"必 403"：只看状态码的话，"先写了再拒"这种形状是绿的。
    档位故意起点 FULL、请求 full（同档）也不行 —— 见下面那条同档用例，这里先按异档验。
    """
    seed_row(session, "FULL")
    client = make_client(session)

    code, report, _ = run_cli(client, "set", "safe", "--as-agent", AUTOMATED_ACTOR)

    assert (code, report["http"], report["code"]) == (1, 403, "principal_not_allowed")
    assert str(store.read_autonomy_state(session, project_key=PROJECT).mode) == "FULL"


def test_the_refusal_survives_a_same_tier_write_attempt(session: Session) -> None:
    """"档位没变所以看不出动没动"这一格单独堵掉：起点 FULL、请求也写 FULL。

    改档一定写 `updated_at`（T102 成文过），所以这一发的确会推进时刻 —— 被拒时**连时刻都不许动**。
    只比较 `mode` 的用例在这一发上是绿的，那是恒真读数。
    """
    seed_row(session, "FULL")
    before = store.read_autonomy_state(session, project_key=PROJECT)
    client = make_client(session)

    code, report, _ = run_cli(client, "set", "full", "--as-agent", AUTOMATED_ACTOR)

    after = store.read_autonomy_state(session, project_key=PROJECT)
    assert (code, report["http"]) == (1, 403)
    assert (after.mode, after.updated_by, after.updated_at) == (
        before.mode,
        before.updated_by,
        before.updated_at,
    )


def test_a_registered_agent_id_is_refused_even_without_the_machine_set(
    session: Session,
) -> None:
    """两道判定各拦一次：EN-03 在册的 `agent_id`（`doubao`）在**没注入自动主体集合**时也被拒。

    方向要说清：`machine_principals` 是 fail-closed 注入项，缺它整条端点 503（那一格由
    `test_autonomy_api.py` 钉）；这里注入的是**空有值的集合**（只含 orchestrator），
    而 `doubao` 靠 EN-03 那条被拒 ⇒ 两条来源互不替代。
    """
    seed_row(session, "FULL")
    _register_agent(session, "doubao")
    client = make_client(session, principals=MACHINES)

    code, report, _ = run_cli(client, "set", "safe", "--as-agent", "doubao")

    assert (code, report["http"], report["code"]) == (1, 403, "principal_not_allowed")
    assert str(store.read_autonomy_state(session, project_key=PROJECT).mode) == "FULL"


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (("--principal", HUMAN, "--as-agent", AUTOMATED_ACTOR), "两个都给"),
        ((), "两个都不给"),
        (("--principal", "   "), "空白主体"),
    ],
)
def test_identity_misuse_is_refused_locally_without_sending_anything(argv: tuple[str], reason: str) -> None:
    """身份这一格在本地判（退出码 2），**且一发都没出去**。

    为什么这一条与下一条（档位词）方向相反：主体缺失是"命令没写完"，服务端无从判；
    而"档位词是不是第三档"是域的活，本地预拒就会造出第二个拒的口径。
    """
    seen, client = recorder()
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        code = autonomy_main(["set", "safe", "--project", PROJECT, *argv], client=client)

    assert code == 2, (reason, buffer.getvalue())
    assert buffer.getvalue().startswith("usage error:")
    assert seen == []


# ── 三、档位词：形状映射取自域，不在本地当第二道门 ────────────────────────────


@pytest.mark.parametrize("spelling", ["safe", "SAFE", "Safe", "  safe  ", " full  "])
def test_the_domain_words_are_normalized_to_the_domain_value_in_every_spelling(
    spelling: str,
) -> None:
    """`set safe`（owner 给的写法）与 `set SAFE` 都发域值，映射表**由域派生**。

    断言只走 `body_of()`（产品面），不在用例里再抄一遍 `strip().casefold()` —— 抄了就成了
    "拿实现算实现"，实现里删掉 `strip` 这条用例照样绿。空白也吃掉：这一层是"形状"而不是
    "合法性"，域里没有带空白的取值，洗空白不会把一个非法词变成合法词。
    """
    expected = "FULL" if "full" in spelling else "SAFE"
    assert body_of(PROJECT, spelling)["mode"] == expected


def test_the_tier_table_is_the_domain_casefolded_neither_more_nor_less() -> None:
    """符合性用例（跨实现）：CLI 的映射表 **键集 = 域值小写集**、值集 = 域值集。

    这条才是"两个载体"的对照：`TIERS` 由 `values_of("autonomy_mode")` 派生，所以
    ① 域里加第三档 ⇒ 这里红（CLI 收不到它，而契约说端点会答 422 —— 两处口径分家）；
    ② 有人把 `TIERS` 改成手抄字面量清单并漏一档 ⇒ 这里也红。上一节的四条做不到这两件事。
    """
    domain = list(values_of("autonomy_mode"))
    assert set(TIERS) == {value.casefold() for value in domain}
    assert sorted(TIERS.values()) == sorted(domain)


def test_an_unknown_tier_is_sent_as_is_and_the_domain_answers_422(session: Session) -> None:
    """CLI 不预拒第三档写法：原样发出去，由 `values_of("autonomy_mode")` 答 422。

    这条是上一节的对偶：如果 CLI 自己拿一张表拒词，这一发就会变成退出码 2 而**没有 HTTP 读数**，
    那时"第三档不存在"这句话有两个人在说，而契约 §1.5 只登记了服务端那一个。
    """
    seed_row(session, "FULL")
    client = make_client(session)

    code, report, _ = run_cli(client, "set", "paranoid", "--principal", HUMAN)

    assert (code, report["http"], report["code"], report["sent_mode"]) == (
        1,
        422,
        "mode_not_allowed",
        "paranoid",
    )
    assert str(store.read_autonomy_state(session, project_key=PROJECT).mode) == "FULL"


def _tier_literals(source: str) -> set[str]:
    """一份源码里"域值字面串"的集合（`ast.Constant` 的字符串，**先剔掉文档串**）。

    文档串要剔是因为口径说明里会写 `SAFE` / `FULL` 这两个词来说明"从哪来"，那不是第二份载体。
    剔法是节点身份（`id()`），不是内容比对 —— 按内容比会把与文档串同字的真载体一起放过。
    """
    tree = ast.parse(source)
    doc_consts = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                doc_consts.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in doc_consts
    }


def test_the_cli_module_holds_no_tier_literals() -> None:
    """改档 CLI 的源码里不许出现 `"SAFE"` / `"FULL"` 字面串（域值的唯一载体在 `persistence/enums.py`）。

    失效形状很具体：有人把映射表写成 `{"safe": "SAFE", "full": "FULL"}`
    ⇒ 域改了名而 CLI 还收着旧词，读数看着正常。
    """
    literals = _tier_literals(AUTONOMY_CLI_SOURCE.read_text(encoding="utf-8"))
    assert literals & set(values_of("autonomy_mode")) == set(), sorted(literals)


@pytest.mark.parametrize(
    "source",
    [
        'TIERS = {"safe": "SAFE", "full": "FULL"}\n',
        'def f():\n    return {"mode": "SAFE"}\n',
        'MODE = "FULL"\n',
    ],
)
def test_the_tier_literal_scanner_fires_on_each_shape_it_claims_to_cover(
    source: str,
) -> None:
    """**先证扫描器会开火**，再拿它去断真模块 0 命中 —— 否则那个 0 可能只是扫描面本来就是空的。

    三种形态对应三种真实退化：手抄映射表、把域值写进请求体、把域值提成模块常量（后者最像"顺手重构"）。
    文档串豁免的**限度**同时钉住：写在 docstring 里的 `SAFE` 不算载体，所以本扫描器抓不到
    "把载体藏进 f-string 变量名"这一类（那是名字流分析的活，不在 T106 射程）。
    """
    assert _tier_literals(source) & {"SAFE", "FULL"}, source


def test_a_docstring_mention_of_a_tier_word_is_not_counted_as_a_carrier() -> None:
    """反向对照（不许误红）：文档串里写 `SAFE` 是**说明口径**，判据必须放过它。

    少了这一条，上一节就成了"任何位置出现这两个词都红"，而模块头的第 2 条决定正好要写它们 ——
    那时要么判据被放宽成整串子匹配（更坏），要么文档被删掉（把 why 删了）。
    """
    assert _tier_literals('"""`SAFE` 与 `FULL` 由域派生。"""\nx = 1\n') == set()


# ── 四、地址与凭据不回显 ─────────────────────────────────────────────────────


def test_no_base_url_and_no_client_names_no_address() -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = autonomy_main(["set", "safe", "--project", PROJECT, "--principal", HUMAN])
    out = buffer.getvalue()
    assert code == 2 and "base-url" in out
    assert "http://" not in out and "127.0.0.1" not in out


def test_a_transport_failure_prints_only_the_exception_class() -> None:
    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    client = httpx.Client(
        base_url=f"http://{LEAK_PROBE}:1", transport=httpx.MockTransport(raise_connect)
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = autonomy_main(
            ["set", "safe", "--project", PROJECT, "--principal", HUMAN], client=client
        )
    out = buffer.getvalue()
    assert code == 3, out
    assert "ConnectError" in out
    assert LEAK_PROBE not in out and "://" not in out


def test_a_rejection_detail_from_the_server_is_washed_before_the_terminal(
    session: Session,
) -> None:
    """服务端 detail 里会带上我们发过去的那个词 ⇒ 打到终端前过 `redact_bus_text()`。

    构造方式很自然：把假凭据当档位词发出去，422 的消息里就有它的原文。
    断言取两侧：原文不许出现，而"这里洗过"这件事要有可读的痕迹（不是数某个占位串出现几次，
    只要出现一次即可 —— 洗法的形状属 `config/loader.py` 的判据）。
    """
    seed_row(session, "FULL")
    client = make_client(session)

    code, report, out = run_cli(client, "set", FAKE_KEY, "--principal", HUMAN)

    assert (code, report["http"]) == (1, 422)
    assert FAKE_KEY not in out
    assert REDACTED in out


def test_a_credential_shaped_project_key_is_washed_at_the_same_boundary() -> None:
    """`_report` 明写它洗三个面 ⇒ 三个面各注入一次，不能只测 `detail` 那一个。

    这条走成功路径（canned 200，不碰库）：`--project` 是唯一由使用者自由填写、又会**原样回到
    stdout** 的字段，把它填成凭据形状是"这一行被贴进总线文件 / 聊天记录"的真实失效形状。
    两侧都要断：请求体里仍是原文（CLI 不替域判 `project_key`，脱敏只发生在打印边界），
    而终端那一行不许有原文 —— 反过来（把发出去的也洗掉）会让端点收到一个永远查不到的键。
    """
    seen, client = recorder()
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        code = autonomy_main(
            ["set", "safe", "--project", FAKE_KEY, "--principal", HUMAN], client=client
        )

    out = buffer.getvalue()
    assert code == 0, out
    assert FAKE_KEY not in out
    assert REDACTED in out
    assert json.loads(seen[0].content)["project_key"] == FAKE_KEY


# ── 五、selfcheck --show-danger（约束 4）──────────────────────────────────────


def test_show_danger_prints_the_fingerprint_the_guard_reads() -> None:
    """打印的指纹必须与拦截路径当时用的是**同一个数**（同一份读法，不是两份实现）。

    复算命令：`PYTHONPATH=src python -m acp.selfcheck --show-danger`。
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = selfcheck.main([selfcheck.SHOW_DANGER_FLAG])
    out = buffer.getvalue()
    loaded = load_danger_list()

    assert code == 0, out
    assert f"fingerprint={loaded.fingerprint}" in out
    assert f"entries={len(loaded.entries)}" in out
    assert out.count("entry ") == len(loaded.entries)
    assert f"actions={[DANGER_INTERCEPT]}" in out


def test_show_danger_does_not_run_the_checkpoint_gate() -> None:
    """这个旗标是**呈现入口**，不是门禁项：不许动 `CHECKPOINT0` 的判据面。

    读数很具体：输出里没有 `CHECKPOINT0` 那行、也没有 `[C1 interpreter]`。
    要把它升成 C6 得 owner 裁（登记在 `evidence/phase-08.md §十二`）。
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        selfcheck.main([selfcheck.SHOW_DANGER_FLAG])
    out = buffer.getvalue()

    assert "CHECKPOINT0" not in out and "[C1" not in out
    assert out.startswith("DANGER PASS")


def test_show_danger_fails_loudly_when_the_list_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """读不出清单 ⇒ 退出码 1，而不是"没有清单 = 谁都不拦"的那句安慰话通过。

    方向与 loader 那条口径一致（空文件按错误处理）。这里 monkeypatch 的是 loader 的函数属性，
    所以走的是 `show_danger()` 里那条 `except DangerListError` 分支，不是导入失败那条。
    """
    from acp.config import loader

    def boom(path=None) -> DangerList:
        raise loader.DangerListError("编一个读不出的理由")

    monkeypatch.setattr(loader, "load_danger_list", boom)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = selfcheck.main([selfcheck.SHOW_DANGER_FLAG])

    assert code == 1
    assert "DANGER FAIL" in buffer.getvalue()


# ── 六、python -m acp.danger add（约束 3）────────────────────────────────────


def list_copy(tmp_path: Path) -> Path:
    """把真清单复制到临时目录 —— **生产那份文件不许被用例碰**（下面第二条判的就是这件事）。"""
    target = tmp_path / "danger_list.yaml"
    source = REPO_ROOT / "config" / "danger_list.yaml"
    target.write_bytes(source.read_bytes())
    return target


def test_add_appends_a_user_added_entry_and_moves_the_fingerprint(tmp_path: Path) -> None:
    """扩展项落成 `user_added: true` + 默认 `intercept`，指纹跟着变（FR-058）。

    指纹"跟着变"这件事在这里是**语义变化**的证据：新条目改变了拦截判据，不是排版。
    """
    target = list_copy(tmp_path)
    before = load_danger_list(target)
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        code = danger_main(["add", "--pattern", "my-tool  wipe"], path=target)
    report = json.loads(buffer.getvalue().strip())
    after = load_danger_list(target)

    assert code == 0, buffer.getvalue()
    assert report["added"] == "my-tool wipe" and report["action"] == DANGER_INTERCEPT
    assert report["user_added"] == before.user_added_count + 1 == after.user_added_count
    assert after.fingerprint != before.fingerprint
    assert after.entries[-1].pattern == "my-tool wipe"
    assert after.entries[-1].user_added is True


def test_a_rejected_extension_leaves_the_production_list_byte_identical(tmp_path: Path) -> None:
    """重复项被 loader 拒 ⇒ 临时副本与**生产那份**都不许有一个字节的差。

    这条同时钉两件事：① 校形只交给 loader（CLI 里没有第二份校验），
    ② 用例注入 `path=` 时不碰 `config/danger_list.yaml`（`load_danger_list()` 那句
    "生产调用方 MUST 不传" 的口径对齐，写在 `acp/danger/__init__.py` 模块头第 3 条）。
    """
    source = REPO_ROOT / "config" / "danger_list.yaml"
    production_before = source.read_bytes()
    target = list_copy(tmp_path)
    before = target.read_bytes()

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = danger_main(["add", "--pattern", "RM  -RF"], path=target)

    assert code == 2 and target.read_bytes() == before
    assert source.read_bytes() == production_before
    assert "usage error" in buffer.getvalue()


def test_add_preserves_the_comment_header(tmp_path: Path) -> None:
    """扩展一条不许把文件头的口径说明抹掉（那是"为什么"，不只是"是什么"）。

    写法上的对应决定在 `acp.danger.candidate_text()`：只 dump **新增那一行**拿到转义，
    其余原文照抄 —— 整份 `safe_load` + `safe_dump` 会把十几段注释全丢，而指纹不变，
    所以那种破坏在判据上看不见，只能这样单独钉。
    """
    target = tmp_path / "danger_list.yaml"
    target.write_text(
        "# 口径说明第一行\n# 口径说明第二行\n- pattern: sudo\n",
        encoding="utf-8",
    )

    with contextlib.redirect_stdout(io.StringIO()):
        code = danger_main(["add", "--pattern", "my-tool wipe"], path=target)

    text = target.read_text(encoding="utf-8")
    assert code == 0
    assert "# 口径说明第一行\n# 口径说明第二行\n" in text
    assert "user_added: true" in text


def test_a_blank_or_non_add_verb_is_a_usage_error(tmp_path: Path) -> None:
    target = list_copy(tmp_path)
    before = target.read_bytes()
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        blank = danger_main(["add", "--pattern", "   "], path=target)
        remove = danger_main(["remove", "--pattern", "sudo"], path=target)

    assert (blank, remove) == (2, 2)
    assert target.read_bytes() == before
    assert "只有 add" in buffer.getvalue()


def test_the_danger_entry_has_no_way_to_ask_for_a_pass(tmp_path: Path) -> None:
    """没有 `--action` 这个旗标：FR-058 那句"新增项默认拦截"不给可撤销的口子。

    两种退化都要挡：① 给一个 `--action allow` 的旗标 —— argparse 认不出来，按 `acp.approve`
    的既有口径这是 `SystemExit(2)`（"本地就能判的用法错"这一族），不是我们自己的 return 2，
    所以这里断的是**抛出去的状态码**；判据若只写 `code == 2` 就会在这一发上直接崩而不是红；
    ② 新增那一行里偷偷带 `action:`（`candidate_text` 只给 `pattern` 与 `user_added`）。
    """
    target = list_copy(tmp_path)
    before = target.read_bytes()
    buffer = io.StringIO()

    with pytest.raises(SystemExit) as refused, contextlib.redirect_stdout(buffer):
        danger_main(["add", "--pattern", "x", "--action", "allow"], path=target)
    assert refused.value.code == 2
    assert target.read_bytes() == before  # 被拒的那一发一个字没写

    produced = candidate_text("- pattern: sudo\n", "my-tool wipe")
    assert "action:" not in produced
    assert "user_added: true" in produced


def test_the_identity_helper_is_the_only_place_that_judges_identity() -> None:
    """`principal_of` 的三种形状（正向一条 + 三条拒）各判一次，且它**不碰库**。

    为什么单独判这个纯函数：403 那三条走的是真端点（第二节），而"两个旗标同时给"这种形状
    根本发不出请求 —— 它只能在函数这一层判，不判就是空档。
    """
    def args(**overrides: object) -> types.SimpleNamespace:
        base = {"principal": None, "as_agent": None}
        base.update(overrides)
        return types.SimpleNamespace(**base)

    assert principal_of(args(principal=HUMAN)) == HUMAN
    assert principal_of(args(as_agent=AUTOMATED_ACTOR)) == AUTOMATED_ACTOR
    for bad, match in (
        (args(principal=HUMAN, as_agent=AUTOMATED_ACTOR), "只能给一个"),
        (args(), "MUST 给"),
        (args(principal="  "), "空白"),
    ):
        with pytest.raises(CliUsage, match=match):
            principal_of(bad)


def test_the_verb_surface_is_set_only() -> None:
    """改档通道只有 `set`：`get` / `show` 这类读法**没开**（契约 §1.5 只有 PATCH 这一行）。

    登记在册的一条仍开项：§六 ⑬ 那句"CLI 呈现取 `DEFAULT_MODE`"因此在 T106 仍没有落点
    —— 读侧要么开 GET（owner 未裁），要么由 `selfcheck` 承担，这一条把它钉成读数而不是散文。
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = autonomy_main(
            ["show", "safe", "--project", PROJECT, "--principal", HUMAN],
            client=recorder()[1],
        )
    assert code == 2
    assert "只有 set" in buffer.getvalue()
