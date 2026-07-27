import json
import unicodedata
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


def test_load_rejects_a_manifest_whose_job_block_is_not_a_dict(tmp_path):
    # The caller does `dict(loaded.get('job') or {})` and `loaded['job'].update(...)`
    # on whatever comes back. A scalar here used to reach both and raise mid-run,
    # with Chrome already open and part of the job downloaded.
    (tmp_path / manifest.MANIFEST_FILENAME).write_text(
        '{"schema": 1, "job": "Cook", "candidates": {}, "runs": []}', encoding="utf-8")
    assert manifest.load(tmp_path, timestamp="20260727_120002") is None
    assert (tmp_path / "manifest.corrupt-20260727_120002.json").exists()


def test_load_rejects_a_manifest_whose_runs_is_not_a_list(tmp_path):
    # `previous_runs + loaded['runs']` raises TypeError on anything else.
    (tmp_path / manifest.MANIFEST_FILENAME).write_text(
        '{"schema": 1, "job": {}, "candidates": {}, "runs": 3}', encoding="utf-8")
    assert manifest.load(tmp_path, timestamp="20260727_120003") is None
    assert (tmp_path / "manifest.corrupt-20260727_120003.json").exists()


def test_load_accepts_everything_this_module_writes(tmp_path):
    # The stricter gate must reject only hand-damaged files. A manifest that
    # went through new_manifest, record and a run append still loads.
    m = manifest.new_manifest({"title": "Cook"})
    manifest.record(m, "id1", "Jane Smith", "Jane Smith", True, "2026-07-27")
    m["runs"].append({"at": "2026-07-27T09:00:00", "fetched": 1})
    manifest.save(tmp_path, m)

    assert manifest.load(tmp_path) == m
    assert not list(tmp_path.glob("manifest.corrupt-*.json"))


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


def test_allocate_folder_suffixes_when_names_differ_only_by_case(tmp_path):
    # NTFS and APFS both resolve names case-insensitively, so "John Smith" and
    # "john smith" are ONE directory. Comparing raw strings let the second
    # applicant take the first's folder and overwrite their resume — and
    # because both keys then hit the `existing` branch on every later run, the
    # state stayed wedged instead of self-correcting.
    m = manifest.new_manifest({})
    first = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")
    (first / "resume.pdf").write_bytes(b"A" * 5000)
    manifest.record(m, "id1", "John Smith", first.name, True, "2026-07-27")

    second = manifest.allocate_candidate_folder(tmp_path, m, "id2", "john smith")
    (second / "resume.pdf").write_bytes(b"B" * 5000)
    manifest.record(m, "id2", "john smith", second.name, True, "2026-07-27")

    # The folder keeps the applicant's own spelling; only the collision test folds.
    assert second.name == "john smith (2)"
    assert (first / "resume.pdf").read_bytes()[:1] == b"A"
    assert (second / "resume.pdf").read_bytes()[:1] == b"B"


def test_allocate_folder_treats_nfc_and_nfd_spellings_as_one_folder(tmp_path):
    # backfill_from_disk records folder names straight off the filesystem, and
    # APFS hands back decomposed (NFD) spellings while the Indeed API sends
    # composed (NFC) ones. Both address the same directory, so the second
    # candidate has to take a suffix.
    nfd = unicodedata.normalize("NFD", "José Ruiz")
    nfc = unicodedata.normalize("NFC", "José Ruiz")
    assert nfd != nfc

    m = manifest.new_manifest({})
    manifest.record(m, "id1", nfd, nfd, True, "2026-07-27")

    second = manifest.allocate_candidate_folder(tmp_path, m, "id2", nfc)

    assert second.name == nfc + " (2)"
    assert second.is_dir()


def test_allocation_is_not_reserved_until_record_is_called(tmp_path):
    # allocate_candidate_folder derives the suffix from the manifest, and the
    # manifest only learns about a folder when record() writes it. Pinning the
    # consequence: a caller that allocates twice without recording in between
    # hands two applicants one directory. Task 6 must record before allocating
    # the next candidate, including when the download in between failed.
    m = manifest.new_manifest({})

    a = manifest.allocate_candidate_folder(tmp_path, m, "id1", "John Smith")
    b = manifest.allocate_candidate_folder(tmp_path, m, "id2", "John Smith")
    assert a == b == tmp_path / "John Smith"

    manifest.record(m, "id1", "John Smith", a.name, True, "2026-07-27")

    c = manifest.allocate_candidate_folder(tmp_path, m, "id2", "John Smith")
    d = manifest.allocate_candidate_folder(tmp_path, m, "id3", "John Smith")
    assert c == d == tmp_path / "John Smith (2)"


def test_find_key_by_name_returns_none_when_the_name_is_ambiguous():
    # Two genuine John Smiths already have separate, correct folders. Guessing
    # the first one would write the second applicant's screener Q&A into the
    # first's folder — corrupting a folder that was already right. Returning
    # None lets the caller fall through to its own _nokey: path instead.
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "John Smith", "John Smith", True, "2026-07-27")
    manifest.record(m, "id2", "John Smith", "John Smith (2)", True, "2026-07-27")

    assert manifest.find_key_by_name(m, "John Smith") is None

    # An unambiguous name still resolves — that is the whole point of the lookup.
    manifest.record(m, "id3", "Jane Doe", "Jane Doe", True, "2026-07-27")
    assert manifest.find_key_by_name(m, "Jane Doe") == "id3"


def test_find_key_by_name_counts_case_variants_as_the_same_ambiguous_name():
    # normalize_name folds case, so these are two spellings of one name as far
    # as the lookup is concerned. They are still two different applicants.
    m = manifest.new_manifest({})
    manifest.record(m, "id1", "John Smith", "John Smith", True, "2026-07-27")
    manifest.record(m, "id2", "john smith", "john smith (2)", True, "2026-07-27")

    assert manifest.find_key_by_name(m, "John Smith") is None


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
    # Named so the short_id-only folder sorts FIRST. Precedence has to come
    # from the two-pass structure — all employer_job_id candidates, then all
    # short_id candidates — not from directory order. A single per-folder pass
    # that checks employer then short on each folder in turn would return
    # "Aaa wrong" here, and this is what rules that structure out.
    _seed_job_folder(tmp_path, "Aaa wrong", {"short_id": "111"})
    _seed_job_folder(tmp_path, "Zzz right", {"employer_job_id": "iri-abc", "short_id": "999"})

    assert manifest.resolve_job_folder(tmp_path, "iri-abc", "111") == tmp_path / "Zzz right"


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


def test_resolve_legacy_leaves_a_folder_another_job_already_claimed(tmp_path):
    # This is what keeps two same-titled jobs apart in one all-jobs run. Task 6
    # writes a manifest into a folder the moment it adopts it, so the next job
    # in the loop must not adopt that same folder by name and merge the two
    # jobs' applicants. Only the ID path may return a folder that has a
    # manifest, and only when the ID actually matches.
    _seed_job_folder(tmp_path, "Cook", {"employer_job_id": "iri-first"})

    assert manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", None) is None
    assert manifest.resolve_job_folder(tmp_path, "iri-second", None) is None


def test_resolve_legacy_refuses_two_dated_folders_when_the_job_has_no_date(tmp_path):
    # Single-job mode — the mode this whole task exists to fix — has no posting
    # date, so it cannot tell these two apart. Latching the sorted-first one
    # picks 14-01-2026 over 22-09-2025 because '1' < '2': the format is
    # DD-MM-YYYY, so that is ordering by DAY OF MONTH, not by recency. The
    # current posting's applicants would land in an unrelated posting's
    # directory. The manifest skip cannot save this — on the first run after
    # upgrade neither folder has a manifest.
    (tmp_path / "Cook (14-01-2026)").mkdir()
    (tmp_path / "Cook (22-09-2025)").mkdir()

    assert manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", None) is None


def test_resolve_legacy_adopts_a_single_dated_folder_when_the_job_has_no_date(tmp_path):
    # The refusal above must not cost the adoption this task exists for. One
    # unambiguous dated folder is still the right answer for a dateless
    # single-job run: this is the "Cook (12-05-2026) written by all-jobs mode,
    # re-run in single-job mode" case that used to fork a second folder.
    (tmp_path / "Cook (14-01-2026)").mkdir()

    found = manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", None)

    assert found == tmp_path / "Cook (14-01-2026)"


def test_resolve_legacy_prefers_an_exact_date_over_a_dateless_folder(tmp_path):
    # "Cook" sorts before "Cook (12-05-2026)", so anything that returns the
    # first acceptable candidate rather than holding out for the date match
    # answers "Cook" — a wrong-folder resolution with the right folder sitting
    # right there. An agreeing date is the strongest evidence available and
    # must win over a dateless fallback regardless of directory order.
    (tmp_path / "Cook").mkdir()
    (tmp_path / "Cook (12-05-2026)").mkdir()

    found = manifest.resolve_legacy_folder_by_name(tmp_path, "Cook", "12-05-2026")

    assert found == tmp_path / "Cook (12-05-2026)"


def test_resolve_legacy_handles_a_missing_download_root(tmp_path):
    # Task 6 calls this before backfill_from_disk, which raises
    # FileNotFoundError on a missing folder. Degrading to None keeps the
    # first-ever run — no download root yet — off that path.
    assert manifest.resolve_legacy_folder_by_name(tmp_path / "nope", "Cook", None) is None


def test_count_candidate_folders_counts_applicants_not_bookkeeping_files(tmp_path):
    # The all-jobs pre-scan counts a manifest-less folder this way, so it has
    # to see the applicants and ignore the files the tool drops beside them.
    _make_candidate(tmp_path, "Jane Smith", 5000)
    _make_candidate(tmp_path, "Bob Jones", None)
    (tmp_path / "no_cv.txt").write_text("Carla Diaz\n", encoding="utf-8")
    (tmp_path / "stats.json").write_text("{}", encoding="utf-8")

    assert manifest.count_candidate_folders(tmp_path) == 2


def test_count_candidate_folders_agrees_with_what_backfill_recovers(tmp_path):
    # The summary line and the "Recovered N existing candidates from disk"
    # line the job loop prints a moment later must not contradict each other.
    for i in range(33):
        _make_candidate(tmp_path, f"Person {i:02d}", 5000)

    recovered = manifest.backfill_from_disk(tmp_path, {}, "2026-07-27")

    assert manifest.count_candidate_folders(tmp_path) == len(recovered["candidates"]) == 33


def test_count_candidate_folders_on_a_missing_folder_is_zero(tmp_path):
    assert manifest.count_candidate_folders(tmp_path / "nope") == 0


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


def test_two_same_name_applicants_are_only_separable_by_id(tmp_path):
    """Why the app-data pass has to hand over the API dict it already holds.

    Two Maria Garcias with different Indeed IDs come out of the CV pass in
    two correct folders. A later pass that resolves by NAME cannot tell them
    apart: find_key_by_name refuses an ambiguous name, both fall back to one
    shared _nokey: key, and — nothing calls record() between the two
    allocations — they get the SAME third folder, so both applicants'
    screener Q&A lands in a phantom directory away from either resume.
    Keying on entry_key instead returns each applicant their own folder.
    """
    m = manifest.new_manifest({})
    first, second = _api("Maria Garcia", "id-aaa"), _api("Maria Garcia", "id-bbb")

    for candidate in (first, second):
        key = manifest.entry_key(candidate)
        folder = manifest.allocate_candidate_folder(tmp_path, m, key, candidate["name"])
        manifest.record(m, key, candidate["name"], folder.name, True, "2026-07-27")

    assert m["candidates"]["id-aaa"]["folder"] == "Maria Garcia"
    assert m["candidates"]["id-bbb"]["folder"] == "Maria Garcia (2)"

    # The property the fix depends on: the id resolves, the shared name does not.
    assert manifest.find_key_by_name(m, "Maria Garcia") is None
    assert manifest.allocate_candidate_folder(
        tmp_path, m, manifest.entry_key(first), "Maria Garcia") == tmp_path / "Maria Garcia"
    assert manifest.allocate_candidate_folder(
        tmp_path, m, manifest.entry_key(second), "Maria Garcia") == tmp_path / "Maria Garcia (2)"

    # And the shape of the bug that made passing the dict necessary.
    nokey = manifest.NOKEY_PREFIX + manifest.normalize_name("Maria Garcia")
    phantom_a = manifest.allocate_candidate_folder(tmp_path, m, nokey, "Maria Garcia")
    phantom_b = manifest.allocate_candidate_folder(tmp_path, m, nokey, "Maria Garcia")
    assert phantom_a == phantom_b == tmp_path / "Maria Garcia (3)"


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
