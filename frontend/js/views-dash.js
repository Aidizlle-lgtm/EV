// Command Center dashboard. Every number shown comes from /api/dashboard.
import { api, post } from "./api.js";
import { h, icon, toast, modal, fmtTime, relTime, STATUS_LABEL } from "./ui.js";
import { startOrb, drawConstellation, orbMode } from "./orb.js";
import { openChat, voiceTurn } from "./chat.js";

export function panel(title, body, { cls = "", link = null, live = false } = {}) {
  return h("section", { class: `panel ${cls}` },
    h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, title),
      live ? h("span", { class: "live-tag" }, "LIVE") : null,
      link ? h("button", { class: "link", onclick: link.onclick }, link.label, " ›") : null),
    body);
}

const go = (route) => { location.hash = `#/${route}`; };
const AGENT_ICON = { code: "code", search: "search", memory: "memory", tasks: "tasks", system: "system", browser: "browser", core: "core" };
const FEED_TAG = { info: "INFO", warn: "WARN", tip: "TIP", live: "LIVE", setup: "SETUP" };
const FEED_ICON = { calendar: "calendar", tasks: "warn", github: "github", system: "cpu", workflow: "flow", briefing: "bulb" };

export async function newTaskDialog() {
  const title = h("input", { type: "text", maxlength: 200, placeholder: "What needs doing?" });
  const due = h("input", { type: "datetime-local" });
  const pr = h("select", {}, ["normal", "high", "low"].map((p) => h("option", { value: p }, p)));
  const ok = await modal("New task", h("div", { class: "stack" }, h("label", {}, "Title", title), h("div", { class: "form-grid" }, h("label", {}, "Due (optional)", due), h("label", {}, "Priority", pr))),
    [{ label: "Cancel", value: false, cls: "ghost" }, { label: "Add task", value: true, cls: "primary" }]);
  if (!ok || !title.value.trim()) return;
  try {
    await post("/api/tasks", { title: title.value.trim(), priority: pr.value, due_at: due.value || null });
    toast("Task added.", "ok");
    document.dispatchEvent(new CustomEvent("ev:changed"));
  } catch (e) { toast(e.message, "err"); }
}

function gauge(label, value, color) {
  const r = 40, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, value ?? 0));
  const wrap = h("div", { class: "gauge", role: "img", "aria-label": `${label} ${v}%` });
  wrap.innerHTML = `<svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="${r}" fill="none" stroke="#14233d" stroke-width="7"/>
    <circle cx="50" cy="50" r="${r}" fill="none" stroke="${color}" stroke-width="7" stroke-linecap="round" stroke-dasharray="${(v / 100) * c} ${c}" style="filter:drop-shadow(0 0 4px ${color})"/></svg>`;
  wrap.append(h("div", { class: "lbl" }, h("span", {}, label), h("b", {}, `${v}%`)));
  return wrap;
}

export function renderDashboard(view) {
  const els = {
    core: h("div", { class: "rows" }), feed: h("div", { class: "rows" }), agents: h("div", { class: "agents-grid" }),
    timeline: h("div", { class: "timeline" }), monitor: h("div"), memory: h("div", { class: "mem-wrap" }), llm: h("div", { class: "llm-grid" }),
    orbState: h("div", { class: "orb-state" }),
  };
  const canvas = h("canvas", { "aria-hidden": "true" });
  const orb = h("section", { class: "panel orb-wrap c-orb", "aria-label": "AI core" }, canvas,
    h("div", { class: "orb-label" }, h("div", { class: "n" }, "E.V"), h("div", { class: "k" }, "AI CORE"), h("div", { class: "v", id: "orb-version" }, "")), els.orbState);
  const memCanvas = h("canvas");
  const quick = h("div", { class: "quick" },
    [["plus", "Start New Task", newTaskDialog], ["calendar", "Open Calendar", () => go("calendar")],
      ["mic", "Start Voice Chat", () => voiceTurn(true)], ["play", "Run Workflow", () => go("workflows")]]
      .map(([ic, label, fn]) => h("button", { onclick: fn }, h("span", { class: "ico sq" }, icon(ic)), label)));

  view.replaceChildren(h("div", { class: "dash", id: "dash" },
    panel("AI Core Overview", els.core, { cls: "c-core" }),
    orb,
    panel("Live Intelligence Feed", h("div", {}, els.feed), { cls: "c-feed", live: true, link: { label: "View all", onclick: showAllNotifications } }),
    panel("Active Agents", els.agents, { cls: "c-agents", link: { label: "View all", onclick: () => go("agents") } }),
    panel("Mission Timeline", els.timeline, { cls: "c-timeline", link: { label: "Full schedule", onclick: () => go("calendar") } }),
    panel("Quick Commands", quick, { cls: "c-quick" }),
    panel("System Monitor", els.monitor, { cls: "c-monitor" }),
    panel("Memory Insights", els.memory, { cls: "c-memory", link: { label: "View memory", onclick: () => go("memory") } }),
    panel("LLM Status", els.llm, { cls: "c-llm", link: { label: "Manage providers", onclick: () => go("core") } }),
  ));
  if (document.body.classList.contains("focus")) view.querySelector("#dash").classList.add("focus-mode");
  startOrb(canvas);

  let data = null;
  const fill = (d) => {
    data = d;
    const p = d.core.provider;
    const core = [
      ["core", "AI Core", p && p.status !== "setup_required" ? `${p.label} · ${p.model || "no model"}` : "Setup required", p?.status === "setup_required" ? "setup_required" : p?.status === "error" ? "error" : "active", "core"],
      ["memory", "Memory", `${d.core.memory_count} saved`, "info", "memory"],
      ["mic", "Voice", d.core.voice.tts === "elevenlabs" ? "ElevenLabs online" : "Browser voice (fallback)", d.core.voice.tts === "elevenlabs" ? "active" : "setup_required", "settings"],
      ["bot", "Agents", d.core.agents_running ? `${d.core.agents_running} running` : "Standing by", "info", "agents"],
      ["zap", "LLMs", `${d.core.llms_connected} configured`, d.core.llms_connected ? "active" : "setup_required", "core"],
      ["shield", "System", d.status === "OPTIMAL" ? "Optimal" : d.status.toLowerCase().replace(/^./, (c) => c.toUpperCase()), d.status === "OPTIMAL" ? "active" : "setup_required", "tools"],
    ];
    els.core.replaceChildren(...core.map(([ic, t, s, st, route], i) => h("div", { class: "row clickable", onclick: () => go(route) },
      h("span", { class: `ico ${i % 2 ? "blue" : ""}` }, icon(ic)), h("div", { class: "grow" }, h("div", { class: "t" }, t), h("div", { class: `s st-${st}` }, s)))));
    if (d.core.pending_approvals) {
      els.core.append(h("div", { class: "row clickable", onclick: () => go("tools") }, h("span", { class: "ico" }, icon("shield")),
        h("div", { class: "grow" }, h("div", { class: "t" }, "Approvals"), h("div", { class: "s st-setup_required" }, `${d.core.pending_approvals} waiting for you`))));
    }

    const feed = d.feed.map((n) => ({ ...n, tag: n.kind }));
    const setup = d.setup.slice(0, Math.max(0, 6 - feed.length)).map((s) => ({ title: `${s.name}: setup required`, body: s.detail, tag: "setup", source: "settings", link: "#/settings", read: 1 }));
    const items = [...feed, ...setup];
    els.feed.replaceChildren(...(items.length ? items : [{ title: "All quiet. Nothing needs your attention.", tag: "info", source: "system", read: 1 }]).map((n) =>
      h("div", { class: `row clickable feed-item ${n.read ? "" : "unread"}`, onclick: () => openNotification(n) },
        h("span", { class: "ico sq" }, icon(n.tag === "setup" ? "gear" : FEED_ICON[n.source] || "info")),
        h("div", { class: "grow" }, h("div", { class: "t" }, n.title), h("div", { class: "s" }, n.created_at ? `${n.source} · ${relTime(n.created_at)}` : n.body || n.source)),
        h("span", { class: `tag tag-${n.tag === "setup" ? "warn" : n.tag}` }, FEED_TAG[n.tag] || "INFO"))));

    els.agents.replaceChildren(...d.agents.filter((a) => a.id !== "ev").map((a) => h("div", {
      class: `agent-card ${a.status}`, title: a.detail, onclick: () => a.status === "not_implemented" ? toast(a.detail) : a.kind === "specialist" ? go(a.id) : openChat({ agent: a.id }),
    }, h("span", { class: `ico ${a.status === "running" ? "" : "blue"}` }, icon(AGENT_ICON[a.icon] || "bot")),
      h("div", { class: "grow" }, h("div", { class: "t" }, a.name), h("div", { class: `s st-${a.status}` }, STATUS_LABEL[a.status] || a.status)),
      h("div", { class: `mini-wave st-${a.status}`, "aria-hidden": "true" }, Array.from({ length: 10 }, (_, i) => h("i", { style: { animationDelay: `${i * 0.08}s` } }))))));

    els.timeline.replaceChildren(...(d.timeline.length ? d.timeline.slice(0, 6).map((it) => h("div", { class: "tl-item" },
      h("div", { class: "tl-time" }, fmtTime(it.at)), h("span", { class: `tl-dot ${it.kind}` }),
      h("div", { class: "tl-title", title: it.title }, it.title), h("div", { class: "tl-when" }, relTime(it.at))))
      : [h("div", { class: "empty" }, "Nothing scheduled today.", h("br"), h("button", { class: "btn sm", style: { marginTop: "8px" }, onclick: () => go("calendar") }, icon("plus"), "Add event"))]));

    const m = d.monitor;
    els.monitor.replaceChildren(h("div", { class: "gauges" }, gauge("CPU", m.cpu_percent, "#ff2d4a"), gauge("RAM", m.ram_percent, "#38c6ff"), gauge("Disk", m.disk_percent, "#ff2d4a")),
      h("div", { class: "monitor-meta" }, h("span", {}, `RAM ${m.ram_used_gb}/${m.ram_total_gb} GB`), h("span", {}, m.battery ? `Battery ${m.battery.percent}%${m.battery.plugged ? " ⚡" : ""}` : `Up ${m.uptime_hours}h`)));

    const mi = d.memory_insights;
    els.memory.replaceChildren(memCanvas, h("div", { class: "mem-stats" },
      h("span", {}, "Memories"), h("b", {}, mi.memories), h("span", {}, "Turns today"), h("b", {}, mi.turns_today), h("span", {}, "Tool calls today"), h("b", {}, mi.tool_calls_today)));
    requestAnimationFrame(() => drawConstellation(memCanvas, mi.categories));

    els.llm.replaceChildren(...d.providers.map((p) => h("div", { class: "row clickable", onclick: () => go("core"), title: p.detail },
      h("span", { class: "ico sq" }, icon(p.id === "ollama" ? "cpu" : p.id === "anthropic" ? "spark" : "zap")),
      h("div", { class: "grow" }, h("div", { class: "t" }, p.label, p.active ? " ★" : ""), h("div", { class: `s st-${p.status}` }, STATUS_LABEL[p.status] || p.status)))));
  };

  const refresh = async () => {
    try { fill(await api("/api/dashboard")); } catch (e) { if (e.status !== 401) toast(e.message, "err"); }
  };
  const stateTimer = setInterval(() => { els.orbState.textContent = { idle: "Standing by", thinking: "Processing", listening: "Listening", speaking: "Speaking" }[orbMode()] || ""; }, 300);
  refresh();
  const timer = setInterval(refresh, 10000);
  const onChange = () => refresh();
  document.addEventListener("ev:changed", onChange);
  return () => { clearInterval(timer); clearInterval(stateTimer); document.removeEventListener("ev:changed", onChange); };
}

async function openNotification(n) {
  if (n.link && n.link.startsWith("#")) { location.hash = n.link; }
  if (n.id && !n.read) { post(`/api/notifications/${n.id}/read`).then(() => document.dispatchEvent(new CustomEvent("ev:changed"))).catch(() => {}); }
  if (n.link?.startsWith("http")) {
    modal(n.title, h("div", {}, h("pre", {}, n.body || ""), h("a", { href: n.link, target: "_blank", rel: "noopener noreferrer" }, "Open link ↗")));
  } else if (n.body && !n.link) {
    modal(n.title, h("pre", {}, n.body));
  }
}

export async function showAllNotifications() {
  try {
    const list = await api("/api/notifications?limit=100");
    await post("/api/notifications/read-all");
    document.dispatchEvent(new CustomEvent("ev:changed"));
    modal("Live Intelligence Feed", list.length ? h("div", { class: "rows" }, list.map((n) => h("div", { class: "row clickable", onclick: () => modal(n.title, h("pre", {}, n.body || "(no details)")) },
      h("span", { class: `tag tag-${n.kind}` }, FEED_TAG[n.kind] || "INFO"),
      h("div", { class: "grow" }, h("div", { class: "t" }, n.title), h("div", { class: "s" }, `${n.source} · ${relTime(n.created_at)}`)))))
      : h("p", { class: "muted" }, "No notifications yet."));
  } catch (e) { toast(e.message, "err"); }
}

