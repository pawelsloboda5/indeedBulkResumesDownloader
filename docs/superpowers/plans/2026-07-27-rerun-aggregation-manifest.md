# Re-run Aggregation Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a second run of the Indeed downloader over a job that already has a folder download exactly the applicants that are new, instead of classifying the job as complete and skipping it.

**Architecture:** A per-job `manifest.json` keyed on Indeed's `legacyID` becomes the single source of truth for what is already on disk. It lives inside the job folder, so it survives deleting `logs\`, moving `downloads\` to a document management system, and launching the .exe from a different directory. All identity logic moves into a new `manifest.py` of pure functions so it can be tested without a browser; `indeed_downloader.py` keeps the Selenium and GraphQL work and calls into it.

**Tech Stack:** pytest (new, dev-only), PyInstaller 6.3.0 `--onefile`, Selenium 4.16.0, undetected-chromedriver 3.5.5.

**Python version split (discovered during Task 2, 2026-07-27):** CI builds the .exe on **3.11** (`build-exe.yml`), but the local `python3` these tests run under is **3.9.6**. Code must be valid on 3.9: use `Optional[X]` from `typing`, never PEP 604 `X | None`, which raises `TypeError` at collection time on 3.9. The repo already follows this convention — `indeed_downloader.py` uses `Optional[...]` throughout and contains zero `X | None`. Any `str | None` appearing in an **Interfaces** block below is prose describing a nullable value, not source to transcribe.

**Spec:** `docs/superpowers/specs/2026-07-27-indeed-downloader-rerun-aggregation-design.md`

## Global Constraints

- Working directory for every command: `/Users/pawelsloboda/Desktop/indeed_bulk_resumes_downloader/indeedBulkResumesDownloader`. Current branch `build-exe-from-audited-source`. Do not push.
- Line numbers below are as of commit `2c20a16`. After Task 6 lands they shift. **Locate edit sites by the quoted anchor text, not the line number.**
- `manifest.py` must be imported statically (`import manifest` at module top). PyInstaller's Analysis follows static imports and bundles it with no workflow change; a dynamic `importlib.import_module("manifest")` works from source and raises `ModuleNotFoundError` inside the .exe. Verified against Context7 `/websites/pyinstaller_en_stable`, 2026-07-27.
- `manifest.py` imports only the standard library. No Selenium, no network, no `IndeedDownloader` state. This is what makes it testable.
- pytest goes in a new `requirements-dev.txt`, never `requirements.txt` — that file feeds the PyInstaller build.
- Date strings are `%Y-%m-%d`. Run timestamps are `%Y-%m-%dT%H:%M:%S`. Every function that needs "now" takes it as a parameter so tests are deterministic.
- This tool never deletes a candidate folder or a resume. Candidates Indeed stops returning get `stale: true` and stay.
- `MIN_RESUME_BYTES = 1000`, matching the existing accept threshold at `download_cv_api:1449`.
- Never `git add .` or `git add -A`. The repo has untracked PII (`indeed_cookies.json`, `image.png`, report files). Stage named paths only.

**Amendment 1 — `normalize_name` empty-key fallback (ruled 2026-07-27, during Task 2).** As originally written below, `normalize_name` returns `""` for any name with no ASCII-Latin characters (Cyrillic, Arabic, CJK, Greek, Hebrew), so every such applicant collapses onto the single key `_backfill:`. Task 2 only under-populates the manifest as a result, but Task 3's `promote_backfilled` would then bind one applicant's Indeed `legacyID` to an entry whose `folder` points at a different applicant's directory, and later writes would land in the wrong person's folder. `normalize_name` now falls back to the lowercased, whitespace-collapsed raw name when the ASCII pipeline yields empty, so two distinct non-Latin names produce two distinct keys. Behavior for any name that already normalized non-empty is unchanged. This intentionally drops the "exact parity with `indeed_downloader.py:3421`" rationale cited in Task 1: that function lives inside `_find_existing_job_folders`, which Task 8 deletes, and the only surviving `normalize_name` consumer outside candidate matching is job-title matching, where titles are Latin. Because the fix lives in `normalize_name` itself, every downstream key derivation (`backfill_from_disk`, `promote_backfilled`, `entry_key`, `find_key_by_name`) inherits it with no further change — Tasks 3 through 9 below need no edit.

## File Structure

| File | Responsibility |
|---|---|
| `manifest.py` (new) | Pure functions over dicts and paths: normalize, atomic write, load, backfill, promote, diff, folder allocation, job-folder resolution, stale marking, `no_cv.txt` rendering. |
| `tests/test_manifest.py` (new) | Unit coverage for every `manifest.py` function. No browser, no network. |
| `tests/test_rerun_e2e.py` (new) | One regression harness reproducing the reported scenario: 33 on disk, API returns 38, expect exactly 5 fetches. |
| `requirements-dev.txt` (new) | Pinned pytest. |
| `indeed_downloader.py` (modify) | Calls into `manifest.py`. Broken scan, dead code, and the S/N/K prompt removed. |
| `HR_GUIDE.md` (modify) | Documents the real folder layout, the removed prompt, and that `logs\` is now safe to delete. |

---

### Task 1: manifest.py foundation — normalize, atomic write, load with corruption recovery

**Files:**
- Create: `manifest.py`
- Create: `tests/test_manifest.py`
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: nothing.
- Produces: `SCHEMA_VERSION: int`, `MANIFEST_FILENAME: str`, `MIN_RESUME_BYTES: int`, `BACKFILL_PREFIX: str`, `NOKEY_PREFIX: str`, `normalize_name(s: str) -> str`, `sanitize_folder_name(name: str) -> str`, `new_manifest(job: dict) -> dict`, `write_atomic(path: Path, data: dict) -> None`, `save(job_folder: Path, manifest: dict) -> None`, `load(job_folder: Path, timestamp: str | None = None) -> dict | None`.

- [ ] **Step 1: Create the dev requirements file**

```
pytest==8.3.4
```

Save as `requirements-dev.txt`. Then install: `python3 -m pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_manifest.py`:

```python
import json
from pathlib import Path

import manifest


def test_normalize_name_strips_accents_case_and_punctuation():
    assert manifest.normalize_name("José  O'Brien-Smith") == "jose obriensmith"
    assert manifest.normalize_name("  JANE   SMITH  ") == "jane smith"


def test_normalize_name_matches_across_representations():
    assert manifest.normalize_name("Renée Dupont") == manifest.normalize_name("renee dupont")


def test_sanitize_folder_name_keeps_readable_characters():
    assert manifest.sanitize_folder_name("Jean-Luc O'Connor") == "Jean-Luc OConnor"
    assert manifest.sanitize_folder_name("!!!") == "unknown"


def test_new_manifest_has_expected_shape():
    m = manifest.new_manifest({"title": "Cook", "short_id": "33723070"})
    assert m["schema"] == manifest.SCHEMA_VERSION
    assert m["job"]["title"] == "Cook"
    assert m["candidates"] == {}
    assert m["runs"] == []


def test_save_then_load_round_trips(tmp_path):
    m = manifest.new_manifest({"title": "Cook"})
    m["candidates"]["abc"] = {"name": "Jane Smith", "folder": "Jane Smith",
                              "has_cv": True, "stale": False,
                              "first_seen": "2026-07-27", "last_seen": "2026-07-27"}
    manifest.save(tmp_path, m)
    assert manifest.load(tmp_path) == m


def test_save_leaves_no_tmp_file_behind(tmp_path):
    manifest.save(tmp_path, manifest.new_manifest({"title": "Cook"}))
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_returns_none_when_absent(tmp_path):
    assert manifest.load(tmp_path) is None


def test_load_backs_up_corrupt_file_and_returns_none(tmp_path):
    (tmp_path / manifest.MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    assert manifest.load(tmp_path, timestamp="20260727_120000") is None
    backup = tmp_path / "manifest.corrupt-20260727_120000.json"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "{not json"


def test_load_backs_up_valid_json_of_wrong_shape(tmp_path):
    (tmp_path / manifest.MANIFEST_FILENAME).write_text('["a", "b"]', encoding="utf-8")
    assert manifest.load(tmp_path, timestamp="20260727_120001") is None
    assert (tmp_path / "manifest.corrupt-20260727_120001.json").exists()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'manifest'`

- [ ] **Step 4: Write the implementation**

Create `manifest.py`:

```python
"""Per-job download manifest.

Single source of truth for which candidates a job folder already holds.
Lives inside the job folder (not in logs/) so it survives HR deleting
logs/, archiving downloads/ to the document management system, and
launching the .exe from a different working directory — the three things
that used to reset all re-run state.

Standard library only, no Selenium, no network, no IndeedDownloader
state: every function here is a pure transformation over dicts and paths
so it can be tested without a browser.
"""

import json
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
NO_CV_FILENAME = "no_cv.txt"
RESUME_FILENAME = "resume.pdf"

# Matches the accept threshold in download_cv_api — anything smaller is a
# truncated or error-page download, not a resume.
MIN_RESUME_BYTES = 1000

# Key prefixes for entries that have no Indeed legacyID yet.
BACKFILL_PREFIX = "_backfill:"   # recovered from disk, awaiting promotion
NOKEY_PREFIX = "_nokey:"         # API returned the candidate without an ID


def normalize_name(s: str) -> str:
    """Aggressive form used only for MATCHING two spellings of one person.

    Not for naming folders — see sanitize_folder_name for that.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode("ASCII")
    s = re.sub(r"[^a-z0-9\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sanitize_folder_name(name: str) -> str:
    """Readable form used to NAME a candidate folder a human will browse."""
    safe = "".join(c for c in (name or "") if c.isalnum() or c in (" ", "-", "_")).strip()
    return safe or "unknown"


def new_manifest(job: dict) -> dict:
    return {"schema": SCHEMA_VERSION, "job": dict(job or {}), "candidates": {}, "runs": []}


def write_atomic(path: Path, data: dict) -> None:
    """Write via a temp file + os.replace so an interrupted run can never
    leave a half-written index behind."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save(job_folder: Path, manifest: dict) -> None:
    write_atomic(Path(job_folder) / MANIFEST_FILENAME, manifest)


def load(job_folder: Path, timestamp: Optional[str] = None) -> Optional[dict]:
    """Return the manifest, or None if there isn't a usable one.

    A corrupt or wrong-shaped file is copied aside before returning None.
    It must never silently return an empty manifest: silently-empty is what
    produces a full re-download of a folder that was already complete.
    """
    path = Path(job_folder) / MANIFEST_FILENAME
    if not path.exists():
        return None

    data = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        data = None

    if isinstance(data, dict) and isinstance(data.get("candidates"), dict):
        return data

    stamp = timestamp or time.strftime("%Y%m%d_%H%M%S")
    try:
        shutil.copy2(path, Path(job_folder) / f"manifest.corrupt-{stamp}.json")
    except OSError:
        pass
    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 9 passed

- [ ] **Step 6: Commit**

```bash
git add manifest.py tests/test_manifest.py requirements-dev.txt
git commit -m "feat(manifest): module foundation — normalize, atomic write, corruption-safe load"
```

---

### Task 2: Backfill an existing job folder from disk

This is the migration path for the folders HR already has. Without it, the first run after the upgrade re-downloads everything.

**Files:**
- Modify: `manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `new_manifest`, `normalize_name`, `BACKFILL_PREFIX`, `MIN_RESUME_BYTES`, `RESUME_FILENAME`, `NO_CV_FILENAME` from Task 1.
- Produces: `backfill_from_disk(job_folder: Path, job: dict, today: str) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manifest.py`:

```python
def _make_candidate(job_folder: Path, name: str, resume_bytes: int | None):
    folder = job_folder / name
    folder.mkdir(parents=True, exist_ok=True)
    if resume_bytes is not None:
        (folder / "resume.pdf").write_bytes(b"x" * resume_bytes)
    return folder


def test_backfill_reads_candidate_folders(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    _make_candidate(tmp_path, "Bob Jones", 5000)

    m = manifest.backfill_from_disk(tmp_path, {"title": "Cook"}, "2026-07-27")

    assert set(m["candidates"]) == {"_backfill:jane smith", "_backfill:bob jones"}
    entry = m["candidates"]["_backfill:jane smith"]
    assert entry["name"] == "Jane Smith"
    assert entry["folder"] == "Jane Smith"
    assert entry["has_cv"] is True
    assert entry["first_seen"] == "2026-07-27"


def test_backfill_treats_tiny_resume_as_missing(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 10)
    _make_candidate(tmp_path, "Bob Jones", None)

    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert m["candidates"]["_backfill:jane smith"]["has_cv"] is False
    assert m["candidates"]["_backfill:bob jones"]["has_cv"] is False


def test_backfill_ignores_loose_files_and_own_artifacts(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    (tmp_path / "stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / "stray.pdf").write_bytes(b"x" * 5000)

    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert set(m["candidates"]) == {"_backfill:jane smith"}


def test_backfill_picks_up_no_cv_names(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    (tmp_path / "no_cv.txt").write_text("Bob Jones\n\nCarla Diaz\n", encoding="utf-8")

    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert set(m["candidates"]) == {
        "_backfill:jane smith", "_backfill:bob jones", "_backfill:carla diaz",
    }
    assert m["candidates"]["_backfill:bob jones"]["has_cv"] is False
    assert m["candidates"]["_backfill:bob jones"]["folder"] is None


def test_backfill_does_not_let_no_cv_override_a_real_folder(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    (tmp_path / "no_cv.txt").write_text("Jane Smith\n", encoding="utf-8")

    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert m["candidates"]["_backfill:jane smith"]["has_cv"] is True
    assert m["candidates"]["_backfill:jane smith"]["folder"] == "Jane Smith"


def test_backfill_on_empty_folder_yields_empty_manifest(tmp_path):
    m = manifest.backfill_from_disk(tmp_path, {"title": "Cook"}, "2026-07-27")
    assert m["candidates"] == {}
    assert m["job"]["title"] == "Cook"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k backfill -v`
Expected: FAIL with `AttributeError: module 'manifest' has no attribute 'backfill_from_disk'`

- [ ] **Step 3: Write the implementation**

Append to `manifest.py`:

```python
def backfill_from_disk(job_folder: Path, job: dict, today: str) -> dict:
    """Rebuild a manifest for a job folder downloaded by an older build.

    Candidate subdirectories become _backfill: entries keyed on the
    normalized folder name. They carry no Indeed ID yet; promote_backfilled
    swaps in the real legacyID during the next API pass, so nothing gets
    re-downloaded.
    """
    job_folder = Path(job_folder)
    m = new_manifest(job)

    for child in sorted(job_folder.iterdir()):
        if not child.is_dir():
            continue
        resume = child / RESUME_FILENAME
        has_cv = resume.exists() and resume.stat().st_size > MIN_RESUME_BYTES
        m["candidates"][BACKFILL_PREFIX + normalize_name(child.name)] = {
            "name": child.name,
            "folder": child.name,
            "has_cv": has_cv,
            "stale": False,
            "first_seen": today,
            "last_seen": today,
        }

    no_cv = job_folder / NO_CV_FILENAME
    if no_cv.exists():
        try:
            lines = no_cv.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        for line in lines:
            name = line.strip()
            if not name:
                continue
            key = BACKFILL_PREFIX + normalize_name(name)
            # A real folder on disk always wins over a no_cv.txt line.
            if key in m["candidates"]:
                continue
            m["candidates"][key] = {
                "name": name,
                "folder": None,
                "has_cv": False,
                "stale": False,
                "first_seen": today,
                "last_seen": today,
            }

    return m
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 15 passed

- [ ] **Step 5: Commit**

```bash
git add manifest.py tests/test_manifest.py
git commit -m "feat(manifest): backfill an existing job folder from disk"
```

---

### Task 3: Promote backfilled entries and diff against the API

**Files:**
- Modify: `manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `normalize_name`, `BACKFILL_PREFIX`, `NOKEY_PREFIX` from Tasks 1-2.
- Produces: `entry_key(candidate: dict) -> str`, `promote_backfilled(manifest: dict, api_candidates: list) -> int`, `diff(manifest: dict, api_candidates: list) -> list`, `mark_stale(manifest: dict, api_candidates: list, today: str) -> None`.
- API candidate dicts have the shape `indeed_downloader._fetch_candidates_batch` already produces: `{"name": str, "legacy_id": str, "download_url": str | None}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manifest.py`:

```python
def _api(name, legacy_id, has_url=True):
    return {"name": name, "legacy_id": legacy_id,
            "download_url": "https://x/cv" if has_url else None}


def test_entry_key_uses_legacy_id_when_present():
    assert manifest.entry_key(_api("Jane Smith", "abc123")) == "abc123"


def test_entry_key_falls_back_to_name_when_id_missing():
    assert manifest.entry_key(_api("Jane Smith", None)) == "_nokey:jane smith"


def test_promote_rewrites_backfilled_key_and_keeps_folder(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    promoted = manifest.promote_backfilled(m, [_api("Jane Smith", "abc123")])

    assert promoted == 1
    assert "_backfill:jane smith" not in m["candidates"]
    assert m["candidates"]["abc123"]["folder"] == "Jane Smith"
    assert m["candidates"]["abc123"]["has_cv"] is True
    assert m["candidates"]["abc123"]["first_seen"] == "2026-07-27"


def test_promote_matches_across_accent_and_case_differences(tmp_path):
    _make_candidate(tmp_path, "renee dupont", 5000)
    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert manifest.promote_backfilled(m, [_api("Renée Dupont", "xyz")]) == 1
    assert m["candidates"]["xyz"]["folder"] == "renee dupont"


def test_promote_leaves_unmatched_backfill_entries_alone(tmp_path):
    _make_candidate(tmp_path, "Jane Smith", 5000)
    _make_candidate(tmp_path, "Gone Person", 5000)
    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    manifest.promote_backfilled(m, [_api("Jane Smith", "abc123")])

    assert "_backfill:gone person" in m["candidates"]


def test_diff_returns_only_unknown_candidates(tmp_path):
    m = manifest.new_manifest({})
    m["candidates"]["known1"] = {"name": "A", "folder": "A", "has_cv": True,
                                 "stale": False, "first_seen": "x", "last_seen": "x"}

    todo = manifest.diff(m, [_api("A", "known1"), _api("B", "new1"), _api("C", "new2")])

    assert [c["legacy_id"] for c in todo] == ["new1", "new2"]


def test_diff_after_promotion_reproduces_the_reported_scenario(tmp_path):
    for i in range(33):
        _make_candidate(tmp_path, f"Person {i:02d}", 5000)
    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    api = [_api(f"Person {i:02d}", f"id{i:02d}") for i in range(38)]
    manifest.promote_backfilled(m, api)
    todo = manifest.diff(m, api)

    assert len(todo) == 5
    assert [c["legacy_id"] for c in todo] == [f"id{i}" for i in range(33, 38)]


def test_mark_stale_flags_candidates_the_api_stopped_returning():
    m = manifest.new_manifest({})
    for key in ("a", "b"):
        m["candidates"][key] = {"name": key, "folder": key, "has_cv": True,
                                "stale": False, "first_seen": "old", "last_seen": "old"}

    manifest.mark_stale(m, [_api("a", "a")], "2026-07-30")

    assert m["candidates"]["a"]["stale"] is False
    assert m["candidates"]["a"]["last_seen"] == "2026-07-30"
    assert m["candidates"]["b"]["stale"] is True
    assert m["candidates"]["b"]["last_seen"] == "old"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k "entry_key or promote or diff or stale" -v`
Expected: FAIL with `AttributeError: module 'manifest' has no attribute 'entry_key'`

- [ ] **Step 3: Write the implementation**

Append to `manifest.py`:

```python
def entry_key(candidate: dict) -> str:
    """Manifest key for an API candidate.

    Indeed's legacyID identifies a submission, so it is unique per job and
    exact. Candidates that arrive without one fall back to a name-derived
    key, which is weaker but still beats re-downloading them every run.
    """
    legacy_id = candidate.get("legacy_id")
    if legacy_id:
        return str(legacy_id)
    return NOKEY_PREFIX + normalize_name(candidate.get("name", ""))


def promote_backfilled(manifest: dict, api_candidates: list) -> int:
    """Swap _backfill: keys for real legacyIDs by matching on normalized name.

    Mutates `manifest` in place. Returns how many entries were promoted.
    Runs before diff() so a backfilled folder is never re-downloaded.
    """
    promoted = 0
    for candidate in api_candidates:
        legacy_id = candidate.get("legacy_id")
        if not legacy_id or legacy_id in manifest["candidates"]:
            continue
        backfill_key = BACKFILL_PREFIX + normalize_name(candidate.get("name", ""))
        entry = manifest["candidates"].pop(backfill_key, None)
        if entry is None:
            continue
        entry["name"] = candidate.get("name") or entry["name"]
        manifest["candidates"][str(legacy_id)] = entry
        promoted += 1
    return promoted


def diff(manifest: dict, api_candidates: list) -> list:
    """API candidates that aren't in the manifest yet, in API order."""
    known = manifest["candidates"]
    return [c for c in api_candidates if entry_key(c) not in known]


def mark_stale(manifest: dict, api_candidates: list, today: str) -> None:
    """Refresh last_seen for everyone the API returned; flag the rest stale.

    Stale entries are kept. This tool never removes a candidate from a job
    folder or its manifest.
    """
    seen = {entry_key(c) for c in api_candidates}
    for key, entry in manifest["candidates"].items():
        if key in seen:
            entry["last_seen"] = today
            entry["stale"] = False
        else:
            entry["stale"] = True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 23 passed

- [ ] **Step 5: Commit**

```bash
git add manifest.py tests/test_manifest.py
git commit -m "feat(manifest): promote backfilled entries, diff against API, mark stale"
```

---

### Task 4: Candidate folder allocation with collision handling

Fixes the silent overwrite where two applicants named John Smith share one folder and the second replaces the first's resume.

**Files:**
- Modify: `manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `sanitize_folder_name`, `entry_key`, `normalize_name` from Tasks 1-3.
- Produces: `allocate_candidate_folder(job_folder: Path, manifest: dict, key: str, name: str) -> Path`, `record(manifest: dict, key: str, name: str, folder: str | None, has_cv: bool, today: str) -> None`, `write_no_cv(job_folder: Path, manifest: dict) -> None`, `find_key_by_name(manifest: dict, name: str) -> str | None`.

`find_key_by_name` exists because `_create_candidate_folder` has four callers, and only one of them holds an API candidate dict. `download_cv_api` keys on the legacy ID; the app-data pass and the frontend path see a display name only. Without a shared resolver they would allocate two different folders for one person, and the screener Q&A would land somewhere other than the resume.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manifest.py`:

```python
def test_allocate_folder_uses_the_sanitized_name(tmp_path):
    m = manifest.new_manifest({})
    folder = manifest.allocate_candidate_folder(tmp_path, m, "abc", "Jane O'Brien")
    assert folder == tmp_path / "Jane OBrien"
    assert folder.is_dir()


def test_allocate_folder_suffixes_on_collision_with_a_different_id(tmp_path):
    m = manifest.new_manifest({})
    first = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")
    manifest.record(m, "id1", "John Smith", first.name, True, "2026-07-27")

    second = manifest.allocate_candidate_folder(tmp_path, m, "id2", "John Smith")

    assert first.name == "John Smith"
    assert second.name == "John Smith (2)"
    assert second.is_dir()


def test_allocate_folder_is_stable_for_the_same_id(tmp_path):
    m = manifest.new_manifest({})
    first = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")
    manifest.record(m, "id1", "John Smith", first.name, True, "2026-07-27")

    again = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")

    assert again == first


def test_two_same_name_candidates_keep_separate_resumes(tmp_path):
    m = manifest.new_manifest({})
    a = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")
    (a / "resume.pdf").write_bytes(b"A" * 5000)
    manifest.record(m, "id1", "John Smith", a.name, True, "2026-07-27")

    b = manifest.allocate_candidate_folder(tmp_path, m, "id2", "John Smith")
    (b / "resume.pdf").write_bytes(b"B" * 5000)
    manifest.record(m, "id2", "John Smith", b.name, True, "2026-07-27")

    assert (a / "resume.pdf").read_bytes()[:1] == b"A"
    assert (b / "resume.pdf").read_bytes()[:1] == b"B"


def test_record_preserves_first_seen_on_update():
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane", "Jane", False, "2026-07-27")
    manifest.record(m, "id1", "Jane", "Jane", True, "2026-07-30")

    entry = m["candidates"]["id1"]
    assert entry["first_seen"] == "2026-07-27"
    assert entry["last_seen"] == "2026-07-30"
    assert entry["has_cv"] is True


def test_write_no_cv_is_byte_identical_across_runs(tmp_path):
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Bob Jones", None, False, "2026-07-27")
    manifest.record(m, "id2", "Jane Smith", "Jane Smith", True, "2026-07-27")

    manifest.write_no_cv(tmp_path, m)
    first = (tmp_path / "no_cv.txt").read_bytes()
    manifest.write_no_cv(tmp_path, m)
    second = (tmp_path / "no_cv.txt").read_bytes()

    assert first == second
    assert first.decode("utf-8") == "Bob Jones\n"


def test_write_no_cv_removes_the_file_when_everyone_has_a_cv(tmp_path):
    (tmp_path / "no_cv.txt").write_text("Stale Name\n", encoding="utf-8")
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane Smith", "Jane Smith", True, "2026-07-27")

    manifest.write_no_cv(tmp_path, m)

    assert not (tmp_path / "no_cv.txt").exists()


def test_find_key_by_name_locates_an_entry_keyed_on_a_legacy_id():
    m = manifest.new_manifest({})
    manifest.record(m, "abc123", "Renée Dupont", "Renee Dupont", True, "2026-07-27")

    assert manifest.find_key_by_name(m, "renee dupont") == "abc123"
    assert manifest.find_key_by_name(m, "Someone Else") is None


def test_find_key_by_name_lets_the_app_data_pass_reuse_the_cv_folder(tmp_path):
    """The CV download keys on the legacy id; the app-data pass sees only a
    name. Both must resolve to the same folder or the Q&A files land apart."""
    m = manifest.new_manifest({})
    cv_folder = manifest.allocate_candidate_folder(tmp_path, m, "abc123", "John Smith")
    manifest.record(m, "abc123", "John Smith", cv_folder.name, True, "2026-07-27")

    key = manifest.find_key_by_name(m, "John Smith") or "_nokey:john smith"
    app_data_folder = manifest.allocate_candidate_folder(tmp_path, m, key, "John Smith")

    assert app_data_folder == cv_folder
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k "allocate or record or no_cv or find_key" -v`
Expected: FAIL with `AttributeError: module 'manifest' has no attribute 'allocate_candidate_folder'`

- [ ] **Step 3: Write the implementation**

Append to `manifest.py`:

```python
def allocate_candidate_folder(job_folder: Path, manifest: dict, key: str, name: str) -> Path:
    """Return (and create) this candidate's folder inside the job folder.

    Folders are named for humans, so two different people named John Smith
    would collide. The manifest knows which folder belongs to which ID, so
    the second one gets " (2)" instead of silently overwriting the first.
    """
    existing = manifest["candidates"].get(key, {}).get("folder")
    if existing:
        folder = Path(job_folder) / existing
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    taken = {
        entry["folder"]
        for other_key, entry in manifest["candidates"].items()
        if other_key != key and entry.get("folder")
    }
    base = sanitize_folder_name(name)
    chosen, n = base, 2
    while chosen in taken:
        chosen = f"{base} ({n})"
        n += 1

    folder = Path(job_folder) / chosen
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def record(manifest: dict, key: str, name: str, folder: Optional[str],
           has_cv: bool, today: str) -> None:
    """Insert or update one candidate. Preserves first_seen across updates."""
    existing = manifest["candidates"].get(key)
    manifest["candidates"][key] = {
        "name": name,
        "folder": folder,
        "has_cv": has_cv,
        "stale": False,
        "first_seen": existing["first_seen"] if existing else today,
        "last_seen": today,
    }


def write_no_cv(job_folder: Path, manifest: dict) -> None:
    """Regenerate no_cv.txt from the manifest.

    Rewritten rather than appended: the old append-per-run behaviour
    duplicated every name on each pass and inflated the candidate counts
    derived from the file. Sorted so two runs over the same data produce
    byte-identical output.
    """
    path = Path(job_folder) / NO_CV_FILENAME
    names = sorted(
        entry["name"] for entry in manifest["candidates"].values()
        if not entry.get("has_cv")
    )
    if not names:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return
    path.write_text("\n".join(names) + "\n", encoding="utf-8")


def find_key_by_name(manifest: dict, name: str) -> Optional[str]:
    """Find an existing entry's key by matching on normalized display name.

    Only download_cv_api holds an API candidate dict with a legacyID. The
    app-data pass and the Selenium path see a display name only, and must
    land in the SAME folder the resume went to — otherwise the screener Q&A
    files end up in a second folder for the same person.
    """
    target = normalize_name(name)
    if not target:
        return None
    for key, entry in manifest["candidates"].items():
        if normalize_name(entry.get("name", "")) == target:
            return key
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 32 passed

- [ ] **Step 5: Commit**

```bash
git add manifest.py tests/test_manifest.py
git commit -m "feat(manifest): folder allocation with collision suffix, record, no_cv rewrite"
```

---

### Task 5: Resolve a job folder by Indeed job ID

Collapses `Cook`, `Cook (12-05-2026)`, and `Job_33723070` onto one folder, so single-job and all-jobs mode stop splitting a job's applicants.

**Files:**
- Modify: `manifest.py`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `load`, `save`, `new_manifest`, `normalize_name` from Task 1.
- Produces: `resolve_job_folder(download_root: Path, employer_job_id: str | None, short_id: str | None) -> Path | None`, `resolve_legacy_folder_by_name(download_root: Path, title_clean: str, job_date: str | None) -> Path | None`.

`resolve_legacy_folder_by_name` is what `_find_existing_job_folders` becomes. Folders written by an older build have no manifest and so no job ID to match on. Matching on the normalized title (with an exact date match required when both sides carry a date) adopts them on the next run instead of creating a duplicate beside them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manifest.py`:

```python
def _seed_job_folder(root: Path, folder_name: str, job: dict) -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    manifest.save(folder, manifest.new_manifest(job))
    return folder


def test_resolve_finds_folder_by_employer_job_id(tmp_path):
    _seed_job_folder(tmp_path, "Cook (12-05-2026)", {"employer_job_id": "iri-abc"})
    found = manifest.resolve_job_folder(tmp_path, "iri-abc", None)
    assert found == tmp_path / "Cook (12-05-2026)"


def test_resolve_finds_the_same_job_under_any_folder_name(tmp_path):
    for name in ("Cook", "Cook (12-05-2026)", "Job_33723070"):
        (tmp_path / name).mkdir()
    manifest.save(tmp_path / "Job_33723070",
                  manifest.new_manifest({"employer_job_id": "iri-abc", "short_id": "33723070"}))

    assert manifest.resolve_job_folder(tmp_path, "iri-abc", None) == tmp_path / "Job_33723070"
    assert manifest.resolve_job_folder(tmp_path, None, "33723070") == tmp_path / "Job_33723070"


def test_resolve_prefers_employer_job_id_over_short_id(tmp_path):
    _seed_job_folder(tmp_path, "Wrong", {"short_id": "111"})
    _seed_job_folder(tmp_path, "Right", {"employer_job_id": "iri-abc", "short_id": "999"})

    assert manifest.resolve_job_folder(tmp_path, "iri-abc", "111") == tmp_path / "Right"


def test_resolve_returns_none_when_nothing_matches(tmp_path):
    _seed_job_folder(tmp_path, "Cook", {"employer_job_id": "iri-other"})
    assert manifest.resolve_job_folder(tmp_path, "iri-abc", None) is None


def test_resolve_ignores_folders_without_a_manifest(tmp_path):
    (tmp_path / "Legacy Folder").mkdir()
    assert manifest.resolve_job_folder(tmp_path, "iri-abc", None) is None


def test_resolve_handles_a_missing_download_root(tmp_path):
    assert manifest.resolve_job_folder(tmp_path / "nope", "iri-abc", None) is None


def test_resolve_ignores_empty_identifiers(tmp_path):
    _seed_job_folder(tmp_path, "Cook", {"employer_job_id": "", "short_id": ""})
    assert manifest.resolve_job_folder(tmp_path, "", "") is None
    assert manifest.resolve_job_folder(tmp_path, None, None) is None


def test_resolve_legacy_adopts_a_manifestless_folder_by_name(tmp_path):
    (tmp_path / "Cook (12-05-2026)").mkdir()

    found = manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", "12-05-2026")

    assert found == tmp_path / "Cook (12-05-2026)"


def test_resolve_legacy_refuses_a_folder_whose_date_disagrees(tmp_path):
    (tmp_path / "Cook (01-01-2020)").mkdir()

    assert manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", "12-05-2026") is None
    # A dateless folder still matches — the old single-job path made those.
    (tmp_path / "Cook").mkdir()
    assert manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", "12-05-2026") == tmp_path / "Cook"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k resolve -v`
Expected: FAIL with `AttributeError: module 'manifest' has no attribute 'resolve_job_folder'`

- [ ] **Step 3: Write the implementation**

Append to `manifest.py`:

```python
def resolve_job_folder(download_root: Path, employer_job_id: Optional[str],
                       short_id: Optional[str]) -> Optional[Path]:
    """Find this job's existing folder by Indeed job ID, whatever it's named.

    Single-job mode names folders without a posting date, all-jobs mode with
    one, and a generic page <h1> produces Job_<uuid8>. Matching on the ID
    instead of the name collapses all of those onto one folder.

    employer_job_id wins over short_id: it is the GraphQL identifier and the
    more specific of the two.
    """
    root = Path(download_root)
    if not root.exists():
        return None

    folders = [child for child in sorted(root.iterdir()) if child.is_dir()]
    manifests = []
    for child in folders:
        data = load(child)
        if data:
            manifests.append((child, data.get("job") or {}))

    if employer_job_id:
        for child, job in manifests:
            if job.get("employer_job_id") == employer_job_id:
                return child
    if short_id:
        for child, job in manifests:
            if job.get("short_id") == short_id:
                return child
    return None


_DATED_FOLDER_RE = re.compile(r"^(.+) \((\d{2}-\d{2}-\d{4})\)$")


def resolve_legacy_folder_by_name(download_root: Path, title_clean: str,
                                  job_date: Optional[str]) -> Optional[Path]:
    """Adopt a folder written by an older build, which has no manifest.

    Replaces the fuzzy scoring in the old _find_existing_job_folders. When
    both the job and the folder carry a date they must agree, so two
    postings of the same title stay separate. A dateless folder matches
    either way, because the old single-job path never wrote a date.
    """
    root = Path(download_root)
    if not root.exists() or not title_clean:
        return None

    target = normalize_name(title_clean)
    if not target:
        return None

    dateless_match = None
    for child in sorted(root.iterdir()):
        if not child.is_dir() or (child / MANIFEST_FILENAME).exists():
            continue
        matched = _DATED_FOLDER_RE.match(child.name)
        folder_name, folder_date = (matched.group(1), matched.group(2)) if matched else (child.name, None)
        if normalize_name(folder_name) != target:
            continue
        if folder_date and job_date:
            if folder_date == job_date:
                return child
            continue
        dateless_match = dateless_match or child

    return dateless_match
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 41 passed

- [ ] **Step 5: Commit**

```bash
git add manifest.py tests/test_manifest.py
git commit -m "feat(manifest): resolve a job folder by Indeed job id, not folder name"
```

---

### Task 6: Wire the manifest into the download path

Replaces the scan that made `already_processed` always zero, and makes every successful download record itself immediately.

**Files:**
- Modify: `indeed_downloader.py` — add import; `_create_job_folder`; `download_cv_api`; `_download_all_candidates_api`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: `self.current_manifest: dict` on `IndeedDownloader`, populated by `_create_job_folder` and consumed by `download_cv_api` and `_download_all_candidates_api`.

- [ ] **Step 1: Add the static import**

Find the import block at the top of `indeed_downloader.py` and add, alongside the other first-party-free imports:

```python
import manifest as manifest_mod
```

Must be a plain top-level `import`. A dynamic import breaks the .exe (see Global Constraints).

- [ ] **Step 2: Initialise the manifest attribute**

In `__init__`, find the anchor:

```python
        self.current_job_is_existing = False  # True if job folder already existed
```

Replace that line with:

```python
        self.current_manifest = None       # dict, set by _create_job_folder
        self.current_job_identifiers = {}  # {'employer_job_id': str, 'short_id': str}
```

- [ ] **Step 3: Make `_create_job_folder` resolve by ID, then load or backfill**

Replace the body of `_create_job_folder` (anchor: `"""Create folder for job with name and date"""`) with:

```python
    def _create_job_folder(self, job_name: str, job_date: str = None,
                           employer_job_id: str = None, short_id: str = None) -> Path:
        """Resolve (or create) this job's folder and load its manifest.

        Identity comes from the Indeed job id via the manifest, so a job
        downloaded once in single-job mode and again in all-jobs mode lands
        in the same folder even though the two modes name folders
        differently. Falls back to name-and-date only for folders written
        by an older build.
        """
        today = time.strftime('%Y-%m-%d')
        self.current_job_identifiers = {
            'employer_job_id': employer_job_id or '',
            'short_id': short_id or '',
        }

        safe_name = self._clean_job_title(job_name)[:80]

        # 1. By Indeed job id via the manifest — exact, name-independent.
        job_folder = manifest_mod.resolve_job_folder(
            Path(self.download_folder), employer_job_id, short_id
        )
        # 2. By name, for folders an older build wrote with no manifest.
        if job_folder is None:
            job_folder = manifest_mod.resolve_legacy_folder_by_name(
                Path(self.download_folder), safe_name, job_date
            )
            if job_folder is not None:
                print(f"   Adopting existing folder: {job_folder.name}")
        # 3. Otherwise a new folder.
        if job_folder is None:
            folder_name = f"{safe_name} ({job_date})" if job_date else safe_name
            job_folder = Path(self.download_folder) / folder_name

        job_folder.mkdir(parents=True, exist_ok=True)

        job_meta = {
            'title': job_name,
            'posted_date': job_date or '',
            'employer_job_id': employer_job_id or '',
            'short_id': short_id or '',
        }

        loaded = manifest_mod.load(job_folder)
        if loaded is None:
            loaded = manifest_mod.backfill_from_disk(job_folder, job_meta, today)
            recovered = len(loaded['candidates'])
            if recovered:
                print(f"   Recovered {recovered} existing candidates from disk")
        else:
            loaded['job'].update({k: v for k, v in job_meta.items() if v})

        self.current_manifest = loaded
        manifest_mod.save(job_folder, self.current_manifest)

        self.current_job_folder = job_folder
        self._point_chrome_downloads_at(job_folder)
        return job_folder
```

- [ ] **Step 4: Add the shared candidate-folder resolver**

`_create_candidate_folder` has four callers and only one of them holds an API candidate dict. They must all land on the same folder for a given person. Add this method to `IndeedDownloader`, directly above `download_cv_api`:

```python
    def _candidate_folder_for(self, name: str, candidate: dict = None) -> Path:
        """Resolve (and create) one candidate's folder, consistently.

        download_cv_api has the API dict and keys on the legacyID. The
        app-data pass and the Selenium path see a display name only, so they
        look the key up by name first. Without that lookup the two flows
        allocate different folders and the screener Q&A files land away from
        the resume.
        """
        if candidate is not None:
            key = manifest_mod.entry_key(candidate)
        else:
            key = (manifest_mod.find_key_by_name(self.current_manifest, name)
                   or manifest_mod.NOKEY_PREFIX + manifest_mod.normalize_name(name))
        return manifest_mod.allocate_candidate_folder(
            self.current_job_folder, self.current_manifest, key, name
        )
```

- [ ] **Step 5: Make `download_cv_api` write into the manifest folder and record the result**

In `download_cv_api`, delete the global-checkpoint early return (anchor: `if legacy_id in self.checkpoint_data['downloaded_ids']:` and its three-line body). It no longer gates downloads; the manifest does.

Then replace the anchor block:

```python
            candidate_folder = self._create_candidate_folder(name)
            filepath = candidate_folder / "resume.pdf"
```

with:

```python
            key = manifest_mod.entry_key(candidate)
            candidate_folder = self._candidate_folder_for(name, candidate)
            filepath = candidate_folder / "resume.pdf"
```

And replace the success block:

```python
            if filepath.stat().st_size > 1000:
                self._save_checkpoint(name=name, legacy_id=legacy_id)
                self.stats['downloaded'] += 1
                return True
```

with:

```python
            if filepath.stat().st_size > manifest_mod.MIN_RESUME_BYTES:
                manifest_mod.record(
                    self.current_manifest, key, name, candidate_folder.name,
                    True, time.strftime('%Y-%m-%d'),
                )
                # Persist after every candidate so a killed run resumes
                # exactly where it stopped — the guarantee HR_GUIDE makes.
                manifest_mod.save(self.current_job_folder, self.current_manifest)
                self._save_checkpoint(name=name, legacy_id=legacy_id)
                self.stats['downloaded'] += 1
                return True
```

- [ ] **Step 6: Replace the broken dedupe scan with a manifest diff**

In `_download_all_candidates_api`, delete the whole block from the anchor comment:

```python
        # Load already processed names (PDFs + no_cv.txt)
        processed_names = set()
```

down to and including the loop that ends:

```python
            if c['download_url']:
                candidates_with_cv.append(c)
            else:
                candidates_no_cv.append(c)
```

Replace it with:

```python
        # Diff against the manifest. The previous implementation scanned PDF
        # filenames with rsplit('_', 2), a shape this code stopped producing
        # when downloads moved to <candidate>/resume.pdf — every file
        # resolved to the string "resume", so already_processed was always 0
        # and nothing was ever recognised as already downloaded.
        today = time.strftime('%Y-%m-%d')
        promoted = manifest_mod.promote_backfilled(self.current_manifest, all_candidates_list)
        if promoted:
            print(f"   Matched {promoted} existing candidates to Indeed ids")

        to_fetch = manifest_mod.diff(self.current_manifest, all_candidates_list)
        already_processed = len(all_candidates_list) - len(to_fetch)

        candidates_with_cv = [c for c in to_fetch if c['download_url']]
        candidates_no_cv = [c for c in to_fetch if not c['download_url']]

        for c in candidates_no_cv:
            manifest_mod.record(
                self.current_manifest, manifest_mod.entry_key(c),
                c['name'], None, False, today,
            )
        if candidates_no_cv:
            print(f"   {len(candidates_no_cv)} new candidates without a CV")
```

- [ ] **Step 7: Run the existing unit tests to confirm nothing regressed**

Run: `python3 -m pytest tests/ -v`
Expected: PASS, 41 passed

- [ ] **Step 8: Verify the module imports cleanly**

Run: `python3 -c "import ast,sys; ast.parse(open('indeed_downloader.py').read()); print('syntax ok')"`
Expected: `syntax ok`

- [ ] **Step 9: Commit**

```bash
git add indeed_downloader.py
git commit -m "fix(rerun): diff against the manifest instead of a filename scan that never matched"
```

---

### Task 7: Regenerate no_cv.txt, guard against an empty API result, record the run

**Files:**
- Modify: `indeed_downloader.py` — `_download_all_candidates_api`
- Modify: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `write_no_cv`, `mark_stale`, `save` from Tasks 3-4.
- Produces: no new public interfaces.

**Amendment 4 — `diff` must revisit a no-CV entry when a resume appears (ruled 2026-07-27, during Task 6).** `diff` keys purely on manifest membership, so any candidate recorded `has_cv=False` is excluded from `to_fetch` permanently. The sharp case is not an applicant who genuinely attached no resume — it is a *transient* missing `download_url` from an archived, rate-limited, or partial GraphQL page, which silently blacklists that applicant from every future run with no retry and no signal. The shipped tool has the same hole via `no_cv.txt` → `processed_names`, but Task 6 made the manifest the sole authority, so it is now load-bearing. This repo already carries 429-backoff code, so rate-limited pages are not hypothetical.

- [ ] **Step 0a: Write the failing tests for the no-CV revisit**

Append to `tests/test_manifest.py`:

```python
def test_diff_revisits_a_no_cv_entry_once_a_resume_appears():
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane Smith", None, False, "2026-07-27")

    todo = manifest.diff(m, [_api("Jane Smith", "id1")])

    assert [c["legacy_id"] for c in todo] == ["id1"]


def test_diff_does_not_revisit_a_no_cv_entry_that_still_has_no_resume():
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane Smith", None, False, "2026-07-27")

    todo = manifest.diff(m, [_api("Jane Smith", "id1", has_url=False)])

    assert todo == []


def test_diff_never_revisits_an_entry_that_already_has_a_cv():
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane Smith", "Jane Smith", True, "2026-07-27")

    assert manifest.diff(m, [_api("Jane Smith", "id1")]) == []
```

- [ ] **Step 0b: Run them to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k "revisit or never_revisits" -v`
Expected: the first test FAILS (`assert [] == ['id1']`); the other two already pass under the current implementation.

- [ ] **Step 0c: Amend `diff`**

Replace `diff`'s body in `manifest.py` with:

```python
def diff(manifest: dict, api_candidates: list) -> list:
    """API candidates that still need fetching, in API order.

    Unknown candidates always qualify. A candidate already recorded WITHOUT a
    CV also qualifies once the API starts offering a download_url: a missing
    resume is often transient (archived page, rate limit, partial GraphQL
    response), and keying on membership alone would blacklist that applicant
    from every future run. An applicant who genuinely attached no resume keeps
    returning no download_url, so this adds no repeated work for them.
    """
    known = manifest["candidates"]
    out = []
    for candidate in api_candidates:
        entry = known.get(entry_key(candidate))
        if entry is None:
            out.append(candidate)
        elif not entry.get("has_cv") and candidate.get("download_url"):
            out.append(candidate)
    return out
```

- [ ] **Step 0d: Run the full file**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: PASS, 61 passed (58 + 3)

- [ ] **Step 0e: Commit**

```bash
git add manifest.py tests/test_manifest.py
git commit -m "fix(manifest): revisit a no-CV entry once the API offers a resume"
```

- [ ] **Step 1: Write the failing test for the guard helper**

Append to `tests/test_manifest.py`:

```python
def test_should_abort_on_empty_api_only_when_manifest_has_entries():
    empty = manifest.new_manifest({})
    populated = manifest.new_manifest({})
    manifest.record(populated, "id1", "Jane", "Jane", True, "2026-07-27")

    assert manifest.should_abort_empty_api(populated, fetched=0) is True
    assert manifest.should_abort_empty_api(empty, fetched=0) is False
    assert manifest.should_abort_empty_api(populated, fetched=5) is False


def test_manifest_is_untouched_by_an_aborted_run(tmp_path):
    m = manifest.new_manifest({"title": "Cook"})
    manifest.record(m, "id1", "Jane", "Jane", True, "2026-07-27")
    manifest.save(tmp_path, m)
    before = (tmp_path / "manifest.json").read_bytes()

    if not manifest.should_abort_empty_api(m, fetched=0):
        manifest.save(tmp_path, m)

    assert (tmp_path / "manifest.json").read_bytes() == before
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_manifest.py -k abort -v`
Expected: FAIL with `AttributeError: module 'manifest' has no attribute 'should_abort_empty_api'`

- [ ] **Step 3: Add the guard helper**

Append to `manifest.py`:

```python
def should_abort_empty_api(manifest: dict, fetched: int) -> bool:
    """True when the API returned nothing for a job that has entries on disk.

    That combination means the session expired or Indeed throttled the run,
    not that the applicants vanished. Writing state in that case would wipe
    a good index, so the caller leaves every file untouched.
    """
    return fetched == 0 and bool(manifest.get("candidates"))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_manifest.py -k abort -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Wire the guard into the download path**

In `_download_all_candidates_api`, find the anchor:

```python
        if len(all_candidates_list) == 0 and total_expected > 0:
            print(f"   No candidates fetched - job too old or data archived")
            self.stats['archived'] += 1
            return
```

Replace it with:

```python
        if manifest_mod.should_abort_empty_api(self.current_manifest, len(all_candidates_list)):
            on_disk = len(self.current_manifest['candidates'])
            print(f"   API returned 0 candidates but {on_disk} are already on disk.")
            print(f"   Leaving this job untouched — usually an expired session or a rate limit.")
            print(f"   Log in again in the Chrome window and re-run this job.")
            self.stats['archived'] += 1
            if self.log:
                self.log.event('empty_api_guard_tripped', {
                    'job_folder': str(self.current_job_folder),
                    'manifest_entries': on_disk,
                })
            return

        if len(all_candidates_list) == 0 and total_expected > 0:
            print(f"   No candidates fetched - job too old or data archived")
            self.stats['archived'] += 1
            return
```

- [ ] **Step 6: Replace the append-mode no_cv.txt write**

Delete the anchor block:

```python
        # Save candidates without CV to no_cv.txt
        if candidates_no_cv and self.current_job_folder:
            no_cv_file = self.current_job_folder / 'no_cv.txt'
            with open(no_cv_file, 'a', encoding='utf-8') as f:
                for c in candidates_no_cv:
                    f.write(c['name'] + '\n')
            print(f"   {len(candidates_no_cv)} candidates without CV (saved to no_cv.txt)")
```

It is replaced by the finalise step below, which rewrites the file from the manifest instead of appending to it.

- [ ] **Step 6b: Restore the `Skipped` counter for the backend path**

**Amendment 3 (added 2026-07-27, during Task 6).** Task 6 deleted `download_cv_api`'s global-checkpoint early return, which was the only place the backend path incremented `self.stats['skipped']`. The frontend path still increments it at its own site, so after Task 6 a backend run — the mode HR_GUIDE.md tells HR to use — prints `Skipped: 0` in the end-of-run STATISTICS block while the per-job report correctly reports `skipped: already_processed`. That counter is the primary signal HR reads to confirm a re-run recognised the applicants she already had, so leaving it at zero undermines confidence in exactly the behavior this plan delivers.

In `_download_all_candidates_api`, immediately after the `already_processed` computation Task 6 introduced (anchor: `already_processed = len(all_candidates_list) - len(to_fetch)`), add:

```python
        # The global-checkpoint early return in download_cv_api used to do
        # this; Task 6 removed it, and without this line a backend run
        # reports "Skipped: 0" while the per-job report shows the real count.
        self.stats['skipped'] += already_processed
```

Verify afterwards that `grep -n "stats\['skipped'\] +=" indeed_downloader.py` returns two sites: this one and the pre-existing frontend one.

- [ ] **Step 7: Add a finalise helper and call it on both exit paths**

Add this method to `IndeedDownloader`, directly above `_download_all_candidates_api`:

```python
    def _finalize_job_manifest(self, total_announced: int, total_recovered: int,
                               all_candidates_list: list, downloaded: int) -> None:
        """Persist manifest, regenerate no_cv.txt, append the run record.

        Called on every path that completes a job, including the one where
        there was nothing new to fetch.
        """
        if not self.current_job_folder or self.current_manifest is None:
            return
        today = time.strftime('%Y-%m-%d')
        manifest_mod.mark_stale(self.current_manifest, all_candidates_list, today)
        self.current_manifest['runs'].append({
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'announced': total_announced,
            'fetched': total_recovered,
            'new': downloaded,
        })
        manifest_mod.save(self.current_job_folder, self.current_manifest)
        manifest_mod.write_no_cv(self.current_job_folder, self.current_manifest)
        # stats.json stays for backward compatibility with older reports.
        # Nothing reads it for a decision any more.
        self._save_job_stats(total_announced, total_recovered,
                             len(self.current_manifest['candidates']))
```

Then in `_download_all_candidates_api`, in the `if not candidates_with_cv:` branch, replace the anchor:

```python
            # Save stats: announced, recovered, processed
            self._save_job_stats(total_expected, total_recovered, already_processed + len(candidates_no_cv))
```

with:

```python
            self._finalize_job_manifest(total_expected, total_recovered,
                                        all_candidates_list, 0)
```

And at the end of the download loop, replace the anchor:

```python
        # Save stats: announced, recovered, processed
        total_processed = already_processed + len(candidates_no_cv) + downloaded_count
        self._save_job_stats(total_expected, total_recovered, total_processed)
```

with:

```python
        self._finalize_job_manifest(total_expected, total_recovered,
                                    all_candidates_list, downloaded_count)
```

- [ ] **Step 8: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS, 43 passed

- [ ] **Step 9: Commit**

```bash
git add manifest.py tests/test_manifest.py indeed_downloader.py
git commit -m "fix(rerun): rewrite no_cv.txt from the manifest, guard against an empty API pass"
```

---

### Task 8: Remove the S/N/K prompt and the dead code

**Files:**
- Modify: `indeed_downloader.py` — `run_all_jobs`, `_find_existing_job_folders`, `_ask_skip_existing_jobs`, `_load_job_checkpoint`, `_save_job_checkpoint`, `_create_candidate_folder`, `run_backend_single_job`

**Interfaces:**
- Consumes: `resolve_job_folder`, `load` from Task 5.
- Produces: no new public interfaces. `_ask_skip_existing_jobs`, `_find_existing_job_folders`, `_load_job_checkpoint`, `_save_job_checkpoint`, and `_create_candidate_folder` no longer exist.

- [ ] **Step 1: Delete the four dead or replaced methods**

Delete these whole method definitions from `IndeedDownloader`:

- `_ask_skip_existing_jobs` (anchor: `"""Ask user which existing jobs to skip`)
- `_find_existing_job_folders` (anchor: `"""Find which jobs already have folders in downloads`)
- `_load_job_checkpoint` (anchor: `"""Load checkpoint for current job folder - returns (downloaded_ids, downloaded_names)`)
- `_save_job_checkpoint` (anchor: `"""Save checkpoint for current job folder"""`)
- `_create_candidate_folder` (anchor: `"""Return (and create) downloads/<job>/<safe candidate name>/.`)

`_load_job_checkpoint` and `_save_job_checkpoint` have no callers today; confirm before deleting:

Run: `grep -n "_load_job_checkpoint\|_save_job_checkpoint" indeed_downloader.py`
Expected: only the two `def` lines.

- [ ] **Step 2: Point the three remaining callers at `_candidate_folder_for`**

`_create_candidate_folder` had four callers. Task 6 Step 5 already converted the one in `download_cv_api`. Three remain, in three different methods:

Run: `grep -n "_create_candidate_folder" indeed_downloader.py`
Expected: exactly three hits, at roughly `:2245` (`_run_app_data_pass_backend`), `:2427` (`_download_all_candidates_frontend`), `:2906` (`_late_claim_application_html`), plus the `def` line.

In **all three**, the surrounding line is identical, so replace each occurrence of:

```python
                candidate_folder = self._create_candidate_folder(name)
```

with:

```python
                candidate_folder = self._candidate_folder_for(name)
```

Watch the indentation: the site in `_download_all_candidates_frontend` and the one in `_late_claim_application_html` are at 12 spaces, the one in `_run_app_data_pass_backend` at 16. Preserve whatever is already there.

None of the three holds an API candidate dict, so all three go through the name lookup in `_candidate_folder_for`, which finds the key the CV download already recorded. That is what keeps the screener Q&A files in the same folder as the resume. They also inherit the collision suffix.

- [ ] **Step 2b: Verify the app-data and CV flows agree on a folder**

Append to `tests/test_manifest.py`:

```python
def test_app_data_lands_in_the_same_folder_as_the_resume(tmp_path):
    """Guards the four-caller convergence _candidate_folder_for exists for."""
    m = manifest.new_manifest({})
    cv_key = manifest.entry_key(_api("John Smith", "abc123"))
    cv_folder = manifest.allocate_candidate_folder(tmp_path, m, cv_key, "John Smith")
    manifest.record(m, cv_key, "John Smith", cv_folder.name, True, "2026-07-27")

    later_key = (manifest.find_key_by_name(m, "John Smith")
                 or manifest.NOKEY_PREFIX + manifest.normalize_name("John Smith"))
    later_folder = manifest.allocate_candidate_folder(tmp_path, m, later_key, "John Smith")

    assert later_folder == cv_folder
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 1
```

Run: `python3 -m pytest tests/test_manifest.py -k app_data_lands -v`
Expected: PASS, 1 passed

- [ ] **Step 3: Replace the prompt in `run_all_jobs` with a plan line**

Find the anchor:

```python
        # Check for existing folders (compare by name, not checkpoint)
        existing_jobs = self._find_existing_job_folders(jobs)

        if existing_jobs:
            jobs = self._ask_skip_existing_jobs(jobs, existing_jobs)

        if not jobs:
            print("No jobs to process!")
            if self.log:
                self.log.event('all_jobs_all_skipped_existing', {})
            return
```

Replace it with:

```python
        # Every job is re-checked. The old S/N/K prompt classified a job as
        # complete by comparing stats.json to itself, so any job that had
        # finished once was [OK] forever and option N silently dropped its
        # new applicants. An id-level diff also catches churn a count
        # comparison misses: one applicant withdraws, one arrives, the
        # total is unchanged.
        print("\n   Checking which applicants each job already has...")
        for job in jobs:
            folder = manifest_mod.resolve_job_folder(
                Path(self.download_folder), job.get('id'), job.get('short_id')
            )
            existing = manifest_mod.load(folder) if folder else None
            on_disk = len(existing['candidates']) if existing else 0
            live = job.get('total_candidates', 0)
            title = job.get('title_clean', job['title'])
            if on_disk:
                print(f"   {title}: {on_disk} on disk · {live} live")
            else:
                print(f"   {title}: new · {live} live")
```

- [ ] **Step 4: Pass the job identifiers into `_create_job_folder`**

In `run_all_jobs`, replace the anchor:

```python
                self._create_job_folder(job['title'], job['date'])
```

with:

```python
                self._create_job_folder(
                    job['title'], job['date'],
                    employer_job_id=job.get('id'),
                    short_id=job.get('short_id'),
                )
```

In `run_backend_single_job`, replace the anchor:

```python
        try:
            self._create_job_folder(job_name)
            print(f"📁 Folder: {self.current_job_folder}")
        except Exception:
            pass
```

with:

```python
        try:
            # No posting date is available on the candidates page. That is
            # fine: the manifest carries the job id, so a later all-jobs run
            # resolves to this same folder even though it would have named a
            # new one "<title> (DD-MM-YYYY)".
            self._create_job_folder(
                job_name, None,
                employer_job_id=self.current_job_id,
                short_id=self.current_job_legacy_id,
            )
            print(f"📁 Folder: {self.current_job_folder}")
        except Exception as e:
            print(f"❌ Could not prepare the job folder: {e!r}")
            if self.log:
                self.log.event('single_job_folder_failed', {'err': repr(e)})
            return
```

The bare `except Exception: pass` is replaced deliberately: with the manifest in play, a failure here leaves `self.current_manifest` as `None` and every later call would fail confusingly.

**Amendment 2 — the third call site (ruled 2026-07-27, during Task 5).** `_create_job_folder` has THREE callers, not two: `run_backend_single_job:1560`, `run_frontend_single_job:2400`, and `run_all_jobs:3798`. This step originally updated only the first and third. Left alone, `run_frontend_single_job` creates a folder whose manifest carries no job ID, so no later run can resolve to it by ID and that job's applicants split across folders — the exact bug this plan exists to fix, surviving in the fallback mode.

`run_frontend_single_job` never parses the job ID at all, unlike the backend path. Give it the same extraction, then pass what it finds. Replace the anchor:

```python
            job_name = self.driver.execute_script("""
                const el = document.querySelector('[data-testid="job-title"]') ||
                           document.querySelector('h1');
                return el ? el.textContent.trim() : 'Job';
            """)
            self._create_job_folder(job_name)
            print(f"📁 Folder: {self.current_job_folder}")
        except Exception:
            pass
```

with:

```python
            job_name = self.driver.execute_script("""
                const el = document.querySelector('[data-testid="job-title"]') ||
                           document.querySelector('h1');
                return el ? el.textContent.trim() : 'Job';
            """)
            # Frontend mode lands the user on a candidate page rather than the
            # jobs table, so the URL shape is not guaranteed to carry either
            # identifier. Harvest whatever is present and pass it through:
            # with an id the manifest collapses this folder onto the same one
            # the backend and all-jobs paths use; without one it still works,
            # falling back to name-and-date resolution.
            job_url = self.driver.current_url
            employer_job_id = self._extract_job_id_from_url(job_url)
            short_id = None
            try:
                params = parse_qs(urlparse(job_url).query)
                candidate_short = params.get('legacyJobId', [None])[0] or params.get('id', [None])[0]
                if candidate_short and candidate_short != '0':
                    short_id = candidate_short
            except (ValueError, KeyError, IndexError):
                pass
            self._create_job_folder(
                job_name, None,
                employer_job_id=employer_job_id,
                short_id=short_id,
            )
            print(f"📁 Folder: {self.current_job_folder}")
            if self.log:
                self.log.event('frontend_single_job_ids', {
                    'has_employer_job_id': bool(employer_job_id),
                    'has_short_id': bool(short_id),
                })
        except Exception as e:
            print(f"❌ Could not prepare the job folder: {e!r}")
            if self.log:
                self.log.event('frontend_single_job_folder_failed', {'err': repr(e)})
            return
```

Do not assume either parameter resolves. On a `/candidates/view` profile URL the candidate's own `id=` is present and is NOT the job id, which is why `legacyJobId` is tried first and why the `log.event` records which identifiers were actually found — that telemetry is how we learn the real URL shape in frontend mode without guessing at it here.

- [ ] **Step 4b: Correct the `stats.json` comment and prove it true**

**Amendment 5 (added 2026-07-27, during Task 7 review).** Task 7 changed the `processed` value written to `stats.json` from a per-run count bounded by `total_recovered` to the cumulative manifest size, which includes stale and unmatched `_backfill:` entries. While `_find_existing_job_folders` still existed, that could classify a job as `[OK] complete` even when today's downloads all failed — `cv_count < total_recovered` reads `400 < 300` as False — and `[N] NewOnly` would then drop that job from the run. Deleting `_find_existing_job_folders` in Step 1 removes the only consumer, which is why this is a comment fix here rather than a value fix in Task 7.

The comment Task 7 was told to write is false until this step lands. In `_finalize_job_manifest`, replace the anchor:

```python
        # stats.json stays for backward compatibility with older reports.
        # Nothing reads it for a decision any more.
```

with:

```python
        # stats.json stays for the end-of-run report, which reads only
        # total_announced and total_recovered (see _generate_report). The
        # `processed` value is the cumulative manifest size — it counts stale
        # and unmatched backfill entries, so it is NOT bounded by
        # total_recovered and must never be compared against it.
```

- [ ] **Step 4c: Prove no consumer of `processed` survives**

Run: `grep -n "get('processed'\|\[.processed.\]" indeed_downloader.py`
Expected: no hit that reads the value back. `_save_job_stats`'s own parameter and its `'processed': processed` dict literal are writes, not reads, and may remain.

If any read survives, stop and report it — the value is unbounded relative to `total_recovered` and any comparison between the two is a live misclassification.

- [ ] **Step 5: Verify no references to the deleted methods remain**

Run: `grep -n "_ask_skip_existing_jobs\|_find_existing_job_folders\|_load_job_checkpoint\|_save_job_checkpoint\|_create_candidate_folder\|current_job_is_existing" indeed_downloader.py`
Expected: no output

- [ ] **Step 6: Verify the file still parses and the suite passes**

Run: `python3 -c "import ast; ast.parse(open('indeed_downloader.py').read()); print('syntax ok')" && python3 -m pytest tests/ -v`
Expected: `syntax ok`, then 44 passed

- [ ] **Step 7: Commit**

```bash
git add indeed_downloader.py tests/test_manifest.py
git commit -m "refactor(rerun): drop the S/N/K prompt and the dead per-job checkpoint code"
```

---

### Task 9: End-to-end regression harness and HR guide update

**Files:**
- Create: `tests/test_rerun_e2e.py`
- Modify: `HR_GUIDE.md`

**Interfaces:**
- Consumes: the full `manifest.py` surface.
- Produces: no new interfaces.

**Amendment 6 — an empty manifest must not block backfill (ruled 2026-07-27, during Task 8 review).** Frontend (Selenium) mode writes resumes to disk but never records them: `manifest_mod.record` and `manifest_mod.save` exist only on the API path. `_create_job_folder` still writes a manifest with `candidates: {}`, and on the next run `load()` *succeeds*, so the `if loaded is None` condition skips `backfill_from_disk` — the resumes on disk become permanently invisible and a later backend run diffs against `{}` and re-downloads all of them. The empty manifest makes recovery harder than having no manifest at all.

The ruling is to close the dangerous half here and file the rest: **make backfill trigger whenever the manifest has no candidates**, and file frontend-side recording (`record` in `_download_cv_frontend`, `_finalize_job_manifest` at the end of `_download_all_candidates_frontend`) as separate work after merge.

The condition goes in `manifest.py` as a named predicate rather than inline in `indeed_downloader.py`, both so it is testable — nothing under `tests/` imports `indeed_downloader` — and because it is identity/state logic, which this plan keeps in one module.

- [ ] **Step 0a: Write the failing tests for the backfill predicate**

Append to `tests/test_manifest.py`:

```python
def test_needs_backfill_is_true_when_there_is_no_manifest():
    assert manifest.needs_backfill(None) is True


def test_needs_backfill_is_true_when_the_manifest_has_no_candidates():
    """Frontend mode writes resumes but no entries, so load() succeeds on an
    empty manifest and would otherwise skip recovery forever."""
    assert manifest.needs_backfill(manifest.new_manifest({"title": "Cook"})) is True


def test_needs_backfill_is_false_once_entries_exist():
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "Jane Smith", "Jane Smith", True, "2026-07-27")
    assert manifest.needs_backfill(m) is False
```

- [ ] **Step 0b: Run them to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -k needs_backfill -v`
Expected: FAIL, `AttributeError: module 'manifest' has no attribute 'needs_backfill'`

- [ ] **Step 0c: Add the predicate**

Append to `manifest.py`:

```python
def needs_backfill(manifest: Optional[dict]) -> bool:
    """True when a job folder's contents must be recovered from disk.

    Covers two cases. No manifest at all is the obvious one. An EMPTY manifest
    is the subtle one: the Selenium download path writes resumes without ever
    calling record(), so its folders carry a manifest with no candidates, and
    keying recovery on `load() is None` alone would leave those resumes
    invisible forever — and a later diff would re-download every one of them.
    A genuinely empty job folder backfills to empty, so triggering here costs
    nothing when there is nothing to find.
    """
    return manifest is None or not manifest.get("candidates")
```

- [ ] **Step 0d: Use it, preserving run history**

In `indeed_downloader.py`'s `_create_job_folder`, replace the anchor:

```python
        loaded = manifest_mod.load(job_folder)
        if loaded is None:
            loaded = manifest_mod.backfill_from_disk(job_folder, job_meta, today)
```

with:

```python
        loaded = manifest_mod.load(job_folder)
        if manifest_mod.needs_backfill(loaded):
            # Keep any run history the empty manifest already carried —
            # backfill_from_disk starts from new_manifest and would drop it.
            previous_runs = loaded.get('runs', []) if loaded else []
            loaded = manifest_mod.backfill_from_disk(job_folder, job_meta, today)
            loaded['runs'] = previous_runs + loaded['runs']
```

- [ ] **Step 0e: Guard the frontend id fallback (Amendment 7, ruled 2026-07-27)**

Amendment 2's extraction falls back to `params.get('id')`. `run_frontend_single_job` tells HR to click a candidate before pressing Enter, so the URL is a candidate-profile URL where `id` is the CANDIDATE's id, not the job's. Persisting that into `job.short_id` is worse than persisting nothing: no id falls back to name-and-date resolution, but a WRONG id can also falsely match a different job's folder later and merge two jobs' applicants.

Replace the anchor:

```python
                candidate_short = params.get('legacyJobId', [None])[0] or params.get('id', [None])[0]
```

with:

```python
                candidate_short = params.get('legacyJobId', [None])[0]
                # `id` on a /candidates/view URL is the CANDIDATE's id, not the
                # job's. Writing it into job.short_id would make this folder
                # falsely match a different job later, so only trust `id` when
                # we are not on a profile view.
                if not candidate_short and '/candidates/view' not in urlparse(job_url).path:
                    candidate_short = params.get('id', [None])[0]
```

- [ ] **Step 0f: Verify and commit**

Run: `python3 -m pytest tests/test_manifest.py -v` — expected 67 passed (64 + 3)
Run: `python3 -c "import ast; ast.parse(open('indeed_downloader.py').read()); print('syntax ok')"`

```bash
git add manifest.py tests/test_manifest.py indeed_downloader.py
git commit -m "fix(rerun): backfill an empty manifest, and never trust a candidate id as a job id"
```

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_rerun_e2e.py`:

```python
"""Regression harness for the reported bug.

HR downloads a job, gets 33 applicants in a folder, comes back a few days
later when Indeed shows 38, and re-runs. Before the manifest, the job was
classified complete and the 5 new applicants never downloaded.

This exercises the manifest pipeline the downloader calls, without a
browser or network: build a real 33-candidate folder on disk, hand it an
API result of 38, and assert exactly 5 are queued.
"""

from pathlib import Path

import manifest


def _api(name, legacy_id, has_cv=True):
    return {"name": name, "legacy_id": legacy_id,
            "download_url": "https://x/cv" if has_cv else None}


def _run_pass(job_folder: Path, job: dict, api_candidates: list, today: str):
    """Mirror the sequence _download_all_candidates_api performs."""
    loaded = manifest.load(job_folder)
    if loaded is None:
        loaded = manifest.backfill_from_disk(job_folder, job, today)

    manifest.promote_backfilled(loaded, api_candidates)
    to_fetch = manifest.diff(loaded, api_candidates)

    for candidate in to_fetch:
        key = manifest.entry_key(candidate)
        if candidate["download_url"]:
            folder = manifest.allocate_candidate_folder(job_folder, loaded, key, candidate["name"])
            (folder / "resume.pdf").write_bytes(b"x" * 5000)
            manifest.record(loaded, key, candidate["name"], folder.name, True, today)
        else:
            manifest.record(loaded, key, candidate["name"], None, False, today)

    manifest.mark_stale(loaded, api_candidates, today)
    manifest.save(job_folder, loaded)
    manifest.write_no_cv(job_folder, loaded)
    return loaded, to_fetch


def test_second_run_fetches_only_the_new_applicants(tmp_path):
    job_folder = tmp_path / "Cook (12-05-2026)"
    job_folder.mkdir()
    job = {"title": "Cook", "employer_job_id": "iri-cook", "short_id": "33723070"}

    first_api = [_api(f"Person {i:02d}", f"id{i:02d}") for i in range(33)]
    first, fetched_first = _run_pass(job_folder, job, first_api, "2026-07-27")
    assert len(fetched_first) == 33
    assert len(first["candidates"]) == 33

    second_api = [_api(f"Person {i:02d}", f"id{i:02d}") for i in range(38)]
    second, fetched_second = _run_pass(job_folder, job, second_api, "2026-07-30")

    assert len(fetched_second) == 5
    assert [c["legacy_id"] for c in fetched_second] == [f"id{i}" for i in range(33, 38)]
    assert len(second["candidates"]) == 38
    assert len(list(job_folder.glob("Person */resume.pdf"))) == 38


def test_folder_from_an_older_build_is_not_re_downloaded(tmp_path):
    """The migration path: 33 candidate folders, no manifest at all."""
    job_folder = tmp_path / "Cook"
    job_folder.mkdir()
    for i in range(33):
        person = job_folder / f"Person {i:02d}"
        person.mkdir()
        (person / "resume.pdf").write_bytes(b"old" * 2000)

    api = [_api(f"Person {i:02d}", f"id{i:02d}") for i in range(38)]
    loaded, to_fetch = _run_pass(job_folder, {"title": "Cook"}, api, "2026-07-30")

    assert len(to_fetch) == 5
    assert len(loaded["candidates"]) == 38
    # The pre-existing files were left exactly as they were.
    assert (job_folder / "Person 00" / "resume.pdf").read_bytes()[:3] == b"old"


def test_single_job_and_all_jobs_runs_share_one_folder(tmp_path):
    single = tmp_path / "Job_33723070"
    single.mkdir()
    job = {"title": "Cook", "employer_job_id": "iri-cook", "short_id": "33723070"}
    _run_pass(single, job, [_api(f"P{i}", f"id{i}") for i in range(3)], "2026-07-27")

    # An all-jobs run would have named a fresh folder "Cook (12-05-2026)".
    resolved = manifest.resolve_job_folder(tmp_path, "iri-cook", "33723070")

    assert resolved == single
    assert list(tmp_path.iterdir()) == [single]


def test_no_cv_file_does_not_grow_across_runs(tmp_path):
    job_folder = tmp_path / "Cook"
    job_folder.mkdir()
    api = [_api("Has CV", "id1"), _api("No CV", "id2", has_cv=False)]

    _run_pass(job_folder, {"title": "Cook"}, api, "2026-07-27")
    first = (job_folder / "no_cv.txt").read_bytes()
    _run_pass(job_folder, {"title": "Cook"}, api, "2026-07-30")
    second = (job_folder / "no_cv.txt").read_bytes()

    assert first == second == b"No CV\n"
```

- [ ] **Step 2: Run it to verify it passes**

The manifest functions all exist by now, so this suite should pass on first run. If any test fails, that is a real defect in Tasks 1-5, not in the test.

Run: `python3 -m pytest tests/test_rerun_e2e.py -v`
Expected: PASS, 4 passed

- [ ] **Step 3: Run the whole suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS, 48 passed

- [ ] **Step 4: Update HR_GUIDE.md — the folder layout**

Find the anchor block:

```markdown
Files you now have:
- `downloads\<Job Name>\*.pdf` — the resumes
- `downloads\<Job Name>\no_cv.txt` — candidates who applied without attaching a CV
- `downloads\<Job Name>\stats.json` — per-job download statistics
- `downloads\download_report.txt` — overall summary across all jobs
```

Replace with:

```markdown
Files you now have:
- `downloads\<Job Name>\<Candidate Name>\resume.pdf` — one folder per applicant
- `downloads\<Job Name>\no_cv.txt` — applicants who applied without attaching a CV
- `downloads\<Job Name>\manifest.json` — the record of who has already been downloaded
- `downloads\download_report.txt` — overall summary across all jobs

Leave `manifest.json` in the folder. It is how the tool knows who it already has, so
running the same job again downloads only the people who applied since last time.
```

- [ ] **Step 5: Update HR_GUIDE.md — the removed prompt**

Find the anchor block:

```markdown
**If you picked "All jobs":** the tool fetches the full list and shows you what it found. If some jobs already have downloaded folders, it asks what to do:
- **S** — skip all jobs that already have a folder
- **N** — only re-check jobs where new candidates arrived since last time (most common for a repeat run)
- **K** — download everything again regardless
```

Replace with:

```markdown
**If you picked "All jobs":** the tool fetches the full list, shows you how many
applicants each job already has on disk versus how many Indeed is showing, then
gets to work. There is nothing to answer. It downloads only the applicants that
are new to each folder, so re-running a job you have already done is quick and
safe.
```

- [ ] **Step 6: Update HR_GUIDE.md — the logs folder and re-running after archiving**

Find the anchor line in the security checklist:

```markdown
- When you're done with the whole project, **delete the entire `logs\` folder.**
```

Replace with:

```markdown
- When you're done with the whole project, **delete the entire `logs\` folder.** This is
  safe to do at any time. The record of who has already been downloaded lives in each
  job folder (`manifest.json`), not in `logs\`.
```

Then find the anchor line about archiving:

```markdown
- Delete the local `downloads\` folder after it's been archived centrally.
```

Replace with:

```markdown
- Delete the local `downloads\` folder after it's been archived centrally.
- If you want to keep topping a job up later, copy the job's folder back next to the
  .exe before re-running. It carries its own `manifest.json`, so the tool picks up
  exactly where it left off instead of downloading everyone again.
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_rerun_e2e.py HR_GUIDE.md
git commit -m "test(rerun): end-to-end regression harness; docs(hr): real layout, no prompt"
```

---

## Verification

After Task 9, confirm the whole thing from a clean state:

```bash
python3 -m pytest tests/ -v                      # expect 48 passed
grep -n "rsplit('_', 2)" indeed_downloader.py   # expect exactly 1 hit, inside a COMMENT
# The one surviving mention is the comment in _download_all_candidates_api explaining
# what the removed scan did and why it never matched. That is deliberate documentation
# of the defect. Confirm no EXECUTABLE hit remains:
grep -n "rsplit('_', 2)" indeed_downloader.py | grep -v "^\s*[0-9]*:\s*#"   # expect no output
grep -c "import manifest as manifest_mod" indeed_downloader.py   # expect 1, and it must be top-level
python3 -c "import ast; ast.parse(open('indeed_downloader.py').read()); print('ok')"
```

Test totals, **corrected 2026-07-27 after Task 6** — the original projection did not account for the tests added by each task's review fix round, so every number from Task 2 onward was stale and implementers were reporting a "wrong" count against it. Actuals as committed:

| after | tests | note |
|---|---|---|
| Task 1 | 9 | |
| Task 2 | 15 → **18** | fix round added 3 (non-Latin key distinctness) |
| Task 3 | 26 → **30** | fix round added 4 (per-character fold + invariant) |
| Task 4 | 39 → **44** | fix round added 5 (case collision, allocate→record, ambiguity) |
| Task 5 | 54 → **58** | fix round added 4 (ambiguity refusal, id precedence, date preference, missing root) |
| Task 6 | **58** | wiring only, adds none |
| Task 7 | 60 projected | +2 from the brief |
| Task 8 | 61 projected | +1 from the brief |
| Task 9 | 65 projected | +4 from the e2e harness |

If a task's run reports a different number than the row above it plus its own new tests, a test was dropped or duplicated; fix it before moving on.

The .exe rebuild (`Build Windows .exe` workflow, `workflow_dispatch`) is the last gate and needs a real Indeed session to test end to end. Ask Pawel before triggering it; the SHA-256 in HR_GUIDE.md:148 has to be updated from the build log and re-sent to HR with the new .exe.
