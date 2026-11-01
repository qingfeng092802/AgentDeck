"""T027：凭据注入路径与 `secrets` 脱敏过滤器（FR-016｜R-03，宪法「二」）。

两面都要挂住，缺一面就是漏口：
- **产物面** = `bus/gateway.py::write_artifact` 落盘前洗（`orchestrator_writer` 的五个动作
  全部经这一个入口）；
- **日志面** = `install_logging_filter()`（logging 的 `secrets` 过滤器）。

测试里的"凭据"全是**造的形态**（域名用 `example.invalid`，值里带 `notreal`），
不是任何真实值的片段——把真值抄进用例本身就是泄漏，而且撤掉它时最容易再抄一遍。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from acp.bus import acl, gateway
from acp.bus.orchestrator_writer import ORCHESTRATOR
from acp.config.loader import (
    MIN_VALUE_LEN,
    REDACTED,
    SECRET_NAME_RE,
    CredentialMissing,
    SecretsRedactor,
    db_url,
    env_value,
    install_logging_filter,
    redact_bus_text,
)

def fake_dsn(scheme: str = "postgresql", user: str = "acp_user", pw: str = "notreal-pw-9f3a2b",
             host: str = "db.example.invalid:5432/acp") -> str:
    """测试用的**假**连接串在运行时拼装。

    源码里不留任何"凭据形态"的整串，防泄漏门禁（只看形态、不看真值）在全仓库保持 0 命中，
    于是没有必要为测试文件开一条排除项——排除项是漏口，多一条少一条都要人工核实。
    域名用 `example.invalid`（RFC 保留，不可达），值里带 `notreal`，任何一处都不是真凭据片段。
    """
    return f"{scheme}://{user}:{pw}@{host}"


FAKE_DSN = fake_dsn()
FAKE_KEY = "sk-" + "notreal0123456789abcdef"
FAKE_LONG_SECRET = "notreal-secret-value-0123456789"


def test_env_value_returns_the_value_and_never_the_default_file():
    assert env_value("ACP_DB_DSN", {"ACP_DB_DSN": FAKE_DSN}) == FAKE_DSN


@pytest.mark.parametrize("env", [{}, {"ACP_DB_DSN": ""}])
def test_missing_or_empty_credential_raises_by_name_only(env):
    with pytest.raises(CredentialMissing) as excinfo:
        env_value("ACP_DB_DSN", env)
    assert "ACP_DB_DSN" in str(excinfo.value)
    assert excinfo.value.name == "ACP_DB_DSN"
    assert FAKE_DSN not in str(excinfo.value)  # 消息里不可能带出值


@pytest.mark.parametrize(
    ("scheme", "expected_scheme"),
    [
        ("postgresql", "postgresql+psycopg"),
        ("postgresql+psycopg2", "postgresql+psycopg"),
        ("postgresql+psycopg", "postgresql+psycopg"),
    ],
)
def test_db_url_pins_the_psycopg3_driver(scheme, expected_scheme):
    """清单装的是 psycopg 3，而 `postgresql://` 的默认驱动是 psycopg2（本机没装）
    ⇒ 不归一化就会以"缺 psycopg2"的假象失败。迁移与用例共用这一条，两处不许分叉。
    """
    given = fake_dsn(scheme=scheme)
    assert db_url({"ACP_DB_DSN": given}) == fake_dsn(scheme=expected_scheme)


def test_db_url_missing_raises():
    with pytest.raises(CredentialMissing):
        db_url({})


@pytest.mark.parametrize(
    ("sample", "needle"),
    [
        (f"连接失败 {FAKE_DSN}", FAKE_DSN),
        (f"Authorization: Bearer {FAKE_KEY}", FAKE_KEY),
        (f"ARK_API_KEY={FAKE_LONG_SECRET}", FAKE_LONG_SECRET),
        ('  "password": "notreal-pw-abc",\n', "notreal-pw-abc"),
        ("-----BEGIN PRIVATE KEY-----\nMAOBNOTREAL\n-----END PRIVATE KEY-----", "MAOBNOTREAL"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJub3RyZWFsIjoxfQ.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
    ],
    # 显式 ids：默认 id 会把**参数值**写进 pytest 缓存与 `-v` 输出。这里全是造的值无所谓，
    # 但同一条形状将来若换成真凭据，就会把真值打进 `.pytest_cache`（它不在 §7 的扫描面里，
    # 也不该由扫描面兜住）。名字化参数是不花成本的封口。
    ids=["dsn-with-password", "sk-prefixed-key", "assignment-line", "json-password", "pem-block", "jwt-three-part"],
)
def test_credential_forms_are_redacted(sample, needle):
    out = redact_bus_text(sample)
    assert needle not in out
    assert REDACTED in out


def test_assignment_lines_keep_the_key_name():
    """键名不是秘密，抹掉它日志就失去可读性；只洗值。"""
    out = redact_bus_text(f"QODER_PERSONAL_ACCESS_TOKEN=notreal-tok-1234567890")
    assert out.startswith("QODER_PERSONAL_ACCESS_TOKEN=")
    assert "notreal-tok-1234567890" not in out


def test_innocuous_content_is_not_mangled():
    """负对照：不含凭据形态的正常内容 MUST 一字不改。

    没有这条，"全都洗一遍"也能让上面所有用例绿——那是把产物洗花而不是防泄漏。
    """
    plain = (
        "# 首页\n目标: 做一个产品介绍页\n"
        "tokenizer: cl100k_base  # 名字里含 token，但不是凭据\n"
        "见 https://example.com/docs（普通 URL 没有 user:pass@，不该被洗）\n"
    )
    assert redact_bus_text(plain) == plain


def test_env_values_are_scrubbed_by_exact_match():
    """形态正则看不见"换了形状的键"，但值必然还在那个变量里 ⇒ 第二道路径。"""
    redactor = SecretsRedactor({"ARK_API_KEY": FAKE_LONG_SECRET, "PATH": "/usr/bin"})
    assert FAKE_LONG_SECRET in redactor._values
    out = redactor.redact(f"日志里出现了 {FAKE_LONG_SECRET}，同一段里还有无害的 /usr/bin")
    assert FAKE_LONG_SECRET not in out
    assert "/usr/bin" in out  # 非凭据变量名的值不参与整体替换


def test_bare_value_without_a_form_survives_by_design():
    """特征化用例：既不合任何"形态"、又不在环境变量里的裸串，**洗不掉**。

    覆盖面 = 形态正则 ∪ 当前环境的凭据值，两路之外是如实登记的残余。
    想把它也洗掉的唯一办法是"凡像密码的都洗"，那会把产物正常内容一起洗花
    （见 `test_innocuous_content_is_not_mangled`），代价比收益大。
    """
    bare = "notreal-secret-value-0123456789"
    assert SecretsRedactor({}).redact(f"摘要含 {bare}") == f"摘要含 {bare}"


def test_short_env_values_are_left_alone():
    """`MIN_VALUE_LEN` 的代价如实写明：短于阈值的凭据值靠形态正则兜，不靠整串替换。"""
    redactor = SecretsRedactor({"API_KEY": "abc123"})
    assert redactor.tracked_env_values == 0
    assert len("abc123") < MIN_VALUE_LEN


def test_repr_and_hits_do_not_leak_the_value():
    redactor = SecretsRedactor({"ARK_API_KEY": FAKE_LONG_SECRET})
    assert FAKE_LONG_SECRET not in repr(redactor)
    assert "tracked_env_values=1" in repr(redactor)
    assert redactor.hits(f"x {FAKE_LONG_SECRET}") is True
    assert redactor.hits("clean text") is False


def test_secret_name_pattern_covers_the_credential_env_shape():
    """名字形态判定，不写变量名单：名单的唯一口径在 `params-ref §4 / §5`。"""
    for name in ("ARK_API_KEY", "QODER_PERSONAL_ACCESS_TOKEN", "ACP_DB_DSN", "WORKBUDDY_SECRET_KEY"):
        assert SECRET_NAME_RE.search(name), name
    for name in ("ACP_PG_HOST_PORT", "PATH", "HOME", "LANG"):
        assert not SECRET_NAME_RE.search(name), name


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_logging_filter_redacts_and_keeps_the_line():
    logger = logging.getLogger("acp.test.credentials")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)  # 不设就跟着 root 的 WARNING ⇒ INFO 那条根本不会走到 handler
    handler = _Collector()
    logger.addHandler(handler)
    install_logging_filter(logger)
    logger.info("连接串 %s 与键 %s", FAKE_DSN, FAKE_KEY)
    assert handler.lines
    joined = "\n".join(handler.lines)
    assert FAKE_DSN not in joined
    assert FAKE_KEY not in joined
    assert REDACTED in joined  # 留痕不丢：抹掉内容不等于丢掉这条记录
    logger.handlers.clear()


def test_install_logging_filter_is_idempotent():
    logger = logging.getLogger("acp.test.idempotent")
    first = install_logging_filter(logger)
    second = install_logging_filter(logger)
    assert first is second
    assert sum(1 for f in logger.filters if f is first) == 1


def _adapter(agent_id: str = "qoder") -> acl.Actor:
    return acl.Actor(agent_id, "frontend")


def _cap(task_id: str = "T01", agent_id: str = "qoder") -> acl.Capability:
    return acl.Capability(agent_id, task_id)


def test_artifact_written_through_the_gateway_lands_scrubbed(tmp_path: Path):
    """产物面：真落盘字节里 MUST NOT 有凭据形态（宪法「二」+ T027）。"""
    gw = gateway.BusGateway(tmp_path)
    payload = f"<html><body>built with {FAKE_KEY} and {FAKE_DSN}</body></html>"
    written = gw.write_artifact(_adapter(), "qoder-workspace/index.html", payload, _cap())
    on_disk = written.read_text(encoding="utf-8")
    assert FAKE_KEY not in on_disk
    assert FAKE_DSN not in on_disk
    assert REDACTED in on_disk
    assert written.read_text(encoding="utf-8") != payload  # 落盘的确实不是送进来的那份


def test_clean_artifact_is_written_byte_identical(tmp_path: Path):
    gw = gateway.BusGateway(tmp_path)
    payload = "<html><body><h1>hello</h1></body></html>\n"
    written = gw.write_artifact(_adapter(), "qoder-workspace/index.html", payload, _cap())
    assert written.read_bytes() == payload.encode("utf-8")


def test_no_credential_form_survives_anywhere_in_the_bus_tree(tmp_path: Path):
    """总线任何目录都不得留凭据：写三面（workspace / 任务卡 / 日志），再整棵树回读。"""
    gw = gateway.BusGateway(tmp_path)
    gw.write_artifact(_adapter(), "qoder-workspace/index.html", f"a {FAKE_DSN} b", _cap())
    gw.write_artifact(
        ORCHESTRATOR,
        "tasks/T01-home.md",
        gateway.render_card(
            gateway.TaskCardBody(
                objective=f"目标文本含 {FAKE_KEY}",
                inputs="无",
                expected_output="首页",
            )
        ),
        None,
    )
    gw.write_artifact(
        ORCHESTRATOR,
        "daily-log.md",
        f"- 2026-10-12 | task | 换人 | qoder | 摘要含 {FAKE_KEY}\n",
        None,
    )
    leaks = []
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in (FAKE_DSN, FAKE_KEY, FAKE_LONG_SECRET):
            if needle in text:
                leaks.append(f"{path.relative_to(tmp_path)}:{needle[:6]}…")
    assert leaks == []


@pytest.mark.parametrize(
    "sample",
    [
        "pt-" + "notreal0123456789abcdef",
        "ark-" + "notreal0123456789abcdef",
        "WORKBUDDY_9f8e7d6c5b4a39281706",
    ],
    ids=["qoder-pat", "ark-key", "workbuddy-prefix"],
)
def test_p1_credential_prefixes_are_redacted(sample):
    out = redact_bus_text(f"日志里出现了 {sample}，后面还有正常文字")
    assert sample not in out
    assert "[REDACTED]" in out
