"""交付清单按**文件**去重（2026-10-28 第二次真跑撞出来的）。

现场：一张"总览页"的卡跑完，执行器把整套站都写了一遍 —— `index.html` / `members.html` /
`tasks.html` 全在。于是同一个路径在 `artifact` 里留下两三行（T02 与 T04 都写了 `index.html`），
而盘上只有一个文件（后写覆盖先写）。

不去重的两个后果都是硬的：
- 组包按行数报成员数 ⇒ 交付包说自己有 19 份，盘上只有 12 份；
- 入口判定读到两份同名 `index.html` ⇒ `entry_ambiguous` ⇒ **交付包永远组不出来**（T97 缺席）。

留哪一行：`created_at` 最新的那一行 —— 它是盘上那份内容的实际写者，`bytes` / `lines` 只有它说的是真的。
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

from acp.orchestrate.nodes.assemble import validated_manifest

BASE = dt.datetime(2026, 10, 28, 5, 0, tzinfo=dt.timezone.utc)


def _row(path: str, card: str, *, seconds: int, size: int, lines: int, agent: str = "qoder"):
    return SimpleNamespace(
        rel_path=path,
        writer_agent_id=agent,
        task_card_id=card,
        bytes=size,
        lines=lines,
        format="html",
        created_at=BASE + dt.timedelta(seconds=seconds),
    )


class _Session:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self, *_args, **_kwargs):
        return SimpleNamespace(all=lambda: list(self._rows))


def test_the_same_file_written_by_two_cards_is_one_manifest_entry():
    rows = [
        _row("qoder-workspace/index.html", "T02", seconds=1, size=6026, lines=131),
        _row("qoder-workspace/index.html", "T04", seconds=2, size=6073, lines=132),
        _row("qoder-workspace/about.html", "T04", seconds=2, size=8747, lines=162),
    ]

    manifest = validated_manifest(_Session(rows), uuid.uuid4())

    assert [item.rel_path for item in manifest] == [
        "qoder-workspace/about.html",
        "qoder-workspace/index.html",
    ]
    entry = next(item for item in manifest if item.rel_path.endswith("index.html"))
    assert entry.task_card_id == "T04", "留的不是最后写的那一行 ⇒ 交付包里的字节数是旧的"
    assert (entry.bytes, entry.lines) == (6073, 132)


def test_the_order_of_the_rows_does_not_change_the_pick():
    """查询顺序换了（比如加了索引）也不许换人 —— 同一时刻按卡号取大的。"""
    rows = [
        _row("qoder-workspace/index.html", "T04", seconds=2, size=6073, lines=132),
        _row("qoder-workspace/index.html", "T02", seconds=2, size=6026, lines=131),
    ]
    manifest = validated_manifest(_Session(rows), uuid.uuid4())
    assert manifest[0].task_card_id == "T04"


def test_no_rows_is_still_a_refusal_not_an_empty_package():
    import pytest

    from acp.orchestrate.nodes.assemble import AssembleMisuse

    with pytest.raises(AssembleMisuse):
        validated_manifest(_Session([]), uuid.uuid4())
