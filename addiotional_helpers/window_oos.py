"""Would dropping the losing windows have helped OUT of sample?

Selection uses ONLY 2020-2022. The verdict is measured ONLY on 2023-2026.
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
rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
HARD = af.Harvest("level", keep=rule.safety_net)
POL = af.Harvest("ratchet", chunk=200.0, step=400.0)
cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                 max_per_event=1, funding="cash")

wins = sorted(p.name for p in SWEEP.iterdir() if p.is_dir())
blown = [w for w in wins if af.pass_is_blown(STATS, w, 1.00) is True]

rows = []
for w in wins:
    f = SWEEP / w / f"{w}_1.00.csv"
    d = af.read_trade_file(f)
    pnl = d["PNL"] - C
    rows.append({"window": w,
                 "A_2020_22": pnl[d["Exit_time"] < SPLIT].sum(),
                 "B_2023_26": pnl[d["Exit_time"] >= SPLIT].sum(),
                 "blown": w in blown})
T = pd.DataFrame(rows).set_index("window").round(0)
print("Per-window net at RR 1.00, split at 2023-01-01 (raw files, pre-replay)")
print(T.to_string())

lose_A = [w for w in wins if T.loc[w, "A_2020_22"] < 0 and w not in blown]
lose_B = [w for w in wins if T.loc[w, "B_2023_26"] < 0 and w not in blown]
both = [w for w in lose_A if w in lose_B]
print(f"\n  lost in A (2020-22), knowable in advance : {lose_A}")
print(f"  lost in B (2023-26), the answer key      : {lose_B}")
print(f"  lost in BOTH                             : {both}")
hit = len(both) / len(lose_A) if lose_A else 0
print(f"  -> picking losers from A, {len(both)}/{len(lose_A)} stayed losers in B "
      f"({hit:.0%}); {len(lose_B)-len(both)} new losers appeared that A could not warn you about")


def evaluate(drop, label, start, end, horizon_years):
    keep = [w for w in wins if w not in drop and w not in blown]
    a = argparse.Namespace(input_csv=None, sweep_root=SWEEP, stats_root=STATS,
                           rr=1.00, windows=",".join(keep),
                           start_date=start, end_date=end)
    st = af.build_stream(a)
    ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
    en = pd.to_datetime(st["en"]); last = pd.Timestamp(ex[-1])
    days = pd.Series(pd.to_datetime(ex).normalize()).drop_duplicates().sort_values()
    froze = n = 0
    for d in days.iloc[::5]:
        if (last - d).days < 300: continue
        i0 = int(np.searchsorted(en.values, np.datetime64(d)))
        if i0 >= len(net): continue
        acc = af.run_account(net, mae, mfe, i0, HARD, rule); n += 1
        froze += acc["froze_i"] is not None
    horizon = pd.Timedelta(days=int(365.25 * horizon_years))
    darr = pd.to_datetime(ex); Q = []
    for d in pd.date_range(darr[0].normalize(), darr[-1], freq="QS"):
        if d + horizon > darr[-1]: break
        j = int(np.searchsorted(darr, d)); k = int(np.searchsorted(darr, d + horizon))
        if k - j > 150: Q.append((j, k))
    res = [af.run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], POL, rule, cfg)
           for j, k in Q]
    nets = np.array([r["wealth"] for r in res])
    print(f"{label:<32}{len(keep):>4}{len(net):>8,}{net.sum():>10,.0f}"
          f"{af.dd_equity(net,mae,mfe):>9,.0f}{froze/max(n,1):>7.0%}"
          f"{np.mean([r['worst_wipe']>=5 for r in res]):>9.0%}"
          f"{np.median(nets):>10,.0f}{len(Q):>5}")


hdr = (f"\n{'variant':<32}{'win':>4}{'trades':>8}{'net $':>10}{'eqDD $':>9}"
       f"{'froze':>7}{'collapse':>9}{'net med':>10}{'#win':>5}")
print("\n" + "=" * 96)
print("OUT OF SAMPLE: selection from 2020-22, measured on 2023-2026 only")
print("=" * 96 + hdr)
evaluate([], "keep everything", "2023-01-01", None, 1.0)
evaluate(lose_A, f"drop {len(lose_A)} that lost in 2020-22", "2023-01-01", None, 1.0)
evaluate(["12-13"], "drop 12-13 only", "2023-01-01", None, 1.0)
evaluate(["12-13", "15-16"], "drop 12-13 + 15-16", "2023-01-01", None, 1.0)
evaluate(["12-13", "15-16", "21-22"], "drop 12-13 + 15-16 + 21-22",
         "2023-01-01", None, 1.0)
evaluate(lose_B, f"ORACLE: drop {len(lose_B)} that lost in B", "2023-01-01", None, 1.0)

print("\n  ORACLE is unattainable - it uses the answer key. It is the ceiling on")
print("  what perfect window selection could have been worth.")

print("\n" + "=" * 96)
print("IS 15-16 REALLY PROFITABLE NEAR RR 0.6, OR IS THAT ONE LUCKY CELL?")
print("=" * 96)
out = []
for rr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 1.00]:
    f = SWEEP / "15-16" / f"{'15-16'}_{rr:.2f}.csv"
    if not f.exists(): continue
    d = af.read_trade_file(f); pnl = d["PNL"] - C
    out.append((rr, pnl.sum(), pnl[d["Exit_time"] < SPLIT].sum(),
                pnl[d["Exit_time"] >= SPLIT].sum(), len(d)))
print(f"{'RR':>6}{'full $':>10}{'2020-22 $':>12}{'2023-26 $':>12}{'trades':>8}")
for rr, tot, aa, bb, n in out:
    print(f"{rr:>6.2f}{tot:>10,.0f}{aa:>12,.0f}{bb:>12,.0f}{n:>8,}")
