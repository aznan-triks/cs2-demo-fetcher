#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Demo Fetcher — GUI Edition
Downloads demos via Steam API (auth code, like Leetify).
All the logic lives in cs2_demo_core.py (shared with the CLI).
"""

import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cs2_demo_core as core

# ════════════════════════════════════════════════════════════════
#  THEME
# ════════════════════════════════════════════════════════════════

PAL = {
    "bg": "#1e1e1e", "fg": "#d4d4d4", "alt_bg": "#252526",
    "sel_bg": "#094771", "sel_fg": "#ffffff",
    "btn_bg": "#333333", "btn_hov": "#404040",
    "link": "#4a90d9", "accent": "#007acc",
    "ok": "#4ec9b0", "fail": "#f44747", "warn": "#dcdcaa", "info": "#569cd6",
    "dim": "#6a6a6a",
}

AUTH_HELP_URL = "https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128"


def _apply_theme(root):
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(".", background=PAL["bg"], foreground=PAL["fg"],
                    fieldbackground=PAL["alt_bg"], insertcolor=PAL["fg"],
                    troughcolor=PAL["bg"],
                    selectbackground=PAL["sel_bg"], selectforeground=PAL["sel_fg"])
    style.configure("TButton", background=PAL["btn_bg"], borderwidth=0,
                    focusthickness=1, focuscolor=PAL["sel_bg"])
    style.map("TButton", background=[("active", PAL["btn_hov"])],
              foreground=[("disabled", PAL["dim"])])
    style.configure("TLabel", background=PAL["bg"])
    style.configure("TLabelframe", background=PAL["bg"], bordercolor=PAL["btn_bg"])
    style.configure("TLabelframe.Label", background=PAL["bg"])
    style.configure("TScrollbar", background=PAL["btn_bg"],
                    troughcolor=PAL["bg"], arrowcolor=PAL["fg"], borderwidth=0)
    style.configure("Horizontal.TProgressbar",
                    troughcolor=PAL["alt_bg"], background=PAL["accent"])
    style.configure("Action.TButton", padding=(10, 6))
    root.configure(bg=PAL["bg"])


def _entry(parent, **kw):
    return tk.Entry(parent, width=45, bg=PAL["alt_bg"], fg=PAL["fg"],
                    insertbackground=PAL["fg"], relief="flat",
                    highlightthickness=1, highlightcolor=PAL["accent"],
                    highlightbackground=PAL["btn_bg"], **kw)


def _dialog_buttons(parent, ok, cancel):
    bf = tk.Frame(parent, bg=PAL["bg"])
    for text, cmd in [("OK", ok), ("Cancel", cancel)]:
        tk.Button(bf, text=text, command=cmd, width=12,
                  bg=PAL["btn_bg"], fg=PAL["fg"],
                  activebackground=PAL["btn_hov"], activeforeground=PAL["fg"],
                  relief="flat", cursor="hand2").pack(side="left", padx=5)
    return bf


# ════════════════════════════════════════════════════════════════
#  GUI — PLAYER DIALOG
# ════════════════════════════════════════════════════════════════

class PlayerDialog(tk.Toplevel):
    FIELDS = [
        ("name",            "Nickname:"),
        ("steam_id",        "SteamID64:"),
        ("api_key",         "Steam API Key:"),
        ("auth_code",       "Auth Code:"),
        ("last_known_code", "Share Code (start):"),
    ]
    LINKS = {
        "api_key":         ("Get yours here", "https://steamcommunity.com/dev/apikey"),
        "auth_code":       ("Get yours here", AUTH_HELP_URL),
        "last_known_code": ("Same page as Auth Code", AUTH_HELP_URL),
    }
    SECRET_FIELDS = {"api_key", "auth_code"}

    def __init__(self, parent, title="Player", data=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=PAL["bg"])
        self.resizable(False, False)
        self.result = None
        d = data or {}
        self.entries = {}

        for i, (key, label) in enumerate(self.FIELDS):
            val = d.get(key, "")
            if key == "last_known_code" and not val:
                val = d.get("oldest_share_code", "")
            tk.Label(self, text=label, bg=PAL["bg"], fg=PAL["fg"]
                     ).grid(row=i, column=0, sticky="e", padx=(10, 5), pady=4)
            is_secret = key in self.SECRET_FIELDS
            e = _entry(self, show=("•" if is_secret else ""))
            e.insert(0, val)
            e.grid(row=i, column=1, padx=(0, 5), pady=4)
            self.entries[key] = e

            if is_secret:
                btn = tk.Button(self, text="👁", width=2, relief="flat",
                                bg=PAL["btn_bg"], fg=PAL["fg"],
                                activebackground=PAL["btn_hov"], cursor="hand2")
                btn.config(command=lambda entry=e, b=btn: (
                    entry.config(show="" if entry.cget("show") else "•"),
                    b.config(text="🙈" if not entry.cget("show") else "👁")))
                btn.grid(row=i, column=3, padx=(2, 0), pady=4)

            link = self.LINKS.get(key)
            if link:
                lnk = tk.Label(self, text=link[0], bg=PAL["bg"], fg=PAL["link"],
                               cursor="hand2", font=("TkDefaultFont", 9, "underline"))
                lnk.bind("<Button-1>", lambda _e, u=link[1]: webbrowser.open(u))
                lnk.grid(row=i, column=2, sticky="w", padx=(0, 10))

        _dialog_buttons(self, self._ok, self.destroy).grid(
            row=len(self.FIELDS), column=0, columnspan=3, pady=10)

        self.entries["name"].focus_set()
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _ok(self):
        data = {k: e.get().strip() for k, e in self.entries.items()}
        if not core.valid_steam_id(data.get("steam_id", "")):
            messagebox.showerror("Error", "Invalid SteamID64 (17 digits).", parent=self); return
        if not data.get("api_key"):
            messagebox.showerror("Error", "API Key is required.", parent=self); return
        if not data.get("auth_code"):
            messagebox.showerror("Error", "Auth Code is required.", parent=self); return
        if not core.valid_share_code(data.get("last_known_code", "")):
            messagebox.showerror("Error", "Invalid share code.", parent=self); return
        self.result = data
        self.destroy()


# ════════════════════════════════════════════════════════════════
#  GUI — SHARE CODE RESET DIALOG
# ════════════════════════════════════════════════════════════════

class ResetCodeDialog(tk.Toplevel):
    def __init__(self, parent, player_name, current, initial):
        super().__init__(parent)
        self.title("Reset Share Code")
        self.configure(bg=PAL["bg"])
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text=f"Player: {player_name}", bg=PAL["bg"], fg=PAL["fg"],
                 font=("TkDefaultFont", 10, "bold")).pack(padx=15, pady=(10, 5))
        tk.Label(self, text=f"Current: {current}", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15)
        tk.Label(self, text=f"Initial: {initial}", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15, pady=(0, 10))
        tk.Label(self, text="New share code:", bg=PAL["bg"], fg=PAL["fg"]).pack(padx=15)

        self.entry = _entry(self)
        self.entry.pack(padx=15, pady=5)
        self.entry.focus_set()
        _dialog_buttons(self, self._ok, self.destroy).pack(pady=10)

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _ok(self):
        code = self.entry.get().strip()
        if not core.valid_share_code(code):
            messagebox.showerror("Error", "Invalid share code.", parent=self); return
        self.result = code
        self.destroy()


# ════════════════════════════════════════════════════════════════
#  GUI — MAIN APPLICATION
# ════════════════════════════════════════════════════════════════

class App(tk.Tk):
    POLL_MS = 50

    def __init__(self, cfg, config_path=None):
        super().__init__()
        _apply_theme(self)
        self.title(f"CS2 Demo Fetcher v{core.__version__}")
        self.geometry("920x660")
        self.minsize(750, 500)

        self.cfg = cfg
        self.config_path = config_path
        self._busy = False
        self._cancel = None
        self._ui_queue = queue.Queue()      # worker threads → main thread
        self._last_progress_update = 0.0
        self._dl_progress = {}
        self._dl_progress_lock = threading.Lock()
        self.dl_path = self._init_dl_path()

        self._build_ui()
        self._set_busy(False)
        self._log("[✓]  Self-test OK")
        self._log(f"[📁]  Folder: {self.dl_path}")
        if not self.dl_path.is_dir():
            self._log(f"[⚠]  Demo folder not found: {self.dl_path} — plug the drive in "
                      f"or click 'Change…'. Your setting was kept.")
        if not core.HAS_REQUESTS:
            self._log("[⚠]  pip install requests (recommended)")
        self._refresh_players()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_id = self.after(self.POLL_MS, self._pump)

    def _save(self):
        core.save_config(self.cfg, self.config_path)

    def _init_dl_path(self):
        cur = self.cfg.get("download_path", "")
        if cur:
            return Path(cur)       # kept even if missing (unplugged drive)
        p = Path.home() / "CS2_Demos"
        p.mkdir(parents=True, exist_ok=True)
        self.cfg["download_path"] = str(p); self._save()
        return p

    # ── BUILD UI ─────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text="📁 Download Folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=str(self.dl_path))
        ttk.Label(top, textvariable=self.folder_var,
                  foreground=PAL["link"]).pack(side="left", padx=(5, 10))
        self.folder_btn = ttk.Button(top, text="Change…", command=self._change_folder)
        self.folder_btn.pack(side="left")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(pane, width=270)
        pane.add(left, weight=0)

        plf = ttk.LabelFrame(left, text="Players")
        plf.pack(fill="both", expand=True, padx=(0, 5))
        self.player_list = tk.Listbox(
            plf, height=8, font=("Consolas", 10),
            bg=PAL["alt_bg"], fg=PAL["fg"],
            selectbackground=PAL["sel_bg"], selectforeground=PAL["sel_fg"],
            highlightthickness=0, relief="flat", exportselection=False)
        self.player_list.pack(fill="both", expand=True, padx=5, pady=5)
        self.player_list.bind("<Double-Button-1>", lambda _e: self._edit_player())

        pb = ttk.Frame(plf)
        pb.pack(fill="x", padx=5, pady=(0, 5))
        self._locked = [self.folder_btn]          # disabled while an operation runs
        for text, cmd in [("+ Add", self._add_player), ("✏ Edit", self._edit_player),
                          ("✗ Remove", self._remove_player)]:
            b = ttk.Button(pb, text=text, command=cmd, width=8)
            b.pack(side="left", padx=2)
            self._locked.append(b)

        af = ttk.LabelFrame(left, text="Actions")
        af.pack(fill="x", padx=(0, 5), pady=(10, 0))

        self.scan_btn = ttk.Button(af, text="▶  Scan & Download",
                                   style="Action.TButton", command=self._scan)
        self.scan_btn.pack(fill="x", padx=8, pady=(8, 4))
        self.cancel_btn = ttk.Button(af, text="■  Cancel", command=self._cancel_scan)
        self.cancel_btn.pack(fill="x", padx=8, pady=(0, 4))
        self._locked.append(self.scan_btn)

        for txt, cmd in [
            ("🔄  Reset Share Code",   self._reset_code),
            ("🔧  Test GC Connection", self._test_gc),
            ("📦  Reinstall Boiler",    self._reinstall_boiler),
        ]:
            b = ttk.Button(af, text=txt, command=cmd)
            b.pack(fill="x", padx=8, pady=2)
            self._locked.append(b)
        ttk.Frame(af).pack(pady=4)

        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        self.log_text = tk.Text(
            right, wrap="word", font=("Consolas", 9),
            bg=PAL["bg"], fg=PAL["fg"], insertbackground=PAL["fg"],
            state="disabled", relief="flat",
            highlightthickness=1, highlightbackground=PAL["btn_bg"])
        scroll = ttk.Scrollbar(right, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        for tag in ("ok", "fail", "warn", "info"):
            self.log_text.tag_configure(tag, foreground=PAL[tag])
        self.log_text.tag_configure("timestamp", foreground=PAL["dim"])

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=10, pady=(0, 10))
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(bot, variable=self.progress_var, maximum=100
                        ).pack(fill="x", side="left", expand=True, padx=(0, 10))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bot, textvariable=self.status_var).pack(side="right")

    # ── THREAD → UI BRIDGE ────────────────────────────────────
    # Tk must only be touched from the main thread: workers enqueue callables.

    def _ui(self, fn):
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self._ui_queue.put(fn)

    def _pump(self):
        try:
            for _ in range(500):
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self._pump_id = self.after(self.POLL_MS, self._pump)

    def destroy(self):
        try:
            self.after_cancel(self._pump_id)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    # ── LOGGING ──────────────────────────────────────────────

    def _log(self, msg, tag=None):
        stamp = datetime.now().strftime("[%H:%M:%S] ")
        msg = str(msg)

        def _do():
            self.log_text.configure(state="normal")
            t = tag
            if not t:
                if   "[✓]" in msg: t = "ok"
                elif "[✗]" in msg: t = "fail"
                elif "[⚠]" in msg or "[⌛]" in msg or "[■]" in msg: t = "warn"
                elif "═" in msg: t = "info"
            for line in msg.split("\n"):
                if line.strip():
                    self.log_text.insert("end", stamp, "timestamp")
                    self.log_text.insert("end", line + "\n", t or ())
                else:
                    self.log_text.insert("end", "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self._ui(_do)

    # ── PROGRESS ─────────────────────────────────────────────

    def _progress_cb(self, name, dl, total, phase="downloading"):
        with self._dl_progress_lock:
            prev = self._dl_progress.get(name)
            if phase == "downloading":
                if total <= 0:
                    if prev is None:
                        self._dl_progress[name] = (0, 0, "downloading")
                    return
                self._dl_progress[name] = (dl, total, "downloading")
            elif phase in ("extracting", "done", "failed"):
                t = prev[1] if prev else 0
                self._dl_progress[name] = (t, t, phase)
            else:                                   # cancelled
                self._dl_progress.pop(name, None)
            agg_dl    = sum(d for d, _, _ in self._dl_progress.values())
            agg_total = sum(t for _, t, _ in self._dl_progress.values())
            snapshot  = list(self._dl_progress.values())

        now = time.monotonic()
        if phase == "downloading" and (now - self._last_progress_update) < 0.1:
            return
        self._last_progress_update = now

        pct = agg_dl * 100 / agg_total if agg_total > 0 else 0
        count = lambda ph: sum(1 for _, _, p in snapshot if p == ph)
        parts = []
        if count("downloading"): parts.append(f"{count('downloading')} downloading")
        if count("extracting"):  parts.append(f"{count('extracting')} extracting")
        if count("done"):        parts.append(f"{count('done')} done")
        if count("failed"):      parts.append(f"{count('failed')} failed")
        msg = f"⬇ {', '.join(parts) or 'processing'}  ({agg_dl >> 20}/{agg_total >> 20} MB)"
        self._ui(lambda p=pct, m=msg: (self.progress_var.set(p), self.status_var.set(m)))

    # ── PLAYER MANAGEMENT ────────────────────────────────────

    def _refresh_players(self):
        def _do():
            sel = self.player_list.curselection()
            self.player_list.delete(0, "end")
            for p in self.cfg.get("players", []):
                self.player_list.insert("end", f"{p['name']}  ({p['steam_id']})")
            if sel and sel[0] < self.player_list.size():
                self.player_list.selection_set(sel[0])
        self._ui(_do)

    def _selected_index(self):
        sel = self.player_list.curselection()
        if not sel:
            messagebox.showinfo("Info", "Select a player first.", parent=self)
            return None
        return sel[0]

    def _add_player(self):
        if self._busy: return
        dlg = PlayerDialog(self, "Add Player")
        if dlg.result:
            d = dlg.result
            self.cfg.setdefault("players", []).append(core.new_player(
                d["name"], d["steam_id"], d["api_key"], d["auth_code"], d["last_known_code"]))
            self._save(); self._refresh_players()
            self._log(f"  [✓] Added: {d['name'] or d['steam_id']}", "ok")

    def _edit_player(self):
        if self._busy: return
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        dlg = PlayerDialog(self, "Edit Player", p)
        if dlg.result:
            d = dlg.result
            p["name"]            = d["name"] or p["name"]
            p["steam_id"]        = d["steam_id"]
            p["api_key"]         = d["api_key"]
            p["auth_code"]       = d["auth_code"]
            p["last_known_code"] = d["last_known_code"]
            self._save(); self._refresh_players()
            self._log(f"  [✓] Updated: {p['name']}", "ok")

    def _remove_player(self):
        if self._busy: return
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        if messagebox.askyesno("Confirm", f"Remove {p['name']}?", parent=self):
            self.cfg["players"].pop(idx)
            self._save(); self._refresh_players()
            self._log(f"  [✓] Removed: {p['name']}", "ok")

    def _reset_code(self):
        if self._busy: return
        idx = self._selected_index()
        if idx is None: return
        p = self.cfg["players"][idx]
        dlg = ResetCodeDialog(self, p["name"], p.get("last_known_code", "—"),
                              p.get("oldest_share_code", "—"))
        if dlg.result:
            p["last_known_code"] = dlg.result
            p["oldest_share_code"] = dlg.result
            self._save()
            self._log(f"  [✓] Share code reset for {p['name']}: {dlg.result}", "ok")

    # ── FOLDER ────────────────────────────────────────────────

    def _change_folder(self):
        if self._busy: return
        start = self.dl_path if self.dl_path.is_dir() else Path.home()
        folder = filedialog.askdirectory(initialdir=str(start),
                                         title="Select download folder", parent=self)
        if folder:
            self.dl_path = Path(folder)
            self.dl_path.mkdir(parents=True, exist_ok=True)
            self.cfg["download_path"] = str(self.dl_path)
            self._save()
            self.folder_var.set(str(self.dl_path))
            self._log(f"[📁]  Folder: {self.dl_path}")

    # ── THREADED ACTIONS ──────────────────────────────────────

    def _set_busy(self, busy, cancellable=False):
        self._busy = busy
        for b in self._locked:
            b.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if (busy and cancellable) else "disabled")
        if not busy:
            self.status_var.set("Ready")
            self.progress_var.set(0)

    def _run_threaded(self, fn, cancellable=False):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running.", parent=self)
            return
        self._cancel = threading.Event() if cancellable else None
        self._set_busy(True, cancellable)

        def worker():
            try:
                fn()
            except core.ScanCancelled:
                self._log("  [■] Cancelled.")
            except Exception as e:
                self._log(f"\n  [✗] Error: {type(e).__name__}: {e}", "fail")
            finally:
                self._ui(lambda: self._set_busy(False))
        threading.Thread(target=worker, daemon=True).start()

    def _cancel_scan(self):
        if self._cancel is not None and not self._cancel.is_set():
            self._cancel.set()
            self.cancel_btn.configure(state="disabled")
            self.status_var.set("Cancelling…")
            self._log("  [■] Cancelling — finishing the current step…")

    def _scan(self):
        def do():
            with self._dl_progress_lock:
                self._dl_progress.clear()
            self._ui(lambda: self.status_var.set("Scanning…"))
            core.run_scan(self.cfg, self.dl_path, log=self._log,
                          progress_cb=self._progress_cb, cancel=self._cancel,
                          config_path=self.config_path)
            self._refresh_players()
        self._run_threaded(do, cancellable=True)

    def _test_gc(self):
        def do():
            settings = core.get_settings(self.cfg)
            boiler = core.ensure_boiler(settings, self._log)
            if boiler:
                self._log(f"\n[⚙]  Boiler: {boiler}")
                core.boiler_test(boiler, settings, self._log)
        self._run_threaded(do)

    def _reinstall_boiler(self):
        def do():
            self._log("\n[📦]  Reinstalling boiler-writter…")
            if not core.reinstall_boiler(core.get_settings(self.cfg), self._log):
                self._log("  [✗] Installation failed.", "fail")
        self._run_threaded(do)

    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno(
                    "Quit", "An operation is running. Quit anyway?\n"
                            "(progress already saved is kept)", parent=self):
                return
            if self._cancel is not None:
                self._cancel.set()
        self.destroy()


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

def _fatal(title, msg):
    try:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(title, msg)
        root.destroy()
    except Exception:
        print(f"{title}: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    core.selftest()
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        _fatal("Config error", str(e))
    App(cfg).mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        _fatal("Startup Error", f"{type(e).__name__}: {e}")
