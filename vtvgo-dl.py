#!/usr/bin/env python3
"""vtvgo-dl - download videos, series, whole pages, TV catch-up and live channels from vtvgo.vn
in the best quality, with full metadata.

How it works
------------
1. Resolve input     expand()            /watch/<id>, /page/<slug>, /catalog/node/<id>, /catalog/<id> or an id
                                         -> list of VOD ids (a series becomes all of its episodes).
2. Read metadata     Api.detail()        content/v21.0/get-detail-vod (title, description, cast, images...).
3. Find the stream   Api.vod_source()    live-channel/v21.0/playback/source {id, isVOD: true}.
4. Pick quality      list_variants()     every clear (non-DRM) HLS source is probed. A media playlist named
                                         index.m3u8 also gets its sibling master.m3u8 checked, which lists
                                         every rendition. The highest resolution / bitrate wins.
5. Download          download_stream()   segments in parallel (aria2c, or requests as fallback), resumable.
6. Mux               to_mp4()            ffmpeg joins the segments into one MP4 with tags, cover and
                                         subtitles, next to .info.json, .nfo (Kodi/Jellyfin) and the artwork.

TV channels (/channel/<id>) use the same playback/source call with {channelId, platform}:
   --epg             Api.epg()           display/v21.0/epg (programs with programID, start and end time).
   --program         run_catchup()       playback/source {channelId, programId, startTime, endTime}
                                         -> a finished playlist, downloaded like a video.
   live              record_live()       follows the live playlist, appends new segments to a .ts file,
                                         renews expired links, and remuxes to MP4 at the end. Livestream
                                         videos (/watch/<id> whose playlist has no end) are recorded too.
Logged-in channel requests need the device id that --login saves next to the token.

Privacy: the access token and device id only ever go to the API (never to the CDN), are never printed,
and are never written anywhere but vtvgo-session.json. Everything saved next to the videos goes through
scrub() / redact(), which drop user, device and session ids, signed-link tokens and IP addresses.

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
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

import requests

__version__ = "1.1.0"

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
MAX_PATH = 240          # stay below the classic Windows limit of 260 characters

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
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)   # VT processing
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


def log(msg: str = "") -> None:
    """Print a line above the status line."""
    with _print_lock:
        _clear_status()
        sys.stdout.flush()
        print(msg, flush=True)
        _draw_status()


def status(msg: str) -> None:
    """Set the bottom status line ("" clears it). Only shown in a terminal."""
    global _status_line
    with _print_lock:
        _clear_status()
        _status_line = msg
        _draw_status()


def warn(msg: str) -> str:
    return f"  {yellow('!')} {yellow(msg)}"


def fail_line(msg) -> str:
    return f"    {red('✗')} {msg}"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_clock(seconds: Optional[float]) -> str:
    """Seconds as [h:]mm:ss, "--:--" when unknown."""
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class Ticker:
    """Background thread that redraws the status line until the `with` block ends."""

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, interval: float = 0.25):
        self.interval = interval
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
        while not self._stop.wait(self.interval):
            self.frame = (self.frame + 1) % len(self.SPINNER)
            try:
                status(self.line())
            except Exception:
                pass

    def spinner(self) -> str:
        return cyan(self.SPINNER[self.frame]) if _COLOR else "-"

    def line(self) -> str:
        raise NotImplementedError


def progress_bar(frac: float, width: int) -> str:
    if _COLOR:
        halves = int(frac * width * 2)
        full, half = divmod(halves, 2)
        done = "━" * full + ("╸" if half else "")
        rest = "━" * (width - full - (1 if half else 0))
        return (green if frac >= 1 else cyan)(done) + gray(rest)
    full = int(frac * width)
    return "█" * full + "░" * (width - full)


class SegmentBar(Ticker):
    """Finished segments, downloaded bytes, speed and ETA of one video."""

    def __init__(self, files: list[Path]):
        super().__init__()
        self.files = files
        self.samples: list[tuple[float, int]] = []
        self.started = time.monotonic()

    def _scan(self) -> tuple[int, int]:
        done = size = 0
        for f in self.files:
            try:
                size += f.stat().st_size
                done += 1
            except OSError:
                try:
                    size += part_of(f).stat().st_size
                except OSError:
                    pass
        return done, size

    def line(self) -> str:
        now = time.monotonic()
        done, size = self._scan()
        total = max(1, len(self.files))
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, size)]
        t0, b0 = self.samples[0]
        speed = (size - b0) / (now - t0) if now - t0 >= 0.5 else 0.0
        frac = min(1.0, done / total)
        eta = (now - self.started) / frac * (1 - frac) if frac > 0.01 else None
        sep = gray(" │ ")
        tail = sep.join([bold(f"{frac * 100:5.1f}%"), f"{done}/{total} {t('seg', 'đoạn')}",
                         f"{fmt_bytes(size):>8}", cyan(f"{fmt_bytes(speed) + '/s':>10}"),
                         f"ETA {yellow(fmt_clock(eta))}"])
        prefix = f"  {self.spinner()} "
        bar_w = max(10, min(40, _width() - visible_len(prefix) - visible_len(tail) - 3))
        return f"{prefix}{progress_bar(frac, bar_w)}  {tail}"


# --------------------------------------------------------------------------- #
# Text, names and privacy helpers
# --------------------------------------------------------------------------- #

class VtvgoError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


# Set on Ctrl+C. Worker threads check it, and no new item is started once it is set.
STOP = threading.Event()

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Zero-width and bidi control characters, built from code points so none appears literally in this file.
_INVISIBLE = re.compile("[%s]" % "".join(chr(c) for c in (*range(0x200B, 0x2010), *range(0x202A, 0x202F),
                                                           *range(0x2066, 0x206A), 0xFEFF)))
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize(text, limit: int = 90) -> str:
    """Safe file / folder name, short enough for deep Windows paths."""
    text = _INVISIBLE.sub("", _BAD_CHARS.sub(" ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if len(text) > limit:
        text = text[:limit].rstrip(". ")
    if text.split(".")[0].upper() in _RESERVED:
        text = f"_{text}"
    return text or "untitled"


def fit_stem(folder: Path, stem: str, reserve: int = 30) -> str:
    """Shorten `stem` so folder/stem + the longest suffix we add (`reserve`) fits in MAX_PATH."""
    room = MAX_PATH - len(str(folder.resolve())) - 1 - reserve
    if len(stem) <= room:
        return stem
    tail = re.search(r"( - S\d+E\d+| \[[^\]]+\])$", stem)      # keep the episode number / id
    tail = tail.group(1) if tail else ""
    head = stem[: max(8, room - len(tail))].rstrip(". ")
    return head + tail


def fit_folder(root: Path, parts: tuple) -> Path:
    """root/parts..., dropping the outer page folders when the path would leave too little room for
    a file name (nested pages can be deep)."""
    parts = tuple(parts)
    while len(parts) > 1 and len(str(root.resolve().joinpath(*parts))) > MAX_PATH - 80:
        parts = parts[1:]
    return root.joinpath(*parts)


_TOKEN_PATH = re.compile(r"/[A-Za-z0-9_-]{16,}/\d{10}(?=/)")     # /<signature>/<expiry>/ in CDN paths
_SIGNED_QUERY = re.compile(r"(token|sig|hmac|hdnts|hdntl|policy|key-pair|expires|exp|auth|st|e|acl)=", re.I)
_MEDIA_PATH = re.compile(r"\.(m3u8|mpd|ts|m4s|mp4|aac|key|vtt)$", re.I)
_URL = re.compile(r"https?://[^\s\"'<>]+")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b")


def _redact_url(match: re.Match) -> str:
    url = match.group(0)
    base, _, query = url.partition("?")
    base = _TOKEN_PATH.sub("/...", base)
    if query and (_MEDIA_PATH.search(base) or _SIGNED_QUERY.search(query)):
        query = ""                                   # a signed stream link: the query is the signature
    return base + (f"?{query}" if query else "")


def redact(text) -> str:
    """Remove what could identify the user or the session from text that is shown or saved:
    signed-link tokens (in the path or the query string) and IP addresses."""
    text = _URL.sub(_redact_url, str(text))
    return _IP.sub("x.x.x.x", text)


_PRIVATE_KEY = re.compile(r"token|user|uid|phone|email|device|session|finger|kick|merchant|operator|"
                          r"(^|_)ip($|_)|ipaddr|address|client|geo|license", re.I)


def scrub(obj):
    """Copy of an API answer that is safe to save: keys about the user / device / session / network are
    dropped, and every string goes through redact()."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()
                if not _PRIVATE_KEY.search(str(k)) and k not in ("baseDrmInfo", "trackingData", "drmLicenses")}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def one_line(text, limit: int = 300) -> str:
    """Error text for videos.csv / failed.txt: redacted, on one line."""
    return re.sub(r"\s+", " ", redact(text)).strip()[:limit]


def parse_time(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(VN_TZ)
    except ValueError:
        return None


def original_image(url: Optional[str]) -> Optional[str]:
    """thumb.vtvdigital.org/transform?url=<original> -> <original> (full size, not re-encoded)."""
    if not url or not isinstance(url, str):
        return None
    q = parse_qs(urlparse(url).query)
    if "transform" in url and q.get("url"):
        return q["url"][0]
    return url


def parse_ranges(text: Optional[str]) -> Optional[set[int]]:
    """"1-5,8,10-" -> set of episode numbers (an open end means up to 9999)."""
    if not text:
        return None
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m and (m.group(1) or m.group(2)):
            lo, hi = int(m.group(1) or 1), int(m.group(2) or 9999)
            if hi < lo or hi - lo > 100_000:
                raise ValueError(part)
            out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(part)
    return out


def parse_duration(text: Optional[str]) -> Optional[float]:
    """"90m", "1h30m", "1h30", "1:30:00", "45:00", "5400" -> seconds."""
    if not text:
        return None
    text = text.strip().lower().replace(" ", "")
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part or 0)
        return total
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?(?:(\d+(?:\.\d+)?)s?)?", text)
    if not m or not any(m.groups()):
        raise ValueError(text)
    h, mi, s = m.groups()
    if h and not mi and s and not text.endswith("s"):      # "1h30" means 1 h 30 min
        mi, s = s, None
    return float(h or 0) * 3600 + float(mi or 0) * 60 + float(s or 0)


def names_of(items) -> list[str]:
    """API people / category lists hold strings or objects: return the names."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            it = it.get("name") or it.get("fullName") or it.get("title")
        if it:
            out.append(str(it))
    return out


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # e.g. the file is open in another program
            time.sleep(0.5 * (i + 1))
    os.replace(tmp, path)


def write_json(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Login session
# --------------------------------------------------------------------------- #

def load_session(path: Path) -> tuple[Optional[str], Optional[str]]:
    """(access token, device id) saved by --login. An expired token is reported and ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not token:
        return None, None
    exp = jwt_payload(token).get("exp")
    if exp and exp < time.time() + 60:
        log(warn(t("The saved login has expired. Run  python vtvgo-dl.py --login  again.",
                   "Phiên đăng nhập đã hết hạn. Chạy lại  python vtvgo-dl.py --login")))
        return None, None
    return token, data.get("deviceId")


def login(path: Path) -> int:
    """Open a real browser window where the user logs in by hand, then save the token and device id.
    The script never sees or stores the password."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log(red(t("--login needs Playwright:  pip install playwright", "--login cần Playwright:  pip install playwright")))
        return 2
    log(bold_cyan(t("A browser window opens on vtvgo.vn. Log in there yourself (phone number / password / QR).",
                    "Cửa sổ trình duyệt sẽ mở vtvgo.vn. Bạn tự đăng nhập ở đó (số điện thoại / mật khẩu / QR).")))
    log(gray(t("The window closes by itself once you are logged in (waits up to 10 minutes).",
               "Cửa sổ tự đóng khi đăng nhập xong (chờ tối đa 10 phút).")))
    token = device_id = None
    with sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = pw.chromium.launch(headless=False, channel=channel) if channel \
                    else pw.chromium.launch(headless=False)
                break
            except Exception:
                continue
        if browser is None:
            log(red(t("No browser found. Install Edge/Chrome or run:  python -m playwright install chromium",
                      "Không tìm thấy trình duyệt. Cài Edge/Chrome hoặc chạy:  python -m playwright install chromium")))
            return 2
        try:
            ctx = browser.new_context(locale="vi-VN")
            page = ctx.new_page()
            page.goto(SITE, wait_until="domcontentloaded")
            deadline = time.time() + 600
            while time.time() < deadline and not page.is_closed():
                cookies = {c["name"]: c["value"] for c in ctx.cookies(SITE)}
                try:
                    # Read only to tell a real login from a guest; never printed or saved.
                    store = json.loads(unquote(cookies.get("vtvgo.auth2.dataStore", "")) or "{}")
                except ValueError:
                    store = {}
                user = store.get("userInfo") if isinstance(store, dict) else None
                raw = unquote(cookies.get("vtvgo.auth2.token", ""))
                if raw and user and not user.get("isTempUser"):
                    token = raw
                    device_id = page.evaluate("() => localStorage.getItem('vtvgo.deviceId.v2')")
                    break
                time.sleep(1)
        except Exception:
            pass                           # the window was closed
        finally:
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
    write_json(path, data)
    try:
        os.chmod(path, 0o600)              # owner only (no effect on Windows)
    except OSError:
        pass
    log(bold_green(t(f"Logged in. Session saved to {path.name}", f"Đã đăng nhập. Lưu phiên vào {path.name}")))
    if exp:
        log(gray(t(f"Valid until {data['expiresAt']}", f"Hiệu lực đến {data['expiresAt']}")))
    log(gray(t("Keep this file private: it works like a password. --logout deletes it.",
               "Giữ file này riêng tư: nó có tác dụng như mật khẩu. --logout sẽ xoá nó.")))
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
    "PROGRAM_NOT_FOUND": ("program not available (too old for catch-up?)",
                          "chương trình không còn (quá hạn xem lại?)"),
}


def api_error(status: int, payload) -> VtvgoError:
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code") or f"HTTP_{status}")
    if code in ERROR_TEXT:
        en, vi = ERROR_TEXT[code]
        msg = f"{t(en, vi)} [{code}]"
    else:
        msg = f"{err.get('message') or code}" + (f" [{code}]" if err.get("message") else "")
    need = (err.get("data") or {}).get("requiredContentIds") if isinstance(err.get("data"), dict) else None
    if need:
        msg += f" ({t('package', 'gói')}: {', '.join(map(str, need))})"
    return VtvgoError(redact(msg), code)


class Api:
    """vtvgo.vn JSON API. The token (and device id) are only sent to these endpoints."""

    def __init__(self, token: Optional[str], proxy: Optional[str], device_id: Optional[str] = None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": f"{SITE}/", "Origin": SITE,
                               "Accept": "application/json", "Accept-Language": "vi"})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.token = token
        self.device_id = device_id
        self._details: dict[str, dict] = {}
        self._channels: Optional[dict] = None
        self._warned_device = False

    def request(self, method: str, url: str, auth: bool = False, **kw) -> dict:
        """JSON answer of a successful call; VtvgoError otherwise. Retries busy / failing servers."""
        last = ""
        for attempt in range(5):
            if STOP.is_set():
                raise VtvgoError(t("stopped", "đã dừng"))
            send_auth = auth and bool(self.token)
            headers = {"Authorization": f"Bearer {self.token}"} if send_auth else {}
            try:
                r = self.s.request(method, url, headers=headers, timeout=30, **kw)
            except requests.RequestException as e:
                last = redact(e)
                STOP.wait(2 * (attempt + 1))
                continue
            if r.status_code == 401 and send_auth:
                # The login expired or was revoked during the run: carry on as a guest.
                log(warn(t("The login was rejected (expired?). Continuing as a guest; run --login again.",
                           "Phiên đăng nhập bị từ chối (hết hạn?). Tiếp tục như khách; hãy chạy lại --login.")))
                self.token = None
                if isinstance(kw.get("json"), dict):
                    kw["json"] = {k: v for k, v in kw["json"].items() if k != "deviceId"}
                continue
            if r.status_code in (202, 429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                STOP.wait(min(30, wait or 2 * (attempt + 1)))
                continue
            try:
                payload = r.json()
            except ValueError:
                payload = {"error": {"code": f"HTTP_{r.status_code}"}}
            if r.status_code >= 400 or not isinstance(payload, dict) or payload.get("error"):
                raise api_error(r.status_code, payload)
            return payload
        raise VtvgoError(t(f"network error: {last}", f"lỗi mạng: {last}"))

    # ---- catalog
    def get(self, path: str, auth: bool = False, **params) -> dict:
        return self.request("GET", CONTENT + path, auth, params=params)

    def detail(self, vod_id: str) -> dict:
        if vod_id not in self._details:
            data = self.get("/get-detail-vod", auth=True, id=vod_id).get("data")
            if not isinstance(data, dict) or not data.get("id"):
                raise VtvgoError(t("not found", "không tìm thấy"), "VOD_NOT_FOUND")
            self._details[vod_id] = data
        return self._details[vod_id]

    def episodes(self, series: dict) -> list[dict]:
        """Every episode of a series (GROUP_VIDEO). The API wants episode ranges: walk them 50 at a time,
        and stop after two empty ranges past the known episode count."""
        total = as_int(series.get("currentEpisodeCount") or series.get("totalEpisode"))
        first = as_int((series.get("firstEpisode") or {}).get("episodeNumber"), 1) or 1
        out: dict[str, dict] = {}
        start, step, empty = max(1, first), 50, 0
        while start < 100_000 and empty < 2:
            try:
                data = self.get("/vod/episode", auth=True, id=series["id"],
                                startEpisodeNumber=start, endEpisodeNumber=start + step - 1).get("data")
            except VtvgoError as e:
                if e.code not in ("NOT_FOUND", "VOD_NOT_FOUND"):
                    raise
                data = []
            items = data.get("items", []) if isinstance(data, dict) else (data or [])
            items = [it for it in items if isinstance(it, dict) and it.get("id")]
            for it in items:
                out[it["id"]] = it
            start += step
            empty = 0 if items or start <= total else empty + 1
        return sorted(out.values(), key=lambda e: (as_int(e.get("episodeNumber")), str(e.get("id"))))

    def node(self, node_id: str, limit: Optional[int]) -> list[dict]:
        """VODs of a catalog node (newest first), 20 per page."""
        out, page = [], 1
        while page < 500:
            data = self.get("/vod/list-vod", nodeId=node_id, page=page, limit=20)
            items = [i for i in data.get("data") or [] if isinstance(i, dict) and i.get("id")]
            out += items
            if limit and len(out) >= limit:
                return out[:limit]
            if not items or not (data.get("meta") or {}).get("next"):
                break
            page += 1
        return out

    def page(self, page_id: str) -> tuple[str, list[dict]]:
        """Title and blocks of a /page/<slug-or-id>."""
        blocks, page, title = [], 1, None
        while page < 30:
            data = self.get("/section", id=page_id, limit=20, page=page)
            meta = data.get("meta") or {}
            title = title or (meta.get("parentPage") or {}).get("title")
            blocks += [b for b in data.get("data") or [] if isinstance(b, dict)]
            if not meta.get("next"):
                break
            page += 1
        return title or page_id, blocks

    def section(self, section_id: str) -> Optional[dict]:
        try:
            return self.get("/content-block", type="SECTION", id=section_id).get("data")
        except VtvgoError:
            return None

    # ---- playback
    def _playback(self, url: str, body: dict, channel: bool = False) -> dict:
        auth = bool(self.token)
        if auth and self.device_id:
            body = {**body, "deviceId": self.device_id}
        elif auth and channel:
            # Logged-in channel / catch-up calls must carry the device id saved by --login; a session
            # saved without one falls back to guest access.
            auth = False
            if not self._warned_device:
                self._warned_device = True
                log(warn(t("The saved login has no device id: channels play as a guest (catch-up limited to 24h, "
                           "paid channels unavailable). Run --login again.",
                           "Phiên đã lưu thiếu device id: kênh sẽ phát như khách (xem lại tối đa 24h, "
                           "không xem được kênh trả phí). Chạy lại --login.")))
        data = self.request("POST", url, auth, json=body).get("data")
        if not isinstance(data, dict):
            raise VtvgoError(t("no playable stream", "không có luồng phát"))
        return data

    def vod_source(self, vod_id: str) -> dict:
        return self._playback(PLAYBACK, {"id": vod_id, "isVOD": True})

    def channel_source(self, channel_id: str) -> dict:
        return self._playback(PLAYBACK, {"channelId": channel_id, "platform": PLATFORM}, channel=True)

    def catchup_source(self, channel_id: str, prog: dict) -> dict:
        return self._playback(PLAYBACK, {"channelId": channel_id, "programId": prog.get("programID"),
                                         "startTime": prog.get("startTime"), "endTime": prog.get("endTime"),
                                         "platform": PLATFORM}, channel=True)

    def live_session(self, channel_id: str, session_id: str) -> dict:
        return self._playback(LIVE_SESSION, {"sessionId": session_id, "channelId": channel_id,
                                             "platform": PLATFORM}, channel=True)

    # ---- channels
    def channels(self) -> dict:
        """id -> channel info (name, logo, catch-up hours...), with the name of its group."""
        if self._channels is None:
            out: dict = {}
            data = self.request("GET", f"{DISPLAY}/catalogs/with-channels", params={"platform": PLATFORM})
            for grp in data.get("data") or []:
                for ch in grp.get("channels") or []:
                    if isinstance(ch, dict) and ch.get("id") and ch["id"] not in out:
                        out[ch["id"]] = {**ch, "group": grp.get("name")}
            self._channels = out
        return self._channels

    def epg(self, channel_id: str, day: str) -> list[dict]:
        data = self.request("GET", f"{DISPLAY}/epg",
                            params={"channelIDs": channel_id, "from": day, "to": day}).get("data") or {}
        progs = [p for d in data.get(channel_id) or [] for p in d.get("programs") or [] if isinstance(p, dict)]
        seen, out = set(), []
        for p in sorted(progs, key=lambda p: p.get("startTime") or ""):
            key = (p.get("programID"), p.get("startTime"))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out


# --------------------------------------------------------------------------- #
# Input -> jobs
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    vod_id: str
    folder: tuple = ()          # sub folders for single videos (page title, section title)


def parse_input(text: str) -> tuple[str, str]:
    """(kind, value): kind is watch | page | node | section | channel. A channel value may end
    with "?<sessionId>" for a live session (restream)."""
    text = text.strip()
    m = re.fullmatch(r"(watch|page|node|section|catalog|channel)[:=](\S+)", text, re.I)
    if m:
        kind = m.group(1).lower()
        return ("section" if kind == "catalog" else kind), m.group(2)
    if re.fullmatch(r"\d{6,}", text):
        return "node", text
    if re.fullmatch(r"[A-Za-z0-9]{1,12}", text):
        return "watch", text
    u = urlparse(text if "://" in text else f"https://{text}")
    if u.hostname and not u.hostname.endswith("vtvgo.vn"):
        raise ValueError(text)
    parts = [unquote(p) for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "watch":
        return "watch", parts[1]
    if len(parts) >= 2 and parts[0] == "channel":
        sid = (parse_qs(u.query).get("sessionId") or [""])[0]
        return "channel", parts[1] + (f"?{sid}" if sid else "")
    if len(parts) >= 2 and parts[0] == "page":
        return "page", parts[1]
    if len(parts) >= 3 and parts[0] == "catalog" and parts[1] == "node":
        return "node", parts[2]
    if len(parts) >= 2 and parts[0] == "catalog":
        return "section", parts[1]
    raise ValueError(text)


def block_ids(api: Api, block: dict, limit: Optional[int]) -> list[str]:
    """VOD ids of one page block (a catalog node or a hand-picked list)."""
    extra = block.get("extraData") or {}
    kind = extra.get("contentType") or block.get("type")
    if kind == "vod" and extra.get("nodeId"):
        return [i["id"] for i in api.node(str(extra["nodeId"]), limit)]
    if kind == "manual":
        ids = []
        for it in (extra.get("filter") or {}).get("manualItems") or []:
            it = it.get("id") or it.get("vodID") if isinstance(it, dict) else it
            if it:
                ids.append(str(it))
        return ids[:limit] if limit else ids
    return []


def expand(api: Api, kind: str, value: str, opts) -> list[Job]:
    if kind == "watch":
        return [Job(value)]
    if kind == "node":
        log(gray(t(f"  Listing catalog node {value} ...", f"  Đang liệt kê mục {value} ...")))
        return [Job(i["id"], (sanitize(f"node {value}", 60),)) for i in api.node(value, opts.limit)]
    if kind == "section":
        block = api.section(value)
        if not block:
            raise VtvgoError(t("section not found", "không tìm thấy mục"))
        return [Job(i, (sanitize(block.get("title") or value, 60),)) for i in block_ids(api, block, opts.limit)]
    title, blocks = api.page(value)                                   # page
    log(f"  {t('Page', 'Trang')}: {bold(title)} — {len(blocks)} {t('blocks', 'khối')}")
    jobs: list[Job] = []
    seen = {value}
    queue = [((sanitize(title, 60),), blocks, 0)]
    skipped_sub = False
    while queue:
        folder, blks, depth = queue.pop(0)
        for b in blks:
            if b.get("type") == "nested-page":
                if not opts.recursive:
                    skipped_sub = True
                    continue
                for it in b.get("items") or []:
                    target = it.get("target") or {}
                    pid = target.get("sourceModeID") or target.get("slug")
                    if pid and pid not in seen and depth < 2:
                        seen.add(pid)
                        try:
                            sub_title, sub_blocks = api.page(pid)
                        except VtvgoError:
                            continue
                        queue.append((folder + (sanitize(sub_title, 60),), sub_blocks, depth + 1))
                continue
            ids = block_ids(api, b, opts.limit)
            if ids:
                name = sanitize(b.get("title") or b.get("id"), 60)
                log(gray(f"    • {name}: {len(ids)}"))
                jobs += [Job(i, folder + (name,)) for i in ids]
    if skipped_sub:
        log(gray(t("    (sub pages skipped, add --recursive to include them)",
                   "    (bỏ qua trang con, thêm --recursive để tải cả trang con)")))
    return jobs


def series_of(detail: dict) -> Optional[dict]:
    """The series a VOD belongs to (or the VOD itself when it is the series)."""
    if detail.get("type") == "GROUP_VIDEO":
        return detail
    pm = detail.get("parentMetadata")
    return pm if isinstance(pm, dict) and pm.get("id") and pm.get("title") else None


def collect_jobs(api: Api, inputs: list[tuple[str, str]], opts) -> list[Job]:
    """Expand every input. A series (or an episode with --whole-series) becomes all its episodes."""
    jobs: list[Job] = []
    for kind, value in inputs:
        try:
            found = expand(api, kind, value, opts)
        except VtvgoError as e:
            log(red(f"{value}: {e}"))
            continue
        for job in found:
            try:
                d = api.detail(job.vod_id)
            except VtvgoError:
                jobs.append(job)               # reported when the job runs
                continue
            series = d if d.get("type") == "GROUP_VIDEO" else (series_of(d) if opts.whole_series else None)
            if not series:
                jobs.append(job)
                continue
            total = series.get("currentEpisodeCount") or series.get("totalEpisode") or "?"
            log(f"  {t('Series', 'Phim bộ')}: {bold(series.get('title'))} — {total} {t('episodes', 'tập')}")
            try:
                eps = api.episodes(series)
            except VtvgoError as e:
                log(red(f"{series.get('title')}: {e}"))
                continue
            if opts.episodes:
                eps = [e for e in eps if as_int(e.get("episodeNumber")) in opts.episodes]
            jobs += [Job(str(e["id"]), job.folder) for e in eps]
    seen, uniq = set(), []
    for j in jobs:
        if j.vod_id not in seen:
            seen.add(j.vod_id)
            uniq.append(j)
    return uniq


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
    audio: Optional[str] = None       # separate audio playlist
    source: str = ""                  # CDN label

    @property
    def label(self) -> str:
        res = f"{self.width}x{self.height}" if self.height else t("unknown", "không rõ")
        bw = f"{self.bandwidth / 1e6:.2f} Mbps" if self.bandwidth else "?"
        return f"{res} {bw} {self.fps + 'fps ' if self.fps else ''}{self.codecs}".strip()

    def safe(self) -> dict:
        """For .info.json: without the signed parts of the links."""
        return scrub(self.__dict__)


def inherit_query(child: str, parent: str) -> str:
    """Child URL with the parent's query string, for CDNs that sign every request the same way."""
    q = urlparse(parent).query
    return child if not q or urlparse(child).query else f"{child}?{q}"


class Http:
    """Playlists, segments and images. Never carries the API token."""

    def __init__(self, proxy: Optional[str]):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": f"{SITE}/", "Origin": SITE})
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.proxy = proxy

    def text(self, url: str, parent: Optional[str] = None) -> tuple[str, str]:
        """(body, final URL). On 401/403 a child playlist is retried with its parent's query string."""
        last = ""
        for attempt in range(4):
            if STOP.is_set():
                raise VtvgoError(t("stopped", "đã dừng"))
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 200:
                    return r.text, r.url
                last = f"HTTP {r.status_code}"
                if r.status_code in (401, 403) and parent and inherit_query(url, parent) != url:
                    url = inherit_query(url, parent)
                    continue
                if r.status_code in (400, 401, 403, 404, 410):
                    break
            except requests.RequestException as e:
                last = redact(e)
            STOP.wait(1 + attempt)
        raise VtvgoError(redact(f"{url}: {last}"))


def parse_master(text: str, base: str, source: str = "") -> list[Variant]:
    lines = [ln.strip() for ln in text.splitlines()]
    audio: dict[str, str] = {}
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA:"):
            a = attrs(ln)
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                if a.get("GROUP-ID") not in audio or a.get("DEFAULT") == "YES":
                    audio[a.get("GROUP-ID", "")] = urljoin(base, a["URI"])
    out = []
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
        if not uri:
            continue
        a = attrs(ln)
        w, _, h = a.get("RESOLUTION", "0x0").lower().partition("x")
        out.append(Variant(urljoin(base, uri), as_int(a.get("BANDWIDTH") or a.get("AVERAGE-BANDWIDTH")),
                           as_int(w), as_int(h), a.get("FRAME-RATE", "").split(".")[0], a.get("CODECS", ""),
                           audio.get(a.get("AUDIO", "")), source))
    return out


def stream_urls(src: dict) -> list[tuple[str, str, str]]:
    """(url, manifest type, cdn) of every clear source, in the order the API gives them."""
    clear, drm_only = [], []
    for mode in src.get("sourceModes") or []:
        if not isinstance(mode, dict):
            continue
        for grp in mode.get("multiSource") or [mode]:
            drm = str((grp.get("drmInfo") or mode.get("drmInfo") or {}).get("drmType") or "none").lower()
            kind = grp.get("manifestType") or mode.get("manifestType") or ""
            for s in grp.get("sources") or []:
                if isinstance(s, dict) and s.get("url"):
                    (clear if drm in CLEAR_DRM else drm_only).append((s["url"], kind, s.get("cdnID") or ""))
    if src.get("sourceUrl"):
        clear.append((src["sourceUrl"], "", ""))
    seen, uniq = set(), []
    for item in clear:
        if item[0] not in seen:
            seen.add(item[0])
            uniq.append(item)
    if not uniq and drm_only:
        raise VtvgoError(t("protected by DRM (Widevine/FairPlay), cannot be downloaded",
                           "có DRM (Widevine/FairPlay), không tải được"), "DRM")
    return uniq


def list_variants(http: Http, src: dict) -> list[Variant]:
    """Every rendition of every clear source."""
    variants: list[Variant] = []
    for url, kind, cdn in stream_urls(src):
        path = urlparse(url).path.lower()
        if path.endswith(".mpd") or kind.lower() == "dash" or path.endswith(".mp4"):
            variants.append(Variant(url, source=cdn or "direct"))
            continue
        try:
            text, final = http.text(url)
        except VtvgoError:
            continue
        found = parse_master(text, final, cdn)
        if not found and urlparse(final).path.endswith("/index.m3u8"):
            # A single media playlist. The packager keeps the list of every rendition in master.m3u8
            # next to it, so look there before settling for this one stream.
            try:
                sib = final.split("?")[0].rsplit("/", 1)[0] + "/master.m3u8"
                mtext, mfinal = http.text(inherit_query(sib, final))
                found = parse_master(mtext, mfinal, cdn)
            except VtvgoError:
                found = []
        variants += found or [Variant(final, source=cdn)]
    return variants


def pick_variant(variants: list[Variant], quality: str = "best") -> Variant:
    if not variants:
        raise VtvgoError(t("no playable stream", "không có luồng phát"))
    ranked = sorted(variants, key=lambda v: (v.height, v.bandwidth), reverse=True)
    if quality == "worst":
        return ranked[-1]
    if quality.isdigit():
        fit = [v for v in ranked if v.height and v.height <= int(quality)]
        return fit[0] if fit else ranked[-1]
    return ranked[0]


@dataclass
class Segment:
    url: str
    dest: Path
    kind: str = "media"          # media | encrypted (media under an EXT-X-KEY) | key | init


@dataclass
class MediaPlaylist:
    local_text: str              # the playlist rewritten to point at the local files
    segments: list[Segment]
    byterange: bool = False
    live: bool = False


def plan_media(http: Http, variant: Variant, folder: Path, prefix: str, quality: str,
               url: Optional[str] = None) -> tuple[MediaPlaylist, Variant]:
    """Read a media playlist and plan the download of every segment, key and init section.
    Returns the plan and the variant really used (a "media" URL that turns out to be a master is
    resolved with the same --quality)."""
    url = url or variant.url
    text, final = http.text(url, parent=variant.url if url != variant.url else None)
    if "#EXT-X-STREAM-INF" in text:
        inner = pick_variant(parse_master(text, final, variant.source), quality)
        return plan_media(http, inner, folder, prefix, quality, inherit_query(inner.url, final)
                          if urlparse(final).query else inner.url)
    out, segs = [], []
    n_seg = n_key = 0
    encrypted = False
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith(("#EXT-X-KEY", "#EXT-X-MAP")):
            a = attrs(ln)
            if ln.startswith("#EXT-X-KEY"):
                encrypted = a.get("METHOD", "NONE").upper() != "NONE"
            if a.get("URI") and a.get("METHOD", "").upper() != "NONE":
                n_key += 1
                kind = "key" if ln.startswith("#EXT-X-KEY") else "init"
                dest = folder / f"{prefix}{n_key:03d}.{kind}"
                segs.append(Segment(urljoin(final, a["URI"]), dest, kind))
                ln = ln.replace(f'URI="{a["URI"]}"', f'URI="{dest.name}"')
            out.append(ln)
        elif ln.startswith("#"):
            out.append(ln)
        else:
            n_seg += 1
            ext = Path(urlparse(ln).path).suffix.lower()
            ext = ext if ext in (".ts", ".m4s", ".mp4", ".aac", ".m4a") else ".ts"
            dest = folder / f"{prefix}{n_seg:05d}{ext}"
            segs.append(Segment(urljoin(final, ln), dest, "encrypted" if encrypted else "media"))
            out.append(dest.name)
    if not n_seg:
        raise VtvgoError(t("empty playlist", "danh sách phát rỗng"))
    live = "#EXT-X-ENDLIST" not in text and "#EXT-X-PLAYLIST-TYPE:VOD" not in text
    return MediaPlaylist("\n".join(out) + "\n", segs, "#EXT-X-BYTERANGE" in text, live), variant


# --------------------------------------------------------------------------- #
# Segment download
# --------------------------------------------------------------------------- #

def part_of(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def looks_valid(data_head: bytes, kind: str, suffix: str) -> bool:
    """Reject error pages that came back with status 200. Keys and encrypted segments are random
    bytes, so only plain segments get the format check."""
    if kind in ("key", "encrypted"):
        return True
    head = data_head.lstrip()[:1]
    if head in (b"<", b"{"):
        return False
    if suffix == ".ts":
        return data_head[:1] == b"G"          # MPEG-TS sync byte
    return True


def segment_ok(seg: Segment) -> bool:
    """A finished segment: renamed from .part only after a complete download."""
    try:
        if seg.dest.stat().st_size == 0:
            return False
        with open(seg.dest, "rb") as fh:
            return looks_valid(fh.read(16), seg.kind, seg.dest.suffix)
    except OSError:
        return False


def download_aria2(aria2: str, segs: list[Segment], opts) -> None:
    """One aria2c pass. Files are written as .part and renamed only once aria2c reports them done
    (they are no longer listed in its saved session), so a killed run never leaves a cut segment."""
    todo = [s for s in segs if not segment_ok(s)]
    if not todo:
        return
    lines = []
    for s in todo:
        for leftover in (s.dest, part_of(s.dest), Path(f"{part_of(s.dest)}.aria2")):
            leftover.unlink(missing_ok=True)
        lines += [s.url, f"  dir={s.dest.parent}", f"  out={part_of(s.dest).name}"]
        if opts.proxy:
            lines.append(f"  all-proxy={opts.proxy}")      # per download, so not on the command line
    with tempfile.TemporaryDirectory(prefix="vtvgo-") as tmp:
        session = Path(tmp) / "aria2.session"
        cmd = [aria2, "--input-file=-", f"--max-concurrent-downloads={opts.jobs}",
               "--max-connection-per-server=1", "--split=1", "--continue=false", "--allow-overwrite=true",
               "--auto-file-renaming=false", "--file-allocation=none", "--max-tries=6", "--retry-wait=2",
               "--timeout=30", "--connect-timeout=15", "--quiet=true", f"--save-session={session}",
               f"--user-agent={UA}", f"--referer={SITE}/", f"--header=Origin: {SITE}"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data: Optional[bytes] = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            while True:            # short waits, so Ctrl+C is noticed on Windows too
                try:
                    proc.communicate(data, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    data = None
        except KeyboardInterrupt:
            STOP.set()
            proc.terminate()
            proc.wait()
            raise
        unfinished = set()
        try:
            unfinished = set(re.findall(r"^\s*out=(.+)$", session.read_text(encoding="utf-8"), re.M))
        except OSError:
            pass
    for s in todo:
        part = part_of(s.dest)
        if part.name in unfinished or Path(f"{part}.aria2").exists() or not part.is_file():
            continue
        os.replace(part, s.dest)
        if not segment_ok(s):
            s.dest.unlink(missing_ok=True)


def download_requests(http: Http, segs: list[Segment], jobs: int) -> None:
    def one(seg: Segment) -> None:
        if STOP.is_set() or segment_ok(seg):
            return
        part = part_of(seg.dest)
        for attempt in range(6):
            if STOP.is_set():
                break
            try:
                with http.s.get(seg.url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    expected = as_int(r.headers.get("Content-Length"), -1)
                    got = 0
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            if STOP.is_set():
                                raise KeyboardInterrupt
                            fh.write(chunk)
                            got += len(chunk)
                if expected in (-1, got) and got > 0:
                    os.replace(part, seg.dest)
                    if segment_ok(seg):
                        return
                    seg.dest.unlink(missing_ok=True)
            except KeyboardInterrupt:
                break
            except (requests.RequestException, OSError):
                pass
            STOP.wait(1 + attempt)
        part.unlink(missing_ok=True)

    pool = ThreadPoolExecutor(jobs)
    try:
        futures = [pool.submit(one, s) for s in segs]
        for fut in futures:
            while not fut.done():
                time.sleep(0.1)
    except KeyboardInterrupt:
        STOP.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=STOP.is_set())


def download_segments(http: Http, segs: list[Segment], opts, aria2: Optional[str]) -> None:
    for attempt in range(3):
        todo = [s for s in segs if not segment_ok(s)]
        if not todo or STOP.is_set():
            break
        if aria2 and attempt < 2:
            download_aria2(aria2, todo, opts)
        else:
            download_requests(http, todo, opts.jobs)
    if STOP.is_set():
        raise KeyboardInterrupt
    todo = [s for s in segs if not segment_ok(s)]
    if todo:
        raise VtvgoError(t(f"{len(todo)} segment(s) failed to download", f"{len(todo)} đoạn tải không được"))


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

def run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    """(ok, last lines of stderr, redacted: ffmpeg quotes the URLs it reads)."""
    if STOP.is_set():
        raise KeyboardInterrupt
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        return False, str(e)
    if p.returncode and STOP.wait(0.5):
        raise KeyboardInterrupt
    return p.returncode == 0, one_line(p.stderr.decode("utf-8", "replace")[-600:])


@dataclass
class MuxInput:
    path: str                     # local file or URL
    hls: bool = False             # a local HLS playlist (needs the extension whitelist)
    remote: bool = False          # read over the network (headers, proxy)


def to_mp4(ffmpeg: str, video: MuxInput, out: Path, *, audio: Optional[MuxInput] = None,
           cover: Optional[Path] = None, subs: Optional[list[tuple[Path, str]]] = None,
           tags: Optional[dict] = None, proxy: Optional[str] = None) -> None:
    """Join / remux into `out` with tags, cover art and subtitles. If a cover or subtitle makes
    ffmpeg fail, it tries again without it."""
    subs = subs or []
    tmp = out.with_name(out.stem + ".tmp.mp4")

    def source(inp: MuxInput) -> list[str]:
        args = []
        if inp.hls:
            args += ["-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto,data"]
        if inp.remote:
            args += ["-user_agent", UA, "-headers", f"Referer: {SITE}/\r\n"]
            if proxy and proxy.lower().startswith("http"):
                args += ["-http_proxy", proxy]
        return args + ["-i", inp.path]

    attempts = []
    for with_cover, with_subs in ((True, True), (False, True), (False, False)):
        a = (with_cover and cover is not None and cover.suffix == ".jpg", with_subs and bool(subs))
        if a not in attempts:
            attempts.append(a)
    last = ""
    for use_cover, use_subs in attempts:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *source(video)]
        maps, idx = ["-map", "0:v:0"], 1
        if audio:
            cmd += source(audio)
            maps += ["-map", f"{idx}:a:0"]
            idx += 1
        else:
            maps += ["-map", "0:a?"]
        if use_cover:
            cmd += ["-i", str(cover)]
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
        if use_cover:
            cmd += ["-disposition:v:1", "attached_pic"]
        cmd += ["-movflags", "+faststart"]
        for k, v in (tags or {}).items():
            cmd += ["-metadata", f"{k}={v}"]
        cmd.append(str(tmp))
        ok, err = run_ffmpeg(cmd)
        if ok and tmp.is_file() and tmp.stat().st_size > 0:
            os.replace(tmp, out)
            return
        last = err
        tmp.unlink(missing_ok=True)
    raise VtvgoError(f"ffmpeg: {last}")


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #

def nfo(root: str, fields: list[tuple[str, object]], actors: Optional[list[str]] = None,
        uid: Optional[str] = None) -> str:
    """Kodi / Jellyfin .nfo. Empty values are left out; a list value repeats the tag."""
    body = ""
    for tag, value in fields:
        for v in value if isinstance(value, list) else [value]:
            if v not in (None, "", 0):
                body += f"  <{tag}>{xml_escape(str(v))}</{tag}>\n"
    if uid:
        body += f'  <uniqueid type="vtvgo" default="true">{xml_escape(uid)}</uniqueid>\n'
    for a in actors or []:
        body += f"  <actor>\n    <name>{xml_escape(a)}</name>\n  </actor>\n"
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<{root}>\n{body}</{root}>\n'


def released(detail: dict) -> Optional[datetime]:
    return parse_time(detail.get("availableDate") or detail.get("createdAt"))


def episode_title(detail: dict, series: Optional[dict]) -> str:
    """"Series | Tập 5" -> "Tập 5" inside a series."""
    title = str(detail.get("title") or "")
    if series and "|" in title:
        return title.split("|", 1)[1].strip() or title
    return title


def people(detail: dict, series: Optional[dict], key: str) -> list[str]:
    return names_of(detail.get(key)) or names_of((series or {}).get(key))


def vod_nfo(detail: dict, series: Optional[dict], thumb: Optional[str]) -> str:
    when = released(detail)
    fields = [("title", episode_title(detail, series) if series else detail.get("title"))]
    if series:
        fields += [("showtitle", series.get("title")),
                   ("season", as_int(detail.get("seasonNumber") or series.get("seasonNumber"), 1) or 1),
                   ("episode", as_int(detail.get("episodeNumber")))]
    fields += [("plot", detail.get("description")),
               ("runtime", round(as_int(detail.get("durationSecond")) / 60)),
               ("aired" if series else "premiered", when.strftime("%Y-%m-%d") if when else None),
               ("year", detail.get("year") or (when.year if when else None)),
               ("mpaa", detail.get("ageRating")), ("country", detail.get("nation")), ("studio", "VTVgo"),
               ("director", people(detail, series, "directors")),
               ("genre", names_of(detail.get("categories"))),
               ("tag", names_of(detail.get("terms"))),
               ("thumb", thumb),
               ("dateadded", datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"))]
    return nfo("episodedetails" if series else "movie", fields, people(detail, series, "actors"), str(detail["id"]))


def tvshow_nfo(series: dict) -> str:
    when = parse_time(series.get("availableDate"))
    return nfo("tvshow", [("title", series.get("title")), ("plot", series.get("description")),
                          ("premiered", when.strftime("%Y-%m-%d") if when else None),
                          ("year", series.get("year") or (when.year if when else None)),
                          ("mpaa", series.get("ageRating")), ("studio", "VTVgo"),
                          ("director", names_of(series.get("directors")))],
               names_of(series.get("actors")), str(series.get("id")))


def vod_tags(detail: dict, series: Optional[dict]) -> dict:
    when = released(detail)
    ep = as_int(detail.get("episodeNumber"))
    tags = {
        "title": episode_title(detail, series) if series else detail.get("title"),
        "description": str(detail.get("description") or "")[:250],
        "synopsis": detail.get("description"),
        "comment": f"{SITE}/watch/{detail['id']}",
        "date": when.strftime("%Y-%m-%d") if when else None,
        "artist": ", ".join(people(detail, series, "directors")) or "VTVgo",
        "composer": ", ".join(people(detail, series, "actors")[:20]),
        "genre": ", ".join(names_of(detail.get("categories"))),
        "keywords": ", ".join(names_of(detail.get("terms"))),
        "network": "VTVgo",
        "publisher": "VTVgo",
        "episode_id": detail["id"],
    }
    if series:
        tags.update({"show": series.get("title"), "album": series.get("title"),
                     "episode_sort": str(ep or ""), "track": str(ep or ""),
                     "season_number": str(as_int(detail.get("seasonNumber") or series.get("seasonNumber"), 1) or 1),
                     "media_type": "10"})               # TV show (iTunes stik)
    return {k: v for k, v in tags.items() if v}


def fetch_image(http: Http, ffmpeg: Optional[str], url: Optional[str], stem: Path) -> Optional[Path]:
    """The original image, saved as JPEG (which the MP4 cover needs). Skipped if already there."""
    url = original_image(url)
    if not url or not url.startswith("http"):
        return None
    jpg = stem.with_suffix(".jpg")
    if jpg.is_file() and jpg.stat().st_size > 0:
        return jpg
    try:
        r = http.s.get(url, timeout=30)
        r.raise_for_status()
        if not r.content or r.content.lstrip()[:1] == b"<":
            return None
        ext = Path(urlparse(url).path).suffix.lower() or ".img"
        if ext in (".jpg", ".jpeg"):
            jpg.write_bytes(r.content)
            return jpg
        raw = stem.with_name(stem.name + ".src" + ext)
        raw.write_bytes(r.content)
    except (requests.RequestException, OSError):
        return None
    if ffmpeg:
        p = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                            "-frames:v", "1", "-q:v", "2", str(jpg)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if p.returncode == 0 and jpg.is_file():
            raw.unlink(missing_ok=True)
            return jpg
    final = stem.with_suffix(ext)
    os.replace(raw, final)
    return final


ISO3 = {"vi": "vie", "vn": "vie", "vie": "vie", "en": "eng", "eng": "eng", "zh": "zho", "ko": "kor", "ja": "jpn"}


def subtitle_tracks(detail: dict, src: dict) -> list[tuple[str, str]]:
    """(url, ISO 639-2 language) of the subtitles the API lists, in any of the shapes it uses."""
    out: dict[str, str] = {}
    for coll in (detail.get("subTitles"), detail.get("subtitles"), src.get("subTitles"), src.get("subtitles")):
        for s in coll or []:
            if isinstance(s, str):
                url, lang = s, "vi"
            elif isinstance(s, dict):
                url = next((s[k] for k in ("url", "link", "file", "src", "path") if isinstance(s.get(k), str)), "")
                lang = next((str(s[k]) for k in ("language", "lang", "code", "languageCode", "label", "name")
                             if s.get(k)), "und")
            else:
                continue
            if url.startswith("http") and url not in out:
                low = lang.lower()
                out[url] = ISO3.get(low[:3], ISO3.get(low[:2], "und"))
    return list(out.items())


# --------------------------------------------------------------------------- #
# Progress database and reports
# --------------------------------------------------------------------------- #

FINISHED = ("done", "recorded")


class Progress:
    """SQLite record of every item, so a re-run skips finished videos."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, status TEXT, file TEXT, "
                        "title TEXT, series TEXT, episode INTEGER, date TEXT, duration INTEGER, "
                        "quality TEXT, size INTEGER, error TEXT, updated TEXT)")
        self.db.commit()

    def done(self, key: str, root: Path) -> Optional[str]:
        """File of an item finished in an earlier run, if that file still exists."""
        with self.lock:
            row = self.db.execute("SELECT status, file FROM videos WHERE id = ?", (key,)).fetchone()
        if row and row[0] == "done" and row[1] and (root / row[1]).exists():
            return row[1]
        return None

    def owner(self, file: str) -> Optional[str]:
        """Id of the item that a file name already belongs to."""
        with self.lock:
            row = self.db.execute("SELECT id FROM videos WHERE file = ? OR file LIKE ?",
                                  (file, file.rsplit(".", 1)[0] + ".%")).fetchone()
        return row[0] if row else None

    def mark(self, key: str, state: str, **kw) -> None:
        with self.lock:
            old = self.db.execute("SELECT status FROM videos WHERE id = ?", (key,)).fetchone()
            if old and state == "metadata" and old[0] in FINISHED:
                state = old[0]                 # --metadata-only never undoes a finished download
                kw.pop("file", None)
            if "error" in kw and kw["error"]:
                kw["error"] = one_line(kw["error"])
            cols = ["status", "updated", *kw]
            vals = [state, datetime.now(VN_TZ).isoformat(timespec="seconds"), *kw.values()]
            if old:
                self.db.execute(f"UPDATE videos SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                                [*vals, key])
            else:
                self.db.execute(f"INSERT INTO videos (id, {', '.join(cols)}) VALUES ({', '.join('?' * (len(cols) + 1))})",
                                [key, *vals])
            self.db.commit()

    def rows(self) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT id, status, file, title, series, episode, date, duration, quality, "
                                   "size, error FROM videos ORDER BY series, episode, date, id").fetchall()

    def close(self) -> None:
        with self.lock:
            self.db.close()


def item_url(key: str) -> str:
    """Progress key -> link. Keys are VOD ids, "catchup:<channel>:<program>:<start>" or "live:<channel>:<time>"."""
    kind, _, rest = key.partition(":")
    if kind in ("catchup", "live") and rest:
        return f"{SITE}/channel/{rest.split(':')[0]}"
    return f"{SITE}/watch/{key}"


def item_input(key: str) -> str:
    """Progress key -> a line `-a` can read again (catch-up keeps its program id)."""
    kind, _, rest = key.partition(":")
    if kind == "catchup":
        ch, _, prog = rest.partition(":")
        return f"channel:{ch}  --program {prog.split(':')[0]}"
    return item_url(key)


def write_reports(progress: Progress, root: Path) -> int:
    """videos.csv with every item, failed.txt with the failures. Returns the number of failures."""
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
        log(warn(t("videos.csv is open in another program, not updated",
                   "videos.csv đang mở ở chương trình khác, chưa cập nhật")))
        tmp.unlink(missing_ok=True)
    failed = [r for r in rows if r[1] == "error" and not r[0].startswith("live:")]
    path = root / "failed.txt"
    if failed:
        lines = [t("# Retry with:  python vtvgo-dl.py -a failed.txt", "# Thử lại:  python vtvgo-dl.py -a failed.txt")]
        lines += [f"{item_input(r[0])}  # {one_line(r[3] or '', 80)} — {r[10] or ''}" for r in failed]
        write_atomic(path, "\n".join(lines) + "\n")
    else:
        path.unlink(missing_ok=True)
    return len(failed)


def read_batch(path: Path) -> list[str]:
    """Links from a text file: one per line, "#" starts a comment (as in failed.txt)."""
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        if line:
            out.append(line)
    return out


# --------------------------------------------------------------------------- #
# Download one stream
# --------------------------------------------------------------------------- #

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
    claimed: dict = field(default_factory=dict)         # output file -> id, within this run


class LiveStream(Exception):
    """The playlist has no end: record it instead."""


def stream_signature(variant: Variant) -> str:
    """Identifies a rendition across runs (the signed parts of the link change, the rest does not)."""
    path = urlparse(redact(variant.url)).path
    return f"{variant.width}x{variant.height} {variant.bandwidth} {'/'.join(path.split('/')[-3:])}"


def download_stream(ctx: Ctx, variant: Variant, out: Path, *, cover: Optional[Path] = None,
                    subs: Optional[list] = None, tags: Optional[dict] = None) -> None:
    """Download a finished (VOD) stream into `out`. Raises LiveStream when the playlist turns out to be live."""
    work = out.with_name(out.stem + ".parts")
    # Segments are numbered, not named after the stream: if an unfinished earlier run used another
    # rendition (other --quality or CDN), start over instead of mixing parts of both.
    marker = work / "stream.txt"
    sig = stream_signature(variant)
    try:
        if work.is_dir() and marker.read_text(encoding="utf-8") != sig:
            shutil.rmtree(work, ignore_errors=True)
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
    if ".m3u8" in urlparse(variant.url).path:
        work.mkdir(parents=True, exist_ok=True)
        marker.write_text(sig, encoding="utf-8")
        vplan, variant = plan_media(ctx.http, variant, work, "v", ctx.opts.quality)
        if vplan.live:
            shutil.rmtree(work, ignore_errors=True)
            raise LiveStream()
        aplan = None
        if variant.audio:
            aplan, _ = plan_media(ctx.http, Variant(variant.audio), work, "a", ctx.opts.quality)
        if not (vplan.byterange or (aplan and aplan.byterange)):
            (work / "video.m3u8").write_text(vplan.local_text, encoding="utf-8")
            if aplan:
                (work / "audio.m3u8").write_text(aplan.local_text, encoding="utf-8")
            segs = vplan.segments + (aplan.segments if aplan else [])
            with SegmentBar([s.dest for s in segs]):
                download_segments(ctx.http, segs, ctx.opts, ctx.aria2)
            status(t("  joining with ffmpeg ...", "  đang ghép bằng ffmpeg ..."))
            try:
                to_mp4(ctx.ffmpeg, MuxInput(str(work / "video.m3u8"), hls=True), out,
                       audio=MuxInput(str(work / "audio.m3u8"), hls=True) if aplan else None,
                       cover=cover, subs=subs, tags=tags)
            finally:
                status("")
            if not ctx.opts.keep_parts:
                shutil.rmtree(work, ignore_errors=True)
            return
    # DASH, a single MP4 or byte-range HLS: let ffmpeg read the stream itself.
    status(t("  downloading with ffmpeg ...", "  đang tải bằng ffmpeg ..."))
    try:
        to_mp4(ctx.ffmpeg, MuxInput(variant.url, remote=True), out,
               audio=MuxInput(variant.audio, remote=True) if variant.audio else None,
               cover=cover, tags=tags, proxy=ctx.opts.proxy)
    finally:
        status("")
    shutil.rmtree(work, ignore_errors=True)


EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")


def closest(variants: list[Variant], height: int, quality: str) -> Variant:
    same = [v for v in variants if v.height == height]
    return pick_variant(same or variants, "best" if same else quality)


class LiveBar(Ticker):
    def __init__(self, limit: Optional[float]):
        super().__init__(0.5)
        self.limit = limit
        self.recorded = 0.0
        self.size = 0
        self.gaps = 0
        self.res = ""

    def line(self) -> str:
        dot = red("●") if self.frame % 2 else gray("●")
        sep = gray(" │ ")
        goal = f" / {fmt_clock(self.limit)}" if self.limit else ""
        gaps = f"{sep}{yellow(str(self.gaps) + ' gap')}" if self.gaps else ""
        return f"  {dot} {bold('REC')} {fmt_clock(self.recorded)}{goal}{sep}{fmt_bytes(self.size)}{sep}{self.res}{gaps}"


def record_live(ctx: Ctx, variant: Variant, refresh: Callable[[], list[Variant]], folder: Path, stem: str,
                tags: dict, cover: Optional[Path]) -> Path:
    """Follow a live HLS playlist, append each new segment to a .ts file, then remux it to MP4.

    Stops after --duration, when the stream ends, on Ctrl+C, or when the stream stops answering; what
    was recorded is kept in every case. Expired links are renewed with `refresh` (a new playback/source
    call) at the same resolution."""
    if variant.audio:
        raise VtvgoError(t("live streams with separate audio are not supported",
                           "chưa hỗ trợ luồng trực tiếp có âm thanh tách riêng"))
    started = datetime.now(VN_TZ)
    name = fit_stem(folder, f"{stem} [{started:%Y%m%d-%H%M%S}]")
    ts_path, out = folder / f"{name}.ts", folder / f"{name}.mp4"
    media_url, height = variant.url, variant.height
    bar = LiveBar(ctx.opts.duration)
    bar.res = f"{variant.width}x{variant.height}" if variant.height else ""
    last_seq: Optional[int] = None
    failures = 0
    problem: Optional[str] = None
    stopped = False
    pool = ThreadPoolExecutor(4)
    how_long = fmt_clock(bar.limit) if bar.limit else t("until Ctrl+C", "đến khi bấm Ctrl+C")
    log(gray(t(f"    recording {how_long} → {ts_path.name}", f"    đang ghi {how_long} → {ts_path.name}")))

    def fetch(url: str) -> bytes:
        for attempt in range(4):
            try:
                r = ctx.http.s.get(url, timeout=30)
                if r.status_code == 200 and r.content and looks_valid(r.content[:16], "media", ".ts"):
                    return r.content
            except requests.RequestException:
                pass
            if STOP.wait(1 + attempt):
                break
        return b""

    try:
        with open(ts_path, "wb") as fh, bar:
            while not STOP.is_set():
                try:
                    text, final = ctx.http.text(media_url)
                    failures = 0
                except VtvgoError:
                    failures += 1
                    if failures > 6:
                        problem = t("the live stream stopped answering", "luồng trực tiếp không phản hồi")
                        break
                    try:                         # signed link expired: ask the API for a new one
                        fresh = closest(refresh(), height, ctx.opts.quality)
                        media_url, height = fresh.url, fresh.height or height
                        last_seq = -1            # sequence numbers may restart on a new link
                    except VtvgoError:
                        STOP.wait(3)
                    continue
                if "#EXT-X-STREAM-INF" in text:
                    best = closest(parse_master(text, final), height, ctx.opts.quality)
                    media_url, height = best.url, best.height or height
                    bar.res = f"{best.width}x{best.height}" if best.height else bar.res
                    continue
                if re.search(r"#EXT-X-KEY:.*METHOD=(?!NONE)", text):
                    problem = t("encrypted live stream, not supported", "luồng trực tiếp mã hoá, chưa hỗ trợ")
                    break
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
                elif last_seq == -1 or (segs and segs[-1][0] < last_seq):
                    new = segs[-1:]              # new link / restarted numbering: carry on from the edge
                else:
                    new = [s for s in segs if s[0] > last_seq]
                    if new and new[0][0] > last_seq + 1:
                        bar.gaps += 1
                futures = [pool.submit(fetch, s[2]) for s in new]
                for (sq, d, _), fut in zip(new, futures):
                    while not fut.done():
                        time.sleep(0.05)
                    data = fut.result()
                    if data:
                        fh.write(data)
                        bar.size += len(data)
                        bar.recorded += d
                    else:
                        bar.gaps += 1
                    last_seq = sq
                    if bar.limit and bar.recorded >= bar.limit:
                        break
                fh.flush()
                if (bar.limit and bar.recorded >= bar.limit) or "#EXT-X-ENDLIST" in text:
                    break
                target = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
                STOP.wait(max(1.0, (float(target.group(1)) if target else 4.0) / 2))
    except KeyboardInterrupt:
        stopped = True
        log(yellow(t("    stopping the recording ...", "    đang dừng ghi ...")))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        status("")
    if bar.size == 0:
        ts_path.unlink(missing_ok=True)
        raise VtvgoError(problem or t("nothing was recorded", "không ghi được gì"))
    if problem:
        log(warn(problem))
    if bar.gaps:
        log(warn(t(f"{bar.gaps} gap(s) in the recording (network too slow?)",
                   f"bản ghi bị hụt {bar.gaps} chỗ (mạng chậm?)")))
    status(t("  joining with ffmpeg ...", "  đang ghép bằng ffmpeg ..."))
    was_stopped = STOP.is_set()
    STOP.clear()                    # let ffmpeg run even after Ctrl+C: the recording must be saved
    try:
        to_mp4(ctx.ffmpeg, MuxInput(str(ts_path)), out, cover=cover,
               tags={**tags, "date": started.strftime("%Y-%m-%d")})
        if not ctx.opts.keep_parts:
            ts_path.unlink(missing_ok=True)
    except VtvgoError:
        log(warn(t(f"could not convert to MP4, kept {ts_path.name}", f"không chuyển được sang MP4, giữ {ts_path.name}")))
        out = ts_path
    finally:
        status("")
        if stopped or was_stopped:
            STOP.set()              # Ctrl+C ends the whole run, after the recording is saved
    log(gray(t(f"    recorded {fmt_clock(bar.recorded)}", f"    đã ghi {fmt_clock(bar.recorded)}")))
    return out


# --------------------------------------------------------------------------- #
# Videos (VOD)
# --------------------------------------------------------------------------- #

def vod_stem(ctx: Ctx, detail: dict, series: Optional[dict], folder: Path) -> str:
    """File name (without extension). Series episodes are "Series - S01E05", single videos
    "YYYY-MM-DD Title [id]". An episode name already taken by another id gets the id appended."""
    vid = str(detail["id"])
    if series:
        ep = as_int(detail.get("episodeNumber"))
        season = as_int(detail.get("seasonNumber") or series.get("seasonNumber"), 1) or 1
        base = sanitize(series.get("title") or detail.get("title"), 80)
        stem = f"{base} - S{season:02d}E{ep:02d}" if ep else f"{base} - {sanitize(detail.get('title'), 60)} [{vid}]"
    else:
        when = released(detail)
        stem = f"{when:%Y-%m-%d} " if when else ""
        stem += f"{sanitize(detail.get('title'), 90)} [{vid}]"
    stem = fit_stem(folder, stem)
    rel = str((folder / f"{stem}.mp4").relative_to(ctx.root))
    taken = ctx.claimed.get(rel) or ctx.progress.owner(rel)
    if taken and taken != vid:
        stem = fit_stem(folder, f"{stem} [{vid}]")
        rel = str((folder / f"{stem}.mp4").relative_to(ctx.root))
    ctx.claimed[rel] = vid
    return stem


def write_series(ctx: Ctx, series: dict, folder: Path) -> None:
    if series.get("id") in ctx.series_done:
        return
    ctx.series_done.add(series.get("id"))
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(folder / "tvshow.nfo", tvshow_nfo(series))
    write_json(folder / "series.info.json", scrub(series))
    if not ctx.opts.no_images:
        imgs = series.get("images") or {}
        for kind, key in (("poster", "vertical"), ("fanart", "background"), ("landscape", "horizontal"),
                          ("banner", "horizontalBig")):
            fetch_image(ctx.http, ctx.ffmpeg, imgs.get(key), folder / kind)


def list_formats(ctx: Ctx, job: Job, head: str) -> str:
    try:
        detail = ctx.api.detail(job.vod_id)
        log(f"{head} {bold(detail.get('title') or job.vod_id)} {gray(f'({job.vod_id})')}")
        variants = list_variants(ctx.http, ctx.api.vod_source(job.vod_id))
    except VtvgoError as e:
        log(f"{head} {job.vod_id}")
        log(fail_line(e))
        return "error"
    for v in sorted(variants, key=lambda v: (v.height, v.bandwidth), reverse=True):
        log(f"    {cyan(v.label)}  {gray(v.source)}")
    return "skip"


def process_vod(ctx: Ctx, job: Job, n: int, total: int) -> str:
    """Download one VOD with its metadata. Returns "done", "skip" or "error"."""
    opts = ctx.opts
    head = gray(f"[{n}/{total}]")
    if opts.list_formats:
        return list_formats(ctx, job, head)
    try:
        detail = ctx.api.detail(job.vod_id)
    except VtvgoError as e:
        log(f"{head} {job.vod_id}")
        log(fail_line(e))
        ctx.progress.mark(job.vod_id, "error", error=str(e))
        return "error"
    series = series_of(detail)
    folder = ctx.root / sanitize(series.get("title"), 80) if series else fit_folder(ctx.root, job.folder)
    title = detail.get("title") or job.vod_id
    dur = as_int(detail.get("durationSecond"))
    log(f"{head} {bold(title)} {gray(f'({fmt_clock(dur) if dur else chr(63)}, {job.vod_id})')}")

    prev = ctx.progress.done(job.vod_id, ctx.root)
    if prev and not opts.metadata_only:
        log(gray(t(f"    already downloaded: {prev}", f"    đã tải rồi: {prev}")))
        return "skip"

    folder.mkdir(parents=True, exist_ok=True)
    stem = vod_stem(ctx, detail, series, folder)
    row = dict(title=title, series=(series or {}).get("title"), episode=as_int(detail.get("episodeNumber")) or None,
               date=(released(detail) or datetime.now(VN_TZ)).strftime("%Y-%m-%d"), duration=dur or None)
    if series:
        write_series(ctx, series, folder)

    src: dict = {}
    variant: Optional[Variant] = None
    problem: Optional[VtvgoError] = None
    if not opts.metadata_only:
        try:
            src = ctx.api.vod_source(job.vod_id)
            variant = pick_variant(list_variants(ctx.http, src), opts.quality)
            log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
        except VtvgoError as e:
            problem = e

    # Metadata is written even when the stream is not available (e.g. it needs a plan).
    thumb = None
    if not opts.no_images:
        imgs = detail.get("images") or {}
        thumb = fetch_image(ctx.http, ctx.ffmpeg, imgs.get("horizontal") or imgs.get("horizontalBig")
                            or src.get("firstImage") or imgs.get("background"), folder / f"{stem}-thumb")
        if not series:
            for kind, key in (("poster", "vertical"), ("fanart", "background")):
                fetch_image(ctx.http, ctx.ffmpeg, imgs.get(key), folder / f"{stem}-{kind}")
    write_json(folder / f"{stem}.info.json", {
        "id": detail["id"], "url": f"{SITE}/watch/{detail['id']}", "title": detail.get("title"),
        "series": (series or {}).get("title"), "episode": row["episode"],
        "format": variant.safe() if variant else None,
        "downloaded_at": datetime.now(VN_TZ).isoformat(timespec="seconds"), "tool": f"vtvgo-dl {__version__}",
        "detail": scrub(detail), "playback": scrub(src) if src else None})
    write_atomic(folder / f"{stem}.nfo", vod_nfo(detail, series, thumb.name if thumb else None))
    if detail.get("description"):
        write_atomic(folder / f"{stem}.description.txt", f"{detail['description']}\n")

    if opts.metadata_only:
        ctx.progress.mark(job.vod_id, "metadata", file=str((folder / f"{stem}.info.json").relative_to(ctx.root)), **row)
        log(green(t("    metadata saved", "    đã lưu metadata")))
        return "done"
    if problem or not variant:
        log(fail_line(problem))
        ctx.progress.mark(job.vod_id, "error", error=str(problem), **row)
        return "error"

    subs: list[tuple[Path, str]] = []
    if not opts.no_subs:
        for i, (url, lang) in enumerate(subtitle_tracks(detail, src)):
            dest = folder / f"{stem}.{lang}{'' if i == 0 else i}{Path(urlparse(url).path).suffix.lower() or '.vtt'}"
            try:
                r = ctx.http.s.get(url, timeout=30)
                r.raise_for_status()
                dest.write_bytes(r.content)
                subs.append((dest, lang))
            except (requests.RequestException, OSError):
                log(warn(t(f"subtitle ({lang}) failed", f"phụ đề ({lang}) lỗi")))

    out = folder / f"{stem}.mp4"
    tags = vod_tags(detail, series)
    state = "done"
    try:
        if detail.get("isLive") or src.get("isLive"):
            raise LiveStream()
        download_stream(ctx, variant, out, cover=thumb, subs=subs, tags=tags)
    except LiveStream:
        log(gray(t("    live stream: recording", "    luồng trực tiếp: đang ghi")))
        try:
            out = record_live(ctx, variant, lambda: list_variants(ctx.http, ctx.api.vod_source(job.vod_id)),
                              folder, stem, tags, thumb)
        except VtvgoError as e:
            log(fail_line(e))
            ctx.progress.mark(job.vod_id, "error", error=str(e), **row)
            return "error"
        state = "recorded"          # not "done": running the command again records again
    except VtvgoError as e:
        log(fail_line(e))
        ctx.progress.mark(job.vod_id, "error", error=str(e), **row)
        return "error"
    size = out.stat().st_size
    ctx.progress.mark(job.vod_id, state, file=str(out.relative_to(ctx.root)), quality=variant.label,
                      size=size, error=None, **row)
    log(f"    {green('✓')} {out.name} {gray(fmt_bytes(size))}")
    return "done"


# --------------------------------------------------------------------------- #
# TV channels: list, EPG, catch-up, live
# --------------------------------------------------------------------------- #

def vn_day(text: Optional[str]) -> str:
    """--date value -> YYYY-MM-DD (Vietnam time)."""
    now = datetime.now(VN_TZ)
    if not text or text.lower() in ("today", "homnay", "hom-nay"):
        return now.strftime("%Y-%m-%d")
    if text.lower() in ("yesterday", "homqua", "hom-qua"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m"):
        try:
            d = datetime.strptime(text, fmt)
            return (d.replace(year=now.year) if fmt == "%d/%m" else d).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(text)


def prog_times(p: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    return parse_time(p.get("startTime")), parse_time(p.get("endTime"))


def list_channels(api: Api) -> None:
    group = None
    for ch in api.channels().values():
        if ch.get("group") != group:
            group = ch.get("group")
            log("\n" + bold_cyan(group or ""))
        hours = ch.get("maxCatchUpTimeValue") if ch.get("catchUpToVodEnabled") else None
        extra = f"{t('catch-up', 'xem lại')} {hours}h" if hours else ""
        log(f"  {cyan(str(ch['id']).ljust(28))} {ch.get('name', '')}  {gray(extra)}")
    log(gray(t("\nUse: python vtvgo-dl.py https://vtvgo.vn/channel/<id>  (--epg, --program, --duration)",
               "\nDùng: python vtvgo-dl.py https://vtvgo.vn/channel/<id>  (--epg, --program, --duration)")))


def print_epg(api: Api, ch_id: str, name: str, day: str) -> None:
    now = datetime.now(VN_TZ)
    log(bold(f"{name} — {day}"))
    for p in api.epg(ch_id, day):
        st, en = prog_times(p)
        if not st or not en:
            continue
        mark = red("● LIVE") if st <= now < en else (green("✓") if en < now else " ")
        desc = f" — {p['description']}" if p.get("description") else ""
        log(f"  {st:%H:%M}-{en:%H:%M}  {mark:<6} {gray(str(p.get('programID')))}  {p.get('title', '')}{gray(desc)}")
    log(gray(t("✓ = finished (catch-up). Download with  --program <id or title>  (and --date).",
               "✓ = đã phát xong (xem lại). Tải bằng  --program <id hoặc tên>  (kèm --date).")))


def select_programs(api: Api, ch_id: str, wanted: str, day: str) -> list[dict]:
    """Programs of `day` matching a comma list of program ids / title words / "all". An id not
    found on that day is looked up in the 8 days before it. Programs not finished yet are skipped."""
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


def channel_nfo(ch: dict, title: str, plot: str, when: Optional[datetime], seconds: float) -> str:
    return nfo("episodedetails", [("title", title), ("showtitle", ch.get("name")), ("plot", plot),
                                  ("aired", when.strftime("%Y-%m-%d") if when else None),
                                  ("runtime", round(seconds / 60)), ("studio", ch.get("name")), ("genre", "TV")])


def run_catchup(ctx: Ctx, ch: dict, folder: Path, cover: Optional[Path], counts: dict) -> None:
    opts = ctx.opts
    ch_id, name = ch["id"], ch.get("name") or ch["id"]
    progs = select_programs(ctx.api, ch_id, opts.program, vn_day(opts.date))
    log(bold(t(f"{name}: {len(progs)} program(s) to download", f"{name}: {len(progs)} chương trình cần tải")))
    for n, p in enumerate(progs, 1):
        if STOP.is_set():
            break
        st, en = prog_times(p)
        title, desc = p.get("title") or "", p.get("description") or ""
        full = title + (f" - {desc}" if desc else "")
        key = f"catchup:{ch_id}:{p.get('programID')}:{p.get('startTime')}"
        stem = fit_stem(folder, sanitize(f"{st:%Y-%m-%d %H%M} {full}", 110))
        log(f"{gray(f'[{n}/{len(progs)}]')} {bold(title)} {gray(f'{st:%d/%m %H:%M}-{en:%H:%M}' + (' — ' + desc if desc else ''))}")
        if ctx.progress.done(key, ctx.root) and not opts.metadata_only:
            log(gray(t("    already downloaded", "    đã tải rồi")))
            counts["skip"] += 1
            continue
        row = dict(title=full, series=name, date=f"{st:%Y-%m-%d}", duration=int((en - st).total_seconds()))
        try:
            src = ctx.api.catchup_source(ch_id, p)
            variant = pick_variant(list_variants(ctx.http, src), opts.quality)
            log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
            write_json(folder / f"{stem}.info.json", {
                "channel": scrub(ch), "program": scrub(p), "format": variant.safe(), "playback": scrub(src),
                "tool": f"vtvgo-dl {__version__}", "downloaded_at": datetime.now(VN_TZ).isoformat(timespec="seconds")})
            write_atomic(folder / f"{stem}.nfo", channel_nfo(ch, full, desc or title, st, (en - st).total_seconds()))
            if opts.metadata_only:
                counts["done"] += 1
                continue
            out = folder / f"{stem}.mp4"
            download_stream(ctx, variant, out, cover=cover, tags=channel_tags(ch, full, desc, st, f"{SITE}/channel/{ch_id}"))
        except (VtvgoError, LiveStream) as e:
            msg = str(e) or t("the program is still being broadcast", "chương trình vẫn đang phát")
            log(fail_line(msg))
            ctx.progress.mark(key, "error", error=msg, **row)
            counts["error"] += 1
            continue
        size = out.stat().st_size
        ctx.progress.mark(key, "done", file=str(out.relative_to(ctx.root)), quality=variant.label,
                          size=size, error=None, **row)
        log(f"    {green('✓')} {out.name} {gray(fmt_bytes(size))}")
        counts["done"] += 1


def run_live(ctx: Ctx, ch: dict, session_id: str, folder: Path, cover: Optional[Path], counts: dict) -> None:
    ch_id, name = ch["id"], ch.get("name") or ch["id"]
    now = datetime.now(VN_TZ)
    try:
        today = ctx.api.epg(ch_id, now.strftime("%Y-%m-%d"))
    except VtvgoError:
        today = []
    current = next((p for p in today if (lambda st, en: bool(st and en and st <= now < en))(*prog_times(p))), None)
    what, desc = (current or {}).get("title") or "", (current or {}).get("description") or ""
    key = f"live:{ch_id}:{now:%Y%m%d%H%M%S}"
    log(f"{bold(name)} {red('● LIVE')} {what} {gray(desc)}")
    try:
        if session_id:
            data = ctx.api.live_session(ch_id, session_id)
            if not (data.get("live") or {}).get("url"):
                raise VtvgoError(t(f"the live session is '{data.get('status')}' (start {data.get('startTime')}), "
                                   "nothing to record now",
                                   f"phiên live đang '{data.get('status')}' (bắt đầu {data.get('startTime')}), "
                                   "chưa có gì để ghi"))
            what = data.get("title") or what

            def refresh() -> list[Variant]:
                d = ctx.api.live_session(ch_id, session_id)
                return list_variants(ctx.http, {"sourceModes": [{"sources": [{"url": (d.get("live") or {}).get("url")}]}]})
        else:
            def refresh() -> list[Variant]:
                return list_variants(ctx.http, ctx.api.channel_source(ch_id))
        variant = pick_variant(refresh(), ctx.opts.quality)
        log(f"    {t('quality', 'chất lượng')}: {green(variant.label)}")
        label = f"{name} LIVE {now:%d/%m/%Y %H:%M}" + (f" - {what}" if what else "")
        stem = sanitize(f"{now:%Y-%m-%d} {name} LIVE" + (f" - {what}" if what else ""), 100)
        out = record_live(ctx, variant, refresh, folder, stem,
                          channel_tags(ch, label, desc, now, f"{SITE}/channel/{ch_id}"), cover)
    except VtvgoError as e:
        log(fail_line(e))
        ctx.progress.mark(key, "error", error=str(e), title=f"{name} LIVE", series=name)
        counts["error"] += 1
        return
    end = datetime.now(VN_TZ)
    progs = [p for p in today if (lambda st, en: bool(st and en and en > now and st < end))(*prog_times(p))]
    lineup = "\n".join(f"{prog_times(p)[0]:%H:%M} {p.get('title', '')} {p.get('description', '')}".strip() for p in progs)
    write_json(folder / f"{out.stem}.info.json", {
        "channel": scrub(ch), "recorded_from": now.isoformat(timespec="seconds"),
        "recorded_to": end.isoformat(timespec="seconds"), "programs": scrub(progs),
        "format": variant.safe(), "tool": f"vtvgo-dl {__version__}"})
    write_atomic(folder / f"{out.stem}.nfo", channel_nfo(ch, label, lineup, now, (end - now).total_seconds()))
    size = out.stat().st_size
    ctx.progress.mark(key, "recorded", file=str(out.relative_to(ctx.root)), quality=variant.label, size=size,
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
    if ctx.opts.epg:
        print_epg(ctx.api, ch_id, ch.get("name") or ch_id, vn_day(ctx.opts.date))
        return
    folder = ctx.root / "Channels" / sanitize(ch.get("name") or ch_id, 60)
    folder.mkdir(parents=True, exist_ok=True)
    cover = None if ctx.opts.no_images else fetch_image(ctx.http, ctx.ffmpeg, ch.get("thumbnail") or ch.get("logo"),
                                                         folder / "channel")
    if ctx.opts.program:
        run_catchup(ctx, ch, folder, cover, counts)
    else:
        run_live(ctx, ch, session_id, folder, cover, counts)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def find_tool(name: str) -> Optional[str]:
    """An executable next to vtvgo-dl.py (aria2c.exe, ffmpeg.exe), else on PATH."""
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vtvgo-dl",
        description=t("Download videos, series, pages, catch-up and live TV from vtvgo.vn in the best quality, "
                      "with full metadata.",
                      "Tải video, phim bộ, cả trang, xem lại và truyền hình trực tiếp từ vtvgo.vn với chất lượng "
                      "cao nhất, kèm đầy đủ metadata."),
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
    p.add_argument("-a", "--batch-file", help=t("read links from a text file (one per line, # comments)",
                                                "đọc link từ file văn bản (mỗi dòng một link, # là ghi chú)"))
    p.add_argument("-o", "--output", default=str(SCRIPT_DIR / "downloads"),
                   help=t("output folder (default: downloads)", "thư mục lưu (mặc định: downloads)"))
    p.add_argument("-q", "--quality", default="best",
                   help=t("best (default), worst, or a max height such as 720",
                          "best (mặc định), worst, hoặc chiều cao tối đa, vd 720"))
    p.add_argument("-F", "--list-formats", action="store_true",
                   help=t("only list the available qualities", "chỉ liệt kê các chất lượng có sẵn"))
    p.add_argument("-e", "--episodes", help=t("episodes to download, e.g. 1-5,8,10-", "các tập cần tải, vd 1-5,8,10-"))
    p.add_argument("--whole-series", action="store_true",
                   help=t("an episode link downloads its whole series", "link một tập sẽ tải cả bộ phim"))
    p.add_argument("-n", "--limit", type=int, help=t("max videos per list / section", "số video tối đa mỗi danh sách / mục"))
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
    p.add_argument("--no-aria2c", action="store_true",
                   help=t("download with requests instead of aria2c", "tải bằng requests thay vì aria2c"))
    p.add_argument("--proxy", default=os.environ.get("VTVGO_PROXY"),
                   help=t("HTTP/SOCKS proxy, e.g. a Vietnamese one for geo-blocked videos",
                          "proxy HTTP/SOCKS, vd proxy Việt Nam cho video bị chặn vùng"))
    live = p.add_argument_group(t("TV channels (https://vtvgo.vn/channel/<id>)",
                                  "Kênh truyền hình (https://vtvgo.vn/channel/<id>)"))
    live.add_argument("--channels", action="store_true", help=t("list every channel and its id", "liệt kê mọi kênh và id"))
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
    p.add_argument("--login", action="store_true", help=t("open a browser to log in to VTVgo, and save the session",
                                                          "mở trình duyệt để đăng nhập VTVgo và lưu phiên"))
    p.add_argument("--logout", action="store_true", help=t("delete the saved session", "xoá phiên đã lưu"))
    p.add_argument("--session", default=str(SESSION_FILE), help=argparse.SUPPRESS)
    p.add_argument("--lang", choices=("en", "vi"), help=t("message language", "ngôn ngữ thông báo"))
    p.add_argument("--no-color", action="store_true", help=t("no colors", "tắt màu"))
    p.add_argument("-V", "--version", action="version", version=f"vtvgo-dl {__version__}")
    return p


def read_inputs(opts) -> list[str]:
    """Links from the command line and from -a. A line from failed.txt may carry "--program <id>"."""
    lines = list(opts.inputs)
    if opts.batch_file:
        lines += read_batch(Path(opts.batch_file))
    out = []
    for line in lines:
        m = re.fullmatch(r"(\S+)\s+--program\s+(\S+)", line.strip())
        if m:                                  # a catch-up line written to failed.txt
            opts.program = ",".join(x for x in (opts.program, m.group(2)) if x)
            line = m.group(1)
        out.append(line.strip())
    return [x for x in out if x]


def check_options(opts) -> None:
    """Normalise option values; ValueError for a bad one."""
    opts.episodes = parse_ranges(opts.episodes)
    opts.duration = parse_duration(opts.duration)
    vn_day(opts.date)
    opts.quality = str(opts.quality).lower().rstrip("p")
    if opts.quality not in ("best", "worst") and not opts.quality.isdigit():
        raise ValueError(f"--quality {opts.quality}")
    if opts.limit is not None and opts.limit < 1:
        raise ValueError(f"--limit {opts.limit}")
    opts.jobs = max(1, min(64, opts.jobs))


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
    parser = build_parser()
    opts = parser.parse_intermixed_args()        # links may come before or after the options
    init_colors(not opts.no_color)
    session_path = Path(opts.session)
    if opts.logout:
        session_path.unlink(missing_ok=True)
        log(green(t("Logged out (session file deleted).", "Đã đăng xuất (đã xoá file phiên).")))
        return 0
    if opts.login:
        return login(session_path)

    try:
        texts = read_inputs(opts)
        check_options(opts)
    except (OSError, ValueError) as e:
        log(red(t(f"Bad value: {e}", f"Giá trị sai: {e}")))
        return 2
    if not texts and not opts.channels:
        parser.print_help()
        return 2
    channels, vods = [], []
    for text in texts:
        try:
            kind, value = parse_input(text)
        except ValueError:
            log(red(t(f"Not a vtvgo link or id: {text}", f"Không phải link/id vtvgo: {text}")))
            continue
        (channels if kind == "channel" else vods).append((kind, value))

    token, device_id = load_session(session_path)
    api = Api(token, opts.proxy, device_id)
    if opts.channels:
        try:
            list_channels(api)
        except VtvgoError as e:
            log(red(str(e)))
            return 1
        if not channels and not vods:
            return 0

    ffmpeg = find_ffmpeg()
    aria2 = None if opts.no_aria2c else find_tool("aria2c")
    if aria2 and opts.proxy and opts.proxy.lower().startswith("socks"):
        aria2 = None                  # aria2c has no SOCKS support: requests does it (pip install pysocks)
    if not ffmpeg and not (opts.metadata_only or opts.list_formats or opts.epg):
        log(red(t("ffmpeg not found. Put ffmpeg.exe next to vtvgo-dl.py or  pip install imageio-ffmpeg",
                  "Không tìm thấy ffmpeg. Đặt ffmpeg.exe cạnh vtvgo-dl.py hoặc  pip install imageio-ffmpeg")))
        return 2

    root = Path(opts.output)
    root.mkdir(parents=True, exist_ok=True)
    yes, no = t("yes", "có"), t("no", "không")
    log(bold_cyan(f"vtvgo-dl {__version__}") + gray(
        f"  {t('login', 'đăng nhập')}: {green(yes) if token else no}  aria2c: {yes if aria2 else no}"
        f"  {t('quality', 'chất lượng')}: {opts.quality}" + (f"  proxy: {yes}" if opts.proxy else "")))
    progress = Progress(root / "vtvgo-dl.db")
    ctx = Ctx(api, Http(opts.proxy), opts, root, progress, ffmpeg, aria2)
    counts = {"done": 0, "skip": 0, "error": 0}
    try:
        for _, value in channels:
            if STOP.is_set():
                break
            try:
                run_channel(ctx, value, counts)
            except (VtvgoError, OSError) as e:
                log(red(f"{value}: {one_line(e)}"))
                counts["error"] += 1
        jobs = collect_jobs(api, vods, opts) if vods and not STOP.is_set() else []
        if vods and not jobs:
            log(yellow(t("Nothing to download.", "Không có gì để tải.")))
        if jobs:
            log(bold(t(f"{len(jobs)} video(s) to process", f"{len(jobs)} video cần xử lý")))
        for n, job in enumerate(jobs, 1):
            if STOP.is_set():
                break
            try:
                counts[process_vod(ctx, job, n, len(jobs))] += 1
            except (VtvgoError, OSError) as e:      # e.g. disk full, file locked: go on with the next one
                status("")
                log(fail_line(one_line(e)))
                progress.mark(job.vod_id, "error", error=str(e))
                counts["error"] += 1
    except KeyboardInterrupt:
        STOP.set()
    finally:
        status("")
        failed = write_reports(progress, root)
        progress.close()
    if STOP.is_set():
        log(yellow(t("\nStopped. Run the same command again to resume.", "\nĐã dừng. Chạy lại đúng lệnh này để tải tiếp.")))
    if opts.epg and not vods:
        return 0
    log("")
    log(bold(t("Summary", "Tổng kết")) + f": {green(str(counts['done']))} {t('done', 'xong')}, "
        f"{counts['skip']} {t('skipped', 'bỏ qua')}, {red(str(counts['error'])) if counts['error'] else 0} "
        f"{t('failed', 'lỗi')}  →  {root}")
    if failed:
        log(gray(t(f"Failed links are listed in {root / 'failed.txt'} (retry with -a)",
                   f"Link lỗi được ghi trong {root / 'failed.txt'} (tải lại bằng -a)")))
    if STOP.is_set():
        return 130
    return 1 if counts["error"] and not counts["done"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        STOP.set()
        status("")
        sys.exit(130)
