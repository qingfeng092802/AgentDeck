"""`artifact_format` 的第六个值 `js`：域、PG 枚举、路由表、下限表、扫描面五处必须一致。

owner 2026-10-29 的裁决（第十一轮）。背景是一处真事故：2026-10-28 真跑里 T02 交了 `cart.js`，
`bus/gateway.py::format_of()` 判 `unsupported_format`，**那次写入整件**被拒 ⇒ 一张卡直接跑砸。

当时的处置是把它记成"执行器乱写后缀"，那是把"域缺一个值"读成了模型的错。一份能被双击打开的
静态站，样式（css）、脚本（js）、页面（html）是三件平级的东西；少了中间那件，
"给表单加前端校验""加个购物车"这类需求在平台上**根本无处安放**。

这个文件盯的不是"加了一个字符串"，而是**加了之后五处有没有一起跟上**：

1. `refs.py::ArtifactFormat`（域本身）；
2. `persistence/enums.py` 的 `artifact_format`（PG 枚举，从 Literal 派生）；
3. `prompts.ROLE_BY_FORMAT`（卡上没有岗位列，格式是唯一能区分岗位的事实）；
4. 两张 `_FLOORS`（`llm_split` 拆单 / `reporting.cards` 系统卡）—— 少一处就是 KeyError 或静默用默认值；
5. `checks.service.scan_security` 的后缀白名单（**最容易漏的一处**：放进来一个可执行格式却不扫它，
   等于凭空多一类没人看的代码）。

真库那一侧（`ALTER TYPE … ADD VALUE 'js'` 有没有真落到 `pg_type`/`pg_enum` 上）由
`tests/integration/test_schema_invariants.py::test_artifact_format_type_carries_the_live_domain` 盯，
需要 DSN，不在本文件里。
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from acp.bus.gateway import ARTIFACT_FORMATS, format_of
from acp.checks.service import scan_security
from acp.config.auto_approval import low_risk_formats
from acp.orchestrate import llm_split
from acp.persistence.enums import pg_enum, values_of
from acp.prompts import ROLE_BY_FORMAT
from acp.protocol.messages import ArtifactFloor
from acp.protocol.refs import ArtifactFormat
from acp.reporting import cards as reporting_cards


def test_js_is_the_sixth_value_and_sits_last() -> None:
    """**顺序是判据的一部分**，不是排版偏好。

    `ALTER TYPE … ADD VALUE` 只能追加到末尾（`BEFORE`/`AFTER` 要 PG 12+ 且要重建排序），
    迁移 `0010` 正是这么加的。把 `js` 插在 `html` 后面，`pg_enum(...).enums` 与
    `values_of(...)` 就会错位，而 `test_the_two_changed_pg_enums_carry_the_live_domain_in_order`
    那类顺序断言会红在"顺序"上、而不是"少了 js"上 —— 那种红最难读。
    """
    assert values_of("artifact_format") == ("html", "css", "md", "json", "py", "js")
    assert get_args(ArtifactFormat)[-1] == "js"


def test_the_pg_enum_object_carries_js_in_the_same_order() -> None:
    """模型侧（`pg_enum()`）与域侧（`values_of()`）是同一个域的两种投影，逐位相等。"""
    assert tuple(pg_enum("artifact_format").enums) == values_of("artifact_format")
    assert pg_enum("artifact_format").name == "artifact_format_type"


def test_the_bus_accepts_a_js_artifact() -> None:
    """总线是这次事故的现场。`cart.js` 现在 MUST 被认成 `js`，而不是抛 `BusError`。"""
    assert "js" in ARTIFACT_FORMATS
    assert format_of("cart.js") == "js"
    # 大小写不敏感（`format_of` 先 `lower()`）：`Cart.JS` 与 `cart.js` 是同一个格式。
    assert format_of("Cart.JS") == "js"
    # 子目录里的脚本也一样 —— 总线看的是后缀，不是路径深度。
    assert format_of("static/js/checkout.js") == "js"


def test_a_domain_outside_the_six_is_still_refused() -> None:
    """放宽到六值**不等于**放开自由字符串：`php` 仍然当场拒（2026-10-28 的另一处真事故）。"""
    from acp.bus.gateway import BusError

    with pytest.raises(BusError) as caught:
        format_of("submit-contact.php")
    assert caught.value.code == "unsupported_format"


def test_pydantic_accepts_js_in_the_floor() -> None:
    """这一条就是 owner 说的"确保 Pydantic 校验不再报错"。

    `ArtifactFloor.format` 的类型是 `ArtifactFormat`，`extra="forbid"` 只拒多余键；
    域里没有 `js` 时，模型回一句 `format="js"` 会在**构造期**失败，而失败点在
    `llm_split` 的拆单回话解析里 —— 表现为"整单被拒"，不是"这一张卡格式不对"。
    """
    floor = ArtifactFloor(min_bytes=400, min_lines=10, format="js")
    assert floor.format == "js"
    # 域外的值仍然构造失败（放宽 ≠ 不校验）。
    with pytest.raises(ValidationError):
        ArtifactFloor(min_bytes=400, min_lines=10, format="php")


def test_the_role_table_routes_js_to_the_frontend_seat() -> None:
    """卡上没有岗位列，`ROLE_BY_FORMAT` 是唯一能区分"这卡归谁写"的事实。

    归 `frontend` 而不是 `backend`：写 `.js` 的人就是写那张页面卡的人，归后端会让
    "页面里加个购物车"变成跨岗协作，而实际执行时只有一个座位在动。
    """
    assert ROLE_BY_FORMAT["js"] == "frontend"
    # 六个值一个不许缺 —— 缺一个就是"这类卡拿不到提示词"，而表现是执行器凭空变笨。
    assert set(ROLE_BY_FORMAT) == set(values_of("artifact_format"))


def test_both_floor_tables_know_js() -> None:
    """拆单侧读运行期配置，系统卡侧读同一份出厂表；`js` 两边都不许缺。

    旧口径是"两张 `_FLOORS` 各写一份"；P1-16 之后拆单侧的门槛来自
    `config/runtime.yaml`，系统卡侧引用 `DEFAULT_ARTIFACT_FLOORS`，字面量只剩一处。
    """
    from acp.config import runtime

    loaded = runtime.load_runtime_params()
    assert loaded.artifact_floors["js"] == (400, 10)
    assert reporting_cards._FLOORS["js"] == (400, 10)
    assert reporting_cards._FLOORS == runtime.DEFAULT_ARTIFACT_FLOORS
    # 六个格式一个不许缺；缺一个就是拆单时 `table[fmt]` 直接 KeyError。
    assert set(loaded.artifact_floors) == set(get_args(ArtifactFormat))
    assert set(reporting_cards._FLOORS) == set(get_args(ArtifactFormat))


def test_the_security_scanner_actually_reads_js() -> None:
    """放进来一个可执行格式却不扫它 = 凭空多一类没人看的代码。

    `.js` 比 `.py` 更该扫：它会被浏览器在客户的机器上直接执行，而交付包里
    "看起来是个静态站"这件事，恰恰是最容易让人放松警惕的地方。
    """
    report = scan_security({"cart.js": 'const f = new Function("return 1");\n'})
    assert any(finding.code == "sec_eval" for finding in report.findings), report.findings

    # 对照组：同样的内容放在 `.md` 里不扫（口径没变，不是"什么文件都扫"）。
    quiet = scan_security({"notes.md": 'new Function("return 1")\n'})
    assert not any(finding.code == "sec_eval" for finding in quiet.findings)


def test_js_is_never_auto_accepted() -> None:
    """`js` 会执行，口径与 `py` 同档 ⇒ MUST NOT 进低风险自动验收清单。

    这条钉的是"以后有人顺手把 js 加进去"：那份清单管的是"哪些卡不用人签字"，
    加错一个值不会报错，只会让一类可执行产物悄悄绕过人工验收。
    """
    listed = low_risk_formats()
    assert "js" not in listed
    assert listed == frozenset({"md", "css"})


def test_the_split_prompt_names_every_format_the_bus_accepts() -> None:
    """拆单提示词必须点名六种后缀，否则模型会按旧口径写判据。

    这条与 `test_llm_split.py::test_the_split_prompt_forbids_artifacts_the_bus_would_refuse`
    是同一件事的两端：那条盯"平台没有的东西被拒"，这条盯"平台有的东西被写上"。
    只做前者，模型会以为脚本也不能交，于是把交互塞进内联 `<script>` —— 而那是 warn。
    """
    for suffix in ARTIFACT_FORMATS:
        assert suffix in llm_split.SYSTEM, f"拆单提示词没有点名后缀 {suffix}"
