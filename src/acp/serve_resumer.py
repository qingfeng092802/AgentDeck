"""Phase 15 第三步：网页批准之后**真的把图和厂商跑起来**（owner 2026-10-22 裁决 ③）。

在此之前 `serve.build_app()` 不注 `graph_resumer`，于是 `POST /approvals/{id}/resolve` 只改库、
不起工人（`evidence/phase-15.md` §二十九 的 29.11 那格）。这一笔把那一格接上，做三件事：

1. **续跑链只借一遍，不另写一份**：`run.run_resume()` + `realrun.run_real()` 是 `acp.realrun --resume`
   走的那同两条（`realrun.py:973` 与 `realrun.py:1001`）。这里只负责把它们放进一个工作单元、
   补上看板这一侧需要的两样东西（`ensure_archive_root` 的建现场、会话的生死），不重写驱动。
   "第二份驱动"的代价本仓已经量过：两处各拼一次「先恢复再派发」的次序，漂开的表现是
   "CLI 跑得动、网页点不动"或者反过来，而两边各自都自洽。
2. **HTTP 不被它拖住**：调用点是 `api/approvals.py::_resume` 里的 `background.add_task(...)`，
   响应先回、续跑后跑（同步端点 ⇒ Starlette 把后台任务放线程池）。这里再保证一次
   "同一张需求单同时只有一条续跑链"：两条链并跑会各自 `run_assign` 同一批卡，
   一条占槽失败、一条把卡跑到一半，归因起来是两条都不像。
3. **每一步都要在活动流里看得见**：续跑开始 / 结束 / 失败各写一行 EN-14（`kind=APPROVAL`），
   走 `persistence/events.py` 那唯一入口（脱敏与 kind 域检都在那里）。**失败不往外抛**：
   后台任务抛出去只剩 uvicorn 一条日志，看板上什么都不会发生 —— 那正是 owner 说的"停摆"。

**提交点两侧各一个，各有理由**（会话口径同 `persistence/session.py::new_session`）：

- 续跑 + 派发**整发一个事务**，由 `realrun.run_real(commit_after_dispatch=session.commit)` 在
  "派发完、厂商还没开始"那一拍落地。不这样的话，派发与 `TASK dispatched` 留痕要等厂商跑完才可见
  （CLI 那一支本来就是整发一个事务：它靠 stdout 看进度，看板没有 stdout）。
- 收尾那几行留痕（`resume_finished` / `resume_failed`）**另开一个会话**：它们要在主工作单元
  回滚之后仍然留下 —— 一次失败的续跑如果连"失败过"都读不到，看板上就等于没发生过。

**这一发证明不到什么**（写在最前面，免得读成"一键全流程已通"）：

- 厂商凭据仍在环境变量里，本模块一次都不读、也不打印；缺凭据的那一发会被记成
  `resume_failed / credential_missing`（名字，不是值）。
- 活动流只有**结构行**粒度：dispatch / 结果回投 / 门禁各有留痕，厂商在那 105 秒里做了什么都推不出来
  （口径同 `realrun.py` 模块头"日志的粒度是诚实的"那一句）。
- 只唤醒 `A_SPLIT` 那一类单据：图上的中断点只有 `ApprovalGate`，别的类别的批准不碰图。
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from acp import realrun, run
from acp.bus.orchestrator_writer import ORCHESTRATOR
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.orchestrate.nodes.approval_gate import GATE
from acp.persistence import events
from acp.persistence import session as session_module
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.review import policy as review_policy

#: 留痕的主体列：续跑是编排器那一侧的动作（与 `bus/orchestrator_writer.py` 写 daily-log 的角色同名，
#: 名字从 `bus/acl.py` 的 Actor 取，不在这里再抄一份字面量）。
RESUME_ACTOR = ORCHESTRATOR.role_id


class WebResumeRefused(RuntimeError):
    """装配面/现场的问题（不是厂商的问题）。`code` 是写进活动流那一行的归因词。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@dataclass
class WebResumer:
    """`app.state.graph_resumer` 的生产实现。

    `transport_factory` 与 `realrun.real_transport` 同形（`(agent_id, gateway, cwd) -> transport`）：
    判据换成替身，生产不传 ⇒ 出厂就是真厂商那一条。
    """

    session_factory: Callable[[], Session]
    bus_root: Path
    transport_factory: Callable[[str, Any, Path], object] = realrun.real_transport
    clock: Callable[[], dt.datetime] = utcnow
    emit_to: Callable[[str], object] = _stderr
    #: Phase 17 P2：自动审查跑者。`manual` 模式保持 `None`（Phase 16 行为逐字不变）。
    review_runner: Any | None = None
    #: Phase 17 P3：把关选人的模式（决定 reviewer 席是否收窄到 `deepseek-reviewer`）。
    #: 与 `review_runner` 由 `serve.build_app` 按同一个 `ACP_REVIEW_MODE` 一起注入。
    review_mode: str = review_policy.REVIEW_MODE_MANUAL

    _locks: dict[str, threading.Lock] = field(default_factory=dict, init=False, repr=False)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __call__(self, *, feature_request_id: Any, conclusion: str, principal: str | None = None) -> None:
        """`api/approvals.py::_resume` 唯一的调用面。**永不抛** —— 失败走留痕。

        `principal` 是"谁按下了这一发"：只有人工入口（`POST /requests/{id}/continue`）会传，
        「批准」那一发不传。它决定要不要在开跑前把停在 `RUNNING` 的孤儿卡收回 `READY`
        （`realrun.reclaim_orphaned_cards` 那段解释了为什么这件事必须由人触发）。
        """
        if feature_request_id is None:
            self._trace(None, action="resume_skipped", reason="no_feature_request_id", conclusion=conclusion)
            return
        # 生产形态是 UUID（`ApprovalTicket.feature_request_id` 是 `UUID(as_uuid=True)`）；字符串也收，
        # 因为调用面不该把"从哪来"的知识泄漏出去。**不能直接写 `uuid.UUID(x)`**：x 本身是 UUID 时
        # 它会去找 `.replace` 而在后台线程里炸掉 —— 判据只喂字符串时这一格是绿的（实测踩到）。
        fid = feature_request_id if isinstance(feature_request_id, uuid.UUID) else uuid.UUID(str(feature_request_id))
        lock = self._lock_for(str(fid))
        if not lock.acquire(blocking=False):
            # 同一张需求单已经有一条在跑：第二发不是"再跑一次"，是同一件事的重复触发。
            self._trace(fid, action="resume_skipped", reason="already_running", conclusion=conclusion)
            return
        followups: tuple[uuid.UUID, ...] = ()
        review_ids: tuple[str, ...] = ()
        try:
            review_ids = self._run(fid, conclusion, principal)
        finally:
            lock.release()
        # 审查跑者与续跑都 MUST 在锁释放之后：锁不是可重入的，持锁递归只会得到
        # `resume_skipped/already_running`，而那种"跳过"在活动流里看着像跑完了。
        if review_ids and self.review_runner is not None:
            followups = self._review(fid, review_ids)
        if followups:
            self(feature_request_id=fid, conclusion="continue")

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _waiting_nodes(self, session: Session, feature_request_id: uuid.UUID) -> tuple[str, ...]:
        """P1-3：`continue` 也要先看图上还有没有等待点。

        单独开一个方法是为了让判据能注入：测试零库，不该为了读一次 `next` 去连 checkpointer。
        """
        return run.waiting_nodes(
            feature_request_id,
            session=session,
            bus_root=self.bus_root,
        )

    def _run(self, fid: uuid.UUID, conclusion: str, principal: str | None = None) -> tuple[str, ...]:
        feature_request_id = str(fid)
        self._emit(f"RESUME start :: request={feature_request_id} conclusion={conclusion}")
        self._trace(feature_request_id, action="resume_started", conclusion=conclusion)

        summary: dict[str, Any] = {"conclusion": conclusion}
        outcomes: list[Any] = []
        try:
            with session_module.new_session(self.session_factory) as session:
                request = session.get(FeatureRequest, fid)
                if request is None:
                    raise WebResumeRefused(
                        f"EN-01 里没有这条需求单 ⇒ 无现场可续跑：feature_request_id={feature_request_id}",
                        code="feature_request_missing",
                    )
                project_key = str(request.project_key)
                summary["project_key"] = project_key
                # 与 `acp.realrun` 同一次建现场：看板这一侧不要求人先跑过 CLI。
                realrun.ensure_archive_root(project_key, self.bus_root, emit_to=self.emit_to)
                needs_resume = conclusion != "continue"
                if conclusion == "continue":
                    # P1-3：评审通过后的第二段通常已经没有等待点（图上的 gate 已消费）。
                    # 但"单据已批、图还停在 gate"是一种真实现场（上一次 resume 失败/进程被杀）；
                    # 这时 `resolve` 已无单可批，只有先 `run_resume` 才能把卡推进。先读 `next`，
                    # 不拿状态猜。
                    needs_resume = GATE in self._waiting_nodes(session, fid)
                if needs_resume:
                    # `continue` 是评审通过之后的第二段：图上那个等待点已经被消费掉了，
                    # 再调恢复段只会得到 `no_waiting_point`；这一段只把已解锁的卡接着跑。
                    # 例外见上面：`next` 里还有 gate ⇒ 必须先恢复。
                    report = run.run_resume(
                        fid,
                        transport=realrun.FROZEN_SEGMENT_GATE,
                        bus_root=self.bus_root,
                        session=session,
                    )
                    summary["ticket_state"] = report.ticket_state
                    summary["waiting_after"] = ",".join(report.waiting_after) or "-"
                if conclusion in ("approve", "continue"):
                    outcomes = list(
                        realrun.run_real(
                            session,
                            feature_request_id=fid,
                            project_key=project_key,
                            bus_root=self.bus_root,
                            now=self.clock(),
                            emit_to=self.emit_to,
                            # 路线 B：这一单若点名了纯 API 执行器，工厂要带着它的 `model_routing`
                            # 去装配（没有路由时原样返回，老行为不变）。
                            transport_factory=realrun.transport_factory_for(
                                session, fid, self.transport_factory
                            ),
                            # 派发一落地就可见：看板没有 stdout，只能靠这一拍提交。
                            commit_after_dispatch=session.commit,
                            review_mode=self.review_mode,
                            # 只有人工入口带着 `principal` 进来：那一刻才允许把没人推的 `RUNNING` 卡收回。
                            reclaim_principal=principal,
                        )
                    )
                    summary["cards"] = len(outcomes)
                    summary["card_errors"] = sum(1 for item in outcomes if item.error is not None)
                    summary["card_statuses"] = ",".join(
                        f"{item.card_id}:{item.validated_status or '-'}" for item in outcomes
                    ) or "-"
        except run.RunFailure as exc:
            if exc.code == run.NO_WAITING_POINT:
                # 第二次之后的批准（评审通过让下游卡变 READY）没有等待点可恢复 —— 这是常态不是故障。
                self._emit(f"RESUME skip :: {exc.code}")
                self._trace(feature_request_id, action="resume_skipped", reason=exc.code, **summary)
            else:
                self._emit(f"RESUME fail :: {exc.code} :: {exc}")
                summary["released_cards"] = ",".join(self._release_stuck_cards(fid, code=exc.code)) or "-"
                self._trace(feature_request_id, action="resume_failed", code=exc.code, error=str(exc), **summary)
            return ()
        except Exception as exc:  # noqa: BLE001 - 后台任务抛出去就没人看得见了（模块头第 3 条）
            code = str(getattr(exc, "code", type(exc).__name__))
            detail: dict[str, Any] = {"code": code, "error": str(exc)}
            name = getattr(exc, "name", None)  # `CredentialMissing` 只报变量名，不报值
            if name:
                detail["name"] = str(name)
            self._emit(f"RESUME fail :: {code} :: {exc}")
            summary["released_cards"] = ",".join(self._release_stuck_cards(fid, code=code)) or "-"
            self._trace(feature_request_id, action="resume_failed", **detail, **summary)
            return ()
        else:
            # 这里已经退出 `with new_session` ⇒ 本轮 `REVIEWING` 已提交，审查跑者用另一条
            # 连接也看得到。调用点仍在 `__call__` 的锁释放之后（见那里的注释）。
            review_ids = (
                tuple(
                    str(item.card_id)
                    for item in outcomes
                    if item.error is None and str(getattr(item, "card_status", "")) == "REVIEWING"
                )
                if self.review_runner is not None
                else ()
            )
            self._emit("RESUME done :: " + " ".join(f"{key}={value}" for key, value in summary.items()))
            self._trace(feature_request_id, action="resume_finished", **summary)
            return review_ids

    def _runner_for(self, fid: uuid.UUID):
        """按需求单选审查跑者：点名了模型席位 ⇒ 通用客户端；否则沿用启动期的那一个。"""
        if self.session_factory is None:
            return self.review_runner
        try:
            session = self.session_factory()
        except Exception:  # noqa: BLE001 - 拿不到会话就用启动期那一个，别把审查整段打死
            return self.review_runner
        try:
            from acp import routing_vault
            from acp.routing import routing_of, seat_model
            from acp.persistence.models_core import FeatureRequest
            from acp.review.routing_client import RoutingReviewClient, seat_for
            from acp.review.runner import ReviewRunner

            request = session.get(FeatureRequest, fid)
            routing = routing_of(request)
            if seat_model(routing, "reviewer") is None:
                return self.review_runner
            seat = seat_for(routing, "reviewer", request_id=fid, vault_key_for=routing_vault.key_for)
            return ReviewRunner(
                session_factory=self.session_factory,
                client=RoutingReviewClient(seat),
                bus_root=self.bus_root,
                clock=self.clock,
            )
        except Exception as exc:  # noqa: BLE001 - 配错了就退回原跑者，并在活动流留痕
            self._emit(f"REVIEW routing-client failed :: {type(exc).__name__}: {exc}")
            return self.review_runner
        finally:
            session.close()

    def _review(self, fid: uuid.UUID, card_ids: tuple[str, ...]) -> tuple[uuid.UUID, ...]:
        """把本轮仍为 `REVIEWING` 的卡交给审查跑者；失败只留痕，不影响续跑结果。

        路线 C：这一单若点名了审查模型，用**它**跑（通用客户端）；否则用启动期那一个。
        """
        runner = self._runner_for(fid)
        if runner is None:
            return ()
        try:
            batch = runner(feature_request_id=fid, card_ids=card_ids)
        except Exception as exc:  # noqa: BLE001 - 审查失败不能反过来把续跑判成失败
            self._emit(f"REVIEW batch-failed :: {type(exc).__name__}: {exc}")
            self._trace(
                fid,
                action="review_auto_failed",
                code=type(exc).__name__,
                error=str(exc),
                cards=",".join(card_ids),
            )
            return ()
        followups = tuple(getattr(batch, "continue_feature_ids", ()) or ())
        self._emit(
            "REVIEW done :: "
            f"cards={','.join(card_ids)} "
            f"processed={len(getattr(batch, 'processed', ()))} "
            f"escalated={len(getattr(batch, 'escalated', ()))} "
            f"skipped={len(getattr(batch, 'skipped', ()))} "
            f"failed={len(getattr(batch, 'failed', ()))} "
            f"continue={len(followups)}"
        )
        return followups

    def _release_stuck_cards(self, fid: uuid.UUID, *, code: str) -> tuple[str, ...]:
        """把这一单里"派发过、没跑完"的卡挪出 `RUNNING`（→ `ESCALATED`），返回挪了哪几张。

        **为什么必须另开一个会话**：走到这里说明主工作单元已经抛了，`new_session` 的 except 分支
        会 `rollback()` —— 在那个会话里改的卡态会被一起撤掉。2026-10-28 的现场就是这样：
        05:54 的 `unsupported_format` 之后 T05 在库里一直是 `RUNNING`，而之后三次续跑都读到
        "没有可派发的卡"（`cards=0`），整单停摆 6 小时，界面上一个能点的东西都没有。
        这与 `_trace()` 为什么自己开会话是同一条理由：**失败过这件事，以及失败留下的现场，都要活过回滚**。

        自己出错不外抛：挪不动卡只是"这次没救回来"，不该把归因换成一个更难读的错。
        """
        try:
            with session_module.new_session(self.session_factory) as session:
                rows = (
                    session.query(TaskCard)
                    .filter(TaskCard.feature_request_id == fid, TaskCard.status == dispatch_node.STARTED)
                    .order_by(TaskCard.id)
                    .all()
                )
                moved: list[str] = []
                for row in rows:
                    if realrun.escalate_card_after_failure(
                        session,
                        feature_request_id=fid,
                        task_id=str(row.id),
                        code=code,
                        detail=f"续跑失败（{code}）时这张卡仍在 {dispatch_node.STARTED}，没有任何节点会再碰它",
                        now=self.clock(),
                    ):
                        moved.append(str(row.id))
                return tuple(moved)
        except Exception as exc:  # noqa: BLE001 - 见 docstring 最后一段
            self._emit(f"RESUME release-failed :: {type(exc).__name__}: {exc}")
            return ()

    def _trace(self, feature_request_id: Any, *, action: str, **fields: Any) -> None:
        """一行 EN-14。自己开会话、自己收尾：主工作单元回滚之后这行仍然要留下。"""
        payload: Mapping[str, Any] = {
            "action": action,
            "feature_request_id": None if feature_request_id is None else str(feature_request_id),
            **fields,
        }
        try:
            with session_module.new_session(self.session_factory) as session:
                events.record(
                    session,
                    entity="feature_request",
                    entity_id=str(feature_request_id) if feature_request_id is not None else "unknown",
                    kind="APPROVAL",
                    actor_agent_id=RESUME_ACTOR,
                    payload=payload,
                    created_at=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 留痕写不进去不能反过来把续跑判成失败
            self._emit(f"RESUME trace-failed :: {action} :: {type(exc).__name__}: {exc}")

    def _emit(self, line: str) -> None:
        try:
            self.emit_to(f"[web-resume] {line}")
        except Exception:  # noqa: BLE001 - 诊断输出不许影响续跑
            pass
