# Contract: 审批接口与双通道（Approval API）

**Owner**: `api/` + `approval/` | **Governance**: 宪法 V（卡点走 interrupt、断点恢复、禁重跑）

一条硬约束：**看板与文本指令操作同一张审批单**。两者都只调本契约的同一端点，
不存在"通道各写一套状态"的第二条路径（FR-042）。

---

## 1. 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/approvals?state=pending` | 列未决单，按 `priority` 排序（C/D > B > A，FR-044） |
| GET | `/approvals/{ticket_id}` | 单据详情（五要素 + 状态 + 流水） |
| POST | `/approvals/{ticket_id}/resolve` | 提交结论（approve / reject / need_info），带 `version` |
| POST | `/approvals` | 由编排图内部建单（外部不直调） |
| GET | `/approvals/{ticket_id}/events` | `resolution_event` 流水，含 duplicate 留痕 |

CLI 侧（同一实现，不另开逻辑）：`/approve T01` → `resolve(approve)`；
`/reject T01 --reason "…"` → `resolve(reject)`；`/needinfo T01 --ask "…"` → `resolve(need_info)`。
文本通道与看板通道对同一 `ticket_id` 并发操作时，**由本契约的 CAS 决定唯一生效者**。

> **本契约的端点清单只覆盖 `/approvals…` 这条审批单面**（Owner 行那句 `api/ + approval/` 就是这个域）。
> 任务卡停在 `ESCALATED` 之后的**人工出口不建审批单**，走另一条独立路由
> `POST /tasks/{task_id}/escalation` —— owner 2026-10-14 待裁 ⑫ 定为独立路由，**权威在 `agent-protocol.md §1.4`**，
> 本文件不重述它的三处不同（入口态 / 改哪一列 / 主语是谁）。
> 两面唯一相同的那一条是"主语 MUST 是人"：审批面靠 CAS + `version`，人工出口靠 `principal ≠ orchestrator` 那道门。
> **两面不同的那一条也要读准**：`data-model.md §2` 写着 `PENDING_APPROVAL(kind=B) ──▶ ESCALATED`，
> 但 B 类卡点在 MVP-1 未启用（`approval/` 的 `ACTIVE_KINDS = {A, D}`）⇒ 现在落到 `ESCALATED` 的卡**没有对应的审批单**，
> 所以它只能经 §1.4 那条路由出，不能靠本契约 §1 的 `resolve` 端点出。
> CLI 文本通道**未接**人工出口（上面那三行只覆盖审批单）⇒ 登记在 `evidence/phase-07.md` 未闭清单。

## 2. 建单校验（缺项 MUST NOT 呈交，FR-039）

`POST /approvals` 的 `payload` 必含五要素：

| 键 | 内容 |
|---|---|
| `pending_item` | 待批内容 |
| `context_digest` | 上下文摘要 |
| `suggested_verdict` | 系统建议结论 |
| `impact` | 影响面：波及的后续任务 id 列表 + 预估额外用量 |
| `timeout_policy` | 该类卡点的超时时长与超时后行为（**数值读 `config/params-ref.md` §1**） |

缺任一键 ⇒ `400 payload_incomplete`，**不生成单据**（宁可卡住也不呈交不完整审批单）。

## 3. `resolve` 请求 / 响应

```jsonc
// 请求
{ "verdict": "approve" | "reject" | "need_info",
  "reason_or_ask": "…",         // reject 必填理由；need_info 必填追问内容
  "version": 3,                  // 客户端读到的单据版本，用于 CAS
  "channel": "dashboard" | "cli" }

// 成功生效
{ "state": "APPROVED", "resumed_from": "ApprovalGate", "applied": true }

// 后到的结论（或签：已被先到达的决定性结论定案）
{ "state": "APPROVED", "applied": false, "logged_as": "duplicate", "code": "already_decided" }
```

### 3.1 或签语义（FR-040，owner 2026-10-11 采纳方案 B 的落地）
- 先到达的**决定性结论**（approve / reject）以 `version` 乐观锁 CAS 写入；成功即 `applied=true`、
  单据离开待决态、触发恢复。
- 后到达的任何决定性结论：写 `resolution_event(applied=false, logged_as=duplicate)`，
  **不二次迁移状态**，返回 `409 already_decided`（响应体仍含 `applied:false` 以便通道侧如实显示）。
- `need_info` **不结束待决**：`state → NEED_INFO` 后回到 `PENDING_APPROVAL`，
  原 `payload` 五要素保留不丢（FR-040 三）。
- 本期只配置一名实际审批者；`decided_by_principal` 记录归属，多人时语义不变（任一有权限者的先到结论生效）。

**T-6 闭合（owner 2026-10-12 裁决 4）**：`approval_state` 枚举里**没有** `DUPLICATED`（也从来没有——
本契约上表就规定后到者"不二次迁移状态"）。"后到结论"这一事实的**唯一**载体是
`resolution_event(applied=false)` + 响应体的 `logged_as: duplicate`，不是单据的一个态。
`data-model.md` EN-09 的取值行已同步为五值（`PENDING / APPROVED / REJECTED / NEED_INFO / TIMED_OUT_CONVERGED`），
判据见 `tests/contract/test_states.py::test_t6_closed_duplicated_is_out_of_the_domain`
与库侧 `test_duplicated_is_out_of_the_type_after_ruling_4`（插入该值 MUST 被类型拒）。

## 4. 状态迁移与恢复

| verdict | 迁移 | 恢复动作 |
|---|---|---|
| approve | `PENDING_APPROVAL → READY`（或回中断前态） | 用同一 `thread_id` 从 `ApprovalGate` 续跑 |
| reject | `PENDING_APPROVAL → REJECTED` | 携理由回到执行或规划节点，留 `reason_text` |
| need_info | `PENDING_APPROVAL → NEED_INFO → PENDING_APPROVAL` | 不重跑已完成节点 |

恢复不变式（`tests/guards/test_approval_resume.py` 钉死）：
①已完成节点的执行次数在恢复后仍为 1；②不产生第二张同 id 任务卡；③`checkpoint` 与中断前一致。

## 5. 超时行为（结构约束硬编码，数值外置）

| kind | 默认 | 结构约束（写进代码，不随参数漂移） |
|---|---|---|
| A 拆单确认 | 挂起等待 | MUST NOT 自动放行 |
| B 验收未过 | 按时长走默认策略 | **收敛仅在跑满验收轮次上限后生效**；未跑满只能延长等待（有延长次数上限），MUST NOT 提前收敛；收敛态 `DELIVERED_WITH_ISSUES` |
| C 超预算 | 挂起等待 | 成本不可逆，MUST NOT 自动放行 |
| D 危险操作 | 挂起等待 | 安全优先，MUST NOT 自动放行 |

时长、延长次数、收敛策略取值一律读 `config/params-ref.md §1`；改这些值属引用型参数变更（PATCH），
但上表四条结构约束改动需走宪法修订。

**"无超时"在接口上的载体（owner 2026-10-13 裁决 ②，本轮随契约同步写明）**：上表 A/C/D 三行的
"挂起等待"对应 EN-09 `timeout_at` = **`NULL`**（列已由迁移 `0006_ruling_2_4_timeout_auto` 放宽为可空），
**不是** `9999-12-31` 那一类上界哨兵时刻——第一版那么落过，被判反模式后改判，旧哨兵行同批改回 `NULL`。
连带三条都属本契约面：

- 超时扫描谓词 `WHERE timeout_at <= now()` 在 `NULL` 上恒不命中（SQL 三值逻辑）⇒ 三类天然不会被扫成到期，
  不需要魔法数就满足上面"MUST NOT 自动放行"。
- GET `/approvals` 与详情响应里的 `timeout_at` 会是 `null`（`TicketSummary.timeout_at: str | None`），
  通道侧 MUST NOT 按"必有值"渲染。
- 同档排序里"无超时"排在**最前**：永不到期的单只有人能推，会到期的那张系统早晚自己收口。
  FR-044 没写这种情况 ⇒ 这是一条实现决定而非引用，载体是 `repository.list_tickets()` 的排序键，
  要反过来只改那一行，别在调用方各排一次。

## 6. 去重与并发

- 未决态部分唯一索引 `(task_card_id, kind) WHERE state='PENDING'` ⇒ 同任务同类型重复触发**合并**（FR-045）。
- **同一任务同时只允许一个未决卡点**：建单前先查该任务是否已有未决单，有则合并并返回既有 `ticket_id`。
- 排序：`priority = C/D(0) > B(1) > A(2)`（FR-044）。

## 7. 错误码

| code | 含义 | 触发方 |
|---|---|---|
| `400 payload_incomplete` | 五要素缺项 | 建单 |
| `409 already_decided` | 或签下后到的决定性结论 | resolve |
| `409 stale_version` | 客户端 `version` 过期 | resolve |
| `422 guard_not_closable` | 三岗矩阵无法闭合而试图放行 | 编排层（本应转 B 类单） |
| `403 principal_not_allowed` | 提交者不在审批者集合内 | resolve |
| `422 channel_not_allowed` | `resolve` 收到了人不该提交的通道：`channel` 在域里但没有在 `HUMAN_CHANNELS = {DASHBOARD, CLI}` 里（`AUTO` 由规则表免确认独占，owner 2026-10-13 裁决 ④） | resolve |

## 8. 字段名对照：API ↔ 库列（owner 2026-10-20 工作单第 4 条）

直接原因：owner 在终端上核库里那张单时敲了 `select … from approval_ticket where ticket_id = …`，
PG 报 `column "ticket_id" does not exist`。这**不是 schema 缺陷**，是两侧命名惯例不同：
表内主键按 SQL 惯例叫 `id`，对外字段按业务语义叫 `ticket_id`。映射只有一处载体
（`src/acp/api/approvals.py::_summary()` 与 `EventRow` 的构造），本节把它抄成可核对的表。

| API 字段 | 出现端点 | 库里的落点 | 备注 |
|---|---|---|---|
| `ticket_id` | 全部 | `approval_ticket.id` | 主键；库里没有 `ticket_id` 这一列 |
| `kind` | list / detail | `approval_ticket.kind`（`approval_kind`） | 值是 `A_SPLIT` 这种全名，不是 `A` |
| `state` | list / detail / resolve | `approval_ticket.state`（`approval_state`） | psql 里比较要 `state = 'PENDING'`，取值写 `state::text` |
| `version` | 全部 | `approval_ticket.version` | CAS 计数，从 0 起，每次生效 +1 |
| `task_card_id` | list / detail | `approval_ticket.task_card_id` | 可空；A 类单就是 NULL |
| `feature_request_id` | list / detail | `approval_ticket.feature_request_id` | 可空；EN-01 的外键 |
| `payload` | detail | `approval_ticket.payload`（jsonb） | 五要素整体存在这一个键里，不是五列 |
| `risk_exempt_eligible` | list / detail | `approval_ticket.risk_exempt_eligible` | 同名 |
| `timeout_at` | list / detail | `approval_ticket.timeout_at` | A / C / D 恒为 NULL（裁决 ②：NULL 是"没有可算的时刻"） |
| `decided_by_principal` | detail | `approval_ticket.decided_by_principal` | 未决时 NULL |
| `decided_at` | detail | `approval_ticket.decided_at` | 未决时 NULL |
| `priority` | list / detail | **没有列** | 由 `repository.priority_key(kind)` 现算（`C/D` 0 &gt; `B` 1 &gt; `A` 2，FR-044） |
| `event_count` | detail | **没有列** | `count(*) from resolution_event where approval_ticket_id = …` |
| `events[].event_id` | events | `resolution_event.id` | EN-10 指向单据的列叫 **`approval_ticket_id`**，也不叫 `ticket_id` |
| `events[].arrived_at` / `channel` / `verdict` / `applied` / `reason_text` | events | `resolution_event` 的同名五列 | `applied=false` 那行就是 or 签里后到的那一份 |
| `logged_as` | resolve | **没有列** | duplicate 这一事实的唯一载体是 `resolution_event.applied=false`（T-6 闭合） |
| `resumed_from` | resolve | **没有列** | 常量 = 图里那个节点名（`orchestrate.nodes.approval_gate.GATE`）；没注入 resumer **或**单据不是图上那个等待点（`kind != A_SPLIT`）时是 `null`。2026-10-22 起语义是"已交给谁"而不是"已经跑完"（续跑进了后台） |

核对这两侧的活命令（只读，不改任何东西）：

```bash
# ① 表里到底有哪些列（名字与顺序都取自 information_schema，不是抄本文）
psql "$ACP_DB_DSN" -c "select column_name, data_type from information_schema.columns
  where table_name in ('approval_ticket','resolution_event') and table_schema='public'
  order by table_name, ordinal_position"

# ② 拿 API 给的单据号回库里读那一行（把 :ticket 换成响应里的 ticket_id）
psql "$ACP_DB_DSN" -c "select id, state::text, version, kind::text,
    feature_request_id, task_card_id, decided_by_principal, timeout_at
  from approval_ticket where id = ':ticket'::uuid"

# ③ 那张单的到达流水（有几发结论真的到过）
psql "$ACP_DB_DSN" -c "select verdict::text, applied, channel::text, arrived_at, reason_text
  from resolution_event where approval_ticket_id = ':ticket'::uuid order by arrived_at"
```

**读法要说清楚**（否则这张表会被当成另一件事）：② 读到 `PENDING` 而接口返回过 `APPROVED`，
含义是那一笔结论**没提交**（M2-W11，本节不承担修法）；② 读到 0 行而 `realrun` 打印过单据号，
含义是建单那一步也没提交。两者的判据在 `tests/integration/test_approval_commit_boundary.py`。
