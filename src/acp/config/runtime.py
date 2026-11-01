"""运行时可调参数（MVP-2 第三步 ②"极简参数控制面板"的载体）。

一条 YAML，一个封闭键集，一个加载器。形状照 `load_danger_list()` 那一条程序走，因为要防的是同一件事：
**"改了配置而行为不变"**。四条规则：

1. **键是封闭集合**。表外键一律拒 —— 多出来的键没人解析，那份"参数写了但没生效"比缺键更难发现。
2. **没有内置默认值的回退**。文件读不到就报错，不"当成用了默认值"：那会把"配置丢了"读成"配置没改"。
   唯一的默认来自代码里那两个常量，而它只服务测试与判据（`path=` 是判据专用指位，口径同 danger_list）。
3. **模块级不留缓存**：每次调用都重新读盘。面板改完值就生效，不需要重启服务，
   这正是 owner 要"随时调整"的那个形状。
4. **值按语义校形，不按类型将就**：`fragment_merge_ms` 必须 ≥ 1（0 毫秒的"等待"会把推流循环变成全表扫的
   忙等，而本表除 PK 外没有二级索引）；`max_tokens` 允许 `null`，因为"不发这个键"本身是一个合法取值
   （`DoubaoApiSpec.max_tokens` 的默认就是 `None`，那条"默认不发"的判据在册）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import yaml

from acp.config.loader import REPO_ROOT, redact_bus_text
from acp.protocol.refs import ArtifactFormat

#: 参数的唯一位置。`params-ref §12` 登记它，`tests/guards/test_runtime_params.py` 逐字比两边。
RUNTIME_PATH = REPO_ROOT / "config" / "runtime.yaml"

#: 封闭键集合。加一个键要同时加一个消费者，否则面板上就是一件摆设（那条规则由判据量，不是注释）。
#: `auto_accept_low_risk`（2026-10-28）：低风险自动验收的总开关。`null` 表示"跟随部署期环境变量
#: `ACP_AUTO_ACCEPT_LOW_RISK`"，布尔表示"人在这里显式定过"。消费者 =
#: `config/auto_approval.py::effective_switch()`（那条优先级只在那里实现一次）。
#: `artifact_floors`（2026-10-30）：产物门槛（字节 / 行）按格式一张表。消费者 =
#: `orchestrate/llm_split.py`；此前 `512/12` 等数写死在拆单器里，改一次要发一次版。
RUNTIME_KEYS: tuple[str, ...] = ("max_tokens", "fragment_merge_ms", "auto_accept_low_risk", "artifact_floors")

#: 产物门槛的出厂值。**单一来源**：`runtime.yaml` 缺这一项时按缺键拒，不回落这里；
#: 这里只服务两件事 —— `reporting/cards.py` 的系统卡默认下限，与测试里构造 `RuntimeParams`。
#: 值与原 `llm_split._FLOORS` 逐格相同（json 是 120/5，不是 512/12）。
DEFAULT_ARTIFACT_FLOORS: dict[str, tuple[int, int]] = {
    "html": (512, 12),
    "css": (512, 12),
    "py": (400, 10),
    "js": (400, 10),
    "md": (200, 6),
    "json": (120, 5),
}

#: 格式域的唯一来源是 `protocol/refs.py::ArtifactFormat`；这里只把 `Literal` 拆成集合用于校形。
ARTIFACT_FORMATS = frozenset(get_args(ArtifactFormat))

#: 合并等待的下界：见模块头第 4 条。
MIN_FRAGMENT_MERGE_MS = 1

#: `max_tokens` 的上界取一个"明显超过任何现役窗口"的数，为的是把"写成字符串/浮点"这类错形
#: 变成可读的报错，而不是让一个 1e18 的数安静地进了报文。
MAX_TOKENS_BOUND = 10_000_000


class RuntimeParamsError(ValueError):
    """参数文件读不出、解析失败、有表外键、缺键或值不成形。失败关闭，不返回半份参数。"""


@dataclass(frozen=True)
class RuntimeParams:
    """一份解析结果。`source_sha256` 回答的是"当时是那一版**文件**"（口径同 danger_list）。"""

    max_tokens: int | None
    fragment_merge_ms: int
    #: `None` = 跟随环境变量（`ACP_AUTO_ACCEPT_LOW_RISK`）；布尔 = 面板上显式定过的值。
    #: 三态而不是两态，是因为"没人表过态"与"有人明确关掉了"必须能分开 —— 前者才该读环境默认。
    auto_accept_low_risk: bool | None
    source_sha256: str
    #: 格式 → `(min_bytes, min_lines)`。默认值只服务测试/系统卡；生产必须从 YAML 显式读到。
    artifact_floors: Mapping[str, tuple[int, int]] = field(
        default_factory=lambda: dict(DEFAULT_ARTIFACT_FLOORS)
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "fragment_merge_ms": self.fragment_merge_ms,
            "auto_accept_low_risk": self.auto_accept_low_risk,
            "artifact_floors": {name: list(pair) for name, pair in self.artifact_floors.items()},
        }


def _checked_floors(value: Any) -> dict[str, tuple[int, int]]:
    """`artifact_floors` 的校形：六个格式一个不许缺、值必须是两个正整数。

    缺一个格式不能回落成 `(512, 12)`：那会让"配置漏了"表现成"一个安静的默认值"，
    而默认值恰好会让 300B 的 js 被判不达标（或反之放过一个占位页）。失败关闭。
    """
    if not isinstance(value, Mapping):
        raise RuntimeParamsError(f"`artifact_floors` 必须是 格式→[字节, 行数] 的映射（收到 {type(value).__name__}）")
    unknown = sorted(set(value) - ARTIFACT_FORMATS)
    if unknown:
        raise RuntimeParamsError(f"`artifact_floors` 有域外格式：{unknown} ⇒ 只接受 {sorted(ARTIFACT_FORMATS)}")
    missing = sorted(ARTIFACT_FORMATS - set(value))
    if missing:
        raise RuntimeParamsError(f"`artifact_floors` 缺格式：{missing} ⇒ 每类产物都必须有下限，不回落默认值")
    checked: dict[str, tuple[int, int]] = {}
    for name in sorted(ARTIFACT_FORMATS):
        pair = value[name]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RuntimeParamsError(
                f"`artifact_floors.{name}` 必须是 [min_bytes, min_lines]（收到 {pair!r}）"
            )
        low_bytes, low_lines = pair
        for label, number in (("min_bytes", low_bytes), ("min_lines", low_lines)):
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise RuntimeParamsError(
                    f"`artifact_floors.{name}.{label}` 必须是正整数（收到 {number!r}）"
                )
        checked[name] = (int(low_bytes), int(low_lines))
    return checked


def _checked(name: str, value: Any) -> Any:
    if name == "artifact_floors":
        return _checked_floors(value)
    if name == "auto_accept_low_risk":
        # `None` 是合法取值（"没人表过态，跟随环境变量"）。`bool` 必须是真的布尔：
        # `"true"` 这种字符串在这里**不猜** —— 猜错方向就是把机器代签悄悄打开。
        if value is None or isinstance(value, bool):
            return value
        raise RuntimeParamsError(
            f"`auto_accept_low_risk` 要么是 true/false、要么是 null（收到 {type(value).__name__}: {value!r}）"
        )
    if name == "max_tokens":
        if value is None:
            return None
        # `bool` 是 `int` 的子类：不单独挡掉，`true` 会被当成 1 收下。
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeParamsError(f"`max_tokens` 要么是整数、要么是 null（收到 {type(value).__name__}）")
        if not 1 <= value <= MAX_TOKENS_BOUND:
            raise RuntimeParamsError(f"`max_tokens` 只接受 1..{MAX_TOKENS_BOUND} 或 null（收到 {value}）")
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeParamsError(f"`fragment_merge_ms` 必须是整数毫秒（收到 {type(value).__name__}）")
    if value < MIN_FRAGMENT_MERGE_MS:
        raise RuntimeParamsError(
            f"`fragment_merge_ms` 必须 ≥ {MIN_FRAGMENT_MERGE_MS}（收到 {value}）"
            "⇒ 0 毫秒的等待会把推流循环变成忙等，而 event_log 除 PK 外没有二级索引"
        )
    return value


def load_runtime_params(path: Path | None = None) -> RuntimeParams:
    """读 `config/runtime.yaml` → 校键、校值 → 交出参数。**每次调用都重新读盘**。

    `path` 只为判据服务（用例要构造"写坏一个值"与"加一个表外键"两种文件）；生产调用方 MUST 不传，
    否则"参数可以指向别处"就成了第二个事实源，面板上写的值与生效的值可能不是同一份。
    """
    target = RUNTIME_PATH if path is None else path
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeParamsError(
            f"运行时参数读不出来：{target}（reason={type(exc).__name__}）。"
            "这里没有'读不到就用内置默认'的回退：那会把'配置丢了'读成'配置没改'"
        ) from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML 的正文会带出错行原文，而参数文件是可被人粘贴的自由文本 ⇒ 过一遍脱敏，
        # 并 `from None` 断掉因果链里那份未洗的原始异常（口径同 `load_danger_list`）。
        raise RuntimeParamsError(redact_bus_text(f"{target.name} 解析失败 {type(exc).__name__}: {exc}")) from None
    if not isinstance(raw, dict):
        raise RuntimeParamsError(f"{target.name} 顶层必须是映射（收到 {type(raw).__name__}）")
    unknown = sorted(set(raw) - set(RUNTIME_KEYS))
    if unknown:
        raise RuntimeParamsError(f"{target.name} 有表外键：{unknown} ⇒ 表外键是第二份参数的开始，不是扩充")
    # 先校**已出现**的键的值，再报缺键：这样"写了 max_tokens: true 但漏了别的键"得到的是
    # 那条值的具体错误，而不是一句笼统的"缺必填键"（判据逐条钉这两种消息）。
    values = {name: _checked(name, raw[name]) for name in RUNTIME_KEYS if name in raw}
    missing = sorted(set(RUNTIME_KEYS) - set(raw))
    if missing:
        raise RuntimeParamsError(f"{target.name} 缺必填键：{missing}")
    return RuntimeParams(
        max_tokens=values["max_tokens"],
        fragment_merge_ms=values["fragment_merge_ms"],
        auto_accept_low_risk=values["auto_accept_low_risk"],
        artifact_floors=values["artifact_floors"],
        source_sha256="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def dump_runtime_text(params: RuntimeParams) -> str:
    """把参数写回那份 YAML 的**文本形状**（面板保存走这一支，注释行由调用方补）。

    刻意不是"就地改一行"：键序、空白写法与注释在逐字替换里最容易把文件改坏，
    而这份文件只有一组封闭键，整段重写是更小的一面（保存方 MUST 过 `write_runtime_params`，那里才有原子性）。
    """
    header = (
        "# 运行时可调参数（MVP-2 参数面板的载体；唯一事实源 = config/params-ref.md §12）\n"
        "# 键是封闭集合，加一个键要同时加一个消费者。改完即生效（加载器不缓存）。\n"
    )
    return header + yaml.safe_dump(params.as_dict(), sort_keys=False, allow_unicode=True)


def write_runtime_params(params: RuntimeParams, path: Path | None = None) -> str:
    """原子写回并回读校验（"写进去了"这件事以再读一遍为准，不以函数返回为准）。"""
    import os
    import tempfile

    target = RUNTIME_PATH if path is None else path
    text = dump_runtime_text(params)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".runtime.", suffix=".yaml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    reread = load_runtime_params(target)
    if reread.as_dict() != params.as_dict():
        raise RuntimeParamsError(f"写回后读回的值与送进来的不等：{reread.as_dict()} != {params.as_dict()}")
    return reread.source_sha256
