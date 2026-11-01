"""`scripts/serve_studio.py` 读 `.env` 的那一小段：三种写法都认、且不覆盖已有环境变量。

**为什么这一小段值得单独立判据**：双击 `.bat` 的人没有 bash，`set -a && . ./.env` 那条路走不通，
所以 `.env` 的解析被搬进了 Python。这段代码一旦错，表现是"服务起来了、每个端点都回 503"或者
"我在命令行显式给的 DSN 被 `.env` 顶掉了" —— 两种都离出错点很远，最难查。

盯四件事：

1. `KEY=VALUE` / `export KEY=VALUE` / 值两侧成对引号，三种写法都要认；
2. **不覆盖已有环境变量**（`setdefault`）—— 命令行优先；
3. 行内 `#` 不剥（值里可能有 `#`），要注释就整行注释；
4. 文件不在时返回 0，不炸。

`os.environ` 是进程级全局，测试里要自己快照/还原：`load_env` 用 `os.environ.setdefault` 直接写，
`monkeypatch` 记不到这种写入，不管的话会漏到别的用例里。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "serve_studio.py"


def _load_module():
    """按路径加载 `scripts/serve_studio.py`（`scripts/` 不是包，`import` 不到）。"""
    spec = importlib.util.spec_from_file_location("_serve_studio", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def studio():
    """加载模块，并把 `os.environ` 整表快照 —— 用例结束时原样还回去。"""
    snapshot = dict(os.environ)
    try:
        yield _load_module()
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def _write_env(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_three_spellings_are_all_understood(studio, tmp_path):
    """`KEY=VALUE`、`export KEY=VALUE`、成对引号 —— 仓库 `.env` 里三种都出现过。"""
    for key in ("STUDIO_PLAIN", "STUDIO_EXPORTED", "STUDIO_QUOTED"):
        os.environ.pop(key, None)
    path = _write_env(
        tmp_path,
        "STUDIO_PLAIN=one\n"
        "export STUDIO_EXPORTED=two\n"
        'STUDIO_QUOTED="three # still inside the quotes"\n',
    )

    assert studio.load_env(path) == 3
    assert os.environ["STUDIO_PLAIN"] == "one"
    assert os.environ["STUDIO_EXPORTED"] == "two"
    assert os.environ["STUDIO_QUOTED"] == "three # still inside the quotes"


def test_an_existing_value_wins_over_the_file(studio, tmp_path):
    """命令行里显式给的 `ACP_DB_DSN=...` 必须压过 `.env`，否则"我明明改了"会被悄悄顶掉。"""
    os.environ["STUDIO_PRECEDENCE"] = "from-command-line"
    path = _write_env(tmp_path, "STUDIO_PRECEDENCE=from-dotenv\n")

    studio.load_env(path)

    assert os.environ["STUDIO_PRECEDENCE"] == "from-command-line"


def test_a_comment_line_is_skipped_but_an_inline_hash_is_kept(studio, tmp_path):
    """整行注释跳过；行内 `#` 是值的一部分（口令里常有）。"""
    for key in ("STUDIO_KEPT", "STUDIO_COMMENTED"):
        os.environ.pop(key, None)
    path = _write_env(
        tmp_path,
        "# 整行注释，不算一项\n"
        "STUDIO_KEPT=pa#ss\n"
        "STUDIO_COMMENTED # 这一行没有等号，跳过\n",
    )

    assert studio.load_env(path) == 1
    assert os.environ["STUDIO_KEPT"] == "pa#ss"
    assert "STUDIO_COMMENTED" not in os.environ


def test_a_missing_env_file_is_not_an_error(studio, tmp_path):
    """没有 `.env` 也要能起 —— 环境变量全在命令行里给的那种跑法同样合法。"""
    assert studio.load_env(tmp_path / "nope.env") == 0


def test_the_launcher_runs_from_the_repository_root(studio):
    """`ROOT` 是仓库根（`scripts/` 的上一级）：迁移脚本、`web/dist`、总线根都按它解析。"""
    assert studio.ROOT == ROOT
    assert (studio.ROOT / "src" / "acp" / "serve.py").exists()
