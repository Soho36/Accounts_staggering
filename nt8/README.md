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

Seats holding a **position** are invisible to the router. That is the overlap
which forces `K > R`, and it is why total open contracts can exceed R.

## Per-seat settings

All six charts: same instrument, same timeframe, all 23 windows enabled with the
same R:R values. Only the four seat properties differ.

| Account | Instance ID | Seat starting balance | Seat trailing drawdown | Tier |
|---|---:|---:|---:|---|
| pa-09 | 1 | 25000 | 1500 | 25K |
| pa-10 | 2 | 25000 | 1500 | 25K |
| pa-12 | 3 | 50000 | 2500 | 50K |
| pa-13 | 4 | 50000 | 2500 | 50K |
| pa-14 | 5 | 50000 | 2500 | 50K |
| pa-15 | 6 | 50000 | 2500 | 50K |

Identical on every chart:

| Property | Value |
|---|---|
| Routing Mode | `Shadow` at first, `Live` only after the checks below |
| Book ID | `LIVE` (use a different ID for any Sim101 chart) |
| R — copies per signal | `1` |
| Frozen floor offset | `100` |
| Track peak on unrealized | `true` (intraday trailing rule) |
| Require headroom covers stop | `false` (matches the study; `true` adds a safety filter) |

Starting balances are inferred from the Apex 25K / 50K tiers. **Confirm them**
— every floor is measured against this number.

## Seeding the peak before going Live

The router only learns an account's high-water mark from the moment it starts
running. Until each seat's peak is correct, its headroom is wrong, and a wrong
peak is worse than no router at all — an understated peak makes a damaged seat
look pristine and attracts the next trade.

Read the current trailing threshold off the Apex dashboard and convert:

```
peak = trailing_threshold + drawdown
```

Then either edit `Documents\NinjaTrader 8\PropRouter\peaks_LIVE.csv` directly:

```
account,start_balance,drawdown,peak,updated_utc
pa-09,25000.00,1500.00,26600.00,...
```

or call `PropRouter.OverridePeak("LIVE", instanceId, peak)` once.

For a seat whose floor has already frozen, any peak at or above
`start + dd + 100` gives the same floor, so the exact value does not matter.
For a seat still trailing, it matters to the dollar.

## Checks before switching to Live

1. **Shadow for a full session.** Every chart prints a `👁 shadow` line on each
   signal. Confirm each seat's computed floor equals the dashboard's trailing
   threshold. If they disagree, the peak or the starting balance is wrong.
2. **Restart NT8 mid-session.** Headroom must come back identical. If it jumps,
   persistence is broken.
3. **Six Sim101 charts on book `SIM`, Routing Mode Live, R=1.** Exactly one seat
   should submit per signal, and `routing_SIM_*.csv` should show one winner per
   decision with no seat starved.
4. **Cut the whole book over at once.** A half-routed book is neither
   architecture and cannot be attributed.

## Output

`Documents\NinjaTrader 8\PropRouter\`

- `peaks_<book>.csv` — high-water marks, rewritten on every new high
- `routing_<book>_<yyyyMMdd>.csv` — one row per seat per decision, with equity,
  peak, floor, headroom, frozen flag and trades taken

The routing log is what lets you replay live decisions through
`signal_router.py`'s allocator and confirm the two agree. Keep it permanently.

## Known limitations

- **NT8 downtime loses peaks.** If the platform is offline while an account
  makes a new intraday high, the stored peak is stale and headroom is
  overstated. Re-seed from the dashboard after any outage.
- **Shadow mode does not increment `trades_taken`**, so its tie-breaks fall
  through to instance id. Live mode counts properly.
- **Mixed drawdown sizes.** Raw dollar headroom is the correct survival measure
  across tiers, but the 25K seats have half the drawdown of the 50K seats and
  reach their floor twice as fast in a bad run.
- **Frozen seats are payout-capable assets.** `max_headroom` will preferentially
  route to them because a frozen floor lets headroom exceed the drawdown size.
  `signal_router.py` has a `protect_frozen` policy that deliberately does the
  opposite. This is a real policy question, not a bug.
