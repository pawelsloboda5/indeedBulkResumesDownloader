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

**If you picked "All jobs":** the tool fetches the full list, shows you how many
applicants each job already has on disk versus how many Indeed is showing, then
gets to work. There is nothing to answer. It downloads only the applicants that
are new to each folder, so re-running a job you have already done is quick and
safe.

## Step 5 — Watch the progress bar

The console shows a progress bar like:
```
Business Developer:  45/237 |██████░░░░░░░░░░░| 19%
```

PDFs are appearing in `downloads\<Job Name>\<Candidate Name>\`, one folder per applicant, as this runs. You can leave it and come back.

**If you need to stop:** just close the console window. The tool updates the job's record after every applicant, so the next time you run it, it picks up exactly where it left off.

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
- `downloads\<Job Name>\<Candidate Name>\resume.pdf` — one folder per applicant
- `downloads\<Job Name>\no_cv.txt` — applicants who applied without attaching a CV
- `downloads\<Job Name>\manifest.json` — the record of who has already been downloaded
- `downloads\download_report.txt` — overall summary across all jobs

Leave `manifest.json` in the folder. It is how the tool knows who it already has, so
running the same job again downloads only the people who applied since last time.

`no_cv.txt` may list more people than it used to. It now covers everyone the tool has no
usable resume for — someone who applied without attaching one, and also anyone whose
resume file is missing or came down empty on an earlier run. That's a more accurate
count, not a new problem: the tool tries those people again the next time you run the
job, as long as Indeed is still listing them on it. Anyone Indeed has stopped returning —
an applicant who withdrew, or an old posting whose applicants Indeed has archived —
stays on the list without being retried, because the tool has no way left to fetch them.

Close the console window when you're satisfied.

---

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| Chrome doesn't open | Another Chrome with the same profile is still running | Close all Chrome windows and double-click the .exe again |
| "Cookies expired or invalid" or login page reappears | Your saved session expired (happens ~once a day) | Just log in again in the Chrome window when prompted |
| A job downloads 0 CVs and 50 failed | Indeed rate-limited the API briefly | Re-run; the tool only fetches the applicants it doesn't already have |
| `API returned 0 candidates but 33 are already on disk` | Indeed sent back nothing for a job you've already downloaded — usually an expired session or a rate limit. The tool leaves that job's folder completely alone rather than risk wiping a good record | Log in again in the Chrome window and re-run the job. If Indeed has genuinely aged out every applicant on an old job, you'll see this message every time you run it — that's expected, and your existing folder is safe |
| `⚠ Skipping — no Indeed employerJobId on the jobs-table link` | That specific job's row in your dashboard doesn't expose a real Indeed job ID (usually very old / archived jobs) | Skip it with Backend mode; re-run just that one job using Frontend mode (Option 2) |
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
- When you're done with the whole project, **delete the entire `logs\` folder.** This is
  safe to do at any time. The record of who has already been downloaded lives in each
  job folder (`manifest.json`), not in `logs\`.

**Downloaded CVs — treat per GDPR / your company policy:**
- The `downloads\` folder contains candidate PII.
- Move it into the company's document management system (wherever HR normally stores candidate records) as soon as you can.
- Delete the local `downloads\` folder after it's been archived centrally.
- If you want to keep topping a job up later, copy the job's folder back next to the
  .exe before re-running. It carries its own `manifest.json`, so the tool picks up
  exactly where it left off instead of downloading everyone again.
- Don't leave it on a personal laptop indefinitely.

**On the .exe itself:**

Every build of the .exe has its own SHA-256 fingerprint — a long string of letters and
numbers. If even one byte of the file changes on its way to you, the fingerprint changes
completely. Checking it is how you know you're running the file Pawel actually built.

- **Pawel sends the expected fingerprint in the same message as the .exe.** It is not
  printed in this guide: the guide is written once, and the fingerprint changes every
  time the tool is rebuilt, so a number here would go out of date and you'd have no way
  to tell. Use the one in the message that came with your copy.
- If Pawel sends the .exe inside a `.zip`, **extract it first** — check the extracted
  `.exe`, not the zip.
- In PowerShell, in the folder where you saved the .exe:
  ```
  Get-FileHash IndeedCVDownloader.exe -Algorithm SHA256
  ```
- Compare the `Hash` value it prints with the one in Pawel's message. Upper- and
  lower-case don't matter; the characters themselves do.
- If the hash doesn't match what Pawel gave you, **stop and tell Pawel** — the file may
  have been altered in transit. Don't run it.
- If you didn't get a fingerprint with your copy, ask Pawel for it before running the
  .exe rather than skipping the check.

---

## Quick reference card (print this)

```
1.  Double-click IndeedCVDownloader.exe
2.  Log in to Indeed in the Chrome window that opens
3.  Back in the console:  type  1  (Backend)      Enter
                          type  2  (All jobs)     Enter   [or 1 for single job]
                          type  4  (Open+Paused)  Enter   [or your preferred status]
4.  Wait for the progress bar to finish
5.  Find your CVs in:  downloads\<Job Name>\<Candidate Name>\resume.pdf
6.  When done with the project:  delete the  logs\  folder
                                  move  downloads\  to the central archive
                                  keep each job folder's  manifest.json  if you
                                  might top that job up later
```
