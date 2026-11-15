import base64
import io
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_file
from mutagen.id3 import ID3
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB per request

WORK = Path(tempfile.mkdtemp(prefix="silencecropper_"))
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


@app.post("/api/trim")
def api_trim():
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
        RESULTS[rid] = {"path": out, "name": f"{stem}_trimmed.mp3"}
        return {
            "id": rid,
            "name": f"{stem}.mp3",
            "duration": len(audio),
            "start": start,
            "end": end,
            "peaks": waveform(audio),
            "threshold": threshold,
            "bitrate": bitrate,
            "tags": bool(tags),
        }

    items = []
    for f in request.files.getlist("files"):
        src = WORK / f"{uuid.uuid4().hex}_src.mp3"
        try:
            f.save(src)
            items.append(
                make_item(
                    AudioSegment.from_file(str(src)),
                    Path(f.filename or "track").stem or "track",
                    read_tags(src),
                )
            )
        except Exception as e:  # unreadable file, ffmpeg missing, etc.
            items.append({"name": f.filename, "error": str(e)[:200]})
        finally:
            src.unlink(missing_ok=True)
    return jsonify(items)


@app.get("/download/<rid>")
def download(rid):
    r = RESULTS.get(rid) or abort(404)
    return send_file(r["path"], as_attachment=True, download_name=r["name"], mimetype="audio/mpeg")


@app.get("/download-all")
def download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rid in request.args.get("ids", "").split(","):
            if rid in RESULTS:
                z.write(RESULTS[rid]["path"], RESULTS[rid]["name"])
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="silencecropper.zip", mimetype="application/zip")


@app.get("/")
def index():
    return render_template_string(PAGE)

import base64
import io
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_file, send_from_directory
from mutagen.id3 import ID3
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB per request

WORK = Path(tempfile.mkdtemp(prefix="silencecropper_"))
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


@app.post("/api/trim")
def api_trim():
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
        RESULTS[rid] = {"path": out, "name": f"{stem}_trimmed.mp3"}
        return {
            "id": rid,
            "name": f"{stem}.mp3",
            "duration": len(audio),
            "start": start,
            "end": end,
            "peaks": waveform(audio),
            "threshold": threshold,
            "bitrate": bitrate,
            "tags": bool(tags),
        }

    items = []
    for f in request.files.getlist("files"):
        src = WORK / f"{uuid.uuid4().hex}_src.mp3"
        try:
            f.save(src)
            items.append(
                make_item(
                    AudioSegment.from_file(str(src)),
                    Path(f.filename or "track").stem or "track",
                    read_tags(src),
                )
            )
        except Exception as e:  # unreadable file, ffmpeg missing, etc.
            items.append({"name": f.filename, "error": str(e)[:200]})
        finally:
            src.unlink(missing_ok=True)
    return jsonify(items)


@app.get("/download/<rid>")
def download(rid):
    r = RESULTS.get(rid) or abort(404)
    return send_file(r["path"], as_attachment=True, download_name=r["name"], mimetype="audio/mpeg")


@app.get("/download-all")
def download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rid in request.args.get("ids", "").split(","):
            if rid in RESULTS:
                z.write(RESULTS[rid]["path"], RESULTS[rid]["name"])
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="silencecropper.zip", mimetype="application/zip")

@app.get("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )

@app.get("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sshhh Vanisher — Silence Cropper</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800;900&display=swap" rel="stylesheet">
<script>
try {
  const th = localStorage.getItem('theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = th;
} catch (e) { document.documentElement.dataset.theme = 'light'; }
</script>
<style>
  /* =========================================================
     CLAYMORPHISM DESIGN
     ========================================================= */
  :root, :root[data-theme="light"] {
    --bg: #e7ecf5;
    --bg-gradient: radial-gradient(circle at 15% 15%, #f3f7ff 0%, #e4eaf4 55%, #dce4f0 100%);
    --clay-surface: #edf2fa;
    --clay-surface-hover: #f2f6fd;
    --clay-well: #dfe6f2;
    --text: #273043;
    --text-muted: #68758d;

    --shadow-clay-card:
      12px 12px 26px rgba(166, 179, 201, 0.55),
      -10px -10px 22px rgba(255, 255, 255, 0.92),
      inset 4px 4px 8px rgba(255, 255, 255, 0.85),
      inset -4px -4px 10px rgba(176, 189, 212, 0.35);

    --shadow-clay-pill:
      6px 6px 14px rgba(166, 179, 201, 0.45),
      -6px -6px 14px rgba(255, 255, 255, 0.9),
      inset 2px 2px 5px rgba(255, 255, 255, 0.8),
      inset -2px -2px 5px rgba(176, 189, 212, 0.3);

    --shadow-clay-well:
      inset 6px 6px 12px rgba(166, 179, 201, 0.55),
      inset -6px -6px 12px rgba(255, 255, 255, 0.92),
      0 4px 12px rgba(255, 255, 255, 0.5);

    --pri: #0aa4c4;
    --pri-surface: linear-gradient(145deg, #12b8db, #088ba6);
    --shadow-clay-pri:
      8px 10px 20px rgba(10, 164, 196, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.75),
      inset 3px 3px 6px rgba(184, 239, 247, 0.65),
      inset -3px -4px 8px rgba(4, 105, 127, 0.45);

    --acc: #F46096;
    --accent-surface: linear-gradient(145deg, var(--pri), var(--acc));
    --acc-surface: linear-gradient(145deg, #ff74a7, #e04880);
    --shadow-clay-acc:
      6px 8px 16px rgba(244, 96, 150, 0.3),
      -4px -4px 10px rgba(255, 255, 255, 0.85),
      inset 2px 2px 5px rgba(255, 204, 223, 0.75),
      inset -2px -3px 6px rgba(179, 36, 88, 0.38);

    --green-surface: linear-gradient(145deg, #27d366, #1db454);
    --shadow-clay-green:
      8px 10px 20px rgba(34, 197, 94, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.7),
      inset 3px 3px 6px rgba(154, 247, 186, 0.65),
      inset -3px -4px 8px rgba(19, 138, 62, 0.45);

    --cut: #ef4444;
    --err-bg: #ffe8eb;
    --err-ink: #c92a3e;
    --shadow-clay-err:
      8px 8px 18px rgba(201, 42, 62, 0.14),
      -6px -6px 14px rgba(255, 255, 255, 0.85),
      inset 3px 3px 6px rgba(255, 255, 255, 0.8),
      inset -3px -3px 7px rgba(224, 135, 148, 0.3);
  }

  :root[data-theme="dark"] {
    --bg: #1e212b;
    --bg-gradient: radial-gradient(circle at 15% 15%, #262a37 0%, #1d2029 60%, #171921 100%);
    --clay-surface: #252936;
    --clay-surface-hover: #2b303f;
    --clay-well: #1b1e27;
    --text: #f1f5fb;
    --text-muted: #9aa5bc;

    --shadow-clay-card:
      12px 14px 28px rgba(10, 12, 17, 0.68),
      -8px -8px 20px rgba(48, 54, 72, 0.42),
      inset 3px 3px 7px rgba(64, 72, 95, 0.48),
      inset -4px -5px 10px rgba(13, 15, 22, 0.65);

    --shadow-clay-pill:
      6px 7px 14px rgba(10, 12, 17, 0.6),
      -5px -5px 12px rgba(48, 54, 72, 0.38),
      inset 2px 2px 5px rgba(64, 72, 95, 0.45),
      inset -2px -3px 6px rgba(13, 15, 22, 0.6);

    --shadow-clay-well:
      inset 6px 6px 14px rgba(10, 12, 17, 0.72),
      inset -5px -5px 12px rgba(46, 52, 69, 0.35),
      0 2px 6px rgba(48, 54, 72, 0.2);

    --shadow-clay-pri:
      8px 10px 22px rgba(10, 12, 17, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(184, 239, 247, 0.45),
      inset -3px -4px 8px rgba(4, 105, 127, 0.6);

    --shadow-clay-acc:
      6px 8px 16px rgba(10, 12, 17, 0.55),
      -3px -3px 10px rgba(48, 54, 72, 0.25),
      inset 2px 2px 5px rgba(255, 204, 223, 0.55),
      inset -2px -3px 6px rgba(150, 25, 72, 0.55);

    --shadow-clay-green:
      8px 10px 22px rgba(13, 18, 15, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(134, 239, 172, 0.5),
      inset -3px -4px 8px rgba(15, 118, 53, 0.6);

    --cut: #f87171;
    --err-bg: #352128;
    --err-ink: #ff8796;
    --shadow-clay-err:
      8px 10px 20px rgba(10, 12, 17, 0.6),
      -5px -5px 12px rgba(48, 54, 72, 0.3),
      inset 2px 2px 5px rgba(95, 52, 64, 0.45),
      inset -3px -3px 7px rgba(20, 11, 14, 0.65);
  }

  * { box-sizing: border-box; }
  [hidden] { display: none !important; }

  body {
    margin: 0;
    background: var(--bg);
    background-image: var(--bg-gradient);
    background-attachment: fixed;
    color: var(--text);
    font-family: "Nunito", "Inter", system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    transition: background 0.3s ease, color 0.25s ease;
  }

  a { color: inherit; text-decoration: none; }
  main { flex: 1; }

  /* =========================================================
     MOLDED NAVBAR
     ========================================================= */
  .navbar {
    width: 100%;
    max-width: 1020px;
    margin: 18px auto 0;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    background: var(--clay-surface);
    border-radius: 28px;
    box-shadow: var(--shadow-clay-card);
  }

  .logo {
    font-size: 1.38rem;
    font-weight: 900;
    letter-spacing: -0.02em;
    display: flex;
    align-items: center;
    gap: 11px;
    margin-right: auto;
  }
  .logo-mold {
    width: 40px;
    height: 40px;
    border-radius: 15px;
    background: var(--clay-surface);
    box-shadow: var(--shadow-clay-pill);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .logo b { color: var(--pri); }

  .nav-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .nav-btn {
    background: var(--clay-surface);
    border: none;
    color: var(--text);
    font-family: inherit;
    font-size: 0.88rem;
    font-weight: 800;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 9px 15px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.16s ease;
  }
  .nav-btn:hover { transform: translateY(-2px); }
  .nav-btn:active {
    transform: translateY(1px) scale(0.97);
    box-shadow: var(--shadow-clay-well);
  }

  .bitrate-pill {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  .seg {
    display: inline-flex;
    background: var(--clay-well);
    padding: 4px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-well);
    gap: 3px;
  }
  .seg button {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-family: inherit;
    font-weight: 800;
    font-size: 0.8rem;
    padding: 6px 11px;
    border-radius: 999px;
    cursor: pointer;
    transition: all 0.18s ease;
  }
  .seg button[aria-pressed="true"] {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  /* =========================================================
     HERO & MOLDED WORKBENCH
     ========================================================= */
  .hero {
    max-width: 840px;
    margin: 38px auto 48px;
    padding: 0 20px;
    text-align: center;
  }

  /* Pill Tab Switcher (clay.py style with accent-surface gradient) */
  .tabs {
    display: inline-flex;
    align-items: center;
    background: var(--clay-well);
    border-radius: 999px;
    padding: 7px;
    margin-bottom: 26px;
    gap: 6px;
    box-shadow: var(--shadow-clay-well);
  }
  .tab {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-family: inherit;
    font-size: 0.84rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    padding: 10px 22px;
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .tab:hover:not([aria-selected="true"]) { color: var(--text); }
  .tab[aria-selected="true"] {
    background: var(--accent-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-pri);
    transform: translateY(-1px);
  }

  h1 {
    font-size: clamp(2.1rem, 4.8vw, 3rem);
    font-weight: 900;
    margin: 0 0 10px;
    letter-spacing: -0.025em;
  }
  .sub {
    color: var(--text-muted);
    font-size: 1.05rem;
    font-weight: 600;
    margin: 0 0 32px;
  }

  /* Sculpted Clay Workbench Card */
  .workbench {
    background: var(--clay-surface);
    border-radius: 32px;
    padding: 28px;
    box-shadow: var(--shadow-clay-card);
    text-align: left;
  }

  /* Tall Recessed Clay Dropzone Well (from clay.py) */
  .dropzone {
    background: var(--clay-well);
    border-radius: 24px;
    padding: 36px 24px;
    text-align: center;
    box-shadow: var(--shadow-clay-well);
    border: 2.5px dashed rgba(10, 164, 196, 0.35);
    cursor: pointer;
    transition: border-color 0.2s ease, transform 0.2s ease;
  }
  .dropzone:hover, .dropzone.over {
    border-color: var(--pri);
    transform: scale(1.006);
  }
  .drop-icon-mold {
    width: 62px;
    height: 62px;
    margin: 0 auto 14px;
    border-radius: 22px;
    background: var(--clay-surface);
    box-shadow: var(--shadow-clay-pill);
    color: var(--pri);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .dropzone h3 {
    margin: 0 0 6px;
    font-size: 1.15rem;
    font-weight: 900;
  }
  .dropzone p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.9rem;
    font-weight: 700;
  }

  /* Slider Controls Strip */
  .opts {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
    justify-content: space-between;
    margin-top: 22px;
    background: var(--clay-well);
    padding: 15px 22px;
    border-radius: 22px;
    box-shadow: var(--shadow-clay-well);
    color: var(--text-muted);
    font-size: 0.92rem;
    font-weight: 800;
  }
  .opts label {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    min-width: 240px;
  }
  .opts output {
    background: var(--clay-surface);
    color: var(--pri);
    font-weight: 900;
    padding: 5px 12px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    min-width: 72px;
    text-align: center;
  }
  input[type="range"] {
    flex: 1;
    accent-color: var(--pri);
    cursor: pointer;
  }

  /* Queued File Chips (from clay.py) */
  .file-list {
    margin-top: 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 240px;
    overflow-y: auto;
    padding: 4px;
  }
  .file-item {
    background: var(--clay-surface);
    border-radius: 18px;
    padding: 11px 16px;
    box-shadow: var(--shadow-clay-pill);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-size: 0.9rem;
    font-weight: 700;
  }
  .file-item-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .file-name {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-size {
    color: var(--text-muted);
    font-size: 0.8rem;
    flex-shrink: 0;
  }
  .remove-file-btn {
    background: var(--clay-well);
    border: none;
    color: var(--text-muted);
    width: 28px;
    height: 28px;
    border-radius: 50%;
    cursor: pointer;
    font-weight: 900;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .remove-file-btn:hover { color: var(--err-ink); }

  /* Primary Action Bar: Crop Silence (with Scissors Icon) + Clear */
  .action-bar {
    margin-top: 22px;
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .go {
    flex: 1;
    background: var(--pri);
    color: #fff;
    border: none;
    border-radius: 22px;
    padding: 16px 26px;
    font-family: inherit;
    font-size: 1.04rem;
    font-weight: 900;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    box-shadow: var(--shadow-clay-pri);
    transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1), filter 0.15s ease;
  }
  .go:hover:not(:disabled) {
    transform: translateY(-2px);
    filter: brightness(1.04);
  }
  .go:active:not(:disabled) {
    transform: translateY(2px) scale(0.98);
  }
  .go:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-clear {
    background: var(--clay-surface);
    color: var(--text-muted);
    border: none;
    border-radius: 22px;
    padding: 16px 22px;
    font-family: inherit;
    font-size: 0.94rem;
    font-weight: 800;
    cursor: pointer;
    box-shadow: var(--shadow-clay-pill);
    transition: transform 0.15s ease, color 0.15s ease;
  }
  .btn-clear:hover { color: var(--text); transform: translateY(-1px); }

  /* =========================================================
     RESULTS & WAVEFORM CARDS
     ========================================================= */
  #results {
    margin-top: 30px;
    display: grid;
    gap: 20px;
  }

  .item {
    background: var(--clay-surface);
    border-radius: 28px;
    padding: 24px;
    box-shadow: var(--shadow-clay-card);
    text-align: left;
  }

  .top {
    display: flex;
    gap: 18px;
    align-items: center;
    flex-wrap: wrap;
  }
  .info { flex: 1; min-width: 220px; }
  .info h3 {
    margin: 0 0 10px;
    font-size: 1.22rem;
    font-weight: 900;
    overflow-wrap: anywhere;
  }

  .tags { display: flex; gap: 8px; flex-wrap: wrap; }
  .tag {
    background: var(--clay-well);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 0.8rem;
    font-weight: 800;
    color: var(--text-muted);
    box-shadow: inset 2px 2px 5px rgba(0, 0, 0, 0.06), inset -2px -2px 5px rgba(255, 255, 255, 0.25);
  }
  .tag.hl {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  .dl {
    background: var(--green-surface);
    color: #fff;
    font-weight: 900;
    font-size: 0.94rem;
    border-radius: 18px;
    padding: 13px 24px;
    white-space: nowrap;
    box-shadow: var(--shadow-clay-green);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .dl:hover { transform: translateY(-2px); }

  .canvas-well {
    margin-top: 18px;
    background: var(--clay-well);
    border-radius: 20px;
    padding: 12px 14px;
    box-shadow: var(--shadow-clay-well);
  }
  canvas {
    width: 100%;
    height: 86px;
    display: block;
  }

  .err {
    background: var(--err-bg);
    color: var(--err-ink);
    box-shadow: var(--shadow-clay-err);
    padding: 13px 20px;
    border-radius: 18px;
    font-weight: 800;
    margin: 0;
  }

  .zip {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 6px auto 0;
    background: var(--acc-surface);
    color: #fff;
    font-weight: 900;
    font-size: 0.98rem;
    border-radius: 22px;
    padding: 15px 30px;
    box-shadow: var(--shadow-clay-acc);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .zip:hover { transform: translateY(-2px); }

  /* Molded FAQ Section */
  #faq {
    margin: 48px auto 0;
    max-width: 840px;
    background: var(--clay-surface);
    border-radius: 32px;
    padding: 28px 32px;
    box-shadow: var(--shadow-clay-card);
    color: var(--text-muted);
    line-height: 1.65;
    text-align: left;
    font-weight: 600;
  }
  #faq h2 {
    margin-top: 0;
    color: var(--pri);
    font-size: 1.35rem;
    font-weight: 900;
  }
  #faq b { color: var(--text); font-weight: 800; }

  footer {
    text-align: center;
    color: var(--text-muted);
    padding: 26px 20px 32px;
    font-size: 0.9rem;
    font-weight: 700;
  }

  button:focus-visible, .dropzone:focus-visible, a:focus-visible {
    outline: 3px solid var(--acc);
    outline-offset: 2px;
  }

  @media (max-width: 700px) {
    .navbar { margin: 12px 14px 0; width: auto; padding: 12px 16px; border-radius: 22px; }
    .nav-faq { display: none; }
    .workbench { padding: 20px; border-radius: 26px; }
    .action-bar { flex-direction: column; }
    .go, .btn-clear { width: 100%; }
  }
</style>
</head>
<body>

<header class="navbar">
  <a href="/" class="logo">
    <span class="logo-mold">
      <img src="/favicon.ico" width="24" height="24" >
    </span>
    <div><b>sshhh</b>Vanisher</div>
  </a>
  <div class="nav-right">
    <button type="button" class="nav-btn bitrate-pill" id="bitrate" value="320k" aria-label="Output bitrate: 320kbps MP3. Click to change">320kbps MP3</button>
    <a class="nav-btn nav-faq" href="#faq">FAQ</a>
    <div class="seg" role="group" aria-label="Language">
      <button type="button" data-lang="en" aria-pressed="true">EN</button>
      <button type="button" data-lang="id" aria-pressed="false">ID</button>
    </div>
    <button type="button" class="nav-btn" id="themeBtn"></button>
  </div>
</header>

<main>
  <section class="hero">
    <!-- Pill Tab Switcher with Music Note & Folder Icons + accent-surface gradient -->
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" aria-selected="true" data-mode="file">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
        <span data-i18n="tabFile">SINGLE MP3</span>
      </button>
      <button class="tab" role="tab" aria-selected="false" data-mode="batch">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span data-i18n="tabBatch">BATCH / FOLDER</span>
      </button>
    </div>

    <h1>Silence Cropper</h1>
    <p class="sub" data-i18n="sub"></p>

    <div class="workbench">
      <input type="file" id="file" accept=".mp3,audio/mpeg" hidden>

      <!-- Tall Recessed Clay Dropzone Well -->
      <div class="dropzone" id="drop" tabindex="0" role="button">
        <div class="drop-icon-mold">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
        </div>
        <h3 id="dropTitle"></h3>
        <p id="dropSub"></p>
      </div>

      <!-- Cut Below & Keep Padding Sliders -->
      <div class="opts">
        <label><span data-i18n="cut"></span> <input type="range" id="thr" min="-60" max="-5" value="-40"><output id="thrOut">-40 dB</output></label>
        <label><span data-i18n="pad"></span> <input type="range" id="pad" min="0" max="500" step="10" value="0"><output id="padOut">0 ms</output></label>
      </div>

      <!-- Queued File Chips -->
      <div class="file-list" id="fileList"></div>

      <!-- Primary Action Bar with Scissors Icon -->
      <div class="action-bar">
        <button type="button" class="go" id="go" disabled>
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>
          </svg>
          <span id="goText"></span>
        </button>
        <button type="button" class="btn-clear" id="clearBtn" style="display:none;" data-i18n="btnClear">Clear</button>
      </div>
    </div>

    <div id="results" aria-live="polite"></div>

    <section id="faq">
      <h2>FAQ</h2>
      <p><b data-i18n="q1"></b><br><span data-i18n="a1"></span></p>
      <p><b data-i18n="q2"></b><br><span data-i18n="a2"></span></p>
      <p><b data-i18n="q3"></b><br><span data-i18n="a3"></span></p>
    </section>
  </section>
</main>

<footer>All rights reserved &copy; 2026 ikhwnfzan.</footer>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = ms => { const s = Math.round(ms / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const fmtBytes = b => b < 1024 * 1024 ? (b / 1024).toFixed(1) + ' KB' : (b / (1024 * 1024)).toFixed(2) + ' MB';

const T = {
  en: {
    tabFile: 'SINGLE MP3', tabBatch: 'BATCH / FOLDER',
    sub: 'Cut the quiet intro and outro off your MP3s. Nothing leaves your computer.',
    dropSingleTitle: 'Drop an MP3 file here or click to browse',
    dropSingleSub: 'Supports 320kbps MP3 with embedded ID3 cover art',
    dropBatchTitle: 'Drop multiple MP3 files here or click to select',
    dropBatchSub: 'Select multiple .mp3 tracks to trim in batch and download as ZIP',
    crop: 'Crop Silence', cropping: 'Cropping silence…', btnClear: 'Clear',
    cut: 'Cut below', pad: 'Keep padding',
    tagsKept: 'Tags kept', removed: s => s + 's removed', intro: 'Intro', outro: 'Outro',
    dl: '⬇ Download MP3', zip: 'Download all as ZIP',
    errFile: 'Could not trim this file:', errReq: 'Request failed. Is the server running?',
    toLight: 'Switch to light theme', toDark: 'Switch to dark theme',
    q1: 'What does -40 dB mean?', a1: 'Any part at the start or end that is quieter than the cut level (default -40 dBFS) is removed. Move the slider lower, for example -50 dB, to cut less.',
    q2: 'Is the middle of the song changed?', a2: 'No. Only the intro and outro are removed. The file is re-encoded as MP3, so pick a high bitrate.',
    q3: 'Are tags and cover art kept?', a3: 'Yes. Title, artist, album, cover art and other ID3 tags are copied from the original to the trimmed file.'
  },
  id: {
    tabFile: 'SATU MP3', tabBatch: 'BATCH / BANYAK FILE',
    sub: 'Potong intro dan outro yang sunyi dari MP3 kamu. Tidak ada yang keluar dari komputermu.',
    dropSingleTitle: 'Tarik satu file MP3 ke sini atau klik untuk memilih',
    dropSingleSub: 'Mendukung MP3 320kbps dengan cover art ID3 tertanam',
    dropBatchTitle: 'Tarik banyak file MP3 ke sini atau klik untuk memilih',
    dropBatchSub: 'Pilih beberapa file .mp3 sekaligus untuk dipotong dan diunduh sebagai ZIP',
    crop: 'Potong Hening', cropping: 'Memotong hening…', btnClear: 'Hapus',
    cut: 'Potong di bawah', pad: 'Sisakan jeda',
    tagsKept: 'Tag dipertahankan', removed: s => s + ' dtk dipotong', intro: 'Intro', outro: 'Outro',
    dl: '⬇ Unduh MP3', zip: 'Unduh semua (ZIP)',
    errFile: 'Gagal memotong file ini:', errReq: 'Permintaan gagal. Apakah server berjalan?',
    toLight: 'Ganti ke tema terang', toDark: 'Ganti ke tema gelap',
    q1: 'Apa arti -40 dB?', a1: 'Bagian awal atau akhir yang lebih pelan dari batas potong (bawaan -40 dBFS) akan dihapus. Geser slider lebih rendah, misalnya -50 dB, agar lebih sedikit yang dipotong.',
    q2: 'Apakah bagian tengah lagu berubah?', a2: 'Tidak. Hanya intro dan outro yang dihapus. File di-encode ulang sebagai MP3, jadi pilih bitrate yang tinggi.',
    q3: 'Apakah tag dan sampul tetap ada?', a3: 'Ya. Judul, artis, album, sampul, dan tag ID3 lainnya disalin dari file asli ke file hasil potong.'
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

const BITRATES = ['128k', '192k', '256k', '320k'];
$('#bitrate').onclick = e => {
  const idx = BITRATES.indexOf(e.currentTarget.value);
  const next = BITRATES[(idx + 1) % BITRATES.length];
  e.currentTarget.value = next;
  e.currentTarget.textContent = `${next}bps MP3`;
  e.currentTarget.setAttribute('aria-label', `Output bitrate: ${next}bps MP3. Click to change`);
};

function refreshDrop() {
  $('#dropTitle').textContent = t(mode === 'file' ? 'dropSingleTitle' : 'dropBatchTitle');
  $('#dropSub').textContent = t(mode === 'file' ? 'dropSingleSub' : 'dropBatchSub');
}
function setBtn() {
  $('#goText').textContent = t(busy ? 'cropping' : 'crop');
}

function renderFileList() {
  const list = $('#fileList');
  list.replaceChildren();
  files.forEach((f, idx) => {
    const item = document.createElement('div');
    item.className = 'file-item';

    const meta = document.createElement('div');
    meta.className = 'file-item-meta';

    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = f.name;

    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = fmtBytes(f.size);

    meta.append(name, size);

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'remove-file-btn';
    rm.textContent = '×';
    rm.onclick = () => {
      files.splice(idx, 1);
      renderFileList();
    };

    item.append(meta, rm);
    list.append(item);
  });

  $('#go').disabled = busy || files.length === 0;
  $('#clearBtn').style.display = files.length > 0 ? 'inline-flex' : 'none';
}

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
}

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
      el.innerHTML = `<div class="top"><div class="info"><h3>${esc(d.name)}</h3><div class="tags">
        <span class="tag">${fmt(d.duration)} → ${fmt(d.end - d.start)}</span>
        <span class="tag hl">${esc(t('removed', (removed / 1000).toFixed(1)))}</span>
        <span class="tag">${esc(t('intro'))} ${(d.start / 1000).toFixed(1)}s</span>
        <span class="tag">${esc(t('outro'))} ${((d.duration - d.end) / 1000).toFixed(1)}s</span>
        <span class="tag">${d.threshold} dB</span>${d.tags ? `<span class="tag">${esc(t('tagsKept'))}</span>` : ''}</div></div>
        <a class="dl" href="/download/${d.id}">${esc(t('dl'))}</a></div><div class="canvas-well"><canvas></canvas></div>`;
    }
    out.appendChild(el);
    const cv = el.querySelector('canvas'); if (cv) draw(cv, d);
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
  document.querySelectorAll('[data-lang]').forEach(b => b.setAttribute('aria-pressed', b.dataset.lang === l));
  refreshDrop(); setBtn(); applyTheme(document.documentElement.dataset.theme);
}
document.querySelectorAll('[data-lang]').forEach(b => b.onclick = () => applyLang(b.dataset.lang));
$('#themeBtn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

document.querySelectorAll('.tab').forEach(tb => tb.onclick = () => {
  mode = tb.dataset.mode;
  document.querySelectorAll('.tab').forEach(x => x.setAttribute('aria-selected', x === tb));
  input.multiple = mode === 'batch';
  files = [];
  last = [];
  failed = false;
  renderFileList();
  render();
  refreshDrop();
});

function addFiles(list) {
  const mp3s = [...list].filter(f => /\.mp3$/i.test(f.name));
  if (!mp3s.length) return;
  if (mode === 'file') {
    files = [mp3s[0]];
  } else {
    files = [...files, ...mp3s];
  }
  renderFileList();
}

drop.onclick = () => input.click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
input.onchange = () => { if (input.files.length) addFiles(input.files); input.value = ''; };
['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => { if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });

$('#clearBtn').onclick = () => {
  files = [];
  last = [];
  failed = false;
  renderFileList();
  render();
};

$('#thr').oninput = e => $('#thrOut').textContent = e.target.value + ' dB';
$('#pad').oninput = e => $('#padOut').textContent = e.target.value + ' ms';

$('#go').onclick = async () => {
  if (!files.length) return;
  busy = true; failed = false; last = []; setBtn(); $('#go').disabled = true; render();
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  fd.append('threshold', $('#thr').value);
  fd.append('padding', $('#pad').value);
  fd.append('bitrate', $('#bitrate').value);
  try {
    const res = await fetch('/api/trim', { method: 'POST', body: fd });
    last = await res.json();
  } catch (e) { failed = true; }
  busy = false; $('#go').disabled = files.length === 0; setBtn(); render();
};

applyLang(lang);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sshhh Vanisher - The Silence Cropper</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.ico">
<link rel="shortcut icon" href="/static/favicon.ico">
<link rel="preconnect" href="https://fvonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800;900&display=swap" rel="stylesheet">
<script>
try {
  const th = localStorage.getItem('theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = th;
} catch (e) { document.documentElement.dataset.theme = 'light'; }
</script>
<style>
  /* =========================================================
     CLAYMORPHISM DESIGN
     ========================================================= */
  :root, :root[data-theme="light"] {
    --bg: #e7ecf5;
    --bg-gradient: radial-gradient(circle at 15% 15%, #f3f7ff 0%, #e4eaf4 55%, #dce4f0 100%);
    --clay-surface: #edf2fa;
    --clay-surface-hover: #f2f6fd;
    --clay-well: #dfe6f2;
    --text: #273043;
    --text-muted: #68758d;

    --shadow-clay-card:
      12px 12px 26px rgba(166, 179, 201, 0.55),
      -10px -10px 22px rgba(255, 255, 255, 0.92),
      inset 4px 4px 8px rgba(255, 255, 255, 0.85),
      inset -4px -4px 10px rgba(176, 189, 212, 0.35);

    --shadow-clay-pill:
      6px 6px 14px rgba(166, 179, 201, 0.45),
      -6px -6px 14px rgba(255, 255, 255, 0.9),
      inset 2px 2px 5px rgba(255, 255, 255, 0.8),
      inset -2px -2px 5px rgba(176, 189, 212, 0.3);

    --shadow-clay-well:
      inset 6px 6px 12px rgba(166, 179, 201, 0.55),
      inset -6px -6px 12px rgba(255, 255, 255, 0.92),
      0 4px 12px rgba(255, 255, 255, 0.5);

    --pri: #0aa4c4;
    --pri-surface: linear-gradient(145deg, #12b8db, #088ba6);
    --shadow-clay-pri:
      8px 10px 20px rgba(10, 164, 196, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.75),
      inset 3px 3px 6px rgba(184, 239, 247, 0.65),
      inset -3px -4px 8px rgba(4, 105, 127, 0.45);

    --acc: #F46096;
    --accent-surface: linear-gradient(145deg, var(--pri), var(--acc));
    --acc-surface: linear-gradient(145deg, #ff74a7, #e04880);
    --shadow-clay-acc:
      6px 8px 16px rgba(244, 96, 150, 0.3),
      -4px -4px 10px rgba(255, 255, 255, 0.85),
      inset 2px 2px 5px rgba(255, 204, 223, 0.75),
      inset -2px -3px 6px rgba(179, 36, 88, 0.38);

    --green-surface: linear-gradient(145deg, #27d366, #1db454);
    --shadow-clay-green:
      8px 10px 20px rgba(34, 197, 94, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.7),
      inset 3px 3px 6px rgba(154, 247, 186, 0.65),
      inset -3px -4px 8px rgba(19, 138, 62, 0.45);

    --cut: #ef4444;
    --err-bg: #ffe8eb;
    --err-ink: #c92a3e;
    --shadow-clay-err:
      8px 8px 18px rgba(201, 42, 62, 0.14),
      -6px -6px 14px rgba(255, 255, 255, 0.85),
      inset 3px 3px 6px rgba(255, 255, 255, 0.8),
      inset -3px -3px 7px rgba(224, 135, 148, 0.3);
  }

  :root[data-theme="dark"] {
    --bg: #1e212b;
    --bg-gradient: radial-gradient(circle at 15% 15%, #262a37 0%, #1d2029 60%, #171921 100%);
    --clay-surface: #252936;
    --clay-surface-hover: #2b303f;
    --clay-well: #1b1e27;
    --text: #f1f5fb;
    --text-muted: #9aa5bc;

    --shadow-clay-card:
      12px 14px 28px rgba(10, 12, 17, 0.68),
      -8px -8px 20px rgba(48, 54, 72, 0.42),
      inset 3px 3px 7px rgba(64, 72, 95, 0.48),
      inset -4px -5px 10px rgba(13, 15, 22, 0.65);

    --shadow-clay-pill:
      6px 7px 14px rgba(10, 12, 17, 0.6),
      -5px -5px 12px rgba(48, 54, 72, 0.38),
      inset 2px 2px 5px rgba(64, 72, 95, 0.45),
      inset -2px -3px 6px rgba(13, 15, 22, 0.6);

    --shadow-clay-well:
      inset 6px 6px 14px rgba(10, 12, 17, 0.72),
      inset -5px -5px 12px rgba(46, 52, 69, 0.35),
      0 2px 6px rgba(48, 54, 72, 0.2);

    --shadow-clay-pri:
      8px 10px 22px rgba(10, 12, 17, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(184, 239, 247, 0.45),
      inset -3px -4px 8px rgba(4, 105, 127, 0.6);

    --shadow-clay-acc:
      6px 8px 16px rgba(10, 12, 17, 0.55),
      -3px -3px 10px rgba(48, 54, 72, 0.25),
      inset 2px 2px 5px rgba(255, 204, 223, 0.55),
      inset -2px -3px 6px rgba(150, 25, 72, 0.55);

    --shadow-clay-green:
      8px 10px 22px rgba(13, 18, 15, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(134, 239, 172, 0.5),
      inset -3px -4px 8px rgba(15, 118, 53, 0.6);

    --cut: #f87171;
    --err-bg: #352128;
    --err-ink: #ff8796;
    --shadow-clay-err:
      8px 10px 20px rgba(10, 12, 17, 0.6),
      -5px -5px 12px rgba(48, 54, 72, 0.3),
      inset 2px 2px 5px rgba(95, 52, 64, 0.45),
      inset -3px -3px 7px rgba(20, 11, 14, 0.65);
  }

  * { box-sizing: border-box; }
  [hidden] { display: none !important; }

  body {
    margin: 0;
    background: var(--bg);
    background-image: var(--bg-gradient);
    background-attachment: fixed;
    color: var(--text);
    font-family: "Nunito", "Inter", system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    transition: background 0.3s ease, color 0.25s ease;
  }

  a { color: inherit; text-decoration: none; }
  main { flex: 1; }

  /* =========================================================
     MOLDED NAVBAR
     ========================================================= */
  .navbar {
    width: 100%;
    max-width: 1020px;
    margin: 18px auto 0;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    background: var(--clay-surface);
    border-radius: 28px;
    box-shadow: var(--shadow-clay-card);
  }

  .logo {
    font-size: 1.38rem;
    font-weight: 900;
    letter-spacing: -0.02em;
    display: flex;
    align-items: center;
    gap: 11px;
    margin-right: auto;
  }
  .logo-mold {
    width: 40px;
    height: 40px;
    border-radius: 15px;
    background: var(--clay-surface);
    box-shadow: var(--shadow-clay-pill);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .logo b { color: var(--pri); }

  .nav-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .nav-btn {
    background: var(--clay-surface);
    border: none;
    color: var(--text);
    font-family: inherit;
    font-size: 0.88rem;
    font-weight: 800;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 9px 15px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.16s ease;
  }
  .nav-btn:hover { transform: translateY(-2px); }
  .nav-btn:active {
    transform: translateY(1px) scale(0.97);
    box-shadow: var(--shadow-clay-well);
  }

  .bitrate-pill {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  .seg {
    display: inline-flex;
    background: var(--clay-well);
    padding: 4px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-well);
    gap: 3px;
  }
  .seg button {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-family: inherit;
    font-weight: 800;
    font-size: 0.8rem;
    padding: 6px 11px;
    border-radius: 999px;
    cursor: pointer;
    transition: all 0.18s ease;
  }
  .seg button[aria-pressed="true"] {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  /* =========================================================
     HERO & MOLDED WORKBENCH
     ========================================================= */
  .hero {
    max-width: 840px;
    margin: 38px auto 48px;
    padding: 0 20px;
    text-align: center;
  }

  /* Pill Tab Switcher (clay.py style with accent-surface gradient) */
  .tabs {
    display: inline-flex;
    align-items: center;
    background: var(--clay-well);
    border-radius: 999px;
    padding: 7px;
    margin-bottom: 26px;
    gap: 6px;
    box-shadow: var(--shadow-clay-well);
  }
  .tab {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-family: inherit;
    font-size: 0.84rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    padding: 10px 22px;
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .tab:hover:not([aria-selected="true"]) { color: var(--text); }
  .tab[aria-selected="true"] {
    background: var(--accent-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-pri);
    transform: translateY(-1px);
  }

  h1 {
    font-size: clamp(2.1rem, 4.8vw, 3rem);
    font-weight: 900;
    margin: 0 0 10px;
    letter-spacing: -0.025em;
  }
  .sub {
    color: var(--text-muted);
    font-size: 1.05rem;
    font-weight: 600;
    margin: 0 0 32px;
  }

  /* Sculpted Clay Workbench Card */
  .workbench {
    background: var(--clay-surface);
    border-radius: 32px;
    padding: 28px;
    box-shadow: var(--shadow-clay-card);
    text-align: left;
  }

  /* Tall Recessed Clay Dropzone Well (from clay.py) */
  .dropzone {
    background: var(--clay-well);
    border-radius: 24px;
    padding: 36px 24px;
    text-align: center;
    box-shadow: var(--shadow-clay-well);
    border: 2.5px dashed rgba(10, 164, 196, 0.35);
    cursor: pointer;
    transition: border-color 0.2s ease, transform 0.2s ease;
  }
  .dropzone:hover, .dropzone.over {
    border-color: var(--pri);
    transform: scale(1.006);
  }
  .drop-icon-mold {
    width: 62px;
    height: 62px;
    margin: 0 auto 14px;
    border-radius: 22px;
    background: var(--clay-surface);
    box-shadow: var(--shadow-clay-pill);
    color: var(--pri);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .dropzone h3 {
    margin: 0 0 6px;
    font-size: 1.15rem;
    font-weight: 900;
  }
  .dropzone p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.9rem;
    font-weight: 700;
  }

  /* Slider Controls Strip */
  .opts {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
    justify-content: space-between;
    margin-top: 22px;
    background: var(--clay-well);
    padding: 15px 22px;
    border-radius: 22px;
    box-shadow: var(--shadow-clay-well);
    color: var(--text-muted);
    font-size: 0.92rem;
    font-weight: 800;
  }
  .opts label {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    min-width: 240px;
  }
  .opts output {
    background: var(--clay-surface);
    color: var(--pri);
    font-weight: 900;
    padding: 5px 12px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    min-width: 72px;
    text-align: center;
  }
  input[type="range"] {
    flex: 1;
    accent-color: var(--pri);
    cursor: pointer;
  }

  /* Queued File Chips (from clay.py) */
  .file-list {
    margin-top: 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 240px;
    overflow-y: auto;
    padding: 4px;
  }
  .file-item {
    background: var(--clay-surface);
    border-radius: 18px;
    padding: 11px 16px;
    box-shadow: var(--shadow-clay-pill);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-size: 0.9rem;
    font-weight: 700;
  }
  .file-item-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .file-name {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-size {
    color: var(--text-muted);
    font-size: 0.8rem;
    flex-shrink: 0;
  }
  .remove-file-btn {
    background: var(--clay-well);
    border: none;
    color: var(--text-muted);
    width: 28px;
    height: 28px;
    border-radius: 50%;
    cursor: pointer;
    font-weight: 900;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .remove-file-btn:hover { color: var(--err-ink); }

  /* Primary Action Bar: Crop Silence (with Scissors Icon) + Clear */
  .action-bar {
    margin-top: 22px;
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .go {
    flex: 1;
    background: var(--pri);
    color: #fff;
    border: none;
    border-radius: 22px;
    padding: 16px 26px;
    font-family: inherit;
    font-size: 1.04rem;
    font-weight: 900;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    box-shadow: var(--shadow-clay-pri);
    transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1), filter 0.15s ease;
  }
  .go:hover:not(:disabled) {
    transform: translateY(-2px);
    filter: brightness(1.04);
  }
  .go:active:not(:disabled) {
    transform: translateY(2px) scale(0.98);
  }
  .go:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-clear {
    background: var(--clay-surface);
    color: var(--text-muted);
    border: none;
    border-radius: 22px;
    padding: 16px 22px;
    font-family: inherit;
    font-size: 0.94rem;
    font-weight: 800;
    cursor: pointer;
    box-shadow: var(--shadow-clay-pill);
    transition: transform 0.15s ease, color 0.15s ease;
  }
  .btn-clear:hover { color: var(--text); transform: translateY(-1px); }

  /* =========================================================
     RESULTS & WAVEFORM CARDS
     ========================================================= */
  #results {
    margin-top: 30px;
    display: grid;
    gap: 20px;
  }

  .item {
    background: var(--clay-surface);
    border-radius: 28px;
    padding: 24px;
    box-shadow: var(--shadow-clay-card);
    text-align: left;
  }

  .top {
    display: flex;
    gap: 18px;
    align-items: center;
    flex-wrap: wrap;
  }
  .info { flex: 1; min-width: 220px; }
  .info h3 {
    margin: 0 0 10px;
    font-size: 1.22rem;
    font-weight: 900;
    overflow-wrap: anywhere;
  }

  .tags { display: flex; gap: 8px; flex-wrap: wrap; }
  .tag {
    background: var(--clay-well);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 0.8rem;
    font-weight: 800;
    color: var(--text-muted);
    box-shadow: inset 2px 2px 5px rgba(0, 0, 0, 0.06), inset -2px -2px 5px rgba(255, 255, 255, 0.25);
  }
  .tag.hl {
    background: var(--acc-surface);
    color: #fff;
    box-shadow: var(--shadow-clay-acc);
  }

  .dl {
    background: var(--green-surface);
    color: #fff;
    font-weight: 900;
    font-size: 0.94rem;
    border-radius: 18px;
    padding: 13px 24px;
    white-space: nowrap;
    box-shadow: var(--shadow-clay-green);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .dl:hover { transform: translateY(-2px); }

  .canvas-well {
    margin-top: 18px;
    background: var(--clay-well);
    border-radius: 20px;
    padding: 12px 14px;
    box-shadow: var(--shadow-clay-well);
  }
  canvas {
    width: 100%;
    height: 86px;
    display: block;
  }

  .err {
    background: var(--err-bg);
    color: var(--err-ink);
    box-shadow: var(--shadow-clay-err);
    padding: 13px 20px;
    border-radius: 18px;
    font-weight: 800;
    margin: 0;
  }

  .zip {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 6px auto 0;
    background: var(--acc-surface);
    color: #fff;
    font-weight: 900;
    font-size: 0.98rem;
    border-radius: 22px;
    padding: 15px 30px;
    box-shadow: var(--shadow-clay-acc);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .zip:hover { transform: translateY(-2px); }

  /* Molded FAQ Section */
  #faq {
    margin: 48px auto 0;
    max-width: 840px;
    background: var(--clay-surface);
    border-radius: 32px;
    padding: 28px 32px;
    box-shadow: var(--shadow-clay-card);
    color: var(--text-muted);
    line-height: 1.65;
    text-align: left;
    font-weight: 600;
  }
  #faq h2 {
    margin-top: 0;
    color: var(--pri);
    font-size: 1.35rem;
    font-weight: 900;
  }
  #faq b { color: var(--text); font-weight: 800; }

  footer {
    text-align: center;
    color: var(--text-muted);
    padding: 26px 20px 32px;
    font-size: 0.9rem;
    font-weight: 700;
  }

  button:focus-visible, .dropzone:focus-visible, a:focus-visible {
    outline: 3px solid var(--acc);
    outline-offset: 2px;
  }

  @media (max-width: 700px) {
    .navbar { margin: 12px 14px 0; width: auto; padding: 12px 16px; border-radius: 22px; }
    .nav-faq { display: none; }
    .workbench { padding: 20px; border-radius: 26px; }
    .action-bar { flex-direction: column; }
    .go, .btn-clear { width: 100%; }
  }
</style>
</head>
<body>

<header class="navbar">
  <a href="/" class="logo">
    <span class="logo-mold">
      <img src="/static/favicon.ico" width="24" height="24" alt="">
    </span>
    <div><b>sshhh</b>Vanisher</div>
  </a>
  <div class="nav-right">
    <button type="button" class="nav-btn bitrate-pill" id="bitrate" value="320k" aria-label="Output bitrate: 320kbps MP3. Click to change">320kbps MP3</button>
    <a class="nav-btn nav-faq" href="#faq">FAQ</a>
    <div class="seg" role="group" aria-label="Language">
      <button type="button" data-lang="en" aria-pressed="true">EN</button>
      <button type="button" data-lang="id" aria-pressed="false">ID</button>
    </div>
    <button type="button" class="nav-btn" id="themeBtn"></button>
  </div>
</header>

<main>
  <section class="hero">
    <!-- Pill Tab Switcher with Music Note & Folder Icons + accent-surface gradient -->
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" aria-selected="true" data-mode="file">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
        <span data-i18n="tabFile">SINGLE MP3</span>
      </button>
      <button class="tab" role="tab" aria-selected="false" data-mode="batch">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span data-i18n="tabBatch">BATCH / FOLDER</span>
      </button>
    </div>

    <h1>Silence Cropper</h1>
    <p class="sub" data-i18n="sub"></p>

    <div class="workbench">
      <input type="file" id="file" accept=".mp3,audio/mpeg" hidden>

      <!-- Tall Recessed Clay Dropzone Well -->
      <div class="dropzone" id="drop" tabindex="0" role="button">
        <div class="drop-icon-mold">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
        </div>
        <h3 id="dropTitle"></h3>
        <p id="dropSub"></p>
      </div>

      <!-- Cut Below & Keep Padding Sliders -->
      <div class="opts">
        <label><span data-i18n="cut"></span> <input type="range" id="thr" min="-60" max="-5" value="-40"><output id="thrOut">-40 dB</output></label>
        <label><span data-i18n="pad"></span> <input type="range" id="pad" min="0" max="500" step="10" value="0"><output id="padOut">0 ms</output></label>
      </div>

      <!-- Queued File Chips -->
      <div class="file-list" id="fileList"></div>

      <!-- Primary Action Bar with Scissors Icon -->
      <div class="action-bar">
        <button type="button" class="go" id="go" disabled>
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>
          </svg>
          <span id="goText"></span>
        </button>
        <button type="button" class="btn-clear" id="clearBtn" style="display:none;" data-i18n="btnClear">Clear</button>
      </div>
    </div>

    <div id="results" aria-live="polite"></div>

    <section id="faq">
      <h2>FAQ</h2>
      <p><b data-i18n="q1"></b><br><span data-i18n="a1"></span></p>
      <p><b data-i18n="q2"></b><br><span data-i18n="a2"></span></p>
      <p><b data-i18n="q3"></b><br><span data-i18n="a3"></span></p>
    </section>
  </section>
</main>

<footer>All rights reserved &copy; 2026 ikhwnfzan.</footer>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = ms => { const s = Math.round(ms / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const fmtBytes = b => b < 1024 * 1024 ? (b / 1024).toFixed(1) + ' KB' : (b / (1024 * 1024)).toFixed(2) + ' MB';

const T = {
  en: {
    tabFile: 'SINGLE MP3', tabBatch: 'BATCH / FOLDER',
    sub: 'Cut the quiet intro and outro off your MP3s. Nothing leaves your computer.',
    dropSingleTitle: 'Drop an MP3 file here or click to browse',
    dropSingleSub: 'Supports 320kbps MP3 with embedded ID3 cover art',
    dropBatchTitle: 'Drop multiple MP3 files here or click to select',
    dropBatchSub: 'Select multiple .mp3 tracks to trim in batch and download as ZIP',
    crop: 'Crop Silence', cropping: 'Cropping silence…', btnClear: 'Clear',
    cut: 'Cut below', pad: 'Keep padding',
    tagsKept: 'Tags kept', removed: s => s + 's removed', intro: 'Intro', outro: 'Outro',
    dl: '⬇ Download MP3', zip: 'Download all as ZIP',
    errFile: 'Could not trim this file:', errReq: 'Request failed. Is the server running?',
    toLight: 'Switch to light theme', toDark: 'Switch to dark theme',
    q1: 'What does -40 dB mean?', a1: 'Any part at the start or end that is quieter than the cut level (default -40 dBFS) is removed. Move the slider lower, for example -50 dB, to cut less.',
    q2: 'Is the middle of the song changed?', a2: 'No. Only the intro and outro are removed. The file is re-encoded as MP3, so pick a high bitrate.',
    q3: 'Are tags and cover art kept?', a3: 'Yes. Title, artist, album, cover art and other ID3 tags are copied from the original to the trimmed file.'
  },
  id: {
    tabFile: 'SATU MP3', tabBatch: 'BATCH / BANYAK FILE',
    sub: 'Potong intro dan outro yang sunyi dari MP3 kamu. Tidak ada yang keluar dari komputermu.',
    dropSingleTitle: 'Tarik satu file MP3 ke sini atau klik untuk memilih',
    dropSingleSub: 'Mendukung MP3 320kbps dengan cover art ID3 tertanam',
    dropBatchTitle: 'Tarik banyak file MP3 ke sini atau klik untuk memilih',
    dropBatchSub: 'Pilih beberapa file .mp3 sekaligus untuk dipotong dan diunduh sebagai ZIP',
    crop: 'Potong Hening', cropping: 'Memotong hening…', btnClear: 'Hapus',
    cut: 'Potong di bawah', pad: 'Sisakan jeda',
    tagsKept: 'Tag dipertahankan', removed: s => s + ' dtk dipotong', intro: 'Intro', outro: 'Outro',
    dl: '⬇ Unduh MP3', zip: 'Unduh semua (ZIP)',
    errFile: 'Gagal memotong file ini:', errReq: 'Permintaan gagal. Apakah server berjalan?',
    toLight: 'Ganti ke tema terang', toDark: 'Ganti ke tema gelap',
    q1: 'Apa arti -40 dB?', a1: 'Bagian awal atau akhir yang lebih pelan dari batas potong (bawaan -40 dBFS) akan dihapus. Geser slider lebih rendah, misalnya -50 dB, agar lebih sedikit yang dipotong.',
    q2: 'Apakah bagian tengah lagu berubah?', a2: 'Tidak. Hanya intro dan outro yang dihapus. File di-encode ulang sebagai MP3, jadi pilih bitrate yang tinggi.',
    q3: 'Apakah tag dan sampul tetap ada?', a3: 'Ya. Judul, artis, album, sampul, dan tag ID3 lainnya disalin dari file asli ke file hasil potong.'
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

const BITRATES = ['128k', '192k', '256k', '320k'];
$('#bitrate').onclick = e => {
  const idx = BITRATES.indexOf(e.currentTarget.value);
  const next = BITRATES[(idx + 1) % BITRATES.length];
  e.currentTarget.value = next;
  e.currentTarget.textContent = `${next}bps MP3`;
  e.currentTarget.setAttribute('aria-label', `Output bitrate: ${next}bps MP3. Click to change`);
};

function refreshDrop() {
  $('#dropTitle').textContent = t(mode === 'file' ? 'dropSingleTitle' : 'dropBatchTitle');
  $('#dropSub').textContent = t(mode === 'file' ? 'dropSingleSub' : 'dropBatchSub');
}
function setBtn() {
  $('#goText').textContent = t(busy ? 'cropping' : 'crop');
}

function renderFileList() {
  const list = $('#fileList');
  list.replaceChildren();
  files.forEach((f, idx) => {
    const item = document.createElement('div');
    item.className = 'file-item';

    const meta = document.createElement('div');
    meta.className = 'file-item-meta';

    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = f.name;

    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = fmtBytes(f.size);

    meta.append(name, size);

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'remove-file-btn';
    rm.textContent = '×';
    rm.onclick = () => {
      files.splice(idx, 1);
      renderFileList();
    };

    item.append(meta, rm);
    list.append(item);
  });

  $('#go').disabled = busy || files.length === 0;
  $('#clearBtn').style.display = files.length > 0 ? 'inline-flex' : 'none';
}

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
}

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
      el.innerHTML = `<div class="top"><div class="info"><h3>${esc(d.name)}</h3><div class="tags">
        <span class="tag">${fmt(d.duration)} → ${fmt(d.end - d.start)}</span>
        <span class="tag hl">${esc(t('removed', (removed / 1000).toFixed(1)))}</span>
        <span class="tag">${esc(t('intro'))} ${(d.start / 1000).toFixed(1)}s</span>
        <span class="tag">${esc(t('outro'))} ${((d.duration - d.end) / 1000).toFixed(1)}s</span>
        <span class="tag">${d.threshold} dB</span>${d.tags ? `<span class="tag">${esc(t('tagsKept'))}</span>` : ''}</div></div>
        <a class="dl" href="/download/${d.id}">${esc(t('dl'))}</a></div><div class="canvas-well"><canvas></canvas></div>`;
    }
    out.appendChild(el);
    const cv = el.querySelector('canvas'); if (cv) draw(cv, d);
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
  document.querySelectorAll('[data-lang]').forEach(b => b.setAttribute('aria-pressed', b.dataset.lang === l));
  refreshDrop(); setBtn(); applyTheme(document.documentElement.dataset.theme);
}
document.querySelectorAll('[data-lang]').forEach(b => b.onclick = () => applyLang(b.dataset.lang));
$('#themeBtn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

document.querySelectorAll('.tab').forEach(tb => tb.onclick = () => {
  mode = tb.dataset.mode;
  document.querySelectorAll('.tab').forEach(x => x.setAttribute('aria-selected', x === tb));
  input.multiple = mode === 'batch';
  files = [];
  last = [];
  failed = false;
  renderFileList();
  render();
  refreshDrop();
});

function addFiles(list) {
  const mp3s = [...list].filter(f => /\.mp3$/i.test(f.name));
  if (!mp3s.length) return;
  if (mode === 'file') {
    files = [mp3s[0]];
  } else {
    files = [...files, ...mp3s];
  }
  renderFileList();
}

drop.onclick = () => input.click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
input.onchange = () => { if (input.files.length) addFiles(input.files); input.value = ''; };
['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => { if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });

$('#clearBtn').onclick = () => {
  files = [];
  last = [];
  failed = false;
  renderFileList();
  render();
};

$('#thr').oninput = e => $('#thrOut').textContent = e.target.value + ' dB';
$('#pad').oninput = e => $('#padOut').textContent = e.target.value + ' ms';

$('#go').onclick = async () => {
  if (!files.length) return;
  busy = true; failed = false; last = []; setBtn(); $('#go').disabled = true; render();
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  fd.append('threshold', $('#thr').value);
  fd.append('padding', $('#pad').value);
  fd.append('bitrate', $('#bitrate').value);
  try {
    const res = await fetch('/api/trim', { method: 'POST', body: fd });
    last = await res.json();
  } catch (e) { failed = true; }
  busy = false; $('#go').disabled = files.length === 0; setBtn(); render();
};

applyLang(lang);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
