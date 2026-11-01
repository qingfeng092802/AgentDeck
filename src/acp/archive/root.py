"""T109（T-4 接口定稿）：存档根的唯一解析点。

`snapshot.ref` 的**形态**在 `cadence.snapshot_ref()`（`git:<对象名>`），本模块管的是那句话里剩下的另一半：
**那个对象住在哪一个仓库里**。owner 2026-10-16 裁 ㉝ 归 T109，而 T108 的 `cadence` 明写"根由调用方交出、
本模块不猜默认值" ⇒ 猜默认值这一步必须有人承担，它就在这里，且只在这里。

**存档根 = 总线项目目录 `<bus_root>/<project_key>`**（权威出处 = `config/params-ref.md §10`）。
选它的理由不是"顺手"：`recover.py` 的 `--from-file-bus <project_key>` 重建的就是这个目录，
`assemble.py` 查交付包入口文件用的也是总线根 ⇒ **"revert 可复原"（宪法「三」）与"从文件总线重建三元组"
（D-12 第二条回退路径）作用在同一个目录上**。若存档根另指一处，两条回退路径就会各自复原一半现场。

**三条刻意的取舍**：

1. **默认值借 `bus.layout.BUS_ROOT_NAME`，不在这里重述那个字符串**。目录名换一个载体写第二遍，
   就会出现"总线在 A 处、存档根在 B 处"而两边都自称对齐了契约 §1。
2. **`<bus_root>/<project_key>` 这条拼接规则借 `layout.project_dir()`**。它是那条规则现有的唯一载体
   （含 `project_key` 的形态校验）；在这里另写一份 `root / key` 等于把同一条规则交给两个函数各自漂。
3. **解析不出可用根 ⇒ 抛，不回退成"没有存档也行"**。这一层一旦允许静默回退，调用方就会把它当成
   T-15 那种自指句柄的替代出口 —— 而 `persistence/snapshots.record_point()` 已经明确拒收临时形态，
   两边必须同向：**要么拿到一个真 commit 的对象名，要么这一轮不成立**。

错误消息口径同 `cadence`：只带变量名、参数名、退出码与原因词，不带 git 的 stderr 原文。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from acp.archive import cadence
from acp.bus import layout

#: 总线根的环境变量名。**这一行与 `config/params-ref.md §10` 的表是同一件事的两个面**：
#: 围栏里那张表是权威出处，本常量是它的代码载体，`tests/guards/test_snapshot_ref.py` 第一节把两者对齐
#: （改了 params-ref 而不改这里 ⇒ 判据红；反之也红）。
BUS_ROOT_ENV = "ACP_BUS_ROOT"

#: 总线根的默认目录名 —— 从 `bus.layout` 取，不在本模块重述（模块头第 1 条）。
DEFAULT_BUS_ROOT_NAME = layout.BUS_ROOT_NAME


class ArchiveRootMisuse(ValueError):
    """存档根解析不出来，或在执行之前就被发现不是一个仓库。全部发生在一条 git 命令都没跑之前。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def resolve_bus_root(
    *, bus_root: Path | str | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """总线根的解析优先级：**显式参数 > `ACP_BUS_ROOT` > `AI项目台`（相对进程工作目录）**。

    显式参数排第一是为了判据与装配方能指定现场（测试用临时总线根，不给环境变量污染进程）；
    环境变量排第二是生产形态（一次部署一个总线根）；默认值与 `recover.py --bus-root` 的默认同形，
    那条 CLI 早就是"相对 cwd 的 `AI项目台`"，这里另起一套就会让两条回退路径看着不同的目录。

    空白值按"没设"处理：`ACP_BUS_ROOT=""` 会被读成一个空路径，而空路径 resolve 出来是文件系统根，
    那一步会往下走很远才失败 —— 失败得越早，越不会有人在半程态里以为存档已经发生。
    """
    if bus_root is not None:
        return Path(str(bus_root))
    source = os.environ if env is None else env
    raw = source.get(BUS_ROOT_ENV, "")
    return Path(raw) if raw.strip() else Path(DEFAULT_BUS_ROOT_NAME)


def archive_root(
    project_key: str,
    *,
    bus_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """本项目的**存档根** = 总线项目目录。`project_key` 形态非法 ⇒ `ValueError`（借 `layout.project_dir`）。

    这一处是"外层总线根套一层项目名"**全仓唯一的拼法**：装配段（`run.py` 的网关、交付包、公共区投影、
    整链活动流）与核验段（`verify_mvp1`）过去各有一份自己的拼法，owner 2026-10-17 裁决 2 把它们合并到这里。
    合并不只是省一行 `/`：形态校验也跟着只剩 `layout.project_dir()` 一处，非法 `project_key` 在两个段里
    会一起拒而不是"一边拒、一边静默拼出一个目录"。
    """
    root = resolve_bus_root(bus_root=bus_root, env=env)
    project: PurePosixPath = layout.project_dir(str(root), project_key)
    return Path(str(project))


def require_archive_root(
    project_key: str,
    *,
    bus_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    runner: cadence.GitRunner = cadence.run_git,
) -> Path:
    """解析 + **在执行之前**确认这个目录自己就是一个仓库的顶层，回该路径。

    两道检查各有各的用处，缺一不可：

    - **目录不存在** ⇒ `archive_root_missing`：默认值是"相对 cwd 的 `AI项目台`"，而进程未必从那一级启动；
      目录不存在是配置问题，不是"这一轮没有现场可存"。
    - **它不是仓库顶层** ⇒ `archive_root_not_toplevel`：`git rev-parse --show-toplevel` 会**向上**找到
      第一个真的仓库。如果总线目录恰好嵌在平台自己的仓库里（把 `AI项目台/` 放在仓库根下就是这样），
      不加这道检查的 `add -A` + `commit` 就会把**平台仓库的整棵工作树**提交掉 —— 那是存档机制
      最坏的一次误伤，而且它一路都是退出码 0，事后只在别人的提交历史里看得见。
    """
    target = archive_root(project_key, bus_root=bus_root, env=env)
    if not target.is_dir():
        raise ArchiveRootMisuse(
            f"存档根不存在：{str(target)!r}（解析优先级 = 显式 `bus_root` 参数 > {BUS_ROOT_ENV} > "
            f"默认目录名 {DEFAULT_BUS_ROOT_NAME!r}；权威出处 `config/params-ref.md §10`）",
            code="archive_root_missing",
        )
    top = repository_toplevel(target, runner=runner)
    # 两侧都先 `resolve()` 再比：默认值那条路径是**相对 cwd** 的 `AI项目台/<project_key>`，
    # 而 git 报的是绝对路径 —— 不 resolve 就永远不相等，这道检查会变成"恒拒"（它红的时候看着像机制坏了）。
    if top is None or top.resolve() != target.resolve():
        raise ArchiveRootMisuse(
            f"存档根 {str(target)!r} 不是一个 git 仓库的顶层 ⇒ 拒打存档点。"
            "向上找到的那个仓库不是本项目该被存档的地方"
            "（把 `add -A` 打到别人的工作树上不是'存档'，是替别人提交）",
            code="archive_root_not_toplevel",
        )
    return target


def repository_toplevel(root: Path, *, runner: cadence.GitRunner = cadence.run_git) -> Path | None:
    """`rev-parse --show-toplevel` 的读数；退非零或读不出路径 ⇒ `None`（不猜）。

    用的是 `cadence` 允许名单里**已有**的子命令（`rev-parse`）⇒ 这一道自检不扩大 git 面。
    """
    argv = cadence.TOPLEVEL_ARGV
    cadence.assert_allowed(argv)
    result = runner(argv, root)
    if result.code != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return Path(lines[0]) if lines else None
