"""Download tracks listed in an Exportify CSV by searching YouTube Music with yt-dlp.

Usage example:
    python exportify_downloader.py playlist.csv --output downloads

The script reads the Exportify CSV, builds YouTube search queries using the
artist and track names, and downloads the best audio stream (optionally
converting it to MP3). See `python exportify_downloader.py --help` for all
options.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older Pythons
    import tomli as tomllib

import yt_dlp
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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download songs from an Exportify CSV by searching YouTube Music with yt-dlp. "
            "Each row becomes a search query composed of artist and track names."
        )
    )
    parser.add_argument(
        "csv_file",
        type=Path,
        nargs="?",
        help="Path to the Exportify CSV file (can be set in config)",
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

    config = load_config(args.config)

    defaults = {
        "output": Path("downloads"),
        "limit": None,
        "include_album": True,
        "audio_format": "mp3",
        "audio_quality": "192",
        "dry_run": False,
        "search_provider": "youtube-music",
    }

    # Flatten simple config keys
    config_section = config.get("exportify_downloader", {}) if isinstance(config, dict) else {}
    config_settings = {
        "csv_file": config_section.get("csv_file"),
        "output": Path(config_section["output"]) if config_section.get("output") else None,
        "limit": config_section.get("limit"),
        "include_album": config_section.get("include_album"),
        "audio_format": config_section.get("audio_format"),
        "audio_quality": config_section.get("audio_quality"),
        "dry_run": config_section.get("dry_run"),
        "search_provider": config_section.get("search_provider"),
    }

    resolved = argparse.Namespace()
    resolved.csv_file = args.csv_file or config_settings["csv_file"]
    resolved.output = args.output or config_settings["output"] or defaults["output"]
    resolved.limit = args.limit if args.limit is not None else config_settings["limit"]
    if resolved.limit == 0:
        resolved.limit = None
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
    resolved.config = args.config
    return resolved


def read_tracks(csv_path: Path, limit: Optional[int]) -> Iterable[Track]:
    """Yield Track objects from the Exportify CSV."""

    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        for index, row in enumerate(reader):
            if limit is not None and index >= limit:
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


def build_downloader(
    output_dir: Path,
    audio_format: str,
    audio_quality: str,
    progress_hook,
) -> yt_dlp.YoutubeDL:
    """Create a configured YoutubeDL instance for audio downloads."""

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "quiet": False,
        "ignoreerrors": True,
        "addmetadata": True,
        "embedthumbnail": True,
        "writethumbnail": True,
        "progress_hooks": [progress_hook],
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
) -> List[Path]:
    downloaded_files: List[Path] = []

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

    for track in tracks:
        terms = track.build_terms(include_album=include_album)
        if not terms:
            print("Skipping row with missing track and artist info.")
            continue

        query = f"ytsearch5:{terms}"
        display = query

        if ytmusic_client:
            url, matched_title = search_ytmusic(ytmusic_client, terms)
            if url:
                query = url
                display = matched_title or url

        print(f"Searching and downloading: {display}")
        if dry_run:
            continue

        try:
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
                    downloaded_files.append(Path(filepath))

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
        except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
            print(f"Failed to download {query}: {exc}")
        except AttributeError as exc:
            # Protect against unexpected result shapes that are not dictionaries.
            print(f"Skipped malformed download result for {display}: {exc}")

    return downloaded_files


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


def main(argv: Optional[List[str]] = None) -> int:
    args = resolve_settings(parse_args(argv))

    if not args.csv_file:
        print("No CSV file provided via CLI or config.", file=sys.stderr)
        return 1

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"CSV file not found: {csv_path}", file=sys.stderr)
        return 1

    playlist_dir = args.output / csv_path.stem
    playlist_dir.mkdir(parents=True, exist_ok=True)

    tracks = list(read_tracks(csv_path, args.limit))
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
    extracted_files = download_tracks(
        tracks,
        downloader,
        playlist_dir,
        include_album=args.include_album,
        dry_run=args.dry_run,
        search_provider=args.search_provider,
    )

    for file_path in extracted_files:
        if file_path not in downloaded_files:
            downloaded_files.append(file_path)

    playlist_path = playlist_dir / f"{csv_path.stem}.m3u"
    write_m3u_playlist(playlist_path, downloaded_files, playlist_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
