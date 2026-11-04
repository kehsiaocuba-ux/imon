import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from imon.cli import main
from imon.monitor import (
    KIND_REFERENCE,
    KIND_USER,
    ActiveMonitor,
    InternetMonitor,
    ResultStore,
    PERIOD_PRESETS,
    build_availability_report,
    build_details,
    build_report,
    check_now,
    format_availability_text,
    format_details_text,
    format_duration,
    format_report_text,
    parse_timestamp,
    resolve_period,
    validate_url,
)
from imon.server import _parse_multipart
from imon.server import ReportServer


class _Target:
    """A tiny local web server whose health can be flipped from tests."""

    def __init__(self):
        self.healthy = True
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code = 200 if outer.healthy else 503
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def target():
    t = _Target()
    yield t
    t.close()


class _SpeedServer:
    """Local stand-in for a speed-test endpoint: GET ?bytes=N streams N bytes, POST swallows the body."""

    def __init__(self, fail_upload=False):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                n = int(parse_qs(urlparse(self.path).query).get("bytes", ["1024"])[0])
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                chunk = b"x" * 65536
                while n > 0:
                    self.wfile.write(chunk[:n])
                    n -= 65536

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(500 if outer.fail_upload else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.fail_upload = fail_upload
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def speed_server():
    s = _SpeedServer()
    yield s
    s.close()


@pytest.fixture
def reference():
    """A local 'reference' site standing in for Google/Cloudflare/Microsoft."""
    t = _Target()
    yield t
    t.close()


@pytest.fixture
def store():
    s = ResultStore(":memory:")
    yield s
    s.close()


def _res(url, available, checked_at, cycle=None, latency=10.0):
    return {"url": url, "status": 200 if available else 500, "latency_ms": latency if available else 0,
            "available": available, "error": None if available else "HTTPError", "checked_at": checked_at,
            "cycle": cycle}


def _ts(i):
    return "2024-01-01T00:{:02d}:00+00:00".format(i)


# --- InternetMonitor -------------------------------------------------------

def test_monitor_initialization():
    m = InternetMonitor(["https://example.com"])
    assert "https://example.com" in m.urls


def test_check_url_failure():
    m = InternetMonitor()
    res = m.check_url("https://invalid-domain-xyz-99999.com", timeout=1)
    assert res["available"] is False
    assert res["status"] == 0
    assert res["error"]
    assert res["checked_at"]


def test_check_url_success_and_http_error(target):
    m = InternetMonitor([target.url])
    ok = m.check_url(target.url)
    assert ok["available"] is True and ok["status"] == 200 and ok["latency_ms"] > 0

    target.healthy = False
    bad = m.check_url(target.url)
    assert bad["available"] is False and bad["status"] == 503


# --- ResultStore -------------------------------------------------------------

def test_store_records_and_filters(store):
    store.record(_res("http://a", True, _ts(0)))
    store.record([_res("http://a", False, _ts(1)), _res("http://b", True, _ts(1))], cycle="c1")
    assert store.urls() == ["http://a", "http://b"]
    assert len(store.results()) == 3
    assert len(store.results(url="http://a")) == 2
    assert len(store.results(since=_ts(1))) == 2
    newest = store.results(limit=1)
    assert len(newest) == 1 and newest[0]["checked_at"] == _ts(1) and newest[0]["cycle"] == "c1"
    assert store.results(url="http://a")[1]["available"] is False


def test_store_manages_sites(store):
    assert store.sites() == []
    refs = store.ensure_reference_sites()
    assert len(refs) == 3  # at least three reliable sites by default
    assert store.ensure_reference_sites(["http://other"]) == refs  # does not re-seed
    assert store.add_site("https://mine.example/ ") is True  # normalised
    assert store.add_site("https://mine.example/") is False  # duplicate
    assert store.site_urls(KIND_USER) == ["https://mine.example/"]
    assert store.site_kinds()["https://mine.example/"] == KIND_USER
    assert store.site_urls()[:3] == refs  # reference sites listed first
    assert store.remove_site("https://mine.example/") is True
    assert store.remove_site("https://mine.example/") is False
    with pytest.raises(ValueError):
        store.add_site("ftp://nope")
    with pytest.raises(ValueError):
        store.add_site("https://a", kind="bogus")


def test_store_replace_site(store):
    store.ensure_reference_sites(["http://r1", "http://r2"])
    store.add_site("http://mine")
    assert store.replace_site("http://r1", "http://r9") is True
    assert store.site_urls(KIND_REFERENCE) == ["http://r2", "http://r9"]  # kind kept
    assert store.replace_site("http://nope", "http://x") is False
    with pytest.raises(ValueError):  # cannot collide with an existing site
        store.replace_site("http://r9", "http://mine")
    with pytest.raises(ValueError):
        store.replace_site("http://r9", "garbage")
    assert store.replace_site("http://r9", "http://r9") is True  # no-op is fine


def test_validate_url():
    assert validate_url(" HTTPS://example.com ") == "HTTPS://example.com"
    for bad in ("", "example.com", "http://", "http://a b"):
        with pytest.raises(ValueError):
            validate_url(bad)


def test_store_persists_to_disk(tmp_path):
    path = str(tmp_path / "h.db")
    s = ResultStore(path)
    s.add_site("http://a")
    s.record({"url": "http://a", "status": 200, "latency_ms": 1, "available": True})
    s.close()
    s2 = ResultStore(path)
    assert len(s2.results()) == 1 and s2.site_urls() == ["http://a"]
    s2.close()


def test_store_rebuilds_cycle_summary_for_old_databases(tmp_path):
    import sqlite3
    path = str(tmp_path / "v2.db")
    s = ResultStore(path)
    s.add_site("http://ref", KIND_REFERENCE)
    s.add_site("http://site")
    s.record([_res("http://ref", True, _at(0)), _res("http://site", True, _at(0))], cycle="c0")
    s.record([_res("http://ref", False, _at(10)), _res("http://site", False, _at(10))], cycle="c1")
    s.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE cycles")  # simulate a database from before the summary existed
    conn.commit()
    conn.close()
    s = ResultStore(path)
    assert [(c["checked_at"], c["available"], c["total"]) for c in s.cycle_states()] == [(_at(0), True, 1), (_at(10), False, 1)]
    rep = build_report(s)
    assert rep["internet"]["state"] == "DOWN" and rep["sites"][0]["inconclusive"] == 1
    s.close()


def test_store_upgrades_old_schema(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, url TEXT, status INTEGER, latency_ms REAL, "
                 "available INTEGER, error TEXT, checked_at TEXT)")
    conn.execute("INSERT INTO results VALUES (1, 'http://a', 200, 1, 1, NULL, '2024-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    s = ResultStore(path)
    assert s.results()[0]["cycle"] is None
    s.close()


# --- ActiveMonitor -----------------------------------------------------------

def test_active_monitor_runs_cycles_and_records(target, store):
    seen = []
    am = ActiveMonitor([target.url], store, interval=0.05, on_results=seen.append)
    assert store.site_kinds() == {target.url: KIND_USER}
    am.run(max_cycles=3)
    assert am.cycles == 3
    assert len(seen) == 3
    results = store.results(url=target.url)
    assert len(results) == 3
    assert len({r["cycle"] for r in results}) == 3  # each cycle tagged


def test_active_monitor_picks_up_site_changes(target, reference, store):
    am = ActiveMonitor(store=store, interval=0.05)
    store.add_site(reference.url, KIND_REFERENCE)
    am.run_once()
    assert store.urls() == [reference.url]
    store.add_site(target.url)  # e.g. added from the dashboard
    am.run_once()
    assert sorted(store.urls()) == sorted([reference.url, target.url])
    store.remove_site(target.url)
    n = len(store.results(url=target.url))
    am.run_once()
    assert len(store.results(url=target.url)) == n


def test_check_now_all_and_single(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    store.add_site(target.url)
    results = check_now(store)
    assert [r["url"] for r in results] == [reference.url, target.url]
    assert len({r["cycle"] for r in results}) == 1

    # a single-site check also checks the reference sites (same cycle)
    results = check_now(store, [target.url])
    assert [r["url"] for r in results] == [reference.url, target.url]
    assert len(store.results(url=reference.url)) == 2
    assert check_now(store, [reference.url]) and len(store.results(url=reference.url)) == 3

    only = check_now(store, [target.url], include_reference=False)
    assert [r["url"] for r in only] == [target.url]
    assert [r["url"] for r in check_now(store, [])] == [reference.url]  # reference sites only


def test_active_monitor_requires_store():
    with pytest.raises(ValueError):
        ActiveMonitor(["http://a"], None)


def test_active_monitor_background_thread_start_stop(target, store):
    am = ActiveMonitor([target.url], store, interval=0.05)
    am.start()
    assert am.running
    deadline = time.time() + 5
    while len(store.results()) < 2 and time.time() < deadline:
        time.sleep(0.02)
    am.stop()
    assert not am.running
    assert len(store.results()) >= 2


def test_active_monitor_rejects_bad_interval(store):
    with pytest.raises(ValueError):
        ActiveMonitor(["http://a"], store, interval=0)


# --- Reports -----------------------------------------------------------------

def _seed(store):
    store.add_site("http://a")
    store.add_site("http://b")
    avail = [True, False, False, True, False]  # one closed incident + one ongoing
    for i, a in enumerate(avail):
        store.record(_res("http://a", a, _ts(i)), cycle="c{}".format(i))
    store.record(_res("http://b", True, _ts(0), latency=20), cycle="c0")


def _seed_with_reference(store):
    """Two reference sites + one user site over 5 cycles.

    cycle: 0    1    2    3    4
    ref1 : up   up   DOWN DOWN up
    ref2 : up   DOWN DOWN DOWN up      -> internet down in cycles 2 and 3
    site : up   up   DOWN DOWN DOWN    -> cycles 2,3 inconclusive; 4 is real downtime
    """
    store.add_site("http://ref1", KIND_REFERENCE)
    store.add_site("http://ref2", KIND_REFERENCE)
    store.add_site("http://site")
    ref1 = [True, True, False, False, True]
    ref2 = [True, False, False, False, True]
    site = [True, True, False, False, False]
    for i in range(5):
        store.record([_res("http://ref1", ref1[i], _ts(i)), _res("http://ref2", ref2[i], _ts(i)),
                      _res("http://site", site[i], _ts(i))], cycle="c{}".format(i))


def test_build_report_summary_and_incidents(store):
    _seed(store)
    report = build_report(store)
    assert report["internet"]["state"] == "UNKNOWN"  # no reference sites registered
    assert report["reference"] == []
    by_url = {s["url"]: s for s in report["sites"]}

    a = by_url["http://a"]
    assert a["checks"] == 5 and a["up"] == 2 and a["down"] == 3
    assert a["uptime_pct"] == 40.0
    assert a["avg_latency_ms"] == 10.0
    assert a["current_state"] == "DOWN"
    assert len(a["incidents"]) == 2
    assert a["incidents"][0]["ongoing"] is False and a["incidents"][0]["checks"] == 2
    assert a["incidents"][1]["ongoing"] is True
    assert a["incidents"][0]["error"] == "HTTPError" and a["incidents"][1]["error"] == "HTTPError"
    assert a["avg_latency_ms"] == 10.0 and a["min_latency_ms"] == 10.0 and a["max_latency_ms"] == 10.0

    b = by_url["http://b"]
    assert b["uptime_pct"] == 100.0 and b["current_state"] == "UP" and b["incidents"] == []

    assert report["summary"] == {
        "sites": 2, "sites_up": 1, "sites_down": 1, "checks": 6, "inconclusive": 0,
        "overall_uptime_pct": 50.0, "open_incidents": 1,
    }
    # JSON-serialisable for the API / --json flag
    json.dumps(report)


def test_report_internet_state_from_reference_sites(store):
    _seed_with_reference(store)
    report = build_report(store)
    inet = report["internet"]
    assert inet["state"] == "UP" and inet["reference_up"] == 2 and inet["reference_total"] == 2
    assert inet["cycles"] == 5 and inet["uptime_pct"] == 60.0
    assert [s["url"] for s in report["reference"]] == ["http://ref1", "http://ref2"]
    assert all(s["kind"] == KIND_REFERENCE for s in report["reference"])
    # reference site downtime is counted as-is (no "inconclusive" for references)
    assert {s["url"]: s["uptime_pct"] for s in report["reference"]} == {"http://ref1": 60.0, "http://ref2": 40.0}


def test_report_marks_user_failures_inconclusive_when_internet_down(store):
    _seed_with_reference(store)
    site = build_report(store)["sites"][0]
    assert site["url"] == "http://site" and site["kind"] == KIND_USER
    assert site["checks"] == 5
    assert site["inconclusive"] == 2          # cycles 2 and 3
    assert site["up"] == 2 and site["down"] == 1
    assert site["uptime_pct"] == round(100 * 2 / 3, 2)  # inconclusive excluded
    assert site["current_state"] == "DOWN"
    assert len(site["incidents"]) == 1
    assert site["incidents"][0]["start"] == _ts(4) and site["incidents"][0]["ongoing"] is True


def test_report_state_unknown_when_latest_check_inconclusive(store):
    store.add_site("http://ref", KIND_REFERENCE)
    store.add_site("http://site")
    store.record([_res("http://ref", True, _ts(0)), _res("http://site", True, _ts(0))], cycle="c0")
    store.record([_res("http://ref", False, _ts(1)), _res("http://site", False, _ts(1))], cycle="c1")
    report = build_report(store)
    assert report["internet"]["state"] == "DOWN"
    site = report["sites"][0]
    assert site["current_state"] == "UNKNOWN" and site["incidents"] == [] and site["uptime_pct"] == 100.0
    assert report["summary"]["sites_down"] == 0 and report["summary"]["inconclusive"] == 1


def test_one_reference_site_up_means_internet_up(store):
    for u in ("http://r1", "http://r2", "http://r3"):
        store.add_site(u, KIND_REFERENCE)
    store.record([_res("http://r1", False, _ts(0)), _res("http://r2", False, _ts(0)), _res("http://r3", True, _ts(0))], cycle="c0")
    inet = build_report(store)["internet"]
    assert inet["state"] == "UP" and inet["reference_up"] == 1


def test_build_report_since_and_url_filters(store):
    _seed(store)
    report = build_report(store, since=_ts(3), urls=["http://a"])
    assert [s["url"] for s in report["sites"]] == ["http://a"]
    assert report["sites"][0]["checks"] == 2


def test_build_report_url_filter_keeps_reference_context(store):
    _seed_with_reference(store)
    report = build_report(store, urls=["http://site"])
    assert report["internet"]["state"] == "UP"
    assert report["sites"][0]["inconclusive"] == 2


def test_format_report_text(store):
    assert "No results recorded yet" in format_report_text(build_report(store))
    _seed(store)
    text = format_report_text(build_report(store))
    assert "http://a" in text and "DOWN" in text and "40.00%" in text
    assert "overall uptime: 50.00%" in text
    assert "INTERNET: UNKNOWN" in text


def test_format_report_text_with_reference(store):
    _seed_with_reference(store)
    text = format_report_text(build_report(store))
    assert "INTERNET: UP (2/2 reference sites reachable, 60.00% of 5 cycles)" in text
    assert "http://ref1" in text and "http://site" in text and "inconclusive: 2" in text


# --- Time-window availability reports ---------------------------------------

T0 = datetime(2024, 3, 10, 12, 0, tzinfo=timezone.utc)


def _at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _seed_timeline(store, states, ref_states=None, step=10):
    """Record one check every ``step`` minutes for http://site (and a reference site)."""
    store.add_site("http://ref", KIND_REFERENCE)
    store.add_site("http://site")
    for i, st in enumerate(states):
        ref_ok = True if ref_states is None else ref_states[i]
        store.record([_res("http://ref", ref_ok, _at(i * step)), _res("http://site", st, _at(i * step))],
                     cycle="c{}".format(i))


def test_resolve_period_presets():
    now = datetime(2024, 3, 10, 15, 30, 0, tzinfo=timezone.utc)
    assert [p for p, _ in PERIOD_PRESETS] == ["last-hour", "today", "yesterday", "last-24h", "last-7d", "last-30d", "all"]
    w = resolve_period("last-hour", now=now)
    assert (w["start"], w["end"]) == ("2024-03-10T14:30:00+00:00", "2024-03-10T15:30:00+00:00")
    w = resolve_period("today", now=now)
    assert (w["start"], w["end"]) == ("2024-03-10T00:00:00+00:00", "2024-03-10T15:30:00+00:00")
    w = resolve_period("yesterday", now=now)
    assert (w["start"], w["end"]) == ("2024-03-09T00:00:00+00:00", "2024-03-10T00:00:00+00:00")
    assert resolve_period("last-24h", now=now)["start"] == "2024-03-09T15:30:00+00:00"
    assert resolve_period("last-7d", now=now)["start"] == "2024-03-03T15:30:00+00:00"
    assert resolve_period("last-30d", now=now)["start"] == "2024-02-09T15:30:00+00:00"
    assert resolve_period("all", now=now)["start"] is None
    assert resolve_period(now=now)["period"] == "last-24h"  # default
    with pytest.raises(ValueError):
        resolve_period("last-century", now=now)


def test_resolve_period_custom_interval():
    now = datetime(2024, 3, 10, 15, 30, 0, tzinfo=timezone.utc)
    w = resolve_period(since="2024-03-01T10:00", until="2024-03-02", now=now)  # naive = UTC
    assert w["period"] == "custom"
    assert (w["start"], w["end"]) == ("2024-03-01T10:00:00+00:00", "2024-03-02T00:00:00+00:00")
    w = resolve_period("last-7d", since="2024-03-01T10:00:00Z", now=now)  # custom wins over preset
    assert w["period"] == "custom" and w["end"] == "2024-03-10T15:30:00+00:00"
    w = resolve_period(since="2024-03-01T12:00:00+02:00", now=now)
    assert w["start"] == "2024-03-01T10:00:00+00:00"  # converted to UTC
    with pytest.raises(ValueError):
        resolve_period(since="2024-03-05", until="2024-03-01", now=now)
    with pytest.raises(ValueError):
        parse_timestamp("not a date")
    assert parse_timestamp("") is None
    assert parse_timestamp("2024-03-01T10:00:00 00:00") == parse_timestamp("2024-03-01T10:00:00+00:00")


def test_availability_time_without_incidents(store):
    # 7 checks, 10 min apart: up up DOWN DOWN up up up  -> 20 min down of 60
    _seed_timeline(store, [True, True, False, False, True, True, True])
    now = T0 + timedelta(minutes=60)
    rep = build_availability_report(store, since=_at(0), until=_at(60), now=now)
    assert rep["window"]["period"] == "custom"
    site = rep["sites"][0]
    assert site["url"] == "http://site"
    assert site["observed_seconds"] == 3600 and site["downtime_seconds"] == 1200
    assert site["time_without_incidents_pct"] == 66.67  # two decimals
    assert site["incidents"] == 1
    assert (site["measures"], site["measures_ok"], site["measures_failed"], site["inconclusive"]) == (7, 5, 2, 0)
    assert site["incidents_per_1000_ok"] == 200.0
    sm = rep["summary"]
    assert sm["sites"] == 1 and sm["incidents"] == 1 and sm["measures_ok"] == 5
    assert sm["time_without_incidents_pct"] == site["time_without_incidents_pct"]
    json.dumps(rep)


def test_availability_window_uses_state_before_window(store):
    _seed_timeline(store, [True, True, False, False, True, True, True])
    # window 12:25 -> 12:45: state at 12:25 comes from the 12:20 check (down)
    rep = build_availability_report(store, since=_at(25), until=_at(45), now=T0 + timedelta(minutes=60))
    site = rep["sites"][0]
    assert site["observed_seconds"] == 1200 and site["downtime_seconds"] == 900  # 12:25-12:40
    assert site["time_without_incidents_pct"] == 25.0
    assert site["incidents"] == 1  # started before the window, still counts
    assert (site["measures"], site["measures_ok"], site["measures_failed"]) == (2, 1, 1)
    # a window fully inside the healthy tail
    rep = build_availability_report(store, since=_at(41), until=_at(59), now=T0 + timedelta(minutes=60))
    site = rep["sites"][0]
    assert site["time_without_incidents_pct"] == 100.0 and site["incidents"] == 0 and site["measures"] == 1


def test_availability_ongoing_incident_counts_until_now(store):
    _seed_timeline(store, [True, False])  # down since 12:10
    now = T0 + timedelta(minutes=30)
    rep = build_availability_report(store, since=_at(0), now=now)  # until defaults to now
    site = rep["sites"][0]
    assert site["observed_seconds"] == 1800 and site["downtime_seconds"] == 1200
    assert site["time_without_incidents_pct"] == 33.33 and site["incidents"] == 1


def test_availability_ignores_inconclusive_checks(store):
    # same failures as above, but the internet was down during them
    _seed_timeline(store, [True, True, False, False, True, True, True],
                   ref_states=[True, True, False, False, True, True, True])
    rep = build_availability_report(store, since=_at(0), until=_at(60), now=T0 + timedelta(minutes=60))
    site = rep["sites"][0]
    assert site["time_without_incidents_pct"] == 100.0 and site["downtime_seconds"] == 0
    assert site["incidents"] == 0 and site["inconclusive"] == 2 and site["measures_failed"] == 0
    assert rep["summary"]["inconclusive"] == 2


def test_availability_presets_and_empty_window(store):
    _seed_timeline(store, [True, True, False, True])
    now = T0 + timedelta(minutes=40)
    rep = build_availability_report(store, "last-hour", now=now)
    assert rep["window"]["period"] == "last-hour" and rep["window"]["label"] == "Last hour"
    assert rep["sites"][0]["incidents"] == 1 and rep["sites"][0]["downtime_seconds"] == 600
    # observation starts at the first check (12:00), not at the window start (11:40)
    assert rep["sites"][0]["observed_seconds"] == 2400
    rep = build_availability_report(store, "all", now=now)
    assert rep["window"]["start"] is None and rep["sites"][0]["incidents"] == 1
    # a window before any data: the site is still listed, without measures
    rep = build_availability_report(store, since="2024-01-01", until="2024-01-02", now=now)
    assert [(x["url"], x["measures"], x["time_without_incidents_pct"]) for x in rep["sites"]] == [("http://site", 0, None)]
    assert rep["summary"]["time_without_incidents_pct"] is None
    # reference sites are never part of this report
    assert all(s["url"] != "http://ref" for s in build_availability_report(store, "all", now=now)["sites"])


def test_format_availability_text_and_duration(store):
    assert format_duration(5) == "5s" and format_duration(125) == "2m 5s"
    assert format_duration(3700) == "1h 1m" and format_duration(90000) == "1d 1h"
    _seed_timeline(store, [True, True, False, False, True, True, True])
    rep = build_availability_report(store, since=_at(0), until=_at(60), now=T0 + timedelta(minutes=60))
    text = format_availability_text(rep)
    assert "TIME WITHOUT INCIDENTS: 66.67%  (downtime 20m 0s)" in text
    assert "INCIDENTS / MEASURES WITHOUT INCIDENTS: 1 / 5" in text
    assert "INTERNET WITHOUT OUTAGES: 100.00%  (0 outage(s), 7 of 7 cycles OK" in text
    assert "http://site" in text and "66.67%" in text and "66.667" not in text
    empty = ResultStore(":memory:")
    assert "No websites monitored yet" in format_availability_text(build_availability_report(empty))
    empty.close()


def test_availability_internet_category(store):
    # internet down in cycles 2 and 3 (all reference sites failing)
    _seed_timeline(store, [True] * 7, ref_states=[True, True, False, False, True, True, True])
    store.add_site("http://ref2", KIND_REFERENCE)
    rep = build_availability_report(store, since=_at(0), until=_at(60), now=T0 + timedelta(minutes=60))
    inet = rep["internet"]
    assert inet["category"] == "internet" and inet["reference_total"] == 2
    assert inet["measures"] == 7 and inet["measures_ok"] == 5 and inet["measures_failed"] == 2
    assert inet["incidents"] == 1 and inet["downtime_seconds"] == 1200
    assert inet["time_without_incidents_pct"] == 66.67
    # the outage that started before the window is still an incident
    rep = build_availability_report(store, since=_at(25), until=_at(45), now=T0 + timedelta(minutes=60))
    assert rep["internet"]["incidents"] == 1 and rep["internet"]["downtime_seconds"] == 900
    # without any reference data
    empty = ResultStore(":memory:")
    inet = build_availability_report(empty)["internet"]
    assert inet["measures"] == 0 and inet["time_without_incidents_pct"] is None and inet["reference_total"] == 0
    empty.close()


def test_availability_newly_added_site_shows_up_immediately(store):
    store.add_site("http://ref", KIND_REFERENCE)
    store.record(_res("http://ref", True, _ts(0)), cycle="c0")
    store.add_site("http://new")  # just added, not checked yet
    rep = build_availability_report(store, "last-24h")
    assert [x["url"] for x in rep["sites"]] == ["http://new"]
    assert rep["sites"][0]["measures"] == 0 and rep["sites"][0]["time_without_incidents_pct"] is None

    # first check happens "right now" (same second as the window end): still 100%, not n/a
    now = datetime.now(timezone.utc)
    store.record([_res("http://ref", True, now.isoformat(timespec="seconds")),
                  _res("http://new", True, now.isoformat(timespec="seconds"))], cycle="c1")
    rep = build_availability_report(store, "last-24h", now=now)
    x = rep["sites"][0]
    assert x["measures"] == 1 and x["time_without_incidents_pct"] == 100.0 and x["incidents"] == 0
    assert rep["internet"]["time_without_incidents_pct"] == 100.0
    # ... and 0% when that very first check failed
    store.add_site("http://bad")
    store.record(_res("http://bad", False, now.isoformat(timespec="seconds")), cycle="c1")
    rep = build_availability_report(store, "last-24h", now=now)
    bad = [x for x in rep["sites"] if x["url"] == "http://bad"][0]
    assert bad["time_without_incidents_pct"] == 0.0 and bad["incidents"] == 1


def test_availability_window_end_in_future_is_clipped_to_now(store):
    _seed_timeline(store, [True, False])  # down since 12:10
    now = T0 + timedelta(minutes=20)
    rep = build_availability_report(store, since=_at(0), until=_at(120), now=now)
    x = rep["sites"][0]
    assert x["observed_seconds"] == 1200 and x["downtime_seconds"] == 600 and x["time_without_incidents_pct"] == 50.0


def test_build_details_website_and_internet(store):
    _seed_timeline(store, [True, True, False, False, True, True, True],
                   ref_states=[True, True, True, False, True, True, True])
    d = build_details(store, "http://site", since=_at(0), until=_at(60))
    assert d["label"] == "http://site" and d["total"] == 7 and not d["truncated"]
    assert [r["result"] for r in d["rows"]] == ["OK", "OK", "OK", "INCONCLUSIVE", "FAIL", "OK", "OK"]  # newest first
    assert d["rows"][0]["checked_at"] == _at(60) and d["rows"][3]["internet"] == "DOWN"
    assert d["rows"][4]["status"] == 500 and d["rows"][4]["error"] == "HTTPError"

    d = build_details(store, "internet", since=_at(0), until=_at(60))
    assert d["label"] == "Internet" and d["total"] == 7
    assert [r["result"] for r in d["rows"]] == ["UP", "UP", "UP", "DOWN", "UP", "UP", "UP"]
    assert d["rows"][3]["reachable"] == 0 and d["rows"][3]["total"] == 1 and d["rows"][3]["sites"][0]["url"] == "http://ref"

    d = build_details(store, "http://site", since=_at(0), until=_at(60), limit=3)
    assert d["total"] == 7 and len(d["rows"]) == 3 and d["truncated"]
    d = build_details(store, "http://site", since=_at(0), until=_at(60), limit=3, offset=3)
    assert [r["checked_at"] for r in d["rows"]] == [_at(30), _at(20), _at(10)] and d["truncated"]
    d = build_details(store, "http://site", since=_at(0), until=_at(60), limit=3, offset=6)
    assert [r["checked_at"] for r in d["rows"]] == [_at(0)] and not d["truncated"]
    assert "(showing checks 7-7 of 7" in format_details_text(d)
    assert build_details(store, "http://site", since=_at(100), until=_at(200))["rows"] == []
    with pytest.raises(ValueError):
        build_details(store, "http://not-monitored", "all")

    text = format_details_text(build_details(store, "http://site", since=_at(0), until=_at(60)))
    assert "7 check(s)" in text and "INCONCLUSIVE" in text and "FAIL" in text
    text = format_details_text(build_details(store, "internet", since=_at(0), until=_at(60)))
    assert "Internet" in text and "DOWN" in text and "0/1" in text


# --- Report server -----------------------------------------------------------

def _post(url, data, headers=None):
    req = Request(url, data=urlencode(data).encode(), method="POST", headers=headers or {})
    try:
        resp = urlopen(req)
    except HTTPError as e:
        resp = e
    return resp.getcode(), resp.read().decode()


class _NoRedirect(object):
    """urlopen follows 303s; use a raw opener to inspect the redirect."""

    @staticmethod
    def post(url, data):
        import urllib.request

        class H(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(H)
        req = Request(url, data=urlencode(data).encode(), method="POST")
        try:
            resp = opener.open(req)
        except HTTPError as e:
            resp = e
        return resp.getcode(), resp.headers.get("Location", "")


def test_report_server_endpoints(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    am = ActiveMonitor([target.url], store, interval=0.05)
    am.run(max_cycles=2)
    srv = ReportServer(store, port=0, active_monitor=am)
    srv.start()
    try:
        html = urlopen(srv.url).read().decode()
        # one page, several sections
        for section in ("id=\"internet\"", "id=\"sites\"", "id=\"incidents\"", "id=\"recent\"", "id=\"add-site\""):
            assert section in html
        assert "Internet is <b>UP</b>" in html
        assert reference.url in html and target.url in html
        assert "Add website" in html and "Remove" in html
        assert "href=\"/api/report\"" not in html  # no raw-JSON links in the UI

        partial = urlopen(srv.url + "?partial=1").read().decode()
        assert "<html" not in partial and "id=\"internet\"" in partial and target.url in partial

        report = json.loads(urlopen(srv.url + "api/report").read())
        assert report["summary"]["checks"] == 2 and report["monitor_running"] is False
        assert report["internet"]["state"] == "UP"

        sites = json.loads(urlopen(srv.url + "api/sites").read())["sites"]
        assert [s["kind"] for s in sites] == [KIND_REFERENCE, KIND_USER]

        results = json.loads(urlopen(srv.url + "api/results?limit=1&url=" + target.url).read())["results"]
        assert len(results) == 1 and results[0]["url"] == target.url

        health = json.loads(urlopen(srv.url + "health").read())
        assert health["status"] == "ok"

        with pytest.raises(Exception) as exc:
            urlopen(srv.url + "nope")
        assert "404" in str(exc.value)
    finally:
        srv.shutdown()


def test_dashboard_add_and_remove_sites(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        # add via the dashboard form -> redirect back to the page with a message
        code, location = _NoRedirect.post(srv.url + "sites", {"action": "add", "url": target.url})
        assert code == 303 and location.startswith("/?msg=Added")
        assert store.site_urls(KIND_USER) == [target.url]
        page = urlopen(srv.url + location).read().decode()
        assert "Added website " + target.url in page
        assert "PENDING" in page and target.url in page  # listed before its first check

        # invalid URL -> error message, nothing added
        code, location = _NoRedirect.post(srv.url + "sites", {"action": "add", "url": "not-a-url"})
        assert code == 303 and "ok=0" in location
        assert store.site_urls(KIND_USER) == [target.url]

        # duplicates are reported, not added twice
        code, body = _post(srv.url + "api/sites", {"action": "add", "url": target.url})
        assert code == 200 and "already monitored" in json.loads(body)["message"]

        # reference sites cannot be removed from the UI
        code, body = _post(srv.url + "api/sites", {"action": "remove", "url": reference.url})
        assert code == 400 and store.site_urls(KIND_REFERENCE) == [reference.url]

        # remove user site (JSON body variant)
        req = Request(srv.url + "api/sites", data=json.dumps({"action": "remove", "url": target.url}).encode(),
                      method="POST", headers={"Content-Type": "application/json"})
        body = json.loads(urlopen(req).read())
        assert body["ok"] is True and [s["url"] for s in body["sites"]] == [reference.url]
        assert store.site_urls(KIND_USER) == []

        code, _ = _post(srv.url + "nope", {"url": "x"})
        assert code == 404
    finally:
        srv.shutdown()


def test_dashboard_reports_internet_down(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    am = ActiveMonitor([target.url], store, interval=0.05)
    am.run_once()
    reference.healthy = False
    target.healthy = False
    am.run_once()
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        html = urlopen(srv.url).read().decode()
        assert "Internet is <b>DOWN</b>" in html
        assert "UNKNOWN" in html  # user site cannot be judged while internet is down
        report = json.loads(urlopen(srv.url + "api/report").read())
        assert report["sites"][0]["inconclusive"] == 1 and report["summary"]["sites_down"] == 0
    finally:
        srv.shutdown()


def test_report_server_with_live_monitor(target, store):
    am = ActiveMonitor([target.url], store, interval=0.05)
    am.start()
    srv = ReportServer(store, port=0, active_monitor=am)
    srv.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            report = json.loads(urlopen(srv.url + "api/report").read())
            if report["summary"]["checks"] >= 2:
                break
            time.sleep(0.02)
        assert report["monitor_running"] is True
        assert report["summary"]["sites_up"] == 1
    finally:
        srv.shutdown()  # also stops the monitor
    assert not am.running


def test_dashboard_reference_add_edit_remove(reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "+ Add reference site" in page
        assert "Only 1 reference site(s) configured" in page  # warning below the minimum
        assert page.count("<summary>Edit</summary>") == 1 and "Remove" in page

        # cannot remove the last reference site
        code, body = _post(srv.url + "api/sites", {"action": "remove", "url": reference.url})
        assert code == 400 and "last reference site" in json.loads(body)["message"]
        assert store.site_urls(KIND_REFERENCE) == [reference.url]

        # add two more reference sites -> warning disappears
        for u in ("http://r2.example", "http://r3.example"):
            code, body = _post(srv.url + "api/sites", {"action": "add", "kind": "reference", "url": u})
            assert code == 200 and "Added reference site" in json.loads(body)["message"]
        assert len(store.site_urls(KIND_REFERENCE)) == 3
        page = urlopen(srv.url).read().decode()
        assert "reference site(s) configured" not in page
        assert "PENDING" in page and "r2.example" in page

        # edit (replace) a reference site that went away permanently
        code, location = _NoRedirect.post(srv.url + "sites", {"action": "replace", "url": "http://r2.example",
                                                               "new_url": "http://r2-new.example"})
        assert code == 303 and "Replaced" in location
        # the replacement keeps the position of the site it substitutes
        assert store.site_urls(KIND_REFERENCE) == [reference.url, "http://r2-new.example", "http://r3.example"]
        code, body = _post(srv.url + "api/sites", {"action": "replace", "url": "http://r3.example", "new_url": "bad"})
        assert code == 400

        # now removal is allowed
        code, body = _post(srv.url + "api/sites", {"action": "remove", "url": "http://r3.example"})
        assert code == 200 and len(store.site_urls(KIND_REFERENCE)) == 2
    finally:
        srv.shutdown()


def test_dashboard_check_now(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    store.add_site(target.url)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "Check all now" in page and "Check reference sites now" in page
        assert page.count("title=\"Check now\"") >= 2  # one icon button per row

        # check all
        code, body = _post(srv.url + "api/check", {})
        d = json.loads(body)
        assert code == 200 and d["ok"] and d["internet_up"] is True
        assert [r["url"] for r in d["results"]] == [reference.url, target.url]
        assert d["message"].startswith("Checked 2 site(s)") and "2 up, 0 down" in d["message"]
        assert len(store.results()) == 2

        # single site (form submit -> redirect with message); reference checked too
        target.healthy = False
        code, location = _NoRedirect.post(srv.url + "check", {"url": target.url})
        assert code == 303 and "1+up%2C+1+down" in location
        assert len(store.results(url=reference.url)) == 2 and len(store.results(url=target.url)) == 2
        assert json.loads(urlopen(srv.url + "api/report").read())["sites"][0]["current_state"] == "DOWN"

        # reference sites only
        code, body = _post(srv.url + "api/check", {"kind": "reference"})
        assert [r["url"] for r in json.loads(body)["results"]] == [reference.url]

        # internet down -> explained in the message, site failure inconclusive
        reference.healthy = False
        code, body = _post(srv.url + "api/check", {"url": target.url})
        d = json.loads(body)
        assert d["internet_up"] is False and "inconclusive" in d["message"]
        assert json.loads(urlopen(srv.url + "api/report").read())["sites"][0]["current_state"] == "UNKNOWN"

        code, body = _post(srv.url + "api/check", {"url": "http://unknown.example"})
        assert code == 400

        # the browser script sends multipart FormData
        import urllib.request
        boundary = "XyZ123"
        mp = ("--{b}\r\nContent-Disposition: form-data; name=\"url\"\r\n\r\n{u}\r\n--{b}--\r\n"
              .format(b=boundary, u=target.url)).encode()
        req = Request(srv.url + "api/check", data=mp, method="POST",
                      headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        d = json.loads(urlopen(req).read())
        assert d["ok"] and [r["url"] for r in d["results"]] == [reference.url, target.url]
    finally:
        srv.shutdown()


def test_dashboard_reports_section_and_api(store):
    _seed_timeline(store, [True, True, False, False, True, True, True])
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "id=\"reports\"" in page
        for label in ("Last hour", "Today", "Yesterday", "Last 24 hours", "Last 7 days", "Last 30 days", "All time"):
            assert label in page
        assert "chip active\" href=\"/?period=last-24h\"" in page  # default preset highlighted
        assert "type=\"datetime-local\" name=\"since\"" in page

        # custom interval via the form (GET) -> both measures rendered
        page = urlopen(srv.url + "?since=" + _at(0)[:16] + "&until=" + _at(60)[:16]).read().decode()
        assert "<span class=\"chip active\">Custom</span>" in page
        assert "value=\"2024-03-10T12:00\"" in page and "value=\"2024-03-10T13:00\"" in page
        assert "66.67%" in page and "66.667" not in page and "20m 0s" in page
        assert "<div class=\"value\">1 / 5</div>" in page  # incidents / measures without incidents
        assert "Internet (1 reference sites)" in page and "Internet without outages" in page
        assert page.count(">Details</a>") == 3  # internet + its one reference site + one website
        assert "\u21b3 reference: http://ref" in page
        assert "100.000%" not in page

        # preset via chip keeps working in the partial refresh
        partial = urlopen(srv.url + "?partial=1&period=all").read().decode()
        assert "chip active\" href=\"/?period=all\"" in partial and "http://site" in partial

        # invalid custom window -> error shown, page still renders
        page = urlopen(srv.url + "?since=garbage").read().decode()
        assert "invalid timestamp" in page and "id=\"sites\"" in page

        # JSON API
        d = json.loads(urlopen(srv.url + "api/reports?" + urlencode({"since": _at(0), "until": _at(60)})).read())
        assert d["summary"]["time_without_incidents_pct"] == 66.67 and d["summary"]["incidents"] == 1
        assert d["internet"]["measures"] == 7 and d["internet"]["time_without_incidents_pct"] == 100.0
        # an un-encoded "+00:00" (decoded as a space) is tolerated
        d = json.loads(urlopen(srv.url + "api/reports?since=" + _at(0) + "&until=" + _at(60)).read())
        assert d["summary"]["incidents"] == 1
        d = json.loads(urlopen(srv.url + "api/reports?period=all").read())
        assert d["window"]["period"] == "all" and d["sites"][0]["url"] == "http://site"
        d = json.loads(urlopen(srv.url + "api/reports?period=all&url=http://nothing").read())
        assert d["sites"] == []
        with pytest.raises(HTTPError) as exc:
            urlopen(srv.url + "api/reports?period=bogus")
        assert exc.value.code == 400
    finally:
        srv.shutdown()


def test_dashboard_detailed_report(store):
    _seed_timeline(store, [True, True, False, False, True, True, True],
                   ref_states=[True, True, True, False, True, True, True])
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        q = urlencode({"since": _at(0)[:16], "until": _at(60)[:16]})
        page = urlopen(srv.url + "?" + q + "&detail=http://site").read().decode()
        assert "Detailed report: http://site" in page and "7 check(s)" in page
        block = page[page.index("id=\"detail\""):page.index("id=\"incidents\"")]
        assert block.count("<td class=\"INCONCLUSIVE\">INCONCLUSIVE</td>") == 1
        assert block.count("<td class=\"DOWN\">FAIL</td>") == 1
        assert block.count("<td class=\"UP\">OK</td>") == 5  # 7 rows fit on one page of 25
        assert "page 1 of 1 &middot; checks 1-7 of 7" in block
        assert "Close details</a>" in page and "Download CSV (7 checks)" in page
        assert "id=\"sites\"" in page  # still the same single page

        page = urlopen(srv.url + "?period=all&detail=internet").read().decode()
        assert "Detailed report: Internet" in page and "Reference site reliability in this window" in page
        assert "<td class=\"DOWN\">DOWN</td>" in page and "0/1" in page
        assert "<th>ref</th>" in page  # one column per reference site

        # the in-place refresh keeps the detail open (same query string)
        partial = urlopen(srv.url + "?partial=1&period=all&detail=internet").read().decode()
        assert "Detailed report: Internet" in partial

        # JSON + CSV
        d = json.loads(urlopen(srv.url + "api/details?period=all&category=http://site").read())
        assert d["total"] == 7 and d["rows"][0]["result"] == "OK"
        d = json.loads(urlopen(srv.url + "api/details?period=all&category=http://site&limit=2&offset=2").read())
        assert [r["checked_at"] for r in d["rows"]] == [_at(40), _at(30)] and d["truncated"]
        resp = urlopen(srv.url + "details.csv?period=all&category=http://site")
        assert resp.headers["Content-Type"].startswith("text/csv")
        assert "imon-site-all.csv" in resp.headers["Content-Disposition"]
        lines = resp.read().decode().strip().splitlines()
        assert lines[0] == "checked_at,url,result,http_status,latency_ms,error,internet" and len(lines) == 8
        assert "INCONCLUSIVE" in "\n".join(lines)
        csv_inet = urlopen(srv.url + "details.csv?period=all&category=internet").read().decode()
        assert csv_inet.splitlines()[0] == "checked_at,internet,reference_reachable,reference_total,http://ref result,http://ref latency_ms"
        assert "DOWN" in csv_inet

        for bad in ("api/details?period=all", "api/details?period=all&category=http://nope", "details.csv?category=x"):
            with pytest.raises(HTTPError) as exc:
                urlopen(srv.url + bad)
            assert exc.value.code == 400
        # unknown detail on the dashboard -> error message, page still renders
        page = urlopen(srv.url + "?period=all&detail=http://nope").read().decode()
        assert "not monitored" in page and "id=\"reports\"" in page
    finally:
        srv.shutdown()


def test_dashboard_detail_pagination(store):
    store.add_site("http://ref", KIND_REFERENCE)
    store.add_site("http://site")
    for i in range(60):  # 60 checks, one fails
        store.record([_res("http://ref", True, _at(i)), _res("http://site", i != 7, _at(i))], cycle="c{}".format(i))
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        def detail_block(q):
            page = urlopen(srv.url + "?" + q).read().decode()
            return page[page.index("id=\"detail\""):page.index("id=\"incidents\"")]

        # default: 25 per page, newest first
        b = detail_block("period=all&detail=http://site")
        assert b.count("<td class=\"UP\">OK</td>") == 25 and "page 1 of 3 &middot; checks 1-25 of 60" in b
        assert _at(59) in b and _at(35) in b and _at(34) not in b
        assert "class=\"btn disabled\">&laquo; First</a>" in b and "Next &rsaquo;</a>" in b
        assert "class=\"chip active\" href=\"/?period=all&amp;detail=http%3A%2F%2Fsite&amp;per=25&amp;page=1#detail\">25</a>" in b
        assert b.count("class=\"pager\"") == 2  # above and below the table

        b = detail_block("period=all&detail=http://site&page=3")
        assert "page 3 of 3 &middot; checks 51-60 of 60" in b and _at(9) in b and _at(0) in b
        assert "class=\"btn disabled\">Next &rsaquo;</a>" in b
        assert "<td class=\"DOWN\">FAIL</td>" in b  # the failure at minute 7 is on the last page

        b = detail_block("period=all&detail=http://site&per=50&page=2")
        assert "page 2 of 2 &middot; checks 51-60 of 60" in b
        b = detail_block("period=all&detail=http://site&per=100")
        assert "page 1 of 1 &middot; checks 1-60 of 60" in b and b.count("<td>" + "2024-03-10") == 60
        # invalid sizes / pages fall back safely
        b = detail_block("period=all&detail=http://site&per=7&page=abc")
        assert "page 1 of 3" in b
        b = detail_block("period=all&detail=http://site&page=99")  # past the end -> last page
        assert "page 3 of 3 &middot; checks 51-60 of 60" in b

        # internet category pages by cycle
        b = detail_block("period=all&detail=internet&per=50&page=2")
        assert "page 2 of 2 &middot; checks 51-60 of 60" in b and b.count("<td class=\"UP\">UP</td>") == 10

        # the CSV still has everything
        assert len(urlopen(srv.url + "details.csv?period=all&category=http://site").read().decode().splitlines()) == 61
    finally:
        srv.shutdown()


def _seed_three_references(store):
    """Three reference sites; r2 is flaky (fails in cycles 1 and 3), r3 dies in cycle 4."""
    for u in ("http://r1", "http://r2", "http://r3"):
        store.add_site(u, KIND_REFERENCE)
    r1 = [True, True, True, True, True]
    r2 = [True, False, True, False, True]
    r3 = [True, True, True, True, False]
    for i in range(5):
        store.record([_res("http://r1", r1[i], _at(i * 10)), _res("http://r2", r2[i], _at(i * 10), latency=50),
                      _res("http://r3", r3[i], _at(i * 10))], cycle="c{}".format(i))


def test_internet_details_show_every_reference_site(store):
    _seed_three_references(store)
    d = build_details(store, "internet", since=_at(0), until=_at(60))
    assert d["reference_urls"] == ["http://r1", "http://r2", "http://r3"]
    assert d["total"] == 5 and all(r["result"] == "UP" for r in d["rows"])
    # every row carries the result of every reference site
    assert all([x["url"] for x in r["sites"]] == ["http://r1", "http://r2", "http://r3"] for r in d["rows"])
    newest = d["rows"][0]
    assert newest["checked_at"] == _at(40) and newest["reachable"] == 2 and newest["total"] == 3
    assert [x["available"] for x in newest["sites"]] == [True, True, False]
    # reliability summary per reference site makes the flaky one obvious
    summary = {x["url"]: x for x in d["reference_summary"]}
    assert summary["http://r1"]["uptime_pct"] == 100.0 and summary["http://r1"]["failed"] == 0
    assert summary["http://r2"]["uptime_pct"] == 60.0 and summary["http://r2"]["failed"] == 2
    assert summary["http://r2"]["avg_latency_ms"] == 50.0
    assert summary["http://r3"]["uptime_pct"] == 80.0 and summary["http://r3"]["last_checked_at"] == _at(40)

    text = format_details_text(d)
    assert "Reference site reliability" in text and "http://r2" in text and "60.00%" in text
    assert "FAIL HTTPError" in text and text.count("OK 50 ms") == 3

    # a reference site that was not part of a cycle (added later) shows as "not checked"
    store.add_site("http://r4", KIND_REFERENCE)
    d = build_details(store, "internet", since=_at(0), until=_at(60))
    assert d["rows"][0]["sites"][3]["available"] is None and d["rows"][0]["total"] == 3


def test_details_limit_applied_in_database(store):
    _seed_three_references(store)
    store.add_site("http://site")
    for i in range(5):
        store.record(_res("http://site", i != 2, _at(i * 10)), cycle="c{}".format(i))
    d = build_details(store, "internet", since=_at(0), until=_at(60), limit=2)
    assert d["total"] == 5 and len(d["rows"]) == 2 and d["truncated"]
    assert [r["checked_at"] for r in d["rows"]] == [_at(40), _at(30)]
    assert all(r["total"] == 3 for r in d["rows"])  # complete cycles even at the cut
    d = build_details(store, "http://site", since=_at(0), until=_at(60), limit=3)
    assert d["total"] == 5 and [r["checked_at"] for r in d["rows"]] == [_at(40), _at(30), _at(20)]
    assert d["rows"][2]["result"] == "FAIL" and d["rows"][2]["internet"] == "UP"
    # reference site details: plain OK/FAIL, never inconclusive
    d = build_details(store, "http://r2", since=_at(0), until=_at(60))
    assert d["kind"] == KIND_REFERENCE and [r["result"] for r in d["rows"]] == ["OK", "FAIL", "OK", "FAIL", "OK"]


def test_store_count_and_aggregate(store):
    _seed_three_references(store)
    assert store.count() == 15 and store.count(url="http://r2") == 5
    assert store.count(urls=["http://r1", "http://r3"], since=_at(10)) == 8
    assert store.count_cycles() == 5
    assert store.count_cycles(since=_at(30)) == 2
    assert store.count_cycles(until=_at(10)) == 2
    # cycles without any reference site (e.g. no reference configured) are not internet measures
    store.add_site("http://user")
    store.record(_res("http://user", True, _at(90)), cycle="user-only")
    assert store.count_cycles() == 5 and store.cycle_before(_at(95))["checked_at"] == _at(40)
    assert store.cycle_before(_at(0)) is None
    agg = store.aggregate(["http://r2"])
    assert agg["http://r2"]["checks"] == 5 and agg["http://r2"]["ok"] == 3 and agg["http://r2"]["max_latency_ms"] == 50.0
    assert store.aggregate(["http://nothing"]) == {}


def test_availability_report_lists_reference_sites(store):
    _seed_three_references(store)
    rep = build_availability_report(store, since=_at(0), until=_at(50), now=T0 + timedelta(minutes=50))
    ref = {x["url"]: x for x in rep["reference"]}
    assert set(ref) == {"http://r1", "http://r2", "http://r3"}
    assert all(x["category"] == "reference" for x in ref.values())
    assert ref["http://r1"]["time_without_incidents_pct"] == 100.0 and ref["http://r1"]["incidents"] == 0
    assert ref["http://r2"]["incidents"] == 2 and ref["http://r2"]["downtime_seconds"] == 1200
    assert ref["http://r3"]["incidents"] == 1 and ref["http://r3"]["time_without_incidents_pct"] == 80.0
    assert rep["internet"]["incidents"] == 0  # at least one reference was always up
    text = format_availability_text(rep)
    assert "  ref: http://r2" in text and "60.00%" in text


def test_dashboard_internet_detail_columns(store):
    _seed_three_references(store)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url + "?period=all&detail=internet").read().decode()
        block = page[page.index("id=\"detail\""):page.index("id=\"incidents\"")]
        assert "<th>r1</th><th>r2</th><th>r3</th>" in block
        assert block.count("<td class=\"DOWN\">FAIL <span class=\"muted\">HTTPError</span></td>") == 3
        assert block.count("OK 50 ms") == 3
        assert "<td>http://r2</td><td class=\"DOWN\">60.00%</td><td>5</td><td>3</td><td>2</td>" in block
        csv_text = urlopen(srv.url + "details.csv?period=all&category=internet").read().decode()
        header = csv_text.splitlines()[0].split(",")
        assert header[4:7] == ["http://r1 result", "http://r2 result", "http://r3 result"]
        assert csv_text.splitlines()[1].split(",")[4:7] == ["OK", "OK", "HTTPError"]
    finally:
        srv.shutdown()


def test_store_cycle_states(store):
    _seed_three_references(store)
    refs = ["http://r1", "http://r2", "http://r3"]
    states = store.cycle_states(refs)
    assert [(c["checked_at"], c["available"], c["reachable"], c["total"]) for c in states] == [
        (_at(0), True, 3, 3), (_at(10), True, 2, 3), (_at(20), True, 3, 3), (_at(30), True, 2, 3), (_at(40), True, 2, 3)]
    assert [c["checked_at"] for c in store.cycle_states(refs, limit=2)] == [_at(30), _at(40)]
    assert [c["checked_at"] for c in store.cycle_states(refs, since=_at(10), until=_at(20))] == [_at(10), _at(20)]
    assert [c["checked_at"] for c in store.cycle_states(refs, limit=2, offset=3)] == [_at(0), _at(10)]
    # a cycle where nothing answered
    store.record([_res("http://r1", False, _at(50)), _res("http://r2", False, _at(50))], cycle="c5")
    assert store.cycle_states(refs)[-1]["available"] is False
    assert store.series("http://r2", since=_at(30)) == [
        {"checked_at": _at(30), "available": False, "cycle": "c3"},
        {"checked_at": _at(40), "available": True, "cycle": "c4"},
        {"checked_at": _at(50), "available": False, "cycle": "c5"}]


def test_dashboard_status_window_and_cache(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    store.add_site(target.url)
    # old history (2024) plus a fresh check
    store.record([_res(reference.url, True, _ts(0)), _res(target.url, False, _ts(0))], cycle="old")
    check_now(store)
    srv = ReportServer(store, port=0)  # default status window: all time
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "Your websites <span class=\"muted\">(All time)</span>" in page
        assert "Internet connection <span class=\"muted\">(All time)</span>" in page
        assert "id=\"status-filter\"" in page and "name=\"ssince\"" in page
        d = json.loads(urlopen(srv.url + "api/report").read())
        assert d["sites"][0]["checks"] == 2  # all time

        # same presets as the reports, independent of them
        page = urlopen(srv.url + "?speriod=last-24h&period=all").read().decode()
        assert "(Last 24 hours)</span>" in page
        assert "chip active\" href=\"/?period=all&amp;speriod=last-24h\">Last 24 hours</a>" in page
        assert "chip active\" href=\"/?speriod=last-24h&amp;period=all\">All time</a>" in page
        d = json.loads(urlopen(srv.url + "api/report?speriod=last-24h").read())
        assert d["sites"][0]["checks"] == 1  # the 2024 check is outside
        d = json.loads(urlopen(srv.url + "api/report?since=2024-01-01&until=2024-01-02").read())
        assert d["sites"][0]["checks"] == 1 and d["sites"][0]["current_state"] == "DOWN"

        # custom status window through the form (naive = UTC), report filter kept as hidden fields
        page = urlopen(srv.url + "?ssince=2024-01-01T00:00&suntil=2024-01-02T00:00&period=last-7d").read().decode()
        assert "(2024-01-01T00:00:00+00:00 to 2024-01-02T00:00:00+00:00)</span>" in page
        assert "value=\"2024-01-01T00:00\"" in page
        assert "<input type=\"hidden\" name=\"period\" value=\"last-7d\">" in page
        assert "Checks: <b>1</b>" in page
        # invalid custom window: error shown, falls back to default window, page still renders
        page = urlopen(srv.url + "?ssince=garbage").read().decode()
        assert "invalid timestamp" in page and "(All time)</span>" in page

        # cache: a result recorded directly in the store is seen (key includes the count) ...
        first = urlopen(srv.url + "?partial=1").read().decode()
        assert urlopen(srv.url + "?partial=1").read().decode() == first
        check_now(store)
        assert "Checks: <b>3</b>" in urlopen(srv.url + "?partial=1").read().decode()
        # ... and any write through the server clears it
        _post(srv.url + "api/sites", {"action": "add", "url": "http://new.example"})
        assert "http://new.example" in urlopen(srv.url + "?partial=1").read().decode()
    finally:
        srv.shutdown()

    srv = ReportServer(store, port=0, status_period="last-hour")
    srv.start()
    try:
        assert "(Last hour)</span>" in urlopen(srv.url).read().decode()
    finally:
        srv.shutdown()


def test_dashboard_links_keep_both_filters_and_detail(store):
    _seed_timeline(store, [True, True, False])
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        q = "speriod=last-7d&period=all&detail=http://site&per=50&page=1"
        page = urlopen(srv.url + "?" + q).read().decode()
        # report chips keep the status window and the open detail (back on page 1)
        assert "href=\"/?speriod=last-7d&amp;detail=http%3A%2F%2Fsite&amp;per=50&amp;period=last-hour\">Last hour</a>" in page
        # status chips keep the report window and the open detail
        assert "href=\"/?period=all&amp;detail=http%3A%2F%2Fsite&amp;per=50&amp;speriod=today\">Today</a>" in page
        # close keeps both windows
        assert "href=\"/?speriod=last-7d&amp;period=all#reports\">Close details</a>" in page
        # actions redirect back with everything preserved
        import urllib.request

        class H(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        req = Request(srv.url + "sites", data=urlencode({"action": "add", "url": "http://x.example"}).encode(),
                      method="POST", headers={"Referer": srv.url + "?" + q})
        try:
            resp = urllib.request.build_opener(H).open(req)
        except HTTPError as e:
            resp = e
        loc = resp.headers["Location"]
        assert "speriod=last-7d" in loc and "period=all" in loc and "detail=http%3A%2F%2Fsite" in loc and "per=50" in loc
    finally:
        srv.shutdown()


def test_ttl_cache():
    from imon.server import _TTLCache
    c = _TTLCache(ttl=60)
    calls = []
    assert c.get(("a",), lambda: calls.append(1) or "x") == "x"
    assert c.get(("a",), lambda: calls.append(1) or "y") == "x" and len(calls) == 1
    c.clear()
    assert c.get(("a",), lambda: calls.append(1) or "y") == "y" and len(calls) == 2
    c = _TTLCache(ttl=0)
    c.get(("b",), lambda: 1)
    assert c.get(("b",), lambda: 2) == 2  # expired immediately


def test_dashboard_script_has_long_running_safeguards(store):
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        for snippet in ("if (inflight) return", "document.hidden", "AbortController", "t !== lastHtml",
                        "location.reload()", "visibilitychange", "failures += 1"):
            assert snippet in page, snippet
    finally:
        srv.shutdown()


def test_details_result_filter(store):
    # site: 7 checks -> 4 OK, 1 FAIL (internet up), 2 INCONCLUSIVE (internet down in cycles 2,3)
    _seed_timeline(store, [True, True, False, False, True, False, True],
                   ref_states=[True, True, False, False, True, True, True])
    w = dict(since=_at(0), until=_at(60))
    for result, expected in (("ok", 4), ("fail", 1), ("inconclusive", 2), (None, 7)):
        d = build_details(store, "http://site", result=result, **w)
        assert d["total"] == expected and len(d["rows"]) == expected and d["result"] == result
        assert all(r["result"] == (result or r["result"]).upper() for r in d["rows"])
    assert build_details(store, "http://site", result="FAIL", **w)["total"] == 1  # case-insensitive
    assert build_details(store, "http://site", result="fail", **w)["rows"][0]["checked_at"] == _at(50)
    # filter + paging work together
    d = build_details(store, "http://site", result="ok", limit=3, offset=3, **w)
    assert [r["checked_at"] for r in d["rows"]] == [_at(0)] and d["total"] == 4
    # reference site: failures are never inconclusive
    assert build_details(store, "http://ref", result="fail", **w)["total"] == 2
    assert build_details(store, "http://ref", result="inconclusive", **w)["total"] == 0
    # internet: up / down
    assert build_details(store, "internet", result="up", **w)["total"] == 5
    d = build_details(store, "internet", result="down", **w)
    assert d["total"] == 2 and [r["checked_at"] for r in d["rows"]] == [_at(30), _at(20)]
    for bad in (("http://site", "up"), ("internet", "ok"), ("http://site", "bogus")):
        with pytest.raises(ValueError):
            build_details(store, bad[0], result=bad[1], **w)
    assert "with result FAIL" in format_details_text(build_details(store, "http://site", result="fail", **w))


def test_dashboard_detail_result_filter_and_recent_paging(store):
    _seed_timeline(store, [True, True, False, False, True, False, True],
                   ref_states=[True, True, False, False, True, True, True])
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        def block(q, start="id=\"detail\"", end="id=\"incidents\""):
            page = urlopen(srv.url + "?" + q).read().decode()
            return page[page.index(start):page.index(end)]

        b = block("period=all&detail=http://site")
        assert "<span class=\"muted\">Show:</span>" in b
        for label in ("All", "OK", "FAIL", "INCONCLUSIVE"):
            assert ">{}</a>".format(label) in b
        assert "chip active\" href=\"/?period=all&amp;detail=http%3A%2F%2Fsite#detail\">All</a>" in b

        b = block("period=all&detail=http://site&result=fail&page=3")
        assert "(1 FAIL check(s)," in b and b.count("<td class=\"DOWN\">FAIL</td>") == 1
        assert "<td class=\"UP\">OK</td>" not in b and "page 1 of 1" in b  # page reset past the end
        assert "chip active\" href=\"/?period=all&amp;detail=http%3A%2F%2Fsite&amp;result=fail#detail\">FAIL</a>" in b
        assert "Download CSV (1 checks)" in b and "details.csv?period=all&amp;category=http%3A%2F%2Fsite&amp;result=fail" in b
        b = block("period=all&detail=http://site&result=inconclusive")
        assert b.count("<td class=\"INCONCLUSIVE\">INCONCLUSIVE</td>") == 2
        b = block("period=all&detail=internet&result=down")
        assert b.count("<td class=\"DOWN\">DOWN</td>") == 2 and ">UP</a>" in b and ">INCONCLUSIVE</a>" not in b
        b = block("period=all&detail=http://ref")
        assert ">INCONCLUSIVE</a>" not in b  # reference sites have no inconclusive filter
        assert "result must be one of ok, fail, inconclusive" in urlopen(srv.url + "?period=all&detail=http://site&result=bogus").read().decode()

        csv_text = urlopen(srv.url + "details.csv?period=all&category=http://site&result=fail").read().decode()
        assert len(csv_text.strip().splitlines()) == 2 and ",FAIL," in csv_text
        d = json.loads(urlopen(srv.url + "api/details?period=all&category=internet&result=down").read())
        assert d["total"] == 2 and d["result"] == "down"

        # recent checks: paginated with its own keys (14 results in total)
        r = block("", "id=\"recent\"", "</main>")
        assert "page 1 of 1 &middot; checks 1-14 of 14" in r
        r = block("rper=50", "id=\"recent\"", "</main>")
        assert "chip active\" href=\"/?rper=50&amp;rpage=1#recent\">50</a>" in r
    finally:
        srv.shutdown()

    for i in range(40):
        store.record(_res("http://site", True, _at(100 + i)), cycle="x{}".format(i))
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url + "?rpage=2&period=all&detail=http://site&page=1").read().decode()
        r = page[page.index("id=\"recent\""):page.index("</main>")]
        assert "page 2 of 3 &middot; checks 26-50 of 54" in r and r.count("<td class=\"time\">") == 25
        assert "href=\"/?period=all&amp;detail=http%3A%2F%2Fsite&amp;page=1&amp;rpage=3#recent\">Next &rsaquo;</a>" in r
        # the newest check is on page 1, the oldest on the last page
        first = urlopen(srv.url + "?rpage=1").read().decode()
        assert _at(139) in first[first.index("id=\"recent\""):]
        last = urlopen(srv.url + "?rpage=3").read().decode()
        assert _at(0) in last[last.index("id=\"recent\""):] and "page 3 of 3 &middot; checks 51-54 of 54" in last
        # detail paging and recent paging are independent
        page = urlopen(srv.url + "?period=all&detail=http://site&page=2&rpage=3&rper=25").read().decode()
        assert "page 2 of 2 &middot; checks 26-47 of 47" in page[page.index("id=\"detail\""):page.index("id=\"incidents\"")]
        assert "page 3 of 3" in page[page.index("id=\"recent\""):]
    finally:
        srv.shutdown()


def test_dashboard_sites_and_incidents_paging(store):
    store.add_site("http://ref", KIND_REFERENCE)
    store.record(_res("http://ref", True, _at(0)), cycle="c0")
    for i in range(30):  # 30 websites, each with one incident (fail then recover)
        u = "http://site{:02d}".format(i)
        store.add_site(u)
        store.record([_res(u, False, _at(1 + i)), _res(u, True, _at(40 + i))], cycle="c{}".format(i))
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        def section(q, sid, nxt):
            page = urlopen(srv.url + "?" + q).read().decode()
            return page[page.index("id=\"{}\"".format(sid)):page.index("id=\"{}\"".format(nxt))]

        sites = section("", "sites", "reports")
        assert "page 1 of 2 &middot; checks 1-25 of 30" in sites and sites.count("<td class=\"url\">") == 25
        assert "Sites: <b>30</b>" in sites  # the summary still covers every site
        sites = section("wpage=2", "sites", "reports")
        assert "checks 26-30 of 30" in sites and sites.count("<td class=\"url\">") == 5 and "http://site29" in sites
        sites = section("wper=50", "sites", "reports")
        assert "page 1 of 1 &middot; checks 1-30 of 30" in sites
        assert "href=\"/?wper=50&amp;wpage=1#sites\">50</a>" in sites

        inc = section("", "incidents", "recent")
        assert "page 1 of 2 &middot; checks 1-25 of 30" in inc and inc.count("resolved") == 25
        assert "http://site29" in inc and "http://site04" not in inc  # newest incidents first
        inc = section("ipage=2&wpage=2", "incidents", "recent")
        assert "checks 26-30 of 30" in inc and "http://site04" in inc
        assert "href=\"/?ipage=1&amp;wpage=2#incidents\">&laquo; First</a>" in inc  # keeps the other pager
    finally:
        srv.shutdown()

    # with few rows the pagers stay out of the way
    small = ResultStore(":memory:")
    _seed_timeline(small, [True, False, True])
    srv = ReportServer(small, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        sites = page[page.index("id=\"sites\""):page.index("id=\"reports\"")]
        inc = page[page.index("id=\"incidents\""):page.index("id=\"recent\"")]
        assert "class=\"pager\"" not in sites and "class=\"pager\"" not in inc
        assert "class=\"pager\"" in page[page.index("id=\"recent\""):]
    finally:
        srv.shutdown()
    small.close()


def _check_pdf(data):
    """Structural validation: header, xref offsets pointing at the right objects, trailer."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    startxref = int(data[data.rindex(b"startxref") + 9:].split()[0])
    assert data[startxref:startxref + 4] == b"xref"
    count = int(data[startxref:].split(b"\n")[1].split()[1])
    entries = data[startxref:].split(b"\n")[2:2 + count]
    for n, entry in enumerate(entries[1:], start=1):
        offset = int(entry.split()[0])
        assert data[offset:].startswith("{} 0 obj".format(n).encode()), n
    return data.count(b"/Type /Page ")


def test_pdf_rendering(store):
    from imon.pdf import render_availability_pdf, render_details_pdf, _clip, _pdf_text
    _seed_three_references(store)
    store.add_site("http://site")
    for i in range(120):
        store.record(_res("http://site", i % 17 != 3, _at(i)), cycle="c{}".format(i))

    pdf = render_details_pdf(build_details(store, "http://site", "all"))
    assert _check_pdf(pdf) == 4  # 120 rows -> several pages, header repeated
    assert b"(Time (UTC))" not in pdf and pdf.count(b"(Time \\(UTC\\))") == 4  # parentheses escaped, header on every page
    assert b"(Page 1 of 4)" in pdf and b"(Page 4 of 4)" in pdf
    assert b"(FAIL)" in pdf and b"(OK)" in pdf and b"/BaseFont /Courier" in pdf

    pdf = render_details_pdf(build_details(store, "internet", "all"))
    assert _check_pdf(pdf) == 1
    assert b"(Reference site reliability in this window)" in pdf and b"(r2)" in pdf and b"(FAIL HTTPError)" in pdf
    assert b"(60.00%)" in pdf

    pdf = render_details_pdf(build_details(store, "http://site", "all", result="fail"))
    assert b"(7 FAIL check\\(s\\), newest first." in pdf and b"(OK)" not in pdf
    pdf = render_details_pdf(build_details(store, "http://site", since="2020-01-01", until="2020-01-02"))
    assert b"No checks in this period." in pdf

    pdf = render_availability_pdf(build_availability_report(store, "all"))
    assert _check_pdf(pdf) == 1 and b"(Internet \\(3 reference sites\\))" in pdf and b"(  ref: http://r2)" in pdf

    # helpers: clipping and escaping
    assert _clip("abc", 100, 8) == "abc" and _clip("a" * 50, 50, 8).endswith("\u2026") and len(_clip("a" * 50, 50, 8)) < 50
    assert _pdf_text("a(b)c\\d\n") == b"a\\(b\\)c\\\\d "
    assert _pdf_text("caf\u00e9 \u4e2d") == b"caf\xe9 ?"


def test_dashboard_pdf_download(store):
    _seed_timeline(store, [True, False, True])
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url + "?period=all&detail=http://site&result=fail").read().decode()
        assert "details.pdf?period=all&amp;category=http%3A%2F%2Fsite&amp;result=fail\">Download PDF</a>" in page
        resp = urlopen(srv.url + "details.pdf?period=all&category=http://site&result=fail")
        assert resp.headers["Content-Type"] == "application/pdf"
        assert resp.headers["Content-Disposition"] == "attachment; filename=\"imon-site-all-fail.pdf\""
        data = resp.read()
        assert _check_pdf(data) == 1 and b"(1 FAIL check\\(s\\), newest first." in data
        resp = urlopen(srv.url + "details.pdf?period=all&category=internet")
        assert resp.headers["Content-Disposition"] == "attachment; filename=\"imon-internet-all.pdf\""
        assert _check_pdf(resp.read()) == 1
        with pytest.raises(HTTPError) as exc:
            urlopen(srv.url + "details.pdf?period=all&category=http://nope")
        assert exc.value.code == 400
    finally:
        srv.shutdown()


def _speed_opts(speed_server):
    return dict(download_url=speed_server.url + "down?bytes={bytes}", upload_url=speed_server.url + "up",
                download_bytes=512 * 1024, upload_bytes=128 * 1024)


def test_speed_test_measurements(speed_server, reference):
    from imon.speedtest import format_speed, measure_download, measure_ping, measure_upload, run_speed_test
    d = measure_download(speed_server.url + "down?bytes={bytes}", 512 * 1024)
    assert d["bytes"] == 512 * 1024 and d["mbps"] > 0 and d["seconds"] > 0
    u = measure_upload(speed_server.url + "up", 128 * 1024)
    assert u["bytes"] == 128 * 1024 and u["mbps"] > 0
    p = measure_ping([reference.url, "http://127.0.0.1:1"])  # second one: nothing listening
    assert p[0]["host"] == "127.0.0.1" and p[0]["ms"] is not None and p[0]["ms"] < 1000
    assert p[1]["ms"] is None

    r = run_speed_test(ping_hosts=[reference.url], **_speed_opts(speed_server))
    assert r["ok"] and r["errors"] == {}
    assert r["download_mbps"] > 0 and r["upload_mbps"] > 0 and r["ping_ms"] is not None
    assert r["measured_at"].endswith("+00:00") and r["duration_seconds"] >= 0
    text = format_speed(r)
    assert text.startswith("Download ") and " Mbps \u00b7 Upload " in text and " \u00b7 Ping " in text
    assert format_speed(None) == "not measured yet"

    # partial failure is reported, not raised
    speed_server.fail_upload = True
    r = run_speed_test(ping_hosts=[reference.url], **_speed_opts(speed_server))
    assert not r["ok"] and "upload" in r["errors"] and r["upload_mbps"] is None and r["download_mbps"] > 0
    assert "Upload failed" in format_speed(r)
    r = run_speed_test(ping_hosts=[], download_url="http://127.0.0.1:1/{bytes}", upload_url="http://127.0.0.1:1/")
    assert set(r["errors"]) == {"download", "upload"} and r["ping_ms"] is None


def test_speed_test_runs_one_at_a_time(speed_server):
    from imon import speedtest
    assert speedtest._lock.acquire(blocking=False)
    try:
        assert speedtest.run_exclusive(**_speed_opts(speed_server)) is None
    finally:
        speedtest._lock.release()
    assert speedtest.run_exclusive(**_speed_opts(speed_server))["ok"]


def test_store_speedtests(store):
    assert store.latest_speedtest() is None and store.speedtests() == []
    store.record_speedtest({"measured_at": _at(0), "download_mbps": 10.5, "upload_mbps": 2.0, "ping_ms": 12.3, "errors": {}})
    store.record_speedtest({"measured_at": _at(1), "download_mbps": None, "upload_mbps": 3.0, "ping_ms": None,
                            "errors": {"download": "x"}})
    latest = store.latest_speedtest()
    assert latest["measured_at"] == _at(1) and latest["download_mbps"] is None and latest["errors"] == {"download": "x"}
    assert [x["measured_at"] for x in store.speedtests()] == [_at(1), _at(0)]


def test_dashboard_speed_button_and_box(speed_server, reference, tmp_path):
    db = str(tmp_path / "speed.db")
    store = ResultStore(db)
    store.add_site(reference.url, KIND_REFERENCE)
    srv = ReportServer(store, port=0, speed_options=_speed_opts(speed_server))
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "Check internet speed" in page and "js-speed" in page
        assert "Internet is <b>" in page and "internet &amp; website" not in page  # capital I everywhere
        box = page[page.index("id=\"speed\""):page.index("id=\"speed\"") + 400]
        assert "not measured yet" in box and "class=\"banner speed\"" in page

        code, body = _post(srv.url + "api/speedtest", {})
        d = json.loads(body)
        assert code == 200 and d["ok"] and d["message"].startswith("Internet speed: Download ")
        assert d["result"]["download_mbps"] > 0 and d["result"]["ping"][0]["host"] == "127.0.0.1"

        page = urlopen(srv.url).read().decode()
        box = page[page.index("id=\"speed\""):page.index("</div>", page.index("id=\"speed\""))]
        assert "Download <b>" in box and "Mbps</b>" in box and "Upload <b>" in box and "Ping <b>" in box and "ms</b>" in box
        assert "measured " + d["result"]["measured_at"] in box

        # a second measurement replaces the box content; the form variant redirects back
        code, location = _NoRedirect.post(srv.url + "speedtest", {})
        assert code == 303 and "msg=Internet+speed" in location
        latest = json.loads(urlopen(srv.url + "api/speedtest").read())
        assert latest["latest"]["measured_at"] >= d["result"]["measured_at"] and len(latest["history"]) == 2

        # failures are shown in the box, and the box still shows the latest measure
        speed_server.fail_upload = True
        code, body = _post(srv.url + "api/speedtest", {})
        assert code == 400 and "upload:" in json.loads(body)["message"]
        page = urlopen(srv.url).read().decode()
        box = page[page.index("id=\"speed\""):page.index("</div>", page.index("id=\"speed\""))]
        assert "Upload <span class=\"err\">failed</span>" in box and "Download <b>" in box
    finally:
        srv.shutdown()
        store.close()

    # the latest measurement survives a restart (stored in the database)
    store = ResultStore(db)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        box = page[page.index("id=\"speed\""):page.index("</div>", page.index("id=\"speed\""))]
        assert "Download <b>" in box and "Upload <span class=\"err\">failed</span>" in box
    finally:
        srv.shutdown()
        store.close()


def test_cli_speed(speed_server, reference, tmp_path, capsys):
    db = str(tmp_path / "h.db")
    s = ResultStore(db)
    s.add_site(reference.url, KIND_REFERENCE)  # ping target (the defaults are unreachable offline)
    s.close()
    opts = _speed_opts(speed_server)
    args = ["speed", "--db", db, "--speed-download-url", opts["download_url"], "--speed-upload-url", opts["upload_url"],
            "--speed-download-mb", "0.5", "--speed-upload-mb", "0.125"]
    assert main(args + ["--record"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("INTERNET SPEED: Download ") and "ping " in out
    s = ResultStore(db)
    assert s.latest_speedtest()["download"]["bytes"] == 512 * 1024
    s.close()
    assert main(args + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    speed_server.fail_upload = True
    assert main(args) == 1
    assert "upload failed" in capsys.readouterr().out


def test_dashboard_layout_classes(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    ActiveMonitor([target.url], store, interval=0.05).run(max_cycles=1)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        assert "overflow-x: auto" in page                       # wide tables scroll inside their frame
        assert "details summary {" in page and "border: 1px solid #2563eb" in page  # Edit styled as a button
        assert page.count("<td class=\"time\">") >= 3 and "<td class=\"url\">" in page
    finally:
        srv.shutdown()


def test_dashboard_percentages_have_two_decimals(target, reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    am = ActiveMonitor([target.url], store, interval=0.05)
    am.run(max_cycles=3)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        page = urlopen(srv.url).read().decode()
        import re
        main_part = page[page.index("<main>"):page.index("</main>")]  # skip CSS like "width: 100%"
        main_part = re.sub(r"href=\"[^\"]*\"", "", main_part)        # skip URL-encoded links (%2F)
        pcts = re.findall(r"(\d+(?:\.\d+)?)%", main_part)
        assert len(pcts) >= 6 and all(re.fullmatch(r"\d+\.\d\d", p) for p in pcts), pcts
    finally:
        srv.shutdown()


def test_dashboard_actions_keep_selected_period(reference, store):
    store.add_site(reference.url, KIND_REFERENCE)
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        import urllib.request

        class H(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        req = Request(srv.url + "sites", data=urlencode({"action": "add", "url": "http://x.example"}).encode(),
                      method="POST", headers={"Referer": srv.url + "?period=last-7d"})
        try:
            resp = urllib.request.build_opener(H).open(req)
        except HTTPError as e:
            resp = e
        assert resp.getcode() == 303 and "period=last-7d" in resp.headers["Location"]
    finally:
        srv.shutdown()


def test_check_now_with_no_sites(store):
    srv = ReportServer(store, port=0)
    srv.start()
    try:
        code, body = _post(srv.url + "api/check", {})
        assert code == 400 and "add a site" in json.loads(body)["message"]
    finally:
        srv.shutdown()


def test_parse_multipart():
    body = ("--B\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n1\r\n"
            "--B\r\nContent-Disposition: form-data; name=\"b\"\r\n\r\nx y\r\n--B--\r\n")
    assert _parse_multipart(body, "multipart/form-data; boundary=B") == {"a": ["1"], "b": ["x y"]}
    assert _parse_multipart("", "multipart/form-data") == {}


# --- CLI ---------------------------------------------------------------------

def test_cli_check_and_legacy_invocation(target, reference, capsys):
    ref = ["--reference", reference.url]
    assert main(["check", "--url", target.url] + ref) == 0
    out = capsys.readouterr().out
    assert "INTERNET: UP (1/1" in out and "[UP] " + target.url in out
    assert main(["--url", target.url] + ref) == 0  # legacy form, no subcommand
    capsys.readouterr()
    target.healthy = False
    assert main(["--url", target.url] + ref) == 1
    assert "[DOWN]" in capsys.readouterr().out
    reference.healthy = False
    assert main(["--url", target.url] + ref) == 2  # internet down trumps site state
    assert "INTERNET: DOWN" in capsys.readouterr().out


def test_cli_check_uses_three_default_reference_sites(monkeypatch, capsys):
    import imon.cli as cli
    calls = []
    monkeypatch.setattr(cli.InternetMonitor, "check_url",
                        lambda self, url, timeout=None: calls.append(url) or _res(url, True, _ts(0)))
    assert main(["check"]) == 0
    assert calls == cli.DEFAULT_REFERENCE_SITES and len(calls) >= 3
    assert all(u.startswith("https://") for u in calls)


def test_cli_watch_then_report(target, reference, tmp_path, capsys):
    db = str(tmp_path / "h.db")
    ref = ["--reference", reference.url]
    assert main(["watch", "--url", target.url, "--db", db, "--interval", "0.05", "--cycles", "3"] + ref) == 0
    out = capsys.readouterr().out
    assert "1 reference site(s), 1 of your website(s)" in out
    assert out.count("[UP] " + target.url) == 3 and out.count("[UP] " + reference.url) == 3

    assert main(["report", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "INTERNET: UP" in out and target.url in out and "100.00%" in out

    assert main(["report", "--db", db, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["checks"] == 3 and report["internet"]["cycles"] == 3

    # reference sites persist in the DB; a later run without --reference keeps them
    assert main(["watch", "--db", db, "--interval", "0.05", "--cycles", "1", "--quiet"]) == 0
    s = ResultStore(db)
    assert s.site_urls(KIND_REFERENCE) == [reference.url] and s.site_urls(KIND_USER) == [target.url]
    s.close()


def test_cli_report_periods(tmp_path, capsys):
    db = str(tmp_path / "h.db")
    st = ResultStore(db)
    _seed_timeline(st, [True, True, False, False, True, True, True])
    st.close()
    assert main(["report", "--db", db, "--since", _at(0), "--until", _at(60)]) == 0
    out = capsys.readouterr().out
    assert "TIME WITHOUT INCIDENTS: 66.67%" in out and "INCIDENTS / MEASURES WITHOUT INCIDENTS: 1 / 5" in out
    assert "imon availability report - 2024-03-10T12:00:00+00:00 to 2024-03-10T13:00:00+00:00" in out

    assert main(["report", "--db", db, "--period", "all", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["availability"]["window"]["period"] == "all"
    assert d["availability"]["sites"][0]["incidents"] == 1 and d["internet"]["state"] == "UP"

    assert main(["report", "--db", db, "--since", "nonsense"]) == 2
    assert "invalid timestamp" in capsys.readouterr().err

    assert main(["report", "--db", db, "--period", "all", "--detail", "http://site"]) == 0
    out = capsys.readouterr().out
    assert "imon detailed report - http://site - 7 check(s)" in out and out.count(" FAIL ") == 2
    assert "100.0%" not in out and "100.00%" in out
    assert main(["report", "--db", db, "--period", "all", "--detail", "internet", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["details"]["total"] == 7
    assert main(["report", "--db", db, "--detail", "http://nope"]) == 2

    pdf = str(tmp_path / "detail.pdf")
    assert main(["report", "--db", db, "--period", "all", "--detail", "http://site", "--result", "fail", "--pdf", pdf]) == 0
    capsys.readouterr()
    data = open(pdf, "rb").read()
    assert _check_pdf(data) == 1 and b"(2 FAIL check\\(s\\), newest first." in data
    assert main(["report", "--db", db, "--period", "all", "--pdf", pdf]) == 0
    assert b"(imon availability report: All time)" in open(pdf, "rb").read()


def test_cli_report_exit_codes(target, reference, tmp_path, capsys):
    db = str(tmp_path / "h.db")
    ref = ["--reference", reference.url]
    target.healthy = False
    main(["check", "--url", target.url, "--db", db, "--record"] + ref)
    assert main(["report", "--db", db]) == 1
    assert "DOWN" in capsys.readouterr().out
    reference.healthy = False
    main(["check", "--url", target.url, "--db", db, "--record"] + ref)
    assert main(["report", "--db", db]) == 2
    out = capsys.readouterr().out
    assert "INTERNET: DOWN" in out and "UNKNOWN" in out
