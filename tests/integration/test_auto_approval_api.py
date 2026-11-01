"""`GET/PATCH /autonomy/auto-approval` 的 HTTP 面判据（2026-10-28，分级授权的开关）。

这一格是**面板上那颗胶囊按钮的唯一后端**。它要回答的问题不是"按钮亮没亮"，而是三件事：

1. **"开"是谁开的** —— 响应里的 `source` 必须区分"你在面板上按的"/"部署时的环境变量"/"没设过"。
   没有这一格的界面会撒谎：部署期设了变量、面板上没人按过，按钮亮着而人以为自己没开过。
2. **写一个能改变"谁签字"的开关需要身份**，读不需要（与 `GET/PUT /runtime` 同一条口径）。
3. **写进去的值就是读回来的值** —— 一律回 `config/runtime.yaml` 那一份读，不看响应体自述。
   判据因此全程把 `runtime.RUNTIME_PATH` 指到临时目录：**绝不碰仓库里那份真配置**。

**本文件只有"留痕那一节"需要 PG**（EN-14 要真库才读得回），其余各节在无库时也照跑 ——
所以不用整档 skip，而是给那一条单独挂 `needs_pg`。
"""

from __future__ import annotations

import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.api import autonomy as api
from acp.api import web as web_api
from acp.config import auto_approval
from acp.config import runtime
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import events

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 这一条要真 PG（读回 EN-14）"
)

PRINCIPAL_HEADER = web_api.PRINCIPAL_HEADER
HUMAN = "owner-1"
HEADERS = {PRINCIPAL_HEADER: HUMAN}


@pytest.fixture
def sandbox(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把参数文件指到临时目录，并保证环境变量干净（每条用例自己决定要不要设）。"""
    target = tmp_path / "runtime.yaml"
    monkeypatch.setattr(runtime, "RUNTIME_PATH", target)
    monkeypatch.setattr(auto_approval, "AUTO_ACCEPT_PATH", auto_approval.REPO_ROOT / "config" / "auto_accept.yaml")
    monkeypatch.delenv(auto_approval.AUTO_ACCEPT_ENV, raising=False)
    target.write_text(
        runtime.dump_runtime_text(
            runtime.RuntimeParams(
                max_tokens=None,
                fragment_merge_ms=500,
                auto_accept_low_risk=None,
                source_sha256="",
            )
        ),
        encoding="utf-8",
    )
    return target


def client_for(*, session_factory=None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if session_factory is not None:
        app.state.session_factory = session_factory
    return TestClient(app)


# ---------------------------------------------------------------- 一、读

def test_reading_the_switch_needs_neither_a_principal_nor_a_database(sandbox: pathlib.Path):
    """读配置不该先证明自己是谁；也不该因为没库就打不开面板。"""
    body = client_for().get("/autonomy/auto-approval").json()
    assert body["enabled"] is False
    assert body["source"] == auto_approval.SOURCE_DEFAULT, "没设过 ⇒ 必须是 default，不许含糊成 env"
    assert body["low_risk_formats"] == ["css", "md"], "边界要让界面显示得出来，且顺序稳定"
    assert body["env_flag"] == auto_approval.AUTO_ACCEPT_ENV
    assert body["reviewer_ready"] is False, "默认没开 deepseek 审查 ⇒ 打开开关不会有任何效果，界面得知道"


def test_the_view_reports_the_environment_as_the_source_when_the_file_is_silent(
    sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """部署期设了变量、面板上没人按过 ⇒ `source=env`（这是"按钮亮着但不是我开的"那一刻）。"""
    monkeypatch.setenv(auto_approval.AUTO_ACCEPT_ENV, "1")
    body = client_for().get("/autonomy/auto-approval").json()
    assert (body["enabled"], body["source"]) == (True, auto_approval.SOURCE_ENV)


def test_reviewer_ready_follows_the_same_variable_the_server_reads(
    sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """`reviewer_ready` 读的是 `ACP_REVIEW_MODE`，不是第二份配置。

    这一格之所以要断：开关打开而审查员没开时，机器**一个都不会签**（没有 PASS 就没有代签）。
    界面若把这两件事混成一个开关，owner 会以为"我开了，它怎么不动"。
    """
    from acp.review import policy as review_policy

    monkeypatch.setenv(review_policy.REVIEW_MODE_ENV, review_policy.REVIEW_MODE_DEEPSEEK)
    assert client_for().get("/autonomy/auto-approval").json()["reviewer_ready"] is True
    monkeypatch.setenv(review_policy.REVIEW_MODE_ENV, review_policy.REVIEW_MODE_MANUAL)
    assert client_for().get("/autonomy/auto-approval").json()["reviewer_ready"] is False


# ---------------------------------------------------------------- 二、写

def test_writing_the_switch_needs_a_principal(sandbox: pathlib.Path):
    """这是一个"改变谁签字"的动作，与批单同级 —— 缺身份头 403，且文件不许动。"""
    before = sandbox.read_text(encoding="utf-8")
    response = client_for().patch("/autonomy/auto-approval", json={"enabled": True})
    assert response.status_code == 403, response.text[:200]
    assert sandbox.read_text(encoding="utf-8") == before


def test_a_write_lands_and_the_panel_value_wins_over_the_environment(
    sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """写进去 → 从**加载器**读回来就是那个值；面板定过的值压过环境变量（三态里的中间那一态）。"""
    monkeypatch.setenv(auto_approval.AUTO_ACCEPT_ENV, "1")
    client = client_for()

    opened = client.patch("/autonomy/auto-approval", json={"enabled": False}, headers=HEADERS)
    assert opened.status_code == 200, opened.text[:300]
    assert opened.json()["source"] == auto_approval.SOURCE_RUNTIME
    assert runtime.load_runtime_params().auto_accept_low_risk is False, "写进文件的那一格必须是真布尔"
    assert auto_approval.effective_switch() == (False, auto_approval.SOURCE_RUNTIME), (
        "面板明确关掉 ⇒ 环境变量再开着也不生效（这就是'面板压过 env'那一半）"
    )

    # `null` = 恢复成"跟随部署期变量"，于是又回到 env 那一档（不是回到"关"）。
    client.patch("/autonomy/auto-approval", json={"enabled": None}, headers=HEADERS)
    assert runtime.load_runtime_params().auto_accept_low_risk is None
    assert auto_approval.effective_switch() == (True, auto_approval.SOURCE_ENV)


def test_a_string_is_not_a_boolean_here_either(sandbox: pathlib.Path):
    """`{"enabled": "true"}` ⇒ 422。宽松模式会把它收成 `True`，而那等于"拼错一个字符串就开了代签"。"""
    response = client_for().patch(
        "/autonomy/auto-approval", json={"enabled": "true"}, headers=HEADERS
    )
    assert response.status_code == 422, response.text[:300]
    assert runtime.load_runtime_params().auto_accept_low_risk is None, "被拒的那一发不许留下任何写入"


def test_an_unknown_field_in_the_body_is_rejected(sandbox: pathlib.Path):
    response = client_for().patch(
        "/autonomy/auto-approval", json={"enabled": True, "bypass_triple": True}, headers=HEADERS
    )
    assert response.status_code == 422, response.text[:300]


def test_the_write_does_not_disturb_the_other_runtime_keys(sandbox: pathlib.Path):
    """写这一格不许把 `max_tokens` / `fragment_merge_ms` 冲掉（`dataclass_replace` 那一条的理由）。"""
    runtime.write_runtime_params(
        runtime.RuntimeParams(
            max_tokens=1024,
            fragment_merge_ms=800,
            auto_accept_low_risk=None,
            source_sha256="",
        )
    )
    client_for().patch("/autonomy/auto-approval", json={"enabled": True}, headers=HEADERS)
    after = runtime.load_runtime_params()
    assert (after.max_tokens, after.fragment_merge_ms) == (1024, 800)
    assert after.auto_accept_low_risk is True


# ---------------------------------------------------------------- 三、留痕

@needs_pg
def test_the_write_leaves_an_event_row(sandbox: pathlib.Path):
    """谁在什么时候把"谁能代签"改了 —— 审计里必须有这一行，且主体是那个真人。

    只写文件是不够的：文件不带"谁改的"。只写事件也不够：真正生效的是文件。
    所以判据两条一起断（事件在库里、值在文件里）。
    """
    engine = create_engine(db_url(), future=True)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            session = Session(bind=conn)
            try:
                client = client_for(session_factory=lambda: Session(bind=conn))
                response = client.patch(
                    "/autonomy/auto-approval", json={"enabled": True}, headers=HEADERS
                )
                assert response.status_code == 200, response.text[:300]

                rows = list(
                    session.scalars(
                        select(events.EventLog)
                        .where(events.EventLog.entity == "autonomy_state")
                        .order_by(events.EventLog.id.desc())
                    ).all()
                )
                assert rows, "改了'谁可以代签'却没留痕"
                latest = rows[0]
                assert latest.actor_agent_id == HUMAN, "留痕要记到那个真人头上，不是记给机器"
                assert latest.payload["action"] == "auto_approval_switch_written"
                assert latest.payload["enabled"] is True
                assert latest.payload["source"] == auto_approval.SOURCE_RUNTIME
            finally:
                session.close()
                transaction.rollback()
    finally:
        engine.dispose()


# ---------------------------------------------------------------- 四、挂在公开 app 上

def test_the_switch_is_mounted_on_the_public_app():
    """装配点真的会把这条路由挂上去 —— 否则前端那颗胶囊永远读不到东西。"""
    from acp.app import create_app

    paths = create_app().openapi()["paths"]
    assert "/autonomy/auto-approval" in paths, sorted(paths)
    assert {"get", "patch"} <= set(paths["/autonomy/auto-approval"]), paths["/autonomy/auto-approval"]
