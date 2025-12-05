"""Download tracks listed in an Exportify CSV by searching YouTube Music with yt-dlp.

Usage example:
    python exportify_downloader.py playlist.csv --output downloads
    python exportify_downloader.py playlist1.csv playlist2.csv --output downloads

The script reads the Exportify CSV, builds YouTube search queries using the
artist and track names, and downloads the best audio stream (optionally
converting it to MP3). See `python exportify_downloader.py --help` for all
options.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older Pythons
    import tomli as tomllib

import yt_dlp
from yt_dlp.utils import sanitize_filename
from ytmusicapi import YTMusic


@dataclass
class Track:
    """A single track entry extracted from the Exportify CSV."""

    title: str
    artists: str
    album: str

    def build_terms(self, include_album: bool) -> str:
        """Build raw search terms that always include title and artist.

        Album stays optional, but we always lead with track + artist and append
        the word "audio" to bias results away from music videos.
        """

        if not self.title or not self.artists:
            return ""

        parts: List[str] = [self.title, self.artists]
        if include_album and self.album:
            parts.append(self.album)

        # Using "audio" at the end helps yt-dlp avoid grabbing music videos.
        return " ".join(parts + ["audio"])

    def identifier(self, include_album: bool) -> str:
        """Create a stable key for the track to avoid duplicate downloads."""

        if not self.title and not self.artists:
            return ""

        parts = [self.title.strip().lower(), self.artists.strip().lower()]
        if include_album and self.album:
            parts.append(self.album.strip().lower())

        return " | ".join(filter(None, parts))


class DownloadCancelled(Exception):
    """Raised when a user cancels an in-progress download."""


class DownloadRateLimiter:
    """Throttle download throughput to a fixed rate per hour."""

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max_per_hour
        self._timestamps: Deque[float] = deque()

    def wait_for_slot(
        self,
        pause_event: Optional[threading.Event] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Block until a new download is allowed under the hourly cap."""

        if self.max_per_hour <= 0:
            return

        while True:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()

            while pause_event and pause_event.is_set():
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled()
                time.sleep(0.1)

            now = time.time()
            cutoff = now - 3600

            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) < self.max_per_hour:
                self._timestamps.append(now)
                return

            sleep_for = (self._timestamps[0] + 3600) - now
            sleep_for = max(sleep_for, 0)
            wait_minutes = sleep_for / 60
            print(
                f"Download limit reached ({self.max_per_hour}/hour). "
                f"Waiting ~{wait_minutes:.1f} minutes before continuing..."
            )
            time.sleep(sleep_for)


class TextRedirector:
    """Forward stdout/stderr writes into a Tkinter text widget with styling."""

    def __init__(self, widget: tk.Text):
        self.widget = widget

    @staticmethod
    def _classify(message: str) -> str:
        lower = message.lower()
        if "error" in lower or "failed" in lower:
            return "error"
        if "warning" in lower:
            return "warning"
        if "skipping" in lower or "skip" in lower:
            return "skip"
        if "complete" in lower or "downloaded" in lower:
            return "success"
        return "info"

    def write(self, message: str) -> None:  # pragma: no cover - UI side effect
        def append() -> None:
            tag = self._classify(message)
            self.widget.insert("end", message, (tag,))
            self.widget.see("end")

        self.widget.after(0, append)

    def flush(self) -> None:
        return


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download songs from an Exportify CSV by searching YouTube Music with yt-dlp. "
            "Each row becomes a search query composed of artist and track names."
        )
    )
    parser.add_argument(
        "csv_files",
        type=Path,
        nargs="*",
        help="One or more Exportify CSV files to process (can be set in config)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("exportify_downloader.toml"),
        help="Path to a TOML config file with default settings",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directory where downloaded audio files will be saved (default: downloads)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on the number of tracks to process",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="1-based position in the CSV to begin processing (default: 1)",
    )
    parser.add_argument(
        "--no-album",
        action="store_true",
        dest="include_album",
        default=None,
        help="Do not include the album name in the YouTube search query",
    )
    parser.add_argument(
        "--album",
        action="store_true",
        dest="include_album",
        help="Force include album name in the search query",
    )
    parser.add_argument(
        "--audio-format",
        default=None,
        help=(
            "Audio format for yt-dlp conversion (passed to FFmpeg). "
            "Use 'best' to skip conversion and keep the source format."
        ),
    )
    parser.add_argument(
        "--audio-quality",
        default=None,
        help="Audio quality for FFmpeg postprocessing (e.g., 128, 192, 320)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Print search queries without downloading any files",
    )
    parser.add_argument(
        "--search-provider",
        choices=["youtube-music", "youtube"],
        default=None,
        help=(
            "Where to search for tracks. 'youtube-music' resolves songs through the "
            "YouTube Music API; 'youtube' uses standard YouTube search queries."
        ),
    )
    parser.add_argument(
        "--max-downloads-per-hour",
        type=int,
        dest="max_downloads_per_hour",
        default=None,
        help="Throttle downloads to this many songs per hour (default: 100)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch a simple GUI for selecting options instead of CLI flags",
    )
    return parser.parse_args(argv)


def load_config(config_path: Path) -> dict:
    """Load a TOML configuration file if it exists."""

    if not config_path.exists():
        return {}

    with config_path.open("rb") as f:
        try:
            return tomllib.load(f)
        except Exception as exc:  # pragma: no cover - passthrough to caller
            raise ValueError(f"Failed to read config file {config_path}: {exc}")


def resolve_settings(args: argparse.Namespace) -> argparse.Namespace:
    """Merge CLI args with config values, preferring CLI when provided."""

    def normalize_csv_inputs(value) -> List[Path]:
        if not value:
            return []
        if isinstance(value, Path):
            return [value]
        if isinstance(value, (str, bytes)):
            return [Path(value)]
        if isinstance(value, Iterable):
            return [Path(item) for item in value if item]
        return []

    config = load_config(args.config)

    defaults = {
        "output": Path("downloads"),
        "limit": None,
        "start": 1,
        "include_album": True,
        "audio_format": "mp3",
        "audio_quality": "192",
        "dry_run": False,
        "search_provider": "youtube-music",
        "max_downloads_per_hour": 100,
    }

    # Flatten simple config keys
    config_section = config.get("exportify_downloader", {}) if isinstance(config, dict) else {}
    config_settings = {
        "csv_file": config_section.get("csv_file"),
        "csv_files": config_section.get("csv_files"),
        "output": Path(config_section["output"]) if config_section.get("output") else None,
        "limit": config_section.get("limit"),
        "start": config_section.get("start"),
        "include_album": config_section.get("include_album"),
        "audio_format": config_section.get("audio_format"),
        "audio_quality": config_section.get("audio_quality"),
        "dry_run": config_section.get("dry_run"),
        "search_provider": config_section.get("search_provider"),
        "max_downloads_per_hour": config_section.get("max_downloads_per_hour"),
    }

    resolved = argparse.Namespace()
    cli_csvs = normalize_csv_inputs(args.csv_files)
    config_csvs = normalize_csv_inputs(config_settings["csv_files"]) or normalize_csv_inputs(
        config_settings["csv_file"]
    )
    resolved.csv_files = cli_csvs or config_csvs
    resolved.csv_file = resolved.csv_files[0] if resolved.csv_files else None
    resolved.output = args.output or config_settings["output"] or defaults["output"]
    resolved.limit = args.limit if args.limit is not None else config_settings["limit"]
    if resolved.limit == 0:
        resolved.limit = None
    resolved.start = args.start if args.start is not None else config_settings["start"]
    if resolved.start is None or resolved.start <= 0:
        resolved.start = defaults["start"]
    resolved.include_album = (
        args.include_album
        if args.include_album is not None
        else (config_settings["include_album"] if config_settings["include_album"] is not None else defaults["include_album"])
    )
    resolved.audio_format = args.audio_format or config_settings["audio_format"] or defaults["audio_format"]
    resolved.audio_quality = args.audio_quality or config_settings["audio_quality"] or defaults["audio_quality"]
    resolved.dry_run = (
        args.dry_run
        if args.dry_run is not None
        else (config_settings["dry_run"] if config_settings["dry_run"] is not None else defaults["dry_run"])
    )
    resolved.search_provider = (
        args.search_provider
        or config_settings["search_provider"]
        or defaults["search_provider"]
    )
    resolved.max_downloads_per_hour = (
        args.max_downloads_per_hour
        if args.max_downloads_per_hour is not None
        else (
            config_settings["max_downloads_per_hour"]
            if config_settings["max_downloads_per_hour"] is not None
            else defaults["max_downloads_per_hour"]
        )
    )
    resolved.config = args.config
    return resolved


def read_tracks(csv_path: Path, limit: Optional[int], start: int) -> Iterable[Track]:
    """Yield Track objects from the Exportify CSV."""

    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        yielded = 0
        for index, row in enumerate(reader, start=1):
            if index < start:
                continue

            if limit is not None and yielded >= limit:
                break

            try:
                track = Track(
                    title=row.get("Track Name", "").strip(),
                    artists=row.get("Artist Name(s)", "").strip(),
                    album=row.get("Album Name", "").strip(),
                )
            except AttributeError:
                # If any field is None, ensure we still yield an empty string
                track = Track(
                    title=row.get("Track Name") or "",
                    artists=row.get("Artist Name(s)") or "",
                    album=row.get("Album Name") or "",
                )
            yield track
            yielded += 1


MAX_AUDIO_FILESIZE = 100 * 1024 * 1024  # 100 MB ceiling per track


def build_downloader(
    output_dir: Path,
    audio_format: str,
    audio_quality: str,
    progress_hook,
) -> yt_dlp.YoutubeDL:
    """Create a configured YoutubeDL instance for audio downloads."""

    def enforce_filesize_limit(info_dict, *, incomplete=False):
        """Skip downloads that exceed the maximum allowed filesize."""

        for key in ("filesize", "filesize_approx"):
            size = info_dict.get(key)
            if size is not None and size > MAX_AUDIO_FILESIZE:
                return (
                    f"{key} {size} exceeds cap of {MAX_AUDIO_FILESIZE} bytes; skipping"
                )

        return None

    ydl_opts = {
        "format": (
            "bestaudio[filesize<={limit}]/"
            "bestaudio[filesize_approx<={limit}]"
        ).format(limit=MAX_AUDIO_FILESIZE),
        "noplaylist": True,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "quiet": False,
        "ignoreerrors": True,
        "addmetadata": True,
        "embedthumbnail": True,
        "writethumbnail": True,
        "progress_hooks": [progress_hook],
        "match_filter": enforce_filesize_limit,
    }

    postprocessors = [
        {
            "key": "FFmpegMetadata",
        },
        {
            "key": "EmbedThumbnail",
        },
    ]

    if audio_format != "best":
        postprocessors.insert(
            0,
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": audio_quality,
            },
        )

    ydl_opts["postprocessors"] = postprocessors

    return yt_dlp.YoutubeDL(ydl_opts)


def search_ytmusic(ytmusic: YTMusic, terms: str) -> Tuple[Optional[str], Optional[str]]:
    """Return a YouTube Music URL and title if a song match is found."""

    if not terms:
        return None, None

    try:
        results = ytmusic.search(terms, filter="songs", limit=5)
    except Exception as exc:  # pragma: no cover - network/HTTP handled at runtime
        print(f"YouTube Music search failed for '{terms}': {exc}")
        return None, None

    for entry in results:
        if not isinstance(entry, dict):
            continue

        video_id = entry.get("videoId")
        title = entry.get("title")
        if video_id:
            return f"https://music.youtube.com/watch?v={video_id}", title

    return None, None


def download_tracks(
    tracks: Iterable[Track],
    downloader: yt_dlp.YoutubeDL,
    output_dir: Path,
    include_album: bool,
    dry_run: bool,
    search_provider: str,
    audio_format: str,
    max_downloads_per_hour: int = 100,
    progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    total_tracks: Optional[int] = None,
    start_index: int = 1,
    pause_event: Optional[threading.Event] = None,
    cancel_event: Optional[threading.Event] = None,
    existing_track_keys: Optional[Set[str]] = None,
) -> List[Tuple[Path, Optional[str]]]:
    downloaded_entries: List[Tuple[Path, Optional[str]]] = []
    rate_limiter = DownloadRateLimiter(max_downloads_per_hour)

    existing_track_keys = existing_track_keys or set()

    def wait_if_paused() -> None:
        while pause_event and pause_event.is_set():
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()
            time.sleep(0.1)

    if search_provider == "youtube-music":
        try:
            ytmusic_client = YTMusic()
        except Exception as exc:  # pragma: no cover - runtime environment difference
            print(
                "YouTube Music client could not be initialized; falling back to regular "
                f"YouTube search. ({exc})"
            )
            ytmusic_client = None
    else:
        ytmusic_client = None

    resolved_total = total_tracks
    if resolved_total is None:
        try:
            resolved_total = (start_index - 1) + len(tracks)  # type: ignore[arg-type]
        except TypeError:
            resolved_total = None

    for index, track in enumerate(tracks, start=start_index):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled()

        wait_if_paused()

        track_key = track.identifier(include_album=include_album)
        terms = track.build_terms(include_album=include_album)
        if not terms:
            print("Skipping row with missing track and artist info.")
            continue

        query = f"ytsearch5:{terms}"
        display = query

        sanitized_title = sanitize_filename(track.title, restricted=False)
        skipped_due_to_file = False
        if sanitized_title:
            candidate_exts = (
                [audio_format]
                if audio_format != "best"
                else ["mp3", "m4a", "opus", "webm", "mp4", "flac", "wav", "aac"]
            )
            for ext in candidate_exts:
                candidate_path = output_dir / f"{sanitized_title}.{ext}"
                if candidate_path.exists():
                    message = (
                        f"Skipping already downloaded track (file exists): "
                        f"{candidate_path.name}"
                    )
                    print(message)
                    downloaded_entries.append((candidate_path, track_key))
                    if track_key:
                        existing_track_keys.add(track_key)
                    if progress_callback:
                        progress_callback(
                            "finish", index, resolved_total or 0, f"Skipped: {display}"
                        )
                    skipped_due_to_file = True
                    break

        if skipped_due_to_file:
            continue

        if track_key and track_key in existing_track_keys:
            message = f"Skipping already downloaded track: {terms}"
            print(message)
            if progress_callback:
                progress_callback("finish", index, resolved_total or 0, f"Skipped: {display}")
            continue

        if ytmusic_client:
            url, matched_title = search_ytmusic(ytmusic_client, terms)
            if url:
                query = url
                display = matched_title or url

        if progress_callback:
            progress_callback("start", index, resolved_total or 0, display)

        print(f"Searching and downloading: {display}")
        if dry_run:
            if progress_callback:
                progress_callback("finish", index, resolved_total or 0, display)
            continue

        try:
            wait_if_paused()
            rate_limiter.wait_for_slot(pause_event=pause_event, cancel_event=cancel_event)
            wait_if_paused()
            info = downloader.extract_info(query, download=True)
            # extract_info may return a playlist of entries. Each entry has already
            # gone through post-processing, so capture their final filepaths when
            # available. Some yt-dlp versions can return non-dict objects (e.g.,
            # plain strings) on failure; guard these cases so we do not crash
            # when accessing `.get`.
            if not info:
                continue

            def record_filepath(entry: dict) -> None:
                filepath = entry.get("filepath") or entry.get("_filename")
                if filepath:
                    downloaded_entries.append((Path(filepath), track_key))
                    if track_key:
                        existing_track_keys.add(track_key)

            if isinstance(info, dict):
                entries = info.get("entries") if isinstance(info.get("entries"), list) else None
                if entries:
                    for entry in entries:
                        if isinstance(entry, dict):
                            record_filepath(entry)
                else:
                    record_filepath(info)
            elif isinstance(info, list):
                for entry in info:
                    if isinstance(entry, dict):
                        record_filepath(entry)

            if progress_callback:
                progress_callback("finish", index, resolved_total or 0, display)
        except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
            print(f"Failed to download {query}: {exc}")
            if progress_callback:
                progress_callback("error", index, resolved_total or 0, f"Failed: {display}")
        except AttributeError as exc:
            # Protect against unexpected result shapes that are not dictionaries.
            print(f"Skipped malformed download result for {display}: {exc}")
            if progress_callback:
                progress_callback("error", index, resolved_total or 0, f"Skipped: {display}")

    return downloaded_entries


def resolve_entry_path(path: Path, audio_format: str) -> Path:
    """Resolve the final audio path for a single downloaded entry."""

    if audio_format == "best":
        return path

    target_ext = f".{audio_format}"
    if path.suffix.lower() != target_ext.lower():
        candidate = path.with_suffix(target_ext)
        if candidate.exists():
            return candidate

    return path


def load_download_manifest(manifest_path: Path) -> Dict[str, str]:
    """Load track identifiers mapped to downloaded file paths."""

    if not manifest_path.exists():
        return {}

    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read manifest {manifest_path}: {exc}")
        return {}


def save_download_manifest(manifest_path: Path, manifest: Dict[str, str]) -> None:
    """Persist the manifest of downloaded tracks to disk."""

    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def write_m3u_playlist(playlist_path: Path, downloaded_files: List[Path], output_dir: Path) -> None:
    """Write an M3U playlist with paths relative to the output directory."""

    if not downloaded_files:
        print("No files were downloaded; skipping playlist creation.")
        return

    lines = ["#EXTM3U"]
    for file_path in downloaded_files:
        try:
            relative_path = file_path.relative_to(output_dir)
            lines.append(str(relative_path))
        except ValueError:
            lines.append(str(file_path))

    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Created playlist: {playlist_path}")


def create_backup_m3u_from_downloads(
    csv_path: Path, output_base: Path, log: Callable[[str], None] = print
) -> None:
    """Create a backup M3U using existing MP3 files for the given CSV."""

    playlist_dir = output_base / csv_path.stem
    if not playlist_dir.exists():
        log(f"No download folder found for {csv_path.name} at {playlist_dir}")
        return

    mp3_files = sorted(
        (path for path in playlist_dir.iterdir() if path.suffix.lower() == ".mp3"),
        key=lambda p: p.name.lower(),
    )

    if not mp3_files:
        log(f"No MP3 files found in {playlist_dir}; skipping backup playlist creation.")
        return

    backup_playlist = playlist_dir / f"{csv_path.stem}_backup.m3u"
    write_m3u_playlist(backup_playlist, mp3_files, playlist_dir)


def resolve_final_audio_paths(downloaded_files: List[Path], audio_format: str) -> List[Path]:
    """Prefer post-processed audio files (e.g., MP3) over original downloads."""

    if audio_format == "best":
        return downloaded_files

    resolved: List[Path] = []
    seen = set()
    for path in downloaded_files:
        final_path = path
        target_ext = f".{audio_format}"
        if path.suffix.lower() != target_ext.lower():
            candidate = path.with_suffix(target_ext)
            if candidate.exists():
                final_path = candidate

        if final_path not in seen:
            resolved.append(final_path)
            seen.add(final_path)

    return resolved


def run_downloader_for_csv(
    csv_path: Path,
    args: argparse.Namespace,
    progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    pause_event: Optional[threading.Event] = None,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Execute the downloader for a single CSV file."""

    if not csv_path.exists():
        print(f"CSV file not found: {csv_path}", file=sys.stderr)
        return 1

    playlist_dir = args.output / csv_path.stem
    playlist_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = playlist_dir / ".downloaded_tracks.json"
    manifest = load_download_manifest(manifest_path)
    cleaned_manifest: Dict[str, str] = {}
    existing_track_keys: Set[str] = set()

    for track_key, stored_path in manifest.items():
        resolved_path = Path(stored_path)
        if not resolved_path.is_absolute():
            resolved_path = playlist_dir / resolved_path
        if resolved_path.exists():
            cleaned_manifest[track_key] = stored_path
            existing_track_keys.add(track_key)

    tracks = list(read_tracks(csv_path, args.limit, args.start))
    if not tracks:
        print("No tracks found in CSV. Nothing to download.")
        return 0

    print(
        f"Found {len(tracks)} tracks. Output directory: {playlist_dir.resolve()}"
    )
    downloaded_files: List[Path] = []

    def progress_hook(status):
        if not isinstance(status, dict):
            return

        if status.get("status") == "finished":
            info = status.get("info_dict") if isinstance(status.get("info_dict"), dict) else {}
            filepath = info.get("filepath") or status.get("filename")
            if filepath:
                downloaded_files.append(Path(filepath))

    downloader = build_downloader(
        playlist_dir, args.audio_format, args.audio_quality, progress_hook
    )
    try:
        extracted_entries = download_tracks(
            tracks,
            downloader,
            playlist_dir,
            include_album=args.include_album,
            dry_run=args.dry_run,
            search_provider=args.search_provider,
            audio_format=args.audio_format,
            max_downloads_per_hour=args.max_downloads_per_hour,
            progress_callback=progress_callback,
            total_tracks=(args.start - 1) + len(tracks),
            start_index=args.start,
            pause_event=pause_event,
            cancel_event=cancel_event,
            existing_track_keys=existing_track_keys,
        )
    except DownloadCancelled:
        print("Download cancelled by user.")
        return 1

    for file_path, _ in extracted_entries:
        if file_path not in downloaded_files:
            downloaded_files.append(file_path)

    resolved_entries: List[Tuple[Path, Optional[str]]] = []
    for path, track_key in extracted_entries:
        final_path = resolve_entry_path(path, args.audio_format)
        resolved_entries.append((final_path, track_key))

    downloaded_files = resolve_final_audio_paths(downloaded_files, args.audio_format)
    playlist_path = playlist_dir / f"{csv_path.stem}.m3u"
    write_m3u_playlist(playlist_path, downloaded_files, playlist_dir)

    for final_path, track_key in resolved_entries:
        if not track_key or not final_path.exists():
            continue

        try:
            stored_path = final_path.relative_to(playlist_dir)
        except ValueError:
            stored_path = final_path

        cleaned_manifest[track_key] = str(stored_path)
        existing_track_keys.add(track_key)
        save_download_manifest(manifest_path, cleaned_manifest)

    return 0


def run_downloader(
    args: argparse.Namespace,
    progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    pause_event: Optional[threading.Event] = None,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Execute the downloader with already-resolved settings."""

    if not args.csv_files:
        print("No CSV file provided via CLI or config.", file=sys.stderr)
        return 1

    status = 0
    total_files = len(args.csv_files)
    for file_index, csv_path in enumerate(args.csv_files, start=1):
        print(f"\nProcessing CSV {file_index}/{total_files}: {csv_path}")
        if progress_callback:
            progress_callback("file", file_index, total_files, str(csv_path))
        result = run_downloader_for_csv(
            csv_path,
            args,
            progress_callback=progress_callback,
            pause_event=pause_event,
            cancel_event=cancel_event,
        )
        if cancel_event and cancel_event.is_set():
            return 1
        if result != 0:
            status = result

    return status


def launch_gui(defaults: argparse.Namespace) -> None:
    """Launch a lightweight Tkinter GUI for selecting downloader options."""

    root = tk.Tk()
    root.title("Exportify YouTube Music Downloader")
    root.geometry("700x500")

    csv_paths: List[str] = [str(path) for path in getattr(defaults, "csv_files", [])] or (
        [str(defaults.csv_file)] if getattr(defaults, "csv_file", None) else []
    )
    csv_display_var = tk.StringVar()
    output_var = tk.StringVar(value=str(defaults.output) if defaults.output else "")
    limit_var = tk.StringVar(value=str(defaults.limit or ""))
    start_var = tk.StringVar(value=str(defaults.start or 1))
    album_var = tk.BooleanVar(value=defaults.include_album)
    dry_run_var = tk.BooleanVar(value=defaults.dry_run)
    audio_format_var = tk.StringVar(value=defaults.audio_format)
    audio_quality_var = tk.StringVar(value=defaults.audio_quality)
    provider_var = tk.StringVar(value=defaults.search_provider)
    max_per_hour_var = tk.StringVar(value=str(defaults.max_downloads_per_hour))
    pause_event = threading.Event()
    cancel_event = threading.Event()
    download_thread: Optional[threading.Thread] = None

    def refresh_csv_display() -> None:
        display_value = "\n".join(csv_paths) if csv_paths else "No CSVs selected."
        csv_display_var.set(display_value)

    def browse_csvs() -> None:
        paths = filedialog.askopenfilenames(
            title="Select Exportify CSVs", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if paths:
            for path in paths:
                if path not in csv_paths:
                    csv_paths.append(path)
            refresh_csv_display()

    def clear_csvs() -> None:
        csv_paths.clear()
        refresh_csv_display()

    refresh_csv_display()

    def browse_output() -> None:
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            output_var.set(path)

    form = ttk.Frame(root, padding=10)
    form.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    def add_row(label: str, widget: tk.Widget, row: int, button: Optional[tk.Widget] = None) -> None:
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        form.columnconfigure(1, weight=1)
        if button:
            button.grid(row=row, column=2, padx=4, pady=4)

    csv_display = ttk.Label(
        form,
        textvariable=csv_display_var,
        relief="solid",
        padding=6,
        anchor="w",
        justify="left",
    )
    csv_buttons = ttk.Frame(form)
    ttk.Button(csv_buttons, text="Browse", command=browse_csvs).pack(side="left", padx=(0, 4))
    ttk.Button(csv_buttons, text="Clear", command=clear_csvs).pack(side="left")
    add_row("CSV files", csv_display, 0, csv_buttons)
    add_row(
        "Output folder",
        ttk.Entry(form, textvariable=output_var),
        1,
        ttk.Button(form, text="Browse", command=browse_output),
    )
    add_row("Limit (0 = all)", ttk.Entry(form, textvariable=limit_var), 2)
    add_row("Start at row", ttk.Entry(form, textvariable=start_var), 3)
    add_row("Audio format", ttk.Entry(form, textvariable=audio_format_var), 4)
    add_row("Audio quality", ttk.Entry(form, textvariable=audio_quality_var), 5)
    add_row(
        "Search provider",
        ttk.Combobox(
            form,
            textvariable=provider_var,
            values=["youtube-music", "youtube"],
            state="readonly",
        ),
        6,
    )
    add_row("Max downloads/hour", ttk.Entry(form, textvariable=max_per_hour_var), 7)

    flags = ttk.Frame(form)
    flags.grid(row=8, column=0, columnspan=3, sticky="w", pady=6)
    ttk.Checkbutton(flags, text="Include album in search", variable=album_var).grid(
        row=0, column=0, padx=(0, 12)
    )
    ttk.Checkbutton(flags, text="Dry run", variable=dry_run_var).grid(row=0, column=1)

    log_frame = ttk.LabelFrame(root, text="Progress", padding=10)
    log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Success.Horizontal.TProgressbar", background="#2da44e", troughcolor="#e5f6e5")
    style.configure("Error.Horizontal.TProgressbar", background="#d1242f", troughcolor="#fbeae5")
    style.configure("Active.Horizontal.TProgressbar", background="#218bff", troughcolor="#e7f3ff")

    status_var = tk.StringVar(value="Idle")
    ttk.Label(log_frame, textvariable=status_var, anchor="w").pack(fill="x")

    progress_var = tk.DoubleVar(value=0)
    progress_bar = ttk.Progressbar(log_frame, variable=progress_var, maximum=1)
    progress_bar.pack(fill="x", pady=(4, 8))

    track_list_frame = ttk.Frame(log_frame)
    track_list_frame.pack(fill="both", expand=False, pady=(0, 8))
    track_canvas = tk.Canvas(track_list_frame, height=160, highlightthickness=0)
    track_auto_scroll = tk.BooleanVar(value=True)

    def update_auto_scroll_state() -> None:
        _, last = track_canvas.yview()
        track_auto_scroll.set(abs(1.0 - last) < 0.01)

    def scroll_to_bottom_if_needed() -> None:
        if track_auto_scroll.get():
            track_canvas.yview_moveto(1.0)

    def scroll_via_scrollbar(*args) -> None:
        track_canvas.yview(*args)
        update_auto_scroll_state()

    track_scrollbar = ttk.Scrollbar(track_list_frame, orient="vertical", command=scroll_via_scrollbar)
    track_container = ttk.Frame(track_canvas)
    track_container.bind(
        "<Configure>", lambda e: (track_canvas.configure(scrollregion=track_canvas.bbox("all")), scroll_to_bottom_if_needed())
    )
    track_canvas.create_window((0, 0), window=track_container, anchor="nw", tags="track_container")
    track_canvas.bind(
        "<Configure>",
        lambda e: track_canvas.itemconfigure("track_container", width=e.width),
    )
    def on_mousewheel(event) -> str:
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            track_canvas.yview_scroll(delta, "units")
            update_auto_scroll_state()
        return "break"

    def on_mousewheel_linux(event) -> str:
        delta = -1 if event.num == 5 else 1
        track_canvas.yview_scroll(delta, "units")
        update_auto_scroll_state()
        return "break"

    track_canvas.bind("<MouseWheel>", on_mousewheel)
    track_canvas.bind("<Button-4>", on_mousewheel_linux)
    track_canvas.bind("<Button-5>", on_mousewheel_linux)
    track_canvas.configure(yscrollcommand=track_scrollbar.set)
    track_canvas.pack(side="left", fill="both", expand=True)
    track_scrollbar.pack(side="right", fill="y")

    log_text = tk.Text(log_frame, wrap="word")
    log_text.tag_configure("info", foreground="#1f2328")
    log_text.tag_configure("success", foreground="#2da44e")
    log_text.tag_configure("warning", foreground="#9a6700")
    log_text.tag_configure("error", foreground="#d1242f")
    log_text.tag_configure("skip", foreground="#57606a")
    log_text.pack(fill="both", expand=True)

    def append_log_line(message: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", message + "\n")
        log_text.see("end")

    track_widgets: Dict[int, Tuple[ttk.Progressbar, tk.DoubleVar, tk.StringVar]] = {}

    def reset_track_statuses() -> None:
        for child in track_container.winfo_children():
            child.destroy()
        track_widgets.clear()
        track_auto_scroll.set(True)
        scroll_to_bottom_if_needed()

    def ensure_track_widget(
        index: int, description: str
    ) -> Tuple[ttk.Progressbar, tk.DoubleVar, tk.StringVar]:
        truncated = description if len(description) <= 70 else description[:67] + "..."
        if index not in track_widgets:
            item = ttk.Frame(track_container, padding=(0, 2))
            label_var = tk.StringVar(value=f"{index}. {truncated}")
            ttk.Label(item, textvariable=label_var, anchor="w").pack(fill="x")
            progress_var = tk.DoubleVar(value=0)
            bar = ttk.Progressbar(
                item,
                variable=progress_var,
                maximum=1,
                style="Active.Horizontal.TProgressbar",
            )
            bar.pack(fill="x", pady=(0, 2))
            item.pack(fill="x", anchor="w")
            track_widgets[index] = (bar, progress_var, label_var)
            root.after_idle(scroll_to_bottom_if_needed)
        bar, progress_var, label_var = track_widgets[index]
        label_var.set(f"{index}. {truncated}")
        return bar, progress_var, label_var

    def reset_controls() -> None:
        download_button.config(state=tk.NORMAL)
        pause_button.config(state=tk.DISABLED, text="Pause")
        cancel_button.config(state=tk.DISABLED)
        pause_event.clear()
        cancel_event.clear()
        status_var.set("Idle")
        reset_track_statuses()

    def toggle_pause() -> None:
        nonlocal download_thread
        if not download_thread or not download_thread.is_alive():
            return
        if pause_event.is_set():
            pause_event.clear()
            pause_button.config(text="Pause")
            status_var.set("Resuming downloads...")
        else:
            pause_event.set()
            pause_button.config(text="Resume")
            status_var.set("Paused")

    def cancel_download() -> None:
        nonlocal download_thread
        if not download_thread or not download_thread.is_alive():
            return
        cancel_event.set()
        pause_event.clear()
        pause_button.config(text="Pause")
        status_var.set("Cancelling downloads...")

    def create_m3u_backup() -> None:
        nonlocal download_thread
        if download_thread and download_thread.is_alive():
            messagebox.showinfo(
                "Download in progress",
                "Please wait for downloads to finish before creating backup playlists.",
            )
            return

        if not csv_paths:
            messagebox.showerror(
                "Missing CSV", "Select at least one CSV to build backup playlists."
            )
            return

        output_base = Path(output_var.get().strip() or str(defaults.output))
        append_log_line("Creating backup playlists...")

        for csv_str in csv_paths:
            create_backup_m3u_from_downloads(Path(csv_str), output_base, log=append_log_line)

        append_log_line("Backup playlist creation finished.")

    def start_download() -> None:
        nonlocal download_thread
        if not csv_paths:
            messagebox.showerror("Missing CSV", "Please select at least one CSV file to download from.")
            return

        output_base = output_var.get().strip() or str(defaults.output)

        try:
            limit_val = int(limit_var.get()) if limit_var.get().strip() else None
            if limit_val == 0:
                limit_val = None
        except ValueError:
            messagebox.showerror("Invalid limit", "Limit must be a number.")
            return

        try:
            start_val = int(start_var.get()) if start_var.get().strip() else defaults.start
            if start_val <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid start position", "Start position must be 1 or greater."
            )
            return

        try:
            max_per_hour = (
                int(max_per_hour_var.get()) if max_per_hour_var.get().strip() else defaults.max_downloads_per_hour
            )
        except ValueError:
            messagebox.showerror("Invalid rate limit", "Max downloads per hour must be a number.")
            return

        cancel_event.clear()
        pause_event.clear()
        progress_var.set(0)
        status_var.set("Starting...")
        progress_bar.configure(maximum=1)
        reset_track_statuses()

        selected_csvs = [Path(path) for path in csv_paths]

        args = argparse.Namespace(
            csv_files=selected_csvs,
            csv_file=selected_csvs[0] if selected_csvs else None,
            output=Path(output_base),
            limit=limit_val,
            start=start_val,
            include_album=album_var.get(),
            audio_format=audio_format_var.get() or defaults.audio_format,
            audio_quality=audio_quality_var.get() or defaults.audio_quality,
            dry_run=dry_run_var.get(),
            search_provider=provider_var.get(),
            max_downloads_per_hour=max_per_hour,
            config=defaults.config,
            gui=False,
        )

        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.insert("end", "Starting downloads...\n")
        log_text.see("end")
        download_button.config(state=tk.DISABLED)
        pause_button.config(state=tk.NORMAL, text="Pause")
        cancel_button.config(state=tk.NORMAL)

        def progress_update(event: str, index: int, total: int, description: str) -> None:
            def update_ui() -> None:
                if event == "file":
                    progress_bar.configure(maximum=1)
                    progress_var.set(0)
                    status_var.set(f"File {index}/{max(total, 1)}: {description}")
                    reset_track_statuses()
                    return

                if total <= 0:
                    total_value = max(progress_bar["maximum"], index)
                else:
                    total_value = total

                progress_bar.configure(maximum=total_value)

                if event == "start":
                    track_bar, track_value, _ = ensure_track_widget(index, description)
                    track_bar.configure(style="Active.Horizontal.TProgressbar", maximum=1)
                    track_value.set(0)
                elif event == "finish":
                    track_bar, track_value, _ = ensure_track_widget(index, description)
                    track_bar.configure(style="Success.Horizontal.TProgressbar", maximum=1)
                    track_value.set(1)
                elif event == "error":
                    track_bar, track_value, _ = ensure_track_widget(index, description)
                    track_bar.configure(style="Error.Horizontal.TProgressbar", maximum=1)
                    track_value.set(1)

                if event == "start":
                    progress_var.set(max(index - 1, 0))
                elif event in {"finish", "error"}:
                    progress_var.set(index)

                if total_value:
                    status_var.set(f"{event.title()} {index}/{int(total_value)}: {description}")
                else:
                    status_var.set(f"{event.title()}: {description}")

            root.after(0, update_ui)

        def worker() -> None:
            writer = TextRedirector(log_text)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                result = run_downloader(
                    args,
                    progress_callback=progress_update,
                    pause_event=pause_event,
                    cancel_event=cancel_event,
                )
                if cancel_event.is_set():
                    print("Downloads cancelled.")
                    status_var.set("Downloads cancelled.")
                elif result == 0:
                    print("Downloads complete.")
                    status_var.set("Downloads complete.")
                else:
                    print("Downloader exited with errors.")
                    status_var.set("Downloader exited with errors.")

            root.after(0, reset_controls)

        download_thread = threading.Thread(target=worker, daemon=True)
        download_thread.start()

    controls = ttk.Frame(root)
    controls.grid(row=2, column=0, pady=(0, 10))

    download_button = ttk.Button(controls, text="Start Download", command=start_download)
    download_button.grid(row=0, column=0, padx=(0, 8))
    pause_button = ttk.Button(controls, text="Pause", command=toggle_pause, state=tk.DISABLED)
    pause_button.grid(row=0, column=1, padx=4)
    cancel_button = ttk.Button(controls, text="Cancel", command=cancel_download, state=tk.DISABLED)
    cancel_button.grid(row=0, column=2, padx=(4, 0))

    backup_button = ttk.Button(controls, text="Create M3U Backup", command=create_m3u_backup)
    backup_button.grid(row=1, column=0, columnspan=3, pady=(6, 0))

    root.mainloop()


def main(argv: Optional[List[str]] = None) -> int:
    args = resolve_settings(parse_args(argv))
    if getattr(args, "gui", False):
        launch_gui(args)
        return 0

    if not args.csv_files:
        print("No CSV file provided via CLI or config. Launching GUI for selection...")
        try:
            launch_gui(args)
            return 0
        except tk.TclError as exc:
            print(
                "Unable to open the GUI automatically. "
                "Provide a CSV path via CLI flags or run with --gui instead.",
                file=sys.stderr,
            )
            print(exc, file=sys.stderr)
            return 1

    return run_downloader(args)


if __name__ == "__main__":
    raise SystemExit(main())
