"""低风险自动验收的判定（2026-10-28，FR-046 / FR-047 的第四处扩展）。

**它解决的是哪一种痛**：`ACP_REVIEW_MODE=deepseek` 打开之后，DeepSeek 审查员给出 PASS 就由
`review/runner.py` 直接代填验收条目、卡进 `DELIVERED`。这条路**不分轻重** —— 一份 README 与一张
会写库的表单页，机器签的字一样重。owner 的原话是"审批卡点太密集，我成了盖章机器"，而解法不是
"把章全交给机器"，是**按风险分级**：低风险的让它自己走，其余的仍然叫人。

**四条闸，缺一不可**（`decide()` 的返回值里 `code` 就是"卡在哪一条"）：

1. `switch_off` —— 开关没开。开关的生效值见 `effective_switch()`；
2. `not_low_risk` —— 任务类型不在 `config/auto_accept.yaml` 的 `low_risk_formats` 里；
3. `triple_broken` —— 这张卡的执行/审查/验收三岗**不是三支不同的工具**（`guard/triple.py` 那一份口径）。
   这一条是"机器代签也必须建立在三岗互斥成立之上"的落点：互斥不成立时，代签等于让同一支工具
   自己审自己、自己签自己；
4. `reviewer_not_pass` —— 独立审查员没有给出 PASS。**没有 PASS 就没有代签**：审查是签字的**前提**，
   不是签字的替代。

**为什么把"开关"与"分类"分成两层**：开关是人的意图（"我现在愿意让机器代签低风险的"），
分类是平台的事实（"这张卡属于低风险吗"）。把两者揉成一个布尔，就会出现"想开但一开全放"或者
"只放某几张但要改代码"这两种都很难解释的形状。

**为什么开关的默认值来自环境变量、运行期值来自 `config/runtime.yaml`**：
- 环境变量 `ACP_AUTO_ACCEPT_LOW_RISK` 是**部署期**的意图：一套环境一个默认；
- `config/runtime.yaml` 的 `auto_accept_low_risk` 是**运行期**的意图：面板上那个胶囊按钮写它。
- 优先级：`runtime.yaml` 写了布尔值就以它为准（`source=runtime`），写 `null` 就跟随环境变量
  （`source=env`），环境变量也没设就是关（`source=default`）。
- 这条优先级**只有一处实现**（`effective_switch`），面板只负责把 `source` 显示出来 ——
  否则"按钮显示开、实际没开"这种最难查的分歧就会长出来。

**失败方向**：这份模块抛的每一类错都朝"**不许代签**"倒。清单读不出、开关值不成形、
运行期参数文件坏了 —— 全部按 `switch_off` / 报错处理，绝不"当没命中就放行"。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from acp.config import runtime
from acp.config.loader import REPO_ROOT

#: 部署期默认。**只认这六个词**，别的写法一律报错 —— "true " / "yes!" / "1 " 这类近似值
#: 在 shell 里极常见，猜错方向（把 `false` 读成真）就是一次静默放行。
AUTO_ACCEPT_ENV = "ACP_AUTO_ACCEPT_LOW_RISK"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

#: 低风险类型清单的唯一位置（与 `risk_matrix.yaml` / `runtime.yaml` 同一个目录、同一条纪律）。
AUTO_ACCEPT_PATH = REPO_ROOT / "config" / "auto_accept.yaml"
FORMATS_KEY = "low_risk_formats"
ALLOWED_KEYS = frozenset({"version", FORMATS_KEY})

#: 判定结果的两档。**只有两档**：低风险（可以代签）与需要人（不代签）。
#: 不设"中风险"是因为它没有对应的动作差异 —— 多一档只会让调用方去猜它该走哪条路。
LOW_RISK = "LOW_RISK"
NEEDS_HUMAN = "NEEDS_HUMAN"

#: 开关的来源。面板上那行小字直接显示它，用户才知道"我按了按钮"与"部署时设了变量"是两件事。
SOURCE_RUNTIME = "runtime"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"

#: 四条闸的代号。`code` 是给人看"为什么没代签"的稳定键名，改了就是调用方要跟着改。
OK = "auto_accept_ok"
SWITCH_OFF = "switch_off"
NOT_LOW_RISK = "not_low_risk"
TRIPLE_BROKEN = "triple_broken"
REVIEWER_NOT_PASS = "reviewer_not_pass"


class AutoApprovalConfigError(ValueError):
    """清单读不出 / 形状不合 / 开关值不成形。失败关闭，不返回半份判定。"""


def env_default(env: Mapping[str, str] | None = None) -> bool:
    """`ACP_AUTO_ACCEPT_LOW_RISK` → 布尔。**空值 = 关**，不认识的写法 = 报错。"""
    source = os.environ if env is None else env
    raw = str(source.get(AUTO_ACCEPT_ENV, "") or "").strip().lower()
    if not raw:
        return False
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise AutoApprovalConfigError(
        f"{AUTO_ACCEPT_ENV}={raw!r} 不是布尔写法 ⇒ 只接受 {sorted(_TRUTHY | _FALSY)} 或空。"
        "这里**不猜**：把 false 读成真就是一次静默放行"
    )


def low_risk_formats(path: Path | None = None) -> frozenset[str]:
    """读 `config/auto_accept.yaml` 的 `low_risk_formats`。读不出 ⇒ 报错，不回落空集。

    回落空集的后果与"规则表没读到"一模一样：所有任务都要人签，看起来保守，
    实际上是**这条路整条失灵而没人喊**（`risk_engine.py` 模块头把这种假绿写成过一条纪律）。
    """
    target = AUTO_ACCEPT_PATH if path is None else path
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoApprovalConfigError(f"低风险清单不存在：{target}") from exc
    except yaml.YAMLError as exc:
        raise AutoApprovalConfigError(f"低风险清单解析失败：{target}：{exc}") from exc
    if not isinstance(raw, dict):
        raise AutoApprovalConfigError(f"{target.name} 的顶层必须是映射（收到 {type(raw).__name__}）")
    unknown = sorted(set(raw) - ALLOWED_KEYS)
    if unknown:
        raise AutoApprovalConfigError(f"{target.name} 有表外键：{unknown} ⇒ 表外键是第二份清单的开始")
    values = raw.get(FORMATS_KEY)
    if not isinstance(values, list) or not values:
        raise AutoApprovalConfigError(f"{target.name} 的 {FORMATS_KEY} 必须是非空列表（收到 {values!r}）")
    formats: set[str] = set()
    for item in values:
        text = str(item or "").strip().lower()
        if not text:
            raise AutoApprovalConfigError(f"{target.name} 的 {FORMATS_KEY} 里有空值")
        formats.add(text)
    return frozenset(formats)


def _raw_env(env: Mapping[str, str] | None) -> str:
    source = os.environ if env is None else env
    return str(source.get(AUTO_ACCEPT_ENV, "") or "").strip()


def _switch_from(params: runtime.RuntimeParams, env: Mapping[str, str] | None) -> tuple[bool, str]:
    """`(enabled, source)` 的唯一判定。**两个入口共用它**，所以优先级不会长成两份。"""
    if params.auto_accept_low_risk is not None:
        return bool(params.auto_accept_low_risk), SOURCE_RUNTIME
    if _raw_env(env):
        return env_default(env), SOURCE_ENV
    return False, SOURCE_DEFAULT


def effective_switch(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """开关的生效值 `(enabled, source)`。**优先级只有这一处实现**（见模块头）。

    `config/runtime.yaml` 里 `auto_accept_low_risk` 写布尔 ⇒ 以它为准（面板按下去了）；
    写 `null` ⇒ 跟随环境变量；环境变量也没有 ⇒ 关。

    这个函数**故意把取键那一行写在体内**：`api/web.py::_runtime_consumers` 数的正是
    "调了加载器、又在同一个函数里取了这个键"的地方。挪进 `_switch_from` 就会让面板上这一键的
    `consumers` 栏变成空的，而那栏空着的意思是"改了没人读"—— 恰恰是这条判据要防的假动作。
    """
    params = runtime.load_runtime_params()
    value = params.auto_accept_low_risk
    if value is not None:
        return bool(value), SOURCE_RUNTIME
    if _raw_env(env):
        return env_default(env), SOURCE_ENV
    return False, SOURCE_DEFAULT


def risk_class_of(card: Any, *, formats: frozenset[str] | None = None) -> str:
    """这张卡属于哪一档风险。**只看平台侧事实**（`artifact_floor_format`），不看标题、不问模型。

    `formats` 只为判据留一个注入位（换一份清单才能证明读的是清单而不是硬编码），生产不传。
    """
    table = low_risk_formats() if formats is None else formats
    raw = str(getattr(card, "artifact_floor_format", "") or "").strip().lower()
    # 枚举值在库里可能带引号形态（`ArtifactFormat.md`），统一取最后一段再比。
    value = raw.rsplit(".", 1)[-1]
    return LOW_RISK if value in table else NEEDS_HUMAN


@dataclass(frozen=True)
class Decision:
    """一次"要不要让机器代签"的判定。`allowed` 与 `code` 必须同时被读 —— 只读 `allowed` 的调用方
    在失败时说不出理由，活动流里就会出现"机器没签、也没说为什么"。
    """

    allowed: bool
    code: str
    risk_class: str
    source: str
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "risk_class": self.risk_class,
            "switch_source": self.source,
            "detail": self.detail,
        }


def decide(
    card: Any,
    *,
    reviewer_pass: bool,
    triple_reasons: tuple[str, ...] = (),
    formats: frozenset[str] | None = None,
    env: Mapping[str, str] | None = None,
    params: runtime.RuntimeParams | None = None,
) -> Decision:
    """四条闸依次判，第一条不成立就返回。顺序是刻意的：开关 → 风险 → 互斥 → 审查。

    为什么"互斥"排在"审查"之前：互斥是**这张卡有没有资格**被机器签，审查是**这次签不签得成**。
    资格问题先答，理由才读得顺（"这张卡本来就不该机器签" vs "这张卡本该机器签但这次没通过"）。
    """
    enabled, source = (
        effective_switch(env) if params is None else _switch_from(params, env)
    )
    risk = risk_class_of(card, formats=formats)
    if not enabled:
        return Decision(False, SWITCH_OFF, risk, source, "自动审批开关没开（环境变量未设且面板未打开）")
    if risk != LOW_RISK:
        return Decision(False, NOT_LOW_RISK, risk, source, f"任务类型 {_format_of(card)!r} 不在低风险清单里")
    if triple_reasons:
        return Decision(
            False, TRIPLE_BROKEN, risk, source,
            "三岗互斥不成立 ⇒ 机器代签会变成同一支工具自己审自己：" + "；".join(triple_reasons),
        )
    if not reviewer_pass:
        return Decision(False, REVIEWER_NOT_PASS, risk, source, "独立审查员没有给出 PASS，代签没有依据")
    return Decision(True, OK, risk, source, "低风险 + 三岗互斥成立 + 审查员 PASS ⇒ 机器代签")


def _format_of(card: Any) -> str:
    raw = str(getattr(card, "artifact_floor_format", "") or "").strip().lower()
    return raw.rsplit(".", 1)[-1]
