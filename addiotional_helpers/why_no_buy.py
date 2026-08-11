"""Trace the $200-per-$1,000 bootstrap book: why no purchase Jan-Nov 2021?"""
import argparse
from pathlib import Path
import numpy as np, pandas as pd
import sys
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
a = argparse.Namespace(
    input_csv=None, sweep_root=ROOT / "1_sweeps" / "RR",
    stats_root=ROOT / "1_sweeps" / "RR_stats", rr=1.00, windows="all",
    start_date=None, end_date=None)
st = af.build_stream(a)
ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]

rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                 max_per_event=1, funding="cash")
h = af.Harvest("ratchet", chunk=200.0, step=1000.0)
print(f"safety net ${rule.safety_net:,.0f}; a seat must reach "
      f"${rule.safety_net + h.step:,.0f} of value before it pays ANYTHING\n")

# replay run_book by hand so we can look inside
cash, live, bought, withdrawn = cfg.seed, [], 0, 0.0
last_start = None
log, buys = [], []
for i in range(len(net)):
    day = pd.Timestamp(ex[i])
    if len(live) < cfg.seats and af.should_start(live, day, last_start, cfg):
        n = min(cfg.max_per_event, cfg.seats - len(live), int(cash // rule.cost))
        for _ in range(max(0, n)):
            acc = af.new_account(i, rule); acc["start"] = day
            live.append(acc); cash -= rule.cost; bought += 1; last_start = day
            buys.append((day, bought, cash))
    got = sum(af.step(acc, net[i], mae[i], mfe[i], h, rule) for acc in live)
    cash += got; withdrawn += got
    live = [acc for acc in live if acc["alive"]]
    log.append((day, len(live), cash, sum(acc["eq"] for acc in live), withdrawn,
                sum(1 for acc in live if acc["frozen"]),
                max([acc["eq"] + acc["banked"] for acc in live], default=0.0)))

L = pd.DataFrame(log, columns=["day", "seats", "cash", "equity", "withdrawn",
                               "frozen", "best_seat_value"])
D = L.groupby(L["day"].dt.date).last()

print("=== every purchase ===")
for d, n, c in buys:
    print(f"  {d.date()}  seat #{n:<3} cash left ${c:,.0f}")

print("\n=== month ends, 2020-06 .. 2022-01 ===")
M = L.set_index("day").groupby(pd.Grouper(freq="M")).last().dropna()
M = M[(M.index >= "2020-06-01") & (M.index <= "2022-01-31")]
print(f"{'month':<10}{'seats':>6}{'cash':>10}{'equity':>10}{'eq/seat':>9}"
      f"{'frozen':>8}{'best seat':>11}{'withdrawn':>11}")
for d, r in M.iterrows():
    per = r.equity / r.seats if r.seats else 0
    print(f"{d:%Y-%m}   {int(r.seats):>5}{r.cash:>10,.0f}{r.equity:>10,.0f}"
          f"{per:>9,.0f}{int(r.frozen):>8}{r.best_seat_value:>11,.0f}"
          f"{r.withdrawn:>11,.0f}")

apr = L[(L["day"] >= "2021-04-16") & (L["day"] < "2021-04-17")]
if len(apr):
    r = apr.iloc[-1]
    print(f"\n=== 2021-04-16 ===")
    print(f"  seats {int(r.seats)}   equity ${r.equity:,.0f}  "
          f"(${r.equity/r.seats:,.0f} per seat)")
    print(f"  cash on hand ${r.cash:,.0f}   -> can buy "
          f"{int(r.cash//rule.cost)} seats")
    print(f"  frozen seats {int(r.frozen)} of {int(r.seats)}; "
          f"best single seat value ${r.best_seat_value:,.0f}")
    print(f"  a seat pays out only above ${rule.safety_net + h.step:,.0f}")
