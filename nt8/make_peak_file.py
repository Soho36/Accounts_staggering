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
    python make_peak_file.py Broker_statement.csv --book LIVE               ---> Live book, write locally
    python make_peak_file.py Broker_statement.csv --book LIVE --install     ---> Live book, write into NinjaTrader
    python make_peak_file.py --seats sim --book SIM --install`              ---> Simulation book
    python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv    ---> Verify a live file is current
    python make_peak_file.py --seats sim --check peaks_SIM.csv              ---> Verify a simulation file
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

# Simulation book. The drawdowns mirror the live mix (1500/1500/2000/2000/2000/2500)
# so Playback exercises the same max_headroom ranking dynamics as the real book.
#
# "start" must equal the SIM account's actual starting balance in NinjaTrader, not
# the live tier - the router measures headroom against real account equity. The
# start balance does not affect the dynamics anyway: a seat freezes once its peak
# reaches start + dd + frozen_offset, so the distance to freeze is dd + 100
# regardless of where the account starts.
#
# Every seat is "intraday" here: the router models all seats that way, and a
# simulation account has no end-of-day broker rule to reproduce.
SEAT_CONFIG_SIM = {
    "SIM101": {"instance": 1, "start": "100000", "dd": "1500", "kind": "intraday"},
    "SIM102": {"instance": 2, "start": "100000", "dd": "1500", "kind": "intraday"},
    "SIM103": {"instance": 3, "start": "100000", "dd": "2000", "kind": "intraday"},
    "SIM104": {"instance": 4, "start": "100000", "dd": "2000", "kind": "intraday"},
    "SIM105": {"instance": 5, "start": "100000", "dd": "2000", "kind": "intraday"},
    "SIM106": {"instance": 6, "start": "100000", "dd": "2500", "kind": "intraday"},
}

SEAT_CONFIGS = {"live": SEAT_CONFIG, "sim": SEAT_CONFIG_SIM}

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


def validate_seat_config(cfg, label):
    """Catch config mistakes that would otherwise surface as unregisterable charts."""
    if not cfg:
        raise SeedError("SEAT_CONFIG for %r is empty" % label)

    instances = {}
    for account, c in sorted(cfg.items()):
        for key in ("instance", "start", "dd", "kind"):
            if key not in c:
                raise SeedError("%s seat %s is missing %r" % (label, account, key))
        if c["kind"] not in ("intraday", "eod"):
            raise SeedError("%s seat %s has kind %r; expected 'intraday' or 'eod'"
                            % (label, account, c["kind"]))
        try:
            start = Decimal(c["start"])
            dd = Decimal(c["dd"])
        except InvalidOperation:
            raise SeedError("%s seat %s has a non-numeric start or dd" % (label, account))
        if start <= 0 or dd <= 0:
            raise SeedError("%s seat %s needs start and dd greater than zero" % (label, account))

        inst = c["instance"]
        if inst in instances:
            raise SeedError(
                "%s seats %s and %s both use Instance ID %d. Each chart in a book needs "
                "its own ID, and the router requires every ID from 1 to the seat count "
                "exactly once." % (label, instances[inst], account, inst))
        instances[inst] = account

    expected = set(range(1, len(cfg) + 1))
    if set(instances) != expected:
        raise SeedError(
            "%s Instance IDs are %s; with %d seats they must be exactly %s"
            % (label, sorted(instances), len(cfg), sorted(expected)))


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


def nt8_state_dir():
    """Best-effort location of the NinjaTrader 8 PropRouter directory."""
    home = os.path.expanduser("~")
    for parent in (os.path.join(home, "Documents", "NinjaTrader 8"),
                   os.path.join(home, "OneDrive", "Documents", "NinjaTrader 8")):
        if os.path.isdir(parent):
            return os.path.join(parent, "PropRouter")
    return None


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


def build_seed(row, seat_config):
    account = row.get("Account", "")
    if account not in seat_config:
        raise SeedError(
            "the statement contains account %r, which is not in SEAT_CONFIG.\n"
            "  configured seats: %s\n"
            "  Add it with the exact start balance and drawdown set on its chart, "
            "or remove it from the statement." % (account, ", ".join(sorted(seat_config))))

    status = row.get("Status", "")
    if status.lower() != "active":
        raise SeedError("account %s has status %r, not Active" % (account, status))

    cfg = seat_config[account]
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


def build_untraded_seeds(seat_config):
    """Seed brand-new accounts that have no trading history and so no statement.

    An account that has never traded has no high-water mark above its starting
    balance, so peak = start, floor = start - dd, and the full drawdown is
    available. This is ONLY correct before the account trades: once it has, the
    real peak may be higher and seeding at start would put the floor too low and
    overstate headroom. Reset the simulation accounts rather than guessing.
    """
    seeds = []
    for account, cfg in seat_config.items():
        start = Decimal(cfg["start"])
        dd = Decimal(cfg["dd"])
        peak = start
        floor = model_floor(peak, dd, start)
        seeds.append({
            "account": account,
            "instance": cfg["instance"],
            "start": start,
            "dd": dd,
            "kind": cfg["kind"],
            "peak": peak,
            "balance": start,
            "floor": floor,
            "broker_floor": floor,
            "headroom": start - floor,
            "frozen": False,
            "freeze_at": start + FROZEN_OFFSET + dd,
            "note": "untraded",
        })
    return seeds


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
    ap.add_argument("statement", nargs="?", default=None,
                    help="broker statement CSV (required for --seats live)")
    ap.add_argument("--seats", choices=sorted(SEAT_CONFIGS), default="live",
                    help="which SEAT_CONFIG table to use (default: live)")
    ap.add_argument("--book", default="LIVE", help="Book ID (default: LIVE)")
    ap.add_argument("--out", default=None,
                    help="output path (default: peaks_<BOOK>.csv)")
    ap.add_argument("--check", metavar="PEAKFILE", default=None,
                    help="verify an existing peak file instead of writing one")
    ap.add_argument("--install", action="store_true",
                    help="write straight into the NinjaTrader PropRouter directory, "
                         "naming the file from --book so the Book ID is never retyped")
    ap.add_argument("--nt8-dir", default=None, dest="nt8_dir",
                    help="override the NinjaTrader PropRouter directory used by --install")
    args = ap.parse_args(argv)

    seat_config = SEAT_CONFIGS[args.seats]

    try:
        validate_seat_config(seat_config, args.seats)

        if args.statement and args.seats == "sim":
            raise SeedError(chr(10).join([
                "--seats sim does not take a broker statement.",
                "",
                "  Simulation accounts have no broker statement. Peaks come from",
                "  SEAT_CONFIG_SIM with peak = start, which is correct while those",
                "  accounts are untraded.",
                "",
                "  Run instead:",
                "      python make_peak_file.py --seats sim --book %s --install" % args.book,
                "",
                "  If they have already traded, reset them in NinjaTrader and re-run.",
                "  Do not hand-write a statement to approximate their peaks: every",
                "  number in it must satisfy the same checks as a real broker export,",
                "  including peak - threshold == the configured drawdown.",
            ]))

        if args.statement:
            rows = read_statement(args.statement)
            seeds = [build_seed(r, seat_config) for r in rows]
            missing = set(seat_config) - set(s["account"] for s in seeds)
            if missing:
                raise SeedError("statement is missing configured seats: %s"
                                % ", ".join(sorted(missing)))
            source = args.statement
        elif args.seats == "live":
            raise SeedError("a broker statement is required for --seats live")
        else:
            seeds = build_untraded_seeds(seat_config)
            source = "SEAT_CONFIG_SIM (no statement; accounts assumed untraded)"
    except SeedError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    print("Prepared %d seats from %s" % (len(seeds), source))
    if not args.statement:
        print("")
        print("  Seeded at the starting balance, which is correct ONLY while these")
        print("  accounts have never traded. If they have, reset them in NinjaTrader")
        print("  and re-run - a peak below the real high-water mark overstates headroom.")
    report(seeds)

    if args.check:
        print("")
        print("Checking %s against %s:"
              % (args.check, "the statement" if args.statement else "SEAT_CONFIG"))
        try:
            ok = check_existing(seeds, args.check)
        except SeedError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2
        print("")
        print("Peak file is current."
              if ok else "Peak file is STALE - re-run without --check to rewrite.")
        return 0 if ok else 1

    if args.install and args.out:
        print("ERROR: use either --install or --out, not both", file=sys.stderr)
        return 2

    if args.install:
        state_dir = args.nt8_dir or nt8_state_dir()
        if not state_dir:
            print("ERROR: could not locate the NinjaTrader 8 directory. Pass "
                  "--nt8-dir with the PropRouter folder path.", file=sys.stderr)
            return 2
        try:
            if not os.path.isdir(state_dir):
                os.makedirs(state_dir)
        except OSError as exc:
            print("ERROR: cannot create %s: %s" % (state_dir, exc), file=sys.stderr)
            return 2
        out = os.path.join(state_dir, "peaks_%s.csv" % args.book)
    else:
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
    if args.install:
        print("  1. The file is already in place. Every strategy in book %r must have"
              % args.book)
        print("     been stopped when this ran - the router rewrites the file on every")
        print("     new equity high and would have clobbered it.")
        print("  2. Fully restart NinjaTrader. Disabling and re-enabling a strategy does")
        print("     NOT reload the peak file; the load is latched per book per process.")
        print("  3. Start the strategies; each should print 'peak seeded at ...'.")
    else:
        print("  1. Stop every strategy in book %r before copying this into place. The"
              % args.book)
        print("     router rewrites the file on every new equity high and would clobber it.")
        print("  2. Copy to:  %s" % os.path.join(
            nt8_state_dir() or "<Documents>/NinjaTrader 8/PropRouter",
            os.path.basename(out)))
        print("  3. Fully restart NinjaTrader, then start the strategies; each should")
        print("     print 'peak seeded at ...'.")
    print("")
    print("  The file name carries the Book ID. This file only serves charts whose")
    print("  Book ID property is exactly %r. A different Book ID looks for a" % args.book)
    print("  different file, finds nothing, and every seat reports PEAK NOT SEEDED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
