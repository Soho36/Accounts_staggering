# TESTING

How to tell whether a change actually works. Nothing here can be assumed from the
code; most of it exists because a check was missing when something broke.

---

## Python regression suite

Run after changes to the study, peak helper or their tests:

```powershell
python -m unittest discover -s tests -v
```

Current expected result: `Ran 24 tests` and `OK`.

---

## Router regression suite

Compiles the **real** `PropRouter.cs` against a minimal NinjaTrader stub and runs
15 cases. Fast; run it after any change to `PropRouter.cs`.

```powershell
cd nt8\tests
.\run-router-tests.ps1
```

Expected: `Result: 15 passed, 0 failed`.

Covers: durable peak-row preservation, strict CSV parsing, manifest/quorum
readiness, atomic pending reservations, delayed-winner revalidation, transient
peak capture, lease ownership, fail-closed invalid state.

**It does not emulate NinjaTrader's managed-order engine.** Entry re-pricing,
fills, rejections and position lifecycle are *not* covered — those need Playback.

---

## Syntax-checking C# without NinjaTrader

NinjaTrader cannot be driven from the agent environment, and the NinjaScript
Editor is the only real compiler. Roslyn still catches syntax and name errors:

```powershell
$csc = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe'
$out = & $csc /nologo /t:library /out:"$env:TEMP\chk.dll" 'nt8\<file>.cs' 2>&1
$all = $out | Where-Object { $_ -match 'error CS' }
$syn = $all | Where-Object { $_ -notmatch 'CS0246|CS0234|CS0518|CS1069|CS0400' }
Write-Output ("total {0}  non-missing-type {1}" -f $all.Count, $syn.Count)
```

**Interpretation:** a few hundred `CS0246`-class errors are expected — those are
the missing NinjaTrader assemblies. What matters is `non-missing-type = 0`.
Anything else is a real error.

This has caught a duplicated 86-line block (`CS0111`) that a text-anchor edit
introduced. Always run it after scripted edits to a `.cs` file.

Also check brace balance after any block edit:

```bash
python -c "import io;s=io.open('nt8/<file>.cs',encoding='utf-8').read();print(s.count('{')==s.count('}'))"
```

---

## Peak file verification

Before any live session, confirm the seeds still match the broker:

```bash
cd nt8
python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv   # 0 = matches source, 1 = mismatch
python make_peak_file.py --seats sim --check peaks_SIM.csv
```

Use a freshly exported statement: exit `0` proves equality with the supplied
source and strict schema, not that the statement itself is current. The
generator self-verifies and refuses to write on any disagreement. Known failure
modes it catches, each of which has occurred:

| Symptom | Cause |
|---|---|
| implied drawdown ≠ configured | wrong `SEAT_CONFIG` dd, or an edited/stale statement |
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
