"""``last_tick_at`` means ONE thing, and REPLAY and LIVE must both mean it.

Why this file exists
--------------------

REPLAY and LIVE disagreed about what ``last_tick_at`` was. LIVE recorded tick arrival;
REPLAY returned the close of the last COMPLETED bar. Both satisfied the Protocol's type
signature, so nothing failed -- but the engine's one staleness threshold was being
applied to two different quantities.

The consequence was measurable and wrong: a completed bar only appears once a minute by
construction, so measuring bar recency against a 30-second threshold declared a perfectly
healthy replayed feed stale for the last 29 seconds of every minute. A replayed position
flipped ``HOLD -> STALE_DATA -> HOLD`` once a minute. The common speed settings only
dodged it because their clock steps landed exactly on :00 and :30.

That defeats the point of replay. "Fully driveable in REPLAY before it ever runs live" is
only true if REPLAY exercises the same code on inputs that MEAN the same thing.

The agreed meaning, now in :class:`~src.positions.feed.PriceFeed`:

    the most recent moment this feed HAD DATA for the symbol.

Live feeds satisfy it by recording tick arrival. Stored feeds satisfy it through
:func:`~src.positions.feed.data_available_through`, which answers ``cursor`` when the
minute containing the cursor has a bar, and the previous bar's close when it does not.

**No threshold was changed to achieve this.** ``stale_feed_seconds`` is still 30.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from config.settings import get_exit_engine_settings
from src.positions.exit_engine import evaluate, feed_staleness
from src.positions.feed import (
    MultiSymbolReplayFeed,
    ReplayFeed,
    data_available_through,
)
from src.positions.live_feed import FyersLiveFeed
from src.positions.models import Position, PositionRuntime, PositionState
from tests.conftest import TRADE_DATE, ist, make_series

CFG = get_exit_engine_settings()
SYMBOL = "RELIANCE"


def bars(minutes: int = 20, start=None):
    return make_series([1000.0] * minutes, start=start or ist(10, 0),
                       volumes=[20000] * minutes, spread=1.2)


def stale_at(feed, now, symbol=SYMBOL):
    """The engine's own staleness verdict for whatever this feed reports."""
    return feed_staleness(now, feed.last_tick_at(symbol), CFG)[0]


# --------------------------------------------------------------------------------
# data_available_through: the stored-feed implementation of the agreed meaning
# --------------------------------------------------------------------------------


def test_a_minute_with_a_bar_reports_data_right_now():
    """The fix. Mid-bar is not staleness -- a live socket is receiving ticks here."""
    series = bars()
    for second in (0, 1, 15, 29, 31, 45, 59):
        at = ist(10, 5) + timedelta(seconds=second)
        assert data_available_through(series, at) == at, second


def test_a_hole_in_the_session_still_goes_stale():
    """The property the old definition existed to provide. It must survive."""
    holed = [b for b in bars() if not (ist(10, 3) <= b.ts < ist(10, 7))]

    assert data_available_through(holed, ist(10, 2, 30)) == ist(10, 2, 30)
    # Inside the hole the answer stops moving, so the age grows exactly as a dead
    # socket's would.
    assert data_available_through(holed, ist(10, 3, 15)) == ist(10, 3)
    assert data_available_through(holed, ist(10, 4, 30)) == ist(10, 3)
    assert not feed_staleness(ist(10, 3, 15), ist(10, 3), CFG)[0]
    assert feed_staleness(ist(10, 3, 45), ist(10, 3), CFG)[0]
    # And it recovers the moment data resumes.
    assert data_available_through(holed, ist(10, 7, 10)) == ist(10, 7, 10)


def test_past_the_last_bar_the_age_grows():
    series = bars()                       # 10:00..10:19, last closes 10:20
    assert data_available_through(series, ist(10, 19, 30)) == ist(10, 19, 30)
    assert data_available_through(series, ist(10, 20)) == ist(10, 20)
    assert data_available_through(series, ist(10, 25)) == ist(10, 20)
    assert feed_staleness(ist(10, 25), ist(10, 20), CFG)[0]


def test_before_the_first_bar_there_is_no_data():
    assert data_available_through(bars(), ist(9, 30)) is None
    assert data_available_through([], ist(10, 5)) is None
    assert data_available_through(bars(), None) is None


def test_only_the_existence_of_the_forming_bar_is_consulted():
    """Not look-ahead: a live feed knows the same thing at the same instant.

    Changing the forming bar's VALUES must not change the answer. If it could, this
    function would be leaking a price the engine is not allowed to see yet.
    """
    from src.positions.models import Bar

    series = bars()
    at = ist(10, 5, 30)
    baseline = data_available_through(series, at)

    poisoned = [
        Bar(ts=b.ts, open=b.open, high=b.high * 3, low=b.low / 3,
            close=b.close * 2, volume=b.volume * 7) if b.ts == ist(10, 5) else b
        for b in series
    ]
    assert data_available_through(poisoned, at) == baseline


# --------------------------------------------------------------------------------
# THE INVARIANT, stated directly
# --------------------------------------------------------------------------------


def live_feed_with_ticks(through, *, every_seconds: int = 5, start=None):
    """A real FyersLiveFeed driven with ticks up to ``through`` and none after.

    Every network dependency is injected, so this is the live class itself rather than
    a stand-in -- the invariant is worthless if it is asserted against a mock.
    """
    feed = FyersLiveFeed([SYMBOL], history=object(), socket_factory=lambda kw: object(),
                         clock=lambda: through)
    at = start or ist(10, 0)
    while at <= through:
        feed.on_tick(SYMBOL, at, 1000.0, 10)
        at += timedelta(seconds=every_seconds)
    return feed


@pytest.mark.parametrize("offset_seconds", [0, 1, 15, 29, 31, 45, 59, 61, 90, 300])
def test_replay_and_live_agree_on_staleness_given_equivalent_inputs(offset_seconds):
    """THE INVARIANT. Same data, same instant, same verdict -- in both modes.

    "Equivalent inputs" is precise here: both feeds hold data whose LAST DATUM lands at
    the same instant, 10:20:00. The live one got ticks up to it; the replayed one got
    the bars those ticks would have aggregated into, the last covering 10:19-10:20.

    If the two disagree at any instant, replay is not a rehearsal for live and every
    conclusion drawn in replay is about a different system.
    """
    through = ist(10, 20)
    live = live_feed_with_ticks(through, every_seconds=1)
    replay = MultiSymbolReplayFeed({SYMBOL: bars()})

    now = through + timedelta(seconds=offset_seconds)
    replay.advance_to(now)

    assert stale_at(live, now) == stale_at(replay, now), (
        f"at +{offset_seconds}s live says stale={stale_at(live, now)} and replay says "
        f"stale={stale_at(replay, now)} -- the two modes disagree about the same data"
    )


def test_stored_bars_cannot_say_WHEN_in_a_minute_the_last_print_was():
    """The residual difference, measured and bounded rather than hidden.

    A bar stamped 10:19 says "this symbol printed somewhere in 10:19-10:20". It does NOT
    say the last print was at 10:19:59. So for a symbol whose final print was at 10:19:03
    a live feed goes stale at 10:19:33, while replay -- which has only the bar -- cannot
    know that and goes stale at 10:20:30.

    This is irreducible: the information is not in the stored data. It is bounded by ONE
    bar interval, it makes replay OPTIMISTIC about staleness rather than pessimistic, and
    it is the honest limit of the unified definition. Asserting exact equality here would
    be asserting something false.
    """
    last_print = ist(10, 19, 3)
    live = live_feed_with_ticks(last_print, every_seconds=1)
    replay = MultiSymbolReplayFeed({SYMBOL: bars()})

    worst = timedelta(0)
    for offset in range(0, 121, 3):
        now = last_print + timedelta(seconds=offset)
        replay.advance_to(now)
        gap = abs(feed_staleness(now, live.last_tick_at(SYMBOL), CFG)[1]
                  - feed_staleness(now, replay.last_tick_at(SYMBOL), CFG)[1])
        worst = max(worst, timedelta(seconds=gap))

    assert worst <= timedelta(minutes=1), worst
    assert worst > timedelta(seconds=30), (
        "this test is meant to exercise the disagreement, not a case without one")


@pytest.mark.parametrize("second", [0, 15, 29, 31, 45, 59])
def test_replay_and_live_agree_at_every_point_within_a_minute(second):
    """The specific disagreement that was there: mid-bar, mid-session, feed healthy."""
    now = ist(10, 10) + timedelta(seconds=second)
    live = live_feed_with_ticks(now)
    replay = MultiSymbolReplayFeed({SYMBOL: bars()})
    replay.advance_to(now)

    assert stale_at(live, now) is False, "a live feed ticking now is not stale"
    assert stale_at(replay, now) is False, (
        f"replay called a healthy feed stale {second}s into the bar")


def position_at(entry=ist(10, 0)):
    from decimal import Decimal

    return Position(position_id="p1", symbol=SYMBOL, quantity=100,
                    entry_price=Decimal("1000.00"), entry_time=entry,
                    trade_date=TRADE_DATE)


@pytest.mark.parametrize("second", [0, 29, 31, 59])
def test_the_engine_reaches_the_same_state_in_both_modes(second):
    """One level up: the invariant where it actually matters, on the advice itself."""
    now = ist(10, 10) + timedelta(seconds=second)
    series = bars()

    live = live_feed_with_ticks(now)
    replay = MultiSymbolReplayFeed({SYMBOL: series})
    replay.advance_to(now)

    position = position_at()
    states = []
    for feed in (live, replay):
        advice = evaluate(position, PositionRuntime.initial(position), series, now,
                          feed.last_tick_at(SYMBOL), engine_cfg=CFG)
        states.append(advice.state)

    assert states[0] == states[1], f"LIVE {states[0]} vs REPLAY {states[1]} at +{second}s"


def test_a_healthy_replayed_position_no_longer_flickers():
    """The defect, gone. It used to read HOLD, HOLD, STALE_DATA, STALE_DATA.

    Fixed by unifying the MEANING, not by moving the threshold: stale_feed_seconds is
    still 30, which the assertion below pins so a future "fix" by nudging it is visible.
    """
    assert int(CFG.stale_feed_seconds) == 30, "the threshold must not have moved"

    series = bars()
    feed = ReplayFeed(SYMBOL, series)
    position = position_at()

    seen = []
    for second in (0, 15, 29, 31, 45, 59):
        now = ist(10, 10) + timedelta(seconds=second)
        feed.advance_to(now)
        advice = evaluate(position, PositionRuntime.initial(position), feed.bars(), now,
                          feed.last_tick_at(), engine_cfg=CFG)
        seen.append(advice.state)

    assert PositionState.STALE_DATA not in seen, seen
    assert len(set(seen)) == 1, f"the state still changes within one minute: {seen}"


# --------------------------------------------------------------------------------
# Nothing that has already been measured moves
# --------------------------------------------------------------------------------


def test_at_bar_close_times_the_new_definition_matches_the_old_one_exactly():
    """`run_session` evaluates ONLY at bar close times, and there the two agree.

    That is what makes this change safe for every number already reported from a batch
    replay -- reports/momentum_analysis.md in particular. Were it not true, §8's figures
    would have silently become unreproducible.
    """
    series = bars()
    feed = ReplayFeed(SYMBOL, series)

    for step in feed.steps():
        feed.advance_to(step)
        completed = [b for b in series if b.close_time <= step]
        old = completed[-1].close_time if completed else None
        assert feed.last_tick_at() == old, step


def test_the_batch_replay_driver_evaluates_only_at_bar_close_times():
    """The premise the test above rests on, pinned rather than assumed."""
    import inspect

    from src.positions import replay as mod

    source = inspect.getsource(mod.replay_session)
    assert "for step in feed.steps()" in source
    assert "feed.advance_to(step)" in source
