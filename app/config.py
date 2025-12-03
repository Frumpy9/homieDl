from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CONFIG_PATH = Path("config.json")


@dataclass
class AppConfig:
    output_dir: Path = Path("downloads")
    library_dir_name: str = "library"
    playlists_dir_name: str = "playlists"
    output_template: str = "{artists} - {title}.{ext}"
    overwrite_strategy: str = "skip"
    threads: int = 4
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None

    @property
    def library_dir(self) -> Path:
        return self.output_dir / self.library_dir_name

    @property
    def playlists_dir(self) -> Path:
        return self.output_dir / self.playlists_dir_name

    @staticmethod
    def load(path: Path = CONFIG_PATH) -> "AppConfig":
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            return AppConfig(
                output_dir=Path(raw.get("output_dir", "downloads")),
                library_dir_name=raw.get("library_dir_name", "library"),
                playlists_dir_name=raw.get("playlists_dir_name", "playlists"),
                output_template=raw.get("output_template", "{artists} - {title}.{ext}"),
                overwrite_strategy=raw.get("overwrite_strategy", "skip"),
                threads=int(raw.get("threads", 4)),
                spotify_client_id=raw.get("spotify_client_id"),
                spotify_client_secret=raw.get("spotify_client_secret"),
            )
        config = AppConfig()
        config.save(path)
        return config

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "output_dir": str(self.output_dir),
                    "library_dir_name": self.library_dir_name,
                    "playlists_dir_name": self.playlists_dir_name,
                    "output_template": self.output_template,
                    "overwrite_strategy": self.overwrite_strategy,
                    "threads": self.threads,
                    "spotify_client_id": self.spotify_client_id,
                    "spotify_client_secret": self.spotify_client_secret,
                },
                handle,
                indent=2,
            )

    def ensure_paths(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.playlists_dir.mkdir(parents=True, exist_ok=True)
