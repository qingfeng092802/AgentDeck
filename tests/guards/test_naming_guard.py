"""T028：禁用命名与禁用依赖的静态扫描（FR-015 / FR-016｜R-01，宪法「二」护栏）。

口径与 `config/params-ref.md §7 / §8` 一致：清单不在测试里复制，样本从解析结果里**派生**
（把禁用项抄进测试源码，测试文件自己就变成了门禁的命中项）。

正对照锁定"命中 ≥ 1"而不是定值：绝对命中数会随文档提到某个词的次数漂，写成常数就是假判据。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from acp.guard import naming

REPO = naming.REPO_ROOT


def test_spec_parses_and_matches_its_own_registration_rules():
    spec = naming.load_naming_spec()
    assert spec.includes and spec.excludes and spec.exclude_dirs
    # §7 的说明：排除的是"经登记的具体文件名"，通配 = 未来文件自动豁免。
    assert not any(ch in token for token in spec.excludes for ch in "*?")
    assert "params-ref.md" in spec.excludes  # 缺这条 = 门禁命中清单自身
    spec.validate()


def test_two_carriers_parse_the_same_surface():
    """Python 载体与 `scripts/guard_naming.sh` 必须扫同一张面，否则"0 命中"两边各说各话。"""
    proc = subprocess.run(
        ["bash", "scripts/guard_naming.sh", "--show-spec"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 or "include=" not in proc.stdout:
        pytest.skip("本机没有可用于对照的 bash ⇒ 这一条未闭合，不是通过")
    collected: dict[str, set[str]] = {}
    for line in proc.stdout.splitlines():
        match = re.fullmatch(r"(include|exclude|exclude-dir)=(.+)", line.strip())
        if match:
            collected.setdefault(match.group(1).replace("-", "_"), set()).add(match.group(2))
    spec = naming.load_naming_spec()
    assert collected.get("include", set()) == set(spec.includes)
    assert collected.get("exclude", set()) == set(spec.excludes)
    assert collected.get("exclude_dir", set()) == set(spec.exclude_dirs)


def test_scan_surface_is_proved_not_collapsed():
    """"0 命中"有两种成因：真干净，或扫描面塌成空集。后者一样绿，所以面要自证。

    判据写成**关系式**（必须看见这几个我方产物文件、必须看不见依赖目录与已登记排除项），
    不写成文件数常数——加一个文件就红的那种门槛是假判据。
    """
    paths = set(naming.scanned_rel_paths())
    for must_see in (
        "pyproject.toml",
        "src/acp/guard/naming.py",
        "specs/001-multi-agent-collab-platform/spec.md",
    ):
        assert must_see in paths, f"扫描面看不见 {must_see} ⇒ 门禁已失效"
    assert "config/params-ref.md" not in paths  # 已登记的排除项确实在起作用
    assert not [p for p in paths if p.startswith((".venv", ".git", "__pycache__"))]


def test_scan_surface_keys_are_posix_shaped():
    """扫描面与命中侧的相对路径 MUST 是 POSIX 形态（P2-B，owner 2026-10-19 裁"产品缺陷"）。

    反斜杠是 Windows 上 `str(path.relative_to(base))` 的原生形态 ⇒ 判据拿正斜杠键永远查不到，
    "**门禁本身没坏**却报看不见 X"就是这个形态差造成的（Phase 3 / 6 / 8 / 9 / 12 五轮把它读成环境残差的根）。
    最后一条断言才是跨平台的：Linux 上两种写法同形，只保证不误伤；Windows 上它只在归一存在时才绿。
    """
    paths = naming.scanned_rel_paths()
    assert paths, "扫描面塌成空集 ⇒ 这条判据无从证伪"
    offenders = [p for p in paths if "\\" in p]
    assert not offenders, f"{len(offenders)} 项未归一（样本 {offenders[:1]}）⇒ 正斜杠键永远查不到"
    assert [p for p in paths if "/" in p], "多段路径里一个正斜杠都没有 ⇒ 归一没落地"


def test_hit_files_are_posix_shaped(tmp_path: Path):
    """命中侧同一条不变式：`hit_files` 进的是报错文本与 CLI 的 `hit-files=` 回显，形态不能与面分叉。"""
    spec = naming.load_naming_spec()
    sample = naming.banned_literals(spec.ban)[0]
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "mod.py").write_text(f"REF = {sample!r}\n", encoding="utf-8")
    scan = naming.scan_names(tmp_path, spec)
    assert scan.main_line_hits >= 1, "派生样本不开火 ⇒ 这条判据是死的"
    assert scan.hit_files, "命中行数 >0 却没记录文件 ⇒ 回显面塌了"
    assert not [h for h in scan.hit_files if "\\" in h], f"hit_files 未归一：{scan.hit_files}"
    assert scan.hit_files[0] == "pkg/sub/mod.py", f"嵌套样本形态不对：{scan.hit_files}"


def test_gate_reads_zero_hits_on_the_repository():
    scan = naming.scan_names()
    assert scan.main_line_hits == 0, f"命中文件={scan.hit_files}"
    assert scan.env_token_hits == 0
    assert scan.scanned_files > 0  # 扫到 0 个文件的"0 命中"是死判据


def test_positive_control_fires_without_copying_a_banned_name(tmp_path: Path):
    """派生样本 MUST 真的命中，且扫描管线会开火（防"恒绿"）。"""
    spec = naming.load_naming_spec()
    literals = naming.banned_literals(spec.ban)
    assert literals, "BAN 里取不出纯字面量分支 ⇒ 无法构造正对照"
    sample = literals[0]
    assert re.search(spec.ban, sample, re.IGNORECASE)  # 派生样本自证有效
    (tmp_path / "artifact.md").write_text(f"# 引用 {sample} 作为错误示范\n", encoding="utf-8")
    scan = naming.scan_names(tmp_path, spec)
    assert scan.main_line_hits >= 1


def test_dependency_list_has_only_one_source(tmp_path: Path):
    """裁决 5："改清单不该改代码" ⇒ §8 是唯一来源，代码里既无名单也无兜底默认值。

    两条断言各挡一种退化：
    ① 把 §8 的赋值行删掉 MUST 报 `SpecError`（而不是静默改用代码里写死的一份 ⇒ 那正是第二份事实源）；
    ② `naming.py` 的**源码文本**里 MUST NOT 出现任何禁用包名（名单只从解析结果流入，不落进源码）。
    样本从 `banned_literals()` 派生，本测试文件自己也因此不含禁用名——抄进测试就成门禁命中项。
    """
    names = naming.banned_literals(naming.load_dependency_ban())
    assert names, "§8 取不出纯字面量分支 ⇒ 无法证伪"
    source = (REPO / "src/acp/guard/naming.py").read_text(encoding="utf-8")
    leaked = [name for name in names if name in source]
    assert not leaked, f"禁用依赖名硬编码进了门禁源码：{leaked}"
    stripped = "\n".join(
        line
        for line in (REPO / "config/params-ref.md").read_text(encoding="utf-8").splitlines()
        if not line.startswith("DEP_BAN='")
    )
    copy = tmp_path / "params-ref.md"
    copy.write_text(stripped, encoding="utf-8")
    with pytest.raises(naming.SpecError):
        naming.load_dependency_ban(copy)


def test_dependency_surface_is_empty_on_both_sides():
    deps = naming.scan_dependencies()
    assert deps.hit_count == 0, f"命中={deps.hits}"
    # 两面都必须非空，否则交集为空是"集合本身为空"造成的假绿。
    assert deps.declared and deps.installed


def test_dependency_scan_can_fire(tmp_path: Path):
    ban = naming.load_dependency_ban()
    sample = naming.banned_literals(ban)[0]
    assert re.match(ban, sample)
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "probe"\ndependencies = ["' + sample + '"]\n', encoding="utf-8"
    )
    deps = naming.scan_dependencies(manifest, ban=ban, include_installed=False)
    assert deps.hit_count >= 1
    assert sample in deps.hits


def test_declared_dependencies_are_read_from_the_manifest():
    names = naming.declared_dependencies()
    # 取"必然在清单里"的判据形态而不是逐字名单：规范化后仍含字母且无版本符号。
    assert names
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", name) for name in names)


def test_cli_reports_the_gate(tmp_path: Path, capsys):
    """`python -m acp.guard.naming` 的退出码：干净树 0，含禁用名的树 1。"""
    assert naming.main([str(REPO)]) == 0
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    sample = naming.banned_literals(naming.load_naming_spec().ban)[0]
    (dirty / "x.md").write_text(f"{sample}\n", encoding="utf-8")
    assert naming.main([str(dirty)]) == 1


def test_the_dependency_field_is_named_for_what_it_counts(capsys):
    """字段名 MUST 是 `declared_incl_dev=`，不许再叫裸 `declared=`（owner 2026-10-12 裁决 3 / T046）。

    起因是现跑的两位读数：`selfcheck` C5 `declared=10`（只数 `[project].dependencies`）与本门禁
    `declared_incl_dev=12`（运行时面 + 全部 extras）。两个都真、单位不同；同名会让人把差额读成数据漂移，
    本仓已经这样误报过一次。改名没有判据护着就会被改回去 ⇒ 这条用例就是那个判据。
    """
    assert naming.main([str(REPO)]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("surface "))
    assert "declared_incl_dev=" in line
    assert "declared=" not in line, "又叫回 `declared` 就重新造出了那个同名歧义"
    count = int(line.split("declared_incl_dev=")[1].split()[0])
    assert count == len(naming.declared_dependencies()) >= 1, "字段名改了也不许把数丢掉"
