"""WorkBuddy 适配器包（T040）。

`__init__` 重导出 `Transport` 不是风格问题：注册表里的入口写法是 `acp.adapters.workbuddy:Transport`
（`registry/loader.py` 的 `ENTRY_RE` 就按这个形状校验），而 `Registration.resolve()` 是
`getattr(模块, 类名)` ⇒ 这一层不重导出，注册表那条入口就解析不出类。
`adapters/__init__.py` 那边**不**导出厂商实现（owner 2026-10-12 裁决 2 的口径：编排层只认协议形状）。
"""

from acp.adapters.workbuddy.transport import Transport

__all__ = ["Transport"]
