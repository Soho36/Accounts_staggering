"""How much is 'per-window optimised RR' actually costing, out of sample?

Optimise each window's RR on 2020-2022 only. Then run 2023-2026 and compare
against just setting every window to RR 1.00.
"""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
SWEEP, STATS = ROOT / "1_sweeps" / "RR", ROOT / "1_sweeps" / "RR_stats"
C = af.COMMISSION_ROUNDTURN
SPLIT = pd.Timestamp("2023-01-01")
RRS = [round(0.5 + 0.1 * i, 2) for i in range(31)]        # 0.5 .. 3.5
rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
POL = af.Harvest("ratchet", chunk=200.0, step=400.0)
cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                 max_per_event=1, funding="cash")

wins = sorted(p.name for p in SWEEP.iterdir() if p.is_dir())
wins = [w for w in wins if af.pass_is_blown(STATS, w, 1.00) is not True]

cache = {}
def load(w, rr):
    k = (w, rr)
    if k not in cache:
        f = SWEEP / w / f"{w}_{rr:.2f}.csv"
        cache[k] = af.read_trade_file(f) if f.exists() else None
    return cache[k]

pnlA, pnlB = {}, {}
for w in wins:
    for rr in RRS:
        d = load(w, rr)
        if d is None: continue
        p = d["PNL"] - C
        pnlA[(w, rr)] = p[d["Exit_time"] < SPLIT].sum()
        pnlB[(w, rr)] = p[d["Exit_time"] >= SPLIT].sum()

bestA = {w: max((rr for rr in RRS if (w, rr) in pnlA),
                key=lambda rr: pnlA[(w, rr)]) for w in wins}
bestB = {w: max((rr for rr in RRS if (w, rr) in pnlB),
                key=lambda rr: pnlB[(w, rr)]) for w in wins}
print("Per-window RR chosen on 2020-22, and what was actually best in 2023-26")
print(f"{'window':<8}{'RR fit on A':>12}{'A P&L':>9}{'B P&L at that RR':>18}"
      f"{'B P&L at RR 1':>15}{'best RR in B':>14}")
same = 0
for w in wins:
    ra, rb = bestA[w], bestB[w]
    same += abs(ra - rb) < 0.001
    print(f"{w:<8}{ra:>12.2f}{pnlA[(w,ra)]:>9,.0f}{pnlB[(w,ra)]:>18,.0f}"
          f"{pnlB[(w,1.0)]:>15,.0f}{rb:>14.2f}")
print(f"\n  the RR fitted on 2020-22 was also the best choice for 2023-26 in "
      f"{same}/{len(wins)} windows")


def book(rr_map, label, start="2023-01-01"):
    parts = []
    for w in wins:
        d = load(w, rr_map[w])
        if d is None: continue
        d = d.sort_values("Exit_time")
        parts.append({"en": d["Entry_time"].values.astype("datetime64[s]"),
                      "ex": d["Exit_time"].values.astype("datetime64[s]"),
                      "net": (d["PNL"].values - C).astype(float),
                      "mae": d["MAE"].values.astype(float),
                      "mfe": d["MFE"].values.astype(float)})
    keep = af.replay(parts)
    rows = []
    for t, m in zip(parts, keep):
        for i in np.flatnonzero(m):
            rows.append((t["ex"][i], t["net"][i], t["mae"][i], t["mfe"][i]))
    rows.sort(key=lambda r: r[0])
    ex = np.array([r[0] for r in rows])
    msk = ex >= np.datetime64(pd.Timestamp(start))
    ex = ex[msk]
    net = np.array([r[1] for r in rows])[msk]
    mae = np.array([r[2] for r in rows])[msk]
    mfe = np.array([r[3] for r in rows])[msk]
    horizon = pd.Timedelta(days=365); darr = pd.to_datetime(ex); Q = []
    for d in pd.date_range(darr[0].normalize(), darr[-1], freq="QS"):
        if d + horizon > darr[-1]: break
        j = int(np.searchsorted(darr, d)); k = int(np.searchsorted(darr, d + horizon))
        if k - j > 150: Q.append((j, k))
    res = [af.run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], POL, rule, cfg)
           for j, k in Q]
    nets = np.array([r["wealth"] for r in res])
    print(f"{label:<38}{len(net):>8,}{net.sum():>10,.0f}"
          f"{af.dd_equity(net,mae,mfe):>9,.0f}{np.median(nets):>11,.0f}"
          f"{np.percentile(nets,10):>10,.0f}")


print("\n" + "=" * 88)
print("MEASURED ON 2023-2026 ONLY")
print("=" * 88)
print(f"{'configuration':<38}{'trades':>8}{'net $':>10}{'eqDD $':>9}"
      f"{'book med':>11}{'book p10':>10}")
book({w: bestA[w] for w in wins}, "per-window RR fitted on 2020-22  (OLD)")
book({w: 1.00 for w in wins},     "every window at RR 1.00          (NEW)")
book({w: bestB[w] for w in wins}, "ORACLE: per-window RR fitted on B")
