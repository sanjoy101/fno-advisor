# F&O positioning features on a 1-day horizon — DEV only

**Generated:** 2026-08-23T13:07:40
**Pre-registration:** `reports/.fno_preregistration.json` — verified against the live
constants before this ran; the script refuses to execute if they have drifted.
**Panel:** 19 symbols, DEV 2020-01-01 .. 2024-12-31 (1236 sessions),
1116 decision dates after a 120-session warmup
**Holdout:** 2025-01-01 onward — HOLDOUT UNUSED -- available for exactly one evaluation.
**Horizon:** 1 trading day, entry 15:15, one decision point per session

---

## Conclusion — the pre-stated rule applies

**Gross excess return over the equal-weight buy-and-hold comparator does not clear zero
on DEV.**

On the pre-stated arm the mean gross excess is **-0.088%**,
95% CI **[-0.1698, -0.0062]** on n_eff = 907. On the power-reference arm,
which takes the day's best candidate every session and so carries the largest sample the
design can produce, it is **-0.1119%**, 95% CI **[-0.1851, -0.0387]** on
n_eff = 1114. Neither lower bound is above zero.

| arm | trades | EXCESS mean % | excess 95% CI (effective n) | effective n | MDE | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| primary (pre-stated bar) | 907 | -0.088 | [-0.1698, -0.0062] | 907 | 0.1168 | NEGATIVE — interval entirely below zero |
| power reference (no bar) | 1114 | -0.1119 | [-0.1851, -0.0387] | 1114 | 0.1046 | NEGATIVE — interval entirely below zero |


The rule fixed before this ran, recorded in `reports/.fno_preregistration.json`, was:

> If gross excess return over the buy-and-hold comparator does not clear zero on DEV,
> F&O features carry no edge either, and the conclusion is that this approach is
> exhausted. Report that plainly. No sweeps, no additional horizons, no search for a
> subset that works.

**That is the answer. The F&O features carry no edge on this horizon, and this approach
is exhausted.**

Per the rule: no sweep, no second horizon, no subset search, and **no holdout
evaluation**. The swing holdout (2025-01-01 onward) is untouched and stays that way.

---

## What was actually computable, and what was not

The request asked for OI change over **1, 5, 15 and 30 minutes**. That does not exist in
this data and could not be manufactured:

- Fyers' history endpoint returns six-field OHLCV and carries **no open interest at any
  resolution**. OI comes from the exchange's end-of-day derivatives bhavcopy, which is
  daily by construction.
- India VIX comes from NSE's daily index-close file — one OHLC row per session. There is
  no intraday India VIX here at any resolution.
- Futures prices are daily bhavcopy closes. With no intraday futures bars there is no
  intraday basis either.

So **every lookback in this experiment is in trading days, not minutes.** Resampling a
daily row into minutes would have manufactured a series the exchange never published.
The features built were:

| requested | built | note |
| --- | --- | --- |
| OI change 1/5/15/30 min | OI change 1/5/15/30 **trading days**, absolute and % | daily is the finest resolution that exists |
| OI change day-over-day | 1-day change, near-month and total | |
| OI–price divergence, four states | `LONG_BUILDUP` / `SHORT_BUILDUP` / `SHORT_COVERING` / `LONG_UNWINDING` | price leg from adjusted spot, not raw futures |
| futures basis, absolute and % of spot | `basis_pct`, plus an annualised carry rate | annualised because raw basis decays mechanically into expiry |
| basis rate of change | `basis_roc_pp`, one-session change in points | None across a roll |
| India VIX level, change, 1-year percentile | all three | percentile needs 120 sessions of history |
| rollover % near expiry | `rollover_ratio` and `rollover_relative` | relative to the same-days-to-expiry norm over prior cycles |

---

## Three discontinuities, each of which would have produced a fake signal

All three are handled explicitly, and all three yield `None` rather than zero. A zero
reads as "no change", which is the opposite of "not comparable".

### 1. The contract roll

OI does not carry across a roll — the new contract is a different pool of positions.
Measured on this data, RELIANCE's near-month OI moves **+292%** on its 2024-10-31 roll.
Near-month OI change and basis rate-of-change are therefore `None` across a roll.

Total OI, summed over every listed futures expiry, *is* continuous through a roll, and
that is what the 5/15/30-day windows are built from — a monthly contract does not live
long enough for a 30-day window to fit inside one.

### 2. Corporate actions — the trap that is not the roll

NSE restates F&O lot size at a split or bonus, so OI counted in units jumps by the
action's factor on the ex-date. Measured here:

| underlying | ex-date | near-month OI move | total OI move |
| --- | --- | --- | --- |
| RELIANCE 1:1 bonus | 2024-10-28 | +73.9% | +99.2% |
| WIPRO 1:1 bonus | 2024-12-03 | +88.9% | +90.2% |

Neither is a position build. Every OI window spanning an ex-date is invalidated, exactly
as a roll is.

### 3. The price-scale mismatch between the two sources — silent, and fatal to the basis

Fyers serves spot history **split/bonus ADJUSTED**; the bhavcopy serves futures **RAW**.
Before its bonus, RELIANCE's futures close divided by its stored spot close is **2.007**,
not 1.003. TATASTEEL's is **10.0**. A basis computed as `futures − spot` on those two
numbers is not a mispriced carry, it is a 100% error that would dwarf every real signal
in the panel — and nothing about it looks wrong on inspection.

`price_scale_factor` recovers the factor by snapping that ratio to the nearest plausible
corporate-action factor, and returns `None` when nothing fits within 5.0%.
This is only safe because the quantity being measured is small and the candidates are far
apart — a real basis is well under 5% while the closest candidate factors are 20% apart —
and that premise is asserted in the tests rather than assumed.

**Price CHANGES are taken from the adjusted spot series, never from raw futures**, so no
four-state classification can be flipped by an ex-date.

### 3b. Demergers defeat the snap, and one symbol in this panel proves it

The snap works because splits and bonuses are simple rationals. **Demergers are not.**
Their adjustment factor is a value ratio — an arbitrary number — so it lands *inside* the
snap's 5.0% tolerance, is absorbed into a factor of 1.0, and
re-emerges as a constant fake premium indistinguishable from an expensive stock to carry.

An audit of the resulting basis distributions caught it. **ADANIENT's futures/spot ratio
is 1.0350 in every one of the twenty quarters from 2020Q1 to 2024Q4** — including 2023Q1,
when the stock lost roughly seventy percent on the Hindenburg report. A carry premium
cannot hold flat through that. SBIN's ratio over the same years sits at 1.002 and moves
around, which is what a real basis does.

Unchecked, this mattered: at ~3.5% of spot the annualised carry clamps `basis_carry` to
+1.0 on essentially every session, adding up to 0.138 to
ADANIENT's composite for free and systematically over-selecting it.

Widening the candidate factors to catch 1.035 would be the wrong fix — it would destroy
the 20% separation that makes the snap unambiguous, after which a *genuine* 3% basis
would be silently snapped away. Instead `basis_is_plausible` refuses the two basis
components for any symbol whose MEDIAN basis exceeds 1.5% of spot
(three times fair monthly carry at a ~6% annual rate). They drop out with their weight
through the ordinary "could not be measured" path. The tell is persistence, not size: a
real basis mean-reverts to carry and collapses into every expiry.

**Suppressed in this run: ADANIENT.**

#### This experiment was run twice, and both results are recorded

The ADANIENT contamination was found by auditing the basis distributions **after** a
first full run, so there are two results and hiding either would be dishonest:

| run | primary excess % | primary 95% CI | power-arm excess % | power 95% CI |
| --- | --- | --- | --- | --- |
| 1 — ADANIENT basis contaminated | −0.0637 | [−0.1476, +0.0201] | −0.0878 | [−0.1633, −0.0124] |
| 2 — basis suppressed for ADANIENT | -0.088 | [-0.1698, -0.0062] | -0.1119 | [-0.1851, -0.0387] |

Nothing about the pre-registration changed between them: same horizon, same bar, same
weights, same saturation constants, same decision rule. The only change was refusing a
feature that was measuring a demerger adjustment rather than a market price.

The correction's DIRECTION was predictable before it was applied — removing a constant
positive bias on one symbol can only reduce how often that symbol is selected — and it
made the result **more** negative, not less. The first run's power arm was already below
zero. This is a fix that cost the strategy, not one that rescued it.

### What the snap found across the panel

| symbol | built | no spot bars | near contract absent | scale unresolved | factors | ex-dates in DEV | median basis % | basis usable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RELIANCE | 1207 | 3 | 25 | 0 | 1, 2 | 2024-10-28 | 0.289 | yes |
| HDFCBANK | 1207 | 3 | 25 | 0 | 2 | — | 0.196 | yes |
| ICICIBANK | 1207 | 3 | 25 | 0 | 1 | — | 0.247 | yes |
| INFY | 1207 | 3 | 25 | 0 | 1 | — | 0.199 | yes |
| TCS | 1207 | 3 | 25 | 0 | 1 | — | 0.196 | yes |
| SBIN | 1207 | 3 | 25 | 0 | 1 | — | 0.249 | yes |
| AXISBANK | 1207 | 3 | 25 | 0 | 1 | — | 0.276 | yes |
| MARUTI | 1207 | 3 | 25 | 0 | 1 | — | 0.266 | yes |
| LT | 1207 | 3 | 25 | 0 | 1 | — | 0.24 | yes |
| BHARTIARTL | 1207 | 3 | 25 | 0 | 1 | — | 0.224 | yes |
| ITC | 1207 | 3 | 25 | 0 | 1 | — | 0.224 | yes |
| KOTAKBANK | 1207 | 3 | 25 | 0 | 5 | — | 0.204 | yes |
| HINDALCO | 1207 | 3 | 25 | 0 | 1 | — | 0.23 | yes |
| TATASTEEL | 1205 | 3 | 25 | 2 | 1, 10 | 2022-07-28 | 0.27 | yes |
| ADANIENT | 1205 | 3 | 25 | 2 | 1 | — | 3.525 | **NO** |
| BAJFINANCE | 1207 | 3 | 25 | 0 | 10 | — | 0.273 | yes |
| SUNPHARMA | 1207 | 3 | 25 | 0 | 1 | — | 0.234 | yes |
| WIPRO | 1207 | 3 | 25 | 0 | 1, 2 | 2024-12-03 | 0.221 | yes |
| ONGC | 1207 | 3 | 25 | 0 | 1 | — | 0.187 | yes |


Reading the drop columns:

- **no spot bars** — sessions with a futures row but no stored equity bars. These are the
  circuit-breaker halt days quarantined by the equity backfill (2020-03-13, 2020-03-23,
  2022-03-07), which is correct data of a halted market being correctly withheld.
- **near contract absent** — sessions where the roll rule selects an expiry that has no
  row in the exchange file that day. Almost all of these fall in 2023-05-25 → 2023-06-29,
  around the one expiry NSE moved off its scheduled date (2023-06-29 was Bakri Id). The
  session is skipped rather than filled from a neighbouring contract: a fabricated bar in
  a price series is worse than a hole in it.
- **scale unresolved** — no candidate factor fit within 5.0%.

---

## Causality

Every F&O feature is strictly `trade_date < decision date`. The bhavcopy for session D
and the index-close file for session D both publish **after the close of D**, so a
decision at 15:15 on D — thirteen minutes before the close that produces them — may use
D-1 at the newest. A daily row looks like a property of the day it is stamped with, which
is exactly what makes `<=` the natural mistake here.

This is a **cross-session** leak shape, and the bar-level poison harness cannot see it:
every session in a wrong date list is genuinely complete, so poisoning intraday bars
proves nothing. `assert_session_causal` is the only thing that covers it, and in
`tests/test_fno_features.py` it is applied to:

- every public feature in `src.signals.fno_features`, at four decision dates each;
- the assembled composite `score_symbol_fno`, because a leak can live in the wiring
  rather than in any one feature;
- **two deliberately leaky controls that the check must reject** — one using `<=`
  instead of `<` (the publication-lag bug), one reading `sessions[-1]`. A check that
  never fails is not evidence.

A coverage test fails if a new feature is added to `__all__` without a causality test,
and a non-vacuity test fails if the fixture is so flat that a leak could hide in it.

Feature availability across the 21,204 scored symbol-days:

| component | weight | measured | of scored symbol-days | availability % |
| --- | --- | --- | --- | --- |
| oi_price_state_1d | 3.0 | 20036 | 21204 | 94.5 |
| oi_buildup_5d | 2.0 | 21143 | 21204 | 99.7 |
| oi_buildup_15d | 1.0 | 21144 | 21204 | 99.7 |
| oi_buildup_30d | 1.0 | 21115 | 21204 | 99.6 |
| basis_carry | 2.0 | 18738 | 21204 | 88.4 |
| basis_roc | 2.0 | 19114 | 21204 | 90.1 |
| vix_percentile | 1.5 | 21204 | 21204 | 100.0 |
| vix_change | 1.0 | 21204 | 21204 | 100.0 |
| rollover_relative | 1.0 | 21147 | 21204 | 99.7 |


---

## Stop calibration — recomputed on this horizon

Stop-less holds over the whole DEV panel; the stop is the p75 of the
adverse excursion suffered by positions that were **profitable at the horizon**, in daily
ATRs. A winner is defined by profit at the horizon rather than by reaching a target,
because the target is derived from the stop and defining winners by it would be circular.

| horizon | stop-less holds measured | profitable at horizon | win rate % | survivable MAE p50 | survivable MAE p75 | STOP (daily ATR) | TARGET (daily ATR) | target reachable % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1d | 22933 | 11901 | 51.9 | 0.119 | 0.302 | 0.302 | 0.452 | 48.4 |


---

## Results

| arm | score bar | trades | NO_SIGNAL | gross mean % | net mean % | panel comparator % | EXCESS mean % | excess 95% CI (effective n) | effective n | MDE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| primary (pre-stated bar) | +0.25 | 907 | 209 | 0.0022 | -0.3362 | 0.0903 | -0.088 | [-0.1698, -0.0062] | 907 | 0.1168 |
| power reference (no bar) | — | 1114 | 2 | 0.0004 | -0.3381 | 0.1123 | -0.1119 | [-0.1851, -0.0387] | 1114 | 0.1046 |


Gross is the number the decision rule turns on: if the features do not predict, no cost
treatment rescues them. Net is reported beside it throughout, per CLAUDE.md 1.4.

**The comparator column is the point.** Beta is not edge: a long-only rule in a market
that rose makes money for reasons that have nothing to do with open interest. Excess is
gross minus an equal-weight hold of the same panel over the *same entry and exit dates*.

### The power-reference arm exists so a null cannot hide behind a small sample

The pre-stated bar of +0.25 was fixed from the scale, not from the data, and
it was always possible it would leave too few trades to conclude anything. The
no-bar arm was declared in advance for exactly that reason: it takes the day's best
candidate every session, giving the largest sample this design can produce. Both are
reported whatever they show.

---

## Score distribution

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F&O composite score | 21204 | 0.011 | -0.162 | 0.021 | 0.187 | 0.339 | 0.429 | 0.847 |


The bar of **+0.25** was set from the [-1, +1] scale before any P&L existed —
zero means the weighted evidence is balanced, and the bar asks for a quarter of the way
from balanced to unanimous. It is **not** a percentile of this distribution. Had it been,
it would have been chosen after seeing the data it was meant to filter.

### OI–price state distribution across scored symbol-days

| state | n | share % |
| --- | --- | --- |
| SHORT_COVERING | 7238 | 36.1 |
| LONG_UNWINDING | 6068 | 30.3 |
| SHORT_BUILDUP | 3610 | 18.0 |
| LONG_BUILDUP | 3120 | 15.6 |


Frequencies only. Per-state P&L is deliberately **not** reported: the pre-registration
forbids searching for a subset that works, and a table of returns broken out by state is
the most direct invitation to do it.

---

## Exit distribution

| arm | exit | n | pct |
| --- | --- | --- | --- |
| primary | STOP | 384 | 42.3 |
| primary | TARGET | 257 | 28.3 |
| primary | STOP_GAP | 94 | 10.4 |
| primary | TIME | 88 | 9.7 |
| primary | TARGET_GAP | 84 | 9.3 |
| power reference | STOP | 482 | 43.3 |
| power reference | TARGET | 312 | 28.0 |
| power reference | STOP_GAP | 113 | 10.1 |
| power reference | TARGET_GAP | 105 | 9.4 |
| power reference | TIME | 102 | 9.2 |


Gapped through the stop: 94 of the primary arm's stops
(19.7%), mean shortfall 0.866% of entry.
A stop cannot execute while the market is shut, so those fill at the OPEN.

---

## Costs — delivery, because the position is held overnight

Round trip on Rs 1,00,000: **Rs 338.41**
(0.338%), of which STT is
Rs 200.00 charged on **both** legs at 0.1%, against
intraday's 0.025% on the sell only. Delivery costs more than intraday, not less, despite
zero brokerage.

> Cost rates are unverified against actual contract notes
> (`rates_verified=False`), and delivery brokerage is as supplied by
> the account holder. The statutory charges are the material part and do not depend on
> the broker. The decision rule turns on GROSS excess, so this does not affect the
> conclusion.

---

## Caveats

- **DEV only.** The swing holdout (2025-01-01 onward) is untouched and, under the
  decision rule, stays that way.
- **Regime coverage of the panel:** 2020 crash & recovery 251 DEV, 2021-22 trending/bear 494 DEV, 2023-24 recovery 491 DEV.
- **India VIX sessions missing from DEV:** 0. Where VIX is absent the
  two market-wide components are unavailable and drop out of the composite with their
  weight, rather than being counted as neutral.
- **The feature set was not pre-registered, only the constants and the rule.** Nine
  components chosen from domain convention is itself a design choice with freedom in it.
  What is protected is that no constant was tuned against an outcome.
- **Survivorship**: today's liquid names applied backwards. TMCV is excluded because no
  futures contract on it exists in the exchange file on any date; TMPV because its
  futures begin 2025-10-24, outside DEV entirely.
- **Within-day resolution order** assumes a day touching both stop and target resolves as
  the LOSS, which biases results downward.
- **The F&O ban check is a stub.** A symbol in ban has no new-position capacity and
  should not be rankable; that gate is not yet implemented.
- **Prices are split/bonus adjusted**, so percentage returns are correct but absolute
  prices and share counts are not what would have been seen at the time.

---

*Written by `scripts/fno_evaluation.py`.*
