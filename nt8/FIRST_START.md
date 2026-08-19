# First start of the NT8 routed book

> **Use this version only in Playback with distinct simulation accounts.**
> The procedure below also explains how the future broker-derived peak file is
> constructed, but it does not make the current strategy live-ready. The release
> blockers and full Playback test matrix are in `README.md`.

## 1. Prepare the book while everything is stopped

For a first installation, copy `PropRouter.cs` to
`Documents\NinjaTrader 8\bin\Custom\AddOns\` and the routed strategy `.cs` file
to `Documents\NinjaTrader 8\bin\Custom\Strategies\`. Do not copy the local
`tests` directory into NinjaTrader.

1. Disable every strategy instance and close NinjaTrader. All six accounts must be flat and must have
   no working orders for the instrument.
2. Choose one Book ID containing only letters, digits, `-`, or `_`, for example
   `PLAYBACK_ROUTER_V2`. Do not reuse a live-looking Book ID for testing.
3. Create **six distinct simulation accounts**. Six charts assigned to the same
   `Sim101` account are not six independent seats.
4. Use one chart/strategy instance per account and assign Instance IDs `1`–`6`
   exactly once. Set **Expected seats = 6** and **R = 1** on all six.
5. Record the NT8 global application time zone. Use the identical instrument,
   30-minute BarsPeriod, Trading Hours template and data connection on every
   instance.

For the recovered study configuration, use one contract, `Exit Offset = 0`,
`Track peak on unrealized = true`, `Require headroom covers stop = false`,
`00:00-01:00 R:R = 0`, and every hourly R:R from `01:00-02:00` through
`23:00-00:00 = 1`. Do not assume the hour mapping is correct until Playback has
matched the source signals in the chosen NT8 time zone.

## 2. Collect one authoritative peak record per account

For each account, collect these four values from the broker dashboard, statement,
or support—not from the strategy:

| Value | Meaning |
|---|---|
| `account` | Exact account name. Text after the first `!` is ignored by the router. |
| `start_balance` | Contractual starting balance configured on that strategy seat. |
| `drawdown` | Contractual trailing drawdown amount configured on that seat. |
| `peak` | Highest equity/balance value that governs the broker's current trailing floor. |

The seed `peak` is **not current equity**, current balance, current profit, or the
liquidation threshold itself. It must be a high-water mark and must be at least
`start_balance`.

Use the broker rule applicable to that account:

- **Intraday trailing account:** if the dashboard exposes a still-trailing
  auto-liquidation threshold, reconstruct `peak = threshold + drawdown` and
  cross-check it against the broker's high-water value. If the threshold has
  frozen, that formula may no longer reconstruct the historical peak; use the
  broker's authoritative high-water mark instead.
- **End-of-day trailing account:** use the highest qualifying end-of-day closed
  balance reported by the broker. Do not invent an intraday threshold. With
  `Track peak on unrealized = true`, the router may subsequently ratchet to a
  higher observed NetLiquidation peak, which is conservative only under the
  broker-rule assumptions documented in `README.md`.
- **Uncertain or conflicting value:** do not enable the book. Obtain the value
  from the broker and reconcile it first.

For every row, calculate the router floor independently:

```text
floor    = min(peak - drawdown, start_balance + frozen_offset)
headroom = current NetLiquidation - floor
```

The recovered study uses `frozen_offset = 100`. Compare the calculated floor
with the broker's displayed liquidation threshold/cushion. A mismatch is a stop
condition; do not “correct” it by guessing a different peak.

## 3a. Generate the peak file with make_peak_file.py

Do not hand-derive peaks. `make_peak_file.py` builds the file, verifies every row
against the broker's own numbers, and refuses to write anything that does not
reconcile. Run it from the `nt8` folder.

### Commands

| Purpose | Command |
|---|---|
| Live book, write locally | `python make_peak_file.py Broker_statement.csv --book LIVE` |
| Live book, write into NinjaTrader | `python make_peak_file.py Broker_statement.csv --book LIVE --install` |
| Simulation book | `python make_peak_file.py --seats sim --book SIM --install` |
| Verify a live file is current | `python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv` |
| Verify a simulation file | `python make_peak_file.py --seats sim --check peaks_SIM.csv` |
| Non-standard NinjaTrader folder | add `--nt8-dir "D:\path\to\PropRouter"` |

`--install` writes straight into `Documents\NinjaTrader 8\PropRouter\` and names
the file from `--book`, so the Book ID is typed once instead of twice. Without it
the file lands in the current directory and the script prints the exact
destination to copy it to. `--install` and `--out` cannot be combined.

`--check` exits `0` when the file is current and `1` when any seat is stale, so
it can be wired into a pre-start check.

### Live book, from a broker statement

Export the account statement CSV from the broker portal without reformatting it.
It must contain `Account`, `Status`, `Account Balance`,
`Auto Liquidate Threshold Value`, `Auto Liquidate EOD Value` and
`Auto Liquidate Peak Balance`; a missing column is reported by name.

The seed is the broker's own governing high-water mark,
`Auto Liquidate Peak Balance`, used unmodified. The script then proves it:

- **Intraday seat, not yet frozen.** `peak - threshold` *is* the drawdown, so it
  must equal the configured value exactly. This is what catches a wrong
  `SEAT_CONFIG` drawdown before it can reach the router.
- **Intraday seat, frozen.** That identity no longer holds because the floor is
  pinned, so the script verifies `threshold == start + frozen offset` instead.
- **End-of-day seat** (`Auto Liquidate EOD Value` populated, threshold blank).
  Modelled as intraday, and the script proves the modelled floor sits at or
  above the broker's real EOD floor, reporting the gap. A modelled floor *below*
  it would overstate headroom and is refused.

Any disagreement prints both sides and blames neither - a wrong `SEAT_CONFIG`
and a stale or edited statement produce the same symptom.

### Simulation book, with no statement

Simulation accounts have no broker statement. An account that has never traded
has no high-water mark above its starting balance, so the script seeds
`peak = start` directly from `SEAT_CONFIG_SIM`, giving each seat its full
drawdown as headroom. No statement argument is needed or accepted.

This is correct **only before those accounts trade**. Once they have, the real
peak may be higher, and seeding at the starting balance would put the floor too
low and overstate headroom. Reset the simulation accounts in NinjaTrader and
re-run rather than guessing a value.

`SEAT_CONFIG_SIM` mirrors the live drawdown mix (1500/1500/2000/2000/2000/2500)
so Playback exercises the same ranking dynamics as the real book. Its `start`
values must equal the simulation accounts' actual starting balances in
NinjaTrader, because the router measures headroom against real account equity.
The start balance does not change the dynamics: a seat freezes once its peak
reaches `start + dd + frozen offset`, so the distance to freeze is `dd + 100`
wherever the account starts.

### What must match the charts

`start_balance` and `drawdown` come from the `SEAT_CONFIG` / `SEAT_CONFIG_SIM`
table at the top of the script. **They must equal each chart's Seat starting
balance and Seat trailing drawdown exactly** - the router compares them with an
exact equality test and refuses to register the seat on any mismatch. Edit the
table, never the generated CSV. Every run prints the per-chart values to enter.

The script also validates the table itself: Instance IDs must be unique and
cover `1..N` with no gaps, which is what the router's quorum requires.

### Output guarantees

The generated file is UTF-8 without BOM, LF endings, period decimals, exact
header, five fields per row - the precise form the router's strict parser
accepts. It never needs hand editing, and must never be written from Excel.

Copy or install it with **every strategy stopped**: the router rewrites this file
on every new equity high and will clobber an edit made while it is running.


## 3b. When the peak file must be re-seeded

The peak file is **persistent**. It is not rebuilt per session: the router reads
it once per book per NT8 process and rewrites it on every new equity high. A
normal shutdown does not invalidate it.

The peak only advances while NT8 is running and receiving account updates, and
account equity only moves when something trades. So the file goes stale exactly
when equity moved while the router was not watching.

| Situation | Re-seed? |
|---|---|
| Weekend or overnight shutdown, all accounts flat | No |
| Routine restart, flat, no orders | No |
| Disable and re-enable a strategy, NT8 still running | No (and the file would not be re-read anyway) |
| Connection loss while all accounts were flat | No |
| Connection loss or crash **while a position was open** | **Yes** |
| NT8 closed with an open position | **Yes** |
| Any manual or external trade on a seat account | **Yes** |
| Payout, withdrawal or other broker balance adjustment | **Yes** |
| Account reset, replacement or tier change | **Yes** |

A stale peak is always stale **low**, which makes the modelled floor too low and
headroom too high — the seat looks healthier than it is and attracts trades it
should not get. That is the one error direction that actively hurts, so never
assume; verify.

Verification costs seconds. Export a fresh broker statement and run:

```bash
python make_peak_file.py Broker_statement.csv --check peaks_LIVE.csv
```

Exit code `0` means the file is current and NT8 can start. Exit `1` lists the
stale seats; re-run without `--check` to rewrite. Make this part of the
start-of-day routine rather than something you remember to do after an incident.

Partial self-healing exists but cannot be relied on: on reconnect the router
ratchets `peak` up to current equity, so a high that is *still* in place is
recovered automatically. A high that was made and given back while NT8 was blind
is lost, and only a fresh statement will reveal it.

> **A replaced peak file needs a full NinjaTrader restart.** The load is latched
> per book per process, so disabling and re-enabling the strategies will silently
> keep using the old peaks. Close NT8 completely, replace the file, then start.

## 3. Create the peak file manually

Use this path for simulation books, or when no broker statement is available.

With NinjaTrader fully closed, create this directory if it does not exist:

```text
Documents\NinjaTrader 8\PropRouter\
```

Create `peaks_<BookId>.csv`. For the example Book ID above, the exact path is:

```text
Documents\NinjaTrader 8\PropRouter\peaks_PLAYBACK_ROUTER_V2.csv
```

With Windows file-name extensions visible, confirm it ends in `.csv`, not
`.csv.txt`.

Use a plain-text editor and this exact five-column schema:

```csv
account,start_balance,drawdown,peak,updated_utc
SIM-ROUTER-01,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
SIM-ROUTER-02,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
SIM-ROUTER-03,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
SIM-ROUTER-04,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
SIM-ROUTER-05,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
SIM-ROUTER-06,50000.00,2500.00,50000.00,2026-08-18T12:00:00Z
```

Replace every example account and number with the actual simulation-account
configuration. The start balance and drawdown in each row must exactly match that
seat's strategy properties.

The previously collected future-live reference values were:

```csv
account,start_balance,drawdown,peak,updated_utc
PA-APEX-240737-09,25000.00,1500.00,28903.95,2026-08-19T00:00:00Z
PA-APEX-240737-10,25000.00,1500.00,27870.23,2026-08-19T00:00:00Z
PA-APEX-240737-12,50000.00,2000.00,51228.65,2026-08-19T00:00:00Z
PA-APEX-240737-13,50000.00,2000.00,50480.45,2026-08-19T00:00:00Z
PA-APEX-240737-14,50000.00,2000.00,50338.31,2026-08-19T00:00:00Z
PA-APEX-240737-15,50000.00,2500.00,50844.85,2026-08-19T00:00:00Z
```

These are an audit reference, **not values to copy blindly**. Re-read fresh
broker values immediately before any future cutover. In particular, a peak can
only stay the same or rise during an account lifecycle unless the broker has
performed a documented reset.

CSV rules are strict:

- Use periods for decimals, never decimal commas or thousands separators.
- Keep exactly five comma-separated fields per data row and no blank rows.
- Use UTC timestamps exactly as `yyyy-MM-ddTHH:mm:ssZ`.
- Keep account names unique after removing any suffix beginning with `!`.
- Save as plain UTF-8 text; do not round-trip the file through Excel.

Make a separate, dated operator backup before starting NT8.

## 4. Start and verify before the first signal

1. Start NinjaTrader only after the file is saved. The router reads a book's seed
   file once per NT8 process; correcting it later requires another full restart.
2. Compile `PropRouter.cs` and the routed strategy in the NinjaScript Editor.
   Proceed only with zero relevant errors or warnings.
3. Add/enable the six `Routed` instances on their six distinct simulation
   accounts. Confirm all settings that form the manifest are identical; only
   Instance ID, account, start balance and drawdown may be seat-specific.
4. In the Output window, confirm each seat reports `peak seeded` with the expected
   peak and floor. Confirm there are no messages containing `STARTUP INTERLOCK`,
   `REGISTRATION REFUSED`, `PEAK NOT SEEDED`, `ROUTER FAILURE`, or `not ready`.
5. Let market data run for at least one heartbeat cycle. All six seats need fresh
   equity and status within 20 seconds before the book can route.
6. Re-open `peaks_<BookId>.csv` read-only. Confirm it still has the header plus six
   rows. After the first persisted high, also confirm `.bak` exists and neither
   file lost rows.
7. Calculate each seat's floor and headroom manually from the broker/simulation
   equity and compare the expected highest-headroom winner with the first routed
   decision log.

## 5. First Playback order-lifecycle checks

Before any wider test, observe one complete controlled sequence:

1. A valid red 30-minute candle routes exactly one one-contract buy stop-limit
   for `R=1`.
2. While it is working, a newer valid red candle requests cancellation of the old
   entry. No replacement is submitted until the old order reports `Cancelled`.
3. After cancellation, exactly one replacement uses the newer candle's high,
   low and R:R.
4. Repeat with the old order filling during cancellation. Confirm the replacement
   is discarded and the fill uses the old candle's low/R:R.
5. After a fill, confirm the original equal-price protective stop-limit is sent.
6. Confirm an intrabar target touch with a close below target does nothing; a
   later 30-minute close at/above 1R submits the sell limit at close plus offset.
7. Confirm the actual bar series performs the end-of-day cancel/flatten behavior.

Then complete every release-gate test in `README.md`.

## Stop conditions

Disable the book and reconcile before continuing if any seat is unseeded, the
row count changes unexpectedly, primary/backup timestamps do not advance after a
new high, a floor differs from the broker rule, a seat/account is duplicated,
the six-instance manifest disagrees, an order is rejected, a connection becomes
stale, or NT8 is restarted with an open position/working order. The current code
cannot adopt pre-existing broker state safely.
