# Konverter Font → Dataset (Web UI Lokal)

Alat untuk mengubah file font (**OTF / TTF / TTC**) menjadi dataset `.jsonl`
siap fine-tuning model glyph — lewat antarmuka web sederhana di browser.
**Semua diproses 100% lokal** di komputermu (`localhost`). Tidak ada font atau
data yang dikirim ke server eksternal mana pun.

Aplikasi ini membungkus skrip CLI yang sudah teruji,
[`font_to_dataset.py`](./font_to_dataset.py) — logika ekstraksi glyph dan
tagging-nya **dipakai ulang apa adanya**, jadi dataset yang dihasilkan tetap
kompatibel dengan pipeline training yang sudah ada.

---

## Fitur

- **Upload jumbo** — drag-and-drop, pilih banyak file, atau seret/pilih satu
  folder utuh (`webkitdirectory`) agar struktur folder terjaga.
- **Pratinjau tag (live)** — sebelum memproses ribuan glyph, lihat dulu tag yang
  akan dihasilkan dari beberapa font sampel. Tabel ikut berubah otomatis saat
  opsi diubah. Tombol "Konversi Semua" baru aktif setelah pratinjau tampil.
- **Kategori dipandu** — dropdown preset (mis. `high-contrast serif display`,
  `sans-serif`, `monospace`, …) + opsi kustom. Mode "Kategori default" kini
  **memaksa kategori seragam** untuk seluruh batch (lewat jalur CSV-override
  prioritas tertinggi), jadi tidak lagi terganggu metadata PANOSE per font.
- **Peringatan otomatis** — banner muncul bila kategori belum dipilih atau bila
  deteksi otomatis menghasilkan label tidak konsisten untuk batch satu-gaya.
- **Cek konsistensi** — ringkasan menampilkan "Deskriptor kategori unik: N"
  (idealnya 1 untuk batch homogen).
- **3 mode tagging:**
  - **Kategori default** — satu kategori untuk seluruh batch (paling praktis
    untuk batch homogen, mis. `high-contrast serif display`).
  - **Berbasis folder** — nama subfolder jadi kategori (`Serif/`, `Sans/`, …).
  - **Otomatis** — deteksi dari PANOSE + petunjuk nama font (tidak selalu akurat).
  - Opsi **estimasi kontras dari bentuk** (`--detect-contrast`).
  - Opsi lanjutan: daftar karakter, `val-split`, `max-path-len`.
- **Progress real-time** via Server-Sent Events (progress bar + "Memproses X
  dari Y font" + nama file berjalan).
- **Tahan banting** — font rusak dilewati & dicatat, proses tidak berhenti.
- **Hemat memori** — record ditulis ke disk secara streaming (aman untuk ribuan font).
- **Hasil & download** — ringkasan lengkap + unduh `dataset_train.jsonl`,
  `dataset_val.jsonl`, `summary.txt`, atau semuanya dalam satu `.zip`.

---

## Cara Install

Butuh **Python 3.8+**.

```bash
# (disarankan) buat virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# pasang dependency
pip install -r requirements.txt
```

## Cara Menjalankan

```bash
python3 app.py
```

Lalu buka di browser:

```
http://localhost:5000
```

Langkah pakai:

1. **Pilih font** — tarik-lepas, *Pilih File*, atau *Pilih Folder*.
2. **Pilih mode tagging** + opsi (default category / folder / otomatis).
3. Klik **Konversi** dan tunggu progress bar.
4. **Unduh** dataset hasilnya.

Tekan `CTRL+C` di terminal untuk menghentikan server.

---

## Format Output (kompatibel pipeline)

Tiap baris `.jsonl` adalah satu record chat:

```json
{"messages": [
  {"role": "system", "content": "You are a specialized vector glyph designer..."},
  {"role": "user", "content": "Font design requirements: {tag}\nText content: {char}"},
  {"role": "assistant", "content": "<path d=\"...\"/>"}
]}
```

Detail ekstraksi (tidak diubah, sudah teruji):

- Glyph diekstrak via `fontTools` → `getBestCmap`, `getGlyphSet`, `SVGPathPen`.
- Dinormalkan ke **UPM 1000** (`scale = 1000 / unitsPerEm`), koordinat
  **font-native y-naik** (tanpa flip-Y), dibulatkan ke integer.
- Glyph kosong (path < 4) dan karakter di luar cmap dilewati.
- Format tag: `"{Family} style, {kategori}, {berat} weight, {italic|normal} style"`.
- Mendukung `.ttc` (font collection berisi banyak font dalam satu file).

---

## Tetap Bisa Lewat Terminal

Skrip aslinya tetap berfungsi penuh sebagai CLI:

```bash
# Batch homogen
python3 font_to_dataset.py --input ./fonts --output ds.jsonl \
        --default-category "high-contrast serif display"

# Tag per folder
python3 font_to_dataset.py --input ./fonts --output ds.jsonl --use-folder-tag

# Lihat semua opsi
python3 font_to_dataset.py --help
```

---

## Catatan

- File font mentah yang diunggah disimpan sementara di folder `jobs/<id>/uploads`
  dan **dihapus otomatis** setelah konversi selesai. Hasil dataset disimpan di
  `jobs/<id>/out` agar bisa diunduh ulang. Folder `jobs/` di-*ignore* oleh git.
- Server hanya mendengarkan di `127.0.0.1` (localhost) — tidak terekspos ke jaringan.
- Sepenuhnya offline: tidak ada CDN atau dependensi jaringan eksternal.
