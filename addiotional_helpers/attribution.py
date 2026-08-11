"""Standalone window P&L vs its ACTUAL contribution once windows compete for the slot."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
SWEEP, STATS = ROOT / "1_sweeps" / "RR", ROOT / "1_sweeps" / "RR_stats"
C = af.COMMISSION_ROUNDTURN
SPLIT = pd.Timestamp("2023-01-01")
wins = sorted(p.name for p in SWEEP.iterdir() if p.is_dir())
blown = [w for w in wins if af.pass_is_blown(STATS, w, 1.00) is True]
live = [w for w in wins if w not in blown]


def merged(window_list):
    parts, names = [], []
    for w in window_list:
        d = af.read_trade_file(SWEEP / w / f"{w}_1.00.csv").sort_values("Exit_time")
        parts.append({"en": d["Entry_time"].values.astype("datetime64[s]"),
                      "ex": d["Exit_time"].values.astype("datetime64[s]"),
                      "net": (d["PNL"].values - C).astype(float),
                      "mae": d["MAE"].values.astype(float),
                      "mfe": d["MFE"].values.astype(float)})
        names.append(w)
    keep = af.replay(parts)
    rows = []
    for w, t, m in zip(names, parts, keep):
        for i in np.flatnonzero(m):
            rows.append((t["ex"][i], t["net"][i], t["mae"][i], t["mfe"][i], w))
    rows.sort(key=lambda r: r[0])
    return rows


rows = merged(live)
A = pd.DataFrame(rows, columns=["ex", "net", "mae", "mfe", "w"])
A["ex"] = pd.to_datetime(A["ex"])
tot = A["net"].sum()
print(f"merged stream: {len(A):,} trades, net ${tot:,.0f}\n")

standalone = {}
for w in live:
    d = af.read_trade_file(SWEEP / w / f"{w}_1.00.csv")
    p = d["PNL"] - C
    standalone[w] = {"alone_net": p.sum(), "alone_n": len(d),
                     "alone_A": p[d["Exit_time"] < SPLIT].sum(),
                     "alone_B": p[d["Exit_time"] >= SPLIT].sum()}

g = A.groupby("w")["net"].agg(["sum", "count"])
T = pd.DataFrame(standalone).T
T["taken_n"] = g["count"]
T["taken_net"] = g["sum"]
T["kept%"] = (T["taken_n"] / T["alone_n"] * 100).round(0)
T = T.sort_values("taken_net")
print("Per window: ALONE vs its share of the merged book")
print(f"{'window':<8}{'alone n':>9}{'alone $':>10}{'taken n':>9}{'kept%':>7}"
      f"{'in-book $':>11}")
for w, r in T.iterrows():
    print(f"{w:<8}{int(r.alone_n):>9,}{r.alone_net:>10,.0f}{int(r.taken_n):>9,}"
          f"{r['kept%']:>6.0f}%{r.taken_net:>11,.0f}")

print("\n" + "=" * 78)
print("LEAVE-ONE-OUT: what the merged book does WITHOUT each window")
print("=" * 78)
print(f"{'dropped':<10}{'its in-book $':>15}{'book net w/o it':>17}{'change':>10}")
cands = ["12-13", "15-16", "21-22", "6-7", "4-5", "1-2", "22-23", "17-18"]
for w in cands:
    r2 = merged([x for x in live if x != w])
    n2 = sum(r[1] for r in r2)
    print(f"{w:<10}{T.loc[w,'taken_net']:>15,.0f}{n2:>17,.0f}{n2-tot:>10,.0f}")
print("\n  17-18 is the BEST window, included as a control: dropping it should hurt.")

print("\n" + "=" * 78)
print("DOES A WINDOW'S PAST PREDICT ITS FUTURE? (standalone, RR 1.00)")
print("=" * 78)
T["A"] = [standalone[w]["alone_A"] for w in T.index]
T["B"] = [standalone[w]["alone_B"] for w in T.index]
pear = T["A"].corr(T["B"])
spear = T["A"].rank().corr(T["B"].rank())   # no scipy in this venv
print(f"  Pearson  corr(2020-22 P&L, 2023-26 P&L) across {len(T)} windows = {pear:+.3f}")
print(f"  Spearman rank correlation                                   = {spear:+.3f}")
neg_then = T[T["A"] < 0]
pos_then = T[T["A"] >= 0]
print(f"\n  windows negative in 2020-22 (n={len(neg_then)}): "
      f"{(neg_then['B'] < 0).sum()} stayed negative, "
      f"{(neg_then['B'] >= 0).sum()} turned positive")
print(f"  windows positive in 2020-22 (n={len(pos_then)}): "
      f"{(pos_then['B'] < 0).sum()} turned negative, "
      f"{(pos_then['B'] >= 0).sum()} stayed positive")
print(f"\n  base rate: {(T['B'] >= 0).mean():.0%} of windows were profitable in 2023-26")
