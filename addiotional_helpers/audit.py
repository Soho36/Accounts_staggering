"""Check the five reported issues against the code."""
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

# Each robustness window must resolve its own one-position competition from a
# flat boundary. Slicing the all-history replay inherits blocking decisions made
# by positions that did not exist in the new book.
Q = af.robustness_periods(st, 2.0, min_trades=200)

POL = {
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
 "1/int, $1000 per $2000": (af.BookCfg(**base, seed=SEED, max_per_event=1,
                                       funding="cash"),
                            af.Harvest("ratchet", chunk=1000.0, step=2000.0)),
}

print("=" * 92)
print("ISSUE 1 - is cash_median a BALANCE or CUMULATIVE WITHDRAWALS?")
print("=" * 92)
print(f"{'policy':<26}{'balance med':>13}{'withdrawn med':>15}{'diff':>11}"
      f"{'seats':>7}")
rank_bal, rank_wd = {}, {}
for name, (cfg, h) in POL.items():
    res = [af.run_book(p["ex"], p["net"], p["mae"], p["mfe"], h, rule, cfg)
           for _, p in Q]
    bal = np.median([r["cash"] for r in res])
    wd = np.median([r["withdrawn"] for r in res])
    rank_bal[name], rank_wd[name] = bal, wd
    print(f"{name:<26}{bal:>13,.0f}{wd:>15,.0f}{wd-bal:>11,.0f}"
          f"{int(np.median([r['bought'] for r in res])):>7}")
print(f"\n  best by BALANCE     : {max(rank_bal, key=rank_bal.get)}")
print(f"  best by WITHDRAWALS : {max(rank_wd, key=rank_wd.get)}")

print("\n" + "=" * 92)
print("ISSUE 2 - does --split change compounding, or only scale the answer?")
print("=" * 92)
cfg, h = POL["1/int, $200 per $400"]
b = af.run_book(ex, net, mae, mfe, h, rule, cfg)
print(f"  current model, split applied AFTER the fact:")
for s in (1.0, 0.9, 0.8):
    print(f"    split {s:.0%} -> seats bought {b['bought']} (unchanged), "
          f"reported cash ${b['cash']*s:,.0f}, equity ${b['equity']*s:,.0f}")
print("  ^ seat count identical at every split: the book compounded on 100%.")


def run_book_split(ex, net, mae, mfe, h, rule, cfg, split):
    """run_book, but only the trader's share ever reaches the cash pot."""
    cash, live, bought, withdrawn = cfg.seed, [], 0, 0.0
    last_start = None
    for i in range(len(net)):
        day = pd.Timestamp(ex[i])
        if len(live) < cfg.seats and af.should_start(live, day, last_start, cfg):
            n = min(cfg.max_per_event, cfg.seats - len(live),
                    int(cash // rule.cost))
            for _ in range(max(0, n)):
                acc = af.new_account(i, rule); acc["start"] = day
                live.append(acc); cash -= rule.cost; bought += 1; last_start = day
        got = sum(af.step(acc, net[i], mae[i], mfe[i], h, rule) for acc in live)
        cash += got * split                      # <- the actual payout
        withdrawn += got * split
        live = [acc for acc in live if acc["alive"]]
    return {"bought": bought, "cash": cash, "withdrawn": withdrawn,
            "equity": sum(acc["eq"] for acc in live) * split, "live": len(live)}


print("\n  if the split were applied at payout time (correct model):")
for s in (1.0, 0.9, 0.8):
    r = run_book_split(ex, net, mae, mfe, h, rule, cfg, s)
    print(f"    split {s:.0%} -> seats bought {r['bought']:>3}, "
          f"cash ${r['cash']:>9,.0f}, equity ${r['equity']:>9,.0f}, "
          f"net ${r['cash']+r['equity']-cfg.seed:>10,.0f}")

print("\n" + "=" * 92)
print("ISSUE 3 - does a new seat take the trade that has just closed?")
print("=" * 92)


def run_book_nolook(ex, net, mae, mfe, h, rule, cfg):
    """Buy AFTER the trade is applied, so a new seat starts at i+1."""
    cash, live, bought, withdrawn = cfg.seed, [], 0, 0.0
    last_start = None
    for i in range(len(net)):
        day = pd.Timestamp(ex[i])
        got = sum(af.step(acc, net[i], mae[i], mfe[i], h, rule) for acc in live)
        cash += got; withdrawn += got
        live = [acc for acc in live if acc["alive"]]
        if len(live) < cfg.seats and af.should_start(live, day, last_start, cfg):
            n = min(cfg.max_per_event, cfg.seats - len(live),
                    int(cash // rule.cost))
            for _ in range(max(0, n)):
                acc = af.new_account(i + 1, rule); acc["start"] = day
                live.append(acc); cash -= rule.cost; bought += 1; last_start = day
    return {"bought": bought, "cash": cash, "withdrawn": withdrawn,
            "equity": sum(acc["eq"] for acc in live), "live": len(live)}


print(f"{'policy':<26}{'net NOW':>12}{'net FIXED':>12}{'diff':>10}{'diff %':>9}")
for name, (cfg, h) in POL.items():
    n1 = [af.run_book(p["ex"], p["net"], p["mae"], p["mfe"], h, rule, cfg)
          for _, p in Q]
    n2 = [run_book_nolook(p["ex"], p["net"], p["mae"], p["mfe"], h, rule, cfg)
          for _, p in Q]
    a1 = np.median([r["cash"] + r["equity"] - cfg.seed for r in n1])
    a2 = np.median([r["cash"] + r["equity"] - cfg.seed for r in n2])
    print(f"{name:<26}{a1:>12,.0f}{a2:>12,.0f}{a2-a1:>10,.0f}"
          f"{100*(a2-a1)/abs(a1) if a1 else 0:>8.1f}%")

print("\n" + "=" * 92)
print("ISSUE 5 - is mfe-first optimistic or pessimistic?")
print("=" * 92)
print("  floor = min(peak - dd, frozen_floor); death test is eq + MAE <= floor.")
print("  mfe-first raises peak (and thus floor) BEFORE that test, so the floor")
print("  can only be >= what mae-first would use -> death at least as likely.\n")
for label, mf in (("mae-first (default)", False), ("mfe-first", True)):
    r2 = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=mf)
    days = pd.to_datetime(ex).normalize()
    fi = pd.Series(range(len(ex))).groupby(days).min().sort_index()
    froze = died = n = 0
    for d, i0 in fi.items():
        if (pd.Timestamp(ex[-1]) - d).days < 365:
            continue
        acc = af.run_account(net, mae, mfe, int(i0), af.HARDLESS
                             if hasattr(af, "HARDLESS")
                             else af.Harvest("level", keep=r2.safety_net), r2)
        n += 1
        froze += acc["frozen"]
        died += acc["dead_i"] is not None
    cfgb, hb = POL["1/int, $200 per $400"]
    bb = af.run_book(ex, net, mae, mfe, hb, r2, cfgb)
    print(f"  {label:<22} froze {froze/n:>6.1%}  died {died/n:>6.1%}   "
          f"| book: seats {bb['bought']:>3} blowups {bb['deaths']:>3} "
          f"net ${bb['wealth']:>10,.0f}")
