"""Core monitoring primitives for imon.

This module provides:

* :class:`InternetMonitor` - performs a single availability check of one or
  more URLs.
* :class:`ResultStore`    - persists the list of monitored sites and every
  check result to SQLite so reports can be built over any time window.
* :class:`ActiveMonitor`  - runs checks continuously on a fixed interval
  (optionally in a background thread) and records them.
* :func:`build_report` / :func:`format_report_text` - summarise the recorded
  history (internet state, uptime %, latency, incidents).

Sites come in two kinds:

* ``reference`` sites are highly-available public sites used to decide whether
  the *internet connection itself* is up. The internet is considered UP when at
  least one reference site answers.
* ``user`` sites are the sites the user actually cares about. A failed check of
  a user site during a cycle in which the internet was down is recorded as
  *inconclusive* and excluded from that site's uptime, so a local outage does
  not show up as downtime of the user's websites.
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".imon", "history.db")

#: Highly available sites used to determine whether the internet is reachable.
DEFAULT_REFERENCE_SITES = [
    "https://www.google.com",
    "https://www.cloudflare.com",
    "https://www.microsoft.com",
]

#: Recommended minimum number of reference sites for trustworthy detection.
MIN_REFERENCE_SITES = 3

KIND_REFERENCE = "reference"
KIND_USER = "user"

#: Result filters for detailed reports: websites use ok/fail/inconclusive,
#: the internet category uses up/down.
RESULT_FILTERS_SITE = ("ok", "fail", "inconclusive")
RESULT_FILTERS_INTERNET = ("up", "down")
_DOWN_CYCLES = "(SELECT cycle FROM cycles WHERE ref_total > 0 AND ref_reachable = 0)"


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_url(url):
    """Return a normalised URL or raise ``ValueError`` if it is not http(s)."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    if not urlparse(url).netloc or " " in url:
        raise ValueError("URL is not valid")
    return url


class InternetMonitor:
    """Performs one-off availability checks against a list of URLs."""

    def __init__(self, urls=None, timeout=5):
        self.urls = list(urls or [])
        self.timeout = timeout

    def check_url(self, url, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        start = time.time()
        try:
            resp = requests.get(url, timeout=timeout)
            latency = (time.time() - start) * 1000
            return {
                "url": url,
                "status": resp.status_code,
                "latency_ms": latency,
                "available": resp.status_code < 400,
                "error": None,
                "checked_at": _utcnow_iso(),
            }
        except Exception as exc:  # network errors, timeouts, bad URLs...
            return {
                "url": url,
                "status": 0,
                "latency_ms": 0,
                "available": False,
                "error": exc.__class__.__name__,
                "checked_at": _utcnow_iso(),
            }

    def run_checks(self):
        return [self.check_url(u) for u in self.urls]


class ResultStore:
    """SQLite-backed list of monitored sites and history of check results.

    Use ``":memory:"`` as the path for an ephemeral store (handy in tests).
    The store is safe to use from multiple threads.
    """

    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    available INTEGER NOT NULL,
                    error TEXT,
                    checked_at TEXT NOT NULL,
                    cycle TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_results_url_time ON results (url, checked_at);
                CREATE TABLE IF NOT EXISTS sites (
                    url TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle TEXT PRIMARY KEY,
                    checked_at TEXT NOT NULL,
                    first_id INTEGER NOT NULL,
                    ref_total INTEGER NOT NULL DEFAULT 0,
                    ref_reachable INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_cycles_time ON cycles (checked_at, first_id);
                CREATE TABLE IF NOT EXISTS speedtests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    measured_at TEXT NOT NULL,
                    download_mbps REAL,
                    upload_mbps REAL,
                    ping_ms REAL,
                    details TEXT
                );
                """
            )
            # Upgrade databases created before the ``cycle`` column existed.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(results)")}
            if "cycle" not in cols:
                self._conn.execute("ALTER TABLE results ADD COLUMN cycle TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_results_cycle ON results (cycle)")
            self._conn.commit()
        self._rebuild_cycles_if_empty()

    def _rebuild_cycles_if_empty(self):
        """One-time build of the per-cycle summary for databases from older versions."""
        with self._lock:
            if self._conn.execute("SELECT 1 FROM cycles LIMIT 1").fetchone():
                return
            if not self._conn.execute("SELECT 1 FROM results WHERE cycle IS NOT NULL LIMIT 1").fetchone():
                return
            refs = [r["url"] for r in self._conn.execute("SELECT url FROM sites WHERE kind = ?", (KIND_REFERENCE,))]
            is_ref = "url IN ({})".format(",".join("?" * len(refs))) if refs else "0"
            self._conn.execute(
                "INSERT INTO cycles (cycle, checked_at, first_id, ref_total, ref_reachable) "
                "SELECT cycle, MIN(checked_at), MIN(id), SUM(CASE WHEN {0} THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN {0} AND available THEN 1 ELSE 0 END) FROM results WHERE cycle IS NOT NULL "
                "GROUP BY cycle".format(is_ref),
                refs + refs,
            )
            self._conn.commit()

    # -- speed tests ---------------------------------------------------------

    def record_speedtest(self, result):
        with self._lock:
            self._conn.execute(
                "INSERT INTO speedtests (measured_at, download_mbps, upload_mbps, ping_ms, details) VALUES (?, ?, ?, ?, ?)",
                (result["measured_at"], result.get("download_mbps"), result.get("upload_mbps"), result.get("ping_ms"),
                 json.dumps(result)),
            )
            self._conn.commit()

    def latest_speedtest(self):
        """The most recent speed test result dict, or None."""
        with self._lock:
            row = self._conn.execute("SELECT details FROM speedtests ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["details"]) if row else None

    def speedtests(self, limit=50):
        with self._lock:
            rows = self._conn.execute("SELECT details FROM speedtests ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [json.loads(r["details"]) for r in rows]

    # -- sites ---------------------------------------------------------------

    def add_site(self, url, kind=KIND_USER):
        """Register a site to monitor. Returns True if it was newly added."""
        url = validate_url(url)
        if kind not in (KIND_REFERENCE, KIND_USER):
            raise ValueError("kind must be 'reference' or 'user'")
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO sites (url, kind, added_at) VALUES (?, ?, ?)",
                (url, kind, _utcnow_iso()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def remove_site(self, url):
        """Stop monitoring ``url`` (its history is kept). Returns True if removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sites WHERE url = ?", (url,))
            self._conn.commit()
            return cur.rowcount == 1

    def replace_site(self, old_url, new_url):
        """Substitute ``old_url`` with ``new_url`` keeping its kind. Returns True if replaced."""
        new_url = validate_url(new_url)
        with self._lock:
            if new_url != old_url and self._conn.execute(
                "SELECT 1 FROM sites WHERE url = ?", (new_url,)
            ).fetchone():
                raise ValueError("{} is already monitored".format(new_url))
            cur = self._conn.execute("UPDATE sites SET url = ? WHERE url = ?", (new_url, old_url))
            self._conn.commit()
            return cur.rowcount == 1

    def sites(self, kind=None):
        query = "SELECT url, kind, added_at FROM sites"
        params = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY CASE kind WHEN 'reference' THEN 0 ELSE 1 END, added_at, url"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def site_urls(self, kind=None):
        return [s["url"] for s in self.sites(kind)]

    def site_kinds(self):
        """Mapping of url -> kind for all registered sites."""
        return {s["url"]: s["kind"] for s in self.sites()}

    def ensure_reference_sites(self, urls=None):
        """Make sure reference sites exist, seeding ``urls`` (or the defaults) if none do."""
        if not self.sites(KIND_REFERENCE):
            for u in urls or DEFAULT_REFERENCE_SITES:
                self.add_site(u, KIND_REFERENCE)
        return self.site_urls(KIND_REFERENCE)

    # -- results -------------------------------------------------------------

    def record(self, results, cycle=None):
        """Persist one result dict or an iterable of result dicts."""
        if isinstance(results, dict):
            results = [results]
        rows = [
            (
                r["url"],
                int(r["status"]),
                float(r["latency_ms"]),
                1 if r["available"] else 0,
                r.get("error"),
                r.get("checked_at") or _utcnow_iso(),
                r.get("cycle") or cycle,
            )
            for r in results
        ]
        kinds = self.site_kinds()
        with self._lock:
            for row in rows:
                cur = self._conn.execute(
                    "INSERT INTO results (url, status, latency_ms, available, error, checked_at, cycle) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                url, available, checked_at, cycle = row[0], row[3], row[5], row[6]
                if cycle is None:
                    continue
                is_ref = 1 if kinds.get(url) == KIND_REFERENCE else 0
                # per-cycle summary of the reference sites (keeps reports cheap)
                self._conn.execute(
                    "INSERT OR IGNORE INTO cycles (cycle, checked_at, first_id, ref_total, ref_reachable) "
                    "VALUES (?, ?, ?, 0, 0)",
                    (cycle, checked_at, cur.lastrowid),
                )
                self._conn.execute(
                    "UPDATE cycles SET ref_total = ref_total + ?, ref_reachable = ref_reachable + ?, "
                    "checked_at = MIN(checked_at, ?) WHERE cycle = ?",
                    (is_ref, is_ref & available, checked_at, cycle),
                )
            self._conn.commit()

    def results(self, url=None, since=None, limit=None, urls=None, until=None, cycle=None, cycles=None, offset=0,
                available=None, result=None, user_site=True):
        """Return results, oldest first, optionally filtered by URL(s) / ISO timestamps.

        ``limit``/``offset`` select a page counted from the newest result
        (page 1 = ``offset=0``), still returned oldest-first.
        """
        query = "SELECT * FROM results"
        clauses, params = [], []
        if url:
            clauses.append("url = ?")
            params.append(url)
        if urls is not None:
            if not urls:
                return []
            clauses.append("url IN ({})".format(",".join("?" * len(urls))))
            params.extend(urls)
        if since:
            clauses.append("checked_at >= ?")
            params.append(since)
        if until:
            clauses.append("checked_at <= ?")
            params.append(until)
        if cycle:
            clauses.append("cycle = ?")
            params.append(cycle)
        if cycles is not None:
            if not cycles:
                return []
            clauses.append("cycle IN ({})".format(",".join("?" * len(cycles))))
            params.extend(cycles)
        if available is not None:
            clauses.append("available = ?")
            params.append(1 if available else 0)
        if result:
            extra, _ = self._where(result=result, user_site=user_site)
            clauses.append("(" + extra[len(" WHERE "):] + ")")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY checked_at ASC, id ASC"
        if limit:
            # take the newest N (after skipping ``offset``), then present them oldest-first
            query = ("SELECT * FROM (" + query.replace(" ASC", " DESC") + " LIMIT ? OFFSET ?) "
                     "ORDER BY checked_at ASC, id ASC")
            params += [int(limit), int(offset or 0)]
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _where(self, url=None, urls=None, since=None, until=None, cycle_only=False, result=None, user_site=True):
        clauses, params = [], []
        if result == "ok":
            clauses.append("available = 1")
        elif result == "fail":
            clauses.append("available = 0")
            if user_site:  # failures while the internet was down are inconclusive, not failures
                clauses.append("(cycle IS NULL OR cycle NOT IN {})".format(_DOWN_CYCLES))
        elif result == "inconclusive":
            clauses.append("available = 0 AND cycle IN {}".format(_DOWN_CYCLES) if user_site else "0")
        elif result:
            raise ValueError("result must be one of {}".format(", ".join(RESULT_FILTERS_SITE)))
        if url:
            clauses.append("url = ?")
            params.append(url)
        if urls is not None:
            clauses.append("url IN ({})".format(",".join("?" * len(urls))) if urls else "0")
            params.extend(urls)
        if since:
            clauses.append("checked_at >= ?")
            params.append(since)
        if until:
            clauses.append("checked_at <= ?")
            params.append(until)
        if cycle_only:
            clauses.append("cycle IS NOT NULL")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def series(self, url, since=None, until=None):
        """Lightweight timeline of one URL: dicts with checked_at/available/cycle only."""
        where, params = self._where(url=url, since=since, until=until)
        with self._lock:
            rows = self._conn.execute(
                "SELECT checked_at, available, cycle FROM results" + where + " ORDER BY checked_at ASC, id ASC", params
            ).fetchall()
        return [{"checked_at": r[0], "available": bool(r[1]), "cycle": r[2]} for r in rows]

    @staticmethod
    def _cycle_where(since, until, state=None):
        clauses, params = ["ref_total > 0"], []
        if state == "up":
            clauses.append("ref_reachable > 0")
        elif state == "down":
            clauses.append("ref_reachable = 0")
        elif state:
            raise ValueError("result must be one of {}".format(", ".join(RESULT_FILTERS_INTERNET)))
        if since:
            clauses.append("checked_at >= ?")
            params.append(since)
        if until:
            clauses.append("checked_at <= ?")
            params.append(until)
        return " WHERE " + " AND ".join(clauses), params

    def cycle_states(self, urls=None, since=None, until=None, limit=None, offset=0, state=None):
        """One entry per check cycle that included reference sites, oldest first:
        ``{"cycle", "checked_at", "available", "reachable", "total"}`` where
        ``available`` means at least one reference site answered (internet up).
        ``limit``/``offset`` page from the newest cycle. (``urls`` is accepted
        for compatibility; the summary always covers the reference sites.)"""
        where, params = self._cycle_where(since, until, state)
        query = ("SELECT cycle, checked_at, ref_reachable, ref_total, first_id FROM cycles" + where +
                 " ORDER BY checked_at, first_id")
        if limit:
            query = "SELECT * FROM (" + query.replace("ORDER BY checked_at, first_id", "ORDER BY checked_at DESC, first_id DESC") + \
                    " LIMIT ? OFFSET ?) ORDER BY checked_at, first_id"
            params = params + [int(limit), int(offset or 0)]
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [{"cycle": r[0], "checked_at": r[1], "available": r[2] > 0, "reachable": r[2], "total": r[3]}
                for r in rows]

    def cycle_before(self, ts):
        """The last reference cycle strictly before ISO timestamp ``ts`` (or None)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT cycle, checked_at, ref_reachable, ref_total FROM cycles WHERE ref_total > 0 AND checked_at < ? "
                "ORDER BY checked_at DESC, first_id DESC LIMIT 1", (ts,)
            ).fetchone()
        if not r:
            return None
        return {"cycle": r[0], "checked_at": r[1], "available": r[2] > 0, "reachable": r[2], "total": r[3]}

    def count(self, url=None, urls=None, since=None, until=None, result=None, user_site=True):
        """Number of results matching the filters (cheap, no row materialisation)."""
        where, params = self._where(url, urls, since, until, result=result, user_site=user_site)
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM results" + where, params).fetchone()[0]

    def count_cycles(self, urls=None, since=None, until=None, state=None):
        """Number of check cycles that included reference sites in the window."""
        where, params = self._cycle_where(since, until, state)
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM cycles" + where, params).fetchone()[0]

    def aggregate(self, urls, since=None, until=None):
        """Per-URL totals computed in SQL: checks, ok, avg/max latency of OK checks, last error."""
        where, params = self._where(urls=urls, since=since, until=until)
        with self._lock:
            rows = self._conn.execute(
                "SELECT url, COUNT(*) AS checks, SUM(available) AS ok, "
                "AVG(CASE WHEN available THEN latency_ms END) AS avg_latency_ms, "
                "MIN(CASE WHEN available THEN latency_ms END) AS min_latency_ms, "
                "MAX(CASE WHEN available THEN latency_ms END) AS max_latency_ms, "
                "MAX(checked_at) AS last_checked_at FROM results" + where + " GROUP BY url",
                params,
            ).fetchall()
        out = {}
        for r in rows:
            out[r["url"]] = {
                "url": r["url"], "checks": r["checks"], "ok": r["ok"] or 0, "failed": r["checks"] - (r["ok"] or 0),
                "uptime_pct": round(100.0 * (r["ok"] or 0) / r["checks"], 2) if r["checks"] else 0.0,
                "avg_latency_ms": round(r["avg_latency_ms"], 2) if r["avg_latency_ms"] is not None else None,
                "min_latency_ms": round(r["min_latency_ms"], 2) if r["min_latency_ms"] is not None else None,
                "max_latency_ms": round(r["max_latency_ms"], 2) if r["max_latency_ms"] is not None else None,
                "last_checked_at": r["last_checked_at"],
            }
        return out

    def last_before(self, url, ts):
        """The most recent result for ``url`` strictly before ISO timestamp ``ts``, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM results WHERE url = ? AND checked_at < ? ORDER BY checked_at DESC, id DESC LIMIT 1",
                (url, ts),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def urls(self):
        """Distinct URLs that have at least one recorded result."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT url FROM results ORDER BY url").fetchall()
        return [r["url"] for r in rows]

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM results")
            self._conn.execute("DELETE FROM cycles")
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_dict(row):
        return {
            "url": row["url"],
            "status": row["status"],
            "latency_ms": row["latency_ms"],
            "available": bool(row["available"]),
            "error": row["error"],
            "checked_at": row["checked_at"],
            "cycle": row["cycle"],
        }


def check_now(store, urls=None, timeout=5, include_reference=True):
    """Check sites immediately and record the results as one cycle.

    ``urls=None`` checks every registered site. When specific ``urls`` are
    given the reference sites are checked as well (unless
    ``include_reference`` is False) so the internet state of this cycle is
    known and failures can be classified as real or inconclusive.
    Returns the list of results (reference sites first).
    """
    if urls is None:
        targets = store.site_urls()
    else:
        targets = list(store.site_urls(KIND_REFERENCE)) if include_reference else []
        targets += [u for u in urls if u not in targets]
    cycle = uuid.uuid4().hex
    results = InternetMonitor(targets, timeout=timeout).run_checks()
    for r in results:
        r["cycle"] = cycle
    store.record(results)
    return results


class ActiveMonitor:
    """Continuously checks sites every ``interval`` seconds and records results.

    The list of sites is read from ``store`` at the start of every cycle, so
    sites added or removed (e.g. from the web dashboard) take effect on the
    next cycle. ``urls`` given here are registered in the store as user sites.

    ``on_results`` (optional) is called with the list of results after every
    cycle, which the CLI uses for live output. ``start()`` runs the loop in a
    daemon thread; ``run()`` blocks the calling thread.
    """

    def __init__(self, urls=None, store=None, interval=60, timeout=5, on_results=None):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        if store is None:
            raise ValueError("store is required")
        self.store = store
        self.monitor = InternetMonitor([], timeout=timeout)
        for u in urls or []:
            store.add_site(u, KIND_USER)
        self.interval = interval
        self.on_results = on_results
        self.cycles = 0
        self._stop = threading.Event()
        self._thread = None

    @property
    def urls(self):
        return self.store.site_urls()

    def run_once(self):
        results = check_now(self.store, timeout=self.monitor.timeout)
        self.cycles += 1
        if self.on_results:
            self.on_results(results)
        return results

    def run(self, max_cycles=None):
        """Block and run check cycles until stopped (or ``max_cycles`` reached)."""
        self._stop.clear()
        while not self._stop.is_set():
            started = time.monotonic()
            self.run_once()
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            elapsed = time.monotonic() - started
            if self._stop.wait(max(0.0, self.interval - elapsed)):
                break

    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="imon-active-monitor", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_timeout=5):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(join_timeout)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())


def _summarise(url, kind, series, last, agg, internet_by_cycle, errors=None):
    """Summarise one site's results.

    ``series`` is the light timeline (checked_at/available/cycle), ``last`` the
    newest full result, ``agg`` the SQL aggregate for latency figures and
    ``errors`` maps (checked_at, cycle) of failed checks to their error text.
    ``internet_by_cycle`` maps cycle id -> bool (internet up during that cycle).
    For user sites, failures in cycles where the internet was down are
    *inconclusive*: they are neither up nor down and do not open incidents.
    """
    conclusive = []
    inconclusive = 0
    for r in series:
        if kind == KIND_USER and _inconclusive(r, internet_by_cycle):
            inconclusive += 1
        else:
            conclusive.append(r)

    total = len(conclusive)
    up = sum(1 for r in conclusive if r["available"])

    # Incidents: contiguous runs of conclusive failed checks.
    incidents = []
    current = None
    for r in conclusive:
        if not r["available"]:
            if current is None:
                current = {"start": r["checked_at"], "end": r["checked_at"], "checks": 0, "ongoing": True,
                           "error": (errors or {}).get((r["checked_at"], r["cycle"]), "failed")}
            current["end"] = r["checked_at"]
            current["checks"] += 1
        elif current is not None:
            current["ongoing"] = False
            incidents.append(current)
            current = None
    if current is not None:
        incidents.append(current)
    if incidents and incidents[-1]["ongoing"] and not errors:
        incidents[-1]["error"] = last["error"] or "HTTP {}".format(last["status"])

    if last["available"]:
        state = "UP"
    elif conclusive and conclusive[-1] is series[-1]:
        state = "DOWN"
    else:  # latest check failed while the internet was down: cannot tell
        state = "UNKNOWN"

    return {
        "url": url,
        "kind": kind,
        "checks": len(series),
        "up": up,
        "down": total - up,
        "inconclusive": inconclusive,
        "uptime_pct": round(100.0 * up / total, 2) if total else 0.0,
        "avg_latency_ms": agg["avg_latency_ms"] if agg else None,
        "min_latency_ms": agg["min_latency_ms"] if agg else None,
        "max_latency_ms": agg["max_latency_ms"] if agg else None,
        "current_state": state,
        "last_available": last["available"],
        "last_status": last["status"],
        "last_error": last["error"],
        "last_checked_at": last["checked_at"],
        "first_checked_at": series[0]["checked_at"],
        "incidents": incidents,
    }


def _internet_by_cycle(store, ref_urls, since=None, until=None):
    """Map cycle id -> internet up (any reference site answered).

    Also returns the id and timestamp of the most recent cycle. Cycles are
    ordered by insertion so two cycles within the same second are told apart.
    """
    by_cycle = {}
    last_cycle, last_ts = None, None
    for c in store.cycle_states(ref_urls, since, until):
        by_cycle[c["cycle"]] = c["available"]
        last_cycle, last_ts = c["cycle"], c["checked_at"]
    return by_cycle, last_cycle, last_ts


def _inconclusive(r, internet_by_cycle):
    return (
        not r["available"]
        and r.get("cycle") is not None
        and internet_by_cycle.get(r["cycle"]) is False
    )


def build_report(store, since=None, urls=None, until=None):
    """Aggregate recorded results into a JSON-serialisable status report.

    The report contains ``internet`` (state derived from reference sites),
    ``reference`` (per reference site summaries), ``sites`` (per user site
    summaries) and an overall ``summary``, for checks between ``since`` and
    ``until`` (both optional ISO timestamps).
    """
    kinds = store.site_kinds()
    ref_urls = [u for u, k in kinds.items() if k == KIND_REFERENCE]
    # Reference sites are always evaluated (they define the internet state);
    # ``urls`` only filters which user sites are reported. URLs with history
    # that are no longer registered are included when asked for explicitly.
    if urls:
        targets = ref_urls + [u for u in urls if u not in ref_urls]
    else:
        targets = store.site_urls()
    internet_by_cycle, last_cycle, last_ts = _internet_by_cycle(store, ref_urls, since, until)
    aggregates = store.aggregate(targets, since, until)

    reference, sites = [], []
    for u in targets:
        series = store.series(u, since, until)
        if not series:
            continue
        last = store.results(url=u, since=since, until=until, limit=1)[-1]
        errors = {(r["checked_at"], r["cycle"]): r["error"] or "HTTP {}".format(r["status"])
                  for r in store.results(url=u, since=since, until=until, available=False)}
        kind = kinds.get(u, KIND_USER)
        summary = _summarise(u, kind, series, last, aggregates.get(u), internet_by_cycle, errors)
        (reference if kind == KIND_REFERENCE else sites).append(summary)

    # Current internet state = state of the most recent reference cycle.
    if last_cycle is not None:
        internet_state = "UP" if internet_by_cycle[last_cycle] else "DOWN"
        internet_last = last_ts
    elif reference:
        internet_state = "UP" if any(s["last_available"] for s in reference) else "DOWN"
        internet_last = max(s["last_checked_at"] for s in reference)
    else:
        internet_state, internet_last = "UNKNOWN", None

    cycles_total = len(internet_by_cycle)
    cycles_up = sum(1 for v in internet_by_cycle.values() if v)
    internet = {
        "state": internet_state,
        "last_checked_at": internet_last,
        "reference_up": sum(1 for s in reference if s["last_available"]),
        "reference_total": len(reference),
        "cycles": cycles_total,
        "uptime_pct": round(100.0 * cycles_up / cycles_total, 2) if cycles_total else 0.0,
    }

    total_checks = sum(s["checks"] - s["inconclusive"] for s in sites)
    total_up = sum(s["up"] for s in sites)
    return {
        "generated_at": _utcnow_iso(),
        "since": since,
        "until": until,
        "internet": internet,
        "reference": reference,
        "sites": sites,
        "summary": {
            "sites": len(sites),
            "sites_up": sum(1 for s in sites if s["current_state"] == "UP"),
            "sites_down": sum(1 for s in sites if s["current_state"] == "DOWN"),
            "checks": total_checks,
            "inconclusive": sum(s["inconclusive"] for s in sites),
            "overall_uptime_pct": round(100.0 * total_up / total_checks, 2) if total_checks else 0.0,
            "open_incidents": sum(1 for s in sites for i in s["incidents"] if i["ongoing"]),
        },
    }


# --- time-window reports ----------------------------------------------------

PERIOD_PRESETS = [
    ("last-hour", "Last hour"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last-24h", "Last 24 hours"),
    ("last-7d", "Last 7 days"),
    ("last-30d", "Last 30 days"),
    ("all", "All time"),
]
DEFAULT_PERIOD = "last-24h"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value):
    """Parse an ISO-8601 timestamp (naive values are taken as UTC) into an aware datetime."""
    value = (value or "").strip()
    if not value:
        return None
    if len(value) == 25 and value.endswith("+00:00"):  # our own canonical format: fast path
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # "+00:00" arrives as " 00:00" when a query string is not URL-encoded
    value = re.sub(r" (\d\d:?\d\d)$", r"+\1", value)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid timestamp: {!r} (use e.g. 2024-01-31T14:00)".format(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_period(period=None, since=None, until=None, now=None):
    """Turn a preset name or a custom since/until into a concrete window.

    Returns ``{"period", "label", "start", "end"}`` with ISO timestamps
    (``start`` may be None for "all time"). A custom window is used when
    ``since`` or ``until`` is given; otherwise ``period`` (default last 24h).
    """
    now = now or datetime.now(timezone.utc)
    if since or until:
        start = parse_timestamp(since)
        end = parse_timestamp(until) or now
        if start and start > end:
            raise ValueError("'since' must be before 'until'")
        label = "{} to {}".format(_iso(start) if start else "beginning", _iso(end))
        return {"period": "custom", "label": label, "start": _iso(start) if start else None, "end": _iso(end)}

    period = period or DEFAULT_PERIOD
    labels = dict(PERIOD_PRESETS)
    if period not in labels:
        raise ValueError("unknown period {!r}; choose one of {}".format(period, ", ".join(labels)))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "last-hour":
        start, end = now - timedelta(hours=1), now
    elif period == "today":
        start, end = midnight, now
    elif period == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif period == "last-24h":
        start, end = now - timedelta(hours=24), now
    elif period == "last-7d":
        start, end = now - timedelta(days=7), now
    elif period == "last-30d":
        start, end = now - timedelta(days=30), now
    else:  # all
        start, end = None, now
    return {"period": period, "label": labels[period], "start": _iso(start) if start else None, "end": _iso(end)}


def _site_availability(url, results, previous, start, end, internet_by_cycle, category="website"):
    """Time-based availability of one category inside [start, end].

    ``results`` are the checks inside the window, ``previous`` the last check
    before it (or None). A check's state holds until the next conclusive
    check; inconclusive checks do not change the state.
    """
    conclusive = [r for r in results if not _inconclusive(r, internet_by_cycle)]
    if previous is not None and _inconclusive(previous, internet_by_cycle):
        previous = None

    measures = len(results)
    ok = sum(1 for r in results if r["available"])
    failed = sum(1 for r in conclusive if not r["available"])
    inconclusive = measures - len(conclusive)

    # Timeline of (time, available) points, clipped to the window. Only state
    # transitions matter for the maths, so runs of identical state collapse
    # into their first check (keeps long histories cheap).
    points = []
    if previous is not None:
        points.append((start, previous["available"]))
    prev_state = previous["available"] if previous is not None else None
    for r in conclusive:
        if r["available"] != prev_state:
            points.append((parse_timestamp(r["checked_at"]), r["available"]))
            prev_state = r["available"]
    base = {
        "category": category, "url": url, "measures": measures, "measures_ok": ok, "measures_failed": failed,
        "inconclusive": inconclusive, "incidents": 0, "observed_seconds": 0, "downtime_seconds": 0,
        "time_without_incidents_pct": None, "incidents_per_1000_ok": None,
        "first_checked_at": results[0]["checked_at"] if results else None,
        "last_checked_at": results[-1]["checked_at"] if results else None,
    }
    if not points:
        return base
    observed_start = max(points[0][0], start)
    observed_end = max(end, observed_start)
    downtime = 0.0
    incidents = 0
    in_incident = False
    for i, (t, available) in enumerate(points):
        seg_start = max(t, observed_start)
        seg_end = points[i + 1][0] if i + 1 < len(points) else observed_end
        seg_end = min(seg_end, observed_end)
        if not available:
            if not in_incident:
                incidents += 1
                in_incident = True
            if seg_end > seg_start:
                downtime += (seg_end - seg_start).total_seconds()
        else:
            in_incident = False
    observed = (observed_end - observed_start).total_seconds()
    if observed > 0:
        pct = round(100.0 * (observed - downtime) / observed, 2)
    else:  # first check happened just now: the state so far is the whole story
        pct = 0.0 if in_incident else 100.0
    base.update({
        "incidents": incidents,
        "observed_seconds": round(observed),
        "downtime_seconds": round(downtime),
        "time_without_incidents_pct": pct,
        "incidents_per_1000_ok": round(1000.0 * incidents / ok, 2) if ok else None,
    })
    return base


def _internet_cycles(store, ref_urls, since=None, until=None):
    """Reference cycles in a window, aggregated in SQL (see ``ResultStore.cycle_states``)."""
    return store.cycle_states(ref_urls, since, until)


def _internet_cycle_before(store, ref_urls, ts):
    """The last reference cycle strictly before ISO timestamp ``ts`` (or None)."""
    return store.cycle_before(ts)


def build_availability_report(store, period=None, since=None, until=None, urls=None, now=None):
    """Report for a time window: % of time without incidents and incident
    counts vs. measures without incidents, for the internet connection and
    for each of the user's websites (every registered website is listed,
    even before its first check)."""
    now = now or datetime.now(timezone.utc)
    window = resolve_period(period, since, until, now)
    start = parse_timestamp(window["start"]) if window["start"] else None
    # Compute against the real clock (not truncated to seconds) so a check
    # made in the current second still yields an observation, and never
    # extend into the future.
    end = parse_timestamp(window["end"])
    end = now if window["end"] == _iso(now) else min(end, now)

    kinds = store.site_kinds()
    ref_urls = [u for u, k in kinds.items() if k == KIND_REFERENCE]
    targets = urls or store.site_urls(KIND_USER)
    targets = [u for u in targets if kinds.get(u, KIND_USER) == KIND_USER]

    # Internet category: one "measure" per reference cycle.
    ref_cycles = _internet_cycles(store, ref_urls, window["start"], window["end"])
    ref_prev = _internet_cycle_before(store, ref_urls, window["start"]) if start else None
    inet_start = start if start else (parse_timestamp(ref_cycles[0]["checked_at"]) if ref_cycles else end)
    internet = _site_availability(None, ref_cycles, ref_prev, min(inet_start, end), end, {}, category="internet")
    internet["reference_total"] = len(ref_urls)

    # Each reference site on its own, so an unreliable one can be spotted and
    # substituted. Reference sites are never "inconclusive".
    reference = []
    for u in store.site_urls(KIND_REFERENCE):
        rs = store.series(u, window["start"], window["end"])
        prev = store.last_before(u, window["start"]) if start else None
        first = parse_timestamp(rs[0]["checked_at"]) if rs else end
        reference.append(_site_availability(u, rs, prev, start if start else min(first, end), end, {},
                                            category="reference"))

    # Internet state per cycle is needed to classify website failures; it has
    # to cover each site's previous check too, so fetch from a bit earlier.
    previous = {u: (store.last_before(u, window["start"]) if start else None) for u in targets}
    earliest = [p["checked_at"] for p in previous.values() if p]
    inet_since = min(earliest + [window["start"]]) if start else None
    internet_by_cycle, _, _ = _internet_by_cycle(store, ref_urls, inet_since, window["end"])

    sites = []
    for u in targets:
        rs = store.series(u, window["start"], window["end"])
        if u not in kinds and not rs:  # unknown URL without history: nothing to report
            continue
        first = parse_timestamp(rs[0]["checked_at"]) if rs else end
        site_start = start if start else min(first, end)
        sites.append(_site_availability(u, rs, previous[u], site_start, end, internet_by_cycle))

    measured = [s for s in sites if s["time_without_incidents_pct"] is not None]
    observed = sum(s["observed_seconds"] for s in measured)
    downtime = sum(s["downtime_seconds"] for s in measured)
    total_ok = sum(s["measures_ok"] for s in sites)
    total_incidents = sum(s["incidents"] for s in sites)
    return {
        "generated_at": _utcnow_iso(),
        "window": window,
        "internet": internet,
        "reference": reference,
        "sites": sites,
        "summary": {
            "sites": len(sites),
            "time_without_incidents_pct": round(100.0 * (observed - downtime) / observed, 2) if observed else None,
            "downtime_seconds": downtime,
            "incidents": total_incidents,
            "measures": sum(s["measures"] for s in sites),
            "measures_ok": total_ok,
            "measures_failed": sum(s["measures_failed"] for s in sites),
            "inconclusive": sum(s["inconclusive"] for s in sites),
            "incidents_per_1000_ok": round(1000.0 * total_incidents / total_ok, 2) if total_ok else None,
        },
    }


def build_details(store, category, period=None, since=None, until=None, limit=None, offset=0, now=None,
                  result=None):
    """Every check of one category (``"internet"`` or a monitored URL) in a window.

    Rows are newest first. ``limit``/``offset`` select a page (applied in the
    database, so large histories stay cheap) while ``total`` always holds the
    full count. ``result`` filters rows: ``ok``/``fail``/``inconclusive`` for
    a site, ``up``/``down`` for the internet. For the internet category each
    row is one cycle with the result of every reference site, and
    ``reference_summary`` gives each reference site's reliability over the
    whole window.
    """
    window = resolve_period(period, since, until, now)
    kinds = store.site_kinds()
    ref_urls = store.site_urls(KIND_REFERENCE)
    rows = []
    extra = {}
    offset = int(offset or 0)
    result = (result or "").lower() or None
    if category == "internet":
        total = store.count_cycles(ref_urls, window["start"], window["end"], state=result)
        page = store.cycle_states(ref_urls, window["start"], window["end"], limit=limit, offset=offset, state=result)
        order = [c["cycle"] for c in page]
        cycles = {c["cycle"]: {"checked_at": c["checked_at"], "sites": {}} for c in page}
        for r in store.results(urls=ref_urls, cycles=order):
            cycles[r["cycle"]]["sites"][r["url"]] = {
                "url": r["url"], "available": r["available"], "status": r["status"],
                "latency_ms": r["latency_ms"], "error": r["error"],
            }
        for c in order:
            entry = cycles[c]
            sites = [entry["sites"].get(u) for u in ref_urls]
            reachable = sum(1 for x in sites if x and x["available"])
            rows.append({
                "checked_at": entry["checked_at"],
                "result": "UP" if reachable else "DOWN",
                "reachable": reachable,
                "total": sum(1 for x in sites if x),
                "sites": [x or {"url": u, "available": None, "status": 0, "latency_ms": 0, "error": "not checked"}
                          for u, x in zip(ref_urls, sites)],
            })
        agg = store.aggregate(ref_urls, window["start"], window["end"])
        extra = {
            "reference_urls": ref_urls,
            "reference_summary": [agg.get(u, {"url": u, "checks": 0, "ok": 0, "failed": 0, "uptime_pct": 0.0,
                                              "avg_latency_ms": None, "max_latency_ms": None,
                                              "last_checked_at": None}) for u in ref_urls],
        }
    else:
        if category not in kinds:
            raise ValueError("{} is not monitored".format(category))
        is_user = kinds[category] == KIND_USER
        total = store.count(url=category, since=window["start"], until=window["end"], result=result, user_site=is_user)
        raw = store.results(url=category, since=window["start"], until=window["end"], limit=limit, offset=offset,
                            result=result, user_site=is_user)
        inet_since = raw[0]["checked_at"] if raw else window["start"]
        internet_by_cycle, _, _ = _internet_by_cycle(store, ref_urls, inet_since, window["end"])
        for r in raw:
            if r["available"]:
                verdict = "OK"
            elif is_user and _inconclusive(r, internet_by_cycle):
                verdict = "INCONCLUSIVE"
            else:
                verdict = "FAIL"
            inet = internet_by_cycle.get(r.get("cycle"))
            rows.append({
                "checked_at": r["checked_at"], "result": verdict, "status": r["status"],
                "latency_ms": r["latency_ms"], "error": r["error"],
                "internet": None if inet is None else ("UP" if inet else "DOWN"),
            })
    rows.reverse()
    out = {
        "generated_at": _utcnow_iso(),
        "category": category,
        "kind": "internet" if category == "internet" else kinds[category],
        "label": "Internet" if category == "internet" else category,
        "window": window,
        "result": result,
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": rows,
        "truncated": total > offset + len(rows),
    }
    out.update(extra)
    return out


def format_duration(seconds):
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return "{}s".format(seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "{}m {}s".format(minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "{}h {}m".format(hours, minutes)
    days, hours = divmod(hours, 24)
    return "{}d {}h".format(days, hours)


def format_availability_text(report):
    """Render an availability report as plain text."""
    w, sm = report["window"], report["summary"]
    lines = ["imon availability report - {}".format(w["label"])]
    lines.append("window:    {} -> {}".format(w["start"] or "beginning", w["end"]))
    lines.append("generated: {}".format(report["generated_at"]))
    lines.append("")
    inet = report["internet"]
    ipct = "{:.2f}%".format(inet["time_without_incidents_pct"]) if inet["time_without_incidents_pct"] is not None else "n/a"
    lines.append("INTERNET WITHOUT OUTAGES: {}  ({} outage(s), {} of {} cycles OK, downtime {})".format(
        ipct, inet["incidents"], inet["measures_ok"], inet["measures"], format_duration(inet["downtime_seconds"])))
    pct = "{:.2f}%".format(sm["time_without_incidents_pct"]) if sm["time_without_incidents_pct"] is not None else "n/a"
    lines.append("TIME WITHOUT INCIDENTS: {}  (downtime {})".format(pct, format_duration(sm["downtime_seconds"])))
    lines.append("INCIDENTS / MEASURES WITHOUT INCIDENTS: {} / {}  ({} per 1000 OK measures; {} failed, {} inconclusive)".format(
        sm["incidents"], sm["measures_ok"],
        sm["incidents_per_1000_ok"] if sm["incidents_per_1000_ok"] is not None else "n/a",
        sm["measures_failed"], sm["inconclusive"]))
    lines.append("")
    header = "{:<40} {:>12} {:>10} {:>9} {:>8} {:>8} {:>6} {:>12}".format(
        "CATEGORY", "NO-INCIDENT%", "DOWNTIME", "INCIDENTS", "OK", "FAILED", "INCON", "INC/1000 OK")
    lines += [header, "-" * len(header)]
    for s in report["reference"]:
        pct = "{:.2f}%".format(s["time_without_incidents_pct"]) if s["time_without_incidents_pct"] is not None else "n/a"
        url = "  ref: " + (s["url"] if len(s["url"]) <= 33 else s["url"][:30] + "...")
        lines.append("{:<40} {:>12} {:>10} {:>9} {:>8} {:>8} {:>6} {:>12}".format(
            url, pct, format_duration(s["downtime_seconds"]), s["incidents"], s["measures_ok"],
            s["measures_failed"], s["inconclusive"], "-"))
    if not report["sites"]:
        lines.append("No websites monitored yet.")
        return "\n".join(lines)
    for s in report["sites"]:
        url = s["url"] if len(s["url"]) <= 40 else s["url"][:37] + "..."
        pct = "{:.2f}%".format(s["time_without_incidents_pct"]) if s["time_without_incidents_pct"] is not None else "n/a"
        rate = s["incidents_per_1000_ok"] if s["incidents_per_1000_ok"] is not None else "n/a"
        lines.append("{:<40} {:>12} {:>10} {:>9} {:>8} {:>8} {:>6} {:>12}".format(
            url, pct, format_duration(s["downtime_seconds"]), s["incidents"], s["measures_ok"],
            s["measures_failed"], s["inconclusive"], rate))
    return "\n".join(lines)


def _short_host(url):
    host = urlparse(url).netloc or url
    return host[4:] if host.startswith("www.") else host


def format_details_text(detail):
    """Render a details report (every check of one category) as plain text."""
    w = detail["window"]
    lines = ["imon detailed report - {} - {} check(s){}, {} -> {}".format(
        detail["label"], detail["total"], " with result {}".format(detail["result"].upper()) if detail.get("result") else "",
        w["start"] or "beginning", w["end"])]
    if detail["truncated"] or detail["offset"]:
        lines.append("(showing checks {}-{} of {}, newest first)".format(
            detail["offset"] + 1, detail["offset"] + len(detail["rows"]), detail["total"]))
    if detail["category"] == "internet":
        lines.append("Reference site reliability in this window:")
        for x in detail["reference_summary"]:
            avg = "{:.1f} ms".format(x["avg_latency_ms"]) if x["avg_latency_ms"] is not None else "-"
            lines.append("  {:<40} {:>7.2f}%  {:>6} checks  {:>5} failed  avg {}".format(
                x["url"], x["uptime_pct"], x["checks"], x["failed"], avg))
        lines.append("")
        cols = [_short_host(u) for u in detail["reference_urls"]]
        lines.append("{:<25} {:<6} {:>9}  {}".format("TIME", "STATE", "REACHABLE", "  ".join("{:<22}".format(c) for c in cols)))
        for r in detail["rows"]:
            cells = []
            for x in r["sites"]:
                if x["available"]:
                    cells.append("OK {:.0f} ms".format(x["latency_ms"]))
                elif x["available"] is None:
                    cells.append("-")
                else:
                    cells.append("FAIL " + (x["error"] or "HTTP {}".format(x["status"])))
            lines.append("{:<25} {:<6} {:>9}  {}".format(
                r["checked_at"], r["result"], "{}/{}".format(r["reachable"], r["total"]),
                "  ".join("{:<22}".format(c[:22]) for c in cells)))
    else:
        lines.append("{:<25} {:<12} {:>6} {:>10}  {:<18} {}".format("TIME", "RESULT", "HTTP", "LATENCY", "ERROR", "INTERNET"))
        for r in detail["rows"]:
            lines.append("{:<25} {:<12} {:>6} {:>10}  {:<18} {}".format(
                r["checked_at"], r["result"], r["status"] or "-",
                "{:.1f} ms".format(r["latency_ms"]) if r["result"] == "OK" else "-",
                r["error"] or "", r["internet"] or "unknown"))
    if not detail["rows"]:
        lines.append("No checks in this period.")
    return "\n".join(lines)


def _site_table(sites):
    header = "{:<7} {:<40} {:>8} {:>7} {:>6} {:>10} {:>9} {}".format(
        "STATE", "URL", "UPTIME", "CHECKS", "INCON", "AVG ms", "INCIDENTS", "LAST CHECK"
    )
    lines = [header, "-" * len(header)]
    for s in sites:
        url = s["url"] if len(s["url"]) <= 40 else s["url"][:37] + "..."
        avg = "{:.1f}".format(s["avg_latency_ms"]) if s["avg_latency_ms"] is not None else "-"
        lines.append(
            "{:<7} {:<40} {:>7.2f}% {:>7} {:>6} {:>10} {:>9} {}".format(
                s["current_state"], url, s["uptime_pct"], s["checks"], s["inconclusive"], avg,
                len(s["incidents"]), s["last_checked_at"],
            )
        )
    return lines


def format_report_text(report):
    """Render a report dict as plain text for the terminal."""
    lines = []
    summary = report["summary"]
    inet = report["internet"]
    lines.append("imon availability report")
    lines.append("generated: {}".format(report["generated_at"]))
    if report.get("since"):
        lines.append("window:    since {}".format(report["since"]))
    lines.append("")
    lines.append("INTERNET: {} ({}/{} reference sites reachable, {:.2f}% of {} cycles)".format(
        inet["state"], inet["reference_up"], inet["reference_total"], inet["uptime_pct"], inet["cycles"]))
    if report["reference"]:
        lines.extend(_site_table(report["reference"]))
    lines.append("")
    lines.append(
        "YOUR WEBSITES: {sites}  up: {sites_up}  down: {sites_down}  checks: {checks}  "
        "inconclusive: {inconclusive}  overall uptime: {overall_uptime_pct:.2f}%  "
        "open incidents: {open_incidents}".format(**summary)
    )
    if report["sites"]:
        lines.extend(_site_table(report["sites"]))
    elif not report["reference"]:
        lines.append("No results recorded yet. Run `imon watch` or `imon serve` to start monitoring.")
    else:
        lines.append("No user websites monitored yet. Add one with `--url` or from the dashboard.")
    return "\n".join(lines)
