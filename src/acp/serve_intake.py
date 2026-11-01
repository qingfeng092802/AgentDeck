"""网页受理之后的拆解跑者：`POST /requests` 落库之后把需求推到 A 类卡点。

**为什么要有这一格**：`POST /requests` 此前只写 EN-01 那一行，网页上提交需求之后永远不会有任务卡；
而 `acp.run` 的受理段又会**新建**一张需求单 —— 网页入口不能靠再跑一遍 CLI 来续上，否则同一句话
在库里会变成两张单。所以这里调 `run.run_intake_existing()`：同一行、同一份摘要，只把图推到挂起点。

**诚实边界**（与 `run.intake` 的仿真拆解器同一条）：

- 拆单器是 `StubOrchestrator`：固定四张卡（样式 + 三张页面），**不读需求正文**。
  网页上的"正在拆解"不能被读成"模型读懂了这句话"。
- 写进活动流的是 `APPROVAL / split_ready`（一行 EN-14）：它只说"卡与单据已经落库、可以批了"，
  不是审批结论本身；结论仍归 `POST /approvals/{id}/resolve`。
- **失败不往外抛**：后台任务抛出去只剩 uvicorn 一条日志，页面上什么都不会发生。失败写一行
  `split_failed`，带代号（不是凭据值），然后结束。
- 同一张需求单只跑一条：锁是进程内的（与 `serve_resumer` 同形），多进程部署下不成立 ——
  这是本机看板的边界，不假装它是分布式锁。
- **拆单确认在 `ACP_APPROVAL_MODE=auto` 下由规则免确认**（2026-10-28）：`apply_auto_pass()` 用
  `approval.policy.SPLIT_AUTO_RULE` 署名把那张 A 类单据签掉，然后**在同一个后台线程里**交给
  `graph_resumer` 续跑。这不是"把审批删了"，而是把它从"每次都要人点"改成"规则表决定"——
  EN-10 会多一行 `channel=AUTO`，`decided_by_principal` 记规则名，查得出是哪一档开关放的行。
"""

from __future__ import annotations

import datetime as dt
import inspect
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from acp import realrun, run
from acp.approval import policy as approval_policy
from acp.approval import repository as approval_repo
from acp.docs import PRD_SECTION, SECTIONS as DOC_SECTIONS
from acp.meeting import DEFAULT_ROUNDS, MEETING_SECTION, ROLE_SPECS, merge_into_digest
from acp.bus.orchestrator_writer import ORCHESTRATOR
from acp.persistence import events
from acp.persistence import session as session_module
from acp.persistence.models_core import FeatureRequest, TaskCard


#: `summary_digest` 里"客户追加的需求"那一格（owner 2026-10-28 第 2 条「增加需求」）。
#:
#: 一行一条，形如 `HH:MM｜追加的那句话`。**原文不动** —— `raw_input` 是"客户最初说的那句话"，
#: 被后续追加改写之后，"这一单是怎么来的"就查不回来了。存储上分开（这里），使用时合起来
#: （`_effective_input`）。
ADD_SECTION = "追加"

#: `summary_digest` 里"上一版 PRD"那一格。
#:
#: 为什么要有它：追加需求这一趟要把 `PRD` / `架构` / `令牌` 三格**清空**，理由在
#: `api/requests.py::extend_request` 里（不清的话前端会拿上一版 PRD 冒充这一版，
#: 而且 `stageOf()` 一见到文档就判"正在拆卡"，新的讨论反而看不见）。
#: 但清掉不等于不要了 —— PRD 正是这一趟的**修订底稿**，`_effective_input` 要从这里读。
#: 单独一格而不是留在原处打标记：读面（前端 `parseDocs`）只认那三个键，多一个"stale"标记
#: 就得让前端也认识它，而前端认识它的唯一用途就是忽略它。
PREV_DOCS_SECTION = "上一版"


def extend_digest(
    digest: Mapping[str, list[str]] | None,
    text: str,
    *,
    stamp: str,
) -> dict[str, list[str]]:
    """把一条追加需求并进 digest。**不改入参**（它是从 ORM 读出来的对象，就地改会让"写没写"难判断）。

    三件事，每条都对应屏上的一种假话：

    1. `追加` 加一行 `HH:MM｜那句话` —— 这是"这一单追加过需求"的唯一凭据
       （前端 `hasAdditions` 靠它判"这一趟是重开一场会"）；
    2. 清空 `会议`：上一场的台词留着，前端 `stageOf()` 一见到"轮数 × 4"条台词就判
       "会议已结束"，新的讨论反而看不见；
    3. 把 `PRD` / `架构` / `令牌` 挪进 `上一版`（**只留 PRD**）：不清的话前端 `parseDocs`
       读到的是上一版 PRD，屏上把旧版当这一版；清掉又会让这一趟丢掉修订底稿
       （`_effective_input` 要拿 PRD 拼提示词）。挪走两头都对。
       架构与令牌一并清掉：它们没有"底稿"作用，`_docs` 会整组重出。

    为什么它是自由函数而不是端点里的十行：这三条是 **digest 的形状**，归这一层管；
    而端点那一层要能被判据在**不连 PG** 的前提下量到（`tests/guards/test_extend_digest.py`）。
    """
    merged = {
        str(key): [str(item) for item in value]
        for key, value in (digest or {}).items()
        if isinstance(value, list)
    }
    merged.setdefault(ADD_SECTION, []).append(f"{stamp}｜{text}")
    merged[MEETING_SECTION] = []
    prev_prd = merged.pop(PRD_SECTION, None)
    for name in DOC_SECTIONS:
        merged.pop(name, None)
    if prev_prd:
        merged[PREV_DOCS_SECTION] = prev_prd
    return merged


class IntakeRefused(RuntimeError):
    """现场的问题（不是拆单器的问题）。`code` 是写进活动流那一行的归因词。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _accepts_on_turn(fn: Callable[..., object]) -> bool:
    """注入的会议跑者收不收 `on_turn`。

    为什么要问一句：`meeting` 是**注入位**，判据里换的是 `lambda raw: [...]` 这种只收一个参数的替身。
    硬传 `on_turn=` 会让它抛 `TypeError`，而那个异常会被当成"会议失败" ——
    一次签名不合就把四段真台词全丢了，是最难查的那类失败。问不出来就当不收。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD or param.name == "on_turn" for param in params
    )


@dataclass
class WebIntakeRunner:
    """`app.state.intake_runner` 的生产实现。判据把下游换成记账替身，生产不传就是真拆解。"""

    session_factory: Callable[[], Session]
    bus_root: Path
    clock: Callable[[], dt.datetime] = utcnow
    emit_to: Callable[[str], object] = _stderr
    #: 四岗位会议（2026-10-27）。`None` = 这一档不接会议（判据里的裸 app、或没配模型凭据）。
    #: 签名 `(raw_input, *, on_turn=None) -> list[Turn]`；失败抛 `MeetingUnavailable`，
    #: 本类捕获后只留痕、不挡拆单。`on_turn` 是可选的增量交付点（2026-10-28）：
    #: 每轮说完回调一次，`_meet` 借此**逐轮落库**，网页会议桌才看得见四个岗位依次发言。
    #: 只收一个参数的老替身也接受 —— 见 `_accepts_on_turn`。
    meeting: Callable[..., object] | None = None
    #: 真拆单工厂：`(raw_input) -> 拆单器`。`None` = 用 Stub（判据里的裸 app）。
    #: 拆单器失败会自己回落（见 `orchestrate/llm_split.py`），这里只负责把异常挡在拆单之外。
    splitter: Callable[[str], object] | None = None
    #: 立项文档节点（2026-10-28 owner 选 B）。`None` = 这一档不落文档（判据里的裸 app）。
    #: 签名 `(raw_input, 会议台词) -> ProjectDocs`；失败抛 `DocumentUnavailable`，
    #: 本类捕获后只留痕、不挡拆单 —— 与会议同一条口径。
    documents: Callable[[str, list[str]], object] | None = None
    #: 审批门档位（`ACP_APPROVAL_MODE`）。`auto` = 拆单完成即由规则免确认并自动续跑。
    #: 默认 `manual`：装配方不传就是 Phase 16 的逐字行为（单据停在 `PENDING` 等人）。
    approval_mode: str = approval_policy.APPROVAL_MODE_MANUAL
    #: 续跑者（`serve_resumer.WebResumer` 同形）。`None` = 这一档没有续跑者，
    #: 那么免确认之后**不假装跑下去了** —— 单据是 `APPROVED`，留痕写 `resume_skipped`。
    resumer: Callable[..., object] | None = None
    #: 这场会开几轮（`ACP_MEETING_ROUNDS`，见 `meeting/service.py::resolve_rounds`）。
    #: 只用来写「会议轮数」那一格与打印进度：轮数本身由注入的 `meeting` 调用面执行。
    meeting_rounds: int = DEFAULT_ROUNDS

    _locks: dict[str, threading.Lock] = field(default_factory=dict, init=False, repr=False)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __call__(self, *, feature_request_id: Any) -> None:
        """`POST /requests` 后台唯一的调用面。**永不抛** —— 失败走留痕。"""
        if feature_request_id is None:
            return
        fid = feature_request_id if isinstance(feature_request_id, uuid.UUID) else uuid.UUID(str(feature_request_id))
        key = str(fid)
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            self._trace(fid, action="split_skipped", reason="already_running")
            return
        try:
            self._run(fid)
        finally:
            lock.release()

    def _effective_input(self, request: FeatureRequest) -> str:
        """这一趟真正喂给会议 / 文档 / 拆单的输入。

        **原文不动**：`raw_input` 是"客户最初说的那句话"，被后续追加改写之后，
        "这一单是怎么来的"就查不回来了。追加的话另存 `summary_digest["追加"]`，
        这里在读出时拼上去 —— 存储上分开，使用时合起来。

        追加过的那一趟还要带上**上一版 PRD**：owner 2026-10-28 第 2 条要的是
        "在已产生的 prd 的基础上增加"，而不是从零重写一份。没有这一段，四岗位会当新需求重新讨论，
        第一版里已经谈定的东西全部消失。
        """
        raw = str(request.raw_input)
        digest = request.summary_digest or {}
        adds = [str(item) for item in (digest.get(ADD_SECTION) or [])]
        if not adds:
            return raw
        raw += "\n\n【客户追加的需求】\n" + "\n".join(adds)
        # 上一版 PRD：追加时后端把它从 `PRD` 挪到了 `PREV_DOCS_SECTION`（见那个常量的说明）。
        # 回落到 `PRD` 是为了兼容"先追加、后升级"的单 —— 那种单的 PRD 还在原位。
        prev = digest.get(PREV_DOCS_SECTION) or digest.get("PRD") or []
        if prev:
            raw += "\n\n【上一版 PRD（这是修订，不是重写：没被追加需求影响的部分要保留）】\n" + str(prev[0])
        return raw

    @staticmethod
    def _card_count(session: Session, fid: uuid.UUID) -> int:
        return int(session.query(TaskCard).filter(TaskCard.feature_request_id == fid).count())

    def _meet(self, session: Session, request: FeatureRequest, fid: uuid.UUID, summary: dict[str, Any]) -> None:
        """先开会、再拆单。**会议失败不挡拆单** —— 把"模型没连上"表现成"需求提交失败"是另一种假话。

        台词写进 `summary_digest` 的「会议」段（已有 JSONB 列，不新开表、不加迁移）：
        形状是 `role｜text` 的行数组，与那份 JSON 的"分区 → 行"结构同形。
        端点回体与 `GET /requests/{id}` 都用同一份 digest，前端不另取一次。

        **逐轮落库（2026-10-28 修）**：四轮是串行的、加起来 1–3 分钟，而原先只有"四轮全跑完"
        这一个提交点 —— 那三分钟里 `GET /requests/{id}` 的「会议」段是空的，网页会议桌上
        四个岗位一句话都不出现，只剩一句"会议进行中"。现在每轮说完就 `commit` 一次，
        屏上看到的是"总管说完 → 产品正在说 → 技术正在说"，与模型的实际进度同拍。

        这不是"逐字流式"：一次 `chat()` 拿回整段才可能落库，字是整段到的。
        逐字的观感由前端做（见 `web/src/components/studio/StudioScreen.tsx` 的 `useTypewriter`），
        那一层不许自称"模型正在吐字"。
        """
        if self.meeting is None:
            return
        landed: list[Any] = []

        def land(turn: Any, said: list[Any]) -> None:
            """一轮说完就落一次库。留痕排在 commit 之后：`meeting_turn` 说"这一轮可读了"，
            那它必须先真的可读（与 `meeting_ready` 同一条口径）。"""
            landed.append(turn)
            request.summary_digest = merge_into_digest(  # type: ignore[arg-type]
                request.summary_digest, said, rounds=self.meeting_rounds
            )
            session.flush()
            session.commit()
            self._trace(
                fid,
                action="meeting_turn",
                turn=len(said),
                role=str(getattr(turn, "role_id", "")),
                chars=len(str(getattr(turn, "text", ""))),
            )
            total = len(ROLE_SPECS) * self.meeting_rounds
            self._emit(f"MEETING turn {len(said)}/{total} :: {getattr(turn, 'role_id', '?')}")

        try:
            raw = self._effective_input(request)
            if _accepts_on_turn(self.meeting):
                turns = list(self.meeting(raw, on_turn=land))  # type: ignore[call-arg]
            else:
                # 老签名：整段一次性返回。屏上就没有"逐岗出现"，但台词一条不少。
                self._trace(fid, action="meeting_no_stream", code="runner_without_on_turn")
                turns = list(self.meeting(raw))  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001 - 会议的任何失败都不该把拆单带走
            code = str(getattr(exc, "code", type(exc).__name__))
            self._emit(f"MEETING fail :: {code}")
            # 已经说完的那几轮**留在库里**：它们真的发生过。前端据此显示"说到第 2 轮就断了"。
            self._trace(fid, action="meeting_failed", code=code, error=str(exc)[:200], landed=len(landed))
            return
        if not turns:
            self._trace(fid, action="meeting_failed", code="meeting_empty")
            return
        # 兜底再并一次：万一注入的跑者不支持 `on_turn`（老签名），这条路径保证台词仍然落库。
        request.summary_digest = merge_into_digest(  # type: ignore[arg-type]
            request.summary_digest, turns, rounds=self.meeting_rounds
        )
        session.flush()
        # **立刻提交**：台词是"这场会已经开过了"的事实，不该被后面拆单的成败拖着。
        #
        # 2026-10-28 实测踩到：会议 16:21:59 结束，整条受理事务 16:25:25 才提交 ——
        # 那 3 分半里 `GET /requests/{id}` 读回 0 条台词（未提交的写对别的连接不可见），
        # 屏上就是"会议进行中"一动不动，而模型其实早就说完了。
        # 提交在这里的代价只是"拆单失败时会议仍在库里"，那本来就是真发生过的事。
        session.commit()
        summary["meeting"] = f"{len(turns)}轮"
        # 留痕排在提交之后：`meeting_ready` 说"台词可读了"，那它必须先真的可读。
        self._trace(fid, action="meeting_ready", turns=len(turns))
        self._emit(f"MEETING done :: turns={len(turns)}")

    def _docs(
        self,
        session: Session,
        request: FeatureRequest,
        fid: uuid.UUID,
        summary: dict[str, Any],
        project_dir: Path,
    ) -> object | None:
        """落三份立项文档（PRD / 架构 / 令牌）。**失败不挡拆单**。

        与 `_meet` 同形，两处细节是承重的：

        1. `session.commit()` 排在留痕之前 —— 文档写得再对，只要还在未提交的事务里，
           `GET /requests/{id}` 就读不到，屏上那三格就一直是空的（会议那一步踩过一模一样的坑）。
        2. 文件是**投影**，digest 才是事实。文件写失败只留痕，不改 `summary` 里的结论 ——
           否则会出现"库里说文档好了、盘上没有"或者反过来，两处口径各说一套。
        """
        if self.documents is None:
            return None
        try:
            said = list(((request.summary_digest or {}).get("会议") or ()))
            docs = self.documents(str(request.raw_input), said)
        except Exception as exc:  # noqa: BLE001 - 文档的任何失败都不该把拆单带走
            code = str(getattr(exc, "code", type(exc).__name__))
            self._emit(f"DOCS fail :: {code}")
            self._trace(fid, action="docs_failed", code=code, error=str(exc)[:200])
            return None

        merged = dict(request.summary_digest or {})
        merged.update(docs.digest())  # type: ignore[attr-defined]
        request.summary_digest = merged  # type: ignore[assignment]
        session.flush()
        session.commit()

        written: list[str] = []
        try:
            folder = Path(project_dir) / "results" / "docs"
            folder.mkdir(parents=True, exist_ok=True)
            for name, body in docs.files().items():  # type: ignore[attr-defined]
                (folder / name).write_bytes(body.encode("utf-8"))
                written.append(name)
        except Exception as exc:  # noqa: BLE001 - 文件是投影，写不出来只留痕，绝不改结论
            self._trace(fid, action="docs_files_failed", code=type(exc).__name__)

        prd = getattr(docs, "prd", {}) or {}
        summary["docs"] = "prd+arch+tokens"
        extras = tuple(getattr(docs, "extras", ()) or ())
        self._trace(
            fid,
            action="docs_ready",
            pages=len(prd.get("pages") or ()),
            acceptance=len(prd.get("acceptance") or ()),
            files=",".join(written),
            # 模型多吐的字段名只留痕、不拒单（见 `docs.ProjectDocs.extras`）。
            # 记下来是因为它是有用的信号：某个字段反复出现，说明 prompt 该写清楚，而不是模型跑偏。
            extra=",".join(extras)[:200],
        )
        self._emit(f"DOCS done :: pages={len(prd.get('pages') or ())} files={len(written)}")
        return docs

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def is_running(self, feature_request_id: Any) -> bool:
        """这一单此刻有没有人在跑（**锁是唯一的真相**）。

        为什么问锁而不是问 `status`：续跑者（`resumer`）是在 `_run` 里**同步**跑的，
        所以"执行 14 张卡"的那十分钟里锁也held着，而 `status` 早就从 `INTAKE` 翻成
        `SPLIT`/`RUNNING` 了。只看 `status` 会得出"没人在跑"的结论 —— 2026-10-28 第十轮
        真链路就是这么翻的：`POST /extend` 放行、digest 改完（追加写了、会议清了、
        文档挪进上一版），而 `__call__` 那边 `acquire(blocking=False)` 拿到 False，
        留痕写 `split_skipped/already_running` 就返回了 ⇒ 半应用状态，屏上永远"会议无产出"。

        只读、不建锁：查询本身不该在 `_locks` 里留下一把永远没人用的锁。
        """
        with self._locks_guard:
            lock = self._locks.get(str(feature_request_id))
        return bool(lock is not None and lock.locked())

    def _run(self, fid: uuid.UUID) -> None:
        summary: dict[str, Any] = {}
        self._emit(f"INTAKE start :: request={fid}")
        self._trace(fid, action="split_started")
        try:
            with session_module.new_session(self.session_factory) as session:
                request = session.get(FeatureRequest, fid)
                if request is None:
                    raise IntakeRefused(
                        f"EN-01 里没有这条需求单 ⇒ 没有可拆的对象：feature_request_id={fid}",
                        code="feature_request_missing",
                    )
                project_key = str(request.project_key)
                summary["project_key"] = project_key
                self._meet(session, request, fid, summary)
                # 与 CLI 的受理段同一次建现场：网页这侧不要求人先跑过 `acp.realrun`。
                project_dir = realrun.ensure_archive_root(project_key, self.bus_root, emit_to=self.emit_to)
                # 文档排在拆单**之前**：拆单要么读这份 PRD，要么退回只读台词。
                self._docs(session, request, fid, summary, project_dir)
                # **有卡就不重拆**（2026-10-28 追加需求）。同一张需求单重跑立项时，PRD 会重出，
                # 但已经拆出来的卡是"上一版 PRD 的结论"，重拆要么撞 `AlreadyAdvanced`、
                # 要么拆出第二套卡 —— 两种都错。所以这里按**事实**（库里有没有卡）分叉，
                # 不另设开关：开关会漂，事实不会。
                cards = self._card_count(session, fid)
                if cards:
                    summary["cards_kept"] = cards
                    self._emit(f"INTAKE keep :: {cards} cards already exist, prd refreshed only")
                    self._trace(fid, action="split_skipped", reason="cards_exist", cards=cards, **summary)
                    return
                orchestrator = None
                if self.splitter is not None:
                    try:
                        orchestrator = self.splitter(str(request.raw_input))
                    except Exception as exc:  # noqa: BLE001 - 造不出拆单器就用 Stub，不挡受理
                        self._trace(fid, action="split_llm_skipped", code=type(exc).__name__)
                report = run.run_intake_existing(
                    fid,
                    transport=realrun.FROZEN_SEGMENT_GATE,
                    bus_root=self.bus_root,
                    session=session,
                    now=self.clock(),
                    orchestrator=orchestrator,  # type: ignore[arg-type]
                )
                # 拆单走的是模型还是回落：`last_error` 是拆单器自己写的归因码，不在这里猜。
                fallback = getattr(orchestrator, "last_error", None)
                if orchestrator is not None:
                    self._trace(fid, action="split_fallback" if fallback else "split_llm",
                                code=str(fallback) if fallback else "ok")
                summary["ticket_id"] = str(report.ticket_id)
                summary["cards"] = ",".join(report.card_ids)
                summary["request_status"] = report.request_status
                # 这一行是 SSE 的触发点：卡与单据在同一事务里，读者看到它时两者都已可见。
                events.record(
                    session,
                    entity="feature_request",
                    entity_id=str(fid),
                    kind="APPROVAL",
                    actor_agent_id=ORCHESTRATOR.role_id,
                    payload={
                        "action": "split_ready",
                        "stage": "A_SPLIT",
                        "ticket_id": str(report.ticket_id),
                        "cards": list(report.card_ids),
                        "request_status": report.request_status,
                    },
                    created_at=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 后台任务抛出去就没人看得见了（模块头第 4 条）
            code = str(getattr(exc, "code", type(exc).__name__))
            detail: dict[str, Any] = {"code": code, "error": str(exc)}
            name = getattr(exc, "name", None)
            if name:
                detail["name"] = str(name)
            self._emit(f"INTAKE fail :: {code} :: {exc}")
            # 先把状态改掉，再留痕（U6，2026-10-28）。原来只写一行 `split_failed`，需求单永远停在
            # `INTAKE` —— 而 `INTAKE` 在界面上读作"正在拆解"。于是一次失败看起来像"卡住了"：
            # 会议桌说"会议已锁定方案"，文档格一直画骨架屏，而真正发生的是这件事已经结束了。
            #
            # `FAILED` 是 `feature_request_status` 枚举里本来就有的取值（EN-01），不需要迁移；
            # 前端 `stageOf()` 见到"状态不是 INTAKE 却没有文档"会判 `stalled` 并收手，
            # 所以这一行改完，屏上从"永远正在拆解"变成"停在这里了"。
            self._mark_failed(fid, code, str(exc))
            self._trace(fid, action="split_failed", **detail, **summary)
        else:
            self._emit("INTAKE done :: " + " ".join(f"{key}={value}" for key, value in summary.items()))
            self._trace(fid, action="split_finished", **summary)
            # 免确认排在 `split_finished` 之后、**另开一个会话**：
            # 主工作单元到这里已经提交，`graph_resumer` 与这一段读到的都必须是"已提交的 APPROVED"。
            # 塞进同一个会话里写，等于让另一条连接去看一个还没提交的结论。
            #
            # 档位判断**在 `_auto_pass_split` 里面**，不在这里：那一个方法是唯一会动审批状态的地方，
            # 它自己守自己的前提，就不存在"调用方忘了判"这个形状（判据见
            # tests/guards/test_approval_mode_auto.py 的 test_the_switch_is_checked_inside_the_signer）。
            if self._auto_pass_split(fid, report.ticket_id, summary):
                self._resume_after_auto_pass(fid)

    def _auto_pass_split(self, fid: uuid.UUID, ticket_id: uuid.UUID, summary: dict[str, Any]) -> bool:
        """`ACP_APPROVAL_MODE=auto`：用规则名把这张 A 类单据签掉。返回"签成了没有"。

        为什么复用 `approval.repository.apply_auto_pass()` 而不是自己 UPDATE 一行状态：

        - 它是本仓为"规则表免确认"（FR-046 / FR-048）留的**唯一写者**，只允许 A 类，
          并且必须由一条规则署名（`decided_by` 空就抛）—— 这两条拦的是"绕过服务层直调仓储"；
        - 它会写一行 `channel=AUTO` 的 EN-10 到达流水。`resolution_channel` 的 `AUTO` 值
          只有它能写（`HUMAN_CHANNELS` 里没有它），所以审计面永远分得清
          "人批的（DASHBOARD / CLI）"与"规则免确认的（AUTO）"。自己改状态就把这条区分抹了。

        **签不掉不算失败**：单据可能已经被别人定过（`NOT_PENDING`），或者根本不存在。
        那时如实留痕、返回 `False`，让这一单回到"等人批"的老路上 —— 把"免确认没成"
        表现成"整单失败"是另一种假话。`ApprovalMisuse`（非 A 类 / 没署名）才是真 bug，
        但也不在这里抛：这是后台线程，抛出去只剩一条 uvicorn 日志。

        **档位判断在这一格里面**：本方法是全仓唯一会动审批状态的地方（`serve_intake` 里没有
        第二处 `update(`，判据钉着），前提就该由它自己守 —— 放在调用方那边，
        "新加一个调用点忘了判"就是一次无人批过的批准。
        """
        if not approval_policy.auto_split_confirm(self.approval_mode):
            return False
        try:
            with session_module.new_session(self.session_factory) as session:
                outcome = approval_repo.apply_auto_pass(
                    session,
                    ticket_id=ticket_id,
                    decided_by=approval_policy.SPLIT_AUTO_RULE,
                    now=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 后台线程里抛出去没人看得见
            code = str(getattr(exc, "code", type(exc).__name__))
            summary["auto_pass"] = f"failed:{code}"
            self._trace(fid, action="split_auto_pass_failed", code=code, error=str(exc)[:200])
            return False

        applied = bool(outcome.applied)
        summary["auto_pass"] = "applied" if applied else str(outcome.code or outcome.state)
        self._trace(
            fid,
            action="split_auto_passed" if applied else "split_auto_pass_skipped",
            ticket=str(ticket_id),
            state=str(outcome.state),
            code=str(outcome.code or ""),
            # 规则名进留痕：EN-10 里那一行的 `reason_text` 也是它，两处对得上才查得回去。
            rule=approval_policy.SPLIT_AUTO_RULE,
        )
        return applied

    def _resume_after_auto_pass(self, fid: uuid.UUID) -> None:
        """免确认之后立刻把这一单交给续跑者 —— 不等下一次刷新，也没有人再点一下。

        `graph_resumer` 与「批准」那条路是**同一个对象、同一个入口**（`serve.build_app` 造一次，
        两处共用），所以这里不另写一份"批准后怎么跑"的驱动。它自己保证"同一张单只有一条续跑链"。

        没有注续跑者时**如实留痕**、不假装跑下去了：单据是 `APPROVED`，但没人接着跑 ——
        这个状态必须能被读出来（口径同 `api/approvals.py::_resume` 的"装配方没注入就返回 None"）。
        """
        if self.resumer is None:
            self._trace(fid, action="resume_skipped", reason="resumer_not_configured", conclusion="approve")
            return
        try:
            self.resumer(feature_request_id=fid, conclusion="approve")
        except Exception as exc:  # noqa: BLE001 - 续跑者按契约永不抛，这里只兜它违约
            self._trace(fid, action="resume_failed", code=type(exc).__name__, error=str(exc)[:200])

    def _mark_failed(self, feature_request_id: uuid.UUID, code: str, error: str) -> None:
        """把需求单改成 `FAILED`。自己开会话、自己收尾 —— 与 `_trace` 同一条理由。

        **只在状态还是 `INTAKE` 时改**：受理跑的过程中可能有别的写者把它推到 `SPLIT`（比如重试
        与上一跑交叠），把 `SPLIT` 退回 `FAILED` 会抹掉一个已经成立的事实。改不动就留痕说明，
        不硬写。

        为什么失败原因要进 `event_log` 而不是新加一列：`last_error` 是**一次**的事实，不是这条
        需求单的属性；为它加列就得为"重试成功后清空"再写一条规则。留痕里已经有 `split_failed`
        那一行的 `error`，读面按它取最近一条就够了。
        """
        try:
            with session_module.new_session(self.session_factory) as session:
                request = session.get(FeatureRequest, feature_request_id)
                if request is None:
                    return
                status = getattr(request, "status", None)
                if status is not None and str(status) != "INTAKE":
                    self._emit(
                        f"INTAKE fail-not-marked :: 状态是 {status} 不是 INTAKE :: {code}"
                    )
                    return
                request.status = "FAILED"
                events.record(
                    session,
                    entity="feature_request",
                    entity_id=str(feature_request_id),
                    kind="APPROVAL",
                    actor_agent_id=ORCHESTRATOR.role_id,
                    payload={
                        "action": "intake_failed",
                        "feature_request_id": str(feature_request_id),
                        "code": code,
                        "error": error[:400],
                    },
                    created_at=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 改状态失败不能反过来把受理再炸一次
            self._emit(f"INTAKE mark-failed-broken :: {type(exc).__name__}: {exc}")

    def _trace(self, feature_request_id: uuid.UUID, *, action: str, **fields: Any) -> None:
        """一行 EN-14。自己开会话、自己收尾：主工作单元回滚之后这行仍然要留下。"""
        payload = {
            "action": action,
            "feature_request_id": str(feature_request_id),
            **fields,
        }
        try:
            with session_module.new_session(self.session_factory) as session:
                events.record(
                    session,
                    entity="feature_request",
                    entity_id=str(feature_request_id),
                    kind="APPROVAL",
                    actor_agent_id=ORCHESTRATOR.role_id,
                    payload=payload,
                    created_at=self.clock(),
                )
        except Exception as exc:  # noqa: BLE001 - 留痕写不进去不能反过来把拆解判成失败
            self._emit(f"INTAKE trace-failed :: {action} :: {type(exc).__name__}: {exc}")

    def _emit(self, line: str) -> None:
        try:
            self.emit_to(f"[web-intake] {line}")
        except Exception:  # noqa: BLE001 - 诊断输出不许影响拆解
            pass
