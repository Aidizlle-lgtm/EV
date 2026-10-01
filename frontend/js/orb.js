// The AI Core: a rotating wireframe sphere with orbiting signals. Purely visual;
// its speed and glow reflect E.V.'s real state (idle / thinking / listening / speaking).

const state = { mode: "idle", level: 0 };
export function setOrbState(mode, level = 0) { state.mode = mode; state.level = level; }
export function orbMode() { return state.mode; }

export function startOrb(canvas) {
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = 1, rot = 0, t = 0, raf = 0;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = r.width; h = r.height;
    canvas.width = Math.max(1, w * dpr); canvas.height = Math.max(1, h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);
  resize();

  function project(x, y, z, R, cx, cy, tilt) {
    const cy1 = Math.cos(tilt), sy1 = Math.sin(tilt);
    const y2 = y * cy1 - z * sy1, z2 = y * sy1 + z * cy1;
    const persp = 1 / (1 + z2 / (R * 4));
    return [cx + x * persp, cy + y2 * persp, z2];
  }

  function frame() {
    if (!canvas.isConnected) { cancelAnimationFrame(raf); ro.disconnect(); return; }
    raf = requestAnimationFrame(frame);
    const speed = { idle: 0.0035, thinking: 0.014, listening: 0.007, speaking: 0.009 }[state.mode] || 0.004;
    if (!reduce) { rot += speed; t += 0.016; }
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const pulse = state.mode === "listening" || state.mode === "speaking" ? state.level * 0.12 : state.mode === "thinking" ? Math.sin(t * 6) * 0.015 : 0;
    const R = Math.min(w * 0.32, h * 0.36) * (1 + pulse);
    const tilt = -0.35;

    // glow
    const g = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, R * 1.6);
    g.addColorStop(0, state.mode === "thinking" ? "rgba(255,60,90,0.35)" : "rgba(47,140,255,0.30)");
    g.addColorStop(0.5, "rgba(47,140,255,0.08)");
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);

    // orbits (behind)
    const orbits = [
      { rx: R * 2.15, ry: R * 0.62, rot: -0.12, sp: 0.45, color: "rgba(255,45,74,0.55)" },
      { rx: R * 1.75, ry: R * 0.42, rot: 0.08, sp: -0.6, color: "rgba(47,140,255,0.55)" },
    ];
    for (const o of orbits) {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(o.rot);
      ctx.strokeStyle = o.color; ctx.lineWidth = 1.1;
      ctx.beginPath(); ctx.ellipse(0, 0, o.rx, o.ry, 0, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }

    // wireframe sphere
    const lat = 9, lon = 14;
    ctx.lineWidth = 1;
    for (let i = 1; i < lat; i++) {
      const phi = (i / lat) * Math.PI;
      ctx.beginPath();
      for (let j = 0; j <= 48; j++) {
        const th = (j / 48) * Math.PI * 2 + rot;
        const [x, y, z] = project(R * Math.sin(phi) * Math.cos(th), R * Math.cos(phi), R * Math.sin(phi) * Math.sin(th), R, cx, cy, tilt);
        j ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.strokeStyle = "rgba(90,160,255,0.28)"; ctx.stroke();
    }
    for (let j = 0; j < lon; j++) {
      const th = (j / lon) * Math.PI * 2 + rot;
      ctx.beginPath();
      let front = 0;
      for (let i = 0; i <= 32; i++) {
        const phi = (i / 32) * Math.PI;
        const [x, y, z] = project(R * Math.sin(phi) * Math.cos(th), R * Math.cos(phi), R * Math.sin(phi) * Math.sin(th), R, cx, cy, tilt);
        front += z;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      const red = Math.cos(th) > 0.55;
      ctx.strokeStyle = red ? "rgba(255,70,100,0.55)" : front < 0 ? "rgba(110,175,255,0.55)" : "rgba(60,120,220,0.22)";
      ctx.stroke();
    }
    // nodes
    for (let i = 1; i < lat; i++) {
      for (let j = 0; j < lon; j += 2) {
        const phi = (i / lat) * Math.PI, th = (j / lon) * Math.PI * 2 + rot;
        const [x, y, z] = project(R * Math.sin(phi) * Math.cos(th), R * Math.cos(phi), R * Math.sin(phi) * Math.sin(th), R, cx, cy, tilt);
        if (z > 0) continue;
        ctx.fillStyle = (i + j) % 5 === 0 ? "rgba(255,90,112,0.95)" : "rgba(160,205,255,0.8)";
        ctx.beginPath(); ctx.arc(x, y, 1.4, 0, Math.PI * 2); ctx.fill();
      }
    }
    // rim
    ctx.strokeStyle = "rgba(120,185,255,0.75)"; ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

    // orbit signals (front)
    for (const [k, o] of orbits.entries()) {
      for (let n = 0; n < 2; n++) {
        const a = t * o.sp + n * Math.PI + k;
        const px = Math.cos(a) * o.rx, py = Math.sin(a) * o.ry;
        const x = cx + px * Math.cos(o.rot) - py * Math.sin(o.rot);
        const y = cy + px * Math.sin(o.rot) + py * Math.cos(o.rot);
        const rg = ctx.createRadialGradient(x, y, 0, x, y, 12);
        rg.addColorStop(0, k ? "rgba(140,200,255,1)" : "rgba(255,90,110,1)");
        rg.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = rg; ctx.beginPath(); ctx.arc(x, y, 12, 0, Math.PI * 2); ctx.fill();
      }
    }
    // base ripple
    ctx.save(); ctx.translate(cx, cy + R * 1.05);
    for (let r = 0; r < 4; r++) {
      ctx.strokeStyle = `rgba(255,45,74,${0.35 - r * 0.07})`; ctx.setLineDash([2, 5]);
      ctx.beginPath(); ctx.ellipse(0, 0, R * (0.7 + r * 0.22), R * (0.1 + r * 0.03), 0, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.restore(); ctx.setLineDash([]);
  }
  frame();
}

// Memory constellation: one node per memory category, sized by its real count.
export function drawConstellation(canvas, categories) {
  const ctx = canvas.getContext("2d");
  const r = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = r.width * dpr; canvas.height = r.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, r.width, r.height);
  if (!categories.length) {
    ctx.fillStyle = "#5a6e90"; ctx.font = "13px Rajdhani, sans-serif"; ctx.textAlign = "center";
    ctx.fillText("No memories saved yet", r.width / 2, r.height / 2);
    return;
  }
  const max = Math.max(...categories.map((c) => c.n));
  const pts = categories.map((c, i) => {
    const a = (i / categories.length) * Math.PI * 2 + 0.6;
    const rad = Math.min(r.width, r.height) * (0.22 + (i % 2) * 0.14);
    return { x: r.width / 2 + Math.cos(a) * rad * 1.4, y: r.height / 2 + Math.sin(a) * rad, s: 3 + (c.n / max) * 5, label: c.category, n: c.n };
  });
  ctx.strokeStyle = "rgba(47,140,255,0.6)"; ctx.lineWidth = 1;
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
  ctx.stroke();
  for (const p of pts) {
    ctx.fillStyle = "#ff2d4a"; ctx.shadowColor = "#ff2d4a"; ctx.shadowBlur = 8;
    ctx.beginPath(); ctx.arc(p.x, p.y, p.s, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0; ctx.fillStyle = "#8ea3c4"; ctx.font = "11px Rajdhani, sans-serif"; ctx.textAlign = "center";
    ctx.fillText(`${p.label} (${p.n})`, p.x, p.y - p.s - 4);
  }
}
