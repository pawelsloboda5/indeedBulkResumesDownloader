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
    """Mirror the sequence _download_all_candidates_api performs.

    The backfill block mirrors _create_job_folder. Nothing under tests/
    imports indeed_downloader, so this helper is the only executable record
    of that sequence — keep the two in step. A divergence here passes while
    the real path is broken, which is exactly how the job-block loss this
    file now guards against went unnoticed.
    """
    loaded = manifest.load(job_folder)
    if manifest.needs_backfill(loaded):
        previous_runs = loaded.get("runs", []) if loaded else []
        previous_job = dict(loaded.get("job") or {}) if loaded else {}
        loaded = manifest.backfill_from_disk(job_folder, job, today)
        loaded["runs"] = previous_runs + loaded["runs"]
        previous_job.update({k: v for k, v in job.items() if v})
        loaded["job"] = previous_job

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

    # Byte-identity across the two runs is the subject: the old code appended,
    # so the file grew on every pass. The line terminator is deliberately
    # platform-native — write_no_cv writes in text mode so no_cv.txt opens
    # cleanly in Notepad on HR's Windows machine — so the content assertion
    # goes through splitlines() rather than pinning "\n" and breaking there.
    assert first == second
    assert first.decode("utf-8").splitlines() == ["No CV"]


def test_recovering_an_empty_manifest_keeps_the_job_identity_and_history(tmp_path):
    """A second frontend run must not erase what the first one recorded.

    Frontend mode writes resumes but never calls record(), so its folder
    carries a manifest with no candidates — which is the whole reason
    recovery has to trigger on an EMPTY manifest and not just a missing one.

    The second frontend run starts from a /candidates/view URL, so it has no
    identifiers to offer: there is no employerJobId on a profile URL, and the
    `id` there belongs to the CANDIDATE, so it is refused. Recovery must
    therefore MERGE onto the stored job block rather than replace it.
    Replacing drops short_id, and resolve_job_folder matches on ids alone, so
    the folder would fall back to name-and-date matching permanently — the
    split-folder failure the id-based resolver exists to prevent.
    """
    job_folder = tmp_path / "Cook"
    job_folder.mkdir()
    for i in range(3):
        person = job_folder / f"Person {i:02d}"
        person.mkdir()
        (person / "resume.pdf").write_bytes(b"old" * 2000)

    # What a first frontend run leaves behind: the ids it harvested from the
    # job-list URL and a run record, but no candidate entries at all.
    seeded = manifest.new_manifest({"title": "Cook", "posted_date": "",
                                    "employer_job_id": "", "short_id": "33723070"})
    seeded["runs"] = [{"at": "2026-07-27T09:00:00", "announced": 3,
                       "fetched": 3, "new": 3}]
    manifest.save(job_folder, seeded)

    id_less = {"title": "Cook", "posted_date": "",
               "employer_job_id": "", "short_id": ""}
    api = [_api(f"Person {i:02d}", f"id{i:02d}") for i in range(3)]
    loaded, to_fetch = _run_pass(job_folder, id_less, api, "2026-07-30")

    # The resumes already on disk were recovered, not re-downloaded.
    assert to_fetch == []
    assert len(loaded["candidates"]) == 3
    assert (job_folder / "Person 00" / "resume.pdf").read_bytes()[:3] == b"old"

    # The identity and the history survived the recovery.
    assert loaded["job"]["short_id"] == "33723070"
    assert len(loaded["runs"]) == 1

    # Which is what keeps this folder findable by id on every later run.
    assert manifest.resolve_job_folder(tmp_path, None, "33723070") == job_folder
