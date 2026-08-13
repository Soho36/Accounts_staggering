"""Does 'negative at EVERY RR' generalise, when computed without the answer key?

The criterion is evaluated on 2020-2022 only. The verdict is 2023-2026 only.
"""
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
SWEEP, STATS = ROOT / "1_sweeps" / "RR", ROOT / "1_sweeps" / "RR_stats"
C = af.COMMISSION_ROUNDTURN
SPLIT = pd.Timestamp("2023-01-01")
RRS = [0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00]
rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
POL = af.Harvest("ratchet", chunk=200.0, step=400.0)
cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                 max_per_event=1, funding="cash")

wins = sorted(p.name for p in SWEEP.iterdir() if p.is_dir())
blown = [w for w in wins if af.pass_is_blown(STATS, w, 1.00) is True]

rec = {}
for w in wins:
    negA = negFull = 0
    a_tot = b_tot = 0.0
    for rr in RRS:
        f = SWEEP / w / f"{w}_{rr:.2f}.csv"
        if not f.exists():
            continue
        d = af.read_trade_file(f); pnl = d["PNL"] - C
        pa = pnl[d["Exit_time"] < SPLIT].sum()
        pb = pnl[d["Exit_time"] >= SPLIT].sum()
        negA += pa < 0
        negFull += (pa + pb) < 0
        if rr == 1.00:
            a_tot, b_tot = pa, pb
    rec[w] = {"negA": negA, "negFull": negFull, "A": a_tot, "B": b_tot,
              "blown": w in blown}
R = pd.DataFrame(rec).T
print("negA = how many of the 8 RR settings lost money in 2020-22 alone")
print(R[["negA", "negFull", "A", "B", "blown"]].to_string())

# the criterion, using 2020-22 ONLY
picked = [w for w in wins if R.loc[w, "negA"] == len(RRS) and w not in blown]
print(f"\n  windows negative at ALL {len(RRS)} RRs using 2020-22 only: {picked}")
kept_losers = [w for w in picked if R.loc[w, "B"] < 0]
print(f"  of those, still losing in 2023-26: {kept_losers} "
      f"({len(kept_losers)}/{len(picked)})")


def evaluate(drop, label):
    keep = [w for w in wins if w not in drop and w not in blown]
    a = argparse.Namespace(input_csv=None, sweep_root=SWEEP, stats_root=STATS,
                           rr=1.00, windows=",".join(keep),
                           start_date="2023-01-01", end_date=None)
    st = af.build_stream(a)
    ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
    # Re-resolve entry blocking inside every OOS year. A slice of the replayed
    # 2023+ stream can otherwise inherit a position opened before that quarter.
    Q = af.robustness_periods(st, 1.0, min_trades=150)
    res = [af.run_book(p["ex"], p["net"], p["mae"], p["mfe"], POL, rule, cfg)
           for _, p in Q]
    nets = np.array([r["wealth"] for r in res])
    print(f"{label:<40}{len(keep):>4}{len(net):>8,}{net.sum():>10,.0f}"
          f"{af.dd_equity(net,mae,mfe):>9,.0f}"
          f"{np.median(nets):>10,.0f}{np.percentile(nets,10):>10,.0f}")


print("\n" + "=" * 92)
print("VERDICT ON 2023-2026 ONLY")
print("=" * 92)
print(f"{'variant':<40}{'win':>4}{'trades':>8}{'net $':>10}{'eqDD $':>9}"
      f"{'net med':>10}{'net p10':>10}")
evaluate([], "keep everything")
evaluate(picked, f"drop {len(picked)} picked by 2020-22 RR-robustness")
oracleB = [w for w in wins if R.loc[w, "B"] < 0 and w not in blown]
evaluate(oracleB, f"ORACLE: drop {len(oracleB)} that lost in 2023-26")
