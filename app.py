import base64
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path

# When packaged as an .exe, ffmpeg / ffprobe / yt-dlp are unpacked into <bundle>/bin.
# A copy placed next to the .exe wins, so yt-dlp can be updated by replacing yt-dlp.exe.
# This must run before pydub is imported, because pydub looks for ffmpeg on import.
_here = Path(__file__).resolve().parent
_bundle = Path(getattr(sys, "_MEIPASS", _here))
_exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else _here
os.environ["PATH"] = os.pathsep.join([str(_exe_dir), str(_bundle / "bin"), os.environ.get("PATH", "")])

from flask import Flask, abort, jsonify, render_template_string, request, send_file
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

app = Flask(__name__)
# Limits for a public server (0 = no limit). Set them as environment variables.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))  # whole request
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "0"))      # one file
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "0"))      # one song
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

WORK = Path(tempfile.mkdtemp(prefix="silencecropper_"))
ALLOW_LINK = os.environ.get("ALLOW_LINK", "1") == "1"  # set ALLOW_LINK=0 on a public server
KEEP_SECONDS = 30 * 60  # trimmed files are deleted after 30 minutes
RESULTS = {}  # id -> {"path": Path, "name": str}
BITRATES = {"128k", "192k", "256k", "320k"}


def find_cuts(audio, threshold_db, padding_ms, chunk_ms=10):
    """Return (start_ms, end_ms) of the part to keep."""
    lead = detect_leading_silence(audio, silence_threshold=threshold_db, chunk_size=chunk_ms)
    tail = detect_leading_silence(audio.reverse(), silence_threshold=threshold_db, chunk_size=chunk_ms)
    if lead + tail >= len(audio):  # whole file is quieter than the threshold
        return 0, len(audio)
    return max(0, lead - padding_ms), min(len(audio), len(audio) - tail + padding_ms)


def waveform(audio, bins=160):
    """Peak level (0..1) for each of `bins` slices of the track."""
    step = max(1, len(audio) // bins)
    peak = audio.max_possible_amplitude or 1
    return [round(audio[i * step:(i + 1) * step].max / peak, 3) for i in range(bins)]


def read_tags(path):
    """Return the ID3 tags (title, artist, cover art, ...) of an MP3, or None."""
    try:
        tags = ID3(str(path))
    except Exception:  # no tags, or not readable
        return None
    tags.delall("TLEN")  # the old length would be wrong after trimming
    return tags if len(tags) else None


def fetch_audio(url):
    """Download a link with yt-dlp and return (AudioSegment, title, tags)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("Enter a full link that starts with https://")
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp was not found on your PATH")
    tmp = Path(tempfile.mkdtemp(dir=WORK))
    try:
        proc = subprocess.run(
            [exe, "-x", "--audio-format", "mp3", "--audio-quality", "0", "--no-playlist",
             "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
             "-o", str(tmp / "%(title).120B.%(ext)s"), url],
            capture_output=True, text=True, timeout=900)
        found = list(tmp.glob("*.mp3"))
        if proc.returncode or not found:
            lines = proc.stderr.strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "yt-dlp could not download this link")
        return AudioSegment.from_file(str(found[0])), found[0].stem, read_tags(found[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.errorhandler(413)
def too_big(_):
    return jsonify([{"name": "upload", "error": f"The upload is bigger than {MAX_UPLOAD_MB} MB."}]), 413


def check_limits(path):
    """Reject files over the size or length limit before decoding (decoding is what uses memory)."""
    if MAX_FILE_MB and path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"File is {path.stat().st_size / 1048576:.1f} MB. The limit on this server is {MAX_FILE_MB:g} MB.")
    if MAX_MINUTES:
        minutes = MP3(str(path)).info.length / 60
        if minutes > MAX_MINUTES:
            raise ValueError(f"Song is {minutes:.1f} min long. The limit on this server is {MAX_MINUTES:g} min.")


def purge_old():
    """Delete trimmed files older than KEEP_SECONDS so a public server does not fill its disk."""
    now = time.time()
    for rid in [k for k, v in RESULTS.items() if now - v.get("t", now) > KEEP_SECONDS]:
        RESULTS.pop(rid)["path"].unlink(missing_ok=True)


@app.post("/api/trim")
def api_trim():
    purge_old()
    threshold = max(-90.0, min(0.0, float(request.form.get("threshold", -40))))
    padding = max(0, min(2000, int(float(request.form.get("padding", 0)))))
    bitrate = request.form.get("bitrate", "320k")
    if bitrate not in BITRATES:
        bitrate = "320k"

    def make_item(audio, stem, tags=None):
        start, end = find_cuts(audio, threshold, padding)
        rid = uuid.uuid4().hex
        out = WORK / f"{rid}.mp3"
        audio[start:end].export(out, format="mp3", bitrate=bitrate)
        if tags:  # copy title, artist, album, cover art, ... onto the trimmed file
            try:
                tags.save(str(out), v2_version=3)
            except Exception:
                tags = None
        RESULTS[rid] = {"path": out, "name": f"{stem}_trimmed.mp3", "t": time.time()}
        return {"id": rid, "name": f"{stem}.mp3", "duration": len(audio),
                "start": start, "end": end, "peaks": waveform(audio),
                "threshold": threshold, "bitrate": bitrate, "tags": bool(tags)}

    items = []
    url = request.form.get("url", "").strip()
    if url and not ALLOW_LINK:
        items.append({"name": url, "error": "Link download is turned off on this server."})
    elif url:
        try:
            audio, stem, tags = fetch_audio(url)
            items.append(make_item(audio, stem, tags))
        except Exception as e:
            items.append({"name": url, "error": str(e)[:300]})
    for f in request.files.getlist("files"):
        src = WORK / f"{uuid.uuid4().hex}_src.mp3"
        try:
            f.save(src)
            check_limits(src)
            items.append(make_item(AudioSegment.from_file(str(src)),
                                   Path(f.filename or "track").stem or "track", read_tags(src)))
        except Exception as e:  # unreadable file, ffmpeg missing, etc.
            items.append({"name": f.filename, "error": str(e)[:200]})
        finally:
            src.unlink(missing_ok=True)
    return jsonify(items)


@app.get("/download/<rid>")
def download(rid):
    r = RESULTS.get(rid) or abort(404)
    return send_file(r["path"], as_attachment=True, download_name=r["name"], mimetype="audio/mpeg")


@app.get("/play/<rid>")
def play(rid):
    r = RESULTS.get(rid) or abort(404)
    return send_file(r["path"], mimetype="audio/mpeg")  # inline, so the player can seek


@app.get("/download-all")
def download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rid in request.args.get("ids", "").split(","):
            if rid in RESULTS:
                z.write(RESULTS[rid]["path"], RESULTS[rid]["name"])
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="silencecropper.zip", mimetype="application/zip")


FAVICON = base64.b64decode(
    "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"
    "8gAAAABJRU5ErkJggg=="
)


@app.get("/static/favicon.ico")
def favicon():
    return send_file(io.BytesIO(FAVICON), mimetype="image/x-icon", max_age=86400)


@app.get("/")
def index():
    page = PAGE if ALLOW_LINK else PAGE.replace("</head>", "<style>[data-mode=link]{display:none}</style></head>")
    return render_template_string(page)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Silence Cropper</title>
<link rel="icon" href="/static/favicon.ico" sizes="any">
<script>
try {
  const th = localStorage.getItem('theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = th;
} catch (e) { document.documentElement.dataset.theme = 'dark'; }
</script>
<style>
:root{--bg:#121212;--card:#1c1c1c;--line:#2b2b2b;--text:#fff;--muted:#a1a1aa;--pri:#0aa4c4;--pri-d:#0b7f97;--acc:#F46096;--acc-ink:#2a0615;--tabs:#1a1a1a;--tag:#2a2a2a;--tag-t:#ccc;--dropb:transparent;--cut:#ef4444;--glow:rgba(10,164,196,.32);--err:#f87171}
:root[data-theme=light]{--bg:#f4f6f8;--card:#fff;--line:#dde2e8;--text:#14181c;--muted:#5a6572;--pri:#0891b2;--pri-d:#0e7490;--tabs:#e9edf1;--tag:#eef1f5;--tag-t:#39424c;--dropb:#cdd4dc;--cut:#dc2626;--glow:rgba(8,145,178,.22);--err:#c62828}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,"Segoe UI",system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column}
a{color:inherit;text-decoration:none}
header{display:flex;align-items:center;gap:16px;padding:16px 48px;flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:7px;font-weight:900;font-size:26px;letter-spacing:-.5px;margin-right:auto}
.logo svg{width:24px;height:24px;color:var(--pri);flex:none}
.logo b{color:var(--pri)}
.badge{background:var(--acc);color:var(--acc-ink);font-weight:700;border:0;border-radius:8px;padding:9px 16px;font-size:15px;cursor:pointer}
.bitrate-wrap{position:relative}
#bitrate{display:flex;align-items:center;gap:10px}
#bitrate svg{width:12px;height:12px;transition:transform .15s ease}
#bitrate[aria-expanded=true] svg{transform:rotate(180deg)}
.bitrate-menu{position:absolute;z-index:5;top:calc(100% + 6px);right:0;min-width:100%;padding:5px;background:var(--card);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 20px #0003}
.bitrate-menu button{display:block;width:100%;padding:9px 12px;text-align:left;white-space:nowrap;background:none;border:0;border-radius:5px;color:var(--text);font:inherit;cursor:pointer}
.bitrate-menu button:hover,.bitrate-menu button[aria-selected=true]{background:var(--tag)}
.nav{font-weight:700;font-size:17px;cursor:pointer;background:none;border:0;color:var(--text)}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{background:none;border:0;color:var(--muted);font-weight:700;font-size:14px;padding:8px 11px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--acc);color:var(--acc-ink)}
.ico{background:none;border:1px solid var(--line);color:var(--text);border-radius:8px;width:38px;height:36px;font-size:18px;cursor:pointer}
main{width:100%;max-width:1030px;margin:0 auto;padding:28px 20px 60px;flex:1}
.tabs{display:flex;width:max-content;margin:0 auto;padding:6px;background:var(--tabs);border:1px solid var(--line);border-radius:12px;gap:4px}
.tab{background:none;border:0;color:var(--muted);font-weight:700;font-size:16px;padding:12px 24px;border-radius:8px;cursor:pointer;letter-spacing:.5px}
.tab[aria-selected=true]{background:var(--pri);color:#fff}
h1{font-size:clamp(40px,7vw,64px);font-weight:900;letter-spacing:-2px;text-align:center;margin:56px 0 12px}
.sub{text-align:center;color:var(--muted);font-size:20px;margin:0 0 36px}
.row{display:flex;gap:16px;align-items:stretch}
.drop{flex:1;background:#fff;color:#111;border-radius:12px;padding:0 22px;height:67px;display:flex;align-items:center;gap:14px;cursor:pointer;font-size:19px;min-width:0;border:2px solid var(--dropb)}
[hidden]{display:none!important}
.drop svg{color:var(--pri);flex:none}
.drop input{flex:1;border:0;outline:0;background:none;font:inherit;color:#111;min-width:0}
.drop.over{border-color:var(--pri)}
.drop span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555}
.drop.has span{color:#111}
.go{background:var(--pri);color:#fff;border:0;border-radius:12px;font-weight:700;font-size:20px;padding:0 36px;cursor:pointer;box-shadow:0 0 28px var(--glow)}
.go:disabled{opacity:.5;cursor:wait}
.opts{display:flex;gap:40px;flex-wrap:wrap;justify-content:center;margin:26px 0 0;color:var(--muted);font-size:16px}
.opts label{display:flex;align-items:center;gap:12px}
.opts output{color:var(--text);font-weight:700;min-width:64px}
input[type=range]{accent-color:var(--pri);width:200px}
#results{margin-top:44px;display:grid;gap:18px}
.item{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
.top{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.info{flex:1;min-width:220px}
.info h3{margin:0 0 6px;font-size:24px;overflow-wrap:anywhere}
.tags{display:flex;gap:8px;flex-wrap:wrap}
.tag{background:var(--tag);border:1px solid var(--line);border-radius:6px;padding:5px 11px;font-size:15px;color:var(--tag-t)}
.tag.hl{color:var(--acc);border-color:var(--acc)}
.play{width:52px;height:52px;border-radius:50%;border:0;background:var(--pri);color:#fff;font-size:18px;cursor:pointer;flex:none;box-shadow:0 0 18px var(--glow)}
.dl{background:var(--pri-d);color:#fff;font-weight:700;font-size:17px;border-radius:8px;padding:14px 28px;white-space:nowrap}
canvas{width:100%;height:90px;margin-top:20px;display:block}
.err{color:var(--err)}
.zip{display:block;margin:0 auto;background:none;border:1px solid var(--acc);color:var(--acc);font-weight:700;border-radius:10px;padding:12px 22px;cursor:pointer}
#faq{margin-top:70px;color:var(--muted);line-height:1.6;max-width:720px;margin-inline:auto}
#faq h2{color:var(--text);font-size:22px}
#faq b{color:var(--text)}
footer{border-top:1px solid var(--line);text-align:center;color:var(--muted);padding:26px;font-size:17px}
button:focus-visible,.drop:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
@media(max-width:700px){header{padding:14px 18px;gap:10px}.row{flex-direction:column}.go{height:60px}.nav{display:none}}
</style>
</head>
<body>
<header>
  <a class="logo" href="/"><img src="/static/favicon.ico" width="28" height="28"><span><b>sshhh</b>Vanisher</span></a>
  <div class="bitrate-wrap">
    <button type="button" class="badge" id="bitrate" value="320k" aria-label="Output bitrate" aria-haspopup="listbox" aria-expanded="false">
      <span id="bitrateLabel">320kbps MP3</span><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2 4 4 4 4-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="bitrate-menu" id="bitrateMenu" role="listbox" aria-label="Output bitrate" hidden>
      <button type="button" role="option" data-value="128k">128kbps MP3</button>
      <button type="button" role="option" data-value="192k">192kbps MP3</button>
      <button type="button" role="option" data-value="256k">256kbps MP3</button>
      <button type="button" role="option" data-value="320k" aria-selected="true">320kbps MP3</button>
    </div>
  </div>
  <a class="nav" href="#faq">FAQ</a>
  <div class="seg" role="group" aria-label="Language">
    <button type="button" data-lang="en" aria-pressed="true">EN</button>
    <button type="button" data-lang="id" aria-pressed="false">ID</button>
  </div>
  <button type="button" class="ico" id="themeBtn"></button>
</header>

<main>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" aria-selected="true" data-mode="file" data-i18n="tabFile"></button>
    <button class="tab" role="tab" aria-selected="false" data-mode="batch" data-i18n="tabBatch"></button>
  </div>

  <h1>Silence Cropper</h1>
  <p class="sub" data-i18n="sub"></p>

  <div class="row">
    <div class="drop" id="drop" tabindex="0" role="button">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 16V4m0 0L7 9m5-5 5 5M4 20h16"/></svg>
      <span id="dropText"></span>
    </div>
    <label class="drop" id="urlbox" hidden>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>
      <input id="url" type="url" data-i18n-ph="urlPh">
    </label>
    <button class="go" id="go"></button>
  </div>
  <input type="file" id="file" accept=".mp3,audio/mpeg" hidden>

  <div class="opts">
    <label><span data-i18n="cut"></span> <input type="range" id="thr" min="-60" max="-5" value="-40"><output id="thrOut">-40 dB</output></label>
    <label><span data-i18n="pad"></span> <input type="range" id="pad" min="0" max="500" step="10" value="0"><output id="padOut">0 ms</output></label>
  </div>

  <div id="results" aria-live="polite"></div>

  <section id="faq">
    <h2>FAQ</h2>
    <p><b data-i18n="q1"></b><br><span data-i18n="a1"></span></p>
    <p><b data-i18n="q2"></b><br><span data-i18n="a2"></span></p>
    <p><b data-i18n="q3"></b><br><span data-i18n="a3"></span></p>
    <p><b data-i18n="q4"></b><br><span data-i18n="a4"></span></p>
  </section>
</main>

<footer>All rights reserved &copy; 2026 ikhwnfzan.</footer>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = ms => { const s = Math.round(ms / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };

const T = {
  en: {
    tabFile: 'FILE', tabBatch: 'BATCH', tabLink: 'LINK',
    sub: 'Cut the quiet intro and outro off your MP3s. Nothing leaves your computer.',
    dropOne: 'Choose or drop an MP3 file', dropMany: 'Choose or drop MP3 files',
    nFiles: n => n + ' files selected', pickFirst: 'Pick an MP3 first',
    urlPh: 'Paste a YouTube link', trim: 'Trim', trimming: 'Trimming…',
    cut: 'Cut below', pad: 'Keep padding',
    tagsKept: 'Tags kept', removed: s => s + 's removed', intro: 'Intro', outro: 'Outro',
    play: 'Play preview', pause: 'Pause',
    dl: '⬇ Download MP3', zip: 'Download all as ZIP',
    errFile: 'Could not trim this file:', errReq: 'Request failed. Is the server running?',
    toLight: 'Switch to light theme', toDark: 'Switch to dark theme',
    q1: 'What does -40 dB mean?', a1: 'Any part at the start or end that is quieter than the cut level (default -40 dBFS) is removed. Move the slider lower, for example -50 dB, to cut less.',
    q2: 'Is the middle of the song changed?', a2: 'No. Only the intro and outro are removed. The file is re-encoded as MP3, so pick a high bitrate.',
    q3: 'Which links work in the LINK tab?', a3: 'Anything yt-dlp can download as audio, such as YouTube. Spotify links do not work because the audio is DRM protected. Only use content you have the right to download.',
    q4: 'Are tags and cover art kept?', a4: 'Yes. Title, artist, album, cover art and other ID3 tags are copied from the original to the trimmed file.'
  },
  id: {
    tabFile: 'FILE', tabBatch: 'MASSAL', tabLink: 'TAUTAN',
    sub: 'Potong intro dan outro yang sunyi dari MP3 kamu. Tidak ada yang keluar dari komputermu.',
    dropOne: 'Pilih atau seret file MP3', dropMany: 'Pilih atau seret file MP3',
    nFiles: n => n + ' file dipilih', pickFirst: 'Pilih MP3 dulu',
    urlPh: 'Tempel tautan YouTube', trim: 'Potong', trimming: 'Memotong…',
    cut: 'Potong di bawah', pad: 'Sisakan jeda',
    tagsKept: 'Tag dipertahankan', removed: s => s + ' dtk dipotong', intro: 'Intro', outro: 'Outro',
    play: 'Putar pratinjau', pause: 'Jeda',
    dl: '⬇ Unduh MP3', zip: 'Unduh semua (ZIP)',
    errFile: 'Gagal memotong file ini:', errReq: 'Permintaan gagal. Apakah server berjalan?',
    toLight: 'Ganti ke tema terang', toDark: 'Ganti ke tema gelap',
    q1: 'Apa arti -40 dB?', a1: 'Bagian awal atau akhir yang lebih pelan dari batas potong (bawaan -40 dBFS) akan dihapus. Geser slider lebih rendah, misalnya -50 dB, agar lebih sedikit yang dipotong.',
    q2: 'Apakah bagian tengah lagu berubah?', a2: 'Tidak. Hanya intro dan outro yang dihapus. File di-encode ulang sebagai MP3, jadi pilih bitrate yang tinggi.',
    q3: 'Tautan apa yang bisa dipakai di tab TAUTAN?', a3: 'Apa pun yang bisa diunduh yt-dlp sebagai audio, seperti YouTube. Tautan Spotify tidak bisa karena audionya dilindungi DRM. Gunakan hanya konten yang boleh kamu unduh.',
    q4: 'Apakah tag dan sampul tetap ada?', a4: 'Ya. Judul, artis, album, sampul, dan tag ID3 lainnya disalin dari file asli ke file hasil potong.'
  }
};
const store = {
  get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} }
};
let lang = store.get('lang') || ((navigator.language || '').toLowerCase().startsWith('id') ? 'id' : 'en');
const t = (k, ...a) => { const v = T[lang][k]; return typeof v === 'function' ? v(...a) : v; };

let files = [], mode = 'file', busy = false, last = [], failed = false;
const input = $('#file'), drop = $('#drop');

function refreshDrop() {
  $('#dropText').textContent = pickHint ? t('pickFirst') : !files.length ? t(mode === 'file' ? 'dropOne' : 'dropMany')
    : files.length === 1 ? files[0].name : t('nFiles', files.length);
}
let pickHint = false;
function setBtn() { $('#go').textContent = t(busy ? 'trimming' : 'trim'); }

function draw(canvas, d) {
  const css = getComputedStyle(document.documentElement);
  const keep = css.getPropertyValue('--pri').trim(), cutc = css.getPropertyValue('--cut').trim();
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext('2d'); c.scale(dpr, dpr);
  const n = d.peaks.length, bw = w / n;
  d.peaks.forEach((p, i) => {
    const tm = (i + .5) / n * d.duration, kept = tm >= d.start && tm <= d.end;
    const bh = Math.max(2, p * h);
    c.fillStyle = kept ? keep : cutc;
    c.fillRect(i * bw + 1, (h - bh) / 2, Math.max(1, bw - 2), bh);
  });
  if (d.pos != null) {  // playhead
    c.fillStyle = css.getPropertyValue('--acc').trim();
    c.fillRect(Math.min(w - 2, d.pos / d.duration * w), 0, 2, h);
  }
}

// ---------- preview player: play the trimmed song, click the waveform to jump ----------
const player = new Audio(); let cur = null;
const setPlay = d => {
  if (!d || !d.btn) return;
  const on = cur === d && !player.paused;
  d.btn.textContent = on ? '❚❚' : '▶\uFE0E';
  d.btn.setAttribute('aria-label', t(on ? 'pause' : 'play'));
};
function tick() {
  if (!cur) return;
  cur.pos = cur.start + player.currentTime * 1000;
  if (cur.cv) draw(cur.cv, cur);
  if (!player.paused) requestAnimationFrame(tick);
}
function play(d, atMs) {
  if (cur !== d) {
    const o = cur; cur = d;
    if (o) { o.pos = null; if (o.cv) draw(o.cv, o); setPlay(o); }
    player.src = d.url || ('/play/' + d.id);
  }
  if (atMs != null) { player.currentTime = (atMs - d.start) / 1000; player.play(); }
  else if (player.paused) player.play(); else player.pause();
}
player.onplay = () => { setPlay(cur); tick(); };
player.onpause = () => setPlay(cur);
player.onended = () => { if (!cur) return; cur.pos = null; if (cur.cv) draw(cur.cv, cur); setPlay(cur); };

function render() {
  const out = $('#results'); out.innerHTML = '';
  if (failed) { out.innerHTML = `<div class="item"><p class="err">${esc(t('errReq'))}</p></div>`; return; }
  const ok = [];
  last.forEach(d => {
    const el = document.createElement('div'); el.className = 'item';
    if (d.error) {
      el.innerHTML = `<h3>${esc(d.name)}</h3><p class="err">${esc(t('errFile'))} ${esc(d.error)}</p>`;
    } else {
      ok.push(d);
      const removed = d.duration - (d.end - d.start);
      el.innerHTML = `<div class="top"><button type="button" class="play"></button><div class="info"><h3>${esc(d.name)}</h3><div class="tags">
        <span class="tag">${fmt(d.duration)} → ${fmt(d.end - d.start)}</span>
        <span class="tag hl">${esc(t('removed', (removed / 1000).toFixed(1)))}</span>
        <span class="tag">${esc(t('intro'))} ${(d.start / 1000).toFixed(1)}s</span>
        <span class="tag">${esc(t('outro'))} ${((d.duration - d.end) / 1000).toFixed(1)}s</span>
        <span class="tag">${d.threshold} dB</span>${d.tags ? `<span class="tag">${esc(t('tagsKept'))}</span>` : ''}</div></div>
        <a class="dl" href="/download/${d.id}">${esc(t('dl'))}</a></div><canvas></canvas>`;
    }
    out.appendChild(el);
    const cv = el.querySelector('canvas');
    if (cv) {
      d.cv = cv; d.btn = el.querySelector('.play'); draw(cv, d); setPlay(d);
      d.btn.onclick = () => play(d);
      cv.style.cursor = 'pointer';
      cv.onclick = e => {
        const r = cv.getBoundingClientRect(), ms = (e.clientX - r.left) / r.width * d.duration;
        if (ms >= d.start && ms <= d.end) play(d, ms);
      };
    }
  });
  if (ok.length > 1) {
    const z = document.createElement('a'); z.className = 'zip';
    z.href = '/download-all?ids=' + ok.map(d => d.id).join(',');
    z.textContent = t('zip'); out.appendChild(z);
  }
}

function applyTheme(th) {
  document.documentElement.dataset.theme = th; store.set('theme', th);
  const b = $('#themeBtn');
  b.textContent = th === 'light' ? '☀' : '☾';
  b.setAttribute('aria-label', t(th === 'dark' ? 'toLight' : 'toDark')); b.title = b.getAttribute('aria-label');
  render();
}
function applyLang(l) {
  lang = l; store.set('lang', l);
  document.documentElement.lang = l;
  document.querySelectorAll('[data-i18n]').forEach(e => e.textContent = t(e.dataset.i18n));
  document.querySelectorAll('[data-i18n-ph]').forEach(e => e.placeholder = t(e.dataset.i18nPh));
  document.querySelectorAll('[data-lang]').forEach(b => b.setAttribute('aria-pressed', b.dataset.lang === l));
  refreshDrop(); setBtn(); applyTheme(document.documentElement.dataset.theme);
}
document.querySelectorAll('[data-lang]').forEach(b => b.onclick = () => applyLang(b.dataset.lang));
$('#themeBtn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

const bitrateBtn = $('#bitrate'), bitrateMenu = $('#bitrateMenu');
function showBitrates(open) {
  bitrateBtn.setAttribute('aria-expanded', open);
  bitrateMenu.hidden = !open;
}
bitrateBtn.onclick = () => showBitrates(bitrateMenu.hidden);
bitrateMenu.onclick = e => {
  const option = e.target.closest('[data-value]');
  if (!option) return;
  bitrateBtn.value = option.dataset.value;
  $('#bitrateLabel').textContent = option.textContent;
  bitrateMenu.querySelectorAll('[role=option]').forEach(x => x.setAttribute('aria-selected', x === option));
  showBitrates(false);
};
document.addEventListener('click', e => { if (!e.target.closest('.bitrate-wrap')) showBitrates(false); });
bitrateBtn.onkeydown = e => { if (e.key === 'Escape') showBitrates(false); };

document.querySelectorAll('.tab').forEach(tb => tb.onclick = () => {
  mode = tb.dataset.mode;
  document.querySelectorAll('.tab').forEach(x => x.setAttribute('aria-selected', x === tb));
  input.multiple = mode === 'batch';
  drop.hidden = mode === 'link';
  $('#urlbox').hidden = mode !== 'link';
  setFiles([]);
});
function setFiles(list) {
  files = [...list].filter(f => /\.mp3$/i.test(f.name));
  if (mode === 'file') files = files.slice(0, 1);
  pickHint = false;
  drop.classList.toggle('has', files.length > 0);
  refreshDrop();
}
drop.onclick = () => input.click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
input.onchange = () => setFiles(input.files);
['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => setFiles(e.dataTransfer.files));

$('#url').onkeydown = e => { if (e.key === 'Enter') $('#go').click(); };
$('#thr').oninput = e => $('#thrOut').textContent = e.target.value + ' dB';
$('#pad').oninput = e => $('#padOut').textContent = e.target.value + ' ms';

$('#go').onclick = async () => {
  const link = $('#url').value.trim();
  if (mode === 'link' ? !link : !files.length) {
    if (mode === 'link') $('#url').focus(); else { pickHint = true; refreshDrop(); }
    return;
  }
  player.pause(); cur = null; busy = true; failed = false; last = []; setBtn(); $('#go').disabled = true; render();
  const fd = new FormData();
  if (mode === 'link') fd.append('url', link); else files.forEach(f => fd.append('files', f));
  fd.append('threshold', $('#thr').value);
  fd.append('padding', $('#pad').value);
  fd.append('bitrate', $('#bitrate').value);
  try {
    const res = await fetch('/api/trim', { method: 'POST', body: fd });
    last = await res.json();
  } catch (e) { failed = true; }
  busy = false; $('#go').disabled = false; setBtn(); render();
};

applyLang(lang);
</script>
</body>
</html>
"""

def free_port(preferred=5000):
    for port in (preferred, 0):  # 0 = let the OS pick a free port
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"Silence Cropper is running at {url}  (close this window to quit)")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)