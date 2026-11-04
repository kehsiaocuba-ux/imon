"""Built-in HTTP report server.

Serves a single-page dashboard where everything lives in sections of the same
page (internet status, your websites, incidents, recent checks) plus forms to
add/remove monitored websites. A small script refreshes the sections in place;
without JavaScript the page simply reloads periodically.

Routes:

* ``GET  /``             - dashboard
* ``POST /sites``        - form: ``action=add|remove|replace``, ``url=...``,
                           ``kind=user|reference`` (add), ``new_url=...`` (replace)
                           -> redirect to ``/`` (``/api/sites`` answers JSON)
* ``POST /check``        - check all sites now, or ``url=...`` (plus the reference
                           sites) -> redirect to ``/`` (``/api/check`` answers JSON)
* ``POST /speedtest``    - measure download / upload / ping now -> redirect to ``/``
                           (``/api/speedtest`` answers JSON; ``GET`` returns the latest)
* ``GET  /api/report``   - JSON status report (same data as ``imon report --json``)
* ``GET  /api/reports``  - JSON time-window report: ``?period=last-24h`` or
                           ``?since=...&until=...`` (``?url=`` to filter)
* ``GET  /api/details``  - every check of one category in the window:
                           ``?category=internet`` or ``?category=<url>``
* ``GET  /details.csv``  - the same as CSV (``?category=...&period=...&result=...``)
* ``GET  /details.pdf``  - the same as PDF (same parameters)
* ``GET  /api/results``  - recent raw check results (``?url=``, ``?limit=``, ``?since=``)
* ``GET  /api/sites``    - JSON list of monitored sites
* ``GET  /health``       - liveness probe for the server itself

No dependencies beyond the standard library.
"""

import csv
import html
import io
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 25
STATUS_KEYS = ("speriod", "ssince", "suntil")
REPORT_KEYS = ("period", "since", "until")
DETAIL_KEYS = ("detail", "page", "per", "result")
RECENT_KEYS = ("rpage", "rper")
INCIDENT_KEYS = ("ipage", "iper")
SITES_KEYS = ("wpage", "wper")
ALL_KEYS = STATUS_KEYS + REPORT_KEYS + DETAIL_KEYS + RECENT_KEYS + INCIDENT_KEYS + SITES_KEYS

from imon.monitor import (
    DEFAULT_PERIOD,
    KIND_REFERENCE,
    KIND_USER,
    MIN_REFERENCE_SITES,
    PERIOD_PRESETS,
    build_availability_report,
    build_details,
    build_report,
    check_now,
    RESULT_FILTERS_INTERNET,
    RESULT_FILTERS_SITE,
    format_duration,
    resolve_period,
)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>
<title>imon - availability monitor</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f6f8; color: #222; }}
  header {{ background: #1f2933; color: #fff; padding: 1rem 2rem; display: flex; align-items: baseline; gap: 1rem; }}
  header h1 {{ margin: 0; font-size: 1.3rem; }}
  header .muted {{ color: #cbd2d9; }}
  main {{ padding: 1rem 2rem 2rem; max-width: 1200px; margin: 0 auto; }}
  section {{ background: #fff; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow-x: auto; }}
  section h2 {{ margin: 0 0 .5rem; font-size: 1.1rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .95rem; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #e4e7eb; vertical-align: top; }}
  th {{ background: #f5f7fa; font-weight: 600; }}
  .UP {{ color: #0a7d28; font-weight: 600; }}
  .DOWN {{ color: #b00020; font-weight: 600; }}
  .UNKNOWN, .INCONCLUSIVE {{ color: #a05a00; font-weight: 600; }}
  .muted {{ color: #666; font-size: .9rem; }}
  .banner {{ font-size: 1.5rem; padding: .75rem 1rem; border-radius: 6px; margin-bottom: .75rem; }}
  .banner.UP {{ background: #e3f6e8; }}
  .banner.DOWN {{ background: #fde8ea; }}
  .banner.UNKNOWN {{ background: #fff3e0; }}
  .banner.speed {{ background: #e3efff; font-size: 1.15rem; }}
  .banner.speed b {{ font-size: 1.35rem; }}
  .banner.speed .err {{ color: #b00020; font-size: .95rem; }}
  .stats span {{ margin-right: 1.25rem; }}
  form.inline {{ display: inline; }}
  .add {{ display: flex; gap: .5rem; margin: .75rem 0 .25rem; }}
  .add input[type=url] {{ flex: 1; padding: .5rem; font-size: 1rem; border: 1px solid #cbd2d9; border-radius: 4px; }}
  button {{ padding: .45rem .9rem; font-size: .95rem; border: 0; border-radius: 4px; background: #2563eb; color: #fff; cursor: pointer; }}
  button.danger {{ background: #fff; color: #b00020; border: 1px solid #b00020; padding: .2rem .6rem; font-size: .85rem; }}
  button.small {{ padding: .2rem .6rem; font-size: .85rem; }}
  button.icon {{ background: #fff; color: #2563eb; border: 1px solid #2563eb; padding: .1rem .45rem; font-size: 1rem; line-height: 1.2; }}
  button.icon[disabled], button[disabled] {{ opacity: .5; cursor: wait; }}
  .head {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
  .head h2 {{ margin: 0; }}
  details {{ display: inline-block; vertical-align: middle; }}
  details summary {{ cursor: pointer; list-style: none; display: inline-block; padding: .2rem .6rem; border: 1px solid #2563eb;
    color: #2563eb; border-radius: 4px; font-size: .85rem; background: #fff; }}
  details summary::-webkit-details-marker {{ display: none; }}
  details[open] summary {{ background: #2563eb; color: #fff; }}
  details form {{ display: flex; gap: .3rem; margin-top: .3rem; }}
  td.time {{ white-space: nowrap; }}
  td.url {{ word-break: break-all; min-width: 12rem; }}
  details input[type=url] {{ width: 16rem; padding: .25rem; }}
  .warn {{ background: #fff3e0; padding: .5rem .75rem; border-radius: 4px; margin: .5rem 0; }}
  td.actions {{ white-space: nowrap; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin: .5rem 0; }}
  .chip {{ padding: .3rem .7rem; border-radius: 999px; border: 1px solid #2563eb; color: #2563eb; text-decoration: none; font-size: .9rem; }}
  .chip.active {{ background: #2563eb; color: #fff; }}
  form.period {{ display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: .5rem 0; }}
  form.period input {{ padding: .35rem; border: 1px solid #cbd2d9; border-radius: 4px; }}
  .kpis {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: .75rem 0; }}
  .kpi {{ flex: 1 1 16rem; background: #f5f7fa; border-radius: 6px; padding: .75rem 1rem; }}
  .kpi .value {{ font-size: 1.8rem; font-weight: 600; }}
  .kpi .label {{ color: #666; font-size: .9rem; }}
  .detail {{ margin-top: 1rem; border-top: 2px solid #e4e7eb; padding-top: .75rem; }}
  .detail .head a {{ margin-left: .75rem; }}
  .detail h4 {{ margin: 1rem 0 .25rem; }}
  a.btn {{ display: inline-block; padding: .2rem .6rem; border: 1px solid #2563eb; color: #2563eb; border-radius: 4px; text-decoration: none; font-size: .85rem; white-space: nowrap; }}
  button {{ white-space: nowrap; }}
  .PENDING {{ color: #666; font-weight: 600; }}
  .pager {{ display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: .5rem 0; }}
  .pager .muted {{ margin: 0 .5rem; }}
  a.btn.disabled {{ opacity: .4; pointer-events: none; }}
  section.filter {{ padding: .6rem 1.25rem; }}
  .msg {{ padding: .5rem .75rem; border-radius: 4px; margin: .5rem 0; }}
  .msg.ok {{ background: #e3f6e8; }} .msg.err {{ background: #fde8ea; }}
  footer {{ text-align: center; padding: 1rem; }}
</style>
</head>
<body>
<header>
  <h1>imon</h1>
  <span class="muted">Internet &amp; website availability monitor &middot; monitor: {monitor_state} &middot;
  <span id="updated">updated {generated_at}</span></span>
</header>
<main>
{message}
<section id="add-site">
  <h2>Your websites</h2>
  <form class="add" method="post" action="/sites">
    <input type="hidden" name="action" value="add">
    <input type="url" name="url" placeholder="https://www.example.com" required>
    <button type="submit">Add website</button>
  </form>
  <p class="muted">Added websites are checked from the next monitoring cycle on.</p>
</section>
<div id="content">
{content}
</div>
</main>
<footer class="muted">For scripts: <code>/api/report</code>, <code>/api/results</code>, <code>/api/sites</code></footer>
<script>
(function () {{
  // Long-running dashboard safeguards: one request at a time, request timeout,
  // no refresh in background tabs, no DOM rebuild when nothing changed, back
  // off on errors and do a full page reload every few hours to start clean.
  var every = {refresh} * 1000;
  var content = document.getElementById('content');
  var busy = false, inflight = false, failures = 0;
  var RELOAD_AFTER = 6 * 60 * 60 * 1000, REQUEST_TIMEOUT = 20000;
  var loadedAt = Date.now(), lastHtml = null;
  function stamp() {{
    document.getElementById('updated').textContent = 'updated ' + new Date().toISOString().slice(0, 19) + '+00:00';
  }}
  function fetchTimeout(url, opts, ms) {{
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    if (ctrl) {{ opts.signal = ctrl.signal; setTimeout(function () {{ ctrl.abort(); }}, ms || REQUEST_TIMEOUT); }}
    return fetch(url, opts);
  }}
  function refresh() {{
    if (inflight) return Promise.resolve();
    inflight = true;
    var qs = location.search.replace(/^[?]/, '');  // keeps the selected report period / detail
    return fetchTimeout('/?partial=1' + (qs ? '&' + qs : ''), {{cache: 'no-store'}})
      .then(function (r) {{ if (!r.ok) throw new Error(r.status); return r.text(); }})
      .then(function (t) {{
        failures = 0;
        if (t !== lastHtml) {{ content.innerHTML = t; lastHtml = t; }}
        stamp();
      }})
      .catch(function () {{ failures += 1; }})
      .then(function () {{ inflight = false; }});
  }}
  function editing() {{  // do not wipe an open edit form or a field being typed in
    return busy || content.querySelector('details[open]') || content.contains(document.activeElement) &&
      document.activeElement.tagName === 'INPUT';
  }}
  function notify(text, ok) {{
    var m = document.getElementById('flash');
    m.className = 'msg ' + (ok ? 'ok' : 'err'); m.textContent = text; m.style.display = '';
  }}
  function tick() {{
    if (document.hidden || editing()) return;
    if (Date.now() - loadedAt > RELOAD_AFTER) {{ location.reload(); return; }}
    if (failures && (Date.now() / every) % Math.min(failures + 1, 10) >= 1) return;  // back off
    refresh();
  }}
  setInterval(tick, every);
  document.addEventListener('visibilitychange', function () {{ if (!document.hidden) tick(); }});
  // "Check now" buttons: run the check in the background and refresh in place.
  document.addEventListener('submit', function (ev) {{
    var form = ev.target;
    if (!form.classList.contains('js-check')) return;
    ev.preventDefault();
    busy = true;
    var speed = form.classList.contains('js-speed');
    var buttons = content.querySelectorAll('form.js-check button');
    buttons.forEach(function (b) {{ b.disabled = true; }});
    notify(speed ? 'Measuring internet speed (download, upload, ping)... this takes up to about 30 seconds.' : 'Checking...', true);
    fetchTimeout(speed ? '/api/speedtest' : '/api/check',
                 {{method: 'POST', body: new FormData(form), headers: {{'Accept': 'application/json'}}}}, speed ? 120000 : 0)
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{ notify(d.message, d.ok); }})
      .catch(function () {{ notify(speed ? 'Speed test failed or timed out' : 'Check failed or timed out', false); }})
      .then(function () {{ busy = false; lastHtml = null; return refresh(); }});
  }});
}})();
</script>
</body>
</html>
"""

_CONTENT = """
<section id="status-filter" class="filter">
  <div class="chips"><span class="muted">Status window (Internet connection &amp; Your websites):</span>{status_chips}</div>
  <form class="period" method="get" action="/">
    {status_hidden}
    <label>From <input type="datetime-local" name="ssince" value="{ssince_value}"></label>
    <label>To <input type="datetime-local" name="suntil" value="{suntil_value}"></label>
    <button class="small" type="submit">Apply</button>
    <span class="muted">times in UTC; leave "To" empty for now</span>
  </form>
  {status_error}
</section>

<section id="internet">
  <div class="head"><h2>Internet connection <span class="muted">({status_window})</span></h2>
    <span>
    <form class="inline js-check" method="post" action="/check"><input type="hidden" name="kind" value="reference">
      <button class="small" type="submit" title="Check all reference sites now">&#x21bb; Check reference sites now</button></form>
    <form class="inline js-check js-speed" method="post" action="/speedtest">
      <button class="small" type="submit" title="Measure download, upload and ping (takes up to about 30 s)">&#x21af; Check internet speed</button></form>
    </span>
  </div>
  <div class="banner {istate}">Internet is <b>{istate}</b>
    <span class="muted">&middot; {ref_up}/{ref_total} reference sites reachable &middot; {iuptime:.2f}% of {icycles} cycles</span></div>
  <div class="banner speed" id="speed">{speed_box}</div>
  <p class="muted">The internet counts as UP when at least one reference site answers. While it is DOWN,
  failed checks of your websites are recorded as <span class="INCONCLUSIVE">inconclusive</span> instead of downtime.</p>
  {reference_warning}
  <form class="add" method="post" action="/sites">
    <input type="hidden" name="action" value="add"><input type="hidden" name="kind" value="reference">
    <input type="url" name="url" placeholder="https://www.example.com (highly available site)" required>
    <button type="submit">+ Add reference site</button>
  </form>
  <table>
    <thead><tr><th title="Check now"></th><th>State</th><th>Reference site</th><th>Uptime</th><th>Checks</th><th>Avg latency</th><th>Last status</th><th>Last check (UTC)</th><th></th></tr></thead>
    <tbody>{reference_rows}</tbody>
  </table>
</section>

<section id="sites">
  <div class="head"><h2>Your websites <span class="muted">({status_window})</span></h2>
    <form class="inline js-check" method="post" action="/check">
      <button class="small" type="submit" title="Check all sites now">&#x21bb; Check all now</button></form>
  </div>
  <p class="stats">
    <span>Sites: <b>{sites}</b></span>
    <span class="UP">Up: {sites_up}</span>
    <span class="DOWN">Down: {sites_down}</span>
    <span>Checks: <b>{checks}</b></span>
    <span>Inconclusive: <b>{inconclusive}</b></span>
    <span>Overall uptime: <b>{overall_uptime_fmt}%</b></span>
    <span>Open incidents: <b>{open_incidents}</b></span>
  </p>
  {sites_pager}
  <table>
    <thead><tr><th title="Check now"></th><th>State</th><th>URL</th><th>Uptime</th><th>Checks</th><th>Inconclusive</th><th>Avg latency</th>
    <th>Incidents</th><th>Last status</th><th>Last check (UTC)</th><th></th></tr></thead>
    <tbody>{site_rows}</tbody>
  </table>
  {sites_pager}
</section>

<section id="reports">
  <h2>Reports</h2>
  <div class="chips"><span class="muted">Period:</span>{period_chips}</div>
  <form class="period" method="get" action="/">
    {report_hidden}
    <label>From <input type="datetime-local" name="since" value="{since_value}"></label>
    <label>To <input type="datetime-local" name="until" value="{until_value}"></label>
    <button class="small" type="submit">Apply</button>
    <span class="muted">times in UTC; leave "To" empty for now</span>
  </form>
  {report_error}
  <p class="muted">Window: <b>{window_label}</b> &middot; {window_start} &rarr; {window_end}</p>
  <div class="kpis">
    <div class="kpi"><div class="value">{r_pct}</div>
      <div class="label">Time without incidents (your websites) &middot; downtime {r_downtime}</div></div>
    <div class="kpi"><div class="value">{r_incidents} / {r_ok}</div>
      <div class="label">Incidents / measures without incidents &middot; {r_rate} incidents per 1000 OK measures</div></div>
    <div class="kpi"><div class="value">{i_pct}</div>
      <div class="label">Internet without outages &middot; {i_incidents} outage(s) / {i_ok} cycles OK &middot; downtime {i_downtime}</div></div>
  </div>
  <table>
    <thead><tr><th>Category</th><th>Time without incidents</th><th>Downtime</th><th>Incidents</th>
    <th>Measures OK</th><th>Failed</th><th>Inconclusive</th><th>Incidents / 1000 OK</th><th></th></tr></thead>
    <tbody>{report_rows}</tbody>
  </table>
  {detail_block}
</section>

<section id="incidents">
  <h2>Incidents <span class="muted">({status_window})</span></h2>
  {incidents_pager}
  <table>
    <thead><tr><th>Site</th><th>Started (UTC)</th><th>Ended (UTC)</th><th>Failed checks</th><th>Error</th><th>Status</th></tr></thead>
    <tbody>{incident_rows}</tbody>
  </table>
  {incidents_pager}
</section>

<section id="recent">
  <h2>Recent checks</h2>
  {recent_pager}
  <table>
    <thead><tr><th>Time (UTC)</th><th>URL</th><th>Result</th><th>HTTP status</th><th>Latency</th><th>Error</th></tr></thead>
    <tbody>{recent_rows}</tbody>
  </table>
  {recent_pager}
</section>
"""


def _fmt_latency(ms):
    return "{:.1f} ms".format(ms) if ms is not None else "-"


def _hidden(name, value):
    return "<input type=\"hidden\" name=\"{}\" value=\"{}\">".format(name, html.escape(value, quote=True))


def _check_button(url):
    return (
        "<form class=\"inline js-check\" method=\"post\" action=\"/check\">{}"
        "<button class=\"icon\" type=\"submit\" title=\"Check now\">&#x21bb;</button></form>"
    ).format(_hidden("url", url))


def _remove_button(url):
    return (
        "<form class=\"inline\" method=\"post\" action=\"/sites\">{}{}"
        "<button class=\"danger\" type=\"submit\">Remove</button></form>"
    ).format(_hidden("action", "remove"), _hidden("url", url))


def _edit_form(url):
    return (
        "<details><summary>Edit</summary><form method=\"post\" action=\"/sites\">{}{}"
        "<input type=\"url\" name=\"new_url\" value=\"{}\" required>"
        "<button class=\"small\" type=\"submit\">Save</button></form></details>"
    ).format(_hidden("action", "replace"), _hidden("url", url), html.escape(url, quote=True))


def _site_row(s, registered):
    last_status = s["last_status"] if s["last_status"] else (s["last_error"] or "error")
    is_ref = s["kind"] == KIND_REFERENCE
    cells = [
        "<td>{}</td>".format(_check_button(s["url"]) if registered else ""),
        "<td class=\"{0}\">{0}</td>".format(s["current_state"]),
        "<td class=\"url\">{}</td>".format(html.escape(s["url"])),
        "<td>{:.2f}%</td>".format(s["uptime_pct"]),
        "<td>{}</td>".format(s["checks"]),
    ]
    if not is_ref:
        cells.append("<td>{}</td>".format(s["inconclusive"]))
    cells.append("<td>{}</td>".format(_fmt_latency(s["avg_latency_ms"])))
    if not is_ref:
        cells.append("<td>{}</td>".format(len(s["incidents"])))
    cells += [
        "<td>{}</td>".format(html.escape(str(last_status))),
        "<td class=\"time\">{}</td>".format(html.escape(s["last_checked_at"])),
    ]
    if registered:
        cells.append("<td class=\"actions\">{} {}</td>".format(_edit_form(s["url"]), _remove_button(s["url"])))
    else:
        cells.append("<td class=\"muted\">not monitored</td>")
    return "<tr>" + "".join(cells) + "</tr>"


def _pending_row(site, ncols):
    return (
        "<tr><td>{}</td><td class=\"UNKNOWN\">PENDING</td><td>{}</td><td colspan=\"{}\" class=\"muted\">waiting for first check</td>"
        "<td class=\"actions\">{} {}</td></tr>"
    ).format(_check_button(site["url"]), html.escape(site["url"]), ncols, _edit_form(site["url"]), _remove_button(site["url"]))


def _local_value(iso):
    """ISO timestamp -> value for <input type=datetime-local> (UTC, minute precision)."""
    return iso[:16] if iso else ""


def _pct(value):
    return "{:.2f}%".format(value) if value is not None else "n/a"


def _window_qs(window, keys=REPORT_KEYS):
    """Query-string parameters reproducing a selected window (report or status keys)."""
    period_key, since_key, until_key = keys
    if window["period"] == "custom":
        return {since_key: _local_value(window["start"]), until_key: _local_value(window["end"])}
    return {period_key: window["period"]}


def _current_params(qs):
    """The navigation parameters of the current page (one value each)."""
    out = {}
    for k in ALL_KEYS:
        v = qs.get(k, [""])[0] if qs else ""
        if v:
            out[k] = v
    return out


def _link(params, drop=(), anchor=None, **overrides):
    """Dashboard link keeping the current parameters, minus ``drop``, plus ``overrides``."""
    merged = {k: v for k, v in params.items() if k not in drop}
    merged.update({k: v for k, v in overrides.items() if v not in (None, "")})
    href = "/?" + urlencode(merged) if merged else "/"
    return html.escape(href + ("#" + anchor if anchor else ""))


def _hidden_fields(params, keys):
    return "".join(_hidden(k, params[k]) for k in keys if params.get(k))


def _chips(params, keys, active):
    period_key = keys[0]
    drop = keys + ("page",)  # a new window starts on page 1 of an open detail
    chips = "".join(
        "<a class=\"chip{}\" href=\"{}\">{}</a>".format(
            " active" if key == active else "", _link(params, drop=drop, **{period_key: key}), html.escape(label))
        for key, label in PERIOD_PRESETS
    )
    if active == "custom":
        chips += "<span class=\"chip active\">Custom</span>"
    return chips


def _render_status_filter(window, error, params):
    return {
        "status_chips": _chips(params, STATUS_KEYS, window["period"] if window else "all"),
        "status_hidden": _hidden_fields(params, REPORT_KEYS + DETAIL_KEYS),
        "ssince_value": _local_value(window["start"]) if window and window["period"] == "custom" else "",
        "suntil_value": _local_value(window["end"]) if window and window["period"] == "custom" else "",
        "status_error": "<div class=\"msg err\">{}</div>".format(html.escape(error)) if error else "",
        "status_window": html.escape(window["label"]) if window else "all time",
    }


def _report_row(x, window, label, detail, params):
    pct = x["time_without_incidents_pct"]
    if x["measures"] == 0 and pct is None:
        state, pct_text = "PENDING", "no measures yet"
    elif pct == 100:
        state, pct_text = "UP", _pct(pct)
    elif x["incidents"]:
        state, pct_text = "DOWN", _pct(pct)
    else:
        state, pct_text = "UNKNOWN", _pct(pct)
    cat = x["category"] if x["category"] == "internet" else x["url"]
    if detail == cat:
        button = "<a class=\"btn\" href=\"{}\">Close details</a>".format(_link(params, drop=DETAIL_KEYS, anchor="reports"))
    else:
        button = "<a class=\"btn\" href=\"{}\">Details</a>".format(_link(params, drop=DETAIL_KEYS, anchor="reports", detail=cat))
    return (
        "<tr><td>{}</td><td class=\"{}\">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>"
    ).format(
        html.escape(label), state, pct_text, format_duration(x["downtime_seconds"]), x["incidents"],
        x["measures_ok"], x["measures_failed"], x["inconclusive"],
        x["incidents_per_1000_ok"] if x["incidents_per_1000_ok"] is not None else "n/a", button,
    )


def _short_host(url):
    host = urlparse(url).netloc or url
    return host[4:] if host.startswith("www.") else host


def _ref_cell(x):
    if x["available"] is None:
        return "<td class=\"muted\">-</td>"
    if x["available"]:
        return "<td class=\"UP\" title=\"HTTP {}\">OK {:.0f} ms</td>".format(x["status"], x["latency_ms"])
    return "<td class=\"DOWN\">FAIL <span class=\"muted\">{}</span></td>".format(
        html.escape(x["error"] or "HTTP {}".format(x["status"])))


def _page_of(items, qs, page_key, per_key, default_per=DEFAULT_PAGE_SIZE):
    """Slice ``items`` according to the paging parameters in ``qs``."""
    per = _page_size(qs.get(per_key, [""])[0]) if qs and qs.get(per_key) else default_per
    page = _page_number(qs.get(page_key, [""])[0]) if qs else 1
    pages = max(1, (len(items) + per - 1) // per)
    page = min(page, pages)
    offset = (page - 1) * per
    return items[offset:offset + per], offset, per


def _pager(total, offset, per, shown, params, page_key="page", per_key="per", anchor="detail"):
    """First/Prev/Next/Last navigation plus page-size chips for a paginated table."""
    per = per or DEFAULT_PAGE_SIZE
    pages = max(1, (total + per - 1) // per)
    page = offset // per + 1
    first, last = (offset + 1, offset + shown) if shown else (0, 0)

    def btn(label, target, enabled):
        if not enabled:
            return "<a class=\"btn disabled\">{}</a>".format(label)
        return "<a class=\"btn\" href=\"{}\">{}</a>".format(_link(params, anchor=anchor, **{page_key: str(target)}), label)

    nav = btn("&laquo; First", 1, page > 1) + btn("&lsaquo; Prev", page - 1, page > 1)
    nav += "<span class=\"muted\">page {} of {} &middot; checks {}-{} of {}</span>".format(page, pages, first, last, total)
    nav += btn("Next &rsaquo;", page + 1, page < pages) + btn("Last &raquo;", pages, page < pages)
    sizes = "".join(
        "<a class=\"chip{}\" href=\"{}\">{}</a>".format(
            " active" if n == per else "", _link(params, anchor=anchor, **{per_key: str(n), page_key: "1"}), n)
        for n in PAGE_SIZES
    )
    return "<div class=\"pager\">{}<span class=\"muted\">per page:</span>{}</div>".format(nav, sizes)


def _result_chips(detail, params):
    """Filter-by-result chips for a detailed report."""
    options = RESULT_FILTERS_INTERNET if detail["category"] == "internet" else RESULT_FILTERS_SITE
    if detail.get("kind") == KIND_REFERENCE:
        options = tuple(o for o in options if o != "inconclusive")
    active = detail.get("result") or ""
    chips = "<a class=\"chip{}\" href=\"{}\">All</a>".format(
        "" if active else " active", _link(params, drop=("page", "result"), anchor="detail"))
    chips += "".join(
        "<a class=\"chip{}\" href=\"{}\">{}</a>".format(
            " active" if active == o else "", _link(params, drop=("page",), anchor="detail", result=o), o.upper())
        for o in options
    )
    return "<div class=\"chips\"><span class=\"muted\">Show:</span>{}</div>".format(chips)


def _render_detail(detail, params):
    """The 'detailed report' block: one page of the checks of one category in the window."""
    w = detail["window"]
    csv_params = dict(_window_qs(w), category=detail["category"])
    if detail.get("result"):
        csv_params["result"] = detail["result"]
    csv_href = "/details.csv?" + html.escape(urlencode(csv_params))
    pdf_href = "/details.pdf?" + html.escape(urlencode(csv_params))
    close_href = _link(params, drop=DETAIL_KEYS, anchor="reports")
    what = "{} {} check(s)".format(detail["total"], detail["result"].upper()) if detail.get("result") else \
        "{} check(s)".format(detail["total"])
    head = (
        "<div class=\"head\"><h3>Detailed report: {} <span class=\"muted\">({}, {} &rarr; {})</span></h3>"
        "<span><a class=\"btn\" href=\"{}\">Download CSV ({} checks)</a><a class=\"btn\" href=\"{}\">Download PDF</a>"
        "<a class=\"btn\" href=\"{}\">Close</a></span></div>"
    ).format(html.escape(detail["label"]), what, html.escape(w["start"] or "beginning"),
             html.escape(w["end"]), csv_href, detail["total"], pdf_href, close_href)
    pager = _result_chips(detail, params) + _pager(detail["total"], detail["offset"], detail["limit"],
                                                   len(detail["rows"]), params)
    note = ""
    if detail["category"] == "internet":
        summary = "".join(
            "<tr><td>{}</td><td class=\"{}\">{:.2f}%</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(x["url"]), "UP" if x["checks"] and x["failed"] == 0 else ("DOWN" if x["failed"] else "UNKNOWN"),
                x["uptime_pct"], x["checks"], x["ok"], x["failed"], _fmt_latency(x["avg_latency_ms"]),
                _fmt_latency(x["max_latency_ms"]),
            )
            for x in detail["reference_summary"]
        ) or "<tr><td colspan=\"7\" class=\"muted\">No reference sites configured.</td></tr>"
        note += (
            "<h4>Reference site reliability in this window</h4>"
            "<p class=\"muted\">The internet counts as UP when at least one reference site answers; a reference site "
            "that fails often should be substituted (Edit in the Internet connection section).</p>"
            "<table><thead><tr><th>Reference site</th><th>Success rate</th><th>Checks</th><th>OK</th><th>Failed</th>"
            "<th>Avg latency</th><th>Max latency</th></tr></thead><tbody>{}</tbody></table>"
            "<h4>Every check</h4>"
        ).format(summary)
        thead = "<tr><th>Time (UTC)</th><th>Internet</th><th>Reachable</th>{}</tr>".format(
            "".join("<th>{}</th>".format(html.escape(_short_host(u))) for u in detail["reference_urls"]))
        rows = "".join(
            "<tr><td>{}</td><td class=\"{}\">{}</td><td>{}/{}</td>{}</tr>".format(
                html.escape(r["checked_at"]), r["result"], r["result"], r["reachable"], r["total"],
                "".join(_ref_cell(x) for x in r["sites"]),
            )
            for r in detail["rows"]
        )
    else:
        thead = "<tr><th>Time (UTC)</th><th>Result</th><th>HTTP status</th><th>Latency</th><th>Error</th><th>Internet</th></tr>"
        rows = "".join(
            "<tr><td>{}</td><td class=\"{}\">{}</td><td>{}</td><td>{}</td><td>{}</td><td class=\"{}\">{}</td></tr>".format(
                html.escape(r["checked_at"]),
                {"OK": "UP", "FAIL": "DOWN"}.get(r["result"], "INCONCLUSIVE"), r["result"],
                r["status"] or "-", _fmt_latency(r["latency_ms"]) if r["result"] == "OK" else "-",
                html.escape(r["error"] or ""), r["internet"] or "", r["internet"] or "unknown",
            )
            for r in detail["rows"]
        )
    if not rows:
        rows = "<tr><td colspan=\"9\" class=\"muted\">No checks in this period{}.</td></tr>".format(
            " with this result" if detail.get("result") else "")
    bottom = _pager(detail["total"], detail["offset"], detail["limit"], len(detail["rows"]), params)
    return "<div class=\"detail\" id=\"detail\">{}{}{}<table><thead>{}</thead><tbody>{}</tbody></table>{}</div>".format(
        head, note, pager, thead, rows, bottom)


def _render_reports(avail, error=None, detail=None, params=None):
    params = params or {}
    period = avail["window"]["period"] if avail else DEFAULT_PERIOD
    fields = {
        "period_chips": _chips(params, REPORT_KEYS, period),
        "report_hidden": _hidden_fields(params, STATUS_KEYS),
        "report_error": "<div class=\"msg err\">{}</div>".format(html.escape(error)) if error else "",
        "since_value": "", "until_value": "", "window_label": "-", "window_start": "-", "window_end": "-",
        "r_pct": "n/a", "r_downtime": "0s", "r_incidents": 0, "r_ok": 0, "r_rate": "n/a",
        "i_pct": "n/a", "i_downtime": "0s", "i_incidents": 0, "i_ok": 0,
        "report_rows": "<tr><td colspan=\"9\" class=\"muted\">No websites monitored yet.</td></tr>",
        "detail_block": "",
    }
    if avail:
        w, sm, inet = avail["window"], avail["summary"], avail["internet"]
        detail_cat = detail["category"] if detail else None
        fields.update({
            "since_value": _local_value(w["start"]) if w["period"] == "custom" else "",
            "until_value": _local_value(w["end"]) if w["period"] == "custom" else "",
            "window_label": html.escape(w["label"]),
            "window_start": html.escape(w["start"] or "beginning"),
            "window_end": html.escape(w["end"]),
            "r_pct": _pct(sm["time_without_incidents_pct"]),
            "r_downtime": format_duration(sm["downtime_seconds"]),
            "r_incidents": sm["incidents"], "r_ok": sm["measures_ok"],
            "r_rate": sm["incidents_per_1000_ok"] if sm["incidents_per_1000_ok"] is not None else "n/a",
            "i_pct": _pct(inet["time_without_incidents_pct"]),
            "i_downtime": format_duration(inet["downtime_seconds"]),
            "i_incidents": inet["incidents"], "i_ok": inet["measures_ok"],
        })
        rows = _report_row(inet, w, "Internet ({} reference sites)".format(inet["reference_total"]), detail_cat, params)
        rows += "".join(_report_row(x, w, "\u21b3 reference: " + x["url"], detail_cat, params) for x in avail["reference"])
        rows += "".join(_report_row(x, w, x["url"], detail_cat, params) for x in avail["sites"])
        fields["report_rows"] = rows
        if detail:
            fields["detail_block"] = _render_detail(detail, params)
    return fields


def render_speed_box(result):
    """Contents of the blue 'internet speed' box: always the latest measurement."""
    if not result:
        return ("Internet speed: <b>not measured yet</b> <span class=\"muted\">&middot; press "
                "\"Check internet speed\" to measure download, upload and ping</span>")
    def val(key, unit):
        v = result.get(key)
        return "<b>{} {}</b>".format(v, unit) if v is not None else "<span class=\"err\">failed</span>"
    parts = "Download {} &middot; Upload {} &middot; Ping {}".format(
        val("download_mbps", "Mbps"), val("upload_mbps", "Mbps"), val("ping_ms", "ms"))
    errors = result.get("errors") or {}
    err_html = ""
    if errors:
        err_html = " <span class=\"err\">({})</span>".format(html.escape("; ".join(
            "{}: {}".format(k, v) for k, v in errors.items())))
    return "Internet speed: {} <span class=\"muted\">&middot; measured {}</span>{}".format(
        parts, html.escape(result.get("measured_at", "")), err_html)


def render_content(report, registered, recent, avail=None, avail_error=None, detail=None,
                   status_window=None, status_error=None, qs=None, speed=None):
    """Render the refreshable sections of the dashboard."""
    params = _current_params(qs)
    reg_urls = {r["url"] for r in registered}
    ref_sites = [r for r in registered if r["kind"] == KIND_REFERENCE]
    user_sites = [r for r in registered if r["kind"] != KIND_REFERENCE]

    reported = {s["url"] for s in report["reference"]}
    ref_rows = "".join(_site_row(s, s["url"] in reg_urls) for s in report["reference"])
    ref_rows += "".join(_pending_row(r, 5) for r in ref_sites if r["url"] not in reported)
    if not ref_rows:
        ref_rows = "<tr><td colspan=\"9\" class=\"muted\">No reference sites. Add at least {} highly available sites.</td></tr>".format(MIN_REFERENCE_SITES)
    if len(ref_sites) < MIN_REFERENCE_SITES:
        reference_warning = (
            "<div class=\"warn\">Only {} reference site(s) configured. Keep at least {} so a single site "
            "going down cannot be mistaken for an internet outage.</div>".format(len(ref_sites), MIN_REFERENCE_SITES)
        )
    else:
        reference_warning = ""

    reported = {s["url"] for s in report["sites"]}
    site_items = [("site", s) for s in report["sites"]]
    site_items += [("pending", r) for r in user_sites if r["url"] not in reported]
    page_items, offset, per = _page_of(site_items, qs, "wpage", "wper")
    site_rows = "".join(_site_row(x, x["url"] in reg_urls) if kind == "site" else _pending_row(x, 7)
                        for kind, x in page_items)
    if not site_rows:
        site_rows = "<tr><td colspan=\"11\" class=\"muted\">No websites added yet. Use the form above to add one.</td></tr>"
    sites_pager = _pager(len(site_items), offset, per, len(page_items), params,
                         page_key="wpage", per_key="wper", anchor="sites") if len(site_items) > PAGE_SIZES[0] else ""

    incidents = []
    for s in report["sites"]:
        for i in s["incidents"]:
            incidents.append((i["start"], s["url"], i))
    incidents.sort(reverse=True)
    inc_page, inc_offset, inc_per = _page_of(incidents, qs, "ipage", "iper")
    incidents_pager = _pager(len(incidents), inc_offset, inc_per, len(inc_page), params,
                             page_key="ipage", per_key="iper", anchor="incidents") if len(incidents) > PAGE_SIZES[0] else ""
    inc_rows = "".join(
        "<tr><td class=\"url\">{}</td><td class=\"time\">{}</td><td class=\"time\">{}</td><td>{}</td><td>{}</td><td class=\"{}\">{}</td></tr>".format(
            html.escape(url), html.escape(i["start"]), "-" if i["ongoing"] else html.escape(i["end"]),
            i["checks"], html.escape(str(i["error"])), "DOWN" if i["ongoing"] else "UP",
            "ongoing" if i["ongoing"] else "resolved",
        )
        for _, url, i in inc_page
    ) or "<tr><td colspan=\"6\" class=\"muted\">No incidents recorded.</td></tr>"

    rows = recent["rows"] if isinstance(recent, dict) else recent
    recent_rows = "".join(
        "<tr><td class=\"time\">{}</td><td class=\"url\">{}</td><td class=\"{}\">{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(r["checked_at"]), html.escape(r["url"]),
            "UP" if r["available"] else "DOWN", "OK" if r["available"] else "FAIL",
            r["status"] or "-", _fmt_latency(r["latency_ms"]) if r["available"] else "-",
            html.escape(r["error"] or ""),
        )
        for r in reversed(rows)
    ) or "<tr><td colspan=\"6\" class=\"muted\">No checks recorded yet.</td></tr>"
    if isinstance(recent, dict):
        recent_pager = _pager(recent["total"], recent["offset"], recent["limit"], len(rows), params,
                              page_key="rpage", per_key="rper", anchor="recent")
    else:
        recent_pager = ""

    inet = report["internet"]
    return _CONTENT.format(
        istate=inet["state"], ref_up=inet["reference_up"], ref_total=inet["reference_total"],
        speed_box=render_speed_box(speed),
        iuptime=inet["uptime_pct"], icycles=inet["cycles"],
        reference_rows=ref_rows, reference_warning=reference_warning,
        site_rows=site_rows, sites_pager=sites_pager, incident_rows=inc_rows, incidents_pager=incidents_pager,
        recent_rows=recent_rows, recent_pager=recent_pager,
        overall_uptime_fmt="{:.2f}".format(report["summary"]["overall_uptime_pct"]),
        **dict(report["summary"], **dict(_render_status_filter(status_window, status_error, params),
                                         **_render_reports(avail, avail_error, detail, params)))
    )


def render_page(report, registered, recent, refresh=30, monitor_running=None, message=None, message_ok=True,
                avail=None, avail_error=None, detail=None, status_window=None, status_error=None, qs=None,
                speed=None):
    if monitor_running is None:
        monitor_state = "external"
    else:
        monitor_state = "active" if monitor_running else "stopped"
    if message:
        msg_html = "<div id=\"flash\" class=\"msg {}\">{}</div>".format("ok" if message_ok else "err", html.escape(message))
    else:
        msg_html = "<div id=\"flash\" class=\"msg\" style=\"display:none\"></div>"
    return _PAGE.format(
        refresh=int(refresh),
        generated_at=html.escape(report["generated_at"]),
        monitor_state=monitor_state,
        message=msg_html,
        content=render_content(report, registered, recent, avail, avail_error, detail, status_window,
                               status_error, qs, speed),
    )


def _parse_multipart(body, ctype):
    """Minimal multipart/form-data parser for the small text fields we use."""
    boundary = None
    for part in ctype.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
    form = {}
    if not boundary:
        return form
    for chunk in body.split("--" + boundary)[1:]:
        if chunk.strip() in ("", "--"):
            continue
        head, _, value = chunk.partition("\r\n\r\n")
        name = None
        for h in head.split("\r\n"):
            if h.lower().startswith("content-disposition"):
                for p in h.split(";"):
                    p = p.strip()
                    if p.startswith("name="):
                        name = p[len("name="):].strip('"')
        if name is not None:
            form.setdefault(name, []).append(value[:-2] if value.endswith("\r\n") else value)
    return form


def _details_csv(detail):
    out = io.StringIO()
    w = csv.writer(out)
    if detail["category"] == "internet":
        urls = detail["reference_urls"]
        w.writerow(["checked_at", "internet", "reference_reachable", "reference_total"]
                   + [u + " result" for u in urls] + [u + " latency_ms" for u in urls])
        for r in detail["rows"]:
            w.writerow([r["checked_at"], r["result"], r["reachable"], r["total"]]
                       + ["OK" if x["available"] else ("" if x["available"] is None else (x["error"] or "HTTP {}".format(x["status"])))
                          for x in r["sites"]]
                       + ["{:.1f}".format(x["latency_ms"]) if x["available"] else "" for x in r["sites"]])
    else:
        w.writerow(["checked_at", "url", "result", "http_status", "latency_ms", "error", "internet"])
        for r in detail["rows"]:
            w.writerow([r["checked_at"], detail["category"], r["result"], r["status"],
                        "{:.1f}".format(r["latency_ms"]) if r["result"] == "OK" else "", r["error"] or "",
                        r["internet"] or ""])
    return out.getvalue()


def _page_number(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _page_size(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return n if n in PAGE_SIZES else DEFAULT_PAGE_SIZE


class _TTLCache:
    """Tiny time-based cache so several open dashboards share one computation.

    Entries expire after ``ttl`` seconds and the whole cache is dropped on any
    write (site changes, manual checks) and whenever the monitor records a
    new cycle (detected through the results count).
    """

    def __init__(self, ttl=5.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._items = {}

    def get(self, key, compute):
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = compute()
        with self._lock:
            self._items[key] = (now + self.ttl, value)
        return value

    def clear(self):
        with self._lock:
            self._items.clear()


def _make_handler(store, active_monitor, refresh, recent_limit, timeout, status_period, cache, speed_options=None):
    class ReportHandler(BaseHTTPRequestHandler):
        server_version = "imon"

        def log_message(self, fmt, *args):  # keep the CLI output quiet
            pass

        def _send(self, code, body, content_type):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code, payload):
            self._send(code, json.dumps(payload, indent=2), "application/json")

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _running(self):
            return active_monitor.running if active_monitor else None

        def _cached(self, key, compute):
            # a new result in the store invalidates everything
            return cache.get(("n", store.count()) + key, compute)

        def _status(self, qs):
            """Status report for the status window (``speriod``/``ssince``/``suntil``)."""
            now = datetime.now(timezone.utc)
            window = resolve_period(qs.get("speriod", [None])[0] or status_period,
                                    qs.get("ssince", [""])[0] or None, qs.get("suntil", [""])[0] or None, now=now)
            since = window["start"]
            # a window ending "now" has no upper bound (so a check made this second counts)
            until = None if window["end"] >= now.isoformat(timespec="seconds") else window["end"]
            # key on the minute so a sliding window does not defeat the cache
            key = ("status", (since or "")[:16], until)
            return window, self._cached(key, lambda: build_report(store, since=since, until=until))

        def _availability(self, qs):
            period = qs.get("period", [None])[0] or None
            since = qs.get("since", [""])[0] or None
            until = qs.get("until", [""])[0] or None
            urls = tuple(qs.get("url") or ())
            return self._cached(("avail", period, since, until, urls), lambda: build_availability_report(
                store, period=period, since=since, until=until, urls=list(urls) or None))

        def _details(self, qs, category, limit, offset=0, result=None):
            if not category:
                raise ValueError("category is required: 'internet' or a monitored URL")
            period = qs.get("period", [None])[0] or None
            since = qs.get("since", [""])[0] or None
            until = qs.get("until", [""])[0] or None
            return self._cached(("details", category, period, since, until, limit, offset, result),
                                lambda: build_details(store, category, period=period, since=since, until=until,
                                                      limit=limit, offset=offset, result=result))

        def _recent(self, qs):
            """One page of the newest checks (all sites) for the Recent checks section."""
            per = _page_size(qs.get("rper", [""])[0]) if qs.get("rper") else recent_limit
            page = _page_number(qs.get("rpage", [""])[0])
            total = store.count()
            pages = max(1, (total + per - 1) // per)
            page = min(page, pages)
            offset = (page - 1) * per
            rows = self._cached(("recent", per, offset), lambda: store.results(limit=per, offset=offset))
            return {"rows": rows, "total": total, "offset": offset, "limit": per}

        def _back_to_dashboard(self, params):
            """Redirect to the dashboard keeping the report period the user had selected."""
            ref = urlparse(self.headers.get("Referer", ""))
            keep = parse_qs(ref.query)
            for key in ALL_KEYS:
                if keep.get(key, [""])[0]:
                    params[key] = keep[key][0]
            self._redirect("/?" + urlencode(params))

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            since = qs.get("since", [None])[0]

            if parsed.path == "/":
                status_window, status_error = None, None
                try:
                    status_window, report = self._status(qs)
                except ValueError as exc:
                    status_error = str(exc)
                    status_window, report = self._status({})
                registered = store.sites()
                recent = self._recent(qs)
                avail, avail_error, detail = None, None, None
                try:
                    avail = self._availability(qs)
                    if qs.get("detail", [""])[0]:
                        per = _page_size(qs.get("per", [""])[0])
                        page = _page_number(qs.get("page", [""])[0])
                        result = qs.get("result", [""])[0] or None
                        detail = self._details(qs, qs["detail"][0], per, (page - 1) * per, result)
                        if not detail["rows"] and detail["total"] and page > 1:  # past the end: last page
                            last = (detail["total"] + per - 1) // per
                            detail = self._details(qs, qs["detail"][0], per, (last - 1) * per, result)
                except ValueError as exc:
                    avail_error = str(exc)
                speed = store.latest_speedtest()
                if qs.get("partial"):
                    self._send(200, render_content(report, registered, recent, avail, avail_error, detail,
                                                   status_window, status_error, qs, speed), "text/html")
                else:
                    msg = qs.get("msg", [None])[0]
                    ok = qs.get("ok", ["1"])[0] == "1"
                    self._send(200, render_page(report, registered, recent, refresh, self._running(), msg, ok,
                                                avail, avail_error, detail, status_window, status_error, qs, speed),
                               "text/html")
            elif parsed.path == "/api/reports":
                try:
                    self._json(200, self._availability(qs))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif parsed.path == "/api/details":
                try:
                    limit = int(qs.get("limit", ["0"])[0]) or None
                    offset = int(qs.get("offset", ["0"])[0] or 0)
                    self._json(200, self._details(qs, qs.get("category", [""])[0], limit, offset,
                                                  qs.get("result", [""])[0] or None))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif parsed.path in ("/details.csv", "/details.pdf"):
                try:
                    detail = self._details(qs, qs.get("category", [""])[0], None, 0, qs.get("result", [""])[0] or None)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                name = "internet" if detail["category"] == "internet" else urlparse(detail["category"]).netloc or "site"
                filename = "imon-{}-{}{}".format(name, detail["window"]["period"],
                                                 "-" + detail["result"] if detail.get("result") else "")
                if parsed.path.endswith(".pdf"):
                    from imon.pdf import render_details_pdf
                    body, ctype, filename = render_details_pdf(detail), "application/pdf", filename + ".pdf"
                else:
                    body, ctype, filename = _details_csv(detail).encode("utf-8"), "text/csv; charset=utf-8", filename + ".csv"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", "attachment; filename=\"{}\"".format(filename))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            elif parsed.path == "/api/report":
                try:
                    if since or qs.get("until", [""])[0]:
                        report = dict(build_report(store, since=since, until=qs.get("until", [""])[0] or None))
                    else:
                        report = dict(self._status(qs)[1])
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                report["monitor_running"] = self._running()
                self._json(200, report)
            elif parsed.path == "/api/results":
                try:
                    limit = int(qs.get("limit", ["100"])[0])
                except ValueError:
                    return self._json(400, {"error": "limit must be an integer"})
                url = qs.get("url", [None])[0]
                self._json(200, {"results": store.results(url=url, since=since, limit=limit)})
            elif parsed.path == "/api/sites":
                self._json(200, {"sites": store.sites()})
            elif parsed.path == "/api/speedtest":
                self._json(200, {"latest": store.latest_speedtest(), "history": store.speedtests(20)})
            elif parsed.path == "/health":
                self._json(200, {"status": "ok", "monitor_running": self._running()})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            ctype = self.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    form = {k: [str(v)] for k, v in json.loads(body or "{}").items()}
                except ValueError:
                    return self._json(400, {"error": "invalid JSON"})
            elif "multipart/form-data" in ctype:
                form = _parse_multipart(body, ctype)
            else:
                form = parse_qs(body)
            wants_json = parsed.path.startswith("/api/") or "json" in ctype or "json" in self.headers.get("Accept", "")

            if parsed.path in ("/sites", "/api/sites"):
                ok, message, extra = self._manage_sites(form)
            elif parsed.path in ("/check", "/api/check"):
                ok, message, extra = self._check(form)
            elif parsed.path in ("/speedtest", "/api/speedtest"):
                ok, message, extra = self._speedtest()
            else:
                return self._json(404, {"error": "not found"})
            cache.clear()

            if wants_json:
                payload = {"ok": ok, "message": message}
                payload.update(extra)
                self._json(200 if ok else 400, payload)
            else:
                self._back_to_dashboard({"msg": message, "ok": "1" if ok else "0"})

        def _manage_sites(self, form):
            action = form.get("action", ["add"])[0]
            url = form.get("url", [""])[0].strip()
            kind = form.get("kind", [KIND_USER])[0]
            ok, message = True, ""
            try:
                if action == "add":
                    label = "reference site" if kind == KIND_REFERENCE else "website"
                    added = store.add_site(url, kind)
                    message = "Added {} {}".format(label, url) if added else "{} is already monitored".format(url)
                elif action == "remove":
                    kinds = store.site_kinds()
                    if url not in kinds:
                        ok, message = False, "{} is not monitored".format(url)
                    elif kinds[url] == KIND_REFERENCE and len(store.sites(KIND_REFERENCE)) <= 1:
                        ok, message = False, "Cannot remove the last reference site - add a replacement first"
                    else:
                        store.remove_site(url)
                        message = "Removed {}".format(url)
                elif action == "replace":
                    new_url = form.get("new_url", [""])[0].strip()
                    if store.replace_site(url, new_url):
                        message = "Replaced {} with {}".format(url, new_url)
                    else:
                        ok, message = False, "{} is not monitored".format(url)
                else:
                    ok, message = False, "Unknown action"
            except ValueError as exc:
                ok, message = False, str(exc)
            return ok, message, {"sites": store.sites()}

        def _speedtest(self):
            from imon.speedtest import format_speed, run_exclusive
            opts = dict(speed_options or {})
            opts.setdefault("ping_hosts", store.site_urls(KIND_REFERENCE))
            result = run_exclusive(**opts)
            if result is None:
                return False, "A speed test is already running - please wait for it to finish", {"result": None}
            store.record_speedtest(result)
            message = "Internet speed: {} (measured {})".format(format_speed(result), result["measured_at"])
            if result["errors"]:
                message += " - " + "; ".join("{}: {}".format(k, v) for k, v in result["errors"].items())
            return result["ok"], message, {"result": result}

        def _check(self, form):
            url = form.get("url", [""])[0].strip()
            kind = form.get("kind", [None])[0]
            if url:
                if url not in store.site_kinds():
                    return False, "{} is not monitored".format(url), {"results": []}
                results = check_now(store, [url], timeout=timeout)
            elif kind == KIND_REFERENCE:
                results = check_now(store, [], timeout=timeout)
            else:
                results = check_now(store, timeout=timeout)
            if not results:
                return False, "Nothing to check - add a site first", {"results": []}
            kinds = store.site_kinds()
            ref = [r for r in results if kinds.get(r["url"]) == KIND_REFERENCE]
            internet_up = any(r["available"] for r in ref) if ref else None
            down = [r["url"] for r in results if not r["available"]]
            message = "Checked {} site(s) at {}: {} up, {} down".format(
                len(results), results[-1]["checked_at"], len(results) - len(down), len(down))
            if internet_up is False:
                message += " - Internet is DOWN, failures of your websites are inconclusive"
            elif down:
                message += " ({})".format(", ".join(down))
            return True, message, {"results": results, "internet_up": internet_up}

    return ReportHandler


class ReportServer:
    """Serve the dashboard/API for ``store`` on ``host:port``.

    If ``active_monitor`` is given its running state is reported and it is
    stopped when the server shuts down. ``port=0`` picks a free port (tests).
    ``speed_options`` are keyword arguments for ``imon.speedtest.run_speed_test``
    (download/upload URLs and sizes). ``status_period`` is the default window of the status tables (a preset
    name, "all" by default); the user can change it on the page. Detailed
    reports are paginated (25/50/100 per page). Computed pages are cached for
    ``cache_ttl`` seconds (dropped on any write or new result).
    """

    def __init__(self, store, host="127.0.0.1", port=8080, active_monitor=None, refresh=30, recent_limit=DEFAULT_PAGE_SIZE,
                 timeout=5, status_period="all", cache_ttl=5.0, speed_options=None):
        self.store = store
        self.active_monitor = active_monitor
        self.cache = _TTLCache(cache_ttl)
        self._httpd = ThreadingHTTPServer(
            (host, port), _make_handler(store, active_monitor, refresh, recent_limit, timeout, status_period,
                                        self.cache, speed_options)
        )
        self._httpd.daemon_threads = True
        self._thread = None

    @property
    def host(self):
        return self._httpd.server_address[0]

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def url(self):
        return "http://{}:{}/".format(self.host, self.port)

    def serve_forever(self):
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, name="imon-report-server", daemon=True)
        self._thread.start()
        return self._thread

    def shutdown(self):
        self._httpd.shutdown()
        if self._thread:
            self._thread.join(5)
        if self.active_monitor:
            self.active_monitor.stop()
