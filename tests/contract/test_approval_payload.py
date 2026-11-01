"""T047 的判据：审批单五要素（FR-039 / FR-038｜R-26），`contracts/approval-api.md §2` 的落地。

本文件属 `contract` 一档：**不碰 PG**，只验"建单路径的形状"。为什么把形状与库分开验：
五要素缺项这件事必须在**离库最远的地方**拦（`MUST NOT 呈交` = 一行都不该写），
把它放到 integration 档去验就会出现"库里已经有半张单，测试只是在数它的行数"。

四条判据各自的用意：
1. **键名与契约文档做双向差集**（`test_the_five_keys_match_the_contract_both_ways`）：
   `_FIVE` 那份元组与 `approval-api.md §2` 那张表必须一字不差地互等，两个方向都查。
   只查"代码里的键都在文档里"会漏掉"文档加了第六项而代码没跟" —— 那正是 FR-039 的
   "缺项 MUST NOT 呈交"最容易被绕过去的形态：文档改口径，代码不知道自己缺了一项。
2. **`timeout_policy` 不符也要拒**，而且拒的码与"缺项"同一条（细分在 detail 的键名里）：
   调用方递一份"5 分钟后自动放行"的策略进来，就是 FR-043 那句 MUST NOT 想挡的东西。
3. **B / C 类报错而不是静默建单**（MVP-1 只启 A / D）。
4. **免确认没有入参**（FR-047 的"AI 无覆盖入口"）：签名上查得到 `exempt=` / `override=`
   就是入口，不是文档里的一句话。
"""

from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path

import pytest

from acp.approval import service as S
from acp.approval import timeouts as T
from acp.approval.repository import ApprovalMisuse
from acp.approval.timeouts import KIND_BY_LETTER
from acp.persistence.enums import values_of
from acp.registry.risk_engine import RequestFacts

CONTRACT = (
    Path(S.__file__).resolve().parents[3]
    / "specs" / "001-multi-agent-collab-platform" / "contracts" / "approval-api.md"
)

SECTION = "## 2. 建单校验（缺项 MUST NOT 呈交，FR-039）"
_KEY_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|")


def contract_keys() -> list[str]:
    """从 §2 那张表的"键"列取五要素名（锚点按前缀取，与 `timeouts.section_one_lines` 同一条口径）。"""
    lines = CONTRACT.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(SECTION))
    keys: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        hit = _KEY_RE.match(line)
        if hit:
            keys.append(hit.group(1))
    return keys


def payload_for(kind: str, **over: object) -> dict:
    base = {
        "pending_item": "把首页拆成 3 张卡，确认这份清单",
        "context_digest": "需求：做个介绍公司产品的静态网站；自行补全：首页取 1 个",
        "suggested_verdict": "approve",
        "impact": {"downstream_task_ids": ["T01", "T02"], "estimated_extra_usage": {"TOKEN": 1200}},
        "timeout_policy": T_policy(kind),
    }
    base.update(over)
    return base


def T_policy(kind: str) -> dict:
    return S.render_timeout_policy(kind)


# ---------------------------------------------------------------- 键名的唯一载体

def test_the_five_keys_match_the_contract_both_ways():
    """代码那份元组与契约 §2 那张表**双向差集为空**（同 `test_states.py` 之于 `data-model.md §2`）。"""
    doc = set(contract_keys())
    code = set(S._FIVE)
    assert doc - code == set(), f"文档里要求而代码不查的键：{sorted(doc - code)}"
    assert code - doc == set(), f"代码里查而文档没要求的键：{sorted(code - doc)}"
    assert len(S._FIVE) == 5, "FR-039 写的是五项；条数一变就不是同一件事了"


def test_missing_key_is_refused_before_anything_is_written():
    """逐条缺项都要拒，且 detail 点名缺的是哪一键（五缺一≠没缺）。"""
    for key in S._FIVE:
        broken = payload_for(KIND_BY_LETTER["A"])
        broken[key] = None
        with pytest.raises(S.PayloadIncomplete) as caught:
            S.validate_payload(broken, kind=KIND_BY_LETTER["A"])
        assert caught.value.missing == (key,), f"{key} 缺项时点名错了：{caught.value.missing}"
        assert caught.value.code == "payload_incomplete"


@pytest.mark.parametrize("extra", [{"approver": "boss"}, {"auto_pass": True}])
def test_a_sixth_key_is_refused(extra):
    """多一个键就拒：payload 里混进「谁来批」「要不要免确认」这类字段，就是第二条判定路径的开始。"""
    broken = {**payload_for(KIND_BY_LETTER["A"]), **extra}
    with pytest.raises(S.PayloadIncomplete) as caught:
        S.validate_payload(broken, kind=KIND_BY_LETTER["A"])
    assert set(caught.value.malformed) == set(extra)


# ---------------------------------------------------------------- 超时策略不接受自带

def test_a_hand_written_timeout_policy_is_refused_not_trusted():
    """调用方写"0 秒后自动放行"必须被拒 —— FR-043 的 MUST NOT 在这条线上落地。"""
    sneaky = payload_for(KIND_BY_LETTER["A"])
    sneaky["timeout_policy"] = {**T_policy(KIND_BY_LETTER["A"]), "wait_seconds": 0}
    with pytest.raises(S.PayloadIncomplete) as caught:
        S.validate_payload(sneaky, kind=KIND_BY_LETTER["A"])
    assert caught.value.conflicts == ("timeout_policy",)
    # 反向对照：逐字照 §1 算出来的那份必须过，否则上一条的红来自"改过"而不是"不符"。
    assert S.validate_payload(payload_for(KIND_BY_LETTER["A"]), kind=KIND_BY_LETTER["A"])["timeout_policy"]


def test_rendered_policy_carries_the_doc_values_and_A_C_D_are_flagged():
    """渲染出来的五要素⑤：A / C / D 是"无超时 + 不得自动放行"，B 带 7200 秒与轮次上限。"""
    for letter in "ACD":
        rendered = T_policy(KIND_BY_LETTER[letter])
        assert rendered["wait_seconds"] is None, f"{letter} 类无超时 ⇒ 不许给一个秒数"
        assert rendered["auto_pass_forbidden"] is True
        assert rendered["convergence_round_limit"] is None, f"{letter} 类没有「跑满轮次才收敛」这回事"
    b = T_policy(KIND_BY_LETTER["B"])
    assert (b["wait_seconds"], b["max_extensions"], b["convergence_round_limit"]) == (7200, 3, 3)
    assert b["auto_pass_forbidden"] is False


def test_rendered_policy_follows_the_injected_table(tmp_path):
    """把 §1 的 B 行改成 5 小时再渲染 ⇒ 快照跟着变（证明渲染读的是那份表，不是内联的 7200）。"""
    text = T.params_ref_default().read_text(encoding="utf-8")
    variant = tmp_path / "params-ref.md"
    variant.write_text(
        text.replace(
            "| B 验收未过 / 需人工定论 | 2 小时 |", "| B 验收未过 / 需人工定论 | 5 小时 |", 1
        ),
        encoding="utf-8",
    )
    table = T.parse_timeout_table(path=variant)
    assert S.render_timeout_policy(KIND_BY_LETTER["B"], table=table)["wait_seconds"] == 18000


# ---------------------------------------------------------------- 建议结论与影响面

def test_suggested_verdict_is_the_same_domain_as_human_verdicts():
    """系统建议的结论词表 = EN-10 的 `resolution_verdict`，不另造一套"系统专用"的词。"""
    for verdict in values_of("resolution_verdict"):
        assert S.validate_payload(
            payload_for(KIND_BY_LETTER["A"], suggested_verdict=verdict), kind=KIND_BY_LETTER["A"]
        )["suggested_verdict"] == verdict
    with pytest.raises(S.PayloadIncomplete) as caught:
        S.validate_payload(payload_for(KIND_BY_LETTER["A"], suggested_verdict="LGTM"), kind=KIND_BY_LETTER["A"])
    assert "suggested_verdict" in caught.value.malformed


def test_impact_usage_is_split_by_billing_basis():
    """FR-061：用量按口径分键存放。表外的键（一个加总的数、一个"合计"）都拒。"""
    ok = {"downstream_task_ids": ["T01"], "estimated_extra_usage": {k: 1 for k in values_of("billing_basis")}}
    assert S.validate_payload(
        payload_for(KIND_BY_LETTER["A"], impact=ok), kind=KIND_BY_LETTER["A"]
    )["impact"] == ok
    for bad in (
        {"downstream_task_ids": ["T01"], "estimated_extra_usage": {"TOTAL": 1}},
        {"downstream_task_ids": [], "estimated_extra_usage": {"TOKEN": 1}},
        {"downstream_task_ids": ["T01"], "estimated_extra_usage": {"TOKEN": -1}},
        {"downstream_task_ids": ["  "], "estimated_extra_usage": {"TOKEN": 1}},
    ):
        with pytest.raises(S.PayloadIncomplete):
            S.validate_payload(payload_for(KIND_BY_LETTER["A"], impact=bad), kind=KIND_BY_LETTER["A"])


# ---------------------------------------------------------------- 卡点作用域与免确认入口

@pytest.mark.parametrize("letter", ["B", "C"])
def test_reserved_kinds_are_refused_at_create_time(letter):
    """MVP-1 未启用的两类：域里有值、表能建，但**建单这条路拒**（不静默产出看起来像真单的记录）。"""
    kind = KIND_BY_LETTER[letter]
    with pytest.raises(S.KindNotEnabled):
        S.create_approval(
            None,  # 会话在这里用不上：拒绝发生在任何写之前
            kind=kind,
            payload=payload_for(kind),
            feature_request_id=uuid.uuid4(),
        )


def test_active_kinds_are_derived_from_the_domain_and_are_exactly_A_D_and_E():
    """Phase 17 P1 起 E 也有建单路径；B / C 仍是结构预留（只建模不启用）。"""
    assert S.ACTIVE_KINDS == frozenset(
        {KIND_BY_LETTER["A"], KIND_BY_LETTER["D"], KIND_BY_LETTER["E"]}
    )
    assert set(values_of("approval_kind")) - S.ACTIVE_KINDS == {
        KIND_BY_LETTER["B"],
        KIND_BY_LETTER["C"],
    }


def test_E_ticket_allows_an_empty_downstream_impact_and_stays_unautopassable():
    """E 类（审查不确定）的叶子卡没有后继 ⇒ 五要素④允许空 `downstream_task_ids`。

    这条同时钉三件事：
    ① 空列表只对 E 放开，**不是**把 non-E 的校验一起放宽（反向对照在下一支）；
    ② E 的 `timeout_policy` 从 §1 的 E 行算出来：无超时 + 不得自动放行；
    ③ E 真的过了建单路径的"启用"门（不是 `KindNotEnabled`），五要素 schema 合法。
    """
    kind = KIND_BY_LETTER["E"]
    payload = payload_for(
        kind,
        impact={"downstream_task_ids": [], "estimated_extra_usage": {"TOKEN": 0}},
        suggested_verdict="need_info",
    )
    normalized = S.validate_payload(payload, kind=kind)
    assert normalized["impact"]["downstream_task_ids"] == []
    assert normalized["timeout_policy"]["wait_seconds"] is None
    assert normalized["timeout_policy"]["auto_pass_forbidden"] is True
    assert normalized["timeout_policy"]["convergence_round_limit"] is None

    # 反向对照：A / C / D 的空列表仍必须被拒 —— E 的例外不许顺手放开别人的门。
    for letter in "ACD":
        non_e = KIND_BY_LETTER[letter]
        broken = payload_for(
            non_e, impact={"downstream_task_ids": [], "estimated_extra_usage": {"TOKEN": 0}}
        )
        with pytest.raises(S.PayloadIncomplete) as caught:
            S.validate_payload(broken, kind=non_e)
        assert "impact" in caught.value.malformed, caught.value.malformed

    # 建单门：会话给 None，后面必然炸在别处；这条只管"没被 KindNotEnabled 拒掉"。
    try:
        S.create_approval(None, kind=kind, payload=payload, feature_request_id=uuid.uuid4())
    except S.KindNotEnabled:
        pytest.fail("E 类在 Phase 17 P1 已启用，建单路径不许再报 KindNotEnabled")
    except Exception:
        pass


def test_E_still_refuses_blank_ids_and_unknown_usage_keys():
    """空列表是 E 的唯一例外；列表里塞空白串或用量键越域，照旧拒。"""
    kind = KIND_BY_LETTER["E"]
    for impact in (
        {"downstream_task_ids": ["  "], "estimated_extra_usage": {"TOKEN": 0}},
        {"downstream_task_ids": [], "estimated_extra_usage": {"TOTAL": 0}},
        {"downstream_task_ids": [], "estimated_extra_usage": {"TOKEN": -1}},
    ):
        with pytest.raises(S.PayloadIncomplete):
            S.validate_payload(payload_for(kind, impact=impact), kind=kind)


def test_a_card_level_ticket_must_also_carry_the_request_id():
    """挂到卡却漏了需求单 ⇒ 建单路径拒（库里那条复合外键在 MATCH SIMPLE 下不校验）。

    这条判据存在的原因是 `data-model.md §4` 把这件事明确指派给 `approval/service.py`：
    数据库那一侧只保证"两列都非空时配对正确"，一列 NULL 就整个跳过；而"半拉子行"一旦落库，
    看板会显示一张有卡没主的单，FK 与用例都看不见它错在哪。
    """
    kind = KIND_BY_LETTER["A"]
    with pytest.raises(ApprovalMisuse, match="没挂需求单"):
        S.create_approval(None, kind=kind, payload=payload_for(kind), task_card_id="T01")


def test_a_request_level_ticket_is_the_legal_shape_not_a_rejected_one():
    """反向对照：A 类单的正常形状（挂需求单、卡为 NULL）不许被上面那道检查拒掉。

    单独一条而不是塞进上一条里，是因为两件事红的含义不同：上一条红 = 门禁漏了，
    这一条红 = 门禁过宽（把文档明写的合法形态拦掉了，`models_approval.py:110-115` 那段
    "为什么不用 MATCH FULL"讲的正是这种过宽）。
    """
    kind = KIND_BY_LETTER["A"]
    try:
        S.create_approval(
            None, kind=kind, payload=payload_for(kind), feature_request_id=uuid.uuid4()
        )
    except ApprovalMisuse as exc:
        pytest.fail(f"需求单级的 A 类单被建单校验拒了：{exc}")
    except Exception:
        # 会话给的是 None：后面必然炸在别处（拿 None 当 Session）。这条判据只管"没被拒在建单校验上"。
        pass


def test_no_entry_point_lets_the_caller_set_the_exemption():
    """FR-047"AI 无覆盖入口"的**形状**判据：公共建单路径与判定函数都没有能设免确认的参数。"""
    for function, name in ((S.create_approval, "service.create_approval"),):
        params = inspect.signature(function).parameters
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), name
        forbidden = [p for p in params if any(word in p for word in ("exempt", "auto_pass", "override"))]
        assert forbidden == [], f"{name} 出现了覆盖入口：{forbidden}"
    verdict_params = inspect.signature(S.evaluate).parameters
    assert set(verdict_params) <= {"facts", "rules", "threshold"}, sorted(verdict_params)
