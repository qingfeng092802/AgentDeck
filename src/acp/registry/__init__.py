"""注册表面（T039）：`acp.registry.loader` 是唯一入口，本包**不导入任何适配器**。

`import acp.registry` 不连带 `acp.adapters.*` ⇒ 编排层拿得到注册表而拿不到任何具体 transport，
这是 FR-012"删掉一个适配器目录后编排图仍可构建"在导入面上的前提。
"""

from __future__ import annotations

from acp.registry.loader import (
    DEFAULT_REGISTRY_PATH,
    Registration,
    RegistryError,
    load_registrations,
)

__all__ = ["DEFAULT_REGISTRY_PATH", "Registration", "RegistryError", "load_registrations"]
