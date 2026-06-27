#!/usr/bin/env python3
"""
app.py — Web UI LOKAL untuk konversi Font -> Dataset .jsonl
==========================================================
Membungkus `font_to_dataset.py` (logika ekstraksi + tagging yang sudah teruji)
jadi aplikasi web satu halaman yang jalan di komputer sendiri (localhost).
TIDAK ada cloud, TIDAK ada data yang keluar.

Jalankan:  python3 app.py    lalu buka  http://localhost:5000

Catatan: semua logika ekstraksi/format output DIPAKAI ULANG dari
font_to_dataset.py (process_font, classify, extract_glyph, to_record, ...).
File ini hanya menambahkan: penerimaan upload, orkestrasi proses + progress
real-time (SSE), penulisan streaming ke disk, dan endpoint download.
"""
import io
import json
import os
import queue
import random
import shutil
import threading
import time
import uuid
import zipfile
from collections import Counter

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, stream_with_context)
from werkzeug.utils import secure_filename

import font_to_dataset as fd

app = Flask(__name__)
# Tool lokal, satu pengguna — izinkan upload besar (ribuan font). Tak ada batas.
app.config["MAX_CONTENT_LENGTH"] = None

ALLOWED_EXT = (".otf", ".ttf", ".ttc")
WORK_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
os.makedirs(WORK_ROOT, exist_ok=True)

# Registry job dalam memori (cukup untuk pemakaian lokal satu pengguna).
JOBS = {}
PREVIEWS = {}            # preview_id -> {samples:[{name,rel,path}], root_name}
JOBS_LOCK = threading.Lock()


def _job_dir(job_id):
    return os.path.join(WORK_ROOT, job_id)


def _allowed(name):
    return name.lower().endswith(ALLOWED_EXT)


def _build_fopts(mode, category, folder_tag, detect_contrast):
    """Bangun opsi tagging yang dipakai SAMA persis oleh preview & konversi.

    Mode 'Kategori default' dialirkan lewat jalur CSV override (pola "" cocok ke
    semua nama file) — di classify() itu prioritas TERTINGGI, jadi kategori
    pilihan pengguna memaksa SELURUH batch jadi seragam (mengalahkan PANOSE/
    petunjuk nama). Mode 'folder' memakai nama subfolder (prioritas di atas
    PANOSE). Mode 'auto' membiarkan PANOSE + petunjuk nama bekerja.
    Logika classify() sendiri TIDAK diubah."""
    csv = [("", category, None)] if (mode == "default" and category) else []
    return {"csv": csv,
            "folder_tag": folder_tag if mode == "folder" else None,
            "default_cat": None,
            "detect_contrast": detect_contrast}


def _safe_rel(rel, fallback):
    rel = (rel or "").replace("\\", "/").lstrip("/")
    parts = [secure_filename(p) for p in rel.split("/") if p not in ("", ".", "..")]
    parts = [p for p in parts if p] or [secure_filename(fallback)]
    return "/".join(parts), parts


# --------------------------------------------------------------------------- #
#  UPLOAD                                                                      #
# --------------------------------------------------------------------------- #
@app.route("/api/upload", methods=["POST"])
def upload():
    """Terima banyak file (atau satu folder via webkitdirectory).

    Simpan file font ke direktori kerja per-job sambil mempertahankan path
    relatif (penting untuk mode tag berbasis folder). Tolak ekstensi lain.
    """
    files = request.files.getlist("files")
    rel_paths = request.form.getlist("paths")  # sejajar dgn `files` (urut sama)
    if not files:
        return jsonify(error="Tidak ada file yang diunggah."), 400

    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)
    up_dir = os.path.join(jdir, "uploads")
    os.makedirs(up_dir, exist_ok=True)

    accepted, rejected, root_names = [], [], set()
    for idx, fs in enumerate(files):
        rel = rel_paths[idx] if idx < len(rel_paths) and rel_paths[idx] else fs.filename
        base = os.path.basename((rel or "").replace("\\", "/").lstrip("/"))
        if not base or not _allowed(base):
            if base:
                rejected.append(base)
            continue

        # Bersihkan tiap komponen path tapi pertahankan struktur folder.
        safe_rel, parts = _safe_rel(rel, base)
        if len(parts) >= 2:
            root_names.add(parts[0])

        dst = os.path.join(up_dir, safe_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        fs.save(dst)
        accepted.append({"name": base, "rel": safe_rel, "path": dst})

    if not accepted:
        shutil.rmtree(jdir, ignore_errors=True)
        return jsonify(
            error="Tidak ada file font yang valid (.otf/.ttf/.ttc).",
            rejected=rejected,
        ), 400

    # Kalau seluruh batch berasal dari satu folder root, catat namanya supaya
    # folder root tidak salah dianggap kategori.
    root_name = next(iter(root_names)) if len(root_names) == 1 else None

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "files": accepted,
            "root_name": root_name,
            "state": "uploaded",
            "done": 0,
            "total": len(accepted),
            "current": "",
            "events": queue.Queue(),
            "result": None,
            "error": None,
        }

    return jsonify(
        job_id=job_id,
        accepted=len(accepted),
        rejected=rejected,
        names=[a["name"] for a in accepted[:50]],
    )


# --------------------------------------------------------------------------- #
#  PREVIEW TAG (sampel font, sebelum konversi penuh)                           #
# --------------------------------------------------------------------------- #
@app.route("/api/preview_upload", methods=["POST"])
def preview_upload():
    """Unggah HANYA font sampel (5–10) untuk preview tag cepat. Terpisah dari
    upload penuh supaya preview ringan & bisa live saat opsi diubah."""
    files = request.files.getlist("files")
    rel_paths = request.form.getlist("paths")
    root_name = (request.form.get("root_name") or "").strip() or None
    if not files:
        return jsonify(error="Tidak ada sampel."), 400

    preview_id = uuid.uuid4().hex
    pdir = os.path.join(_job_dir("preview_" + preview_id), "sample")
    os.makedirs(pdir, exist_ok=True)

    samples = []
    for idx, fs in enumerate(files):
        rel = rel_paths[idx] if idx < len(rel_paths) and rel_paths[idx] else fs.filename
        base = os.path.basename((rel or "").replace("\\", "/").lstrip("/"))
        if not base or not _allowed(base):
            continue
        safe_rel, _ = _safe_rel(rel, base)
        dst = os.path.join(pdir, secure_filename(base))
        fs.save(dst)
        samples.append({"name": base, "rel": safe_rel, "path": dst})

    if not samples:
        return jsonify(error="Sampel tidak valid."), 400

    with JOBS_LOCK:
        PREVIEWS[preview_id] = {"samples": samples, "root_name": root_name,
                                "dir": _job_dir("preview_" + preview_id)}
    return jsonify(preview_id=preview_id, count=len(samples))


@app.route("/api/preview", methods=["POST"])
def preview():
    """Hitung tag untuk sampel dengan opsi saat ini. Live: dipanggil ulang
    tiap kali pengguna mengubah mode/kategori/kontras (tanpa unggah ulang)."""
    data = request.get_json(force=True, silent=True) or {}
    with JOBS_LOCK:
        pv = PREVIEWS.get(data.get("preview_id"))
    if not pv:
        return jsonify(error="Sesi preview tidak ditemukan. Pilih file lagi."), 404

    mode = data.get("mode", "auto")
    default_category = (data.get("default_category") or "").strip() or None
    detect_contrast = bool(data.get("detect_contrast"))

    rows, descs = [], Counter()
    for s in pv["samples"]:
        folder_tag = fd.folder_tag_for(s["rel"], pv["root_name"]) if mode == "folder" else None
        fopts = _build_fopts(mode, default_category, folder_tag, detect_contrast)
        info = fd.preview_tag(s["path"], fopts, folder_tag=fopts["folder_tag"])
        if info is None:
            rows.append({"name": s["name"], "rel": s["rel"], "tag": None,
                         "desc": None, "source": "unreadable"})
            continue
        descs[info["desc"]] += 1
        rows.append({"name": s["name"], "rel": s["rel"], "tag": info["tag"],
                     "desc": info["desc"], "category": info["category"],
                     "source": info["source"]})

    readable = [r for r in rows if r["tag"]]
    unique_desc = dict(descs.most_common())
    # Apakah ada yang jatuh ke default generik 'serif'?
    generic = any(r.get("source") == "default" and r.get("category") == "serif"
                  for r in readable)
    no_category = mode == "default" and not default_category

    return jsonify(rows=rows, unique_desc=unique_desc,
                   n_unique_desc=len(unique_desc), generic_serif=generic,
                   no_category=no_category, mode=mode, detect_contrast=detect_contrast)


# --------------------------------------------------------------------------- #
#  KONVERSI (jalan di thread latar + progress via SSE)                         #
# --------------------------------------------------------------------------- #
@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job tidak ditemukan."), 404
    if job["state"] in ("processing", "done"):
        return jsonify(error="Job sudah diproses."), 400

    chars = data.get("chars") or fd.DEFAULT_CHARS
    # Buang duplikat & spasi tapi pertahankan urutan.
    seen = set()
    chars = "".join(c for c in chars if not (c in seen or seen.add(c)) and not c.isspace())
    if not chars:
        chars = fd.DEFAULT_CHARS

    opts = {
        "mode": data.get("mode", "auto"),               # default | folder | auto
        "default_category": (data.get("default_category") or "").strip() or None,
        "detect_contrast": bool(data.get("detect_contrast")),
        "chars": chars,
        "max_path_len": int(data.get("max_path_len") or 1600),
        "val_split": float(data.get("val_split") if data.get("val_split") is not None else 0.05),
        "seed": int(data.get("seed") or 42),
    }
    # Batasi nilai biar aman.
    opts["max_path_len"] = max(100, min(opts["max_path_len"], 100000))
    opts["val_split"] = min(max(opts["val_split"], 0.0), 0.9)

    job["state"] = "processing"
    t = threading.Thread(target=_run_job, args=(job, opts), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


def _emit(job, **payload):
    job["events"].put(payload)


def _run_job(job, opts):
    """Worker: proses tiap font, tulis record ke disk secara streaming."""
    jdir = _job_dir(job["id"])
    out_dir = os.path.join(jdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    combined = os.path.join(out_dir, "combined.jsonl")

    stats = fd.new_stats()
    total = len(job["files"])
    n_records = 0

    try:
        with open(combined, "w", encoding="utf-8") as cf:
            for i, finfo in enumerate(job["files"], 1):
                job["done"] = i - 1
                job["current"] = finfo["name"]
                _emit(job, type="progress", done=i - 1, total=total,
                      current=finfo["name"])

                folder_tag = None
                if opts["mode"] == "folder":
                    folder_tag = fd.folder_tag_for(finfo["rel"], job.get("root_name"))

                fopts = _build_fopts(opts["mode"], opts["default_category"],
                                     folder_tag, opts["detect_contrast"])

                try:
                    recs = fd.process_font(finfo["path"], opts["chars"],
                                           opts["max_path_len"], fopts, stats)
                    for tag, ch, p in recs:
                        cf.write(json.dumps(fd.to_record(tag, ch, p),
                                            ensure_ascii=False) + "\n")
                        n_records += 1
                except Exception as e:  # error per-file: lewati & catat
                    stats["skipped"].append((finfo["name"], f"error: {e}"))

                job["done"] = i
                _emit(job, type="progress", done=i, total=total,
                      current=finfo["name"], ok=stats["ok"], glyphs=n_records)

        # ---- split train/val secara streaming (hemat memori) ----
        base = os.path.join(out_dir, "dataset")
        n_train, n_val = _split_streaming(
            combined, base, n_records, opts["val_split"], opts["seed"])

        summary = _build_summary(stats, n_records, n_train, n_val, opts)
        with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(summary["text"])

        # ---- bundel zip ----
        zip_path = os.path.join(out_dir, "dataset_bundle.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(f"{base}_train.jsonl", "dataset_train.jsonl")
            if n_val > 0:
                z.write(f"{base}_val.jsonl", "dataset_val.jsonl")
            z.write(os.path.join(out_dir, "summary.txt"), "summary.txt")

        try:
            os.remove(combined)  # tak diperlukan lagi
        except OSError:
            pass

        job["result"] = summary
        job["state"] = "done"
        _emit(job, type="done", result=summary)
    except Exception as e:
        job["error"] = str(e)
        job["state"] = "error"
        _emit(job, type="error", message=str(e))


def _split_streaming(combined, base, n_records, val_split, seed):
    """Bagi file gabungan jadi train/val tanpa menahan semua record di memori.

    Hanya himpunan indeks val (integer) yang disimpan — bukan teks record-nya.
    Setara dengan shuffle+split di skrip asli: isi dataset identik, hanya
    urutan dalam file yang berbeda (tak berpengaruh untuk training).
    """
    train_path = f"{base}_train.jsonl"
    val_path = f"{base}_val.jsonl"

    nv = max(1, int(n_records * val_split)) if val_split > 0 and n_records > 0 else 0
    nv = min(nv, n_records)
    val_set = set()
    if nv > 0:
        idx = list(range(n_records))
        random.seed(seed)
        random.shuffle(idx)
        val_set = set(idx[:nv])

    vf = open(val_path, "w", encoding="utf-8") if nv > 0 else None
    try:
        with open(combined, "r", encoding="utf-8") as cf, \
                open(train_path, "w", encoding="utf-8") as tf:
            for i, line in enumerate(cf):
                if i in val_set:
                    vf.write(line)
                else:
                    tf.write(line)
    finally:
        if vf:
            vf.close()
    if nv == 0 and os.path.exists(val_path):
        os.remove(val_path)
    return n_records - nv, nv


def _build_summary(stats, n_records, n_train, n_val, opts):
    cat = dict(stats["cat"].most_common())
    wt = dict(stats["wt"].most_common())
    src = dict(stats["src"].most_common())
    desc = dict(stats["desc"].most_common())
    skipped = [{"name": nm, "reason": why} for nm, why in stats["skipped"]]

    sample, seen = [], set()
    for fam, tag in stats["sample"]:
        if fam not in seen:
            seen.add(fam)
            sample.append(tag)
        if len(sample) >= 10:
            break

    lines = []
    lines.append("=" * 60)
    lines.append("RINGKASAN KONVERSI FONT -> DATASET")
    lines.append("=" * 60)
    lines.append(f"Font OK            : {stats['ok']}")
    lines.append(f"Total glyph        : {n_records} (train {n_train} | val {n_val})")
    lines.append(f"Glyph dilewati     : {stats['too_long']} (path terlalu panjang)")
    lines.append(f"Karakter           : {opts['chars']}")
    lines.append(f"val-split          : {opts['val_split']}  |  max-path-len: {opts['max_path_len']}")
    lines.append("")
    lines.append(f"Deskriptor kategori unik: {len(desc)}"
                 + ("  (konsisten ✓)" if len(desc) == 1 else "  (campur)"))
    for d, n in desc.items():
        lines.append(f"   {str(d):28s} {n}")
    lines.append("")
    lines.append("KATEGORI:")
    for c, n in cat.items():
        lines.append(f"   {str(c):28s} {n}")
    lines.append("")
    lines.append("BERAT:")
    for w, n in wt.items():
        lines.append(f"   {str(w):28s} {n}")
    lines.append("")
    lines.append("Sumber tag (akurasi): " + ", ".join(f"{k}={v}" for k, v in src.items()))
    if skipped:
        lines.append("")
        lines.append(f"Font bermasalah ({len(skipped)}):")
        for s in skipped[:30]:
            lines.append(f"   {s['name']}: {s['reason']}")
    lines.append("")
    lines.append("Contoh tag:")
    for t in sample:
        lines.append(f"   {t}")
    lines.append("=" * 60)
    text = "\n".join(lines) + "\n"

    return {
        "ok": stats["ok"],
        "glyphs": n_records,
        "train": n_train,
        "val": n_val,
        "too_long": stats["too_long"],
        "categories": cat,
        "weights": wt,
        "sources": src,
        "descriptors": desc,
        "n_unique_desc": len(desc),
        "skipped": skipped,
        "sample_tags": sample,
        "has_val": n_val > 0,
        "text": text,
    }


# --------------------------------------------------------------------------- #
#  PROGRESS (Server-Sent Events)                                              #
# --------------------------------------------------------------------------- #
@app.route("/api/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job tidak ditemukan."), 404

    @stream_with_context
    def gen():
        # Kirim snapshot awal supaya UI langsung sinkron.
        yield _sse({"type": "progress", "done": job["done"],
                    "total": job["total"], "current": job["current"]})
        q = job["events"]
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"  # cegah timeout proxy/browser
                if job["state"] in ("done", "error"):
                    break
                continue
            yield _sse(ev)
            if ev.get("type") in ("done", "error"):
                break

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
#  DOWNLOAD                                                                    #
# --------------------------------------------------------------------------- #
FILE_MAP = {
    "train": ("dataset_train.jsonl", "dataset_train.jsonl"),
    "val": ("dataset_val.jsonl", "dataset_val.jsonl"),
    "summary": ("summary.txt", "summary.txt"),
    "zip": ("dataset_bundle.zip", "dataset_bundle.zip"),
}


@app.route("/api/download/<job_id>/<kind>")
def download(job_id, kind):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["state"] != "done":
        return jsonify(error="Hasil belum siap."), 404
    if kind not in FILE_MAP:
        return jsonify(error="Jenis file tidak dikenal."), 404
    fname, dlname = FILE_MAP[kind]
    path = os.path.join(_job_dir(job_id), "out", fname)
    if not os.path.exists(path):
        return jsonify(error="File tidak tersedia."), 404
    return send_file(path, as_attachment=True, download_name=dlname)


# --------------------------------------------------------------------------- #
#  KEBERSIHAN                                                                  #
# --------------------------------------------------------------------------- #
@app.route("/api/cleanup/<job_id>", methods=["POST"])
def cleanup(job_id):
    """Hapus file upload (font mentah) setelah konversi untuk hemat disk;
    hasil dataset tetap disimpan agar bisa di-download ulang."""
    up_dir = os.path.join(_job_dir(job_id), "uploads")
    shutil.rmtree(up_dir, ignore_errors=True)
    return jsonify(ok=True)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    import webbrowser

    # Port 8000 supaya tidak bentrok dengan AirPlay Receiver macOS (yang
    # memakai port 5000). Bisa diganti lewat env var PORT bila perlu.
    port = int(os.environ.get("PORT", "8000"))
    url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print(" Konverter Font -> Dataset  (LOKAL)")
    print(f" Buka:  {url}")
    print(" Browser akan terbuka otomatis sebentar lagi.")
    print(" Tekan CTRL+C untuk berhenti.")
    print("=" * 60)

    # Buka browser otomatis sesaat setelah server siap melayani.
    threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
