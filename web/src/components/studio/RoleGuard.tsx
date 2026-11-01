import { roleSet, roleText, useSettings, type RoleKey } from "../../lib/settingsStore";

/** 四席与模型：统筹 / 执行 / 审查 / 验收，一人一行，行里写**模型**而不是工位名。
 *
 *  为什么这一格从"三个 agent"改成"四席 × 模型"：owner 2026-10-27 把模型搬进了设置，
 *  执行者 / 审查者 / 统筹 / 验收由所选模型决定 —— 那么"这三件事是不是同一主体干的"
 *  就不能再按岗位名判断（岗位名永远是三个不同的字符串，这条判据因此永远绿，等于没判）。
 *  现在比的是 (厂商, 型号)：同一把模型坐了两席，独立性就不成立。
 *
 *  互斥只判**执行 / 审查 / 验收**这三席：统筹是派单和裁决的那一席，它和执行席同模型不构成
 *  "自己审自己"，把统筹算进来会让默认配置直接变红 —— 那不是发现了问题，是把判据用错了地方。
 *
 *  未选模型的席留白（`<dd>` 里一个字符都不写），结论降级为「席位未坐满」而不是「互斥成立」：
 *  空着不是"没问题"，是"还没法判"。
 */
export function RoleGuard() {
  const settings = useSettings();
  const keys: readonly RoleKey[] = ["orchestrator", "executor", "reviewer", "acceptor"];
  const title: Record<RoleKey, string> = { orchestrator: "统筹", executor: "执行", reviewer: "审查", acceptor: "验收" };

  const filled = keys.filter((key) => roleSet(settings[key]));
  const missing = filled.length < keys.length;
  const checked = (["executor", "reviewer", "acceptor"] as const).filter((key) => roleSet(settings[key]));
  const texts = checked.map((key) => roleText(settings[key]));
  const overlap = new Set(texts).size !== texts.length;

  const state = missing ? "incomplete" : overlap ? "violation" : "ok";
  const label = missing ? "席位未坐满" : overlap ? "三岗重合" : "三岗互斥成立";
  const detail = missing
    ? `${keys.length - filled.length} 席还没选模型 —— 结论还不能算`
    : overlap
      ? "同一把模型占了两席 —— 自己审自己，互斥不成立"
      : "执行 / 审查 / 验收 由三把不同的模型担任";

  return (
    <div className="trio" data-state={state} data-role-guard="true">
      <div className="trio-h">
        <span>{state === "ok" ? "✓" : state === "violation" ? "⚠" : "·"}</span>
        <span>{label}</span>
        <em>{detail}</em>
      </div>
      <dl>
        {keys.map((key) => {
          const text = roleText(settings[key]);
          return (
            <div key={key} data-seat={key}>
              <dt>{title[key]}</dt>
              {/* 未选时这一格是空的。不写「未选择」也不写占位型号：
                  填空就是填空，多一个字都会让人以为那是系统给的默认值。 */}
              <dd>{text || ""}</dd>
            </div>
          );
        })}
      </dl>
    </div>
  );
}
