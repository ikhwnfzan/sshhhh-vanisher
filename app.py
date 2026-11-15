import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile

from flask import Flask, jsonify, render_template_string, request, send_file
from mutagen.id3 import ID3, error as ID3Error
from mutagen.mp3 import MP3
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload

JOBS: dict[str, dict] = {}
WORK_DIR = os.path.join(tempfile.gettempdir(), "silence_cropper_jobs")
os.makedirs(WORK_DIR, exist_ok=True)


def _sanitize(name: str) -> str:
    base = secure_filename(name)
    return base or "audio.mp3"


def _crop_silence_mp3(input_path: str, output_path: str, threshold_db: float = -40.0, bitrate: int = 320):
    """
    Trim intro & outro silence below threshold_db using FFmpeg silenceremove,
    encode at 320kbps MP3, and copy over original ID3 tags (including cover art).
    """
    # Trim leading silence, reverse, trim trailing silence, reverse back
    af_filter = (
        f"silenceremove=start_periods=1:start_duration=0:start_threshold={threshold_db}dB,"
        f"areverse,"
        f"silenceremove=start_periods=1:start_duration=0:start_threshold={threshold_db}dB,"
        f"areverse"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-af",
        af_filter,
        "-codec:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate}k",
        "-map_metadata",
        "0",
        "-id3v2_version",
        "3",
        output_path,
    ]
    subprocess.run(cmd, check=True)

    # Ensure full ID3v2 tags (such as APIC album art) are preserved cleanly
    try:
        src_tags = ID3(input_path)
        dst_audio = MP3(output_path, ID3=ID3)
        try:
            dst_audio.add_tags()
        except ID3Error:
            pass
        for frame in src_tags.values():
            dst_audio.tags.add(frame)
        dst_audio.save(v2_version=3)
    except Exception:
        pass


def _process_job(job_id: str, saved_inputs: list[tuple[str, str]], threshold_db: float, bitrate: int):
    job_dir = os.path.join(WORK_DIR, job_id)
    out_dir = os.path.join(job_dir, "out")
    os.makedirs(out_dir, exist_ok=True)

    total = len(saved_inputs)
    processed_files: list[tuple[str, str]] = []

    try:
        for idx, (orig_name, in_path) in enumerate(saved_inputs, start=1):
            JOBS[job_id]["completed"] = idx - 1
            JOBS[job_id]["currentFile"] = orig_name

            stem, _ = os.path.splitext(orig_name)
            out_name = f"{stem}_cropped.mp3"
            out_path = os.path.join(out_dir, f"{idx:03d}_{_sanitize(out_name)}")

            try:
                _crop_silence_mp3(in_path, out_path, threshold_db=threshold_db, bitrate=bitrate)
                if os.path.exists(out_path):
                    processed_files.append((out_path, out_name))
            except Exception as e:
                if total == 1:
                    raise e
                continue

        if not processed_files:
            JOBS[job_id] = {"status": "failed", "error": "FFmpeg failed to crop audio."}
            return

        if len(processed_files) == 1:
            final_path, final_name = processed_files[0]
            JOBS[job_id] = {
                "status": "ready",
                "completed": 1,
                "total": 1,
                "filePath": final_path,
                "filename": final_name,
                "mimetype": "audio/mpeg",
                "downloadLink": f"/api/download/{job_id}",
            }
        else:
            zip_path = os.path.join(job_dir, "cropped_tracks.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for full_path, arcname in processed_files:
                    zf.write(full_path, arcname=arcname)

            JOBS[job_id] = {
                "status": "ready",
                "completed": total,
                "total": total,
                "filePath": zip_path,
                "filename": "cropped_mp3_batch.zip",
                "mimetype": "application/zip",
                "downloadLink": f"/api/download/{job_id}",
            }
    except Exception as e:
        JOBS[job_id] = {"status": "failed", "error": str(e)}


@app.post("/api/crop")
def start_crop():
    files = request.files.getlist("files")
    valid_files = [f for f in files if f and f.filename.lower().endswith(".mp3")]

    if not valid_files:
        return jsonify(error="Please upload at least one .mp3 file."), 400

    try:
        threshold_db = float(request.form.get("threshold", -40))
    except ValueError:
        threshold_db = -40.0

    try:
      bitrate = int(request.form.get("bitrate", 320))
    except ValueError:
      bitrate = 320
    if bitrate not in {128, 192, 256, 320}:
      bitrate = 320

    job_id = uuid.uuid4().hex
    in_dir = os.path.join(WORK_DIR, job_id, "in")
    os.makedirs(in_dir, exist_ok=True)

    saved_inputs: list[tuple[str, str]] = []
    for idx, f in enumerate(valid_files, start=1):
        orig_name = os.path.basename(f.filename)
        safe_in = os.path.join(in_dir, f"{idx:03d}_{_sanitize(orig_name)}")
        f.save(safe_in)
        saved_inputs.append((orig_name, safe_in))

    JOBS[job_id] = {
        "status": "pending",
        "completed": 0,
        "total": len(saved_inputs),
        "currentFile": saved_inputs[0][0],
    }

    threading.Thread(
        target=_process_job,
        args=(job_id, saved_inputs, threshold_db, bitrate),
        daemon=True,
    ).start()

    return jsonify(jobId=job_id, status="pending", total=len(saved_inputs)), 202


@app.get("/api/status/<job_id>")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(
        status=job["status"],
        completed=job.get("completed", 0),
        total=job.get("total", 1),
        currentFile=job.get("currentFile", ""),
        downloadLink=job.get("downloadLink"),
        filename=job.get("filename"),
        error=job.get("error"),
    )


@app.get("/api/download/<job_id>")
def download_result(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "ready":
        return "File not ready", 404
    return send_file(
        job["filePath"],
        as_attachment=True,
        download_name=job["filename"],
        mimetype=job.get("mimetype", "application/octet-stream"),
    )


@app.get("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/static/favicon.ico">
<title>sshhhh Vanisher - Crop the Silence In Your MP3</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@600;700;800;900&display=swap" rel="stylesheet">
<style>
  /* =========================================================
     CLAYMORPHISM (MOLD / CLAY) DESIGN SYSTEM
     ========================================================= */
  :root {
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

    --accent: #0aa4c4;
    --secondary: #F46096;
    --accent-surface: linear-gradient(145deg, var(--accent), var(--secondary));
    --shadow-clay-accent:
      8px 10px 20px rgba(10, 164, 196, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.75),
      inset 3px 3px 6px rgba(184, 239, 247, 0.65),
      inset -3px -4px 8px rgba(4, 105, 127, 0.45);

    --green-surface: linear-gradient(145deg, #27d366, #1db454);
    --shadow-clay-green:
      8px 10px 20px rgba(34, 197, 94, 0.34),
      -4px -4px 12px rgba(255, 255, 255, 0.7),
      inset 3px 3px 6px rgba(154, 247, 186, 0.65),
      inset -3px -4px 8px rgba(19, 138, 62, 0.45);

    --yellow-surface: linear-gradient(145deg, #ffe066, #fcc419);
    --yellow-ink: #3d2e00;
    --shadow-clay-yellow:
      6px 6px 14px rgba(224, 172, 16, 0.28),
      -4px -4px 10px rgba(255, 255, 255, 0.85),
      inset 2px 2px 5px rgba(255, 250, 214, 0.8),
      inset -2px -3px 6px rgba(204, 150, 0, 0.35);

    --err-bg: #ffe8eb;
    --err-ink: #c92a3e;
    --shadow-clay-err:
      8px 8px 18px rgba(201, 42, 62, 0.14),
      -6px -6px 14px rgba(255, 255, 255, 0.85),
      inset 3px 3px 6px rgba(255, 255, 255, 0.8),
      inset -3px -3px 7px rgba(224, 135, 148, 0.3);
  }

  html[data-theme="dark"] {
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

    --shadow-clay-accent:
      8px 10px 22px rgba(10, 12, 17, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(184, 239, 247, 0.45),
      inset -3px -4px 8px rgba(4, 105, 127, 0.6);

    --shadow-clay-green:
      8px 10px 22px rgba(13, 18, 15, 0.65),
      -4px -4px 12px rgba(48, 54, 72, 0.25),
      inset 3px 3px 6px rgba(134, 239, 172, 0.5),
      inset -3px -4px 8px rgba(15, 118, 53, 0.6);

    --shadow-clay-yellow:
      6px 8px 16px rgba(10, 12, 17, 0.55),
      -3px -3px 10px rgba(48, 54, 72, 0.25),
      inset 2px 2px 5px rgba(255, 248, 196, 0.75),
      inset -2px -3px 6px rgba(184, 134, 0, 0.5);

    --err-bg: #352128;
    --err-ink: #ff8796;
    --shadow-clay-err:
      8px 10px 20px rgba(10, 12, 17, 0.6),
      -5px -5px 12px rgba(48, 54, 72, 0.3),
      inset 2px 2px 5px rgba(95, 52, 64, 0.45),
      inset -3px -3px 7px rgba(20, 11, 14, 0.65);
  }

  * { box-sizing: border-box; }

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

  main { flex: 1; }

  /* Molded Navbar */
  .navbar {
    width: 100%;
    max-width: 980px;
    margin: 18px auto 0;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--clay-surface);
    border-radius: 28px;
    box-shadow: var(--shadow-clay-card);
  }

  .brand {
    font-size: 1.38rem;
    font-weight: 900;
    letter-spacing: -0.02em;
    text-decoration: none;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-icon-mold {
    width: 42px;
    height: 42px;
    border-radius: 16px;
    background: var(--accent);
    color: #fff;
    box-shadow: var(--shadow-clay-accent);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .brand span { color: var(--accent); }

  .nav-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .badge-hq {
    background: var(--yellow-surface);
    color: var(--yellow-ink);
    font-weight: 800;
    font-size: 0.8rem;
    padding: 8px 15px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-yellow);
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
    gap: 7px;
    padding: 9px 15px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .nav-btn:hover { transform: translateY(-2px); }
  .nav-btn:active {
    transform: translateY(1px) scale(0.97);
    box-shadow: var(--shadow-clay-well);
  }
  .bitrate-btn {
    background: var(--secondary);
    color: #fff;
    box-shadow: var(--shadow-clay-accent);
  }
  .bitrate-btn:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }

  /* Hero & Sculpted Workbench */
  .hero {
    max-width: 780px;
    margin: 38px auto 36px;
    padding: 0 20px;
    text-align: center;
  }

  /* Mode Switcher Well */
  .pill-bar {
    display: inline-flex;
    align-items: center;
    background: var(--clay-well);
    border-radius: 999px;
    padding: 7px;
    margin-bottom: 26px;
    gap: 6px;
    box-shadow: var(--shadow-clay-well);
  }

  .pill-tab {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-family: inherit;
    font-size: 0.82rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    padding: 10px 20px;
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .pill-tab:hover:not(.active) { color: var(--text); }
  .pill-tab.active {
    background: var(--accent-surface);
    color: #ffffff;
    box-shadow: var(--shadow-clay-accent);
    transform: translateY(-1px);
  }

  .hero h1 {
    font-size: clamp(2rem, 4.6vw, 2.85rem);
    font-weight: 900;
    margin: 0 0 10px;
    letter-spacing: -0.025em;
  }
  .hero p.subtitle {
    color: var(--text-muted);
    font-size: 1.04rem;
    font-weight: 600;
    margin: 0 0 30px;
  }

  /* Main Molded Card */
  .workbench-card {
    background: var(--clay-surface);
    border-radius: 32px;
    padding: 28px;
    box-shadow: var(--shadow-clay-card);
    text-align: left;
  }

  /* Recessed Clay Dropzone Well */
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
  .dropzone:hover, .dropzone.dragover {
    border-color: var(--accent);
    transform: scale(1.006);
  }
  .drop-icon-mold {
    width: 62px;
    height: 62px;
    margin: 0 auto 14px;
    border-radius: 22px;
    background: var(--clay-surface);
    box-shadow: var(--shadow-clay-pill);
    color: var(--accent);
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

  /* Threshold Control Strip */
  .controls-row {
    margin-top: 22px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    background: var(--clay-well);
    padding: 14px 20px;
    border-radius: 22px;
    box-shadow: var(--shadow-clay-well);
  }
  .threshold-info {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 800;
    font-size: 0.92rem;
  }
  .db-badge {
    background: var(--clay-surface);
    color: var(--accent);
    padding: 5px 12px;
    border-radius: 999px;
    box-shadow: var(--shadow-clay-pill);
    font-weight: 900;
    min-width: 64px;
    text-align: center;
  }
  input[type="range"] {
    flex: 1;
    min-width: 160px;
    accent-color: var(--accent);
    cursor: pointer;
  }

  /* Queued File Chips */
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

  /* Primary Squishy Action Buttons */
  .action-bar {
    margin-top: 22px;
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .btn-primary {
    flex: 1;
    background: var(--accent);
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
    box-shadow: var(--shadow-clay-accent);
    transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1), filter 0.15s ease;
  }
  .btn-primary:hover:not(:disabled) {
    transform: translateY(-2px);
    filter: brightness(1.04);
  }
  .btn-primary:active:not(:disabled) {
    transform: translateY(2px) scale(0.98);
  }
  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-clear {
    background: var(--clay-surface);
    color: var(--text-muted);
    border: none;
    border-radius: 22px;
    padding: 16px 20px;
    font-family: inherit;
    font-size: 0.94rem;
    font-weight: 800;
    cursor: pointer;
    box-shadow: var(--shadow-clay-pill);
  }
  .btn-clear:hover { color: var(--text); }

  /* Output Result Banner */
  .result-box {
    margin-top: 22px;
    background: var(--clay-well);
    border-radius: 24px;
    padding: 20px 22px;
    box-shadow: var(--shadow-clay-well);
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .result-box.visible { display: flex; }
  .result-info h4 {
    margin: 0 0 4px;
    font-size: 1.05rem;
    font-weight: 900;
  }
  .result-info p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.88rem;
    font-weight: 700;
  }

  .btn-download {
    background: var(--green-surface);
    color: #fff;
    text-decoration: none;
    border-radius: 18px;
    padding: 13px 22px;
    font-weight: 900;
    font-size: 0.95rem;
    box-shadow: var(--shadow-clay-green);
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .btn-download:hover { transform: translateY(-2px); }

  .error-pill {
    margin-top: 18px;
    background: var(--err-bg);
    color: var(--err-ink);
    box-shadow: var(--shadow-clay-err);
    padding: 13px 20px;
    border-radius: 18px;
    font-weight: 800;
    font-size: 0.92rem;
    display: none;
    text-align: center;
  }
  .error-pill.visible { display: block; }

  .site-footer {
    text-align: center;
    padding: 20px;
    color: var(--text-muted);
    font-size: 0.86rem;
    font-weight: 700;
  }

  @media (max-width: 640px) {
    .navbar { margin: 12px 14px 0; width: auto; padding: 12px 16px; border-radius: 22px; }
    .badge-hq { display: none; }
    .workbench-card { padding: 20px; border-radius: 26px; }
    .action-bar { flex-direction: column; }
    .btn-primary, .btn-clear { width: 100%; }
  }
</style>
</head>
<body>

<header class="navbar">
  <a href="/" class="brand">
    <span class="brand-icon-mold">
      <img src="/static/favicon.ico" width="22" height="22" alt="">
    </span>
    <div><span>Silence</span> Cropper</div>
  </a>
  <div class="nav-right">
    <button type="button" class="nav-btn bitrate-btn" id="bitrate-select" value="320" aria-label="MP3 bitrate: 320 kbps. Click to change">320 kbps</button>
    <button type="button" class="nav-btn" id="lang-toggle">
      <span id="lang-label">EN</span>
    </button>
    <button type="button" class="nav-btn" id="theme-toggle" aria-label="Toggle theme">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
    </button>
  </div>
</header>

<main>
  <section class="hero">
    <!-- Molded Mode Selector -->
    <div class="pill-bar" role="tablist">
      <button type="button" class="pill-tab active" data-mode="single">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
        <span data-i18n="tab_single">SINGLE MP3</span>
      </button>
      <button type="button" class="pill-tab" data-mode="batch">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span data-i18n="tab_batch">BATCH / FOLDER</span>
      </button>
    </div>

    <h1 data-i18n="hero_title">Auto Silence Cropper</h1>
    <p class="subtitle" data-i18n="hero_sub">Trim intro &amp; outro silence below -40dB while preserving 320kbps audio &amp; ID3 tags</p>

    <div class="workbench-card">
      <input type="file" id="file-input" accept=".mp3,audio/mpeg" hidden>

      <div class="dropzone" id="dropzone">
        <div class="drop-icon-mold">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
        </div>
        <h3 id="drop-title" data-i18n="drop_single_title">Drop an MP3 file here or click to browse</h3>
        <p id="drop-sub" data-i18n="drop_single_sub">Supports 320kbps MP3 with embedded ID3 cover art</p>
      </div>

      <div class="controls-row">
        <div class="threshold-info">
          <span data-i18n="threshold_label">Silence Threshold:</span>
          <span class="db-badge" id="db-display">-40 dB</span>
        </div>
        <input type="range" id="threshold-slider" min="-50" max="-5" step="1" value="-40">
      </div>

      <div class="file-list" id="file-list"></div>

      <div class="action-bar">
        <button type="button" class="btn-primary" id="crop-btn" disabled>
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>
          </svg>
          <span id="crop-btn-text" data-i18n="btn_crop">Crop Silence</span>
        </button>
        <button type="button" class="btn-clear" id="clear-btn" style="display:none;" data-i18n="btn_clear">Clear</button>
      </div>

      <div class="error-pill" id="error-box"></div>

      <div class="result-box" id="result-box">
        <div class="result-info">
          <h4 id="result-title">Ready!</h4>
          <p id="result-sub">Cropped at 320kbps with ID3 tags preserved.</p>
        </div>
        <a href="#" class="btn-download" id="download-link" download>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          <span id="download-text" data-i18n="btn_download">Download File</span>
        </a>
      </div>
    </div>
  </section>
</main>

<footer class="site-footer">
  Silence Cropper • FFmpeg -40dB Auto Intro/Outro Trimmer
</footer>

<script>
const $ = (id) => document.getElementById(id);
let mode = "single"; // "single" | "batch"
let selectedFiles = [];
let lang = "en";

const I18N = {
  en: {
    tab_single: "SINGLE MP3",
    tab_batch: "BATCH / FOLDER",
    hero_title: "Auto Silence Cropper",
    hero_sub: "Trim intro & outro silence below -40dB while preserving 320kbps audio & ID3 tags",
    drop_single_title: "Drop an MP3 file here or click to browse",
    drop_single_sub: "Select a single .mp3 file to trim leading and trailing silence",
    drop_batch_title: "Drop multiple MP3 files here or click to select",
    drop_batch_sub: "Select multiple .mp3 tracks to crop in batch and download as ZIP",
    threshold_label: "Silence Threshold:",
    btn_crop: "Crop Silence",
    btn_clear: "Clear",
    btn_download: "Download File",
    processing: "Cropping silence…",
    ready_title: "Cropping Complete!",
    ready_sub: "Trimmed at 320kbps with original ID3 tags preserved."
  },
  id: {
    tab_single: "SATU MP3",
    tab_batch: "BATCH / BANYAK FILE",
    hero_title: "Auto Silence Cropper",
    hero_sub: "Potong hening intro & outro di bawah -40dB dengan kualitas 320kbps & tag ID3 utuh",
    drop_single_title: "Tarik satu file MP3 ke sini atau klik untuk memilih",
    drop_single_sub: "Pilih satu file .mp3 untuk memotong bagian hening awal & akhir",
    drop_batch_title: "Tarik banyak file MP3 ke sini atau klik untuk memilih",
    drop_batch_sub: "Pilih beberapa file .mp3 sekaligus untuk diproses dan diunduh sebagai ZIP",
    threshold_label: "Ambang Hening:",
    btn_crop: "Potong Hening",
    btn_clear: "Hapus",
    btn_download: "Unduh Hasil",
    processing: "Memotong bagian hening…",
    ready_title: "Selesai Dipotong!",
    ready_sub: "Audio 320kbps & metadata ID3 tetap terjaga."
  }
};

function applyLang() {
  const dict = I18N[lang];
  $("lang-label").textContent = lang.toUpperCase();
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const k = el.dataset.i18n;
    if (dict[k]) el.textContent = dict[k];
  });
  updateDropText();
}

function updateDropText() {
  const dict = I18N[lang];
  if (mode === "single") {
    $("drop-title").textContent = dict.drop_single_title;
    $("drop-sub").textContent = dict.drop_single_sub;
    $("file-input").removeAttribute("multiple");
  } else {
    $("drop-title").textContent = dict.drop_batch_title;
    $("drop-sub").textContent = dict.drop_batch_sub;
    $("file-input").setAttribute("multiple", "multiple");
  }
}

document.querySelectorAll(".pill-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".pill-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    mode = btn.dataset.mode;
    selectedFiles = [];
    renderFileList();
    updateDropText();
    hideFeedback();
  });
});

$("lang-toggle").addEventListener("click", () => {
  lang = lang === "en" ? "id" : "en";
  applyLang();
});

const SUN_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
const MOON_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
$("theme-toggle").addEventListener("click", () => {
  const html = document.documentElement;
  const next = html.dataset.theme === "dark" ? "light" : "dark";
  html.dataset.theme = next;
  $("theme-toggle").innerHTML = next === "dark" ? MOON_ICON : SUN_ICON;
});

const BITRATES = [128, 192, 256, 320];
$("bitrate-select").addEventListener("click", (e) => {
  const currentIndex = BITRATES.indexOf(Number(e.currentTarget.value));
  const bitrate = BITRATES[(currentIndex + 1) % BITRATES.length];
  e.currentTarget.value = bitrate;
  e.currentTarget.textContent = `${bitrate} kbps`;
  e.currentTarget.setAttribute("aria-label", `MP3 bitrate: ${bitrate} kbps. Click to change`);
});

$("threshold-slider").addEventListener("input", (e) => {
  $("db-display").textContent = `${e.target.value} dB`;
});

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(2) + " MB";
}

function hideFeedback() {
  $("error-box").classList.remove("visible");
  $("result-box").classList.remove("visible");
}

function addFiles(fileListObj) {
  hideFeedback();
  const mp3s = Array.from(fileListObj).filter((f) => f.name.toLowerCase().endsWith(".mp3"));
  if (!mp3s.length) {
    $("error-box").textContent = "Please select valid .mp3 files.";
    $("error-box").classList.add("visible");
    return;
  }
  if (mode === "single") {
    selectedFiles = [mp3s[0]];
  } else {
    selectedFiles = [...selectedFiles, ...mp3s];
  }
  renderFileList();
}

function renderFileList() {
  const list = $("file-list");
  list.replaceChildren();
  selectedFiles.forEach((f, idx) => {
    const item = document.createElement("div");
    item.className = "file-item";

    const meta = document.createElement("div");
    meta.className = "file-item-meta";

    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = f.name;

    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = formatBytes(f.size);

    meta.append(name, size);

    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "remove-file-btn";
    rm.textContent = "×";
    rm.onclick = () => {
      selectedFiles.splice(idx, 1);
      renderFileList();
      hideFeedback();
    };

    item.append(meta, rm);
    list.append(item);
  });

  $("crop-btn").disabled = selectedFiles.length === 0;
  $("clear-btn").style.display = selectedFiles.length > 0 ? "inline-flex" : "none";
}

$("dropzone").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => {
  if (e.target.files.length) addFiles(e.target.files);
  e.target.value = "";
});

$("dropzone").addEventListener("dragover", (e) => {
  e.preventDefault();
  $("dropzone").classList.add("dragover");
});
$("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("dragover"));
$("dropzone").addEventListener("drop", (e) => {
  e.preventDefault();
  $("dropzone").classList.remove("dragover");
  if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});

$("clear-btn").addEventListener("click", () => {
  selectedFiles = [];
  renderFileList();
  hideFeedback();
});

$("crop-btn").addEventListener("click", async () => {
  if (!selectedFiles.length) return;
  const dict = I18N[lang];
  hideFeedback();

  $("crop-btn").disabled = true;
  $("crop-btn-text").textContent = dict.processing;

  const formData = new FormData();
  selectedFiles.forEach((f) => formData.append("files", f));
  formData.append("threshold", $("threshold-slider").value);
  formData.append("bitrate", $("bitrate-select").value);

  try {
    const startRes = await fetch("/api/crop", { method: "POST", body: formData });
    const startData = await startRes.json();
    if (!startRes.ok) throw new Error(startData.error || "Upload failed");

    const jobId = startData.jobId;
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/api/status/${jobId}`);
        const data = await res.json();

        if (data.status === "pending") {
          $("crop-btn-text").textContent = `${dict.processing} (${data.completed + 1}/${data.total})`;
        } else if (data.status === "ready") {
          clearInterval(timer);
          $("crop-btn").disabled = false;
          $("crop-btn-text").textContent = dict.btn_crop;

          $("result-title").textContent = dict.ready_title;
          $("result-sub").textContent = `${data.filename} • ${dict.ready_sub}`;
          $("download-link").href = data.downloadLink;
          $("download-link").download = data.filename;
          $("result-box").classList.add("visible");
        } else if (data.status === "failed") {
          clearInterval(timer);
          throw new Error(data.error || "Cropping failed");
        }
      } catch (err) {
        clearInterval(timer);
        $("crop-btn").disabled = false;
        $("crop-btn-text").textContent = dict.btn_crop;
        $("error-box").textContent = err.message;
        $("error-box").classList.add("visible");
      }
    }, 1200);
  } catch (err) {
    $("crop-btn").disabled = false;
    $("crop-btn-text").textContent = dict.btn_crop;
    $("error-box").textContent = err.message;
    $("error-box").classList.add("visible");
  }
});

applyLang();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
