from __future__ import annotations

import asyncio
import uuid
from typing import Dict, Optional

from .config import AppConfig
from .downloader import run_job
from .models import CreateJobRequest, Job, JobStatus


class JobManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.jobs: Dict[str, Job] = {}
        self._event_subscribers: Dict[str, list[asyncio.Queue[str]]] = {}
        self._lock = asyncio.Lock()

    def _broadcast(self, job_id: str) -> None:
        for queue in self._event_subscribers.get(job_id, []):
            queue.put_nowait("update")

    async def subscribe(self, job_id: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        async with self._lock:
            self._event_subscribers.setdefault(job_id, []).append(queue)
        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            if job_id in self._event_subscribers:
                self._event_subscribers[job_id] = [q for q in self._event_subscribers[job_id] if q is not queue]

    async def create_job(self, payload: CreateJobRequest) -> Job:
        job_id = str(uuid.uuid4())
        job = Job(id=job_id, url=payload.url)
        self.jobs[job_id] = job

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, run_job, job, self.config, lambda: self._broadcast(job_id))
        return job

    def list_jobs(self) -> Dict[str, Job]:
        return self.jobs

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)
