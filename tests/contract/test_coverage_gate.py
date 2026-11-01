"""P2-8：覆盖率基建的判据。

这一档的判据量的是"基建在不在、还能不能跑"，**不是**"覆盖率多少" ——
后者一旦被写成断言，会逼着人写"为了绿而绿"的断言（那种断言证的是"这行跑过了"，
不是"这件事是对的"）。等有了一两个版本的基线再谈指标，那时才有得谈。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_cov_is_a_declared_dev_dependency(project: dict) -> None:
    declared = " ".join(project["project"]["optional-dependencies"]["dev"])
    assert "pytest-cov" in declared, declared


def test_the_scope_is_the_package_not_the_tests(project: dict) -> None:
    """`source = ["src/acp"]`：把 tests 或整个仓库算进来，分母里会混进测试自己的行。"""
    assert project["tool"]["coverage"]["run"]["source"] == ["src/acp"]


def test_migrations_are_left_out_of_the_denominator(project: dict) -> None:
    """迁移是被 Alembic 在别的事务里执行的一次性脚本，算进分母只会稀释读数。"""
    omitted = project["tool"]["coverage"]["run"]["omit"]
    assert any("migrations" in item for item in omitted), omitted


def test_report_shows_the_gaps(project: dict) -> None:
    """`show_missing` 关掉之后报告只剩一个百分比 —— 那是最没用的形态：
    它告诉你"不够"，却不说不够在哪。"""
    report = project["tool"]["coverage"]["report"]
    assert report["show_missing"] is True
    assert report["skip_empty"] is True


def test_no_fail_under_yet(project: dict) -> None:
    """**刻意不设 `fail_under`**：在覆盖率还没有基线的时候挂一条硬门槛，
    唯一的作用是诱导"补上几行没有断言的调用"来把数字抬绿。真要挂，
    等下一轮拿着这份报告定了基线再挂。"""
    report = project["tool"]["coverage"]["report"]
    assert "fail_under" not in report, "要挂门槛就先拿这份报告定基线，别直接写死一个数"


def test_coverage_actually_runs() -> None:
    """真跑一次。**配置对**与**跑得通**是两件事**，后者才是有没有基建的分界。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "--cov=src/acp",
            "--cov-report=term-missing:skip-covered",
            "tests/guards/test_state_validation.py",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TOTAL" in completed.stdout, "没打出 TOTAL 行 ⇒ 覆盖率插件没接上"
