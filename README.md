# Auto Clipper

Android-friendly web app: video URL or upload → candidate highlight clips → vertical 9:16 MP4 → editable burned-in caption.

## What works

- Upload a video or submit a public URL supported by yt-dlp.
- Generates 3, 5, or 8 highlight candidates.
- Detects quiet gaps with FFmpeg and prefers longer active-audio sections.
- Exports 1080x1920 vertical MP4 clips.
- Optional Whisper transcription (when enabled and installed).
- Edit a caption and burn it into the clip.
- Job status and individual downloads.

## Important limitations

This is an AI-assisted prototype, not a guaranteed viral-clip predictor. Current highlight ranking uses audio activity / silence-gap heuristics. It does not understand game context, jokes, kills, or streamer reactions semantically. Whisper is optional and only creates transcript subtitles; it does not decide virality.

Video URLs work only when the source is publicly accessible and yt-dlp supports the page. Platform login, DRM, private videos, geo restrictions, or platform changes can prevent downloads. Follow each platform's terms and only process content you have permission to use. Do not use cookies or bypass access controls.

The app stores jobs in memory and files in a local `work/` folder. A restart clears job status; ephemeral free hosting can delete rendered files. Do not use this setup for confidential videos or long-term storage.

## Run locally

Requires Python 3.11+ and FFmpeg/FFprobe.

```bash
# Install FFmpeg first
# Ubuntu/Debian:
sudo apt-get update && sudo apt-get install -y ffmpeg

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`.

## Optional AI subtitles

Install optional package and enable Whisper:

```bash
pip install -r requirements-ai.txt
ENABLE_WHISPER=true WHISPER_MODEL=tiny uvicorn app:app --host 0.0.0.0 --port 8000
```

The first run downloads the Whisper model. `tiny` is the lightest recommended model for CPU testing. For Hindi/English mixed speech, results can vary. Transcription may be slow on free CPU hosting.

## Deploy free on Render (Docker)

1. Create a GitHub account and a new repository named `auto-clipper`.
2. Upload all project files from this ZIP to the repository (keep `Dockerfile` and `render.yaml` at the root).
3. In Render, choose **New → Blueprint**, connect the GitHub repository, and deploy the `auto-clipper` service.
4. Wait for the Docker build. Open the generated `onrender.com` URL.
5. Test first with a short, public, downloadable video or a small upload.

Render free services may sleep after inactivity, have limited RAM/CPU, and use ephemeral storage. Long renders can be terminated and uploaded files may disappear after restart/redeploy. Free plan availability/limits can change. This app is best treated as a demo on free hosting; a persistent disk or paid worker is needed for reliable production use.

## Deploy on another Docker host

Build and run:

```bash
docker build -t auto-clipper .
docker run --rm -p 8000:8000 -e MAX_UPLOAD_MB=500 auto-clipper
```

## Environment variables

- `PORT`: web server port (default 8000)
- `WORK_DIR`: job storage folder (default `./work`)
- `MAX_UPLOAD_MB`: upload limit (default 500)
- `ENABLE_WHISPER`: `true` to enable optional faster-whisper transcription (default false)
- `WHISPER_MODEL`: `tiny`, `base`, etc. (default tiny)

## API

- `GET /health`
- `POST /api/jobs` multipart form fields: `url` (optional), `video` (optional), `n` (1–8)
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/clips/{clip_id}/caption` JSON: `{"text":"Your caption"}`
- `GET /media/{job_id}/{filename}`

For a reliable production release, move job metadata to SQLite/Postgres, use persistent object storage, add authentication/rate limits, and run rendering in a background worker with a queue.
