"""P1-1：静态门禁本身要有判据 —— 它开了哪儿、跑得通、以及"不该有的东西不在"。

门禁最容易腐烂的方式不是"报错"，是**范围悄悄变大或变没**：有人把 `files` 改成全仓、
有人把 `disable_error_code` 加了一串、有人往两个目录里塞回 `# type: ignore`。
这些都不影响运行期判据，所以只有一份静态判据能挡住。
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: 门禁覆盖的两个目录（与 `pyproject.toml` 的 `files` 逐字同源，改一边就得改另一边）。
CORE_DIRS = ("src/acp/orchestrate", "src/acp/adapters")


@pytest.fixture(scope="module")
def mypy_config() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["mypy"]


def test_the_gate_covers_exactly_the_two_load_bearing_dirs(mypy_config: dict) -> None:
    """范围是判据的一部分：**只**这两个目录。

    全仓一次开严会产出几百条与改动无关的错，那种门禁活不过两周；反过来，把范围悄悄缩到
    一个文件就等于把门禁关掉了 —— 两个方向都得钉住。
    """
    assert mypy_config["files"] == list(CORE_DIRS), mypy_config.get("files")


def test_the_gate_does_not_quietly_grow_a_suppression_list(mypy_config: dict) -> None:
    """允许的抑制只有**一处**且必须点名理由：LangGraph 的 `add_node` 协议推断（见配置注释）。"""
    overrides = mypy_config.get("overrides") or []
    assert len(overrides) == 1, overrides
    only = overrides[0]
    assert only["module"] == ["acp.orchestrate.graph"]
    assert only["disable_error_code"] == ["call-overload", "arg-type"]


def test_mypy_is_a_declared_dev_dependency() -> None:
    """不声明 = "谁装过谁才跑得了"。那种门禁第二周就会被绕过。"""
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    declared = " ".join(project["optional-dependencies"]["dev"])
    assert "mypy" in declared, declared


def _annotations(tree: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None:
            found.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            found.append(node.returns)
    return found


def test_no_bare_any_survives_in_the_two_dirs() -> None:
    """裸 `Any`（`x: Any` / `-> Any`）在这两个目录里必须绝迹。

    `Mapping[str, Any]` 这类**带参数**的不算：JSON 边界的值形状本来就是未知的，把 `Any`
    收窄进一个明确的容器里是诚实的写法；裸 `Any` 才是"这一格是什么都行"的静默放行。
    """
    offenders: list[str] = []
    for directory in CORE_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for annotation in _annotations(tree):
                if isinstance(annotation, ast.Name) and annotation.id == "Any":
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{annotation.lineno}")
    assert offenders == [], f"还有裸 Any：{offenders}"


def test_no_type_ignore_pragma_survives_in_the_two_dirs() -> None:
    """`# type: ignore` 也绝迹。

    用 `tokenize` 取注释而不是文本搜：注释里**提到**这三个字（解释"为什么不用 ignore"）
    不该被读成一条 ignore。只有注释本身以它开头才算。
    """
    offenders: list[str] = []
    for directory in CORE_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type != tokenize.COMMENT:
                    continue
                if token.string.strip().startswith("# type: ignore"):
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{token.start[0]}")
    assert offenders == [], f"还有 type: ignore：{offenders}"


def test_mypy_reports_no_issues_on_the_configured_scope() -> None:
    """真跑一遍。配置对不对是一回事，跑不跑得通是另一回事。

    失败时把 mypy 的原话打出来（不加 `--no-error-summary`）：这条判据的价值全在"错在哪一行"。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        # 子进程那一侧的写入口径要钉住：本仓的 `test_subprocess_decoding` 要求每一个起解释器
        # 的调用点都明写编码，否则父进程按 utf-8 读、子进程按 locale 写（Windows 上是 cp936），
        # 中文错误消息会变成半个乱码 —— 而这条判据失败时最需要读的正是那段中文。
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
