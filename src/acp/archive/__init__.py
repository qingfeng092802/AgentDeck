"""T108 + T109：存档这一层（FR-053 / FR-054｜R-14，宪法「三」+ D-12）。

包只有三个关注点，各一个模块：

- `cadence.py` —— 机制半边：一轮的开工前 / 完工后两拍，落到一个真 git commit 对象。
- `root.py` —— T109（T-4 定稿）新加的那一半：**句柄指向的那份仓库在哪儿**。权威出处
  `config/params-ref.md §10`，代码侧只有 `require_archive_root()` 这一个解析入口。
- `persistence/snapshots.py` —— 登记半边：EN-12 的行、可枚举的读面、EN-14 的 `SNAPSHOT` 留痕。

不在这里做的事：回退（T110 `--from-snapshot`）、场景注入（T111 `acp.chaos`）。
`ref` 的接口在 T109 定稿：形态 = `cadence.snapshot_ref()`（`git:<对象名>`），根 = `root.py`，
落库 = `snapshots.record_point()` —— 三段各一处，`orchestrate/graph.py` 从 T109 起不再是其中任何一段。
"""
