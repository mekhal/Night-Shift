"use strict";

// All data is inserted with textContent only; nothing from the API is parsed as HTML.

const POLL_MS = 5000;
const FETCH_TIMEOUT_MS = 4000;
const HEARTBEAT_STALE_S = 180; // supervisor beats every 30 s
const CLOCK_SKEW_S = 60;
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const DISTRO = "privasheet-dev";

const STATUS_CLASS = {
  ready: "", in_progress: "warn", merged: "ok", would_merge: "ok", needs_human: "bad", skipped: "",
  retry: "warn", quota: "warn", aborted: "", error: "bad", interrupted: "warn", running: "warn", unfinished: "bad",
};
const COUNT_ORDER = ["ready", "in_progress", "merged", "would_merge", "needs_human", "skipped"];

let lastOkAt = null;
let lastTablesKey = "";

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  if (opts.cls) node.className = opts.cls;
  if (opts.href) node.setAttribute("href", opts.href);
  if (opts.label) node.setAttribute("aria-label", opts.label);
  for (const child of children) node.appendChild(child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text; // avoid re-announcing unchanged live text
}

function parseTime(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d : null;
}

function localTime(iso) {
  const d = parseTime(iso);
  return d ? d.toLocaleString() : "—";
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "unknown";
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${s} s`;
  return `${s} s`;
}

function pill(text, cls) {
  return el("span", { text, cls: "pill " + (cls || "") });
}

function table(node, headers, rows) {
  clear(node);
  if (!rows.length) {
    node.appendChild(el("tbody", {}, [el("tr", {}, [el("td", { text: "Nothing yet.", cls: "empty" })])]));
    return;
  }
  node.appendChild(el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]));
  node.appendChild(el("tbody", {}, rows.map((cells) => el("tr", {}, cells))));
}

function td(content, cls) {
  if (content instanceof Node) return el("td", { cls }, [content]);
  return el("td", { text: content ?? "—", cls });
}

function alertBox(kind, title, body) {
  const box = el("div", { cls: "alert " + kind }, [el("strong", { text: title })]);
  if (body) box.appendChild(el("pre", { text: body }));
  return box;
}

// Supervisor health from the heartbeat: "alive", "stale", "skew" or "never".
function health(st, now) {
  const beat = parseTime(st.heartbeat);
  if (!beat) return { kind: "never", age: NaN };
  const age = (now - beat) / 1000;
  if (age < -CLOCK_SKEW_S) return { kind: "skew", age };
  if (age > HEARTBEAT_STALE_S) return { kind: "stale", age };
  return { kind: "alive", age };
}

function userIsReading() {
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) return true;
  const active = document.activeElement;
  return Boolean(active && active !== document.body && active.closest("table"));
}

function renderHeader(st, h) {
  const mode = document.getElementById("mode");
  setText(mode, "AUTOPILOT: " + (st.mode || "unknown"));
  mode.className = "pill " + ({ on: "ok", "on-limited": "ok", "dry-run": "warn", off: "bad" }[st.mode] || "");

  const alive = document.getElementById("alive");
  const text = {
    alive: "supervisor running",
    stale: `no heartbeat for ${duration(h.age)}`,
    skew: "clock mismatch — heartbeat is in the future",
    never: "supervisor not started",
  }[h.kind];
  setText(alive, text);
  alive.className = "pill " + (h.kind === "alive" ? "ok" : "bad");
}

function renderAlerts(st, limits, now) {
  const alerts = document.getElementById("alerts");
  clear(alerts);
  if (st.breaker_open === "1") {
    alerts.appendChild(alertBox("bad", "Circuit breaker open — no tasks will run",
      (st.breaker_reason || "") + "\nAfter fixing the cause, run: nightshift reset-breaker"));
  }
  for (const [provider, limit] of Object.entries(limits || {})) {
    const resume = parseTime(limit.resume_after);
    if (!resume || resume <= now) continue;
    alerts.appendChild(alertBox("warn",
      `${provider} usage limit — free again ${resume.toLocaleString()} (in ${duration((resume - now) / 1000)})`,
      limit.message));
  }
  if (st.config_error) alerts.appendChild(alertBox("bad", "config.toml error", st.config_error));
  if (st.tasks_error) alerts.appendChild(alertBox("bad", "Task file error", st.tasks_error));
  if (st.last_error) alerts.appendChild(alertBox("bad", "Last supervisor error", st.last_error));
}

function renderNow(st, h, tasks, now) {
  const main = document.getElementById("now-main");
  const sub = document.getElementById("now-sub");
  const current = st.current_task && tasks.get(st.current_task);
  if (current && h.kind === "alive") {
    setText(main, `${current.task_id} · ${current.title}`);
    const started = parseTime(st.step_started_at);
    const elapsed = started ? duration((now - started) / 1000) : "unknown";
    setText(sub, `step: ${current.step || "starting"} (${elapsed}) · attempt ${current.attempts} · review: ${current.review}`);
    return;
  }
  if (h.kind !== "alive") {
    setText(main, "Supervisor is not running");
    setText(sub, current ? `last known task: ${current.task_id} (${current.step || "—"})` : "The scheduled task restarts it within 15 minutes.");
    return;
  }
  const labels = {
    idle: "Idle — no eligible task", off: "Stopped (AUTOPILOT=off)", breaker: "Stopped by circuit breaker",
    "quota-wait": "Waiting for usage limit reset", "config-error": "Waiting — config.toml has an error",
  };
  setText(main, labels[st.last_action] || st.last_action || "Waiting");
  setText(sub, `last heartbeat ${localTime(st.heartbeat)}`);
}

function renderTables(data, st, h) {
  const key = JSON.stringify([data.tasks, data.runs, data.needs_human, st.current_task, h.kind === "alive"]);
  if (key === lastTablesKey || userIsReading()) return;
  lastTablesKey = key;

  const counts = document.getElementById("counts");
  clear(counts);
  const addCount = (label, n) => counts.appendChild(el("div", { cls: "count" }, [el("div", { cls: "n", text: n }), el("div", { cls: "k", text: label })]));
  for (const status of COUNT_ORDER) addCount(status.replace("_", " "), data.tasks.filter((t) => t.status === status).length);
  addCount("total", data.tasks.length);

  const humanBox = document.getElementById("human");
  clear(humanBox);
  if (!data.needs_human.length) {
    humanBox.appendChild(el("p", { cls: "empty", text: "Nothing waiting for you." }));
  } else {
    const t = el("table");
    table(t, ["Task", "Type", "Created", "Reason", "Answer with (choose retry or skip, score 1–5)"], data.needs_human.map((item) => {
      const command = TASK_ID.test(item.task_id)
        ? `wsl -d ${DISTRO} -u agent -- nightshift answer ${item.task_id} <retry|skip> --score <1-5>`
        : "invalid task id — answer manually";
      return [
        td(item.task_id, "mono"), td(pill(item.type, "bad")), td(localTime(item.created)),
        td(el("a", { text: "open", href: `/needs-human/${encodeURIComponent(item.file)}`, label: `Reason for ${item.task_id}` })),
        td(command, "mono detail"),
      ];
    }));
    humanBox.appendChild(el("div", { cls: "scroll" }, [t]));
  }

  table(document.getElementById("tasks"), ["ID", "Title", "Tier", "Review", "Status", "Step", "Attempts", "Last run"],
    data.tasks.map((t) => [
      td(t.task_id, "mono"), td(t.title, "detail"), td(t.tier), td(t.review),
      td(pill(t.status, STATUS_CLASS[t.status])), td(t.status === "in_progress" ? t.step : "—"),
      td(t.attempts), td(t.last_run, "mono"),
    ]));

  table(document.getElementById("runs"), ["Run", "Task", "Started", "Outcome", "Detail", "Logs"],
    data.runs.map((r) => {
      let outcome = r.outcome;
      if (!outcome) outcome = h.kind === "alive" && r.task_id === st.current_task ? "running" : "unfinished";
      const files = el("span", { cls: "files" }, r.files.map((f) => el("a", {
        text: f, href: `/runs/${encodeURIComponent(r.run_id)}/${encodeURIComponent(f)}`, label: `${f} for run ${r.run_id}`,
      })));
      return [
        td(r.run_id, "mono"), td(r.task_id, "mono"), td(localTime(r.started_at)),
        td(pill(outcome, STATUS_CLASS[outcome] ?? "warn")), td(r.detail, "detail"), td(files),
      ];
    }));
}

function render(data) {
  const st = data.state;
  const now = parseTime(data.server_time) || new Date();
  const h = health(st, now);
  const tasks = new Map(data.tasks.map((t) => [t.task_id, t]));
  renderHeader(st, h);
  renderAlerts(st, data.limits, now);
  renderNow(st, h, tasks, now);
  renderTables(data, st, h);
}

function markStale(message) {
  document.body.classList.add("stale");
  const alive = document.getElementById("alive");
  setText(alive, message);
  alive.className = "pill bad";
  setText(document.getElementById("updated"), lastOkAt ? `data from ${lastOkAt.toLocaleTimeString()} (stale)` : "no data yet");
}

async function refresh() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let data;
  try {
    const resp = await fetch("/api/status", { cache: "no-store", signal: controller.signal });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    data = await resp.json();
  } catch (err) {
    markStale("dashboard unreachable");
    return;
  } finally {
    clearTimeout(timer);
  }
  try {
    render(data);
  } catch (err) {
    markStale("display error — see browser console");
    console.error(err);
    return;
  }
  lastOkAt = new Date();
  document.body.classList.remove("stale");
  setText(document.getElementById("updated"), "updated " + lastOkAt.toLocaleTimeString());
}

async function loop() {
  await refresh();
  setTimeout(loop, POLL_MS); // next poll only after this one finished: no overlap, no out-of-order results
}

loop();
