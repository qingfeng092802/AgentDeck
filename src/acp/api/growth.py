"""M2-001 的档案面：读两份进化档案、存回去（owner 2026-10-19 MVP-2 第三步 ①）。

这一支 router 只做**文件面**的读写，不碰库：档案的事实态就在那两个 `.md` 里，
再往 EN-14 抄一份就是第二个载体（而且 `event_log_kind` 里没有"档案改动"这个值，
加一个要开迁移 —— 与本轮的零迁移纪律相冲，已登记为待裁）。
改动本身的可追溯性由两处承担：文件内容自己，加上存档点（`archive/cadence.py` 那一拍）。

三处刻意的写法：

1. **写必须带身份头**。名字与审批面同一个（`api/approvals.py::PRINCIPAL_HEADER`，这里不重述字面量）。
   它挡的是"披错身份误操作"，**挡不住冒充** —— 本仓现阶段无鉴权，这条限制与审批那边同形，
   不留成一句没人读的注释。
2. **可选的 `expected_sha256` 是覆盖保护，不是乐观锁的完整实现**。给了就必须等于当前内容，
   否则 409 `archive_stale`；不给就直写（单人本机场景下"我就是想覆盖"是合法意图，
   把它逼成必填会让"忘了带"变成一次 409 而不是"按你说的做"）。
3. **保存失败就是失败**，返回 502/500 之类"看起来存上了"的形状一律不许出现：
   过界与含凭据形态都是 422 并带上拒写原因（`growth.store.GrowthError` 的消息）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from acp.api.approvals import PRINCIPAL_HEADER
from acp.growth import store

router = APIRouter()


class ArchiveRow(BaseModel):
    """一份档案的读回形状：正文 + 指纹 + 上界（上界回显出来，前端才敢在本地提示"还剩多少"）。"""

    name: str
    content: str
    sha256: str
    max_bytes: int


class ArchivePage(BaseModel):
    archives: list[ArchiveRow]
    #: 注入块的两段标题。前端拿它标注"这两段是要全文进报文的那两段"，
    #: 而不是自己再抄一份字面量 —— 标题改了，这里跟着变，判据会指出来。
    inject_headers: list[str]


class ArchiveWrite(BaseModel):
    """请求体：全文 + 可选的"我看到的指纹"。

    `extra=\"forbid\"` 是白名单的强制点（口径同 `api/tasks.py::TaskPatch`）：想改哪份档案写在路径上，
     body 里多出来的键没人解析 ⇒ 422 并点名它，而不是"收下但忽略"。
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    expected_sha256: str | None = None


def _principal(request: Request) -> str:
    """写操作要求身份头。缺失 ⇒ 403；缺省不许被解释成"匿名允许"（口径同 `api/approvals.py::_principal`）。"""
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        raise HTTPException(status_code=403, detail=f"principal_not_allowed：请求头 {PRINCIPAL_HEADER} 缺失")
    return principal


def _row(name: str) -> ArchiveRow:
    try:
        content = store.read(name)
    except store.GrowthError as exc:
        raise HTTPException(status_code=503, detail=f"archive_unavailable：{exc}") from None
    return ArchiveRow(
        name=name,
        content=content,
        sha256=store.digest_of(content),
        max_bytes=store.MAX_ARCHIVE_BYTES,
    )


@router.get("/growth", response_model=ArchivePage)
def read_archives() -> ArchivePage:
    """两份档案的全文与指纹（看板的事实库 / 错题本面板读这一条）。"""
    return ArchivePage(
        archives=[_row(name) for name in store.ARCHIVES],
        inject_headers=[store.PROFILE_HEADER, store.MISTAKES_HEADER],
    )


@router.put("/growth/{name}", response_model=ArchiveRow)
def write_archive(name: str, body: ArchiveWrite, request: Request) -> ArchiveRow:
    """整份覆盖写。路径里只允许那两个名字，别的 422 —— 见 `store.ARCHIVES` 的封闭集合理由。"""
    _principal(request)
    if name not in store.ARCHIVES:
        raise HTTPException(
            status_code=422,
            detail=f"archive_name_unknown：只接受 {list(store.ARCHIVES)}（收到 {name!r}）",
        )
    if body.expected_sha256 is not None:
        current = _row(name)
        if body.expected_sha256 != current.sha256:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"archive_stale：你申报的是 {body.expected_sha256[:16]}… 而库里现在是 "
                    f"{current.sha256[:16]}…（两个指纹不同形 ⇒ 请先重读再改，本端点不做'合并两份编辑'）"
                ),
            )
    try:
        digest = store.write(name, body.content)
    except store.GrowthError as exc:
        raise HTTPException(status_code=422, detail=f"archive_rejected：{exc}") from None
    return ArchiveRow(name=name, content=body.content, sha256=digest, max_bytes=store.MAX_ARCHIVE_BYTES)
