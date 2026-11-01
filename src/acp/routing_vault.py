"""路由凭据的**进程内存**仓（路线 C，2026-10-31）。

为什么不在库里：`model_routing` 会随 `GET /requests/{id}` 回给浏览器；把 key 写进那一列等于
把凭据写进数据库与 HTTP 读面（宪法「二」明禁）。所以请求体里的 key 在这里按**需求单号**暂存，
路由列里只落 `key_fp`（sha256 前 10 位，用来判"同模型不同 key"），**不落 key、不回读**。

代价两条，明写：① 服务重启后该单的 key 丢失 ⇒ 用到时抛 `CredentialExpired`，提示重新提交；
② 本仓是单机单用户工具，仓的容量上限按"最近 200 单"淘汰最老的。多实例部署需要外部密钥管理，
不在本期。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping

#: 最多记多少单的凭据（超出按提交顺序淘汰最老的）。
MAX_REQUESTS = 200

_LOCK = threading.Lock()
_KEYS: dict[str, dict[str, str]] = {}
_ORDER: list[str] = []


class CredentialExpired(RuntimeError):
    """这一单的凭据不在进程内存里（重启过 / 淘汰了）⇒ 不猜、不回落环境变量以外的来源。"""


def remember(request_id: uuid.UUID | str, keys: Mapping[str, str]) -> None:
    """按席位记下 key。空字符串按"没给"处理。"""
    payload = {seat: value for seat, value in keys.items() if value}
    if not payload:
        return
    key = str(request_id)
    with _LOCK:
        _KEYS[key] = payload
        if key in _ORDER:
            _ORDER.remove(key)
        _ORDER.append(key)
        while len(_ORDER) > MAX_REQUESTS:
            _KEYS.pop(_ORDER.pop(0), None)


def key_for(request_id: uuid.UUID | str, seat: str) -> str | None:
    with _LOCK:
        return _KEYS.get(str(request_id), {}).get(seat)


def forget(request_id: uuid.UUID | str) -> None:
    key = str(request_id)
    with _LOCK:
        _KEYS.pop(key, None)
        if key in _ORDER:
            _ORDER.remove(key)


def size() -> int:
    with _LOCK:
        return len(_KEYS)
