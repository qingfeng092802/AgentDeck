// Local-page evidence driver for `acp.verify_mvp1`: console errors, subresource failures, link inventory,
// empty blocks. Zero dependencies — Node's built-in WebSocket + Chrome DevTools Protocol over a headless Edge.
// Port discovery reads <profile>/DevToolsActivePort (stderr is not scrapeable: this Edge build floods it
// with libpng iCCP warnings), same finding as the 2026-10-17 07:5x probe.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

function parseArgv(argv) {
  const pages = [];
  const opts = { waitMs: 15000, settleMs: 400, edge: EDGE };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--page") pages.push(argv[++i]);
    else if (flag === "--edge") opts.edge = argv[++i];
    else if (flag === "--wait-ms") opts.waitMs = Number(argv[++i]);
    else if (flag === "--settle-ms") opts.settleMs = Number(argv[++i]);
    else throw new Error(`unknown flag ${flag}`);
  }
  return { pages, opts };
}

function fileUrl(absolute) {
  const posix = absolute.replace(/\\/g, "/");
  const withSlash = posix.startsWith("/") ? posix : `/${posix}`;
  return "file://" + withSlash.replace(/[^\w./:\-~%@+&=$,;!*'()#[\]]/g, (c) =>
    encodeURIComponent(c));
}

async function waitForFile(filePath, ms) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try {
      const text = readFileSync(filePath, "utf8").trim();
      const lines = text.split(/\r?\n/);
      if (lines.length >= 2 && lines[0].match(/^\d+$/)) return lines;
    } catch {
      /* not written yet */
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
}

const DOM_EXPRESSION =
  "JSON.stringify({" +
  "links:[...document.querySelectorAll('a[href]')].map(a=>a.getAttribute('href'))," +
  "subresources:[...document.querySelectorAll('link[href],script[src],img[src]')].map(n=>n.getAttribute('href')||n.getAttribute('src'))," +
  "emptyBlocks:[...document.querySelectorAll('section,div,article')]" +
  ".filter(n=>!n.textContent.trim()&&!n.querySelector('img,svg,video,iframe,canvas,input,button'))" +
  ".map(n=>n.tagName.toLowerCase()+(n.id?'#'+n.id:''))," +
  "title:document.title||null," +
  "bodyChars:document.body?document.body.innerText.trim().length:0})";

function dedupe(raw) {
  const seen = new Set();
  const out = [];
  for (const text of raw) {
    const key = String(text).replace(/^Uncaught\s+/, "").trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(String(text));
  }
  return out;
}

async function main() {
  const { pages, opts } = parseArgv(process.argv.slice(2));
  if (!pages.length) {
    console.log(JSON.stringify({ ok: false, why: "no_page_argument" }));
    process.exitCode = 1;
    return;
  }
  const profile = mkdtempSync(path.join(tmpdir(), "acp-cdp-"));
  const child = spawn(opts.edge, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--allow-file-access-from-files",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (d) => { stderr += d; });

  const active = await waitForFile(path.join(profile, "DevToolsActivePort"), opts.waitMs);
  if (!active) {
    child.kill();
    console.log(JSON.stringify({ ok: false, why: "devtools_port_not_found", stderrTail: stderr.slice(-400) }));
    process.exitCode = 1;
    return;
  }
  const browserUrl = `ws://127.0.0.1:${active[0]}${active[1]}`;
  const ws = new WebSocket(browserUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error("websocket_open_failed"));
  });

  let nextId = 0;
  const pending = new Map();
  const events = [];
  ws.onmessage = (frame) => {
    const msg = JSON.parse(frame.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
    if (msg.method) events.push({ sessionId: msg.sessionId, method: msg.method, params: msg.params });
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve) => {
    const id = ++nextId;
    pending.set(id, resolve);
    ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
  });

  const out = [];
  for (const page of pages) {
    const created = await send("Target.createTarget", { url: "about:blank" });
    const targetId = created.result.targetId;
    const attached = await send("Target.attachToTarget", { targetId, flatten: true });
    const sid = attached.result.sessionId;
    for (const domain of ["Runtime", "Log", "Network", "Page"]) await send(`${domain}.enable`, {}, sid);
    const before = events.length;
    const loadWait = new Promise((resolve) => {
      const timer = setTimeout(() => resolve("timeout"), opts.waitMs);
      const tick = () => {
        if (events.some((e) => e.sessionId === sid && e.method === "Page.loadEventFired")) {
          clearTimeout(timer);
          resolve("load");
          return;
        }
        setTimeout(tick, 50);
      };
      tick();
    });
    await send("Page.navigate", { url: fileUrl(page) }, sid);
    const loadState = await loadWait;
    await new Promise((r) => setTimeout(r, opts.settleMs));
    const slice = events.slice(before).filter((e) => e.sessionId === sid);
    const consoleErrs = slice.filter((e) => e.method === "Runtime.consoleAPICalled" && e.params.type === "error")
      .map((e) => (e.params.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
    const consoleWarns = slice.filter((e) => e.method === "Runtime.consoleAPICalled" && e.params.type === "warning")
      .map((e) => (e.params.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
    const logErrs = slice.filter((e) => e.method === "Log.entryAdded" && e.params.entry.level === "error")
      .map((e) => e.params.entry.text);
    const exceptions = slice.filter((e) => e.method === "Runtime.exceptionThrown")
      .map((e) => e.params.exceptionDetails.exception?.description ?? e.params.exceptionDetails.text);
    const failed = slice.filter((e) => e.method === "Network.loadingFailed")
      .map((e) => ({ errorText: e.params.errorText, type: e.params.type, blocked: e.params.blockedReason || null }));
    const evaluated = await send("Runtime.evaluate", { expression: DOM_EXPRESSION, returnByValue: true }, sid);
    let dom = null;
    let domError = null;
    if (evaluated.result?.result?.value) {
      dom = JSON.parse(evaluated.result.result.value);
    } else {
      domError = evaluated.result?.exceptionDetails?.text ?? "evaluate_returned_no_value";
    }
    out.push({
      page,
      loadState,
      errors: dedupe([...consoleErrs, ...logErrs, ...exceptions]),
      consoleWarnings: dedupe(consoleWarns),
      rawCounts: { consoleError: consoleErrs.length, logError: logErrs.length, exception: exceptions.length },
      loadingFailed: failed,
      dom,
      domError,
    });
    await send("Target.closeTarget", { targetId });
  }

  ws.close();
  child.kill();
  const version = stderr.match(/Edge\/([\d.]+)/)?.[1] ?? stderr.match(/Edg\/([\d.]+)/)?.[1] ?? null;
  console.log(JSON.stringify({ ok: true, browser: "msedge", browserVersion: version, browserPath: opts.edge, profile, pages: out }));
}

main().catch((error) => {
  console.log(JSON.stringify({ ok: false, why: "driver_failed", error: String(error) }));
  process.exitCode = 1;
});
