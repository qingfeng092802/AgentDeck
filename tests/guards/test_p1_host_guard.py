"""P1-12：本机看板只接受环回 Host，挡 DNS rebinding。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from acp import serve


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "1"}

    serve.install_host_guard(app)
    return app


def test_a_foreign_host_header_is_refused() -> None:
    client = TestClient(_app(), base_url="http://evil.example")
    assert client.get("/ping").status_code == 400


def test_loopback_hosts_are_allowed() -> None:
    for base in ("http://127.0.0.1", "http://localhost"):
        client = TestClient(_app(), base_url=base)
        assert client.get("/ping").status_code == 200, base
