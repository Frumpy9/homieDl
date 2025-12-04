"""Download tracks listed in an Exportify CSV by searching YouTube with yt-dlp.

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
from typing import Iterable, List, Optional

import yt_dlp


@dataclass
class Track:
    """A single track entry extracted from the Exportify CSV."""

    title: str
    artists: str
    album: str

    def build_query(self, include_album: bool) -> str:
        parts: List[str] = []
        if self.artists:
            parts.append(self.artists)
        if self.title:
            parts.append(self.title)
        if include_album and self.album:
            parts.append(self.album)
        query = " ".join(parts)
        return f"ytsearch1:{query}" if query else ""


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download songs from an Exportify CSV by searching YouTube with yt-dlp. "
            "Each row becomes a search query composed of artist and track names."
        )
    )
    parser.add_argument("csv_file", type=Path, help="Path to the Exportify CSV file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("downloads"),
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
        help="Do not include the album name in the YouTube search query",
    )
    parser.add_argument(
        "--audio-format",
        default="mp3",
        help=(
            "Audio format for yt-dlp conversion (passed to FFmpeg). "
            "Use 'best' to skip conversion and keep the source format."
        ),
    )
    parser.add_argument(
        "--audio-quality",
        default="192",
        help="Audio quality for FFmpeg postprocessing (e.g., 128, 192, 320)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print search queries without downloading any files",
    )
    return parser.parse_args(argv)


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


def build_downloader(output_dir: Path, audio_format: str, audio_quality: str) -> yt_dlp.YoutubeDL:
    """Create a configured YoutubeDL instance for audio downloads."""

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "quiet": False,
        "ignoreerrors": True,
    }

    if audio_format != "best":
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": audio_quality,
            }
        ]

    return yt_dlp.YoutubeDL(ydl_opts)


def download_tracks(tracks: Iterable[Track], downloader: yt_dlp.YoutubeDL, include_album: bool, dry_run: bool) -> None:
    for track in tracks:
        query = track.build_query(include_album=include_album)
        if not query:
            print("Skipping row with missing track and artist info.")
            continue

        print(f"Searching and downloading: {query}")
        if dry_run:
            continue

        try:
            downloader.download([query])
        except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
            print(f"Failed to download {query}: {exc}")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if not args.csv_file.exists():
        print(f"CSV file not found: {args.csv_file}", file=sys.stderr)
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    tracks = list(read_tracks(args.csv_file, args.limit))
    if not tracks:
        print("No tracks found in CSV. Nothing to download.")
        return 0

    include_album = not args.no_album

    print(
        f"Found {len(tracks)} tracks. Output directory: {args.output.resolve()}"
    )
    downloader = build_downloader(args.output, args.audio_format, args.audio_quality)
    download_tracks(tracks, downloader, include_album=include_album, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
