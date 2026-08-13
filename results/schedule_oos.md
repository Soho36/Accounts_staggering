# RR 1.00 raw schedule OOS diagnostic

> **Scope:** raw trade-stream diagnostic only. It does **not** model prop-account deaths, payouts, withdrawals, seat costs, replacements, or farming economics.

All comparisons use the exact validated 23-window universe and $1.05 round-turn commission. Train is 2020-2022; test begins 2023-01-01. Both start flat, and the allocation is fixed from train only. A training trade crossing the test boundary is purged.

`round_robin_schedule` assigns chronological windows cyclically across K seats and replays the one-position rule inside each seat's group. `identical_all_window` repeats the same all-window replay K times.

## Train (2020-2022)

| K | variant | contract-trades | vs same-K all-window | contract-hours | hours vs same-K all-window | raw net | worst template equity DD | aggregate closed DD | aggregate daily DD | max concurrent |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | identical_all_window | 4,228 | 100.0% | 6,795 | 100.0% | $2,938 | $4,602 | $4,468 | $4,309 | 1 |
| 1 | round_robin_schedule | 4,228 | 100.0% | 6,795 | 100.0% | $2,938 | $4,602 | $4,468 | $4,309 | 1 |
| 2 | identical_all_window | 8,456 | 100.0% | 13,590 | 100.0% | $5,875 | $4,602 | $8,936 | $8,618 | 2 |
| 2 | round_robin_schedule | 5,011 | 59.3% | 7,834 | 57.6% | $9,955 | $3,062 | $4,906 | $4,682 | 2 |
| 3 | identical_all_window | 12,684 | 100.0% | 20,385 | 100.0% | $8,813 | $4,602 | $13,404 | $12,927 | 3 |
| 3 | round_robin_schedule | 5,373 | 42.4% | 8,303 | 40.7% | $7,040 | $6,637 | $5,261 | $5,103 | 3 |
| 4 | identical_all_window | 16,912 | 100.0% | 27,180 | 100.0% | $11,750 | $4,602 | $17,872 | $17,235 | 4 |
| 4 | round_robin_schedule | 5,539 | 32.8% | 8,475 | 31.2% | $9,065 | $4,330 | $5,345 | $5,165 | 4 |
| 6 | identical_all_window | 25,368 | 100.0% | 40,770 | 100.0% | $17,626 | $4,602 | $26,808 | $25,853 | 6 |
| 6 | round_robin_schedule | 5,675 | 22.4% | 8,639 | 21.2% | $8,269 | $6,804 | $5,719 | $5,584 | 5 |
| 23 | identical_all_window | 97,244 | 100.0% | 156,285 | 100.0% | $67,565 | $4,602 | $102,763 | $99,104 | 23 |
| 23 | round_robin_schedule | 5,726 | 5.9% | 8,688 | 5.6% | $8,623 | $4,521 | $5,586 | $5,452 | 5 |
| 23 | top20_plus_3_train_duplicates | 5,454 | 5.6% | 8,891 | 5.7% | $25,734 | $2,412 | $3,927 | $3,744 | 7 |

## Test (2023+)

| K | variant | contract-trades | vs same-K all-window | contract-hours | hours vs same-K all-window | raw net | worst template equity DD | aggregate closed DD | aggregate daily DD | max concurrent |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | identical_all_window | 5,071 | 100.0% | 8,495 | 100.0% | $24,745 | $6,567 | $6,468 | $6,379 | 1 |
| 1 | round_robin_schedule | 5,071 | 100.0% | 8,495 | 100.0% | $24,745 | $6,567 | $6,468 | $6,379 | 1 |
| 2 | identical_all_window | 10,142 | 100.0% | 16,989 | 100.0% | $49,490 | $6,567 | $12,936 | $12,757 | 2 |
| 2 | round_robin_schedule | 6,045 | 59.6% | 9,780 | 57.6% | $23,059 | $5,700 | $7,604 | $7,569 | 2 |
| 3 | identical_all_window | 15,213 | 100.0% | 25,484 | 100.0% | $74,235 | $6,567 | $19,405 | $19,136 | 3 |
| 3 | round_robin_schedule | 6,485 | 42.6% | 10,377 | 40.7% | $25,743 | $5,681 | $6,363 | $6,292 | 3 |
| 4 | identical_all_window | 20,284 | 100.0% | 33,979 | 100.0% | $98,980 | $6,567 | $25,873 | $25,515 | 4 |
| 4 | round_robin_schedule | 6,695 | 33.0% | 10,608 | 31.2% | $26,763 | $5,116 | $7,500 | $7,429 | 4 |
| 6 | identical_all_window | 30,426 | 100.0% | 50,968 | 100.0% | $148,470 | $6,567 | $38,809 | $38,273 | 6 |
| 6 | round_robin_schedule | 6,859 | 22.5% | 10,804 | 21.2% | $25,925 | $4,241 | $7,011 | $6,940 | 5 |
| 23 | identical_all_window | 116,633 | 100.0% | 195,376 | 100.0% | $569,134 | $6,567 | $148,769 | $146,711 | 23 |
| 23 | round_robin_schedule | 6,932 | 5.9% | 10,882 | 5.6% | $25,804 | $4,962 | $7,089 | $7,018 | 5 |
| 23 | top20_plus_3_train_duplicates | 6,591 | 5.7% | 10,762 | 5.5% | $31,554 | $4,566 | $6,754 | $6,447 | 6 |

## Locked K=23 candidate

The candidate keeps the 20 best standalone windows by **train-only** raw net, then assigns one additional seat to each of the top three. Duplicate seats are counted as duplicate contract exposure; no test result enters the ranking.

- Selected 20: 17-18, 20-21, 14-15, 5-6, 7-8, 8-9, 2-3, 3-4, 13-14, 4-5, 18-19, 6-7, 23-24, 11-12, 21-22, 10-11, 1-2, 9-10, 22-23, 12-13
- Dropped 3: 15-16, 16-17, 19-20
- Duplicated train winners: 17-18, 20-21, 14-15

| phase | one/window raw net | candidate raw net | candidate minus baseline | candidate contract-trades vs one/window |
|---|---:|---:|---:|---:|
| train_2020_2022 | $8,623 | $25,734 | $17,111 | 95.2% |
| test_2023_plus | $25,804 | $31,554 | $5,750 | 95.1% |

| window | train raw net |
|---|---:|
| 17-18 | $4,146 |
| 20-21 | $3,382 |
| 14-15 | $2,033 |
| 5-6 | $1,857 |
| 7-8 | $1,717 |
| 8-9 | $1,539 |
| 2-3 | $955 |
| 3-4 | $864 |
| 13-14 | $786 |
| 4-5 | $664 |
| 18-19 | $604 |
| 6-7 | $524 |
| 23-24 | $502 |
| 11-12 | $422 |
| 21-22 | $159 |
| 10-11 | $-459 |
| 1-2 | $-544 |
| 9-10 | $-700 |
| 22-23 | $-1,028 |
| 12-13 | $-1,249 |
| 15-16 | $-1,261 |
| 19-20 | $-2,608 |
| 16-17 | $-3,682 |

## Rolling-year robustness of the K=23 ranking rule

For each year, the same top-20 plus duplicated-top-3 rule is re-ranked using only completed standalone trades from 2020 through the preceding December 31, then locked for the next calendar year. The 2021 row has only one training year and is explicitly a short-history test.

| test year | prior train | dropped | duplicated | one/window trades | candidate trades | one/window net | candidate net | difference | win |
|---:|---|---|---|---:|---:|---:|---:|---:|:---:|
| 2021 | 2020-01-01 to 2020-12-31 (one-year train) | 12-13,15-16,16-17 | 8-9,14-15,7-8 | 1,911 | 1,870 | $562 | $213 | $-349 | no |
| 2022 | 2020-01-01 to 2021-12-31 | 15-16,16-17,19-20 | 17-18,14-15,8-9 | 1,913 | 1,858 | $6,260 | $10,878 | $4,618 | yes |
| 2023 | 2020-01-01 to 2022-12-31 | 15-16,16-17,19-20 | 17-18,20-21,14-15 | 1,947 | 1,802 | $4,575 | $6,316 | $1,741 | yes |
| 2024 | 2020-01-01 to 2023-12-31 | 15-16,16-17,19-20 | 17-18,20-21,18-19 | 1,951 | 1,853 | $12,414 | $9,140 | $-3,275 | no |
| 2025 | 2020-01-01 to 2024-12-31 | 12-13,19-20,22-23 | 17-18,14-15,20-21 | 1,977 | 1,946 | $7,560 | $1,847 | $-5,713 | no |
| 2026 | 2020-01-01 to 2025-12-31 | 12-13,15-16,21-22 | 17-18,14-15,20-21 | 1,057 | 1,046 | $1,255 | $11,429 | $10,174 | yes |

- Annual wins: **3/6**
- Cumulative candidate minus one/window: **$7,196**
- Exact two-sided paired sign-flip p-value: **0.6250**

The six annual observations are few and adjacent years are not guaranteed independent; the p-value is a diagnostic, not proof of a persistent edge.

## Fixed round-robin assignments

- K=1: `seat1=1-2|2-3|3-4|4-5|5-6|6-7|7-8|8-9|9-10|10-11|11-12|12-13|13-14|14-15|15-16|16-17|17-18|18-19|19-20|20-21|21-22|22-23|23-24`
- K=2: `seat1=1-2|3-4|5-6|7-8|9-10|11-12|13-14|15-16|17-18|19-20|21-22|23-24; seat2=2-3|4-5|6-7|8-9|10-11|12-13|14-15|16-17|18-19|20-21|22-23`
- K=3: `seat1=1-2|4-5|7-8|10-11|13-14|16-17|19-20|22-23; seat2=2-3|5-6|8-9|11-12|14-15|17-18|20-21|23-24; seat3=3-4|6-7|9-10|12-13|15-16|18-19|21-22`
- K=4: `seat1=1-2|5-6|9-10|13-14|17-18|21-22; seat2=2-3|6-7|10-11|14-15|18-19|22-23; seat3=3-4|7-8|11-12|15-16|19-20|23-24; seat4=4-5|8-9|12-13|16-17|20-21`
- K=6: `seat1=1-2|7-8|13-14|19-20; seat2=2-3|8-9|14-15|20-21; seat3=3-4|9-10|15-16|21-22; seat4=4-5|10-11|16-17|22-23; seat5=5-6|11-12|17-18|23-24; seat6=6-7|12-13|18-19`
- K=23: one chronological hourly window per seat.
