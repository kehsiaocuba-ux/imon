import argparse
import json
import sys
import uuid

from imon.monitor import (
    DEFAULT_DB_PATH,
    DEFAULT_REFERENCE_SITES,
    KIND_USER,
    DEFAULT_PERIOD,
    PERIOD_PRESETS,
    ActiveMonitor,
    InternetMonitor,
    ResultStore,
    build_availability_report,
    build_details,
    build_report,
    format_availability_text,
    format_details_text,
    format_report_text,
)


def _print_results(results, prefix=""):
    for r in results:
        status = "UP" if r["available"] else "DOWN"
        detail = "{:.2f}ms".format(r["latency_ms"]) if r["available"] else (r.get("error") or "HTTP {}".format(r["status"]))
        print("{}[{}] {} - {}".format(prefix, status, r["url"], detail), flush=True)


def _add_url_args(parser):
    parser.add_argument("--url", action="append", help="Your website to monitor (repeatable)")
    parser.add_argument(
        "--reference", action="append",
        help="Highly available site used to detect whether the internet is up (repeatable; "
             "default: {})".format(", ".join(DEFAULT_REFERENCE_SITES)),
    )
    parser.add_argument("--timeout", type=float, default=5, help="Per-request timeout in seconds (default: 5)")


def _add_store_args(parser):
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite history file (default: %(default)s)")


def _add_speed_args(parser):
    from imon.speedtest import DEFAULT_DOWNLOAD_BYTES, DEFAULT_DOWNLOAD_URL, DEFAULT_UPLOAD_BYTES, DEFAULT_UPLOAD_URL
    parser.add_argument("--speed-download-url", default=DEFAULT_DOWNLOAD_URL,
                        help="URL serving a large response for the download test ('{bytes}' is replaced by the size)")
    parser.add_argument("--speed-upload-url", default=DEFAULT_UPLOAD_URL, help="URL accepting a POST body for the upload test")
    parser.add_argument("--speed-download-mb", type=float, default=DEFAULT_DOWNLOAD_BYTES / 1048576,
                        help="Download test size in MB (default: %(default)s)")
    parser.add_argument("--speed-upload-mb", type=float, default=DEFAULT_UPLOAD_BYTES / 1048576,
                        help="Upload test size in MB (default: %(default)s)")


def _speed_options(args):
    return {
        "download_url": args.speed_download_url, "upload_url": args.speed_upload_url,
        "download_bytes": int(args.speed_download_mb * 1048576), "upload_bytes": int(args.speed_upload_mb * 1048576),
    }


def cmd_speed(args):
    from imon.speedtest import format_speed, run_speed_test

    store = ResultStore(args.db)
    try:
        result = run_speed_test(ping_hosts=store.site_urls("reference") or DEFAULT_REFERENCE_SITES, **_speed_options(args))
        if args.record:
            store.record_speedtest(result)
    finally:
        store.close()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("INTERNET SPEED: {}  (measured {}, {}s)".format(format_speed(result), result["measured_at"],
                                                              result["duration_seconds"]))
        for p in result["ping"]:
            print("  ping {:<40} {}".format(p["host"], "{} ms".format(p["ms"]) if p["ms"] is not None else "unreachable"))
        for k, v in result["errors"].items():
            print("  {} failed: {}".format(k, v))
    return 0 if result["ok"] else 1


def build_parser():
    parser = argparse.ArgumentParser(prog="imon", description="imon: Internet and webpage availability monitor")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Run a single availability check and exit")
    _add_url_args(p_check)
    p_check.add_argument("--record", action="store_true", help="Also save the results to the history database")
    _add_store_args(p_check)

    p_watch = sub.add_parser("watch", help="Actively monitor URLs on an interval and record history")
    _add_url_args(p_watch)
    _add_store_args(p_watch)
    p_watch.add_argument("--interval", type=float, default=60, help="Seconds between checks (default: 60)")
    p_watch.add_argument("--cycles", type=int, default=None, help="Stop after this many cycles (default: run forever)")
    p_watch.add_argument("--quiet", action="store_true", help="Do not print each result")

    p_report = sub.add_parser("report", help="Show an availability report from recorded history")
    _add_store_args(p_report)
    p_report.add_argument("--period", choices=[k for k, _ in PERIOD_PRESETS], default=None,
                          help="Preset time window (default: {})".format(DEFAULT_PERIOD))
    p_report.add_argument("--since", help="Custom window start, ISO-8601 (naive = UTC), e.g. 2024-03-01T08:00")
    p_report.add_argument("--until", help="Custom window end, ISO-8601 (default: now)")
    p_report.add_argument("--url", action="append", help="Limit the report to these URLs")
    p_report.add_argument("--detail", metavar="CATEGORY",
                          help="List every check of one category in the window: 'internet' or a monitored URL")
    p_report.add_argument("--result", choices=["ok", "fail", "inconclusive", "up", "down"],
                          help="With --detail: only checks with this result (ok/fail/inconclusive for a site, up/down for internet)")
    p_report.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    p_report.add_argument("--pdf", metavar="FILE",
                          help="Write a PDF (of the detailed report with --detail, otherwise of the availability report)")

    p_serve = sub.add_parser("serve", help="Monitor continuously and serve the report over HTTP")
    _add_url_args(p_serve)
    _add_store_args(p_serve)
    p_serve.add_argument("--interval", type=float, default=60, help="Seconds between checks (default: 60)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Address to bind (default: 127.0.0.1; use 0.0.0.0 for LAN access)")
    p_serve.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    p_serve.add_argument("--refresh", type=int, default=30, help="Dashboard auto-refresh seconds (default: 30)")
    p_serve.add_argument("--recent", type=int, default=25, choices=[25, 50, 100],
                         help="Recent checks per page on the dashboard (default: 25)")
    p_serve.add_argument("--status-period", choices=[k for k, _ in PERIOD_PRESETS], default="all",
                         help="Default window of the status tables on the dashboard (default: all)")
    p_serve.add_argument("--no-monitor", action="store_true", help="Only serve existing history, do not run checks")
    _add_speed_args(p_serve)

    p_speed = sub.add_parser("speed", help="Measure internet speed now (download, upload, ping)")
    _add_speed_args(p_speed)
    _add_store_args(p_speed)
    p_speed.add_argument("--json", action="store_true", help="Emit JSON")
    p_speed.add_argument("--record", action="store_true", help="Save the result so the dashboard shows it")

    return parser


def _setup_store(args):
    """Open the history DB, seed reference sites and register --url sites."""
    store = ResultStore(args.db)
    store.ensure_reference_sites(args.reference)
    for u in args.url or []:
        store.add_site(u, KIND_USER)
    return store


def cmd_check(args):
    reference = args.reference or DEFAULT_REFERENCE_SITES
    user = args.url or []
    results = InternetMonitor(reference + user, timeout=args.timeout).run_checks()
    ref_results, user_results = results[: len(reference)], results[len(reference):]
    internet_up = any(r["available"] for r in ref_results)
    if args.record:
        store = _setup_store(args)
        store.record(results, cycle=uuid.uuid4().hex)
        store.close()
    print("INTERNET: {} ({}/{} reference sites reachable)".format(
        "UP" if internet_up else "DOWN", sum(r["available"] for r in ref_results), len(ref_results)))
    _print_results(ref_results, prefix="  ")
    if user_results:
        print("YOUR WEBSITES:" + ("" if internet_up else " (internet is down - results are inconclusive)"))
        _print_results(user_results, prefix="  ")
    if not internet_up:
        return 2
    return 0 if all(r["available"] for r in user_results) else 1


def _describe(store, args):
    ref, user = store.site_urls("reference"), store.site_urls(KIND_USER)
    print("imon: {} reference site(s), {} of your website(s), checking every {:g}s, history -> {}".format(
        len(ref), len(user), args.interval, args.db), flush=True)


def cmd_watch(args):
    store = _setup_store(args)
    on_results = None if args.quiet else (lambda results: _print_results(results, prefix="{} ".format(results[0]["checked_at"])))
    monitor = ActiveMonitor(store=store, interval=args.interval, timeout=args.timeout, on_results=on_results)
    _describe(store, args)
    try:
        monitor.run(max_cycles=args.cycles)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        store.close()
    return 0


def cmd_report(args):
    store = ResultStore(args.db)
    try:
        availability = build_availability_report(store, period=args.period, since=args.since, until=args.until,
                                                 urls=args.url)
        report = build_report(store, since=availability["window"]["start"], urls=args.url)
        detail = build_details(store, args.detail, period=args.period, since=args.since, until=args.until,
                               result=args.result) if args.detail else None
    except ValueError as exc:
        print("imon: {}".format(exc), file=sys.stderr)
        return 2
    finally:
        store.close()
    report["availability"] = availability
    if detail:
        report["details"] = detail
    if args.pdf:
        from imon.pdf import render_availability_pdf, render_details_pdf
        with open(args.pdf, "wb") as fh:
            fh.write(render_details_pdf(detail) if detail else render_availability_pdf(availability))
        print("imon: wrote {}".format(args.pdf), file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report_text(report))
        print()
        print(format_availability_text(availability))
        if detail:
            print()
            print(format_details_text(detail))
    if report["internet"]["state"] == "DOWN":
        return 2
    return 0 if report["summary"]["sites_down"] == 0 else 1


def cmd_serve(args):
    from imon.server import ReportServer

    store = _setup_store(args)
    monitor = None
    if not args.no_monitor:
        monitor = ActiveMonitor(store=store, interval=args.interval, timeout=args.timeout)
        monitor.start()
        _describe(store, args)
    server = ReportServer(store, host=args.host, port=args.port, active_monitor=monitor,
                          refresh=args.refresh, recent_limit=args.recent, timeout=args.timeout,
                          status_period=args.status_period, speed_options=_speed_options(args))
    print("imon: dashboard available at {}".format(server.url), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor:
            monitor.stop()
        store.close()
    return 0


def main(argv=None):
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    # Backwards compatibility: `imon --url X` behaves like `imon check --url X`.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["check"] + argv
    args = parser.parse_args(argv)
    handlers = {"check": cmd_check, "watch": cmd_watch, "report": cmd_report, "serve": cmd_serve, "speed": cmd_speed}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
