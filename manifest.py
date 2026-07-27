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


def _fold_char(ch: str) -> str:
    """NFKD-fold one character to ASCII, or keep it as-is when it has no fold.

    Folding a whole string at once erases every name written without Latin
    characters — Cyrillic, Arabic, CJK, Greek and Hebrew all reduce to "".
    Per character, é→e and ﬁ→fi as before, while 李 and А survive intact.
    """
    ascii_form = unicodedata.normalize("NFKD", ch).encode("ASCII", "ignore").decode("ASCII")
    return ascii_form or ch


def normalize_name(s: str) -> str:
    """Aggressive form used only for MATCHING two spellings of one person.

    Not for naming folders — see sanitize_folder_name for that.

    Promotion compares a normalized API name against the normalized name of a
    folder on disk, and that folder was written by sanitize_folder_name. So the
    invariant this has to hold is:

        normalize_name(sanitize_folder_name(x)) == normalize_name(x)

    Punctuation is what makes that easy to break. sanitize_folder_name drops
    everything non-alphanumeric, so `阿卜杜拉·穆罕默德` lands on disk as
    `阿卜杜拉穆罕默德`; if the interpunct survives normalization on the API side
    the two never meet, promotion fails, and that applicant re-downloads on
    every run, silently, forever. Hence the Unicode-aware class below: it keeps
    letters and digits in ANY script and drops the rest. `_` is dropped
    explicitly because `re` counts it as a word character while the older
    ASCII-only class did not, and ASCII behavior must not shift.

    Folding per character — rather than folding the whole string and falling
    back to the raw name when that came back empty — is also what keeps two
    non-Latin names distinct, including names that MIX scripts, where a
    whole-string fold leaves only the Latin fragment and keys `李伟 Smith` and
    `王芳 Smith` both on "smith".

    Accepted side effect: ß, Æ, Ø and Þ have no NFKD ASCII form, so they now
    survive rather than being dropped — `Straße` keys `straße`, not `strae`.
    Matching is unaffected because both sides change together.
    """
    folded = "".join(_fold_char(c) for c in (s or ""))
    stripped = re.sub(r"[^\w\s]|_", "", folded.lower())
    return re.sub(r"\s+", " ", stripped).strip()


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


def _folder_key(folder: str) -> str:
    """Comparison form for a folder name — what the FILESYSTEM considers equal.

    Two folder strings can differ and still be one directory. NTFS and APFS
    both resolve names case-insensitively, and APFS normalizes Unicode as
    well, so `John Smith`/`john smith` and the NFC/NFD spellings of `José`
    each address a single folder. Comparing raw strings handed the second
    applicant the first one's directory and let mkdir(exist_ok=True) overwrite
    their resume.
    """
    return unicodedata.normalize("NFC", folder or "").casefold()


def allocate_candidate_folder(job_folder: Path, manifest: dict, key: str, name: str) -> Path:
    """Return (and create) this candidate's folder inside the job folder.

    Folders are named for humans, so two different people named John Smith
    would collide. The manifest knows which folder belongs to which ID, so
    the second one gets " (2)" instead of silently overwriting the first.

    Collision is tested on _folder_key, not on the raw string, because the
    filesystem is the thing that decides whether two names are one directory.
    The folder itself keeps the applicant's own spelling — only the test folds.

    CALLER CONTRACT — allocate, then record, before allocating the next
    candidate. This function reserves nothing: the suffix is derived from the
    manifest, and the manifest only learns a folder exists when record()
    writes it. Two allocations with no record between them read the same
    unchanged manifest and return the SAME path, which is exactly the
    overwrite this function exists to prevent. A caller that skips record()
    when a download fails reopens the bug for the next candidate.
    """
    existing = manifest["candidates"].get(key, {}).get("folder")
    if existing:
        folder = Path(job_folder) / existing
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    taken = {
        _folder_key(entry["folder"])
        for other_key, entry in manifest["candidates"].items()
        if other_key != key and entry.get("folder")
    }
    base = sanitize_folder_name(name)
    chosen, n = base, 2
    while _folder_key(chosen) in taken:
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

    Returns None when the name is AMBIGUOUS as well as when it is unknown.
    Two genuine John Smiths already have two separate, correct folders;
    guessing the first would write the second applicant's Q&A into the first
    one's folder and corrupt a folder that was already right. The caller
    falls through to its own _nokey: path instead, so the worst case is a
    spurious extra folder rather than a destroyed one.
    """
    target = normalize_name(name)
    if not target:
        return None
    matches = [
        key for key, entry in manifest["candidates"].items()
        if normalize_name(entry.get("name", "")) == target
    ]
    if len(matches) != 1:
        return None
    return matches[0]
