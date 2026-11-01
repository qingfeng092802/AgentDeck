"""总线目录布局（T014，FR-063｜R-24）。

**逐字对齐 `contracts/file-bus.md` §1**——那张 ASCII 树是本模块的唯一事实源，改名等于改契约，
MUST 两边同批动。这里只做路径代数：不建目录、不判断归属（归属在 `acl.py`，创建在
`orchestrator_writer.py`），这样"布局"与"权限"不会在一个文件里互相背书。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

BUS_ROOT_NAME = "AI项目台"

AGENTS_MD = "AGENTS.md"
STATUS_MD = "status.md"
DAILY_LOG_MD = "daily-log.md"
TASKS_DIR = "tasks"
RESULTS_DIR = "results"
MEETINGS_DIR = "docs/meetings"

#: T095 / EN-17：交付包的公共区落点。路径只有这一个载体 —— `data-model.md` 的 EN-17 那一段
#: 与 `assemble.py` 都引用本常量，两处各写一遍 `results/` 就会在两处各自漂。
#: 它**不进** `PUBLIC_ENTRIES`：那是"顶层目录/文件"的清单，本路径在 `results/` 之下（契约 §1 结构不变）。
DELIVERY_PACKAGE_JSON = f"{RESULTS_DIR}/delivery_package.json"

WORKSPACE_SUFFIX = "-workspace"

# 契约 §1 的公共区清单（新增 Agent 只增 workspace 目录，公共区结构不变 ⇒ FR-012）。
PUBLIC_ENTRIES = (AGENTS_MD, STATUS_MD, DAILY_LOG_MD, TASKS_DIR, RESULTS_DIR, MEETINGS_DIR)

# 同一份清单的两种角色，分开列是因为初始化时**只有目录该 mkdir**：
# 三个 .md 是文件，把 `status.md` 建成目录会让后面的写入直接撞 NotADirectoryError。
PUBLIC_DIRS = (TASKS_DIR, RESULTS_DIR, MEETINGS_DIR)
PUBLIC_FILES = (AGENTS_MD, STATUS_MD, DAILY_LOG_MD)

# agent_id 的形态：契约 §1 里的三个目录名是 workbuddy / qoder / doubao，全小写。
# 收紧成规则的理由：目录名要能反解出 agent_id 才能做归属比对（acl.workspace_agent_id），
# 允许大写或符号会让 "Qoder-workspace" 与 "qoder-workspace" 变成两个不同的写入者。
# **连字符是 Phase 17 P3 放开的**：`deepseek-reviewer` 是 owner 定名的审查身份，厂商侧
# 常用连字符命名；大写、空格与其余符号仍然拒（仍然只有一种拼法能反解回同一个写入者）。
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# 契约 §3 的文件名规范：T<NN>-<slug>.md，NN 在需求单内唯一。
TASK_FILE_RE = re.compile(r"^T\d{2,}-[a-z0-9][a-z0-9_-]*\.md$")

WORKSPACE_PATH_RE = re.compile(r"^([a-z][a-z0-9_-]*)" + re.escape(WORKSPACE_SUFFIX) + r"/.*$")


def workspace_dir_name(agent_id: str) -> str:
    if not AGENT_ID_RE.match(agent_id):
        raise ValueError(f"agent_id 形态非法：{agent_id!r}（MUST 匹配 {AGENT_ID_RE.pattern}）")
    return f"{agent_id}{WORKSPACE_SUFFIX}"


def workspace_agent_id(directory_name: str) -> str | None:
    """从目录名反解 agent_id；不是 workspace 就返回 None（不猜测）。"""
    if not directory_name.endswith(WORKSPACE_SUFFIX):
        return None
    prefix = directory_name[: -len(WORKSPACE_SUFFIX)]
    return prefix if AGENT_ID_RE.match(prefix) else None


def is_task_file(path: str) -> bool:
    return TASK_FILE_RE.match(PurePosixPath(path).name) is not None


def normalize_rel_path(path: str) -> PurePosixPath:
    """把调用方给的路径收成**相对、无 `..`、无绝对前缀**的 POSIX 形态。

    Windows 反斜杠在这里一次转 POSIX，否则 `acl.py` 的模式匹配会因分隔符差异而漏判。
    """
    posix = str(path).replace("\\", "/")
    if posix.startswith("/") or re.match(r"^[A-Za-z]:", posix):
        raise ValueError(f"MUST 用相对总线根的路径，收到绝对路径：{path!r}")
    candidate = PurePosixPath(posix)
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"越界路径（含 `..`）：{path!r}")
    return candidate


def project_dir(bus_root: str, project_key: str) -> PurePosixPath:
    """总线根下的项目子目录（单实例承载单项目；多项目 = 兄弟子目录）。"""
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", project_key):
        raise ValueError(f"project_key 形态非法：{project_key!r}")
    return PurePosixPath(bus_root) / project_key
