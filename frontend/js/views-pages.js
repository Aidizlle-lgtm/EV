// All non-dashboard screens, in the same HUD style.
import { api, post, patch, put, del } from "./api.js";
import { h, icon, toast, modal, confirmDialog, fmtDate, relTime, toLocalInput, STATUS_LABEL } from "./ui.js";
import { panel, newTaskDialog } from "./views-dash.js";
import { openChat } from "./chat.js";
import { loadVoiceConfig, speak } from "./voice.js";

const changed = () => document.dispatchEvent(new CustomEvent("ev:changed"));
const head = (title, desc, ...actions) => h("div", { class: "view-head" }, h("h1", {}, title), h("span", { class: "spacer" }), ...actions, desc ? h("p", {}, desc) : null);
const statusChip = (st, detail) => h("span", { class: `chip st-${st}`, title: detail || "" }, STATUS_LABEL[st] || st);
const levelChip = (lv, label) => h("span", { class: `chip lv-${lv}` }, label || lv);
const sw = (checked, onchange, label) => h("label", { class: "switch", title: label }, h("input", { type: "checkbox", checked, "aria-label": label, onchange: (e) => onchange(e.target.checked) }), h("span"));
async function guard(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "err"); return undefined; }
}
function field(label, input) { return h("label", {}, label, input); }

// ------------------------------------------------------------------ AI Core
export function renderCore(view) {
  const wrap = h("div", { class: "stack" });
  view.replaceChildren(head("AI Core", "Choose which AI model E.V. thinks with, and tune its personality. Keys stay in your local .env file and are never shown again."), wrap);
  const load = async () => {
    const [provs, cfg, conns, agentList] = await Promise.all([api("/api/providers"), api("/api/settings"), api("/api/connections"), api("/api/agents")]);
    const s = cfg.settings;
    const cards = provs.map((p) => {
      const model = p.models.length
        ? h("select", { "aria-label": `${p.label} model` }, [h("option", { value: "" }, "— choose a model —"), ...p.models.map((m) => h("option", { value: m, selected: m === p.model }, m))])
        : h("input", { type: "text", value: p.model || "", placeholder: "model id (Test to load the list)", "aria-label": `${p.label} model` });
      const key = p.needs_key ? h("input", { type: "password", placeholder: p.key_hint ? `Saved ${p.key_hint} — paste to replace` : `Paste ${p.needs_key}`, autocomplete: "off" }) : null;
      return h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, p.label), statusChip(p.status, p.detail), p.active ? h("span", { class: "chip high" }, "ACTIVE") : null, p.fallback ? h("span", { class: "chip" }, "FALLBACK") : null),
        h("p", { class: "small muted", style: { margin: "0 0 8px" } }, p.detail),
        h("div", { class: "form-grid" },
          key ? field(p.needs_key, key) : field("Server URL", h("input", { type: "text", value: s["providers.ollama.base_url"], onchange: (e) => guard(() => patch("/api/settings", { "providers.ollama.base_url": e.target.value }), "Saved.") })),
          field("Model", model)),
        h("div", { class: "inline", style: { marginTop: "10px" } },
          key ? h("button", { class: "btn sm", onclick: async () => { if (!key.value.trim()) return toast("Paste a key first."); if (await guard(() => put(`/api/secrets/${p.needs_key}`, { value: key.value.trim() }), "Key saved locally.")) load(); } }, icon("key"), "Save key") : null,
          h("button", { class: "btn sm blue", onclick: async (e) => { e.target.disabled = true; const r = await guard(() => post(`/api/providers/${p.id}/test`)); if (r?.error) toast(r.error, "err"); else if (r) toast(`${p.label}: ${r.detail}`, "ok"); load(); } }, icon("refresh"), "Test & load models"),
          h("button", { class: "btn sm", onclick: async () => { if (await guard(() => post(`/api/providers/${p.id}`, { model: model.value }), "Model saved.")) load(); } }, icon("check"), "Save model"),
          !p.active ? h("button", { class: "btn sm primary", onclick: async () => { if (await guard(() => post(`/api/providers/${p.id}`, { model: model.value || undefined, active: true }), `${p.label} is now active.`)) { load(); changed(); } } }, icon("zap"), "Make active") : null,
          !p.active ? h("label", { class: "inline small" }, sw(p.fallback, async (v) => { await guard(() => post(`/api/providers/${p.id}`, { fallback: v })); load(); }, "Use as fallback"), "Fallback") : null,
          key && p.key_hint ? h("button", { class: "btn sm ghost", onclick: async () => { if (await confirmDialog("Remove key", `Remove ${p.needs_key} from this laptop?`, "Remove")) { await guard(() => del(`/api/secrets/${p.needs_key}`), "Key removed."); load(); } } }, icon("trash"), "Remove key") : null));
    });

    const name = h("input", { type: "text", value: s["profile.name"], maxlength: 60 });
    const title = h("input", { type: "text", value: s["profile.title"], maxlength: 40 });
    const wit = h("select", {}, ["off", "light", "playful"].map((v) => h("option", { value: v, selected: s["persona.wit"] === v }, { off: "Off — strictly professional", light: "Light — occasional dry wit", playful: "Playful" }[v])));
    const verb = h("select", {}, ["brief", "balanced", "detailed"].map((v) => h("option", { value: v, selected: s["persona.verbosity"] === v }, v[0].toUpperCase() + v.slice(1))));
    const custom = h("textarea", { rows: 4, maxlength: 3000, placeholder: "e.g. Use metric units. Challenge my assumptions. Keep finance advice conservative." }, s["persona.custom"]);
    const temp = h("input", { type: "number", min: 0, max: 1.5, step: 0.1, value: s["llm.temperature"] });
    const steps = h("input", { type: "number", min: 1, max: 20, step: 1, value: s["llm.max_steps"] });
    const effort = h("select", {}, ["low", "medium", "high", "xhigh", "max"].map((v) => h("option", { value: v, selected: s["anthropic.effort"] === v }, v)));
    wrap.replaceChildren(
      h("div", { class: "callout info" }, "Claude Code, Cursor, and Copilot are development tools — they don't give E.V. runtime API access. E.V. talks to models only through the providers below, using your own keys."),
      h("div", { class: "grid-2" }, ...cards),
      connectionsPanel(conns, load),
      routingPanel(agentList, conns.connections, load),
      panel("Personality", h("div", { class: "stack" },
        h("div", { class: "form-grid" }, field("Preferred name", name), field("Title shown under your name", title), field("Wit", wit), field("Verbosity", verb)),
        field("Your instructions for E.V.", custom),
        h("div", { class: "form-grid" }, field("Creativity (temperature, non-Claude models)", temp), field("Max tool steps per reply", steps), field("Claude effort", effort)),
        h("div", {}, h("button", { class: "btn primary", onclick: () => guard(() => patch("/api/settings", {
          "profile.name": name.value.trim(), "profile.title": title.value.trim() || "Operator", "persona.wit": wit.value, "persona.verbosity": verb.value,
          "persona.custom": custom.value, "llm.temperature": Number(temp.value), "llm.max_steps": Math.round(Number(steps.value)), "anthropic.effort": effort.value,
        }).then(changed), "Personality saved.") }, icon("check"), "Save personality")))));
  };
  load().catch((e) => wrap.replaceChildren(h("div", { class: "empty" }, e.message)));
}

// ------------------------------------------------------------------ AI connections (several keys per provider)
const VERIFY = { mock_tested: "adapter tested with simulated responses", docs_checked: "endpoint taken from provider docs — untested by E.V.", needs_adapter: "not supported yet" };

function connectionsPanel({ connections, presets }, reload) {
  const supported = presets.filter((p) => p.supported);
  const name = h("input", { type: "text", maxlength: 60, placeholder: "e.g. OpenAI — school key" });
  const preset = h("select", { "aria-label": "Provider" }, supported.map((p) => h("option", { value: p.id }, p.label)));
  const baseUrl = h("input", { type: "text", maxlength: 300, placeholder: "https://…/v1" });
  const key = h("input", { type: "password", autocomplete: "off", placeholder: "Paste the API key" });
  const model = h("input", { type: "text", maxlength: 200, placeholder: "model id (or Test later to pick)" });
  const note = h("p", { class: "small muted", style: { margin: "4px 0 0" } });
  const sync = () => {
    const p = supported.find((x) => x.id === preset.value);
    baseUrl.disabled = !(p.custom_base_url || p.adapter === "anthropic");
    baseUrl.placeholder = p.base_url || (p.custom_base_url ? "required — https://host/v1" : "fixed by the provider");
    key.placeholder = p.key_required ? "Paste the API key" : "API key (optional for this provider)";
    note.textContent = `${VERIFY[p.verification]}${p.notes ? ` · ${p.notes}` : ""}`;
  };
  preset.onchange = sync; sync();
  const add = async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim(), preset: preset.value, default_model: model.value.trim() };
    if (!baseUrl.disabled && baseUrl.value.trim()) body.base_url = baseUrl.value.trim();
    if (key.value.trim()) body.api_key = key.value.trim();
    if (await guard(() => post("/api/connections", body), "Connection saved. The key is stored only in your .env file.")) { key.value = ""; name.value = ""; reload(); }
  };
  const custom = connections.filter((c) => !c.builtin);
  const rows = custom.map((c) => {
    const m = c.models.length
      ? h("select", { "aria-label": `${c.name} model` }, [h("option", { value: "" }, "— choose —"), ...c.models.map((x) => h("option", { value: x, selected: x === c.default_model }, x))])
      : h("input", { type: "text", value: c.default_model || "", placeholder: "model id", "aria-label": `${c.name} model` });
    const newKey = h("input", { type: "password", autocomplete: "off", placeholder: c.key_hint ? `Saved ${c.key_hint} — paste to replace` : "Paste API key" });
    return h("div", { class: "panel", style: { margin: 0 } },
      h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, c.name), h("span", { class: "chip" }, c.preset_label),
        statusChip(c.configured ? "configured" : "setup_required", c.configured ? "Key saved" : "Needs a key")),
      h("p", { class: "small muted", style: { margin: "0 0 6px" } }, `id: ${c.id}${c.base_url ? ` · ${c.base_url}` : ""} · ${VERIFY[c.verification] || ""}`),
      h("div", { class: "form-grid" }, field("Default model", m), field("API key", newKey)),
      h("div", { class: "inline", style: { marginTop: "8px" } },
        h("button", { class: "btn sm blue", onclick: async (e) => { e.target.disabled = true; const r = await guard(() => post(`/api/connections/${c.id}/test`)); if (r?.error) toast(r.error, "err"); else if (r) toast(`${c.name}: ${r.detail}`, "ok"); reload(); } }, icon("refresh"), "Test & load models"),
        h("button", { class: "btn sm", onclick: async () => { const body = { default_model: m.value.trim() }; if (newKey.value.trim()) body.api_key = newKey.value.trim(); if (await guard(() => patch(`/api/connections/${c.id}`, body), "Saved.")) reload(); } }, icon("check"), "Save"),
        h("button", { class: "btn sm ghost", onclick: async () => { if (await confirmDialog("Delete connection", `Delete “${c.name}” and remove its key from this computer? Agents using it will go back to inheriting.`, "Delete")) { await guard(() => del(`/api/connections/${c.id}`), "Deleted."); reload(); } } }, icon("trash"), "Delete")));
  });
  const unsupported = presets.filter((p) => !p.supported);
  return panel("Connections — several providers, several keys", h("div", { class: "stack" },
    h("p", { class: "small muted", style: { margin: 0 } }, "A connection is a provider + your own key + model settings. Add as many as you like (even two keys for the same provider) and assign them to E.V. and each specialist below. The provider cards above are built-in connections and can be assigned too. Extra keys are for separating usage — after a rate limit, E.V. never switches to another key from the same provider."),
    h("form", { class: "stack", onsubmit: add },
      h("div", { class: "form-grid" }, field("Name", name), field("Provider", preset), field("Base URL", baseUrl), field("API key", key), field("Default model", model)),
      note, h("div", {}, h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add connection"))),
    rows.length ? h("div", { class: "grid-2" }, ...rows) : h("div", { class: "empty" }, "No extra connections yet."),
    unsupported.length ? h("p", { class: "small muted", style: { margin: 0 } }, "Not supported yet (need their own adapter): ", unsupported.map((p) => p.label).join(", "), ".") : null));
}

function routingPanel(agentList, connections, reload) {
  const routable = agentList.filter((a) => a.routing);
  const connOptions = (sel, inheritLabel) => [h("option", { value: "" }, inheritLabel), ...connections.map((c) => h("option", { value: c.id, selected: c.id === sel }, `${c.name}${c.builtin ? " (built-in)" : ""}`))];
  const modelsFor = (cid) => connections.find((c) => c.id === cid)?.models || [];
  const rows = routable.map((a) => {
    const r = a.routing;
    const listId = `models-${a.id}`;
    const dl = h("datalist", { id: listId });
    const conn = h("select", { "aria-label": `${a.name} connection` }, connOptions(r.connection, a.id === "ev" ? "Active provider in AI Core (current setup)" : "Same as E.V."));
    const model = h("input", { type: "text", value: r.model, list: listId, placeholder: "connection's default model", "aria-label": `${a.name} model` });
    const fbs = [0, 1].map((i) => {
      const f = r.fallbacks[i] || {};
      return { conn: h("select", { "aria-label": `${a.name} fallback ${i + 1}` }, connOptions(f.connection, "— none —")),
               model: h("input", { type: "text", value: f.model || "", placeholder: "default model", "aria-label": `${a.name} fallback ${i + 1} model` }) };
    });
    const fillModels = () => dl.replaceChildren(...modelsFor(conn.value).map((m) => h("option", { value: m })));
    conn.onchange = fillModels; fillModels();
    const save = async () => {
      const body = { connection: conn.value, model: model.value.trim(),
        fallbacks: fbs.filter((f) => f.conn.value).map((f) => ({ connection: f.conn.value, model: f.model.value.trim() })) };
      if (await guard(() => put(`/api/agents/${a.id}/routing`, body), `${a.name} model saved.`)) reload();
    };
    return h("div", { class: "stack", style: { borderTop: "1px solid var(--border)", paddingTop: "10px" } },
      h("div", { class: "inline" }, h("b", {}, a.name), h("span", { class: "small muted" }, "→ " + r.effective.map((e) => `${e.label} · ${e.model || "no model"}${e.cooling_down_s ? ` (cooling down ${e.cooling_down_s}s)` : ""}`).join("  ⟶  "))),
      h("div", { class: "route-row" }, field("Connection", conn), field("Model", model), h("span")), dl,
      ...fbs.map((f, i) => h("div", { class: "route-row" }, field(`Fallback ${i + 1}`, f.conn), field("Model", f.model), h("span"))),
      h("div", {}, h("button", { class: "btn sm primary", onclick: save }, icon("check"), `Save ${a.name}`)));
  });
  return panel("Agent models", h("div", { class: "stack" },
    h("p", { class: "small muted", style: { margin: 0 } }, "Choose the connection and model each agent thinks with. One connection can serve several agents. Fallbacks are tried in order when the main one is down or rate-limited (never another key from a rate-limited provider). E.V.'s modes (Coding, Research…) use E.V.'s setting."),
    ...rows));
}

// ------------------------------------------------------------------ Agents
export function renderAgents(view) {
  const grid = h("div", { class: "grid-3" });
  view.replaceChildren(head("Agents", "E.V. is the hub. Modes are focused versions of E.V.; specialists are separate agents she delegates to. Tool lists, permission levels, memory access, and limits are enforced by the backend — prompt files only set personality and behaviour."), grid);
  const load = () => api("/api/agents").then((agents) => grid.replaceChildren(...agents.map((a) => agentCard(a, load)))).catch((e) => toast(e.message, "err"));
  load();
}

function agentCard(a, reload) {
  const kindLabel = { orchestrator: "Central hub", mode: "Mode of E.V.", specialist: "Specialist" }[a.kind] || a.kind;
  const route = a.routing ? a.routing.effective.map((e) => `${e.label} · ${e.model || "no model"}`).join(" → ") : null;
  const md = a.market_data;
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { class: "panel-title" }, a.name), h("span", { class: "chip" }, kindLabel), statusChip(a.status, a.detail)),
    h("p", { class: "muted" }, a.policy?.domain || a.detail),
    a.prompt ? h("p", { class: "small" }, statusChip(a.prompt.status, a.prompt.detail), " ", h("code", {}, a.prompt.file), a.prompt.status === "temporary" ? " — replace with your own" : "") : null,
    route ? h("p", { class: "small" }, icon("cpu"), " ", route, a.routing.inherits && a.kind === "specialist" ? " (same as E.V.)" : "") : null,
    h("p", { class: "small" }, a.tools === null ? "All tools (except specialist-only)" : a.tools.length ? a.tools.join(", ") : "No tools"),
    a.policy ? h("p", { class: "small muted" }, `Max level: ${a.policy.max_level} · ${a.policy.max_steps} steps · ${Math.round(a.policy.timeout_s)}s limit · memory: ${a.policy.memory} · can't delegate`) : null,
    md ? h("div", { class: md.is_live ? "callout info" : "callout" },
      h("b", {}, "Market data: "), md.label,
      md.data_status === "TEST_DATA" ? h("div", { class: "small" }, "Every figure F.R.I.D.A.Y. sees is synthetic and labelled TEST DATA. No live source is connected yet — your AI keys don't include market data.") : null,
      h("div", { class: "inline", style: { marginTop: "6px" } }, h("select", { "aria-label": "Market data source", onchange: async (e) => { await guard(() => patch("/api/settings", { "market_data.source": e.target.value }), "Saved."); reload(); } },
        [...md.available.map((x) => h("option", { value: x.id, selected: md.source === x.id }, x.is_live ? x.label : "Test data (synthetic)")), h("option", { value: "none", selected: md.source === "none" }, "Off")])),
      h("div", { class: "small muted", style: { marginTop: "4px" } }, "Live sources that could be added: ", md.options.map((o) => `${o.name} (${o.credential})`).join(" · "))) : null,
    a.kind === "specialist" ? h("p", { class: "small muted" }, "Ask E.V. — she delegates to this specialist and explains the report.")
      : a.status !== "not_implemented" ? h("button", { class: "btn sm primary", onclick: () => openChat({ agent: a.id }) }, icon("chat"), "Open chat") : null);
}

// ------------------------------------------------------------------ Tasks
export function renderTasks(view) {
  let filter = "open";
  const list = h("div", { class: "rows" });
  const tabs = h("div", { class: "inline" });
  view.replaceChildren(head("Tasks", "Your task board. E.V. can read it, and changes it only with your approval (unless you allow reversible writes in Tools & Skills).",
    h("button", { class: "btn primary", onclick: newTaskDialog }, icon("plus"), "New task")), panel("Board", h("div", { class: "stack" }, tabs, list)));
  const load = async () => {
    tabs.replaceChildren(...["open", "done", "all"].map((f) => h("button", { class: `btn sm ${f === filter ? "blue" : "ghost"}`, onclick: () => { filter = f; load(); } }, f[0].toUpperCase() + f.slice(1))));
    const tasks = await api(`/api/tasks?status=${filter}`);
    const now = Date.now();
    list.replaceChildren(...(tasks.length ? tasks.map((t) => h("div", { class: "row" },
      h("button", { class: `check ${t.status === "done" ? "on" : ""}`, "aria-label": t.status === "done" ? "Mark open" : "Mark done",
        onclick: async () => { await guard(() => patch(`/api/tasks/${t.id}`, { status: t.status === "done" ? "open" : "done" })); load(); changed(); } }, t.status === "done" ? icon("check") : null),
      h("div", { class: "grow" }, h("div", { class: `t ${t.status === "done" ? "done-title" : ""}` }, t.title),
        h("div", { class: `s ${t.due_at && t.status === "open" && new Date(t.due_at) < now ? "st-error" : ""}` }, [t.due_at ? `Due ${fmtDate(t.due_at)} (${relTime(t.due_at)})` : "No due date", t.notes].filter(Boolean).join(" · "))),
      t.priority !== "normal" ? h("span", { class: `chip ${t.priority}` }, t.priority) : null,
      h("button", { class: "icon-btn", "aria-label": "Edit", onclick: () => editTask(t, load) }, icon("edit")),
      h("button", { class: "icon-btn", "aria-label": "Delete", onclick: async () => { if (await confirmDialog("Delete task", `Delete “${t.title}”?`, "Delete")) { await guard(() => del(`/api/tasks/${t.id}`)); load(); changed(); } } }, icon("trash"))))
      : [h("div", { class: "empty" }, filter === "open" ? "No open tasks. Nice." : "Nothing here.")]));
  };
  const onChange = () => load().catch(() => {});
  document.addEventListener("ev:changed", onChange);
  load().catch((e) => toast(e.message, "err"));
  return () => document.removeEventListener("ev:changed", onChange);
}

async function editTask(t, reload) {
  const title = h("input", { type: "text", value: t.title, maxlength: 200 });
  const notes = h("textarea", { rows: 3 }, t.notes);
  const due = h("input", { type: "datetime-local", value: t.due_at ? toLocalInput(t.due_at) : "" });
  const pr = h("select", {}, ["low", "normal", "high"].map((p) => h("option", { value: p, selected: p === t.priority }, p)));
  if (!(await modal("Edit task", h("div", { class: "stack" }, field("Title", title), field("Notes", notes), h("div", { class: "form-grid" }, field("Due", due), field("Priority", pr))),
    [{ label: "Cancel", value: false, cls: "ghost" }, { label: "Save", value: true, cls: "primary" }]))) return;
  await guard(() => patch(`/api/tasks/${t.id}`, { title: title.value, notes: notes.value, priority: pr.value, due_at: due.value || "" }), "Saved.");
  reload(); changed();
}

// ------------------------------------------------------------------ Calendar
export function renderCalendar(view) {
  const agenda = h("div", { class: "stack" });
  const title = h("input", { type: "text", maxlength: 200, placeholder: "Event title" });
  const start = h("input", { type: "datetime-local", value: toLocalInput() });
  const end = h("input", { type: "datetime-local" });
  const loc = h("input", { type: "text", maxlength: 200, placeholder: "Optional" });
  view.replaceChildren(head("Calendar", "E.V.'s local calendar on this laptop. (Google Calendar sync is planned, not built yet.)"),
    h("div", { class: "stack" },
      panel("Add event", h("form", { class: "form-grid", onsubmit: async (e) => {
        e.preventDefault();
        if (!title.value.trim()) return toast("Give the event a title.");
        if (await guard(() => post("/api/events", { title: title.value.trim(), starts_at: start.value, ends_at: end.value || null, location: loc.value }), "Event added.")) { title.value = ""; load(); changed(); }
      } }, field("Title", title), field("Starts", start), field("Ends", end), field("Location", loc), h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add"))),
      panel("Next 14 days", agenda)));
  const load = async () => {
    const from = new Date(); from.setHours(0, 0, 0, 0);
    const to = new Date(from.getTime() + 14 * 86400000);
    const events = await api(`/api/events?start=${encodeURIComponent(toLocalInput(from.toISOString()))}&end=${encodeURIComponent(toLocalInput(to.toISOString()))}`);
    const byDay = {};
    for (const e of events) (byDay[new Date(e.starts_at).toDateString()] ||= []).push(e);
    agenda.replaceChildren(...(events.length ? Object.entries(byDay).map(([day, evs]) => h("div", {},
      h("div", { class: "panel-title", style: { fontSize: "13px", margin: "4px 0 6px" } }, fmtDate(evs[0].starts_at, false)),
      h("div", { class: "rows" }, evs.map((e) => h("div", { class: "row" },
        h("span", { class: "ico" }, icon("calendar")),
        h("div", { class: "grow" }, h("div", { class: "t" }, e.title), h("div", { class: "s" }, `${fmtDate(e.starts_at)}${e.ends_at ? ` – ${new Date(e.ends_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}${e.location ? ` · ${e.location}` : ""}`)),
        h("button", { class: "icon-btn", "aria-label": "Delete event", onclick: async () => { if (await confirmDialog("Delete event", `Delete “${e.title}”?`, "Delete")) { await guard(() => del(`/api/events/${e.id}`)); load(); changed(); } } }, icon("trash")))))))
      : [h("div", { class: "empty" }, "No events in the next two weeks.")]));
  };
  load().catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ Memory
export function renderMemory(view) {
  const list = h("div", { class: "stack" });
  const content = h("input", { type: "text", maxlength: 1000, placeholder: "e.g. Prefers metric units" });
  const cat = h("input", { type: "text", maxlength: 40, value: "preferences", list: "mem-cats" });
  const scope = h("select", { "aria-label": "Who can read it" }, h("option", { value: "shared" }, "Shared (E.V.; specialists by category)"));
  api("/api/agents").then((ags) => ags.filter((a) => a.kind === "specialist").forEach((a) => scope.append(h("option", { value: a.id }, `${a.name} only (and E.V.)`)))).catch(() => {});
  const q = h("input", { type: "search", placeholder: "Search memories" });
  const file = h("input", { type: "file", accept: "application/json", class: "hidden", onchange: () => importFile(file) });
  view.replaceChildren(
    head("Memory", "E.V. only knows what's saved here. E.V. can propose memories, but you approve each one. You can correct, export, or delete anything.",
      h("a", { class: "btn", href: "/api/memories/export", download: "ev-memories.json" }, icon("download"), "Export"),
      h("button", { class: "btn", onclick: () => file.click() }, icon("upload"), "Import"), file),
    h("div", { class: "stack" },
      panel("Add a memory", h("form", { class: "form-grid", onsubmit: async (e) => {
        e.preventDefault();
        if (!content.value.trim()) return;
        if (await guard(() => post("/api/memories", { content: content.value.trim(), category: cat.value.trim() || "general", scope: scope.value }), "Saved.")) { content.value = ""; load(); changed(); }
      } }, field("Fact or preference", content), field("Category", cat), field("Who can read it", scope), h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Save"))),
      panel("Saved memories", h("div", { class: "stack" }, q, list))));
  q.addEventListener("input", () => load());
  const load = async () => {
    const mems = await api(`/api/memories${q.value.trim() ? `?q=${encodeURIComponent(q.value.trim())}` : ""}`);
    const groups = {};
    for (const m of mems) (groups[m.category] ||= []).push(m);
    view.querySelector("#mem-cats")?.remove();
    view.append(h("datalist", { id: "mem-cats" }, Object.keys(groups).map((c) => h("option", { value: c }))));
    list.replaceChildren(...(mems.length ? Object.entries(groups).map(([c, ms]) => h("div", {},
      h("div", { class: "panel-title", style: { fontSize: "13px", margin: "2px 0 6px" } }, `${c} (${ms.length})`),
      h("div", { class: "rows" }, ms.map((m) => h("div", { class: "row" },
        h("div", { class: "grow" }, h("div", { style: { whiteSpace: "normal" } }, m.content), h("div", { class: "s" }, `M${m.id} · ${m.scope && m.scope !== "shared" ? `only ${m.scope} + E.V. · ` : ""}${m.source === "assistant" ? "proposed by E.V., approved by you" : "added by you"} · ${relTime(m.updated_at)}`)),
        h("button", { class: "icon-btn", "aria-label": "Edit memory", onclick: () => editMemory(m, load) }, icon("edit")),
        h("button", { class: "icon-btn", "aria-label": "Delete memory", onclick: async () => { if (await confirmDialog("Forget this?", m.content, "Delete")) { await guard(() => del(`/api/memories/${m.id}`), "Forgotten."); load(); changed(); } } }, icon("trash")))))))
      : [h("div", { class: "empty" }, q.value ? "No matches." : "No memories yet. Add one above, or tell E.V. “remember that…”.")]));
  };
  async function importFile(input) {
    const f = input.files[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      const memories = (data.memories || data).map((m) => ({ content: m.content, category: m.category || "general", scope: m.scope || "shared" })).filter((m) => m.content);
      if (!(await confirmDialog("Import memories", `Add ${memories.length} memories from ${f.name}?`, "Import", false))) return;
      const r = await post("/api/memories/import", { memories });
      toast(`Imported ${r.imported}.`, "ok"); load(); changed();
    } catch (e) { toast(`Import failed: ${e.message}`, "err"); } finally { input.value = ""; }
  }
  load().catch((e) => toast(e.message, "err"));
}

async function editMemory(m, reload) {
  const text = h("textarea", { rows: 3, maxlength: 1000 }, m.content);
  const cat = h("input", { type: "text", value: m.category, maxlength: 40 });
  if (!(await modal("Correct memory", h("div", { class: "stack" }, field("Memory", text), field("Category", cat)),
    [{ label: "Cancel", value: false, cls: "ghost" }, { label: "Save", value: true, cls: "primary" }]))) return;
  await guard(() => patch(`/api/memories/${m.id}`, { content: text.value.trim(), category: cat.value.trim() }), "Updated.");
  reload(); changed();
}

// ------------------------------------------------------------------ Conversations
export function renderConversations(view) {
  const list = h("div", { class: "rows" });
  view.replaceChildren(head("Conversations", null, h("button", { class: "btn primary", onclick: () => openChat() }, icon("plus"), "New chat")), panel("History", list));
  const load = async () => {
    const convs = await api("/api/conversations?limit=200");
    list.replaceChildren(...(convs.length ? convs.map((c) => h("div", { class: "row clickable", onclick: () => openChat({ conversationId: c.id }) },
      h("span", { class: "ico blue" }, icon("chat")),
      h("div", { class: "grow" }, h("div", { class: "t" }, c.title), h("div", { class: "s" }, `${c.turns} turn(s) · ${c.agent} · ${relTime(c.updated_at)}`)),
      h("button", { class: "icon-btn", "aria-label": "Delete", onclick: async (e) => { e.stopPropagation(); if (await confirmDialog("Delete conversation", `Delete “${c.title}”?`, "Delete")) { await guard(() => del(`/api/conversations/${c.id}`)); load(); changed(); } } }, icon("trash"))))
      : [h("div", { class: "empty" }, "No conversations yet.")]));
  };
  load().catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ Knowledge Base
export function renderKnowledge(view) {
  const docs = h("div", { class: "table-wrap" });
  const results = h("div", { class: "stack" });
  const input = h("input", { type: "file", multiple: true, class: "hidden", accept: ".pdf,.docx,.txt,.md,.csv,.json,.html,.htm,.py,.js,.ts,.yaml,.yml,.log,.xml,.ini,.toml,.rst,.tex,.sql" });
  const drop = h("div", { class: "drop", tabindex: 0, role: "button", onclick: () => input.click(), onkeydown: (e) => { if (e.key === "Enter") input.click(); } },
    icon("upload"), h("div", {}, "Drop files here or click to upload"), h("div", { class: "small" }, "PDF, Word (.docx), and text/code files · up to 25 MB"));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); });
  input.onchange = () => { upload(input.files); input.value = ""; };
  const pathIn = h("input", { type: "text", placeholder: "C:\\Users\\you\\Documents\\notes.pdf (must be in an authorized folder)" });
  const q = h("input", { type: "search", placeholder: "Search your documents…" });
  view.replaceChildren(head("Knowledge Base", "Documents E.V. can search and cite. Answers include references like [file p.3 · D2] so you can check the source."),
    h("div", { class: "grid-2" },
      panel("Add documents", h("div", { class: "stack" }, drop, input,
        h("form", { class: "inline", onsubmit: async (e) => { e.preventDefault(); if (pathIn.value.trim() && await guard(() => post("/api/documents/ingest-path", { path: pathIn.value.trim() }), "Added.")) { pathIn.value = ""; load(); } } },
          h("div", { style: { flex: 1 } }, pathIn), h("button", { class: "btn", type: "submit" }, icon("folder"), "Add from laptop")))),
      panel("Search", h("form", { class: "stack", onsubmit: async (e) => { e.preventDefault(); search(); } }, h("div", { class: "inline" }, h("div", { style: { flex: 1 } }, q), h("button", { class: "btn blue", type: "submit" }, icon("search"), "Search")), results))),
    h("div", { style: { height: "12px" } }), panel("Documents", docs));
  async function upload(fileList) {
    for (const f of fileList) {
      const form = new FormData();
      form.append("file", f);
      toast(`Reading ${f.name}…`);
      const doc = await guard(() => api("/api/documents", { method: "POST", form }));
      if (doc) toast(`${doc.name}: ${doc.chunk_count} passages indexed.`, "ok");
    }
    load(); changed();
  }
  async function search() {
    if (q.value.trim().length < 2) return;
    const hits = await guard(() => api(`/api/documents/search?q=${encodeURIComponent(q.value.trim())}&k=8`));
    if (!hits) return;
    results.replaceChildren(...(hits.length ? hits.map((r) => h("div", { class: "row", style: { display: "block" } }, h("div", { class: "ref" }, r.ref), h("div", { class: "passage" }, r.text)))
      : [h("div", { class: "empty" }, "No matching passages.")]));
  }
  const load = async () => {
    const list = await api("/api/documents");
    docs.replaceChildren(list.length ? h("table", { class: "table" }, h("tr", {}, ["Name", "Passages", "Size", "Added", ""].map((c) => h("th", {}, c))),
      list.map((d) => h("tr", {}, h("td", {}, d.name, d.source_path ? h("div", { class: "small muted" }, d.source_path) : null), h("td", {}, d.chunk_count),
        h("td", {}, `${(d.size / 1024).toFixed(0)} KB`), h("td", {}, relTime(d.created_at)),
        h("td", {}, h("button", { class: "icon-btn", "aria-label": "Remove document", onclick: async () => { if (await confirmDialog("Remove document", `Remove “${d.name}” from the Knowledge Base?`, "Remove")) { await guard(() => del(`/api/documents/${d.id}`)); load(); changed(); } } }, icon("trash"))))))
      : h("div", { class: "empty" }, "No documents yet."));
  };
  load().catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ Tools & Skills
export function renderTools(view) {
  const levels = h("div", { class: "grid-2" });
  const pending = h("div", { class: "rows" });
  const tools = h("div", { class: "table-wrap" });
  const audit = h("div", { class: "table-wrap" });
  view.replaceChildren(head("Tools & Skills", "Every action E.V. can take, its permission level, and a full audit trail. These rules are enforced by the backend — the AI can't override them."),
    h("div", { class: "stack" }, panel("Permission levels", levels), panel("Waiting for your approval", pending), panel("Tools", tools),
      panel("Audit log", audit, { link: { label: "Refresh", onclick: () => loadAudit() } })));
  const load = async () => {
    const [t, s, p] = await Promise.all([api("/api/tools"), api("/api/settings"), api("/api/approvals/pending")]);
    levels.replaceChildren(...Object.entries(t.levels).map(([lv, info]) => h("div", { class: "row", style: { alignItems: "flex-start" } },
      levelChip(lv, info.label),
      h("div", { class: "grow" }, h("div", { style: { whiteSpace: "normal" } }, info.description), h("div", { class: "s" }, `Policy: ${lv === "reversible_write" ? (t.reversible_write_policy === "auto" ? "Automatic (you chose this)" : "Ask first") : info.policy}`)),
      lv === "reversible_write" ? h("select", { "aria-label": "Reversible write policy", style: { width: "auto" }, onchange: async (e) => { await guard(() => patch("/api/settings", { "permissions.reversible_write": e.target.value }), "Policy saved."); load(); } },
        h("option", { value: "ask", selected: t.reversible_write_policy === "ask" }, "Ask first"), h("option", { value: "auto", selected: t.reversible_write_policy === "auto" }, "Automatic")) : null)),
      h("div", { class: "row" }, h("div", { class: "grow" }, h("div", {}, "Always confirm new memories"), h("div", { class: "s" }, "Even when reversible writes are automatic.")),
        sw(s.settings["memory.require_approval"], (v) => guard(() => patch("/api/settings", { "memory.require_approval": v }), "Saved."), "Always confirm memories")));
    pending.replaceChildren(...(p.length ? p.map((a) => h("div", { class: "row" }, levelChip(a.level, a.level_label),
      h("div", { class: "grow" }, h("div", { class: "t" }, a.tool), h("div", { class: "s" }, a.summary)),
      h("button", { class: "btn sm primary", onclick: async () => { await guard(() => post(`/api/approvals/${a.id}`, { decision: "approve" }), "Approved."); load(); } }, "Approve"),
      h("button", { class: "btn sm", onclick: async () => { await guard(() => post(`/api/approvals/${a.id}`, { decision: "deny" }), "Denied."); load(); } }, "Deny")))
      : [h("div", { class: "empty" }, "Nothing waiting.")]));
    tools.replaceChildren(h("table", { class: "table" }, h("tr", {}, ["Tool", "Level", "Approval", "Status", "On"].map((c) => h("th", {}, c))),
      t.tools.map((x) => h("tr", {}, h("td", {}, h("b", {}, x.name), h("div", { class: "small muted" }, x.description)), h("td", {}, levelChip(x.level, x.level_label)),
        h("td", {}, x.can_auto_approve
          ? h("label", { class: "inline small", title: "Run this tool without asking each time" },
              sw(x.auto_approve, async (v) => { await guard(() => patch(`/api/tools/${x.name}`, { auto_approve: v }), v ? `${x.name} will run without asking.` : `${x.name} will ask first.`); load(); }, `Run ${x.name} without asking`),
              x.requires_approval ? "Asks you" : "Automatic")
          : (x.requires_approval ? (x.level === "external" ? "Always asks" : "Asks you") : "Automatic")), h("td", {}, x.setup_issue ? h("span", { class: "st-setup_required small" }, "Setup required") : h("span", { class: "st-ok small" }, "Ready")),
        h("td", {}, sw(x.enabled, (v) => guard(() => patch(`/api/tools/${x.name}`, { enabled: v }), v ? "Enabled." : "Disabled."), `Enable ${x.name}`))))));
  };
  const loadAudit = async () => {
    const rows = await api("/api/audit?limit=150");
    audit.replaceChildren(rows.length ? h("table", { class: "table" }, h("tr", {}, ["When", "Who", "Tool", "Level", "Result", "Detail"].map((c) => h("th", {}, c))),
      rows.map((r) => h("tr", {}, h("td", { class: "small" }, relTime(r.ts)), h("td", {}, r.actor), h("td", {}, r.tool), h("td", {}, levelChip(r.level)),
        h("td", { class: `st-${r.status.replace("user_", "")}` }, r.status), h("td", { class: "small muted" }, (r.detail || "").slice(0, 160)))))
      : h("div", { class: "empty" }, "No activity yet."));
  };
  load().catch((e) => toast(e.message, "err"));
  loadAudit().catch(() => {});
}

// ------------------------------------------------------------------ Workflows
export function renderWorkflows(view) {
  const list = h("div", { class: "rows" });
  const name = h("input", { type: "text", maxlength: 120, placeholder: "e.g. Morning briefing" });
  const kind = h("select", {}, h("option", { value: "reminder" }, "Reminder"), h("option", { value: "briefing" }, "Executive briefing"), h("option", { value: "prompt" }, "Ask E.V. (read-only)"));
  const stype = h("select", {}, h("option", { value: "daily" }, "Daily"), h("option", { value: "once" }, "Once"), h("option", { value: "interval" }, "Every N minutes"));
  const at = h("input", { type: "datetime-local", value: toLocalInput() });
  const time = h("input", { type: "time", value: "08:00" });
  const minutes = h("input", { type: "number", min: 5, value: 60 });
  const days = h("div", { class: "inline" }, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => h("label", { class: "inline small" }, h("input", { type: "checkbox", value: i, checked: true }), d)));
  const text = h("textarea", { rows: 2, placeholder: "Reminder text, or what E.V. should check" });
  const sync = () => {
    at.parentElement.classList.toggle("hidden", stype.value !== "once");
    time.parentElement.classList.toggle("hidden", stype.value !== "daily");
    days.parentElement.classList.toggle("hidden", stype.value !== "daily");
    minutes.parentElement.classList.toggle("hidden", stype.value !== "interval");
    text.parentElement.classList.toggle("hidden", kind.value === "briefing");
  };
  stype.onchange = sync; kind.onchange = sync;
  view.replaceChildren(head("Workflows", "Scheduled jobs that run in the background while E.V. is open. They can read and prepare, but never send, delete, or buy — anything needing approval is refused."),
    h("div", { class: "stack" },
      panel("New workflow", h("form", { class: "stack", onsubmit: async (e) => {
        e.preventDefault();
        const schedule = stype.value === "once" ? { type: "once", at: at.value } : stype.value === "daily"
          ? { type: "daily", time: time.value, weekdays: [...days.querySelectorAll("input:checked")].map((c) => +c.value) }
          : { type: "interval", minutes: +minutes.value };
        if (schedule.weekdays?.length === 7) schedule.weekdays = null;
        const payload = kind.value === "reminder" ? { message: text.value } : kind.value === "prompt" ? { prompt: text.value } : {};
        if (await guard(() => post("/api/jobs", { name: name.value.trim() || kind.selectedOptions[0].textContent, kind: kind.value, schedule, payload }), "Workflow scheduled.")) { name.value = ""; text.value = ""; load(); }
      } }, h("div", { class: "form-grid" }, field("Name", name), field("Type", kind), field("When", stype), field("At", at), field("Time", time), field("Minutes", minutes)),
        field("Days", days), field("Details", text), h("div", {}, h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Schedule")))),
      panel("Scheduled", list)));
  sync();
  const load = async () => {
    const jobs = await api("/api/jobs");
    list.replaceChildren(...(jobs.length ? jobs.map((j) => h("div", { class: "row", style: { flexWrap: "wrap" } },
      h("span", { class: "ico" }, icon(j.kind === "reminder" ? "bell" : j.kind === "briefing" ? "bulb" : "flow")),
      h("div", { class: "grow" }, h("div", { class: "t" }, j.name), h("div", { class: "s" }, `${j.kind} · ${j.schedule_text}${j.next_run_at && j.enabled ? ` · next ${relTime(j.next_run_at)}` : ""}${j.last_status ? ` · last: ${j.last_status}` : ""} · by ${j.created_by}`)),
      j.last_result ? h("button", { class: "btn sm ghost", onclick: () => modal(`Last result — ${j.name}`, h("pre", {}, j.last_result)) }, "Result") : null,
      h("button", { class: "btn sm", onclick: async (e) => { e.target.disabled = true; toast("Running…"); await guard(() => post(`/api/jobs/${j.id}/run`), "Done — see the feed."); load(); changed(); } }, icon("play"), "Run now"),
      sw(!!j.enabled, async (v) => { await guard(() => patch(`/api/jobs/${j.id}`, { enabled: v })); load(); }, "Enabled"),
      h("button", { class: "icon-btn", "aria-label": "Delete workflow", onclick: async () => { if (await confirmDialog("Delete workflow", `Delete “${j.name}”?`, "Delete")) { await guard(() => del(`/api/jobs/${j.id}`)); load(); } } }, icon("trash"))))
      : [h("div", { class: "empty" }, "No workflows yet.")]));
  };
  load().catch((e) => toast(e.message, "err"));
}

// ------------------------------------------------------------------ Settings
export function renderSettings(view, { logout }) {
  const ints = h("div", { class: "rows" });
  const secrets = h("div", { class: "rows" });
  const voiceBox = h("div", { class: "stack" });
  const wxBox = h("div", { class: "stack" });
  const smtpBox = h("div", { class: "stack" });
  const folderBox = h("div", { class: "stack" });
  const phoneBox = h("div", { class: "stack" });
  const secBox = h("div", { class: "stack" });
  const tvBox = h("div", { class: "stack" });
  view.replaceChildren(head("Settings", "Integrations, keys, voice, folders, and security. Anything not connected shows “Setup required”."),
    h("div", { class: "stack" }, panel("Integrations", ints),
      h("div", { class: "grid-2" }, panel("API keys (stored only in your local .env)", secrets), panel("Voice", voiceBox)),
      h("div", { class: "grid-2" }, panel("Laptop folders", folderBox), panel("Weather & location", wxBox)),
      h("div", { class: "grid-2" }, panel("Email sending (SMTP)", smtpBox), panel("Phone access", phoneBox)),
      h("div", { class: "grid-2" }, panel("Fire TV", tvBox), panel("Security", secBox))));

  const load = async () => {
    const [list, cfg, folders, sys] = await Promise.all([api("/api/integrations"), api("/api/settings"), api("/api/folders"), api("/api/system")]);
    const s = cfg.settings;
    const testable = new Set(["weather", "github", "elevenlabs", "email", "firetv"]);
    ints.replaceChildren(...list.map((i) => h("div", { class: "row" },
      h("div", { class: "grow" }, h("div", { class: "t" }, i.name, h("span", { class: "muted small" }, ` · ${i.category}`)), h("div", { class: "s" }, i.detail)),
      statusChip(i.status, i.detail),
      i.id.startsWith("llm:") ? h("button", { class: "btn sm ghost", onclick: () => (location.hash = "#/core") }, "Configure") : null,
      testable.has(i.id) && i.status !== "setup_required" ? h("button", { class: "btn sm", onclick: async (e) => { e.target.disabled = true; const r = await guard(() => post(`/api/integrations/${i.id}/test`)); if (r) toast(r.ok ? `OK: ${typeof r.result === "string" ? r.result : "working"}` : r.error, r.ok ? "ok" : "err"); load(); } }, "Test") : null)));

    secrets.replaceChildren(...cfg.secrets.map((sec) => {
      const inp = h("input", { type: "password", autocomplete: "off", placeholder: sec.configured ? `Saved ${sec.hint}` : "Not set" });
      return h("div", { class: "row", style: { flexWrap: "wrap" } },
        h("div", { style: { minWidth: "170px", flex: "1 1 170px" } }, h("div", { class: "t" }, sec.label), h("div", { class: `s st-${sec.configured ? "ok" : "setup_required"}` }, sec.configured ? `Configured ${sec.hint}` : "Setup required")),
        h("div", { style: { flex: "2 1 200px" } }, inp),
        h("button", { class: "btn sm", onclick: async () => { if (!inp.value.trim()) return toast("Paste a value first."); if (await guard(() => put(`/api/secrets/${sec.name}`, { value: inp.value.trim() }), "Saved to .env.")) { load(); loadVoiceConfig(); changed(); } } }, "Save"),
        sec.configured ? h("button", { class: "icon-btn", "aria-label": `Remove ${sec.name}`, onclick: async () => { if (await confirmDialog("Remove key", `Remove ${sec.name} from this laptop?`, "Remove")) { await guard(() => del(`/api/secrets/${sec.name}`), "Removed."); load(); loadVoiceConfig(); changed(); } } }, icon("trash")) : null);
    }));

    const tts = h("select", {}, [["auto", "Auto (ElevenLabs if set up)"], ["elevenlabs", "ElevenLabs"], ["browser", "Browser voice"], ["off", "Off"]].map(([v, l]) => h("option", { value: v, selected: s["voice.tts"] === v }, l)));
    const stt = h("select", {}, [["auto", "Auto (Groq Whisper if set up)"], ["groq", "Groq Whisper"], ["browser", "Browser recognizer"]].map(([v, l]) => h("option", { value: v, selected: s["voice.stt"] === v }, l)));
    const vid = h("input", { type: "text", value: s["voice.elevenlabs_voice_id"] });
    const vmodel = h("input", { type: "text", value: s["voice.elevenlabs_model"], list: "eleven-models" });
    const maxc = h("input", { type: "number", min: 100, max: 5000, value: s["voice.max_tts_chars"] });
    let speakReplies = !!s["voice.speak_replies"];
    voiceBox.replaceChildren(h("div", { class: "form-grid" }, field("Speech output", tts), field("Speech input", stt), field("ElevenLabs voice ID", vid), field("ElevenLabs model", vmodel), field("Max characters spoken", maxc)),
      h("datalist", { id: "eleven-models" }, ["eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5"].map((m) => h("option", { value: m }))),
      h("label", { class: "inline" }, sw(speakReplies, (v) => (speakReplies = v), "Speak typed replies too"), "Also speak replies to typed messages"),
      h("p", { class: "small muted", style: { margin: 0 } }, "eleven_multilingual_v2 sounds most natural; eleven_flash_v2_5 starts speaking faster. Character usage counts against your ElevenLabs plan."),
      h("div", { class: "callout info small" }, "Hear E.V. through your Echo: say “Alexa, pair Bluetooth”, then on this laptop open Settings → Bluetooth & devices → Add device and pick the Echo. E.V.'s voice will play from the Echo. Turn on “Also speak replies to typed messages” below to hear every answer."),
      h("div", { class: "inline" }, h("button", { class: "btn primary", onclick: async () => { if (await guard(() => patch("/api/settings", { "voice.tts": tts.value, "voice.stt": stt.value, "voice.elevenlabs_voice_id": vid.value.trim(), "voice.elevenlabs_model": vmodel.value.trim(), "voice.max_tts_chars": +maxc.value, "voice.speak_replies": speakReplies }), "Voice saved.")) { await loadVoiceConfig(); changed(); } } }, "Save voice"),
        h("button", { class: "btn", onclick: async () => { await loadVoiceConfig(); speak("Hello. E.V. voice systems are online."); } }, icon("speaker"), "Test voice")));

    const city = h("input", { type: "text", value: s["weather.location"] || "", placeholder: "e.g. Bhimber, Pakistan" });
    const units = h("select", {}, [["celsius", "°C"], ["fahrenheit", "°F"]].map(([v, l]) => h("option", { value: v, selected: s["weather.units"] === v }, l)));
    wxBox.replaceChildren(h("p", { class: "small muted", style: { margin: 0 } }, s["weather.label"] ? `Current: ${s["weather.label"]}` : "Not set — weather shows “Setup required”. Uses Open-Meteo (free, no key)."),
      h("div", { class: "form-grid" }, field("City", city), field("Units", units)),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => {
        await guard(() => patch("/api/settings", { "weather.units": units.value }));
        if (city.value.trim()) { const r = await guard(() => post("/api/weather/location", { name: city.value.trim() })); if (r) toast(`Location set: ${r.label}`, "ok"); }
        load(); changed();
      } }, "Save location")));

    const host = h("input", { type: "text", value: s["smtp.host"], placeholder: "smtp.gmail.com" });
    const port = h("input", { type: "number", value: s["smtp.port"] });
    const user = h("input", { type: "text", value: s["smtp.user"], placeholder: "you@gmail.com" });
    const from = h("input", { type: "text", value: s["smtp.from"], placeholder: "Defaults to the username" });
    smtpBox.replaceChildren(h("p", { class: "small muted", style: { margin: 0 } }, "Lets E.V. send emails you approve, one by one. For Gmail, create an App Password (Google Account → Security) and save it as SMTP_PASSWORD under API keys."),
      h("div", { class: "form-grid" }, field("SMTP host", host), field("Port (587 or 465)", port), field("Username", user), field("From address", from)),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { await guard(() => patch("/api/settings", { "smtp.host": host.value.trim(), "smtp.port": +port.value || 587, "smtp.user": user.value.trim(), "smtp.from": from.value.trim() }), "SMTP saved."); load(); } }, "Save SMTP")));

    const fpath = h("input", { type: "text", placeholder: "C:\\Users\\you\\Projects" });
    const flabel = h("input", { type: "text", placeholder: "Label (optional)" });
    let fw = false;
    folderBox.replaceChildren(h("p", { class: "small muted", style: { margin: 0 } }, "E.V. can only read folders you add here, and only write in folders marked writable (overwrites keep a backup). Keys, .env files, and E.V.'s own data are always off-limits."),
      h("div", { class: "rows" }, folders.length ? folders.map((f) => h("div", { class: "row" }, h("span", { class: "ico sq" }, icon("folder")),
        h("div", { class: "grow" }, h("div", { class: "t" }, f.label), h("div", { class: `s ${f.exists ? "" : "st-error"}` }, f.exists ? f.path : `${f.path} (missing)`)),
        h("span", { class: "chip" }, f.writable ? "read + write" : "read only"),
        h("button", { class: "icon-btn", "aria-label": "Remove folder", onclick: async () => { await guard(() => del(`/api/folders/${f.id}`), "Folder removed."); load(); changed(); } }, icon("trash"))))
        : h("div", { class: "empty" }, "No folders authorized.")),
      h("div", { class: "form-grid" }, field("Folder path", fpath), field("Label", flabel)),
      h("label", { class: "inline" }, sw(false, (v) => (fw = v), "Writable"), "Allow E.V. to create files here (each write still asks you, unless you change the policy)"),
      h("div", {}, h("button", { class: "btn primary", onclick: async () => { if (!fpath.value.trim()) return; if (await guard(() => post("/api/folders", { path: fpath.value.trim(), label: flabel.value.trim(), writable: fw }), "Folder authorized.")) { load(); changed(); } } }, icon("plus"), "Authorize folder")));

    const lan = sys.lan || [];
    phoneBox.replaceChildren(h("p", { style: { margin: 0 } }, location.hostname === "localhost" || location.hostname === "127.0.0.1" ? "You're on the laptop." : `You're connected from ${location.hostname}.`),
      h("p", { class: "small muted", style: { margin: 0 } }, "To use E.V. from your phone: set EV_HOST=0.0.0.0 in .env, restart, allow it through Windows Firewall (private networks only), and open one of these on the same Wi-Fi:"),
      h("div", { class: "rows" }, lan.length ? lan.map((ip) => h("div", { class: "row" }, icon("wifi"), h("code", {}, `${location.protocol}//${ip}:${location.port || (location.protocol === "https:" ? 443 : 80)}`))) : h("div", { class: "empty" }, "No network address found.")),
      h("p", { class: "small muted", style: { margin: 0 } }, "Phones only allow the microphone over HTTPS. Typing works over plain Wi-Fi; for voice, use Tailscale HTTPS (see README)."));

    const tvHost = h("input", { type: "text", value: s["firetv.host"], placeholder: "e.g. 192.168.1.40" });
    const tvAdb = h("input", { type: "text", value: s["firetv.adb_path"], placeholder: "Leave blank to auto-detect" });
    const tvInt = list.find((i) => i.id === "firetv");
    tvBox.replaceChildren(
      h("p", { class: "small muted", style: { margin: 0 } }, "E.V. controls your Fire TV over your home Wi-Fi using ADB — nothing goes through the internet. On the TV: Settings → My Fire TV → Developer options → ADB debugging ON. Find its IP under My Fire TV → About → Network."),
      h("div", { class: "form-grid" }, field("Fire TV IP address", tvHost), field("ADB path (optional)", tvAdb)),
      tvInt ? h("div", { class: "inline small" }, statusChip(tvInt.status, tvInt.detail), h("span", { class: "muted" }, tvInt.detail)) : null,
      h("div", { class: "inline" },
        h("button", { class: "btn primary", onclick: async () => { if (await guard(() => patch("/api/settings", { "firetv.host": tvHost.value.trim(), "firetv.adb_path": tvAdb.value.trim() }), "Fire TV saved.")) load(); } }, "Save"),
        h("button", { class: "btn", onclick: async (e) => {
          e.target.disabled = true;
          toast("Connecting… if your TV shows “Allow USB debugging?”, choose Always allow.");
          const r = await guard(() => post("/api/integrations/firetv/test"));
          if (r) toast(r.ok ? `Connected: ${r.result.model}${r.result.foreground_app ? ` · showing ${r.result.foreground_app}` : ""}` : r.error, r.ok ? "ok" : "err");
          load();
        } }, icon("refresh"), "Test connection")),
      h("p", { class: "small muted", style: { margin: 0 } }, "Each TV command asks you first. To make it instant, turn on “run without asking” for tv_remote (and tv_open_app) in Tools & Skills."));

    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const nw = h("input", { type: "password", minlength: 6, autocomplete: "new-password" });
    secBox.replaceChildren(h("div", { class: "form-grid" }, field("Current passcode", cur), field("New passcode (6+ characters)", nw),
      h("button", { class: "btn", onclick: async () => { if (await guard(() => post("/api/auth/change", { current: cur.value, new: nw.value }), "Passcode changed; other devices signed out.")) { cur.value = ""; nw.value = ""; } } }, icon("lock"), "Change passcode"),
      h("button", { class: "btn danger", onclick: logout }, icon("logout"), "Sign out")));
  };
  load().catch((e) => toast(e.message, "err"));
}
