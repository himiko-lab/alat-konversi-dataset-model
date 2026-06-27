/* app.js — Konverter Font -> Dataset (frontend lokal, vanilla JS) */
"use strict";

const ALLOWED = [".otf", ".ttf", ".ttc"];

const el = (id) => document.getElementById(id);
const drop = el("drop");
const fileInput = el("fileInput");
const folderInput = el("folderInput");
const fileSummary = el("fileSummary");
const convertBtn = el("convertBtn");

let selectedFiles = []; // { file, rel }
let previewId = null;    // sesi preview di backend
let previewReady = false; // tabel preview sudah tampil minimal sekali

/* ---------------- pemilihan file ---------------- */
function extOK(name) {
  const n = name.toLowerCase();
  return ALLOWED.some((e) => n.endsWith(e));
}

function setFiles(list) {
  const accepted = [];
  let rejected = 0;
  for (const item of list) {
    if (extOK(item.file.name)) accepted.push(item);
    else rejected++;
  }
  selectedFiles = accepted;
  renderFileSummary(rejected);
}

function renderFileSummary(rejected) {
  if (selectedFiles.length === 0) {
    fileSummary.classList.add("hidden");
    resetPreview();
    if (rejected > 0) {
      fileSummary.classList.remove("hidden");
      fileSummary.innerHTML = `<span class="badge">Ditolak</span> ${rejected} file bukan font (.otf/.ttf/.ttc) diabaikan.`;
    }
    return;
  }
  fileSummary.classList.remove("hidden");
  let html = `<strong>${selectedFiles.length}</strong> file font siap dikonversi.`;
  if (rejected > 0) html += ` <span class="badge">${rejected} ditolak</span>`;
  const names = selectedFiles.slice(0, 6).map((f) => f.file.name).join(", ");
  html += `<div class="muted small" style="margin-top:6px">${names}${
    selectedFiles.length > 6 ? ` … (+${selectedFiles.length - 6} lagi)` : ""
  }</div>`;
  fileSummary.innerHTML = html;
  setupPreview(); // unggah sampel & tampilkan pratinjau tag
}

function updateConvertEnabled() {
  const ready = selectedFiles.length > 0 && previewReady;
  convertBtn.disabled = !ready;
  el("convertHint").classList.toggle("hidden", ready);
}

function filesFromInput(input) {
  return Array.from(input.files).map((file) => ({
    file,
    rel: file.webkitRelativePath || file.name,
  }));
}

el("pickFiles").addEventListener("click", () => fileInput.click());
el("pickFolder").addEventListener("click", () => folderInput.click());
fileInput.addEventListener("change", () => setFiles(filesFromInput(fileInput)));
folderInput.addEventListener("change", () => {
  const picked = filesFromInput(folderInput);
  // Kalau pengguna pilih folder, mode "Berbasis folder" paling masuk akal —
  // set SEBELUM setFiles agar pratinjau langsung pakai mode yang benar.
  if (picked.length) document.querySelector('input[name=mode][value=folder]').checked = true;
  setFiles(picked);
});

/* ---------------- drag & drop (termasuk folder) ---------------- */
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === "dragleave" && drop.contains(e.relatedTarget)) return;
    drop.classList.remove("drag");
  })
);

drop.addEventListener("drop", async (e) => {
  const items = e.dataTransfer.items;
  if (items && items.length && items[0].webkitGetAsEntry) {
    const collected = [];
    const entries = [];
    for (const it of items) {
      const entry = it.webkitGetAsEntry();
      if (entry) entries.push(entry);
    }
    for (const entry of entries) await walkEntry(entry, "", collected);
    setFiles(collected);
  } else {
    setFiles(Array.from(e.dataTransfer.files).map((file) => ({ file, rel: file.name })));
  }
});

// Telusuri folder yang di-drop secara rekursif agar struktur folder terjaga.
function walkEntry(entry, prefix, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => {
        out.push({ file, rel: prefix + entry.name });
        resolve();
      }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readAll = () => {
        reader.readEntries(async (batch) => {
          if (!batch.length) return resolve();
          for (const child of batch) await walkEntry(child, prefix + entry.name + "/", out);
          readAll(); // readEntries bisa parsial — panggil ulang sampai kosong
        }, () => resolve());
      };
      readAll();
    } else resolve();
  });
}

/* ---------------- konversi ---------------- */
convertBtn.addEventListener("click", startConversion);

function lockUI(lock) {
  convertBtn.disabled = lock;
  document.querySelectorAll("input, button.btn").forEach((n) => {
    if (n.id === "resetBtn") return;
    n.disabled = lock;
  });
}

async function startConversion() {
  if (!selectedFiles.length) return;
  el("errorBox").classList.add("hidden");
  el("resultCard").classList.add("hidden");
  el("progress").classList.remove("hidden");
  setProgress(0, selectedFiles.length, "Mengunggah font…");
  el("progFile").textContent = "";
  lockUI(true);
  convertBtn.textContent = "Memproses…";

  try {
    // 1) Upload
    const fd = new FormData();
    for (const item of selectedFiles) {
      fd.append("files", item.file, item.file.name);
      fd.append("paths", item.rel);
    }
    const upRes = await fetch("/api/upload", { method: "POST", body: fd });
    const up = await upRes.json();
    if (!upRes.ok) throw new Error(up.error || "Upload gagal.");

    // 2) Mulai konversi
    const opts = collectOptions(up.job_id);
    const cRes = await fetch("/api/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    });
    const c = await cRes.json();
    if (!cRes.ok) throw new Error(c.error || "Gagal memulai konversi.");

    // 3) Dengarkan progress (SSE)
    listenProgress(up.job_id);
  } catch (err) {
    showError(err.message);
    lockUI(false);
    convertBtn.textContent = "Konversi";
  }
}

function effectiveCategory() {
  const preset = el("categoryPreset").value;
  if (preset === "__custom__") return el("defaultCategory").value.trim();
  return preset; // "" berarti belum dipilih
}

function currentMode() {
  return document.querySelector("input[name=mode]:checked").value;
}

function collectOptions(jobId) {
  return {
    job_id: jobId,
    mode: currentMode(),
    default_category: effectiveCategory(),
    detect_contrast: el("detectContrast").checked,
    chars: el("chars").value,
    val_split: parseFloat(el("valSplit").value),
    max_path_len: parseInt(el("maxPathLen").value, 10),
  };
}

/* ---------------- preview tag (live) ---------------- */
// Tampilkan/sembunyikan input kustom saat preset "(kustom…)" dipilih.
el("categoryPreset").addEventListener("change", () => {
  const custom = el("categoryPreset").value === "__custom__";
  el("defaultCategory").classList.toggle("hidden", !custom);
  if (custom) el("defaultCategory").focus();
  refreshPreview();
});

// Opsi apa pun berubah -> perbarui pratinjau (debounce).
let previewTimer = null;
function refreshPreview() {
  if (!previewId) return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(doPreview, 250);
}
["change", "input"].forEach((ev) => {
  document.querySelectorAll("input[name=mode], #defaultCategory, #detectContrast").forEach((n) =>
    n.addEventListener(ev, refreshPreview)
  );
});

function rootNameOf(files) {
  // Nama folder root bersama (komponen pertama) bila semua file berbagi sama.
  const firsts = new Set();
  for (const f of files) {
    const parts = f.rel.replace(/\\/g, "/").split("/").filter(Boolean);
    if (parts.length >= 2) firsts.add(parts[0]);
  }
  return firsts.size === 1 ? [...firsts][0] : "";
}

function pickSample(files, n) {
  if (files.length <= n) return files.slice();
  // Ambil merata di seluruh daftar agar variasi folder/gaya terwakili.
  const step = files.length / n;
  const out = [];
  for (let i = 0; i < n; i++) out.push(files[Math.floor(i * step)]);
  return out;
}

async function setupPreview() {
  resetPreview(false);
  el("previewHint").classList.add("hidden");
  setPreviewLoading("Menyiapkan pratinjau…");
  try {
    const sample = pickSample(selectedFiles, 8);
    const fd = new FormData();
    for (const item of sample) {
      fd.append("files", item.file, item.file.name);
      fd.append("paths", item.rel);
    }
    fd.append("root_name", rootNameOf(selectedFiles));
    const res = await fetch("/api/preview_upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Gagal menyiapkan pratinjau.");
    previewId = data.preview_id;
    await doPreview();
  } catch (err) {
    setPreviewLoading("⚠ " + err.message);
  }
}

async function doPreview() {
  if (!previewId) return;
  setPreviewLoading("Menghitung tag sampel…");
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        preview_id: previewId,
        mode: currentMode(),
        default_category: effectiveCategory(),
        detect_contrast: el("detectContrast").checked,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Gagal membuat pratinjau.");
    renderPreview(data);
    previewReady = true;
    updateConvertEnabled();
  } catch (err) {
    setPreviewLoading("⚠ " + err.message);
  }
}

function setPreviewLoading(msg) {
  el("previewArea").innerHTML = `<p class="preview-loading">${escapeHtml(msg)}</p>`;
}

function renderPreview(data) {
  const rows = data.rows || [];
  renderWarnings(data);

  const nUniq = data.n_unique_desc || 0;
  const consistent = nUniq <= 1;
  let html = `<div class="preview-meta">
    <span class="pill ${consistent ? "ok" : "warn"}">Deskriptor unik: ${nUniq}${consistent ? " ✓" : ""}</span>
    <span class="muted small">menampilkan ${rows.length} font sampel</span>
  </div>`;

  html += `<table class="preview-table"><thead><tr>
    <th>File</th><th>Tag yang akan dihasilkan</th></tr></thead><tbody>`;
  for (const r of rows) {
    if (!r.tag) {
      html += `<tr class="bad"><td class="fname">${escapeHtml(r.name)}</td>
        <td class="tag">⚠ tak terbaca / bukan font valid</td></tr>`;
      continue;
    }
    const src = r.source ? `<span class="src-tag">${escapeHtml(r.source)}</span>` : "";
    html += `<tr><td class="fname">${escapeHtml(r.name)}</td>
      <td class="tag">${escapeHtml(r.tag)}${src}</td></tr>`;
  }
  html += `</tbody></table>`;
  el("previewArea").innerHTML = html;
}

function renderWarnings(data) {
  const w = el("warnings");
  const banners = [];
  if (data.no_category) {
    banners.push(
      `⚠️ <strong>Kategori belum dipilih</strong> — tag akan jatuh ke deteksi otomatis (PANOSE/nama font) ` +
      `dan bisa tidak konsisten. Pilih kategori di "Kategori default" agar seluruh batch seragam.`
    );
  } else if (data.generic_serif) {
    banners.push(
      `⚠️ Sebagian font jatuh ke kategori <code>serif</code> generik (metadata kosong). ` +
      `Kalau itu bukan serif, pilih kategori yang sesuai.`
    );
  }
  if ((data.detect_contrast || data.mode === "auto" || data.no_category) && data.n_unique_desc > 1) {
    banners.push(
      `⚠️ <strong>Label tidak konsisten</strong> — terdeteksi ${data.n_unique_desc} deskriptor berbeda ` +
      `untuk batch ini. Kalau batch-nya satu-gaya, pakai <strong>"Kategori default"</strong> ` +
      `(dan matikan estimasi kontras) agar seragam.`
    );
  }
  w.innerHTML = banners.map((b) => `<div class="warn-banner">${b}</div>`).join("");
}

function resetPreview(clearArea = true) {
  previewId = null;
  previewReady = false;
  el("warnings").innerHTML = "";
  if (clearArea) {
    el("previewArea").innerHTML =
      `<p class="muted small" id="previewHint">Pilih file/folder dulu untuk melihat contoh tag.</p>`;
  }
  updateConvertEnabled();
}

function listenProgress(jobId) {
  const es = new EventSource(`/api/progress/${jobId}`);
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "progress") {
      setProgress(msg.done, msg.total, `Memproses ${msg.done} dari ${msg.total} font…`);
      if (msg.current) el("progFile").textContent = "↳ " + msg.current;
    } else if (msg.type === "done") {
      es.close();
      setProgress(msg.result.ok ? 1 : 0, 1, "Selesai!");
      fetch(`/api/cleanup/${jobId}`, { method: "POST" }).catch(() => {});
      showResult(jobId, msg.result);
    } else if (msg.type === "error") {
      es.close();
      showError(msg.message || "Terjadi kesalahan saat konversi.");
      lockUI(false);
      convertBtn.textContent = "Konversi";
    }
  };
  es.onerror = () => {
    // Browser otomatis reconnect; biarkan kecuali job sudah selesai/error.
  };
}

function setProgress(done, total, text) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  el("barFill").style.width = pct + "%";
  el("progText").textContent = text;
}

function showError(msg) {
  const box = el("errorBox");
  box.textContent = "⚠ " + msg;
  box.classList.remove("hidden");
  el("progress").classList.add("hidden");
}

/* ---------------- hasil ---------------- */
function showResult(jobId, r) {
  el("progress").classList.add("hidden");
  const sum = el("resultSummary");

  const stat = (num, lbl) => `<div class="stat"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`;
  let html = `<div class="stat-grid">
    ${stat(r.ok, "font OK")}
    ${stat(r.glyphs, "total glyph")}
    ${stat(r.train, "train")}
    ${stat(r.val, "val")}
  </div>`;

  const nUniq = r.n_unique_desc || 0;
  const consistent = nUniq <= 1;
  html += `<div class="dist"><h3>Konsistensi tag</h3>
    <p class="muted small" style="margin:0 0 8px">Deskriptor kategori unik:
      <strong style="color:${consistent ? "var(--ok)" : "#ffd479"}">${nUniq}</strong>
      ${nUniq === 1 ? "(konsisten ✓)" : "(campuran — idealnya 1 untuk batch satu-gaya)"}</p>
    <div class="chip-row">${
      Object.keys(r.descriptors || {})
        .map((k) => `<span class="chip">${escapeHtml(k)}<b>${r.descriptors[k]}</b></span>`)
        .join("")
    }</div></div>`;

  html += distBlock("Kategori", r.categories);
  html += distBlock("Berat", r.weights);
  html += distBlock("Sumber tag (akurasi)", r.sources);

  if (r.too_long > 0)
    html += `<p class="muted small">${r.too_long} glyph dilewati karena path terlalu panjang.</p>`;

  if (r.sample_tags && r.sample_tags.length) {
    html += `<div class="dist"><h3>Contoh tag</h3><div class="chip-row">`;
    html += r.sample_tags.map((t) => `<span class="chip mono" style="font-size:.78rem">${escapeHtml(t)}</span>`).join("");
    html += `</div></div>`;
  }

  if (r.skipped && r.skipped.length) {
    html += `<details class="skipped"><summary>${r.skipped.length} font dilewati (lihat alasan)</summary><ul>`;
    html += r.skipped.slice(0, 50).map((s) => `<li><b>${escapeHtml(s.name)}</b>: ${escapeHtml(s.reason)}</li>`).join("");
    if (r.skipped.length > 50) html += `<li>… dan ${r.skipped.length - 50} lainnya</li>`;
    html += `</ul></details>`;
  }
  sum.innerHTML = html;

  // Tombol download
  const dl = el("downloads");
  const link = (kind, label) => `<a class="btn primary" href="/api/download/${jobId}/${kind}" download>${label}</a>`;
  let dls = link("train", "⬇ dataset_train.jsonl");
  if (r.has_val) dls += link("val", "⬇ dataset_val.jsonl");
  dls += link("summary", "⬇ ringkasan.txt");
  dls += `<a class="btn" href="/api/download/${jobId}/zip" download>⬇ Semua (.zip)</a>`;
  dl.innerHTML = dls;

  el("resultCard").classList.remove("hidden");
  el("resultCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function distBlock(title, obj) {
  const keys = obj ? Object.keys(obj) : [];
  if (!keys.length) return "";
  let h = `<div class="dist"><h3>${title}</h3><div class="chip-row">`;
  h += keys.map((k) => `<span class="chip">${escapeHtml(String(k))}<b>${obj[k]}</b></span>`).join("");
  return h + `</div></div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ---------------- reset ---------------- */
el("resetBtn").addEventListener("click", () => {
  selectedFiles = [];
  fileInput.value = "";
  folderInput.value = "";
  fileSummary.classList.add("hidden");
  resetPreview(true);
  el("resultCard").classList.add("hidden");
  el("errorBox").classList.add("hidden");
  el("progress").classList.add("hidden");
  el("barFill").style.width = "0%";
  lockUI(false);
  convertBtn.disabled = true;
  convertBtn.textContent = "Konversi";
  window.scrollTo({ top: 0, behavior: "smooth" });
});

/* Mode "Berbasis folder" hanya masuk akal kalau ada struktur folder, tapi
   tetap diizinkan — backend akan fallback ke default bila tak ada subfolder. */
