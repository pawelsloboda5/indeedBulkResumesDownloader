import json
from pathlib import Path
from typing import Optional

import manifest


def test_normalize_name_strips_accents_case_and_punctuation():
    assert manifest.normalize_name("José  O'Brien-Smith") == "jose obriensmith"
    assert manifest.normalize_name("  JANE   SMITH  ") == "jane smith"


def test_normalize_name_matches_across_representations():
    assert manifest.normalize_name("Renée Dupont") == manifest.normalize_name("renee dupont")


def test_normalize_name_keeps_non_latin_names_distinct():
    # The ASCII fold empties these entirely. Without a fallback every non-Latin
    # name collides onto one key, which mis-binds two applicants downstream.
    li = manifest.normalize_name("李伟")
    wang = manifest.normalize_name("王芳")
    assert li != ""
    assert wang != ""
    assert li != wang


def test_normalize_name_fallback_leaves_latin_names_alone():
    # Accented Latin still folds to ASCII — proof the fallback did not engage
    # and quietly start keying on the raw spelling.
    assert manifest.normalize_name("Renée Dupont") == "renee dupont"
    assert manifest.normalize_name("Jane Smith") == "jane smith"


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


def _make_candidate(job_folder: Path, name: str, resume_bytes: Optional[int]):
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


def test_backfill_keeps_non_latin_candidates_distinct(tmp_path):
    _make_candidate(tmp_path, "李伟", 5000)
    _make_candidate(tmp_path, "王芳", 5000)

    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    # Two people on disk must stay two entries. Collapsing them onto the bare
    # prefix is what lets a later promotion bind one applicant's legacyID to
    # the other applicant's folder.
    assert len(m["candidates"]) == 2
    assert {e["folder"] for e in m["candidates"].values()} == {"李伟", "王芳"}
    assert manifest.BACKFILL_PREFIX not in m["candidates"]
