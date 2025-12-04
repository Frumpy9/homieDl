from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotdl import Spotdl
from spotdl.types.options import DownloaderOptionalOptions
from spotdl.types.song import Song
from spotdl.utils.formatter import create_file_name

from .config import AppConfig
from .models import Job, JobStatus, TrackState


logger = logging.getLogger(__name__)


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


def get_artist_name(artist: object) -> str:
    # SpotDL typically returns Artist objects, but be defensive if strings appear
    return getattr(artist, "name", None) or str(artist)


def build_track_id(song: Song) -> str:
    primary_artist = song.artists[0] if song.artists else "unknown"
    return song.url or f"{get_artist_name(primary_artist)}-{song.name}"


def requires_user_auth(url: str) -> bool:
    normalized = url.lower()
    return "open.spotify.com/user" in normalized or "/user/" in normalized or "/users/" in normalized


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
    logger.info("Job %s: starting download pipeline for %s", job.id, job.url)
    config.ensure_paths()

    spotdl_client = create_spotdl(config)
    spotify_client = build_spotify_client(config)

    if requires_user_auth(job.url) and spotify_client is None:
        raise DownloadError(
            "Spotify user-library URLs require client credentials. Add spotify_client_id and "
            "spotify_client_secret to config.json."
        )

    job.playlist_name = get_playlist_name(spotify_client, job.url)
    logger.info("Job %s: resolved playlist name %s", job.id, job.playlist_name)

    job.status = JobStatus.running
    job.add_log("Starting search...")
    event_callback()

    try:
        results = spotdl_client.download(job.url)
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc)
        job.add_log(f"Job failed: {exc}")
        logger.exception("Job %s: spotdl download failed", job.id)
        event_callback()
        return

    if not results:
        job.status = JobStatus.failed
        job.error = "No tracks resolved from the provided URL"
        job.add_log(job.error)
        event_callback()
        return

    logger.info("Job %s: spotdl returned %s tracks", job.id, len(results))

    for result_song, downloaded_path in results:
        track_id = build_track_id(result_song)
        track = job.tracks.setdefault(
            track_id,
            TrackState(
                id=track_id,
                title=getattr(result_song, "name", str(result_song)),
                artist=", ".join(
                    get_artist_name(artist) for artist in getattr(result_song, "artists", [])
                ),
            ),
        )
        track.status = "downloading"
        job.add_log(f"Downloading {track.title}...")
        event_callback()

        target_path = downloaded_path or planned_output_path(result_song, config)
        track.path = target_path
        track.status = "downloaded"
        job.add_log(f"Finished {track.title}")
        logger.info("Job %s: finished track %s stored at %s", job.id, track.id, target_path)
        if job.playlist_name:
            sync_playlist_manifest(job.playlist_name, job.tracks.values(), config)

    job.status = JobStatus.completed
    event_callback()


def run_job(job: Job, config: AppConfig, event_callback) -> None:
    try:
        download_job(job, config, event_callback)
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed
        job.error = str(exc)
        job.add_log(f"Job failed: {exc}")
        logger.exception("Job %s: fatal error %s", job.id, exc)
        event_callback()
