"""
account_farming.py
==================
Buy seats, harvest the survivors.

One tool. It absorbs the staggered-book simulator that used to live in
`prop_account_staggering(LEGACY).py` (sweep loader, start triggers, per-account
output) and keeps the three things that script did not model:

  1. SINGLE-POSITION REPLAY. The EA holds one position at a time, so when every
     hourly window is enabled they compete for one slot. The sweep files were
     each generated with only their own window active, so simply concatenating
     them counts entries that could never have been taken — about 26% of them
     on this data set. The old script printed a warning about the overlap; this
     one replays the merged stream and drops any entry that arrives while the
     slot is busy, so there is nothing left to warn about.

  2. WITHDRAWALS AS SEED CAPITAL. Once an account's trailing drawdown freezes,
     its floor is nailed at the frozen floor forever, so its cushion is just
     equity minus that floor — every dollar withdrawn makes it more fragile.
     But at a few hundred dollars a seat, withdrawn cash BUYS MORE SEATS.
     Harvesting is therefore not only a safety-for-cash trade, it is the only
     way to fund growth. An account that dies having paid for three
     replacements was a good account.

  3. THE DISTRIBUTION, NOT ONE PATH. Bootstrapping a book has an absorbing
     state: lose every seat while holding less than one seat's price in cash and
     it is over permanently. That makes the process chaotic — adjacent
     withdrawal levels can differ by orders of magnitude on a single run. Every
     policy is therefore scored over many overlapping fixed-length windows and
     reported as median / p10 / p90 / ruin rate.

ACCOUNT RULE (Legacy Performance Account):
    floor = min(peak_unrealized - dd, frozen_floor)
    Safety Net = dd + frozen_floor of peak profit freezes the floor.
The Safety Net is DERIVED, not a free parameter: the moment peak profit reaches
dd + frozen_floor, `peak - dd` overtakes `frozen_floor` and the min() pins the
floor there by itself. "Frozen" is a label for that crossing, not a separate
rule. Reaching it is the whole game — after it the account can absorb a full
trailing drawdown and never dies from trailing again.

Intraday matters, because the rule says *unrealized*: a trade that runs to +MFE
raises the floor even if it closes lower, and one that dips to -MAE can hit the
floor without ever closing there. Closed-trade P&L understates both. The sweep
gives the two extremes but not their order within a trade, so `--intratrade-path`
stays a switch. MAE-first is the default because it reproduced MT5's equity
drawdown exactly on every pass tested in the source project; MFE-first is the
optimistic case, since lifting the floor before the dip can save a seat.

CAUTION: this is LEVERAGE, not alpha. Every seat trades identical signals and
differs only by start date. N seats is N contracts, and a drawdown deep enough
to kill one is deep enough to kill the book.

    python account_farming.py --dd 2500 --cost 200 --seats 20 --seed 1200
    python account_farming.py --interval-days 14 --start-policy any
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
DEPOSIT = 5000.0            # tester deposit, used only to spot liquidated passes

# Round-turn commission. The sweep exports carry a seventh column that is NOT a
# commission (too large, and it tracks trade risk), so the real cost has to be
# applied here. This is the broker's actual figure and is not a tuning knob.
COMMISSION_ROUNDTURN = 1.05

# Reference figures from an MT5 run of the same configuration (RR strategy, all
# windows, RR 1.0, 2020-01 to 2026-07). Kept as a reconstruction check: if the
# merge below cannot approximately reproduce these, nothing downstream is valid.
MT5_REF = {"trades": 9775, "gross": 46343.50, "eq_dd": 6069.00}


@dataclass(frozen=True)
class Rule:
    """The account rule. Both the single-seat study and the book use only this."""
    dd: float = 2500.0              # trailing drawdown
    frozen_floor: float = 100.0     # where the floor stops once the peak is high enough
    cost: float = 200.0             # price of a seat
    mfe_first: bool = False         # intra-trade ordering; False == MAE first

    @property
    def safety_net(self) -> float:
        return self.dd + self.frozen_floor


@dataclass(frozen=True)
class Harvest:
    """When and how much cash comes out of a frozen seat.

    `level` strips the seat down to a fixed equity. It banks the most cash and it
    is also what synchronises a book: two seats with different start dates hold
    different equity until they both freeze and get reset to the same number,
    after which they are identical and die on the same trade.

    `ratchet` instead takes one `chunk` per `step` of lifetime gain, so the seat
    keeps a fixed share of everything it earns. chunk/step is the withdrawal
    rate: 200/1000 means take 20%, leave 80% in as cushion. Seats keep their
    dispersion, so the book does not converge.
    """
    mode: str = "level"             # level | ratchet | none
    keep: float = 0.0               # level: withdraw down to this equity
    chunk: float = 200.0            # ratchet: withdraw in units of this
    step: float = 1000.0            # ratchet: one chunk per this much gain

    @property
    def label(self) -> str:
        if self.mode == "none":
            return "never"
        if self.mode == "level":
            return f"strip to ${self.keep:,.0f}"
        return f"${self.chunk:,.0f} per ${self.step:,.0f} gained"

    @property
    def rate(self) -> float:
        return self.chunk / self.step if self.mode == "ratchet" else 0.0


@dataclass
class BookCfg:
    """How the book buys seats. The start triggers came from the old simulator."""
    seats: int = 20
    seed: float = 1200.0
    interval_days: int = 30
    policy: str = "time"            # time | profit | dd | any
    profit_trigger: float = 1000.0
    dd_trigger: float = 400.0
    min_days: int = 1
    adaptive: bool = False
    # Seats bought on one trade are identical, so a wave of k is k times the
    # exposure with none of the diversification. 1 is the staggering fix; the
    # old "buy everything affordable" behaviour is max_per_event = seats.
    max_per_event: int = 1
    funding: str = "cash"           # cash = bootstrap | external = subscription


# ---------------------------------------------------------------- input ----
def sweep_files(root: Path, rr: float, windows: str) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Sweep root does not exist: {root}")
    wanted = None if windows.lower() == "all" else {w.strip() for w in windows.split(",")}
    files: list[Path] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted is not None and directory.name not in wanted:
            continue
        candidate = directory / f"{directory.name}_{rr:.2f}.csv"
        if candidate.exists():
            files.append(candidate)
        else:
            print(f"WARNING: no RR={rr:.2f} file for window {directory.name}: {candidate}")
    if not files:
        raise FileNotFoundError("No sweep files matched the selected RR/windows.")
    return files


def read_trade_file(path: Path) -> pd.DataFrame:
    """Read both headerless MT5 sweep exports and the original MAE export."""
    # MT5 exports in this folder are not consistently encoded: most are UTF-8
    # but some were saved as UTF-16 LE.  The content is otherwise identical.
    raw = None
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            raw = pd.read_csv(path, sep="\t", header=None, dtype=str, encoding=encoding)
            break
        except UnicodeDecodeError as error:
            last_error = error
    if raw is None:
        raise last_error or ValueError(f"Could not read {path}")
    if raw.empty:
        return pd.DataFrame(columns=["Ticket", "Entry_time", "Exit_time",
                                     "MAE", "MFE", "PNL"])

    header = str(raw.iloc[0, 0]).strip().lower() in {"ticket", "#", "id"}
    if header:
        columns = [str(value).strip() for value in raw.iloc[0].tolist()]
        raw = raw.iloc[1:].reset_index(drop=True)
        raw.columns = columns
        aliases = {"ticket": "Ticket", "entry_time": "Entry_time",
                   "exit_time": "Exit_time", "mae": "MAE", "mfe": "MFE", "pnl": "PNL"}
        raw = raw.rename(columns={c: aliases.get(c.lower(), c) for c in raw.columns})
    else:
        if raw.shape[1] < 6:
            raise ValueError(f"{path} has {raw.shape[1]} columns; expected at least 6.")
        # The headerless sweep export has a seventh, undocumented field.  It is
        # not a commission (far too large, and it tracks trade risk), so keep it
        # as Extra and charge COMMISSION_ROUNDTURN instead.
        names = ["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL", "Extra"]
        raw = raw.iloc[:, :len(names)]
        raw.columns = names[:raw.shape[1]]

    required = ["Entry_time", "Exit_time", "MAE", "MFE", "PNL"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
    if "Ticket" not in raw:
        raw["Ticket"] = np.arange(1, len(raw) + 1, dtype=int)

    for column in ("MAE", "MFE", "PNL"):
        raw[column] = pd.to_numeric(
            raw[column].astype(str).str.replace(",", ".", regex=False).str.strip(),
            errors="coerce").fillna(0.0)
    for column in ("Entry_time", "Exit_time"):
        raw[column] = pd.to_datetime(raw[column], errors="coerce")
    raw = raw.dropna(subset=["Entry_time", "Exit_time"]).copy()
    return raw[["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL"]]


def pass_is_blown(stats_root: Path, window: str, rr: float):
    """True if MT5 liquidated this pass, so its per-trade export is truncated.

    The tester force-closes on a margin call and the EA's export hook never
    fires, so the fatal trade is missing from the file. Scoring such a pass
    would invent a result. Returns None when no stats file is available.
    """
    path = stats_root / window / f"{window}_{rr:.2f}_stats.csv"
    if not path.is_file():
        return None
    for encoding in ("utf-16", "utf-8", "cp1252"):
        try:
            stats = pd.read_csv(path, sep="\t", encoding=encoding)
            break
        except (UnicodeError, UnicodeDecodeError):
            continue
    else:
        return None
    try:
        row = stats.iloc[0]
        return (float(row["equity_dd"]) >= DEPOSIT * 0.95
                or float(row["net_profit"]) <= -DEPOSIT * 0.95)
    except (KeyError, IndexError, ValueError):
        return None


def replay(parts):
    """One-position replay: keep an entry only if the slot is free."""
    order = []
    for pi, t in enumerate(parts):
        for i in range(len(t["en"])):
            order.append((t["en"][i], t["ex"][i], pi, i))
    order.sort(key=lambda x: (x[0], x[1]))
    keep = [np.zeros(len(t["en"]), dtype=bool) for t in parts]
    open_until = np.datetime64("1970-01-01")
    for en, ex, pi, i in order:
        if en >= open_until:
            keep[pi][i] = True
            open_until = ex
    return keep


def _part(frame: pd.DataFrame) -> dict:
    d = frame.sort_values("Exit_time")
    return {
        "en": d["Entry_time"].values.astype("datetime64[s]"),
        "ex": d["Exit_time"].values.astype("datetime64[s]"),
        "net": (d["PNL"].values - COMMISSION_ROUNDTURN).astype(np.float64),
        "mae": d["MAE"].values.astype(np.float64),
        "mfe": d["MFE"].values.astype(np.float64),
    }


def build_stream(a) -> dict:
    """The trade stream one seat actually trades, after single-position replay."""
    if a.input_csv:
        parts, kept, blown, unknown = [_part(read_trade_file(a.input_csv))], \
            [a.input_csv.name], [], []
    else:
        parts, kept, blown, unknown = [], [], [], []
        for path in sweep_files(a.sweep_root, a.rr, a.windows):
            window = path.parent.name
            state = pass_is_blown(a.stats_root, window, a.rr)
            if state is True:
                blown.append(window)
                continue
            if state is None:
                unknown.append(window)
            parts.append(_part(read_trade_file(path)))
            kept.append(window)

    keep = replay(parts)
    rows = []
    for t, m in zip(parts, keep):
        for i in np.flatnonzero(m):
            rows.append((t["ex"][i], t["net"][i], t["mae"][i], t["mfe"][i]))
    rows.sort(key=lambda r: r[0])

    ex = np.array([r[0] for r in rows])
    mask = np.ones(len(ex), dtype=bool)
    if a.start_date:
        mask &= ex >= np.datetime64(pd.Timestamp(a.start_date))
    if a.end_date:
        mask &= ex < np.datetime64(pd.Timestamp(a.end_date) + pd.Timedelta(days=1))
    if not mask.any():
        raise ValueError("No trades remain after the date filter.")

    return {
        "ex": ex[mask],
        "net": np.array([r[1] for r in rows])[mask],
        "mae": np.array([r[2] for r in rows])[mask],
        "mfe": np.array([r[3] for r in rows])[mask],
        "windows": kept, "blown": blown, "unknown": unknown,
        "offered": sum(len(p["en"]) for p in parts),
        "taken": sum(int(m.sum()) for m in keep),
    }


# --------------------------------------------------------------- metrics ----
def dd_equity(net, mae, mfe):
    """MAE-then-MFE equity drawdown — the ordering validated against MT5."""
    eq = peak = mdd = 0.0
    for n, a, f in zip(net, mae, mfe):
        mdd = max(mdd, peak - (eq + min(a, 0.0)))
        peak = max(peak, eq + max(f, 0.0))
        eq += n
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return float(mdd)


def dd_paths(ex, net, mae, mfe) -> pd.DataFrame:
    """Closed and floating drawdown of the merged stream, as series.

    Closed DD measures the cumulative P&L of the trades as they settle.
    Floating DD adds the intra-trade extremes, which is what the account rule
    actually watches. The gap between the two is the whole reason MAE and MFE
    have to be modelled at all.
    """
    eq = peak = closed_peak = 0.0
    rows = []
    for i in range(len(net)):
        low = eq + min(mae[i], 0.0)
        floating = low - peak
        peak = max(peak, eq + max(mfe[i], 0.0))
        eq += net[i]
        peak = max(peak, eq)
        closed_peak = max(closed_peak, eq)
        rows.append((pd.Timestamp(ex[i]), eq, closed_peak, low,
                     eq - closed_peak, min(floating, eq - peak)))
    return pd.DataFrame(rows, columns=["day", "eq", "peak", "low",
                                       "dd_closed", "dd_float"])


# -------------------------------------------------------------- accounts ----
def new_account(i0, rule: Rule):
    return {"i0": i0, "eq": 0.0, "peak": 0.0, "floor": -rule.dd,
            "frozen": False, "banked": 0.0, "alive": True}


def _lift(a, mf, rule: Rule):
    """Raise the unrealized peak, and the floor with it."""
    a["peak"] = max(a["peak"], a["eq"] + max(mf, 0.0))
    a["floor"] = min(a["peak"] - rule.dd, rule.frozen_floor)


def due(a, rule: Rule, h: Harvest) -> float:
    """Cash a frozen seat should pay out now under the withdrawal policy.

    Neither mode ever takes equity below the Safety Net: at that point the seat's
    cushion is exactly one full trailing drawdown, and taking more buys cash with
    the seat's life.
    """
    if not a["frozen"] or h.mode == "none":
        return 0.0
    if h.mode == "level":
        return max(0.0, a["eq"] - h.keep) if h.keep else 0.0
    value = a["eq"] + a["banked"]                      # unaffected by withdrawing
    target = h.chunk * math.floor(max(0.0, value - rule.safety_net) / h.step)
    return max(0.0, min(target - a["banked"], a["eq"] - rule.safety_net))


def step(a, n, ma, mf, h: Harvest, rule: Rule):
    """Advance one account by one trade. Returns cash withdrawn on this trade."""
    if rule.mfe_first:                                 # optimistic ordering
        _lift(a, mf, rule)
    if a["eq"] + min(ma, 0.0) <= a["floor"]:           # intraday dip kills it
        a["alive"] = False
        return 0.0
    if not rule.mfe_first:                             # MAE-first: lift after
        _lift(a, mf, rule)
    a["eq"] += n
    a["peak"] = max(a["peak"], a["eq"])
    a["floor"] = min(a["peak"] - rule.dd, rule.frozen_floor)
    if a["eq"] <= a["floor"]:
        a["alive"] = False
        return 0.0
    if not a["frozen"] and a["peak"] >= rule.safety_net:
        a["frozen"] = True
    w = due(a, rule, h)
    if w > 0.0:
        a["eq"] -= w
        a["banked"] += w
    return w


def run_account(net, mae, mfe, i0, h: Harvest, rule: Rule):
    a = new_account(i0, rule)
    a["froze_i"] = None
    for i in range(i0, len(net)):
        step(a, net[i], mae[i], mfe[i], h, rule)
        if not a["alive"]:
            a["dead_i"] = i
            return a
        if a["frozen"] and a["froze_i"] is None:
            a["froze_i"] = i
    a["dead_i"] = None
    return a


# ------------------------------------------------------------------ book ----
def should_start(live, day, last_start, cfg: BookCfg) -> bool:
    """Start triggers, carried over from the staggering simulator."""
    if last_start is None:
        return True
    days = (day - last_start).days
    if days < cfg.min_days:
        return False
    if cfg.policy in ("time", "any") and days >= cfg.interval_days:
        return True
    if cfg.policy in ("profit", "any") and live and live[-1]["eq"] >= cfg.profit_trigger:
        return True
    if cfg.policy in ("dd", "any") and live and \
            min(a["eq"] - a["peak"] for a in live) <= -cfg.dd_trigger:
        return True
    return False


def run_book(ex, net, mae, mfe, h: Harvest, rule: Rule, cfg: BookCfg, trace=False):
    """A book of seats, funded either by withdrawals or from outside.

    cfg.funding == "cash" is the bootstrap: seats are paid for out of money the
    seats themselves withdrew, so the book can die permanently. "external" is the
    subscription: seats are paid for from the trader's own pocket on a fixed
    cadence, so it cannot be ruined and `spent` is the real cost.

    cfg.adaptive harvests hard (down to the Safety Net) while the book is still
    growing, then switches to `h` once the seat limit is reached and extra cash
    no longer buys anything.
    """
    hard = Harvest("level", keep=rule.safety_net)
    cash, live, bought, deaths, withdrawn, spent = cfg.seed, [], 0, 0, 0.0, 0.0
    last_start, series, ruined, done, wipeouts = None, [], False, [], 0
    for i in range(len(net)):
        day = pd.Timestamp(ex[i])
        if len(live) < cfg.seats and should_start(live, day, last_start, cfg):
            room = cfg.seats - len(live)
            if cfg.funding == "external":
                n_buy = min(cfg.max_per_event, room)
            else:
                # Bootstrap: seats are bought only out of `cash`, and the only
                # thing that ever adds to `cash` is a withdrawal from a live
                # seat. A policy that never withdraws therefore buys exactly
                # seed/cost seats and then stops for good.
                n_buy = min(cfg.max_per_event, room, int(cash // rule.cost))
            for _ in range(max(0, n_buy)):
                a = new_account(i, rule)
                a["start"] = day
                if trace:
                    a["path"] = [(day, 0.0)]
                live.append(a)
                if cfg.funding == "external":
                    spent += rule.cost
                else:
                    cash -= rule.cost
                bought += 1
                last_start = day

        k = hard if (cfg.adaptive and len(live) < cfg.seats) else h
        got = 0.0
        for a in live:
            got += step(a, net[i], mae[i], mfe[i], k, rule)
            if trace:
                a["path"].append((day, a["eq"] + a["banked"]))
        cash += got
        withdrawn += got

        n0 = len(live)
        if trace:
            done.extend(a for a in live if not a["alive"])
        for a in live:
            if not a["alive"]:
                a["end"] = day
        live = [a for a in live if a["alive"]]
        deaths += n0 - len(live)
        # Seats bought together are identical, so they die together. Losing the
        # whole book at once is survivable while cash can rebuy it, which is why
        # it never shows up in the ruin rate - count it separately.
        if n0 and not live:
            wipeouts += 1
        if cfg.funding == "cash" and not live and cash < rule.cost:
            ruined = True                              # absorbing state
        equity = sum(a["eq"] for a in live)
        series.append((day, len(live), withdrawn, cash, equity, spent,
                       cash + equity - cfg.seed - spent))

    S = pd.DataFrame(series, columns=["day", "live", "withdrawn", "cash",
                                      "equity", "spent", "wealth"])
    equity = sum(a["eq"] for a in live)
    return {"series": S, "bought": bought, "deaths": deaths, "cash": cash,
            "equity": equity, "withdrawn": withdrawn, "spent": spent,
            "live": len(live), "ruined": ruined, "seats": done + live,
            "wipeouts": wipeouts, "starts": len({a["i0"] for a in done + live}),
            "wealth": cash + equity - cfg.seed - spent}


def monthly(index, values, cumulative: bool):
    """Monthly P&L from either a level series (cumulative) or per-trade flows."""
    s = pd.Series(values, index=pd.to_datetime(index))
    g = s.groupby(s.index.to_period("M"))
    m = g.last() if cumulative else g.sum()
    if cumulative:
        first = m.iloc[0]
        m = m.diff()
        m.iloc[0] = first
    return m


# --------------------------------------------------------------- report ----
_TPL = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Prop-account farming</title>
<script>__PLOTLYJS__</script>
<style>
 body{font:14px/1.45 system-ui,Segoe UI,Arial;margin:0;background:#f4f5f7;color:#1a1a1a}
 header{background:#1f2937;color:#fff;padding:14px 22px}h1{font-size:17px;margin:0}
 header p{margin:4px 0 0;font-size:12.5px;color:#cbd5e1}
 main{max-width:1240px;margin:16px auto;padding:0 16px}
 .cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px}
 .card{background:#fff;border-radius:10px;padding:12px 18px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:165px;flex:1}
 .card .t{font-size:11.5px;color:#6b7280;text-transform:uppercase}
 .card .v{font-size:21px;font-weight:600}.card .v.ok{color:#15803d}.card .v.bad{color:#b91c1c}
 .card .s{font-size:11.5px;color:#6b7280}
 .panel{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 table{border-collapse:collapse;width:100%;font-size:12.4px}
 th{text-align:right;padding:5px 8px;background:#f1f5f9;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 td{padding:4px 8px;text-align:right;border-top:1px solid #eef0f3;white-space:nowrap}
 .note{font-size:12.3px;color:#6b7280;margin:6px 0}
 h2{font-size:15px;margin:18px 0 8px}
 .warn{background:#fef3c7;border-left:3px solid #d97706;padding:10px 14px;border-radius:6px;font-size:12.8px;margin-bottom:16px}
 .warn b{color:#92400e}
 .onepath{background:#eef2ff;border-left:3px solid #4f46e5;padding:8px 12px;border-radius:6px;font-size:12.3px;color:#3730a3;margin:6px 0}
 .pickwrap{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:2px 0 10px}
 .picklabel{font-size:11.5px;color:#6b7280;text-transform:uppercase;letter-spacing:.03em}
 .picks{display:flex;flex-wrap:wrap;gap:6px}
 .pick{font:inherit;font-size:12.2px;padding:4px 10px;border:1px solid #d3d8de;
  background:#fff;border-radius:999px;cursor:pointer;color:#374151}
 .pick:hover{border-color:#9aa3ad;background:#f8fafc}
 .pick.on{background:#1f2937;border-color:#1f2937;color:#fff}
 .pick .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
 .pstat{display:flex;flex-wrap:wrap;gap:0 22px;font-size:12.4px;color:#374151;
  background:#f8fafc;border:1px solid #eef0f3;border-radius:8px;padding:8px 12px;margin-bottom:10px}
 .pstat b{color:#111;font-weight:600}
 .pstat .bad{color:#b91c1c}.pstat .ok{color:#15803d}
</style></head><body>
<header><h1>Prop-account farming &mdash; buy seats, harvest the survivors</h1>
<p>RR strategy, every window, RR __RRV__, $__DDV__ trailing drawdown, $__COSTV__ per seat,
$__COMMV__ round-turn commission, a new seat every __IVLV__ days.</p>
</header><main>
<div class="cards" id="cards"></div>
<div class="warn"><b>This is leverage, not alpha.</b> Every seat trades identical
signals and differs only by start date. N seats is N contracts, and a drawdown deep
enough to kill one is deep enough to kill the book. The totals below scale with seat
count; the per-seat figures are what actually measure the strategy.</div>

<h2>1 &middot; Does a seat reach the Safety Net? By start date</h2>
<div class="panel"><div id="c_starts" style="height:380px"></div>
 <div class="note">Green = reached the Safety Net, plotted at the number of days it took.
 Red = died first. The red clusters are what matters: seats started near each other share
 one fate, so staggering spreads entry points, not outcomes.</div></div>

<h2>2 &middot; Closed vs floating drawdown</h2>
<div class="panel"><div id="c_ddeq" style="height:300px"></div>
 <div id="c_dd" style="height:340px"></div>
 <div class="note">The account rule watches <i>unrealized</i> equity, so the floating line
 is the one that liquidates seats &mdash; the closed line is what a trade list alone would
 show you. The gap between them is the entire reason MAE and MFE have to be modelled;
 scoring this strategy on closed P&amp;L would understate the risk by the depth of that gap.
 Ordering within a trade is __PATHV__.</div></div>

<div class="warn"><b>Why a book of seats dies all at once.</b> Two seats bought on the
same trade are not merely correlated, they are <i>identical</i> &mdash; same rule, same
stream, same withdrawal policy &mdash; so their curves coincide to the cent and they
liquidate on the same trade. Worse, withdrawing <i>down to a level</i> recreates that
condition even for seats with different start dates: they hold different equity right up
until they both freeze and get stripped to the same number, and from then on they are
twins. The withdrawal policy is what synchronises a book. Both fixes are below: buy one
seat at a time, and withdraw a share of gains instead of stripping to a level.</div>

<h2>3 &middot; Mode 1 &mdash; subscription: own money, one seat per interval, hold forever</h2>
<div class="panel"><div id="c_sub" style="height:400px"></div>
 <div class="note">A new seat every __IVLV__ days paid for out of pocket, no withdrawals
 ever. Equity across all live seats on the left axis, seats held on the right. This mode
 cannot be ruined &mdash; funding is external &mdash; so the only questions are how much
 capital it consumes and how many seats it burns through.</div></div>
<div class="panel"><div id="c_sub_seats" style="height:420px"></div>
 <div class="note">Each line is one seat. Because nothing is ever withdrawn, seats never
 get reset to a common equity, so they keep the dispersion their different start dates gave
 them. That is the whole difference from the harvested books below.</div>
 <div class="note"><b>Everything here is unrealized.</b> The equity is inside live prop
 accounts, subject to the firm's rules, and a seat that has never withdrawn has never
 returned a cent. Compare it against the bootstrap's realized cash with that in mind.</div>
 <div class="onepath">One illustrative path, not an expectation. Section 6 has the
 distribution across every window.</div></div>

<h2>4 &middot; Mode 2 &mdash; bootstrap: the seats pay for their own replacements</h2>
<div class="panel">
 <div class="pickwrap"><span class="picklabel">withdrawal policy</span>
  <div class="picks" id="picks"></div></div>
 <div id="p_stat" class="pstat"></div>
 <div id="c_boot" style="height:380px"></div>
 <div class="note">Seats live (filled) against cumulative cash withdrawn. Every step down
 in seat count is a liquidation. Switch policies above &mdash; the axes rescale, so compare
 the shapes and read the numbers off the strip rather than eyeballing heights.</div></div>
<div class="panel"><div id="c_boot_seats" style="height:420px"></div>
 <div class="note">Each distinct purchase date is drawn once; hover for how many seats it
 stands for. These lines are equity <i>plus</i> cash already withdrawn, so they do not
 overlap even when the seats behind them are identical &mdash; the synchronisation is in
 equity alone, which is the only thing the liquidation floor is measured against. What you
 can see is the consequence: switch to a level policy and the lines all turn together and
 stop together, and the seat count above drops to zero in one step. Switch to a ratchet and
 the deaths spread out.</div>
 <div class="onepath">One illustrative path per policy, not an expectation. Section 5 has
 the distribution across every window.</div></div>
<div class="panel"><div id="c_year" style="height:320px"></div>
 <div class="note">Cash per calendar year from the selected policy. The spread between best
 and worst year is the risk this design carries, and it is not smoothed by holding more
 seats, because the seats are not independent.</div>
 <div class="onepath">One illustrative path, not an expectation.</div></div>

<h2>5 &middot; Both modes, across every window</h2>
<div class="panel"><div id="c_keep" style="height:560px"></div>
 <div class="note">Median realized cash across every __NWIN__ overlapping __HZV__-year
 window; whiskers are p10 to p90. Read the whisker, not the bar &mdash; the spread is wider
 than the difference between most policies. The subscription bar is zero by construction:
 it never withdraws, so all of its value sits in the equity column of the table instead.</div></div>
<div class="panel"><div id="c_vs" style="height:400px"></div>
 <div class="note">Net position over time for both illustrative books, on the same axis:
 cash withdrawn plus equity still in live seats, minus every dollar of own capital put in.
 The subscription line is what you would have if you never took a cent out; the bootstrap
 line is mostly money already banked.</div>
 <div class="onepath">One illustrative path each, not an expectation.</div></div>

<h2>6 &middot; Which policy? &mdash; the decision</h2>
<div class="panel"><div id="c_front" style="height:480px"></div>
 <div class="note"><b>How to read this.</b> Up is a better typical outcome; right is a
 better bad case. A policy with another policy above <i>and</i> to the right of it is
 <b>dominated</b> &mdash; strictly worse on both counts, so there is no preference under
 which you would pick it. Those are drawn hollow. The line joins what is left: the
 frontier, where buying more typical outcome costs you downside. Green outlines never lost
 the whole book in any window; red outlines did.</div>
 <div class="note">Both axes are <b>net</b> = cash withdrawn + equity still live &minus;
 own capital spent, over a __HZV__-year window. Net is the only measure the two funding
 modes can share, but it mixes banked cash with equity that can still be lost &mdash; the
 <i>banked</i> share in the hover is how much of it you would actually be holding.</div></div>
<div class="panel" style="overflow:auto" id="t_pick"></div>
<div class="note">Same numbers, sorted by what you might care about. Pick the row whose
constraint is really yours, not the biggest number: these are medians of __NWIN__
overlapping windows on one strategy over one 6.5-year sample, so differences smaller than
the p10&ndash;p90 spread are not differences at all.</div>
<div class="warn"><b>What this cannot tell you.</b> The withdrawal rate is fitted to this
sample &mdash; nothing here is out-of-sample. The windows overlap heavily, so 18 of them is
more like 3&ndash;4 independent periods. And every policy shares one strategy on one
instrument: the frontier says which withdrawal rule was best <i>given</i> that the strategy
kept working, not what happens if it stops.</div>

<h2>7 &middot; Monthly P&amp;L</h2>
<div class="panel"><div id="c_mstrat" style="height:360px"></div>
 <div class="note">The strategy itself, one slot, no account rule and no seats &mdash; the
 baseline everything else is built on. This one <i>is</i> a property of the data rather
 than of a policy choice.</div></div>
<div class="panel"><div id="c_mbook" style="height:360px"></div>
 <div class="note">The business: month-on-month change in cash withdrawn plus equity still
 inside live seats, with seat purchases charged as the expenses they are. Losing months are
 deeper than the strategy's own because every live seat takes the same loss at once.</div>
 <div class="onepath"><b>One illustrative path, not a track record.</b> A different seed,
 start date or withdrawal level moves these bars a long way &mdash; treat the win rate as a
 description of this single run, not an expected hit rate.</div></div>

<h2>8 &middot; Every policy, across all windows</h2>
<div class="panel" style="overflow:auto" id="t_keep"></div>
<div class="note">Cash is realized. Equity is still inside live accounts and can be lost
&mdash; never add the two together and call it profit. <b>net</b> = cash + equity &minus;
own capital spent, which is the only column the two modes can be compared on.
<b>blowups</b> is the median number of seats liquidated per window.</div>
<div class="warn"><b>Read the wipeout column before the cash column.</b> "Ruin" means the
book ended with no seats <i>and</i> too little cash to replace one &mdash; a book that loses
every seat and immediately rebuys them out of withdrawn cash is not ruined by that
definition, but it did go to zero seats. The all-in policy scores highest on median cash
precisely because it carries the most exposure, and it gets there through repeated total
wipeouts. Nothing here is a recommendation; the rows are sorted by nothing but the order
they were defined.</div>

<h2>9 &middot; Reconstruction check</h2>
<div class="panel" style="overflow:auto" id="t_rec"></div>
<div class="note">The merged single-position stream against a real MT5 run of the same
configuration. It runs light: one window's export is tester-blown and excluded, and the
sweeps were each generated in isolation so the merge approximates the blocking rather
than reproducing it.</div>
<div class="note" id="foot"></div>
</main><script>
const D=__DATA__,CFG={displaylogo:false,responsive:true};
const F={family:'system-ui,Segoe UI,Arial',size:11.5};
const BG={plot_bgcolor:'#fff',paper_bgcolor:'#fff'};
document.getElementById('cards').innerHTML=D.cards.map(c=>
 `<div class="card"><div class="t">${c[0]}</div><div class="v ${c[2]||''}">${c[1]}</div>
  <div class="s">${c[3]||''}</div></div>`).join('');
// One colour for every seat, on purpose: the point of these charts is the shared
// shape, and a rotating palette invites you to track individual lines instead.
function seatchart(id,curves,title){
 Plotly.newPlot(id,curves.map(s=>({
  x:s.x,y:s.y,type:'scatter',mode:'lines',showlegend:false,
  line:{width:1.2,color:'#4e79a7'},opacity:.55,
  hovertemplate:'bought '+s.d+(s.n>1?' &times;'+s.n+' seats':'')+
   '<br>%{x|%Y-%m-%d}<br>$%{y:,.0f} each<extra></extra>'})),
  Object.assign({margin:{l:66,r:14,t:28,b:34},font:F,
   title:{text:title,x:0,font:{size:13}},
   shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:D.safety,y1:D.safety,
    line:{color:'#15803d',width:1,dash:'dash'}},
    {type:'line',xref:'paper',x0:0,x1:1,y0:0,y1:0,line:{color:'#9ca3af',width:1}}],
   xaxis:{type:'date',gridcolor:'#eef0f3'},
   yaxis:{title:'$ per seat',gridcolor:'#eef0f3'}},BG),CFG);}
seatchart('c_sub_seats',D.sub.curves,
 'Subscription - every seat, P&L including cash withdrawn (never)');
Plotly.newPlot('c_sub',[
 {x:D.sub.x,y:D.sub.equity,type:'scatter',mode:'lines',name:'portfolio equity',
  fill:'tozeroy',line:{width:1.8,color:'#15803d'},fillcolor:'rgba(21,128,61,.14)'},
 {x:D.sub.x,y:D.sub.spent,type:'scatter',mode:'lines',name:'own capital spent',
  line:{width:1.4,color:'#b91c1c',dash:'dot',shape:'hv'}},
 {x:D.sub.x,y:D.sub.live,type:'scatter',mode:'lines',name:'seats held',
  yaxis:'y2',line:{width:1,color:'#4e79a7',shape:'hv'}}],
 Object.assign({margin:{l:70,r:60,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'Subscription - portfolio equity against capital put in',x:0,
   font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'$',gridcolor:'#eef0f3'},
  yaxis2:{title:'seats',overlaying:'y',side:'right',showgrid:false,rangemode:'tozero'},
  legend:{orientation:'h',y:-.16}},BG),CFG);
Plotly.newPlot('c_vs',[
 {x:D.sub.x,y:D.sub.wealth,type:'scatter',mode:'lines',name:'mode 1 - subscription',
  line:{width:2,color:'#15803d'}},
 {x:D.boot.x,y:D.boot.wealth,type:'scatter',mode:'lines',name:'mode 2 - bootstrap',
  line:{width:2,color:'#4e79a7'}},
 {x:D.boot.x,y:D.boot.cash,type:'scatter',mode:'lines',
  name:'mode 2 - of which already banked',
  line:{width:1.3,color:'#4e79a7',dash:'dot'}}],
 Object.assign({margin:{l:74,r:14,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'Net position - cash plus equity, less own capital spent',x:0,
   font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'$',gridcolor:'#eef0f3'},
  legend:{orientation:'h',y:-.16}},BG),CFG);
Plotly.newPlot('c_starts',[
 {x:D.starts.okx,y:D.starts.oky,type:'scatter',mode:'markers',name:'reached Safety Net',
  marker:{size:5,color:'#59a14f',opacity:.65},
  hovertemplate:'start %{x|%Y-%m-%d}<br>froze after %{y} days<extra></extra>'},
 {x:D.starts.badx,y:D.starts.bady,type:'scatter',mode:'markers',name:'died first',
  marker:{size:6,color:'#e15759',symbol:'x',opacity:.75},
  hovertemplate:'start %{x|%Y-%m-%d}<br>never froze<extra></extra>'}],
 Object.assign({margin:{l:66,r:14,t:28,b:34},font:F,
  title:{text:'Days from purchase to the Safety Net, by start date',x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'days to freeze',gridcolor:'#eef0f3',zeroline:false},
  legend:{orientation:'h',y:-.16}},BG),CFG);
Plotly.newPlot('c_ddeq',[
 {x:D.dd.x,y:D.dd.peak,type:'scatter',mode:'lines',name:'unrealized peak',
  line:{width:1,color:'#59a14f'}},
 {x:D.dd.x,y:D.dd.low,type:'scatter',mode:'lines',name:'unrealized low',
  line:{width:1,color:'#e15759'}},
 {x:D.dd.x,y:D.dd.eq,type:'scatter',mode:'lines',name:'closed equity',
  line:{width:1.8,color:'#111'}}],
 Object.assign({margin:{l:66,r:14,t:28,b:10},font:F,hovermode:'x unified',
  title:{text:'One slot, cumulative P&L with its unrealized envelope',x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3',showticklabels:false},
  yaxis:{title:'$',gridcolor:'#eef0f3'},legend:{orientation:'h',y:-.10}},BG),CFG);
Plotly.newPlot('c_dd',[
 {x:D.dd.x,y:D.dd.closed,type:'scatter',mode:'lines',name:'closed drawdown',
  line:{width:1.6,color:'#4e79a7'},fill:'tozeroy',fillcolor:'rgba(78,121,167,.15)'},
 {x:D.dd.x,y:D.dd.floating,type:'scatter',mode:'lines',name:'floating drawdown',
  line:{width:1.6,color:'#e15759'}}],
 Object.assign({margin:{l:66,r:14,t:26,b:34},font:F,hovermode:'x unified',
  title:{text:'Drawdown - what settled, against what the account rule sees',
   x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'$ below peak',gridcolor:'#eef0f3'},
  shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:-D.dd_limit,y1:-D.dd_limit,
   line:{color:'#b91c1c',width:1.2,dash:'dash'}}],
  annotations:[{xref:'paper',x:0.01,y:-D.dd_limit,text:'a fresh seat dies here',
   showarrow:false,yshift:-10,font:{size:11,color:'#b91c1c'}}],
  legend:{orientation:'h',y:-.18}},BG),CFG);
// Horizontal, because the policy names are sentences. Wipeout-free bootstrap
// policies are green, ones that lost the whole book are red, mode 1 is grey.
Plotly.newPlot('c_keep',[{
 x:D.keep.med,y:D.keep.labels,type:'bar',orientation:'h',
 marker:{color:D.keep.colour},
 error_x:{type:'data',symmetric:false,array:D.keep.hi,arrayminus:D.keep.lo,
  color:'#6b7280',thickness:1.2,width:3},
 customdata:D.keep.sub,
 hovertemplate:'%{y}<br>median $%{x:,.0f}<br>%{customdata}<extra></extra>'}],
 Object.assign({margin:{l:290,r:20,t:28,b:40},font:F,
  title:{text:'Realized cash over a fixed window - median, with p10 to p90',
   x:0,font:{size:13}},
  xaxis:{title:'cash withdrawn $',gridcolor:'#eef0f3'},
  yaxis:{automargin:true,autorange:'reversed'}},BG),CFG);
// ---- policy switcher: one full-period run is stored per policy -------------
const money=v=>(v<0?'-$':'$')+Math.abs(Math.round(v)).toLocaleString();
function drawPolicy(i){
 const b=D.books[i];
 [...document.querySelectorAll('.pick')].forEach((el,j)=>
  el.classList.toggle('on',j===i));
 Plotly.react('c_boot',[
  {x:b.x,y:b.live,type:'scatter',mode:'lines',name:'seats live',
   fill:'tozeroy',line:{width:1,color:'#4e79a7',shape:'hv'},
   fillcolor:'rgba(78,121,167,.22)'},
  {x:b.x,y:b.cash,type:'scatter',mode:'lines',name:'cash withdrawn',
   yaxis:'y2',line:{width:2,color:'#15803d'}},
  {x:b.x,y:b.equity,type:'scatter',mode:'lines',name:'equity in live seats',
   yaxis:'y2',line:{width:1.4,color:'#b07aa1',dash:'dot'}},
  {x:b.x,y:b.hand,type:'scatter',mode:'lines',name:'cash on hand (buying power)',
   yaxis:'y2',line:{width:1.2,color:'#c2760c'}}],
  Object.assign({margin:{l:60,r:70,t:28,b:34},font:F,hovermode:'x unified',
   title:{text:'Seats live, cash out and equity - '+b.name,x:0,font:{size:13}},
   xaxis:{type:'date',gridcolor:'#eef0f3'},
   yaxis:{title:'seats',gridcolor:'#eef0f3',rangemode:'tozero'},
   yaxis2:{title:'$',overlaying:'y',side:'right',showgrid:false},
   legend:{orientation:'h',y:-.16}},BG),CFG);
 seatchart('c_boot_seats',b.curves,
  'Every purchase date - P&L including cash already withdrawn - '+b.name);
 Plotly.react('c_year',[{x:b.year_x,y:b.year_y,type:'bar',
  marker:{color:b.year_y.map(v=>v>=0?'#4e79a7':'#e15759')},
  hovertemplate:'%{x}<br>$%{y:,.0f}<extra></extra>'}],
  Object.assign({margin:{l:70,r:14,t:28,b:34},font:F,
   title:{text:'Cash withdrawn per year - '+b.name,x:0,font:{size:13}},
   xaxis:{type:'category'},yaxis:{gridcolor:'#eef0f3'}},BG),CFG);
 document.getElementById('p_stat').innerHTML=
  `<span>seats bought <b>${b.bought}</b> on <b>${b.starts}</b> dates</span>`+
  `<span>blowups <b>${b.deaths}</b></span>`+
  `<span>full wipeouts <b class="${b.wipeouts?'bad':'ok'}">${b.wipeouts}</b></span>`+
  `<span>alive at end <b>${b.live_end}</b></span>`+
  `<span>withdrawn <b>${money(b.withdrawn)}</b></span>`+
  `<span>cash in hand <b class="ok">${money(b.final_cash)}</b></span>`+
  `<span>equity left <b>${money(b.final_equity)}</b></span>`+
  `<span>net <b>${money(b.net)}</b></span>`;}
document.getElementById('picks').innerHTML=D.books.map((b,i)=>
 `<button class="pick" onclick="drawPolicy(${i})"><span class="dot" style="background:${
  b.colour}"></span>${b.name}</button>`).join('');
drawPolicy(D.book_default);
// ---- the frontier: typical outcome against bad case ------------------------
Plotly.newPlot('c_front',[
 {x:D.front.fx,y:D.front.fy,type:'scatter',mode:'lines',name:'frontier',
  line:{width:1.4,color:'#9aa3ad',dash:'dot'},hoverinfo:'skip'},
 {x:D.front.x,y:D.front.y,type:'scatter',mode:'markers+text',name:'policy',
  text:D.front.tag,textposition:'middle right',
  textfont:{size:10.5,color:'#374151'},
  cliponaxis:false,
  marker:{size:D.front.size,symbol:D.front.symbol,
   color:D.front.fill,line:{width:2,color:D.front.edge}},
  customdata:D.front.info,showlegend:false,
  hovertemplate:'<b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>'}],
 Object.assign({margin:{l:80,r:150,t:30,b:52},font:F,
  title:{text:'Typical outcome against bad case - hollow markers are dominated',
   x:0,font:{size:13}},
  xaxis:{title:'net at p10, the bad case  $',gridcolor:'#eef0f3',zeroline:true,
   zerolinecolor:'#9ca3af'},
  yaxis:{title:'net at the median  $',gridcolor:'#eef0f3'}},BG),CFG);
function bars(id,m,title,ylab){
 Plotly.newPlot(id,[{x:m.x,y:m.y,type:'bar',
  marker:{color:m.y.map(v=>v>=0?'#59a14f':'#e15759'),
   line:{color:'#00000022',width:.5}},
  hovertemplate:'%{x}<br>$%{y:,.0f}<extra></extra>'}],
  Object.assign({margin:{l:72,r:14,t:28,b:44},font:F,bargap:.15,
   title:{text:title,x:0,font:{size:13}},
   xaxis:{type:'category',gridcolor:'#eef0f3',nticks:24,tickangle:-45},
   yaxis:{title:ylab,gridcolor:'#eef0f3',zeroline:true,zerolinecolor:'#374151'},
   // inside the plot area, as the legacy matplotlib version had it - keeping it
   // out of the margin stops it colliding with the title
   annotations:[{xref:'paper',yref:'paper',x:0.008,y:0.99,
    xanchor:'left',yanchor:'top',text:m.note,showarrow:false,align:'left',
    font:{size:11.5,color:'#3f3f46'},bgcolor:'#fef9c3',bordercolor:'#e5d98b',
    borderwidth:1,borderpad:4}]},BG),CFG);}
bars('c_mstrat',D.mstrat,'Strategy monthly P&L - one slot, no account rule','P&L $');
bars('c_mbook',D.mbook,'Book monthly P&L - cash withdrawn plus live equity','P&L $');
function tbl(id,rows,cols,hdr){document.getElementById(id).innerHTML=
 '<table><thead><tr>'+hdr.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>'+
 rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c];if(v==null)v='';
  if(typeof v==='number')v=v.toLocaleString();
  return `<td>${v}</td>`;}).join('')+'</tr>').join('')+'</tbody></table>';}
tbl('t_keep',D.robust,['policy','withdraw','below_water','worst','wipeout_rate',
 'ruin_rate','blowups','cash_median','equity_median','net_p10','net_median',
 'seats_median'],
 ['policy','withdraws','ended BELOW what you put in','worst window net $',
  'windows with a wipeout','ruin rate','blowups (median)','cash MEDIAN $',
  'equity left (median) $','net p10 $','net MEDIAN $','seats bought (median)']);
tbl('t_pick',D.pick,['want','policy','why'],
 ['if what you care about is...','then take','the numbers behind it']);
tbl('t_rec',D.rec,['metric','sim','mt5'],['metric','this simulation','MT5 run']);
document.getElementById('foot').textContent=
 `code ${D.git} · generated ${D.gen} · measured on 2020-2026. RR and the window set are `+
 `EA defaults, not fitted, but the window design still came from looking at this history. `+
 `The withdrawal level is a real free parameter and should be validated out-of-sample `+
 `before it is trusted.`;
</script></body></html>"""


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True,
                              timeout=10).stdout.strip() or "n/a"
    except (OSError, subprocess.SubprocessError):
        return "n/a"


def build_html(payload):
    try:
        import plotly.offline as po
    except ImportError:
        print("\n  plotly is not installed in this venv, so the HTML was skipped.")
        print("  Enable it with:  venv\\Scripts\\python.exe -m pip install plotly")
        return
    payload["gen"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    payload["git"] = git_rev()
    html = (_TPL.replace("__PLOTLYJS__", po.get_plotlyjs())
            .replace("__RRV__", f"{payload['rr']:g}")
            .replace("__DDV__", f"{payload['dd_limit']:,.0f}")
            .replace("__COSTV__", f"{payload['cost']:,.0f}")
            .replace("__COMMV__", f"{COMMISSION_ROUNDTURN:.2f}")
            .replace("__IVLV__", str(payload["interval_days"]))
            .replace("__PATHV__", payload["path_label"])
            .replace("__NWIN__", str(payload["n_windows"]))
            .replace("__HZV__", f"{payload['horizon']:g}")
            .replace("__DATA__", json.dumps(payload, separators=(",", ":"),
                                            default=str)))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "account_farming.html"
    out.write_text(html, encoding="utf-8")
    print(f"Saved {out}")


def thin(seq, n=1200):
    return seq[::max(1, len(seq) // n)]


# ------------------------------------------------------- bootstrap explorer ----
_EXP_TPL = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Bootstrap explorer</title>
<script>__PLOTLYJS__</script>
<style>
 body{font:14px/1.45 system-ui,Segoe UI,Arial;margin:0;background:#f4f5f7;color:#1a1a1a}
 header{background:#1f2937;color:#fff;padding:14px 22px}h1{font-size:17px;margin:0}
 header p{margin:4px 0 0;font-size:12.5px;color:#cbd5e1}
 main{max-width:1240px;margin:16px auto;padding:0 16px}
 .panel{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .note{font-size:12.3px;color:#6b7280;margin:6px 0}
 h2{font-size:15px;margin:20px 0 8px}
 table{border-collapse:collapse;width:100%;font-size:12.4px}
 th{text-align:right;padding:5px 8px;background:#f1f5f9;white-space:nowrap;cursor:pointer}
 th:first-child,td:first-child{text-align:left}
 td{padding:4px 8px;text-align:right;border-top:1px solid #eef0f3;white-space:nowrap}
 tr.sel td{background:#fef9c3}
 .flow{background:#eef2ff;border-left:3px solid #4f46e5;padding:12px 16px;border-radius:8px;font-size:13px;color:#312e81;margin-bottom:16px}
 .flow b{color:#1e1b4b}
 .flow code{background:#e0e7ff;padding:1px 5px;border-radius:4px;font-size:12.4px}
 .warn{background:#fef3c7;border-left:3px solid #d97706;padding:10px 14px;border-radius:6px;font-size:12.8px;margin-bottom:16px}
 .warn b{color:#92400e}
 .ctl{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .ctl label{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.03em}
 select{font:inherit;font-size:14px;padding:6px 10px;border:1px solid #d3d8de;border-radius:7px;background:#fff;color:#111}
 .rate{font-size:13px;color:#374151;background:#f1f5f9;padding:6px 12px;border-radius:999px}
 .rate b{font-size:15px}
 .pstat{display:flex;flex-wrap:wrap;gap:0 22px;font-size:12.6px;color:#374151;
  background:#f8fafc;border:1px solid #eef0f3;border-radius:8px;padding:10px 14px;margin-bottom:14px}
 .pstat b{color:#111;font-weight:600}
 .pstat .bad{color:#b91c1c}.pstat .ok{color:#15803d}
 .mb{font:inherit;font-size:12.2px;padding:4px 12px;border:1px solid #d3d8de;background:#fff;
  border-radius:999px;cursor:pointer;color:#374151;margin-right:6px}
 .mb.on{background:#1f2937;border-color:#1f2937;color:#fff}
</style></head><body>
<header><h1>Bootstrap explorer &mdash; the seats pay for their own replacements</h1>
<p>RR __RRV__, $__DDV__ trailing drawdown, Safety Net $__SNV__, $__COSTV__ per seat,
$__SEEDV__ starting cash, one seat at a time every __IVLV__ days, seat cap __SEATSV__.</p>
</header><main>

<div class="flow"><b>How the money actually moves.</b> There is exactly one pot of cash.
It starts at <code>$__SEEDV__</code>. A seat that has passed the Safety Net pays money
<i>into</i> that pot when the withdrawal rule fires; buying a seat takes
<code>$__COSTV__</code> <i>out</i> of it. Nothing else adds to it &mdash; no outside money
ever comes in. So <b>withdrawals are the only thing that funds growth</b>, and the two
dropdowns below are the entire growth engine.
<br><br>That is also why a never-withdraw rule is not a strategy here but a dead end: with
<code>$__SEEDV__</code> of seed at <code>$__COSTV__</code> a seat it buys exactly
<b>__NEVERV__ seats</b>, one per interval, and then the pot is empty forever and it can
never buy another. Any policy on this page that banks nothing behaves the same way.</div>

<div class="ctl">
 <label for="sel_ck">withdraw</label>
 <select id="sel_ck"></select>
 <label for="sel_st">per</label>
 <select id="sel_st"></select>
 <label>of lifetime gain</label>
 <span class="rate" id="rate"></span>
</div>
<div id="stat" class="pstat"></div>
<div class="warn" id="alert" style="display:none"></div>

<div class="panel"><div id="c_book" style="height:370px"></div>
 <div class="note">The pot and the book together: seats held (filled, left axis) against
 cumulative cash withdrawn and equity sitting in live seats (right axis). Every step down
 in the filled area is a liquidation; every step up is a purchase, which is only possible
 because the green line moved first.</div>
 <div id="c_cash" style="height:210px"></div>
 <div class="note"><b>This is the panel that explains the gaps.</b> It is the cash
 <i>balance</i>, not the running total &mdash; the only thing a purchase can be paid from.
 Below the dashed line the book cannot buy a seat no matter how much equity it is sitting
 on, and it spends most of its life there, because every dollar that arrives is spent on
 the next seat almost immediately.</div>
 <div class="note">Equity is not buying power. The rule pays out on <b>each seat's own</b>
 lifetime gain, so a wide, shallow book pays nothing: nine seats averaging $2,500 are worth
 $22,500 together and are all still below the Safety Net individually, so not one of them
 owes a withdrawal. Only a seat that has personally cleared
 <span id="thresh"></span> pays anything at all.</div></div>

<div class="panel"><div id="c_seats" style="height:420px"></div>
 <div class="note">One line per purchase date, equity plus cash that seat has already paid
 out. Seats bought on the same trade are identical, so each distinct date is drawn once
 &mdash; hover for how many seats it stands for.</div></div>

<div class="panel"><div id="c_year" style="height:300px"></div>
 <div class="note">Cash withdrawn per calendar year under this rule.</div></div>

<h2>The whole grid</h2>
<div class="panel">
 <div style="margin-bottom:10px" id="mbtns"></div>
 <div id="c_heat" style="height:440px"></div>
 <div class="note">Every combination scored across __NWIN__ overlapping __HZV__-year
 windows. Click any cell to load it above. Blank cells are combinations where the
 withdrawal is larger than the gain that triggers it, which is the same as withdrawing the
 whole gain &mdash; they duplicate the diagonal and are left out.</div>
 <div class="note"><b>Read the wipeout panel before the money panels.</b> Cash and net both
 rise with how aggressively you withdraw, and so does the chance of losing the whole book;
 a cell that looks best on net may be one you would never actually run.</div></div>

<div class="panel" style="overflow:auto" id="t_grid"></div>
<div class="note" id="foot"></div>
</main><script>
const D=__DATA__,CFG={displaylogo:false,responsive:true};
const F={family:'system-ui,Segoe UI,Arial',size:11.5};
const BG={plot_bgcolor:'#fff',paper_bgcolor:'#fff'};
const BASE=D.base,DAY=864e5;
const money=v=>(v<0?'-$':'$')+Math.abs(Math.round(v)).toLocaleString();
const key=(c,s)=>c+'_'+s;
let metric='net';
const METRICS={
 net:['net, median $','#eef7f0','#15803d'],
 cash:['cash withdrawn, median $','#eef3f9','#2b6cb0'],
 wipe:['windows that lost the whole book %','#fdf1f1','#b91c1c'],
 blow:['seats liquidated, median','#fdf5ec','#c2760c'],
 seats:['seats bought, median','#f4f1f8','#6b46c1']};

function sel(){return key(+document.getElementById('sel_ck').value,
                          +document.getElementById('sel_st').value);}
function draw(){
 const k=sel(),c=D.cells[k];
 const ck=+document.getElementById('sel_ck').value;
 const st=+document.getElementById('sel_st').value;
 document.getElementById('rate').innerHTML=c?
  `effective rate <b>${(100*ck/st).toFixed(0)}%</b> of every dollar gained`:
  '<b>not simulated</b>';
 if(!c){document.getElementById('stat').innerHTML=
   '<span>This combination withdraws more than the gain that triggers it, '+
   'so it behaves identically to the equal-amount case on the diagonal.</span>';
  document.getElementById('alert').style.display='none';return;}
 const b=c.book;
 const x=o=>o.map(v=>BASE+v*DAY);
 Plotly.react('c_book',[
  {x:x(b.o),y:b.live,type:'scatter',mode:'lines',name:'seats held',
   fill:'tozeroy',line:{width:1,color:'#4e79a7',shape:'hv'},
   fillcolor:'rgba(78,121,167,.22)'},
  {x:x(b.o),y:b.cash,type:'scatter',mode:'lines',name:'cash withdrawn (cumulative)',
   yaxis:'y2',line:{width:2,color:'#15803d'}},
  {x:x(b.o),y:b.eq,type:'scatter',mode:'lines',name:'equity in live seats',
   yaxis:'y2',line:{width:1.4,color:'#b07aa1',dash:'dot'}}],
  Object.assign({margin:{l:56,r:70,t:28,b:34},font:F,hovermode:'x unified',
   title:{text:`$${ck.toLocaleString()} per $${st.toLocaleString()} gained`,
    x:0,font:{size:13}},
   xaxis:{type:'date',gridcolor:'#eef0f3'},
   yaxis:{title:'seats',gridcolor:'#eef0f3',rangemode:'tozero'},
   yaxis2:{title:'$',overlaying:'y',side:'right',showgrid:false},
   legend:{orientation:'h',y:-.16}},BG),CFG);
 // Log scale, because the pot runs from $0 to five figures while the decision
 // it drives sits at $200 - on a linear axis the years that answer "why did it
 // not buy" are squashed flat against zero. Zeros are floored to $1 to plot.
 const lg=b.hand.map(v=>Math.max(v,1));
 const buyable=b.hand.map(v=>v>=D.cost?Math.max(v,1):null);
 Plotly.react('c_cash',[
  {x:x(b.o),y:lg,type:'scatter',mode:'lines',name:'cash on hand',
   line:{width:1.4,color:'#c2760c'},customdata:b.hand,
   hovertemplate:'$%{customdata:,.0f} in the pot<extra></extra>'},
  {x:x(b.o),y:buyable,type:'scatter',mode:'markers',name:'could afford a seat',
   marker:{size:3.5,color:'#15803d'},connectgaps:false,hoverinfo:'skip'}],
  Object.assign({margin:{l:62,r:70,t:26,b:30},font:F,
   title:{text:'Cash on hand - buying power, not the running total (log scale)',
    x:0,font:{size:12.5}},
   xaxis:{type:'date',gridcolor:'#eef0f3'},
   yaxis:{title:'$ in the pot',gridcolor:'#eef0f3',type:'log',
    tickvals:[1,10,100,1000,10000,100000],
    ticktext:['$0','$10','$100','$1k','$10k','$100k']},
   shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:Math.log10(D.cost),
    y1:Math.log10(D.cost),line:{color:'#b91c1c',width:1.2,dash:'dash'}}],
   annotations:[{xref:'paper',x:0.995,y:Math.log10(D.cost),xanchor:'right',
    text:'$'+D.cost.toLocaleString()+' = one seat',showarrow:false,
    yshift:-9,bgcolor:'rgba(255,255,255,.85)',borderpad:2,
    font:{size:10.5,color:'#b91c1c'}}],
   legend:{orientation:'h',y:-.22}},BG),CFG);
 document.getElementById('thresh').textContent=
  '$'+(D.safety+st).toLocaleString()+' of lifetime value ($'+
  D.safety.toLocaleString()+' Safety Net plus the $'+st.toLocaleString()+
  ' that triggers a payout)';
 Plotly.react('c_seats',b.curves.map(s=>({
  x:x(s.o),y:s.y,type:'scatter',mode:'lines',showlegend:false,
  line:{width:1.2,color:'#4e79a7'},opacity:.55,
  hovertemplate:'bought '+s.d+(s.n>1?' &times;'+s.n+' seats':'')+
   '<br>%{x|%Y-%m-%d}<br>$%{y:,.0f} each<extra></extra>'})),
  Object.assign({margin:{l:66,r:14,t:28,b:34},font:F,
   title:{text:'Every purchase date - P&L including cash already withdrawn',
    x:0,font:{size:13}},
   shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:D.safety,y1:D.safety,
     line:{color:'#15803d',width:1,dash:'dash'}},
    {type:'line',xref:'paper',x0:0,x1:1,y0:0,y1:0,line:{color:'#9ca3af',width:1}}],
   xaxis:{type:'date',gridcolor:'#eef0f3'},
   yaxis:{title:'$ per seat',gridcolor:'#eef0f3'}},BG),CFG);
 Plotly.react('c_year',[{x:b.yx,y:b.yy,type:'bar',
  marker:{color:b.yy.map(v=>v>=0?'#4e79a7':'#e15759')},
  hovertemplate:'%{x}<br>$%{y:,.0f}<extra></extra>'}],
  Object.assign({margin:{l:70,r:14,t:28,b:34},font:F,
   title:{text:'Cash withdrawn per year',x:0,font:{size:13}},
   xaxis:{type:'category'},yaxis:{gridcolor:'#eef0f3'}},BG),CFG);
 document.getElementById('stat').innerHTML=
  `<span>across ${D.nwin} windows &mdash; net median <b>${money(c.net_median)}</b></span>`+
  `<span>cash <b>${money(c.cash_median)}</b></span>`+
  `<span>equity left <b>${money(c.equity_median)}</b></span>`+
  `<span>net p10 <b>${money(c.net_p10)}</b></span>`+
  `<span>lost the whole book in <b class="${c.wipeout_pct?'bad':'ok'}">`+
   `${c.wipeout_pct}%</b> of windows</span>`+
  `<span>blowups <b>${c.blowups}</b></span>`+
  `<span>seats <b>${c.seats_median}</b></span>`;
 const al=document.getElementById('alert');
 if(c.wipeout_pct>0){al.style.display='';al.innerHTML=
   `<b>This rule lost every seat it held in ${c.wipeout_pct}% of windows.</b> `+
   `It survives on this page only because withdrawn cash could rebuy the book. `+
   `Whatever the money columns say, that is the number to decide on.`+
   (ck===st?` <br>Withdrawing the entire gain that triggers the withdrawal puts every `+
    `frozen seat back on the Safety Net each time, which is strip-to-a-level under `+
    `another name - and that is what synchronises a book into dying all at once. `+
    `Every rule on the 100% diagonal wipes out; nothing below it does.`:'');}
 else if(c.cash_median<=0){al.style.display='';al.innerHTML=
   `<b>This rule never banks anything.</b> With no cash coming back into the pot it `+
   `buys ${D.never} seats out of the seed and then stops permanently.`;}
 else al.style.display='none';
 [...document.querySelectorAll('#t_grid tr')].forEach(tr=>
  tr.classList.toggle('sel',tr.dataset.k===k));}

function heat(){
 const m=METRICS[metric];
 [...document.querySelectorAll('.mb')].forEach(b=>
  b.classList.toggle('on',b.dataset.m===metric));
 Plotly.react('c_heat',[{
  z:D.heat[metric],x:D.steps.map(s=>'$'+s.toLocaleString()),
  y:D.chunks.map(c=>'$'+c.toLocaleString()),
  text:D.txt[metric],texttemplate:'%{text}',textfont:{size:10.5},
  type:'heatmap',colorscale:[[0,m[1]],[1,m[2]]],hoverongaps:false,
  xgap:2,ygap:2,colorbar:{title:{text:m[0],side:'right'},thickness:12},
  hovertemplate:'withdraw %{y} per %{x} gained<br>'+m[0]+': %{z:,.0f}<extra></extra>'}],
  Object.assign({margin:{l:80,r:20,t:30,b:52},font:F,
   title:{text:m[0]+' - click a cell to load it',x:0,font:{size:13}},
   xaxis:{title:'per this much lifetime gain',type:'category'},
   yaxis:{title:'withdraw this much',type:'category'}},BG),CFG);
 document.getElementById('c_heat').on('plotly_click',ev=>{
  const p=ev.points[0];
  document.getElementById('sel_ck').value=D.chunks[p.pointIndex[0]];
  document.getElementById('sel_st').value=D.steps[p.pointIndex[1]];
  draw();window.scrollTo({top:0,behavior:'smooth'});});}

document.getElementById('sel_ck').innerHTML=D.chunks.map(c=>
 `<option value="${c}">$${c.toLocaleString()}</option>`).join('');
document.getElementById('sel_st').innerHTML=D.steps.map(s=>
 `<option value="${s}">$${s.toLocaleString()}</option>`).join('');
document.getElementById('sel_ck').value=D.def_ck;
document.getElementById('sel_st').value=D.def_st;
document.getElementById('sel_ck').onchange=draw;
document.getElementById('sel_st').onchange=draw;
document.getElementById('mbtns').innerHTML=Object.keys(METRICS).map(k=>
 `<button class="mb" data-m="${k}">${METRICS[k][0]}</button>`).join('');
[...document.querySelectorAll('.mb')].forEach(b=>
 b.onclick=()=>{metric=b.dataset.m;heat();});
const HDR=['withdraw','per gain of','rate','net MEDIAN $','cash MEDIAN $',
 'equity left $','net p10 $','wipeout windows %','blowups','seats bought'];
const KEYS=['ck','st','rate','net_median','cash_median','equity_median','net_p10',
 'wipeout_pct','blowups','seats_median'];
document.getElementById('t_grid').innerHTML='<table><thead><tr>'+
 HDR.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+
 D.rows.map(r=>`<tr data-k="${key(r.ck,r.st)}" style="cursor:pointer">`+
  KEYS.map(c=>{let v=r[c];
   if(c==='ck'||c==='st')v='$'+v.toLocaleString();
   else if(c==='rate')v=v+'%';
   else if(typeof v==='number')v=v.toLocaleString();
   return `<td>${v}</td>`;}).join('')+'</tr>').join('')+'</tbody></table>';
[...document.querySelectorAll('#t_grid tr[data-k]')].forEach(tr=>tr.onclick=()=>{
 const p=tr.dataset.k.split('_');
 document.getElementById('sel_ck').value=p[0];
 document.getElementById('sel_st').value=p[1];
 draw();window.scrollTo({top:0,behavior:'smooth'});});
document.getElementById('foot').textContent=
 `code ${D.git} · generated ${D.gen} · one illustrative path per rule for the charts, `+
 `medians of ${D.nwin} overlapping windows for the grid. The withdrawal rule is fitted `+
 `to this sample and has not been validated out-of-sample.`;
heat();draw();
</script></body></html>"""


def build_explorer(payload):
    try:
        import plotly.offline as po
    except ImportError:
        return
    payload["gen"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    payload["git"] = git_rev()
    html = (_EXP_TPL.replace("__PLOTLYJS__", po.get_plotlyjs())
            .replace("__RRV__", f"{payload['rr']:g}")
            .replace("__DDV__", f"{payload['dd']:,.0f}")
            .replace("__SNV__", f"{payload['safety']:,.0f}")
            .replace("__COSTV__", f"{payload['cost']:,.0f}")
            .replace("__SEEDV__", f"{payload['seed']:,.0f}")
            .replace("__IVLV__", str(payload["interval_days"]))
            .replace("__SEATSV__", str(payload["seat_cap"]))
            .replace("__NEVERV__", str(payload["never"]))
            .replace("__NWIN__", str(payload["nwin"]))
            .replace("__HZV__", f"{payload['horizon']:g}")
            .replace("__DATA__", json.dumps(payload, separators=(",", ":"),
                                            default=str)))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "bootstrap_explorer.html"
    out.write_text(html, encoding="utf-8")
    print(f"Saved {out}")


# ----------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[3])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--input-csv", type=Path, help="One MAE-style TSV/CSV trade export.")
    src.add_argument("--sweep-root", type=Path, default=ROOT / "1_sweeps" / "RR")
    ap.add_argument("--stats-root", type=Path, default=ROOT / "1_sweeps" / "RR_stats")
    ap.add_argument("--rr", type=float, default=1.00)
    ap.add_argument("--windows", default="all",
                    help="Comma-separated windows, e.g. 9-10,10-11; default: all.")
    ap.add_argument("--start-date", help="Inclusive YYYY-MM-DD filter.")
    ap.add_argument("--end-date", help="Inclusive YYYY-MM-DD filter.")

    ap.add_argument("--dd", "--dd-limit", dest="dd", type=float, default=2500.0,
                    help="Trailing drawdown. The Safety Net is derived as dd + floor.")
    ap.add_argument("--frozen-dd-floor", type=float, default=100.0,
                    help="Fixed account P&L floor once the trailing DD freezes.")
    ap.add_argument("--intratrade-path", choices=("mae-first", "mfe-first"),
                    default="mae-first",
                    help="Unknown MAE/MFE ordering within a trade. mae-first "
                         "reproduced MT5; mfe-first is the optimistic case.")

    ap.add_argument("--cost", type=float, default=200.0, help="Price of one seat.")
    ap.add_argument("--seats", type=int, default=20,
                    help="max concurrent accounts the firm allows")
    ap.add_argument("--seed", type=float, default=1200.0,
                    help="starting cash; this drives the ruin rate more than anything")
    ap.add_argument("--interval-days", type=int, default=30,
                    help="calendar days between new account starts")
    ap.add_argument("--start-policy", choices=("time", "profit", "dd", "any"),
                    default="time", help="'any' combines every enabled trigger.")
    ap.add_argument("--profit-trigger", type=float, default=1000.0,
                    help="P&L on the newest live seat that triggers a profit start")
    ap.add_argument("--dd-trigger", type=float, default=400.0,
                    help="drawdown on any live seat that triggers a DD start")
    ap.add_argument("--min-days-between-starts", type=int, default=1)
    ap.add_argument("--max-per-event", type=int, default=1,
                    help="seats bought at one time. 1 staggers; higher stacks "
                         "identical seats that then die together.")
    ap.add_argument("--withdraw-chunk", type=float, default=None,
                    help="ratchet: withdraw in units of this (default: one seat)")
    ap.add_argument("--withdraw-step", type=float, default=1000.0,
                    help="ratchet: one chunk per this much lifetime gain")
    ap.add_argument("--split", type=float, default=1.0,
                    help="trader's share of profit (real plans are 0.8-0.9)")
    ap.add_argument("--horizon", type=float, default=2.0,
                    help="years per book window in the robustness sweep")
    ap.add_argument("--no-explore", action="store_true",
                    help="skip results/bootstrap_explorer.html, which is most of "
                         "the runtime")
    a = ap.parse_args()

    rule = Rule(dd=a.dd, frozen_floor=a.frozen_dd_floor, cost=a.cost,
                mfe_first=a.intratrade_path == "mfe-first")
    cfg = BookCfg(seats=a.seats, seed=a.seed, interval_days=a.interval_days,
                  policy=a.start_policy, profit_trigger=a.profit_trigger,
                  dd_trigger=a.dd_trigger, min_days=a.min_days_between_starts,
                  max_per_event=a.max_per_event)
    chunk = a.withdraw_chunk if a.withdraw_chunk else rule.cost
    HARD = Harvest("level", keep=rule.safety_net)

    # A bootstrap book cannot start below one seat's price: buying needs cash,
    # cash only arrives from a withdrawal, and a withdrawal needs a live seat.
    # Below the cost that is a deadlock, not a slow start, and it would other-
    # wise show up as a silent run of zeroes flagged "ruined".
    if a.seed < rule.cost:
        ap.error(f"--seed {a.seed:,.0f} is below --cost {rule.cost:,.0f}. The "
                 f"bootstrap can never buy its first seat, so nothing would "
                 f"happen at all: seats are what generate the withdrawals that "
                 f"buy seats. ${rule.cost:,.0f} is the true minimum (one seat of "
                 f"your own money, everything after it out of profit).")

    st = build_stream(a)
    ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
    gross = float((net + COMMISSION_ROUNDTURN).sum())

    print("=" * 88)
    print(f"RECONSTRUCTION — all windows at RR {a.rr:g}, one position at a time")
    print("=" * 88)
    print(f"  windows merged          {len(st['windows'])}"
          + (f"   (excluded, tester-blown: {', '.join(st['blown'])})"
             if st["blown"] else ""))
    if st["unknown"]:
        print(f"  WARNING: no MT5 stats for {len(st['unknown'])} window(s), included "
              f"unchecked: {', '.join(st['unknown'])}")
    print(f"  entries offered         {st['offered']:,}")
    print(f"  entries taken           {st['taken']:,}   "
          f"({st['offered'] - st['taken']:,} blocked by the open position — the "
          f"correction a plain concatenation misses)")
    print(f"  commission          ${COMMISSION_ROUNDTURN:>10.2f} per round turn"
          f"   (${COMMISSION_ROUNDTURN * len(net):,.0f} total)")
    print(f"  gross profit        ${gross:>10,.0f}     MT5: ${MT5_REF['gross']:,.0f}"
          f"   ({100 * gross / MT5_REF['gross'] - 100:+.1f}%)")
    print(f"  trades               {len(net):>10,}     MT5: {MT5_REF['trades']:,}"
          f"   ({100 * len(net) / MT5_REF['trades'] - 100:+.1f}%)")
    print(f"  net after commission ${net.sum():>9,.0f}")
    print(f"  equity DD (MAE-first) ${dd_equity(net, mae, mfe):>8,.0f}     "
          f"MT5: ${MT5_REF['eq_dd']:,.0f}")
    print(f"  account rule: floor = min(peak - {rule.dd:,.0f}, "
          f"{rule.frozen_floor:,.0f}), Safety Net ${rule.safety_net:,.0f}, "
          f"{a.intratrade_path}")

    # ---- one seat, from every possible start date ---------------------------
    days = pd.to_datetime(ex).normalize()
    first_i = pd.Series(range(len(ex))).groupby(days).min().sort_index()
    last_day = pd.Timestamp(ex[-1])
    out = []
    for d, i0 in first_i.items():
        h = run_account(net, mae, mfe, int(i0), HARD, rule)
        out.append({
            "start": d.date(), "start_i": int(i0),
            "runway_days": (last_day - d).days,
            "frozen": h["frozen"], "dead": h["dead_i"] is not None,
            "days_to_freeze": ((pd.Timestamp(ex[h["froze_i"]]) - d).days
                               if h["froze_i"] is not None else None),
            "banked": round(h["banked"]),
            "value": round(h["banked"] + (h["eq"] if h["dead_i"] is None else 0.0)),
        })
    S = pd.DataFrame(out)
    FULL = S[S["runway_days"] >= 365]     # censoring-free subset
    okS = S[S["frozen"]]
    badS = S[~S["frozen"]]
    med = FULL[FULL["frozen"]]["days_to_freeze"].median()

    print("\n" + "=" * 88)
    print(f"ONE SEAT, ${rule.dd:,.0f} trailing DD — every possible start date")
    print("=" * 88)
    for label, X in (("all starts", S), ("starts with >=1yr of runway", FULL)):
        print(f"  {label:<30} n={len(X):<5} froze {X['frozen'].mean():>6.1%}   "
              f"died before freezing {(X['dead'] & ~X['frozen']).mean():>6.1%}")
    print(f"\n  days to the Safety Net (uncensored): median {med:.0f}, "
          f"p25 {okS['days_to_freeze'].quantile(.25):.0f}, "
          f"p75 {okS['days_to_freeze'].quantile(.75):.0f}")

    # ---- policies over many overlapping windows -----------------------------
    print("\n" + "=" * 88)
    print(f"ROBUSTNESS — every policy run from {a.horizon:g}-year windows, "
          f"quarterly starts, ${cfg.seed:,.0f} seed, a seat every "
          f"{cfg.interval_days}d ({cfg.policy})")
    print("=" * 88)
    horizon = pd.Timedelta(days=int(365.25 * a.horizon))
    day_arr = pd.to_datetime(ex)
    q_starts = []
    for d in pd.date_range(day_arr[0].normalize(), day_arr[-1], freq="QS"):
        if d + horizon > day_arr[-1]:
            break
        j = int(np.searchsorted(day_arr, d))
        k = int(np.searchsorted(day_arr, d + horizon))
        if k - j > 200:
            q_starts.append((d, j, k))
    print(f"  {len(q_starts)} windows, {q_starts[0][0].date()} .. "
          f"{q_starts[-1][0].date()}\n")

    # Mode 1 is the subscription: own money, one seat per interval, hold forever.
    # Mode 2 is the bootstrap: the seats pay for their own replacements. Every
    # bootstrap variant here buys at most one seat per event, because a wave of
    # identical seats is extra exposure and no extra diversification.
    # Every parameter these policies depend on is pinned here rather than
    # inherited from the CLI, so a label like "1/interval" cannot be quietly
    # falsified by --max-per-event. CLI settings get their own row at the end.
    def boot(**kw):
        base = {**cfg.__dict__, "funding": "cash", "max_per_event": 1}
        return BookCfg(**{**base, **kw})

    POLICIES = [
        ("subscription", BookCfg(**{**cfg.__dict__, "funding": "external",
                                    "seed": 0.0, "max_per_event": 1}),
         Harvest("none")),
        # the old winner, kept as the baseline the new conditions have to beat
        ("bootstrap · all-in, strip to net",
         boot(max_per_event=cfg.seats), HARD),
        ("bootstrap · 1/interval, strip to net", boot(), HARD),
        ("bootstrap · 1/interval, keep $4,000", boot(),
         Harvest("level", keep=4000.0)),
        ("bootstrap · 1/interval, never withdraw", boot(), Harvest("none")),
    ]
    # One seat's price per N of gains: the smallest withdrawal that can actually
    # buy something, swept across rates.
    for st_ in (400.0, 600.0, 1000.0, 2000.0):
        POLICIES.append((f"bootstrap · 1/interval, ${chunk:,.0f} per ${st_:,.0f} "
                         f"({chunk / st_:.0%})",
                         boot(), Harvest("ratchet", chunk=chunk, step=st_)))
    # The same idea in plain money terms, with bigger withdrawals taken less
    # often. Rate alone does not determine behaviour: a seat on $1,000/$5,000
    # holds its gains far longer between payouts than one on $200/$1,000, even
    # though both take 20% in the end.
    for ck, st_ in ((500.0, 2500.0), (1000.0, 5000.0),
                    (1000.0, 2000.0), (2000.0, 4000.0)):
        POLICIES.append((f"bootstrap · 1/interval, ${ck:,.0f} per ${st_:,.0f} "
                         f"({ck / st_:.0%})",
                         boot(), Harvest("ratchet", chunk=ck, step=st_)))
    # Whatever the CLI actually asked for, if it is not already in the menu.
    if a.max_per_event != 1 or a.withdraw_step != 1000.0 or a.withdraw_chunk:
        POLICIES.append((
            f"bootstrap · custom: {a.max_per_event}/event, ${chunk:,.0f} per "
            f"${a.withdraw_step:,.0f}",
            boot(max_per_event=a.max_per_event),
            Harvest("ratchet", chunk=chunk, step=a.withdraw_step)))

    def score(c: BookCfg, h: Harvest) -> dict:
        """Run one policy across every window and reduce it to comparable stats."""
        res = [run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], h, rule, c)
               for _, j, k in q_starts]
        cashes = np.array([r["cash"] for r in res]) * a.split
        equities = np.array([r["equity"] for r in res]) * a.split
        # Net has to charge every dollar of the trader's own money, whichever way
        # it went in: `spent` for the subscription, which pays per seat forever,
        # and the seed for the bootstrap, which pays once up front. Leaving the
        # seed out overstated every bootstrap net by exactly one seed.
        nets = (cashes + equities - np.array([r["spent"] for r in res]) - c.seed)
        return {
            "withdraw": h.label,
            "ruin_rate": "n/a" if c.funding == "external"
                         else f"{np.mean([r['ruined'] for r in res]):.0%}",
            # Share of windows that saw at least one wipeout, not the median
            # count: wipeouts are rare enough per window that a median of 0 hides
            # a policy which loses the whole book several times over a long run.
            "wipeout_rate": f"{np.mean([r['wipeouts'] > 0 for r in res]):.0%}",
            "wipeout_pct": round(100 * float(np.mean([r["wipeouts"] > 0
                                                      for r in res]))),
            "wipeouts": round(float(np.mean([r["wipeouts"] for r in res])), 2),
            "blowups": int(np.median([r["deaths"] for r in res])),
            "spent": round(float(np.median([r["spent"] for r in res]))),
            "cash_p10": round(np.percentile(cashes, 10)),
            "cash_median": round(np.median(cashes)),
            "cash_p90": round(np.percentile(cashes, 90)),
            "equity_median": round(np.median(equities)),
            "net_p10": round(np.percentile(nets, 10)),
            "net_median": round(np.median(nets)),
            # The plainest question anyone actually has: how often did I end up
            # with less than I put in? Medians hide this completely.
            "below_water": f"{np.mean(nets <= 0):.0%}",
            "worst": round(float(nets.min())),
            "seats_median": int(np.median([r["bought"] for r in res])),
            "starts_median": int(np.median([r["starts"] for r in res])),
        }

    RB = pd.DataFrame([{"policy": label, **score(c, h)}
                       for label, c, h in POLICIES])
    cols = ["policy", "withdraw", "below_water", "worst", "wipeout_rate", "ruin_rate",
            "blowups", "cash_median", "equity_median", "net_p10", "net_median",
            "seats_median"]
    print(RB[cols].to_string(index=False))
    print("\n  cash_* is REALIZED, withdrawn money. equity_median is still sitting in")
    print("  live accounts and can be lost — do not add the two and call it profit.")
    print("  net_* = cash + equity - capital spent, so the subscription is comparable.")
    print("  wipeout_rate is the share of windows that lost the whole book at least")
    print("  once; wipeouts is the mean count. blowups is the median seats liquidated.")

    BOOT = RB[RB["policy"] != "subscription"].copy()
    BOOT["_wr"] = BOOT["wipeout_rate"].str.rstrip("%").astype(float)
    best = BOOT.loc[BOOT["cash_median"].idxmax()]
    # Rank the survivable policies on net, not on realized cash: a policy that
    # banks less but keeps far more equity alive is not worse, and cash alone is
    # what crowned the all-in book that wipes out repeatedly. If nothing manages
    # a clean sweep, say so rather than crowning the least-bad one silently.
    floor_wr = BOOT["_wr"].min()
    cand = BOOT[BOOT["_wr"] == floor_wr]
    safest = cand.loc[cand["net_median"].idxmax()]
    safe_label = ("best net, never wiped out" if floor_wr == 0 else
                  f"best net at the lowest wipeout rate seen ({safest['wipeout_rate']})")
    sub = RB[RB["policy"] == "subscription"].iloc[0]
    print(f"\n  MODE 1 subscription      spent ${sub['spent']:,.0f}, "
          f"equity ${sub['equity_median']:,.0f}, net ${sub['net_median']:,.0f} "
          f"median, {sub['blowups']} blowups, wipeouts in {sub['wipeout_rate']} "
          f"of windows")
    print(f"  MODE 2 best cash         {best['policy']} -> "
          f"${best['cash_median']:,.0f} cash, ruin {best['ruin_rate']}, "
          f"wipeouts in {best['wipeout_rate']} of windows")
    print(f"  MODE 2 {safe_label}  {safest['policy']} -> "
          f"net ${safest['net_median']:,.0f} (${safest['cash_median']:,.0f} of it "
          f"realized), ruin {safest['ruin_rate']}, {safest['blowups']} blowups")

    RESULTS.mkdir(exist_ok=True)
    S.to_csv(RESULTS / "farming_starts.csv", index=False)
    RB.to_csv(RESULTS / "farming_withdrawal_policies.csv", index=False)

    # ---- both modes over the whole period, as illustration ------------------
    PMAP = {p[0]: (p[1], p[2]) for p in POLICIES}

    def full_run(label):
        c, h = PMAP[label]
        return run_book(ex, net, mae, mfe, h, rule, c, trace=True)

    sub_bk = full_run("subscription")
    bk = full_run(safest["policy"])
    # Every bootstrap policy also gets a full-period run so the report can switch
    # between them. This is the expensive part of the script.
    boot_runs = {label: (bk if label == safest["policy"] else full_run(label))
                 for label, c, _ in POLICIES if c.funding == "cash"}

    def seat_table(b):
        return pd.DataFrame([{
            "seat": i + 1,
            "start_date": s["start"].date(),
            "end_date": s.get("end").date() if s.get("end") is not None else None,
            "status": "alive" if s["alive"] else "blown",
            "reached_safety_net": s["frozen"],
            "banked": round(s["banked"]),
            "equity_at_end": round(s["eq"]),
            "value": round(s["banked"] + (s["eq"] if s["alive"] else 0.0)),
        } for i, s in enumerate(sorted(b["seats"], key=lambda x: x["start"]))])

    seat_table(bk).to_csv(RESULTS / "farming_book_seats.csv", index=False)
    seat_table(sub_bk).to_csv(RESULTS / "farming_subscription_seats.csv", index=False)
    print(f"\nSaved {RESULTS / 'farming_starts.csv'}, "
          f"{RESULTS / 'farming_withdrawal_policies.csv'}, "
          f"{RESULTS / 'farming_book_seats.csv'}, "
          f"{RESULTS / 'farming_subscription_seats.csv'}")

    print("\n" + "=" * 88)
    print("THE TWO MODES OVER THE WHOLE PERIOD (one path each, not an expectation)")
    print("=" * 88)
    for name, b in (("MODE 1  subscription", sub_bk),
                    (f"MODE 2  {safest['policy'].replace('bootstrap · ', '')}", bk)):
        print(f"  {name}")
        print(f"    seats bought {b['bought']:>4} on {b['starts']:>3} distinct dates   "
              f"blowups {b['deaths']:>4}   full wipeouts {b['wipeouts']}   "
              f"alive at end {b['live']}")
        print(f"    own capital spent ${b['spent']:>9,.0f}   "
              f"cash withdrawn ${b['withdrawn']:>9,.0f}   "
              f"equity left ${b['equity']:>9,.0f}   net ${b['wealth']:>9,.0f}")

    # Seats bought on the same trade are not merely similar, they are identical:
    # same rule, same stream, same withdrawal level, so the paths coincide to the
    # cent. Drawing all of them stacks opaque duplicates on one curve and makes
    # the book look more diversified than it is. Draw each distinct start once
    # and carry the multiplicity in the hover instead.
    # Fifteen books, each with a line per purchase date, is most of the page
    # weight. Day resolution and whole dollars are plenty at this zoom, and only
    # the two featured books need the net-position series.
    def book_payload(b, name="", full=False):
        s = b["series"]
        ds = thin(s.groupby(s["day"].dt.date).last().reset_index(drop=True), 700)
        ycum = s.assign(y=s["day"].dt.year).groupby("y")["withdrawn"].last()
        yr = (ycum.diff().fillna(ycum.iloc[0]) * a.split).round()
        groups: dict[int, list] = {}
        for seat in b["seats"]:
            groups.setdefault(seat["i0"], []).append(seat)
        curves = []
        for i0 in sorted(groups):
            g = groups[i0]
            pts = thin(g[0]["path"], 150)
            curves.append({"x": [p[0].strftime("%Y-%m-%d") for p in pts],
                           "y": [round(p[1]) for p in pts],
                           "n": len(g),
                           "d": g[0]["start"].strftime("%Y-%m-%d")})
        out = {
            "name": name,
            "x": [str(d) for d in ds["day"]],
            "live": [int(v) for v in ds["live"]],
            "cash": [round(float(v)) for v in ds["withdrawn"]],
            "hand": [round(float(v)) for v in ds["cash"]],
            "equity": [round(float(v)) for v in ds["equity"]],
            "year_x": [str(i) for i in yr.index],
            "year_y": [float(v) for v in yr],
            "curves": curves,
            "bought": b["bought"], "starts": b["starts"],
            "deaths": b["deaths"], "wipeouts": b["wipeouts"], "live_end": b["live"],
            "final_cash": round(b["cash"]), "final_equity": round(b["equity"]),
            "withdrawn": round(b["withdrawn"]),
            "spent_total": round(b["spent"]), "net": round(b["wealth"]),
        }
        if full:
            out["wealth"] = [round(float(v)) for v in ds["wealth"]]
            out["spent"] = [round(float(v)) for v in ds["spent"]]
        return out

    DDP = dd_paths(ex, net, mae, mfe)
    DDd = DDP.groupby(DDP["day"].dt.date).agg(
        eq=("eq", "last"), peak=("peak", "max"), low=("low", "min"),
        dd_closed=("dd_closed", "min"), dd_float=("dd_float", "min")).reset_index()

    m_strat = monthly(ex, net, cumulative=False)
    m_book = monthly(bk["series"]["day"], bk["series"]["wealth"].values, cumulative=True)

    def bar_payload(m):
        wins = int((m > 0).sum())
        return {"x": [str(p) for p in m.index],
                "y": [round(float(v)) for v in m.values],
                "note": (f"total ${m.sum():,.0f}  ·  positive months "
                         f"{wins}/{len(m)} ({wins / len(m):.0%})  ·  "
                         f"best ${m.max():,.0f}  ·  worst ${m.min():,.0f}")}

    print("\n" + "-" * 88)
    print("MONTHLY P&L")
    print("-" * 88)
    print(f"  strategy (one slot)  {bar_payload(m_strat)['note']}")
    print(f"  book (illustrative)  {bar_payload(m_book)['note']}")

    # ---- the bootstrap explorer: a grid over (withdraw, per gain) -----------
    if not a.no_explore:
        CHUNKS = [200.0, 400.0, 600.0, 1000.0, 1500.0, 2000.0]
        STEPS = [400.0, 600.0, 1000.0, 1500.0, 2000.0, 3000.0, 5000.0]
        base = pd.Timestamp(ex[0]).normalize()

        def offsets(days):
            return [int((pd.Timestamp(d) - base).days) for d in days]

        def cell_book(b):
            """Same shape as the main report, but dates as day offsets.

            Thirty-odd books each carrying a line per purchase date is the whole
            page weight, and an ISO date string costs three times what its day
            offset does.
            """
            s = b["series"]
            ds = thin(s.groupby(s["day"].dt.date).last().reset_index(drop=True), 420)
            ycum = s.assign(y=s["day"].dt.year).groupby("y")["withdrawn"].last()
            yr = (ycum.diff().fillna(ycum.iloc[0]) * a.split).round()
            groups: dict[int, list] = {}
            for seat in b["seats"]:
                groups.setdefault(seat["i0"], []).append(seat)
            curves = []
            for i0 in sorted(groups):
                g = groups[i0]
                pts = thin(g[0]["path"], 90)
                curves.append({"o": offsets(p[0] for p in pts),
                               "y": [round(p[1]) for p in pts],
                               "n": len(g),
                               "d": g[0]["start"].strftime("%Y-%m-%d")})
            return {"o": offsets(ds["day"]),
                    "live": [int(v) for v in ds["live"]],
                    "cash": [round(float(v)) for v in ds["withdrawn"]],
                    # The balance, not the running total: this is the only thing
                    # a purchase can come out of, and it is usually near zero.
                    "hand": [round(float(v)) for v in ds["cash"]],
                    "eq": [round(float(v)) for v in ds["equity"]],
                    "yx": [str(i) for i in yr.index],
                    "yy": [float(v) for v in yr],
                    "curves": curves}

        print("\n" + "=" * 88)
        print(f"BOOTSTRAP EXPLORER — {sum(1 for c in CHUNKS for s in STEPS if c <= s)} "
              f"withdrawal rules, each over {len(q_starts)} windows plus a full run")
        print("=" * 88)
        cells, rows = {}, []
        for ck in CHUNKS:
            for st_ in STEPS:
                if ck > st_:          # withdrawing more than the gain that
                    continue          # triggers it is just the diagonal again
                h = Harvest("ratchet", chunk=ck, step=st_)
                c = boot()
                stats = score(c, h)
                full = run_book(ex, net, mae, mfe, h, rule, c, trace=True)
                cells[f"{ck:g}_{st_:g}"] = {**stats, "book": cell_book(full)}
                rows.append({"ck": ck, "st": st_,
                             "rate": round(100 * ck / st_), **stats})
        G = pd.DataFrame(rows)
        best_cell = G.loc[G[G["wipeout_pct"] == 0]["net_median"].idxmax()] \
            if (G["wipeout_pct"] == 0).any() else G.loc[G["net_median"].idxmax()]
        print(G[["ck", "st", "rate", "wipeout_pct", "blowups", "cash_median",
                 "equity_median", "net_p10", "net_median",
                 "seats_median"]].to_string(index=False))
        print(f"\n  best net with no wipeout in any window: ${best_cell['ck']:,.0f} "
              f"per ${best_cell['st']:,.0f} ({best_cell['rate']:.0f}%) -> "
              f"net ${best_cell['net_median']:,.0f}, "
              f"cash ${best_cell['cash_median']:,.0f}")

        def matrix(col):
            return [[(None if ck > st_ else
                      float(G[(G.ck == ck) & (G.st == st_)][col].iloc[0]))
                     for st_ in STEPS] for ck in CHUNKS]

        def texts(col, fmt):
            return [[("" if ck > st_ else
                      fmt.format(G[(G.ck == ck) & (G.st == st_)][col].iloc[0]))
                     for st_ in STEPS] for ck in CHUNKS]

        build_explorer({
            "rr": a.rr, "dd": rule.dd, "safety": rule.safety_net,
            "cost": rule.cost, "seed": cfg.seed, "interval_days": cfg.interval_days,
            "seat_cap": cfg.seats, "horizon": a.horizon, "nwin": len(q_starts),
            "never": int(cfg.seed // rule.cost),
            "base": int(base.timestamp() * 1000),
            "chunks": [int(c) for c in CHUNKS], "steps": [int(s) for s in STEPS],
            "def_ck": int(best_cell["ck"]), "def_st": int(best_cell["st"]),
            "cells": cells, "rows": rows,
            "heat": {"net": matrix("net_median"), "cash": matrix("cash_median"),
                     "wipe": matrix("wipeout_pct"), "blow": matrix("blowups"),
                     "seats": matrix("seats_median")},
            "txt": {"net": texts("net_median", "{:,.0f}"),
                    "cash": texts("cash_median", "{:,.0f}"),
                    "wipe": texts("wipeout_pct", "{:.0f}%"),
                    "blow": texts("blowups", "{:.0f}"),
                    "seats": texts("seats_median", "{:.0f}")},
        })

    short = lambda s: s.replace("bootstrap · ", "")
    sub_pl = book_payload(sub_bk, "subscription", full=True)
    boot_pl = book_payload(bk, short(safest["policy"]), full=True)

    def bar_colour(r):
        if r["policy"] == "subscription":
            return "#6b7280"
        return "#59a14f" if r["wipeout_rate"] == "0%" else "#e15759"

    RBi = RB.set_index("policy")
    books, default_i = [], 0
    for label, b in boot_runs.items():
        if label == safest["policy"]:
            default_i = len(books)
        pl = book_payload(b, short(label))
        pl["colour"] = bar_colour(RBi.loc[label].to_dict() | {"policy": label})
        books.append(pl)

    # A policy is dominated when another beats it on BOTH the typical outcome and
    # the bad case. Those can be dropped without knowing anyone's risk appetite,
    # which is the only part of this choice the data can settle on its own.
    pts = RB[["policy", "net_median", "net_p10"]].to_dict("records")
    for p in pts:
        p["dominated"] = any(
            q["net_median"] >= p["net_median"] and q["net_p10"] >= p["net_p10"]
            and (q["net_median"] > p["net_median"] or q["net_p10"] > p["net_p10"])
            for q in pts)
    dom = {p["policy"]: p["dominated"] for p in pts}
    front = sorted((p for p in pts if not p["dominated"]), key=lambda p: p["net_p10"])

    R = RB.set_index("policy")

    def info(label):
        r = R.loc[label]
        return [short(label),
                f"net median {r['net_median']:,.0f} · net p10 {r['net_p10']:,.0f}<br>"
                f"banked {r['cash_median']:,.0f} · equity {r['equity_median']:,.0f}<br>"
                f"wipeouts in {r['wipeout_rate']} of windows · "
                f"{r['blowups']} blowups<br>"
                + ("DOMINATED - another policy beats it on both"
                   if dom[label] else "on the frontier")]

    # The chooser: each row is a constraint someone might actually have, and the
    # best policy under it. No row is "the answer" - which constraint is yours is
    # not something this data can tell you.
    def pick(mask, key, want, why_col, fmt):
        sel = RB[mask]
        if not len(sel):
            return None
        r = sel.loc[sel[key].idxmax()]
        return {"want": want, "policy": short(r["policy"]),
                "why": fmt.format(**r), "_p": r["policy"]}

    clean = RB["wipeout_rate"] == "0%"
    is_boot = RB["policy"] != "subscription"
    PICKS = [p for p in (
        pick(clean & is_boot, "net_median",
             "never losing the whole book, best overall",
             None, "net ${net_median:,.0f} median · ${cash_median:,.0f} of it banked "
                   "· p10 ${net_p10:,.0f} · {blowups} blowups"),
        pick(clean & is_boot, "cash_median",
             "cash in hand rather than equity at risk",
             None, "${cash_median:,.0f} banked · net ${net_median:,.0f} · "
                   "p10 ${net_p10:,.0f}"),
        pick(is_boot, "net_p10", "the best bad case, whatever the upside",
             None, "p10 ${net_p10:,.0f} · net ${net_median:,.0f} median · "
                   "wipeouts in {wipeout_rate} of windows"),
        pick(is_boot, "net_median", "the highest typical outcome, accepting wipeouts",
             None, "net ${net_median:,.0f} median but p10 ${net_p10:,.0f} · "
                   "wipeouts in {wipeout_rate} of windows · ruin {ruin_rate}"),
        pick(~is_boot, "net_median", "not touching withdrawn money at all (mode 1)",
             None, "net ${net_median:,.0f} median, all unrealized · "
                   "${spent:,.0f} of own capital · {blowups} blowups"),
    ) if p]

    build_html({
        "rr": a.rr, "dd_limit": rule.dd, "cost": rule.cost,
        "frozen_floor": rule.frozen_floor, "safety": rule.safety_net,
        "interval_days": cfg.interval_days, "path_label": a.intratrade_path,
        "horizon": a.horizon, "n_windows": len(q_starts),
        "cards": [
            ["reaches Safety Net", f"{FULL['frozen'].mean():.0%}", "ok",
             "of seats with a full year of runway"],
            ["median days to get there", f"{med:.0f}", "",
             f"p25 {okS['days_to_freeze'].quantile(.25):.0f} / "
             f"p75 {okS['days_to_freeze'].quantile(.75):.0f}"],
            ["mode 1 · net", f"${sub['net_median']:,.0f}", "",
             f"all unrealized · ${sub['spent']:,.0f} own capital · "
             f"{sub['blowups']} blowups"],
            ["mode 2 · best cash, any risk", f"${best['cash_median']:,.0f}",
             "bad" if best["wipeout_rate"] != "0%" else "ok",
             f"{best['policy'].replace('bootstrap · ', '')} · wiped out in "
             f"{best['wipeout_rate']} of windows · ruin {best['ruin_rate']}"],
            [f"mode 2 · {safe_label}", f"${safest['net_median']:,.0f}",
             "ok" if safest["wipeout_rate"] == "0%" else "bad",
             f"{boot_pl['name']} · ${safest['cash_median']:,.0f} realized · "
             f"{safest['blowups']} blowups"],
            ["seat cap", str(cfg.seats), "", "firm rule, not a maths question"],
        ],
        "sub": sub_pl,
        "boot": boot_pl,
        "books": books,
        "book_default": default_i,
        "front": {
            "x": [int(r["net_p10"]) for r in RB.to_dict("records")],
            "y": [int(r["net_median"]) for r in RB.to_dict("records")],
            # Only the frontier gets a printed label; the dominated cluster in the
            # middle is unreadable with fifteen captions on top of each other, and
            # those are the points you are meant to be ignoring anyway.
            "tag": ["" if dom[r["policy"]] else
                    short(r["policy"]).replace("1/interval, ", "")
                    for r in RB.to_dict("records")],
            "fill": ["rgba(0,0,0,0)" if dom[r["policy"]] else bar_colour(r)
                     for r in RB.to_dict("records")],
            "edge": [bar_colour(r) for r in RB.to_dict("records")],
            "size": [11 if not dom[r["policy"]] else 9
                     for r in RB.to_dict("records")],
            "symbol": ["square" if r["policy"] == "subscription" else "circle"
                       for r in RB.to_dict("records")],
            "info": [info(r["policy"]) for r in RB.to_dict("records")],
            "fx": [int(p["net_p10"]) for p in front],
            "fy": [int(p["net_median"]) for p in front],
        },
        "pick": PICKS,
        "starts": {"okx": [str(d) for d in okS["start"]],
                   "oky": [int(v) for v in okS["days_to_freeze"]],
                   "badx": [str(d) for d in badS["start"]],
                   "bady": [-40] * len(badS)},
        "dd": {"x": [str(d) for d in DDd["day"]],
               "eq": [round(float(v)) for v in DDd["eq"]],
               "peak": [round(float(v)) for v in DDd["peak"]],
               "low": [round(float(v)) for v in DDd["low"]],
               "closed": [round(float(v)) for v in DDd["dd_closed"]],
               "floating": [round(float(v)) for v in DDd["dd_float"]]},
        "dd_limit": rule.dd,
        "keep": {
            "labels": [r["policy"].replace("bootstrap · ", "") for r in
                       RB.to_dict("records")],
            "med": [int(v) for v in RB["cash_median"]],
            "hi": [int(h - m) for h, m in zip(RB["cash_p90"], RB["cash_median"])],
            "lo": [int(m - l) for m, l in zip(RB["cash_median"], RB["cash_p10"])],
            "colour": [bar_colour(r) for r in RB.to_dict("records")],
            "sub": [f"withdraws {r['withdraw']} · {r['wipeouts']:g} wipeouts · "
                    f"ruin {r['ruin_rate']} · equity left "
                    f"${r['equity_median']:,.0f}" for r in RB.to_dict("records")],
        },
        "robust": RB.to_dict("records"),
        "mstrat": bar_payload(m_strat),
        "mbook": bar_payload(m_book),
        "rec": [
            {"metric": "trades", "sim": f"{len(net):,}", "mt5": f"{MT5_REF['trades']:,}"},
            {"metric": "gross profit", "sim": f"${gross:,.0f}",
             "mt5": f"${MT5_REF['gross']:,.0f}"},
            {"metric": "equity drawdown", "sim": f"${dd_equity(net, mae, mfe):,.0f}",
             "mt5": f"${MT5_REF['eq_dd']:,.0f}"},
            {"metric": "windows merged", "sim": f"{len(st['windows'])}",
             "mt5": f"{len(st['windows']) + len(st['blown'])} "
                    f"(one export tester-blown, dropped)"},
        ],
    })


if __name__ == "__main__":
    main()
