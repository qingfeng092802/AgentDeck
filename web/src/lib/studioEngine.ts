/**
 * 项目工作室 · 纯前端引擎（原型 `项目工作室-原型.html` 的逐条移植）。
 *
 * 为什么这一版不读后端：owner 2026-10-27 定的是「先纯前端 1:1，真后端下一轮接线」。
 * 所以这里的每一条数据都是**本机推演**出来的，不假装是库里的行 —— 页面右上角那句
 * 「原型：不连后端」和这里是同一件事的两种说法。
 *
 * 与原型的三处偏离（都不是偷懒）：
 * 1) 字符串不再拼 HTML 再塞进 DOM，而是把「有语气的那几个词」标成 `<em>` / `<code>`
 *    交给 React 渲染；需要富文本的地方（审查结论、交付预览）才走 innerHTML，
 *    且所有动态值都过 `esc()` —— 需求原话是用户输入，直接拼进 innerHTML 就是 XSS。
 * 2) 状态机从「全局可变对象 + 每帧重画 DOM」改成「全局可变对象 + 版本号 + React 订阅」。
 *    推进逻辑一字未改，改的只是"画"的方式。
 * 3) 原型那六个控制键（倍速 / 暂停 / 注入故障 / 重置）不进产品顶栏；演示直链
 *    `?studio=demo&speed=4&dispatch=1` 已于 2026-10-29 退役（不再自动播放），
 *    固定脚本只由受理台右上角「固定演示」显式启动。
 */

/* ================================================================
   一、编制：12 个位，总管也在名册里
   ================================================================ */

export interface AgentDef {
  readonly id: string;
  /** 主行：**专家职级**（`工作流编排专家` / `需求分析专家` …）。左栏与会议桌读它。 */
  readonly title: string;
  /** 身份缩写。做成 2px 小签而不是裸字：标题本身以「产品/前端/审查」开头，裸字会读成"产品产品经理"。 */
  readonly mono: string;
  readonly tone: string;
  /** 副行：**核心职责**（`拆单与调度` / `PRD 与验收标准` …）。左栏第二行与工位抽屉的「职责」读它。 */
  readonly note: string;
}

/** 十二个位：**专家职级 + 核心职责**（owner 2026-10-28「UI 质感升维」）。
 *
 *  这一版把职称从"产品经理 / 技术总监 / 全能工程师"换成工业级 HUD 的写法，
 *  两行固定：主行是职级（13px 加粗亮白），副行是职责（12px 常规暗灰）。
 *  换的只是**名字与排版**，不动任何底层数据 —— `id` / `mono` / `tone` 一个字没改，
 *  所以 `agentTitle()` / `agentMono()` 的调用点全部照旧，会议、看板、活动流、交付清单
 *  跟着一起变，不需要各自再改一遍。
 *
 *  `mono` 保持两字小签（总管 / 产品 / 技术 …）：它是**色块上的那两个字**，
 *  不是职称的缩写 —— 职称变长了，小签再跟着变长就会把左栏挤成一列碎词。 */
export const STUDIO_AGENTS: readonly AgentDef[] = [
  { id: "orchestrator", title: "工作流编排专家", mono: "总管", tone: "--a1", note: "拆单与调度" },
  { id: "pm", title: "需求分析专家", mono: "产品", tone: "--a2", note: "PRD 与验收标准" },
  { id: "cto", title: "系统架构专家", mono: "技术", tone: "--a3", note: "技术选型与结构" },
  { id: "designer", title: "UI/UX 设计师", mono: "设计", tone: "--a4", note: "设计系统与令牌" },
  { id: "fe", title: "前端开发专家", mono: "前端", tone: "--a5", note: "页面骨架与交互" },
  { id: "be", title: "后端开发专家", mono: "后端", tone: "--a6", note: "接口与数据模型" },
  { id: "writer", title: "技术文档专家", mono: "文档", tone: "--a2", note: "规范手册与说明" },
  { id: "qa", title: "测试开发专家", mono: "测试", tone: "--a3", note: "E2E 回归与打回" },
  { id: "sec", title: "应用安全专家", mono: "安全", tone: "--a5", note: "漏洞扫描与越权" },
  { id: "reviewer", title: "独立代码审查员", mono: "审查", tone: "--a1", note: "门禁比对与互斥" },
  { id: "acceptor", title: "交付验收专家", mono: "验收", tone: "--a4", note: "终局签字与交付" },
  { id: "rescue", title: "全栈保障专家", mono: "全能", tone: "--a6", note: "异常接管与补位" },
];

export const AGENT_OF: Readonly<Record<string, AgentDef>> = Object.fromEntries(
  STUDIO_AGENTS.map((agent) => [agent.id, agent]),
);

/** 编制 = 全部 12 个位，**含总管**。
 *
 *  2026-10-27 owner 指出：「编制」下面数出来只有 11 行，而门上写着「12 工位」——
 *  缺的那一位正是总管自己。当时的理由是"总管升成了一扇门，不占椅子"，但那是**入口**的
 *  说法，不是**编制**的说法：门说的是"点这里进工作室"，编制说的是"这个工作室有谁"。
 *  总管既在名册里（第一行），也另外有一扇门，两件事不冲突 —— 冲突的是把门当成椅子，
 *  于是名册少一人、门上的数字对不上。
 *
 *  顺带把它的职称从「项目工作室」改成「工作室总管」：项目工作室是**这间屋子**的名字
 *  （门、面包屑都写它），工作室总管是**那个人**。原来两处共用一个名字，
 *  会议桌上就出现了「总管 · 项目工作室」这种把房间当人的读法。
 *
 *  2026-10-28 之后那位人的职称是「工作流编排专家」（见上面那张表），"项目工作室"仍然是
 *  **屋子**的名字 —— 上面这条结论没变，变的只是那个人的叫法。 */
export const STUDIO_SEATS: readonly AgentDef[] = STUDIO_AGENTS;

/** 名册外的身份（后端真实 Agent：workbuddy / qoder / doubao / deepseek-reviewer）**按 id 说话**。
 *
 *  2026-10-27 接线后名册多了一种人：引擎那 12 个是推演工位，后端那 4 个是注册 Agent，
 *  两者不是一张表。查不到名册时回落到 `orchestrator` 会把 qoder 写成"总管"—— 那是**冒充**，
 *  不是兜底。所以回落规则是：标题用原 id，缩写截前四字，色相按 id 稳定散列到六档之一
 *  （同一 id 每次刷新同一个色，不会闪）。 */
function unknownSeat(id: string): { tone: string; title: string; mono: string } {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return { tone: `--a${(hash % 6) + 1}`, title: id || "未知", mono: id.length <= 5 ? id || "—" : id.slice(0, 4) };
}

export function toneVar(id: string): string {
  const agent = AGENT_OF[id];
  return `var(${agent ? agent.tone : unknownSeat(id).tone})`;
}

export function agentTitle(id: string): string {
  return AGENT_OF[id]?.title ?? unknownSeat(id).title;
}

/** 两字缩写。**窄列里唯一该用的那一套。**
 *
 *  2026-10-27 之前同一屏里有四种写法，用户在两栏之间对不上号：
 *    左栏 / 会议 / DAG  → `agent.mono`            → 设计 / 审查
 *    活动流 / 审查 / 总线 → `agentTitle().slice(0,2)` → UI / 独立
 *    任务详情的交付清单   → 完整职称               → 独立审查员
 *    交付页的归属列       → 按扩展名猜             → 后端 / 文档 / 前端
 *  第二套是最坏的那种：`"独立审查员".slice(0,2)` 得到"独立"、`"UI 设计师".slice(0,2)` 得到"UI" ——
 *  既不是缩写也不是职称，是一个从长字符串上切下来的碎片，读的人认不出它指的是哪个工位。
 *
 *  规则收敛成两条：
 *    · 窄列（活动流的作者列、审查卡的角标、文件总线的归属列、交付清单的归属列）→ 一律 `agentMono()`
 *    · 句子（"出自 独立审查员"这种完整表达）→ 一律 `agentTitle()`
 *  `slice(0, 2)` 这个派生删掉，别再长回来。判据 = `test_the_agent_label_has_one_abbreviation`。
 */
export function agentMono(id: string): string {
  return AGENT_OF[id]?.mono ?? unknownSeat(id).mono;
}

/* ================================================================
   二、工具
   ================================================================ */

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** 唯一的转义入口。凡是进 innerHTML 的动态值都必须先过这里。 */
export function esc(value: unknown): string {
  return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] ?? ch);
}

function nowHM(): string {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

function nowDate(): string {
  const d = new Date();
  return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"), String(d.getDate()).padStart(2, "0")].join("-");
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/* ================================================================
   三、需求解析：自然语言 → 结构化立项材料
   —— 这一层是"通用"的根。换一句需求，页面/验收/任务卡都跟着变，
      但岗位与流程不变。不追求 NLP 精度，追求"规则可解释、可替换"。
   ================================================================ */

interface PageDef {
  readonly key: string;
  readonly label: string;
  readonly file: string;
  readonly keys: readonly string[];
}

const PAGE_LIB: readonly PageDef[] = [
  { key: "home", label: "首页", file: "index.html", keys: ["首页", "主页", "落地页", "首屏", "门户"] },
  { key: "products", label: "商品展示", file: "products.html", keys: ["商品展示", "商品", "产品", "菜单", "款式", "服务项目", "作品"] },
  { key: "detail", label: "商品详情", file: "detail.html", keys: ["详情"] },
  { key: "cases", label: "案例", file: "cases.html", keys: ["案例", "客户故事"] },
  { key: "pricing", label: "价格", file: "pricing.html", keys: ["价格", "定价", "套餐", "报价", "资费"] },
  { key: "booking", label: "在线预约", file: "booking.html", keys: ["预约", "订场", "排课", "挂号"] },
  { key: "courses", label: "课程列表", file: "courses.html", keys: ["课程", "课表", "班型"] },
  { key: "approval", label: "审批", file: "approval.html", keys: ["审批", "工作流", "审批流"] },
  { key: "member", label: "个人中心", file: "member.html", keys: ["个人中心", "会员中心", "我的"] },
  { key: "cart", label: "购物车", file: "cart.html", keys: ["购物车"] },
  { key: "login", label: "登录", file: "login.html", keys: ["登录", "注册", "账号"] },
  { key: "dashboard", label: "数据看板", file: "dashboard.html", keys: ["看板", "仪表盘", "数据概览", "报表", "大屏"] },
  { key: "about", label: "关于我们", file: "about.html", keys: ["关于我们", "关于", "品牌故事", "团队介绍"] },
  { key: "news", label: "资讯", file: "news.html", keys: ["资讯", "博客", "文章", "新闻"] },
  { key: "contact", label: "联系我们", file: "contact.html", keys: ["联系我们", "联系", "咨询", "留言"] },
];

export interface Palette {
  readonly name: string;
  readonly bg: string;
  readonly surface: string;
  readonly ink: string;
  readonly muted: string;
  readonly accent: string;
  readonly accent2: string;
  readonly line: string;
}

const PALETTES: readonly (Palette & { readonly keys: readonly string[] })[] = [
  {
    keys: ["咖啡", "手冲", "茶", "烘焙", "面包", "餐厅", "餐饮", "酒", "食品", "甜品"],
    name: "烘焙棕 / 米白", bg: "#FBF7F0", surface: "#FFFFFF", ink: "#2B1D14", muted: "#7A6552", accent: "#6B4226", accent2: "#C0763C", line: "#E8DCCB",
  },
  {
    keys: ["健身", "运动", "瑜伽", "跑步", "体育"],
    name: "动能黑 / 荧光绿", bg: "#0E1116", surface: "#161B22", ink: "#F2F6F4", muted: "#93A29B", accent: "#B8F24A", accent2: "#4ADE80", line: "#252C35",
  },
  {
    keys: ["医疗", "健康", "诊所", "牙科", "医院", "康复"],
    name: "临床青 / 冷白", bg: "#F4FBFA", surface: "#FFFFFF", ink: "#0E2A2A", muted: "#5A7C7A", accent: "#0E7C7B", accent2: "#39A9A4", line: "#D6EDEB",
  },
  {
    keys: ["金融", "银行", "保险", "投资", "理财", "基金"],
    name: "藏青 / 香槟金", bg: "#F7F8FB", surface: "#FFFFFF", ink: "#0F1B33", muted: "#5B6B85", accent: "#12324F", accent2: "#C9A227", line: "#DDE3ED",
  },
  {
    keys: ["教育", "培训", "课程", "学校", "学习"],
    name: "学院蓝 / 暖橙", bg: "#F7F9FF", surface: "#FFFFFF", ink: "#101B33", muted: "#5A6785", accent: "#1D4ED8", accent2: "#F97316", line: "#DCE4F5",
  },
  {
    keys: ["时尚", "美妆", "服装", "珠宝", "香水"],
    name: "墨黑 / 藕粉", bg: "#FAF7F7", surface: "#FFFFFF", ink: "#1A1416", muted: "#6E5C60", accent: "#1A1A1A", accent2: "#D98C9A", line: "#EBDFE1",
  },
  {
    keys: ["律师", "法律", "咨询", "会计", "事务所"],
    name: "石青 / 米灰", bg: "#F8F8F6", surface: "#FFFFFF", ink: "#1C2430", muted: "#5C6775", accent: "#2C3E50", accent2: "#A67C52", line: "#E3E4E0",
  },
];

const DEFAULT_PALETTE: Palette = {
  name: "深空青 / 石墨", bg: "#0B1220", surface: "#131C2B", ink: "#EAF1FA", muted: "#93A3B8", accent: "#45D6F0", accent2: "#9A8CF5", line: "#22303F",
};

/** 真实数据没有配色主张时的占位。导出它是因为 live 适配层要拼一个**最小 Plan**，
 *  而"深空青"必须与原型那套默认值同源 —— 再抄一份 hex 就会出现两个默认色。 */
export { DEFAULT_PALETTE };

export interface AcceptanceCriterion {
  readonly id: string;
  readonly text: string;
}

export interface Plan {
  readonly raw: string;
  readonly type: "static" | "miniapp" | "dashboard" | "saas" | "commerce";
  readonly typeLabel: string;
  readonly feat: {
    readonly responsive: boolean;
    readonly cart: boolean;
    readonly form: boolean;
    readonly auth: boolean;
    readonly payment: boolean;
    readonly data: boolean;
    readonly realtime: boolean;
    readonly api: boolean;
    readonly admin: boolean;
    readonly seo: boolean;
    readonly i18n: boolean;
  };
  readonly pages: readonly PageDef[];
  readonly palette: Palette;
  readonly stack: string;
  readonly why: string;
  readonly ac: readonly AcceptanceCriterion[];
  readonly outOfScope: readonly string[];
  /** 架构里那一处真实风险。推演档没有这一格（`analyze()` 不产风险），所以是可选的。 */
  readonly risk?: string;
  readonly title: string;
  readonly prj: string;
  /** 由 `buildTasks` 回填：非隐藏、phase>=2 的执行卡张数。 */
  cards: number;
}

function guessTitle(text: string, typeLabel: string): string {
  const m = text.match(/(?:做|建|搭|开发|设计|要)(?:一个|一家|个|一套)?([^，。,.；;]{2,20}?)(?:网站|官网|小程序|页面|系统|平台|看板)/);
  if (m) {
    let t = m[1]
      .replace(/^(一个|一家|个|套|卖|做|开|经营|主打)+/g, "")
      .replace(/(的)?(静态|动态|响应式|简单|小型|展示型|官方|企业)+$/g, "")
      .replace(/的$/, "")
      .trim();
    if (t.length > 10) t = t.slice(0, 10);
    if (t) return t;
  }
  return typeLabel;
}

export function analyze(raw: string): Plan {
  const text = raw.trim();
  const has = (...keys: string[]): boolean => keys.some((key) => text.includes(key));

  const feat = {
    responsive: has("手机", "移动", "响应式", "适配", "平板", "多端", "端上"),
    cart: has("购物车", "加购", "下单"),
    form: has("表单", "联系", "留言", "咨询", "预约", "报名"),
    auth: has("登录", "注册", "账号", "会员", "用户体系"),
    payment: has("支付", "付款", "结算", "收费", "购买"),
    data: has("数据", "报表", "看板", "图表", "统计", "指标", "大屏"),
    realtime: has("实时", "推送", "直播", "websocket", "WebSocket"),
    api: has("接口", "后端", "数据库", "服务端", "API", "api"),
    admin: has("后台", "管理端", "运营", "CMS", "内容管理"),
    seo: has("SEO", "seo", "搜索排名", "收录"),
    i18n: has("多语言", "英文", "国际化", "出海"),
  };

  let type: Plan["type"] = "static";
  let typeLabel = "静态多页站点";
  if (has("小程序")) {
    type = "miniapp";
    typeLabel = "微信小程序";
  } else if (has("看板", "大屏", "仪表盘", "数据平台", "数据可视化")) {
    type = "dashboard";
    typeLabel = "数据看板";
  } else if (has("后台", "管理端", "SaaS", "saas", "系统", "平台", "审批", "工作流")) {
    type = "saas";
    typeLabel = "业务系统";
  } else if (has("官网", "静态", "落地页", "展示页", "宣传页")) {
    type = "static";
    typeLabel = "静态多页站点";
  } else if (has("商城", "电商", "店铺", "下单")) {
    type = "commerce";
    typeLabel = "电商站点";
  }

  // 页面清单：显式命中的页 + 类型兜底。
  // 注意：购物车 / 详情 / 登录 常以"按钮""功能"出现，不构成页面 —— 必须带"页"字才算。
  const strict: Record<string, RegExp> = {
    cart: /购物车(页面|页|列表|界面)/,
    detail: /详情(页面|页)/,
    login: /(登录|注册)(页面|页)|登录注册/,
  };
  const picked: string[] = [];
  for (const page of PAGE_LIB) {
    const rule = strict[page.key];
    if (rule) {
      if (rule.test(text)) picked.push(page.key);
      continue;
    }
    if (page.keys.some((key) => text.includes(key))) picked.push(page.key);
  }
  if (!picked.includes("home")) picked.unshift("home");
  let pages = PAGE_LIB.filter((page) => picked.includes(page.key));
  if (pages.length <= 1) {
    if (type === "commerce") pages = PAGE_LIB.filter((page) => ["home", "products", "cart", "contact"].includes(page.key));
    else if (type === "dashboard") pages = PAGE_LIB.filter((page) => ["home", "dashboard", "contact"].includes(page.key));
    else pages = PAGE_LIB.filter((page) => ["home", "products", "contact"].includes(page.key));
  }
  if (type === "saas" && !pages.some((page) => page.key === "login")) {
    const login = PAGE_LIB.find((page) => page.key === "login");
    if (login) pages = [pages[0], login, ...pages.slice(1)];
  }
  pages = pages.slice(0, 6);

  let palette: Palette = DEFAULT_PALETTE;
  for (const candidate of PALETTES) {
    if (candidate.keys.some((key) => text.includes(key))) {
      palette = candidate;
      break;
    }
  }

  let stack = "原生 HTML / CSS / JS（无构建链）";
  let why = "页面数少、无登录与数据写入，引框架只会让交付物变重；零依赖才能保证断网可用。";
  if (type === "dashboard") {
    stack = "Vite + TypeScript + Canvas 图表";
    why = "图表与状态多，浏览器原生节点拼装会失控；但不引 UI 库，避免样式返工。";
  } else if (type === "saas") {
    stack = "React + TypeScript + FastAPI + PostgreSQL";
    why = "有多角色与写操作，必须有鉴权与事务边界。";
  } else if (type === "miniapp") {
    stack = "小程序原生框架 + 云开发";
    why = "客户要微信内打开，原生包体最小、审核最稳。";
  } else if (type === "commerce") {
    stack = "原生 HTML/CSS/JS + 本地 mock 数据";
    why = "本期不含真实支付，先把浏览与加购路径跑通。";
  }

  // 验收标准（编号即门禁的比对行）
  const ac: AcceptanceCriterion[] = [];
  ac.push({ id: "AC-1", text: `页面齐全且可互相跳转：${pages.map((page) => page.label).join(" / ")}，无死链` });
  if (feat.responsive) ac.push({ id: "AC-2", text: "375px 视口下无横向滚动，导航不遮挡正文，可点区域 ≥ 40px" });
  if (feat.cart) ac.push({ id: "AC-3", text: "购物车角标点击 +1 且有动效；刷新归零（仅 UI，不落库）" });
  if (feat.form) ac.push({ id: "AC-4", text: "表单必填项为空时拦截提交并给出就地提示，不刷新页面" });
  if (feat.data) ac.push({ id: "AC-5", text: "图表在 1280 / 375 两档都可读，空数据有占位而不是白屏" });
  if (feat.auth) ac.push({ id: "AC-6", text: "未登录访问受限页跳转登录，登录态过期回到登录页" });
  ac.push({ id: `AC-${ac.length + 1}`, text: "首屏无外链依赖（字体 / 图标 / CDN），断网仍可用" });
  ac.push({ id: `AC-${ac.length + 1}`, text: "交付包含 README 与一键本地预览脚本" });

  // 非目标（会上要被"裁决"掉的那条）
  const outOfScope: string[] = [];
  if (!pages.some((page) => page.key === "about")) outOfScope.push("「关于我们」页（客户未提，二期再加）");
  if (feat.payment && type !== "commerce") outOfScope.push("真实支付对接（本期只做 UI 与订单态）");
  if (!feat.auth) outOfScope.push("账号体系与登录（无写操作，先不做）");
  if (type === "static") outOfScope.push("后台内容管理（改文案直接改 HTML）");
  if (!outOfScope.length) outOfScope.push("多语言（本期只做简体中文）");

  return {
    raw,
    type,
    typeLabel,
    feat,
    pages,
    palette,
    stack,
    why,
    ac,
    outOfScope,
    title: guessTitle(text, typeLabel),
    prj: `PRJ-${new Date().toISOString().slice(0, 10).replace(/-/g, "")}-01`,
    cards: 0,
  };
}

/* ================================================================
   四、会议剧本：把解析结果写成"人话"
   ================================================================ */

export interface ScriptLine {
  readonly who: string;
  readonly text: string;
  readonly kind?: "open" | "close" | "arb";
  /** 这一轮说完就落一份产物。`startProject` 会在建好剧本之后回填它，所以不是 readonly。 */
  art?: { readonly name: string; readonly owner: string };
  wait?: number;
}

export function buildMeeting(plan: Plan): ScriptLine[] {
  const pageLabels = plan.pages.map((page) => page.label).join(" → ");
  const files = plan.pages.map((page) => page.file).concat(["style.css", "app.js"]).join("、");
  const responsiveAc = plan.ac.find((item) => item.id === "AC-2") ?? plan.ac[1];
  return [
    {
      who: "orchestrator",
      kind: "open",
      text: `收到客户原话。先不写代码，先开三个口子：① 把模糊表述翻译成<em>可验收</em>的条目；② 定页面清单与边界；③ 定技术选型。@产品 @技术 这一轮只要"能拆单"的结论。`,
    },
    {
      who: "pm",
      text: `页面我拆成 ${plan.pages.length} 页：<code>${pageLabels}</code>。每页只背一个职责，不做"什么都有"的首页。验收标准写成 ${plan.ac.length} 条（AC-1 … AC-${plan.ac.length}），客户那句"${plan.feat.responsive ? "能在手机上正常看" : "能用"}"我落成 <em>${responsiveAc ? responsiveAc.text : ""}</em> —— 不给像素的"正常看"没法验收。`,
    },
    {
      who: "cto",
      text: `选型：<code>${plan.stack}</code>。理由：${plan.why} 目录 ${files}。按依赖拆成 ${plan.cards} 张执行卡，返工与兜底各留一张待命；其中 3 张可并行起跑。`,
    },
    {
      who: "pm",
      text: `我建议把「${plan.outOfScope[0]}」也做进去 —— 完整度会强很多。`,
    },
    {
      who: "cto",
      text: `不加。每多一页就多一轮响应式返工和一次验收，客户没提，这个成本不该我们替他决定。`,
    },
    {
      who: "orchestrator",
      kind: "arb",
      text: `裁决：记进 PRD 的非目标，二期再说。本期以 AC 清单为准 —— <em>${plan.outOfScope[0]}</em>。`,
    },
    {
      who: "designer",
      text: `主色取 <code>${plan.palette.accent}</code>（${plan.palette.name}），底 <code>${plan.palette.bg}</code>。字号 4 档：12 / 14 / 20 / 32；间距全走 8 的倍数。全部落成 CSS 变量，前端只引变量、不写死色值 —— 否则改一版配色要动 5 个文件。`,
    },
    {
      who: "orchestrator",
      kind: "close",
      text: `方案锁定。派单：${plan.pages.length} 页 + 1 套令牌 + 1 条门禁链，并发上限 3。@全体 进入执行，遇到不确定就回会议室，不要自己加需求。`,
    },
  ];
}

/* ================================================================
   五、拆卡：岗位模板 + 依赖规则 → DAG
   —— 卡不是写死的清单，是"能力 × 交付物"的组合。需求特征只决定
      "哪些卡存在、谁来做、依赖谁"。
   ================================================================ */

export type TaskStatus = "wait" | "ready" | "run" | "review" | "done" | "fail";

export interface StudioTask {
  readonly id: string;
  readonly title: string;
  readonly owner: string;
  /** **推演档的固定层号**。实时档不设它 —— 让它空着，`dagGeometry()` 才会按 `depends_on` 算真层号。
   *
   *  2026-10-28 之前这里是必填，`live.ts::mapTask` 拿数组下标塞进来。后果很具体：
   *  `dagGeometry` 的 `rankOf()` 第一行就是 `if (task.rank != null) return task.rank`，
   *  于是四张卡各自成层、排成一条竖线 —— 而它们的依赖关系其实是一张扇形的图。
   *  下标是"它在数组里第几个"，不是"它在依赖树上第几层"，这两件事不是同一个东西。
   */
  readonly rank?: number;
  readonly phase: 1 | 2 | 3 | 4;
  readonly dur: number;
  readonly artifact: string;
  readonly input: string;
  readonly act: string;
  readonly out: string;
  status: TaskStatus;
  progress: number;
  round: number;
  deps: string[];
  hidden: boolean;
  kind?: "rework" | "rescue";
  /** 这张卡的验收是**机器代签**的（`/tasks` 的 `auto_approved`）。
   *  DAG 节点据此画 `Auto-Approved` 徽章 —— 那是"这不是人签的字"在屏上唯一的位置。 */
  autoApproved?: boolean;
  /** 机器审查给过的建议结论（`auto_review`）。非空而 `autoApproved` 假 ⇒ "机器审过了、等人签"。 */
  autoReview?: string | null;
  /** 这张卡属于流水线哪一段（`/tasks` 的 `family`）。看板按它分行。
   *  缺席 ⇒ 当 `build`：推演档的卡没有这个字段，而它们本来就是一整条执行链。 */
  family?: TaskFamily;
  /** 后端报的**原态**（`/tasks` 的 `status` 一字不改）。推演档没有这一格。
   *
   *  `status` 把十五个态压成屏上六档，`ARTIFACT_INVALID` / `REJECTED` / `CHANGES_REQUESTED` /
   *  `ESCALATED` 全落进 `fail` —— 那一档只说"这张卡停了"，**不说为什么停**。平时够用
   *  （人看的是节点颜色），但交付清单空着的时候，人问的恰恰是后者：产物没过门禁该去补文件，
   *  审查退回该去看判据，两件事的下一步动作完全不同。
   *
   *  为什么不把六档拆细：屏上六档是**视觉契约**（`STATUS_LABEL` 与节点配色都按它），
   *  拆细会连带改一屏的配色与文案。原态只给需要它的那一处用，不惊动别的。 */
  readonly raw?: string;
  /** DAG 布局结果，由 `dagGeometry()` 回填。 */
  gr?: number;
  /** 视觉行号，由 `dagGeometry()` 回填。**与 `gr` 不是一回事**：`gr` 是依赖层号，
   *  而一行可能是一个类别、也可能是某个类别里的某一层（见 `FAMILY_ROWS`）。 */
  row?: number;
  gx?: number;
  gy?: number;
}

export const STATUS_LABEL: Readonly<Record<TaskStatus, string>> = {
  wait: "待依赖",
  ready: "就绪",
  run: "运行中",
  review: "待复审",
  done: "已完成",
  fail: "失败",
};

/** 节点顶行状态词（≤3 汉字），`stopWordOf()` 的唯一词表。
 *
 *  为什么单独一张表：顶行从 x=33 到 x=70 只有 37 个用户单位，10 单位/汉字 ⇒ **3 字是几何上限**。
 *  `STATUS_LABEL` 仍服务详情与汇总（那里没有这个宽度约束），节点只用这张表。
 *  `过期` 是正交属性（已完成 + 产物被覆盖），**不占状态词位**，所以不在表里。
 *  判据：tests/guards/test_studio_frontend.py 的 test_node_status_words_fit_three_chars。 */
export const NODE_STATUS_WORDS = {
  wait: "待依赖",
  ready: "就绪",
  run: "运行中",
  review: "待复审",
  done: "已完成",
  fail: "失败",
  stalled: "超时",
  invalid: "不合格",
  rejected: "退回",
  changes: "待改",
  escalated: "升级",
} as const;

export type NodeWordKey = keyof typeof NODE_STATUS_WORDS;
/** 节点上的形状通道：左竖条=结构阻断，右竖条+计时=时间性，点线/折角=内容失效。 */
export type NodeSignal = "block" | "timeout" | "stale";

export interface StopReading {
  /** 顶行状态词，≤3 汉字。 */
  readonly word: string;
  /** 第二行动宾，≤12 汉字（几何预算 184 用户单位）。 */
  readonly action: string;
  /** 形状通道；null = 不加通道。 */
  readonly signal: NodeSignal | null;
}

/** 第二行的静态动宾。动态的 `run`（进度）与 `done`（产物名）在函数里覆写。 */
const WORD_ACTION: Readonly<Record<NodeWordKey, string>> = {
  wait: "等前置就绪",
  ready: "就绪待起跑",
  run: "执行中",
  review: "等审查结论",
  done: "产物已交付",
  fail: "失败待接管",
  stalled: "到点没产出 · 重跑",
  invalid: "补文件 · 或重跑",
  rejected: "看判据 · 或换人",
  changes: "按意见改一次",
  escalated: "等人裁决",
};

/** 后端 15 态里只把这四个拆成动作词；其余仍按六档显示。 */
const RAW_WORD_KEY: Readonly<Record<string, NodeWordKey>> = {
  ARTIFACT_INVALID: "invalid",
  REJECTED: "rejected",
  CHANGES_REQUESTED: "changes",
  ESCALATED: "escalated",
};

/** 六档 → 词表的键。放在 `stopWordOf` 之前：读代码的人先看到映射，再看到分叉。 */
const STATUS_WORD_KEY: Readonly<Record<TaskStatus, NodeWordKey>> = {
  wait: "wait",
  ready: "ready",
  run: "run",
  review: "review",
  done: "done",
  fail: "fail",
};

/** 把"停了"翻译成"词 + 下一步 + 形状通道"。
 *
 *  三个来源的优先级是刻意的：
 *  1. **stale（正交属性）最高**：一张卡可以"已完成 + 产物被覆盖"，颜色仍按 done 给绿，
 *     第二行说覆盖；它不能占状态词位，否则绿字与"过期"互相打脸。
 *  2. **stalled 只认 run**：流水线停在"执行器不回话"时，只有正在跑的那张卡是超时；
 *     已完成的卡不该因为整单停了就变成"超时"。
 *  3. **raw 次之**：`ARTIFACT_INVALID / REJECTED / CHANGES_REQUESTED / ESCALATED` 是后端原态，
 *     比六档准；raw 缺席（推演档、旧接口）才退回六档。
 */
export function stopWordOf(task: StudioTask, flags: { stalled?: boolean; stale?: string | null } = {}): StopReading {
  if (flags.stale) {
    return { word: NODE_STATUS_WORDS[STATUS_WORD_KEY[task.status]], action: flags.stale, signal: "stale" };
  }
  if (flags.stalled && task.status === "run") {
    return { word: NODE_STATUS_WORDS.stalled, action: WORD_ACTION.stalled, signal: "timeout" };
  }
  const key = task.raw ? RAW_WORD_KEY[task.raw] : undefined;
  if (key) {
    return {
      word: NODE_STATUS_WORDS[key],
      action: WORD_ACTION[key],
      signal: key === "invalid" || key === "rejected" ? "block" : null,
    };
  }
  const status = task.status;
  const word = NODE_STATUS_WORDS[STATUS_WORD_KEY[status]];
  if (status === "run") {
    return { word, action: task.progress > 0 ? `${Math.round(task.progress * 100)}% · 执行中` : "执行中", signal: null };
  }
  if (status === "done") {
    return { word, action: clip(task.artifact || "产物已交付", 16), signal: null };
  }
  return { word, action: WORD_ACTION[STATUS_WORD_KEY[status]], signal: null };
}

/** 让交付清单空着的两种卡态。owner 2026-10-29 的裁决点名了这两个，**只认这两个**。
 *
 *  为什么不是"所有 `fail`"：`ESCALATED` 与 `CHANGES_REQUESTED` 的下一步在治理台
 *  （要人改判据 / 换人），它们不挡交付包这条路 —— 把四档一起列出来，交付清单会变成
 *  "什么问题都往里塞"的一锅，反而没人看。这一格只回答一个问题：
 *  **为什么这一单的文件出不来。**
 */
export const BLOCKING_STATES = ["ARTIFACT_INVALID", "REJECTED"] as const;
export type BlockingState = (typeof BLOCKING_STATES)[number];

/** 这张卡有没有卡在"出不了交付物"那一格上；没有就返回 null。 */
export function blockingOf(task: StudioTask): BlockingState | null {
  return task.raw === "ARTIFACT_INVALID" || task.raw === "REJECTED" ? task.raw : null;
}

export function buildTasks(plan: Plan): StudioTask[] {
  const tasks: StudioTask[] = [];
  const add = (spec: Partial<StudioTask> & Pick<StudioTask, "id" | "title" | "owner" | "rank" | "phase" | "dur" | "artifact" | "input" | "act" | "out">): void => {
    tasks.push({ status: "wait", progress: 0, round: 1, deps: [], hidden: false, ...spec });
  };
  const pagesTxt = plan.pages.map((page) => page.file).join(" / ");

  add({
    id: "K1", title: "写 PRD 与验收标准", owner: "pm", rank: 0, phase: 1, dur: 2600, artifact: "prd.md",
    input: "客户原话（自然语言）",
    act: `把"${plan.feat.responsive ? "能在手机上正常看" : "能用"}"翻译成可量化的 AC；定页面清单与边界`,
    out: `${plan.pages.length} 页结构 + ${plan.ac.length} 条验收标准 + 非目标清单`,
  });
  add({
    id: "K2", title: "技术选型与目录结构", owner: "cto", rank: 1, phase: 1, dur: 2400, deps: ["K1"], artifact: "architecture.md",
    input: "prd.md", act: "定栈、定文件树、按依赖拆执行卡",
    out: `${plan.stack}；${plan.pages.length + 2} 个文件；派单 ${plan.pages.length > 0 ? "13" : "12"} 张`,
  });
  add({
    id: "K3", title: "设计令牌与版式", owner: "designer", rank: 1, phase: 1, dur: 2000, deps: ["K1"], artifact: "tokens.json",
    input: "PRD 页面结构", act: "定主色、字号阶梯、间距节奏，全部落 CSS 变量",
    out: `${plan.palette.name}；4 档字号；8px 栅格`,
  });
  add({
    id: "K4", title: "页面骨架与语义结构", owner: "fe", rank: 2, phase: 2, dur: 3600, deps: ["K2", "K3"], artifact: pagesTxt,
    input: "architecture.md + tokens.json", act: "按文件树写 HTML 骨架，先结构后样式，不写内联色值", out: pagesTxt,
  });
  add({
    id: "K5", title: "响应式样式与媒体查询", owner: "fe", rank: 3, phase: 2, dur: 4200, deps: ["K4"], artifact: "style.css",
    input: "页面骨架 + tokens.json", act: "用 CSS 变量拼版式，720px / 375px 两档断点", out: "style.css（含 2 个断点）",
  });

  const hasInteract = plan.feat.cart || plan.feat.form;
  if (hasInteract) {
    add({
      id: "K6", title: plan.feat.cart ? "交互脚本（购物车 +1）" : "表单校验与交互", owner: "fe", rank: 3, phase: 2, dur: 3200, deps: ["K4"], artifact: "app.js",
      input: "骨架里的交互位",
      act: plan.feat.cart ? "原生 JS 做角标 +1 与 pop 动效，不引库" : "原生 JS 做必填校验与就地提示",
      out: "app.js",
    });
  }

  const needsApi = plan.type === "saas" || plan.type === "miniapp" || plan.feat.api || plan.feat.auth;
  if (needsApi) {
    add({
      id: "K7", title: "接口与数据模型", owner: "be", rank: 3, phase: 2, dur: 3600, deps: ["K2"], artifact: "api.md",
      input: "architecture.md", act: "按页面反推接口：列表 / 详情 / 写入；先定 schema 再写实现", out: "api.md + 数据模型",
    });
    add({
      id: "K7B", title: "鉴权与权限边界", owner: "be", rank: 4, phase: 2, dur: 3000, deps: ["K7"], artifact: "auth.md",
      input: "api.md + 角色清单", act: "登录态、越权拦截、只读账号隔离", out: "auth.md",
    });
  } else {
    add({
      id: "K7", title: "本地预览服务器脚本", owner: "be", rank: 3, phase: 2, dur: 1800, deps: ["K2"], artifact: "serve.py",
      input: "architecture.md（静态站点，无接口）",
      act: "轻量参与：写 20 行 http.server 脚本，绑定 127.0.0.1", out: "serve.py",
    });
  }

  const interactDeps = hasInteract ? ["K6"] : [];
  add({
    id: "K8", title: "README 与使用手册", owner: "writer", rank: 4, phase: 2, dur: 2600, deps: ["K5", ...interactDeps], artifact: "README.md",
    input: "最终代码 + 架构文档", act: "提取页面文案与目录结构，生成上手说明", out: "README.md",
  });
  add({
    id: "K9", title: "E2E 回归（含 375px）", owner: "qa", rank: 4, phase: 3, dur: 3400, deps: ["K5", ...interactDeps], artifact: "e2e-report.md",
    input: "全部前端产物", act: "跑三页跳转、点击、两档视口截图比对", out: "e2e-report.md",
  });
  add({
    id: "K10", title: "安全扫描（密钥 / XSS / 越权）", owner: "sec", rank: 4, phase: 3, dur: 2200,
    deps: ["K5", ...interactDeps, ...(needsApi ? ["K7B"] : [])], artifact: "security-report.md",
    input: "app.js + 表单页", act: "扫硬编码密钥、外链、表单输出转义、依赖来源", out: "security-report.md",
  });
  add({
    id: "K11", title: "门禁比对（逐条 AC）", owner: "reviewer", rank: 5, phase: 3, dur: 2600, deps: ["K9", "K10"], artifact: "gate-report.md",
    input: "代码 + PRD 需求单", act: "不看做得漂不漂亮，只逐条比对 AC 是否成立", out: "gate-report.md · PASS/FAIL",
  });
  add({
    id: "K12", title: "验收（老板视角）", owner: "acceptor", rank: 6, phase: 3, dur: 2400, deps: ["K11"], artifact: "验收签字",
    input: "门禁通过的交付包", act: "真的点一遍：购物车能不能 +1、手机宽度乱不乱", out: "签字 PASS",
  });
  add({
    id: "K13", title: "打包与交付", owner: "orchestrator", rank: 7, phase: 4, dur: 1600, deps: ["K12"], artifact: "交付包.zip",
    input: "全部产物 + 签字", act: "打 zip、写清单、挂下载", out: "交付包（可下载 / 可预览）",
  });

  // 返工卡与兜底卡：先藏在图里，触发时才亮
  add({
    id: "K5R", title: "返工：修 375px 遮挡", owner: "fe", rank: 3, phase: 3, dur: 2800, deps: ["K5"], artifact: "style.css (v2)",
    input: "QA 的失败用例（截图 + 视口）", act: "导航改可折叠，正文加安全边距，重跑 375px",
    out: "style.css v2", hidden: true, kind: "rework",
  });
  add({
    id: "F1", title: "兜底接管：重写 app.js", owner: "rescue", rank: 3, phase: 2, dur: 3600, deps: [], artifact: "app.js",
    input: "报错日志 + 上一版产物", act: "读日志定位，直接接管并把产物补齐",
    out: "app.js（接管版）", hidden: true, kind: "rescue",
  });

  plan.cards = tasks.filter((task) => !task.hidden && task.phase >= 2).length;
  return tasks;
}

/* ================================================================
   六、代码生成：交付物不是"示意"，是真能跑的静态站
   ================================================================ */

interface GeneratedFile {
  readonly name: string;
  readonly content: string;
}

const ITEMS_LIB: Readonly<Record<string, readonly string[]>> = {
  咖啡: ["耶加雪菲 · 日晒", "哥伦比亚 · 水洗", "曼特宁 · 湿刨", "云南 · 蜜处理", "肯尼亚 AA", "巴拿马 · 瑰夏"],
  健身: ["月卡 · 不限次", "私教 10 节", "体测 + 训练方案", "团课 · 搏击", "季卡 · 含储物柜", "年卡 · 双人"],
  医疗: ["初诊评估", "洁牙护理", "复诊随访", "影像检查", "康复理疗", "家庭医生"],
  金融: ["稳健组合", "指数增强", "现金管理", "养老目标", "海外配置", "定投计划"],
  教育: ["入门班", "进阶班", "一对一辅导", "寒暑假集训", "作品集班", "企业内训"],
  时尚: ["当季新款", "经典基础款", "联名系列", "配饰", "礼盒", "定制"],
  律师: ["合同审查", "劳动仲裁", "股权设计", "知识产权", "婚姻家事", "常年顾问"],
};

function itemsOf(plan: Plan): readonly string[] {
  const hit = PALETTES.find((entry) => entry.name === plan.palette.name);
  const key = hit ? hit.keys.find((candidate) => plan.raw.includes(candidate)) : undefined;
  return (key ? ITEMS_LIB[key] : undefined) ?? ["标准方案", "进阶方案", "尊享方案", "增值服务", "年度服务", "定制服务"];
}

function genCss(plan: Plan): string {
  const c = plan.palette;
  return `:root{
  --bg:${c.bg}; --surface:${c.surface}; --ink:${c.ink}; --muted:${c.muted};
  --accent:${c.accent}; --accent2:${c.accent2}; --line:${c.line};
  --r:12px; --max:1080px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.75;font-size:15px;overflow-x:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
.wrap{max-width:var(--max);margin:0 auto;padding:0 24px}
header{position:sticky;top:0;z-index:20;background:var(--surface);border-bottom:1px solid var(--line)}
.nav{display:flex;align-items:center;gap:20px;height:68px}
.logo{font-size:16px;font-weight:700;letter-spacing:.14em;white-space:nowrap}
.logo span{color:var(--accent2)}
.nav nav{display:flex;gap:22px;margin-left:auto;font-size:14px}
.nav nav a{color:var(--muted);padding:6px 0;border-bottom:2px solid transparent;transition:.2s}
.nav nav a:hover{color:var(--ink)}
.nav nav a.on{color:var(--ink);border-bottom-color:var(--accent)}
.cart{position:relative;display:flex;align-items:center;gap:7px;border:1px solid var(--line);
  background:var(--surface);border-radius:999px;padding:7px 13px;font-size:13px;cursor:pointer;
  color:var(--ink);font-family:inherit;transition:.2s}
.cart:hover{border-color:var(--accent)}
.badge{min-width:19px;height:19px;border-radius:10px;background:var(--accent);color:#fff;font-size:11px;
  display:grid;place-items:center;padding:0 4px;font-variant-numeric:tabular-nums}
.badge.pop{animation:pop .42s cubic-bezier(.3,1.6,.5,1)}
@keyframes pop{0%{transform:scale(1)}42%{transform:scale(1.55)}100%{transform:scale(1)}}
.burger{display:none;margin-left:auto;border:1px solid var(--line);background:var(--surface);
  border-radius:8px;width:40px;height:38px;align-items:center;justify-content:center;cursor:pointer}
.hero{padding:76px 0 60px;display:grid;grid-template-columns:1.1fr .9fr;gap:48px;align-items:center}
.hero h1{margin:0 0 16px;font-size:42px;line-height:1.22;letter-spacing:-.02em}
.hero h1 em{font-style:normal;color:var(--accent)}
.hero p{margin:0 0 24px;color:var(--muted);font-size:16px;max-width:34em}
.hero .cta{display:flex;gap:12px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 22px;border-radius:999px;
  font-size:14px;font-weight:600;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
.art{aspect-ratio:1/1;border-radius:20px;border:1px solid var(--line);display:grid;place-items:center;overflow:hidden;
  background:radial-gradient(120% 90% at 20% 15%,color-mix(in srgb,var(--accent2) 55%,transparent),transparent 60%),
  radial-gradient(100% 80% at 80% 85%,color-mix(in srgb,var(--accent) 70%,transparent),transparent 62%),var(--surface)}
.art b{font-size:13px;letter-spacing:.32em;color:color-mix(in srgb,var(--ink) 55%,transparent);text-transform:uppercase}
section{padding:56px 0}
.sec-h{display:flex;align-items:baseline;gap:12px;margin-bottom:26px}
.sec-h h2{margin:0;font-size:26px;letter-spacing:-.01em}
.sec-h span{font-size:13px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.card{border:1px solid var(--line);border-radius:var(--r);background:var(--surface);overflow:hidden;transition:.22s}
.card:hover{transform:translateY(-3px);box-shadow:0 18px 36px -26px rgba(0,0,0,.45)}
.thumb{aspect-ratio:4/3;background:radial-gradient(90% 90% at 25% 20%,color-mix(in srgb,var(--accent2) 40%,transparent),transparent 62%),
  radial-gradient(80% 80% at 78% 82%,color-mix(in srgb,var(--accent) 32%,transparent),transparent 60%),color-mix(in srgb,var(--ink) 5%,var(--surface))}
.card .b{padding:14px 16px 16px}
.card h3{margin:0 0 4px;font-size:16px}
.card .m{font-size:13px;color:var(--muted)}
.card .p{margin-top:9px;font-size:14px;font-weight:700;color:var(--accent)}
.contact{display:grid;grid-template-columns:1fr 1fr;gap:40px;align-items:start}
form{display:grid;gap:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}
input,textarea{width:100%;border:1px solid var(--line);background:var(--surface);color:var(--ink);
  border-radius:10px;padding:11px 13px;font-size:15px;font-family:inherit;outline:none;transition:.18s}
input:focus,textarea:focus{border-color:var(--accent)}
textarea{min-height:120px;resize:vertical}
.err{color:#C0392B;font-size:12px;margin-top:4px;min-height:16px}
.note{border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;background:var(--surface);font-size:14px;color:var(--muted)}
footer{border-top:1px solid var(--line);padding:26px 0;font-size:13px;color:var(--muted)}
footer .wrap{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
@media (max-width:720px){
  .wrap{padding:0 18px}
  .nav{height:60px;gap:10px}
  .nav nav{display:none;position:absolute;left:0;right:0;top:60px;background:var(--surface);
    border-bottom:1px solid var(--line);flex-direction:column;gap:0;padding:6px 18px 12px}
  .nav nav.open{display:flex}
  .nav nav a{padding:11px 0;border-bottom:1px solid var(--line);font-size:15px}
  .nav nav a.on{border-bottom-color:var(--line);color:var(--accent)}
  .burger{display:flex}
  .cart{margin-left:auto;padding:7px 10px}
  .cart .t{display:none}
  .hero{grid-template-columns:1fr;gap:26px;padding:34px 0 30px}
  .hero h1{font-size:29px;line-height:1.3}
  .art{aspect-ratio:16/10;border-radius:14px}
  section{padding:34px 0}
  .sec-h{flex-direction:column;gap:2px;margin-bottom:18px}
  .sec-h h2{font-size:21px}
  .grid{grid-template-columns:1fr;gap:14px}
  .contact{grid-template-columns:1fr;gap:24px}
  .btn{width:100%}
}
`;
}

function genAppJs(): string {
  return `(function(){
  "use strict";
  var burger = document.getElementById("burger");
  var nav = document.getElementById("nav");
  if (burger && nav) burger.addEventListener("click", function(){ nav.classList.toggle("open"); });

  var badge = document.getElementById("cartBadge");
  var cart = document.getElementById("cartBtn");
  if (cart && badge) {
    cart.addEventListener("click", function(){
      badge.textContent = String(parseInt(badge.textContent || "0", 10) + 1);
      badge.classList.remove("pop");
      void badge.offsetWidth;
      badge.classList.add("pop");
    });
  }

  var form = document.getElementById("contactForm");
  if (form) {
    form.addEventListener("submit", function(e){
      e.preventDefault();
      var rules = [["name","请填写你的称呼"],["email","请填写邮箱"],["message","请写下你想问的"]];
      var ok = true;
      rules.forEach(function(r){
        var el = form.querySelector('[name="' + r[0] + '"]');
        var box = form.querySelector('[data-err="' + r[0] + '"]');
        if (!el) return;
        var v = (el.value || "").trim();
        var bad = !v || (r[0] === "email" && !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(v));
        if (box) box.textContent = bad ? r[1] : "";
        if (bad) { ok = false; el.style.borderColor = "#C0392B"; }
        else { el.style.borderColor = ""; }
      });
      if (ok) {
        form.reset();
        var tip = document.getElementById("formOk");
        if (tip) { tip.textContent = "已收到，我们会在 1 个工作日内回复。"; }
      }
    });
  }
})();
`;
}

function genPage(plan: Plan, page: PageDef): string {
  const nav = plan.pages.map((item) => `        <a href="${item.file}"${item.key === page.key ? ' class="on"' : ""}>${item.label}</a>`).join("\n");
  const cart = plan.feat.cart
    ? `      <button class="cart" id="cartBtn" aria-label="购物车">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 4h2l2.4 11.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.5L21 8H6"/><circle cx="10" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/></svg>
        <span class="t">购物车</span><span class="badge" id="cartBadge">0</span>
      </button>`
    : "";
  const head = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(plan.title)} · ${esc(page.label)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
  <div class="wrap">
    <div class="nav">
      <a class="logo" href="index.html">${esc(plan.title)}<span>.</span></a>
      <button class="burger" id="burger" aria-label="菜单">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
      </button>
      <nav id="nav">
${nav}
      </nav>
${cart}
    </div>
  </div>
</header>
<main>`;
  const foot = `</main>
<footer>
  <div class="wrap">
    <span>© ${new Date().getFullYear()} ${esc(plan.title)}</span>
    <span>·</span>
    <span>示例交付物 · 内容可在源码中直接修改</span>
  </div>
</footer>
<script src="app.js"></script>
</body>
</html>
`;

  const items = itemsOf(plan);
  let body = "";
  if (page.key === "home") {
    body = `
  <section><div class="wrap"><div class="hero">
    <div>
      <h1>把每一件<em>${esc(plan.title)}</em><br>做到刚刚好</h1>
      <p>${esc(plan.title)} 只做一件事：把当季最好的东西，用最稳定的手法递到你手里。下单前可以先看这一季的清单。</p>
      <div class="cta">
        <a class="btn" href="products.html">看看这一季</a>
        <a class="btn ghost" href="contact.html">联系我们</a>
      </div>
    </div>
    <div class="art"><b>${esc(plan.palette.name)}</b></div>
  </div></div></section>
  <section>
    <div class="wrap">
      <div class="sec-h"><h2>本季清单</h2><span>每周三更新，48 小时内发出</span></div>
      <div class="grid">
${items.slice(0, 3).map((item, i) => `        <article class="card"><div class="thumb"></div><div class="b"><h3>${esc(item)}</h3><div class="m">${["浅烘 · 花香与柑橘", "中烘 · 坚果与焦糖", "深烘 · 可可与香料"][i] ?? "当季精选"}</div><div class="p">¥ ${[88, 78, 68][i] ?? 78} / 200g</div></div></article>`).join("\n")}
      </div>
    </div>
  </section>`;
  } else if (page.key === "products") {
    body = `
  <section>
    <div class="wrap">
      <div class="sec-h"><h2>${esc(page.label)}</h2><span>共 ${items.length} 款 · 库存每周更新</span></div>
      <div class="grid">
${items.map((item, i) => `        <article class="card"><div class="thumb"></div><div class="b"><h3>${esc(item)}</h3><div class="m">${["花香 · 柑橘 · 明亮", "坚果 · 焦糖 · 均衡", "可可 · 香料 · 醇厚"][i % 3]}</div><div class="p">¥ ${[88, 78, 68, 98, 118, 138][i] ?? 88} / 200g</div></div></article>`).join("\n")}
      </div>
    </div>
  </section>`;
  } else if (page.key === "contact") {
    body = `
  <section>
    <div class="wrap">
      <div class="sec-h"><h2>${esc(page.label)}</h2><span>工作日 10:00 – 19:00 都有人在</span></div>
      <div class="contact">
        <form id="contactForm" novalidate>
          <div><label for="name">怎么称呼你</label><input id="name" name="name" placeholder="例如：陈先生"><div class="err" data-err="name"></div></div>
          <div><label for="email">邮箱</label><input id="email" name="email" placeholder="you@example.com"><div class="err" data-err="email"></div></div>
          <div><label for="message">想聊点什么</label><textarea id="message" name="message" placeholder="批发、课程、合作都可以"></textarea><div class="err" data-err="message"></div></div>
          <button class="btn" type="submit">发送</button>
          <div class="err" id="formOk" style="color:var(--accent)"></div>
        </form>
        <div>
          <div class="note">
            <b>门店</b><br>${esc(plan.title)} · 工坊<br>
            周三至周日 10:00 – 19:00<br><br>
            <b>联系</b><br>hello@example.com<br>021-0000 0000
          </div>
        </div>
      </div>
    </div>
  </section>`;
  } else {
    body = `
  <section>
    <div class="wrap">
      <div class="sec-h"><h2>${esc(page.label)}</h2><span>由「${esc(plan.title)}」项目模板生成</span></div>
      <div class="note">${esc(page.label)} 的内容区。交付后可在源码里直接替换文案与图片，不需要构建。</div>
    </div>
  </section>`;
  }
  return head + body + foot;
}

function genServe(): string {
  return `"""本地预览：python serve.py  →  http://127.0.0.1:8000"""
import http.server, socketserver, os

PORT = 8000
os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

if __name__ == "__main__":
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"serving at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
        httpd.serve_forever()
`;
}

function genReadme(plan: Plan): string {
  return `# ${plan.title}

${plan.typeLabel}，由 AgentDeck「项目工作室」从一句需求生成。

## 页面

${plan.pages.map((page) => `- \`${page.file}\` — ${page.label}`).join("\n")}

## 本地预览

\`\`\`bash
python serve.py
# 打开 http://127.0.0.1:8000
\`\`\`

也可以直接双击 \`index.html\`（全部为相对路径，无外链依赖）。

## 验收标准（PRD 摘录）

${plan.ac.map((item) => `- ${item.id} ${item.text}`).join("\n")}

## 本期不做

${plan.outOfScope.map((item) => `- ${item}`).join("\n")}

## 目录

\`\`\`
${plan.pages.map((page) => page.file).join("\n")}
style.css      设计令牌 + 版式 + 两档断点
app.js         购物车角标 / 表单校验 / 移动端菜单
serve.py       本地预览脚本
\`\`\`
`;
}

export interface CodegenResult {
  readonly runnable: boolean;
  readonly files: readonly GeneratedFile[];
}

export function codegen(plan: Plan): CodegenResult {
  if (plan.type !== "static" && plan.type !== "commerce") {
    return {
      runnable: false,
      files: [
        { name: "README.md", content: genReadme(plan) },
        { name: "architecture.md", content: `# ${plan.title} · 架构\n\n选型：${plan.stack}\n\n理由：${plan.why}\n` },
      ],
    };
  }
  const files: GeneratedFile[] = [];
  for (const page of plan.pages) files.push({ name: page.file, content: genPage(plan, page) });
  files.push({ name: "style.css", content: genCss(plan) });
  files.push({ name: "app.js", content: genAppJs() });
  files.push({ name: "serve.py", content: genServe() });
  files.push({ name: "README.md", content: genReadme(plan) });
  return { runnable: true, files };
}

/** 把多文件站内联成单页，供 iframe / 新窗口预览。 */
export function inlineDoc(plan: Plan): string | null {
  const result = codegen(plan);
  if (!result.runnable) return null;
  const index = result.files.find((file) => file.name === "index.html");
  const css = result.files.find((file) => file.name === "style.css");
  const js = result.files.find((file) => file.name === "app.js");
  if (!index || !css || !js) return null;
  return index.content
    .replace('<link rel="stylesheet" href="style.css">', `<style>\n${css.content}\n</style>`)
    .replace('<script src="app.js"></script>', `<script>\n${js.content}\n</script>`);
}

/* ---- 极简 zip（store，无压缩）：交付包真的能解压 ---- */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export function makeZip(files: readonly GeneratedFile[]): Blob {
  const encoder = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const file of files) {
    const data = encoder.encode(file.content);
    const name = encoder.encode(file.name);
    const crc = crc32(data);
    const local = new Uint8Array(30 + name.length);
    const dv = new DataView(local.buffer);
    dv.setUint32(0, 0x04034b50, true);
    dv.setUint16(4, 20, true);
    dv.setUint16(6, 0x0800, true);
    dv.setUint32(14, crc, true);
    dv.setUint32(18, data.length, true);
    dv.setUint32(22, data.length, true);
    dv.setUint16(26, name.length, true);
    local.set(name, 30);
    parts.push(local, data);
    const header = new Uint8Array(46 + name.length);
    const cv = new DataView(header.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(8, 0x0800, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(42, offset, true);
    header.set(name, 46);
    central.push(header);
    offset += local.length + data.length;
  }
  let centralSize = 0;
  for (const chunk of central) centralSize += chunk.length;
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, central.length, true);
  ev.setUint16(10, central.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end] as BlobPart[], { type: "application/zip" });
}

/* ================================================================
   七、状态机
   ================================================================ */

export type StudioScreen = "studio" | "board";
export type SideTab = "detail" | "feed" | "review" | "bus" | "usage" | "deliver";
export type OutputTab = "prd" | "arch" | "token" | "cards";

export interface Turn {
  readonly who: string;
  readonly text: string;
  readonly kind?: "open" | "close" | "arb";
  readonly t: string;
  /** **未转义**的原文。只有 live 档有（推演档的 `text` 里本来就带 `<b>` / `<code>` 标记）。
   *
   *  为什么必须单独存一份：live 档的 `text` 已经过了 `esc()`，是给 `innerHTML` 用的；
   *  而打字机要按**字符**切片 —— 在 `&amp;` 这种实体中间下刀会切出 `&am`，
   *  于是屏幕上闪过一个裸的 `&am`。切原文、每帧再 `esc()`，切片永远落在字符边界上。
   */
  readonly raw?: string;
}

export interface ArtifactRecord {
  readonly name: string;
  readonly owner: string;
  readonly kind: string;
  readonly lines: number;
  readonly at: string;
  /** 真链路里产物的**完整相对路径**（`qoder-workspace/about.html`）。推演档没有这一格。
   *
   *  为什么要它：`name` 是给人看的文件名（末段），而不同目录下可能有同名文件
   *  （`a/index.html` / `b/index.html`）—— React key 与悬停提示都得靠整条路径才分得开。 */
  readonly path?: string;
  /** 产出它的那张卡（`task_card_id`）。**任务详情按这一格筛，不按 `owner` 筛**。
   *
   *  owner 是"谁写的"，而一个执行者会连着写好几张卡（真链路里 T01–T06 全是 `qoder`）。
   *  按 owner 筛的后果是双向的错：卡详情要么把别人的产物算进来（T02 显示 T03 的页面），
   *  要么一份都不显示 —— owner 那一格对不上（卡上写 `qoder`、产物写模型名）时就是后者，
   *  屏上表现为「任务完成没有产物」（owner 2026-10-29 抓到的就是这个）。
   *
   *  推演档没有真卡号，留空；读取侧按"有卡号按卡号、没卡号退回 owner"处理。 */
  readonly card?: string;
  /** 这一份**过没过校验**（`/evidence` 的 `state`：`valid` / `invalid` / `pending`）。
   *
   *  为什么原样透出：`invalid` 与 `pending` 都不是"已验证"，但它们在屏上是两件事 ——
   *  一份**写进来了却没达标**的文件（`invalid`，多半是字节数/行数没过门槛）与一份
   *  "校验节点还没跑"的文件（`pending`），对人说的下一句话完全不同。推演档没有这一格。 */
  readonly state?: string;
}

export interface StudioEventRecord {
  readonly t: string;
  readonly a: string;
  readonly text: string;
  readonly kind: "info" | "ok" | "warn" | "danger" | "sys" | "art";
  readonly file: string | null;
}

export interface ReviewRecord {
  readonly k: "good" | "bad" | "gate";
  readonly who: string;
  readonly title: string;
  readonly t: string;
  readonly body: string;
  /** 这条结论属于**哪张卡**（`task_card_id`）。
   *
   *  `title` 里其实也写着卡号，但那是给人看的排版串 —— 拿它 `startsWith` 去反解，
   *  以后谁改一次标题格式就会静默失配（不报错，只是再也对不上）。交付清单要靠这一格
   *  说清"是哪张卡的判据没过"，所以它得是字段，不是字符串里的第 0 段。 */
  readonly card?: string;
  /** 没通过的判据原文（`item_findings` 里 `met` 为假的那几条）。
   *
   *  `body` 里也有它们（带 ✓/✕ 与证据），但那是要交给 `dangerouslySetInnerHTML` 的 HTML；
   *  往屏上拼一句话时不能拿它去剥标签。 */
  readonly failed?: readonly string[];
}

export interface GateItem {
  readonly id: string;
  readonly text: string;
  on: boolean;
}

export interface Deliverable {
  readonly gen: CodegenResult;
  readonly html: string | null;
  readonly version: number;
}

interface MeetingState {
  readonly script: readonly ScriptLine[];
  i: number;
  t: number;
  done: boolean;
}

export interface SeatView {
  readonly s: "work" | "think" | "review" | "take" | "done" | "idle" | "sleep";
  readonly note: string;
}

export interface StudioState {
  screen: StudioScreen;
  plan: Plan | null;
  /** P2-10：DAG 布局的**最近一次**结果。render 只读它，不再现算（现算会写任务对象）。 */
  dag: DagLayout;
  meeting: MeetingState | null;
  turns: Turn[];
  tasks: StudioTask[];
  artifacts: ArtifactRecord[];
  events: StudioEventRecord[];
  reviews: ReviewRecord[];
  gate: GateItem[];
  override: Record<string, SeatView>;
  phase: number;
  tab: SideTab;
  otab: OutputTab;
  selected: string | null;
  reworked: boolean;
  failUsed: boolean;
  deliver: Deliverable | null;
  dev: "phone" | "desk";
  run: boolean;
  conc: number;
  speed: number;
  paused: boolean;
  /** 演示用：下一张交互卡完成时判执行器超时，走兜底换人。
   *  顶栏的「注入故障」键与 `?fail=1` 直链都已退役；旗子保留给本地验证/测试，
   *  因为「兜底接管」是这条链路的第三根柱子，删掉旗子等于把 F1 变成永远不亮的死卡。 */
  injectFail: boolean;
  /** 抽屉：null = 关着。只承载二级信息（为什么是这张卡 / 调度日志 / 工位产出）。 */
  drawer: { title: string; html: string } | null;
  toast: { id: number; title: string; body: string; kind: string } | null;
  /** 右栏「有新东西」的角标。**不是**"自动翻到那一页"的开关。
   *
   *  2026-10-27 之前这里是反过来的：审查落地就把 `tab` 改成 `review`，交付就把 `tab` 改成 `deliver`
   *  （只在 `!selected` 时，看起来像留了余地）。但 `selected` 只在**点过 DAG 节点**之后才非空 ——
   *  也就是说只要用户没点过节点，页签就一定被抢。实测：派单后 1.5 秒，面板自己跳到「审查」，
   *  而读者此刻可能正在读「文件总线」。控制权被夺比任何视觉问题都更伤信任。
   *
   *  改法：引擎只负责**置位**，页签只由 `setSideTab`（用户点击）与 `selectTask`（用户点节点）改。
   *  角标在进入该页签时清掉。判据 = `test_no_engine_path_steals_the_side_tab`。 */
  unread: { review: boolean; deliver: boolean };
  /** 受理台输入框里的文字。
   *
   *  它原来只是 `IntakeCard` 里一个 `useState`。改成引擎状态是因为两个新动作都要**从外面**写它：
   *  「修改需求」把当前需求原文填回去，「开启新需求」把它清空。
   *  而组件现在不再随 `plan` 重挂载（未立项与已立项共用同一棵树），本地 state 不会自己重置 ——
   *  继续留在组件里，就会出现"点了清空但框里还是旧字"。 */
  draft: string;
  /** 立项过的需求。点一条 = 用原文重新立项（不恢复旧状态），所以只存"能重跑"的最小信息。 */
  history: HistoryEntry[];
  /** 数据源。`live` = 后端真数据（2026-10-27 接线后的默认档）；`demo` = 原型推演（受理台「固定演示」或历史记录）。
   *
   *  为什么把"源"放进同一個 state 而不是另开一棵树：这一屏所有组件都读 `getStudio()`，
   *  多一棵树就意味着每个组件都要写一次"我在哪一档"的分支 —— 那会在三十处各错一次。
   *  放进 state 之后，分支只剩两个：`tick()` 是否推进、受理是否打后端。
   */
  source: "live" | "demo";
  /** 实时连接的读数。状态与错误分开存：`status="error"` 时必须能说出**为什么**，
   *  否则页面上只会出现一个红点，而红点不可排障。 */
  live: LiveState;
}

/** 三份立项文档的结构。**类型放在引擎里**是因为 `LiveState` 持有它，而 `live.ts` 已经依赖引擎；
 *  反过来让引擎依赖 `live.ts` 会成环。`live.ts` 负责解析，引擎只负责存。 */
export interface PrdDoc {
  readonly title: string;
  readonly pages: readonly { readonly label: string; readonly file: string; readonly duty: string }[];
  readonly acceptance: readonly string[];
  readonly out_of_scope: readonly string[];
}

export interface ArchDoc {
  readonly stack: string;
  readonly why: string;
  readonly risk: string;
}

export interface TokensDoc {
  readonly name: string;
  readonly bg: string;
  readonly surface: string;
  readonly ink: string;
  readonly muted: string;
  readonly accent: string;
  readonly accent2: string;
  readonly line: string;
}

export interface Docs {
  readonly prd: PrdDoc | null;
  readonly arch: ArchDoc | null;
  readonly tokens: TokensDoc | null;
}

export interface DeliverySnapshot {
  readonly entryPath: string;
  readonly manifest: readonly { relPath: string; lines: number; format: string }[];
  readonly openIssues: number;
}

/** P1-4：A 类卡点按宪法无超时，死单会永远停着 ⇒ 屏上必须有一句"它挂了多久"。 */
export interface SuspendedAlert {
  /** 超过阈值还没人处理的未决单张数。 */
  readonly count: number;
  /** 最久那一张挂了几天（向下取整，最少 1 —— 说"0 天"等于没说）。 */
  readonly oldestDays: number;
  /** 最久那一张的卡点类型（`A_SPLIT` 之类），给悬停用。 */
  readonly oldestKind: string;
}

export interface LiveState {
  status: "idle" | "connecting" | "live" | "error";
  /** 原始拒因/异常消息，不翻译。空串表示没有错误。 */
  error: string;
  projectKey: string;
  requestId: string | null;
  /** EN-17 交付包索引；null = 还没组包（或这一单没有交付包）。 */
  delivery: DeliverySnapshot | null;
  /** P1-4：挂太久的未决单。null = 没有（或还没读到）。**只读告警**，不给任何写动作。 */
  suspended: SuspendedAlert | null;
  /** 最近一次成功读到任务的时间（`HH:MM:SS`）。null = 还没读到过。 */
  updatedAt: string | null;
  /** 这批卡是怎么来的（2026-10-31，读 `/tasks` 的 `split_source`）。
   *
   *  `fallback_stub` / `stub` = 拆单走了通用清单、没接真模型 —— 这一格非空且是降级时，
   *  工作室抬头必须显眼标出来。`null` = 还没读到，或本列上线前的历史行；
   *  **历史行不许被读成 `llm`**（那些行确实没人记过来源）。 */
  splitSource: "llm" | "prd" | "fallback_stub" | "stub" | null;
  /** 收到的 SSE 帧数。它是"连接真的在推"的唯一读数，比一个绿点可信。 */
  frames: number;
  /** 受理链走到哪一段。**界面不许永远说"进行中"** —— 观察窗到期就是到期。
   *
   *  `meeting` 会议四轮（约 1–3 分钟）→ `docs` 三份文档（约 1 分钟）→ `split` 拆卡。
   *  `stalled` = 窗口用完还没拿到该拿的东西：屏上如实说"没产出"，不让一屏静止的字冒充在跑。
   *  2026-10-28 实测：一场会 2 分 48 秒，而旧的观察窗只有 120 秒。
   *  `needs_human` = R-26 卡点：拆单两次都过不了契约，单停下来**等人**（不是失败）。
   *  它与 `stalled` 分开的原因只有一个但足够：下一步该做什么不同 —— 前者去改需求，
   *  后者去查留痕；屏上一个是琥珀的"待人工干预"，一个是红的"停住了"。 */
  stage: "idle" | "meeting" | "docs" | "split" | "stalled" | "needs_human" | "failed";
  /** 文档节点产出的三份结构。null = 还没到（或这一单没有）。 */
  docs: Docs | null;
  /** 开始盯这一单受理链的时刻（ISO）。会议卡用它算"已等 mm:ss"。
   *
   *  为什么记"开始盯"而不是"会议开始"：后端不存会议起始时刻，前端也无从得知。
   *  从**我们开始等**那一刻算起，这个读数是可核对的；编一个会议开始时间就是假精度。
   *  没在盯（idle / 推演档）时为 null。 */
  meetingSince: string | null;
  /** 进入当前 `stage` 的时刻（ISO）。**"这一步等多久"用它，不是 `meetingSince`。**
   *
   *  两个计时器各答一个问题：`meetingSince` 答"这一单等了多久"，`stageSince` 答
   *  "屏幕上这一句话已经说了多久"。会议说完之后屏上换成"正在落文档"，
   *  这时候把整单的 2 分钟算进"落文档已等 2 分钟"是错的。
   *  `watchRequest` 在 stage 真的变了时才写它 —— 每轮轮询都写，计时会永远停在 0:00。 */
  stageSince: string | null;
  /** 受理失败的原因（U6，2026-10-28）。空串 = 没失败过。
   *
   *  为什么它必须单独一格：后端把失败写成需求单状态 `FAILED` 之后，屏上会从"正在拆解"变成
   *  "停住了"，但**为什么停**只有这一格说得出来。只报"停住了"而不报原因，用户能做的动作
   *  和看到"卡住了"时一模一样 —— 都得去翻日志。 */
  lastError: string;
  /** 这一单的会议开几轮（`summary_digest["会议轮数"]`，后端 `ACP_MEETING_ROUNDS`）。
   *
   *  为什么它是**读数**而不是配置：轮数是后端按环境变量定的，前端猜不到；
   *  而会议桌的分母（"说到第几条了"）与"会议说完了没有"都要用它。
   *  `0` = 还没读到（刚提交、受理任务还没起头）；此时按 `LEGACY_ROUNDS` 算，
   *  也就是老单那 4 条台词 —— 下一秒真值就到了，见 `live.roundsOf`。 */
  rounds: number;
  /** 后端**没有** `/requests/{id}/evidence` 这个读面（404/405）。
   *
   *  为什么单独一格：右栏「文件总线」「审查」空着有两种完全不同的原因 ——
   *  ① 真的还没产出；② 这一版前端读不到证据面（前端比后端新，或页面不是从这个后端打开的）。
   *  两者显示同一句"还是空的"，就是把"我读不到"说成"你没有"。
   *  owner 2026-10-29 看到的正是这一句：库里 12 条产物、屏上 0 份。
   *  只有 404/405 才置位 —— 5xx 与断网是抖动，留旧值、不下结论。 */
  evidenceStale: boolean;
}

/** 一条历史 = 一次立项的最小快照。
 *
 *  为什么不存整份 `StudioState`：那样"历史"就变成了"存档"，点开要恢复当时的任务进度、
 *  定时器、事件流 —— 那是另一个功能，而且很容易恢复出一个自相矛盾的中间态。
 *  这里存的是"当时提交了什么、跑出多大一张单"，点一条就**用原文再跑一次**。 */
export interface HistoryEntry {
  prj: string;
  raw: string;
  date: string;
  at: string;
  pages: number;
  cards: number;
}

const initialState = (): StudioState => ({
  screen: "studio",
  plan: null,
  dag: { visible: [], width: 0, height: 0, maxRank: 0 },
  meeting: null,
  turns: [],
  tasks: [],
  artifacts: [],
  events: [],
  reviews: [],
  gate: [],
  override: {},
  phase: 0,
  tab: "detail",
  otab: "prd",
  selected: null,
  reworked: false,
  failUsed: false,
  injectFail: false,
  deliver: null,
  dev: "phone",
  run: false,
  conc: 3,
  speed: 1,
  paused: false,
  drawer: null,
  toast: null,
  unread: { review: false, deliver: false },
  draft: "",
  history: [],
  source: "demo",
  live: { status: "idle", error: "", projectKey: "", requestId: null, delivery: null, suspended: null, updatedAt: null, splitSource: null, frames: 0, stage: "idle", docs: null, meetingSince: null, stageSince: null, lastError: "", rounds: 0, evidenceStale: false },
});

let state: StudioState = initialState();
/* 历史是本机偏好，不是推演状态 —— 刷新页面之后它应该还在，所以落在 localStorage。
   读失败就当空（隐私模式、被清过、格式坏了）：历史丢一条不算故障，抛异常把整屏干掉才是。 */
const HISTORY_KEY = "acp-history";
const HISTORY_MAX = 20;

function loadHistory(): HistoryEntry[] {
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is HistoryEntry => !!item && typeof item.raw === "string" && typeof item.prj === "string").slice(0, HISTORY_MAX);
  } catch {
    return [];
  }
}

function saveHistory(): void {
  try {
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
  } catch {
    /* 写不进去就只在这一次会话里有效，不打扰用户 */
  }
}

/** 记一条历史。**按需求原文去重**：同一条需求改一个标点再提交，不应该在历史里变成两条。 */
function recordHistory(entry: HistoryEntry): void {
  state.history = [entry, ...state.history.filter((item) => item.raw !== entry.raw)].slice(0, HISTORY_MAX);
  saveHistory();
}

state.history = loadHistory();
let version = 0;
const listeners = new Set<() => void>();

function emit(): void {
  version += 1;
  listeners.forEach((listener) => listener());
}

export function getStudio(): StudioState {
  return state;
}

export function subscribeStudio(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function studioVersion(): number {
  return version;
}

/* ================================================================
   数据源切换（2026-10-27 接线）
   —— 这一组是 live 适配层唯一的写入口。它不生成任何业务数据，
      只把"后端读到了什么"搬进同一棵树，让既有组件不必分叉。
   ================================================================ */

export function setSource(source: "live" | "demo"): void {
  if (state.source === source) return;
  state.source = source;
  if (source === "live") {
    /* 从 demo 切过来时把推演残留清干净：会议剧本、任务卡、留痕都是上一条假需求的，
       留着会让"未立项"看起来像"已经跑完一半"。真实数据由连接层随后灌入。 */
    state.plan = null;
    state.meeting = null;
    state.turns = [];
    state.tasks = [];
    state.dag = { visible: [], width: 0, height: 0, maxRank: 0 };
    state.artifacts = [];
    state.events = [];
    state.reviews = [];
    state.gate = [];
    state.deliver = null;
    state.phase = 0;
    state.selected = null;
    state.screen = "studio";
  }
  emit();
}

/** 把后端的**证据面**搬进右栏那两页（文件总线 / 审查）。
 *
 *  **整段替换而不是增量合并**：这两格回答的是"这一单现在有什么"，
 *  不是"发生过什么"。增量合并会让已经作废的产物、上一轮的结论一直留在清单上 ——
 *  而它们看起来与现在的完全一样（2026-10-29：这两页在真链路上一直是空的，
 *  因为引擎里只有推演档会写它们）。
 *
 *  `undefined` = "这一格没取到"（不是"取到了空"）：只有真的读到证据面时才调用，
 *  读失败时调用方**不要调它** —— 一次网络抖动把清单清空，比留旧值更骗人。
 */
export function adoptEvidence(patch: {
  artifacts?: ArtifactRecord[];
  reviews?: ReviewRecord[];
  gate?: GateItem[];
}): void {
  if (patch.artifacts) state.artifacts = patch.artifacts;
  if (patch.reviews) state.reviews = patch.reviews;
  if (patch.gate) state.gate = patch.gate;
  emit();
}

export function setLive(patch: Partial<LiveState>): void {
  const next = { ...state.live, ...patch };
  const changed = (Object.keys(next) as (keyof LiveState)[]).some((key) => next[key] !== state.live[key]);
  if (!changed) return;
  state.live = next;
  emit();
}

/** 把后端任务搬进引擎。`phase` 由调用方按状态分布算好（它属于读数，不属于单卡）。 */
export function adoptTasks(tasks: StudioTask[], phase: number, plan: Plan | null): void {
  state.tasks = tasks;
  state.phase = phase;
  if (plan) state.plan = plan;
  // P2-10：任务集合换了就地重算一次布局。以前是 render 里算（顺带写任务对象），
  // 那让"渲染"变成了写操作；现在写在这里，render 只读 `state.dag`。
  dagGeometry();
  emit();
}

/** 一条真实留痕。追加在尾部（与 `log()` 同序：数组顺序 = 时间顺序），超上限从头丢。 */
export function pushLiveEvent(record: StudioEventRecord): void {
  state.events.push(record);
  if (state.events.length > 240) state.events.shift();
  emit();
}

/** 真会议的台词搬进会议桌。
 *
 *  **整段替换而不是追加**：后端那一段是"四轮跑完一次性落库"的，每 1.5 秒读回来的都是
 *  同一段的完整快照（1 行、2 行……4 行）。按追加写，第四次读回来桌上就有 10 条重复台词。
 *
 *  时间戳给的是**读到它的时刻**，不是发言时刻 —— 后端没存每轮的时间，编一个出来
 *  比留空更糟：那是给读者的假精度。 */
export function adoptTurns(turns: Turn[]): void {
  state.turns = turns;
  emit();
}

let toastSeq = 0;

function toast(title: string, body: string, kind: string, ms = 4200): void {
  toastSeq += 1;
  state.toast = { id: toastSeq, title, body, kind };
  window.setTimeout(() => {
    if (state.toast && state.toast.id === toastSeq) {
      state.toast = null;
      emit();
    }
  }, ms);
}

function log(a: string, text: string, kind: StudioEventRecord["kind"] = "info", file: string | null = null): void {
  state.events.push({ t: nowHM(), a, text, kind, file });
  if (state.events.length > 240) state.events.shift();
}

function addArtifact(
  name: string,
  owner: string,
  kind = "file",
  lines?: number,
  card?: string,
): void {
  /* 一个任务可以同时交多个文件 —— 前端那张卡是 `index.html / products.html / contact.html`
   * 三页一起交。但文件总线是**一张文件清单**，一行一个文件：三个文件名挤在一行、
   * 后面跟一个「行数」，那个数是什么？三份之和？没有意义。
   *
   * 所以拆在记账这一层。任务卡上的 `artifact` 字段仍然是合并写法（那是"这张卡产出什么"
   * 的一句话说明，DAG 节点上正好一行放得下），总线只认文件。
   *
   * 拆分之前这三页在总线里是一行、且因为没有同名文件而**不可点** —— 交付包里最重要的
   * 三个页面是唯一打不开的三个，这是 P1-J 接线时被截图抓出来的。 */
  for (const part of name.split(" / ")) {
    state.artifacts.push({
      name: part,
      owner,
      kind,
      lines: lines ?? Math.round(40 + Math.random() * 160),
      at: nowHM(),
      ...(card ? { card } : {}),
    });
  }
}

function addReview(record: ReviewRecord): void {
  state.reviews.push(record);
  // 新结论到了：只点角标，不抢页签。正停在这一页时不点（读者已经看见了）。
  if (state.tab !== "review") state.unread.review = true;
}

function isDone(id: string): boolean {
  const task = state.tasks.find((item) => item.id === id);
  return !!task && task.status === "done";
}

/* 座位的状态全部从任务推导，不手写 —— 否则"卡完成了但灯还亮着"这种假话迟早出现 */
export function agentView(id: string): SeatView {
  const override = state.override[id];
  if (override) return override;
  /* 总管不持有执行卡，所以下面那几条按任务状态推导的路径永远点不亮它 ——
     它那几段读数（主持立项会 / 正在拍板 / 等派单确认 / 盯盘）全部由 `state.override`
     直接给，`startProject`、`stepMeeting`、`dispatch` 各写一次。
     唯一漏掉的是**交付那一刻**：`K13` 会把 `override.orchestrator` 删掉，
     于是「工作室总管」这一行会退回空白 —— 而那正好是最该看到"这单结束了"的时刻。 */
  if (id === "orchestrator") {
    return state.deliver ? { s: "done", note: "已交付" } : { s: "idle", note: "" };
  }
  const mine = state.tasks.filter((task) => task.owner === id && !task.hidden);
  const running = mine.filter((task) => task.status === "run");
  if (running.length) return { s: "work", note: running[0].title };
  const review = mine.filter((task) => task.status === "review" || task.status === "fail");
  if (review.length) return { s: "review", note: `待处理 · ${review[0].title}` };
  const done = mine.filter((task) => task.status === "done");
  if (done.length) return { s: "done", note: done[done.length - 1].artifact || "已交付" };
  if (mine.some((task) => task.status === "ready")) return { s: "think", note: "就绪待起跑" };
  // 兜底 idle 不吐岗位说明书（"PRD 与验收标准"那一行）：那是 `AgentDef.note`，由左栏**固定**
  // 显示在副行（2026-10-28 owner 要的"专家职级 + 核心职责"双行），不是状态。
  // 这里的 `note` 只表示**读数**（`prd.md` / `返工：修 375px 遮挡` / `等 PRD`），
  // 空闲时为空 —— 它现在去左栏那枚 `title` 提示里，不再挤占副行。
  // 判据 = `test_the_seat_shows_a_rank_and_a_duty_on_two_fixed_lines`。
  return { s: "idle", note: "" };
}

/* ================================================================
   八、DAG 布局
   ================================================================ */

export const NODE_W = 200;
export const NODE_H = 64;
/** 卡片倒角（owner 2026-10-28 第 5 条「适当调整每个卡片的倒角」）。
 *
 *  10 而不是更大：节点是 200×64 的横条，圆角半径超过 12 就开始像一颗胶囊，
 *  四张卡并排时"卡"的形状反而读不出来。10 与右栏 `.th-card` 的 10 同档。 */
export const NODE_RADIUS = 10;
/** 卡与卡之间的间距。2026-10-28 从 16/26 加到 22/34 —— 同一条 5：
 *  原来横向只隔 16px，而连线从右侧出来、在 22px 的缝里还要拐两次弯，
 *  四条边挤在一起像一团；纵向 26px 也放不下"从上一行底边到下一行顶边"的箭头。
 *  代价是整图变宽变高：三列时 684×?，`dag-wrap` 本来就能滚。 */
const GAP_X = 22;
const GAP_Y = 34;

/** 一张卡属于流水线哪一段（后端 `reporting/cards.py::family_of` 算好的事实，前端不重算）。
 *  执行卡带能力标签（`frontend_coding` 之类），它们一个都不在这四个词里 ⇒ 落到 `build`。 */
export type TaskFamily = "doc" | "build" | "gate" | "summary";

/** 行序：**类别优先、层号次之**（owner 2026-10-28：「不同任务的卡片竖向排列，同类任务横向排列」）。
 *
 *  为什么要定这个序而不是按层号直接排：真链路里文档卡（PRD / 架构 / 令牌）与执行卡的样式表卡
 *  **依赖层都是 0**，按层号排会把 T01 和 T90/T91/T92 挤进同一行 —— 一张样式表和一份 PRD 并排，
 *  读起来像同一批活。现场那张截图（`47d8d998`）就是这个形状。
 *  类别先行之后：文档一行、执行卡按依赖逐层向下、门禁一行、汇总一行。 */
export const FAMILY_ROWS: readonly TaskFamily[] = ["doc", "build", "gate", "summary"];

export interface DagLayout {
  readonly visible: StudioTask[];
  readonly width: number;
  readonly height: number;
  /** 依赖层数（**不是**行数）：一行可能是一个类别、也可能是某个类别里的某一层。 */
  readonly maxRank: number;
}

/** 一张卡在画布上的位置（一个格子的四个数）。 */
export interface DagPositions {
  /** 依赖层号。**不是行号**：一行可能是一个类别，也可能是某个类别里的某一层。 */
  readonly gr: number;
  /** 视觉行号（按 `FAMILY_ROWS` 分行之后的那一行）。 */
  readonly row: number;
  readonly gx: number;
  readonly gy: number;
}

/** 布局的**纯**计算：只读任务数组，返回位置表与画布尺寸。
 *
 *  P2-10 之前这里叫 `dagGeometry()`，而且是 BoardScreen 在 render 体内直接调的 ——
 *  它会把 `gr` / `row` / `gx` / `gy` **写进入参**。那是一条渲染期副作用：
 *  同一份 props 渲染两次会得到两份不同的对象状态，React 的严格模式会把它读成"渲染不纯"，
 *  并发渲染下更可能出现"这次画的是上一次的坐标"。
 *
 *  现在拆成两半：这个函数只算不写（可以放心在 render、`useMemo`、测试里调）；
 *  写回那一半叫 `dagGeometry()`，只在任务集合真的变了之后调（`adoptTasks` / `setSource` /
 *  推演起跑 / `tick`）—— 那几处本来就在写 state，多写四个字段不算新的副作用。
 */
export function computeDagLayout(
  tasks: readonly StudioTask[],
): DagLayout & { readonly positions: ReadonlyMap<string, DagPositions> } {
  const visible = tasks.filter((task) => !task.hidden);
  const byId = new Map(visible.map((task) => [task.id, task]));
  const memo = new Map<string, number>();
  const rankOf = (task: StudioTask, stack: Set<string>): number => {
    if (task.rank != null) return task.rank;
    const cached = memo.get(task.id);
    if (cached != null) return cached;
    if (stack.has(task.id)) return 0;
    stack.add(task.id);
    const deps = task.deps.filter((dep) => byId.has(dep));
    const rank = deps.length ? 1 + Math.max(...deps.map((dep) => rankOf(byId.get(dep) as StudioTask, stack))) : 0;
    stack.delete(task.id);
    memo.set(task.id, rank);
    return rank;
  };
  /* 层号先落在本地表里，不写任务对象 —— 纯函数的判据就是"调用之后入参一个字段都没变"。 */
  const ranks = new Map<string, number>();
  for (const task of visible) ranks.set(task.id, rankOf(task, new Set()));
  const maxRank = Math.max(...visible.map((task) => ranks.get(task.id) ?? 0), 0);

  /* 分行。类别之间竖向堆叠，同一类别里的同一层横向并排。
   *
   *  `gr` 仍然是**依赖层号**（`edgePath` 之外还有别处读它，也是"依赖层数"那个读数的来源），
   *  视觉上的行号另存在 `row` 上 —— 两者在这一版之前恰好相等，现在不再相等，所以 MUST 分开。
   *  把 `gr` 改成行号会顺手改掉"第 N 层"的含义，也会让 `live.test.ts` 那条
   *  "推演档的固定 rank 仍然优先"变成一句假话。 */
  const rows: StudioTask[][] = [];
  const known = new Set<string>(FAMILY_ROWS);
  for (const family of FAMILY_ROWS) {
    const scoped = visible.filter((task) => (task.family ?? "build") === family);
    if (!scoped.length) continue;
    const layers = [...new Set(scoped.map((task) => ranks.get(task.id) ?? 0))].sort((a, b) => a - b);
    for (const layer of layers) rows.push(scoped.filter((task) => (ranks.get(task.id) ?? 0) === layer));
  }
  // 认不出的类别排在最后一行，不丢卡：后端加了第五段而前端还没跟上时，宁可排得难看也不能让卡消失。
  const strays = visible.filter((task) => !known.has(task.family ?? "build"));
  if (strays.length) rows.push(strays);

  const widest = Math.max(...rows.map((row) => row.length), 1);
  const width = widest * NODE_W + (widest - 1) * GAP_X + 40;
  const height = Math.max(rows.length, 1) * (NODE_H + GAP_Y) - GAP_Y + 40;
  const positions = new Map<string, DagPositions>();
  rows.forEach((row, index) => {
    const rowWidth = row.length * NODE_W + (row.length - 1) * GAP_X;
    const x0 = (width - rowWidth) / 2;
    row.forEach((task, column) => {
      positions.set(task.id, {
        gr: ranks.get(task.id) ?? 0,
        row: index,
        gx: x0 + column * (NODE_W + GAP_X),
        gy: 20 + index * (NODE_H + GAP_Y),
      });
    });
  });
  return { visible, width, height, maxRank, positions };
}

/** 把算好的布局**写回**任务对象与 `state.dag`，返回给调用方读的那一份。
 *
 *  **只在任务集合变化之后调**（`adoptTasks` / `setSource` / `startProject` / `tick`）——
 *  render 里 MUST NOT 调，理由见 `computeDagLayout` 的注释。判据 `web/tests/purity.test.ts`
 *  会扫组件源码，`dagGeometry(` 出现在任何组件的 render 路径里就红。
 */
export function dagGeometry(): DagLayout {
  const layout = computeDagLayout(state.tasks);
  for (const task of layout.visible) {
    const spot = layout.positions.get(task.id);
    if (!spot) continue;
    task.gr = spot.gr;
    task.row = spot.row;
    task.gx = spot.gx;
    task.gy = spot.gy;
  }
  state.dag = {
    visible: layout.visible,
    width: layout.width,
    height: layout.height,
    maxRank: layout.maxRank,
  };
  return state.dag;
}

export function edgePath(from: StudioTask, to: StudioTask): string {
  /* 同**行**走横向弧、跨行走上下的曲线。
   *
   * 判据从 `gr`（依赖层号）换成 `row`（视觉行号）：按类别分行之后这两个数不再相等 ——
   * 文档卡与执行卡的样式表卡依赖层都是 0，却不在同一行。继续用 `gr` 判"同一层"，
   * 一条跨类别的依赖会被画成横向弧，从上一行飘到下一行的卡片上。 */
  if ((from.row ?? from.gr) === (to.row ?? to.gr)) {
    const x1 = from.gx ?? 0;
    const y1 = (from.gy ?? 0) + NODE_H / 2;
    const x2 = to.gx ?? 0;
    const y2 = (to.gy ?? 0) + NODE_H / 2;
    const d = Math.min(46, Math.abs(x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 - d} ${y1}, ${x2 - d} ${y2}, ${x2} ${y2}`;
  }
  const x1 = (from.gx ?? 0) + NODE_W / 2;
  const y1 = (from.gy ?? 0) + NODE_H;
  const x2 = (to.gx ?? 0) + NODE_W / 2;
  const y2 = to.gy ?? 0;
  const dy = Math.max(16, (y2 - y1) / 2);
  return `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
}

export function edgeClass(task: StudioTask, depId: string): string {
  const dep = state.tasks.find((item) => item.id === depId);
  if (!dep) return "e";
  if (task.kind === "rework" || depId === "K5R") return "e rework";
  if (task.status === "run" || task.status === "ready") return "e live";
  if (dep.status === "done") return "e done";
  return "e";
}

export function nodeClass(task: StudioTask): string {
  if (task.status === "run") return "node st-run";
  if (task.status === "review" || task.status === "fail") return "node st-review";
  if (task.status === "done") return "node st-done";
  return "node";
}

export function statusTone(status: TaskStatus): string {
  if (status === "ready" || status === "run") return "var(--accent)";
  if (status === "review") return "var(--warn)";
  if (status === "done") return "var(--ok)";
  if (status === "fail") return "var(--danger)";
  return "var(--ink-muted)";
}

/* 节点第二行：岗位由左边的缩写负责，这一行只放"此刻在干什么"。 */
export function actOf(task: StudioTask): string {
  return stopWordOf(task).action;
}

/* ================================================================
   九、动作
   ================================================================ */

export function setScreen(screen: StudioScreen): void {
  if (state.screen === screen) return;
  state.screen = screen;
  emit();
}

export function setSideTab(tab: SideTab): void {
  state.tab = tab;
  // 进来了就等于看过了。只清 review / deliver 两个带角标的。
  if (tab === "review" || tab === "deliver") state.unread[tab] = false;
  emit();
}

export function setOutputTab(tab: OutputTab): void {
  state.otab = tab;
  emit();
}

export function setDevice(dev: "phone" | "desk"): void {
  state.dev = dev;
  emit();
}

/** 推进倍率。产品界面不再暴露倍速键（owner 2026-10-27 把顶栏收到只剩主题与设置），
 *  演示直链也已退役；这个口子保留给本地验证/脚本，正常界面不调用。 */
export function setSpeed(value: number): void {
  state.speed = value > 0 ? value : 1;
  emit();
}

export function selectTask(id: string): void {
  state.selected = id;
  emit();
}

/** 显式打开某张卡的详情：选中 + 切到「任务详情」。
 *
 *  与 `selectTask` 分开是这一版的核心：选中是"我关注这张卡"，翻页是"我要读它的详情"。
 *  点 DAG 节点时，如果用户正在读审查/交付，翻页会把他刚建立的位置抹掉 —— 所以那条路径
 *  只 `selectTask` + 顶栏提示；只有"前往处理"这种明确意图才 `openTaskDetail`。 */
export function openTaskDetail(id: string): void {
  state.selected = id;
  state.tab = "detail";
  emit();
}

/** 选中提示：3 秒后自动消失，不抢页签。
 *
 *  文案拆成"标题 + 正文"两格，是为了不在顶栏里读成「已选中 K5 已选中」——
 *  `title` 是状态词，`body` 才是"哪张卡、去哪儿看"。 */
export function noticeSelection(id: string): void {
  toast("已选中", `${id} · 在详情里打开`, "ok", 3000);
}

/** 「前往处理」：把左栏选到那张卡上，并确保人还在看板。
 *
 *  为什么单独具名而不是让组件直接调 `selectTask`：这一格是**交付清单的出口**，
 *  它的语义是"去处理这张卡"；`selectTask` 的语义只是"选中"。出口要不要顺手把屏切回看板
 *  是这一格的事 —— 让人从交付清单点出去，落到详情页却发现左栏选中的是另一张卡，比没有按钮更糟。 */
export function goHandleTask(id: string): void {
  // 只改字段、不在这里 `emit()`：紧接着的 `openTaskDetail` 会发一次。
  state.screen = "board";
  openTaskDetail(id);
}

export function closeDrawer(): void {
  state.drawer = null;
  emit();
}

function openDrawer(title: string, html: string): void {
  state.drawer = { title, html };
  emit();
}

export function dismissToast(): void {
  state.toast = null;
  emit();
}

export function startProject(raw: string): void {
  const plan = analyze(raw);
  const tasks = buildTasks(plan);
  const script = buildMeeting(plan);
  const waits = [1100, 1500, 1600, 850, 850, 1100, 1500, 1100];
  script.forEach((line, index) => {
    line.wait = waits[index] ?? 1200;
  });
  script[1].art = { name: "prd.md", owner: "pm" };
  script[2].art = { name: "architecture.md", owner: "cto" };
  script[6].art = { name: "tokens.json", owner: "designer" };

  state.plan = plan;
  state.tasks = tasks;
  dagGeometry();
  state.turns = [];
  state.artifacts = [];
  state.events = [];
  state.reviews = [];
  state.override = {
    orchestrator: { s: "think", note: "主持立项会" },
    pm: { s: "think", note: "等总管点名" },
    cto: { s: "idle", note: "等 PRD" },
    designer: { s: "idle", note: "等页面结构" },
  };
  state.gate = [];
  state.selected = null;
  state.reworked = false;
  state.failUsed = false;
  state.deliver = null;
  state.run = false;
  state.phase = 1;
  state.otab = "prd";
  state.tab = "detail";
  state.unread = { review: false, deliver: false };
  state.dev = "phone";
  state.meeting = { script, i: 0, t: 0, done: false };
  state.screen = "studio";

  log("orchestrator", `受理需求 · 判定为「${plan.typeLabel}」`, "sys");
  /* 立项即记账。存的是"能重跑"的四项：单号、原文、页数、执行卡数 ——
     时间用来排序（同一条需求重跑会顶到最上面，不新增一条）。 */
  recordHistory({
    prj: plan.prj,
    raw,
    date: nowDate(),
    at: nowHM().slice(0, 5),
    pages: plan.pages.length,
    cards: tasks.filter((task) => !task.hidden && task.phase >= 2).length,
  });
  toast("已立项", `${plan.typeLabel} · ${plan.pages.length} 页 · ${plan.ac.length} 条验收标准`, "info");
  emit();
}

function commitTurn(line: ScriptLine): void {
  state.turns.push({ who: line.who, text: line.text, kind: line.kind, t: nowHM() });
  if (line.art) addArtifact(line.art.name, line.art.owner, "doc");
  if (line.kind === "open") log("orchestrator", "项目启动会开始 · 先拆验收标准", "sys");
  else if (line.kind === "close") log("orchestrator", "方案锁定 · 准备派单", "sys");
  else if (line.kind === "arb") log("orchestrator", "裁决分歧 · 记入非目标", "warn");
  else if (line.art) log(line.who, `产出 ${line.art.name}`, "art", line.art.name);
}

function stepMeeting(dt: number): void {
  const meeting = state.meeting;
  if (!meeting || meeting.done) return;
  meeting.t += dt;
  const current = meeting.script[meeting.i];
  if (!current) {
    meeting.done = true;
    return;
  }
  if (meeting.t < (current.wait ?? 1200)) return;
  meeting.t = 0;
  commitTurn(current);
  meeting.i += 1;
  if (meeting.i >= meeting.script.length) {
    meeting.done = true;
    onMeetingDone();
    return;
  }
  const who = meeting.script[meeting.i].who;
  state.override.orchestrator = { s: who === "orchestrator" ? "think" : "idle", note: who === "orchestrator" ? "正在拍板" : "已交办" };
  state.override.pm = { s: who === "pm" ? "think" : meeting.i > 2 ? "done" : "idle", note: who === "pm" ? "正在写 PRD" : "PRD 已交" };
  state.override.cto = { s: who === "cto" ? "think" : meeting.i > 2 ? "done" : "idle", note: who === "cto" ? "正在定结构" : "架构已交" };
  state.override.designer = { s: who === "designer" ? "think" : meeting.i > 6 ? "done" : "idle", note: who === "designer" ? "正在定令牌" : "等页面结构" };
}

function onMeetingDone(): void {
  addArtifact("tasks.md", "cto", "doc");
  const cards = state.tasks.filter((task) => !task.hidden && task.phase >= 2).length;
  log("orchestrator", `方案锁定 · ${cards} 张执行卡 · ${dagGeometry().maxRank + 1} 层依赖 · 并发 3`, "ok", "tasks.md");
  state.override.pm = { s: "done", note: "prd.md 已交" };
  state.override.cto = { s: "done", note: "architecture.md 已交" };
  state.override.designer = { s: "done", note: "tokens.json 已交" };
  state.override.orchestrator = { s: "think", note: "等派单确认" };
  toast("方案已出", `${cards} 张执行卡待派单`, "ok");
}

export function dispatch(): void {
  /* live 档没有"派单"这个动作：真链路的下一道门是后端 A 类审批（`A_SPLIT`），
     由 `ACP_APPROVAL_MODE=auto` 的规则免确认（或 CLI 上的批准）触发。
     在这里推进一格，等于把后端的门禁页面上绕过去。 */
  if (state.source === "live") return;
  if (!state.plan || !state.meeting || !state.meeting.done || state.run) return;
  state.run = true;
  state.phase = 2;
  state.screen = "board";
  for (const id of ["K1", "K2", "K3"]) {
    const task = state.tasks.find((item) => item.id === id);
    if (task) {
      task.status = "done";
      task.progress = 1;
    }
  }
  state.override = {
    orchestrator: { s: "work", note: "盯盘 · 并发 3" },
    be: { s: "sleep", note: "摸鱼 · 静态站无接口" },
    rescue: { s: "sleep", note: "兜底待命" },
  };
  log("orchestrator", "派单完成 · 12 工位进入执行", "ok");
  toast("已派单", "依赖就绪即起跑，并发上限 3", "ok");
  emit();
}

/** 演示：布置一次执行器超时。顶栏键与演示直链入口都已退役；保留给本地验证/脚本。 */
export function injectFailure(): void {
  if (!state.plan || !state.run || state.failUsed) return;
  state.injectFail = true;
  emit();
}

function startTask(task: StudioTask): void {
  task.status = "run";
  task.progress = 0;
  if (task.id === "K7") delete state.override.be;
  if (task.id === "F1") delete state.override.rescue;
  if (task.id === "K11" && !state.gate.length && state.plan) {
    state.gate = state.plan.ac.slice(0, 5).map((item) => ({ id: item.id, text: item.text, on: false }));
  }
  log(task.owner, `开工 · ${task.title}`, "info");
}

function qaFail(task: StudioTask): void {
  task.status = "wait";
  task.round = 2;
  task.deps = ["K5R"];
  task.progress = 0;
  const rework = state.tasks.find((item) => item.id === "K5R");
  if (rework) {
    rework.hidden = false;
    rework.status = "wait";
  }
  addReview({
    k: "bad", who: "qa", title: "QA 打回 · 1 例失败", t: nowHM(),
    body: `<span class="q">375px 视口：导航栏遮挡商品标题。</span><br>截图已附，期望：导航与正文之间保留 ≥ 16px 安全边距。打回前端返工。`,
  });
  log("qa", "E2E @375px 失败：导航遮挡商品标题 → 打回前端", "danger");
  toast("QA 打回", "375px 导航遮挡商品标题，前端返工中", "danger");
  // 这里原来还有一句 `if (!selected) tab = "review"`：把右栏切到「审查」。
  // 删了 —— 抢页签的判定条件（`!selected`）看起来留了余地，实际是"只要没点过 DAG 节点就一定抢"。
  // 现在由 `addReview()` 点「审查」上的角标，页签留给读者自己翻。
}

function execFail(task: StudioTask): void {
  state.failUsed = true;
  task.status = "fail";
  task.progress = 1;
  const rescue = state.tasks.find((item) => item.id === "F1");
  if (rescue) {
    rescue.hidden = false;
    rescue.status = "wait";
  }
  state.override.fe = { s: "review", note: "执行器超时 · 已失败" };
  log("fe", "执行器超时（120s 无响应），任务失败", "danger");
  log("orchestrator", "兜底触发：换人 → 全栈保障专家接管", "warn");
  addReview({
    k: "bad", who: "orchestrator", title: "兜底 · 换人接管", t: nowHM(),
    body: "前端执行器超时。已下发接管指令给全栈保障专家：读报错日志、全盘接手、补齐产物，不改变验收标准。",
  });
  toast("兜底触发", "前端执行器超时，全栈保障专家接管", "warn");
}

let gateShown = 0;

function revealGate(progress: number): void {
  const total = state.gate.length;
  if (!total) return;
  const should = state.gate.filter((_, index) => progress > (index + 0.55) / total).length;
  if (should !== gateShown) {
    gateShown = should;
    state.gate.forEach((item, index) => {
      item.on = index < should;
    });
  }
}

function finishTask(task: StudioTask): void {
  task.progress = 1;
  if (task.id === "K9" && task.round === 1 && !state.reworked) {
    qaFail(task);
    return;
  }
  if (task.id === "K6" && state.injectFail && !state.failUsed) {
    execFail(task);
    return;
  }
  task.status = "done";
  addArtifact(task.artifact, task.owner, "file", undefined, task.id);
  log(task.owner, `完成 · ${task.out}`, "ok", task.artifact);

  if (task.id === "K5R") {
    state.reworked = true;
    addReview({
      k: "good", who: "fe", title: "返工完成 · style.css v2", t: nowHM(),
      body: "导航改可折叠，正文加 <code>padding-top</code> 安全边距，375px 重跑通过。",
    });
    log("fe", "返工完成 · 375px 遮挡已修", "ok");
  }
  if (task.id === "F1") {
    const k6 = state.tasks.find((item) => item.id === "K6");
    if (k6) k6.status = "done";
    log("rescue", "接管完成 · app.js 已补齐，流程回归", "ok", "app.js");
  }
  if (task.id === "K11") {
    state.gate.forEach((item) => {
      item.on = true;
    });
    addReview({
      k: "gate", who: "reviewer", title: "门禁比对 · PASS", t: nowHM(),
      body: `${state.gate.length}/${state.gate.length} 条 AC 全部成立，逐条留痕。判定 PASS，移交验收。`,
    });
    log("reviewer", `门禁 PASS · ${state.gate.length}/${state.gate.length} 条 AC`, "ok", "gate-report.md");
  }
  if (task.id === "K12" && state.plan) {
    const gen = codegen(state.plan);
    state.deliver = { gen, html: inlineDoc(state.plan), version: (state.deliver?.version ?? 0) + 1 };
    log("acceptor", gen.runnable ? "验收签字 · 已点过购物车，375px 无乱版" : "验收签字 · 对照 AC 逐条确认，页面与接口清单齐全", "ok");
    toast("验收通过", "交付包已生成，可在右侧预览与下载", "ok");
    // 同「打回」那条：原来是 `if (!selected) tab = "deliver"` 自动翻页，现在只点角标。
    if (state.tab !== "deliver") state.unread.deliver = true;
  }
  if (task.id === "K13") {
    log("orchestrator", "交付完成 · 交付包已挂下载", "ok", "交付包.zip");
    delete state.override.orchestrator;
    toast("交付成功", "点右侧「交付」可预览与下载", "ok");
  }
}

function computePhase(): number {
  if (!state.plan) return 0;
  if (!state.meeting || !state.meeting.done) return 1;
  if (!state.run) return 1;
  const left = (phase: number): number => state.tasks.filter((task) => !task.hidden && task.phase === phase && task.status !== "done").length;
  if (left(2)) return 2;
  if (left(3)) return 3;
  if (left(4)) return 4;
  return 4;
}

function stepRun(dt: number): void {
  for (const task of state.tasks) {
    if (task.hidden || task.status !== "wait") continue;
    if (task.deps.every(isDone)) task.status = "ready";
  }
  while (state.tasks.filter((task) => task.status === "run").length < state.conc) {
    const next = state.tasks.find((task) => !task.hidden && task.status === "ready");
    if (!next) break;
    startTask(next);
  }
  for (const task of state.tasks) {
    if (task.status !== "run") continue;
    task.progress = Math.min(1, task.progress + dt / task.dur);
    if (task.id === "K11") revealGate(task.progress);
    if (task.progress >= 1) finishTask(task);
  }
  state.phase = computePhase();
}

/** 推进一帧。`dt` 是真实毫秒；`speed` 是演示用的倍率（产品界面不再暴露倍速键）。 */
export function tick(dt: number): void {
  /* 实时档没有推演：任务状态由后端说了算，这里推进任何一格都会变成"页面自己编进度"。 */
  if (state.source === "live") return;
  if (state.paused) return;
  const scaled = dt * state.speed;
  let changed = false;
  if (state.screen === "studio" && state.meeting && !state.meeting.done) {
    stepMeeting(scaled);
    changed = true;
  }
  if (state.run) {
    stepRun(scaled);
    changed = true;
  }
  if (changed) {
    // P2-10：`hidden` 翻面（返工卡/兜底卡出现）会改变行与坐标，所以推进之后重算一次；
    // 13 个节点的代价可以忽略，而 render 从此不必再算。
    dagGeometry();
    emit();
  }
}

/** 回到未立项。**历史保留** —— 它是"立项过什么"的账，不该被"重置这一单"抹掉。
 *  数据源与连接读数同样保留：重置的是"这一单"，不是"这一屏从哪来数据"。 */
export function resetStudio(): void {
  const history = state.history;
  const source = state.source;
  const live = state.live;
  state = initialState();
  state.history = history;
  state.source = source;
  state.live = live;
  gateShown = 0;
  toast("已重置", source === "live" ? "已清空本屏读数（后端数据不受影响）" : "回到待立项状态", "info");
  emit();
}

/** 受理台输入框。打字、点示例、被"修改需求"填回 —— 都走这一条。 */
export function setDraft(text: string): void {
  state.draft = text;
  emit();
}

/** 「修改需求」：把这一单的需求原文带回受理台，清空这一单，等你改完再立项。
 *
 *  它和「重新立项」的区别就是**带不带原文**：
 *  - 修改需求 = 需求基本对，只改几处 → 带回来改（不然你得重新打一遍）；
 *  - 重新立项 = 换一个需求重来 → 清空。
 *  两个动作分开，比让一个按钮去猜意图好。 */
export function editRequirement(): void {
  const raw = state.plan?.raw ?? "";
  const history = state.history;
  const source = state.source;
  const live = state.live;
  state = initialState();
  state.history = history;
  /* 数据源与连接读数跟着走：改需求不等于"退回推演模式"。
     2026-10-27 之前这两行没有，于是 live 档点一下「修改需求」，
     下一帧 `tick()` 就开始推进假会议 —— 页面自己把真数据换成了假的。 */
  state.source = source;
  state.live = live;
  state.draft = raw;
  gateShown = 0;
  toast("已带回受理台", "需求原文已填回，改完再立项", "info");
  emit();
}

/** 「开启新需求」：清空并回到受理台。交付之后唯一该做的事。 */
export function startNewRequirement(): void {
  const history = state.history;
  const source = state.source;
  const live = state.live;
  state = initialState();
  state.history = history;
  state.source = source;
  state.live = live;
  gateShown = 0;
  toast("可以开始新需求了", "受理台已清空，历史里还留着上一单", "info");
  emit();
}

/** 点一条历史 = 用那条原文**重新立项**（不是恢复当时的进度）。 */
export function reopenFromHistory(entry: HistoryEntry): void {
  startProject(entry.raw);
}

/* ================================================================
   十、派生读数
   ================================================================ */

export function detailTask(): StudioTask | null {
  const visible = state.tasks.filter((task) => !task.hidden);
  if (!visible.length) return null;
  return (
    visible.find((task) => task.id === state.selected) ??
    visible.find((task) => task.status === "run") ??
    visible.find((task) => task.status === "review" || task.status === "fail") ??
    visible[0]
  );
}

export function extensionOf(name: string): string {
  const match = String(name || "").match(/\.([a-z0-9]+)$/i);
  return match ? match[1] : "未设";
}

/** 交付清单里每一行都要说清"这份是谁出的"。按产物登记反查，查不到再按扩展名兜底 ——
 *  一律写成当前选中卡的主人是假话（K1 的详情里，"index.html 出自产品经理"）。 */
export function ownerOfFile(name: string): string {
  const record = state.artifacts.find((item) => item.name === name);
  if (record) return record.owner;
  if (/\.py$/i.test(name)) return "be";
  if (/\.md$/i.test(name)) return "writer";
  if (/\.(html|css|js)$/i.test(name)) return "fe";
  return "orchestrator";
}


export function traceOf(task: StudioTask): string {
  const lines = [`[plan] rank=${task.rank ?? task.gr ?? 0} deps=${task.deps.join(",") || "-"} dur=${task.dur}ms`];
  if (task.status === "done") lines.push(`[done] artifact=${task.artifact}`);
  if (task.status === "run") lines.push(`[run ] progress=${Math.round(task.progress * 100)}%`);
  if (task.round > 1) lines.push(`[loop] round=${task.round}`);
  if (task.hidden) lines.push("[idle] 未激活（触发条件未满足）");
  return lines.join("\n");
}

export function openAgentDrawer(id: string): void {
  const agent = AGENT_OF[id];
  if (!agent) return;
  const mine = state.tasks.filter((task) => task.owner === id && !task.hidden);
  const view = agentView(id);
  openDrawer(
    agent.title,
    `<dl class="dkv">
      <dt>工位</dt><dd>${esc(agent.title)} <span class="mono" style="font-size:10px;color:var(--ink-muted)">${esc(id)}</span></dd>
      <dt>职责</dt><dd>${esc(agent.note)}</dd>
      <dt>状态</dt><dd>${esc(view.s)} · ${esc(view.note)}</dd>
      <dt>在手卡</dt><dd>${mine.length ? mine.map((task) => `<span class="dep">${esc(task.id)}</span>`).join(" ") : "无"}</dd>
    </dl>
    <div style="margin-top:12px;font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-muted);font-family:var(--mono)">产出</div>
    <div class="pre">${esc(state.artifacts.filter((item) => item.owner === id).map((item) => item.name).join("\n") || "（暂无）")}</div>`,
  );
}

/* ================================================================
   十点五、文件正文（P1-J）
   ================================================================ */

/** 某个产物的正文。**只有交付之后才有。**
 *
 *  这里有两份关于同一批文件的记账，它们不是重复：
 *  - `state.artifacts`（`ArtifactRecord`）= **过程记账**：谁在什么时候产出了哪个文件、多少行。
 *    流程中逐个 push，所以文件总线在派单后立刻就有内容。
 *  - `state.deliver.gen.files`（`GeneratedFile`）= **最终正文**：`codegen(plan)` 一次性生成，
 *    交付页的 iframe 预览和下载的 zip 用的都是它。
 *
 *  两份按 `name` 对齐。正文只存在后一份里 —— 把它复制进 `artifacts` 就等于养了两份正文，
 *  迟早分叉。所以"能不能点开"这件事，判据就是"交付了没有"：交付前文件总线是一张清单
 *  （它本来就是清单），交付后每一行才是可以打开的文件。
 */
export function contentOf(name: string): string | null {
  if (!state.deliver) return null;
  const files = state.deliver.gen.files;
  const exact = files.find((file) => file.name === name);
  if (exact) return exact.content;
  /* 返工卡产出的是「同一个文件的第 N 版」（`style.css (v2)`）。正文只有一份 —— 交付包里
   * 现在这一份，而它正是返工之后的样子。所以去掉版本后缀再找一次：点 `style.css (v2)`
   * 看到的是"改完长什么样"，这恰好是读者点它的原因。 */
  const base = name.replace(/\s*\(v\d+\)$/, "");
  if (base === name) return null;
  return files.find((file) => file.name === base)?.content ?? null;
}

/** 打开一个文件：抽屉在这一刻才有正当理由 —— 它是**内容查看器**，不是重复的详情页。
 *  （原来那个"任务抽屉"把页签里已经有的 6 项又列一遍，只为了多一段调度日志；那段日志
 *  2026-10-27 已经内联进页签了，所以抽屉这个容器空出来，正好给文件正文用。） */
export function openFileDrawer(name: string): void {
  const content = contentOf(name);
  if (content === null) return;
  const lines = content.split("\n").length;
  openDrawer(
    name,
    `<dl class="dkv">
      <dt>行数</dt><dd class="mono">${lines}</dd>
      <dt>大小</dt><dd class="mono">${new TextEncoder().encode(content).length} B</dd>
    </dl>
    <pre class="pre" style="margin-top:9px">${esc(content)}</pre>`,
  );
}

/* ================================================================
   十一、示例需求（受理台的五个例子）
   ================================================================ */

export const STUDIO_EXAMPLES: readonly { label: string; text: string }[] = [
  { label: "手冲咖啡 · 静态官网", text: "做一个卖手冲咖啡的静态官网，要包含首页、商品展示、联系我们三个页面。要有购物车按钮（仅UI交互），并且能在手机上正常看。" },
  { label: "健身房 · 预约小程序", text: "做一个健身房会员预约小程序，要有首页、课程列表、在线预约、个人中心，支持手机号登录，能在手机上正常使用。" },
  { label: "电商 · 数据看板", text: "做一个电商运营数据看板，要有概览、明细报表、图表统计，支持实时刷新，能在手机上看。" },
  { label: "律师 · 个人品牌站", text: "做一个律师个人品牌官网，包含首页、案例、关于我们、联系我们，要有咨询表单，风格稳重，手机适配。" },
  { label: "报销 · 内部审批系统", text: "做一个公司内部报销审批系统，要登录、审批流、导出报表，手机也能用。" },
];
