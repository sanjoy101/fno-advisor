# Module B — one decision per session

**Generated:** 2026-08-21T13:47:59
**Data:** DEV only, 2020-01-01 .. 2024-11-26, 1212 sessions, 20 symbols
**Holdout:** HOLDOUT UNUSED -- available for exactly one evaluation.
**Score bar:** 0.594 — the p90 of the previously
measured DEV score distribution. Not swept, not re-derived.
**Levels:** stop 2.37 ATR, T1
3.56, T2 7.11 — derived
earlier from measured noise, unchanged here.

> **Change under test:** one decision point per session instead of 61. Rank once, take
> only the day's single best, trade it only if it clears the absolute bar. This turns
> ~1,220 draws per session into 1. Nothing else changed.

> Two decision points are reported side by side and **neither is selected**. The point
> is whether the choice matters, not which is better.

---

## Conclusion — inconclusive on gross, and still no case for the engine

The gross intervals span zero at both decision points, so **this sample cannot
distinguish the feature set's edge from nothing.** That is not "no edge" and not "an
edge" — it is a statement about the sample, and the pre-stated rule fires only on a
decisively negative gross, so it does not strictly apply.

What the sample CAN distinguish, on identical sessions:

* **The baseline's gross edge clears zero; Module B's does not.** Picking the day's
  highest-scoring name earns less gross than taking whatever traded most that
  morning, at both decision points.
* **The two decision points disagree on the sign of the gross edge** while agreeing on
  only 52.7% of traded days.
* **Net is decisively negative for everything**, engine and baseline alike, because
  costs of about 0.182% of notional dwarf a gross edge of at most a few
  hundredths of a percent.

The structural fix did what it was asked to do — NO_SIGNAL went from 3.7% to
31.2% at 09:45 and
51.0% at 10:15, and gross moved from
decisively negative to indistinguishable from zero. It did not produce an edge,
because reducing the number of draws cannot create one where the features have none.

Per the rule: no threshold adjustment, no further variant, no search for a subset that
works follows this.

---

## Coverage — how often a signal now happens

| decision point | sessions | signals | NO_SIGNAL | NO_SIGNAL % | below bar | gated after clearing | draws/session |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 09:45 | 1212 | 834 | 378 | 31.2 | 364 | 1 | 1 |
| 10:15 | 1212 | 594 | 618 | 51.0 | 437 | 0 | 1 |


For comparison, continuous re-evaluation produced NO_SIGNAL on **3.7%** of sessions
(45 of 1212) with ~1,220 draws a day.

---

## Gross P&L — the number that decides this

| label | n | total_gross_pnl | mean_gross_pnl | gross_ci95_low | gross_ci95_high | hit_rate_gross_pct |
| --- | --- | --- | --- | --- | --- | --- |
| Module B single-shot @09:45 | 834 | -27566.2 | -33.05 | -70.34 | 4.23 | 37.05 |
| Module B single-shot @10:15 | 594 | 24791.97 | 41.74 | -7.24 | 90.72 | 42.09 |


**09:45:** **GROSS IS INCONCLUSIVE.** Mean gross Rs -33.05/trade, 95% CI [-70.34, 4.23], n=834 — the interval spans zero, so this sample cannot distinguish the edge from nothing.

**10:15:** **GROSS IS INCONCLUSIVE.** Mean gross Rs 41.74/trade, 95% CI [-7.24, 90.72], n=594 — the interval spans zero, so this sample cannot distinguish the edge from nothing.

---

## Like-for-like: Module B against the baseline on IDENTICAL days

| decision point | sessions compared | Module B gross/trade | baseline gross/trade | difference | Module B CI clears zero | baseline CI clears zero |
| --- | --- | --- | --- | --- | --- | --- |
| 09:45 | 834 | -33.05 | 29.1 | -62.15 | no | yes |
| 10:15 | 594 | 41.74 | 61.45 | -19.71 | no | yes |


:red_circle: **The baseline earns more gross than Module B at both decision points, on exactly the same sessions.** Selecting the day's best-scoring name is worse than taking whatever traded most that morning. The features are not identifying the better opportunity; they are identifying a different one, and a worse one.

## The cost hurdle, which neither clears

Costs run about **Rs 182 per trade** on a Rs 100,000 notional —
**0.182%**. The mean favourable excursion over the whole holding period is
**0.489%**.

So a trade must capture roughly **37% of the best price it ever
sees** just to break even — on positions that are stopped out most of the time and
held about 16.9 minutes. That is the
arithmetic every intraday strategy on this universe has to beat, and it is why every
net figure in this report is negative regardless of which selection rule produced it.

---

## Full comparison, including the baseline

| label | n | total_gross_pnl | mean_gross_pnl | gross_ci95_low | gross_ci95_high | hit_rate_gross_pct | total_net_pnl | mean_net_pnl | ci95_low | ci95_high | hit_rate_net_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Module B single-shot @09:45 | 834 | -27566.2 | -33.05 | -70.34 | 4.23 | 37.05 | -179222.4 | -214.89 | -252.15 | -177.64 | 23.02 |
| Baseline @09:45, on the 09:45 signal days only | 834 | 24265.72 | 29.1 | 0.08 | 58.11 | 25.42 | -128004.28 | -153.48 | -182.48 | -124.49 | 15.83 |
| Module B single-shot @10:15 | 594 | 24791.97 | 41.74 | -7.24 | 90.72 | 42.09 | -83265.96 | -140.18 | -189.13 | -91.23 | 28.62 |
| Baseline @09:45, on the 10:15 signal days only | 594 | 36501.55 | 61.45 | 22.61 | 100.29 | 26.09 | -71972.76 | -121.17 | -159.97 | -82.36 | 18.01 |
| Baseline @09:45, all DEV sessions | 1209 | 19537.44 | 16.16 | -4.58 | 36.9 | 21.59 | -201167.3 | -166.39 | -187.11 | -145.67 | 12.99 |


Each variant is shown against the highest-volume-at-09:45 baseline restricted to the
SAME sessions it traded, so the two are compared on identical days rather than on
different sets of them. The baseline over all 1212 DEV sessions is included as
the full-period reference.

---

## R multiples and excursions

| label | n | mean_r_multiple | median_r_multiple | mean_mfe_pct | mean_mae_pct | mean_minutes_held |
| --- | --- | --- | --- | --- | --- | --- |
| Module B single-shot @09:45 | 834 | -0.44 | -0.453 | 0.4892 | 0.3332 | 16.9 |
| Module B single-shot @10:15 | 594 | -0.343 | -0.48 | 0.5446 | 0.2862 | 15.4 |


---

## Exit-rule distribution

### 09:45

| exit_rule | n | pct | mean_gross_pnl | mean_net_pnl | hit_rate_net_pct | mean_r |
| --- | --- | --- | --- | --- | --- | --- |
| STOP_HIT | 666 | 79.9 | 60.13 | -121.78 | 28.8 | -0.289 |
| MOMENTUM_REVERSAL | 142 | 17.0 | -401.71 | -583.25 | 0.0 | -1.031 |
| VWAP_LOSS | 26 | 3.1 | -406.58 | -588.29 | 0.0 | -1.091 |


### 10:15

| exit_rule | n | pct | mean_gross_pnl | mean_net_pnl | hit_rate_net_pct | mean_r |
| --- | --- | --- | --- | --- | --- | --- |
| STOP_HIT | 452 | 76.1 | 151.96 | -30.05 | 37.6 | -0.135 |
| MOMENTUM_REVERSAL | 140 | 23.6 | -307.36 | -488.94 | 0.0 | -1.004 |
| VWAP_LOSS | 2 | 0.3 | -432.49 | -614.59 | 0.0 | -1.205 |


---

## Does the decision point matter?

The two variants traded on **493** of the **935** sessions either of them
traded — **52.7% agreement**.

:red_circle: **The two decision points disagree on the SIGN of the gross edge** — 09:45 is Rs -33.05/trade and 10:15 is Rs +41.74/trade — while agreeing on only 52.7% of the days they trade. A result whose sign flips on a 30-minute change in when the question is asked is not a robust one, and neither number should be read as the engine's edge.

---

## Caveats

- **DEV only**, n stated on every claim above. The holdout is untouched.
- **Two variants, one sample.** Reporting both rather than selecting avoids picking a
  winner off DEV, but it does not make either an out-of-sample result.
- **The F&O ban check is a stub**, so a banned symbol could have been surfaced.
- **Costs unverified** (`rates_verified=False`).
- **Prices are split/bonus adjusted**: percentage returns are right, absolute
  historical prices and share counts are not.
- **Survivorship**: today's liquid names applied backwards over five years.

Raw data: `reports/single_shot_0945_dev.csv`, `reports/single_shot_1015_dev.csv`.

---

*Written by `scripts/single_shot_evaluation.py`.*
