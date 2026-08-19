#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build peaks_<BOOK>.csv for PropRouter from an Apex broker statement.

The router reconstructs each seat's liquidation floor from the seeded high-water
mark:

    floor = min(peak - drawdown, start_balance + frozen_offset)

so the seed peak is simply the broker's governing high-water mark:

    peak = "Auto Liquidate Peak Balance"

The broker statement is not self-describing, and the drawdown differs per
account. For an intraday seat that has not yet frozen, the statement proves its
own drawdown:

    Auto Liquidate Peak Balance - Auto Liquidate Threshold Value = drawdown

This script uses that identity as a cross-check against the configured value and
refuses to write anything on a mismatch. For a frozen seat the identity does not
hold (the floor is pinned at start + frozen_offset, not at peak - drawdown), so
the frozen level is verified instead.

Accounts 12 and 13 are end-of-day trailing accounts: their threshold column is
blank and "Auto Liquidate EOD Value" carries the floor instead. Per the project
decision they are still modelled as intraday, which is the conservative
direction - an intraday peak is always at or above an end-of-day peak, so the
modelled floor sits at or above the broker's real floor and headroom is
understated, never overstated. The script proves that inequality per row and
reports the size of the gap.

Usage:
    python make_peak_file.py Broker_statement.csv
    python make_peak_file.py Broker_statement.csv --book LIVE --out peaks_LIVE.csv
    python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv
"""

from __future__ import print_function

import argparse
import csv
import io
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# start / dd MUST equal the "Seat starting balance" and "Seat trailing drawdown"
# properties on the matching NT8 chart. The router compares the CSV values with
# the chart values using an exact != test and refuses the seat on any mismatch.
# These are per-seat: differing drawdowns across the book are expected and fine.
#
# "kind" is how the BROKER runs the account, not how the router models it. The
# router models every seat as intraday.
# ---------------------------------------------------------------------------
SEAT_CONFIG = {
    "PA-APEX-240737-09": {"instance": 1, "start": "25000", "dd": "1500", "kind": "intraday"},
    "PA-APEX-240737-10": {"instance": 2, "start": "25000", "dd": "1500", "kind": "intraday"},
    "PA-APEX-240737-12": {"instance": 3, "start": "50000", "dd": "2000", "kind": "eod"},
    "PA-APEX-240737-13": {"instance": 4, "start": "50000", "dd": "2000", "kind": "eod"},
    "PA-APEX-240737-14": {"instance": 5, "start": "50000", "dd": "2000", "kind": "intraday"},
    "PA-APEX-240737-15": {"instance": 6, "start": "50000", "dd": "2500", "kind": "intraday"},
}

# Must be identical on every chart: it is part of the book manifest fingerprint.
FROZEN_OFFSET = Decimal("100")
HEADER = "account,start_balance,drawdown,peak,updated_utc"
TOL = Decimal("0.005")          # half a cent


REQUIRED_COLUMNS = (
    "Account",
    "Status",
    "Account Balance",
    "Auto Liquidate Threshold Value",
    "Auto Liquidate EOD Value",
    "Auto Liquidate Peak Balance",
)


class SeedError(Exception):
    pass


def disagreement(account, pairs, explain):
    """Format a statement-vs-config conflict without asserting which side is wrong."""
    width = max(len(k) for k, _ in pairs)
    out = ["", "account %s: STATEMENT AND CONFIG DISAGREE" % account, ""]
    for key, value in pairs:
        out.append("    %-*s  %s" % (width, key, value))
    out.append("")
    out.append("  " + explain)
    out.append("  Either the statement is stale, edited or misread, or SEAT_CONFIG is")
    out.append("  wrong - and SEAT_CONFIG must also match the chart's Signal Routing")
    out.append("  properties. Check the statement against the broker portal first.")
    out.append("  No peak file was written or verified.")
    return "\n".join(out)


def dec(raw):
    """Parse a broker cell into a Decimal, or None when blank."""
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise SeedError("cannot parse number %r" % raw)


def model_floor(peak, dd, start, frozen_offset=FROZEN_OFFSET):
    """The router's floor formula, verbatim."""
    return min(peak - dd, start + frozen_offset)


def read_statement(path):
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = [(f or "").strip() for f in (reader.fieldnames or [])]
        rows = list(reader)

    absent = [c for c in REQUIRED_COLUMNS if c not in fields]
    if absent:
        raise SeedError(
            "%s is not a recognised broker statement.\n"
            "  missing columns: %s\n"
            "  columns found  : %s\n"
            "  Re-export the account statement from the broker portal without "
            "reformatting it." % (path, ", ".join(absent), ", ".join(fields) or "(none)"))

    if not rows:
        raise SeedError("%s has the right columns but no data rows" % path)

    cleaned = [dict(((k or "").strip(), (v or "").strip()) for k, v in row.items())
               for row in rows]

    seen = {}
    for i, row in enumerate(cleaned, start=2):
        name = row.get("Account", "")
        if name in seen:
            raise SeedError("%s lists account %s twice (rows %d and %d); one row "
                            "per account is required" % (path, name, seen[name], i))
        seen[name] = i
    return cleaned


def build_seed(row):
    account = row.get("Account", "")
    if account not in SEAT_CONFIG:
        raise SeedError(
            "the statement contains account %r, which is not in SEAT_CONFIG.\n"
            "  configured seats: %s\n"
            "  Add it with the exact start balance and drawdown set on its chart, "
            "or remove it from the statement." % (account, ", ".join(sorted(SEAT_CONFIG))))

    status = row.get("Status", "")
    if status.lower() != "active":
        raise SeedError("account %s has status %r, not Active" % (account, status))

    cfg = SEAT_CONFIG[account]
    start = Decimal(cfg["start"])
    dd = Decimal(cfg["dd"])
    kind = cfg["kind"]

    balance = dec(row.get("Account Balance"))
    threshold = dec(row.get("Auto Liquidate Threshold Value"))
    eod_value = dec(row.get("Auto Liquidate EOD Value"))
    peak = dec(row.get("Auto Liquidate Peak Balance"))

    if balance is None:
        raise SeedError("account %s has no Account Balance" % account)
    if peak is None:
        raise SeedError("account %s has an empty Auto Liquidate Peak Balance cell. "
                        "The governing high-water mark is unknown, so the seat "
                        "cannot be seeded. Re-export the statement." % account)

    frozen_level = start + FROZEN_OFFSET
    floor = model_floor(peak, dd, start)
    frozen = (peak - dd) >= frozen_level
    note = ""

    if kind == "intraday":
        if threshold is None:
            raise SeedError("account %s is configured intraday but has no "
                            "Auto Liquidate Threshold Value" % account)

        if frozen:
            # peak - dd is meaningless once the cap binds; verify the cap instead.
            if abs(threshold - frozen_level) > TOL:
                raise SeedError(disagreement(account, [
                    ("Auto Liquidate Threshold Value", threshold),
                    ("start balance + frozen offset", frozen_level),
                    ("SEAT_CONFIG start balance", start),
                    ("frozen floor offset", FROZEN_OFFSET),
                ], "This seat's floor has frozen, so the threshold must equal "
                   "start + frozen offset exactly."))
        else:
            # The statement proves its own drawdown. This is the check that
            # catches a wrong SEAT_CONFIG drawdown before it reaches the router.
            implied_dd = peak - threshold
            if abs(implied_dd - dd) > TOL:
                raise SeedError(disagreement(account, [
                    ("Auto Liquidate Peak Balance", peak),
                    ("Auto Liquidate Threshold Value", threshold),
                    ("implied drawdown (peak - threshold)", implied_dd),
                    ("SEAT_CONFIG drawdown", dd),
                ], "For an intraday seat that has not frozen, peak minus threshold "
                   "IS the drawdown, so these must match exactly."))

        if abs(floor - threshold) > TOL:
            raise SeedError("account %s: reconstructed floor %s does not match the "
                            "broker threshold %s" % (account, floor, threshold))
        broker_floor = threshold

    else:  # end-of-day account, modelled as intraday
        if eod_value is None:
            raise SeedError("account %s is configured end-of-day but has no "
                            "Auto Liquidate EOD Value" % account)

        # The intraday peak is at or above the EOD peak, so peak - eod_value is
        # at or above the true drawdown. A value below it means dd is too large.
        implied_min_dd = peak - eod_value
        if implied_min_dd + TOL < dd:
            raise SeedError(disagreement(account, [
                ("Auto Liquidate Peak Balance", peak),
                ("Auto Liquidate EOD Value", eod_value),
                ("implied maximum drawdown", implied_min_dd),
                ("SEAT_CONFIG drawdown", dd),
            ], "The intraday peak is at or above the end-of-day peak, so peak minus "
               "the EOD floor cannot be smaller than the drawdown."))

        if floor + TOL < eod_value:
            raise SeedError(
                "account %s: modelled floor %s is BELOW the broker's EOD floor %s. "
                "That overstates headroom and must never ship."
                % (account, floor, eod_value))

        note = "+%.2f vs EOD floor" % (floor - eod_value)
        broker_floor = eod_value

    if peak < start:
        raise SeedError("account %s: seed peak %s is below start balance %s; the "
                        "router's strict CSV validation rejects peak < start"
                        % (account, peak, start))

    return {
        "account": account,
        "instance": cfg["instance"],
        "start": start,
        "dd": dd,
        "kind": kind,
        "peak": peak,
        "balance": balance,
        "floor": floor,
        "broker_floor": broker_floor,
        "headroom": balance - floor,
        "frozen": frozen,
        "freeze_at": frozen_level + dd,     # peak needed for the floor to lock
        "note": note,
    }


def write_peak_file(seeds, path, stamp):
    lines = [HEADER]
    for s in sorted(seeds, key=lambda x: x["account"]):
        lines.append("%s,%.2f,%.2f,%.2f,%s"
                     % (s["account"], s["start"], s["dd"], s["peak"], stamp))
    body = "\n".join(lines) + "\n"
    # UTF-8 without BOM, period decimals, LF endings. Never write this from Excel.
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return body


def report(seeds):
    print("")
    print("  %-20s %3s %8s %-9s %10s %10s %10s %9s"
          % ("account", "id", "dd", "broker", "balance", "floor", "peak seed", "headroom"))
    print("  " + "-" * 88)
    for s in sorted(seeds, key=lambda x: x["account"]):
        flag = "  FROZEN" if s["frozen"] else ""
        if s["note"]:
            flag += "  " + s["note"]
        print("  %-20s %3d %8.0f %-9s %10.2f %10.2f %10.2f %9.2f%s"
              % (s["account"], s["instance"], s["dd"], s["kind"], s["balance"],
                 s["floor"], s["peak"], s["headroom"], flag))

    unfrozen = [s for s in seeds if not s["frozen"]]
    if unfrozen:
        print("")
        print("  distance to floor freeze (peak must reach start + dd + 100):")
        for s in sorted(unfrozen, key=lambda x: x["account"]):
            print("    %-20s needs peak %10.2f, at %10.2f  (%+.2f)"
                  % (s["account"], s["freeze_at"], s["peak"], s["peak"] - s["freeze_at"]))

    print("")
    print("  max_headroom ranking - the order the router will prefer:")
    for i, s in enumerate(sorted(seeds, key=lambda x: -x["headroom"]), 1):
        print("    %d. %-20s %9.2f%s"
              % (i, s["account"], s["headroom"],
                 "   <- frozen, payout-capable" if s["frozen"] else ""))


def check_existing(seeds, path):
    """Compare a previously written peak file against freshly derived seeds."""
    if not os.path.exists(path):
        raise SeedError("%s does not exist" % path)
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        lines = fh.read().replace("\r\n", "\n").rstrip("\n").split("\n")
    if not lines or lines[0] != HEADER:
        raise SeedError("%s has a bad header; expected exactly '%s'" % (path, HEADER))

    on_disk = {}
    for n, line in enumerate(lines[1:], start=2):
        f = line.split(",")
        if len(f) != 5:
            raise SeedError("%s row %d: expected exactly 5 fields, got %d"
                            % (path, n, len(f)))
        on_disk[f[0].strip()] = f

    ok = True
    for s in sorted(seeds, key=lambda x: x["account"]):
        f = on_disk.get(s["account"])
        if f is None:
            print("  MISSING  %s is not in the file" % s["account"])
            ok = False
            continue
        want = ("%.2f" % s["start"], "%.2f" % s["dd"], "%.2f" % s["peak"])
        got = (f[1].strip(), f[2].strip(), f[3].strip())
        if want != got:
            print("  STALE    %s file start/dd/peak=%s, statement implies %s"
                  % (s["account"], "/".join(got), "/".join(want)))
            ok = False
        else:
            print("  ok       %s" % s["account"])

    for name in sorted(set(on_disk) - set(s["account"] for s in seeds)):
        print("  EXTRA    %s is in the file but not in the statement" % name)
        ok = False
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build or verify a PropRouter peak file from a broker statement.")
    ap.add_argument("statement", help="broker statement CSV")
    ap.add_argument("--book", default="LIVE", help="Book ID (default: LIVE)")
    ap.add_argument("--out", default=None,
                    help="output path (default: peaks_<BOOK>.csv)")
    ap.add_argument("--check", metavar="PEAKFILE", default=None,
                    help="verify an existing peak file instead of writing one")
    args = ap.parse_args(argv)

    try:
        rows = read_statement(args.statement)
        seeds = [build_seed(r) for r in rows]
    except SeedError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    missing = set(SEAT_CONFIG) - set(s["account"] for s in seeds)
    if missing:
        print("ERROR: statement is missing configured seats: %s"
              % ", ".join(sorted(missing)), file=sys.stderr)
        return 2

    print("Read %d active seats from %s" % (len(seeds), args.statement))
    report(seeds)

    if args.check:
        print("")
        print("Checking %s against the statement:" % args.check)
        try:
            ok = check_existing(seeds, args.check)
        except SeedError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2
        print("")
        print("Peak file is current."
              if ok else "Peak file is STALE - re-run without --check to rewrite.")
        return 0 if ok else 1

    out = args.out or ("peaks_%s.csv" % args.book)
    stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    body = write_peak_file(seeds, out, stamp)

    print("")
    print("Wrote %s" % os.path.abspath(out))
    print("")
    for line in body.rstrip("\n").split("\n"):
        print("  " + line)
    print("")
    print("Chart properties that must match these rows exactly:")
    for s in sorted(seeds, key=lambda x: x["instance"]):
        print("  Instance %d  %s   Seat starting balance=%.0f  Seat trailing drawdown=%.0f"
              % (s["instance"], s["account"], s["start"], s["dd"]))
    print("  All six charts: Frozen floor offset=%.0f (book manifest - must be identical)"
          % FROZEN_OFFSET)
    print("")
    print("Next:")
    print("  1. Stop every strategy in book '%s' before copying this into place. The"
          % args.book)
    print("     router rewrites the file on every new equity high and would clobber it.")
    print("  2. Copy to:  Documents\\NinjaTrader 8\\PropRouter\\%s"
          % os.path.basename(out))
    print("  3. Start the strategies; each should print 'peak seeded at ...'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
