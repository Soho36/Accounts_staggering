# TODO

Concrete planned work only. Delete completed and dead items rather than
archiving them here.

---

## Blocking the next live session

### 1. Recompile and confirm the seat-status self-heal
Three fixes are in source but not compiled (see `STATE.md`).
- [ ] Rebuild in the NinjaScript Editor.
- [ ] Confirm all six seats register and seed.
- [ ] After the first completed trade, confirm that seat returns to `Free` in the
      next decision's ranking rather than staying `InPosition`.
- [ ] Watch for `⚠️ releasing an unbacked InPosition reservation` — that means the
      latch still forms and the self-heal is masking it.

**Acceptance:** a full session where `eligible` never reaches `0/6` while
accounts are flat.

### 2. Isolate the `InPosition` latch root cause
The self-heal works around it; the cause was never found. Candidates: the cached
`strategyPositionFlat` flag never being set true, or `HasActiveExitOrders()`
staying true because a terminal exit order is reported as `Unknown`.
- [ ] Add temporary instrumentation logging `strategyPositionFlat`,
      `HasActiveExitOrders()`, `PositionAccount.MarketPosition` on each publish.
- [ ] Reproduce on Playback and identify which condition fails.

---

## Verification debt

### 3. Replay live decisions through `signal_router.py`
The claim "live reproduces the study" is untested end to end.
- [ ] Feed `routing_LIVE_*.csv` seat states into the Python allocator.
- [ ] Confirm the chosen seat matches on every decision.
- [ ] Any divergence is a bug in one of the two.

### 4. Zero-band stop-limit gap
Live-release blocker. `stop == limit` may not fill on a gap through the level.
- [ ] Gap a Playback session through the stop price.
- [ ] Document whether it fills, and by how much it slips if it does.
- [ ] If it fails, design a mitigation that does not change fill behaviour on the
      entry side.

### 5. Complete the Playback release gate
`nt8/README.md` holds the checklist. Not yet run end to end on six distinct
simulation accounts with all 23 windows and `Routed`.

---

## Open policy question

### 6. `max_headroom` vs `protect_frozen` for frozen seats
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
