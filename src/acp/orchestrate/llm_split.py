"""真拆单：让模型按"需求原文 + 会议结论"拆卡，替掉固定四张的 `StubOrchestrator`。

为什么必须做这一步：执行侧下发给 Agent 的两格是
- `objective` ← **卡标题**（`run._card_face_of` 的投影）
- `expected_output` ← **验收标准**拼接

所以拆单器写什么，qoder 就照着做什么。Stub 的标题是"首页/产品说明页/联系页"，
于是产物里一个"咖啡"都没有 —— 不是执行器不行，是它没被告知。

三条设计约束：

1. **输出必须是严格的 JSON**，且只允许这一步用到的字段（`extra` 一律拒）。模型多写的键意味着
   它在猜协议；收下就等于让"协议之外的东西"静默生效。
2. **卡必须过既有规则**：`validate_draft` → `normalize`（成环/引用非法在这里被拒），
   验收映射再过 `validate_links`（一条判据恰一条映射）。本模块**不重写**任何一条规则，
   只把模型输出折成 `CardDraft` 再交给它们判。
3. **跑偏就回落**：任何一步失败（超时、JSON 坏、规则拒）都交回 `fallback`（生产 = Stub），
   并把原因码留在 `last_error` 里给留痕用。**绝不半信半疑地收一半卡** —— 那会造出一张拆了一半的单。

标题里放需求语义（"手冲咖啡 · 首页"）不是装饰：它是执行器唯一能看到的"这是什么站"。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, cast, get_args

from pydantic import Field, ValidationError

from acp.config import runtime as runtime_config
from acp.docs.service import PRD_SECTION
from acp.orchestrate.nodes.normalize import DependencyViolation, normalize
from acp.orchestrate.nodes.split import (
    AcceptanceLink,
    CardDraft,
    SplitViolation,
    validate_draft,
    validate_links,
)
from acp.prompts import PromptNotFoundError, compose_system_prompt
from acp.protocol import ArtifactFloor, ArtifactFormat
from acp.protocol.messages import CUSTOMER_INPUT_CLOSE, CUSTOMER_INPUT_OPEN, UNTRUSTED_INPUT_NOTE
from acp.protocol.refs import StrictModel

#: 卡数上界。超过六张的"拆单"通常是模型把验收标准也当成了卡。
MAX_CARDS = 6

#: 每张卡的判据条数：1 条太松、4 条以上执行器会挑着做。
MIN_CRITERIA = 2
MAX_CRITERIA = 3

#: R-26 卡点的**对外错误码**（2026-10-28 裁决：方案二）。
#:
#: 为什么必须是一个**固定码**、而不是把最后一次的 `split_extra_field` / `split_bad_deps` 抛上去：
#: 那两个码是"这一跑具体错在哪"的诊断信息，一次一换；而屏上要判的是"**这一单是不是在等人**"。
#: 把诊断码当事件码用，前端就得维护一张越来越长的枚举，漏一个的表现是
#: "该显示琥珀色的单显示成了红色失败"。所以两者分开：
#: `code` 恒为 `split_needs_human`（读面据此判状态），`cause_code` 留诊断。
#: 它同时是 EN-01 状态枚举之外的那一格 —— **不加枚举值、不写迁移**：
#: 需求单仍是 `FAILED`（库里的域只有那五个值），区别落在留痕的 `code` 与屏上的那一档。
NEEDS_HUMAN_CODE = "split_needs_human"


class SplitUnavailable(RuntimeError):
    """模型拆不动（超时 / JSON 坏 / 规则拒）。`code` 进留痕，不带正文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SplitNeedsHuman(SplitUnavailable):
    """契约校验连续失败，且调用方要求**不回落**（R-26 卡点：交人工，不放脏数据过关）。

    与 `SplitUnavailable` 的区别只有一条，但那条很要紧：`SplitUnavailable` 的下游是
    `fallback`（Stub 拆单器），它会给出一份"看起来像拆单结果"的通用卡；`SplitNeedsHuman`
    的下游是**人**。2026-10-28 那次两张卡 `ARTIFACT_INVALID` 的根因就是通用卡流到了执行器。

    **继承 `SplitUnavailable`** 是刻意的：任何已经写了 `except SplitUnavailable` 的地方
    都会一并接住它（拿到的是 `code` 与消息，不是一条没人认识的异常）。

    `code` 恒为 `NEEDS_HUMAN_CODE`，最后一次失败的具体原因进 `cause_code`（并写进消息）——
    理由见那个常量的注释：事件码要稳定，诊断码要具体，两者不是一件事。
    """

    def __init__(self, code: str, message: str, *, attempts: int, cause_code: str | None = None) -> None:
        super().__init__(code, message)
        self.attempts = attempts
        #: 最后一次契约失败的原因码（`split_extra_field` 之类）。**只用于诊断**，不要拿它判状态。
        self.cause_code = cause_code


#: 一张卡的**契约形状**。`extra="forbid"` 是"多写一个键整单被拒"的结构强制点 ——
#: 与 `CardDraft` 同一条纪律，但这里判的是**模型回话**，不是我们自己的中间结构。
#:
#: 为什么 `format` 用 `Literal` 而不是事后 `in ("css","html")`：前者在构造期就拒，
#: 后者要先构造出一个非法值再判 —— 而"先收下再判"的写法，迟早有人把判定那行删掉。
class SplitCardPayload(StrictModel):
    task_id: str
    title: str
    difficulty: int = 1
    depends_on: list[str] = Field(default_factory=list)
    format: Literal["css", "html"] = "html"
    acceptance: list[str]


#: 顶层形状。**不是数组**：`_json_object` 取的是第一个 `{` 到最后一个 `}`，
#: 顶层数组在它那一步就已经不是"JSON 对象"了；这里再钉一次，让直接调 `to_drafts` 的人
#: 也拿到 `split_bad_json` 而不是一个 `TypeError: unhashable type: 'dict'`。
class SplitPayload(StrictModel):
    cards: list[SplitCardPayload]


#: pydantic 的错误类型 → 既有错误码。**不改码**：`split_extra_field` / `split_criteria_count`
#: 这些已经在判据里被逐字钉住，换一套码等于让留痕与判据各说各话。
def _code_of(error: Mapping[str, Any]) -> str:
    kind = str(error.get("type", ""))
    loc = tuple(error.get("loc") or ())
    if kind == "extra_forbidden":
        return "split_extra_field"
    if not loc or loc[0] != "cards":
        return "split_bad_json"
    if len(loc) == 1:
        # `cards` 缺键 / 不是数组 / 给成对象：旧口径一律是 `split_no_cards`，这里不改码。
        return "split_no_cards" if kind in ("missing", "list_type", "model_type") else "split_bad_json"
    field = str(loc[-1]) if len(loc) >= 2 else ""
    if kind == "model_type" or kind == "model_attributes_type":
        return "split_bad_card"
    if field == "format":
        return "split_bad_format"
    if field == "depends_on":
        return "split_bad_deps"
    if field in ("task_id", "title", "acceptance"):
        return "split_missing_field"
    return "split_bad_card"


class ChatClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


class FallbackSplitter(Protocol):
    def split(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[CardDraft, ...]: ...
    def acceptance_links(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[AcceptanceLink, ...]: ...


SYSTEM = """你是项目拆单器。读客户原话与立项会结论，拆成可执行的任务卡。

只输出一个 JSON 对象，形如：
{"cards":[{"task_id":"T01","title":"...","difficulty":1,"depends_on":[],"format":"css","acceptance":["...","..."]}]}

硬规则：
1. 第一张必须是全站样式表，format="css"；其余卡 format="html"，depends_on 至少含第一张的卡号。
2. 卡号从 T01 连续编号，depends_on 只能引用前面出现过的卡号，不许成环。
3. 每张卡 2-3 条验收标准，必须**可验证**（能拿文件内容或浏览器行为对照），并且用客户原话里的词。
4. 卡标题必须带业务主语（例："手冲咖啡 · 首页（导航 + 主推品）"），不要写"首页"这种通用词。
5. 3-5 张卡；不要输出解释、不要 markdown 代码块、不要多余字段。
6. **产物只有静态文件**：平台收 html / css / js / md / json / py 六种后缀，没有数据库、没有邮件服务、
   没有后端接口。所以验收标准里**不许**出现"存入数据库 / 发邮件 / 服务端校验 / 接口返回"这类做不到的事 ——
   写了它，执行器就只能交一支 .php 去凑，而总线会当场拒收，这张卡永远过不了（2026-10-28 的真实事故）。
   表单类需求写成："前端校验必填项 + 提交后给出成功提示"，不落库、不发信。
   **卡自己的 `format` 仍然只写 `css` 或 `html`**：一张页面卡可以同时交 `index.html` 与 `cart.js`
   （交互写在 .js 里是允许的），但卡的下限按 html 算 —— `format` 说的是"这张卡的下限按哪种产物算"，
   不是"只许交这一种后缀"。"""


#: 拆单这一步的**机器契约**。与 `SYSTEM` 里的示例是同一件事，但这里只写"形状"，不写"怎么拆" ——
#: 两份文本的分工是：`SYSTEM` 管内容（拆什么卡），这一份管格式（回话长什么样）。
#:
#: 为什么 `guards` / `bus_gate` 要**点名禁止**：它们是编排层的概念（守卫、总线闸门），
#: 不是 EN-02 的列。模型见过别的框架的协议，很容易顺手带上，而 `extra="forbid"` 会因此
#: 把**整单**拒掉 —— 一次全对、只因多一个键而全废，是最难查的那种失败。
OUTPUT_CONTRACT = """只输出一个 JSON 对象，顶层只能是对象，**不许是数组**：
{"cards":[{"task_id":"T01","title":"业务主语 · 这一页干什么","difficulty":1,"depends_on":[],"format":"css","acceptance":["…","…"]}]}

- 顶层除 `cards` 外不许有别的键；每张卡只允许六个键：task_id / title / difficulty / depends_on / format / acceptance。
- **不许**出现 `guards`、`bus_gate`、`layer`、`route` 这类编排层字段 —— 多写一个键，整单被拒。
- `format` 只能是 `css` 或 `html`；`task_id` 形如 `T01`；每张卡 2–3 条 `acceptance`；3–6 张卡。
- 不要 markdown 围栏、不要解释文字。"""


def _prd_of(digest: Mapping[str, Sequence[str]]) -> dict[str, Any] | None:
    """从摘要里取回 PRD 结构。取不到就返回 None —— 老单没有这一段，那不是错误。

    为什么在这里宽容：`summary_digest` 是历史列，2026-10-28 之前的单子只有「会议」段。
    拆单读不到 PRD 时退回"只读台词"，并把这个事实交给 `last_error` 之外的留痕去说，
    不在这里编一份空的顶上。
    """
    section = digest.get(PRD_SECTION) or ()
    if not section:
        return None
    try:
        parsed = json.loads(section[0])
    except (json.JSONDecodeError, TypeError, IndexError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _prompt(
    raw_input: str,
    digest: Mapping[str, Sequence[str]],
    *,
    retry_hint: str | None = None,
) -> str:
    # P1-9：客户原话是**不可信输入**。围栏 + 声明必须排在最前面，且标签内只放原话，
    # 不要让模型把"客户说的"读成"系统说的"。SYSTEM 里那句"用客户原话里的词"仍然成立：
    # 它要求复用词面，不要求执行指令。
    lines = [
        UNTRUSTED_INPUT_NOTE,
        CUSTOMER_INPUT_OPEN,
        raw_input.strip(),
        CUSTOMER_INPUT_CLOSE,
        "",
    ]
    # PRD 优先：它是这一步的**契约输入**。台词是"讨论过程"，PRD 是"讨论结论" ——
    # 两处冲突时按 PRD 拆，否则前面那一步等于白跑。
    prd = _prd_of(digest)
    if prd:
        lines.append("产品已定稿的 PRD（按它拆，不要另起一套）：")
        lines.append(f"- 标题：{prd.get('title', '')}")
        for page in prd.get("pages") or ():
            if isinstance(page, dict):
                lines.append(f"- 页面：{page.get('label')}（{page.get('file')}）：{page.get('duty')}")
        for item in prd.get("acceptance") or ():
            lines.append(f"- 验收：{item}")
        for item in prd.get("out_of_scope") or ():
            lines.append(f"- 本期不做：{item}")
        lines.append("")
    said = digest.get("会议") or ()
    if said:
        lines.append("立项会结论：")
        lines.extend(f"- {item}" for item in said)
        lines.append("")
    completed = digest.get("自行补全项") or ()
    if completed:
        lines.append("已自行补全（不要再追问）：")
        lines.extend(f"- {item}" for item in completed)
    if retry_hint:
        # 重试**不是重采样**：把上一次被拒的原因写回去，第二次才有机会改对。
        # 同一份 prompt 再问一遍，模型大概率再错一次 —— 那只是把等待时间翻倍。
        lines.append("")
        lines.append(f"上一次的回话被拒（{retry_hint}）。请严格按输出格式重答，只给那一个 JSON 对象。")
    return "\n".join(lines)


def _json_object(text: str) -> dict[str, Any]:
    """从模型回话里取出那个 JSON 对象。允许它手贱包了 ```json 围栏。"""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.S)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise SplitUnavailable("split_not_json", "回话里没有 JSON 对象")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        raise SplitUnavailable("split_bad_json", "JSON 解析失败") from None
    if not isinstance(parsed, dict):
        raise SplitUnavailable("split_bad_json", "顶层不是对象")
    return parsed


def _cards_of(payload: object) -> list[dict[str, Any]]:
    """契约形状校验：**pydantic 严格模式**，多余字段一律拒（FR-009 的结构强制点）。

    换掉原来那一段手写 `set(card) - allowed` 的理由不是"pydantic 更时髦"，是原来那版有两处漏：
    ① 顶层传数组时 `set(payload)` 抛 `TypeError: unhashable type: 'dict'` —— 一个**未捕获**的
    异常，落进 `split()` 的 `except Exception` 后变成 `split_client_failed:TypeError`，
    排障时看不出是"模型回了数组"；
    ② `difficulty` 写 `"2a"` 时 `int()` 抛 `ValueError`，同样不是 `SplitUnavailable`。
    两处都属"脏数据没被拦住，只是恰好崩了"。现在它们各自有稳定错误码。
    """
    if not isinstance(payload, dict):
        raise SplitUnavailable("split_bad_json", "顶层不是对象（不许是数组）")
    try:
        model = SplitPayload.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        code = _code_of(first)
        where = ".".join(str(part) for part in (first.get("loc") or ()))
        raise SplitUnavailable(code, f"{where}：{first.get('msg', '')}"[:200]) from None
    cards = [card.model_dump() for card in model.cards]
    if not cards:
        raise SplitUnavailable("split_no_cards", "cards 为空")
    if len(cards) > MAX_CARDS:
        raise SplitUnavailable("split_too_many", f"{len(cards)} 张 > {MAX_CARDS}")
    return cards


def to_drafts(
    payload: dict[str, Any],
    *,
    floors: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[CardDraft, ...]:
    """JSON → `CardDraft`，并让既有规则判一遍（成环/引用/形状）。"""
    table = dict(floors) if floors is not None else _artifact_floors()
    drafts: list[CardDraft] = []
    for card in _cards_of(payload):
        try:
            task_id = str(card["task_id"]).strip()
            title = str(card["title"]).strip()
            acceptance = [str(item).strip() for item in card["acceptance"] if str(item).strip()]
        except (KeyError, TypeError) as exc:
            raise SplitUnavailable("split_missing_field", f"缺字段：{exc}") from None
        if not MIN_CRITERIA <= len(acceptance) <= MAX_CRITERIA:
            raise SplitUnavailable("split_criteria_count", f"{task_id} 有 {len(acceptance)} 条判据")
        fmt = str(card.get("format", "html")).strip().lower()
        if fmt not in ("css", "html"):
            raise SplitUnavailable("split_bad_format", f"{task_id} format={fmt!r}")
        deps = card.get("depends_on") or []
        if not isinstance(deps, list):
            raise SplitUnavailable("split_bad_deps", f"{task_id} depends_on 不是数组")
        drafts.append(
            CardDraft(
                task_id=task_id,
                title=title,
                difficulty=int(card.get("difficulty", 1)),
                skill_tags=["frontend_coding"],
                depends_on=[str(item) for item in deps],
                artifact_floor=ArtifactFloor(
                    min_bytes=table[fmt][0], min_lines=table[fmt][1], format=_artifact_format(fmt)
                ),
                acceptance_criteria=acceptance,
            )
        )
    try:
        for draft in drafts:
            validate_draft(draft)
        normalize(list(drafts))
    except (SplitViolation, DependencyViolation) as exc:
        raise SplitUnavailable("split_rules_rejected", str(exc)[:200]) from None
    return tuple(drafts)


def links_of(drafts: Sequence[CardDraft]) -> tuple[AcceptanceLink, ...]:
    """一卡判据 → 一卡映射。`requirement_item` 用卡标题：它是这张卡对需求的那一条。"""
    links: list[AcceptanceLink] = []
    for draft in drafts:
        for criterion in draft.acceptance_criteria:
            links.append(
                AcceptanceLink(
                    task_id=draft.task_id,
                    requirement_item=draft.title,
                    acceptance_item=criterion,
                    produced_by="orchestrator",
                )
            )
    try:
        validate_links(list(drafts), links)
    except Exception as exc:  # noqa: BLE001 - 规则拒就是拆单失败，这里只换成语义码
        raise SplitUnavailable("split_links_rejected", str(exc)[:200]) from None
    return tuple(links)


#: 固定骨架里"非页面卡"的三张：样式表、交互脚本、说明。页面卡按 PRD 的 pages 展开后夹在中间。
#:
#: 为什么是这三张：一份能交付的静态站至少要有 ① 一份全站样式（不然每页各写一套）、
#: ② 一个能跑起来的本地预览（不然拿到目录不知道怎么看）、③ 一份说明（不然接手的人只能读代码）。
#: 它们都不是"模型每单现编"的东西 —— 编出来的形状每单都不一样，门禁也就没法比。
_SHELL_TAIL = (
    # (卡号后缀, 标题后缀, format, 依赖, 判据模板, 难度)
    ("交互脚本与本地预览", "py", ["T02"], ["提供一个本地静态服务器脚本（只依赖标准库）", "运行后首页可访问、静态资源路径正确"], 2),
    ("README 与使用说明", "md", ["T02"], ["说明如何本地预览与目录结构", "列出每个页面文件与其职责"], 1),
)

#: 判据下限（字节 / 行）按格式给。md/json 的行数门槛比代码低 —— 一份 README 十几行就够了，
#: 拿代码那套 512B/12 行去卡它，只会逼模型往说明里灌水。
#:
#: `js` 与 `py` 同档（400 B / 10 行）：两者都是"要读得懂才算写完"的代码，
#: 而不是"有几行样式就算数"的声明式产物。
def _artifact_format(raw: str) -> ArtifactFormat:
    """`str` → `ArtifactFormat`：**先校验再 cast**，不给裸 cast 留口子。

    `ArtifactFloor.format` 是封闭取值域（`refs.py` 的 `ArtifactFormat`），而这两处的来源
    是模型输出的裸字符串。直接 `cast` 等于替模型担保；这里先查一遍域，查不过就是一次
    `split_bad_format`，与 `format=` 那一格既有的判据同一条口径。
    """
    allowed = get_args(ArtifactFormat)
    if raw not in allowed:
        raise SplitUnavailable("split_bad_format", f"产物格式 {raw!r} 不在 {list(allowed)} 里")
    return cast(ArtifactFormat, raw)


def _artifact_floors() -> dict[str, tuple[int, int]]:
    """读一次运行期门槛表。**每次拆单都重新读盘**（`runtime` 无缓存），改配置不用重启。

    这个函数同时是 `api/web.py::_runtime_consumers("artifact_floors")` 的唯一消费者：
    扫描器只认"同一个函数里既调 `load_runtime_params()` 又取那个键"，所以读取点集中在这里，
    不散到每个 `floor_of()` 调用里。
    """
    return dict(runtime_config.load_runtime_params().artifact_floors)


def skeleton_from_prd(
    prd: Mapping[str, Any],
    *,
    floors: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[CardDraft, ...]:
    """PRD → 卡。**确定性投影，不调模型。**

    PRD 已经把"几个页面、每页叫什么、职责是什么、判据有哪些"定死了。再让模型读一遍
    去"拆单"，等于把同一件事做两遍，而且第二遍会漂：2026-10-28 那次拆单回落后，
    模型给出的标题是「首页 / 产品说明页 / 联系页」——通用词，执行器据此做出通用页，
    两张卡直接 `ARTIFACT_INVALID`。

    所以这里只做**投影**：一张样式表 → 每页一张卡 → 交互脚本 → README。
    卡标题 = `PRD 标题 · 页面名`，业务主语由 PRD 保证，不靠提示词提醒。
    """
    title = str(prd.get("title") or "").strip() or "本站"
    pages = [page for page in (prd.get("pages") or ()) if isinstance(page, Mapping)]
    if not pages:
        raise SplitUnavailable("split_prd_no_pages", "PRD 里没有页面清单")

    table = dict(floors) if floors is not None else _artifact_floors()

    def floor_of(fmt: str) -> ArtifactFloor:
        low_bytes, low_lines = table[fmt]
        return ArtifactFloor(min_bytes=low_bytes, min_lines=low_lines, format=_artifact_format(fmt))

    acceptance_all = [str(item) for item in (prd.get("acceptance") or ()) if str(item).strip()]
    out_of_scope = [str(item) for item in (prd.get("out_of_scope") or ()) if str(item).strip()]

    drafts: list[CardDraft] = [
        CardDraft(
            task_id="T01",
            title=f"{title} · 全站样式表（含移动端）",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor=floor_of("css"),
            acceptance_criteria=[
                "样式表用 :root 变量集中管理颜色与字号",
                "含至少一条 @media 断点，375px 下内容不被截断且无横向溢出",
            ],
        )
    ]

    # 页面卡：每页一张，全部依赖样式表。判据的前两句是固定的：
    # ① 这页干什么（PRD 的 duty），② **写到哪个文件**（PRD 的 file）。
    #
    # 第二句是 2026-10-28 真跑补的，也是那次"交付包永远组不出来"的最后一公里：
    # 原来 `file` 只出现在 `else` 分支里，第一页的卡**完全不带文件名** ⇒ 执行器自己起名
    # `kanban.html`，而 PRD 要的是 `dashboard.html` ⇒ E2E 判"不通过"、组包 `entry_absent`。
    # 文件名是这条链上唯一不能漂的锚点：卡说写哪个文件，执行器就写哪个文件。
    for index, page in enumerate(pages, start=1):
        label = str(page.get("label") or f"第{index}页").strip()
        duty = str(page.get("duty") or "").strip()
        file = str(page.get("file") or f"page{index}.html").strip()
        criteria = [
            f"{label}：{duty}" if duty else f"{label} 页面结构与职责一致",
            f"产物文件名必须是 {file}",
        ]
        if index == 1:
            # 全局验收判据只挂第一页，免得同一句话在每张卡上重复（重复的判据会让门禁比对失去意义）。
            criteria.extend(acceptance_all[:2] or ["首屏能看到主推内容与导航"])
        else:
            criteria.append(f"{file} 可通过站内导航到达")
        drafts.append(
            CardDraft(
                task_id=f"T{index + 1:02d}",
                title=f"{title} · {label}",
                difficulty=2,
                skill_tags=["frontend_coding"],
                depends_on=["T01"],
                artifact_floor=floor_of("html"),
                acceptance_criteria=criteria,
            )
        )

    next_id = len(pages) + 2
    for suffix, fmt, deps, criteria, difficulty in _SHELL_TAIL:
        drafts.append(
            CardDraft(
                task_id=f"T{next_id:02d}",
                title=f"{title} · {suffix}",
                difficulty=difficulty,
                skill_tags=["frontend_coding"],
                depends_on=list(deps),
                artifact_floor=floor_of(fmt),
                acceptance_criteria=list(criteria),
            )
        )
        next_id += 1

    # 本期不做要有个去处：挂在说明卡的判据里，否则 PRD 里写了、卡上没人认。
    if out_of_scope:
        drafts[-1] = drafts[-1].model_copy(
            update={"acceptance_criteria": [*drafts[-1].acceptance_criteria[:2], f"说明里写明本期不做：{out_of_scope[0]}"]}
        )

    try:
        for draft in drafts:
            validate_draft(draft)
        normalize(list(drafts))
    except (SplitViolation, DependencyViolation) as exc:
        raise SplitUnavailable("split_skeleton_rejected", str(exc)[:200]) from None
    return tuple(drafts)


class LlmSplitter:
    """真拆单器。`split()` 失败自动回落 `fallback`，`acceptance_links()` 跟着同一条路走。"""

    def __init__(
        self,
        *,
        client: ChatClient,
        raw_input: str,
        fallback: FallbackSplitter | None = None,
        retries: int = 1,
        escalate_on_failure: bool = False,
    ) -> None:
        self._client = client
        self._raw_input = raw_input
        self._fallback = fallback
        self._links: tuple[AcceptanceLink, ...] = ()
        self.last_error: str | None = None
        #: 这一单的卡是怎么来的：`prd` = 按 PRD 投影（不调模型）、`llm` = 模型拆的、
        #: `fallback_stub` = 模型没过契约后回落通用清单。留痕要带它：三种来源的可复现性差一个
        #: 量级，而 `fallback_stub` 还会让 `graph._split` 打一条 `split_fallback` 并在屏上标出来。
        self.last_source: str | None = None
        #: 契约失败后再试几次。默认 1 = 一共问两次（一次重试）。
        self._retries = max(0, int(retries))
        #: 两次都失败时：`False` = 回落 Stub（旧行为），`True` = 抛 `SplitNeedsHuman` 交人工。
        self._escalate = bool(escalate_on_failure)
        #: 岗位提示词取不到时的原因码。**不是静默降级**：基础 SYSTEM 自带完整契约，
        #: 缺岗位稿只是少了那一段，但这件事必须可查 —— 否则"改了 md 没生效"永远查不出来。
        self.role_prompt_error: str | None = None
        self._system = self._compose_system()

    def _compose_system(self) -> str:
        """基础拆单 SYSTEM + 编排岗的「拆单输出约束」。取不到岗位稿就退回基础 SYSTEM。"""
        try:
            return compose_system_prompt(
                SYSTEM,
                "orchestrator",
                section="拆单输出约束",
                output_contract=OUTPUT_CONTRACT,
            )
        except PromptNotFoundError as exc:
            self.role_prompt_error = exc.reason
            return SYSTEM

    def split(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[CardDraft, ...]:
        # 门槛表每单读一次：改 `runtime.yaml` 不必重启（`runtime` 无缓存）。
        floors = _artifact_floors()
        # PRD 在就先按它投影：这一步是**确定性的**，同一个 PRD 永远拆出同一组卡。
        prd = _prd_of(summary_digest)
        if prd:
            try:
                drafts = skeleton_from_prd(prd, floors=floors)
            except SplitUnavailable as exc:
                # PRD 残缺（比如没有页面清单）不直接掉到 Stub —— 先试模型那条路，
                # 让"文档写得不全"与"模型也拆不动"在留痕里是两个码。
                self.last_error = exc.code
            else:
                self._links = links_of(drafts)
                self.last_error = None
                self.last_source = "prd"
                return drafts

        attempts = 1 + self._retries
        last: SplitUnavailable | None = None
        for _ in range(attempts):
            try:
                reply = self._client.chat(
                    self._system,
                    _prompt(self._raw_input, summary_digest, retry_hint=last.code if last else None),
                )
                if getattr(self._client, "last_finish_reason", None) == "length":
                    raise SplitUnavailable("split_truncated", "模型回话被 max_tokens 截断")
                drafts = to_drafts(_json_object(reply), floors=floors)
            except SplitUnavailable as exc:
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001 - 客户端异常形状不归这一层猜
                last = SplitUnavailable(f"split_client_failed:{type(exc).__name__}", str(exc)[:120])
                continue
            self._links = links_of(drafts)
            self.last_source = "llm"
            self.last_error = None
            return drafts

        assert last is not None  # attempts >= 1，循环体每条路要么 return 要么赋值 last
        self.last_error = last.code
        if self._escalate:
            # 不回落：Stub 会给出一份通用卡，那正是 2026-10-28 两张卡 `ARTIFACT_INVALID` 的起点。
            # 抛的是**固定码**，不是 `last.code`：读面靠它判"这一单在等人"，
            # 而 `last.code` 一次一换（见 `NEEDS_HUMAN_CODE` 的注释）。诊断码仍进消息与 `cause_code`。
            raise SplitNeedsHuman(
                NEEDS_HUMAN_CODE,
                f"拆单 {attempts} 次都过不了契约（最后一次 {last.code}：{last}）",
                attempts=attempts,
                cause_code=last.code,
            )
        return self._fall_back(summary_digest)

    def _fall_back(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[CardDraft, ...]:
        if self._fallback is None:
            raise SplitUnavailable(self.last_error or "split_failed", "没有回落拆单器")
        self._links = ()
        # 来源必须自报（`graph._split` 靠它决定要不要打 `split_fallback` 留痕）：
        # 只写 `last_error` 的话，"回落了"这件事永远只在日志里，屏上和库里都读不出来。
        self.last_source = "fallback_stub"
        return self._fallback.split(summary_digest)

    def acceptance_links(self, summary_digest: Mapping[str, Sequence[str]]) -> tuple[AcceptanceLink, ...]:
        if self._links:
            return self._links
        if self._fallback is not None:
            return self._fallback.acceptance_links(summary_digest)
        return ()
