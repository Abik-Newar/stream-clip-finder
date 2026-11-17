/* ClipDesk: static GitHub-Pages dashboard for the stream-clip-finder system.
   Backend = the GitHub API itself (repo contents + actions).
   Admin gate = a repo-scoped GitHub token with push access (kept per-browser).

   Pages: Board (review tasks) · Agents (9 slots + manual) · Manual (paste a
   live URL and start an agent on it). */
"use strict";

const REPO = "Abik-Newar/stream-clip-finder";
const API = "https://api.github.com";
const DATA_DIR = "dashboard-data/clips";
const AGENTS_PATH = "dashboard-data/agents.json";
const ARCHIVE_DAYS = 3;
const AUTH_KEY = "clipdesk_auth_v1";
const SLOT_PLATFORM = { f: "twitch", "twitch-1": "twitch", "twitch-2": "twitch", "twitch-3": "twitch",
  "kick-1": "kick", "kick-2": "kick", "kick-3": "kick",
  "youtube-1": "youtube", "youtube-2": "youtube", "youtube-3": "youtube" };

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
  dot: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="5"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 .9h8a1 1 0 0 0 1-.9L18 7"/></svg>',
};

/* ------------------------------------------------------------------- state */

const state = {
  auth: null,
  clips: [],
  view: "board",         // board | agents | manual
  filter: "all",
  channel: "all",
  system: null,          // { on, agents }
  agentsDoc: null,       // { slots: {name: {enabled}}, sha }
  runs: [],              // [{slot, platform, channel, status, id, created}]
  agentState: {},        // {slot: {platform, channel, run_id, since}}
  busy: new Set(),
  stopping: new Map(),   // slot → ts: runs are being cancelled, show "Stopping…"
  starting: 0,           // ts of the last master Start — "Starting…" until confirmed
  loaded: false,
  error: null,
  lastSync: null,
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

async function deleteClip(clipKey) {
  const meta = await gh(`/repos/${REPO}/contents/${DATA_DIR}/${clipKey}.json`);
  if (!meta || !meta.sha) throw new ApiError(404, "clip file not found");
  await gh(`/repos/${REPO}/contents/${DATA_DIR}/${clipKey}.json`, {
    method: "DELETE",
    body: { message: `clip: delete ${clipKey}`, sha: meta.sha },
  });
}

/* ------------------------------------------------------- agents (9 slots) */

async function loadAgents() {
  const [listing, runsJson, stateList] = await Promise.all([
    gh(`/repos/${REPO}/contents/${AGENTS_PATH}`),
    gh(`/repos/${REPO}/actions/runs?per_page=40`),
    gh(`/repos/${REPO}/contents/dashboard-data/agent-state`).catch(() => null),
  ]);
  if (listing && typeof listing.content === "string") {
    try {
      const b64 = listing.content.replace(/\s/g, "");
      const doc = JSON.parse(atob(b64));
      state.agentsDoc = { slots: doc.slots || {}, sha: listing.sha };
    } catch {
      state.agentsDoc = state.agentsDoc || { slots: {}, sha: listing.sha };
    }
  } else if (listing === null) {
    state.agentsDoc = { slots: {}, sha: null };
  }
  const runs = [];
  for (const r of runsJson.workflow_runs || []) {
    const parts = (r.display_title || "").split("|");
    if (parts.length >= 4 && parts[0] === "clip-capture") {
      runs.push({
        slot: parts[3], platform: parts[1], channel: parts[2],
        status: r.status, id: r.id, created: r.created_at,
      });
    }
  }
  state.runs = runs;
  state.agentState = {};
  if (Array.isArray(stateList)) {
    const docs = await Promise.all(stateList
      .filter((f) => f.type === "file" && f.name.endsWith(".json"))
      .map(async (f) => {
        try {
          return JSON.parse(await gh(
            `/repos/${REPO}/contents/dashboard-data/agent-state/${f.name}`,
            { raw: true }));
        } catch { return null; }
      }));
    for (const d of docs.filter(Boolean)) state.agentState[d.slot] = d;
  }
  // clear "Stopping…" markers once the runs are really gone (or after 3 min)
  for (const slot of [...state.stopping.keys()]) {
    const stillRunning = state.runs.some((r) => r.slot === slot &&
      (r.status === "in_progress" || r.status === "queued"));
    if (!stillRunning || Date.now() - state.stopping.get(slot) > 180000) {
      state.stopping.delete(slot);
    }
  }
}

async function saveSlot(slot, enabled) {
  const doc = { slots: { ...(state.agentsDoc ? state.agentsDoc.slots : {}) } };
  doc.slots[slot] = { enabled };
  const body = {
    message: `agents: ${slot} ${enabled ? "enabled" : "stopped"}`,
    content: b64encode(JSON.stringify(doc, null, 2) + "\n"),
    sha: state.agentsDoc ? state.agentsDoc.sha : undefined,
  };
  try {
    const res = await gh(`/repos/${REPO}/contents/${AGENTS_PATH}`, { method: "PUT", body });
    state.agentsDoc = { slots: doc.slots, sha: res.content.sha };
  } catch (err) {
    if (err.status === 409 || err.status === 422) {   // sha race — re-read and retry once
      await loadAgents();
      const doc2 = { slots: { ...(state.agentsDoc.slots || {}) } };
      doc2.slots[slot] = { enabled };
      const res = await gh(`/repos/${REPO}/contents/${AGENTS_PATH}`, {
        method: "PUT",
        body: {
          message: `agents: ${slot} ${enabled ? "enabled" : "stopped"}`,
          content: b64encode(JSON.stringify(doc2, null, 2) + "\n"),
          sha: state.agentsDoc.sha,
        },
      });
      state.agentsDoc = { slots: doc2.slots, sha: res.content.sha };
    } else { throw err; }
  }
}

async function commitAgents(on) {
  // Master start/stop: flip every slot in agents.json (the workflows do the
  // same server-side — this is the fast browser-side commit).
  const slots = { ...(state.agentsDoc ? state.agentsDoc.slots : {}) };
  for (const s of Object.keys(SLOT_PLATFORM)) slots[s] = { enabled: on };
  const msg = `agents: master ${on ? "start" : "stop"} — all slots ${on ? "enabled" : "disabled"}`;
  try {
    const res = await gh(`/repos/${REPO}/contents/${AGENTS_PATH}`, {
      method: "PUT",
      body: {
        message: msg,
        content: b64encode(JSON.stringify({ slots }, null, 2) + "\n"),
        sha: state.agentsDoc ? state.agentsDoc.sha : undefined,
      },
    });
    state.agentsDoc = { slots, sha: res.content.sha };
  } catch (err) {
    if (err.status === 409 || err.status === 422) {   // sha race — retry once
      await loadAgents();
      const slots2 = { ...(state.agentsDoc ? state.agentsDoc.slots : {}) };
      for (const s of Object.keys(SLOT_PLATFORM)) slots2[s] = { enabled: on };
      const res = await gh(`/repos/${REPO}/contents/${AGENTS_PATH}`, {
        method: "PUT",
        body: {
          message: msg,
          content: b64encode(JSON.stringify({ slots: slots2 }, null, 2) + "\n"),
          sha: state.agentsDoc ? state.agentsDoc.sha : undefined,
        },
      });
      state.agentsDoc = { slots: slots2, sha: res.content.sha };
    } else { throw err; }
  }
}

async function dispatchCapture(platform, slot, channel = "", threshold = "") {
  await gh(`/repos/${REPO}/actions/workflows/capture.yml/dispatches`, {
    method: "POST",
    body: { ref: "main", inputs: { platform, slot, channel, threshold } },
  });
}

async function cancelRun(runId) {
  await gh(`/repos/${REPO}/actions/runs/${runId}/cancel`, { method: "POST" });
}

async function dispatch(workflow) {
  await gh(`/repos/${REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST", body: { ref: "main" },
  });
}

async function loadSystem() {
  try {
    const [wf, v] = await Promise.all([
      gh(`/repos/${REPO}/actions/workflows?per_page=50`),
      gh(`/repos/${REPO}/actions/variables/SCF_RUNNING`).catch(() => null),
    ]);
    const monitor = (wf.workflows || [])
      .find((w) => (w.path || "").endsWith("monitor.yml"));
    const monitorActive = monitor ? monitor.state === "active" : false;
    const runningVar = !!(v && v.value === "true");
    const enabledSlots = state.agentsDoc
      ? Object.values(state.agentsDoc.slots || {})
          .filter((s) => s && s.enabled).length
      : 0;
    const activeRuns = state.runs.some((r) =>
      r.status === "in_progress" || r.status === "queued");
    // Start is confirmed once anything real is up (workflows queue first)
    if ((monitorActive || runningVar || activeRuns) && state.starting) {
      state.starting = 0;
    }
    state.system = {
      on: monitorActive || runningVar || enabledSlots > 0 || activeRuns,
      starting: state.starting > 0 && Date.now() - state.starting < 180000,
    };
  } catch (err) {
    console.warn("system status failed", err);
  }
}

function slotState(slot) {
  const run = state.runs.find((r) => r.slot === slot);
  const enabled = state.agentsDoc && state.agentsDoc.slots &&
    state.agentsDoc.slots[slot] && state.agentsDoc.slots[slot].enabled;
  return { run, enabled: !!enabled };
}

/* ------------------------------------------------------------ clip helpers */

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
  const del = `<button class="iconbtn del" data-act="delete" title="Delete this clip from the board (the video file stays in Google Drive)" ${busy ? "disabled" : ""}>${ICONS.trash}</button>`;
  const watch = c.drive_id
    ? `<button class="videobtn" data-act="play">${ICONS.playCircle} Watch</button>
       <a class="videobtn" title="Open in Drive (new tab)" target="_blank" rel="noopener"
          href="https://drive.google.com/file/d/${esc(c.drive_id)}/view">${ICONS.external}</a>`
    : `<span class="meta-item" title="The Drive share link for this clip is not created yet">link pending</span>`;

  const actions = isActive(c)
    ? `${watch}
       <select class="statussel s-${c.status}" data-act="status" aria-label="Task status" ${busy ? "disabled" : ""}>
         ${STATUS.filter((s) => s !== "done").map((s) =>
           `<option value="${s}" ${c.status === s ? "selected" : ""}>${LABEL[s]}</option>`).join("")}
         <option value="done">${LABEL.done}</option>
       </select>`
    : `<span class="pill pill-warn">${ICONS.clock} ${countdown(c.done_at)}</span>
       <button class="btn btn-ghost btn-sm" data-act="restore" ${busy ? "disabled" : ""}>${ICONS.restore} Restore</button>
       ${c.drive_id
         ? `<button class="videobtn" data-act="play">${ICONS.playCircle} Watch</button>
            <a class="videobtn" title="Open in Drive (new tab)" target="_blank" rel="noopener"
               href="https://drive.google.com/file/d/${esc(c.drive_id)}/view">${ICONS.external}</a>`
         : ""}`;

  return `
  <article class="card ${c.starred ? "starred" : ""} ${c.status === "done" ? "done" : ""}" data-key="${esc(c.clip_key)}">
    <div class="card-top">
      <span class="chbadge"><span class="plat">${esc(c.platform)}</span><span class="nm">${esc(c.channel)}</span></span>
      <span class="spacer"></span>
      ${star}
      ${del}
    </div>
    <div class="card-meta">
      <span class="meta-item">${ICONS.clock} ${relTime(c.created_at)}</span>
      <span class="meta-item mono">${fmtDur(c.duration)}</span>
      <span class="scorechip">score ${Number(c.score || 0).toFixed(1)}</span>
      ${pillFor(c)}
    </div>
    ${c.trigger ? `<div class="trigger-line">${esc(c.trigger)}</div>` : ""}
    ${c.title ? `<div class="clip-title">${ICONS.play}${esc(c.title)}</div>` : ""}
    <div class="card-actions">${actions}</div>
  </article>`;
}

function syncLabel() {
  if (!state.lastSync) return "";
  return `<span class="meta-item synced">synced ${relTime(state.lastSync)}</span>`;
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
      ${syncLabel()}
      <span class="syspill" id="syspill"><span class="dot"></span> …</span>
      <button class="btn btn-primary btn-sm" id="btn-start" data-act="start">${ICONS.power} Start all</button>
      <button class="btn btn-danger btn-sm" id="btn-stop" data-act="stop">${ICONS.stop} Stop all</button>
      <button class="iconbtn" data-act="logout" title="Sign out">${ICONS.logout}</button>
    </div>
    <div class="wrap tabs">
      <button class="tab ${state.view === "board" ? "active" : ""}" data-view="board">Board</button>
      <button class="tab ${state.view === "agents" ? "active" : ""}" data-view="agents">Agents <span class="tabcount" id="agent-count"></span></button>
      <button class="tab ${state.view === "manual" ? "active" : ""}" data-view="manual">Manual capture</button>
    </div>
  </header>
  <main id="page"></main>`;
}

function renderSystem() {
  const s = state.system;
  const pill = $("#syspill");
  if (!s || !pill) return;
  if (s.starting) {
    pill.className = "syspill starting";
    pill.innerHTML = '<span class="dot"></span> Starting… · waiting for the runner queue';
  } else if (s.on === true) {
    pill.className = "syspill on";
    pill.innerHTML = '<span class="dot"></span> System ON';
  } else if (s.on === false) {
    pill.className = "syspill off";
    pill.innerHTML = '<span class="dot"></span> System OFF';
  } else {
    pill.className = "syspill";
    pill.innerHTML = '<span class="dot"></span> Status unknown';
  }
  const start = $("#btn-start");
  const stop = $("#btn-stop");
  if (start) start.disabled = s.on === true || s.starting;
  if (stop) stop.disabled = s.on === false;
  const live = state.runs.filter((r) => r.status === "in_progress" || r.status === "queued").length;
  const count = $("#agent-count");
  if (count) count.textContent = live ? String(live) : "";
}

function renderBoard() {
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
    : `<div class="statebox"><h3>No clips here yet</h3><p>Agents pull hype moments automatically. New tasks appear within seconds.</p></div>`;

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

function slotRowHTML(slot) {
  const platform = SLOT_PLATFORM[slot] || "twitch";
  const { run, enabled } = slotState(slot);
  const st = state.agentState[slot] || {};
  const busy = state.busy.has(`slot:${slot}`);
  let status;
  if (state.stopping.has(slot)) {
    status = '<span class="pill pill-ip">Stopping…</span>';
  } else if (run && run.status === "in_progress") {
    const ch = (st.channel && st.channel !== "auto")
      ? `${st.platform}/${st.channel}`
      : (run.channel && run.channel !== "auto" ? `${run.platform}/${run.channel}` : "");
    status = `<span class="pill pill-dn">${ICONS.dot} Live${ch ? ` · ${esc(ch)}` : " · picking a channel"}</span>`;
  } else if (run && run.status === "queued") {
    status = '<span class="pill pill-ip">Starting…</span>';
  } else if (enabled) {
    status = '<span class="pill pill-ns">On · waiting for a live channel</span>';
  } else {
    status = '<span class="pill pill-ns">Stopped</span>';
  }
  const btn = enabled
    ? `<button class="btn btn-danger btn-sm" data-slotact="stop" data-slot="${slot}" ${busy ? "disabled" : ""}>${ICONS.stop} Stop</button>`
    : `<button class="btn btn-primary btn-sm" data-slotact="start" data-slot="${slot}" ${busy ? "disabled" : ""}>${ICONS.power} Start</button>`;
  return `
  <div class="slotrow">
    <span class="slotname mono">${esc(slot)}</span>
    ${status}
    <span class="spacer"></span>
    ${btn}
  </div>`;
}

function renderAgents() {
  const groups = ["twitch", "kick", "youtube"].map((platform) => {
    const slots = Object.keys(SLOT_PLATFORM).filter((s) => SLOT_PLATFORM[s] === platform).sort();
    const manual = state.runs.filter((r) => r.slot.startsWith("manual") && r.platform === platform);
    return `
    <div class="slotgroup">
      <div class="section-title">${platform} agents <span class="count">3</span></div>
      <div class="slotcard">${slots.map(slotRowHTML).join("")}
        ${manual.map((r) => `
          <div class="slotrow">
            <span class="slotname mono">${esc(r.slot)}</span>
            <span class="pill ${r.status === "in_progress" ? "pill-dn" : "pill-ip"}">${ICONS.dot} ${r.status === "in_progress" ? `Live · ${esc(r.channel)}` : "Starting…"}</span>
            <span class="spacer"></span>
            <button class="btn btn-danger btn-sm" data-act="cancelrun" data-run="${r.id}" title="Cancel this agent">${ICONS.stop} Stop</button>
          </div>`).join("")}
      </div>
    </div>`;
  }).join("");

  return `
  <div class="wrap">
    <div class="section-title">The nine agents
      <span class="hint">each slot picks the next live channel of its platform automatically · stopped slots stay off</span>
    </div>
    <div class="agentgrid">${groups}</div>
    <div class="footer">
      <span>Slot switches are stored in-repo (dashboard-data/agents.json) — the monitor keeps enabled slots alive.</span>
    </div>
  </div>`;
}

function renderManual() {
  return `
  <div class="wrap">
    <div class="section-title">Manual capture <span class="hint">paste a live channel link — an agent watches it and pulls clips</span></div>
    <div class="manualcard">
      <div class="field">
        <label for="murl">Stream link (Twitch, Kick or YouTube)</label>
        <input id="murl" type="url" spellcheck="false" placeholder="https://kick.com/rachitroo_1" />
        <div class="err" id="murl-err"></div>
      </div>
      <div id="murl-preview"></div>
      <button class="btn btn-primary" id="murl-go" disabled>${ICONS.power} Start agent on this link</button>
      <p class="manualnote">The agent captures the live stream, detects hype moments and files clips on the Board automatically. It keeps watching until the live ends.</p>
    </div>
  </div>`;
}

function parseStreamUrl(raw) {
  const url = String(raw || "").trim();
  let m = url.match(/^(?:https?:\/\/)?(?:www\.)?twitch\.tv\/([\w-]{2,})/i);
  if (m) return { platform: "twitch", name: m[1] };
  m = url.match(/^(?:https?:\/\/)?(?:www\.)?kick\.com\/([\w-]{2,})/i);
  if (m) return { platform: "kick", name: m[1] };
  m = url.match(/^(?:https?:\/\/)?(?:www\.)?youtube\.com\/@([\w.-]{2,})/i);
  if (m) return { platform: "youtube", name: `@${m[1]}` };
  m = url.match(/^(?:https?:\/\/)?(?:www\.)?youtube\.com\/channel\/(UC[\w-]{10,})/i);
  if (m) return { platform: "youtube", name: m[1] };
  return null;
}

function render() {
  const page = $("#page");
  if (!page) return;
  page.innerHTML =
    state.view === "board" ? renderBoard() :
    state.view === "agents" ? renderAgents() : renderManual();
  renderSystem();
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
        ${clip.title ? `<span class="meta-item">${esc(clip.title)}</span>` : ""}
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
  const tab = event.target.closest("[data-view]");
  if (tab) {
    state.view = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.view === state.view));
    render();
    return;
  }

  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;

  if (act === "closemodal") { closePlayer(); return; }
  if (act === "logout") { logout(); return; }

  if (act === "start" || act === "stop") {
    btn.disabled = true;
    const on = act === "start";
    try {
      await dispatch(on ? "start.yml" : "stop.yml");
      state.system = { on };   // optimistic, confirmed by the next poll
      // flip every agent slot immediately so the rows match the master switch
      if (state.agentsDoc) {
        state.agentsDoc.slots = Object.fromEntries(
          Object.keys(SLOT_PLATFORM).map((s) => [s, { enabled: on }]));
      }
      if (on) {
        state.stopping.clear();
        state.starting = Date.now();
        // dispatch enabled slots directly so rows light up in seconds
        // (the monitor nurse remains the 10-minute fallback)
        const doc = state.agentsDoc ? state.agentsDoc.slots || {} : {};
        for (const slot of Object.keys(SLOT_PLATFORM)) {
          if (!doc[slot] || !doc[slot].enabled) continue;
          if (state.runs.some((r) => r.slot === slot)) continue;
          state.runs.push({ slot, platform: SLOT_PLATFORM[slot], channel: "",
            status: "queued", id: 0, created: new Date().toISOString() });
          dispatchCapture(SLOT_PLATFORM[slot], slot).catch(() => {});
        }
      } else {
        state.starting = 0;
        for (const r of state.runs) {
          try { await cancelRun(r.id); } catch { /* best effort */ }
        }
        for (const s of Object.keys(SLOT_PLATFORM)) state.stopping.set(s, Date.now());
      }
      render();
      // commit agents.json too (stop.yml/start.yml also do it server-side)
      commitAgents(on).catch(() => {});
      toast(on ? "Start dispatched — enabled slots are coming up" : "Stop dispatched — agents are being cancelled", "ok");
      // staged rechecks — workflows queue on free runners, so confirm slowly
      for (const ms of [5000, 15000, 35000, 75000]) {
        setTimeout(() => Promise.all([loadSystem(), loadAgents()])
          .then(render).catch(() => {}), ms);
      }
    } catch (err) {
      state.system = null;
      toast(`Failed: ${err.message}`, "err");
      btn.disabled = false;
    }
    return;
  }

  const slotBtn = event.target.closest("[data-slotact]");
  if (slotBtn) {
    const slot = slotBtn.dataset.slot;
    const start = slotBtn.dataset.slotact === "start";
    const stopRuns = state.runs.filter((x) => x.slot === slot);
    // instant flip — button + pill change BEFORE any network call
    state.busy.add(`slot:${slot}`);
    if (state.agentsDoc) {
      state.agentsDoc.slots = { ...(state.agentsDoc.slots || {}),
        [slot]: { enabled: start } };
    }
    state.runs = state.runs.filter((r) => r.slot !== slot);
    if (start) {
      state.stopping.delete(slot);
      state.runs.push({ slot, platform: SLOT_PLATFORM[slot] || "twitch",
        channel: "", status: "queued", id: 0,
        created: new Date().toISOString() });
    } else {
      state.stopping.set(slot, Date.now());
    }
    render();
    try {
      await saveSlot(slot, start);
      if (start) {
        await dispatchCapture(SLOT_PLATFORM[slot] || "twitch", slot);
        toast(`${slot} starting — picking a live channel`, "ok");
      } else {
        for (const r of stopRuns) {
          try { await cancelRun(r.id); } catch { /* best effort */ }
        }
        toast(`${slot} stopped — runs cancelled`, "ok");
        // the monitor can dispatch in the seconds around a stop — sweep again
        setTimeout(async () => {
          try {
            await loadAgents();
            for (const r of state.runs.filter((x) => x.slot === slot)) {
              try { await cancelRun(r.id); } catch { /* best effort */ }
            }
            await loadAgents();
          } catch { /* keep going */ }
          render();
        }, 5000);
      }
      setTimeout(() => loadAgents().then(render).catch(() => {}), 3000);
    } catch (err) {
      toast(`Failed: ${err.message}`, "err");
      // revert the optimistic flip so the UI matches reality again
      if (state.agentsDoc) state.agentsDoc.slots[slot] = { enabled: !start };
      if (start) state.runs = state.runs.filter((r) => r.slot !== slot);
      state.stopping.delete(slot);
      setTimeout(() => loadAgents().then(render).catch(() => {}), 500);
    }
    state.busy.delete(`slot:${slot}`);
    if (start) state.stopping.delete(slot);   // stop markers purge via loadAgents
    render();
    return;
  }

  if (act === "cancelrun") {
    try {
      await cancelRun(btn.dataset.run);
      toast("Agent cancelled", "ok");
      setTimeout(() => loadAgents().then(render).catch(() => {}), 3000);
    } catch (err) { toast(`Failed: ${err.message}`, "err"); }
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

  if (act === "delete") {
    if (!confirm(`Delete "${clip.clip_key}" from the board?\n\nThe video file stays in Google Drive — this only removes the task row.`)) return;
    state.busy.add(clip.clip_key);
    render();
    try {
      await deleteClip(clip.clip_key);
      state.clips = state.clips.filter((c) => c.clip_key !== clip.clip_key);
      toast("Clip deleted from the board (video stays in Drive)", "ok");
    } catch (err) { toast(`Failed: ${err.message}`, "err"); }
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
      if (status === "done") toast("Moved to archive · kept 3 days, restorable", "ok");
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
  if (event.target.closest("[data-filter]")) {
    state.filter = event.target.closest("[data-filter]").dataset.filter;
    render();
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id !== "murl") return;
  const parsed = parseStreamUrl(event.target.value);
  const err = $("#murl-err");
  const prev = $("#murl-preview");
  const go = $("#murl-go");
  if (!event.target.value.trim()) { err.textContent = ""; prev.innerHTML = ""; go.disabled = true; return; }
  if (parsed) {
    err.textContent = "";
    prev.innerHTML = `<span class="pill pill-ip">${esc(parsed.platform)}</span>
      <span class="meta-item">agent will watch <b>${esc(parsed.name)}</b></span>`;
    go.disabled = false;
  } else {
    err.textContent = "Not a Twitch / Kick / YouTube channel link. For YouTube paste the @handle page.";
    prev.innerHTML = "";
    go.disabled = true;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePlayer();
});

document.addEventListener("click", async (event) => {
  if (event.target.closest("#murl-go")) {
    const parsed = parseStreamUrl($("#murl").value);
    if (!parsed) return;
    const go = $("#murl-go");
    go.disabled = true;
    go.textContent = "Dispatching…";
    try {
      await dispatchCapture(parsed.platform, "manual", parsed.name);
      toast(`Manual agent dispatched on ${parsed.platform}/${parsed.name}`, "ok");
      state.view = "agents";
      document.querySelectorAll(".tab").forEach((t) =>
        t.classList.toggle("active", t.dataset.view === "agents"));
      setTimeout(() => loadAgents().then(render).catch(() => {}), 3000);
    } catch (err) {
      toast(`Failed: ${err.message}`, "err");
      go.disabled = false;
      go.innerHTML = `${ICONS.power} Start agent on this link`;
    }
  }
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
    await Promise.all([loadClips(), loadSystem(), loadAgents()]);
    state.lastSync = new Date().toISOString();
  } catch (err) {
    state.error = `Could not load data: ${err.message}`;
  }
  render();

  state.timers.forEach(clearInterval);
  state.timers = [
    setInterval(async () => {
      if (document.hidden) return;
      try { await loadClips(); state.lastSync = new Date().toISOString(); render(); }
      catch (err) { state.error = err.message; }
    }, 10000),
    setInterval(async () => {
      if (document.hidden) return;
      await loadAgents();
      await loadSystem();
      render();
    }, 15000),
  ];
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    Promise.all([loadClips(), loadAgents(), loadSystem()])
      .then(() => { state.lastSync = new Date().toISOString(); render(); })
      .catch(() => {});
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
