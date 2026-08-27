# PROJECT

## Objective

Test whether **portfolio architecture** — how trading signals are allocated
across independent prop-account drawdown containers — creates economic value,
without changing the trading strategy itself.

The strategy, its entry rules and its time windows are held fixed. The only
variable is which account carries each signal.

## Two controls, deliberately separate

| Symbol | Meaning |
|---|---|
| **R** | copies requested per signal — actual market exposure |
| **K** | number of paid prop accounts (drawdown containers) available |

`R = 2, K = 10` does not mean 10 contracts. It means two copies of every signal,
spread across ten separate drawdown buffers. Raising R raises leverage; raising K
does not change exposure at all, only where the risk sits.

This separation is the entire point of the project. Comparing two books at the
same R is an exposure-matched comparison, so any difference in cash must come
from the allocation decision rather than from trading more.

## Core requirements

- Keep all 23 entry windows enabled; never discard a signal for its time of day.
- Route each valid signal to the free account with the largest remaining drawdown
  headroom (`max_headroom`).
- Live routing must reproduce `addiotional_helpers/signal_router.py` decisions
  exactly, given the same seat states.
- Nothing may suppress an entry the original strategy would have taken, beyond
  the router claim, the one-time startup interlock and the full-book quorum.

## Key measured facts

- Peak simultaneous open signals on the historical tape: **5**.
  Therefore capacity requires **K ≥ 5R**.
- With six live accounts, the supported exposure is **R = 1** (needs 5, one spare).
- At the `K = 5R` boundary the study's holdout showed `max_headroom` beating
  round-robin on cashout in 18/18 comparisons, with identical fills,
  contract-hours, stop risk and raw P&L. Source: `results/signal_routing.md`.

## Invariants that must not be violated

1. `floor = min(peak - drawdown, start_balance + 100)` where `peak` is the
   high-water mark of **unrealised** equity (NetLiquidation). `peak` only rises.
2. `headroom = equity - floor`. A seat's headroom is **not** its P&L.
3. Selection key is `(-headroom, trades_taken, instance_id)` — identical to
   `signal_router.py`.
4. A seat whose peak was not seeded from a verified source is never selected.
5. Fail closed: if the book is not fully ready, nobody trades.
6. One broker account is one drawdown container, and belongs to exactly one seat.

## Constraints

- Six funded Apex accounts with **non-uniform** drawdowns and two rule types
  (intraday trailing and end-of-day). See `DECISIONS.md`.
- One contract per seat, enforced at startup. Multi-contract partial-fill
  handling is not implemented.
- Live execution is NinjaTrader 8; the strategy is managed-order NinjaScript.
- The study's rule set omits payout calendars, eligibility caps, activation
  delays and the real account-tier menu. All economic figures are conditional on
  that simplification.

## Non-goals

- Optimising the strategy: entries, exits, R:R and time windows are fixed.
- Declaring any hour "bad" or dropping signals by time of day.
- A hard cap on total open contracts across the book. Overlap is expected and is
  the reason `K > R`; a cap would be a separate risk governor.
- Turning `signal_router.py` into an order-lifecycle simulator. It is a
  completed-trade allocation model; NinjaTrader owns the order lifecycle.

## Terminology

| Term | Meaning |
|---|---|
| seat | one prop account participating in a book, identified by Instance ID |
| book | a set of seats competing for the same signal stream, named by Book ID |
| headroom | `equity - floor`; distance to liquidation |
| frozen | the trailing floor has stopped rising; the seat is payout-capable |
| quorum | all expected seats registered, matching and fresh |
| orphan | an order NinjaTrader reports in state `Unknown` and cannot reconcile |

## Status

**Live-observation prototype, not certified live-ready.** The repository cannot
prove which source revision is compiled in NinjaTrader; see `STATE.md` for the
last recorded operator state and `nt8/README.md` for the release gate.
