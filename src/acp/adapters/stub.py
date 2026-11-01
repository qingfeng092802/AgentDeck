"""T123 的仿真 transport（owner 2026-10-16 五格裁决 ① 选甲）。

**为什么要有这一支**：R-07 那一发要的是"一条命令把全链路走完并留下可预览的产物"，而本轮的硬约束是
**零真实外部 API 调用**。两者同时成立只有一个形状：出站动作由一支**显式选中**的仿真 transport 应答。
这一支不提供任何厂商语义（没有 argv、没有 SDK、没有 HTTP），它只证明**装配点接对了**：
图 → 派发 → 下发 → 回收 → 门禁 → 审查 → 验收 → 交付包，这一段一条命令跑通。

**三条边界，写在这里而不是只在文档里说**（台账 `evidence/phase-12.md` §一 与 `quickstart.md §6` 同步登记）：

① **不构成 FR-002 / FR-004 的实证**：需求补全与拆单在这一发里由仿真侧给出的固定内容承担，
不是真总管。这一发跑通 = "链路通了"，不等于"总管会补全/会拆单"。
② **不构成 FR-028 / FR-029 的消息面实证**（owner 2026-10-16 裁 丙）：跨 Agent 的 `TaskResult`
**没有逐条审查结论的载体**（`protocol/messages.py` 的七个字段里就没有 findings），所以审查与验收的
结论由**装配点进程内构造**，不经这一支。⇒ 真接厂商时那个载体必须先补，否则审查结论进不来；
这一格已作为待裁项登记在 `tasks.md` 的 T123 行下。
③ **`usage` 一律为空**：没有真实调用就没有用量。宁可让 EN-13 在这一发里零行，也不造数
（口径同宪法「不造数」与 `cost/ledger.py` 那侧的"读数只来自真回执"）。
④ **交付物必须由卡点名**（`expected_files`）：卡上没写文件名就停下，不回落到 `task_id.html`。
回落会让一张没写清交付物的卡照样"跑通"，于是"产物 = 卡上声明的那几份"这件事再没人管。

**选中规则（"不给默认、缺它拒跑"落在 `resolve_transport_flag()`，不在 CLI 里）**：
理由与 `guard/autonomy.py` 那道唯一拦截点同一件事 —— 把"必须显式"写在函数上，第二个调用方就绕不过去；
写在 `argparse` 的 `default=` 上，下一个装配点复制参数就会带一个默认值出来。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import re

from acp.bus import acl, layout
from acp.bus.gateway import BusGateway
from acp.protocol import CapabilityManifest, Handle, Task, TaskResult
from acp.protocol.messages import ArtifactRef
from acp.registry.loader import load_registrations

TRANSPORT_FLAG = "stub"

#: 协议四支之外的"必须显式"这条的门名。错误消息里只出现门名与收到的值，不带任何凭据形态的内容。
FLAG_MISSING = "transport_flag_missing"
FLAG_UNKNOWN = "transport_flag_unknown"
HANDLE_UNKNOWN = "transport_handle_unknown"
#: 与 `HANDLE_UNKNOWN` **分列**：一个是"回执不是我这支发出去的"（身份串了），
#: 一个是"我这支没下发过这张卡"（进程换了）。共用一个代号的话，装配点出错时这两种根本分不开。
AGENT_MISMATCH = "transport_agent_mismatch"
#: 卡上没点名交付物。与 `BODY_MISSING` 分列：一个是"没说清要什么"（拆单侧的问题），
#: 一个是"要的东西仿真侧没有"（仿真侧的问题，得往 `PAGE_BODIES` 加）。混成一个代号就没法归因。
UNNAMED_DELIVERABLE = "transport_deliverable_unnamed"
BODY_MISSING = "stub_body_missing"


class StubMisuse(ValueError):
    """仿真侧的拒绝。`code` 是稳定键名（措辞可以改，代号改了就是契约变了，口径同 `dispatch.SlotConfigError`）。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def resolve_transport_flag(raw: str | None) -> str:
    """只有 `stub` 这一个值能通过；**空值不通过**（这就是"不给默认"）。

    故意不接 `bool`、不做大小写折叠：`--transport Stub` 通过与否是用户在命令行上打错没打错的问题，
    而"漏了旗标"与"打错旗标"必须给出**不同代号**，否则运维读到的是同一句"配置不对"，
    分不清是忘了还是打错了。
    """
    if raw is None or raw == "":
        raise StubMisuse(
            f"缺 `--transport` ⇒ 本装配点不预设任何 transport，也不回落到场内那三支真适配器"
            "（R-07 那一发 MUST 显式选中仿真侧，否则一次误敲就是一次真实外部调用）",
            code=FLAG_MISSING,
        )
    if raw != TRANSPORT_FLAG:
        raise StubMisuse(
            f"`--transport` 收到 {raw!r}，本轮只实现了 {TRANSPORT_FLAG!r} ⇒ 拒跑"
            "（真适配器由注册表按 `adapter_entry` 定位，不走这个旗标）",
            code=FLAG_UNKNOWN,
        )
    return raw


_FILE_TOKEN = re.compile(r"[\w.-]+\.(?:html|css)", re.IGNORECASE)


def expected_files(task: Task) -> tuple[str, ...]:
    """这张卡要交哪几份文件 —— 只认**卡上点名的文件名**，卡没点名就停下。

    刻意不做"回落到 `task_id.html`"那种省事写法：回落会让一张没写清交付物的卡交出一份
    没人要求的网页，链路照样"跑通"，而 R-07 要看的那个"产物 = 卡上声明的那几份"这件事就再没人管。
    停下反而是可诊断的：错误里点名是哪张卡、它的 `expected_output` 里没找到文件名。
    只取**文件名**：路径由网关按归属表落到本工具自己的 workspace，仿真侧不自称能写公共区。
    """
    names = tuple(dict.fromkeys(match.group(0).lower() for match in _FILE_TOKEN.finditer(task.expected_output)))
    if not names:
        raise StubMisuse(
            f"卡 {task.task_id} 的期望产物里没有任何 `.html` / `.css` 文件名 ⇒ 仿真侧不知道该交什么，"
            "停下而不是挑一份默认页（拆单侧要把交付物点名）",
            code=UNNAMED_DELIVERABLE,
        )
    return names


def body_for(file_name: str) -> str:
    """仿真侧的正文来源只有一份：模块末尾那个 `PAGE_BODIES`。**不在运行时现编网页** ——
    现编就意味着这一发的内容取决于一 Model，而 R-07 要验的是链路，不是文案。
    """
    body = PAGE_BODIES.get(file_name)
    if not body:
        raise StubMisuse(
            f"{file_name!r} 在仿真侧没有对应正文 ⇒ 宁缺勿造，要加就往 `PAGE_BODIES` 加，"
            "不在这里给一份兜底文案",
            code=BODY_MISSING,
        )
    return body


def artifact_path(agent_id: str, file_name: str) -> str:
    """`<agent_id>-workspace/<文件名>` —— 这条不是拼接偏好，是归属表要求的形状。

    `acl.RULES` 里适配器唯一能写的是 `*-workspace/**`，而那个模式要**两段以上**（`_pattern_hit`
    对 `X/**` 判的是 `len(parts) > len(head_parts)`）⇒ 裸文件名匹配不到任何规则，落到
    `unmapped_path` 403。第一版就是在这里直接撞红的，所以把它做成一个函数而不是一处字面量：
    判据能在不碰网关的情况下钉住这个形状。
    """
    return f"{layout.workspace_dir_name(agent_id)}/{file_name}"


class StubAdapter:
    """`AgentAdapter` 四支的仿真实现（契约 §4 同形；`tests/contract/test_adapter_protocol.py` 比的是形状）。

    **只应答执行席**：审查与验收在这一发里不经 transport（owner 2026-10-16 裁 丙，见模块头边界 ②），
    所以这一支没有"审查请求"这一形 —— 真接厂商时那一形要连 findings 的载体一起补。

    刻意**没有**的两样：
    - 没有 `plan()` / `split()`：调度与拆单不属于 transport（宪法 II），这一支也不碰；
    - 没有厂商字段：`log_excerpt` 那句固定文案里没有令牌形态的串，`usage` 是空列表（边界 ③）。

    观察面（`submits` / `collects` / `cancels` / `written`）是给判据用的，**不在协议里**，
    所以编排层即使拿到这个实例也读不到它们 —— 契约 §4 的围栏比对只认四支 + `agent_id`。
    """

    #: 数据成员在类层声明（口径同三支真适配器）：契约 §4 围栏写的是 `agent_id: str`，
    #: 少这行注解，"实现与契约同形"那条判据会报"数据成员只在一侧出现"。
    agent_id: str

    def __init__(
        self,
        *,
        agent_id: str,
        gateway: BusGateway,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not agent_id:
            raise StubMisuse(
                "仿真侧也要一个明确的服务身份（workspace 归属按它算，空身份会把文件写进别人的目录）",
                code="stub_agent_id_missing",
            )
        self.agent_id = agent_id
        self.gateway = gateway
        self._clock = clock
        self._tasks: dict[str, Task] = {}
        self._cancelled: set[str] = set()
        self.submits = 0
        self.collects = 0
        self.cancels = 0
        self.written: list[str] = []

    # --- describe ---------------------------------------------------------------

    def describe(self) -> CapabilityManifest:
        """交回注册表里那份申报单：这一支不生产能力事实，也不合并默认值（口径同三支真适配器）。"""
        for registration in load_registrations():
            if registration.agent_id == self.agent_id:
                return registration.manifest
        raise StubMisuse(
            f"注册表里没有 `{self.agent_id}` 这一条 ⇒ 仿真侧也拿不到申报单"
            "（唯一来源 = `config/agents.yaml`，仿真不改注册面，只替出站动作应答）",
            code="stub_not_registered",
        )

    # --- submit -----------------------------------------------------------------

    def submit(self, task: Task) -> Handle:
        """记下这张卡要交什么，回执一个可重查的 `native_ref`。**不在下发时写文件** ——
        写发生在 `collect()`，与"回收才落盘"那条真适配器口径一致（FR-019 的实测值来自盘上）。
        """
        self.submits += 1
        self._tasks[task.task_id] = task
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=f"stub:{self.agent_id}:{task.task_id}",
            submitted_at=self._clock(),
        )

    # --- collect ----------------------------------------------------------------

    def collect(self, handle: Handle) -> TaskResult:
        """落盘 + **从盘上量**（`gateway.write_and_measure`），自报的字节/行数一个都不读。

        撤销过的单子必须交不出东西：`cancel()` 之后再 `collect()` ⇒ 拒，而不是"照样落一份"。
        这一条不是防御性冗余，是 FR-022 那条"撤单后不许再写"在仿真侧的同形约束。
        """
        if handle.agent_id != self.agent_id:
            raise StubMisuse(
                f"回执的 agent_id 是 {handle.agent_id!r}，不是本 transport 的 {self.agent_id!r}",
                code=AGENT_MISMATCH,
            )
        if handle.task_id in self._cancelled:
            raise StubMisuse(
                f"卡 {handle.task_id} 已撤销 ⇒ 撤销之后落盘就是越界", code="stub_cancelled"
            )
        task = self._tasks.get(handle.task_id)
        if task is None:
            raise StubMisuse(
                f"回执 {handle.native_ref!r} 在本 transport 里没有对应的下发记录 ⇒ "
                "`submit()` 没跑过，或者跑在了别的进程里（仿真侧不假装能凭 `native_ref` 反推内容）",
                code=HANDLE_UNKNOWN,
            )
        self.collects += 1
        actor = acl.Actor(actor_id=f"{acl.ADAPTER_OWNER_PREFIX}{self.agent_id}", role_id=acl.NO_ROLE_DECLARED)
        capability = acl.Capability(agent_id=self.agent_id, task_id=handle.task_id)
        measured: list[ArtifactRef] = []
        for file_name in expected_files(task):
            rel_path = artifact_path(self.agent_id, file_name)
            measured.append(self.gateway.write_and_measure(actor, rel_path, body_for(file_name), capability))
            self.written.append(rel_path)
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status="success",
            artifacts=measured,
            log_excerpt=STUB_LOG_LINE,
            self_assessment=f"仿真交付 {len(measured)} 份产物；本次运行零外部调用",
            issues=[],
            usage=[],
        )

    # --- cancel -----------------------------------------------------------------

    def cancel(self, handle: Handle) -> None:
        """记一个撤销标记。返回值刻意是 `None`：撤销不是"取回一份东西"，没什么可回。"""
        self.cancels += 1
        self._cancelled.add(handle.task_id)


#: 回收报文里那句固定文案。写成一行的理由：`log_excerpt` 会进 EN-14 与交付包，
#: 多行文本会让"这一发是仿真"这件事在日志里被夹在别的行中间看不见。
STUB_LOG_LINE = "stub transport: no external call made (R-07 rehearsal only)"

# —————————————————————————————— 正文（静态文案，由本轮作者手写，不来自任何模型输出）
#
# 四页一表，互相之间有可点的相对链接（R-07 的"链接可点"量的就是这个），且没有任何空容器
# （"页面无空白区块"）。样式表被每一页 `<link>` 引到，路径都是同目录相对名 ⇒ 交付包把它们
# 原样放在一起就能预览，不需要构建步骤。

PAGE_BODIES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>示例产品 · 首页</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="site-header">
    <h1>示例产品：把多Agent协作放到一张桌面上</h1>
    <p class="lead">一支工具做，一支工具审，一支工具验收；每一步都留下一条可查的记录。</p>
    <nav class="site-nav" aria-label="主导航">
      <a href="about.html">产品说明</a>
      <a href="contact.html">联系我们</a>
    </nav>
  </header>
  <main class="cards">
    <section class="card">
      <h2>过程看得见</h2>
      <p>谁做的、做了什么、结果是什么、审查意见是什么，四件事各自成行，可以在看板上逐张卡查。</p>
    </section>
    <section class="card">
      <h2>把关不重叠</h2>
      <p>执行、审查、验收三个岗位必须是三支不同的工具，同一支工具不能给自己签字。</p>
    </section>
    <section class="card">
      <h2>产物是文件</h2>
      <p>交付以总线上的实际文件为准，字节数与行数由网关从磁盘量出来，不采信自报。</p>
    </section>
  </main>
  <footer class="site-footer"><p>本页是 R-07 演练产物，内容不构成对外承诺。</p></footer>
</body>
</html>
""",
    "about.html": """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>示例产品 · 产品说明</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="site-header">
    <h1>产品说明</h1>
    <nav class="site-nav" aria-label="主导航">
      <a href="index.html">返回首页</a>
      <a href="contact.html">联系我们</a>
    </nav>
  </header>
  <main>
    <section class="prose">
      <h2>它解决的那件事</h2>
      <p>一个人同时驱动多支工具时，最容易丢的是过程：哪张卡交给了谁、为什么这么判、被退回了几次。</p>
      <p>本产品把这三件事变成库里的三行，界面上逐张卡可展开，展开的就是当初落下的那三行。</p>
    </section>
    <section class="prose">
      <h2>它不做的事</h2>
      <ul>
        <li>不替人决定验收标准是否满足，只把满足与否连同依据一起记下来。</li>
        <li>不把会话内容当交付，交付只认落在总线上的文件。</li>
        <li>不在缺少把关人的时候静默改派，凑不出合法三元组就停下来等人。</li>
      </ul>
    </section>
  </main>
  <footer class="site-footer"><p>本页是 R-07 演练产物，内容不构成对外承诺。</p></footer>
</body>
</html>
""",
    "contact.html": """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>示例产品 · 联系我们</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="site-header">
    <h1>联系我们</h1>
    <nav class="site-nav" aria-label="主导航">
      <a href="index.html">返回首页</a>
      <a href="about.html">产品说明</a>
    </nav>
  </header>
  <main>
    <section class="prose">
      <h2>唯一联系方式</h2>
      <p>邮箱：<a href="mailto:team@example.invalid">team@example.invalid</a>。标题请写任务卡号，方便对着记录回。</p>
      <p>本页面刻意不放表单：没有后端的时候，一个提交按钮就是一条会报错的死路。</p>
    </section>
  </main>
  <footer class="site-footer"><p>本页是 R-07 演练产物，内容不构成对外承诺。</p></footer>
</body>
</html>
""",
    "styles.css": """/* R-07 演练用样式：一份文件被三页共同引用，颜色对比按 WCAG AA 取值。 */
:root {
  --ink: #14202b;
  --paper: #ffffff;
  --accent: #1c5cab;
  --muted: #4f6273;
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 2rem 1.25rem 3rem;
  max-width: 44rem;
  font: 16px/1.7 "Segoe UI", "Microsoft YaHei", sans-serif;
  color: var(--ink);
  background: var(--paper);
}
.site-header h1 { margin: 0 0 .5rem; font-size: 1.9rem; }
.lead { margin: 0 0 1rem; color: var(--muted); }
.site-nav { display: flex; gap: 1rem; margin-bottom: 1.5rem; }
.site-nav a { color: var(--accent); text-decoration: underline; }
.cards { display: grid; gap: 1rem; grid-template-columns: 1fr; }
.card, .prose {
  border: 1px solid #d3dce5;
  border-radius: .6rem;
  padding: 1rem 1.15rem;
}
.card h2, .prose h2 { margin-top: 0; font-size: 1.15rem; }
.site-footer { margin-top: 2rem; color: var(--muted); font-size: .85rem; }
""",
}
