# Accounts Staggering

This project explores a simple prop-style account staggering idea: instead of starting every account at once, new accounts are opened over time or when certain conditions are met. The goal is to diversify account age and risk position so that one account may be close to drawdown while another is still early in its equity curve.

The current simulator focuses on a prop account rule set with a trailing drawdown that eventually freezes at a safety-net level. It can load copied sweep files, model staggered account starts, and estimate how many accounts survive long enough to reach the frozen drawdown state.

## What the project contains

- `prop_account_staggering.py`: the main simulator for staggered prop accounts
- `account_farming.py`: the farming study — withdrawals as seed capital, and the
  distribution of outcomes rather than one run (see below)
- `Accounts_starts_extended.py`: the older legacy script kept for reference
- `1_sweeps/`: copied sweep files used as input data
- `MAE/`: source trade exports / MAE-style data
- `results/`: generated output files from simulator runs

## The two simulators

They answer different questions and share the sweep loader (`account_farming.py`
imports `read_trade_file` and `sweep_files` from `prop_account_staggering.py`, so
there is only one file parser).

`prop_account_staggering.py` asks **"how does a staggered book behave?"** — start
policies (time / profit / drawdown triggers), per-account survival, daily
portfolio snapshots.

`account_farming.py` asks **"is buying seats a better business than tuning the
strategy?"** and adds three things the other does not model:

1. **Single-position replay.** The EA holds one position at a time, so with every
   hourly window enabled they compete for one slot. Each sweep file was generated
   with only its own window active, so concatenating them counts entries that
   could never have been taken — **3,109 of 11,754 (26%)** on this data.
   `prop_account_staggering.py` prints a warning about the overlap;
   `account_farming.py` resolves it by replaying the merged stream.
2. **Withdrawals as seed capital.** Once the trailing drawdown freezes, the floor
   is fixed at +$100, so cushion = equity − 100 and every withdrawal makes the
   seat more fragile. But at $200 a seat, withdrawn cash buys more seats.
   Harvesting is the only way to fund growth.
3. **The distribution, not one path.** Bootstrapping has an absorbing state: lose
   every seat while holding less than one seat's price in cash and it is over
   permanently. That makes single runs chaotic — adjacent withdrawal levels
   differed by orders of magnitude. Every policy is scored across many
   overlapping fixed-length windows and reported as median / p10 / p90 / ruin.

```powershell
python account_farming.py --dd 2500 --cost 200 --seats 20 --seed 1200
```

Needs `plotly` for the HTML report (`pip install plotly`); without it the script
still runs and writes the CSVs, and simply skips the report.

## What has been established so far

Measured on RR strategy, all windows, RR 1.0, 2020-2026, $2,500 trailing DD,
$200 per seat. Numbers reproduce exactly from the source project.

- **A seat reaches the Safety Net 87.7%** of the time (starts with a full year of
  runway), median **174 days**. Only 12.3% die before ever freezing.
- **Harvest as hard as the rules allow.** Withdrawing down to the Safety Net beat
  every softer level, monotonically. Never withdrawing earns nothing withdrawable
  no matter how well it trades — it can never afford a second seat.
- **Seed size drives ruin, not policy.** From 1 seat the book was wiped out in 17%
  of windows; from 6 seats, at one new seat a month, **0 of 18 windows were wiped
  out** (p10 $7,195, median $32,558 realized cash per 2 years).
- **Per seat-year ≈ $3,850**, against ≈ $778 for the per-window RR-tuned
  allocation this project split off from. Tuning risk down per account cost about
  5× in exposure; the binding constraint is how many seats you may run.

### Read these caveats before trusting any of it

- **This is leverage, not alpha.** N seats is N contracts. Every seat trades
  identical signals and differs only by start date, so a drawdown deep enough to
  kill one is deep enough to kill the book. Staggering spreads entry points, not
  outcomes.
- **The windows overlap heavily.** 18 two-year windows over a 6.5-year sample is
  more like 3–4 independent periods. Treat p10/p90 as shape, not precise
  quantiles.
- **The reconstruction runs ~13% light** vs a real MT5 run of the same config
  (8,645 trades / $40,195 vs 9,775 / $46,344), because one window's export is
  tester-blown and the merge approximates blocking rather than reproducing it.
- **The withdrawal level is a real free parameter** and has not been validated
  out-of-sample. It should be, before it is trusted.

### The open question worth doing next

RR was never chosen — 1.0 is the EA default, and the strategy is reportedly
profitable anywhere from 0.5 to 10. That makes RR a *farming* parameter now, not
a profit one: the best RR is the one that **reaches the Safety Net fastest
without dying on the way**, because time-to-freeze governs how fast the book
compounds. Lower RR turns the single slot over faster. `account_farming.py`
already has the machinery — it is one loop over RR reporting freeze rate and
days-to-freeze.

## How to run

Use Python 3.10+ if possible. From the project folder:

```powershell
python prop_account_staggering.py --commission-per-trade 0.75
```

That runs the simulator against the default `1_sweeps/RR` input set and writes account-level and daily output into `results/`.

## Common options

- `--input-csv PATH` - run from a single CSV export instead of the sweep folder
- `--sweep-root PATH` - point at a different sweep directory
- `--rr VALUE` - choose the RR file suffix to load, such as `1.00`
- `--windows 1-2 2-3 ...` - restrict the hourly windows to test
- `--start-policy time|profit|dd|any` - choose how new accounts are started
- `--max-accounts N` - cap the number of active accounts
- `--interval-days N` - start new accounts on a fixed cadence
- `--dd-limit N` - set the trailing drawdown amount
- `--freeze-at-profit N` - set the profit level where the trailing drawdown freezes
- `--commission-per-trade N` - apply a fixed commission per trade
- `--no-plot` - skip chart generation

## Notes

- The copied sweep files do not directly reveal the exact intratrade order of MAE and MFE, so the simulator uses a configurable assumption for that path.
- The seventh column in the copied sweep files is not treated as commission unless an explicit `Commission` field exists in the source.
- Results are only as good as the trade export underneath them, so a full account equity stream from MT5 is still the best source for final validation.

## Output

The script writes CSV files into `results/` for:

- per-account summaries
- daily portfolio snapshots

If plotting support is available in your Python environment, it can also generate a chart for the run.
