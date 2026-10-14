/* model_eval control panel — vanilla JS, no build step.
 *
 * Everything reads from /api/state, which is served from the same SQLite
 * database scripts/run-eval.sh writes to. Poll fast while a job is running,
 * slowly when idle, and never keep more than one in flight.
 */

"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const UI = {
  view: "leaderboard",
  state: null,
  busy: false,
  lbSort: { key: "rank", asc: true },
  runFilter: { q: "", stage: "", status: "" },
  selectedRun: null,
  runTab: "summary",
  poller: null,
};

/* ------------------------------------------------------------------ utils */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function num(v, digits = 1) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function compact(v) {
  const n = Number(v || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function secs(s) {
  const n = Number(s || 0);
  if (n < 60) return n.toFixed(0) + "s";
  if (n < 3600) return (n / 60).toFixed(0) + "m";
  return (n / 3600).toFixed(1) + "h";
}

function ago(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const d = Math.max(0, Date.now() - t);
  if (d < 60e3) return Math.round(d / 1e3) + "s ago";
  if (d < 3600e3) return Math.round(d / 60e3) + "m ago";
  if (d < 864e5) return Math.round(d / 3600e3) + "h ago";
  return Math.round(d / 864e5) + "d ago";
}

function toast(msg, kind = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + (kind === "error" ? "err" : kind === "ok" ? "ok" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 6000);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new Error("server unreachable — is webui.py still running?");
  }
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; }
  catch { data = { error: text.slice(0, 400) }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ----------------------------------------------------------------- router */

function setView(name) {
  UI.view = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "view-" + name));
  render();
  if (name === "runs") loadRuns();
  if (name === "jobs") UI._jobsLoaded = false;
}

/* ------------------------------------------------------------------ state */

async function refreshState() {
  if (UI.busy) return;
  UI.busy = true;
  try {
    UI.state = await api("/api/state");
    render();
    $("#hdr-status").textContent = UI.state.running ? "running" : "idle";
  } catch (e) {
    $("#hdr-status").textContent = "offline";
    $("#hdr-status").style.color = "var(--bad)";
  } finally {
    UI.busy = false;
    schedule();
  }
}

function schedule() {
  clearTimeout(UI.poller);
  const fast = UI.state && UI.state.running;
  UI.poller = setTimeout(refreshState, fast ? 2500 : 12000);
}

function render() {
  const s = UI.state;
  if (!s) return;
  $("#hdr-pending").textContent = `${s.pending} pending · ${s.counts.runs} runs`;
  $("#hdr-pending").className = "pill" + (s.pending > 0 ? " warn" : "");
  $("#hdr-clock").textContent = s.generated ? ago(s.generated) : "—";

  renderLeaderboard(s);
  renderRuns(s);
  renderTasks(s);
  renderModels(s);
  renderJobs(s);
  renderSettings(s);
}

/* ------------------------------------------------------------ leaderboard */

function renderLeaderboard(s) {
  const lb = s.leaderboard || { rows: [], generated: null };
  const rows = (lb.rows || []).slice();
  const profile = $("#lb-profile").value;
  $("#lb-generated").textContent = lb.generated
    ? `written ${ago(lb.generated)} · profile: ${lb.profile || "all tasks"}`
    : "no results yet";

  $("#lb-empty").classList.toggle("hidden", rows.length > 0);
  $("#lb-table").closest(".table-wrap").classList.toggle("hidden", rows.length === 0);

  const { key, asc } = UI.lbSort;
  rows.sort((a, b) => {
    const av = key === "rank" ? 0 : a[key];
    const bv = key === "rank" ? 0 : b[key];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    const c = typeof av === "string" ? av.localeCompare(bv) : av - bv;
    return asc ? c : -c;
  });

  $$("#lb-table thead th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === key);
    th.classList.toggle("asc", asc);
  });

  const tbody = $("#lb-table tbody");
  tbody.innerHTML = rows.map((r, i) => {
    const cells = [
      `<td class="num">${i + 1}</td>`,
      `<td><code>${esc(r.model)}</code>${r.enabled ? "" : ' <span class="badge warn">off</span>'}` +
        (r.status === "delisted" ? ' <span class="badge err">delisted</span>' :
         r.status === "degraded" ? ' <span class="badge warn">degraded</span>' : "") + "</td>",
      scoreCell(r.composite, 30),
      num(r.plan_rubric),
      num(r.code_rubric),
      num(r.tests),
      `<td class="num">${r.tasks_scored}/${r.tasks_total}</td>`,
      `<td class="num">${secs(r.wall_clock_s)}</td>`,
      `<td class="num">${compact(r.tokens_out)}</td>`,
      `<td class="num">${compact(r.tool_calls)}</td>`,
      `<td class="num">${r.failures ? `<span class="badge err">${r.failures}</span>` : "0"}</td>`,
      `<td class="num">${r.judge_n == null ? "—" : r.judge_n}</td>`,
      num(r.judge_spread, 2),
    ];
    return `<tr class="clickable" data-model="${esc(r.model)}">${cells.join("")}</tr>`;
  }).join("");

  $$("#lb-table tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => selectModelDetail(tr.dataset.model));
  });
}

function scoreCell(v, max) {
  if (v === null || v === undefined) return '<td class="num" title="not measured">—</td>';
  const pct = Math.max(0, Math.min(100, v));
  return `<td class="num"><span class="bar-cell" style="width:46px"><i style="width:${pct}%"></i></span> ${num(v)}</td>`;
}

function selectModelDetail(model) {
  const s = UI.state;
  const row = (s.leaderboard.rows || []).find((r) => r.model === model);
  const el = $("#lb-detail");
  if (!row) { el.innerHTML = ""; return; }
  const tasks = row.tasks || [];
  el.innerHTML = `
    <div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap">
      <span class="score-big">${row.composite === null || row.composite === undefined ? "—" : num(row.composite)}</span>
      <h2 style="margin:0;font-size:14px"><code>${esc(row.model)}</code></h2>
      <span class="hint">last run ${ago(row.last_run)} · ${secs(row.wall_clock_s)} wall ·
        ${compact(row.tokens_in)}/${compact(row.tokens_out)} tokens ·
        ${row.judge_n ?? "—"} judge(s) · judge spread ${num(row.judge_spread, 2)}</span>
    </div>
    <table style="margin-top:12px">
      <thead><tr><th>Task</th><th class="num">Score</th><th class="num">Plan</th>
        <th class="num">Code</th><th class="num">Tests</th></tr></thead>
      <tbody>
        ${tasks.map((t) => {
          const c = t.components || {};
          return `<tr><td><code>${esc(t.task)}</code></td><td class="num">${num(t.score)}</td>
            <td class="num">${num(c.judge_plan)}</td><td class="num">${num(c.judge_code)}</td>
            <td class="num">${num(c.tests)}</td></tr>`;
        }).join("")}
      </tbody>
    </table>`;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* -------------------------------------------------------------------- runs */

async function loadRuns() {
  const params = new URLSearchParams();
  if (UI.runFilter.stage) params.set("stage", UI.runFilter.stage);
  if (UI.runFilter.status) params.set("status", UI.runFilter.status);
  try {
    const data = await api("/api/runs?" + params.toString());
    renderRunRows(data.runs || []);
  } catch (e) { toast(e.message, "error"); }
}

function renderRunRows(runs) {
  const q = UI.runFilter.q.toLowerCase();
  const filtered = q
    ? runs.filter((r) => (r.model + " " + r.task_id + " " + r.id).toLowerCase().includes(q))
    : runs;
  $("#runs-count").textContent = `${filtered.length} run(s)`;
  const tbody = $("#runs-table tbody");
  tbody.innerHTML = filtered.map((r) => {
    const badge = { ok: "ok", error: "err", timeout: "warn", cancelled: "warn",
                    running: "warn", skipped: "dim" }[r.status] || "";
    const tests = r.stage === "verify" ? `${r.tests_passed}/${r.tests_total}` : "";
    return `<tr class="clickable ${UI.selectedRun === r.id ? "selected" : ""}" data-id="${esc(r.id)}">
      <td title="${esc(r.started)}">${ago(r.started)}</td>
      <td><code>${esc(r.model)}</code></td>
      <td><code>${esc(r.task_id)}</code></td>
      <td>${esc(r.stage)}${r.source && r.source !== "own" ? " <span class='badge'>" + esc(r.source) + "</span>" : ""}</td>
      <td><span class="badge ${badge}">${esc(r.status)}</span></td>
      <td class="num">${secs(r.duration_s)}</td>
      <td class="num">${compact(r.tokens_out)}</td>
      <td class="num">${tests}</td>
      <td class="num">${r.score === null || r.score === undefined ? "—" : num(r.score)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="hint">no runs yet</td></tr>`;

  $$("#runs-table tbody tr.clickable").forEach((tr) => {
    tr.addEventListener("click", () => selectRun(tr.dataset.id));
  });
}

async function selectRun(id) {
  UI.selectedRun = id;
  UI.runTab = "summary";
  $$("#runs-table tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.id === id));
  const el = $("#run-detail");
  el.innerHTML = `<p class="hint">loading…</p>`;
  try {
    const d = await api("/api/run/" + encodeURIComponent(id));
    el._data = d;
    renderRunDetail();
  } catch (e) { el.innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

function renderRunDetail() {
  const d = $("#run-detail")._data;
  const el = $("#run-detail");
  if (!d || d.error) { el.innerHTML = `<p class="hint">${esc((d && d.error) || "no run selected")}</p>`; return; }

  const tabs = ["summary", "plan", "diff", "verify", "checks", "judges", "log"];
  const labels = { summary: "Summary", plan: "Plan", diff: "Diff", verify: "Verify",
                   checks: "Checks", judges: "Judges", log: "Events" };
  const avail = tabs.filter((t) => {
    if (t === "summary") return true;
    if (t === "plan") return d.plan;
    if (t === "diff") return d.diff;
    if (t === "verify") return d.verify;
    if (t === "checks") return d.checks;
    if (t === "judges") return (d.judges || []).length > 0;
    if (t === "log") return d.events_tail;
    return false;
  });
  if (!avail.includes(UI.runTab)) UI.runTab = "summary";

  let body = "";
  if (UI.runTab === "summary") {
    const rows = [
      ["Run id", `<code>${esc(d.id)}</code>`], ["Model", `<code>${esc(d.model)}</code>`],
      ["Task", `<code>${esc(d.task_id)}</code>`], ["Stage", esc(d.stage)],
      ["Source", esc(d.source || "own")], ["Status", esc(d.status)],
      ["Started", esc(d.started)], ["Finished", esc(d.finished || "—")],
      ["Duration", secs(d.duration_s)],
      ["Attempts", d.attempts],
      ["Tokens in / out", `${compact(d.tokens_in)} / ${compact(d.tokens_out)}`],
      ["Reasoning tokens", compact(d.tokens_reasoning)],
      ["Tool calls", `${d.tool_calls} (${d.shell_calls} shell, ${d.edit_calls} edit)`],
      ["Files touched", d.files_touched],
      ["Tests", d.stage === "verify" ? `${d.tests_passed}/${d.tests_total}` : "—"],
      ["Session", `<code>${esc(d.session_id || "—")}</code>`],
      ["Cache key", `<code>${esc((d.key || "").slice(0, 16))}</code>…`],
      ["Error", d.error ? `<span class="badge err">${esc(d.error)}</span>` : "—"],
    ];
    body = `<dl class="kv">${rows.map(([k, v]) =>
      `<dt>${k}</dt><dd>${v === null || v === undefined ? "—" : v}</dd>`).join("")}</dl>`;
  } else if (UI.runTab === "judges") {
    body = (d.judges || []).map((j) => {
      const p = j.payload_parsed || {};
      const dims = p.dimensions || {};
      return `<div class="judge-row">
          <div><code>${esc(j.judge_model)}</code>
            <div class="dims">${Object.entries(dims).map(([k, v]) =>
              `<span class="chip">${esc(k)}=${esc(v)}</span>`).join("")}</div>
            ${p.notes ? `<div class="hint" style="margin-top:5px;white-space:normal">${esc(p.notes)}</div>` : ""}
            ${(p.fabrication || p.violations || []).length
              ? `<div class="hint" style="color:var(--warn);white-space:normal">⚠ ${(p.fabrication || p.violations).map(esc).join("; ")}</div>` : ""}
          </div>
          <div class="mono">${j.score === null || j.score === undefined ? "—" : num(j.score)}</div>
        </div>`;
    }).join("") || `<p class="hint">no judge results</p>`;
  } else {
    const map = { plan: d.plan, diff: d.diff, verify: d.verify, checks: d.checks, log: d.events_tail };
    body = `<pre class="code">${esc(map[UI.runTab] || "")}</pre>`;
  }

  el.innerHTML = `
    <div class="tabs">${avail.map((t) =>
      `<button class="${t === UI.runTab ? "active" : ""}" data-tab="${t}">${labels[t]}</button>`).join("")}
      <span style="flex:1"></span>
      <button class="ghost" data-tab="__close">✕</button>
    </div>${body}`;

  $$("#run-detail .tabs button").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.tab === "__close") { el.innerHTML = `<p class="hint">Select a run.</p>`; el._data = null; return; }
      UI.runTab = b.dataset.tab; renderRunDetail();
    });
  });
}

/* ------------------------------------------------------------------ tasks */

function renderTasks(s) {
  const list = $("#tasks-list");
  list.innerHTML = (s.tasks || []).map((t) => `
    <div class="card ${t.enabled ? "" : "off"}" data-task="${esc(t.id)}">
      <h3>${esc(t.id)}</h3>
      <div class="hint">${esc(t.name)}</div>
      <p class="desc">${esc(t.description || "").slice(0, 320)}</p>
      <div class="row">
        <label class="chk"><input type="checkbox" data-k="enabled" ${t.enabled ? "checked" : ""}> enabled</label>
        <label>weight <input type="number" step="0.1" min="0" data-k="weight" value="${t.weight}" style="width:70px"></label>
      </div>
      <div class="facts">
        <span class="chip">profiles: ${esc((t.profiles || []).join(", "))}</span>
        <span class="chip">stages: ${esc((t.stages || []).join(" → "))}</span>
        <span class="chip">hidden ${t.hidden_tests_active}/${t.hidden_tests_total} active</span>
        <span class="chip">checks ${t.active_checks.length}/${t.checks.length}</span>
        <span class="chip" title="fixture fingerprint">${esc(t.fixture_sha.slice(0, 10))}</span>
      </div>
      <div class="row"><span class="hint">scoring</span>
        <span class="chips">${Object.entries(t.scoring).map(([k, v]) =>
          `<span class="chip on">${esc(k)} ${(v * 100).toFixed(0)}%</span>`).join("")}</span></div>
      <div class="row"><span class="hint">judges</span>
        <span class="chips">${t.judge_models.map((j) =>
          `<span class="chip">${esc(j)}</span>`).join("")}</span></div>
      ${hiddenTestChips(t)}
      ${checkChips(t)}
    </div>`).join("");

  $$("#tasks-list .card").forEach((card) => {
    const taskId = card.dataset.task;
    card.querySelectorAll("[data-k]").forEach((inp) => {
      inp.addEventListener("change", async () => {
        const key = inp.dataset.k;
        const value = inp.type === "checkbox" ? inp.checked
          : inp.type === "number" ? Number(inp.value) : inp.value;
        try {
          await api("/api/task/" + encodeURIComponent(taskId), { method: "POST", body: { [key]: value } });
          toast(`${taskId}: ${key} = ${value}`, "ok");
          refreshState();
        } catch (e) { toast(e.message, "error"); refreshState(); }
      });
    });
    card.querySelectorAll("[data-toggle-test]").forEach((chip) => {
      chip.addEventListener("click", async () => {
        const t = UI.state.tasks.find((x) => x.id === taskId);
        const next = chip.classList.contains("on")
          ? t.excluded_tests.concat(chip.dataset.toggleTest)
          : t.excluded_tests.filter((x) => x !== chip.dataset.toggleTest);
        try {
          await api("/api/task/" + encodeURIComponent(taskId),
            { method: "POST", body: { exclude_tests: next } });
          toast(`${taskId}: excluded ${next.length} hidden test(s)`, "ok");
          refreshState();
        } catch (e) { toast(e.message, "error"); }
      });
    });
    card.querySelectorAll("[data-toggle-check]").forEach((chip) => {
      chip.addEventListener("click", async () => {
        const t = UI.state.tasks.find((x) => x.id === taskId);
        const next = chip.classList.contains("on")
          ? t.excluded_checks.concat(chip.dataset.toggleCheck)
          : t.excluded_checks.filter((x) => x !== chip.dataset.toggleCheck);
        try {
          await api("/api/task/" + encodeURIComponent(taskId),
            { method: "POST", body: { exclude_checks: next } });
          toast(`${taskId}: excluded ${next.length} check(s)`, "ok");
          refreshState();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  });
}

function hiddenTestChips(t) {
  if (!t.hidden_tests_total) return "";
  return `<div class="row"><span class="hint">hidden tests (click to disable)</span>
    <span class="chips">${t.hidden_tests_all.map((x) => {
      const on = !t.excluded_tests.includes(x);
      return `<span class="chip ${on ? "on" : "off"}" data-toggle-test="${esc(x)}">${esc(x)}</span>`;
    }).join("")}</span></div>`;
}

function checkChips(t) {
  if (!t.checks.length) return "";
  return `<div class="row"><span class="hint">checks (click to disable)</span>
    <span class="chips">${t.checks.map((x) => {
      const on = !t.excluded_checks.includes(x);
      return `<span class="chip ${on ? "on" : "off"}" data-toggle-check="${esc(x)}">${esc(x.replace(/^check_/, ""))}</span>`;
    }).join("")}</span></div>`;
}

/* ----------------------------------------------------------------- models */

function renderModels(s) {
  const tbody = $("#models-table tbody");
  tbody.innerHTML = (s.models || []).map((m) => {
    const badge = { known: "ok", new: "", degraded: "warn", delisted: "err" }[m.status] || "";
    return `<tr data-model="${esc(m.id)}">
      <td><code>${esc(m.id)}</code></td>
      <td><span class="badge ${badge}">${esc(m.status)}</span></td>
      <td><input type="checkbox" data-k="enabled" ${m.enabled ? "checked" : ""}></td>
      <td><input type="number" step="0.1" min="0" data-k="weight" value="${m.weight}" style="width:70px"></td>
      <td class="hint">${esc((m.first_seen || "").slice(0, 10))}</td>
      <td class="hint" title="${esc(m.last_full_run || "")}">${ago(m.last_seen)}</td>
      <td class="hint" style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc(m.notes || "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="hint">no models discovered yet</td></tr>`;

  $$("#models-table tbody tr[data-model]").forEach((tr) => {
    const id = tr.dataset.model;
    tr.querySelectorAll("[data-k]").forEach((inp) => {
      inp.addEventListener("change", async () => {
        const key = inp.dataset.k;
        const value = inp.type === "checkbox" ? inp.checked : Number(inp.value);
        try {
          await api("/api/model/" + encodeURIComponent(id), { method: "POST", body: { [key]: value } });
          toast(`${id}: ${key} = ${value}`, "ok");
          refreshState();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  });
}

/* ------------------------------------------------------------------- jobs */

async function renderJobs(s) {
  const job = s.job;
  const running = !!s.running;
  $("#job-start").disabled = running;
  $("#job-pause").disabled = !running;
  $("#job-stop").disabled = !running;
  $("#job-progress").classList.toggle("hidden", !job);
  if (job) {
    const total = Number(job.progress_total || 0);
    const done = Number(job.progress_done || 0);
    const pct = total > 0 ? (done / total) * 100 : 0;
    $("#job-fill").style.width = (running ? Math.max(2, pct) : pct) + "%";
    $("#job-progress-text").textContent =
      `${job.status}${running ? "" : " · " + (job.finished ? ago(job.finished) : "")} — ${done}/${total}`;
    $("#job-current").textContent = job.current ? `now: ${job.current} — ${job.message || ""}` : (job.message || "");
  }

  if (!UI._jobsLoaded || running) {
    try {
      const data = await api("/api/jobs");
      const tbody = $("#jobs-table tbody");
      tbody.innerHTML = (data.jobs || []).map((j) => {
        const badge = { running: "warn", done: "ok", failed: "err", paused: "warn",
                        stopped: "dim" }[j.status] || "";
        return `<tr>
          <td>${ago(j.started)}</td><td>${esc(j.mode || "")}</td>
          <td><span class="badge ${badge}">${esc(j.status)}</span></td>
          <td>${esc(j.control || "")}</td>
          <td class="num">${j.progress_done || 0}/${j.progress_total || 0}</td>
          <td class="hint" style="max-width:420px;overflow:hidden;text-overflow:ellipsis">${esc(j.message || "")}</td>
        </tr>`;
      }).join("") || `<tr><td colspan="6" class="hint">no jobs yet</td></tr>`;
      UI._jobsLoaded = true;
    } catch (e) { /* non-fatal */ }
  }

  if (running && (!UI._logLoaded || UI._lastLogMode !== "open")) {
    $("#job-log").dataset.open = "1";
  }
  const logBox = $("#job-log");
  if (logBox.dataset.open === "1" || running) {
    try {
      const data = await api("/api/log?lines=160");
      logBox.textContent = (data.lines || []).join("");
      logBox.scrollTop = logBox.scrollHeight;
    } catch (e) { /* non-fatal */ }
  }
}

async function startJob() {
  const body = {
    mode: $("#job-mode").value,
    concurrency: Number($("#job-conc").value || 3),
    budget_minutes: Number($("#job-budget").value || 0) || null,
    force: $("#job-force").checked,
    no_judge: $("#job-nojudge").checked,
  };
  const models = $("#job-models").value.trim();
  const tasks = $("#job-tasks").value.trim();
  if (models) body.models = models.split(",").map((x) => x.trim()).filter(Boolean);
  if (tasks) body.tasks = tasks.split(",").map((x) => x.trim()).filter(Boolean);
  try {
    const res = await api("/api/job/start", { method: "POST", body });
    toast(`job started (${res.log ? res.log.split("/").pop() : "ok"})`, "ok");
    $("#job-msg").textContent = "spawned — the runner picks this up in a moment";
    setTimeout(refreshState, 600);
  } catch (e) { toast(e.message, "error"); }
}

async function controlJob(action) {
  try {
    const res = await api("/api/job/control", { method: "POST", body: { action } });
    toast(`${action} sent to ${res.job_id}`, "ok");
    setTimeout(refreshState, 500);
  } catch (e) { toast(e.message, "error"); }
}

/* --------------------------------------------------------------- settings */

function renderSettings(s) {
  const st = s.settings || {};
  if (document.activeElement === $("#set-conc") ||
      document.activeElement === $("#set-budget") ||
      document.activeElement === $("#set-judges")) return;
  $("#set-conc").value = st.concurrency ?? 3;
  $("#set-budget").value = st.budget_minutes ?? "";
  const judges = st.judge_models;
  $("#set-judges").value = Array.isArray(judges) ? judges.join(", ")
    : (judges == null ? "" : String(judges));
  $("#set-env").innerHTML = [
    ["State dir", st.state_dir], ["Live log", st.live_log],
    ["Runs", s.counts.runs], ["Judge results", s.counts.judges],
    ["Models", s.models.length], ["Tasks", (s.tasks || []).length],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v === undefined || v === null ? "—" : v)}</dd>`).join("");
}

async function saveSettings() {
  const body = {
    concurrency: Number($("#set-conc").value || 3),
    budget_minutes: Number($("#set-budget").value || 0) || null,
    judge_models: $("#set-judges").value.trim() || null,
  };
  try {
    await api("/api/settings", { method: "POST", body });
    $("#set-msg").textContent = "saved " + new Date().toLocaleTimeString();
    toast("settings saved", "ok");
    refreshState();
  } catch (e) { toast(e.message, "error"); }
}

/* ------------------------------------------------------------------ wiring */

document.addEventListener("DOMContentLoaded", () => {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

  $("#lb-profile").addEventListener("change", () => {
    const p = $("#lb-profile").value;
    api("/api/score", { method: "POST", body: {} })
      .then(() => toast("re-scored with profile filter applied", "ok"))
      .catch((e) => toast(e.message, "error"))
      .then(refreshState);
    void p;
  });
  $("#lb-rescore").addEventListener("click", () => {
    api("/api/score", { method: "POST", body: {} })
      .then((r) => toast(`re-scored ${r.rows} row(s)`, "ok"))
      .catch((e) => toast(e.message, "error"))
      .then(refreshState);
  });
  $$("#lb-table thead th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      UI.lbSort = { key, asc: UI.lbSort.key === key ? !UI.lbSort.asc : key === "model" };
      render();
    });
  });

  $("#runs-filter").addEventListener("input", (e) => { UI.runFilter.q = e.target.value; loadRuns(); });
  $("#runs-stage").addEventListener("change", (e) => { UI.runFilter.stage = e.target.value; loadRuns(); });
  $("#runs-status").addEventListener("change", (e) => { UI.runFilter.status = e.target.value; loadRuns(); });

  $("#job-start").addEventListener("click", startJob);
  $("#job-pause").addEventListener("click", () => controlJob("pause"));
  $("#job-stop").addEventListener("click", () => {
    if (confirm("Stop the running job? In-flight units finish; the rest is rescheduled next run.")) {
      controlJob("stop");
    }
  });

  $("#models-refresh").addEventListener("click", async () => {
    try {
      const r = await api("/api/models/refresh", { method: "POST", body: {} });
      toast(`${r.available.length} models, ${r.new.length} new, ${r.delisted.length} delisted`, "ok");
      refreshState();
    } catch (e) { toast(e.message, "error"); }
  });

  $("#set-save").addEventListener("click", saveSettings);

  refreshState();
  loadRuns();
});
