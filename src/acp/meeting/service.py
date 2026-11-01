"""四岗位会议：让总管 / 产品 / 技术 / 设计**真的各调一次模型**，把需求原文过一遍。

为什么要有这一层：网页受理此前的"会议桌"是前端脚本按需求正文派生的台词 —— 看着像讨论，
实际上模型一次都没被调用过。owner 2026-10-27 选的就是把这一步做实：先真开会，再谈拆单。

四条口径：

1. **模型是注入的**。`MeetingRunner` 只认一个 `chat(system, user) -> str`，真实现是
   `DoubaoChatClient`（火山方舟 OpenAI 兼容端点）。判据喂假客户端，一次网络都不发 ——
   与 `adapters` 层的"注入 httpx.Client"同一条纪律。
2. **角色是封闭四个**：`ROLE_SPECS` 就是全部，顺序 = 发言顺序。后端注册表里没有"产品经理"
   这种身份，所以这里的 role id（`pm`/`cto`/`designer`）是**会议角色**，不是 EN-03 的 Agent id ——
   两者不混用，名字也不同形。
3. **台词有长度上限**。一屏会议桌放不下四段长文，`clip()` 按字符截断并加省略号；
   上限是这一个模块的常量，不散到前端。
4. **落库借用 `summary_digest` 的「会议」段**（JSONB，已有列、已有读面）。不新开表、不加迁移：
   这一段的形状就是"一串 `role｜text`"，与那份 JSON 的"分区 → 行"结构同形。

会议失败**不挡拆单**：调用方捕获 `MeetingUnavailable`，留一句原因继续走。
把"模型没连上"表现成"需求提交失败"是另一种假话。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from acp.prompts import PromptNotFoundError, compose_system_prompt

import httpx

from acp.adapters.doubao.transport import API_KEY_ENV, ARK_BASE_URL, CHAT_COMPLETIONS_PATH, HTTP_TIMEOUT_S
from acp.config.loader import env_value

#: `summary_digest` 里会议那一段的键名。前端按它取台词，后端按它写。
MEETING_SECTION = "会议"

#: 一行台词里角色与正文的分隔符。用全角竖线：正文里出现半角 `|` 是常事（表格、命令行）。
LINE_SEP = "｜"

#: 单句上限（字符）。四段加起来仍能在一屏会议桌里读完。
MAX_TURNS_CHARS = 140

#: 会议模型 ID 的环境变量名（与执行链同一个变量，口径见 `adapters/spec_factory.py` 模块头）。
MODEL_ENV = "ACP_DOUBAO_MODEL"

#: 讨论轮数（owner 2026-10-28 第 1 条：「怎么让四个岗位多讨论几轮再出合理的方案」）。
#:
#: 一轮 = 四个岗位各说一次。第二轮起**换一种问法**：每人必须先点名上一轮里别人对自己方案的
#: 那一句质疑或补充，再回答它（改了什么 / 坚持什么+理由），最后由总管收敛拍板。
#: 光把同样四句话再问一遍没有意义 —— 那是四份重复的稿子，不是讨论。
#:
#: 代价是模型调用次数乘以轮数（1 轮 ≈ 1–3 分钟，2 轮 ≈ 2–5 分钟），所以默认 2 而不是 3。
ROUNDS_ENV = "ACP_MEETING_ROUNDS"
DEFAULT_ROUNDS = 2
#: 允许的轮数区间。上限 3：再多的话一场会就要 7 分钟以上，而屏上的观察窗只有 5 分钟。
ROUNDS_RANGE = (1, 3)

#: `summary_digest` 里"这一场开了几轮"的那一格。前端靠它算"第 N / M 轮"与"四轮齐了没有"，
#: 写死 4 会让第二轮刚开始就被判成"会议结束"。
ROUND_SECTION = "会议轮数"


class RoundsInvalid(ValueError):
    """`ACP_MEETING_ROUNDS` 不是合法轮数。**启动期拒，不降级** ——
    一个手滑写成 `0` 或 `many` 的环境变量，静默变成"默认 2 轮"的话，
    现场只会觉得"怎么又是两轮"，而配置看上去是对的。"""

    def __init__(self, raw: str) -> None:
        low, high = ROUNDS_RANGE
        super().__init__(f"{ROUNDS_ENV}={raw!r} 不是合法轮数：只接 {low}–{high} 的整数")
        self.raw = raw


def resolve_rounds(raw: str | None) -> int:
    """环境变量 → 轮数。空/未设 ⇒ `DEFAULT_ROUNDS`；非整数或越界 ⇒ `RoundsInvalid`。"""
    if raw is None or not str(raw).strip():
        return DEFAULT_ROUNDS
    text = str(raw).strip()
    low, high = ROUNDS_RANGE
    if not text.isdigit() or not low <= int(text) <= high:
        raise RoundsInvalid(text)
    return int(text)


#: 第二轮起的追问：必须回应别人的话，不许重新自我介绍。
REBUTTAL_SUFFIX = (
    "\n\n这是第 {n} 轮（共 {total} 轮）复审。先点名上一轮里**别人对你方案的那一句质疑或补充**"
    "（说清是谁说的哪一点），再回答它：改了什么，或者坚持什么并给出理由。"
    "不复述已经说过的内容，不重新自我介绍，不超过 80 字。"
)

#: 第二轮起总管**最后一个说**，做收敛 —— 让它先说完再听，就没有"拍板"可言。
FINAL_SUFFIX = (
    "\n\n这是第 {n} 轮（共 {total} 轮）的收敛，你最后一个说。"
    "一句话点清这一轮改掉了哪几条、还有哪一条没谈拢（没有就说没有），然后拍板。"
    "不超过 90 字，不复述方案，不写代码。"
)


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    title: str
    system: str


#: 每个岗位在 `roles/<id>.md` 里的哪一节是"这一轮该说的话"。
#:
#: **只取那一节**，不塞整份：产品经理的岗位稿里还有 `PRD 结构` 那一段，整份塞进去，
#: 会议里就会冒出一个 JSON，而会议层的契约是"只说话，不输出 JSON"（见本模块头第 1 条）。
MEETING_TURN_SECTION: dict[str, str] = {
    "orchestrator": "一、立项会第一轮主持词",
    "pm": "一、立项会台词",
    "cto": "一、立项会第三轮台词",
    "designer": "一、立项会第四轮台词",
}

#: 每轮的输出契约：只有一段人话，且长度上限与 `ROLE_SPECS` 里的要求一致。
#: 这里再写一遍不是重复 —— `ROLE_SPECS` 那段是"人设"，这一段是"形状"，模型对后者的遵守度更高。
MEETING_CONTRACT: dict[str, str] = {
    "orchestrator": "只输出一段主持词，不超过 80 字。不要 JSON、不要分点、不要角色名前缀。",
    "pm": "只输出一段台词，不超过 120 字。不要 JSON、不要角色名前缀。",
    "cto": "只输出一段台词，不超过 110 字，先点名风险再给选型。不要 JSON、不要角色名前缀。",
    "designer": "只输出一段台词，不超过 100 字，先接住选型约束再给颜色与版式。不要 JSON、不要角色名前缀。",
}


def system_of(spec: RoleSpec) -> str:
    """一轮发言的 system。取不到岗位稿就退回 `spec.system`（它本身是自足的）。

    **退回不是静默的**：`meeting_role_prompt_errors` 会记下原因码；它目前只有进程内读面
    （没有 `/health` 之类的 HTTP 端点），排障时直接读这个字典，不要假设有一个健康页。
    不抛异常的理由：岗位稿是**加强**，不是前提；为了一个附加说明让整场会开不起来，
    是把可选优化做成了单点故障。
    """
    try:
        return compose_system_prompt(
            spec.system,
            spec.role_id,
            section=MEETING_TURN_SECTION.get(spec.role_id, ""),
            output_contract=MEETING_CONTRACT.get(spec.role_id, "只输出一段台词。"),
        )
    except PromptNotFoundError as exc:
        meeting_role_prompt_errors[spec.role_id] = exc.reason
        return spec.system


#: 岗位稿取不到时的原因码（键 = 角色 id）。空 dict = 四个岗位都读到了。
meeting_role_prompt_errors: dict[str, str] = {}


#: 四个岗位的封闭集合，顺序 = 发言顺序。system 里刻意**不写**"你是 AI"，也不要求 JSON：
#: 这一层要的是人话台词，结构化留给拆单那一步。
#:
#: **每一条都要求"接住前一个人"**（2026-10-28）。改之前四条 system 各自独立，模型就各说各的，
#: 四条并排读下来像四份并行交付，不像一场会 —— 现场原话是"只会讨论一轮"。
#: 真正的差别不在轮次（一次 run 就是四轮，`ROLE_SPECS` 循环一次），而在**有没有交锋**：
#: 技术总监必须先点名产品方案里的一处风险，设计师必须先接住选型对视觉的约束。
#: 这样同样是四次调用、同样四句话，读起来才是"讨论"。
#: 为什么不加第二轮：那要多 1–3 次模型调用（会议本来已 1–3 分钟），
#: 而"有没有人反驳"这件事在 prompt 里就能拿到，不必用时间换。
ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        "orchestrator",
        "工作室总管",
        "你主持一场项目立项会。先复述客户原话里最关键的一句，再点名这一轮要谁回答什么，"
        "并说明这一轮只收敛哪几件事。只说话，不写代码，不列模板。不超过 80 字，不用敬语。",
    ),
    RoleSpec(
        "pm",
        "产品经理",
        "你是产品经理。基于客户原话给出：几个页面、每页职责、可验收的判据（要能量）。"
        "不要复述客户原话。客户原话里有一处模糊时，你必须选定一种解释并说清它的代价"
        "（不要问回去，会议要的是能拆单的结论）。不超过 120 字。",
    ),
    RoleSpec(
        "cto",
        "技术总监",
        "你是技术总监。**先点名产品经理方案里的一处真实风险**（说清是哪一页或哪一条判据），"
        "再给选型与理由（一句话）。如果你同意他的方案，也要指出它最脆的那一环。"
        "不写代码。不超过 110 字。",
    ),
    RoleSpec(
        "designer",
        "UI 设计师",
        "你是 UI 设计师。**先接住技术选型对视觉的一条约束**（比如渲染方式限制了动效或字体加载），"
        "再给主色方向、字号档位、一个具体版式决定。不超过 100 字。",
    ),
)


@dataclass(frozen=True)
class Turn:
    role_id: str
    title: str
    text: str


class MeetingUnavailable(RuntimeError):
    """会议跑不起来（缺凭据 / 模型拒绝 / 网络断）。`code` 是给留痕看的归因词，不带凭据值。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: 一轮说完之后的回调。签名 `(turn, said) -> None`，`said` 是**含这一轮在内**的累计台词。
#:
#: 为什么需要它：`run_meeting` 是四轮**串行**的（第 N 轮要读第 1..N-1 轮说过的话），
#: 而四轮加起来实测 1–3 分钟。原先只有"四轮全跑完"这一个交付点，于是那三分钟里
#: `GET /requests/{id}` 的「会议」段永远是空的 —— 网页上四个岗位一句话都看不见。
#: 回调让调用方**每轮落一次库**，屏上就是"总管说完 → 产品正在说 → 技术正在说"。
#:
#: 回调抛异常**不中止会议**：落库是顺手做的事，不是开会的前提（见 `run_meeting` 里的 try）。
OnTurn = Callable[["Turn", "list[Turn]"], None]


class MeetingRunner(Protocol):
    """一次会议 = 一次 `run(raw_input)`。实现只负责"说人话"，不做拆单。

    `on_turn` 是可选增量交付点：给了就每轮回调一次，不给就只在末尾返回整段。
    """

    def run(self, raw_input: str, *, on_turn: OnTurn | None = None) -> list[Turn]: ...


class ChatClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


def clip(text: str, limit: int = MAX_TURNS_CHARS) -> str:
    """按**字符**截断（不是字节）：中文一屏的字数才是可读性单位。"""
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _blames_thinking(response: "httpx.Response") -> bool:
    """这个 400 是不是"不认 `thinking` 字段"。

    只在**错误体里出现 `thinking` 这个词**时才认：把任何 400 都当成"字段不支持"去重试，
    会在"内容被拒"时白白多发一次同样的请求，而真正的拒因被第二次的结果盖住。
    错误体本身不落进异常消息（它可能回显 prompt），这里只是临时读一眼。
    """
    try:
        return "thinking" in response.text.lower()
    except Exception:  # noqa: BLE001 - 读不出正文就当不是这个原因
        return False


def round_system(spec: RoleSpec, round_no: int, rounds: int) -> str:
    """第 `round_no` 轮这一位该拿到的 system。

    第一轮走 `system_of()`（岗位稿 + 这一轮该说的那一段）；第二轮起在原 system **后面追加**一段
    复审要求，而不是换一份岗位稿 —— 岗位稿讲的是"你是谁"，复审讲的是"这一轮你要回答谁"，
    两件事叠在一起才对：换掉岗位稿等于让第二轮的人忘了自己是谁。
    """
    base = system_of(spec)
    if round_no <= 1:
        return base
    suffix = FINAL_SUFFIX if spec.role_id == "orchestrator" else REBUTTAL_SUFFIX
    return base + suffix.format(n=round_no, total=rounds)


def speaking_order(round_no: int) -> tuple[RoleSpec, ...]:
    """这一轮的发言顺序。第一轮总管开场；第二轮起总管**挪到最后**做收敛。

    只换顺序、不换人：第二轮如果总管还第一个说，它就没有别人的新话可收敛，
    所谓"拍板"只是把第一轮的主持词再念一遍。
    """
    if round_no <= 1:
        return ROLE_SPECS
    return ROLE_SPECS[1:] + ROLE_SPECS[:1]


def _prompt(raw_input: str, said: list[Turn], *, round_no: int = 1, rounds: int = 1) -> str:
    """把"客户原话 + 前面已经说过的话"拼成这一轮的 user 消息。

    显式标注谁说过什么，而不是让模型自己从一段文本里猜 —— 猜错的形状是"产品经理替技术选型"，
    而那正是四人会议最该避免的越位。
    """
    lines = [f"客户原话：{raw_input.strip()}", "", "已经说过的话："]
    if said:
        lines.extend(f"- {turn.title}：{turn.text}" for turn in said)
    else:
        lines.append("-（这是第一轮）")
    lines.append("")
    if round_no > 1:
        lines.append(f"现在轮到你，第 {round_no} 轮（共 {rounds} 轮）。只说你要说的那一段，不要重复别人说过的内容，不要加角色名前缀。")
    else:
        lines.append("现在轮到你。只说你要说的那一段，不要重复别人说过的内容，不要加角色名前缀。")
    return "\n".join(lines)


def run_meeting(
    raw_input: str,
    *,
    client: ChatClient,
    limit: int = MAX_TURNS_CHARS,
    on_turn: OnTurn | None = None,
    rounds: int = DEFAULT_ROUNDS,
) -> list[Turn]:
    """四个岗位依次发言，`rounds` 轮。任何一轮失败都抛 `MeetingUnavailable`（不做"部分成功"）。

    `on_turn` 每轮成功后就回调一次 —— 这是网页"逐岗发言"的唯一来源。
    回调里的异常**吞掉**：那一轮台词已经真的生成出来了，因为"写不进库"就把它丢掉，
    等于让一次数据库抖动抹掉模型刚说的话。留痕由回调自己负责（它知道自己在写哪一行）。

    `rounds` 是 owner 2026-10-28 第 1 条要的"多讨论几轮"。第一轮是各说一遍，
    第二轮起每人必须先回应别人对自己方案的那一句（见 `REBUTTAL_SUFFIX`），总管最后收敛。
    `said` 是**累计**的，所以第二轮的人看得到第一轮全部四段 —— 不需要另传上下文。
    """
    low, high = ROUNDS_RANGE
    if not low <= rounds <= high:
        raise RoundsInvalid(str(rounds))
    said: list[Turn] = []
    for round_no in range(1, rounds + 1):
        for spec in speaking_order(round_no):
            try:
                reply = client.chat(round_system(spec, round_no, rounds), _prompt(raw_input, said, round_no=round_no, rounds=rounds))
            except MeetingUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - 第三方客户端的异常形状不归这一层猜
                raise MeetingUnavailable("meeting_call_failed", f"{spec.role_id} 发言失败：{type(exc).__name__}") from None
            text = clip(reply, limit)
            if not text:
                raise MeetingUnavailable("meeting_empty_reply", f"{spec.role_id} 回了空内容")
            said.append(Turn(role_id=spec.role_id, title=spec.title, text=text))
            if on_turn is not None:
                try:
                    on_turn(said[-1], list(said))
                except Exception:  # noqa: BLE001 - 见 docstring 第 2 段
                    pass
    return said


def digest_lines(turns: list[Turn]) -> list[str]:
    """`Turn[]` → 可存进 `summary_digest` 的字符串行。"""
    return [f"{turn.role_id}{LINE_SEP}{turn.text}" for turn in turns]


def parse_lines(lines: list[str]) -> list[Turn]:
    """反向解析。没有分隔符的行整段当正文、角色记 `unknown` —— 不丢字，也不假装知道是谁说的。"""
    titles = {spec.role_id: spec.title for spec in ROLE_SPECS}
    turns: list[Turn] = []
    for line in lines:
        role_id, sep, text = str(line).partition(LINE_SEP)
        if not sep:
            role_id, text = "unknown", str(line)
        turns.append(Turn(role_id=role_id, title=titles.get(role_id, role_id), text=text))
    return turns


def merge_into_digest(
    digest: Mapping[str, list[str]] | None,
    turns: list[Turn],
    *,
    rounds: int | None = None,
) -> dict[str, list[str]]:
    """把会议段并进已有摘要。**不改入参**（它是从 ORM 读出来的对象，就地改会让"写没写"难判断）。

    `rounds` 给了就一并写「会议轮数」那一格：前端靠它算"四轮齐了没有"，
    写死 4 会让第二轮刚开始就被判成"会议结束"（见 `ROUND_SECTION`）。
    """
    merged = {str(key): [str(item) for item in value] for key, value in (digest or {}).items()}
    merged[MEETING_SECTION] = digest_lines(turns)
    if rounds is not None:
        merged[ROUND_SECTION] = [str(rounds)]
    return merged


class DoubaoChatClient:
    """火山方舟 OpenAI 兼容端点。凭据只从环境来（构造时取值，请求时使用）。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = ARK_BASE_URL,
        timeout: float = HTTP_TIMEOUT_S,
        http: httpx.Client | None = None,
        max_tokens: int = 1200,
        thinking: bool = True,
    ) -> None:
        if not api_key.strip():
            raise MeetingUnavailable("meeting_credential_missing", f"{API_KEY_ENV} 为空")
        if not model.strip():
            raise MeetingUnavailable("meeting_model_missing", f"{MODEL_ENV} 为空")
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
        self._timeout = timeout
        self._http = http
        self._max_tokens = max_tokens
        #: 允不允许模型自己"先想再说"。**默认 True = 不干预**，因为这一层不认识所有端点；
        #: `thinking=False` 由调用方按"这一格要不要想"决定，那时请求里带
        #: `thinking: {"type": "disabled"}`。会议与文档两条链传 False，理由见 `serve._llm_callables`。
        #: 端点不认这个字段时置回 True（见 `chat` 里那段 400 兜底）—— 关不掉就只好让它想。
        self._allow_thinking = thinking
        #: 上一次调用的收尾原因。`length` = 被上限截断 —— 拆单那条链靠它区分"模型不会写 JSON"
        #: 和"我们给的额度不够"，两者混在一起就只会看到 split_bad_json。
        self.last_finish_reason: str | None = None

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.5,
            "max_tokens": self._max_tokens,
        }
        if not self._allow_thinking:
            payload["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        response = self._send(payload, headers)
        if response.status_code == 400 and "thinking" in payload and _blames_thinking(response):
            # 这个端点不认 `thinking` 字段。去掉重试一次，并**记住** —— 每轮都撞一次 400
            # 会让一场会多出四次无谓的往返，而错误码长得像"内容被拒"，读日志的人会查错方向。
            self._allow_thinking = True
            payload.pop("thinking", None)
            response = self._send(payload, headers)
        if response.status_code >= 400:
            # 厂商错误体可能回显请求内容，这里只留状态码与它的错误码字段，不落正文。
            code = "unknown"
            try:
                body = response.json()
                code = str((body.get("error") or {}).get("code") or body.get("code") or code)
            except (ValueError, AttributeError):
                pass
            raise MeetingUnavailable("meeting_http_error", f"HTTP {response.status_code} · {code}")
        try:
            body = response.json()
            self.last_finish_reason = str(body["choices"][0].get("finish_reason") or "")
            return str(body["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError, ValueError):
            raise MeetingUnavailable("meeting_bad_response", "响应里没有 choices[0].message.content") from None

    def _send(self, payload: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        try:
            if self._http is not None:
                return self._http.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            with httpx.Client(timeout=self._timeout) as client:
                return client.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException:
            # 这句原来写的是"模型 {max_tokens} tokens 上限内没有返回" —— 把**超时**说成了额度不足。
            # 2026-10-28 因此查错方向：文档节点其实是被 120 秒读超时掐掉的，不是 1200 tokens 截断。
            raise MeetingUnavailable("meeting_timeout", f"模型在 {self._timeout:.0f} 秒内没有返回") from None
        except httpx.TransportError as exc:
            # 只报类名：httpx 的连接错误消息里可能带完整 URL。
            raise MeetingUnavailable("meeting_transport", f"{type(exc).__name__}") from None


def default_client(
    env: Mapping[str, str] | None = None,
    *,
    max_tokens: int = 1200,
    timeout: float = HTTP_TIMEOUT_S,
    thinking: bool = True,
) -> ChatClient:
    """按环境造真客户端。缺凭据 ⇒ 抛 `MeetingUnavailable`（消息里只有变量名）。

    三条链各造一个实例，凭据与模型都来自同一组环境变量 —— 分开的理由是**额度、超时与
    思考开关是每一格的事**，不是全站一个数：

    · 会议：`thinking=False`。四段"不超过 140 字"的人话不需要思维链，见下。
    · 文档：`thinking=False` + 2400 tokens + 300 秒。三份 JSON 比一段台词长得多，
      1200 tokens / 120 秒对它不够（2026-10-28 实测：那次调用正好卡在 120 秒上整份丢掉）。
    · 拆单：保留默认（不干预）。它要吐结构化 JSON，且实测本来就只花 1–2 秒。

    **`thinking=False` 的来历（2026-10-28 实测，`doubao-seed-2-1-pro`）**：
    同一个 prompt，开思考 63.8 秒 / 2573 个 reasoning token，关思考 **2.8 秒 / 0 个**，
    输出都是 65 字左右；而且关掉之后**更贴 system prompt** —— 开着思考那次总管把活派给了
    "前端开发"，而会议里根本没有这个角色。四轮串行 ⇒ 这一项就是 133 秒与 12 秒的差别。
    """
    source = os.environ if env is None else env
    try:
        api_key = env_value(API_KEY_ENV, source)
    # 宽捕的理由：`env_value()` 的失败只有"这个变量没配"一种，抛的是 `CredentialMissing`
    # （`Exception` 的子类）。这里接住它只是为了把变量名写进会议自己的那条读数，
    # 不是"不知道会出什么错" —— 只捕 `CredentialMissing` 会让环境面将来换了异常类型时
    # 静默漏过去，而这一格要的恰恰是"缺的是哪个变量"。
    except Exception as exc:  # noqa: BLE001 - 只捕 env_value 的缺变量分支，且立刻原样重抛
        raise MeetingUnavailable("meeting_credential_missing", f"{API_KEY_ENV} 未注入") from exc
    model = (source.get(MODEL_ENV) or "").strip()
    if not model:
        raise MeetingUnavailable("meeting_model_missing", f"{MODEL_ENV} 未注入")
    return DoubaoChatClient(
        api_key=api_key, model=model, max_tokens=max_tokens, timeout=timeout, thinking=thinking
    )


def default_runner(env: Mapping[str, str] | None = None) -> MeetingRunner:
    """按环境造真跑者。缺凭据 ⇒ 抛 `MeetingUnavailable`，由调用方决定是否降级。"""

    return _Runner(default_client(env))


class _Runner:
    """把 `ChatClient` 适配成 `MeetingRunner`。单独一层是为了让测试只替 `chat`。"""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def run(self, raw_input: str, *, on_turn: OnTurn | None = None) -> list[Turn]:
        return run_meeting(raw_input, client=self._client, on_turn=on_turn)


def lines_json(turns: list[Turn]) -> str:
    """排障用：把台词折成一行 JSON（不含任何凭据）。"""
    return json.dumps([{"role": turn.role_id, "text": turn.text} for turn in turns], ensure_ascii=False)
