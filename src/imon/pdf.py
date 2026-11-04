"""Minimal PDF writer (standard library only) and the detailed-report PDF.

Only what the report needs: landscape A4 pages, the built-in Helvetica /
Courier fonts (no embedding required), text, lines, filled rectangles and
page numbers. Table rows use Courier so column widths are exact.
"""

from imon.monitor import format_duration

PAGE_W, PAGE_H = 842.0, 595.0  # A4 landscape, points
MARGIN = 36.0
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier", "F4": "Courier-Bold"}
COURIER_CHAR = 0.6  # Courier glyph width in em

GREEN, RED, ORANGE, GREY, BLACK = (0.04, 0.49, 0.16), (0.69, 0.0, 0.13), (0.63, 0.35, 0.0), (0.4, 0.4, 0.4), (0, 0, 0)
HEADER_BG, RULE = (0.96, 0.97, 0.98), (0.85, 0.85, 0.85)


def _pdf_text(value):
    """Encode text for a PDF literal string (WinAnsi, unknown chars -> '?')."""
    data = str(value).encode("cp1252", "replace")
    data = data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return data.replace(b"\r", b" ").replace(b"\n", b" ")


class PDFDocument:
    def __init__(self, title="imon report"):
        self.title = title
        self.pages = []  # list of byte-chunk lists
        self._ops = None
        self.y = 0.0

    # -- drawing ---------------------------------------------------------------

    def new_page(self):
        self._ops = []
        self.pages.append(self._ops)
        self.y = PAGE_H - MARGIN

    def text(self, x, y, s, font="F1", size=9, color=BLACK):
        self._ops.append(
            b"BT %s rg /%s %s Tf %.1f %.1f Td (%s) Tj ET\n"
            % (_color(color), font.encode(), _num(size), x, y, _pdf_text(s))
        )

    def line(self, x1, y1, x2, y2, color=RULE, width=0.5):
        self._ops.append(b"%s RG %s w %.1f %.1f m %.1f %.1f l S\n" % (_color(color), _num(width), x1, y1, x2, y2))

    def rect(self, x, y, w, h, color=HEADER_BG):
        self._ops.append(b"%s rg %.1f %.1f %.1f %.1f re f\n" % (_color(color), x, y, w, h))

    def space(self, points):
        self.y -= points

    def paragraph(self, s, font="F1", size=9, color=BLACK, leading=None):
        """Write a line (wrapped conservatively on the usable width), moving down."""
        leading = leading or size * 1.35
        max_chars = int((PAGE_W - 2 * MARGIN) / (size * 0.5))  # Helvetica average ~0.5em
        words, line = str(s).split(" "), ""
        for w in words:
            if line and len(line) + 1 + len(w) > max_chars:
                self._write_line(line, font, size, color, leading)
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            self._write_line(line, font, size, color, leading)

    def _write_line(self, line, font, size, color, leading):
        if self.y - leading < MARGIN + 20:
            self.new_page()
        self.y -= leading
        self.text(MARGIN, self.y, line, font, size, color)

    def table(self, columns, rows, size=8, row_h=12.0):
        """Draw a table with header repeated on every page.

        ``columns``: list of (title, width_pt). ``rows``: list of lists of
        cells; a cell is a string or (string, color). Text is clipped to the
        column width with an ellipsis.
        """
        x0 = MARGIN
        total_w = sum(w for _, w in columns)

        def header():
            if self.y - row_h < MARGIN + 20:
                self.new_page()
            self.y -= row_h
            self.rect(x0, self.y - 3, total_w, row_h, HEADER_BG)
            x = x0
            for title, w in columns:
                self.text(x + 2, self.y, _clip(title, w, size), "F4", size)
                x += w
            self.line(x0, self.y - 3, x0 + total_w, self.y - 3)

        header()
        for row in rows:
            if self.y - row_h < MARGIN + 20:
                self.new_page()
                header()
            self.y -= row_h
            x = x0
            for (title, w), cell in zip(columns, row):
                if isinstance(cell, tuple):
                    value, color = cell
                else:
                    value, color = cell, BLACK
                self.text(x + 2, self.y, _clip(value, w, size), "F3", size, color)
                x += w
            self.line(x0, self.y - 3, x0 + total_w, self.y - 3)

    # -- output ----------------------------------------------------------------

    def build(self):
        objects = []  # list of bytes, object number = index + 1

        def add(body):
            objects.append(body)
            return len(objects)

        font_ids = {key: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name.encode())
                    for key, name in FONTS.items()}
        resources = b"<< /Font << " + b" ".join(b"/%s %d 0 R" % (k.encode(), n) for k, n in font_ids.items()) + b" >> >>"
        pages_id = len(objects) + 1 + 2 * len(self.pages)  # reserve: content+page per page, then Pages
        page_ids = []
        total = len(self.pages)
        for i, ops in enumerate(self.pages):
            footer = b"BT %s rg /F1 8 Tf %.1f %.1f Td (%s) Tj ET\n" % (
                _color(GREY), PAGE_W - MARGIN - 60, MARGIN - 14, _pdf_text("Page {} of {}".format(i + 1, total)))
            stream = b"".join(ops) + footer
            content_id = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            page_ids.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R /Resources %s >>"
                % (pages_id, _num(PAGE_W), _num(PAGE_H), content_id, resources)))
        assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % p for p in page_ids) +
                   b"] /Count %d >>" % len(page_ids)) == pages_id
        catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        info_id = add(b"<< /Title (%s) /Producer (imon) >>" % _pdf_text(self.title))

        out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
        offsets = []
        pos = len(out[0])
        for i, body in enumerate(objects):
            offsets.append(pos)
            chunk = b"%d 0 obj\n" % (i + 1) + body + b"\nendobj\n"
            out.append(chunk)
            pos += len(chunk)
        xref = b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        xref += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        trailer = b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, catalog_id, info_id, pos)
        return b"".join(out) + xref + trailer


def _num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".").encode() or b"0"


def _color(c):
    return b"%s %s %s" % (_num(c[0]), _num(c[1]), _num(c[2]))


def _clip(value, width, size):
    """Clip text to fit a Courier column of ``width`` points."""
    value = str(value)
    max_chars = max(1, int((width - 4) / (size * COURIER_CHAR)))
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)] + "\u2026"


def _result_color(result):
    return {"OK": GREEN, "UP": GREEN, "FAIL": RED, "DOWN": RED, "INCONCLUSIVE": ORANGE}.get(result, BLACK)


def _short_host(url):
    from urllib.parse import urlparse
    host = urlparse(url).netloc or url
    return host[4:] if host.startswith("www.") else host


def render_details_pdf(detail):
    """Build the PDF of a detailed report (see ``imon.monitor.build_details``)."""
    w = detail["window"]
    doc = PDFDocument("imon detailed report - {}".format(detail["label"]))
    doc.new_page()
    doc.paragraph("imon detailed report: {}".format(detail["label"]), "F2", 15, leading=18)
    what = "{} {} check(s)".format(detail["total"], detail["result"].upper()) if detail.get("result") else \
        "{} check(s)".format(detail["total"])
    doc.paragraph("Window: {}  ({} -> {})".format(w["label"], w["start"] or "beginning", w["end"]), "F1", 9, GREY)
    doc.paragraph("{}, newest first. Generated {} (all times UTC).".format(what, detail["generated_at"]), "F1", 9, GREY)
    doc.space(6)

    usable = PAGE_W - 2 * MARGIN
    if detail["category"] == "internet":
        doc.paragraph("Reference site reliability in this window", "F2", 11, leading=16)
        doc.paragraph("The internet counts as UP when at least one reference site answers; a reference site that "
                      "fails often should be substituted.", "F1", 8, GREY)
        doc.space(2)
        cols = [("Reference site", 300), ("Success rate", 80), ("Checks", 60), ("OK", 60), ("Failed", 60),
                ("Avg latency", 80), ("Max latency", 80)]
        rows = []
        for x in detail["reference_summary"]:
            color = GREEN if x["checks"] and x["failed"] == 0 else (RED if x["failed"] else GREY)
            rows.append([x["url"], ("{:.2f}%".format(x["uptime_pct"]), color), x["checks"], x["ok"], x["failed"],
                         _ms(x["avg_latency_ms"]), _ms(x["max_latency_ms"])])
        doc.table(cols, rows or [["No reference sites configured."] + [""] * 6])
        doc.space(10)
        doc.paragraph("Every check", "F2", 11, leading=16)
        doc.space(2)
        refs = detail["reference_urls"]
        fixed = [("Time (UTC)", 130), ("Internet", 50), ("Reach.", 45)]
        per_ref = (usable - sum(w for _, w in fixed)) / max(1, len(refs))
        cols = fixed + [(_short_host(u), per_ref) for u in refs]
        rows = []
        for r in detail["rows"]:
            cells = [r["checked_at"], (r["result"], _result_color(r["result"])), "{}/{}".format(r["reachable"], r["total"])]
            for x in r["sites"]:
                if x["available"] is None:
                    cells.append(("-", GREY))
                elif x["available"]:
                    cells.append(("OK {:.0f} ms".format(x["latency_ms"]), GREEN))
                else:
                    cells.append(("FAIL " + (x["error"] or "HTTP {}".format(x["status"])), RED))
            rows.append(cells)
        doc.table(cols, rows or [["No checks in this period."] + [""] * (len(cols) - 1)])
    else:
        cols = [("Time (UTC)", 130), ("Result", 80), ("HTTP", 40), ("Latency", 70), ("Error", 340), ("Internet", 60)]
        rows = []
        for r in detail["rows"]:
            rows.append([
                r["checked_at"], (r["result"], _result_color(r["result"])), r["status"] or "-",
                _ms(r["latency_ms"]) if r["result"] == "OK" else "-", r["error"] or "",
                (r["internet"] or "unknown", _result_color(r["internet"] or "")),
            ])
        doc.table(cols, rows or [["No checks in this period."] + [""] * 5])
    return doc.build()


def _ms(value):
    return "{:.1f} ms".format(value) if value is not None else "-"


def render_availability_pdf(report):
    """Build the PDF of a time-window availability report (bonus for the CLI)."""
    w, sm, inet = report["window"], report["summary"], report["internet"]
    doc = PDFDocument("imon availability report - {}".format(w["label"]))
    doc.new_page()
    doc.paragraph("imon availability report: {}".format(w["label"]), "F2", 15, leading=18)
    doc.paragraph("Window: {} -> {}. Generated {} (all times UTC).".format(
        w["start"] or "beginning", w["end"], report["generated_at"]), "F1", 9, GREY)
    doc.space(6)
    pct = lambda v: "{:.2f}%".format(v) if v is not None else "n/a"
    doc.paragraph("Time without incidents (your websites): {}  (downtime {})".format(
        pct(sm["time_without_incidents_pct"]), format_duration(sm["downtime_seconds"])), "F2", 10, leading=14)
    doc.paragraph("Incidents / measures without incidents: {} / {}  ({} per 1000 OK measures)".format(
        sm["incidents"], sm["measures_ok"], sm["incidents_per_1000_ok"] if sm["incidents_per_1000_ok"] is not None else "n/a"),
        "F2", 10, leading=14)
    doc.paragraph("Internet without outages: {}  ({} outage(s), {} of {} cycles OK)".format(
        pct(inet["time_without_incidents_pct"]), inet["incidents"], inet["measures_ok"], inet["measures"]), "F2", 10, leading=14)
    doc.space(6)
    cols = [("Category", 300), ("No-incident %", 80), ("Downtime", 70), ("Incidents", 60), ("OK", 60),
            ("Failed", 60), ("Inconcl.", 60), ("Inc/1000 OK", 80)]
    rows = []
    entries = [("Internet ({} reference sites)".format(inet["reference_total"]), inet)]
    entries += [("  ref: " + x["url"], x) for x in report["reference"]]
    entries += [(x["url"], x) for x in report["sites"]]
    for label, x in entries:
        p = x["time_without_incidents_pct"]
        color = GREEN if p == 100 else (RED if x["incidents"] else GREY)
        rows.append([label, (pct(p), color), format_duration(x["downtime_seconds"]), x["incidents"], x["measures_ok"],
                     x["measures_failed"], x["inconclusive"],
                     x["incidents_per_1000_ok"] if x["incidents_per_1000_ok"] is not None else "n/a"])
    doc.table(cols, rows)
    return doc.build()
