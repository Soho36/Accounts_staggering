# NT8 VWAP experiment — 2026-10-29

Two additive variants of the existing MaxRedRun strategies, plus one shared custom
indicator. No Order Flow VWAP or Order Flow subscription is used. The original
strategy files are unchanged.

## Install for simulation

1. Copy `RTLSessionVWAP.cs` into your NinjaTrader user-data folder's
   `bin\Custom\Indicators`. Install **one copy**, even if using both strategies.
2. Copy the wanted `...MaxRedRun_VWAP.cs` strategy file(s) into
   `bin\Custom\Strategies`. The routed variant also needs your existing
   `bin\Custom\AddOns\PropRouter.cs`.
3. In NinjaScript Editor, compile (F5). The new strategy names are:
   - `RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsMaxRedRunVWAP`
   - `RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRoutedMaxRedRunVWAP`
4. Use a standard **30-minute** chart, **Calculate.OnBarClose**, and your simulation
   account. Re-enter your existing R:R windows, quantity, Instance ID and routing
   settings. These are new strategy types; saved settings for the original types
   are not automatically transferred. All R:R windows still default to zero.
5. Use the full CME **ETH** trading-hours template to reproduce the futures study's
   overnight VWAP. The added one-minute series follows the primary instrument and
   its trading-hours selection; an RTH-only chart produces an RTH-only VWAP. Load
   several days of history for the session VWAP and 21-bar A20 warmup.

The strategy automatically adds the indicator to the chart by default. A blue line
shows session VWAP and an orange line shows the entry threshold. Set **Show VWAP
indicator=false** to hide the plots while retaining the entry filter. You can also
attach `RTLSessionVWAP` separately to a standard M30 chart.

For the routed strategy, use consistent VWAP settings on every seat in a book.
Enabled VWAP settings are included in the router configuration key; mixed settings
cannot form an agreeing book. A separate simulation book avoids conflicting with
an existing book's configuration. Existing router registration and seeding rules
still apply.

## Exact rule

For a red signal candle that passes the existing entry filters:

```
VWAP = sum(M1 HLC3 * M1 volume) / sum(M1 volume)
A20  = arithmetic mean of the latest 20 M30 true ranges, including the signal
line = VWAP + FarBelowZ * A20
skip = signal close < line
```

The default `FarBelowZ=-1` skips closes strictly below `VWAP-A20`. Equality allows
the entry. A20 spans session boundaries and is **not Wilder ATR**. VWAP uses NT8's
one-minute bar volume, with no tick-volume fallback or transaction-level Order Flow
calculation. Provider data can differ from MT5's broker data.

VWAP includes only completed minutes ending after the anchor and at or before the
signal's close, including the minute with that exact closing timestamp. NT8 stamps
bars by close time; the MT5 implementation stamps them by open time.

Unknown values allow entries, matching MT5: insufficient A20 history, zero A20,
unavailable/zero-volume minute data, pre-anchor bars, or a previous-session signal
evaluated when the new session starts. A partial initial session or missing final
minute also gives an unknown value, so a truncated or stale sum cannot reject an
entry. Internal gaps in a provider's minute history are not reconstructed.

A rejected red signal cancels the previous pending entry through the strategy's
existing cancellation flow. Green/doji bars retain the original pending-entry
behavior. VWAP does not close positions or change stop protection, take profit,
timeouts, entry expiry, or router reservation release rules. The new one-minute
callbacks are excluded from strategy order logic.

## VWAP settings

| Setting | Default | Meaning |
|---|---|---|
| Use VWAP gate | true | Apply the entry filter |
| Far-below Z | -1.0 | Threshold in units of simple A20 |
| Anchor minute | 0 | Minutes after the shifted session clock's midnight |
| Session time zone ID | Central Standard Time | Windows ID for Chicago, including US DST |
| Session clock shift (minutes) | 480 | Chicago + 8 hours, matching the MT5 futures session date |
| Show VWAP indicator | true | Add the blue VWAP and orange gate plots |
| Log VWAP decisions | false | Print candidate values and decisions to NinjaScript Output |

The default date rolls at **16:00 Chicago**, in the normal CME maintenance break;
the usual 17:00 Chicago open is 01:00 on the shifted clock. NT8's configured time
zone is converted to Chicago first. Do not substitute a fixed Tallinn/UTC offset:
US and European DST dates differ. Existing R:R windows continue to use NT8's clock.

For New York calendar dates, use `Eastern Standard Time` and shift `0`; anchor
`570` starts at 09:30. The default futures settings are the requested MT5/NQ
experiment, not a claim that its results transfer to another instrument.

To disable the gate, set **Use VWAP gate=false**. Plots and optional logging can
remain enabled on M30; disable all three VWAP switches if using another timeframe.
The gate-off routed manifest matches the original strategy's manifest.

Both strategies retain their original **realtime-only order logic**. Historical
bars warm the indicator but do not submit historical strategy trades; use Sim or
Market Replay for forward execution checks, not Strategy Analyzer trade counts.

## Validation and limits

The accompanying validation scripts compile against the installed NT8 assemblies
and exercise the actual calculation code and entry-gate methods. They check volume
weighting, true range averaging, session/DST conversion, exact cutoff inclusion,
no future-bar leakage, unknown values, and rejection semantics. They do not run
NT8's order engine or place trades.

Completed validation (2026-10-29): **47 calculation assertions and 37 entry-gate /
router-key assertions passed**. Both new strategies, both originals, the indicator,
and the existing router compiled together with **0 errors**. The external compile
reported 9 expected CS0436 source-versus-stock-base type warnings because it includes
NT8's generated base source alongside the stock Custom assembly.

Validation sources are retained in `MT5_Redgreen_tester/nt8_vwap`:
`verify_compile.ps1`, `tests/run-vwap-math-tests.ps1`, and
`tests/run-strategy-tests.ps1`. No strategy was enabled and no orders were placed.

Before interpreting a forward run, enable **Log VWAP decisions** and check the
first few M30 closes: a known decision should show VWAP, A20 and the threshold;
`SKIP` should coincide with a red close below the orange line. Check a session
rollover and an existing pending entry followed by a rejected red candle in Sim.
Native compilation cannot verify broker callbacks or real-time data arrival order.

Implementation references: [NT8 multi-series timing](https://ninjatrader.com/support/helpGuides/nt8/multi-time_frame__instruments.htm),
[hosted data-series requirements](https://ninjatrader.com/support/helpGuides/nt8/adddataseries.htm),
and [bar timestamps](https://ninjatrader.com/support/helpGuides/nt8/how_bars_are_built.htm).
The MT5 definition is in `MT5_Redgreen_tester/docs/baseline/rtl-vwap/MT5_EA.md`
and `mt5/experts/rtl_session_vwap.mqh`.
