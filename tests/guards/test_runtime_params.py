"""MVP-2 第三步 ②：运行时可调参数的判据（`config/runtime.py` + `GET/PUT /runtime`）。

owner 要的是"随时调整"。这句话里有两个必须分开钉的东西：**改了值** 与 **值真的被读到**。
本文件的第三节就是为后者写的 —— 它数"谁调了加载器又在同一个函数里取了那个键"，
所以一个只存在于面板上的数会被读成空列表，而不是被注释写成"已生效"。

三节分工：

- 节 一 = 权威源绑定（`params-ref §12`）：文件名、封闭键集、两个界限，两侧逐字比。
- 节 二 = 加载器的四条规则：表外键拒、缺键拒、值按语义校形、**没有默认值回退**、模块级不缓存。
- 节 三 = 端点面：读、写（带身份）、写完立刻能被读回，以及"不许碰仓库里那份真文件"的隔离。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.api import web
from acp.api.approvals import PRINCIPAL_HEADER
from acp.app import create_app
from acp.config import runtime

REPO = Path(__file__).resolve().parents[2]
PARAMS_REF = REPO / "config" / "params-ref.md"
HEADERS = {PRINCIPAL_HEADER: "owner-1"}


def fence_values() -> dict[str, str]:
    text = PARAMS_REF.read_text(encoding="utf-8")
    section = text.split("## 12. ", 1)
    assert len(section) == 2, "params-ref 里没有 §12 ⇒ 运行时参数没有权威出处"
    block = re.search(r"```text\n(.*?)```", section[1], re.S)
    assert block, "§12 里没有 ```text 围栏"
    return {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in block.group(1).splitlines()
        if "=" in line
    }


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch):
    """把加载器指到临时目录。**端点与判据都不许动仓库里那份 `config/runtime.yaml`**：
    面板保存走的是生产路径，测试要是直接跑在它上面，就会把使用者设的值改掉。
    """
    target = tmp_path / "runtime.yaml"
    monkeypatch.setattr(runtime, "RUNTIME_PATH", target)
    target.write_text(
        runtime.dump_runtime_text(runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=500, auto_accept_low_risk=None, source_sha256="")),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------- 一、权威源绑定


def test_section_12_registers_the_same_file_keys_and_bounds():
    values = fence_values()
    assert Path(values["RUNTIME_FILE"]) == runtime.RUNTIME_PATH.relative_to(REPO), "参数文件的位置两侧不同"
    assert tuple(values["RUNTIME_KEYS"].split(",")) == runtime.RUNTIME_KEYS, "封闭键集两侧不同集"
    assert int(values["RUNTIME_MIN_FRAGMENT_MERGE_MS"]) == runtime.MIN_FRAGMENT_MERGE_MS
    assert int(values["RUNTIME_MAX_TOKENS_BOUND"]) == runtime.MAX_TOKENS_BOUND


def test_the_shipped_file_is_valid_and_within_the_registered_bounds():
    """仓库里那份真的能被读出来 —— 否则 §12 写的一切都是一句承诺。"""
    params = runtime.load_runtime_params()
    assert set(params.as_dict()) == set(runtime.RUNTIME_KEYS)
    assert params.fragment_merge_ms >= runtime.MIN_FRAGMENT_MERGE_MS
    assert params.max_tokens is None or 1 <= params.max_tokens <= runtime.MAX_TOKENS_BOUND


# ---------------------------------------------------------------- 二、加载器四条规则


def test_a_key_outside_the_closed_set_is_refused(sandbox: Path):
    sandbox.write_text("max_tokens: null\nfragment_merge_ms: 500\nretry_seconds: 3\n", encoding="utf-8")
    with pytest.raises(runtime.RuntimeParamsError, match="表外键"):
        runtime.load_runtime_params()


def test_a_missing_key_is_refused(sandbox: Path):
    sandbox.write_text("max_tokens: null\n", encoding="utf-8")
    with pytest.raises(runtime.RuntimeParamsError, match="缺必填键"):
        runtime.load_runtime_params()


@pytest.mark.parametrize(
    "body,marker",
    [
        ("max_tokens: true\nfragment_merge_ms: 500\nauto_accept_low_risk: null\n", "整数"),
        ("max_tokens: 0\nfragment_merge_ms: 500\nauto_accept_low_risk: null\n", "1.."),
        ("max_tokens: 99999999999\nfragment_merge_ms: 500\nauto_accept_low_risk: null\n", "1.."),
        ("max_tokens: null\nfragment_merge_ms: 0\nauto_accept_low_risk: null\n", "≥"),
        ("max_tokens: null\nfragment_merge_ms: 1.5\nauto_accept_low_risk: null\n", "整数"),
        # 2026-10-28 新增的那一格：`"true"`（带引号的字符串）**不猜**，必须是真布尔或 null。
        # 猜错方向就是把机器代签悄悄打开 —— 所以这条必须报错，而不是当 1 收下。
        ("max_tokens: null\nfragment_merge_ms: 500\nauto_accept_low_risk: \"true\"\n", "true/false"),
        ("max_tokens: null\nfragment_merge_ms: 500\nauto_accept_low_risk: 1\n", "true/false"),
        ("max_tokens: null\nfragment_merge_ms: 500\nextra: 1\nauto_accept_low_risk: null\n", "表外键"),
        ("- 这不是映射\n", "映射"),
    ],
)
def test_each_bad_shape_has_its_own_message(sandbox: Path, body: str, marker: str):
    sandbox.write_text(body, encoding="utf-8")
    with pytest.raises(runtime.RuntimeParamsError) as caught:
        runtime.load_runtime_params()
    assert marker in str(caught.value), str(caught.value)


def test_a_missing_file_is_an_error_not_a_set_of_defaults(tmp_path: Path):
    """读不到文件 ⇒ 报错。**不许**"当成用了默认值"：那会把"配置丢了"读成"配置没改"。"""
    with pytest.raises(runtime.RuntimeParamsError, match="回退"):
        runtime.load_runtime_params(tmp_path / "nope.yaml")


def test_the_loader_caches_nothing(sandbox: Path):
    """改完即生效（owner 那句"随时调整"的实现形态）：两次调用之间不许有记忆。"""
    first = runtime.load_runtime_params().fragment_merge_ms
    sandbox.write_text(
        runtime.dump_runtime_text(runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=900, auto_accept_low_risk=None, source_sha256="")),
        encoding="utf-8",
    )
    assert runtime.load_runtime_params().fragment_merge_ms == 900
    assert first == 500, "上一发读到的值被留住了 ⇒ 模块级有缓存"


def test_write_then_read_is_the_same_pair_of_values(sandbox: Path):
    digest = runtime.write_runtime_params(
        runtime.RuntimeParams(max_tokens=2048, fragment_merge_ms=300, auto_accept_low_risk=None, source_sha256="")
    )
    reread = runtime.load_runtime_params()
    assert (reread.max_tokens, reread.fragment_merge_ms) == (2048, 300)
    assert reread.source_sha256 == digest
    # `null` 与"没有这个键"是两件事：前者合法（不发那个键），后者按缺键拒。
    runtime.write_runtime_params(runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=300, auto_accept_low_risk=None, source_sha256=""))
    assert runtime.load_runtime_params().max_tokens is None
    assert sandbox.read_text(encoding="utf-8").splitlines()[0].startswith("#")


def test_a_write_that_cannot_be_read_back_is_refused(sandbox: Path, monkeypatch):
    """`write_runtime_params` 末尾那道"回读不等就报错"得有判据盯着。

    前面几条断的都是"读回来等于写进去"，那种写法看不见这道守卫：把守卫拆掉（变异 M-H11）三档照样全绿，
    因为它从没被要求过一次"不等"。这里改为让**读**那一侧说谎 —— 写面必须发现不等并拒，
    否则面板上显示 800、文件里其实 500 这件事就只会在几个月后以"参数怎么自己变了"的样子出现。
    """
    honest = runtime.load_runtime_params

    def lying(path: Path | None = None) -> runtime.RuntimeParams:
        real = honest(path)
        return runtime.RuntimeParams(
            max_tokens=real.max_tokens,
            fragment_merge_ms=real.fragment_merge_ms + 1,
            auto_accept_low_risk=real.auto_accept_low_risk,
            source_sha256=real.source_sha256,
        )

    monkeypatch.setattr(runtime, "load_runtime_params", lying)
    with pytest.raises(runtime.RuntimeParamsError, match="不等"):
        runtime.write_runtime_params(runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=700, auto_accept_low_risk=None, source_sha256=""))
    # 反向半边：不撒谎时同一发必须过得去，否则上一条只是恒真。
    monkeypatch.setattr(runtime, "load_runtime_params", honest)
    runtime.write_runtime_params(runtime.RuntimeParams(max_tokens=None, fragment_merge_ms=700, auto_accept_low_risk=None, source_sha256=""))
    assert honest().fragment_merge_ms == 700


# ---------------------------------------------------------------- 三、消费者与端点


def test_the_consumer_count_excludes_the_definition_site_and_the_name_droppers():
    """"谁读了这个键"必须是数出来的，而且两侧都不许误算：

    - 定义处（`config/runtime.py`）不算消费者，否则任何一个加载器都自我认证；
    - 只在**类字段名**里出现那个键的地方也不算（`api/web.py` 的 `RuntimeWrite.max_tokens` 就是这种），
      否则"面板写了这个字段"会被读成"生产读了这个参数" —— 那正是这条判据要防的假动作。
    """
    assert web._runtime_consumers("fragment_merge_ms") == ["api/web.py"], "SSE 那一条消费者必须被数到"
    # 旧读数（笔 E~G，2026-10-19）是 `[]` —— 那就是"载体里有、没人读"的如实形状。
    # 笔 H 把它变成 spec 构造点。这一格是**故意的绝对读数**：再多一个模块读这个键，
    # 必须先在此处显式改掉（第二个来源不该由一次无关提交顺手带进来）。
    assert web._runtime_consumers("max_tokens") == ["adapters/spec_factory.py"], (
        "长度上限的读取点应当恰好是厂商 spec 的构造点：少一个 = 工厂没读它，多一个 = 出现第二个来源"
    )
    # 2026-10-28 新增的那一格。**恰好一处**是关键：三态（null/true/false）与"面板压过环境变量"
    # 的优先级只在 `effective_switch()` 里实现一次。多一个读它的模块，就等于多一份优先级判断，
    # 而两份判断迟早会在某次改动里各说一套（"面板明明关着，它怎么还在代签"）。
    assert web._runtime_consumers("auto_accept_low_risk") == ["config/auto_approval.py"], (
        "谁可以代签这一格只许有一个读取点（风险引擎的优先级逻辑）"
    )
    # P1-16：门槛表的读取点只许在拆单器里，不许再出现第二份 `512/12` 字面量。
    assert web._runtime_consumers("artifact_floors") == ["orchestrate/llm_split.py"], (
        "产物门槛的读取点必须恰好是拆单器；第二个读取点意味着又长出了一份独立门槛"
    )
    for key in runtime.RUNTIME_KEYS:
        assert "config/runtime.py" not in web._runtime_consumers(key), f"定义处被算成了消费者：{key}"


def test_the_consumer_scanner_sees_a_loader_caller_and_ignores_both_lookalikes(tmp_path: Path, monkeypatch):
    """扫描器的三条规则要在**假树**上分别量：真树只有两个读数（`['api/web.py']` 与
    笔 H 起的 `['adapters/spec_factory.py']`），从这两个数反推不出"定义处跳过"和"只取字段名不算"
    这两条支路今天到底有没有在起作用。

    三个文件各挡一种假动作：
    - `caller.py` 是**该被数到**的那个（调加载器 + 同一个函数里取键）；少了它，后面两条"没数到"全是假绿；
    - `saver.py` 只从请求体里取 `max_tokens`，没调加载器 ⇒ 不许数（"面板有这个字段"冒充"生产读了参数"）；
    - `config/runtime.py` 两条都满足，但它是定义处 ⇒ 不许数（加载器给自己认证）。
    """
    (tmp_path / "caller.py").write_text(
        "from acp.config import runtime\n\n\ndef use():\n"
        "    return runtime.load_runtime_params().fragment_merge_ms\n",
        encoding="utf-8",
    )
    (tmp_path / "saver.py").write_text(
        "def save(body):\n    return body.max_tokens\n",
        encoding="utf-8",
    )
    definition = tmp_path / "config"
    definition.mkdir()
    # 这一支**两条规则都满足**（属性形式的 `load_runtime_params()` 调用 + 同函数取键），
    # 让它没被数到的只有"跳过定义处"那一条 ⇒ 撤掉跳过，这里就是唯一红点。
    (definition / "runtime.py").write_text(
        "from acp.config import runtime\n\n\n"
        "def self_certify():\n    return runtime.load_runtime_params().fragment_merge_ms\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web, "_SRC_ROOT", tmp_path)
    assert web._runtime_consumers("fragment_merge_ms") == ["caller.py"], (
        "该数到的没数到，或那两类不该数到的混进来了"
    )
    assert web._runtime_consumers("max_tokens") == [], "只取请求体字段名的那一支被数成了消费者"


def test_the_view_reports_both_keys_and_never_a_made_up_number():
    client = TestClient(create_app())
    body = client.get("/runtime").json()
    assert sorted(body["params"]) == sorted(runtime.RUNTIME_KEYS)
    assert sorted(body["consumers"]) == sorted(runtime.RUNTIME_KEYS), "消费者栏漏一个键就等于把那条规则藏起来"
    assert body["source_sha256"].startswith("sha256:")


def test_reading_the_view_needs_no_injection_but_writing_needs_a_principal():
    client = TestClient(create_app())
    assert client.get("/runtime").status_code == 200, "参数面不碰库，缺 session 也该能读"
    refused = client.put("/runtime", json={"max_tokens": None, "fragment_merge_ms": 700})
    assert refused.status_code == 403
    assert client.get("/runtime").json()["params"]["fragment_merge_ms"] != 700


def test_the_splitter_really_reads_the_floors_from_the_yaml(sandbox: Path):
    """P1-16 的行为半边：改 `artifact_floors` 后，拆单产出的卡下限必须跟着变。

    只断"消费者模块存在"不够 —— 那可以是读了一次但没用。这里把 html/css 两档改成
    700/17 与 600/15，再从 PRD 投影一份卡，直接看 `CardDraft.artifact_floor`。
    """
    from acp.orchestrate import llm_split

    current = runtime.load_runtime_params()
    floors = dict(current.artifact_floors)
    floors["html"] = (700, 17)
    floors["css"] = (600, 15)
    runtime.write_runtime_params(
        runtime.RuntimeParams(
            max_tokens=current.max_tokens,
            fragment_merge_ms=current.fragment_merge_ms,
            auto_accept_low_risk=current.auto_accept_low_risk,
            artifact_floors=floors,
            source_sha256="",
        )
    )
    drafts = llm_split.skeleton_from_prd(
        {
            "title": "测试站",
            "pages": [{"label": "首页", "duty": "展示产品", "file": "index.html"}],
            "acceptance": ["首屏有标题"],
        }
    )
    got = {draft.artifact_floor.format: (draft.artifact_floor.min_bytes, draft.artifact_floor.min_lines) for draft in drafts}
    assert got["html"] == (700, 17), got
    assert got["css"] == (600, 15), got


def test_an_artifact_floor_outside_the_format_domain_is_refused(sandbox: Path):
    body = (
        "max_tokens: null\nfragment_merge_ms: 500\nauto_accept_low_risk: null\n"
        "artifact_floors:\n  html: [512, 12]\n  css: [512, 12]\n  py: [400, 10]\n"
        "  js: [400, 10]\n  md: [200, 6]\n  php: [100, 3]\n"
    )
    sandbox.write_text(body, encoding="utf-8")
    with pytest.raises(runtime.RuntimeParamsError, match="域外格式"):
        runtime.load_runtime_params()


def test_a_write_lands_and_is_readable_back_through_the_same_endpoint(sandbox: Path, monkeypatch):
    """写完从端点读回来必须就是那个值 —— 只断 `PUT` 返回 200 不算数（面板看的不是返回值）。

    `web.runtime` 与 `runtime` 是同一个模块对象，所以指位一次就够；这条用 `is` 显式钉住那个假设，
    哪天装配方式变了（比如端点改成 from-import 具体函数），这里先红而不是让上一发的隔离失效。
    """
    assert web.runtime is runtime
    client = TestClient(create_app())
    response = client.put("/runtime", json={"max_tokens": 1024, "fragment_merge_ms": 800}, headers=HEADERS)
    assert response.status_code == 200, response.text[:200]
    assert response.json()["params"] == {
        "max_tokens": 1024,
        "fragment_merge_ms": 800,
        # 这一格 PUT 不收、也不许被清掉：它有自己的端点（PATCH /autonomy/auto-approval），
        # 那条要身份头、要留痕。面板调一下流式节流就把"谁能代签"顺手改回默认，是这条要防的事。
        "auto_accept_low_risk": None,
        # P1-16：门槛表也原样带过去；PUT 不收它，改它要编辑 runtime.yaml。
        "artifact_floors": {
            name: list(pair) for name, pair in runtime.DEFAULT_ARTIFACT_FLOORS.items()
        },
    }
    assert client.get("/runtime").json()["params"]["fragment_merge_ms"] == 800
    assert "api/web.py" in response.json()["consumers"]["fragment_merge_ms"]


def test_the_panel_write_preserves_the_auto_accept_switch(sandbox: Path):
    """先在旁边把开关打开，再用面板保存一次别的键 ⇒ 开关必须还在。

    这条断的是"逐键重抄"那个陷阱：`RuntimeWrite` 里没有这一格，写面若自己拼一个 `RuntimeParams`
    就会把没提到的键填成默认，于是 owner 明明开了自动审批，调一次流式节流就悄悄关了。
    """
    runtime.write_runtime_params(
        runtime.RuntimeParams(
            max_tokens=None,
            fragment_merge_ms=500,
            auto_accept_low_risk=True,
            source_sha256="",
        )
    )
    client = TestClient(create_app())
    response = client.put("/runtime", json={"max_tokens": 2048, "fragment_merge_ms": 600}, headers=HEADERS)
    assert response.status_code == 200, response.text[:200]
    reread = runtime.load_runtime_params()
    assert (reread.max_tokens, reread.fragment_merge_ms) == (2048, 600)
    assert reread.auto_accept_low_risk is True, "面板保存不许把自动审批开关顺手关掉"


def test_an_out_of_range_write_is_rejected_before_the_file_moves(sandbox: Path):
    before = sandbox.read_text(encoding="utf-8")
    client = TestClient(create_app())
    response = client.put("/runtime", json={"max_tokens": None, "fragment_merge_ms": 0}, headers=HEADERS)
    assert response.status_code == 422, response.text[:200]
    assert sandbox.read_text(encoding="utf-8") == before, "被拒的那一发不许留下半写的参数文件"


def test_the_panel_surface_is_mounted_on_the_public_app():
    paths = create_app().openapi()["paths"]
    assert "/runtime" in paths, sorted(paths)
    assert "put" in paths["/runtime"], "写面掉了就只剩一个只读数，而 owner 要的是能调"
    assert "request" in inspect.signature(web.write_runtime).parameters, "写面必须能从请求里取身份头"
