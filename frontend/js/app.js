// E.V. front end entry point: boot, sign-in, navigation, top bar, dock.
import { api, post, setUnauthorizedHandler } from "./api.js";
import { h, icon, setIcon, toast, modal, relTime } from "./ui.js";
import { renderDashboard, showAllNotifications } from "./views-dash.js";
import { renderCore, renderAgents, renderTasks, renderCalendar, renderMemory, renderConversations, renderKnowledge, renderTools, renderWorkflows, renderSettings } from "./views-pages.js";
import { initChat, openChat, loadAgents, voiceTurn } from "./chat.js";
import { renderFriday } from "./friday.js";
import { loadVoiceConfig, attachWave, onVoiceState, micSupportIssue } from "./voice.js";

const $ = (id) => document.getElementById(id);

const ROUTES = [
  { id: "dashboard", label: "Command Center", icon: "grid", render: renderDashboard },
  { id: "core", label: "AI Core", icon: "core", render: renderCore },
  { id: "agents", label: "Agents", icon: "bot", render: renderAgents },
  { id: "tasks", label: "Tasks", icon: "tasks", render: renderTasks, badge: "tasks_overdue" },
  { id: "calendar", label: "Calendar", icon: "calendar", render: renderCalendar },
  { id: "memory", label: "Memory", icon: "memory", render: renderMemory },
  { id: "conversations", label: "Conversations", icon: "chat", render: renderConversations, badge: "conversations" },
  { id: "knowledge", label: "Knowledge Base", icon: "book", render: renderKnowledge },
  { id: "tools", label: "Tools & Skills", icon: "tools", render: renderTools, badge: "tools" },
  { id: "workflows", label: "Workflows", icon: "flow", render: renderWorkflows },
  { id: "settings", label: "Settings", icon: "gear", render: (v) => renderSettings(v, { logout }) },
];

let cleanup = null;
let started = false;

// ---------------------------------------------------------------- boot & auth
async function boot() {
  setUnauthorizedHandler(() => showLogin(false));
  const minSplash = new Promise((r) => setTimeout(r, 900));
  let status;
  try { status = await api("/api/auth/status"); } catch (e) { status = null; }
  await minSplash;
  $("boot").classList.add("done");
  setTimeout(() => $("boot").remove(), 700);
  if (!status) { document.body.append(h("div", { class: "login" }, h("div", { class: "login-card panel" }, h("h1", {}, "E.V. OFFLINE"), h("p", {}, "Can't reach the E.V. server. Start it with start_ev.bat (or python run.py) and reload.")))); return; }
  if (status.authenticated) return startApp();
  showLogin(status.setup_required, status.local_request);
}

function showLogin(setup, local = true) {
  started = false;
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  document.querySelectorAll(".setup-only").forEach((el) => el.classList.toggle("hidden", !setup));
  $("login-msg").textContent = setup
    ? (local ? "First run: create a passcode. You'll use it on this laptop and your phone." : "E.V. hasn't been set up yet. Open it on the laptop (http://localhost) to create a passcode first.")
    : "Enter your passcode.";
  $("login-btn").textContent = setup ? "Create passcode & enter" : "Unlock";
  $("login-pass").autocomplete = setup ? "new-password" : "current-password";
  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    $("login-err").textContent = "";
    const pass = $("login-pass").value;
    try {
      if (setup) {
        if (pass !== $("login-pass2").value) { $("login-err").textContent = "Passcodes don't match."; return; }
        await post("/api/auth/setup", { passcode: pass, name: $("login-name").value.trim() });
      } else {
        await post("/api/auth/login", { passcode: pass });
      }
      $("login-pass").value = ""; $("login-pass2").value = "";
      $("login").classList.add("hidden");
      startApp();
    } catch (err) { $("login-err").textContent = err.message; }
  };
  setTimeout(() => (setup ? $("login-name") : $("login-pass")).focus(), 50);
}

async function logout() {
  try { await post("/api/auth/logout"); } catch { /* ignore */ }
  location.hash = "";
  location.reload();
}

// ---------------------------------------------------------------- app shell
function buildShell() {
  $("nav").replaceChildren(...ROUTES.map((r) => h("a", { href: `#/${r.id}`, "data-route": r.id }, icon(r.icon), h("span", {}, r.label), r.badge ? h("span", { class: "badge", id: `badge-${r.id}`, "data-n": "0" }) : null)));
  $("nav").addEventListener("click", () => $("sidebar").classList.remove("open"));
  setIcon("menu-btn", "menu");
  setIcon("fullscreen-btn", "expand");
  setIcon("gear-btn", "gear");
  setIcon("search-ico", "search");
  setIcon("op-avatar", "user");
  setIcon("mic-main", "mic");
  setIcon("d-loc-ico", "pin");
  setIcon("d-wx-ico", "sun");
  setIcon("d-net-ico", "wifi");
  setIcon("brief-ico", "play");
  $("bell-btn").prepend(icon("bell"));
  $("focus-btn").replaceChildren(icon("zap"), "Focus Mode");
  document.querySelectorAll(".talk .bars").forEach((b) => b.replaceChildren(...Array.from({ length: 14 }, (_, i) => h("i", { style: { height: `${20 + Math.abs(Math.sin(i * 1.7)) * 75}%` } }))));

  $("menu-btn").onclick = () => $("sidebar").classList.toggle("open");
  $("gear-btn").onclick = () => (location.hash = "#/settings");
  $("bell-btn").onclick = showAllNotifications;
  $("operator").onclick = () => (location.hash = "#/core");
  $("fullscreen-btn").onclick = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.());
  $("talk-btn").onclick = () => openChat();
  $("mic-main").onclick = () => voiceTurn(true);
  $("brief-btn").onclick = briefing;
  $("focus-btn").onclick = () => {
    document.body.classList.toggle("focus");
    $("dash")?.classList.toggle("focus-mode", document.body.classList.contains("focus"));
    toast(document.body.classList.contains("focus") ? "Focus mode: non-essential panels hidden." : "Focus mode off.");
  };
  document.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => (location.hash = `#/${b.dataset.go}`)));
  initSearch();
  initChat();
  attachWave($("voice-wave"));
  onVoiceState((state, detail) => {
    const label = { listening: "Listening…", transcribing: "Transcribing…", speaking: "Speaking…", idle: "Ready", error: "Voice error" }[state] || state;
    $("voice-state").textContent = detail && state !== "idle" ? `${label} (${detail})` : detail || label;
    $("mic-main").classList.toggle("busy", state === "transcribing");
    $("talk-sub").textContent = state === "listening" ? "I am listening…" : state === "speaking" ? "Speaking…" : "Open chat";
  });
}

function route() {
  const m = location.hash.match(/^#\/(\w+)(?:\?(.*))?/) || [];
  const id = m[1] || "dashboard";
  // F.R.I.D.A.Y.'s workspace takes the whole screen, with her own look; E.V.'s shell is hidden, not destroyed.
  if (id === "friday") {
    if (typeof cleanup === "function") cleanup();
    $("app").classList.add("hidden");
    cleanup = renderFriday($("friday-root"), { query: Object.fromEntries(new URLSearchParams(m[2] || "")) });
    document.title = "F.R.I.D.A.Y. · E.V.";
    return;
  }
  $("app").classList.remove("hidden");
  const r = ROUTES.find((x) => x.id === id) || ROUTES[0];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === r.id));
  if (typeof cleanup === "function") cleanup();
  const view = $("view");
  view.scrollTop = 0;
  cleanup = r.render(view) || null;
  document.title = `${r.label} · E.V.`;
}

async function startApp() {
  $("app").classList.remove("hidden");
  if (!started) {
    started = true;
    buildShell();
    window.addEventListener("hashchange", route);
    tickClock();
    setInterval(tickClock, 1000);
    setInterval(refreshChrome, 15000);
    document.addEventListener("ev:changed", refreshChrome);
  }
  await loadVoiceConfig();
  loadAgents();
  route();
  refreshChrome();
  const issue = micSupportIssue();
  if (issue) $("voice-state").textContent = "Mic needs HTTPS here";
}

function tickClock() {
  const d = new Date();
  $("clock-date").textContent = d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  $("clock-time").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function refreshChrome() {
  try {
    const d = await api("/api/dashboard");
    $("status-val").textContent = d.status;
    $("status-pill").classList.toggle("warn", d.status !== "OPTIMAL");
    $("op-name").textContent = d.operator.name || "Operator";
    $("op-title").textContent = d.operator.title || "Commander";
    $("bell-badge").textContent = d.counts.unread || "";
    $("bell-badge").dataset.n = d.counts.unread;
    for (const r of ROUTES.filter((x) => x.badge)) {
      const el = $(`badge-${r.id}`);
      const n = d.counts[r.badge] || 0;
      el.textContent = n || ""; el.dataset.n = n;
    }
    $("d-net").textContent = d.network.online ? "Online" : "Offline";
    if (!d.weather_configured) { $("d-loc").textContent = "Setup required"; $("d-wx").textContent = "Setup required"; }
    else {
      const w = await api("/api/weather");
      if (w.temperature !== undefined) {
        $("d-loc").textContent = w.location;
        $("d-wx").textContent = `${Math.round(w.temperature)}${w.unit} ${w.condition}`;
      } else { $("d-wx").textContent = w.message ? "Unavailable" : "—"; }
    }
  } catch { /* the view shows its own errors */ }
}

async function briefing() {
  const btn = $("brief-btn");
  btn.disabled = true;
  toast("Preparing your briefing…");
  try {
    const { text } = await post("/api/briefing");
    const body = h("div", { class: "msg assistant", style: { maxWidth: "100%" } });
    const { markdown } = await import("./ui.js");
    body.innerHTML = markdown(text);
    const { speak } = await import("./voice.js");
    const choice = await modal("Executive Briefing", body, [{ label: "Close", value: false, cls: "ghost" }, { label: "Read it to me", value: true, cls: "primary" }]);
    if (choice) speak(text);
    document.dispatchEvent(new CustomEvent("ev:changed"));
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; }
}

// ---------------------------------------------------------------- search
function initSearch() {
  const input = $("search"), box = $("search-results");
  let timer = 0;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    if (input.value.trim().length < 2) { box.classList.add("hidden"); return; }
    timer = setTimeout(async () => {
      try {
        const r = await api(`/api/search?q=${encodeURIComponent(input.value.trim())}`);
        const sec = (title, items, fn) => items.length ? [h("div", { class: "panel-title", style: { fontSize: "12px", margin: "6px 4px" } }, title), ...items.map(fn)] : [];
        const row = (t, s, onclick) => h("div", { class: "row clickable", onclick: () => { box.classList.add("hidden"); input.value = ""; onclick(); } }, h("div", { class: "grow" }, h("div", { class: "t" }, t), s ? h("div", { class: "s" }, s) : null));
        const parts = [
          ...sec("Tasks", r.tasks, (t) => row(t.title, `${t.status}${t.due_at ? ` · due ${relTime(t.due_at)}` : ""}`, () => (location.hash = "#/tasks"))),
          ...sec("Memories", r.memories, (m) => row(m.content, m.category, () => (location.hash = "#/memory"))),
          ...sec("Documents", r.documents, (d) => row(d.ref, d.text.slice(0, 120), () => (location.hash = "#/knowledge"))),
          ...sec("Conversations", r.conversations, (c) => row(c.title, c.agent === "friday" ? "F.R.I.D.A.Y." : null,
            () => (c.agent === "friday" ? (location.hash = `#/friday?c=${c.id}`) : openChat({ conversationId: c.id })))),
        ];
        box.replaceChildren(...(parts.length ? parts : [h("div", { class: "empty" }, "No results.")]));
        box.classList.remove("hidden");
      } catch (e) { toast(e.message, "err"); }
    }, 250);
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { box.classList.add("hidden"); input.blur(); } });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) box.classList.add("hidden"); });
}

boot();
