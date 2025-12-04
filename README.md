# SpotDL Web Helper

A small FastAPI + vanilla JS interface for running [spotDL](https://github.com/spotDL/spotify-downloader) downloads from your browser. It organizes tracks into a shared library directory, writes playlist-specific M3U manifests, and shows per-track progress through server-sent events.

## Features
- Submit Spotify track, album, playlist, or user links from a dark-themed web UI.
- Per-track status and live logs streamed to the browser.
- Shared library output to avoid duplicate downloads across playlists.
- Playlist folders with `playlist.m3u` and hardlinks/symlinks to library files.
- Configurable output paths, filename template, overwrite behavior, and thread count via `config.json`.

## Setup
1. Install dependencies (Python 3.9+). The app pins `spotdl==4.3.1`, the latest version currently available on PyPI:
   ```bash
   pip install -r requirements.txt
   ```
2. Update `config.json` with your desired output directory and Spotify client credentials. Public playlists work best with credentials, and **user-library URLs require them**; without credentials those jobs will fail early with a clear error. Ensure `ffmpeg` is on your PATH.
3. Run the server:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
4. Open `http://localhost:8000` in your browser and start a download.

## Notes
- Downloads are written to `<output_dir>/<library_dir_name>`; playlist folders and M3U manifests live under `<output_dir>/<playlists_dir_name>`.
- Jobs and logs are held in memory while the server runs.
- If `ffmpeg` or Spotify credentials are missing, the backend will report errors in the job logs/status.
