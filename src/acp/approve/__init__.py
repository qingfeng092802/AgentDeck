"""T056：文本通道 `python -m acp.approve`（FR-042｜R-26 ②）。

一条硬约束：**CLI 不自己判状态、不自己写状态**，它只做"把命令翻译成一次 `POST
/approvals/{ticket_id}/resolve`"。看板与文本两条通道因此打的是同一个端点、同一份 CAS、同一套错误码
（`contracts/approval-api.md §1` 末段"由本契约的 CAS 决定唯一生效者"）。
凡是"CLI 里顺手读一下单据然后直接改"的写法，都会造出第二条状态路径 —— 那正是 FR-042 禁的东西。

三个必须交代清楚的实现决定：

1. **命令接受卡号或单据号两种写法**（契约 §1 的示例是 `/approve T01`，而端点按 `ticket_id` 寻址）。
   给一个看起来像 UUID 的参数 ⇒ 直接寻址；否则先 `GET /approvals?state=pending` 按卡号找那张未决单。
   **找不到就报错，不"挑一张"**：替用户挑一张待批单去批准，是最典型的越权形状。
2. **`--principal` 是必填项，且它是自报身份**。MVP-1 没有认证（认证与审批者集合的归属归 M2），
   所以 403 `principal_not_allowed` 挡的是误操作而不是冒充 —— 这条限制在 `api/approvals.py` 的模块头
   也写着，两处同口径。
3. **`--base-url` 没有默认值**。留一个 `http://127.0.0.1:8000` 的默认值等于把"端口归谁定"这件事
   写进代码，而装配点（T123）还没落地；缺它就报错退出 2，比连到一个人没打算连的地址好。
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from acp.config import install_logging_filter

#: 命令词 → 契约 §3 的 `verdict` 取值。三对一是刻意的：文本侧要短词，线上侧要域值。
COMMANDS: dict[str, str] = {
    "approve": "approve",
    "reject": "reject",
    "needinfo": "need_info",
}

#: 每个命令要求的补充参数名（`reject` 必须带理由、`needinfo` 必须带追问内容，FR-040）。
REQUIRES: dict[str, str] = {"reject": "reason", "needinfo": "ask"}

CHANNEL = "CLI"


class CliUsage(ValueError):
    """命令行本身不合法。退出码 2，与"服务端拒了"（1）分开，好让脚本分清是谁的错。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acp.approve",
        description="审批文本通道：与看板打同一个 resolve 端点（FR-042）",
    )
    parser.add_argument("command", help="approve / reject / needinfo（可带前导斜杠）")
    parser.add_argument("target", help="单据 UUID，或待批卡的卡号（如 T01）")
    parser.add_argument("--reason", default="", help="reject 的理由（reject 时必填）")
    parser.add_argument("--ask", default="", help="needinfo 的追问内容（needinfo 时必填）")
    parser.add_argument("--principal", required=True, help="提交者标识（自报身份，见模块头第 2 条）")
    parser.add_argument(
        "--base-url",
        default=None,
        help="看板后端地址，例 http://127.0.0.1:8000（不带 client 时必填，见模块头第 3 条）",
    )
    parser.add_argument("--version", type=int, default=None, help="客户端读到的单据版本（CAS）")
    return parser


def resolve_body(command: str, reason: str, ask: str, version: int | None) -> dict[str, Any]:
    """把命令词翻成契约 §3 的请求体。缺补充参数即 `CliUsage`（不服务端猜）。"""
    verdict = COMMANDS.get(command.lstrip("/").lower())
    if verdict is None:
        raise CliUsage(f"不认识的动作 {command!r}，可写的只有：{sorted(COMMANDS)}")
    required = REQUIRES.get(command.lstrip("/").lower())
    text = {"reason": reason, "ask": ask}.get(required, "")
    if required and not text.strip():
        raise CliUsage(f"{command} MUST 带 --{required}（FR-040：驳回要留理由、追问要留内容）")
    return {
        "verdict": verdict,
        "reason_or_ask": text,
        "version": version,
        "channel": CHANNEL,
    }


def find_pending_ticket(client: httpx.Client, principal: str, card_id: str) -> tuple[str, int]:
    """按卡号找那张未决单（`--version` 也要从这张单上读，见 `_ticket` 的注释）。"""
    response = client.get(
        "/approvals", params={"state": "PENDING"}, headers={"x-acp-principal": principal}
    )
    if response.status_code >= 400:
        raise CliUsage(f"列未决单失败：HTTP {response.status_code}")
    matches = [row for row in response.json() if row.get("task_card_id") == card_id]
    if not matches:
        raise CliUsage(
            f"卡 {card_id!r} 没有未决审批单 ⇒ 不替你挑一张别的单批"
        )
    if len(matches) > 1:
        raise CliUsage(
            f"卡 {card_id!r} 有 {len(matches)} 张未决单 ⇒ 与 FR-045"
            "「同一任务同时只允许一个未决卡点」冲突，先查清再说"
        )
    return str(matches[0]["ticket_id"]), int(matches[0]["version"])


def _ticket(client: httpx.Client, principal: str, target: str) -> tuple[str, int]:
    """返回 `(单据号, 服务端当前的版本)`。

    版本不是可选装饰：契约 §3 要求 `resolve` 带 `version` 做 CAS。CLI 不读版本就只能让用户自己填，
    而用户填的很可能是**他上次看到看板时**的那个号 —— 那正是 `stale_version` 的设计场景，
    所以这里默认帮它读一次，`--version` 留给"我就是要拿这个号去撞"的用法。
    """
    try:
        return str(uuid.UUID(target)), None  # 调用方没带 --version 时再补读
    except ValueError:
        return find_pending_ticket(client, principal, target)


def _report(ticket_id: str, response: httpx.Response) -> dict[str, Any]:
    """把一次 resolve 的结果压成一行 JSON 打给终端。

    细节有两种形状：成功响应是**顶层**字段，错误响应 FastAPI 放在 **`detail`** 里
    （契约 §3 的"后到的结论"那一段就把 `applied:false` 写在响应体里）。两种都读，
    读不到就是空 —— 不猜。
    """
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    top = payload if isinstance(payload, dict) else {}
    return {
        "ticket_id": ticket_id,
        "http": response.status_code,
        "applied": top.get("applied", detail.get("applied")),
        "state": top.get("state", detail.get("state")),
        "logged_as": top.get("logged_as", detail.get("logged_as")),
        "code": detail.get("code", top.get("code") if "code" in top else None),
    }


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    install_logging_filter()
    args = _parser().parse_args(argv)
    owns_client = client is None
    if owns_client:
        if not args.base_url:
            print("usage error: 没带 client 就 MUST 给 --base-url（本模块不猜后端地址）")
            return 2
        client = httpx.Client(base_url=args.base_url)
    try:
        body = resolve_body(args.command, args.reason, args.ask, args.version)
        ticket_id, observed_version = _ticket(client, args.principal, args.target)
        if body["version"] is None:
            if observed_version is None:
                probe = client.get(
                    f"/approvals/{ticket_id}", headers={"x-acp-principal": args.principal}
                )
                if probe.status_code >= 400:
                    print(json.dumps(_report(ticket_id, probe), ensure_ascii=False))
                    return 1
                observed_version = int(probe.json()["version"])
            body["version"] = observed_version
        response = client.post(
            f"/approvals/{ticket_id}/resolve",
            json=body,
            headers={"x-acp-principal": args.principal},
        )
        print(json.dumps(_report(ticket_id, response), ensure_ascii=False))
        return 0 if response.status_code < 400 else 1
    except CliUsage as exc:
        print(f"usage error: {exc}")
        return 2
    except httpx.TransportError as exc:
        # 只报异常类名：httpx 的连接错误消息里带完整 URL（含 userinfo 与端口），
        # 让它裸抛就等于把后端地址抄进终端与日志。地址是操作者自己给的，回显它没有信息量。
        print(
            f"transport error: {type(exc).__name__}（连不上 --base-url 给定的后端；"
            "本行不回显 URL 与凭据）"
        )
        return 3
    except httpx.HTTPError as exc:
        # 只打异常类名：httpx 的消息里会带 URL（未来可能是带 token 的那种），不替它把值抄到终端。
        print(f"transport error: {type(exc).__name__}（地址由 --base-url 给定，本行不回显 URL 与凭据）")
        return 2
    finally:
        if owns_client:
            client.close()
