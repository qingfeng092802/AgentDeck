"""P1-2：进程入口必须把 `SecretsFilter` 装到 root logger 上。

过滤器本体与脱敏规则在 `tests/guards/test_credentials.py` 里已经逐形态钉过；本文件只钉
**装配面**：`install_logging_filter()` 定义了却没人调用时，规则再全也不会生效。
"""

from __future__ import annotations

import pytest

from acp import serve


def test_serve_main_installs_the_logging_filter(monkeypatch):
    """`acp.serve` 的 main 在解析参数之前就安装过滤器；`--help` 足以触发。"""
    calls: list[None] = []
    monkeypatch.setattr(serve, "install_logging_filter", lambda: calls.append(None))

    with pytest.raises(SystemExit):
        serve.main(["--help"])

    assert calls == [None], "serve.main 没有调用 install_logging_filter()"
