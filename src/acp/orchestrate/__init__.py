"""编排层（R-03 / R-04）：`orchestrate/nodes/*.py` 的图节点与 `graph.py` 的落点。

本包**只有节点函数**，不建连接、不持会话 —— 事实态的唯一写在 `persistence/`，
唯一 HTTP 面在 `api/`（见 `plan.md → Source Code` 的分层声明）。
`state.py` 与 `graph.py` 属 T036，本轮（T029 / T030 / T037）不提前建，免得把图形状写两遍。
"""
