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
