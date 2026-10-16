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
  try { localStorage.setItem("roxy-tab", name); } catch (_) { /* storage blocked */ }
}
tabs.forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
try { const saved = localStorage.getItem("roxy-tab"); if (saved) showTab(saved); } catch (_) { /* storage blocked */ }

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
  $("heard-meta").innerHTML = (e.wake ? '<span class="chip wake">Hey Roxy</span>' : '<span class="chip">background</span>')
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
}

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
