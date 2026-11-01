import { Fragment, useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  agentMono,
  agentTitle,
  contentOf,
  detailTask,
  extensionOf,
  makeZip,
  openFileDrawer,
  ownerOfFile,
  setDevice,
  setScreen,
  setSideTab,
  startNewRequirement,
  stopWordOf,
  traceOf,
  statusTone,
  toneVar,
  type SideTab,
  type StudioEventRecord,
  type StudioState,
} from "../../lib/studioEngine";
import { artifactsOfCard, deliveryBlockers, detailSay, staleOf, type DeliveryBlocker } from "../../lib/live";
import { RoleGuard } from "./RoleGuard";
import { DeliveryBlocked } from "./DeliveryBlocked";
import { UsageTab } from "./UsageTab";
import { VirtualFeed, WINDOW_THRESHOLD } from "./VirtualFeed";

/** 右栏：任务详情 / 活动流 / 审查 / 文件总线 / 用量 / 交付。
 *
 *  页签顺序照产品：任务详情是主面板，其余是它的旁证。新增的「用量」排在「交付」之前 ——
 *  成本和交付都是"这一单的收尾读数"，但成本要在验收之前看，所以它靠前。
 */

const TABS: readonly { k: SideTab; label: string }[] = [
  { k: "detail", label: "任务详情" },
  { k: "feed", label: "活动流" },
  { k: "review", label: "审查" },
  { k: "bus", label: "文件总线" },
  { k: "usage", label: "用量" },
  { k: "deliver", label: "交付" },
];

function SecHead({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="sec-h">
      <h4>{title}</h4>
      {hint ? <i>{hint}</i> : null}
      <span className="rule" />
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="meet-empty">{children}</div>;
}

/** 交付清单空着时"为什么"—— 任务详情与交付两页共用同一份口径。
 *
 *  规则本身在 `deliveryBlockers` 上（只认两种卡态、产物按卡筛、证据面读不到时记"不知道"）；
 *  这里只是把 `LiveState` 里那三格凑齐。两页各写一遍会漂：一处改了筛选条件，另一处还是旧口径，
 *  于是同一张单在两张页签里说两种原因 —— 那种不一致没人会当成 bug 报，只会让人不再信这屏。
 */
function blockersOf(studio: StudioState): DeliveryBlocker[] {
  return deliveryBlockers(studio.tasks, studio.artifacts, studio.reviews, studio.live.evidenceStale);
}

/** 详情首句的"已等 N 分"：只取整分钟，避免每秒重算一个只用于文案的数。 */
function minutesSince(iso: string | null): number {
  if (!iso) return 0;
  return Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 60_000));
}

/* ---------------------------- 任务详情 ---------------------------- */

function DetailTab({ studio }: { studio: StudioState }) {
  const task = detailTask();
  if (!task) {
    return (
      <Empty>
        还没有任务卡。
        <br />
        立项并派单后，这里会显示选中卡的三岗、依赖与门禁读数。
      </Empty>
    );
  }

  /* 产物按**卡**筛，不按 `owner` 筛 —— 口径与理由都在 `artifactsOfCard` 上（含 owner 2026-10-29
     那次「任务完成没有产物」的现场）。这里只调用，不在这里再写一遍筛选条件。 */
  const artifacts = artifactsOfCard(studio.artifacts, task);
  const rework = studio.tasks.filter((item) => item.kind === "rework" && item.deps.includes(task.id)).length;
  const difficulty = Math.min(5, 1 + task.deps.length);
  const floor = extensionOf(task.artifact) === "未设" ? "—" : "≥512 字节 / 12 行";
  const files = studio.deliver ? studio.deliver.gen.files : [];
  /* 交付清单空着时，是哪几张卡把它挡住的（口径全在 `blockersOf` → `deliveryBlockers` 上）。
     清单里有文件就没人问这个问题，所以只在空着的时候用 —— 但算它很便宜，不必分叉。 */
  const blockers = blockersOf(studio);
  /* 三层告知里的"就地"：只回答这一张卡为什么停、下一步点哪里。
     事实来自 `deliveryBlockers`（与交付页同一份口径），句子由 `detailSay` 统一生成。 */
  const stale = staleOf(task, studio.artifacts, studio.live.evidenceStale);
  const stalled = studio.live.stage === "stalled" && task.status === "run";
  const reading = stopWordOf(task, { stalled, stale: stale?.reason ?? null });
  const say = detailSay({
    task,
    blocker: blockers.find((item) => item.task.id === task.id),
    stale,
    stalled,
    elapsedMin: minutesSince(studio.live.stageSince),
  });
  const acs = (studio.plan ? studio.plan.ac : []).map((item) => {
    const gate = studio.gate.find((entry) => entry.id === item.id);
    return { id: item.id, text: item.text, met: gate ? (gate.on ? 1 : 0) : -1, round: gate ? 1 : null };
  });
  const doneDeps = task.deps.filter((dep) => studio.tasks.find((item) => item.id === dep)?.status === "done");

  const readings: [string, string][] = [
    ["审查轮", `${task.round} / 3`],
    ["返工", String(rework)],
    ["难度", String(difficulty)],
    ["换人", task.kind === "rescue" ? "1" : "0"],
    ["产物门槛", floor],
    ["格式", extensionOf(task.artifact)],
  ];
  if (studio.deliver) readings.push(["预览", "已生成"]);

  return (
    <div className="dt">
      <div className="dt-head">
        <span className="id" title={task.id}>{task.id}</span>
        <span className="phase-pill pill" style={{ color: statusTone(task.status) }}>
          {reading.word}
        </span>
      </div>
      {say ? (
        <div className="dt-say" data-tone={say.tone}>
          <p>{say.line}</p>
          <button type="button" className="ghost dt-say-act" onClick={() => setSideTab(say.action)}>
            {say.actionLabel}
          </button>
        </div>
      ) : null}
      <div>
        <p className="dt-sub">{`${agentTitle(task.owner)} · 第 ${task.round} 轮 · ${task.deps.length} 条前置`}</p>
        <p className="dt-title">{task.title}</p>
      </div>

      <RoleGuard />

      <section className="dt-sec">
        <SecHead title="前置依赖" hint={`${task.deps.length} 条`} />
        {task.deps.length ? (
          <>
            <div className="dt-deps">
              {task.deps.map((dep) => (
                <span key={dep} className="d">
                  {dep}
                  {doneDeps.includes(dep) ? " ✓" : ""}
                </span>
              ))}
            </div>
            <p className="dt-none" style={{ marginTop: 5 }}>{`${doneDeps.length}/${task.deps.length} 已就绪`}</p>
          </>
        ) : (
          <p className="dt-none">无前置 —— 这一列是链头。</p>
        )}
      </section>

      <section className="dt-sec">
        <SecHead title="门禁读数" />
        {/* 读数只列**真会变**的东西。
            原来这里有八行，其中「限时」是硬编码的 `"未设"`，任何状态下都是未设 —— 一行永远空的读数，
            读起来像"功能没做完"，而不是"这个维度不适用"；「预览」在交付前也是未设。
            现在「限时」整行删掉，「预览」改成有值才出现：缺席比"未设"诚实。 */}
        <dl className="dt-kv">
          {readings.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="dt-sec">
        <SecHead title="已验证产物" hint={`${artifacts.length} 份`} />
        {artifacts.length ? (
          <ul className="dt-art">
            {artifacts.map((item) => (
              <li key={`${item.name}-${item.path ?? item.at}`}>
                <div className="r">
                  <b>{item.name}</b>
                  <span>{`${item.lines} 行 · ${extensionOf(item.name)}`}</span>
                </div>
                <div className="bar">
                  <i style={{ width: "100%" }} />
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="dt-none">这张卡还没有已验证产物。</p>
        )}
      </section>

      <section className="dt-sec">
        {/* 清单空着的时候，标题右边不再只写一个「整单 0 项」—— 那个数字是真的，
            却读不出"为什么"。有卡挡住时把张数写在读数里，人一眼就知道该往下看。 */}
        <SecHead
          title="交付清单"
          hint={
            files.length
              ? `整单 ${files.length} 项`
              : blockers.length
                ? `0 项 · ${blockers.length} 张卡待处理`
                : "整单 0 项"
          }
        />
        {files.length ? (
          <ul className="dt-mani">
            {files.map((file) => (
              <li key={file.name}>
                <span className="p">{file.name}</span>
                {` · 出自 ${agentTitle(ownerOfFile(file.name))} · ${new TextEncoder().encode(file.content).length} 字节 / ${file.content.split("\n").length} 行`}
              </li>
            ))}
          </ul>
        ) : blockers.length ? (
          <DeliveryBlocked blockers={blockers} />
        ) : (
          <p className="dt-none">还没有可交付的文件</p>
        )}
      </section>

      <section className="dt-sec">
        <SecHead title="验收标准" hint={`${acs.length} 条`} />
        {acs.length ? (
          <ul className="dt-ac">
            {acs.map((item) => (
              <li key={item.id} data-met={item.met}>
                <span className="m">{item.met === 1 ? "✓" : item.met === 0 ? "✕" : "…"}</span>
                <span className="t">{item.text}</span>
                <span className="k">{item.round ? `第 ${item.round} 轮` : "尚未判定"}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="dt-none">这张卡没有登记验收标准。</p>
        )}
      </section>

      <section className="dt-sec">
        <SecHead title="审查历史" hint={`整单 ${studio.reviews.length} 轮`} />
        {studio.reviews.length ? (
          <ul className="dt-hist">
            {studio.reviews.slice(-4).map((review, index) => (
              <li key={`${review.t}-${index}`} data-k={review.k}>
                <span className="w">{`${review.t} · ${agentTitle(review.who)}`}</span>
                <br />
                {review.title}
              </li>
            ))}
          </ul>
        ) : (
          <p className="dt-none">这张卡还没有审查结论。</p>
        )}
      </section>

      {/* 调度日志**内联**在页签里，不再是一个盖住半屏的抽屉。
       *
       * 2026-10-27 之前它是一个按钮 + 抽屉：抽屉里 6 项字段（负责人/状态/依赖/输入/动作/产出）
       * 有 5 项在这一页已经显示过，唯一的独有内容是这段日志。为了 1 项重复 5 项、
       * 还要额外付一次"打开 → 读 → 关掉"的上下文切换，不划算。
       *
       * 用 `<details>` 而不是直接铺开：日志是**按需查证**的东西（"这张卡为什么重跑了两轮"），
       * 不是每次都要读的正文。默认收起，键盘可达（原生元素，Enter/Space 开合）。 */}
      <details className="dt-log">
        <summary>调度日志</summary>
        <pre className="pre">{traceOf(task)}</pre>
      </details>
    </div>
  );
}

/* ---------------------------- 活动流 ---------------------------- */

function eventRow(event: StudioEventRecord) {
  return (
    <div className="ev" data-k={event.kind} style={{ ["--tone" as string]: toneVar(event.a) }}>
      <span className="t">{event.t}</span>
      <span className="a">{agentMono(event.a)}</span>
      <span className="x">
        {event.text}
        {/* 文件名只在正文**没提到**它的时候才补一个 chip。
            原来无条件渲染，于是 25 行里至少 6 行读成结巴：
            「产出 prd.md」后面再挂一个 `prd.md`、「完成 · serve.py」后面再挂一个 `serve.py`。
            判断用 `includes` 而不是改引擎里二十几处 `log()` 调用 —— 改调用点会漏，
            而"正文提到过就不再补"这条规则对所有调用点都成立，包括以后新加的。 */}
        {event.file && !event.text.includes(event.file) ? <span className="f">{event.file}</span> : null}
      </span>
    </div>
  );
}

function FeedTab({ studio }: { studio: StudioState }) {
  if (!studio.events.length) return <Empty>还没有留痕。</Empty>;
  /* P2-11：240 行全量 `.map` 的做法在"留痕一直在追"的时候会让右栏每帧重排 240 个节点。
     超过 `WINDOW_THRESHOLD` 时只渲染视口那一段 + 上下留白（滚动条长度不变，看起来一样）；
     少的时候维持全量渲染，那条路径也是测试与 SSR 都能跑的对照路径。 */
  if (studio.events.length <= WINDOW_THRESHOLD) {
    return (
      <>
        {studio.events.map((event, index) => (
          <Fragment key={`${event.t}-${index}`}>{eventRow(event)}</Fragment>
        ))}
      </>
    );
  }
  return (
    <VirtualFeed
      items={studio.events}
      keyOf={(event, index) => `${event.t}-${index}`}
      renderItem={(event) => eventRow(event)}
    />
  );
}

/* ---------------------------- 审查 ---------------------------- */

function ReviewTab({ studio }: { studio: StudioState }) {
  if (!studio.reviews.length && !studio.gate.length) {
    return studio.live.evidenceStale ? (
      <Empty>
        读不到审查结论。
        <br />
        后端这一版没有 `/evidence` 读面 —— 重启一次 acp.serve，再从它打印的地址打开界面。
      </Empty>
    ) : (
      <Empty>
        门禁还没开始。
        <br />
        E2E、安全扫描、AC 比对、验收都会落在这里。
      </Empty>
    );
  }
  const gateDone = studio.gate.filter((item) => item.on).length;
  return (
    <>
      {studio.gate.length ? (
        <div className="th-card" data-k="gate">
          <div className="th-h">
            <span className="mono-chip" style={{ ["--tone" as string]: toneVar("reviewer") }}>
              审查
            </span>
            门禁比对与互斥 · 独立代码审查员
            <span className="r">{`${gateDone}/${studio.gate.length}`}</span>
          </div>
          {/* 这里原来还有一句「不看做得漂不漂亮，只逐条比对 PRD 的 AC 是否成立」——
              标题已经写着"门禁比对"，下面列的就是逐条 AC，那句话在替界面解释界面。删。 */}
          {studio.gate.map((item) => (
            <div key={item.id} className={`chk${item.on ? " on" : ""}`}>
              <span className="m">✓</span>
              <span>{item.text}</span>
              <span className="ref">{item.id}</span>
            </div>
          ))}
          {gateDone === studio.gate.length ? (
            <div style={{ marginTop: 7, fontSize: "var(--fs-body)", color: "var(--ok)", fontWeight: 650 }}>判定：PASS · 移交验收</div>
          ) : null}
        </div>
      ) : null}
      {studio.reviews
        .slice()
        .reverse()
        .map((review, index) => (
          <div key={`${review.t}-${index}`} className="th-card" data-k={review.k}>
            <div className="th-h">
              <span className="mono-chip" style={{ ["--tone" as string]: toneVar(review.who) }}>
                {agentMono(review.who)}
              </span>
              {review.title}
              <span className="r">{review.t}</span>
            </div>
            <div className="th-b" dangerouslySetInnerHTML={{ __html: review.body }} />
          </div>
        ))}
    </>
  );
}

/* ---------------------------- 文件总线 ---------------------------- */

function BusTab({ studio }: { studio: StudioState }) {
  if (!studio.artifacts.length) {
    /* 空着有两种原因，不许用同一句话说（见 `LiveState.evidenceStale`）。 */
    return studio.live.evidenceStale ? (
      <Empty>
        读不到文件总线。
        <br />
        后端这一版没有 `/evidence` 读面 —— 重启一次 acp.serve，再从它打印的地址打开界面。
      </Empty>
    ) : (
      <Empty>文件总线还是空的。</Empty>
    );
  }
  return (
    <>
      <div className="bus-h">
        <span>文件</span>
        <span className="ow">归属</span>
        <span className="sz">行数</span>
      </div>
      {studio.artifacts
        .slice()
        .reverse()
        .map((item, index) => {
          /* 这一行能不能点，判据是"这个文件的正文存在不存在"（= 交付了没有）。
           *
           * 交付之前文件总线是一张**清单**：它回答"产出了什么"，不回答"写了什么"。
           * 所以那些行就是 div —— 不给按钮语义，也就不需要一句"交付后可查看"的说明：
           * 可点本身是状态。交付之后同一行变成 button，键盘可达、Enter 打开。 */
          const content = contentOf(item.name);
          const lines = content === null ? item.lines : content.split("\n").length;
          const tone = { ["--tone" as string]: toneVar(item.owner) };
          const cells = (
            <>
              <span className="fn" title={item.path ?? item.name}>
                {item.name}
              </span>
              <span className="ow">{agentMono(item.owner)}</span>
              {/* 行数在交付前是记账时估的（`addArtifact` 里那个 40~200 的随机数），
                  交付后换成正文的真实行数 —— 前者是估算、后者是事实，切换本身是对的。 */}
              <span className="sz">{lines}</span>
            </>
          );
          return content === null ? (
            <div key={`${item.name}-${item.path ?? index}`} className="bus-row" style={tone}>
              {cells}
            </div>
          ) : (
            <button
              type="button"
              key={`${item.name}-${item.path ?? index}`}
              className="bus-row"
              style={tone}
              title={`打开 ${item.path ?? item.name}`}
              onClick={() => openFileDrawer(item.name)}
            >
              {cells}
            </button>
          );
        })}
    </>
  );
}

/* ---------------------------- 交付 ---------------------------- */

function DeliverTab({ studio }: { studio: StudioState }) {
  const [downloaded, setDownloaded] = useState<string | null>(null);
  const deliver = studio.deliver;
  if (!deliver) {
    const liveDelivery = studio.source === "live" ? studio.live.delivery : null;
    if (liveDelivery) {
      return (
        <div className="deliver">
          <div className="stamp">
            <div className="big">交付包已生成</div>
            <div className="sub">后端交付包 · {liveDelivery.manifest.length} 个文件</div>
          </div>
          <dl className="dkv">
            <dt>入口</dt>
            <dd className="mono">{liveDelivery.entryPath}</dd>
            <dt>清单</dt>
            <dd>{liveDelivery.manifest.length} 项</dd>
            <dt>未达标项</dt>
            <dd>{liveDelivery.openIssues}</dd>
          </dl>
          <div className="pre" style={{ marginTop: 10 }}>
            {liveDelivery.manifest.map((item) => `${item.relPath} · ${item.lines} 行 · ${item.format}`).join("\n")}
          </div>
          <p className="hud-hint" style={{ marginTop: 8 }}>
            实时档只给索引；文件正文留在 results/&lt;project&gt;/ 的 workspace 里。
          </p>
        </div>
      );
    }
    /* 实时档的交付包要等验收通过才生成，所以这一页在交付前本来就是空的。
       但"空"有两种：**还没验收**（这句原话说的就是它）与**卡住了**（有卡停在门禁前，
       验收根本到不了）。只说前一种，人会一直等一个永远不会出现的交付包 ——
       所以有卡挡着的时候，把同一块"为什么"接在下面，与任务详情那一格说同一句话。 */
    const blockers = blockersOf(studio);
    return (
      <>
        <Empty>
          {studio.source === "live" ? (
            "实时档交付包尚未生成：验收通过后由后端 assemble 生成。"
          ) : (
            <>
              验收通过后，交付包会出现在这里：
              <br />
              可下载 zip、可当场预览、可点购物车。
            </>
          )}
        </Empty>
        {blockers.length ? (
          <div style={{ marginTop: 12 }}>
            <DeliveryBlocked blockers={blockers} />
          </div>
        ) : null}
      </>
    );
  }
  const gen = deliver.gen;

  const handleDownload = (): void => {
    const blob = makeZip(gen.files);
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `${studio.plan ? studio.plan.title : "项目"}-交付包.zip`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(anchor.href), 5000);
    setDownloaded(`${gen.files.length} 个文件`);
  };

  return (
    <div className="deliver">
      <div className="stamp">
        <div className="big">PASS · 已签字</div>
        <div className="sub">交付验收专家对照原始需求逐条确认</div>
        <div className="sig">
          <span>{studio.plan ? studio.plan.prj : ""}</span>
          <span>{`v${deliver.version}`}</span>
        </div>
      </div>

      {gen.runnable ? (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
            <span style={{ fontSize: "var(--fs-body)", fontWeight: 650 }}>交付预览</span>
            <div className="dev-toggle">
              <button type="button" className={studio.dev === "phone" ? "on" : ""} onClick={() => setDevice("phone")}>
                手机 375
              </button>
              <button type="button" className={studio.dev === "desk" ? "on" : ""} onClick={() => setDevice("desk")}>
                桌面
              </button>
            </div>
          </div>
          <div className="frame-wrap">
            <div className={`frame${studio.dev === "desk" ? "" : " phone"}`} id="frame">
              <iframe id="pv" title="交付预览" srcDoc={deliver.html ?? ""} sandbox="allow-scripts" />
            </div>
          </div>
        </>
      ) : (
        /* 不可运行的时候什么都不说。原来这里有一句"本场景未内置可运行代码生成器
           （原型只对静态站点生成真实代码），下面是交付清单" —— 那是写给开发者看的边界说明，
           用户只需要知道"这一单没有可预览的东西"，而"没有预览块"本身已经把这件事说完了。 */
        null
      )}

      <div className="dl-row">
        <button type="button" className="primary" id="btnDl" onClick={handleDownload}>
          下载交付包 .zip
        </button>
        {gen.runnable ? (
          <button
            type="button"
            className="ghost"
            onClick={() => {
              const url = URL.createObjectURL(new Blob([deliver.html ?? ""], { type: "text/html" }));
              window.open(url, "_blank");
              window.setTimeout(() => URL.revokeObjectURL(url), 20000);
            }}
          >
            新窗口
          </button>
        ) : null}
      </div>
      {downloaded ? <p className="dt-none" style={{ marginTop: 6 }}>{`已下载 · ${downloaded}`}</p> : null}

      <div style={{ marginTop: 12 }}>
        <div className="bus-h">
          <span>文件</span>
          <span className="ow">归属</span>
          <span className="sz">KB</span>
        </div>
        {gen.files.map((file) => (
          <button
            type="button"
            key={file.name}
            className="bus-row"
            style={{ ["--tone" as string]: "var(--a5)" }}
            title={`打开 ${file.name}`}
            onClick={() => openFileDrawer(file.name)}
          >
            <span className="fn">{file.name}</span>
            <span className="ow">{agentMono(ownerOfFile(file.name))}</span>
            <span className="sz">{(new TextEncoder().encode(file.content).length / 1024).toFixed(1)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------- 面板 ---------------------------- */

export function SidePanel({ studio }: { studio: StudioState }) {
  /* 交付完成 = 有交付物。用它而不是 `phase === 4`：phase 是阶段条的读数，会在收尾动画里
     短暂停在 3，而"能不能开下一单"只取决于这一单是不是真的交出去了。 */
  const delivered = studio.deliver !== null;

  /* roving tabindex：六个页签只有当前页是 Tab 停靠点，其余用 ←/→ 跳。
     `setSideTab` 已经处理"进入即清角标"，这里只负责把焦点送过去。
     用 DOM 查询而不是 ref 数组：六个页签的顺序就是 TABS 的顺序，别的地方不持有它。 */
  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number): void => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else next = TABS.length - 1;
    setSideTab(TABS[next].k);
    const tabs = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    tabs?.[next]?.focus();
  };

  /* 页签溢出 = **只在真溢出时**给右侧渐隐。
   *
   *  为什么不把 `mask-image` 写死在 CSS 里：六个页签的自然宽度是 288px、容器内宽 350px
   *  （352 − 两侧各 1px 边框），余量 62px —— 按现在的字档，它们放得下。
   *  无条件渐隐会把「交付」最后两笔啃掉一块，那是在报一个不存在的状态。
   *  所以判据由 DOM 实测：`scrollWidth - clientWidth > 1` 才算吃紧。
   *
   *  另记一个 `data-end`：已经滚到最右时撤掉渐隐 —— 否则"到头了"和"还有更多"长得一样，
   *  而这两个状态要用户做的动作是相反的（停下 / 继续滑）。
   *
   *  页签名是静态的，会变的只有列宽（1440 下 352px，1000 下右栏沉底变宽）与字体度量；
   *  所以观察容器尺寸 + 滚动位置，并在 `document.fonts.ready` 后补一次。 */
  const tabsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return undefined;
    const sync = (): void => {
      const overflow = el.scrollWidth - el.clientWidth > 1;
      const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 1;
      el.dataset.overflow = overflow ? "true" : "false";
      el.dataset.end = atEnd ? "true" : "false";
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    el.addEventListener("scroll", sync, { passive: true });
    void document.fonts?.ready.then(sync);
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", sync);
    };
  }, []);

  return (
    <aside className="side">
      {/* 六个页签是**真的** tablist，不是六个长得像页签的按钮。
          之前一个 role 都没有：读屏读到的是六个没有任何状态的按钮，用户没法知道"我现在在哪一页"、
          也没法知道"审查那一页有没有新东西"（角标是纯视觉的）。 */}
      <div className="side-tabs" id="sideTabs" role="tablist" aria-label="执行看板右侧面板" ref={tabsRef}>
        {TABS.map((tab, index) => {
          /* 角标 = "这一页有新东西，你还没看"。它取代了原来的自动翻页：
             引擎只置位（`state.unread`），翻页只由用户点击（`setSideTab`）或点节点（`selectTask`）触发。
             用 `!` 不用数字 —— 它说的是"去看看吧"，不是一个可以累加的计数；
             写 `1` 会让人以为"只有一条"，而实际是"从上次看过之后有新的"。 */
          const badge = (tab.k === "review" && studio.unread.review) || (tab.k === "deliver" && studio.unread.deliver);
          return (
            <button
              key={tab.k}
              type="button"
              role="tab"
              className={`side-tab${studio.tab === tab.k ? " on" : ""}`}
              data-tab={tab.k}
              aria-selected={studio.tab === tab.k}
              aria-controls="sideBody"
              tabIndex={studio.tab === tab.k ? 0 : -1}
              onKeyDown={(event) => onTabKey(event, index)}
              onClick={() => setSideTab(tab.k)}
            >
              {tab.label}
              {badge ? (
                <span className="badge-n" aria-label="有新内容">
                  !
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      <div className="side-body" id="sideBody" role="tabpanel" aria-label={TABS.find((tab) => tab.k === studio.tab)?.label}>
        {studio.tab === "detail" ? <DetailTab studio={studio} /> : null}
        {studio.tab === "feed" ? <FeedTab studio={studio} /> : null}
        {studio.tab === "review" ? <ReviewTab studio={studio} /> : null}
        {studio.tab === "bus" ? <BusTab studio={studio} /> : null}
        {studio.tab === "usage" ? <UsageTab studio={studio} /> : null}
        {studio.tab === "deliver" ? <DeliverTab studio={studio} /> : null}
      </div>
      {/* 回首页的那条路钉在右栏底部，**不随页签走**。
          执行看板这一屏把整块画布都占了，唯一的"回去"原来是左栏那扇门（在 12 个工位的最上面），
          而人在右栏读交付清单时，目光与鼠标都在右边 —— 出口就得在这一侧。
          钉在底部而不是塞进某个页签：六个页签都有可能需要回去，塞进去就有五处会漏。

          未立项时不渲染：那时人就站在工作室里（受理卡就在左边），再给一个"回项目工作室"
          等于在原地打转 —— 而且它是这一屏唯一一个点了不动的按钮。

          交付之后多一颗「开启新需求」：这时候"回去看看"已经没意义了（该看的都看完了），
          真正要做的下一件事是**开下一单**。原来只有「← 项目工作室」，点回去还停在那一单上，
          要开新的还得自己找到受理卡的「重新立项」—— 交付态的最后一米断在这里。 */}
      {studio.plan ? (
        <div className="side-foot">
          <button type="button" className="ghost side-home" onClick={() => setScreen("studio")}>
            ← 项目工作室
          </button>
          {delivered ? (
            <button type="button" className="primary side-new" onClick={() => startNewRequirement()}>
              开启新需求
            </button>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}
