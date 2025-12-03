from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class TrackState:
    id: str
    title: str
    artist: str
    status: str = "pending"
    message: Optional[str] = None
    path: Optional[Path] = None


@dataclass
class Job:
    id: str
    url: str
    status: JobStatus = JobStatus.queued
    error: Optional[str] = None
    tracks: Dict[str, TrackState] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    playlist_name: Optional[str] = None

    def add_log(self, message: str) -> None:
        self.logs.append(message)


@dataclass
class CreateJobRequest:
    url: str
    force_redownload: bool = False


@dataclass
class ApiMessage:
    message: str
