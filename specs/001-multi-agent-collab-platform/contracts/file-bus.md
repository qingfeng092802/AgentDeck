# Contract: 文件总线（目录布局与写入归属）

**Owner**: `bus/` | **Governance**: 宪法 II（文件系统是唯一协作总线）、III（写隔离）、R-24 / FR-013 / FR-014

要点：**跨 Agent 的一切上下文、任务内容、产物、状态只能经文件总线流转**；工具之间 MUST NOT
建立私有直连通道。写入者由静态表决定，运行时不推断（D-4）。

---

## 1. 目录布局

```text
AI项目台/
└── <project_key>/                     # 单实例承载单项目；多项目 = 兄弟子目录
    ├── AGENTS.md                      # 协议说明（公共区，只读）
    ├── status.md                      # 当前状态总览（公共区，只读）
    ├── daily-log.md                   # 过程日志（公共区，只读）
    ├── tasks/                         # 任务卡（公共区，只读）
    │   └── T01-<slug>.md
    ├── results/                       # 产物索引（公共区，只读）
    │   └── T01.md
    ├── docs/meetings/                 # 架构师岗位纪要（公共区，只读）
    ├── workbuddy-workspace/           # 唯一写入者 = WorkBuddy 适配器
    ├── qoder-workspace/               # 唯一写入者 = Qoder 适配器
    └── doubao-workspace/              # 唯一写入者 = 豆包适配器
```

新增 Agent ⇒ 新增一个 `<agent>-workspace/`，公共区结构不变（FR-012）。

## 2. 写入归属表（`bus/acl.py` 的形状）

| 路径模式 | 唯一写入者 | 其他主体 |
|---|---|---|
| `tasks/**` | `orchestrator_writer`（编排引擎单点） | 全体只读 |
| `results/**` | `orchestrator_writer` | 全体只读 |
| `status.md`、`daily-log.md` | `orchestrator_writer` | 全体只读 |
| `AGENTS.md` | `orchestrator_writer`（且仅协议变更时） | 全体只读 |
| `docs/meetings/**` | `orchestrator_writer`（代架构师岗位执笔） | 全体只读 |
| `<agent>-workspace/**` | 对应 agent 的适配器实例 | 其余只读 |

规则：
- **跨 workspace 读取允许，写入拒绝**（FR-013）。
- 公共区写入请求来自任何适配器 ⇒ 一律 `403 write_forbidden`，并落 `GUARD_VIOLATION` 审计行。
- 审查员 / 验收员岗位（`role_spec.read_only = true`）**对该任务的产物路径无写权限**（FR-033 / D-2）：
  需要改动产物的唯一路径是出具 `CHANGES_REQUESTED`，由原执行者返工。
  **口径按 owner 2026-10-13 裁决 1 改为任务级**：只读约束跟的是"这次请求所披的身份"，不是工具本身 ⇒
  同一支工具作为**别的任务**的执行者写它自己那一份 workspace 是允许的（判据 `declared_owner(path) == actor.actor_id`，
  实现 `bus/acl.py:160-166`）。本条此前写的是"在任何路径都无写权限，**含其自身 workspace**"，
  那句保留在此作它自己的时点读数，改写发生在 19:4x、与 ACL 那一笔同批。

### 2.1 实现侧错误码（Phase 3 收口补登：此前契约一个都没列）

**这批码是网关与 ACL 的产出，不是厂商报的**，取值域的唯一事实源是下面这张表 + 那两个文件。
owner 点名的是 `unsupported_format` / `bad_path` / `path_escape` 三个；登记时把同族的另三个
（`unmapped_path` / `write_forbidden` / `read_only_violation`）一并补齐，免得为半个表再来一轮。

| 码 | HTTP | `guard_kind` | 触发点（代码位置 = 唯一） | 为什么要单独一档 | 断它的用例 |
|---|---|---|---|---|---|
| `bad_path` | 400 | `GUARD_VIOLATION` | `bus/acl.py:154`（`matches(rel_path)` 抛 `ValueError`） | 形状就不对的路径 MUST NOT 喂给后续判定（归因要落在"路径"而不是"权限"） | `tests/guards/test_bus_isolation.py:190` |
| `unmapped_path` | 403 | `GUARD_VIOLATION` | `bus/acl.py:156` | 归属表里没有这条形状 ⇒ **拒**，没有默认放行分支（§8 那行已落地口径） | `tests/guards/test_bus_isolation.py` |
| `read_only_violation` | 403 | `READONLY_VIOLATION` | `bus/acl.py:160-166`（判定式 = `declared_owner(rel_path) != actor.actor_id`） | 只读身份对**不是它自己那一份** workspace 的路径无写权限（FR-033 / D-2，owner 2026-10-13 裁决 1 把只读定为**任务级**）。这一条仍排在归属比对之前，但放行只放到"不被本条拒"，第 4 条的任务级凭证照查 | `test_readonly_actor_denied_on_public_paths`、`test_readonly_actor_cannot_write_other_workspace`（正向的另一半 = `test_readonly_actor_can_write_its_own_workspace`，它断的是**不出现**这一档） |
| `write_forbidden` | 403 | `GUARD_VIOLATION` | `bus/acl.py:169`（公共区非编排身份）、`:177`（缺任务级能力凭证）、`:180`（凭证与目录声明不符） | 三处都是"写入身份不对"，共用一个码；细分在 `reason` 里，不在码名里 | §2 规则二 + `test_bus_isolation.py` |
| `path_escape` | 403 | 写面 `GUARD_VIOLATION`（`bus/gateway.py:228`）、读面 `READONLY_VIOLATION`（`bus/gateway.py:277`） | 解析**之后**目标越出总线根（`..`、符号链接、绝对路径拼接） | 归属表只管路径形状，越界是 resolve 之后才成立的事实 ⇒ 两道必须分开，否则"表里允许"会盖住"根外面" | `tests/guards/test_bus_isolation.py:190` |
| `unsupported_format` | 400 | `GUARD_VIOLATION` | `bus/gateway.py:321-340`（`format_of`，后缀 ∉ `ARTIFACT_FORMATS`） | 放过域外值 ⇒ "格式不符"这一支永远不可证伪（`protocol/refs.py` 顶部把它写成 T-5 的关闭条件） | `tests/contract/test_qoder_transport.py:653`、`tests/contract/test_workbuddy_transport.py:380` |

> **登记时顺手抓出的一处不一致（owner 2026-10-13 裁决 3：归 Phase 6 的 EN-14 重做，本轮未改代码）**：
> 读面越界（`gateway.py:277`）报的 `guard_kind` 是
> `READONLY_VIOLATION`，而那一档在审计里的语义是"**只读岗位**试图写"（`acl.py:160` 那一支）。
> 于是一次"读别人根外的路径"会被记成"只读岗违规"，`daily-log.md` 与审计行的归因就错了。
> 两个候选：(a) 读面改 `GUARD_VIOLATION`（审计分类变准，代价是历史留痕里已有的那几行读起来会与新行不同）；
> (b) 保持现状并在本表加一句"读面复用 `READONLY_VIOLATION` 是有意的"。⇒ 已裁归 **Phase 6**，不在 Phase 3 动。
> 【2026-10-13 19:4x 补一句时效】裁决 1 之后写面那一支的语义又窄了一次（只读身份**可以**写自己 workspace），
> 所以 (a) 的代价比登记时更小：审计里"只读岗位试图写"现在只剩"写公共区或别人的目录"一种形态。

## 3. 任务卡格式（三行封顶）

```markdown
目标: <一句话说明要产出什么>
输入: <引用的上下文/文件/依赖产物路径>
期望产物: <形态与落点，如 qoder-workspace/index.html>
```

- **正文三行封顶**；第四行起视为协议违规，网关拒写（R-24 验收标准）。
- 元数据（`artifact_floor` / `acceptance_criteria` / `token_budget` / 三岗 agent_id / 轮次）**不进正文**，
  以受控 frontmatter 由编排引擎写入并回读；**`bus/gateway.py` 在渲染给 Agent 的时刻 MUST 剥去
  frontmatter，Agent 可见面恰为三行**（强制点，判据见 `tests/guards/test_bus_isolation.py`）；
  三行只承载"给 Agent 看的工作说明"。
- **分层的四条约束随宪法 v1.0.3 原则 II「受控 frontmatter 分层」成文**，本契约只登记落点，不复述条文：
  ① 字段类**封闭于上述五类**，新增字段类须走宪法修订，`bus/gateway.py` 对超类字段**拒写**；
  ② 渲染面**剥去** frontmatter（上一条即其强制点）；③ **正文自足**——无 frontmatter 时三行仍可独立解析，
  PG 不可用时按宪法「三」的回退路径**只依赖正文三行**重建"任务—状态—产物"三元组；
  ④ 四条断言全部落在 `tests/guards/test_bus_isolation.py`（渲染输出不含 frontmatter 键值 / 超五类拒写 /
  四行及以上正文拒写 / 无 frontmatter 时三行仍可被解析），判据行见 `quickstart.md §3`。
- 文件名规范：`T<NN>-<slug>.md`，`<NN>` 在需求单内唯一。

## 4. `status.md` 与 `daily-log.md`

| 文件 | 内容 | 更新时机 |
|---|---|---|
| `status.md` | 每张任务卡的 `状态 / 执行者 / 审查者 / 验收者 / 产物落点 / 累计用量（按口径分列）` | 每次状态迁移后由单点写入 |
| `daily-log.md` | 追加式事件行：`时间 \| 实体 \| 动作 \| actor \| 摘要` | 每次交互落盘（FR-052） |

两者是 PG 不可用时的**重建输入**：`status.md` + `tasks/**` + `results/**` ⇒ 可推出
"任务—状态—产物"三元组（宪法「三」崩溃回退第 2 条，D-9）。

## 5. 脱敏与凭据红线

任何经总线落盘的内容 MUST NOT 含凭据形态（密钥、令牌、签名头），MUST NOT 含 `params-ref §7`
禁用命名（该表是静态扫描门禁的输入）。落盘前统一过 `secrets` 过滤器；违规写按越权动作处理（卡点 D）。

> **落地状态与两处未闭合（2026-10-12 / T027 + T028，逐条点名，不把"部分做到"写成"做到"）**：
> ① 过滤器的**产物面**强制点在 `bus/gateway.py::write_artifact` 落盘之前——只这一道就覆盖全部写入面，
> 因为 `orchestrator_writer` 的五个公共区动作全部经本入口写盘；**日志面** = `logging` 的 `secrets`
> 过滤器（`config/loader.py::install_logging_filter`）。
> ② **本句后半条"按越权动作处理（卡点 D）"未接线**：现在的处置是"洗掉内容 +
> `logging.warning`（只报路径与长度，不回显任何片段）"，**不**转 D 类单——D 类单的建单路径属
> Phase 4（T036 之后）。所以这句现在只对"MUST NOT 落盘"成立，对"成卡点"未成立。
> ③ 覆盖面 = **凭据形态 ∪ 当前环境的凭据值**，两路之外的裸串（不合任何形态、又不在环境变量里）
> 洗不掉；把它也洗掉的唯一办法是"凡像密码的都洗"，那会把产物正常内容一起洗花，代价比收益大。
> 这条以特征化用例留档（`tests/guards/test_credentials.py::test_bare_value_without_a_form_survives_by_design`），
> 不是已解决问题。

## 6. 一致性关系式（不写死绝对数）

- `results/**` 中某任务卡的条目数 == 该任务 `artifact` 记录数（不一致即视为总线与事实态漂移，报错）。
- 每张 `tasks/T**` 在 `status.md` 中必有对应行；反之亦然（缺行 = 状态未落盘，报错）。
- `<agent>-workspace/` 的文件属主 agent == 该目录 ACL 声明的唯一写入者（启动自检逐目录断言）。

## 7. 校验用例（`tests/guards/test_bus_isolation.py`）

| 用例 | 期望 |
|---|---|
| Qoder 适配器写 `tasks/T02.md` | 拒绝 + 审计行 |
| Qoder 适配器写 `doubao-workspace/x.md` | 拒绝 + 审计行 |
| reviewer 身份改写自己已读产物的一行 | 拒绝（read_only_violation）+ 卡点 D |
| 四行任务卡写入 | 拒写，报协议违规 |
| 并发 10 次 `status.md` 更新 | 串行单点写，无 interleaving 半行 |
| 停 PG 后按 §4 重建三元组 | 三元组与停库前一致（回退路径可执行，不只存在于文档） |

---

## 8. 落地进度（2026-10-12，Phase 1.2 / T014 ~ T022）

| 本契约的位置 | 实现落点 | 断它的用例 | 本轮状态 |
|---|---|---|---|
| §1 目录布局 | `src/acp/bus/layout.py` | `test_ensure_layout_creates_contract_dirs` | 已落地。公共区分成 `PUBLIC_DIRS`(3) 与 `PUBLIC_FILES`(3)——三个 `.md` 是文件，`mkdir` 它们会得到一个名叫 `status.md` 的目录 |
| §2 写入归属表 | `src/acp/bus/acl.py::RULES`（7 条，行序与本表一致） | `test_declared_owner_is_single_valued_per_path` | 已落地，且**匹配不到 = 拒**（无默认放行分支）。判定顺序：路径 → `read_only` → 公共区身份 → workspace 凭证 |
| §2 规则一（跨 workspace 读允许） | `gateway.read` | `test_cross_workspace_read_is_allowed` | 已落地。`read` 的 `actor` 参数**当前不参与判定**——契约与宪法都没有"按角色限读"的条款，这条写在方法 docstring 里，免得日后被读成"作者忘了" |
| §3 三行封顶与五类封闭 | `gateway.validate_card` / `gateway.render_card` | 四行、缺行、重复标签、第六类共 7 例 | 已落地。冒号固定 ASCII：落盘面由代码生成，宽松认中文冒号会让"四行拒写"因输入形态不同而漂 |
| §3 分层条件②（渲染剥 frontmatter） | `gateway.render_for_agent` | `test_rendered_frontmatter_never_reaches_the_agent` | 已落地。单点变异"不剥"⇒ 该用例红 |
| §3 分层条件③（无 frontmatter 仍可解析） | `gateway.split_card` / `recover._parse_body` | `test_card_without_frontmatter_still_parses`、`test_recover_reads_body_when_frontmatter_is_absent` | 已落地 |
| §4 `status.md` / `daily-log.md` | `orchestrator_writer.upsert_status` / `append_event` | `test_status_upsert_replaces_not_appends`、`test_concurrent_event_appends_keep_every_line` | 已落地。**整表重写**而非追加，使 §6 第二式由构造成立 |
| §5 凭据与禁用命名 | `acp/config/loader.py`（形态正则 ∪ 环境凭据值，两路）；产物面 = `gateway.write_artifact` 落盘前一道，日志面 = `secrets` 过滤器；命名面 = `acp/guard/naming.py` 解析 `params-ref §7 / §8` | `test_credentials.py` 25 例、`test_naming_guard.py` 9 例 | 已落地（T027 / T028，2026-10-12）。产物面只挂一道即可覆盖全部写入面：`orchestrator_writer` 的五个动作全部经 `write_artifact`。**两条未闭合照原文登记**：卡点 D 的处置分支未接（现只洗不建单）；`TaskResult.log_excerpt` 的入库前脱敏要等 Phase 2 写 EN-14 时经同一条 `redact_bus_text`。变异对照：摘掉网关那一行 ⇒ **2 failed**，其余 23 条仍绿 ⇒ 产物面与过滤器本体是两道关 |
| §6 关系式一（`results/**` == `artifact` 数） | `recover.reconcile`（**只读**，`--verify-consistency` 那一档） | `test_verify_consistency_passes_when_both_sides_hold_the_same_pairs` 系列：同式成立那条 + 两个方向各点名一次差集那条 + 空项目算 0 对不算失败那条 | **2026-10-16 T112 实算闭合**（owner 裁决 3 允许同步本行）。2026-10-12 那一格的原读数 **未落地 / `pg_side_checked=False`** 留档不改写——它当时的成立条件是"没有 PG 侧计数路径"，T112 建了这条路径所以条件被推翻，不是被追上。**形状比原文严一档**：原文写"某任务卡的 `results/**` 条目数 == 该任务 `artifact` 记录数"（逐卡），`reconcile()` 两侧都按**卡集合的条目总数**成对比，逐卡差额要靠差集非空才能暴露 ⇒ 差集两个方向各有一条用例兜着。**写入侧仍不交**：本行只把"算不算得出"从 False 翻成 True，`status.md` → EN-02 的回填会给 `task_card.status` 造第二个写者，归 owner 裁（tasks.md 的 ㉞）。`Reconstruction.pg_side_checked` 在另两档仍是 **False**（那是它们各自的真实状态，没有跟着本行翻） |
| §6 关系式二（任务卡与 `status.md` 成对） | `recover.rebuild` | `test_recover_fails_when_status_row_is_missing` | 已落地 |
| §6 关系式三（workspace 目录名 == 声明 owner） | `selfcheck.check_bus_layout`（C3） | C3 探针：无项目 `BLOCKED`、3 个 workspace `PASS`、`Bad_Agent-workspace` `FAIL` | 已落地。C3 无输入时读 **BLOCKED 而不是 PASS** |
| §7 六行用例 | `tests/guards/test_bus_isolation.py`（47 passed / 1.20 s） | 四道单点变异各红 2 / 1 / 2 / 1 例 | 已落地 |

