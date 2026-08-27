# TODO

Concrete planned work only. Delete completed and dead items rather than
archiving them here.

---

## Blocking the next live session

### 1. Confirm the deployed revision and the seat-status self-heal
Three fixes are in source but their deployed/validated status is not recorded
(see `STATE.md`).
- [ ] Confirm NinjaTrader has the current source; rebuild in the NinjaScript
      Editor if necessary.
- [ ] Confirm all six seats register and seed.
- [ ] After the first completed trade, confirm that seat returns to `Free` in the
      next decision's ranking rather than staying `InPosition`.
- [ ] Watch for `⚠️ releasing an unbacked InPosition reservation` — that means the
      latch still forms and the self-heal is masking it.

**Acceptance:** a full session where `eligible` never reaches `0/6` while
accounts are flat.

### 2. Apply exact-id orphan acknowledgement to runtime self-heal
`ValidateStartup()` ignores an acknowledged `Unknown` order when the account is
flat, but `HasLiveOrderOnAccount()` still counts the same order as live. The
known Instance 1 orphan can therefore defeat the new self-heal.
- [ ] In the runtime scan, ignore only state `Unknown`, only when flat, and only
      when that exact order id is acknowledged.
- [ ] Playback-test an acknowledged old orphan plus a stranded `Pending` and
      `InPosition` state.
- [ ] Confirm a different/new `Unknown` id still blocks.

**Acceptance:** startup and runtime apply the same narrow orphan rule, while an
unacknowledged or non-`Unknown` order always preserves the reservation.

### 3. Correct blocked-order persistence reporting
`RecordBlockedOrder()` catches write errors but returns the intended path, so the
startup reason can falsely claim the id was saved.
- [ ] Return an empty result (and print an explicit warning) when persistence
      fails; do not change the fail-closed startup decision.
- [ ] Test with an unwritable/missing state path.

### 4. Isolate the `InPosition` latch root cause
The self-heal works around it; the cause was never found. Candidates: the cached
`strategyPositionFlat` flag never being set true, or `HasActiveExitOrders()`
staying true because a terminal exit order is reported as `Unknown`.
- [ ] Add temporary instrumentation logging `strategyPositionFlat`,
      `HasActiveExitOrders()`, `PositionAccount.MarketPosition` on each publish.
- [ ] Reproduce on Playback and identify which condition fails.

---

## Verification debt

### 5. Replay live decisions through `signal_router.py`
The claim "live reproduces the study" is untested end to end.
- [ ] Feed `routing_LIVE_*.csv` seat states into the Python allocator.
- [ ] Confirm the chosen seat matches on every decision.
- [ ] Any divergence is a bug in one of the two.

### 6. Zero-band stop-limit gap
Live-release blocker. `stop == limit` may not fill on a gap through the level.
- [ ] Gap a Playback session through the stop price.
- [ ] Document whether it fills, and by how much it slips if it does.
- [ ] If it fails, design a mitigation that does not change fill behaviour on the
      entry side.

### 7. Complete the Playback release gate
`nt8/README.md` holds the checklist. Not yet run end to end on six distinct
simulation accounts with all 23 windows and `Routed`.

---

## Open policy question

### 8. `max_headroom` vs `protect_frozen` for frozen seats
Seats 1 and 2 are frozen and payout-capable. Because a frozen floor lets headroom
exceed the drawdown size, `max_headroom` ranks them first and routes to them most
often. `signal_router.py` has a `protect_frozen` policy that deliberately does the
opposite, treating a frozen seat as an asset to shelter.
- [ ] Decide which the live book should use.
- [ ] If `protect_frozen`, the policy must be selectable and recorded in the
      manifest fingerprint.

---

## Deferred / not planned

- Multi-contract support. The startup preflight enforces exactly one contract;
  partial-fill handling is not implemented and is not currently needed.
- A hard cap on total open contracts. Explicitly a non-goal (`PROJECT.md`); would
  be a separate risk governor, not part of the router.
- A server-backed control dashboard for `make_peak_file.py`. Rejected: it would
  create a web write-path into live drawdown state to save three commands. The
  read-only `routing_report.py` covers the actual need.
- Extending `signal_router.py` into an order-lifecycle simulator.
