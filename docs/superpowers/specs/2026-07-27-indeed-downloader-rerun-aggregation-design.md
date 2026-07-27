# Re-run aggregation: per-job manifest keyed on Indeed candidate ID

Status: approved, not yet implemented
Date: 2026-07-27
Affects: `indeed_downloader.py`, new `manifest.py`, new `tests/`, `HR_GUIDE.md`

## The problem

HR runs the downloader on a job and gets 33 applicants in a folder on her Desktop. A few days later there are 38 applicants on Indeed. She runs it again and the 5 new ones do not end up in that folder.

Line numbers below refer to `indeed_downloader.py` as of commit `7f54926`.

### Root cause 1: new-applicant detection compares stale data to itself

`_ask_skip_existing_jobs` decides whether a job has new candidates with:

```python
if cv_count < total_recovered:      # :3619
```

Both values come from the same `stats.json` written by the previous run (`:3440-3443`). After a run that completed, they are equal by construction, so the branch is always false and the job is classified `[OK]` forever.

The live applicant count from the jobs table is available as `total_announced` and is printed on the next line (`:3628`), but never used in the decision. HR_GUIDE.md:73 recommends option `N` ("only jobs with new candidates") for repeat runs, which filters the job out entirely. Nothing is downloaded and nothing looks wrong.

This alone fully explains the reported symptom.

### Root cause 2: the on-disk dedupe scan parses the wrong filename shape

```python
name_part = pdf_file.stem.rsplit('_', 2)[0]   # :1800, expects "Jane Smith_20251126_154317.pdf"
```

The current layout is `<job folder>/<Candidate Name>/resume.pdf` (`:1443-1444`, `_create_candidate_folder:2545`). Every PDF therefore resolves to the literal string `resume`, so `processed_names` is `{"resume"}` no matter how many candidates are on disk and `already_processed` is always 0.

HR_GUIDE.md:105 still documents the old flat layout, which is how the two drifted apart.

### Root cause 3: re-run state lives in a folder HR is told to delete

The only dedupe that currently works is the global `downloaded_ids` check in `download_cv_api:1411`, backed by `logs/checkpoint_unified.json` (`:216`). HR_GUIDE.md:136 instructs HR to delete the entire `logs\` folder when finished. Both `downloads` and `logs` are also resolved relative to the working directory (`:190-191`), so launching the .exe from a different place produces a fresh empty state.

### Root cause 4: job folders are identified by name, so modes disagree

Single-job mode calls `_create_job_folder(job_name)` with no date (`:1560`), producing `Cook`. All-jobs mode passes the posting date (`:3798`), producing `Cook (12-05-2026)`. When the page `h1` is generic, single-job mode falls back to `Job_<uuid8>` (`:1546-1557`); `logs/download_report 8.txt` shows a real `Job_33723070` folder from that path.

Mixing modes across runs splits one job's applicants across two or three folders.

### Root cause 5: the per-job resume mechanism is dead code

`_load_job_checkpoint` (`:1567`) and `_save_job_checkpoint` (`:1602`) are defined and never called. `current_job_is_existing` (`:1174`) is assigned and never read. The per-job `checkpoint.json` these would write does not exist on disk.

`no_cv.txt` is opened in append mode on every run (`:1832`), so candidates without a CV are re-listed each time. That inflates the fallback `cv_count` in `_find_existing_job_folders:3448-3450` on subsequent runs.

### Also fixed: same-name overwrite

`_create_candidate_folder` (`:2545`) keys the folder on the display name with `mkdir(exist_ok=True)`. Two applicants named John Smith share one folder and the second overwrites the first's `resume.pdf`. Nothing in the current code uses Indeed's candidate ID for on-disk identity.

### Ruled out: cross-job skipping

An earlier reading suggested the global `downloaded_ids` list could skip a person who applied to two different jobs. The captured GraphQL (`indeed_graphql_1.txt`) shows `legacyID` is a field on `CandidateSubmission`, so it identifies an application rather than a person. Two jobs means two IDs. The global list is still wrong to gate downloads on (see root cause 3), but it does not cause cross-job data loss.

## Design

### Per-job manifest

`downloads/<Job Folder>/manifest.json` becomes the single source of truth for what has been downloaded.

```json
{
  "schema": 1,
  "job": {
    "title": "Cook",
    "posted_date": "12-05-2026",
    "employer_job_id": "aXJpOi8vYXBpcy5pbmRlZWQ...",
    "short_id": "33723070"
  },
  "candidates": {
    "1a2b3c4d5e": {
      "name": "Jane Smith",
      "folder": "Jane Smith",
      "has_cv": true,
      "stale": false,
      "first_seen": "2026-07-27",
      "last_seen": "2026-07-30"
    }
  },
  "runs": [
    { "at": "2026-07-27T14:03:00", "announced": 33, "fetched": 33, "new": 33 }
  ]
}
```

Keys are Indeed `legacyID` values, so identity is exact and two people with the same display name cannot collide. `folder` records the real on-disk directory, so a collision-suffixed `Jane Smith (2)` round-trips. `has_cv: false` replaces `no_cv.txt` as the source of truth; that file is still written for HR to read, but regenerated rather than appended.

The manifest lives beside the CVs, so it survives HR deleting `logs\`, moving `downloads\` into the document management system, and launching the .exe from a different directory. Those are the three things that currently reset all state.

### Migration for existing folders

On the first run after the upgrade, a job folder with candidate subdirectories but no manifest is backfilled from disk. Each subdirectory becomes an entry keyed `_backfill:<normalized name>`, with `has_cv` set by whether `resume.pdf` is present and larger than 1000 bytes (the same threshold `download_cv_api:1449` uses to accept a download). Names in an existing `no_cv.txt` are added as `has_cv: false` entries.

`<normalized name>` means: NFKD-decompose, drop non-ASCII, lowercase, strip everything outside `[a-z0-9 ]`, collapse runs of whitespace, trim. This is the `normalize()` already defined inside `_find_existing_job_folders:3421`, which moves to `manifest.py` as a shared helper. It is deliberately more aggressive than the candidate-folder sanitizer at `:2552`, which keeps case, dashes, and underscores because it produces a name a human reads. Both are needed: the sanitizer names folders, the normalizer matches them.

During that same run's API pass, each fetched candidate whose normalized name matches a `_backfill:` entry is promoted: the key is rewritten to the real `legacyID` and the `folder` value is preserved. Nothing is re-downloaded.

Entries that never get promoted are people Indeed no longer returns (archived, withdrawn, or a name that changed). They are marked `stale: true` and kept. This tool never deletes anything from a job folder.

### Job folder identity

Before creating or reusing a folder, scan `downloads/*/manifest.json` for a matching `employer_job_id`, then `short_id`. A hit reuses that exact directory regardless of what it is named, which collapses `Cook`, `Cook (12-05-2026)`, and `Job_33723070` onto one folder once any of them has a manifest.

With no manifest match, fall back to the existing name-and-date scoring in `_find_existing_job_folders` for legacy folders, then create a new folder.

Single-job mode runs from the candidates page, which does not carry the posting date the jobs table provides, so it will often have no date to pass. That is acceptable: it creates `Cook` rather than `Cook (12-05-2026)`, writes a manifest carrying the job ID, and every later run in either mode resolves to that same folder by ID. Naming consistency is a readability nicety here, not the correctness mechanism. If a date is available on the page, pass it; do not add a network round-trip to go find one.

### Run flow

Inside `_download_all_candidates_api`:

1. Resolve the job folder by job ID; load the manifest, or backfill it from disk.
2. Fetch the full candidate list from Indeed. Unchanged: all dispositions, all pagination, all fallback passes.
3. `to_fetch = [c for c in api_list if c.legacy_id not in manifest.candidates]`.
4. Download only those. Write the manifest entry immediately after each success, so an interrupted run resumes exactly. This is the guarantee HR_GUIDE.md:85 already makes and that the dead per-job checkpoint was meant to provide.
5. Update `last_seen` on every candidate the API returned; mark the rest `stale: true`.
6. Regenerate `no_cv.txt` from the manifest, truncating rather than appending.
7. Append a `runs[]` entry. Keep writing `stats.json` for backward compatibility, but never read it for a decision again.

### The S/N/K prompt is removed

Per-job output becomes a plan line and nothing to answer:

```
[2/7] Cook (12-05-2026)
      33 on disk · 38 live · fetching 5 new
```

Always re-checking every job costs one API pass per job (seconds, since no PDFs re-download) and is correct in the churn case where someone withdraws and someone new applies, leaving the count unchanged. A count comparison would miss that; an ID diff does not.

## Code changes

| Location | Change |
|---|---|
| `:1794-1813` | Delete the `rsplit('_', 2)` PDF scan. Replaced by manifest lookup. |
| `:3589` `_ask_skip_existing_jobs` | Delete along with the prompt. |
| `:3402` `_find_existing_job_folders` | Reduce to a legacy-folder fallback used only when no manifest matches. |
| `:1567` `_load_job_checkpoint` | Delete (dead code). |
| `:1602` `_save_job_checkpoint` | Delete (dead code). |
| `:213`, `:1174` `current_job_is_existing` | Delete (assigned, never read). |
| `:1411` | Global `downloaded_ids` check becomes advisory; it no longer gates downloads. |
| `:1832` | `no_cv.txt` append mode becomes truncate-and-rewrite. |
| `:1560` | Single-job mode passes a posting date when it can resolve one. |
| `:2545` `_create_candidate_folder` | Take the legacy ID; on collision with a different ID, suffix ` (2)`, ` (3)` and record the result in the manifest entry. |

### New module

`manifest.py` holds pure functions over dicts and paths with no Selenium, no network, and no `IndeedDownloader` state: `load`, `backfill_from_disk`, `promote_backfilled`, `diff`, `record`, `write_atomic`, `resolve_job_folder`.

`indeed_downloader.py` keeps the browser and API work. This split is what makes the identity logic testable without a browser, and it is the only structural change. No unrelated refactoring.

Check `.github/workflows/build-exe.yml` picks up the new module in the PyInstaller build.

## Error handling

- **Atomic writes.** Write `manifest.json.tmp`, then `os.replace`. An interrupted run cannot leave a half-written index.
- **Corrupt manifest.** Copy to `manifest.corrupt-<timestamp>.json`, rebuild by backfill from disk, print a visible warning. It must never silently start empty, because silently-empty is what produces a full re-download.
- **Empty API result.** The existing branch at `:1784` already catches `len(all_candidates_list) == 0` and counts the job as archived. Extend it: when the manifest for that job is non-empty, leave `manifest.json`, `no_cv.txt`, and `stats.json` untouched, print that the job has entries on disk but the API returned nothing, and name the likely causes (expired session, rate limit). Without this guard, one throttled run wipes the index. The weaker case, where the API returns fewer candidates than the manifest holds without returning zero, is handled by marking the missing ones `stale: true` rather than removing them.
- **Name collision.** A candidate whose name maps to a folder already claimed by a different legacy ID gets ` (2)`, recorded in `folder`.
- **Missing legacy ID.** A candidate the API returns without a `legacyID` is downloaded to a name-derived folder and recorded under a `_nokey:<normalized name>` key, so it is at least not re-downloaded next run.

## Testing

The repo has no tests today. Add `tests/test_manifest.py` using pytest against `manifest.py`. No browser, no network.

1. Backfill from a fake folder tree produces correct entries, with `has_cv` matching `resume.pdf` presence.
2. Promotion: a `_backfill:jane smith` entry plus an API result carrying a legacy ID rewrites the key and preserves `folder`.
3. Diff: 33 manifest entries against 38 API results yields exactly the 5 new IDs.
4. Same-name collision produces two entries and two folders, with the first `resume.pdf` intact.
5. Folder resolution finds one job across `Cook`, `Cook (12-05-2026)`, and `Job_33723070` by job ID.
6. `no_cv.txt` is byte-identical after two consecutive runs over the same data.
7. A corrupt manifest is backed up and rebuilt from disk with no entry loss.
8. An empty API result against a non-empty manifest leaves the manifest byte-identical.

Plus one end-to-end regression harness: a fake 33-candidate job folder and a stubbed API returning 38, asserting exactly 5 downloads and 38 manifest entries. That is the reported scenario, locked down.

## Documentation

`HR_GUIDE.md` needs a pass in the same change:

- Line 105 documents `downloads\<Job Name>\*.pdf`, a flat layout the code stopped producing.
- Line 73 explains the S/N/K prompt, which is being removed.
- Line 136 tells HR to delete `logs\` when finished. That becomes accurate advice rather than a data-loss trap, and is worth saying explicitly.
- Add a line explaining that a job folder can be moved to the document management system and still be re-run against later, because the manifest travels with it.

## Out of scope

Frontend (Selenium click) mode shares `_create_candidate_folder` and will inherit the collision fix, but its download path is not otherwise changed here. The application-data pass keeps its own dedup list in `checkpoint_data['downloaded_application_data']`; folding that into the manifest is a reasonable follow-up but is not needed to fix the reported problem.
