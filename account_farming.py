"""
account_farming.py
==================
Buy seats, harvest the survivors.

Companion to `prop_account_staggering.py`. That script simulates a staggered
book of accounts; this one adds the three things it does not model:

  1. SINGLE-POSITION REPLAY. The EA holds one position at a time, so when every
     hourly window is enabled they compete for one slot. The sweep files were
     each generated with only their own window active, so simply concatenating
     them counts entries that could never have been taken — about 26% of them
     on this data set. `prop_account_staggering.py` warns about the overlap;
     this script resolves it by replaying the merged stream and dropping any
     entry that arrives while the slot is busy.

  2. WITHDRAWALS AS SEED CAPITAL. Once an account's trailing drawdown freezes,
     its floor is nailed at +$100 forever, so its cushion is just equity minus
     100 — every dollar withdrawn makes it more fragile. But at a few hundred
     dollars a seat, withdrawn cash BUYS MORE SEATS. Harvesting is therefore not
     only a safety-for-cash trade, it is the only way to fund growth. An account
     that dies having paid for three replacements was a good account.

  3. THE DISTRIBUTION, NOT ONE PATH. Bootstrapping a book has an absorbing
     state: lose every seat while holding less than one seat's price in cash and
     it is over permanently. That makes the process chaotic — adjacent
     withdrawal levels can differ by orders of magnitude on a single run. Every
     policy is therefore scored over many overlapping fixed-length windows and
     reported as median / p10 / p90 / ruin rate.

ACCOUNT RULE (Legacy Performance Account):
    floor = min(peak_unrealized - TRAILING_DD, +100)
    Safety Net = TRAILING_DD + 100 of peak profit freezes the floor at +100.
Reaching the Safety Net is the whole game: after it the account can absorb a
full TRAILING_DD drawdown and never dies from trailing again.

Intraday matters, because the rule says *unrealized*: a trade that runs to +MFE
raises the floor even if it closes lower, and one that dips to -MAE can hit the
floor without ever closing there. Closed-trade P&L understates both. The
MAE-then-MFE ordering used here reproduced MT5's equity drawdown exactly on
every pass tested in the source project.

CAUTION: this is LEVERAGE, not alpha. Every seat trades identical signals and
differs only by start date. N seats is N contracts, and a drawdown deep enough
to kill one is deep enough to kill the book.

    python account_farming.py --dd 2500 --cost 200 --seats 20 --seed 1200
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from prop_account_staggering import read_trade_file, sweep_files

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
DEPOSIT = 5000.0            # tester deposit, used only to spot liquidated passes

# Reference figures from an MT5 run of the same configuration (RR strategy, all
# windows, RR 1.0, 2020-01 to 2026-07). Kept as a reconstruction check: if the
# merge below cannot approximately reproduce these, nothing downstream is valid.
MT5_REF = {"trades": 9775, "gross": 46343.50, "eq_dd": 6069.00}

TRAILING_DD = 2500.0
SAFETY_NET = TRAILING_DD + 100.0
FROZEN_FLOOR = 100.0
ACCOUNT_COST = 200.0


# ---------------------------------------------------------------- input ----
def pass_is_blown(stats_root: Path, window: str, rr: float):
    """True if MT5 liquidated this pass, so its per-trade export is truncated.

    The tester force-closes on a margin call and the EA's export hook never
    fires, so the fatal trade is missing from the file. Scoring such a pass
    would invent a result. Returns None when no stats file is available.
    """
    path = stats_root / window / f"{window}_{rr:.2f}_stats.csv"
    if not path.is_file():
        return None
    for encoding in ("utf-16", "utf-8", "cp1252"):
        try:
            stats = pd.read_csv(path, sep="\t", encoding=encoding)
            break
        except (UnicodeError, UnicodeDecodeError):
            continue
    else:
        return None
    try:
        row = stats.iloc[0]
        return (float(row["equity_dd"]) >= DEPOSIT * 0.95
                or float(row["net_profit"]) <= -DEPOSIT * 0.95)
    except (KeyError, IndexError, ValueError):
        return None


def replay(parts):
    """One-position replay: keep an entry only if the slot is free."""
    order = []
    for pi, t in enumerate(parts):
        for i in range(len(t["en"])):
            order.append((t["en"][i], t["ex"][i], pi, i))
    order.sort(key=lambda x: (x[0], x[1]))
    keep = [np.zeros(len(t["en"]), dtype=bool) for t in parts]
    open_until = np.datetime64("1970-01-01")
    for en, ex, pi, i in order:
        if en >= open_until:
            keep[pi][i] = True
            open_until = ex
    return keep


def dd_equity(net, mae, mfe):
    """MAE-then-MFE equity drawdown — the ordering validated against MT5."""
    eq = peak = mdd = 0.0
    for n, a, f in zip(net, mae, mfe):
        mdd = max(mdd, peak - (eq + min(a, 0.0)))
        peak = max(peak, eq + max(f, 0.0))
        eq += n
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return float(mdd)


def all_window_stream(sweep_root: Path, stats_root: Path, rr: float,
                      windows: str, commission: float):
    """Every window at one RR, merged into the stream one seat actually trades."""
    files = sweep_files(sweep_root, rr, windows)
    parts, kept_names, blown, unknown = [], [], [], []
    for path in files:
        window = path.parent.name
        state = pass_is_blown(stats_root, window, rr)
        if state is True:
            blown.append(window)
            continue
        if state is None:
            unknown.append(window)
        d = read_trade_file(path).sort_values("Exit_time")
        parts.append({
            "en": d["Entry_time"].values.astype("datetime64[s]"),
            "ex": d["Exit_time"].values.astype("datetime64[s]"),
            "net": (d["PNL"].values - commission).astype(np.float64),
            "mae": d["MAE"].values.astype(np.float64),
            "mfe": d["MFE"].values.astype(np.float64),
        })
        kept_names.append(window)
    keep = replay(parts)
    rows = []
    for t, m in zip(parts, keep):
        for i in np.flatnonzero(m):
            rows.append((t["ex"][i], t["net"][i], t["mae"][i], t["mfe"][i]))
    rows.sort(key=lambda r: r[0])
    offered = sum(len(p["en"]) for p in parts)
    taken = sum(int(m.sum()) for m in keep)
    return rows, kept_names, blown, unknown, offered, taken


# ------------------------------------------------------------- accounts ----
def new_account(i0):
    return {"i0": i0, "eq": 0.0, "peak": 0.0, "floor": -TRAILING_DD,
            "frozen": False, "banked": 0.0, "alive": True}


def step(a, n, ma, mf, keep):
    """Advance one account by one trade. Returns cash withdrawn on this trade."""
    if a["eq"] + min(ma, 0.0) <= a["floor"]:               # intraday dip kills it
        a["alive"] = False
        return 0.0
    a["peak"] = max(a["peak"], a["eq"] + max(mf, 0.0))     # unrealized peak counts
    a["floor"] = min(a["peak"] - TRAILING_DD, FROZEN_FLOOR)
    a["eq"] += n
    a["peak"] = max(a["peak"], a["eq"])
    a["floor"] = min(a["peak"] - TRAILING_DD, FROZEN_FLOOR)
    if a["eq"] <= a["floor"]:
        a["alive"] = False
        return 0.0
    if not a["frozen"] and a["peak"] >= SAFETY_NET:
        a["frozen"] = True
    if a["frozen"] and keep and a["eq"] > keep:
        w = a["eq"] - keep
        a["eq"] = keep
        a["banked"] += w
        return w
    return 0.0


def run_account(net, mae, mfe, i0, keep):
    a = new_account(i0)
    a["froze_i"] = None
    for i in range(i0, len(net)):
        step(a, net[i], mae[i], mfe[i], keep)
        if not a["alive"]:
            a["dead_i"] = i
            return a
        if a["frozen"] and a["froze_i"] is None:
            a["froze_i"] = i
    a["dead_i"] = None
    return a


def trace_account(ex, net, mae, mfe, i0, keep):
    """Full path of one seat: equity, the moving floor, and cash taken out."""
    a = new_account(i0)
    path = []
    for i in range(i0, len(net)):
        step(a, net[i], mae[i], mfe[i], keep)
        path.append((pd.Timestamp(ex[i]), a["eq"] + a["banked"], a["eq"], a["floor"]))
        if not a["alive"]:
            break
    return path


def run_book(ex, net, mae, mfe, keep, max_live, seed_cash, reinvest, adaptive):
    """A book of seats where withdrawn cash buys more seats.

    adaptive=True harvests hard (down to the Safety Net) while the book is still
    growing, then switches to `keep` once the seat limit is reached and extra
    cash no longer buys anything.
    """
    cash, live, bought, deaths, withdrawn = seed_cash, [], 0, 0, 0.0
    last_month, series, ruined = None, [], False
    for i in range(len(net)):
        day = pd.Timestamp(ex[i])
        if (day.year, day.month) != last_month:
            last_month = (day.year, day.month)
            want = int(cash // ACCOUNT_COST) if reinvest else 1
            for _ in range(max(0, min(want, max_live - len(live),
                                      int(cash // ACCOUNT_COST)))):
                live.append(new_account(i))
                cash -= ACCOUNT_COST
                bought += 1
        k = SAFETY_NET if (adaptive and len(live) < max_live) else keep
        got = sum(step(a, net[i], mae[i], mfe[i], k) for a in live)
        cash += got
        withdrawn += got
        n0 = len(live)
        live = [a for a in live if a["alive"]]
        deaths += n0 - len(live)
        if not live and cash < ACCOUNT_COST:           # absorbing state
            ruined = True
        series.append((day, len(live), withdrawn, cash))
    S = pd.DataFrame(series, columns=["day", "live", "withdrawn", "cash"])
    return {"series": S, "bought": bought, "deaths": deaths, "cash": cash,
            "equity": sum(a["eq"] for a in live), "withdrawn": withdrawn,
            "live": len(live), "ruined": ruined,
            "wealth": cash + sum(a["eq"] for a in live) - seed_cash}


# --------------------------------------------------------------- report ----
_TPL = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Prop-account farming</title>
<script>__PLOTLYJS__</script>
<style>
 body{font:14px/1.45 system-ui,Segoe UI,Arial;margin:0;background:#f4f5f7;color:#1a1a1a}
 header{background:#1f2937;color:#fff;padding:14px 22px}h1{font-size:17px;margin:0}
 header p{margin:4px 0 0;font-size:12.5px;color:#cbd5e1}
 main{max-width:1240px;margin:16px auto;padding:0 16px}
 .cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px}
 .card{background:#fff;border-radius:10px;padding:12px 18px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:165px;flex:1}
 .card .t{font-size:11.5px;color:#6b7280;text-transform:uppercase}
 .card .v{font-size:21px;font-weight:600}.card .v.ok{color:#15803d}.card .v.bad{color:#b91c1c}
 .card .s{font-size:11.5px;color:#6b7280}
 .panel{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 table{border-collapse:collapse;width:100%;font-size:12.4px}
 th{text-align:right;padding:5px 8px;background:#f1f5f9;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 td{padding:4px 8px;text-align:right;border-top:1px solid #eef0f3;white-space:nowrap}
 .note{font-size:12.3px;color:#6b7280;margin:6px 0}
 h2{font-size:15px;margin:18px 0 8px}
 .warn{background:#fef3c7;border-left:3px solid #d97706;padding:10px 14px;border-radius:6px;font-size:12.8px;margin-bottom:16px}
 .warn b{color:#92400e}
</style></head><body>
<header><h1>Prop-account farming &mdash; buy seats, harvest the survivors</h1>
<p>RR strategy, every window, RR __RRV__, $__DDV__ trailing drawdown, $__COSTV__ per seat.</p>
</header><main>
<div class="cards" id="cards"></div>
<div class="warn"><b>This is leverage, not alpha.</b> Every seat trades identical
signals and differs only by start date. N seats is N contracts, and a drawdown deep
enough to kill one is deep enough to kill the book. The totals below scale with seat
count; the per-seat figures are what actually measure the strategy.</div>

<h2>1 &middot; How a seat lives and dies</h2>
<div class="panel"><div id="c_life" style="height:400px"></div>
 <div class="note">The floor chases the peak upward until peak profit reaches the Safety
 Net, then freezes at +$100 forever. Everything after that point is cushion. Withdrawing
 cash lowers equity toward that frozen floor &mdash; which is why harvesting kills seats.</div></div>

<h2>2 &middot; Does a seat reach the Safety Net? By start date</h2>
<div class="panel"><div id="c_starts" style="height:380px"></div>
 <div class="note">Green = reached the Safety Net, plotted at the number of days it took.
 Red = died first. The red clusters are what matters: seats started near each other share
 one fate, so staggering spreads entry points, not outcomes.</div></div>

<h2>3 &middot; The withdrawal question</h2>
<div class="panel"><div id="c_keep" style="height:440px"></div>
 <div class="note">Withdrawn cash buys more seats, so harvesting is not only a
 safety-for-cash trade &mdash; it is the only way to fund growth. "never" withdraw can
 never afford a second seat. Bars are the MEDIAN realized cash across every __NWIN__
 overlapping __HZV__-year window; whiskers are p10 to p90. Read the whisker, not the bar:
 the spread is wider than the difference between policies.</div></div>

<h2>4 &middot; The book over time</h2>
<div class="panel"><div id="c_book" style="height:400px"></div>
 <div class="note">Seats live (filled) against cumulative cash withdrawn. Every step down
 in seat count is a liquidation. One illustrative path, not an expectation.</div></div>
<div class="panel"><div id="c_year" style="height:320px"></div>
 <div class="note">Cash per calendar year from that same book. The spread between best and
 worst year is the risk this design carries, and it is not smoothed by holding more seats,
 because the seats are not independent.</div></div>

<h2>5 &middot; Every withdrawal policy, across all windows</h2>
<div class="panel" style="overflow:auto" id="t_keep"></div>
<div class="note">Cash is realized. Equity is still inside live accounts and can be lost
&mdash; never add the two together and call it profit.</div>

<h2>6 &middot; Reconstruction check</h2>
<div class="panel" style="overflow:auto" id="t_rec"></div>
<div class="note">The merged single-position stream against a real MT5 run of the same
configuration. It runs light: one window's export is tester-blown and excluded, and the
sweeps were each generated in isolation so the merge approximates the blocking rather
than reproducing it.</div>
<div class="note" id="foot"></div>
</main><script>
const D=__DATA__,CFG={displaylogo:false,responsive:true};
const F={family:'system-ui,Segoe UI,Arial',size:11.5};
document.getElementById('cards').innerHTML=D.cards.map(c=>
 `<div class="card"><div class="t">${c[0]}</div><div class="v ${c[2]||''}">${c[1]}</div>
  <div class="s">${c[3]||''}</div></div>`).join('');
Plotly.newPlot('c_life',[
 {x:D.life.x,y:D.life.eq,type:'scatter',mode:'lines',name:'equity in the account',
  line:{width:1.8,color:'#111'}},
 {x:D.life.x,y:D.life.fl,type:'scatter',mode:'lines',name:'liquidation floor',
  line:{width:1.6,color:'#e15759',shape:'hv'}},
 {x:D.life.x,y:D.life.tot,type:'scatter',mode:'lines',name:'equity + cash taken out',
  line:{width:1.4,color:'#4e79a7',dash:'dot'}}],
 {margin:{l:66,r:14,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'One seat, from purchase to liquidation',x:0,font:{size:13}},
  shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:D.safety,y1:D.safety,
   line:{color:'#15803d',width:1.2,dash:'dash'}}],
  annotations:[{xref:'paper',x:0.01,y:D.safety,text:'Safety Net - floor freezes here',
   showarrow:false,yshift:10,font:{size:11,color:'#15803d'}}],
  xaxis:{type:'date',gridcolor:'#eef0f3'},yaxis:{title:'$',gridcolor:'#eef0f3'},
  legend:{orientation:'h',y:-.14},plot_bgcolor:'#fff',paper_bgcolor:'#fff'},CFG);
Plotly.newPlot('c_starts',[
 {x:D.starts.okx,y:D.starts.oky,type:'scatter',mode:'markers',name:'reached Safety Net',
  marker:{size:5,color:'#59a14f',opacity:.65},
  hovertemplate:'start %{x|%Y-%m-%d}<br>froze after %{y} days<extra></extra>'},
 {x:D.starts.badx,y:D.starts.bady,type:'scatter',mode:'markers',name:'died first',
  marker:{size:6,color:'#e15759',symbol:'x',opacity:.75},
  hovertemplate:'start %{x|%Y-%m-%d}<br>never froze<extra></extra>'}],
 {margin:{l:66,r:14,t:28,b:34},font:F,
  title:{text:'Days from purchase to the Safety Net, by start date',x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'days to freeze',gridcolor:'#eef0f3',zeroline:false},
  legend:{orientation:'h',y:-.16},plot_bgcolor:'#fff',paper_bgcolor:'#fff'},CFG);
Plotly.newPlot('c_keep',D.keep.series.map((s,i)=>({
 x:D.keep.labels,y:s.y,name:s.name,type:'bar',
 marker:{color:['#4e79a7','#59a14f','#b07aa1'][i]},
 error_y:{type:'data',symmetric:false,array:s.hi,arrayminus:s.lo,
  color:'#6b7280',thickness:1.2,width:3},
 hovertemplate:'%{x}<br>'+s.name+'<br>median $%{y:,.0f}<extra></extra>'})),
 {margin:{l:70,r:14,t:28,b:52},font:F,barmode:'group',
  title:{text:'Realized cash over a fixed window - median, with p10 to p90',
   x:0,font:{size:13}},
  xaxis:{title:'withdraw down to',type:'category'},
  yaxis:{title:'cash withdrawn $',gridcolor:'#eef0f3'},
  legend:{orientation:'h',y:-.22},plot_bgcolor:'#fff',paper_bgcolor:'#fff'},CFG);
Plotly.newPlot('c_book',[
 {x:D.book.x,y:D.book.live,type:'scatter',mode:'lines',name:'seats live',
  fill:'tozeroy',line:{width:1,color:'#4e79a7',shape:'hv'},
  fillcolor:'rgba(78,121,167,.22)'},
 {x:D.book.x,y:D.book.cash,type:'scatter',mode:'lines',name:'cash withdrawn',
  yaxis:'y2',line:{width:2,color:'#15803d'}}],
 {margin:{l:60,r:66,t:28,b:34},font:F,hovermode:'x unified',
  title:{text:'Seats live and cash taken out - '+D.book.name,x:0,font:{size:13}},
  xaxis:{type:'date',gridcolor:'#eef0f3'},
  yaxis:{title:'seats',gridcolor:'#eef0f3'},
  yaxis2:{title:'cash $',overlaying:'y',side:'right',showgrid:false},
  legend:{orientation:'h',y:-.16},plot_bgcolor:'#fff',paper_bgcolor:'#fff'},CFG);
Plotly.newPlot('c_year',[{x:D.year.x,y:D.year.y,type:'bar',
 marker:{color:'#4e79a7'},hovertemplate:'%{x}<br>$%{y:,.0f}<extra></extra>'}],
 {margin:{l:70,r:14,t:28,b:34},font:F,
  title:{text:'Cash withdrawn per year',x:0,font:{size:13}},
  yaxis:{gridcolor:'#eef0f3'},plot_bgcolor:'#fff',paper_bgcolor:'#fff'},CFG);
function tbl(id,rows,cols,hdr){document.getElementById(id).innerHTML=
 '<table><thead><tr>'+hdr.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>'+
 rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c];if(v==null)v='';
  if(typeof v==='number')v=v.toLocaleString();
  return `<td>${v}</td>`;}).join('')+'</tr>').join('')+'</tbody></table>';}
tbl('t_keep',D.robust,['policy','withdraw_to','ruin_rate','cash_p10','cash_median',
 'cash_p90','equity_median','seats_median'],
 ['seat-buying policy','withdraw down to','ruin rate','cash p10 $','cash MEDIAN $',
  'cash p90 $','equity left (median) $','seats bought (median)']);
tbl('t_rec',D.rec,['metric','sim','mt5'],['metric','this simulation','MT5 run']);
document.getElementById('foot').textContent=
 `code ${D.git} · generated ${D.gen} · measured on 2020-2026. RR and the window set are `+
 `EA defaults, not fitted, but the window design still came from looking at this history. `+
 `The withdrawal level is a real free parameter and should be validated out-of-sample `+
 `before it is trusted.`;
</script></body></html>"""


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True,
                              timeout=10).stdout.strip() or "n/a"
    except (OSError, subprocess.SubprocessError):
        return "n/a"


def build_html(payload):
    try:
        import plotly.offline as po
    except ImportError:
        print("\n  plotly is not installed in this venv, so the HTML was skipped.")
        print("  Enable it with:  venv\\Scripts\\python.exe -m pip install plotly")
        return
    payload["gen"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    payload["git"] = git_rev()
    html = (_TPL.replace("__PLOTLYJS__", po.get_plotlyjs())
            .replace("__RRV__", f"{payload['rr']:g}")
            .replace("__DDV__", f"{payload['dd']:,.0f}")
            .replace("__COSTV__", f"{payload['cost']:,.0f}")
            .replace("__NWIN__", str(payload["n_windows"]))
            .replace("__HZV__", f"{payload['horizon']:g}")
            .replace("__DATA__", json.dumps(payload, separators=(",", ":"),
                                            default=str)))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "account_farming.html"
    out.write_text(html, encoding="utf-8")
    print(f"Saved {out}")


# ----------------------------------------------------------------- main ----
def main():
    global TRAILING_DD, SAFETY_NET, ACCOUNT_COST
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[3])
    ap.add_argument("--sweep-root", type=Path, default=ROOT / "1_sweeps" / "RR")
    ap.add_argument("--stats-root", type=Path, default=ROOT / "1_sweeps" / "RR_stats")
    ap.add_argument("--rr", type=float, default=1.00)
    ap.add_argument("--windows", default="all")
    ap.add_argument("--commission", type=float, default=1.0,
                    help="per round turn, subtracted from every trade")
    ap.add_argument("--dd", type=float, default=TRAILING_DD)
    ap.add_argument("--cost", type=float, default=ACCOUNT_COST)
    ap.add_argument("--seats", type=int, default=20,
                    help="max concurrent accounts the firm allows")
    ap.add_argument("--seed", type=float, default=1200.0,
                    help="starting cash; this drives the ruin rate more than anything")
    ap.add_argument("--split", type=float, default=1.0,
                    help="trader's share of profit (real plans are 0.8-0.9)")
    ap.add_argument("--horizon", type=float, default=2.0,
                    help="years per book window in the robustness sweep")
    a = ap.parse_args()
    TRAILING_DD, SAFETY_NET, ACCOUNT_COST = a.dd, a.dd + 100.0, a.cost
    KEEP = SAFETY_NET

    rows, names, blown, unknown, offered, taken = all_window_stream(
        a.sweep_root, a.stats_root, a.rr, a.windows, a.commission)
    ex = np.array([r[0] for r in rows])
    net = np.array([r[1] for r in rows])
    mae = np.array([r[2] for r in rows])
    mfe = np.array([r[3] for r in rows])
    gross = float((net + a.commission).sum())

    print("=" * 88)
    print(f"RECONSTRUCTION — all windows at RR {a.rr:g}, one position at a time")
    print("=" * 88)
    print(f"  windows merged          {len(names)}"
          + (f"   (excluded, tester-blown: {', '.join(blown)})" if blown else ""))
    if unknown:
        print(f"  WARNING: no MT5 stats for {len(unknown)} window(s), included "
              f"unchecked: {', '.join(unknown)}")
    print(f"  entries offered         {offered:,}")
    print(f"  entries taken           {taken:,}   "
          f"({offered - taken:,} blocked by the open position — the correction "
          f"a plain concatenation misses)")
    print(f"  gross profit        ${gross:>10,.0f}     MT5: ${MT5_REF['gross']:,.0f}"
          f"   ({100*gross/MT5_REF['gross']-100:+.1f}%)")
    print(f"  trades               {len(net):>10,}     MT5: {MT5_REF['trades']:,}"
          f"   ({100*len(net)/MT5_REF['trades']-100:+.1f}%)")
    print(f"  net after commission ${net.sum():>9,.0f}")
    print(f"  equity DD (MAE-first) ${dd_equity(net, mae, mfe):>8,.0f}     "
          f"MT5: ${MT5_REF['eq_dd']:,.0f}")

    # ---- one seat, from every possible start date ---------------------------
    days = pd.to_datetime(ex).normalize()
    first_i = pd.Series(range(len(ex))).groupby(days).min().sort_index()
    last_day = pd.Timestamp(ex[-1])
    out = []
    for d, i0 in first_i.items():
        h = run_account(net, mae, mfe, int(i0), KEEP)
        out.append({
            "start": d.date(), "start_i": int(i0),
            "runway_days": (last_day - d).days,
            "frozen": h["frozen"], "dead": h["dead_i"] is not None,
            "days_to_freeze": ((pd.Timestamp(ex[h["froze_i"]]) - d).days
                               if h["froze_i"] is not None else None),
            "banked": round(h["banked"]),
            "value": round(h["banked"] + (h["eq"] if h["dead_i"] is None else 0.0)),
        })
    S = pd.DataFrame(out)
    FULL = S[S["runway_days"] >= 365]     # censoring-free subset
    okS = S[S["frozen"]]
    badS = S[~S["frozen"]]
    med = FULL[FULL["frozen"]]["days_to_freeze"].median()

    print("\n" + "=" * 88)
    print(f"ONE SEAT, ${TRAILING_DD:,.0f} trailing DD — every possible start date")
    print("=" * 88)
    for label, X in (("all starts", S), ("starts with >=1yr of runway", FULL)):
        print(f"  {label:<30} n={len(X):<5} froze {X['frozen'].mean():>6.1%}   "
              f"died before freezing {(X['dead'] & ~X['frozen']).mean():>6.1%}")
    print(f"\n  days to the Safety Net (uncensored): median {med:.0f}, "
          f"p25 {okS['days_to_freeze'].quantile(.25):.0f}, "
          f"p75 {okS['days_to_freeze'].quantile(.75):.0f}")

    # ---- policies over many overlapping windows -----------------------------
    print("\n" + "=" * 88)
    print(f"ROBUSTNESS — every policy run from {a.horizon:g}-year windows, "
          f"quarterly starts, ${a.seed:,.0f} seed")
    print("=" * 88)
    horizon = pd.Timedelta(days=int(365.25 * a.horizon))
    day_arr = pd.to_datetime(ex)
    q_starts = []
    for d in pd.date_range(day_arr[0].normalize(), day_arr[-1], freq="QS"):
        if d + horizon > day_arr[-1]:
            break
        j = int(np.searchsorted(day_arr, d))
        k = int(np.searchsorted(day_arr, d + horizon))
        if k - j > 200:
            q_starts.append((d, j, k))
    print(f"  {len(q_starts)} windows, {q_starts[0][0].date()} .. "
          f"{q_starts[-1][0].date()}\n")
    POLICIES = (("1 seat/month", False, False),
                ("buy when affordable", True, False),
                ("affordable + hold when full", True, True))
    KEEPS = (SAFETY_NET, 3000.0, 4000.0, 5000.0, 7000.0, 0.0)
    rowsr = []
    for label, reinvest, adaptive in POLICIES:
        for kp in KEEPS:
            res = [run_book(ex[j:k], net[j:k], mae[j:k], mfe[j:k], kp, a.seats,
                            a.seed, reinvest, adaptive) for _, j, k in q_starts]
            cashes = np.array([r["cash"] for r in res]) * a.split
            rowsr.append({
                "policy": label,
                "withdraw_to": "never" if kp == 0 else f"${kp:,.0f}",
                "ruin_rate": f"{np.mean([r['ruined'] for r in res]):.0%}",
                "cash_p10": round(np.percentile(cashes, 10)),
                "cash_median": round(np.median(cashes)),
                "cash_p90": round(np.percentile(cashes, 90)),
                "equity_median": round(np.median([r["equity"] for r in res]) * a.split),
                "seats_median": int(np.median([r["bought"] for r in res])),
            })
    RB = pd.DataFrame(rowsr)
    print(RB.to_string(index=False))
    print("\n  cash_* is REALIZED, withdrawn money. equity_median is still sitting in")
    print("  live accounts and can be lost — do not add the two and call it profit.")
    best = RB.loc[RB["cash_median"].idxmax()]
    print(f"\n  best by MEDIAN realized cash: {best['policy']}, withdraw to "
          f"{best['withdraw_to']}  ->  ${best['cash_median']:,.0f} median over "
          f"{a.horizon:g}y  (p10 ${best['cash_p10']:,.0f}, ruin {best['ruin_rate']})")

    RESULTS.mkdir(exist_ok=True)
    S.to_csv(RESULTS / "farming_starts.csv", index=False)
    RB.to_csv(RESULTS / "farming_withdrawal_policies.csv", index=False)
    print(f"\nSaved {RESULTS / 'farming_starts.csv'}, "
          f"{RESULTS / 'farming_withdrawal_policies.csv'}")

    # ---- one illustrative book over the whole period ------------------------
    kp = 0.0 if best["withdraw_to"] == "never" else float(
        best["withdraw_to"].replace("$", "").replace(",", ""))
    pol = {p[0]: p[1:] for p in POLICIES}[best["policy"]]
    bk = run_book(ex, net, mae, mfe, kp, a.seats, a.seed, pol[0], pol[1])
    bs = bk["series"].groupby(bk["series"]["day"].dt.date).last().reset_index(drop=True)
    ycum = bk["series"].assign(y=bk["series"]["day"].dt.year).groupby("y")["withdrawn"].last()
    bkyr = (ycum.diff().fillna(ycum.iloc[0]) * a.split).round()

    rep = FULL[FULL["frozen"]].iloc[
        (FULL[FULL["frozen"]]["days_to_freeze"] - med).abs().argsort().iloc[0]]
    path = trace_account(ex, net, mae, mfe, int(rep["start_i"]), KEEP)
    path = path[::max(1, len(path) // 1500)]

    build_html({
        "rr": a.rr, "dd": TRAILING_DD, "cost": ACCOUNT_COST,
        "safety": SAFETY_NET, "horizon": a.horizon, "n_windows": len(q_starts),
        "cards": [
            ["reaches Safety Net", f"{FULL['frozen'].mean():.0%}", "ok",
             "of seats with a full year of runway"],
            ["median days to get there", f"{med:.0f}", "",
             f"p25 {okS['days_to_freeze'].quantile(.25):.0f} / "
             f"p75 {okS['days_to_freeze'].quantile(.75):.0f}"],
            ["best withdrawal policy", best["withdraw_to"], "", best["policy"]],
            [f"median cash per {a.horizon:g}y", f"${best['cash_median']:,.0f}", "ok",
             f"p10 ${best['cash_p10']:,.0f} · p90 ${best['cash_p90']:,.0f}"],
            ["ruin rate", best["ruin_rate"],
             "bad" if best["ruin_rate"] != "0%" else "ok",
             f"from a ${a.seed:,.0f} seed"],
            ["seat cap", str(a.seats), "", "firm rule, not a maths question"],
        ],
        "life": {"x": [str(p[0]) for p in path],
                 "tot": [round(p[1], 1) for p in path],
                 "eq": [round(p[2], 1) for p in path],
                 "fl": [round(p[3], 1) for p in path]},
        "starts": {"okx": [str(d) for d in okS["start"]],
                   "oky": [int(v) for v in okS["days_to_freeze"]],
                   "badx": [str(d) for d in badS["start"]],
                   "bady": [-40] * len(badS)},
        "keep": {
            "labels": list(RB[RB["policy"] == RB["policy"].iloc[0]]["withdraw_to"]),
            "series": [{"name": p,
                        "y": [int(v) for v in RB[RB["policy"] == p]["cash_median"]],
                        "hi": [int(h - m) for h, m in
                               zip(RB[RB["policy"] == p]["cash_p90"],
                                   RB[RB["policy"] == p]["cash_median"])],
                        "lo": [int(m - l) for m, l in
                               zip(RB[RB["policy"] == p]["cash_median"],
                                   RB[RB["policy"] == p]["cash_p10"])]}
                       for p in RB["policy"].unique()]},
        "robust": RB.to_dict("records"),
        "book": {"x": [str(d) for d in bs["day"]],
                 "live": [int(v) for v in bs["live"]],
                 "cash": [round(float(v)) for v in bs["withdrawn"]],
                 "name": f"{best['policy']}, withdraw to {best['withdraw_to']}"},
        "year": {"x": [str(i) for i in bkyr.index],
                 "y": [float(v) for v in bkyr]},
        "rec": [
            {"metric": "trades", "sim": f"{len(net):,}", "mt5": f"{MT5_REF['trades']:,}"},
            {"metric": "gross profit", "sim": f"${gross:,.0f}",
             "mt5": f"${MT5_REF['gross']:,.0f}"},
            {"metric": "equity drawdown", "sim": f"${dd_equity(net, mae, mfe):,.0f}",
             "mt5": f"${MT5_REF['eq_dd']:,.0f}"},
            {"metric": "windows merged", "sim": f"{len(names)}",
             "mt5": f"{len(names) + len(blown)} (one export tester-blown, dropped)"},
        ],
    })


if __name__ == "__main__":
    main()
