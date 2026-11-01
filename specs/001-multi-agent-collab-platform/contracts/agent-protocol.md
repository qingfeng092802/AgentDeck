# Contract: 跨 Agent 协作协议（Agent Protocol）

**Owner**: 编排层 | **Consumers**: 所有适配器 | **Governance**: 宪法 IV（统一协议、无旁路、产物先校验）

规则：**跨 Agent 只允许下面三个数据结构**。编排层任何函数签名 MUST NOT 接受裸 dict / 裸 JSON /
自由文本作为任务或结果的载体（D-7）；形态差异只在 `adapters/<vendor>/transport.py` 内消化，
MUST NOT 出现在编排图中（无 `if vendor == ...` 分支）。
**结构强制点（T008）**：三个消息模型与三个引用类型一律继承 `StrictModel`
（`model_config = ConfigDict(extra="forbid")`，定义在 `src/acp/protocol/refs.py`）——
"无旁路"因此不是一句约定，多余的厂商字段在**构造期**就被拒（`test_extra_field_rejected` 三个模型各一例）。

---

## 1. `Task`（下发）

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `task_id` | str | ✓ | 需求单内唯一，形如 `T01` |
| `project_key` | str | ✓ | 项目子目录标识 |
| `assignee` | `AgentRoleRef` | ✓ | `agent_id` + `role_id` **复合定位**（FR-010，Q-5） |
| `objective` | str | ✓ | 目标（任务卡三行之一） |
| `inputs` | list[str] | ✓ | 输入与上下文引用（只给路径/内容，不给可写句柄） |
| `expected_output` | str | ✓ | 期望产物形态（三行之一） |
| `artifact_floor` | `ArtifactFloor` | ✓ | 见 §1.1，**由任务卡声明**，适配器不得自定 |
| `acceptance_criteria` | list[str] | ✓（且**非空**） | 审查逐条比对的唯一标准来源（FR-029）；空标准 ⇒ 审查无从比对 ⇒ 判据恒真 |
| `token_budget` | Decimal \| null | ✓（键必在；值非 null 时 **> 0**） | 任务级用量上限（FR-060）。owner 2026-10-12 裁决 3：0 **不表示"无预算"**，0 与负数在构造期拒；"无预算"用 **null** 表达（同一裁决明写"不新增标志字段"，nullable 是原生语义）。T-13 由此闭合，DB 侧同口径落在 EN-02 的 CHECK ⇒ 本表的"✓"指**键必须存在**（缺键 = 消息不合法，不许省略来表达"没预算"），与值可为 null 不冲突 |
| `title` | str | ✓ | **FR-004 四项必填之一**（本轮补入：原表只有 `objective`，而 T009 明写 `Task` 的四项必填） |
| `difficulty` | int | ✓ | FR-004 之一；与 `CapabilityManifest.tier` 配对比较（`plan.md` 设计项 8 第二排序键） |
| `skill_tags` | list[str] | ✓（且**非空**） | FR-004 之一；`plan.md` 设计项 8 的 `skill_fit` 拿它当**分母**（交集大小除以本集合大小），空集合即除零 ⇒ MUST 在构造期拦 |
| `depends_on` | list[str] | ✓ | FR-004 之一；可为空列表。**禁环与禁悬空引用不在本契约**：那是跨任务性质，归 EN-02 入库前的 DAG 校验（FR-005 / T025） |
| `deadline_minutes` | int \| None | — | 时限 |
| `constraints` | list[str] | — | 附加约束 |
| `dev_model` | str \| None | — | **开发期扩展位**，默认 None；MUST NOT 参与选人、把关、成本判定 |

> **本表的实现落点（T008 / T009 / T012）**：`src/acp/protocol/messages.py::Task`。
> 本轮补入 `title` / `difficulty` / `skill_tags` / `depends_on` 四行后，**表与代码同口径**；
> 复算（2026-10-12 00:2x 实跑）：`get_args`-型对齐由 `tests/contract/test_protocol.py::
> test_task_required_set_matches_declaration` 钉住，`Task` 的必填项数 = **13**。

### 1.1 `ArtifactFloor`
| 字段 | 类型 | 约束 |
|---|---|---|
| `min_bytes` | int | > 0 |
| `min_lines` | int | > 0 |
| `format` | str | 取值域 = `html / css / js / md / json / py`（**已定死**，见下） |

三项下限的实现落点 = `src/acp/protocol/messages.py::ArtifactFloor`：任一项缺失或 ≤ 0 ⇒ **Task 构建失败**
（不许"先发再补下限"，否则 FR-017 无法执行）；`min_bytes` 与 `min_lines`
用 `Field(gt=0)`，`format` 用 `Literal`，故"缺失"和"≤ 0"都在**构造期**失败，不需要下游再判一次。
`data-model.md §5` 的 **T-1** 与 **T-5** 第三项（`artifact.format` 取值域）本轮一并闭合，代码侧常量在
`src/acp/protocol/refs.py::ArtifactFormat`，EN-02 / EN-06 与 1.3 的 PG 枚举 MUST 引用它，不再各写一份字面量。

**2026-10-29 扩到六值（owner 裁决，第十一轮）**：加 `js`。此前的五值把"样式 / 脚本 / 页面"里的脚本那一层
关在门外，而页面卡写一支 `.js` 是常态 —— 2026-10-28 的真跑里 T02 交了 `cart.js`，
`bus/gateway.py::format_of()` 判 `unsupported_format`，**那次写入整件**被拒、一张卡直接跑砸。
`js` MUST 追加在取值域**末尾**（PG 的 `ALTER TYPE … ADD VALUE` 只能追加到末尾，而
`pg_enum(...).enums` 与 `values_of(...)` 的顺序被 `tests/contract/test_states.py` 逐位比对）。
配套三处同批跟上：`prompts.ROLE_BY_FORMAT["js"] = "frontend"`、两张 `_FLOORS` 补 `js`、
`checks.service.scan_security` 的后缀白名单补 `.js`（放进来一个可执行格式却不扫它，等于多一类没人看的代码）。
`js` **不进** `config/auto_accept.yaml`：那份清单管"哪些卡不用人签字"，而 js 会执行，口径与 `py` 同档。

### 1.2 `PATCH /tasks/{task_id}` —— 下发前调整（FR-006｜T035，**T-8 由此闭合**）

这是"预览并调整"的那**一个**端点：看板与 CLI 都调它，同一张卡不存在第二条改法
（与 `approval-api.md` 的"双通道同一张单"同一条纪律）。它改的是 **EN-02 的列**，不是 §1 的 `Task` 消息——
消息在 `DispatchTask` 时才组装，届时读的就是这些列，所以本节的白名单与 §1 的字段表不需要同步改。

**白名单（强制点只有一个 = `src/acp/api/tasks.py::TaskPatch` 的 `extra="forbid"`）**。
下面两个代码块是**机器读取的**，竖线分隔、按字母序：

可改（7 项）：

```text
acceptance_criteria | deadline_minutes | depends_on | difficulty | skill_tags | title | token_budget
```

不可改（传了即 422，错误体点名那个键）：

```text
acceptor_agent_id | artifact_floor_format | artifact_floor_min_bytes | artifact_floor_min_lines | executor_agent_id | executor_role_id | handoff_count | id | reviewer_agent_id | review_round | review_standard_locked | rework_count | snapshot_id | status
```

> 漂移判据 = `tests/integration/test_task_patch.py`：可改集合与代码侧 `ADJUSTABLE_FIELDS` **相等**；
> 不可改集合与可改集合**不相交**、每一项都是 EN-02 的真实列、且逐项真的被 422 拒掉；
> **两张围栏加起来 MUST 覆盖 EN-02 的每一列**（唯一例外是上面第 1 条那个寻址用的 `feature_request_id`）。
> 最后这条是迁移 `0007` 加 `rework_count` 时补的：在那之前判据只查"点名了不存在的列"这一个方向，
> **新加一列而两张围栏都不点它，判据不会红** —— 那是"没写进白名单"与"写不进白名单"读起来一样的形状。
> `ADJUSTABLE_FIELDS` 是从 `TaskPatch` 的字段表**推导**出来的（减去两个非列字段），不是第二份手抄清单。

**两处对本节读起来会绊一下的事，明写在此**：

1. `feature_request_id` 按 owner 决策 1 属"不可改"，但它**必须出现在请求体里** —— 它是寻址的一半
   （EN-02 主键是复合的 `(feature_request_id, id)`，裁决 2），表达"在哪张需求单下找这张卡"，
   不是"把这行的这列改成 X"。所以它不进上面那份拒绝清单；两者的区别由
   `test_feature_request_id_addresses_but_never_rewrites` 钉住。
   **owner 2026-10-12 决策 3 已认可这条例外**（原话："不是'改写'，是'定位'"），并认可上面那条
   `acceptance_map_without_criteria` 保留（决策 2："与 `acceptance_map_not_configured` 同一类 ——
   映射与判据不一致；FR-029 同一件事；多一个错误码成本极低，语义更精确"）。
2. 决策 1 原文写"三个指派列"，EN-02 上实际是四列（三岗的 `executor_agent_id` / `reviewer_agent_id` /
   `acceptor_agent_id` 加岗位标识 `executor_role_id`），本节按四列展开；`review_standard_locked` /
   `handoff_count` / `review_round` 是决策 1 没点名、但显然不在"下发前调整"范围内的三列，一并列入。
   **owner 同日补充认可这两条展开**（"实际是四列（含 `executor_role_id`）"、"三列一并不可改"、
   "上述登记进 §1.2 与 T-8 闭合段"）。
   **2026-10-14 待裁 ⑬ 之后这条列表是四列**：迁移 `0007` 给 EN-02 加了 `rework_count`，它同样不在
   "下发前调整"的范围内（写者唯一 = `mark_rework()`），本节 fence 与 T-8 同步点名，代码侧无需改动 ——
   白名单是从 `TaskPatch` 的字段表推导的，没声明的列本来就 422。
3. **指派列不通过本端点改**（owner 2026-10-12 决策 1："给使用者开'直接改派'会绕过三岗互斥和选人评分"）。
   人工覆盖指派另有**独立端点**（`tasks.md` 的 **T071**，FR-024｜M1）——owner 同日 19:5x 裁决：
   "保留 T071 作为独立端点，不并入 PATCH"，且它 MUST 比本端点更严（过 `guard/triple.py`、过选人判据、
   `event_log(kind=SELECTION)` + `overridden = true`）。**本节的白名单不因它而放宽**：
   四个指派列与 `artifact_floor_*` / `review_standard_locked` / `handoff_count` / `review_round`
   在 `PATCH /tasks/{task_id}` 上始终不可传；T071 落地时的路径与请求/响应契约**另起一节**（现为下方 §1.3）回填本文件，
   不共用本节的错误码与门禁语义。

**门禁（owner 2026-10-12 决策 1 的收紧）**：`status` 必须是 `DRAFT` 才允许 PATCH；进入 `READY` 或之后
**任何**字段都拒 ⇒ `409 task_not_adjustable`。理由归 owner：FR-006 明文"下发**前**预览并调整"，
下发之后再改会让已经跑出去的状态迁移与依赖关系错位。

**请求体**：`feature_request_id`（必填 UUID）+ 上面 7 项的任意非空子集（没传的列一律不动）+
可选 `acceptance_links`（不是 EN-02 的列，是决策 2 要求随批申报的 EN-07 映射行，见下）。
**"没传"与"传了 null"是两件事**：`token_budget: null` = 改成无预算（裁决 3 的 null 语义），
不传 = 保持原值；对 NOT NULL 列显式传 null ⇒ `422 null_not_allowed`
（可空的只有 `token_budget` 与 `deadline_minutes` 两列）。

**改了判据 MUST 同批重申报映射（决策 2，取 A「重算」+ 强约束）**：`acceptance_criteria` 一旦出现在请求里，
`acceptance_links` MUST **非空**地覆盖该卡改后的每条判据，否则 `422 acceptance_map_not_configured`
（不带这个键与带 `[]` 同判——两者都是"没有申报"）；**不存在"改成功、映射为空"的中间态**。
申报的行按 EN-07 的形状判据校验（`orchestrate/nodes/split.py::validate_links`，FR-007 四条：
每条判据恰覆盖一次、不许出现别的卡的卡号、不许引入卡上没写的标准），通过后**整批替换**这张卡的 EN-07 行
（同事务内先删后插），其余卡的行不动。反向也拦：判据未改而申报映射 ⇒ `422 acceptance_map_without_criteria`
（落库的话就是往卡上塞它没写过的标准，与 FR-029 同一件事）。

**改后 MUST 重跑 `Normalize`（决策 3）**：同一事务内对**该需求单下这批卡改后的依赖集合**跑
`orchestrate/nodes/normalize.py::normalize`；非法 ⇒ `422 dependency_invalid`。
**不把整张图重跑**：那会重新调用补全器与拆单器 = 同一需求第二次记模型账（FR-059 / FR-060），
还会撞出"卡该不该重新建"这个本不存在的问题。

**原子性**：先全部校验、后全部写。上面任何一个 422 路径上 EN-02 与 EN-07 都**一行未动**，
而这不是靠回滚兜住的——端点里没有 `commit`（提交点归装配方 T123），绊线见
`test_the_patch_neither_commits_nor_closes_the_borrowed_session`。

**响应**（200，`TaskPatchResponse`）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `task_id`、`feature_request_id` | str、UUID | 复合主键那一对，原样回显 |
| `status` | str | 卡态；能走到 200 ⇒ 必为 `DRAFT` |
| `request_status` | str | EN-01 的状态，**从需求单行读**，不在卡上复述（原则 I） |
| `adjusted` | list[str] | 本轮真正写入的列名（字母序） |
| `depends_on`、`acceptance_criteria` | list[str] | 改后的值 |
| `acceptance_map_rows`、`acceptance_map_rows_replaced` | int、int | 写完后库里这张卡的 EN-07 行数、被替换掉的旧行数（**从库里数**，不是"打算写几行"的意图数） |
| `card_order` | list[str] | 改后整批卡的拓扑序（决策 3 的结果）——看板据此重画依赖图，不必再问一次模型 |

**错误码**（命名风格与 `approval-api.md §7` 一致：小写下划线码 + 说明）：

| 状态码 | 码 | 触发 |
|---|---|---|
| 404 | `task_not_found` | `(feature_request_id, task_id)` 在 EN-02 里没有对应行 |
| 409 | `task_not_adjustable` | 卡态不是 `DRAFT` |
| 422 | `extra_forbidden`（pydantic 原生） | 传了不可改或未声明的键，错误体点名该键 |
| 422 | `no_field_adjusted` | 请求体里一个可改字段都没有 |
| 422 | `null_not_allowed` | 对 NOT NULL 列显式传 null |
| 422 | `task_shape_invalid` | FR-004 单卡形状不成立（空白标题 / 空 `skill_tags` / 空判据 / 引用项空白或重复） |
| 422 | `dependency_invalid` | FR-005：悬空引用或成环 |
| 422 | `acceptance_map_not_configured` | 改了判据但没申报映射 |
| 422 | `acceptance_map_invalid` | 申报的映射覆盖不足 / 重复 / 卡号不属于本卡 / 引入卡外判据 |
| 422 | `acceptance_map_without_criteria` | 判据未改却申报了映射 |
| 503 | `store_not_configured` | 装配方未注入 `app.state.session_factory` |

### 1.3 `POST /tasks/{task_id}/worker` —— 人工覆盖执行者（T071 定稿，FR-024｜M1）

§1.2 第 3 条留的"另起一节回填"就是本节。**本节不复用 §1.2 的门禁语义**：`PATCH` 管下发前调整形状，
这里管"谁来做"，两者对同一张卡的字段集**互不相交**（`executor_agent_id` 在 §1.2 是不可改列，
它唯一的写者是选人路径 —— 依据 `data-model.md:34`"可空至 `SelectWorker` 填充"）。

```jsonc
// 请求
{ "feature_request_id": "…",   // 寻址的一半（EN-02 复合主键），不是"把这列改成 X"
  "agent_id": "adapter:…",     // 覆盖目标
  "operator": "user:…" }       // 必填非空：FR-051 四要素里的"谁做的"
// 响应 200
{ "task_id": "T01", "feature_request_id": "…", "executor_agent_id": "adapter:…",
  "auto_selected": "adapter:…", "overridden": true, "keys": […], "skipped": […] }
```

**三道检查按这个顺序，不可重排**：资格 → 自审 → 可闭合。反过来会把"根本没资格的人"送进闭合判定，
报出来的理由是误导（这是"必须比 PATCH 更严"那条裁决的具体形态）。

| 状态 | code | 触发 |
|---|---|---|
| 409 | `not_assignable` | 卡态不在 `{DRAFT, PENDING_APPROVAL, READY}` —— 执行中换人走 FR-026 的 handoff（T081），不是本端点 |
| 404 | `task_not_found` | 该需求单下没有这张卡（两列一起比，卡号只在需求单内唯一） |
| 422 | `override_not_eligible` | 覆盖目标本轮不可参选：`current_load ≥ max_parallel`（错误体点名是哪一根数）或未注册 / 已停用 / 缺能力申报 |
| 422 | `self_review_forbidden` | 覆盖目标已是这张卡的 `reviewer` 或 `acceptor` —— 指成执行者会让同一支工具自审（FR-034） |
| 422 | `guard_not_closable` | 换成这个执行者后三岗矩阵填不满（`orchestrate/nodes/assign_guards.py` 的存在性判定） |
| 422 | `guard_violation` | 这张卡的 `skill_tags` 为空 ⇒ 键①分母为 0，选人无法成立（MVP-1 的 B 类是结构预留，先响亮失败） |
| 422 | `extra_forbidden` | 传了上面三个键之外的键 |
| 503 | `store_not_configured` | 装配方未注入 `app.state.session_factory` |

**三条 MUST 的落点**（owner 2026-10-12 裁决四条原文里的后三条）：
①**走选人判据** = `override_not_eligible` 那一道，覆盖的是排序结果的**选择权**，不是资格；
②**过 `guard/triple.py`** = 本轮过的是 `assign_guards.SELF_REVIEW_FORBIDDEN` + `assert_closable`
这两处**现有实现**，权威断言随 T085（Phase 7）落地后**改调用点而不改判据**（owner 2026-10-14 裁决
"复用现有单点 + 登记迁移"）；
③**留痕** = EN-14 `event_log(kind=SELECTION)` 且 `payload.overridden = true`，
写入点与自动选人共用 `select_worker.apply_selection()` 这**一处** ⇒ 不会出现"自动选人记了键序、
人工覆盖只记了个名字"的两套口径。`payload.auto_selected` 留住被推翻的原首位，这是"覆盖了什么"的唯一载体。

**本节证明不到的一半，如实写明**：库里现在没有 `role_spec` 行（岗位说明正文归 T087，Phase 7），
所以真跑这条端点会得 `422 guard_not_closable`。方向在保守侧（宁可拒，不放行一个凑不满三岗的改派），
但它确实是"MVP-1 现阶段这条端点在产品路径上还不通"，判据靠自己在夹具里种三行 `role_spec`。
另：`ASSIGNABLE_STATES` 含 `DRAFT` 是**放宽**（文档只说"覆盖自动选择"，没说必须算过之后才许覆盖），
收紧只需从集合里去掉一个串 —— 登记在 `evidence/phase-05.md §T071` 等 owner。

### 1.4 `POST /tasks/{task_id}/escalation` —— 升级态的人工出口（T093｜待裁 ⑫ owner 2026-10-14 定为独立路由）

**为什么又是一条独立路由**（owner 原话："escalation.py 已做，与 PATCH 的语义不同"）：与 §1.2 的三处不同
逐条写在这里，以免后来人把"看起来都是改卡"读成可以合并 ——

| | §1.2 `PATCH /tasks/{task_id}` | 本节 `POST …/escalation` |
|---|---|---|
| 入口态 | `DRAFT`（下发**前**） | `ESCALATED`（升级**后**） |
| 改什么 | 七列白名单字段 | **只改 `status` 一列**，而它在 §1.2 的不可改清单里 |
| 主语 | 可以是系统 | MUST 是人（`principal` 非空且 ≠ 自动主体 `orchestrator`） |

请求体：`{feature_request_id, decision, principal, reason?}`（`extra="forbid"`）。
**`decision` 的合法取值不在本文也不在代码里写死**：权威是 `data-model.md §2` 那条边的箭头右侧，
运行时由 `orchestrate/nodes/escalation.py::exit_targets_in_doc()` 从原文解析 ——
本契约若与那句解析结果不一致，红的是判据，不是这里的手抄份（"域值只引用不重述"）。

状态码映射（稳定键在 `escalation.EscalationMisuse.code`，端点不重判一遍）：
`404 card_not_found` / `409 state_not_escalated` / `422 exit_not_allowed` / `422 principal_required` /
`422 human_principal_required`（本端点自己那一道：自动主体不许签成人工决定）/
其余（`doc_anchor_ambiguous`、`doc_anchor_unparsed`）= `500` —— 那两条意味着**状态机文档读不出唯一那条边**，
属服务端自身失配，不许伪装成使用者的输入错。

响应带 `handoff_count` 与 `rework_count` 两个计数：这条边只动 `status`，所以 200 响应里这两个数
必须与进来时一致 —— 把"我没顺手改别的列"做成可核对的面，而不是注释里的一句承诺。

**本节证明到的一半与证明不到的一半**：落库与审计（EN-14 `kind=TASK`、`action=human_exit`、
`actor_agent_id = principal`）有判据；**提交边界没有** —— 与 §1.2 / §1.3 同一口径，端点只 `flush()`，
`commit` 归调用方的 UoW（T123 那笔用 `scoped_session` 按请求收尾）。所以"人工决定已经落库"这件事
在 MVP-1 只能是段内成立，端到端未量，登记在 `evidence/phase-07.md` 未闭清单。

### 1.5 `PATCH /autonomy` —— 档位的改档面（T103｜owner 2026-10-15 裁决 ⑥：并入本契约，不新建文件）

**为什么写在这里而不是 `contracts/autonomy-api.md`**（owner 原话："agent-protocol.md 已承载 §1.4 的人工出口；
autonomy 是同一域的扩展"）⇒ 不新建文件、不占任务号。与 §1.4 同域的那一条是**主语 MUST 是人**：
§1.4 挡的是"自动主体把升级态的人工决定签出去"，本节挡的是"自动主体把档位从 SAFE 切到 FULL"（FR-055）。

| 方法 | 路径 | 用途 |
|---|---|---|
| PATCH | `/autonomy` | 切换 EN-16 的 `mode`（只有 `FULL / SAFE` 两档，宪法「三」） |

**只有这一个动作**：读档面（`GET`）未开 —— `tasks.md` 的 T103 行只写"改档端点 + 403 拦截"，
而档位在使用者侧的呈现归看板（R-13）与 CLI（T106 的 `python -m acp.autonomy`）。
先在这里开一条 `GET`，就会多出一条"谁都能读到当前档位"的读侧契约，而那份契约 owner 还没写。

**文本通道不是第二个契约**（T106 落地后回填，owner 2026-10-15 约束 5："CLI 与看板 / HTTP 调同一端点，
不另开逻辑"）：`python -m acp.autonomy set safe --project <项目> --principal <使用者>` 就是本节这条
PATCH 的另一个**调用方** —— 它自己 MUST NOT 判主体、判档位、写 EN-16，仓内的谓词、状态码表、
fail-closed 注入项各仍只有一处成文。因此本契约对它只提三条可读事实：
① 请求形状与上面完全一致（体两字段、头同一个 `X-ACP-Principal`）；
② `--as-agent <agent_id>` 是本节的**负向探针**：它把一个机器身份放进那个头，期望的就是上面那行 403
（判据在 `tests/integration/test_autonomy_cli.py`，与 T103 的 `test_autonomy_api.py` 各钉一侧）；
③ 退出码 0 / 1 / 2 / 3 分别对应"服务端成 / 服务端拒 / 本地用法错 / 连不上"，
其中"本地用法错"只有身份那一格（两者都不给或都给）—— **档位词不在本地判**，第三档由域答 422。
清单侧的两个入口（`python -m acp.danger add --pattern`、`python -m acp.selfcheck --show-danger`）
不属于本契约的消息面，成文在 `config/danger_list.yaml` 头部与 `quickstart.md §5`。

请求体：`{project_key, mode}`，`extra="forbid"` ⇒ 多余字段 422。
**`mode` 在契约里是裸 `str`，MUST NOT 写成枚举字面量集合**：域值的唯一载体是 `persistence/enums.py` 的
`autonomy_mode`（`data-model.md` EN-16 的 `mode` 行只引用不重述）。在请求模型里写死两档就是造第二个载体，
而且 pydantic 会抢在仓储之前 422，响应体里就没有 `mode_not_allowed` 这个 code 了 ——
裁决 1 给这个 code 定 422 要的是"值不合法由域来说"，而不是由某个模型的字段类型来说。
**同为 422 的两条要分得开**：pydantic 那条（多余字段、`project_key` 空串）响应体的 `detail` 是**结构化列表**，
本表那三条是 `code：消息` 的**字符串** ⇒ 使用者据此能分辨"平台没听懂我的请求"与"平台听懂了但拒绝"。

请求头：`X-ACP-Principal`（头名的唯一载体 = `api/approvals.py::PRINCIPAL_HEADER`，与审批面同一个头、
同一个 `principal_not_allowed`）。缺头 ⇒ 403，**不降级成"匿名改档"**。

响应：EN-16 的六列一一映出（`project_key` / `mode` / `danger_list_version` / `updated_by` / `updated_at` /
`last_intercept_at`），一个都不折 —— 使用者改完要能当场核对"我这一动手动了哪几列"。

状态码映射（**表成文在 `persistence/autonomy.py::HTTP_STATUS`**，端点只做翻译，不在 HTTP 层重判一遍 —— 原则 I）：

| 状态 | 稳定键 | 什么时候 |
|---|---|---|
| 403 | `principal_not_allowed` | 四类：`updated_by` 空白／它是 EN-03 注册过的 `agent_id`／它在装配点申报的自动主体集合里／它就是建档主体 `bootstrap` 却走改档路径 |
| 422 | `mode_not_allowed` | 第三档不存在（`FULL / SAFE` 之外）。大小写不对的串同样拒 —— 不做静默归一 |
| 400 | `danger_list_required` | 该项目还没有 EN-16 行 ⇒ 改档要建行，而 `danger_list_version` 是 NOT NULL，本模块不编指纹 |

谓词的唯一成文点是 `persistence/autonomy.py::_guard_updated_by`；本表只回答"拒了之后使用者看到什么码"。
判"是不是机器"这件事在仓储成文一次、端点翻译一次，反过来写就会出现"端点放行了、仓储还在拒"。

另有三个注入项归端点自己处理，前两个是 **fail-closed**（缺失即整条端点不可用，不读成"不限制"）：

| 状态 | 稳定键 | 装配方（T123）MUST 交什么 |
|---|---|---|
| 503 | `store_not_configured` | `app.state.session_factory` |
| 503 | `machine_principals_not_configured` | `app.state.machine_principals` = `{orchestrate/nodes/select_worker.py::AUTOMATED_ACTOR}`，**缺失与注入空集合都算没配** |
| — | （不拦） | `app.state.danger_list`：只在无行建行那一支用得上，缺它给 `None`，那一支由仓储抛 400 |

`machine_principals` 为什么是注入项而不是在本模块写死那个名字：它住在编排层，仓储反过来 import 就是向上成环；
而那条常量自己的注释写着"这是一条临时约定，不是一个已成立的身份" ⇒ 抄第二份必漂。
方向也不能反：这一项漏配的后果正是 FR-055 唯一要挡的那扇门（总管工具自己把档位切到 FULL），
所以宁可 503 也不把空集合读成"这个部署没有机器"。第三个注入项不给 503 是同一枚硬币的反面 ——
既有行分支用不上清单，把它变成整条端点的前置条件就会出现"档位明明能改，却因为一项没配而全拒"的过度失败。

**本节证明不到的一半，如实写明**（三条都在 `evidence/phase-08.md §六` 有对应未闭项）：
① 这条路由**还没有装配方**（`grep -rn "include_router" src/acp/` 只命中 `api/approvals.py` 里的一句注释），
装配归 T123；② 端点只 `flush()` 不 `commit()` ⇒ 真跑会回 200 而重启后档位回旧值，
提交边界与 §1.2／§1.3／§1.4 是同一条（Phase 7 的 A-1），也归 T123；
③ 改档**不往 EN-14 写事件** —— `event_log_kind` 域里没有 `MODE_CHANGE` 这个取值（`persistence/enums.py:59-69`），
要记就得改域 + 加迁移而 owner 未裁 ⇒ 本面的审计载体只有 EN-16 本身那三列。
另与审批面同条披露：MVP-1 没有认证，所以这里的 403 挡的是"披错身份误操作"，**挡不住冒充**
（一支工具填一个使用者名，`_guard_updated_by` 分不出来）。

判据落点 = `tests/integration/test_autonomy_api.py`（2026-10-15 04:4x 实跑 **19** 例全绿；需要 PG ——
`mode` 是 PG 原生枚举，假库里"第三档"反而会写成功）。复算入口
`PYTHONPATH=src .venv-linux/bin/python -m pytest -q tests/integration/test_autonomy_api.py`；
变异账 P8-T103-0..6 在 `evidence/phase-08.md §八`。

### 1.6 `POST /tasks/{task_id}/artifact-retry` —— 产物不达标的人工出口（2026-10-28 立）

**为什么必须有这一节**：`ARTIFACT_INVALID` 在 2026-10-28 之前是一条**真正的死路** ——
状态机给它写了出边（`data-model.md §2`：`ARTIFACT_INVALID ──▶ HANDED_OFF ──▶ RUNNING（换人≤1）`），
但那条出边只由 `orchestrate/nodes/handoff.py::handoff()` 实现，而 `handoff()` 在产品路径上**没有调用点**
（`realrun.py::_human_stop` 的 handoff 分支明写"一字不写"）。同时 `dispatch.DISPATCHABLE` 不含这一格
⇒ 「继续执行」推它不动、评审列表里没有它、「卡住了」那一栏只收 `ESCALATED`。
现场：需求单 `9b715923` 的 T03，执行器返回零产物（`examined: 0`），卡死 1 小时，唯一出路是手改库。

与 §1.4 的三处不同，逐条写在这里以免被读成"两条可以合并"——

| | §1.4 `POST …/escalation` | 本节 `POST …/artifact-retry` |
|---|---|---|
| 入口态 | `ESCALATED`（换人/工具超限、冲突自裁之后） | `ARTIFACT_INVALID`（门禁判产物不达标之后） |
| 下家 | `READY / REJECTED`（人决定还要不要做） | **只有一个**：`HANDED_OFF`（人决定"这张卡再跑一次"） |
| 换人 | 不动执行者 | 不动执行者，**也不动 `handoff_count`** |

请求体：`{feature_request_id, principal, reason?}`（`extra="forbid"`）。**没有 `decision`**：
这条边只有一个合法下家，取值由 `orchestrate/nodes/artifact_retry.py::exit_target_in_doc()` 从状态机原文解析 ——
让调用方去选一个只有一个选项的东西，等于把"文档说去哪"变成"调用方猜去哪"。
解析取那条线**第一个** `▶` 右侧的第一个态名：那条线后面还有第二个 `▶`（`HANDED_OFF ──▶ RUNNING`），
取最后一个会把 `RUNNING` 当成出口，而 `RUNNING` 与"占了槽"必须是同一件事（`dispatch.py` 模块头第 3 条）。

**为什么是"原地重跑"而不是"自动换人"**：`handoff()` 要候选集（EN-03 + EN-05 申报）与三岗席位，
那是**派发段**的依赖面；停点拿不到它们（"停点不碰 session"由
`tests/guards/test_realrun_driver.py::test_the_stop_nodes_structurally_cannot_write` 用哨兵会话钉住）。
更实际的一条：这张卡的两个把关位往往已经被另外两支工具占着（现场 T03：`executor=qoder` /
`reviewer=doubao` / `acceptor=workbuddy`），换谁都撞三岗互斥 ⇒ `handoff()` 会走"无可替代 ⇒ `ESCALATED`"，
等于让人多点一次才回到同一处。所以本节的语义是**执行者不变、次数不动**，只把卡挪到状态机写着的那一格。
`handoff_count` 计的是换人（`data-model.md` 规则区："返工计 `rework_count`，换人计 `handoff_count`"），
原地重跑没有换人；顺手 +1 就把 FR-027 那唯一一次换人额度用光了，而这次失败**根本没换过工具**。
想真换人走 `handoff()`（FR-026 自动路径）或 §1.3 的覆盖端点 —— 两条都不在本节。

状态码映射（稳定键在 `artifact_retry.ArtifactRetryMisuse.code`，端点只翻译、不重判 —— 原则 I）：
`404 card_not_found` / `409 state_not_artifact_invalid` / `422 principal_required` /
`422 executor_required`（执行者为空时**拒**：`HANDED_OFF` 之后唯一的下家是 `dispatch`，
而它在执行者为空时抛 `executor_missing` ⇒ 放行只是把死路搬进一个"看起来能跑、点下去才报错"的格子）/
`422 human_principal_required`（本端点自己那一道：自动主体不许给自己签"人已决定"的通行证，
与 §1.4 同一道门）/ `500 doc_anchor_ambiguous`、`500 doc_anchor_unparsed`（状态机文档读不出唯一那条边）。

响应带 `handoff_count` 与 `rework_count`：这条边只动 `status`，所以 200 响应里这两个数必须与进来时一致 ——
与 §1.4 同一条理由，把"我没顺手改别的列"做成可核对的面。

**本节证明不到的一半，如实写明**：① 这条路由只解决"人点得到"，**没有**让失败自动走完 ——
"执行器空交付 ⇒ 自动换人"仍是 `handoff()` 的职责，它今天仍无调用点；
② 提交边界与 §1.2 / §1.3 / §1.4 同一条（端点 `commit`，调用方 UoW 未接）；
③ 它把卡送到 `HANDED_OFF` 之后**还需要人再点一次「继续执行」**才真的重派 ——
这一步没有合成一个动作，因为"放回可派发态"与"立刻占用一个并发槽"是两件事，
合并会让一次点击在后台起一个几分钟的厂商调用，而接口只能回 202。

判据落点 = `tests/guards/test_artifact_retry.py`（13 例；PG 那几条在**独立测试库**上跑，
不碰产品库 —— `tests/conftest.py` 的 PG 档入口守卫要求十张业务表为空）。

## 2. `TaskResult`（回收）

| 字段 | 类型 | 语义 |
|---|---|---|
| `task_id` | str | |
| `self_reported_status` | enum(`success`,`failed`) | **仅是输入信号**，不决定最终态 |
| `artifacts` | list[`ArtifactRef`] | 每条含 `rel_path` / `bytes` / `lines` / `format`（实测值） |
| `log_excerpt` | str | 过程摘要，凭据经脱敏过滤 |
| `self_assessment` | str | 自评 |
| `issues` | list[str] | 遇到的问题 |
| `usage` | list[`UsageNotice`] | 用量通告，供 `cost_record` 落账（按 agent + task 两维） |

**实现落点（T008）**：`src/acp/protocol/messages.py::TaskResult`。必填 = `task_id` /
`self_reported_status` / `log_excerpt` / `self_assessment` 四项；`artifacts` / `issues` / `usage`
允许为空列表——§5 的"`success` + 空产物"用例要求它**构造得出来**，`invalid` 由门禁判（§2.2）。
`log_excerpt` 的**脱敏过滤器不在协议层**，属 T027（`src/acp/config/loader.py` + logging 侧）：
过滤器要同时作用于日志与产物两个面，只洗这一个字段会漏。
**T027 已于 2026-10-12 落地，但只落地了它该落的那两个面**：过滤器挂在
`bus/gateway.py::write_artifact`（产物面，全部写入方经此一道）与 `logging` 的 `secrets` 过滤器（日志面）；
**协议层不洗**（`TaskResult` 仍是裸模型，`extra="forbid"` 那条也不因此放松）。
`log_excerpt` 字符串本身的入库前清洗，接线点是 Phase 2 写 EN-14 `event_log.payload` 的那一步
（经同一个 `redact_bus_text`）⇒ **这一条现在未闭合**，MUST NOT 读成"消息字段已脱敏"。

**【Phase 3 收口补登，2026-10-13：三支适配器送进来的那份字符串，洗没洗过不一样】**
逐支读代码所得（不是真机观测），点名不外推：

| 适配器 | `log_excerpt` 的来源 | 经没经过网关那道洗 | 代码位置 |
|---|---|---|---|
| `qoder` | 厂商 stdout 经 `write_artifact` 落盘后**从盘上重读**再取末段 | **已洗**（落盘走网关，回收读的是那份盘上字节） | `adapters/qoder/transport.py:503-510`、`:704-725` |
| `workbuddy` | 厂商响应里的日志字段**直取** | **未洗** | `adapters/workbuddy/transport.py:219` |
| `doubao` | 模型原文的末段（`content[-400:]`） | **未洗**；同一串落进 `reviews/*.md` 的那一份是洗过的 ⇒ "盘上干净、回传面不干净"这一对在 API 形态同样成立 | `adapters/doubao/transport.py:233` |

⇒ §2 表里"凭据经脱敏过滤"那一列，**当下只对"经网关落盘的面"成立**，回传面上的三形态是 **1 洗 / 2 未洗**。
这不是"实现没做完"，而是 §2 那句话的适用范围一直没写清 ⇒ 收口给两条路等他定（我不自行择一，也不为凑判据改两支适配器）：
**(a)** 在协议侧加一条单点："构造 `TaskResult` 前 MUST 过 `redact_bus_text`" ⇒ 三支统一、口径最硬，代价是改两支适配器的回收面；
**(b)** 承认回传面就是原文，把 §2 那一列改成"**入库后**经脱敏过滤"，与本段合起来读才不撒谎。
真凭据冒烟时若厂商回显里出现账号 ID 那一类标识符（`tasks.md` 的 T042 申报里甲档那条 404 就是实例），
(a) 的优先级会立刻升上来 ⇒ 这一条挂在 Checkpoint 3，不在这里替他决。


### 2.1 `ArtifactRef`
`rel_path` MUST 落在该 Agent 自己的 `workspace_dir` 内；越界路径由文件网关直接拒收（FR-013）。
`bytes` / `lines` / `format` MUST 是**实测值**（网关读工作区得出），不是工具自报（FR-019）。

### 2.2 有效性门禁的契约位置
最终态由 `ValidateArtifact` 节点依据 §1.1 三项**覆写**为 `valid` / `invalid`：
`self_reported_status = success` 且任一下限不满足 ⇒ 记 `invalid` → 状态 `ARTIFACT_INVALID` → `HandOff`，
且**审查节点永不被调用**（FR-018 / SC-007）。适配器 MUST NOT 自行判定"达标"。
结构侧的保证：`TaskResult` 继承 `extra="forbid"`，因此**没有** `validated_status` 字段可写，
适配器自带结论会在构造期失败（`test_task_result_cannot_self_declare_validity`）。

### 2.3 `UsageNotice`（T010 补齐：此前只有名字、无字段）

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `usage_kind` | enum(`INPUT`,`OUTPUT`,`CALL`,`SEAT_DAY`) | ✓ | 与 EN-13 `usage_kind` 同一取值域 |
| `quantity` | Decimal | ✓ | 用量数值 |
| `unit` | str | ✓ | 计量单位 |
| `billing_basis` | enum(`TOKEN`,`SEAT_CREDIT`,`BYOK`) | ✓ | EN-13 / FR-061 的三口径 |
| `model_ref` | str \| None | — | 可来自 `dev_model`；EN-13 已钉它 MUST NOT 参与把关与选人 |
| `occurred_at` | datetime（带时区） | ✓ | 与 `task_card_id` + `agent_id` + `usage_kind` 合成 EN-13 的唯一索引四元 |

实现落点 `src/acp/protocol/refs.py::UsageNotice`。**刻意没有 `cost_amount` / `currency`**：
金额由 `cost/ledger.py`（T114，Phase 10）依 `config/params-ref.md §6` 的单价换算——
让工具自报金额等于把 FR-061 的口径分列交回报价方，与 FR-019「以实况而非自述」是同一条理由。
该缺席由 `test_usage_notice_carries_no_amount` 钉住（断言 `cost_amount` / `currency` / `price` / `usd`
四个名字不出现在 `UsageNotice` 上），字段全集由 `test_usage_notice_covers_en13_input_columns` 对齐 EN-13。

## 3. `CapabilityManifest`（能力申报）

| 字段 | 类型 | 语义 |
|---|---|---|
| `agent_id` | str | 三岗互斥判定单位 |
| `form` | enum(`IN_PLATFORM`,`SDK`,`API`) | G1 三类形态；CLI/GUI 本期不接 |
| `capabilities` | list[str] | 能做什么 |
| `tier` | int | 能力档次 |
| `cost_band` | enum(`CHEAP`,`MID`,`EXPENSIVE`) | 成本档位 |
| `max_parallel` | int | 可并行数 |
| `role_mounts` | list[str] | 该工具可承载的 `role_id` 集合 |

缺任一项 ⇒ 注册失败（FR-011 四项硬要）。实现落点 `src/acp/protocol/messages.py::CapabilityManifest`，
七个字段全部必填、无默认值；`capabilities` 非空、`max_parallel > 0`；
`form` 与 `cost_band` 用 `Literal` 拒未知值（含 `CLI` / `GUI`，本期不接）。

### 3.1 字段归属与冗余声明（T011 要求的结论）

| 契约字段 | 落库位置 | 是否冗余 | 生效口径 |
|---|---|---|---|
| `agent_id` | EN-03 主键 | 否（同一标识） | — |
| `form` / `tier` / `cost_band` / `max_parallel` | EN-03 同名列 | **同一份申报值的两个视图**，不是两笔录入 | EN-03 是生效快照；EN-05 是本结构的落库投影（带 `declared_at` 的申报历史） |
| `capabilities` | EN-05 主字段 | 否（EN-03 明令不存技能字段：契合是集合运算结果） | EN-05 |
| `role_mounts` | EN-04 `mounted_agents` | **结构上冗余**（同一关系的两个方向：工具→岗位 ⇄ 岗位→工具），两份都留 | **以 EN-04 为生效口径** |

`role_mounts` 的两份为什么要留、差异怎么处置（这是 T011 真正要的答案）：申报值来自**工具自己**
（它说能挂哪些岗），`mounted_agents` 是 **owner 批准后**的生效集合。二者不等 ⇒ 注册节点记
`GUARD_VIOLATION` 并**拒注册**，MUST NOT 静默取并集——取并集等于工具自己给自己挂牌。
强制点与用例：EN-04 与 EN-05 的落库在 Phase 1.3（T025），申报比对在 Phase 3 注册路径，
断言落 `tests/contract/test_all_adapters.py`（§5 最后一行）。
`Handle` 的定义见 §4；三个此前"只有名字无字段"的类型（`AgentRoleRef` / `UsageNotice` / `Handle`）
现均已定义，T-5 的第一项随之闭合。

## 4. `AgentAdapter` 接口（编排层唯一可见形状）

```python
class AgentAdapter(Protocol):
    agent_id: str
    def describe(self) -> CapabilityManifest: ...
    def submit(self, task: Task) -> Handle: ...
    def collect(self, handle: Handle) -> TaskResult: ...
    def cancel(self, handle: Handle) -> None: ...
```
- 无 `plan()/select()/route()` 之类方法：**调度与选人不属于适配器**（宪法 II）。
- 适配器 MUST NOT 持有除自身 workspace 外的写句柄（FR-013 / D-2）。
- **`submit()` 的载荷 MUST 含 `Task.acceptance_criteria` 的全量**（owner 2026-10-13 裁决 2，三支统一口径）：
  FR-029 的"审查 MUST 逐条比对"要求比对主体手里必须有那份逐条标准，而一支工具在某个任务上挂哪个岗是
  **任务级**的决定 ⇒ transport MUST NOT 在渲染时按岗位预先筛掉细则。判据 = `test_every_submit_carries_the_criteria`
  （跨三支跑；此前三支各钉各的方向，三份单测同时全绿而口径是三套 —— 缺的就是这条符合性断言）。
  连带后果点名：文件面的任务卡仍剥 frontmatter（`gateway.render_for_agent`），**消息面与文件面因此故意不一致**；
  文件面要不要一起放出来是另一个决定，不在本条替 owner 定。
- **只读按任务算，不按工具算**（owner 2026-10-13 裁决 1，FR-033）：同一支工具可以同时是某任务的执行者与
  另一任务的审查者 ⇒ "该 agent 对某路径只读"当且仅当它在**这个任务**上担的是审查/验收岗。
  上面那条"只许持有自身 workspace 的写句柄"仍按工具算，两条不冲突：那条划**能写到哪儿**，这条划**能不能写**。
  **现状与缺口点名**（写这条不等于它已生效）：强制点 `bus/acl.py::can_write` 判的正是"这次请求所披的身份"
  （`Actor` 携带 `role_id` 与 `read_only`，后者按注释取自 EN-04），所以它与任务级同形。
  实现已按本条收窄（`acl.py:160-166`，判据 = 路径的 `declared_owner(rel_path)` 是否等于 `actor.actor_id`）：
  只读身份写公共区、写别人 workspace 都拿 `read_only_violation`，写**自己那一份 workspace** 放行且第 4 条的
  任务级凭证照查 —— 三条用例分别钉住这三种结局（owner 2026-10-13 裁决 1 落地轮）。
  比较的是 `actor.actor_id`（生产形态 = `adapter:<agent_id>`）而不是 `capability.agent_id` ⇒
  用例若用裸 `agent_id` 造 `Actor`，"只读岗写自己 workspace 被拒"会因为名字不匹配而**假绿**；
  `test_bus_isolation.py` 的 `adapter()` 已改成生产形态，理由写在那三行注释里。
  **仍然开着的那半条**：`Actor.read_only` 在生产侧**还没有写者** —— **三支** transport 构造 `Actor` 时都走默认值 `False` 加
  `role_id=NO_ROLE_DECLARED`（`workbuddy:248` / `qoder:765` / `doubao:522`），把只读身份传进去的目前只有用例 ⇒
  "以只读身份发出的写请求被拒"这条链在现网仍跑不到，缺的一步在装配点（把 EN-04 该岗的 `read_only` 随下发身份带进 ACL），归 Phase 6 / 7。
- `transport.py` 内允许出现厂商专有字段；`adapters/<vendor>/transport.py` 之外出现即算越界。
- **待外部核验标记（D-1）——2026-10-11 23:52 起覆盖面收窄**：Qoder 侧 `connect(prompt)` 的一次真实往返
  已成功（`subtype=success`、`messages_seen=6`、CLI `exit_code=0`），故**鉴权形态与单次下发已验**；
  `NEEDS VERIFICATION` **仍保留**在未跑过的四支上：`query()` 多轮、`priority` 参数、`interrupt()`、
  `StreamEvent` 与工具/MCP 载荷。`doubao`（方舟）侧仍是**未验**：账号未开通那 4 个 Model ID（404
  `ModelNotOpen`），签名未实跑。本契约的四个方法签名不受上述任何一项影响。

### 4.1 `Handle`（T010 补齐：此前只有名字、无字段）

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `task_id` | str | ✓ | 与 `Task.task_id` 同值，供 `collect()` 对号 |
| `agent_id` | str | ✓ | 回执来自哪个单元 |
| `native_ref` | str | ✓ | **厂商侧可重查标识**，由各 transport 自己解释，协议层不理解其内容 |
| `submitted_at` | datetime（带时区） | ✓ | 下发时刻 |

实现落点 `src/acp/protocol/refs.py::Handle`。三条边界已在代码与用例两侧钉住：
- **不含可写句柄**：跨 Agent 只送路径与内容（§1 `inputs` 同一理由，FR-013）。
- **不混瞬态层**：字段名里不得出现 `slot`——`SlotHandle` 是进程内对象，随本结构落进任务卡或日志
  就等价于自造第二层持久化（宪法 I；消歧见 §6 的 N-11）。断言 = `test_handle_has_no_transient_slot_field`。
- **无厂商专有字段外的扩展**：`extra="forbid"`，厂商字段只进 `transport.py` 的内部表示，
  要跨出去必须先映射成上面四列。

## 5. 契约测试（`tests/contract/test_all_adapters.py`）

| 用例 | 断言 |
|---|---|
| 同一份 Task fixture 双向往返 | 三适配器均通过 schema 校验，字段无丢失 |
| 缺 `artifact_floor` 的 Task | 构建期失败，不进入下发 |
| `success` + 空产物 | 经门禁后 `validated_status=invalid`，且 reviewer 调用计数 = 0 |
| `CapabilityManifest` 缺项 | 注册失败 |
| 删除某适配器目录后重建编排图 | 图仍可构建（**零耦合证明**，FR-012） |
| `dev_model` 置为某值 | 选人与把关结果不变（扩展位不得影响语义） |

**落地进度（2026-10-12，Phase 1.1）**：本表六行里**三行已在 `tests/contract/test_protocol.py` 落地**
——"同一份 Task fixture 双向往返"（`test_three_messages_roundtrip`）、"缺 `artifact_floor` 的 Task"
（`test_task_missing_required_rejected[artifact_floor]` + `test_artifact_floor_rejects_missing_and_nonpositive`）、
"`CapabilityManifest` 缺项"（`test_manifest_missing_any_of_seven_rejected`）。
"删除某适配器目录后重建编排图"要等 Phase 3 的适配器与 `test_all_adapters.py`；
"`success` + 空产物 ⇒ `invalid` 且 reviewer 调用计数 = 0"要等 `ValidateArtifact` 节点（Phase 6），
本轮只落了它的**前置条件**（空产物构造得出来：`test_task_result_empty_artifacts_is_constructible`）；
"`dev_model` 不影响语义"本轮只落了**形状守卫**（`test_dev_model_is_the_only_difference`），
语义守卫归 Phase 5 的 `tests/guards/test_select_worker.py`——三条都尚未闭合，MUST NOT 读成已验。
**这套用例不是恒绿的**：2026-10-12 00:2x 做过变异对照，把 `extra` 改成 `allow`、去掉 `ArtifactFloor`
的两个 `gt=0` 与 `skill_tags` / `acceptance_criteria` 的 `min_length=1` 后，同一套用例读
**10 failed / 63 passed**，失败名单与三处削弱点逐一对应；按字节还原（sha256 前后相同）后复跑 **73 passed**。
**同轮末读数已随 owner 2026-10-12 裁决 3 变成 77**（`token_budget` 的 `gt=0` 净增 4 例），
且对这一条单独做过单点变异：摘掉 `gt=0` ⇒ 恰好 **3 failed / 74 passed**。

## 6. `SlotManager` 接口（瞬态层的契约面）

> 本节不是跨 Agent 消息，而是**运行时接口的契约面**。补写原因：宪法 v1.0.2 原则 I「批次性推迟的豁免条件」
> 第②条要求"实现推迟的模块 MUST 以目标组件的接口契约为准，并提供**替换实现不改调用方签名**的契约测试证明"，
> 而该接口名此前只出现在 `plan.md` / `research.md`，**契约层零落点**（`plan-review-v1.0.1.md` 附 C1·②）。

```python
class SlotManager(Protocol):
    def acquire(self, task_id: str, agent_id: str) -> SlotHandle: ...
    def release(self, handle: SlotHandle) -> None: ...
    def usage(self) -> dict[str, int]: ...   # agent_id -> 当前占用数
```

- 三个方法名与语义以 **Redis 侧的目标形状**为准（取 / 放 / 读计数）。MVP-1 用**进程内信号量**实现，
  **实现形态由调用方的同步性决定**（本契约不规定 `async`，只规定三个动词的签名与语义）——
  owner 2026-10-14 裁决待裁 1：原文写的是"进程内**异步**信号量"，而调用方 `Dispatch` 与 `SelectWorker`
  都是 LangGraph 的**同步节点函数**，无事件循环时 `await acquire()` 直接 `RuntimeError`，
  所以实现侧取 `threading.Condition` 是对的、**措辞侧是错的** ⇒ 本行改契约、**不动代码**；
  改契约属 PATCH（版本号由 owner 在发版时统一抬）。
  MVP-2 换 Redis 实现体**不改本签名**，调用方（`Dispatch` 与 `SelectWorker`）零改动。
- `usage()` 是 `agent_unit.current_load` 的**唯一权威来源**；PG 侧同名列只是上一次读到的快照
  （存储分层见原则 I，不得反向把快照当事实源）。
- **消歧声明（N-11）**：本方法名与 §2 的 `TaskResult.usage`（`UsageNotice` 列表）**同名不同物，
  二者无语义关联**——一个是瞬态层的**占用计数读取**，一个是跨 Agent 的**用量通告载体**；
  实现与命名检索时 MUST NOT 混为一谈，也 MUST NOT 为消除重名而把二者合并成同一类型
  （合并会让瞬态数据混进跨 Agent 消息，触原则 I 的存储分层与原则 II 的单一总线）。
- MUST NOT 对外暴露与目标组件语义冲突的接口：`SlotHandle` 在 MVP-1 是**进程内对象**，
  MUST NOT 被序列化进任务卡、日志、产物或 PG（一落盘即等价于自造第二层持久化）。
- **契约测试**：`tests/contract/test_slot_manager.py` —— 同一 fixture 在两种实现下双向通过，
  并断言调用方签名不变；这是豁免条件②的取证入口，判据行已登记在 `quickstart.md §3`。
- 依赖清单静态扫描（豁免条件③）：MVP-1 的 `pyproject.toml` MUST NOT 出现 `redis` 及其替代品。
  本轮该判据**无输入可扫**（仓库尚无依赖清单），故在 `plan.md` 的 Constitution Check 里登记为
  "已登记 · 待验"，关闭时点为 `implement`，不在 plan 阶段宣称已满足。
