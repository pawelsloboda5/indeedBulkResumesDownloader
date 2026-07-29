# Next steps — what to do on GitHub

The audit is done and the build workflow is committed on the local branch `build-exe-from-audited-source`. From here, everything happens on GitHub. `gh` CLI is not installed locally, so the steps below are done through github.com in a browser.

## 1. Fork the repo (once)

1. Open https://github.com/YasserLoukniti/indeedBulkResumesDownloader
2. Click **Fork** → create the fork under your account (e.g., `pawelsloboda/indeedBulkResumesDownloader`).
3. GitHub will disable workflows in the fork by default — that is fine, we enable ours in step 3.

## 2. Point this clone at your fork and push

Run these in the project directory (`/Users/pawelsloboda/Desktop/indeed_bulk_resumes_downloader/indeedBulkResumesDownloader`). Replace `<you>` with your GitHub username.

```
git remote rename origin upstream
git remote add origin https://github.com/<you>/indeedBulkResumesDownloader.git
git push -u origin build-exe-from-audited-source
```

`upstream` now points at the original repo (for pulling future updates); `origin` points at your fork (where we push).

## 3. Enable Actions in your fork and run the build

1. On GitHub, go to your fork → **Actions** tab. If it says workflows are disabled, click **I understand my workflows, go ahead and enable them**.
2. Pick the **Build Windows .exe** workflow in the sidebar.
3. Click **Run workflow** → choose branch `build-exe-from-audited-source` → **Run workflow**.
4. Wait ~3–5 minutes for the run to finish.

## 4. Verify + download the artifact

1. Open the completed workflow run. In the **Build** step log, find the line printed by `certutil` with the SHA-256 hash. Copy it.
2. Scroll to the bottom — under **Artifacts** there is `IndeedCVDownloader.zip`. Download it.
3. Unzip it on your Mac. Compute the hash locally and confirm it matches the CI log:

   ```
   shasum -a 256 IndeedCVDownloader.exe
   ```

   Matching means the binary on your disk is byte-identical to what CI built. Keep the hash — you will re-check it on HR's machine.

## 5. Smoke-test on a Windows machine

Before a full production run, test on HR's laptop (or any Windows box) with a single job:

1. Copy `IndeedCVDownloader.exe` over. Re-run `Get-FileHash -Algorithm SHA256 IndeedCVDownloader.exe` in PowerShell and compare with the hash from step 4.
2. Double-click it. Chrome opens — HR logs into Indeed Employer.
3. Choose **Single job** mode, pick one job with 2–3 candidates.
4. Confirm: resumes appear under `downloads/<job>/`, `logs/indeed_cookies.json` is created, and no files appear outside those folders.
5. Open **Resource Monitor → Network** while it runs. You should see connections only to `indeed.com` hosts (and `storage.googleapis.com` or similar the first time, for the ChromeDriver download from Google's Chrome for Testing CDN).

## 6. Hand off to HR

Give HR:

- `IndeedCVDownloader.exe`
- The SHA-256 (so they can verify it matches what you sent — defends against tampering in transit)
- A short note:
  > - Double-click the `.exe`. Chrome will open — log in to Indeed Employer. The script never sees your password.
  > - Pick your download options in the menu. Each applicant gets their own folder:
  >   `downloads/<job>/<Candidate Name>/resume.pdf`. Keep the `manifest.json` that
  >   sits in each job folder — it is what makes a re-run fetch only new applicants.
  > - `logs/indeed_cookies.json` is your active session token. Do not share it. Delete the `logs/` folder when you are done.
  > - The `downloads/` folder holds candidate PII — handle per our GDPR policy and delete when no longer needed.

## 7. After HR is done

- Confirm `downloads/` has been moved into your company's document system (or deleted).
- Delete `logs/` on HR's machine so the session token isn't sitting around.
- If HR needs to run it again later, the `.exe` is safe to reuse as long as its SHA-256 still matches.

---

## If something goes wrong

| Symptom | Most likely cause | Fix |
|---|---|---|
| "Chrome won't open" | Another Chrome window is open with a profile lock | Close all Chrome windows and retry |
| "Cookies expired" on re-run | Session > ~24h | The script detects this and reopens the login flow |
| Workflow fails on `pip install` | A dependency bumped its floor version of Python | **Do not bump `python-version` past `"3.11"`.** See the note below; pin the offending dependency instead |
| SHA-256 on HR's machine differs from CI log | File tampered in transit, or wrong download | Re-download the artifact directly from GitHub |

## Why `python-version` is pinned to 3.11 in both workflows

`undetected-chromedriver==3.5.5` does `from distutils.version import LooseVersion`
in its `patcher.py`. `distutils` was removed from the standard library in Python
3.12 (PEP 632), and upstream has shipped nothing since February 2024, so there is
no release that fixes it.

The failure is the dangerous kind: on 3.12 the import raises, `indeed_downloader`
catches it, and falls back to stock Selenium — which is the mode the code's own
comments say cannot get past Indeed's Cloudflare bot check. **The build stays
green and the .exe silently loses stealth mode.** Leave the pin at `"3.11"` in
both `build-exe.yml` and `tests.yml`.

## Why we are not shipping `dist/IndeedCVDownloader.exe`

The binary in `dist/` (committed by the upstream maintainer) cannot be cryptographically tied to the source in the same commit — no CI build, no signing. The source code passed audit, so the *only* trust gap is the binary. The workflow above closes that gap by rebuilding from the same source on a public, clean Windows runner whose logs you can inspect.
