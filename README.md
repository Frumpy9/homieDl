# Exportify YouTube Music Downloader

A small command-line helper that reads an [Exportify](https://watsonbox.github.io/exportify/) CSV
playlist export and downloads each track from **YouTube Music** using `yt-dlp`.

## Requirements
- Python 3.10+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (installed via `pip install -r requirements.txt`)
- [`ffmpeg`](https://ffmpeg.org/) in your `PATH` when using audio conversion (the default)
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) for accurate YouTube Music lookups

## Installation
```
pip install -r requirements.txt
```

## Usage
```
python exportify_downloader.py <playlist.csv> [--output downloads] [--limit 5]
```

Key options:
- `--output`: Base directory for downloads (defaults to `downloads`). A subfolder with the
  playlist name is created automatically (e.g., `downloads/<playlist-name>/`).
- `--limit`: Only process the first N tracks from the CSV.
- `--max-downloads-per-hour`: Enforce a rolling cap on how many tracks start
  downloading each hour (default: 100). Set to `0` to disable.
- `--no-album`: Build search queries without the album name.
- `--search-provider`: Choose YouTube Music (default) or regular YouTube search.
- `--audio-format`: Audio format passed to FFmpeg (default: `mp3`). Use `best` to keep the
  original stream without conversion.
- `--audio-quality`: FFmpeg audio quality/bitrate (e.g., `192`, `320`).
- `--dry-run`: Show the generated search queries without downloading anything.

### Config file
- A TOML config named `exportify_downloader.toml` is read by default (override with
  `--config path/to/file`).
- Place your defaults under the `[exportify_downloader]` section; CLI flags override
  any values in the config.

Example `exportify_downloader.toml`:

```
[exportify_downloader]
csv_file = "playlist.csv"      # Optional: let the tool read this CSV without a CLI argument
output = "downloads"            # Base folder; the playlist name becomes a subfolder
limit = 0                       # 0 means no limit
include_album = true            # Include album name in search queries
audio_format = "mp3"            # Use "best" to keep the source format
audio_quality = "192"           # FFmpeg bitrate when converting
search_provider = "youtube-music"  # Or "youtube"
dry_run = false                 # Only print queries when true
max_downloads_per_hour = 100    # Rate limit to avoid throttling (0 disables)
```

## How it works
1. Each CSV row is converted into a search query composed of the **track name and artist name**
   (album is optional). The term `audio` is appended to bias results toward official audio.
2. The tool asks **YouTube Music** for song results using `ytmusicapi`; if nothing is found or
   if the Music client cannot initialize, it falls back to a regular YouTube `ytsearch5:<query>`
 query. The search terms include "audio" to steer toward audio uploads rather than videos.
3. The best audio stream is downloaded and converted to MP3 via FFmpeg by default.
4. Metadata (title, artist, album when available) and the YouTube Music thumbnail are embedded
   into the output file so your library software can identify each track.
5. After the downloads finish, an M3U playlist file (named after the CSV, e.g., `playlist.m3u`)
   is written inside the playlist-named subfolder with relative paths to the downloaded tracks
   so you can load the set on a player that supports playlists.
6. Files are saved with yt-dlp's default title-based naming in the playlist-named subfolder
   under your chosen output base directory.

## Example
```
python exportify_downloader.py example.csv --output playlist_downloads --limit 10
```

This will download the first ten tracks from `example.csv` into
`playlist_downloads/example/`.
