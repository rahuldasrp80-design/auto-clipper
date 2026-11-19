import os, re, uuid, shutil, subprocess, threading, time, json
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).parent.resolve()
WORK = Path(os.getenv("WORK_DIR", str(ROOT / "work")))
WORK.mkdir(parents=True, exist_ok=True)
JOBS = {}
LOCK = threading.Lock()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_CLIPS = 8

app = FastAPI(title="Auto Clipper", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def run(cmd, timeout=None):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-1800:])
    return p.stdout

def ffmpeg_ok():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")

def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=60)
    return float(out.strip())

def download_source(url, dest):
    # yt-dlp only downloads publicly accessible content the user is permitted to access.
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
           "--merge-output-format", "mp4", "-o", str(dest), url]
    run(cmd, timeout=60*60)
    if not dest.exists():
        candidates = list(dest.parent.glob(dest.stem + ".*"))
        if candidates:
            return candidates[0]
        raise RuntimeError("Video download nahi hua. Link public/accessible hona chahiye.")
    return dest

def detect_silences(src, duration):
    # A lightweight heuristic: locate quieter gaps, then center clips in active regions.
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-af",
                        "silencedetect=noise=-34dB:d=0.45", "-f", "null", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
    log = p.stderr
    silences = []
    starts = re.findall(r" silence_start: ([0-9.]+)", log)
    ends = re.findall(r" silence_end: ([0-9.]+)", log)
    for i, s in enumerate(starts):
        e = float(ends[i]) if i < len(ends) else duration
        silences.append((float(s), e))
    active = []
    cursor = 0.0
    for s, e in silences:
        if s - cursor > 3:
            active.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > 3:
        active.append((cursor, duration))
    return active

def candidate_segments(src, duration, count):
    # Prefer active-audio windows; spread candidates across the full source.
    clip_len = min(55.0, max(20.0, duration / max(1, count)))
    try:
        active = detect_silences(src, duration)
    except Exception:
        active = []
    if not active:
        active = [(0.0, duration)]
    scored = []
    for a, b in active:
        span = b - a
        if span < 8:
            continue
        center = (a + b) / 2
        start = max(0, min(duration - clip_len, center - clip_len / 2))
        # Favor longer active stretches, with a mild distribution bonus.
        score = min(span, 120) + (center / max(duration, 1)) * 5
        scored.append((score, start, min(duration, start + clip_len)))
    if not scored:
        scored = [(1, max(0, (duration - clip_len) * i / max(1, count-1)),
                   min(duration, max(0, (duration - clip_len) * i / max(1, count-1)) + clip_len))
                  for i in range(count)]
    scored.sort(reverse=True)
    chosen = []
    for score, s, e in scored:
        if all(abs(s - x[0]) > clip_len * 0.65 for x in chosen):
            chosen.append((s, e, score))
        if len(chosen) >= count:
            break
    if len(chosen) < count:
        for i in range(count * 3):
            s = max(0, (duration - clip_len) * i / max(1, count*3-1))
            if all(abs(s - x[0]) > clip_len * 0.55 for x in chosen):
                chosen.append((s, min(duration, s + clip_len), 0))
            if len(chosen) >= count:
                break
    chosen.sort(key=lambda x: x[0])
    return [(s, e, "High activity / highlight candidate") for s, e, _ in chosen[:count]]

def transcribe(src):
    if os.getenv("ENABLE_WHISPER", "false").lower() != "true":
        return []
    try:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "tiny")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(src), vad_filter=True)
        return [{"start": float(x.start), "end": float(x.end), "text": x.text.strip()}
                for x in segments]
    except Exception as e:
        print("Whisper unavailable:", e)
        return []

def srt_time(sec):
    ms = int(max(0, sec) * 1000)
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def write_srt(path, words, start, end, custom_text=None):
    rows = []
    if custom_text:
        rows = [(0, end-start, custom_text)]
    else:
        for seg in words:
            a, b = max(start, seg["start"]), min(end, seg["end"])
            if b > a:
                rows.append((a-start, b-start, seg["text"]))
    if not rows:
        return None
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(rows, 1):
            safe = str(t).replace("-->", "→").strip()
            f.write(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{safe}\n\n")
    return path

def render_clip(src, dest, start, end, subtitle_path=None):
    duration = max(1, end-start)
    # Center-crop to vertical 9:16, scale to 1080x1920.
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if subtitle_path and subtitle_path.exists():
        sub = str(subtitle_path).replace("\\", "/").replace(":", r"\:")
        vf += f",subtitles='{sub}':force_style='FontSize=20,Outline=2,Shadow=1,Alignment=2,MarginV=120'"
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(duration),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)]
    run(cmd, timeout=60*30)

def process_job(job_id, source_path, n):
    job = JOBS[job_id]
    try:
        if not ffmpeg_ok():
            raise RuntimeError("FFmpeg/FFprobe server par installed nahi hai.")
        job["status"] = "Video analyse ho raha hai..."
        duration = probe_duration(source_path)
        if duration < 10:
            raise RuntimeError("Video kam se kam 10 seconds ka hona chahiye.")
        job["duration"] = round(duration, 1)
        words = transcribe(source_path)
        candidates = candidate_segments(source_path, duration, n)
        job["status"] = "Clips render ho rahe hain..."
        clips = []
        for idx, (start, end, reason) in enumerate(candidates, 1):
            out = WORK / job_id / f"clip_{idx}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            srt = out.with_suffix(".srt")
            srt_path = write_srt(srt, words, start, end)
            render_clip(source_path, out, start, end, srt_path)
            clips.append({"id": idx, "start": round(start, 1), "end": round(end, 1),
                          "reason": reason, "text": "", "url": f"/media/{job_id}/clip_{idx}.mp4"})
            job["clips"] = clips
        job["state"] = "completed"
        job["status"] = f"{len(clips)} clips ready!"
    except Exception as e:
        job["state"] = "failed"
        job["status"] = "Error: " + str(e)
    finally:
        try:
            if source_path.exists():
                source_path.unlink()
        except Exception:
            pass

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.post("/api/jobs")
async def create_job(background_tasks: BackgroundTasks, url: Optional[str] = Form(None),
                     n: int = Form(5), video: Optional[UploadFile] = File(None)):
    if not 1 <= n <= MAX_CLIPS:
        raise HTTPException(400, f"1 se {MAX_CLIPS} clips select karein.")
    if not url and not video:
        raise HTTPException(400, "Video link ya upload select karein.")
    job_id = uuid.uuid4().hex[:12]
    folder = WORK / job_id
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "source.mp4"
    try:
        if video:
            size = 0
            with source.open("wb") as f:
                while True:
                    chunk = await video.read(1024*1024)
                    if not chunk: break
                    size += len(chunk)
                    if size > MAX_UPLOAD_MB * 1024 * 1024:
                        raise HTTPException(413, f"Maximum upload {MAX_UPLOAD_MB} MB hai.")
                    f.write(chunk)
        else:
            source = download_source(url.strip(), source)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, str(e))
    JOBS[job_id] = {"id": job_id, "state": "running", "status": "Job start ho gaya...",
                    "clips": [], "created": time.time()}
    background_tasks.add_task(process_job, job_id, source, n)
    return {"id": job_id}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS: raise HTTPException(404, "Job nahi mila.")
    return JOBS[job_id]

@app.post("/api/jobs/{job_id}/clips/{clip_id}/caption")
async def update_caption(job_id: str, clip_id: int, payload: dict):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404, "Job nahi mila.")
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip: raise HTTPException(404, "Clip nahi mila.")
    text = str(payload.get("text", ""))[:500]
    folder = WORK / job_id
    src = folder / f"clip_{clip_id}.mp4"
    dest = folder / f"clip_{clip_id}_captioned.mp4"
    srt = folder / f"caption_{clip_id}.srt"
    dur = clip["end"] - clip["start"]
    write_srt(srt, [], 0, dur, text)
    try:
        # Re-encode existing clip with the edited caption.
        render_clip(src, dest, 0, dur, srt)
        os.replace(dest, src)
        clip["text"] = text
        clip["url"] = f"/media/{job_id}/clip_{clip_id}.mp4?v={int(time.time())}"
        return {"url": clip["url"], "v": int(time.time())}
    except Exception as e:
        raise HTTPException(500, "Caption render failed: " + str(e))

@app.get("/media/{job_id}/{filename}")
def media(job_id: str, filename: str):
    path = WORK / job_id / filename
    if not path.exists() or path.parent.resolve() != (WORK / job_id).resolve():
        raise HTTPException(404, "File nahi mila.")
    return FileResponse(path, media_type="video/mp4", filename=filename)

@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": bool(ffmpeg_ok())}
