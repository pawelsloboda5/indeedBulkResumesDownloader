# Re-run aggregation: follow-up work

Carried out of the nine-task manifest branch (`634461e..ec5aab9`). Everything here was found by review, triaged, and deliberately deferred. Ranked by value, not by when it was found.

The branch itself is complete: HR re-runs a job she already downloaded and gets exactly the applicants who arrived since, with the existing ones untouched and never re-fetched.

## 1. Add a test that imports `indeed_downloader`

Nothing under `tests/` imports it. All 79 tests cover `manifest.py` plus an end-to-end harness that *mirrors* the download sequence rather than invoking it, because the real method needs Selenium and a live Indeed session.

That gap has a demonstrated cost, not a theoretical one. Two defects escaped into `indeed_downloader.py` during this branch and were caught only by a human reading the code:

- the mirror drifted from the shipped code and stopped covering a change made in the same task
- the app-data pass discarded a candidate ID it was already holding, which would have misfiled two same-named applicants' screener data into a phantom folder

Neither was catchable by any `manifest.py` test, because both were questions of which argument the downloader passes.

The blocker was that `import indeed_downloader` fails without `selenium`. The CI job added at the end of this branch installs `requirements.txt` alongside `requirements-dev.txt` specifically to unblock this. A test can now build a bare instance with `object.__new__(IndeedDownloader)`, set `download_folder` and `log`, and call `_create_job_folder` against a `tmp_path`.

Start with `_candidate_folder_for`'s call sites: today nothing fails if `indeed_downloader.py:2309` is reverted to `(name)`.

## 2. Record manifest entries from the frontend (Selenium) path

`manifest_mod.record` and `manifest_mod.save` are called only from the API path. The Selenium path writes resumes to disk without recording them, so its folders carry a manifest with no candidates.

The dangerous half is already closed: `manifest.needs_backfill` treats an empty manifest as "recover from disk", so those resumes are found on the next run rather than staying invisible. What remains is that frontend mode does not get the collision suffix from `allocate_candidate_folder`, so two applicants sharing a display name still overwrite each other in one folder.

That is exactly the old build's behavior, so it ships no regression. It does mean the collision fix is backend-only, which is worth saying out loud when this gets picked up.

## 3. Cover the failed-download path

`download_cv_api` deliberately skips `record()` when a download comes back under `MIN_RESUME_BYTES`, unlinking the truncated file first. That is the precise condition `allocate_candidate_folder`'s own docstring names as reopening the folder-overwrite bug: allocation reserves nothing until `record()` is called.

The current code is safe because the unlink runs first and no path writes a good resume then skips recording. Nothing asserts it.

## 4. Refresh the .exe fingerprint when you rebuild

`HR_GUIDE.md` no longer embeds a hash. It now tells HR to compare against the fingerprint in the message that accompanies the .exe, which removes the drift entirely. `NEXT_STEPS.md` records where to read it from: the `certutil` step in `build-exe.yml`.

Do not skip this. The guide instructs HR to refuse to run on a mismatch, and that instruction only stays credible if the number she is given is real.

## 5. Smaller items, genuinely fine to carry

- **The all-jobs pre-scan can under-report.** `indeed_downloader.py:3553` short-circuits on a manifest that loads but is empty, so a frontend-written folder prints `0 on disk` while resumes sit in it. Display only, and it self-corrects one line later when the folder is backfilled. Guard with `if existing and existing["candidates"]:`.
- **Two remaining latch-the-first sites.** `resolve_job_folder` with duplicate `employer_job_id`, and the exact-date early return in `resolve_legacy_folder_by_name`. Both need a hand-copied folder or a synthesized-ID collision. Consistent with the doctrine this branch settled on: a duplicate folder is recoverable by hand, a merged one is not.
- **`load()`'s copy-aside is not idempotent.** `resolve_job_folder` calls `load()` on every folder, so one damaged manifest accrues a backup file per scan.
- **`SCHEMA_VERSION` is written but never validated.** A future v2 will read a v1 file as fully valid.
- **`write_atomic` uses a fixed `.tmp` name** and leaves it behind if serialization raises. Not concurrency-safe, though single-instance use is the realistic case.
- **CI runs on `windows-latest` at a 2× minute multiplier, unfiltered on push.** Deliberate: it exercises the case-insensitive filesystem behavior `manifest.py` is written for. A `branches:` filter would halve the cost.

## Normalization residuals, and why they are fine

Several deferred findings concern `normalize_name`: no NFC pre-pass, all-punctuation names collapsing to the empty key, `casefold()` being stricter than the simple folding NTFS and APFS actually use.

Every one of them fails in the same direction. They over-separate, producing a duplicate folder or a redundant download, and never merge two people into one folder. The invariant that matters was verified across Latin, Cyrillic, CJK, Arabic, Hangul, Greek, Vietnamese, apostrophes, interpuncts and `ß` with zero violations:

```
normalize_name(sanitize_folder_name(x)) == normalize_name(x)
```

That property is what lets an existing folder on disk be matched to the applicant Indeed reports, and it is what the whole migration turns on. It has a test.
