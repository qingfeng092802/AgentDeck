"""P2-6：工作区残留不许进版本库 —— 忽略表本身要有判据。

为什么值得单开一条：这类目录（`.wbtmp/` 的浏览器现场、`.coverage` 的二进制读数、
`.venv/`、`node_modules/`）的共同特点是**它们每天都被重写一遍**。漏掉其中任何一个，
表现不是"报错"，而是工作树从此再也干净不了 —— 于是 `git status` 里那堆噪声会被
习惯性忽略，真正的改动就藏进去了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"


@pytest.fixture(scope="module")
def ignored() -> tuple[str, ...]:
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.startswith("#"))


@pytest.mark.parametrize(
    "entry,why",
    [
        (".wbtmp/", "浏览器与扫描自动化现场（每次跑都重写，且带本机绝对路径）"),
        (".coverage", "覆盖率的二进制读数（每次跑都重写）"),
        ("htmlcov/", "覆盖率的 HTML 报告（生成物）"),
        (".venv/", "本地虚拟环境"),
        ("node_modules/", "前端依赖树"),
        (".mypy_cache/", "类型门禁的缓存"),
        (".pytest_cache/", "测试缓存"),
        ("__pycache__/", "字节码缓存"),
        ("web/dist/", "前端构建产物（由 `npm run build` 生成）"),
        ("results/", "真跑的总线根（厂商响应与 workspace 都落在里面）"),
        ("shots/", "取证截图（33MB 且随轮次无限增长）"),
        (".env", "凭据（明文口令与四家厂商 key）"),
    ],
)
def test_the_workspace_leftovers_stay_out(ignored: tuple[str, ...], entry: str, why: str) -> None:
    assert entry in ignored, f"`.gitignore` 少了 {entry} ⇒ {why}"


def test_the_env_template_is_NOT_ignored(ignored: tuple[str, ...]) -> None:
    """反向对照：`.env.example` 必须留下来 —— 新环境照它抄才配得起来。

    只写 `.env.*` 而忘了 `!.env.example`，表现是"克隆下来没有样例"，
    那种缺口只有第一次在新机器上配环境的人会撞到。
    """
    assert ".env.*" in ignored
    assert "!.env.example" in ignored
