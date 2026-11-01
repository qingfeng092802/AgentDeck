"""`python -m acp.verify_mvp1 --project <项目键>` —— T124：把 quickstart §4 那四条产物判据变成一条命令的读数。

四条判据本身在 `quickstart.md` §4「产物与判据」那张表里，本模块**不重述判据、只执行它**：
① 产出物构成（3 个页面 + 1 个样式，入口页在成员里）；② 浏览器打开后 console 错误 0 条；
③ 页内链接可点（死链 / 404 = 0）；④ 无空白区块。四条**缺一即判未跑通** ⇒ 结论是这四条的合取，
不是"前三条过了就算过"。

三条边界，先写在这里，因为它们决定了读数的证明范围：
· **成员从 EN-17 那行读，不在这里重判**。`assemble.validated_manifest()` 是"哪些产物有资格进包"的
  唯一判定点（门禁 `valid` 那道筛在组包段），本模块若再筛一次就会出现两个口径 —— 交付包说有什么，
  这里就核什么；判据与规格对"资格"的改动只应落在一处。
· **链接按浏览器的方式解**：相对路径相对**那一页自己所在的目录**解（`file://` 就这么做），
  所以"四份文件是不是落在同一个目录"是这一条能否成立的前提。本轮实测落在同一个
  `qoder-workspace/`（四张卡的执行岗都是它），但那是选人的现值，不是结构保证 ⇒ 不同执行岗时
  这一条会如实报死链，不许改按 basename 放宽。
· **`http(s)` 链接只登记不核验**（宪法级边界：这一发不许产生真实外部调用），所以它进 `unchecked`
  而不是进 `dead_links`；读数面上"未核验"必须看得见，否则 0 死链会被读成"全部链接都能点"。

console 那一半边由 `cdp_console.mjs` 走 CDP 真取（本机 Edge 155 headless + Node 内置 WebSocket，
零新依赖，2026-10-17 实测可行）。这一发不联网、不碰任何厂商 API：它打开的是本地 `file://` 页面。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from sqlalchemy import select

from acp.archive import root as archive
from acp.orchestrate.nodes import assemble as assemble_node
from acp.persistence import session as session_module
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import Deliverable

PROG = "acp.verify_mvp1"
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2

#: §4 表第一行写的期望构成（R-07 / FR-017：3 个页面文件 + 1 个样式文件）。
#: 这两个数**不许在本文件里自证**：判据把它们与 `quickstart.md` 那一行逐字比对，
#: 文档改了数而代码没改 ⇒ 那条判据红（与「拿实现常量验实现」相反的形状）。
EXPECT_HTML = 3
EXPECT_CSS = 1

HTML_SUFFIXES = (".html", ".htm")
CSS_SUFFIXES = (".css",)
#: 不参与"死链"判定的方案。`mailto:` / `tel:` / `data:` 不是文件引用；
#: `http(s)` 是**外部**引用 —— 本发的边界是不联网，所以它们只登记（`unchecked`），不解析。
NON_FILE_SCHEMES = ("mailto", "tel", "data")
EXTERNAL_SCHEMES = ("http", "https")

CODE_ABSENT = "deliverable_absent"
CODE_AMBIGUOUS = "deliverable_ambiguous"
CODE_USAGE_ONE_OF = "arguments_missing"
CODE_PACKAGE_JSON_MISSING = "package_json_missing"
CODE_PACKAGE_DIVERGED = "package_json_diverged"
CODE_MEMBER_MISSING = "package_member_missing"
CODE_DRIVER_MISSING = "console_driver_missing"
CODE_DRIVER_FAILED = "console_driver_failed"
CODE_DRIVER_TIMEOUT = "console_driver_timeout"

DRIVER_NAME = "cdp_console.mjs"
DRIVER_PATH = Path(__file__).with_name(DRIVER_NAME)
NODE_DEFAULT = "node"


class VerifyFailure(RuntimeError):
    """前置不成立（没有交付包、包与库分叉、驱动起不来）。

    它**不是**"判据没过"：判据没过是一条 `VERIFY FAIL` 读数（退出码 1 里的一格），
    而这一档是"连判据要读的东西都没有"，混成一个代号就没法归因是产物不合格还是根本没跑过链。
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Composition:
    """交付包成员按后缀分好的三堆 —— 判据 ① 的读数面。"""

    entry_path: str
    pages: tuple[str, ...]
    styles: tuple[str, ...]
    other: tuple[str, ...]

    @property
    def matches_expectation(self) -> bool:
        return (
            len(self.pages) == EXPECT_HTML
            and len(self.styles) == EXPECT_CSS
            and not self.other
            and self.entry_path in self.pages
        )


@dataclass(frozen=True)
class PageReport:
    """一页的四格读数。`unchecked_*` 两格存在的原因是：0 必须能解释自己为什么是 0。"""

    rel_path: str
    console_errors: tuple[str, ...]
    console_warnings: tuple[str, ...]
    empty_blocks: tuple[str, ...]
    dead_links: tuple[str, ...]
    unchecked_links: tuple[str, ...]
    load_failed: tuple[str, ...]


@dataclass(frozen=True)
class VerifyReport:
    feature_request_id: uuid.UUID
    project_key: str
    package_path: str
    composition: Composition
    pages: tuple[PageReport, ...]

    @property
    def console_error_count(self) -> int:
        return sum(len(page.console_errors) for page in self.pages)

    @property
    def dead_link_count(self) -> int:
        return sum(len(page.dead_links) + len(page.load_failed) for page in self.pages)

    @property
    def empty_block_count(self) -> int:
        return sum(len(page.empty_blocks) for page in self.pages)

    @property
    def unchecked_count(self) -> int:
        return sum(len(page.unchecked_links) for page in self.pages)

    @property
    def passed(self) -> bool:
        """四条判据的合取 —— 这也是退出码唯一的来源。"""
        return (
            self.composition.matches_expectation
            and self.console_error_count == 0
            and self.dead_link_count == 0
            and self.empty_block_count == 0
        )


def _suffix_of(rel_path: str) -> str:
    return PurePosixPath(rel_path).suffix.lower()


def _member_paths(payload: dict) -> tuple[str, ...]:
    """包 JSON 里那串 `rel_path` —— 成员的**唯一**来源，不在这里加筛选条件。"""
    return tuple(str(item["rel_path"]) for item in payload.get("manifest", ()))


def deliverable_row(session, *, project_key: str, feature_request_id: uuid.UUID | None) -> Deliverable:
    """定位那张需求单的 EN-17 行。给了 `--request` 就按它，没给就按项目键 —— 项目键不唯一时**拒**。

    为什么不能"取最新那条"：`--project demo` 在演练里会跑第二发，默认取新等于让一次敲错的项目键
    把另一条需求单的产物当成这一发的读数。
    """
    if feature_request_id is not None:
        row = session.scalar(
            select(Deliverable).where(Deliverable.feature_request_id == feature_request_id)
        )
        if row is None:
            raise VerifyFailure(
                f"需求单 {feature_request_id} 没有 EN-17 行 ⇒ 交付包段没跑过（或跑在别的需求单上）",
                code=CODE_ABSENT,
            )
        return row
    rows = list(
        session.scalars(
            select(Deliverable)
            .join(FeatureRequest, Deliverable.feature_request_id == FeatureRequest.id)
            .where(FeatureRequest.project_key == project_key)
        ).all()
    )
    if not rows:
        raise VerifyFailure(
            f"项目 {project_key!r} 下没有任何交付包 ⇒ 链条没跑完"
            f"（受理 / 恢复 / 指派 / 执行 / 验收 / 交付包，一段都不能缺）",
            code=CODE_ABSENT,
        )
    if len(rows) > 1:
        raise VerifyFailure(
            f"项目 {project_key!r} 下有 {len(rows)} 张交付包，必须用 `--request <需求单号>` 点名其中一张",
            code=CODE_AMBIGUOUS,
        )
    return rows[0]


def package_payload(root: Path, row: Deliverable) -> dict:
    """读盘上那份包 JSON，并且**当面核对它和 EN-17 说的是不是同一个入口**。

    分叉不是理论风险：交付包段先写盘、门禁后落库，任何一侧后来被单独改过就会出现
    "库说入口是 A、包说入口是 B"，而 §4 那句"可直接预览"押的正是盘上那份。
    """
    path = root / assemble_node.delivery_path()
    if not path.is_file():
        raise VerifyFailure(
            f"库里有 EN-17 行，盘上却没有 {assemble_node.delivery_path()} ⇒ 包 JSON 与库分叉",
            code=CODE_PACKAGE_JSON_MISSING,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if str(payload.get("entry_path")) != str(row.entry_path):
        raise VerifyFailure(
            f"包 JSON 的入口 {payload.get('entry_path')!r} 与 EN-17 的 {str(row.entry_path)!r} 不是同一个",
            code=CODE_PACKAGE_DIVERGED,
        )
    if not _member_paths(payload):
        raise VerifyFailure("包 JSON 的 manifest 是空的 ⇒ 没有可核的交付物", code=CODE_PACKAGE_DIVERGED)
    return payload


def composition_of(root: Path, payload: dict, entry_path: str) -> Composition:
    """按后缀分堆，并顺手挡住"索引里点了名、盘上却没有那份文件"。

    成员缺文件走拒绝而不是走死链计数：那不是"页面里一个链接点不开"，那是整份索引在说谎，
    §4 的第一条判据（构成）在它上面就没有可信的读数可报。
    """
    members = _member_paths(payload)
    for rel in members:
        if not (root / rel).is_file():
            raise VerifyFailure(
                f"manifest 点了 {rel!r} 这份成员，盘上不存在 ⇒ 索引与实际不符", code=CODE_MEMBER_MISSING
            )
    pages = tuple(rel for rel in members if _suffix_of(rel) in HTML_SUFFIXES)
    styles = tuple(rel for rel in members if _suffix_of(rel) in CSS_SUFFIXES)
    other = tuple(rel for rel in members if rel not in pages and rel not in styles)
    return Composition(entry_path=str(entry_path), pages=pages, styles=styles, other=other)


def _classify(href: str) -> str:
    text = href.strip()
    if not text:
        return "empty"
    if text.startswith("#"):
        return "fragment"
    parts = urlsplit(text)
    if parts.scheme in EXTERNAL_SCHEMES:
        return "external"
    if parts.scheme in NON_FILE_SCHEMES:
        return "non_file"
    if parts.scheme:
        return "other_scheme"
    if text.startswith("/"):
        return "root_relative"
    return "relative"


def resolve_links(root: Path, page_rel: str, hrefs: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回 (死链, 未核验)。基目录 = **那一页自己所在的目录**，与 `file://` 的解析规则一致。

    `fragment` / `non_file` 既不是死链也不是"没核的外链"，所以两格都不进 —— 但 `empty`（空 href）
    与 `other_scheme` 落进死链：前者点了会回到本页顶（行为与写它的人预期相反，属于要看见的缺陷），
    后者在这里没有可解析的载体。`root_relative` 同样落死链：`file://` 下它指向盘上的绝对路径，
    一个"只在某人机器上成立"的链接不是可交付的链接。
    """
    base = (root / page_rel).parent
    dead: list[str] = []
    unchecked: list[str] = []
    for href in hrefs:
        kind = _classify(href)
        if kind == "external":
            unchecked.append(href)
            continue
        if kind in ("fragment", "non_file"):
            continue
        target = unquote(urlsplit(href.strip()).path)
        if kind in ("empty", "other_scheme", "root_relative") or not (base / target).is_file():
            dead.append(href)
    return tuple(dead), tuple(unchecked)


def run_cdp_driver(
    page_paths: Sequence[Path],
    *,
    node: str = NODE_DEFAULT,
    driver: Path = DRIVER_PATH,
    edge: str | None = None,
    timeout_s: int = 180,
) -> dict[str, dict]:
    """起一次 headless Edge，把所有页各开一个 target，按页返回原始读数（键 = 传进去的路径字符串）。

    驱动自己拥有 Edge 的默认路径与端口发现（`<profile>/DevToolsActivePort`）：这两样在本文件里
    再各写一遍就会分叉。这里只做三件事 —— 拼 argv、等退出、把 JSON 拆开；任何一步不成都静默。
    """
    argv = [node, str(driver)]
    for path in page_paths:
        argv += ["--page", str(path)]
    if edge:
        argv += ["--edge", edge]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s)
    except FileNotFoundError as exc:
        raise VerifyFailure(
            f"找不到 {node!r} ⇒ console 那一半没有读数来源。要么装上 Node，要么把这一格显式注入 "
            f"runner（判据用的就是显式注入），不许把「没浏览器」读成「没有报错」",
            code=CODE_DRIVER_MISSING,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VerifyFailure(
            f"console 驱动超过 {timeout_s} s 没回来（{driver.name}）", code=CODE_DRIVER_TIMEOUT
        ) from exc
    if not proc.stdout.strip():
        raise VerifyFailure(
            f"console 驱动没吐任何 JSON（退出码 {proc.returncode}）⇒ 无法判定，不按 0 处理",
            code=CODE_DRIVER_FAILED,
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VerifyFailure(f"console 驱动的输出不是 JSON：{exc}", code=CODE_DRIVER_FAILED) from exc
    if not parsed.get("ok"):
        raise VerifyFailure(
            f"console 驱动报了 {parsed.get('why')!r}（退出码 {proc.returncode}）⇒ 这一发没有 console 读数",
            code=CODE_DRIVER_FAILED,
        )
    return {str(item.get("page")): item for item in parsed.get("pages", ())}


Runner = Callable[[Sequence[Path]], dict[str, dict]]


def verify(
    *,
    project_key: str,
    session,
    bus_root: Path | str | None = None,
    feature_request_id: uuid.UUID | None = None,
    runner: Runner | None = None,
    node: str = NODE_DEFAULT,
    edge: str | None = None,
) -> VerifyReport:
    """把四条判据跑一遍，返回逐页读数。`runner` 是 console 那一半的注入口（判据用它给假读数）。

    项目根不在这里拼：`archive.archive_root()` 是「总线根 + 项目键」那条的现成承载点
    （`recover.py` 与受理段都走它），本文件另拼一次就会在它被改时安静地指向另一个目录。
    owner 2026-10-17 裁决 2 之后 `run.py` 那一份私有拼法也并到这里 ⇒ 全仓只剩这一处，核验段量的目录
    与装配段写的目录不可能再是两个。
    """
    row = deliverable_row(session, project_key=project_key, feature_request_id=feature_request_id)
    root = archive.archive_root(project_key, bus_root=bus_root)
    payload = package_payload(root, row)
    composition = composition_of(root, payload, str(row.entry_path))
    active = runner or (lambda paths: run_cdp_driver(paths, node=node, edge=edge))
    readings = active([root / page for page in composition.pages])
    pages: list[PageReport] = []
    for rel in composition.pages:
        item = readings.get(str(root / rel))
        if item is None:
            raise VerifyFailure(
                f"console 驱动没回 {rel!r} 这一页的读数 ⇒ 页集与读数集不是一套，不许按空读数放行",
                code=CODE_DRIVER_FAILED,
            )
        dom = item.get("dom") or {}
        hrefs = tuple(str(href) for href in dom.get("links", ())) + tuple(
            str(src) for src in dom.get("subresources", ())
        )
        dead, unchecked = resolve_links(root, rel, hrefs)
        pages.append(
            PageReport(
                rel_path=rel,
                console_errors=tuple(str(text) for text in item.get("errors", ())),
                console_warnings=tuple(str(text) for text in item.get("consoleWarnings", ())),
                empty_blocks=tuple(str(node_name) for node_name in (dom.get("emptyBlocks") or ())),
                dead_links=dead,
                unchecked_links=unchecked,
                load_failed=tuple(str(entry.get("errorText")) for entry in (item.get("loadingFailed") or ())),
            )
        )
    return VerifyReport(
        feature_request_id=row.feature_request_id,
        project_key=project_key,
        package_path=assemble_node.delivery_path(),
        composition=composition,
        pages=tuple(pages),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="MVP-1 端到端产物核验（quickstart §4 四条判据）")
    parser.add_argument("--project", required=True, help="项目键（总线根下的目录名）")
    parser.add_argument("--bus-root", help="总线根目录；缺省时走 `archive.resolve_bus_root()`"
                                           "（显式参数 > `ACP_BUS_ROOT` > cwd 下的 `AI项目台`）")
    parser.add_argument("--request", metavar="FEATURE_REQUEST_ID", help="项目键下有两张以上包时点名其中一张")
    parser.add_argument("--node", default=NODE_DEFAULT, help="跑 console 驱动用的 node 可执行文件")
    parser.add_argument("--edge", help="浏览器可执行文件；缺省用驱动里那份默认值（同一事实只写一处）")
    return parser


def _emit(verdict: str, **fields) -> None:
    joined = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    print(f"VERIFY {verdict} :: {joined}" if joined else f"VERIFY {verdict}")


def _report(report: VerifyReport) -> int:
    _emit(
        "PACKAGE" if report.passed else "FAIL",
        request=report.feature_request_id,
        project=report.project_key,
        entry=report.composition.entry_path,
        pages=len(report.composition.pages),
        styles=len(report.composition.styles),
        other=len(report.composition.other),
    )
    for page in report.pages:
        _emit(
            "PAGE",
            path=page.rel_path,
            console_errors=len(page.console_errors),
            dead_links=len(page.dead_links),
            empty_blocks=len(page.empty_blocks),
            subresource_failures=len(page.load_failed),
            unchecked_links=len(page.unchecked_links),
        )
        for text in page.console_errors:
            print(f"VERIFY CONSOLE :: {page.rel_path} :: {text}")
        for href in page.dead_links:
            print(f"VERIFY DEAD :: {page.rel_path} -> {href}")
        for block in page.empty_blocks:
            print(f"VERIFY EMPTY :: {page.rel_path} :: {block}")
        for href in page.unchecked_links:
            print(f"VERIFY UNCHECKED :: {page.rel_path} -> {href}")
    if report.composition.other:
        print(f"VERIFY OTHER :: {', '.join(report.composition.other)}")
    if report.passed:
        return EXIT_OK
    _emit(
        "VERDICT",
        console_error_count=report.console_error_count,
        dead_link_count=report.dead_link_count,
        empty_block_count=report.empty_block_count,
        composition_ok=report.composition.matches_expectation,
    )
    print(
        f"VERIFY HINT :: §4 那四条判据是**合取**：期望 页面={EXPECT_HTML} / 样式={EXPECT_CSS} / "
        f"console 错误=0 / 死链=0 / 空区块=0。哪一格不为 0 就照那一格回查，"
        f"不许把「某一格过了」读成 §4 已过"
    )
    return EXIT_RUNTIME


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    raw = (args.request or "").strip()
    request_id: uuid.UUID | None
    if raw:
        try:
            request_id = uuid.UUID(raw)
        except ValueError:
            _emit("FAIL", code=CODE_USAGE_ONE_OF, hint="`--request` 要一个需求单号（UUID）")
            return EXIT_USAGE
    else:
        request_id = None
    try:
        factory = session_module.make_session_factory()
        with session_module.new_session(factory) as session:
            report = verify(
                project_key=args.project,
                bus_root=args.bus_root,
                session=session,
                feature_request_id=request_id,
                node=args.node,
                edge=args.edge,
            )
    except VerifyFailure as exc:
        _emit("FAIL", code=exc.code)
        print(f"VERIFY DETAIL :: {exc}")
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001 - 命令行面只转写代号与类型名，不带凭据
        _emit("FAIL", code=getattr(exc, "code", type(exc).__name__))
        print(f"VERIFY DETAIL :: {type(exc).__name__}: {exc}")
        return EXIT_RUNTIME
    return _report(report)


if __name__ == "__main__":
    raise SystemExit(main())
