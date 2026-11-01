#!/usr/bin/env python
"""Markdown 表列完整性检查器（`tasks.md` 附录 B 的表列判据入口，2026-10-12 落库）。

落库原因（这条本身就是本轮暴露的缺陷）：判据在 `tasks.md` 里写的是"同一支转义感知脚本"，
而脚本此前只存在于产出方的临时目录 ⇒ 复核者拿不到复跑入口，判据等于没写。现入库，
文档里的复算命令一律改指 `scripts/check_tables.py`。

口径：
- 只解析**行首为 `|`** 的连续行块（组）；组内第一行是表头，第二行若形如 `|---|---|` 则是分隔行。
- **引用块里的表不在本门禁覆盖内**：行首是 `>` 而竖线出现在剥离引用标记之后（`> |` 起首）的行，
  上面那条解析器天然看不见。owner 2026-10-17 裁 Q12 取**乙** —— 这类表形**按人工核**，清单与现扫读数在
  `evidence/phase-12.md` §廿一；把 `> |` 纳入解析面（甲）已登记为 P2（`tasks.md` Phase 12 段末），
  等量过之后再裁扩不扩。**这一条是覆盖范围的声明，不是缺陷登记**：本文件的解析器代码不因它改变行为。
- 单元格里的转义竖线 `\\|` 先折成占位串再切列——不折就会把 `int \\| None` 读成两列（此前实测踩过）。
- 判据是**组内列数唯一**，不是"列数等于某个数"：加列减列都合法，混进错列才算畸形。
- 分隔行不参与一致性判定以外的任何计算（表头与数据行同组，一起比）。

作用域刻意分两段：`OWNED` 是本轮产出方负责的我方产物，畸形 ⇒ 退出码 1；
`RESIDUAL` 是 owner 的输入物与 peer 的复审产物，**只报不拦**——里面的畸形表要由作者自己改，
由我改会把别人的论点抄进我的笔（历史留痕不改写）。当前命中数见 SUMMARY 行的 `residual=`。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "specs" / "001-multi-agent-collab-platform"

OWNED = [
    SPEC / "tasks.md",
    SPEC / "plan.md",
    SPEC / "research.md",
    SPEC / "quickstart.md",
    SPEC / "data-model.md",
    SPEC / "spec.md",
    SPEC / "spec-skeleton.md",
    SPEC / "checklists" / "requirements.md",
    SPEC / "contracts" / "agent-protocol.md",
    SPEC / "contracts" / "file-bus.md",
    SPEC / "contracts" / "approval-api.md",
    # owner 2026-10-14 裁决 4：`evidence/` 是 Phase 产物目录（我方自己的笔），畸形要拦而不是只报。
    # 直接暴露这个决定的那一次是 Phase 7 收口轮：台账 §八 里有一处未转义竖线，门禁全程绿着放过，
    # 因为 `OWNED` 此前只列 `specs/` 那 11 份 ⇒ "owned_malformed=0" 从来没覆盖过台账自身。
    # 这里用 glob 而不是逐份点名：Phase 台账每轮都在长，点名等于让下一轮的台账天然落在门禁外。
    *sorted((ROOT / "evidence").glob("phase-*.md")),
]

RESIDUAL = [
    ROOT / "PRD-v1.4.md",
    ROOT / "config" / "params-ref.md",
    SPEC / "plan-review-v1.0.0.md",
    SPEC / "plan-review-v1.0.1.md",
    SPEC / "plan-review-v1.0.2.md",
    SPEC / "tasks-review-v1.0.0.md",
    SPEC / "spec-review-v1.0.1.md",
    SPEC / "constitution-review-v1.0.0.md",
]

SEP = re.compile(r"^\|[\s:\-\|]+\|$")


def cells(line: str) -> int:
    body = line.replace("\\|", "\x01").strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return body.count("|") + 1 if body != "" else 1


def scan(path: Path) -> tuple[int, list, list[tuple[int, list[int]]]]:
    lines = path.read_text(encoding="utf-8").split("\n")
    groups: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for number, text in enumerate(lines, 1):
        if text.startswith("|"):
            cur.append((number, text))
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    widths, malformed = [], []
    for group in groups:
        row_widths = [cells(text) for _, text in group]
        has_sep = len(group) > 1 and bool(SEP.match(group[1][1].strip()))
        widths.append(row_widths[1] if has_sep else row_widths[0])
        if len(set(row_widths)) != 1:
            malformed.append((group[0][0], sorted(set(row_widths))))
    return len(groups), widths, malformed


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets = [Path(a) for a in argv[1:]] if len(argv) > 1 else OWNED + RESIDUAL
    owned_hit = residual_hit = missing = 0
    for path in targets:
        if not path.exists():
            print(f"MISSING {path}")
            missing += 1
            continue
        count, widths, malformed = scan(path)
        bucket = "owned" if (path in OWNED or len(argv) > 1) else "residual"
        if malformed:
            where = [(line, cols) for line, cols in malformed]
            print(f"[{bucket}] {path.name} groups={count} cols_by_group={widths} malformed={len(malformed)}")
            for line, cols in where:
                print(f"    BAD at line {line} widths={cols}")
            if bucket == "owned":
                owned_hit += len(malformed)
            else:
                residual_hit += len(malformed)
        else:
            print(f"[{bucket}] {path.name} groups={count} cols_by_group={widths} malformed=0")

    gate = "PASS" if owned_hit == 0 and missing == 0 else "FAIL"
    print(f"SUMMARY owned_malformed={owned_hit} residual={residual_hit} missing={missing} gate={gate}")
    return 0 if gate == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
