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


def test_normalize_name_survives_sanitize_folder_name():
    # The invariant promotion rests on. One side of the match is an API name;
    # the other is a folder that name already went through sanitize_folder_name
    # to produce. Any character sanitize drops must normalize away here too, or
    # the two sides never meet and the applicant re-downloads every run.
    for name in ("阿卜杜拉·穆罕默德", "Анна, Мария", "李伟 ☆",
                 "Jane O'Brien", "Jean-Luc O'Connor", "Renée Dupont"):
        assert manifest.normalize_name(manifest.sanitize_folder_name(name)) == \
            manifest.normalize_name(name), name


def test_promote_matches_a_punctuated_non_latin_name(tmp_path):
    # The interpunct cannot survive in a folder name, so the folder and the API
    # name differ by a character. They must still normalize to one key.
    assert manifest.sanitize_folder_name("阿卜杜拉·穆罕默德") == "阿卜杜拉穆罕默德"
    _make_candidate(tmp_path, "阿卜杜拉穆罕默德", 5000)
    m = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert manifest.promote_backfilled(m, [_api("阿卜杜拉·穆罕默德", "id9")]) == 1
    assert m["candidates"]["id9"]["folder"] == "阿卜杜拉穆罕默德"
    assert m["candidates"]["id9"]["has_cv"] is True


def test_normalize_name_keeps_mixed_script_names_distinct():
    # A whole-string ASCII fold erases the CJK and leaves both of these keyed
    # on "smith", which binds one applicant's legacyID to the other's folder.
    assert manifest.normalize_name("李伟 Smith") == "李伟 smith"
    assert manifest.normalize_name("王芳 Smith") == "王芳 smith"


def test_normalize_name_drops_underscores():
    # Underscores were dropped by the old ASCII-only character class, and the
    # Unicode-aware class that replaces it counts "_" as a word character.
    # It has to keep dropping them, in any script.
    assert manifest.normalize_name("Jane_Smith") == "janesmith"
    assert manifest.normalize_name("jane _ smith") == "jane smith"
    assert manifest.normalize_name("李伟_Smith") == "李伟smith"


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
