# Module B (Signal Engine) — DEV evaluation

**Generated:** 2026-08-21T13:13:38
**Data:** DEV only — 2020-01-01 .. 2024-11-26, 1212 sessions,
20 symbols (TMCV excluded: 4% coverage)
**Holdout:** untouched. HOLDOUT UNUSED -- available for exactly one evaluation.
**Costs:** modelled, `rates_verified=False`
**Config:** score bar +0.59, max
2/day, signals 09:30–14:30
every 5 min, liquidity floor Rs 50,000,000,
min ATR 0.15%

> Nothing here was tuned. The thresholds were fixed before this ran. Entry is the
> engine's own signal; the exit is Module C, unchanged.

---

## Headline

Module B returns Rs -384.16 per DEV session against the baseline's Rs -165.98. **Module B does NOT beat the baseline.** The extra machinery is not earning its place.

**Module B:** LOSS-MAKING after costs: mean net Rs -208.23/trade, 95% CI [-232.91, -183.55], n=2236.

**Baseline:** LOSS-MAKING after costs: mean net Rs -166.39/trade, 95% CI [-187.11, -145.67], n=1209.

---

## Coverage

| measure | value |
| --- | --- |
| DEV sessions | 1212 |
| sessions with at least one signal | 1167 |
| NO_SIGNAL sessions | 45 |
| signals taken | 2236 |
| engine evaluations | 14223 |


---

## Head to head

| label | n | total_net_pnl | total_gross_pnl | total_costs | mean_net_pnl | median_net_pnl | hit_rate_net_pct | ci95_low | ci95_high | mean_minutes_held |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Module B signals | 2236 | -465602.28 | -59092.09 | 406510.19 | -208.23 | -223.2 | 26.3 | -232.91 | -183.55 | 17.3 |
| Baseline: highest volume @09:45 | 1209 | -201167.3 | 19537.44 | 220704.74 | -166.39 | -182.63 | 12.99 | -187.11 | -145.67 | 7.6 |


R multiple is net P&L over the risk the signal declared, ``(entry − stop) × qty``.
The baseline sets no stop, so it has no R.

| label | n | mean_r_multiple | median_r_multiple | mean_mfe_pct | mean_mae_pct |
| --- | --- | --- | --- | --- | --- |
| Module B signals | 2236 | -0.391 | -0.429 | 0.5117 | 0.3557 |


---

## Diagnosis — three things the headline does not say

**1. Module B is negative BEFORE costs; the baseline is positive before costs.**
Module B gross Rs -59,092.09 against the baseline's
Rs 19,537.44. Costs then take
Rs 406,510.19 and Rs 220,704.74
respectively. So this is not a strategy with an edge that costs eat — the selection
itself is worse than "whatever traded most this morning", and it trades roughly twice
as often to do it.

**2. Stop geometry — now outside the noise.**

| measure | value |
| --- | --- |
| mean declared stop distance | 0.635% of entry |
| mean adverse excursion (MAE) | 0.356% |
| mean favourable excursion (MFE) | 0.512% |
| stop distance / MAE | 1.78x |
| hit rate (net) | 26.3% |
| mean declared R:R to T1 | 1.50 |
| break-even hit rate needed at that R:R | 40.0% |
| expectancy at the observed hit rate | -0.342 R before costs |


:white_check_mark: The stop sits at 1.78x the mean adverse excursion, clear of the noise floor. STOP_HIT is 77.7% of exits (PASS against the stated 80% threshold).

At the declared R:R the rule still needs the break-even hit rate above to survive, and
it is not achieving it. The expectancy line is what that costs before a rupee of
brokerage — a wider stop reduced the loss per unit of risk without turning the sign.

**3. Selectivity.** 45 NO_SIGNAL sessions out of
1212 (**3.7%**), and 14223 evaluations
across 1212 sessions — about
11.7 of the 61 available per day.

| signal_time | n | pct |
| --- | --- | --- |
| 09:30 | 895 | 40.0 |
| 09:35 | 668 | 29.9 |
| 09:40 | 251 | 11.2 |
| 09:45 | 114 | 5.1 |
| 09:50 | 60 | 2.7 |
| 09:55 | 55 | 2.5 |
| 10:00 | 26 | 1.2 |
| 10:05 | 24 | 1.1 |
| 10:10 | 18 | 0.8 |
| 10:15 | 8 | 0.4 |
| 10:20 | 10 | 0.4 |
| 10:25 | 15 | 0.7 |


Raising the bar moved this, but not far: 3.7% of sessions produce nothing. **A per-candidate bar cannot make SESSIONS selective**, because a session surfaces a signal if any of 20 symbols clears at any of 61 evaluations — roughly 1,220 chances a day. Making NO_SIGNAL common needs a per-SESSION gate (only trade the day's best if it clears a higher bar), fewer evaluation opportunities, or both. That is a design change, not a threshold.

---

## Module B by exit rule

| exit_rule | n | pct | mean_net_pnl | hit_rate_pct | mean_r | median_minutes |
| --- | --- | --- | --- | --- | --- | --- |
| STOP_HIT | 1737 | 77.7 | -80.83 | 33.9 | -0.211 | 14.0 |
| MOMENTUM_REVERSAL | 285 | 12.7 | -624.85 | 0.0 | -1.001 | 19.0 |
| VWAP_LOSS | 214 | 9.6 | -687.44 | 0.0 | -1.034 | 12.0 |


**STOP_HIT is 77.7% of exits.** The stated failure threshold was 80%:
the fix PASSED that test — the stop is no longer the default exit.

---

## Module B by regime

Six years is several markets, and a rule that only works in one of them is a rule
fitted to one of them.

| regime | n | total_net_pnl | mean_net_pnl | hit_rate_pct |
| --- | --- | --- | --- | --- |
| 2020 crash & recovery | 494 | -88208.71 | -178.56 | 27.7 |
| 2021-22 trending/bear | 937 | -192913.41 | -205.88 | 27.9 |
| 2023-24 recovery | 805 | -184480.16 | -229.17 | 23.6 |


---

## Caveats

- **DEV only.** The holdout has not been touched and must not be, until there is a
  configuration worth a single evaluation.
- **State n on every claim.** Anything whose 95% interval spans zero is labelled
  INCONCLUSIVE above and should not drive a decision.
- **The F&O ban check is a stub.** No NSE ban list has been scraped, so a banned
  symbol could have been surfaced. This overstates the tradeable universe slightly.
- **Costs unverified** (`rates_verified=False`).
- **Prices are split/bonus adjusted.** Percentage returns are right; absolute
  historical prices and share counts are not.
- **Survivorship.** Today's liquid names applied backwards over six years.
- **No tuning was done and none should be done off this report alone.** The
  diagnosis above points at the stop width, the score bar and the daily budget, but
  changing any of them because it improves this number is fitting to DEV. Change one,
  re-measure on DEV, and keep the holdout for the single evaluation at the end.

Raw per-signal data: `reports\signal_outcomes_dev.csv` (2,236 rows).

---

*Written by `scripts/signal_evaluation.py`.*
