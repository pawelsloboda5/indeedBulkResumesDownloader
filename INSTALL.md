# Install, and what to do with the log files

Two pages. `HR_GUIDE.md` covers using the tool; this covers getting it onto a
machine and handling the files it leaves behind.

## Install

**1. Get the .exe.**
GitHub → **Actions** → **Build Windows .exe** → open the most recent green run →
**Artifacts** → download `IndeedCVDownloader`. It arrives as a `.zip`.

**2. Unzip it.** Right-click → Extract All. You want the `.exe` out of the zip
before the next step — hashing the zip gives a different number.

**3. Check the fingerprint.** Open PowerShell in the folder holding the `.exe`:

```powershell
Get-FileHash IndeedCVDownloader.exe -Algorithm SHA256
```

Compare it to the fingerprint sent with the file. It is printed on the build
run's summary page, under the SHA-256 heading.

Upper or lower case does not matter. **If it does not match, stop and ask** —
that means the file changed between the build and your machine.

**4. Put it in its own folder.** Something like `C:\Users\<you>\IndeedDownloader\`.
On first run it creates `downloads\` and `logs\` beside itself, so give it a
folder of its own rather than dropping it in Downloads.

**5. Double-click it.** Chrome opens; sign in to Indeed Employer yourself. The
tool never sees your password. From here, follow `HR_GUIDE.md`.

> Do not use `dist\IndeedCVDownloader.exe` from the repository. That one is old
> and its menus no longer match the guide.

## Where the files end up

Everything lands beside the `.exe`:

```
IndeedDownloader\
├── IndeedCVDownloader.exe
├── downloads\
│   ├── download_report.txt          Summary of the run — counts only
│   └── Cook (12-05-2026)\           One folder per job
│       ├── manifest.json            Who has been downloaded. KEEP THIS.
│       ├── no_cv.txt                Applicants who attached no resume
│       ├── stats.json               Per-job counts
│       └── Jane Dupont\             One folder per applicant
│           ├── resume.pdf
│           ├── application.html     Only if you chose to save application data
│           └── application.json
└── logs\
    ├── latest.log                   Copy of the most recent run's log
    ├── run_20260729_211530.log      One per run, kept forever
    ├── indeed_cookies.json          Your live Indeed session
    ├── checkpoint_unified.json      Progress tracking
    ├── app_data_urls.json           Diagnostic endpoint list
    └── chrome_profile\              Only if you used Attach mode
```

**Keep `manifest.json`** in each job folder. It is how a re-run knows to fetch
only the applicants who arrived since, instead of downloading everyone again.

## If you are asked to send a log

Send **one file**: `logs\latest.log`.

Attach that file specifically. Do not zip the `logs\` folder — it also holds
your live Indeed session, which nobody should ever receive.

**What is in `latest.log`:** every line the console showed you, plus diagnostic
detail. It includes **applicant names**, job titles, and candidate ID codes. It
does not include resume contents, applicant emails or phone numbers.

Because it contains applicant names, send it the way your company expects
candidate information to be handled — internal email or Teams to the person who
asked, not a public link or an outside address.

## Never share these

| File | Why |
|---|---|
| `logs\indeed_cookies.json` | Your live Indeed session. Anyone holding it can act as you on Indeed for about a day. |
| `logs\chrome_profile\` | Same thing in another form, if you used Attach mode. |
| Anything from `downloads\` | Applicant resumes and personal details. |

If you ever paste browser output for debugging — a "Copy as cURL", a Network tab
screenshot — **ask first**. That output normally carries your session token.

## Cleaning up

- Move `downloads\` into wherever candidate records normally live, as soon as
  the run is done.
- Delete `logs\` when the whole project is finished. If you used **Backend**
  mode (option `1`) you can delete it any time. If you used **Frontend** mode
  (option `2`), deleting it mid-project means those jobs re-download applicants
  you already have — nothing is lost, it just costs another pass.
- Old `run_*.log` files accumulate and are never pruned. They each contain
  applicant names, so clear them out with the rest of `logs\`.

## If it will not start

| What you see | Do this |
|---|---|
| Console flashes and closes | Ask for help — this needs the log, and it may not have been written |
| "Cookies expired or invalid" | Delete `logs\indeed_cookies.json` and run again |
| Fingerprint does not match | Stop. Re-download the artifact from the build run |
| Chrome will not open | Close every Chrome window, then retry |
