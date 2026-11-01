"""T106：文本通道 `python -m acp.autonomy`（FR-055｜R-15，与 T103 的 HTTP 面打**同一个端点**）。

一条硬约束（owner 的约束 5，口径与 `acp.approve` 的 FR-042 双通道同形）：**CLI 不判主体、不判档位、
不写 EN-16**，它只做"把命令翻译成一次 `PATCH /autonomy`"。看板 / CLI / HTTP 三条路因此共用
同一份谓词（`persistence/autonomy.py::_guard_updated_by`）、同一张状态码表（同模块的 `HTTP_STATUS`）、
同一个 fail-closed 注入项（`app.state.machine_principals`）。
凡"CLI 里顺手读一下当前档位再决定要不要发请求"的写法，都会造出第二条判定路径。

四个必须交代清楚的实现决定：

1. **`--project` 必填**。EN-16 的主键是 `project_key`（`data-model.md` EN-16 第一行），
   而本仓没有"默认项目"这个载体 —— 判据形态是"没有任何 `ACP_` 前缀的项目级默认环境变量"。
   **这条说明自身就是那个串的唯一命中处**：拿 `grep -rn "ACP_PROJECT" src/acp` 复算会读到 1，
   而那 1 是本行自己（自指），真实载体数 = 0。写出来是为了下一轮不必再查一遍。
   留一个默认值等于替使用者决定改哪个项目的档位 —— 单实例单项目是部署形态，不是可以省略申报的理由。
2. **档位词只做到域值的形状映射，不做第二道门**。`set safe` / `set SAFE` 都映射到域里的 `SAFE`
   （映射表从 `values_of("autonomy_mode")` 派生，代码里没有字面量清单）；**其他写法原样交出去**，
   由仓储答 422 `mode_not_allowed`。在 CLI 里预拒"不在表里的词"就会出现"CLI 拒的理由与服务端拒的
   理由不同"，而契约 §1.5 只登记了服务端那三种。
3. **`--as-agent NAME` 就是把 NAME 放进 `X-ACP-Principal`**。403 由端点判（NAME 在 EN-03 在册集合里，
   或在装配点注入的自动主体集合里），CLI 侧不加判定。⇒ 这一条与 T103 共享那条披露：
   MVP-1 没有认证，挡的是"披错身份误操作"，**挡不住冒充** —— 一个没注册过的假名，服务端分不出来。
4. **不回显地址与凭据**。`--base-url` 没有默认值（同 `acp.approve` 第 3 条：装配点未落地前，
   留默认地址等于把"端口归谁定"写进代码）；服务端的 `detail` 打出来之前过 `redact_bus_text()`，
   httpx 的异常只打类名。

退出码分档与 `acp.approve` 一致：`0` 成 / `1` 服务端拒 / `2` 用法错（含连不上之外的本地拒） /
`3` 传输层失败。分档的意义是让脚本能分清"我这条命令写错了"与"平台不同意"。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

import httpx

from acp.api.approvals import PRINCIPAL_HEADER
from acp.config.loader import install_logging_filter, redact_bus_text
from acp.persistence.enums import values_of

#: 档位词的形状映射表：**从域派生，不写字面量清单**（口径同 `guard/autonomy.py` 的 import 期核对）。
#: 域里若删掉 `SAFE`，这张表跟着少一个键，而不是"CLI 还收着两个词而仓储已经改了名"。
TIERS: dict[str, str] = {value.casefold(): value for value in values_of("autonomy_mode")}

#: 头名不在这份模块里重述：唯一载体 = `api/approvals.py::PRINCIPAL_HEADER`（`api/autonomy.py` 也用它）。


class CliUsage(ValueError):
    """命令行本身不合法（本地就能判的那一类）。退出码 2，与"服务端拒了"（1）分开。"""


def normalize_mode(word: str) -> str:
    """`safe` / `SAFE` / ` Safe ` ⇒ 域值；其他写法**原样返回**，由域去答 422。

    吃掉首尾空白是"形状"那一层：`set  safe ` 与 `set safe` 指同一档，而域里没有带空白的取值，
    所以这里洗空白不会把一个非法词变成合法词以外的任何东西。
    """
    return TIERS.get(word.strip().casefold(), word)


def body_of(project_key: str, mode: str) -> dict[str, str]:
    """请求体两个字段，形状由 `ModePatch`（`extra="forbid"`）核 —— 这里不多给一个键。"""
    return {"project_key": project_key, "mode": normalize_mode(mode)}


def principal_of(args: argparse.Namespace) -> str:
    """`--principal` 与 `--as-agent` 二者恰取其一。

    两个都给 ⇒ 拒：那等于让"我以使用者身份发，但我又想声明我是机器"这句话有一个未声明的胜出者。
    两个都不给 ⇒ 拒（同 T103 的"缺头就 403"，只是这里在本地就能判，不必发出去再撞）。
    """
    principal, as_agent = args.principal, args.as_agent
    if principal is not None and as_agent is not None:
        raise CliUsage("--principal 与 --as-agent 只能给一个 ⇒ 两个都给就没有『谁是主体』这一件事了")
    chosen = principal if principal is not None else as_agent
    if chosen is None:
        raise CliUsage(
            "MUST 给 --principal <使用者名>（改档只能是使用者做的）"
            "或 --as-agent <agent_id>（这一发是用来验证平台会拒的）"
        )
    if not chosen.strip():
        raise CliUsage("主体名是空白的 ⇒ 没有主体就没有改档（同仓储那条空白拒法）")
    return chosen


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acp.autonomy",
        description="自主度档位的文本通道：与看板打同一个 PATCH /autonomy（FR-055）",
    )
    parser.add_argument("verb", help="set（本通道只有改档这一个动作，读档面 §1.5 未开）")
    parser.add_argument("mode", help="safe / full（其他写法照样发出去，由域答 422）")
    parser.add_argument("--project", required=True, help="项目标识（EN-16 的主键，本 CLI 不猜）")
    parser.add_argument("--principal", default=None, help="使用者标识（自报身份，见模块头第 3 条）")
    parser.add_argument(
        "--as-agent",
        default=None,
        dest="as_agent",
        help="用某个 agent_id 发这一发 ⇒ 服务端 MUST 拒（403 principal_not_allowed）",
    )
    parser.add_argument("--base-url", default=None, help="后端地址，例 http://127.0.0.1:8000")
    return parser


def _report(args: argparse.Namespace, response: httpx.Response, sent_mode: str) -> dict[str, Any]:
    """一行 JSON 打给终端。成功读响应体（EN-16 那六列），失败读 `detail` 并先过脱敏。

    **洗的三个面**：`--project`、发出去的档位词、服务端的 `detail`。前两个是使用者自己敲的，
    但"敲过"不等于"可以随 stdout 被复制走"（这一行会被贴进总线文件与聊天记录）；而第三个是
    厂商侧自由文本，本仓的口径一直是不过滤不打印。常量位（`http`）与域值位（`mode`）不洗：
    前者是数字，后者只可能是 `FULL`/`SAFE`。
    """
    report: dict[str, Any] = {
        "project_key": redact_bus_text(args.project),
        "sent_mode": redact_bus_text(sent_mode),
        "http": response.status_code,
    }
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    if isinstance(payload, dict) and response.status_code < 400:
        report.update(
            {
                "mode": payload.get("mode"),
                "danger_list_version": payload.get("danger_list_version"),
                "updated_by": payload.get("updated_by"),
                "updated_at": payload.get("updated_at"),
                "last_intercept_at": payload.get("last_intercept_at"),
                "code": None,
            }
        )
    else:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        report["mode"] = None
        report["detail"] = redact_bus_text(text)
        # `api/autonomy.py` 把稳定键放在 detail 的冒号前（`f"{code}：{消息}"`），这里只是**把它显示出来**，
        # 不重新判一次；解析不出冒号就整串照打，不猜一个码。
        report["code"] = text.split("：", 1)[0] if "：" in text else None
    return report


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    install_logging_filter()
    args = _parser().parse_args(argv)
    if args.verb != "set":
        print(f"usage error: 本通道只有 set 这一个动作（收到 {args.verb!r}）")
        return 2
    owns_client = client is None
    if owns_client:
        if not args.base_url:
            print("usage error: 没带 client 就 MUST 给 --base-url（本模块不猜后端地址）")
            return 2
        client = httpx.Client(base_url=args.base_url)
    try:
        principal = principal_of(args)
        body = body_of(args.project, args.mode)
        response = client.patch("/autonomy", json=body, headers={PRINCIPAL_HEADER: principal})
        print(json.dumps(_report(args, response, body["mode"]), ensure_ascii=False))
        return 0 if response.status_code < 400 else 1
    except CliUsage as exc:
        print(f"usage error: {exc}")
        return 2
    except httpx.TransportError as exc:
        # 只报类名：httpx 的连接错误消息里带完整 URL（含 userinfo 与端口）。
        print(
            f"transport error: {type(exc).__name__}"
            "（连不上 --base-url 给定的后端；本行不回显 URL 与凭据）"
        )
        return 3
    finally:
        if owns_client:
            client.close()
