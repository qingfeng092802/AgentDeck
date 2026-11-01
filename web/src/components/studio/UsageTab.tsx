import { useState } from "react";
import { useSettings } from "../../lib/settingsStore";
import { agentTitle, type StudioState } from "../../lib/studioEngine";
import { BUDGET_STEPS, usageOf } from "../../lib/studioUsage";

/** 成本与用量（执行看板右栏的第五个页签）。
 *
 *  这一格最容易写歪的地方是"把估算画成账单"：一个漂亮的进度条 + 两位小数，
 *  读者就会当成真花了这么多钱。所以：
 *  ① 标题旁边常驻「估算」二字，页脚再说一遍口径；
 *  ② 四席分档、**不跨口径求和**（统筹/执行/审查/验收各用各的模型，加在一起没有意义）；
 *  ③ 未选模型的席不产生金额，并明说"有一席没选，这张表只算得到已选的那几席"。
 */
export function UsageTab({ studio }: { studio: StudioState }) {
  const settings = useSettings();
  const [budgetIndex, setBudgetIndex] = useState(2);
  const budget = BUDGET_STEPS[budgetIndex] ?? BUDGET_STEPS[2];
  const report = usageOf(studio, settings, budget);
  const usedPercent = budget > 0 ? Math.min(100, Math.round((report.totalCost / budget) * 100)) : 0;
  const over = report.totalCost > budget;

  return (
    <div className="dt" data-usage-tab="true">
      <div className="dt-head">
        <span className="id">COST · 估算</span>
        <span className="phase-pill pill" style={{ color: over ? "var(--danger)" : "var(--ink-muted)" }}>
          {over ? "超预算" : "在预算内"}
        </span>
      </div>

      <section className="dt-sec">
        <div className="sec-h">
          <h4>单项目预算</h4>
          <i>{`¥ ${budget.toFixed(2)}`}</i>
          <span className="rule" />
        </div>
        <input
          type="range"
          min={0}
          max={BUDGET_STEPS.length - 1}
          step={1}
          value={budgetIndex}
          aria-label="单项目预算档位"
          onChange={(event) => setBudgetIndex(Number(event.target.value))}
          style={{ width: "100%", accentColor: "var(--accent)" }}
        />
        <div className="tags" style={{ marginTop: 6 }}>
          {BUDGET_STEPS.map((step, index) => (
            <button
              key={step}
              type="button"
              className={`tag${index === budgetIndex ? " hot" : ""}`}
              style={{ cursor: "pointer", background: "transparent" }}
              onClick={() => setBudgetIndex(index)}
            >
              {`¥${step}`}
            </button>
          ))}
        </div>
      </section>

      <section className="dt-sec">
        <div className="sec-h">
          <h4>读数</h4>
          <i>估算，非账单</i>
          <span className="rule" />
        </div>
        <dl className="dt-kv">
          {[
            ["已用", `¥ ${report.totalCost.toFixed(3)}`],
            ["剩余", `¥ ${report.remaining.toFixed(3)}`],
            ["占比", `${usedPercent}%`],
            ["调用", String(report.checkpoints.length)],
            ["Token", report.totalTokens.toLocaleString("zh-CN")],
          ].map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <div className="bar" style={{ height: 3, background: "var(--row-alt)", marginTop: 8, overflow: "hidden" }}>
          <i
            style={{
              display: "block",
              height: "100%",
              width: `${usedPercent}%`,
              background: over ? "var(--danger)" : "var(--accent)",
            }}
          />
        </div>
      </section>

      <section className="dt-sec">
        <div className="sec-h">
          <h4>按席位</h4>
          <i>口径之间不可相加</i>
          <span className="rule" />
        </div>
        <div className="bus-h" style={{ gridTemplateColumns: "minmax(0,1fr) 52px 44px" }}>
          <span>席位 · 模型</span>
          <span className="ow">调用</span>
          <span className="sz">¥</span>
        </div>
        {report.seats.map((seat) => (
          <div key={seat.key} className="bus-row" style={{ gridTemplateColumns: "minmax(0,1fr) 52px 44px" }}>
            <span
              className="fn"
              // 席位与模型分两行：挤在一行里，`deepseek-v4-pro` 这种没有空格可断的型号会被
              // 省略号吃掉尾巴 —— 而"pro 还是 flash"正是这张表要看的东西。
              title={`${seat.label} · ${seat.model || "未选模型"}`}
              style={{ display: "flex", flexDirection: "column", gap: 1, whiteSpace: "normal" }}
            >
              <b style={{ fontSize: "var(--fs-body)", fontWeight: 600 }}>{seat.label}</b>
              {/* 未选模型的那一席留空 —— 空着不是"零成本"，是"还没定价"。 */}
              <i
                style={{
                  fontStyle: "normal",
                  fontFamily: "var(--mono)",
                  fontSize: "var(--fs-meta)",
                  color: "var(--ink-muted)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {seat.model}
              </i>
            </span>
            <span className="ow">{seat.calls}</span>
            <span className="sz">{seat.cost.toFixed(3)}</span>
          </div>
        ))}
        {report.incomplete ? (
          <p className="dt-none" style={{ marginTop: 6 }}>
            有一席还没选模型：这张表只算得到已选的那几席，金额偏低。
          </p>
        ) : null}
      </section>

      <section className="dt-sec">
        <div className="sec-h">
          <h4>存档点</h4>
          <i>{`${report.checkpoints.length} 个`}</i>
          <span className="rule" />
        </div>
        {report.checkpoints.length ? (
          <ul className="dt-mani">
            {report.checkpoints.map((point) => (
              <li key={point.id}>
                <span className="p">{point.id}</span>
                {` · ${point.artifact} · 出自 ${agentTitle(point.owner)} · ${point.model || "未选模型"} · `}
                {`${point.tokensIn + point.tokensOut} tok · ¥ ${point.cost.toFixed(3)}`}
              </li>
            ))}
          </ul>
        ) : (
          <p className="dt-none">还没有存档点：每张卡完成时会在这里留一条。</p>
        )}
      </section>

      <p className="dt-none">
        口径：一个存档点 = 一张完成的卡；token 由产物行数推（行数 × 固定系数），单价取各厂公开价位的量级。
        这是估算，不是账单 —— 真实计费要等后端成本读端点落地。
      </p>
    </div>
  );
}
