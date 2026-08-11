"""RR as a FARMING parameter: which RR reaches the Safety Net fastest without dying?"""
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
HARD = af.Harvest("level", keep=rule.safety_net)
POLICY = af.Harvest("ratchet", chunk=200.0, step=400.0)   # the recommended rule
cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                 max_per_event=1, funding="cash", split=1.0)

RRS = [0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00]
print(f"{'RR':>6}{'trades':>8}{'net $':>10}{'eqDD $':>9}{'froze':>8}{'days':>7}"
      f"{'seats':>7}{'blow':>6}{'collapse':>10}{'cash med':>10}{'net med':>10}")
for rr in RRS:
    a = argparse.Namespace(input_csv=None, sweep_root=ROOT / "1_sweeps" / "RR",
                           stats_root=ROOT / "1_sweeps" / "RR_stats", rr=rr,
                           windows="all", start_date=None, end_date=None)
    try:
        st = af.build_stream(a)
    except Exception as e:
        print(f"{rr:>6.2f}  skipped: {e}")
        continue
    ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
    en = pd.to_datetime(st["en"])
    last = pd.Timestamp(ex[-1])

    # freeze rate / days-to-freeze, sampled every 5th start day for speed
    days = pd.Series(pd.to_datetime(ex).normalize()).drop_duplicates().sort_values()
    froze = d2f = n = 0
    for d in days.iloc[::5]:
        if (last - d).days < 365:
            continue
        i0 = int(np.searchsorted(en.values, np.datetime64(d)))
        if i0 >= len(net):
            continue
        acc = af.run_account(net, mae, mfe, i0, HARD, rule)
        n += 1
        if acc["froze_i"] is not None:
            froze += 1
            d2f += (pd.Timestamp(ex[acc["froze_i"]]) - d).days

    # the recommended policy across quarterly 2-year windows
    horizon = pd.Timedelta(days=int(365.25 * 2.0))
    darr = pd.to_datetime(ex)
    Q = []
    for d in pd.date_range(darr[0].normalize(), darr[-1], freq="QS"):
        if d + horizon > darr[-1]:
            break
        j = int(np.searchsorted(darr, d)); k = int(np.searchsorted(darr, d + horizon))
        if k - j > 200:
            Q.append((j, k))
    res = [af.run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], POLICY, rule, cfg)
           for j, k in Q]
    nets = np.array([r["wealth"] for r in res])
    print(f"{rr:>6.2f}{len(net):>8,}{net.sum():>10,.0f}"
          f"{af.dd_equity(net, mae, mfe):>9,.0f}"
          f"{froze/n:>7.0%}{d2f/max(froze,1):>7.0f}"
          f"{int(np.median([r['bought'] for r in res])):>7}"
          f"{int(np.median([r['deaths'] for r in res])):>6}"
          f"{np.mean([r['worst_wipe'] >= 5 for r in res]):>9.0%}"
          f"{np.median([r['cash'] for r in res]):>10,.0f}"
          f"{np.median(nets):>10,.0f}")

print("\n  froze/days = share of sampled start dates reaching the Safety Net, and the")
print("  mean days it took. collapse = share of windows losing a 5+ seat book.")
print("  ALL IN-SAMPLE on 2020-2026. Nothing here is a forward test.")
