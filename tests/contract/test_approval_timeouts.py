"""T057 的判据：`approval/timeouts.py`（FR-043 / FR-035｜R-26，宪法 V）。

本文件属 `contract` 一档（秒级、不碰 PG）：它验的是**代码与两份治理文件的对齐方式**，
形态与 `test_states.py` 同族 —— 不是"抄一份数字再断言相等"，而是**改动来源即改动读数**，
所以每条数值判据都带一个"换一份文本进去 ⇒ 读数跟着变"的注入对照，每条结构约束都带一个
"数值摆到最宽松也不许变行为"的反向对照。

三条最容易做假的地方，逐条点名：
1. **数值判据不许拿代码常量当事实源**。首版我差点写成 `assert policy.wait == dt.timedelta(hours=2)` ——
   那等于用实现值验实现值，把 §1 改成 5 小时也不会红。现在的写法一律"先读文档、再断言读数、
   最后换文本证伪"，三步缺一步这条判据就是死的。
2. **结构约束的反向对照必须故意排在会被错选的那一侧**：A / C / D 三行在 §1 里写的正是"无超时"，
   拿 `decide()` 传一个**已经过了任何可能的时刻**的 `review_round` 与 `extensions_used` 去测它们，
   才是"MUST NOT 自动放行"的真判据（只测 B 那条正例，会把"三类都被放宽"读成绿）。
3. **"延长次数用尽而轮次未跑满"这一档取的是继续挂起**，文档没写（见模块头的登记），
   所以这里钉的是**本轮实现的选择**而不是文档的原文；owner 若改判，这条要跟着改，
   而不是让它默默把实现的两可状态读成已裁。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from acp.approval import timeouts as T
from acp.persistence.enums import values_of

PARAMS_REF = T.params_ref_default()
CONSTITUTION = T.constitution_default()

#: §1 表里 B 行的原文片段，用来做"换文本 ⇒ 换读数"的注入对照。
_B_ROW_WAIT = "| B 验收未过 / 需人工定论 | 2 小时 |"


def policies(params_ref: Path | None = None) -> dict[str, T.TimeoutPolicy]:
    return T.parse_timeout_table(path=params_ref or PARAMS_REF)


def write_variant(tmp_path: Path, text: str, name: str = "params-ref.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def patch_params_ref(tmp_path: Path, old: str, new: str) -> Path:
    text = PARAMS_REF.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"注入点不唯一（{text.count(old)} 处），换文本等于没换到那一行：{old!r}"
    return write_variant(tmp_path, text.replace(old, new))


def patch_constitution(tmp_path: Path, old: str, new: str) -> Path:
    text = CONSTITUTION.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"宪法里的注入点不唯一：{old!r}"
    path = tmp_path / "constitution.md"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 数值面：来源唯一

def test_all_five_kinds_have_a_row_and_the_domain_is_the_only_key_source():
    """五类卡点（Phase 17 起含 E）在 §1 里各有一行，键名由域派生而不是在本模块抄一份映射。"""
    table = policies()
    assert set(table) == set(values_of("approval_kind"))
    assert {p.kind for p in table.values()} == set(values_of("approval_kind"))
    # 首字母 ⇄ 域取值的对应关系必须成立（域里冒出 `E_X` 而表里写 `E …` 才算这条有效）。
    assert T.KIND_BY_LETTER == {value[0]: value for value in values_of("approval_kind")}


def test_no_approval_kind_literal_is_copied_into_the_loader():
    """卡点名（现读五类）的唯一来源是 `approval_kind` 域：源码里出现其字面量即算抄了第二份映射。

    这一条只查**域取值**（`A_SPLIT` 那四个串）。首版还加了一条「字母 A 也不许出现在源码里」，
    实测恒红 —— `KIND_BY_LETTER[letter] for letter in "ACD"` 与 `CONVERGE_KINDS` 那行本来就该按字母取，
    字母是**表的列值**不是域的抄写。留一条永远过不去的判据和留一条永远过得去的一样没用。
    """
    source = Path(T.__file__).read_text(encoding="utf-8")
    for kind in values_of("approval_kind"):
        assert f'"{kind}"' not in source, f"timeouts.py 里出现了 {kind} 的字面量 ⇒ 映射被抄进代码"


def test_durations_are_read_from_section_one_and_follow_the_text(tmp_path):
    """**换文本就换读数**：把 B 行的 2 小时改成 5 小时，`wait` 必须是 5 小时。

    这条是数值判据里唯一能证明"代码没有把时长写死"的一支；只断言"读到了 2 小时"是拿实现验实现。
    """
    table = policies()
    assert table[T.KIND_BY_LETTER["B"]].wait == dt.timedelta(hours=2)
    swapped = T.parse_timeout_table(
        path=patch_params_ref(tmp_path, _B_ROW_WAIT, "| B 验收未过 / 需人工定论 | 5 小时 |")
    )
    assert swapped[T.KIND_BY_LETTER["B"]].wait == dt.timedelta(hours=5)
    # 反向对照：只改加粗标记（纯排版）不许改读数，否则这条判据会把任何改写都当成"生效"。
    cosmetic = T.parse_timeout_table(
        path=patch_params_ref(tmp_path, "**收敛仅在第 3 轮结束后生效**", "收敛仅在第 3 轮结束后生效")
    )
    assert cosmetic[T.KIND_BY_LETTER["B"]].wait == dt.timedelta(hours=2)
    assert cosmetic[T.KIND_BY_LETTER["B"]].round_stated_in_params_ref == 3


def test_no_timeout_rows_carry_none_and_the_sentinel_constant_is_gone():
    """A / C / D / E 四行写"无超时" ⇒ `wait is None`，且 `expires_at()` 不给时刻。

    这里钉的是"无超时"这件事**没有**被实现成"一个很大的时长"：裁决 ②（owner 2026-10-13）之后
    `expires_at()` 的 `None` **一路到底**成 SQL `NULL`（`timeout_at` 已放宽为可空，见迁移 0006），
    不再由仓储层折成某个上界哨兵。所以这一支断的是"None 没有被换算成任何时刻"。
    """
    table = policies()
    for letter in "ACDE":
        policy = table[T.KIND_BY_LETTER[letter]]
        assert policy.never_times_out, f"{letter} 类在 §1 里是「无超时」，读成有超时就是给了它一个到期时刻"
        assert policy.expires_at(dt.datetime.now(dt.timezone.utc)) is None
        assert policy.on_timeout == "挂起等待"
    now = dt.datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert table[T.KIND_BY_LETTER["B"]].expires_at(now) == now + dt.timedelta(hours=2)
    # 裁决 ② 的形态判据：哨兵常量必须**从模块里消失**，否则"nullable"只是嘴上说说、
    # 某个角落还会把它落回库。（这一条不是风格洁癖：留着常量就等于留着第二条口径。）
    assert not hasattr(T, "NEVER_TIMES_OUT"), "上界哨兵还在 timeouts 模块里 ⇒ 仓储层可能又去落它"


# ---------------------------------------------------------------- 结构面：不随数值漂移

@pytest.mark.parametrize("letter", ["A", "C", "D", "E"])
def test_timeout_never_auto_passes_for_A_C_D_E_even_with_the_loosest_numbers(letter):
    """FR-043 的"MUST NOT 自动放行"是**结构约束**：轮次给到 99、延长给到 99 也不许收敛。

    故意取远超 §1 任何数值的参数（反向对照排在会被错选那一侧）：若实现把"超过延长上限"
    当成"那就收敛吧"，这一条红，而 B 的正例照样绿。E（审查不确定）是 Phase 17 加进来的人工
    升级单：它连超时都没有，更不许被任何数值档位放行。
    """
    assert T.decide(T.KIND_BY_LETTER[letter], review_round=99, extensions_used=99) == T.WAIT


def test_E_is_a_human_escalation_row_without_timeout():
    """E 行在 §1 里必须被解析成"无超时 + 挂起等待"，且该类的结构约束归 `AUTO_PASS_ON_TIMEOUT_FORBIDDEN`。"""
    e = T.KIND_BY_LETTER["E"]
    policy = policies()[e]
    assert policy.never_times_out and policy.on_timeout == "挂起等待"
    assert e in T.AUTO_PASS_ON_TIMEOUT_FORBIDDEN
    assert e not in T.CONVERGE_KINDS


def test_B_converges_only_after_the_round_limit_read_from_the_constitution(tmp_path):
    """轮次上限来自宪法 原则 III（不是 §1、也不是代码常量），且**跑满**才收敛。"""
    limit = T.review_round_limit()
    assert limit == 3
    b = T.KIND_BY_LETTER["B"]
    assert T.decide(b, review_round=limit - 1) == T.EXTEND
    assert T.decide(b, review_round=limit) == T.CONVERGE
    # 换两份治理文件里的轮次数 ⇒ 判定门随文档移动（§1 那句复述必须同步改，否则先被上面的漂移
    # 判据拦住 —— 这正说明两处数字是**同一个约束的两个面**，不是一个数值的两份抄写）。
    const = patch_constitution(tmp_path, "验收轮次上限 3", "验收轮次上限 5")
    ref = patch_params_ref(tmp_path, "收敛仅在第 3 轮结束后生效", "收敛仅在第 5 轮结束后生效")
    assert T.review_round_limit(const) == 5
    assert T.decide(b, review_round=4, params_ref=ref, constitution=const) == T.EXTEND
    assert T.decide(b, review_round=5, params_ref=ref, constitution=const) == T.CONVERGE


def test_extension_step_and_cap_come_from_the_table(tmp_path):
    """延长档位（每次几小时、最多几次）是 §1 的数值 ⇒ 改掉文本，档位跟着变。"""
    table = policies()
    b = table[T.KIND_BY_LETTER["B"]]
    assert (b.extension_length, b.max_extensions) == (dt.timedelta(hours=2), 3)
    swapped = T.parse_timeout_table(
        path=patch_params_ref(tmp_path, "每次 2 小时、最多 3 次", "每次 8 小时、最多 7 次")
    )[T.KIND_BY_LETTER["B"]]
    assert (swapped.extension_length, swapped.max_extensions) == (dt.timedelta(hours=8), 7)


def test_extensions_exhausted_before_full_rounds_keeps_waiting(tmp_path):
    """延长次数用尽而轮次未跑满 ⇒ 继续挂起（本实现取的保守侧，文档未写，见模块头登记）。"""
    b = T.KIND_BY_LETTER["B"]
    max_used = policies()[b].max_extensions
    assert T.decide(b, review_round=1, extensions_used=max_used) == T.WAIT
    # 反向对照：把上限提到 9 次再测同一个 `extensions_used` ⇒ 又变成延长等待，
    # 证明上一条的红来自"上限"而不是来自"未跑满"这一支被写死了。
    raised = patch_params_ref(tmp_path, "每次 2 小时、最多 3 次", "每次 2 小时、最多 9 次")
    table = T.parse_timeout_table(path=raised)
    assert T.decide(b, review_round=1, extensions_used=max_used, table=table) == T.EXTEND


# ---------------------------------------------------------------- 漂移与缺失：都要喊

def test_restatement_drift_between_the_two_docs_is_an_error(tmp_path):
    """§1 复述的"第 3 轮"与宪法的硬值不一致 ⇒ 报错，而不是各读各的算出两个数。

    `data-model.md:46` 明写轮次上限"本表不复述数字以免三处漂移"，而 §1 的 B 行确实复述了一次；
    这条判据把那句自律变成机器判定：改一处必红。
    """
    variant = patch_params_ref(tmp_path, "收敛仅在第 3 轮结束后生效", "收敛仅在第 4 轮结束后生效")
    with pytest.raises(T.ParamsRefError, match="params-ref §1 复述的轮次上限是 4"):
        T.load_policies(params_ref=variant, constitution=CONSTITUTION)
    # 宪法那一侧同步改动 ⇒ 两处一致，判定恢复（证明上一条红的是"不一致"而不是"改过"）。
    const = patch_constitution(tmp_path, "验收轮次上限 3", "验收轮次上限 4")
    table, limit = T.load_policies(params_ref=variant, constitution=const)
    assert (limit, table[T.KIND_BY_LETTER["B"]].round_stated_in_params_ref) == (4, 4)


def test_missing_row_or_unreadable_cell_fails_loudly(tmp_path):
    """缺一行、时长列读不出、扩展列读不出，三种形态都不许回落成"没有超时 / 不限次"。"""
    text = PARAMS_REF.read_text(encoding="utf-8")
    dropped = write_variant(tmp_path, "\n".join(
        line for line in text.split("\n") if not line.startswith("| C 超预算 |")
    ))
    with pytest.raises(T.ParamsRefError, match="缺这几类卡点的行"):
        T.parse_timeout_table(path=dropped)

    vague = patch_params_ref(tmp_path, _B_ROW_WAIT, "| B 验收未过 / 需人工定论 | 看情况 |")
    with pytest.raises(T.ParamsRefError, match="读不出时长"):
        T.parse_timeout_table(path=vague)

    no_extension = patch_params_ref(tmp_path, "每次 2 小时、最多 3 次", "延长等待")
    with pytest.raises(T.ParamsRefError, match="延长档位没有数值载体"):
        T.parse_timeout_table(path=no_extension)


def test_unclassified_kind_is_refused_instead_of_defaulting_to_wait():
    """第六类卡点（E 之后的下一类）没登记结构约束 ⇒ 报错。

    回落到"挂起等待"会让"忘了归类"读起来像安全默认。
    """
    with pytest.raises(T.ParamsRefError, match="结构约束未归类"):
        T.decide("F_FUTURE", review_round=1)


def test_section_one_stops_at_the_next_heading(tmp_path):
    """§1 之后的节（§2 阈值表、§3 并发表、§9 的 yaml 围栏）不许被当成超时行读进来。"""
    text = PARAMS_REF.read_text(encoding="utf-8")
    # 在 §2 的标题行**之后**插一张形状相同、首列也是 A/B/C/D 的小表：扫描若越过 `## ` 边界，
    # 这张干扰表就会被当成 §1 的行读进来（A 行会拿到 9 小时），这条判据即红。
    marker = "\n## 2. 低风险免确认阈值（对应宪法原则 V）\n"
    injected = text.replace(
        marker,
        marker + "\n| 卡点 | 默认超时 | 超时后行为 | 附加结构约束 |\n|---|---|---|---|\n"
        "| A 拆单确认 | 9 小时 | 挂起等待 | 干扰行 |\n",
        1,
    )
    assert injected != text
    variant = write_variant(tmp_path, injected)
    assert T.parse_timeout_table(path=variant)[T.KIND_BY_LETTER["A"]].wait is None
    assert len(T.section_one_lines(text)) == 5, "§1 的数据行恰是五类卡点，多一行就是扫过了节边界"
