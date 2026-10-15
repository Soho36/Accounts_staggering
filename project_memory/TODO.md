# TODO

Concrete planned work only. Delete completed and dead items rather than
archiving them here.

---

## Blocking the next live session

### 1. GG rollout (routed GG on its own router)
Generated, compiled and router-tested 2026-10-15; nothing deployed yet.
- [ ] Choose the GG accounts and fill `BOOKS["GG"]`, plus `BOOKS["GG_SIM"]` with
      NEW simulation accounts, in `nt8/make_peak_file.py`. Size K from DECISIONS
      2026-10-15: K=4 at R:R 1.0, K=5 above it.
- [ ] Copy `PropRouterGG.cs` to `bin\Custom\AddOns\` and the routed GG to
      `bin\Custom\Strategies\`; compile in the NinjaScript Editor.
- [ ] `python make_peak_file.py --book GG_SIM --install`, full NT8 restart;
      confirm every GG seat registers and seeds, and that an account also listed
      in an RTL peak file is refused with "is an RTL router seat".
- [ ] Run the release gate (`nt8/README.md`) on the GG simulation book, then a
      live GG book.

**Acceptance:** a full Playback session where GG routes green signals across its
own seats, RTL is unaffected, and `python nt8/gg_sync.py` reports no drift.

### 2. Playback-test the exact-id orphan exception
The source now applies the same narrow rule at startup and runtime.
- [ ] Test an acknowledged old `Unknown` orphan plus a stranded `Pending` and
      `InPosition` status; confirm the reservation self-heals.
- [ ] Confirm a different/new `Unknown` id, a non-`Unknown` active order, and a
      non-flat position each still preserve the reservation.

**Acceptance:** only flat + `Unknown` + exact acknowledged id is ignored.

### 3. Validate deterministic end-of-session ownership
On 27 August account Auto Close fired at 23:59, before the strategy's fixed
23:59:30 session close for a midnight template. The strategy is now compiled with
a 120-second lead (23:58), while account Auto Close remains 23:59.
- [ ] Confirm the strategy cancels/flattens at 23:58 and its callbacks complete
      before account Auto Close.
- [ ] Confirm account Auto Close remains a later backstop and does not submit an
      `External` exit during the normal close.
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

### 5. Playback-test the stop-limit timeouts (zero-band gap mitigation)
Live-release blocker. Since 2026-10-12 a triggered-but-unfilled protective
stop-limit is replaced by a market exit after 60 s, and a triggered-but-unfilled
entry is cancelled after 25 min (`RT30_min_nt8_standalone/STOP_TIMEOUT.md`).
Present in routed RTL and GG; only harness-tested.
- [ ] Gap a Playback session through a protective stop; confirm cancellation,
      the market exit, and that the seat stays busy until flat.
- [ ] Trigger an entry without a fill; confirm cancellation at 25 minutes and
      that the seat is released.

### 6. Complete the Playback release gate
`nt8/README.md` holds the checklist. Not yet run end to end on six distinct
simulation accounts with all 23 windows and `Routed`.

---

## Open operational question

### 7. Withdrawal and replacement plan for frozen seats
Routing remains `max_headroom`, matching the selected `signal_router.py` policy.
`protect_frozen` is an evaluated alternative, not the reference specification;
for R=1/K=6 it performed materially worse in the stored study.
The existing withdrawal result cannot answer the live question by itself:
`account_farming.run_book()` gives every live seat every trade, so growth raises
exposure, while `signal_router.py` keeps R fixed but starts all K homogeneous and
uses payout cash only for replacement up to the original K.

- [ ] Add a routed-farming scenario initialized from the six actual seats: start,
      drawdown, equity, peak/floor, frozen state and availability date.
- [ ] Keep R fixed while allowing K to change when a purchase is made; newly
      acquired seats must enter the same `max_headroom` competition only after
      they become tradable.
- [ ] Model payout constraints explicitly: eligibility days, consistency, payout
      number, minimum/maximum request, approval/removal timing and payout split.
- [ ] Model a cash pot plus time-varying product inventory. Legacy evaluations are
      available intermittently, so neither always-available nor never-available
      is correct; record the actual offer, tier and all-in cost at each purchase.
- [ ] Sweep withdrawal amount/cadence and minimum retained headroom jointly with
      purchase rules. Include the applicable post-withdrawal MAE limit;
      liquidation headroom alone is not a payout-compliance calculation.
- [ ] Compare policies on fixed delivered exposure/P&L and report realized cash,
      safe endpoint withdrawal, live K, purchases, deaths and worst seat-loss
      cluster across chronological/blocked periods.
- [ ] Only after a policy survives that test, turn it into an operator plan and
      verify NetLiquidation/headroom after each real withdrawal before resuming.

**Acceptance:** one reproducible model answers both where each fixed-R signal is
routed and when/how much cash leaves or buys a seat, starting from the real book;
no result relies on increasing R when K grows.

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
