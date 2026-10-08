const LANGS = [
  ["fr", "Français"], ["en", "Anglais"], ["es", "Espagnol"], ["de", "Allemand"],
  ["it", "Italien"], ["pt", "Portugais"], ["nl", "Néerlandais"], ["pl", "Polonais"],
  ["sv", "Suédois"], ["no", "Norvégien"], ["da", "Danois"], ["fi", "Finnois"],
  ["el", "Grec"], ["tr", "Turc"], ["ru", "Russe"], ["uk", "Ukrainien"],
  ["cs", "Tchèque"], ["hu", "Hongrois"], ["ro", "Roumain"], ["ar", "Arabe"],
  ["iw", "Hébreu"], ["hi", "Hindi"], ["ja", "Japonais"], ["ko", "Coréen"],
  ["zh-CN", "Chinois (simplifié)"], ["vi", "Vietnamien"], ["th", "Thaï"], ["id", "Indonésien"],
];
const LANG_NAME = Object.fromEntries(LANGS);

const $ = (id) => document.getElementById(id);
const screen = $("screen"), fileInput = $("file");
const capMain = $("cap-main"), capSrc = $("cap-src");
const timeline = $("timeline"), fill = $("fill"), head = $("head");
const tlLabel = $("tl-label"), tlStats = $("tl-stats");
const IDLE_CAPTION = capMain.innerHTML;

let upload = null;   // réponse de /api/upload
let jobId = null;
let busy = false;
let pollTimer = null;
let xhr = null;

// ------------------------------------------------------------------ helpers

function show(...ids) {
  for (const id of ["settings", "working", "done"]) $(id).hidden = !ids.includes(id);
}

function caption(main, src = "") {
  capMain.textContent = main;
  capSrc.textContent = src;
  capMain.classList.remove("swap");
  void capMain.offsetWidth;
  capMain.classList.add("swap");
}

function progress(ratio, label, stats) {
  timeline.hidden = false;
  timeline.classList.toggle("indeterminate", ratio == null);
  if (ratio != null) {
    const pct = Math.max(0, Math.min(1, ratio)) * 100;
    fill.style.width = pct + "%";
    head.style.left = pct + "%";
  }
  tlLabel.textContent = label;
  tlStats.textContent = stats || "";
}

function alertMsg(msg) {
  $("alert").textContent = msg;
  $("alert").hidden = !msg;
}

function bytes(n) {
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " Ko";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1).replace(".", ",") + " Mo";
  return (n / 1024 ** 3).toFixed(2).replace(".", ",") + " Go";
}

function duration(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return m ? `${m} min ${String(s % 60).padStart(2, "0")} s` : `${s} s`;
}

// Timecode décoratif qui avance avec la progression
function timecode(ratio) {
  const t = Math.floor(ratio * 2 * 3600 * 1000);
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return `${p(Math.floor(t / 3600000))}:${p(Math.floor(t / 60000) % 60)}:${p(Math.floor(t / 1000) % 60)},${p(t % 1000, 3)}`;
}

function nf(n) { return n.toLocaleString("fr-FR"); }

function reset() {
  clearTimeout(pollTimer);
  if (xhr) xhr.abort();
  xhr = null;
  if (upload) fetch(`/api/uploads/${upload.id}`, { method: "DELETE" }).catch(() => {});
  if (jobId) fetch(`/api/jobs/${jobId}`, { method: "DELETE" }).catch(() => {});
  upload = jobId = null;
  busy = false;
  screen.classList.remove("busy");
  capMain.innerHTML = IDLE_CAPTION;
  capSrc.textContent = "";
  $("tc-left").textContent = timecode(0);
  $("tc-right").textContent = "MKV · MP4 · SRT · ASS · VTT";
  timeline.hidden = true;
  fileInput.value = "";
  alertMsg("");
  show();
}

// ------------------------------------------------------------------- upload

function pick() { if (!busy) fileInput.click(); }

screen.addEventListener("click", pick);
screen.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
});
fileInput.addEventListener("change", () => fileInput.files[0] && send(fileInput.files[0]));

for (const ev of ["dragenter", "dragover"]) {
  screen.addEventListener(ev, (e) => { e.preventDefault(); if (!busy) screen.classList.add("drag"); });
}
for (const ev of ["dragleave", "drop"]) {
  screen.addEventListener(ev, (e) => { e.preventDefault(); screen.classList.remove("drag"); });
}
screen.addEventListener("drop", (e) => {
  if (!busy && e.dataTransfer.files[0]) send(e.dataTransfer.files[0]);
});
// Évite que le navigateur ouvre le fichier si on le lâche à côté
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

function send(file) {
  reset();
  busy = true;
  screen.classList.add("busy");
  $("tc-right").textContent = file.name;
  const isVideo = !/\.(srt|ass|ssa|vtt|sub)$/i.test(file.name);
  caption(isVideo ? "Envoi de la vidéo…" : "Lecture du fichier…", isVideo ? "Seules les pistes de sous-titres sont gardées." : "");
  progress(0, "Envoi", `0 / ${bytes(file.size)}`);

  const started = performance.now();
  xhr = new XMLHttpRequest();
  xhr.open("PUT", `/api/upload?name=${encodeURIComponent(file.name)}`);
  xhr.responseType = "json";

  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const r = e.loaded / e.total;
    const speed = e.loaded / ((performance.now() - started) / 1000);
    const left = speed > 0 ? (e.total - e.loaded) / speed : 0;
    $("tc-left").textContent = timecode(r);
    if (r < 1) {
      progress(r, `Envoi · ${Math.floor(r * 100)} %`,
        `${bytes(e.loaded)} / ${bytes(e.total)}${r > 0.02 ? " · ~" + duration(left) : ""}`);
    } else {
      progress(null, isVideo ? "Extraction des pistes" : "Analyse", "");
      caption(isVideo ? "Extraction des sous-titres…" : "Analyse…");
    }
  };

  xhr.onload = function () {
    const res = this.response || {};
    xhr = null;
    busy = false;
    screen.classList.remove("busy");
    if (this.status !== 200) return fail(res.detail || `Erreur ${this.status}`);
    upload = res;
    timeline.hidden = true;
    renderSettings();
  };
  xhr.onerror = () => { xhr = null; fail("Connexion interrompue pendant l’envoi."); };
  xhr.send(file);
}

function fail(msg) {
  busy = false;
  screen.classList.remove("busy");
  timeline.hidden = true;
  capMain.innerHTML = IDLE_CAPTION;
  capSrc.textContent = "";
  alertMsg(msg);
}

// ----------------------------------------------------------------- settings

function fillSelect(el, withAuto, value) {
  el.innerHTML = "";
  if (withAuto) el.add(new Option("Détection automatique", "auto"));
  for (const [code, name] of LANGS) el.add(new Option(name, code));
  el.value = value;
}

function renderSettings() {
  const { tracks, filename, skipped } = upload;
  $("file-name").textContent = filename;
  $("tc-right").textContent = filename;
  $("tc-left").textContent = timecode(0);

  const box = $("tracks");
  box.innerHTML = "";
  tracks.forEach((t, i) => {
    const label = document.createElement("label");
    label.className = "track-opt";
    const lang = t.language ? t.language.split("-")[0] : "—";
    const title = t.title || (t.language && LANG_NAME[t.language]) || (tracks.length > 1 ? `Piste ${i + 1}` : "Sous-titres");
    label.innerHTML = `
      <input type="radio" name="track" value="${t.index}" ${i === 0 ? "checked" : ""}>
      <span class="lang-tag"></span>
      <span class="track-title"></span>
      <span class="track-count"></span>
      <span class="track-sample"></span>`;
    label.querySelector(".lang-tag").textContent = lang;
    label.querySelector(".track-title").textContent = title;
    label.querySelector(".track-count").textContent = `${nf(t.lines)} répliques · ${t.format.toUpperCase()}`;
    label.querySelector(".track-sample").textContent = t.sample[0] ? `« ${t.sample[0]} »` : "";
    label.querySelector("input").addEventListener("change", syncSource);
    box.appendChild(label);
  });
  $("tracks-field").hidden = false;

  fillSelect($("source"), true, "auto");
  const saved = safeGet("target");
  fillSelect($("target"), false, saved && LANG_NAME[saved] ? saved : "en");
  syncSource();

  const first = tracks[0];
  caption(first.sample[1] || first.sample[0] || "Prêt.",
    `${tracks.length} piste${tracks.length > 1 ? "s" : ""} trouvée${tracks.length > 1 ? "s" : ""}` +
    (skipped ? ` · ${skipped} piste${skipped > 1 ? "s" : ""} image ignorée${skipped > 1 ? "s" : ""}` : ""));
  show("settings");
}

function currentTrack() {
  const idx = +document.querySelector('input[name="track"]:checked').value;
  return upload.tracks.find((t) => t.index === idx);
}

function syncSource() {
  const t = currentTrack();
  $("source").value = t.language && LANG_NAME[t.language] ? t.language : "auto";
  // Si la cible = la source, on propose une autre langue
  if ($("target").value === $("source").value) $("target").value = t.language === "en" ? "fr" : "en";
  const sample = t.sample[1] || t.sample[0];
  if (sample) caption(sample, t.title || "");
}

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

$("change").addEventListener("click", () => { reset(); pick(); });

// -------------------------------------------------------------- translation

$("go").addEventListener("click", async () => {
  const t = currentTrack();
  const source = $("source").value, target = $("target").value;
  if (source === target) return alertMsg("La langue d’origine et la langue cible sont identiques.");
  alertMsg("");
  safeSet("target", target);
  $("go").disabled = true;

  try {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: upload.id, track: t.index, source, target, remove_sdh: $("sdh").checked }),
    });
    const res = await r.json();
    if (!r.ok) throw new Error(res.detail || `Erreur ${r.status}`);
    jobId = res.id;
  } catch (e) {
    $("go").disabled = false;
    return alertMsg(e.message);
  }
  $("go").disabled = false;

  busy = true;
  screen.classList.add("busy");
  show("working");
  caption("Préparation…", `${LANG_NAME[source] || "Détection auto"} → ${LANG_NAME[target]}`);
  progress(null, "Démarrage", "");
  poll(performance.now());
});

async function poll(started) {
  let s;
  try {
    const r = await fetch(`/api/jobs/${jobId}`);
    s = await r.json();
    if (!r.ok) throw new Error(s.detail);
  } catch (e) {
    if (!jobId) return;
    busy = false;
    show("settings");
    return fail(e.message || "Connexion perdue.");
  }

  if (s.status === "running") {
    if (s.total) {
      const r = s.done / s.total;
      const elapsed = (performance.now() - started) / 1000;
      const eta = s.done ? (elapsed / s.done) * (s.total - s.done) : null;
      progress(r, `Traduction · ${Math.floor(r * 100)} %`,
        `${nf(s.done)} / ${nf(s.total)} répliques` + (eta != null && s.done < s.total ? ` · ~${duration(eta)}` : ""));
      $("tc-left").textContent = timecode(r);
      if (s.last_target && s.last_target !== capMain.textContent) caption(s.last_target, s.last_source);
    }
    pollTimer = setTimeout(() => poll(started), 450);
    return;
  }

  busy = false;
  screen.classList.remove("busy");

  if (s.status === "done") {
    progress(1, "Terminé", `${nf(s.total)} répliques · ${duration(s.elapsed)}`);
    $("tc-left").textContent = timecode(1);
    caption("Fin.", "");
    const dl = $("download");
    dl.href = `/api/jobs/${jobId}/download`;
    dl.setAttribute("download", s.filename);
    dl.textContent = `Télécharger le .${s.filename.split(".").pop()}`;
    $("done-line").innerHTML = "";
    $("done-line").append(`Sous-titres traduits.`);
    const small = document.createElement("small");
    small.textContent = s.filename + (s.removed_sdh ? ` · ${nf(s.removed_sdh)} descriptions retirées` : "");
    $("done-line").append(small);
    show("done");
  } else {
    show("settings");
    timeline.hidden = true;
    alertMsg(s.error || "La traduction a échoué.");
  }
}

$("cancel").addEventListener("click", () => {
  clearTimeout(pollTimer);
  fetch(`/api/jobs/${jobId}`, { method: "DELETE" }).catch(() => {});
  jobId = null;
  busy = false;
  screen.classList.remove("busy");
  timeline.hidden = true;
  caption("Traduction annulée.", "");
  show("settings");
});

$("again").addEventListener("click", () => { reset(); pick(); });

fetch("/api/config").then((r) => r.json()).then((c) => { $("provider").textContent = c.provider; }).catch(() => {});
reset();
