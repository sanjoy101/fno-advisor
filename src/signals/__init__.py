"""Module B -- Signal Engine.  BUILT, and it does not yet earn its place.

Ranks the F&O universe every 5 minutes from 09:30 to 14:30 and surfaces UP TO 2 long
candidates. Rule-based; no ML. See ``reports/signal_evaluation.md``.

Layout
------
``features.py``  causal feature functions -- opening range, trend, momentum, volume,
                 volatility, relative strength
``scoring.py``   each feature mapped to [-1, +1] and combined into a composite
``universe.py``  hard gates: F&O ban (STUB), liquidity, minimum ATR, one per sector
``models.py``    Candidate, ComponentScore, SignalDecision, Rejection
``engine.py``    the ranking and selection pipeline
``evaluate.py``  DEV evaluation and the baseline to beat

Rules this module is built to
-----------------------------
* **No look-ahead.** Every feature takes ``(bars, at)`` and does its own
  completed-bar filtering; ``tests/test_signal_leak.py`` evaluates each one with
  future bars present, absent and poisoned and fails on any disagreement. Verified on
  real bars as well as synthetic. Cross-session leaks need
  ``assert_session_causal`` -- the bar-level harness cannot see them.
* **Up to 2 is a CEILING, never a quota.** Zero candidates is a correct answer. Never
  relax a threshold or rank-fill to reach two.
* **Long only.** Module C's exit logic is long-specific; a short would get inverted,
  confidently wrong advice.
* **Module C has the final word.** Every candidate must pass its entry-validity check
  before being surfaced. A signal the exit engine would instantly flag as
  INVALID_ENTRY is a bug here.
* **An unavailable feature scores None, not zero.** Unknown is not neutral; it is
  dropped from the weighted average along with its weight.

What the DEV evaluation found (2020-01-01..2024-11-26, 1212 sessions, n=2368)
------------------------------------------------------------------------------
It loses money, and it loses more than the baseline. Mean net Rs -209/trade against
the "buy the highest-volume stock at 09:45" baseline's Rs -166. Worse, Module B is
NEGATIVE before costs while the baseline is positive before costs -- the selection
itself is the problem, not the friction.

Three structural faults, all diagnosed and none tuned away:

1. 97.5% of exits are STOP_HIT. The 1-ATR stop sits at roughly one MAE, so it is not
   protecting the trade, it is the exit.
2. The hit rate is far below what the declared R:R needs to break even.
3. NO_SIGNAL fires on ~1% of sessions. The score bar is too low to discriminate, and
   the daily budget fills in the first evaluation or two, so 59 of the 61 scheduled
   evaluations never happen.

Do NOT fix these by editing thresholds until the numbers improve. That is fitting to
DEV. Change one thing, re-measure on DEV, and keep the holdout for a single
evaluation at the end -- see ``src/analysis/split.py``.
"""
