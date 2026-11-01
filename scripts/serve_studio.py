"""起工作室：把 `.env` 读进环境变量，再交给 `acp.serve`。

**为什么不直接 `python -m acp.serve`**：这个仓库的配置全部走 `os.environ`
（`acp/config/loader.py::env_value` 只读进程环境，没有 dotenv），所以 `.env` 必须先被读进来。
在 bash 里是 `set -a && . ./.env`，而双击 `.bat` 的人没有 bash —— 这里用 Python 读，
顺带把引号、`export ` 前缀、行尾注释这些写法都吃掉（批处理的 `for /f` 遇到 `&`、`!`、`%` 会当场坏掉）。

用法（一般由 `启动工作室.bat` 调用，也可以直接跑）：

    .venv\\Scripts\\python.exe scripts\\serve_studio.py --port 8766 --approver owner

参数原样透传给 `acp.serve`，这里不解释任何参数 —— 那是它自己的事。
"""

from __future__ import annotations

import os
import pathlib
import runpy
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def load_env(path: pathlib.Path) -> int:
    """把 `KEY=VALUE` 读进 `os.environ`，返回读到的条数。

    **不覆盖已有的环境变量**（`setdefault`）：命令行里显式给的 `ACP_XXX=...` 优先，
    否则"我明明在命令行改了"会被 `.env` 悄悄顶掉 —— 那种失败查起来最费劲。

    三种写法都认：`KEY=VALUE`、`export KEY=VALUE`、值两侧的成对引号。
    行内 `#` **不剥**（值里可能有 `#`，比如口令），要注释就整行注释。
    """
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
            count += 1
    return count


def main() -> int:
    os.chdir(ROOT)  # 迁移脚本、`web/dist`、总线根都按仓库根解析，别指望调用方在哪个目录
    loaded = load_env(ENV_FILE)
    print(f"[studio] 已读 {ENV_FILE.name}：{loaded} 项", flush=True)
    if not os.environ.get("ACP_DB_DSN"):
        print(
            "[studio] 警告：ACP_DB_DSN 没有值 —— 服务能起来，但每个端点都会回 503（存储没接）。",
            flush=True,
        )
    # `runpy` 而不是 `import acp.serve`：`acp.serve` 的 `__main__` 分支才解析参数、起 uvicorn，
    # 直接 import 只会拿到一个空模块。`sys.argv` 原样传下去，参数含义归它自己解释。
    runpy.run_module("acp.serve", run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
