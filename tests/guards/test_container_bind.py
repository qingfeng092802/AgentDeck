"""P0-5：容器里绑 `0.0.0.0` 的那一档放行，必须是**显式开关**，不是默认值。

为什么值得单独钉一条：`resolve_host()` 守的是"对外可写、无鉴权的审批决议端点不许离开这台机器"。
容器要活下来就必须绑全网卡（`127.0.0.1` 在容器命名空间里只指容器自己，`-p 127.0.0.1:8765:8765`
打不进来），所以这里必须有一条放行路径 —— 但它一旦变成默认值，默认安装就变成一次敞开的门。
本文件量三件事：默认仍拒、显式才放、放行范围只有 `0.0.0.0` 这一个值。
"""

from __future__ import annotations

import pytest

from acp import serve


def test_container_bind_is_refused_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(serve.NON_LOOPBACK_ENV, raising=False)
    with pytest.raises(serve.HostRefused):
        serve.resolve_host(serve.CONTAINER_BIND)


def test_the_switch_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(serve.NON_LOOPBACK_ENV, "1")
    assert serve.resolve_host(serve.CONTAINER_BIND) == serve.CONTAINER_BIND
    for truthy in ("true", "YES", " on "):
        monkeypatch.setenv(serve.NON_LOOPBACK_ENV, truthy)
        assert serve.resolve_host(serve.CONTAINER_BIND) == serve.CONTAINER_BIND, truthy
    for falsy in ("", "0", "no", "false"):
        monkeypatch.setenv(serve.NON_LOOPBACK_ENV, falsy)
        with pytest.raises(serve.HostRefused):
            serve.resolve_host(serve.CONTAINER_BIND)


def test_the_switch_unlocks_nothing_but_the_container_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """开了开关也只认 `0.0.0.0`：`192.168.x.x` / `*` / `::` 依旧拒。

    这一条是"放行的是一件事，不是一个类别"的判据 —— 放宽成"任何非环回都行"才是真的把门拆了。
    """
    monkeypatch.setenv(serve.NON_LOOPBACK_ENV, "1")
    for raw in ("192.168.1.8", "*", "::", "localhost"):
        with pytest.raises(serve.HostRefused):
            serve.resolve_host(raw)


def test_loopback_still_passes_without_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(serve.NON_LOOPBACK_ENV, raising=False)
    assert serve.resolve_host("127.0.0.1") == "127.0.0.1"
    assert serve.resolve_host("::1") == "::1"


def test_the_warning_names_the_publish_side_rule(capsys: pytest.CaptureFixture[str]) -> None:
    """放行时打一行 stderr，且这行必须说到"发布侧只发到环回"。

    没有这行，运维只会看到"绑成功了"，不会知道安全边界现在靠 compose 那一行守着。
    """
    serve.resolve_host(serve.CONTAINER_BIND, allow_non_loopback=True)
    err = capsys.readouterr().err
    assert "127.0.0.1" in err and "环回" in err, err


def test_the_refusal_message_says_how_to_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    """拒的那一面要把开关名字写出来 —— 否则容器用户只能去翻源码。"""
    with pytest.raises(serve.HostRefused) as refused:
        serve.resolve_host(serve.CONTAINER_BIND, allow_non_loopback=False)
    assert serve.NON_LOOPBACK_ENV in str(refused.value)
