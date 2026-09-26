#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Demo Fetcher — shared engine (used by both the GUI and the CLI).

Share code chain (Steam API) → boiler-writter (Game Coordinator) → demo URL
→ parallel download → validate → decompress → match730_<mid>_<oid>_<token>.dem

Every mode Valve records is fetched (Premier/Competitive 5v5, Wingman, Rush 3v3…):
the mode is only detected for display, never used to filter.
"""

__version__ = "3.4.0"

import bz2
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ════════════════════════════════════════════════════════════════
#  PATHS
# ════════════════════════════════════════════════════════════════

SCRIPT_DIR  = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
BOILER_DIR  = SCRIPT_DIR / "boiler"

# ════════════════════════════════════════════════════════════════
#  SETTINGS — defaults, overridable in config.json → "settings"
# ════════════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    "download_workers":  4,       # parallel demo downloads
    "boiler_delay":      4,       # seconds between two Game Coordinator calls
    "api_rate_delay":    0.4,     # seconds between two Steam API calls
    "max_retries":       3,       # download attempts per demo
    "retry_backoff":     2,       # seconds, doubled at each retry
    "http_timeout":      15,      # seconds, small JSON requests
    "download_timeout":  180,     # seconds, per network read during a download
    "boiler_timeout":    45,      # seconds, one boiler-writter run
    "api_max_failures":  5,       # consecutive Steam API failures before giving up
    "fetch_recent_gc_matches": True,  # also fetch the logged-in account's recent
                                      # matches listed by the GC (catches matches
                                      # missing from the share code chain)
    "steam_next_code_url": "https://api.steampowered.com"
                           "/ICSGOPlayers_730/GetNextMatchSharingCode/v1",
    "github_boiler_api":   "https://api.github.com/repos/akiver/boiler-writter/releases/latest",
}

USER_AGENT = "cs2dl/5"
CHUNK_SIZE = 1 << 16

SHARECODE_ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
SHARECODE_BASE     = len(SHARECODE_ALPHABET)

BOILER_EXIT_CODES = {
    0: "OK", 1: "Invalid arguments", 2: "Steam needs restart",
    3: "Steam not running", 4: "User not logged in / GC busy",
    5: "CS2 not installed", 6: "Game Coordinator connection error",
    7: "GC timeout", 8: "Match not found (expired > 30 days)",
}
BOILER_RC_EXPIRED = 8

PLATFORM_ASSETS = {
    ("Windows","AMD64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Windows","ARM64"):  {"kw":["win"],         "exc":["mac","linux"],"bin":"boiler-writter.exe","ext":".zip"},
    ("Linux","x86_64"):   {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Linux","aarch64"):  {"kw":["linux"],       "exc":["mac","win"],  "bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","x86_64"):  {"kw":["mac"],         "exc":["arm64","win"],"bin":"boiler-writter",    "ext":".zip"},
    ("Darwin","arm64"):   {"kw":["mac","arm64"], "exc":["win"],        "bin":"boiler-writter",    "ext":".zip"},
}

_RE_STEAMID      = re.compile(r'^\d{17}$')
_RE_BIGNUM       = re.compile(r'\d{8,21}')
_RE_DEMO_URL     = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}\.dem(?:\.bz2)?')
_RE_FALLBACK_URL = re.compile(r'https?://[^\x00-\x1f\x7f\s]{15,}')
_BZ2_MAGIC       = b'BZ'
_DEM_MAGICS      = (b'PBDEMS2', b'HL2DEMO')

# Hide the console window of child processes when running under pythonw (GUI)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

SKIPPED = "__skipped__"


class ScanCancelled(Exception):
    """Raised when the user cancels — never reported as an error."""


class ConfigError(Exception):
    """config.json exists but cannot be used — never silently overwritten."""


def _check_cancel(cancel: Optional[threading.Event]):
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()


def _sleep(seconds, cancel=None):
    """Interruptible sleep."""
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise ScanCancelled()


# ════════════════════════════════════════════════════════════════
#  THREAD-SAFE SET
# ════════════════════════════════════════════════════════════════

class SafeSet:
    __slots__ = ('_data', '_lock')

    def __init__(self, initial=None):
        self._data = set(initial) if initial else set()
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock: self._data.add(item)

    def update(self, items):
        with self._lock: self._data.update(items)

    def __contains__(self, item):
        with self._lock: return item in self._data

    def __len__(self):
        with self._lock: return len(self._data)


# ════════════════════════════════════════════════════════════════
#  SHARE CODE
# ════════════════════════════════════════════════════════════════

def decode_share_code(code: str) -> Tuple[int, int, int]:
    """→ (matchId, outcomeId/reservationId, token). Raises ValueError."""
    if not isinstance(code, str):
        raise ValueError("Share code must be a string")
    clean = code.strip().replace("CSGO-", "").replace("-", "")
    if len(clean) != 25:
        raise ValueError(f"Expected 25 characters, got {len(clean)}")
    val = 0
    for ch in reversed(clean):
        idx = SHARECODE_ALPHABET.find(ch)
        if idx == -1: raise ValueError(f"Invalid character: {ch!r}")
        val = val * SHARECODE_BASE + idx
    if val >= 1 << 144:
        raise ValueError("Share code out of range")
    raw = val.to_bytes(18, byteorder='big')
    return (
        int.from_bytes(raw[0:8],   "little"),
        int.from_bytes(raw[8:16],  "little"),
        int.from_bytes(raw[16:18], "little"),
    )


def encode_share_code(mid: int, oid: int, token: int) -> str:
    """Inverse of decode_share_code (token = tv_port & 0xFFFF)."""
    raw = (mid.to_bytes(8, "little") + oid.to_bytes(8, "little")
           + (token & 0xFFFF).to_bytes(2, "little"))
    val = int.from_bytes(raw, "big")
    chars = []
    for _ in range(25):
        val, r = divmod(val, SHARECODE_BASE)
        chars.append(SHARECODE_ALPHABET[r])
    s = "".join(chars)
    return f"CSGO-{s[0:5]}-{s[5:10]}-{s[10:15]}-{s[15:20]}-{s[20:25]}"


def valid_share_code(code) -> bool:
    try: decode_share_code(code); return True
    except ValueError: return False


def valid_steam_id(sid) -> bool:
    return bool(_RE_STEAMID.match(sid or ""))


def csdm_name(mid, oid, token):
    return f"match730_{mid:021d}_{oid & 0xFFFFFFFF:010d}_{token}"


def selftest():
    assert len(SHARECODE_ALPHABET) == 57, "Alphabet corrupted"
    m, r, t = decode_share_code("CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK")
    assert m == 3230642215713767580 and r == 3230647599455273103 and t == 55788
    assert encode_share_code(m, r, t) == "CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK"


# ════════════════════════════════════════════════════════════════
#  GAME COORDINATOR RESPONSE (protobuf) — minimal dependency-free reader
#  CMsgGCCStrike15_v2_MatchList
#    4: repeated CDataGCCStrike15_v2_MatchInfo
#       1: matchid  3: watchablematchinfo (2: tv_port)
#       4: roundstats_legacy  5: repeated roundstatsall
#          (last) 1: reservationid  3: demo URL
#                 2: reservation  (1: repeated account_ids, 2: game_type)
# ════════════════════════════════════════════════════════════════

def _pb_varint(b, i):
    r = s = 0
    while True:
        if i >= len(b): raise ValueError("truncated varint")
        c = b[i]; i += 1
        r |= (c & 0x7F) << s; s += 7
        if c < 0x80: return r, i
        if s > 70: raise ValueError("varint too long")


def _pb_fields(b):
    """Yields (field_number, wire_type, value) — value is int or bytes."""
    i = 0
    while i < len(b):
        key, i = _pb_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _pb_varint(b, i)
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 5:
            v = b[i:i + 4]; i += 4
        elif w == 2:
            n, i = _pb_varint(b, i)
            if i + n > len(b): raise ValueError("truncated field")
            v = b[i:i + n]; i += n
        else:
            raise ValueError(f"unsupported wire type {w}")
        yield f, w, v


# Map bits from CS:DM (akiver/cs-demo-manager get-map-name.ts) — display only
_MAP_BITS = {
    1 << 0: "de_warden", 1 << 1: "de_dust2", 1 << 2: "de_train", 1 << 3: "de_ancient",
    1 << 4: "de_inferno", 1 << 5: "de_nuke", 1 << 6: "de_vertigo", 1 << 7: "de_mirage",
    1 << 8: "cs_office", 1 << 9: "de_poseidon", 1 << 10: "de_eldorado",
    1 << 11: "de_sanctum", 1 << 12: "de_cache", 1 << 13: "de_stronghold",
    1 << 14: "de_boulder", 1 << 15: "de_anubis", 1 << 16: "de_tuscan",
    1 << 18: "de_fachwerk", 1 << 19: "cs_shelter", 1 << 20: "de_overpass",
    1 << 21: "de_cobblestone", 1 << 22: "de_canals",
}

# Mode is derived from the number of reserved players: the GC "game_type" low
# byte is NOT reliable (a 10-player match was observed with the byte CS:DM
# labels as wingman). Rush (22 Sep 2026) is the only 3v3 queued mode.
_MODE_BY_PLAYERS = {10: "5v5", 4: "Wingman", 6: "Rush"}


def match_mode(players: int) -> str:
    return _MODE_BY_PLAYERS.get(players, "Unknown")


def map_name(game_type: Optional[int], mode: str) -> str:
    if mode == "Rush":
        return "rush_001"      # single official Rush map (TBD: map bit unknown)
    if not game_type:
        return "?"
    return _MAP_BITS.get((game_type >> 8) & 0xFFFFFF, "?")


def parse_match_list(data: bytes) -> List[dict]:
    """Parses a boiler-writter output file. Returns one dict per match:
       {matchid, reservationid, tv_port, url, game_type, players, rounds, mode, map}
       Raises ValueError on malformed data."""
    matches = []
    for f, w, v in _pb_fields(data):
        if f != 4 or w != 2:
            continue
        m = {"matchid": None, "reservationid": None, "tv_port": None,
             "url": None, "game_type": None, "players": 0, "rounds": 0}
        rounds = []
        legacy = None
        for ff, ww, vv in _pb_fields(v):
            if ff == 1 and ww == 0:
                m["matchid"] = vv
            elif ff == 3 and ww == 2:
                for a, wa, x in _pb_fields(vv):
                    if a == 2 and wa == 0: m["tv_port"] = x
            elif ff == 4 and ww == 2:
                legacy = vv
            elif ff == 5 and ww == 2:
                rounds.append(vv)
        m["rounds"] = len(rounds)
        last = legacy if legacy is not None else (rounds[-1] if rounds else None)
        if last is not None:
            for a, wa, x in _pb_fields(last):
                if a == 1 and wa == 0:
                    m["reservationid"] = x
                elif a == 3 and wa == 2:
                    m["url"] = x.decode("utf-8", errors="replace")
                elif a == 2 and wa == 2:
                    for r, wr, y in _pb_fields(x):
                        if r == 1 and wr == 0:
                            m["players"] += 1
                        elif r == 1 and wr == 2:          # packed encoding
                            j = 0
                            while j < len(y):
                                _, j = _pb_varint(y, j); m["players"] += 1
                        elif r == 2 and wr == 0:
                            m["game_type"] = y
        m["mode"] = match_mode(m["players"])
        m["map"]  = map_name(m["game_type"], m["mode"])
        matches.append(m)
    return matches


def describe_match(info: Optional[dict]) -> str:
    if not info:
        return ""
    s = f"{info['mode']}"
    if info["mode"] == "Rush":
        s += " 3v3"
    if info["map"] != "?":
        s += f" · {info['map']}"
    if info["mode"] in ("Rush", "Unknown"):
        # raw values logged so the Rush game_type can be confirmed/reported
        s += f" (game_type={info['game_type']}, players={info['players']})"
    return s


# ════════════════════════════════════════════════════════════════
#  DUPLICATE DETECTION + CLEANUP
# ════════════════════════════════════════════════════════════════

def _ids_in_name(stem: str):
    for m in _RE_BIGNUM.finditer(stem):
        yield str(int(m.group(0)))          # normalize away leading zeros


def scan_folder(path: Path) -> SafeSet:
    """Index every ≥8-digit number found in every .dem filename (any tool,
    any subfolder). The folder itself is the source of truth."""
    ids = set()
    for dem in path.rglob("*.dem"):
        ids.update(_ids_in_name(dem.stem))
    return SafeSet(ids)


def match_keys(mid, oid=None):
    """Every exact string form a match's id can appear as in a filename:
       matchId, reservationId, and reservationId truncated to 32 bits."""
    keys = {str(mid)}
    if oid is not None:
        keys.add(str(oid))
        keys.add(str(oid & 0xFFFFFFFF))
    return keys


def is_present(known, mid, oid=None):
    return any(k in known for k in match_keys(mid, oid))


def register(known, mid, oid=None):
    known.update(match_keys(mid, oid))


def on_disk(dl_path: Path, mid, oid=None) -> bool:
    """Fresh disk check (catches files written by another tool mid-run)."""
    keys = match_keys(mid, oid)
    for dem in dl_path.rglob("*.dem"):
        if keys.intersection(_ids_in_name(dem.stem)):
            return True
    return False


def cleanup_tmp(dl_path: Path) -> int:
    count = 0
    for f in dl_path.glob("_tmp_*"):
        try:
            f.unlink(); count += 1
        except FileNotFoundError:
            pass
    return count


# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════

def default_config():
    return {"download_path": "", "players": [], "settings": dict(DEFAULT_SETTINGS)}


def load_config(path: Path = None):
    """Missing file → defaults. Unreadable/invalid file → ConfigError
       (fail fast: never replace the user's players with an empty list)."""
    path = path or CONFIG_FILE
    if not path.exists():
        return default_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} is unreadable ({e}). Fix or remove it — "
                          f"it was NOT modified.") from e
    if not isinstance(cfg, dict) or not isinstance(cfg.get("players", []), list):
        raise ConfigError(f"{path} has an unexpected structure — it was NOT modified.")
    cfg.setdefault("players", [])
    cfg.setdefault("download_path", "")
    settings = cfg.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    for k, v in DEFAULT_SETTINGS.items():
        settings.setdefault(k, v)
    cfg["settings"] = settings
    return cfg


def save_config(cfg, path: Path = None):
    """Atomic write (temp file + replace)."""
    path = path or CONFIG_FILE
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".cfg_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True); raise


def get_settings(cfg) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update((cfg or {}).get("settings") or {})
    return s


def new_player(name, steam_id, api_key, auth_code, share_code):
    return {
        "name": name or steam_id, "steam_id": steam_id,
        "api_key": api_key, "auth_code": auth_code,
        "oldest_share_code": share_code, "last_known_code": share_code,
    }


# ════════════════════════════════════════════════════════════════
#  HTTP LAYER
# ════════════════════════════════════════════════════════════════

class HttpError(Exception):
    def __init__(self, status, msg=""):
        super().__init__(msg or f"HTTP {status}")
        self.status = status


def http_get_json(url, timeout, params=None, log=None):
    try:
        if HAS_REQUESTS:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": USER_AGENT})
            r.raise_for_status(); return r.json()
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        if log: log(f"  [!] HTTP GET failed: {type(e).__name__}: {str(e)[:120]}")
        return None


def http_download(url, dest: Path, settings, on_progress=None, cancel=None):
    """Returns (ok, error, permanent). permanent=True → 404/410: the file is
       gone from Valve's CDN, retrying is pointless."""
    last_error = ""
    retries = max(1, int(settings["max_retries"]))
    for attempt in range(1, retries + 1):
        try:
            _check_cancel(cancel)
            dl, total = _download_once(url, dest, settings["download_timeout"],
                                       on_progress, cancel)
            if total and dl != total:
                raise IOError(f"incomplete transfer ({dl}/{total} bytes)")
            return True, "", False
        except ScanCancelled:
            dest.unlink(missing_ok=True); raise
        except HttpError as e:
            dest.unlink(missing_ok=True)
            last_error = str(e)
            if e.status in (404, 410):
                return False, last_error, True
            if 400 <= e.status < 500 and e.status != 429:
                return False, last_error, False
        except Exception as e:
            dest.unlink(missing_ok=True)
            last_error = f"{type(e).__name__}: {e}"
        if attempt < retries:
            _sleep(settings["retry_backoff"] * (2 ** (attempt - 1)), cancel)
    return False, last_error, False


def _download_once(url, dest, timeout, on_progress, cancel):
    dl = 0
    if HAS_REQUESTS:
        with requests.get(url, stream=True, timeout=timeout,
                          headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code >= 400:
                raise HttpError(r.status_code, f"HTTP {r.status_code} {r.reason}")
            # Content-Length describes the encoded body only when no
            # Content-Encoding is applied — otherwise don't check it.
            enc = r.headers.get("Content-Encoding", "").lower()
            total = int(r.headers.get("Content-Length", 0) or 0) if enc in ("", "identity") else 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    _check_cancel(cancel)
                    f.write(chunk); dl += len(chunk)
                    if on_progress: on_progress(dl, total)
        return dl, total
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        raise HttpError(e.code, f"HTTP {e.code} {e.reason}") from None
    with r:
        total = int(r.headers.get("Content-Length", 0) or 0)
        with open(dest, "wb") as f:
            while True:
                _check_cancel(cancel)
                chunk = r.read(CHUNK_SIZE)
                if not chunk: break
                f.write(chunk); dl += len(chunk)
                if on_progress: on_progress(dl, total)
    return dl, total


# ════════════════════════════════════════════════════════════════
#  FILE VALIDATION + DECOMPRESSION
# ════════════════════════════════════════════════════════════════

def validate_file(path: Path, is_bz2: bool) -> Optional[str]:
    if not path.exists():
        return "file missing"
    size = path.stat().st_size
    if size < 100:
        return f"file too small ({size} B)"
    with open(path, "rb") as f:
        header = f.read(64)
    if b'<html' in header.lower() or header.startswith(b'<!'):
        return "HTML response (demo expired/unavailable)"
    if is_bz2 and not header.startswith(_BZ2_MAGIC):
        return f"not a valid bz2 (magic: {header[:4].hex()})"
    if not is_bz2 and not header.startswith(_DEM_MAGICS):
        return "not a valid .dem file"
    return None


def decompress_bz2(src: Path, dest: Path, cancel=None):
    """Returns (ok, error). Detects truncated archives (stream end missing)."""
    try:
        d = bz2.BZ2Decompressor()
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            while True:
                _check_cancel(cancel)
                chunk = fi.read(CHUNK_SIZE)
                if not chunk: break
                if d.eof:
                    # concatenated stream: continue with a new decompressor
                    chunk = d.unused_data + chunk
                    d = bz2.BZ2Decompressor()
                fo.write(d.decompress(chunk))
                while d.eof and d.unused_data:
                    rest = d.unused_data
                    d = bz2.BZ2Decompressor()
                    fo.write(d.decompress(rest))
        if not d.eof:
            return False, "truncated archive (end of bz2 stream missing)"
        return True, ""
    except ScanCancelled:
        raise
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════════════
#  BOILER-WRITTER — install
# ════════════════════════════════════════════════════════════════

def _platform_info():
    s, m = platform.system(), platform.machine()
    a = {"AMD64":"AMD64","x86_64":"x86_64","ARM64":"ARM64",
         "aarch64":"aarch64","arm64":"arm64"}.get(m, m)
    return PLATFORM_ASSETS.get((s, a))


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve()); return True
    except ValueError:
        return False


def _safe_zip_extract(zf, dest):
    dest = dest.resolve()
    for name in zf.namelist():
        if not _is_within(dest, dest / name):
            raise ValueError(f"Path traversal: {name}")
    zf.extractall(dest)


def _safe_tar_extract(tf, dest):
    dest = dest.resolve()
    if sys.version_info >= (3, 12):
        tf.extractall(dest, filter='data')
    else:
        for m in tf.getmembers():
            if not _is_within(dest, dest / m.name):
                raise ValueError(f"Path traversal: {m.name}")
        tf.extractall(dest)


def find_boiler():
    pi = _platform_info()
    if pi:
        d = BOILER_DIR / pi["bin"]
        if d.is_file(): return d
        if BOILER_DIR.exists():
            found = list(BOILER_DIR.rglob(pi["bin"]))
            if found: return found[0]
    e = shutil.which("boiler-writter") or shutil.which("boiler-writter.exe")
    return Path(e) if e else None


def install_boiler(settings, log=print):
    pi = _platform_info()
    if not pi: log("  [✗] Unsupported platform"); return None
    log("  [→] GitHub → latest release…")
    rel = http_get_json(settings["github_boiler_api"], settings["http_timeout"], log=log)
    if not rel: log("  [✗] Failed to reach GitHub"); return None
    assets = rel.get("assets", [])
    log(f"  [i] {rel.get('tag_name', '?')} — {len(assets)} assets")
    kw  = [k.lower() for k in pi["kw"]]
    exc = [k.lower() for k in pi["exc"]]
    asset = next(
        (a for a in assets if a["name"].lower().endswith(pi["ext"])
         and all(k in a["name"].lower() for k in kw)
         and not any(k in a["name"].lower() for k in exc)), None)
    if not asset:
        log("  [✗] Matching asset not found")
        for a in assets: log(f"      • {a['name']}")
        return None
    BOILER_DIR.mkdir(parents=True, exist_ok=True)
    arc = BOILER_DIR / asset["name"]
    log(f"  [→] Downloading {asset['name']}…")
    ok, err, _ = http_download(asset["browser_download_url"], arc, settings)
    if not ok: log(f"  [✗] Download failed: {err}"); return None
    try:
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            expected = digest.split(":", 1)[1]
            h = hashlib.sha256()
            with open(arc, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    h.update(chunk)
            if h.hexdigest() != expected:
                log(f"  [✗] Checksum mismatch — expected {expected[:12]}… got {h.hexdigest()[:12]}…")
                return None
            log("  [✓] sha256 verified")
        else:
            log("  [!] No digest published — skipping integrity check")
        if arc.suffix == ".zip":
            with zipfile.ZipFile(arc) as z: _safe_zip_extract(z, BOILER_DIR)
        else:
            with tarfile.open(arc, "r:gz") as t: _safe_tar_extract(t, BOILER_DIR)
    except Exception as e:
        log(f"  [✗] Extraction: {e}"); return None
    finally:
        arc.unlink(missing_ok=True)
    for c in [BOILER_DIR / pi["bin"]] + list(BOILER_DIR.rglob(pi["bin"])):
        if c.is_file():
            if platform.system() != "Windows": c.chmod(c.stat().st_mode | 0o111)
            log(f"  [✓] Installed: {c}"); return c
    log("  [✗] Binary not found after extraction"); return None


def reinstall_boiler(settings, log=print):
    if BOILER_DIR.exists():
        shutil.rmtree(BOILER_DIR)
    return install_boiler(settings, log)


def ensure_boiler(settings, log=print):
    b = find_boiler()
    if b: return b
    log("[⚙]  Installing boiler-writter…")
    return install_boiler(settings, log)


# ════════════════════════════════════════════════════════════════
#  BOILER-WRITTER — calls
# ════════════════════════════════════════════════════════════════

def _run(cmd, timeout, text=False):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, text=text,
                          creationflags=_NO_WINDOW)


def kill_boiler(boiler: Path):
    """Kill leftover instances of OUR boiler-writter (matched by full path),
       falling back to the image name only if the path query is unavailable."""
    full_path = str(boiler.resolve())
    try:
        if platform.system() == "Windows":
            q = _run(["tasklist", "/FI", f"IMAGENAME eq {boiler.name}", "/NH", "/FO", "CSV"],
                     10, text=True)
            if boiler.name.lower() not in (q.stdout or "").lower():
                return                          # nothing running — fast path
            # WQL string literal: backslashes and quotes must be escaped
            wql = full_path.replace("\\", "\\\\").replace("'", "\\'")
            ps = (f"Get-CimInstance Win32_Process -Filter \"ExecutablePath='{wql}'\" "
                  f"| ForEach-Object {{ $_.ProcessId }}")
            try:
                r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         15, text=True)
                pids = [p.strip() for p in (r.stdout or "").splitlines() if p.strip().isdigit()]
                if r.returncode == 0:
                    for pid in pids:
                        _run(["taskkill", "/F", "/PID", pid, "/T"], 5)
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            _run(["taskkill", "/F", "/IM", boiler.name, "/T"], 5)
        else:
            _run(["pkill", "-9", "-f", full_path], 5)
    except (OSError, subprocess.SubprocessError):
        pass                                    # best effort only


def _boiler_call(boiler, args, settings, log, debug=False, _retry=False):
    """Runs boiler-writter. Returns (rc, output_bytes_or_None)."""
    kill_boiler(boiler)
    fd, out_str = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    out = Path(out_str)
    cmd = [str(boiler), str(out)] + [str(a) for a in args]
    if debug: log(f"    [DEBUG] CMD: {' '.join(cmd)}")
    try:
        r = _run(cmd, settings["boiler_timeout"])
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        if debug:
            log(f"    [DEBUG] exit={r.returncode}")
            if stderr: log(f"    [DEBUG] stderr: {stderr[:300]}")
        if r.returncode == 4 and "Already connected" in stderr and not _retry:
            kill_boiler(boiler); time.sleep(6)
            return _boiler_call(boiler, args, settings, log, debug, _retry=True)
        if r.returncode != 0:
            return r.returncode, None
        data = out.read_bytes() if out.exists() else b""
        return 0, data
    except subprocess.TimeoutExpired:
        log("    [!] Boiler timeout"); kill_boiler(boiler); return -1, None
    except OSError as e:
        log(f"    [!] {e}"); return -1, None
    finally:
        out.unlink(missing_ok=True)


def _url_from_raw(data: bytes) -> Optional[str]:
    text = data.decode("latin-1", errors="replace")
    m = _RE_DEMO_URL.search(text)
    if m: return m.group(0)
    for u in _RE_FALLBACK_URL.findall(text):
        if any(w in u.lower() for w in ("dem", "replay", "valve")):
            return u
    return None


def boiler_match(boiler, mid, oid, token, settings, log=print, debug=False):
    """Returns (url, rc, info). info = parsed match dict (or None)."""
    rc, data = _boiler_call(boiler, [mid, oid, token], settings, log, debug)
    if rc != 0:
        log(f"    [!] {BOILER_EXIT_CODES.get(rc, f'code {rc}')}")
        return None, rc, None
    if not data:
        return None, -1, None
    info = None
    try:
        ms = parse_match_list(data)
        info = next((m for m in ms if m["matchid"] == mid), ms[0] if ms else None)
    except ValueError as e:
        if debug: log(f"    [DEBUG] protobuf parse failed: {e}")
    url = (info or {}).get("url") or _url_from_raw(data)
    if debug and info:
        log(f"    [DEBUG] {info}")
    return url, (0 if url else -1), info


def boiler_recent_matches(boiler, settings, log=print, debug=False):
    """Recent matches of the Steam account logged in locally.
       Returns list of match dicts, or None on failure."""
    rc, data = _boiler_call(boiler, [], settings, log, debug)
    if rc != 0:
        log(f"  [✗] GC: {BOILER_EXIT_CODES.get(rc, f'code {rc}')}"); return None
    if not data:
        log("  [✗] Empty GC response"); return None
    try:
        return parse_match_list(data)
    except ValueError as e:
        log(f"  [✗] Unreadable GC response: {e}"); return None


def boiler_test(boiler, settings, log=print, debug=False):
    log("  [🔧] Testing GC connection…")
    ms = boiler_recent_matches(boiler, settings, log, debug)
    if ms is None:
        return False
    log(f"  [✓] GC connected — {len(ms)} recent match(es)")
    for m in ms:
        state = "demo available" if m["url"] else "no demo"
        log(f"      • {m['matchid']}  {describe_match(m)}  — {state}")
    return True


# ════════════════════════════════════════════════════════════════
#  STEAM API — share code chaining
# ════════════════════════════════════════════════════════════════

def next_code(player, known_code, settings):
    """Returns (code, ok, http_status).
       ok=True  → proper answer (code None = end of chain; 202 = no new match yet).
       ok=False → error: 403 wrong key/auth code, 412 share code mismatch
                  (both fatal), anything else transient (-1 = network)."""
    params = {"key": player["api_key"], "steamid": player["steam_id"],
              "steamidkey": player["auth_code"], "knowncode": known_code}
    url, timeout = settings["steam_next_code_url"], settings["http_timeout"]
    try:
        if HAS_REQUESTS:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": USER_AGENT})
            status = r.status_code
            body = r.json() if status == 200 else None
        else:
            full = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status = r.status
                    raw = r.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, b""
                e.close()
            body = json.loads(raw) if status == 200 and raw else None
    except Exception:                       # network / JSON error → transient
        return None, False, -1
    if status == 200:
        c = ((body or {}).get("result") or {}).get("nextcode", "")
        if c and c != "n/a":
            return c, True, 200
        return None, True, 200
    if status == 202:
        return None, True, 202
    return None, False, status


def collect_codes(player, known_ids, settings, log=print, cancel=None):
    start = player.get("last_known_code") or player.get("oldest_share_code", "")
    if not start:
        log("  [!] No share code configured."); return []
    codes = []
    try:
        mid, oid, _ = decode_share_code(start)
        if not is_present(known_ids, mid, oid): codes.append(start)
    except ValueError:
        log(f"  [!] Stored share code is invalid: {start}")
    cur, seen = start, {start}
    failures, max_failures = 0, int(settings["api_max_failures"])
    while True:
        _check_cancel(cancel)
        nxt, ok, status = next_code(player, cur, settings)
        if not ok:
            if status == 403:
                log("  [✗]  API returned 403 — check your API key and auth code"); break
            if status == 412:
                log("  [✗]  API returned 412 — share code mismatch, reset the share code "
                    "with a recent one from this account"); break
            failures += 1
            if failures >= max_failures:
                log(f"  [⚠]  API failed {failures} times in a row, stopping chain"); break
            log(f"  [⚠]  API call failed (HTTP {status}), retrying ({failures}/{max_failures})…")
            _sleep(settings["retry_backoff"] * failures, cancel)
            continue
        failures = 0
        if not nxt or nxt in seen:
            break
        seen.add(nxt); codes.append(nxt); cur = nxt
        _sleep(settings["api_rate_delay"], cancel)
    log(f"  [→] {len(codes)} new match(es)")
    return codes


# ════════════════════════════════════════════════════════════════
#  PHASE 1 — resolve URLs via boiler
# ════════════════════════════════════════════════════════════════

def resolve_urls(codes, boiler, known_ids, dl_path, settings, queued=None,
                 log=print, debug=False, cancel=None, timeline=None):
    """timeline = chain-ordered (code, state) used to advance the cursor safely.
         True  → definitively done, cursor may pass
         False → transient failure, cursor must STOP here
         int   → match id, done only if it lands on disk (download phase)
       queued = {mid: task} shared across players → a match already resolved
       for another player is not sent to the GC again."""
    queued = queued if queued is not None else {}
    tasks, total = [], len(codes)
    stats = {"resolved": 0, "expired": 0, "skipped": 0, "errors": 0,
             "dupes": 0, "timeline": timeline if timeline is not None else []}
    called = False
    for i, code in enumerate(codes, 1):
        _check_cancel(cancel)
        pre = f"  [{i}/{total}]"
        try:
            mid, oid, token = decode_share_code(code)
        except ValueError as e:
            log(f"{pre} [⏭]  Invalid: {code[:40]} ({e})")
            stats["errors"] += 1
            stats["timeline"].append((code, True))       # never decodable
            continue
        if debug:
            log(f"{pre} [DEBUG] mid={mid} oid={oid} tok={token}")
        if mid in queued:
            stats["dupes"] += 1
            stats["timeline"].append((code, mid)); continue
        if is_present(known_ids, mid, oid):
            stats["skipped"] += 1
            stats["timeline"].append((code, True)); continue
        name = csdm_name(mid, oid, token)
        if (dl_path / f"{name}.dem").exists():
            register(known_ids, mid, oid)
            stats["skipped"] += 1
            stats["timeline"].append((code, True)); continue

        if called:
            _sleep(settings["boiler_delay"], cancel)
        called = True
        log(f"{pre} [🔍]  {code}  →  boiler…")
        url, rc, info = boiler_match(boiler, mid, oid, token, settings, log, debug)
        if not url:
            if rc == BOILER_RC_EXPIRED:
                stats["expired"] += 1; log(f"{pre} [⌛]  Expired")
                stats["timeline"].append((code, True))   # permanently gone
            else:
                stats["errors"] += 1; log(f"{pre} [!]   No URL (boiler rc={rc})")
                stats["timeline"].append((code, False))  # transient → retry next run
        else:
            task = {"mid": mid, "oid": oid, "name": name, "url": url, "code": code,
                    "info": info}
            tasks.append(task); queued[mid] = task
            stats["resolved"] += 1
            desc = describe_match(info)
            log(f"{pre} [✓]   URL ready{f'  [{desc}]' if desc else ''}")
            stats["timeline"].append((code, mid))
    return tasks, stats


def recent_gc_tasks(boiler, known_ids, dl_path, settings, queued,
                    log=print, debug=False, cancel=None):
    """Matches listed by the GC for the locally logged-in account that are
       neither on disk nor already queued (e.g. absent from the share code chain)."""
    _check_cancel(cancel)
    log("  [🔍]  Recent matches of the Steam account logged in on this PC…")
    ms = boiler_recent_matches(boiler, settings, log, debug)
    if ms is None:
        return []
    tasks = []
    for m in ms:
        mid, oid, port = m["matchid"], m["reservationid"], m["tv_port"]
        if not (mid and oid and m["url"]):
            continue
        if mid in queued or is_present(known_ids, mid, oid):
            continue
        token = (port or 0) & 0xFFFF
        name = csdm_name(mid, oid, token)
        if (dl_path / f"{name}.dem").exists():
            register(known_ids, mid, oid); continue
        task = {"mid": mid, "oid": oid, "name": name, "url": m["url"],
                "code": encode_share_code(mid, oid, token), "info": m}
        tasks.append(task); queued[mid] = task
        log(f"  [+]  {task['code']}  [{describe_match(m)}]")
    log(f"  [→] {len(tasks)} extra match(es) from the GC list")
    return tasks


# ════════════════════════════════════════════════════════════════
#  PHASE 2 — parallel downloads
# ════════════════════════════════════════════════════════════════

ProgressCb = Callable[[str, int, int, str], None]


def download_one(task, dl_path, known_ids, settings, progress_cb=None, cancel=None):
    """Returns (ok, reason, permanent). reason == SKIPPED when already on disk."""
    mid, oid, name, url = task["mid"], task["oid"], task["name"], task["url"]
    final = dl_path / f"{name}.dem"
    if final.exists() or is_present(known_ids, mid, oid) or on_disk(dl_path, mid, oid):
        register(known_ids, mid, oid)
        return True, SKIPPED, False

    def cb(phase, dl=0, total=0):
        if progress_cb: progress_cb(name, dl, total, phase)

    path_is_bz2 = urllib.parse.urlparse(url).path.endswith(".bz2")
    tmp = dl_path / f"_tmp_{name}{'.dem.bz2' if path_is_bz2 else '.dem'}"
    dem = dl_path / f"_tmp_{name}.dem"
    try:
        cb("downloading")
        ok, err, permanent = http_download(
            url, tmp, settings, on_progress=lambda d, t: cb("downloading", d, t),
            cancel=cancel)
        if not ok:
            cb("failed")
            if permanent:
                return False, f"Demo deleted from Valve's servers ({err})", True
            if any(c in err for c in ("502", "503", "504")):
                return False, f"Valve CDN error (retry next run): {err[:60]}", False
            return False, f"Download failed: {err[:80]}", False

        verr = validate_file(tmp, path_is_bz2)
        if verr:
            cb("failed"); return False, verr, False

        if path_is_bz2:
            cb("extracting")
            ok, berr = decompress_bz2(tmp, dem, cancel)
            tmp.unlink(missing_ok=True)
            if not ok:
                cb("failed"); return False, f"bz2: {berr[:80]}", False
            verr = validate_file(dem, False)
            if verr:
                cb("failed"); return False, f"after decompression: {verr}", False
            dem.replace(final)
        else:
            tmp.replace(final)
    except ScanCancelled:
        cb("cancelled"); raise
    finally:
        tmp.unlink(missing_ok=True)
        dem.unlink(missing_ok=True)

    register(known_ids, mid, oid)
    cb("done")
    return True, "", False


def download_all(tasks, dl_path, known_ids, settings, log=print,
                 progress_cb=None, cancel=None):
    """Returns (downloaded, skipped, permanent_failures) task lists."""
    if not tasks: return [], [], []
    n = len(tasks)
    workers = max(1, min(int(settings["download_workers"]), n))
    log(f"\n[⬇]  {n} demo(s) — {workers} workers\n")
    downloaded, skipped, gone, errors = [], [], [], []
    cancelled = False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_one, t, dl_path, known_ids, settings,
                          progress_cb, cancel): t for t in tasks}
        for f in as_completed(futs):
            task = futs[f]
            nm = task["name"]
            short = ("…" + nm[-40:]) if len(nm) > 41 else nm
            try:
                ok, reason, permanent = f.result()
            except ScanCancelled:
                cancelled = True; continue
            except Exception as e:
                ok, reason, permanent = False, f"{type(e).__name__}: {e}", False
            if ok and reason == SKIPPED:
                skipped.append(task); log(f"  [⏭] {short}  (already on disk)")
            elif ok:
                downloaded.append(task)
                desc = describe_match(task.get("info"))
                log(f"  [✓] {short}{f'  [{desc}]' if desc else ''}")
            else:
                errors.append((short, reason))
                if permanent: gone.append(task)
                log(f"  [✗] {short} — {reason[:80]}")

    if skipped:
        log(f"\n  [⏭] {len(skipped)} already on disk (not re-downloaded)")
    if errors:
        reasons: Dict[str, List[str]] = {}
        for nm, reason in errors:
            reasons.setdefault(reason, []).append(nm)
        log(f"\n  [📋] {len(errors)} failure(s):")
        for reason, names in reasons.items():
            log(f"\n    ▸ {reason}  ({len(names)})")
            for nm in names[:5]: log(f"      — {nm}")
            if len(names) > 5: log(f"      … +{len(names)-5}")
    if cancelled:
        raise ScanCancelled()
    return downloaded, skipped, gone


# ════════════════════════════════════════════════════════════════
#  CURSOR
# ════════════════════════════════════════════════════════════════

def advance_cursor(timeline, done_mids) -> Optional[str]:
    """Last code of the longest fully-done prefix of the chain (None = no move).
       Stopping at the first hole guarantees failed matches are retried."""
    cursor = None
    for code, state in timeline:
        done = state is True or (state is not False and state in done_mids)
        if not done:
            break
        cursor = code
    return cursor


# ════════════════════════════════════════════════════════════════
#  SCAN
# ════════════════════════════════════════════════════════════════

def run_scan(cfg, dl_path: Path, log=print, progress_cb=None, cancel=None,
             debug=False, config_path: Path = None):
    """Full scan. Saves the advanced cursors even when cancelled.
       Returns a summary dict (or None if nothing could start)."""
    settings = get_settings(cfg)
    players = cfg.get("players", [])
    if not players and not settings["fetch_recent_gc_matches"]:
        log("\n  [!] No players — add one first."); return None
    if not dl_path or not Path(dl_path).is_dir():
        log(f"\n  [✗] Demo folder not found: {dl_path} — plug the drive in or "
            f"choose another folder."); return None
    boiler = ensure_boiler(settings, log)
    if not boiler:
        log("\n  [✗] boiler-writter unavailable."); return None
    log(f"\n[⚙]  Boiler: {boiler}")
    log("[⚠]  Steam must be running and logged in. CS2 must be CLOSED.\n")

    known_ids = scan_folder(dl_path)
    cleaned = cleanup_tmp(dl_path)
    if cleaned: log(f"[🧹]  {cleaned} orphaned temp file(s) removed")
    log(f"[📂]  {len(known_ids)} id(s) indexed from existing demos.\n")

    all_tasks, timelines, queued = [], {}, {}
    downloaded, skipped, gone = [], [], []
    cancelled = False
    try:
        for player in players:
            _check_cancel(cancel)
            pid = player["steam_id"]
            log(f"{'═'*56}\n  {player['name']}  ({pid})\n{'═'*56}")
            codes = collect_codes(player, known_ids, settings, log, cancel)
            if not codes:
                log("  [i] Nothing new.\n"); continue
            # shared list: progress made before a cancel still moves the cursor
            timeline = timelines.setdefault(id(player), [])
            tasks, stats = resolve_urls(codes, boiler, known_ids, dl_path, settings,
                                        queued, log, debug, cancel, timeline)
            parts = []
            if tasks:             parts.append(f"{len(tasks)} to download")
            if stats["dupes"]:    parts.append(f"{stats['dupes']} already queued")
            if stats["expired"]:  parts.append(f"{stats['expired']} expired")
            if stats["skipped"]:  parts.append(f"{stats['skipped']} already present")
            if stats["errors"]:   parts.append(f"{stats['errors']} error(s)")
            log(f"  [Σ] {' | '.join(parts) if parts else 'nothing'}")
            if stats["expired"] and not stats["resolved"]:
                log("  [⚠]  All expired — update the share code.")
            all_tasks.extend(tasks)
            log("")

        if settings["fetch_recent_gc_matches"]:
            log(f"{'═'*56}\n  GC recent matches\n{'═'*56}")
            if all_tasks or any(timelines.values()):
                _sleep(settings["boiler_delay"], cancel)
            all_tasks.extend(recent_gc_tasks(boiler, known_ids, dl_path, settings,
                                             queued, log, debug, cancel))
            log("")

        downloaded, skipped, gone = download_all(all_tasks, dl_path, known_ids,
                                                 settings, log, progress_cb, cancel)
    except ScanCancelled:
        cancelled = True
        log("\n  [■] Cancelled — progress so far is kept.")
    finally:
        done = {t["mid"] for t in downloaded + skipped + gone}
        for p in players:
            cursor = advance_cursor(timelines.get(id(p), []), done)
            if cursor: p["last_known_code"] = cursor
        save_config(cfg, config_path)
        cleanup_tmp(dl_path)

    failed = len(all_tasks) - len(downloaded) - len(skipped)
    log(f"\n{'═'*56}")
    log(f"  {'Cancelled' if cancelled else 'Done'} — {len(downloaded)} downloaded, "
        f"{len(skipped)} skipped, {failed if not cancelled else '—'} failed")
    log(f"  {dl_path}")
    log(f"{'═'*56}")
    return {"downloaded": len(downloaded), "skipped": len(skipped),
            "failed": failed, "cancelled": cancelled}
