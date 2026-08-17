# NT8 max-headroom signal router

> **SIMULATION / PLAYBACK ONLY — NOT LIVE-READY**
>
> These scripts are a prototype translation of the Account_staggering study into
> NinjaTrader 8. Use them only with Playback and separately named simulation
> accounts. Do not attach either unrouted mode to a live account: neither is a
> dry run. Do not promote this version to live trading until every release-gate
> test below passes and the unresolved exit and allocation risks are either fixed
> or explicitly accepted.

Start with the step-by-step [first-start operator manual](FIRST_START.md),
especially before creating or editing a peak file.

## Files and scope

| File | NinjaTrader location |
|---|---|
| `PropRouter.cs` | `Documents\NinjaTrader 8\bin\Custom\AddOns\` |
| `RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits_Routed.cs` | `Documents\NinjaTrader 8\bin\Custom\Strategies\` |
| `FIRST_START.md` | operator checklist; read before creating the book or peak file |
| `tests\*` | local regression harness only; do not copy into NinjaTrader |

The routed strategy is derived from
`RR_m_w_r_stoplimits_and_tplimit_InstanceID_WindowRR_Offsets_SafeExits(EXAMPLE).cs`,
but it is **not byte-identical** to that file. Its core trading rules are
intentionally preserved: latest-red-candle stop-limit entry re-priced **in place**,
candle-low zero-band stop-limit loss exit, window R:R, and bar-close-triggered
take-profit limit. `RealtimeErrorHandling` remains `IgnoreAllErrors` and the
strategy-level end-of-day cutoff has been removed in favour of NinjaTrader's
built-in session close.

Fill behaviour must match what `signal_router.py` measured, so nothing may
suppress an entry the original would have taken. The only additions that can
withhold an entry are the router claim itself, the one-time startup preflight,
and the full-book quorum — all of which exist to keep live allocation faithful to
the study rather than to add a risk policy. Routing, state publication and
logging add plumbing around the rules but not new entry conditions.
Regression-test the complete order lifecycle before relying on the variant.

The self-contained router regression suite compiles the real `PropRouter.cs`
against a minimal `Globals.UserDataDir` stub:

```powershell
& .\tests\run-router-tests.ps1
```

The current suite has 15 cases covering durable row preservation, strict CSV
parsing, manifest/quorum readiness, atomic pending reservations, delayed-winner
revalidation, transient peak capture, ownership/leases and fail-closed invalid
state. It does **not** emulate NinjaTrader's managed-order engine; the Playback
checklist remains mandatory for the strategy and broker lifecycle.

## Model and exposure

The core max-headroom ranking is:

```text
floor    = min(peak - dd, start + 100)
headroom = equity - floor
need     = max(0, R - seats with a pending entry reservation/order)
select   = sorted(eligible_free_seats,
                  key=(-headroom, trades_taken, instance_id))[:need]
```

`peak` and `equity` use NetLiquidation when **Track peak on unrealized** is
`true`, and CashValue when it is `false`.

`R` counts selected **seats**, not arbitrary contract quantity. This version
has a startup interlock that requires every seat to submit exactly one contract,
so `R=1` means at most one new one-contract seat is selected for a decision.
Positions from older signals are busy and excluded from the next allocation;
they do not count against `R`. Consequently, overlapping signals can still
produce more than `R` total open contracts across the book.

The study found `K >= 5R` as its observed full-capacity boundary. For this
six-seat pilot, `K=6` and `R=1` satisfy that study boundary, but this is not a
guarantee of live fills, survival or profitability.

### Full-book quorum and eligibility

Routed mode fails closed unless all of the following are true:

1. The book manifest exists and every registered instance agrees on
   **Expected seats**, `R`, and the configuration fingerprint.
2. Exactly `ExpectedSeats` seats are registered, with every Instance ID from
   `1` through `ExpectedSeats` present once.
3. Every seat is connected, seeded, has valid equity and peak data, and has both
   equity and status updates no more than 20 seconds old.
4. Peak persistence is healthy.

Only after the full book is ready can a seat be eligible. An eligible winner
must also be `Free`, have positive headroom, and—when **Require headroom covers
stop** is enabled—have headroom at least equal to the candidate candle's nominal
initial stop risk.

A granted or working entry remains `Pending` and reserved in the `R` count. A filled position remains
registered and must stay fresh, but is invisible to winner selection until it is
flat. If any one of the six seats disconnects, becomes stale, loses a valid seed,
or makes persistence unhealthy, the entire book refuses new routed entries.
Existing broker orders and positions are not automatically cancelled by that
router refusal.

Market-data events publish a heartbeat at most every five seconds, and bar/order
events also publish state. Playback must therefore be running and delivering
market data. A quiet or paused feed can intentionally make the book stale and
fail closed.

### Book manifest, decision key and leases

The first registered seat establishes an in-memory manifest containing:

- expected seat count and `R`;
- instrument;
- BarsPeriod;
- Trading Hours template;
- `Calculate` mode;
- routing mode (so `Routed` and `UnroutedLogOnly` cannot share a manifest);
- one-contract quantity;
- exit offset;
- frozen-floor offset;
- NetLiquidation versus CashValue equity source;
- the headroom-covers-stop setting;
- all 24 window R:R values.

A mismatch refuses registration. The manifest does not replace operator review:
all six seats must also use the intended account-specific start balance and
drawdown. A Book ID must contain only ASCII
letters, digits, hyphens or underscores and must identify one exact signal stream.
Never reuse a Book ID for another instrument, timeframe, Trading Hours template,
strategy variant, or simulation/live environment.

Decisions are cached inside a book by the full `Time[0].Ticks` bar timestamp.
The first claimant computes the winners; later claimants must present the same
`R`, required headroom and topology. All six series must therefore produce the
same bar timestamps, not merely look like the same timeframe.

Registration returns a private lease. Every later seat-specific router call must
carry that lease, so callbacks from an old strategy instance cannot mutate a new
owner. Duplicate Instance IDs and duplicate normalized account names are
refused. Account-name ownership is global across router books because one broker
account is one shared drawdown container. A stale `Free` owner can be replaced
inside its existing book; a `Pending` or `InPosition` reservation is not stolen
even when its heartbeat is stale.

The manifest, leases and decision cache are in memory. A full NT8 process restart
clears them. `trades_taken` is also in memory and resets on restart.
Disabling and re-enabling a strategy does not reset the static book. After
changing a loaded seed file or manifest field, fully restart NT8 while every
account is flat and has no working orders.

## Intended six-seat settings

These are the real-account parameters used to translate the study. They are
reference data only while this version remains simulation/playback-only.

| Account | Instance ID | Start balance | Drawdown | Tier |
|---|---:|---:|---:|---|
| PA-APEX-240737-09 | 1 | 25000 | 1500 | 25K |
| PA-APEX-240737-10 | 2 | 25000 | 1500 | 25K |
| PA-APEX-240737-12 | 3 | 50000 | 2500 | 50K |
| PA-APEX-240737-13 | 4 | 50000 | 2500 | 50K |
| PA-APEX-240737-14 | 5 | 50000 | 2500 | 50K |
| PA-APEX-240737-15 | 6 | 50000 | 2500 | 50K |

For Playback, create **six distinct simulation accounts**, one per seat. Do not
use six charts all assigned to Sim101. Shared Sim101 charts share account equity
and account-level position state, so they do not emulate six independent
drawdown containers. Give the simulation accounts stable, unique names and use
those names in the Playback seed file.

Common settings for all six Playback instances:

| Property | Value |
|---|---|
| Account | one distinct simulation account per seat |
| Routing Mode | `Routed` for allocation tests |
| Book ID | a simulation-only ID, for example `PLAYBACK_ROUTER_V2` |
| R — copies per signal | `1` |
| Expected seats in book | `6` |
| Entry quantity | exactly `1` contract |
| Exit Offset | identical on every seat; `0` for study-compatible tests |
| Frozen floor offset | `100` |
| Track peak on unrealized | `true` for this six-seat model |
| Require headroom covers stop | `false` to match the study; test `true` separately |
| Instrument, BarsPeriod, Trading Hours | identical |
| Window R:R values | identical |

The code enforces one contract even in an unrouted mode. Either set the strategy
default quantity to one, or enable **Use Custom Quantity** and set **Custom
Quantity** to one.

## Window R:R and NinjaTrader time

There is no separate operator-visible window switch. The hidden legacy
`W00..W23` values are overwritten from the R:R fields at DataLoaded:

- R:R greater than zero enables that hour and supplies its target multiple.
- R:R equal to zero disables that hour.
- All R:R values default to zero, so a newly added strategy does not trade.

The study's canonical signal universe is hours 01 through 23:

| Field | Value |
|---|---|
| `00:00-01:00` | `0` — outside the recovered study universe |
| `01:00-02:00` through `23:00-00:00` | `1` — 23 windows at global R:R 1 |

`GetWindowRiskReward()` indexes `Time[0].Hour`. That hour is based on
NinjaTrader's **global application time-zone setting**, not an independent chart
time zone. Set and record the NT8 global time zone before building the charts.
Then explicitly map each MT5 export hour into that global NT8 time basis,
including daylight-saving transitions. Do not copy the 01–23 labels until a
Playback signal-by-signal comparison proves the mapping.

The Trading Hours template controls the session and which bars exist. It is part
of the manifest. The global time zone is not in the manifest, so it remains a
manual release check.

The explicit end-of-day branch runs only on an OnBarClose callback whose
timestamp is at or after 23:57 and still before midnight. Once reached, it
requests flatten/cancel and blocks later entries that date. A series with no
23:57–23:59 bar can still miss that explicit branch; the built-in session-close
exit is a separate backstop. Test the actual BarsPeriod, Trading Hours template
and global time zone in Playback.

The following core order semantics are intentional and match the original
strategy design:

1. A valid red candle submits a buy stop-limit at that candle's high. Its low and
   the active hour's R:R stay attached to that order setup.
2. If another valid red candle closes before entry, the strategy requests
   cancellation of the old order. It submits the newest red-candle order only
   after the broker reports the old order `Cancelled`.
3. If the old order fills before cancellation is confirmed, the queued
   replacement is discarded. That fill keeps the old candle's stop and R:R; it
   can never inherit the newer candle's risk values.
4. After entry, the loss exit remains the original sell stop-limit with equal
   stop and limit at the signal candle's low.
5. The take-profit remains bar-close-driven. It is **not** a resting target
   placed at entry. Only after a completed bar closes at or above the target does
   the strategy submit a sell limit at `Close + ExitOffset`. An intrabar target
   touch followed by a close below target does nothing.

Cancel acknowledgement creates a real broker-time gap. If price reaches or
passes the queued replacement stop before the old cancellation is confirmed,
the replacement is skipped rather than submitting an already-triggered stop.
This preserves order/risk ownership; test both the cancel-first and fill-first
races in Playback.

## Routing modes — none is a dry run

Every mode can submit orders to the account assigned to the strategy. With a
simulation account those are simulated orders; with a live account they are
live orders.

| Mode | Router behavior | Entry behavior | Router CSV |
|---|---|---|---|
| `Routed` | manifest, lease, quorum and allocation enforced | only selected seats submit; failures stand down | yes, on the first claim for a new decision |
| `UnroutedLogOnly` | registers when possible and prints a non-mutating preview | every locally valid signal still submits, even if registration/preview is unavailable | no |
| `Unrouted` | no router registration | every locally valid signal submits | no |

`UnroutedLogOnly` previews appear only in the NinjaTrader Output window. They
do not share/cache a routed decision, increment `trades_taken`, or create
`routing_*.csv`. “Log only” describes the router's observation; it does not
describe order submission.

The startup preflight interlock applies to every mode. However, after preflight
passes, a router registration or persistence failure blocks `Routed` but does
**not** stop `UnroutedLogOnly` from submitting entries. This is why no unrouted
mode belongs on a live account.

Never combine all 23 enabled windows with an unrouted mode across six accounts:
that requests six one-contract copies of every locally valid signal rather than
`R=1`.

## EOD accounts and conservative modelling

`-12` and `-13` have no published intraday Auto-Liquidate Threshold because
they are end-of-day trailing accounts. This model nevertheless sets **Track peak
on unrealized = true** on all six seats.

Under the assumptions that the observed NetLiquidation high-water mark is at
least the governing EOD closed-balance high, and that the firm's freeze rule is
represented correctly, this is conservative:

```text
peak_modelled >= peak_EOD
floor_modelled >= floor_real
headroom_modelled <= headroom_real
```

That can route to the two EOD seats less often. It should not be described as
risk-free: the exact broker definitions, update timing and freeze behavior must
be reconciled against statements and liquidation thresholds. In particular, it
remains unverified whether the EOD threshold freezes at `start + 100`. That
matters once those accounts clear roughly +$2,600.

## Peak seeds

An unseeded seat can publish state but cannot satisfy the full-book quorum.
Current equity is never allowed to bootstrap a trusted peak.

Reference values from the broker sheet:

| Seat | Source | Peak | Floor | Headroom |
|---|---|---:|---:|---:|
| `-09` | threshold + dd | 26600.00 | 25100.00 | 2993.18 |
| `-10` | threshold + dd | 26600.00 | 25100.00 | 2570.70 |
| `-12` | sheet **Peak** column (EOD) | 51228.65 | 48728.65 | 1159.71 |
| `-13` | sheet **Peak** column (EOD) | 50480.45 | 47980.45 | 2235.89 |
| `-14` | threshold + dd | 50838.31 | 48338.31 | 1526.87 |
| `-15` | threshold + dd | 50844.85 | 48344.85 | 2319.43 |

All four accounts with a published threshold reproduce the broker cushion to the
cent. For an intraday seat, do not substitute the sheet's closed-balance Peak:
`-14` would understate its governing peak and floor by $500. For the EOD seats,
the closed-balance peak is the intended source.

The current max-headroom ordering is:

```text
1. -09  2993.18      4. -13  2235.89
2. -10  2570.70      5. -14  1526.87
3. -15  2319.43      6. -12  1159.71
```

Max-headroom is not a fairness policy. A thinner seat can legitimately receive
no allocations while a higher-headroom seat remains free. “No starvation” is
therefore not a valid acceptance criterion.

### Strict seed-file contract

The file is:

```text
Documents\NinjaTrader 8\PropRouter\peaks_<book>.csv
```

For the future live book, the reference file is:

```csv
account,start_balance,drawdown,peak,updated_utc
PA-APEX-240737-09,25000.00,1500.00,26600.00,2026-08-17T00:00:00Z
PA-APEX-240737-10,25000.00,1500.00,26600.00,2026-08-17T00:00:00Z
PA-APEX-240737-12,50000.00,2500.00,51228.65,2026-08-17T00:00:00Z
PA-APEX-240737-13,50000.00,2500.00,50480.45,2026-08-17T00:00:00Z
PA-APEX-240737-14,50000.00,2500.00,50838.31,2026-08-17T00:00:00Z
PA-APEX-240737-15,50000.00,2500.00,50844.85,2026-08-17T00:00:00Z
```

Playback needs the same shape but must use the six distinct simulation-account
names and the corresponding configured start balances and drawdowns.

Parsing is deliberately strict and fail-closed:

- The header must be exactly
  `account,start_balance,drawdown,peak,updated_utc`. A UTF-8 BOM is accepted.
- Every data row must contain exactly five comma-separated fields. Blank lines,
  quoted commas and extra columns are not accepted.
- `start_balance`, `drawdown` and `peak` must use invariant-culture periods,
  be finite, and be positive. Peak must be at least start balance.
- `updated_utc` must be exactly `yyyy-MM-ddTHH:mm:ssZ`.
- Normalized account names must be unique. Normalization trims the name and
  ignores everything from the first `!` onward.
- The seed row's start balance and drawdown must exactly equal that seat's
  configured values. They are validated, not merely informational.

One malformed or duplicate row makes persistence unhealthy for the whole book
and registration fails. A missing file is not a parse error, but it leaves every
seat unseeded, so Routed cannot reach quorum. The file is read once when the
book first registers. After correcting or replacing it, perform a full NT8
process restart before retrying.

Do not edit the file in Excel on a comma-decimal locale. Use a plain-text editor,
keep periods as decimal separators, disable every strategy first, and retain a
separate operator backup.

### Atomic merged persistence and backup

The router loads all durable rows into a book-level map independent of the
currently registered seats. On a new high for a seeded seat, it updates that one
record and writes the **complete merged map**, so stopping a strategy does not
delete its seed.

Account-item callback values feed a monotonic `ObservePeak` path, preserving a
transient high even if current equity has already moved lower. Current headroom
is published separately from serialized `Account.Get` snapshots, so a delayed
callback cannot overwrite current equity while its high is still retained.

Writes go to a uniquely named temporary file with write-through enabled, then
replace the primary atomically. The previous primary is retained as:

```text
peaks_<book>.csv.bak
```

On first creation, the router also creates a `.bak` copy. The backup is not
loaded automatically; recovery is an operator action performed with all
strategies disabled, followed by a full NT8 restart.

Any durable peak write failure latches persistence unhealthy and makes the
Routed book refuse new allocations. It does not flatten existing positions or
cancel existing broker orders. Verify both row count and timestamp after every
fault test. The public `OverridePeak` method also requires the current lease,
can only raise an existing trusted peak, and persists atomically. There is no
operator-facing UI for it; a reset or other re-seeding is an offline
file-and-restart procedure.

## Startup and restart interlock

Before registering—or before entering even in an unrouted mode—the strategy
requires:

- an assigned account and non-empty Book ID;
- valid ExpectedSeats, `R`, Instance ID, start balance, drawdown and offset;
- exactly one contract;
- a flat account position for the instrument;
- no active entry, stop, target, flatten or emergency order from any instance of
  this strategy family on the account/instrument.

If preflight fails, the strategy sets a startup interlock and permits no new
entries in any mode until it is disabled, corrected and re-enabled.

Operationally, use a stricter restart rule: **all six accounts must be flat and
have no working orders of any kind for the instrument**. Realtime startup clears
the strategy's local entry, stop, risk and target state. It cannot adopt or
safely reconstruct an open position or an existing order. Test restarts only
when flat/no-orders; never use a mid-position restart as a persistence test.

Order errors and rejections are **logged only**. `RealtimeErrorHandling` is
`IgnoreAllErrors`, matching the original strategy: `StopCancelClose` would
flatten a live position at market on any transient rejection, and the zero-band
protective stop-limit is exactly the order most likely to trigger that. No
in-session condition disarms a running seat — the startup interlock is a
one-time preflight only. This trades a platform fail-safe for fidelity to the
measured system, which makes the order-rejection Playback test more important,
not less.

## Output and audit limits

Files are under:

```text
Documents\NinjaTrader 8\PropRouter\
```

- `peaks_<book>.csv` — strict durable high-water marks.
- `peaks_<book>.csv.bak` — previous primary, for manual recovery.
- `routing_<book>_<yyyyMMdd>.csv` — Routed decisions only, one row per
  registered seat when the first claimant creates a decision.

The routing CSV is useful for diagnosis and approximate comparison with
`signal_router.py`, but it is not a complete exact-replay ledger. It omits
connection flags, equity/status timestamps, required-headroom input, explicit
eligibility reasons and claimant acceptance. The logged `trades` count is an
allocation count, not a fill count, and winners are incremented before the row is
written. Routing-log I/O remains best-effort and does not itself fail the book
closed.

`UnroutedLogOnly` writes previews to the Output window only. Preserve the
Output text separately when testing that mode.

## Known unresolved limitations

- **Zero-band stop-limit non-fill.** After an entry fill, the protective exit is
  a stop-limit whose stop and limit are both the candle low. A gap through that
  price can leave the long position open and unprotected. The headroom filter
  covers only nominal candle risk; it does not cover commissions, slippage or a
  non-filling stop-limit. A null API return triggers an emergency market exit,
  but there is no independent accepted/working acknowledgement timeout. This is
  the primary live-release blocker.
- **Take-profit limit non-fill.** Bar-close target evaluation is intentional and
  unchanged from the original rules, but the limit submitted at close plus
  offset is not guaranteed to fill. There is no independent accepted/working
  acknowledgement timeout.
- **Cancel/replace depends on broker acknowledgement.** A newer valid red candle
  cancels the prior working entry and replaces it only after confirmed
  `Cancelled`. If the old order fills first, the replacement is discarded. If
  price is already at/above the new stop after cancellation, the replacement is
  skipped. Both races require provider-specific Playback testing.
- **No open-position/order adoption.** Startup deliberately refuses an existing
  account position or this instance's non-terminal orders. It cannot recover
  their original stop/R:R state.
- **No complete two-phase claim/accept protocol.** A selected caller that reaches
  `TryClaim` is atomically marked `Pending` before the grant returns, closing the
  local grant-to-submit quota gap. But an allocated winner that never calls is
  not proactively reserved, and failed/rejected submissions are not reassigned
  to another seat. There is no broker-acceptance token or acknowledgement timeout.
- **A fail-closed decision stays closed for that bar timestamp.** If the first
  claim occurs while a seat is stale or otherwise not ready, later heartbeats do
  not recompute that cached decision. A later signal timestamp can recover.
- **Decision identity is only `Time[0].Ticks`.** Repeated local timestamps around
  daylight-saving changes, duplicate/replay bars, or a claimant delayed beyond
  the 128-decision cache need explicit signal IDs/tombstones before live use.
- **`trades_taken` resets on process restart.** It counts allocations, not
  accepted orders or fills, and is only a tie-break after headroom.
- **Downtime can miss a peak.** NT8 can ratchet NetLiquidation only while it is
  running and receiving updates. Reconcile and re-seed from the broker after any
  outage in which a higher governing peak may have occurred.
- **Durability is synchronous.** Every observed new seeded high atomically writes
  the merged peak file while holding the router lock. This favors high-water-mark
  safety over latency, but disk stalls can delay strategy callbacks; stress-test
  the actual storage and monitor write latency before any release.
- **EOD modelling depends on external rules.** The conservative argument above
  depends on broker definitions and the unverified freeze behavior.
- **Freshness depends on market data.** A paused/quiet Playback stream can make a
  seat older than 20 seconds and stop the whole book. This is safe failure, but
  it must be operationally understood.
- **No Strategy Analyzer qualification.** Entry logic and router registration
  run only in Realtime; use Playback for end-to-end tests.
- **No independent hard risk governor.** The router vetoes new entries; it does
  not flatten on a floor breach, cap aggregate open loss/positions, enforce an
  acknowledgement timeout, or provide an operator kill switch.
- **Errors are not fail-safe, by design.** `IgnoreAllErrors` and the absence of
  in-session disarming are deliberate fidelity choices. A rejected protective
  stop leaves an unprotected position and the strategy keeps running; only the
  emergency market exit and the built-in session close will act.
- **Entry re-pricing carries the original's race.** A new red candle re-prices
  the working order in place. If the old price fills before the modification
  lands, that fill is protected with the newer candle's stop. This is the
  original behaviour, retained deliberately so fills match the measured system.

## Playback release-gate checklist

Use a dedicated simulation Book ID and six distinct simulation accounts. Run
each configuration-fault test with a fresh Book ID or after a full NT8 restart,
because the first registration latches the in-memory manifest and peak-file
health for that process.

A “no order” observation is not enough. The Output reason and any Routed CSV
detail must show the expected fail-closed cause.

### Build and deterministic setup

- [ ] Copy both files, compile all NinjaScript, and obtain zero compile errors or
  warnings relevant to these classes.
- [ ] Create six distinct simulation accounts and map Instance IDs 1–6 once.
- [ ] Set one NT8 global time zone; record the MT5-to-NT8 hour conversion,
  including daylight-saving cases.
- [ ] Use identical instrument, BarsPeriod, Trading Hours, quantity, exit offset,
  stop-cover flag and 24 R:R values.
- [ ] Create a strict five-column Playback peak file with six unique simulation
  account rows; verify start/DD equality and exact UTC timestamps.
- [ ] Start with all six accounts flat and with no working orders.

### Router and persistence faults

- [ ] **Quorum:** enable only five seats, advance Playback to a valid signal, and
  confirm zero routed entries with `registered seats=5, expected exactly 6`.
  Enable seat 6 and confirm that original signal remains denied but a new signal
  timestamp can route once the book is ready.
- [ ] **Identity/lease:** try a duplicate Instance ID and a duplicate normalized
  account. Confirm registration is refused and the original owner remains intact.
- [ ] **Manifest:** change one seat's R:R, ExitOffset, BarsPeriod, Trading Hours,
  routing mode, frozen offset, equity source, `R`, ExpectedSeats or stop-cover
  flag. Confirm registration/claim fails closed with a manifest or
  decision-input mismatch.
- [ ] **Quantity:** set quantity to two. Confirm the startup interlock blocks the
  strategy in every routing mode.
- [ ] **Seed schema:** separately test a bad header, comma decimal, extra field,
  blank row, invalid timestamp, duplicate normalized account, peak below start,
  and configured start/DD mismatch. Each must prevent Routed allocation.
- [ ] **Persistence:** force a new simulated equity high. Confirm the primary and
  `.bak` exist, all six rows remain, the intended peak changes, and a flat
  restart restores the same floor/headroom.
- [ ] **Persistence failure:** make the state directory unwritable in an isolated
  test, trigger a new high, and confirm subsequent Routed claims fail closed.
  Restore permissions and the primary from a verified backup before restarting.
- [ ] **Freshness/disconnect:** pause market data for more than 20 seconds or
  disconnect one simulation account. Confirm all new Routed allocations stop;
  resume/reconnect and confirm all six must become fresh before routing resumes.

### Signal and order lifecycle

- [ ] **Normal allocation:** with six ready/free seats and no pending order,
  confirm one and only one one-contract entry is submitted for `R=1`, to the
  highest-headroom eligible seat. Do not require round-robin fairness.
- [ ] **Pending reservation:** leave one entry working and generate another
  signal. Confirm it consumes the `R=1` pending quota and no second entry is
  allocated.
- [ ] **Cancel-first replacement:** leave red-candle A's entry working, then close
  a valid red candle B. Confirm A receives a cancel request, no B order is sent
  before A is terminal, and exactly one B buy stop-limit appears after A reports
  `Cancelled`, using B's high, low and R:R.
- [ ] **Fill-first replacement race:** fill A while its cancellation is pending.
  Confirm B is discarded and A's protective stop and target calculation use A's
  low and R:R—not B's.
- [ ] **Overlap:** fill a seat, keep it in position, then generate a new signal.
  Confirm that seat is excluded and another free seat may receive the new copy.
- [ ] **Headroom filter:** with stop coverage enabled, test one seat below and one
  above nominal candle risk. Confirm only the latter is eligible; then repeat
  with coverage disabled and document the difference.
- [ ] **Claimant loss:** prevent or reject the selected winner's submission.
  Confirm no automatic second-seat reroute occurs. Treat this as an unresolved
  behavior, not a passing redundancy test.
- [ ] **Order rejection:** induce an entry/exit rejection in the simulator.
  Confirm the strategy logs the error, keeps running, and still takes new
  entries. Confirm no position is auto-flattened.
- [ ] **Re-pricing:** with an unfilled entry working, produce a later red candle.
  Confirm the working order is modified in place with no cancel/resubmit gap, and
  that re-pricing consumes no additional router claim.
- [ ] **Stop gap:** gap Playback through the equal stop/limit price. Confirm and
  document whether the stop remains unfilled. Live release remains blocked until
  this failure mode has a tested mitigation.
- [ ] **Take profit:** test an intrabar target touch with close below target, then
  a close above target. Confirm only the latter submits the close-plus-offset
  limit and record whether it fills.
- [ ] **End of day:** verify NinjaTrader's built-in session-close exit flattens
  and cancels at the Trading Hours template's session end. There is no
  strategy-level cutoff to test.
- [ ] **Restart:** first restart flat/no-orders and confirm peaks survive. Then,
  on simulation only, attempt startup with a position and with a non-terminal
  strategy order; confirm the startup interlock refuses adoption.

### Modes and evidence

- [ ] In `UnroutedLogOnly`, confirm every local signal still submits, previews
  appear in Output, and no routing CSV is created.
- [ ] In `Routed`, confirm the first claim creates one decision block in the
  routing CSV and later claimants reuse it.
- [ ] Archive strategy settings, global time zone, Trading Hours template, seed
  primary/backup, Output, routing CSVs and Playback data for the test run.
- [ ] Review every known limitation above and record a fix, accepted control, or
  explicit release blocker. At present the stop-limit non-fill and missing
  claimant acceptance/reroute keep this README at simulation/playback-only.

## Future off-hours cutover procedure

This is a procedure for a future release candidate, not approval to deploy the
current prototype.

1. Obtain a written release decision tied to an archived passing Playback run
   and a specific code revision.
2. Choose a closed-market maintenance window. Verify all six accounts are flat
   and have no working orders. Disable every related strategy.
3. Back up the current NinjaScript files, strategy templates, peak primary and
   peak `.bak`. Reconcile every live peak/floor against the broker.
4. Install both reviewed files and compile with zero errors. Set and record the
   NT8 global time zone before opening/enabling the charts.
5. Use a new, live-only Book ID for the release. Create its exact five-column
   seed file while all strategies are disabled.
6. Configure the **final** settings on all six disabled instances: unique IDs
   1–6, correct accounts/start/DD, `ExpectedSeats=6`, `R=1`, one contract,
   identical manifest fields, the final 01–23 R:R map, and `Routed`.
   Never stage all-window live charts in an unrouted mode.
7. Fully restart NT8 so the new Book ID and seed are loaded into clean in-memory
   state. Reconfirm flat/no-orders after reconnection.
8. While the market is still closed, enable the six Routed instances one at a
   time. Quorum keeps new routing disarmed until all six are registered, seeded,
   connected and fresh. Confirm every registration/seed message; any mismatch or
   persistence error aborts the cutover.
9. If any check fails, disable the book while it is still flat, correct the
   offline configuration/file, and restart the whole NT8 process before retrying.
10. Supervise the first session and retain Output/routing/state files. On any
    unexpected order, stale seat, rejection, persistence fault or broker-state
    mismatch, stop new entries and manage existing broker exposure according to
    the approved incident procedure; router fail-closed behavior does not itself
    flatten that exposure.

Never hand-edit a loaded peak file, change the global time zone, or change a
manifest field during an active session.
