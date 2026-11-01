"""禁用命名与禁用依赖的静态扫描（T028，FR-015 / FR-016｜R-01，宪法「二」防回归护栏）。

**唯一事实源 = `config/params-ref.md` §7（命名）与 §8（依赖）**，本模块只解析、不复制清单。
复制一份清单的后果是"改了表、门禁还在用旧的"，而 §7 的说明明写"改本节即改门禁行为"。

与 `scripts/guard_naming.sh` 的关系：两个载体、同一张源表。
用例 `tests/guards/test_naming_guard.py` 断言两者解析出的 `BAN` / include / exclude /
exclude-dir **逐字同集**，且命中数相等 ⇒ 任一侧私自改口径就会红。
Python 侧存在的理由不是替换 shell 侧，而是：① 进 fast 档（`tests/guards/`）才能被每次改动触发；
② 依赖面扫描（§8）需要读 `pyproject.toml` 与运行时 `.dist-info`，shell 侧没有这个能力面。
"""

from __future__ import annotations

import fnmatch
import importlib.metadata
import os
import re
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PARAMS_REF_RELPATH = "config/params-ref.md"

SECTION_BAN_PREFIX = "BAN='"
SECTION_DEP_BAN_PREFIX = "DEP_BAN='"


class SpecError(RuntimeError):
    """params-ref 的门禁段落读不出可用规格（读不出来 = 不能当成"通过"）。"""


def _to_ere(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _section_lines(text: str, heading_prefix: str) -> list[str]:
    """取某一节的正文：从 `## <heading_prefix>` 起，到下一个 `## ` 标题或文件末止。"""
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"## {heading_prefix}"))
    except StopIteration as exc:
        raise SpecError(f"params-ref.md 找不到以 `## {heading_prefix}` 开头的章节") from exc
    body: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return body


def _first_grep_statement(section: Sequence[str]) -> str:
    """§7 里**第一条** `grep -rniE` 语句（含 `\\` 续行）。

    只取第一条是刻意的：该节正文留着"原写 `--exclude='tasks-review-*.md'`"这类**历史留痕**，
    按整节解析会把已被裁决废弃的通配排除项重新当成活参数吃进来（shell 侧 2026-10-11 实测踩过）。
    """
    collected: list[str] = []
    grabbing = False
    for ln in section:
        if not grabbing and ln.startswith("grep -rniE"):
            grabbing = True
        if grabbing:
            collected.append(ln)
            if not ln.rstrip().endswith("\\"):
                break
    if not collected:
        raise SpecError("§7 里找不到以 `grep -rniE` 起首的门禁语句")
    return " ".join(collected)


def _flag_values(statement: str, flag: str) -> tuple[str, ...]:
    found = re.findall(rf"--{flag}=('([^']*)'|([^\s]+))", statement)
    return tuple((second or third).strip("'") for second, third in ((f[1], f[2]) for f in found))


@dataclass(frozen=True)
class NamingSpec:
    """§7 解析结果。`ban` 是 ERE 源串，与 shell 侧共用同一串字面量。"""

    ban: str
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    exclude_dirs: tuple[str, ...]
    params_ref: Path = REPO_ROOT / PARAMS_REF_RELPATH

    def validate(self) -> None:
        """§7 的**说明**与**活参数**必须一致，否则"0 命中"是假的。"""
        if not self.includes:
            raise SpecError("§7 门禁语句里没有 --include= 扫描面")
        if not self.excludes:
            raise SpecError("§7 门禁语句里没有 --exclude= 登记项")
        if "params-ref.md" not in self.excludes:
            raise SpecError("排除表里没有 params-ref.md ⇒ 门禁会命中清单自身，登记已破口")
        for token in self.excludes:
            if "*" in token or "?" in token:
                raise SpecError(f"排除项 {token} 含通配 ⇒ 违反「排除的是经登记的具体文件名」")


def load_naming_spec(params_ref: Path | None = None) -> NamingSpec:
    path = params_ref or REPO_ROOT / PARAMS_REF_RELPATH
    if not path.is_file():
        raise SpecError(f"缺少 {path}（§7 / §8 清单的唯一事实源）")
    text = path.read_text(encoding="utf-8")
    section = _section_lines(text, "7.")
    ban_lines = [ln for ln in section if ln.startswith(SECTION_BAN_PREFIX)]
    if not ban_lines:
        raise SpecError("§7 未找到 BAN='…' 赋值行")
    ban = ban_lines[0][len(SECTION_BAN_PREFIX) :].rstrip("'")
    statement = _first_grep_statement(section)
    spec = NamingSpec(
        ban=ban,
        includes=_flag_values(statement, "include"),
        excludes=_flag_values(statement, "exclude"),
        exclude_dirs=_flag_values(statement, "exclude-dir"),
        params_ref=path,
    )
    spec.validate()
    return spec


def load_dependency_ban(params_ref: Path | None = None) -> str:
    """§8 的 `DEP_BAN='…'`（发行包名形态）。"""
    path = params_ref or REPO_ROOT / PARAMS_REF_RELPATH
    section = _section_lines(path.read_text(encoding="utf-8"), "8.")
    lines = [ln for ln in section if ln.startswith(SECTION_DEP_BAN_PREFIX)]
    if not lines:
        raise SpecError("§8 未找到 DEP_BAN='…' 赋值行")
    return lines[0][len(SECTION_DEP_BAN_PREFIX) :].rstrip("'")


def banned_literals(ban: str) -> tuple[str, ...]:
    """从 BAN 源串里取出**可以直接写出的纯字面量分支**，供正对照使用。

    用例需要一个"必然命中"的样本时 MUST 从这里取，MUST NOT 把禁用命名抄进测试源码——
    抄进去的那一刻测试文件自己就成了门禁的命中项。
    只收"逐字面"的分支：含字符类 / 分组 / 选中的分支（如 `qodercn[-_]agent[-_]sdk`）
    被抹掉元字符后得到的串**未必再命中原式**，拿它当样本会得到"正对照不开火"的假结论。
    """
    out = []
    for alternative in ban.strip().removeprefix("^").removesuffix("$").split("|"):
        if re.fullmatch(r"(?:[A-Za-z0-9_-]|\\.)+", alternative):
            out.append(alternative.replace("\\.", "."))
    return tuple(out)


def _walk_files(
    root: Path, includes: Sequence[str], excludes: Sequence[str], exclude_dirs: Sequence[str]
) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # 目录名**逐字**比对（与 grep 的 --exclude-dir 同语义）。正因为是逐字而非前缀，
        # `--exclude-dir=.venv` 管不住 `.venv-linux` —— §7 于 2026-10-12 补了这一项。
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pattern) for pattern in includes):
                if any(fnmatch.fnmatch(name, skip) for skip in excludes):
                    continue
                yield Path(dirpath) / name


def scanned_rel_paths(root: Path | None = None, spec: NamingSpec | None = None) -> tuple[str, ...]:
    """扫描面的**相对路径清单**（关系式判据的取证入口），形态统一为 POSIX（`/` 分隔）。

    命中数 = 0 有两种成因：真的干净，或扫描面塌成了空集。后者读起来一样绿，
    所以门禁必须能自证"我看见过这些文件"——本函数就是那条自证的面。
    """
    spec = spec or load_naming_spec()
    base = root or REPO_ROOT
    out = []
    for path in _walk_files(base, spec.includes, spec.excludes, spec.exclude_dirs):
        try:
            rel: Path = path.relative_to(base)
        except ValueError:
            rel = path
        out.append(rel.as_posix())
    return tuple(sorted(out))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@dataclass(frozen=True)
class NameScan:
    main_line_hits: int
    env_token_hits: int
    scanned_files: int
    hit_files: tuple[str, ...] = field(default=())

    @property
    def hits(self) -> int:
        return self.main_line_hits + self.env_token_hits


def scan_names(
    root: Path | None = None, spec: NamingSpec | None = None, extra_roots: Sequence[Path] = ()
) -> NameScan:
    """主扫描按**命中行数**计数（与 grep 的 `wc -l` 同口径），
    `.env*` 单独扫按**命中 token 数**计数（与 `grep -o` 同口径）。
    两个数不同口径是事实，不是笔误：一行里出现两个被否决命名时前者算 1、后者算 2。
    """
    spec = spec or load_naming_spec()
    base = root or REPO_ROOT
    pattern = _to_ere(spec.ban)
    main_hits = 0
    hit_files: list[str] = []
    scanned = 0
    for path in _walk_files(base, spec.includes, spec.excludes, spec.exclude_dirs):
        scanned += 1
        matched = sum(1 for line in _read(path).split("\n") if pattern.search(line))
        if matched:
            main_hits += matched
            hit_files.append(path.relative_to(base).as_posix())
    env_hits = 0
    roots = [base, *extra_roots]
    for scan_root in roots:
        for path in _walk_files(scan_root, (".env*",), (), spec.exclude_dirs):
            content = _read(path)
            tokens = len(pattern.findall(content))
            if tokens:
                env_hits += tokens
                hit_files.append(f"{path.relative_to(scan_root).as_posix()}(.env)")
    return NameScan(main_hits, env_hits, scanned, tuple(hit_files))


def declared_dependencies(pyproject: Path | None = None) -> tuple[str, ...]:
    path = pyproject or REPO_ROOT / "pyproject.toml"
    with path.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    names = [item for item in project.get("dependencies", [])]
    for group in project.get("optional-dependencies", {}).values():
        names.extend(group)
    out = []
    for spec_string in names:
        raw = re.split(r"[\[\s;<>=!~]", spec_string.strip(), maxsplit=1)[0].strip()
        if raw:
            out.append(raw.lower().replace("_", "-"))
    return tuple(sorted(set(out)))


def installed_distribution_names() -> tuple[str, ...]:
    """当前环境里**全部**已安装发行包名（含传递依赖）。

    只查 `pyproject.toml` 的声明面会漏掉"清单没写但环境里装着 / 由传递依赖带进来"的那一类，
    而 §8 的判据是包名集合的交集为空，两面都得算。
    """
    out = set()
    for dist in importlib.metadata.distributions():
        try:  # 半装坏的 dist-info（无 METADATA / 无 Name 头）不该让整条门禁崩
            meta = dist.metadata
            name = ((meta.get("Name") if meta is not None else "") or "").strip()
        except Exception:
            continue
        if name:
            out.add(name.lower().replace("_", "-"))
    return tuple(sorted(out))


@dataclass(frozen=True)
class DependencyScan:
    hits: tuple[str, ...]
    declared: tuple[str, ...]
    installed: tuple[str, ...]
    lockfile_present: bool

    @property
    def hit_count(self) -> int:
        return len(self.hits)


def scan_dependencies(
    pyproject: Path | None = None,
    ban: str | None = None,
    include_installed: bool = True,
) -> DependencyScan:
    pattern = _to_ere(ban if ban is not None else load_dependency_ban())
    declared = declared_dependencies(pyproject)
    installed = installed_distribution_names() if include_installed else ()
    hits = tuple(sorted({n for n in (*declared, *installed) if pattern.match(n)}))
    lock = (pyproject or REPO_ROOT / "pyproject.toml").parent / "uv.lock"
    return DependencyScan(hits, declared, installed, lock.is_file())


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = REPO_ROOT
    if len(argv) == 1:
        root = Path(argv[0])
    elif len(argv) > 1:
        print("usage: python -m acp.guard.naming [ROOT]", file=sys.stderr)
        return 2
    spec = load_naming_spec()
    names = scan_names(root, spec)
    deps = scan_dependencies()
    print(
        f"naming-guard main={names.main_line_hits} env={names.env_token_hits} "
        f"dep={deps.hit_count} scanned_files={names.scanned_files}"
    )
    print(
        # 字段名带 `incl_dev` 是有主的（owner 2026-10-12 裁决 3，随 T046 落地）：这一支数的是
        # "运行时面 + 全部 extras 组"的并集，而 `selfcheck` 的 C5 只数运行时面。
        # 两处都叫 `declared` 时，读数 10 与 12 会被读成"数据漂移"——本仓真错过一次，见 tasks.md 的 T039 段。
        f"surface declared_incl_dev={len(deps.declared)} installed={len(deps.installed)} "
        f"lockfile={'yes' if deps.lockfile_present else 'no'}"
    )
    if deps.hits:
        # 只回显包名，不回显任何路径行内容（与 §7 对 .env 的处置同一条理由）。
        print("banned-dependencies=" + ",".join(deps.hits), file=sys.stderr)
    if names.hit_files:
        print("hit-files=" + ",".join(names.hit_files), file=sys.stderr)
    passed = names.hits == 0 and deps.hit_count == 0
    print(f"SUMMARY main={names.main_line_hits} env={names.env_token_hits} dep={deps.hit_count} "
          f"gate={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
