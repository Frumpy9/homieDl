from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from spotdl import Spotdl
from spotdl.types.song import Song
from spotdl.types.options import DownloaderOptionalOptions
from spotdl.utils.formatter import create_file_name
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from .config import AppConfig
from .models import Job, JobStatus, TrackState


class DownloadError(Exception):
    """Raised when a job cannot be executed."""


def build_spotify_client(config: AppConfig) -> Optional[spotipy.Spotify]:
    if not config.spotify_client_id or not config.spotify_client_secret:
        return None
    auth_manager = SpotifyClientCredentials(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def get_playlist_name(sp: Optional[spotipy.Spotify], url: str) -> Optional[str]:
    if sp is None:
        return None
    if "playlist" in url:
        playlist_id = url.rstrip("/").split("playlist/")[-1].split("?")[0]
        try:
            details = sp.playlist(playlist_id)
            return details.get("name")
        except spotipy.SpotifyException:
            return None
    return None


def create_spotdl(config: AppConfig) -> Spotdl:
    settings = DownloaderOptionalOptions(
        output=str(Path(config.library_dir, config.output_template)),
        overwrite=config.overwrite_strategy,
        threads=config.threads,
    )
    return Spotdl(
        client_id=config.spotify_client_id or "",
        client_secret=config.spotify_client_secret or "",
        user_auth=False,
        downloader_settings=settings,
    )


def build_track_id(song: Song) -> str:
    return song.url or f"{song.artists[0].name}-{song.name}"


def planned_output_path(song: Song, config: AppConfig) -> Path:
    path = create_file_name(
        song=song,
        template=str(Path(config.library_dir, config.output_template)),
        file_extension="mp3",
    )
    return path


def ensure_symlink(target: Path, link_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        return
    try:
        os.link(target, link_path)
    except OSError:
        link_path.symlink_to(target)


def sync_playlist_manifest(playlist_name: str, tracks: Iterable[TrackState], config: AppConfig) -> Path:
    playlist_dir = config.playlists_dir / playlist_name
    playlist_dir.mkdir(parents=True, exist_ok=True)
    m3u_path = playlist_dir / "playlist.m3u"

    with m3u_path.open("w", encoding="utf-8") as handle:
        for track in tracks:
            if track.path:
                handle.write(f"{track.path.as_posix()}\n")
                ensure_symlink(track.path, playlist_dir / track.path.name)
    return m3u_path


def download_job(job: Job, config: AppConfig, event_callback) -> None:
    config.ensure_paths()

    spotdl_client = create_spotdl(config)
    spotify_client = build_spotify_client(config)

    job.playlist_name = get_playlist_name(spotify_client, job.url)

    job.status = JobStatus.running
    job.add_log("Starting search...")
    event_callback()

    songs: List[Song] = spotdl_client.search([job.url])
    if not songs:
        raise DownloadError("No tracks resolved from the provided URL")

    for song in songs:
        track_id = build_track_id(song)
        track_state = job.tracks.setdefault(
            track_id,
            TrackState(
                id=track_id,
                title=song.name,
                artist=", ".join(artist.name for artist in song.artists),
            ),
        )

    event_callback()

    for song in songs:
        track_id = build_track_id(song)
        track = job.tracks[track_id]
        try:
            job.add_log(f"Downloading {track.title}...")
            event_callback()
            result_song, downloaded_path = spotdl_client.download(song)
            target_path = downloaded_path or planned_output_path(song, config)
            track.path = target_path
            track.status = "downloaded"
            job.add_log(f"Finished {track.title}")
            if job.playlist_name:
                sync_playlist_manifest(job.playlist_name, job.tracks.values(), config)
        except Exception as exc:  # noqa: BLE001
            track.status = "error"
            track.message = str(exc)
            job.add_log(f"Failed {track.title}: {exc}")
            event_callback()

    if all(track.status == "downloaded" for track in job.tracks.values()):
        job.status = JobStatus.completed
    else:
        job.status = JobStatus.failed
        job.error = "Some tracks failed. Check logs."
    event_callback()


def run_job(job: Job, config: AppConfig, event_callback) -> None:
    try:
        download_job(job, config, event_callback)
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc)
        job.add_log(f"Job failed: {exc}")
        event_callback()
