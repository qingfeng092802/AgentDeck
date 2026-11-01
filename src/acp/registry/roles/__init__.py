"""T087：R-16 前三岗的**内置岗位说明**（FR-071 ①②｜R-16）—— EN-04 那份生效口径的唯一生产者。

**T-3 在这里闭合**（`data-model.md §5` 那条"charter 正文来源与岗位说明文件的存放位置"）：
- **存放位置** = 本包目录（`src/acp/registry/roles/`），**一岗一个 `<role_id>.md`**；
- **正文来源** = 那个文件的**全文**（原样进 `role_spec.charter`，不改写、不拼接、不加 frontmatter）；
- **岗位名与只读属性** = 下面 `BUILTIN_ROLES` 那一份声明（文件里不重复写这两个值 ——
  写进去就会有第二份口径，而 `charter` 是给人读的正文，不该同时是配置载体）。

为什么正文放文件而不写成 Python 字符串常量：FR-071 的原文是"使'员工'由一份**可读**的岗位说明确定，
而不是平台内部写死的角色"。放在 `.md` 里，使用者改一份说明就能改岗位；
写成代码常量，改一次岗位就要改一次实现体，那与"补一份岗位说明并登记"（FR-071 ③）这个新增方式相互矛盾。

**MVP-1 只交这三岗**（`constitution.md` 原则 III 之后的批次澄清 + `spec.md:496`）：
其余六岗与"使用者新增岗位"的入口归 MVP-2。所以本模块交的是**前三岗的种子**，不是九岗清单。

`MOUNTS` 是 EN-04 的**生效口径**（谁可以挂这个岗），三条约束同时成立才敢这么写：
① 覆盖 `registry/agents.yaml` 的申报值 —— 申报超出生效集合会被注册节点整批拒（`registry/persist.py` 的 R4），
   所以生效集 MUST ⊇ 申报集，否则生产路径上三支工具全注册不上；
② 让把关矩阵在三种执行者下都闭合（否则 T086 对某些卡只能升人工，R-07 那条全链路走不完）；
③ 与宪法的白名单规则不冲突 —— rule 2（执行者是 WorkBuddy ⇒ 审查/验收只能是 Qoder 或豆包）
   是**执行时**的收窄，不在本表里表达，本表只说"这个岗允许哪些工具挂"。
把这三条写成用例而不是注释（见 `tests/guards/test_triple_roles.py` 的 ⑤⑥⑦ 三条），
因为这三条都会被人无意改掉：改 yaml 的申报、加第四支工具、改宪法的批次名单。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.persistence.models_registry import RoleSpec

ROLE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BuiltinRole:
    """一岗的**声明**（名字 + 是否只读）。正文不在这里，见 `charter_path()`。"""

    role_id: str
    name: str
    read_only: bool


#: 前三岗。`read_only` 的两条来自 FR-033（审查员 / 验收员 MUST 只读），
#: 架构师不是只读岗（它是总管，可写自己那一份 workspace），这一条 difference 由判据逐岗钉住。
BUILTIN_ROLES: tuple[BuiltinRole, ...] = (
    BuiltinRole(role_id="architect", name="架构师", read_only=False),
    BuiltinRole(role_id="reviewer", name="审查员", read_only=True),
    BuiltinRole(role_id="acceptor", name="验收员", read_only=True),
)

#: EN-04 生效口径：`{role_id: 允许挂载的 agent_id}`。顺序即落库顺序（数组列要可复现）。
#: `deepseek-reviewer`（Phase 17 P3）**只挂审查**：它不在这里之外的任何一行出现，
#: 所以执行席与验收席从生效口径就排除了它；manual 模式的额外剔除在选人池那一层
#: （`assign_guards.pool_from_role_specs`）——那是运行模式契约，不是批准面。
MOUNTS: dict[str, tuple[str, ...]] = {
    "architect": ("workbuddy", "qoder"),
    "reviewer": ("qoder", "doubao", "workbuddy", "deepseek-reviewer"),
    "acceptor": ("qoder", "workbuddy"),
}

#: `spec.md` FR-071 ① 要求正文含这两节。判据按小节名查，不查整句 —— 句子会被人改写，小节名不会。
REQUIRED_SECTIONS = ("岗位定位", "岗位守则")


class RoleSpecConflict(RuntimeError):
    """库里已有一行同名岗位但内容不同 ⇒ 拒。改内置岗位的正文属迁移，不属"再跑一次种子"。"""


def charter_path(role_id: str) -> Path:
    return ROLE_DIR / f"{role_id}.md"


def read_charter(role_id: str) -> str:
    """取该岗正文全文。缺文件 / 空文件 / 缺任一小节 ⇒ 直接抛，不落一行半成品进 EN-04。"""
    path = charter_path(role_id)
    if not path.is_file():
        raise RoleSpecConflict(
            f"岗位 {role_id!r} 的说明文件不在它该在的位置：{path.name} 缺席 ⇒ "
            "正文来源与存放位置是 T-3 闭合的内容，缺文件就是 T-3 没落地"
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RoleSpecConflict(f"岗位 {role_id!r} 的说明文件是空的 ⇒ 可读岗位说明不能是空串")
    missing = [section for section in REQUIRED_SECTIONS if f"## {section}" not in text]
    if missing:
        raise RoleSpecConflict(
            f"岗位 {role_id!r} 的说明缺小节 {missing} ⇒ FR-071 ① 要求正文同时含定位与守则"
        )
    return text


def builtin_rows() -> list[dict[str, object]]:
    """把声明 + 正文 + 生效挂载合成 EN-04 的行字典（种子与判据共用的唯一取数口）。"""
    rows: list[dict[str, object]] = []
    for role in BUILTIN_ROLES:
        rows.append(
            {
                "role_id": role.role_id,
                "name": role.name,
                "charter": read_charter(role.role_id),
                "read_only": role.read_only,
                "builtin": True,
                "mounted_agents": list(MOUNTS[role.role_id]),
            }
        )
    return rows


def _same(stored: object, declared: object) -> bool:
    """数组列按元素逐个比，标量列按值比。

    不用 `==` 直接比两列：`mounted_agents` 从库里回来是 `list`、声明里也是 `list`，但顺序不同就是不同口径
    （挂载集合的"顺序即落库顺序"写在 `MOUNTS` 上面那段）；而 `NULL` 与空数组在 PG 里也不是一回事，
    这里不把它们当等价，缺列会被算成差异报出来而不是悄悄放过。
    """
    if isinstance(declared, (list, tuple)) or isinstance(stored, (list, tuple)):
        return list(stored or []) == list(declared or [])
    return bool(stored == declared)


def seed_builtin_roles(session: Session, *, role_ids: Sequence[str] | None = None) -> dict[str, int]:
    """把前三岗写进 EN-04（幂等）。返回 `{"inserted": n, "unchanged": m}`。

    **同键不同内容 ⇒ 抛**，不静默覆盖：EN-04 是生效口径，覆盖它等于在不留痕的情况下改变
    "谁能挂哪个岗"，那会让已经注册上的申报（EN-05）与新口径对不上 —— 而那种不一致是注册节点要拒的。
    真要改正文，走一次迁移，不是再跑一次种子。
    """
    wanted = [row for row in builtin_rows() if role_ids is None or row["role_id"] in set(role_ids)]
    existing = {
        row.role_id: row
        for row in session.scalars(select(RoleSpec).where(RoleSpec.role_id.in_([w["role_id"] for w in wanted])))
    }
    inserted = unchanged = 0
    for row in wanted:
        found = existing.get(str(row["role_id"]))
        if found is None:
            session.add(RoleSpec(**row))
            inserted += 1
            continue
        differs = sorted(
            column
            for column, value in row.items()
            if column != "role_id" and _same(getattr(found, column), value) is False
        )
        if differs:
            raise RoleSpecConflict(
                f"EN-04 里已有 {row['role_id']!r}，但与内置声明在 {sorted(differs)} 上不一致 ⇒ "
                "种子不覆盖生效口径；改正文属迁移，改挂载属批准动作，两件事都要留痕"
            )
        unchanged += 1
    session.flush()
    return {"inserted": inserted, "unchanged": unchanged}
