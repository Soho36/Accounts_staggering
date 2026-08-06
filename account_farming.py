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
            return f"to ${self.keep:,.0f}"
        return f"{self.chunk / self.step:.0%} of gains"


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
    reserve: float = 0.0            # cash held back before any purchase


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


def trace_account(ex, net, mae, mfe, i0, h: Harvest, rule: Rule):
    """Full path of one seat: equity, the moving floor, and cash taken out."""
    a = new_account(i0, rule)
    path = []
    for i in range(i0, len(net)):
        step(a, net[i], mae[i], mfe[i], h, rule)
        path.append((pd.Timestamp(ex[i]), a["eq"] + a["banked"], a["eq"], a["floor"]))
        if not a["alive"]:
            break
    return path


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
                affordable = int(max(0.0, cash - cfg.reserve) // rule.cost)
                n_buy = min(cfg.max_per_event, room, affordable)
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

<h2>1 &middot; How a seat lives and dies</h2>
<div class="panel"><div id="c_life" style="height:400px"></div>
 <div class="note">The floor chases the peak upward until peak profit reaches the Safety
 Net, then freezes at $__FLOORV__ forever. Everything after that point is cushion. Withdrawing
 cash lowers equity toward that frozen floor &mdash; which is why harvesting kills seats.</div></div>

<h2>2 &middot; Does a seat reach the Safety Net? By start date</h2>
<div class="panel"><div id="c_starts" style="height:380px"></div>
 <div class="note">Green = reached the Safety Net, plotted at the number of days it took.
 Red = died first. The red clusters are what matters: seats started near each other share
 one fate, so staggering spreads entry points, not outcomes.</div></div>

<h2>3 &middot; Closed vs floating drawdown</h2>
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

<h2>4 &middot; Mode 1 &mdash; subscription: own money, one seat per interval, hold forever</h2>
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

<h2>5 &middot; Mode 2 &mdash; bootstrap: the seats pay for their own replacements</h2>
<div class="panel"><div id="c_boot" style="height:400px"></div>
 <div class="note">Seats live (filled) against cumulative cash withdrawn, under
 <b>__BESTV__</b>. Every step down in seat count is a liquidation.</div></div>
<div class="panel"><div id="c_boot_seats" style="height:420px"></div>
 <div class="note">__DUPV__ Each distinct purchase date is drawn once; hover for how many
 seats it stands for. Under a ratchet the lines stay spread out, because no seat is ever
 reset to a common level.</div>
 <div class="onepath">One illustrative path, not an expectation.</div></div>
<div class="panel"><div id="c_year" style="height:320px"></div>
 <div class="note">Cash per calendar year from that same book. The spread between best and
 worst year is the risk this design carries, and it is not smoothed by holding more seats,
 because the seats are not independent.</div>
 <div class="onepath">One illustrative path, not an expectation.</div></div>

<h2>6 &middot; Both modes, across every window</h2>
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
Plotly.newPlot('c_life',[
 {x:D.life.x,y:D.life.eq,type:'scatter',mode:'lines',name:'equity in the account',
  line:{width:1.8,color:'#111'}},
 {x:D.life.x,y:D.life.fl,type:'scatter',mode:'lines',name:'liquidation floor',
  line:{width:1.6,color:'#e15759',shape:'hv'}},
 {x:D.life.x,y:D.life.tot,type:'scatter',mode:'lines',name:'equity + cash taken out',
  line:{width:1.4,color:'#4e79a7',dash:'dot'}}],
 Object.assign({margin:{l:66,r:14,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'One seat, from purchase to liquidation',x:0,font:{size:13}},
  shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:D.safety,y1:D.safety,
   line:{color:'#15803d',width:1.2,dash:'dash'}}],
  annotations:[{xref:'paper',x:0.01,y:D.safety,text:'Safety Net - floor freezes here',
   showarrow:false,yshift:10,font:{size:11,color:'#15803d'}}],
  xaxis:{type:'date',gridcolor:'#eef0f3'},yaxis:{title:'$',gridcolor:'#eef0f3'},
  legend:{orientation:'h',y:-.14}},BG),CFG);
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
seatchart('c_boot_seats',D.boot.curves,
 'Bootstrap - every purchase date, P&L including cash already withdrawn');
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
Plotly.newPlot('c_boot',[
 {x:D.boot.x,y:D.boot.live,type:'scatter',mode:'lines',name:'seats live',
  fill:'tozeroy',line:{width:1,color:'#4e79a7',shape:'hv'},
  fillcolor:'rgba(78,121,167,.22)'},
 {x:D.boot.x,y:D.boot.cash,type:'scatter',mode:'lines',name:'cash withdrawn',
  yaxis:'y2',line:{width:2,color:'#15803d'}}],
 Object.assign({margin:{l:60,r:66,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'Seats live and cash taken out - '+D.boot.name,x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'seats',gridcolor:'#eef0f3',rangemode:'tozero'},
  yaxis2:{title:'cash $',overlaying:'y',side:'right',showgrid:false},
  legend:{orientation:'h',y:-.16}},BG),CFG);
Plotly.newPlot('c_year',[{x:D.year.x,y:D.year.y,type:'bar',
 marker:{color:'#4e79a7'},hovertemplate:'%{x}<br>$%{y:,.0f}<extra></extra>'}],
 Object.assign({margin:{l:70,r:14,t:28,b:34},font:F,
  title:{text:'Cash withdrawn per year',x:0,font:{size:13}},
  yaxis:{gridcolor:'#eef0f3'}},BG),CFG);
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
tbl('t_keep',D.robust,['policy','withdraw','ruin_rate','wipeout_rate','wipeouts',
 'blowups','cash_p10','cash_median','cash_p90','equity_median','net_p10',
 'net_median','seats_median'],
 ['policy','withdraws','ruin rate','windows with a wipeout','wipeouts (mean)',
  'blowups (median)','cash p10 $','cash MEDIAN $','cash p90 $',
  'equity left (median) $','net p10 $','net MEDIAN $','seats bought (median)']);
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
            .replace("__FLOORV__", f"{payload['frozen_floor']:,.0f}")
            .replace("__PATHV__", payload["path_label"])
            .replace("__DUPV__", payload["dup_note"])
            .replace("__BESTV__", payload["best_label"])
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
    ap.add_argument("--reserve", type=float, default=0.0,
                    help="cash held back before any purchase (bootstrap mode)")
    ap.add_argument("--withdraw-chunk", type=float, default=None,
                    help="ratchet: withdraw in units of this (default: one seat)")
    ap.add_argument("--withdraw-step", type=float, default=1000.0,
                    help="ratchet: one chunk per this much lifetime gain")
    ap.add_argument("--split", type=float, default=1.0,
                    help="trader's share of profit (real plans are 0.8-0.9)")
    ap.add_argument("--horizon", type=float, default=2.0,
                    help="years per book window in the robustness sweep")
    a = ap.parse_args()

    rule = Rule(dd=a.dd, frozen_floor=a.frozen_dd_floor, cost=a.cost,
                mfe_first=a.intratrade_path == "mfe-first")
    cfg = BookCfg(seats=a.seats, seed=a.seed, interval_days=a.interval_days,
                  policy=a.start_policy, profit_trigger=a.profit_trigger,
                  dd_trigger=a.dd_trigger, min_days=a.min_days_between_starts,
                  max_per_event=a.max_per_event, reserve=a.reserve)
    chunk = a.withdraw_chunk if a.withdraw_chunk else rule.cost
    HARD = Harvest("level", keep=rule.safety_net)

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
        base = {**cfg.__dict__, "funding": "cash", "max_per_event": 1,
                "reserve": 0.0}
        return BookCfg(**{**base, **kw})

    POLICIES = [
        ("subscription", BookCfg(**{**cfg.__dict__, "funding": "external",
                                    "seed": 0.0, "max_per_event": 1,
                                    "reserve": 0.0}), Harvest("none")),
        # the old winner, kept as the baseline the new conditions have to beat
        ("bootstrap · all-in, strip to net",
         boot(max_per_event=cfg.seats), HARD),
        ("bootstrap · 1/interval, strip to net", boot(), HARD),
        ("bootstrap · 1/interval, keep $4,000", boot(),
         Harvest("level", keep=4000.0)),
        ("bootstrap · 1/interval, never withdraw", boot(), Harvest("none")),
    ]
    for st_ in (400.0, 600.0, 1000.0, 2000.0):
        POLICIES.append((f"bootstrap · 1/interval, {chunk / st_:.0%} of gains",
                         boot(), Harvest("ratchet", chunk=chunk, step=st_)))
    for rsv in (2, 5):
        POLICIES.append((f"bootstrap · 1/interval, {chunk / 1000:.0%} + {rsv}-seat "
                         f"reserve", boot(reserve=rsv * rule.cost),
                         Harvest("ratchet", chunk=chunk, step=1000.0)))
    # Whatever the CLI actually asked for, if it is not already in the menu.
    if (a.max_per_event != 1 or a.reserve or a.withdraw_step != 1000.0
            or a.withdraw_chunk):
        POLICIES.append((
            f"bootstrap · custom: {a.max_per_event}/event, "
            f"{chunk / a.withdraw_step:.0%} of gains"
            + (f", ${a.reserve:,.0f} reserve" if a.reserve else ""),
            boot(max_per_event=a.max_per_event, reserve=a.reserve),
            Harvest("ratchet", chunk=chunk, step=a.withdraw_step)))

    rowsr = []
    for label, c, h in POLICIES:
        res = [run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], h, rule, c)
               for _, j, k in q_starts]
        cashes = np.array([r["cash"] for r in res]) * a.split
        equities = np.array([r["equity"] for r in res]) * a.split
        # For the subscription the seats are bought with the trader's own money,
        # so its net is equity minus what was spent. For the bootstrap nothing
        # external goes in, so realized cash is the number that matters.
        nets = cashes + equities - np.array([r["spent"] for r in res])
        rowsr.append({
            "policy": label,
            "withdraw": h.label,
            "ruin_rate": "n/a" if c.funding == "external"
                         else f"{np.mean([r['ruined'] for r in res]):.0%}",
            # Share of windows that saw at least one wipeout, not the median
            # count: wipeouts are rare enough per window that a median of 0 hides
            # a policy which loses the whole book several times over a long run.
            "wipeout_rate": f"{np.mean([r['wipeouts'] > 0 for r in res]):.0%}",
            "wipeouts": round(float(np.mean([r["wipeouts"] for r in res])), 2),
            "blowups": int(np.median([r["deaths"] for r in res])),
            "spent": round(float(np.median([r["spent"] for r in res]))),
            "cash_p10": round(np.percentile(cashes, 10)),
            "cash_median": round(np.median(cashes)),
            "cash_p90": round(np.percentile(cashes, 90)),
            "equity_median": round(np.median(equities)),
            "net_p10": round(np.percentile(nets, 10)),
            "net_median": round(np.median(nets)),
            "seats_median": int(np.median([r["bought"] for r in res])),
            "starts_median": int(np.median([r["starts"] for r in res])),
        })
    RB = pd.DataFrame(rowsr)
    cols = ["policy", "withdraw", "ruin_rate", "wipeout_rate", "wipeouts", "blowups",
            "cash_median", "equity_median", "net_p10", "net_median", "seats_median"]
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
    bs = bk["series"].groupby(bk["series"]["day"].dt.date).last().reset_index(drop=True)
    ycum = (bk["series"].assign(y=bk["series"]["day"].dt.year)
            .groupby("y")["withdrawn"].last())
    bkyr = (ycum.diff().fillna(ycum.iloc[0]) * a.split).round()

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

    rep = FULL[FULL["frozen"]].iloc[
        (FULL[FULL["frozen"]]["days_to_freeze"] - med).abs().argsort().iloc[0]]
    path = thin(trace_account(ex, net, mae, mfe, int(rep["start_i"]), HARD, rule), 1500)

    # Seats bought on the same trade are not merely similar, they are identical:
    # same rule, same stream, same withdrawal level, so the paths coincide to the
    # cent. Drawing all of them stacks opaque duplicates on one curve and makes
    # the book look more diversified than it is. Draw each distinct start once
    # and carry the multiplicity in the hover instead.
    def book_payload(b):
        s = b["series"]
        ds = s.groupby(s["day"].dt.date).last().reset_index(drop=True)
        groups: dict[int, list] = {}
        for seat in b["seats"]:
            groups.setdefault(seat["i0"], []).append(seat)
        curves = []
        for i0 in sorted(groups):
            g = groups[i0]
            pts = thin(g[0]["path"], 260)
            curves.append({"x": [p[0].strftime("%Y-%m-%d") for p in pts],
                           "y": [round(p[1]) for p in pts],
                           "n": len(g),
                           "d": g[0]["start"].strftime("%Y-%m-%d")})
        return {
            "x": [str(d) for d in ds["day"]],
            "live": [int(v) for v in ds["live"]],
            "cash": [round(float(v)) for v in ds["withdrawn"]],
            "equity": [round(float(v)) for v in ds["equity"]],
            "wealth": [round(float(v)) for v in ds["wealth"]],
            "spent": [round(float(v)) for v in ds["spent"]],
            "curves": curves,
            "bought": b["bought"], "starts": b["starts"],
            "deaths": b["deaths"], "wipeouts": b["wipeouts"], "live_end": b["live"],
            "final_cash": round(b["cash"]), "final_equity": round(b["equity"]),
            "spent_total": round(b["spent"]), "net": round(b["wealth"]),
        }

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

    sub_pl = book_payload(sub_bk)
    boot_pl = book_payload(bk)
    boot_pl["name"] = safest["policy"].replace("bootstrap · ", "")

    def bar_colour(r):
        if r["policy"] == "subscription":
            return "#6b7280"
        return "#59a14f" if r["wipeout_rate"] == "0%" else "#e15759"

    build_html({
        "rr": a.rr, "dd_limit": rule.dd, "cost": rule.cost,
        "frozen_floor": rule.frozen_floor, "safety": rule.safety_net,
        "interval_days": cfg.interval_days, "path_label": a.intratrade_path,
        "dup_note": (f"{boot_pl['bought']} seats were bought on "
                     f"{boot_pl['starts']} distinct dates."),
        "best_label": boot_pl["name"],
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
        "life": {"x": [str(p[0]) for p in path],
                 "tot": [round(p[1], 1) for p in path],
                 "eq": [round(p[2], 1) for p in path],
                 "fl": [round(p[3], 1) for p in path]},
        "sub": sub_pl,
        "boot": boot_pl,
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
        "year": {"x": [str(i) for i in bkyr.index],
                 "y": [float(v) for v in bkyr]},
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
