"""HTTP 面（`plan.md → Source Code` 里 `api/` 那一层）：FastAPI 路由。

一图一路由文件，装配（`app.py` / `python -m acp.run`）归 T123，本轮不提前建 ——
现在造一个"临时 main"会让 T123 落地时有两份 app 装配可指。用例通过 `app.state` 注入
补全器与会话工厂，因此**测试不需要产品代码里存在任何默认值**。
"""
