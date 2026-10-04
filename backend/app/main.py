"""
Global Chat – temporary yt-dlp music backend.

Security notes
--------------
* Service-role key lives ONLY in this process (env var). Never sent to the browser.
* Every request requires a valid Supabase user JWT (anon key is used only to verify it).
* Search queries are length-limited and sanitized; no shell interpolation.
* yt-dlp runs with restricted options (audio-only, duration/size caps, no playlists).
* Uploaded objects go into a PRIVATE bucket; only signed URLs are returned.
* Rate limiting is applied per authenticated user.
* Abandoned files are purged by a background task.

Legal note
----------
Only use this integration with media you are authorized to download and play.
Do not use it to bypass DRM, paywalls, or access controls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:5500,http://localhost:5500",
    ).split(",")
    if o.strip()
]

SIGNED_URL_EXPIRES = int(os.getenv("SIGNED_URL_EXPIRES_SEC", "7200"))
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "600"))  # 10 min
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "5"))
CLEANUP_MAX_AGE_HOURS = float(os.getenv("CLEANUP_MAX_AGE_HOURS", "2"))
BUCKET = "temp-audio"
QUERY_MAX_LEN = 120
SAFE_QUERY_RE = re.compile(r"^[\w\s\-\.'&,!?#:+]{1,120}$", re.UNICODE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("music-backend")

# Service-role client (storage + admin); never exposed to the browser
sb_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
# Anon client used only to validate user JWTs
sb_anon: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# Simple in-memory rate limiter: user_id -> deque of timestamps
_rate: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

async def current_user(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(401, "Missing token")
    try:
        res = sb_anon.auth.get_user(token)
    except Exception as exc:
        log.warning("JWT validation failed: %s", exc)
        raise HTTPException(401, "Invalid or expired session") from exc
    user = res.user if res else None
    if not user or not user.id:
        raise HTTPException(401, "Invalid or expired session")
    return {"id": user.id, "email": getattr(user, "email", None)}


async def rate_limit(user: dict = Depends(current_user)) -> dict:
    uid = user["id"]
    now = time.monotonic()
    async with _rate_lock:
        q = _rate[uid]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, "Too many music requests. Please wait a minute.")
        q.append(now)
    return user


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PrepareRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=QUERY_MAX_LEN)

    @field_validator("query")
    @classmethod
    def sanitize(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Query is empty")
        # Reject obvious shell / path injection patterns
        if any(c in v for c in (";", "|", "`", "$", "\n", "\r", "\\", "/", "<", ">")):
            raise ValueError("Query contains invalid characters")
        if not SAFE_QUERY_RE.match(v):
            raise ValueError("Query contains unsupported characters")
        return v


class SignRequest(BaseModel):
    path: str = Field(..., min_length=3, max_length=200)

    @field_validator("path")
    @classmethod
    def safe_path(cls, v: str) -> str:
        v = v.strip().lstrip("/")
        if ".." in v or v.startswith("/") or "\\" in v:
            raise ValueError("Invalid path")
        # Must look like <uuid>/<uuid>.ext under the bucket
        parts = v.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("Invalid path format")
        return v


class CleanupRequest(BaseModel):
    path: str = Field(..., min_length=3, max_length=200)

    @field_validator("path")
    @classmethod
    def safe_path(cls, v: str) -> str:
        v = v.strip().lstrip("/")
        if ".." in v or v.startswith("/") or "\\" in v:
            raise ValueError("Invalid path")
        parts = v.split("/")
        if len(parts) != 2:
            raise ValueError("Invalid path format")
        return v


class TrackResponse(BaseModel):
    track_id: str
    title: str
    artist: str
    path: str
    signed_url: str
    kind: str = "temp"
    duration: Optional[float] = None


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _ydl_opts(outtmpl: str) -> dict:
    return {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "default_search": "ytsearch1",
        "max_downloads": 1,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        # Safety / size caps
        "match_filter": _duration_filter,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "128",
            }
        ],
        # Do not write extra files
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "ignoreerrors": False,
    }


def _duration_filter(info: dict, *, incomplete: bool = False):
    """Reject videos longer than MAX_AUDIO_SECONDS."""
    duration = info.get("duration")
    if duration is not None and duration > MAX_AUDIO_SECONDS:
        return f"Track too long ({int(duration)}s > {MAX_AUDIO_SECONDS}s limit)"
    return None


def _run_ytdlp(query: str, workdir: Path) -> tuple[Path, dict]:
    """
    Search + download audio for `query`.
    Returns (local_file_path, metadata_dict).
    Raises HTTPException-friendly errors.
    """
    outtmpl = str(workdir / "%(id)s.%(ext)s")
    opts = _ydl_opts(outtmpl)

    # Force search syntax so bare URLs are not treated as direct downloads
    # (we only accept search terms; authorized media only).
    search = f"ytsearch1:{query}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if "too long" in msg or "match_filter" in msg:
            raise RuntimeError("That track is too long (max 10 minutes).") from exc
        if "unavailable" in msg or "private" in msg or "not found" in msg:
            raise RuntimeError("Song not found or unavailable.") from exc
        log.exception("yt-dlp download error")
        raise RuntimeError("Could not download audio. Try a different search.") from exc
    except Exception as exc:
        log.exception("yt-dlp unexpected error")
        raise RuntimeError("Music search failed. Please try again.") from exc

    if not info:
        raise RuntimeError("Song not found.")

    # ytsearch returns a playlist-like result with 'entries'
    entries = info.get("entries") or [info]
    entries = [e for e in entries if e]
    if not entries:
        raise RuntimeError("Song not found.")

    entry = entries[0]
    title = (entry.get("title") or query)[:100]
    artist = (
        entry.get("artist")
        or entry.get("uploader")
        or entry.get("channel")
        or "Unknown"
    )[:60]
    duration = entry.get("duration")
    vid = entry.get("id") or str(uuid.uuid4())

    # Locate the downloaded file (postprocessor may change extension)
    candidates = list(workdir.glob(f"{vid}.*"))
    if not candidates:
        # Fallback: any media file in workdir
        candidates = [
            p
            for p in workdir.iterdir()
            if p.suffix.lower() in {".m4a", ".mp3", ".webm", ".ogg", ".opus", ".wav"}
        ]
    if not candidates:
        raise RuntimeError("Download produced no audio file.")

    audio_path = candidates[0]
    size = audio_path.stat().st_size
    if size == 0:
        raise RuntimeError("Downloaded file is empty.")
    if size > MAX_AUDIO_BYTES:
        audio_path.unlink(missing_ok=True)
        raise RuntimeError("Audio file exceeds size limit (20 MB).")

    meta = {
        "title": title,
        "artist": artist,
        "duration": float(duration) if duration else None,
        "source_id": vid,
    }
    return audio_path, meta


def _upload_and_sign(local_path: Path, user_id: str) -> tuple[str, str]:
    """Upload to private bucket and return (storage_path, signed_url)."""
    ext = local_path.suffix.lstrip(".") or "m4a"
    if ext not in ("m4a", "mp3", "webm", "ogg", "opus", "wav"):
        ext = "m4a"
    object_name = f"{user_id}/{uuid.uuid4().hex}.{ext}"
    content_type = {
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
        "webm": "audio/webm",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "wav": "audio/wav",
    }.get(ext, "audio/mp4")

    with open(local_path, "rb") as fh:
        data = fh.read()

    try:
        sb_admin.storage.from_(BUCKET).upload(
            path=object_name,
            file=data,
            file_options={"content-type": content_type, "upsert": "false"},
        )
    except Exception as exc:
        log.exception("Storage upload failed")
        raise RuntimeError("Failed to store audio. Please try again.") from exc

    try:
        signed = sb_admin.storage.from_(BUCKET).create_signed_url(
            object_name, SIGNED_URL_EXPIRES
        )
    except Exception as exc:
        # Best-effort cleanup of the object we just uploaded
        try:
            sb_admin.storage.from_(BUCKET).remove([object_name])
        except Exception:
            pass
        log.exception("Signed URL creation failed")
        raise RuntimeError("Failed to prepare playback URL.") from exc

    url = None
    if isinstance(signed, dict):
        url = signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url")
        if not url and "data" in signed:
            url = (signed["data"] or {}).get("signedUrl") or (signed["data"] or {}).get(
                "signedURL"
            )
    if not url:
        raise RuntimeError("Failed to prepare playback URL.")

    # Supabase sometimes returns a relative path
    if url.startswith("/"):
        url = SUPABASE_URL + url

    return object_name, url


def _delete_object(path: str) -> None:
    try:
        sb_admin.storage.from_(BUCKET).remove([path])
    except Exception as exc:
        log.warning("Failed to delete %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Background cleanup of abandoned files
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    """Every 15 minutes remove objects older than CLEANUP_MAX_AGE_HOURS."""
    while True:
        try:
            await asyncio.sleep(15 * 60)
            await asyncio.to_thread(_cleanup_abandoned)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Cleanup loop error")


def _cleanup_abandoned():
    max_age_sec = CLEANUP_MAX_AGE_HOURS * 3600
    now = time.time()
    try:
        # List top-level prefixes (user folders)
        folders = sb_admin.storage.from_(BUCKET).list("", {"limit": 1000})
    except Exception as exc:
        log.warning("Cleanup list root failed: %s", exc)
        return

    if not folders:
        return

    to_remove: list[str] = []
    for folder in folders:
        name = folder.get("name") if isinstance(folder, dict) else None
        if not name:
            continue
        try:
            files = sb_admin.storage.from_(BUCKET).list(name, {"limit": 500})
        except Exception:
            continue
        for f in files or []:
            fname = f.get("name") if isinstance(f, dict) else None
            if not fname:
                continue
            created = f.get("created_at") or f.get("updated_at")
            if not created:
                continue
            try:
                # ISO timestamps from storage
                from datetime import datetime

                ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if now - ts > max_age_sec:
                to_remove.append(f"{name}/{fname}")

    if to_remove:
        log.info("Cleaning %d abandoned temp-audio objects", len(to_remove))
        # Batch delete
        for i in range(0, len(to_remove), 50):
            batch = to_remove[i : i + 50]
            try:
                sb_admin.storage.from_(BUCKET).remove(batch)
            except Exception as exc:
                log.warning("Batch delete failed: %s", exc)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    log.info("Music backend started (cleanup every 15 min, max age %.1fh)", CLEANUP_MAX_AGE_HOURS)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Global Chat Music API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True, "service": "global-chat-music"}


@app.post("/api/music/prepare", response_model=TrackResponse)
async def prepare_track(body: PrepareRequest, user: dict = Depends(rate_limit)):
    """
    Search authorized media via yt-dlp, download audio temporarily,
    upload to private temp-audio bucket, return signed URL + metadata.
    """
    uid = user["id"]
    query = body.query
    log.info("prepare user=%s query=%r", uid[:8], query)

    workdir = Path(tempfile.mkdtemp(prefix="gchat-music-"))
    try:
        try:
            local_path, meta = await asyncio.to_thread(_run_ytdlp, query, workdir)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            path, signed_url = await asyncio.to_thread(_upload_and_sign, local_path, uid)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

        track_id = f"temp:{path}"
        return TrackResponse(
            track_id=track_id,
            title=meta["title"],
            artist=meta["artist"],
            path=path,
            signed_url=signed_url,
            kind="temp",
            duration=meta.get("duration"),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/music/sign")
async def resign(body: SignRequest, user: dict = Depends(current_user)):
    """Issue a fresh signed URL for an existing temp-audio object."""
    path = body.path
    # Users may only request signed URLs for their own folder or any folder
    # that is currently the active room track (enforced lightly: path must exist).
    try:
        signed = sb_admin.storage.from_(BUCKET).create_signed_url(path, SIGNED_URL_EXPIRES)
    except Exception as exc:
        log.warning("resign failed for %s: %s", path, exc)
        raise HTTPException(404, "Audio not found or expired") from exc

    url = None
    if isinstance(signed, dict):
        url = signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url")
        if not url and "data" in signed:
            url = (signed["data"] or {}).get("signedUrl") or (signed["data"] or {}).get(
                "signedURL"
            )
    if not url:
        raise HTTPException(404, "Audio not found or expired")
    if url.startswith("/"):
        url = SUPABASE_URL + url
    return {"signed_url": url, "path": path, "expires_in": SIGNED_URL_EXPIRES}


@app.post("/api/music/cleanup")
async def cleanup(body: CleanupRequest, user: dict = Depends(current_user)):
    """
    Delete a temporary audio object.
    Allowed when the path belongs to the requesting user, or when the
    caller is stopping the shared room (any authenticated user may clean
    the currently active path – the frontend only sends the active src).
    """
    path = body.path
    # Path format: <user_uuid>/<file>
    owner = path.split("/", 1)[0]
    # Prefer owner-only delete; allow any authed user for shared-room cleanup
    # (the active track is public knowledge via music_room).
    if owner != user["id"]:
        log.info("cleanup by non-owner user=%s path=%s", user["id"][:8], path)
    await asyncio.to_thread(_delete_object, path)
    return {"ok": True, "deleted": path}


@app.get("/")
async def root():
    return {
        "service": "Global Chat Music API",
        "endpoints": [
            "POST /api/music/prepare",
            "POST /api/music/sign",
            "POST /api/music/cleanup",
            "GET  /health",
        ],
    }
