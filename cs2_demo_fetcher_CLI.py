#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Demo Fetcher — CLI Edition
Downloads demos via Steam API (auth code, like Leetify).
All the logic lives in cs2_demo_core.py (shared with the GUI).
"""

import os
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import cs2_demo_core as core

# Windows consoles default to a legacy code page → emojis would crash print()
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


# ════════════════════════════════════════════════════════════════
#  OUTPUT — log lines + one live progress line (no ANSI cursor moves)
# ════════════════════════════════════════════════════════════════

class Console:
    def __init__(self):
        self._lock = threading.Lock()
        self._active: Dict[str, Tuple[int, int, str]] = {}
        self._line = ""
        self._live = sys.stdout.isatty()      # live line only on a real terminal

    def _clear(self):
        if self._line:
            sys.stdout.write("\r" + " " * len(self._line) + "\r")
            self._line = ""

    def log(self, msg=""):
        with self._lock:
            self._clear()
            print(msg)
            self._draw()

    def progress(self, name, dl, total, phase):
        with self._lock:
            if phase in ("done", "failed", "cancelled"):
                self._active.pop(name, None)
            else:
                self._active[name] = (dl, total, phase)
            self._draw()

    def reset(self):
        with self._lock:
            self._clear(); self._active.clear()
            sys.stdout.flush()

    def _draw(self):
        if not self._live:
            return
        if not self._active:
            self._clear(); sys.stdout.flush(); return
        extracting = sum(1 for _, _, p in self._active.values() if p == "extracting")
        dl = sum(d for d, _, p in self._active.values() if p == "downloading")
        tot = sum(t for _, t, p in self._active.values() if p == "downloading")
        line = f"  [⬇ {len(self._active) - extracting} active"
        if extracting: line += f", 📦 {extracting} extracting"
        if tot: line += f" — {dl >> 20}/{tot >> 20} MB ({dl * 100 // tot}%)"
        line += "]"
        try:
            cols = os.get_terminal_size().columns
            if len(line) > cols - 1:
                line = line[:cols - 2] + "…"
        except OSError:
            pass
        self._clear()
        sys.stdout.write(line); sys.stdout.flush()
        self._line = line


CON = Console()
log = CON.log


# ════════════════════════════════════════════════════════════════
#  PROMPTS
# ════════════════════════════════════════════════════════════════

def ask(p, d=""):
    v = input(f"{p}{f' [{d}]' if d else ''}: ").strip()
    return v or d


def clean_path(r):
    r = r.strip()
    for q in ('"', "'"):
        if r.startswith(q) and r.endswith(q) and len(r) > 1:
            r = r[1:-1].strip()
    return r


def ask_dl_path(cfg) -> Path:
    cur = cfg.get("download_path", "")
    if cur:
        p = Path(cur)
        if p.is_dir(): return p
        print(f"\n[!]  Folder not found: {cur}")
        if ask("  Keep it (drive unplugged) and choose it later? y/n", "y").lower().startswith("y"):
            return p
    while True:
        raw = clean_path(ask("\n[📁]  Download folder", str(Path.home() / "CS2_Demos")))
        path = Path(raw).expanduser().resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
            cfg["download_path"] = str(path); core.save_config(cfg)
            print(f"       → {path}"); return path
        except OSError as e:
            print(f"  [✗] {e}")


def change_dl_path(cfg) -> Path:
    cfg["download_path"] = ""
    return ask_dl_path(cfg)


def _pick_player(cfg) -> Optional[dict]:
    ps = cfg.get("players", [])
    if not ps:
        print("\n  No players."); return None
    list_players(cfg)
    try:
        i = int(ask("\n  Player number")) - 1
        if i < 0: raise IndexError
        return ps[i]
    except (ValueError, IndexError):
        print("  [!] Invalid."); return None


# ════════════════════════════════════════════════════════════════
#  MENU ACTIONS
# ════════════════════════════════════════════════════════════════

def add_player(cfg):
    print("\n" + "─" * 62)
    print("  API Key  : https://steamcommunity.com/dev/apikey")
    print("  Auth code: https://help.steampowered.com/en/wizard/")
    print("             HelpWithGameIssue/?appid=730&issueid=128")
    print("  SteamID  : https://steamid.io/")
    print("─" * 62)
    name = ask("\n  Nickname")
    sid  = ask("  SteamID64")
    if not core.valid_steam_id(sid): print("  [!] Invalid SteamID64."); return
    key  = ask("  Steam API Key")
    if not key: print("  [!] Required."); return
    auth = ask("  Auth code")
    if not auth: print("  [!] Required."); return
    code = ask("  Starting share code")
    if not core.valid_share_code(code): print("  [!] Invalid."); return
    cfg.setdefault("players", []).append(core.new_player(name, sid, key, auth, code))
    core.save_config(cfg)
    print(f"  [✓] {name or sid} added.")


def list_players(cfg):
    ps = cfg.get("players", [])
    if not ps: print("\n  No players."); return
    for i, p in enumerate(ps, 1):
        print(f"\n  {i}. {p['name']} — {p['steam_id']}")
        print(f"       Last code: {p.get('last_known_code', '—')}")
        o = p.get('oldest_share_code', '—')
        if o != p.get('last_known_code', ''):
            print(f"       Initial  : {o}")


def edit_player(cfg):
    p = _pick_player(cfg)
    if not p: return
    print(f"\n  {p['name']} (Enter = keep current)")
    for f, label, check in [
        ("name",            "Nickname",   None),
        ("steam_id",        "SteamID64",  core.valid_steam_id),
        ("api_key",         "API Key",    None),
        ("auth_code",       "Auth code",  None),
        ("last_known_code", "Share code", core.valid_share_code),
    ]:
        val = ask(f"  {label}", p.get(f, ""))
        if val and val != p.get(f, ""):
            if check and not check(val): print("    [!] Invalid, kept old value.")
            else: p[f] = val
    core.save_config(cfg)
    print("  [✓] Updated.")


def remove_player(cfg):
    p = _pick_player(cfg)
    if not p: return
    if ask(f"  Remove {p['name']}? y/n", "n").lower().startswith("y"):
        cfg["players"].remove(p)
        core.save_config(cfg)
        print(f"  [✓] {p['name']} removed.")


def reset_player_code(cfg):
    p = _pick_player(cfg)
    if not p: return
    print(f"\n  {p['name']}")
    print(f"  Current: {p.get('last_known_code', '—')}")
    print(f"  Initial: {p.get('oldest_share_code', '—')}")
    print("\n  1. Revert to initial code\n  2. Enter a new code")
    c = ask("  Choice", "2")
    if c == "1":
        o = p.get("oldest_share_code", "")
        if o:
            p["last_known_code"] = o; core.save_config(cfg)
            print(f"  [✓] → {o}")
        else:
            print("  [!] No initial code.")
    elif c == "2":
        code = ask("  New share code")
        if core.valid_share_code(code):
            p["last_known_code"] = code
            p["oldest_share_code"] = code
            core.save_config(cfg)
            print(f"  [✓] → {code}")
        else:
            print("  [!] Invalid.")


def scan(cfg, dl_path, debug):
    """Ctrl+C cancels the scan cleanly (cursor progress is saved)."""
    cancel = threading.Event()
    result = {}

    def worker():
        try:
            result["r"] = core.run_scan(cfg, dl_path, log=log, progress_cb=CON.progress,
                                        cancel=cancel, debug=debug)
        except Exception as e:
            log(f"\n  [✗] Error: {type(e).__name__}: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        cancel.set()
        log("\n  [■] Cancelling — finishing the current step…")
        while t.is_alive():
            try:
                t.join(0.2)
            except KeyboardInterrupt:
                pass
    CON.reset()


def test_gc(cfg, debug):
    settings = core.get_settings(cfg)
    b = core.ensure_boiler(settings, log)
    if not b: return
    log(f"\n[⚙]  {b}")
    core.boiler_test(b, settings, log, debug)


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

def main():
    print("\n╔══════════════════════════════════════════════════════════╗")
    banner = f"CS2 Demo Fetcher v{core.__version__} — Leetify Style"
    print(f"║{banner.center(58)}║")
    print("╚══════════════════════════════════════════════════════════╝")
    if not core.HAS_REQUESTS:
        print("\n[⚠]  pip install requests\n")

    core.selftest()
    print("[✓]  Self-test OK")

    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        print(f"\n[✗]  {e}\n"); sys.exit(1)
    dl_path = ask_dl_path(cfg)
    debug = False
    print(f"[📁]  Folder: {dl_path}")

    while True:
        print("\n" + "─" * 62)
        print("  1. Scan and download   (Ctrl+C to cancel)")
        print("  2. Add a player")
        print("  3. Edit a player")
        print("  4. Remove a player")
        print("  5. List players")
        print("  6. Change download folder")
        print("  7. Reinstall boiler-writter")
        print("  8. Reset share code")
        print("  9. Test GC connection (lists recent matches + mode)")
        print(f"  D. Debug: {'ON 🟢' if debug else 'OFF ⚪'}")
        print("  0. Quit")
        print("─" * 62)
        c = ask("  Choice", "1").upper()

        if   c == "1": scan(cfg, dl_path, debug)
        elif c == "2": add_player(cfg)
        elif c == "3": edit_player(cfg)
        elif c == "4": remove_player(cfg)
        elif c == "5": list_players(cfg)
        elif c == "6":
            dl_path = change_dl_path(cfg)
            print(f"[📁]  {dl_path}")
        elif c == "7":
            if not core.reinstall_boiler(core.get_settings(cfg), log):
                print("  [✗] Installation failed.")
        elif c == "8": reset_player_code(cfg)
        elif c == "9": test_gc(cfg, debug)
        elif c == "D":
            debug = not debug
            print(f"  Debug {'ON 🟢' if debug else 'OFF ⚪'}")
        elif c == "0":
            print("\n  Goodbye!\n"); break
        else:
            print("  [!] ?")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Interrupted.\n")
        sys.exit(0)
