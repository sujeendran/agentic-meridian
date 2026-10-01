// Meridian Co-pilot frontend: one SSE stream drives both the chat and the dashboard.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const fmt = {
  num: (v, d = 3) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d)),
  pct: (v, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`),
  money: (v) => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v),
};

let sessionId = null;
let state = null;
let selectedRun = 0;        // 0 = always follow the latest fit
let chartsShownFor = null;  // "fit id:benchmark" of the charts on screen
let reportKind = "summary";
let priorsDirty = false;    // user is editing the priors table
let current = null;         // assistant message being streamed

// ------------------------------------------------------------------ API

async function post(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, ...body }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3500);
}

// ------------------------------------------------------------------ chat

const messages = $("messages");

function scrollChat() {
  messages.scrollTop = messages.scrollHeight;
}

function addMessage(cls, text) {
  $("welcome").hidden = true;
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  messages.appendChild(el);
  scrollChat();
  return el;
}

function startAssistant(notice) {
  if (notice) addMessage("notice", notice);
  const el = addMessage("msg assistant", "");
  el.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  current = { el, segment: null, raw: "" };
}

function ensureText() {
  current.el.querySelector(".typing")?.remove();
  if (!current.segment) {
    current.segment = document.createElement("div");
    current.segment.className = "text";
    current.el.appendChild(current.segment);
    current.raw = "";
  }
}

function appendText(text) {
  if (!current) startAssistant();
  ensureText();
  current.raw += text;
  current.segment.innerHTML = DOMPurify.sanitize(marked.parse(current.raw));
  scrollChat();
}

function addToolCall({ name, args }) {
  if (!current) startAssistant();
  current.el.querySelector(".typing")?.remove();
  const el = document.createElement("details");
  el.className = "tool";
  el.dataset.name = name;
  el.innerHTML = `<summary><span class="name"></span><span class="status muted">running…</span></summary><pre></pre>`;
  el.querySelector(".name").textContent = name.replaceAll("_", " ");
  el.querySelector("pre").textContent = `args: ${JSON.stringify(args, null, 2)}`;
  current.el.appendChild(el);
  current.segment = null; // text after a tool call starts a new block
  scrollChat();
}

function addToolResult({ name, response }) {
  const pending = [...(current?.el.querySelectorAll(`details.tool[data-name="${name}"]`) || [])]
    .find((el) => !el.dataset.done);
  if (!pending) return;
  pending.dataset.done = "1";
  const failed = response?.status === "error";
  const status = pending.querySelector(".status");
  status.textContent = failed ? "✕ error" : "✓ done";
  status.className = `status ${failed ? "down" : "up"}`;
  const text = JSON.stringify(response, null, 2);
  pending.querySelector("pre").textContent += `\n\nresult: ${text.length > 3000 ? text.slice(0, 3000) + "…" : text}`;
}

function endAssistant() {
  if (current && !current.el.querySelector(".text, details")) current.el.remove();
  current = null;
}

function sendMessage(text) {
  text = text.trim();
  if (!text) return;
  addMessage("msg user", text);
  post("/api/chat", { text }).catch((e) => addMessage("msg error", e.message));
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage($("input").value);
  $("input").value = "";
  $("input").style.height = "auto";
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = `${e.target.scrollHeight}px`;
});
document.querySelectorAll(".suggestions button").forEach((b) =>
  b.addEventListener("click", () => sendMessage(b.textContent)));

$("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  addMessage("msg user", `📎 Uploaded ${file.name}`);
  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("file", file);
  const resp = await fetch("/api/upload", { method: "POST", body: form });
  if (!resp.ok) addMessage("msg error", (await resp.json()).detail);
  e.target.value = "";
});

// ------------------------------------------------------------------ dashboard

document.querySelectorAll("#tabs button").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${btn.dataset.tab}`));
  }));

$("runSelect").addEventListener("change", (e) => {
  selectedRun = Number(e.target.value);
  renderOverview();
});

document.querySelectorAll("#reportSwitch button").forEach((btn) =>
  btn.addEventListener("click", () => {
    reportKind = btn.dataset.report;
    document.querySelectorAll("#reportSwitch button").forEach((b) => b.classList.toggle("active", b === btn));
    renderReports();
  }));

$("stopBtn").addEventListener("click", () => post("/api/stop", {}).catch((e) => toast(e.message)));
$("fitBtn").addEventListener("click", async () => {
  try {
    if (priorsDirty) await applyPriors();
    await post("/api/fit", {});
  } catch (e) {
    toast(e.message);
  }
});
$("applyPriors").addEventListener("click", () => applyPriors().then(() => toast("Priors updated")).catch((e) => toast(e.message)));

async function applyPriors() {
  const priors = {};
  $("priorsTable").querySelectorAll("tr[data-channel]").forEach((row) => {
    priors[row.dataset.channel] = {
      mean: Number(row.querySelector("[data-field=mean]").value),
      sd: Number(row.querySelector("[data-field=sd]").value),
    };
  });
  priorsDirty = false;
  await post("/api/priors", { priors });
}

function shownRun() {
  const runs = state.runs;
  return runs.find((r) => r.id === selectedRun) || runs[runs.length - 1];
}

function render() {
  const d = state.dataset;
  $("datasetChip").textContent =
    `${d.name} · ${d.n_geos === 1 ? "National" : `${d.n_geos} geos`} · ${d.n_weeks} weeks (${d.start} → ${d.end}) · ${d.channels.length} channels`;
  $("jobBanner").hidden = !state.job;
  if (state.job) $("jobLabel").textContent = state.job.label;
  else $("jobProgress").textContent = "";
  $("fitBtn").disabled = Boolean(state.job);

  $("runSelect").innerHTML =
    `<option value="0">Latest fit</option>` +
    state.runs.map((r) => `<option value="${r.id}">Fit #${r.id} (${esc(r.source)})</option>`).join("");
  $("runSelect").value = String(state.runs.some((r) => r.id === selectedRun) ? selectedRun : 0);

  renderOverview();
  renderPriors();
  renderRuns();
  renderReports();
  renderBudget();
}

function tile(label, value, status, note) {
  const icon = { pass: "✓", fail: "✕", none: "" }[status];
  return `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div>
    <div class="status ${status}"><span class="dot"></span>${icon} ${note}</div></div>`;
}

function renderOverview() {
  const run = state.runs.length ? shownRun() : null;
  $("overviewEmpty").hidden = Boolean(run);
  $("overviewBody").hidden = !run;
  if (!run) return;
  const m = run.metrics, t = state.targets;
  const r2ok = m.r2 >= t.min_r2, rhatok = m.max_rhat != null && m.max_rhat <= t.max_rhat, gapok = m.gap != null && m.gap <= t.max_gap;
  $("tiles").innerHTML = [
    tile("Holdout R²", fmt.num(m.r2), r2ok ? "pass" : "fail", `${r2ok ? "Meets" : "Below"} target ${t.min_r2}`),
    tile("Holdout MAPE", fmt.pct(m.mape), "none", "Mean abs. % error"),
    tile("Max R-hat", fmt.num(m.max_rhat, 2), rhatok ? "pass" : "fail", rhatok ? `Converged (≤ ${t.max_rhat})` : `Not converged (> ${t.max_rhat})`),
    m.gap == null
      ? tile("Benchmark gap", "—", "none", "No benchmark set")
      : tile("Benchmark gap", fmt.pct(m.gap), gapok ? "pass" : "fail", `${gapok ? "Within" : "Above"} ${fmt.pct(t.max_gap)}`),
  ].join("");
  const key = `${run.id}:${JSON.stringify(state.benchmark_shares)}`;
  if (chartsShownFor !== key) loadCharts(run.id, key);
}

async function loadCharts(runId, key) {
  chartsShownFor = key;
  const resp = await fetch(`/api/runs/${runId}/charts?session_id=${sessionId}`);
  if (!resp.ok) return;
  const charts = await resp.json();
  for (const name of ["fit", "shares", "roi", "rhat"]) {
    const el = $(`chart-${name}`);
    el.closest(".card").hidden = !charts[name];
    if (charts[name]) vegaEmbed(el, charts[name], { actions: false, renderer: "svg" });
  }
}

function renderPriors() {
  const latest = state.runs[state.runs.length - 1];
  const bench = state.benchmark_shares;
  if (!priorsDirty) {
    const rows = Object.entries(state.priors).map(([ch, p]) => {
      const roi = latest?.metrics.roi[ch];
      return `<tr data-channel="${esc(ch)}">
        <td>${esc(ch)}</td>
        <td class="num"><input type="number" step="0.1" min="0.01" data-field="mean" value="${p.mean}"></td>
        <td class="num"><input type="number" step="0.1" min="0.01" data-field="sd" value="${p.sd}"></td>
        <td class="num">${bench ? fmt.pct(bench[ch]) : "—"}</td>
        <td class="num">${latest ? fmt.pct(latest.metrics.shares[ch]) : "—"}</td>
        <td class="num">${roi ? `${fmt.num(roi.mean, 2)} <span class="muted small">[${fmt.num(roi.ci_lo, 2)}–${fmt.num(roi.ci_hi, 2)}]</span>` : "—"}</td>
      </tr>`;
    });
    $("priorsTable").innerHTML = `<tr><th>Channel</th><th class="num">Prior ROI mean</th><th class="num">Prior ROI sd</th>
      <th class="num">Benchmark share</th><th class="num">Model share</th><th class="num">Posterior ROI [90% CI]</th></tr>${rows.join("")}`;
    $("priorsTable").querySelectorAll("input").forEach((i) => i.addEventListener("input", () => (priorsDirty = true)));
  }
  const s = state.settings, t = state.targets;
  $("settingsList").innerHTML = `<dt>Max lag</dt><dd>${s.max_lag} weeks</dd><dt>Baseline knots</dt><dd>${s.knots}</dd>
    <dt>Holdout</dt><dd>${fmt.pct(s.holdout_fraction, 0)} of geo-weeks (random)</dd><dt>Sampling</dt><dd>${s.sampling}</dd>`;
  $("targetsList").innerHTML = `<dt>Min holdout R²</dt><dd>${t.min_r2}</dd>
    <dt>Max benchmark gap</dt><dd>${fmt.pct(t.max_gap)}</dd><dt>Max R-hat</dt><dd>${t.max_rhat}</dd>`;
  $("guidanceCard").hidden = !state.guidance.length;
  $("guidanceList").innerHTML = state.guidance.map((g) => `<li>${esc(g)}</li>`).join("");
}

function renderRuns() {
  $("runsEmpty").hidden = state.runs.length > 0;
  $("runsList").innerHTML = [...state.runs].reverse().map((r) => {
    const m = r.metrics;
    const priors = Object.entries(r.priors).map(([c, p]) => `${esc(c)} ${fmt.num(p.mean, 2)}±${fmt.num(p.sd, 2)}`).join(" · ");
    return `<div class="run">
      <div class="run-head"><strong>Fit #${r.id}</strong><span class="tag">${r.source}</span>
        <span class="run-metrics">R² ${fmt.num(m.r2)} · MAPE ${fmt.pct(m.mape)} · R-hat ${fmt.num(m.max_rhat, 2)}
          · gap ${fmt.pct(m.gap)} · ${Math.round(r.seconds)}s</span></div>
      ${r.rationale ? `<div class="rationale">${esc(r.rationale)}</div>` : ""}
      <div class="priors">ROI priors: ${priors}</div>
    </div>`;
  }).join("");
}

function renderReports() {
  const file = state.reports[reportKind];
  $("reportsEmpty").hidden = Boolean(file);
  $("reportFrame").hidden = !file;
  const src = file ? `/reports/${file}` : "";
  if (file && $("reportFrame").getAttribute("src") !== src) $("reportFrame").setAttribute("src", src);
}

function renderBudget() {
  $("budgetEmpty").hidden = state.scenarios.length > 0;
  $("scenarioList").innerHTML = [...state.scenarios].reverse().map((s) => {
    const rows = Object.entries(s.spend).map(([ch, v]) => {
      const change = v.optimized / v.current - 1;
      return `<tr><td>${esc(ch)}</td><td class="num">${fmt.money(v.current)}</td><td class="num">${fmt.money(v.optimized)}</td>
        <td class="num ${change >= 0 ? "up" : "down"}">${change >= 0 ? "▲" : "▼"} ${fmt.pct(Math.abs(change))}</td></tr>`;
    }).join("");
    const lift = s.outcome_after / s.outcome_before - 1;
    return `<div class="card">
      <div class="panel-head"><h4 style="margin:0">Scenario #${s.id} · fit #${s.run_id} · budget ${fmt.money(s.budget)}</h4>
        <a class="btn ghost small" href="/reports/${s.report}" target="_blank">Full report ↗</a></div>
      <div class="tiles">
        ${tile("ROI", `${fmt.num(s.roi_before, 2)} → ${fmt.num(s.roi_after, 2)}`, "none", "Current → optimized")}
        ${tile("Incremental outcome", `${lift >= 0 ? "+" : ""}${fmt.pct(lift)}`, "none", `${fmt.money(s.outcome_before)} → ${fmt.money(s.outcome_after)}`)}
      </div>
      <table class="table"><tr><th>Channel</th><th class="num">Current spend</th><th class="num">Optimized</th><th class="num">Change</th></tr>${rows}</table>
    </div>`;
  }).join("");
}

setInterval(() => {
  if (!state?.job) return;
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - state.job.started));
  $("jobElapsed").textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}, 1000);

// ------------------------------------------------------------------ events

function handle(ev) {
  switch (ev.type) {
    case "state": state = ev.state; render(); break;
    case "agent_start": startAssistant(ev.notice); break;
    case "delta": appendText(ev.text); break;
    case "tool_call": addToolCall(ev); break;
    case "tool_result": addToolResult(ev); break;
    case "agent_end": endAssistant(); break;
    case "progress": $("jobProgress").textContent = ev.message; break;
    case "run": toast(`Fit #${ev.run.id} ready: R² ${fmt.num(ev.run.metrics.r2)}`); break;
    case "error": addMessage("msg error", ev.message); break;
  }
}

async function init() {
  ({ session_id: sessionId } = await post("/api/session", {}));
  const events = new EventSource(`/api/events?session_id=${sessionId}`);
  events.onmessage = (e) => handle(JSON.parse(e.data));
}
init();
