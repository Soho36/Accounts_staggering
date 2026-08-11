"""How little of my own money does the bootstrap actually need?"""
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
a = argparse.Namespace(input_csv=None, sweep_root=ROOT / "1_sweeps" / "RR",
                       stats_root=ROOT / "1_sweeps" / "RR_stats", rr=1.00,
                       windows="all", start_date=None, end_date=None)
st = af.build_stream(a)
ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)

# same quarterly 2-year windows the report uses
horizon = pd.Timedelta(days=int(365.25 * 2.0))
day_arr = pd.to_datetime(ex)
q = []
for d in pd.date_range(day_arr[0].normalize(), day_arr[-1], freq="QS"):
    if d + horizon > day_arr[-1]:
        break
    j = int(np.searchsorted(day_arr, d)); k = int(np.searchsorted(day_arr, d + horizon))
    if k - j > 200:
        q.append((j, k))

print("=== 1. does seed=1 wait, or deadlock? (full 6.5y run) ===")
for sd in (1.0, 199.0, 200.0):
    cfg = af.BookCfg(seats=20, seed=sd, interval_days=30, policy="time",
                     max_per_event=1, funding="cash")
    h = af.Harvest("ratchet", chunk=200.0, step=400.0)
    b = af.run_book(ex, net, mae, mfe, h, rule, cfg)
    print(f"  seed ${sd:>6,.0f}  seats bought {b['bought']:>3}  "
          f"withdrawn ${b['withdrawn']:>9,.0f}  net ${b['wealth']:>9,.0f}  "
          f"ruined={b['ruined']}")

print("\n=== 2. seed sweep, 18 windows, $200 per $400 (the best-net rule) ===")
print(f"{'seed':>7}{'seats':>7}{'ruin':>7}{'wipeout':>9}{'blowups':>9}"
      f"{'cash med':>11}{'net p10':>10}{'net med':>10}")
for sd in (200, 400, 600, 800, 1000, 1200, 1600, 2000, 3000, 4000):
    cfg = af.BookCfg(seats=20, seed=float(sd), interval_days=30, policy="time",
                     max_per_event=1, funding="cash")
    h = af.Harvest("ratchet", chunk=200.0, step=400.0)
    res = [af.run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], h, rule, cfg)
           for j, k in q]
    cash = np.array([r["cash"] for r in res])
    eq = np.array([r["equity"] for r in res])
    nets = cash + eq - np.array([r["spent"] for r in res]) - sd
    print(f"{sd:>7,}{int(np.median([r['bought'] for r in res])):>7}"
          f"{np.mean([r['ruined'] for r in res]):>6.0%}"
          f"{np.mean([r['wipeouts'] > 0 for r in res]):>8.0%}"
          f"{int(np.median([r['deaths'] for r in res])):>9}"
          f"{np.median(cash):>11,.0f}{np.percentile(nets, 10):>10,.0f}"
          f"{np.median(nets):>10,.0f}")

print("\n=== 3. how often does a lone first seat die before it pays out? ===")
h = af.Harvest("ratchet", chunk=200.0, step=400.0)
days = pd.to_datetime(ex).normalize()
first_i = pd.Series(range(len(ex))).groupby(days).min().sort_index()
paid, died_first, n = 0, 0, 0
for d, i0 in first_i.items():
    if (pd.Timestamp(ex[-1]) - d).days < 365:
        continue
    acc = af.run_account(net, mae, mfe, int(i0), h, rule)
    n += 1
    if acc["banked"] > 0:
        paid += 1
    elif acc["dead_i"] is not None:
        died_first += 1
print(f"  of {n} possible start dates with >=1yr runway:")
print(f"    {paid/n:.1%} paid out at least $200 before dying")
print(f"    {died_first/n:.1%} died having never paid a cent "
      f"-> from a one-seat seed that is game over")
