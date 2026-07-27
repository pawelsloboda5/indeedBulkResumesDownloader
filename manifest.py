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

    The ASCII fold below erases any name written without Latin characters:
    Cyrillic, Arabic, CJK, Greek and Hebrew all reduce to "". Every such name
    would then share one key, collapsing two applicants into a single manifest
    entry and letting a later promotion bind one person's Indeed ID to another
    person's folder. So when the fold comes back empty we key on the raw name
    instead, lowercased and whitespace-collapsed — still deterministic and
    stable across runs, but distinct per person.
    """
    raw = s or ""
    folded = unicodedata.normalize("NFKD", raw).encode("ASCII", "ignore").decode("ASCII")
    folded = re.sub(r"[^a-z0-9\s]", "", folded.lower())
    folded = re.sub(r"\s+", " ", folded).strip()
    if folded:
        return folded
    return re.sub(r"\s+", " ", raw.lower()).strip()


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
