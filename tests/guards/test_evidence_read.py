"""右栏「文件总线」与「审查」两页的证据面（owner 2026-10-29 第十轮）。

缺陷的形状：14 张卡全部 `DELIVERED`、交付包都生成了，这两页仍然是"文件总线还是空的"/
"门禁还没开始"。根因不在前端 —— 真链路从来没往 `studio.artifacts` / `studio.reviews`
写过东西，那两格只有推演档会填。补的是 `GET /requests/{id}/evidence`。

本文件盯的是这条读面上**唯一一条会静默出错的规则**：一个文件一行。

流水线上每张卡各写自己那一段、最后合成一个文件，所以库里同一个 `about.html` 会有五行
（真实那单：198 行 artifact → 去重后 14 个文件）。照原样铺到文件总线上，读起来就是
"产出了五份 about.html"，产物计数虚高。规则错了不报错，屏上照样渲染 —— 所以要判据。

不碰 PG：这条规则是纯函数，喂 SimpleNamespace 就能量，不必为它建一张卡。
"""

from __future__ import annotations

from types import SimpleNamespace

from acp.api import requests as api


def row(rel_path: str, *, written: str, card: str = "T01") -> SimpleNamespace:
    """`artifact` 那一行的最小替身：判据只读 `rel_path`，其余只为读起来像真的。"""
    return SimpleNamespace(rel_path=rel_path, task_card_id=card, written=written)


def test_one_row_per_file_and_the_last_write_wins():
    """同一个文件被几张卡先后写过 ⇒ 只留最后一次写的那个。

    入参按 `created_at` 升序（端点用 `order_by` 保证），所以"最后一次"就是列表里靠后的那个。
    这条断言用 `written` 而不是行数：只数行数会放过"留了第一份"这种错。
    """
    rows = [
        row("site/about.html", written="10:01", card="T03"),
        row("site/index.html", written="10:02", card="T03"),
        row("site/about.html", written="10:05", card="T07"),
        row("site/about.html", written="10:09", card="T11"),
    ]
    kept = api.latest_per_path(rows)
    assert [item.rel_path for item in kept] == ["site/about.html", "site/index.html"]
    about = next(item for item in kept if item.rel_path == "site/about.html")
    assert about.written == "10:09"
    assert about.task_card_id == "T11"


def test_two_files_with_the_same_basename_are_two_rows():
    """`a/index.html` 与 `b/index.html` 是两个产物，不是同一个文件。

    去重键是**整条相对路径**，不是文件名。按文件名去重会把多页站点压成一页 ——
    而"压掉了哪一页"在屏上完全看不出来（剩下的那个还叫 index.html）。
    """
    rows = [row("site/index.html", written="10:01"), row("admin/index.html", written="10:02")]
    kept = api.latest_per_path(rows)
    assert [item.rel_path for item in kept] == ["admin/index.html", "site/index.html"]


def test_the_output_is_ordered_by_path_so_the_list_does_not_jump_between_polls():
    """按路径排序，不是按写入顺序。

    这张表在交付前会被反复读（每 1.5 秒一次的刷新也会带上它）。按写入顺序排的话，
    每来一个新产物整张清单就重排一次，人正在看的那一行会往下跑。
    """
    rows = [row("z.html", written="10:01"), row("a.html", written="10:02"), row("m.html", written="10:03")]
    assert [item.rel_path for item in api.latest_per_path(rows)] == ["a.html", "m.html", "z.html"]


def test_an_empty_table_is_an_empty_list_not_a_placeholder():
    """真的没有产物就是空清单 —— 不编一行"暂无产物"。

    空是合法结果（还没派单的单就是空的），但它必须与"读面根本没接"区分开：
    前端把 `[]` 渲染成"文件总线还是空的"，把 `undefined` 当作"没取到、保留旧值"。
    这两种在屏幕上长得像，语义相反。
    """
    assert api.latest_per_path([]) == []


def test_the_route_is_mounted_where_the_frontend_looks_for_it():
    """路径与前端 `fetchEvidence` 逐字一致。

    两边各写一份字面量（前端不 import 后端）。漂了的表现是 404 ⇒ 前端 `catch` 里什么都不做
    ⇒ 右栏两页**又是空的**，而且不报错。这正是这次要修的那个症状。
    """
    paths = [getattr(route, "path", "") for route in api.router.routes]
    assert "/requests/{request_id}/evidence" in paths
