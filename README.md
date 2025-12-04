# Exportify YouTube Downloader

A small command-line helper that reads an [Exportify](https://watsonbox.github.io/exportify/) CSV
playlist export and downloads each track from YouTube using `yt-dlp`.

## Requirements
- Python 3.10+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (installed via `pip install -r requirements.txt`)
- [`ffmpeg`](https://ffmpeg.org/) in your `PATH` when using audio conversion (the default)

## Installation
```
pip install -r requirements.txt
```

## Usage
```
python exportify_downloader.py <playlist.csv> [--output downloads] [--limit 5]
```

Key options:
- `--output`: Directory for the downloaded audio files (defaults to `downloads`).
- `--limit`: Only process the first N tracks from the CSV.
- `--no-album`: Build search queries without the album name.
- `--audio-format`: Audio format passed to FFmpeg (default: `mp3`). Use `best` to keep the
  original stream without conversion.
- `--audio-quality`: FFmpeg audio quality/bitrate (e.g., `192`, `320`).
- `--dry-run`: Show the generated search queries without downloading anything.

## How it works
1. Each CSV row is converted into a search query composed of the artist, track name, and
   optionally the album name.
2. `yt-dlp` searches YouTube using `ytsearch1:<query>` to grab the best match.
3. The best audio stream is downloaded; by default it is converted to MP3 via FFmpeg.

## Example
```
python exportify_downloader.py example.csv --output playlist_downloads --limit 10
```

This will download the first ten tracks from `example.csv` into `playlist_downloads`.
