# Accounts Staggering

This project explores a simple prop-style account staggering idea: instead of starting every account at once, new accounts are opened over time or when certain conditions are met. The goal is to diversify account age and risk position so that one account may be close to drawdown while another is still early in its equity curve.

The current simulator focuses on a prop account rule set with a trailing drawdown that eventually freezes at a safety-net level. It can load copied sweep files, model staggered account starts, and estimate how many accounts survive long enough to reach the frozen drawdown state.

## What the project contains

- `prop_account_staggering.py`: the main simulator for staggered prop accounts
- `Accounts_starts_extended.py`: the older legacy script kept for reference
- `1_sweeps/`: copied sweep files used as input data
- `MAE/`: source trade exports / MAE-style data
- `results/`: generated output files from simulator runs

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
