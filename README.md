# vtvgo-dl

[Tiếng Việt](README.vi.md)

Download videos, whole series, whole pages, TV catch-up and live channels from [vtvgo.vn](https://vtvgo.vn) in the **best available quality** (usually 1080p), with **full metadata**.

> For personal, educational use only. Respect VTVgo's terms of service and copyright. Do not redistribute downloaded content.

## Features

- **Best quality**: every clear stream the site offers is probed, including the hidden `master.m3u8` that lists all renditions. The highest resolution and bitrate wins.
- **Series**: a series link downloads every episode (`--episodes 1-5,8` picks some), named `Series - S01E05.mp4`.
- **Pages**: `/page/thoi-su` downloads every video section of that page (`--limit` caps each section, `--recursive` follows sub pages).
- **Full metadata**:
  - MP4 tags (title, show, episode, date, director, cast, description, link)
  - embedded cover art
  - `.info.json` with the raw API data
  - Kodi / Jellyfin `.nfo` and `tvshow.nfo`
  - poster, fanart and thumbnail at original size
  - `.description.txt`
  - `videos.csv` index
- **TV channels**:
  - TV guide (`--epg`)
  - catch-up of finished programs (`--program`)
  - live recording (`--duration`, or stop with Ctrl+C)
  - livestream / restream videos, which are recorded automatically
- **Fast and resumable**: parallel segments through aria2c (falls back to requests). Run the same command again to resume. Failures go to `failed.txt`; `-a failed.txt` retries them.
- **Login**: `--login` opens a browser window where *you* log in. Videos that need an account or a paid plan then work too.

## Install

```bash
pip install -r requirements.txt
```

Put `ffmpeg.exe` (and optionally `aria2c.exe`) next to `vtvgo-dl.py`, or have them on your PATH. If ffmpeg is missing, the `imageio-ffmpeg` package provides one.

## Usage

```bash
python vtvgo-dl.py https://vtvgo.vn/watch/576y
```

```bash
python vtvgo-dl.py https://vtvgo.vn/watch/2E8I --episodes 1-10
```

```bash
python vtvgo-dl.py https://vtvgo.vn/page/thoi-su --limit 5
```

```bash
python vtvgo-dl.py -F https://vtvgo.vn/watch/576y
```

| Input | Meaning |
|---|---|
| `https://vtvgo.vn/watch/<id>` or `<id>` | one video. For a series id, every episode is downloaded. |
| `https://vtvgo.vn/page/<slug>` | every video section on that page |
| `https://vtvgo.vn/catalog/node/<id>` or `node:<id>` | one catalog list |
| `https://vtvgo.vn/catalog/<id>` | one section |
| `https://vtvgo.vn/channel/<id>` | a TV channel: live recording, or catch-up with `--program` |

| Option | |
|---|---|
| `-o DIR` | output folder (default `downloads`) |
| `-q best / worst / 720` | quality (default `best`) |
| `-F` | list qualities only |
| `-e 1-5,8` | episodes to download |
| `--whole-series` | an episode link downloads its whole series |
| `-n N` | at most N videos per list or section |
| `-r` | also follow sub pages of a page |
| `--metadata-only` | metadata and artwork only |
| `--no-images`, `--no-subs` | skip artwork or subtitles |
| `-j 16` | parallel segment downloads |
| `--no-aria2c` | download with requests even when aria2c is available |
| `--keep-parts` | keep the downloaded segments after joining |
| `--proxy URL` | proxy for all requests |
| `-a file.txt` | read links from a file (one per line, `#` starts a comment) |
| `--login` / `--logout` | log in / delete the saved session |
| `--lang vi` | Vietnamese messages |
| `--no-color` | no colors |

## TV channels, catch-up and live

```bash
python vtvgo-dl.py --channels
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --epg --date yesterday
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --program "Thời sự" --date yesterday
```

```bash
python vtvgo-dl.py https://vtvgo.vn/channel/1 --duration 1h30m
```

| Option | |
|---|---|
| `--channels` | list every channel and its id |
| `--epg` | TV guide of the channel for `--date`. `✓` = finished, can be downloaded. |
| `--date` | `YYYY-MM-DD`, `DD/MM` or `yesterday` (default: today, Vietnam time) |
| `-p, --program` | catch-up: program ids from `--epg`, words of the title, or `all`, comma separated |
| `-d, --duration` | live: stop after `30m`, `1h30m`, `1:00:00`… (default: until Ctrl+C) |
| `--live-from-start` | live: start from the oldest part the stream still keeps |

Without `--program`, a channel link records the live stream. Ctrl+C stops and saves the recording.

Live recordings and catch-up are saved in `downloads/Channels/<channel>/` with `.info.json` (channel, programs aired during the recording) and `.nfo`.

Catch-up is available for 24 hours as a guest, and up to 8 days on many channels with an account. Channels from paid packages (VTVcab, SCTV…) need that package.

## Login and paid content

```bash
python vtvgo-dl.py --login
```

A browser window opens on vtvgo.vn. Log in there yourself. The window closes once you are logged in. The session (token and device id) is saved to `vtvgo-session.json` and stays valid for about a day, after which you run `--login` again.

Keep that file private: it works like a password. `--logout` deletes it. The script never asks for or stores your password.

## Limits

- Some videos are licensed for Vietnam only (`OUTSIDE_VIETNAM`). Outside Vietnam, those need `--proxy` with a Vietnamese proxy, even when logged in.
- Videos and channels protected by DRM (Widevine / FairPlay) are reported and skipped.
- A live recording needs a steady connection. Missing parts are reported as "gaps".

## License

MIT © loooveee
