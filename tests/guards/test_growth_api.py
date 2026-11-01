"""MVP-2 第三步 ①：档案读写端点的判据（owner 2026-10-19 指令的"专属面板能直接编辑"那一格）。

**这一文件不连库**：档案面只碰那两份 `.md`，一次 SQL 都没有。它用 `ACP_GROWTH_ROOT` 把根指到
临时目录（口径同 §11 那条行为式绑定），所以**不读使用者的真档案**，也不往仓库里写。

四条刻意的写法：

1. **写必带身份头**：缺头发 403，而这条判据同时断"文件一字未动" —— 只断状态码的话，
   "先写了再拒"这种形状也是 403。
2. **覆盖保护要真比**：`expected_sha256` 给了就必须与库里当前值相等，409 那一发也断文件未动。
   只测"相等就通过"是一条半判据（它看不见"给了但没比"这种实现）。
3. **拒写的三种形状各自一条**：名字不在封闭集合（422）、body 里有表外键（422）、含凭据形态（422），
   再加上根目录缺失（503）。四种失败原因不许混成同一个数，否则排障时读不出是哪一条。
4. **中文与空白逐字回读**：owner 那句"确保不丢字"在 HTTP 面上的可证形态就是
   `read == 送进来的那串`，包括首尾空白与行尾。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acp.api.approvals import PRINCIPAL_HEADER
from acp.api import growth as api
from acp.app import create_app
from acp.growth import store

HEADERS = {PRINCIPAL_HEADER: "owner-1"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把档案根指到临时目录，并放两份空档案（"档案存在但为空"是合法首态）。"""
    directory = tmp_path / "growth"
    directory.mkdir()
    for name in store.ARCHIVES:
        (directory / name).write_text("", encoding="utf-8")
    monkeypatch.setenv(store.GROWTH_ROOT_ENV, str(directory))
    return directory


@pytest.fixture
def client():
    return TestClient(create_app())


def test_the_read_returns_both_archives_with_their_fingerprints(client, root):
    store.write(store.PROFILE_FILE, "我是事实档案\n", root=root)
    page = client.get("/growth").json()
    assert [row["name"] for row in page["archives"]] == list(store.ARCHIVES), (
        "面板拿到的档案集合与封闭集合不齐 ⇒ 要么漏了一支要么多了一支"
    )
    profile = page["archives"][0]
    assert profile["content"] == "我是事实档案\n"
    assert profile["sha256"] == store.digest_of("我是事实档案\n")
    assert profile["max_bytes"] == store.MAX_ARCHIVE_BYTES
    assert page["inject_headers"] == [store.PROFILE_HEADER, store.MISTAKES_HEADER]


def test_a_missing_archive_is_a_503_not_an_empty_200(client, root):
    (root / store.PROFILE_FILE).unlink()
    response = client.get("/growth")
    assert response.status_code == 503, response.text[:160]
    assert "archive_unavailable" in response.text


def test_a_write_without_the_principal_header_is_refused_and_writes_nothing(client, root):
    response = client.put(f"/growth/{store.PROFILE_FILE}", json={"content": "没人批准的字\n"})
    assert response.status_code == 403, response.text[:160]
    assert (root / store.PROFILE_FILE).read_text(encoding="utf-8") == "", "先写了再拒 = 403 是假的"


def test_a_write_lands_verbatim_including_chinese_and_trailing_spaces(client, root):
    sent = "  错题：把「已裁」读成「已修」  \n第二行保留空行\n\n"
    response = client.put(f"/growth/{store.PROFILE_FILE}", json={"content": sent}, headers=HEADERS)
    assert response.status_code == 200, response.text[:200]
    assert (root / store.PROFILE_FILE).read_text(encoding="utf-8") == sent, "不丢字在 HTTP 面上的形状"
    assert response.json()["sha256"] == store.digest_of(sent)


def test_a_stale_fingerprint_is_409_and_does_not_touch_the_file(client, root):
    store.write(store.MISTAKES_FILE, "库里现在是这一版\n", root=root)
    response = client.put(
        f"/growth/{store.MISTAKES_FILE}",
        json={"content": "我手上是旧版\n", "expected_sha256": "sha256:" + "0" * 64},
        headers=HEADERS,
    )
    assert response.status_code == 409, response.text[:200]
    assert "archive_stale" in response.text
    assert (root / store.MISTAKES_FILE).read_text(encoding="utf-8") == "库里现在是这一版\n"
    # 反向半边：给了**相等**的指纹就必须通过，否则这条判据只测了拒的那一半。
    ok = client.put(
        f"/growth/{store.MISTAKES_FILE}",
        json={"content": "我重读过再改\n", "expected_sha256": store.digest_of("库里现在是这一版\n")},
        headers=HEADERS,
    )
    assert ok.status_code == 200, ok.text[:200]
    assert (root / store.MISTAKES_FILE).read_text(encoding="utf-8") == "我重读过再改\n"


@pytest.mark.parametrize(
    ("name", "body", "code", "marker"),
    [
        # 名字不在封闭集合 ⇒ 422（这条同时挡住"把读档案的接口长成读任意文件的接口"）。
        (".env", {"content": "x\n"}, 422, "archive_name_unknown"),
        # body 里有表外键 ⇒ pydantic 层 422 并点名它（白名单的强制点，口径同 api/tasks.py）。
        (store.PROFILE_FILE, {"content": "x\n", "force": True}, 422, "extra"),
        # 含凭据形态 ⇒ 拒写而不是洗掉。
        (store.PROFILE_FILE, {"content": "ARK_API_KEY=" + "a" * 24 + "\n"}, 422, "凭据形态"),
        # 超上界 ⇒ 拒写而不是截断。
        (store.PROFILE_FILE, {"content": "长" * (store.MAX_ARCHIVE_BYTES + 1)}, 422, "拒写"),
    ],
)
def test_each_refusal_shape_has_its_own_reason(client, root, name, body, code, marker):
    response = client.put(f"/growth/{name}", json=body, headers=HEADERS)
    assert response.status_code == code, response.text[:200]
    assert marker in response.text, response.text[:200]
    assert (root / store.PROFILE_FILE).read_text(encoding="utf-8") == ""
    assert (root / store.MISTAKES_FILE).read_text(encoding="utf-8") == ""


def test_the_growth_surface_writes_no_database_and_reads_no_env_credential(monkeypatch, root):
    """档案面**不碰库**：`session_factory` 不给也照样能读能写。

    这条是"缺一格就退化成整档空判据"那一族：本文件其余用例都用了不带注入的 app，
    这一发把它显式钉成规则 —— 哪天有人给档案面加了库依赖，这里先红，
    而不是等到"没库就起不来看板"才在集成档里暴露。
    """
    client = TestClient(create_app())
    assert client.get("/growth").status_code == 200
    assert client.put(f"/growth/{store.PROFILE_FILE}", json={"content": "无库也写得进\n"}, headers=HEADERS).status_code == 200


def test_the_router_is_mounted_on_the_public_app():
    """端点写了但没挂 = 全绿而对外不存在。这条把"挂载面"与"这一支的路径集"连起来。"""
    paths = create_app().openapi()["paths"]
    assert "/growth" in paths and "/growth/{name}" in paths, sorted(paths)
    assert "put" in paths["/growth/{name}"], "写面掉了就只剩一个只读面板，而 owner 要的是能编辑"
    assert api.router is not None
