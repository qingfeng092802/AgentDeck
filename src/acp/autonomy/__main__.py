"""`python -m acp.autonomy` 的入口（T106）。逻辑都在 `acp.autonomy.__init__`，这里只转交退出码。

薄到只有一行的理由与 `acp.approve.__main__` 同一条：判据要能直接调 `main(argv, client=...)`，
而 `__main__` 里放逻辑就等于给"怎么起这个通道"留了第二个载体。
"""

from __future__ import annotations

from acp.autonomy import main

if __name__ == "__main__":
    raise SystemExit(main())
