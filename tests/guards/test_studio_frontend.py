"""项目工作室那一屏（2026-10-27 大改版）的判据。

这一版把整块界面换成了 `项目工作室-原型.html` 的 1:1 复刻：
一级视图从两档（任务图 / 治理台）收敛成**一档**「项目工作室」，视图内部再分两段
（工作室屏 / 执行看板屏）；顶栏右上角只剩浅色/深色与设置；右栏多一个「用量」页签；
"三 agent"那一格改成**四席 × 模型**，模型由设置里选的厂商型号决定，未选就留空。

为什么单开一个文件而不是往 `test_board_frontend.py` 里加：那一份钉的是**旧座舱**的形状
（七块面板、两档 tab、四计数、彩排、状态条）。旧对象整批退役后，那些判据要么摘除、
要么把对象换掉 —— 换掉的那几条搬到这里，按**新对象**重写，一条一条说明它接的是哪一条旧意图。
留在原地的那些一字未动，它们量的东西（令牌、对比度、OpenAPI 面、端口同值）与这一屏无关但仍然有效。

十八节分工：
- 一 = 视图域收敛到一档，且退役的那份视图域文件真的不在了。
- 二 = 工作室两段互斥挂载（不是显隐）。
- 三 = 顶栏只剩两颗键，演示控制键一颗都不许回来。
- 四 = 壳一屏高，每一块区域自己滚（旧判据"壳一屏高 + 视图内滚"的新对象）。
- 五 = 按钮的单行下限（旧判据按 Tailwind 类名量，这一屏穿的是 CSS，所以改量那份 CSS）。
- 六 = 右栏六个页签，「用量」排在「交付」之前。
- 七 = 用量页签自己声明是估算。
- 八 = 四席 × 模型，互斥只判执行/审查/验收，未选留空。
- 九 = 未选模型时不许编占位型号（旧判据"缺读数就写 0 而不是留空"的反面：这里是"没有就真的没有"）。
- 十 = `studio.css` 的引入顺序（`@import` 必须先于 `@source`）。
- 十一 = 关键帧只许两支。
- 十二 = `studio.css` 的颜色全部从令牌层借。
- 十三 = 退役文件不许被活代码重新连上（承接"整页删掉"）。
- 十四 = 工作室屏的四张卡各挂一次（复刻的骨架）。
- 十五 = 提示条挂在顶栏中段，不许回到画布浮层（2026-10-27 第二轮）。
- 十六 = 右栏底部有一条"回项目工作室"的路，且它不随页签走（2026-10-27 第三轮）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"
SRC = WEB / "src"

STUDIO_CSS = "src/styles/studio.css"


def read(rel: str) -> str:
    """读 `web/` 下的一个文件。缺失就是失败 —— 不许"文件不在了就跳过"。"""
    path = WEB / rel
    assert path.is_file(), f"前端文件不在：{path}"
    return path.read_text(encoding="utf-8")


def ts_code(rel: str) -> str:
    """剥掉注释后的 TS 正文。禁令类判据只能扫代码面：注释里正当提着某个词是在解释"为什么删了"。"""
    text = re.sub(r"/\*.*?\*/", "", read(rel), flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def css_rule(selector: str) -> str:
    """取一条选择器的声明块（只取第一处）。找不到就红 —— 选择器改名等于判据失效。"""
    text = read(STUDIO_CSS)
    at = text.find(selector + " {")
    assert at != -1, f"{STUDIO_CSS} 里找不到 `{selector} {{`：这条选择器被改名或删掉了"
    end = text.find("}", at)
    assert end != -1, f"{STUDIO_CSS} 里 `{selector}` 的声明块没有闭合"
    return text[at:end]


# ---------------------------------------------------------------- 一


def test_the_view_domain_has_collapsed_to_one_view():
    """一级视图只剩一档，且退役的 `shell/view.ts` 真的不在了。

    接的旧意图：`test_the_two_tabs_are_the_only_top_level_navigation` /
    `test_hash_is_the_only_view_state`（两条都随两档视图一起退役）。
    新的口径是"没有第二档"，所以判据从"两档 tab 长什么样"变成"第二档的产地不存在"：
    视图域文件删掉、hash 解析函数在 `src` 里一次都不出现、壳里只有一处 `<StudioView />`。
    """
    assert not (SRC / "shell" / "view.ts").is_file(), (
        "`src/shell/view.ts` 还在：视图只剩一档之后，它是一份没有任何调用方的"
        "「视图名 ↔ hash」映射，留着就会有人拿它把第二档接回来"
    )
    for rel in ("src/App.tsx", "src/shell/AppShell.tsx", "src/views/StudioView.tsx"):
        code = ts_code(rel)
        for gone in ("viewFromHash", "hashOfView", "VIEWS", "ViewKey"):
            assert gone not in code, f"{rel} 里还有 `{gone}` ⇒ 视图域没收敛"

    shell = ts_code("src/shell/AppShell.tsx")
    assert shell.count("<StudioView />") == 1, "壳里必须恰好挂一次工作室，且不带条件"


# ---------------------------------------------------------------- 二


def test_the_two_studio_screens_are_mounted_exclusively():
    """工作室屏与执行看板屏是**二选一**，不是两块常驻靠 hidden 切换。

    接的旧意图：`test_the_two_views_are_conditionally_mounted_and_never_hidden`。
    对象从"两个一级视图"换成"同一屏里的两段" —— 判据的实质没变：同一时刻只构造一段。

    2026-10-27 第四轮：条件从 `screen === "studio" ?` 改成 `screen === "board" && plan ?`。
    改的不是形状而是**保证的层次**：原来"未立项到不了看板"靠的是"进看板那颗按钮只在有 plan 时渲染"
    —— 那是可达性层面的保证，别处多一条 `setScreen("board")` 就破。写进挂载条件之后，
    它在结构层面成立，`BoardScreen` 也就不必再为空态留分支（那张空态从来没上过屏）。
    """
    view = ts_code("src/views/StudioView.tsx")
    ternary = re.search(r"\{\s*([^\n?]*?)\?\s*<BoardScreen[^\n]*?:\s*<StudioScreen", view)
    assert ternary, "两段不是按一条三元挂载的 ⇒ 多一个分支就会多挂一棵树"
    condition = ternary.group(1)
    assert 'studio.screen === "board"' in condition, "三元条件没按 `screen` 判"
    assert "studio.plan" in condition, (
        "看板分支不再带 `plan` 条件 ⇒ 未立项有可能被挂上看板，而看板的每一块都建立在「已立项」之上"
    )
    assert view.count("<StudioScreen") == 1
    assert view.count("<BoardScreen") == 1
    assert not re.search(r"(?<![\w-])hidden(?![\w-])", view), "工作室里不许用 hidden 做显隐"
    assert "display:" not in view and "visibility" not in view


# ---------------------------------------------------------------- 三


def test_the_top_bar_carries_only_the_theme_toggle_and_settings():
    """顶栏右上角只剩两颗键：浅色/深色、设置。

    接的旧意图：`test_the_top_bar_carries_two_tabs_and_the_governance_badge` /
    `test_the_settings_entry_replaces_the_truth_badge`。两条量的是"顶栏上有什么"，
    新口径是"顶栏上只剩什么"：原型那四颗演示控制键（倍速 / 暂停 / 注入故障 / 重置）
    是给看演示的人用的，不是产品能力，一颗都不许回到产品壳里。
    """
    topbar = ts_code("src/shell/TopBar.tsx")
    assert topbar.count('className="ibtn"') == 2, "顶栏右上角必须恰好两颗按钮"
    assert 'aria-haspopup="dialog"' in topbar, "设置那颗键要声明它开的是 dialog"
    assert 'theme === "light" ? "深色" : "浅色"' in topbar, (
        "主题键的文案要说**点了会变成什么**：深色时写「浅色」，浅色时写「深色」"
    )
    for gone in ("倍速", "暂停", "注入故障", "重置", "彩排", "发起需求", "待处理"):
        assert gone not in topbar, f"顶栏里还有 `{gone}` ⇒ 退役的演示控制键回来了"
    for gone in ("setSpeed", "togglePause", "injectFailure", "resetStudio", "rehearsal"):
        assert gone not in topbar, f"顶栏还在调 `{gone}`"
    assert "useStudio()" in topbar, "顶栏读数必须来自引擎，不许再吃后端快照"


# ---------------------------------------------------------------- 四


def test_the_shell_is_one_viewport_tall_and_each_region_scrolls_inside_itself():
    """壳一屏高，滚动归各区域自己。

    接的旧意图：`test_the_shell_is_one_viewport_tall_and_each_view_scrolls_inside_itself`。
    对象从"两档视图各自的滚动盒"换成"左栏 / 主区 / 右栏 / DAG 画布各自的滚动盒"。
    这一条防的是"整页滚"：整页一滚，顶栏就会跟着走，左栏与右栏也会各自错位。
    """
    root = css_rule(".studio-root")
    for decl in ("height: 100vh", "display: flex", "flex-direction: column", "overflow: hidden"):
        assert decl in root, f"`.studio-root` 少了 `{decl}` ⇒ 壳不是一屏高"

    shell = css_rule(".studio-root .shell")
    assert "flex:1" in shell, "`.shell` 必须吃掉顶栏之外的全部高度"
    assert "min-height:0" in shell, "`.shell` 少了 `min-height:0` ⇒ grid 子项会被内容顶开"
    # 左栏宽度从字面量 246px 改成 `var(--rail-w)`：窄屏块要用它算主区下限
    # （`calc(var(--app-min-width) - var(--rail-w))`），两处写死同一个数就迟早会分叉。
    # 值本身仍由 tokens.json 的 layout.rail_px 钉住（见 test_the_min_width_is_declared_and_enforced）。
    assert "grid-template-columns:var(--rail-w) minmax(0,1fr)" in shell, (
        "左栏宽度必须接 `var(--rail-w)` 且主区带 `minmax(0,…)`：不写 0 的下限，长串会把主区撑破"
    )

    main = css_rule(".studio-root .main")
    assert "min-width:0" in main and "min-height:0" in main, "`.main` 少了 `min-*:0`"

    for selector, why in (
        (".studio-root .rail", "左栏"),
        (".studio-root .side-body", "右栏正文"),
        (".studio-root .dag-wrap", "DAG 画布"),
    ):
        rule = css_rule(selector)
        assert "overflow-y:auto" in rule or "overflow:auto" in rule, f"{why}没有自己的滚动盒"


# ---------------------------------------------------------------- 五


def test_studio_buttons_cannot_wrap():
    """按钮的单行下限由那份 CSS 保证，不由类名保证。

    接的旧意图：`test_every_button_has_room_to_stay_on_one_line`。旧判据从 Tailwind 类名
    算最小高度（`text-sm` + `py-2` ≥ 32px），这一屏穿的是 `studio.css`，类名里根本没有 `text-*`。
    等价判据改量 CSS：三档按钮各自写死字号、内边距、圆角，并且 `white-space:nowrap` ——
    字号由按钮自己给，行高才是确定的；nowrap 才是"不许折成两行"本身。
    """
    for selector, name in (
        (".studio-root .primary", "主按钮"),
        (".studio-root .ghost", "次按钮"),
        (".studio-root .ibtn", "顶栏那颗小键"),
    ):
        rule = css_rule(selector)
        for decl in ("font-size:", "padding:", "border-radius:", "white-space:nowrap"):
            assert decl in rule, f"{name}（`{selector}`）少了 `{decl}`"

    base = css_rule(".studio-root :where(button)")
    assert "color" not in base, (
        "`.studio-root :where(button)` 里写了 `color` ⇒ 这份**无层**样式表会盖掉 Tailwind 的分层 "
        "utility（`.text-accent-ink` 这类），按钮文字会变回继承色"
    )


# ---------------------------------------------------------------- 六


def test_the_right_rail_has_six_tabs_and_the_usage_tab_sits_before_delivery():
    """右栏六个页签，顺序是 详情 / 活动流 / 审查 / 文件总线 / 用量 / 交付。

    为什么要钉顺序：「用量」与「交付」都是这一单的收尾读数，但成本要在验收之前看，
    所以它必须排在交付前面。钉键名是因为 `SideTab` 是联合类型，少一个键就少一屏内容。
    """
    panel = ts_code("src/components/studio/SidePanel.tsx")
    block = panel[panel.find("const TABS") : panel.find("];", panel.find("const TABS"))]
    keys = re.findall(r'\{\s*k:\s*"([a-z]+)"', block)
    labels = re.findall(r'label:\s*"([^"]+)"', block)
    assert keys == ["detail", "feed", "review", "bus", "usage", "deliver"], f"页签键序漂了：{keys}"
    assert labels == ["任务详情", "活动流", "审查", "文件总线", "用量", "交付"], f"页签文案漂了：{labels}"

    engine = ts_code("src/lib/studioEngine.ts")
    assert 'export type SideTab =' in engine
    for key in keys:
        assert f'"{key}"' in engine.split("export type SideTab =", 1)[1][:400], (
            f"`SideTab` 联合类型里少了 `{key}`"
        )


# ---------------------------------------------------------------- 七


def test_the_usage_tab_declares_itself_an_estimate():
    """成本与用量这一格必须自己声明"这是估算"。

    这一格最容易写歪的地方是"把估算画成账单"：一个漂亮的进度条 + 两位小数，
    读者就会当成真花了这么多钱。所以标题旁边常驻「估算」二字，页脚再说一遍口径，
    而且口径里不许出现 markdown 星号（那是把说明当字面量画出来）。
    """
    usage = read("src/components/studio/UsageTab.tsx")
    assert "COST · 估算" in usage, "用量页签的标题没有声明「估算」"
    assert "估算" in usage.split("<div className=\"dt\"", 1)[1][:600], "「估算」必须出现在页签顶部，不是页脚才有"
    # 星号与"冒充账单"的词都只在**代码面**上查：文件头的 JSDoc 正当地在讲"别把估算画成账单"
    code = ts_code("src/components/studio/UsageTab.tsx")
    assert "**" not in code, "页签里出现了 markdown 星号 ⇒ 界面会把星号当字面量画出来"
    # 「账单」本身不违禁 —— 页签上那句 `<i>估算，非账单</i>` 正是要说的意思。
    # 违禁的是"把估算说成已经花掉的钱"：实际花费 / 已扣费 / 对账单这一族。
    for gone in ("实际花费", "已扣费", "对账单"):
        assert gone not in code, f"用量页签里写了 `{gone}`：估算不能冒充账单"

    store = ts_code("src/lib/studioUsage.ts")
    assert "PRICE_PER_K" in store, "单价表不在 `studioUsage.ts` 里"
    assert "export function usageOf(" in store


# ---------------------------------------------------------------- 八


def test_the_four_seats_are_matched_against_the_selected_models():
    """四席（统筹 / 执行 / 审查 / 验收）× 模型，互斥只判后三席。

    接的旧意图：`test_the_three_agents_are_one_body`（`TrioGuard` 那条）。旧判据比的是**岗位名**，
    而岗位名永远是三个不同的字符串 —— 那条判据因此永远绿，等于没判。模型搬进设置之后，
    "这三件事是不是同一个主体干的"只能比 (厂商, 型号)。
    统筹不算进互斥：它是派单与裁决的那一席，与执行席同模型不构成"自己审自己"，
    把它算进来会让出厂配置从第一次打开就红 —— 那不是发现了问题，是把判据用错了地方。
    """
    guard = ts_code("src/components/studio/RoleGuard.tsx")
    keys = re.search(r'const keys: readonly RoleKey\[\] = \[([^\]]+)\]', guard)
    assert keys, "RoleGuard 里找不到四席的键名清单"
    order = re.findall(r'"([a-z]+)"', keys.group(1))
    assert order == ["orchestrator", "executor", "reviewer", "acceptor"], f"四席顺序漂了：{order}"

    checked = re.search(r'const checked = \(\[([^\]]+)\] as const\)', guard)
    assert checked, "RoleGuard 里找不到「参与互斥的那几席」"
    checked_keys = re.findall(r'"([a-z]+)"', checked.group(1))
    assert checked_keys == ["executor", "reviewer", "acceptor"], (
        f"互斥只许判执行/审查/验收三席，现在判的是 {checked_keys}"
    )
    assert "roleText(" in guard, "互斥要比 (厂商, 型号) 拼出来的串，不是比岗位名"
    assert "data-role-guard=\"true\"" in guard, "这一格的挂载点丢了"


# ---------------------------------------------------------------- 九


def test_an_unset_seat_renders_nothing_instead_of_a_placeholder():
    """未选模型的那一席留空 —— 不填占位型号，也不写「未选择」。

    接的旧意图：`test_the_top_bar_counters_come_from_the_one_graph_stats` 的另一半 ——
    旧判据是"缺读数就写 0 而不是留空"，这里是它的反面：**没有就真的没有**。
    owner 的原话是"未选择模型填空"：填空就是填空，多一个字都会让人以为那是系统给的默认值。
    """
    store = ts_code("src/lib/settingsStore.ts")
    assert 'return roleSet(role) ? `${normalizeVendor(role.vendor)} · ${role.model}` : "";' in store, (
        "`roleText()` 在未选时必须返回空串（显示名走 `normalizeVendor`，空 vendor 仍归一成空串）"
    )
    assert "export const UNSET_ROLE" in store, "「未选择」必须是一个合法状态，不是一个错误"
    assert 'export function roleSet(role: RoleSelection): boolean' in store

    guard = ts_code("src/components/studio/RoleGuard.tsx")
    assert "<dd>{text || \"\"}</dd>" in guard, (
        "未选的那一格必须真的是空的：写成 `—` / `未选择` / 默认型号都会让人以为那是系统给的"
    )

    # 受理卡上那一格写的是**状态**而不是型号：「执行席 · 未选模型」。
    # 这是允许的 —— 它说的是"这一席还没选"，不是"这一席是某个型号"。
    screen = ts_code("src/components/studio/StudioScreen.tsx")
    assert "执行席 · 未选模型" in screen, "受理卡没有交代「执行席还没选」这件事"

    # 设置里必须有一条"回到未选"的路，否则选错了厂商就再也回不去空状态
    modal = ts_code("src/components/SettingsModal.tsx")
    assert 'const UNSET_LABEL = "未选择";' in modal
    assert "vendor === UNSET_LABEL" in modal


# ---------------------------------------------------------------- 十


def test_studio_css_is_imported_before_the_tailwind_source():
    """`@import` 必须先于其它语句 —— 含 `@source`。

    这不是风格问题：CSS 规范要求 `@import` 出现在所有其它规则之前，写晚了整条被**丢弃**，
    表现是"工作室那一屏一点样式都没有"，而构建不报错。
    """
    css = read("src/index.css")
    imported = css.find('@import "./styles/studio.css";')
    assert imported != -1, "`index.css` 没有引入 `styles/studio.css`"
    # 按**行首**找：文件头的说明文字里也提着 `@theme`（那是在讲令牌口径），按子串找会把它当成规则
    for later in ("@source", "@theme", "@layer", "@utility"):
        at = re.search(rf"(?m)^{later}\b", css)
        if at is not None:
            assert imported < at.start(), f"`@import` 写在了 `{later}` 之后 ⇒ 整条会被浏览器丢弃"


# ---------------------------------------------------------------- 十一


def test_the_only_keyframes_are_lamp_breathe_and_card_in():
    """关键帧**名单是封闭的**，且每一支都不许把内容停在 `opacity: 0`。

    接的旧意图：`test_the_three_keyframes_are_named_and_none_stops_at_zero_opacity`（笔 AD 实测的隐形卡）。
    从原型搬来的那一份带两支：卡面入场的位移、指示灯呼吸。2026-10-28 加第三支 `node-run`
    （执行中的卡描边跑马灯，owner 第 4 条）—— 它只动 `stroke-dashoffset`，不碰 `opacity`，
    也不进 `prefers-reduced-motion` 那一档（文件头那条全局规则会把它一起关掉）。

    名单仍然是**枚举**而不是"只要不停在 0 就行"：多一支就要回答"它是干什么的、为什么不能再借一支"。
    """
    css = read(STUDIO_CSS)
    names = re.findall(r"@keyframes\s+([\w-]+)", css)
    assert sorted(names) == ["card-in", "lamp-breathe", "node-run"], f"关键帧名单漂了：{names}"

    for block in re.findall(r"@keyframes\s+[\w-]+\s*\{(.*?\})\s*\}", css, flags=re.S):
        assert "opacity:0" not in block.replace(" ", ""), (
            "关键帧里有 `opacity:0`：动画结束时若停在那一帧，内容就隐形了"
        )


# ---------------------------------------------------------------- 十二


def test_studio_css_borrows_every_colour_from_the_token_layer():
    """这份样式表里不许出现第二份颜色。

    接的旧意图：`test_the_tokens_have_one_source`。从原型搬来的那份 CSS 里，
    每一条颜色声明都必须走 `var(--…)`；允许的硬编码只有三处与主题无关的：
`#fff`（iframe 与色卡的白底）、`#000`（遮罩的叠色）、`transparent`。
    """
    css = read(STUDIO_CSS)
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    offenders: list[str] = []
    for match in re.finditer(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body):
        token = match.group(0)
        if token.lower() in {"#fff", "#ffffff", "#000", "#000000"}:
            continue
        offenders.append(f"{token} @ {body[: match.start()].count(chr(10)) + 1}")
    assert not offenders, "`studio.css` 里出现了令牌层之外的颜色：" + "、".join(offenders)


# ---------------------------------------------------------------- 十三

#: 2026-10-27 整批退役的文件。它们**不许**被活代码重新连上 —— 留着是为了让旧判据还能读，
#: 但只要没人 import，它们就不进产物。这份名单只许变短。
RETIRED = ()

_SPEC = re.compile(r"""\bfrom\s*["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)["']\s*\)""")


def _resolve(spec: str, here: Path) -> Path | None:
    if not spec.startswith("."):
        return None
    base = (here.parent / spec).resolve()
    for cand in (
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base / "index.ts",
        base / "index.tsx",
    ):
        if cand.is_file():
            return cand
    return None


def _live_files() -> set[Path]:
    """从 `main.tsx` 出发做一次可达性，得到"真的会进产物"的文件集合。"""
    start = SRC / "main.tsx"
    assert start.is_file(), "入口 `src/main.tsx` 不在了"
    seen: set[Path] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        text = cur.read_text(encoding="utf-8")
        for match in _SPEC.finditer(text):
            spec = match.group(1) or match.group(2)
            got = _resolve(spec, cur) if spec else None
            if got is not None:
                stack.append(got)
    return seen


def test_no_retired_file_is_reachable_from_the_entry():
    """退役文件不许被活代码重新连上。

    治理台整页删掉之后，最容易发生的事是"下一个人不知道它已经死了，又把某个面板挂回去"。
    这一条不看文件在不在，只看**它还能不能被走到** —— 那才是"删掉了"的实质。
    """
    live = {path.resolve() for path in _live_files()}
    assert len(live) > 15, f"可达文件只有 {len(live)} 个 ⇒ 走法本身坏了，这条判据没在量东西"

    reconnected = [rel for rel in RETIRED if (WEB / rel).resolve() in live]
    assert not reconnected, "这些退役文件又被活代码 import 回来了：" + "、".join(reconnected)


#: 2026-10-27 清理后基线：`src/` 下 25 个源文件全部可达，不可达 0 个。
#: 两个基线从 67 / 44 降到 0 是这一轮 Gen A–D 清理的读数 —— 它们只许变小，正好当进度条用。
#:
#: 这两个数字是**上限**，只许变小。它们防的不是"死代码存在"，是"**新的**死代码悄悄出现"：
#: 这一轮之前 `test_no_retired_file_is_reachable_from_the_entry` 只查 `RETIRED` 这份**手写白名单**，
#: 于是四个从没被 import 过的文件（`IntakeCard` / `MeetingCard` / `OutputsCard` / `DispatchCard`）
#: 在 `components/studio/` 里躺了一整轮没人发现 —— 它们和 `StudioScreen.tsx` 里的内联函数**同名**，
#: 谁去改那四个文件，屏幕一点变化都没有。这就是"手写名单"的漏洞：名单只记得住它写下的那些。
DEAD_BASELINE = 0
UNLISTED_DEAD_BASELINE = 0


def test_no_new_orphan_source_file_appears():
    """从入口走不到的源文件只许减少，不许增加。

    上一轮补这条判据的理由很具体：`components/studio/` 下四个**从未被 import** 的组件
    （`IntakeCard` / `MeetingCard` / `OutputsCard` / `DispatchCard`）与 `StudioScreen.tsx` 里的
    内联函数同名，整整一轮没人发现。改它们的人（尤其是 Agent）会以为改好了，实际屏幕上什么也没变。

    做法：用 `_live_files()` 算真可达性，与 `src/` 下的全部源文件求差。差集数量只许 ≤ 基线。
    新增一个没人 import 的文件会让差集 +1，当场红 —— 那时只有两条路：把它接上，或者删掉它。
    已登记的退役文件（`RETIRED`）不计入 `UNLISTED_DEAD_BASELINE`，它们是"有账的死"。
    """
    live = {path.resolve() for path in _live_files()}
    all_src = {p.resolve() for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx")}
    dead = sorted(all_src - live)
    # 2026-10-27 清理后 dead 应当为空：这时"量具自检"改量**可达集本身**够不够大，
    # 而不是要求差集非空 —— 否则"清干净了"会被判成"走法坏了"。
    assert len(live) >= 20, f"可达文件只有 {len(live)} 个 ⇒ 走法本身坏了，这条判据没在量东西"

    assert len(dead) <= DEAD_BASELINE, (
        f"不可达源文件 {len(dead)} 个，超过基线 {DEAD_BASELINE} ⇒ 有新长出来的孤儿文件。"
        "从入口走不到的组件改了不会有任何效果："
        + "、".join(p.relative_to(WEB).as_posix() for p in dead)
    )

    unlisted = [p for p in dead if p.relative_to(WEB).as_posix() not in set(RETIRED)]
    assert len(unlisted) <= UNLISTED_DEAD_BASELINE, (
        f"没登记的不可达文件 {len(unlisted)} 个，超过基线 {UNLISTED_DEAD_BASELINE} ⇒ "
        "新增的孤儿要显式登记进 `RETIRED`（承认它死了）或者删掉："
        + "、".join(p.relative_to(WEB).as_posix() for p in unlisted)
    )


def test_the_studio_component_folder_has_no_orphan():
    """`components/studio/` 是当前工作区，这里**一个孤儿都不许有**。

    比上一条更严：上一条给整个 `src/` 留了历史欠账，这一条不给。理由是这个目录正在被频繁改动，
    而且同名文件最容易在这里长出来（`StudioScreen.tsx` 里就是内联的 Intake/Meeting/Outputs/Dispatch）。
    """
    live = {path.resolve() for path in _live_files()}
    folder = SRC / "components" / "studio"
    assert folder.is_dir(), "`components/studio/` 不在了 ⇒ 这条判据的对象没了"
    orphans = sorted(
        p.relative_to(WEB).as_posix()
        for p in folder.rglob("*.tsx")
        if p.resolve() not in live
    )
    assert not orphans, (
        "这些组件从入口走不到（改了不会有任何效果）：" + "、".join(orphans)
        + "　要么接上，要么删掉 —— 别留同名僵尸。"
    )


# ---------------------------------------------------------------- 十四


def test_the_studio_screen_keeps_the_prototype_four_cards():
    """工作室屏**永远**是四张卡：受理 / 会议桌 / 产出 / 派单，各挂一次。

    这一条守的是"1:1 复刻"这件事的骨架：形状由 `studio.css` 保证（那份 CSS 是从原型机械搬来的），
    但四张卡**各是什么**由 JSX 决定，CSS 管不着。少一张就是"看起来像"，不是复刻。

    这一屏一天里改过五版，判据跟着改了五次 —— 记下来是因为**每次错的都不是同一件事**：

      ① 四张卡 + 五句说明（原型原样）。判据钉"四张都在"，同时钉那五句说明文字在。
      ② 只留受理卡（重设计）。判据改成"未立项只有一张"：理由是三张空卡等权重、
         只有一张能点，顺序只能靠五句话讲五遍。**这个判断当时是对的**，错的是下一版。
      ③ `.studio-solo`：整屏居中一张 820 的表单。owner：「受理需求也没必要单独做出来一页」。
      ④ `.intake-pane`：进了主区，但 `justify-content:center` 让它浮在主区正中间，
         而立项之后这张卡明明在最上面。owner 又指了一次：「移到这个位置」。
      ⑤ 现在（owner 给了原型图，说「主页面以此图为准」）：**四张卡都回来**，共用同一个
         `.studio-grid`，位置在立项前后一个像素不动。②的判断用另一种方式兑现 ——
         三张空卡的空态各说**一件不同的事**（受理：你该怎么写 / 会议：你写完会发生什么 /
         产出：这块地方将来装什么 / 派单：为什么按钮是灰的），不再是同一句话的五个副本。

    所以判据不再钉"未立项有没有某个容器"（那正是 ③④ 出问题的地方：容器一变位置就变），
    改成钉**没有分支**：
      - `StudioScreen` 里不许出现 `if (!studio.plan)` —— `!plan` 只活在四张卡内部各自渲染空态，
        函数体里判一次就会分叉出两套布局，而那正是"位置会飘"的产地；
      - 四张卡在函数体里各挂**恰好一次**；
      - 网格只有一个 `className="studio-grid"`；
      - 不许出现 `<SidePanel`（右栏是执行看板的，工作室这一屏从来没有它 —— 上一版把 `.board`
        搬进来，立项那一刻右栏会凭空消失）。
    """
    screen = ts_code("src/components/studio/StudioScreen.tsx")
    at = screen.find("export function StudioScreen(")
    assert at != -1, "`StudioScreen` 组件没了"
    body = screen[at:]

    assert "if (!studio.plan)" not in body, (
        "`StudioScreen` 里又按 `!studio.plan` 分叉了。四张卡在两种状态下都在，"
        "`!plan` 只该活在每张卡内部（各自渲染空态）—— 函数体里判一次，就会长出两套布局，"
        "立项那一刻位置会跳（③④ 两版都是这么错的）"
    )

    for component in ("<IntakeCard", "<MeetingCard", "<OutputsCard", "<DispatchCard"):
        assert body.count(component) == 1, (
            f"{component} 在工作室屏里必须恰好挂一次（现在 {body.count(component)} 次）"
        )

    assert body.count('className="studio-grid"') == 1, (
        "四卡网格必须只有一份。两份 = 又分叉成「未立项一套、已立项一套」，"
        "而那两套的卡片顶边一定会差几个像素"
    )
    assert "<StudioHead" in body, (
        "没挂 `StudioHead` ⇒ `<h1 class=\"sr-only\">` 就在那一行里，整页会没有标题"
    )
    assert "<SidePanel" not in body, (
        "工作室屏挂上了右栏。右栏（六个页签读任务 / 产物 / 门禁读数）是**执行看板**的，"
        "挂在这里，切屏那一刻它会凭空出现 / 消失"
    )

    # 四张卡的空态各说一件事 —— 这是 ⑤ 用来说明"四张空卡不是抢权重"的依据，
    # 也是"四张卡都回来"这个决定的**前提**：只要有一张卡在未立项时不解释自己为什么是空的，
    # 四张卡就退回到 ① 那个"要靠五句话补救"的形态。
    for owner, marker in (
        ("IntakeCard", "客户原话，越口语越好"),
        ("MeetingCard", "会议室待启动"),
        ("OutputsCard", "还没有产出"),
        ("DispatchCard", "方案未锁定"),
    ):
        assert marker in screen, f"{owner} 的未立项空态丢了（找不到「{marker}」）"

    css = read(STUDIO_CSS)
    assert ".studio-root .intake-pane" not in css, (
        "`.intake-pane` 的样式又回来了。那个容器存在过两版，两版都是在给「位置」打补丁 ——"
        "位置本来就由 `.studio-grid` 定义，不需要第二个人来说一遍"
    )

    board = ts_code("src/components/studio/BoardScreen.tsx")
    for part in ('className="stage-bar"', 'className="dag-wrap"', "<Dag ", "<NowStrip "):
        assert part in board, f"执行看板少了 `{part}`"


# ---------------------------------------------------------------- 十五


def test_the_notice_lives_in_the_top_bar_and_never_covers_the_canvas():
    """提示条挂在顶栏中段，不许回到画布上的浮层。

    原型那条 toast 是**底部居中**的浮层，搬进产品后正好压在执行看板底部的
    「现在 · 依赖就绪 / 进度 %」上 —— 而派单、QA 打回、兜底触发这些提示，
    恰恰发生在你盯着那条进度的时候。所以它换了个位置：顶栏中段（面包屑与右侧两颗键之间）
    本来就是空的，放这里谁都不挡，位置也固定，不用去猜用户在看哪一块。

    钉住的三件事：① 它在 `TopBar` 里、不在 `StudioView` 的浮层里；
    ② 位置是绝对定位在顶栏内、`pointer-events:none`（它不许抢点击）；
    ③ 单行 + 省略号（`white-space:nowrap`）—— 换行会长高，48px 的顶栏装不下两行。
    """
    topbar = ts_code("src/shell/TopBar.tsx")
    assert 'className="top-notice"' in topbar, "提示条不在顶栏里"
    assert "studio.toast" in topbar, "顶栏没有读引擎的 toast 状态"

    view = ts_code("src/views/StudioView.tsx")
    assert "toast-wrap" not in view, "`StudioView` 里还留着 toast 浮层 ⇒ 提示会重新压到画布上"
    assert 'className="toast"' not in view

    css = read(STUDIO_CSS)
    assert ".studio-root .toast-wrap" not in css, "底部浮层那两条样式还在 ⇒ 位置会被改回去"
    rule = css_rule(".studio-root .top-notice")
    for decl in ("position:absolute", "top:11px", "pointer-events:none", "white-space:nowrap"):
        assert decl in rule, f"`.top-notice` 少了 `{decl}`"

    # 居中**不许用 transform**：入场动画 `card-in` 动的就是 transform，两个一起写
    # 会在第一帧把提示从中间甩到左边再弹回来。
    assert "transform" not in rule, "`.top-notice` 用 transform 居中会和 `card-in` 打架"
    assert "margin:0 auto" in rule and "width:fit-content" in rule, "居中要靠 margin:auto + fit-content"


# ---------------------------------------------------------------- 十六


def test_the_right_rail_carries_a_way_back_home_outside_the_tab_body():
    """执行看板这一屏把整块画布都占了，右栏必须有一条**不随页签走**的"回工作室"的路。

    原来的唯一出口是左栏那扇门 —— 它排在 12 个工位的最上面。人在右栏读交付清单时，
    目光与鼠标都在右边，出口却在对角线的另一头。所以这条钉在右栏底部：
    ① 它在 `SidePanel` 里、在 `.side-body`（滚动盒）**之外** —— 塞进某一个页签就会在另外五个页签漏掉；
    ② 它调的是 `setScreen("studio")`，不是往 hash 里塞第二个视图；
    ③ `.side-foot` 是 `flex:none` 且带 `border-top` —— 它得读起来像"栏的底座"，不是又一条内容。
    """
    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert 'className="side-foot"' in panel, "右栏底部没有那条出口"
    assert 'className="ghost side-home"' in panel, "回首页的按钮没有复用次按钮那一档样式"
    assert 'setScreen("studio")' in panel, "回首页调的不是 `setScreen(\"studio\")`"

    # 位置：`.side-foot` 必须在 `.side-body` 的闭合之后（同一层、兄弟节点），不在它的里面
    body_at = panel.find('className="side-body"')
    foot_at = panel.find('className="side-foot"')
    assert body_at != -1 and foot_at != -1
    body_end = panel.find("</div>", panel.find("DeliverTab", body_at))
    assert body_end != -1 and foot_at > body_end, (
        "`.side-foot` 被塞进了 `.side-body` 里 ⇒ 它会跟着内容一起滚走，甚至只在某个页签里出现"
    )

    rule = css_rule(".studio-root .side-foot")
    for decl in ("flex:none", "border-top", "background:"):
        assert decl in rule, f"`.side-foot` 少了 `{decl}`"


def test_no_tab_carries_its_own_way_back_home():
    """六个页签里一个都不许各写一份"回首页"。

    这是上一条的反面：出口只许有一个产地。页签里再写一颗，就会出现"详情页的按钮回到工作室、
    交付页的按钮回到别处"这种两份真相。
    """
    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert panel.count('setScreen("studio")') == 1, (
        f"`setScreen(\"studio\")` 出现了 {panel.count('setScreen(\"studio\")')} 次 ⇒ 出口有了第二个产地"
    )
    assert panel.count("side-home") == 1, "`side-home` 这个类名出现了不止一次 ⇒ 出口有了第二个产地"


# ---------------------------------------------------------------- 十七、命名空间不相交（2026-10-27 第四轮）


def test_no_utility_shadows_a_studio_class():
    """`index.css` 的 `@utility` 名不许与 `studio.css` 里的类名重名。

    这不是洁癖，是 2026-10-27 实测出来的两个真缺陷：

    ① `@utility board`（给已退役的指挥台写的，`max-width:1560px; margin-inline:auto`）与工作室内核的
       `.board`（执行看板那张两列网格）撞名。Tailwind 的 `@utility` 是**全局**的，`<div className="board">`
       于是同时吃到 `margin-inline:auto`；在 flex 列容器里，自动外边距让子项从 stretch 退化成
       shrink-to-fit 并居中 —— 实测 1920 视口 `main` 1674px、看板只有 1046px，**左右各空 314px**；
       1440 各空 74px；1280 恰好相等。所以它在所有人工检查里都没显形，只在宽屏上白扔三分之一。
    ② `@utility lamp` 与 `.studio-root .lamp`（左栏门与十二张座位右侧那颗 6px 灯）撞名。
       studio 那侧没写 `box-shadow`，于是 `0 0 12px currentColor` 漏了进来，**十二颗待命灯全带一圈墨色光晕**，
       读成"都亮着"；`display:inline-block` 也是从那里漏进来的，等于 studio 的灯一直在借别人的骨架长。

    两条都改成了不撞的名字（`board-column` / `hud-lamp`）。这条判据防的是第三处：
    下一次有人给设计系统加 `@utility`，名字正好撞上原型搬来的某个类，屏幕上就会再长出一层看不见的继承。

    为什么不反过来在 studio 侧补 `margin-inline:0` / `box-shadow:none`：那是把冲突藏起来，
    下一个同名类还会踩同一个坑，而且"这个属性为什么写在这里"在代码里读不出来。
    """
    utilities = set(re.findall(r"@utility\s+([a-zA-Z][\w-]*)", read("src/index.css")))
    assert utilities, "index.css 里一个 `@utility` 都没读到 ⇒ 正则或文件结构变了"
    classes = set(re.findall(r"\.([a-zA-Z][\w-]*)", read(STUDIO_CSS)))
    assert classes, "studio.css 里一个类名都没读到 ⇒ 正则或文件结构变了"

    shadowed = sorted(utilities & classes)
    assert not shadowed, (
        "这些 `@utility` 名与工作室内核的类名撞了，屏幕上会多出一层看不见的继承："
        + "、".join(shadowed)
        + "　改法是把 utility 改到语义更准的族里（`board-column` / `hud-*`），"
        "不要反过来在 studio 侧补属性去盖。"
    )


def test_the_board_grid_is_not_pushed_into_shrink_to_fit():
    """看板那张网格必须铺满中栏，不许被自动外边距压成"收缩并居中"。

    上一条钉的是"不许撞名"，这一条钉的是**后果**：即使将来换了别的办法把 `margin-inline:auto`
    带进来（继承、`@layer`、另一个同名 utility），这里也会红。

    实测基线（修好之后）：1920 视口 `.main` 1674、`.board` 1674；`margin-left/right` 都是 0。
    修之前是 1046 与 314/314。
    """
    rule = css_rule(".studio-root .board")
    assert "grid-template-columns" in rule, "`.board` 不再是两列网格 ⇒ 这条判据的对象没了"
    for forbidden in ("margin-inline", "margin-left", "margin-right", "width:fit-content", "max-width"):
        assert forbidden not in rule, (
            f"`.board` 里出现了 `{forbidden}` ⇒ 看板会被压成收缩并居中，宽屏上两侧留死白"
        )


def test_no_engine_path_steals_the_side_tab():
    """右栏页签只许被**用户**改：点击页签、或在明确意图下打开详情。引擎自己的业务推进不许碰它。

    2026-10-27 之前有两处引擎路径会改 `tab`：

    - `qaFail()`：`if (!state.selected) state.tab = "review"` —— 打回时把面板翻到「审查」；
    - 交付生成处：`if (!state.selected) state.tab = "deliver"`。

    那句 `!selected` 看起来是"留了余地"（读者正在看某张卡就不抢），但 `selected` 只在**点过 DAG 节点**
    之后才非空 —— 也就是说只要用户没点过节点，页签就一定被抢。实测：派单后 1.5 秒，面板自己跳到「审查」，
    而读者此刻可能正在读「文件总线」；翻回去之后下一次审查还会再抢一次。

    2026-10-29（B3）再拆一层：`selectTask` 只负责选中，**不再翻页**。
    点 DAG 节点时如果用户正在读「审查/交付」，只选中 + 顶栏提示；只有 `openTaskDetail`
    （详情页「前往处理」或用户明确要看详情）才把页签切过去。
    这条判据钉三件事：
    ① `state.tab =` 只许出现在 `setSideTab` / `openTaskDetail` / `startProject` 三处；
    ② `selectTask` 体内不许出现 `state.tab`；
    ③ `openTaskDetail` 必须真的切到 detail，`goHandleTask` 必须走它；
    ④ `unread` 必须真的被置位（否则角标不亮，等于把"提示"一起删了，用户就永远不知道有结论）。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    assigns = [m.start() for m in re.finditer(r"state\.tab\s*=", engine)]
    owners = []
    for pos in assigns:
        last = None
        for m in re.finditer(r"(?:export )?function (\w+)", engine[:pos]):
            last = m.group(1)
        owners.append(last)
    # 三处，各有理由：
    #   setSideTab    —— 用户点了页签（唯一正常的入口）
    #   openTaskDetail—— 明确要看某张卡的详情（「前往处理」/ 详情意图）
    #   startProject  —— 立项时把面板归位到「任务详情」。它不是业务推进，是初始化：
    #                    此刻 reviews / gate / deliver 都刚被清空，面板上没有任何内容可读，
    #                    归位不会夺走读者正在看的东西。
    assert sorted(owners) == ["openTaskDetail", "setSideTab", "startProject"], (
        f"改 `state.tab` 的函数是 {sorted(owners)} ⇒ 只许这三处；"
        "`selectTask` 只负责选中，翻页只由 `openTaskDetail` 承担"
    )

    def fn_body(name: str) -> str:
        at = engine.find(f"export function {name}(")
        assert at != -1, f"引擎里找不到 `export function {name}`"
        brace = engine.find("{", at)
        depth = 0
        for pos in range(brace, len(engine)):
            if engine[pos] == "{":
                depth += 1
            elif engine[pos] == "}":
                depth -= 1
                if depth == 0:
                    return engine[brace : pos + 1]
        raise AssertionError(f"`{name}` 的函数体没有闭合")

    assert "state.tab" not in fn_body("selectTask"), (
        "`selectTask` 又在改页签了 ⇒ 点 DAG 节点会再次夺走读者正在读的「审查/交付」"
    )
    assert 'state.tab = "detail"' in fn_body("openTaskDetail"), (
        "`openTaskDetail` 没有切到 detail ⇒ 「前往处理」会停在原页签，按钮承诺的事没发生"
    )
    assert "openTaskDetail(id)" in fn_body("goHandleTask"), (
        "`goHandleTask` 没走 `openTaskDetail` ⇒ 交付清单的出口又变成只选中、不翻页"
    )

    assert "state.unread.review = true" in engine, "打回不再点亮「审查」角标 ⇒ 提示和抢页签一起被删掉了"
    assert "state.unread.deliver = true" in engine, "交付不再点亮「交付」角标"
    assert 'state.unread[tab] = false' in engine, "进入页签时没有清角标 ⇒ 角标永远亮着"


# ---------------------------------------------------------------- 十八、归属标签只有一个缩写产地（2026-10-27 第五轮）


def test_the_agent_label_has_one_abbreviation():
    """"这个工位是谁"在界面上只许有**一套**两字缩写。

    2026-10-27 实测：同一屏（执行看板）里同一个角色有四种写法，用户在两栏之间对不上号 ——

    | 概念       | 左栏 / 会议 / DAG | 活动流 / 审查 / 文件总线 | 交付页     |
    |------------|-------------------|--------------------------|------------|
    | UI 设计师  | 设计              | **UI**                   | 前端       |
    | 独立审查员 | 审查              | **独立**                 | 文档（按 .md 猜） |

    第二列是最坏的那种：`"独立审查员".slice(0, 2)` 得到"独立"、`"UI 设计师".slice(0, 2)` 得到"UI" ——
    既不是缩写也不是职称，是从长字符串上切下来的一块碎片。第三列更糟：按扩展名反推作者，
    等于让文件后缀去猜"谁写的"。

    规则收敛成两条：
      · 窄列（活动流作者列、审查卡角标、文件总线归属列、交付清单归属列）→ `agentMono()`
      · 句子（"出自 独立审查员"）→ `agentTitle()`

    这条判据钉三件事：`agentMono` 是唯一产地；`slice(0, 2)` 这种派生一处都不许有；
    交付页不许再按扩展名猜归属。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    assert "export function agentMono(id: string): string {" in engine, (
        "`agentMono` 不在引擎里 ⇒ 两字缩写又散回各处了"
    )

    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert ".slice(0, 2)" not in panel, (
        "`SidePanel` 里又出现了 `.slice(0, 2)` ⇒ 归属标签回到了「从职称上切两个字」那套"
    )
    assert 'endsWith(".py")' not in panel, (
        "交付页又在按扩展名猜归属（`.py` → 后端）⇒ 同一个文件在两栏里会有两个作者"
    )
    # 正向：四处窄列都真的走了 agentMono
    assert panel.count("agentMono(") >= 4, (
        f"`agentMono(` 只出现 {panel.count('agentMono(')} 次 ⇒ 活动流 / 审查 / 总线 / 交付 没都换过来"
    )


# ---------------------------------------------------------------- 十九、浮层与页签的键盘/读屏合同（2026-10-27 第五轮）


def test_the_tab_strips_are_real_tablists():
    """两处页签都要是 `tablist`，不许是"六个长得像页签的按钮"。

    2026-10-27 之前侧栏六个页签、产出四个页签一个 `role` 都没有：读屏读到的是
    "任务详情 / 活动流 / 审查 / 文件总线 / 用量 / 交付"六个没有状态的按钮 ——
    既不知道"我现在在哪一页"，也不知道"审查那一页有没有新东西"（`!` 角标是纯视觉的）。
    阶段条同理：四条进度只是 `div` 里一条被撑开的 `i`，`3/3` 与 `0/1` 的差别没有第二个通道。

    这条判据钉四件事：两处 tablist、每个 tab 带 `aria-selected`、
    阶段条带 `aria-current`、进度条带 `progressbar` 与 `aria-valuenow`。
    """
    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert 'role="tablist"' in panel, "右栏六个页签不是 tablist"
    assert 'role="tab"' in panel, "页签按钮没有 `role=\"tab\"`"
    assert "aria-selected={studio.tab === tab.k}" in panel, "页签没有把选中态报给读屏"
    assert 'role="tabpanel"' in panel, "页签内容区不是 tabpanel ⇒ tab 没有可指向的面板"

    screen = ts_code("src/components/studio/StudioScreen.tsx")
    assert 'role="tablist"' in screen, "产出四个页签不是 tablist"
    assert "aria-selected={studio.otab === tab.k}" in screen, "产出页签没有把选中态报给读屏"

    board = ts_code("src/components/studio/BoardScreen.tsx")
    assert "aria-current={studio.phase === phase" in board, "阶段条没报出「现在是哪一阶段」"
    assert 'role="progressbar"' in board, "阶段进度条不是 progressbar"
    assert "aria-valuenow={percent}" in board, "进度条没报出数值 ⇒ 读屏只知道「有个进度条」"


def test_the_drawer_is_reachable_and_leavable_by_keyboard():
    """抽屉的键盘合同：Esc 关、开时焦点进来、关时焦点还回去、关着时不可达。

    原来三条一条都没有 —— 同一个应用里设置弹窗处理了 Esc，抽屉没有；
    抽屉关着的时候只是 `transform:translateX(102%)` 移出屏幕，而 **transform 不影响可聚焦性**，
    所以键盘按 Tab 仍然会走进那个 18×19 的 `×`，读屏也仍然读得到一个空 dialog。
    """
    view = ts_code("src/views/StudioView.tsx")
    assert 'event.key === "Escape"' in view, "抽屉不能用 Esc 关闭"
    assert "closeDrawer()" in view, "Esc 没有真的关抽屉"
    assert "drawerRef.current?.focus()" in view, "打开抽屉时焦点没有进来"
    assert "restoreRef.current?.focus()" in view, "关闭抽屉时焦点没有还回原处"
    assert "aria-hidden={studio.drawer ? false : true}" in view, (
        "抽屉关着时没有对读屏隐藏 ⇒ 屏幕外那个空 dialog 仍然在可访问树里"
    )

    rule = css_rule(".studio-root .drawer")
    assert "visibility:hidden" in rule, (
        "关闭态只有 `transform` ⇒ 移出屏幕但仍可聚焦，Tab 会走进看不见的抽屉"
    )


def _mix_hex(foreground: str, background: str, alpha: float) -> str:
    """`color-mix(in srgb, fg alpha%, bg)` 的结果色。"""
    f = [int(foreground[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * alpha + y * (1 - alpha)):02x}" for x, y in zip(f, b))


def test_the_dag_edges_clear_the_non_text_contrast_floor():
    """DAG 连线的对比度要 ≥ 3:1（WCAG 1.4.11 对"理解内容所必需的图形"）。

    这是这一版唯一一处**不达标**的对比度 —— 文字那一侧是系统性的（令牌里 7 张底 × 6 个前景逐对算过，
    最低 4.91，判据在守），只有 SVG 连线不在那套矩阵里，因为它是 `stroke` 不是 `color`。

    实测（2026-10-27 修之前）：未完成态的边是 `ink@12%`，叠在 `#0E121A` 上合成 rgb(40 44 52)，
    对底色 **1.35:1** —— 几乎看不见。已完成态是绿的（4.65:1），一直达标。
    也就是说"已经走过的路"看得见、"还没走的路"看不见，而那恰恰是用户最需要预判的部分。

    这条判据不只看百分比，而是**按两套主题各自的底色算一遍真实对比度** ——
    因为同一份 alpha 在两套主题里不是同一个可见度（深色 36% 够，浅色要 50% 才够）。
    """
    import json

    from test_board_frontend import contrast_ratio

    tokens = json.loads(read("src/styles/tokens.json"))
    dark = tokens["color"]
    light = tokens["light"]["color"]

    cases = (
        (".studio-root .e", dark["ink"], dark["canvas-hi"], 3.0),
        ('[data-theme="light"] .studio-root .e', light["ink"], light["canvas-hi"], 3.0),
    )
    for selector, ink, bg, floor in cases:
        rule = css_rule(selector)
        found = re.search(r"color-mix\(in srgb,var\(--ink\) (\d+)%,transparent\)", rule)
        assert found, (
            f"`{selector}` 的描边不再是「ink 百分比叠透明」⇒ 这条判据量不到。"
            "注意：连线不许借 `--line-strong`（那条是 12% 的分隔线，给表格线和卡片边用的）"
        )
        alpha = int(found.group(1)) / 100
        ratio = contrast_ratio(_mix_hex(ink, bg, alpha), bg)
        assert ratio >= floor, (
            f"`{selector}` 的连线对比度 {ratio:.2f}:1，低于 {floor}:1 —— "
            f"ink {alpha:.0%} 叠在 {bg} 上合成 {_mix_hex(ink, bg, alpha)}。"
            "这条线是这张图的主干（谁依赖谁全靠它读），不是装饰"
        )

# ---------------------------------------------------------------- 十九


#: 五档字号的名字与顺序。**顺序即层级**：从符号到页面标题，只能单向递增。
TYPE_TIERS = ("micro", "meta", "body", "title", "hero")


def _token_type_scale() -> dict[str, float]:
    """`tokens.json` 的 type 块。缺键就是失败 —— 少了那一档，`var(--fs-x)` 会静默回退到继承字号。"""
    import json

    tokens = json.loads(read("src/styles/tokens.json"))
    assert "type" in tokens, "tokens.json 没有 type 块 ⇒ 五档字号失去唯一产地"
    scale = tokens["type"]
    assert tuple(scale) == TYPE_TIERS, (
        f"type 块的键是 {tuple(scale)}，应为 {TYPE_TIERS} —— 顺序即层级，不许调换或增删"
    )
    return {k: float(v) for k, v in scale.items()}


def _theme_type_scale() -> dict[str, float]:
    """`index.css` 的 `@theme` 里那五个 `--fs-*`。"""
    found = dict(re.findall(r"--fs-([a-z]+):\s*([\d.]+)px", read("src/index.css")))
    assert found, "index.css 的 @theme 里没有 `--fs-*` ⇒ 工作室那一屏全部回退到继承字号"
    return {k: float(v) for k, v in found.items()}


def test_the_studio_has_exactly_five_type_sizes():
    """字号只许五档，且三处同源（tokens.json ↔ @theme ↔ studio.css 的 var 用量）。

    收敛前 `studio.css` 里有 8.5 / 9 / 9.5 / 10 / 10.5 / 11 / 11.5 / 12 / 12.5 / 13 / 14 / 15
    共十二个离散值：最大 15、正文 11.5，跨度只有 1.3 倍。每加一个字号等于在说"这里重要一点点"，
    但 0.5px 的差读者感知不到 —— 结果不是"层级细腻"，是"层级不存在"，只剩一片同样大小的字。

    为什么这条要量三处而不是一处：`tokens.json` 是产地、`@theme` 是运行时的出口、
    `studio.css` 是消费者。任一处改名（比如 `body` 写成 `base`）都不会报错 ——
    `var(--fs-base)` 解析失败时 CSS 的行为是**整条声明被丢弃**，字号静默回退到父级继承。
    屏上看起来"还行"，于是没人发现那一档已经没了。这条判据就是钉住那个静默。
    """
    tokens = _token_type_scale()
    theme = _theme_type_scale()

    assert set(theme) == set(tokens), (
        f"@theme 有 {sorted(theme)}，tokens.json 有 {sorted(tokens)} —— 两边键集必须相同"
    )
    for key, value in tokens.items():
        assert theme[key] == value, (
            f"`--fs-{key}` 在 @theme 里是 {theme[key]}px，tokens.json 里是 {value}px。"
            "留两份的后果是「改一处不生效」，量的是 A、屏上是 B"
        )

    css = read(STUDIO_CSS)

    bare = re.findall(r"font-size:\s*([\d.]+)px", css)
    assert not bare, (
        f"`{STUDIO_CSS}` 里还有 {len(bare)} 处裸 px 字号（{sorted(set(bare))}）—— "
        "字号只能从五档令牌借，写死的那一刻就又长出一个离散值"
    )

    used = re.findall(r"var\(--fs-([a-z]+)\)", css)
    assert used, f"`{STUDIO_CSS}` 一处都没借 `var(--fs-*)` ⇒ 五档没落地"
    unknown = sorted(set(used) - set(TYPE_TIERS))
    assert not unknown, (
        f"`{STUDIO_CSS}` 借了不存在的档位 {unknown} —— `var(--fs-{unknown[0]})` 解析失败，"
        "那一处的字号会静默回退到父级继承"
    )

    # 正文必须是主体。若某天有人把成段的文字塞进 micro 换密度，这条会先红。
    counts = {tier: used.count(tier) for tier in TYPE_TIERS}
    assert counts["body"] == max(counts.values()), (
        f"用得最多的一档是 {max(counts, key=counts.get)}（{counts}）—— 正文才该是主体。"
        "micro 只放符号与单字符（勾选的勾、页签角标、色板 hex），不是「更小的正文」"
    )
    assert counts["micro"] <= 6, (
        f"micro 档被用了 {counts['micro']} 次 —— 9px 只够放单字符，多出来的都在拿可读性换密度"
    )


def test_the_type_scale_actually_has_a_span():
    """五档之间要拉得开。收敛的目的是让层级**看得见**，不是把十二个数换成五个数。

    跨度 < 1.6 倍时，最小档和最大档在 1080p 上只差几个像素 —— 那等于换了个写法的老问题。
    body 不低于 12px：中文 11.5px 在 1x 屏上笔画会糊，这是下限而不是偏好。
    """
    scale = _token_type_scale()
    span = scale["hero"] / scale["micro"]
    assert span >= 1.6, (
        f"最大档 / 最小档 = {scale['hero']} / {scale['micro']} = {span:.2f}，不到 1.6 倍 ⇒ 层级看不见"
    )
    assert scale["body"] >= 12, (
        f"正文档是 {scale['body']}px —— 中文正文低于 12px 时笔画会糊成一团，这是下限"
    )
    ordered = [scale[t] for t in TYPE_TIERS]
    assert ordered == sorted(ordered) and len(set(ordered)) == len(ordered), (
        f"五档必须严格递增，实际是 {dict(zip(TYPE_TIERS, ordered))}"
    )

def test_no_component_writes_an_inline_pixel_type_size():
    """`studio.css` 干净了不代表整屏干净：`style={{fontSize: 11}}` 能绕过那份表。

    收敛前有 7 处这样绕过去的内联值（SidePanel 2、StudioScreen 3、UsageTab 2），
    值是 10 / 10.5 / 11 / 11.5 / 12 —— 又是五个离散值，而它们不在任何一份令牌表里，
    改档位时不会跟着动。判据扫的是 `web/src` 下全部 .ts/.tsx：
    内联字号只许写成 `var(--fs-*)`，不许出现数字字面量。
    """
    offenders = []
    for path in sorted(SRC.rglob("*.ts*")):
        if path.name.endswith(".d.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"fontSize:\s*[\d.]", line) or re.search(r"fontSize=\{\s*[\d.]", line):
                offenders.append(f"{path.relative_to(WEB)}:{lineno}  {line.strip()[:60]}")
    assert not offenders, (
        "内联字号写了数字字面量，绕过了五档令牌：\n  " + "\n  ".join(offenders) + "\n"
        "改成 `var(--fs-body)` / `var(--fs-meta)` 之类；SVG 图元另有 NODE_META / NODE_TITLE 两档"
    )


def test_the_node_figure_has_two_type_sizes():
    """DAG 节点里的 `<text>` 用两档，不是四个。

    这六个 `fontSize` 是 **SVG 用户单位**：整张图套在 viewBox 里等比缩放，`fontSize={10}`
    落到屏上是 10 × 缩放比，和 `--fs-meta: 10.5px` 不是一个坐标系 —— 所以它不并进五档，
    而是自己留两档常量。理由写在 BoardScreen.tsx 顶部。

    但"不并进五档"不等于"可以写裸数字"：收敛前这里是 9.5 / 10 / 10.5 / 12 四个值，
    0.5 的差经过缩放连一个像素都不到，四个数只发出两个信号。这条判据钉住那两档常量。
    """
    code = ts_code("src/components/studio/BoardScreen.tsx")

    defined = dict(re.findall(r"const (NODE_META|NODE_TITLE) = ([\d.]+);", code))
    assert set(defined) == {"NODE_META", "NODE_TITLE"}, (
        f"BoardScreen.tsx 只定义了 {sorted(defined)}，应为 NODE_META / NODE_TITLE 两个常量"
    )
    meta, title = float(defined["NODE_META"]), float(defined["NODE_TITLE"])
    assert meta < title, f"NODE_META({meta}) 必须小于 NODE_TITLE({title}) —— 顺序即层级"

    used = re.findall(r"fontSize=\{(NODE_META|NODE_TITLE|[\d.]+)\}", code)
    assert used, "BoardScreen.tsx 里一个 `fontSize={...}` 都没有 ⇒ 这条判据量不到"
    literal = sorted({v for v in used if not v.startswith("NODE_")})
    assert not literal, (
        f"节点里有裸数字字号 {literal} —— 图元字号只能引用 NODE_META / NODE_TITLE，"
        "写死的那一刻就又长出一个离散值"
    )
    assert set(used) == {"NODE_META", "NODE_TITLE"}, (
        f"节点里用到的档位是 {sorted(set(used))} —— 图元只有两档，"
        "第 3 个档位出现说明有人把标注拆成了「更小的标注」"
    )

# ---------------------------------------------------------------- 二十


def _layout_tokens() -> dict[str, int]:
    """`tokens.json` 的 layout 块。五个数各有出处，缺一个就没法做算式。"""
    import json

    tokens = json.loads(read("src/styles/tokens.json"))
    assert "layout" in tokens, "tokens.json 没有 layout 块 ⇒ 宽度这件事失去唯一产地"
    layout = tokens["layout"]
    for key in ("min_width_px", "rail_px", "side_px", "side_stacks_below_px", "dag_width_px", "dag_gutter_px", "board_gap_px"):
        assert key in layout, f"layout 缺 `{key}` ⇒ 断点与宽度的算式缺一项"
    return {k: int(v) for k, v in layout.items()}


def test_the_min_width_is_declared_and_enforced():
    """最小宽度必须**声明**，而不是让它烂掉。

    这块屏的宽度不是弹性的：DAG 的 SVG 固定 `dag_width_px`（节点坐标是算出来的，不是流式排的），
    而图上的字只有 10~12 个用户单位 —— 一缩放就看不清。375 视口下主区只有 129px，
    把 12 个 agent 的 DAG 压成一条缝不叫适配。

    做法是三条一起：锁死壳的列宽、整块横向滚动、顶上一条明说原因的提示。
    三条缺任何一条都会退化成"看起来能用的坏界面"：只锁不滚 → 内容被裁；
    只滚不提示 → 用户以为是页面坏了；有提示但没锁 → 提示在说谎（下面其实是挤压的）。

    这条判据同时钉住三处同源（tokens ↔ @theme ↔ studio.css）与提示文案里的数字 ——
    改令牌忘了改文案，用户会看到"需要至少 1240px 宽"而实际按 1000 排。
    """
    layout = _layout_tokens()
    css = read(STUDIO_CSS)

    # 1) @theme 的三个变量与令牌同值
    theme = {
        "min_width": re.search(r"--app-min-width:\s*(\d+)px", read("src/index.css")),
        "rail": re.search(r"--rail-w:\s*(\d+)px", read("src/index.css")),
        "side": re.search(r"--side-w:\s*(\d+)px", read("src/index.css")),
    }
    for name, found in theme.items():
        assert found, f"`index.css` 的 @theme 里没有 `--{name}` 对应变量"
    assert int(theme["min_width"].group(1)) == layout["min_width_px"], (
        f"`--app-min-width` 是 {theme['min_width'].group(1)}px，tokens 是 {layout['min_width_px']}px"
    )
    assert int(theme["rail"].group(1)) == layout["rail_px"], (
        f"`--rail-w` 是 {theme['rail'].group(1)}px，tokens 是 {layout['rail_px']}px"
    )
    assert int(theme["side"].group(1)) == layout["side_px"], (
        f"`--side-w` 是 {theme['side'].group(1)}px，tokens 是 {layout['side_px']}px"
    )

    # 2) 两栏宽度只有一处产地：CSS 里不许再出现裸的 246px / 352px 列宽
    assert "grid-template-columns:var(--rail-w)" in css, (
        "`.shell` 的左栏宽度没有接 `var(--rail-w)` ⇒ 改令牌不会传导到布局"
    )
    assert "var(--side-w)" in css, (
        "`.board` 的右栏宽度没有接 `var(--side-w)` ⇒ 改令牌不会传导到布局"
    )

    # 3) 两个断点各自落在"下界 - 1"
    stack = layout["side_stacks_below_px"] - 1
    narrow = layout["min_width_px"] - 1
    assert f"@media (max-width:{stack}px)" in css, (
        f"右栏沉底的断点不是 {stack}px（= side_stacks_below_px - 1）—— "
        "断点写小了会在中间留一段『右栏还挂着、DAG 已经要横向滚』的窗口"
    )
    assert f"@media (max-width:{narrow}px)" in css, (
        f"窄屏块不是 {narrow}px（= min_width_px - 1）—— 最小宽度与实际生效的媒体查询对不上"
    )

    # 4) 窄屏块里真的锁了列宽，而且下限是用令牌算出来的
    block_at = css.find(f"@media (max-width:{narrow}px)")
    block = css[block_at : css.find("\n}", block_at)]
    assert "calc(var(--app-min-width) - var(--rail-w))" in block, (
        "窄屏块里的主区下限不是 `calc(var(--app-min-width) - var(--rail-w))` ⇒ "
        "改最小宽度时主区不会跟着变，横向滚动的距离会算错"
    )
    assert "overflow-x: auto" in block or "overflow-x:auto" in block, (
        "窄屏块里没有 `overflow-x: auto` ⇒ 锁了列宽但不让滚，内容直接被裁掉"
    )

    # 5) 提示条：默认不占位，窄屏才出现
    bar = css_rule(".studio-root .narrow-bar")
    assert "display: none" in bar or "display:none" in bar, (
        "`.narrow-bar` 默认不是 `display:none` ⇒ 宽屏上也会留一条空带"
    )
    assert ".studio-root .narrow-bar {display: flex" in block.replace(" ", " "), (
        "窄屏块里没把提示条打开 ⇒ 布局被锁住却没有任何解释"
    )

    # 6) 提示文案里的数字必须等于令牌
    view = read("src/views/StudioView.tsx")
    assert "narrow-bar" in view, "`StudioView` 里没有提示条节点 ⇒ 上面第 5 条量的是一个不存在的元素"
    said = re.search(r"需要至少 (\d+)px 宽", view)
    assert said, "提示条文案里没有『需要至少 N px 宽』—— 用户不知道要拉多宽"
    assert int(said.group(1)) == layout["min_width_px"], (
        f"提示条说『至少 {said.group(1)}px』，而实际按 {layout['min_width_px']}px 排 —— 提示在说谎"
    )


def test_the_breakpoint_leaves_room_for_the_dag():
    """断点必须给 DAG 留够宽度，否则"三栏"这个状态本身就是坏的。

    算式**必须含四项**：`rail + side + dag + 2×padding + 竖向滚动条`，再加画布与右栏的缝。
    当前 246 + 352 + 672 + 24 + 17 + 10 = 1321，断点是 1336（三栏从 1336 起）。

    这个式子改过三次，三次都是因为漏项 —— 这是本条判据存在的原因：

      ① 第一版：只写 `rail + dag + side` = 1270，断点取 1280，看起来留了 10px 余量。
         实测 1280 视口 `dag-wrap` clientWidth 665 / scrollWidth 692 —— 图被裁 27px。
      ② 第二版：断点提到 1280（正是 ① 的"下界"），1300 视口实测仍差 7px。
         因为 `dag-wrap` 左右各 10 的 padding、以及 board 列那条 17px 的竖向滚动条
         从来没进过算式。
      ③ B2 把 `dag-wrap` 的 padding 从 10 调到 12（4px 阶梯），gutter 37→41，
         断点同步 1320→1336；漏掉这一步，1000~1335 区间右列会被裁。

    所以这里把 `dag_gutter_px`（padding×2 + 滚动条）当成 tokens 里的一个**显式数字**，
    而不是判据里硬编码的 12*2+15 —— 硬编码的那个 15 本来就是估的，实测是 17。

    `dag_width_px` 是**当前场景**（13 节点分 3 列）的实测值。节点数变了要重算，
    这条判据会先红 —— 那是提醒，不是误报。
    """
    layout = _layout_tokens()
    gutter = layout["dag_gutter_px"]
    gap = layout["board_gap_px"]
    need = layout["rail_px"] + layout["side_px"] + layout["dag_width_px"] + gutter + gap
    assert need <= layout["side_stacks_below_px"], (
        f"三栏真实下界 {need}px（{layout['rail_px']} 左栏 + {layout['side_px']} 右栏 + "
        f"{layout['dag_width_px']} 图 + {gutter} 内边距与滚动条 + {gap} 画布与右栏之间的缝）"
        f"超过了右栏沉底的断点 {layout['side_stacks_below_px']}px —— 那一段里 DAG 会被裁，"
        "而且裁掉的是右列节点"
    )

    # CSS 里那两条缝必须与令牌同值 —— 否则上面这个不等式算的是另一个界面的宽度
    css = read(STUDIO_CSS)
    board_rule = css[css.find(".studio-root .board {"):]
    board_rule = board_rule[: board_rule.find("}")]
    assert f"gap:{gap}px" in board_rule, (
        f"`.board` 的列间距不是令牌里的 {gap}px ⇒ 断点算式又漏了一项（上一版就是漏项漏出 27px）"
    )

    # 右栏沉底之后，主区宽度要装得下图（同样含 padding、滚动条；右栏沉底时没有那条缝）
    got = layout["min_width_px"] - layout["rail_px"]
    assert got >= layout["dag_width_px"] + gutter, (
        f"最小宽度 {layout['min_width_px']}px 减掉左栏 {layout['rail_px']}px 只剩 {got}px，"
        f"而 DAG 需要 {layout['dag_width_px'] + gutter}px（图 {layout['dag_width_px']} + "
        f"{gutter} 内边距与滚动条）—— 在最小宽度上 DAG 仍要横向滚，那这个下界就没有意义"
    )


# ---------------------------------------------------------------- 二十一


def test_the_task_drawer_is_gone_and_the_agent_drawer_stays():
    """抽屉只保留一个用途：看**独有的**内容。

    2026-10-27 之前有两个抽屉：
    - `openTaskDrawer`：从「任务详情」页签底部一个按钮点开，里面 6 项字段
      （负责人 / 状态 / 依赖 / 输入 / 动作 / 产出）有 5 项页签里已经有了，
      唯一独有的是那段调度日志。**为了 1 项重复 5 项**，还要付一次开关浮层的上下文切换。
    - `openAgentDrawer`：从点左栏座位打开，内容是工位 / 职责 / 状态 / 在手卡 / 产出 ——
      右栏六个页签里**没有**对应页面。它是对的。

    所以删前者、留后者。这条判据两头都钉：少了一个会退回去，多删一个会丢功能。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    assert "openTaskDrawer" not in engine, (
        "`openTaskDrawer` 又回来了 —— 它把页签里已经有的 5 项再列一遍，只为多一段日志"
    )
    assert "export function openAgentDrawer" in engine, (
        "`openAgentDrawer` 被误删了 —— 工位 / 职责 / 在手卡 / 产出在右栏没有对应页签，"
        "删了它左栏 12 个座位就点不动了"
    )
    assert "function openDrawer" in engine, (
        "`openDrawer` 这个底层容器被删了 —— 文件查看器（P1-J）还要用它"
    )

    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert "openTaskDrawer" not in panel, "`SidePanel` 还在调 `openTaskDrawer`"


def test_the_trace_log_lives_in_the_tab():
    """调度日志内联在「任务详情」页签里，用原生 `<details>`，不再是一个浮层。

    为什么是 `<details>` 而不是直接铺开：日志是按需查证的东西（「这张卡为什么重跑了两轮」），
    不是每次都要读的正文。原生元素的键盘合同（Enter / Space 开合、焦点停得住）浏览器自带，
    不需要再写一遍 JS。
    """
    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert "traceOf(task)" in panel, (
        "调度日志没有内联进页签 —— 它要么被删了（丢了「为什么重跑」的线索），"
        "要么又被挪回浮层里了"
    )
    assert 'className="dt-log"' in panel, "日志块没有 `dt-log` 容器"
    assert "<details" in panel and "<summary>" in panel, (
        "日志不是用 `<details>` 折叠的 ⇒ 要么一直铺开（挤占页签），"
        "要么又变成需要 JS 的按钮 + 浮层"
    )
    assert "dt-foot" not in panel, "「展开调度日志」那颗按钮还在 ⇒ 抽屉的入口没有删干净"

    css = read(STUDIO_CSS)
    assert ".studio-root .dt-log summary" in css, "日志的 summary 没有样式 ⇒ 浏览器默认三角会露出来"
    assert "::-webkit-details-marker" in css, (
        "没有抹掉 WebKit 的默认三角 —— `list-style:none` 只对 Firefox 有效，两套都要写"
    )
    assert ".studio-root .dt-foot" not in css, "`dt-foot` 的样式还在，但那个按钮已经不存在了"


def test_the_file_bus_rows_open_the_generated_content():
    """文件总线：有正文的行才可点，正文只有一处产地（`codegen` 的产物）。

    这里有一个容易走错的岔路：`state.artifacts` 与 `state.deliver.gen.files` 是**同一批文件的
    两份记账**，但不是重复 ——
    - `artifacts` 是过程记账（谁 / 何时 / 多少行），流程中逐个 push，所以派单后总线立刻有内容；
    - `gen.files` 是最终正文，`codegen(plan)` 一次性生成，交付页 iframe 与 zip 都用它。

    正文只存在后一份里。把它复制进 `artifacts` 就等于养了两份正文，迟早分叉 ——
    这条判据盯住"不许复制"，也盯住"不许另写一个生成器"。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    assert "export function contentOf" in engine, "没有 `contentOf` ⇒ 行没法判断自己有没有正文"
    body = engine[engine.find("export function contentOf") :]
    body = body[: body.find("\n}")]
    assert "state.deliver.gen.files" in body, (
        "`contentOf` 的正文不是从 `deliver.gen.files` 取的 ⇒ 要么复制了一份，要么另写了一个生成器"
    )
    assert "export function openFileDrawer" in engine, "没有 `openFileDrawer` ⇒ 行点开了也没东西可显示"

    panel = ts_code("src/components/studio/SidePanel.tsx")
    bus = panel[panel.find("function BusTab") :]
    bus = bus[: bus.find("\n}")]
    assert "contentOf(" in bus, "`BusTab` 没有问「这个文件有没有正文」⇒ 所有行要么全可点要么全不可点"
    assert "content === null" in bus, (
        "`BusTab` 没有按「有没有正文」分支 ⇒ 没有正文的行也会渲染成按钮，点了没反应"
    )
    assert 'className="bus-row"' in bus, "行丢了 `bus-row` 类 ⇒ 三列网格塌掉"

    css = read(STUDIO_CSS)
    assert ".studio-root button.bus-row" in css, (
        "可点的行没有样式重置 ⇒ 浏览器默认按钮边框和灰底会盖在表格行上"
    )
    assert "button.bus-row:focus-visible" in css, (
        "可点的行没有可见焦点环 ⇒ 键盘用户看不出自己停在哪一行"
    )


# ---------------------------------------------------------------- 二十二、倒角与它所修饰的面成比例（2026-10-27 第六轮）


def test_the_corner_radii_are_proportional_to_what_they_round():
    """三档倒角必须成阶梯，且 2026-10-29 起**统一收在 12px 以内**。

    事实经过仍是"令牌全对、观感全错"的标本：owner 曾反馈"倒角没了"，量 `border-radius`
    量到 **12px**，放大 6 倍才看见圆角确实在 —— 受理卡 820×233，12px 只占短边 5%，
    1x 下读起来就是直角；当时把 card 提到 16。

    2026-10-29 owner 重新裁定：**12px 是硬上限**，圆角不再承担"倒角感"。
    所以"12 看不见"改由另外三条通道补偿（内侧 1px 高光、卡头 14% 底线、阴影收窄），
    而不是把半径放大。这条判据量的仍是三件事：

      ① 三档严格递增（chip < sub < card）。相等或倒挂 = 层级消失，而层级是这三档存在的**唯一**
         理由（否则一个数就够了）；
      ② **三档都 ≤ 12**（红线）。16px 那一版是被 owner 明确收回的，不许因为"看起来像直角"再改回去；
      ③ 卡片走 `--r-card`、**嵌在卡片里的输入框**走 `--r-sub`。两层同半径读起来是
         "贴纸贴贴纸"，看不出谁在谁里面 —— 这比圆角小更伤层级。
    """
    index = read("src/index.css")
    radii: dict[str, int] = {}
    for key in ("r-card", "r-sub", "r-chip"):
        match = re.search(rf"--{key}:\s*([0-9]+)px", index)
        assert match, f"`--{key}` 没有在 index.css 里以 px 声明 ⇒ 三档缺一档"
        radii[key] = int(match.group(1))

    assert radii["r-chip"] < radii["r-sub"] < radii["r-card"], (
        f"三档没有严格递增：chip={radii['r-chip']} sub={radii['r-sub']} "
        f"card={radii['r-card']} —— 相等或倒挂就等于只有一档"
    )
    assert radii["r-card"] <= 12, (
        f"卡片半径是 {radii['r-card']}px，超过 2026-10-29 的 12px 红线。"
        "12px 在 820×233 的受理卡上确实偏小，但那要靠内侧高光 / 卡头底线 / 阴影收窄补偿，"
        "不是把半径放大 —— 16px 那一版已被 owner 收回。"
    )
    assert max(radii.values()) <= 12, (
        f"三档里还有 >12px 的值：{radii} —— 红线是三档整体，不是只量 card"
    )
    assert radii["r-sub"] - radii["r-chip"] >= 2, (
        "次级档与小件档只差 1px ⇒ 这两档在屏幕上分不出来，等于把三档又压回两档"
    )

    css = read(STUDIO_CSS)
    card = css[css.find(".studio-root .card {") :]
    card = card[: card.find("}")]
    assert "var(--r-card)" in card or "var(--r)" in card, (
        "卡片没有用卡片档半径 ⇒ 三档里最大的那一档没人用"
    )

    box = css[css.find(".studio-root .intake textarea {") :]
    box = box[: box.find("}")]
    assert "var(--r-sub)" in box, (
        "输入框和它所在的卡片用了同一档半径 ⇒ 两层同半径，读起来是贴纸贴贴纸；"
        "输入框是**控件**，该走次级档"
    )

    # 浮层不在三档里，但 16px 的 `1rem` 同样越过 12px 红线 —— 它必须回到卡片档。
    glass = index[index.find("@utility glass {") :]
    glass = glass[: glass.find("}")]
    assert "border-radius: var(--r-card)" in glass, (
        "`glass` 还写着 `border-radius: 1rem`（16px）—— 浮层不在三档里，"
        "但 16px 同样越过红线；改成 var(--r-card)"
    )

    # AUTO 徽章是 15px 高的 SVG 胶囊（rx=7.5），是三档之外最后一处胶囊化。
    board = read("src/components/studio/BoardScreen.tsx")
    assert "rx={7.5}" not in board, (
        "AUTO 徽章还是 15px 高的胶囊（rx=7.5）—— 直角规范里连它也不该是胶囊，改 rx=2"
    )

    # 圆角只许从三档里取，不许再有裸 px。唯一的例外是手机外框（`375px` 的设备边框，
    # 14px 是按"手机看起来像手机"定的，与三档的层级无关）。
    bare = [
        line.strip()
        for line in css.splitlines()
        if "border-radius" in line
        and re.search(r"border-radius:\s*[0-9]+px", line)
        and ".frame.phone" not in line
    ]
    assert not bare, (
        f"studio.css 里还有裸 px 圆角（不走三档）：{bare} —— "
        "下一个人改令牌时会漏掉它们，于是界面上一半圆角跟着变、一半不变"
    )




# ---------------------------------------------------------------- 二十二·补：节点 id 与状态词的几何契约（2026-10-29）


def test_the_node_id_stays_within_four_chars():
    """节点 id 槽只有 24 个用户单位（4 字符），且**前端不做截断**。

    几何：节点 200 宽，id 右对齐到 192，AUTO 徽章占 126–166。
    4 字符 × 6 单位 = 24 ⇒ 左端 168，与徽章右缘只余 **2 单位**；5 字符左端 162 就直接压字。
    所以这条契约必须由**源侧**保证，不能靠渲染层截断 —— 截断会把"id 超长"一个故障，
    放大成"图上两张卡看起来是同一张 + 汇总页只指得中其中一张"两个故障。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    demo_ids = re.findall(r'id:\s*"([A-Z][A-Z0-9]*)"[^\n]*\bphase:', engine)
    assert demo_ids, "没扫到推演档的任务 id —— 任务定义的行形状变了，这条判据失效"
    assert len(demo_ids) >= 13, (
        f"只扫到 {len(demo_ids)} 个推演档 id —— 扫描面缩了，这条判据会变成空转"
    )
    too_long = [task_id for task_id in demo_ids if len(task_id) > 4]
    assert not too_long, (
        f"推演档有超过 4 字符的任务 id：{too_long} —— "
        "4 字符是节点 id 槽的上限（左端 168 / 徽章右缘 166 / 余量 2）"
    )

    # 真链路的 id 由后端生成：`T{序号:02d}`。固定两位序号 ⇒ 3 字符；
    # 三位序号（T100）仍是 4 字符，仍在槽内；任何更宽的格式都必须在这里被看见。
    backend_files = (
        REPO / "src/acp/orchestrate/llm_split.py",
        REPO / "src/acp/reporting/cards.py",
    )
    exprs: list[str] = []
    for path in backend_files:
        assert path.is_file(), f"后端 id 生成文件不在：{path}"
        exprs += re.findall(r'f"T\{([^}]*)\}"', path.read_text(encoding="utf-8"))
    assert exprs, "后端没有扫到 T 开头的任务 id 生成式 —— 生成位置变了，这条判据失效"
    assert len(exprs) >= 3, (
        f"只扫到 {len(exprs)} 处后端 id 生成式 —— 扫描面缩了（llm_split 两处 + cards 一处）"
    )
    unbounded = [expr for expr in exprs if ":02d" not in expr]
    assert not unbounded, (
        f"后端任务 id 生成式不是固定两位序号：{unbounded} —— "
        "改成变宽格式会越出节点 id 槽"
    )

    # 前端零兜底：既不许截断，也不许用 textLength 压字距（会破坏 mono 列对齐）。
    board = read("src/components/studio/BoardScreen.tsx")
    assert "task.id.slice" not in board, "前端在截断 task.id —— id 是追踪键，源侧该拦就拦"
    assert "textLength" not in board, "前端在用 textLength 压 id —— 等宽列会歪"


def test_node_status_words_fit_three_chars():
    """节点顶行状态词 ≤3 汉字；`过期` 不占状态词位。

    几何：状态词从 x=33 起，负责人从 x=70 起，中间只有 37 单位；10 单位/汉字 ⇒ 3 字。
    语义：`过期` 是正交属性（已完成 + 产物被覆盖），写进状态词位会让颜色（绿）与文字互相打脸。
    """
    engine = ts_code("src/lib/studioEngine.ts")
    block = re.search(r"NODE_STATUS_WORDS\s*=\s*\{(.*?)\}\s*as const", engine, re.S)
    assert block, "`NODE_STATUS_WORDS` 不见了 —— `stopWordOf()` 的唯一词表必须有这一处"

    words = re.findall(r'"([^"]+)"', block.group(1))
    assert len(words) == 11, (
        f"状态词表是 {len(words)} 个（{words}）—— 应为六档 + 超时/不合格/退回/待改/升级 = 11"
    )
    assert len(set(words)) == len(words), f"状态词表有重复：{words}"
    too_long = [word for word in words if len(word) > 3]
    assert not too_long, (
        f"有超过 3 字的状态词：{too_long} —— 4 字就会压住 x=70 的负责人"
    )
    assert not [word for word in words if re.search(r"[A-Za-z]", word)], (
        f"状态词里混进了拉丁字符：{words}"
    )
    assert "过期" not in words, (
        "`过期` 进了状态词位 —— 它是正交属性（已完成 + 产物被覆盖），只该出现在第二行与 aria 里"
    )


# ---------------------------------------------------------------- 二十三、总线一行一个文件（2026-10-27 第六轮）


def test_every_row_in_the_bus_is_one_file_and_the_delivered_ones_open():
    """文件总线是**文件清单**：一行一个文件，交付包里有的都要能打开。

    这是 P1-J 接线之后被截图抓出来的两个漏子 —— 都不是"没接上"，是**名字对不上**：

      ① 前端那张卡同时交三页，记账时 `artifact` 写成一串
         `index.html / products.html / contact.html`。总线把它当**一个文件**列出来，
         于是交付包里最重要的三个页面成了唯一打不开的三个；而"行数"那一格填的是
         一个随机估算 —— 三个文件的行数之和？不是。那是三个文件里某个数的替身。
      ② 返工卡产出 `style.css (v2)`。版本后缀让它匹配不上 `style.css`，
         返工那一版打不开 —— 而返工正是最需要点开看的那一版。

    两处的修法都在"名字"这一层，所以判据也钉名字：`addArtifact` 拆 `" / "`，
    `contentOf` 去掉 `(vN)` 再找一次。**不钉数字**（比如"必须 8 行可点"），
    因为节点数一变那个数就变；钉的是"同一个文件不许有两个名字、一个文件不许占两行"。
    """
    engine = ts_code("src/lib/studioEngine.ts")

    add = engine[engine.find("function addArtifact(") :]
    add = add[: add.find("\n}")]
    assert 'name.split(" / ")' in add, (
        "`addArtifact` 没有把 `a / b / c` 拆成三行 ⇒ 三个文件挤在一行，"
        "那一行的「行数」与「可点」都说不通，而且它们打不开"
    )

    lookup = engine[engine.find("export function contentOf") :]
    lookup = lookup[: lookup.find("\n}")]
    assert r"\(v\d+\)" in lookup, (
        "`contentOf` 没有处理 `(vN)` 版本后缀 ⇒ 返工产出的 `style.css (v2)` 打不开"
    )
    assert "state.deliver.gen.files" in lookup, (
        "正文不是从 `deliver.gen.files` 取的 ⇒ 又养了第二份正文"
    )


# ---------------------------------------------------------------- 二十四、历史记录（2026-10-27 第七轮）


def _fn_body(engine: str, head: str) -> str:
    """取一个函数体（从 `head` 到下一个行首 `}`）。取不到就红 —— 函数改名等于判据失效。"""
    at = engine.find(head)
    assert at != -1, f"引擎里找不到 `{head}`"
    return engine[at : engine.find("\n}", at)]


def test_the_history_survives_a_reset_and_only_replays_what_you_committed():
    """历史是"立项过什么"的账，不许被"重置这一单"抹掉；点一条是**重跑**，不是恢复存档。

    这条钉四件事，每一件都对应一个具体的坏结果：

      ① **三个"回到未立项"的动作都要保住历史。** 引擎的状态是整份替换
         （`state = initialState()`），少写一句 `state.history = history` 历史就没了 ——
         而它落在 `localStorage` 里，用户会以为"历史功能坏了"，其实是被自己点的一次
         「重新立项」清空的。三个动作是 `resetStudio` / `editRequirement` / `startNewRequirement`。
      ② **`reopenFromHistory` 只调 `startProject(entry.raw)`。** 恢复进度要连任务状态、
         定时器、事件流一起存，很容易恢复出一个自相矛盾的中间态（完成一半、定时器停在中途），
         而那不是一个"历史记录"该有的行为。
      ③ **按原文去重。** 同一条需求改个标点再提交，不该在历史里变成两条 —— 那会让
         "最近 20 条"里有一半是同一件事。
      ④ **浮层必须 `position:fixed`。** 这一屏的 `.studio` 是 `overflow:hidden`
         （四张卡各自滚、整屏不滚），绝对定位的下拉到第三四行就被裁掉 ——
         "只看得见前两条的历史"比没有历史更坏。

    另有一条观感事故值得记：遮罩第一版写的是 `color-mix(in srgb,var(--canvas) 64%,transparent)`，
    深色主题下 `--canvas` 本来就近黑，等于没有遮罩，浮层像直接浮在页面上。现在与抽屉那条
    `.mask` 同一套口径（深色 `#000 55%`、浅色 `var(--ink) 42%`）。
    """
    engine = ts_code("src/lib/studioEngine.ts")

    # ① 三个动作都先存后放
    for head in (
        "export function resetStudio(",
        "export function editRequirement(",
        "export function startNewRequirement(",
    ):
        body = _fn_body(engine, head)
        assert "state = initialState();" in body, f"`{head}` 不再整份重置 ⇒ 这条判据的对象变了"
        saved = body.find("const history = state.history;")
        wiped = body.find("state = initialState();")
        restored = body.find("state.history = history;")
        assert -1 not in (saved, wiped, restored) and saved < wiped < restored, (
            f"`{head}` 没有在整份重置前后保住 `history` ⇒ 点一次就清空整本历史账。"
            "顺序必须是：先存 `const history = state.history`，再 `state = initialState()`，"
            "最后 `state.history = history`"
        )

    # ② 点一条 = 用原文重跑
    reopen = _fn_body(engine, "export function reopenFromHistory(")
    assert "startProject(entry.raw)" in reopen, (
        "`reopenFromHistory` 不是用原文重新立项 ⇒ 它要么在恢复旧状态，要么什么都没做"
    )
    assert "state =" not in reopen, (
        "`reopenFromHistory` 自己动状态了 ⇒ 它会绕过 `startProject`，长出一条只恢复一半的路径"
    )

    # ③ 去重 + 上限
    record = _fn_body(engine, "function recordHistory(")
    assert "item.raw !== entry.raw" in record, (
        "`recordHistory` 不再按原文去重 ⇒ 同一条需求改个标点就会在历史里变成两条，"
        "「最近 20 条」里一半是同一件事"
    )
    assert "HISTORY_MAX" in record, "`recordHistory` 没有截断到上限 ⇒ 历史会无限长"
    assert 'const HISTORY_KEY = "acp-history";' in engine, "历史的 `localStorage` 键改了 ⇒ 老用户的账全丢"
    assert "window.localStorage.setItem(HISTORY_KEY" in engine, "历史没有落到 `localStorage` ⇒ 刷新就没"

    # ④ 浮层的位置与遮罩
    screen = ts_code("src/components/studio/StudioScreen.tsx")
    assert 'data-act="history"' in screen, "抬头那颗「历史」没了 ⇒ 这个功能没有入口"
    assert 'role="dialog"' in screen and 'aria-modal="true"' in screen, (
        "历史浮层没有报出 dialog / modal ⇒ 读屏读到的是一段普通内容"
    )
    assert 'event.key === "Escape"' in screen, "历史浮层不能用 Esc 关闭"
    assert "trigger.current?.focus()" in screen, (
        "关闭时没有把焦点还给那颗触发键 ⇒ 键盘用户得从页面开头重新 Tab 一遍"
    )

    modal = css_rule(".studio-root .hist-modal")
    assert "position:fixed" in modal, (
        "历史浮层不是 fixed ⇒ 它会被 `.studio` 的 `overflow:hidden` 裁掉（这一屏整屏不滚，"
        "四张卡各自滚），下拉到第三四行就看不见了"
    )

    mask = css_rule(".studio-root .hist-mask")
    assert "var(--canvas)" not in mask, (
        "遮罩又用 `--canvas` 了 —— 深色主题下它本来就近黑，等于没有遮罩，浮层会像直接浮在页面上"
    )
    assert "#000" in mask, "深色遮罩不是 `#000` 系 ⇒ 与抽屉那条 `.mask` 不是同一套口径"


# ---------------------------------------------------------------- 二十五、编制含总管，门不是椅子（2026-10-27 第八轮）


def test_the_roster_counts_the_orchestrator_and_the_door_is_not_a_seat():
    """编制 12 行，第一行是工作室总管；门是**入口**，不是第 12 张椅子。

    2026-10-27 owner 贴了两张图：

      ① 左栏「编制」下面数得出 **11 行**，而门上写着「12 工位」—— 缺的那位正是总管自己；
      ② 会议桌第一个席位写的是「总管 · 项目工作室」—— 把**房间**当成了人。

    两处的根因是同一个：总管只以"门"的形式存在（`door: true` + `STUDIO_SEATS` 把它滤掉），
    于是名册少一人、门上的数字对不上；而它的职称借了房间的名字（`项目工作室`），
    于是一开口就自称一间屋子（会议气泡、活动流主语全跟着错）。

    当时那条注释写的是"总管升成一扇门，不在 11 张椅子上"—— 这句话混淆了两件事：
    门说的是"点这里进工作室"（入口），编制说的是"这个工作室有谁"（名册）。
    总管既在名册里（第一行），也另外有一扇门，两者不冲突；冲突的是拿门去顶椅子。

    现在钉住四件事：
      · `STUDIO_AGENTS` 12 位、第一位是 orchestrator；
      · 总管的职称是「工作室总管」（不是房间名），`door` 这个字段连同过滤一起删掉；
      · 门上那句「N 工位」读 `STUDIO_HEADCOUNT`，不写死数字（否则加一个人就对不上）；
      · `agentView` 给总管留一条读数 —— 它不持有执行卡，交付后那条 override 被删掉时
        必须还有话说，否则第一行会退回空白。
    """
    engine = ts_code("src/lib/studioEngine.ts")

    roster = engine[engine.find("export const STUDIO_AGENTS") :]
    roster = roster[: roster.find("\n];")]
    ids = re.findall(r'\{ id: "([a-z]+)"', roster)
    assert len(ids) == 12, f"编制是 {len(ids)} 位（{ids}）—— 「12 工位」这句话得对得上名册"
    assert ids[0] == "orchestrator", f"第一行不是总管，是 {ids[0]}"

    assert 'title: "工作流编排专家"' in roster, (
        "总管的职称不是「工作流编排专家」⇒ 会议桌席位 / 会议气泡 / 活动流主语会读错人"
        "（2026-10-28 之前叫「工作室总管」，那一版已经按 owner 的职级表换掉了）"
    )
    assert 'title: "项目工作室"' not in roster, (
        "总管又叫「项目工作室」了 ⇒ 那是**房间**的名字（门与面包屑），不是人"
    )
    assert "readonly door" not in engine and "!agent.door" not in engine, (
        "`door` 标记又回来了 ⇒ 只要它还在，名册就会被滤掉一位，门上那句「12 工位」立刻对不上"
    )
    assert "export const STUDIO_SEATS: readonly AgentDef[] = STUDIO_AGENTS;" in engine, (
        "`STUDIO_SEATS` 不再等于整份编制 ⇒ 左栏少一行（总管）"
    )

    rail = ts_code("src/components/studio/StudioRail.tsx")
    assert "STUDIO_SEATS.map(" in rail, "左栏不是按编制逐行渲染的"
    assert "STUDIO_HEADCOUNT" in rail, "门上的「N 工位」没有读编制总数"
    assert "12 工位" not in rail, (
        "门上那个 12 是写死的 ⇒ 编制一变，门就在说谎（这次错的就是这个数）"
    )

    view = engine[engine.find("export function agentView") :]
    view = view[: view.find("\n}")]
    assert 'if (id === "orchestrator")' in view, (
        "`agentView` 没有总管那一条 ⇒ 它不持有执行卡，交付后 override 被删掉时第一行会空白"
    )


# ---------------------------------------------------------------- 二十六、原型与产品同一个人（2026-10-27 第八轮）


def test_the_prototype_and_the_engine_agree_on_who_the_orchestrator_is():
    """`项目工作室-原型.html` 是这一屏的**移植源**，它和引擎必须说同一个人。

    这一条是补漏：上一轮把产品里的总管改成了「工作室总管」、名册补到 12 位，
    但**原型没改** —— 于是 owner 打开原型一看，还是「总管 · 项目工作室」、编制还是 11 行，
    又报了一次同样的问题。这不是"文档过期"，是**移植源本身错着**：
    产品这份代码就是从它逐条搬过来的（`studio.css` 甚至是机械搬运的），
    谁下一次按原型再对一遍，同样的两个缺陷会原样长回来。

    钉三件事，都对着原型里的那一份数据表：
      · 总管的职称是「工作室总管」，且没有 `door:true`；
      · `SEATS` 就是整份 `AGENTS`（不再 `filter(a=>!a.door)`）；
      · 门上那句「N 工位」读 `AGENTS.length`，编制角标读 `SEATS.length/AGENTS.length`
        —— 原来写的是 `${SEATS.length+1}/12`，靠"给门加一"凑出 12，正是那次错误的痕迹。
    """
    proto = read("../项目工作室-原型.html")

    roster = proto[proto.find("const AGENTS = [") :]
    roster = roster[: roster.find("\n];")]
    assert 'title:"工作流编排专家"' in roster, (
        "原型里总管的职称与产品不一致 ⇒ 产品下次照原型再搬一遍，会议桌上又会是旧名字"
    )
    assert "door:true" not in roster, (
        "原型里总管还带 `door:true` ⇒ 名册会被滤掉一位，编制只有 11 行而门上写 12 工位"
    )
    assert "const SEATS = AGENTS;" in proto, (
        "原型里 `SEATS` 还在过滤 `door` ⇒ 名册少一人"
    )
    assert "filter(a=>!a.door)" not in proto, "原型里那条 `door` 过滤还在"

    door = proto[proto.find('class="door-txt"') :]
    door = door[: door.find("</span>")]
    assert "AGENTS.length" in door, (
        "原型门上那句「N 工位」还是写死的 ⇒ 编制一变，门就在说谎（这次错的就是这个数）"
    )
    assert "SEATS.length+1" not in proto, (
        "编制角标又写成 `${SEATS.length+1}` 了 ⇒ 那是「给门加一凑 12」的旧写法，"
        "名册少一位却看不出来"
    )


# ---------------------------------------------------------------- 廿三

#: 工作室屏的源码。两条真链路判据都扫它 —— 它们守的不是"有没有这个功能"，
#: 而是"真数据旁边有没有一条会把假话掺进来的路"。
STUDIO_SCREEN = "src/components/studio/StudioScreen.tsx"


def test_the_primary_intake_is_real_and_the_demo_is_an_explicit_second_entry():
    """默认立项只走真链路；固定脚本演示必须是显式按钮，不能自动接管。

    2026-10-29 owner 报障：打开演示直链会自动放一遍固定脚本，四个岗位的台词每次一样，
    看起来像"提前彩排"。旧实现把"项目键留空"当推演开关，于是默认路径就可能是假会。
    新合同：
    ① `begin` 只调 `startLive`；项目键为空时先自动生成（`autoProjectKey`）；
    ② `startProject` 只在 `runDemo` 里，由右上角「固定演示」按钮触发；
    ③ 两条路各只有一处入口 —— 防止某一处偷偷把推演掺进真链路，或反过来。
    """
    screen = ts_code(STUDIO_SCREEN)
    at = screen.find("function IntakeCard(")
    assert at != -1, "`IntakeCard` 没了 ⇒ 这条判据的对象不在"
    # 边界用下一个组件的函数头，不用分节注释：`ts_code` 会**剥掉块注释**，
    # 拿注释当界标的判据会静默退化成"扫整份文件"。
    end = screen.find("function MeetingCard(", at)
    assert end != -1, "`MeetingCard` 没了 ⇒ 受理卡的边界划不出来，判据扫的是一整份文件"
    body = screen[at:end]

    begin_at = body.find("const begin =")
    demo_at = body.find("const runDemo =")
    assert begin_at != -1, "`begin` 没了 ⇒ 主立项入口不在判据里"
    assert demo_at > begin_at, "`runDemo` 没了或排在 `begin` 前面 ⇒ 演示入口的边界划不出来"
    begin_body = body[begin_at:demo_at]
    demo_body = body[demo_at:]

    assert begin_body.count("startLive(") == 1, (
        f"主立项入口里 `startLive(` 出现 {begin_body.count('startLive(')} 次 —— 真链路只许有一条"
    )
    assert "startProject(" not in begin_body, (
        "主立项入口又调了 `startProject` ⇒ 点一次立项仍可能得到固定脚本，owner 报障会原样复现"
    )
    assert "autoProjectKey()" in begin_body, (
        "项目键留空时没有自动生成 ⇒ 主按钮会被后端 422 挡住，用户只剩演示按钮可点"
    )
    assert demo_body.count("startProject(") == 1, (
        f"演示入口里 `startProject(` 出现 {demo_body.count('startProject(')} 次 —— 固定脚本只许有一条"
    )
    assert 'id="btnDemo"' in body, "右上角「固定演示」按钮没了 ⇒ 断网/看版式的入口消失"
    assert "不调用任何模型" in body, (
        "演示按钮没有写明「不调用任何模型」⇒ 用户会把固定脚本当成模型输出"
    )


def test_the_three_output_tabs_read_the_backend_docs_not_local_guesses():
    """真链路的 PRD / 架构 / 令牌读**后端文档结构**（`live.docs`），不是前端猜的那一套。

    2026-10-28 owner 选 B：会议之后加文档节点，产 PRD / 架构 / 令牌三份结构化 JSON。
    在那之前这三格要么空着、要么拿 `analyze()` 的关键词派生内容填 —— 后者长得跟真的一样
    （有标题、有条目、有配色），读者分不出哪一句是模型说的。

    判据钉两件事：
    1. 三格的"就绪"在 live 档必须问 `docs`（而不是 `artifacts` 或 `plan` 有没有值）；
    2. 那句「后端暂无此读面」必须**已经不在**了 —— 现在有读面，它成了谎话。
    """
    screen = ts_code(STUDIO_SCREEN)
    assert "后端暂无此读面" not in screen, (
        "文档节点已经落地了，「暂无此读面」现在是假话 —— 要么改文案，要么说明为什么没接"
    )
    for part in ("docs?.prd", "docs?.arch", "docs?.tokens"):
        assert part in screen, f"三格没有一格读后端文档：找不到 `{part}`"

    at = screen.find("function OutputBody(")
    assert at != -1, "`OutputBody` 没了 ⇒ 这条判据的对象不在"
    body = screen[at:]
    # 每一格的等待分支都必须在 live 档给出"在写/没写出来"的说法，而不是一句通用空态
    assert body.count("docWait(studio)") >= 3, (
        "三格没有都接上 `docWait` ⇒ 真链路下会退回推演档那句「产品经理正在写」"
    )


def test_leaving_a_unit_also_forgets_the_saved_session():
    """「重新立项 / 修改需求」之后刷新，不许把刚离开的那一单接回来。

    会话落盘（`acp-live-session`）是为了 F5 之后能接回在跑的单。它同时意味着：
    **离开就必须忘记**，否则"重新立项"只是清了屏幕，刷新一次原地复活。
    """
    screen = ts_code(STUDIO_SCREEN)
    at = screen.find("function leaveProject(")
    assert at != -1, "`leaveProject` 没了 ⇒ 清屏与断连又分回两处，顺序迟早会错"
    body = screen[at : screen.find("function ", at + 10)]
    assert "disconnect()" in body, "离开时没断连 ⇒ 5 秒后的轮询会把刚清掉的单搬回来"
    assert "forgetSession()" in body, "离开时没忘掉落盘的会话 ⇒ 刷新一次这一单原地复活"


# ---------------------------------------------------------------- 二十七、左栏双行规范化（2026-10-28 第九轮）


#: owner 2026-10-28「UI 质感升维」给的映射字典，逐字抄下来。
#: 这张表是**判据**而不是文档：职称只许是这十二个，多一个字少一个字都红。
EXPERT_DUTIES: dict[str, tuple[str, str]] = {
    "orchestrator": ("工作流编排专家", "拆单与调度"),
    "pm": ("需求分析专家", "PRD 与验收标准"),
    "cto": ("系统架构专家", "技术选型与结构"),
    "designer": ("UI/UX 设计师", "设计系统与令牌"),
    "fe": ("前端开发专家", "页面骨架与交互"),
    "be": ("后端开发专家", "接口与数据模型"),
    "writer": ("技术文档专家", "规范手册与说明"),
    "qa": ("测试开发专家", "E2E 回归与打回"),
    "sec": ("应用安全专家", "漏洞扫描与越权"),
    "reviewer": ("独立代码审查员", "门禁比对与互斥"),
    "acceptor": ("交付验收专家", "终局签字与交付"),
    "rescue": ("全栈保障专家", "异常接管与补位"),
}


def _roster(rel: str) -> str:
    """取文件里那张编制表（从 `STUDIO_AGENTS` / `AGENTS` 到行尾的 `];`）。"""
    text = read(rel) if rel.startswith("src/") else (REPO / rel).read_text(encoding="utf-8")
    at = max(text.find("const STUDIO_AGENTS"), text.find("const AGENTS = ["))
    assert at != -1, f"{rel} 里找不到编制表"
    return text[at : text.find("\n];", at)]


def test_every_seat_is_a_rank_plus_a_duty():
    """十二个位的**主行是职级、副行是职责**，逐字等于 owner 给的字典。

    为什么值得钉成一张表而不是"能渲染就行"：这一次改的是**名字**，
    而名字散落在会议桌、活动流、看板、任务详情、交付清单五处 —— 它们全靠
    `agentTitle()` 读同一张表。表里错一个字，五处一起错；表里对了，五处不用各改一遍。
    """
    roster = _roster("src/lib/studioEngine.ts")
    rows = re.findall(r'\{ id: "([a-z]+)", title: "([^"]+)", mono: "([^"]+)", tone: "(--a\d)", note: "([^"]+)" \}', roster)
    assert len(rows) == 12, f"编制是 {len(rows)} 位 —— 「12 工位」这句话得对得上名册"

    got = {rid: (title, duty) for rid, title, _mono, _tone, duty in rows}
    assert got == EXPERT_DUTIES, (
        "职级 / 职责与 owner 给的字典不一致：\n"
        f"  多出来或写错的：{ {k: v for k, v in got.items() if EXPERT_DUTIES.get(k) != v} }\n"
        f"  少了：{sorted(set(EXPERT_DUTIES) - set(got))}"
    )


def test_the_prototype_roster_says_the_same_twelve_titles():
    """原型是这一屏的**移植源**，它的编制表必须与产品同一个人、同一句职责。

    2026-10-27 已经吃过一次"产品改了、原型没改"的亏（编制 11 行 vs 门上 12 工位）。
    这一条是同一件事在**职称**上的复刻：谁下一次按原型再对一遍，不能把旧名字搬回来。
    """
    roster = _roster("项目工作室-原型.html")
    titles = re.findall(r'title:"([^"]+)"', roster)
    duties = re.findall(r'note:"([^"]+)"', roster)
    assert len(titles) == 12, f"原型编制是 {len(titles)} 位"
    assert titles == [v[0] for v in EXPERT_DUTIES.values()], f"原型的职称与产品不一致：{titles}"
    assert duties == [v[1] for v in EXPERT_DUTIES.values()], f"原型的职责与产品不一致：{duties}"


def test_the_seat_is_one_line_and_the_reading_lives_in_the_tooltip():
    """左栏那一行**只有职级**一行（owner 2026-10-29 第十轮：「把副标题也删去」）。

    历史：2026-10-27 是单行 + 空闲留白；2026-10-28 改成"职级 + 职责"双行常显；
    2026-10-29 owner 用了半天之后要求删掉副标题 —— 十二行两行文本把一列导航撑成了一篇文档。
    职责没有丢：它进这一行的 `title` 提示（连同读数一起读），扫一眼仍有据可查。

    判据扫的是**去注释后的代码**：注释里正当写着"职责挪到提示里"，那是解释，不是实现。
    """
    rail = ts_code("src/components/studio/StudioRail.tsx")
    start = rail.find('<span className="seat-txt">')
    assert start != -1, "左栏那一行没有 `seat-txt` 容器了"
    seat = rail[start : rail.find("</span>", start) + len("</span>")]
    assert "<b>{agent.title}</b>" in seat, "主行不是职级（`agent.title`）⇒ 左栏又开始自己拼名字了"
    assert "<i>" not in seat and "agent.note" not in seat, (
        "副行（职责）又回来了 ⇒ 左栏回到两行文本，一列导航读起来像一篇文档"
    )
    assert "agent.note" in rail, (
        "职责被整条删掉了 ⇒ 「这位是干什么的」再也查不到；它应当进 `title` 提示"
    )
    assert "view.note" in rail, (
        "读数被整条删掉了 ⇒ 左栏再也看不出「这个工位此刻在不在动」；它应当进 `title` 提示"
    )


def test_the_single_line_goes_grey_when_the_seat_is_not_moving():
    """字色**跟着状态走**：13px / 600，忙的行亮、空闲与休眠的行压成暗灰 500。

    2026-10-28 那一版要求"职级恒亮白、状态只交给灯"，owner 2026-10-29 的反馈是
    「十二个岗位的名称改回原来的颜色，现在的颜色太刺眼了」—— 十二行全亮白加粗，
    一列导航糊成一片，而且看不出谁在动。所以这里钉回原来的口径。
    """
    main = css_rule(".studio-root .seat-txt b")
    assert "font-size:var(--fs-title)" in main, "主行字号不是 `--fs-title`（13px）"
    assert "font-weight:600" in main, "主行字重不是 600（700 是 2026-10-28 那一版，太压）"
    assert "color:var(--ink)" not in main, (
        "主行写死了亮白 ⇒ 下面那条「空闲压灰」永远盖不住它，十二行全亮"
    )

    css = read(STUDIO_CSS)
    assert '.seat[data-s="idle"] .seat-txt b' in css, (
        "「空闲把职级压成暗灰」那条规则不见了 ⇒ 十二行里十行也是亮的，看不出谁在动"
    )
    idle = css_rule('.studio-root .seat[data-s="idle"] .seat-txt b')
    assert "color:var(--ink-muted)" in idle and "font-weight:500" in idle, (
        "空闲行不是「暗灰 + 500」⇒ 与忙的行分不开"
    )


# ---------------------------------------------------------------- 十九


def test_every_spacing_token_reference_resolves():
    """`var(--sp-N)` 指向一个不存在的令牌时，**整条声明会静默失效**，而 CSS 不会报错。

    2026-10-29 B4 就踩了这一下：页签渐隐写成 `calc(100% - var(--sp-5))`，
    而间距阶梯是 4/8/12/16/24/32（`--sp-1/2/3/4/6/8`，**没有 5**）——
    浏览器把 `mask-image` 当作非法值丢掉，`getComputedStyle` 读到的是 `none`，
    看起来像"选择器没匹配上"，实际是变量没定义。排查花了三轮。

    所以这条判据不看具体值，只看**引用面 ⊆ 定义面**：`studio.css` 里出现的每一个
    `--sp-*` 都必须在 `index.css` 的 `:root` 里有定义。阶梯以后要加档，也不会漏在这一步。
    """
    index = read("src/index.css")
    defined = set(re.findall(r"(--sp-\d+)\s*:", index))
    assert defined, "`index.css` 里一个 `--sp-*` 都没读到 —— 间距阶梯被改名了"

    used = set(re.findall(r"var\((--sp-\d+)\)", read(STUDIO_CSS)))
    missing = sorted(used - defined)
    assert not missing, (
        f"`studio.css` 引用了不存在的间距令牌 {missing}（已定义：{sorted(defined)}）。"
        "这种引用不会报错，只会让整条声明失效 —— 外观看起来像"
        "「选择器没命中」，实际是变量不存在。"
    )


def test_the_tab_fade_only_appears_on_measured_overflow():
    """页签渐隐只在**实测溢出**时出现，且滚到最右要撤掉。

    为什么不是一条无条件的 `mask-image`：六个页签的自然宽度之和是 288px、容器内宽 350px
    （352 − 两侧各 1px 边框），常态下有 62px 余量。无条件挂着渐隐，会把「交付」的右半
    啃掉一块 —— 那是在屏上报一个不存在的状态（"右边还有"）。
    而滚到最右仍留着渐隐，同样在说假话：到头了就该停手。

    所以三段一起钉：①`studio.css` 里只有**一处** `mask-image`；②它挂在
    `[data-overflow="true"]` 上、宽度取令牌不取裸 px；③`[data-end="true"]` 把它撤回。
    再补一条正面断言：那两个 `data-*` 是 `SidePanel` 实测 `scrollWidth` 后写上去的
    —— 没有这段实测，"只在溢出时出现"就只是一句注释。
    """
    css = read(STUDIO_CSS)
    mask_rules = [sel.strip() for sel, body in re.findall(r"([^{}\n]*)\{([^{}]*mask-image[^{}]*)\}", css)]
    assert len(mask_rules) == 2, (
        f"`studio.css` 里声明 `mask-image` 的选择器有 {len(mask_rules)} 条（{mask_rules}）⇒ "
        "渐隐要么被复制到别处，要么绕过了溢出判定（应当只有「溢出时出现」与「到最右撤回」两条）"
    )
    assert all("side-tabs" in sel for sel in mask_rules), (
        f"渐隐挂到了页签栏之外的选择器上：{mask_rules}"
    )

    gated = css_rule('.studio-root .side-tabs[data-overflow="true"]')
    assert "mask-image: linear-gradient(" in gated, "渐隐没有挂在 `[data-overflow=\"true\"]` 上"
    assert "var(--sp-6)" in gated, "渐隐宽度没走间距令牌（裸 px 会在字档变化时对不上）"
    decl = gated.split("mask-image:", 1)[1]
    assert "px" not in decl, f"渐隐声明里还有裸 px：{decl.strip()[:60]}"

    withdrawn = css_rule('.studio-root .side-tabs[data-overflow="true"][data-end="true"]')
    assert "mask-image:none" in withdrawn, (
        "滚到最右没有撤回渐隐 ⇒ 「到头了」和「还有更多」在屏上长得一样"
    )

    panel = ts_code("src/components/studio/SidePanel.tsx")
    assert "dataset.overflow" in panel and "dataset.end" in panel, (
        "`SidePanel` 没有把实测结果写进 `data-overflow` / `data-end` ⇒ "
        "上面对的那两条选择器永远不命中，渐隐等于没做"
    )
    assert "scrollWidth - el.clientWidth > 1" in panel, (
        "溢出判定不是 `scrollWidth - clientWidth > 1` ⇒ 亚像素抖动会让渐隐在临界宽度上闪"
    )
