"""`registry/agents.yaml` 的载入与校验（T039｜FR-011 / FR-012，R-01）。

只做三件事：**读清单 → 逐条校验 → 交出入册对象**。刻意不做第四件事：**不在载入时 import 适配器**。

- **为什么不 import**（本模块唯一一处设计决策，代价与收益都写在 `Registration.resolve()` 上）：
  FR-012 说"新工具仅通过登记配置加入，MUST NOT 因此改动协作调度本身"，而 `plan.md` 设计项 6 的判据是
  **删掉某个适配器目录后编排图仍可构建**（契约 §5 最后一行、`tasks.md` 的 Phase 3 Independent Test）。
  载入时导入 = 那份配置把它的目录变成了硬依赖，删目录必然连累编排图 ⇒ 入口字符串在这里只做
  **形状校验**，真正解析留给 `resolve()`，由装配点（T123）与派发路径（Phase 6）在需要时才调。
  代价点名：入口里的类名写错，**载入不报**、要到 `resolve()` 才炸。这一条已在用例里钉住
  （`test_load_never_imports_and_resolve_reports_the_missing_entry`），不是没人管的状态。
- **为什么顶层是清单而不是带元数据的映射**：`acp/selfcheck.py` 的 C4 与本模块读**同一个形状**
  （C4 直接迭代 `yaml.safe_load(...)` 的结果）。改成 `{agents: [...]}` 就得同时改 C4 那段代码，
  而 C4 的形状属 owner 裁过的 Checkpoint 口径 ⇒ 不在这里顺手改；要加 schema 版本位时两侧一起动。
- **FR-011 的"四项必填"在哪强制**：不在本文件另写一遍校验，而是**构造 `CapabilityManifest` 时由
  pydantic 拒**（契约 §3：七字段全必填、`capabilities` 非空、`max_parallel > 0`）。本模块只补两件
  pydantic 管不到的事：**未知键拒**（防凭据 / 端点混进注册表）与 **`agent_id` 重复拒**
  （三岗互斥的判定单位必须唯一，FR-031）。
- **`agent_id` 的形态口径不在这里重述**：直接用 `acp.bus.layout.AGENT_ID_RE`（`:35`）——
  那个正则的存在理由是"目录名要能反解出 agent_id 才能做归属比对"，注册表与总线必须同一口径，
  写第二份就会分叉出"注册能过、归属比对认不出"的那类缺陷。

凭据纪律（宪法「二」）在本模块的落点：`agents.yaml` 里出现任何凭据键名形态都属未知键 ⇒ 整份拒收；
本模块也**不读环境变量**，所以"注册表里藏着凭据"这条路径不成立。
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from acp.bus.layout import AGENT_ID_RE
from acp.protocol import CapabilityManifest

#: 仓库根 = `src/acp/registry/loader.py` 往上三层（`config/params-ref.md` 的落点口径）。
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 注册表的默认位置 = `selfcheck` 的 C4 读的那一份（同一个路径常量口径，不各处拼一遍）。
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("agents.yaml")

#: pydantic 那份模型的字段 = 注册表必须申报的字段（一事实一载体：清单从模型推导，不手抄）。
MANIFEST_FIELDS = frozenset(CapabilityManifest.model_fields)

#: 注册表独有、不属于跨 Agent 消息的两项。
REGISTRY_ONLY_FIELDS = frozenset({"adapter_entry", "enabled"})

KNOWN_FIELDS = MANIFEST_FIELDS | REGISTRY_ONLY_FIELDS

#: `模块路径:类名`，模块必须是 `acp.adapters.<vendor>`（厂商专有代码唯一的落点）。
ENTRY_RE = re.compile(r"^acp\.adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$")

#: 报错时要点名的四项 = FR-011 的"能做什么 / 能力档次 / 成本档位 / 可并行数"。
FR011_FIELDS = ("capabilities", "tier", "cost_band", "max_parallel")

#: `tier` / `cost_band` 的取值域**不在这里内联**（owner 2026-10-13 裁决 1：唯一来源 = `config/params-ref.md §9`）。
#: 本节缺失或读不出 ⇒ 报错，**不退回任何内联兜底**（把"清单没读到"当成通过是最要的假绿，
#: 与 `guard_naming.sh` 读不到 params-ref §7 就退出是同一条程序）。
PARAMS_REF = REPO_ROOT / "config" / "params-ref.md"
VALUE_DOMAIN_SECTION = "## 9. Agent 能力档位与并发申报域"

#: §9 围栏里**必须且只能**有这两张表。写死这两个键名不算内联取值域：键名是解析器的输入形状，
#: 而两侧的具体档位名与它们映射到的线上值，一个都不在这里。多出来的键没人解析
#: ⇒ 那份"清单"改了而行为不变，比缺键更难发现。
VALUE_DOMAIN_KEYS = frozenset({"tier", "cost_band"})


class RegistryError(ValueError):
    """注册表不合法。消息里只出现键名与 `agent_id`，MUST NOT 出现任何凭据值。"""


@dataclass(frozen=True)
class ValueDomain:
    """params-ref §9 那张映射表的运行期形状：注册表侧的档位名 ⇄ 线上值。"""

    tier_by_name: dict[str, int]
    band_by_name: dict[str, str]


def load_value_domain(params_ref: Path | None = None) -> ValueDomain:
    """从 params-ref §9 的 ```yaml 围栏取映射表。只接受**恰好一个**围栏。"""
    path = params_ref or PARAMS_REF
    if not path.exists():
        raise RegistryError(f"取值域载体不存在：{path}（§9 是 tier / cost_band 的唯一来源）")
    text = path.read_text(encoding="utf-8")
    start = text.find(VALUE_DOMAIN_SECTION)
    if start < 0:
        raise RegistryError(f"{path.name} 里找不到「{VALUE_DOMAIN_SECTION}」这一节 ⇒ 取值域没有来源")
    # 本节范围到**下一个 `## ` 标题**为止，不扫到文末：将来 §10 若带一个 yaml 围栏，
    # "恰好一个围栏"的读数会把它算进来 ⇒ 报出"两份取值域"这种假账，而真两份时反倒分不清。
    rest = text[start + len(VALUE_DOMAIN_SECTION):]
    boundary = re.search(r"^## ", rest, re.M)
    section = rest[: boundary.start()] if boundary else rest
    fences = re.findall(r"```yaml\n(.*?)```", section, re.S)
    if len(fences) != 1:
        raise RegistryError(
            f"§9 的 yaml 围栏数 = {len(fences)}，期望 1（两份取值域就没有唯一来源可言）"
        )
    try:
        raw = yaml.safe_load(fences[0])
    except yaml.YAMLError as exc:
        raise RegistryError(f"§9 的围栏 YAML 解析失败 {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError(f"§9 的围栏必须是映射，收到 {type(raw).__name__}")
    if set(raw) != VALUE_DOMAIN_KEYS:
        raise RegistryError(
            f"§9 的围栏的键 = {sorted(raw)}，期望恰好 {sorted(VALUE_DOMAIN_KEYS)}"
            "（缺 = 取值域没有来源；多 = 围栏里躺着一份没人解析的第二清单）"
        )
    tier = raw["tier"]
    band = raw["cost_band"]
    if not isinstance(tier, dict) or not tier or not all(isinstance(v, int) for v in tier.values()):
        raise RegistryError("§9 的 `tier` 必须是「档位名: 整数」的非空映射")
    if not isinstance(band, dict) or not band or not all(isinstance(v, str) for v in band.values()):
        raise RegistryError("§9 的 `cost_band` 必须是「档位名: 线上值」的非空映射")
    return ValueDomain(tier_by_name=dict(tier), band_by_name=dict(band))


@dataclass(frozen=True)
class Registration:
    """一条在册单元：能力申报单 + 适配器入口 + 启用位。"""

    manifest: CapabilityManifest
    adapter_entry: str
    enabled: bool

    @property
    def agent_id(self) -> str:
        return self.manifest.agent_id

    def resolve(self) -> type:
        """把入口字符串解析成类 —— **只有调用它才会 import**（`ImportError` 在这里才可能发生）。"""
        module_name, _, class_name = self.adapter_entry.partition(":")
        try:
            return getattr(importlib.import_module(module_name), class_name)
        except Exception as exc:
            raise RegistryError(
                f"{self.agent_id} 的适配器入口 {self.adapter_entry} 解析失败："
                f"{type(exc).__name__}: {exc}"
            ) from exc


def _describe(index: int, raw: object) -> str:
    if isinstance(raw, dict):
        return f"第 {index} 条（agent_id={raw.get('agent_id', '?')}）"
    return f"第 {index} 条（{type(raw).__name__}）"


def _map_name(
    field: str, declared: object, table: dict[str, object], label: str, source: str
) -> object:
    """注册表侧写 §9 的**档位名**，这里映射成线上值；写数字 / 写表外名一律拒。

    允许名单是 `table` 的键（运行时从 params-ref 读出来的），源码里没有第二份 ——
    `tests/contract/test_registry_loader.py` 双向钉它：loader 文本里档位名命中 0、
    而 §9 那节被删掉时报错而非静默改用别的清单。
    """
    if isinstance(declared, str) and declared in table:
        return table[declared]
    kind = "必须是 §9 的档位名" if isinstance(declared, str) else "必须是 §9 的档位名字符串"
    if not isinstance(declared, str):
        raise RegistryError(
            f"{source} {label}：`{field}` {kind}，收到 {type(declared).__name__} {declared!r}"
            f" —— 数字档位正是本道映射要挡掉的形态（见 {VALUE_DOMAIN_SECTION}）"
        )
    raise RegistryError(
        f"{source} {label}：`{field}` 档位名 {declared!r} 不在 §9 的映射里"
        f"（本次从 params-ref 读到的名单 = {sorted(table)}）"
    )


def _validate_entry(index: int, raw: object, source: str, domain: ValueDomain) -> Registration:
    label = _describe(index, raw)
    if not isinstance(raw, dict):
        raise RegistryError(f"{source} {label}：条目必须是映射，收到 {type(raw).__name__}")
    keys = set(raw)
    unknown = sorted(keys - KNOWN_FIELDS)
    if unknown:
        raise RegistryError(
            f"{source} {label}：未知键 {unknown}。注册表只认 {sorted(KNOWN_FIELDS)}；"
            "端点与凭据不在这一层（凭据只从环境变量来，宪法「二」）"
        )
    missing = sorted(KNOWN_FIELDS - keys)
    if missing:
        named = [field for field in FR011_FIELDS if field in missing]
        tail = f"；其中 FR-011 的四项缺：{named}" if named else ""
        raise RegistryError(f"{source} {label}：缺必填键 {missing}{tail}")
    agent_id = raw["agent_id"]
    if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id):
        raise RegistryError(
            f"{source} {label}：`agent_id` 形态非法（要匹配总线口径 {AGENT_ID_RE.pattern}，收到 {agent_id!r}）"
        )
    entry = raw["adapter_entry"]
    if not isinstance(entry, str) or not ENTRY_RE.match(entry):
        raise RegistryError(
            f"{source} {label}：`adapter_entry` 形态非法"
            f"（要 `acp.adapters.<vendor>:<Class>`，收到 {entry!r}）"
        )
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise RegistryError(f"{source} {label}：`enabled` 必须是布尔，收到 {type(enabled).__name__}")
    declared = dict(raw)
    declared["tier"] = _map_name("tier", raw["tier"], domain.tier_by_name, label, source)
    declared["cost_band"] = _map_name("cost_band", raw["cost_band"], domain.band_by_name, label, source)
    try:
        manifest = CapabilityManifest(**{field: declared[field] for field in MANIFEST_FIELDS})
    except ValidationError as exc:
        raise RegistryError(f"{source} {label}：能力申报校验失败：{exc}") from exc
    return Registration(manifest=manifest, adapter_entry=entry, enabled=enabled)


def load_registrations(
    path: Path | None = None,
    *,
    domain: ValueDomain | None = None,
    source_name: str | None = None,
) -> tuple[Registration, ...]:
    """读注册表并逐条校验；**任何一条不合法 ⇒ 整份拒收**（不留"前两条生效、第三条被跳过"的中间态）。"""
    target = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    source = source_name or target.name
    value_domain = domain if domain is not None else load_value_domain()
    if not target.exists():
        raise RegistryError(f"{source}：注册表文件不存在（{target}）")
    try:
        entries = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"{source}：YAML 解析失败 {type(exc).__name__}: {exc}") from exc
    if isinstance(entries, dict):
        raise RegistryError(
            f"{source}：顶层必须是清单（一条一个 Agent），收到映射的键 {sorted(entries)}"
        )
    if not isinstance(entries, list) or not entries:
        raise RegistryError(f"{source}：清单为空或不是清单（收到 {type(entries).__name__}）")
    registrations = tuple(
        _validate_entry(index, raw, source, value_domain) for index, raw in enumerate(entries, 1)
    )
    seen: dict[str, int] = {}
    for position, registration in enumerate(registrations, 1):
        agent_id = registration.agent_id
        if agent_id in seen:
            raise RegistryError(
                f"{source}：`agent_id` {agent_id!r} 重复（第 {seen[agent_id]} 条与第 {position} 条）"
                " ⇒ 三岗互斥的判定单位必须唯一（FR-031）"
            )
        seen[agent_id] = position
    return registrations
