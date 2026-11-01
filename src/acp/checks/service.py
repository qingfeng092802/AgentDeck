"""确定性检查器：E2E 与安全。**不调模型。**

这两张卡在演示里是"模型写一份报告"。那是最假的一格：报告长得像结论，但没有人真的看过产物。
这里换成**代码检查**——输入是产物文件内容，输出是"哪一条不成立 + 在哪一行"，可复算、可复现。

边界（写在函数签名里，不写在文档里）：
- 只做**静态**检查：能读文件、能比对结构、能数标签。不跑浏览器、不截图、不模拟点击。
- 所以"375px 下不横向滚动"这类**必须看渲染**的判据，这里只能降级成"有 viewport meta +
  有 @media 断点"这种间接证据，并在报告里明写"间接"。把间接证据写成"已验证"就是撒谎。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: 一条发现。`level` 只有两档：`fail` 让整张卡不通过，`warn` 只记录。
Level = str

_VIEWPORT = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1 = re.compile(r"<h1[\s>].*?</h1>", re.I | re.S)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ALT = re.compile(r'\balt\s*=\s*["\'][^"\']+["\']', re.I)
_LINK = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\']', re.I)
_SCRIPT_SRC = re.compile(r'<script\b[^>]*src=["\']([^"\']+)["\']', re.I)
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>", re.I)
_MEDIA = re.compile(r"@media\b")
_ROOT_VARS = re.compile(r":root\s*\{")

_JS_SCHEME = re.compile(r"href\s*=\s*[\"']\s*javascript:", re.I)
_EVAL = re.compile(r"\b(eval|new\s+Function)\s*\(", re.I)
_SECRET = re.compile(r"(sk-[A-Za-z0-9]{16,}|bearer\s+[A-Za-z0-9._-]{16,}|password\s*=\s*[\"'][^\"']+[\"'])", re.I)
_ABS_URL = re.compile(r"^(https?:)?//", re.I)


@dataclass(frozen=True)
class Finding:
    level: Level
    code: str
    text: str


@dataclass(frozen=True)
class Report:
    name: str
    findings: tuple[Finding, ...] = ()

    @property
    def failed(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.level == "fail")

    @property
    def passed(self) -> bool:
        return not self.failed

    def markdown(self) -> str:
        lines = [f"# {self.name}", "", f"- 结论：{'通过' if self.passed else '不通过'}", ""]
        if not self.findings:
            lines.append("没有发现任何问题。")
            return "\n".join(lines) + "\n"
        lines.append("## 发现")
        lines.append("")
        for item in self.findings:
            mark = "✕" if item.level == "fail" else "!"
            lines.append(f"- {mark} `{item.code}` {item.text}")
        return "\n".join(lines) + "\n"

    def acceptance(self) -> list[str]:
        """这张卡的验收判据：检查器自己的结论就是它的判据。"""
        head = "静态检查全部通过" if self.passed else f"静态检查未通过（{len(self.failed)} 项）"
        return [head, f"报告见 {self.name}，含 {len(self.findings)} 条发现"]


def _pages(files: Mapping[str, str]) -> dict[str, str]:
    return {name: body for name, body in files.items() if name.endswith(".html")}


def scan_e2e(files: Mapping[str, str], *, expect_links: Sequence[str] = ()) -> Report:
    """结构层面的 E2E。`files` 是 `{相对路径: 内容}`。"""
    findings: list[Finding] = []
    pages = _pages(files)
    if not pages:
        return Report("e2e-report.md", (Finding("fail", "e2e_no_pages", "交付目录里没有 html 页面"),))

    for name, body in pages.items():
        title = _TITLE.search(body)
        if not title or not title.group(1).strip():
            findings.append(Finding("fail", "e2e_no_title", f"{name} 没有非空 <title>"))
        if not _H1.search(body):
            findings.append(Finding("fail", "e2e_no_h1", f"{name} 没有 <h1>（读屏与 SEO 都没有主标题）"))
        if not _VIEWPORT.search(body):
            findings.append(Finding("fail", "e2e_no_viewport", f"{name} 缺 viewport meta ⇒ 手机端不会按设备宽度排版"))
        for img in _IMG.findall(body):
            if not _ALT.search(img):
                findings.append(Finding("warn", "e2e_img_no_alt", f"{name} 有一张图没有 alt：{img[:48]}"))
                break
        for href in _LINK.findall(body):
            target = href.split("#", 1)[0].split("?", 1)[0]
            if not target or _ABS_URL.match(target):
                continue
            if not target.endswith(".html"):
                continue
            if target not in files:
                findings.append(Finding("fail", "e2e_broken_link", f"{name} 链到不存在的 {target}"))

    for wanted in expect_links:
        if wanted not in files:
            findings.append(Finding("fail", "e2e_missing_page", f"PRD 要求的页面 {wanted} 没有产出"))

    css = "\n".join(body for name, body in files.items() if name.endswith(".css"))
    if css:
        if not _MEDIA.search(css):
            findings.append(Finding("fail", "e2e_no_media", "样式表里没有 @media 断点 ⇒ 手机端没有任何适配"))
        if not _ROOT_VARS.search(css):
            findings.append(Finding("warn", "e2e_no_root_vars", "样式表没有 :root 变量（改一版配色要动多处）"))
    else:
        findings.append(Finding("fail", "e2e_no_css", "交付目录里没有 css 文件"))
    return Report("e2e-report.md", tuple(findings))


def scan_security(files: Mapping[str, str]) -> Report:
    """静态安全扫描。查的是"这类代码不该出现在静态站里"，不是漏洞评估。

    后缀白名单**必须与 `ArtifactFormat` 同步**：放进来一个可执行的格式却不扫它，
    等于凭空多一类没人看的代码。`.js` 是 2026-10-29 随协议域一起加的 ——
    它比 `.py` 更该扫（页面里能被浏览器直接执行），不是"顺带补上"。
    """
    findings: list[Finding] = []
    for name, body in files.items():
        if not name.endswith((".html", ".css", ".py", ".js")):
            continue
        for match in _JS_SCHEME.finditer(body):
            findings.append(Finding("fail", "sec_js_scheme", f"{name} 出现 javascript: 链接（第 {body[: match.start()].count(chr(10)) + 1} 行）"))
            break
        for match in _EVAL.finditer(body):
            findings.append(Finding("fail", "sec_eval", f"{name} 出现 {match.group(0)}（第 {body[: match.start()].count(chr(10)) + 1} 行）"))
            break
        for match in _SECRET.finditer(body):
            findings.append(Finding("fail", "sec_secret", f"{name} 疑似写入凭据（第 {body[: match.start()].count(chr(10)) + 1} 行）"))
            break
        if name.endswith(".html"):
            for src in _SCRIPT_SRC.findall(body):
                if _ABS_URL.match(src):
                    findings.append(Finding("warn", "sec_external_script", f"{name} 引用了外站脚本 {src[:48]}（供应链风险）"))
            inline = len(_INLINE_SCRIPT.findall(body))
            if inline:
                findings.append(Finding("warn", "sec_inline_script", f"{name} 有 {inline} 段内联脚本（难做 CSP，也不利于缓存）"))
            outside = [
                href
                for href in _LINK.findall(body)
                if _ABS_URL.match(href) and not href.startswith("//127.0.0.1") and not href.startswith("//localhost")
            ]
            if outside:
                findings.append(Finding("warn", "sec_external_link", f"{name} 有 {len(outside)} 个外站链接"))
    if not findings:
        findings.append(Finding("warn", "sec_clean", "没有命中任何规则（不代表安全，只代表没撞上已知模式）"))
    return Report("security-report.md", tuple(findings))


def scan_gate(cards: Sequence[Mapping[str, object]], *, upstream: Sequence[Report] = ()) -> Report:
    """门禁汇总：每张卡的产物是否达到它自己声明的下限，**以及上游检查器说了什么**。

    `cards` 每项形如 `{"id","title","format","bytes","lines","min_bytes","min_lines","criteria"}`。

    `upstream` 是 E2E / 安全那两份报告（U5，2026-10-28 真跑）。原来这一格只按"产物达到下限"
    判，于是同一轮里出现过一个自相矛盾的结果：`e2e-report` 判**不通过**，
    `gate-report` 判**通过，没有发现任何问题**。检查器写了一份报告，却没有任何东西读它做决定 ——
    那两张卡就只是"板上多了两行字"。汇总的职责本来就是汇总，不读上游等于不汇总。
    """
    findings: list[Finding] = []
    for card in cards:
        cid = str(card.get("id"))
        fmt = str(card.get("format"))
        got_bytes, got_lines = int(card.get("bytes") or 0), int(card.get("lines") or 0)
        want_bytes, want_lines = int(card.get("min_bytes") or 0), int(card.get("min_lines") or 0)
        if got_bytes == 0:
            findings.append(Finding("fail", "gate_no_artifact", f"{cid} 没有任何产物"))
            continue
        if got_bytes < want_bytes or got_lines < want_lines:
            findings.append(
                Finding(
                    "fail",
                    "gate_below_floor",
                    f"{cid} 产物 {got_bytes}B/{got_lines}行 低于声明的 {want_bytes}B/{want_lines}行（{fmt}）",
                )
            )
        if not card.get("criteria"):
            findings.append(Finding("warn", "gate_no_criteria", f"{cid} 没有可用于比对的判据"))
    if not cards:
        findings.append(Finding("fail", "gate_no_cards", "这一单没有任何卡可汇总"))

    for report in upstream:
        for item in report.failed:
            findings.append(Finding("fail", f"gate_{item.code}", f"{report.name}：{item.text}"))
        warns = [item for item in report.findings if item.level != "fail"]
        if warns and report.passed:
            # 上游只有提示时不当成失败，但也不吞掉 —— 吞掉的话"读过了"和"没读"看不出区别。
            findings.append(Finding("warn", "gate_upstream_warn", f"{report.name} 有 {len(warns)} 条提示，未阻断"))
    return Report("gate-report.md", tuple(findings))
