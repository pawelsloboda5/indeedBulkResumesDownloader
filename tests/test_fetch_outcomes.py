"""Tests for the API enumeration path: does it know when it failed?

Every counting bug found in review traced to one thing — a failed query and an
empty job both came back as ([], 0, False), so nothing downstream could tell
them apart. A total failure in single-job mode reached "All CVs are already
downloaded!", and the announced-vs-fetched guard compared an active-only
denominator against an all-disposition numerator, so it could never fire.

These tests stub the driver. fetch_candidates_api's only outside contact is
self.driver.execute_script returning Indeed's parsed JSON, so a fake driver
covers the whole decision surface without a browser or a live session.
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


class _FakeDriver:
    """Returns a queued response per execute_script call, or raises."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def execute_script(self, _js):
        self.calls += 1
        item = self._responses.pop(0) if self._responses else None
        if isinstance(item, Exception):
            raise item
        return item


class _SilentLog:
    def __init__(self):
        self.events = []

    def event(self, name, payload=None):
        self.events.append((name, payload))

    def error(self, *args, **kwargs):
        pass


def _downloader(responses):
    dl = object.__new__(indeed_downloader.IndeedDownloader)
    dl.driver = _FakeDriver(responses)
    dl.log = _SilentLog()
    dl.api_key = "test-api-key"
    dl.ctk = "test-ctk"
    dl.current_job_id = None
    return dl


def _page(matches, total, has_next=False):
    return {
        "data": {
            "findRCPMatches": {
                "overallMatchCount": total,
                "matchConnection": {
                    "matches": matches,
                    "pageInfo": {"hasNextPage": has_next},
                },
            }
        }
    }


def _match(legacy_id, name="Someone"):
    return {
        "candidateSubmission": {
            "data": {
                "legacyID": legacy_id,
                "profile": {"name": {"displayName": name}},
                "resume": {"downloadUrl": f"https://x/{legacy_id}"},
            }
        }
    }


# --- fetch_candidates_api: failure must be distinguishable from emptiness ---

def test_a_genuinely_empty_job_reports_success():
    dl = _downloader([_page([], 0)])
    _matches, _total, _next, ok = dl.fetch_candidates_api()
    assert ok is True


def test_a_graphql_error_reports_failure():
    # This is the AUTO_REJECTED / WITHDRAWN case seen on every real run:
    # AllMatchProvidersFailedException with valid auth.
    dl = _downloader([{"errors": [{"message": "AllMatchProvidersFailedException"}]}])
    matches, total, _next, ok = dl.fetch_candidates_api()
    assert ok is False
    assert matches == [] and total == 0


def test_a_null_response_reports_failure():
    dl = _downloader([None])
    _matches, _total, _next, ok = dl.fetch_candidates_api()
    assert ok is False


def test_a_transport_exception_reports_failure():
    dl = _downloader([RuntimeError("connection reset")])
    _matches, _total, _next, ok = dl.fetch_candidates_api()
    assert ok is False


def test_missing_auth_reports_failure():
    dl = _downloader([_page([_match("id1")], 1)])
    dl.api_key = None
    _matches, _total, _next, ok = dl.fetch_candidates_api()
    assert ok is False
    assert dl.driver.calls == 0


# --- _fetch_candidates_batch: a mid-pagination failure is not "end of list" ---

def test_batch_reports_success_for_a_complete_walk():
    dl = _downloader([
        _page([_match("id1")], 2, has_next=True),
        _page([_match("id2")], 2, has_next=False),
    ])
    candidates, total, ok = dl._fetch_candidates_batch(["NEW"])
    assert ok is True
    assert total == 2
    assert sorted(c["legacy_id"] for c in candidates) == ["id1", "id2"]


def test_batch_reports_failure_when_a_later_page_dies():
    # Page 2 failing looked identical to reaching the end, so the caller got a
    # truncated list it believed was complete — and mark_stale then flagged
    # every applicant who did not make it into that list.
    dl = _downloader([
        _page([_match("id1")], 250, has_next=True),
        {"errors": [{"message": "boom"}]},
    ])
    candidates, total, ok = dl._fetch_candidates_batch(["NEW"])
    assert ok is False
    assert total == 250
    assert len(candidates) == 1


def test_batch_advances_by_the_page_size_actually_returned():
    # offset += 100 assumed the server honored limit=100. If it clamps the page
    # while still reporting hasNextPage, a fixed stride skips records silently.
    dl = _downloader([
        _page([_match(f"id{i}") for i in range(20)], 40, has_next=True),
        _page([_match(f"id{i}") for i in range(20, 40)], 40, has_next=False),
    ])
    candidates, _total, ok = dl._fetch_candidates_batch(["NEW"])
    assert ok is True
    assert len(candidates) == 40


# --- credential redaction: nothing full-length reaches the teed run log ---

class _UrlDriver:
    """Just enough driver for the diagnostics block: a current_url."""

    def __init__(self, url):
        self.current_url = url


def test_the_diagnostics_block_never_prints_a_full_ctk(capsys, monkeypatch, tmp_path):
    # Every print() is teed into logs/latest.log, and the tool then tells HR to
    # send that file when something breaks. A full CTK here walked a live
    # session credential into an email. It used to print in full; the API key
    # one line above was already truncated.
    ctk = "1jndggp6uha0qanp"          # the real value from run_20260429_165717.log
    api_key = "0f2b0de1b8ffdeadbeef"

    dl = object.__new__(indeed_downloader.IndeedDownloader)
    dl.driver = _UrlDriver("https://employers.indeed.com/candidates?id=abc123&statusName=All")
    dl.log = _SilentLog()
    dl.ctk = ctk
    dl.api_key = api_key
    dl.current_job_id = None
    dl.current_job_legacy_id = None
    dl.download_folder = str(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    try:
        dl.run_backend_single_job()
    except Exception:
        pass  # Aborts on the missing job IRI — after the diagnostics printed.

    out = capsys.readouterr().out
    assert "CTK:" in out, "diagnostics block did not run — this test proves nothing"
    assert ctk not in out
    assert api_key not in out
    assert ctk[:6] in out       # a prefix is kept: the question is "did we get one"


class _PerfLogDriver:
    """A driver whose performance log replays real captured request URLs."""

    def __init__(self, urls):
        self._urls = urls

    def get_log(self, _kind):
        return [
            {"message": json.dumps({"message": {
                "method": "Network.requestWillBeSent",
                "params": {"request": {"url": u, "method": "GET"}},
            }})}
            for u in self._urls
        ]


def _capture_to(tmp_path, urls):
    """Run the real _maybe_capture_app_data_urls and return what it wrote."""
    dl = object.__new__(indeed_downloader.IndeedDownloader)
    dl.driver = _PerfLogDriver(urls)
    dl.log = _SilentLog()
    dl.log_folder = str(tmp_path)
    dl._maybe_capture_app_data_urls()
    written = tmp_path / "app_data_urls.json"
    return json.loads(written.read_text()) if written.exists() else []


def test_captured_urls_never_contain_a_csrf_token(tmp_path):
    # app_data_urls.json carried 60 live indeedcsrftoken values, because the
    # whole URL was persisted verbatim. Verified against the real shape.
    entries = _capture_to(tmp_path, [
        "https://employers.indeed.com/api/v2/iq/job/answers"
        "?candidateIds=0d0bd0251e19&indeedcsrftoken=69a152157701bf7442157cb0ba24d69c"
    ])

    blob = json.dumps(entries)
    assert "69a152157701bf7442157cb0ba24d69c" not in blob
    assert "indeedcsrftoken=" not in blob
    assert "0d0bd0251e19" not in blob
    # The endpoint shape — the thing this file exists to capture — survives.
    assert any(e["url"].endswith("/api/v2/iq/job/answers") for e in entries)
    assert any("indeedcsrftoken" in e["query_param_names"] for e in entries)


def test_telemetry_urls_are_not_captured_just_for_saying_application(tmp_path):
    # Matching the whole URL meant any Indeed URL with "application" anywhere
    # in its query landed here — 38 telemetry requests did, with their params.
    entries = _capture_to(tmp_path, [
        "https://t.indeed.com/signals/gnav/log?application=globalnav&tk=secret-tk",
    ])

    assert entries == []


def test_the_real_app_data_endpoint_is_still_captured(tmp_path):
    # The path anchor must not throw away the one endpoint this file exists for.
    # /api/v2/iq/job/answers has no "application" in its path — it matched the
    # old whole-URL pattern only via an indeedClientApplication query param, so
    # anchoring naively dropped all 60 real entries and kept the JS bundles.
    entries = _capture_to(tmp_path, [
        "https://employers.indeed.com/api/v2/iq/job/answers?candidateIds=x&indeedcsrftoken=y",
        "https://d3oklwo3y1bx83.cloudfront.net/talent-original-application-modules/remoteEntry.js",
    ])

    urls = [e["url"] for e in entries]
    assert urls == ["https://employers.indeed.com/api/v2/iq/job/answers"]
