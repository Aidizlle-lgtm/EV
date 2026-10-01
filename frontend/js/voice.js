// Voice input/output.
// Input: records with MediaRecorder and transcribes on the server (Groq Whisper),
//        or falls back to the browser's SpeechRecognition when the server can't.
// Output: ElevenLabs through the backend (key never reaches the browser), or the
//         browser's built-in speechSynthesis as a labelled fallback.
import { api } from "./api.js";
import { setOrbState } from "./orb.js";

let cfg = { tts: "browser", stt: "browser", speak_replies: false };
let recorder = null, stream = null, audioCtx = null, analyser = null, rafId = 0;
let recognition = null, currentAudio = null;
const listeners = new Set();

export function onVoiceState(fn) { listeners.add(fn); }
function emit(state, detail = "") { for (const fn of listeners) fn(state, detail); }

export async function loadVoiceConfig() {
  try { cfg = await api("/api/voice/config"); } catch { /* keep defaults */ }
  return cfg;
}
export const voiceConfig = () => cfg;

export function micSupportIssue() {
  if (!window.isSecureContext) return "The microphone only works over HTTPS or on localhost. On your phone, use HTTPS (see README → Phone access).";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser doesn't support microphone access.";
  return null;
}

export function isRecording() { return !!recorder || !!recognition; }

// Waveform drawing for the sidebar voice panel: real mic levels when listening, flat line otherwise.
export function attachWave(canvas) {
  const ctx = canvas.getContext("2d");
  const data = new Uint8Array(64);
  function draw() {
    rafId = requestAnimationFrame(draw);
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const bars = 44, bw = w / bars;
    if (analyser) analyser.getByteFrequencyData(data);
    for (let i = 0; i < bars; i++) {
      const v = analyser ? data[Math.floor((i / bars) * data.length)] / 255 : 0.04;
      const bh = Math.max(2, v * h * 0.95);
      ctx.fillStyle = i < bars / 2 ? "#ff2d4a" : "#2f8cff";
      ctx.globalAlpha = analyser ? 0.95 : 0.45;
      ctx.fillRect(i * bw + 1, (h - bh) / 2, Math.max(1, bw - 2), bh);
    }
    ctx.globalAlpha = 1;
  }
  cancelAnimationFrame(rafId);
  draw();
}

function level() {
  if (!analyser) return 0;
  const d = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(d);
  let sum = 0;
  for (const v of d) sum += ((v - 128) / 128) ** 2;
  return Math.sqrt(sum / d.length);
}

// Start listening. Resolves with the transcript (or "" if nothing was heard).
export async function listen({ maxMs = 30000 } = {}) {
  if (isRecording()) { stopListening(); return ""; }
  stopSpeaking();
  if (cfg.stt !== "groq") return browserRecognize();
  const issue = micSupportIssue();
  if (issue) throw new Error(issue);
  stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const src = audioCtx.createMediaStreamSource(stream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((m) => window.MediaRecorder?.isTypeSupported?.(m)) || "";
  recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  const stopped = new Promise((res) => (recorder.onstop = res));
  recorder.start(250);
  emit("listening");

  // Stop automatically after ~1.4s of silence once speech was detected, or at maxMs.
  const started = Date.now();
  let heard = false, quietSince = 0;
  const vad = setInterval(() => {
    const l = level();
    setOrbState("listening", Math.min(1, l * 6));
    if (l > 0.035) { heard = true; quietSince = 0; } else if (heard && !quietSince) quietSince = Date.now();
    if ((heard && quietSince && Date.now() - quietSince > 1400) || Date.now() - started > maxMs) stopListening();
  }, 100);

  await stopped;
  clearInterval(vad);
  const type = recorder?.mimeType || mime || "audio/webm";
  cleanupMic();
  if (!heard || !chunks.length) { emit("idle", "Didn't catch anything."); return ""; }
  emit("transcribing");
  const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
  const form = new FormData();
  form.append("audio", new Blob(chunks, { type }), `speech.${ext}`);
  try {
    const { text } = await api("/api/voice/stt", { method: "POST", form });
    emit("idle");
    return text;
  } catch (e) {
    emit("error", e.message);
    throw e;
  }
}

export function stopListening() {
  if (recognition) { try { recognition.stop(); } catch { /* already stopped */ } return; }
  if (recorder && recorder.state !== "inactive") recorder.stop();
}

function cleanupMic() {
  stream?.getTracks().forEach((t) => t.stop());
  audioCtx?.close().catch(() => {});
  stream = null; audioCtx = null; analyser = null; recorder = null;
  setOrbState("idle");
}

function browserRecognize() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return Promise.reject(new Error("Voice input needs either a Groq key (Settings) or a browser with speech recognition, like Chrome or Edge."));
  return new Promise((resolve, reject) => {
    recognition = new SR();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    let text = "";
    recognition.onresult = (e) => { text = Array.from(e.results).map((r) => r[0].transcript).join(" "); };
    recognition.onerror = (e) => { recognition = null; emit("error", e.error); setOrbState("idle"); reject(new Error(`Speech recognition error: ${e.error}`)); };
    recognition.onend = () => { recognition = null; emit("idle"); setOrbState("idle"); resolve(text.trim()); };
    emit("listening", "browser recognizer");
    setOrbState("listening", 0.4);
    recognition.start();
  });
}

// ---------- speaking ----------
function plain(text) {
  return text.replace(/```[\s\S]*?```/g, " (code omitted) ").replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/[*_#>|]+/g, " ").replace(/\s+/g, " ").trim();
}

export async function speak(text) {
  stopSpeaking();
  if (!text || cfg.tts === "off") return;
  if (cfg.tts === "elevenlabs") {
    try {
      const job = await api("/api/voice/tts", { method: "POST", body: { text } });
      currentAudio = new Audio(job.url);
      currentAudio.onplay = () => { emit("speaking"); setOrbState("speaking", 0.5); };
      currentAudio.onended = currentAudio.onerror = () => { emit("idle"); setOrbState("idle"); currentAudio = null; };
      await currentAudio.play();
      return;
    } catch (e) {
      emit("error", `ElevenLabs: ${e.message}. Using the browser voice.`);
    }
  }
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(plain(text).slice(0, 1500));
  u.rate = 1.03;
  u.onstart = () => { emit("speaking", "browser voice"); setOrbState("speaking", 0.4); };
  u.onend = u.onerror = () => { emit("idle"); setOrbState("idle"); };
  speechSynthesis.speak(u);
}

export function stopSpeaking() {
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}
