# Entry-check diagnostic — does the check discriminate?

## The answer, first

**The verdict does not separate forward outcomes to any degree worth acting on.**

SOUND minus UNSOUND on the forward 60-minute return, cross-sectionally
demeaned, is **+0.0287pp** with a 95% interval of
**[-0.0067, +0.0641]** across 3,626 independent
session-slot cells. The interval includes zero
(t = +1.59).

**The size settles it even if the sign is real.** A round trip costs
**0.183%** of notional (computed from `config/settings.py`, not copied). The
whole measured SOUND-vs-UNSOUND gap is **16% of one round trip**. Taking
the point estimate entirely at face value, and assuming it survived out of sample, it
would still be 6.4x too small to pay for the trade it is advising on.

The check is **not** firing on pure noise — the median UNSOUND-by-VWAP verdict sits
0.300% below VWAP, about
3.0 one-minute bar ranges, and only
18.35% are inside a single one-minute range. It is describing a real
displacement. That displacement simply does not predict the next hour.

It also fires less than it feels like: **37.17% SOUND** overall, not
"nearly everything UNSOUND" — though it reaches 33.44%
SOUND at the least permissive slot.

**Treat the verdict as a description of the present, not a forecast.** "Price is below
VWAP and momentum has rolled over" is a true and checkable statement about right now.
This measurement says it carries no information about the next 60 minutes, so a UNSOUND
badge should not be read as "this will fall" — and a SOUND badge emphatically should not
be read as "this will rise".

---


**DEV ONLY.** Panel `full-history-2020`, 2020-01-01..2024-11-26,
1212 sessions, 21 symbols, four slots per session.
**n = 96,720 evaluations.** Regimes spanned: 2020 crash & recovery, 2021-22 trending/bear, 2023-24 recovery.
1,272 symbol-sessions had no stored bars and were skipped.

**The six-year holdout is NOT touched and NOT claimed.** This is a diagnostic, not an
evaluation of a change; spending the only out-of-sample data on it would be waste.

**No threshold was changed.** Everything here is measured against the shipping config.

---

## 1. How often does it fire?

| n | sound | unsound | sound_pct | partly_unevaluated |
|---|---|---|---|---|
| 96720 | 35946 | 60774 | 37.17 | 1 |

By slot:

| slot | n | sound | unsound | sound_pct |
|---|---|---|---|---|
| 09:45 | 24180 | 11156 | 13024 | 46.14 |
| 11:00 | 24180 | 8553 | 15627 | 35.37 |
| 13:00 | 24180 | 8152 | 16028 | 33.71 |
| 14:30 | 24180 | 8085 | 16095 | 33.44 |

---

## 2. Which predicate fired?

Keyed off `EntryProblem` tags, never the prose. "Present in" double-counts overlaps;
"sole cause" is what would change if the predicate were deleted.

| problem | present_n | present_pct | sole_cause_n | sole_cause_pct |
|---|---|---|---|---|
| BELOW_VWAP | 51992 | 85.55 | 17559 | 28.89 |
| MOMENTUM_REVERSED | 43215 | 71.11 | 8782 | 14.45 |
| STOP_ALREADY_HIT | 0 | 0.0 | 0 | 0.0 |

Exact combinations:

| combination | n | pct |
|---|---|---|
| BELOW_VWAP + MOMENTUM_REVERSED | 34433 | 56.66 |
| BELOW_VWAP | 17559 | 28.89 |
| MOMENTUM_REVERSED | 8782 | 14.45 |

**STOP_ALREADY_HIT fired 0 times, and that is structural, not
empirical.** The panel sends no stop, and with no stop the predicate has nothing to
compare against. Even when a stop IS supplied, `check_hypothetical_entry` defaults the
entry price to the current price, so the condition can only fire if the user types a
stop at or above where the stock is already trading. It is a property of the user's
input, not of the market. **The check is effectively two predicates, not three.**

---

## 3. How far below VWAP is a typical UNSOUND verdict?

n = 51,992 observations where BELOW_VWAP fired (51,992 with an
ATR to scale by). Positive numbers are distance BELOW VWAP.

Percent below VWAP:

| p10 | p25 | p50 | p75 | p90 | p95 |
|---|---|---|---|---|---|
| 0.0559 | 0.1394 | 0.3003 | 0.5533 | 0.9042 | 1.206 |

Same distances in ATR multiples. **Read this carefully: `atr14` here is the 14-period
ATR of ONE-MINUTE bars, not a daily ATR.** One unit is a typical one-minute bar range,
so "3.5 ATR below VWAP" means about three and a half minutes' worth of normal movement
— not the enormous number it would be on a daily scale. This is the scale that says
whether a gap is chatter, because a fixed 0.2% is a wide move for a quiet symbol and
inside the spread for a volatile one:

| p10 | p25 | p50 | p75 | p90 | p95 |
|---|---|---|---|---|---|
| 0.5456 | 1.3619 | 2.9801 | 5.5668 | 8.4535 | 10.3635 |

| within | share of BELOW_VWAP verdicts |
|---|---|
| 0.10% | 17.98% |
| 0.25% | 42.68% |
| 0.50% | 71.01% |
| 0.25 one-minute ATR | 4.58% |
| 0.50 one-minute ATR | 9.15% |
| 1.00 one-minute ATR | 18.35% |

The one-minute-ATR row is the "is this noise" number: a gap smaller than a single
typical one-minute bar range is not a market trading below its average, it is a tick.

---

## 4. Does the verdict predict anything? — THE ONE THAT MATTERS

Forward 60-minute return after SOUND vs after UNSOUND.

**Primary measure is cross-sectionally demeaned**: each observation minus the mean
forward return of every symbol at that same session and slot. Without that, a verdict
that fires on falling mornings scores a gap purely because falling mornings keep
falling — a fact about the market, not about the check (CLAUDE.md 2.3).

Intervals use the **independent-cell count** (session x slot), not the observation
count: 20 symbols sampled in one minute share that minute's market move.

| label | sound_n | sound_mean | unsound_n | unsound_mean | diff | ci95_low | ci95_high | effective_n | mde |
|---|---|---|---|---|---|---|---|---|---|
| 60-min, demeaned | 27859 | 0.0177 | 44661 | -0.011 | 0.0287 | -0.0067 | 0.0641 | 3626 | 0.0243 |
| 60-min, raw | 27859 | 0.0172 | 44661 | -0.0174 | 0.0346 | -0.0072 | 0.0763 | 3626 | 0.0288 |

**Demeaned:** INDISTINGUISHABLE (diff +0.0287pp, 95% CI [-0.0067, +0.0641] on 3626 independent cells). The sample could have detected an effect this size, so this is a measured null, not an absent measurement.

**Raw (not the answer — carries the market's own move):** INDISTINGUISHABLE (diff +0.0346pp, 95% CI [-0.0072, +0.0763] on 3626 independent cells). The sample could have detected an effect this size, so this is a measured null, not an absent measurement.

Per slot, demeaned:

| label | sound_n | sound_mean | unsound_n | unsound_mean | diff | ci95_low | ci95_high | effective_n | mde |
|---|---|---|---|---|---|---|---|---|---|
| 09:45 | 11156 | 0.0172 | 13024 | -0.0147 | 0.032 | -0.0395 | 0.1035 | 1209 | 0.0508 |
| 11:00 | 8553 | 0.0144 | 15627 | -0.0079 | 0.0222 | -0.033 | 0.0775 | 1209 | 0.0372 |
| 13:00 | 8150 | 0.0218 | 16010 | -0.0111 | 0.0328 | -0.0215 | 0.0872 | 1208 | 0.0361 |

The 14:30 slot, whose window is truncated at the 15:15 square-off
(45 minutes, so not comparable with the rest):

| label | sound_n | sound_mean | unsound_n | unsound_mean | diff | ci95_low | ci95_high | effective_n | mde |
|---|---|---|---|---|---|---|---|---|---|
| 14:30 slot, truncated at 15:15 | 8083 | 0.0164 | 16077 | -0.0083 | 0.0247 | -0.0379 | 0.0873 | 1208 | 0.0414 |

---

## What this does NOT license

Nothing here says the entry check should be deleted, kept, or retuned. It says what the
check does and whether the verdict separated forward outcomes on DEV. Any change to a
threshold made in response to these numbers is fitting to DEV, and the honest way to
act on it is to change ONE thing, re-measure on DEV, and keep the holdout for a single
evaluation at the end (CLAUDE.md 2.3).

The check is a statement about whether an entry is *already* violated, not a
prediction. If section 4 is indistinguishable from zero, that does not make the check
wrong — it makes it uninformative about the next hour, which is a narrower and more
useful thing to know.
