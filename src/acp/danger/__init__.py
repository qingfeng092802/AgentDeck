"""T106：使用者扩展高危清单的入口 `python -m acp.danger`（FR-058｜R-15）。

**本模块是 `config/danger_list.yaml` 在使用者侧的唯一写入点**，这件事有三层要说清：

1. **归属没变**（`plan.md` 设计项 9 / C5、清单头部第 2 段）：清单的写入者 = 使用者，平台只读它。
   本模块不是"平台自扩清单"的一条路 —— 它是**人敲下去的那一条命令**；平台运行期
   （`guard/` / `orchestrate/` / `api/` / `adapters/`）至今 0 处写它，这条由
   `tests/contract/test_danger_list.py` 第五节钉（允许名单就一条，就是本模块，
   并另有一条"运行期不许 import 本模块"的对照 —— 名单不是空话，也不是后门）。
   方向要说准：若清单可由被拦的一方自己扩缩，命中判据就变成"审查岗改验收标准"（宪法原则 III）的失效形态。
2. **只有 `add`，没有 `remove`，也没有 `--action`**。删项是改文件（改动可见、可回溯，写在清单头部），
   不是本入口的一个子命令；`action` 的取值域只有 `intercept`（T100 定死），
   给它开一个开关就是 FR-058 明令不给的那个口子 —— "新增项先放行"。
3. **不自己校形，校形只交给 loader**。本模块不重复实现"什么是合法条目"：
   先在临时文件上把**候选内容**过一遍 `config/loader.load_danger_list()`，过了才落到真文件；
   过不了就一个字都不写、把 loader 的原话报出来。⇒ 真文件永远不会处于"写了一半而清单不合法"的状态，
   而"合法"这句话在仓内仍然只有一个载体（在 CLI 里再判一遍空白/重复项就是第二个载体）。
   **一处要点对齐**：`load_danger_list()` 的 `path` 参数写着"生产调用方 MUST 不传"（那句的理由是
   "生效清单的来源不许指向别处"）。本模块是仓内第一个在运行路径上传 `path` 的调用方，且只传给
   **候选预检**那一次；写完之后打印指纹读的是 `danger_list_file`（不注入时就是 `DANGER_LIST_PATH`）。
   ⇒ 生效来源没变，变的只是"校验器可以被拿来检查一份还没落盘的候选"。这一条由判据钉住：
   拿 `path=` 跑一次 CLI 之后，`config/danger_list.yaml` 的字节必须一字不变（用例真跑过一次才敢说）。

退出码与 `acp.autonomy` / `acp.approve` 同档：`0` 成 / `1` 读写失败 / `2` 用法错或 loader 拒收。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

from acp.config.loader import DANGER_INTERCEPT, DANGER_LIST_PATH, DangerListError, load_danger_list


class DangerCliUsage(ValueError):
    """命令行不合法，或候选清单被 loader 拒收（两者都是"你的输入不行"，退出码 2）。"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acp.danger",
        description="高危清单的使用者扩展入口：add 一条（FR-058；新项默认拦截，没有放行开关）",
    )
    parser.add_argument("verb", help="add（本入口只有这一个动作，见模块头第 2 条）")
    parser.add_argument("--pattern", required=True, help="要拦的词序列，如 \"rm -rf\"")
    return parser


def candidate_text(raw: str, pattern: str) -> str:
    """把新条目拼在原文**末尾**，其余字节一字不动。

    不用 `yaml.safe_load` 整份重 dump：那份文件头部有十几段口径说明（写入者归属、匹配语义、
    指纹算法、为什么 `delete from` 不在清单里），重 dump 会把注释全抹掉 —— 那是把策略文件的
    "为什么"删了，只留下"是什么"。这里只用 `safe_dump` 生成**新增那一行**（顺带拿到正确的引号转义），
    其余保持原样。末尾补一个换行的理由是：原文件若不以换行结束，直接拼就会把两条粘成一条。
    """
    item = yaml.safe_dump(
        [{"pattern": " ".join(pattern.split()), "user_added": True}],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    base = raw if raw.endswith("\n") or not raw else raw + "\n"
    return base + f"# 由 `python -m acp.danger add` 加入（FR-058 的使用者扩展项，默认 {DANGER_INTERCEPT}）\n" + item


def load_or_reject(path: Path, pattern: str) -> str:
    """在临时文件上验一遍候选内容，返回候选文本；loader 拒了就抛（真文件此时还没被碰）。"""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DangerCliUsage(f"清单读不出来：reason={type(exc).__name__}（不回显路径与凭据）") from None
    if not pattern.strip():
        raise DangerCliUsage("--pattern 是空白的 ⇒ 一条拦不住任何动作的条目不是扩展，是噪声")
    candidate = candidate_text(raw, pattern)
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    temp = Path(handle.name)
    handle.close()
    try:
        temp.write_bytes(candidate.encode("utf-8"))
        try:
            load_danger_list(temp)
        except DangerListError as exc:
            raise DangerCliUsage(f"loader 拒收这条扩展，真文件未改动：{exc}") from None
    finally:
        os.unlink(temp)
    return candidate


def main(argv: Sequence[str] | None = None, *, path: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    # 变量名带 `danger_list` 这一段不是风格：`tests/contract/test_danger_list.py` 的写入面扫描
    # **按调用点文本认**，改名成 `target` 会让这一发落进它钉住的盲区，允许名单随之变成死条目。
    danger_list_file = DANGER_LIST_PATH if path is None else path
    if args.verb != "add":
        print(f"usage error: 本入口只有 add 这一个动作（收到 {args.verb!r}；删项请直接改文件，改动要可见）")
        return 2
    try:
        candidate = load_or_reject(danger_list_file, args.pattern)
    except DangerCliUsage as exc:
        print(f"usage error: {exc}")
        return 2
    try:
        danger_list_file.write_bytes(candidate.encode("utf-8"))
    except OSError as exc:
        print(f"write error: 清单没写成，reason={type(exc).__name__}（本行不回显路径）")
        return 1
    loaded = load_danger_list(danger_list_file)
    print(
        json.dumps(
            {
                "file": danger_list_file.name,
                "added": " ".join(args.pattern.split()),
                "action": DANGER_INTERCEPT,
                "entries": len(loaded.entries),
                "user_added": loaded.user_added_count,
                "fingerprint": loaded.fingerprint,
            },
            ensure_ascii=False,
        )
    )
    return 0
