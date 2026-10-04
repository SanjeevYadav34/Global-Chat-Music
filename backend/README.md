# Global Chat – Music Backend

FastAPI service that powers `/music` for [Global Chat](https://github.com/SanjeevYadav34/Global-Chat).

It searches authorized media with **yt-dlp**, stores audio temporarily in a **private** Supabase Storage bucket (`temp-audio`), and returns short-lived signed URLs. The existing `music_room` row keeps multi-user playback in sync.

## Legal notice

Use only with media you are authorized to download and play. Do not use this stack to bypass DRM, paywalls, or access controls.

## Environment variables

Copy `.env.example` → `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | yes | Project URL |
| `SUPABASE_ANON_KEY` | yes | Anon key (JWT verification only) |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Service role key (**server only**) |
| `ALLOWED_ORIGINS` | yes | Comma-separated frontend origins |
| `SIGNED_URL_EXPIRES_SEC` | no | Default `7200` (2 h) |
| `MAX_AUDIO_SECONDS` | no | Default `600` (10 min) |
| `MAX_AUDIO_BYTES` | no | Default `20971520` (20 MB) |
| `RATE_LIMIT_PER_MINUTE` | no | Default `5` per user |
| `CLEANUP_MAX_AGE_HOURS` | no | Default `2` |

## Local run

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# system dependency for audio extraction
# Debian/Ubuntu: sudo apt install ffmpeg
# macOS: brew install ffmpeg

cp .env.example .env
# edit .env

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check: `GET http://127.0.0.1:8000/health`

## Docker

```bash
docker build -t global-chat-music .
docker run --env-file .env -p 8000:8000 global-chat-music
```

## Deploy (example: Render / Railway / Fly)

1. Create a new web service from this `backend/` folder.
2. Set the environment variables above (never commit the service-role key).
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Ensure **ffmpeg** is available on the image (use the provided `Dockerfile`).
6. Put the public HTTPS URL into the frontend `MUSIC_API` constant.

## API

All endpoints except `/health` require `Authorization: Bearer <supabase_access_token>`.

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| POST | `/api/music/prepare` | `{ "query": "espresso" }` | Search, download, upload, signed URL |
| POST | `/api/music/sign` | `{ "path": "<user>/<file>.m4a" }` | Fresh signed URL |
| POST | `/api/music/cleanup` | `{ "path": "..." }` | Delete temp object |
| GET | `/health` | — | Liveness |

## Supabase setup

Run the SQL in `../supabase/temp-audio.sql` once in the SQL editor.
