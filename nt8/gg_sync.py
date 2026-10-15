#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate the GG router and the routed GG strategy from their RTL sources.

GG is never edited by hand. Each GG file is produced mechanically from the RTL
file it mirrors, so the two routers cannot drift apart and every future RTL fix
reaches GG by re-running this script:

    PropRouter.cs                        -> PropRouterGG.cs
    RR_..._Routed_MaxRedRun.cs           -> GG_..._Routed_MaxGreenRun.cs
    tests/PropRouterRegressionTests.cs   -> tests/PropRouterGGRegressionTests.cs

Router (PropRouterGG.cs)
  * Every router type gets a GG suffix (PropRouterGG, SeatStatusGG, ...). The
    names must be unique, not merely in another namespace: NinjaTrader serializes
    strategy properties, and two enums sharing one simple name break that.
  * The state folder becomes Documents\\NinjaTrader 8\\PropRouterGG, so GG peak
    files, routing logs and blocked-order lists can never collide with RTL ones.
  * One GG-only addition: Register() refuses an account listed in any RTL peak
    file. One account is one drawdown container across BOTH routers, and the
    cross-book check inside a router cannot see into the other router class.

Strategy (GG_..._Routed_MaxGreenRun.cs)
  * The RR->GG transform that RT30_min_nt8_standalone/verify_stop_timeout.ps1
    defines for the standalone pair: strategy identity, red<->green wording,
    MaxRedRun -> MaxGreenRun, red circle -> green circle.
  * EVERY Close/Open comparison is mirrored (< <-> >, >= <-> <=), so "red",
    "not red" and "red run broken" become "green", "not green" and "green run
    broken". The standalone transform replaces two literal lines; the routed
    source has three comparisons and two of them would slip past those literals.
    Generation refuses to run if any Open[...] use is not a recognised comparison.
  * Bound to PropRouterGG instead of PropRouter.

The transform is self-tested against the standalone RR/GG pair whenever the
RT30_min_nt8_standalone project sits next to this one.

Usage:
    python gg_sync.py            ---> verify the GG files match their RTL sources (exit 1 on drift)
    python gg_sync.py --write    ---> regenerate after changing an RTL source, then review the diff
"""

from __future__ import print_function

import argparse
import difflib
import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STANDALONE_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "RT30_min_nt8_standalone")

RTL_ROUTER = "PropRouter.cs"
GG_ROUTER = "PropRouterGG.cs"
RTL_STRATEGY = "RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxRedRun.cs"
GG_STRATEGY = "GG_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed_MaxGreenRun.cs"
RTL_TESTS = os.path.join("tests", "PropRouterRegressionTests.cs")
GG_TESTS = os.path.join("tests", "PropRouterGGRegressionTests.cs")

ROUTED_IDENTITY = (
    ("RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRoutedMaxRedRun",
     "GGLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRoutedGreenRun", 2),
)

STANDALONE_RR = "RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_MaxRedRun.cs"
STANDALONE_GG = "GG_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_MaxGreenRun.cs"
STANDALONE_IDENTITY = (
    ("RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsMaxRedRun",
     "GGLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsGreenRun", 2),
)

ROUTER_TYPES = ("PropRouterMode", "PropRouter", "SeatStatus", "PropSeat",
                "PropBook", "PeakRecord", "RoutingDecision")
ROUTER_TYPE_RE = re.compile(r"\b(%s)\b" % "|".join(ROUTER_TYPES))

COLOR = {"RED": "GREEN", "Red": "Green", "red": "green", "reds": "greens",
         "Green": "Red", "green": "red"}
COLOR_RE = re.compile(r"\b(RED|Red|red|reds|Green|green)\b")
UNSUPPORTED_COLOR_RE = re.compile(r"\b(GREEN|greens|Reds|Greens|REDS|GREENS)\b")

CANDLE_CMP_RE = re.compile(
    r"\b(Close|Open)(\s*\[[^\]\n]*\]\s*)(<=|>=|==|!=|<|>)(\s*)(Close|Open)(\s*\[[^\]\n]*\])")
MIRROR = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}
OPEN_USE_RE = re.compile(r"\bOpen\s*\[")

RED_CIRCLE = u"\U0001F534"
GREEN_CIRCLE = u"\U0001F7E2"

# ---------------------------------------------------------------------------
# GG-only router additions. Indented with tabs, like PropRouter.cs.
# ---------------------------------------------------------------------------
REGISTER_ANCHOR = ("\t\t\t\tPeakRecordGG record;\n"
                   "\t\t\t\tif (b.PeakRecords.TryGetValue(normalizedAccount, out record)")

REGISTER_GUARD = (
    "\t\t\t\t// GG ONLY (gg_sync.py). This router is a separate class from the RTL\n"
    "\t\t\t\t// PropRouter, so the cross-book loop above cannot see RTL seats. Refuse an\n"
    "\t\t\t\t// account listed in any RTL peak file: one account is one drawdown\n"
    "\t\t\t\t// container across BOTH routers.\n"
    "\t\t\t\tif (!EnsureNotAnRtlSeat(normalizedAccount, accountName, out reason))\n"
    "\t\t\t\t\treturn false;\n"
    "\n"
)

STATE_DIR_ANCHOR = (
    "\t\tprivate static string StateDir\n"
    "\t\t{\n"
    "\t\t\tget { return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, \"PropRouterGG\"); }\n"
    "\t\t}\n"
)

RTL_GUARD_METHODS = (
    "\n"
    "\t\t// GG ONLY (gg_sync.py). The RTL router's state folder. Its peak files list\n"
    "\t\t// every account that can trade an RTL book: a seat without a peak row is\n"
    "\t\t// never eligible there. OverridePeak can also seed a seat, but no strategy\n"
    "\t\t// calls it and it persists the row it creates.\n"
    "\t\tprivate static string RtlStateDir\n"
    "\t\t{\n"
    "\t\t\tget { return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, \"PropRouter\"); }\n"
    "\t\t}\n"
    "\n"
    "\t\t// Caller must hold the lock. Fails closed: when the RTL files cannot be\n"
    "\t\t// read, the account cannot be proven free, so the seat is refused.\n"
    "\t\tprivate static bool EnsureNotAnRtlSeat(string normalizedAccount, string accountName,\n"
    "\t\t\tout string reason)\n"
    "\t\t{\n"
    "\t\t\treason = string.Empty;\n"
    "\t\t\tstring dir = RtlStateDir;\n"
    "\t\t\ttry\n"
    "\t\t\t{\n"
    "\t\t\t\tif (!Directory.Exists(dir))\n"
    "\t\t\t\t\treturn true;\n"
    "\n"
    "\t\t\t\tforeach (string file in Directory.GetFiles(dir, \"peaks_*.csv\"))\n"
    "\t\t\t\t{\n"
    "\t\t\t\t\t// The legacy 8.3 pattern match also accepts longer extensions.\n"
    "\t\t\t\t\tif (!file.EndsWith(\".csv\", StringComparison.OrdinalIgnoreCase))\n"
    "\t\t\t\t\t\tcontinue;\n"
    "\n"
    "\t\t\t\t\tstring[] lines = File.ReadAllLines(file);\n"
    "\t\t\t\t\tfor (int i = 1; i < lines.Length; i++)\n"
    "\t\t\t\t\t{\n"
    "\t\t\t\t\t\tstring line = lines[i];\n"
    "\t\t\t\t\t\tint comma = line.IndexOf(',');\n"
    "\t\t\t\t\t\tstring listed = NormalizeAccount(comma >= 0 ? line.Substring(0, comma) : line);\n"
    "\t\t\t\t\t\tif (listed.Length > 0 && string.Equals(listed, normalizedAccount,\n"
    "\t\t\t\t\t\t\tStringComparison.OrdinalIgnoreCase))\n"
    "\t\t\t\t\t\t{\n"
    "\t\t\t\t\t\t\treason = string.Format(CultureInfo.InvariantCulture,\n"
    "\t\t\t\t\t\t\t\t\"account '{0}' is an RTL router seat (listed in {1}); one account is one \"\n"
    "\t\t\t\t\t\t\t\t+ \"drawdown container and cannot trade in both the RTL and GG routers\",\n"
    "\t\t\t\t\t\t\t\taccountName, file);\n"
    "\t\t\t\t\t\t\treturn false;\n"
    "\t\t\t\t\t\t}\n"
    "\t\t\t\t\t}\n"
    "\t\t\t\t}\n"
    "\t\t\t\treturn true;\n"
    "\t\t\t}\n"
    "\t\t\tcatch (Exception ex)\n"
    "\t\t\t{\n"
    "\t\t\t\treason = string.Format(CultureInfo.InvariantCulture,\n"
    "\t\t\t\t\t\"cannot verify that account '{0}' is not an RTL router seat ({1}: {2}); \"\n"
    "\t\t\t\t\t+ \"refusing the seat rather than risk one account in both routers\",\n"
    "\t\t\t\t\taccountName, ex.GetType().Name, ex.Message);\n"
    "\t\t\t\treturn false;\n"
    "\t\t\t}\n"
    "\t\t}\n"
)


class SyncError(Exception):
    pass


def read_source(path):
    """Return (text with LF newlines, had_bom, original newline)."""
    with io.open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, newline


def encode(text, bom, newline):
    if newline != "\n":
        text = text.replace("\n", newline)
    data = text.encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def replace_exact(text, old, new, count, label):
    found = text.count(old)
    if found != count:
        raise SyncError("%s: expected %d occurrence(s) of %r, found %d. The RTL source "
                        "changed shape; update gg_sync.py deliberately."
                        % (label, count, old, found))
    return text.replace(old, new)


def banner(source):
    return ("// =============================================================================\n"
            "// GENERATED FILE - DO NOT EDIT BY HAND.\n"
            "// Produced by nt8/gg_sync.py from %s.\n"
            "// Change the RTL source instead, run `python gg_sync.py --write`, review the\n"
            "// diff, and confirm `python gg_sync.py` then reports every file up to date.\n"
            "// =============================================================================\n"
            % source.replace(os.sep, "/"))


def rename_router_types(text):
    return ROUTER_TYPE_RE.sub(lambda m: m.group(1) + "GG", text)


def mirror_candles(text, label):
    """Turn every red test into the matching green test, and vice versa."""
    swapped = []

    def swap(m):
        if m.group(1) == m.group(5):
            raise SyncError("%s: %r compares a series with itself" % (label, m.group(0)))
        new = m.group(1) + m.group(2) + MIRROR[m.group(3)] + m.group(4) + m.group(5) + m.group(6)
        swapped.append((m.group(0), new))
        return new

    out = CANDLE_CMP_RE.sub(swap, text)
    opens = len(OPEN_USE_RE.findall(text))
    if opens != len(swapped):
        raise SyncError(
            "%s: %d Open[...] use(s) but %d recognised Close/Open comparison(s). Candle-"
            "colour logic in any other form cannot be mirrored mechanically; review it by "
            "hand and extend gg_sync.py." % (label, opens, len(swapped)))
    return out, swapped


def rr_to_gg(text, identity, label):
    """The RR->GG strategy transform verified by the RT30 standalone pair."""
    bad = sorted(set(UNSUPPORTED_COLOR_RE.findall(text)))
    if bad:
        raise SyncError("%s: colour word(s) %s are outside the RR->GG map; extend it "
                        "deliberately" % (label, ", ".join(bad)))
    for old, new, count in identity:
        text = replace_exact(text, old, new, count, label)
    text = text.replace("MaxRedRun", "MaxGreenRun").replace("redRun", "greenRun")
    text = text.replace("max_red_run", "max_green_run")
    text = COLOR_RE.sub(lambda m: COLOR[m.group(0)], text)
    text = text.replace(RED_CIRCLE, GREEN_CIRCLE)
    return mirror_candles(text, label)


def build_router(text):
    text = rename_router_types(text)
    text = replace_exact(text, REGISTER_ANCHOR, REGISTER_GUARD + REGISTER_ANCHOR, 1, GG_ROUTER)
    text = replace_exact(text, STATE_DIR_ANCHOR, STATE_DIR_ANCHOR + RTL_GUARD_METHODS, 1, GG_ROUTER)
    return text


def build_strategy(text):
    text, swapped = rr_to_gg(text, ROUTED_IDENTITY, GG_STRATEGY)
    if "using NinjaTrader.NinjaScript.AddOns;" not in text:
        raise SyncError("%s: no 'using NinjaTrader.NinjaScript.AddOns;' - router binding "
                        "changed shape" % RTL_STRATEGY)
    return rename_router_types(text), swapped


def generate():
    """Return [(relative path, bytes)] and the mirrored candle comparisons."""
    outputs = []

    text, bom, nl = read_source(os.path.join(HERE, RTL_ROUTER))
    outputs.append((GG_ROUTER, encode(banner(RTL_ROUTER) + build_router(text), bom, nl)))

    text, bom, nl = read_source(os.path.join(HERE, RTL_STRATEGY))
    strategy, swapped = build_strategy(text)
    outputs.append((GG_STRATEGY, encode(banner(RTL_STRATEGY) + strategy, bom, nl)))

    text, bom, nl = read_source(os.path.join(HERE, RTL_TESTS))
    outputs.append((GG_TESTS, encode(banner(RTL_TESTS) + rename_router_types(text), bom, nl)))
    return outputs, swapped


def normalize_for_parity(text):
    """Line-ending and trailing-whitespace normalization used by verify_stop_timeout.ps1."""
    text = text.replace("\r\n", "\n").rstrip()
    return re.sub(r"(?m)[\t ]+$", "", text)


def standalone_self_test():
    """True/False when the RT30 standalone pair is available, else None."""
    rr_path = os.path.join(STANDALONE_DIR, STANDALONE_RR)
    gg_path = os.path.join(STANDALONE_DIR, STANDALONE_GG)
    if not (os.path.isfile(rr_path) and os.path.isfile(gg_path)):
        return None
    rr, _, _ = read_source(rr_path)
    gg, _, _ = read_source(gg_path)
    expected, _ = rr_to_gg(normalize_for_parity(rr), STANDALONE_IDENTITY, STANDALONE_RR)
    return normalize_for_parity(expected) == normalize_for_parity(gg)


def atomic_write(path, data):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".gg_sync-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def show_diff(current, generated, limit=40):
    old = current.decode("utf-8-sig", "replace").splitlines() if current is not None else []
    new = generated.decode("utf-8-sig", "replace").splitlines()
    lines = list(difflib.unified_diff(old, new, "on disk", "generated", n=1, lineterm=""))
    for line in lines[:limit]:
        # The sources contain emoji; a Windows console codepage cannot print them.
        print("      " + line.encode("ascii", "backslashreplace").decode("ascii"))
    if len(lines) > limit:
        print("      ... %d more diff lines" % (len(lines) - limit))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate or verify the GG router and routed GG strategy.")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the GG files instead of only verifying them")
    args = ap.parse_args(argv)

    try:
        outputs, swapped = generate()
        self_test = standalone_self_test()
    except (SyncError, IOError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    if self_test is None:
        print("Transform self-test: SKIPPED (%s not found)" % STANDALONE_DIR)
    else:
        print("Transform self-test against the standalone RR/GG pair: %s"
              % ("PASS" if self_test else "FAIL"))
        if not self_test:
            print("  The standalone GG no longer equals the RR->GG transform of the standalone",
                  file=sys.stderr)
            print("  RR. GG's definition changed upstream; review it before trusting the routed",
                  file=sys.stderr)
            print("  GG generated here.", file=sys.stderr)

    print("Candle comparisons mirrored in the routed strategy:")
    for old, new in swapped:
        print("  %-40s -> %s" % (old, new))

    drift = []
    for rel, data in outputs:
        path = os.path.join(HERE, rel)
        current = None
        if os.path.exists(path):
            with io.open(path, "rb") as fh:
                current = fh.read()
        if current == data:
            print("  up to date  %s" % rel)
        elif args.write:
            atomic_write(path, data)
            print("  WRITTEN     %s" % rel)
        else:
            drift.append(rel)
            print("  DRIFT       %s" % rel)
            show_diff(current, data)

    if drift:
        print("")
        print("%d GG file(s) differ from what the RTL sources generate. If the RTL source"
              % len(drift))
        print("changed, run `python gg_sync.py --write`. If a GG file was edited by hand,")
        print("move that change into the RTL source and regenerate.")
    if args.write:
        print("")
        print("Next: copy PropRouterGG.cs into Documents\\NinjaTrader 8\\bin\\Custom\\AddOns\\,")
        print("the GG strategy into ...\\bin\\Custom\\Strategies\\, then compile in the")
        print("NinjaScript Editor and restart any running GG instances.")
    return 1 if drift or self_test is False else 0


if __name__ == "__main__":
    sys.exit(main())
