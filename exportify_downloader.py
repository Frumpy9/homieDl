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
from typing import Iterable, List, Optional

import yt_dlp


@dataclass
class Track:
    """A single track entry extracted from the Exportify CSV."""

    title: str
    artists: str
    album: str

    def build_query(self, include_album: bool, search_prefix: str) -> str:
        """Build a search query that always includes title and artist.

        Album stays optional, but we always lead with track + artist and append
        the word "audio" to bias results away from music videos.
        """

        if not self.title or not self.artists:
            return ""

        parts: List[str] = [self.title, self.artists]
        if include_album and self.album:
            parts.append(self.album)

        # Using "audio" at the end helps yt-dlp avoid grabbing music videos.
        query = " ".join(parts + ["audio"])
        return f"{search_prefix}:{query}"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download songs from an Exportify CSV by searching YouTube Music with yt-dlp. "
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
    parser.add_argument(
        "--search-provider",
        choices=["youtube-music", "youtube"],
        default="youtube-music",
        help=(
            "Where to search for tracks. 'youtube-music' uses yt-dlp's ytmusicsearch "
            "and embeds metadata from the YouTube Music result; 'youtube' falls back "
            "to the regular YouTube search."
        ),
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
        # Avoid music videos by rejecting common video markers; the search query
        # already appends "audio", but this adds an extra safeguard.
        "match_filter": yt_dlp.utils.match_filter_func(
            " & ".join(
                [
                    "!is_live",
                    "!contains(title, 'music video')",
                    "!contains(title, 'official video')",
                    "!contains(title, 'official music video')",
                ]
            )
        ),
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


def download_tracks(
    tracks: Iterable[Track],
    downloader: yt_dlp.YoutubeDL,
    include_album: bool,
    dry_run: bool,
    search_prefix: str,
) -> List[Path]:
    downloaded_files: List[Path] = []

    for track in tracks:
        query = track.build_query(include_album=include_album, search_prefix=search_prefix)
        if not query:
            print("Skipping row with missing track and artist info.")
            continue

        print(f"Searching and downloading: {query}")
        if dry_run:
            continue

        try:
            info = downloader.extract_info(query, download=True)
            # extract_info may return a playlist of entries. Each entry has already
            # gone through post-processing, so capture their final filepaths when
            # available.
            if info:
                entries = info.get("entries") if isinstance(info, dict) else None
                if entries:
                    for entry in entries:
                        filepath = entry.get("filepath") or entry.get("_filename")
                        if filepath:
                            downloaded_files.append(Path(filepath))
                else:
                    filepath = info.get("filepath") or info.get("_filename")
                    if filepath:
                        downloaded_files.append(Path(filepath))
        except yt_dlp.utils.DownloadError as exc:  # type: ignore[attr-defined]
            print(f"Failed to download {query}: {exc}")

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

    search_prefix = "ytmusicsearch5" if args.search_provider == "youtube-music" else "ytsearch5"

    print(
        f"Found {len(tracks)} tracks. Output directory: {args.output.resolve()}"
    )
    downloaded_files: List[Path] = []

    def progress_hook(status):
        if status.get("status") == "finished":
            info = status.get("info_dict") or {}
            filepath = info.get("filepath") or status.get("filename")
            if filepath:
                downloaded_files.append(Path(filepath))

    downloader = build_downloader(
        args.output, args.audio_format, args.audio_quality, progress_hook
    )
    extracted_files = download_tracks(
        tracks,
        downloader,
        include_album=include_album,
        dry_run=args.dry_run,
        search_prefix=search_prefix,
    )

    for file_path in extracted_files:
        if file_path not in downloaded_files:
            downloaded_files.append(file_path)

    playlist_path = args.output / f"{args.csv_file.stem}.m3u"
    write_m3u_playlist(playlist_path, downloaded_files, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
