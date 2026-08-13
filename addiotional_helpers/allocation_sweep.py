"""Focused Mode-2 allocation diagnostics for the current account simulator.

This helper deliberately does not search for an optimum.  It replays the small
set of seed, cadence, trigger, withdrawal, drawdown, seat-cap and payout-split
points discussed in the project review, and writes every result from the same
scored rows to:

    results/allocation_sweep.csv
    results/allocation_sweep.md

All reported books use the same 18 quarterly-started, overlapping two-year
windows.  They are in-sample diagnostics, not 18 independent observations.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"

import sys

sys.path.insert(0, str(ROOT))
import account_farming as af  # noqa: E402


RR = 1.00
HORIZON_YEARS = 2.0
EXPECTED_WINDOWS = 23
EXPECTED_BOOK_WINDOWS = 18
DEFAULTS = {
    "seed": 3000.0,
    "interval_days": 30,
    "start_policy": "time",
    "profit_trigger": 1000.0,
    "dd_trigger": 400.0,
    "min_days": 1,
    "dd": 2500.0,
    "frozen_floor": 100.0,
    "cost": 200.0,
    "withdraw_chunk": 200.0,
    "withdraw_step": 400.0,
    "seat_cap": 20,
    "split": 1.0,
}


def load_stream() -> dict:
    """Load a strict, complete RR=1 stream; never silently drop a window."""
    args = argparse.Namespace(
        input_csv=None,
        sweep_root=ROOT / "1_sweeps" / "RR",
        stats_root=ROOT / "1_sweeps" / "RR_stats",
        rr=RR,
        windows="all",
        start_date=None,
        end_date=None,
        allow_incomplete=False,
    )
    stream = af.build_stream(args)
    if not stream.get("complete", False):
        raise RuntimeError("RR=1 stream is incomplete; refusing allocation comparison.")
    if len(stream["windows"]) != EXPECTED_WINDOWS:
        raise RuntimeError(
            f"Expected {EXPECTED_WINDOWS} complete RR=1 windows, got "
            f"{len(stream['windows'])}."
        )
    return stream


def book_windows(stream: dict) -> list[tuple[pd.Timestamp, dict]]:
    """The report's quarterly-started, overlapping two-year horizons."""
    windows = af.robustness_periods(stream, HORIZON_YEARS, min_trades=200)
    if len(windows) != EXPECTED_BOOK_WINDOWS:
        raise RuntimeError(
            f"Expected {EXPECTED_BOOK_WINDOWS} two-year book windows, got "
            f"{len(windows)}."
        )
    return windows


def case(sweep: str, label: str, **overrides) -> dict:
    row = {"sweep": sweep, "label": label, **DEFAULTS, **overrides}
    return row


def cases() -> list[dict]:
    out: list[dict] = []

    for seed in (1200, 2000, 3000, 4000):
        for interval in (14, 21, 30, 45, 60, 90):
            out.append(case(
                "cadence_seed", f"seed ${seed:,}, every {interval}d",
                seed=float(seed), interval_days=interval,
            ))

    out.extend([
        case("start_trigger", "calendar 30d"),
        case("start_trigger", "calendar 45d", interval_days=45),
        case("start_trigger", "calendar 60d", interval_days=60),
    ])
    for threshold in (400, 800, 1000, 1500, 2500):
        out.append(case(
            "start_trigger", f"profit trigger ${threshold:,}",
            start_policy="profit", profit_trigger=float(threshold),
        ))
    for threshold in (200, 400, 800, 1200, 2000):
        out.append(case(
            "start_trigger", f"drawdown trigger ${threshold:,}",
            start_policy="dd", dd_trigger=float(threshold),
        ))
    for spacing in (7, 14, 30, 45, 60):
        out.append(case(
            "start_trigger", f"any trigger, min {spacing}d",
            start_policy="any", min_days=spacing,
        ))

    withdrawal_rules = (
        (200, 400), (200, 600), (200, 1000), (200, 2000),
        (500, 2500), (1000, 2000), (1000, 5000), (2000, 4000),
    )
    for seed in (1200, 3000):
        for interval in (30, 45, 60):
            for chunk, step in withdrawal_rules:
                out.append(case(
                    "withdrawal", f"${chunk:,}/${step:,}; seed ${seed:,}; {interval}d",
                    seed=float(seed), interval_days=interval,
                    withdraw_chunk=float(chunk), withdraw_step=float(step),
                ))

    # Same $200 seat price at every DD is intentionally unrealistic.  This is a
    # mechanism diagnostic only, not a comparison of real prop-account tiers.
    for seed in (1200, 3000):
        for dd in (1000, 1500, 2000, 2500, 3000, 4000, 5000, 6500):
            out.append(case(
                "dd_same_price", f"DD ${dd:,}; seed ${seed:,}",
                seed=float(seed), dd=float(dd),
            ))

    for interval in (30, 45):
        for cap in (1, 2, 3, 5, 8, 10, 15, 20, 25):
            out.append(case(
                "seat_cap", f"cap {cap}; every {interval}d",
                interval_days=interval, seat_cap=cap,
            ))

    for seed in (2000, 3000):
        for interval in (30, 45, 60):
            for split in (0.8, 0.9, 1.0):
                out.append(case(
                    "payout_split", f"{split:.0%}; seed ${seed:,}; {interval}d",
                    seed=float(seed), interval_days=interval, split=split,
                ))
    return out


def percentile(values: np.ndarray, q: int) -> float:
    return float(np.percentile(values, q))


def score(one: dict, stream: dict,
          windows: list[tuple[pd.Timestamp, dict]]) -> dict:
    rule = af.Rule(
        dd=one["dd"], frozen_floor=one["frozen_floor"],
        cost=one["cost"], mfe_first=False,
    )
    harvest = af.Harvest(
        "ratchet", chunk=one["withdraw_chunk"], step=one["withdraw_step"],
    )
    cfg = af.BookCfg(
        seats=one["seat_cap"], seed=one["seed"],
        interval_days=one["interval_days"], policy=one["start_policy"],
        profit_trigger=one["profit_trigger"], dd_trigger=one["dd_trigger"],
        min_days=one["min_days"], max_per_event=1,
        funding="cash", split=one["split"],
    )
    runs = [
        af.run_book(period["ex"], period["net"], period["mae"],
                    period["mfe"], harvest, rule, cfg)
        for _, period in windows
    ]

    def values(name: str) -> np.ndarray:
        return np.asarray([run[name] for run in runs], dtype=float)

    cash = values("cash")
    withdrawable = values("withdrawable")
    realized = values("realized_wealth")
    cashout = values("cashout_wealth")
    mark = values("wealth")
    received = values("withdrawn")
    bought = values("bought")
    live = values("live")

    return {
        **one,
        "n_book_windows": len(windows),
        "horizon_years": HORIZON_YEARS,
        "book_start_first": windows[0][0].date().isoformat(),
        "book_start_last": windows[-1][0].date().isoformat(),
        "overlapping_windows": True,
        "in_sample": True,
        "cash_p10": percentile(cash, 10),
        "cash_median": float(np.median(cash)),
        "withdrawable_p10": percentile(withdrawable, 10),
        "withdrawable_median": float(np.median(withdrawable)),
        "payout_received_median": float(np.median(received)),
        "realized_p10": percentile(realized, 10),
        "realized_median": float(np.median(realized)),
        "cashout_p10": percentile(cashout, 10),
        "cashout_median": float(np.median(cashout)),
        "cashout_below_water_rate": float(np.mean(cashout <= 0)),
        "mark_p10": percentile(mark, 10),
        "mark_median": float(np.median(mark)),
        "mark_below_water_rate": float(np.mean(mark <= 0)),
        "ruin_rate": float(np.mean([run["ruined"] for run in runs])),
        "wipeout_rate": float(np.mean([run["wipeouts"] > 0 for run in runs])),
        "shock_5plus_rate": float(np.mean(
            [run["worst_cluster"] >= 5 for run in runs]
        )),
        "full_collapse_5plus_rate": float(np.mean(
            [run["worst_wipe"] >= 5 for run in runs]
        )),
        "worst_cluster": int(max(run["worst_cluster"] for run in runs)),
        "worst_wipe": int(max(run["worst_wipe"] for run in runs)),
        "seats_bought_median": float(np.median(bought)),
        "live_seats_median": float(np.median(live)),
    }


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def rate(value: float) -> str:
    return f"{value:.0%}"


def markdown_table(frame: pd.DataFrame, columns: list[tuple[str, str, str]]) -> str:
    lines = [
        "| " + " | ".join(label for _, label, _ in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for record in frame.to_dict("records"):
        cells = []
        for key, _, kind in columns:
            value = record[key]
            if kind == "money":
                cells.append(money(float(value)))
            elif kind == "rate":
                cells.append(rate(float(value)))
            elif kind == "int":
                cells.append(f"{float(value):,.0f}")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_markdown(scored: pd.DataFrame, stream: dict,
                   windows: list[tuple[pd.Timestamp, dict]], digest: str) -> str:
    cadence = scored[scored["sweep"] == "cadence_seed"]
    triggers = scored[scored["sweep"] == "start_trigger"]
    withdrawal = scored[
        (scored["sweep"] == "withdrawal")
        & (scored["seed"] == 3000)
        & (scored["interval_days"] == 30)
    ]
    dd = scored[
        (scored["sweep"] == "dd_same_price") & (scored["seed"] == 3000)
    ]
    caps = scored[scored["sweep"] == "seat_cap"]
    splits = scored[scored["sweep"] == "payout_split"]

    common = [
        ("label", "case", "text"),
        ("cashout_median", "cashout median", "money"),
        ("cashout_p10", "cashout p10", "money"),
        ("realized_median", "realized median", "money"),
        ("mark_median", "mark median", "money"),
        ("ruin_rate", "ruin", "rate"),
        ("shock_5plus_rate", "5+ shock", "rate"),
        ("worst_cluster", "max cluster", "int"),
        ("seats_bought_median", "bought median", "int"),
    ]
    compact = [
        ("label", "case", "text"),
        ("cashout_median", "cashout median", "money"),
        ("cashout_p10", "cashout p10", "money"),
        ("ruin_rate", "ruin", "rate"),
        ("shock_5plus_rate", "5+ shock", "rate"),
        ("worst_cluster", "max cluster", "int"),
    ]

    return f"""# Mode-2 allocation sweep

This is a reproducible diagnostic from `addiotional_helpers/allocation_sweep.py`.
It does not identify a statistically validated optimum.

## Fixed assumptions

- RR 1.00, all {len(stream['windows'])} complete hourly windows, one-position replay.
- MAE-first intratrade path and ${af.COMMISSION_ROUNDTURN:.2f} round-turn commission.
- {len(windows)} quarterly-started, two-year books from {windows[0][0].date()} to {windows[-1][0].date()}.
  Each book starts flat and independently replays window competition; a position crossing the
  horizon can block another entry, but its own post-horizon outcome is not scored.
- The books overlap heavily. They represent only about 3-4 independent two-year regimes.
- Mode 2 only, one seat per event, $200 seat cost, $2,500 DD, $100 frozen floor,
  $200 per $400 gain-ratchet, 20-seat cap and 100% payout split unless a row says otherwise.
- Every row is in-sample on the same 2020-2026 history. P10 is descriptive, not a calibrated 10% forecast.
- Simulator file SHA-256 at artifact write time: `{digest}`.

## How to read the money columns

- **Realized** = terminal cash pot minus initial seed. Cumulative payouts are not used because
  payouts spent on replacement seats cannot also be counted as cash still owned.
- **Cashout** = realized plus only the endpoint-withdrawable cushion above the Safety Net on
  frozen seats. This is the primary decision metric here.
- **Mark** additionally credits positive P&L in every live prop account, including seats that
  are not yet payout-eligible. It is an optimistic continuation value, not cash.

## Cadence x seed

{markdown_table(cadence, common)}

## Start triggers

All rows use a $3,000 seed. Profit/DD/any triggers are endogenous to the common market path;
high typical values can therefore be paid for by clustered tail loss.

{markdown_table(triggers, common)}

## Withdrawal rules: $3,000 seed, 30-day cadence

The complete CSV also contains these rules at a $1,200 seed and at 45/60-day cadences.

{markdown_table(withdrawal, common)}

## Drawdown: same-price diagnostic

Every DD below is assigned the same $200 seat price and the same payout mechanics. Real firms
do not price tiers this way, so this table diagnoses the model mechanism; it is not a tier recommendation.

{markdown_table(dd, common)}

## Seat cap

All rows use a $3,000 seed. The same MNQ signals drive every seat, so a higher cap is leverage,
not strategy diversification.

{markdown_table(caps, compact)}

## Payout split sensitivity

The 100% split used elsewhere is optimistic. At 80-90%, each gross withdrawal delivers less
cash to the replacement pot, which can reverse a small positive p10.

{markdown_table(splits, compact)}

## Observed diagnostic takeaways

- With a $1,200 seed, 90 days was the only tested cadence with zero observed ruin. Under the
  optimistic 100% payout split, 45 days was the balanced observed point at a $2,000-$3,000
  seed, while 30 days retained more typical upside but produced a 5+ seat same-trade shock.
  At an 80-90% split that small positive 45-day p10 disappears, so this is not yet a live rule.
- Profit and drawdown start triggers did not diversify entries. Aggressive triggers clustered
  purchases into the same market state and produced poor p10/shock combinations.
- At $3,000/30 days, $200 per $1,000 increased typical cashout versus $200 per $400 but starved
  the replacement pot and made p10 materially worse. The ratchet step remains an unvalidated
  free parameter.
- Under equal $200 pricing, $2,500 DD was the observed cashout sweet spot. Larger DD tiers looked
  much better only in mark-to-model value because more profit remained trapped below payout level.
- Cashout flattened around an 8-10 seat cap. Expanding toward 20 added little typical cashout
  and introduced 5+ seat shocks in the observed sample.

## Required before a real tier allocation

The repository has no firm menu mapping DD to seat/activation/reset cost, payout split,
eligibility threshold, minimum/maximum payout, frequency/consistency rules, trailing-floor
details or account cap. The simulator also applies one DD/cost rule to the whole book and cannot
yet score a mixed-tier portfolio. Obtain that menu, model it exactly, then validate candidate
policies chronologically or by block bootstrap. Do not optimize from these 18 overlapping rows.
"""


def main() -> None:
    stream = load_stream()
    windows = book_windows(stream)
    planned = cases()
    scored = []
    total = len(planned)
    for index, one in enumerate(planned, 1):
        print(f"[{index:>3}/{total}] {one['sweep']}: {one['label']}")
        scored.append(score(one, stream, windows))

    frame = pd.DataFrame(scored)
    frame.insert(0, "case_index", np.arange(1, len(frame) + 1))
    frame["dd_same_price_diagnostic"] = frame["sweep"].eq("dd_same_price")
    digest = hashlib.sha256((ROOT / "account_farming.py").read_bytes()).hexdigest()
    frame["simulator_sha256"] = digest

    RESULTS.mkdir(exist_ok=True)
    csv_path = RESULTS / "allocation_sweep.csv"
    md_path = RESULTS / "allocation_sweep.md"
    frame.to_csv(csv_path, index=False, float_format="%.6f")
    md_path.write_text(build_markdown(frame, stream, windows, digest), encoding="utf-8")

    print(f"Saved {len(frame)} cases to {csv_path}")
    print(f"Saved interpretation guide to {md_path}")


if __name__ == "__main__":
    main()
