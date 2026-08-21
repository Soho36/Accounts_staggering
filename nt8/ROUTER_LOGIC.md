# How the router decides — read this before judging a log

The one-line rule is "send the signal to the account with the most headroom".
Everything below is the nuance around that sentence, written so a log that looks
wrong can be checked against what the router is actually supposed to do.

## The rule

```
floor    = min(peak - drawdown, start_balance + 100)
headroom = equity - floor
need     = R - (seats currently holding an unfilled entry order)
winners  = sorted(eligible_free_seats,
                  key = (-headroom, trades_taken, instance_id))[:need]
```

`equity` is NetLiquidation, so an open position moves a seat's headroom tick by
tick. `peak` only ever rises.

## Seat states, and what each means for allocation

| State | Meaning | Counts against R? | Can win a signal? |
|---|---|---|---|
| **Free** | flat, no working entry order | no | **yes** |
| **Pending** | entry order working, not filled | **yes** | no |
| **InPosition** | filled, stop and target live | **no** | no |

Those three rows are the whole design. Two of them surprise people.

### Pending counts against R — so one working order blocks the book

With `R = 1`, a seat holding an unfilled entry order makes `need = 1 - 1 = 0`,
so every other seat is told `winners=[none]`. That is correct: the system wants
one copy of the signal, and a copy is already offered to the market. When a later
red candle arrives, the Pending seat **re-prices its own order in place** and no
new claim is made.

In the log this looks like a run of:

```
[Long1_1] not routed here — not selected; headroom=1500.00; winners=[none]
[Long1_6] Submitted BUY STOP-LIMIT @ 29331 (re-priced working entry)
```

Seat 6 is not "winning again". It is carrying the same offer at a new price.

### InPosition does NOT count against R — so simultaneous positions are normal

A seat that has already filled is busy with an *earlier* signal. It is invisible
to the router, and it does not reserve any part of `R`. So the next signal goes
to a different free seat, and the book ends up holding two positions at once.

This is the entire reason `K > R`. `R` is copies **per signal**; it is not a cap
on open contracts across the book. Overlapping signals are exactly what the extra
seats exist to absorb — the study measured a peak of 5 simultaneous open signals,
which is where `K >= 5R` comes from.

Worked example from the 5-minute Playback log:

```
22:23:16  seat 6 fills          -> seat 6 InPosition
22:35:00  new signal            -> book 6:2077.50(InPosition) 4:2000 5:2000 3:2000(Pending)
          seat 6 is skipped (busy), seat 3 wins on headroom
22:35:59  seat 3 fills          -> seats 3 AND 6 both hold positions
22:40:00  seat 6 reaches 1R and submits its target
```

Two open contracts, `R = 1`, and nothing is wrong. If you ever want a hard cap on
total open contracts, that is a **separate** risk governor — the router does not
provide one, by design, because the study did not model one.

## Eligibility — a Free seat can still be skipped

A seat only competes if **all** of these hold:

- the book is ready: every expected seat registered, matching manifest, none stale;
- the seat is `Free`;
- its account is connected;
- its peak came from the seed file (never bootstrapped from current equity);
- `headroom > 0`;
- with *Require headroom covers stop* enabled, `headroom >= this candle's stop risk`.

If the book is not ready, **nobody** trades. That is fail-closed and deliberate.

## Tie-breaks

`(-headroom, trades_taken, instance_id)` — highest headroom, then fewest trades
taken, then lowest Instance ID. Identical to `signal_router.py`.

With a fresh book every seat sits at exactly its drawdown, so the ranking is
decided purely by drawdown size and the largest-drawdown seat takes everything
until it loses the difference:

```
6:2500  3,4,5:2000  1,2:1500
```

Seat 6 keeps winning until it is down $500, then seats 3/4/5 take over. That is
concentration, not a fault. The live book behaves the same way.

## Reading a decision line

```
✅ routed here — selected; headroom=2126.00; winners=[6];
   book 6:2126.00(Pending) 4:2000.00 5:2000.00 3:1949.50(InPosition) 1:1500.00 2:1500.00
```

- `headroom=` is this seat's own value.
- `winners=[...]` is the allocation for this bar, cached so all six agree.
- `book ...` is the full ranking, printed once per signal by the winner only.
  A bare number is Free; `(Pending)` and `(InPosition)` are tagged.

The full per-seat record — equity, peak, floor, headroom, frozen, seeded,
trades — is written to `PropRouter\routing_<BOOK>_<yyyymmdd>.csv`, one row per
seat per decision. That file, not the Output window, is the thing to analyse.

Render it rather than reading the CSV by hand:

```bash
python routing_report.py ".../PropRouter/routing_SIM_20260820.csv"
```

That writes one self-contained HTML file: headroom per seat across the session
with routed decisions marked, an equity-against-floor panel per seat that makes
the ratchet visible, the full decision table, and a seat summary carrying the
identity `change in headroom = change in equity - change in floor`.

## Things that look like bugs and are not

| Observation | Why it is correct |
|---|---|
| Two accounts hold positions at once | `InPosition` does not consume `R` |
| `winners=[none]` while a seat has a working order | `need = R - pending = 0` |
| One seat wins many signals in a row | It genuinely has the most headroom |
| A seat is skipped although it is flat | Book not ready, or another seat ranks higher |
| A busy seat prints no red-candle line | It returns early while in a position |
| `unregister refused while seat status is InPosition` | The seat holds real broker exposure; the reservation is retained on purpose |

## Things that ARE worth investigating

- A fill whose printed `Stop=` does not match the most recent red candle's `SL`.
- Any `⚠️ bound setup was stale` or `⛔ cannot identify the candle` line.
- `⚠️ releasing an unbacked Pending reservation` — means a submission never
  reached the broker.
- `⛔ PEAK NOT SEEDED` on any seat.

## Why headroom falls by more than the trade lost

This is the single most confusing thing in the log, and it is the mechanic the
whole study is about.

`equity` is NetLiquidation, so it includes **unrealised** profit, and `peak` is a
high-water mark that only ever rises. An open trade that goes into profit
therefore raises the peak, which raises the floor — permanently. Giving that
profit back does not lower the floor again.

    headroom lost  =  realised loss  +  unrealised profit given back

Two worked examples from the 5-minute Playback log, both verified against the
NinjaTrader trade export:

**Seat 3 — a losing trade that cost more headroom than it lost.**

```
entry 29310.50 -> exit 29290.50 (StopLimit_3)   realised -40.00, MFE +50.00
peak ratchets 100000.00 -> 100050.00   (the +50.00 unrealised high)
floor          98000.00 ->  98050.00
equity after the stop-out                        99960.00
headroom = 99960.00 - 98050.00 =                  1910.00   <- matches the log
```

The stop filled exactly at its price for exactly -$40. The extra $50 of headroom
was consumed by the floor ratcheting on profit the trade never kept. NinjaTrader
reports this as ETD (End Trade Drawdown) = $90.00 = MFE $50 + loss $40.

**Seat 4 — a WINNING trade that still cost headroom.**

```
entry 29294 -> target 29305.25                   realised +22.50, MFE +30.50
peak ratchets 100000.00 -> 100030.50
floor          98000.00 ->  98030.50
equity after the win                            100022.50
headroom = 100022.50 - 98030.50 =                 1992.00   <- matches the log
```

Up $22.50 on the trade, down $8.00 of headroom, because the floor rose $30.50 and
$8.00 of that was given back before the exit.

So: **a seat's headroom is not its P&L.** It falls on give-back as well as on
loss, and it only rises when equity makes a genuinely new high above the previous
peak. Reconstructing equity from headroom without accounting for the ratchet will
mislead you — assume `floor = start - dd` and every seat that has ever been in
profit will look worse than it is.

Until a seat's floor freezes (`peak >= start + dd + 100`), maximising MFE
give-back is the fastest way to burn a container without losing a single trade.

## Shutting down with open positions

Disabling the strategies while a seat is `InPosition` leaves the position and its
protective stop live at the broker (`CancelExitsOnStrategyDisable=False`), and
the router refuses to release the seat.

The next startup will then **refuse** that seat, because the preflight requires a
flat account with no live orders of ours. Flatten and cancel manually before
restarting, or that seat stays out of the book and the quorum never completes —
which stops the whole book.
