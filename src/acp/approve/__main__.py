"""`python -m acp.approve` 的入口（T056）。逻辑都在 `acp.approve.__init__`，这里只转交退出码。"""

from __future__ import annotations

from acp.approve import main

if __name__ == "__main__":
    raise SystemExit(main())
