# Indeed CV Downloader — Step-by-step guide

Use this with the `IndeedCVDownloader.exe` file that Pawel sent you. No install needed, no Python, no extension.

---

## Before we start

You'll need:
- Your Indeed Employer login (email + password, plus any 2FA device you normally use)
- Google Chrome installed on the laptop (the tool drives Chrome, so Chrome has to be present — any recent version is fine)
- The `IndeedCVDownloader.exe` file (Pawel will send it — keep it somewhere you can find, e.g., `C:\IndeedDownloader\`)
- A few GB of free disk space (each CV is ~100 KB, so 3,000 CVs ≈ 300 MB — multiply by number of jobs)

Close any open Chrome windows before you start. The tool opens its own Chrome and can get confused if another one is already running.

---

## Step 1 — Put the .exe in its own folder

Example: `C:\IndeedDownloader\IndeedCVDownloader.exe`

Why its own folder: when it runs, it will create two sibling folders next to the .exe — `downloads\` (PDFs) and `logs\` (progress + cookies). Keeping them together makes cleanup easy later.

## Step 2 — Double-click `IndeedCVDownloader.exe`

Two windows open:
- A **black console window** (this is the tool's output — don't close it, we'll come back to it)
- A **Chrome window** (automatically opened by the tool)

If Windows SmartScreen warns "Windows protected your PC" — click **More info** → **Run anyway**. This happens because Pawel's build isn't code-signed; that's expected.

## Step 3 — Log in to Indeed in the Chrome window the tool opened

In the Chrome window the tool just opened, you'll land on https://employers.indeed.com. **Log in as you normally do** (email, password, 2FA).

> The tool never sees your password — Indeed's login page handles that. The tool only captures a session cookie after you're logged in, the same way your browser remembers you between visits.

Once you're on your employer dashboard (you see your jobs listed), **go back to the black console window**.

## Step 4 — Answer the menu in the console window

The menu is in English. Here's what each option means and what Pawel recommends:

```
📥 DOWNLOAD MODE:
   1. Backend (API) - Faster, parallel downloads         ← recommended
   2. Frontend (Selenium) - More stable, simulated clicks ← fallback only
```
Type **`1`** and press Enter.

```
📋 JOB SELECTION MODE:
   1. Single job - You navigate to the desired job     ← first test run
   2. All jobs - Automatically processes every job     ← full bulk run
```
For the live test call: **`1`** and press Enter. For the real bulk run afterwards: **`2`**.

```
📊 JOB STATUS FILTER:
   1. Open only (ACTIVE)
   2. Paused only (PAUSED)
   3. Closed only (CLOSED)
   4. Open + Paused                                     ← common choice
   5. All (Open + Paused + Closed)
```
Most teams want **`4`** (Open + Paused) or **`1`** (Open only). Pick what fits your current need.

**If you picked "Single job":** the tool will pause and ask you to navigate in the Chrome window to the job you want. Click into the job's candidate list on employers.indeed.com, then press Enter in the console.

**If you picked "All jobs":** the tool fetches the full list and shows you what it found. If some jobs already have downloaded folders, it asks what to do:
- **S** — skip all jobs that already have a folder
- **N** — only re-check jobs where new candidates arrived since last time (most common for a repeat run)
- **K** — download everything again regardless

## Step 5 — Watch the progress bar

The console shows a progress bar like:
```
Business Developer:  45/237 |██████░░░░░░░░░░░| 19%
```

PDFs are appearing in `downloads\<Job Name>\` as this runs. You can leave it and come back.

**If you need to stop:** just close the console window. The tool saves a checkpoint after every candidate, so the next time you run it, it picks up exactly where it left off.

## Step 6 — When it's done

The console prints a summary like:
```
============================================================
STATISTICS
============================================================
Total processed:  252
Downloaded:       215
Skipped:          22
Failed:           0

Total time:       0h 8m 43s
Avg/CV:           2.4s
============================================================
```

Files you now have:
- `downloads\<Job Name>\*.pdf` — the resumes
- `downloads\<Job Name>\no_cv.txt` — candidates who applied without attaching a CV
- `downloads\<Job Name>\stats.json` — per-job download statistics
- `downloads\download_report.txt` — overall summary across all jobs

Close the console window when you're satisfied.

---

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Chrome doesn't open | Another Chrome with the same profile is still running | Close all Chrome windows and double-click the .exe again |
| "Cookies expired or invalid" or login page reappears | Your saved session expired (happens ~once a day) | Just log in again in the Chrome window when prompted |
| A job downloads 0 CVs and 50 failed | Indeed rate-limited the API briefly | Re-run; the checkpoint will only retry the failed ones |
| Downloads going much slower than expected | Too many parallel requests, Indeed is throttling | Nothing to do — it'll finish, just slower |
| Windows SmartScreen blocks the exe | The exe isn't code-signed | **More info** → **Run anyway** (one time) |
| Console window instantly closes | Usually a Chrome version mismatch | Ping Pawel — he'll send a fresh build |

---

## Security checklist (important — please read)

These are specific to this tool because you're handling candidate PII and a live Indeed session token.

**Session cookie — treat like a password:**
- The file `logs\indeed_cookies.json` is your active Indeed session. Anyone with this file can act as you on Indeed for ~24 hours.
- **Do not** email, Slack, or share this file with anyone.
- **Do not** commit it to any git repo / SharePoint / shared drive.
- When you're done with the whole project, **delete the entire `logs\` folder.**

**Downloaded CVs — treat per GDPR / your company policy:**
- The `downloads\` folder contains candidate PII.
- Move it into the company's document management system (wherever HR normally stores candidate records) as soon as you can.
- Delete the local `downloads\` folder after it's been archived centrally.
- Don't leave it on a personal laptop indefinitely.

**On the .exe itself:**
- The SHA-256 hash Pawel gave you should match what you see if you run this in PowerShell:
  ```
  Get-FileHash IndeedCVDownloader.exe -Algorithm SHA256
  ```
- If the hash doesn't match, **stop and tell Pawel** — the file may have been altered in transit. Don't run it.

---

## Quick reference card (print this)

```
1.  Double-click IndeedCVDownloader.exe
2.  Log in to Indeed in the Chrome window that opens
3.  Back in the console:  type  1  (Backend)      Enter
                          type  2  (All jobs)     Enter   [or 1 for single job]
                          type  4  (Open+Paused)  Enter   [or your preferred status]
4.  Wait for the progress bar to finish
5.  Find your CVs in:  downloads\<Job Name>\*.pdf
6.  When done with the project:  delete the  logs\  folder
                                  move  downloads\  to the central archive
```
