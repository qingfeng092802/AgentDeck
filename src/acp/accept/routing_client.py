"""通用验收 Runner 的模型客户端（路线 C，2026-10-31）。

**调用点**：低风险自动验收开关打开时，`realrun._sign_by_gate` 若发现这一单点名了验收模型，
代签决定由它给出（ACCEPT/REJECT/NEED_HUMAN），不再用 `gate-review-pass` 这个固定代理。

形状：只输出严格 JSON `{"decision":"ACCEPT|REJECT","reason":"..."}`；解析复用
`settings_run._json_object`（剥离围栏 + 末级扫描），传输复用 `_chat`（预算翻倍/退避）。
**最多重试 2 次**（共 3 发）：三次都拿不到可解析的二值决定 ⇒ `NEED_HUMAN`，卡留给人工签。
"""

from __future__ import annotations

import json

from acp import settings_run

DECISIONS = ("ACCEPT", "REJECT")
MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试
MAX_TOKENS = 1024
#: 2026-10-31 真机：豆包 lite 在长正文清单上 120s 读超时（T02–T04 三张都撞）⇒ 放到 300s，
#: 与审查席同档；超时仍折成 NEED_HUMAN（不猜结论）。
TIMEOUT_S = 300.0

SYSTEM = (
    "你是交付验收员。只输出一个严格 JSON 对象，不要 Markdown 代码围栏，不要解释。\n"
    '对象字段：decision（只能是 "ACCEPT" 或 "REJECT"）、reason（一句话理由）。\n'
    "你验收的是**当前这一张卡**的交付：`criteria` 是这张卡的验收标准，逐条判；`requirement` 是"
    "整单上下文，**不要要求这张卡覆盖需求里其它卡负责的页面/文件** —— 例如需求要三页而本卡只交一页，"
    "只要这一页与卡上标准相符就算通过（2026-10-31 真机：按整单口径判会把每张单页卡都判成缺页）。\n"
    "产物已通过门禁；`artifacts` 给出文件名、字节/行数与正文预览，判据必须能从这些证据核对。\n"
    "不得引入卡外标准；不得因为读不到内容就判 ACCEPT。"
)


def decide(
    seat: settings_run.SeatModel,
    *,
    requirement: str,
    criteria: tuple[str, ...],
    artifacts: list[dict[str, object]],
    evidence: str,
) -> tuple[str, str]:
    """返回 `(ACCEPT|REJECT|NEED_HUMAN, reason)`。异常一律折成 `NEED_HUMAN`，不抛给调用方。

    `artifacts` 是**已过门禁的产物清单**（`rel_path` / `filename` / `bytes` / `lines` / `format`）：
    只给"门禁通过"四个字时，模型无法确认判据点名的文件在不在，只能判 REJECT
    （2026-10-31 真机实测：两次 REJECT 都落在"无法确认产物存在"）。
    """
    user = json.dumps(
        {
            "requirement": requirement,
            "criteria": list(criteria),
            "artifacts": artifacts,
            "gate_evidence": evidence,
        },
        ensure_ascii=False,
    )
    last = "没有拿到可解析的 ACCEPT/REJECT"
    for _attempt in range(MAX_ATTEMPTS):
        try:
            text, _usage = settings_run._chat(
                seat, system=SYSTEM, user=user, max_tokens=MAX_TOKENS, timeout=TIMEOUT_S
            )
        except settings_run.SettingsRunError as exc:
            return "NEED_HUMAN", f"{exc.stage}:{exc.code}: {exc.detail}"[:400]
        try:
            obj = settings_run._json_object("accept", text)
        except settings_run.SettingsRunError as exc:
            last = f"accept:bad_json: {exc.detail}"
            continue
        decision = str(obj.get("decision") or "").strip().upper()
        if decision in DECISIONS:
            return decision, str(obj.get("reason") or "")[:400]
        last = f"决定不在 ACCEPT/REJECT：{decision!r}"
    return "NEED_HUMAN", f"{last}（已试 {MAX_ATTEMPTS} 发）"
