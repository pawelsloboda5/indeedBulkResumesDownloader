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
