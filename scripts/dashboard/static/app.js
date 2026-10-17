// Roxy dashboard: renders the events from dashboard.py (Server-Sent Events on /events).
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const GLYPH = { forward: "↑", turn_left: "↶", turn_right: "↷", stop: "■" };
const MAX_LOG = 600, MAX_STEPS = 40, MAX_HISTORY = 12;

function actionLabel(e) {
  if (e.kind === "forward") return `Forward ${Math.round(e.value * 100)} cm`;
  if (e.kind === "turn_left") return `Turn left ${Math.round(e.value)}°`;
  if (e.kind === "turn_right") return `Turn right ${Math.round(e.value)}°`;
  if (e.kind === "stop") return "Stop";
  return e.action || "?";
}

// ---------------- tabs ----------------
const tabs = document.querySelectorAll(".tab");
function showTab(name) {
  tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  // the VLA tab's second camera stream only runs while that tab is open
  document.querySelectorAll("#view-vla img[data-src]").forEach((img) => {
    img.src = name === "vla" ? img.dataset.src : "";
  });
  if (name === "voice") queueMicrotask(loadVoices);  // after the whole script has run
  try { localStorage.setItem("roxy-tab", name); } catch (_) { /* storage blocked */ }
}
tabs.forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
// #voice etc. in the URL opens that tab (a bookmark for the operator), else the last one used
const hashTab = location.hash.slice(1);
if ($(`view-${hashTab}`)) showTab(hashTab);
else try { const saved = localStorage.getItem("roxy-tab"); if (saved) showTab(saved); } catch (_) { /* storage blocked */ }

// logos: a missing file becomes a text badge (drop PNGs into static/logos/)
document.querySelectorAll(".logos img").forEach((img) => {
  const fallback = () => {
    const span = document.createElement("span");
    span.className = "logo-fallback";
    span.textContent = img.dataset.fallback;
    img.replaceWith(span);
  };
  if (img.complete && img.naturalWidth === 0) fallback(); // already failed before this script ran
  else img.addEventListener("error", fallback);
});

// ---------------- live: heard / route / decision ----------------
function onHeard(e) {
  if (!e.text && e.wake) {
    $("heard-text").innerHTML = '<span class="muted">Hey Roxy… listening</span>';
    $("heard-meta").innerHTML = '<span class="chip wake">Hey Roxy</span>';
    return;
  }
  if (!e.text) return;
  // a sentence addressed to Roxy is bold white; background speech stays grey
  $("heard-text").innerHTML = e.wake ? `<q>${esc(e.text)}</q>` : `<span class="muted">${esc(e.text)}</span>`;
  const chip = e.typed ? '<span class="chip typed">typed</span>'
    : e.wake ? '<span class="chip wake">Hey Roxy</span>' : '<span class="chip">background</span>';
  $("heard-meta").innerHTML = chip
    + `<span>${clock(e.t)}</span>`;
}

function onRoute(e, replay) {
  $("lane-gemini").classList.toggle("on", e.target === "gemini");
  $("lane-navila").classList.toggle("on", e.target === "navila");
  const reply = $("reply");
  reply.hidden = !e.reply;
  reply.textContent = e.reply || "";
  if (!replay) {
    $("heard-text").innerHTML = `<q>${esc(e.heard)}</q>`;
  }
  const li = document.createElement("li");
  const chip = { navila: "navila\">→ NaVILA", gemini: "gemini\">→ Gemini", offline: "offline\">offline" }[e.target];
  li.innerHTML = `<span class="time">${clock(e.t)}</span>
    <span class="said">${esc(e.heard)}${e.reply ? `<small>Roxy: ${esc(e.reply)}</small>` : ""}</span>
    <span class="chip ${chip}</span>`;
  const list = $("history-list");
  list.prepend(li);
  while (list.children.length > MAX_HISTORY) list.lastChild.remove();
}

let overlayTimer = null;
function showDecision(e) {
  const label = actionLabel(e), glyph = GLYPH[e.kind] || "•";
  const card = document.querySelector(".card.decision");
  card.classList.remove("thinking");
  $("decision-glyph").textContent = glyph;
  $("decision-text").textContent = label;
  $("decision-meta").textContent = `step ${e.n} · ${e.roundtrip_s ?? e.latency_s} s`;
  const ov = $("overlay-action");
  ov.classList.toggle("stop", e.kind === "stop");
  $("overlay-glyph").textContent = glyph;
  $("overlay-text").textContent = label;
  // the camera overlay stays up 15 s after a decision (also for a page opened mid-episode)
  const left = 15000 - (Date.now() - e.t * 1000);
  clearTimeout(overlayTimer);
  ov.hidden = left <= 0;
  if (left > 0) overlayTimer = setTimeout(() => { ov.hidden = true; }, left);
}

function setThinking(text) {
  const card = document.querySelector(".card.decision");
  card.classList.add("thinking");
  $("decision-glyph").textContent = "◌";
  $("decision-text").textContent = text;
}

// ---------------- VLA tab ----------------
const stepCards = new Map(); // decision number -> element, for its outcome
function timelineAdd(el) {
  const tl = $("timeline");
  tl.querySelector(".empty")?.remove();
  tl.prepend(el);
  while (tl.children.length > MAX_STEPS) tl.lastChild.remove();
}

function onStep(e, replay) {
  switch (e.type) {
    case "instruction": {
      $("instruction").innerHTML = `Instruction: <b>${esc(e.text)}</b>`;
      setThinking("Starting");
      stepCards.clear();
      const div = document.createElement("div");
      div.className = "episode";
      div.innerHTML = `${clock(e.t)} · new instruction <b>“${esc(e.text)}”</b>${e.motion ? "" : " · dry run"}`;
      timelineAdd(div);
      break;
    }
    case "query":
      setThinking("Thinking");
      $("thinking").hidden = false;
      break;
    case "decision": {
      $("thinking").hidden = true;
      showDecision(e);
      const frames = e.frames || [];
      if (frames.length) {
        const img = $("sent-frame");
        img.src = `data:image/jpeg;base64,${frames[frames.length - 1]}`;
        img.hidden = false;
        $("sent-empty").hidden = true;
        const sa = $("sent-action");
        sa.innerHTML = `<span class="glyph">${GLYPH[e.kind] || "•"}</span>${esc(actionLabel(e))}`;
        sa.classList.toggle("stop", e.kind === "stop");
        sa.hidden = false;
        $("sent-meta").textContent = `step ${e.n} · ${clock(e.t)}`;
      }
      const card = document.createElement("div");
      card.className = "step";
      const strip = frames.map((f, i) => {
        const pad = f.length < 1200 ? " pad" : ""; // a black padding frame compresses to almost nothing
        return `<figure class="${pad}"><img src="data:image/jpeg;base64,${f}" alt="frame ${i + 1}">
          <figcaption>${i === frames.length - 1 ? "now" : `t-${frames.length - 1 - i}`}</figcaption></figure>`;
      }).join("");
      card.innerHTML = `<div class="strip-wrap"><div class="strip">${strip}</div></div>
        <div class="step-info">
          <div class="step-head"><span class="step-n">#${String(e.n).padStart(2, "0")}</span>
            <span class="step-action"><span class="glyph">${GLYPH[e.kind] || "•"}</span>${esc(actionLabel(e))}</span></div>
          <div class="step-text">${esc(e.text)}</div>
          <div class="step-meta"><span class="chip navila">model ${e.latency_s} s</span>
            <span class="chip">round trip ${e.roundtrip_s} s</span><span class="chip">${clock(e.t)}</span></div>
          <div class="outcome" data-outcome>${e.kind === "stop" ? "episode ends" : (e.motion ? "executing…" : "dry run: not executed")}</div>
        </div>`;
      stepCards.set(e.n, card);
      timelineAdd(card);
      break;
    }
    case "outcome": {
      const card = stepCards.get(e.n);
      const el = card?.querySelector("[data-outcome]");
      if (el) {
        el.className = `outcome ${e.timed_out ? "bad" : "ok"}`;
        el.textContent = `did ${e.measured} of ${e.asked} ${e.unit} in ${e.seconds} s${e.timed_out ? " · TIMED OUT" : ""}`;
      }
      break;
    }
    case "finished":
    case "stopped": {
      $("thinking").hidden = true;
      document.querySelector(".card.decision").classList.remove("thinking");
      $("decision-glyph").textContent = "■";
      $("decision-text").textContent = e.type === "finished" ? "Arrived" : "Stopped";
      $("overlay-action").hidden = true;
      const div = document.createElement("div");
      div.className = "episode";
      div.innerHTML = `${clock(e.t)} · <b>${e.type === "finished" ? "episode finished" : "stopped"}</b>`;
      timelineAdd(div);
      break;
    }
    case "error":
      $("thinking").hidden = true;
      setThinking("NaVILA error");
      break;
  }
}

// ---------------- logs ----------------
let levelFilter = "all", search = "";
const RANK = { debug: 0, info: 1, warn: 2, error: 3, fatal: 4 };
const ICON = { debug: "·", info: "i", warn: "!", error: "✕", fatal: "✕" };
const counts = { laptop: 0, pi: 0, server: 0 };

function visible(li) {
  const lv = li.dataset.level;
  if (levelFilter === "warn" && RANK[lv] < 2) return false;
  if (levelFilter === "error" && RANK[lv] < 3) return false;
  return !search || li.dataset.search.includes(search);
}

function onLog(e) {
  const list = $(`log-${e.machine}`);
  if (!list) return;
  const last = list.lastElementChild;
  // the same line again: count it instead of repeating it
  if (last && last.dataset.key === `${e.node}|${e.text}`) {
    const rep = last.querySelector(".rep") || last.querySelector(".msg").appendChild(document.createElement("span"));
    rep.className = "rep";
    last.dataset.n = String(Number(last.dataset.n || 1) + 1);
    rep.textContent = `×${last.dataset.n}`;
    last.querySelector(".ts").textContent = clock(e.t);
    return;
  }
  const li = document.createElement("li");
  li.className = e.level;
  li.dataset.level = e.level;
  li.dataset.key = `${e.node}|${e.text}`;
  li.dataset.search = `${e.node} ${e.text}`.toLowerCase();
  li.innerHTML = `<span class="ts">${clock(e.t)}</span><span class="lv ${e.level}">${ICON[e.level] || "i"}</span>
    <span class="msg"><span class="node">${esc(e.node)}</span>${esc(e.text)}</span>`;
  li.hidden = !visible(li);
  const follow = $("log-follow").checked && list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.appendChild(li);
  while (list.children.length > MAX_LOG) list.firstChild.remove();
  counts[e.machine] += 1;
  $(`count-${e.machine}`).textContent = counts[e.machine];
  if (follow || $("log-follow").checked && !list.dataset.touched) list.scrollTop = list.scrollHeight;
}

function refilter() {
  document.querySelectorAll(".log li").forEach((li) => { li.hidden = !visible(li); });
}
document.querySelectorAll("#level-filter button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#level-filter button").forEach((x) => x.classList.toggle("on", x === b));
  levelFilter = b.dataset.level;
  refilter();
}));
$("log-search").addEventListener("input", (ev) => { search = ev.target.value.trim().toLowerCase(); refilter(); });

// ---------------- status ----------------
function pill(id, state, text) {
  const p = $(id);
  p.classList.remove("ok", "warn", "err");
  if (state) p.classList.add(state);
  if (text !== undefined) p.querySelector("span").textContent = text;
}

function onStatus(e) {
  pill("st-robot", e.odom_hz > 5 ? "ok" : "err", e.odom_hz > 0 ? `${e.odom_hz} Hz odometry` : "no odometry");
  pill("st-camera", e.camera_fps > 8 ? "ok" : e.camera_fps > 0 ? "warn" : "err",
    e.camera_fps > 0 ? `${e.camera_fps} fps` : "no frames");
  $("live-fps").textContent = `${e.camera_fps} fps`;
  const s = e.server || {};
  pill("st-server", s.ok ? "ok" : "err", s.ok ? `${s.model} · ${s.ms} ms` : "unreachable");
  $("st-server").title = s.ok ? `${e.server_url} · ${s.precision} · ${s.vram_gib} GiB VRAM` : `${e.server_url}: ${s.error || ""}`;
  const v = $("st-voice");
  v.classList.toggle("speaking", !!e.speaking);
  pill("st-voice", e.speaking ? null : "ok", e.speaking ? "Roxy speaking" : "listening");
  const mode = $("st-mode");
  mode.classList.toggle("live", !!e.motion);
  mode.querySelector("b").textContent = e.motion ? "MOTION ON" : "DRY RUN";
  lastStatus = e;
  if (!typeBusy) $("type-status").textContent = e.typing ? "" : "bringup not running";
  $("voice-current").textContent = e.voice ? e.voice.label : "from roxy.yaml (tts_node voice_preset)";
  $("play-roxy").disabled = !e.tts;
  $("type-say").disabled = !e.tts;
}
let lastStatus = {};

// ---------------- typing to Roxy (a hall too noisy for the mic) ----------------
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return r;
}

let typeBusy = false;
const typed = [];  // sent lines, newest last: ↑ / ↓ in the box
let typedAt = 0;
function typeNote(text, bad) {
  typeBusy = true;
  const el = $("type-status");
  el.textContent = text;
  el.classList.toggle("bad", !!bad);
  clearTimeout(typeNote.t);
  typeNote.t = setTimeout(() => { typeBusy = false; el.textContent = ""; el.classList.remove("bad"); }, 4000);
}
async function typeSend(path, okText) {
  const box = $("type-text"), text = box.value.trim();
  if (!text) return box.focus();
  try {
    await post(path, { text });
    if (typed[typed.length - 1] !== text) typed.push(text);
    typedAt = typed.length;
    box.value = "";
    typeNote(okText);
  } catch (err) { typeNote(err.message, true); }
  box.focus();
}
$("type-form").addEventListener("submit", (ev) => { ev.preventDefault(); typeSend("api/type", "sent"); });
$("type-say").addEventListener("click", () => typeSend("api/speak", "speaking"));
$("type-stop").addEventListener("click", async () => {
  try { await post("api/stop"); typeNote("stop sent"); } catch (err) { typeNote(err.message, true); }
});
$("type-text").addEventListener("keydown", (ev) => {
  if (ev.key !== "ArrowUp" && ev.key !== "ArrowDown") return;
  ev.preventDefault();
  typedAt = Math.max(0, Math.min(typed.length, typedAt + (ev.key === "ArrowUp" ? -1 : 1)));
  ev.target.value = typed[typedAt] ?? "";
});

// ---------------- voice lab ----------------
// ~10 words each, and never "Roxy": laptop speakers playing it could wake her through the mic
const SENTENCES = [
  "Hello everyone, welcome to our stand, I am a robot dog.",
  "Point anywhere you like and tell me where to go.",
  "Beep boop, my sensors say you look amazing today, friend.",
  "Ask me a question, or tell me to sit down.",
  "Watch me walk over there and sit down for you.",
];
// [key, label, min, max, step, shown as]
const KNOBS = [
  ["pitch", "Pitch", -600, 1400, 25, (v) => `${v > 0 ? "+" : ""}${(v / 100).toFixed(1)} semitones`],
  ["rate", "Speed", 0.6, 1.6, 0.01, (v) => `${v.toFixed(2)}×`],  // spec.speed = 1 / rate (Piper length)
  ["ring_hz", "Ring mod", 0, 300, 1, (v) => v ? `${v} Hz` : "off"],
  ["ring_mix", "Ring mix", 0, 1, 0.05, (v) => `${Math.round(v * 100)}%`],
  ["bits", "Bit-crush", 0, 12, 1, (v) => v ? `${v} bits` : "off"],
  ["overdrive", "Overdrive", 0, 20, 1, (v) => v ? `${v} dB` : "off"],
  ["bass", "Bass", -15, 15, 1, (v) => `${v > 0 ? "+" : ""}${v} dB`],
  ["echo_ms", "Echo", 0, 120, 1, (v) => v ? `${v} ms` : "off"],
  ["tremolo_hz", "Wobble", 0, 15, 0.5, (v) => v ? `${v} Hz` : "off"],
  ["tremolo_depth", "Wobble depth", 0, 100, 5, (v) => `${v}%`],
];
const TOGGLES = [["punch", "Cut through noise"], ["flanger", "Flanger"], ["chorus", "Chorus"]];

const lab = { voices: [], byName: {}, bases: [], defaults: {}, name: null, spec: null, tuned: false,
  style: "All", search: "", audio: null, cache: new Map(), loaded: false };
let favs = new Set();
try { favs = new Set(JSON.parse(localStorage.getItem("roxy-fav") || "[]")); } catch (_) { /* storage blocked */ }
function saveFavs() { try { localStorage.setItem("roxy-fav", JSON.stringify([...favs])); } catch (_) { /* storage blocked */ } }

function voiceMsg(text, bad) {
  const el = $("voice-msg");
  el.textContent = text;
  el.classList.toggle("bad", !!bad);
}

$("test-text").value = SENTENCES[0];
$("test-another").addEventListener("click", () => {
  const i = SENTENCES.indexOf($("test-text").value);
  $("test-text").value = SENTENCES[(i + 1) % SENTENCES.length];
});

async function loadVoices() {
  if (lab.loaded) return;
  try {
    const c = await (await fetch("api/voices")).json();
    if (!c.ok) throw new Error(c.error);
    lab.loaded = true;
    Object.assign(lab, { voices: c.voices, bases: c.bases, defaults: c.defaults });
    lab.voices.forEach((v) => { lab.byName[v.name] = v; });
    $("voice-count").textContent = lab.voices.filter((v) => v.n).length;
    buildKnobs();
    const styles = ["All", "★", ...new Set(lab.voices.map((v) => v.style))];
    $("voice-style").innerHTML = styles.map((st) => `<button data-style="${esc(st)}" class="${st === "All" ? "on" : ""}">${esc(st)}</button>`).join("");
    $("voice-style").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      lab.style = b.dataset.style;
      $("voice-style").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      renderList();
    }));
    renderList();
    const cur = c.current;
    const match = cur && lab.voices.find((v) => v.label === cur.label);
    if (match) pick(match.name, false);
    else if (cur) setSpec(cur.spec, null, cur.label);
    else pick(lab.voices[0].name, false);
  } catch (err) {
    $("voice-list").innerHTML = `<li class="empty">Voice lab unavailable: ${esc(err.message)}</li>`;
  }
}

function shown() {
  const q = lab.search;
  return lab.voices.filter((v) => (lab.style === "All" || (lab.style === "★" ? favs.has(v.name) : v.style === lab.style))
    && (!q || `${v.n} ${v.label} ${v.style} ${v.spec.model}`.toLowerCase().includes(q)));
}

function renderList() {
  const list = $("voice-list"), items = shown();
  if (!items.length) {
    list.innerHTML = `<li class="empty">${lab.style === "★" ? "Star voices you like to list them here" : "No voice matches"}</li>`;
    return;
  }
  list.innerHTML = items.map((v) => `<li data-name="${esc(v.name)}" class="${v.name === lab.name ? "on" : ""}${v.installed ? "" : " missing"}">
      <span class="vn">${v.n ? String(v.n).padStart(3, "0") : "—"}</span>
      <span class="vl">${esc(v.label)}${v.installed ? "" : '<small>model not in this image: make build-ros</small>'}</span>
      <span class="chip">${esc(v.style)}</span>
      <button class="star${favs.has(v.name) ? " on" : ""}" data-star title="Favourite">★</button>
      <button class="vplay" data-play title="Play">▶</button></li>`).join("");
  list.querySelectorAll("li[data-name]").forEach((li) => li.addEventListener("click", (ev) => {
    const name = li.dataset.name;
    if (ev.target.closest("[data-star]")) {
      favs.has(name) ? favs.delete(name) : favs.add(name);
      saveFavs();
      return renderList();
    }
    pick(name, ev.target.closest("[data-play]") || $("autoplay").checked);
  }));
}
$("voice-search").addEventListener("input", (ev) => { lab.search = ev.target.value.trim().toLowerCase(); renderList(); });

function pick(name, play) {
  const v = lab.byName[name];
  if (!v) return;
  setSpec(v.spec, name);
  lab.name = name;
  document.querySelectorAll("#voice-list li").forEach((li) => li.classList.toggle("on", li.dataset.name === name));
  document.querySelector(`#voice-list li[data-name="${CSS.escape(name)}"]`)?.scrollIntoView({ block: "nearest" });
  if (play) playHere();
}

function setSpec(spec, name, label) {
  lab.spec = { ...lab.defaults, ...spec };
  lab.name = name;
  lab.tuned = false;
  lab.label = label || null;
  syncKnobs();
  showPicked();
}

function currentLabel() {
  const v = lab.byName[lab.name];
  const base = v ? v.label : (lab.label || "Custom voice");
  return lab.tuned ? `${base} (tuned)` : base;
}
function showPicked() {
  const v = lab.byName[lab.name];
  $("picked").innerHTML = `<b>${esc(currentLabel())}</b>${v && v.n ? ` <span class="muted">#${v.n} of 100</span>` : ""}`;
}

function buildKnobs() {
  $("knob-base").innerHTML = lab.bases.map((b) => `<option value="${esc(b.model)}#${b.speaker}"${b.installed ? "" : " disabled"}>
    ${esc(b.label)} · ${esc(b.model.replace(/-medium$/, ""))}${b.installed ? "" : " (not installed)"}</option>`).join("");
  $("knob-grid").innerHTML = KNOBS.map(([k, label, min, max, step]) => `<label class="knob">
      <span class="kl">${label}<b id="kv-${k}"></b></span>
      <input type="range" id="knob-${k}" min="${min}" max="${max}" step="${step}"></label>`).join("");
  $("knob-toggles").innerHTML = TOGGLES.map(([k, label]) =>
    `<label class="toggle"><input type="checkbox" id="knob-${k}"> ${label}</label>`).join("");
  KNOBS.forEach(([k]) => $(`knob-${k}`).addEventListener("input", (ev) => tune(k, Number(ev.target.value))));
  TOGGLES.forEach(([k]) => $(`knob-${k}`).addEventListener("change", (ev) => tune(k, ev.target.checked)));
  $("knob-base").addEventListener("change", (ev) => {
    const [model, speaker] = ev.target.value.split("#");
    tune("model", model);
    tune("speaker", Number(speaker));
  });
  $("knob-persona").addEventListener("change", (ev) => tune("persona", ev.target.value));
}

function syncKnobs() {
  const s = lab.spec;
  KNOBS.forEach(([k, , , , , fmt]) => {
    const v = k === "rate" ? 1 / s.speed : s[k];
    $(`knob-${k}`).value = v;
    $(`kv-${k}`).textContent = fmt(Number($(`knob-${k}`).value));
  });
  TOGGLES.forEach(([k]) => { $(`knob-${k}`).checked = !!s[k]; });
  const opt = `${s.model}#${s.speaker}`;
  if (![...$("knob-base").options].some((o) => o.value === opt)) {
    $("knob-base").insertAdjacentHTML("beforeend", `<option value="${esc(opt)}">${esc(s.model)} #${s.speaker}</option>`);
  }
  $("knob-base").value = opt;
  $("knob-persona").value = s.persona;
}

let tunePlay = null;
function tune(key, value) {
  if (key === "rate") {
    lab.spec.speed = Math.round(1000 / value) / 1000;
  } else lab.spec[key] = value;
  const knob = KNOBS.find(([k]) => k === key);
  if (knob) $(`kv-${key}`).textContent = knob[5](value);
  lab.tuned = true;
  showPicked();
  // hear a change half a second after the last slider move
  clearTimeout(tunePlay);
  if ($("autoplay").checked) tunePlay = setTimeout(playHere, 500);
}
$("voice-reset").addEventListener("click", () => {
  if (lab.name) pick(lab.name, $("autoplay").checked);
});

function testText() {
  return $("test-text").value.trim() || SENTENCES[0];
}

async function playHere() {
  if (!lab.spec) return;
  const text = testText(), key = JSON.stringify([text, lab.spec]);
  lab.audio?.pause();
  try {
    let url = lab.cache.get(key);
    if (!url) {
      voiceMsg("Rendering…");
      const r = await post("api/voice/preview", { text, spec: lab.spec });
      url = URL.createObjectURL(await r.blob());
      lab.cache.set(key, url);
      if (lab.cache.size > 80) {  // oldest first out
        const [k0, u0] = lab.cache.entries().next().value;
        URL.revokeObjectURL(u0);
        lab.cache.delete(k0);
      }
    }
    if (JSON.stringify([testText(), lab.spec]) !== key) return;  // the pick changed while rendering
    lab.audio = new Audio(url);
    await lab.audio.play();
    voiceMsg(`Playing ${currentLabel()} here`);
  } catch (err) { voiceMsg(err.message, true); }
}
$("play-here").addEventListener("click", playHere);

$("play-roxy").addEventListener("click", async () => {
  try {
    await post("api/speak", { text: testText(), spec: lab.spec });
    voiceMsg(`Playing ${currentLabel()} on Roxy`);
  } catch (err) { voiceMsg(err.message, true); }
});

$("voice-use").addEventListener("click", async () => {
  try {
    await post("api/voice/use", { label: currentLabel(), spec: lab.spec });
    $("voice-current").textContent = currentLabel();
    voiceMsg(lastStatus.tts ? `Roxy now speaks as ${currentLabel()}` : `Saved: Roxy uses ${currentLabel()} once VOICE=true is running`);
  } catch (err) { voiceMsg(err.message, true); }
});

function step(dir) {
  const items = shown();
  if (!items.length) return;
  const i = items.findIndex((v) => v.name === lab.name);
  const next = items[(i + dir + items.length) % items.length];
  pick(next.name, true);
}
$("voice-prev").addEventListener("click", () => step(-1));
$("voice-next").addEventListener("click", () => step(1));
document.addEventListener("keydown", (ev) => {
  if (!$("view-voice").classList.contains("active") || ev.target.closest("input, textarea, select")) return;
  if (ev.key === "ArrowRight") { ev.preventDefault(); step(1); }
  else if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-1); }
  else if (ev.key === " ") { ev.preventDefault(); playHere(); }
});

// ---------------- connection ----------------
function dispatch(e, replay = false) {
  if (e.ev === "heard") onHeard(e);
  else if (e.ev === "route") onRoute(e, replay);
  else if (e.ev === "step") onStep(e, replay);
  else if (e.ev === "log") onLog(e);
  else if (e.ev === "status") onStatus(e);
}

async function loadHistory() {
  try {
    const s = await (await fetch("state")).json();
    s.log.forEach((e) => dispatch(e, true));
    s.route.forEach((e) => dispatch(e, true));
    s.step.forEach((e) => dispatch(e, true));
    const lastHeard = s.heard[s.heard.length - 1];
    if (lastHeard) onHeard(lastHeard);
  } catch (_) { /* first load before the server is up: events will fill in */ }
}

function connect() {
  const es = new EventSource("events");
  es.onopen = () => pill("st-link", "ok", "live");
  es.onmessage = (m) => { try { dispatch(JSON.parse(m.data)); } catch (_) { /* malformed event */ } };
  es.onerror = () => pill("st-link", "err", "reconnecting");
}

loadHistory().then(connect);
