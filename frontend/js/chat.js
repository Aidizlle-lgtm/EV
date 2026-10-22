// Chat drawer: streaming replies, tool activity, inline approvals, cancel, voice.
import { api, post, del, streamPost } from "./api.js";
import { h, icon, setIcon, toast, markdown, relTime, confirmDialog } from "./ui.js";
import { setOrbState } from "./orb.js";
import { listen, speak, stopSpeaking, isRecording, voiceConfig } from "./voice.js";

const $ = (id) => document.getElementById(id);
const S = { conv: null, agent: "ev", runId: null, ctrl: null, busy: false, bubble: null, bubbleText: "", lastText: "", voiceReply: false, chips: {} };

export function initChat() {
  setIcon("chat-history-btn", "history");
  setIcon("chat-new", "plus");
  setIcon("chat-close", "x");
  setIcon("chat-mic", "mic");
  setIcon("chat-send", "send");
  $("chat-close").onclick = closeChat;
  $("chat-scrim").onclick = closeChat;
  $("chat-new").onclick = () => newConversation();
  $("chat-history-btn").onclick = toggleHistory;
  $("chat-agent").onchange = (e) => { S.agent = e.target.value; updateSub(); };
  $("composer").onsubmit = (e) => {
    e.preventDefault();
    if (!S.busy) return send($("chat-input").value);
    // Only an explicit click on the Stop button interrupts a reply — never the Enter key.
    if (e.submitter === $("chat-send")) stopRun();
    else toast("E.V. is still replying. Press Stop to interrupt, or wait a moment.");
  };
  const input = $("chat-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(160, input.scrollHeight)}px`; });
  $("chat-mic").onclick = () => voiceTurn(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("chat").classList.contains("open") && !document.querySelector(".modal-bg")) closeChat(); });
}

export async function loadAgents() {
  try {
    const agents = await api("/api/agents");
    const sel = $("chat-agent");
    // Specialists (F.R.I.D.A.Y. …) aren't chatted with directly: E.V. delegates to them.
    sel.replaceChildren(...agents.filter((a) => a.kind !== "specialist").map((a) => h("option", { value: a.id, disabled: a.status === "not_implemented" },
      a.status === "not_implemented" ? `${a.name} (not built yet)` : a.name)));
    sel.value = S.agent;
  } catch { /* shown elsewhere */ }
}

export function openChat({ conversationId = null, agent = null, focus = true } = {}) {
  $("chat").classList.add("open");
  $("chat-scrim").classList.remove("hidden");
  if (agent) { S.agent = agent; $("chat-agent").value = agent; }
  if (conversationId && conversationId !== S.conv?.id) loadConversation(conversationId);
  else if (!S.conv) renderEmpty();
  updateSub();
  if (focus && matchMedia("(min-width: 900px)").matches) setTimeout(() => $("chat-input").focus(), 50);
}

export function closeChat() {
  $("chat").classList.remove("open");
  $("chat-scrim").classList.add("hidden");
  $("chat-history").classList.add("hidden");
}

function updateSub() {
  const opt = $("chat-agent").selectedOptions[0];
  $("chat-sub").textContent = S.busy ? "Replying…" : `${opt ? opt.textContent : "E.V."} mode`;
}

function renderEmpty() {
  $("chat-title").textContent = "New conversation";
  $("chat-log").replaceChildren(h("div", { class: "empty" },
    h("p", {}, "Ask me anything, or try:"),
    ...["What's on my plate today?", "Add a task to renew my passport by Friday", "Summarise the documents in my Knowledge Base", "How is my laptop doing?"]
      .map((q) => h("button", { class: "btn sm ghost", style: { margin: "3px" }, onclick: () => send(q) }, q))));
}

export async function newConversation(agent) {
  if (S.busy) return toast("Stop the current reply first.", "err");
  S.conv = null;
  if (agent) { S.agent = agent; $("chat-agent").value = agent; }
  renderEmpty();
  $("chat-history").classList.add("hidden");
  $("chat-input").focus();
}

async function loadConversation(id) {
  if (S.busy) return toast("Stop the current reply first.", "err");
  try {
    const conv = await api(`/api/conversations/${id}`);
    S.conv = conv;
    S.agent = conv.agent || "ev";
    $("chat-agent").value = S.agent;
    $("chat-title").textContent = conv.title;
    const log = $("chat-log");
    log.replaceChildren();
    for (const m of conv.messages) {
      if (m.role === "user") log.append(h("div", { class: "msg user" }, m.content));
      else if (m.role === "assistant" && (m.content || m.status === "error")) {
        log.append(assistantBubble(m.content, m.status));
      } else if (m.role === "tool") {
        log.append(toolChip(m.name, m.tool_status, m.summary));
      }
    }
    log.scrollTop = log.scrollHeight;
    updateSub();
  } catch (e) { toast(e.message, "err"); }
}

function assistantBubble(text, status = "complete") {
  const b = h("div", { class: `msg assistant ${status}` });
  b.innerHTML = markdown(text);
  return b;
}

const CHIP_ICON = { ok: "check", running: "refresh", awaiting: "shield", denied: "x", expired: "x", cancelled: "x", error: "warn", invalid: "warn", timeout: "warn", unavailable: "lock" };
// ---------- delegation (E.V. → specialist) ----------
function delegationBox(agentName, task) {
  return h("div", { class: "deleg" },
    h("div", { class: "deleg-head" }, icon("bot"), h("b", {}, `E.V. → ${agentName}`), h("span", { class: "deleg-state" }, "working…")),
    task ? h("div", { class: "deleg-task" }, task) : null,
    h("ul", { class: "deleg-steps" }));
}

function updateDelegation(box, ev) {
  const state = box.querySelector(".deleg-state");
  const steps = box.querySelector(".deleg-steps");
  const argText = (a) => Object.values(a || {}).filter((v) => typeof v === "string" || typeof v === "number").join(" ");
  if (ev.event === "tool") steps.append(h("li", { "data-tool": ev.name }, h("code", {}, ev.name), " ", argText(ev.args), h("span", { class: "s" }, " …")));
  else if (ev.event === "tool_done") {
    const li = [...steps.querySelectorAll(`li[data-tool="${ev.name}"]`)].find((x) => !x.dataset.done);
    if (li) { li.dataset.done = "1"; li.querySelector(".s").textContent = ev.status === "ok" ? " ✓" : ` — ${ev.status}`; }
  } else if (ev.event === "fallback") steps.append(h("li", { class: "s" }, `${ev.from} unavailable — using its fallback`));
  else if (ev.stage === "finished") state.textContent = ev.status === "completed" ? "report ready" : "stopped at step limit";
  else if (ev.stage === "timeout") state.textContent = "timed out";
  else if (ev.stage === "cancelled") state.textContent = "stopped";
  else if (ev.stage === "error") state.textContent = "couldn't reach its AI model";
}

const DATA_BADGE = { TEST_DATA: ["TEST DATA — not real", "warn"], NONE: ["No market data", "warn"], LIVE: ["Live data", "ok"], MIXED: ["Delayed / mixed data", "warn"] };

function reportCard(r) {
  const [label, tone] = DATA_BADGE[r.data_status] || [r.data_status || "unknown", "warn"];
  return h("div", { class: "report-card" },
    h("div", { class: "deleg-head" }, icon("chart"), h("b", {}, `${r.agent_name} report`),
      h("span", { class: `data-badge ${tone}` }, label), r.confidence ? h("span", { class: "chip" }, `confidence: ${r.confidence}`) : null),
    r.data_notice ? h("div", { class: "data-notice" }, r.data_notice) : null,
    r.summary ? h("div", { class: "report-summary" }, r.summary) : null,
    r.sources?.length ? h("div", { class: "s" }, "Sources: ", r.sources.map((x) => `${x.source} (${x.is_live ? "live" : "not live"}, ${x.as_of || "no timestamp"})`).filter((v, i, a) => a.indexOf(v) === i).join("; ")) : null);
}

function toolChip(name, status, summary) {
  return h("div", { class: `tool-chip st-${status}` }, icon(CHIP_ICON[status] || "tools"), h("span", { class: "nm" }, name),
    h("span", { class: "sm" }, status === "running" ? "running…" : summary || status));
}

async function toggleHistory() {
  const panel = $("chat-history");
  if (!panel.classList.contains("hidden")) return panel.classList.add("hidden");
  panel.classList.remove("hidden");
  panel.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  try {
    const convs = await api("/api/conversations");
    panel.replaceChildren(
      h("div", { class: "inline", style: { marginBottom: "8px" } }, h("b", {}, "Conversations"), h("span", { class: "spacer", style: { flex: 1 } }),
        h("button", { class: "btn sm primary", onclick: () => newConversation() }, icon("plus"), "New")),
      convs.length ? h("div", { class: "rows" }, convs.map((c) => h("div", { class: "row clickable", onclick: () => { panel.classList.add("hidden"); loadConversation(c.id); } },
        h("div", { class: "grow" }, h("div", { class: "t" }, c.title), h("div", { class: "s" }, `${c.turns} turn(s) · ${relTime(c.updated_at)}`)),
        h("button", { class: "icon-btn", "aria-label": "Delete conversation", onclick: async (e) => {
          e.stopPropagation();
          if (!(await confirmDialog("Delete conversation", `Delete “${c.title}”? This can't be undone.`, "Delete"))) return;
          try { await del(`/api/conversations/${c.id}`); if (S.conv?.id === c.id) newConversation(); toggleHistory(); toggleHistory(); } catch (err) { toast(err.message, "err"); }
        } }, icon("trash")))))
        : h("div", { class: "empty" }, "No conversations yet."));
  } catch (e) { panel.replaceChildren(h("div", { class: "empty" }, e.message)); }
}

function setBusy(on) {
  S.busy = on;
  setIcon("chat-send", on ? "stop" : "send");
  $("chat-send").setAttribute("aria-label", on ? "Stop" : "Send");
  $("talk-sub").textContent = on ? "Thinking…" : "Open chat";
  setOrbState(on ? "thinking" : "idle");
  updateSub();
}

async function stopRun() {
  stopSpeaking();
  if (S.runId) { try { await post(`/api/runs/${S.runId}/cancel`); } catch { /* fall through to abort */ } }
  setTimeout(() => S.ctrl?.abort(), 1500);
}

function scroll() { const log = $("chat-log"); log.scrollTop = log.scrollHeight; }

export async function send(raw, { voiceReply = false } = {}) {
  const text = (raw || "").trim();
  if (!text || S.busy) return;
  openChat({ focus: false });
  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  S.voiceReply = voiceReply || !!voiceConfig().speak_replies;
  try {
    if (!S.conv) {
      S.conv = await post("/api/conversations", { agent: S.agent });
      $("chat-log").replaceChildren();
    }
  } catch (e) { return toast(e.message, "err"); }
  const log = $("chat-log");
  log.append(h("div", { class: "msg user" }, text));
  const thinking = h("div", { class: "thinking" }, h("i"), h("i"), h("i"), "Thinking");
  log.append(thinking);
  scroll();
  S.bubble = null; S.bubbleText = ""; S.lastText = ""; S.chips = {}; S.deleg = {};
  S.ctrl = new AbortController();
  setBusy(true);
  let renderQueued = false;
  const render = () => { renderQueued = false; if (S.bubble) { S.bubble.innerHTML = markdown(S.bubbleText); scroll(); } };

  const onEvent = (ev) => {
    switch (ev.type) {
      case "run":
        S.runId = ev.run_id;
        $("chat-sub").textContent = `${ev.provider_label} · ${ev.model || "no model"}`;
        break;
      case "delta":
        thinking.remove();
        if (!S.bubble) { S.bubble = assistantBubble(""); log.append(S.bubble); }
        S.bubbleText += ev.text;
        if (!renderQueued) { renderQueued = true; requestAnimationFrame(render); }
        break;
      case "tool_start": {
        thinking.remove();
        render(); // flush text still waiting for an animation frame before starting a new bubble
        if (S.bubbleText) S.lastText = S.bubbleText;
        S.bubble = null; S.bubbleText = "";
        const chip = toolChip(ev.name, "running");
        S.chips[ev.id] = chip; S.currentTool = ev.id; log.append(chip); scroll();
        break;
      }
      case "approval_required": {
        const waiting = toolChip(ev.approval.tool, "awaiting", "waiting for your approval");
        S.chips[S.currentTool]?.replaceWith(waiting);
        S.chips[S.currentTool] = waiting;
        const card = approvalCard(ev.approval);
        S.cards = { ...(S.cards || {}), [S.currentTool]: card };
        log.append(card); scroll();
        break;
      }
      case "approval_resolved": {
        const card = log.querySelector(`[data-approval="${ev.approval_id}"]`);
        if (card) markResolved(card, ev.decision);
        break;
      }
      case "agent_progress": {
        thinking.remove();
        let box = S.deleg?.[ev.agent];
        if (!box || ev.stage === "started") {
          box = delegationBox(ev.agent_name, ev.task);
          S.deleg = { ...(S.deleg || {}), [ev.agent]: box };
          log.append(box);
        }
        updateDelegation(box, ev);
        scroll();
        break;
      }
      case "tool_result": {
        const chip = toolChip(ev.name, ev.status, ev.summary);
        S.chips[ev.id]?.replaceWith(chip); S.chips[ev.id] = chip;
        if (ev.report) { log.append(reportCard(ev.report)); }
        if (ev.name === "delegate_to_specialist" && ev.status !== "ok") {
          const box = Object.values(S.deleg || {}).at(-1);
          if (box) box.querySelector(".deleg-state").textContent = ev.status;
        }
        const resolved = S.cards?.[ev.id]?.querySelector(".resolved");
        if (resolved && ev.status === "ok") resolved.textContent = "Approved — done.";
        else if (resolved && ["error", "timeout", "unavailable", "invalid"].includes(ev.status)) { resolved.textContent = `Approved, but it failed: ${ev.summary}`; resolved.className = "resolved st-error"; }
        log.append(thinking); scroll();
        break;
      }
      case "notice": log.append(h("div", { class: "notice" }, ev.message)); scroll(); break;
      case "done":
      case "cancelled":
        thinking.remove();
        if (ev.type === "cancelled") log.append(h("div", { class: "notice" }, "Stopped."));
        if (S.bubbleText) S.lastText = S.bubbleText;
        break;
      case "error":
        thinking.remove();
        log.append(assistantBubble(`**${ev.kind === "setup" ? "Setup required" : "Couldn't complete that"}** — ${ev.message}`, "error"));
        scroll();
        break;
      default: break;
    }
  };

  try {
    await streamPost(`/api/conversations/${S.conv.id}/messages`, { text, agent: S.agent }, onEvent, S.ctrl.signal);
  } catch (e) {
    thinking.remove();
    if (e.name !== "AbortError") log.append(assistantBubble(`**Connection problem** — ${e.message}`, "error"));
  } finally {
    render();
    setBusy(false);
    S.runId = null; S.ctrl = null;
    if (S.conv) $("chat-title").textContent = S.conv.title === "New conversation" ? text.slice(0, 60) : S.conv.title;
    document.dispatchEvent(new CustomEvent("ev:changed"));
    if (S.voiceReply && S.lastText) speak(S.lastText);
  }
}

function approvalCard(a) {
  const card = h("div", { class: `approval lv-${a.level}`, "data-approval": a.id },
    h("h4", {}, icon("shield"), "Approval needed", h("span", { class: `chip lv-${a.level}` }, a.level_label), h("span", { class: "muted small" }, a.tool)),
    h("pre", {}, a.summary),
    h("div", { class: "inline" },
      h("button", { class: "btn primary sm", onclick: () => decide(a.id, "approve", card) }, icon("check"), "Approve"),
      h("button", { class: "btn sm", onclick: () => decide(a.id, "deny", card) }, icon("x"), "Deny"),
      h("span", { class: "muted small" }, "Nothing happens until you choose.")));
  return card;
}

async function decide(id, decision, card) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try { await post(`/api/approvals/${id}`, { decision }); markResolved(card, decision === "approve" ? "approved" : "denied"); }
  catch (e) { toast(e.message, "err"); markResolved(card, "expired"); }
}

function markResolved(card, decision) {
  const row = card.querySelector(".inline");
  const label = { approved: "Approved — running…", denied: "Denied — nothing was done.", expired: "Expired — nothing was done.", cancelled: "Cancelled." }[decision] || decision;
  row?.replaceWith(h("div", { class: `resolved st-${decision === "approved" ? "ok" : "denied"}` }, label));
}

// Push-to-talk: listen, transcribe, send, and (for voice turns) speak the reply.
export async function voiceTurn(fromDock = true) {
  if (isRecording()) { const { stopListening } = await import("./voice.js"); stopListening(); return; }
  if (fromDock) openChat({ focus: false });
  $("chat-mic").classList.add("live");
  document.getElementById("mic-main")?.classList.add("live");
  const unlive = () => {
    $("chat-mic").classList.remove("live");
    document.getElementById("mic-main")?.classList.remove("live");
  };
  try {
    const text = await listen();
    unlive();
    if (text) await send(text, { voiceReply: true });
    else toast("I didn't catch that — tap the mic and try again.");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    unlive();
  }
}
