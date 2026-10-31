# Multi-day swing experiment — DEV only

**Generated:** 2026-08-21T15:44:34
**Panel:** 20 symbols, DEV 2020-01-01 .. 2024-12-31 (1236 sessions)
**Holdout:** 2025-01-01 onward — HOLDOUT UNUSED -- available for exactly one evaluation.
**Entry:** 15:15 on the signal day, from that day's completed bars only
**Arms:** 1 trading days, 3 trading days, 5 trading days, reported separately

---

## Conclusion — the pre-stated rule applies

**Gross excess return over the buy-and-hold baseline does not clear zero at any of the
three horizons.** On the effective sample, at 1d the interval lies ENTIRELY BELOW zero; at 3d, 5d it spans zero. None lies above it, and
every point estimate is negative.

| horizon | excess mean % | 95% CI (effective n) | effective n | MDE | verdict |
| --- | --- | --- | --- | --- | --- |
| 1d | -0.12 | [-0.2078, -0.0322] | 898 | 0.1254 | NEGATIVE — interval entirely below zero |
| 3d | -0.089 | [-0.2172, 0.0393] | 300 | 0.1832 | INCONCLUSIVE — interval spans zero, and |-0.089| is below the 0.1832 MDE, so the sample could not have detected an effect this small either way |
| 5d | -0.0704 | [-0.2292, 0.0964] | 180 | 0.2326 | INCONCLUSIVE — interval spans zero, and |-0.0704| is below the 0.2326 MDE, so the sample could not have detected an effect this small either way |


The rule fixed before this ran was:

> If gross excess return over the buy-and-hold baseline does not clear zero on DEV at
> any of the three horizons, the feature set does not predict on this horizon either,
> and the conclusion is that these features carry no edge at any tested timescale.

**That is the answer. These features carry no edge at any tested timescale.**

They have now been tested intraday (~16-minute holds, both continuous and single-shot
selection) and on 1, 3 and 5 trading-day holds, against a naive comparator each time.
Not one of those tests produced an excess whose interval clears zero. The consistent
result across horizons is itself the finding: the problem is the features, not the
holding period and not the friction.

Per the rule: no holdout evaluation, no sweep, no fourth horizon.

---

## Costs — delivery, and it is WORSE than intraday

A position carried overnight settles as delivery (CNC), not intraday (MIS). That was
checked rather than assumed, and it moves the arithmetic in the opposite direction to
what zero brokerage suggests:

| | intraday (MIS) | delivery (CNC) |
| --- | --- | --- |
| brokerage, round trip | Rs 40.00 | Rs 13.50 |
| STT | Rs 25.00 (sell only, 0.025%) | **Rs 200.00** (both legs, 0.1% each) |
| stamp duty | Rs 3.00 | Rs 15.00 |
| slippage (modelled) | Rs 100.00 | Rs 100.00 |
| **total on Rs 1,00,000** | **Rs 182.68** (0.183%) | **Rs 338.41** (0.338%) |

**Delivery costs 1.9x what
intraday costs, despite zero brokerage**, because delivery STT is charged on both legs
at four times the intraday rate. The premise that "friction is not the dominant term
on a longer horizon" holds only because the expected MOVE is larger over days — not
because friction shrinks. It grows.

> Brokerage of Rs 0 on delivery is as supplied by the account holder and is NOT
> independently verified here. The statutory charges are the material part and do not
> depend on the broker.

---

## Stop calibration — recomputed on THIS horizon, not reused

The intraday stop was 2.37 intraday-ATRs. It is not carried over: daily ATR spans the
overnight gap and the holding window is days, so survivable noise is a different
distribution. Measured the same way — stop-less holds, p75 of the
adverse excursion suffered by positions that were PROFITABLE AT THE HORIZON, in DAILY
ATRs.

A winner is defined by profit at the horizon, not by reaching a target. Defining it by
a target would be circular — the target is derived from the stop, the stop from what
winners survived, and the winners from the target. A first attempt did exactly that
and produced a stop of 0.32 ATR against a traded target of 0.48 ATR, calibrated
against a 1.5 ATR target that was never used.

| horizon | observations | profitable at horizon | win rate % | survivable MAE p50 | survivable MAE p75 | STOP (ATR) | TARGET (ATR) | target reachable % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1d | 24140 | 12529 | 51.9 | 0.116 | 0.299 | 0.299 | 0.448 | 48.9 |
| 3d | 24140 | 12908 | 53.5 | 0.293 | 0.571 | 0.571 | 0.856 | 45.7 |
| 5d | 24140 | 13246 | 54.9 | 0.408 | 0.759 | 0.759 | 1.138 | 46.2 |


The stop widens with the horizon, as it must: a five-day hold has to sit through more
noise than a one-day hold.

---

## Results — gross first

| horizon | trades | NO_SIGNAL | gross mean % | net mean % | EXCESS mean % | excess 95% CI (effective) | hit rate excess % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1d | 898 | 298 | 0.0424 | -0.2961 | -0.12 | [-0.2078, -0.0322] | 45.2 |
| 3d | 898 | 298 | 0.1707 | -0.168 | -0.089 | [-0.2172, 0.0393] | 45.3 |
| 5d | 898 | 298 | 0.2534 | -0.0854 | -0.0704 | [-0.2292, 0.0964] | 46.9 |


Gross is the number that matters here: if the features do not predict, no cost
treatment rescues them.

### Gross is positive, and all of it is beta

| horizon | gross mean % | minus panel buy-and-hold | = EXCESS % |
| --- | --- | --- | --- |
| 1d | 0.0424 | 0.1624 | -0.12 |
| 3d | 0.1707 | 0.2597 | -0.089 |
| 5d | 0.2534 | 0.3238 | -0.0704 |


Raw gross rises with the horizon and looks like an edge growing. It is not. Subtract an
equal-weight hold of the same panel over the SAME entry and exit dates and what remains
is negative at every horizon. **The strategy was long in a market that went up** — that
is what the baseline exists to strip out, and stripping it out is what turns an
apparent +0.25% into −0.07%.

---

## Effective sample size — the trade count is not it

Daily entries with an h-day hold share h−1 days with their neighbours, and trades
opened on the same date share that day's market move. The unit is therefore the
non-overlapping DATE BLOCK, not the trade.

| horizon | trades (raw) | distinct entry dates | effective n (blocks) | overstatement | SD of block excess | MDE at effective n |
| --- | --- | --- | --- | --- | --- | --- |
| 1d | 898 | 898 | 898 | 1.0x | 1.3421 | 0.1254 |
| 3d | 898 | 898 | 300 | 3.0x | 1.1334 | 0.1832 |
| 5d | 898 | 898 | 180 | 5.0x | 1.1144 | 0.2326 |


The MDE column is computed from the effective n. Using the raw trade count would
shrink it by roughly the square root of the overstatement factor and turn a null into
a discovery.

---

## Overnight gaps — a stop cannot execute while the market is shut

| horizon | stopped trades | gapped through | % of stops that gapped | mean shortfall Rs | mean shortfall % |
| --- | --- | --- | --- | --- | --- |
| 1d | 509 | 82 | 16.1 | 10.09 | 0.819 |
| 3d | 455 | 45 | 9.9 | 10.26 | 0.79 |
| 5d | 447 | 44 | 9.8 | 10.24 | 0.85 |


Gapped trades are filled at the OPEN, not at the stop. The shortfall column is the
difference, which is money the position lost with no opportunity to act.

---

## Exit distribution

| horizon | exit | n | pct |
| --- | --- | --- | --- |
| 1d | STOP | 427 | 47.6 |
| 1d | TARGET | 249 | 27.7 |
| 1d | TARGET_GAP | 104 | 11.6 |
| 1d | STOP_GAP | 82 | 9.1 |
| 1d | TIME | 36 | 4.0 |
| 3d | STOP | 410 | 45.7 |
| 3d | TARGET | 317 | 35.3 |
| 3d | TIME | 81 | 9.0 |
| 3d | STOP_GAP | 45 | 5.0 |
| 3d | TARGET_GAP | 45 | 5.0 |
| 5d | STOP | 403 | 44.9 |
| 5d | TARGET | 323 | 36.0 |
| 5d | TIME | 85 | 9.5 |
| 5d | STOP_GAP | 44 | 4.9 |
| 5d | TARGET_GAP | 43 | 4.8 |


---

## Corporate actions

The 2020–2026 backfill was checked during ingestion: Fyers serves split- and
bonus-ADJUSTED history. A >20% discontinuity scan across 21 symbols and six years
found exactly one step, and it was the COVID crash (AXISBANK, 2020-03-20 → 2020-03-24,
−21.8%), not a corporate action. Verified against known events — TATASTEEL's 1:10
split, RELIANCE's and WIPRO's 1:1 bonuses all pass through with no discontinuity.

Consequently **no trade in this experiment has a return that depends on an unadjusted
corporate action**. The cost of that adjustment is stated elsewhere and still applies:
absolute historical prices and share counts are not what would have been seen at the
time, though percentage returns — which is all this experiment uses — are correct.

---

## Score distribution and bar

| group | n | mean | p25 | p50 | p75 | p90 | p95 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| daily composite score | 3420 | 0.124 | -0.238 | 0.19 | 0.443 | 0.64 | 0.752 | 0.982 |


Bar set at p90 = **0.640**, the same rule as the intraday
calibration. The value is re-read rather than reused because the daily distribution is
a different one.

---

## Caveats

- **DEV only.** The holdout (2025-01-01 onward) is untouched and, under the decision
  rule above, stays that way.
- **Every daily feature passed `assert_session_causal`** at the new frequency, plus
  two deliberately leaky controls that the check correctly rejected.
- **Within-day resolution order** assumes a day touching both stop and target resolves
  as the LOSS, because the order inside a daily bar is unknowable. This biases results
  downward.
- **The F&O ban check is a stub.**
- **Costs unverified** (`rates_verified=False`), and delivery
  brokerage is as supplied, not confirmed.
- **Survivorship**: today's liquid names applied backwards.

---

*Written by `scripts/swing_evaluation.py`.*
