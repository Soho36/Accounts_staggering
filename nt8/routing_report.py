#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Render a PropRouter routing log as a self-contained HTML session report.

Reads Documents\\NinjaTrader 8\\PropRouter\\routing_<BOOK>_<yyyymmdd>.csv and
writes one HTML file with the data inlined - no server, no network, no external
libraries. Open it straight from disk.

The report exists to make one thing obvious that the Output window hides: a
seat's headroom is not its P&L. Because equity includes unrealised profit and the
peak only ever rises, an open trade that goes into profit raises the floor
permanently. The identity behind every row is

    change in headroom = change in equity - change in floor

so a winning trade can still cost headroom, and a seat can be up on the day with
less headroom than a seat that is down.

Usage:
    python routing_report.py routing_SIM_20260820.csv
    python routing_report.py routing_SIM_20260820.csv --out session.html
    python make_peak_file.py --report routing_SIM_20260820.csv
"""

from __future__ import print_function

import argparse
import csv
import io
import os
import sys
from datetime import datetime

SERIES_COLORS = [
    "#D1603A", "#2E9088", "#5B7FD1", "#B08A2E", "#9A5FBF", "#4A9A5A",
    "#C2506E", "#4F8FA8", "#8B7355", "#6A8F3C",
]

REQUIRED = ("bar_time", "winners", "detail", "seat", "account", "status",
            "equity", "peak", "floor", "headroom", "frozen", "seeded", "trades")


class ReportError(Exception):
    pass


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _num(text):
    try:
        return float((text or "").strip())
    except ValueError:
        return float("nan")


def _flag(text):
    return (text or "").strip().lower() in ("true", "1", "yes")


def read_routing_log(path):
    if not os.path.isfile(path):
        raise ReportError("%s does not exist. PropRouter writes routing logs into "
                          "the PropRouter folder as routing_<BOOK>_<yyyymmdd>.csv"
                          % path)
    try:
        fh = io.open(path, "r", encoding="utf-8-sig", newline="")
    except IOError as exc:
        raise ReportError("cannot read %s: %s" % (path, exc))
    with fh:
        reader = csv.DictReader(fh)
        fields = [(f or "").strip() for f in (reader.fieldnames or [])]
        rows = list(reader)

    missing = [c for c in REQUIRED if c not in fields]
    if missing:
        raise ReportError(
            "%s is not a PropRouter routing log.\n"
            "  missing columns: %s\n"
            "  columns found  : %s"
            % (path, ", ".join(missing), ", ".join(fields) or "(none)"))
    if not rows:
        raise ReportError("%s has no data rows" % path)

    decisions = []
    index = {}
    for raw in rows:
        row = dict(((k or "").strip(), (v or "").strip()) for k, v in raw.items())
        bar = row["bar_time"]
        if bar not in index:
            winners = [] if row["winners"].lower() in ("none", "") else row["winners"].split()
            index[bar] = {
                "bar_time": bar,
                "winners": winners,
                "detail": row["detail"],
                "seats": {},
            }
            decisions.append(index[bar])
        seat = row["seat"]
        index[bar]["seats"][seat] = {
            "account": row["account"],
            "status": row["status"],
            "equity": _num(row["equity"]),
            "peak": _num(row["peak"]),
            "floor": _num(row["floor"]),
            "headroom": _num(row["headroom"]),
            "frozen": _flag(row["frozen"]),
            "seeded": _flag(row["seeded"]),
            "trades": _num(row["trades"]),
        }

    decisions.sort(key=lambda d: d["bar_time"])
    seats = sorted(set(s for d in decisions for s in d["seats"]), key=lambda s: int(s))
    if not seats:
        raise ReportError("%s contains no seat rows" % path)
    return decisions, seats


def summarise(decisions, seats):
    out = []
    for i, seat in enumerate(seats):
        present = [d["seats"][seat] for d in decisions if seat in d["seats"]]
        if not present:
            continue
        first, last = present[0], present[-1]
        wins = sum(1 for d in decisions if seat in d["winners"])
        out.append({
            "seat": seat,
            "color": SERIES_COLORS[i % len(SERIES_COLORS)],
            "account": last["account"],
            "wins": wins,
            "trades": last["trades"],
            "equity_first": first["equity"], "equity_last": last["equity"],
            "floor_first": first["floor"], "floor_last": last["floor"],
            "headroom_first": first["headroom"], "headroom_last": last["headroom"],
            "d_equity": last["equity"] - first["equity"],
            "d_floor": last["floor"] - first["floor"],
            "d_headroom": last["headroom"] - first["headroom"],
            "frozen": last["frozen"],
            "seeded": last["seeded"],
            "min_headroom": min(p["headroom"] for p in present),
        })
    return out


def collect_flags(decisions, summary):
    flags = []
    for s in summary:
        if not s["seeded"]:
            flags.append("seat %s (%s) is NOT SEEDED - it can never be selected"
                         % (s["seat"], s["account"]))
        if s["min_headroom"] <= 0:
            flags.append("seat %s (%s) reached zero or negative headroom (%.2f)"
                         % (s["seat"], s["account"], s["min_headroom"]))
    for d in decisions:
        if "FAIL_CLOSED" in d["detail"]:
            flags.append("%s: book was fail-closed - %s" % (d["bar_time"], d["detail"]))
        elif "UNSEEDED" in d["detail"]:
            flags.append("%s: %s" % (d["bar_time"], d["detail"]))
    return flags


# ---------------------------------------------------------------------------
# svg
# ---------------------------------------------------------------------------

def _path(points):
    return " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % p
                    for i, p in enumerate(points))


def _scale(lo, hi, pad=0.08):
    if not (hi > lo):
        hi, lo = lo + 1.0, lo - 1.0
    span = hi - lo
    return lo - span * pad, hi + span * pad


def headroom_chart(decisions, summary):
    W, H, ML, MR, MT, MB = 980, 360, 66, 14, 16, 48
    n = len(decisions)
    values = [p["headroom"] for d in decisions for p in d["seats"].values()]
    lo, hi = _scale(min(values), max(values))

    def px(i):
        return ML if n < 2 else ML + (float(i) / (n - 1)) * (W - ML - MR)

    def py(v):
        return MT + (1.0 - (v - lo) / (hi - lo)) * (H - MT - MB)

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Headroom per seat '
             'across %d routing decisions">' % (W, H, n)]

    steps = 5
    for k in range(steps + 1):
        v = lo + (hi - lo) * k / float(steps)
        y = py(v)
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
                     % (ML, y, W - MR, y))
        parts.append('<text class="ax" x="%d" y="%.1f" text-anchor="end">%s</text>'
                     % (ML - 8, y + 4, "{:,.0f}".format(v)))

    every = max(1, n // 9)
    for i, d in enumerate(decisions):
        if i % every == 0 or i == n - 1:
            label = d["bar_time"][11:16] or d["bar_time"]
            parts.append('<text class="ax" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                         % (px(i), H - MB + 20, label))

    for s in summary:
        pts = []
        for i, d in enumerate(decisions):
            p = d["seats"].get(s["seat"])
            if p:
                pts.append((px(i), py(p["headroom"])))
        if pts:
            parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
                         'stroke-linejoin="round"/>' % (_path(pts), s["color"]))
        for i, d in enumerate(decisions):
            if s["seat"] in d["winners"] and s["seat"] in d["seats"]:
                parts.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s" '
                             'stroke="var(--bg)" stroke-width="1.5"/>'
                             % (px(i), py(d["seats"][s["seat"]]["headroom"]), s["color"]))

    parts.append('<text class="ax" x="%.1f" y="%d" text-anchor="middle">'
                 'decision (filled dot = routed here)</text>'
                 % ((W + ML) / 2.0, H - 6))
    parts.append("</svg>")
    return "".join(parts)


def seat_panel(decisions, s):
    """Equity, peak and floor for one seat; the shaded gap is its headroom."""
    W, H, ML, MR, MT, MB = 320, 168, 54, 10, 14, 26
    pts = [(i, d["seats"][s["seat"]]) for i, d in enumerate(decisions)
           if s["seat"] in d["seats"]]
    if not pts:
        return ""
    lo, hi = _scale(min(p["floor"] for _, p in pts),
                    max(max(p["peak"] for _, p in pts),
                        max(p["equity"] for _, p in pts)))
    n = len(decisions)

    def px(i):
        return ML if n < 2 else ML + (float(i) / (n - 1)) * (W - ML - MR)

    def py(v):
        return MT + (1.0 - (v - lo) / (hi - lo)) * (H - MT - MB)

    eq = [(px(i), py(p["equity"])) for i, p in pts]
    fl = [(px(i), py(p["floor"])) for i, p in pts]
    pk = [(px(i), py(p["peak"])) for i, p in pts]

    band = _path(eq) + " " + " ".join("L%.1f %.1f" % q for q in reversed(fl)) + " Z"

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Seat %s equity, peak '
             'and floor; the shaded gap is headroom">' % (W, H, s["seat"])]
    for k in range(3):
        v = lo + (hi - lo) * k / 2.0
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
                     % (ML, py(v), W - MR, py(v)))
        parts.append('<text class="ax sm" x="%d" y="%.1f" text-anchor="end">%s</text>'
                     % (ML - 6, py(v) + 3, "{:,.0f}".format(v)))
    parts.append('<path d="%s" fill="%s" opacity="0.15"/>' % (band, s["color"]))
    parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.1" '
                 'stroke-dasharray="4 3"/>' % (_path(pk), s["color"]))
    parts.append('<path d="%s" fill="none" stroke="currentColor" stroke-width="1.2" '
                 'opacity="0.55"/>' % _path(fl))
    parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2"/>'
                 % (_path(eq), s["color"]))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#EDEFEE;--panel:#F8FAF9;--ink:#16222A;--dim:#5A6A70;--faint:#84969B;
--rule:#C7D0CF;--good:#1B6E70;--bad:#98291A;--warn:#A9531D;
--warn-bg:rgba(169,83,29,.10);--good-bg:rgba(27,110,112,.10)}
@media (prefers-color-scheme:dark){:root{--bg:#111719;--panel:#182023;--ink:#E3EAE8;
--dim:#93A4A6;--faint:#73868A;--rule:#2C393D;--good:#4EB1AF;--bad:#D9584B;--warn:#DE8B4E;
--warn-bg:rgba(222,139,78,.14);--good-bg:rgba(78,177,175,.14)}}
*{box-sizing:border-box}
body{margin:0;padding:0 22px 72px;background:var(--bg);color:var(--ink);
font:14px/1.6 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:1040px;margin:0 auto}
h1,h2{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-weight:600;margin:0}
h1{font-size:1.6rem;letter-spacing:-.02em}
h2{font-size:1.02rem;margin:34px 0 12px}
header{padding:34px 0 18px;border-bottom:1px solid var(--rule)}
.meta{color:var(--dim);font-size:.86rem;margin-top:8px}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:4px;padding:16px}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{padding:7px 10px;border-bottom:1px solid var(--rule);text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}
th{font-family:ui-monospace,Consolas,monospace;font-size:.66rem;letter-spacing:.08em;
text-transform:uppercase;color:var(--faint);font-weight:600}
td:first-child,th:first-child,td.l,th.l{text-align:left}
.pos{color:var(--good)}.neg{color:var(--bad)}
.chip{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;
vertical-align:-1px}
.grid{stroke:currentColor;opacity:.14}
.ax{fill:var(--faint);font:11px ui-monospace,Consolas,monospace}
.ax.sm{font-size:9.5px}
svg{max-width:100%;height:auto;display:block}
.mult{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:4px;padding:10px 12px}
.card h3{margin:0 0 2px;font:600 .88rem ui-monospace,Consolas,monospace}
.card p{margin:0 0 6px;color:var(--dim);font-size:.76rem}
.flags{border-left:3px solid var(--warn);background:var(--warn-bg);
padding:12px 16px;border-radius:0 4px 4px 0;margin-top:14px}
.flags ul{margin:6px 0 0;padding-left:18px}
.flags li{font-size:.85rem;margin:3px 0}
.ok{border-left-color:var(--good);background:var(--good-bg)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px;font-size:.8rem;color:var(--dim)}
.note{color:var(--dim);font-size:.84rem;max-width:74ch;margin-top:10px}
code{font-family:ui-monospace,Consolas,monospace;font-size:.9em}
.scroll{overflow-x:auto}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:.78rem;color:var(--dim)}
"""


def _sign(v, digits=2):
    if abs(v) < 0.005:          # avoid rendering a negated zero as "-0.00"
        v = 0.0
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return '<td class="%s">%s%s</td>' % (cls, "+" if v > 0 else "", ("{:,.%df}" % digits).format(v))


def build_html(decisions, seats, source):
    summary = summarise(decisions, seats)
    flags = collect_flags(decisions, summary)
    book = os.path.basename(source)

    rows = []
    for s in summary:
        rows.append(
            '<tr><td class="l"><span class="chip" style="background:%s"></span>%s</td>'
            '<td class="l">%s</td><td>%d</td><td>%.0f</td>'
            '<td>%s</td><td>%s</td>%s%s%s<td class="l">%s</td></tr>'
            % (s["color"], s["seat"], s["account"], s["wins"], s["trades"],
               "{:,.2f}".format(s["headroom_first"]), "{:,.2f}".format(s["headroom_last"]),
               _sign(s["d_equity"]), _sign(-s["d_floor"]), _sign(s["d_headroom"]),
               ("frozen" if s["frozen"] else "") + ("" if s["seeded"] else " UNSEEDED")))

    dec_rows = []
    for d in decisions:
        who = ", ".join(d["winners"]) if d["winners"] else "&mdash;"
        rank = " ".join(
            "%s:%.0f%s" % (seat, p["headroom"],
                           "" if p["status"] == "Free" else "(" + p["status"][:1] + ")")
            for seat, p in sorted(d["seats"].items(),
                                  key=lambda kv: -kv[1]["headroom"]))
        dec_rows.append('<tr><td class="l">%s</td><td class="l">%s</td>'
                        '<td class="l mono">%s</td><td class="l mono">%s</td></tr>'
                        % (d["bar_time"], who, rank, d["detail"]))

    panels = []
    for s in summary:
        panels.append(
            '<div class="card"><h3><span class="chip" style="background:%s"></span>'
            'seat %s &middot; %s</h3><p>equity solid &middot; peak dashed &middot; '
            'floor grey &middot; shaded gap = headroom</p>%s</div>'
            % (s["color"], s["seat"], s["account"], seat_panel(decisions, s)))

    flag_html = (
        '<div class="flags"><strong>Flags</strong><ul>%s</ul></div>'
        % "".join("<li>%s</li>" % f for f in flags)
        if flags else
        '<div class="flags ok"><strong>No flags.</strong> Every seat stayed seeded '
        'with positive headroom and no decision was fail-closed.</div>')

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Routing session &mdash; %(book)s</title><style>%(css)s</style></head><body>
<div class="wrap">
<header>
  <h1>Routing session report</h1>
  <p class="meta">%(book)s &middot; %(ndec)d decisions &middot; %(nseat)d seats
  &middot; %(first)s to %(last)s &middot; generated %(gen)s</p>
  <p class="meta">%(routed)d decisions routed a copy, %(blocked)d produced no winner
  &mdash; normally because a seat already held a working entry order
  (<code>need = R &minus; pending = 0</code>).</p>
</header>

%(flags)s

<h2>Seats</h2>
<div class="panel scroll">
<table>
<thead><tr><th class="l">seat</th><th class="l">account</th><th>routed</th><th>trades</th>
<th>headroom start</th><th>headroom end</th><th>&Delta; equity</th>
<th>&Delta; floor</th><th>&Delta; headroom</th><th class="l">state</th></tr></thead>
<tbody>%(rows)s</tbody>
</table>
</div>
<p class="note"><strong>&Delta; headroom = &Delta; equity &minus; &Delta; floor.</strong>
The floor only ever rises, so unrealised profit that is later given back is
consumed permanently. That is why a winning trade can still cost headroom, and
why a seat can be up on the day with less headroom than a seat that is down.
The &Delta; floor column is shown negated, as the cost it represents.</p>

<h2>Headroom over the session</h2>
<div class="panel">%(chart)s
<div class="legend">%(legend)s</div></div>

<h2>Per seat: equity against its floor</h2>
<div class="mult">%(panels)s</div>

<h2>Decisions</h2>
<div class="panel scroll">
<table>
<thead><tr><th class="l">bar time</th><th class="l">routed to</th>
<th class="l">ranking (headroom, P=pending I=in position)</th>
<th class="l">detail</th></tr></thead>
<tbody>%(dec)s</tbody>
</table>
</div>
</div></body></html>""" % {
        "book": book,
        "css": CSS,
        "ndec": len(decisions),
        "nseat": len(seats),
        "first": decisions[0]["bar_time"],
        "last": decisions[-1]["bar_time"],
        "gen": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "routed": sum(1 for d in decisions if d["winners"]),
        "blocked": sum(1 for d in decisions if not d["winners"]),
        "flags": flag_html,
        "rows": "".join(rows),
        "chart": headroom_chart(decisions, summary),
        "legend": "".join('<span><span class="chip" style="background:%s"></span>'
                          'seat %s</span>' % (s["color"], s["seat"]) for s in summary),
        "panels": "".join(panels),
        "dec": "".join(dec_rows),
    }


def render(source, out=None):
    decisions, seats = read_routing_log(source)
    html = build_html(decisions, seats, source)
    if not out:
        base = os.path.splitext(os.path.basename(source))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(source)), base + "_report.html")
    with io.open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(html)
    return out, decisions, seats


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render a PropRouter routing log as a self-contained HTML report.")
    ap.add_argument("routing_csv", help="PropRouter routing_<BOOK>_<yyyymmdd>.csv")
    ap.add_argument("--out", default=None, help="output HTML path")
    args = ap.parse_args(argv)

    try:
        out, decisions, seats = render(args.routing_csv, args.out)
    except ReportError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    print("Read %d decisions across %d seats from %s"
          % (len(decisions), len(seats), args.routing_csv))
    print("Wrote %s" % os.path.abspath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
