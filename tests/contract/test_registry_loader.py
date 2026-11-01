"""T039：注册表的载入、校验与"C4 转 PASS"（FR-011 / FR-012｜R-01，`tests/contract/` ⇒ fast 档）。

分档由目录决定（`tests/conftest.py`），本文件不需要装饰器。

**这批用例里最要紧的一条不是"能读出来"，而是"读的时候不 import 适配器"**
（`test_load_never_imports_the_declared_entries`）：FR-012 的"删掉一个适配器目录后编排图仍可构建"
只要载入阶段导一次 transport 就作废了。其余各条拒例都用**合成的临时文件**跑，
为的是让"缺项 / 重复 / 未知键 / 入口形态"这四类拒收各自有可复跑的取证入口，而不是只在文档里说会拒。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest
import yaml

import acp.registry.loader as registry_loader
from acp import selfcheck
from acp.bus.layout import workspace_dir_name
from acp.protocol import CapabilityManifest
from acp.protocol.refs import CostBand
from acp.registry.loader import (
    DEFAULT_REGISTRY_PATH,
    FR011_FIELDS,
    KNOWN_FIELDS,
    Registration,
    RegistryError,
    ValueDomain,
    load_registrations,
    load_value_domain,
)

FR011_KEYS = frozenset(FR011_FIELDS)

#: §9 的取值域（现跑读出来的，不在用例里另抄一份清单）——下面的"漂移"断言用它做唯一比较对象。
DOMAIN = load_value_domain()

#: 一条合法记录的模板。合成用例在它上面**每次只改一处**，改完写进 tmp_path。
#: `tier` / `cost_band` 写的是 `config/params-ref.md §9` 的**档位名**（owner 裁决 1），
#: 线上值由 loader 映射得出 ⇒ 模板里出现裸数字或线上值本身，正是接下来两条用例要拒的形态。
TEMPLATE: dict = {
    "agent_id": "fakeagent",
    "form": "SDK",
    "capabilities": ["frontend_coding"],
    "tier": "advanced",
    "cost_band": "mid",
    "max_parallel": 1,
    "role_mounts": ["developer"],
    "adapter_entry": "acp.adapters.fakeagent:Transport",
    "enabled": True,
}


def write_registry(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(list(entries), sort_keys=False), encoding="utf-8")
    return path


def mutated(**changes) -> dict:
    entry = dict(TEMPLATE)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def test_default_registry_covers_three_forms_with_three_units():
    """SC-001 的"形态各 ≥1"落在注册表上：三支、三形态、一一对应到三个厂商适配器入口。"""
    registrations = load_registrations()
    forms = {r.manifest.form for r in registrations}
    assert forms == {"IN_PLATFORM", "SDK", "API"}, forms
    assert len(registrations) == len({r.agent_id for r in registrations})
    for registration in registrations:
        assert registration.adapter_entry.startswith("acp.adapters."), registration.adapter_entry
        if registration.agent_id == "deepseek-reviewer":
            # Phase 17 P3：审查身份是标记类，不是执行 transport；其余三支仍必须是 `:Transport`。
            assert registration.adapter_entry.endswith(":ReviewEntry"), registration.adapter_entry
        else:
            assert registration.adapter_entry.endswith(":Transport"), registration.adapter_entry


def test_every_declared_unit_is_a_valid_capability_manifest():
    """七字段全必填这条不在 loader 里重述，靠 `CapabilityManifest` 自己拒（一事实一载体）。"""
    for registration in load_registrations():
        manifest = registration.manifest
        assert isinstance(manifest, CapabilityManifest)
        assert manifest.agent_id == registration.agent_id
        assert manifest.capabilities and manifest.max_parallel > 0
        assert CapabilityManifest.model_validate(manifest.model_dump()) == manifest


def test_declared_agent_ids_are_the_bus_workspace_owners():
    """FR-013 的归属比对靠目录名反解 agent_id ⇒ 注册表的 id 集必须能被总线布局命名规则接住。"""
    for registration in load_registrations():
        directory = workspace_dir_name(registration.agent_id)
        assert directory == f"{registration.agent_id}-workspace"


def test_selfcheck_c4_turns_pass_after_the_registry_lands():
    """owner 21:5x 的第三条关注点：C4 从 `BLOCKED` 转 `PASS`，读的就是上面那份默认路径。"""
    status, evidence = selfcheck.check_registry()
    assert status == selfcheck.PASS, evidence
    assert DEFAULT_REGISTRY_PATH.exists()
    assert "forms=" in evidence and "IN_PLATFORM" in evidence, evidence


def test_default_registry_is_the_same_file_selfcheck_reads():
    """两个消费者读同一个形状：`safe_load` 的顶层清单与 loader 的入册数必须相等。"""
    entries = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(entries, list), type(entries)
    assert len(entries) == len(load_registrations())


@pytest.mark.parametrize(
    "missing",
    ["capabilities", "tier", "cost_band", "max_parallel", "agent_id", "form", "role_mounts"],
)
def test_missing_required_key_is_refused_and_named(tmp_path, missing):
    """FR-011 的硬要：缺任一项 ⇒ 注册失败，消息点名缺的是哪一项（不是笼统"校验失败"）。

    两类键分开断言才有信息量：`capabilities` / `tier` / `cost_band` / `max_parallel` 这四项缺时
    消息 MUST 带 `FR-011`（那是"缺任一项 ⇒ 注册失败"这条规格强制的可追溯凭据）；
    另外三项（`agent_id` / `form` / `role_mounts`）缺时 MUST NOT 带 —— 每条错误都贴规格标签
    就等于没贴，读的人分不出哪条是 FR 强制的、哪条只是结构要求。
    """
    path = write_registry(tmp_path, mutated(**{missing: None}))
    with pytest.raises(RegistryError) as caught:
        load_registrations(path)
    message = str(caught.value)
    assert missing in message, message
    if missing in FR011_KEYS:
        assert "FR-011" in message, message
    else:
        assert "FR-011" not in message, message


def test_a_credential_shaped_key_is_named_in_the_refusal(tmp_path):
    """未知键被拒时 MUST 把那个键名打出来（"哪一行写错了"要能定位，只报"未知键"等于让人重读整份文件）。"""
    path = write_registry(tmp_path, mutated(secret_id="x"))
    with pytest.raises(RegistryError, match="secret_id"):
        load_registrations(path)


def test_extra_key_is_refused_because_registry_is_not_a_credential_carrier(tmp_path):
    """未知键 ⇒ 整份拒收。多一行 `api_key` 就变成"凭据落文件"，违宪法「二」。"""
    path = write_registry(tmp_path, mutated(api_key="x"))
    with pytest.raises(RegistryError, match="api_key"):
        load_registrations(path)


@pytest.mark.parametrize("form", ["CLI", "GUI", "cli"])
def test_unsupported_form_is_refused_at_construction_time(tmp_path, form):
    """G1 只接三形态，CLI / GUI 本期不接；拒在构造期，不靠运行时 `if form ==` 分支。"""
    path = write_registry(tmp_path, mutated(form=form))
    with pytest.raises(RegistryError, match="form"):
        load_registrations(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("capabilities", []), ("max_parallel", 0), ("cost_band", "FREE"), ("role_mounts", "developer")],
)
def test_value_level_rules_come_from_the_manifest_model(tmp_path, field, value):
    """空集合、0 并发、未知成本档、把清单写成标量 —— 四类都由 pydantic 拒，loader 不重复实现。"""
    path = write_registry(tmp_path, mutated(**{field: value}))
    with pytest.raises(RegistryError, match=field):
        load_registrations(path)


def test_duplicate_agent_id_is_refused(tmp_path):
    """三岗互斥的判定单位必须唯一：两个同 id 条目 ⇒ 拒，并报出是哪两条。

    第二条只能改**合法值**（`tier` 换成 §9 里另一档名）：改成非法形态的话条目在
    `_validate_entry` 就死了，重复检查根本轮不到执行 ⇒ 这条用例会在报"另一个原因"的消息上假过。
    """
    path = write_registry(tmp_path, mutated(), mutated(tier="basic"))
    with pytest.raises(RegistryError, match="重复") as caught:
        load_registrations(path)
    assert "第 1 条" in str(caught.value) and "第 2 条" in str(caught.value), caught.value


@pytest.mark.parametrize("enabled", ["true", 1, None])
def test_enabled_must_be_a_boolean(tmp_path, enabled):
    """`enabled: "true"`（YAML 里最常见的错法）不许被当成启用。"""
    path = write_registry(tmp_path, mutated(enabled=enabled))
    with pytest.raises(RegistryError, match="enabled"):
        load_registrations(path)


@pytest.mark.parametrize(
    "entry",
    [
        "acp.adapters.workbuddy",
        "workbuddy:Transport",
        "acp.transport:Adapter",
        "acp.adapters.:Transport",
        "acp.adapters.workbuddy:",
    ],
    ids=["no-colon", "no-acp-prefix", "wrong-subpackage", "empty-vendor", "empty-class"],
)
def test_adapter_entry_shape_is_enforced(tmp_path, entry):
    """入口必须写成 `acp.adapters.<vendor>:<Class>`；写歪当场拒，不等 resolve() 才发现。"""
    path = write_registry(tmp_path, mutated(adapter_entry=entry))
    with pytest.raises(RegistryError, match="adapter_entry"):
        load_registrations(path)


def test_agent_id_must_match_the_bus_naming_rule(tmp_path):
    """`Workbuddy` 与 `workbuddy` 在总线上是两个写入者 ⇒ 注册表沿用 `layout.AGENT_ID_RE` 的口径。"""
    path = write_registry(tmp_path, mutated(agent_id="Workbuddy"))
    with pytest.raises(RegistryError, match="agent_id"):
        load_registrations(path)


def test_empty_and_non_list_top_levels_are_refused(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("[]\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="为空"):
        load_registrations(empty)
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text("agents: []\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="顶层必须是清单"):
        load_registrations(mapping)


def test_missing_registry_file_is_refused_not_returned_empty(tmp_path):
    """文件不在 ⇒ 报错，不返回空清单。返回空清单会让 C4 那类"数一数形态"的判据读成"配得不全"而不是"没配"。"""
    with pytest.raises(RegistryError, match="不存在"):
        load_registrations(tmp_path / "nope.yaml")


def test_malformed_yaml_is_refused_with_a_named_source(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("- agent_id: a\n   form: SDK\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="broken.yaml"):
        load_registrations(broken)


def test_load_never_imports_the_declared_entries(tmp_path):
    """载入阶段 MUST NOT 碰 transport（FR-012 的"删目录仍可构建"就靠这一条撑着）。"""
    modules = (
        "acp.adapters.workbuddy",
        "acp.adapters.qoder",
        "acp.adapters.doubao",
        "acp.adapters.deepseek",
    )
    for name in modules:
        sys.modules.pop(name, None)
    registrations = load_registrations()
    assert len(registrations) == 4
    for name in modules:
        assert name not in sys.modules, name


def test_resolve_reports_the_missing_entry_loudly(tmp_path):
    """入口写歪到 resolve() 才发现的代价：不静默、不返回 None，报出是哪条哪个入口。"""
    path = write_registry(tmp_path, mutated(adapter_entry="acp.adapters.notthere:Transport"))
    registration = load_registrations(path)[0]
    with pytest.raises(RegistryError, match="notthere"):
        registration.resolve()


def test_resolve_returns_the_declared_class_when_the_module_exists(monkeypatch, tmp_path):
    """正对照：造一个真在 `sys.modules` 里的假 transport，`resolve()` 必须取回那个类本身。"""
    module = ModuleType("acp.adapters.fakeagent")
    marker = type("Transport", (), {})
    module.Transport = marker
    monkeypatch.setitem(sys.modules, "acp.adapters.fakeagent", module)
    path = write_registry(tmp_path, mutated())
    assert load_registrations(path)[0].resolve() is marker


def test_resolve_reports_a_missing_class_and_not_a_silent_none(monkeypatch, tmp_path):
    """模块在、类名写错 ⇒ MUST 抛，不许返回 `None`。

    这一条是被自己的变异对照逼出来的：`M11` 把 `getattr(module, class_name)` 改成
    `getattr(module, class_name, None)` 之后，原先那两条 resolve 用例**全绿**（它们只覆盖了
    "模块不存在"那一支）。返回 `None` 的下游形态是派发路径上一句 `TypeError: 'NoneType' is not iterable`，
    既没有入口名也没有 agent_id ⇒ 排查成本比一句明确的 `RegistryError` 高一个量级。
    """
    module = ModuleType("acp.adapters.fakeagent")
    monkeypatch.setitem(sys.modules, "acp.adapters.fakeagent", module)
    path = write_registry(tmp_path, mutated(adapter_entry="acp.adapters.fakeagent:NotThere"))
    registration = load_registrations(path)[0]
    with pytest.raises(RegistryError, match="NotThere"):
        registration.resolve()


def test_registry_only_fields_do_not_leak_into_the_manifest():
    """`adapter_entry` / `enabled` 是注册表面，跨 Agent 消息里没有它们（否则会进 EN-05 投影）。"""
    fields = set(CapabilityManifest.model_fields)
    assert {"adapter_entry", "enabled"} & fields == set()
    assert KNOWN_FIELDS == fields | {"adapter_entry", "enabled"}
    assert isinstance(load_registrations()[0], Registration)


# —— 四、取值域来自 params-ref §9（owner 2026-10-13 裁决 1）——


def test_section_9_is_the_domain_and_its_wire_values_match_the_contract_enum():
    """§9 是取值域的唯一来源；它的 `cost_band` 线上值 MUST 与契约 §3 / PG enum 的取值域同集合。

    这是**关系式**而不是清单：§9 加一档、或把某档改成契约外的拼写 ⇒ 本条红（而不是靠人记着同步）。
    `tier` 侧同理取"关系式"：三档映射到的整数必须互不相同（同值就等于两档合并，选人次级会失效），
    但不锁具体数字（owner 改数不动结构）。
    """
    assert set(DOMAIN.band_by_name.values()) == set(get_args(CostBand)), DOMAIN.band_by_name
    assert len(set(DOMAIN.tier_by_name.values())) == len(DOMAIN.tier_by_name), DOMAIN.tier_by_name
    assert all(isinstance(value, int) for value in DOMAIN.tier_by_name.values())
    assert DOMAIN.tier_by_name and DOMAIN.band_by_name


def test_registry_names_are_mapped_to_wire_values(tmp_path):
    """注册表写档位名 ⇒ 交出的 `CapabilityManifest` 里是 §9 映射后的线上值。"""
    entries = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    registrations = load_registrations()
    assert len(entries) == len(registrations)
    for raw, registration in zip(entries, registrations, strict=True):
        assert registration.manifest.tier == DOMAIN.tier_by_name[raw["tier"]]
        assert registration.manifest.cost_band == DOMAIN.band_by_name[raw["cost_band"]]


def test_bare_number_tier_is_refused(tmp_path):
    """`tier: 3` 这类"直接写线上值 / 裸数字"被拒 —— 这正是裁决 1 要挡的 `tier=1.5` 形态。"""
    for bad in (3, 1.5, "3"):
        path = write_registry(tmp_path, mutated(tier=bad))
        with pytest.raises(RegistryError, match="tier"):
            load_registrations(path)


def test_wire_value_used_as_name_is_refused(tmp_path):
    """`cost_band` 写线上值本身（而不是 §9 的名字）也拒：一档两种写法迟早分叉。"""
    for value in DOMAIN.band_by_name.values():
        path = write_registry(tmp_path, mutated(cost_band=value))
        with pytest.raises(RegistryError, match="cost_band"):
            load_registrations(path)


def test_unknown_tier_name_is_refused_and_reports_the_domain_it_read(tmp_path):
    """表外的名字要带出"本次从 §9 读到的名单"，否则报错的人只能猜门禁认什么。"""
    path = write_registry(tmp_path, mutated(tier="elite"))
    with pytest.raises(RegistryError) as caught:
        load_registrations(path)
    message = str(caught.value)
    assert "elite" in message
    for name in DOMAIN.tier_by_name:
        assert name in message, message


def test_injected_domain_proves_the_loader_reads_it_and_not_a_hardcoded_table(tmp_path):
    """把 §9 换成一份**完全不同的**名单：注册表里的原名必须被拒 ⇒ 证明取值域真从外部读。

    反向也要成立（否则"永远报错"也算过这条）：用合成域里有的名字时，必须映射出合成域的值。
    """
    synthetic = ValueDomain(tier_by_name={"onlyTier": 99}, band_by_name={"onlyBand": "MID"})
    with pytest.raises(RegistryError, match="tier"):
        load_registrations(write_registry(tmp_path, mutated()), domain=synthetic)
    ok = load_registrations(
        write_registry(tmp_path, mutated(tier="onlyTier", cost_band="onlyBand")), domain=synthetic
    )
    assert ok[0].manifest.tier == 99
    assert ok[0].manifest.cost_band == "MID"


def test_loader_source_does_not_inline_the_domain():
    """裁决 1 的"唯一来源"要有反面：**§9 的档位名与 `cost_band` 的线上值都不许出现在 loader 源码里**。

    抄一份进代码 = 两处清单，改 §9 不红 ⇒ 门禁失去意义（与 params-ref §8 那条
    `test_dependency_list_has_only_one_source` 同一条程序）。

    **覆盖面限制（明写，免得读成"代码里没有任何档位痕迹"）**：`tier` 的线上值是单个整数，
    而 `1` / `2` / `3` 在任何源码里都会以 `parents[3]`、`enumerate(..., 1)` 这类形态命中 ⇒
    把它列入子串判据是**一条恒红的判据**，和恒绿的一样没用。"没抄数字"由
    `test_injected_domain_proves_the_loader_reads_it_and_not_a_hardcoded_table` 从**行为**侧钉。
    """
    source = Path(registry_loader.__file__).read_text(encoding="utf-8")
    tokens = [*DOMAIN.tier_by_name, *DOMAIN.band_by_name, *DOMAIN.band_by_name.values()]
    offenders = [token for token in tokens if str(token) in source]
    assert offenders == [], offenders
    assert re.search(r"\b(?:basic|standard|advanced|cheap|expensive)\b", source) is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("no-section", "找不到"),
        ("no-fence", "围栏数 = 0"),
        ("two-fences", "围栏数 = 2"),
        ("missing-tier-key", "期望恰好"),
        ("extra-key-in-fence", "期望恰好"),
        ("broken-yaml", "YAML 解析失败"),
    ],
    ids=[
        "no-section",
        "no-fence",
        "two-fences",
        "missing-tier-key",
        "extra-key-in-fence",
        "broken-yaml",
    ],
)
def test_value_domain_fails_closed_when_section_9_is_unreadable(tmp_path, mode, expected):
    """六种"§9 读不出"的形态 MUST 报错，而不是静默改用内联兜底。

    `extra-key-in-fence` 是这一组里唯一**不减少信息**的一条：往围栏里加一个键，解析器不消费它 ⇒
    宽松版（只查 `<=`）会照常返回域，而那份多出来的清单改了而行为不变。这正是要挡的形态。
    """
    path = tmp_path / "params-ref.md"
    path.write_text(mutated_params_ref(mode), encoding="utf-8")
    with pytest.raises(RegistryError, match=expected):
        load_value_domain(path)


def mutated_params_ref(mode: str) -> str:
    """在 §9 上做**单点**破坏（其余部分逐字保留），返回整份文本。"""
    text = registry_loader.PARAMS_REF.read_text(encoding="utf-8")
    head, marker, tail = text.partition(registry_loader.VALUE_DOMAIN_SECTION)
    if mode == "no-section":
        return text.replace(registry_loader.VALUE_DOMAIN_SECTION, "## 9. 已删除", 1)
    if mode == "no-fence":
        return head + marker + re.sub(r"```yaml\n.*?```", "", tail, flags=re.S)
    if mode == "two-fences":
        return head + marker + "\n```yaml\ntier:\n  extraName: 7\n```\n" + tail
    if mode == "missing-tier-key":
        mutated = re.sub(r"\ntier:\n(?:  \S.*\n)+", "\n", tail, count=1)
        assert mutated != tail, "破坏没生效 ⇒ 这条对照是空的"
        return head + marker + mutated
    if mode == "extra-key-in-fence":
        mutated = tail.replace("cost_band:\n", "extraKey: 1\ncost_band:\n", 1)
        assert mutated != tail, "破坏没生效 ⇒ 这条对照是空的"
        return head + marker + mutated
    if mode == "broken-yaml":
        mutated = tail.replace("advanced: 3", "advanced: ]", 1)
        assert mutated != tail, "破坏没生效 ⇒ 这条对照是空的"
        return head + marker + mutated
    raise AssertionError(f"未知的破坏形态：{mode}")


def test_value_domain_parser_is_not_vacuous():
    """上一条的对照组：拿**真文件**跑同一个解析器，必须正常出映射表（不是恒抛）。"""
    assert DOMAIN.tier_by_name == load_value_domain(registry_loader.PARAMS_REF).tier_by_name
    assert DOMAIN.band_by_name == load_value_domain().band_by_name


def test_value_domain_section_scan_is_bounded_by_the_next_heading(tmp_path):
    """§9 的扫描范围必须止于下一个 `## ` 标题：否则将来 §10 带个 yaml 围栏就会被算成"两份取值域"。

    这条是**反向对照**（不该红）：在 §9 之后追加一节、里面放一个合法围栏 ⇒ 本节仍只读到一个围栏，
    域照旧解析出来。把 `text[start:]` 改成扫到文末，本条就红。
    """
    text = registry_loader.PARAMS_REF.read_text(encoding="utf-8")
    path = tmp_path / "params-ref.md"
    path.write_text(
        text + "\n\n## 10. 将来的某一节\n\n```yaml\nsomeOtherList:\n  a: 1\n```\n", encoding="utf-8"
    )
    assert load_value_domain(path).tier_by_name == DOMAIN.tier_by_name


def test_section_9_points_at_section_3_for_the_concurrency_default():
    """owner 2026-10-13 裁决 1 的追加：正文那个 §3 指针要能被复算，不能只是文档里的一句话。

    锚点是**字面串** `params-ref §3`，全文件命中数 MUST = 1：
    0 ⇒ 指针被改写或删掉（正文那三句就重新变成"改了没人知道"）；
    2 ⇒ 有人把默认并发那个数抄了第二份（正是他裁决 1 第一条理由要挡的事）。
    文档里给的 grep 用方括号写法，是为了让那条命令行自身不被它要数的串命中 ⇒ 两侧数才相等。
    """
    text = registry_loader.PARAMS_REF.read_text(encoding="utf-8")
    assert text.count("params-ref §3") == 1, text.count("params-ref §3")
    # 指针必须落在 §9 那一节里（落在别处 = 本节又变成没人指的孤节）
    start = text.index(registry_loader.VALUE_DOMAIN_SECTION)
    assert text.index("params-ref §3") > start


def test_selfcheck_c4_runs_the_loader_and_passes():
    """owner 裁决 2 的正对照：C4 = 文件在 + loader 全条跑通 + 形态 ≥3。

    证据里必须带 `loader 全条通过` —— 旧版那句话是 `entries=3 forms=[...]` 就收尾，
    没有这半句 ⇒ 这半句就是"升级真的接上了"的痕迹（不是只把 C4 的分支数改了改）。
    """
    status, evidence = selfcheck.check_registry()
    assert status == selfcheck.PASS, evidence
    assert "loader 全条通过" in evidence, evidence


def test_selfcheck_c4_turns_fail_when_a_declaration_is_illegal(tmp_path, monkeypatch):
    """裁决 2 的反面：把某条 `tier` 改成裸数字 ⇒ C4 必须转 **FAIL**（旧版这里是 PASS，因为它只数形态）。

    用临时目录顶掉 `selfcheck.ROOT`，不动工作树里那份注册表。
    形态一栏也一起验：`BLOCKED` 只留给"文件不存在"，读不出来的合法路径不许算缺输入。
    挑 `3` 而不是 `1.5`：3 恰是 §9 里 `advanced` 的**合法线上值** ⇒ "值对而面错"是最容易被放过的形态，
    任何"顺手接受整数"的改动都会让本条红（`test_bare_number_tier_is_refused` 里 1.5 那支继续管乱数）。
    """
    broken = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    broken[0]["tier"] = 3
    registry_dir = tmp_path / "src" / "acp" / "registry"
    registry_dir.mkdir(parents=True)
    (registry_dir / "agents.yaml").write_text(
        yaml.safe_dump(broken, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)
    status, evidence = selfcheck.check_registry()
    assert status == selfcheck.FAIL, evidence
    assert "loader 拒收" in evidence and "tier" in evidence, evidence
