"""Internet speed measurement: download, upload and ping.

* download - streams ``download_bytes`` from ``download_url`` and measures the
  throughput from the first byte received (a time cap keeps slow links from
  blocking the dashboard for too long);
* upload   - POSTs ``upload_bytes`` of random data to ``upload_url``;
* ping     - TCP connect time to the reference sites (ICMP would need root),
  best of a few attempts per host; the reported ping is the best host.

The defaults use Cloudflare's public speed-test endpoints, which are built for
exactly this; any URL that serves a large response / accepts a POST body works.
"""

import os
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

DEFAULT_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
DEFAULT_UPLOAD_URL = "https://speed.cloudflare.com/__up"
DEFAULT_DOWNLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 15  # per direction

_lock = threading.Lock()


def _mbps(num_bytes, seconds):
    return round(num_bytes * 8 / seconds / 1e6, 2) if seconds > 0 else None


def measure_download(url, num_bytes=DEFAULT_DOWNLOAD_BYTES, timeout=10, max_seconds=MAX_SECONDS):
    url = url.format(bytes=num_bytes)
    requested = time.monotonic()
    resp = requests.get(url, stream=True, timeout=timeout, headers={"Cache-Control": "no-cache"})
    resp.raise_for_status()
    first = None
    received = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        now = time.monotonic()
        if first is None:
            first = now
        received += len(chunk)
        if received >= num_bytes or now - first >= max_seconds:
            break
    done = time.monotonic()
    resp.close()
    if received == 0:
        raise RuntimeError("no data received")
    # Throughput from the first byte excludes connection latency; when the
    # transfer is too short for that to be meaningful, use the whole request.
    elapsed = done - first
    if elapsed < 0.05:
        elapsed = done - requested
    return {"bytes": received, "seconds": round(elapsed, 4), "mbps": _mbps(received, elapsed)}


def measure_upload(url, num_bytes=DEFAULT_UPLOAD_BYTES, timeout=10, max_seconds=MAX_SECONDS):
    payload = os.urandom(num_bytes)
    started = time.monotonic()
    resp = requests.post(url, data=payload, timeout=(timeout, max(timeout, max_seconds)),
                         headers={"Content-Type": "application/octet-stream"})
    elapsed = time.monotonic() - started
    resp.raise_for_status()
    return {"bytes": num_bytes, "seconds": round(elapsed, 4), "mbps": _mbps(num_bytes, elapsed)}


def measure_ping(hosts, attempts=3, timeout=3):
    """TCP connect latency per host (ms); ``None`` when the host is unreachable."""
    results = []
    for url in hosts:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.hostname
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        best = None
        for _ in range(attempts):
            t0 = time.monotonic()
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    ms = (time.monotonic() - t0) * 1000
            except OSError:
                continue
            best = ms if best is None else min(best, ms)
        results.append({"host": host, "ms": round(best, 1) if best is not None else None})
    return results


def run_speed_test(download_url=DEFAULT_DOWNLOAD_URL, upload_url=DEFAULT_UPLOAD_URL,
                   download_bytes=DEFAULT_DOWNLOAD_BYTES, upload_bytes=DEFAULT_UPLOAD_BYTES,
                   ping_hosts=(), timeout=10):
    """Run all three measurements; failures are reported per part, not raised.

    Returns ``{"measured_at", "download_mbps", "upload_mbps", "ping_ms",
    "download", "upload", "ping", "errors", "ok"}``.
    """
    result = {"measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "download_mbps": None, "upload_mbps": None, "ping_ms": None,
              "download": None, "upload": None, "ping": [], "errors": {}}
    t0 = time.monotonic()
    try:
        result["ping"] = measure_ping(ping_hosts, timeout=min(timeout, 3))
        reachable = [p["ms"] for p in result["ping"] if p["ms"] is not None]
        if reachable:
            result["ping_ms"] = min(reachable)
        elif ping_hosts:
            result["errors"]["ping"] = "no reference site reachable"
    except Exception as exc:  # pragma: no cover - defensive
        result["errors"]["ping"] = exc.__class__.__name__
    try:
        result["download"] = measure_download(download_url, download_bytes, timeout)
        result["download_mbps"] = result["download"]["mbps"]
    except Exception as exc:
        result["errors"]["download"] = "{}: {}".format(exc.__class__.__name__, exc)[:200]
    try:
        result["upload"] = measure_upload(upload_url, upload_bytes, timeout)
        result["upload_mbps"] = result["upload"]["mbps"]
    except Exception as exc:
        result["errors"]["upload"] = "{}: {}".format(exc.__class__.__name__, exc)[:200]
    result["duration_seconds"] = round(time.monotonic() - t0, 1)
    result["ok"] = not result["errors"]
    return result


def run_exclusive(*args, **kwargs):
    """Run a speed test unless one is already running (returns None in that case)."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        return run_speed_test(*args, **kwargs)
    finally:
        _lock.release()


def format_speed(result):
    """One-line human summary, e.g. 'Download 93.4 Mbps · Upload 21.0 Mbps · Ping 12.3 ms'."""
    if not result:
        return "not measured yet"
    parts = []
    parts.append("Download {} Mbps".format(result["download_mbps"]) if result.get("download_mbps") is not None
                 else "Download failed")
    parts.append("Upload {} Mbps".format(result["upload_mbps"]) if result.get("upload_mbps") is not None
                 else "Upload failed")
    parts.append("Ping {} ms".format(result["ping_ms"]) if result.get("ping_ms") is not None else "Ping failed")
    return " \u00b7 ".join(parts)
