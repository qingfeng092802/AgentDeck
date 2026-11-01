"""分级授权（2026-10-28）：低风险自动验收的四条闸、开关优先级、以及"它只能缩小不能扩大"。

这一份判据回答的是 owner 那句"审批卡点太密集，我成了盖章机器"之后最要紧的一个问题：
**把章交给机器之后，机器能签的范围到底有多大**。答案必须是可数的、可复算的，而不是一句"低风险"。

四组：

1. **开关值**：`ACP_AUTO_ACCEPT_LOW_RISK` 只认六个词，别的写法报错（不猜）；
2. **风险清单**：`config/auto_accept.yaml` 读不出就报错（不回落空集）；只认平台侧事实（后缀）；
3. **四条闸**：开关 → 风险 → 互斥 → 审查，各有一个稳定代号；
4. **边界**：这条开关**不碰 `ApprovalGate` 的免确认**（FR-048 只允许 A 类），也不许在审查员
   没给 PASS 时签任何字。
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from acp.bus.gateway import ARTIFACT_FORMATS
from acp.config import auto_approval as aa
from acp.config import runtime
from acp.review import runner as rr

SRC = pathlib.Path(aa.__file__).resolve()
REPO = SRC.parents[2]


class FakeCard:
    """判定只读 `artifact_floor_format` 一列 —— 假件只给那一列，多给一列就等于给判定开了后门。"""

    def __init__(self, fmt: str) -> None:
        self.artifact_floor_format = fmt


class RunnerCard(FakeCard):
    """`ReviewRunner` 那一层的假件：它还要把三岗搬进 `triple.Assignment`，所以得多三列。

    和 `FakeCard` 分开写，是为了让"`decide` 只读风险那一列"这条保持得住 —— 拿一个带三岗的假件
    去测 `decide`，就等于默许它以后偷看 executor。
    """

    def __init__(self, fmt: str = "md", *, cid: str = "T01") -> None:
        super().__init__(fmt)
        self.id = cid
        self.executor_agent_id = "claude-code"
        self.executor_role_id = "role-builder"
        self.reviewer_agent_id = rr.DEEPSEEK_REVIEWER
        self.acceptor_agent_id = "codex"


def params(*, auto: bool | None) -> runtime.RuntimeParams:
    return runtime.RuntimeParams(
        max_tokens=None, fragment_merge_ms=500, auto_accept_low_risk=auto, source_sha256="sha256:x"
    )


# ---------------------------------------------------------------- 1 开关值

@pytest.mark.parametrize("raw", ["", "  ", "0", "false", "no", "off", "FALSE", " Off "])
def test_a_falsy_env_value_is_off(raw: str) -> None:
    assert aa.env_default({aa.AUTO_ACCEPT_ENV: raw}) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " Yes "])
def test_a_truthy_env_value_is_on(raw: str) -> None:
    assert aa.env_default({aa.AUTO_ACCEPT_ENV: raw}) is True


@pytest.mark.parametrize("raw", ["maybe", "y", "t", "2", "trueish", "开"])
def test_an_unknown_env_value_refuses_instead_of_guessing(raw: str) -> None:
    """猜错方向就是把机器代签**悄悄打开**。这条必须报错，不许默认成 false 也不许默认成 true。"""
    with pytest.raises(aa.AutoApprovalConfigError, match="不是布尔写法"):
        aa.env_default({aa.AUTO_ACCEPT_ENV: raw})


# ---------------------------------------------------------------- 2 风险清单

def test_the_low_risk_list_is_a_file_and_a_missing_file_is_an_error(tmp_path: pathlib.Path) -> None:
    """读不到清单 ⇒ 报错。回落成空集会让"这条路整条失灵"看起来像"今天恰好都要人签"。"""
    with pytest.raises(aa.AutoApprovalConfigError, match="不存在"):
        aa.low_risk_formats(tmp_path / "nope.yaml")


def test_the_low_risk_list_refuses_an_unknown_key(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "auto_accept.yaml"
    target.write_text("low_risk_formats: [md]\nallow_everything: true\n", encoding="utf-8")
    with pytest.raises(aa.AutoApprovalConfigError, match="表外键"):
        aa.low_risk_formats(target)


def test_the_low_risk_list_refuses_an_empty_or_malformed_table(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "auto_accept.yaml"
    for body in ("low_risk_formats: []\n", "low_risk_formats: md\n", "version: 1\n"):
        target.write_text(body, encoding="utf-8")
        with pytest.raises(aa.AutoApprovalConfigError):
            aa.low_risk_formats(target)


def test_every_low_risk_format_is_one_the_bus_would_accept() -> None:
    """清单里写一个总线不认的后缀 ⇒ 那条规则永远不会命中，而界面上它看起来是开着的。

    这条把两份域钉在一起：`config/auto_accept.yaml` ⊆ `bus.gateway.ARTIFACT_FORMATS`。
    """
    listed = aa.low_risk_formats()
    assert listed <= set(ARTIFACT_FORMATS), f"清单里有总线不认的后缀：{sorted(listed - set(ARTIFACT_FORMATS))}"


def test_the_shipped_table_holds_exactly_the_two_harmless_formats() -> None:
    """**故意的绝对读数**：放宽到 `html` 必须先回来改这一行，而改它的人会看见下面这段理由。

    `md` 是给人读的说明，`css` 只影响观感 —— 两者的共同点是"改错了没有副作用，回滚是一行 diff"。
    `html` 不在此列：它可以有表单、外链、内嵌脚本，"静态页"不等于"没有副作用"。
    """
    assert aa.low_risk_formats() == frozenset({"md", "css"})


# ---------------------------------------------------------------- 3 分类

@pytest.mark.parametrize("fmt", ["md", "css"])
def test_a_low_risk_format_is_classified_low(fmt: str) -> None:
    assert aa.risk_class_of(FakeCard(fmt)) == aa.LOW_RISK


@pytest.mark.parametrize("fmt", ["html", "py", "json", "", "unknown"])
def test_everything_else_needs_a_human(fmt: str) -> None:
    assert aa.risk_class_of(FakeCard(fmt)) == aa.NEEDS_HUMAN


def test_the_classification_reads_the_enum_shape_too() -> None:
    """库里那一列是枚举，取出来的字符串可能带引号形态（`ArtifactFormat.md`）；判定要认它。"""
    assert aa.risk_class_of(FakeCard("ArtifactFormat.md")) == aa.LOW_RISK


# ---------------------------------------------------------------- 4 四条闸

def test_the_switch_off_blocks_before_anything_else() -> None:
    """开关关着时，**连"这是低风险"都不必判** —— 顺序是刻意的：意图先于事实。"""
    decision = aa.decide(FakeCard("md"), reviewer_pass=True, params=params(auto=None), env={})
    assert (decision.allowed, decision.code, decision.source) == (False, aa.SWITCH_OFF, aa.SOURCE_DEFAULT)


def test_the_runtime_value_wins_over_the_environment() -> None:
    """面板按下去之后，部署期的环境变量不再说话 —— 否则按钮按了没反应，而屏上还亮着"已开启"。"""
    decision = aa.decide(
        FakeCard("md"),
        reviewer_pass=True,
        params=params(auto=True),
        env={aa.AUTO_ACCEPT_ENV: "false"},
    )
    assert decision.allowed is True
    assert decision.source == aa.SOURCE_RUNTIME


def test_the_environment_value_applies_when_the_runtime_key_is_null() -> None:
    decision = aa.decide(
        FakeCard("md"),
        reviewer_pass=True,
        params=params(auto=None),
        env={aa.AUTO_ACCEPT_ENV: "true"},
    )
    assert decision.allowed is True
    assert decision.source == aa.SOURCE_ENV


def test_a_non_low_risk_card_is_blocked_even_with_the_switch_on() -> None:
    decision = aa.decide(
        FakeCard("html"),
        reviewer_pass=True,
        params=params(auto=True),
        env={},
    )
    assert (decision.allowed, decision.code, decision.risk_class) == (False, aa.NOT_LOW_RISK, aa.NEEDS_HUMAN)


def test_a_broken_triple_is_blocked_before_the_reviewer_is_consulted() -> None:
    """互斥是**资格**问题（这张卡有没有资格被机器签），审查是**这一次**签不签得成。

    所以互斥不成立时，`code` 必须是 `triple_broken` 而不是 `reviewer_not_pass` ——
    两者混起来读，人就没法判断"是这张卡本来就不该机器签，还是这次碰巧没过"。
    """
    decision = aa.decide(
        FakeCard("md"),
        reviewer_pass=False,
        triple_reasons=("'deepseek-reviewer' 同时占了 reviewer/acceptor 两个岗",),
        params=params(auto=True),
        env={},
    )
    assert decision.code == aa.TRIPLE_BROKEN
    assert "自己审自己" in decision.detail


def test_no_pass_means_no_signature() -> None:
    decision = aa.decide(FakeCard("md"), reviewer_pass=False, params=params(auto=True), env={})
    assert (decision.allowed, decision.code) == (False, aa.REVIEWER_NOT_PASS)


def test_all_four_hold_and_only_then_it_is_allowed() -> None:
    decision = aa.decide(FakeCard("css"), reviewer_pass=True, params=params(auto=True), env={})
    assert decision.allowed is True
    assert decision.code == aa.OK
    payload = decision.payload()
    assert set(payload) == {"allowed", "code", "risk_class", "switch_source", "detail"}


# ---------------------------------------------------------------- 5 边界：它只能缩小，不能扩大

def test_the_module_never_touches_the_approval_exemption_path() -> None:
    """FR-048 只允许 A 类免确认。这份模块若去碰 `repository.exempt` / `create_approval`，
    就是给"免确认"开了第二条路 —— 而那条路的门槛（规则表 + 平台事实）比这里严得多。

    读 AST 而不是 grep：注释里提到这两个名字是允许的（本文档就在提），代码里调它们不行。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "exempt" not in called, "低风险自动验收不许走 FR-048 的免确认通道"
    assert "create_approval" not in called, "它不建任何审批单 —— 这正是'不生成 ApprovalTicket'的落点"
    assert "resolve" not in called, "它也不替人做审批决定"


def test_the_runner_asks_this_module_before_it_signs() -> None:
    """`_accept` 的第一件事必须是问这一格。顺序反了（先验收再问）就没有"闸"这回事了。"""
    source = textwrap.dedent(
        __import__("inspect").getsource(rr.ReviewRunner._accept)
    )
    head = source.split("findings = self._findings_for")[0]
    assert "_auto_accept_decision" in head, "风险闸必须排在写 EN-08 之前"
    assert "_advisory" in head, "被拦下时要走'只建议'那一支"


def test_the_advisory_path_writes_no_conclusion_row() -> None:
    """被拦下的那一支只许留 EN-14 一条痕：**不写 EN-08、不写 EN-18、不建单**。

    这条守的是"建议 ≠ 结论"。一旦它顺手写了 `review_report` 或 `review_verdict`，
    人工打开这张卡时会看到"已经有人审过了"的结论行 —— 而那个人其实是机器，且它没签字权。
    半截结论比没有结论更难解释：下一轮 `review_round` 会从那条行继续算。
    """
    body = ast.parse(textwrap.dedent(__import__("inspect").getsource(rr.ReviewRunner._advisory)))
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("_record_report", "record", "record_verdict", "submit_review", "create_approval"):
        assert forbidden not in called, f"建议这条路不许调 {forbidden}"
    assert "_event" in called, "但必须留痕 —— 否则屏上分不清'没审'与'审了没签'"


def test_a_config_error_blocks_instead_of_passing(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """配置坏掉时不许代签，也不许把卡弄丢：判定退化成 `config_error` + 需要人。

    现场造法：开关打开、但风险清单文件不见了。这正是"有人在部署时改了目录"的形状 ——
    此时若把异常当成"没开"吞掉，运维会以为今天恰好都要人签，而真相是清单丢了。
    """
    monkeypatch.setattr(aa, "AUTO_ACCEPT_PATH", tmp_path / "nope.yaml")
    monkeypatch.setenv(aa.AUTO_ACCEPT_ENV, "1")
    runner = rr.ReviewRunner(session_factory=lambda: object(), client=lambda _: None, bus_root=REPO)

    broken = runner._auto_accept_decision(RunnerCard("md"))
    assert broken.allowed is False, "配置读不出时必须朝'不许代签'倒"
    assert broken.code == "config_error", "代号要能把它和'开关没开'区分开"
    assert broken.risk_class == aa.NEEDS_HUMAN
    assert "AutoApprovalConfigError" in broken.detail, "详情要带上异常类名，否则日志里看不出坏在哪"


def test_the_shipped_switch_is_off_out_of_the_box() -> None:
    """仓库里那一份 `config/runtime.yaml` 出厂必须是 `null`（= 跟随环境变量，而变量默认关）。

    为什么要一条专门的判据：**这台机器上它已经被写成过 `true`**（2026-10-28 调试面板时
    真发了一次 PATCH，落到了仓库那份文件里）。那一下不会报任何错，界面也照常 ——
    它只是让下一次部署一装好就"机器在替人签字"，而部署日志里没有一句话会提这件事。
    代签是"谁签字"的改变，它必须由人主动打开，不能作为调试的残留被带上线。
    """
    params = runtime.load_runtime_params()
    assert params.auto_accept_low_risk is None, (
        f"仓库那份 runtime.yaml 里 auto_accept_low_risk={params.auto_accept_low_risk!r}；"
        "出厂必须是 null（跟随环境变量），真打开要由人在面板上按"
    )
    assert aa.effective_switch(env={}) == (False, aa.SOURCE_DEFAULT), "没有任何人表态时必须是关的"
