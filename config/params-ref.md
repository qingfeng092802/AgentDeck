# 参数与事实引用表（params-ref）

**被谁引用**：`.specify/memory/constitution.md`「二、接入方式、凭据与成本归因约束」、原则 V、
「三、开发流程与质量门禁」，以及 Governance 修订程序第 5 步与版本策略的归类判据。

**本表的治理地位**：本文件是**治理承载文件**，不是普通配置备忘。宪法把易变取值下沉到此处，
前提是这些取值在这里**仍然可被门禁执行**。因此：

- 本表的值变更按宪法版本策略属**引用型参数/事实值变更 ⇒ PATCH**，仅需同步本表与 PRD 出处，
  不触发 MAJOR / MINOR。
- 本表「已否决命名」一节 MUST 作为静态扫描门禁的输入（用法见该节）。
- 本表任何一节的删除，或使宪法某条判据失去可执行性的改写，**不属 PATCH**，按宪法「归类判据」
  升为 MINOR / MAJOR 并重走修订程序。
- 取值来源全部标注 PRD 章节与行号；PRD 与本表不一致时，**以 PRD 的核实结论为准并同步修正本表**
  （宪法修订程序第 5 步的同步义务）。

**最近一次同步**：2026-10-11（随宪法 v1.0.1 从正文下沉）| **来源**：`PRD-v1.4.md` 第六章、3.4、7.0、8.3

---

## 1. 卡点超时默认值（对应宪法原则 V）

| 卡点 | 默认超时 | 超时后行为 | 附加结构约束 | PRD 出处 |
|---|---|---|---|---|
| A 拆单确认 | 无超时 | 挂起等待 | 涉及整体方向，MUST NOT 自动放行 | 3.4 ③（`:161`） |
| B 验收未过 / 需人工定论 | 2 小时 | 按默认策略继续 | **收敛仅在第 3 轮结束后生效**；未达 3 轮时改为延长等待，**每次 2 小时、最多 3 次** | 3.4 ③（`:161`） |
| C 超预算 | 无超时 | 挂起等待 | 成本不可逆，不自动放行 | 3.4 ③（`:161`） |
| D 危险操作 / 高风险动作 | 无超时 | 挂起等待 | 安全优先 | 3.4 ③（`:161`） |
| E 审查不确定 / 需人工定论 | 无超时 | 挂起等待 | 审查无法判定，MUST NOT 自动放行 | Phase 17 D3（owner 2026-10-23） |

> 结构约束（"仅在第 3 轮后收敛""未跑满只延长不收敛"）留在宪法正文，不在此表；本表只承载时长与次数。
> 每类卡点的超时时长与超时后行为均 MUST 可配置。

**非卡点执行超时的默认值 = 900 s**（owner 2026-10-20 裁决 2，会话即凭证）。它不是 A/B/C/D 四类卡点里的
任何一类 —— 上面那张表的列是"给人看的闸"，这一行管的是**一次外部执行**在子进程里最多跑多久，所以不进那张表。

- 唯一代码载体：`QoderCliSpec.timeout_seconds`（`src/acp/adapters/qoder/transport.py`）。
- 超时后行为：杀掉直接子进程、登记 `timed_out=True`、归**可重试**（transport 模块头约束 ③）；
  失败消息同时带上"最后发起的工具"与累计工具块数（裁决 3，用来分"在干活时被杀"与"启动就僵住"）。
- 两侧对读判据：`tests/contract/test_qoder_transport.py::test_the_run_timeout_has_one_number_on_both_sides`
  （改这里而不改代码 ⇒ 红，反之也红）。
- **这一行 MUST NOT 写成 `|` 开头的表格**：`src/acp/approval/timeouts.py::section_one_lines()` 吃的是
  "§1 这一节里所有以 `|` 开头的行"，它只按首列 == `卡点` 剔表头 ⇒ 多一张小表就会被当成第五类卡点，
  现场是 `ParamsRefError: §1 的卡点列首字母 '外' 不在 approval_kind 域里`（本笔实测撞过一次）。
- 另一半账仍挂着：这个值在**代码**里而不在 `config/runtime.yaml` 那个能读的面里 ⇒ 改它要动代码，
  与本节末句"MUST 可配置"相冲（原文见下方 2026-10-13 挂账块，那条记录不改写）。

> **【2026-10-13 owner 裁决登记：三条"代码里有、配置里没有"的参数，在本节挂账】**
> owner 两次点名"登记 `params-ref §1` 待补"（T041 的 `timeout_seconds`；T042 的 `max_tokens` 与 `reasoning_tokens`）。
> 上面那张表的列是「卡点 / 默认超时 / 超时后行为 / 附加结构约束 / PRD 出处」，后两条**不是超时**（一条是请求参数、
> 一条是计费维度）⇒ 塞进表里会让本节说谎，所以这里只以**账目**形式登记"缺载体"这件事本身，并给出落点候选等他定，
> 不改本表语义、也不自加枚举值：
> - `timeout_seconds` —— 适配器侧"一次外部执行"的等待上限。现读唯一来源 = `QoderCliSpec.timeout_seconds`，默认 **120.0**，
>   配置文件里没有对应项。候选落点：本节加一行"非卡点执行超时"，或在 §3 旁新开一节。
>   **【2026-10-20 落位：三条里的第一格按第一个候选平了】** 载体 = 上方那段"非卡点执行超时的默认值"（刻意不
>   写成表格，理由写在那一段里），值 = owner 裁决 2 定的 **900 s**，两侧读数有判据对读。上面那行 `120.0`
>   是 2026-10-13 的现读、原文不改写；它已被第四发点火的实测推翻 —— 那一次 120 s 杀掉的是一枚**还在正常
>   推进**的会话（19 轮 / 29 个工具块），它量的是"initialize 握手要多久"而不是"一张卡跑完要多久"。
>   没平的是另一半：值仍在代码里、不在 `config/runtime.yaml` 那个面里 ⇒ 见本节新增段末条。
> - `max_tokens` —— 豆包方舟的请求参数。现读唯一来源 = `DoubaoApiSpec.max_tokens`，默认 `None`（**不发这个键**），
>   甲档那次带的 `16` 是探针值、不是平台参数。候选落点：§4 接入参数表的豆包行加一列。
>   **【2026-10-19 更新：载体已落，账未全平】** 落点最终不是 §4 加列，而是新开 **§12**（`config/runtime.yaml`）——
>   "改它要动代码"这半条从此不再成立。仍挂着的是另一半：这个键**至今没有生产形态的消费者**
>   （复算 = `grep -rn "DoubaoApiSpec(" src/acp --include=*.py` 现读 **0** 处），§12 把它如实标成 `consumers: []`。
>   本节这条记录留原文不改，是为了让"这条账分两半、哪一半先平"这件事在后面还能被看见。
> - `reasoning_tokens` —— 方舟 `usage.completion_tokens_details.reasoning_tokens`，甲档真机读到 **29 / 25**；
>   而 `protocol/refs.UsageKind` 只有 `INPUT / OUTPUT / CALL / SEAT_DAY` 四档，**没有能放它的地方** ⇒ 现在它是结果里的一条
>   `issue` 留证，不落账本。候选落点：§6 价目快照加维度，或 EN-13 加一列。
> 三条共同点：**默认值在代码里、配置里没有** ⇒ 改它要动代码，与本节末句"MUST 可配置"相冲 ⇒ 这处冲突一并挂在账上，
> 等他定载体形态后再迁。**（现读 2026-10-19：三条里只剩 `timeout_seconds` 与 `reasoning_tokens` 仍是"配置里没有"；
> `max_tokens` 的载体见 §12。复算 = `grep -c "^RUNTIME_KEYS=" config/params-ref.md` 现读 **1**（锚在行首，
> 免得这句正文里的提及把自己数进去），且 `config/runtime.yaml` 存在。）**

## 2. 低风险免确认阈值（对应宪法原则 V）

| 参数 | 值 | 说明 | PRD 出处 |
|---|---|---|---|
| 预估 token 预算阈值 | **5 万 Tokens** | 三条低风险条件之一（③）；规则表路径 `config/risk_matrix.yaml` | 3.4 ①（`:159`） |

> 低风险三条件本身（不涉及文件写入/删除、不调用外部付费 API 或在免费额度内、预估预算 ≤ 阈值）
> 与"AI 不得覆盖判定""免确认仅免除卡点 A"属结构约束，留在宪法正文。

## 3. 并发默认值（对应宪法「三」）

| 参数 | 值 | 关系式判据 | 配置文件 | PRD 出处 |
|---|---|---|---|---|
| 并发上限默认 | **3** | 默认并发上限 = 首批接入 Agent 数（首批 3 个 ⇒ 3） | `config/concurrency.yaml` | R-05（`:118`）、7.0 ④（`:418`） |

> 首批接入规模若变化，本值随之变化即触发该关系式的重新核对，属 PATCH。

## 4. 接入参数表（对应宪法「二」）

| 工具 | 形态 | 接入方式 | 鉴权 / 凭据 | 端点与版本 | 来源 |
|---|---|---|---|---|---|
| WorkBuddy | 平台内 | Managed Agents API（REST，云 API 3.0；另有 Python / TypeScript ADK） | 腾讯云 **CAM 鉴权**：**TC3-HMAC-SHA256 签名 + SecretId / SecretKey**（**不是 API Key**） | `workbuddyenterprise.tencentcloudapi.com`；公共参数含 `Action`、`Version=2026-07-09`；`Region` 无需传递 | 6.1（`:323`）、8.3 T-2（`:533`） |
| Qoder | IDE / SDK | Qoder Agent SDK，PyPI 包 **`qoder-agent-sdk`**，导入名 **`qoder_agent_sdk`** | **Service Account（推荐）** / PAT（备选）/ 本机 qodercli 登录态（仅本地开发）；费用由所配模型厂商结算（BYOK），不占 Qoder Credits | 见 §5 令牌与置换 | 5.2（`:281`）、6.1（`:324`）、6.2（`:331`、`:335-350`） |
| Qoder（国内版执行器，2026-10-24） | CLI 子进程（直起） | **`qoderclicn`** —— 默认安装位 `%USERPROFILE%\.qoder-cn\bin\qoderclicn\qoderclicn.exe`；PATH 上的 `qodercn` 是 `.cmd` dispatcher，只作最后手段 | **`QODERCN_PERSONAL_ACCESS_TOKEN`**（CN 主变量）；国际版 `QODER_PERSONAL_ACCESS_TOKEN` 保留给回退档。CN 运行时子环境把选中的值**同时**放到两个 PAT 名字下 | `qoderclicn --version` ⇒ **1.1.78**；查找顺序与降级 warning 见 `quickstart.md` 第 10 条 | owner 2026-10-24 裁决 1/2 |
| 豆包 | HTTP / API | 火山引擎方舟 OpenAI 兼容 API | **`ARK_API_KEY`**（凭据，只从环境来）；模型 ID 走环境变量 `ACP_DOUBAO_MODEL`（部署事实 ⇒ 代码里不留默认值，缺了就由构造点拒，MVP-2 笔 H） | `base_url = https://ark.cn-beijing.volces.com/api/v3` | 6.1（`:325`）、6.3 |

**真装配面的载体（2026-10-19 B 计划 第一步）**：`acp.adapters.spec_factory.build_real_transport()` 按**厂商包的约定属性**取值，
工厂自己不留任何凭据变量名与秒数的副本 —— 约定属性 = `CREDENTIAL_ENV_NAMES`（凭据变量名，任一有非空值即可，与厂商自己那道门同语义）、
`HTTP_TIMEOUT_S`（一次请求的上限秒数，唯一载体 = `src/acp/adapters/doubao/transport.py`，现值 **120.0**，owner 明示）、
`PRODUCTION_RUNNER`（SDK 形态用哪个 runner，唯一载体 = `src/acp/adapters/qoder/transport.py`）。
缺任何一个 ⇒ `RealWiringRefused`，**不降级成 stub**；`acp.run --transport` 那道门不受影响，仍只放 `stub`（R-07）。
Qoder 那一支的 120 秒是**子进程**上限（`QoderCliSpec.timeout_seconds`，见 §1 那条"没有签批载体"的登记），与上面那个 httpx 数同值不同物。
复算 = `PYTHONPATH=src python -m pytest -q tests/guards/test_transport_spec_factory.py`（节 五）。

**模型 ID 写法**：火山方舟官方 Model ID 采用**连字符**命名，例 `doubao-seed-2-1-pro-260915`；
`deepseek-*` 为通配写法，具体型号以方舟控制台实际 Model ID 为准（PRD 3.5 `:176`、T-4 `:535`、T-5 `:536`）。

**DeepSeek 官方端点的请求名与实际路由（2026-10-23 POC 实测）**：Phase 17 的甲路线走 `https://api.deepseek.com/v1`，
请求体的 `model` 写 **`deepseek-chat`**；官方服务端把它路由到 **`deepseek-flash`**，响应体的 `model` 字段与
POC-7 的 400 报错都显示 `deepseek-flash`。因此 P1 的甲路线请求构造 MUST 用 `deepseek-chat` 作为请求名；
审计与成本归因 MUST 记录响应体里的实际 `model`，不得把请求名当成实际计费模型。乙路线的
`ACP_DEEPSEEK_MODEL` 仍由 owner 在方舟控制台确认，不套用本句；火山方舟上的 `deepseek-*` 也仍按上一句
以控制台 Model ID 为准。

**自动审查的运行模式 `ACP_REVIEW_MODE`（Phase 17 P3，2026-10-28 加第三格 `auto`）**：
取值域 = `manual` / `deepseek` / `auto`，**默认 `manual`**；
其它值在启动期拒（`ReviewModeInvalid`，不降级成 manual）。该变量由 `acp.serve` 在**进程启动时读一次**，
运行期不热更新 ⇒ 改模式 MUST 重启服务。`manual` 下审查席仍是 Phase 16 的 qoder / doubao / workbuddy，
行为逐字不变；`deepseek` 下非 workbuddy 执行的卡把审查席收到 `deepseek-reviewer`（workbuddy 执行的卡仍取
qoder / doubao —— 宪法「三」的三岗互斥优先，故该模式下仍可能出现人工审查席）。`deepseek` 模式缺
`DEEPSEEK_API_KEY` 时在构造审查客户端处直接失败（消息只点变量名），**不把服务降级成 manual**；该变量只
作用于 `acp.serve` 的网页受理/续跑链（`WebResumer` → `acp.realrun`），CLI 路径的默认值是 `manual`。
每次自动审查尝试另写一行 EN-18 `review_report`（只存 sha256 与结构化判定，不存原始 prompt / 响应正文）；
读取面 = `GET /tasks/{task_id}/review-report`，操作口径见 `quickstart.md §9.5`，判据 =
`tests/integration/test_review_report_api.py`。

**第三格 `auto` = 门禁代签（2026-10-28 立）**：产物过门禁（`validate_artifact` 判 `valid`）即视为通过，
卡**不进人工评审、也不调模型** —— 签在 `acp.realrun.run_card_segment()` 的跑卡事务里
（`_sign_by_gate()`），主语写 `gate-reviewer`（不是 `orchestrator`：编排器没有审查职责），
验收 mark 是 `gate-review-pass:artifact-gate`，与 `human-review-pass:*` / `review-auto-pass:*` 三者互不相同。
它与 `deepseek` 的差别只有一条但很关键：`deepseek` 是"独立审查员给 PASS，再按风险档决定谁签"，
`auto` 是"没有人审，门禁判产物达标就过" ⇒ **`auto_accept_low_risk` 那个开关在 `auto` 下不参与判定**
（它管的是"机器能不能替人签一份被审过的结论"，而这里根本没有那份结论；面板据此如实显示）。
`auto` **不建审查跑者**（`serve.resolve_review_runner()` 回 `None`），因此也不需要 `DEEPSEEK_API_KEY`；
选人池与 `manual` 逐字相同（不把 `deepseek-reviewer` 放进来）。签不掉时（例如卡不在 `REVIEWING`）
卡留在原态、留一条 `review_gate_failed`，人工照旧接得住。判据 = `tests/guards/test_review_mode_auto.py`。

**拆单确认的运行模式 `ACP_APPROVAL_MODE`（2026-10-28 立）**：
取值域 = `manual` / `auto`，**默认 `manual`**；其它值在启动期拒（`ApprovalModeInvalid`，不降级成 manual）。
与 `ACP_REVIEW_MODE` **各管一件事、互不推导**：本变量管"拆单方案要不要人点头"，`ACP_REVIEW_MODE`
管"卡交上来谁签验收"。由前者推导后者，会让"只免验收、拆单要看一眼"这种组合没有表达方式。
`auto` 下拆单方案不再停在 `PENDING` 等人点「批准」，而是由**规则**署名通过 —— 主语写
`auto-split-confirm:ACP_APPROVAL_MODE=auto`（形状照 `risk_rule:workbuddy:architect`，三个月后能一路查回
这一行 `.env`），走 `acp.approval.repo.apply_auto_pass()` 这唯一写者，另写一行 `channel=AUTO` 的 EN-10
到达流水（`resolution_channel` 的 `AUTO` 值只有它能写，人工通道枚举里没有它 ⇒ 机器免确认与"人批的"
在流水上分得开）。签完由 `WebIntakeRunner` 另开会话调 `graph_resumer` 续跑（排在会话关闭之后：
续跑自己开会话、自己提交，要读到已提交的 `APPROVED`）。
只免确认**不碰门禁、不碰验收**；`manual` 下行为逐字不变（仍停在 `PENDING`）。判据 =
`tests/guards/test_approval_mode_auto.py`。

**会议轮数 `ACP_MEETING_ROUNDS`（2026-10-28 立）**：
取值域 = `1` / `2` / `3`，**默认 `2`**；其它值（`0` / `4` / `two` / 空串以外的任何东西）在启动期拒
（`RoundsInvalid`，**不降级成默认值**）。一轮 = 四个岗位各说一次，所以 `2` 轮 = 8 次串行模型调用。
为什么不默认 1：一轮是"四个人各说一遍"，没有人回应过别人 —— 那叫汇报，不叫讨论；
为什么上限 3：每多一轮多 4 次调用（实测一场 2 轮的会 2–4 分钟），3 轮已接近一次受理能等的边。
**第二轮起换发言顺序**：总管挪到最后做收敛（`speaking_order`），其余三人先点名回应上一轮别人对
自己方案的那句质疑（`REBUTTAL_SUFFIX`）。总管如果还第一个说，它手里没有别人的新话可收敛，
所谓"拍板"只是把主持词再念一遍 —— 八次调用、八段话，读起来仍然是四段。
轮数同时写进 `summary_digest["会议轮数"]`（每轮落库时一并写），前端靠它算"说完了没有"：
写死 4 会让第 2 轮刚开始就被判成"会议结束"。判据 = `tests/contract/test_meeting_service.py`。

**追加需求 `POST /requests/{id}/extend`（2026-10-28 立）**：
在**已产生的 PRD 基础上**加一句，四岗位带着上一版 PRD 重开一场会，只重出 PRD，
**已有任务卡不动**（`serve_intake._run` 按"库里有没有卡"这条事实分叉，不另设开关）。
不改 `raw_input`（那是"客户最初说的那句话"）、不改 `status`（这一趟不推进状态机）。
digest 的变更有三条硬规则，见 `serve_intake.extend_digest` 与 `tests/guards/test_extend_digest.py`。

**认证方式推荐级别**：平台作为编排中枢推荐 Service Account（不依赖个人账号）；PAT 适用于需要用户权限的
脚本与 CI；本机登录态仅限本地开发阶段（PRD 6.2 `:337-341`）。

## 5. 令牌与置换（对应宪法「二」凭据管理）

| 参数 | 值 | 来源 |
|---|---|---|
| 短期令牌形态 | SAT（JWT），以 `Authorization: Bearer <SAT>` 调业务接口 | 8.3 Q-12（`:478`） |
| 最长有效期 | **12 小时**，即 `ttl_seconds ≤ 43200` | Q-12（`:478`） |
| 刷新 | **过期不可刷新，只能用长期凭据重新置换** | Q-12（`:478`） |
| SA Key 创建 | 创建时**不选 scope** | Q-12（`:478`） |
| 端点与 scope 取值 | MUST 与 SA Key 所在**区域一致** | 8.3 口径说明（`:480`） |

> "长期凭据 ≠ 业务令牌""过期不可刷新只能重新置换""端点与区域一致"三条留在宪法正文（结构约束）；
> 本表承载其具体数值与字段。

## 6. 价目快照（对应宪法「二」成本归因）

> 本节全部为**外部市场事实**，随厂商调整而变化（⇒ PATCH）。宪法正文只保留"三类口径 MUST 分列、
> 不得互相加总""任一类小计当作平台总成本属口径违规""免费额度不得作容量承诺"三条原则。

**WorkBuddy（按坐席订阅 + Credits 计量，非按 Token）** — PRD 8.3 Q-11（`:477`）、6.4（`:380`）

| 项 | 值 |
|---|---|
| 企业旗舰版 | 198 元/人/月，含 2,000 Credits/月 |
| 企业专享版 | 316 元/人/月，含 2,000 Credits/月 |
| 加量包 | 2,000 Credits = 100 元；5,000 = 245 元；10,000 = 480 元（有效期 6 个月） |
| 日均坐席成本（1 坐席、按月计） | 约 6.6 元/坐席/天（198 ÷ 30）；年付 2,376 元/人/年折合约 6.5 元/天 |
| 接口频率限制 | 以各接口文档为准（`CreateAgent` 等 20 次/秒，维度 = API + 接入地域 + 子账号） |
| 定价生效 | 腾讯云计费公告，2026-05-15 起 |

**Qoder（内置模型 Credits 计费 vs BYOK）** — PRD 6.2（`:347`）

| 方案 | 值 | 本平台建议 |
|---|---|---|
| 体验版 | 免费（2 周试用 + 300 Credits） | 仅试用 |
| 个人专业版 | 59 元/月，2,000 Credits | 不推荐（成本不可归因到单任务） |
| 高级版 | 169 元/月，6,000 Credits | 不推荐（同上） |
| BYOK | 自带厂商 Key，费用由模型厂商结算，不占 Credits | **推荐** |

> BYOK = 自带**厂商** Key，具体用哪个模型由配置决定（豆包 / DeepSeek / 其他皆可），不固定等于豆包。

**豆包 / 火山方舟（按 Token）** — PRD 6.3（`:360-363`、`:368`）

| 模型 | 输入 | 输出 | 适用 |
|---|---|---|---|
| Doubao-Seed-2.1-pro | 6 元/百万 Tokens | 30 元/百万 Tokens | 深度推理、复杂生成 |
| Doubao-Seed-2.0-mini（≤32K） | 0.2 元/百万 | 2 元/百万 | 检索、分类、简单生成 |
| Doubao-Seed-2.0-mini（32K–128K） | 0.4 元/百万 | 4 元/百万 | 中等长度内容 |
| Doubao-Seed-2.0-mini（>128K） | 0.8 元/百万 | 8 元/百万 | 长文档处理 |

| 项 | 值 |
|---|---|
| 开发期免费额度 | Doubao-Seed-2.1-pro 每日最高 500 万 Tokens 免费 —— **活动性权益、按日领取、可能调整** |
| 日成本快照（假设 50 任务/天、每任务 2 万输入 + 5 千输出） | 豆包输入 6 元 + 输出 7.5 元 = **约 13.5 元/天（仅豆包部分小计）** |
| 完整总账口径 | 豆包 Token 成本 + Qoder BYOK 成本（按所配厂商）+ WorkBuddy 坐席成本，**三者不可合并为单一数字** |

## 7. 已否决命名（静态扫描门禁输入，对应宪法「二」防回归护栏）

| 禁用项 | 错误类型 | 正确替代 | 核实出处 |
|---|---|---|---|
| `WORKBUDDY_API_KEY` | 经核实**不存在**的凭据形态 | CAM 鉴权：`SecretId` / `SecretKey` + TC3-HMAC-SHA256 签名 | PRD 6.1（`:323`）、T-2（`:533`） |
| `qodercn-agent-sdk` | 包名错误 | `qoder-agent-sdk` | PRD `v1.4-fix-2`（`:333`、`:518`） |
| `qodercn_agent_sdk` | 导入名错误 | `qoder_agent_sdk` | 同上 |
| `DOUBAO_API_KEY` | 非官方环境变量名 | `ARK_API_KEY` | PRD 6.1（`:325`） |
| `DOUBAO_BASE_URL` | 非官方环境变量名 | `base_url` | PRD 6.1（`:325`） |
| `DOUBAO_MODEL_NAME` | 非官方环境变量名 | 以方舟实际 Model ID 配置 | PRD 6.1（`:325`） |
| `doubao-seed-2.1-pro` | 点号写法非官方 ID | 连字符 `doubao-seed-2-1-pro-260915` | PRD 3.5（`:176`）、T-5（`:536`） |

**门禁用法**：在仓库根执行，命中数 MUST 为 0。排除项**只限"必须记录被否决项历史"的审计类文档**
（需求文档、本表、宪法评审报告、规格质量清单、**已逐份登记文件名的**复审报告）；其余一律计入，出现即视为护栏被破坏：

```bash
BAN='WORKBUDDY_API_KEY|qodercn[-_]agent[-_]sdk|DOUBAO_(API_KEY|BASE_URL|MODEL_NAME)|doubao-seed-2\.1-pro'
grep -rniE "$BAN" --include='*.py' --include='*.md' --include='*.yaml' --include='*.json' --include='*.ts' \
  --include='*.toml' --include='*.yml' --include='*.tsx' \
  --exclude-dir=.venv --exclude-dir=.venv-linux --exclude-dir=.git --exclude-dir=__pycache__ \
  --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=results --exclude-dir=.wbtmp \
  --exclude=PRD-v1.4.md --exclude=params-ref.md --exclude=requirements.md \
  --exclude=constitution-review-v1.0.0.md --exclude=tasks-review-v1.0.0.md .

# `.env*` 单独扫：它的行里可能带凭据值，故用 -o 只输出命中的 token、不回显整行（防打进终端 / CI 日志）。
# 扫描面含 .env.example 与本机真实 .env（后者虽被 git 忽略，但同样不得出现被否决命名）。
grep -rnoiE "$BAN" --include='.env*' --exclude-dir=.venv --exclude-dir=.venv-linux --exclude-dir=.git .  # MUST 无输出
```

**执行方式（2026-10-11 按 owner 裁决把上面的命令落成脚本）**：在仓库根跑
`bash scripts/guard_naming.sh`。脚本**不复制清单**——它从上面这个 fenced block 解析
`BAN` / `--include=` / `--exclude=` / `--exclude-dir=`，所以**本节仍是唯一事实源**，改本节即改门禁行为。
脚本自带两道自证：①排除表里若丢了 `params-ref.md` 就直接报错退出（防"清单自指导火"被当成 0 命中）；
②`--positive-control <无害词>` 用无害词验扫描管线会开火，**不为了自证把被否决命名再抄一遍**。

> **`--exclude-dir` 的来历（2026-10-11 落成脚本时暴露）**：Phase 0 之后仓库根多了 `.venv/`（stdlib venv，
> 内含 `pip` 自带的上万文件），门禁若继续递归它会扫进**第三方包**——而被否决命名护栏针对的是**我方产物**
> （`pyproject.toml` / `.env` / 源码 / 文档），第三方包里的同名字符串不是本仓库能负责的写入面，
> 且 `.venv/` 已被 `.gitignore` 拦、不会入库。排除的是**依赖与版本库目录**，不是任何文档文件，
> 故与上一条"排除项只限审计类文档"的登记规则不冲突。
>
> **同族的第二个依赖目录（2026-10-12 Phase 1.3 补，PATCH）**：WSL2 主路径建的是 `.venv-linux/`，
> 名字不等于 `.venv` ⇒ `--exclude-dir=.venv` **管不住它**，门禁一直在递归扫这 4655 个
> `*.py` / `*.json` / `*.toml` 第三方文件（实测 `find .venv-linux -name '*.py' | wc -l` = 4655，
> 主扫描耗时 5.3 s）。补 `--exclude-dir=.venv-linux` 后：① 门禁读数不变（`main=0 env=0`，
> 见下面的复跑记录）；② 扫描面回到"我方产物"这一条登记规则的本意；③ Python 侧载体
> `src/acp/guard/naming.py`（T028）与 shell 载体从此**同一张 surface**——在此之前两者不可能等值
> （Python 侧要扫 4655 个依赖文件才与 shell 一致，那已不是"秒级 fast 档"该做的事）。
> 与"include 缺口"那两条同类：**这是扫描面的漏口，不是命中数的变化**，所以取证看的是
> `--show-spec` 的 `exclude-dir` 项数（**3 → 4**）与主门禁读数不变，两者一起才成立。

> **同族第三个"我方临时产物"目录（2026-10-20 笔 AD 收口，owner 明示"必须在扫描器的配置里加上
> `--exclude-dir='.wbtmp'`"）**：`.wbtmp/` 是架构师为 `plan-web-v2.md` 那套探针建的临时目录，`.gitignore:12`
> 早把它拦在版本库外，但 §7 的扫描器跟的是**工作树而不是 git 索引**（上面那句"未登记的排除项不算通过"管的
> 正是这一格）⇒ 它一直在门禁的扫描面上：落在 include 扩展名里的份数 = **20**
> （复算 = `find .wbtmp -maxdepth 1 -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.ts' -o -name '*.tsx' \) | wc -l`），
> 而 naming 的 `scanned_files` 从笔 AC 那发的 **282** 涨到本笔的 **302** —— 本笔自己只新增 **1** 份被扫文件
> （`web/src/components/AgentRail.tsx`），其余差额当时**没能完全归因**，这一格由本条排除项关掉。
> 取证（三行都实跑，不是推断）：① 补项前 `PYTHONPATH=src python -m acp.guard.naming` ⇒ `scanned_files=302`、
> 补后 ⇒ **282**（差额 20 = 上面那条 find 的份数，逐份对得上）；② `--show-spec` 的 `exclude-dir` 项数
> **7 → 8**；③ 主门禁两发都 `main=0 env=0 dep=0 gate=PASS` ⇒ 与 `.venv` / `.venv-linux` 那两条同类：
> **漏的是扫描面，不是命中数**。**再点名一句**：补后这个 **282** 与笔 AC 那发同数，但**不代表是同一批文件**
> —— 本笔新加了 1 份前端源码、架构师在两次测量之间也动过 `.wbtmp` 之外的东西；"数值相同"不是"集合相同"，
> 要证集合得逐份列，本笔没有逐份列，所以这句只作为限度登记，不拿它当"回到原状"的证据。
> **owner 那句里的 `.venv` 这一半本来就在**：活命令第一行就是 `--exclude-dir=.venv`（2026-10-11 落成脚本时
> 补的，见上面那条来历），本笔没有新增它，只在这里点名 —— 免得下一轮把"加了 .venv"当成一处改动去找。

> **include 缺口的来历（2026-10-11，Phase 0 实证，owner 裁决采纳）**：为确认 `pyproject.toml` 的
> `redis = 0` 需额外手工指定 `--include='*.toml'`，说明原 include 有缺口——被否决的**包名 / 导入名**
> 最容易正是写进 `pyproject.toml` 与 `.env` 系列文件里，而这两类此前不在扫描面内。本行即该次实跑
> 暴露出的修口记录。
> **第二个同类缺口（2026-10-11 20:20，owner 额外授权 1 采纳）**：`*.yaml` 不含 `*.yml`，
> 为核新建的 `docker-compose.yml` 是否含禁用命名需额外手工指定 ⇒ 已把 `--include='*.yml'` 补进活命令。
> 取证（两条都实跑，20:30 复算）：这条判据**写成关系式而不是绝对数**——绝对命中数会随着文档里
> 提到这个 token 的次数漂（我本轮就先写了"1"、随后自己把它改假过一次）。关系式两条：
> ① **同一正对照 token，去掉 `--include='*.yml'` ⇒ 命中文件 0 个；带上 ⇒ 1 个，且 `comm -13` 的差值文件
> 正是 `docker-compose.yml`**（复算：`grep -rliE "<旧 include>" <排除项> 'POSTGRES_PASSWORD' .` 与
> `<新 include>` 各跑一次，比对文件列表）；② 带上 `.yml` 后主门禁仍 `main=0 env=0`（`--show-spec` 的
> include 项数由 **6** 变 **7**）。补之前那是**假绿**：文件里写着 `POSTGRES_PASSWORD`，门禁看不见。

> 读数（2026-10-11，随宪法 v1.0.1 落地后复算）：**0 命中**。新增审计类文档若需引用禁用项，
> MUST 把其文件名补进上面的 `--exclude` 列表并在合规审查中说明理由；未登记的排除项不算通过。

> **脚本化后的读数（2026-10-11 19:32 +0800 实跑，四行都是跑出来的不是推断）**：
> `bash scripts/guard_naming.sh` ⇒ `naming-guard main=0 env=0`、`SUMMARY main=0 env=0 gate=PASS`、退出码 **0**。
> `--show-spec` ⇒ 解析出 **6** 个 `--include` / **5** 个 `--exclude` / **3** 个 `--exclude-dir`（与上面的活命令逐字同集，
> 无通配项）。`--positive-control requires-python`（无害词）⇒ 命中 **8**、退出码 **0** ⇒ 管线会开火，不是恒绿。
> **该项判据锁"命中 ≥ 1"而不是定值**：正对照词在本表与本清单里被提到几次读数就涨几次（同轮内由 **7** 涨到
> **8**，就是改述时多写了一次这个词）——写成固定数字等于自造一条会漂的假判据。
> `--positive-control pkg_resources`（只存在于 `.venv` 的词）⇒ 命中 **0**、脚本按"正对照必须能开火"的规则
> 报错退出 **2**；同词去掉 `--exclude-dir=.venv` 直接跑 grep ⇒ 命中 **61** ⇒ 三条排除项里
> `.venv` 确实生效且只作用到依赖目录，没顺手豁免任何我方产物目录。

> **排除项登记（2026-10-11 18:31 +0800 复算，owner 裁决随 `tasks-review-v1.0.0.md` 入库同笔）**：
> **审计类文档允许逐字引用禁用项作为探针，前提是文件名已登记在排除表。**
> 取证（三条都实跑过，非推断）：入库前该门禁读数 = **0**，入库后 = **1**；唯一命中经核为该报告
> **`:626`** 行——它逐字复制了上面的 `BAN='…'` 赋值本身（**探针自指**），不是误用了被否决的命名。
> **本排除的边界（护栏未被削弱的条件）**：排除的是**经登记的具体文件名**，不是文件名族——
> `--exclude=tasks-review-v1.0.0.md` 只放过这一份**命中内容已逐行核实过**的报告；**新增审计类报告
> MUST 走同一登记程序**（逐份登记 + 附命中内容核实 + 说明理由），未登记的同类新报告仍会被本门禁抓到。
> **收紧记录（2026-10-11 18:34 +0800，owner 裁"通配会让盲区累积"）**：本行原写 `--exclude='tasks-review-*.md'`，
> 通配等于把该文件名族的**所有未来报告**一并豁免（含可能出现实质误用的那份），故改为具体文件名。
> **反证机制仍在开火**（用既有文件测，不造含禁用名的文件）：本表自身命中 **10**、
> `constitution-review-v1.0.0.md` 命中 **2**、被排除的这份报告命中 **1**；去掉本排除项后门禁读数由 **0** 变 **1**
> ⇒ 归零来自登记，不来自扫描失效。
> **同类项已按 owner 裁决收紧（2026-10-11 18:38 +0800）**：`constitution-review` 的排除同样由通配改为
> **具体文件名**——本句原写 `--exclude='constitution-review-*.md'`（**仅作历史留痕，不在活命令里**），
> 现活命令为 `--exclude=constitution-review-v1.0.0.md`。理由与前一笔同：通配等于让该文件名的**所有未来报告**
> 自动豁免，盲区会累积。
> **登记取证**：该报告的命中经核为 **2 行**（`:64`、`:199`；含 4 个被禁 token），内容是复述本表禁用项以论证
> "该不该入宪"，属**审计类引文**而非误用（上一段反证所写"命中 2"与本句"2 行"是同一事实、同一 `grep -c` 口径）。
> **未登记的同类文件仍在扫描范围内**（逐份实跑，命中全 **0**）：`spec-review-v1.0.1.md`、
> `plan-review-v1.0.0.md` ~ `v1.0.2.md`、`tasks.md`、`spec.md`、`plan.md`、`data-model.md`、`quickstart.md`、
> `research.md`、`contracts/` 三件、`checklists/requirements.md`。

> 上表内容 MUST NOT 从本表删除：它是"经验证为错的值"的防回归清单，宪法原则级护栏直接指向它。
> 新增禁用项属本表扩充（PATCH）；删除禁用项使护栏失效 ⇒ 按宪法归类判据 MUST 升为 MINOR / MAJOR。

> **同类修口的第三、第四条：`.tsx` 进 include、`node_modules/` 与 `dist/` 进 exclude-dir**（2026-10-19 MVP-2 骨架笔，owner 第二步指令）。
> 起因是本仓要引入前端工程，而 include 面**原本没有 `.tsx`**：在 `%TEMP%` 造一棵含 `node_modules/pkg-0..199` 的探针 `web/`
> 跑 `scanned_rel_paths()`，磁盘 207 个文件里扫描面看见 **204 个，其中 200 条是 `node_modules/*/package.json`**
> （`package.json` 撞 `--include='*.json'`），而 `.tsx` / `.css` / `.html` **一个都不在面上** ⇒ 不补的后果是
> "第三方产物进面"与"我方前端源码不受控"**同时成立**，与 `.venv` / `.venv-linux` 那两条同类：**漏的是扫描面，不是命中数**。
> 取证照旧写成关系式：① `--show-spec` 的 `include` 项数 **7 → 8**、`exclude-dir` 项数 **4 → 6**；
> ② 主门禁读数不变（`main=0 env=0 dep=0 gate=PASS`）——今日仓内**还没有 `.tsx` 文件**，所以这条此刻只证明"面扩了、没造成假阳性"，
> 它要在前端落地那一笔之后才有完整鉴别力；③ 双载体仍同一张面（`test_two_carriers_parse_the_same_surface` 逐字同集）；
> ④ `.env*` 那一支复用同一张 `exclude-dir`（shell 与 Python 两侧都是），故 `node_modules` 里可能出现的 `.env*` 也不再进面。
> `dist/` 按**目录名逐字**排除（`--exclude-dir` 的语义 = basename 相等，不是路径前缀）⇒ 同名目录一并豁免，
> 这是刻意的：构建产物可再生、已由 `.gitignore` 拦在库外，与 `.venv` 同级处置。

> **同类修口的第五条：`results/` 进 exclude-dir**（2026-10-19 B 计划 第二步，owner 决定 2 同笔）。
> 起因与上面两条**方向相反**：前几条漏的是"第三方文件进面"，这一条多的是"**厂商交回来的自由文本进面**"。
> `python -m acp.realrun` 的产物落在仓库根下的 `results/<project_key>/`，里面是厂商响应的原样留档与
> `*-workspace/**` —— 那些字节不是我方产物、也不受我方命名纪律约束（型号名由厂商自己写），
> 而 Python 侧扫描器 `src/acp/guard/naming.py` 跟的是**工作树而不是 git 索引** ⇒ `.gitignore` 单独拦不住它。
> 取证的三条关系式（2026-10-19 实跑，不是推断）：① `--show-spec` 的 `exclude-dir` 项数 **6 → 7**；
> ② 主门禁读数不变（`bash scripts/guard_naming.sh` ⇒ `main=0 env=0 dep=0 gate=PASS`，`git ls-files` 里
> 今天**没有任何** `results/` 下的文件 ⇒ 这条此刻不减现有扫描面，它挡的是真跑之后落进工作树的那批字节）；
> ③ 活性对照：临时造 `results/_naming_probe/na.md`（内含一个被否决命名），同一条 grep 只换那一项排除，
> 带 `--exclude-dir=results` ⇒ 命中 **32**，不带 ⇒ **33**（差额恰为那条诱饵，其余 32 条全部来自本节与
> 审计类文档自身，由 `--exclude=` 那一份名单豁免）；诱饵用完即删，`results/` 目录本身也已不在树上。
> 边界与 `.venv` 那几条同规：排除的是**可再生、由 `.gitignore` 拦在库外的产物目录**，不是任何文档文件；
> 我方源码与文档里的禁用命名照旧计入。名字巧合要点名：文件总线的公共区里也有一个 `results/`
> （`bus/layout.py::RESULTS_DIR`，在存档根**里面**），这一条排除按 basename 逐字命中，两处一并豁免 ——
> 而公共区那些行本来就是 EN-06 实测值的投影，权威在库里，不在盘上（`contracts/file-bus.md §6`）。

---

## 8. 禁用依赖（依赖面静态扫描门禁输入，对应 FR-015 与宪法原则 I）

**为什么另开一节而不是并进 §7**：§7 是**命名**门禁（被否决的包名 / 导入名 / 环境变量名，
判据是"这些字符串不得再出现在我方产物里"），本节是**依赖面**门禁（判据是"清单与运行时解析出的
发行包名集合与禁用集合的交集为空"）。两者共用一条 grep 会把两种判据混成一个数，
而 §7 的说明明令"排除清单须与其说明一致"——它针对的是我方产物文本，不是依赖解析结果。

```bash
DEP_BAN='^(pyautogui|pynput|keyboard|mouse(_info)?|mouseinfo|pygetwindow|pywinauto|autopy|pyuserinput|robot[a-z0-9._-]*)$'
```

| 禁用依赖（发行包名形态） | 错误类型 | 正确替代 | 核实出处 |
|---|---|---|---|
| `pyautogui` | 屏幕点击式自动化主链路 | 各工具官方程序化入口（SDK / API） | FR-015、spec A-3、US-11 |
| `pynput` | 键鼠事件注入 | 同上 | FR-015 |
| `keyboard` | 全局键盘钩子 | 同上 | FR-015 |
| `mouse` / `mouseinfo` | 鼠标控制与屏幕坐标读取 | 同上 | FR-015 |
| `pygetwindow` | 窗口枚举与置顶（配合点击使用） | 同上 | FR-015 |
| `pywinauto` / `autopy` / `pyuserinput` | Windows 级 GUI 自动化 | 同上 | FR-015 |
| `robot` 起头的包（`robotframework` 等） | 以屏幕关键字驱动 UI 的框架 | 同上 | FR-015 |

**门禁用法**：由 `src/acp/guard/naming.py`（T028）执行，两面都查——
① **清单声明面** = `pyproject.toml` 的 `[project].dependencies` 与 `[project.optional-dependencies]`
（本仓库现无 lockfile，`find -name 'uv.lock'` = 0 ⇒ 传递闭包查不了，这条如实登记为残余）；
② **运行时解析面** = 已安装环境里每个 `import` 名反查 `.dist-info` 得到的发行包名。
只查 ① 的表现是"代码里 `import pyautogui` 但清单没写"——那既是依赖面的漏口也是运行时的漏口，
② 专门补它。命中数 MUST 为 **0**，判据写成关系式：`解析出的包名集合 ∩ 禁用集合 = ∅`。

> **owner 2026-10-12 裁决 5（名单归属定形）**：本节是禁用依赖名单的**唯一来源**，与 §7 禁用命名同一条程序
> ——"改清单不该改代码"。`src/acp/guard/naming.py::load_dependency_ban()` 只解析本节的 `DEP_BAN='…'`
> 赋值行，**代码里没有兜底默认值**：读不出那行就抛 `SpecError`（把"名单没读到"当成通过正是最要的假绿）。
> 这条口径由 `tests/guards/test_naming_guard.py::test_dependency_list_has_only_one_source` 双向钉：
> ① 门禁源码文本里禁用包名命中 **0**；② 删掉本节那行 ⇒ 加载器报错而非静默改用另一份。
> 因此**新增 / 删除禁用项只改本节**，不动 `src/`；反之若有人把名单抄进代码，该用例红。

> **本节的边界（不假装它证明了更多）**：本节列的是**已知实现名**，出处只能指到 FR-015 的
> 行为描述（规格文档里没有点名任何屏幕自动化库，实测 `grep -rni 'pyautogui' specs/` = 0）。
> 因此"MUST NOT 依赖屏幕点击式自动化"这条宪法级要求的**穷尽性不由本表保证**：
> 出现表外的新实现时，新增禁用项属本表扩充（PATCH），与 §7 同一条程序；
> 而"主链路是否真的只走程序化入口"的行为判据归 `tests/contract/test_all_adapters.py`
> 与 Phase 2 的适配器用例，不归本表。
> **与 EN-03 `form` 的关系**：`AgentForm` 里没有 `GUI` 取值（`IN_PLATFORM / SDK / API`，
> `data-model.md` EN-03 明写"GUI 仅作补位且本期不接"）⇒ 注册路径也接不住一个 GUI 形态的工具，
> 两处是同一道约束的两个面，不是两份事实。

## 9. Agent 能力档位与并发申报域（注册表校验的唯一事实源，T039 落地；对应 FR-011 / FR-012 与宪法「三」）

owner 2026-10-13 裁决 1 原话（逐字）：**"tier / cost_band / max_parallel 写入 params-ref §8"**，
理由三条 = "**跨 Agent 通用能力等级，散落 agents.yaml 会导致每个新 Agent 重新定标准**"、
"**唯一来源 = params-ref §8**"，并给了 `tier` 三档语义名、`cost_band` 三档、`max_parallel.default: 3`
与三条理由（"tier 用语义等级而非数字（避免 tier=1.5 这种无意义值）"、
"cost_band 与 §6 的三类计费口径呼应"、"max_parallel 默认 3（与宪法一致）"）。

**编号更正（不改语义，只改落点）**：本节编为 **§9 而不是 §8** —— 本文件**已有 §8**
（`## 8. 禁用依赖`，2026-10-11 起为依赖面门禁的清单源，实测 `grep -n '^## 8' config/params-ref.md`
命中那一行）。原话的"或并入 §7 之后"是同一意图的备选落点，而 §7 之后现已被 §8 占住 ⇒
按"新开一节、排在既有各节之后"执行，**语义与内容一字未减**。

```yaml
# 本围栏是 `src/acp/registry/loader.py` 的解析对象（同 §7 之于 scripts/guard_naming.sh、
# §8 之于 src/acp/guard/naming.py 的做法）：**本节是唯一事实源，改本节即改注册校验行为**。
# 注册表（`src/acp/registry/agents.yaml`）里 `tier` / `cost_band` MUST 写本表的**语义名**，
# 由 loader 映射成线上值后交给 `CapabilityManifest`；写线上值本身、写数字、写表外的名字 ⇒ 注册失败。
# 解析器只认这两个键：围栏里的键集 ≠ {tier, cost_band} ⇒ `RegistryError`（多一个键就是
# 一份没人解析的第二清单，它改了而行为不变，比缺键更难发现）。
tier:
  basic: 1          # 轻量任务
  standard: 2       # 标准任务
  advanced: 3       # 复杂任务
cost_band:
  cheap: CHEAP      # 免费 / 极低成本
  mid: MID          # 中等
  expensive: EXPENSIVE   # 贵档（需记账）
```

**`max_parallel` 不放进上面那个围栏**（它是本节唯一没有线上映射的申报项，理由要说清）：
解析器只消费 `tier` / `cost_band` 两个键，把 `max_parallel` 抄进围栏就等于往机器读的那份清单里
塞一个**没人解析的键** ⇒ 改它不红，那是最坏的一种死判据。所以它落在**本节正文**里，口径三句话：

1. **默认并发上限的数值载体是 `params-ref §3`**（= 本文件那节 `## 3. 并发默认值`，现读：并发上限默认 = **3**，
   且宪法「三」把它写成关系式"默认并发上限 = 首批接入 Agent 数"）。owner 原话"max_parallel 默认 3（与宪法一致）"里的
   那个 **3** 与 §3 是同一个数 ⇒ 本节**指过去**而不重述，否则一处改数另一处不变，正是他裁决 1
   第一条理由（"唯一来源"）要挡的事。**这是他给的写法的一处偏离，已在此点名**，不是漏做。

   owner 2026-10-13 追加：正文引用要有可复算锚点（"本节正文没有机器守卫"这句自陈就是他这条裁决要补的洞）⇒
   锚点就是上面那对反引号里的写法，**全文只许出现一次**（他原话里那两处 `§8` 是逐字引用、串不同，不冲突）：

   ```bash
   grep -c "params-ref [§]3" config/params-ref.md      # MUST = 1（0 ⇒ 指针被改写；2 ⇒ 抄了第二份）
   ```

   方括号是那类老写法：让**这条命令行自身不被它自己要数的那个串命中**，否则"命令写在文档里"这件事
   就会把读数从 1 顶成 2 ⇒ 文档给的数与真跑的数不符。同一关系落 fast 用例
   `test_section_9_points_at_section_3_for_the_concurrency_default`（按字面串数，全文件命中 **1**）⇒
   改掉那个指针是用例红，不是等人 grep。
2. **注册表侧没有默认值可套**：FR-011 把 `max_parallel` 列为**逐条必填**申报项 ⇒ 缺项即
   `RegistryError`，`agents.yaml` 里任何一条都不许"按默认 3"过关（默认值属运行时槽位侧，归 T047 的 SlotManager）。
3. **两个面各管一件事**：§3 的 3 是"平台同时最多跑几张卡"，`agents.yaml` 的逐条 `max_parallel` 是
   "这个 Agent 自己最多同时接几项"⇒ 不是同一个量，不能互相顶替。

**三条理由（owner 给的第一条已在上面逐字入账；下面两条是本表按他的措辞落下来后的口径说明）**：

1. **`tier` 用档位名而不是数字**：他的原话是"避免 `tier=1.5` 这种无意义值"。
   实现方式点名，免得读成"契约改了"：`contracts/agent-protocol.md §3` 与 EN-03 的
   `tier` **仍是 `int`**（`CapabilityManifest.tier: int`、`data-model.md:55` `| tier | int |`），
   本表把"合法档位"从"任意整数"收窄成**三档**，注册表侧只接受档位名 ⇒
   `tier=1.5` 与 `tier=4` 都进不来，而**跨层类型没动**（改类型要连契约 §3、EN-03 列、迁移、
   `CapabilityManifest` 与已入库的 66 条相关用例一起动，那属另一笔跨层改动，不在这里顺手做）。
2. **`cost_band` 与 §6 呼应**：§6 的三块计价形态（WorkBuddy 按坐席 + Credits、Qoder 按 Credits 或 BYOK、
   豆包按 token）是本表三档的排序依据。**但 `cost_band` 不是 `billing_basis`**
   （后者 = `TOKEN / SEAT_CREDIT / BYOK`，FR-061 要求三口径分列、不得互相加总），
   两轴各管一件事：`cost_band` 是注册表侧的**档位标签**（供选人），`billing_basis` 是记账侧的**口径标签**
   （供 EN-13 分列）。金额一律由 `cost/ledger.py`（T114）依 §6 换算，本表不带金额、也不带单位。
   本表列的值是**大写**（`CHEAP / MID / EXPENSIVE`），与契约 §3 的 enum、PG 类型 `cost_band_type`
   的取值逐字一致（owner 原话写的是小写；改成大写形态需要发迁移并动 `cost_band_type`，
   那属跨层改动 ⇒ 这里保留线上拼写，把小写作为**注册表侧的语义名**接进映射，两侧都取得到）。
3. **`max_parallel`**：见上面"不放进围栏"那三句 —— 数值留在 §3，申报义务留在 FR-011，
   本节钉的是"注册表必须逐条申报、不许靠默认值过关"这条，并且它**不由解析器消费**（这正是它不进围栏的原因）。

**消费方与自证口径**（与 §7 / §8 同一条程序，不另立规矩）：
解析器在 `src/acp/registry/loader.py`，**代码里没有内联的档位兜底清单**：本节或那个围栏读不出、
围栏里的键集不是 `{tier, cost_band}` ⇒ 一律抛 `RegistryError`（把"清单没读到"当成通过正是最要的假绿）。
双向自证落 `tests/contract/test_registry_loader.py`：① loader 源码文本里**两侧档位名**
（`basic` / `standard` / `advanced` 与 `cheap` / `mid` / `expensive`、`CHEAP` / `MID` / `EXPENSIVE`）
命中 **0**（名单只在本文，抄进代码就红）；
**这一条覆盖面有一处已知限制，写出来免得读成"代码里没有任何档位痕迹"**：`tier` 的线上值是**单个整数**，
而 `1` / `2` / `3` 在任何源码文本里都会以行号、`enumerate(..., 1)` 这类形态命中 ⇒ 按子串查它必然恒红，
一条永远过不去的判据和一条永远过得去的判据一样没用。所以"代码里没抄数字"由**行为**钉：
`test_injected_domain_proves_the_loader_reads_it_and_not_a_hardcoded_table` 换一份完全不同的注入域，
真注册表的原名必须被拒、合成名必须映射出合成值 ⇒ 名单若内联，这条就红。
② 删掉本节本围栏 ⇒ loader 报错，而不是静默改用另一份（§9 有六种读不出的形态，逐条参数化，
含"往围栏里加一个没人解析的键"那条）；
③ 正文那个 §3 指针由字面串锚定，全文件命中数 MUST = **1**（上面那条 grep 与同名 fast 用例是同一关系的两侧）
⇒ 这一条补掉了"正文不被解析 ⇒ 改了没人知道"的洞，但**只补了这一个锚点**：正文其余句子仍是纯文档，
没有机器守卫，别把这条读成"§9 正文全部可复算"。
⇒ **新增 / 删除档位只改本节，不动 `src/`**。

**表列检查器影响**：本节**不新增 markdown 表**（那张映射是 ```yaml 围栏，`scripts/check_tables.py` 只数
`|---|` 表列组），所以 params-ref 的组数读数与本笔之前一致；实测的"改前 / 改后"两个读数在
`tasks.md` 本轮申报里给出（params-ref 属 `[residual]` 分类，畸形不由本笔引入）。

## 10. 存档根与 `snapshot.ref` 的接口（T109 / T-4 定稿，对应 FR-053 / FR-054 与宪法「三」）

> T-4 的原话是"`snapshot.ref` 与具体存档机制的接口（D-12 只登记不实现）"。定稿要答的其实是两问：
> **句柄长什么样**（形态）与**那个对象住在哪一个仓库里**（根）。前一问在 T108 已经收成一个函数
> （`archive/cadence.py::snapshot_ref()`，全仓唯一拼装点），后一问此前**在全仓没有任何出处** ——
> 本节就是那个唯一出处。代码侧的解析入口只有一个：`src/acp/archive/root.py::require_archive_root()`。

```text
ACP_BUS_ROOT=AI项目台
ARCHIVE_ROOT=<bus_root>/<project_key>
SNAPSHOT_REF=git:<object-name>
OBJECT_NAME=^(?:[0-9a-f]{40}|[0-9a-f]{64})$
TEMPORARY_REF_PREFIX=local:
```

**逐键的载体与关系**（消费方 = `tests/guards/test_snapshot_ref.py` 第一、二节）：

- **`ACP_BUS_ROOT`** —— 总线根的环境变量名，默认值 `AI项目台`（相对进程工作目录，与
  `recover.py --bus-root` 的默认同形）。解析优先级：**显式 `bus_root` 参数 > 本变量 > 默认值**；
  空白值按"没设"处理。代码载体是 `archive/root.py::BUS_ROOT_ENV` 与 `DEFAULT_BUS_ROOT_NAME`，
  而后者**取自 `bus/layout.py::BUS_ROOT_NAME`**（契约 §1 那个目录名的唯一载体）⇒ 本节改默认值必须
  同时改契约 §1 与 `layout.py`，只改一处会让"总线"和"存档根"指向两个目录。
- **`ARCHIVE_ROOT=<bus_root>/<project_key>`** —— 存档根 = **总线项目目录**，每项目一个仓库。
  选它的理由不是"顺手"：`recover.py --from-file-bus <project_key>` 重建的就是这个目录，
  `assemble.py` 查交付包入口文件用的也是总线根 ⇒ **"revert 可复原"（宪法「三」）与"从文件总线重建三元组"
  （D-12 的第二条回退路径）作用在同一个目录上**。若存档根另指一处，两条回退路径就各自复原一半现场。
  这条拼接规则的载体是 `bus/layout.py::project_dir()`（含 `project_key` 的形态校验），
  `root.py` 调它而不是另写一份 `root / key`。
- **`SNAPSHOT_REF` / `OBJECT_NAME`** —— `ref` 列的形态 = `git:` + 一个**真实存在的 commit 对象名**
  （40 位 = SHA-1，或 64 位 = SHA-256，全小写十六进制）。对象名由
  `cadence.commit_archive_point()` 的第三步 `rev-parse HEAD` 回读，形状不过就不拼句柄。
- **`TEMPORARY_REF_PREFIX=local:`** —— T-15 裁 ① 那个"指向登记行自身"的临时形态。
  **本节与代码在这里必须同向**：`persistence/snapshots.py::record_point()` 拒绝以这个前缀起头的**新写入**
  （`ref_not_a_real_handle`），而 `require_archive_root()` 在解析不出可用根时**抛**而不是回退成临时句柄。
  两侧任何一侧放宽，都会让"`local:` 已经退役"这句话变成注释里的谎。库里既有的那一行**不改写**
  （抹平它就等于假装 T-15 那个时点没存在过；判据钉它的行数仍是 1）。
- **两条根的拒绝形状**（都是"执行之前拒，一条 git 都没跑"）：目录不存在 ⇒ `archive_root_missing`；
  `rev-parse --show-toplevel` 报的路径不等于这个目录 ⇒ `archive_root_not_toplevel`。后一道是安全边界，
  不是洁癖：总线目录如果恰好嵌在平台自己的仓库里，不加这道检查的 `add -A` + `commit`
  会把**平台仓库的整棵工作树**提交掉，而那一路退出码都是 0，事后只在别人的提交历史里看得见。
  用的子命令是允许名单里**已有**的 `rev-parse` ⇒ 这道自检不扩大 git 面。

**消费方式与自证口径**（与 §7 / §8 / §9 同一程序，但**有一处实质区别，写出来免得读成"代码读本文"**）：
本节由**判据**消费，**不由运行时消费** —— `root.py` 不解析这个围栏，它读的是环境变量与自己的常量。
两侧的绑定是行为式的，不是字符串相等：用例把**从本节读出的变量名**写进 `os.environ` 再调
`resolve_bus_root()`，取值必须跟着走；改名单一侧（只改本文，或只改 `BUS_ROOT_ENV`）其中一头必红。
`SNAPSHOT_REF` / `TEMPORARY_REF_PREFIX` 同理：临时仓库里真打一拍、读库里那一行，
句柄必须等于"本节的前缀 + 那个对象名"，且 `cat-file -e <对象名>` 查得出来。
⇒ **本节改口的义务与代码改口是同一笔**：`tasks.md` 的 T109 行下那句"同步 `data-model.md §4/§5` 的
'真实句柄'口径"就是这条义务的落点，关闭判据是关系式
`grep -c 'local:registry' src/acp/orchestrate/graph.py` 归 **0**（现读 **0**，2026-10-16 T109 落地后）。

**表列检查器影响**：本节**不新增 markdown 表**（清单用 ```text 围栏 + 逐键说明），
所以 params-ref 的表组读数与本节之前一致（`scripts/check_tables.py` 只数 `|---|` 表列组）。

---

## 11. 个人进化档案的载体与注入规则（MVP-2 第三步，owner 2026-10-19 指令；对应 FR-049 的看板面板）

> 这一节回答两问：**那两份档案住在哪一个目录里**（根），与**它们以什么形状进入每次外部执行**（注入规则）。
> 代码侧的解析入口只有一个：`src/acp/growth/store.py::growth_root()`；注入块的渲染只有 `render_block()`。
> 在仓内此前**没有任何一节**承载过"使用者本人的事实存哪儿"——原则 VI 那族（FR-061 使用画像 /
> FR-062 错误库 / FR-063 候选更新建议）在 MVP-1 是**零落点**（见 `tasks.md` 的 T130 未做清单），
> 所以本节是新地基而不是给既有条文找出处。

```text
ACP_GROWTH_ROOT=growth
GROWTH_FILES=my_profile.md,mistakes.md
GROWTH_INJECT=unconditional-full-text
GROWTH_MAX_ARCHIVE_BYTES=262144
GROWTH_WRITE_GATE=refuse-on-credential-form
```

**逐键的载体与关系**（消费方 = `tests/guards/test_growth_store.py` 与 `tests/contract/test_growth_injection.py`）：

- **`ACP_GROWTH_ROOT=growth`** —— 档案根的环境变量名，默认值 `growth`（相对仓库根，仓库根取
  `store.py` 的 `parents[3]`，口径同 `config/loader.py::REPO_ROOT`）。解析优先级：
  **显式 `root` 参数 > 本变量 > 默认目录名**；空白值按"没设"处理（同 §10 的 `ACP_BUS_ROOT` 那条）。
  两侧绑定是**行为式**的：用例把本节围栏里读出的变量名写进环境、指向一棵临时目录，
  `growth_root()` 与 `render_block()` 必须跟着走；只改本节或只改 `GROWTH_ROOT_ENV` 各有一头红。
- **`GROWTH_FILES`** —— 封闭集合，两个名字。写接口不接受第三个名字 ⇒ "读档案的接口"不会长成
  "读仓库里任意文件的接口"。这两份文件本身在 §7 的扫描面上（`*.md`），所以**使用者写进去的字也在
  命名护栏之内**：这是一条真实的相互作用，登记为待裁（owner 要不要给 `growth/` 开排除项，
  开了就等于给"被否决命名"多开一个可以自由写入的入口）。
- **`GROWTH_INJECT=unconditional-full-text`** —— 每次给 Agent 下发任务时，两份档案的**全文**进入报文，
  不抽样、不摘要、不截断、不因"这次任务看着不相关"而省略。三支 transport 的落点各有形状
  （qoder = stdin 那行 JSON 的 `message.content`、doubao = `messages[0].content`、
  workbuddy = `Objective` 字段），但顺序一致：**档案在任务面之前**。
  判据是跨支的同一条（`test_growth_injection.py` 参数化三支），逐支单测管不了"是不是同一个口径"。
  ⚠️ **限度**：owner 的原话是"注入到 System Prompt"，而这三支的报文面都**只有一个 user / 自由文本槽**，
  没有 `role=system` 这一档（豆包那支的甲/乙档实测发的就是 `messages:[{role:user}]`）。
  所以本节把它定形为"每次下发都无条件带上的这段前缀"，**不假装它等价于 system 角色**；
  要不要为豆包开 `role=system` 是另一件事（未验假定，开了就没有对照方），待裁。
- **`GROWTH_MAX_ARCHIVE_BYTES=262144`（256 KiB）** —— 单份档案的**写入**上界。它不是"注入时截到这么多"：
  超出就拒写，因为丢字如果发生在报文里，没人能发现；发生在写的时候，使用者立刻看得见。
  取这个数是为了让"整段错题本进上下文"在真模型侧还有意义，它随模型窗口变化 ⇒ 属 PATCH。
- **`GROWTH_WRITE_GATE=refuse-on-credential-form`** —— 保存前过 `config/loader.py` 的凭据**形态**判定
  （`redaction_hits`，与 §7 / 宪法「二」同一支过滤器），命中就**拒写**。
  刻意不"洗掉再存"：那同时违背使用者的"不丢字"与宪法的"凭据不落文件"——前一条被偷偷破坏，
  后一条看起来被遵守了。判据用形态而**不内联任何凭据字面量**（口径同防泄漏那族护栏）。

**消费方式与自证口径**（与 §7 / §8 / §9 / §10 同一程序）：本节由**判据**消费，**不由运行时消费** ——
`store.py` 不解析本围栏，它读环境变量与自己的常量。两侧单独漂走必红（`test_growth_store.py` 第一节）。

**表列检查器影响**：本节同样**不新增 markdown 表** ⇒ `params-ref` 的表组读数不变。

---

## 12. 运行时可调参数（MVP-2 第三步 ② 的载体，对应 `tasks.md` 的参数面板那一格）

> 这一节回答的是 owner 那句"让我能随时调整"。**载体 = `config/runtime.yaml`**，加载器 =
> `src/acp/config/runtime.py::load_runtime_params()`，面板读写 = `api/web.py` 的 `GET/PUT /runtime`。
> 它与 §1 有一条直接关系：§1 那一格挂着 owner 2026-10-13 登记的三条"代码里有、配置里没有"的账，
> 其中 **`max_tokens` 的载体从此节起有了**（那三条里的 `timeout_seconds` 与 `reasoning_tokens` 仍在账上未动）。

```text
RUNTIME_FILE=config/runtime.yaml
RUNTIME_KEYS=max_tokens,fragment_merge_ms,auto_accept_low_risk,artifact_floors
RUNTIME_MIN_FRAGMENT_MERGE_MS=1
RUNTIME_MAX_TOKENS_BOUND=10000000
```

**逐键的载体与关系**（消费方 = `tests/guards/test_runtime_params.py`）：

- **`max_tokens`**（当前值 `null`）—— 回答长度上限。`null` 是**合法取值**而不是缺省填充：它的语义是
  "这个键根本不发"，与 `DoubaoApiSpec.max_tokens = None` 和那条"默认不发 `max_tokens`"的判据同形。
  读取点 = `src/acp/adapters/spec_factory.py::build_transport_spec()`（owner 2026-10-19 裁决 2 落的工厂，
  它把三支点汇成一份构造入参：本文件 §12 那份 yaml + 环境变量 `ACP_DOUBAO_MODEL` + 注册表 `agents.yaml`）。
  ⚠️ **限度（写出来免得读成"面板已经能管回答长度"）**：工厂今天**还没有生产调用点** ——
  复算 = `grep -rn "build_transport_spec" src/acp --include=*.py` 现读 **1** 处（就是定义那一行），
  CLI 真跑只有 `--transport stub`（`resolve_transport_flag()` 只放这一个值）。所以"改了这里就有线上效果"
  要等真跑那一发的 transport 构造点接到工厂，那一步登记为**待裁 W-8**。
  **旧读数按日期留着不改写**（笔 E~G，2026-10-19）：当时 `consumers: []`，
  复算 `grep -rn "DoubaoApiSpec(" src/acp --include=*.py` = **0** 处（构造全在 `tests/`）。
- **`fragment_merge_ms`**（当前值 `500`）—— 活动流 SSE 两次读库之间的等待，也就是"不刷新也能变"的
  延迟上界。这一条**有活消费者**（`api/web.py::stream_events` 不接显式 `poll_ms` 时走它），
  判据是两头的：改小改大必须能在流的行为上看出来，而 `consumers` 那一栏不许把它读成空。
  它与 SC-013 的关系由 `test_board_stream.py` 现读 `spec.md` 那个秒数来比，代码里不留第二份副本。
- **`auto_accept_low_risk`**（当前值 `null`）—— 低风险自动验收的总开关（2026-10-28 加）。
  三态而不是两态：`null` = 没人表过态、跟随部署期环境变量 `ACP_AUTO_ACCEPT_LOW_RISK`；
  `true`/`false` = 面板上那个胶囊按钮显式定过的值。读取点 =
  `src/acp/config/auto_approval.py::effective_switch()`（优先级只在那里实现一次），
  分类清单 = `config/auto_accept.yaml`（哪些产物后缀算低风险），消费方 =
  `src/acp/review/runner.py` 的 PASS 分支。
  ⚠️ **限度**：这个开关**只能缩小**机器代签的范围，不能扩大 —— 它开的只是"低风险 + 三岗互斥成立 +
  审查员 PASS"这三条同时成立时的自动验收；任一条不成立，卡照样停在 `REVIEWING` 等人签。
  它也不会让任何一张单绕过 `ApprovalGate`（FR-048 只允许 A 类免确认，这条边不动）。
- **`artifact_floors`**（2026-10-30 加）—— 产物门槛表：格式 → `[min_bytes, min_lines]`。
  六个格式（html/css/md/json/py/js）一个不许缺；读取点 =
  `src/acp/orchestrate/llm_split.py` 的拆单路径（PRD 投影与模型拆单共用同一份）。
  它取代了原先写死在 `llm_split.py` 的 `512/12` 与 `_FLOORS` 字面量；改完在下一次拆单时生效。
- **封闭集合与"加一个键要同时加一个消费者"**：`RUNTIME_KEYS` 两侧逐字比（本文与 `runtime.py`）。
  表外键 ⇒ 拒（`RuntimeParamsError`），理由与 `danger_list` 同一条：多出来的键没人解析，
  "参数写了而行为不变"比缺键更难发现。

**消费方式与自证口径**（与 §7 / §8 / §9 / §10 / §11 同一程序）：本节由**判据**消费，**不由运行时消费** ——
`runtime.py` 不解析本围栏，它读 `config/runtime.yaml` 与自己的常量。
`consumers` 那一栏是 AST 数出来的（只数"`load_runtime_params()` 的调用方且在同一函数里取了那个键"），
不是注释、也不是产出方口述。

**表列检查器影响**：本节同样**不新增 markdown 表** ⇒ `params-ref` 的表组读数不变。





## 13. 请求级模型路由 `model_routing`（路线 B，owner 2026-10-31 指令）

**与宪法「三」的关系**：三岗互斥是协议层约束，本节只定义"用户怎么点名谁坐哪一席"；
点名不改变互斥、自审禁令与总管白名单——那三条的唯一强制点仍是 `src/acp/guard/triple.py`
与 `src/acp/orchestrate/nodes/assign_guards.py`。

**形状的唯一载体**：`src/acp/routing.py`（本节的散文是说明，不是第二份取值域）。
`POST /requests` 的 `model_routing` 经 `api/requests.py::ModelRoutingBody` 做形状校验
（`extra=forbid`），随需求单落 EN-01 的 `model_routing` 列（迁移 `0013`，NULL = 老请求）。

- `executor.kind` 只接受两个值：`qoder`（默认；CLI 读写仓库那条路，老行为一字不改）
  与 `api`（纯 API 执行者）。`kind=api` 时 `vendor` 与 `model` **必填**，缺一即 422；
  `thinking` 可取 `off` / `low` / `medium` / `high`，缺省 `medium`（与设置页四席同档）。
- `executor` 整格缺失 ⇒ 不干预：执行者仍由 `SelectWorker` 的五段全序选出（qoder 是默认首选）。
- `reviewer` / `acceptor` 接受两种形态：**模型席位**（`vendor` + `model` + 可选
  `base_url` / `api_key` / `thinking`）或旧的 `agent_id`。模型席位由通用审查 / 验收 Runner 调用；
  `agent_id` 形态是当席候选池里的偏好（不在候选里时记 `routing_ignored`）。
- **三岗互斥的判定单位（路线 C，owner 2026-10-31）**：模型席位 = `vendor:model:key_fp`，
  其余席位 = `agent_id`；三者仍必须两两不同。同模型同 key 出现在两个席位会被 `assign_guards`
  拦下（`guard/triple.py` 的不变量没松，只换了单位；宪法原则 III 的措辞待 owner 同步升版）。
- **凭据**：`api_key` 随请求体进来、只留进程内存（`routing_vault`），落库与回读的 `model_routing`
  里只有 `key_fp`（sha256 前 10 位）；未知键仍 422。服务重启后 key 失效，该单需重新提交。
- **验收 Runner 的触发**：低风险自动验收开关打开时，验收模型给出 `ACCEPT` / `REJECT`
  （最多重试 2 次，仍不合规落 `NEED_HUMAN`）；开关关闭时仍由人工签。

**纯 API 执行者的截断零容忍（P0-1）**：`collect()` 剥 Markdown 围栏后 MUST 先过
`src/acp/content_gate.py::artifact_completeness()`；结构性不完整**或** `finish_reason=length`
⇒ **不写盘、交回零产物**，由产物门禁判 `ARTIFACT_INVALID`。一份写到一半的文件
不进交付包；判据是纯字符串的（运行段镜像里没有 node），任何机器可复算。

**消费方式**：运行时读 `acp/routing.py` 与 `api_exec` 执行适配器（`adapters/doubao/exec.py`）；
本节由判据（`tests/contract/test_model_routing.py`、`tests/contract/test_api_exec.py`）与
README 特性 06 对读。**本节不新增 markdown 表** ⇒ `params-ref` 的表组读数不变。
