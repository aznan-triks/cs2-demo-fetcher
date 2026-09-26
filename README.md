# CS2 Demo Fetcher

A desktop tool that automatically retrieves your CS2 matchmaking demos — the same way [Leetify](https://leetify.com) and (in some ways) [CS:DM](https://github.com/akiver/cs-demo-manager) do it.

![GUI Screenshot](https://img.shields.io/badge/GUI-tkinter-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-brightgreen?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey?style=flat-square)

## Versions

There are two versions of the tool available:

- **GUI** (`cs2_demo_fetcher_GUI.py`) — A graphical user interface for ease of use.
- **CLI** (`cs2_demo_fetcher_CLI.py`) — A command-line interface for terminal/console usage.

Both are thin front-ends over the same engine (`cs2_demo_core.py`) and share the same configuration file, so they behave identically.

## How It Works

```
Share Code → Steam API (chain to newer matches)
                ↓
         Decode share code → matchId + outcomeId + token
                ↓
         boiler-writter → Game Coordinator → demo URL + game mode
                ↓
         Fetch .dem.bz2 → validate → decompress → match730_XXX_YYY_ZZZ.dem
```

1. **Share code chaining** — Starting from a known share code, the tool calls Steam's `GetNextMatchSharingCode` API to discover all newer matches
2. **Game Coordinator** — Uses [boiler-writter](https://github.com/akiver/boiler-writter) to communicate with Valve's Game Coordinator and retrieve the demo URL. The answer is decoded to show each match's mode and map
3. **Recent GC matches** — The recent matches of the Steam account logged in on this PC are also fetched, which catches matches missing from the share code chain
4. **Parallel transfers** — Retrieves up to 4 demos simultaneously, checks they are complete and valid, and decompresses `.bz2` archives
5. **Deduplication** — Scans your demo folder to skip already-fetched matches, and deduplicates across players who shared the same match

## Supported modes

Every matchmaking mode Valve records a demo for is retrieved — nothing is filtered:

| Mode | Players | Shown as |
|---|---|---|
| Premier / Competitive | 5v5 | `5v5` |
| Wingman | 2v2 | `Wingman` |
| Rush (added in the *Rush Hour* update, 22 Sep 2026) | 3v3 | `Rush 3v3` |

The mode is detected from the number of players in the match. Rush and unrecognized matches also log their raw `game_type` value (useful for bug reports).

> **Note on Rush:** it is not yet confirmed that Rush matches appear in Steam's share code chain. If they don't, they are still retrieved through the *recent GC matches* step — but only for the Steam account logged in on this PC.

## Features

- 🖥️ **GUI** — Clickable interface built with tkinter, no terminal needed
- 🔄 **Automatic boiler-writter installation** — Fetches the correct binary for your platform from GitHub (sha256 verified)
- 👥 **Multi-player support** — Track demos for multiple Steam accounts
- 🎮 **All modes** — Premier, Competitive, Wingman and Rush
- ⚡ **Parallel transfers** — 4 concurrent workers with retry and exponential backoff
- 🔍 **File validation** — Detects expired demos, incomplete transfers and truncated archives before saving anything
- ■ **Cancel anytime** — Cancel button (GUI) or Ctrl+C (CLI); progress already made is kept
- 🧹 **Orphan cleanup** — Removes leftover temp files from interrupted runs
- 🎨 **Color-coded log** — Green for success, red for errors, yellow for warnings
- 📊 **Progress tracking** — Real-time progress bar during transfers
- 🔒 **Safe config** — Atomic writes; a damaged `config.json` is reported, never overwritten
- 💾 **Unplugged drive safe** — If the demo folder is on a missing drive, the setting is kept and the scan waits for it
- 🛡️ **Safe archive extraction** — Protects against path traversal attacks

## 🛡️ Safety & Privacy

This tool operates entirely locally on your machine using your own Steam API key and match sharing codes to communicate directly with Valve's servers. It does not exfiltrate keys, track your usage, or send your data to any third-party services.

## Prerequisites

- **Python 3.10+**
- **Steam** must be running and logged in
- **CS2** must be **closed** (boiler-writter needs exclusive access to the Game Coordinator)

### Required credentials (per player)

| Credential | Where to get it |
|---|---|
| **Steam API Key** | [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) |
| **Auth Code** | [help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128](https://help.steampowered.com/en/wizard/HelpWithGameIssue/?appid=730&issueid=128) |
| **SteamID64** | [steamid.io](https://steamid.io/) |
| **Share Code** | CS2 → Watch → Your Matches → Copy Share Link / or in the **Auth Code** page |

## Installation

### Option 1: Clone and run

```bash
git clone https://github.com/aznan-triks/cs2-demo-fetcher.git
cd cs2-demo-fetcher
pip install -r requirements.txt
python cs2_demo_fetcher_GUI.py
```

### Option 2: Extract from release

1. Extract the [latest release](https://github.com/aznan-triks/cs2-demo-fetcher/releases) to your desired location
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python cs2_demo_fetcher_GUI.py` (or `run_GUI.bat` on Windows) — CLI: `python cs2_demo_fetcher_CLI.py` (or `run.bat`)

> **Note:** `requests` is optional but recommended. The tool falls back to `urllib` if not installed.

## Usage

### 1. Add a player

Click **+ Add** and fill in the form:

- **Nickname** — Display name (anything you want)
- **SteamID64** — 17-digit Steam ID
- **Steam API Key** — From the link above
- **Auth Code** — From the link above (format: `XXXX-XXXXX-XXXX`)
- **Share Code** — From your most recent CS2 match (format: `CSGO-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`)

### 2. Set demo folder

Click **Change…** next to the folder path and select where demos should be saved.

### 3. Scan & Download

Click **▶ Scan & Download**. The tool will:

1. Chain share codes to find all new matches per player
2. Resolve demo URLs via boiler-writter
3. Add the recent matches of the Steam account logged in on this PC
4. Retrieve and decompress demos in parallel
5. Save progress so the next scan picks up where it left off

Click **■ Cancel** to stop; player editing is locked while a scan runs.

### Other actions

| Button | Description |
|---|---|
| **✏ Edit** | Modify a player's credentials (or double-click the player) |
| **✗ Remove** | Delete a player |
| **🔄 Reset Share Code** | Set a new starting share code for a player |
| **🔧 Test GC Connection** | Verify boiler-writter can reach the Game Coordinator and list your recent matches with their mode |
| **📦 Reinstall Boiler** | Re-fetch boiler-writter (useful after updates) |

## Advanced settings

`config.json` contains a `settings` block (created automatically with the defaults below). Edit it only if needed:

| Key | Default | Meaning |
|---|---|---|
| `download_workers` | 4 | Parallel downloads |
| `boiler_delay` | 4 | Seconds between two Game Coordinator calls |
| `api_rate_delay` | 0.4 | Seconds between two Steam API calls |
| `max_retries` / `retry_backoff` | 3 / 2 | Download attempts and base wait (doubled each retry) |
| `http_timeout` / `download_timeout` / `boiler_timeout` | 15 / 180 / 45 | Timeouts in seconds |
| `api_max_failures` | 5 | Consecutive Steam API failures before giving up on a player |
| `fetch_recent_gc_matches` | true | Also fetch the logged-in account's recent matches |

## File Structure

```
cs2-demo-fetcher/
├── cs2_demo_fetcher_GUI.py   # GUI front-end
├── cs2_demo_fetcher_CLI.py   # CLI front-end
├── cs2_demo_core.py          # Shared engine
├── tests/                    # python -m unittest discover -s tests
├── config.json               # Auto-generated config (players + settings)
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── LICENSE
└── boiler/                   # Auto-fetched boiler-writter binary
    └── bin/
        └── boiler-writter.exe
```

## Demo Naming Convention

```
match730_003811164091523793407_0000000466_55788.dem
         └──── matchId ──────┘ └────┬───┘ └─┬─┘
              outcomeId (lower 32 bits) ┘    └ token
```

Files from [CS:DM](https://github.com/akiver/cs-demo-manager), [Leetify](https://leetify.com) and other tools are recognised too (see below).

## How Deduplication Works

The tool scans all `.dem` files in your demo folder (including subfolders) at startup and extracts every long number from each filename — match ID or reservation ID, whichever tool wrote the file. Any match already present is skipped — no database or index file needed. The folder itself is the source of truth.

Matches shared across multiple tracked players are resolved and retrieved only once.

## Limitations

- **30-day expiration** — Valve deletes demo files after ~30 days. Expired matches cannot be recovered by any tool.
- **Matchmaking only** — Only Valve MM demos (Premier, Competitive, Wingman, Rush). FACEIT/ESEA demos are not supported.
- **One Steam account on the machine** — boiler-writter connects through the locally running Steam client. It uses whichever account is currently logged in.
- **CS2 must be closed** — boiler-writter briefly launches CS2 in the background to communicate with the Game Coordinator.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Steam not running` | Steam client is not open | Launch Steam and log in |
| `User not logged in / GC busy` | Steam is open but GC is unresponsive | Close CS2, wait a few seconds, retry |
| `Match not found (expired > 30 days)` | Demo deleted by Valve | Nothing to do — update share code to a recent match |
| `Demo deleted from Valve's servers` | The URL exists but the file is gone (404) | Nothing to do — the scan moves on |
| `HTML response (demo expired)` | URL works but Valve returns an error page | Retried on the next scan |
| `API returned 412` | Steam rejects the stored share code for this account | Reset the share code with a recent one |
| `Demo folder not found` | Drive unplugged / folder moved | Plug the drive in, or click **Change…** |
| `config.json is unreadable` | The config file is damaged | Fix it or restore `config.json.bak` — it is never overwritten |
| `boiler-writter timeout` | GC is unreachable or CS2 is running | Close CS2 completely, retry |
| `WinError 32` | File locked by another process | Close any program using the demo folder, retry |
| No new matches found | `last_known_code` is already up to date | Play a new match or reset the share code |

## How to Get a New Share Code

1. Open **CS2**
2. Go to **Watch** → **Your Matches**
3. Click on any recent match
4. Click **Copy Share Link**
5. Paste into the tool (Add/Edit player, or Reset Share Code)

## Credits

- **[boiler-writter](https://github.com/akiver/boiler-writter)** by [akiver](https://github.com/akiver) — Game Coordinator communication
- **[csgo-sharecode](https://github.com/akiver/csgo-sharecode)** by [akiver](https://github.com/akiver) — Share code encoding/decoding reference
- **[CS Demo Manager](https://github.com/akiver/cs-demo-manager)** by [akiver](https://github.com/akiver) — Game Coordinator message layout and map ids
- **Valve** — Steam Web API and Game Coordinator protocol
