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
    convertBtn.disabled = true;
    if (rejected > 0) {
      fileSummary.classList.remove("hidden");
      fileSummary.innerHTML = `<span class="badge">Ditolak</span> ${rejected} file bukan font (.otf/.ttf/.ttc) diabaikan.`;
    }
    return;
  }
  fileSummary.classList.remove("hidden");
  convertBtn.disabled = false;
  let html = `<strong>${selectedFiles.length}</strong> file font siap dikonversi.`;
  if (rejected > 0) html += ` <span class="badge">${rejected} ditolak</span>`;
  const names = selectedFiles.slice(0, 6).map((f) => f.file.name).join(", ");
  html += `<div class="muted small" style="margin-top:6px">${names}${
    selectedFiles.length > 6 ? ` … (+${selectedFiles.length - 6} lagi)` : ""
  }</div>`;
  fileSummary.innerHTML = html;
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
  setFiles(filesFromInput(folderInput));
  // Kalau pengguna pilih folder, mode "Berbasis folder" paling masuk akal.
  if (selectedFiles.length) document.querySelector('input[name=mode][value=folder]').checked = true;
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

function collectOptions(jobId) {
  return {
    job_id: jobId,
    mode: document.querySelector("input[name=mode]:checked").value,
    default_category: el("defaultCategory").value,
    detect_contrast: el("detectContrast").checked,
    chars: el("chars").value,
    val_split: parseFloat(el("valSplit").value),
    max_path_len: parseInt(el("maxPathLen").value, 10),
  };
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
