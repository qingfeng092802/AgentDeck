"""MVP-2 笔 F：`web/` 前端的判据 —— 前端不许调不存在的端点，也不许把颜色自己再抄一份。

这一档绝大多数判据扫**文本面**：`web/src/**` 与两份配置。它不跑浏览器、不读 `node_modules/`、不读 `web/dist/`
（后两个是忽略项，读它们就等于让判据依赖一次 `npm install` 的产物）。节 十八 是唯一一处执行前端源码：
Node 24 的 TS type-stripping 直接 import `src/lib/ticketView.ts` 的纯函数，仍然不读 `node_modules/`/`dist`。

为什么这些判据值得钉在 Python 侧而不是前端测试框架里：本仓的门禁、变异与台账都跑在 pytest 上，
而"前端与后端漂开"这件事的**另一半证据在后端**（OpenAPI 路径集、`serve.DEFAULT_PORT`、
`PRINCIPAL_HEADER`、`params-ref §11` 的标题）。跨两侧的判据只可能长在能看到两侧的那一侧。

十七节分工：
- 节 一 = 调用面：`client.ts` 里的路径 ⊆ 公开 OpenAPI 面；dev proxy 覆盖每个根路径；端口与 `serve.py` 同值。
- 节 二 = 头部字面量：`x-acp-principal` 两侧同串（三支写面共用一把钥匙）。
- 节 三 = 令牌单一来源：`tokens.json` 与 `index.css` 的 `@theme` 逐键同值，且不留死令牌；
  玻璃层的 `panel/row/row-alt` 必须逐字等于 tint 按 alpha 叠出来的合成链，光晕的 stop 也必须用令牌写（笔 AD）；
  三个 `@keyframes` 的名单钉住（笔 AM 起办公室的 CSS 敲击动效随人物一起删除，SVG 的 SMIL 另查停靠值），
  且**没有一条动画能把内容停在 opacity=0 那一帧**（笔 AD 第 2 笔实测的隐形卡；SMIL 同一条）。
- 节 四 = AA 在**每一种行底色**上分别量（不是只量页面背景）。
- 节 五 = 构建面的本机约束：vite 钉 7.x、plugin-react 钉 5.x（SAC 拦 rolldown 的原生件，实测过）。
- 节 六 = 忽略面与门禁面同批（`node_modules/` `web/dist/` 两边都得有）。
- 节 七 = 审批与活动流的两侧口径（笔 G）：域值 = 下拉框、通道只许人提交的那两个、
  一条 `EventSource` 不许带第二份间隔或游标、帧名与 `web.STREAM_EVENT` 同串、CAS 的 version 只能来自刚读到的详情。
- 节 八 = 扫描面自证：这些判据真的读到了文件（读到 0 个文件时不许当"通过"）。
- 节 九 = 三视图骨架（PR2，owner 2026-10-24）：七块旧面板各归一个视图且各挂一次、一级导航 = 三个 tab
  （治理带待办数）、**只有当前视图在 DOM 里**（禁 hidden/显隐，非当前视图必须卸载）、栏宽只有一个载体、
  对比度不许靠不透明度买、sticky 只许顶栏一个载体（owner 2026-10-20 裁①，笔 AB）。
- 节 十 = 内容不许把栏撑破（笔 AC，owner 2026-10-20 的溢出裁决）：每一处机器串落点许断词或自己滚、
  落点名单与代码同数、壳里的三栏与包装层带 `min-w-0`、活动流那一列有自己的滚动盒且载体只有一个。
  笔 AD 第 3 笔按规范 §6.3/§6.4 把名册 7 格升到 **13 格**、形状 3 条升到 **9 条**，并把"吸收器"降到**逐格指定**
  （`break-words` 对一条 sha256 不起作用，宽口径会让坏形状绿），形状计数改为**从名册里数出来**（两侧同改不再能凑数）。
  v4 第 A 笔再加 4 格（工位名牌与气泡句、L1 摘要的主句与副句）⇒ **17 格 / 11 条形状**。
- 节 十一 = 按钮不许被压成两行（笔 AD 第 2 笔 · 规范点名的 P1）：每颗按钮的最小高度从类名算出来并 ≥ 32 px、
  字号必须由按钮自己写明、非行按钮必须带 `shrink-0` + `whitespace-nowrap`、"按钮 + 说明"那条操作条只许 `ActionBar` 一个产地。
- 节 十二 = 流与状态的唯一产地（笔 AD 第 2 笔）：整棵前端只有一条 `EventSource` 且构造在 `client.ts`、
  Agent 栏那三态由类型与标签表共同封住（"运行中/阻塞"在代码面上写不出来）、45 000ms 与 200 行各只有一个载体、
  壳外（`App.tsx` 以外）一律不许 `sticky`。§8 那张表没有覆盖这些新产地，所以这一节是**加严**，既有 32 条一字未动。
- 节 十三 = 时间轴与抽屉（笔 AD 第 3 笔）：行内不许有载荷、五个槽各恰好一次、斑马行不许回来；
  `KIND_META` 的键集与 `event_log_kind` 域值逐字对读（闪烁那一档只许那三个拦截/危险 kind）；
  抽屉是按 `row` 早退挂载的模态层，且它的 `overflow-y-auto` 一次都不许出现（滚动归 #31 那唯一载体）；
  "上一/下一条"的方向在抽屉、索引与视图边界在面板；那道竖线的定义与引用各只有一个产地。
- 节 十四 = 视觉定稿（笔 AD 第 4 笔，owner 2026-10-21 裁"Dev 模式外观彻底冻结"）：
  四块面板的外壳只剩 `glass` 一种皮肤（旧的不透明底 `bg-panel` 一格都不许留）、
  三选一是**一个分段控件**但 `name="verdict"` 与三个域值都不许换载体、
  渲染失败那一页也是玻璃卡且三句文案要全、JSX 正文里不许再把 markdown 星号当字面量画出来。
- 节 十五 = 办公室视图（v4 第 A 笔 · AK-1/AK-2）：三档只借一个 45 000ms、没有读数的状态词一个都不许说、
  房间的根是 `<div>` 且不进 `PANEL_FILES`/`anchorOf`/`App.tsx`、房间不吸顶也不自己滚、房间不读 `payload`、
  气泡粘 `glass`、kind 标签复用 `kindMeta`、白板只报"这一页"看到的三个数。
  笔 AM/AN 起人物/桌子/装饰换成 `components/svg/` 的十四张死画：资产白名单、无脚本外链、SMIL 停靠值、
  岗位→形象唯一映射（非 live 与无形象岗位一律 Hiring）。
- 节 十六 = 办公室接流与三视图切换（v4 第 B 笔 + PR2）：房间只订阅那一条流、
  彩排先点名十二个工位（演练帧，owner 2026-10-24）再重放已收到的帧、办公室只在 office 分支挂载
  （切页卸载；owner 2026-10-24 反转 2026-10-21 的"双页常驻"）、壳一屏高、房间底部那颗按钮把视图交给任务页。
- 节 十七 = 主题切换与 FOUC（笔 AH）：`@theme` = dark、`[data-theme="light"]` = light 两遍对读；
  内联脚本先于模块且同帧写 `colorScheme`；切换按钮从 `<html data-theme>` 回读、落盘、两档文案；
  浅色必须覆盖专家台 / 办公室 / 抽屉三块屏，办公室五处装饰光逐处降透明度。
- 节 十八 = OfficeHud 的两个纯决策函数（v1.0.1 复验收尾）：`deliveryCelebrationOf` 对挂载前 4.4s 的帧
  MUST NOT 放庆祝、挂载后 500ms MUST 放；`entryTargetOf` 在 `entry_path` 缺失时 MUST 返回空目标与
  「未申报」note，`entryDownloadDisabled("missing")` MUST 为 True。两条都经 Node 直接执行 `.ts`。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp import serve
from acp.api import web
from acp.api.approvals import PRINCIPAL_HEADER
from acp.app import create_app
from acp.approval import repository as repo
from acp.persistence.enums import values_of

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"
SRC = WEB / "src"


def read(rel: str) -> str:
    """读 `web/` 下的一个文件。缺失就是失败 —— 不许"前端不在了就跳过"。"""
    path = WEB / rel
    assert path.is_file(), f"前端文件不在：{path}"
    return path.read_text(encoding="utf-8")


def ts_code(rel: str) -> str:
    """剥掉注释后的 TS 正文。**"禁止出现某个串"那一类判据只能扫代码面**：
    `client.ts` 的注释里正当提着 `/api`、`/openapi.json`、`poll_ms`、`Last-Event-ID`（都是在解释"为什么不用它"），
    按原文扫就会把说明本身当成违例 —— 第一跑就是这么红的两条。
    反向风险也在这里：把违例藏进注释就不再是违例（注释不执行），所以这一层只用于**禁令类**判据，
    声明类判据（域值数组、`@theme`、令牌）仍按原文读。
    """
    text = re.sub(r"/\*.*?\*/", "", read(rel), flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def svg_code(rel: str) -> str:
    """SVG 组件只剥 `/* … */`，**不能走 `ts_code`**：每张资产的 `<svg>` 都带
    `xmlns="http://www.w3.org/2000/svg"`，`ts_code` 会把那一行从 `http:` 起整段切掉，
    藏在 `xmlns` 之后的事件处理器/外链就会被安全扫描漏掉（变异 M13 自证这一格）。
    这些文件里没有 `//` 行注释，所以少剥一种注释不会让禁令失效。
    """
    return re.sub(r"/\*.*?\*/", "", read(rel), flags=re.S)


TICKET_VIEW = "src/lib/ticketView.ts"


def node_ticket_view(probe: str) -> dict:
    """把一段探针交给 Node 的 TS type-stripping 跑 `ticketView.ts`。

    这是本档**唯一**一处执行前端源码的判据：直接 import 那一份 `.ts`（OfficeHud 用的同一组函数），
    不读 `node_modules/`、不读 `web/dist/`、不起浏览器。Node 不在 PATH 上就红 —— 前端构建也需要它，
    静默跳过会把"没跑"读成"通过"。
    """
    node = shutil.which("node")
    assert node, "Node 不在 PATH 上 ⇒ 无法执行 ticketView.ts"
    source = WEB / TICKET_VIEW
    assert source.is_file(), f"前端文件不在：{source}"
    script = (
        'import { pathToFileURL } from "node:url";\n'
        f"const mod = await import(pathToFileURL({json.dumps(str(source))}).href);\n"
        f"{probe}\n"
    )
    done = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(WEB),
    )
    assert done.returncode == 0, f"Node 探针失败：{done.stderr[-1200:]}"
    return json.loads(done.stdout)


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file())


def public_paths() -> set[str]:
    """公开 app 的 OpenAPI 路径集（**只**从 `create_app()` 拿，不数 router）。

    这一步必须走装配后的应用：建审批单挂在 `internal_router` 上，从结构上就进不了这个集合 ——
    所以"前端不许出现建单入口"这条不需要单独写一条正则去禁，它由"路径必须在这一个集合里"自动挡住。
    """
    document = TestClient(create_app()).get("/openapi.json").json()
    return set(document["paths"])


# ---------------------------------------------------------------- 一、调用面


def test_the_client_only_calls_paths_that_exist_on_the_public_app():
    """每个字面量路径都要在公开面上找得到；带 `${...}` 的按前缀比（参数段本来就不是字面量）。

    这一发同时挡住"看板能建单"：`POST /approvals` 挂在 `internal_router` 上，结构上不在
    `create_app()` 的 OpenAPI 里 ⇒ 前端一旦写出那条路径，这里就红，不需要再单开一条禁令。
    """
    literals = set(re.findall(r'["`](/[a-z_/]+)', ts_code("src/lib/acp.ts")))
    assert len(literals) >= 5, f"取到的路径字面量太少（{sorted(literals)}）⇒ 形状改了要连着改这条正则"
    paths = public_paths()
    unknown = sorted(item for item in literals if not any(path.startswith(item) for path in paths))
    assert not unknown, f"前端调了公开面上没有的路径：{unknown}"


def test_the_dev_proxy_covers_every_root_the_public_app_exposes():
    """proxy 表漏一条的后果只出现在 dev：那个面板永远转圈，而生产同源时一切正常。"""
    config = read("vite.config.ts")
    listed = set(re.findall(r'"(/[a-z_]+)"', config.split("const API_ROOTS")[1].split("];")[0]))
    assert listed, "一条根路径都没取到 ⇒ 这一发扫的是空气（第一次就踩在这里：注释里也提这个名字）"
    roots = {path.split("/")[1] for path in public_paths()}
    missing = sorted(f"/{root}" for root in roots if f"/{root}" not in listed)
    assert not missing, f"`vite.config.ts` 的 API_ROOTS 少了这些根：{missing}"
    extra = sorted(root for root in listed if root.lstrip("/") not in roots)
    assert not extra, f"API_ROOTS 里有公开面已经没有的根（漂移的另一半）：{extra}"


def test_the_dev_proxy_targets_the_same_port_the_launcher_listens_on():
    """端口写两处的话，改了 `serve.py` 而 dev 还指着旧端口 —— 那时坏的是"开发时全 404"这种最难归因的形态。"""
    backend = re.search(r'const BACKEND = "http://127\.0\.0\.1:(\d+)"', read("vite.config.ts"))
    assert backend, "取不到 BACKEND 常量：改名了要同时改这条判据"
    assert int(backend.group(1)) == serve.DEFAULT_PORT, (
        f"dev 指着 {backend.group(1)} 而 `serve.DEFAULT_PORT` 是 {serve.DEFAULT_PORT}"
    )


def test_the_dev_server_also_stays_on_loopback():
    """宪法那句"本地（127.0.0.1）"管的是不止后端：dev 服务器一旦绑 0.0.0.0，代理出去的是同一批无鉴权写端点。"""
    config = read("vite.config.ts")
    assert 'host: "127.0.0.1"' in config, "dev 服务器必须显式只绑环回"
    assert "0.0.0.0" not in config and "host: true" not in config


# ---------------------------------------------------------------- 二、头部字面量


def test_the_client_sends_the_same_principal_header_the_backends_read():
    client = read("src/lib/acp.ts")
    declared = re.search(r'PRINCIPAL_HEADER = "([^"]+)"', client)
    assert declared, "前端没把请求头名字集中成一个可注入的字面量"
    assert declared.group(1) == PRINCIPAL_HEADER, (
        f"两侧的头名不同形：前端 {declared.group(1)!r} vs 后端 {PRINCIPAL_HEADER!r}"
    )


# ---------------------------------------------------------------- 三、令牌单一来源

#: 浅色主题的落地值。owner 2026-10-21 点名了前六个（`canvas` / `panel` / `row` / `row-alt` /
#: `ink` / `ink-muted`）与 accent。
#:
#: 2026-10-25 笔 AE（finesse craft floor）重签过一次，把冷灰往冷板岩挪、accent 换成同族深青。
#:
#: **2026-10-27 owner：「浅色模式看着不顺眼，不必纠结于白色」⇒ 整套换成暖纸，`panel` 不再是纯白。**
#: 病根不是某一格偏了，是**明度阶梯是平的**：旧值 `canvas-hi` 与 `panel` 都是 `#FFFFFF`，
#: 而 `canvas` 只比它深一点点（`#F2F5FA`）—— 顶栏、卡片、画布糊成一片白，屏上没有"层"。
#: 现在三层是 `canvas #EDE7DC`（砂）< `canvas-hi #F7F3EA`（米）< `panel #FCFAF4`（纸），
#: 正文换成暖墨 `#221C14`，accent 收到深青 `#0B5F66`（与深色那支青同族，仍是同一套身份）。
#: 22 个值全部重算过 AA：最低一格是 `warn` on `row-alt` 5.18（旧值 5.54），没有靠降对比度换观感。
LIGHT_PALETTE_PINNED = {
    "canvas": "#EDE7DC",
    "panel": "#FCFAF4",
    "row": "#F2EDE4",
    "row-alt": "#E6DFD2",
    "ink": "#221C14",
    "ink-muted": "#5E554A",
    "accent": "#0B5F66",
    # 2026-10-26 M1：agent 身份 6 档（名册序号轮转）。浅色下必须比底深到能当文字色（AA 4.5）。
    "agent-1": "#1D4E89",
    "agent-2": "#5A3C9E",
    "agent-3": "#9C2A63",
    "agent-4": "#3E6B1E",
    "agent-5": "#0E6B63",
    "agent-6": "#8A4A16",
}


def token_document() -> dict:
    return json.loads(read("src/styles/tokens.json"))


def tokens(theme: str = "dark") -> dict[str, str]:
    """按主题取色表。顶层 `color` 是**默认主题**（dark），浅色是它上面的一层完整覆盖（`light.color`）。"""
    document = token_document()
    if theme == "dark":
        return document["color"]
    light = document.get("light", {}).get("color")
    assert light, "tokens.json 少了 `light.color` ⇒ 浅色主题没有第二套色值可对读"
    return light


def palettes() -> dict[str, dict[str, str]]:
    return {theme: tokens(theme) for theme in ("dark", "light")}


def theme_values() -> dict[str, str]:
    css = read("src/index.css")
    block = re.search(r"@theme \{(.*?)\}", css, re.S)
    assert block, "index.css 里没有 @theme 块 ⇒ Tailwind v4 拿不到色，页面会退回默认色板"
    # 键名允许带数字（`accent-2`）：这一族只认 `[a-z-]` 时，带数字的那个键会**整行匹配不上**，
    # 于是 #6 的两侧对读看不见它 —— 不是放宽比较，是把比较的覆盖面补回它声称的那一份。
    return {
        name: value.strip()
        for name, value in re.findall(r"--color-([a-z0-9-]+):\s*([^;]+);", block.group(1))
    }


def light_theme_values() -> dict[str, str]:
    """读 `[data-theme="light"]` 那个**纯变量块**（不带后代选择器的那一块）。

    浅色的局部覆盖（`.glass` / `.office-*`）选择器在 `]` 后面还有东西，不匹配这里 ——
    这一格要的是"整套色值"，不是"某块皮肤改了一个角"。
    """
    css = read("src/index.css")
    block = re.search(r'\[data-theme="light"\]\s*\{([^}]*)\}', css)
    assert block, 'index.css 里没有 `[data-theme="light"] { … }` 变量块 ⇒ 切到浅色只有局部规则、没有全套令牌'
    return {
        name: value.strip()
        for name, value in re.findall(r"--color-([a-z0-9-]+):\s*([^;]+);", block.group(1))
    }


def test_the_css_theme_reproduces_the_token_file_exactly():
    """两套色值各对读一遍：`@theme` = dark，`[data-theme="light"]` = light。

    只对读一套的后果是"浅色块里某个值写错，AA 判据量的却是 JSON 里那一份"—— 两边各自自洽，
    屏上那层却是第三种颜色。两遍都过，才谈得上"改一处就换色"。
    """
    for theme, actual in (("dark", theme_values()), ("light", light_theme_values())):
        declared = tokens(theme)
        assert sorted(actual) == sorted(declared), (
            f"{theme} 主题的 CSS 与 tokens.json 不同集：只在 CSS {sorted(set(actual) - set(declared))} / "
            f"只在 JSON {sorted(set(declared) - set(actual))}"
        )
        drift = [name for name in declared if actual[name].lower() != declared[name].lower()]
        assert not drift, f"{theme} 主题里同一个语义色在两侧写成两个值：{drift}"
    assert theme_values() != light_theme_values(), (
        "浅色块与默认块逐值相同 ⇒ 切主题只是换了一个 data-theme 字符串，屏上什么都没变"
    )


def test_the_light_palette_is_the_one_the_owner_specified():
    """浅色不是"把深色反过来"：owner 点名的六个值与深蓝 accent 必须逐字落位。

    这一条与上一条互补：上一条管 CSS 有没有抄错 JSON，这一条管 JSON 有没有被悄悄换成另一套
    （比如把 `ink-muted` 留在深色的亮灰上 —— 白底上几乎看不见，而两套 token 各自都"内部一致"）。
    """
    light, dark = tokens("light"), tokens("dark")
    assert set(light) == set(dark), (
        f"两套色表不同键集：只在浅色 {sorted(set(light) - set(dark))} / 只在深色 {sorted(set(dark) - set(light))}"
    )
    assert len(light) == 22, f"色表应当 22 个令牌（16 + 2026-10-26 的 agent-1..6），现读 {len(light)}"
    drift = {
        name: (light.get(name), value)
        for name, value in LIGHT_PALETTE_PINNED.items()
        if light.get(name, "").lower() != value.lower()
    }
    assert not drift, f"浅色色值没有按 owner 指定的落位（现读 / 应为）：{drift}"


def test_no_token_is_declared_and_never_used():
    """死令牌比少一个令牌更糟：它让"改这一处就换色"这句承诺变成假的（口径同 StudyPal 的死类检查）。"""
    used_in = (
        "\n".join(path.read_text(encoding="utf-8") for path in source_files())
        + read("src/index.css")
        # studio.css 把产品令牌映射成 --node/--a1 这些工作区别名：它也是消费者，
        # 不把它算进来，"令牌有人用"这件事就会漏掉整整一层皮肤。
        + read("src/styles/studio.css")
    )
    dead = [
        name
        for name in tokens("dark")
        if f"var(--color-{name})" not in used_in
        and not re.search(rf"\b(?:bg|text|border|from|to)-{re.escape(name)}\b", used_in)
    ]
    assert not dead, f"这些令牌没有任何地方用它们：{dead}"


def glass_contract(theme: str = "dark") -> dict:
    document = token_document()
    block = document.get("glass") if theme == "dark" else document.get("light", {}).get("glass")
    assert block, (
        f"tokens.json 少了 {theme} 的 `glass` 块 ⇒ 玻璃的配方（tint / alpha / 底 / 描边）没有产地，"
        "只能散写在每张卡片上"
    )
    return block


def glass_contracts() -> dict[str, dict]:
    return {theme: glass_contract(theme) for theme in ("dark", "light")}


def css_variable(block: str, name: str) -> str:
    hit = re.search(rf"--{re.escape(name)}:\s*([^;]+);", block)
    assert hit, f"取不到 `--{name}` ⇒ 玻璃的这件东西换了产地，这条判据要跟着改"
    return hit.group(1).strip()


def root_glass_block() -> str:
    css = read("src/index.css")
    block = re.search(r":root\s*\{([^}]*--glass-fill:[^}]*)\}", css)
    assert block, "取不到 `:root` 里的玻璃变量块（--glass-fill / --glass-shadow …）"
    return block.group(1)


def light_glass_block() -> str:
    css = read("src/index.css")
    block = re.search(r'\[data-theme="light"\]\s*\{([^}]*--glass-fill:[^}]*)\}', css)
    assert block, "浅色块里没有 `--glass-fill` ⇒ 玻璃没有跟着主题换配方"
    return block.group(1)


def _composite(foreground: str, alpha: float, background: str) -> str:
    """`color-mix(in srgb, <前景> <alpha>% , transparent)` 叠在一个平色底上的等效色。

    这里按 sRGB 分量直混 —— 与 CSS 那句 `in srgb` 同一个语义。写成 `in oklab` 就不是 alpha 混合，
    下面那条对读当场红（这正是规范点名的那个抄错形状）。
    """
    fg, bg = foreground.lstrip("#"), background.lstrip("#")
    return "#" + "".join(
        f"{round(int(fg[i : i + 2], 16) * alpha + int(bg[i : i + 2], 16) * (1 - alpha)):02x}" for i in (0, 2, 4)
    )


def fill_recipe(contract: dict) -> str:
    return (
        f"color-mix(in srgb, var(--color-{contract['tint']}) "
        f"{float(contract['alpha']) * 100:g}%, transparent)"
    )


def border_recipe(contract: dict) -> str:
    border = contract["border"]
    return (
        f"color-mix(in srgb, var(--color-{border['color']}) "
        f"{float(border['alpha']) * 100:g}%, transparent)"
    )


def test_the_glass_layers_are_the_declared_composite_chain():
    """深色那条链必须逐层复算；浅色的覆盖由下面那条单独量（它不可能是同一条链，见那条 docstring）。

    这条是节 四 的前提，不是它的补充。节 四 只许在平色 hex 上量对比度，而玻璃层的真实底色是叠出来的 ——
    如果那三个令牌不是叠出来的结果，"AA 绿了"量的就不是屏上那一层（口径同笔 AC 那句"不许靠不透明度买 AA"）。
    叠出来的链与令牌逐字相等，于是"声明链"这一列才真的可复算；规范 §1.3 表 B 那条"最坏链更亮"由下面那条管。

    顺带钉四件抄错就红的事：层数只许 3（第 4 层 = 令牌之外还有一层玻璃，链就断了）；
    配方与描边各只有一个产地（`:root` 的变量块）；`@utility glass` 的 fallback 必须**在**变量之前；
    `glass-raised` 不许自己重写底或描边（它是叠加层，不是第二份配方）。
    """
    palette = tokens("dark")
    contract = glass_contract("dark")
    alpha = float(contract["alpha"])
    tint, base, layers = contract["tint"], contract["base"], contract["layers"]
    for name in (tint, base, *layers):
        assert name in palette, f"`glass` 块引的令牌名不在色表里：{name}"
    assert len(layers) == 3, f"玻璃只许叠 1/2/3 层（不变式：承载文字的表面不许超过令牌所代表的层数），现读 {len(layers)}"

    current, drift = palette[base], []
    for depth, layer in enumerate(layers, start=1):
        current = _composite(palette[tint], alpha, current)
        if current != palette[layer].lower():
            drift.append((depth, layer, current, palette[layer]))
    assert not drift, f"这些层不是由 tint@{alpha} 叠在 {base} 上算出来的（叠出色 / 令牌色）：{drift}"

    css = read("src/index.css")
    root = root_glass_block()
    recipe, border = fill_recipe(contract), border_recipe(contract)
    assert css_variable(root, "glass-fill") == recipe, (
        f"玻璃层不是用 `glass` 块的配方写的（要么换了 tint、要么换了 alpha、要么不是 in srgb）："
        f"{css_variable(root, 'glass-fill')!r}"
    )
    assert css_variable(root, "glass-border-color") == border, (
        f"玻璃描边不是 `glass` 块那条 ink 配方：{css_variable(root, 'glass-border-color')!r}"
    )
    assert css.count(recipe) == 1, (
        f"深色配方 `{recipe}` 在 index.css 里写了 {css.count(recipe)} 份 ⇒ tint/alpha 有了第二个产地，"
        "改一处另一处照旧生效，而那条叠层链会安静地对不上令牌"
    )
    assert css.count(border) == 1, f"深色描边配方写了 {css.count(border)} 份 ⇒ 描边有了第二个产地"

    utility = re.search(r"@utility glass \{([\s\S]*?)\}", css)
    assert utility, "取不到 @utility glass ⇒ 玻璃换了产地，这条判据要跟着改"
    body = utility.group(1)
    fallback = body.find(f"background-color: var(--color-{layers[0]})")
    mixed = body.find("background-color: var(--glass-fill)")
    assert fallback != -1, "fallback 那行丢了：不支持 color-mix() 的浏览器会拿到别的底，AA 就不是量过的那个数"
    assert mixed != -1 and fallback < mixed, "fallback 没有排在变量之前 ⇒ 它会被自己覆盖，等于没有 fallback"
    assert "border: 1px solid var(--glass-border-color)" in body, "描边没有走那个唯一的变量"
    assert "box-shadow: var(--glass-shadow)" in body, "玻璃的阴影没有走主题变量 ⇒ 浅色会拿到深色那层纯黑"

    raised = re.search(r"@utility glass-raised \{([\s\S]*?)\}", css)
    assert raised, "取不到 @utility glass-raised"
    assert "background-color" not in raised.group(1) and "border:" not in raised.group(1), (
        "抬高态自己重写了底或描边 ⇒ 它是第二份配方而不是叠加层（粘 `glass-raised` 必须连着 `glass`）"
    )
    assert "box-shadow: var(--glass-raised-shadow)" in raised.group(1), (
        "抬高态的阴影没有走主题变量 ⇒ `hover:glass-raised` 在浅色下会拿到纯黑阴影"
    )


def test_the_light_glass_only_lightens_the_backdrop_and_keeps_declared_bases_measured():
    """浅色的玻璃不可能与深色共用"tint@alpha 叠三层"那条链 —— 玻璃只会把底往 panel 上提亮。

    `panel` 是那张"纸"（2026-10-27 起是米白 `#FCFAF4`，不再是纯白），它**不是** canvas 上叠一层的结果，
    所以这一档改成两条可复算的话：
    ① 玻璃的合成色落在 `[base, opaque]` 之间（只许提亮，不许比不透明的 panel 还亮）；
    ② 最坏的底仍是它下面那层光晕，节 四 把 `glow` 与 `glass-over-glow` 都量一遍，所以
       "声明的四个平色 + 两个合成色"覆盖了屏上那一层，不是另一套口径。
    另外钉住：明度阶梯必须朝上（canvas < canvas-hi < panel，冷白那版前两层相等、阶梯是平的，
    屏上糊成一片白）；阴影用**带色调的**柔灰而不是纯黑（纯黑落在纸上会脏成一圈灰晕）；
    描边比深色更实（10% vs 8%），否则纸色卡片在砂色画布上没有边界。
    """
    palette = tokens("light")
    contract = glass_contract("light")
    alpha = float(contract["alpha"])
    tint, base, opaque = contract["tint"], contract["base"], contract["opaque"]
    for name in (tint, base, opaque):
        assert name in palette, f"浅色 `glass` 块引的令牌名不在色表里：{name}"
    assert 0 < alpha < 1, f"浅色玻璃的 alpha 不在 (0,1) 里：{alpha}"

    # 明度阶梯：画布（砂）< 顶栏（米）< 卡片（纸）。这一条是 2026-10-27 换色的**根因判据** ——
    # 旧值的 canvas-hi 与 panel 都是 #FFFFFF，三层里有两层是同一个数，屏上没有"层"。
    ladder = [
        _relative_luminance(palette["canvas"]),
        _relative_luminance(palette["canvas-hi"]),
        _relative_luminance(palette[opaque]),
    ]
    assert ladder == sorted(ladder) and len(set(ladder)) == 3, (
        f"浅色的三层明度没有拉开（canvas / canvas-hi / {opaque} = {ladder}）⇒ 顶栏、卡片、画布会糊成一片"
    )

    block = light_glass_block()
    recipe, border = fill_recipe(contract), border_recipe(contract)
    assert css_variable(block, "glass-fill") == recipe, (
        f"浅色玻璃没有用 `light.glass` 的配方：{css_variable(block, 'glass-fill')!r}"
    )
    assert css_variable(block, "glass-border-color") == border, (
        f"浅色玻璃描边不是 `light.glass` 那条 ink 配方：{css_variable(block, 'glass-border-color')!r}"
    )
    assert read("src/index.css").count(recipe) == 1, f"浅色配方 `{recipe}` 写了多份 ⇒ 有了第二个产地"
    assert css_variable(root_glass_block(), "glass-fill") != recipe, "浅色与深色用了同一条玻璃配方"

    composite = _composite(palette[tint], alpha, palette[base])
    assert _relative_luminance(composite) >= _relative_luminance(palette[base]), (
        f"浅色玻璃把底压暗了（{composite} < {palette[base]}）⇒ 它不再是「白光」，AA 的底也不对"
    )
    assert _relative_luminance(composite) <= _relative_luminance(palette[opaque]), (
        f"浅色玻璃比不透明的 panel 还亮（{composite} > {palette[opaque]}）⇒ fallback 与实底分家了"
    )

    assert "rgb(0 0 0" not in block, "浅色玻璃的阴影还在用纯黑 ⇒ 纸上会脏成一圈灰晕（owner 第四步）"
    # 阴影必须是**带色调的**柔灰：三个通道相等就是中性灰，落在砂色画布上会显出一层冷调。
    shadow_colors = re.findall(r"rgb\((\d+) (\d+) (\d+) /", block)
    assert shadow_colors, "浅色玻璃的阴影没有走 `rgb(r g b / a)` 这种写法"
    for triple in shadow_colors:
        assert len(set(triple)) > 1, f"浅色玻璃的阴影是中性灰 {triple} ⇒ 落在砂色上会显冷"
    assert css_variable(block, "glass-shadow") != css_variable(root_glass_block(), "glass-shadow"), (
        "浅色没有换玻璃阴影 ⇒ 纯黑的大面积投影还在"
    )
    assert css_variable(block, "glass-raised-shadow") != css_variable(
        root_glass_block(), "glass-raised-shadow"
    ), "浅色没有换抬高态阴影"


def aura_rule(theme: str) -> str:
    css = read("src/index.css")
    pattern = r"@utility aura \{([\s\S]*?)\}" if theme == "dark" else r'\[data-theme="light"\] \.aura \{([\s\S]*?)\}'
    block = re.search(pattern, css)
    assert block, f"取不到 {theme} 的 aura 规则 ⇒ 光晕换了产地，这条判据要跟着改"
    return block.group(1)


def aura_stop(theme: str) -> tuple[str, float]:
    """从 aura 规则里取出那个彩色 stop 的 `(令牌名, alpha)`。两个 stop 的色都必须是令牌写的。"""
    body = aura_rule(theme)
    assert "var(--color-canvas-hi)" in body, f"{theme} 的主光晕峰值底没用令牌 ⇒ canvas-hi 成了死数"
    stop = re.search(r"color-mix\(in srgb, var\(--color-([a-z0-9-]+)\) ([\d.]+)%, transparent\)", body)
    assert stop, f"{theme} 的第二个 stop 不是 `color-mix(in srgb, var(--color-…) N%, transparent)`：{body.strip()[:160]}"
    assert not re.search(r"rgb\(|#[0-9a-fA-F]{6}", body), f"{theme} 的光晕里出现了写死的 rgb() 或 hex"
    return stop.group(1), float(stop.group(2)) / 100


def test_the_glow_stop_is_recomputable_from_tokens():
    """两套主题各量一次，方向相反：深色光晕**提亮**底（所以最坏底比 canvas-hi 更亮），
    浅色光晕**压暗**底（所以最坏底比纯白的 canvas-hi 更暗）。写死任何一个 rgb() 都会让这一格失去复算能力。

    规范 §1.1 的第 2 条不变式在深色里是 `max(canvas-hi, glow)`；在浅色里方向反过来，最坏的是
    `min(canvas-hi, glow)` —— 同一条不变式、不同的比较方向，节 四 两边都按这条取合成底。
    """
    for theme, palette in palettes().items():
        name, alpha = aura_stop(theme)
        assert name == "accent", f"{theme} 的主光晕用的是 {name}，而表 B 那一列是按 accent 算的"
        glow = _composite(palette[name], alpha, palette["canvas"])
        if theme == "dark":
            assert _relative_luminance(glow) > _relative_luminance(palette["canvas-hi"]), (
                f"深色光晕合成色 {glow} 不比声明底 {palette['canvas-hi']} 亮 ⇒ 规范 §1.3 的表 B 失去存在理由"
            )
        else:
            assert _relative_luminance(glow) < _relative_luminance(palette["canvas-hi"]), (
                f"浅色光晕合成色 {glow} 不比纯白底面暗 ⇒ 它没有制造出任何层次，最坏底也不再是它"
            )
    assert aura_stop("light")[1] < aura_stop("dark")[1], (
        "浅色光晕的透明度没有比深色更低（owner 第四步：浅色下灯光/光晕要调低）"
    )


def test_no_animation_can_leave_content_invisible():
    """动效不许把内容停在"全透明"那一帧 —— 这一条是量出来的，不是审美（笔 AD 第 2 笔）。

    原形状：`@keyframes card-in { from { opacity: 0; … } }`，而 `glass` 带着 `animation: card-in 180ms`。
    文档切到后台时 Chrome 冻结动画、`animation-fill-mode` 默认 `none` ⇒ 元素就停在首帧。
    实测两张 Agent 卡：`opacity=0` / `animationPlayState=running` / 宽高正常 —— 内容在 DOM 里、屏上没有，
    而"Agent 栏"恰好是**帧到了才挂出卡片**的那一列，所以它第一个撞上。
    停在 `translateY(4px)` 只是偏一点，停在 0 透明度是造假级别的故障，所以这里只禁后者。
    反向的一半也钉：呼吸灯那一条**必须**能动 opacity（它就是靠明暗说话），所以禁的是"停靠点里有 0"。
    """
    css = read("src/index.css")
    # 与 `ts_code()` 同一个道理：禁令类判据只看代码面。这条判据禁的是**声明**，
    # 而这一节自己的注释里就得写出"opacity"来说明为什么禁它（G6 反向对照钉这一格）。
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    frames = dict(re.findall(r"@keyframes ([\w-]+) \{([\s\S]*?)\n\}", css))
    # 2026-10-26 起只剩两张：`flow`（DAG 活跃边的那条虚线流动）随依赖图一起退休，
    # 中栏换成接力线之后没有第二类连线可动。加新关键帧要连同规范 §2.3 一起改。
    assert set(frames) == {"lamp-breathe", "card-in"}, (
        f"关键帧名单变了（要连同规范 §2.3 / v4 §5.3 一起改）：{sorted(frames)}"
    )
    assert "opacity" not in frames["card-in"], (
        "入场动画又带上 opacity ⇒ 动画被冻结的那些元素会整块看不见（2026-10-20 实测过的那一发）"
    )
    stops = [float(item) for item in re.findall(r"opacity: ([\d.]+)", frames["lamp-breathe"])]
    assert stops and min(stops) > 0, f"呼吸灯的停靠透明度里有 0 ⇒ 灯会整个消失：{stops}"
    glass = re.search(r"@utility glass \{([\s\S]*?)\n\}", css)
    assert glass and "animation: card-in" in glass.group(1), "卡片不再引用入场动画 ⇒ 这条判据与规范 §2.3 分家了"

    # 笔 AM：人物与机柜换成 SVG 后，CSS 一侧不再有敲手动效，也不再有第二处 `animation-delay`。
    # 两条都按"全仓为零"量：留着旧关键帧 = 屏上两套动效叠着演，留着 delay = 旧的使用点没删干净。
    assert "office-tap" not in css, "办公室的 CSS 敲击动效还留着 ⇒ 与 SVG 资产重叠两套动效"
    assert "animation-delay:" not in css, "全仓不再需要 animation-delay（SVG 动效由 SMIL 自己错峰）"

    # 2026-10-29：禁令扩到工作室那一屏。唯一豁免是打字三点（`.typing i:nth-child`）——
    # 它们是三颗 3px 点，不承载内容，只做"正在输入"的节奏；新增任何 delay 都算绕过。
    # 光环的相位错开走不同的 animation-duration，不用负延迟。
    studio = re.sub(r"/\*.*?\*/", "", read("src/styles/studio.css"), flags=re.S)
    stray = [
        line.strip()
        for line in studio.splitlines()
        if "animation-delay" in line and ".typing i:nth-child" not in line
    ]
    assert not stray, (
        "studio.css 里出现了打字三点之外的 animation-delay：\n  " + "\n  ".join(stray) + "\n"
        "相位错开请用不同的 animation-duration，不要用负延迟"
    )
    assert sorted(re.findall(r"animation-delay:\s*([^;}\n]+)", studio)) == [".16s", ".32s"], (
        "打字三点的延迟值变了？它们是这条禁令唯一的豁免，改值要连同这段注释一起改"
    )

    # SMIL 与 CSS 动画共享同一条硬约束：**不许把内容停在透明度 0**。
    # 浏览器冻结 SMIL 时同样停在当前帧，所以每条 `attributeName="opacity"` 的 `<animate>`
    # 停靠值最小值必须 > 0。位移/虚线偏移的 `0` 不是透明态，按属性名分开量。
    # 全景那套 SMIL 资产（机柜 / 管道 / 气泡 / 招聘 / 摸鱼）随办公室视图一起退休：
    # 盘上不再有 `src/components/svg`，所以"停靠值里有 0"这一族判据没有对象了。
    assert not (WEB / "src/components/svg").exists(), (
        "退役的 SVG 资产还在盘上 ⇒ 它们可能被重新挂上，而停靠值与事件面已经没人看着了"
    )


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_value: str) -> float:
    digits = hex_value.lstrip("#")
    assert len(digits) == 6, f"对比度只按 6 位十六进制量（收到 {hex_value!r}）"
    red, green, blue = (int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    first, second = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def test_every_text_color_clears_aa_on_every_row_background():
    """两套主题各量一遍，且不止四种声明底：最坏光晕与玻璃盖在它上面的合成底也要量。

    只量页面背景的那次"AA 通过"不算数；只量声明的四个平色也不够 —— 玻璃是半透明的，
    卡片上的字落在"光晕 + 玻璃"的合成色上。这里的底因此是 4 张声明底 + 2 张合成底，逐主题、
    逐前景色组合量。深色取更亮的那个极端，浅色取更暗的那个极端（方向由节 三 的 aura 判据钉住）。
    """
    contract = token_document()["contrast"]
    minimum = float(contract["minimum"])
    failures = []
    for theme, palette in palettes().items():
        name, aura_alpha = aura_stop(theme)
        glow = _composite(palette[name], aura_alpha, palette["canvas"])
        glass = glass_contract(theme)
        glass_over_glow = _composite(palette[glass["tint"]], float(glass["alpha"]), glow)
        backgrounds = [(label, palette[label]) for label in contract["backgrounds"]]
        backgrounds += [("aura-glow", glow), ("glass-over-glow", glass_over_glow)]
        for foreground in contract["foregrounds"]:
            for background_name, background in backgrounds:
                ratio = contrast_ratio(palette[foreground], background)
                if ratio < minimum:
                    failures.append((theme, foreground, background_name, round(ratio, 3)))
    assert not failures, f"这些组合低于 AA {minimum}：{failures}"


def test_the_inverted_pair_on_the_accent_clears_aa_too():
    """按钮是"深字压亮底"（深色）/ "白字压深蓝"（浅色），与正文那一族正相反。

    漏了它，最容易点的那个控件反而是唯一没过 AA 的。两套主题各量一遍。
    """
    contract = token_document()["contrast"]["inverted"]
    for theme, palette in palettes().items():
        ratio = contrast_ratio(palette[contract["foreground"]], palette[contract["background"]])
        assert ratio >= float(contract["minimum"]), (
            f"{theme} 主题的 {contract['foreground']} on {contract['background']} 只有 {ratio:.3f}"
        )


# ---------------------------------------------------------------- 五、构建面的本机约束


def _dependency_version(section: str, package: str) -> str:
    declared = json.loads(read("package.json"))[section]
    assert package in declared, f"{package} 不在 {section} 里（升/降都要连着这条判据一起改）"
    return declared[package]


def test_vite_stays_on_major_seven_and_plugin_react_on_major_five():
    """这两条钉的是**本机 SAC**，不是审美：Vite 8 起的 rolldown 原生 .node 会被 App Control 拦下来，
    而"能装上报错、构建时才炸"是那次实测的形态。哪天环境变了，这里先红，改的人再决定升不升。
    """
    vite, plugin = _dependency_version("devDependencies", "vite"), _dependency_version(
        "devDependencies", "@vitejs/plugin-react"
    )
    assert vite.split(".")[0] == "7", f"vite 必须是 7.x（现读 {vite}）"
    assert plugin.split(".")[0] == "5", f"@vitejs/plugin-react 必须是 5.x（现读 {plugin}）"
    assert "^" not in vite and "~" not in vite, "构建工具按精确版本钉：范围号会在下一次装出不同的构建器"


def test_tailwind_scan_surface_is_pinned_inside_web():
    """`source(none)` 不是风格：自动探测会走出 `web/` 把仓库里的 .md/.ps1 当模板扫进类表（实测过一轮）。"""
    css = read("src/index.css")
    assert 'source(none)' in css.split("\n")[0] or "source(none)" in css, "Tailwind v4 必须关掉自动探测"
    assert re.search(r"@source \"\./\*\*/\*\.\{ts,tsx\}\"", css), "@source 要钉在 src 下的 ts/tsx"


# ---------------------------------------------------------------- 六、忽略面与门禁面同批


@pytest.mark.parametrize("entry", ["node_modules/", "web/dist/"])
def test_the_ignored_frontend_trees_are_declared_once(entry: str):
    """这两条与 §7 的 `--exclude-dir` 必须同时存在：只在一处忽略，另一处就会把 200 个 package.json 扫进门禁。"""
    lines = [line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert entry in lines, f".gitignore 少了 {entry}"


def test_the_naming_gate_excludes_the_same_two_dirs_the_ignorer_lists():
    """两个载体、同一张表：读 §7 的 exclude-dir 与 .gitignore 比集合（口径同命名门禁的 shell/python 双载体）。

    笔 AD 把这一条**改准**（它原先是死的）：旧写法先 `re.search(r"--exclude-dir=\\S+")` 只取到**第一个**
    匹配（`--exclude-dir=.venv`），再在**那一小段**里找带引号的形式 —— 而活命令写的是不带引号的
    `--exclude-dir=.venv` ⇒ 集合恒空 ⇒ 下面那个 for 一次都没跑过，"两边都得有"这句承诺等于没量。
    现在按 `scripts/guard_naming.sh` 同一条规则取**那条活语句**（从 `grep -rniE` 起、到不以 `\\` 续行为止），
    再从中洗出全部 `--exclude-dir=`，引号可有可无。取活语句而不是取整节，是为了不吃进 §7 正文里
    那些"原写 --exclude=…"的历史留痕（脚本头注释里写着同一句理由）。
    """
    section = (REPO / "config" / "params-ref.md").read_text(encoding="utf-8").split("## 7.")[1].split("## 8.")[0]
    lines = section.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("grep -rniE")), None)
    assert start is not None, "§7 里找不到以 grep -rniE 起首的门禁语句"
    statement = []
    for line in lines[start:]:
        statement.append(line)
        if not line.rstrip().endswith("\\"):
            break
    body = " ".join(statement)
    excluded = {entry.rstrip("/") for entry in re.findall(r"--exclude-dir='?([A-Za-z0-9_.-]+)'?", body)}
    assert excluded, "活语句里一个 --exclude-dir 都没洗出来 ⇒ 这条判据又变成空转（它已经死过一次）"

    # `.git` 是版本库自己的元数据目录：git 永不跟踪它，所以它**不该**在 `.gitignore` 里 ——
    # 这一格是"把死判据改活"之后当场撞到的（旧写法集合恒空，从没跑过这个循环）。
    # 名单只许一条且点名理由；其余每一项都必须有 `.gitignore` 对应项（活性对照 = 往活语句里塞一个
    # `.not-in-ignorer` 假排除项，这条必须红）。
    never_tracked = {".git"}
    lines = [line.strip().rstrip("/") for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    for name in sorted(excluded - never_tracked):
        assert any(entry == name or entry == f"web/{name}" for entry in lines), (
            f"§7 排除了 {name}/ 而 .gitignore 没有对应项 ⇒ 它会进版本库"
        )
    assert excluded - never_tracked, "洗出来的排除项只剩豁免名单 ⇒ 这条判据又空转了"


# ---------------------------------------------------------------- 七、审批与活动流的两侧口径（笔 G）


def _ts_array(name: str) -> list[str]:
    body = read("src/api/client.ts")
    block = re.search(rf"{name} = \[(.*?)\]", body, re.S)
    assert block, f"取不到 TS 侧的 {name} 常量"
    return re.findall(r'"([A-Za-z_]+)"', block.group(1))


def test_the_board_can_only_send_a_human_channel():
    """`resolution_channel` 域里有三个值，`AUTO` 由规则表免确认独占 —— 页面上不许出现能发出它的位置。

    两侧都量：常量本身必须在 `repository.HUMAN_CHANNELS` 里（改坏 ⇒ 红），
    且 `"AUTO"` 这个字面量在整棵 `web/src` 里一次都不许出现（写进任何一处都可能被凑成一次提交）。
    """
    declared = re.search(r'DASHBOARD_CHANNEL = "([A-Z_]+)"', read("src/lib/acp.ts"))
    assert declared, "面板的通道字面量没集中成一个可注入的常量"
    assert declared.group(1) in repo.HUMAN_CHANNELS, f"{declared.group(1)} 不是人可提交的通道：{sorted(repo.HUMAN_CHANNELS)}"
    everywhere = "\n".join(ts_code(f"src/{path.relative_to(SRC).as_posix()}") for path in source_files())
    assert '"AUTO"' not in everywhere and "AUTO," not in everywhere, "前端出现了 AUTO ⇒ 看板就能替规则表免确认，那是另一条授权路径"


def test_the_stream_is_opened_without_a_second_interval_or_cursor():
    """一条 `EventSource` 只许带"我是浏览器"这一件事：间隔归 `runtime.yaml`，游标归服务端接的 `Last-Event-ID`。

    留出 `poll_ms` 就有了第二个间隔来源（面板改了值而这条连接还按旧的走）；
    前端自己存游标、自己发 `Last-Event-ID` 就有了第二份游标（与 `web.py` 那份口径会漂）。
    """
    client = ts_code("src/lib/acp.ts")
    stream_line = re.search(r'new EventSource\("([^"]+)"\)', client)
    assert stream_line, "取不到 EventSource 的连接串 ⇒ 形状改了要连着改这条"
    assert stream_line.group(1) == "/events/stream", f"连的是 {stream_line.group(1)}"
    assert "?" not in stream_line.group(1) and "poll_ms" not in client, "前端出现了 poll_ms ⇒ 间隔有了第二份"
    assert "limit" not in client.split("openActivityStream")[1], (
        "浏览器那条流不该带 limit（那是「推满就收」的判据形状）"
    )
    assert "last-event-id" not in client.lower(), "前端自己发 Last-Event-ID ⇒ 游标有了第二份"


def test_the_frame_event_name_matches_the_server():
    """`addEventListener` 的名字必须等于 `api/web.py::STREAM_EVENT`。
    名字一漂，连接显示"已连接"而页面一行不动 —— 那正是最难归因的假活。"""
    declared = re.search(r'STREAM_EVENT = "([^"]+)"', read("src/lib/acp.ts"))
    assert declared and declared.group(1) == web.STREAM_EVENT, f"{declared and declared.group(1)!r} vs {web.STREAM_EVENT!r}"


# ---------------------------------------------------------------- 八、扫描面自证


def test_the_old_live_page_is_gone_and_old_links_get_a_notice():
    """2026-10-29 owner 指令：`?live` 整页实时看板从文件夹删除，真链路回到项目工作室。

    文件不在 + `StudioView` 不再 import/render `LiveView`，两条一起断；
    旧书签仍允许打开，但必须有一句显式提示，不能静默换成另一屏。

    2026-10-30（owner「每次刷新都会出现影响观感」）：这句话的载体从 `StudioView` 里那两个
    各读一遍 `location.search` 的 effect 搬进 `src/lib/retiredLinks.ts`（纯函数，只进出字符串，
    所以 vitest 能直接跑它 —— 行为判据在 `web/tests/retiredLinks.test.ts`），并且**说完就把退役
    参数从地址栏里摘掉**。所以这一格跟着换成三条结构断言：壳接了载体、摘地址栏那一步还在、
    载体真的认这两类参数。留一条"只许一个 `say(` 调用点"：两类参数各说一句，就是叠两张同款卡。
    """
    assert not (SRC / "components/studio/LiveView.tsx").exists()
    view = read("src/views/StudioView.tsx")
    assert "LiveView" not in view
    assert "explainRetiredLinks(window.location.search, pushToast)" in view, "旧链接落到这一屏却没人解释"
    assert "history.replaceState" in view, (
        "解释了但没摘参数 ⇒ 书签里那几个参数不会自己变，每次刷新都会重说一遍（owner 2026-10-30 报的就是它）"
    )
    carrier = read("src/lib/retiredLinks.ts")
    for retired in ('"live"', '"studio"', '"dispatch"'):
        assert retired in carrier, f"退役参数名单里少了 {retired}：那类旧链接会静默无话"
    assert len(re.findall(r"(?<![\w.])say\(", carrier)) == 1, "说一句变说两句 ⇒ 一张 URL 上叠两张同款卡"


def test_the_scanner_actually_sees_the_frontend_sources():
    """扫到 0 个文件时，上面每一条"没发现违例"都只是没去看。

    这一条把它变成会红的：目录改名、扩展名换掉、或有人把面板挪出 `src/`，这里先报。
    """
    seen = {path.relative_to(SRC).as_posix() for path in source_files()}
    for required in (
        "main.tsx",
        "App.tsx",
        "lib/acp.ts",
        "lib/live.ts",
        "lib/retiredLinks.ts",
        "views/StudioView.tsx",
        "components/studio/StudioScreen.tsx",
        "components/studio/BoardScreen.tsx",
        "components/studio/SidePanel.tsx",
        "shell/TopBar.tsx",
    ):
        assert required in seen, f"扫描面没看见 {required}：{sorted(seen)}"
    # 壳的**头注释**里不许再写"N 块面板"这种死数：笔 F 那一版飘过一次（注释写四块、只渲染两块），
    # 而三栏之后名单还会动。只管头注释那一段 —— 页面正文里"任何一块面板不许卸载"是量词，不是计数。
    # 名单与挂载的一致性由节 九 那几条结构判据保证，不靠散文复述。
    shell_doc = re.search(r"/\*\*([\s\S]*?)\*/", read("src/App.tsx"))
    assert shell_doc, "取不到壳的头注释 ⇒ 这一格的扫描面对象变了"
    assert not re.search(r"[一二三四五六七八九十\d]+\s*块面板", shell_doc.group(1)), (
        "壳的头注释里写死了面板块数：增删一块面板时这句会静默变成假话"
    )


# ---------------------------------------------------------------- 九、指挥台布局与导航（笔 AA）

PANEL_FILES = {
    "activity": "src/panels/ActivityPanel.tsx",
    "growth": "src/panels/GrowthPanel.tsx",
    "approvals": "src/panels/ApprovalsPanel.tsx",
    "runtime": "src/panels/RuntimePanel.tsx",
    "tasks": "src/panels/TaskBoardPanel.tsx",
    "bus": "src/panels/BusPanel.tsx",
}


#: PR2 的三视图产地（owner 2026-10-24）。七块旧面板不再全挂在一页上：
#: 任务视图装沙盘/留痕/Agent 栏，治理视图装审批/总线/参数/档案。
#: 2026-10-27：第一档从「任务图」升成「项目工作室」—— 文件改名 `views/StudioView.tsx`，
#: 视图内部两段（工作室 / 执行看板），但**座舱本身一个字节没动**（仍是四区、仍是那块 `hud-frame`）。
TASK_VIEW = "src/views/StudioView.tsx"
GOV_VIEW = "src/views/GovernanceView.tsx"
#: PR12：`IntakePanel` 从这份名单里退出（文件也没了）—— 受理只剩顶栏那颗 CTA + 全局弹窗。
TASK_VIEW_PANELS = ("TaskBoardPanel", "ActivityPanel", "AgentRail")
GOV_VIEW_PANELS = ("ApprovalsPanel", "BusPanel", "RuntimePanel", "GrowthPanel")


def test_only_the_active_view_is_mounted_and_no_view_is_hidden():
    """不活跃的那一屏根本不构造（owner 2026-10-24 那条红线的延续）。

    2026-10-27 大改版：一级导航只剩一档，所以"切页卸载"这件事下沉了一层 ——
    工作室与执行看板是**同一个视图里的两段**，`StudioView` 用一条三元表达式在
    `StudioScreen` / `BoardScreen` 之间二选一。这一条钉的就是那条三元：
    恰好一处、两个分支、谁都没被 `hidden` 藏起来。

    `App.tsx` 与 `AppShell.tsx` 里同样不许出现 `hidden` / `invisible` / `display:` /
    `visibility`：显隐绕过条件挂载，就等于"两棵树都在，只是有一棵看不见"。
    """
    view = ts_code("src/views/StudioView.tsx")
    ternary = re.search(r"\{\s*([^\n?]*?)\?\s*<BoardScreen[^\n]*?:\s*<StudioScreen", view)
    assert ternary, "工作室两段不是恰好一条三元分支 ⇒ 多一个分支就会多挂一棵树"
    assert 'studio.screen === "board"' in ternary.group(1), "三元条件没按 `screen` 判"
    assert "studio.plan" in ternary.group(1), "看板分支少了 `plan` 条件 ⇒ 未立项可能被挂上看板"
    assert "<StudioScreen" in view and "<BoardScreen" in view, "两段里有一段没挂"
    for banned in ("hidden", "invisible", "display:", "visibility"):
        assert not re.search(rf"(?<![\w-]){re.escape(banned)}(?![\w-])", view), (
            f"StudioView 里出现了 {banned!r} ⇒ 两段开始藏而不是二选一"
        )

    for rel in ("src/shell/AppShell.tsx", "src/App.tsx"):
        code = ts_code(rel)
        for banned in ("hidden", "invisible", "display:", "visibility", "viewMode"):
            assert not re.search(rf"(?<![\w-]){re.escape(banned)}(?![\w-])", code), (
                f"{rel} 里出现了 {banned!r} ⇒ 显隐绕过了条件挂载"
            )

    # 容器 id 跟着那一层走：`#view-studio` 现在挂在 `.shell` 上（它本来就是整屏的壳）。
    assert 'id="view-studio"' in view, "工作室的容器 id 不在 StudioView 上"
    shell = ts_code("src/shell/AppShell.tsx")
    assert "<StudioView" in shell and shell.count("<StudioView") == 1, "壳里挂的不止一处 StudioView"


def test_the_column_widths_have_one_carrier_each():
    """栏宽：`820`（正文行宽）与指挥台那一栏的上限各只许写一处，壳里不许再落一个像素字面量。

    顺带把 `tokens.json` 的 `measure_px` 与 `index.css` 的 `--measure` 对上 —— 这一对从笔 F 起就没人比过，
    比字面量的判据只覆盖 `color` 那一族。
    """
    css = read("src/index.css")
    board = re.search(r"@utility board-column \{[^}]*max-width: (\d+)px", css)
    assert board, "@utility board-column 里没有以像素写出的 max-width ⇒ 壳里的限宽栏根本不限宽"
    literal = f"{board.group(1)}px"
    assert len(re.findall(rf"(?<![\w-]){re.escape(literal)}\b", css)) == 1, f"{literal} 在 index.css 里写了多份"
    assert not re.search(r"\d{3,}px", ts_code("src/App.tsx")), "壳里出现了像素字面量 ⇒ 栏宽有了第二份载体"

    declared = json.loads(read("src/styles/tokens.json"))["measure_px"]
    actual = re.search(r"--measure: (\d+)px", css)
    assert actual and int(actual.group(1)) == declared, f"正文行宽两侧不同值：JSON {declared} vs CSS {actual and actual.group(1)}"

# ---------------------------------------------------------------- 十、内容不许把栏撑破（笔 AC）

#: 面板与组件里"长度由服务端决定、且多半没有空格可断"的那些落点（`JSON.stringify`、哈希、消费者路径清单、单号）。
#: 一处一格，加一格就得在这里补一格 —— 下面第二条数总数，所以"偷偷多渲染一处长串"先红在那条上。
#:
#: **规范 `plan-web-v2.md` §6.3 的名册 v2 = 13 格**（笔 AD 第 3 笔把 7 格升到 13 格）。四列各管一件事：
#: `(文件, 锚点, 形状名, 这一格指定的吸收器)`。第 4 列是**逐格钉**而不是"五个词里出现任何一个就算过"：
#: §6.1 那条规则（机器串用 `break-all`、自然语言用 `break-words`、滚动盒留给 `<pre>`）只有钉到格上才量得到，
#: 否则一个 `break-words` 蹭过窗口就能替一条永远断不开的 sha256 签字。
#:
#: **v4 第 A 笔（办公室视图 AK-1/AK-2）把名册升到 17 格**（形状 9 → 11）。owner 那句"名册 +3"是按
#: `plan-web-v4.md` §8-#6 的三处（`desk.agentId` / 气泡句 / `pending_item`）估的；落地时 `pending_item`
#: 那一格**没有载体**（右栏三张卡不在本笔的组件清单里，见台账 §二十六），而 L1 摘要的主句与副句
#: **各是一处**渲染（两处都拼 `agentId` 与 `describe()` 的 `entity_id`）⇒ 实落 +4。不虚设一格凑数：
#: 形状与命中数是逐条对读的，假数字会让第二条判据当场红。
UNBROKEN_RENDERERS: tuple[tuple[str, str, str, str], ...] = (
    ("src/panels/ActivityPanel.tsx", r">\{row\.entity_id\}<", "entity_id", "break-all"),
    ("src/panels/ActivityPanel.tsx", r">\{row\.actor_agent_id\}<", "actor_id", "break-all"),
    ("src/components/ActivityDetailDrawer.tsx", r"\{JSON\.stringify\(", "json", "overflow-auto"),
    ("src/panels/ApprovalsPanel.tsx", r"\{JSON\.stringify\(detail\.payload", "json", "overflow-auto"),
    ("src/panels/ApprovalsPanel.tsx", r">\{detail\.ticket_id\}<", "ticket_id", "break-all"),
    ("src/panels/ApprovalsPanel.tsx", r">\{event\.reason_text\}<", "reason", "break-words"),
    ("src/panels/GrowthPanel.tsx", r">\{row\.name\}<", "name", "break-all"),
    ("src/panels/GrowthPanel.tsx", r"row\.sha256\.slice\(0, 19\)", "slice", "break-all"),
    ("src/panels/RuntimePanel.tsx", r"view\.source_sha256\.slice\(0, 19\)", "slice", "break-all"),
    ("src/panels/RuntimePanel.tsx", r"\{maxLabel\.text\}", "label", "break-all"),
    ("src/panels/RuntimePanel.tsx", r"\{mergeLabel\.text\}", "label", "break-all"),
    ("src/components/AgentRail.tsx", r">\{slot\.agentId\}<", "agent_id", "break-all"),
)

#: 一格里"消化长串"的两种办法：**断词**，或者**自己做滚动盒**。
#: 后者留给审批详情那个 `<pre>`：把 pretty-print 的 JSON 断成锯齿比让它横向滚更难读。
ABSORBERS = ("break-all", "break-words", "overflow-auto", "overflow-x-auto", "overflow-y-auto")

#: 载体窗口：JSX 的属性写在表达式**前面**，所以从落点往上找最近的 `className=`，再往后吃这些字符。
CARRIER_WINDOW = 240

#: 数落点用的形态。**只数会真渲染出长串的那几种**：`.slice(0, 数字)` 才是把值截短展示，
#: 而 `slice(0, RENDER_WINDOW)` 是页面渲染窗口的 JS 切片（不落到 DOM 文本上），用 `\d+` 把它排除掉。
#:
#: **规范 §6.4：3 形状 → 9 形状 → 11 形状，命中合计 17 = 名册 17**（v4 第 A 笔）。形状带**名字**，
#: 名册那一格必须指名它属于哪个形状 —— 于是"格数"不再靠人手抄一个总数，而是**从名册里数出来的**：
#: 从名册删掉一格而代码没动，该形状的读数就比名册多一条，第二条判据当场红。
#: 第一版这里写的是三个无名串加一句"总数应当相等"，那种写法两侧同改（删一格 + 把总数改成 12）会安静地绿。
RENDERER_SHAPES: tuple[tuple[str, str], ...] = (
    ("json", r"JSON\.stringify\("),
    ("slice", r"\.slice\(0, \d+\)"),
    ("label", r"Label\.text"),
    ("entity_id", r">\{row\.entity_id\}<"),
    ("actor_id", r">\{row\.actor_agent_id\}<"),
    ("ticket_id", r">\{detail\.ticket_id\}<"),
    ("reason", r">\{event\.reason_text\}<"),
    ("name", r">\{row\.name\}<"),
    # 笔 AL 起 DeskUnit 的 ID 是局部量（`{agentId}`），Agent 栏仍是 `{slot.agentId}`；
    # 两侧都是名牌上的同一格，所以一条形状收两种写法、命中数仍是 2。
    ("agent_id", r">\{(?:slot\.)?agentId\}<"),
)


def carrier_before(code: str, anchor: str) -> str:
    """落点往上最近一个 `className=` 的**属性值本身** —— 通常是**这一格自己**的载体。

    第一版从这里往后吃 240 个字符，结果页脚主句的 `break-all` 被摘掉时，窗口把下一段
    `<p className="… break-all">` 的副句也吃了进来，变异 M4 照样绿（2026-10-21 实测）。
    那正是"吸收器挂在兄弟上也算过"的假绿，所以现在只取这一个 `className` 的值；
    祖先继承不再被这条判据兜着 —— 它由下面的数总数判据与布局判据补，口径比"窗口里有个词"强。
    """
    hit = re.search(anchor, code)
    assert hit, f"锚点取不到 ⇒ 那一格被改名或删掉了，这条判据得跟着改：{anchor}"
    start = code.rfind("className=", 0, hit.start())
    assert start != -1, f"落点之前找不到 className ⇒ 这一格没有可检查的载体：{anchor}"
    value_start = start + len("className=")
    quote = code[value_start : value_start + 1]
    if quote in {'"', "'"}:
        end = code.find(quote, value_start + 1)
        if end != -1:
            return code[start : end + 1]
    return code[start : start + CARRIER_WINDOW]


# ---------------------------------------------------------------- 十一、按钮不许被压成两行（笔 AD 第 2 笔 · P1）

#: 下面这张表把"这颗按钮**单行**时至少多高"从**类名**算出来：Tailwind v4 的默认尺度是
#: `--spacing: .25rem`（根字号 16px ⇒ 类名里的数是**档数**，`py-1` = 1 × 4px = 4px、`py-1.5` = 6px），
#: 行高跟着 `text-*` 走（`text-xs` 1rem、`text-sm` 1.25rem、`text-base` 1.5rem、`text-lg` 1.75rem）。
#:
#: 两处口径是量出来的，不是推出来的（DOM 现读见台账 §二十三）：
#: - **不算描边**。描边只会把高度加上去，所以把它省掉得到的是一台**永不虚高**的尺子；而本机 dpr=1.5 时
#:   一根 `border` 的 1px 被 `getComputedStyle` 报成 **0.666667px**（物理像素对齐），把"2px"写进算式
#:   会让算出来的数比屏上的数大 —— 那种尺子中的是"绿了但按钮还是被压扁"。
#: - **行高不许继承**。按钮不带 `text-*` 时它到底多高，取决于祖先挂了什么（实测同一屏里有 25.6px 和 16px 两种），
#:   所以这一条要求每一颗按钮自己写明字号，算式才等于屏上的数。
#: 宽度算不出来（要的是渲染后的字形），所以宽度那一半交给 DOM 实测，读数在台账 §二十三。
PADDING_STEP_PX = 4.0
LINE_HEIGHT_PX = {"text-xs": 16.0, "text-sm": 20.0, "text-base": 24.0, "text-lg": 28.0}
MIN_BUTTON_HEIGHT_PX = 32.0

#: 操作条的唯一产地（§4.12：把 R8 变成**一个组件**，而不是一份要人记住的字符串）。
#: P1 那颗"保/存各占一行"的按钮，根因就是每一块面板自己写了一遍 `flex items-center gap-3`（没有 wrap）
#: 而按钮没有 `shrink-0` —— 两处各写一次，就总有某一处忘了其中一样。
ACTION_ROW_CARRIER = "src/components/ActionBar.tsx"

#: 穿 `styles/studio.css` 那一套类名的文件（2026-10-27 大改版）。它们的按钮不带 Tailwind 的
#: `text-*` / `shrink-0`，单行行为由 CSS 保证 —— 按类名量高度的判据对它们无效，等价判据在
#: `test_studio_frontend.py::test_studio_buttons_cannot_wrap`。这份名单**只许收窄**：
#: 往里塞一个用 utility 拼版式的文件，等于把它从"按钮不许折行"这条判据下摘出去。
STUDIO_SKIN_FILES = frozenset(
    {
        "src/views/StudioView.tsx",
        "src/shell/TopBar.tsx",
        "src/components/studio/StudioRail.tsx",
        "src/components/studio/StudioScreen.tsx",
        "src/components/studio/BoardScreen.tsx",
        "src/components/studio/SidePanel.tsx",
        "src/components/studio/UsageTab.tsx",
        "src/components/studio/RoleGuard.tsx",
        # 2026-10-29 交付清单为空时的那一块：`className="ghost go"` 走的就是 studio.css 那一套
        # （`.dt-blocked` 的四条规则写在同一个文件里），一个字 utility 都没有。
        # 这不是"往名单里塞一个用 utility 拼版式的文件"——那种塞法才是这条注释警告的事。
        "src/components/studio/DeliveryBlocked.tsx",
        # `LiveActions.tsx` 已删（owner 2026-10-28「去除所有待办清单」）：审批动作归后端规则，
        # 界面不再有第二套动作入口。名单照它自己的规矩收窄一格。
    }
)

#: `src` 底下不装界面、但会**生成**界面的文件（2026-10-27）。`studioEngine.ts` 的 `codegen()` 交付的是一份
#: 能下载能预览的静态站点，那三个 `<button class="cart|burger|btn">` 是**产物里的**按钮 —— 它们跟着交付
#: 出去的 HTML 走、样式写在生成的那份 `<style>` 里，既不受本应用的 utility 管、也不会在本应用里被 flex 挤。
#: 把它们按应用按钮量高度是拿错了尺子；量错了还会逼着往产物里塞 Tailwind 类名。
NON_UI_CODE_FILES = frozenset({"src/lib/studioEngine.ts"})


def button_carrier(code: str, start: int) -> str:
    """从 `<button` 往后取它自己的 `className` 载体。

    切在**第一个不是 `=>` 的 `>`** 上：JSX 的开标签里常有 `onClick={() => …}`，那个 `>` 属于箭头函数，
    拿它当结束会把载体截断成 `className="…` 的前半段。取不到 className 就交空串，由调用方判红 ——
    "没有可检查的载体"本身就是这一条要防的形状（那顆按钮的高度和压缩行为都没人管）。
    """
    at = code.find("className=", start)
    if at == -1 or at - start > 400:
        return ""
    end = re.search(r"(?<!=)>", code[at:])
    return code[at : at + (end.start() if end else 400)]


def min_button_height(carrier: str) -> float:
    pads = [float(item) * PADDING_STEP_PX for item in re.findall(r"\bpy-(\d+(?:\.\d+)?)\b", carrier)]
    line = next((px for name, px in LINE_HEIGHT_PX.items() if re.search(rf"\b{name}\b", carrier)), -1.0)
    return line + 2 * (max(pads) if pads else 0.0)


def test_every_button_has_room_to_stay_on_one_line():
    """每一颗按钮的**单行最小高度**从类名算得出来：低于 32 px 就说明它的文字会换行或被裁（P1 的形状）。

    这一条先红过一次（2026-10-20 笔 AD 第 2 笔，判据先行）。改之前的八颗现读：
    三颗 `px-3 py-1 text-sm` = 20 + 8 = **28 px**，审批面板的"重新读取"在 1440 视口下被压成
    52.7 × 89.3 px（**四行**），参数面板的"保存"被压成 46 × 52 px（**两行**）—— DOM 读数在台账 §二十三。
    修完之后按配方是 32（R6/R7）与 36（审批那颗行按钮），描边只会在屏上再加高，算式里省掉它是为了不做虚高的尺子。
    笔 AD 第 3 笔新增的时间轴行按钮算出来是 32（`text-xs` 16 + `py-2` 8×2）。

    另一半是"不许被 flex 兄弟挤扁"：非 `w-full` 的按钮必须同时带 `shrink-0` 与 `whitespace-nowrap`。
    `w-full` 那**两颗**是行按钮：审批列表那一颗、活动流时间轴那一颗（笔 AD 第 3 笔新增）。它们铺满整行、
    没有 flex 兄弟、**故意多行**（时间轴一行就有 3~5 格文字），所以算式量的是"单行下限"而不是"实际高度"；
    给它们加 `nowrap` 反而会把 笔 AC 那条溢出判据打回原形，所以按形状豁免而不是按名字。
    """
    scanned = 0
    for path in source_files():
        rel = f"src/{path.relative_to(SRC).as_posix()}"
        # 2026-10-27：工作室那一屏**不用 utility 拼版式**，它穿的是从原型搬来的那份样式表
        # （`src/styles/studio.css`）。那些按钮的高度与单行行为由 CSS 给（`.primary` / `.ghost` /
        # `.ibtn` …），类名里根本没有 `text-*`，这条按类名算高度的尺子量不到它们 ——
        # 硬给它们补一个 `text-sm` 反而是"为了判据改代码"。
        # 那一族的等价判据在 `tests/guards/test_studio_frontend.py::test_studio_buttons_cannot_wrap`，
        # 它量的对象是那份 CSS（`white-space:nowrap` 与最小高度），不是类名。
        if rel in STUDIO_SKIN_FILES or rel in NON_UI_CODE_FILES:
            continue
        code = ts_code(rel)
        for match in re.finditer(r"<button\b", code):
            scanned += 1
            carrier = button_carrier(code, match.start())
            assert carrier, f"{rel} 里有一颗 <button 没有 className 载体 ⇒ 它的高度与压缩行为没人管"
            assert any(re.search(rf"\b{name}\b", carrier) for name in LINE_HEIGHT_PX), (
                f"{rel} 的这颗按钮没写 `text-*` ⇒ 行高由祖先决定，上面那张表算出来的就不是屏上的数："
                f"{carrier[:120]}"
            )
            height = min_button_height(carrier)
            assert height >= MIN_BUTTON_HEIGHT_PX, (
                f"{rel} 的这颗按钮单行最小高度算出来 {height} px < {MIN_BUTTON_HEIGHT_PX} ⇒ 文字会折行："
                f"{carrier[:120]}"
            )
            if "w-full" in carrier:
                continue
            assert "shrink-0" in carrier and "whitespace-nowrap" in carrier, (
                f"{rel} 的这颗按钮没有 shrink-0/whitespace-nowrap ⇒ 它所在的 flex 行一挤就先压它："
                f"{carrier[:120]}"
            )
    assert scanned >= 5, f"整棵 web/src 只扫到 {scanned} 颗按钮 ⇒ 载体形状变了，这条判据得跟着改（0 颗不等于通过）"


# ---------------------------------------------------------------- 十二、流与状态的唯一产地（笔 AD 第 2 笔）

#: 这一节量的都是"第 2 笔之后才成为 load-bearing 的那几句规范话"（§4.13 / §4.9 / §7.1）。
#: §8 的表里这些判据本来不存在 —— 表只解决"既有判据与方向 B 冲不冲突"，它没有覆盖"新产地自带的新形状"。
#: 所以这一节是**加严**，不是改口径：既有 32 条一字未动。
#: 2026-10-27 起活的那条连接在 `lib/acp.ts`（治理台退役后 `api/client.ts` 只剩死代码引用）。
#: 下面那条判据的用意是"**活代码**里只有一条连接"，所以退役文件里的构造点不算违规 ——
#: `RETIRED_STREAM_CARRIERS` 逐个列出来，清理 Gen A 时这个集合应当变空、文件一起删。
STREAM_CARRIER = "src/lib/acp.ts"
RETIRED_STREAM_CARRIERS: set[str] = set()
ACTIVITY_STORE = "src/lib/activityStore.ts"
ACTIVITY_LIB = "src/lib/activity.ts"
AGENT_RAIL = "src/components/AgentRail.tsx"


def test_the_whole_front_end_has_exactly_one_event_source():
    """**活代码**里只许有一条 `EventSource`，且它构造在 `lib/acp.ts`；`lib/live.ts` 只是它的调用方。

    2026-10-27 之前这条钉的是 `api/client.ts` + `activityStore` 那一对；治理台退役后它们成了死代码，
    而实时看板继承了同一条纪律：连接只能有一个宿主，否则"时间轴在动、别处不动"两边都不报错。
    退役文件里的构造点不算违规（`RETIRED_STREAM_CARRIERS` 逐个登记），清理 Gen A 时那个集合应当变空。
    """
    codes = {f"src/{path.relative_to(SRC).as_posix()}": ts_code(f"src/{path.relative_to(SRC).as_posix()}") for path in source_files()}
    built = [(rel, code.count("new EventSource")) for rel, code in codes.items() if "new EventSource" in code]
    live = [item for item in built if item[0] not in RETIRED_STREAM_CARRIERS]
    assert live == [(STREAM_CARRIER, 1)], (
        f"活代码里 `new EventSource` 的落点应当只有 `{STREAM_CARRIER}` 一处，现读 {live}"
        f"（退役文件允许残留：{sorted(RETIRED_STREAM_CARRIERS)}）"
    )
    assert ts_code("src/lib/live.ts").count("openActivityStream(") == 1, "宿主调用连接的那一句应当只有一处"

# ---------------------------------------------------------------- 十三、时间轴与抽屉（笔 AD 第 3 笔）

DRAWER = "src/components/ActivityDetailDrawer.tsx"
TIMELINE = PANEL_FILES["activity"]
RAIL_UTILITY = "timeline-rail"

#: §5.2 那张表的三档 tone 之外不许有第二个色族来源；`alert` 那一档只有域里那三个"拦截/危险"kind 能用。
KIND_TONES = ("text-accent", "text-warn", "text-ok", "text-danger", "text-ink-muted")
ALERT_KINDS = {"GUARD_VIOLATION", "READONLY_VIOLATION", "AUTONOMY_INTERCEPT"}

#: 一行里的五个槽（§5.1）。前四个各钉一个"恰好一次"的锚点；**第五槽（语义标签）由下面那条回落正则钉**
#: （它写的是三元表达式，按串数会数到连接态那一格上去）。
TIMELINE_SLOTS = (
    r"formatClock\(row\.created_at\)",
    r">\{row\.entity_id\}<",
    r">\{row\.actor_agent_id\}<",
    r">\{row\.id\}<",
)


def kind_table(code: str) -> dict[str, tuple[str, str, bool]]:
    """从 `KIND_META` 的对象字面量里逐条取出 `kind → (标签, tone, alert)`。

    取不到 9 条就红 —— "对象换了写法（比如拆成两份数组）"在这里是**红**，不是"跳过"。
    """
    entries = re.findall(
        r'([A-Z_]+): \{ label: "([^"]+)", tone: "(text-[a-z-]+)", alert: (true|false) \}', code
    )
    assert entries, "取不到 KIND_META 的条目 ⇒ 那张表的形状变了，这条判据得跟着改（0 条不等于通过）"
    return {kind: (label, tone, alert == "true") for kind, label, tone, alert in entries}


# ---------------------------------------------------------------- 十四、视觉定稿（笔 AD 第 4 笔，owner 2026-10-21）

SEGMENTED_PANEL = "src/panels/ApprovalsPanel.tsx"


def test_the_error_page_is_a_glass_card_not_a_bare_paragraph():
    """渲染失败那一页也在同一条设计语言里：玻璃卡 + `text-danger` + 断词。

    `main.tsx` 不挂面板 ⇒ 它逃过了 R1 那条"四块面板"的量；但它是使用者唯一会看到"页面坏了"的时刻，
    一段裸 `<p className="bg-row …">` 与整屏玻璃并排时，读起来像第三方插进来的错误。
    三句文案（失败 / 可重试 / 发给维护者）一句都不许省 —— 含糊的兜底等于没有兜底。
    """
    code = ts_code("src/main.tsx")
    assert "glass" in code, "失败页没有玻璃卡"
    # 两个载体分别量：只写"文件里有 break-words"时，标题那颗顶着，段落那颗摘掉照样绿（第一版就这么漏的）。
    title = re.search(r'<h1 className="([^"]+)"', code)
    body = re.search(r'<p className="([^"]+)"', code)
    assert title and "text-danger" in title.group(1), f"失败页的标题没有用危险色顶出来：{title and title.group(1)!r}"
    assert title and "break-words" in title.group(1), f"标题没有断词：{title.group(1)!r}"
    assert body and "break-words" in body.group(1), f"错误正文没有断词 ⇒ 一条长异常串会把页面顶横：{body and body.group(1)!r}"
    for sentence in ("页面渲染失败", "刷新可以重试", "维护者"):
        assert sentence in code, f"失败页少了这句：{sentence}"
    assert "<pre" not in code, "把异常摊进 `<pre>` ⇒ 长串既不断词也不受容器宽度约束（规范 §4.4 点名过）"


# ---------------------------------------------------------------- 十五、办公室视图（PR3 物理分组 · owner 2026-10-25）

#: PR5 退休了办公室视图，但头衔表本身不属于"房间"：Agent 栏与存档点列表都靠它把
#: `actor_agent_id` 读成人名。它现在住在 `lib/roles.ts`，读者只剩两个。
ROLE_LIB = "src/lib/roles.ts"
ROLE_READERS = ("src/components/AgentRail.tsx", "src/components/CheckpointList.tsx")


def test_the_retired_office_tree_is_gone_for_good():
    """PR5：办公室视图连同它那套拟物资产**整棵树**不在盘上，也没有任何残留引用。

    这条是"删干净"的自证，不是审美：留一个不被引用的 `OfficeView.tsx` 在盘上，
    下一个人就会以为它是活的、改它、然后困惑于为什么屏上没有变化。
    """
    gone = (
        "src/components/OfficeView.tsx",
        "src/components/OfficeSeatCard.tsx",
        "src/components/OfficeHud.tsx",
        "src/components/ReplayPanel.tsx",
        "src/components/BusLanes.tsx",
        "src/components/RehearsalButton.tsx",
        "src/lib/office.ts",
        "src/lib/officeBoard.ts",
        "src/lib/officeLayout.ts",
        "src/lib/officeSeat.ts",
        "src/lib/officeStory.ts",
        "src/lib/avatars.ts",
        "src/lib/busPath.ts",
        "src/lib/replay.ts",
        "src/lib/rehearsal.ts",
        "src/lib/officeFx.ts",
    )
    for rel in gone:
        assert not (WEB / rel).exists(), f"{rel} 还在盘上 ⇒ 办公室这棵树没有删干净"

    for path in source_files():
        rel = f"src/{path.relative_to(SRC).as_posix()}"
        code = ts_code(rel)
        for banned in ("lib/office", "lib/avatars", "lib/busPath", "lib/replay", "lib/rehearsal", "officeFx"):
            assert banned not in code, f"{rel} 仍引用已删除的 {banned}"


def test_the_shell_is_one_viewport_tall_and_each_view_scrolls_inside_itself():
    """页面本身不长高：根节点一屏高、`<main>` 吃掉余下的高度且不外滚。

    PR6 之后滚动分两层但仍在"视图之内"：
    - 任务页（座舱）：容器 `overflow-hidden`，三栏各自 `overflow-y-auto` —— 页面永远不动，
      动的是栏；
    - 治理页：容器自己 `overflow-y-auto`（文档流，内容确实超过一屏）。
    `<main>` 两页都不许自己滚。
    """
    shell = ts_code("src/shell/AppShell.tsx")
    assert "min-h-screen" not in shell, "根节点还是至少一屏高 ⇒ 页面仍会随内容长高"
    roots = re.findall(r'<div className="([^"]*\bstudio-root\b[^"]*)"', shell)
    assert roots == ["studio-root"], f"整壳的高度载体不是唯一一个：{roots}"
    assert shell.count("studio-root") == 1, "`studio-root` 出现第二处 ⇒ 整壳高度有了第二个产地"

    # 2026-10-27：一屏高这件事从 utility（`h-screen`）搬到了那份搬来的样式表里，
    # 因为 `.top` 的 48px 与 `.shell` 的 `flex:1` 是同一条链上的两段，分开写就会漂。
    css = read("src/styles/studio.css")
    root_block = re.search(r"\.studio-root \{([\s\S]*?)\n\}", css)
    assert root_block, "取不到 `.studio-root` 的变量块"
    for word in ("height: 100vh", "display: flex", "flex-direction: column", "overflow: hidden"):
        assert word in root_block.group(1), f"整壳的高度链缺 {word!r} ⇒ 页面会随内容长高"
    assert ".studio-root .shell" in css and "flex:1" in re.search(r"\.studio-root \.shell \{([^}]*)\}", css).group(1), (
        "`.shell` 没有吃掉余下的高度 ⇒ 顶栏之下会留白或溢出"
    )

    view = ts_code("src/views/StudioView.tsx")
    main = re.search(r'<main className="([^"]+)"', view)
    assert main and main.group(1).strip() == "main", f"<main> 的高度载体不再是那一个类名：{main and main.group(1)!r}"
    assert ".studio-root .main {min-width:0; min-height:0;" in css, (
        "`.main` 没有 min-w-0 / min-height:0 ⇒ 一条长串能把中栏顶到 min-content"
    )
    assert "overflow-y-auto" not in (main.group(1) if main else ""), "<main> 自己滚 ⇒ 两段共用一条页面滚动位置"

    # 滚动下沉到栏：左栏、右栏体、画布各自滚，页面本身不动。
    for selector in (".studio-root .rail", ".studio-root .side-body", ".studio-root .dag-wrap"):
        block = re.search(rf"{re.escape(selector)} \{{([^}}]*)\}}", css)
        assert block, f"取不到 {selector} 的规则"
        assert "overflow" in block.group(1), f"{selector} 没有自己的滚动盒 ⇒ 长内容会把整壳顶出第二屏"
    assert "100vh" not in css.replace("height: 100vh", ""), (
        "除了整壳那一处，样式表里不该再有第二个 `100vh` —— 顶栏高度一变就得再改一次"
    )

# ---------------------------------------------------------------- 十七、主题切换与 FOUC（笔 AH）

#: 主题在 localStorage 里的键。`index.html` 的内联脚本与 `App.tsx` 各要写一次（前者不能 import），
#: 所以这一格是"两份字面量对读"，不是一个产地：漂开的表现是"刷新后主题回到默认"，而两边各自自洽。
THEME_KEY = "acp-theme"


def test_the_theme_is_chosen_before_the_bundle_runs():
    """FOUC 的唯一防线是**模块之前**的一段内联脚本：它先把 `data-theme` 写到 `<html>` 上。

    只靠 React 的 `useEffect` 不够：模块解析、首帧渲染都排在它前面，浅色用户每次刷新都会先看到
    一眼深色（或反过来），然后整页换色 —— 那不是"持久化"，是"每次都闪一下"。
    脚本还必须同帧写 `colorScheme`：CSS 到达之前的 UA 画布按它取色，缺了这一句会在深色主题下
    先闪一张白纸（本机 CSS 走模块注入，这个窗口是真实存在的）。
    """
    html = read("index.html")
    inline = re.search(r"<script>([\s\S]*?)</script>", html)
    module = html.find('<script type="module"')
    assert inline and module != -1, "index.html 里没有内联主题脚本或模块入口"
    assert inline.start() < html.find("</head>"), "主题脚本不在 <head> 里 ⇒ 它没有排在首帧之前"
    assert inline.start() < module, "主题脚本排在模块之后 ⇒ 首帧会先按默认主题画一次，切主题时闪一下"
    before = html[: inline.start()]
    assert "<style" not in before and 'rel="stylesheet"' not in before, (
        "主题脚本前面已经有样式 ⇒ 首帧可能先按另一套主色画"
    )
    body = inline.group(1)
    assert f'localStorage.getItem("{THEME_KEY}")' in body, "主题脚本没有读持久化的键"
    assert 'var theme = "dark";' in body, "默认主题不是深色"
    assert '=== "light"' in body, "浅色不是那个唯一的覆盖值"
    assert "catch" in body, "localStorage 抛异常时没有兜底 ⇒ 隐私模式下整段脚本会炸掉，主题反而不生效"
    assert "document.documentElement.dataset.theme = theme" in body, "脚本没有把主题写到 <html data-theme>"
    assert "document.documentElement.style.colorScheme = theme" in body, (
        "脚本没有同帧写 color-scheme ⇒ CSS 到达前的 UA 画布会按系统主题闪白"
    )


def test_the_theme_toggle_persists_and_reads_the_fouc_attribute_back():
    """切换按钮的四件套：状态域、FOUC 属性的回读、写入 effect、两档文案。

    初值必须从 `<html data-theme>` 读回来，而不是写死 `"dark"`：写死的话，浅色用户每次刷新
    React 都会先把主题改回深色 —— 内联脚本做的那一次正确选择被 React 的默认值当场覆盖。
    """
    app = ts_code("src/App.tsx")
    topbar = ts_code("src/shell/TopBar.tsx")
    html_key = re.search(r'localStorage\.getItem\("([^"]+)"\)', read("index.html"))
    app_key = re.search(r'THEME_KEY = "([^"]+)"', app)
    assert html_key and app_key and html_key.group(1) == app_key.group(1) == THEME_KEY, (
        f"主题键在两处不同（index.html {html_key and html_key.group(1)!r} / App.tsx {app_key and app_key.group(1)!r}）"
    )
    assert 'type Theme = "dark" | "light";' in app, "主题状态的域不是两档"
    assert "useState<Theme>(initialTheme)" in app, "主题状态不是从 initialTheme 起步"
    assert 'document.documentElement.dataset.theme === "light" ? "light" : "dark"' in app, (
        "state 的初值不是从 FOUC 脚本已经写好的属性读回来的 ⇒ 刷新后 React 会把主题改回默认"
    )
    assert app.count("document.documentElement.dataset.theme = theme") == 1, "写 data-theme 的地方不是恰好一处"
    assert app.count("document.documentElement.style.colorScheme = theme") == 1, (
        "没有同步内联 colorScheme ⇒ 首帧那段内联样式会盖住切换后的 CSS color-scheme"
    )
    assert "localStorage.setItem(THEME_KEY, theme)" in app, "切换没有落盘"
    assert re.search(r"\}, \[theme\]\);", app), "写主题的 effect 没有盯 theme ⇒ 切换后不生效"
    assert 'aria-pressed={theme === "light"}' in topbar, "主题按钮没有暴露按下态"
    # 2026-10-27：文案从「☀ 浅色 / ☾ 深色」改成「深色 / 浅色」—— 顶栏右上角只剩两颗键，
    # 字形图标在没有同伴的情况下读成装饰；而这两颗键说的是"点了会变成什么"，不是"现在是什么"。
    assert 'theme === "light" ? "深色" : "浅色"' in topbar, "主题按钮没有两档文案，或文案说反了"
    assert "onClick={onThemeToggle}" in topbar, "主题按钮没有接顶栏的切换落点"
    assert 'onThemeToggle={() => setTheme(theme === "light" ? "dark" : "light")}' in app, (
        "App 没有把主题状态接到 AppShell"
    )


def test_the_light_theme_reskins_both_views():
    """浅色必须覆盖两块屏（任务/治理）与两处遮罩：光晕、玻璃、scrim。

    只换 tokens 不够：玻璃的阴影与光晕的方向都是深色专用的配方，它们不在 `@theme` 的 15 个色值里。
    办公室全景那一套（墙/地板/网格/窗）随视图一起退休，不再有浅色覆盖要维护。
    """
    css = read("src/index.css")
    required = (
        '[data-theme="light"] .aura',
        '[data-theme="light"] .scrim',
    )
    missing = [
        selector
        for selector in required
        if not re.search(rf"{re.escape(selector)}(?:\s*,|\s*\{{)", css)
    ]
    assert not missing, f"这些浅色覆盖缺失 ⇒ 对应那块屏在浅色下仍是深色皮肤：{missing}"
    assert ".office-panorama" not in css, "全景的浅色覆盖还留着 ⇒ 退休的视图还有主题代码"
    # HUD 的四个派生量由 `--color-*` 现算，所以主题切换不需要为它们再写一份浅色块。
    for name in ("--hud-line", "--hud-line-strong", "--hud-panel", "--hud-corner"):
        assert name in css, f"HUD 派生量 {name} 不在 index.css 里"
    assert css.count("--hud-line:") == 1, "`--hud-line` 有第二份声明 ⇒ 浅色下会出现两套描边色"


# --------------------------------------------------- 十八、Phase 15 三栏的读面与两处"没有数据源"

PIPELINE_VIEW = "src/components/CollabPipeline.tsx"
GRAPH_LIB = "src/lib/taskGraph.ts"
PIPELINE_LIB = "src/lib/pipeline.ts"
TASK_STORE = "src/lib/taskStore.ts"
EXECUTION_STREAM = "src/components/execution/ExecutionStream.tsx"
APPROVAL_DOCK = "src/components/execution/ApprovalDock.tsx"
AGENT_NOTE = "src/components/execution/AgentNote.tsx"
TOOL_CALL = "src/components/execution/ToolCallCard.tsx"
LANE_CHART = "src/components/LaneChart.tsx"
EXECUTION_CARDS = (
    "src/components/execution/AgentNote.tsx",
    "src/components/execution/ToolCallCard.tsx",
    "src/components/execution/ArtifactCard.tsx",
    "src/components/execution/HandoffCard.tsx",
    "src/components/execution/ReviewThread.tsx",
)


def state_bucket_keys() -> set[str]:
    """从布局那份**纯函数**源文件里取状态色表的键。取不到就红 —— 0 个键不是"没有违例"。

    读 `lib/taskGraph.ts` 而不是 `.tsx`：色表与布局都在那一份里（`.tsx` 只有渲染），
    而 vitest 那一档没有 React 插件，纯函数必须能单独 import —— 产地跟着可测性走。
    """
    block = re.search(r"export const STATE_BUCKETS[^=]*= \{(.*?)\n\};", ts_code(GRAPH_LIB), re.S)
    assert block, f"取不到 {GRAPH_LIB} 里的 STATE_BUCKETS ⇒ 色表换了形状或换了产地（读空不算通过）"
    keys = re.findall(r"^\s*([A-Z_]+):", block.group(1), re.M)
    assert keys, "STATE_BUCKETS 读出来是空的"
    return set(keys)


# ---------------------------------------------------------------- 二十、PR3 核心视图（TrioGuard / 审批六要素 / DAG 三线）

APPROVAL_LIB = "src/lib/approvalCard.ts"
APPROVAL_CARD = "src/components/ApprovalCard.tsx"
TRIO_LIB = "src/lib/trio.ts"
TRIO_GUARD = "src/components/TrioGuard.tsx"
CARD_DETAIL = "src/components/CardDetail.tsx"
# --------------------------------------------------------------------------- PR10：标题中文化与右栏字号


#: 允许以英文出现在标题里的词。它们不是"界面标题"，而是**系统状态或代码名字** ——
#: 读者要在日志、接口与代码里原样再见到一遍，翻成中文反而接不上（owner 的原话：
#: 「除了代表系统状态或代码的专有名词（如 HUD、Token、ID、Agent）」）。
HEADING_LATIN_OK = ("HUD", "Agent", "Token", "ID", "ACP", "API")
CJK = re.compile(r"[\u4e00-\u9fff]")


def _heading_texts() -> list[tuple[str, str]]:
    """所有"面板标题"的落点与它们的字面：`h2/h3/h4` 带 `hud-label` 的那种，
    以及 `<SectionHead title=…>` / `<Frame title=…>` 两个段头工厂的 `title` 实参。
    """
    found: list[tuple[str, str]] = []
    for path in source_files():
        rel = f"src/{path.relative_to(SRC).as_posix()}"
        code = ts_code(rel)
        for match in re.finditer(r"<(h2|h3|h4)\b[^>]*hud-label[^>]*>\s*([^<>{}]+?)\s*<", code):
            found.append((rel, match.group(2)))
        for match in re.finditer(r"<(SectionHead|Frame)\b[^>]*?\btitle=\"([^\"]+)\"", code):
            found.append((rel, match.group(2)))
    return found


def test_the_finesse_craft_floor_holds():
    """PR14：把 finesse 的**通用工艺底线**钉成判据。

    这一层不是审美偏好，是"看起来贵 / 看起来廉价"之间那五条可量的东西：
    ① 页面不是纯白/纯黑，而是**染色**中性色（白卡可以，白页面不行 —— finesse 自己的浅色配方
       就是「染色页 + 白卡」）；
    ② 边框是 hairline：`edge` 与它所在底的对比度落在 1.03–1.6 之间。低于 1.03 等于没画，
       高于 1.6 就是 finesse 点名的"硬 1px #ccc/#333 灰框"；
    ③ 阴影带环境色相：`rgb(0 0 0` 一次都不许出现（纯黑落在蓝黑底上会发脏）；
    ④ 面板不带内发光：`inset 0 0 28px` 那条 accent 辉光正是 finesse 说的 AI glow；
    ⑤ accent lock：折角走 `--color-accent`，不借 `--color-ok`（绿只能表示"已交付"）；
    ⑥ 间距全在 4px 阶梯上：6/10/14px 这几种"差一点"的值不许再出现。

    这一条**不量圆角**：HUD 的直角是九个 PR 建起来的身份，finesse 的 16–22px 属于通用
    dashboard（见 tokens.json 的 naming_note 与 index.css 的 hud-frame 注释）。
    """
    css = read("src/index.css")
    # 只扫声明面：注释里正当写着"那条内发光已删掉"，按原文扫会把说明本身判红（口径同 `ts_code`）。
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for theme, palette in (("dark", tokens("dark")), ("light", tokens("light"))):
        canvas = palette["canvas"].lower()
        assert canvas not in ("#ffffff", "#000000"), f"{theme} 的画布是纯白/纯黑 ⇒ 廉价 dashboard 的第一眼"
        channels = tuple(int(canvas[i : i + 2], 16) for i in (1, 3, 5))
        assert len(set(channels)) > 1, f"{theme} 的画布是纯灰（三通道相等）⇒ 没有染色"
        ratio = contrast_ratio(palette["edge"], palette["canvas"])
        assert 1.03 <= ratio <= 1.6, (
            f"{theme} 的 edge 与画布对比度 {ratio:.2f} 不在 hairline 区间（1.03–1.6）"
            " ⇒ 要么等于没画，要么是硬灰框"
        )

    assert "rgb(0 0 0" not in body, "阴影里还有纯黑 ⇒ finesse 要的是带色相的落影"
    assert "inset 0 0 28px" not in body, "hud-frame 的内发光回来了（AI glow）"
    assert "--hud-corner: var(--color-accent);" in body, "折角不走近 accent ⇒ 屏上出现了第二个品牌色"

    off_grid = re.compile(r"\b(?:p|px|py|pt|pb|pl|pr|gap)-(?:1\.5|2\.5|3\.5)\b")
    offenders = []
    for path in source_files():
        rel = f"src/{path.relative_to(SRC).as_posix()}"
        hits = sorted(set(off_grid.findall(ts_code(rel))))
        if hits:
            offenders.append(f"{rel}: {hits}")
    assert not offenders, "间距掉回 6/10/14px（不在 4 的阶梯上）：\n  " + "\n  ".join(offenders)



def test_the_write_identity_never_comes_from_an_invisible_local_storage_key():
    """写面身份只许来自常量，**不许读盘**。

    2026-10-26 顶栏那格「身份」输入框退役了，但它当年写进 `localStorage["acp-principal"]` 的
    值留在了浏览器里。2026-10-28 实测踩到：服务端明明是 `--approver owner-1`，
    点「批准」却稳定 403 `principal_not_allowed` —— 因为页面把这个没人看得见、也没人能改的
    旧值当成了当前身份，而错误里根本不说本机发的是谁。

    判据扫 `src/lib/acp.ts`：那个键只许出现一次，且只能出现在 `removeItem(...)` 里。
    读它的写法一旦回来，403 就会重新变成"查不出的故障"。
    """
    client = read("src/lib/acp.ts")
    assert "removeItem(RETIRED_PRINCIPAL_KEY)" in client, (
        "退役键没被清掉 ⇒ 下一次有人把读盘那条写回来，它会立刻悄悄生效"
    )
    assert "getItem(RETIRED_PRINCIPAL_KEY)" not in client, (
        "身份又去读 `acp-principal` 了 —— 这个键没有界面能看见、能改，"
        "读它就等于让一个看不见的开关决定谁在批单"
    )
    assert re.search(r"\[PRINCIPAL_HEADER\]:\s*principalOrDefault\(\)", client), (
        "写面的身份不再从 `principalOrDefault()` 这个唯一入口取 ⇒ 又出现了第二条身份来源"
    )


# ---------------------------------------------------------------- 十六、兜底拆单标识（2026-10-31）

#: 降级来源：这两个值从后端 `orchestrate/graph.py::DEGRADED_SPLIT_SOURCES` 来，前端不许自己另起一套。


def test_the_board_marks_a_fallback_split_and_both_sides_agree():
    """P0-2：拆单降级时屏上要显眼标出来，且**前后端的降级集合逐字一致**。

    现场：`.env` 里那把豆包 key 失效 ⇒ 系统静默用了 Stub 拆单（卡上判据是模板句、`meeting_note`
    0 行），而屏上与真模型拆的单长得一模一样。这条判据钉三件事：
    ① 读面把 `split_source` 搬进引擎、活动流认识 `split_fallback`；
    ② 工作室抬头有一个带 `id` 的横幅，只在 live + 降级时渲染；
    ③ 两边的"降级集合"是同一对值（任一侧多加/少写一个值都要红）。
    """
    live = ts_code("src/lib/live.ts")
    screen = ts_code("src/components/studio/StudioScreen.tsx")

    assert "setLive({ splitSource: splitSourceOf(rows[0]) })" in live, "读面没把 split_source 搬进引擎"
    assert 'split_fallback: "warn"' in live, "活动流不认识 split_fallback ⇒ 那行留痕会掉成 info"
    assert 'id="fallbackSplitBadge"' in screen, "工作室抬头没有兜底拆单横幅"
    assert "兜底拆单 · 未接入真模型" in screen, "横幅文案丢了 —— 它必须明说'没接真模型'"
    badge = screen.split('id="fallbackSplitBadge"')[0][-320:]
    assert 'studio.source === "live"' in badge, "横幅必须在真链路才出现（推演档没有 split_source）"
    assert "DEGRADED_SPLIT_SOURCES.includes(studio.live.splitSource)" in badge, "渲染条件没走降级集合"

    graph = (REPO / "src" / "acp" / "orchestrate" / "graph.py").read_text(encoding="utf-8")
    py_set = re.search(r"DEGRADED_SPLIT_SOURCES = frozenset\(\{([^}]*)\}\)", graph)
    ts_set = re.search(r"DEGRADED_SPLIT_SOURCES: readonly SplitSource\[\] = \[([^\]]*)\]", live)
    assert py_set and ts_set, "有一侧的降级集合改了形状 ⇒ 这条判据读不到值（0 条不等于通过）"
    normalize = lambda body: tuple(sorted(re.findall(r'"([a-z_]+)"', body)))  # noqa: E731
    assert normalize(py_set.group(1)) == normalize(ts_set.group(1)) == ("fallback_stub", "stub")
