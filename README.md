# Global Chat (yt-dlp music edition)

Realtime group chat with shared temporary music playback.

This repository is the **yt-dlp music** variant of [SanjeevYadav34/Global-Chat](https://github.com/SanjeevYadav34/Global-Chat):

- **Frontend** – static SPA (`index.html` + `sw.js`) on Vercel / any static host  
- **Backend** – FastAPI + yt-dlp (`backend/`) for search → temp download → private Supabase Storage → signed URL  
- **Database** – existing Supabase `messages` + `music_room` tables (no schema change)  
- **Storage** – private bucket `temp-audio` (see `supabase/temp-audio.sql`)

Audius and SoundCloud integrations from the original app are removed. Chat, auth, images, notifications, and Join/Leave music sync are preserved.

## Legal notice

Use the yt-dlp integration **only** with media you are authorized to download and play. Do not use it to bypass DRM, paywalls, or access controls.

## Quick start

### 1. Supabase

1. Use the same project as the original Global Chat (or create a new one).
2. Run `supabase/temp-audio.sql` in the SQL editor (creates private `temp-audio` bucket).
3. Confirm `messages` and `music_room` tables exist (same as original app).

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Install ffmpeg on the host (apt/brew)
cp .env.example .env   # fill SUPABASE_* and ALLOWED_ORIGINS
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Or: `docker build -t global-chat-music . && docker run --env-file .env -p 8000:8000 global-chat-music`

### 3. Frontend

1. Open `index.html`.
2. Set `SUPABASE_URL` and `SUPABASE_ANON_KEY` (anon key only).
3. Set `MUSIC_API` to your backend URL (e.g. `http://127.0.0.1:8000` or your Render/Railway HTTPS URL).
4. Deploy the static files (`index.html`, `sw.js`, `vercel.json`).

**Never put the service-role key in the frontend.**

## Commands in chat

| Command | Action |
|---------|--------|
| `/music <song name>` | Search, prepare audio, play for the room |
| `/pause` | Pause shared playback |
| `/resume` | Resume |
| `/stop` | Stop and delete temp audio |

Join / Leave controls appear on the music card and player bar.

## Environment variables (backend)

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | yes | Project URL |
| `SUPABASE_ANON_KEY` | yes | Anon key (JWT verification) |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Service role (**server only**) |
| `ALLOWED_ORIGINS` | yes | Frontend origins (CORS) |
| `SIGNED_URL_EXPIRES_SEC` | no | Default 7200 |
| `MAX_AUDIO_SECONDS` | no | Default 600 |
| `MAX_AUDIO_BYTES` | no | Default 20 MB |
| `RATE_LIMIT_PER_MINUTE` | no | Default 5 |
| `CLEANUP_MAX_AGE_HOURS` | no | Default 2 |

## Repo layout

```
index.html          # SPA (chat + music UI)
sw.js               # notification service worker
vercel.json         # static deploy headers
backend/
  app/main.py       # FastAPI + yt-dlp
  requirements.txt
  Dockerfile
  .env.example
  README.md
supabase/
  temp-audio.sql    # private bucket migration
```

## Original project

Based on [https://github.com/SanjeevYadav34/Global-Chat](https://github.com/SanjeevYadav34/Global-Chat).
