#!/usr/bin/env python3
"""
font_to_dataset.py — Konversi folder font (OTF/TTF/TTC) -> dataset .jsonl
========================================================================
Alat batch penyiap data pelatihan model glyph. Jalan di CPU (Mac/PC),
TIDAK butuh GPU.

Dua tugas:
  1. EKSTRAK glyph -> SVG path (dinormalkan ke UPM 1000, koordinat y-naik).
  2. TAG gaya tiap font, dengan AKURASI di tangan kamu.

=== FILOSOFI TAGGING (penting) ===
Klasifikasi otomatis penuh dari font sembarang itu TIDAK andal (banyak font,
termasuk buatan foundry, mengosongkan metadata PANOSE). Jadi akurasi tag
mengikuti prioritas ini (atas = paling akurat):

  1. --tags-csv FILE     : kamu tentukan kategori per pola nama file. PALING akurat.
  2. --use-folder-tag    : nama subfolder = kategori (atur fonts/Serif/, Sans/, dst).
  3. PANOSE (jika diisi) : klasifikasi resmi di dalam font.
  4. Petunjuk nama font  : "sans"/"script"/"slab"/dll. di nama.
  5. --default-category  : default untuk SATU batch homogen (mis. semua serif).
  6. "serif"             : default terakhir.

Berat/lebar/italic SELALU dideteksi otomatis dari OS/2 (andal).
Kontras (untuk Didone) bisa diestimasi dari bentuk dengan --detect-contrast
(bantuan, tidak sempurna) — atau tulis sendiri di --default-category.

=== CONTOH ===
# Batch homogen (semua serif kontras-tinggi) — paling praktis utk Sensatype:
python3 font_to_dataset.py --input ./fonts --output ds.jsonl \\
        --default-category "high-contrast serif display"

# Campur gaya, diatur per folder:
python3 font_to_dataset.py --input ./fonts --output ds.jsonl --use-folder-tag

# Kontrol presisi via CSV (pola_nama,kategori[,kontras]):
python3 font_to_dataset.py --input ./fonts --output ds.jsonl --tags-csv tags.csv

=== CATATAN REFAKTOR ===
Logika ekstraksi + tagging di bawah ini SENGAJA dipisah dari argparse/main()
supaya bisa diimpor ulang oleh aplikasi web lokal (app.py). Konvensi
ekstraksi & format output TIDAK diubah agar tetap kompatibel dengan pipeline
training yang sudah ada.
"""
import argparse, json, os, re, sys, glob, random
from collections import Counter
from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.ttCollection import TTCollection
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

SYSTEM_PROMPT = (
    "You are a specialized vector glyph designer creating SVG path elements.\n\n"
    "CRITICAL REQUIREMENTS:\n- Each glyph must be a complete, self-contained "
    "element, in reading order of the given text.\n- Terminate each element "
    "with a newline character\n- Output ONLY valid SVG elements"
)
DEFAULT_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 "abcdefghijklmnopqrstuvwxyz0123456789")
WEIGHT_MAP = [(150,"thin"),(250,"extralight"),(350,"light"),(450,"regular"),
              (550,"medium"),(650,"semibold"),(750,"bold"),(850,"extrabold"),(1000,"black")]
WIDTH_MAP = {1:"ultra-condensed",2:"extra-condensed",3:"condensed",4:"semi-condensed",
             5:"normal",6:"semi-expanded",7:"expanded",8:"extra-expanded",9:"ultra-expanded"}
PAN_CONTRAST = {2:"no-contrast",3:"low-contrast",4:"low-contrast",5:"low-contrast",
                6:"medium-contrast",7:"high-contrast",8:"high-contrast",9:"high-contrast"}
NAME_HINTS = [("mono","monospace"),("slab","slab-serif"),("script","script"),
              ("hand","handwriting"),("brush","handwriting"),("calligraph","calligraphy"),
              ("gothic","sans-serif"),("grotesk","sans-serif"),("sans","sans-serif"),
              ("serif","serif"),("display","display")]


def weight_name(c):
    if not c: return "regular"
    for thr,n in WEIGHT_MAP:
        if c < thr: return n
    return "black"


# ---------- estimasi kontras dari bentuk (opsional, bantuan) ----------
def estimate_contrast(path, size=240):
    try:
        from PIL import ImageFont, Image, ImageDraw
        font = ImageFont.truetype(path, size)
    except Exception:
        return None
    img = Image.new("L", (size*2, size*2), 0)
    ImageDraw.Draw(img).text((size//2, size//4), "O", font=font, fill=255)
    bb = img.getbbox()
    if not bb: return None
    O = img.crop(bb); w, h = O.size
    floor = max(2, int(0.02*max(w, h)))
    def hruns(y):
        out=[]; r=0
        for x in range(w):
            if O.getpixel((x,y))>128: r+=1
            elif r: out.append(r); r=0
        if r: out.append(r)
        return [v for v in out if v>=floor]
    def vruns(x):
        out=[]; r=0
        for y in range(h):
            if O.getpixel((x,y))>128: r+=1
            elif r: out.append(r); r=0
        if r: out.append(r)
        return [v for v in out if v>=floor]
    V = hruns(int(h*0.5)); H = vruns(int(w*0.5))
    if not V or not H: return None
    ratio = min(V)/min(H)
    if ratio >= 3.5: return "high-contrast"
    if ratio <= 1.5: return "low-contrast"
    return None   # ambigu -> jangan klaim


# ---------- CSV override ----------
def load_csv(path):
    rows = []
    if not path: return rows
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = [p.strip() for p in line.split(",")]
        pat = parts[0].lower()
        cat = parts[1] if len(parts) > 1 and parts[1] else None
        con = parts[2] if len(parts) > 2 and parts[2] else None
        rows.append((pat, cat, con))
    return rows


def classify(font, family, subfamily, fname, opts, contrast_est):
    os2 = font.get("OS/2"); post = font.get("post")
    blob = f"{family} {subfamily} {fname}".lower()
    attr = {"weight":"regular","width":"normal","italic":False,
            "category":None,"contrast":None,"source":"default"}

    # berat
    if os2 is not None and getattr(os2,"usWeightClass",None):
        attr["weight"] = weight_name(os2.usWeightClass)
    else:
        for w in ["thin","extralight","light","medium","semibold","extrabold","black","bold"]:
            if w in blob: attr["weight"]=w; break
    # lebar
    if os2 is not None and getattr(os2,"usWidthClass",None) in WIDTH_MAP:
        attr["width"] = WIDTH_MAP[os2.usWidthClass]
    # italic
    it = False
    if os2 is not None and (os2.fsSelection & 0x01): it=True
    if post is not None and getattr(post,"italicAngle",0): it=True
    if "italic" in blob or "oblique" in blob: it=True
    attr["italic"]=it

    cat=None; con=None; src="default"
    # 1) CSV override
    for pat,c,k in opts["csv"]:
        if pat in fname.lower():
            cat=c or cat; con=k or con
            if c: src="csv"
            break
    # 2) folder
    if cat is None and opts["folder_tag"]:
        cat=opts["folder_tag"]; src="folder"
    # 3) PANOSE
    if cat is None and os2 is not None:
        pan=getattr(os2,"panose",None)
        if pan is not None and pan.bFamilyType:
            ft,sf,pr,ct = pan.bFamilyType,pan.bSerifStyle,pan.bProportion,pan.bContrast
            if pr==9: cat="monospace"
            elif ft==3: cat="handwriting"
            elif ft==4: cat="decorative display"
            elif ft==2:
                cat = "serif" if 2<=sf<=10 else ("sans-serif" if 11<=sf<=15 else None)
            if cat: src="panose"
            if ct in PAN_CONTRAST and con is None: con=PAN_CONTRAST[ct]
    # 4) nama
    if cat is None:
        for kw,c in NAME_HINTS:
            if kw in blob: cat=c; src="name"; break
    # 5) default-category
    if cat is None and opts["default_cat"]:
        cat=opts["default_cat"]; src="default-cat"
    # 6) terakhir
    if cat is None:
        cat="serif"; src="default"

    # kontras: csv/panose > estimasi bentuk (jika diminta) > none
    if con is None and contrast_est and "contrast" not in cat:
        con=contrast_est
    attr["category"]=cat; attr["contrast"]=con; attr["source"]=src

    # rakit tag
    parts=[f"{family} style"]
    desc=cat
    if con and "contrast" not in cat and ("serif" in cat or "display" in cat):
        desc=f"{con} {cat}"
    parts.append(desc)
    if attr["width"]!="normal": parts.append(f"{attr['width']} width")
    parts.append(f"{attr['weight']} weight")
    parts.append("italic style" if it else "normal style")
    attr["desc"]=desc   # deskriptor kategori (untuk cek konsistensi tag)
    return ", ".join(parts), attr


def round_path(d):
    return re.sub(r"-?\d+\.?\d*", lambda m: str(int(round(float(m.group())))), d)


def extract_glyph(glyph_set, cmap, ch, scale):
    code=ord(ch)
    if code not in cmap: return None
    g=cmap[code]
    if g not in glyph_set: return None
    pen=SVGPathPen(glyph_set)
    tgt=pen if scale==1.0 else TransformPen(pen,(scale,0,0,scale,0,0))
    try: glyph_set[g].draw(tgt)
    except Exception: return None
    d=pen.getCommands()
    if not d or len(d)<4: return None
    return round_path(d)


def iter_fonts(path):
    if path.lower().endswith(".ttc"):
        try:
            for f in TTCollection(path, lazy=True).fonts: yield f
        except Exception: return
    else:
        try: yield TTFont(path, lazy=True, fontNumber=0)
        except Exception: return


def get_names(font):
    try:
        nm=font["name"]; fam=nm.getBestFamilyName() or "Unknown"; sub=nm.getBestSubFamilyName() or ""
    except Exception: fam,sub="Unknown",""
    return re.sub(r"\s+"," ",fam).strip(), sub


def process_font(path, chars, max_len, opts, stats):
    out=[]
    contrast_est = estimate_contrast(path) if opts["detect_contrast"] else None
    fname=os.path.basename(path)
    for font in iter_fonts(path):
        try:
            family,subfamily=get_names(font)
            upm=font["head"].unitsPerEm or 1000
            cmap=font.getBestCmap(); gs=font.getGlyphSet()
        except Exception as e:
            stats["skipped"].append((fname,f"baca gagal: {e}")); continue
        tag,attr=classify(font,family,subfamily,fname,opts,contrast_est)
        n=0
        for ch in chars:
            d=extract_glyph(gs,cmap,ch,1000.0/upm)
            if d is None: continue
            if len(d)>max_len: stats["too_long"]+=1; continue
            out.append((tag,ch,f'<path d="{d}"/>')); n+=1
        if n<8: stats["skipped"].append((fname,f"cuma {n} glyph"))
        stats["cat"][attr["category"]]+=1; stats["wt"][attr["weight"]]+=1
        stats["src"][attr["source"]]+=1; stats["desc"][attr["desc"]]+=1; stats["ok"]+=1
        stats["sample"].append((family,tag))
    return out


def preview_tag(path, opts, folder_tag=None):
    """Hitung tag yang AKAN dihasilkan untuk sebuah font tanpa mengekstrak
    glyph (cepat, untuk preview di web). Memakai classify() yang sama persis
    seperti konversi penuh, jadi hasilnya identik.

    Mengembalikan dict {family, tag, desc, category, source} atau None bila
    font tak bisa dibaca.
    """
    fonts = list(iter_fonts(path))
    if not fonts:
        return None
    font = fonts[0]
    contrast_est = estimate_contrast(path) if opts.get("detect_contrast") else None
    fname = os.path.basename(path)
    try:
        family, subfamily = get_names(font)
    except Exception:
        family, subfamily = "Unknown", ""
    fopts = {"csv": opts.get("csv", []), "folder_tag": folder_tag,
             "default_cat": opts.get("default_cat"),
             "detect_contrast": opts.get("detect_contrast", False)}
    try:
        tag, attr = classify(font, family, subfamily, fname, fopts, contrast_est)
    except Exception:
        return None
    return {"family": family, "tag": tag, "desc": attr.get("desc"),
            "category": attr["category"], "source": attr["source"]}


def to_record(style,ch,path):
    return {"messages":[
        {"role":"system","content":SYSTEM_PROMPT},
        {"role":"user","content":f"Font design requirements: {style}\nText content: {ch}"},
        {"role":"assistant","content":path}]}


def new_stats():
    """Wadah statistik kosong — dipakai CLI maupun app.py."""
    return {"ok":0,"too_long":0,"skipped":[],"cat":Counter(),"wt":Counter(),
            "src":Counter(),"desc":Counter(),"sample":[]}


def folder_tag_for(rel_path, root_name=None):
    """Tentukan tag folder dari path relatif (dipakai mode 'Berbasis folder' di web).

    Mengikuti perilaku CLI --use-folder-tag: kategori = nama subfolder
    pembungkus langsung font, KECUALI kalau itu folder root yang dipilih.
    Contoh: 'Fonts/Serif/Foo.ttf' -> 'serif'; 'Fonts/Foo.ttf' -> None.
    """
    rel_path = rel_path.replace("\\", "/")
    parts = [p for p in rel_path.split("/") if p]
    if len(parts) < 3:
        return None
    parent = parts[-2]
    if root_name is not None and parent == root_name:
        return None
    return parent.lower()


def main():
    ap=argparse.ArgumentParser(description="Konversi font -> dataset .jsonl")
    ap.add_argument("--input",required=True)
    ap.add_argument("--output",default="dataset.jsonl")
    ap.add_argument("--chars",default=DEFAULT_CHARS)
    ap.add_argument("--max-path-len",type=int,default=1600)
    ap.add_argument("--val-split",type=float,default=0.05)
    ap.add_argument("--default-category",default=None,
                    help="kategori default utk batch homogen, mis. 'high-contrast serif display'")
    ap.add_argument("--use-folder-tag",action="store_true")
    ap.add_argument("--tags-csv",default=None,help="CSV: pola_nama,kategori[,kontras]")
    ap.add_argument("--detect-contrast",action="store_true",
                    help="estimasi kontras dari bentuk (bantuan, tidak sempurna)")
    ap.add_argument("--seed",type=int,default=42)
    args=ap.parse_args()

    exts=("*.otf","*.ttf","*.ttc","*.OTF","*.TTF","*.TTC")
    files=[]
    for e in exts: files+=glob.glob(os.path.join(args.input,"**",e),recursive=True)
    files=sorted(set(files))
    if not files: print(f"[!] Tidak ada font di {args.input}"); sys.exit(1)
    print(f"[i] {len(files)} font ditemukan.")
    if args.detect_contrast: print("[i] --detect-contrast aktif (estimasi, tidak sempurna).")

    csv=load_csv(args.tags_csv)
    stats=new_stats()
    records=[]
    for i,path in enumerate(files,1):
        folder_tag=None
        if args.use_folder_tag:
            parent=os.path.basename(os.path.dirname(path))
            if os.path.abspath(os.path.dirname(path))!=os.path.abspath(args.input):
                folder_tag=parent.lower()
        opts={"csv":csv,"folder_tag":folder_tag,"default_cat":args.default_category,
              "detect_contrast":args.detect_contrast}
        for tag,ch,p in process_font(path,args.chars,args.max_path_len,opts,stats):
            records.append(to_record(tag,ch,p))
        if i%25==0 or i==len(files):
            print(f"  [{i}/{len(files)}] {stats['ok']} OK, {len(records)} glyph...",flush=True)

    if not records: print("[!] Tidak ada glyph."); sys.exit(1)
    random.seed(args.seed); random.shuffle(records)
    nv=max(1,int(len(records)*args.val_split)) if args.val_split>0 else 0
    val,train=records[:nv],records[nv:]
    base=args.output[:-6] if args.output.endswith(".jsonl") else args.output
    with open(f"{base}_train.jsonl","w") as f:
        for r in train: f.write(json.dumps(r,ensure_ascii=False)+"\n")
    if val:
        with open(f"{base}_val.jsonl","w") as f:
            for r in val: f.write(json.dumps(r,ensure_ascii=False)+"\n")

    print("\n"+"="*60+"\nRINGKASAN\n"+"="*60)
    print(f"Font OK            : {stats['ok']}")
    print(f"Total glyph        : {len(records)} (train {len(train)} | val {len(val)})")
    print(f"Glyph dilewati     : {stats['too_long']} (path terlalu panjang)")
    print("\nKATEGORI:")
    for c,n in stats["cat"].most_common(): print(f"   {c:28s} {n}")
    print("\nBERAT:")
    for w,n in stats["wt"].most_common(): print(f"   {w:28s} {n}")
    print("\nSumber tag (akurasi): "+", ".join(f"{k}={v}" for k,v in stats["src"].most_common()))
    if stats["skipped"]:
        print(f"\nFont bermasalah ({len(stats['skipped'])}):")
        for nm,why in stats["skipped"][:12]: print(f"   {nm}: {why}")
    print("\nContoh tag:")
    seen=set()
    for fam,tag in stats["sample"]:
        if fam not in seen: seen.add(tag); print(f"   {tag}")
        if len(seen)>=10: break
    print("="*60)
    print(f"[OK] {base}_train.jsonl"+(f" + {base}_val.jsonl" if val else ""))


if __name__ == "__main__":
    main()
