"""EN-16 `autonomy_state` 的读写（T101 写 `danger_list_version` 一列，T102 建行、读档、改档，
T105 写 `last_intercept_at` 一列）。

**为什么写侧拆成几个各管一列的函数，而不是一个"整行 upsert"**：这张表有四个合法的写者，
各自管一件事，触发时机互不相干 ——

- `danger_list_version`：随**清单文件**变。触发点是重载清单（`config/loader.load_danger_list()`），
  与谁在跑无关，也**不是一次改档**。
- `mode` / `updated_by` / `updated_at`：随**使用者的改档动作**变（`set_autonomy_mode()`）。
  `data-model.md` EN-16 的 `updated_by` 行与 §4 的强制点表把对外那道门指给 `api/autonomy.py`（T103），
  但**判"这个主体是不是机器"这件事在本模块成文一次**，T103 只做"抛错 → 403"的翻译 ——
  同一个规则两处各写一份就会漂成"端点放行了、仓储还在拒"。
- `last_intercept_at`：随**拦截发生**变，触发点在 `guard/autonomy.py`（T104 判定、T105 落这一列，
  写者 = 本模块的 `record_intercept()`）。这一列**不记"谁"**：拦截的操作者是一支工具，
  把他的名字写进 `updated_by` 就是把"改档主体"那列用成"最近一次动作的主体"，两件事会互相冒充。

三个写者各写各的列 ⇒ "整行 upsert" 会把不属于它的那几列一起改掉，出现的形式是
**重载一次清单就把档位刷回默认**，或**记一次拦截就把改档人覆盖成拦截时刻的操作者**。
这与 Phase 7 的 P1-5（`review.py` 顺手写 `review_round` 而吃掉验收轮次预算）是同一条失效形状，
所以这里用同一种钉子：`tests/integration/test_autonomy_state.py` 里有 AST 用例，
把"某个函数写了哪些属性"当集合断言，多写一列就红。

**建行（T102，owner 2026-10-15 裁决 2 + 本轮"C + E 结合"的处置）**：`ensure_autonomy_state()`
缺行时按 `DEFAULT_MODE = SAFE` 建一行，已有行**一个字段都不改**地原样返回；读取路径
（`read_autonomy_state()`）缺行只返回 `None`，**不建行** —— 无副作用这句话在这里是可断言的，
库里那一行的存在与否只由建档路径决定。为什么不把"读时顺手建行"合并成一件事：
那会让"谁第一次碰了这张表"变成建档凭证，而建档的触发点是启动（selfcheck / lifespan，T103 之后接线），
两者混在一起就再也读不出"是启动建的还是有人查出来的"。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.config.loader import DangerList
from acp.persistence.enums import values_of
from acp.persistence.models_core import AgentUnit
from acp.persistence.models_registry import AutonomyState

#: `record_danger_list_version()` 唯一有权改的列。写成常量是给 AST 那条用例一个对照物，
#: 不是给调用方看的提示。
OWNED_COLUMNS = ("danger_list_version",)

#: `set_autonomy_mode()` 唯一有权改的列（同上：AST 用例的对照物）。
#: 里面**没有** `danger_list_version`，也**没有** `last_intercept_at` —— 那两列各有自己的写者。
MODE_OWNED_COLUMNS = ("mode", "updated_by", "updated_at")

#: `record_intercept()` 唯一有权改的列（同上：AST 用例的对照物）。
#: 这一列与"改档"那三列**互不相交**是有意的：拦截时刻不是改档动作，把它写进 `updated_at`
#: 就会出现"使用者没动过档位，而 EN-16 说有人动过"。
INTERCEPT_OWNED_COLUMNS = ("last_intercept_at",)

#: 新项目第一行的档位（owner 2026-10-15 裁决 2："**默认 SAFE**"；出处 = `data-model.md` EN-16 的
#: `mode` 行与"建行与默认档"段）。这是**应用层的建行默认，不是 schema 默认** ⇒ 没有为它加迁移、
#: 没有 `server_default`、没有回填脚本。下一行的断言防的是"域里改了名而这里还留着旧字面串"。
DEFAULT_MODE = "SAFE"
if DEFAULT_MODE not in values_of("autonomy_mode"):
    raise RuntimeError(
        f"DEFAULT_MODE={DEFAULT_MODE!r} 不在 autonomy_mode 域里 ⇒ 代码与 data-model.md 脱钩了"
    )

#: 建档行的 `updated_by` 取值。**建一档不是"改档"**：第一行是平台自己起的头，把它签成一个使用者名
#: 就是拿审计列冒充"某人做过决定"。所以这一列在建档路径上是这个常量，而 `set_autonomy_mode()`
#: **拒收**它（否则平台可以走"改档"的名义自切档位，正是 FR-055 要挡的那条）。
#: 值 = `bootstrap`（owner 2026-10-15 裁决 2，原话："『platform』太模糊；『bootstrap』明确指建档初始行"；
#: 此前这一格写的是 `platform`，是 T102 那轮我自己定的，他点名换掉）。
#: 常量名仍叫 `PLATFORM_PRINCIPAL`：它指的是**主体**（平台自己），而值指的是**动作**（建档），
#: 两个词各自回答两个问题，把名字一起改掉就会读不出这层区分。
#: 撞名代价明写：真有一个使用者自称 `bootstrap` 的话，他改档会被拒 —— 失败关闭，不误开。
PLATFORM_PRINCIPAL = "bootstrap"

#: `AutonomyWriteRejected.code` 的稳定键名（唯一载体在本模块，T103 只把它映进响应体，
#: 不在 HTTP 层再抄一份字符串 —— 同 `approval/repository.py` 的 `PRINCIPAL_NOT_ALLOWED`）。
#: 取同一个串是与那条 403 对齐的：`data-model.md` §4 的强制点表已经写了
#: "拒绝写入（`403 principal_not_allowed`）"，改档这条没有理由换个键名。
PRINCIPAL_NOT_ALLOWED = "principal_not_allowed"
MODE_NOT_ALLOWED = "mode_not_allowed"
DANGER_LIST_REQUIRED = "danger_list_required"

#: code → HTTP 状态（owner 2026-10-15 裁决 1 逐条给值，原话："mode_not_allowed = 422
#: （与 guard_not_closable 同类：值不合法）"、"danger_list_required = 400（与 payload_incomplete
#: 同类：缺必填）"；`principal_not_allowed` 的 403 早有出处 = `data-model.md` §4 的强制点表）。
#: 成文在仓储而不是散在端点里，口径同 `approval/repository.py::HTTP_STATUS`：映射表只有一份，
#: HTTP 层拿 `exc.http_status` 用，不自己判 code。
#: 下一节的 `AutonomyWriteRejected.__init__` 会在这里查表 ⇒ 新增一个 code 而忘了给状态，
#: 当场 KeyError，不会拖到线上变成"500 冒充业务拒绝"。
HTTP_STATUS: dict[str, int] = {
    PRINCIPAL_NOT_ALLOWED: 403,
    MODE_NOT_ALLOWED: 422,
    DANGER_LIST_REQUIRED: 400,
}


def _now(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else dt.datetime.now(dt.timezone.utc)


class AutonomyStateMissing(RuntimeError):
    """该项目的 EN-16 行还不存在。消息里只有 `project_key`（项目标识，不是凭据）。"""

    def __init__(self, project_key: str) -> None:
        super().__init__(
            f"autonomy_state 里没有 project_key={project_key!r} 这一行 ⇒ "
            "本模块的按列写入（重载清单 / 改档）都要求行已存在，建行归 "
            "`ensure_autonomy_state()`（owner 2026-10-15 裁决 2：首行默认 SAFE）"
        )
        self.project_key = project_key


class AutonomyWriteRejected(ValueError):
    """这次写入被拒（档位不对 / 主体不是使用者 / 缺建行所需的指纹）。

    `code` 是稳定键名，`http_status` 是它的 HTTP 归属（查 `HTTP_STATUS`，同一处成文）。
    HTTP 层（T103 的 `api/autonomy.py`）只用这两个字段做翻译，不在那边再判一次 ——
    口径同 `api/approvals.py` 用 `repository.HTTP_STATUS` 而不自己写映射。
    查表放在构造器里 ⇒ 映射不是装饰物：漏一格就在当场炸，不会拖成"500 冒充业务拒绝"。
    消息里只带身份串与域名，不带任何凭据。
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        self.http_status = HTTP_STATUS[code]
        super().__init__(message)


def _agent_ids(session: Session) -> frozenset[str]:
    """EN-03 里注册过的全部 `agent_id`。**机器身份的名单只从这里取**，不在本模块抄一份：
    抄一份就会漂成"新注册的工具没进名单"，而那正是"工具自改档位"的入口。
    """
    return frozenset(session.scalars(select(AgentUnit.agent_id)))


def _guard_updated_by(
    session: Session,
    updated_by: str,
    *,
    machine_principals: Sequence[str],
    allow_platform: bool,
    action: str,
) -> str:
    """判定"这个 `updated_by` 能不能作为改档主体"，返回归一化后的串（去首尾空白）。

    四类拒法，全部**失败关闭**：空串 / EN-03 注册过的 `agent_id` / 调用方交来的自动主体
    （`machine_principals`）/ 建档主体（`PLATFORM_PRINCIPAL`）走改档路径。

    为什么 `machine_principals` 由调用方交进来而不是在本模块 import：
    `AUTOMATED_ACTOR` 住在 `orchestrate/nodes/select_worker.py`，而那边 import 了
    `acp.persistence.events` ⇒ 反过来 import 就是向上依赖成环（同一个理由见 `guard/` 各处）。
    在这个文件里再写一遍 `"orchestrator"` 就是给一个已经有唯一载体的字面串加第二份，
    而那个常量自己的文档串写着"这是一条临时约定，不是一个已成立的身份"（Phase 7 可能改名）⇒
    两份必漂。所以留接缝，由 T103 把 `{AUTOMATED_ACTOR}` 传进来。

    **这一档证明不到什么（明写）**：MVP-1 没有认证（同 `api/approvals.py` 模块头第 2 条），
    所以这里挡的是"披错身份误操作"，**挡不住冒充** —— 一支工具填一个使用者名，本函数分不出来。
    """
    principal = updated_by.strip()
    if not principal:
        raise AutonomyWriteRejected(
            f"{action} 的 `updated_by` 是空白 ⇒ 宁缺毋滥：没有主体就没有审计行",
            code=PRINCIPAL_NOT_ALLOWED,
        )
    if principal in _agent_ids(session):
        raise AutonomyWriteRejected(
            f"{principal!r} 是 EN-03 注册过的 agent_id ⇒ {action} 只能是使用者做的"
            "（FR-055：'可切换'指使用者可切，不指平台可自切）",
            code=PRINCIPAL_NOT_ALLOWED,
        )
    if principal in {item.strip() for item in machine_principals}:
        raise AutonomyWriteRejected(
            f"{principal!r} 是调用方申报的自动主体 ⇒ {action} 拒绝",
            code=PRINCIPAL_NOT_ALLOWED,
        )
    if not allow_platform and principal == PLATFORM_PRINCIPAL:
        raise AutonomyWriteRejected(
            f"{PLATFORM_PRINCIPAL!r} 是建档主体，不是改档主体 ⇒ 平台可以用默认档起个头（"
            f"`ensure_autonomy_state()`），但不能以'平台'的名义换档（{action} 要走使用者的身份）",
            code=PRINCIPAL_NOT_ALLOWED,
        )
    return principal


def _create_row(
    session: Session,
    project_key: str,
    *,
    mode: str,
    danger_list: DangerList,
    updated_by: str,
    moment: dt.datetime,
) -> AutonomyState:
    """插一行。两个公开函数（建档 / 改档时无行 upsert）共用这一个构造，不留两份建行形状。"""
    row = AutonomyState(
        project_key=project_key,
        mode=mode,
        danger_list_version=danger_list.fingerprint,
        updated_by=updated_by,
        updated_at=moment,
    )
    session.add(row)
    session.flush()
    return row


def ensure_autonomy_state(
    session: Session,
    *,
    project_key: str,
    danger_list: DangerList,
    updated_by: str = PLATFORM_PRINCIPAL,
    now: dt.datetime | None = None,
) -> AutonomyState:
    """缺行就按 `DEFAULT_MODE`（SAFE）建一行；已有行**一个字段都不改**地原样返回（幂等）。

    幂等这件事要挡的失效形状有两种，都写成用例钉住了：① 调两次建出两行（PK 会拦，但拦不住
    "先删再建"那种写法）；② 第二次调用把已有行刷回默认档 —— 那等于"任何一次启动检查都能把使用者
    切的 FULL 档冲掉"，而 selfcheck 是天天跑的。所以已有分支直接 `return`，不比较、不补写。

    不提交（事务边界归调用方，口径同 `persistence/events.py`）。
    `danger_list` 是必填项：`danger_list_version` 在 EN-16 是 NOT NULL，本模块**不编一个指纹**，
    也不在这里顺手 `load_danger_list()`（那会让"建行"隐式读一次磁盘，而调用方本来手里就有一份）。
    """
    row = session.get(AutonomyState, project_key)
    if row is not None:
        return row
    principal = _guard_updated_by(
        session, updated_by, machine_principals=(), allow_platform=True, action="建档"
    )
    return _create_row(
        session,
        project_key,
        mode=DEFAULT_MODE,
        danger_list=danger_list,
        updated_by=principal,
        moment=_now(now),
    )


def read_autonomy_state(session: Session, *, project_key: str) -> AutonomyState | None:
    """只读：有行给行，无行给 `None`。**不建行、不写任何列、不 flush。**

    "无行 ⇒ 读成 SAFE"这句（owner 的处置表）由调用方用 `DEFAULT_MODE` 表达，不在这里建行：
    读取路径一旦能写库，"这一行是启动建的"和"这一行是有人查出来的"就分不开了，
    而 EN-16 是审计表，建档时点是它的一个读数。`DEFAULT_MODE` 是这个模块的唯一出处，
    调用方 MUST NOT 再抄 `"SAFE"` 字面串（抄了就会出现"读取路径默认 SAFE、建档默认另一档"的漂法）。
    """
    return session.get(AutonomyState, project_key)


def set_autonomy_mode(
    session: Session,
    mode: str,
    updated_by: str,
    *,
    project_key: str,
    danger_list: DangerList | None = None,
    machine_principals: Sequence[str] = (),
    now: dt.datetime | None = None,
) -> AutonomyState:
    """改档（upsert）：有行改 `mode` / `updated_by` / `updated_at` 三列，无行建行。

    两档之外没有第三条路：`mode` MUST 是 `autonomy_mode` 域里的值，否则**在碰到库之前**就拒。
    在库里拦（PG 的原生枚举也会拦）不算做到位 —— 那时报的是 `DataError`，
    T103 拿不到可映射的 `code`，使用者看到的就是一句 500。

    改档**一定**写 `updated_at`，即使新旧档位相同（使用者确实动过一次手，
    这一列记的是"最后一次改档动作"，不是"最后一次差值"）；但 MUST NOT 碰
    `danger_list_version` 与 `last_intercept_at` —— 那两个写者各自在别处（见模块头）。

    无行时 `danger_list` 变必填（建行要给 NOT NULL 的指纹列），缺了就拒而不是拿
    `"sha256:0000…"` 之类的占位串凑一行 —— 占位串进了审计表就再也读不出"当时是哪版清单"。
    """
    if mode not in values_of("autonomy_mode"):
        raise AutonomyWriteRejected(
            f"autonomy_mode 只有 {list(values_of('autonomy_mode'))} 两档（宪法「三」/FR-055），"
            f"收到 {mode!r} ⇒ 第三档不存在，改档不生效",
            code=MODE_NOT_ALLOWED,
        )
    principal = _guard_updated_by(
        session,
        updated_by,
        machine_principals=machine_principals,
        allow_platform=False,
        action="改档",
    )
    moment = _now(now)
    row = session.get(AutonomyState, project_key)
    if row is None:
        if danger_list is None:
            raise AutonomyWriteRejected(
                f"project_key={project_key!r} 还没有 EN-16 行，改档要建行 ⇒ "
                "必须同时交来 `danger_list`（`danger_list_version` 是 NOT NULL，本模块不编指纹）",
                code=DANGER_LIST_REQUIRED,
            )
        return _create_row(
            session,
            project_key,
            mode=mode,
            danger_list=danger_list,
            updated_by=principal,
            moment=moment,
        )
    row.mode = mode
    row.updated_by = principal
    row.updated_at = moment
    session.flush()
    return row


def record_danger_list_version(
    session: Session, project_key: str, danger_list: DangerList
) -> AutonomyState:
    """把清单指纹落到 EN-16。不提交（事务边界归调用方，口径同 `persistence/events.py`）。

    只认 `danger_list.fingerprint`（语义指纹），不认 `source_sha256`：EN-16 那一列要回答的是
    "当时生效的是哪版**策略**"，而注释改动换版了文件、没换策略。
    """
    row = session.get(AutonomyState, project_key)
    if row is None:
        raise AutonomyStateMissing(project_key)
    row.danger_list_version = danger_list.fingerprint
    session.flush()
    return row


def record_intercept(
    session: Session, project_key: str, *, now: dt.datetime | None = None
) -> AutonomyState:
    """把"刚刚拦下一次"落到 EN-16 的 `last_intercept_at` 一列。**只写这一列**（AST 第三个对照物）。

    **缺行就抛，不顺手建行**（口径同 owner 2026-10-15 裁决 2 那句"库里那一行的存在与否只由建档路径决定"）：
    拦截路径手里没有"这个项目该拿哪版清单建第一行"这个判断权，而顺手建行会把"启动建档"与
    "第一次撞到拦截"两种世界混成同一个读数 —— EN-16 是审计表，建档时点本身就是它的一个读数。
    ⇒ 装配点 MUST 先调 `ensure_autonomy_state()`（`tasks.md` 的 T103 行下第 ① 项，宿主 = T123）。

    不提交（事务边界归调用方，口径同 `persistence/events.py`）：EN-14 那行流水与本列在**同一笔事务**里，
    所以"有流水而当前态没动"与"当前态动了而没流水"两种半程态会在一次回滚里一起消失。
    """
    row = session.get(AutonomyState, project_key)
    if row is None:
        raise AutonomyStateMissing(project_key)
    row.last_intercept_at = _now(now)
    session.flush()
    return row
