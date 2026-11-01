"""四岗位会议（2026-10-27 owner 选 4）：真调模型讨论需求，台词落进 `summary_digest` 的「会议」段。

这一层只做"说人话"，不做拆单 —— 拆单仍归 `orchestrate/nodes/split.py` 与它的规则。
入口在 `acp.serve_intake.WebIntakeRunner`：网页受理之后先开会、再拆单；
会议失败不挡拆单（`MeetingUnavailable` 由调用方捕获并留痕）。
"""

from acp.meeting.service import (
    DEFAULT_ROUNDS,
    MEETING_SECTION,
    ROUND_SECTION,
    ROUNDS_ENV,
    ROLE_SPECS,
    DoubaoChatClient,
    MeetingRunner,
    MeetingUnavailable,
    RoundsInvalid,
    Turn,
    default_client,
    default_runner,
    digest_lines,
    merge_into_digest,
    parse_lines,
    resolve_rounds,
    run_meeting,
)

__all__ = [
    "DEFAULT_ROUNDS",
    "MEETING_SECTION",
    "ROUND_SECTION",
    "ROUNDS_ENV",
    "ROLE_SPECS",
    "DoubaoChatClient",
    "MeetingRunner",
    "MeetingUnavailable",
    "RoundsInvalid",
    "Turn",
    "default_client",
    "default_runner",
    "digest_lines",
    "merge_into_digest",
    "parse_lines",
    "resolve_rounds",
    "run_meeting",
]
