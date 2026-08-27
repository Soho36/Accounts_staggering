# STATE

*Describes now. Rewrite it; do not append history.*
Last updated: 2026-08-28

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
- Router regression suite: 16/16.
- Python regression suite: 24/24.

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

The 27 August live session ran smoothly overall and confirmed routing/order flow,
but exposed stale `InPosition` status after exits and an account-level Auto Close
race at 23:59. Current source now:

1. derives flatness live from both `Position` and `PositionAccount` on every
   status derivation instead of trusting the cached callback flag;
2. applies the exact same flat + `Unknown` + exact-id orphan acknowledgement at
   startup and during runtime self-heal;
3. reports blocked-order persistence failure truthfully (empty path plus an
   explicit copy-the-id warning); and
4. reconciles shutdown from account position/order truth when NinjaTrader flattens
   externally, without changing any entry, stop or profit-taking order rule.

The router suite is green, but NinjaTrader must compile this revision and the
next live/Playback session must confirm the lifecycle changes.

## Known issues

**Blocking**

- *(needs live confirmation)* Whether live flatness derivation clears every stale
  `InPosition` / `Pending` latch. Watch for `⚠️ releasing an unbacked ...
  reservation`; that means a callback latch still formed but was recovered.
- *(needs timing decision)* NinjaTrader account Auto Close is configured for
  23:59 while the fixed strategy session-close is 30 seconds before a midnight
  session end (23:59:30). Auto Close therefore wins, sends `External` market
  exits and disables all strategies during callback reconciliation. Source now
  handles a completed external flatten cleanly, but deterministic ownership
  requires the strategy close to be scheduled earlier than Auto Close with a
  tested margin. Changing that time changes end-of-day P&L and is not automatic.

**Live-release blockers** (also listed in `nt8/README.md`)

- Zero-band protective stop-limit (`stop == limit`) has no tested mitigation for
  a gap through the level. One suspected instance turned out to be a clean fill,
  so this is unproven rather than disproven.
- No independent hard risk governor: the router vetoes new entries but does not
  flatten on a floor breach or cap aggregate open loss.
- Errors are not fail-safe by design (`IgnoreAllErrors`); see `DECISIONS.md`.

**Operational**

- Startup on 27 August reported orphan `id=2870535822` no longer matches any
  order. It can be removed from Instance 1's *Acknowledged orphan order IDs* field.
- `peaks_LIVE.csv` is no longer in the repo folder; the live copy in
  `Documents\NinjaTrader 8\PropRouter\` is authoritative.
- `nt8/Broker_statement.csv` has been re-exported with fresh balances; a stale
  hand-edited test value was removed.
- On this host the checked-in-path `venv` launcher currently points to a missing
  Python 3.9 installation; system Python 3.12 lacks NumPy/Pandas. The recorded
  24/24 Python result is the last known result, not a rerun from 28 August. The
  independent C# router suite is unaffected.

## Next steps

1. Recompile in NinjaScript Editor. Confirm all six seats register and a completed
   trade returns its seat to `Free` without the acknowledged orphan blocking
   runtime self-heal.
2. Playback-test end of session and choose an ordering that makes the strategy
   session close complete before account Auto Close; then repeat one live close.
3. Run a full session and render it with `routing_report.py`; confirm no seat
   latches and no unexplained `⚠️`/`⛔` lines.
4. Decide the `protect_frozen` question: seats 1 and 2 are frozen and
   payout-capable, and `max_headroom` ranks them first, so they take most
   signals. `signal_router.py` has a policy that deliberately does the opposite.
5. Replay live decisions through `signal_router.py`'s allocator and confirm the
   chosen seat matches on every decision.
6. Address the zero-band stop-limit gap before any live-ready claim.
