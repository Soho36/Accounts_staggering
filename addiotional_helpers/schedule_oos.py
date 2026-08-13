"""Raw out-of-sample diagnostic for assigning clock windows to separate seats.

This deliberately stops before prop-account deaths, payouts, withdrawals, seat
costs, or replacement logic.  It asks one narrower question: with the same
number of seats, how do identical all-window exposure and a fixed disjoint
window schedule differ at RR 1.00?

The training period is 2020-2022 and the test period begins on 2023-01-01.
Each period starts flat and replays only entries offered inside that period.  A
training trade which has not exited by the test boundary is purged so the
top-window selection cannot see a post-boundary outcome.
"""

from __future__ import annotations

import argparse
import itertools
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_farming as af  # noqa: E402  (ROOT must be importable first)


RR = 1.00
WINDOWS = tuple(f"{hour}-{hour + 1}" for hour in range(1, 24))
KS = (1, 2, 3, 4, 6, 23)
TEST_START = pd.Timestamp("2023-01-01")


@dataclass(frozen=True)
class Phase:
    name: str
    start_date: str
    end_date: str | None
    # Training selection must not use a trade outcome learned in the test era.
    exit_before: pd.Timestamp | None = None


PHASES = (
    Phase("train_2020_2022", "2020-01-01", "2022-12-31", TEST_START),
    Phase("test_2023_plus", "2023-01-01", None, None),
)
ROLLING_TEST_YEARS = tuple(range(2021, 2027))


def _strict_paths(sweep_root: Path, stats_root: Path) -> dict[str, Path]:
    """Require the exact, complete 23-window RR 1.00 universe."""
    if not sweep_root.is_dir():
        raise FileNotFoundError(f"Sweep root does not exist: {sweep_root}")
    if not stats_root.is_dir():
        raise FileNotFoundError(f"Stats root does not exist: {stats_root}")

    found = {p.name for p in sweep_root.iterdir()
             if p.is_dir() and p.name in set(WINDOWS)}
    expected = set(WINDOWS)
    missing_dirs = sorted(expected - found, key=_window_start)
    if missing_dirs:
        detail = []
        if missing_dirs:
            detail.append("missing directories: " + ", ".join(missing_dirs))
        raise RuntimeError("Expected all 23 hourly windows; " + "; ".join(detail))

    paths: dict[str, Path] = {}
    incomplete = []
    for window in WINDOWS:
        path = sweep_root / window / f"{window}_{RR:.2f}.csv"
        if not path.is_file():
            incomplete.append(f"{window}: missing sweep export")
            continue
        state = af.pass_is_blown(stats_root, window, RR)
        if state is True:
            incomplete.append(f"{window}: tester-truncated pass")
        elif state is None:
            incomplete.append(f"{window}: missing/unreadable stats")
        else:
            paths[window] = path
    if incomplete:
        raise RuntimeError(
            "RR 1.00 does not have a complete, validated 23-window universe: "
            + "; ".join(incomplete)
        )
    return paths


def _window_start(window: str) -> int:
    try:
        return int(window.split("-", 1)[0])
    except (TypeError, ValueError):
        return 10_000


def _load_parts(paths: dict[str, Path]) -> dict[str, dict]:
    return {window: af._part(af.read_trade_file(paths[window])) for window in WINDOWS}


def _stream_for_group(parts: dict[str, dict], windows: list[str], phase: Phase) -> dict:
    """Replay one seat's assigned windows from a fresh, flat phase boundary."""
    offered = [
        af._filter_part_dates(parts[window], phase.start_date, phase.end_date)
        for window in windows
    ]
    keep = af.replay(offered)
    rows = []
    result_end = (np.datetime64(pd.Timestamp(phase.end_date) + pd.Timedelta(days=1))
                  if phase.end_date else None)
    for window, part, mask in zip(windows, offered, keep):
        for index in np.flatnonzero(mask):
            # Purge the one possible boundary-crossing training position.  Its
            # result was not knowable when the 2023 test allocation was fixed.
            if phase.exit_before is not None and \
                    part["ex"][index] >= np.datetime64(phase.exit_before):
                continue
            if result_end is not None and part["ex"][index] >= result_end:
                continue
            rows.append((part["ex"][index], part["en"][index],
                         part["net"][index], part["mae"][index],
                         part["mfe"][index], window))
    rows.sort(key=lambda row: (row[0], row[1], _window_start(row[5])))
    if not rows:
        raise RuntimeError(
            f"No completed trades for {phase.name}, group {', '.join(windows)}"
        )
    return {
        "ex": np.array([row[0] for row in rows], dtype="datetime64[s]"),
        "en": np.array([row[1] for row in rows], dtype="datetime64[s]"),
        "net": np.array([row[2] for row in rows], dtype=np.float64),
        "mae": np.array([row[3] for row in rows], dtype=np.float64),
        "mfe": np.array([row[4] for row in rows], dtype=np.float64),
        "windows": tuple(windows),
    }


def _round_robin_groups(k: int) -> list[list[str]]:
    groups = [[] for _ in range(k)]
    for index, window in enumerate(WINDOWS):
        groups[index % k].append(window)
    return groups


def _flow_drawdown(values) -> float:
    equity = peak = max_dd = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _aggregate_flows(templates: list[dict], daily: bool) -> list[float]:
    flows: dict[np.datetime64, float] = {}
    for template in templates:
        for exit_time, net in zip(template["ex"], template["net"]):
            key = exit_time.astype("datetime64[D]") if daily else exit_time
            flows[key] = flows.get(key, 0.0) + float(net)
    return [flows[key] for key in sorted(flows)]


def _max_concurrent_contracts(templates: list[dict]) -> int:
    events = []
    for template in templates:
        events.extend((entry, 1) for entry in template["en"])
        events.extend((exit_time, -1) for exit_time in template["ex"])
    # A position ending at T frees capacity for an entry at T.
    events.sort(key=lambda event: (event[0], event[1]))
    live = maximum = 0
    for _, change in events:
        live += change
        maximum = max(maximum, live)
    return maximum


def _assignment_label(groups: list[list[str]]) -> str:
    return "; ".join(
        f"seat{index}=" + "|".join(group)
        for index, group in enumerate(groups, start=1)
    )


def _measure(phase: Phase, k: int, variant: str, templates: list[dict],
             assignment: str) -> dict:
    contract_trades = sum(len(template["net"]) for template in templates)
    contract_hours = sum(
        float(np.sum((template["ex"] - template["en"]) / np.timedelta64(1, "h")))
        for template in templates
    )
    return {
        "rr": RR,
        "commission_roundturn": af.COMMISSION_ROUNDTURN,
        "phase": phase.name,
        "phase_start": phase.start_date,
        "phase_end": phase.end_date or "sample_end",
        "k_seats": k,
        "variant": variant,
        "assignment": assignment,
        "contract_trades": contract_trades,
        "contract_hours": contract_hours,
        "max_concurrent_contracts": _max_concurrent_contracts(templates),
        "raw_net_after_commission": sum(
            float(np.sum(template["net"])) for template in templates
        ),
        "worst_template_equity_dd": max(
            af.dd_equity(template["net"], template["mae"], template["mfe"])
            for template in templates
        ),
        "aggregate_closed_dd": _flow_drawdown(_aggregate_flows(templates, daily=False)),
        "aggregate_daily_dd": _flow_drawdown(_aggregate_flows(templates, daily=True)),
    }


def _attach_comparisons(rows: list[dict]) -> None:
    by_key = {(row["phase"], row["k_seats"], row["variant"]): row for row in rows}
    for row in rows:
        baseline = by_key[(row["phase"], row["k_seats"], "identical_all_window")]
        row["same_k_all_window_contract_trades"] = baseline["contract_trades"]
        row["contract_trade_ratio_vs_same_k_all_window"] = (
            row["contract_trades"] / baseline["contract_trades"]
        )
        row["same_k_all_window_contract_hours"] = baseline["contract_hours"]
        row["contract_hour_ratio_vs_same_k_all_window"] = (
            row["contract_hours"] / baseline["contract_hours"]
        )

        one_per_window = by_key.get((row["phase"], 23, "round_robin_schedule"))
        if row["k_seats"] == 23 and one_per_window is not None:
            row["one_per_window_contract_trades"] = one_per_window["contract_trades"]
            row["contract_trade_ratio_vs_one_per_window"] = (
                row["contract_trades"] / one_per_window["contract_trades"]
            )
            row["raw_net_difference_vs_one_per_window"] = (
                row["raw_net_after_commission"]
                - one_per_window["raw_net_after_commission"]
            )
        else:
            row["one_per_window_contract_trades"] = math.nan
            row["contract_trade_ratio_vs_one_per_window"] = math.nan
            row["raw_net_difference_vs_one_per_window"] = math.nan


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _exact_sign_flip_pvalue(differences: list[float]) -> float:
    """Two-sided paired randomization p-value over the annual differences."""
    observed = abs(sum(differences))
    if not differences:
        return math.nan
    scale = max(1.0, observed, *(abs(value) for value in differences))
    tolerance = np.finfo(float).eps * scale * len(differences) * 8.0
    extreme = 0
    total = 2 ** len(differences)
    for signs in itertools.product((-1.0, 1.0), repeat=len(differences)):
        permuted = abs(sum(sign * value for sign, value in zip(signs, differences)))
        extreme += permuted + tolerance >= observed
    return extreme / total


def _rolling_year_rows(parts: dict[str, dict]) -> list[dict]:
    """Rank on expanding completed history, then score the untouched next year."""
    rows = []
    for year in ROLLING_TEST_YEARS:
        boundary = pd.Timestamp(f"{year}-01-01")
        train_phase = Phase(
            f"rolling_train_2020_{year - 1}", "2020-01-01",
            f"{year - 1}-12-31", boundary,
        )
        test_phase = Phase(
            f"rolling_test_{year}", f"{year}-01-01", f"{year}-12-31", None,
        )
        scores = {
            window: float(np.sum(_stream_for_group(
                parts, [window], train_phase
            )["net"]))
            for window in WINDOWS
        }
        ranked = sorted(WINDOWS, key=lambda item: (-scores[item], _window_start(item)))
        selected, duplicates = ranked[:20], ranked[:3]
        dropped = [window for window in WINDOWS if window not in selected]

        test_streams = {
            window: _stream_for_group(parts, [window], test_phase)
            for window in WINDOWS
        }
        baseline_net = sum(
            float(np.sum(test_streams[window]["net"])) for window in WINDOWS
        )
        candidate_windows = selected + duplicates
        candidate_net = sum(
            float(np.sum(test_streams[window]["net"]))
            for window in candidate_windows
        )
        rows.append({
            "test_year": year,
            "train_start": "2020-01-01",
            "train_end": f"{year - 1}-12-31",
            "train_years": year - 2020,
            "short_one_year_train": year == 2021,
            "selected_top20": ",".join(selected),
            "dropped_windows": ",".join(dropped),
            "duplicated_top3": ",".join(duplicates),
            "one_per_window_contract_trades": sum(
                len(test_streams[window]["net"]) for window in WINDOWS
            ),
            "candidate_contract_trades": sum(
                len(test_streams[window]["net"]) for window in candidate_windows
            ),
            "one_per_window_raw_net": baseline_net,
            "candidate_raw_net": candidate_net,
            "candidate_minus_one_per_window": candidate_net - baseline_net,
            "candidate_win": candidate_net > baseline_net,
        })

    differences = [row["candidate_minus_one_per_window"] for row in rows]
    wins = sum(row["candidate_win"] for row in rows)
    cumulative = sum(differences)
    pvalue = _exact_sign_flip_pvalue(differences)
    for row in rows:
        row["summary_annual_wins"] = wins
        row["summary_annual_tests"] = len(rows)
        row["summary_cumulative_difference"] = cumulative
        row["summary_exact_sign_flip_pvalue"] = pvalue
    return rows


def _markdown(rows: list[dict], train_scores: dict[str, float],
              selected: list[str], duplicates: list[str],
              rolling_rows: list[dict]) -> str:
    lines = [
        "# RR 1.00 raw schedule OOS diagnostic",
        "",
        "> **Scope:** raw trade-stream diagnostic only. It does **not** model prop-account "
        "deaths, payouts, withdrawals, seat costs, replacements, or farming economics.",
        "",
        "All comparisons use the exact validated 23-window universe and $1.05 round-turn "
        "commission. Train is 2020-2022; test begins 2023-01-01. Both start flat, and "
        "the allocation is fixed from train only. A training trade crossing the test "
        "boundary is purged.",
        "",
        "`round_robin_schedule` assigns chronological windows cyclically across K seats "
        "and replays the one-position rule inside each seat's group. "
        "`identical_all_window` repeats the same all-window replay K times.",
        "",
    ]

    for phase in PHASES:
        title = "Train (2020-2022)" if phase.name.startswith("train") else "Test (2023+)"
        lines.extend([
            f"## {title}",
            "",
            "| K | variant | contract-trades | vs same-K all-window | contract-hours | "
            "hours vs same-K all-window | raw net | worst template equity DD | "
            "aggregate closed DD | aggregate daily DD | max concurrent |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        phase_rows = [row for row in rows if row["phase"] == phase.name]
        for row in phase_rows:
            lines.append(
                f"| {row['k_seats']} | {row['variant']} | "
                f"{row['contract_trades']:,} | "
                f"{row['contract_trade_ratio_vs_same_k_all_window']:.1%} | "
                f"{row['contract_hours']:,.0f} | "
                f"{row['contract_hour_ratio_vs_same_k_all_window']:.1%} | "
                f"{_money(row['raw_net_after_commission'])} | "
                f"{_money(row['worst_template_equity_dd'])} | "
                f"{_money(row['aggregate_closed_dd'])} | "
                f"{_money(row['aggregate_daily_dd'])} | "
                f"{row['max_concurrent_contracts']} |"
            )
        lines.append("")

    dropped = [window for window in WINDOWS if window not in selected]
    lines.extend([
        "## Locked K=23 candidate",
        "",
        "The candidate keeps the 20 best standalone windows by **train-only** raw net, "
        "then assigns one additional seat to each of the top three. Duplicate seats are "
        "counted as duplicate contract exposure; no test result enters the ranking.",
        "",
        "- Selected 20: " + ", ".join(selected),
        "- Dropped 3: " + ", ".join(dropped),
        "- Duplicated train winners: " + ", ".join(duplicates),
        "",
        "| phase | one/window raw net | candidate raw net | candidate minus baseline | "
        "candidate contract-trades vs one/window |",
        "|---|---:|---:|---:|---:|",
    ])
    for phase in PHASES:
        one_per_window = next(
            row for row in rows
            if row["phase"] == phase.name and row["k_seats"] == 23
            and row["variant"] == "round_robin_schedule"
        )
        candidate = next(
            row for row in rows
            if row["phase"] == phase.name
            and row["variant"] == "top20_plus_3_train_duplicates"
        )
        lines.append(
            f"| {phase.name} | {_money(one_per_window['raw_net_after_commission'])} | "
            f"{_money(candidate['raw_net_after_commission'])} | "
            f"{_money(candidate['raw_net_after_commission'] - one_per_window['raw_net_after_commission'])} | "
            f"{candidate['contract_trades'] / one_per_window['contract_trades']:.1%} |"
        )
    lines.extend([
        "",
        "| window | train raw net |",
        "|---|---:|",
    ])
    for window in sorted(WINDOWS, key=lambda item: (-train_scores[item], _window_start(item))):
        lines.append(f"| {window} | {_money(train_scores[window])} |")

    lines.extend([
        "",
        "## Rolling-year robustness of the K=23 ranking rule",
        "",
        "For each year, the same top-20 plus duplicated-top-3 rule is re-ranked using "
        "only completed standalone trades from 2020 through the preceding December 31, "
        "then locked for the next calendar year. The 2021 row has only one training "
        "year and is explicitly a short-history test.",
        "",
        "| test year | prior train | dropped | duplicated | one/window trades | "
        "candidate trades | one/window net | candidate net | difference | win |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|:---:|",
    ])
    for row in rolling_rows:
        label = f"{row['train_start']} to {row['train_end']}"
        if row["short_one_year_train"]:
            label += " (one-year train)"
        lines.append(
            f"| {row['test_year']} | {label} | "
            f"{row['dropped_windows']} | {row['duplicated_top3']} | "
            f"{row['one_per_window_contract_trades']:,} | "
            f"{row['candidate_contract_trades']:,} | "
            f"{_money(row['one_per_window_raw_net'])} | "
            f"{_money(row['candidate_raw_net'])} | "
            f"{_money(row['candidate_minus_one_per_window'])} | "
            f"{'yes' if row['candidate_win'] else 'no'} |"
        )
    rolling_wins = sum(row["candidate_win"] for row in rolling_rows)
    differences = [row["candidate_minus_one_per_window"] for row in rolling_rows]
    lines.extend([
        "",
        f"- Annual wins: **{rolling_wins}/{len(rolling_rows)}**",
        f"- Cumulative candidate minus one/window: **{_money(sum(differences))}**",
        f"- Exact two-sided paired sign-flip p-value: "
        f"**{_exact_sign_flip_pvalue(differences):.4f}**",
        "",
        "The six annual observations are few and adjacent years are not guaranteed "
        "independent; the p-value is a diagnostic, not proof of a persistent edge.",
        "",
        "## Fixed round-robin assignments",
        "",
    ])
    for k in KS:
        if k == 23:
            lines.append("- K=23: one chronological hourly window per seat.")
        else:
            lines.append(f"- K={k}: `{_assignment_label(_round_robin_groups(k))}`")
    lines.append("")
    return "\n".join(lines)


def run(sweep_root: Path, stats_root: Path, output_dir: Path) -> pd.DataFrame:
    paths = _strict_paths(sweep_root, stats_root)
    parts = _load_parts(paths)

    # Rank only completed, fresh-flat training trades from each standalone window.
    train_phase = PHASES[0]
    train_window_streams = {
        window: _stream_for_group(parts, [window], train_phase) for window in WINDOWS
    }
    train_scores = {
        window: float(np.sum(stream["net"]))
        for window, stream in train_window_streams.items()
    }
    ranked = sorted(WINDOWS, key=lambda item: (-train_scores[item], _window_start(item)))
    selected, duplicates = ranked[:20], ranked[:3]
    rolling_rows = _rolling_year_rows(parts)

    rows: list[dict] = []
    for phase in PHASES:
        group_cache: dict[tuple[str, ...], dict] = {}

        def stream(group: list[str]) -> dict:
            key = tuple(group)
            if key not in group_cache:
                group_cache[key] = _stream_for_group(parts, group, phase)
            return group_cache[key]

        all_window = stream(list(WINDOWS))
        for k in KS:
            identical = [all_window] * k
            rows.append(_measure(
                phase, k, "identical_all_window", identical,
                f"same all-window stream x{k}",
            ))

            groups = _round_robin_groups(k)
            scheduled = [stream(group) for group in groups]
            rows.append(_measure(
                phase, k, "round_robin_schedule", scheduled,
                _assignment_label(groups),
            ))

        candidate_windows = selected + duplicates
        candidate = [stream([window]) for window in candidate_windows]
        candidate_groups = [[window] for window in candidate_windows]
        rows.append(_measure(
            phase, 23, "top20_plus_3_train_duplicates", candidate,
            _assignment_label(candidate_groups),
        ))

    _attach_comparisons(rows)
    frame = pd.DataFrame(rows)
    order = {name: index for index, name in enumerate(
        ("identical_all_window", "round_robin_schedule",
         "top20_plus_3_train_duplicates")
    )}
    phase_order = {phase.name: index for index, phase in enumerate(PHASES)}
    frame["_phase_order"] = frame["phase"].map(phase_order)
    frame = frame.sort_values(
        ["_phase_order", "k_seats", "variant"],
        key=lambda column: column.map(order) if column.name == "variant" else column,
    ).drop(columns="_phase_order").reset_index(drop=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "schedule_oos.csv"
    markdown_path = output_dir / "schedule_oos.md"
    rolling_csv_path = output_dir / "schedule_oos_rolling_years.csv"
    frame.to_csv(csv_path, index=False, float_format="%.6f")
    pd.DataFrame(rolling_rows).to_csv(
        rolling_csv_path, index=False, float_format="%.6f"
    )
    markdown_path.write_text(
        _markdown(rows, train_scores, selected, duplicates, rolling_rows),
        encoding="utf-8",
    )
    print(f"Wrote {csv_path}")
    print(f"Wrote {rolling_csv_path}")
    print(f"Wrote {markdown_path}")
    print("Raw schedule diagnostic only; payout/death economics are intentionally absent.")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-root", type=Path,
                        default=ROOT / "1_sweeps" / "RR")
    parser.add_argument("--stats-root", type=Path,
                        default=ROOT / "1_sweeps" / "RR_stats")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    run(args.sweep_root, args.stats_root, args.output_dir)


if __name__ == "__main__":
    main()
