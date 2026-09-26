# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [3.4.0] - 2026-09-26

Each item: **plain words** — *technical detail*.

### Added
- **Rush mode (3v3) support: Rush demos are downloaded like any other, and every match now shows its mode and map.** — *`cs2_demo_core.parse_match_list` decodes the boiler-writter protobuf (`CMsgGCCStrike15_v2_MatchList`); mode derived from the reserved player count (10 = 5v5, 4 = Wingman, 6 = Rush); raw `game_type` logged for Rush/unknown.*
- **Your latest matches are also fetched directly from Valve, even if the share code chain misses them.** — *`recent_gc_tasks` (boiler-writter without arguments); setting `fetch_recent_gc_matches` (default true).*
- **A Cancel button (GUI) and Ctrl+C (CLI) stop a scan cleanly without losing progress.** — *`threading.Event` checked per API call, GC call and download chunk; cursors saved in `finally`.*
- **"Test GC Connection" lists your recent matches with their mode.** — *`boiler_test`.*
- **Advanced settings can be changed in `config.json`.** — *`settings` block, defaults in `DEFAULT_SETTINGS`.*
- **Automatic tests.** — *`tests/test_core.py` (unittest, 33 tests, fixture `tests/fixtures/gc_recent_matches.bin`).*

### Fixed
- **The CLI no longer pretends to download demos it skipped (and no longer loses them for good).** — *CLI `run_scan` registered queued matches as present before downloading, so `_dl_one` skipped them and the cursor moved past them.*
- **A damaged `config.json` is reported instead of being replaced by an empty one.** — *`load_config` raises `ConfigError`.*
- **An unplugged demo drive no longer resets your demo folder setting.** — *GUI `_init_dl_path` no longer overwrites `download_path`; `run_scan` aborts if the folder is missing.*
- **Interrupted downloads and truncated archives are rejected instead of saved as broken demos.** — *Content-Length check, `BZ2Decompressor.eof` check, `.dem` header check after decompression.*
- **A demo deleted from Valve's servers no longer blocks the next scans forever.** — *HTTP 404/410 treated as permanent (cursor passes), no retries on 4xx.*
- **Without `requests`, wrong API keys and bad share codes are recognised again.** — *urllib fallback now maps `HTTPError` 403/412 and HTTP 202.*
- **Leftover boiler-writter processes are found reliably on recent Windows.** — *`wmic` (removed from Windows 11 24H2, and its WQL path was not escaped) replaced by `tasklist` + `Get-CimInstance`.*
- **Editing a player during a scan can no longer be silently overwritten.** — *player/folder/action buttons disabled while busy.*
- **`run.bat` launches the CLI again.** — *pointed to the non-existent `cs2_demo_downloader.py`.*

### Changed
- **Matches shared by several players are asked to Valve only once (faster scans).** — *`queued` map across players in `resolve_urls`.*
- **GUI and CLI now share one engine, so they can no longer drift apart.** — *new `cs2_demo_core.py`; the CLI now uses the GUI file naming `match730_<mid>_<oid32>_<token>.dem` (dedup still recognises the old CLI names).*
- **The GUI stays responsive and thread-safe.** — *worker → UI updates through a queue pumped by `after()`; no console windows flash for child processes.*
- **The Steam API key is no longer built into the request URL by hand.** — *`params=`.*
- **README rewritten for the actual file names, Rush, cancel and settings.**
- **Version shown in the CLI banner and GUI window title (kept from 3.3.3).** — *`cs2_demo_core.__version__`.*

## [3.3.3] - 2026-08-08

### Fixed
- **Cursor could skip a failed match forever.** The per-player share-code cursor
  used to advance to the newest *successfully downloaded* demo, jumping clean
  over any match that failed earlier in the same chain (e.g. a Valve CDN 502).
  Those matches were silently lost — the "retry next run" message never
  actually retried them. The cursor now advances only through an unbroken run
  of successes and stops at the first failure, so failed matches are
  re-attempted on the next scan. Applied to both the CLI and GUI.
- Replaced the unreliable ~6T "proximity" heuristic used to detect duplicate
  demos with an exact match on match ID / reservation ID (both 64-bit and
  32-bit forms) — the old distance threshold could misclassify genuinely
  distinct matches as duplicates.

### Security
- Verify the `boiler-writter` binary's SHA-256 before use.
- Drop the insecure `mktemp`-style temp file creation.
- Mask secret fields (API key, auth code) in the GUI.
- More precise process termination when stopping `boiler-writter`.
- Added `.gitignore` to keep local config/secrets out of the repository.

### Changed
- Extraction is now verified before the tool reports "done".

## [CS2-demo-fetcher] - 2026-04-02

Initial public release.
