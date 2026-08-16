# NT8 max_headroom signal router

Two files:

| File | Goes in |
|---|---|
| `PropRouter.cs` | `Documents\NinjaTrader 8\bin\Custom\AddOns\` |
| `RR_..._SafeExits_Routed.cs` | `Documents\NinjaTrader 8\bin\Custom\Strategies\` |

The strategy's trading logic is byte-identical to
`RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits(EXAMPLE).cs`
apart from two inserted lines in the entry block (the routing gate) and status
publishing. Everything downstream of the entry — stop-limit placement, the R:R
limit exit, safe exits, the 23:57 flatten — is untouched.

## Model

Matches `account_farming.py` and `addiotional_helpers/signal_router.py` exactly:

```
floor    = min(peak - dd, start + 100)      # peak = high-water mark of NetLiquidation
headroom = equity - floor
select   = sorted(free_seats, key=(-headroom, trades_taken, instance_id))[:need]
need     = R - (seats already holding an unfilled entry order)
```

`headroom` is the same quantity as the **Cushion** column you compute from the
broker sheet: `balance − auto-liquidate threshold`. The threshold *is* the floor.

Seats holding a **position** are invisible to the router. That is the overlap
which forces `K > R`, and it is why total open contracts can exceed R.

## Per-seat settings

All six charts: same instrument, same timeframe, all 23 windows enabled with the
same R:R values. Only Instance ID, starting balance and drawdown differ.

| Account | Instance ID | Start balance | Drawdown | Tier |
|---|---:|---:|---:|---|
| PA-APEX-240737-09 | 1 | 25000 | 1500 | 25K |
| PA-APEX-240737-10 | 2 | 25000 | 1500 | 25K |
| PA-APEX-240737-12 | 3 | 50000 | 2500 | 50K |
| PA-APEX-240737-13 | 4 | 50000 | 2500 | 50K |
| PA-APEX-240737-14 | 5 | 50000 | 2500 | 50K |
| PA-APEX-240737-15 | 6 | 50000 | 2500 | 50K |

Identical on every chart:

| Property | Value |
|---|---|
| Routing Mode | `UnroutedLogOnly` at first **with the legacy window split**, then `Routed` — see below |
| Book ID | `LIVE` (use a different ID for any Sim101 chart) |
| R — copies per signal | `1` |
| Frozen floor offset | `100` |
| Track peak on unrealized | `true` |
| Require headroom covers stop | `false` (matches the study; `true` adds a safety filter) |

### Window Risk/Reward

The strategy has no separate window on/off switch — `W00..W23` are derived from
the R:R values, and `IsTradeWindow()` is just `RR > 0`. **A window with R:R = 0
is disabled.** With every field left at 0, as they ship, the strategy never
trades.

The study's canonical set is `range(1, 24)` — hours 01 through 23, which is 23
windows. `00:00–01:00` is **not** among them and has no sweep data behind it:

| Field | Value |
|---|---|
| `00:00-01:00` | `0` — outside the study's window set, leave disabled |
| `01:00-02:00` … `23:00-00:00` | `1` — all 23 windows, global R:R of 1 |

Same on all six charts. The router study fixed `RR = 1.0`, so a global 1 is the
setting that makes live behaviour match what was measured.

One thing to confirm yourself: `GetWindowRiskReward()` indexes on `Time[0].Hour`,
which is the *chart's* timezone. The window numbering has to line up with the
hours in the MT5 exports. Your existing per-chart configs already encode that
mapping, so compare against them before overwriting.

### ⚠️ Routing Mode — none of these is a dry run

**Every mode submits real orders to the account the chart is set to.** The mode
controls only whether the router may *veto* an entry. There is no paper-trading
mode; to trade without real orders, set the chart's Account to **Sim101**.

| Mode | Router | Orders on this account |
|---|---|---|
| `Routed` | decides | only signals routed here. An unseeded seat is never selected, so this **fails closed** — no seed file, no trades. |
| `UnroutedLogOnly` | logs what it *would* choose, **never blocks** | **every** signal in every enabled window |
| `Unrouted` | bypassed | **every** signal in every enabled window |

`UnroutedLogOnly` was previously named `Shadow`, which read as "dry run". It is
not. It is `Unrouted` plus a log line. Both unrouted modes trade identically.

**The exposure trap.** With the legacy per-chart window split, an unrouted mode
is harmless — each account still trades only its own hours. But the moment all
six charts carry all 23 windows, an unrouted mode means *all six accounts take
every signal*: six copies per signal on a book sized for one, with nothing to
stop it.

So the window change and the mode change must not be made independently:

1. **`UnroutedLogOnly` first, keeping each chart's existing window split.** This
   validates peaks, floors, account names and persistence without touching
   exposure. The routing decisions it prints are not meaningful yet — the
   signals don't coincide — and that is fine, they are not the point.
2. **Then, in one edit:** set all 23 windows to 1 on all six charts *and* switch
   every chart to `Routed`. Never all-windows with the gate inactive.

The strategy prints a `⚠️ Routing Mode = …: this is NOT a dry run` warning on
startup in either unrouted mode, naming the account and the enabled window count.

The default is `Routed`, chosen because it fails closed: a freshly applied
strategy with no seeded peak submits nothing at all.

### Book ID

There is only one live book, so the ID looks redundant. It exists because all
NinjaScript runs in one process and shares one static registry: a Sim101 chart
started for testing would otherwise register as a seat alongside the live ones
and could win live allocations. The Book ID is the namespace that keeps them
apart, and it also names the state and log files (`peaks_LIVE.csv`,
`routing_LIVE_*.csv`).

Use `LIVE` for the six live charts and something else — `SIM` — for anything on
Sim101. Every chart in a book must also carry the same `R`.

### On the two EOD accounts

`-12` and `-13` have no published Auto-Liquidate Threshold because they are
**end-of-day** trailing accounts; the other four trail intraday. They are
nonetheless configured as intraday (`Track peak on unrealized = true`), which is
deliberate.

An intraday peak is always ≥ an EOD peak, so modelling an EOD seat as intraday
gives `floor_modelled ≥ floor_real` and therefore `headroom_modelled ≤
headroom_real`. The router will route to those two slightly *less* often than it
strictly could. That is a small utilisation loss, not a risk. The reverse —
modelling an intraday seat as EOD — would overstate headroom and route into an
account thinner than it looks, and must never be done.

Unverified: whether Apex freezes an EOD threshold at the same `start + 100`.
The code assumes it does. That only matters once these two clear about +$2,600.

## Seeding the peaks — do this before anything is enabled

The router will not select a seat whose peak came from anywhere except the seed
file or an explicit `OverridePeak`. A peak bootstrapped from whatever equity
happened to be present at startup would put the floor at `equity − dd` and make
a damaged account look pristine, so an unseeded seat publishes state, logs
normally, and is never chosen. Both the strategy log and the routing log say so.

Where each seed peak comes from:

| Seat | Source | Peak | Floor | Headroom |
|---|---|---:|---:|---:|
| `-09` | threshold + dd | 26600.00 | 25100.00 | 2993.18 |
| `-10` | threshold + dd | 26600.00 | 25100.00 | 2570.70 |
| `-12` | sheet **Peak** column (EOD) | 51228.65 | 48728.65 | 1159.71 |
| `-13` | sheet **Peak** column (EOD) | 50480.45 | 47980.45 | 2235.89 |
| `-14` | threshold + dd | 50838.31 | 48338.31 | 1526.87 |
| `-15` | threshold + dd | 50844.85 | 48344.85 | 2319.43 |

All four seats with a published threshold reproduce the broker's cushion column
to the cent, which is the check that the formula is right.

**Use the correct column per account type.** For an intraday seat the sheet's
Peak column is a closed-balance high and is *wrong* — `-14` shows the gap
plainly: threshold + dd = 50838.31 but the sheet's Peak reads 50338.31, a $500
unrealised give-back that never closed. Seeding `-14` from the Peak column would
understate its floor by $500 and overstate its headroom by the same. For the two
EOD seats the closed-balance peak *is* the governing peak, so the Peak column is
exactly right there.

Ranked as `max_headroom` currently sees the book:

```
1. -09  2993.18      4. -13  2235.89
2. -10  2570.70      5. -14  1526.87
3. -15  2319.43      6. -12  1159.71
```

Note `-12` is the thinnest seat on the book, $1,340 below its own peak.

### The file

`Documents\NinjaTrader 8\PropRouter\peaks_LIVE.csv` — create by hand, folder
included:

```
account,start_balance,drawdown,peak,updated_utc
PA-APEX-240737-09,25000.00,1500.00,26600.00,2026-08-17T00:00:00Z
PA-APEX-240737-10,25000.00,1500.00,26600.00,2026-08-17T00:00:00Z
PA-APEX-240737-12,50000.00,2500.00,51228.65,2026-08-17T00:00:00Z
PA-APEX-240737-13,50000.00,2500.00,50480.45,2026-08-17T00:00:00Z
PA-APEX-240737-14,50000.00,2500.00,50838.31,2026-08-17T00:00:00Z
PA-APEX-240737-15,50000.00,2500.00,50844.85,2026-08-17T00:00:00Z
```

Four things that break it silently:

1. **Decimal separator.** The parser uses `InvariantCulture`, so numbers need a
   period: `25000.00`. Excel on an Estonian locale writes `25000,00` — and since
   the delimiter is also a comma, every row splits into the wrong field count and
   fails to match. Open it in Notepad and confirm you see periods. Write it in a
   plain text editor, not Excel.
2. **Account name.** Compared on the part before the first `!`, so both
   `PA-APEX-240737-09` and the `PA-APEX-240737-09!Apex!Apex` form NT8 shows in
   the Account dropdown will match. Everything after the `!` is ignored. If it
   still fails to match, the registration line prints the exact string NT8
   handed the router — copy that.
3. **Write it with the strategies disabled.** The router rewrites the whole file
   on every new equity high, so a hand-edit made while it is running is lost.
4. **It is read once, at `State.Realtime`.** Write the file first, then enable
   the strategies.

UTF-8 with or without BOM is fine. Comma delimiter is what the parser expects.
Only the `peak` column is read back — `start_balance` and `drawdown` come from
each chart's properties and are in the file for your eyes.

After seeding, the file maintains itself: the router rewrites it on every new
equity high. Re-seed by hand only after an NT8 outage (below) or an account reset.

## Checks before switching to Live

1. **Confirm all six seats seeded.** Every strategy should print `peak seeded
   at …` on startup. Any `⛔ PEAK NOT SEEDED` means that seat will never trade.
2. **`UnroutedLogOnly` for a full session, windows unchanged.** Keep each chart's existing
   per-window split — see the exposure trap above. Confirm each seat's computed
   floor equals the broker's threshold. If they disagree, the peak or the
   starting balance is wrong.
3. **Restart NT8 mid-session.** Headroom must come back identical. If it jumps,
   persistence is broken.
4. **Six Sim101 charts on book `SIM`, all 23 windows, `Routed`, R=1.** Setting
   the Account to Sim101 is the only way to run without real orders, and this is
   the first place the all-window config is safe. Exactly one seat should submit
   per signal, and `routing_SIM_*.csv` should show one winner per decision with
   no seat starved.
5. **Cut the whole book over at once** — all 23 windows and `Routed` in the same
   edit, on all six charts. A half-routed book is neither architecture and
   cannot be attributed, and an all-window chart left in an unrouted mode is a
   full extra copy of every signal.

## Output

`Documents\NinjaTrader 8\PropRouter\`

- `peaks_<book>.csv` — high-water marks, rewritten on every new high. Only
  seeded seats are written back, so a bootstrapped peak can never launder itself
  into a seeded one across a restart.
- `routing_<book>_<yyyyMMdd>.csv` — one row per seat per decision, with equity,
  peak, floor, headroom, frozen flag, seeded flag and trades taken.

The routing log is what lets you replay live decisions through
`signal_router.py`'s allocator and confirm the two agree. Keep it permanently.

## Known limitations

- **NT8 downtime loses peaks.** The peak only ratchets while the platform is
  running and in Realtime. If it is offline while an account makes a new intraday
  high, the stored peak is stale — which understates the floor and overstates
  headroom. Re-seed from the broker threshold after any outage.
- **`UnroutedLogOnly` does not increment `trades_taken`**, so its tie-breaks fall
  through to instance id. `Routed` counts properly.
- **Mixed drawdown sizes.** Raw dollar headroom is the correct survival measure
  across tiers, but the 25K seats have half the drawdown of the 50K seats and
  reach their floor twice as fast in a bad run.
- **Frozen seats are payout-capable assets.** `-09` and `-10` have already
  frozen — their floors are locked at 25,100 and will never rise again, which is
  why their headroom exceeds their $1,500 drawdown. `max_headroom` therefore
  ranks them first and will route to them most often. `signal_router.py` has a
  `protect_frozen` policy that deliberately does the opposite, on the reasoning
  that a frozen seat is an asset to shelter rather than a workhorse. This is a
  live policy question, not a bug.
