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
.\venv\Scripts\python.exe account_farming.py --dd 2500 --cost 200 --seats 20 --seed 1200
```

Use the project venv for the helpers as well. The commands in this README use the working
Windows junction at `I:\PycharmProjects\Accounts_staggering`.

Needs `plotly` for the HTML report (`pip install plotly`); without it the script still
runs and writes the CSVs, and simply skips the report.

It answers two questions that used to live in two scripts. *"How does a staggered book
behave?"* — start policies, per-seat survival, the book over time. And *"is buying seats
a better business than tuning the strategy?"* — which needs three things a plain
per-account simulation does not model:

1. **Single-position replay.** The EA holds one position at a time, so with every
   hourly window enabled they compete for one slot. Each sweep file was generated
   with only its own window active, so concatenating them counts entries that could
   never have been taken — **3,359 of 12,658 (27%)** on this data. The old script
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
equity drawdown exactly). **`mfe-first` is the conservative case, not the optimistic one:**
it lifts the peak — and therefore the floor — before the adverse excursion is tested
against it, so the floor is always at least as high and liquidation always at least as
likely. A higher floor is less cushion, never more. On this data the two orderings give
near-identical results on this data (the freeze rates differed by 0.2pp on the 22-window
set; the ordering, not the magnitude, is the point).

Commission is hardcoded at **$1.05 round turn** (`COMMISSION_ROUNDTURN`) — it is the
broker's real figure, not a tuning knob. The seventh column in the sweep exports is *not*
a commission, so it is preserved as `Extra` and ignored.

## ⚠ Historical figures versus the current decision artifacts

Sections below preserve the investigation history and explain why earlier recommendations
changed. They contain dated/default-seed examples and should not be treated as the current
operating table. For current, reproducible decision numbers use:

- `results/allocation_sweep.md` for Mode 2 seed/cadence/DD/cap/withdrawal decisions;
- `results/schedule_oos.md` for the time-window schedule hypothesis; and
- `results/farming_withdrawal_policies.csv` for the exact settings shown in the latest HTML.

Those artifacts distinguish realized cash, safely cashable endpoint value, optimistic prop
mark, 5+ seat shocks, and full-book extinction. Rerun them after any simulator change.

### Current decision summary (2026-08-14)

- Use **Mode 2**, fixed **RR 1.00**, and keep all 23 windows for now. The RR 2.00/2.50
  comparisons are invalid because `16-17` was tester-truncated. A separate RR per hour has
  not produced a reliable out-of-sample improvement.
- Do not turn off hours from this sample. Of eight windows that lost in 2020-2022, only two
  also lost from 2023 onward. A train-only schedule rule won 3 of 6 rolling test years;
  its exact paired sign-flip p-value was 0.625. Bad hours exist in hindsight, but this data
  has not identified them prospectively.
- A disjoint schedule does remove cross-window blocking, but it also changes contract
  exposure. With 23 dedicated hourly seats the 2023+ book took only 5.9% as many
  contract-trades as 23 identical all-window seats. Most of the apparent drawdown reduction
  is therefore de-levering/time-of-day weighting, not evidence that filtering found alpha.
- For starts, buy **one seat per event on a fixed calendar**. At the deliberately optimistic
  100% split, a $3,000 seed and 45-day starts produced cashout median/p10 of $25,018/$287,
  with 0/18 observed ruin and 0/18 five-seat shocks. At 90% and 80% payout splits its p10
  became -$81 and -$450. Those are overlapping in-sample windows, so none of these is a
  claim of safety or a live optimum.
- A provisional cap of **8-10 live seats** captured nearly all of the 45-day median in this
  model. Raising the cap to 20 added no typical cashout there. At 30 days it added little
  median and increased observed liquidation clustering.
- The risk-first withdrawal diagnostic is the **$200 per $400 gain ratchet**. At $3,000/30d,
  $200 per $1,000 raised median cashout from $38,769 to $43,669 but lowered p10 from $220
  to -$2,733 and left much less realized cash. The ratchet remains fitted in-sample.
- The equal-price mechanism test favored a $2,500 DD, but it is not a tier recommendation.
  A real choice needs the firm's price, activation/reset/renewal cost, payout split and
  eligibility rules, contract limit, floor behavior and delays for every tier. The current
  engine cannot yet compare a mixed-tier book.

### Figures restated on 2026-08-13

Every number in this file before that date was computed on **22 windows instead of 23**.
`pass_is_blown()` flagged a pass as tester-liquidated when its `equity_dd` reached 95% of
the $5,000 deposit. Window `16-17` drew down $4,774 — above that line — while completing
all 904 trades and finishing at −$426. It was never liquidated; the filter was simply
wrong, and it silently dropped a valid window from every study in the project.

`16-17` happens to be the most volatile window in the set, so restoring it moves things a
long way:

| | 22 windows (old, wrong) | **23 windows (correct)** |
|---|---|---|
| trades | 8,645 | 9,299 |
| net after commission | $31,118 | **$27,683** |
| max floating drawdown | $5,676 | **$6,567** |
| a seat reaches the Safety Net | 87.5% | **75.8%** |
| fresh seat, 1-year liquidation | 24.5% | **44.6%** |

The direction of every conclusion survived. Several magnitudes did not. The fix is in
`pass_is_blown`: only a wiped-out account (`net_profit <= -95% of deposit`) truncates an
export; a large drawdown does not.

## What has been established so far

Measured on RR strategy, **all 23 windows**, RR 1.0, 2020-2026, $2,500 trailing DD, $200
per seat, $1.05 round turn, a new seat every 30 days.

- **A seat reaches the Safety Net 75.8%** of the time (starts with a full year of runway),
  median **142 days**. **24.2% die before ever freezing.**
- **Do not harvest as hard as the rules allow.** An earlier note here said the opposite —
  that stripping seats to the Safety Net beat every softer level. It does maximise realized
  cash, but it also synchronises the book: it loses a book of 5+ seats in **28–83%** of
  windows, against **0%** for every ratchet rate tested.
- **Never withdrawing earns nothing withdrawable**, no matter how well it trades — under
  bootstrap funding it can never afford a second seat (median 6 seats, $0 cash).
- **$1,200 of seed is no longer enough.** With 23 windows the bootstrap's ruin rate at that
  seed is **17%**, not 0%, and net p10 is −$1,200 — you lose the whole stake in the bottom
  decile. Ruin only reaches 0% at a **$3,000** seed. See the seed table below.
- **Per seat-year, measured per start date: median $622, mean $1,513** (starts with a full
  year of runway). This is the policy-independent version and the one to quote.

  Do **not** quote a book-level seat-year figure. It is not a property of the strategy, it
  is a property of whichever policy you featured — a policy that keeps its seats alive
  concentrates the same profit into far fewer seat-years, so the same data yields anything
  from $4,780 to $21,890 depending on which book you point at. Earlier notes here quoted
  $3,850 and then $4,780 without stating the denominator; neither is comparable to
  anything.

  For context, the per-window RR-tuned allocation this project split off from returned
  ≈ $778 per seat-year — that figure comes from the source project and has not been
  re-derived here.

## The two modes

The simulator runs both funding models and scores every policy across all windows. For
this project the operating objective is **Mode 2**; Mode 1 is retained only as a diagnostic
benchmark and is not a recommendation.

**Mode 1 — subscription.** Own money, one seat every `--interval-days`, never withdraw,
hold forever. Cannot be ruined, because funding is external. It fills to the seat cap and
then only replaces deaths. **`--seed` does not apply here** — mode 1 has no pot. Its own
capital is `spent`, which accrues $200 at a time, on the interval, for as long as you keep
running it (median $4,800 per 2-year window; $5,000 over the full sample).

**Mode 2 — bootstrap.** The seats pay for their own replacements out of withdrawn cash.
This is the one that can die permanently.

There is exactly one pot of cash, and **`--seed` is only ever this pot** — the one-time
payment of your own money that gets the first seats trading. It is the *whole* of your
capital in mode 2; nothing else goes in after it.

The pot starts at `--seed`. A frozen seat pays money *into* it when the withdrawal rule
fires; buying a seat takes `--cost` *out* of it. Nothing else adds to it.

So both modes spend your own money, and the difference is the schedule: mode 1 pays $200
forever, on the interval; mode 2 pays once at the start and never again. The report now
keeps three endpoints separate: realized pot cash, cash plus safely withdrawable surplus,
and an optimistic mark that credits positive P&L in all live prop accounts. So **withdrawals are the only thing that funds growth** — which is why
"never withdraw" under bootstrap funding is not a strategy but a dead end: at $1,200 seed
and $200 a seat it buys exactly **6 seats**, one per interval, and then the pot is empty
permanently. That is the whole explanation for the 6 in the table below.

#### Equity is not buying power

Purchases come out of the **cash balance**, never out of equity, and the balance is
usually at or near zero because every dollar that arrives is spent on the next seat almost
immediately. A book can therefore sit for a year holding $22,000 of equity and buy nothing.

That is not a stall in the model, it is the ratchet working per seat. The rule pays out on
**each seat's own** lifetime gain, so under `$200 per $1,000` a seat pays nothing until it
has personally cleared $3,600 ($2,600 Safety Net + the $1,000 that triggers a payout). Nine
seats averaging $2,500 are worth $22,500 together and are every one of them still below the
Safety Net individually — so not one owes a withdrawal, and the pot stays empty.

The explorer has a dedicated log-scale "cash on hand" panel for exactly this: below the
$200 line the book cannot buy, whatever its equity says.

#### Why `--seed 1200`, and how low it can go

$1,200 is six seats at $200. It came in with the original script (commit `b546d69`) rather
than being derived from anything.

The only hard floor is **`seed >= cost`**. Below one seat's price it is a *deadlock*, not a
slow start: buying needs cash, cash only ever arrives from a withdrawal, and a withdrawal
needs a live seat. `--seed 100` used to produce a silent run of zeroes flagged "ruined";
it now errors out and says why.

So **`--seed 200` is the purest bootstrap** — one seat of your own money, every seat after
it paid for out of profit. Swept across the 18 windows at `$200 per $400`:

| seed | ruin rate | collapse rate | net p10 | net median | seats |
|---|---|---|---|---|---|
| $200 | **39%** | 44% | −$200 | $21,230 | 13 |
| $400 | 33% | 44% | −$400 | $32,481 | 16 |
| $600 | 28% | 39% | −$600 | $42,248 | 17 |
| $800 | 22% | 39% | −$800 | $58,909 | 20 |
| $1,000 | 22% | 39% | −$1,000 | $61,418 | 20 |
| **$1,200 (default)** | **17%** | 33% | **−$1,200** | $62,638 | 21 |
| $2,000 | 11% | 33% | −$376 | $62,638 | 23 |
| **$3,000** | **0%** | 28% | **+$4,707** | $62,638 | 24 |
| $4,000 | 0% | 28% | +$5,234 | $62,638 | 24 |

**The default seed is no longer safe.** On the correct 23 windows, $1,200 leaves a 17%
ruin rate and a p10 of −$1,200 — the bottom decile loses the entire stake. Ruin only
reaches zero at **$3,000**, which is also where p10 first turns positive.

The direct cause: **31.6% of possible start dates give a seat that dies without ever paying
out $200** (it was 21% on the 22-window data), and from a one-seat book that is game over
on the spot.

Note where the curve flattens. Median net is identical from $1,200 upward — **extra seed
buys no upside at all, only survival**. It moves p10 from −$1,200 to +$5,234 and ruin from
17% to 0%, and does nothing to the typical outcome. Seed is insurance, not fuel. The
inherited $1,200 default looked like it sat exactly at the zero-ruin point on the 22-window
data; that was an artefact of the missing window, which is a good illustration of why a
parameter that lands suspiciously well deserves suspicion rather than confidence.

### Why a book dies all at once, and what actually fixes it

Two seats bought on the same trade are not merely correlated, they are **identical** —
same rule, same stream, same withdrawal policy — so their curves coincide to the cent and
they liquidate on the same trade.

The deeper problem is that **withdrawing down to a level recreates that condition even
for seats with different start dates.** They hold different equity right up until they
both freeze and get stripped to the same number, and from then on they are twins. The
withdrawal policy is what synchronises a book.

That is visible in the numbers. Buying one seat at a time is *not* sufficient on its own:

"Collapse" below means a window in which a book of **5 or more** seats was lost at once.
Counting *any* drop to zero seats conflates that with the single starter seat dying in the
first weeks, which nearly every policy does once and which is not the same event.

| policy | collapse rate | biggest book lost | ruin | blowups | cash in hand | net median |
|---|---|---|---|---|---|---|
| all-in, strip to net | **83%** | **20** | 39% | 27 | $108,955 | $152,032 |
| 1/interval, strip to net | **28%** | **9** | 11% | 10 | $35,006 | $43,581 |
| 1/interval, keep $4,000 | 11% | 9 | 17% | 7 | $12,820 | $29,588 |
| 1/interval, **$200 per $400** (50%) | **0%** | 1 | 17% | 6 | $19,900 | **$62,638** |
| 1/interval, $200 per $600 (33%) | **0%** | 1 | 22% | 6 | $12,900 | $62,623 |
| 1/interval, $200 per $1,000 (20%) | **0%** | 1 | 28% | 5 | $5,800 | $59,066 |
| 1/interval, $2,000 per $4,000 (50%) | **0%** | 1 | 17% | 5 | $7,300 | $54,491 |
| 1/interval, $1,000 per $2,000 (50%) | **0%** | 1 | 17% | 5 | $12,900 | $52,213 |
| 1/interval, $200 per $2,000 (10%) | **0%** | 1 | 28% | 5 | $1,200 | $51,344 |
| 1/interval, $1,000 per $5,000 (20%) | **0%** | 1 | 17% | 5 | $2,800 | $40,492 |
| 1/interval, never withdraw | 0% | 1 | 17% | 2 | $0 | $35,797 |
| subscription (mode 1) | 0% | 1 | n/a | 6 | $0 | **$74,337** |

Note what the restated numbers do to the ranking: **the subscription now has the best net
median of anything that doesn't collapse** ($74,337), where on 22 windows the ratchet edged
it. And every bootstrap policy now carries a 17–39% ruin rate at the $1,200 default seed,
where before they were at 0%.

**The ratchet is the fix.** Withdrawing a *share of gains* rather than stripping to a
level never lost a book of more than **one** seat, at any rate tested, because no seat is
ever reset to a common equity and the book keeps its dispersion. The level policies lost
books of **9 and 20**. Withdrawing $200 per $400 gained also beats strip-to-net on **net**
($62,638 vs $43,581) despite banking less cash, because far more equity survives.

**Chunk size matters on its own, not just the rate.** At an identical 20%, taking $200
per $1,000 nets $59,066 while taking $1,000 per $5,000 nets $40,492 — bigger, rarer
withdrawals leave the seat further above the Safety Net for longer, which cuts blowups but
also starves seat purchases. That is why the policies are specified in money terms rather
than as a percentage: two policies with the same rate are not the same policy.

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

### The all-in book looks best on one path and is the worst bet on the distribution

On the restated data, running one full 6.5-year path from January 2020 is brutal for every
bootstrap policy — the early drawdown arrives before the book has any cushion, and a
$1,200 seed cannot absorb it:

| one full 6.5y path | own money in | cash out | equity left | **net** | blowups | collapses |
|---|---|---|---|---|---|---|
| subscription | $9,400 | $0 | $474,436 | **$465,036** | 27 | 0 |
| all-in, strip to net | $1,200 | $2,303 | $0 | **−$1,097** | 17 | 1 |
| 1/interval, strip to net | $1,200 | $282,955 | −$187 | **$270,968** | 58 | 6 |
| $200 per $400 | $1,200 | $600 | $0 | **−$1,200** | 9 | 1 |

Two of the three bootstrap books **died outright**, including the one the sweep ranks best
on median. The third survived and returned $270,968. That spread — total loss to a quarter
million, same rule family, same data — is the clearest statement in this project of how
much a single path is worth as evidence: nothing.

Across all 18 windows:

| | ended below what you put in | worst window | collapse rate | ruin | net p10 | net median |
|---|---|---|---|---|---|---|
| subscription | 6% | −$4,115 | **0%** | n/a | **$6,800** | **$74,337** |
| all-in, strip to net | **39%** | −$1,200 | **83%** | 39% | −$1,200 | $152,032 |
| 1/interval, strip to net | 17% | −$1,503 | 28% | 11% | −$1,158 | $43,581 |
| $200 per $400 | 17% | −$1,200 | **0%** | 17% | −$1,200 | $62,638 |

All-in still has the highest median net by a wide margin — and it now ends **below the
money you put in 39% of the time**, loses a 20-seat book in **83%** of windows, and is
outright ruined in 39%. It is the highest-median and the worst bet in the table by a
larger margin than before.

The comparison that matters most has also flipped:

| | Mode 1 subscription | Mode 2, $200 per $400 |
|---|---|---|
| own capital | $4,800 per 2y, paid $200 at a time | $1,200 seed, paid once |
| net median | **$74,337** | $62,638 |
| net p10 | **$6,800** | **−$1,200** |
| ended below what you put in | **6%** | 17% |
| ruin | **impossible** (external funding) | **17%** |
| collapse rate | 0% | 0% |

On 22 windows the ratchet narrowly beat the subscription. On the correct 23 it does not —
the subscription wins on median, on p10, on below-water rate, and it cannot be ruined at
all. The bootstrap's advantage was always that it needs a quarter of the capital and banks
half its return as cash; that remains true, and it now costs a 17% chance of losing the
stake entirely.

Note that `ruin_rate` hides wipeouts: ruin means ending with no seats *and* too little
cash to replace one. A book that goes to zero seats and immediately rebuys is not
"ruined" by that definition. Read the **windows with a wipeout** column first — and note
it is a *rate*, not a median, because a median of 0 concealed a policy that wiped out six
times over the full run.

### Metric definitions, because two of them were wrong once

- **cash in hand** is the pot *balance* at the end — what is left after seats were rebought
  out of it — and it is already net of `--split`. **payout received** is the cumulative
  post-split amount received; **gross requested** is tracked separately. An earlier version
  labelled the balance as "realized cash withdrawn", which conflated them.
- **realized** = pot cash − own capital. **cashout** adds only surplus currently
  withdrawable above the Safety Net on frozen seats. **mark-to-model** also credits
  positive P&L in all live accounts, even when not payout-eligible; it is not cash.
- **5+ seat shock rate** includes any trade liquidating at least five seats, even when
  other seats survive. **full-collapse rate** is the stricter subset that takes a 5+ seat
  book to zero. The previous single "collapse" metric only saw full extinction and missed,
  for example, a 19-of-20 liquidation.

### Read these caveats before trusting any of it

- **This is leverage, not alpha.** N seats is N contracts. Every seat trades identical
  signals and differs only by start date, so a drawdown deep enough to kill one is deep
  enough to kill the book. Staggering spreads entry points, not outcomes.
- **The windows overlap heavily.** 18 two-year windows over a 6.5-year sample is more
  like 3–4 independent periods. Treat p10/p90 as shape, not precise quantiles.
- **The reconstruction runs ~5% light on trades** vs a real MT5 run of the same 23-window
  config (9,299 vs 9,775) because the sweeps were generated in isolation, so this merge
  approximates the blocking rather than reproducing it. Its **drawdown comes out worse**
  than MT5's ($6,567 vs $6,069) because $1.05/round-turn is charged here and was not in
  that tester run — so these figures are the conservative side of the comparison, which is
  the right side for a risk decision.
- **The withdrawal level is a real free parameter** and has not been validated
  out-of-sample. It should be, before it is trusted.
- **One filter bug cost this project two months of wrong numbers.** Anything derived from
  a heuristic over the sweep metadata deserves a sanity check against the raw files.

### RR: keep it simple; two old rows were invalid

Swept over the 23 windows, all in-sample:

| RR | reaches Safety Net | mean days | 5+ seat shock | optimistic mark median |
|---|---|---|---|---|
| 0.50 | 49% | 127 | 39% | $1,474 |
| 0.75 | 67% | 201 | 11% | $36,042 |
| **1.00** | **76%** | 155 | **0%** | **$61,787** |
| 1.25 | 73% | 173 | 28% | $39,241 |
| 1.50 | 62% | 126 | 33% | $26,227 |
| 2.00 | **INVALID** | — | — | tester-truncated `16-17` pass |
| 2.50 | **INVALID** | — | — | tester-truncated `16-17` pass |
| 3.00 | 61% | 107 | 22% | $49,175 |

The former RR 2.00 and 2.50 rows silently used only 22 windows: MT5 liquidated/truncated
`16-17` in 2022 and `build_stream()` dropped it. Strict mode now rejects those universes.
Regenerate signal exports with a tester setup that cannot terminate signal generation
before comparing them. RR 1.00 remains the conservative operational default; per-window
RR fitting has not shown a reliable out-of-sample gain large enough to justify its extra
degrees of freedom.

## Options

Input:

- `--input-csv PATH` — run from a single CSV export instead of the sweep folder
- `--sweep-root PATH` / `--stats-root PATH` — point at different sweep directories
- `--rr VALUE` — choose the RR file suffix to load, such as `1.00`
- `--windows 9-10,10-11` — restrict the hourly windows to test
- `--start-date` / `--end-date` — inclusive `YYYY-MM-DD` filters
- `--allow-incomplete` — diagnostic escape hatch for missing/truncated MT5 passes. Never
  use it for an RR or window comparison; strict rejection is the default

Account rule:

- `--dd N` (alias `--dd-limit`) — trailing drawdown; the Safety Net follows from it
- `--frozen-dd-floor N` — the fixed floor once the trailing DD freezes
- `--intratrade-path mae-first|mfe-first` — MAE/MFE ordering inside a trade

The book:

- `--cost N` — price of one seat
- `--seats N` — cap on concurrent accounts (a firm rule, not a maths question)
- `--seed N` — the one-time pot of own money for **bootstrap mode only**; mode 1 ignores
  it. Must be at least `--cost`, or the book can never buy its first seat
- `--interval-days N` — calendar days between new account starts
- `--start-policy time|profit|dd|any` — what triggers a start; `any` combines them
- `--profit-trigger N` / `--dd-trigger N` — thresholds for those triggers
- `--min-days-between-starts N` — floor on start spacing
- `--max-per-event N` — seats bought at one time. **1 is the staggering fix**; higher
  stacks identical seats that then die together
- `--withdraw-chunk N` / `--withdraw-step N` — the ratchet. One chunk withdrawn per
  `step` of lifetime gain, so `chunk/step` is the withdrawal rate (200/1000 = 20%)
- `--no-explore` — skip `bootstrap_explorer.html`, which is most of the run time
- `--split N` — trader's share of a payout (real plans are 0.8–0.9). Applied **when the
  money arrives**, so at 80% only 80% ever reaches the pot and only 80% is available to
  buy the next seat
- `--horizon N` — years per book window in the robustness sweep

## Output

Written into `results/`:

- `account_farming.html` — the full report (see below)
- `bootstrap_explorer.html` — a page dedicated to mode 2 alone (see below)
- `farming_starts.csv` — one row per possible seat start date
- `farming_withdrawal_policies.csv` — the robustness sweep, both modes
- `farming_book_seats.csv` — per-seat summary of the illustrative bootstrap book
- `farming_subscription_seats.csv` — the same for the subscription book
- `allocation_sweep.csv` / `allocation_sweep.md` — reproducible Mode 2 cadence, seed,
  withdrawal, DD, cap, trigger, and payout-split diagnostics
- `schedule_oos.csv` / `schedule_oos.md` / `schedule_oos_rolling_years.csv` — raw
  train/test and rolling-year diagnostics for the disjoint-window schedule hypothesis

The HTML report has nine sections: whether a seat reaches the Safety Net by start date;
closed vs floating drawdown; mode 1 with its portfolio curve and per-seat curves; mode 2
with a **policy switcher** that redraws the book, the seat curves and the yearly cash for
any policy in the sweep; both modes across every window plus a cashout-position comparison;
**the decision section**; monthly P&L; the full policy table; and a reconstruction check
against a real MT5 run.

### The decision section

This is the part that answers "which one do I pick". It plots every policy as typical
cashout (median) against bad-case cashout (p10). A policy with another one above **and**
to the right is dominated on those two axes; it can still differ in realized cash, ruin,
or liquidation shocks. Dominated-on-cashout points are drawn hollow and unlabelled; what
remains is that two-axis frontier. Risk is reported separately as same-trade death shocks,
whole-book extinction, and terminal ruin.

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
Below is a heatmap of the whole grid with a metric switcher — cashout, pot cash, 5+ shock rate,
blowups, seats bought — and every cell and table row is clickable to load it above. A
warning banner appears automatically for any rule with a five-or-more-seat same-trade
liquidation shock, and for any rule that banks nothing and therefore stalls at 6 seats.

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
