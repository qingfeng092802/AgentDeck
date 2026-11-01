"""瞬态层（运行时状态）：`slot_manager.py` 的并发槽位是它目前唯一的成员。

这一层刻意与 PG 分开（宪法原则 I 的存储分层）：**这里的数是"此刻"，那里的数是"上一次已知"**。
所以 `agent_unit.current_load` 的权威来源在本层（`usage()`），PG 那一列只是快照，
读侧 MUST NOT 反过来把快照当事实源 —— 那句话的载体是 `contracts/agent-protocol.md §6`，
本包不重述规则，只落实现。
"""
