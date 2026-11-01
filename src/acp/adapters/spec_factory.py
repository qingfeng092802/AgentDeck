"""MVP-2 笔 H：厂商 spec 的唯一构造点 = `max_tokens` 的第一处生产侧读点（owner 2026-10-19 裁决 2）。

三支事实在这里汇成一份构造入参，每支都只有它那只载体，本模块一处都不复制：

| 事实 | 唯一载体 | 本模块经谁读 |
|---|---|---|
| 回答长度上限 | `config/runtime.yaml`（`params-ref §12`） | `runtime.load_runtime_params()` |
| 模型 ID | 环境变量 `ACP_DOUBAO_MODEL` | `config.loader.env_value()` |
| 厂商与适配器入口 | `registry/agents.yaml`（EN-03 申报面） | `registry.loader.load_registrations()` |

三条刻意的写法，各有一条判据：

1. **签名里没有 `max_tokens` 这一项**。长度只能从 §12 那份文件来。给它开一个形参，就等于开了
   第二条来源 —— 面板上写的与线上用的会分叉，而分叉出来的那份不报错，只是行为不同。
2. **模型 ID 不留默认值**（`params-ref §4` 明写"实际 Model ID 以方舟控制台为准"）：缺变量或空白
   ⇒ `SpecRefused` 点名变量名。把某个 ID 写成默认值就是把部署事实伪装成代码事实，与 T040 的
   `service`、`DoubaoApiSpec.model` 同一条纪律。
3. **只登记工厂真的认识的那一支**。`SPEC_SHAPES` 的键是注册表入口的模块段；表外厂商一律拒，
   不替它猜一个字段名 —— `QoderCliSpec` / `WorkBuddyApiSpec` 里没有长度这一项（判据拿
   `dataclasses.fields` 量），把它们塞进表里才是说谎。

**今天没有生产调用点，这一点不粉饰**：`run.py` 的执行段只构造 `StubAdapter`，而
`resolve_transport_flag()`（`adapters/stub.py`）只放 `stub` 一个值 —— 理由是 R-07 那句"一次误敲
就是一次真实外部调用"（owner 成本纪律 5）。所以本模块落地后的真实状态是：
`GET /runtime` 的 `consumers["max_tokens"]` 从 `[]` 变成 `["adapters/spec_factory.py"]`，说的是
**"这一段就是它生效的代码"**，不是"改完就已经有线上效果"。生效的前提是把真跑那一发的构造点接到
这里（登记为待裁 W-8，`tasks.md` 文档笔一并回填）。那格缺口由
`tests/guards/test_transport_spec_factory.py::test_the_factory_still_has_no_production_caller` 用
绝对读数钉在代码里：接上的那一天必须改那一格并同时改本段，不许让它静默变绿。
（**笔 P 改口登记**：上面两处点名的判据名字已随接线那两笔改写 —— `test_the_factory_still_has_no_production_caller`
这个旧名不再存在，现在量同一件事的是
`tests/guards/test_transport_spec_factory.py::test_the_spec_constructor_still_has_no_outside_caller`
与 `tests/guards/test_realrun_driver.py::test_the_factory_has_exactly_one_production_caller_and_it_is_the_driver`。
本节第 22 行那句"没有生产调用点"到笔 P 起**已经不成立**，由下面那段追记接替，原文按仓规不改写。）

**【2026-10-19 20:5x B 计划 第一步追记，上面那段原样保留】** 上面那格的"没有生产调用点"到今天**仍然成立**
（复算 = `test_the_real_wiring_entry_has_no_production_caller_yet`，随笔 P 改名为
`test_the_spec_constructor_still_has_no_outside_caller`，量的是 `build_transport_spec` 与新增的 `build_real_transport` 两个名字）。变化在于**工厂多了一层**：`build_real_transport()` 造真 transport（owner 明示的两支：`doubao` ⇒ `ARK_API_KEY`、`qoder` ⇒
`QODER_PERSONAL_ACCESS_TOKEN`），并在那一刻**验凭据存在**——这与上面第 22 行那句"凭据在 `submit()` 那一刻由
`env_value` 取"不冲突：工厂只问"有没有非空值"，值不留进构造品，transport 自己发请求时再取一次。
凭据变量名与超时秒数都**不在这里写死**，读的是厂商包的约定属性（`CREDENTIAL_ENV_NAMES` / `HTTP_TIMEOUT_S` /
`PRODUCTION_RUNNER`）；缺任何一个就 `RealWiringRefused`，**没有一条路径回落 stub**。
`acp.run --transport` 那道门（`resolve_transport_flag()`）一格没放宽，仍只放 `stub` —— R-07 那句
"一次误敲就是一次真实外部调用"由 `test_wiring_the_factory_did_not_open_the_run_transport_door` 钉着。

**【同日 23:5x B 计划 第二步追记】** 上面那句"仍然成立"到这一笔**不成立了**：`acp.realrun` 落了真驱动，
`build_real_transport()` 从此有一个生产调用方（`src/acp/realrun.py::real_transport()`，全仓恰那一处）。
两点必须一起读，否则这句会被读成"门也开了"：
① `acp.run --transport` 那道门照旧只放 `stub`（驱动走的是**另一支入口**，不经过 `run.py` 的旗标面，
   判据 = `test_realrun_driver.py::test_the_frozen_segments_still_pass_the_door_and_the_driver_leaves_it_closed`）；
② 驱动里 `FROZEN_SEGMENT_GATE = "stub"` 那个值只交进受理 / 恢复 / 指派这三段**一次厂商请求都不发**的冻结链路，
   厂商调用只在 `realrun._produce()` 那一发，那一发构造的是本模块的真 transport（静态那格 =
   `test_the_driver_constructs_vendor_transports_only_through_the_factory`，行为那格 =
   `test_the_driver_never_touches_a_real_vendor_construction`）。

延迟 import 是本模块唯一一处设计取舍：厂商 spec 类只在真要构造它时才 import（口径同
`registry/loader.py` 的"载入时不 import 适配器"）。于是 `tests/contract/test_all_adapters.py` 那套
"拔掉一支目录，编排图仍可构建"的零耦合证明不会因为这里多了一个模块而变成"整个 adapters 层焊在
方舟上"。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path

from acp.config import runtime
from acp.config.loader import CredentialMissing, env_value
from acp.registry.loader import Registration, load_registrations
from collections.abc import Callable
from typing import cast

#: 模型 ID 的环境变量名。**不是凭据**，但同一条纪律：只从环境来、不落进 `agents.yaml`、不进代码默认值。
#: 名字刻意避开 `params-ref §7` 那条已否决命名（门禁扫 `.py`，连说明都不许写出那个形态）。
MODEL_ENV = "ACP_DOUBAO_MODEL"

#: 工厂能构造的厂商：注册表入口的模块段 →（spec 所在子模块段，spec 类名）。
#: 这一张表是"能力申报"而不是"取值域"：具体字段的语义仍在各厂商的 spec 里，这里只定位类。
SPEC_SHAPES: dict[str, tuple[str, str]] = {"acp.adapters.doubao": ("transport", "DoubaoApiSpec")}


class SpecRefused(RuntimeError):
    """构造不出 spec。消息里只出现 `agent_id`、变量名与键名，MUST NOT 出现任何凭据值。"""


def _require_registration(agent_id: str, registry_path: Path | None) -> Registration:
    """在册、且 `enabled: true` 才给构造。

    "注册表里查不到"与"查到了但关着"是两种不同的失败：前者是清单写错，后者是有人明确关掉了这一支。
    合并成一条消息会让人去改清单。
    """
    registrations = load_registrations(registry_path) if registry_path is not None else load_registrations()
    for registration in registrations:
        if registration.agent_id != agent_id:
            continue
        if not registration.enabled:
            raise SpecRefused(
                f"`{agent_id}` 在注册表里但 `enabled: false` ⇒ 不构造 spec"
                "（关掉的那一支不该因为参数面板里有个数就又被造出来）"
            )
        return registration
    raise SpecRefused(
        f"注册表里没有 `{agent_id}` 这一条 ⇒ 身份不由调用方指定（唯一来源 = "
        "`src/acp/registry/agents.yaml`，FR-012 的「新工具仅通过登记配置加入」）"
    )


def _spec_class(registration: Registration) -> type:
    """按注册表入口定位厂商的 spec 类（表外厂商在这里就拒，不往下猜）。"""
    module_name, _, class_name = registration.adapter_entry.partition(":")
    shape = SPEC_SHAPES.get(module_name)
    if shape is None:
        raise SpecRefused(
            f"`{registration.agent_id}` 的入口 {registration.adapter_entry} 不在工厂能构造的表里"
            f"（表内 = {sorted(SPEC_SHAPES)}）⇒ 拒。它的 spec 里若没有长度这一项，"
            "面板上那个数对这个厂商就不生效，这是事实，不该由工厂编一个字段名来假装生效"
        )
    submodule, spec_name = shape
    return getattr(importlib.import_module(f"{module_name}.{submodule}"), spec_name)


def _require_model(env: Mapping[str, str] | None) -> str:
    try:
        raw = env_value(MODEL_ENV, env)
    except CredentialMissing:
        raise SpecRefused(
            f"{MODEL_ENV} 缺失 ⇒ 模型 ID 没有来源（`params-ref §4`：实际 Model ID 以方舟控制台为准）。"
            "这里不回落任何默认 ID"
        ) from None
    model = raw.strip()
    if not model:
        raise SpecRefused(f"{MODEL_ENV} 是空白值 ⇒ 与缺失同一处置（空白 ID 发出去只会得到厂商的 404）")
    return model


def build_transport_spec(
    agent_id: str,
    *,
    env: Mapping[str, str] | None = None,
    registry_path: Path | None = None,
) -> object:
    """造出那一支厂商的 spec：`model` 从环境来，`max_tokens` 从 §12 那份文件来。

    顺序是有主的：**先认身份（注册表）→ 再确认工厂认识这一支 → 再要部署事实（模型 ID）→ 最后读参数**。
    参数放最后是刻意的：`load_runtime_params()` 会读盘，一个拼错的 `agent_id` 不该以"参数文件坏了"
    的面貌失败（归因错位比报错更贵）。

    返回值只由两份配置决定，本函数不发任何请求、不读任何凭据（`ARK_API_KEY` 不在这里，
    它在 `DoubaoTransport.submit()` 那一刻由 `env_value` 取；把凭据提前到构造点，
    就多出一个"构造即持有密钥"的形态）。
    """
    registration = _require_registration(agent_id, registry_path)
    spec_class = _spec_class(registration)
    model = _require_model(env)
    params = runtime.load_runtime_params()
    return spec_class(model=model, max_tokens=params.max_tokens)


# ----------------------------------------------------------------- 真装配面（B 计划 第一步）

#: 厂商包 MUST 暴露的约定属性名。工厂按名字取，**一个都不在这里写死**：
#: 凭据变量名单与超时秒数各有唯一载体（在厂商模块里，`params-ref §4` 是人读的那一份），
#: 抄进工厂就变成两份会漂的东西 —— 与 `max_tokens` 不留形参是同一条纪律。
CREDENTIAL_NAMES_ATTR = "CREDENTIAL_ENV_NAMES"
HTTP_TIMEOUT_ATTR = "HTTP_TIMEOUT_S"
PRODUCTION_RUNNER_ATTR = "PRODUCTION_RUNNER"
#: 路由驱动的 API 执行器在包上暴露这一个可调用：`spec_from_route(routing, env=...) -> spec`。
#: 有它 ⇒ 带 `model_routing` 时换用同包的 `ROUTED_TRANSPORT`（纯 API 执行适配器）。
ROUTED_SPEC_ATTR = "spec_from_route"
#: 与 `ROUTED_SPEC_ATTR` 配套的执行适配器类名（包级名字，不由工厂写死任何 agent）。
ROUTED_TRANSPORT_ATTR = "RoutedTransport"

#: 工厂今天真能装配的形态（取值域 = `protocol/refs.AgentForm`，读注册表的 `form` 列，不在这里抄名单）。
#: `IN_PLATFORM`（workbuddy）不在内：它的凭据是 CAM 的 `SecretId` / `SecretKey` **一对**再加 TC3-HMAC 签名，
#: 不是"某个环境变量名有没有值"这一问 ⇒ 表外一律拒，不替它猜构造入参。
WIRABLE_FORMS: frozenset[str] = frozenset({"API", "SDK"})


class RealWiringRefused(RuntimeError):
    """真装配造不出实例。消息里只有 `agent_id`、形态词、属性名与环境变量**名**，MUST NOT 有任何值。

    与 `SpecRefused` 分开是有意的：前者是"这份配置我认不出"，后者是"这一支我要真跑但缺了真跑要的东西"。
    两者的处置动作不同 —— 配置认不出该去改清单，缺凭据该去注入环境。
    """


def _adapter_module(registration: Registration) -> object:
    """按注册表入口把厂商**包**导进来（延迟到真要装配这一刻，口径同本模块的 spec 类）。"""
    module_name = registration.adapter_entry.partition(":")[0]
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RealWiringRefused(
            f"`{registration.agent_id}` 的适配器模块 {module_name} 导不进来（{type(exc).__name__}: {exc}）"
            " ⇒ 真装配拒，**不回落到 stub**（owner 2026-10-19：禁止静默降级）"
        ) from exc


def _convention_attr(module: object, name: str, agent_id: str) -> object:
    value = getattr(module, name, None)
    if value is None:
        raise RealWiringRefused(
            f"`{agent_id}` 的适配器包没有暴露约定属性 `{name}` ⇒ 工厂不知道该怎么装它，拒而不是猜"
            f"（要接这一支，请在它的 `__init__` 里重导出 `{name}`，与 doubao / qoder 两支同形）"
        )
    return value


def _str_tuple_attr(module: object, name: str, agent_id: str) -> tuple[str, ...]:
    """约定属性必须是**字符串序列**：凭据变量名清单。

    约定属性是"适配器包重导出一个常量"的写法，类型面只剩 `object`。这里是它唯一的收窄点：
    不是字符串序列就按装配缺陷拒（`RealWiringRefused`），而不是把 `int` 之类转成 `str` 之后
    拿去问环境变量 —— 那会得到一句"凭据缺失"，把配置写错读成没配。
    """
    value = _convention_attr(module, name, agent_id)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise RealWiringRefused(
            f"`{agent_id}` 的约定属性 `{name}` 应该是字符串序列，读到的是 {type(value).__name__}"
            " ⇒ 凭据名清单不可信，拒而不是逐个猜"
        )
    return tuple(item for item in value if isinstance(item, str))


def _number_attr(module: object, name: str, agent_id: str) -> float:
    """约定属性必须是数字（或数字字符串）：HTTP 超时那一格。"""
    value = _convention_attr(module, name, agent_id)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RealWiringRefused(
            f"`{agent_id}` 的约定属性 `{name}` 应该是数字，读到的是 {type(value).__name__}"
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        raise RealWiringRefused(
            f"`{agent_id}` 的约定属性 `{name}` 取不到数字：{value!r}"
        ) from None


def _factory_attr(module: object, name: str, agent_id: str) -> Callable[[], object]:
    """约定属性必须是**可调用**：生产 runner 的类。

    `callable()` 在类型面上不做收窄，所以这里补一次 `cast` —— 它是"检查过再声明"，
    不是"替对方担保"：不可调用时上面已经拒了。
    """
    value = _convention_attr(module, name, agent_id)
    if not callable(value):
        raise RealWiringRefused(
            f"`{agent_id}` 的约定属性 `{name}` 不可调用（读到 {type(value).__name__}）"
            " ⇒ 装配点拿不到 runner 的构造器"
        )
    return cast("Callable[[], object]", value)


def _require_credentials(names: tuple[str, ...], env: Mapping[str, str] | None, agent_id: str) -> None:
    """**任一**名字有非空值即通过，与厂商 transport 自己那道凭据门同一语义（两份规则不许分叉）。

    值只在这一个函数里出现，用途只有"是不是空白"这一问；返回类型是 `None` ⇒ 密钥不外流到构造调用里。
    空白值与缺失同一处置：一个空字符串发出去只会得到厂商的 401，比现在报错更难查。
    """
    blanks: list[str] = []
    for name in names:
        try:
            raw = env_value(name, env)
        except CredentialMissing:
            continue
        if raw.strip():
            return
        blanks.append(name)
    raise RealWiringRefused(
        f"`{agent_id}` 的真跑缺凭据：{' 或 '.join(names)} 至少要有一个有非空值"
        + (f"（空白的那些 = {'、'.join(blanks)}，与缺失同一处置）" if blanks else "")
        + "。凭据只从环境来（宪法「二」），本工厂不接受任何默认值，也不降级成 stub"
    )


def build_real_transport(
    agent_id: str,
    *,
    gateway: object,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    registry_path: Path | None = None,
    routing: Mapping[str, object] | None = None,
) -> object:
    """造出**真** transport（B 计划 第一步，owner 2026-10-19 明示的两支：`doubao` / `qoder`）。

    与 `build_transport_spec()` 的分工：那一发只产 spec（不碰凭据），这一发产实例，
    并且**只验凭据存在、不持有凭据** —— transport 自己在发请求那一刻再取一次（`DoubaoTransport.submit()`
    里那句 `env_value` 就是那一处）。所以构造品里没有密钥字段可被日志打出来。

    顺序仍按"先认身份 → 再认形态 → 再要凭据 → 最后才读盘上的参数"：一个拼错的 `agent_id`
    不该以"参数文件坏了"的面貌失败，而缺凭据必须在任何读盘与任何请求之前失败。

    拒绝的形状各有自己的消息，**没有一条会回落 stub**：
    ① 不在册 / `enabled: false` → `SpecRefused`；② 形态不在 `WIRABLE_FORMS` → `RealWiringRefused`；
    ③ 厂商包没暴露约定属性 → `RealWiringRefused`；④ 一个凭据变量都没有非空值 → `RealWiringRefused`；
    ⑤ 模型 ID / 参数文件那一层的失败仍由 `build_transport_spec` 原样抛（不吞、不改写成"没给就是默认"）；
    ⑥ 子进程形态（SDK）没交 `cwd` → `RealWiringRefused`（owner 2026-10-20 裁决 1）。

    **`cwd` 只与子进程形态有关**：API 那一支不发子进程，所以那一条转发不过去（`DoubaoTransport` 的构造
    签名里没有这一格，硬转就是 `TypeError`）。装配方不管这张卡指派给谁都会算一个 `cwd` 交进来，
    因此 API 那一支是"收到了、不适用、不转发"，而不是"缺了一格"。
    """
    registration = _require_registration(agent_id, registry_path)
    if registration.manifest.form not in WIRABLE_FORMS:
        raise RealWiringRefused(
            f"`{agent_id}` 的形态是 {registration.manifest.form!r}，工厂今天只装 {sorted(WIRABLE_FORMS)}"
            f"（它的入口是 {registration.adapter_entry}）⇒ 拒。"
            "IN_PLATFORM 那一支要的是 CAM 的一对密钥加签名，不是这里的一个变量名"
        )
    module = _adapter_module(registration)
    transport_class = registration.resolve()
    if registration.manifest.form == "API":
        import httpx  # 延迟 import：本模块不因多了这一发而把 httpx 变成构造面的硬依赖

        # 路线 B（2026-10-31）：有 `model_routing` 且这一支的包暴露了 `spec_from_route` ⇒
        # 换成**纯 API 执行适配器**（写代码那条），厂商/模型/key 全由路由决定。
        # 没有路由 ⇒ 原样走后面的静态 spec（doubao 的审查路径一字不改）。
        routed_spec_builder = getattr(module, ROUTED_SPEC_ATTR, None)
        routed_class = getattr(module, ROUTED_TRANSPORT_ATTR, None)
        if routing is not None and routed_spec_builder is not None:
            if routed_class is None:
                raise RealWiringRefused(
                    f"`{agent_id}` 声明了路由 spec 却没有 `{ROUTED_TRANSPORT_ATTR}` ⇒ 不知道该用哪个执行适配器"
                )
            from acp.routing import RoutingSpecError

            try:
                spec = routed_spec_builder(routing, env=env)  # 缺路由厂商的 key / 厂商不认识 ⇒ 拒
            except RoutingSpecError as exc:
                raise SpecRefused(str(exc)) from exc
            return routed_class(
                api=spec,
                http=httpx.Client(timeout=_number_attr(module, HTTP_TIMEOUT_ATTR, agent_id)),
                gateway=gateway,
                env=env,
                agent_id=agent_id,
            )
        names = _str_tuple_attr(module, CREDENTIAL_NAMES_ATTR, agent_id)
        _require_credentials(tuple(names), env, agent_id)
        spec = build_transport_spec(agent_id, env=env, registry_path=registry_path)
        return transport_class(
            api=spec,
            http=httpx.Client(timeout=_number_attr(module, HTTP_TIMEOUT_ATTR, agent_id)),
            gateway=gateway,
            env=env,
        )
    names = _str_tuple_attr(module, CREDENTIAL_NAMES_ATTR, agent_id)
    _require_credentials(tuple(names), env, agent_id)
    runner_class = _factory_attr(module, PRODUCTION_RUNNER_ATTR, agent_id)
    if cwd is None:
        raise RealWiringRefused(
            f"`{agent_id}` 是 {registration.manifest.form} 形态 ⇒ 厂商跑在一个真的子进程里，"
            "必须交出那个进程的家（`cwd`）。缺省不是一个安全值：子进程继承调用方的 cwd，而真跑入口是在"
            "平台仓库根里敲的 ⇒ 没交就等于允许外部执行读写平台自己的源码"
            "（owner 2026-10-20 裁决：必须把它关在它自己的 workspace 里）。"
            "生产那一发交的是 `realrun._confine()` = 存档根（`archive.require_archive_root(project_key)`）里、"
            "`layout.workspace_dir_name(agent_id)` 那一层目录"
        )
    return transport_class(runner=runner_class(), gateway=gateway, env=env, cwd=cwd)

