# Mode-2 allocation sweep

This is a reproducible diagnostic from `addiotional_helpers/allocation_sweep.py`.
It does not identify a statistically validated optimum.

## Fixed assumptions

- RR 1.00, all 23 complete hourly windows, one-position replay.
- MAE-first intratrade path and $1.05 round-turn commission.
- 18 quarterly-started, two-year books from 2020-04-01 to 2024-07-01.
  Each book starts flat and independently replays window competition; a position crossing the
  horizon can block another entry, but its own post-horizon outcome is not scored.
- The books overlap heavily. They represent only about 3-4 independent two-year regimes.
- Mode 2 only, one seat per event, $200 seat cost, $2,500 DD, $100 frozen floor,
  $200 per $400 gain-ratchet, 20-seat cap and 100% payout split unless a row says otherwise.
- Every row is in-sample on the same 2020-2026 history. P10 is descriptive, not a calibrated 10% forecast.
- Simulator file SHA-256 at artifact write time: `4dbf14d8ca774c3bc096b838489211273133addf2b49d113730f7d85c77bf4a0`.

## How to read the money columns

- **Realized** = terminal cash pot minus initial seed. Cumulative payouts are not used because
  payouts spent on replacement seats cannot also be counted as cash still owned.
- **Cashout** = realized plus only the endpoint-withdrawable cushion above the Safety Net on
  frozen seats. This is the primary decision metric here.
- **Mark** additionally credits positive P&L in every live prop account, including seats that
  are not yet payout-eligible. It is an optimistic continuation value, not cash.

## Cadence x seed

| case | cashout median | cashout p10 | realized median | mark median | ruin | 5+ shock | max cluster | bought median |
|---|---|---|---|---|---|---|---|---|
| seed $1,200, every 14d | $48,713 | -$1,200 | $25,700 | $85,053 | 22% | 17% | 6 | 20 |
| seed $1,200, every 21d | $39,991 | -$1,200 | $22,400 | $72,961 | 22% | 11% | 6 | 24 |
| seed $1,200, every 30d | $33,637 | -$1,200 | $18,200 | $61,787 | 17% | 0% | 4 | 21 |
| seed $1,200, every 45d | $23,321 | -$1,200 | $12,200 | $40,668 | 17% | 0% | 4 | 16 |
| seed $1,200, every 60d | $21,257 | -$222 | $11,500 | $35,397 | 6% | 0% | 3 | 13 |
| seed $1,200, every 90d | $14,390 | $140 | $7,600 | $22,935 | 0% | 0% | 2 | 9 |
| seed $2,000, every 14d | $62,053 | -$2,000 | $33,500 | $101,040 | 22% | 39% | 9 | 20 |
| seed $2,000, every 21d | $45,369 | -$2,000 | $25,100 | $78,766 | 17% | 28% | 8 | 24 |
| seed $2,000, every 30d | $38,769 | -$1,580 | $21,000 | $62,638 | 11% | 11% | 6 | 24 |
| seed $2,000, every 45d | $25,018 | $272 | $13,200 | $40,668 | 0% | 0% | 4 | 17 |
| seed $2,000, every 60d | $21,257 | $280 | $11,500 | $35,397 | 0% | 0% | 3 | 13 |
| seed $2,000, every 90d | $14,390 | $200 | $7,600 | $22,935 | 0% | 0% | 2 | 9 |
| seed $3,000, every 14d | $68,641 | -$3,000 | $37,900 | $107,628 | 17% | 44% | 12 | 20 |
| seed $3,000, every 21d | $49,567 | -$1,880 | $27,200 | $83,367 | 11% | 33% | 10 | 25 |
| seed $3,000, every 30d | $38,769 | $220 | $21,000 | $62,638 | 0% | 11% | 6 | 24 |
| seed $3,000, every 45d | $25,018 | $287 | $13,200 | $40,668 | 0% | 0% | 4 | 17 |
| seed $3,000, every 60d | $21,257 | $280 | $11,500 | $35,397 | 0% | 0% | 3 | 13 |
| seed $3,000, every 90d | $14,390 | $200 | $7,600 | $22,935 | 0% | 0% | 2 | 9 |
| seed $4,000, every 14d | $73,275 | -$3,225 | $40,100 | $112,262 | 6% | 39% | 12 | 25 |
| seed $4,000, every 21d | $49,567 | -$40 | $27,200 | $83,367 | 0% | 33% | 10 | 28 |
| seed $4,000, every 30d | $38,769 | $1,220 | $21,000 | $62,638 | 0% | 11% | 6 | 24 |
| seed $4,000, every 45d | $25,018 | $287 | $13,200 | $40,668 | 0% | 0% | 4 | 17 |
| seed $4,000, every 60d | $21,257 | $280 | $11,500 | $35,397 | 0% | 0% | 3 | 13 |
| seed $4,000, every 90d | $14,390 | $200 | $7,600 | $22,935 | 0% | 0% | 2 | 9 |

## Start triggers

All rows use a $3,000 seed. Profit/DD/any triggers are endogenous to the common market path;
high typical values can therefore be paid for by clustered tail loss.

| case | cashout median | cashout p10 | realized median | mark median | ruin | 5+ shock | max cluster | bought median |
|---|---|---|---|---|---|---|---|---|
| calendar 30d | $38,769 | $220 | $21,000 | $62,638 | 0% | 11% | 6 | 24 |
| calendar 45d | $25,018 | $287 | $13,200 | $40,668 | 0% | 0% | 4 | 17 |
| calendar 60d | $21,257 | $280 | $11,500 | $35,397 | 0% | 0% | 3 | 13 |
| profit trigger $400 | $60,728 | -$1,152 | $27,100 | $104,092 | 0% | 56% | 6 | 25 |
| profit trigger $800 | $33,958 | -$460 | $15,000 | $61,724 | 0% | 0% | 4 | 17 |
| profit trigger $1,000 | $28,511 | -$423 | $12,600 | $50,470 | 0% | 0% | 3 | 14 |
| profit trigger $1,500 | $20,193 | $32 | $9,000 | $36,015 | 0% | 0% | 3 | 10 |
| profit trigger $2,500 | $13,835 | $487 | $6,100 | $23,589 | 0% | 0% | 2 | 6 |
| drawdown trigger $200 | $97,563 | -$3,000 | $48,700 | $149,563 | 28% | 50% | 15 | 20 |
| drawdown trigger $400 | $89,490 | -$3,000 | $44,900 | $141,490 | 33% | 50% | 15 | 20 |
| drawdown trigger $800 | $85,809 | -$3,000 | $43,700 | $137,809 | 28% | 50% | 15 | 20 |
| drawdown trigger $1,200 | $87,108 | -$3,000 | $42,700 | $139,108 | 33% | 50% | 15 | 20 |
| drawdown trigger $2,000 | $41,564 | $2,380 | $20,400 | $64,182 | 6% | 67% | 15 | 21 |
| any trigger, min 7d | $89,649 | -$3,000 | $41,900 | $141,649 | 22% | 44% | 12 | 20 |
| any trigger, min 14d | $72,962 | -$3,000 | $39,000 | $112,011 | 17% | 44% | 11 | 22 |
| any trigger, min 30d | $36,501 | $220 | $19,400 | $62,638 | 0% | 11% | 6 | 24 |
| any trigger, min 45d | $25,319 | $287 | $13,200 | $40,668 | 0% | 0% | 4 | 17 |
| any trigger, min 60d | $19,108 | $420 | $10,400 | $35,397 | 0% | 0% | 3 | 13 |

## Withdrawal rules: $3,000 seed, 30-day cadence

The complete CSV also contains these rules at a $1,200 seed and at 45/60-day cadences.

| case | cashout median | cashout p10 | realized median | mark median | ruin | 5+ shock | max cluster | bought median |
|---|---|---|---|---|---|---|---|---|
| $200/$400; seed $3,000; 30d | $38,769 | $220 | $21,000 | $62,638 | 0% | 11% | 6 | 24 |
| $200/$600; seed $3,000; 30d | $42,062 | -$1,267 | $13,700 | $69,100 | 0% | 11% | 6 | 24 |
| $200/$1,000; seed $3,000; 30d | $43,669 | -$2,733 | $6,200 | $75,937 | 0% | 11% | 6 | 24 |
| $200/$2,000; seed $3,000; 30d | $42,421 | -$3,000 | $100 | $72,457 | 11% | 11% | 6 | 20 |
| $500/$2,500; seed $3,000; 30d | $42,767 | -$2,387 | $4,800 | $74,837 | 0% | 11% | 6 | 20 |
| $1,000/$2,000; seed $3,000; 30d | $36,769 | -$1,988 | $14,200 | $58,638 | 0% | 11% | 6 | 24 |
| $1,000/$5,000; seed $3,000; 30d | $42,108 | -$2,387 | $1,800 | $68,952 | 0% | 11% | 6 | 18 |
| $2,000/$4,000; seed $3,000; 30d | $42,052 | -$2,387 | $9,300 | $69,444 | 0% | 11% | 6 | 19 |

## Drawdown: same-price diagnostic

Every DD below is assigned the same $200 seat price and the same payout mechanics. Real firms
do not price tiers this way, so this table diagnoses the model mechanism; it is not a tier recommendation.

| case | cashout median | cashout p10 | realized median | mark median | ruin | 5+ shock | max cluster | bought median |
|---|---|---|---|---|---|---|---|---|
| DD $1,000; seed $3,000 | $17,126 | -$716 | $8,800 | $21,630 | 0% | 0% | 3 | 26 |
| DD $1,500; seed $3,000 | $24,299 | -$614 | $13,600 | $31,352 | 0% | 11% | 6 | 25 |
| DD $2,000; seed $3,000 | $31,075 | -$2,460 | $17,200 | $47,699 | 6% | 44% | 6 | 25 |
| DD $2,500; seed $3,000 | $38,769 | $220 | $21,000 | $62,638 | 0% | 11% | 6 | 24 |
| DD $3,000; seed $3,000 | $36,459 | -$860 | $19,600 | $69,300 | 0% | 22% | 8 | 24 |
| DD $4,000; seed $3,000 | $31,611 | -$3,000 | $14,900 | $83,250 | 0% | 44% | 10 | 20 |
| DD $5,000; seed $3,000 | $20,417 | -$3,000 | $7,800 | $90,575 | 0% | 11% | 9 | 20 |
| DD $6,500; seed $3,000 | $6,594 | -$3,000 | $1,100 | $87,059 | 0% | 11% | 10 | 19 |

## Seat cap

All rows use a $3,000 seed. The same MNQ signals drive every seat, so a higher cap is leverage,
not strategy diversification.

| case | cashout median | cashout p10 | ruin | 5+ shock | max cluster |
|---|---|---|---|---|---|
| cap 1; every 30d | $6,959 | $793 | 0% | 0% | 1 |
| cap 2; every 30d | $12,423 | $985 | 0% | 0% | 2 |
| cap 3; every 30d | $16,795 | $988 | 0% | 0% | 3 |
| cap 5; every 30d | $24,337 | $955 | 0% | 0% | 4 |
| cap 8; every 30d | $35,257 | $640 | 0% | 6% | 5 |
| cap 10; every 30d | $36,920 | $360 | 0% | 11% | 6 |
| cap 15; every 30d | $38,563 | $220 | 0% | 11% | 6 |
| cap 20; every 30d | $38,769 | $220 | 0% | 11% | 6 |
| cap 25; every 30d | $38,569 | $220 | 0% | 11% | 6 |
| cap 1; every 45d | $6,959 | $793 | 0% | 0% | 1 |
| cap 2; every 45d | $11,093 | $540 | 0% | 0% | 2 |
| cap 3; every 45d | $15,143 | $654 | 0% | 0% | 3 |
| cap 5; every 45d | $23,393 | $340 | 0% | 0% | 4 |
| cap 8; every 45d | $24,505 | $287 | 0% | 0% | 4 |
| cap 10; every 45d | $25,084 | $287 | 0% | 0% | 4 |
| cap 15; every 45d | $25,018 | $287 | 0% | 0% | 4 |
| cap 20; every 45d | $25,018 | $287 | 0% | 0% | 4 |
| cap 25; every 45d | $25,018 | $287 | 0% | 0% | 4 |

## Payout split sensitivity

The 100% split used elsewhere is optimistic. At 80-90%, each gross withdrawal delivers less
cash to the replacement pot, which can reverse a small positive p10.

| case | cashout median | cashout p10 | ruin | 5+ shock | max cluster |
|---|---|---|---|---|---|
| 80%; seed $2,000; 30d | $29,636 | -$1,864 | 11% | 11% | 6 |
| 90%; seed $2,000; 30d | $33,941 | -$1,692 | 11% | 11% | 6 |
| 100%; seed $2,000; 30d | $38,769 | -$1,580 | 11% | 11% | 6 |
| 80%; seed $2,000; 45d | $19,334 | -$398 | 0% | 0% | 4 |
| 90%; seed $2,000; 45d | $22,176 | -$63 | 0% | 0% | 4 |
| 100%; seed $2,000; 45d | $25,018 | $272 | 0% | 0% | 4 |
| 80%; seed $2,000; 60d | $16,486 | -$296 | 0% | 0% | 3 |
| 90%; seed $2,000; 60d | $18,872 | -$8 | 0% | 0% | 3 |
| 100%; seed $2,000; 60d | $21,257 | $280 | 0% | 0% | 3 |
| 80%; seed $3,000; 30d | $30,055 | -$708 | 0% | 11% | 6 |
| 90%; seed $3,000; 30d | $34,412 | -$244 | 0% | 11% | 6 |
| 100%; seed $3,000; 30d | $38,769 | $220 | 0% | 11% | 6 |
| 80%; seed $3,000; 45d | $19,334 | -$450 | 0% | 0% | 4 |
| 90%; seed $3,000; 45d | $22,176 | -$81 | 0% | 0% | 4 |
| 100%; seed $3,000; 45d | $25,018 | $287 | 0% | 0% | 4 |
| 80%; seed $3,000; 60d | $16,486 | -$296 | 0% | 0% | 3 |
| 90%; seed $3,000; 60d | $18,872 | -$8 | 0% | 0% | 3 |
| 100%; seed $3,000; 60d | $21,257 | $280 | 0% | 0% | 3 |

## Observed diagnostic takeaways

- With a $1,200 seed, 90 days was the only tested cadence with zero observed ruin. Under the
  optimistic 100% payout split, 45 days was the balanced observed point at a $2,000-$3,000
  seed, while 30 days retained more typical upside but produced a 5+ seat same-trade shock.
  At an 80-90% split that small positive 45-day p10 disappears, so this is not yet a live rule.
- Profit and drawdown start triggers did not diversify entries. Aggressive triggers clustered
  purchases into the same market state and produced poor p10/shock combinations.
- At $3,000/30 days, $200 per $1,000 increased typical cashout versus $200 per $400 but starved
  the replacement pot and made p10 materially worse. The ratchet step remains an unvalidated
  free parameter.
- Under equal $200 pricing, $2,500 DD was the observed cashout sweet spot. Larger DD tiers looked
  much better only in mark-to-model value because more profit remained trapped below payout level.
- Cashout flattened around an 8-10 seat cap. Expanding toward 20 added little typical cashout
  and introduced 5+ seat shocks in the observed sample.

## Required before a real tier allocation

The repository has no firm menu mapping DD to seat/activation/reset cost, payout split,
eligibility threshold, minimum/maximum payout, frequency/consistency rules, trailing-floor
details or account cap. The simulator also applies one DD/cost rule to the whole book and cannot
yet score a mixed-tier portfolio. Obtain that menu, model it exactly, then validate candidate
policies chronologically or by block bootstrap. Do not optimize from these 18 overlapping rows.
