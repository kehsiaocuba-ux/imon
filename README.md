# imon

Continuous internet and website availability monitor with a built-in report tool.

* **Active monitoring** – `imon watch` / `imon serve` poll your sites on a fixed
  interval and record every result (status, latency, error) to a SQLite history.
* **Trustworthy data** – three highly available *reference sites* (Google,
  Cloudflare, Microsoft by default) are always checked. The internet counts as
  **UP** when at least one of them answers. If all of them fail, failed checks of
  *your* websites in that cycle are recorded as **inconclusive** instead of
  downtime, so a local outage never shows up as downtime of your sites.
* **Manage sites from the dashboard** – add, edit (substitute) and remove both
  your own websites and the reference sites; the monitor picks up changes on
  its next cycle. The last reference site cannot be removed and the dashboard
  warns when fewer than three are configured.
* **Check now** – a ↻ button next to every site and "Check all now" /
  "Check reference sites now" buttons run a check immediately; results are
  recorded like any other cycle.
* **Reports over any time window** – % of time without incidents and
  incidents vs. measures without incidents for your websites, with presets
  (last hour, today, yesterday, last 24 h, 7 days, 30 days, all time) or a
  custom from/to interval.
* **Accessible reports** – `imon report` prints everything in the terminal or as
  JSON; `imon serve` shows a single-page dashboard with sections for internet
  status, your websites, reports, incidents and recent checks that refreshes
  in place.

## Install

```bash
pip install .
```

Only dependency is `requests`; the report server uses the standard library.

## Usage

```bash
# one-shot check: is the internet up, and are my sites up?
imon check                                   # reference sites only
imon check --url https://example.com         # plus your website(s)

# actively monitor every 30s, recording history to ~/.imon/history.db
imon watch --url https://example.com --interval 30

# show the report from recorded history (status + time-window measures)
imon report                      # last 24 hours (default)
imon report --period last-30d    # last-hour, today, yesterday, last-24h, last-7d, last-30d, all
imon report --since 2024-03-01T08:00 --until 2024-03-02   # custom interval (UTC)
imon report --json --url https://example.com
imon report --period last-7d --detail https://example.com   # every check of one category
imon report --period today --detail internet

# monitor AND serve the dashboard over HTTP
imon serve --url https://example.com --interval 30 --port 8080
#   http://127.0.0.1:8080/   <- open this in a browser
```

The dashboard is one page with these sections:

| Section | Contents |
|---|---|
| Your websites (top) | **Add website** form – new sites are checked from the next cycle |
| Status window | period presets + custom from/to applied to the two status sections below (default: all time) |
| Internet connection | UP/DOWN banner, **Check internet speed** button with a blue box showing the latest download / upload / ping measurement, **+ Add reference site** form, **Check reference sites now**, reference table with ↻ / Edit / Remove per site |
| Your websites | **Check all now**; per site: ↻ check now, state, uptime %, checks, inconclusive checks, latency, incidents, Edit / Remove |
| Reports | period presets + custom from/to; **time without incidents %** and **incidents / measures without incidents** for the Internet connection and every website (listed as soon as it is added); **Details** per category lists every check in the window, with CSV download |
| Incidents | every outage of your sites: start, end, failed checks, error, ongoing/resolved |
| Recent checks | the latest raw results (`--recent N`, default 50) |

Sites you add/edit/remove are stored in the database, so they survive restarts,
and the active monitor reads the list at the start of every cycle. *Edit*
substitutes a URL in place (useful when a reference site goes away for good).
A ↻ check of one of your websites also checks the reference sites in the same
cycle, so the result can be classified as real or inconclusive.

For scripts the same data is available as JSON: `/api/report`,
`/api/reports?period=last-7d` (or `?since=&until=`, `&url=`),
`/api/details?category=internet|<url>&period=…&result=…&limit=…&offset=…`
(and `/details.csv` / `/details.pdf` with the same parameters), `/api/results`
(`?url=&limit=&since=`), `/api/sites` (GET list; POST
`{"action":"add","url":...,"kind":"user|reference"}`, `{"action":"remove","url":...}`,
`{"action":"replace","url":...,"new_url":...}`), `/api/check` (POST `{}` for all,
`{"url":...}` for one site, `{"kind":"reference"}` for the reference sites) and
`/health`.

```bash
# measure internet speed now (download, upload, ping); --record makes the dashboard show it
imon speed --record
```

Options: `--host 0.0.0.0` (`serve`) makes the dashboard reachable from other
machines; `--db PATH` chooses where history is stored; `--reference URL`
(repeatable) replaces the default reference sites the first time a database is
used. Exit codes of `check` / `report`: `0` all good, `1` one of your sites is
down, `2` the internet is down.

Running `imon --url ...` with no subcommand still behaves like `imon check`.

## Internet speed

**Check internet speed** measures, from the machine running imon:

* download - streams 10 MB from `https://speed.cloudflare.com/__down` and
  measures the throughput from the first byte (capped at 15 s);
* upload - POSTs 2 MB to `https://speed.cloudflare.com/__up`;
* ping - TCP connect time to each reference site, best of three; the shown
  ping is the best host (ICMP ping would need root privileges).

The result is stored, so the blue box always shows the latest measurement,
even after a restart; only one test runs at a time. Endpoints and sizes can
be changed (`--speed-download-url`, `--speed-upload-url`, `--speed-download-mb`,
`--speed-upload-mb` on `serve` and `speed`); any URL serving a large response
or accepting a POST body works, e.g. a file on your own server. From the CLI:
`imon speed [--record] [--json]`; API: `GET/POST /api/speedtest`.

## How the internet state is decided

Every monitoring cycle checks all reference sites and all of your sites and
tags the results with a cycle id. For a cycle:

* internet **UP** if at least one reference site responded with HTTP < 400;
* internet **DOWN** if none did.

A failed check of one of *your* sites is counted as downtime (and opens an
incident) only in cycles where the internet was UP. Otherwise it is
**inconclusive**: shown separately, excluded from uptime %, and the site's
current state becomes UNKNOWN until the next conclusive check. Reference sites
are never marked inconclusive – their own history is what tells you how
reliable your connection is.

## Reports: the two measures

Both are computed over the selected window, for the **Internet** category
(one measure per reference cycle; an outage is a cycle where no reference site
answered) and for each of your websites:

1. **Time without incidents (%)** – time-based, not check-based. Each
   conclusive check's state holds until the next conclusive check; the
   timeline is clipped to the window (a check just before the window start
   supplies the initial state, an ongoing incident runs until the window end).
   `100 × (observed − time in incidents) / observed`. Downtime is shown next
   to it.
2. **Incidents / measures without incidents** – the number of incidents that
   overlap the window (including ones that started earlier) next to the number
   of successful measures, plus the rate per 1000 OK measures. Failed and
   inconclusive measures are listed too.

Inconclusive checks (internet down) never count as incidents or downtime in
either measure. All percentages are shown with two decimals. All times are
UTC; custom intervals without a timezone are taken as UTC, and an empty "To"
means now.

The Reports table also has one row per reference site (under the Internet
row), so a reference site that keeps failing stands out and can be substituted.

**Details** (button on every row) lists every check of that category in the
window on the same page: for a website, time, result (OK / FAIL /
INCONCLUSIVE), HTTP status, latency, error and the internet state during that
check; for the Internet, a reliability summary per reference site (success
rate, checks, failures, latency) followed by every cycle with one column per
reference site. The list is paginated - 25 per page by default, switchable to
50 or 100, newest first - so the page stays small, and can be filtered by
result (OK / FAIL / INCONCLUSIVE for a site, UP / DOWN for the internet) to
jump straight to the interesting checks; the **CSV** and **PDF** downloads
and `imon report --detail <internet|url> [--result ...] [--pdf FILE]`
contain all matching checks (the PDF is generated without any extra
dependency: landscape A4, table header repeated on every page, colour-coded
results, page numbers). The Recent checks, Incidents and Your websites
sections are paginated the same way (the pager appears once a table has more
than 25 rows), so the page stays manageable with many monitored sites.
`imon report --pdf FILE` without `--detail` writes the availability report.

The status sections (Internet connection, Your websites) have their own
window selector with the same presets and custom interval; it defaults to
all time (`--status-period` changes the default) and is independent of the
Reports window, so you can e.g. watch today's status while reporting on the
last 30 days. Both selections, and an open detail view, are kept in the page
URL and survive refreshes and actions.

## Keeping the dashboard light over long periods

The dashboard is meant to stay open for days, so both sides are bounded:

* a per-cycle summary table (reference sites checked / reachable) is kept
  up to date on every check, so the internet state, cycle counts and page
  offsets never need to scan the full history (existing databases get it
  rebuilt once, automatically);
* detailed reports are paginated; computed pages are cached for a few
  seconds server-side so several open tabs share one computation; heavy
  aggregations run inside SQLite and only state transitions are walked in
  Python;
* the page script refreshes only when the tab is visible, never overlaps
  requests (20 s timeout), skips DOM updates when nothing changed, backs off
  after errors, and reloads the page completely every 6 hours.

## Report contents

For each site: kind (reference/user), current state (UP/DOWN/UNKNOWN), checks,
inconclusive checks, uptime %, average / min / max latency, last HTTP status or
error, and incidents (contiguous runs of conclusive failures with start/end,
error and whether still ongoing). Plus the internet state and an overall summary.

## Library use

```python
from imon.monitor import ActiveMonitor, ResultStore, build_report
from imon.server import ReportServer

store = ResultStore("history.db")
store.ensure_reference_sites()        # Google / Cloudflare / Microsoft
monitor = ActiveMonitor(["https://example.com"], store, interval=60)
monitor.start()                       # background thread
store.add_site("https://blog.example.com")   # picked up on the next cycle
ReportServer(store, port=8080, active_monitor=monitor).serve_forever()
```

## Development

```bash
pip install -e . pytest
pytest
```

Tests run fully offline against a local HTTP server.

## License

imon is free software released under the MIT License. Copyright (c) 2026 Alejandro Castaneda. See the `LICENSE` file.
