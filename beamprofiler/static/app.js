'use strict';
/* Beam Profiler front end: receives frames + results over a WebSocket,
   draws the image, overlays, profiles and trends, and sends control changes. */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// Canvas colours and fonts for the two styles; the page itself is styled in style.css / pixel.css.
const THEMES = {
  classic: {
    x: '#ff8a5c', y: '#5ad6a4', fit: '#e8edf5', grid: '#232b36', axis: '#8591a3', accent: '#4cc2ff',
    frame: '#323c4a', imgBg: '#05070a', imgBorder: '#2b3442', fitEdge: '#6b7686', warn: '#f2b632', mark: '#ffffff',
    mono: 'ui-monospace, Menlo, Consolas, monospace', scale: 1,
  },
  pixel: {
    x: '#ef7d57', y: '#a7f070', fit: '#f4f4f4', grid: '#2a3150', axis: '#94b0c2', accent: '#73eff7',
    frame: '#566c86', imgBg: '#0b0c15', imgBorder: '#566c86', fitEdge: '#94b0c2', warn: '#ffcd75', mark: '#f4f4f4',
    mono: '"Pixelify Sans", ui-monospace, monospace', scale: 1.2,
  },
};
let COL = THEMES.classic;
const isPixel = () => COL === THEMES.pixel;
const cfont = (px, weight) => `${weight ? weight + ' ' : ''}${Math.round(px * COL.scale)}px ${COL.mono}`;

// ------------------------------------------------------------------ state
const UI_DEFAULTS = {
  widthDef: 'd4xy', showAs: 'radius', unit: 'um', colormap: 'beam',
  ovCross: true, ovEllipse: true, ovSat: true, power: '', isat: 1.67, histWindow: '120', tool: 'roi', theme: 'classic',
};
let ui = { ...UI_DEFAULTS };
try { ui = { ...ui, ...JSON.parse(localStorage.getItem('beamprofiler.ui') || '{}') }; } catch (e) { /* private mode */ }
function saveUi() { try { localStorage.setItem('beamprofiler.ui', JSON.stringify(ui)); } catch (e) { /* ignore */ } }

let settings = {};
let rev = -1;
let simParams = null;
let hdr = null;            // latest packet header
let lastGray = null;       // latest 8-bit image (for re-colouring)
let origin = null;         // position zero, in µm
let history = [];          // {t, a, b, cx, cy} in µm (radii)
let m2pts = [];            // {z, wx, wy} radii in µm
let m2fit = null;
let connected = false;

// --------------------------------------------------------------- helpers
async function api(method, path, body, raw) {
  const opt = { method };
  if (raw !== undefined) opt.body = raw;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(j.detail || r.statusText || 'Request failed');
  return j;
}

function toast(html, isErr, ms, cls) {
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '') + (cls ? ' ' + cls : '');
  el.innerHTML = html;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms || (isErr ? 8000 : 6000));
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fail = (e) => toast(esc(e.message || e), true);

function trimNum(v) { return String(Number(Number(v).toPrecision(6))); }
const unitName = () => (ui.unit === 'mm' ? 'mm' : 'µm');
function fmtLen(um, dec) {
  if (um == null || !isFinite(um)) return '–';
  return ui.unit === 'mm' ? (um / 1000).toFixed(dec != null ? dec + 3 : 4) : um.toFixed(dec != null ? dec : 1);
}
const widthMult = () => (ui.showAs === 'diameter' ? 2 : 1);
const mean = (a) => a.reduce((s, v) => s + v, 0) / (a.length || 1);
function std(a) { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(a.reduce((s, v) => s + (v - m) * (v - m), 0) / (a.length - 1)); }

/** The two beam widths selected by "Beam width from", as 1/e² radii in µm. */
function widthPair(h) {
  const r = h && h.res;
  if (!r) return null;
  const k = h.um_per_px;
  const mul = (v, f) => (v == null ? null : v * f);
  switch (ui.widthDef) {
    case 'd4pa': return { a: mul(r.d4maj, k / 2), b: mul(r.d4min, k / 2), la: 'Major axis', lb: 'Minor axis', phi: r.phi, cx: r.cx, cy: r.cy, name: 'D4σ' };
    case 'g1': return { a: mul(r.fit_wx, k), b: mul(r.fit_wy, k), la: 'X', lb: 'Y', phi: 0, cx: r.cx, cy: r.cy, name: 'Gaussian fit' };
    case 'g2': return r.g2
      ? { a: mul(r.g2.wmaj, k), b: mul(r.g2.wmin, k), la: 'Major axis', lb: 'Minor axis', phi: r.g2.phi, cx: r.g2.cx, cy: r.g2.cy, name: '2D Gaussian fit' }
      : { a: null, b: null, la: 'Major axis', lb: 'Minor axis', phi: 0, cx: r.cx, cy: r.cy, name: '2D Gaussian fit' };
    default: return { a: mul(r.d4x, k / 2), b: mul(r.d4y, k / 2), la: 'X', lb: 'Y', phi: 0, cx: r.cx, cy: r.cy, name: 'D4σ' };
  }
}

// ------------------------------------------------------------- colormaps
const CMAPS = {
  gray: [[0, 0, 0, 0], [1, 255, 255, 255]],
  beam: [[0, 0, 0, 0], [0.12, 30, 0, 110], [0.28, 0, 50, 255], [0.44, 0, 205, 230], [0.6, 30, 225, 50], [0.76, 255, 240, 0], [0.9, 255, 50, 0], [1, 255, 255, 255]],
  jet: [[0, 0, 0, 128], [0.125, 0, 0, 255], [0.375, 0, 255, 255], [0.625, 255, 255, 0], [0.875, 255, 0, 0], [1, 128, 0, 0]],
  viridis: ['440154', '482878', '3e4989', '31688e', '26828e', '1f9e89', '35b779', '6ece58', 'b5de2b', 'fde725'],
  inferno: ['000004', '1b0c41', '4a0c6b', '781c6d', 'a52c60', 'cf4446', 'ed6925', 'fb9b06', 'f7d13d', 'fcffa4'],
  // stepped (posterised) palettes for the pixel look; the measurement is unaffected
  pixel: { steps: ['1a1c2c', '333c57', '29366f', '3b5dc9', '41a6f6', '73eff7', '38b764', 'a7f070', 'ffcd75', 'ef7d57', 'f4f4f4'] },
  gameboy: { steps: ['0f380f', '306230', '8bac0f', '9bbc0f'] },
};
let lut = new Uint32Array(256);
function buildLut() {
  let stops = CMAPS[ui.colormap] || CMAPS.beam;
  if (stops.steps) {
    const n = stops.steps.length;
    for (let i = 0; i < 256; i++) {
      const hex = stops.steps[Math.min(n - 1, Math.floor((Math.min(i, 254) / 254) * n))];
      const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
      lut[i] = (255 << 24) | (b << 16) | (g << 8) | r;
    }
    if (ui.ovSat) lut[255] = (255 << 24) | (255 << 16) | (0 << 8) | 255;
    return;
  }
  if (typeof stops[0] === 'string') {
    stops = stops.map((hex, i, arr) => [i / (arr.length - 1), parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]);
  }
  for (let i = 0; i < 256; i++) {
    const t = Math.min(i, 254) / 254;
    let j = 0;
    while (j < stops.length - 2 && t > stops[j + 1][0]) j++;
    const s0 = stops[j], s1 = stops[j + 1];
    const f = Math.min(1, Math.max(0, (t - s0[0]) / ((s1[0] - s0[0]) || 1)));
    const r = Math.round(s0[1] + (s1[1] - s0[1]) * f), g = Math.round(s0[2] + (s1[2] - s0[2]) * f), b = Math.round(s0[3] + (s1[3] - s0[3]) * f);
    lut[i] = (255 << 24) | (b << 16) | (g << 8) | r;
  }
  if (ui.ovSat) lut[255] = (255 << 24) | (255 << 16) | (0 << 8) | 255; // magenta = saturated
}

// ------------------------------------------------------------ image view
const cvImg = $('#img');
const off = document.createElement('canvas');
let offData = null;
const view = { k: 1, ox: 0, oy: 0, auto: true };
let sensor = { w: 0, h: 0 };
let draft = null;     // ROI being drawn
let drag = null;
let dirtyImg = true;

function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  const w = Math.max(10, Math.round(r.width)), h = Math.max(10, Math.round(r.height));
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function paintFrame(gray, w, h) {
  if (off.width !== w || off.height !== h) { off.width = w; off.height = h; offData = null; }
  const octx = off.getContext('2d');
  if (!offData) offData = octx.createImageData(w, h);
  const out = new Uint32Array(offData.data.buffer);
  for (let i = 0, n = w * h; i < n; i++) out[i] = lut[gray[i]];
  octx.putImageData(offData, 0, 0);
  dirtyImg = true;
}

function fitView() {
  const r = cvImg.getBoundingClientRect();
  if (!sensor.w || r.width < 10) return;
  view.k = Math.min(r.width / sensor.w, r.height / sensor.h) * 0.98;
  view.ox = (r.width - sensor.w * view.k) / 2;
  view.oy = (r.height - sensor.h * view.k) / 2;
  view.auto = true;
  dirtyImg = true;
}
function zoomTo(rc) {
  const r = cvImg.getBoundingClientRect();
  view.k = Math.min(r.width / rc.w, r.height / rc.h) * 0.9;
  view.ox = r.width / 2 - (rc.x + rc.w / 2) * view.k;
  view.oy = r.height / 2 - (rc.y + rc.h / 2) * view.k;
  view.auto = false;
  dirtyImg = true;
}
const toSensor = (e) => { const r = cvImg.getBoundingClientRect(); return { x: (e.clientX - r.left - view.ox) / view.k, y: (e.clientY - r.top - view.oy) / view.k, mx: e.clientX - r.left, my: e.clientY - r.top }; };
function shownRoi() { if (draft) return draft; const r = hdr && hdr.roi; return r && !r.full ? r : null; }

function renderImage() {
  const { ctx, w, h } = setupCanvas(cvImg);
  ctx.fillStyle = COL.imgBg;
  ctx.fillRect(0, 0, w, h);
  if (!off.width || !sensor.w || !hdr || !hdr.cam) return;
  const k = view.k;
  ctx.imageSmoothingEnabled = !isPixel() && k * (hdr.img.scale || 1) < 2;
  ctx.drawImage(off, view.ox, view.oy, sensor.w * k, sensor.h * k);
  ctx.strokeStyle = COL.imgBorder;
  ctx.lineWidth = 1;
  ctx.strokeRect(view.ox - 0.5, view.oy - 0.5, sensor.w * k + 1, sensor.h * k + 1);
  const X = (px) => view.ox + px * k, Y = (py) => view.oy + py * k;

  // region of interest
  const r = shownRoi();
  if (r) {
    const auto = !draft && hdr.roi && hdr.roi.auto;
    ctx.strokeStyle = draft ? COL.mark : auto ? COL.warn : COL.accent;
    ctx.lineWidth = 1.25;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(X(r.x), Y(r.y), r.w * k, r.h * k);
    ctx.setLineDash([]);
    ctx.fillStyle = ctx.strokeStyle;
    if (!auto && ui.tool === 'roi') for (const [hx, hy] of [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]]) ctx.fillRect(X(hx) - 3.5, Y(hy) - 3.5, 7, 7);
    ctx.font = cfont(11);
    ctx.fillText(`${auto ? 'Tracking ' : ''}ROI ${Math.round(r.w)}×${Math.round(r.h)} px`, X(r.x) + 4, Math.max(12, Y(r.y) - 5));
  }

  const res = hdr.res, prof = hdr.prof;
  if (res && prof && ui.ovCross) {
    const rr = hdr.roi || { x: 0, y: 0, w: sensor.w, h: sensor.h };
    ctx.lineWidth = 1;
    ctx.globalAlpha = 0.85;
    if (prof.mode === 'slice') {
      ctx.strokeStyle = COL.x;
      ctx.beginPath(); ctx.moveTo(X(rr.x), Y(prof.cy + 0.5)); ctx.lineTo(X(rr.x + rr.w), Y(prof.cy + 0.5)); ctx.stroke();
      ctx.strokeStyle = COL.y;
      ctx.beginPath(); ctx.moveTo(X(prof.cx + 0.5), Y(rr.y)); ctx.lineTo(X(prof.cx + 0.5), Y(rr.y + rr.h)); ctx.stroke();
    } else {
      ctx.strokeStyle = COL.mark;
      const cx = X(res.cx + 0.5), cy = Y(res.cy + 0.5);
      ctx.beginPath(); ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy); ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  const wp = widthPair(hdr);
  if (wp && ui.ovEllipse && wp.a && wp.b) {
    const ra = wp.a / hdr.um_per_px * k, rb = wp.b / hdr.um_per_px * k;
    const cx = X(wp.cx + 0.5), cy = Y(wp.cy + 0.5);
    ctx.strokeStyle = COL.mark;
    ctx.lineWidth = isPixel() ? 2 : 1.5;
    ctx.beginPath(); ctx.ellipse(cx, cy, ra, rb, wp.phi, 0, 2 * Math.PI); ctx.stroke();
    if (ui.widthDef === 'd4pa' || ui.widthDef === 'g2') {
      const c = Math.cos(wp.phi), s = Math.sin(wp.phi);
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(cx - ra * c, cy - ra * s); ctx.lineTo(cx + ra * c, cy + ra * s);
      ctx.moveTo(cx + rb * s, cy - rb * c); ctx.lineTo(cx - rb * s, cy + rb * c);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
  if (settings.profile_center === 'cursor' && settings.cursor) {
    const cx = X(settings.cursor.x), cy = Y(settings.cursor.y);
    ctx.strokeStyle = COL.mark; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, 6, 0, 2 * Math.PI); ctx.stroke();
  }
  if (hdr.status && hdr.status.paused) {
    ctx.fillStyle = COL.warn;
    ctx.font = cfont(12, 600);
    ctx.fillText('FROZEN', 10, h - 10);
  }
}

// mouse interaction on the image
function hitHandle(r, p) {
  const tol = 9 / view.k;
  for (const [name, hx, hy] of [['nw', r.x, r.y], ['ne', r.x + r.w, r.y], ['sw', r.x, r.y + r.h], ['se', r.x + r.w, r.y + r.h]]) {
    if (Math.abs(p.x - hx) < tol && Math.abs(p.y - hy) < tol) return name;
  }
  return null;
}
function rectFrom(x0, y0, x1, y1) {
  const cl = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  x0 = cl(x0, 0, sensor.w); x1 = cl(x1, 0, sensor.w); y0 = cl(y0, 0, sensor.h); y1 = cl(y1, 0, sensor.h);
  return { x: Math.round(Math.min(x0, x1)), y: Math.round(Math.min(y0, y1)), w: Math.round(Math.abs(x1 - x0)), h: Math.round(Math.abs(y1 - y0)) };
}
let spaceDown = false;
cvImg.addEventListener('pointerdown', (e) => {
  if (!sensor.w) return;
  const p = toSensor(e);
  const tool = (e.button === 1 || e.altKey || spaceDown) ? 'pan' : ui.tool;
  if (e.button === 2) return;
  cvImg.setPointerCapture(e.pointerId);
  if (tool === 'pan') drag = { type: 'pan', mx: p.mx, my: p.my, ox: view.ox, oy: view.oy };
  else if (tool === 'cursor') {
    if (p.x >= 0 && p.y >= 0 && p.x < sensor.w && p.y < sensor.h) postSettings({ cursor: { x: p.x, y: p.y }, profile_center: 'cursor', profile_mode: 'slice' });
  } else {
    const r = shownRoi();
    const auto = hdr && hdr.roi && hdr.roi.auto;
    const hnd = r && !auto ? hitHandle(r, p) : null;
    if (hnd) {
      const fx = hnd.includes('w') ? r.x + r.w : r.x, fy = hnd.includes('n') ? r.y + r.h : r.y;
      drag = { type: 'resize', fx, fy };
    } else if (r && !auto && p.x > r.x && p.x < r.x + r.w && p.y > r.y && p.y < r.y + r.h) drag = { type: 'move', p0: p, r0: { ...r } };
    else drag = { type: 'new', p0: p };
  }
  e.preventDefault();
});
cvImg.addEventListener('pointermove', (e) => {
  const p = toSensor(e);
  if (sensor.w && hdr && hdr.um_per_px && p.x >= 0 && p.y >= 0 && p.x < sensor.w && p.y < sensor.h) {
    const k = hdr.um_per_px;
    $('#hover').textContent = `x ${fmtLen(p.x * k)}  y ${fmtLen(p.y * k)} ${unitName()}   (px ${Math.floor(p.x)}, ${Math.floor(p.y)})`;
  } else $('#hover').textContent = '';
  if (!drag) return;
  if (drag.type === 'pan') { view.ox = drag.ox + p.mx - drag.mx; view.oy = drag.oy + p.my - drag.my; view.auto = false; }
  else if (drag.type === 'new') {
    const rc = rectFrom(drag.p0.x, drag.p0.y, p.x, p.y);
    draft = (rc.w * view.k > 5 && rc.h * view.k > 5) ? rc : null;
  } else if (drag.type === 'resize') draft = rectFrom(drag.fx, drag.fy, p.x, p.y);
  else if (drag.type === 'move') {
    const r0 = drag.r0;
    const x = Math.min(Math.max(r0.x + p.x - drag.p0.x, 0), sensor.w - r0.w);
    const y = Math.min(Math.max(r0.y + p.y - drag.p0.y, 0), sensor.h - r0.h);
    draft = { x: Math.round(x), y: Math.round(y), w: r0.w, h: r0.h };
  }
  dirtyImg = true;
});
function endDrag() {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (d.type !== 'pan' && draft && draft.w >= 8 && draft.h >= 8) {
    const roi = draft;
    postSettings({ roi, auto_roi: false }).finally(() => { draft = null; dirtyImg = true; });
  } else { draft = null; dirtyImg = true; }
}
cvImg.addEventListener('pointerup', endDrag);
cvImg.addEventListener('pointercancel', endDrag);
cvImg.addEventListener('wheel', (e) => {
  if (!sensor.w) return;
  e.preventDefault();
  const p = toSensor(e);
  const r = cvImg.getBoundingClientRect();
  const kFit = Math.min(r.width / sensor.w, r.height / sensor.h);
  const k2 = Math.min(Math.max(view.k * Math.exp(-e.deltaY * 0.0015), kFit * 0.5), 60);
  view.ox = p.mx - (p.mx - view.ox) * (k2 / view.k);
  view.oy = p.my - (p.my - view.oy) * (k2 / view.k);
  view.k = k2;
  view.auto = false;
  dirtyImg = true;
}, { passive: false });
cvImg.addEventListener('dblclick', fitView);
cvImg.addEventListener('pointerleave', () => { $('#hover').textContent = ''; });

// ---------------------------------------------------------------- plots
function niceTicks(lo, hi, n) {
  const span = (hi - lo) || 1;
  const raw = span / Math.max(1, n);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return { ticks, dec: Math.min(6, Math.max(0, -Math.floor(Math.log10(step) + 1e-9))) };
}
function tickLabel(v, dec) {
  const a = Math.abs(v);
  if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return v.toFixed(dec);
}
/** opt: {series:[{x,y,color,dash,pts,width}], vlines:[{x,color}], xLabel, yLabel, yMin, empty} */
function drawPlot(cv, opt) {
  const { ctx, w, h } = setupCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  const m = { l: 48, r: 10, t: 8, b: opt.xLabel ? 30 : 20 };
  const pw = w - m.l - m.r, ph = h - m.t - m.b;
  if (pw < 20 || ph < 20) return;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of opt.series) for (let i = 0; i < s.x.length; i++) {
    const xv = s.x[i], yv = s.y[i];
    if (yv == null || !isFinite(yv) || !isFinite(xv)) continue;
    if (xv < x0) x0 = xv; if (xv > x1) x1 = xv; if (yv < y0) y0 = yv; if (yv > y1) y1 = yv;
  }
  ctx.font = cfont(10.5);
  if (!isFinite(x0) || !isFinite(y0)) {
    ctx.fillStyle = COL.axis; ctx.textAlign = 'center';
    ctx.fillText(opt.empty || 'No data', w / 2, h / 2);
    return;
  }
  if (opt.xRange) [x0, x1] = opt.xRange;
  if (x1 === x0) { x0 -= 1; x1 += 1; }
  if (opt.yMin != null) y0 = Math.min(y0, opt.yMin);
  if (y1 === y0) { const d = Math.abs(y0) * 0.01 || 1; y0 -= d; y1 += d; }
  const pad = (y1 - y0) * 0.07;
  y1 += pad; if (opt.yMin == null || y0 < opt.yMin) y0 -= pad;
  const PX = (v) => m.l + (v - x0) / (x1 - x0) * pw, PY = (v) => m.t + ph - (v - y0) / (y1 - y0) * ph;
  const tx = niceTicks(x0, x1, Math.max(2, Math.floor(pw / 70))), ty = niceTicks(y0, y1, Math.max(2, Math.floor(ph / 26)));
  ctx.strokeStyle = COL.grid; ctx.lineWidth = 1; ctx.fillStyle = COL.axis;
  if (isPixel()) ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const v of tx.ticks) { const px = Math.round(PX(v)) + 0.5; ctx.moveTo(px, m.t); ctx.lineTo(px, m.t + ph); ctx.fillText(tickLabel(v, tx.dec), px, m.t + ph + 4); }
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const v of ty.ticks) { const py = Math.round(PY(v)) + 0.5; ctx.moveTo(m.l, py); ctx.lineTo(m.l + pw, py); ctx.fillText(tickLabel(v, ty.dec), m.l - 5, py); }
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = COL.frame; ctx.strokeRect(m.l + 0.5, m.t + 0.5, pw, ph);
  if (opt.xLabel) { ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillText(opt.xLabel, m.l + pw / 2, h - 1); }
  if (opt.yLabel) { ctx.save(); ctx.translate(10, m.t + ph / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(opt.yLabel, 0, 0); ctx.restore(); }
  ctx.save();
  ctx.beginPath(); ctx.rect(m.l, m.t, pw, ph); ctx.clip();
  for (const vl of (opt.vlines || [])) {
    ctx.strokeStyle = vl.color; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(PX(vl.x), m.t); ctx.lineTo(PX(vl.x), m.t + ph); ctx.stroke();
  }
  ctx.setLineDash([]);
  for (const s of opt.series) {
    ctx.strokeStyle = s.color; ctx.fillStyle = s.color; ctx.lineWidth = isPixel() ? 2 : (s.width || 1.4);
    if (s.pts) {
      for (let i = 0; i < s.x.length; i++) {
        if (s.y[i] == null) continue;
        if (isPixel()) { ctx.fillRect(Math.round(PX(s.x[i])) - 3, Math.round(PY(s.y[i])) - 3, 6, 6); continue; }
        ctx.beginPath(); ctx.arc(PX(s.x[i]), PY(s.y[i]), 3.2, 0, 2 * Math.PI); ctx.fill();
      }
      continue;
    }
    const stair = isPixel() && s.step;  // pixel style: draw camera data as steps, one per pixel
    ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    let pen = false;
    let lastY = 0;
    for (let i = 0; i < s.x.length; i++) {
      const yv = s.y[i];
      if (yv == null || !isFinite(yv)) { pen = false; continue; }
      const px = PX(s.x[i]), py = PY(yv);
      if (pen) { if (stair) ctx.lineTo(px, lastY); ctx.lineTo(px, py); } else { ctx.moveTo(px, py); pen = true; }
      lastY = py;
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();
}

function drawProfile(cv, p, color, infoEl) {
  if (!p || !hdr) {
    const off = hdr && hdr.status && hdr.status.measure === false;
    drawPlot(cv, { series: [], empty: off ? 'Measurement off' : 'No beam' });
    infoEl.textContent = '';
    return;
  }
  const k = hdr.um_per_px * (ui.unit === 'mm' ? 1e-3 : 1);
  const xs = new Array(p.v.length);
  for (let i = 0; i < xs.length; i++) xs[i] = (p.x0 + i * p.dx) * k;
  const series = [{ x: xs, y: p.v, color, step: true }];
  const vlines = [];
  if (p.fit) {
    const f = p.fit;
    const fy = xs.map((_, i) => { const d = (p.x0 + i * p.dx - f.x0) / f.w; return f.c + f.a * Math.exp(-2 * d * d); });
    series.push({ x: xs, y: fy, color: COL.fit, dash: [5, 4], width: 1.1 });
    vlines.push({ x: (f.x0 - f.w) * k, color: COL.fitEdge }, { x: (f.x0 + f.w) * k, color: COL.fitEdge });
    infoEl.textContent = `fit w = ${fmtLen(f.w * hdr.um_per_px)} ${unitName()}`;
  } else infoEl.textContent = 'fit failed';
  drawPlot(cv, { series, vlines, yMin: 0 });
}

// ------------------------------------------------------- results / status
function row(label, value) { return `<tr><td>${label}</td><td>${value}</td></tr>`; }
function pair(a, b, dec) { return `${fmtLen(a, dec)} · ${fmtLen(b, dec)} ${unitName()}`; }

function updateStatus() {
  const h = hdr;
  const badge = $('#sourceBadge');
  const cam = h && h.cam;
  if (!connected) { badge.textContent = 'Disconnected from the profiler program'; badge.className = 'badge err'; }
  else if (!cam) { badge.textContent = 'No camera'; badge.className = 'badge err'; }
  else if (cam.kind === 'sim') { badge.textContent = 'SIMULATED BEAM — not a real measurement'; badge.className = 'badge sim'; }
  else if (cam.kind === 'file') { badge.textContent = 'File: ' + cam.name; badge.className = 'badge file'; }
  else { badge.textContent = cam.name; badge.className = 'badge live'; }
  badge.title = badge.textContent;

  const ov = $('#overlayMsg');
  if (connected && !cam) {
    ov.hidden = false;
    $('#ovTitle').textContent = 'Camera not connected';
    $('#ovText').textContent = (h && h.error) || 'No camera is open.';
  } else if (!connected) {
    ov.hidden = false;
    $('#ovTitle').textContent = 'Not connected';
    $('#ovText').textContent = 'The profiler program is not running or cannot be reached. Start it again and this page will reconnect.';
  } else ov.hidden = true;
  $('#ovRetry').hidden = $('#ovLoad').hidden = $('#ovSim').hidden = !connected;

  const st = (h && h.status) || {};
  $('#stFps').textContent = cam && cam.kind !== 'file' && cam.fps != null ? cam.fps.toFixed(1) : '–';
  $('#stExp').textContent = cam && cam.kind !== 'file' ? `${trimNum(Number(cam.exposure_ms).toPrecision(4))} ms` : '–';
  const pk = st.peak_pct;
  $('#stPeak').textContent = pk != null ? pk.toFixed(0) + ' %' : '–';
  const bar = $('#stPeakBar');
  bar.style.width = (pk != null ? Math.min(100, pk) : 0) + '%';
  bar.style.background = st.sat_px > 0 || pk >= 98 ? 'var(--bad)' : pk < 25 || pk > 92 ? 'var(--warn)' : 'var(--ok)';
  $('#stMsg').textContent = st.measure === false ? '' : (st.msg || '');  // the Measure button already says it

  const paused = !!settings.paused;
  const measuring = settings.measure !== false;
  $('#btnMeasure').textContent = measuring ? 'Measure: on' : 'Measure: off';
  $('#btnMeasure').classList.toggle('off', !measuring);
  $('#btnPause').textContent = paused ? 'Resume' : 'Freeze';
  $('#btnPause').classList.toggle('active', paused);
  const lg = st.log;
  $('#btnLog').textContent = lg ? `Stop log (${lg.rows})` : 'Start log';
  $('#btnLog').classList.toggle('rec', !!lg);

  const di = $('#darkInfo');
  if (st.dark_progress) { di.textContent = `Capturing ${st.dark_progress[0]}/${st.dark_progress[1]}…`; di.className = 'hint'; }
  else if (st.dark) {
    const mismatch = cam && cam.kind !== 'file' && (Math.abs(st.dark.exposure_ms - cam.exposure_ms) > 0.02 * cam.exposure_ms || Math.abs(st.dark.gain_db - cam.gain_db) > 0.05);
    di.textContent = `Stored: ${st.dark.frames} frames at ${trimNum(Number(st.dark.exposure_ms).toPrecision(4))} ms` + (mismatch ? ' — exposure has changed since, capture again' : '');
    di.className = 'hint' + (mismatch ? ' warn' : '');
  } else { di.textContent = 'No dark frame stored.'; di.className = 'hint'; }

  // ROI fields follow the active region
  if (h && h.roi) for (const [id, key] of [['#roiX', 'x'], ['#roiY', 'y'], ['#roiW', 'w'], ['#roiH', 'h']]) {
    const el = $(id);
    if (document.activeElement !== el) el.value = h.roi[key];
  }
}

function updateResults() {
  const h = hdr, r = h && h.res, st = (h && h.status) || {};
  const wp = widthPair(h);
  const mult = widthMult();
  const sym = ui.showAs === 'diameter' ? 'diameter' : 'radius';
  $('#tileLa').textContent = wp ? wp.la : 'X';
  $('#tileLb').textContent = wp ? wp.lb : 'Y';
  const off = st.measure === false;
  $('#tileA').innerHTML = off ? 'off' : wp && wp.a != null ? `${fmtLen(wp.a * mult)}<small>${unitName()}</small>` : '–';
  $('#tileB').innerHTML = off ? 'off' : wp && wp.b != null ? `${fmtLen(wp.b * mult)}<small>${unitName()}</small>` : '–';
  const defName = { d4xy: 'D4σ (ISO 11146) along the sensor axes', d4pa: 'D4σ (ISO 11146) along the beam’s own axes', g1: 'Gaussian fit to the X and Y profiles', g2: '2D elliptical Gaussian fit' }[ui.widthDef];
  $('#tileCap').textContent = `1/e² ${sym} · ${defName}`;

  const warn = [];
  const cam = h && h.cam;
  if (cam && cam.kind === 'sim') warn.push(['info', 'Simulated beam. Connect the camera on the Windows PC for real measurements.']);
  if (st.sat_px > 0) warn.push(['bad', `Saturated (${st.sat_px} px). Lower the exposure or add ND filters — widths read too large when saturated.`]);
  else if (r && st.peak_pct != null && st.peak_pct < 25) warn.push(['', 'Low signal. Raise the exposure for a cleaner measurement.']);
  if (cam && off) warn.push(['info', 'Measurement is off: no widths or fits, and the live image runs at full speed. Press M or “Measure” to turn it back on.']);
  else if (cam && !r && st.msg) warn.push(['', st.msg + '.']);
  if (r && r.edge_ratio != null && r.edge_ratio < 1.5) warn.push(['', 'The beam is cut off by the edge of the region or sensor, so D4σ reads low. Enlarge the region, or use the Gaussian fit.']);
  if (r && ui.widthDef === 'g2' && !r.g2) {
    warn.push(['', settings.fit2d ? '2D fit did not converge on this frame.' : 'Starting the 2D fit…']);
  }
  if (st.avg_n > 1 && st.avg_n < settings.averaging) warn.push(['info', `Averaging: ${st.avg_n}/${settings.averaging} frames collected.`]);
  $('#warnings').innerHTML = warn.map(([c, t]) => `<li class="${c}">${esc(t)}</li>`).join('');

  const tb = $('#results tbody');
  if (!r) { tb.innerHTML = ''; return; }
  const k = h.um_per_px;
  const half = ui.showAs === 'diameter' ? 1 : 0.5;   // D4σ is a diameter
  const d4lab = ui.showAs === 'diameter' ? 'D4σ' : 'D4σ / 2';
  const gl = ui.showAs === 'diameter' ? '2w' : 'w';
  let html = '';
  html += row(`${d4lab} — X · Y`, pair(r.d4x * k * half, r.d4y * k * half));
  html += row(`${d4lab} — major · minor`, pair(r.d4maj * k * half, r.d4min * k * half));
  html += row(`Gaussian ${gl} — X · Y`, pair(r.fit_wx != null ? r.fit_wx * k * mult : null, r.fit_wy != null ? r.fit_wy * k * mult : null));
  if (r.g2) html += row(`2D Gaussian ${gl} — major · minor`, pair(r.g2.wmaj * k * mult, r.g2.wmin * k * mult));
  html += row('FWHM — X · Y', pair(r.fit_wx != null ? r.fit_wx * k * 1.17741 : null, r.fit_wy != null ? r.fit_wy * k * 1.17741 : null));
  html += row('Ellipticity (minor / major)', r.d4maj ? (r.d4min / r.d4maj).toFixed(3) : '–');
  let ang = -(r.g2 && ui.widthDef === 'g2' ? r.g2.phi : r.phi) * 180 / Math.PI;
  html += row('Major-axis angle', `${ang.toFixed(1)}°`);
  const ox = origin ? origin.x : 0, oy = origin ? origin.y : 0;
  html += row(origin ? 'Centroid from zero — X · Y' : 'Centroid — X · Y', pair(r.cx * k - ox, r.cy * k - oy));
  html += row('Peak level', st.peak_pct != null ? `${st.peak_pct.toFixed(1)} % of full scale` : '–');
  html += row('Baseline · noise', `${st.baseline != null ? st.baseline.toFixed(1) : '–'} · ${st.noise != null ? st.noise.toFixed(2) : '–'} counts`);
  html += row('Total signal', r.total != null ? Number(r.total).toExponential(3) + ' counts' : '–');
  const P = parseFloat(ui.power);
  if (P > 0 && wp && wp.a && wp.b) {
    const I0 = 2 * P / (Math.PI * (wp.a * 1e-4) * (wp.b * 1e-4));   // mW/cm²
    html += row('Peak intensity', `${I0 >= 1000 ? (I0 / 1000).toFixed(3) + ' W/cm²' : I0.toFixed(2) + ' mW/cm²'}`);
    const isat = parseFloat(ui.isat);
    if (isat > 0) html += row('I₀ / I<sub>sat</sub>', (I0 / isat).toFixed(2));
  }
  tb.innerHTML = html;
}

// --------------------------------------------------------------- history
function pushHistory() {
  const h = hdr;
  if (!h || !h.res || !h.cam || h.cam.kind === 'file' || (h.status && h.status.paused)) return;
  const wp = widthPair(h);
  if (!wp) return;
  history.push({ t: h.t, a: wp.a, b: wp.b, cx: h.res.cx * h.um_per_px, cy: h.res.cy * h.um_per_px });
  if (history.length > 30000) history.splice(0, 5000);
}
function windowed() {
  const win = parseFloat(ui.histWindow);
  if (!history.length) return [];
  if (!win) return history;
  const t1 = history[history.length - 1].t;
  let i = history.length - 1;
  while (i > 0 && t1 - history[i - 1].t <= win) i--;
  return history.slice(i);
}
let activeTab = 'trend';
function drawBottom() {
  const pts = windowed();
  const mult = widthMult(), u = ui.unit === 'mm' ? 1e-3 : 1;
  const tEnd = pts.length ? pts[pts.length - 1].t : 0;
  const ts = pts.map((p) => p.t - tEnd);
  const wp = widthPair(hdr) || { la: 'X', lb: 'Y' };
  if (activeTab === 'trend') {
    const a = pts.map((p) => (p.a == null ? null : p.a * mult * u)), b = pts.map((p) => (p.b == null ? null : p.b * mult * u));
    drawPlot($('#plotTrend'), { series: [{ x: ts, y: a, color: COL.x }, { x: ts, y: b, color: COL.y }], xLabel: 'time (s)', yLabel: `${ui.showAs} (${unitName()})`, empty: 'Waiting for a beam…' });
    const av = a.filter((v) => v != null), bv = b.filter((v) => v != null);
    const d = ui.unit === 'mm' ? 4 : 1, d2 = ui.unit === 'mm' ? 5 : 2;
    $('#trendStats').innerHTML = av.length
      ? `<span class="x">${wp.la}</span> <b>${mean(av).toFixed(d)} ± ${std(av).toFixed(d2)}</b> ${unitName()} &nbsp; <span class="y">${wp.lb}</span> <b>${mean(bv).toFixed(d)} ± ${std(bv).toFixed(d2)}</b> ${unitName()} &nbsp; (mean ± std, ${av.length} samples)`
      : '';
  } else if (activeTab === 'point') {
    const mx = mean(pts.map((p) => p.cx)), my = mean(pts.map((p) => p.cy));
    const dx = pts.map((p) => (p.cx - mx) * u), dy = pts.map((p) => (p.cy - my) * u);
    drawPlot($('#plotPoint'), { series: [{ x: ts, y: dx, color: COL.x }, { x: ts, y: dy, color: COL.y }], xLabel: 'time (s)', yLabel: `centroid − mean (${unitName()})`, empty: 'Waiting for a beam…' });
    const d = ui.unit === 'mm' ? 5 : 2;
    const pp = (v) => (v.length ? Math.max(...v) - Math.min(...v) : 0);
    $('#pointStats').innerHTML = pts.length
      ? `<span class="x">X</span> RMS <b>${std(dx).toFixed(d)}</b> · p-p <b>${pp(dx).toFixed(d)}</b> ${unitName()} &nbsp; <span class="y">Y</span> RMS <b>${std(dy).toFixed(d)}</b> · p-p <b>${pp(dy).toFixed(d)}</b> ${unitName()} &nbsp; (${pts.length} samples)`
      : '';
  } else drawM2();
}

// -------------------------------------------------------------- M² tool
function caustic(fit, z) { return Math.sqrt(fit.w0_um * fit.w0_um + Math.pow(fit.theta_mrad * (z - fit.z0_mm), 2)); }
function drawM2() {
  const mult = widthMult(), u = ui.unit === 'mm' ? 1e-3 : 1;
  const zs = m2pts.map((p) => p.z);
  const series = [];
  if (m2pts.length) {
    const z0 = Math.min(...zs), z1 = Math.max(...zs), span = (z1 - z0) || 10;
    const grid = Array.from({ length: 121 }, (_, i) => z0 - 0.08 * span + (i / 120) * span * 1.16);
    for (const [ax, color] of [['x', COL.x], ['y', COL.y]]) {
      const f = m2fit && m2fit[ax];
      if (f) series.push({ x: grid, y: grid.map((z) => caustic(f, z) * mult * u), color, width: 1.2 });
      series.push({ x: zs, y: m2pts.map((p) => (p['w' + ax] == null ? null : p['w' + ax] * mult * u)), color, pts: true });
    }
  }
  drawPlot($('#plotM2'), { series, xLabel: 'z (mm)', yLabel: `${ui.showAs} (${unitName()})`, yMin: 0, empty: 'No points yet' });
}
function renderM2() {
  const wp = widthPair(hdr) || { la: 'X', lb: 'Y' };
  const mult = widthMult();
  $('#m2ha').textContent = `${wp.la} (${unitName()})`;
  $('#m2hb').textContent = `${wp.lb} (${unitName()})`;
  $('#m2tbl tbody').innerHTML = m2pts.map((p, i) => `<tr><td>${trimNum(p.z)}</td><td>${fmtLen(p.wx != null ? p.wx * mult : null)}</td><td>${fmtLen(p.wy != null ? p.wy * mult : null)}</td><td><button data-del="${i}" title="Remove this point" aria-label="Remove point">✕</button></td></tr>`).join('');
  const el = $('#m2res');
  if (m2pts.length < 3) el.textContent = `Move the camera along the beam, type its position z and click “Add point”. Three points are needed for a fit (${m2pts.length} so far).`;
  else {
    const line = (cls, lbl, f) => f
      ? `<span class="${cls}">${lbl}</span>: waist w₀ <b>${fmtLen(f.w0_um)} ${unitName()}</b> at z <b>${f.z0_mm.toFixed(2)} mm</b> · z<sub>R</sub> <b>${f.zr_mm.toFixed(2)} mm</b> · half-angle <b>${f.theta_mrad.toFixed(3)} mrad</b> · M² <b>${f.m2.toFixed(2)}</b>`
      : `<span class="${cls}">${lbl}</span>: no fit (the points do not describe a focus)`;
    el.innerHTML = line('x', wp.la, m2fit && m2fit.x) + '<br>' + line('y', wp.lb, m2fit && m2fit.y);
  }
  if (activeTab === 'm2') drawM2();
}
async function fitM2() {
  m2fit = null;
  if (m2pts.length >= 3) {
    try { m2fit = await api('POST', '/api/caustic/fit', { points: m2pts, wavelength_nm: settings.wavelength_nm }); } catch (e) { fail(e); }
  }
  renderM2();
}
$('#m2Add').addEventListener('click', () => {
  const z = parseFloat($('#m2z').value);
  if (!isFinite(z)) return toast('Type the camera position z in mm first.', true);
  const recent = history.slice(-10).filter((p) => hdr && hdr.t - p.t < 3);
  const wp = widthPair(hdr);
  if (!wp || (!recent.length && wp.a == null)) return toast('No beam measurement available to add.', true);
  const av = recent.map((p) => p.a).filter((v) => v != null), bv = recent.map((p) => p.b).filter((v) => v != null);
  m2pts.push({ z, wx: av.length ? mean(av) : wp.a, wy: bv.length ? mean(bv) : wp.b, def: ui.widthDef });
  m2pts.sort((p, q) => p.z - q.z);
  const step = parseFloat($('#m2step').value);
  if (isFinite(step)) $('#m2z').value = trimNum(z + step);
  fitM2();
});
$('#m2Clear').addEventListener('click', () => { m2pts = []; m2fit = null; renderM2(); });
$('#m2tbl').addEventListener('click', (e) => {
  const i = e.target.dataset && e.target.dataset.del;
  if (i !== undefined) { m2pts.splice(Number(i), 1); fitM2(); }
});
$('#m2Export').addEventListener('click', () => {
  if (!m2pts.length) return toast('No points to export.', true);
  let csv = 'z_mm,radius_1_um,radius_2_um\n' + m2pts.map((p) => `${p.z},${p.wx ?? ''},${p.wy ?? ''}`).join('\n') + '\n';
  for (const ax of ['x', 'y']) {
    const f = m2fit && m2fit[ax];
    if (f) csv += `# axis ${ax === 'x' ? 1 : 2}: w0_um=${f.w0_um.toFixed(3)}, z0_mm=${f.z0_mm.toFixed(4)}, zR_mm=${f.zr_mm.toFixed(4)}, theta_mrad=${f.theta_mrad.toFixed(5)}, M2=${f.m2.toFixed(4)}\n`;
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = 'beam_caustic.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
});

// ---------------------------------------------------- settings plumbing
function applyState(st) {
  settings = st.settings;
  rev = st.rev;
  simParams = st.sim;
  $$('[data-s]').forEach((el) => {
    const v = settings[el.dataset.s];
    if (v === undefined || v === null) return;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (document.activeElement === el && el.type === 'number') return;
    else if (el.type === 'number') el.value = trimNum(v);
    else el.value = String(v);
  });
  const cam = st.camera;
  const live = cam && cam.kind !== 'file';
  $('#secCamera').style.opacity = live ? 1 : 0.45;
  $('#gainRow').hidden = !(cam && cam.gain_range[1] > cam.gain_range[0]);
  if (cam) {
    expRange = [Math.max(cam.exp_range[0], 0.01), Math.min(cam.exp_range[1], 1000)];
    const sl = $('#expSlider');
    if (document.activeElement !== sl && expRange[1] > expRange[0]) sl.value = Math.round(1000 * Math.log(settings.exposure_ms / expRange[0]) / Math.log(expRange[1] / expRange[0]));
  }
  $('#secSim').hidden = !simParams;
  ensureFit2d();
  if (simParams) $$('[data-sim]').forEach((el) => { if (document.activeElement !== el) el.value = trimNum(simParams[el.dataset.sim]); });
  dirtyImg = true;
}
/** "Beam width from: 2D Gaussian fit" needs the server's 2D fit, which is off after every start. */
let fit2dAskedRev = -1;
function ensureFit2d() {
  if (ui.widthDef !== 'g2' || settings.fit2d || fit2dAskedRev === rev) return;
  fit2dAskedRev = rev;
  postSettings({ fit2d: true });
}
let expRange = [0.04, 1000];
const settingsInFlight = new Set();
async function postSettings(patch) {
  const req = api('POST', '/api/settings', patch);
  settingsInFlight.add(req);
  try { applyState(await req); } catch (e) { fail(e); } finally { settingsInFlight.delete(req); }
}
let expPending = null;  // exposure from the slider, waiting for its short delay
/** Send what is still being edited (a number being typed, the slider) and wait until the server has it. */
async function commitPendingEdits() {
  const a = document.activeElement;
  if (a && a.matches && a.matches('input, select')) a.blur();  // blur fires "change", which sends the value
  if (expPending) { clearTimeout(expTimer); const p = expPending; expPending = null; postSettings(p); }
  await Promise.allSettled(Array.from(settingsInFlight));
  applyState(await api('GET', '/api/state'));
}
let stateBusy = false;
async function refreshState() {
  if (stateBusy) return;
  stateBusy = true;
  try { applyState(await api('GET', '/api/state')); } catch (e) { /* retried on the next packet */ }
  stateBusy = false;
}

$$('[data-s]').forEach((el) => el.addEventListener('change', () => {
  if (el.dataset.s === 'fit2d' && !el.checked && ui.widthDef === 'g2') {
    ui.widthDef = 'd4pa';
    saveUi(); applyUi(); history = []; redrawAll();
    toast('2D fit switched off: the widths now show D4σ along the major and minor axes.');
  }
  let v;
  if (el.type === 'checkbox') v = el.checked;
  else if (el.type === 'number' || el.dataset.num !== undefined) { v = parseFloat(el.value); if (!isFinite(v)) return; }
  else v = el.value;
  postSettings({ [el.dataset.s]: v });
}));
let expTimer = null;
$('#expSlider').addEventListener('input', (e) => {
  const v = expRange[0] * Math.pow(expRange[1] / expRange[0], e.target.value / 1000);
  $('#exp').value = trimNum(Number(v.toPrecision(3)));
  clearTimeout(expTimer);
  expPending = { exposure_ms: Number(v.toPrecision(3)), auto_exposure: false };
  expTimer = setTimeout(() => { const p = expPending; expPending = null; if (p) postSettings(p); }, 80);
});
$('#exp').addEventListener('change', () => postSettings({ auto_exposure: false }));
$$('[data-sim]').forEach((el) => el.addEventListener('change', async () => {
  const v = parseFloat(el.value);
  if (!isFinite(v)) return;
  try { applyState(await api('POST', '/api/sim', { [el.dataset.sim]: v })); } catch (e) { fail(e); }
}));

function applyTheme() {
  const name = THEMES[ui.theme] ? ui.theme : 'classic';
  document.documentElement.dataset.theme = name;
  COL = THEMES[name];
  // canvas text needs the pixel font loaded before drawing with it
  if (name === 'pixel' && document.fonts) document.fonts.load(cfont(11)).then(redrawAll, () => {});
}
function applyUi() {
  applyTheme();
  $$('[data-ui]').forEach((el) => {
    const v = ui[el.dataset.ui];
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  });
  $$('[data-tool]').forEach((b) => b.classList.toggle('on', b.dataset.tool === ui.tool));
  cvImg.style.cursor = ui.tool === 'pan' ? 'grab' : 'crosshair';
  buildLut();
  if (lastGray && hdr && hdr.img.w) paintFrame(lastGray, hdr.img.w, hdr.img.h);
}
$$('[data-ui]').forEach((el) => el.addEventListener('change', () => {
  const key = el.dataset.ui;
  ui[key] = el.type === 'checkbox' ? el.checked : el.value;
  saveUi();
  if (key === 'widthDef') {
    history = [];
    ensureFit2d();
  }
  applyUi();
  updateResults();
  renderM2();
  redrawAll();
}));
$$('[data-tool]').forEach((b) => b.addEventListener('click', () => { ui.tool = b.dataset.tool; saveUi(); applyUi(); dirtyImg = true; }));
$('#btnTheme').addEventListener('click', () => {
  ui.theme = isPixel() ? 'classic' : 'pixel';
  saveUi(); applyUi(); redrawAll();
  if (isPixel()) toast('★ PIXEL MODE ★ &nbsp;<span class="muted">The measurements are unchanged.</span>', false, 3000, 'saved');
});

for (const id of ['#roiX', '#roiY', '#roiW', '#roiH']) $(id).addEventListener('change', () => {
  const v = ['#roiX', '#roiY', '#roiW', '#roiH'].map((s) => parseFloat($(s).value));
  if (v.every(isFinite)) postSettings({ roi: { x: v[0], y: v[1], w: v[2], h: v[3] }, auto_roi: false });
});
$('#btnRoiFull').addEventListener('click', () => postSettings({ roi: null, auto_roi: false }));
$('#btnRoiZoom').addEventListener('click', () => { const r = shownRoi(); if (r) zoomTo(r); else fitView(); });
$('#btnFit').addEventListener('click', fitView);
$('#btnOrigin').addEventListener('click', () => {
  if (origin) { origin = null; $('#btnOrigin').textContent = 'Set position zero'; }
  else if (hdr && hdr.res) { origin = { x: hdr.res.cx * hdr.um_per_px, y: hdr.res.cy * hdr.um_per_px }; $('#btnOrigin').textContent = 'Clear position zero'; }
  else toast('No beam to take the zero position from.', true);
  $('#btnOrigin').classList.toggle('active', !!origin);
  updateResults();
});

// "Open folder" in Explorer/Finder works only when the page runs on the profiler's own computer.
const IS_LOCAL = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
function openFolderLink(kind) {
  return IS_LOCAL ? ` <a href="#" data-open-folder="${kind}">open folder</a>` : '';
}
document.addEventListener('click', (ev) => {
  const a = ev.target.closest && ev.target.closest('a[data-open-folder]');
  if (!a) return;
  ev.preventDefault();
  api('POST', `/api/open-folder/${a.dataset.openFolder}`).catch(fail);
});

// -------------------------------------------------------- dialog in page
function askModal(title, html, buttons) {
  return new Promise((resolve) => {
    const m = $('#modal'), row = $('#modalBtns');
    $('#modalTitle').textContent = title;
    $('#modalText').innerHTML = html;
    row.innerHTML = '';
    const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(null); } };
    const done = (v) => { m.hidden = true; document.removeEventListener('keydown', onKey, true); resolve(v); };
    for (const b of buttons) {
      const el = document.createElement('button');
      el.className = `btn ${b.cls || ''}`;
      el.textContent = b.label;
      el.addEventListener('click', () => done(b.value));
      row.appendChild(el);
    }
    document.addEventListener('keydown', onKey, true);
    m.hidden = false;
    (row.querySelector('.primary') || row.firstChild).focus();
  });
}

// ------------------------------------------------------------ quick save
let qsBusy = false;
let qsCount = 0;
let qsFolderSaved = '';  // the folder the server has confirmed
function applyQs(q) {
  qsFolderSaved = q.folder;
  const f = $('#qsFolder');
  if (document.activeElement !== f) { f.value = q.folder; f.scrollLeft = f.scrollWidth; }  // show the end of a long path
  f.readOnly = !q.can_choose_folder;
  f.title = q.can_choose_folder ? q.folder : `${q.folder}\n(Only the computer running the profiler can change the folder.)`;
  if (document.activeElement !== $('#qsName')) $('#qsName').value = q.name;
  if (document.activeElement !== $('#qsNote')) $('#qsNote').value = q.note;
  $('#qsRaw').checked = !!q.raw;
  $('#qsPick').hidden = $('#qsOpen').hidden = !q.can_choose_folder;
}
async function loadQs() { try { applyQs(await api('GET', '/api/quicksave')); } catch (e) { /* server not reachable yet */ } }
async function qsPrefs(patch) {
  try { applyQs(await api('POST', '/api/quicksave/prefs', patch)); } catch (e) { fail(e); loadQs(); }
}
$('#qsFolder').addEventListener('change', (e) => qsPrefs({ folder: e.target.value }));
$('#qsName').addEventListener('change', (e) => qsPrefs({ name: e.target.value }));
$('#qsNote').addEventListener('change', (e) => qsPrefs({ note: e.target.value }));
$('#qsRaw').addEventListener('change', (e) => qsPrefs({ raw: e.target.checked }));
$('#qsPick').addEventListener('click', async () => {
  const b = $('#qsPick');
  b.disabled = true;
  try {
    const q = await api('POST', '/api/quicksave/pick-folder');
    applyQs(q);
    if (!q.cancelled) toast(`Quick saves now go to<br><b>${esc(q.folder)}</b>`);
  } catch (e) { fail(e); } finally { b.disabled = false; }
});
$('#qsOpen').addEventListener('click', () => api('POST', '/api/open-folder/saves').catch(fail));

const viewPrefs = () => { const v = { ...ui }; delete v.tool; return v; };
function celebrate(el) {
  if (!isPixel() || !el) return;
  const r = el.getBoundingClientRect();
  const f = document.createElement('div');
  f.className = 'floater';
  f.textContent = '+1 SAVE';
  f.style.left = `${r.left + r.width / 2}px`;
  f.style.top = `${r.bottom + 2}px`;
  document.body.appendChild(f);
  setTimeout(() => f.remove(), 1100);
}
/** Save without asking; only an existing file name brings up a question. */
async function quickSave(overwrite, nameOverride) {
  if (qsBusy || !$('#modal').hidden) return;
  if (settings.measure === false) { toast('Measurement is off, so there are no results to save. Press M or “Measure” to turn it on.', true); return; }
  qsBusy = true;
  const fIn = $('#qsFolder');
  if (!fIn.readOnly && fIn.value.trim() && fIn.value.trim() !== qsFolderSaved) {
    // a folder typed but not yet confirmed (e.g. Ctrl+S straight after typing): use it
    try { applyQs(await api('POST', '/api/quicksave/prefs', { folder: fIn.value })); } catch (e) { qsBusy = false; fail(e); return; }
  }
  const defs = [...new Set(m2pts.map((p) => p.def))];
  const body = {
    name: nameOverride || $('#qsName').value,
    note: $('#qsNote').value,
    raw: $('#qsRaw').checked,
    overwrite: !!overwrite,
    view: viewPrefs(),
    m2: m2pts.length ? {
      width_definition: defs.length === 1 ? defs[0] : 'mixed',
      points: m2pts.map((p) => ({ z_mm: p.z, radius_1_um: p.wx, radius_2_um: p.wy, width_definition: p.def })),
      fit: m2fit,
    } : null,
  };
  let r;
  let j = {};
  try {
    r = await fetch('/api/quicksave', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    try { j = await r.json(); } catch (e) { /* empty body */ }
  } catch (e) { qsBusy = false; fail(e); return; }
  qsBusy = false;
  if (r.status === 409 && j.exists) {
    const files = (j.existing && j.existing.length ? j.existing : [`${j.name}.json`]).map(esc).join(' and ');
    const choice = await askModal('This file name is taken', `<b>${files}</b> already ${j.existing && j.existing.length > 1 ? 'exist' : 'exists'} in<br><span class="muted">${esc(j.folder)}</span>`, [
      { label: `Save as ${j.suggestion}`, value: 'new', cls: 'primary' },
      { label: 'Overwrite', value: 'over', cls: 'danger' },
      { label: 'Cancel', value: null, cls: 'ghost' },
    ]);
    if (choice === 'over') await quickSave(true, j.name);
    else if (choice === 'new') await quickSave(false, j.suggestion);
    return;
  }
  if (!r.ok) { toast(esc(j.detail || r.statusText || 'Save failed'), true); return; }
  qsCount++;
  $('#qsName').value = j.next_name;
  $('#qsLast').innerHTML = `Last save: <b>${esc(j.name)}</b> at ${new Date().toLocaleTimeString()} · ${qsCount} this session`;
  const warn = j.beam_found ? '' : '<br><span class="warnText">No beam was found, so the results in it are empty.</span>';
  toast(`${isPixel() ? '★ ' : ''}Saved <b>${esc(j.files.join(', '))}</b>${warn}<br><span class="muted">${esc(j.folder)}</span>` + openFolderLink('saves'),
    !j.beam_found, 6000, j.beam_found ? 'saved' : '');
  if (j.summary_error) toast(esc(j.summary_error), true, 12000);
  celebrate($('#btnQuickSave'));
}
$('#qsSave').addEventListener('click', () => quickSave());
$('#btnQuickSave').addEventListener('click', () => quickSave());

// --------------------------------------------------------- settings file
const SETTINGS_SKIP = new Set(['paused', 'measure']);
function settingsDoc() {
  const s = {};
  for (const [k, v] of Object.entries(settings)) if (!SETTINGS_SKIP.has(k)) s[k] = v;
  return {
    format: 'beamprofiler-settings', format_version: 1, saved: new Date().toISOString(),
    camera: hdr && hdr.cam ? hdr.cam.name : null, settings: s, view: viewPrefs(),
  };
}
$('#setSave').addEventListener('click', async () => {
  // e.g. an exposure typed just before clicking: make sure the file gets the value now in use
  try { await commitPendingEdits(); } catch (e) { fail(e); return; }
  const text = JSON.stringify(settingsDoc(), null, 1);
  const name = 'beamprofiler-settings.json';
  if (window.showSaveFilePicker) {
    try {
      const h = await window.showSaveFilePicker({ suggestedName: name, types: [{ description: 'Beam profiler settings', accept: { 'application/json': ['.json'] } }] });
      const w = await h.createWritable();
      await w.write(text);
      await w.close();
      toast(`Settings saved to <b>${esc(h.name)}</b>`, false, 5000, 'saved');
      return;
    } catch (e) {
      if (e.name === 'AbortError') return;  // the user closed the dialog
    }
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast(`Settings saved as <b>${name}</b> in the Downloads folder.`);
});
$('#setLoad').addEventListener('click', () => $('#setFile').click());
$('#setFile').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (!f) return;
  let doc;
  try { doc = JSON.parse(await f.text()); } catch (err) { toast(`${esc(f.name)} is not a settings file (not valid JSON).`, true); return; }
  const s = doc && typeof doc === 'object' ? doc.settings : null;
  if (!s || typeof s !== 'object') { toast(`${esc(f.name)} contains no settings.`, true); return; }
  const patch = {};
  for (const [k, v] of Object.entries(s)) if (k in settings && !SETTINGS_SKIP.has(k)) patch[k] = v;
  let nView = 0;
  if (doc.view && typeof doc.view === 'object') {
    for (let [k, v] of Object.entries(doc.view)) {
      const d = UI_DEFAULTS[k];
      // number inputs keep what was typed as text (e.g. I_sat "1.67")
      if (typeof d === 'number' && typeof v === 'string' && v.trim() !== '' && isFinite(Number(v))) v = Number(v);
      if (k in UI_DEFAULTS && k !== 'tool' && typeof v === typeof d) { ui[k] = v; nView++; }
    }
    saveUi(); applyUi(); history = []; updateResults(); renderM2(); redrawAll();
  }
  try { applyState(await api('POST', '/api/settings', patch)); } catch (err) { fail(err); return; }
  toast(`Loaded ${Object.keys(patch).length} settings${nView ? ` and ${nView} display preferences` : ''} from <b>${esc(f.name)}</b>.`);
});

$('#btnPause').addEventListener('click', () => postSettings({ paused: !settings.paused }));
function toggleMeasure() { postSettings({ measure: settings.measure === false }); }
$('#btnMeasure').addEventListener('click', toggleMeasure);
$('#btnSnap').addEventListener('click', async () => {
  try {
    const r = await api('POST', '/api/snapshot');
    toast(`Snapshot saved in<br><b>${esc(r.folder)}</b><br>` + r.files.map((f) => `<a href="/api/files/snapshots/${encodeURIComponent(f)}" download>${esc(f.split('.').pop())}</a>`).join('') + openFolderLink('snapshots'), false, 12000);
  } catch (e) { fail(e); }
});
$('#btnLog').addEventListener('click', async () => {
  try {
    if (hdr && hdr.status && hdr.status.log) {
      const r = await api('POST', '/api/log/stop');
      if (r.name) toast(`Log saved in<br><b>${esc(r.folder)}</b><br><a href="/api/files/logs/${encodeURIComponent(r.name)}" download>${esc(r.name)}</a>` + openFolderLink('logs'), false, 12000);
    } else {
      const r = await api('POST', '/api/log/start');
      toast(`Logging to ${esc(r.name)}`);
    }
  } catch (e) { fail(e); }
});
$('#btnDark').addEventListener('click', async () => {
  try { await api('POST', '/api/dark/capture', { frames: 16 }); toast('Capturing dark frame — keep the beam blocked.'); } catch (e) { fail(e); }
});
$('#btnDarkClear').addEventListener('click', async () => { try { applyState(await api('POST', '/api/dark/clear')); } catch (e) { fail(e); } });

async function uploadFile(f) {
  if (!f) return;
  try { applyState(await api('POST', '/api/load?name=' + encodeURIComponent(f.name), undefined, f)); history = []; view.auto = true; }
  catch (e) { fail(e); }
}
async function goLive(path) {
  try {
    const st = await api('POST', path);
    applyState(st);
    history = [];
    if (!st.camera && st.error) toast(esc(st.error), true);
  } catch (e) { fail(e); }
}
$('#btnLoad').addEventListener('click', () => $('#fileInput').click());
$('#ovLoad').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', (e) => { uploadFile(e.target.files[0]); e.target.value = ''; });
$('#btnLive').addEventListener('click', () => goLive('/api/source/live'));
$('#ovRetry').addEventListener('click', () => goLive('/api/source/live'));
$('#ovSim').addEventListener('click', () => goLive('/api/source/sim'));
const wrap = $('#imgwrap');
wrap.addEventListener('dragover', (e) => { e.preventDefault(); $('#dropHint').hidden = false; });
wrap.addEventListener('dragleave', () => { $('#dropHint').hidden = true; });
wrap.addEventListener('drop', (e) => { e.preventDefault(); $('#dropHint').hidden = true; uploadFile(e.dataTransfer.files[0]); });

$$('.tabs [data-tab]').forEach((b) => b.addEventListener('click', () => {
  activeTab = b.dataset.tab;
  $$('.tabs [data-tab]').forEach((o) => o.classList.toggle('on', o === b));
  for (const t of ['trend', 'point', 'm2']) $('#tab-' + t).hidden = t !== activeTab;
  $('#histCtl').hidden = $('#btnHistClear').hidden = activeTab === 'm2';
  if (activeTab === 'm2') renderM2();
  drawBottom();
}));
$('#btnHistClear').addEventListener('click', () => { history = []; drawBottom(); });

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') { e.preventDefault(); if (!e.repeat) quickSave(); return; }
  if (!$('#modal').hidden) return;
  if (e.target.matches('input, select, textarea')) return;
  if (e.code === 'Space') { if (!e.repeat && e.target.tagName !== 'BUTTON') { spaceDown = true; } return; }
  const key = e.key.toLowerCase();
  if (key === 'f') fitView();
  else if (key === 'm' && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey) toggleMeasure();
  else if (key === 'r' || key === 'p' || key === 'c') { ui.tool = { r: 'roi', p: 'pan', c: 'cursor' }[key]; saveUi(); applyUi(); dirtyImg = true; }
});
document.addEventListener('keyup', (e) => {
  if (e.code !== 'Space') return;
  const wasPan = spaceDown && drag;
  spaceDown = false;
  if (e.target.matches('input, select, textarea, button')) return;
  if (!wasPan) postSettings({ paused: !settings.paused });
});

// --------------------------------------------------------------- stream
function onPacket(buf) {
  const n = new DataView(buf).getUint32(0, true);
  const h = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  hdr = h;
  if (h.cam) {
    if (h.cam.width !== sensor.w || h.cam.height !== sensor.h) { sensor = { w: h.cam.width, h: h.cam.height }; view.auto = true; fitView(); }
    if (h.img.w > 0) { lastGray = new Uint8Array(buf, 4 + n, h.img.w * h.img.h); paintFrame(lastGray, h.img.w, h.img.h); }
  } else { sensor = { w: 0, h: 0 }; lastGray = null; dirtyImg = true; }
  if (h.rev !== rev) refreshState();
  pushHistory();
  dirtyPanels = true;
}
let dirtyPanels = true;
let lastBottom = 0;
function redrawAll() { dirtyImg = true; dirtyPanels = true; lastBottom = 0; }
function frame(now) {
  if (dirtyImg) { dirtyImg = false; renderImage(); }
  if (dirtyPanels) {
    dirtyPanels = false;
    updateStatus();
    updateResults();
    const p = hdr && hdr.prof;
    drawProfile($('#plotX'), p && p.x, COL.x, $('#profXinfo'));
    drawProfile($('#plotY'), p && p.y, COL.y, $('#profYinfo'));
    if (now - lastBottom > 200) { lastBottom = now; drawBottom(); }
  }
  requestAnimationFrame(frame);
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { connected = true; refreshState(); dirtyPanels = true; };
  ws.onmessage = (e) => { if (e.data instanceof ArrayBuffer) onPacket(e.data); };
  ws.onclose = () => { connected = false; dirtyPanels = true; setTimeout(connect, 1000); };
  ws.onerror = () => ws.close();
}

new ResizeObserver(() => { if (view.auto) fitView(); redrawAll(); }).observe(wrap);
new ResizeObserver(redrawAll).observe($('.bottompanel'));
window.addEventListener('resize', redrawAll);

applyUi();
renderM2();
loadQs();
refreshState().then(connect);
requestAnimationFrame(frame);
