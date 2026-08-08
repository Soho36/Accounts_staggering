# Accounts Staggering

This project explores a simple prop-style account staggering idea: instead of starting every account at once, new accounts are opened over time or when certain conditions are met. The goal is to diversify account age and risk position so that one account may be close to drawdown while another is still early in its equity curve.

The simulator models a prop account rule set with a trailing drawdown that eventually
freezes at a safety-net level. It loads copied sweep files, models staggered account
starts, and estimates how many accounts survive long enough to reach the frozen
drawdown state — then asks whether buying more seats with the withdrawn cash is a
better business than tuning the strategy.

## What the project contains

- `account_farming.py`: **the simulator.** One script; see below
- `prop_account_staggering(LEGACY).py`: the previous separate simulator, now fully
  absorbed into `account_farming.py` and no longer imported by anything. Kept only
  as a reference; safe to delete (it is in git at `b546d69`)
- `Accounts_starts_extended(LEGACY).py`: the older legacy script, kept for reference.
  Its plots have been ported into the HTML report
- `1_sweeps/`: copied sweep files used as input data
- `MAE/`: source trade exports / MAE-style data
- `results/`: generated output files, including `account_farming.html`

## The simulator

```powershell
python account_farming.py --dd 2500 --cost 200 --seats 20 --seed 1200
```

Needs `plotly` for the HTML report (`pip install plotly`); without it the script still
runs and writes the CSVs, and simply skips the report.

It answers two questions that used to live in two scripts. *"How does a staggered book
behave?"* — start policies, per-seat survival, the book over time. And *"is buying seats
a better business than tuning the strategy?"* — which needs three things a plain
per-account simulation does not model:

1. **Single-position replay.** The EA holds one position at a time, so with every
   hourly window enabled they compete for one slot. Each sweep file was generated
   with only its own window active, so concatenating them counts entries that could
   never have been taken — **3,109 of 11,754 (26%)** on this data. The old script
   printed a warning; this one replays the merged stream and drops the blocked
   entries, so there is nothing left to warn about.
2. **Withdrawals as seed capital.** Once the trailing drawdown freezes, the floor is
   fixed at +$100, so cushion = equity − 100 and every withdrawal makes the seat more
   fragile. But at $200 a seat, withdrawn cash buys more seats. Harvesting is the only
   way to fund growth.
3. **The distribution, not one path.** Bootstrapping has an absorbing state: lose every
   seat while holding less than one seat's price in cash and it is over permanently.
   That makes single runs chaotic — adjacent withdrawal levels differed by orders of
   magnitude. Every policy is scored across many overlapping fixed-length windows and
   reported as median / p10 / p90 / ruin / wipeouts.

### The account rule

    floor = min(peak_unrealized − dd, frozen_floor)
    Safety Net = dd + frozen_floor

The Safety Net is **derived, not configured**. The moment peak profit reaches
`dd + frozen_floor`, `peak − dd` overtakes `frozen_floor` and the `min()` pins the floor
there by itself — "frozen" is a label for that crossing, not a separate rule. This is
why the old `--freeze-at-profit` flag is gone: setting it independently of `--dd-limit`
could raise the pre-freeze floor above +$100, which no firm actually does.

`--intratrade-path` survives, because the order of MAE and MFE inside a trade is the one
thing the sweep genuinely cannot tell us. `mae-first` is the default (it reproduced MT5's
equity drawdown exactly); `mfe-first` is the optimistic case, since lifting the floor
before the dip can save a seat.

Commission is hardcoded at **$1.05 round turn** (`COMMISSION_ROUNDTURN`) — it is the
broker's real figure, not a tuning knob. The seventh column in the sweep exports is *not*
a commission, so it is preserved as `Extra` and ignored.

## What has been established so far

Measured on RR strategy, all windows, RR 1.0, 2020-2026, $2,500 trailing DD, $200 per
seat, $1.05 round turn, a new seat every 30 days.

- **A seat reaches the Safety Net 87.5%** of the time (starts with a full year of
  runway), median **175 days**. Only 12.5% die before ever freezing.
- **Do not harvest as hard as the rules allow.** An earlier note here said the opposite —
  that stripping seats to the Safety Net beat every softer level. It does maximise
  realized cash, but it also synchronises the book and wipes it out in 33–50% of windows.
  Withdrawing a share of gains earns more *net* with no wipeouts at all. See below.
- **Never withdrawing earns nothing withdrawable**, no matter how well it trades — under
  bootstrap funding it can never afford a second seat (median 6 seats, $0 cash).
- **Per seat-year ≈ $4,780** on the illustrative book (160 seats, 118.7 seat-years,
  $567k of banked cash plus surviving equity), against ≈ $778 for the per-window
  RR-tuned allocation this project split off from — that second figure comes from the
  source project and has not been re-derived here. Tuning risk down per account cost
  roughly 5× in exposure; the binding constraint is how many seats you may run.

  This is a **book-level** figure, and it is policy-dependent: measured per individual
  start date instead, a seat returns a median of $621 and a mean of $1,513 per year of
  runway. The gap is the survivorship the book gets from replacing dead seats. Earlier
  notes quoted $3,850 per seat-year without saying which denominator that was; use the
  numbers above and state the denominator.

## The two modes

The simulator runs both funding models and scores every policy across all windows.

**Mode 1 — subscription.** Own money, one seat every `--interval-days`, never withdraw,
hold forever. Cannot be ruined, because funding is external. It fills to the seat cap and
then only replaces deaths.

**Mode 2 — bootstrap.** The seats pay for their own replacements out of withdrawn cash.
This is the one that can die permanently.

There is exactly one pot of cash. It starts at `--seed`. A frozen seat pays money *into*
it when the withdrawal rule fires; buying a seat takes `--cost` *out* of it. Nothing else
adds to it. So **withdrawals are the only thing that funds growth** — which is why
"never withdraw" under bootstrap funding is not a strategy but a dead end: at $1,200 seed
and $200 a seat it buys exactly **6 seats**, one per interval, and then the pot is empty
permanently. That is the whole explanation for the 6 in the table below.

### Why a book dies all at once, and what actually fixes it

Two seats bought on the same trade are not merely correlated, they are **identical** —
same rule, same stream, same withdrawal policy — so their curves coincide to the cent and
they liquidate on the same trade.

The deeper problem is that **withdrawing down to a level recreates that condition even
for seats with different start dates.** They hold different equity right up until they
both freeze and get stripped to the same number, and from then on they are twins. The
withdrawal policy is what synchronises a book.

That is visible in the numbers. Buying one seat at a time is *not* sufficient on its own:

| policy | windows with a wipeout | blowups | cash median | net median |
|---|---|---|---|---|
| all-in, strip to net | **50%** | 20 | $110,698 | $149,225 |
| 1/interval, strip to net | **33%** | 10 | $35,080 | $47,434 |
| 1/interval, keep $4,000 | 11% | 4 | $18,142 | $45,306 |
| 1/interval, **$200 per $400** (50%) | **0%** | 7 | $22,000 | **$69,610** |
| 1/interval, $200 per $600 (33%) | **0%** | 6 | $12,900 | $67,376 |
| 1/interval, $200 per $1,000 (20%) | **0%** | 5 | $5,600 | $58,693 |
| 1/interval, $200 per $2,000 (10%) | **0%** | 3 | $1,200 | $54,754 |
| 1/interval, $1,000 per $2,000 (50%) | **0%** | 3 | $15,400 | $65,976 |
| 1/interval, $500 per $2,500 (20%) | **0%** | 3 | $3,200 | $53,076 |
| 1/interval, $1,000 per $5,000 (20%) | **0%** | 3 | $2,700 | $46,331 |
| 1/interval, $400 per $400 (100%) | 17% | 10 | $32,100 | $45,645 |
| 1/interval, never withdraw | 0% | 0 | $0 | $38,282 |
| subscription (mode 1) | 0% | 5 | $0 | $66,283 |

**The ratchet is the fix.** Withdrawing a *share of gains* rather than stripping to a
level took the wipeout rate to zero at every rate tested, because no seat is ever reset
to a common equity and the book keeps its dispersion. Withdrawing $200 per $400 gained
also beats strip-to-net on **net** ($69,610 vs $47,434) despite banking less cash,
because far more equity survives.

**Chunk size matters on its own, not just the rate.** At an identical 20%, taking $200
per $1,000 nets $58,693 while taking $1,000 per $5,000 nets $46,331 — bigger, rarer
withdrawals leave the seat further above the Safety Net for longer, which cuts blowups
(3 vs 5 per window) but also starves seat purchases. That is why the policies are
specified in money terms rather than as a percentage: two policies with the same rate are
not the same policy.

**Withdrawing 100% of the trigger amount always wipes out.** Every rule on the diagonal
where the withdrawal equals the gain that triggers it ($400 per $400, $1,000 per $1,000,
…) lost the whole book in 11–17% of windows. Nothing below the diagonal did. That is the
same mechanism as strip-to-a-level: taking the entire gain puts every frozen seat back on
the Safety Net each time, which re-synchronises the book.

*(A cash-reserve condition was tested and rejected — it raised the wipeout rate rather
than lowering it, because it buys fewer seats without making them less correlated. It has
been removed from the code.)*

### Mode 1 vs Mode 2 over the full period

One path each, not an expectation.

| | Mode 1 subscription | Mode 2, $200 per $400 |
|---|---|---|
| own capital in | $5,000 | $1,200 seed |
| seats bought | 25, on 25 distinct dates | 35, on 35 distinct dates |
| blowups / wipeouts | 5 / 0 | 15 / 0 |
| alive at end | 20 | 20 |
| cash withdrawn | $0 | $242,000 |
| equity left | $564,063 | $261,599 |
| **net** | **$559,063** | **$496,599** |

The bootstrap reaches 89% of the subscription's net on a quarter of the capital, and
roughly half of it is already banked rather than sitting inside prop accounts. Everything
in the subscription column is unrealized: a seat that has never withdrawn has never
returned a cent.

Note that `ruin_rate` hides wipeouts: ruin means ending with no seats *and* too little
cash to replace one. A book that goes to zero seats and immediately rebuys is not
"ruined" by that definition. Read the **windows with a wipeout** column first — and note
it is a *rate*, not a median, because a median of 0 concealed a policy that wiped out six
times over the full run.

### Read these caveats before trusting any of it

- **This is leverage, not alpha.** N seats is N contracts. Every seat trades identical
  signals and differs only by start date, so a drawdown deep enough to kill one is deep
  enough to kill the book. Staggering spreads entry points, not outcomes.
- **The windows overlap heavily.** 18 two-year windows over a 6.5-year sample is more
  like 3–4 independent periods. Treat p10/p90 as shape, not precise quantiles.
- **The reconstruction runs ~12% light** vs a real MT5 run of the same config
  (8,645 trades / $40,195 vs 9,775 / $46,344), because one window's export is
  tester-blown and the merge approximates blocking rather than reproducing it.
- **The withdrawal level is a real free parameter** and has not been validated
  out-of-sample. It should be, before it is trusted.

### The open question worth doing next

RR was never chosen — 1.0 is the EA default, and the strategy is reportedly profitable
anywhere from 0.5 to 10. That makes RR a *farming* parameter now, not a profit one: the
best RR is the one that **reaches the Safety Net fastest without dying on the way**,
because time-to-freeze governs how fast the book compounds. Lower RR turns the single
slot over faster. `account_farming.py` already has the machinery — it is one loop over
RR reporting freeze rate and days-to-freeze.

## Options

Input:

- `--input-csv PATH` — run from a single CSV export instead of the sweep folder
- `--sweep-root PATH` / `--stats-root PATH` — point at different sweep directories
- `--rr VALUE` — choose the RR file suffix to load, such as `1.00`
- `--windows 9-10,10-11` — restrict the hourly windows to test
- `--start-date` / `--end-date` — inclusive `YYYY-MM-DD` filters

Account rule:

- `--dd N` (alias `--dd-limit`) — trailing drawdown; the Safety Net follows from it
- `--frozen-dd-floor N` — the fixed floor once the trailing DD freezes
- `--intratrade-path mae-first|mfe-first` — MAE/MFE ordering inside a trade

The book:

- `--cost N` — price of one seat
- `--seats N` — cap on concurrent accounts (a firm rule, not a maths question)
- `--seed N` — starting cash for bootstrap mode
- `--interval-days N` — calendar days between new account starts
- `--start-policy time|profit|dd|any` — what triggers a start; `any` combines them
- `--profit-trigger N` / `--dd-trigger N` — thresholds for those triggers
- `--min-days-between-starts N` — floor on start spacing
- `--max-per-event N` — seats bought at one time. **1 is the staggering fix**; higher
  stacks identical seats that then die together
- `--withdraw-chunk N` / `--withdraw-step N` — the ratchet. One chunk withdrawn per
  `step` of lifetime gain, so `chunk/step` is the withdrawal rate (200/1000 = 20%)
- `--no-explore` — skip `bootstrap_explorer.html`, which is most of the run time
- `--split N` — trader's share of profit (real plans are 0.8–0.9)
- `--horizon N` — years per book window in the robustness sweep

## Output

Written into `results/`:

- `account_farming.html` — the full report (see below)
- `bootstrap_explorer.html` — a page dedicated to mode 2 alone (see below)
- `farming_starts.csv` — one row per possible seat start date
- `farming_withdrawal_policies.csv` — the robustness sweep, both modes
- `farming_book_seats.csv` — per-seat summary of the illustrative bootstrap book
- `farming_subscription_seats.csv` — the same for the subscription book

The HTML report has nine sections: whether a seat reaches the Safety Net by start date;
closed vs floating drawdown; mode 1 with its portfolio curve and per-seat curves; mode 2
with a **policy switcher** that redraws the book, the seat curves and the yearly cash for
any policy in the sweep; both modes across every window plus a net-position comparison;
**the decision section**; monthly P&L; the full policy table; and a reconstruction check
against a real MT5 run.

### The decision section

This is the part that answers "which one do I pick". It plots every policy as typical
outcome (net median) against bad case (net p10). A policy with another one above **and**
to the right of it is **dominated** — strictly worse on both counts, so no risk appetite
would ever choose it. Those are drawn hollow and unlabelled; what remains is the frontier,
where more typical outcome costs you downside. Marker outlines are green if the policy
never lost the whole book in any window, red if it did.

Underneath is a table of "if what you care about is X, then take Y", computed from the
sweep rather than asserted — one row per constraint someone might actually have (never
losing the book; cash in hand; best worst case; highest typical outcome; not touching
withdrawn money at all). None of the rows is *the* answer: which constraint is yours is
the one thing the data cannot settle.

## The bootstrap explorer

`results/bootstrap_explorer.html` is a separate page for mode 2 on its own, because that
is where the decisions are. It opens with the money-flow explanation above, then gives two
dropdowns — **withdraw $X** and **per $Y of lifetime gain** — over a 6 × 7 grid of
amounts (32 valid combinations; the rest withdraw more than the gain that triggers them,
which just duplicates the diagonal).

Picking a combination redraws the book, the per-seat curves and the yearly cash for a full
6.5-year run of that exact rule, with a stat strip of its medians across all 18 windows.
Below is a heatmap of the whole grid with a metric switcher — net, cash, wipeout rate,
blowups, seats bought — and every cell and table row is clickable to load it above. A
warning banner appears automatically for any rule that ever lost the whole book, and for
any rule that banks nothing and therefore stalls at 6 seats.

It costs about 2 minutes of the run time. Skip it with `--no-explore`.

Three of those came from `Accounts_starts_extended(LEGACY).py` — the individual curves,
the monthly P&L bars with their win-rate box, and the closed-vs-floating drawdown panels
— reimplemented in Plotly so there is a single artifact rather than a scatter of PNGs.
Charts drawn from the illustrative book are labelled as one path rather than an
expectation, because that is what they are.

## Notes

- The copied sweep files do not reveal the intratrade order of MAE and MFE, so the
  simulator keeps that as an explicit assumption (`--intratrade-path`).
- The seventh column in the copied sweep files is not a commission and is not treated as
  one.
- Results are only as good as the trade export underneath them, so a full account equity
  stream from MT5 is still the best source for final validation.
