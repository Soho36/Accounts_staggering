# Dynamic all-window signal routing

This is an exploratory routing study, not a live allocation rule. It keeps all 23 entry windows enabled and dispatches each isolated-window export to free accounts. `R` fixes requested copies per signal (market exposure); `K` is the number of paid prop-account drawdown buffers.

## Mechanical capacity

The raw tape has 5,726 completed/offered training entries and 6,932 test entries. Peak positive-duration overlap is 5. Therefore the observed no-death requirement for every copy is `K >= 5R`: 5, 10, 15, 20, 30, and 40 accounts for R=1, 2, 3, 4, 6, and 8 respectively.

| R | K=1 | K=2 | K=3 | K=4 | K=5 | K=6 | K=8 | K=10 | K=15 | K=20 | K=25 | K=30 | K=35 | K=40 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 73.15% | 95.64% | 99.50% | 99.99% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| 2 | 36.58% | 73.15% | 84.40% | 95.64% | 97.57% | 99.50% | 99.99% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| 3 | 24.38% | 48.77% | 73.15% | 80.65% | 88.15% | 95.64% | 98.21% | 99.66% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| 4 | 18.29% | 36.58% | 54.87% | 73.15% | 78.78% | 84.40% | 95.64% | 97.57% | 99.86% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| 6 | 12.19% | 24.38% | 36.58% | 48.77% | 60.96% | 73.15% | 80.65% | 88.15% | 97.57% | 99.66% | 99.99% | 100.00% | 100.00% | 100.00% |
| 8 | 9.14% | 18.29% | 27.43% | 36.58% | 45.72% | 54.87% | 73.15% | 78.78% | 92.83% | 97.57% | 99.56% | 99.86% | 99.99% | 100.00% |

Fill rate is filled copies / requested copies with immortal accounts. Partial rows are not strictly exposure-matched because congestion chooses which offers are omitted.

### Relation to the existing copied all-window book

In the 2023+ holdout, one current globally blocked account took 5,071 trades / 8,495 contract-hours / $388,422 nominal initial stop risk. One recovered all-window copy took 6,932 offers / 10,882 hours / $500,460 risk. The recovered tape therefore carries more exposure per copy.

| Existing copied accounts | Equivalent recovered-tape R by trades | by hours | by initial stop risk | Exact no-block K=5R (rounded exposure) |
|---:|---:|---:|---:|---:|
| 8 | 5.85 | 6.24 | 6.21 | about 30 (R=6) |
| 10 | 7.32 | 7.81 | 7.76 | about 40 (R=8) |

Thus 8-10 routed seats are enough for one or approximately two copies of every offer, but they cannot both preserve the exposure of 8-10 fully copied legacy accounts and eliminate overlap blocking. That higher exposure needs roughly R=6-8 and 30-40 seats for exact observed coverage.

## Prop-account economics

Assumptions: DD $2,500, frozen floor $100, seat cost $200, MAE-first, ratchet $200/$400, payout split 90%, and $1,000 replacement reserve in addition to K initial seat fees. Initial capital is therefore `reserve + K x seat cost`; all initial and replacement seat costs are charged. At most one replacement is bought per recorded exit-timestamp event.

The selection distribution uses 5 quarterly-started, overlapping two-year episodes ending by 2023-01-01; `Test cashout` is then evaluated once on 2023+. These selection episodes are strongly overlapping and contain roughly one independent regime, so their p10 is descriptive, not a calibrated probability.

| R | Defensible pilot boundary | Selection cashout p10 | Cashout med | Test fill | Test cashout | Test gain vs round-robin |
|---:|:---|---:|---:|---:|---:|---:|
| 1 | K=5, max_headroom | $404 | $1,520 | 100.00% | $18,041 | $6,537 |
| 2 | K=10, max_headroom | $808 | $3,040 | 100.00% | $36,083 | $13,074 |
| 3 | K=15, max_headroom | $1,212 | $4,560 | 100.00% | $54,124 | $19,612 |
| 4 | K=20, max_headroom | $1,616 | $6,080 | 100.00% | $72,166 | $26,149 |
| 6 | K=30, max_headroom | $2,424 | $9,120 | 100.00% | $108,249 | $39,223 |
| 8 | K=40, max_headroom | $3,232 | $12,160 | 100.00% | $144,332 | $52,298 |

At exact-capacity K/R pairs, max-headroom beat round-robin on pre-2023 episode median in 17/18 comparisons and on 2023+ cashout in 18/18. In the holdout their fill, contract-hours, nominal stop risk, and delivered raw P&L were identical; the cash difference therefore came from allocating outcomes among nonlinear DD/payout paths, not from selecting signals.

The pre-2023 fitted K above 5R was not stable in the holdout. The table therefore uses the mechanical K=5R boundary as the pilot default instead of calling an extra-seat count optimal. Cheaper near-full rows remain in the CSV but are not same-signal comparisons: congestion changed raw P&L. This is still only one small pre-2023 selection regime followed by one holdout; require a new untouched period before choosing a live rule.

## Interpretation rules

- Compare policies only within the same R, and confirm delivered fill, stop-risk   dollars, contract-hours, and raw net before attributing a cash difference to   routing.
- K above 5R cannot add raw exposure when all accounts are alive. It buys spare   DD containers; any benefit must exceed its seat cost and slower per-seat path   to the Safety Net.
- Routing removes the blocking objection to fixed schedules without disabling an   hour. It does not establish that recovered signals add alpha.
- The 23 hourly exports already enforce one-position blocking inside each window.   Missing within-window signals are unknowable from these files.
- Payout calendars, eligibility days/caps, activation delays, resets, and the real   account-tier price/DD menu are still absent. Values are conditional on the   simplified project rules.
- A death cluster groups positions by their recorded exit timestamp. MT5 exports   do not reveal the exact intratrade time of an MAE floor breach, so this is an   observable stress proxy, not proof that every liquidation was simultaneous.
