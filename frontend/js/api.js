// Thin client for the E.V. backend. Every write carries the CSRF header.

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

async function parseError(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join("; ");
    return JSON.stringify(body);
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

export async function api(path, { method = "GET", body, form, signal } = {}) {
  const headers = { "X-EV-Request": "1" };
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, signal, credentials: "same-origin" });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "Can't reach E.V.'s server. Is it still running on your laptop?");
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) { onUnauthorized(); throw new ApiError(401, "Please sign in again."); }
  if (!res.ok) throw new ApiError(res.status, await parseError(res));
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

export const get = (p) => api(p);
export const post = (p, body) => api(p, { method: "POST", body: body ?? {} });
export const patch = (p, body) => api(p, { method: "PATCH", body });
export const put = (p, body) => api(p, { method: "PUT", body });
export const del = (p) => api(p, { method: "DELETE" });

// Server-Sent Events over POST (EventSource can't POST). Calls onEvent for each JSON event.
export async function streamPost(path, body, onEvent, signal) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST", signal, credentials: "same-origin",
      headers: { "X-EV-Request": "1", "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "Lost connection to E.V.'s server.");
  }
  if (res.status === 401) { onUnauthorized(); throw new ApiError(401, "Please sign in again."); }
  if (!res.ok) throw new ApiError(res.status, await parseError(res));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { console.warn("bad event", e); }
      }
    }
  }
}
