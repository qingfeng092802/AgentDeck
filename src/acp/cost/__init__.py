"""成本记账（R-25 / FR-059 ~ FR-061）—— MVP-1 只做埋点与口径分列，看板展示与预算卡点启用归 M2。

三个模块各一件事，别在这里做第四件：

- `pricing.py`：把 `config/params-ref.md §6` 的价目读成可算的单价（**数值的唯一载体在那份文档里**，
  本包不抄第二份），并按 `billing_basis` 三口径分别算出金额。
- `ledger.py`：写入点（由 `orchestrate/nodes/result_ingest.py` 在**它自己的事务里**调用）
  与双维查询（按 `agent_id`、按 `task_id`）。落库本体在 `persistence/costs.py`（T113）。
- `budget.py`：EN-02 `token_budget` 的阈值比对与 C 类卡点的建单路径（MVP-1 **结构预留、默认不启用**，
  启用与否是 owner 的 Phase 4 裁决 6 的范围，不由本包自扩）。

**不在这里做的事**：事务边界（归调用方，同 Phase 7 的 A-1）、图上的暂停与恢复（状态机面，
动它属硬停项）、把三类口径加成一个数（FR-061 与宪法「二」明令禁止，`ledger.Breakdown.total()`
就是专门用来报错的那一个接口）。
"""
