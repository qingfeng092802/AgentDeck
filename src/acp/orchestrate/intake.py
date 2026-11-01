"""T123：EN-01 的**唯一**建行处（FR-001 / FR-002｜R-03）。

**为什么把它从 `api/requests.py` 里抽出来**：端到端入口（`python -m acp.run`）与 HTTP 端点要建的是
同一张需求单，而建行这件事里有四个决定：`id` 怎么来、`status` 落在哪一个初始态、
`clarification_waived` 由谁判、`created_at` 用哪个钟面。两份构造必然会在某一天漂成
"HTTP 建的卡能被图推进而 CLI 建的那张推不动" —— 而那种差别在任何一个入口的判据里都看不出来。
owner 2026-10-16 裁"允许抽取"，唯一前置 = 抽取后复跑 `test_request_intake.py` 那批判据（行为与状态码一字不变）。

**这一层不碰会话**：只返回对象，`add` / `flush` / commit 点仍归调用方（端点那边是
`app.state.session_factory` 提供的会话，CLI 那边是 `new_session()`）—— 与
`api/requests.py:_persist` 的模块头那条"提交点归装配方"是同一条口径，不在这里另立一份。

`created_at` 收成参数而不是在本函数里读钟面：判据要能钉"同一发重放两次是同一个瞬间"，
而一个自己读 `now()` 的函数做不到（同 `normalize` / `dispatch` / `assemble` 各节点的同一形状）。
"""

from __future__ import annotations

import datetime as dt
import uuid

from acp.orchestrate.nodes.plan import is_clarification_waived
from acp.persistence.models_core import FeatureRequest

#: 建行时写入的初始态。**唯一入口是 INTAKE**：`ADVANCEABLE`（`orchestrate/graph.py:77`）认的就是它，
#: 在这里写第二个值等于造一条图上推不动的需求单。
INITIAL_STATUS = "INTAKE"


def new_request(
    *,
    project_key: str,
    raw_input: str,
    summary_digest: dict[str, list[str]],
    clarification_answers: list[str] | None = None,
    request_id: uuid.UUID | None = None,
    created_at: dt.datetime | None = None,
    model_routing: dict[str, object] | None = None,
) -> FeatureRequest:
    """按 FR-001 / FR-002 的形状建一行 EN-01（不落库）。

    两个"不给就用现值"的参数各有边界：`request_id` 不给就是 `uuid4()`（真入口的形态），
    给了就用于"同一次提交重放两次必须命中同一行"那一类判据；`created_at` 同理。
    两者都不做默认对象缓存 —— 每次调用都得到一个新对象，调用方拿到的行与库里那行是同一份内存态。

    `model_routing`（2026-10-31 路线 B）由 API 层校验过形状后原样落行；`None` = 老请求，
    执行者与把关席都走原来的自动路径（不把"没给"读成"强制 qoder"）。
    """
    return FeatureRequest(
        id=request_id if request_id is not None else uuid.uuid4(),
        project_key=project_key,
        raw_input=raw_input,
        summary_digest=summary_digest,
        clarification_waived=is_clarification_waived(summary_digest, clarification_answers or []),
        status=INITIAL_STATUS,
        model_routing=dict(model_routing) if model_routing else None,
        created_at=created_at if created_at is not None else dt.datetime.now(dt.timezone.utc),
    )
