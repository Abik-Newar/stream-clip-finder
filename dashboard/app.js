/* ClipDesk: static GitHub-Pages dashboard for the stream-clip-finder system.
   Backend = the GitHub API itself (repo contents + actions).
   Admin gate = a repo-scoped GitHub token with push access (kept per-browser). */
"use strict";

const REPO = "Abik-Newar/stream-clip-finder";
const API = "https://api.github.com";
const DATA_DIR = "dashboard-data/clips";
const ARCHIVE_DAYS = 3;
const AUTH_KEY = "clipdesk_auth_v1";

const STATUS = ["not_started", "in_progress", "reviewing", "done"];
const LABEL = {
  not_started: "Not started",
  in_progress: "In progress",
  reviewing: "Reviewing",
  done: "Done",
};

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");

/* ------------------------------------------------------------------ utils */

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function relTime(iso) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(diff)) return "";
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function countdown(iso) {
  const left = ARCHIVE_DAYS * 864e5 - (Date.now() - new Date(iso).getTime());
  if (left <= 0) return "purging";
  const d = Math.floor(left / 864e5);
  const h = Math.floor((left % 864e5) / 36e5);
  return d > 0 ? `${d}d ${h}h left` : `${h}h left`;
}

function toast(msg, kind = "") {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

const ICONS = {
  play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg>',
  star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 3 2.7 5.9 6.3.7-4.7 4.3 1.3 6.2L12 17l-5.6 3.1 1.3-6.2L3 9.6l6.3-.7L12 3z"/></svg>',
  starFill: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 3 2.7 5.9 6.3.7-4.7 4.3 1.3 6.2L12 17l-5.6 3.1 1.3-6.2L3 9.6l6.3-.7L12 3z"/></svg>',
  playCircle: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l6-3.5-6-3.5z" fill="currentColor" stroke="none"/></svg>',
  restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v4h4"/></svg>',
  power: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3v9"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  external: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M14 5h5v5"/><path d="M19 5 9 15"/><path d="M19 14v5H5V5h5"/></svg>',
  logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>',
};

/* ------------------------------------------------------------------- state */

const state = {
  auth: null,            // { token, login }
  clips: [],             // [{ ...doc, _sha, _name }]
  shas: new Map(),
  system: null,          // { on, agents }
  filter: "all",         // all | starred | not_started | in_progress | reviewing
  channel: "all",
  busy: new Set(),
  loaded: false,
  error: null,
  timers: [],
};

class ApiError extends Error {
  constructor(status, detail) {
    super(`GitHub API ${status}${detail ? `: ${String(detail).slice(0, 180)}` : ""}`);
    this.status = status;
  }
}

async function gh(path, opts = {}) {
  const headers = {
    Accept: opts.raw ? "application/vnd.github.raw" : "application/vnd.github+json",
    Authorization: `Bearer ${state.auth.token}`,
  };
  const init = { method: opts.method || "GET", headers };
  if (opts.body) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(`${API}${path}`, init);
  } catch (err) {
    throw new ApiError(0, "network error");
  }
  if (res.status === 401) {
    logout("Session expired, sign in again.");
    throw new ApiError(401);
  }
  if (res.status === 404) return null;
  if (res.status === 204) return {};
  const text = await res.text();
  if (!res.ok) throw new ApiError(res.status, text);
  if (opts.raw) return text;
  return text ? JSON.parse(text) : {};
}

/* -------------------------------------------------------------------- auth */

function saveAuth(auth) {
  state.auth = auth;
  localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
}

function loadAuth() {
  try {
    const raw = JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    if (raw && raw.token && raw.login) return raw;
  } catch { /* ignore */ }
  return null;
}

function logout(msg) {
  localStorage.removeItem(AUTH_KEY);
  state.auth = null;
  state.timers.forEach(clearInterval);
  state.timers = [];
  renderLogin(msg);
}

async function loginWithToken(token) {
  const probe = { token, login: "" };
  const saved = state.auth;
  state.auth = probe;
  try {
    const me = await gh("/user");
    probe.login = me.login;
    const repo = await gh(`/repos/${REPO}`);
    if (!repo || !repo.permissions || !repo.permissions.push) {
      throw new Error("This token does not have push access to the repo. Ask the owner to add you as a collaborator.");
    }
    saveAuth(probe);
    await boot();
    toast(`Signed in as ${me.login}`, "ok");
  } catch (err) {
    state.auth = saved;
    throw err;
  }
}

/* -------------------------------------------------------------------- data */

async function loadClips() {
  const entries = await gh(`/repos/${REPO}/contents/${DATA_DIR}`);
  if (!entries) { state.clips = []; return; }
  const files = entries.filter((e) => e.type === "file" && e.name.endsWith(".json"));
  const results = await Promise.all(files.map(async (f) => {
    try {
      const raw = await gh(`/repos/${REPO}/contents/${DATA_DIR}/${f.name}`, { raw: true });
      return { ...JSON.parse(raw), _sha: f.sha, _name: f.name };
    } catch (err) {
      console.warn("clip load failed", f.name, err);
      return null;
    }
  }));
  state.clips = results.filter(Boolean).sort((a, b) =>
    String(b.created_at || "").localeCompare(String(a.created_at || "")));
  state.loaded = true;
  state.error = null;
}

async function saveClip(clip, patch, message) {
  const merged = { ...clip, ...patch };
  delete merged._sha;
  delete merged._name;
  const body = {
    message: message || `task: ${clip.clip_key} → ${merged.status || clip.status}`,
    content: b64encode(JSON.stringify(merged, null, 2) + "\n"),
    sha: clip._sha,
  };
  const res = await gh(`/repos/${REPO}/contents/${DATA_DIR}/${clip._name}`, {
    method: "PUT", body,
  });
  clip._sha = res.content.sha;
  Object.assign(clip, patch);
  render();
}

async function loadSystem() {
  try {
    const [wf, runs] = await Promise.all([
      gh(`/repos/${REPO}/actions/workflows?per_page=50`),
      gh(`/repos/${REPO}/actions/runs?per_page=30`),
    ]);
    const monitor = (wf.workflows || []).find((w) => (w.path || "").endsWith("monitor.yml"));
    const agents = new Map();
    for (const r of runs.workflow_runs || []) {
      const title = r.display_title || "";
      if (!title.startsWith("clip-capture|")) continue;
      if (r.status !== "in_progress" && r.status !== "queued") continue;
      if (!agents.has(title)) agents.set(title, r.created_at || "");
    }
    state.system = {
      on: monitor ? monitor.state === "active" : null,
      agents: [...agents.keys()].map((t) => ({ title: t.replace("clip-capture|", "") })),
    };
  } catch (err) {
    console.warn("system status failed", err);
  }
}

async function dispatch(workflow) {
  await gh(`/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST", body: { ref: "main" },
  });
}

const purgeCutoff = () => Date.now() - ARCHIVE_DAYS * 864e5;
const isPurged = (c) => c.status === "done" && c.done_at &&
  new Date(c.done_at).getTime() < purgeCutoff();
const isActive = (c) => c.status !== "done";
const isArchived = (c) => c.status === "done" && !isPurged(c);

function visibleClips() {
  let clips = state.clips.filter((c) => !isPurged(c));
  if (state.channel !== "all") clips = clips.filter((c) => c.channel === state.channel);
  if (state.filter === "starred") clips = clips.filter((c) => c.starred);
  else if (state.filter !== "all") clips = clips.filter((c) => c.status === state.filter);
  return clips;
}

/* ------------------------------------------------------------------ render */

function pillFor(c) {
  if (c.status === "done") return '<span class="pill pill-dn">Done</span>';
  if (c.status === "in_progress") return '<span class="pill pill-ip">In progress</span>';
  if (c.status === "reviewing") return '<span class="pill pill-rv">Reviewing</span>';
  return '<span class="pill pill-ns">Not started</span>';
}

function cardHTML(c) {
  const busy = state.busy.has(c.clip_key);
  const star = c.starred
    ? `<button class="iconbtn star-on" data-act="star" title="Remove star" ${busy ? "disabled" : ""}>${ICONS.starFill}</button>`
    : `<button class="iconbtn" data-act="star" title="Star as best clip" ${busy ? "disabled" : ""}>${ICONS.star}</button>`;
  const play = c.drive_id
    ? `<button class="videobtn" data-act="play">${ICONS.playCircle} Watch</button>`
    : `<button class="videobtn" disabled title="Drive link unavailable">${ICONS.playCircle} No link</button>`;

  const actions = isActive(c)
    ? `${play}
       <select class="statussel s-${c.status}" data-act="status" aria-label="Task status" ${busy ? "disabled" : ""}>
         ${STATUS.filter((s) => s !== "done").map((s) =>
           `<option value="${s}" ${c.status === s ? "selected" : ""}>${LABEL[s]}</option>`).join("")}
         <option value="done">${LABEL.done}</option>
       </select>`
    : `<span class="pill pill-warn">${ICONS.clock} ${countdown(c.done_at)}</span>
       <button class="btn btn-ghost btn-sm" data-act="restore" ${busy ? "disabled" : ""}>${ICONS.restore} Restore</button>
       ${c.drive_id ? `<button class="videobtn" data-act="play">${ICONS.playCircle} Watch</button>` : ""}`;

  return `
  <article class="card ${c.starred ? "starred" : ""} ${c.status === "done" ? "done" : ""}" data-key="${esc(c.clip_key)}">
    <div class="card-top">
      <span class="chbadge"><span class="plat">${esc(c.platform)}</span><span class="nm">${esc(c.channel)}</span></span>
      <span class="spacer"></span>
      ${star}
    </div>
    <div class="card-meta">
      <span class="meta-item">${ICONS.clock} ${relTime(c.created_at)}</span>
      <span class="meta-item mono">${fmtDur(c.duration)}</span>
      <span class="scorechip">score ${Number(c.score || 0).toFixed(1)}</span>
      ${pillFor(c)}
    </div>
    ${c.trigger ? `<div class="trigger-line">${esc(c.trigger)}</div>` : ""}
    <div class="card-actions">${actions}</div>
  </article>`;
}

function renderSystem() {
  const s = state.system;
  const pill = $("#syspill");
  const chip = $("#agentchip");
  if (!s) return;
  if (s.on === true) {
    pill.className = "syspill on";
    pill.innerHTML = '<span class="dot"></span> System ON';
  } else if (s.on === false) {
    pill.className = "syspill off";
    pill.innerHTML = '<span class="dot"></span> System OFF';
  } else {
    pill.className = "syspill";
    pill.innerHTML = '<span class="dot"></span> Status unknown';
  }
  const n = s.agents.length;
  chip.className = `agentchip ${n ? "show" : ""}`;
  chip.innerHTML = n
    ? `<b>${n}</b> agent${n === 1 ? "" : "s"} live · ${esc(s.agents.map((a) => a.title).join(", "))}`
    : "";
  const start = $("#btn-start");
  const stop = $("#btn-stop");
  if (start) start.disabled = s.on === true;
  if (stop) stop.disabled = s.on === false;
}

function boardHTML() {
  const clips = visibleClips();
  const active = clips.filter(isActive);
  const archive = clips.filter(isArchived);
  const channels = [...new Set(state.clips.map((c) => c.channel))].sort();
  const starredCount = state.clips.filter((c) => c.starred && !isPurged(c)).length;

  const chip = (id, label, extra = "") =>
    `<button class="chip ${state.filter === id ? "active" : ""}" data-filter="${id}">${label}${extra}</button>`;

  const filters = `
  <div class="filterbar">
    ${chip("all", "All active")}
    ${chip("starred", `<span class="star-ic">${ICONS.starFill}</span> Starred`, starredCount ? ` <b>${starredCount}</b>` : "")}
    ${chip("not_started", "Not started")}
    ${chip("in_progress", "In progress")}
    ${chip("reviewing", "Reviewing")}
    <span class="spacer"></span>
    <select class="chip" data-act="channel">
      <option value="all" ${state.channel === "all" ? "selected" : ""}>All channels</option>
      ${channels.map((ch) => `<option value="${esc(ch)}" ${state.channel === ch ? "selected" : ""}>${esc(ch)}</option>`).join("")}
    </select>
  </div>`;

  const activeGrid = active.length
    ? `<div class="grid">${active.map(cardHTML).join("")}</div>`
    : `<div class="statebox"><h3>No clips here yet</h3><p>Agents pull hype moments automatically. New clips appear as tasks within a minute.</p></div>`;

  const archiveSection = archive.length
    ? `<div class="section-title">Archive · done tasks <span class="count">${archive.length}</span>
         <span class="hint">kept ${ARCHIVE_DAYS} days, restorable, then purged</span></div>
       <div class="grid">${archive.map(cardHTML).join("")}</div>`
    : "";

  return `
  <div class="wrap">
    ${filters}
    ${state.error ? `<div class="banner">${esc(state.error)}</div>` : ""}
    <div class="section-title">Review tasks <span class="count">${active.length}</span></div>
    ${activeGrid}
    ${archiveSection}
    <div class="footer">
      <span>Clips live in Google Drive · tasks stored in-repo</span>
      <a href="https://github.com/${REPO}/actions" target="_blank" rel="noopener">Workflows</a>
      <a href="https://github.com/${REPO}" target="_blank" rel="noopener">Repo</a>
    </div>
  </div>`;
}

function render() {
  const board = $("#board");
  if (!board) return;
  board.innerHTML = boardHTML();
  renderSystem();
}

function topbarHTML() {
  return `
  <header class="topbar">
    <div class="wrap topbar-inner">
      <span class="brand">
        <img src="./icons/icon-192.png" alt="" />
        ClipDesk <small>clip control room</small>
      </span>
      <span class="spacer"></span>
      <span class="syspill" id="syspill"><span class="dot"></span> …</span>
      <span class="agentchip" id="agentchip"></span>
      <button class="btn btn-primary btn-sm" id="btn-start" data-act="start">${ICONS.power} Start</button>
      <button class="btn btn-danger btn-sm" id="btn-stop" data-act="stop">${ICONS.stop} Stop</button>
      <button class="iconbtn" data-act="logout" title="Sign out">${ICONS.logout}</button>
    </div>
  </header>
  <main id="board" class="wrap"></main>`;
}

/* ------------------------------------------------------------------ player */

function openPlayer(clip) {
  const root = $("#modal-root");
  root.innerHTML = `
  <div class="modal-back" data-act="closemodal">
    <div class="modal" role="dialog" aria-label="Clip player">
      <div class="modal-head">
        <span class="chbadge"><span class="plat">${esc(clip.platform)}</span><span class="nm">${esc(clip.channel)}</span></span>
        <span class="spacer"></span>
        <span class="meta-item mono">${fmtDur(clip.duration)}</span>
        <button class="iconbtn" data-act="closemodal" title="Close">✕</button>
      </div>
      <iframe src="https://drive.google.com/file/d/${esc(clip.drive_id)}/preview" allow="autoplay" allowFullScreen></iframe>
      <div class="modal-foot">
        ${pillFor(clip)}
        <span class="meta-item">${relTime(clip.created_at)}</span>
        ${clip.trigger ? `<span class="meta-item">${esc(clip.trigger)}</span>` : ""}
        <span class="spacer"></span>
        <a class="videobtn" href="https://drive.google.com/file/d/${esc(clip.drive_id)}/view" target="_blank" rel="noopener">${ICONS.external} Open in Drive</a>
      </div>
    </div>
  </div>`;
  document.body.style.overflow = "hidden";
}

function closePlayer() {
  $("#modal-root").innerHTML = "";
  document.body.style.overflow = "";
}

/* ------------------------------------------------------------------ events */

document.addEventListener("click", async (event) => {
  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;

  if (act === "closemodal") { closePlayer(); return; }

  if (act === "logout") { logout(); return; }

  if (act === "start" || act === "stop") {
    btn.disabled = true;
    try {
      await dispatch(act === "start" ? "start.yml" : "stop.yml");
      toast(act === "start" ? "Start dispatched — system is coming up" : "Stop dispatched — agents are being cancelled", "ok");
      setTimeout(loadSystem, 4000);
      setTimeout(loadSystem, 12000);
    } catch (err) {
      toast(`Failed: ${err.message}`, "err");
      btn.disabled = false;
    }
    return;
  }

  const card = btn.closest(".card");
  if (!card) return;
  const clip = state.clips.find((c) => c.clip_key === card.dataset.key);
  if (!clip) return;

  if (act === "play") { openPlayer(clip); return; }

  if (act === "star") {
    state.busy.add(clip.clip_key);
    try { await saveClip(clip, { starred: !clip.starred }); }
    catch (err) { toast(`Failed: ${err.message}`, "err"); }
    state.busy.delete(clip.clip_key);
    render();
    return;
  }

  if (act === "restore") {
    state.busy.add(clip.clip_key);
    try {
      await saveClip(clip, { status: "not_started", done_at: null }, `task: ${clip.clip_key} restored`);
      toast("Task restored as Not started", "ok");
    } catch (err) { toast(`Failed: ${err.message}`, "err"); }
    state.busy.delete(clip.clip_key);
    render();
    return;
  }
});

document.addEventListener("change", async (event) => {
  const sel = event.target.closest('[data-act="status"]');
  if (sel) {
    const card = sel.closest(".card");
    const clip = state.clips.find((c) => c.clip_key === card.dataset.key);
    if (!clip) return;
    const status = sel.value;
    state.busy.add(clip.clip_key);
    try {
      await saveClip(clip, {
        status,
        done_at: status === "done" ? new Date().toISOString() : null,
      }, `task: ${clip.clip_key} → ${status}`);
      if (status === "done") toast("Moved to archive · video purged from the board, kept 3 days", "ok");
    } catch (err) {
      toast(`Failed: ${err.message}`, "err");
      render();
    }
    state.busy.delete(clip.clip_key);
    return;
  }
  const chan = event.target.closest('[data-act="channel"]');
  if (chan) { state.channel = chan.value; render(); }
});

document.addEventListener("click", (event) => {
  if (event.target.closest('[data-filter]')) {
    state.filter = event.target.closest("[data-filter]").dataset.filter;
    render();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePlayer();
});

/* ------------------------------------------------------------------- login */

function renderLogin(message) {
  app.innerHTML = `
  <div class="login-wrap">
    <div class="login-card">
      <div class="brand"><img src="./icons/icon-192.png" alt="" /> ClipDesk</div>
      <p class="login-sub">Admin sign in with a GitHub token</p>
      <div class="field">
        <label for="tok">GitHub token</label>
        <input id="tok" type="password" autocomplete="off" spellcheck="false"
               placeholder="github_pat_… or ghp_…" />
        ${message ? `<div class="err">${esc(message)}</div>` : ""}
      </div>
      <button class="btn btn-primary login-btn" id="login-go">Sign in</button>
      <details class="howto">
        <summary>How do I get a token?</summary>
        <ol>
          <li>Open <code>github.com → Settings → Developer settings → Fine-grained tokens</code></li>
          <li>Generate new token · Repository access: <b>only</b> <code>${REPO}</code></li>
          <li>Permissions: <b>Actions: read+write</b>, <b>Contents: read+write</b></li>
          <li>Paste it above. You must be a collaborator on the repo.</li>
        </ol>
        <p class="safe">The token is stored only in this browser's local storage and is never sent anywhere except api.github.com.</p>
      </details>
    </div>
  </div>`;
  $("#login-go").addEventListener("click", onLogin);
  $("#tok").addEventListener("keydown", (e) => { if (e.key === "Enter") onLogin(); });
  $("#tok").focus();
}

async function onLogin() {
  const input = $("#tok");
  const btn = $("#login-go");
  const token = input.value.trim();
  if (!token) { input.focus(); return; }
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    await loginWithToken(token);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Sign in";
    const msg = err.status === 401
      ? "Token rejected by GitHub. Check that it is valid and not expired."
      : err.message;
    renderLogin(msg);
  }
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  app.innerHTML = topbarHTML();
  render();
  try {
    await Promise.all([loadClips(), loadSystem()]);
  } catch (err) {
    state.error = `Could not load data: ${err.message}`;
  }
  render();

  state.timers.forEach(clearInterval);
  state.timers = [
    setInterval(async () => {
      try { await loadClips(); if (!document.hidden) render(); }
      catch (err) { state.error = err.message; }
    }, 20000),
    setInterval(async () => {
      await loadSystem();
      if (!document.hidden) renderSystem();
    }, 30000),
  ];
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadClips().then(render).catch(() => {}); loadSystem().then(renderSystem); }
  });
}

function init() {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () =>
      navigator.serviceWorker.register("./sw.js").catch(() => {}));
  }
  const auth = loadAuth();
  if (auth) {
    state.auth = auth;
    boot().catch((err) => {
      console.error(err);
      renderLogin("Could not load the desk. Check your connection and sign in again.");
    });
  } else {
    renderLogin();
  }
}

init();
