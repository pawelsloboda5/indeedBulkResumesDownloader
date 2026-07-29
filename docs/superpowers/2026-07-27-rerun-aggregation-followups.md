# Re-run aggregation: follow-up work

Carried out of the nine-task manifest branch (`634461e..ec5aab9`). Everything here was found by review, triaged, and deliberately deferred. Ranked by value, not by when it was found.

The branch itself is complete: HR re-runs a job she already downloaded and gets exactly the applicants who arrived since, with the existing ones untouched and never re-fetched.

## 1. ~~Add a test that imports `indeed_downloader`~~ — DONE

`tests/test_downloader_integration.py` now imports it. It builds a bare instance with `object.__new__(IndeedDownloader)` and exercises `_candidate_folder_for` against `tmp_path`, skipping cleanly via `pytest.importorskip` when `selenium` is absent so local development without the runtime deps still runs the rest of the suite.

This item was ranked first for a reason, and the reason held: a review pass immediately afterwards found three more defects in exactly the class it describes, one of which — the folder-stability regression in §2 below — was caught *by writing this test*, not by reading the code.

## 2. ~~Record manifest entries from the frontend (Selenium) path~~ — the dangerous half is now genuinely closed

**The original reasoning here was wrong, and it is worth recording why.** It said:

> The dangerous half is already closed: `manifest.needs_backfill` treats an empty manifest as "recover from disk", so those resumes are found on the next run rather than staying invisible.

That holds only while the manifest is **empty**. `needs_backfill` returns False as soon as one entry exists, so after a single Backend run the job's manifest is non-empty and any Selenium-written folder in it is invisible to recovery *forever*. `allocate_candidate_folder` built its `taken` set from manifest entries alone, so the next same-named API applicant was handed that folder and `mkdir(exist_ok=True)` walked straight into it. The residual risk was never "two frontend applicants share a folder" (which is indeed old-build behavior and no regression) — it was **an API download overwriting a real resume that the Selenium path had already saved.**

Fixed by `manifest._holds_someone_elses_files`: allocation now also refuses a folder that exists on disk with files no entry claims. Emptiness is the discriminator, so the failed-download folder in §3 stays reusable.

Two consequences worth knowing:

- Recording the allocation became mandatory, not optional. A folder nobody recorded is indistinguishable from a Selenium-written one, so an applicant's own app-data folder would migrate to `(2)`, `(3)` on each run. `_candidate_folder_for` now records what it allocates, carrying `has_cv` through untouched.
- What still remains is the original narrow item: frontend mode does not call `record()`, so it does not get a collision suffix, and two applicants sharing a display name still share one folder in that mode. That *is* old-build behavior and ships no regression. The collision fix remains backend-only.

## 3. Cover the failed-download path

`download_cv_api` deliberately skips `record()` when a download comes back under `MIN_RESUME_BYTES`, unlinking the truncated file first. That is the precise condition `allocate_candidate_folder`'s own docstring names as reopening the folder-overwrite bug: allocation reserves nothing until `record()` is called.

The current code is safe because the unlink runs first and no path writes a good resume then skips recording. Nothing asserts it.

## 4. Refresh the .exe fingerprint when you rebuild

`HR_GUIDE.md` no longer embeds a hash. It now tells HR to compare against the fingerprint in the message that accompanies the .exe, which removes the drift entirely. `NEXT_STEPS.md` records where to read it from: the `certutil` step in `build-exe.yml`.

Do not skip this. The guide instructs HR to refuse to run on a mismatch, and that instruction only stays credible if the number she is given is real.

## 5. Smaller items, genuinely fine to carry

- **The all-jobs pre-scan can under-report.** `indeed_downloader.py:3553` short-circuits on a manifest that loads but is empty, so a frontend-written folder prints `0 on disk` while resumes sit in it. Display only, and it self-corrects one line later when the folder is backfilled. Guard with `if existing and existing["candidates"]:`.
- **Two remaining latch-the-first sites.** `resolve_job_folder` with duplicate `employer_job_id`, and the exact-date early return in `resolve_legacy_folder_by_name`. Both need a hand-copied folder or a synthesized-ID collision. Consistent with the doctrine this branch settled on: a duplicate folder is recoverable by hand, a merged one is not.
- **A third latch-the-first site was NOT on this list and was the one that mattered.** `promote_backfilled` matched a `_backfill:` entry to whichever API candidate came first. On migration day the old build had already put two same-named applicants in one folder, so one entry stood for two people; the winner inherited the folder plus `has_cv: True` and `diff()` then never re-fetched it, leaving a resume that may be the other person's. Unlike the two above it needed no hand-copying and no ID collision — just two applicants with the same name. Now refuses when 2+ API candidates claim one entry, matching `find_key_by_name` and `resolve_legacy_folder_by_name`. **Lesson for whoever triages the next batch: rank by whether the failure is recoverable, not by how contrived the trigger looks.**
- **A dateless legacy folder can still be adopted by the wrong posting.** `resolve_legacy_folder_by_name` refuses ambiguity on the folder side but nothing guards the *job* side: with one dateless `Cook/` on disk and two postings both titled "Cook", whichever comes first in the jobs list claims it. Nothing is overwritten and the other job's applicants re-download into their own folder, so this is mis-filing rather than loss — but it is a merged folder, which the doctrine above says is the unrecoverable kind. Fix at the call site in `run_all_jobs`: pre-count cleaned titles and skip legacy adoption for any title appearing more than once.
- **`_create_job_folder` runs before the `has_valid_api_id` skip.** A job with no `employerJobId` on its table link gets a folder and a manifest stamped with a *synthetic* id before the run prints "Skipping". If that folder adopted a legacy one, a later run presenting a real IRI misses it and splits the job across two folders. One-line reorder: move the skip above the folder creation.
- **`load()`'s copy-aside is not idempotent.** `resolve_job_folder` calls `load()` on every folder, so one damaged manifest accrues a backup file per scan.
- **`SCHEMA_VERSION` is written but never validated.** A future v2 will read a v1 file as fully valid.
- **`write_atomic` uses a fixed `.tmp` name** and leaves it behind if serialization raises. Not concurrency-safe, though single-instance use is the realistic case.
- **CI runs on `windows-latest`, unfiltered on push.** The runner choice is deliberate: it exercises the case-insensitive filesystem behavior `manifest.py` is written for. The earlier note here claimed a 2× minute multiplier made this expensive — **that was wrong**: this repo is public, so Actions minutes are free and multipliers do not bill. A `branches:` filter is still worth adding, to stop every branch running twice once a PR is open, but for noise rather than money.

## Normalization residuals, and why they are fine

Several deferred findings concern `normalize_name`: no NFC pre-pass, all-punctuation names collapsing to the empty key, `casefold()` being stricter than the simple folding NTFS and APFS actually use.

Every one of them fails in the same direction. They over-separate, producing a duplicate folder or a redundant download, and never merge two people into one folder. The invariant that matters:

```
normalize_name(sanitize_folder_name(x)) == normalize_name(x)
```

That property is what lets an existing folder on disk be matched to the applicant Indeed reports, and it is what the whole migration turns on. It has a test, and it holds across Latin, Cyrillic, CJK, Arabic, Hangul, Greek, Vietnamese, apostrophes, interpuncts and `ß`.

**It does not hold universally, and the earlier claim of "zero violations" overstated what was checked.** Exotic whitespace breaks it: `sanitize_folder_name` permits only a literal space, so a non-breaking space, tab, newline or thin space is *deleted*, while `normalize_name` matches it via `\s` and collapses it to a space. `Jane\xa0Smith` gives `janesmith` on the left and `jane smith` on the right. A pasted name carrying a non-breaking space is the realistic instance.

The consequence is the safe direction — promotion misses, the applicant downloads once more into a `(2)` folder, and it stabilizes — which is why this sits here rather than above. But the test's input set silently excludes the failing class, so it reads as broader proof than it is. A one-line fix makes both sides agree: map any `c.isspace()` to `" "` in `sanitize_folder_name` before the alnum filter.

Also worth knowing when reading those tests: `unicodedata.unidata_version` is 13.0.0 on Python 3.9 and 14.0.0 on 3.11. The suite runs on 3.11 in CI and the .exe is built on 3.11, so that is the authoritative pairing — a fold asserted on a local 3.9 run is not proof about the shipped binary.
