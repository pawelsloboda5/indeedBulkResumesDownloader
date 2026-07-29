# Indeed CV Downloader

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![Selenium](https://img.shields.io/badge/Selenium-4.16-green?logo=selenium&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**Version 3.0.0**

Bulk download all candidate resumes from your Indeed Employer dashboard in one click. No extension, no manual export — just run and go.

## How It Works

1. **Run the script** (or the `.exe`)
2. **Log in** to your Indeed Employer account in the Chrome window that opens
3. **Choose your options** (download mode, which jobs, status filter)
4. **Let it run** — resumes are downloaded as PDFs, organized by job folder

That's it. On the next run, your session is remembered and already-downloaded CVs are skipped.

## Privacy & Security

- **100% local** — All data stays on your machine. Nothing is sent to any external server.
- **No credentials stored** — The script never sees or saves your password. You log in yourself in Chrome.
- **Session cookies only** — Saved locally in `logs/indeed_cookies.json` to avoid re-login. They expire after ~24h.
- **Open source** — You can read every line of code. No telemetry, no analytics, no tracking.

## Standalone Executable (No Python Required)

The executable is built from source by the **Build Windows .exe** GitHub Actions
workflow, so the binary you run corresponds to a specific commit you can read.

### Quick Start

1. Open the **Actions** tab → **Build Windows .exe** → run it (`workflow_dispatch`)
2. Download `IndeedCVDownloader.exe` from that run's **Artifacts**
3. Double-click to run
4. Log in to your Indeed Employer account in the Chrome window that opens
5. Choose your options in the menu and start downloading!

> No Python, no extension, no configuration needed.

The build log's `certutil` step prints the artifact's SHA-256. Send that
fingerprint alongside the .exe — `HR_GUIDE.md` tells the recipient to verify it
before running, and that instruction only means something if the number is real.

> **Do not distribute `dist/IndeedCVDownloader.exe` from this repo.** It is a
> stale binary inherited from upstream and predates the current code, including
> the per-job `manifest.json` that makes re-runs incremental. Its menus and
> folder layout no longer match `HR_GUIDE.md`.

## Installation (Python)

### 1. Clone the repository

```bash
git clone https://github.com/YasserLoukniti/indeed-cv-downloader.git
cd indeed-cv-downloader
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run

```bash
python indeed_downloader.py
```

### 4. (Optional) Custom configuration

```bash
cp .env.example .env.config
```

## Features

### Authentication
- **Automatic**: Chrome opens, you log in once, cookies are captured and reused
- **Session persistence**: Saved cookies are validated on each run — no re-login until they expire
- **Expiry detection**: If cookies are invalid, the script asks you to log in again

### Download Modes
- **Backend (API)** — Fast parallel downloads via Indeed's GraphQL API
- **Frontend (Selenium)** — Slower but more stable, simulates real browser clicks

### Job Selection
- **Single job** — Navigate to a specific job, press Enter
- **All jobs** — Automatically fetches and processes every job from your dashboard

### Smart Features
- **Resume on interruption** — Checkpoint system lets you stop and restart without losing progress
- **Duplicate detection** — Already downloaded CVs are skipped (by name and ID)
- **New candidates only** — On re-run, only downloads CVs added since last time
- **Folder matching** — Matches existing download folders to jobs by name and date
- **Status filter** — Filter jobs by Open, Paused, or Closed
- **Old job filter** — Jobs older than 2 years are skipped (Indeed archives data)
- **Multi-pass fetch** — Bypasses Indeed's 3000 candidate limit using multiple sort strategies
- **Report generation** — Creates `download_report.txt` with stats per job

## Menu Walkthrough

```
╔════════════════════════════════════════════════════════════╗
║           Indeed CV Downloader - Unified Version           ║
╚════════════════════════════════════════════════════════════╝

DOWNLOAD MODE:
   1. Backend (API) - Faster, parallel downloads
   2. Frontend (Selenium) - More stable, simulated clicks

JOB SELECTION MODE:
   1. Single job - You navigate to the desired job
   2. All jobs - Automatically processes every job

JOB STATUS FILTER:
   1. Open only
   2. Paused only
   3. Closed only
   4. Open + Paused
   5. All
```

### Example: All Jobs Mode

```
145 jobs fetched

Job list:
------------------------------------------------------------
     1. [O] Business Developer
        Date: 22-09-2025 | Candidates: 237
     2. [P] Data Scientist
        Date: 01-07-2025 | Candidates: 550
     3. [F] Marketing Manager
        Date: 15-06-2025 | Candidates: 120
------------------------------------------------------------

JOBS ALREADY PRESENT IN THE DOWNLOADS FOLDER:
============================================================
   Data Scientist:        450 on disk · 550 live
   Marketing Manager:     120 on disk · 120 live
```

There is nothing to answer here. Each job folder's `manifest.json` records who
has already been downloaded, so the run fetches only the applicants that are new
to that folder and leaves the rest untouched. The old `[S]`/`[N]`/`[K]` prompt
was removed — it asked the operator to make a decision the manifest now makes
correctly per job.

## Configuration

Edit `.env.config` to customize parameters:

```bash
# Download speeds
DOWNLOAD_DELAY=0.5              # Delay after clicking download button
NEXT_CANDIDATE_DELAY=1.0        # Delay between candidates

# Download settings
MAX_CVS=3000                    # Max CVs to download per job
PARALLEL_DOWNLOADS=10           # Parallel downloads (backend mode)

# Directories
DOWNLOAD_FOLDER=downloads       # Where CVs are saved
LOG_FOLDER=logs                 # Logs and checkpoints
```

## File Structure

```
indeed-cv-downloader/
├── indeed_downloader.py        # Main script (single file, everything included)
├── manifest.py                 # Per-job download index (stdlib only, no browser)
├── tests/                      # pytest suite; run `python -m pytest tests/` from here
├── requirements.txt            # Python dependencies
├── .env.config                 # Configuration (optional)
├── downloads/                  # Downloaded CVs, organized by job
│   ├── Business Developer (22-09-2025)/
│   │   ├── manifest.json       # Who has been downloaded — drives incremental re-runs
│   │   ├── Jean Dupont/
│   │   │   ├── resume.pdf
│   │   │   ├── application.html    # Only when application data is enabled
│   │   │   └── application.json
│   │   ├── Marie Martin/
│   │   │   └── resume.pdf
│   │   ├── no_cv.txt           # Candidates who attached no CV
│   │   └── stats.json          # Job download statistics
│   └── download_report.txt     # Global download report
└── logs/
    ├── indeed_cookies.json     # Auto-saved session cookies
    └── checkpoint_unified.json # Frontend-mode resume state
```

One folder per applicant, named for them; a second applicant with the same
display name gets a ` (2)` suffix rather than overwriting the first. Each job
folder's `manifest.json` is keyed on the Indeed candidate ID, which is what lets
a re-run fetch only the applicants who arrived since. Keep it with the folder.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Cookies expired | Just re-run — the script will detect it and ask you to log in again |
| Chrome won't open | Close all existing Chrome windows first |
| Jobs missing from list | Check if the page loaded correctly, increase `PAGE_LOAD_DELAY` |
| Downloads failing | Increase `DOWNLOAD_DELAY` in `.env.config` |
| Script interrupted | Re-run it — checkpoint picks up where you left off |

## Legal & Ethics

- For personal use only
- Respect Indeed's Terms of Service
- Only download CVs you have legitimate access to
- Handle candidate data responsibly per GDPR/privacy laws

## License

MIT License - See LICENSE file for details

## Contributing

Pull requests welcome. For major changes, open an issue first.

---

## Changelog

### v3.0.0

**Breaking Changes:**
- **Integrated authentication**: No more browser extension or cookie conversion needed
- Removed `convert_cookies.py` and `ConvertCookies.exe` (no longer needed)
- Single script does everything

**New Features:**
- **Auto-detect login status**: Checks if saved cookies are still valid on startup
- **Interactive login flow**: Opens Chrome and waits for you to log in, then captures cookies automatically
- **Cookie persistence**: Saves cookies to `logs/indeed_cookies.json` for reuse across sessions

**Improvements:**
- Replaced all bare `except: pass` with specific exception types
- Added `PARALLEL_DOWNLOADS` to configuration
- Improved error handling throughout
- Cleaner code organization

### v2.5.0 (2025-11-27)

- Download report generation (`download_report.txt`)
- Job completion tracking with `stats.json`
- Archived candidates handling

### v2.4.0 (2025-11-27)

- Auto-filter jobs older than 2 years
- Track candidates without CV (`no_cv.txt`)

### v2.3.x (2025-11-27)

- Auto-close modals, normalized name matching, duplicate detection fixes

### v2.2.0 (2025-11-27)

- Multi-pass fetch to bypass 3000 candidate limit
- Per-job checkpoint system

### v2.1.0 (2025-11-27)

- Unified single script, interactive menu, HTML table parsing

### v1.0.0 (2025-11-15)

- Initial release
