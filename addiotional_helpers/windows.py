"""Which hourly windows lose at every RR - and does dropping them help?"""
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, r"I:\PycharmProjects\Accounts_staggering")
import account_farming as af

ROOT = Path(r"I:\PycharmProjects\Accounts_staggering")
SWEEP = ROOT / "1_sweeps" / "RR"
STATS = ROOT / "1_sweeps" / "RR_stats"
C = af.COMMISSION_ROUNDTURN
RRS = [0.50, 0.75, 1.00, 1.50, 2.00, 2.50, 3.00]

wins = sorted(p.name for p in SWEEP.iterdir() if p.is_dir())
rows = []
for w in wins:
    r = {"window": w}
    halves = {}
    for rr in RRS:
        f = SWEEP / w / f"{w}_{rr:.2f}.csv"
        if not f.exists():
            r[rr] = np.nan; continue
        d = af.read_trade_file(f)
        pnl = (d["PNL"] - C)
        r[rr] = pnl.sum()
        if rr == 1.00:
            mid = d["Exit_time"].min() + (d["Exit_time"].max() - d["Exit_time"].min())/2
            halves["h1"] = pnl[d["Exit_time"] < mid].sum()
            halves["h2"] = pnl[d["Exit_time"] >= mid].sum()
            r["trades"] = len(d)
    r.update(halves)
    r["neg_at"] = sum(1 for rr in RRS if r.get(rr, 0) < 0)
    r["blown"] = af.pass_is_blown(STATS, w, 1.00)
    rows.append(r)

T = pd.DataFrame(rows).set_index("window")
print("Net P&L per window after $1.05 commission, by RR (raw files, pre-replay)")
print(T[RRS + ["neg_at", "h1", "h2", "blown"]].round(0).to_string())

always_neg = T[(T["neg_at"] == len(RRS)) & (T["blown"] != True)].index.tolist()
both_halves = [w for w in always_neg if T.loc[w, "h1"] < 0 and T.loc[w, "h2"] < 0]
print(f"\n  losing at ALL {len(RRS)} RR values: {always_neg}")
print(f"  ...and losing in BOTH halves of the sample at RR 1.0: {both_halves}")

blown = T[T["blown"] == True].index.tolist()
keep = [w for w in wins if w not in always_neg and w not in blown]


def pipeline(windows, label):
    a = argparse.Namespace(input_csv=None, sweep_root=SWEEP, stats_root=STATS,
                           rr=1.00, windows=windows, start_date=None, end_date=None)
    st = af.build_stream(a)
    ex, net, mae, mfe = st["ex"], st["net"], st["mae"], st["mfe"]
    rule = af.Rule(dd=2500.0, frozen_floor=100.0, cost=200.0, mfe_first=False)
    HARD = af.Harvest("level", keep=rule.safety_net)
    POL = af.Harvest("ratchet", chunk=200.0, step=400.0)
    cfg = af.BookCfg(seats=20, seed=1200.0, interval_days=30, policy="time",
                     max_per_event=1, funding="cash")
    en = pd.to_datetime(st["en"]); last = pd.Timestamp(ex[-1])
    days = pd.Series(pd.to_datetime(ex).normalize()).drop_duplicates().sort_values()
    froze = d2f = n = 0
    for d in days.iloc[::5]:
        if (last - d).days < 365: continue
        i0 = int(np.searchsorted(en.values, np.datetime64(d)))
        if i0 >= len(net): continue
        acc = af.run_account(net, mae, mfe, i0, HARD, rule); n += 1
        if acc["froze_i"] is not None:
            froze += 1; d2f += (pd.Timestamp(ex[acc["froze_i"]]) - d).days
    horizon = pd.Timedelta(days=int(365.25 * 2.0)); darr = pd.to_datetime(ex); Q = []
    for d in pd.date_range(darr[0].normalize(), darr[-1], freq="QS"):
        if d + horizon > darr[-1]: break
        j = int(np.searchsorted(darr, d)); k = int(np.searchsorted(darr, d + horizon))
        if k - j > 200: Q.append((j, k))
    res = [af.run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], POL, rule, cfg)
           for j, k in Q]
    nets = np.array([r["wealth"] for r in res])
    print(f"{label:<26}{len(st['windows']):>4}{st['offered']:>9,}{st['taken']:>9,}"
          f"{net.sum():>10,.0f}{af.dd_equity(net,mae,mfe):>9,.0f}"
          f"{froze/n:>7.0%}{d2f/max(froze,1):>7.0f}"
          f"{np.mean([r['worst_wipe']>=5 for r in res]):>9.0%}"
          f"{np.median(nets):>10,.0f}")


print(f"\n{'variant':<26}{'win':>4}{'offered':>9}{'taken':>9}{'net $':>10}"
      f"{'eqDD $':>9}{'froze':>7}{'days':>7}{'collapse':>9}{'net med':>10}")
pipeline("all", "all windows (current)")
pipeline(",".join(keep), f"drop {len(always_neg)} always-losing")
if both_halves:
    keep2 = [w for w in wins if w not in both_halves and w not in blown]
    pipeline(",".join(keep2), f"drop {len(both_halves)} both-halves losers")
