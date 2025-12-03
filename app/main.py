from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import AppConfig, CONFIG_PATH
from .job_manager import JobManager
from .models import CreateJobRequest, Job

app = FastAPI(title="SpotDL Web Helper", version="0.1.0")

config = AppConfig.load()
job_manager = JobManager(config)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).resolve().parent.parent / "frontend"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


class JobResponse(BaseModel):
    id: str
    url: str
    status: str
    error: str | None
    playlist_name: str | None
    tracks: list[dict[str, Any]]
    logs: list[str]


class CreateJobBody(BaseModel):
    url: str
    force_redownload: bool = False


class ConfigResponse(BaseModel):
    output_dir: str
    library_dir: str
    playlists_dir: str
    output_template: str
    threads: int
    overwrite_strategy: str


@app.get("/api/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        output_dir=str(config.output_dir),
        library_dir=str(config.library_dir),
        playlists_dir=str(config.playlists_dir),
        output_template=config.output_template,
        threads=config.threads,
        overwrite_strategy=config.overwrite_strategy,
    )


@app.post("/api/jobs", response_model=JobResponse)
async def create_job(body: CreateJobBody) -> JobResponse:
    job = await job_manager.create_job(CreateJobRequest(url=body.url, force_redownload=body.force_redownload))
    return serialize_job(job)


@app.get("/api/jobs", response_model=list[JobResponse])
async def list_jobs() -> list[JobResponse]:
    return [serialize_job(job) for job in job_manager.list_jobs().values()]


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream() -> AsyncGenerator[bytes, None]:
        queue = await job_manager.subscribe(job_id)
        try:
            yield format_sse({"type": "snapshot", "data": serialize_job(job)})
            while True:
                await queue.get()
                yield format_sse({"type": "update", "data": serialize_job(job)})
        finally:
            await job_manager.unsubscribe(job_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def serialize_job(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        url=job.url,
        status=job.status.value,
        error=job.error,
        playlist_name=job.playlist_name,
        tracks=[
            {
                "id": track.id,
                "title": track.title,
                "artist": track.artist,
                "status": track.status,
                "message": track.message,
                "path": str(track.path) if track.path else None,
            }
            for track in job.tracks.values()
        ],
        logs=job.logs,
    )


def format_sse(payload: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


@app.get("/api/config/file")
async def download_config_file() -> FileResponse:
    return FileResponse(CONFIG_PATH, filename="config.json")


@app.exception_handler(Exception)
async def on_error(_: Request, exc: Exception):  # noqa: ANN001
    return JSONResponse(status_code=500, content={"message": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
