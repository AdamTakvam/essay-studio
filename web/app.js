/* Essay Studio front end — vanilla JS, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const STAGES = ["write", "media", "publish"];
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const S = {
  project: null, models: [], voices: [], platforms: [], stage: "write",
  savedEssay: "", aiCtrl: null, aiTimer: null, lastAiInput: null, picked: new Set(store.get("es.picked", ["summary"])),
};

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}
const fileUrl = (rel, dl) => `/api/projects/${S.project.id}/files/${rel}${dl ? "?download=1" : ""}`;

function toast(msg, ms = 2200) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}
function confirmBox(title, text, ok = "OK") {
  const d = $("#confirmDialog");
  $("#confirmTitle").textContent = title; $("#confirmText").textContent = text; $("#confirmOk").textContent = ok;
  d.showModal();
  return new Promise(r => d.addEventListener("close", () => r(d.returnValue === "ok"), { once: true }));
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const words = t => (t.trim().match(/\S+/g) || []).length;
const fmtDur = s => { s = Math.round(s || 0); const m = Math.floor(s / 60); return `${m}:${String(s % 60).padStart(2, "0")}`; };

// ------------------------------------------------------------------ tool output pane
const logEl = $("#log");
let logLines = 0;
function logAppend(e) {
  const t = new Date(e.t * 1000).toLocaleTimeString([], { hour12: false });
  const lvl = e.level === "error" ? "ERROR " : e.level === "warn" ? "WARN  " : "";
  const follow = $("#logFollow").checked;
  logEl.value += `${t}  ${("[" + e.source + "]").padEnd(12)} ${lvl}${e.msg}\n`;
  if (++logLines > 4000) { logEl.value = logEl.value.split("\n").slice(-3000).join("\n"); logLines = 3000; }
  if (follow) logEl.scrollTop = logEl.scrollHeight;
}
function localLog(msg, level = "info") { logAppend({ t: Date.now() / 1000, source: "ui", msg, level }); }
$("#logClear").onclick = () => { logEl.value = ""; logLines = 0; };

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("#conn").textContent = "live"; $("#conn").classList.add("live"); };
  es.onerror = () => { $("#conn").textContent = "reconnecting…"; $("#conn").classList.remove("live"); };
  es.onmessage = m => {
    const e = JSON.parse(m.data);
    if (e.kind === "log") logAppend(e);
    else if (e.kind === "job" && S.project && e.project === S.project.id) {
      if (e.state) S.project.media[e.media] = e.state; else delete S.project.media[e.media];
      renderCard(e.media); renderMediaSummary();
    } else if (e.kind === "publish" && S.project && e.project === S.project.id) {
      const runs = S.project.publish.runs; const i = runs.findIndex(r => r.id === e.run.id);
      if (i >= 0) runs[i] = e.run; else runs.push(e.run);
      renderPublishResults();
    }
  };
}

// ------------------------------------------------------------------ resizable split (85 / 15)
(function splitter() {
  const app = $("#app"), sp = $("#splitter");
  const apply = pct => { pct = Math.min(95, Math.max(25, pct)); app.style.setProperty("--top", pct + "%"); return pct; };
  apply(store.get("es.split", 85));
  sp.addEventListener("pointerdown", e => {
    sp.setPointerCapture(e.pointerId); sp.classList.add("drag");
    const move = ev => store.set("es.split", apply((ev.clientY / app.clientHeight) * 100));
    const up = () => { sp.classList.remove("drag"); sp.removeEventListener("pointermove", move); };
    sp.addEventListener("pointermove", move); sp.addEventListener("pointerup", up, { once: true });
  });
  sp.addEventListener("keydown", e => {
    const cur = parseFloat(getComputedStyle(app).getPropertyValue("--top")) || 85;
    if (e.key === "ArrowUp") store.set("es.split", apply(cur - 2));
    if (e.key === "ArrowDown") store.set("es.split", apply(cur + 2));
  });
  sp.addEventListener("dblclick", () => store.set("es.split", apply(85)));
})();

// ------------------------------------------------------------------ stages
function showStage(stage, { persist = true } = {}) {
  S.stage = stage;
  const i = STAGES.indexOf(stage);
  $("#track").style.transform = `translateX(-${(i * 100) / 3}%)`;
  $$(".stage").forEach(s => { const away = s.dataset.stage !== stage; s.classList.toggle("away", away); s.inert = away; });
  $$(".step").forEach((b, j) => { b.classList.toggle("current", j === i); b.classList.toggle("done", j < i); });
  $$(".rail").forEach((r, j) => r.classList.toggle("on", j < i));
  if (stage === "media") renderMedia();
  if (stage === "publish") renderPublish();
  if (persist && S.project && S.project.stage !== stage) {
    S.project.stage = stage;
    api(`/api/projects/${S.project.id}`, { method: "PUT", body: { stage } }).catch(() => {});
  }
}
async function goTo(stage) {
  if (stage === S.stage) return;
  if (stage !== "write") {
    if (!$("#essay").value.trim()) { toast("Write something first"); return; }
    if (!(await save())) return;
  }
  if (stage === "publish") localLog(`Moving to Publish with ${approvedKinds().length} approved media item(s)`);
  showStage(stage);
}
$$(".step").forEach(b => (b.onclick = () => goTo(b.dataset.stage)));
$("#toMedia").onclick = () => goTo("media");
$("#toPublish").onclick = () => goTo("publish");
$("#backToWrite").onclick = () => goTo("write");
$("#backToMedia").onclick = () => goTo("media");
$("#startOver").onclick = () => goTo("write");

// ------------------------------------------------------------------ projects
async function loadProjects(selectId) {
  const list = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = list.map(p => `<option value="${p.id}">${esc(p.title)}</option>`).join("");
  if (selectId) sel.value = selectId;
  return list;
}
async function openProject(id) {
  if (S.project && dirty() && !(await confirmBox("Discard unsaved changes?", "Your essay has changes that haven't been saved.", "Discard"))) {
    $("#projectSelect").value = S.project.id; return;
  }
  S.project = await api(`/api/projects/${id}`);
  store.set("es.last", id);
  $("#essay").value = S.project.essay; S.savedEssay = S.project.essay;
  $("#sysPrompt").value = S.project.system_prompt;
  if (S.project.model && S.models.some(m => m.id === S.project.model)) $("#model").value = S.project.model;
  $("#aiOut").value = ""; S.lastAiInput = null;
  updateCounts(); setSaved(S.project.updated);
  $("#projectSelect").value = id;
  showStage(S.project.stage || "write", { persist: false });
  if (S.project.essay.trim()) scheduleAssist(50);
}
$("#projectSelect").onchange = e => openProject(e.target.value);
$("#newProject").onclick = async () => {
  if (dirty() && !(await confirmBox("Discard unsaved changes?", "Your essay has changes that haven't been saved.", "Discard"))) return;
  S.savedEssay = $("#essay").value; // suppress second prompt
  const p = await api("/api/projects", { method: "POST" });
  await loadProjects(p.id); await openProject(p.id);
  $("#essay").focus();
};
$("#deleteProject").onclick = async () => {
  if (!(await confirmBox("Delete this essay?", `“${S.project.title}” and all of its media and packages will be removed from disk.`, "Delete"))) return;
  await api(`/api/projects/${S.project.id}`, { method: "DELETE" });
  S.savedEssay = $("#essay").value;
  const list = await loadProjects();
  if (list.length) await openProject(list[0].id); else $("#newProject").click();
};

// ------------------------------------------------------------------ write stage
const dirty = () => S.project && ($("#essay").value !== S.savedEssay ||
  $("#sysPrompt").value !== S.project.system_prompt || $("#model").value !== S.project.model);
function updateCounts() {
  $("#essayCount").textContent = `${words($("#essay").value).toLocaleString()} words`;
  $("#saveBtn").textContent = dirty() ? "Save •" : "Save";
}
function setSaved(ts) {
  $("#saveStatus").textContent = ts ? `Saved ${new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Not saved yet";
}
async function save() {
  if (!S.project) return false;
  try {
    const p = await api(`/api/projects/${S.project.id}`, { method: "PUT", body: {
      essay: $("#essay").value, system_prompt: $("#sysPrompt").value, model: $("#model").value } });
    const media = S.project.media; Object.assign(S.project, p); S.project.media = { ...p.media, ...media };
    S.savedEssay = p.essay; setSaved(p.updated); updateCounts();
    const opt = $(`#projectSelect option[value="${p.id}"]`); if (opt) opt.textContent = p.title;
    return true;
  } catch (e) { toast("Save failed: " + e.message, 4000); return false; }
}
$("#saveBtn").onclick = async () => { if (await save()) toast("Saved"); };
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save().then(ok => ok && toast("Saved")); }
});
window.addEventListener("beforeunload", e => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

$("#essay").addEventListener("input", () => { updateCounts(); scheduleAssist(); });
$("#sysPrompt").addEventListener("input", () => { updateCounts(); scheduleAssist(1800); });
$("#model").addEventListener("change", () => { updateCounts(); S.lastAiInput = null; scheduleAssist(200); });
$("#aiLive").addEventListener("change", e => { store.set("es.live", e.target.checked); if (e.target.checked) scheduleAssist(200); });
$("#aiRefresh").onclick = () => { S.lastAiInput = null; runAssist(); };

function scheduleAssist(ms = 1400) {
  clearTimeout(S.aiTimer);
  if (!$("#aiLive").checked) return;
  S.aiTimer = setTimeout(runAssist, ms);
}
async function runAssist() {
  const text = $("#essay").value;
  const key = JSON.stringify([text, $("#sysPrompt").value, $("#model").value]);
  if (!text.trim() || key === S.lastAiInput) return;
  S.lastAiInput = key;
  if (S.aiCtrl) S.aiCtrl.abort();
  const ctrl = (S.aiCtrl = new AbortController());
  const out = $("#aiOut");
  $("#aiPulse").hidden = false;
  let acc = "", first = true;
  try {
    const res = await fetch("/api/assist", {
      method: "POST", signal: ctrl.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, system_prompt: $("#sysPrompt").value, model: $("#model").value }),
    });
    const reader = res.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      if (first) { first = false; }
      const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
      out.value = acc;
      if (atEnd) out.scrollTop = out.scrollHeight;
    }
  } catch (e) {
    if (e.name !== "AbortError") localLog("Writing assistant: " + e.message, "error");
  } finally {
    if (S.aiCtrl === ctrl) { $("#aiPulse").hidden = true; S.aiCtrl = null; }
  }
}

$("#copyBtn").onclick = async () => {
  const ai = $("#aiOut").value;
  if (!ai.trim()) { toast("The AI draft is empty"); return; }
  if (!store.get("es.copyNoAsk", false)) {
    const d = $("#copyDialog"); $("#copyDontAsk").checked = false; d.showModal();
    const ok = await new Promise(r => d.addEventListener("close", () => r(d.returnValue === "ok"), { once: true }));
    if (!ok) return;
    if ($("#copyDontAsk").checked) store.set("es.copyNoAsk", true);
  }
  const ed = $("#essay");
  ed.focus(); ed.select();
  // insertText keeps the browser's undo stack, so Ctrl+Z restores the old essay
  if (!document.execCommand("insertText", false, ai)) ed.value = ai;
  ed.setSelectionRange(0, 0); ed.scrollTop = 0;
  S.lastAiInput = JSON.stringify([ai, $("#sysPrompt").value, $("#model").value]);
  updateCounts(); localLog("Essay replaced with the AI draft (not saved yet)");
};

// ------------------------------------------------------------------ media stage
const ICONS = {
  summary: '<svg viewBox="0 0 24 24" width="20" height="20"><path d="M5 6h14M5 10h14M5 14h9M5 18h6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>',
  audiobook: '<svg viewBox="0 0 24 24" width="20" height="20"><path d="M4 14v-2a8 8 0 0 1 16 0v2M4 14h3v6H5a1 1 0 0 1-1-1zm16 0h-3v6h2a1 1 0 0 0 1-1z" stroke="currentColor" stroke-width="2" fill="none" stroke-linejoin="round"/></svg>',
  podcast: '<svg viewBox="0 0 24 24" width="20" height="20"><rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" stroke-width="2" fill="none"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>',
  video: '<svg viewBox="0 0 24 24" width="20" height="20"><rect x="3" y="5" width="14" height="14" rx="2" stroke="currentColor" stroke-width="2" fill="none"/><path d="m17 10 4-2.5v9L17 14" stroke="currentColor" stroke-width="2" fill="none" stroke-linejoin="round"/></svg>',
  text: '<svg viewBox="0 0 24 24" width="20" height="20"><path d="M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h7" stroke="currentColor" stroke-width="2" fill="none" stroke-linejoin="round"/></svg>',
};
const KINDS = {
  summary: { name: "Summary / Abstract", type: "Text", desc: "A faithful condensation of the essay." },
  audiobook: { name: "Audiobook", type: "Audio", desc: "Your essay read aloud, word for word." },
  podcast: { name: "Podcast", type: "Audio", desc: "Two hosts discuss, challenge and react to the essay." },
  video: { name: "Video", type: "Video", desc: "Explainer slideshow, or a narrated story if the essay tells one." },
};
const DEFAULTS = {
  summary: { style: "abstract", words: 180 },
  audiobook: { voice: "en-US-AndrewNeural", rate: 0 },
  podcast: { voice1: "en-US-AndrewNeural", rate1: 5, voice2: "en-US-EmmaNeural", rate2: 15, length: "standard" },
  video: { voice: "en-US-AvaNeural", rate: 5 },
};
function optionsFor(kind) {
  return { ...DEFAULTS[kind], ...store.get(`es.opts.${kind}`, {}), ...(S.project?.media[kind]?.options || {}) };
}
function voiceSelect(name, value) {
  const groups = {};
  for (const v of S.voices) (groups[v.locale] ||= []).push(v);
  return `<select name="${name}">${Object.entries(groups).map(([loc, vs]) =>
    `<optgroup label="${loc}">${vs.map(v => `<option value="${v.id}" ${v.id === value ? "selected" : ""}>${esc(v.name)} · ${v.gender}</option>`).join("")}</optgroup>`).join("")}</select>`;
}
const range = (name, val, min = -50, max = 50) =>
  `<div class="range"><input type="range" name="${name}" min="${min}" max="${max}" step="5" value="${val}"><output>${val >= 0 ? "+" : ""}${val}%</output></div>`;
function optsHtml(kind, o) {
  switch (kind) {
    case "summary": return `
      <label>Style</label><select name="style">
        ${[["abstract", "Abstract (one paragraph)"], ["key-points", "Key points (bullets)"], ["tldr", "TL;DR (1–2 sentences)"]]
          .map(([v, l]) => `<option value="${v}" ${o.style === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label>Max words</label><input type="number" name="words" min="20" max="600" step="10" value="${o.words}">`;
    case "audiobook": return `
      <label>Voice</label>${voiceSelect("voice", o.voice)}
      <label>Speed</label>${range("rate", o.rate)}`;
    case "podcast": return `
      <label>Host 1</label>${voiceSelect("voice1", o.voice1)}
      <label>Speed</label>${range("rate1", o.rate1)}
      <label>Host 2</label>${voiceSelect("voice2", o.voice2)}
      <label>Speed</label>${range("rate2", o.rate2)}
      <label>Length</label><select name="length">
        ${[["short", "Short (~6 min)"], ["standard", "Standard (~12 min)"], ["long", "Long (~20 min)"]]
          .map(([v, l]) => `<option value="${v}" ${o.length === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    case "video": return `
      <label>Narrator</label>${voiceSelect("voice", o.voice)}
      <label>Speed</label>${range("rate", o.rate)}
      <span class="note">Claude first decides whether the essay explains or tells a story, then makes a slideshow explainer or a narrated scene-by-scene story.</span>`;
  }
}
function readOpts(card) {
  const o = {};
  $$("select, input[name]", $(".opts", card)).forEach(el => { o[el.name] = el.type === "range" || el.type === "number" ? Number(el.value) : el.value; });
  return o;
}

function renderMedia() {
  const grid = $("#mediaGrid");
  if (!grid.children.length) {
    grid.innerHTML = Object.entries(KINDS).map(([k, m]) => `
      <article class="card" data-kind="${k}">
        <header class="card-head">
          <label class="pick"><input type="checkbox" class="pickbox">
            <span class="card-icon">${ICONS[k]}</span>
            <span><h3>${m.name}</h3><p>${m.type} · ${m.desc}</p></span>
          </label>
          <span class="badge">Not generated</span>
        </header>
        <div class="opts"></div>
        <div class="prog"><div class="bar"></div></div>
        <div class="msg"></div>
        <div class="review"></div>
        <footer class="card-foot">
          <button class="secondary sm gen">Generate</button>
          <button class="ghost sm cancel" hidden>Cancel</button>
          <button class="ghost sm discard" hidden>Discard</button>
          <label class="approve"><input type="checkbox" class="approvebox"> Approve</label>
        </footer>
      </article>`).join("");
    $$(".card", grid).forEach(card => {
      const k = card.dataset.kind;
      $(".pickbox", card).onchange = e => { e.target.checked ? S.picked.add(k) : S.picked.delete(k); store.set("es.picked", [...S.picked]); renderMediaSummary(); card.classList.toggle("picked", e.target.checked); };
      $(".gen", card).onclick = () => generate([k]);
      $(".cancel", card).onclick = () => api(`/api/projects/${S.project.id}/media/${k}/cancel`, { method: "POST" });
      $(".discard", card).onclick = async () => {
        if (await confirmBox(`Discard ${KINDS[k].name}?`, "The generated files will be deleted.", "Discard"))
          api(`/api/projects/${S.project.id}/media/${k}`, { method: "DELETE" });
      };
      $(".approvebox", card).onchange = async e => {
        try { S.project.media[k] = await api(`/api/projects/${S.project.id}/media/${k}/approve`, { method: "POST", body: { approved: e.target.checked } }); }
        catch (err) { e.target.checked = false; toast(err.message); }
        renderCard(k); renderMediaSummary();
      };
      card.addEventListener("input", e => {
        if (e.target.type === "range") e.target.nextElementSibling.textContent = `${e.target.value >= 0 ? "+" : ""}${e.target.value}%`;
        if (e.target.closest(".opts")) store.set(`es.opts.${k}`, readOpts(card));
      });
    });
  }
  for (const k of Object.keys(KINDS)) { renderCard(k, true); }
  renderMediaSummary();
}

function renderCard(kind, rebuildOpts = false) {
  const card = $(`.card[data-kind="${kind}"]`);
  if (!card || !S.project) return;
  const m = S.project.media[kind];
  const st = m?.status;
  const running = st === "running" || st === "queued";
  const stale = m && st === "done" && m.essay_hash !== S.project.essay_hash;

  $(".pickbox", card).checked = S.picked.has(kind);
  card.classList.toggle("picked", S.picked.has(kind) && !m?.approved);
  card.classList.toggle("approved", !!m?.approved);

  const opts = $(".opts", card);
  if (rebuildOpts || !opts.innerHTML) opts.innerHTML = optsHtml(kind, optionsFor(kind));
  opts.classList.toggle("disabled", running);

  const badge = $(".badge", card);
  badge.className = "badge " + (stale ? "stale" : st || "");
  badge.textContent = stale ? "Outdated" : { queued: "Queued", running: "Generating…", done: m.approved ? "Approved" : "Ready for review",
    error: "Failed", cancelled: "Cancelled" }[st] || "Not generated";

  const prog = $(".prog", card);
  prog.hidden = !m || st === "cancelled";
  prog.classList.toggle("indet", running && !(m.progress > 0));
  $(".bar", prog).style.width = `${Math.round((m?.progress || 0) * 100)}%`;
  const msg = $(".msg", card);
  msg.textContent = m ? (m.message || "") : "";
  msg.classList.toggle("err", st === "error");

  $(".gen", card).textContent = m ? "Regenerate" : "Generate";
  $(".gen", card).disabled = running;
  $(".cancel", card).hidden = !running;
  $(".discard", card).hidden = !m || running;
  const ap = $(".approve", card), apb = $(".approvebox", card);
  apb.disabled = st !== "done"; apb.checked = !!m?.approved;
  ap.classList.toggle("on", !!m?.approved); ap.classList.toggle("off", st !== "done");

  const review = $(".review", card);
  const key = m && (st === "done" || st === "running") ? `${S.project.id}|${JSON.stringify(m.files)}|${m.finished}` : "";
  if (review.dataset.key === key) return;
  review.dataset.key = key;
  review.innerHTML = "";
  if (!m || !m.files) return;
  const f = m.files, meta = m.meta || {};
  if (kind === "summary" && f.text) {
    review.innerHTML = `<div class="prose">Loading…</div><div class="facts">${meta.words || ""} words · ${esc(meta.style || "")}</div>`;
    fetch(fileUrl(f.text)).then(r => r.text()).then(t => ($(".prose", review).textContent = t));
  }
  if ((kind === "audiobook" || kind === "podcast") && f.audio) {
    review.innerHTML = `<audio controls preload="metadata" src="${fileUrl(f.audio)}?v=${m.finished}"></audio>
      <div class="facts">${fmtDur(meta.duration)} · ${kind === "podcast" ? `${meta.turns} turns · hosts ${esc((meta.hosts || []).join(" & "))}` : `${esc(meta.voice)} @ ${esc(meta.rate)}`}
       · <a class="dl" href="${fileUrl(f.audio, 1)}">Download</a></div>`;
  }
  if (kind === "podcast" && f.script) {
    review.insertAdjacentHTML("beforeend", `<details><summary>Script</summary><pre>Loading…</pre></details>`);
    fetch(fileUrl(f.script)).then(r => r.text()).then(t => ($("pre", review).textContent = t));
  }
  if (kind === "video" && f.video) {
    review.innerHTML = `<video controls preload="metadata" poster="${fileUrl(f.thumbnail)}?v=${m.finished}" src="${fileUrl(f.video)}?v=${m.finished}"></video>
      <div class="facts">${meta.kind === "narrative" ? "Narrated story" : "Explainer"} · ${meta.slides} slides · ${fmtDur(meta.duration)}
       · <a class="dl" href="${fileUrl(f.video, 1)}">Download</a></div>`;
  }
}

function approvedKinds() { return Object.entries(S.project?.media || {}).filter(([, v]) => v.approved && v.status === "done").map(([k]) => k); }
function renderMediaSummary() {
  if (!S.project) return;
  const n = approvedKinds().length;
  $("#approvedCount").textContent = n ? `${n} approved` : "Nothing approved: only the essay text will be published";
  const picked = [...S.picked];
  $("#generateSelected").textContent = picked.length ? `Generate selected (${picked.length})` : "Generate selected";
  $("#generateSelected").disabled = !picked.length;
  $("#staleBanner").hidden = !Object.values(S.project.media).some(m => m.status === "done" && m.essay_hash !== S.project.essay_hash);
  const model = S.models.find(m => m.id === S.project.model);
  $("#mediaModel").textContent = model ? `Using ${model.name}` : "";
}
$("#generateSelected").onclick = () => generate([...S.picked]);

async function generate(kinds) {
  for (const k of kinds) {
    const card = $(`.card[data-kind="${k}"]`);
    const m = S.project.media[k];
    if (m && (m.status === "running" || m.status === "queued")) continue;
    const opts = readOpts(card);
    store.set(`es.opts.${k}`, opts);
    try {
      S.project.media[k] = await api(`/api/projects/${S.project.id}/media/${k}`, { method: "POST", body: { ...opts, model: S.project.model } });
      renderCard(k);
    } catch (e) { toast(`${KINDS[k].name}: ${e.message}`, 4000); }
  }
}

// ------------------------------------------------------------------ publish stage
function renderPublish() {
  const items = [`<li><span class="card-icon">${ICONS.text}</span><span><b>Essay text</b><div class="sub">${words(S.project.essay).toLocaleString()} words · always included</div></span></li>`];
  for (const k of approvedKinds()) {
    const m = S.project.media[k];
    const sub = m.meta?.duration ? fmtDur(m.meta.duration) : m.meta?.words ? `${m.meta.words} words` : "";
    const stale = m.essay_hash !== S.project.essay_hash ? " · <b>made from an earlier version of the essay</b>" : "";
    items.push(`<li><span class="card-icon">${ICONS[k]}</span><span><b>${KINDS[k].name}</b><div class="sub">${KINDS[k].type}${sub ? " · " + sub : ""}${stale}</div></span></li>`);
  }
  if (items.length === 1) items.push(`<li class="empty">No approved media. Only the essay text will be published.</li>`);
  $("#pubContents").innerHTML = items.join("");

  const chosen = new Set(store.get("es.platforms", []));
  $("#platformList").innerHTML = S.platforms.map(p => `
    <label class="platform ${chosen.has(p.id) ? "on" : ""}"><input type="checkbox" value="${p.id}" ${chosen.has(p.id) ? "checked" : ""}>
      <span class="pname">${esc(p.name)}</span><span class="pspec">${esc(p.aspect)} · ${esc(p.text)}</span></label>`).join("");
  $$("#platformList input").forEach(i => (i.onchange = () => {
    i.closest(".platform").classList.toggle("on", i.checked);
    store.set("es.platforms", $$("#platformList input:checked").map(x => x.value)); updatePublishBtn();
  }));
  updatePublishBtn();
  renderPublishResults();
}
function updatePublishBtn() {
  const run = S.project.publish.runs.at(-1);
  $("#publishBtn").disabled = !$$("#platformList input:checked").length || run?.status === "running";
}
$("#publishBtn").onclick = async () => {
  const platforms = $$("#platformList input:checked").map(x => x.value);
  try {
    const run = await api(`/api/projects/${S.project.id}/publish`, { method: "POST", body: { platforms } });
    if (!S.project.publish.runs.some(r => r.id === run.id)) S.project.publish.runs.push(run);
    renderPublishResults();
  } catch (e) { toast(e.message, 4000); }
};
function renderPublishResults() {
  if (S.stage !== "publish" || !S.project) return;
  const run = S.project.publish.runs.at(-1);
  $("#pubResults").hidden = !run;
  updatePublishBtn();
  if (!run) return;
  const names = Object.fromEntries(S.platforms.map(p => [p.id, p.name]));
  $("#pubRunMeta").textContent = `${new Date(run.started * 1000).toLocaleString()} · ${run.status === "running" ? "packaging…" : run.status}${run.error ? " · " + run.error : ""}`;
  $("#pubResultList").innerHTML = run.platforms.map(p => {
    const r = run.results[p] || { status: "queued" };
    const badge = r.status === "packaged" ? `<span class="badge done">Package ready</span>` :
      run.status === "error" ? `<span class="badge error">Failed</span>` : `<span class="badge running">${esc(r.status)}…</span>`;
    const post = r.post ? (r.post.thread ? r.post.thread.map((t, i) => `${i + 1}/ ${t}`).join("\n\n") :
      Object.entries(r.post).map(([k, v]) => `${k.toUpperCase()}: ${Array.isArray(v) ? v.join(", ") : v}`).join("\n\n")) : "";
    return `<div class="result"><div class="result-head"><b>${esc(names[p] || p)}</b>${badge}
      <span class="meta">${r.files ? r.files.length + " files" : ""}</span>
      ${r.zip ? `<a class="dl" href="${fileUrl(r.zip, 1)}">Download package (.zip)</a>` : ""}</div>
      ${(r.warnings || []).map(w => `<div class="warn">⚠ ${esc(w)}</div>`).join("")}
      ${post ? `<pre>${esc(post)}</pre>` : ""}</div>`;
  }).join("");
}

// ------------------------------------------------------------------ boot
(async function boot() {
  connectEvents();
  $("#aiLive").checked = store.get("es.live", true);
  [S.models, S.voices, S.platforms] = await Promise.all([api("/api/models"), api("/api/voices"), api("/api/platforms")]);
  $("#model").innerHTML = S.models.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("");
  let list = await loadProjects();
  if (!list.length) { await api("/api/projects", { method: "POST" }); list = await loadProjects(); }
  const last = store.get("es.last");
  await openProject(list.some(p => p.id === last) ? last : list[0].id);
})().catch(e => { localLog("Startup failed: " + e.message, "error"); });
