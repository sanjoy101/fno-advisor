# Stop and score-bar calibration — derived, not swept

**Generated:** 2026-08-21T12:42:14
**Data:** DEV only, 2020-01-01 .. 2024-11-26, 1212 sessions, 20 symbols
**Holdout:** HOLDOUT UNUSED -- available for exactly one evaluation.

> Nothing here is swept. Two constants are read off stated percentiles of measured
> distributions. Both are DEV-DERIVED — a weaker claim than out-of-sample, and not a
> substitute for the single holdout evaluation.

---

## 1. The noise a correct position must survive

For every simulated entry, a long is opened with NO stop and walked to the 15:15
square-off. For those that reached T1, the worst adverse excursion BEFORE they got
there is recorded. A position that was RIGHT still had to sit through that, so any
stop tighter would have cut a winner.

All figures in ATRs at entry. Entries are screened by the SAME minimum-ATR gate the
engine applies (0.15% of price), so this measures the noise on trades the
engine could actually take. Without that screen a near-zero ATR turns the ratio into
nonsense — an unscreened first pass reported a maximum MAE of 58,755 ATR.

**51,223 simulated entries, 40,105 reached T1
(78.3%).**

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL (reached T1) | 40105 | 1.757 | 0.355 | 1.088 | 2.372 | 4.288 | 5.799 | 89.147 |


### By time of day

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 09:30-10:29 | 20723 | 1.654 | 0.365 | 1.078 | 2.275 | 3.972 | 5.321 | 89.147 |
| 10:30-11:59 | 8332 | 2.04 | 0.385 | 1.197 | 2.718 | 5.125 | 6.97 | 25.063 |
| 12:00-13:29 | 5020 | 2.029 | 0.352 | 1.137 | 2.658 | 4.869 | 6.982 | 45.124 |
| 13:30-14:30 | 6030 | 1.492 | 0.297 | 0.945 | 2.064 | 3.659 | 4.969 | 31.526 |


### By symbol (worst p75 first)

| group | n | mean | p50 | p75 | p90 | max |
| --- | --- | --- | --- | --- | --- | --- |
| BHARTIARTL | 1804 | 1.78 | 1.104 | 2.529 | 4.302 | 23.79 |
| ADANIENT | 4587 | 1.886 | 1.135 | 2.487 | 4.497 | 89.147 |
| TMPV | 3026 | 1.875 | 1.183 | 2.484 | 4.41 | 24.176 |
| AXISBANK | 2186 | 1.848 | 1.067 | 2.477 | 4.555 | 45.124 |
| SBIN | 2043 | 1.821 | 1.065 | 2.472 | 4.561 | 25.063 |
| TATASTEEL | 2985 | 1.764 | 1.108 | 2.389 | 4.281 | 24.856 |
| ICICIBANK | 1631 | 1.806 | 1.092 | 2.381 | 4.651 | 25.864 |
| ONGC | 2643 | 1.755 | 1.109 | 2.379 | 4.341 | 28.577 |


Spread of the p75 across symbols: **0.38 ATR**
(min 2.15, max 2.53 over 20 symbols).

---

## 2. The derived stop

Rule stated in advance: the stop must sit **beyond the 75th
percentile** of survivable adverse excursion, so three quarters of positions that go
on to reach T1 never touch it.

**Derived multiplier: 2.37 ATR** (was 1.00).

Per bucket, for comparison:

| bucket | p75 ATR |
| --- | --- |
| 09:30-10:29 | 2.27 |
| 10:30-11:59 | 2.72 |
| 12:00-13:29 | 2.66 |
| 13:30-14:30 | 2.06 |


A single global multiplier is used rather than one per bucket or per symbol. The
bucket values sit close together, and every extra constant read off DEV is another
degree of freedom fitted to it; one number from 40,105 observations is
the most defensible of the three.

---

## 3. Re-derived targets, and whether they are reachable

The declared reward-to-risk is preserved exactly: **1.5** to T1.

| level | old (ATR) | new (ATR) |
| --- | --- | --- |
| stop | 1.00 | **2.37** |
| T1 | 1.50 | **3.56** |
| T2 | 3.00 | **7.11** |

Is that target reachable? Distribution of the best favourable excursion over the
holding window, all simulated entries:

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MFE, all simulated entries | 51223 | 5.763 | 1.761 | 4.043 | 7.706 | 12.746 | 16.985 | 162.418 |


- **54.7%** of entries reach the new T1 of 3.56 ATR at some point.
- **27.9%** reach the new T2 of 7.11 ATR.

The new T1 of 3.56 ATR sits below the median favourable excursion of 4.043 ATR, so it is reachable on a typical entry.

---

## 4. The score bar

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| composite score, all candidates | 4891 | 0.048 | -0.287 | 0.01 | 0.366 | 0.594 | 0.707 | 1.0 |


The bar sits at the 74th percentile of observed scores.

Rule stated in advance: raise the bar to the **90th percentile**
of observed scores.

**Derived bar: 0.594** (was 0.350).

By construction roughly 10% of individual candidates clear
it, against 26% today. The effect on NO_SIGNAL days is
measured in the re-run, not predicted here — a session surfaces a signal if ANY
symbol clears, so the per-session rate is not the per-candidate rate.

---

## What changes

| setting | old | new | source |
| --- | --- | --- | --- |
| `stop_atr_multiple` | 1.00 | **2.37** | p75 of survivable MAE, n=40,105 |
| `t1_atr_multiple` | 1.50 | **3.56** | stop x 1.5 to preserve R:R |
| `t2_atr_multiple` | 3.00 | **7.11** | stop x 3.0 |
| `min_composite_score` | 0.350 | **0.594** | p90 of observed scores, n=4,891 |

Nothing else changes.

---

*Written by `scripts/calibrate_stops.py`.*
