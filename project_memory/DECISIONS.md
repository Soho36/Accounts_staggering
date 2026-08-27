# DECISIONS

Newest last within each section. Each entry answers: *could a future agent
reasonably change this back because it does not know why we did it?*

---

## Model and exposure

### 2026-08-16 — Separate R (exposure) from K (containers)

**Decision:** Treat copies-per-signal and seat count as independent controls.
**Reason:** "How many accounts should I have?" is unanswerable without stating
the exposure it carries. Eight seats is generous at R=1, short at R=2, and
drastically short at R=4.
**Consequence:** All comparisons are made *within* the same R, so a cash
difference cannot be explained by trading more.

### 2026-08-16 — Capacity boundary is K ≥ 5R

**Decision:** Size the book from the measured peak simultaneous demand of 5.
**Reason:** Measured on the tape, not chosen. Below it, congestion silently drops
copies and picks *which* signals vanish. Source: `results/signal_routing.md`.
**Consequence:** Six live accounts support R = 1. R = 2 would need ten.
**Reconsider if:** the signal set or timeframe changes, which changes the overlap.

### 2026-08-16 — Live pilot uses the mechanical boundary, not a fitted K

**Decision:** Default to `K = 5R` rather than the K that looked best pre-2023.
**Reason:** The fitted extra-seat count did not survive the holdout.

---

## Account model

### 2026-08-17 — Peak tracks UNREALISED equity (NetLiquidation)

**Decision:** `floor = min(peak - dd, start + 100)` with `peak` = high-water mark
of NetLiquidation, matching `account_farming.py` (MAE-first, intraday).
**Reason:** The prop firm's trailing threshold ratchets on unrealised highs.
Using closed balance would understate the floor and overstate headroom — the one
error direction that actively hurts.
**Consequence:** A seat's headroom is **not** its P&L. A *winning* trade can cost
headroom if it gives back part of its MFE, because the floor rose and never falls.
Verified to the cent against a NinjaTrader trade export; worked examples in
`nt8/ROUTER_LOGIC.md`.

### 2026-08-19 — Drawdowns are per-seat and non-uniform

**Decision:** Live seats use 1500 / 1500 / 2000 / 2000 / 2000 / 2500, not a
uniform 2500.
**Reason:** The broker statement proves each one: for an unfrozen intraday seat,
`Auto Liquidate Peak Balance − Auto Liquidate Threshold Value` **is** the
drawdown. I originally assumed 2500 for all 50K accounts and misdiagnosed a
"$500 discrepancy" on account -14 as a data problem; the user corrected it.
**Consequence:** `make_peak_file.py` now enforces that identity and refuses to
write on mismatch. Raw-dollar headroom remains the correct cross-tier comparison.

### 2026-08-19 — Seed peak is the broker's Peak Balance column, used unmodified

**Decision:** `peak = "Auto Liquidate Peak Balance"`.
**Alternatives:** `threshold + dd` (used briefly and wrongly).
**Why rejected:** Equivalent only when the configured dd is correct; it hid the
dd error above. The Peak Balance column is the governing high-water mark directly.

### 2026-08-19 — End-of-day accounts are modelled as intraday

**Decision:** Accounts -12 and -13 are EOD at the broker but modelled intraday.
**Reason:** An intraday peak is always ≥ an EOD peak, so the modelled floor sits
at or **above** the real floor and headroom is understated, never overstated.
The script proves that inequality per row and reports the gap (+12.01, +64.83).
**Reconsider if:** Apex's EOD freeze threshold turns out to differ from
`start + 100`; only matters once those seats clear about +$2,600.

### 2026-08-18 — A seat with an unverified peak is never selected

**Decision:** `PeakSeeded` gates eligibility; peaks bootstrapped from whatever
equity happened to be present at startup do not count. Only seeded rows are
persisted back.
**Reason:** A peak that is too low puts the floor too low, making a damaged seat
look pristine and attracting the next trade. Fail closed instead.

---

## Fidelity to the original strategy

The overriding constraint: **nothing may suppress an entry the original strategy
would have taken.** Fill behaviour must match what `signal_router.py` measured.

### 2026-08-24 — RealtimeErrorHandling stays `IgnoreAllErrors`

**Decision:** Revert the hardened `StopCancelClose`.
**Reason:** `StopCancelClose` flattens a live position at market on *any*
transient rejection, and the zero-band protective stop-limit is exactly the order
most likely to be rejected. The original chose `IgnoreAllErrors` deliberately.
**Consequence accepted:** errors are logged only; a rejected protective stop
leaves an unprotected position. This trades a platform fail-safe for fidelity.

### 2026-08-24 — Entry is re-priced IN PLACE, not cancelled and resubmitted

**Decision:** Revert to the managed API's in-place modification.
**Reason:** Cancel-then-resubmit opens a window with no working entry order, so a
fast move through the entry price is a missed fill the original would have taken.
That changes fill rate, which is the fidelity axis that matters.
**Consequence accepted:** the original's race returns — if the old price fills
before the modification lands, the fill can carry the newer candle's stop. See
the fill-identification decision below, which mitigates it without changing order
flow.

### 2026-08-24 — No strategy-level end-of-day flatten

**Decision:** Removed the 23:57 cutoff entirely; NinjaTrader's
`IsExitOnSessionCloseStrategy` handles it.
**Reason:** The user does not use it, and on coarse bar series it never reliably
fired anyway while still blocking entries.

### 2026-08-24 — No in-session disarming

**Decision:** Removed all runtime `startupBlocked` sites. The only things that may
withhold an entry are the router claim, the one-time startup preflight, and the
book quorum.
**Reason:** Seven separate disarm points suppressed entries the original would
have taken, and made a cash difference unattributable.
**Note:** the preflight flag was renamed `startupInterlocked` so it cannot be
confused with in-session disarming.

### 2026-08-24 — Gap-skipped candles keep the working order's own setup

**Decision:** Keep the routed behaviour, which does **not** overwrite the shared
stop/R:R before the gap check.
**Reason:** The original overwrites them, so an old order filling later is
protected with a *newer* candle's stop — often above the fill, giving negative
risk with no guard. That is a defect, not a spec, and it is inconsistent with the
exported data where every trade's stop belongs to its own entry candle.
**This is a deliberate divergence from the original.** Do not "restore" it.

---

## Routing modes and operation

### 2026-08-18 — Routing modes renamed; none is a dry run

**Decision:** `Off/Shadow/Live` → `Unrouted / UnroutedLogOnly / Routed`, default
`Routed`.
**Reason:** "Shadow" read as "paper trading". It is not — every mode submits real
orders; the mode only controls whether the router may veto. The old default was
the dangerous one; `Routed` fails closed because an unseeded seat trades nothing.
**Consequence:** to trade without real orders, use simulation accounts — one
distinct account per seat.

### 2026-08-18 — There is no live "shadow" step

**Decision:** Abandoned the "run UnroutedLogOnly on the live book first" rollout.
**Reason:** The manifest fingerprint includes all 24 window R:R values, so charts
with different window splits cannot register into one book and produce no
preview. And with identical all-window configs an unrouted mode means six copies
of every signal. There is no configuration where an unrouted mode gives a
meaningful preview on the live book.
**Consequence:** `UnroutedLogOnly` is a Playback/sim diagnostic only. Peak/floor
verification is done offline by `make_peak_file.py --check` instead.

### 2026-08-19 — Book ID namespaces the peak file

**Decision:** `peaks_<BookId>.csv`; Book ID restricted to `[A-Za-z0-9_-]`.
**Reason:** Per-book state keeps a Playback book from writing into the live
book's peaks. The charset restriction makes the filename sanitiser a no-op, so a
Book ID can never silently map to a different filename.

---

## Failure handling

### 2026-08-25 — Stuck `Pending` and `InPosition` self-heal from broker state

**Decision:** Each bar, if a seat is not `Free`, no submission is in flight, both
`PositionAccount` and `Position` report flat, and a scan of `Account.Orders`
finds no live order of ours, release the seat to `Free` after a grace period and
repair the cached flat flag.
**Reason:** Both were **one-way latches**. `Pending` could be stranded by a null
or throwing submission; `InPosition` was cleared only via a cached bool set by a
single callback. On 2026-08-26 all six seats were latched `InPosition` while every
account was flat — `eligible=0/6` — and the book was silently dead from 15:31.
**Consequence:** status is now derived from authoritative broker state, not
trusted from a latch.

### 2026-08-26 — Fills are identified by the ORDER's limit price

**Decision:** Match the filled order to its candle using
`execution.Order.LimitPrice`, not the fill price.
**Reason:** A buy stop-limit fills at its limit **or better**. A price-improved
fill (observed: order 29262.25, filled 29259) matches no candle and triggered a
false "no candle matches" warning. The order carries the latest re-priced value,
so it identifies the candle exactly — and it also resolves the two genuine
stale-binding cases correctly.
**Related:** an explicit setup always overwrites the order's binding, because
in-place re-pricing means the newest candle's stop must replace the old one.
Sticky binding was correct under the abandoned cancel-and-resubmit design and is
exactly wrong now — it produced one emergency exit and one silently wrong stop.

### 2026-08-26 — Orphaned `Unknown` orders: acknowledge by specific id

**Decision:** `Acknowledged orphan order IDs` takes a `;`-separated list of order
ids. The bypass applies only to state `Unknown`, only when the account is flat,
and only to a listed id.
**Alternatives:** a blanket boolean (tried first).
**Why rejected:** a blanket bypass would silently ignore a *future* orphan that
was genuinely live, letting a seat start and duplicate exposure.
**Also:** the router attempts to write the id to `blocked_orders_<BOOK>.csv` the
moment the interlock fires, because the Output window is volatile. A disk failure
must never weaken the interlock. The current write-failure reporting gap is
tracked in `STATE.md` / `TODO.md`. **Never delete `db\NinjaTrader.sqlite` to clear
one stale record** — that destroys order and trade history for every account.
**Leave acknowledged ids in place** until startup reports they no longer match;
the record does not clear on restart, so removing one re-blocks the seat.

---

## Corrections worth remembering

Two "discrepancies" I reported were my own reconstruction errors. Both cost time.

### 2026-08-26 — Headroom reconstructed without the peak ratchet

I flagged two exits as not reconciling with their stop or target. They
reconciled exactly once the floor ratchet was included. Do not reconstruct equity
from headroom assuming `floor = start - dd`; that only holds before a seat has
ever been in profit.

### 2026-08-26 — A "stop that did not fill" that filled exactly

I inferred from headroom that a stop had been missed by 25 points, and suggested
it might be the zero-band stop-limit risk materialising. The Control Center log
showed it filled at its exact price for exactly −$40. **Check the Control Center
log and the routing CSV before diagnosing from the Output window.**
