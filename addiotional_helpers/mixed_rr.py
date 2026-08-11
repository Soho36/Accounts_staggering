"""Would running different RRs on different accounts actually decorrelate them?"""
import argparse, sys, itertools
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
RRS = [1.00, 1.10, 1.25, 1.50, 2.00, 2.50]
daily, entries = {}, {}
for rr in RRS:
    a = argparse.Namespace(input_csv=None, sweep_root=ROOT / "1_sweeps" / "RR",
                           stats_root=ROOT / "1_sweeps" / "RR_stats", rr=rr,
                           windows="all", start_date=None, end_date=None)
    st = af.build_stream(a)
    s = pd.Series(st["net"], index=pd.to_datetime(st["ex"]).normalize())
    daily[rr] = s.groupby(level=0).sum()
    entries[rr] = set(pd.to_datetime(st["en"]).values.astype("datetime64[s]").tolist())

D = pd.DataFrame(daily).fillna(0.0)
print(f"daily P&L series aligned on {len(D):,} trading days\n")
print("PEARSON CORRELATION of daily P&L between RR settings")
print(D.corr().round(3).to_string())

print("\nSHARE OF IDENTICAL ENTRY TIMESTAMPS (how much the trade sets overlap)")
print(f"{'':>7}" + "".join(f"{rr:>8.2f}" for rr in RRS))
for r1 in RRS:
    row = f"{r1:>7.2f}"
    for r2 in RRS:
        inter = len(entries[r1] & entries[r2])
        union = len(entries[r1] | entries[r2])
        row += f"{inter/union:>8.2f}"
    print(row)

print("\nWHAT A MIXED BOOK BUYS YOU (equal-weight, daily P&L)")
base = D[1.00]
print(f"{'book':<34}{'ann.vol $':>11}{'worst day $':>13}{'worst 20d $':>13}")


def stats(x, label):
    w20 = x.rolling(20).sum().min()
    print(f"{label:<34}{x.std()*np.sqrt(252):>11,.0f}{x.min():>13,.0f}{w20:>13,.0f}")


stats(base * 6, "6 seats all at RR 1.00")
for combo in [(1.00, 1.50), (1.00, 2.00), (1.00, 1.25, 1.50),
              (1.00, 1.25, 1.50, 2.00, 2.50)]:
    mix = sum(D[r] for r in combo) / len(combo) * 6
    stats(mix, f"6 seats spread over {len(combo)}: "
               + "/".join(f"{r:g}" for r in combo))
print("\n  All scaled to the same 6-contract exposure so the comparison is like")
print("  for like. Lower vol / shallower worst stretch = real diversification.")
