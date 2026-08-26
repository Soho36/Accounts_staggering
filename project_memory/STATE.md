# STATE

*Describes now. Rewrite it; do not append history.*
Last updated: 2026-08-27

## Current status

The six-seat live book (`Book ID = LIVE`) runs the routed strategy on funded Apex
accounts under close observation. The offline study is complete and unchanged.

**Working:**

- Allocation: `max_headroom` with the study's tie-breaks; decisions cached per
  bar so all six instances agree.
- Seeding: `make_peak_file.py` derives and verifies all six peaks from the broker
  statement, reproducing the broker's cushion to the cent for every seat with a
  published threshold.
- Analysis: `routing_report.py` renders a routing log as a self-contained HTML
  session report.
- Router regression suite: 15/15.

**Live book seats** (drawdowns are non-uniform):

| Instance | Account | start | dd | broker rule |
|---:|---|---:|---:|---|
| 1 | PA-APEX-240737-09 | 25000 | 1500 | intraday, frozen |
| 2 | PA-APEX-240737-10 | 25000 | 1500 | intraday, frozen |
| 3 | PA-APEX-240737-12 | 50000 | 2000 | end-of-day |
| 4 | PA-APEX-240737-13 | 50000 | 2000 | end-of-day |
| 5 | PA-APEX-240737-14 | 50000 | 2000 | intraday |
| 6 | PA-APEX-240737-15 | 50000 | 2500 | intraday |

Simulation book (`SIM`) mirrors the live drawdown mix on six distinct sim
accounts at 100000 start.

## Current work

Three fixes are **in source but not yet compiled or validated live**:

1. `InPosition` / `Pending` self-heal from broker state.
2. Fill identified by the order's limit price rather than the fill price.
3. `blocked_orders_<BOOK>.csv` — orphan ids persisted automatically.

**The user must recompile in the NinjaScript Editor before any of these take
effect.** Until then the seats register but re-latch after their first trade.

## Known issues

**Blocking**

- *(needs live confirmation)* Whether the `InPosition` self-heal actually clears
  the latch. Root cause of the latch itself was never isolated — the fix works
  around it by re-deriving status from broker state rather than trusting the
  cached flag. Watch for `⚠️ releasing an unbacked InPosition reservation`; if it
  appears, the latch is still forming.

**Live-release blockers** (also listed in `nt8/README.md`)

- Zero-band protective stop-limit (`stop == limit`) has no tested mitigation for
  a gap through the level. One suspected instance turned out to be a clean fill,
  so this is unproven rather than disproven.
- No independent hard risk governor: the router vetoes new entries but does not
  flatten on a floor breach or cap aggregate open loss.
- Errors are not fail-safe by design (`IgnoreAllErrors`); see `DECISIONS.md`.

**Operational**

- Instance 1 is blocked by an orphan order `id=2870535822` (state `Unknown`,
  26.08.2026 01:03:57) unless that id is present in *Acknowledged orphan order
  IDs* on its chart. It must stay there until startup reports it no longer
  matches. One blocked seat disarms the whole book.
- `peaks_LIVE.csv` is no longer in the repo folder; the live copy in
  `Documents\NinjaTrader 8\PropRouter\` is authoritative.
- `nt8/Broker_statement.csv` has been re-exported with fresh balances; a stale
  hand-edited test value was removed.

## Next steps

1. Recompile in NinjaScript Editor; confirm all six seats register and that a
   completed trade returns its seat to `Free`.
2. Run a full session and render it with `routing_report.py`; confirm no seat
   latches and no `⚠️`/`⛔` lines beyond the known orphan.
3. Decide the `protect_frozen` question: seats 1 and 2 are frozen and
   payout-capable, and `max_headroom` ranks them first, so they take most
   signals. `signal_router.py` has a policy that deliberately does the opposite.
4. Replay live decisions through `signal_router.py`'s allocator and confirm the
   chosen seat matches on every decision.
5. Address the zero-band stop-limit gap before any live-ready claim.
