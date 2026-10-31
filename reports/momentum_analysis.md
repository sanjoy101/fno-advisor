# Momentum rule analysis — real NSE bars

**Generated:** 2026-08-21T02:00:47
**Panel:** 21 symbols x 125 sessions (2026-02-16 .. 2026-08-19)
**Excluded sessions:** 2026-08-20 — vendor returned
the session twice with conflicting `high`/`low`/`volume` for 20 of 21 symbols
**Split:** DEV 2026-02-16..2026-07-15 (100 sessions) | HOLDOUT 2026-07-16..2026-08-19 (25 sessions)
**Entry:** long at 09:45 IST, filled at the close of the last completed
bar, fixed notional Rs 100,000
**Costs:** modelled (`rates_verified=False`)

> **This is a diagnostic, not a strategy result.** Entry is a fixed clock time, not a
> signal. These numbers describe how the EXIT rules behave; they say nothing about
> whether the system makes money.

## Protocol (fixed before any result was seen)

- Chronological 80/20 split. No shuffling — time-ordered data.
- Diagnostics and both sweeps: **DEV only**, enforced by `HoldoutGuard`.
- Selection rule, declared in advance: **highest total net P&L on DEV across the 9 swept configs (5 trail widths + 4 confirmation settings, baseline common to both); ties broken toward the simpler rule**.
- **One** holdout evaluation, of **one** config. The claim is recorded at
  `reports\.holdout_claim.json`; a second evaluation of a different config raises `HoldoutSpentError`.
- Power computed with the **measured** SD (0.578pp), not an assumption.

### Symbol screen

None — every symbol cleared the threshold.

---

## Summary

:rotating_light: **The dominant finding is not about momentum. 48% of
positions are closed on the FIRST completed bar after entry, and
83% within 15 minutes.** Mean gross P&L is
Rs +2.39 against Rs 181.45 of costs — the exercise is
measuring the price of a round trip, not the quality of an exit rule. Every
comparison below inherits that. See §2b.

**Momentum is 47.0% of DEV exits — BELOW your 70% dead-code threshold.** The trailing stop fires on 53.0% of exits and the trail sweep moves net P&L by Rs 11,456 across widths, so it is not inert either. The suspicion raised on synthetic bars did **not** replicate on real data.

**Forward return 60 min after a momentum exit (DEV):** NOT DISTINGUISHABLE FROM ZERO (95% CI [-0.045, +0.027], n=987). This sample can only detect effects of +/-0.052pp or larger.

> This null is **adequately powered**: the minimum detectable effect
> (0.052pp) is far below the
> ~0.18pp round-trip cost, so an economically meaningful effect WOULD have shown up.
> It did not. **The momentum rule is not cutting winners.** It is losing money a
> different way — see §2b.

**Trail sweep (DEV):** Range -381,694.87 to -370,239.20 (spread 11,455.67, 3.1% of mean). Best on THIS sample: trail=2.0x. That is an in-sample maximum, not a recommendation -- confirming it needs held-out data.

**Confirmation sweep (DEV):** Range -379,264.08 to -376,038.45 (spread 3,225.63, 0.9% of mean). Best on THIS sample: confirm=1bar. That is an in-sample maximum, not a recommendation -- confirming it needs held-out data.

**Holdout:** best DEV config `trail=2.0x` gave mean net
Rs -176.30/position on DEV and Rs -200.66 on HOLDOUT
(-24.36). **NOT CONFIRMED.**

---

## 1. Distribution of exit rules (DEV, n = 2,100)

| exit_rule | n | pct |
| --- | --- | --- |
| MOMENTUM_REVERSAL | 987 | 47.0 |
| STOP_HIT | 604 | 28.76 |
| VWAP_LOSS | 509 | 24.24 |


By coarse state:

| exit_state | n | pct |
| --- | --- | --- |
| EXIT_NOW | 2100 | 100.0 |


---

## 2. Exit time distribution (DEV)

All exits, 15-minute bins:

| bin | n | pct |
| --- | --- | --- |
| 09:45 | 1710 | 81.43 |
| 10:00 | 296 | 14.1 |
| 10:15 | 63 | 3.0 |
| 10:30 | 24 | 1.14 |
| 10:45 | 6 | 0.29 |
| 11:00 | 1 | 0.05 |


Momentum exits only (n = 987):

| bin | n | pct |
| --- | --- | --- |
| 09:45 | 930 | 94.22 |
| 10:00 | 49 | 4.96 |
| 10:15 | 8 | 0.81 |


The 10:52 cluster seen on synthetic bars was an **artifact of the fixture**. On real
bars exits cluster in the first quarter hour after entry.

---

## 2b. Holding period — the effect that dominates everything else

| measure | value |
| --- | --- |
| positions | 2100 |
| exited on the first completed bar | 1013 (48.24%) |
| held <= 5 min | 1256 (59.81%) |
| held <= 15 min | 1739 (82.81%) |
| held >= 60 min | 7 (0.33%) |
| median hold (min) | 2.0 |
| p90 hold (min) | 21.0 |
| max hold (min) | 79.0 |
| mean GROSS P&L (Rs) | 2.39 |
| mean costs (Rs) | 181.45 |
| mean NET P&L (Rs) | -179.07 |
| win rate, gross | 20.05% |
| win rate, net | 9.95% |


Gross P&L is indistinguishable from zero; costs are the whole story. A strategy that
enters at a fixed clock time and exits within a bar or two captures no move and pays
a full round trip.

Per-rule, which matters more than the raw counts — a rule can dominate the count and
still be the one destroying value:

| exit_rule | n | pct | median_hold_min | mean_gross_pct | mean_net_pnl | mean_mfe_pct | mean_mae_pct | win_rate_net_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MOMENTUM_REVERSAL | 987 | 47.0 | 0.0 | -0.0653 | -246.01 | 0.0227 | 0.0789 | 0.0 |
| STOP_HIT | 604 | 28.8 | 16.0 | 0.1944 | 11.39 | 0.492 | 0.1018 | 34.4 |
| VWAP_LOSS | 509 | 24.2 | 1.0 | -0.0947 | -275.26 | 0.037 | 0.1168 | 0.2 |


Read the `median_hold_min` column first. `MOMENTUM_REVERSAL` and `VWAP_LOSS` fire
almost immediately and both have **negative** mean gross return — they close positions
before any move can develop. `STOP_HIT` (the ATR trail, which can only engage after
breakeven arms) holds materially longer, shows the largest MFE, and is the only rule
with positive mean gross AND net.

That is the honest version of "is the momentum rule costing money": not by cutting
winners after the fact, but by preventing a position from existing long enough to
become one.

---

## 3. Forward return after exit — is the rule cutting winners? (DEV)

60-minute return from the exit price, long side. **Positive means price
kept rising after we sold.** Windows running past 15:30 are EXCLUDED, not truncated —
truncating shrinks measured moves and manufactures a null.

| label | n | mean_pct | median_pct | sd_pct | ci95_low | ci95_high | t_stat | mde_pct | censored |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| forward return 60min after MOMENTUM_REVERSAL exit | 987 | -0.0093 | -0.0237 | 0.5779 | -0.0453 | 0.0268 | -0.504 | 0.0515 | 0 |
| forward return 60min after any exit | 2100 | 0.0098 | -0.0013 | 0.5795 | -0.015 | 0.0346 | 0.777 | 0.0354 | 0 |


**Momentum exits:** NOT DISTINGUISHABLE FROM ZERO (95% CI [-0.045, +0.027], n=987). This sample can only detect effects of +/-0.052pp or larger.

**All exits (baseline):** NOT DISTINGUISHABLE FROM ZERO (95% CI [-0.015, +0.035], n=2100). This sample can only detect effects of +/-0.035pp or larger.

### Power, using the measured SD

Measured SD of the momentum forward return: **0.578pp**
(replaces the 0.6pp placeholder used before data existed).

| n (momentum exits) | min detectable mean (pp) |
| --- | --- |
| 50 | 0.229 |
| 100 | 0.162 |
| 250 | 0.102 |
| 500 | 0.072 |
| 1000 | 0.051 |
| 2000 | 0.036 |
| 5000 | 0.023 |


At n = 987, the minimum detectable effect is
**0.052pp**. A measured mean
smaller than that is not a small effect — it is not a measurement.

---

## 4. Trail multiple sweep (DEV only)

| config | n | total_net_pnl | mean_net_pnl | median_net_pnl | win_rate_net_pct | momentum_exit_pct | mean_mfe_pct | mean_mae_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trail=0.5x | 2100 | -381694.87 | -181.76 | -182.22 | 10.9 | 46.9 | 0.125 | 0.0917 |
| trail=1.0x | 2100 | -377470.05 | -179.75 | -182.32 | 10.62 | 47.0 | 0.1456 | 0.093 |
| trail=1.5x | 2100 | -376038.45 | -179.07 | -182.4 | 9.95 | 47.0 | 0.1611 | 0.0947 |
| trail=2.0x | 2100 | -370239.2 | -176.3 | -182.43 | 9.43 | 47.43 | 0.1737 | 0.0954 |
| trail=3.0x | 2100 | -372164.78 | -177.22 | -182.48 | 9.05 | 52.05 | 0.1846 | 0.0963 |


Range -381,694.87 to -370,239.20 (spread 11,455.67, 3.1% of mean). Best on THIS sample: trail=2.0x. That is an in-sample maximum, not a recommendation -- confirming it needs held-out data.

---

## 5. Momentum confirmation sweep (DEV only)

`momentum_confirmation_bars = N` requires the reversal to hold N consecutive completed
bars. N=1 is the shipped rule.

| config | n | total_net_pnl | mean_net_pnl | win_rate_net_pct | momentum_exits | momentum_exit_pct | fwd_ret_after_momentum_mean_pct | fwd_ret_n |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| confirm=1bar | 2100 | -376038.45 | -179.07 | 9.95 | 987 | 47.0 | -0.0093 | 987 |
| confirm=2bar | 2100 | -378482.4 | -180.23 | 10.33 | 898 | 42.76 | -0.0069 | 898 |
| confirm=3bar | 2100 | -379264.08 | -180.6 | 10.57 | 824 | 39.24 | -0.0092 | 824 |
| confirm=5bar | 2100 | -379246.63 | -180.59 | 11.1 | 680 | 32.38 | 0.0007 | 680 |


Range -379,264.08 to -376,038.45 (spread 3,225.63, 0.9% of mean). Best on THIS sample: confirm=1bar. That is an in-sample maximum, not a recommendation -- confirming it needs held-out data.

---

## 6. Holdout — one evaluation, one config

Selected by the declared rule: **`trail=2.0x`**
(fingerprint `0ed3e192ad2ec155`).

HOLDOUT SPENT on 2026-08-21T01:54:53+05:30 for config 0ed3e192ad2ec155 (best DEV config: trail=2.0x).

| | DEV | HOLDOUT |
|---|---|---|
| sessions | 100 | 25 |
| positions | 2,100 | 525 |
| total net P&L | -376,038.45 | -105,346.57 |
| mean net / position | -179.07 | -200.66 |
| median net / position | -182.40 | -182.51 |
| momentum share of exits | 47.0% | 51.4% |
| fwd return after momentum | -0.009pp (n=987) | -0.009pp (n=270) |

**DEV mean for the selected config was Rs -176.30; HOLDOUT delivered
Rs -200.66 (-24.36). NOT CONFIRMED.**

Holdout forward return after momentum exits: NOT DISTINGUISHABLE FROM ZERO (95% CI [-0.057, +0.038], n=270). This sample can only detect effects of +/-0.068pp or larger.

The holdout is now spent. Any further configuration change must be judged on DEV, or
on data collected after 2026-08-19.

---

## Caveats

- **One regime.** 125 sessions is a single market period.
- **Nine in-sample maxima.** The selected config is the best of 9 scored on DEV. The
  holdout number is the honest one; the DEV number is optimistic by construction.
- **Entry is arbitrary.** A fixed 09:45 entry is not the signal engine.
- **Costs unverified.** `rates_verified=False`.
- **Survivorship.** Today's liquid names applied backwards. TMPV/TMCV are
  post-demerger instruments; both cleared the coverage screen.
- **Claims below n=200 are DIRECTIONAL ONLY** and should not
  drive a code change alone.

Raw data: `reports\batch_replay_dev.csv` (2,100 rows), `reports\batch_replay_holdout.csv` (525 rows).

---

*Written by `scripts/momentum_analysis.py`.*
