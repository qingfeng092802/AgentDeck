import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TaskSummary } from "../src/lib/acp";
import { mapTask, planOf, requestPlan } from "../src/lib/live";
import { adoptTasks, adoptTurns, dispatch, getStudio, setLive, setOutputTab, setScreen, setSource } from "../src/lib/studioEngine";
import { StudioScreen } from "../src/components/studio/StudioScreen";

/** 渲染级判据：工作室屏走真链路时，四张卡说的还是不是真话。
 *
 *  这一组盯的不是"有没有渲染出来"，而是三句**会自己冒出来的假话**：
 *    · 受理台印「解析出 0 页 · 0 条验收标准」—— 真链路没有"解析"这一步，
 *      0 会被读成"什么都没拆出来"，而其实只是卡还没回来；
 *    · PRD / 架构 / 令牌 拿本地 `analyze()` 的猜想填满 —— 它长得跟真的一样；
 *    · 派单键在真链路上"点一下就把单派出去了" —— 那等于绕过后端 A 类审批，这一单就没有
 *      "谁批的"了。它只能切屏，批单仍在待办里。
 */

function row(over: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id: "T01",
    feature_request_id: "11111111-2222-3333-4444-555555555555",
    project_key: "real_demo",
    request_status: "SPLIT",
    request_created_at: "2026-10-27T10:00:00",
    title: "首页含导航与页脚",
    status: "RUNNING",
    depends_on: [],
    executor_agent_id: "qoder",
    reviewer_agent_id: "deepseek-reviewer",
    acceptor_agent_id: "workbuddy",
    difficulty: 2,
    skill_tags: ["fe"],
    handoff_count: 0,
    acceptance_criteria: ["首页含导航与页脚"],
    family: "build",
    artifacts: [],
    ...over,
  };
}

const RAW = "做一个卖手冲咖啡的静态官网，含首页、商品、联系三个页面";
const RID = "11111111-2222-3333-4444-555555555555";

function screen(): string {
  return renderToStaticMarkup(createElement(StudioScreen, { studio: getStudio() }));
}

describe("工作室屏走真链路", () => {
  it("受理台说「真链路」，读数按当前那一步说，不印「解析出 0 页」", () => {
    setSource("live");
    setLive({ projectKey: "real_demo", stage: "meeting", docs: null, stageSince: null });
    adoptTasks([], 0, requestPlan(RAW, RID));

    const html = screen();
    expect(html).toContain("REQ-11111111");
    expect(html).toContain("真链路");
    /* 这一行原来是"会议与拆单进行中（通常 3–7 分钟）"—— 一句话管三个阶段。
       现在按 `pipelineStep` 说具体那一步，并带上"已等"：读者要知道的是"现在卡在哪"，
       不是一句覆盖三分钟的估计。 */
    expect(html).toContain("需求单已落库 · 会议进行中");
    expect(html).not.toContain("解析出 0 页");
    // 需求原文是这一单唯一的"人话"，翻成已立项之后不能丢
    expect(html).toContain("卖手冲咖啡");
  });

  it("会议没被观察到就说「无产出」，不永远显示「进行中」", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));

    setLive({ stage: "meeting" });
    expect(screen()).toContain("会议进行中");

    setLive({ stage: "stalled" });
    const html = screen();
    expect(html).toContain("会议无产出");
    expect(html).toContain("拆单不受影响");
  });

  it("会议桌说真台词，轮次按已有条数算", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    // `stage` 与 `docs` 都是模块级状态：上一格把它们推到了 stalled，这里不重置会串成"断了"
    setLive({ stage: "meeting", docs: null });
    adoptTurns([
      { who: "orchestrator", text: "客户要一个手冲咖啡官网", t: "10:00:00" },
      { who: "pm", text: "三页：首页 / 商品 / 联系", t: "10:00:00" },
    ]);

    const html = screen();
    expect(html).toContain("客户要一个手冲咖啡官网");
    expect(html).toContain("三页：首页 / 商品 / 联系");
    /* 数的是**句**不是**轮**（2026-10-28 多轮会议）：一场 2 轮的会是 8 句，
       分母是 `轮数 × 4`。这一格 `live.rounds` 是 0（没读到），按老单口径算 1 轮 ⇒ 4 句。 */
    expect(html).toContain("已说 2 / 4 句");
    // 已开口的两个座位打勾，没开口的两个不打 —— 顶栏那一排就是"谁已经发过言"
    expect(html.match(/seat-tick/g)?.length).toBe(2);
  });

  it("两个岗位说完、第三个正在说 —— 打字气泡挂在**算出来的**那个座位上", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    setLive({ stage: "meeting", docs: null });
    adoptTurns([
      { who: "orchestrator", text: "客户要一个手冲咖啡官网", t: "10:00:00" },
      { who: "pm", text: "三页：首页 / 商品 / 联系", t: "10:00:00" },
    ]);

    const html = screen();
    // 前两轮固定顺序 ⇒ 正在说的是系统架构专家；设计还没轮到，不该出现它的"正在说"
    expect(html).toContain("系统架构专家");
    expect(html).toContain("正在说");
    expect(html).not.toContain("UI/UX 设计师</b><span class=\"tm\">正在说");
    // 读数条只出现在真链路，且必须带"已等"——没有刻度的等待会被读成卡住
    expect(html).toContain("第 3 / 4 句");

    expect(html).toContain("已等");
  });

  /* 多轮会议（owner 2026-10-28 第 1 条）。两条判据，都对着"写死 4 会说什么假话"：
     ① 4 条说完就判"已锁定方案"，而第 2 轮的讨论还在后头；
     ② 第 2 轮的高亮取 `SEATS[n % 4]`，会把"产品在说"显示成"总管在说"。 */
  it("2 轮的会：分母是 8，第 2 轮从产品说起（总管挪到最后）", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    setLive({ stage: "meeting", docs: null, rounds: 2 });
    adoptTurns([
      { who: "orchestrator", text: "开场", t: "10:00:00" },
      { who: "pm", text: "三页", t: "10:00:00" },
      { who: "cto", text: "风险", t: "10:00:00" },
      { who: "designer", text: "主色", t: "10:00:00" },
    ]);

    const html = screen();
    expect(html).not.toContain("已锁定方案");
    expect(html).toContain("已说 4 / 8 句");
    /* 第 5 条（下标 4）在第二轮，换序后是**产品**先说。
       后端 `speaking_order` 第二轮起把总管挪到最后做收敛 —— 它第一个说就没有别人的新话可收敛。 */
    expect(html).toContain('需求分析专家</b><span class="tm">正在说');
    expect(html).not.toContain('工作流编排专家</b><span class="tm">正在说');
    // 轮数是模块级状态，用完复位，别让后面那几格继承成 8 条的分母
    setLive({ rounds: 0 });
  });

  it("四条齐了就不再有人说，屏上写「已锁定方案」", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    setLive({ stage: "split", docs: null, rounds: 0 });
    adoptTurns(
      ["orchestrator", "pm", "cto", "designer"].map((who, i) => ({ who, text: `第 ${i + 1} 段`, t: "10:00:00" })),
    );

    const html = screen();
    expect(html).toContain("已锁定方案");
    expect(html).not.toContain("正在说");
    expect(html.match(/seat-tick/g)?.length).toBe(4);
  });

  it("文档到了就说明会议那一段走完了 —— 断在第二轮不许继续自称「进行中」", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    setLive({
      stage: "meeting",
      docs: {
        prd: { title: "手冲咖啡官网", pages: [], acceptance: [], out_of_scope: [] },
        arch: null,
        tokens: null,
      },
    });
    adoptTurns([
      { who: "orchestrator", text: "客户要一个手冲咖啡官网", t: "10:00:00" },
      { who: "pm", text: "三页：首页 / 商品 / 联系", t: "10:00:00" },
    ]);

    const html = screen();
    /* 「句」不是「轮」：2 轮的会一共 8 句，把条数说成轮数会让"说到第 5 轮"这种话出现在
       一场只有 2 轮的会上。轮数那一格归 `live.rounds`，条数这一格只报条数。 */
    expect(html).toContain("说到第 2 句就断了");
    expect(html).not.toContain("正在说");
    expect(html).toContain("2 个发言位没人接");
  });

  /* 追加需求（owner 2026-10-28 第 2 条）：出口必须在**产出卡底部**、挨着「修改需求」，
     因为读者读完 PRD 只有两个念头，分岔只在一句"换不换单"。 */
  it("产出卡底部并排两个出口，追加在受理跑着的时候不给按", () => {
    setSource("live");
    adoptTasks([], 0, requestPlan(RAW, RID));
    setLive({ stage: "split", docs: null, requestId: RID, rounds: 0 });

    const html = screen();
    expect(html).toContain("修改需求");
    expect(html).toContain("增加需求");
    expect(html).toContain('data-act="extend-open"');
    // 跑完了（split）就该能按
    expect(html).not.toContain('data-act="extend-open" disabled');

    /* 开会 / 落文档期间不给按：后端那道 409 只看 `status == INTAKE`，
       而**追加后的重开不改 status**，所以那一道挡不住"重开期间又追加一次"。
       真按下去的后果不是报错，是两条链同时改同一份 digest。 */
    setLive({ stage: "meeting" });
    expect(screen()).toContain('data-act="extend-open" disabled');
  });

  it("文档没到时 PRD 页签画骨架，不拿本地猜想填", () => {
    setSource("live");
    // 显式回到"文档节点在写"：上一格测过 stalled，而 stage 是模块级状态，这里不重置会串
    setLive({ docs: null, stage: "docs", stageSince: null });
    adoptTasks([], 0, requestPlan(RAW, RID));
    setOutputTab("prd");

    const html = screen();
    /* 原来这里只有一句"文档节点正在写"。实测文档节点要 16–45 秒，那段时间整张卡是一片空白
       加一句灰字 —— 屏上没有任何东西在动，读起来就是"卡住了"。骨架条不承诺进度，
       只承诺"这一格会被填上"。 */
    expect(html).toContain('class="doc-skel"');
    expect(html.match(/class="skel"/g)?.length).toBe(4);
    expect(html).toContain("已等");
    // 本地 `analyze()` 那套派生字段一个都不许上屏
    expect(html).not.toContain("<h4>页面结构</h4>");
    expect(html).not.toContain("<h4>验收标准</h4>");
  });

  it("会议说完、文档还没到时，屏上必须说得出「现在在做什么」", () => {
    /* 用户的原话：「讨论完后没有反应了，这时候在做什么」。
       那一段是文档节点 + 拆单，实测 16 + 2 秒（关思考），原来屏上一个字都没有。 */
    setSource("live");
    setLive({ docs: null, stage: "docs", stageSince: null });
    adoptTasks([], 0, requestPlan(RAW, RID));
    adoptTurns(["orchestrator", "pm", "cto", "designer"].map((who, i) => ({ who, text: `第 ${i + 1} 段`, t: "10:00:00" })));

    const html = screen();
    expect(html).toContain("会议已锁定 · 正在落 PRD / 架构 / 令牌");
    // 三段刻度：会议已过、文档当前、拆卡未到
    expect(html).toContain('<i data-s="done"></i><i data-s="now"></i><i data-s="todo"></i>');
    // 顶栏那一行也不许再自称"会议进行中"
    expect(html).toContain("编排主持 · 会议已锁定 · 正在落 PRD / 架构 / 令牌");
  });

  it("文档到了、卡还没到时，读数换成「正在拆任务卡」", () => {
    setSource("live");
    setLive({
      stage: "split",
      stageSince: null,
      docs: {
        prd: { title: "手冲咖啡官网", pages: [{ label: "首页", file: "index.html", duty: "品牌" }], acceptance: ["能看"], out_of_scope: [] },
        arch: { stack: "原生", why: "够用", risk: "无" },
        tokens: { name: "深烘", bg: "#FFFFFF", surface: "#FAFAFA", ink: "#111111", muted: "#777777", accent: "#6F4E37", accent2: "#C8843B", line: "#DDDDDD" },
      },
    });
    adoptTasks([], 0, requestPlan(RAW, RID));
    adoptTurns(["orchestrator", "pm", "cto", "designer"].map((who, i) => ({ who, text: `第 ${i + 1} 段`, t: "10:00:00" })));

    const html = screen();
    expect(html).toContain("文档已落 · 正在拆任务卡");
    expect(html).toContain('<i data-s="done"></i><i data-s="done"></i><i data-s="now"></i>');
  });

  it("卡到了就不再画读数条 —— 屏上不该留一条永远在呼吸的条", () => {
    setSource("live");
    setLive({ docs: null, stage: "split", stageSince: null });
    adoptTasks([{ ...mapTask(row()), id: "T01" }], 2, requestPlan(RAW, RID));

    const html = screen();
    expect(html).not.toContain('class="meet-prog');
    /* 卡到了 ⇒ 抬头进 `done` 那一支。这里原来断的是"批单在右下待办"，
       而那句话随待办面板一起删掉了（2026-10-28）；现在断的是同一支里的新文案。 */
    expect(html).toContain("任务卡已出 · 执行中，进度见执行看板");
  });

  it("观察窗到期就撤掉骨架，并停掉呼吸", () => {
    /* 还在呼吸的骨架等于在说"马上就来"。观察窗到期正是"它不会来了"那一刻。 */
    setSource("live");
    setLive({ docs: null, stage: "stalled", stageSince: null });
    adoptTasks([], 0, requestPlan(RAW, RID));
    adoptTurns(["orchestrator", "pm", "cto", "designer"].map((who, i) => ({ who, text: `第 ${i + 1} 段`, t: "10:00:00" })));
    setOutputTab("prd");

    const html = screen();
    expect(html).not.toContain('class="doc-skel"');
    expect(html).toContain("这一单没有文档");
    expect(html).toContain("停住了");
  });

  it("文档到了以后三格读的是后端结构，不是前端猜的", () => {
    setSource("live");
    const docs = {
      prd: {
        title: "手冲咖啡官网",
        pages: [
          { label: "首页", file: "index.html", duty: "品牌与主推品" },
          { label: "商品", file: "products.html", duty: "三种豆子" },
        ],
        acceptance: ["首屏能看到主推品", "375px 下导航不换行"],
        out_of_scope: ["在线支付"],
      },
      arch: { stack: "原生 HTML/CSS/JS", why: "三页内容固定", risk: "购物车只是 UI，容易误解" },
      tokens: {
        name: "烘焙棕 / 米白",
        bg: "#FBF7F0",
        surface: "#FFFFFF",
        ink: "#2B1D14",
        muted: "#7A6552",
        accent: "#6B4226",
        accent2: "#C0763C",
        line: "#E8DCCB",
      },
    };
    setLive({ docs });
    adoptTasks([], 0, requestPlan(RAW, RID, docs));

    setOutputTab("prd");
    const prd = screen();
    expect(prd).toContain("手冲咖啡官网");
    expect(prd).toContain("products.html");
    expect(prd).toContain("首屏能看到主推品");
    expect(prd).toContain("在线支付");

    setOutputTab("arch");
    const arch = screen();
    expect(arch).toContain("原生 HTML/CSS/JS");
    expect(arch).toContain("购物车只是 UI，容易误解");

    setOutputTab("token");
    expect(screen()).toContain("烘焙棕 / 米白");
  });

  it("派单键在真链路上可点，但点它只切屏，不碰后端那道审批门", () => {
    setSource("live");
    const rows = [row()];
    adoptTasks(rows.map(mapTask), 2, planOf(rows, "real_demo", RID, RAW));
    setOutputTab("cards");

    const html = screen();
    expect(html).toContain("首页含导航与页脚");
    expect(html).not.toContain("后端暂无此读面");

    /* 2026-10-28 owner：「改为确认派单，进入执行，然后点击就会跳转到执行看板」。
       所以这颗键不再 disabled（原来它长得像动作、实际点不动，而流程里没有第二个向前的入口）。
       但"可点"不等于"能推进"：真链路的派单是后端 A 类审批，页面上多一个能推进的键，
       那一单就没有"谁批的"了 —— 它只切屏。 */
    expect(html).toContain("确认派单，进入执行");
    expect(html).not.toMatch(/id="btnDispatch"[^>]*disabled/);

    /* 同一轮 owner 又说「去除所有待办清单」：那块浮层整块删掉了，于是**屏上不许再出现
       任何指向它的字**。这一条钉的是"文案跟着组件一起走" —— 只删组件不改文案，
       就又是一句"照着这句话去找，屏上什么都没有"。 */
    expect(html).not.toContain("待办");

    /* 被点下去的是"切屏"，不是 `dispatch()`。这条判据盯的就是那半句落差：
       `dispatch()` 在 live 档自己 `return`，所以真被误接到它身上时屏上一动不动 ——
       而用户看到的是"点了没反应"，正是这一轮要消灭的东西。 */
    const phase = getStudio().phase;
    dispatch();
    expect(getStudio().phase).toBe(phase);
    expect(getStudio().screen).toBe("studio");

    setScreen("board");
    expect(getStudio().screen).toBe("board");
    setScreen("studio");
  });

  it("整单交付之后，抬头说「已交付」，不再指任何面板", () => {
    setSource("live");
    const rows = [row({ id: "T01", status: "DELIVERED" }), row({ id: "T02", status: "DELIVERED_WITH_ISSUES" })];
    adoptTasks(rows.map(mapTask), 4, planOf(rows, "real_demo", RID, RAW));
    setOutputTab("cards");

    /* 这一句原来是常量：只要卡出完了就写"批单在右下待办"。交付完的单上它既不真（没有待批的单），
       2026-10-28 之后也指不到东西（面板已删）。现在只说这一单走到哪了。 */
    const html = screen();
    expect(html).toContain("已交付，留痕在右栏");
    expect(html).not.toContain("待办");
  });
});


describe("机器代签那一格：原样带上来，不做判断", () => {
  /* 前端**不许**自己推断"这张卡是不是机器签的"。
     后端的 `auto_approved` 来自 EN-14 里那条 `review_auto_accepted` 事件 —— 它记的是"发生过一次
     代签"，不是"这张卡看起来像自动过的"。前端若按状态（比如"已完成但没有人工验收记录"）去猜，
     猜错的方向是把人工签的字标成机器的，而那是审计上最不能错的一件事。 */
  it("true 就带 true，false/缺省就带 false", () => {
    expect(mapTask(row({ auto_approved: true })).autoApproved).toBe(true);
    expect(mapTask(row({ auto_approved: false })).autoApproved).toBe(false);
    const bare = row();
    delete (bare as { auto_approved?: boolean }).auto_approved;
    expect(mapTask(bare).autoApproved).toBe(false);
  });

  it("auto_review 的代号原样传（界面据此说'因为什么没代签'）", () => {
    expect(mapTask(row({ auto_review: "auto_accept_not_low_risk" })).autoReview).toBe(
      "auto_accept_not_low_risk",
    );
    expect(mapTask(row()).autoReview).toBeNull();
  });
});
