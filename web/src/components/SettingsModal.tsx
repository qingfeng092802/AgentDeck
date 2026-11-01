import { useEffect, useRef, useState } from "react";
import { HudDropdown } from "./HudDropdown";
import { MODEL_CATALOG, modelsFor, vendorEntry } from "../lib/modelCatalog";
import { THINKING_LEVELS, THINKING_LABEL } from "../lib/thinking";
import {
  cloneSettings,
  getSettings,
  requiredKeyEntries,
  ROLE_KEYS,
  ROLE_TITLE,
  saveSettings,
  UNSET_ROLE,
  type RoleKey,
  type RoleSelection,
  type SettingsState,
} from "../lib/settingsStore";
import { useFocusTrap } from "../lib/focusTrap";
import {
  ACCEPTOR_DEFAULT_LABEL,
  EXECUTOR_KIND_OPTIONS,
  REVIEWER_DEFAULT_LABEL,
  loadRouting,
  routingLabel,
  saveRouting,
  type RoutingChoice,
} from "../lib/routing";
import {
  keyOf,
  loadPool,
  newPoolId,
  rememberKey,
  savePool,
  type PoolModel,
} from "../lib/modelPool";
import { pushToast } from "../lib/toastStore";

export interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
}

/** 四席：统筹 → 执行 → 审查 → 验收。顺序取自 `ROLE_KEYS`，这里只补一个「xx模型」的写法，
 *  免得"统筹"在两处各叫一个名字。 */
const ROLES: readonly { key: RoleKey; label: string }[] = ROLE_KEYS.map((key) => ({
  key,
  label: `${ROLE_TITLE[key]}模型`,
}));

const ROLE_LABEL: Record<RoleKey, string> = {
  orchestrator: "统筹模型",
  executor: "执行模型",
  reviewer: "审查模型",
  acceptor: "验收模型",
};

/** 「未选择」是下拉里的一个真选项，不是占位符：owner 要求未选模型时页面留白，
 *  那就必须有一条"回到未选"的路 —— 否则选错了厂商就再也回不去空状态。 */
const UNSET_LABEL = "未选择";

// 厂商下拉的选项来自目录，避免目录与 UI 两处维护同一份清单
const VENDOR_OPTIONS: readonly string[] = [UNSET_LABEL, ...MODEL_CATALOG.map((entry) => entry.vendor)];
const DOMESTIC_COUNT = MODEL_CATALOG.filter((entry) => entry.region === "国内").length;
const THINKING_OPTIONS: readonly string[] = THINKING_LEVELS.map((item) => item.label);

const ROLE_GRID = "grid grid-cols-[64px_minmax(0,1fr)_minmax(0,1.6fr)_88px] gap-2";
const CRED_GRID = "grid grid-cols-[160px_minmax(0,1fr)] items-center gap-2";

// 不写 focus:outline-none：全局的 :focus-visible outline 是键盘用户的定位线，只换边框色就够
const INPUT_CLASS =
  "w-full rounded-[2px] border border-edge bg-canvas px-2 py-2 text-sm text-ink placeholder:text-ink-muted focus:border-accent";

/** 数字输入容错：非法或小于下限时保持原值，避免用户清空输入框瞬间把配置打成异常数。 */
function toPositiveInt(raw: string, fallback: number): number {
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed) || parsed < 1) return fallback;
  return parsed;
}

export function SettingsModal({ open, onClose }: SettingsModalProps) {
  const [draft, setDraft] = useState<SettingsState>(() => cloneSettings(getSettings()));
  //: 主链路路由（2026-10-31 路线 B/C）。与四席分开存：四席是"模型角色"，这一份是"谁来执行这张单"。
  const [routingDraft, setRoutingDraft] = useState<RoutingChoice>(() => loadRouting());
  //: 自定义模型池（路线 C）：落 localStorage 的只有名称/模型/端点，Key 在会话内存（modelPool.ts）。
  const [pool, setPool] = useState<PoolModel[]>(() => loadPool());

  const addPool = (): void => setPool((prev) => [...prev, { id: newPoolId(), name: "", model: "", baseUrl: "" }]);
  const removePool = (index: number): void => setPool((prev) => prev.filter((_, i) => i !== index));
  const updatePool = (index: number, patch: Partial<PoolModel>): void =>
    setPool((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  const poolOptions = pool.filter((item) => item.name && item.model).map((item) => `${item.name} · ${item.model}`);
  const poolIdOf = (label: string): string =>
    pool.find((item) => `${item.name} · ${item.model}` === label)?.id ?? "";
  const poolLabelOf = (id: string): string => {
    const item = pool.find((entry) => entry.id === id);
    return item ? `${item.name} · ${item.model}` : "";
  };

  // 每次打开都从 store 重新拉一份草稿，放弃上一次未保存的改动
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // 焦点陷阱：模态打开时 Tab 不许漏到背后的画布上；关闭时把焦点还给「⚙ 设置」那颗按钮。
  useFocusTrap(dialogRef, open);

  useEffect(() => {
    if (open) {
      setDraft(cloneSettings(getSettings()));
      setRoutingDraft(loadRouting());
      setPool(loadPool());
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const updateRole = (roleKey: RoleKey, next: RoleSelection): void => {
    setDraft((prev) => {
      const draft = cloneSettings(prev);
      // 用显式分支而非计算属性键，保证类型收窄，strict 下不出联合键 widening 问题
      if (roleKey === "orchestrator") draft.orchestrator = next;
      else if (roleKey === "executor") draft.executor = next;
      else if (roleKey === "reviewer") draft.reviewer = next;
      else draft.acceptor = next;
      return draft;
    });
  };

  // 切厂商必须同时重置型号：旧型号几乎必然不属于新厂商，保留会产出非法组合。
  // 选「未选择」时两格一起清空 —— 半清空的组合（有厂商没型号）是脏数据。
  const handleVendorChange = (roleKey: RoleKey, vendor: string): void => {
    if (vendor === UNSET_LABEL) {
      updateRole(roleKey, { ...UNSET_ROLE, thinking: draft[roleKey].thinking });
      return;
    }
    const firstModel = modelsFor(vendor)[0] ?? "";
    updateRole(roleKey, { ...draft[roleKey], vendor, model: firstModel });
  };

  const handleThinkingChange = (roleKey: RoleKey, label: string): void => {
    const level = THINKING_LEVELS.find((item) => item.label === label)?.value;
    if (!level) return;
    updateRole(roleKey, { ...draft[roleKey], thinking: level });
  };

  const rolesUsing = (vendor: string): string[] =>
    ROLES.filter((role) => draft[role.key].vendor === vendor).map((role) => ROLE_LABEL[role.key]);

  // 密钥位跟着当前角色走：同一厂商只出现一次，切换角色后这里会一起增减
  const keyEntries = requiredKeyEntries(draft);
  const missingKeys = keyEntries.filter((entry) => (draft.apiKeys[entry.id] ?? "").trim().length === 0);

  const compatRoles = ROLES.filter(
    (role) => vendorEntry(draft[role.key].vendor)?.customEndpoint === true,
  );
  const usesOpenAiCompat = compatRoles.length > 0;
  // OpenAI 兼容的必填项有两处（接入地址 + 它自己那把密钥），但它们是**同一件事**：
  // 拆成两行 warn 会把同一个角色的同一个问题说两遍，合并成一条更接近"一张待办"。
  const isCompatEntry = (entry: { vendor: string }): boolean =>
    vendorEntry(entry.vendor)?.customEndpoint === true;
  const compatKeyMissing = missingKeys.filter(isCompatEntry);
  const otherMissingKeys = missingKeys.filter((entry) => !isCompatEntry(entry));
  const compatMissingParts: string[] = [
    ...(usesOpenAiCompat && !draft.openaiCompat.baseUrl.trim() ? ["接入地址"] : []),
    ...(compatKeyMissing.length > 0 ? ["OpenAI 兼容 API Key"] : []),
  ];

  const handleSave = (): void => {
    saveSettings(draft);
    saveRouting(routingDraft);
    savePool(pool.filter((item) => item.name && item.model));
    pushToast({
      tone: "ok",
      title: "设置已保存（本机）",
      detail: routingLabel(routingDraft, pool),
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center p-4 pt-[10vh]">
      <button
        type="button"
        aria-label="关闭设置对话框"
        onClick={onClose}
        className="scrim absolute inset-0 shrink-0 cursor-default whitespace-nowrap border-0 px-0 py-2 text-xs text-transparent"
      />
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        className="settings-sheet glass relative flex max-h-[82vh] w-full max-w-2xl flex-col overflow-y-auto rounded-[2px] border border-edge p-5 focus:outline-none"
      >
        <header className="mb-4">
          <h2 id="settings-title" className="hud-label text-ink">
            设置
          </h2>
          <p className="mt-1 text-xs text-ink-muted">
            模型选择存本机；API Key 仅本次会话保留，刷新需重填
          </p>
        </header>

        <section className="mb-4 flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="hud-label text-ink-muted">模型角色</h3>
            <span className="text-xs text-ink-muted">
              国内 {DOMESTIC_COUNT} 家 · 另有 OpenAI 兼容端点
            </span>
          </div>

          <div className={`${ROLE_GRID} items-baseline`} aria-hidden="true">
            <span />
            <span className="hud-hint">厂商</span>
            <span className="hud-hint">型号</span>
            <span className="hud-hint">思考强度</span>
          </div>

          {ROLES.map((role) => {
            const selection = draft[role.key];
            return (
              <div key={role.key} className={`${ROLE_GRID} items-start`}>
                <span className="pt-2 text-xs tracking-[0.06em] text-ink-muted">{role.label}</span>
                <HudDropdown
                  label={`${role.label}厂商`}
                  value={selection.vendor || UNSET_LABEL}
                  options={VENDOR_OPTIONS}
                  onChange={(vendor) => handleVendorChange(role.key, vendor)}
                />
                <HudDropdown
                  label={`${role.label}型号`}
                  value={selection.model}
                  options={modelsFor(selection.vendor)}
                  placeholder={selection.vendor ? "请选择型号" : "先选厂商"}
                  onChange={(model) => updateRole(role.key, { ...selection, model })}
                />
                <HudDropdown
                  label={`${role.label}思考强度`}
                  value={THINKING_LABEL[selection.thinking]}
                  options={THINKING_OPTIONS}
                  onChange={(label) => handleThinkingChange(role.key, label)}
                />
              </div>
            );
          })}
        </section>

        <section className="mb-4 flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="hud-label text-ink-muted">自定义模型池</h3>
            <span className="text-xs text-ink-muted">{pool.length} 个模型 · Key 仅本次会话</span>
          </div>
          <p className="text-xs text-ink-muted">
            名称 / 模型 ID / OpenAI 兼容 Base URL；Key 只留本次会话（不写 localStorage）。
            下面的三个路由下拉全部来自这个池子，不再有写死的模型名单。
          </p>
          {pool.map((item, index) => (
            <div
              key={item.id}
              className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1fr)_28px] items-start gap-2"
            >
              <input
                className={INPUT_CLASS}
                placeholder="名称（如 千问）"
                aria-label={`模型池第 ${index + 1} 行名称`}
                value={item.name}
                onChange={(event) => updatePool(index, { name: event.target.value })}
              />
              <input
                className={INPUT_CLASS}
                placeholder="模型 ID（如 qwen-max）"
                aria-label={`模型池第 ${index + 1} 行模型 ID`}
                value={item.model}
                onChange={(event) => updatePool(index, { model: event.target.value })}
              />
              <input
                className={INPUT_CLASS}
                placeholder="Base URL（https://…/v1）"
                aria-label={`模型池第 ${index + 1} 行 Base URL`}
                value={item.baseUrl}
                onChange={(event) => updatePool(index, { baseUrl: event.target.value })}
              />
              <input
                className={INPUT_CLASS}
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                placeholder="API Key（仅会话）"
                aria-label={`模型池第 ${index + 1} 行 API Key`}
                defaultValue={keyOf(item.id)}
                onChange={(event) => rememberKey(item.id, event.target.value)}
              />
              <button
                type="button"
                className="ghost shrink-0 whitespace-nowrap px-2 py-2 text-xs"
                aria-label={`删除模型 ${item.name || item.id}`}
                onClick={() => removePool(index)}
              >
                ×
              </button>
            </div>
          ))}
          <button type="button" className="ghost shrink-0 self-start whitespace-nowrap px-2 py-2 text-xs" onClick={addPool}>
            + 添加模型
          </button>
        </section>

        <section className="mb-4 flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="hud-label text-ink-muted">主链路路由</h3>
            <span className="text-xs text-ink-muted">{routingLabel(routingDraft, pool)}</span>
          </div>
          <p className="text-xs text-ink-muted">
            执行席可选 Qoder CLI（默认）或池里任意模型；审查/验收席从池里选模型时会由通用 Runner 真正调用。
            三岗互斥按"模型实例（同模型同 Key 视为同一实例）"判定，全默认 ⇒ 请求体不带这一格。
          </p>
          <div className="grid grid-cols-[64px_minmax(0,1fr)] items-center gap-2">
            <span className="text-xs tracking-[0.06em] text-ink-muted">执行者</span>
            <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
              <HudDropdown
                label="主链路执行者"
                value={
                  EXECUTOR_KIND_OPTIONS.find((option) => option.value === routingDraft.executorKind)?.label ??
                  EXECUTOR_KIND_OPTIONS[0].label
                }
                options={EXECUTOR_KIND_OPTIONS.map((option) => option.label)}
                onChange={(label) => {
                  const next = EXECUTOR_KIND_OPTIONS.find((option) => option.label === label)?.value;
                  if (next === "qoder" || next === "pool") {
                    setRoutingDraft((prev) => ({ ...prev, executorKind: next }));
                  }
                }}
              />
              <HudDropdown
                label="主链路执行模型"
                value={poolLabelOf(routingDraft.executor)}
                options={poolOptions}
                placeholder="从模型池选"
                onChange={(label) => setRoutingDraft((prev) => ({ ...prev, executor: poolIdOf(label) }))}
              />
            </div>
          </div>
          <div className="grid grid-cols-[64px_minmax(0,1fr)] items-center gap-2">
            <span className="text-xs tracking-[0.06em] text-ink-muted">审查员</span>
            <HudDropdown
              label="主链路审查员"
              value={routingDraft.reviewer ? poolLabelOf(routingDraft.reviewer) : REVIEWER_DEFAULT_LABEL}
              options={[REVIEWER_DEFAULT_LABEL, ...poolOptions]}
              onChange={(label) =>
                setRoutingDraft((prev) => ({
                  ...prev,
                  reviewer: label === REVIEWER_DEFAULT_LABEL ? "" : poolIdOf(label),
                }))
              }
            />
          </div>
          <div className="grid grid-cols-[64px_minmax(0,1fr)] items-center gap-2">
            <span className="text-xs tracking-[0.06em] text-ink-muted">验收员</span>
            <HudDropdown
              label="主链路验收员"
              value={routingDraft.acceptor ? poolLabelOf(routingDraft.acceptor) : ACCEPTOR_DEFAULT_LABEL}
              options={[ACCEPTOR_DEFAULT_LABEL, ...poolOptions]}
              onChange={(label) =>
                setRoutingDraft((prev) => ({
                  ...prev,
                  acceptor: label === ACCEPTOR_DEFAULT_LABEL ? "" : poolIdOf(label),
                }))
              }
            />
          </div>
        </section>

        <section className="mb-4 flex flex-col gap-2">
          <h3 className="hud-label text-ink-muted">API 凭据</h3>
          <p className="text-xs text-ink-muted">
            仅本次会话保留，不写入 localStorage；当前受理请求暂不携带模型/密钥字段，接入后才会生效。
          </p>
          {keyEntries.length === 0 ? (
            <p className="text-xs text-ink-muted">当前角色不需要 API Key。</p>
          ) : (
            keyEntries.map((entry) => (
              <div key={entry.id} className={CRED_GRID}>
                <label
                  htmlFor={`settings-key-${entry.id}`}
                  className="whitespace-nowrap text-xs tracking-[0.06em] text-ink-muted"
                >
                  {entry.vendor} API Key
                </label>
                <input
                  id={`settings-key-${entry.id}`}
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={draft.apiKeys[entry.id] ?? ""}
                  placeholder={`请输入 ${entry.vendor} API Key`}
                  onChange={(event) =>
                    setDraft((prev) => {
                      const next = cloneSettings(prev);
                      next.apiKeys[entry.id] = event.target.value;
                      return next;
                    })
                  }
                  className={INPUT_CLASS}
                />
              </div>
            ))
          )}
          {usesOpenAiCompat ? (
            <div className={CRED_GRID}>
              <label
                htmlFor="settings-openai-base-url"
                className="whitespace-nowrap text-xs tracking-[0.06em] text-ink-muted"
              >
                接入地址
              </label>
              <input
                id="settings-openai-base-url"
                type="text"
                value={draft.openaiCompat.baseUrl}
                placeholder="https://…/v1"
                onChange={(event) =>
                  setDraft((prev) => ({
                    ...prev,
                    openaiCompat: { ...prev.openaiCompat, baseUrl: event.target.value },
                  }))
                }
                className={INPUT_CLASS}
              />
            </div>
          ) : null}
        </section>

        <section className="mb-4 flex flex-col gap-2">
          <h3 className="hud-label text-ink-muted">系统参数</h3>
          <div className="grid grid-cols-[104px_minmax(0,1fr)] items-center gap-2">
            <label htmlFor="settings-max-concurrency" className="text-xs tracking-[0.06em] text-ink-muted">
              最大并发数
            </label>
            <input
              id="settings-max-concurrency"
              type="number"
              min={1}
              value={draft.maxConcurrency}
              onChange={(event) =>
                setDraft((prev) => ({
                  ...prev,
                  maxConcurrency: toPositiveInt(event.target.value, prev.maxConcurrency),
                }))
              }
              className={INPUT_CLASS}
            />
          </div>
          <div className="grid grid-cols-[104px_minmax(0,1fr)] items-center gap-2">
            <label htmlFor="settings-review-round-limit" className="text-xs tracking-[0.06em] text-ink-muted">
              审查轮次上限
            </label>
            <input
              id="settings-review-round-limit"
              type="number"
              min={1}
              value={draft.reviewRoundLimit}
              onChange={(event) =>
                setDraft((prev) => ({
                  ...prev,
                  reviewRoundLimit: toPositiveInt(event.target.value, prev.reviewRoundLimit),
                }))
              }
              className={INPUT_CLASS}
            />
          </div>
        </section>

        <footer className="mt-4 flex flex-col gap-2">
          {usesOpenAiCompat && compatMissingParts.length > 0 ? (
            <p className="text-xs text-warn">
              {compatRoles.map((role) => ROLE_LABEL[role.key]).join("、")}
              选用了 OpenAI 兼容：未填写 {compatMissingParts.join("、")}（不影响保存）
            </p>
          ) : null}
          {otherMissingKeys.map((entry) => (
            <p key={entry.id} className="text-xs text-warn">
              {rolesUsing(entry.vendor).join("、")}选用了 {entry.vendor}：未填写 {entry.vendor} API Key（不影响保存）
            </p>
          ))}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="shrink-0 whitespace-nowrap rounded-[2px] border border-edge bg-canvas px-4 py-2 text-sm text-ink"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSave}
              className="shrink-0 whitespace-nowrap rounded-[2px] bg-accent px-4 py-2 text-sm text-accent-ink"
            >
              保存配置
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
