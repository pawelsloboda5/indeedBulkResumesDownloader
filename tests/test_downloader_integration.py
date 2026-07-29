"""Tests that import indeed_downloader itself.

Everything under tests/ used to cover manifest.py plus a harness that MIRRORED
the download sequence. That gap had a cost: two defects reached
indeed_downloader.py and were caught only by a human reading the code, because
both were questions of which argument the downloader passes and what it does
with the result — invisible to any manifest.py test.

Importing the module needs selenium, which is why requirements.txt is installed
in CI alongside requirements-dev.txt. The downloader is never constructed for
real here: __init__ opens a log file and builds a Chrome driver. object.__new__
gives a bare instance whose folder logic can be exercised against tmp_path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manifest as manifest_mod

indeed_downloader = pytest.importorskip(
    "indeed_downloader",
    reason="needs selenium; install requirements.txt (CI does)",
)


class _SilentLog:
    """Stands in for the run log. Records events so a test can assert on them."""

    def __init__(self):
        self.events = []

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


def _bare_downloader(job_folder: Path, job_manifest: dict):
    """An IndeedDownloader with only the attributes the folder logic touches."""
    dl = object.__new__(indeed_downloader.IndeedDownloader)
    dl.log = _SilentLog()
    dl.download_folder = str(job_folder.parent)
    dl.current_job_folder = job_folder
    dl.current_manifest = job_manifest
    return dl


def _api(name, legacy_id):
    return {"name": name, "legacy_id": legacy_id, "download_url": f"https://x/{legacy_id}"}


def test_candidate_folder_keys_on_the_id_not_the_display_name(tmp_path):
    # The regression this file exists for: reverting the call sites to pass
    # (name) alone routes two same-named applicants' screener data into one
    # phantom folder. Passing the API dict keys on the Indeed legacyID.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)

    first = dl._candidate_folder_for("John Smith", _api("John Smith", "id-AAA"))
    manifest_mod.record(m, "id-AAA", "John Smith", first.name, True, "2026-07-27")
    second = dl._candidate_folder_for("John Smith", _api("John Smith", "id-BBB"))

    assert first.name == "John Smith"
    assert second.name == "John Smith (2)"


def test_app_data_folder_is_not_reused_by_a_later_same_named_applicant(tmp_path):
    # An applicant who attaches no resume is recorded with folder=None. The
    # app-data pass then allocates a real folder for their screener answers and
    # — before this fix — never recorded it, so `taken` never saw it. A later
    # DIFFERENT applicant with the same display name was handed that same
    # directory, mixing two people's records into one folder.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)

    manifest_mod.record(m, "id-nocv", "John Smith", None, False, "2026-07-27")
    app_data_folder = dl._candidate_folder_for("John Smith", _api("John Smith", "id-nocv"))
    (app_data_folder / "application.html").write_text("screener answers")

    later = dl._candidate_folder_for("John Smith", _api("John Smith", "id-withcv"))

    assert later.resolve() != app_data_folder.resolve()


def test_app_data_folder_is_stable_across_runs_for_the_same_applicant(tmp_path):
    # Allocation refuses a populated folder that no manifest entry claims,
    # because that is how a Selenium-written folder gets protected. The flip
    # side: an applicant's OWN app-data folder looks exactly like that unless
    # the allocation was recorded. Without the record, run 2 walks past the
    # folder it wrote in run 1 and starts "John Smith (2)", stranding the
    # screener answers one directory behind and growing a new folder per run.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)
    manifest_mod.record(m, "id-nocv", "John Smith", None, False, "2026-07-27")

    first = dl._candidate_folder_for("John Smith", _api("John Smith", "id-nocv"))
    (first / "application.html").write_text("screener answers")

    again = dl._candidate_folder_for("John Smith", _api("John Smith", "id-nocv"))

    assert again.resolve() == first.resolve()
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["John Smith"]


def test_recording_app_data_does_not_claim_the_applicant_has_a_resume(tmp_path):
    # has_cv drives no_cv.txt and drives whether diff() re-fetches. Recording a
    # folder for the app-data pass must not flip it, or an applicant whose
    # resume never downloaded silently drops off the retry list.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)
    manifest_mod.record(m, "id-nocv", "John Smith", None, False, "2026-07-27")

    dl._candidate_folder_for("John Smith", _api("John Smith", "id-nocv"))

    assert m["candidates"]["id-nocv"]["has_cv"] is False
    assert manifest_mod.diff(m, [_api("John Smith", "id-nocv")]) != []


def test_recording_app_data_preserves_an_existing_resume_flag(tmp_path):
    # The mirror of the above: an applicant whose resume already downloaded
    # must not be re-queued because the app-data pass touched their entry.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)
    manifest_mod.record(m, "id-hascv", "John Smith", "John Smith", True, "2026-07-27")

    dl._candidate_folder_for("John Smith", _api("John Smith", "id-hascv"))

    assert m["candidates"]["id-hascv"]["has_cv"] is True
    assert manifest_mod.diff(m, [_api("John Smith", "id-hascv")]) == []


def test_first_seen_survives_an_app_data_allocation(tmp_path):
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)
    manifest_mod.record(m, "id-nocv", "John Smith", None, False, "2026-01-01")

    dl._candidate_folder_for("John Smith", _api("John Smith", "id-nocv"))

    assert m["candidates"]["id-nocv"]["first_seen"] == "2026-01-01"


def test_candidate_without_a_legacy_id_still_lands_on_the_recorded_folder(tmp_path):
    # The late-claim sweep can hold a dict with no legacy_id. It passes None so
    # the name lookup finds the folder the CV pass already recorded, rather
    # than keying on _nokey: and allocating a second one.
    m = manifest_mod.new_manifest({})
    dl = _bare_downloader(tmp_path, m)
    manifest_mod.record(m, "id-AAA", "Jane Doe", "Jane Doe", True, "2026-07-27")

    resolved = dl._candidate_folder_for("Jane Doe", None)

    assert resolved.name == "Jane Doe"
