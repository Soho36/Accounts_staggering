# TODO

Concrete planned work only. Delete completed and dead items rather than
archiving them here.

---

## Blocking the next live session

### 1. Confirm the deployed revision and the seat-status self-heal
The live-flatness, runtime-orphan and shutdown-reconciliation fixes are in source
but have not yet been validated in a newly compiled NinjaTrader session.
- [ ] Confirm NinjaTrader has the current source; rebuild in the NinjaScript
      Editor if necessary.
- [ ] Confirm all six seats register and seed.
- [ ] After the first completed trade, confirm that seat returns to `Free` in the
      next decision's ranking rather than staying `InPosition`.
- [ ] Watch for `⚠️ releasing an unbacked InPosition reservation` — that means the
      latch still forms and the self-heal is masking it.

**Acceptance:** a full session where `eligible` never reaches `0/6` while
accounts are flat.

### 2. Playback-test the exact-id orphan exception
The source now applies the same narrow rule at startup and runtime.
- [ ] Test an acknowledged old `Unknown` orphan plus a stranded `Pending` and
      `InPosition` status; confirm the reservation self-heals.
- [ ] Confirm a different/new `Unknown` id, a non-`Unknown` active order, and a
      non-flat position each still preserve the reservation.

**Acceptance:** only flat + `Unknown` + exact acknowledged id is ignored.

### 3. Make end-of-session ownership deterministic
On 27 August account Auto Close fired at 23:59, before the strategy's fixed
23:59:30 session close for a midnight template. It submitted external market
exits and disabled all six strategies while callbacks were still arriving.
- [ ] In Playback, choose and test a strategy session-close lead time that exits
      before account Auto Close with enough order/callback margin.
- [ ] Keep account Auto Close as the final backstop unless broker rules prevent
      that ordering.
- [ ] Confirm completed external flattening releases seats cleanly; if termination
      precedes the fill, confirm the new warning is truthful and the operator
      restart procedure is followed.

**Acceptance:** strategy-owned cancellation/flatten completes first, all six
leases release, and account Auto Close remains a later emergency backstop.

---

## Verification debt

### 4. Replay live decisions through `signal_router.py`
The claim "live reproduces the study" is untested end to end.
- [ ] Feed `routing_LIVE_*.csv` seat states into the Python allocator.
- [ ] Confirm the chosen seat matches on every decision.
- [ ] Any divergence is a bug in one of the two.

### 5. Zero-band stop-limit gap
Live-release blocker. `stop == limit` may not fill on a gap through the level.
- [ ] Gap a Playback session through the stop price.
- [ ] Document whether it fills, and by how much it slips if it does.
- [ ] If it fails, design a mitigation that does not change fill behaviour on the
      entry side.

### 6. Complete the Playback release gate
`nt8/README.md` holds the checklist. Not yet run end to end on six distinct
simulation accounts with all 23 windows and `Routed`.

---

## Open policy question

### 7. `max_headroom` vs `protect_frozen` for frozen seats
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
