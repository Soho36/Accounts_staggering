"""How big was the book when it 'wiped out'? 1 seat is not 20 seats."""
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
base = dict(seats=20, interval_days=30, policy="time", split=1.0)

horizon = pd.Timedelta(days=int(365.25 * 2.0))
day_arr = pd.to_datetime(ex)
Q = []
for d in pd.date_range(day_arr[0].normalize(), day_arr[-1], freq="QS"):
    if d + horizon > day_arr[-1]:
        break
    j = int(np.searchsorted(day_arr, d)); k = int(np.searchsorted(day_arr, d + horizon))
    if k - j > 200:
        Q.append((j, k))

POL = {
 "subscription": (af.BookCfg(**base, seed=0.0, max_per_event=1,
                             funding="external"), af.Harvest("none")),
 "all-in, strip to net": (af.BookCfg(**base, seed=SEED, max_per_event=20,
                                     funding="cash"),
                          af.Harvest("level", keep=rule.safety_net)),
 "1/int, strip to net": (af.BookCfg(**base, seed=SEED, max_per_event=1,
                                    funding="cash"),
                         af.Harvest("level", keep=rule.safety_net)),
 "1/int, $200 per $400": (af.BookCfg(**base, seed=SEED, max_per_event=1,
                                     funding="cash"),
                          af.Harvest("ratchet", chunk=200.0, step=400.0)),
 "1/int, $200 per $1000": (af.BookCfg(**base, seed=SEED, max_per_event=1,
                                      funding="cash"),
                           af.Harvest("ratchet", chunk=200.0, step=1000.0)),
}


def wipes(ex, net, mae, mfe, h, rule, cfg):
    """Same loop as run_book, but records how many seats each wipeout cost."""
    cash, live, sizes = cfg.seed, [], []
    last_start = None
    for i in range(len(net)):
        day = pd.Timestamp(ex[i])
        got = sum(af.step(x, net[i], mae[i], mfe[i], h, rule) for x in live)
        cash += got * cfg.split
        n0 = len(live)
        live = [x for x in live if x["alive"]]
        if n0 and not live:
            sizes.append(n0)
        if len(live) < cfg.seats and af.should_start(live, day, last_start, cfg):
            room = cfg.seats - len(live)
            n = (min(cfg.max_per_event, room) if cfg.funding == "external"
                 else min(cfg.max_per_event, room, int(cash // rule.cost)))
            for _ in range(max(0, n)):
                x = af.new_account(i + 1, rule); x["start"] = day
                live.append(x)
                if cfg.funding != "external":
                    cash -= rule.cost
                last_start = day
    return sizes


print(f"{'policy':<24}{'windows w/ wipe':>17}{'wipes from 1-2 seats':>22}"
      f"{'from >=5 seats':>16}{'biggest':>9}")
for name, (cfg, h) in POL.items():
    allsz, any_w, big_w = [], 0, 0
    for j, k in Q:
        s = wipes(ex[j:k], net[j:k], mae[j:k], mfe[j:k], h, rule, cfg)
        allsz += s
        any_w += bool(s)
        big_w += any(v >= 5 for v in s)
    small = sum(1 for v in allsz if v <= 2)
    big = sum(1 for v in allsz if v >= 5)
    print(f"{name:<24}{any_w}/{len(Q)}{'':>11}{small:>16}{big:>16}"
          f"{max(allsz) if allsz else 0:>9}")

print("\n  A book holding one seat that loses it is counted the same as a book of")
print("  twenty losing all twenty. They are not the same event.")
