import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store, subtitles
from .translator import DEEPL_API_KEY, translate_subs

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20000"))
MAX_SUBTITLE_MB = 20
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Sous-titres", docs_url=None, redoc_url=None)
store.start_janitor()


@app.put("/api/upload")
async def upload(request: Request, name: str):
    ext = Path(name).suffix.lower()
    size = int(request.headers.get("content-length") or 0)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {MAX_UPLOAD_MB} Mo)")

    if ext in subtitles.SUBTITLE_EXTS:
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_SUBTITLE_MB * 1024 * 1024:
                raise HTTPException(413, "Fichier de sous-titres trop volumineux")
        try:
            tracks, skipped = [subtitles.track_from_file(name, bytes(raw))], 0
        except subtitles.ExtractionError as exc:
            raise HTTPException(422, str(exc))
    elif ext in subtitles.VIDEO_EXTS:
        tracks, skipped = await subtitles.extract_from_stream(name, request.stream())
    else:
        raise HTTPException(415, "Format non pris en charge")

    tracks = [t for t in tracks if t.lines]
    if not tracks:
        msg = "Aucune piste de sous-titres texte trouvée dans ce fichier."
        if skipped:
            msg += f" {skipped} piste(s) image (PGS/VobSub) ignorée(s) : elles ne contiennent pas de texte."
        raise HTTPException(422, msg)

    upload_id = store.uploads.put(store.Upload(filename=name, tracks=tracks))
    return {"id": upload_id, "filename": name, "skipped": skipped,
            "tracks": [t.public() for t in tracks]}


class JobRequest(BaseModel):
    upload_id: str
    track: int
    source: str = "auto"
    target: str
    remove_sdh: bool = True


def _run(job: store.Job, track: subtitles.Track, req: JobRequest):
    def progress(done, total, src, dst):
        job.done, job.total = done, total
        if src:
            job.last_source, job.last_target = src, dst

    try:
        subs = subtitles.load(track.content, track.fmt)
        job.removed_sdh = translate_subs(subs, req.source, req.target, req.remove_sdh,
                                         progress, cancelled=lambda: job.status == "cancelled")
        job.result = subs.to_string(track.fmt).encode("utf-8")
        job.status = "done"
    except Exception as exc:
        if job.status != "cancelled":
            job.status, job.error = "error", f"La traduction a échoué : {exc}"
    finally:
        job.finished = time.time()


@app.post("/api/jobs")
def create_job(req: JobRequest):
    up = store.uploads.get(req.upload_id)
    if not up:
        raise HTTPException(404, "Fichier expiré, merci de le déposer à nouveau")
    track = next((t for t in up.tracks if t.index == req.track), None)
    if not track:
        raise HTTPException(404, "Piste introuvable")

    stem = Path(up.filename).stem
    for suffix in (".fr", ".en", ".fre", ".eng", f".{track.language}"):
        stem = stem.removesuffix(suffix)
    job = store.Job(filename=f"{stem}.{req.target}.{track.fmt}")
    job_id = store.jobs.put(job)
    threading.Thread(target=_run, args=(job, track, req), daemon=True).start()
    return {"id": job_id}


def _job(job_id: str) -> store.Job:
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Traduction introuvable ou expirée")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return _job(job_id).public()


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = _job(job_id)
    if job.status != "done" or job.result is None:
        raise HTTPException(409, "Traduction pas encore terminée")
    return Response(
        job.result,
        media_type="application/x-subrip" if job.filename.endswith(".srt") else "text/plain",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(job.filename)}",
                 "Cache-Control": "no-store"},
    )


@app.delete("/api/jobs/{job_id}")
def cancel(job_id: str):
    job = store.jobs.pop(job_id)
    if job and job.status == "running":
        job.status = "cancelled"
    return {"ok": True}


@app.delete("/api/uploads/{upload_id}")
def forget_upload(upload_id: str):
    store.uploads.pop(upload_id)
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"provider": "DeepL" if DEEPL_API_KEY else "Google Traduction"}


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
