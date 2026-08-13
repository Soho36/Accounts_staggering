"""Full-period single paths vs the window distribution, for the key policies."""
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
SEED = 1200.0

base = dict(seats=20, interval_days=30, policy="time")
POL = {
 "MODE 1 subscription":
   (af.BookCfg(**base, seed=0.0, max_per_event=1, funding="external"),
    af.Harvest("none")),
 "MODE 2 all-in, strip to net":
   (af.BookCfg(**base, seed=SEED, max_per_event=20, funding="cash"),
    af.Harvest("level", keep=rule.safety_net)),
 "MODE 2 1/interval, strip to net":
   (af.BookCfg(**base, seed=SEED, max_per_event=1, funding="cash"),
    af.Harvest("level", keep=rule.safety_net)),
 "MODE 2 $200 per $400":
   (af.BookCfg(**base, seed=SEED, max_per_event=1, funding="cash"),
    af.Harvest("ratchet", chunk=200.0, step=400.0)),
}

print("=" * 104)
print("A. ONE FULL 6.5-YEAR PATH  (what the charts in the report show)")
print("=" * 104)
print(f"{'policy':<34}{'seats':>6}{'blow':>6}{'wipe':>6}{'alive':>6}"
      f"{'own $':>9}{'cash out':>11}{'equity':>11}{'NET':>11}")
for name, (cfg, h) in POL.items():
    b = af.run_book(ex, net, mae, mfe, h, rule, cfg)
    own = cfg.seed + b["spent"]
    print(f"{name:<34}{b['bought']:>6}{b['deaths']:>6}{b['wipeouts']:>6}"
          f"{b['live']:>6}{own:>9,.0f}{b['withdrawn']:>11,.0f}"
          f"{b['equity']:>11,.0f}{b['wealth']:>11,.0f}")

q = af.robustness_periods(st, 2.0)

print("\n" + "=" * 104)
print(f"B. THE SAME POLICIES OVER ALL {len(q)} TWO-YEAR WINDOWS  (the distribution)")
print("=" * 104)
print(f"{'policy':<34}{'wipe%':>7}{'ruin%':>7}{'worst':>10}{'net p10':>10}"
      f"{'net med':>10}{'net p90':>10}{'<=own?':>8}")
for name, (cfg, h) in POL.items():
    res = [af.run_book(p["ex"], p["net"], p["mae"], p["mfe"], h, rule, cfg)
           for _, p in q]
    nets = np.array([r["wealth"] for r in res])
    own = np.array([cfg.seed + r["spent"] for r in res])
    print(f"{name:<34}"
          f"{np.mean([r['wipeouts']>0 for r in res]):>6.0%}"
          f"{np.mean([r['ruined'] for r in res]):>7.0%}"
          f"{nets.min():>10,.0f}{np.percentile(nets,10):>10,.0f}"
          f"{np.median(nets):>10,.0f}{np.percentile(nets,90):>10,.0f}"
          f"{np.mean(nets<=0):>7.0%}")

print("\n  worst   = worst single window's net")
print("  <=own?  = share of windows that ended with net <= 0, i.e. you got back")
print("            less than the money you put in")

print("\n" + "=" * 104)
print("C. IS MODE 1's EQUITY ACTUALLY WITHDRAWABLE? (end of the full run)")
print("=" * 104)
cfg, h = POL["MODE 1 subscription"]
b = af.run_book(ex, net, mae, mfe, h, rule, cfg, trace=True)
live = [s for s in b["seats"] if s["alive"]]
eqs = sorted(s["eq"] for s in live)
print(f"  {len(live)} live seats, total equity ${sum(eqs):,.0f}")
print(f"  per seat: min ${eqs[0]:,.0f}  median ${eqs[len(eqs)//2]:,.0f}  "
      f"max ${eqs[-1]:,.0f}")
print(f"  cushion above the frozen floor (${rule.frozen_floor:,.0f}) is what could be "
      f"taken out;")
print(f"  taking it all leaves every seat sitting ON its floor, so the next losing "
      f"trade kills it.")
print(f"  at a realistic 90% profit split that ${sum(eqs):,.0f} is "
      f"${0.9*sum(eqs):,.0f}.")
