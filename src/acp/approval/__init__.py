"""人工审批卡点（R-26）：建单校验、或签 CAS、超时判定。

分层与 `bus/` 同一条口径，且**依赖只能向下**：

- `timeouts.py` 只读两份治理文件（`config/params-ref.md §1` 与宪法 原则 III），不碰库；
- `risk_engine.py` 在 `registry/` 那一面（EN-11 的匹配口径），本包只消费它的判定结果；
- `repository.py` 是唯一写 EN-09 / EN-10 的地方（CAS 与到达流水）；
- `service.py` 在最上面：五要素校验（FR-039）+ 建单（FR-045 合并）+ 结论落库的编排。

本包 **MUST NOT import 任何适配器**：卡点只与库和图打交道，工具形态归 `adapters/`（原则 II）。
"""

from __future__ import annotations
