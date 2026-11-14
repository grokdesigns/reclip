# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend (~150 lines)

## Quick Start

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 -e RECLIP_PASSWORD=changeme reclip
```

## Configuration

All settings are environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8899` | Port for `./reclip.sh` / `python3 app.py` |
| `HOST` | `127.0.0.1` | Bind address for `./reclip.sh` (the Docker image binds `0.0.0.0`) |
| `RECLIP_PASSWORD` | _(none)_ | Require this password (HTTP Basic auth, any username). **Set it whenever ReClip is reachable from other devices.** |
| `RECLIP_ALLOWED_HOSTS` | _(none)_ | Comma-separated hostnames you open the UI with. IP addresses, `localhost` and `*.local` always work; other names are rejected to block DNS-rebinding attacks. |
| `RECLIP_ALLOW_PRIVATE_URLS` | _(off)_ | Set to `1` to allow downloading from private/LAN addresses (blocked by default to prevent SSRF). |
| `RECLIP_MAX_CONCURRENT` | `3` | Downloads running at once (others queue) |
| `RECLIP_MAX_PENDING` | `20` | Queued + running downloads before new ones are refused |
| `RECLIP_MAX_PLAYLIST` | `100` | Maximum videos expanded from one playlist |
| `RECLIP_FILE_TTL` | `3600` | Seconds to keep finished files on the server (`0` = forever) |
| `RECLIP_NO_UPDATE` | _(off)_ | Set to `1` to skip updating yt-dlp on startup |

MP4 downloads are made compatible with iOS Photos/Messages (H.264 or HEVC + AAC). Resolutions only available as VP9/AV1 (typically YouTube 1440p and above) are re-encoded, which can take a while.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
