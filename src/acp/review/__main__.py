"""文本通道 `python -m acp.review`（M2-W8）：与看板打同一个评审端点。

形状照 `acp.approve` 那一套（FR-042 的双通道纪律）：

- CLI 不自己判状态、不自己写库，只把命令翻译成 `GET /tasks/reviewing` 与
  `POST /tasks/{card_id}/review` 两次请求；
- `--base-url` 没有默认值：端口归装配点定，缺它就退 2，不替使用者猜一个地址；
- `--principal` 必填，且是自报身份（本仓现阶段无鉴权，这条门挡披错身份，挡不住冒充）。

`submit` 先读一次待评审列表，用卡上声明的 `acceptance_criteria` 组逐条判定：`pass` 全 `met=true`；
`changes_requested` 则把 `--criterion` 点名的那些记 `met=false`，其余记 `met=true` ——
这正是"文件里每一条都判过"的形状，而不是只交一句整体结论。
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from acp.config import install_logging_filter

PRINCIPAL_HEADER = "x-acp-principal"
VERDICT_WORDS = {
    "pass": "pass",
    "approve": "pass",
    "changes": "changes_requested",
    "changes_requested": "changes_requested",
    "reject": "changes_requested",
}


class CliUsage(ValueError):
    """命令行本身不合法。退出码 2，与服务端拒绝（1）分开。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acp.review", description="人工评审文本通道：与看板打同一个端点"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pending = sub.add_parser("pending", help="列出待评审的卡")
    pending.add_argument("--feature-request", default=None, help="只看这一张需求单")
    pending.add_argument("--base-url", default=None)

    submit = sub.add_parser("submit", help="提交一条评审结论")
    submit.add_argument("card_id", help="任务卡号（如 T01）")
    submit.add_argument("--feature-request", default=None, help="需求单号（卡号跨单重名时必填）")
    submit.add_argument("--verdict", required=True, help="pass / changes_requested")
    submit.add_argument("--evidence", default="", help="评审依据（落进每条 item_findings 的 evidence）")
    submit.add_argument(
        "--criterion",
        action="append",
        default=[],
        dest="criteria",
        help="打回时点名未通过的验收标准，可重复；其余条目由 CLI 标为通过",
    )
    submit.add_argument("--entry-name", default=None, help="最后一张卡过审时组包用的入口页名")
    submit.add_argument("--principal", required=True, help="提交者标识（自报身份）")
    submit.add_argument("--base-url", default=None)
    return parser


def _client(args, client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    if not args.base_url:
        raise CliUsage("没带 client 就 MUST 给 --base-url（本模块不猜后端地址）")
    return httpx.Client(base_url=args.base_url), True


def _pending(client: httpx.Client, feature_request_id: str | None) -> list[dict[str, Any]]:
    params = {"feature_request_id": feature_request_id} if feature_request_id else None
    response = client.get("/tasks/reviewing", params=params)
    if response.status_code >= 400:
        raise CliUsage(f"读待评审列表失败：HTTP {response.status_code}")
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _resolve_card(rows: list[dict[str, Any]], card_id: str, feature_request_id: str | None) -> dict[str, Any]:
    matches = [
        row
        for row in rows
        if row.get("task_id") == card_id
        and (feature_request_id is None or row.get("feature_request_id") == feature_request_id)
    ]
    if not matches:
        raise CliUsage(f"卡 {card_id!r} 不在待评审列表里 ⇒ 不替你挑一张别的卡")
    if len(matches) > 1:
        raise CliUsage(f"卡 {card_id!r} 命中 {len(matches)} 张待评审卡 ⇒ 用 --feature-request 点名")
    return matches[0]


def _findings(card: dict[str, Any], verdict: str, criteria: list[str], evidence: str) -> list[dict[str, Any]] | None:
    if verdict == "pass":
        return None
    declared = [str(item) for item in (card.get("acceptance_criteria") or [])]
    if not criteria:
        raise CliUsage("changes_requested MUST 用 --criterion 至少点名一条未通过的标准")
    unknown = [item for item in criteria if item not in declared]
    if unknown:
        raise CliUsage(f"这些标准不在卡上：{unknown} ⇒ 不引入额外标准（FR-029）")
    failed = set(criteria)
    return [
        {"criterion": item, "met": item not in failed, "evidence": evidence}
        for item in declared
    ]


def _report(card_id: str, response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload.get("detail")
    top = payload if isinstance(payload, dict) else {}
    return {
        "card_id": card_id,
        "http": response.status_code,
        "verdict": top.get("verdict"),
        "card_status": top.get("card_status"),
        "accepted": top.get("accepted"),
        "delivery": top.get("delivery"),
        "next": top.get("next"),
        "detail": detail,
    }


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    install_logging_filter()
    args = _parser().parse_args(argv)
    owns = False
    try:
        client, owns = _client(args, client)
        if args.command == "pending":
            rows = _pending(client, args.feature_request)
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        word = VERDICT_WORDS.get(args.verdict)
        if word is None:
            raise CliUsage(f"不认识结论 {args.verdict!r}，可写的只有 {sorted(VERDICT_WORDS)}")
        feature_request = args.feature_request
        if feature_request:
            try:
                feature_request = str(uuid.UUID(feature_request))
            except ValueError:
                raise CliUsage(f"--feature-request 要一个 UUID，收到 {feature_request!r}") from None
        card = _resolve_card(_pending(client, feature_request), args.card_id, feature_request)
        body: dict[str, Any] = {
            "feature_request_id": card["feature_request_id"],
            "verdict": word,
            "evidence": args.evidence,
        }
        findings = _findings(card, word, args.criteria, args.evidence)
        if findings is not None:
            body["findings"] = findings
        if args.entry_name:
            body["entry_name"] = args.entry_name
        response = client.post(
            f"/tasks/{card['task_id']}/review",
            json=body,
            headers={PRINCIPAL_HEADER: args.principal},
        )
        print(json.dumps(_report(card["task_id"], response), ensure_ascii=False))
        return 0 if response.status_code < 400 else 1
    except CliUsage as exc:
        print(f"usage error: {exc}")
        return 2
    except httpx.TransportError as exc:
        print(f"transport error: {type(exc).__name__}（连不上 --base-url 给定的后端；本行不回显 URL 与凭据）")
        return 3
    except httpx.HTTPError as exc:
        print(f"transport error: {type(exc).__name__}（地址由 --base-url 给定，本行不回显 URL 与凭据）")
        return 2
    finally:
        if owns:
            client.close()


if __name__ == "__main__":  # pragma: no cover - 入口形状由 main() 的判据覆盖
    raise SystemExit(main())
