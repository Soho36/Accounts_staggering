"""Doji frequency and a doji-only replay of the MT5 buy-stop EA.

Question: both routed strategies ignore a doji (close == open) because the MT5
EAs behind the study fire only on strict colours (RR c1 < o1, GG c1 > o1). How
many dojis are there, and what would trading only them have produced?

Method: M30 bars are rebuilt from the 1-minute Databento file the original MT5
symbol (MNQcontDATABENTOcurr6) was made from, and RR/GG_r_MFE_buy-stop-entry is
replayed minute by minute, one run per study window, exactly as the sweep ran:

  at each M30 bar open T
    * 23:30 open: flatten and cancel
    * in position: if the previous bar closed >= entry + risk * R:R, sell at T's open
    * outside the window: cancel the pending buy stop
    * signal candle (previous bar): cancel the old buy stop, place a new one at its
      high with the stop at its low (rejected if price is already at/above it)
    * any other bar leaves a pending order in place
  inside the bar, 1-minute OHLC decides fills, in the MT5 tester's model-1 order
  (bearish minute O-H-L-C, bullish minute O-L-H-C).

The replay is validated against the real RR and GG exports before the doji run.

Usage:
    python addiotional_helpers/doji_study.py      ---> writes results/doji_study.md and results/doji_study_trades.csv
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account_farming as af  # noqa: E402

M1_DEFAULT = Path(r"I:\PycharmProjects\Market_regime_detection\data\raw\databento"
                  r"\MT5_databento-ohlcv-1m.csv")
START, END = pd.Timestamp("2020-01-02"), pd.Timestamp("2026-07-14")
RR = 1.0
DOLLARS_PER_POINT = 2.0
PREDICATES = {
    "RR": lambda o, c: c < o,
    "GG": lambda o, c: c > o,
    "DOJI": lambda o, c: c == o,
}


def load_minutes(path: Path) -> pd.DataFrame:
    """Parse the MT5-format 1-minute file once; cache outside the repository."""
    cache = Path(tempfile.gettempdir()) / "doji_study_m1_2019_2026.pkl"
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        return pd.read_pickle(cache)
    df = pd.read_csv(path, sep="\t", usecols=[0, 1, 2, 3, 4, 5], dtype=str)
    df.columns = ["d", "t", "o", "h", "l", "c"]
    df = df[df["d"] >= "2019.12.20"]
    df["time"] = pd.to_datetime(df["d"] + " " + df["t"], format="%Y.%m.%d %H:%M:%S")
    for col in "ohlc":
        df[col] = df[col].astype(float)
    df = df[["time", "o", "h", "l", "c"]].reset_index(drop=True)
    df.to_pickle(cache)
    return df


def build_bars(minutes: pd.DataFrame) -> pd.DataFrame:
    groups = minutes.groupby(minutes["time"].dt.floor("30min"), sort=True)
    bars = pd.DataFrame({"o": groups["o"].first(), "h": groups["h"].max(),
                         "l": groups["l"].min(), "c": groups["c"].last()})
    index = groups.indices
    bars["m0"] = [index[key][0] for key in bars.index]
    bars["m1"] = [index[key][-1] + 1 for key in bars.index]
    return bars


def replay(bars: pd.DataFrame, minutes: pd.DataFrame, signal, hour: int) -> pd.DataFrame:
    """One window's run of the EA; returns its closed trades."""
    t = bars.index.to_numpy()
    o, h, l, c = (bars[col].to_numpy() for col in "ohlc")
    m0, m1 = bars["m0"].to_numpy(), bars["m1"].to_numpy()
    mt = minutes["time"].to_numpy()
    mo, mh, ml, mc = (minutes[col].to_numpy() for col in "ohlc")
    hours = pd.DatetimeIndex(t).hour.to_numpy()
    mins = pd.DatetimeIndex(t).minute.to_numpy()
    is_signal = signal(o, c)
    first, last = np.datetime64(START), np.datetime64(END + pd.Timedelta(days=1))
    trades, pos, pend = [], None, None

    for i in range(1, len(t)):
        if t[i] < first or t[i] >= last:
            continue
        if hours[i] == 23 and mins[i] == 30:                       # flatten cutoff
            if pos:
                trades.append((pos["et"], t[i], pos["entry"], o[i], pos["range"]))
                pos = None
            pend = None
            continue
        if pos:
            if c[i - 1] >= pos["entry"] + pos["risk"] * RR:          # bar-close target
                trades.append((pos["et"], t[i], pos["entry"], o[i], pos["range"]))
                pos = None
                continue
        elif hours[i] != hour:
            pend = None
        elif is_signal[i - 1]:
            pend = None
            entry, stop = h[i - 1], l[i - 1]
            if entry - stop > 0 and o[i] < entry:
                pend = {"entry": entry, "stop": stop, "range": entry - stop}

        k0, k1 = m0[i], m1[i]
        if pend and not pos:
            hits = np.nonzero(mh[k0:k1] >= pend["entry"])[0]
            if len(hits):
                k = k0 + hits[0]
                fill = max(pend["entry"], mo[k])
                pos = {"entry": fill, "stop": pend["stop"], "risk": fill - pend["stop"],
                       "et": mt[k], "range": pend["range"]}
                pend = None
                bearish = mc[k] < mo[k]
                if (ml[k] if bearish else mc[k]) <= pos["stop"]:
                    trades.append((pos["et"], mt[k], pos["entry"], pos["stop"], pos["range"]))
                    pos = None
                k0 = k + 1
        if pos and k0 < k1:
            hits = np.nonzero(ml[k0:k1] <= pos["stop"])[0]
            if len(hits):
                k = k0 + hits[0]
                price = mo[k] if mo[k] <= pos["stop"] else pos["stop"]
                trades.append((pos["et"], mt[k], pos["entry"], price, pos["range"]))
                pos = None

    out = pd.DataFrame(trades, columns=["entry_time", "exit_time", "entry", "exit", "risk_points"])
    out["pnl"] = (out["exit"] - out["entry"]) * DOLLARS_PER_POINT
    return out


def run_all(bars, minutes, name):
    frames = []
    for window in af.CANONICAL_WINDOWS:
        trades = replay(bars, minutes, PREDICATES[name], int(window.split("-")[0]))
        trades["window"] = window
        frames.append(trades)
    return pd.concat(frames, ignore_index=True)


def validation_row(name, simulated):
    real = pd.concat([af.read_trade_file(ROOT / "1_sweeps" / name / w / f"{w}_1.00.csv")
                      for w in af.CANONICAL_WINDOWS], ignore_index=True)
    real = real[(real.Entry_time >= START) & (real.Entry_time < END + pd.Timedelta(days=1))]
    real_keys = (pd.to_datetime(real.Entry_time).dt.floor("min").astype(str)
                 + "|" + real.groupby(pd.to_datetime(real.Entry_time).dt.floor("min")).cumcount().astype(str))
    sim_keys = (simulated.entry_time.dt.floor("min").astype(str)
                + "|" + simulated.groupby(simulated.entry_time.dt.floor("min")).cumcount().astype(str))
    return {"stream": name, "real": len(real), "replayed": len(simulated),
            "matched": len(set(real_keys) & set(sim_keys)),
            "real_pnl": float(real.PNL.astype(float).sum()), "replayed_pnl": float(simulated.pnl.sum())}


def summarize(trades):
    net = trades.pnl - af.COMMISSION_ROUNDTURN
    return {"trades": len(trades), "win_rate": float((trades.pnl > 0).mean()),
            "gross": float(trades.pnl.sum()), "net": float(net.sum()),
            "net_per_trade": float(net.mean()), "median_risk": float(trades.risk_points.median())}


def money(value):
    return ("-$" if value < 0 else "$") + f"{abs(value):,.0f}"


def markdown(span, validation, summaries, doji_trades):
    doji = span.c == span.o
    lines = [
        "# Doji study: frequency and a doji-only replay",
        "",
        "> **Scope:** raw trade-stream replay of the MT5 buy-stop EA with a doji (close == open) as the",
        "> signal candle. It does **not** model prop-account deaths, payouts or routing, and it treats",
        "> dojis as a strategy of their own: it does not measure how dojis would interact with RR or GG",
        "> orders (re-pricing, cancelling, run caps) if both signals were traded in one strategy.",
        "",
        f"Period {START:%Y-%m-%d} to {END:%Y-%m-%d}, M30 bars rebuilt from the 1-minute Databento file",
        "behind `MNQcontDATABENTOcurr6`, all 23 study windows run separately at R:R 1.00, $2 per point,",
        f"${af.COMMISSION_ROUNDTURN:.2f} round-turn commission. Generated by `addiotional_helpers/doji_study.py`.",
        "",
        "## Doji frequency",
        "",
        "| M30 bars | dojis | share | red | green |",
        "|---:|---:|---:|---:|---:|",
        f"| {len(span):,} | {int(doji.sum()):,} | {100 * doji.mean():.2f}% | "
        f"{int((span.c < span.o).sum()):,} | {int((span.c > span.o).sum()):,} |",
        "",
        "## Replay validation against the real sweep exports",
        "",
        "| stream | real trades | replayed | same entry minute | real gross | replayed gross |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in validation:
        lines.append(f"| {row['stream']} | {row['real']:,} | {row['replayed']:,} | "
                     f"{100 * row['matched'] / row['real']:.1f}% | {money(row['real_pnl'])} | "
                     f"{money(row['replayed_pnl'])} |")
    lines += [
        "",
        "## Results",
        "",
        "| stream | trades | win rate | gross | net | net per trade | median stop distance (pts) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in summaries.items():
        lines.append(f"| {name} | {s['trades']:,} | {100 * s['win_rate']:.1f}% | {money(s['gross'])} | "
                     f"{money(s['net'])} | ${s['net_per_trade']:.2f} | {s['median_risk']:.2f} |")
    by_year = doji_trades.assign(year=pd.to_datetime(doji_trades.entry_time).dt.year,
                                 net=doji_trades.pnl - af.COMMISSION_ROUNDTURN)
    by_year = by_year.groupby("year").agg(trades=("net", "size"), net=("net", "sum"))
    lines += ["", "Doji-only by year:", "", "| year | trades | net |", "|---:|---:|---:|"]
    for year, row in by_year.iterrows():
        lines.append(f"| {year} | {int(row.trades)} | {money(row.net)} |")
    lines += [
        "",
        "## Conclusion",
        "",
        "Dojis are under 1% of bars. Traded alone they are commission-level break-even, and the",
        "yearly results alternate sign. This is why both routed strategies keep the MT5 EAs'",
        "strict signal (no doji); see `project_memory/DECISIONS.md`.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--minutes", type=Path, default=M1_DEFAULT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    minutes = load_minutes(args.minutes)
    bars = build_bars(minutes)
    span = bars[(bars.index >= START) & (bars.index < END + pd.Timedelta(days=1))]

    streams = {name: run_all(bars, minutes, name) for name in PREDICATES}
    validation = [validation_row(name, streams[name]) for name in ("RR", "GG")]
    summaries = {name: summarize(streams[name]) for name in ("DOJI", "RR", "GG")}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    streams["DOJI"].to_csv(args.output_dir / "doji_study_trades.csv", index=False)
    report = args.output_dir / "doji_study.md"
    report.write_text(markdown(span, validation, summaries, streams["DOJI"]), encoding="utf-8")
    print(f"Wrote {report}")
    print(f"Wrote {args.output_dir / 'doji_study_trades.csv'}")


if __name__ == "__main__":
    main()
