import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe as suite, expect, it } from "vitest";
import { AppShell } from "../src/shell/AppShell";
import { TopBar } from "../src/shell/TopBar";

/** 壳的合同（2026-10-27 改版，随 1:1 复刻原型一起换掉）。
 *
 *  ① 一级视图只剩**一档**「项目工作室」。原来那两档（任务图 / 治理台）连同它们的 hash 一起退休，
 *     视图内部分成"工作室屏 / 执行看板屏"两段，由引擎状态 `screen` 决定，不再进 hash。
 *  ② 顶栏右上角只剩**两颗键**：浅色/深色、设置。原型那四颗演示控制键（倍速 / 暂停 / 注入故障 /
 *     重置）全部退役 —— 它们是给看演示的人用的，不是产品能力；重置留在受理卡里（"重新立项"）。
 *  ③ 顶栏读数来自引擎（`useStudio`），不再吃后端快照：这一版是纯前端推演，
 *     读数如果还从后端来，就会出现"会议已经开到第 5 轮，顶栏还写着待立项"。
 */

const TOPBAR_PROPS = {
  theme: "dark" as const,
  onThemeToggle: () => {},
  onOpenSettings: () => {},
};

const SHELL_PROPS = {
  theme: "dark" as const,
  onThemeToggle: () => {},
};

function visibleText(html: string): string {
  return html.replace(/<[^>]*>/g, "").trim();
}

suite("TopBar —— 品牌、面包屑、两档状态", () => {
  it("左边是 ◆ AgentDeck 指挥台；面包屑只写位置，状态归右边那颗胶囊", () => {
    const html = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    expect(html).toContain("◆");
    expect(html).toContain("AgentDeck");
    expect(html).toContain("指挥台");
    // 工作室屏（默认 screen）的那一段名字 —— 面包屑的叶子就是它，不留空
    expect(html).toContain("项目工作室");
    // 2026-10-27 第四轮：面包屑原来在未立项时写「待立项」，可右上那颗胶囊写的也是「待立项」，
    // 同一行里同一个词出现两次。现在分工是：位置归面包屑（只随 `screen` 变），
    // 状态归胶囊（它是状态机的一部分，会随流程变）。所以「待立项」只许出现在胶囊里，
    // 不许在面包屑里再来一份 —— 下面这条数的是它出现的次数。
    expect(html).toContain('class="phase-pill"');
    expect(html).toContain("待立项");
    expect((html.match(/待立项/g) ?? []).length).toBe(1);
    expect(html).toContain("data-p=");
  });

  it("没有第二个一级入口：治理台 / 办公室 / 专家台都不在顶栏", () => {
    const html = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    for (const gone of ["治理台", "办公室", "专家台", "任务图"]) {
      expect(html, gone).not.toContain(gone);
    }
    expect(html).not.toContain('href="#panel-');
  });

  it("右上角只有两颗键：浅色/深色 与 设置", () => {
    const html = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    expect((html.match(/class="ibtn"/g) ?? []).length).toBe(2);
    expect(html).toContain("浅色");
    expect(html).toContain("设置");
    expect(html).toContain('aria-haspopup="dialog"');
  });

  it("原型那四颗演示控制键一个都不在：倍速 / 暂停 / 注入故障 / 重置 / 彩排", () => {
    const html = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    for (const gone of ["倍速", "暂停", "注入故障", "重置", "彩排", "DEMO", "发起需求", "待处理", "身份"]) {
      expect(html, gone).not.toContain(gone);
    }
    expect(html).not.toContain("rehearsal-beacon");
    expect(html).not.toContain('data-demo="true"');
  });

  it("四计数随旧座舱一起退役：顶栏不再画卡/层/依赖/关键边", () => {
    const html = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    for (const gone of ["关键边", "依赖", ">卡<", ">层<"]) {
      expect(html, gone).not.toContain(gone);
    }
    expect(visibleText(html)).not.toMatch(/卡\s*\d/);
  });
});

suite("TopBar —— 主题切换键", () => {
  it("深色时按钮写「浅色」，浅色时写「深色」：文案说的是**点了会变成什么**", () => {
    const dark = renderToStaticMarkup(createElement(TopBar, TOPBAR_PROPS));
    // 量可见文字而不是整段 HTML：`title` 里那句话两种情况都会同时提到深浅色
    const darkText = visibleText(dark);
    expect(darkText).toContain("浅色");
    expect(darkText).not.toContain("深色");
    expect(dark).toContain('aria-pressed="false"');

    const light = renderToStaticMarkup(createElement(TopBar, { ...TOPBAR_PROPS, theme: "light" }));
    const lightText = visibleText(light);
    expect(lightText).toContain("深色");
    expect(lightText).not.toContain("浅色");
    expect(light).toContain('aria-pressed="true"');
  });
});

suite("AppShell —— 一档视图 + 设置弹窗", () => {
  it("根节点挂 .studio-root：整套皮肤的选择器都以它打头", () => {
    const html = renderToStaticMarkup(createElement(AppShell, SHELL_PROPS));
    expect(html).toContain('class="studio-root"');
    expect(html).toContain('<header class="top">');
    /* 默认档是项目工作室，容器 id 因此是 `view-studio`。
       2026-10-29：旧的 `?live` 实时看板整页已删；真链路从受理台的项目键进入。 */
    expect(html).toContain('id="view-studio"');
  });

  it("只挂工作室一档：治理台与办公室的容器都不在 DOM 里", () => {
    const html = renderToStaticMarkup(createElement(AppShell, SHELL_PROPS));
    expect(html).not.toContain('id="view-governance"');
    expect(html).not.toContain('id="view-office"');
    expect(html).not.toContain('data-office-group="');
    expect(html).not.toContain("hud-grid");
    expect(html).not.toMatch(/(?<![\w-])hidden(?![\w-])/);
  });

  it("设置弹窗挂在壳上、默认关闭（关闭时什么都不渲染）", () => {
    const html = renderToStaticMarkup(createElement(AppShell, SHELL_PROPS));
    // 判据不能再用 `role="dialog"` 当"有弹窗"的指纹：2026-10-27 起任务抽屉也是一个 dialog，
    // 而抽屉是常驻 DOM、用 class 控制滑入滑出的（它需要过渡动画），所以那个字符串永远在。
    // 改成设置弹窗**独有**的两样东西：它的容器类名与它的主按钮文案。
    expect(html).not.toContain("settings-sheet");
    expect(html).not.toContain("保存配置");
  });
});

suite("源码合同 —— 壳只做壳的事", () => {
  const app = readFileSync(resolve(__dirname, "../src/App.tsx"), "utf8");
  const shell = readFileSync(resolve(__dirname, "../src/shell/AppShell.tsx"), "utf8");
  const topbar = readFileSync(resolve(__dirname, "../src/shell/TopBar.tsx"), "utf8");
  const view = readFileSync(resolve(__dirname, "../src/views/StudioView.tsx"), "utf8");

  it("AppShell 里没有视图分支：只剩一档，二选一留在 StudioView", () => {
    expect(shell).not.toContain('view === "studio"');
    expect(shell).not.toContain('view === "governance"');
    expect(shell).not.toContain("viewFromHash");
    expect(shell).not.toMatch(/(?<![\w-])hidden(?![\w-])/);
    expect(shell).not.toContain("display:");
    expect(shell).not.toContain("visibility");
    expect((shell.match(/<StudioView \/>/g) ?? []).length).toBe(1);
  });

  it("工作室的两段（工作室屏 / 执行看板屏）在 StudioView 里二选一", () => {
    // 2026-10-27 第四轮：条件从 `screen === "studio" ?` 改成 `screen === "board" && plan ?`。
    // 判的仍是"恰好一条三元、两个分支各挂一次"，外加一条新保证：
    // 看板分支必须带 `plan` —— 看板的每一块都建立在"已立项"之上，未立项不该有看板可画。
    expect(view).toContain('studio.screen === "board" && studio.plan ?');
    expect((view.match(/<StudioScreen/g) ?? []).length).toBe(1);
    expect((view.match(/<BoardScreen/g) ?? []).length).toBe(1);
  });

  it("App.tsx 只持有主题：身份、视图、快照、彩排都已退出壳", () => {
    expect(app).toContain('const THEME_KEY = "acp-theme"');
    expect(app).toContain("dataset.theme");
    // 按**标识符 / import 路径**判，不按词判：注释里提到这些词是在解释"为什么删掉了"，那不算复活
    for (const gone of [
      "viewFromHash",
      "hashOfView",
      "principalOrDefault",
      "data/useSnapshot",
      "useRehearsal",
      "rehearsalStore",
      "PANELS",
      "COLUMNS",
      "anchorOf(",
    ]) {
      expect(app, gone).not.toContain(gone);
    }
  });

  it("设置入口只在顶栏，弹窗只在壳上", () => {
    expect(shell).toContain("<SettingsModal");
    expect(shell).toContain("open={settingsOpen}");
    expect(shell).toContain("onOpenSettings={() => setSettingsOpen(true)}");
    expect(topbar).toContain("onOpenSettings");
    expect(topbar).not.toContain("<SettingsModal");
  });

  it("顶栏读引擎不读快照：没有任何 snapshot / 计数入参", () => {
    expect(topbar).toContain("useStudio()");
    // 同样按标识符判：注释里那句"不再吃 snapshot"是解释，不是残留
    for (const gone of ["snapshot.", "Snapshot", "counters", "pendingCount", "principalOrDefault", "onRequest"]) {
      expect(topbar, gone).not.toContain(gone);
    }
  });
});

suite("阶段条：进度只动 transform，不动 layout", () => {
  const css = readFileSync(resolve(__dirname, "../src/styles/studio.css"), "utf8");
  const board = readFileSync(resolve(__dirname, "../src/components/studio/BoardScreen.tsx"), "utf8");

  it("不再有 transition:width（每次进度变化都触发全量回流）", () => {
    expect(css).not.toContain("transition:width .5s");
    expect(css).toContain("transition:transform .5s");
    expect(css).toContain("transform-origin:left");
  });

  it("进度用 scaleX 表达，宽度固定 100%", () => {
    expect(board).toContain("scaleX(");
    expect(board).not.toContain("width: `${percent}%`");
    expect(css).toContain("width:100%");
  });
});
