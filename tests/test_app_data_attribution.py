"""Tests that an applicant's application data lands under THEIR name.

Two paths could file applicant A's records under applicant B:

1. The HTML claim globbed by name-slug PREFIX, and fell back to a bare
   `*.HTML` sweep of the whole job folder when the slug missed. The rename
   to `application.html` then destroyed the filename that would have shown
   whose file it actually was.

2. The screener-answers JSON was keyed off `driver.current_url` while the
   destination folder came from the caller's loop variable. If a navigation
   silently failed, the browser still showed the previous candidate — so the
   previous candidate's answers were written into the current one's folder.

Both are unrecoverable by inspection once they happen: nothing on disk records
which applicant the data came from. These tests pin the attribution.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

indeed_downloader = pytest.importorskip(
    "indeed_downloader",
    reason="needs selenium; install requirements.txt (CI does)",
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """_move_application_files polls for ~30s waiting on OneDrive. The wait is
    real behavior worth keeping in production and pure dead time in a test."""
    monkeypatch.setattr(indeed_downloader.time, "sleep", lambda *_a, **_k: None)


class _SilentLog:
    def __init__(self):
        self.events = []

    def event(self, name, payload=None):
        self.events.append((name, payload))

    def error(self, *args, **kwargs):
        pass

    def names(self):
        return [n for n, _ in self.events]


class _UrlDriver:
    def __init__(self, url=""):
        self.current_url = url


def _downloader(job_folder, url=""):
    dl = object.__new__(indeed_downloader.IndeedDownloader)
    dl.log = _SilentLog()
    dl.driver = _UrlDriver(url)
    dl.current_job_folder = job_folder
    dl.download_folder = str(job_folder.parent)
    dl.current_manifest = None
    dl.checkpoint_data = {
        "downloaded_application_data": [], "downloaded_names": [],
        "downloaded_ids": [], "completed_jobs": [],
    }
    dl.checkpoint_file = job_folder.parent / "checkpoint_unified.json"
    dl.stats = {"app_data_downloaded": 0}
    return dl


# --- 1. The HTML claim must not take a file that is not provably this person's ---

def test_a_longer_name_keeps_its_own_application_file(tmp_path):
    # "ana-ruiz" is a prefix of "ana-ruiz-martinez". Globbing `{slug}*.HTML`
    # let the shorter name claim the longer name's file whenever her own had
    # not landed yet.
    job = tmp_path / "Cook"
    job.mkdir()
    victim = job / "ana-ruiz-martinez-original-application.HTML"
    victim.write_text("ANA RUIZ MARTINEZ - screener answers")

    dl = _downloader(job)
    folder = job / "Ana Ruiz"
    folder.mkdir()

    claimed = dl._move_application_files("Ana Ruiz", folder)

    assert claimed is False
    assert victim.exists(), "another applicant's file was taken"
    assert not (folder / "application.html").exists()


def test_an_unrelated_html_file_is_never_swept_up(tmp_path):
    # The generic `*.HTML` fallback globbed the entire job folder. Its own
    # docstring conceded it "could pick up someone else's file" and claimed
    # html_target uniqueness protected against it — that protects the
    # DESTINATION from a double write, not the SOURCE from being the wrong
    # person's file.
    job = tmp_path / "Cook"
    job.mkdir()
    stranger = job / "someone-else-original-application.HTML"
    stranger.write_text("SOMEONE ELSE - screener answers")

    dl = _downloader(job)
    folder = job / "John Smith"
    folder.mkdir()

    claimed = dl._move_application_files("John Smith", folder)

    assert claimed is False
    assert stranger.exists()
    assert not (folder / "application.html").exists()


def test_the_candidates_own_file_is_still_claimed(tmp_path):
    # The tightening must not break the case the feature exists for.
    job = tmp_path / "Cook"
    job.mkdir()
    mine = job / "john-smith-original-application.HTML"
    mine.write_text("JOHN SMITH - screener answers")

    dl = _downloader(job)
    folder = job / "John Smith"
    folder.mkdir()

    assert dl._move_application_files("John Smith", folder) is True
    assert (folder / "application.html").read_text() == "JOHN SMITH - screener answers"
    assert not mine.exists()


def test_the_late_claim_sweep_does_not_steal_a_longer_names_file(tmp_path):
    # The sweep walks candidates in list order, so a shorter slug reached the
    # file first: one applicant gained someone else's answers, and the real
    # owner then found nothing.
    job = tmp_path / "Cook"
    job.mkdir()
    (job / "ana-ruiz-martinez-original-application.HTML").write_text("MARTINEZ ANSWERS")

    dl = _downloader(job)
    dl.current_manifest = {"schema": 1, "job": {}, "candidates": {}, "runs": []}

    claimed = dl._late_claim_application_html([
        {"name": "Ana Ruiz", "legacy_id": "id-short"},
        {"name": "Ana Ruiz Martinez", "legacy_id": "id-long"},
    ])

    assert claimed == 1
    martinez = job / "Ana Ruiz Martinez" / "application.html"
    assert martinez.exists(), "the real owner did not get their file"
    assert martinez.read_text() == "MARTINEZ ANSWERS"
    assert not (job / "Ana Ruiz" / "application.html").exists()


# --- 2. The JSON must be fetched for the candidate the caller is holding ---

def _recording_downloader(job, url):
    """Records which candidate id the JSON fetch actually asks Indeed for.

    The id is passed to the page as a script ARGUMENT, not interpolated into
    the JS source, so the argument is what has to be captured.
    """
    dl = _downloader(job, url=url)
    dl._indeed_csrf_token = "tok"      # skip perf-log discovery
    dl.calls = []
    dl.requested_id = None

    def _fake_async(_js, candidate_id=None, _csrf=None):
        dl.calls.append(candidate_id)
        dl.requested_id = candidate_id
        return {"ok": True, "csrf_found": True, "status": 200, "data": {"answers": []}}

    dl.driver.execute_async_script = _fake_async
    dl.driver.set_script_timeout = lambda *_a: None
    return dl


def test_json_fetch_refuses_when_the_browser_is_showing_someone_else(tmp_path):
    # driver.get(profile_url) is never verified. A soft redirect or a
    # navigation the SPA swallowed leaves the PREVIOUS candidate's page up,
    # and reading the id off current_url then wrote that person's screener
    # answers into this person's folder.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Current Person"
    folder.mkdir()

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates/view?id=PREVIOUS-PERSON")

    assert dl._fetch_application_json_via_page(folder, legacy_id="CURRENT-PERSON") is False
    assert dl.calls == [], "fetched anyway despite the mismatch"
    assert not (folder / "application.json").exists()
    assert "app_data_json_fetch_skip" in dl.log.names()


def test_json_fetch_uses_the_callers_id_when_the_browser_agrees(tmp_path):
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Right Person"
    folder.mkdir()

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates/view?id=RIGHT-PERSON")

    dl._fetch_application_json_via_page(folder, legacy_id="RIGHT-PERSON")

    assert dl.requested_id == "RIGHT-PERSON"


def test_the_frontend_path_still_works_from_the_url_alone(tmp_path):
    # The Selenium pass reaches a candidate by clicking, so it has no id of
    # its own and the browser URL is genuinely authoritative there. Refusing
    # outright would have silently dropped JSON for that whole path.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Clicked Person"
    folder.mkdir()

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates/view?id=CLICKED-PERSON")

    dl._fetch_application_json_via_page(folder, legacy_id=None)

    assert dl.requested_id == "CLICKED-PERSON"


def test_json_fetch_skips_when_neither_caller_nor_url_has_an_id(tmp_path):
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Someone"
    folder.mkdir()

    dl = _recording_downloader(job, "https://employers.indeed.com/candidates")

    assert dl._fetch_application_json_via_page(folder, legacy_id=None) is False
    assert dl.calls == []


# --- R1: on the LIST view, id= is the JOB id, not a candidate id ---

def test_json_fetch_refuses_a_list_view_url_whose_id_is_the_job(tmp_path):
    # /candidates?id=X is the JOB short id — indeed_downloader reads exactly
    # that shape into current_job_legacy_id, and the real run log shows
    # "URL: .../candidates?id=039e9cc5ab1c" -> "Job legacy id: 039e9cc5ab1c".
    # Treating it as a candidate id asks Indeed for the job and files the
    # answer under whichever applicant happens to be in hand.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Some Applicant"
    folder.mkdir()

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates?id=039e9cc5ab1c&statusName=All")

    assert dl._fetch_application_json_via_page(folder, legacy_id=None) is False
    assert dl.calls == []


def test_json_fetch_accepts_a_profile_view_url(tmp_path):
    # /candidates/view?id=X IS the candidate. That is the shape the frontend
    # path lands on when navigation works, and it must keep working.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Clicked Person"
    folder.mkdir()

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates/view?id=CAND-1&legacyJobId=JOB-1")

    dl._fetch_application_json_via_page(folder, legacy_id=None)

    assert dl.requested_id == "CAND-1"


# --- R3: a name with no ASCII letters must not silently kill the pass ---

def test_a_non_latin_name_does_not_burn_the_timeout_or_fail_the_helper(tmp_path):
    # _candidate_name_slug strips non-ASCII, so 李雷 / Дмитрий / محمد all slug
    # to ''. Returning [] for those is right — we cannot prove ownership — but
    # it must not look like "Chrome never downloaded anything", because five of
    # those in a row abort the app-data pass for every remaining applicant.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "李雷"
    folder.mkdir()

    dl = _downloader(job)
    claimed = dl._move_application_files("李雷", folder)

    assert claimed is False
    assert "app_data_html_unmatchable_name" in dl.log.names()


# --- R4: Chrome's duplicate counter ---

def test_chromes_duplicate_counter_suffix_is_still_this_persons_file(tmp_path):
    # A same-named orphan already in the folder makes Chrome write
    # "<slug>-original-application (1).HTML". The slug is still exactly ours.
    job = tmp_path / "Cook"
    job.mkdir()
    (job / "john-smith-original-application (1).HTML").write_text("JOHN SMITH ANSWERS")

    dl = _downloader(job)
    folder = job / "John Smith"
    folder.mkdir()

    assert dl._move_application_files("John Smith", folder) is True
    assert (folder / "application.html").read_text() == "JOHN SMITH ANSWERS"


# --- R2: an exact slug is not proof when two applicants share a display name ---

def test_two_applicants_sharing_a_display_name_claim_nothing(tmp_path):
    # Indeed derives the filename from the display name, so two different
    # people named Michael Garcia produce the SAME filename and the second
    # gets Chrome's " (1)". Neither file can be attributed. Claiming either
    # one files a stranger's screener answers and destroys the evidence.
    job = tmp_path / "Cook"
    job.mkdir()
    (job / "michael-garcia-original-application.HTML").write_text("APPLICANT A")
    (job / "michael-garcia-original-application (1).HTML").write_text("APPLICANT B")

    dl = _downloader(job)
    dl.current_roster_slugs = {"michael-garcia": 2}
    folder = job / "Michael Garcia"
    folder.mkdir()

    assert dl._move_application_files("Michael Garcia", folder) is False
    assert not (folder / "application.html").exists()
    assert "app_data_html_ambiguous_name" in dl.log.names()


# --- the wiring: deleting either line that carries the fix must fail a test ---

def test_the_helper_threads_the_callers_id_into_the_json_fetch(tmp_path):
    # Both fixes were reachable only if legacy_id actually travels
    # backend pass -> _download_application_data_frontend -> JSON fetch.
    # Every other JSON test calls the fetch directly, so deleting either
    # wiring line left the whole suite green while the bug was fully live.
    job = tmp_path / "Cook"
    job.mkdir()
    folder = job / "Current Person"
    folder.mkdir()
    (job / "current-person-original-application.HTML").write_text("ANSWERS")

    dl = _recording_downloader(
        job, "https://employers.indeed.com/candidates/view?id=PREVIOUS-PERSON")
    dl._last_helper_failure_step = None
    dl.driver.execute_script = lambda *a, **k: None
    dl._find_element_by_selectors = lambda *a, **k: object()
    dl._click_menu_item_by_text = lambda *a, **k: True
    dl._check_app_data_box = lambda *a, **k: True
    dl._maybe_capture_app_data_urls = lambda *a, **k: None

    reached_end = dl._download_application_data_frontend(
        "Current Person", folder, legacy_id="CURRENT-PERSON")

    # Without this guard the assertion below passes whenever the helper bails
    # early — which is exactly what made the first version of this test a
    # tautology that survived deleting the wiring.
    assert reached_end is True, (
        f"helper aborted at {dl._last_helper_failure_step!r} and never reached "
        f"the JSON fetch, so this test proves nothing"
    )
    # The browser is showing someone else. With legacy_id threaded through,
    # the mismatch is caught and nothing is fetched. Without it the fetch
    # falls back to the URL and pulls PREVIOUS-PERSON's answers into this
    # applicant's folder.
    assert dl.calls == [], "the caller's id did not reach the JSON fetch"
    assert not (folder / "application.json").exists()


def test_the_backend_pass_hands_the_api_id_to_the_helper():
    # The other wiring test covers helper -> JSON fetch. This covers the link
    # above it: backend pass -> helper. Driving the whole pass would need the
    # tqdm loop, Selenium navigation and a live-shaped DOM, so this asserts on
    # the production source instead. It is a structural check and brittle to
    # refactoring on purpose: deleting the argument is exactly what it catches,
    # and without it that deletion left all 116 tests green.
    import inspect
    src = inspect.getsource(
        indeed_downloader.IndeedDownloader._run_app_data_pass_backend)

    assert "_download_application_data_frontend(" in src
    after = src.split("_download_application_data_frontend(", 1)[1]

    # Walk to the matching close paren — the naive split on the first ")"
    # lands inside candidate.get('legacy_id') and silently truncates the
    # argument list this test exists to inspect.
    depth, end = 1, None
    for i, ch in enumerate(after):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, "could not find the end of the call"
    call = after[:end]

    assert "legacy_id=" in call, (
        "the backend pass no longer tells the helper WHICH applicant this is; "
        "the JSON fetch then falls back to the browser URL"
    )
    assert "candidate.get('legacy_id')" in call
