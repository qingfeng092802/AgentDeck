"""`python -m acp.danger` 的入口（T106）。逻辑都在 `acp.danger.__init__`，这里只转交退出码。"""

from __future__ import annotations

from acp.danger import main

if __name__ == "__main__":
    raise SystemExit(main())
