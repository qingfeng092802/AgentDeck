"""四岗位会议的判据：**不发一次网络**。

三件事必须钉住：
1. 角色是封闭四个、顺序固定 —— 会议桌的顺序不是"碰巧"，是产品决定；
2. 每一轮的 prompt 里带上前面说过的话 —— 否则四个人会各说各的，那不叫开会；
3. 失败只有一种形状（`MeetingUnavailable` + 归因码），不把厂商错误体漏进来。
"""

from __future__ import annotations

import json

import httpx
import pytest

from acp.meeting import service as meeting
from acp.meeting.service import (
    DoubaoChatClient,
    MeetingUnavailable,
    ROLE_SPECS,
    digest_lines,
    merge_into_digest,
    parse_lines,
    run_meeting,
)


class FakeChat:
    """按顺序回话，并把手里的 prompt 存下来供断言。"""

    def __init__(self, replies: list[str] | None = None, *, boom: Exception | None = None) -> None:
        self.replies = list(replies or [f"第{i + 1}位的意见" for i in range(4)])
        self.prompts: list[tuple[str, str]] = []
        self.boom = boom

    def chat(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if self.boom is not None:
            raise self.boom
        return self.replies[len(self.prompts) - 1]


def test_the_meeting_has_exactly_four_roles_in_a_fixed_order() -> None:
    assert [spec.role_id for spec in ROLE_SPECS] == ["orchestrator", "pm", "cto", "designer"]


def test_each_turn_sees_what_was_said_before() -> None:
    client = FakeChat(["先定验收口径", "三页：首页/商品/联系", "原生三件套，不引框架", "深棕配米白"])
    turns = run_meeting("做一个手冲咖啡官网", client=client, rounds=1)

    assert [turn.text for turn in turns] == ["先定验收口径", "三页：首页/商品/联系", "原生三件套，不引框架", "深棕配米白"]
    # 第一轮没有前人发言；第二轮的 prompt 里必须有第一轮的原话
    assert "这是第一轮" in client.prompts[0][1]
    assert "先定验收口径" in client.prompts[1][1]
    assert "三页：首页/商品/联系" in client.prompts[2][1]
    # 客户原话每一轮都在，不靠上下文窗口记
    assert all("做一个手冲咖啡官网" in prompt for _, prompt in client.prompts)


def test_each_role_gets_its_own_system_prompt() -> None:
    client = FakeChat()
    run_meeting("做一个官网", client=client, rounds=1)
    systems = [system for system, _ in client.prompts]
    assert len(set(systems)) == 4, "四个岗位不能共用同一段 system，否则只是同一张嘴说四遍"


def test_long_reply_is_clipped_and_marked() -> None:
    client = FakeChat(["长" * 400] * 4)
    turns = run_meeting("x", client=client, limit=20, rounds=1)
    assert all(len(turn.text) == 20 for turn in turns)
    assert all(turn.text.endswith("…") for turn in turns)


def test_empty_reply_is_a_failure_not_an_empty_line() -> None:
    client = FakeChat(["", "b", "c", "d"])
    with pytest.raises(MeetingUnavailable) as caught:
        run_meeting("x", client=client, rounds=1)
    assert caught.value.code == "meeting_empty_reply"


def test_a_client_exception_becomes_one_code_without_the_original_message() -> None:
    client = FakeChat(boom=RuntimeError("sk-secret-in-error"))
    with pytest.raises(MeetingUnavailable) as caught:
        run_meeting("x", client=client, rounds=1)
    assert caught.value.code == "meeting_call_failed"
    assert "sk-secret" not in str(caught.value)


def test_digest_lines_round_trip_and_keep_unknown_speakers() -> None:
    turns = run_meeting("x", client=FakeChat(), rounds=1)
    lines = digest_lines(turns)
    assert lines[0].startswith("orchestrator｜")
    again = parse_lines(lines)
    assert [turn.role_id for turn in again] == [spec.role_id for spec in ROLE_SPECS]
    assert [turn.text for turn in again] == [turn.text for turn in turns]

    stray = parse_lines(["没有分隔符的一句"])
    assert stray[0].role_id == "unknown"
    assert stray[0].text == "没有分隔符的一句"


def test_merging_keeps_the_other_sections_and_does_not_mutate_the_input() -> None:
    original = {"自行补全项": ["页面数量取 3 个"], "澄清问题": ["联系方式？"]}
    merged = merge_into_digest(original, run_meeting("x", client=FakeChat(), rounds=1))
    assert original == {"自行补全项": ["页面数量取 3 个"], "澄清问题": ["联系方式？"]}
    assert merged["自行补全项"] == ["页面数量取 3 个"]
    assert len(merged[meeting.MEETING_SECTION]) == 4


# ---------------------------------------------------------------- 逐轮交付（2026-10-28）


def test_every_turn_is_delivered_before_the_next_one_is_asked() -> None:
    """`on_turn` 必须在**下一轮 chat 之前**触发 —— 网页靠它做"逐岗发言"。

    这条钉的是时序，不是次数：四次回调在四轮 chat 之后补发，屏上仍然是"一次性四段"，
    而那正是要修掉的那个症状。所以回调里读 `client.prompts`，它此刻必须只问过 N 次。
    """
    client = FakeChat()
    seen: list[tuple[str, int]] = []

    def land(turn, said):
        seen.append((turn.role_id, len(client.prompts)))

    run_meeting("做一个官网", client=client, on_turn=land, rounds=1)

    assert [role for role, _ in seen] == [spec.role_id for spec in ROLE_SPECS]
    assert [asked for _, asked in seen] == [1, 2, 3, 4], "第 N 轮回调时，模型应该刚好被问过 N 次"


def test_the_callback_gets_the_cumulative_lines_so_far() -> None:
    """回调拿到的是**累计**台词（含本轮），因为落库那一步就是按累计段整段覆盖写的。"""
    lengths: list[int] = []
    run_meeting("x", client=FakeChat(), on_turn=lambda turn, said: lengths.append(len(said)), rounds=1)
    assert lengths == [1, 2, 3, 4]


def test_a_failing_callback_does_not_lose_the_meeting() -> None:
    """落库写不进去是数据库的事，不该把模型已经说出来的四段话一起判死。"""
    def land(turn, said):
        raise RuntimeError("db down")

    turns = run_meeting("x", client=FakeChat(), on_turn=land, rounds=1)
    assert len(turns) == 4


# ---------------------------------------------------------------- 多轮（2026-10-28 owner 第 1 条）


def test_the_default_is_two_rounds_and_the_env_can_change_it() -> None:
    """一场会开几轮由 `ACP_MEETING_ROUNDS` 定，默认 2。

    为什么默认不是 1：一轮是"四个人各说一遍"，没有人回应过别人 —— 那叫汇报，不叫讨论。
    为什么上限是 3：每多一轮就是 4 次串行调用（实测 1–3 分钟），3 轮已经接近一次受理能等的边。
    """
    assert meeting.DEFAULT_ROUNDS == 2
    assert meeting.ROUNDS_RANGE == (1, 3)
    assert meeting.resolve_rounds(None) == 2
    assert meeting.resolve_rounds("") == 2
    assert meeting.resolve_rounds("  ") == 2
    assert meeting.resolve_rounds("1") == 1
    assert meeting.resolve_rounds("3") == 3


def test_an_illegal_round_count_is_refused_at_startup_not_silently_defaulted() -> None:
    """写错的轮数**不许**静默退回默认值。

    退回默认值的表现是"怎么配都是两轮"，而配置看上去完全正确 —— 排查会从配置文件开始，
    从那里找不到任何东西。拒在启动期，那一行就在启动日志里。
    """
    for bad in ("0", "4", "9", "-1", "two", "2.0", " 2轮 "):
        with pytest.raises(meeting.RoundsInvalid):
            meeting.resolve_rounds(bad)


def test_two_rounds_are_eight_turns_and_the_orchestrator_speaks_last_in_round_two() -> None:
    """第二轮**换序**：总管挪到最后做收敛。

    如果第二轮总管还第一个说，它手里没有别人的新话可收敛，所谓"拍板"只是把主持词再念一遍 ——
    八次调用、八段话，读起来仍然是四段。
    """
    client = FakeChat([f"第{i + 1}句" for i in range(8)])
    turns = run_meeting("做一个手冲咖啡官网", client=client, rounds=2)

    assert len(turns) == 8
    assert [turn.role_id for turn in turns] == [
        "orchestrator", "pm", "cto", "designer",
        "pm", "cto", "designer", "orchestrator",
    ]
    assert meeting.speaking_order(1) == ROLE_SPECS
    assert meeting.speaking_order(2) == ROLE_SPECS[1:] + ROLE_SPECS[:1]


def test_round_two_asks_each_role_to_answer_someone_else_and_the_orchestrator_to_close() -> None:
    """第二轮的 system 是在岗位稿**后面追加**一段复审要求，不是换一份岗位稿。

    换掉岗位稿等于让第二轮的人忘了自己是谁；两件事叠在一起才对：
    岗位稿讲"你是谁"，复审讲"这一轮你要回答谁"。
    """
    client = FakeChat([f"第{i + 1}句" for i in range(8)])
    run_meeting("做一个手冲咖啡官网", client=client, rounds=2)

    first_round = [system for system, _ in client.prompts[:4]]
    second_round = [system for system, _ in client.prompts[4:]]
    assert not any("复审" in system for system in first_round)
    # 三人被要求点名回应别人，总管被要求收敛
    assert [("复审" in system) for system in second_round] == [True, True, True, False]
    assert "收敛" in second_round[3]
    # 岗位稿还在：第二轮的 system 是"岗位稿 + 复审段"，不是另一份人设
    assert all(
        system.startswith(meeting.system_of(spec)) for system, spec in zip(second_round, meeting.speaking_order(2))
    )


def test_round_two_sees_all_of_round_one() -> None:
    """第二轮的第一句就能读到第一轮四段 —— `said` 是累计的，不另传上下文。"""
    client = FakeChat([f"第{i + 1}句" for i in range(8)])
    run_meeting("做一个手冲咖啡官网", client=client, rounds=2)

    second_round_first = client.prompts[4][1]
    for i in range(1, 5):
        assert f"第{i}句" in second_round_first
    assert "第5句" not in second_round_first


def test_the_round_count_is_written_into_the_digest_so_the_page_knows_the_denominator() -> None:
    """`会议轮数` 那一格是前端算"说完了没有"的分母。

    不写它，前端只能按写死的 4 条判 —— 2 轮的会说到第 4 条就被判"已锁定方案"，
    第 2 轮的讨论反而看不见（`web/src/lib/live.ts::roundsOf`）。
    """
    merged = merge_into_digest(None, run_meeting("x", client=FakeChat([f"第{i}句" for i in range(8)]), rounds=2), rounds=2)
    assert merged[meeting.ROUND_SECTION] == ["2"]
    assert len(merged[meeting.MEETING_SECTION]) == 8


def test_the_callback_fires_once_per_turn_across_rounds() -> None:
    """逐岗落库的时序在多轮下不变：八次回调，每次都在对应的那次 chat 之后。"""
    client = FakeChat([f"第{i + 1}句" for i in range(8)])
    seen: list[int] = []
    run_meeting("x", client=client, rounds=2, on_turn=lambda turn, said: seen.append(len(client.prompts)))
    assert seen == [1, 2, 3, 4, 5, 6, 7, 8]


# ---------------------------------------------------------------- 真客户端的形状


def _client(handler) -> DoubaoChatClient:
    return DoubaoChatClient(
        api_key="test-key",
        model="doubao-test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_chat_sends_the_expected_payload_and_reads_the_content() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  三页方案  "}}]},
        )

    text = _client(handler).chat("你是产品经理", "客户原话：做一个官网")
    assert text == "三页方案"
    assert str(seen["url"]).endswith("/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    body = str(seen["body"])
    assert '"model":"doubao-test"' in body.replace(" ", "")
    assert "你是产品经理" in body


def test_http_error_reports_status_and_code_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "InvalidParameter", "message": "secret prompt echo"}},
        )

    with pytest.raises(MeetingUnavailable) as caught:
        _client(handler).chat("s", "u")
    assert caught.value.code == "meeting_http_error"
    assert "InvalidParameter" in str(caught.value)
    assert "secret prompt echo" not in str(caught.value)


def test_bad_response_shape_is_reported_as_such() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(MeetingUnavailable) as caught:
        _client(handler).chat("s", "u")
    assert caught.value.code == "meeting_bad_response"


def test_credentials_are_checked_before_any_request() -> None:
    with pytest.raises(MeetingUnavailable) as caught:
        DoubaoChatClient(api_key="  ", model="m")
    assert caught.value.code == "meeting_credential_missing"


# ---------------------------------------------------------------- 深度思考开关（2026-10-28）


def test_thinking_is_left_alone_by_default() -> None:
    """默认**不带** `thinking` 字段 —— 这一层不认识所有端点，不许替它们做主。

    代价很具体：接一个不认这个字段的端点，每次调用都多一次 400 往返。
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    _client(handler).chat("s", "u")
    assert "thinking" not in seen["body"]


def test_thinking_false_sends_the_disabled_flag() -> None:
    """会议/文档那两条链要的就是这一句：四段人话不需要 2500 个思考 token。

    实测（`doubao-seed-2-1-pro`，同一 prompt）：开 63.8s / 2573 reasoning，
    关 2.8s / 0，输出都是 65 字左右。
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    DoubaoChatClient(
        api_key="k", model="m", http=httpx.Client(transport=httpx.MockTransport(handler)), thinking=False
    ).chat("s", "u")
    assert seen["body"]["thinking"] == {"type": "disabled"}


def test_an_endpoint_that_rejects_thinking_is_retried_without_it_once() -> None:
    """端点不认这个字段时去掉重试，并**记住** —— 否则一场会多出四次无谓的 400 往返。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        bodies.append(body)
        if "thinking" in body:
            return httpx.Response(400, json={"error": {"code": "InvalidParameter", "message": "unknown field: thinking"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    client = DoubaoChatClient(
        api_key="k", model="m", http=httpx.Client(transport=httpx.MockTransport(handler)), thinking=False
    )
    assert client.chat("s", "u") == "好"
    assert client.chat("s", "u") == "好"

    assert ["thinking" in body for body in bodies] == [True, False, False], (
        "第一次撞 400 后应该去掉字段重试，之后不再带 —— 实际请求序列不对"
    )


def test_a_400_that_is_not_about_thinking_is_not_retried() -> None:
    """内容被拒也是 400。把它当"字段不支持"去重试，真拒因会被第二次的结果盖住。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"code": "ContentPolicyViolation", "message": "nope"}})

    client = DoubaoChatClient(
        api_key="k", model="m", http=httpx.Client(transport=httpx.MockTransport(handler)), thinking=False
    )
    with pytest.raises(MeetingUnavailable) as caught:
        client.chat("s", "u")
    assert caught.value.code == "meeting_http_error"
    assert len(calls) == 1
