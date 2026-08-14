"""Causal all-window signal routing across independent prop-account DD buffers.

The existing schedule study assigns clock windows to fixed seats.  This helper
asks a different question: if every exported RR=1 signal remains eligible, can a
live dispatcher remove cross-window blocking and improve prop-account cash
economics without changing market exposure?

Two controls are deliberately kept separate:

* ``R`` is signal multiplicity: every offer requests exactly R portfolio copies.
  Comparing routing policies at the same R is the same-exposure comparison.
* ``K`` is the number of paid accounts (drawdown containers) available to hold
  those copies.  More K costs more, but may spread losses and payout progress.

Routing is causal.  A policy sees only free/live seats and their state immediately
before an entry.  It never sees the trade's outcome, future overlaps, or a ranking
of time windows.  All 23 isolated-window exports remain eligible.  Those exports
already contain within-window MT5 blocking, so this study can remove cross-window
blocking but cannot reconstruct signals absent from the source files.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_farming as af  # noqa: E402


RR = 1.0
WINDOWS = af.CANONICAL_WINDOWS
K_GRID = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)
R_GRID = (1, 2, 3, 4, 6, 8)
CAPACITY_K_GRID = K_GRID + (24, 25, 30, 32, 35, 40)
CAPACITY_R_GRID = tuple(range(1, 9))
POLICIES = (
    "round_robin",
    "least_trades",
    "max_headroom",
    "min_headroom",
    "protect_frozen",
    "stable_hash",
)
EPISODE_POLICIES = (
    "round_robin", "least_trades", "max_headroom", "protect_frozen",
    "stable_hash",
)
# Repeated lifecycle distributions focus on the full-capacity boundary 5R and
# economically plausible cheaper/more-buffered neighbours. The raw and headline
# holdout tables still score the complete Cartesian K/R/policy grid.
EPISODE_K_BY_R = {
    1: K_GRID,
    2: (4, 5, 6, 8, 10, 12, 15, 20),
    3: (6, 8, 10, 12, 15, 20),
    4: (8, 10, 12, 15, 20),
    # Approximate the aggregate trades/hours/risk of 8 and 10 fully copied
    # legacy all-window accounts, respectively.
    6: (20, 24, 25, 30, 35, 40),
    8: (25, 30, 32, 35, 40),
}
HOLDOUT_K_BY_R = {
    copies: (K_GRID if copies <= 4 else EPISODE_K_BY_R[copies])
    for copies in R_GRID
}
TRAIN_START = pd.Timestamp("2020-01-01")
TEST_START = pd.Timestamp("2023-01-01")


@dataclass(frozen=True)
class RouteCfg:
    k: int
    copies: int
    policy: str
    replacement_reserve: float = 1_000.0
    split: float = 0.90
    dollars_per_point: float = 2.0
    max_replacements_per_exit: int = 1


@dataclass(frozen=True)
class Period:
    label: str
    start: pd.Timestamp
    end: pd.Timestamp
    kind: str


@dataclass(frozen=True)
class RouteOffer:
    offer_id: int
    entry: pd.Timestamp
    exit: pd.Timestamp
    net: float
    mae: float
    mfe: float
    stop_risk: float


def _window_start(window: str) -> int:
    return int(window.split("-", 1)[0])


def strict_paths(sweep_root: Path, stats_root: Path) -> dict[str, Path]:
    """Require the exact complete RR=1 universe used by every comparison."""
    missing: list[str] = []
    result: dict[str, Path] = {}
    for window in WINDOWS:
        path = sweep_root / window / f"{window}_{RR:.2f}.csv"
        if not path.is_file():
            missing.append(f"{window}: missing export")
            continue
        state = af.pass_is_blown(stats_root, window, RR)
        if state is True:
            missing.append(f"{window}: tester-truncated")
        elif state is None:
            missing.append(f"{window}: missing/unreadable stats")
        else:
            result[window] = path
    if missing:
        raise RuntimeError(
            "Signal routing requires all 23 validated RR=1 passes: "
            + "; ".join(missing)
        )
    return result


def load_offers(sweep_root: Path, stats_root: Path,
                dollars_per_point: float = 2.0) -> pd.DataFrame:
    """Load the pre-cross-window-replay offer tape with stable unique IDs."""
    paths = strict_paths(sweep_root, stats_root)
    frames = []
    for window in WINDOWS:
        frame = af.read_trade_file(paths[window]).copy()
        if "Candle_range" not in frame:
            raise RuntimeError(
                f"{paths[window]} has no candle_range field; nominal stop-risk "
                "matching would be impossible."
            )
        if frame["Candle_range"].isna().any() or (frame["Candle_range"] <= 0).any():
            raise RuntimeError(f"{paths[window]} has invalid candle_range values.")
        frame["window"] = window
        frame["window_order"] = _window_start(window)
        frame["source_row"] = np.arange(len(frame), dtype=int)
        frames.append(frame)
    offers = pd.concat(frames, ignore_index=True)
    offers = offers.sort_values(
        ["Entry_time", "Exit_time", "window_order", "source_row"],
        kind="stable",
    ).reset_index(drop=True)
    offers["offer_id"] = np.arange(len(offers), dtype=int)
    offers["net"] = offers["PNL"].astype(float) - af.COMMISSION_ROUNDTURN
    offers["stop_risk"] = offers["Candle_range"].astype(float) * dollars_per_point
    offers["duration_hours"] = (
        (offers["Exit_time"] - offers["Entry_time"]).dt.total_seconds() / 3600.0
    ).clip(lower=0.0)
    return offers


def phase_offers(offers: pd.DataFrame, start: pd.Timestamp,
                 end: pd.Timestamp) -> pd.DataFrame:
    """Start flat; retain cross-end entries because they consume capacity."""
    mask = (offers["Entry_time"] >= start) & (offers["Entry_time"] < end)
    tie_columns = [column for column in
                   ("Entry_time", "Exit_time", "window_order", "source_row", "offer_id")
                   if column in offers.columns]
    return offers.loc[mask].copy().sort_values(
        tie_columns, kind="stable",
    ).reset_index(drop=True)


def prepare_route_tape(offers: pd.DataFrame, period: Period) -> list[RouteOffer]:
    """Convert a period once; thousands of simulations reuse these immutable rows."""
    frame = phase_offers(offers, period.start, period.end)
    return [
        RouteOffer(
            int(row.offer_id), pd.Timestamp(row.Entry_time),
            pd.Timestamp(row.Exit_time), float(row.net), float(row.MAE),
            float(row.MFE), float(row.stop_risk),
        )
        for row in frame.itertuples(index=False)
    ]


def peak_overlap(offers: pd.DataFrame, end: pd.Timestamp | None = None) -> int:
    """Maximum positive-duration [entry, exit) interval overlap."""
    events: list[tuple[pd.Timestamp, int]] = []
    for row in offers.itertuples(index=False):
        exit_time = min(row.Exit_time, end) if end is not None else row.Exit_time
        if exit_time <= row.Entry_time:
            continue
        events.append((row.Entry_time, 1))
        events.append((exit_time, -1))
    # An exit at T frees its seat for an entry at T.
    events.sort(key=lambda item: (item[0], item[1]))
    live = maximum = 0
    for _, change in events:
        live += change
        maximum = max(maximum, live)
    return maximum


def capacity_measure(offers: pd.DataFrame, period: Period, k: int,
                     copies: int) -> dict:
    """Immortal-seat interval routing; outcomes never affect availability."""
    tape = phase_offers(offers, period.start, period.end)
    busy_until = [period.start - pd.Timedelta(seconds=1)] * k
    requested = filled = covered = full = 0
    filled_hours = filled_risk = filled_net = 0.0
    maximum = 0
    for row in tape.itertuples(index=False):
        free = [index for index, until in enumerate(busy_until)
                if until <= row.Entry_time]
        n = min(copies, len(free))
        requested += copies
        filled += n
        covered += n > 0
        full += n == copies
        scored_exit = min(row.Exit_time, period.end)
        hours = max(0.0, (scored_exit - row.Entry_time).total_seconds() / 3600.0)
        filled_hours += n * hours
        filled_risk += n * float(row.stop_risk)
        if row.Exit_time < period.end:
            filled_net += n * float(row.net)
        for seat_index in free[:n]:
            busy_until[seat_index] = row.Exit_time
        maximum = max(maximum, sum(until > row.Entry_time for until in busy_until))
    offers_n = len(tape)
    return {
        "evaluation": period.label,
        "evaluation_kind": period.kind,
        "start": period.start,
        "end": period.end,
        "k": k,
        "copies": copies,
        "offers": offers_n,
        "requested_copies": requested,
        "filled_copies": filled,
        "fill_rate": filled / requested if requested else math.nan,
        "offer_coverage": covered / offers_n if offers_n else math.nan,
        "full_copy_coverage": full / offers_n if offers_n else math.nan,
        "contract_hours": filled_hours,
        "initial_stop_risk": filled_risk,
        "raw_net_completed": filled_net,
        "max_concurrent_contracts": maximum,
        "raw_peak_overlap": peak_overlap(tape, period.end),
        "mechanical_full_capacity": k >= copies * peak_overlap(tape, period.end),
    }


def legacy_one_slot_measure(offers: pd.DataFrame, period: Period) -> dict:
    """Measure one existing globally blocked all-window account on the same tape."""
    parts = []
    frames = []
    for window in WINDOWS:
        frame = offers[offers["window"] == window].copy()
        frame = frame[(frame["Entry_time"] >= period.start) &
                      (frame["Entry_time"] < period.end)]
        frame = frame.sort_values("Exit_time", kind="stable").reset_index(drop=True)
        frames.append(frame)
        parts.append({
            "en": frame["Entry_time"].values.astype("datetime64[s]"),
            "ex": frame["Exit_time"].values.astype("datetime64[s]"),
        })
    keep = af.replay(parts)
    selected = []
    for frame, mask in zip(frames, keep):
        taken = frame.loc[mask]
        # Cross-end positions participate in replay/blocking, but their future
        # outcome is not included in the period's exposure/P&L measure.
        selected.append(taken[taken["Exit_time"] < period.end])
    stream = pd.concat(selected, ignore_index=True)
    return {
        "trades": len(stream),
        "contract_hours": float(stream["duration_hours"].sum()),
        "initial_stop_risk": float(stream["stop_risk"].sum()),
        "raw_net": float(stream["net"].sum()),
    }


def _new_seat(seat_id: int, rule: af.Rule, available_after: pd.Timestamp,
              initial: bool) -> dict:
    seat = af.new_account(0, rule)
    seat.update({
        "seat_id": seat_id,
        "busy": False,
        "assigned": 0,
        "completed": 0,
        "available_after": available_after,
        "initial": initial,
    })
    return seat


def _headroom(seat: dict) -> float:
    return float(seat["eq"] - seat["floor"])


def _stable_hash(offer_id: int, seat_id: int) -> int:
    payload = f"{offer_id}:{seat_id}".encode("ascii")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def choose_seats(free: list[dict], copies: int, policy: str,
                 offer_id: int, rr_cursor: int) -> tuple[list[dict], int]:
    """Choose distinct free seats using entry-time information only."""
    if policy not in POLICIES:
        raise ValueError(f"Unknown routing policy: {policy}")
    if not free or copies <= 0:
        return [], rr_cursor
    if policy == "round_robin":
        ordered_ids = sorted(seat["seat_id"] for seat in free)
        at_or_after = [seat_id for seat_id in ordered_ids if seat_id >= rr_cursor]
        before = [seat_id for seat_id in ordered_ids if seat_id < rr_cursor]
        order = at_or_after + before
        lookup = {seat["seat_id"]: seat for seat in free}
        chosen = [lookup[seat_id] for seat_id in order[:copies]]
        if chosen:
            rr_cursor = chosen[-1]["seat_id"] + 1
        return chosen, rr_cursor
    if policy == "least_trades":
        key = lambda seat: (seat["assigned"], seat["seat_id"])
    elif policy == "max_headroom":
        key = lambda seat: (-_headroom(seat), seat["assigned"], seat["seat_id"])
    elif policy == "min_headroom":
        key = lambda seat: (_headroom(seat), seat["assigned"], seat["seat_id"])
    elif policy == "protect_frozen":
        # Frozen accounts are payout-capable assets. Use immature accounts first;
        # overflow to mature ones only when capacity requires it.
        key = lambda seat: (seat["frozen"], seat["assigned"], seat["seat_id"])
    else:  # stable_hash
        key = lambda seat: (_stable_hash(offer_id, seat["seat_id"]), seat["seat_id"])
    return sorted(free, key=key)[:copies], rr_cursor


def simulate_route(offers: pd.DataFrame, period: Period, cfg: RouteCfg,
                   harvest: af.Harvest, rule: af.Rule,
                   prepared: list[RouteOffer] | None = None) -> dict:
    """Route one period and apply the existing per-account DD/payout mechanics."""
    if cfg.k <= 0 or cfg.copies <= 0:
        raise ValueError("k and copies must be positive")
    if not 0 < cfg.split <= 1:
        raise ValueError("split must be in (0, 1]")
    tape = prepared if prepared is not None else prepare_route_tape(offers, period)
    initial_available = period.start - pd.Timedelta(seconds=1)
    seats = {
        seat_id: _new_seat(seat_id, rule, initial_available, True)
        for seat_id in range(cfg.k)
    }
    next_seat_id = cfg.k
    rr_cursor = 0
    # Heap item: (exit ns, offer id, serial, seat id, tape row index).
    positions: list[tuple[int, int, int, int, int]] = []
    serial = 0
    cash = float(cfg.replacement_reserve)
    initial_seat_cost = cfg.k * rule.cost
    initial_capital = cfg.replacement_reserve + initial_seat_cost
    total_seat_spend = initial_seat_cost
    replacements = deaths = zero_seat_events = 0
    worst_cluster = cluster_events = 0
    worst_cluster_fraction = 0.0
    requested = filled = covered = full = completed_fills = 0
    filled_hours = filled_risk = delivered_raw_net = 0.0
    gross_payouts = net_payouts = 0.0
    first_payout: pd.Timestamp | None = None
    maximum_contracts = 0
    min_live = cfg.k
    ruined = False

    def buy_replacements(event_time: pd.Timestamp) -> None:
        nonlocal cash, next_seat_id, replacements, total_seat_spend, min_live
        room = cfg.k - len(seats)
        count = min(cfg.max_replacements_per_exit, room, int(cash // rule.cost))
        for _ in range(max(0, count)):
            seat = _new_seat(next_seat_id, rule, event_time, False)
            seats[next_seat_id] = seat
            next_seat_id += 1
            replacements += 1
            cash -= rule.cost
            total_seat_spend += rule.cost
        min_live = min(min_live, len(seats))

    def close_timestamp(groups: list[list[tuple[int, int, int, int, int]]]) -> None:
        """Settle every offer exiting at one timestamp as one liquidation event."""
        nonlocal cash, deaths, zero_seat_events, worst_cluster
        nonlocal worst_cluster_fraction, cluster_events, gross_payouts
        nonlocal net_payouts, first_payout, completed_fills, delivered_raw_net
        nonlocal min_live, ruined
        if not groups:
            return
        event_time = tape[groups[0][0][4]].exit
        n_before = len(seats)
        dead_ids: list[int] = []
        got = 0.0
        # One seat cannot hold two positions, so groups at a timestamp have
        # disjoint seats. Offer ordering at the shared exit cannot change state.
        for group in groups:
            row = tape[group[0][4]]
            for _, _, _, seat_id, _ in group:
                seat = seats.get(seat_id)
                if seat is None:
                    raise RuntimeError("A routed position outlived its account.")
                seat["busy"] = False
                got += af.step(
                    seat, row.net, row.mae, row.mfe, harvest, rule,
                )
                seat["completed"] += 1
                completed_fills += 1
                delivered_raw_net += row.net
                if not seat["alive"]:
                    dead_ids.append(seat_id)
        gross_payouts += got
        received = got * cfg.split
        net_payouts += received
        cash += received
        if received > 0 and first_payout is None:
            first_payout = event_time
        for seat_id in dead_ids:
            seats.pop(seat_id, None)
        event_deaths = len(dead_ids)
        deaths += event_deaths
        if event_deaths:
            fraction = event_deaths / n_before if n_before else 0.0
            worst_cluster = max(worst_cluster, event_deaths)
            worst_cluster_fraction = max(worst_cluster_fraction, fraction)
            cluster_events += event_deaths >= 5
            if not seats:
                zero_seat_events += 1
        # Record the liquidation trough before an instantaneous replacement can
        # make the book look healthier than it was at this event.
        min_live = min(min_live, len(seats))
        # Preserve the project rule: at most one new seat per market event. A
        # timestamp with several exits is one event, not several rebuy chances.
        buy_replacements(event_time)
        min_live = min(min_live, len(seats))
        if not seats and cash < rule.cost:
            ruined = True

    def close_until(cutoff: pd.Timestamp, inclusive: bool) -> None:
        cutoff_ns = int(cutoff.value)
        while positions and (positions[0][0] <= cutoff_ns if inclusive
                             else positions[0][0] < cutoff_ns):
            exit_ns = positions[0][0]
            timestamp_items = []
            while positions and positions[0][0] == exit_ns:
                timestamp_items.append(heapq.heappop(positions))
            groups: list[list[tuple[int, int, int, int, int]]] = []
            for item in timestamp_items:
                if not groups or groups[-1][0][1] != item[1]:
                    groups.append([item])
                else:
                    groups[-1].append(item)
            close_timestamp(groups)

    for row_index, row in enumerate(tape):
        entry = row.entry
        close_until(entry, inclusive=True)
        requested += cfg.copies
        free = [seat for seat in seats.values()
                if not seat["busy"] and seat["available_after"] < entry]
        chosen, rr_cursor = choose_seats(
            free, cfg.copies, cfg.policy, row.offer_id, rr_cursor,
        )
        n = len(chosen)
        filled += n
        covered += n > 0
        full += n == cfg.copies
        scored_exit = min(row.exit, period.end)
        duration = max(0.0, (scored_exit - entry).total_seconds() / 3600.0)
        filled_hours += n * duration
        filled_risk += n * row.stop_risk
        for seat in chosen:
            seat["busy"] = True
            seat["assigned"] += 1
            heapq.heappush(positions, (
                int(row.exit.value), row.offer_id, serial, seat["seat_id"], row_index,
            ))
            serial += 1
        maximum_contracts = max(maximum_contracts, len(positions))
        # Correctly settle the one zero-duration export before the next entry.
        if row.exit <= entry:
            close_until(entry, inclusive=True)

    # Cross-horizon positions consumed capacity but their future outcomes are not
    # known/scored at this fresh evaluation boundary.
    close_until(period.end, inclusive=False)
    # No current evaluation boundary has a cross-end position, but terminal cash
    # values must remain correct for arbitrary periods: an open seat cannot be
    # liquidated or safely stripped until its unresolved trade closes.
    live = list(seats.values())
    raw_prop_pnl = sum(float(seat["eq"]) for seat in live)
    positive_prop_value = cfg.split * sum(max(0.0, float(seat["eq"])) for seat in live)
    safe_withdrawable = cfg.split * sum(
        max(0.0, float(seat["eq"]) - rule.safety_net)
        for seat in live if seat["frozen"] and not seat["busy"]
    )
    offers_n = len(tape)
    assigned_counts = [float(seat["assigned"]) for seat in live]
    first_payout_days = (
        (first_payout - period.start).total_seconds() / 86400.0
        if first_payout is not None else math.nan
    )
    return {
        "evaluation": period.label,
        "evaluation_kind": period.kind,
        "start": period.start,
        "end": period.end,
        "k": cfg.k,
        "copies": cfg.copies,
        "policy": cfg.policy,
        "split": cfg.split,
        "replacement_reserve": cfg.replacement_reserve,
        "max_replacements_per_exit": cfg.max_replacements_per_exit,
        "offers": offers_n,
        "requested_copies": requested,
        "filled_copies": filled,
        "completed_fills": completed_fills,
        "fill_rate": filled / requested if requested else math.nan,
        "offer_coverage": covered / offers_n if offers_n else math.nan,
        "full_copy_coverage": full / offers_n if offers_n else math.nan,
        "contract_hours": filled_hours,
        "initial_stop_risk": filled_risk,
        "delivered_raw_net": delivered_raw_net,
        "max_concurrent_contracts": maximum_contracts,
        "initial_seat_cost": initial_seat_cost,
        "initial_capital": initial_capital,
        "total_seat_spend": total_seat_spend,
        "replacements": replacements,
        "seats_bought": cfg.k + replacements,
        "deaths": deaths,
        "zero_seat_events": zero_seat_events,
        "ruined": ruined,
        "worst_death_cluster": worst_cluster,
        "worst_death_fraction": worst_cluster_fraction,
        "large_shock_events": cluster_events,
        "min_live_seats": min_live,
        "terminal_live_seats": len(live),
        "terminal_frozen_seats": sum(bool(seat["frozen"]) for seat in live),
        "gross_payouts": gross_payouts,
        "net_payouts": net_payouts,
        "terminal_cash": cash,
        "raw_prop_pnl": raw_prop_pnl,
        "safe_withdrawable": safe_withdrawable,
        "realized_wealth": cash - initial_capital,
        "cashout_wealth": cash + safe_withdrawable - initial_capital,
        "mark_to_model_wealth": cash + positive_prop_value - initial_capital,
        "cashout_return_on_initial": (
            (cash + safe_withdrawable - initial_capital) / initial_capital
            if initial_capital else math.nan
        ),
        "first_payout_days": first_payout_days,
        "live_assignment_std": float(np.std(assigned_counts)) if assigned_counts else math.nan,
    }


def periods_for_sample(offers: pd.DataFrame, horizon_years: float = 2.0) -> tuple[
        list[Period], list[Period]]:
    sample_end = pd.Timestamp(offers["Exit_time"].max()) + pd.Timedelta(seconds=1)
    headline = [
        Period("train_2020_2022", TRAIN_START, TEST_START, "holdout"),
        Period("test_2023_plus", TEST_START, sample_end, "holdout"),
    ]
    horizon = pd.Timedelta(days=int(365.25 * horizon_years))
    episodes = []
    for start in pd.date_range(TRAIN_START, sample_end, freq="QS"):
        end = start + horizon
        if end > sample_end:
            break
        episodes.append(Period(
            f"episode_{start.date()}_{end.date()}", start, end,
            "overlapping_2y_episode",
        ))
    return headline, episodes


def _q(values: pd.Series, probability: float) -> float:
    return float(values.quantile(probability))


def summarize_episodes(economics: pd.DataFrame) -> pd.DataFrame:
    episode = economics[economics["evaluation_kind"] == "overlapping_2y_episode"]
    # Select K/policy only from episodes whose outcomes end before the 2023
    # holdout. They overlap one another, but never leak a holdout result.
    selection = episode[pd.to_datetime(episode["end"]) <= TEST_START]
    rows = []
    for (k, copies, policy), group in selection.groupby(
            ["k", "copies", "policy"], sort=True):
        cashout = group["cashout_wealth"]
        p10 = _q(cashout, 0.10)
        rows.append({
            "k": k,
            "copies": copies,
            "policy": policy,
            "episodes": len(group),
            "selection_end": TEST_START,
            "fill_rate_median": float(group["fill_rate"].median()),
            "fill_rate_p10": _q(group["fill_rate"], 0.10),
            "offer_coverage_median": float(group["offer_coverage"].median()),
            "cashout_median": float(cashout.median()),
            "cashout_p10": p10,
            "cashout_es10": float(cashout[cashout <= p10].mean()),
            "realized_median": float(group["realized_wealth"].median()),
            "net_payouts_median": float(group["net_payouts"].median()),
            "ruin_rate": float(group["ruined"].mean()),
            "zero_seat_episode_rate": float((group["zero_seat_events"] > 0).mean()),
            "large_shock_episode_rate": float((group["large_shock_events"] > 0).mean()),
            "worst_death_cluster": int(group["worst_death_cluster"].max()),
            "replacements_median": float(group["replacements"].median()),
            "first_payout_days_median": float(group["first_payout_days"].median()),
            "initial_capital": float(group["initial_capital"].iloc[0]),
            "cashout_roi_median": float(group["cashout_return_on_initial"].median()),
        })
    summary = pd.DataFrame(rows)
    holdout = economics[economics["evaluation"] == "test_2023_plus"].copy()
    holdout = holdout.rename(columns={
        "fill_rate": "test_fill_rate",
        "cashout_wealth": "test_cashout",
        "realized_wealth": "test_realized",
        "net_payouts": "test_net_payouts",
        "ruined": "test_ruined",
        "worst_death_cluster": "test_worst_death_cluster",
        "replacements": "test_replacements",
    })
    keep = ["k", "copies", "policy", "test_fill_rate", "test_cashout",
            "test_realized", "test_net_payouts", "test_ruined",
            "test_worst_death_cluster", "test_replacements"]
    return summary.merge(holdout[keep], on=["k", "copies", "policy"], how="left")


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def markdown_report(offers: pd.DataFrame, capacity: pd.DataFrame,
                    economics: pd.DataFrame, summary: pd.DataFrame, cfg_template: RouteCfg,
                    episode_count: int) -> str:
    train = capacity[(capacity["evaluation"] == "train_2020_2022") &
                     (capacity["k"] == 5) & (capacity["copies"] == 1)].iloc[0]
    test = capacity[(capacity["evaluation"] == "test_2023_plus") &
                    (capacity["k"] == 5) & (capacity["copies"] == 1)].iloc[0]
    test_period = Period(
        "test_2023_plus", pd.Timestamp(test["start"]), pd.Timestamp(test["end"]),
        "holdout",
    )
    legacy = legacy_one_slot_measure(offers, test_period)
    display_k = (1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 35, 40)
    lines = [
        "# Dynamic all-window signal routing",
        "",
        "This is an exploratory routing study, not a live allocation rule. It keeps all "
        "23 entry windows enabled and dispatches each isolated-window export to free "
        "accounts. `R` fixes requested copies per signal (market exposure); `K` is the "
        "number of paid prop-account drawdown buffers.",
        "",
        "## Mechanical capacity",
        "",
        f"The raw tape has {int(train['offers']):,} completed/offered training entries "
        f"and {int(test['offers']):,} test entries. Peak positive-duration overlap is "
        f"{int(max(train['raw_peak_overlap'], test['raw_peak_overlap']))}. Therefore the "
        "observed no-death requirement for every copy is `K >= 5R`: 5, 10, 15, "
        "20, 30, and 40 accounts for R=1, 2, 3, 4, 6, and 8 respectively.",
        "",
        "| R | " + " | ".join(f"K={k}" for k in display_k) + " |",
        "|---:|" + "---:|" * len(display_k),
    ]
    test_cap = capacity[capacity["evaluation"] == "test_2023_plus"]
    for copies in R_GRID:
        values = []
        for k in display_k:
            row = test_cap[(test_cap["k"] == k) & (test_cap["copies"] == copies)].iloc[0]
            values.append(_pct(float(row["fill_rate"])))
        lines.append(f"| {copies} | " + " | ".join(values) + " |")
    lines += [
        "",
        "Fill rate is filled copies / requested copies with immortal accounts. Partial "
        "rows are not strictly exposure-matched because congestion chooses which offers "
        "are omitted.",
        "",
        "### Relation to the existing copied all-window book",
        "",
        f"In the 2023+ holdout, one current globally blocked account took "
        f"{legacy['trades']:,} trades / {legacy['contract_hours']:,.0f} contract-hours / "
        f"{_money(legacy['initial_stop_risk'])} nominal initial stop risk. One recovered "
        f"all-window copy took {int(test['offers']):,} offers / "
        f"{test['contract_hours']:,.0f} hours / {_money(test['initial_stop_risk'])} risk. "
        "The recovered tape therefore carries more exposure per copy.",
        "",
        "| Existing copied accounts | Equivalent recovered-tape R by trades | by hours | "
        "by initial stop risk | Exact no-block K=5R (rounded exposure) |",
        "|---:|---:|---:|---:|---:|",
    ]
    for legacy_copies in (8, 10):
        r_trades = legacy_copies * legacy["trades"] / float(test["offers"])
        r_hours = legacy_copies * legacy["contract_hours"] / float(test["contract_hours"])
        r_risk = legacy_copies * legacy["initial_stop_risk"] / float(test["initial_stop_risk"])
        rounded = max(1, round((r_trades + r_hours + r_risk) / 3))
        lines.append(
            f"| {legacy_copies} | {r_trades:.2f} | {r_hours:.2f} | {r_risk:.2f} | "
            f"about {5 * rounded} (R={rounded}) |"
        )
    lines += [
        "",
        "Thus 8-10 routed seats are enough for one or approximately two copies of every "
        "offer, but they cannot both preserve the exposure of 8-10 fully copied legacy "
        "accounts and eliminate overlap blocking. That higher exposure needs roughly "
        "R=6-8 and 30-40 seats for exact observed coverage.",
        "",
        "## Prop-account economics",
        "",
        f"Assumptions: DD ${af.Rule().dd:,.0f}, frozen floor ${af.Rule().frozen_floor:,.0f}, "
        f"seat cost ${af.Rule().cost:,.0f}, MAE-first, ratchet $200/$400, payout split "
        f"{cfg_template.split:.0%}, and ${cfg_template.replacement_reserve:,.0f} replacement "
        "reserve in addition to K initial seat fees. Initial capital is therefore "
        "`reserve + K x seat cost`; all initial and replacement seat costs are charged. "
        "At most one replacement is bought per recorded exit-timestamp event.",
        "",
        f"The selection distribution uses {episode_count} quarterly-started, overlapping "
        "two-year episodes ending by 2023-01-01; `Test cashout` is then evaluated once "
        "on 2023+. These selection "
        "episodes are strongly overlapping and contain roughly one independent regime, "
        "so their p10 is descriptive, not a calibrated probability.",
        "",
        "| R | Defensible pilot boundary | Selection cashout p10 | Cashout med | "
        "Test fill | Test cashout | Test gain vs round-robin |",
        "|---:|:---|---:|---:|---:|---:|---:|",
    ]
    for copies in R_GRID:
        k = 5 * copies
        group = summary[(summary["copies"] == copies) & (summary["k"] == k)]
        row = group[group["policy"] == "max_headroom"].iloc[0]
        baseline = group[group["policy"] == "round_robin"].iloc[0]
        lines.append(
            f"| {copies} | K={k}, max_headroom | "
            f"{_money(row['cashout_p10'])} | {_money(row['cashout_median'])} | "
            f"{_pct(row['test_fill_rate'])} | {_money(row['test_cashout'])} | "
            f"{_money(row['test_cashout'] - baseline['test_cashout'])} |"
        )
    exact = summary[summary["k"] >= 5 * summary["copies"]]
    test_pivot = exact.pivot_table(
        index=["k", "copies"], columns="policy",
        values="test_cashout", aggfunc="first",
    )
    test_wins = int((test_pivot["max_headroom"] >
                     test_pivot["round_robin"]).sum())
    exact_pairs = len(test_pivot)
    selection_rows = economics[
        (economics["evaluation_kind"] == "overlapping_2y_episode") &
        (pd.to_datetime(economics["end"]) <= TEST_START) &
        (economics["k"] >= 5 * economics["copies"]) &
        economics["policy"].isin(("max_headroom", "round_robin"))
    ]
    selection_pivot = selection_rows.pivot_table(
        index=["k", "copies", "evaluation"], columns="policy",
        values="cashout_wealth", aggfunc="first",
    )
    paired_medians = (
        selection_pivot["max_headroom"] - selection_pivot["round_robin"]
    ).groupby(level=["k", "copies"]).median()
    selection_wins = int((paired_medians > 0).sum())
    test_rows = economics[
        (economics["evaluation"] == "test_2023_plus") &
        (economics["k"] >= 5 * economics["copies"]) &
        economics["policy"].isin(("max_headroom", "round_robin"))
    ]
    exposure = test_rows.pivot_table(
        index=["k", "copies"], columns="policy",
        values=["fill_rate", "contract_hours", "initial_stop_risk",
                "delivered_raw_net"], aggfunc="first",
    )
    exposure_equal = all(
        np.allclose(exposure[metric]["max_headroom"],
                    exposure[metric]["round_robin"], rtol=0.0, atol=1e-8)
        for metric in ("fill_rate", "contract_hours", "initial_stop_risk",
                       "delivered_raw_net")
    )
    lines += [
        "",
        f"At exact-capacity K/R pairs, max-headroom beat round-robin on pre-2023 episode "
        f"median in {selection_wins}/{exact_pairs} comparisons and on 2023+ cashout in "
        f"{test_wins}/{exact_pairs}. In the holdout their fill, contract-hours, nominal "
        f"stop risk, and delivered raw P&L were "
        f"{'identical' if exposure_equal else 'not identical; inspect the CSV'}; the cash "
        "difference therefore came from allocating outcomes among nonlinear DD/payout "
        "paths, not from selecting signals.",
        "",
        "The pre-2023 fitted K above 5R was not stable in the holdout. The table therefore "
        "uses the mechanical K=5R boundary as the pilot default instead of calling an "
        "extra-seat count optimal. Cheaper near-full rows remain in the CSV but are not "
        "same-signal comparisons: congestion changed raw P&L. This is still only one "
        "small pre-2023 selection regime followed by one holdout; require a new untouched "
        "period before choosing a live rule.",
        "",
        "## Interpretation rules",
        "",
        "- Compare policies only within the same R, and confirm delivered fill, stop-risk "
        "  dollars, contract-hours, and raw net before attributing a cash difference to "
        "  routing.",
        "- K above 5R cannot add raw exposure when all accounts are alive. It buys spare "
        "  DD containers; any benefit must exceed its seat cost and slower per-seat path "
        "  to the Safety Net.",
        "- Routing removes the blocking objection to fixed schedules without disabling an "
        "  hour. It does not establish that recovered signals add alpha.",
        "- The 23 hourly exports already enforce one-position blocking inside each window. "
        "  Missing within-window signals are unknowable from these files.",
        "- Payout calendars, eligibility days/caps, activation delays, resets, and the real "
        "  account-tier price/DD menu are still absent. Values are conditional on the "
        "  simplified project rules.",
        "- A death cluster groups positions by their recorded exit timestamp. MT5 exports "
        "  do not reveal the exact intratrade time of an MAE floor breach, so this is an "
        "  observable stress proxy, not proof that every liquidation was simultaneous.",
        "",
    ]
    return "\n".join(lines)


def run(sweep_root: Path, stats_root: Path, output_dir: Path,
        split: float = 0.90, replacement_reserve: float = 1_000.0,
        horizon_years: float = 2.0) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    offers = load_offers(sweep_root, stats_root)
    headline, episodes = periods_for_sample(offers, horizon_years)
    selection_episodes = [period for period in episodes if period.end <= TEST_START]
    capacity_rows = [
        capacity_measure(offers, period, k, copies)
        for period in headline for k in CAPACITY_K_GRID for copies in CAPACITY_R_GRID
    ]
    capacity = pd.DataFrame(capacity_rows)

    rule = af.Rule()
    harvest = af.Harvest("ratchet", chunk=200.0, step=400.0)
    economics_rows = []
    headline_total = len(headline) * sum(
        len(HOLDOUT_K_BY_R[copies]) for copies in R_GRID
    ) * len(POLICIES)
    episode_cases = sum(len(EPISODE_K_BY_R[copies]) for copies in R_GRID) \
        * len(EPISODE_POLICIES)
    total = headline_total + len(selection_episodes) * episode_cases
    done = 0
    for period in headline + selection_episodes:
        prepared = prepare_route_tape(offers, period)
        for copies in R_GRID:
            ks = (HOLDOUT_K_BY_R[copies] if period.kind == "holdout" else
                  EPISODE_K_BY_R[copies])
            policies = (POLICIES if period.kind == "holdout" else
                        EPISODE_POLICIES)
            for k in ks:
                for policy in policies:
                    cfg = RouteCfg(
                        k=k, copies=copies, policy=policy,
                        replacement_reserve=replacement_reserve, split=split,
                    )
                    economics_rows.append(
                        simulate_route(
                            offers, period, cfg, harvest, rule, prepared=prepared,
                        )
                    )
                    done += 1
        print(f"Scored {period.label} ({done:,}/{total:,} simulations)", flush=True)
    economics = pd.DataFrame(economics_rows)
    summary = summarize_episodes(economics)

    output_dir.mkdir(parents=True, exist_ok=True)
    capacity_path = output_dir / "signal_routing_capacity.csv"
    economics_path = output_dir / "signal_routing_economics.csv"
    summary_path = output_dir / "signal_routing_summary.csv"
    markdown_path = output_dir / "signal_routing.md"
    capacity.to_csv(capacity_path, index=False, float_format="%.8f")
    economics.to_csv(economics_path, index=False, float_format="%.8f")
    summary.to_csv(summary_path, index=False, float_format="%.8f")
    template = RouteCfg(1, 1, POLICIES[0], replacement_reserve, split)
    markdown_path.write_text(
        markdown_report(
            offers, capacity, economics, summary, template,
            len(selection_episodes),
        ),
        encoding="utf-8",
    )
    print(f"Wrote {capacity_path}")
    print(f"Wrote {economics_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {markdown_path}")
    return capacity, economics, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-root", type=Path,
                        default=ROOT / "1_sweeps" / "RR")
    parser.add_argument("--stats-root", type=Path,
                        default=ROOT / "1_sweeps" / "RR_stats")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    parser.add_argument("--split", type=float, default=0.90)
    parser.add_argument("--replacement-reserve", type=float, default=1_000.0)
    parser.add_argument("--horizon-years", type=float, default=2.0)
    args = parser.parse_args()
    run(
        args.sweep_root, args.stats_root, args.output_dir,
        split=args.split, replacement_reserve=args.replacement_reserve,
        horizon_years=args.horizon_years,
    )


if __name__ == "__main__":
    main()
