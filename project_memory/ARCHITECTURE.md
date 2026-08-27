# ARCHITECTURE

Two halves that must agree: an offline **study** in Python, and a live
**implementation** in NinjaTrader 8. The study defines the allocation model; the
NT8 side reproduces it against real accounts.

```
historical exports ─> account_farming.py ─> signal_router.py ─> results/*.md
                          (account model)     (allocation model)

broker statement ──> make_peak_file.py ──> peaks_<BOOK>.csv
                                                │ seeds
                                                v
NT8 charts (6) ──> Strategy instances ──> PropRouter (static) ──> routing_<BOOK>_<date>.csv
                        │                        ^                        │
                        └── orders to broker     └── headroom/status      └─> routing_report.py ─> HTML
```

## Study side (Python, repo root + `addiotional_helpers/`)

| Component | Responsibility |
|---|---|
| `account_farming.py` | prop-account rule model: trailing drawdown, frozen floor, liquidation, payouts. Defines `Rule(dd, frozen_floor)` and the floor formula the whole project depends on. |
| `addiotional_helpers/signal_router.py` | allocation model. Defines the policies (`max_headroom`, `round_robin`, `protect_frozen`, …), the selection key, and the R/K sweep. **Reference implementation for live routing.** |
| `results/signal_routing.md` | generated study output: capacity matrix, `K ≥ 5R`, cashout by R. Regenerate, never hand-edit. |

`signal_router.py` is intentionally a **completed-trade** allocation model. It
does not simulate order placement, pending orders, re-pricing, rejection or
missed fills, and should not be extended to.

## Live side (`nt8/`)

### `PropRouter.cs` — shared allocation state

A `static` class in `bin\Custom\AddOns\`. All NinjaScript runs in one process, so
six strategy instances share it directly — no files, no IPC, no polling.

Responsibilities:
- **Book registry**: seats keyed by Instance ID within a Book ID namespace.
- **Manifest**: the first seat to register fixes expected seats, R and a config
  fingerprint (instrument, BarsPeriod, Trading Hours, Calculate, routing mode,
  quantity, exit offset, frozen offset, equity source, headroom-covers-stop, and
  **all 24 window R:R values**). Any mismatch refuses registration.
- **Leases**: registration returns a GUID that must accompany every later
  seat-specific call, so a dead instance's callbacks cannot mutate a live seat.
- **Headroom model**: floor/headroom/frozen per seat; peak ratchets on published
  equity and is persisted.
- **Allocation**: `need = R - pending`; rank eligible free seats; cache the
  decision by bar timestamp so all six instances read the same answer.
- **Persistence**: `peaks_<BOOK>.csv` (atomic, with `.bak`), decision log
  `routing_<BOOK>_<date>.csv`, and `blocked_orders_<BOOK>.csv`.

### `RR_..._SafeExits_Routed.cs` — the strategy

The original strategy plus routing, lifecycle reconciliation and diagnostics.
Derived from
`RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits(EXAMPLE).cs`
in the repo root, which is the untouched reference.

Core order flow is preserved: latest-red-candle stop-limit entry re-priced **in
place**, candle-low zero-band stop-limit protection and bar-close take-profit
limit. Known deliberate divergences are the gap-skipped-candle setup fix and
emergency exits when no valid protective setup/order can be established; see
`DECISIONS.md` and `nt8/README.md`. One instance runs per chart/account and all
order names carry the Instance ID.

Main integration responsibilities:
1. Startup preflight, orphan acknowledgement and `RegisterSeat()` / `ReleaseSeat()`.
2. Equity/status publication on account, market-data, order, execution, position
   and bar callbacks.
3. `MayEnter()` — the routing gate, placed **after** local disqualifiers (window,
   R:R, gap) so a seat that would refuse anyway never consumes a slot.
4. Per-order entry-setup binding and fill resolution using the execution order's
   reported limit/stop price, including price-improved fills.
5. Broker-state reconciliation for stranded `Pending` / `InPosition` states.

### Seat state machine

```
Free ──wins claim──> Pending ──fills──> InPosition ──closes──> Free
```

| State | Counts against R | Can win |
|---|---|---|
| `Free` | no | yes |
| `Pending` (working entry order) | **yes** | no |
| `InPosition` (filled) | **no** | no |

`InPosition` not consuming R is why overlapping signals produce more than R open
contracts, and why `K > R`. Detail in `nt8/ROUTER_LOGIC.md`.

Both non-`Free` states **self-heal**: on bar callbacks, if broker and strategy
state prove flat with no live order of ours, the seat is released after at least
30 seconds. The wall-clock grace still needs a later bar callback, so on a
30-minute series recovery can take roughly 30–60 minutes. This is necessary
because both states previously became one-way latches that silently disarmed the
whole book. The runtime order scan uses the same narrow orphan exception as
startup: flat position, `Unknown` state and exact acknowledged order id.

## Tooling (`nt8/`, run locally, never inside NinjaTrader)

| Tool | Purpose |
|---|---|
| `make_peak_file.py` | builds/verifies `peaks_<BOOK>.csv` from a broker statement; also `--seats sim` for untraded simulation accounts, `--check`, `--install`, `--report` |
| `routing_report.py` | renders a routing log as a self-contained HTML session report (inline SVG, no server, no network) |
| `tests/run-router-tests.ps1` | compiles the real `PropRouter.cs` against stubs and runs 16 regression cases |

## State files — `Documents\NinjaTrader 8\PropRouter\`

| File | Written by | Purpose |
|---|---|---|
| `peaks_<BOOK>.csv` | seeded by tool, maintained by router | high-water marks; the router only selects seats seeded from here |
| `routing_<BOOK>_<yyyymmdd>.csv` | router | one row per seat per decision — the analysis record |
| `blocked_orders_<BOOK>.csv` | router | best-effort audit of ids that blocked startup, so they can survive the Output window; disk failure never weakens the interlock |

The Book ID is the filename stem of the peak file. A different Book ID looks for
a different file, finds nothing, and every seat reports `PEAK NOT SEEDED`.

## Architectural constraints

- The static registry requires all seats in **one NT8 process**. Separate
  platform logins would need a file-based transport instead.
- Manifest, leases, decision cache and `trades_taken` are in memory and are lost
  on an NT8 restart. Disabling/re-enabling a strategy does **not** reset them.
- A changed peak file needs a **full NT8 restart** — the load latches per book
  per process.
