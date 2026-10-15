# TESTING

How to tell whether a change actually works. Nothing here can be assumed from the
code; most of it exists because a check was missing when something broke.

---

## Python regression suite

Run after changes to the study, peak helper or their tests:

```powershell
& .\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Current expected result: `Ran 43 tests` and `OK` (2026-10-15).

Use the project venv (see `I:\PycharmProjects\AGENTS.md`); system Python lacks
NumPy/Pandas. The workspace is `I:\PycharmProjects\Accounts_staggering\`; the
`C:\Users\Liikurserv\PycharmProjects\...` path is a junction to the same files.

---

## Router regression suite

Compiles the **real** router against a minimal NinjaTrader stub and runs its
regression cases. Fast; run both after any change to `PropRouter.cs`, since GG is
generated from it.

```powershell
cd nt8\tests
.\run-router-tests.ps1              # RTL: Result: 16 passed, 0 failed
.\run-router-tests.ps1 -Router GG   # GG: 16 passed, then 7 passed (cross-router guard)
```

Uses Visual Studio's `csc.exe` when installed, otherwise NinjaTrader's bundled
Roslyn (Visual Studio Build Tools were gone as of 2026-10-15). An antivirus can
hold a freshly built test executable; if the first run prints no results, rerun.

Covers: durable peak-row preservation, strict CSV parsing, manifest/quorum
readiness, atomic pending reservations, delayed-winner revalidation, transient
peak capture, lease ownership, fail-closed invalid state.

**It does not emulate NinjaTrader's managed-order engine.** Entry re-pricing,
fills, rejections and position lifecycle are *not* covered — those need Playback.

---

## Compiling NinjaScript without the NinjaScript Editor

NinjaTrader cannot be driven from the agent environment, but its compiler can:

```powershell
nt8\tests\compile-nt8.ps1
```

A real compile, not a syntax filter: NinjaTrader's bundled Roslyn against the
installed NT8 assemblies in `C:\Program Files\NinjaTrader 8\bin`. By default it
compiles both routers and both routed strategies **together**, which also proves
RTL and GG can coexist in one Custom folder. Expected:
`Result: 0 error(s), 0 source warning(s)`. Pass `-Sources` for other files.

It does not run the managed-order engine. A syntax check once caught a duplicated
86-line block (`CS0111`) introduced by a text-anchor edit; run this after every
scripted edit to a `.cs` file.

---

## GG generation

```bash
python nt8/gg_sync.py
```

Expected: `Transform self-test ... PASS`, the three mirrored comparisons, and
`up to date` for all three GG files (exit 0). After changing any RTL source run
`python nt8/gg_sync.py --write`, review the diff, then compile and run both router
suites. A `FAIL` self-test means the standalone GG in `RT30_min_nt8_standalone`
no longer equals the RR→GG transform of the standalone RR: GG's definition
changed upstream and needs a human decision.

---

## Peak file verification

Before any live session, confirm the seeds still match the broker:

```bash
cd nt8
python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv   # 0 = matches source, 1 = mismatch
python make_peak_file.py --book SIM --check peaks_SIM.csv
```

Use a freshly exported statement: exit `0` proves equality with the supplied
source and strict schema, not that the statement itself is current. The
generator self-verifies and refuses to write on any disagreement. Known failure
modes it catches, each of which has occurred:

| Symptom | Cause |
|---|---|
| implied drawdown ≠ configured | wrong seat-table dd, or an edited/stale statement |
| frozen threshold ≠ `start + 100` | wrong start balance |
| modelled floor below the EOD floor | would overstate headroom — refused |
| rows split into wrong field count | Excel wrote comma decimals on a non-invariant locale |
| duplicate Instance IDs | two charts would collide; the router requires `1..N` once each |

**Never write the peak file from Excel.** UTF-8 without BOM, LF, period decimals.

---

## Session analysis

```bash
python routing_report.py "…/PropRouter/routing_<BOOK>_<yyyymmdd>.csv"
```

Read the report, not the Output window. Check:

- No seat shows `UNSEEDED`.
- `eligible` never reaches `0/6` while accounts are flat — that is the latch.
- Every fill's printed `Stop=` matches its own candle's `SL`.
- `Δ headroom = Δ equity − Δ floor` holds per seat.

---

## Authority of sources

When a log looks wrong, check in this order. Two false diagnoses came from
skipping this.

1. **NinjaTrader Control Center log** (`Order` / `Execution` rows) — authoritative
   for what the broker actually did: order prices, states, fill prices, times.
2. **`routing_<BOOK>_<date>.csv`** — authoritative for what the router believed:
   per-seat equity, peak, floor, headroom, status, winners, `detail`.
3. **NinjaTrader trade export** — realised P&L, MAE, MFE, ETD.
4. **Output window** — convenience only. Volatile, and it does not reliably
   record terminal exit fills.

Do not reconstruct equity from headroom assuming `floor = start - dd`. That holds
only before a seat has ever been in profit; the ratchet breaks it.

---

## Regression scenarios that have bitten before

Re-test these whenever entry, exit, order-state or routing code changes.

| Scenario | Why |
|---|---|
| **Re-priced entry then fill** | the setup bound to the order must be the *newest* candle; a stale binding caused one emergency exit and one silently wrong stop |
| **Price-improved fill** | a buy stop-limit fills at its limit *or better*; matching by fill price falsely reports "no candle matches" |
| **Trade completes, next signal arrives** | the seat must return to `Free`; both `Pending` and `InPosition` have been one-way latches that silently disarmed the whole book |
| **Restart with a stale `Unknown` order** | must block, must record the id to `blocked_orders_<BOOK>.csv`, must be clearable by listing that id |
| **Acknowledged orphan plus self-heal** | the exact flat `Unknown` id may be ignored during startup and runtime reconciliation; any different id or non-`Unknown` state must still preserve the reservation |
| **Blocked-order audit write fails** | startup must remain blocked and must report that persistence failed, never claim an id was saved when no row exists |
| **Restart while a seat is flat** | peaks must reload identically; a changed peak file needs a **full NT8 restart**, not a strategy re-enable |
| **Mixed chart configuration** | differing window R:R or BarsPeriod must refuse registration — all 24 R:R values are in the manifest fingerprint |
| **Shutdown with an open position** | unregister is refused on purpose; that account must be flattened before the next start or the book never reaches quorum |

---

## Playback release gate

The full checklist lives in `nt8/README.md` and is **not yet complete**. It needs
six *distinct* simulation accounts — six charts on one Sim101 account share
equity and position state and do not emulate independent containers.

Timeframe note: any timeframe works, but all six charts must match (BarsPeriod is
in the manifest) and a change needs a full NT8 restart. Faster bars surface
order-lifecycle bugs sooner but are not comparable to the study, whose risk is
defined by each candle's own high and low.
