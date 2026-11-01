"""R-1：`GET /requests/{id}/delivery` 的读面形状（EN-17）。

实时看板「交付」页此前只读推演档的 `studio.deliver`，已交付单仍显示“验收通过后会出现”；
这个端点把 EN-17 的索引交出来。本文件不碰 PG：用假会话钉响应映射与 404 语义，
真库那一侧由 `tests/integration/test_tasks_read_api.py` 的同族用例覆盖。
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi.testclient import TestClient

from acp.app import create_app
from acp.persistence.models_registry import Deliverable


REQUEST_ID = uuid.uuid4()


class _Query:
    def __init__(self, row: Deliverable | None) -> None:
        self._row = row

    def filter(self, *args, **kwargs) -> "_Query":
        return self

    def one_or_none(self) -> Deliverable | None:
        return self._row


class _Session:
    def __init__(self, delivery: Deliverable | None) -> None:
        self._delivery = delivery
        self.closed = False

    def get(self, model, ident):
        return object()

    def query(self, model):
        return _Query(self._delivery)

    def close(self) -> None:
        self.closed = True


def _delivery() -> Deliverable:
    return Deliverable(
        id=uuid.uuid4(),
        feature_request_id=REQUEST_ID,
        manifest=[
            {
                "rel_path": "qoder-workspace/index.html",
                "producer": "qoder",
                "task_card_id": "T02",
                "bytes": 2048,
                "lines": 44,
                "format": "html",
            }
        ],
        entry_path="qoder-workspace/index.html",
        open_issues=[],
        assembled_by_agent_id="orchestrator",
        created_at=dt.datetime(2026, 10, 29, 10, 0, tzinfo=dt.timezone.utc),
    )


def _client(delivery: Deliverable | None) -> TestClient:
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(delivery)
        sessions.append(session)
        return session

    app = create_app(session_factory=factory)
    app.state._test_sessions = sessions  # type: ignore[attr-defined]
    return TestClient(app)


def test_delivery_index_is_returned_with_manifest_and_entry():
    client = _client(_delivery())
    response = client.get(f"/requests/{REQUEST_ID}/delivery")

    assert response.status_code == 200
    body = response.json()
    assert body["entry_path"] == "qoder-workspace/index.html"
    assert body["manifest"] == [
        {
            "rel_path": "qoder-workspace/index.html",
            "producer": "qoder",
            "task_card_id": "T02",
            "bytes": 2048,
            "lines": 44,
            "format": "html",
        }
    ]
    assert body["open_issues"] == []
    assert body["assembled_by_agent_id"] == "orchestrator"


def test_missing_delivery_is_a_404_not_an_empty_package():
    client = _client(None)
    response = client.get(f"/requests/{REQUEST_ID}/delivery")

    assert response.status_code == 404
    assert response.json()["detail"].startswith("delivery_not_assembled")
