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
| `addiotional_helpers/signal_router.py` | allocation model. Defines the policies (`max_headroom`, `round_robin`, `protect_frozen`, …), the selection key, and the R/K sweep. Its **`max_headroom` branch** is the reference implementation for live routing; the other policies are research comparisons. |
| `results/signal_routing.md` | generated study output: capacity matrix, `K ≥ 5R`, cashout by R. Regenerate, never hand-edit. |

`signal_router.py` is intentionally a **completed-trade** allocation model. It
does not simulate order placement, pending orders, re-pricing, rejection or
missed fills, and should not be extended to.

### Relationship between farming, routing and withdrawals

The two Python branches share the same per-seat mechanics: `signal_router.py`
imports `account_farming.py` and calls `new_account()` / `step()` with its `Rule`
and `Harvest` objects. They diverge at book level:

- `account_farming.run_book()` applies each accepted strategy trade to every live
  seat. Buying another seat therefore increases both K and exposure.
- `signal_router.simulate_route()` correctly separates fixed signal exposure R
  from seat count K and advances only the seats selected for each signal, but K
  is fixed at the start; payouts can replace dead seats only up to that original
  K. It does not model staggered growth from the current six-seat live state.

Consequently, a withdrawal rule selected from standalone `account_farming.py`
cannot be declared optimal for the routed book. The next economic model must keep
`signal_router.py`'s fixed-R allocation while adding dynamic K, heterogeneous
live-seat starting states, constrained payout events and time-varying purchase
availability/cost. This is an economic/lifecycle extension, not an order-engine
simulation.

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

### `RR_..._SafeExits_Routed_MaxRedRun.cs` — the RTL strategy

The original strategy plus routing, lifecycle reconciliation and diagnostics.
This build is deployed live, as `...SafeExitsRoutedRedRun.cs`.
`RR_..._SafeExits_Routed.cs` is the earlier routed build without the MaxRedRun
cap and stop timeouts. Both derive from
`RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits(EXAMPLE).cs`
in the repo root, the untouched reference; the current standalone equivalents
live in `I:\PycharmProjects\RT30_min_nt8_standalone\`.

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
6. Stop-limit timeouts: a triggered-but-unfilled protective stop is replaced by a
   market exit after 60 s; a triggered-but-unfilled entry is cancelled after
   25 min (`RT30_min_nt8_standalone/STOP_TIMEOUT.md`).

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

### GG — `PropRouterGG.cs` + `GG_..._Routed_MaxGreenRun.cs` (generated)

A second, independent router and routed strategy for the green-candle signal.
Both are produced by `nt8/gg_sync.py` from the RTL router and routed RTL and are
never edited by hand; `python gg_sync.py` exits 1 on any drift. Why: DECISIONS
2026-10-15.

- Strategy = routed RTL with the RR→GG transform (identity, wording,
  `MaxGreenRun`, every Close/Open comparison mirrored). Entry, stop and target
  rules are otherwise identical: buy above the signal candle's high, stop at its
  low.
- Router = `PropRouter.cs` with GG-suffixed type names, its own state folder
  `PropRouterGG`, and one addition: `Register()` refuses any account listed in an
  RTL peak file.
- RTL and GG are separate static registries in the same NT8 process; a seat's
  router is fixed by the strategy its chart runs.

## Tooling (`nt8/`, run locally, never inside NinjaTrader)

| Tool | Purpose |
|---|---|
| `make_peak_file.py` | builds/verifies `peaks_<BOOK>.csv`. One seat table per Book ID in `BOOKS`; each names its router (the `--install` folder) and source (broker statement, or untraded sim). Enforces one account per book across both routers. `--list-books`, `--check`, `--install`, `--report` |
| `gg_sync.py` | generates and drift-checks the GG router, routed GG and GG regression tests from their RTL sources |
| `routing_report.py` | renders a routing log as a self-contained HTML session report (inline SVG, no server, no network) |
| `tests/compile-nt8.ps1` | real compile of both routers and both routed strategies together, with NinjaTrader's Roslyn against the installed NT8 assemblies |
| `tests/run-router-tests.ps1` | compiles the real router against stubs and runs its regression suite; `-Router GG` adds the cross-router guard tests |

## State files — `Documents\NinjaTrader 8\PropRouter\` (RTL), `PropRouterGG\` (GG)

Both folders have the same layout.

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
- The RTL and GG routers share no state. One account may belong to only one book
  across both; `PropRouterGG` enforces that against RTL peak files and
  `make_peak_file.py` across all seat tables.
