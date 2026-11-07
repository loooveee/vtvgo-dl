#!/usr/bin/env python3
"""vtvgo-dl - download videos, series, whole pages, TV catch-up and live channels from vtvgo.vn
in the best quality, with full metadata.

How it works
------------
1. Resolve input     expand()          /watch/<id>, /page/<slug>, /catalog/node/<id>, /catalog/<id> or a bare id
                                       -> list of VOD ids (series are expanded to every episode).
2. Read metadata     Api.detail()      content/v21.0/get-detail-vod (title, description, cast, images, ...).
3. Find the stream   Api.source()      live-channel/v21.0/playback/source {id, isVOD: true}.
4. Pick quality      pick_variant()    every clear (non-DRM) HLS source is probed; a media playlist named
                                       index.m3u8 also gets its sibling master.m3u8 checked, which lists
                                       every rendition. The highest resolution / bitrate wins.
5. Download          fetch_to_mp4()    segments in parallel (aria2c, or requests as fallback), resumable.
6. Mux               mux()             ffmpeg joins the segments into MP4 with tags, cover and subtitles,
                                       next to .info.json, .nfo (Kodi/Jellyfin) and the artwork.

TV channels (/channel/<id>) use the same playback/source call with {channelId, platform}:
   --epg             Api.epg()         display/v21.0/epg (programs with programID, start and end time).
   --program         run_catchup()     playback/source {channelId, programId, startTime, endTime}
                                       -> a finished playlist, downloaded like a video.
   live              record_live()     follows the live playlist, appends new segments to a .ts file,
                                       renews expired links, and remuxes to MP4 at the end. Livestream
                                       videos (/watch/<id> whose playlist has no end) are recorded too.
Logged-in channel requests need the device id that --login saves next to the token.

Messages are bilingual: t("English", "Tiếng Việt"). Please add both when you add a message.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

import requests

__version__ = "1.0.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SITE = "https://vtvgo.vn"
API = "https://api.vtvdigital.org"
CONTENT = f"{API}/content/v21.0"
DISPLAY = f"{API}/display/v21.0"
PLAYBACK = f"{API}/live-channel/v21.0/playback/source"
LIVE_SESSION = f"{API}/live-channel/v21.0/playback/live-session"
PLATFORM = "webPC"
SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_FILE = SCRIPT_DIR / "vtvgo-session.json"
VN_TZ = timezone(timedelta(hours=7))
CLEAR_DRM = {"", "none", "clear", "unencrypted", "nonedrm"}

LANG = "en"


def t(en: str, vi: str) -> str:
    """Pick the message for the current language."""
    return vi if LANG == "vi" else en


def detect_lang(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1].lower()
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1].lower()
    env = os.environ.get("VTVGO_DL_LANG", "").lower()
    if env in ("en", "vi"):
        return env
    try:
        import locale
        name = (locale.getlocale()[0] or "").lower()
        if name.startswith("vi") or "vietnam" in name:
            return "vi"
    except Exception:
        pass
    return "en"


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #

_print_lock = threading.RLock()
_status_line = ""
_COLOR = False
_TTY = sys.stdout.isatty()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _style(code: str):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m" if _COLOR else str(s)


bold = _style("1")
gray = _style("90")
red = _style("31")
green = _style("32")
yellow = _style("33")
cyan = _style("36")
bold_cyan = _style("1;36")
bold_green = _style("1;32")
bold_red = _style("1;31")


def init_colors(enabled: bool) -> None:
    global _COLOR
    if not enabled or os.environ.get("NO_COLOR") or not (_TTY or os.environ.get("FORCE_COLOR")):
        _COLOR = False
        return
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            for handle_id in (-11, -12):
                handle = kernel32.GetStdHandle(handle_id)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            _COLOR = False
            return
    _COLOR = True


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _width() -> int:
    return max(40, shutil.get_terminal_size((100, 20)).columns - 1)


def _clear_status() -> None:
    if _status_line and _TTY:
        sys.stdout.write("\r" + " " * _width() + "\r")


def _draw_status() -> None:
    if not (_status_line and _TTY):
        return
    w = _width()
    line = _status_line
    if visible_len(line) > w:
        line = _ANSI_RE.sub("", line)[:w]
    sys.stdout.write("\r" + line + " " * max(0, w - visible_len(line)))
    sys.stdout.flush()


def log(msg: str = "", err: bool = False) -> None:
    with _print_lock:
        _clear_status()
        sys.stdout.flush()
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)
        _draw_status()


def status(msg: str) -> None:
    global _status_line
    with _print_lock:
        _clear_status()
        _status_line = msg
        _draw_status()


def warn(msg: str) -> str:
    return f"  {yellow('!')} {yellow(msg)}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class SegmentBar:
    """Progress bar for one video: finished segments, downloaded bytes, speed and ETA."""

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, files: list[Path]):
        self.label = label
        self.files = files
        self.samples: list[tuple[float, int]] = []
        self.started = time.monotonic()
        self.frame = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        status("")

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            self.render()

    def _scan(self) -> tuple[int, int]:
        done = size = 0
        for f in self.files:
            try:
                size += f.stat().st_size
                if not Path(f"{f}.aria2").exists():
                    done += 1
            except OSError:
                part = f.with_name(f.name + ".part")
                try:
                    size += part.stat().st_size
                except OSError:
                    pass
        return done, size

    @staticmethod
    def _bar(frac: float, width: int) -> str:
        if _COLOR:
            halves = int(frac * width * 2)
            full, half = divmod(halves, 2)
            done = "━" * full + ("╸" if half else "")
            rest = "━" * (width - full - (1 if half else 0))
            return (green if frac >= 1 else cyan)(done) + gray(rest)
        full = int(frac * width)
        return "█" * full + "░" * (width - full)

    def render(self) -> None:
        now = time.monotonic()
        done, size = self._scan()
        total = max(1, len(self.files))
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, size)]
        t0, b0 = self.samples[0]
        speed = (size - b0) / (now - t0) if now - t0 >= 0.5 else 0.0
        frac = min(1.0, done / total)
        elapsed = now - self.started
        eta = elapsed / frac * (1 - frac) if frac > 0.01 else None
        self.frame = (self.frame + 1) % len(self.SPINNER)
        sep = gray(" │ ")
        tail = sep.join([
            bold(f"{frac * 100:5.1f}%"),
            f"{done}/{total} {t('seg', 'đoạn')}",
            f"{fmt_bytes(size):>8}",
            cyan(f"{fmt_bytes(speed) + '/s':>10}"),
            f"ETA {yellow(fmt_eta(eta))}",
        ])
        prefix = f"  {cyan(self.SPINNER[self.frame]) if _COLOR else '-'} "
        bar_w = max(10, min(40, _width() - visible_len(prefix) - visible_len(tail) - 3))
        status(f"{prefix}{self._bar(frac, bar_w)}  {tail}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class VtvgoError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


STOP = threading.Event()


def find_tool(name: str) -> Optional[str]:
    """Prefer an executable next to vtvgo-dl.py (aria2c.exe, ffmpeg.exe), then PATH."""
    for cand in (SCRIPT_DIR / f"{name}.exe", SCRIPT_DIR / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def find_ffmpeg() -> Optional[str]:
    path = find_tool("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def sanitize(text: str, limit: int = 90) -> str:
    """Safe file / folder name. Kept short so deep paths stay under the Windows 260-character limit."""
    text = _INVISIBLE.sub("", _BAD_CHARS.sub(" ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if len(text) > limit:
        text = text[:limit].rstrip(". ")
    if text.split(".")[0].upper() in _RESERVED:
        text = f"_{text}"
    return text or "untitled"


_TOKEN_PATH = re.compile(r"/[A-Za-z0-9_-]{16,}/\d{10}/")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){3,7}\b")


def redact(text: str) -> str:
    """Remove what could identify the user from text that is shown or saved: signed-link tokens
    (path tokens and query strings) and IP addresses."""
    text = re.sub(r"(https?://[^\s?#]+)\?[^\s#]*", r"\1", str(text))
    text = _TOKEN_PATH.sub("/.../", text)
    return _IP.sub("x.x.x.x", text)


def redact_urls(obj):
    """redact() every string inside a JSON-like structure."""
    if isinstance(obj, dict):
        return {k: redact_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_urls(v) for v in obj]
    return redact(obj) if isinstance(obj, str) and ("http" in obj or "IP" in obj) else obj


def parse_time(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(VN_TZ)
    except ValueError:
        return None


def original_image(url: Optional[str]) -> Optional[str]:
    """thumb.vtvdigital.org/transform?url=<original> -> <original> (full size, no re-encode)."""
    if not url:
        return None
    try:
        q = parse_qs(urlparse(url).query)
        if "transform" in url and q.get("url"):
            return q["url"][0]
    except ValueError:
        pass
    return url


def parse_ranges(text: Optional[str]) -> Optional[set[int]]:
    """"1-5,8,10-" -> set of episode numbers (open end means up to 9999)."""
    if not text:
        return None
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            lo = int(m.group(1) or 1)
            hi = int(m.group(2) or 9999)
            out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(part)
    return out


def jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.5 * (i + 1))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Login session
# --------------------------------------------------------------------------- #

def load_session(path: Path) -> tuple[Optional[str], Optional[str]]:
    """(access token, device id) saved by --login. Expired tokens are reported and ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    token = data.get("accessToken")
    if not token:
        return None, None
    exp = jwt_payload(token).get("exp")
    if exp and exp < time.time() + 60:
        log(warn(t("The saved login has expired. Run  python vtvgo-dl.py --login  again.",
                   "Phiên đăng nhập đã hết hạn. Chạy lại  python vtvgo-dl.py --login")))
        return None, None
    return token, data.get("deviceId")


def login(path: Path) -> int:
    """Open a real browser window; the user logs in by hand. Only the access token is saved."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log(red(t("--login needs Playwright:  pip install playwright",
                  "--login cần Playwright:  pip install playwright")))
        return 2
    log(bold_cyan(t("A browser window opens on vtvgo.vn. Log in there yourself (phone number / password / QR).",
                    "Cửa sổ trình duyệt sẽ mở vtvgo.vn. Bạn tự đăng nhập ở đó (số điện thoại / mật khẩu / QR).")))
    log(gray(t("The window closes by itself once you are logged in (waits up to 10 minutes).",
               "Cửa sổ tự đóng khi đăng nhập xong (chờ tối đa 10 phút).")))
    with sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = pw.chromium.launch(headless=False, channel=channel) if channel else \
                    pw.chromium.launch(headless=False)
                break
            except Exception:
                continue
        if browser is None:
            log(red(t("No browser found. Install Edge/Chrome or run:  python -m playwright install chromium",
                      "Không tìm thấy trình duyệt. Cài Edge/Chrome hoặc chạy:  python -m playwright install chromium")))
            return 2
        ctx = browser.new_context(locale="vi-VN")
        page = ctx.new_page()
        page.goto(SITE, wait_until="domcontentloaded")
        deadline = time.time() + 600
        token = device_id = None
        while time.time() < deadline:
            try:
                if page.is_closed():
                    break
                cookies = {c["name"]: c["value"] for c in ctx.cookies(SITE)}
            except Exception:   # the whole browser was closed
                break
            store = {}
            try:
                store = json.loads(unquote(cookies.get("vtvgo.auth2.dataStore", "")) or "{}")
            except ValueError:
                pass
            user = store.get("userInfo") or {}
            raw = unquote(cookies.get("vtvgo.auth2.token", ""))
            if raw and user and not user.get("isTempUser"):
                token = raw
                try:   # the API wants this id next to the token for live channels and catch-up
                    device_id = page.evaluate("() => localStorage.getItem('vtvgo.deviceId.v2')")
                except Exception:
                    device_id = None
                break
            time.sleep(1)
        try:
            browser.close()
        except Exception:
            pass
    if not token:
        log(red(t("Not logged in (window closed or timed out).", "Chưa đăng nhập (đã đóng cửa sổ hoặc hết giờ).")))
        return 1
    exp = jwt_payload(token).get("exp")
    data = {"accessToken": token, "deviceId": device_id,
            "savedAt": datetime.now(VN_TZ).isoformat(timespec="seconds")}
    if exp:
        data["expiresAt"] = datetime.fromtimestamp(exp, VN_TZ).isoformat(timespec="seconds")
    write_atomic(path, json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)   # owner only (no effect on Windows)
    except OSError:
        pass
    log(bold_green(t(f"Logged in. Session saved to {path.name}", f"Đã đăng nhập. Lưu phiên vào {path.name}")))
    if exp:
        log(gray(t(f"Valid until {data['expiresAt']}", f"Hiệu lực đến {data['expiresAt']}")))
    log(gray(t("Keep this file private: it works like a password. Delete it to log out.",
               "Giữ file này riêng tư: nó có tác dụng như mật khẩu. Xoá file để đăng xuất.")))
    return 0


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

ERROR_TEXT = {
    "OUTSIDE_VIETNAM": ("only available in Vietnam (use --proxy with a Vietnamese proxy)",
                        "chỉ xem được tại Việt Nam (dùng --proxy với proxy Việt Nam)"),
    "LOGIN_REQUIRED": ("needs a login (run --login)", "cần đăng nhập (chạy --login)"),
    "PLAN_REQUIRED": ("needs a paid VTVgo plan", "cần mua gói VTVgo"),
    "VOD_NOT_FOUND": ("not found", "không tìm thấy"),
    "NOT_FOUND": ("not found", "không tìm thấy"),
}


def explain(code: str, message: str = "") -> str:
    if code in ERROR_TEXT:
        en, vi = ERROR_TEXT[code]
        return f"{t(en, vi)} [{code}]"
    return redact(f"{message or code}" + (f" [{code}]" if code and message else ""))


class Api:
    def __init__(self, token: Optional[str], proxy: Optional[str], device_id: Optional[str] = None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": f"{SITE}/", "Origin": SITE,
                               "Accept": "application/json", "Accept-Language": "vi"})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.token = token
        self.device_id = device_id
        self._channels: Optional[dict] = None
        self._details: dict[str, dict] = {}
        self._warned_device = False

    def _call(self, method: str, url: str, auth: bool, **kw) -> tuple[int, dict]:
        last = None
        for attempt in range(5):
            if STOP.is_set():
                raise VtvgoError(t("stopped", "đã dừng"))
            headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
            try:
                r = self.s.request(method, url, headers=headers, timeout=30, **kw)
            except requests.RequestException as e:
                last = redact(str(e))
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and headers:
                # The saved login expired or was revoked during the run: continue as a guest.
                log(warn(t("The login was rejected (expired?). Continuing as a guest; run --login again.",
                           "Phiên đăng nhập bị từ chối (hết hạn?). Tiếp tục như khách; hãy chạy lại --login.")))
                self.token = None
                if isinstance(kw.get("json"), dict):
                    kw["json"] = {k: v for k, v in kw["json"].items() if k != "deviceId"}
                continue
            if r.status_code in (429, 500, 502, 503, 504) or r.status_code == 202:
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(30, wait or 2 * (attempt + 1)))
                continue
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"error": {"code": f"HTTP_{r.status_code}", "message": r.text[:200]}}
        raise VtvgoError(t(f"network error: {last}", f"lỗi mạng: {last}"))

    def get(self, path: str, auth: bool = False, **params) -> dict:
        code, data = self._call("GET", CONTENT + path, auth, params=params)
        err = data.get("error") if isinstance(data, dict) else None
        if code >= 400 or err:
            err = err or {}
            raise VtvgoError(explain(err.get("code", f"HTTP_{code}"), err.get("message", "")), err.get("code", ""))
        return data

    def detail(self, vod_id: str) -> dict:
        if vod_id not in self._details:
            self._details[vod_id] = self.get("/get-detail-vod", auth=True, id=vod_id)["data"]
        return self._details[vod_id]

    def episodes(self, series_id: str, total_hint: int = 0) -> list[dict]:
        """Every episode of a GROUP_VIDEO, fetched in ranges of 50."""
        out: dict[str, dict] = {}
        start, step, empty = 1, 50, 0
        while start < 10000:
            try:
                items = self.get("/vod/episode", auth=True, id=series_id,
                                 startEpisodeNumber=start, endEpisodeNumber=start + step - 1)["data"]
            except VtvgoError as e:
                if e.code in ("NOT_FOUND", "VOD_NOT_FOUND"):
                    items = []
                else:
                    raise
            items = items.get("items", []) if isinstance(items, dict) else (items or [])
            for it in items:
                if it.get("id"):
                    out[it["id"]] = it
            empty = 0 if items else empty + 1
            start += step
            if empty and start > max(total_hint, 1):
                break
        return sorted(out.values(), key=lambda e: (e.get("episodeNumber") or 0, e.get("id")))

    def node(self, node_id: str, limit: Optional[int]) -> list[dict]:
        """VODs of a catalog node (newest first), paged 20 at a time."""
        out, page = [], 1
        while True:
            data = self.get("/vod/list-vod", nodeId=node_id, page=page, limit=20)
            items = data.get("data") or []
            out += [i for i in items if i.get("id")]
            if limit and len(out) >= limit:
                return out[:limit]
            if not items or not (data.get("meta") or {}).get("next"):
                return out
            page += 1

    def sections(self, page_id: str) -> tuple[str, list[dict]]:
        """Title and blocks of a /page/<slug-or-id>."""
        blocks, page, title = [], 1, None
        while page < 30:
            data = self.get("/section", id=page_id, limit=20, page=page)
            meta = data.get("meta") or {}
            title = title or (meta.get("parentPage") or {}).get("title")
            blocks += data.get("data") or []
            if not meta.get("next"):
                break
            page += 1
        return title or page_id, blocks

    def section_block(self, section_id: str) -> Optional[dict]:
        try:
            return self.get("/content-block", type="SECTION", id=section_id).get("data")
        except VtvgoError:
            return None

    def _playback(self, url: str, body: dict, channel: bool = False) -> dict:
        auth = bool(self.token)
        if auth and self.device_id:
            body = {**body, "deviceId": self.device_id}
        elif auth and channel:
            # Logged-in channel / catch-up requests must carry the device id saved by --login.
            # Sessions saved before it was stored have none: fall back to guest access.
            auth = False
            if not self._warned_device:
                self._warned_device = True
                log(warn(t("The saved login has no device id: channels play as a guest (catch-up limited to 24h, "
                           "paid channels unavailable). Run --login again.",
                           "Phiên đã lưu thiếu device id: kênh sẽ phát như khách (xem lại tối đa 24h, "
                           "không xem được kênh trả phí). Chạy lại --login.")))
        code, data = self._call("POST", url, auth, json=body)
        err = data.get("error") if isinstance(data, dict) else None
        if code >= 400 or err or not data.get("data"):
            err = err or {}
            msg = explain(err.get("code", f"HTTP_{code}"), err.get("message", ""))
            need = (err.get("data") or {}).get("requiredContentIds") or err.get("requiredContentIds")
            if need:
                msg += f" ({t('package', 'gói')}: {', '.join(map(str, need))})"
            raise VtvgoError(msg, err.get("code", ""))
        return data["data"]

    def source(self, vod_id: str) -> dict:
        return self._playback(PLAYBACK, {"id": vod_id, "isVOD": True})

    def channel_source(self, channel_id: str) -> dict:
        return self._playback(PLAYBACK, {"channelId": channel_id, "platform": PLATFORM}, channel=True)

    def catchup_source(self, channel_id: str, prog: dict) -> dict:
        return self._playback(PLAYBACK, {"channelId": channel_id, "programId": prog["programID"],
                                         "startTime": prog["startTime"], "endTime": prog["endTime"],
                                         "platform": PLATFORM}, channel=True)

    def live_session(self, channel_id: str, session_id: str) -> dict:
        return self._playback(LIVE_SESSION, {"sessionId": session_id, "channelId": channel_id,
                                             "platform": PLATFORM}, channel=True)

    def _display(self, path: str, **params) -> dict:
        code, data = self._call("GET", DISPLAY + path, False, params=params)
        err = data.get("error") if isinstance(data, dict) else None
        if code >= 400 or err:
            err = err or {}
            raise VtvgoError(explain(err.get("code", f"HTTP_{code}"), err.get("message", "")), err.get("code", ""))
        return data

    def channels(self) -> dict:
        """id -> channel info (name, logo, catch-up hours, ...), with its group name."""
        if self._channels is None:
            out: dict = {}
            for grp in self._display("/catalogs/with-channels", platform=PLATFORM).get("data") or []:
                for ch in grp.get("channels") or []:
                    if ch.get("id") and ch["id"] not in out:
                        out[ch["id"]] = {**ch, "group": grp.get("name")}
            self._channels = out
        return self._channels

    def epg(self, channel_id: str, day: str) -> list[dict]:
        data = self._display("/epg", channelIDs=channel_id, **{"from": day, "to": day}).get("data") or {}
        progs = [p for d in data.get(channel_id) or [] for p in d.get("programs") or []]
        seen, out = set(), []
        for p in sorted(progs, key=lambda p: p.get("startTime") or ""):
            key = (p.get("programID"), p.get("startTime"))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out


# --------------------------------------------------------------------------- #
# Input -> list of jobs
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    vod_id: str
    folder: tuple = ()          # sub folders for single videos (page title, section title)


def parse_input(text: str) -> tuple[str, str]:
    """Return (kind, value): kind is watch | page | node | section."""
    text = text.strip()
    m = re.match(r"^(watch|page|node|section|catalog|channel)[:=](.+)$", text, re.I)
    if m:
        kind = m.group(1).lower()
        return ("section" if kind == "catalog" else kind), m.group(2).strip()
    if re.match(r"^[A-Za-z0-9]{1,12}$", text) and not text.isdigit() or text.isdigit() and len(text) < 6:
        return "watch", text
    if text.isdigit():
        return "node", text
    u = urlparse(text if "://" in text else f"https://{text}")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "watch":
        return "watch", parts[1]
    if len(parts) >= 2 and parts[0] == "channel":
        sid = (parse_qs(u.query).get("sessionId") or [""])[0]
        return "channel", unquote(parts[1]) + (f"?{sid}" if sid else "")
    if len(parts) >= 2 and parts[0] == "page":
        return "page", unquote(parts[1])
    if len(parts) >= 3 and parts[0] == "catalog" and parts[1] == "node":
        return "node", unquote(parts[2])
    if len(parts) >= 2 and parts[0] == "catalog":
        return "section", parts[1]
    raise ValueError(text)


def expand_block(api: Api, block: dict, limit: Optional[int]) -> list[str]:
    extra = block.get("extraData") or {}
    ctype = extra.get("contentType") or block.get("type")
    if ctype == "vod" and extra.get("nodeId"):
        return [i["id"] for i in api.node(str(extra["nodeId"]), limit)]
    if ctype == "manual":
        ids = (extra.get("filter") or {}).get("manualItems") or []
        return ids[:limit] if limit else ids
    return []


def expand(api: Api, text: str, opts) -> list[Job]:
    kind, value = parse_input(text)
    if kind == "watch":
        return [Job(value)]
    if kind == "node":
        log(gray(t(f"  Listing catalog node {value} ...", f"  Đang liệt kê mục {value} ...")))
        return [Job(i["id"], (f"node {value}",)) for i in api.node(value, opts.limit)]
    if kind == "section":
        block = api.section_block(value)
        if not block:
            raise VtvgoError(t("section not found", "không tìm thấy mục"))
        title = sanitize(block.get("title") or value)
        return [Job(i, (title,)) for i in expand_block(api, block, opts.limit)]
    # page
    title, blocks = api.sections(value)
    log(f"  {t('Page', 'Trang')}: {bold(title)} — {len(blocks)} {t('blocks', 'khối')}")
    jobs: list[Job] = []
    pages_seen = {value}
    queue = [(sanitize(title), blocks, 0)]
    while queue:
        ptitle, blks, depth = queue.pop(0)
        for b in blks:
            btype = b.get("type")
            if btype == "nested-page":
                if not opts.recursive:
                    continue
                for it in b.get("items") or []:
                    pid = (it.get("target") or {}).get("sourceModeID") or (it.get("target") or {}).get("slug")
                    if pid and pid not in pages_seen and depth < 2:
                        pages_seen.add(pid)
                        try:
                            sub_title, sub_blocks = api.sections(pid)
                        except VtvgoError:
                            continue
                        queue.append((f"{ptitle}/{sanitize(sub_title)}", sub_blocks, depth + 1))
                continue
            ids = expand_block(api, b, opts.limit)
            if ids:
                stitle = sanitize(b.get("title") or b.get("id"))
                log(gray(f"    • {stitle}: {len(ids)}"))
                jobs += [Job(i, tuple(ptitle.split("/")) + (stitle,)) for i in ids]
    if not opts.recursive and any(b.get("type") == "nested-page" for b in blocks):
        log(gray(t("    (sub pages skipped, add --recursive to include them)",
                   "    (bỏ qua trang con, thêm --recursive để tải cả trang con)")))
    return jobs


# --------------------------------------------------------------------------- #
# HLS
# --------------------------------------------------------------------------- #

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def attrs(line: str) -> dict:
    return {k: v.strip('"') for k, v in ATTR_RE.findall(line.split(":", 1)[1] if ":" in line else "")}


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    fps: str = ""
    codecs: str = ""
    audio: Optional[str] = None       # separate audio playlist URL
    source: str = ""                  # CDN label

    @property
    def label(self) -> str:
        res = f"{self.width}x{self.height}" if self.height else t("unknown", "không rõ")
        bw = f"{self.bandwidth / 1e6:.2f} Mbps" if self.bandwidth else "?"
        return f"{res} {bw} {self.fps + 'fps ' if self.fps else ''}{self.codecs}".strip()


class Http:
    """Plain HTTP for playlists / segments / images (same proxy as the API)."""

    def __init__(self, proxy: Optional[str]):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": f"{SITE}/", "Origin": SITE})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.proxy = proxy

    def text(self, url: str) -> tuple[str, str]:
        last = None
        for attempt in range(4):
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 200:
                    return r.text, r.url
                last = f"HTTP {r.status_code}"
                if r.status_code in (400, 401, 403, 404, 410):
                    break
            except requests.RequestException as e:
                last = redact(str(e))
            time.sleep(1 + attempt)
        raise VtvgoError(redact(f"{url}: {last}"))


def parse_master(text: str, base: str, source: str) -> list[Variant]:
    lines = [ln.strip() for ln in text.splitlines()]
    audio_groups: dict[str, str] = {}
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA:"):
            a = attrs(ln)
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                if a.get("GROUP-ID") not in audio_groups or a.get("DEFAULT") == "YES":
                    audio_groups[a.get("GROUP-ID", "")] = urljoin(base, a["URI"])
    out = []
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        a = attrs(ln)
        uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
        if not uri:
            continue
        w, h = (a.get("RESOLUTION", "0x0").lower().split("x") + ["0"])[:2]
        out.append(Variant(urljoin(base, uri), int(a.get("BANDWIDTH") or a.get("AVERAGE-BANDWIDTH") or 0),
                           int(w or 0), int(h or 0), a.get("FRAME-RATE", "").split(".")[0], a.get("CODECS", ""),
                           audio_groups.get(a.get("AUDIO", "")), source))
    return out


def stream_urls(src: dict) -> list[tuple[str, str, str]]:
    """(url, manifestType, cdn) of every clear source, in the order the API gives them."""
    out, drm_only = [], []
    modes = list(src.get("sourceModes") or [])
    for mode in modes:
        groups = mode.get("multiSource") or [mode]
        for grp in groups:
            drm = str(((grp.get("drmInfo") or mode.get("drmInfo") or {}).get("drmType")) or "none").lower()
            mtype = grp.get("manifestType") or mode.get("manifestType") or ""
            for s in grp.get("sources") or []:
                if not s.get("url"):
                    continue
                (out if drm in CLEAR_DRM else drm_only).append((s["url"], mtype, s.get("cdnID") or ""))
    if src.get("sourceUrl"):
        out.append((src["sourceUrl"], "", ""))
    seen, uniq = set(), []
    for item in out:
        if item[0] not in seen:
            seen.add(item[0])
            uniq.append(item)
    if not uniq and drm_only:
        raise VtvgoError(t("protected by DRM (Widevine/FairPlay), cannot be downloaded",
                           "có DRM (Widevine/FairPlay), không tải được"), "DRM")
    return uniq


def list_variants(http: Http, src: dict) -> list[Variant]:
    variants: list[Variant] = []
    for url, mtype, cdn in stream_urls(src):
        if ".mpd" in url.split("?")[0].lower() or mtype.lower() == "dash" or url.split("?")[0].endswith(".mp4"):
            variants.append(Variant(url, source=cdn or "direct"))
            continue
        try:
            text, final = http.text(url)
        except VtvgoError:
            continue
        found = parse_master(text, final, cdn)
        if not found:
            # A media playlist. The Akamai/Unified packager keeps the full rendition list in
            # master.m3u8 next to it, so look there before settling for this one stream.
            path = urlparse(final).path
            if path.endswith("/index.m3u8"):
                sib = final.split("?")[0].rsplit("/", 1)[0] + "/master.m3u8"
                query = urlparse(final).query
                try:
                    mtext, mfinal = http.text(sib + (f"?{query}" if query else ""))
                    found = parse_master(mtext, mfinal, cdn)
                except VtvgoError:
                    found = []
            if not found:
                found = [Variant(final, source=cdn)]
        variants += found
    return variants


def pick_variant(variants: list[Variant], quality: str) -> Variant:
    if not variants:
        raise VtvgoError(t("no playable stream", "không có luồng phát"))
    ranked = sorted(variants, key=lambda v: (v.height, v.bandwidth), reverse=True)
    if quality == "worst":
        return ranked[-1]
    if quality.isdigit():
        cap = int(quality)
        fit = [v for v in ranked if v.height and v.height <= cap]
        return fit[0] if fit else ranked[-1]
    return ranked[0]


@dataclass
class MediaPlaylist:
    local_text: str
    tasks: list = field(default_factory=list)     # (url, dest Path)
    byterange: bool = False
    live: bool = False


def plan_media(http: Http, url: str, folder: Path, prefix: str) -> MediaPlaylist:
    """Read a media playlist and plan the download of every segment, key and init section.

    The rewritten playlist points at the local files, so ffmpeg can join (and AES-128 decrypt) them."""
    text, final = http.text(url)
    if "#EXT-X-STREAM-INF" in text:            # a master after all: take its best stream
        best = pick_variant(parse_master(text, final, ""), "best")
        return plan_media(http, best.url, folder, prefix)
    out_lines, tasks = [], []
    seg = key = 0
    byterange = "#EXT-X-BYTERANGE" in text
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-KEY") or ln.startswith("#EXT-X-MAP"):
            a = attrs(ln)
            if a.get("URI") and a.get("METHOD", "").upper() != "NONE":
                key += 1
                ext = ".key" if ln.startswith("#EXT-X-KEY") else ".init"
                dest = folder / f"{prefix}{key:03d}{ext}"
                tasks.append((urljoin(final, a["URI"]), dest))
                ln = ln.replace(f'URI="{a["URI"]}"', f'URI="{dest.name}"')
            out_lines.append(ln)
        elif ln.startswith("#"):
            out_lines.append(ln)
        else:
            seg += 1
            name = urlparse(ln).path.rsplit("/", 1)[-1]
            ext = Path(name).suffix if Path(name).suffix in (".ts", ".m4s", ".mp4", ".aac", ".m4a") else ".ts"
            dest = folder / f"{prefix}{seg:05d}{ext}"
            tasks.append((urljoin(final, ln), dest))
            out_lines.append(dest.name)
    if not seg:
        raise VtvgoError(t("empty playlist", "danh sách phát rỗng"))
    live = "#EXT-X-ENDLIST" not in text and "#EXT-X-PLAYLIST-TYPE:VOD" not in text
    return MediaPlaylist("\n".join(out_lines) + "\n", tasks, byterange, live)


def valid_segment(dest: Path) -> bool:
    try:
        if not dest.is_file() or dest.stat().st_size == 0 or Path(f"{dest}.aria2").exists():
            return False
        with open(dest, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return not head.lstrip()[:1] in (b"<", b"{")


def download_aria2(aria2: str, tasks, jobs: int, proxy: Optional[str]) -> None:
    lines = []
    for url, dest in tasks:
        if valid_segment(dest):
            continue
        dest.unlink(missing_ok=True)
        Path(f"{dest}.aria2").unlink(missing_ok=True)
        lines += [url, f"  dir={dest.parent}", f"  out={dest.name}"]
    if not lines:
        return
    cmd = [aria2, "--input-file=-", f"--max-concurrent-downloads={jobs}", "--max-connection-per-server=1",
           "--split=1", "--continue=true", "--allow-overwrite=false", "--auto-file-renaming=false",
           "--file-allocation=none", "--max-tries=6", "--retry-wait=2", "--timeout=30",
           "--connect-timeout=15", "--quiet=true", f"--user-agent={UA}", f"--referer={SITE}/",
           f"--header=Origin: {SITE}"]
    if proxy:
        cmd.append(f"--all-proxy={proxy}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        while True:
            try:
                proc.communicate(data, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                data = None
    except KeyboardInterrupt:
        STOP.set()
        proc.terminate()
        raise


def download_requests(http: Http, tasks, jobs: int) -> None:
    def one(task):
        url, dest = task
        if STOP.is_set() or valid_segment(dest):
            return
        part = dest.with_name(dest.name + ".part")
        for attempt in range(6):
            if STOP.is_set():
                return
            try:
                with http.s.get(url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
                os.replace(part, dest)
                if valid_segment(dest):
                    return
                dest.unlink(missing_ok=True)
            except (requests.RequestException, OSError):
                pass
            STOP.wait(1 + attempt)
        part.unlink(missing_ok=True)

    pool = ThreadPoolExecutor(jobs)
    try:
        futures = [pool.submit(one, task) for task in tasks]
        for fut in futures:
            while not fut.done():
                time.sleep(0.1)
    except KeyboardInterrupt:
        STOP.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=STOP.is_set())


def download_segments(http: Http, tasks, opts, aria2: Optional[str]) -> None:
    for attempt in range(3):
        missing = [tk for tk in tasks if not valid_segment(tk[1])]
        if not missing or STOP.is_set():
            break
        if aria2 and attempt < 2:
            download_aria2(aria2, missing, opts.jobs, opts.proxy)
        else:
            download_requests(http, missing, opts.jobs)
    missing = [tk for tk in tasks if not valid_segment(tk[1])]
    if missing:
        raise VtvgoError(t(f"{len(missing)} segment(s) failed to download",
                           f"{len(missing)} đoạn tải không được"))


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

def series_info(detail: dict) -> Optional[dict]:
    if detail.get("type") == "GROUP_VIDEO":
        return detail
    pm = detail.get("parentMetadata")
    if isinstance(pm, dict) and pm.get("id"):
        return pm
    return None


def episode_title(detail: dict, series: Optional[dict]) -> str:
    title = detail.get("title") or ""
    if series and series.get("title") and "|" in title:
        tail = title.split("|", 1)[1].strip()
        return tail or title
    return title


def names(detail: dict, series: Optional[dict]) -> tuple[str, Optional[str]]:
    """(file stem, series folder name or None)."""
    if series:
        stitle = sanitize(series.get("title") or detail.get("title"), 80)
        ep = detail.get("episodeNumber") or 0
        season = detail.get("seasonNumber") or series.get("seasonNumber") or 1
        if ep:
            return f"{stitle} - S{int(season):02d}E{int(ep):02d}", stitle
        return f"{stitle} - {sanitize(detail.get('title'), 80)} [{detail['id']}]", stitle
    when = parse_time(detail.get("availableDate") or detail.get("createdAt"))
    date = when.strftime("%Y-%m-%d") + " " if when else ""
    return f"{date}{sanitize(detail.get('title'), 90)} [{detail['id']}]", None


def mp4_tags(detail: dict, series: Optional[dict]) -> dict:
    when = parse_time(detail.get("availableDate") or detail.get("createdAt"))
    directors = detail.get("directors") or (series or {}).get("directors") or []
    actors = detail.get("actors") or (series or {}).get("actors") or []
    genres = [c.get("name") if isinstance(c, dict) else str(c) for c in (detail.get("categories") or [])]
    tags = [x.get("name") for x in detail.get("terms") or [] if isinstance(x, dict) and x.get("name")]
    tags_ = {
        "title": episode_title(detail, series) if series else detail.get("title"),
        "description": (detail.get("description") or "")[:250],
        "synopsis": detail.get("description"),
        "comment": f"{SITE}/watch/{detail['id']}",
        "date": when.strftime("%Y-%m-%d") if when else None,
        "artist": ", ".join(directors) or "VTVgo",
        "composer": ", ".join(actors[:20]) if actors else None,
        "genre": ", ".join(g for g in genres if g) or None,
        "keywords": ", ".join(tags) or None,
        "network": "VTVgo",
        "publisher": "VTVgo",
        "episode_id": detail["id"],
    }
    if series:
        tags_.update({
            "show": series.get("title"),
            "album": series.get("title"),
            "episode_sort": str(detail.get("episodeNumber") or ""),
            "track": str(detail.get("episodeNumber") or ""),
            "season_number": str(detail.get("seasonNumber") or series.get("seasonNumber") or 1),
            "media_type": "10",       # TV show (iTunes stik)
        })
    return {k: v for k, v in tags_.items() if v}


_PRIVATE_KEYS = re.compile(r"token|user|uid|phone|email|device|session|finger|kick|merchant|operator", re.I)


def scrub_source(src: dict) -> dict:
    """Playback answer for the .info.json, without anything tied to the account or the session:
    tokens, user / device / session ids, and the signed parts of the stream links."""
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()
                    if not _PRIVATE_KEYS.search(k) and k not in ("baseDrmInfo", "trackingData", "drmLicenses")}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj
    return redact_urls(clean(src))


def nfo_xml(detail: dict, series: Optional[dict], images: dict) -> str:
    def el(tag, value):
        return f"  <{tag}>{xml_escape(str(value))}</{tag}>\n" if value not in (None, "", []) else ""
    when = parse_time(detail.get("availableDate") or detail.get("createdAt"))
    root = "episodedetails" if series else "movie"
    body = el("title", episode_title(detail, series) if series else detail.get("title"))
    if series:
        body += el("showtitle", series.get("title"))
        body += el("season", detail.get("seasonNumber") or series.get("seasonNumber") or 1)
        body += el("episode", detail.get("episodeNumber"))
    body += el("plot", detail.get("description"))
    body += el("runtime", round((detail.get("durationSecond") or 0) / 60) or None)
    body += el("aired" if series else "premiered", when.strftime("%Y-%m-%d") if when else None)
    body += el("year", detail.get("year") or (when.year if when else None))
    body += el("mpaa", detail.get("ageRating"))
    body += el("country", detail.get("nation"))
    body += el("studio", "VTVgo")
    for d in detail.get("directors") or (series or {}).get("directors") or []:
        body += el("director", d)
    for g in detail.get("categories") or []:
        body += el("genre", g.get("name") if isinstance(g, dict) else g)
    for term in detail.get("terms") or []:
        if isinstance(term, dict) and term.get("name"):
            body += el("tag", term["name"])
    for kind, f in images.items():
        if kind in ("thumb", "poster"):
            body += el("thumb", f)
    body += f'  <uniqueid type="vtvgo" default="true">{xml_escape(detail["id"])}</uniqueid>\n'
    for a in detail.get("actors") or (series or {}).get("actors") or []:
        body += f"  <actor>\n  {el('name', a)}  </actor>\n"
    body += el("trailer", "")
    body += el("dateadded", datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"))
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<{root}>\n{body}</{root}>\n'


def tvshow_nfo(series: dict) -> str:
    def el(tag, value):
        return f"  <{tag}>{xml_escape(str(value))}</{tag}>\n" if value not in (None, "", []) else ""
    when = parse_time(series.get("availableDate"))
    body = el("title", series.get("title")) + el("plot", series.get("description"))
    body += el("premiered", when.strftime("%Y-%m-%d") if when else None)
    body += el("year", series.get("year") or (when.year if when else None))
    body += el("mpaa", series.get("ageRating")) + el("studio", "VTVgo")
    for d in series.get("directors") or []:
        body += el("director", d)
    body += f'  <uniqueid type="vtvgo" default="true">{xml_escape(str(series.get("id")))}</uniqueid>\n'
    for a in series.get("actors") or []:
        body += f"  <actor>\n  {el('name', a)}  </actor>\n"
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<tvshow>\n{body}</tvshow>\n'


def fetch_image(http: Http, ffmpeg: Optional[str], url: Optional[str], dest_stem: Path) -> Optional[Path]:
    """Download the original image and store it as JPEG (needed for the MP4 cover)."""
    url = original_image(url)
    if not url:
        return None
    jpg = dest_stem.with_suffix(".jpg")
    if jpg.is_file() and jpg.stat().st_size > 0:
        return jpg
    try:
        r = http.s.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException:
        return None
    ext = Path(urlparse(url).path).suffix.lower() or ".img"
    if ext in (".jpg", ".jpeg"):
        jpg.write_bytes(r.content)
        return jpg
    raw = dest_stem.with_name(dest_stem.name + ".src" + ext)
    raw.write_bytes(r.content)
    if ffmpeg:
        p = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                            "-frames:v", "1", "-q:v", "2", str(jpg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if p.returncode == 0 and jpg.is_file():
            raw.unlink(missing_ok=True)
            return jpg
    final = dest_stem.with_suffix(ext)
    os.replace(raw, final)
    return final


SUB_URL_KEYS = ("url", "link", "file", "src", "path")
SUB_LANG_KEYS = ("language", "lang", "code", "languageCode", "label", "name")


def subtitle_tracks(detail: dict, src: dict) -> list[tuple[str, str]]:
    out = []
    for coll in (detail.get("subTitles"), detail.get("subtitles"), src.get("subTitles"), src.get("subtitles")):
        for s in coll or []:
            if isinstance(s, str):
                out.append((s, "vie"))
            elif isinstance(s, dict):
                url = next((s[k] for k in SUB_URL_KEYS if isinstance(s.get(k), str) and s[k]), None)
                lang = next((str(s[k]) for k in SUB_LANG_KEYS if s.get(k)), "und")
                if url:
                    out.append((url, lang))
    seen, uniq = set(), []
    for u, lang in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, lang))
    return uniq


ISO3 = {"vi": "vie", "vn": "vie", "vie": "vie", "en": "eng", "eng": "eng", "zh": "zho", "ko": "kor", "ja": "jpn"}


# --------------------------------------------------------------------------- #
# Progress database
# --------------------------------------------------------------------------- #

class Progress:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, status TEXT, file TEXT, "
                        "title TEXT, series TEXT, episode INTEGER, date TEXT, duration INTEGER, "
                        "quality TEXT, size INTEGER, error TEXT, updated TEXT)")
        self.db.commit()

    def done(self, vod_id: str, root: Path) -> Optional[str]:
        row = self.db.execute("SELECT status, file FROM videos WHERE id = ?", (vod_id,)).fetchone()
        if row and row[0] == "done" and row[1] and (root / row[1]).exists():
            return row[1]
        return None

    def mark(self, vod_id: str, state: str, **kw) -> None:
        cols = ["id", "status", "updated"] + list(kw)
        vals = [vod_id, state, datetime.now(VN_TZ).isoformat(timespec="seconds")] + list(kw.values())
        with self.lock:
            old = self.db.execute("SELECT * FROM videos WHERE id = ?", (vod_id,)).fetchone()
            if old:
                sets = ", ".join(f"{c} = ?" for c in cols[1:])
                self.db.execute(f"UPDATE videos SET {sets} WHERE id = ?", vals[1:] + [vod_id])
            else:
                self.db.execute(f"INSERT INTO videos ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                vals)
            self.db.commit()

    def rows(self) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT id, status, file, title, series, episode, date, duration, quality, "
                                   "size, error FROM videos ORDER BY series, episode, date").fetchall()

    def close(self) -> None:
        with self.lock:
            self.db.close()


def item_url(key: str) -> str:
    """Progress key -> link: VOD ids, "catchup:<channel>:<program>:<start>" and "live:<channel>:<time>"."""
    kind, _, rest = key.partition(":")
    if kind in ("catchup", "live") and rest:
        return f"{SITE}/channel/{rest.split(':')[0]}"
    return f"{SITE}/watch/{key}"


def write_reports(progress: Progress, root: Path) -> int:
    rows = progress.rows()
    tmp = root / "videos.csv.tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "status", "file", "title", "series", "episode", "date", "duration_s", "quality",
                     "size_bytes", "error", "url"])
        for r in rows:
            w.writerow(list(r) + [item_url(r[0])])
    try:
        os.replace(tmp, root / "videos.csv")
    except PermissionError:
        log(warn(t("videos.csv is open in another program, not updated", "videos.csv đang mở ở chương trình khác, chưa cập nhật")))
        tmp.unlink(missing_ok=True)
    failed = [r for r in rows if r[1] == "error"]
    path = root / "failed.txt"
    if failed:
        lines = [f"{item_url(r[0])}  # {r[3] or ''} — {r[10] or ''}" for r in failed]
        write_atomic(path, "\n".join(lines) + "\n")
    else:
        path.unlink(missing_ok=True)
    return len(failed)


# --------------------------------------------------------------------------- #
# One video
# --------------------------------------------------------------------------- #

def run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    if STOP.is_set():
        raise VtvgoError(t("stopped", "đã dừng"))
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        return False, str(e)
    if p.returncode and STOP.wait(0.5):
        raise VtvgoError(t("stopped", "đã dừng"))
    return p.returncode == 0, p.stderr.decode("utf-8", "replace").strip()[-400:]


def mux(ffmpeg: str, video_m3u8: Path, audio_m3u8: Optional[Path], cover: Optional[Path],
        subs: list[tuple[Path, str]], tags: dict, out: Path) -> None:
    tmp = out.with_name(out.stem + ".tmp.mp4")
    hls_in = ["-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto,data"]
    last = ""
    attempts = []
    for with_cover, with_subs in ((True, True), (False, True), (False, False)):
        a = (with_cover and cover is not None and cover.suffix == ".jpg", with_subs and bool(subs))
        if a not in attempts:
            attempts.append(a)
    for use_cover, use_subs in attempts:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *hls_in, "-i", str(video_m3u8)]
        maps = ["-map", "0:v:0"]
        idx = 1
        if audio_m3u8:
            cmd += [*hls_in, "-i", str(audio_m3u8)]
            maps += ["-map", f"{idx}:a:0"]
            idx += 1
        else:
            maps += ["-map", "0:a?"]
        cover_idx = None
        if use_cover:
            cmd += ["-i", str(cover)]
            cover_idx = idx
            maps += ["-map", f"{idx}:v:0"]
            idx += 1
        sub_meta = []
        if use_subs:
            for n, (path, lang) in enumerate(subs):
                cmd += ["-i", str(path)]
                maps += ["-map", f"{idx}:s:0"]
                sub_meta += [f"-metadata:s:s:{n}", f"language={lang}"]
                idx += 1
        cmd += maps + ["-c", "copy"]
        if use_subs:
            cmd += ["-c:s", "mov_text", *sub_meta]
        if cover_idx is not None:
            cmd += ["-disposition:v:1", "attached_pic"]
        cmd += ["-metadata:s:a:0", "language=vie", "-movflags", "+faststart"]
        for k, v in tags.items():
            cmd += ["-metadata", f"{k}={v}"]
        cmd.append(str(tmp))
        ok, err = run_ffmpeg(cmd)
        if ok and tmp.is_file() and tmp.stat().st_size > 0:
            os.replace(tmp, out)
            return
        last = err
        tmp.unlink(missing_ok=True)
    raise VtvgoError(f"ffmpeg: {last}")


@dataclass
class Ctx:
    api: Api
    http: Http
    opts: argparse.Namespace
    root: Path
    progress: Progress
    ffmpeg: Optional[str]
    aria2: Optional[str]
    series_done: set = field(default_factory=set)


def write_series(ctx: Ctx, series: dict, folder: Path) -> None:
    if series.get("id") in ctx.series_done:
        return
    ctx.series_done.add(series.get("id"))
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(folder / "tvshow.nfo", tvshow_nfo(series))
    write_atomic(folder / "series.info.json", json.dumps(series, ensure_ascii=False, indent=2))
    if not ctx.opts.no_images:
        imgs = series.get("images") or {}
        for kind, key in (("poster", "vertical"), ("fanart", "background"), ("landscape", "horizontal"),
                          ("banner", "horizontalBig")):
            fetch_image(ctx.http, ctx.ffmpeg, imgs.get(key), folder / kind)


# --------------------------------------------------------------------------- #
# Shared HLS -> MP4, live recording
# --------------------------------------------------------------------------- #

def fetch_to_mp4(ctx: Ctx, variant: Variant, folder: Path, stem: str, title: str, cover: Optional[Path],
                 subs: list, tags: dict, out: Path, refresh=None) -> Path:
    """Download a finished stream into `out`. A playlist that turns out to be live is recorded instead
    (needs `refresh`, which returns fresh variants when the signed links expire)."""
    work = folder / f"{stem}.parts"
    # The segments are numbered, not named after the stream: if an earlier, unfinished run picked another
    # rendition (other --quality, other CDN), start over instead of mixing parts of both.
    sig = redact(f"{variant.width}x{variant.height} {variant.bandwidth} {variant.url}").rsplit("/", 2)
    sig = " ".join(sig[:1] + sig[-2:])
    marker = work / "stream.txt"
    try:
        if work.is_dir() and marker.read_text(encoding="utf-8") != sig:
            shutil.rmtree(work, ignore_errors=True)
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    marker.write_text(sig, encoding="utf-8")
    try:
        if ".m3u8" in variant.url.split("?")[0]:
            vplan = plan_media(ctx.http, variant.url, work, "v")
            if vplan.live:
                shutil.rmtree(work, ignore_errors=True)
                if refresh is None:
                    raise VtvgoError(t("this is a live stream", "đây là luồng trực tiếp"))
                log(gray(t("    live stream: recording", "    luồng trực tiếp: đang ghi")))
                return record_live(ctx, variant, refresh, folder, stem, title, tags, cover)
            aplan = plan_media(ctx.http, variant.audio, work, "a") if variant.audio else None
            if not (vplan.byterange or (aplan and aplan.byterange)):
                (work / "video.m3u8").write_text(vplan.local_text, encoding="utf-8")
                if aplan:
                    (work / "audio.m3u8").write_text(aplan.local_text, encoding="utf-8")
                tasks = vplan.tasks + (aplan.tasks if aplan else [])
                with SegmentBar(title, [d for _, d in tasks]):
                    download_segments(ctx.http, tasks, ctx.opts, ctx.aria2)
                status(t("  joining with ffmpeg ...", "  đang ghép bằng ffmpeg ..."))
                mux(ctx.ffmpeg, work / "video.m3u8", work / "audio.m3u8" if aplan else None, cover, subs, tags, out)
                status("")
                if not ctx.opts.keep_parts:
                    shutil.rmtree(work, ignore_errors=True)
                return out
        # Not plain HLS (DASH, single MP4, byte ranges): let ffmpeg read the stream itself.
        cmd = [ctx.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-user_agent", UA,
               "-headers", f"Referer: {SITE}/\r\n", "-i", variant.url, "-map", "0:v:0", "-map", "0:a?",
               "-c", "copy", "-movflags", "+faststart"]
        for k, v in tags.items():
            cmd += ["-metadata", f"{k}={v}"]
        tmp = out.with_name(out.stem + ".tmp.mp4")
        status(t("  downloading with ffmpeg ...", "  đang tải bằng ffmpeg ..."))
        ok, msg = run_ffmpeg(cmd + [str(tmp)])
        if not ok:
            tmp.unlink(missing_ok=True)
            raise VtvgoError(f"ffmpeg: {msg}")
        os.replace(tmp, out)
        shutil.rmtree(work, ignore_errors=True)
        return out
    finally:
        status("")


def parse_duration(text: Optional[str]) -> Optional[float]:
    """"90m", "1h30m", "1:30:00", "45:00", "5400" -> seconds."""
    if not text:
        return None
    text = text.strip().lower()
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part or 0)
        return total
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m(?:in)?)?\s*(?:(\d+(?:\.\d+)?)s?)?", text)
    if not m or not any(m.groups()):
        raise ValueError(text)
    h, mi, s = (float(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")


def closest(variants: list[Variant], height: int, quality: str) -> Variant:
    same = [v for v in variants if v.height == height]
    return pick_variant(same or variants, "best" if same else quality)


def record_live(ctx: Ctx, variant: Variant, refresh, folder: Path, stem: str, title: str,
                tags: dict, cover: Optional[Path]) -> Path:
    """Follow a live HLS playlist and append every new segment to a .ts file, then remux to MP4.

    Stops after --duration, when the stream ends, or on Ctrl+C. Expired links are renewed through
    `refresh` (a new playback/source call) at the same resolution."""
    if variant.audio:
        raise VtvgoError(t("live streams with separate audio are not supported",
                           "chưa hỗ trợ luồng trực tiếp có âm thanh tách riêng"))
    limit = ctx.opts.duration
    started = datetime.now(VN_TZ)
    name = stem if "LIVE" in stem else f"{stem} [{started:%Y%m%d-%H%M%S}]"
    ts_path = folder / f"{name}.ts"
    out = folder / f"{name}.mp4"
    media_url, height, width = variant.url, variant.height, variant.width
    last_seq: Optional[int] = None
    recorded = 0.0
    size = 0
    failures = gaps = 0
    stopped_by_user = False
    pool = ThreadPoolExecutor(4)
    how_long = fmt_eta(limit) if limit else t("until Ctrl+C", "đến khi bấm Ctrl+C")
    log(gray(t(f"    recording {how_long} → {ts_path.name}", f"    đang ghi {how_long} → {ts_path.name}")))

    def fetch(url: str) -> bytes:
        for attempt in range(4):
            try:
                r = ctx.http.s.get(url, timeout=30)
                if r.status_code == 200 and r.content:
                    return r.content
            except requests.RequestException:
                pass
            time.sleep(1 + attempt)
        return b""

    def show() -> None:
        dot = red("●") if int(time.time()) % 2 else gray("●")
        goal = f" / {fmt_eta(limit)}" if limit else ""
        res = f"{width}x{height}" if height else ""
        sep = gray(" │ ")
        gap_txt = f"{sep}{yellow(str(gaps) + ' gap')}" if gaps else ""
        status(f"  {dot} {bold('REC')} {fmt_eta(recorded)}{goal}{sep}{fmt_bytes(size)}{sep}{res}{gap_txt}")

    try:
        with open(ts_path, "ab") as fh:
            while not STOP.is_set():
                try:
                    text, final = ctx.http.text(media_url)
                    failures = 0
                except VtvgoError:
                    failures += 1
                    if failures > 6:
                        raise VtvgoError(t("the live stream stopped answering", "luồng trực tiếp không phản hồi"))
                    try:   # signed link expired: ask the API for a new one
                        fresh = closest(refresh(), height, ctx.opts.quality)
                        media_url, height, width = fresh.url, fresh.height, fresh.width
                    except VtvgoError:
                        time.sleep(3)
                    continue
                if "#EXT-X-STREAM-INF" in text:
                    best = closest(parse_master(text, final, ""), height, ctx.opts.quality)
                    media_url = best.url
                    height, width = best.height or height, best.width or width
                    continue
                if re.search(r"#EXT-X-KEY:.*METHOD=(?!NONE)", text):
                    raise VtvgoError(t("encrypted live stream, not supported", "luồng trực tiếp mã hoá, chưa hỗ trợ"))
                m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
                seq = int(m.group(1)) if m else 0
                segs, dur = [], 0.0
                for ln in text.splitlines():
                    ln = ln.strip()
                    if ln.startswith("#EXTINF"):
                        mm = EXTINF_RE.match(ln)
                        dur = float(mm.group(1)) if mm else 0.0
                    elif ln and not ln.startswith("#"):
                        segs.append((seq, dur, urljoin(final, ln)))
                        seq += 1
                if last_seq is None:
                    new = segs if ctx.opts.live_from_start else segs[-3:]
                else:
                    new = [s for s in segs if s[0] > last_seq]
                    if new and new[0][0] > last_seq + 1:
                        gaps += 1
                futures = [pool.submit(fetch, s[2]) for s in new]
                for (sq, d, _url), fut in zip(new, futures):
                    while not fut.done():
                        time.sleep(0.05)
                    data = fut.result()
                    if data:
                        fh.write(data)
                        size += len(data)
                        recorded += d
                    else:
                        gaps += 1
                    last_seq = sq
                    show()
                    if limit and recorded >= limit:
                        break
                fh.flush()
                if (limit and recorded >= limit) or "#EXT-X-ENDLIST" in text:
                    break
                target = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
                wait = max(1.0, (float(target.group(1)) if target else 4.0) / 2)
                end = time.monotonic() + wait
                while time.monotonic() < end and not STOP.is_set():
                    show()
                    time.sleep(0.25)
    except KeyboardInterrupt:
        stopped_by_user = True
        status("")
        log(yellow(t("    stopping the recording ...", "    đang dừng ghi ...")))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        status("")
    if size == 0:
        ts_path.unlink(missing_ok=True)
        raise VtvgoError(t("nothing was recorded", "không ghi được gì"))
    if gaps:
        log(warn(t(f"{gaps} gap(s) in the recording (network too slow?)",
                   f"bản ghi bị hụt {gaps} chỗ (mạng chậm?)")))
    tags = {**tags, "date": started.strftime("%Y-%m-%d")}
    cmd = [ctx.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(ts_path)]
    if cover and cover.suffix == ".jpg":
        cmd += ["-i", str(cover), "-map", "0:v:0", "-map", "0:a?", "-map", "1:v:0", "-disposition:v:1", "attached_pic"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
    cmd += ["-c", "copy", "-movflags", "+faststart"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    tmp = out.with_name(out.stem + ".tmp.mp4")
    status(t("  joining with ffmpeg ...", "  đang ghép bằng ffmpeg ..."))
    p = subprocess.run(cmd + [str(tmp)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    status("")
    if p.returncode == 0 and tmp.is_file():
        os.replace(tmp, out)
        if not ctx.opts.keep_parts:
            ts_path.unlink(missing_ok=True)
    else:
        tmp.unlink(missing_ok=True)
        log(warn(t(f"could not convert to MP4, kept {ts_path.name}", f"không chuyển được sang MP4, giữ {ts_path.name}")))
        out = ts_path
    log(gray(t(f"    recorded {fmt_eta(recorded)}", f"    đã ghi {fmt_eta(recorded)}")))
    if stopped_by_user:
        STOP.set()   # Ctrl+C ends the whole run, after the recording has been saved
    return out


# --------------------------------------------------------------------------- #
# Channels: list, EPG, catch-up, live
# --------------------------------------------------------------------------- #

def vn_day(text: Optional[str]) -> str:
    if not text:
        return datetime.now(VN_TZ).strftime("%Y-%m-%d")
    if text.lower() in ("yesterday", "homqua", "hom-qua"):
        return (datetime.now(VN_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m"):
        try:
            d = datetime.strptime(text, fmt)
            if fmt == "%d/%m":
                d = d.replace(year=datetime.now(VN_TZ).year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(text)


def list_channels(api: Api) -> int:
    group = None
    for ch in api.channels().values():
        if ch.get("group") != group:
            group = ch.get("group")
            log("\n" + bold_cyan(group or ""))
        hours = ch.get("maxCatchUpTimeValue") if ch.get("catchUpToVodEnabled") else None
        extra = f"{t('catch-up', 'xem lại')} {hours}h" if hours else ""
        cid = f"{ch['id']:<28}"
        log(f"  {cyan(cid)} {ch.get('name', '')}  {gray(extra)}")
    log(gray(t("\nUse: python vtvgo-dl.py https://vtvgo.vn/channel/<id>  (--epg, --program, --duration)",
               "\nDùng: python vtvgo-dl.py https://vtvgo.vn/channel/<id>  (--epg, --program, --duration)")))
    return 0


def prog_times(p: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    return parse_time(p.get("startTime")), parse_time(p.get("endTime"))


def print_epg(api: Api, ch_id: str, name: str, day: str) -> None:
    now = datetime.now(VN_TZ)
    log(bold(f"{name} — {day}"))
    for p in api.epg(ch_id, day):
        st, en = prog_times(p)
        if not st or not en:
            continue
        mark = red("● LIVE") if st <= now < en else (green("✓") if en < now else " ")
        desc = f" — {p['description']}" if p.get("description") else ""
        pid = str(p.get("programID"))
        log(f"  {st:%H:%M}-{en:%H:%M}  {mark:<6} {gray(pid)}  {p.get('title', '')}{gray(desc)}")
    log(gray(t("✓ = finished (catch-up). Download with  --program <id or title>  (and --date).",
               "✓ = đã phát xong (xem lại). Tải bằng  --program <id hoặc tên>  (kèm --date).")))


def select_programs(api: Api, ch_id: str, wanted: str, day: str) -> list[dict]:
    """Programs of `day` matching a comma list of program ids / title words / "all".
    Ids not found on that day are looked up in the 8 days before it."""
    now = datetime.now(VN_TZ)
    progs = api.epg(ch_id, day)
    chosen: dict = {}
    for token in [x.strip() for x in wanted.split(",") if x.strip()]:
        low = token.lower()
        if low == "all":
            hits = progs
        elif token.isdigit():
            hits = [p for p in progs if str(p.get("programID")) == token]
            back = datetime.strptime(day, "%Y-%m-%d")
            for i in range(1, 9):
                if hits:
                    break
                d = (back - timedelta(days=i)).strftime("%Y-%m-%d")
                hits = [p for p in api.epg(ch_id, d) if str(p.get("programID")) == token]
        else:
            hits = [p for p in progs if low in f"{p.get('title', '')} {p.get('description', '')}".lower()]
        if not hits:
            log(warn(t(f"no program matches '{token}' on {day}", f"không có chương trình khớp '{token}' ngày {day}")))
        for p in hits:
            chosen[(p.get("programID"), p.get("startTime"))] = p
    out = []
    for p in sorted(chosen.values(), key=lambda p: p.get("startTime") or ""):
        st, en = prog_times(p)
        if not st or not en:
            continue
        if en > now:
            log(warn(t(f"{st:%H:%M} {p.get('title')}: not finished yet, skipped",
                       f"{st:%H:%M} {p.get('title')}: chưa phát xong, bỏ qua")))
            continue
        out.append(p)
    return out


def channel_tags(ch: dict, title: str, desc: str, when: Optional[datetime], link: str) -> dict:
    tags = {"title": title, "show": ch.get("name"), "album": ch.get("name"), "artist": ch.get("name"),
            "network": ch.get("name"), "publisher": "VTVgo", "description": (desc or "")[:250],
            "synopsis": desc, "date": when.strftime("%Y-%m-%d") if when else None, "comment": link}
    return {k: v for k, v in tags.items() if v}


def channel_nfo(ch: dict, title: str, desc: str, when: Optional[datetime], runtime: float) -> str:
    def el(tag, value):
        return f"  <{tag}>{xml_escape(str(value))}</{tag}>\n" if value not in (None, "", []) else ""
    body = el("title", title) + el("showtitle", ch.get("name")) + el("plot", desc)
    body += el("aired", when.strftime("%Y-%m-%d") if when else None) + el("runtime", round(runtime / 60) or None)
    body += el("studio", ch.get("name")) + el("genre", "TV")
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n{body}</episodedetails>\n'


def save_meta(folder: Path, stem: str, info: dict, nfo: str) -> None:
    write_atomic(folder / f"{stem}.info.json", json.dumps(info, ensure_ascii=False, indent=2))
    write_atomic(folder / f"{stem}.nfo", nfo)


def run_catchup(ctx: Ctx, ch: dict, folder: Path, cover: Optional[Path], counts: dict) -> None:
    opts = ctx.opts
    ch_id, name = ch["id"], ch.get("name") or ch["id"]
    link = f"{SITE}/channel/{ch_id}"
    progs = select_programs(ctx.api, ch_id, opts.program, vn_day(opts.date))
    log(bold(t(f"{name}: {len(progs)} program(s) to download", f"{name}: {len(progs)} chương trình cần tải")))
    for n, p in enumerate(progs, 1):
        if STOP.is_set():
            break
        st, en = prog_times(p)
        title = p.get("title") or ""
        desc = p.get("description") or ""
        full = title + (f" - {desc}" if desc else "")
        key = f"catchup:{ch_id}:{p.get('programID')}:{p.get('startTime')}"
        stem = sanitize(f"{st:%Y-%m-%d %H%M} {full}", 110)
        when = f"{st:%d/%m %H:%M}-{en:%H:%M}"
        log(f"{gray(f'[{n}/{len(progs)}]')} {bold(title)} {gray(when + (' — ' + desc if desc else ''))}")
        if ctx.progress.done(key, ctx.root) and not opts.metadata_only:
            log(gray(t("    already downloaded", "    đã tải rồi")))
            counts["skip"] += 1
            continue
        row = dict(title=full, series=name, date=f"{st:%Y-%m-%d}", duration=int((en - st).total_seconds()))
        try:
            src = ctx.api.catchup_source(ch_id, p)
            variant = pick_variant(list_variants(ctx.http, src), opts.quality)
            log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
            save_meta(folder, stem, {"channel": ch, "program": p, "format": redact_urls(variant.__dict__),
                                     "playback": scrub_source(src), "tool": f"vtvgo-dl {__version__}",
                                     "downloaded_at": datetime.now(VN_TZ).isoformat(timespec="seconds")},
                      channel_nfo(ch, full, desc or title, st, (en - st).total_seconds()))
            if opts.metadata_only:
                counts["done"] += 1
                continue
            out = fetch_to_mp4(ctx, variant, folder, stem, title, cover, [],
                               channel_tags(ch, full, desc, st, link), folder / f"{stem}.mp4")
        except VtvgoError as e:
            status("")
            log(f"    {red('✗')} {e}")
            ctx.progress.mark(key, "error", error=str(e), **row)
            counts["error"] += 1
            continue
        size = out.stat().st_size
        ctx.progress.mark(key, "done", file=str(out.relative_to(ctx.root)), quality=variant.label,
                          size=size, error=None, **row)
        log(f"    {green('✓')} {out.name} {gray(fmt_bytes(size))}")
        counts["done"] += 1


def run_live(ctx: Ctx, ch: dict, session_id: str, folder: Path, cover: Optional[Path], counts: dict) -> None:
    ch_id, name = ch["id"], ch.get("name") or ch["id"]
    link = f"{SITE}/channel/{ch_id}" + (f"?sessionId={session_id}" if session_id else "")
    now = datetime.now(VN_TZ)
    today = ctx.api.epg(ch_id, now.strftime("%Y-%m-%d"))

    def playing(p: dict) -> bool:
        st, en = prog_times(p)
        return bool(st and en and st <= now < en)
    current = next((p for p in today if playing(p)), None)
    what = (current or {}).get("title") or ""
    desc = (current or {}).get("description") or ""
    if session_id:
        data = ctx.api.live_session(ch_id, session_id)
        if not (data.get("live") or {}).get("url"):
            log(yellow(t(f"Live session is '{data.get('status')}' (start {data.get('startTime')}), nothing to record.",
                         f"Phiên live đang '{data.get('status')}' (bắt đầu {data.get('startTime')}), chưa có gì để ghi.")))
            counts["error"] += 1
            return
        what = data.get("title") or what

        def refresh():
            d = ctx.api.live_session(ch_id, session_id)
            return list_variants(ctx.http, {"sourceModes": [{"sources": [{"url": (d.get("live") or {}).get("url")}]}]})
    else:
        def refresh():
            return list_variants(ctx.http, ctx.api.channel_source(ch_id))
    key = f"live:{ch_id}:{now:%Y%m%d%H%M%S}"
    log(f"{bold(name)} {red('● LIVE')} {what} {gray(desc)}")
    try:
        variant = pick_variant(refresh(), ctx.opts.quality)
        label = f"{name} LIVE {now:%d/%m/%Y %H:%M}" + (f" - {what}" if what else "")
        stem = sanitize(f"{now:%Y-%m-%d %H%M} {name} LIVE" + (f" - {what}" if what else ""), 110)
        log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
        out = record_live(ctx, variant, refresh, folder, stem, label,
                          channel_tags(ch, label, desc, now, link), cover)
    except VtvgoError as e:
        log(f"    {red('✗')} {e}")
        ctx.progress.mark(key, "error", error=str(e), title=f"{name} LIVE", series=name)
        counts["error"] += 1
        return
    end = datetime.now(VN_TZ)
    progs = [p for p in today if prog_times(p)[0] and prog_times(p)[1] and prog_times(p)[1] > now
             and prog_times(p)[0] < end]
    lineup = "\n".join(f"{prog_times(p)[0]:%H:%M} {p.get('title', '')} {p.get('description', '')}".strip()
                       for p in progs)
    save_meta(folder, out.stem, {"channel": ch, "recorded_from": now.isoformat(timespec="seconds"),
                                 "recorded_to": end.isoformat(timespec="seconds"), "programs": progs,
                                 "format": redact_urls(variant.__dict__), "tool": f"vtvgo-dl {__version__}"},
              channel_nfo(ch, label, lineup, now, (end - now).total_seconds()))
    size = out.stat().st_size
    ctx.progress.mark(key, "done", file=str(out.relative_to(ctx.root)), quality=variant.label, size=size,
                      title=label, series=name, date=f"{now:%Y-%m-%d}", duration=int((end - now).total_seconds()))
    log(f"    {green('✓')} {out.name} {gray(fmt_bytes(size))}")
    counts["done"] += 1


def run_channel(ctx: Ctx, value: str, counts: dict) -> None:
    ch_id, _, session_id = value.partition("?")
    try:
        ch = dict(ctx.api.channels().get(ch_id) or {"id": ch_id, "name": ch_id})
    except VtvgoError:
        ch = {"id": ch_id, "name": ch_id}
    for k in ("platforms", "allowCastPlatforms"):
        ch.pop(k, None)
    name = ch.get("name") or ch_id
    if ctx.opts.epg:
        print_epg(ctx.api, ch_id, name, vn_day(ctx.opts.date))
        return
    folder = ctx.root / "Channels" / sanitize(name)
    folder.mkdir(parents=True, exist_ok=True)
    cover = None if ctx.opts.no_images else fetch_image(ctx.http, ctx.ffmpeg, ch.get("thumbnail") or ch.get("logo"),
                                                         folder / "channel")
    if ctx.opts.program:
        run_catchup(ctx, ch, folder, cover, counts)
    else:
        run_live(ctx, ch, session_id, folder, cover, counts)


def process(ctx: Ctx, job: Job, n: int, total: int) -> str:
    """Download one VOD. Returns "done", "skip" or "error"."""
    opts = ctx.opts
    head = gray(f"[{n}/{total}]")
    try:
        detail = ctx.api.detail(job.vod_id)
    except VtvgoError as e:
        log(f"{head} {red('✗')} {job.vod_id}: {e}")
        ctx.progress.mark(job.vod_id, "error", error=str(e))
        return "error"
    series = series_info(detail)
    if series and series.get("id") and series is detail.get("parentMetadata") and not series.get("title"):
        series = None
    stem, series_name = names(detail, series)
    folder = ctx.root / series_name if series_name else ctx.root.joinpath(*job.folder)
    title = detail.get("title") or job.vod_id
    log(f"{head} {bold(title)} {gray(f'({fmt_duration(detail.get('durationSecond'))}, {job.vod_id})')}")

    prev = ctx.progress.done(job.vod_id, ctx.root)
    if prev and not opts.metadata_only:
        log(gray(t(f"    already downloaded: {prev}", f"    đã tải rồi: {prev}")))
        return "skip"

    folder.mkdir(parents=True, exist_ok=True)
    if series:
        write_series(ctx, series, folder)

    src: dict = {}
    variant: Optional[Variant] = None
    err: Optional[VtvgoError] = None
    if not opts.metadata_only or opts.list_formats:
        try:
            src = ctx.api.source(job.vod_id)
            variants = list_variants(ctx.http, src)
            if opts.list_formats:
                for v in sorted(variants, key=lambda v: (v.height, v.bandwidth), reverse=True):
                    log(f"    {cyan(v.label)}  {gray(v.source)}")
                return "skip"
            variant = pick_variant(variants, opts.quality)
            log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
        except VtvgoError as e:
            err = e
            if opts.list_formats:
                log(f"    {red('✗')} {e}")
                return "error"

    # Metadata (written even when the stream is not available, e.g. needs a plan).
    images: dict[str, str] = {}
    if not opts.no_images:
        imgs = detail.get("images") or {}
        thumb = fetch_image(ctx.http, ctx.ffmpeg, imgs.get("horizontal") or imgs.get("horizontalBig")
                            or src.get("firstImage") or imgs.get("background"), folder / f"{stem}-thumb")
        if thumb:
            images["thumb"] = thumb.name
        if not series:
            for kind, key in (("poster", "vertical"), ("fanart", "background")):
                f = fetch_image(ctx.http, ctx.ffmpeg, imgs.get(key), folder / f"{stem}-{kind}")
                if f:
                    images[kind] = f.name
    info = {
        "id": detail["id"],
        "url": f"{SITE}/watch/{detail['id']}",
        "title": detail.get("title"),
        "series": (series or {}).get("title"),
        "episode": detail.get("episodeNumber") or None,
        "format": redact_urls(variant.__dict__) if variant else None,
        "downloaded_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
        "tool": f"vtvgo-dl {__version__}",
        "detail": detail,
        "playback": scrub_source(src) if src else None,   # tokens and signed links removed
    }
    write_atomic(folder / f"{stem}.info.json", json.dumps(info, ensure_ascii=False, indent=2))
    write_atomic(folder / f"{stem}.nfo", nfo_xml(detail, series, images))
    desc = detail.get("description")
    if desc:
        write_atomic(folder / f"{stem}.description.txt", desc + "\n")

    row = dict(title=title, series=(series or {}).get("title"), episode=detail.get("episodeNumber"),
               date=(parse_time(detail.get("availableDate")) or datetime.now(VN_TZ)).strftime("%Y-%m-%d"),
               duration=detail.get("durationSecond"))
    if opts.metadata_only:
        ctx.progress.mark(job.vod_id, "metadata", file=str((folder / f"{stem}.info.json").relative_to(ctx.root)), **row)
        log(green(t("    metadata saved", "    đã lưu metadata")))
        return "done"
    if err or not variant:
        log(f"    {red('✗')} {err}")
        ctx.progress.mark(job.vod_id, "error", error=str(err), **row)
        return "error"
    if not ctx.ffmpeg:
        raise VtvgoError(t("ffmpeg not found", "không tìm thấy ffmpeg"))

    out = folder / f"{stem}.mp4"
    # Subtitles
    subs: list[tuple[Path, str]] = []
    if not opts.no_subs:
        for url, lang in subtitle_tracks(detail, src):
            code = ISO3.get(lang.lower()[:3], ISO3.get(lang.lower()[:2], "und"))
            ext = Path(urlparse(url).path).suffix.lower() or ".vtt"
            dest = folder / f"{stem}.{code}{ext}"
            try:
                r = ctx.http.s.get(url, timeout=30)
                r.raise_for_status()
                dest.write_bytes(r.content)
                subs.append((dest, code))
            except requests.RequestException:
                log(warn(t(f"subtitle {lang} failed", f"phụ đề {lang} lỗi")))

    cover = (folder / images["thumb"]) if images.get("thumb") else None
    tags = mp4_tags(detail, series)
    try:
        if detail.get("isLive") or src.get("isLive"):
            out = record_live(ctx, variant, lambda: list_variants(ctx.http, ctx.api.source(job.vod_id)),
                              folder, stem, title, tags, cover)
        else:
            out = fetch_to_mp4(ctx, variant, folder, stem, title, cover, subs, tags, out,
                         refresh=lambda: list_variants(ctx.http, ctx.api.source(job.vod_id)))
    except VtvgoError as e:
        status("")
        log(f"    {red('✗')} {e}")
        ctx.progress.mark(job.vod_id, "error", error=str(e), **row)
        return "error"
    size = out.stat().st_size
    # A live recording is not "downloaded": running the command again records again.
    state = "done" if out.name == f"{stem}.mp4" else "recorded"
    ctx.progress.mark(job.vod_id, state, file=str(out.relative_to(ctx.root)), quality=variant.label,
                      size=size, error=None, **row)
    log(f"    {green('✓')} {out.name} {gray(fmt_bytes(size))}")
    return "done"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def collect_jobs(ctx: Ctx, inputs: list[str]) -> list[Job]:
    """Expand every input; a series (or an episode with --whole-series) becomes all its episodes."""
    opts = ctx.opts
    jobs: list[Job] = []
    for text in inputs:
        try:
            found = expand(ctx.api, text, opts)
        except ValueError:
            log(red(t(f"Not a vtvgo link or id: {text}", f"Không phải link/id vtvgo: {text}")))
            continue
        except VtvgoError as e:
            log(red(f"{text}: {e}"))
            continue
        for job in found:
            try:
                d = ctx.api.detail(job.vod_id)
            except VtvgoError:
                jobs.append(job)
                continue
            series = None
            if d.get("type") == "GROUP_VIDEO":
                series = d
            elif opts.whole_series and isinstance(d.get("parentMetadata"), dict) and d["parentMetadata"].get("id"):
                series = d["parentMetadata"]
            if series:
                total = series.get("currentEpisodeCount") or series.get("totalEpisode") or 0
                log(f"  {t('Series', 'Phim bộ')}: {bold(series.get('title'))} — "
                    f"{total or '?'} {t('episodes', 'tập')}")
                eps = ctx.api.episodes(str(series["id"]), int(total or 0))
                if opts.episodes:
                    eps = [e for e in eps if (e.get("episodeNumber") or 0) in opts.episodes]
                jobs += [Job(e["id"], job.folder) for e in eps]
            else:
                jobs.append(job)
    seen, uniq = set(), []
    for j in jobs:
        if j.vod_id not in seen:
            seen.add(j.vod_id)
            uniq.append(j)
    return uniq


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vtvgo-dl",
        description=t("Download videos, series and pages from vtvgo.vn in the best quality, with full metadata.",
                      "Tải video, phim bộ và cả trang từ vtvgo.vn với chất lượng cao nhất, kèm đầy đủ metadata."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(t("examples:", "ví dụ:") + "\n"
                "  python vtvgo-dl.py https://vtvgo.vn/watch/576y\n"
                "  python vtvgo-dl.py https://vtvgo.vn/watch/2E8I --episodes 1-10\n"
                "  python vtvgo-dl.py https://vtvgo.vn/page/thoi-su --limit 5\n"
                "  python vtvgo-dl.py --channels\n"
                "  python vtvgo-dl.py https://vtvgo.vn/channel/1 --epg --date yesterday\n"
                "  python vtvgo-dl.py https://vtvgo.vn/channel/1 --program \"Thời sự\" --date yesterday\n"
                "  python vtvgo-dl.py https://vtvgo.vn/channel/1 --duration 1h\n"
                "  python vtvgo-dl.py --login"))
    p.add_argument("inputs", nargs="*", metavar="URL",
                   help=t("vtvgo.vn link (watch / page / catalog / channel) or a video id",
                          "link vtvgo.vn (watch / page / catalog / channel) hoặc id video"))
    p.add_argument("-a", "--batch-file", help=t("read links from a text file (one per line)",
                                                "đọc link từ file văn bản (mỗi dòng một link)"))
    p.add_argument("-o", "--output", default=str(SCRIPT_DIR / "downloads"),
                   help=t("output folder (default: downloads)", "thư mục lưu (mặc định: downloads)"))
    p.add_argument("-q", "--quality", default="best",
                   help=t("best (default), worst, or a max height such as 720",
                          "best (mặc định), worst, hoặc chiều cao tối đa, vd 720"))
    p.add_argument("-F", "--list-formats", action="store_true",
                   help=t("only list the available qualities", "chỉ liệt kê các chất lượng có sẵn"))
    p.add_argument("-e", "--episodes", help=t("episodes to download, e.g. 1-5,8,10-",
                                              "các tập cần tải, vd 1-5,8,10-"))
    p.add_argument("--whole-series", action="store_true",
                   help=t("an episode link downloads its whole series", "link một tập sẽ tải cả bộ phim"))
    p.add_argument("-n", "--limit", type=int, help=t("max videos per list / section",
                                                     "số video tối đa mỗi danh sách / mục"))
    p.add_argument("-r", "--recursive", action="store_true",
                   help=t("also follow sub pages of a /page/ link", "tải cả các trang con của link /page/"))
    p.add_argument("--metadata-only", action="store_true",
                   help=t("save metadata and artwork, no video", "chỉ lưu metadata và ảnh, không tải video"))
    p.add_argument("--no-images", action="store_true", help=t("skip artwork", "không tải ảnh"))
    p.add_argument("--no-subs", action="store_true", help=t("skip subtitles", "không tải phụ đề"))
    p.add_argument("--keep-parts", action="store_true",
                   help=t("keep downloaded segments after joining", "giữ lại các đoạn sau khi ghép"))
    p.add_argument("-j", "--jobs", type=int, default=16,
                   help=t("parallel segment downloads (default 16)", "số đoạn tải song song (mặc định 16)"))
    p.add_argument("--no-aria2c", action="store_true", help=t("download with requests instead of aria2c",
                                                               "tải bằng requests thay vì aria2c"))
    p.add_argument("--proxy", default=os.environ.get("VTVGO_PROXY"),
                   help=t("HTTP/SOCKS proxy, e.g. a Vietnamese one for geo-blocked videos",
                          "proxy HTTP/SOCKS, vd proxy Việt Nam cho video bị chặn vùng"))
    live = p.add_argument_group(t("TV channels (https://vtvgo.vn/channel/<id>)", "Kênh truyền hình (https://vtvgo.vn/channel/<id>)"))
    live.add_argument("--channels", action="store_true", help=t("list every channel and its id",
                                                                "liệt kê mọi kênh và id"))
    live.add_argument("--epg", action="store_true", help=t("show the TV guide of the channel(s) for --date",
                                                           "xem lịch phát sóng của kênh theo --date"))
    live.add_argument("--date", help=t("day for --epg / --program: YYYY-MM-DD, DD/MM or yesterday (default today)",
                                       "ngày cho --epg / --program: YYYY-MM-DD, DD/MM hoặc yesterday (mặc định hôm nay)"))
    live.add_argument("-p", "--program", help=t("catch-up: program ids or title words (comma list), or 'all'",
                                                "xem lại: id chương trình hoặc từ trong tên (cách nhau dấu phẩy), hoặc 'all'"))
    live.add_argument("-d", "--duration", help=t("live: stop recording after e.g. 30m, 1h30m, 1:00:00 (default: Ctrl+C)",
                                                 "live: dừng ghi sau vd 30m, 1h30m, 1:00:00 (mặc định: bấm Ctrl+C)"))
    live.add_argument("--live-from-start", action="store_true",
                      help=t("live: start from the oldest segment the playlist still has, not the live edge",
                             "live: ghi từ đoạn cũ nhất playlist còn giữ, thay vì từ thời điểm hiện tại"))
    p.add_argument("--login", action="store_true",
                   help=t("open a browser to log in to VTVgo, and save the session",
                          "mở trình duyệt để đăng nhập VTVgo và lưu phiên"))
    p.add_argument("--logout", action="store_true", help=t("delete the saved session", "xoá phiên đã lưu"))
    p.add_argument("--session", default=str(SESSION_FILE), help=argparse.SUPPRESS)
    p.add_argument("--lang", choices=("en", "vi"), help=t("message language", "ngôn ngữ thông báo"))
    p.add_argument("--no-color", action="store_true", help=t("no colors", "tắt màu"))
    p.add_argument("-V", "--version", action="version", version=f"vtvgo-dl {__version__}")
    return p


def main() -> int:
    global LANG
    LANG = detect_lang(sys.argv[1:])
    if LANG not in ("en", "vi"):
        LANG = "en"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    opts = build_parser().parse_intermixed_args()   # links may come before or after options
    init_colors(not opts.no_color)
    session_path = Path(opts.session)

    if opts.logout:
        session_path.unlink(missing_ok=True)
        log(green(t("Logged out (session file deleted).", "Đã đăng xuất (đã xoá file phiên).")))
        return 0
    if opts.login:
        return login(session_path)

    inputs = list(opts.inputs)
    if opts.batch_file:
        try:
            inputs += [ln.strip() for ln in Path(opts.batch_file).read_text(encoding="utf-8-sig").splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        except OSError as e:
            log(red(str(e)))
            return 2
    if not inputs and not opts.channels:
        build_parser().print_help()
        return 2
    try:
        opts.episodes = parse_ranges(opts.episodes)
        opts.duration = parse_duration(opts.duration)
        vn_day(opts.date)
    except ValueError as e:
        log(red(t(f"Bad value: {e}", f"Giá trị sai: {e}")))
        return 2
    channel_inputs, vod_inputs = [], []
    for text in inputs:
        try:
            kind, value = parse_input(text)
        except ValueError:
            vod_inputs.append(text)   # reported later
            continue
        (channel_inputs if kind == "channel" else vod_inputs).append(value if kind == "channel" else text)
    opts.quality = str(opts.quality).lower().rstrip("p")
    opts.jobs = max(1, min(64, opts.jobs))

    ffmpeg = find_ffmpeg()
    aria2 = None if opts.no_aria2c else find_tool("aria2c")
    if aria2 and opts.proxy and opts.proxy.lower().startswith("socks"):
        aria2 = None   # aria2c has no SOCKS support: download with requests (needs  pip install pysocks)
    if not ffmpeg and not (opts.metadata_only or opts.list_formats):
        log(red(t("ffmpeg not found. Put ffmpeg.exe next to vtvgo-dl.py or  pip install imageio-ffmpeg",
                  "Không tìm thấy ffmpeg. Đặt ffmpeg.exe cạnh vtvgo-dl.py hoặc  pip install imageio-ffmpeg")))
        return 2

    token, device_id = load_session(session_path)
    if opts.channels or (opts.epg and channel_inputs and not vod_inputs):
        api = Api(token, opts.proxy, device_id)
        try:
            if opts.channels:
                list_channels(api)
            for value in channel_inputs:
                run_channel(Ctx(api, Http(opts.proxy), opts, Path(opts.output), None, ffmpeg, aria2), value, {})
        except VtvgoError as e:
            log(red(str(e)))
            return 1
        return 0
    root = Path(opts.output)
    root.mkdir(parents=True, exist_ok=True)
    log(bold_cyan(f"vtvgo-dl {__version__}") + gray(
        f"  {t('login', 'đăng nhập')}: {green(t('yes', 'có')) if token else t('no', 'không')}"
        f"  aria2c: {t('yes', 'có') if aria2 else t('no', 'không')}"
        f"  {t('quality', 'chất lượng')}: {opts.quality}"
        + (f"  proxy: {t('yes', 'có')}" if opts.proxy else "")))
    progress = Progress(root / "vtvgo-dl.db")
    ctx = Ctx(Api(token, opts.proxy, device_id), Http(opts.proxy), opts, root, progress, ffmpeg, aria2)
    counts = {"done": 0, "skip": 0, "error": 0}
    try:
        for value in channel_inputs:
            if STOP.is_set():
                break
            try:
                run_channel(ctx, value, counts)
            except VtvgoError as e:
                log(red(f"{value}: {e}"))
                counts["error"] += 1
        jobs = collect_jobs(ctx, vod_inputs) if vod_inputs and not STOP.is_set() else []
        if not jobs and not channel_inputs:
            log(yellow(t("Nothing to download.", "Không có gì để tải.")))
            return 1
        if jobs:
            log(bold(t(f"{len(jobs)} video(s) to process", f"{len(jobs)} video cần xử lý")))
        for n, job in enumerate(jobs, 1):
            if STOP.is_set():
                break
            try:
                counts[process(ctx, job, n, len(jobs))] += 1
            except VtvgoError as e:
                log(f"    {red('✗')} {e}")
                progress.mark(job.vod_id, "error", error=str(e))
                counts["error"] += 1
    except KeyboardInterrupt:
        STOP.set()
        status("")
        log(yellow(t("\nStopped. Run the same command again to resume.",
                     "\nĐã dừng. Chạy lại đúng lệnh này để tải tiếp.")))
    finally:
        failed = write_reports(progress, root)
        progress.close()
    log("")
    log(bold(t("Summary", "Tổng kết")) + f": {green(str(counts['done']))} {t('done', 'xong')}, "
        f"{counts['skip']} {t('skipped', 'bỏ qua')}, {red(str(counts['error'])) if counts['error'] else 0} "
        f"{t('failed', 'lỗi')}  →  {root}")
    if failed:
        log(gray(t(f"Failed links are listed in {root / 'failed.txt'}", f"Link lỗi được ghi trong {root / 'failed.txt'}")))
    if STOP.is_set():
        return 130   # stopped with Ctrl+C
    return 1 if counts["error"] and not counts["done"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        STOP.set()
        status("")
        sys.exit(130)
