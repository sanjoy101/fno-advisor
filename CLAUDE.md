# CLAUDE.md — fno-advisor

Operating rules for any Claude session working in this repo. Read this fully before
writing code. These rules are not suggestions; they encode the safety properties the
system is built around.

---

## 0. What this project is

A **personal, single-user, intraday trading DECISION-SUPPORT dashboard** for Indian
NSE F&O stocks.

- Backend: Python 3.11, FastAPI.
- Frontend: React (in `web/`, not yet scaffolded).
- Platform: Windows 11.
- Audience: exactly one person — the repo owner. Not a product, not multi-tenant.

It **advises**. A human reads the advice and acts. That distinction drives everything below.

---

## 1. HARD CONSTRAINTS

These are invariants. If a task appears to require breaking one, **stop and ask the
user** rather than working around it.

### 1.1 ADVISORY ONLY — never place orders

The system **NEVER** places, modifies, or cancels an order. No broker order-placement
API is ever called, not behind a flag, not in a "test mode", not in a comment as a
future step. The user executes every trade manually in their broker app.

Permitted broker API surface:
- authentication / token generation
- profile, funds, holdings, positions (**read** only)
- quotes, market depth, historical candles
- symbol master / instrument lists

Forbidden broker API surface (non-exhaustive):
- `place_order`, `modify_order`, `cancel_order`, `exit_positions`
- basket / bracket / cover order endpoints
- any GTT or algo-order endpoint
- any websocket *write* channel

`config/settings.py` exposes `ALLOW_ORDER_PLACEMENT`, which is hard-wired `False` and
validated to reject `True`. It exists as a tripwire, not as a feature switch. Do not
add a code path that reads it and would place an order if it were `True`.

Vocabulary matters in output the user reads: the system emits **advice** —
`SUGGEST_ENTRY`, `SUGGEST_EXIT`, `HOLD`, `NO_TRADE` — never "buy"/"sell" phrased as
an executed action.

### 1.2 NO LOOK-AHEAD BIAS

Every feature must be computable using **only data available at or before the
prediction timestamp**.

Mandatory rule: **any function that touches time-series data must carry a docstring
stating its exact data-availability boundary.** Use this block verbatim:

```python
def realised_vol(bars: pd.DataFrame, asof: datetime) -> float:
    """Realised volatility from completed bars only.

    Data-availability boundary:
        Uses bars with bar_close_time <= asof. The bar whose interval CONTAINS
        asof is excluded because it is still forming. Requires >= 20 completed
        bars; returns NaN otherwise.
    """
```

Concrete traps in this domain, all of which have bitten real systems:

- **The forming bar.** A 5-minute bar stamped 09:15 is not complete until 09:20. At
  09:17 you may not use its high, low, or close. Only its open is known.
- **Bar timestamp convention.** Fyers historical candles are stamped at bar *open*.
  Never compare `asof >= bar_timestamp`; compare `asof >= bar_timestamp + interval`.
- **Daily bhavcopy** for day *D* publishes after market close on D. It is available
  for decisions on D+1, never intraday on D.
- **Participant-wise OI** publishes with a lag (typically next day). Same rule.
- **F&O ban list** for day D is published the evening of D-1. Use the D-1 publication
  when deciding on D; never a same-day revision.
- **Corporate actions / adjusted prices.** Historical series adjusted with today's
  knowledge leak the future into the past. Store raw and adjustment factors
  separately; apply adjustments as-of the decision date.
- **Survivorship.** The F&O universe changes. Backtesting on today's universe over
  last year's data is look-ahead. Store the universe as-of each date in
  `data/reference/`.
- **`rolling().shift()` errors.** Any centred window, `bfill()`, or
  `interpolate(limit_direction="both")` on a time series is look-ahead by default.
  Forward-fill only. Never backfill a feature.
- **Scaler / normaliser fitting.** Fit on the training window only, never the full
  series, or the test period's distribution leaks in.

Every backtest and validation run is **walk-forward**. No random train/test split on
time-ordered data, ever.

### 1.3 Timestamps are IST and timezone-aware

- Every datetime in this system is timezone-aware in `Asia/Kolkata`.
- **Naive datetimes are a bug.** Functions that accept a datetime must reject naive
  input by raising, not by silently localising it. `src/market/calendar.py` provides
  `ensure_ist()` — use it at every boundary.
- Use **`zoneinfo.ZoneInfo("Asia/Kolkata")`** from the stdlib. `IST` is exported from
  `src/market/calendar.py`; import it, don't re-create it.
- `pytz` is installed only because pandas/`fyers_apiv3` code paths touch it. Do not
  use it in our code. If you ever must, remember `pytz` requires
  `tz.localize(dt)`; attaching a `pytz` zone via `tzinfo=` yields the 1947 LMT offset
  `+05:53`, not `+05:30`. This has silently broken real systems.
- IST has no DST, but do not hardcode `+05:30` as a fixed offset — go through the zone.
- **`tzdata` is a hard dependency on Windows.** Windows ships no IANA tz database, so
  `ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` without it. It is pinned in
  `requirements.txt`; `src/market/calendar.py` re-raises with an actionable message.
  Do not "clean up" that dependency as unused — nothing imports it by name.
- Persist to Parquet as **tz-aware UTC**, convert to IST on read. Parquet round-trips
  UTC reliably; local-zone round-tripping is implementation-dependent.
- Never use `datetime.now()`. Use `now_ist()` from the calendar module, so time can be
  injected/frozen in tests.

### 1.4 Costs are always modelled

No P&L number is ever reported gross-only. **Every gross figure must be printed,
returned, and rendered with its net counterpart beside it.**

Cost components, all mandatory:

| Component | Notes |
|---|---|
| Brokerage | per-order, per-leg, subject to a cap |
| STT / CTT | equity intraday: sell side only; F&O: differs for futures vs options, and options are charged on **premium**, not notional |
| Exchange transaction charge | NSE slab, differs equity vs futures vs options |
| SEBI turnover fee | |
| GST | 18% on (brokerage + exchange charge + SEBI fee) |
| Stamp duty | buy side only, state-dependent |
| **Slippage** | modelled explicitly, never assumed zero |

Rules:
- Return types carry both: `{"gross_pnl": ..., "costs": {...}, "net_pnl": ...}`.
  Never a bare float.
- Slippage is a **modelled assumption**, not a constant to forget. Default to a
  conservative spread-based estimate and record which model was used in the journal.
- Round-trip costs are charged on **both** legs. A common bug is charging entry only.
- **Any edge claim must be net of costs.** "This strategy is profitable" with gross
  numbers is a false statement in this repo.
- Cost rates live in `config/settings.py` and are marked `TODO: verify`. They must be
  reconciled against the user's actual Fyers contract notes before any result is
  trusted. Do not present cost-dependent conclusions as settled until that is done.

### 1.5 NO_TRADE is a first-class output

The system must be able to say **nothing is worth trading today**.

- `NO_TRADE` / `NO_SIGNAL` is a valid, expected, frequently-correct output.
- Module B surfaces **UP TO** 2 candidates. Zero is a normal result. One is a normal
  result.
- **Never lower a threshold, relax a filter, or rank-fill to produce a signal.** There
  is no quota. A day with no qualifying setup returns an empty list.
- Never write code shaped like `if not candidates: candidates = top_n(universe, 1)`.
- If a filter is rejecting everything for weeks, that is a finding to report to the
  user, not a bug to tune away.

---

## 2. BUILD ORDER — deliberate, do not reorder

```
C. Position Tracker   <-- FIRST. Rule-based, zero ML, useful on day one.
A. Morning Setup
B. Signal Engine
D. Journal
(ML)                  <-- much later, conditional. See 2.1.
```

| Module | Dir | Status | Purpose |
|---|---|---|---|
| A. Morning Setup | `src/morning/` | **BUILT** (2026-08-27) | capital, daily target %, max loss %, feasibility check |
| B. Signal Engine | `src/signals/` | **BUILT — loses money** | ranks the F&O universe, surfaces **up to 2** candidates |
| C. Position Tracker | `src/positions/` | **BUILT** | user enters manual buys; system advises when to exit |
| D. Journal | `src/journal/` | **PARTIAL** | records manual taken/skipped notes, closed fills, check outcomes, and bar-scoped exit advice; automatic opportunity/outcome linkage remains planned |

Module C also ships `src/costs.py` (intraday equity cost model) and `src/cli.py`.
The dashboard journal currently combines manual decision notes with Module C fills,
check outcomes, and exit-advice snapshots. It does not automatically link a skipped
decision to an outcome or persist scanner observations as opportunities. Cost figures
remain provisional until reconciled against contract notes.

**Module A as built (2026-08-27).** `src/morning/setup.py`, 66 module tests + 41 CLI
tests. Available as `python -m src.cli morning --capital ... --target-pct ...
--max-loss-pct ...`, with `--date` (plan another session), `--at` (fix the clock for a
deterministic answer) and `--json`. `--capital` is **required and never defaulted**;
the two percentages fall back to config. **Not wired to the dashboard or API.**

`cmd_morning` is a THIN PRESENTER: every rule stays in the module, and
`test_the_command_adds_no_business_logic_of_its_own` fails if it starts recomputing
`round_trip_costs`, `is_trading_day`, `holiday_name`, `square_off_time` or
`rates_verified`, or constructs a `Feasibility`/`SessionState` itself.

**Exit codes follow the existing convention and invent nothing.** A computed answer is
**0 whatever the advice** — `NO_TRADE` is a first-class result, not an error, exactly as
`cmd_session` returns 0 on a non-trading day. A missing or malformed argument exits
through argparse (**2**); an unexpected exception through `main` (**1**).
`check-entry` remains the ONLY verb using exit 2 as a documented shell guard, and a
test asserts there is exactly one such guard in the whole CLI.

Pure arithmetic over the NSE calendar and the shared cost model —
its entire import surface is `dataclasses`, `datetime`, `decimal`, `enum`,
`config.settings`, `src.costs`, `src.market.calendar`, and a test pins that set exactly.
No market data, no indicator, no model, no broker token, no symbol, no ranking.

`Feasibility.POSSIBLE` means **only that the arithmetic does not reject the plan** — not
a probability and not a forecast. `INFEASIBLE` is reserved for the one genuinely
arithmetic case: a round trip costs more than the entire daily loss budget, so a single
trade exited at break-even gross would breach the limit. A shut market yields
`NOT_APPLICABLE` rather than `POSSIBLE`, because `{advice: NO_TRADE, feasibility:
POSSIBLE}` on a Saturday reads as "the plan is fine" — the cost arithmetic still comes
back in `cost_pct_of_capital` and `required_gross_pct`, so refusing the word costs
nothing.

**Cost rates remain unverified, so `READY` is unreachable** until
`COST_RATES_VERIFIED=true`: unverified rates always raise a warning and warnings cap the
advice at `LIMITED`. That is deliberate — the module must not describe an unreliable
feasibility result as reliable. **It predicts no returns and identifies no profitable
trades.**

Rationale for C-first: it is deterministic, needs no model, no training data, and no
historical corpus. It delivers value on day one and it forces the market/session/cost
plumbing to be correct before anything statistical is layered on. Do not propose
starting with the signal engine because it is "the interesting part".

### 2.1 The ML gate

ML is introduced **only** if it beats a **logistic-regression baseline, after costs,
on walk-forward validation**.

- Baseline first, always. Ship the baseline before proposing anything else.
- "Beats" means net-of-costs, out-of-sample, walk-forward — not in-sample accuracy,
  not AUC on a random split.
- If the baseline wins, the baseline ships. That is a success, not a failure.
- Do not add a model dependency (torch, lightgbm, sklearn beyond the baseline) to
  `requirements.txt` before that gate is passed.

---

## 2.2 Module C as built — decisions worth not re-litigating

**Files.** `models.py` (Position, PositionRuntime, Bar, ExitAdvice, PositionState),
`indicators.py` (VWAP/ATR/EMA/RSI/volume), `exit_engine.py` (the state machine),
`store.py` (Parquet+DuckDB), `feed.py` (ReplayFeed, MultiSymbolReplayFeed,
BarAggregator), `live_feed.py` (FyersLiveFeed), `replay.py` (session driver).
Plus `src/costs.py`, `src/cli.py`, `src/api/` (runtime + FastAPI) and `web/index.html`.

**State precedence.** Exactly one state per evaluation, resolved in this order:
`FORCE_EXIT → STALE_DATA → INVALID_ENTRY → EXIT_NOW → TIME_EXIT → PARTIAL_EXIT →
TRAIL → HOLD`. Mirrored by `PRECEDENCE` and asserted in tests. Do not reorder casually.

**INVALID_ENTRY is a diagnostic about the past, not an instruction about now.** It
exists because the momentum analysis found 48% of positions exiting on the first
completed bar — the exit condition was already true before the position existed. The
engine was right; it was saying it in the wrong words. "Momentum reversal" describes
the market. "You entered long while EMA9 was already below EMA21 and RSI was 43"
describes the mistake.

- Fires **once**, only when the evaluation is AT the first completed bar after
  `entry_time`. `PositionRuntime.entry_checked` is the one-shot latch and
  `entry_verdict` keeps the finding after the bar scrolls away.
- Three triggers, each independently tested: momentum already reversed
  (`EMA9 < EMA21 AND RSI14 < 50`), price already below session VWAP, or price/entry
  already at or through the stop.
- The VWAP test has **no volume gate**, unlike the EXIT_NOW rule. Confirmation matters
  when abandoning a position you hold; for one you have not justified yet, being on
  the wrong side of VWAP at all is the finding.
- It outranks EXIT_NOW **on that one bar only**, because both carry the same
  information there and only one of them is useful. It **suppresses nothing**:
  `suggests_exit` is False, `suggested_exit_quantity` is 0, and normal advice resumes
  on the very next bar. Verified on real bars — ONGC 2026-02-13 goes
  `INVALID_ENTRY → PARTIAL_EXIT → TRAIL` as the trade recovers.
- FORCE_EXIT and STALE_DATA still outrank it. A stale feed leaves the check *pending*
  rather than consuming it.
- A late start (restart, or a replay joining mid-session) records the verdict on the
  runtime but does **not** emit the state — old news must not be announced as current.

**`check-entry` asks before buying and creates no position.** `check_hypothetical_entry()`
runs the same three predicates against the last completed bar. Exit code 0 = sound,
2 = unsound, so it works as a shell guard. It creates no position and no runtime state.

> **This used to say "records nothing", and that stopped being true on 2026-08-24.**
> The dashboard's `check_entry` now appends one line to the check log (§2.4) — an
> observation, not a decision. **The CLI verb still writes nothing at all**: it has no
> feed and no runtime, so it could never settle an outcome, and logging checks that can
> never have one would fill the scoreboard with dead rows. The claim that has not
> changed, anywhere, is the one that matters: no position, no order.

**FORCE_EXIT outranks STALE_DATA.** Square-off is a clock fact, needs no price, so a
dead feed cannot make it wrong — and suppressing it would go silent exactly when it
matters. The attached P&L is labelled stale rather than presented as current.
Everything *below* FORCE_EXIT is price-based and IS suppressed by a stale feed.

**Target 1 defaults to `entry + 1.5 × ATR`** when no explicit target was recorded, so
it scales to each symbol's own volatility. An explicit target always wins.

**TIME_EXIT's ±0.3% band is measured on GROSS P&L.** The question it asks is "has the
price stopped moving", which is about price. Costs are a constant drag that would bias
every position toward looking dead and fire the rule early. Both figures are still
reported. This is the one place gross is used for a decision, and it is deliberate.

**Long only.** `Side` has one member. The exit logic is long-specific throughout
(trail *below* the running high, exit on losing VWAP *from above*); a short would be
evaluated with inverted rules and silently wrong advice, so it is rejected at
validation. Adding shorts means a second rule set, not a sign flip.

**The engine reads no clock and no socket.** `evaluate()` takes `now` and
`last_tick_at` as arguments. That is what makes replay reproducible and what a test
asserts by poisoning `now_ist`. Keep it that way — any `datetime.now()` inside the
engine breaks replay silently.

**Runtime state is persisted per evaluation.** The trailing stop is a ratchet built up
over a session; losing it to a restart hands back risk the position had already locked
in. `store.load_or_init_runtime()` is the resume path.

**Exit rules carry a machine-readable tag.** `ExitAdvice.rule` is an `ExitRule` enum.
Analysis keys off it. Never grep the prose `reason` string to classify an exit — it
gets reworded, and the aggregate silently changes.

**`momentum_confirmation_bars`** requires the reversal to hold N consecutive completed
bars before EXIT_NOW fires. `1` is the original single-bar rule. **Do not change this
on intuition** — change it on a swept result over real bars.

**The momentum question is ANSWERED — see `reports/momentum_analysis.md` and §8.**
Short version: the rule is not cutting winners, but 48% of positions exit on the
FIRST bar after entry, so on a fixed-clock entry the exit rules never let a position
develop. Do not re-litigate this from intuition; that holdout is spent.

That finding is what `INVALID_ENTRY` now reports in the right words — see §2.2.

**`synthetic_session()` is a fixture, not market data.** It exists so replay can be
demonstrated before real bars exist. Never evaluate a rule against it.

---

## 2.2a The live feed — connection path verified against the exchange, TICK PATH NOT

`src/positions/live_feed.py`. 50 tests. **Every network dependency is injected**:
`HistorySource` and `SocketLike` are Protocols, and the constructor takes instances.
That is the whole reason it has coverage at all — the tests drive a fake socket, a
fake history client and an explicit clock. Do not reach for `fyersModel` inside a
method; take it as a parameter.

### What the exchange has and has not confirmed

First live run: **2026-08-22, a Saturday — the market was shut.** That is what makes
the record below lopsided, and the lopsidedness is the point. Do not compress this
into "the live feed has been tested against the real exchange": that sentence reads as
covering the tick path, and **the tick path is exactly what has not been tested.**

| VERIFIED against Fyers (2026-08-22) | STILL UNVERIFIED — fakes only |
|---|---|
| Websocket connects to the real endpoint | **Any real tick has ever been parsed.** Zero price ticks arrived; `parse_tick` has still only ever seen injected data |
| Authentication succeeds | Bar aggregation from live ticks |
| Subscription accepted for all 6 symbols | Staleness detection against a real feed going quiet |
| Control frames arrive in the expected shapes (`cn` / `ful` / `sub`) | `reconnect()` and `backfill_gap()` against a real disconnection |
| `seed_history()` classifies a closed market correctly | `seed_history()` with actual bars present |

**A Monday session moves a row; it never assumes one.** Each right-hand row is claimed
only once a live session has actually exercised it, with the date recorded the same
way. Half of a run does not license the other half — Saturday demonstrated that the
socket opens and that seeding is honest when there is nothing to seed, and nothing
whatsoever about what happens when data starts flowing.

Passing tests mean the reconnect logic, the gap arithmetic and the tick parsing behave
as specified **against fakes**. They do not mean Fyers sends what the fixtures send.
For anything in the right-hand column, say "tested, unverified live", not "working".

### What the first run cost, and what it changed

Two defects, both of the shape where a real failure is indistinguishable from a
normal one:

- **Protocol frames were parsed as ticks.** `cn`, `ful` and `sub` acknowledgements
  share the `on_message` callback with quotes, carry no `ltp`, and were logged as
  `unparseable tick` — three warnings per connection, which is the noise a genuine
  parse failure hides in. They are now dropped **by type** before parsing is attempted
  (`CONTROL_FRAME_TYPES`, `is_control_frame`) and logged at debug. Two rules keep that
  from becoming "ignore anything that fails to parse": an *unrecognised* type still
  falls through to the parser and still warns, and a control frame whose `s` is not
  `ok` is an **error** — a rejected subscription is silence on that symbol, not noise.
- **`Fyers history failed (code 200)` for all 6 symbols, with an empty message.**
  200 is a SUCCESS code. Fyers answers an empty range with `s: "no_data", code: 200`,
  and `FyersHistoryClient.fetch` raised on any `s != "ok"` — inventing an outage out
  of a correct answer. It now returns `[]` for `no_data`/code 200 and logs the request
  payload plus the response; a genuine failure raises with the status, the request and
  the trimmed response in the message.

**Zero bars has three causes and only one of them is normal**, so `seed_history`
names which one via `SeedOutcome`, using `expected_completed_minutes()` — fully
elapsed minutes between 09:15 and `min(now, 15:30)`, zero on a non-trading day — as
the discriminator:

| outcome | condition | treatment |
|---|---|---|
| `API_ERROR` | the call raised | error log, `status.last_error` set |
| `MARKET_CLOSED` | 0 expected minutes | info log, **`last_error` deliberately NOT set** |
| `NO_DATA_IN_SESSION` | ≥1 expected minute, 0 bars returned | error log, `status.last_error` set |

`MARKET_CLOSED` must not raise the header alarm, or the alarm stops meaning anything —
this is the `● LIVE`-with-no-data lesson from §2.4 in the other direction. Collapsing
the three is how a Monday-morning seed failure reads as "market closed" and the
dashboard starts a live session with cold indicators. Every symbol logs `symbol=`,
`range=`, `asof=`, `expected_minutes=` and `candles=`; the full `SeedReport` hangs off
`feed.last_seed`, and `seed_outcome` and `control_frames` ride in
`FeedStatus.as_dict()` so both reach `/health`.

The boundary is one bar, and it is pinned: 09:15:59 is `MARKET_CLOSED`, 09:16:00 is
`NO_DATA_IN_SESSION`.

### Design points

- **`seed_history()` runs before the socket opens.** Starting flat means ATR, EMA21
  and VWAP are undefined for the first ~20 minutes, and an engine with no ATR either
  refuses to advise or advises on noise. The feed backfills the session-so-far from
  the history endpoint first, then subscribes.
- **`backfill_gap()` is the reconnect path, and it is not optional.** A socket drop
  loses ticks silently — the next tick simply arrives with a later timestamp and the
  aggregator would happily close a bar built from a fraction of its minute. After any
  reconnect, completed minutes are refetched from history and `BarAggregator.merge()`
  reconciles them. History wins over the partial local bar.
- **`reconnect()` retries with exponential backoff, then backfills.** It returns
  False rather than raising when it gives up, because a dead feed is a *state the UI
  must show*, not an exception that kills the process. `FeedStatus.is_stale` is what
  drives STALE_DATA downstream.
- **Staleness is measured per symbol.** A thin symbol that has not ticked for 30s is
  stale even while the socket is healthy and other symbols are streaming. Reporting
  one global "connected" flag would hide exactly the case that matters.
- **`close_due_bars(now)` takes the clock as an argument**, same rule as the engine.
  A bar closes because the minute elapsed, not because a tick happened to arrive —
  otherwise a symbol stops producing bars at the moment it goes quiet, which is when
  you most need to know.

## 2.4 The dashboard — position desk, descriptive market view, no scored signal panel

`src/api/runtime.py` (mode-agnostic session state), `src/api/app.py` (FastAPI),
`src/api/serve.py` (the one place the wiring lives), `web/index.html` (React 18 UMD +
Tailwind CDN, no build step — there is no npm on this machine). 37 API tests. Binds
127.0.0.1, with no auth on the page.

**Started as `python -m src.cli dashboard`**, matching every other verb.
`scripts/serve_dashboard.py` is a thin wrapper kept for existing habits; both call
`src.api.serve.serve()`. Do not let the two grow separate copies of the wiring — that
is how one of them silently starts LIVE when REPLAY was asked for, and a test
(`test_both_front_doors_share_one_implementation`) pins it.

**Mode, date and speed are CLI flags only.** Nothing reads them from the environment
or from config, and a test asserts that setting `MODE`/`DASHBOARD_MODE`/`FNO_MODE`
does not change the default. An env var that can flip a dashboard to LIVE is a way to
be looking at real money while believing you are in replay.

### What limits `--symbols`: the startup seed, and nothing else (measured 2026-08-24)

**The full ~180-name F&O universe fits.** The guard is `MAX_SYMBOLS = 250`. Measured,
not assumed — three of the four plausible constraints turn out not to bind at all:

| candidate | measurement | binds? |
|---|---|---|
| Broker socket cap | **5000**. `fyers_apiv3`'s `data_ws` sets `symbol_limit = 5000`, rejects more with `LIMIT_EXCEED_MSG_5000`, and chunks the subscribe into 500s itself. 180 is 3.6% of it | no |
| Evaluation per bar | the per-tick loop iterates **open positions, not symbols** — adding symbols costs it nothing. The symbol-scaling path is `/check-entry-all` at **0.25 ms/symbol** over a full 375-bar session = **46 ms for 180**, and only when the button is pressed. Event risk is cached on the ledger's stat stamp | no |
| Memory | **465 kB per symbol-session** of completed bars → **84 MB** for 180 × 375 bars | no |
| **Startup history seed** | **one REST call per symbol** before the socket opens. Of `RateLimiter`'s two caps the per-MINUTE one binds above a single symbol (100/min = 0.6 s apart, vs 3/s = 0.33 s), so **~0.6 s per symbol: ~107 s for 180**, paid on every launch before the page renders | **YES** |

So the price of the whole universe is about two minutes of startup, and that is the
only thing being traded away.

**`seed_history()` and `backfill_gap()` were unpaced until 2026-08-24** — they would
have put 180 requests on the wire in a burst. Both now go through `RateLimiter` via
`FyersLiveFeed._pace()`, using the injected `sleep` so tests pay no wall time. Do not
remove the pacing to make startup faster: being throttled at 09:14 costs the session.

`seed_seconds_estimate()` exists so a two-minute silent startup is announced rather
than looking like a hang, and `test_the_seed_estimate_matches_the_pacing_that_produces_it`
asserts it against `RateLimiter`'s actual defaults — a stale estimate is a promise the
code breaks.

### MEASURED: the entry check describes the present and predicts nothing (2026-08-24)

Full report: `reports/entry_check_diagnostic.md`, data `reports/entry_check_diagnostic.json`.
**DEV only** — `PANEL_FULL_HISTORY` 2020-01-01..2024-11-26, 1212 sessions, 21 symbols,
four slots (09:45 / 11:00 / 13:00 / 14:30), **n = 96,720 evaluations**, three regimes.
**The six-year holdout was NOT touched and NOT claimed** — a diagnostic is not an
evaluation of a change, and spending the only out-of-sample data on one is waste.

**The verdict does not separate forward outcomes to any degree worth acting on.**
SOUND minus UNSOUND on the forward 60-minute return, cross-sectionally demeaned:
**+0.0287pp, 95% CI [−0.0067, +0.0641]** on **3,626 independent session-slot cells**,
t = +1.59. The interval includes zero.

**Note the shape: this is a positive point estimate that does not clear the bar, not a
flat zero.** t = 1.59 with an MDE of 0.024pp against a measured 0.029pp means the sample
was powered for an effect this size and still could not resolve it from zero. That is
the case where it is tempting to say "nearly significant" and go looking for a bigger
sample — and the magnitude is why that would be wasted effort:

**A round trip costs 0.183% of notional. The entire measured gap is 16% of one round
trip** — taken at face value and assumed to survive out of sample, it would still be
**6.4x too small to pay for the trade it is advising on.** Confirming this effect would
change nothing about what to do. All four slots share the sign (+0.032 / +0.022 /
+0.033 / +0.025) and not one clears zero; the shared sessions mean that agreement is not
independent evidence.

**It is NOT firing on noise, and that is the interesting part.** The median
UNSOUND-by-VWAP verdict sits **0.300% below VWAP ≈ 3.0 one-minute ATRs**, and only
18.4% are inside a single one-minute bar range. The check is describing a real
displacement accurately. The displacement just does not predict the next hour.

| | |
|---|---|
| SOUND overall | **37.2%** — less often than it feels; 46.1% at 09:45, 33.4% at 14:30 |
| BELOW_VWAP | present in 85.6% of UNSOUND, sole cause in 28.9% |
| MOMENTUM_REVERSED | present in 71.1%, sole cause in 14.5% |
| both together | 56.7% |
| STOP_ALREADY_HIT | **0, and structurally so — see below** |
| median distance below VWAP | 0.300% / 3.0 one-minute ATR (p75 0.553% / 5.6) |
| within 0.25% of VWAP | 42.7% |

**STOP_ALREADY_HIT can never fire from the panel, and that is a design fact, not a
sample.** The panel sends no stop. Even with one, `check_hypothetical_entry` defaults
the entry price to the CURRENT price, so the predicate can only trip if the user types a
stop at or above where the stock already trades — a property of their typing, not of the
market. **The check is two predicates, not three.** `test_the_stop_predicate_cannot_fire_without_a_stop` pins it.

**What this licenses: wording, not thresholds.** A UNSOUND badge must not be read as
"this will fall", and a SOUND badge emphatically not as "this will rise" — the panel's
existing disclaimer ("SOUND means only that this entry is not obviously unsound right
now") is now a *measured* statement rather than a cautious one. **Do not tune
`rsi_reversal_level` or add a VWAP band in response to these numbers** — that is fitting
to DEV, and the honest route is one change, re-measure on DEV, holdout once at the end.

**Machinery notes that outlive this run.** The predicates now carry `EntryProblem` tags
and `entry_findings()` is their single implementation, with `evaluate_entry_conditions`
projecting prose out of it — analysis keys off tags, never the reason string (§2.2's rule,
now applied to entries). The forward window is capped at the 15:15 square-off, so the
14:30 slot is reported at 45 minutes and never pooled with the 60-minute slots. Slots are
spaced wider than the forward window so one symbol's observations do not overlap each
other, and intervals use the session-slot cell count rather than the 96,720 observations
— 21 symbols sampled in one minute are one draw with 21 readings, the same correction
the swing experiment needed.

**The demeaning is not optional.** `test_the_raw_figure_can_show_a_gap_where_the_demeaned_one_does_not`
builds a case where SOUND fires on an up day and UNSOUND on a down day, every symbol
within a day moving identically: the raw comparison reports a 2pp edge and the demeaned
one reports exactly zero. Here the two happen to agree (raw +0.0346), but only because
the verdict is roughly as likely to fire on up days as down.

### TOKEN_INVALID suppresses advice at the RUNTIME boundary, not in the engine (2026-08-27)

An expired token used to be *reported* and never *enforced*: `require_token()` ran once
in `build_live_runtime()`, after which `_token_status()` was display-only. A token dying
mid-session left advice flowing until the socket dropped and staleness fired 30 s later —
failing closed indirectly, through a different mechanism than the one that knew the truth.

**The exit engine is unchanged and stays credential-agnostic.** `PositionState` has no
`TOKEN_INVALID`, the eight-state precedence chain is untouched, and
`test_the_exit_engine_never_learns_what_a_token_is` parses the engine with `ast`,
**strips every docstring**, and asserts the remaining CODE never says token / credential /
fyers_auth / access_token / secret. (Scoped to code because the engine's docstrings
legitimately explain why `square_off_reason` is safe under suppression — the same trap
the bulk-table "not a ranking" scan hit.)

**Suppression lives in `SessionRuntime.evaluate_all()`,** which is the smallest boundary
that knows about credentials at all:

- `feed_health() -> FeedHealth` is the typed infrastructure result:
  `applicable`, `token_valid`, `advice_suppressed`, `reason` (`TOKEN_INVALID` or None),
  `detail`, `checked_at`. Embedded in `/health` as `feed_health`.
- When suppressed, **`evaluate()` is never called** — it is pure and would happily
  produce price-based advice from bars we can no longer verify — and
  **`save_runtime()` is never called**, so the trail, the breakeven flag and
  `last_state` stay exactly as the last authorised evaluation left them.

**FORCE_EXIT still fires.** `square_off_reason()` was made public (it was already
clock-only, price-free and credential-free) and is consulted on the suppressed path too.
A lapsed token cannot make a clock wrong, and going silent at 15:15 about a position that
is really open is the worst available outcome. Its P&L is computed from the last
authorised bar with `price_is_stale=True`, or is absent — never presented as live.

**TOKEN_INVALID is NOT STALE_DATA.** Different causes, different fixes; collapsing them
would send the user to restart a feed that is working.
`test_token_invalid_is_not_reported_as_stale_data` asserts the feed reports
`stale: False` while the token gate suppresses.

**The token is re-read before EVERY evaluation, deliberately not cached.** My first cut
cached the bundle and revalidated it against the clock — and two tests caught the hole: a
remembered bundle stays valid for its whole IST day, so a token deleted, replaced or
revoked out of band goes unnoticed until midnight *while the dashboard keeps advising*.
`test_the_token_is_reread_before_every_evaluation_not_remembered` asserts 5 reads for 5
evaluations, and `test_a_token_removed_out_of_band_suppresses_immediately` covers the
case caching hid. This costs one small JSON read per tick — which the previous
display-only check already paid, so it is not a regression. If it ever matters, the fix
is the stat-stamp pattern in `StoreEventRiskSource._load`, **not** a time-based cache.

**Recovery needs no restart:** run `python -m src.cli auth` in another terminal and the
next evaluation picks the new token up.

**`FeedHealth.detail` is redacted before it leaves the process.** It is built from an
auth exception message and then rendered into `/health`, onto the dashboard banner, AND
into a log line — three places a live credential must never reach. No auth error in
`src/data/fyers_auth.py` embeds a token today (`TokenBundle.__repr__` is redacted and
every message carries paths, dates or field names), so `_redact()` is defence in depth
rather than a fix. It is there because that string crosses a process boundary and a
future auth change would not obviously be reviewed for it.

**REPLAY never reads a token** — `feed_health()` short-circuits before any credential
code is imported, and a test injects an exploding loader into a replay runtime to prove
the path is never taken.

**On screen:** a loud, **static** `ACCESS TOKEN INVALID — PRICE-BASED ADVICE IS
SUPPRESSED` banner (asserted `animationName: none`; motion stays reserved for STALE_DATA
and FORCE_EXIT), a per-card `NOT CURRENT` strip naming the last authorised state and when
it was reached, and `price_is_current: false` on every suppressed card so no price can
render as live. Gross and net still appear side by side.

### The "checked today" scoreboard — a record of the PAST (2026-08-24)

`src/positions/check_log.py`, `GET /checks`, `CheckedLogPanel`. Every entry-check
evaluation is appended to `data/checks/entry_checks.jsonl` with the verdict, the reason
and the price at the check; once the horizon passes the outcome is SETTLED against the
price then and frozen. Persists across sessions, so the sample accumulates.

**It answers one question: does SOUND/UNSOUND relate to what happened next?** The offline
answer is already known and negative (§2.4 above, n=96,720). This is the user's own
accumulating sample of the same question, and the panel names the offline result rather
than letting them wait to rediscover it.

**This is the most dangerous surface on the page.** A table of symbols with a return
column beside each one IS a buy list unless something structural stops it, and that is
exactly the ranking Module B failed at every horizon. The guards are structural:

- **Ordered by check time, newest first, with no parameter to change it.**
  `test_there_is_no_way_to_sort_or_filter_the_log_from_the_api` calls `/checks` with
  `?sort=change`, `?order_by=return`, `?verdict=SOUND` and more, and asserts the row
  order and content are byte-identical every time.
- **No client-side sort or filter either.** `test_the_checked_log_panel_does_not_sort_or_filter_client_side`
  forbids `.sort(`, `.filter(` and an `onClick` in `<thead>`; a browser test asserts the
  rendered rows are newest-first and that there is no `<select>` and no clickable header.
- **The change column is one NEUTRAL colour for every row.** Green gains and red losses
  rank a table by eye exactly as effectively as a sort does, and the sign is already in
  the number. A browser test asserts every change cell computes to the same colour.
- **Both verdicts always, together.** No filter exists, and a test asserts both SOUND and
  UNSOUND are on screen.
- **Named `change_since_you_checked_pct`** — past tense, and a test rejects any key
  containing expected/forecast/projected.
- **No aggregate "top performers".** The only aggregate is the SOUND-vs-UNSOUND test,
  which is a test OF the check, not a league table of symbols. A recursive banned-key
  scan covers the whole response.

**Only a real verdict is logged.** NOT_SUBSCRIBED, NO_BAR_YET, STALE_FEED and
MARKET_CLOSED evaluated nothing, have no price to measure from, and could never settle.

### DECIDED: no entry verdict on a stale feed, or a closed market (2026-08-24)

A check run at 18:18 against a feed 1h 33m old produced a verdict and logged it. It could
never settle — its horizon was already past with no future bars to reach.

**The check now REFUSES rather than answering, and refusals are not logged.** Two
conditions, checked before anything is evaluated:

| reason | condition | why |
|---|---|---|
| `MARKET_CLOSED` | session state is CLOSED or PRE-OPEN | a clock fact needing no threshold, and the case that actually bit |
| `STALE_FEED` | completed bars have stopped arriving | the data is dead |

**Why refuse instead of logging it as STALE_AT_CHECK.** All three entry predicates —
VWAP, EMA/RSI, stop — are price-based and read the *same bars the exit rules read*.
Exit advice below FORCE_EXIT is already suppressed on stale data; answering the entry
question from those same stale bars while refusing the exit one is incoherent. And the
answer would be worse than incoherent: *"price is already below VWAP"* is present tense
about a price an hour and a half old — the header-says-LIVE-with-no-data failure in a
different place. A row marked STALE_AT_CHECK and excluded from the summary would still
have printed a verdict on screen, and the verdict is the thing that misleads.

Note what does NOT carry over: FORCE_EXIT outranks STALE_DATA because square-off is a
clock fact that needs no price. **The entry check has no such component** — every part of
it is price-based, so there is nothing left to say and full refusal is right.

**"No bar has ever arrived" is not automatically stale.** Before the session's first
minute completes it is normal and resolves by waiting — that is NO_BAR_YET, and saying
STALE_FEED there would be the "waiting will not help" mistake in reverse.
`expected_completed_minutes()` tells the two apart, the same function `seed_history` uses.

#### The entry check asks a DIFFERENT staleness question, and it has to

`feed_staleness()` (tick recency, 30s) is the exit engine's rule and is now the single
definition of it. `bars_have_stopped()` is the entry check's, at one BAR_INTERVAL on top
of that threshold — derived from the bar size, not picked.

**This is not a second opinion, it is a different measurement.** A completed bar appears
once a minute BY CONSTRUCTION, so measuring bar recency against a 30-second tick
threshold reports stale for the second half of every minute whatever the feed is doing.

### `last_tick_at` means ONE thing in both modes (fixed 2026-08-25)

`src/positions/feed.py`, `tests/test_feed_semantics.py` (29 tests).

REPLAY and LIVE disagreed about what `last_tick_at` was: LIVE recorded **tick arrival**,
REPLAY returned **the close of the last completed bar**. Both satisfied the Protocol's
type signature, so nothing failed — but the engine's single staleness threshold was being
applied to two different quantities, and a healthy replayed position flipped
`HOLD → STALE_DATA → HOLD` once a minute. That defeats the point of replay: *"fully
driveable in REPLAY before it ever runs live"* is only true if REPLAY runs the same code
on inputs that MEAN the same thing.

**The agreed meaning, now on `PriceFeed`: the most recent moment this feed HAD DATA for
the symbol.**

Chosen over "close of the last completed bar" because **every consumer reads completed
bars, but what it needs to know is whether the data is alive.** The bar-close reading
answers a question nobody asked and guarantees a false positive for half of every minute.
Live feeds satisfy the meaning by recording tick arrival (unchanged);
`data_available_through()` satisfies it for stored feeds — the cursor when the minute
containing it has a bar, the previous bar's close when it does not.

**The flicker resolved as a consequence. `stale_feed_seconds` is untouched at 30**, and
`test_a_healthy_replayed_position_does_not_flicker_within_a_minute` asserts the threshold
value so a future "fix" by nudging it is visible.

Everything the old definition was there for still works, measured:

| case | behaviour |
|---|---|
| mid-bar, session healthy | age 0 — **was** 0→59s climbing, tripping STALE_DATA at +31s |
| hole in the stored session | age grows from the last bar's close; STALE_DATA at +31s into the hole, recovers when data resumes |
| past the last bar | age grows; STALE_DATA at +31s |
| before the first bar | `None`, as before |

**Only the EXISTENCE of the bar covering the cursor is consulted, never its values** —
and that existence is not look-ahead, it is exactly what a live feed knows at the same
instant, namely that the symbol is printing. `test_only_the_existence_of_the_forming_bar_is_consulted`
poisons the forming bar's OHLCV and asserts the answer does not move.

**No already-reported number changes.** `replay_session` evaluates ONLY at bar close
times, and at those instants the new definition and the old one are identical —
`test_at_bar_close_times_the_new_definition_matches_the_old_one_exactly` checks every
step of a session, and a second test pins the premise that the driver steps that way. So
§8's momentum figures remain reproducible.

**The irreducible residual, stated rather than hidden.** A bar stamped 10:19 says the
symbol printed *somewhere* in 10:19–10:20; it does not say the last print was at 10:19:59.
For a thin symbol whose final print was 10:19:03, LIVE goes stale at 10:19:33 and REPLAY
at 10:20:30. That information is simply not in stored data. It is bounded by ONE bar
interval and makes replay **optimistic** about staleness, never pessimistic.
`test_stored_bars_cannot_say_WHEN_in_a_minute_the_last_print_was` measures the gap and
asserts the bound. Asserting exact equality there would be asserting something false.

**The invariant is stated directly, not inferred.**
`test_replay_and_live_agree_on_staleness_given_equivalent_inputs` drives a real
`FyersLiveFeed` with ticks and a `MultiSymbolReplayFeed` with the bars those ticks would
have aggregated into — both with their last datum at the same instant — and asserts
identical verdicts at ten offsets. A companion test asserts `evaluate()` reaches the same
`PositionState` in both modes.

### A 404 on a live endpoint is a session-identity bug, again

`/checks` returned 404 in the browser while every API test passed. The route was
registered and the frontend path matched: **the server process on :8000 had been started
before the route existed** and was serving the old app to a freshly-loaded page. Same
class as the stale-dashboard incident above, and invisible from the browser.

- `/health` now reports `started_at` (wall clock of the process, never the virtual
  clock), and a 404 from `/checks` says so on the panel by name rather than showing a
  bare status code.
- **`test_every_endpoint_the_frontend_calls_exists_on_the_app`** parses the real
  `index.html` for every `fetch()` and `new WebSocket()` call and asserts each exists on
  the real app. Parsed rather than hand-listed, because a hand-maintained list gets
  forgotten in exactly the commit that adds a panel.
- **`test_the_endpoint_parser_would_notice_a_missing_route`** removes `/checks` from the
  route set and asserts the guard fails. A coverage test that cannot fail is worse than
  none, because it reads as proof.

**A green suite over an absent feature is the blank-page failure mode.** Before drawing
any other conclusion about a dashboard that will not load a panel, check whether the
process serving it predates the code.

**Three outcome states, not two.** `SETTLED` (frozen), `LIVE` (still running), and
`UNRESOLVED` — a past session whose bars we cannot reach. UNRESOLVED exists for the same
reason UNKNOWN does in event risk: leaving a finished check on screen as LIVE shows it as
though it were still in flight. **LIVE and UNRESOLVED rows are excluded from the summary
and counted**, because a number that is still moving is not an outcome.

**The interval is computed on independent MINUTES, not rows, and this is not optional.**
One press of "check all" logs a row per symbol in the same minute; those rows share that
minute's market move and are ONE draw with N readings. Counting them as N would shrink
the interval by ~√N and manufacture a finding out of one quiet afternoon — the same
correction the swing experiment needed (§2.3). `summarise_verdicts` counts distinct
(trade_date, minute) cells and reuses `GroupDiff` from the offline diagnostic, so the
dashboard and the six-year measurement compute the interval the same way.

**INCONCLUSIVE until n ≥ 30 settled checks of EACH verdict**, the same bar the offline
analysis uses — there is no laxer standard for the live dashboard. When the interval
spans zero with adequate power the panel says so in those words: *"on this evidence the
verdict is decoration: it is not telling you what happens next."*

**Settlement is 60 minutes, capped at the 15:15 square-off**, matching the offline
diagnostic so the two answer the same question. It runs inside `checked_log()` as well as
`snapshot()`, so a bare GET settles what is due and the panel is right without the
background loop. **First SETTLE line wins on read** — the ledger is append-only and an
outcome frozen before the answer was known must not be movable afterwards.

### "No verdict" has two causes and only one is fixed by waiting

`check_entry` returned `No completed bar for RISHABH yet` for a symbol that was never
subscribed. There is no feed behind it, so that bar is never coming, and "yet" sent the
user back to refresh a panel that could not change.

- `NOT_SUBSCRIBED` — *"RISHABH is not in the tracked universe, so no bar for it will
  ever arrive. Tracked symbols: … Add it with `--symbols` and relaunch."* It names the
  tracked list rather than implying it, and also returns `tracked_symbols`.
- `NO_BAR_YET` — *"First bar for RELIANCE has not closed yet."* Nothing more; this one
  really does resolve by waiting.

Carried in a `reason` field (`None` when a verdict was reached), and the panel heading
splits too: **NOT TRACKED — WAITING WILL NOT HELP** versus **NO BAR YET**. A single
`CANNOT JUDGE` for both is what made waiting look useful. `check_entry_all` only ever
asks about subscribed symbols, so `NOT_SUBSCRIBED` must never appear in a bulk row —
a test asserts it does not, because if it does the two have drifted apart.

Unrelated but adjacent: `resolve_symbols` now collapses duplicates. Two identical rows
in the bulk table are indistinguishable from one row rendered twice — the same
confusion §2.4's duplicate-position rule exists to prevent.

### Layout: caveats are COLLAPSED, never deleted (2026-08-24)

The page was technically correct and hard to read: the position column was ~70% of the
screen and usually empty, and every panel carried permanent explanatory text about what
it does not do, in ~11px paragraphs, in a 380px rail.

**Standing explanations live behind a per-panel `Why?` disclosure, collapsed by
default.** Native `<details>`, so they stay in the document, keyboard reachable, and
findable by find-in-page when open. `Why` is the one component; `data-testid="why-*"`
addresses each one.

**Collapsed is not deleted, and one test asserts BOTH halves.**
`test_standing_caveats_are_collapsed_but_reachable` checks the phrases are NOT in the
rendered text by default *and* that each is present, unchanged, one click away. Asserting
only reachability would let a future edit quietly drop them; asserting only invisibility
would let a future edit collapse something that must stay visible. Every caveat test now
opens its disclosure via `open_disclosure()` — **a render test that stops finding a
caveat must be fixed by restoring the caveat, never by deleting the assertion.**

**What stayed out of the disclosures, deliberately:**

- **Gross and net.** `test_gross_and_net_are_never_behind_a_toggle` asserts both are in
  the rendered text with no interaction AND that neither appears inside any `<details>`.
- **The advisory-only claim** ("never places, modifies or cancels an order"). Only the
  elaboration and the no-signal-panel explanation are behind `why-advisory`.
- **STALE_DATA, FORCE_EXIT, UNKNOWN mode, the fetch error, and the event-risk state
  chip.** Anything that says the page cannot be trusted stays on the surface.

**The empty state is the bulk check, not an empty box.** With no open positions the wide
column carries `CheckAllPanel` (screening before buying is what that space is for); with
positions it carries the cards and the bulk check sits below them, still in the wide
column where a four-column table is actually readable. Both are pinned —
`test_the_empty_state_offers_the_bulk_check_not_an_empty_box` asserts the button is laid
out >500px wide, and `test_positions_take_the_wide_column_when_there_are_any` asserts the
panel does not vanish once a position exists.

**Feed and token get their own outlined group at a larger size** — they are the two
things that break this tool silently, an expired token stopping the feed with no error on
screen and a quiet socket looking exactly like a quiet market. **They are NOT animated.**
`test_the_two_silent_failure_readouts_are_prominent` asserts `animationName` is `none`:
motion means STALE_DATA or FORCE_EXIT and nothing else, and diluting that is how the
alarm stops meaning anything. An invalid token in LIVE does get a loud static strip.

**Base type is 17px with 1.6 leading**, and `test_the_right_rail_is_not_set_in_tiny_type`
fails on any visible `p`/`td`/`li`/`summary` below 14px. Assume a second monitor at arm's
length.

**Result cards lead with the verdict and ONE line.** The card printed the same VWAP
sentence twice — once inside `summary`, which concatenates every problem, and again in
the bulleted list under it. The engine's problem strings are `"<fact> -- <why it
matters>"`; `leadOf()` takes the fact half, which is specific to this symbol and minute,
and the boilerplate half plus the full summary and every bullet go in the disclosure.
**Both halves still reach the screen in full** — `test_the_entry_result_leads_with_a_verdict_and_one_reason`
asserts the VWAP sentence appears exactly once in the visible card and that the full text
is present after opening the toggle.

**A note on the event-risk panel's settle race.** Its no-payload fallback is *also*
labelled `UNAVAILABLE` — correctly, since "nothing has arrived" is not CLEAR — so a test
that waits on the state chip alone reads the placeholder and then races the first push.
Wait for the note text to stop being the placeholder. Same trap as the mode banner's
`NO DATA — MODE UNKNOWN`, and it was latent in that test before this redesign.

### `/check-entry-all` is a FILTER, and the API shape is what keeps it one

`GET /check-entry-all` runs the same entry check across every subscribed symbol and
returns `{symbol, valid, summary}` rows. It exists because deciding what to buy across
the universe one POST at a time does not work.

It is one edit away from being the signal panel that failed evaluation, so the
guardrails are structural rather than cultural:

- **`SessionRuntime.check_entry_all()` calls `self.check_entry()` per symbol.** One
  implementation, one set of predicates. `test_check_entry_all_rows_match_single_check_entry_exactly`
  compares every row against the single-symbol endpoint, and a structural test asserts
  the delegation is still in the source.
- **Alphabetical order, and no other.** Asserted server-side on symbols registered out
  of order with deliberately mixed verdicts, so a verdict-grouped sort fails. Verified
  by injecting a SOUND-first sort.
- **No score, rank, confidence, or "best" field**, enforced by a recursive scan of the
  whole response against a banned-key list. Verified by injecting a `score` field.
- **The frontend does not sort.** A client-side `.sort()` would satisfy the API test
  and still put a ranking on screen, so a static test asserts `CheckAllPanel` contains
  no `.sort(`, and a browser test reads the rendered rows in DOM order.
- **No row is styled as preferred.** A browser test asserts every `<tr>` carries
  identical classes — a highlight is how a filter becomes a recommendation without
  anyone adding a field — and that the table's own text contains none of
  best/score/rank/recommend/prefer/%.
- **SOUND means "not obviously unsound right now", and the panel says so on screen.**
  It is not a prediction that the price will rise. The disclaimer is asserted in the
  rendered DOM, not just in the JSON.

Note the panel prose necessarily contains the word "ranking" (in "filter, not a
ranking"), so banned-substring scans must be scoped to the table, not the panel.

**There is no Module B signals endpoint or signal panel, and this is not an omission.**
Module B's features produced no excess over a naive comparator at ~16-minute, 1-day,
3-day and 5-day horizons (§2.3). A scored ranking in the UI would present a
known-negative result as a recommendation — the one failure mode that costs real
money. The UI says so on the page, in prose, where the panel would have been. **Any
Module B signal panel still requires the finding to be overturned on holdout first.**

**Owner-approved descriptive scanner (2026-10-31).** A separate market-workspace board
may order up to 20 bullish and 20 bearish observations by a disclosed completed-bar
price/volume formula. This is a descriptive terminal view, not Module B, not an entry
signal, and not a claim of predictive edge. Its current formula skips the first five
09:15 one-minute bars when available, then combines price change, recent-versus-prior
intraday volume, and path efficiency; choppy rows are excluded by a fixed displayed
rule. Session VWAP is descriptive context and is not part of the score. Zones and
alerts use completed five-minute bars by default; charts offer 1/5/15-minute views.
Potential zones require a completed five-minute close through a confirmed swing, a
last opposite-colour candle, and displacement confirmed by a three-candle gap or a
close at least one prior average candle range beyond the zone. Later closes through
the far edge invalidate the zone; overlap is reported as a retest. These are
mechanical annotations only. Intraday OI is unavailable
and must not be invented or substituted with delayed daily OI. Mechanical zones,
confirmed pivots, and equal-price references may be shown, but do not label them
institutional orders or derive projected S1/R1/R2 advice. Keep the board isolated from
entry checks, outcome logs, and position advice. The user explicitly selected this
limited scope.

The descriptive `/market` view is separate from Module B: alphabetical rows report
completed-bar movement, breadth covers fresh subscribed stock symbols, and indices are
separate context. Its owner-approved scanner sub-object has the limited descriptive
price/volume score above; it does not forecast. `/market/{symbol}` draws candles and
mechanical potential zones from completed bars. Default stock coverage remains the
maintained 21-name liquid subset, not the full NSE F&O universe. The index instruments
are context only and are excluded from entry checks.

**REPLAY is the default mode; LIVE must be asked for.** `Mode` is REPLAY or LIVE and
`SessionRuntime.now()` is the *only* mode-dependent read in the whole runtime —
everything else is identical code on identical data. That is what makes "fully
driveable in REPLAY before it ever runs live" true rather than aspirational.

**The header is not subtle about which mode it is in.** `▶ REPLAY — NOT LIVE` in a
colour used nowhere else, versus `● LIVE`. A dashboard that can be confused for live
data while replaying 2024 is worse than no dashboard.

**STALE_DATA and FORCE_EXIT are the two loud states.** They get a pulsing alarm
treatment; nothing else on the page animates, so motion means exactly one thing.
FORCE_EXIT at 15:15 must be unmissable on a second monitor.

**Every price is rendered with its age, and every P&L with gross *and* net.** These
are structural in the components (`PnL` renders both or neither), not conventions a
later edit can quietly drop. A price with no age is a price you cannot trust.

**No localStorage, no sessionStorage.** The server holds all state. A refresh must
not resurrect a stale trailing stop from the browser.

**Nothing is behind a hover.** Assume it is being read at a glance from across a desk.

**`run_loop=False` exists for tests.** `create_app(runtime, run_loop=False)` skips the
background evaluation task so tests advance the clock explicitly instead of racing a
timer. Startup/shutdown is a lifespan context manager — `@app.on_event` is deprecated
and `pytest.ini` has `filterwarnings = error`, so it fails the suite.

**Verified end-to-end on real stored bars** (RELIANCE, 2024-11-26, 600× replay): the
page served, the websocket pushed, and one position walked
`TRAIL → EXIT_NOW → TRAIL → FORCE_EXIT` with gross/net and price ages throughout.
`check-entry` on INFY returned unsound and recorded nothing.

### The frontend ships JavaScript, so a test that does not EXECUTE it proves nothing

The dashboard rendered a blank black page while `/health` returned valid JSON and the
suite was green. Read `tests/test_web_render.py` before touching the frontend.

**Cause: an unpinned CDN shipped a breaking major version.** `@babel/standalone` was
loaded from unpkg with no version. unpkg began serving **Babel 8**, whose
`preset-react` default changed from `runtime: "classic"` to `"automatic"`, so JSX
compiled to `import { jsx as _jsx } from "react/jsx-runtime"`. An ES import inside a
classic `<script>` is a parse error; the whole app script never ran, `#root` stayed
empty, and `body { background: #0b0f14 }` did the rest. **No line of our code
changed.**

- **Everything is vendored in `web/vendor/` and there are ZERO external requests.** A
  local single-user tool must render with the network unplugged.
  `create_app()` raises at startup if a vendored file is missing, and
  `test_web_page_makes_no_external_requests` fails on any off-origin `src`/`href`.
- **The classic JSX runtime is named explicitly** via a registered `react-classic`
  preset, so a future Babel swap cannot reintroduce it. Both guards exist because
  either alone is one edit away from deletion.
- **A substring match on the served HTML is not a render test.** The old check asserted
  "REPLAY"/"FEED STALE"/"FORCE EXIT" were present — they were, inside the
  never-executed `text/babel` block. With the bug reintroduced, all four substring
  assertions still passed and **8 of the 10 tests in `test_web_render.py` failed.**
- **Chrome missing makes those tests FAIL, not skip** (override with
  `FNO_ALLOW_NO_BROWSER=1`). A skipped render test is the green-suite-blank-page
  failure mode again.

**Two more bugs the blank page was hiding**, both of which independently blank the UI:

- **`evaluate()` crashed formatting a label.** `snap.atr14` is `None` when a trailing
  stop is resumed into a session with <15 completed bars, and `f"{None:.2f}"` raises.
  That `TypeError` 500'd `/positions` **and** the websocket snapshot. `evaluate_all()`
  now isolates per position and emits an `ERROR` card carrying the exception text —
  losing every position's advice because one broke is the worst available outcome.
- **The header read `● LIVE` whenever it had no data.** `h.mode === "REPLAY"` is false
  for `undefined`, so a page that had loaded nothing looked like a healthy live
  session. Mode is now three-valued and the unknown case is loud. **Never let an
  unknown state render as LIVE.**

The page also installs a `window.onerror` overlay: if the app fails to mount it says
so on screen, in red, and names the CLI fallback. It caught one of my own regressions
during this work.

### A stale dashboard on the port is a session-identity bug

Three separate defects reported off one rendered page — wrong date in the banner,
duplicate position cards, clock past session close — were **one leftover server**
still holding `:8000` from an earlier launch. The new launch could not bind, and the
browser at that address showed the OLD session: different trade date, different
positions, a finished clock. Nothing on the page said so.

`check_port_free()` now runs before anything else in `serve()`, and when the occupant
is another dashboard it reports that instance's mode, trade date and clock:

```
127.0.0.1:8000 is already in use. It is another fno-advisor dashboard: REPLAY mode,
trade date 2026-08-19, clock 2026-08-19T10:25:00+05:30. That is what your browser
would have shown you -- NOT the session you just asked for.
```

Treat "the dashboard is showing the wrong thing" as a session-identity question first.
`test_the_banner_date_matches_the_date_it_was_launched_with` covers the other half:
it serves two different dates through `build_runtime` and asserts the banner names
the one requested and **not** the other. Verified against a hardcoded banner.

### Exact-duplicate positions are rejected, 409

Two identical cards are indistinguishable from one position rendered twice, and the
difference is whether you hold 100 shares or 200. `add_position()` rejects a match on
symbol + quantity + entry price + entry time among OPEN positions;
`allow_duplicate=true` opts in. Anything differing in any field is not a duplicate.

### The replay clock is clamped to `last_bar + REPLAY_TAIL` (5 min)

It always halted there, but the step was applied *before* the check, so at 600x (one
tick = 10 virtual minutes) the clock parked at 15:45 for a session whose last bar was
15:29. Fifteen minutes of market that never happened, displayed as the current time.
Now clamped, so the terminal clock is the same at any speed.

**FORCE_EXIT after the replay ends is correct and must stay.** The session ending
does not close anything — the position is still held — so the advice keeps saying so
rather than going quiet. Both are pinned by tests.

### ELEVATED_EVENT_RISK is an exclusion filter, and every guard on it is structural

`src/data/announcements.py` (collector), `src/market/event_risk.py` (classification and
assessment), `/event-risk`, a badge on every position card and a column in the bulk
check table. 96 tests across `tests/test_announcements.py` and `tests/test_event_risk.py`.

The claim it makes is narrow and entirely negative: **a material announcement landed
since the previous close or is scheduled today, so price is being driven by information
rather than flow and normal technical reasoning does not apply to this symbol.** It says
nothing about direction, carries no number, and **no claim is made that it improves
returns.** It identifies days on which the model has no business having an opinion.

**It lives in `src/market/`, not `src/signals/`.** It is a property of the market's
state, like a holiday or a ban period. Beside the signal engine it would attract a score.

**FORWARD-COLLECT ONLY, and the endpoint that would break this is one line away.**
`?symbol=RELIANCE` returns 3,336 rows back to 2006. That capability is deliberately
unused and unreachable: `fetch_announcements` pins `from_date` and `to_date` to the same
session, and a test asserts the literal. Filings are revised — a corrigendum is filed,
NSE restates the subject, an attachment is replaced — so what the endpoint returns for a
past date is what the record looks like TODAY. **Neither the poison harness nor
`assert_session_causal` can catch that**: the bar series does not change and every
session in the date list is genuinely complete, so the leak lives in the CONTENT of a
row. There is no harness here that can detect it and the pre-revision text no longer
exists to compare against, so the only safe posture is to never create the data.

**`first_seen_at` is our clock, and it is the earliest sighting, never the latest.** The
ledger is append-only JSONL; `observations()` derives `first_seen_at = min(observed_at)`
per key. A later edit at the source therefore cannot move an observation earlier in
time. The exchange's `an_dt` is stored for audit and is a claim by a mutable source, not
evidence. A revision lands on the SAME key (keys are stable across content changes, by
design) and sets `revised`, which is surfaced on screen.

**UNKNOWN is the third state and it is the important one.** No poll ever, a poll older
than `MAX_POLL_AGE` (30 min), no source configured, a source that raises, or REPLAY —
all yield UNKNOWN for every symbol, never CLEAR. This is the same failure the header had
when it rendered `● LIVE` with no data: an unknown state that looks like a good one.
`UNKNOWN` gets its own colour on the page and a test asserts it does not match CLEAR's.

- **A poll that finds nothing still records that we looked** (`record_empty_poll`).
  Without it "no news" is indistinguishable from "the collector died", and only one of
  those is safe to trade on.
- **REPLAY never consults the source at all** — asserted by injecting an ELEVATED source
  into a replay runtime and checking the source was called zero times.

**It ANNOTATES; it never decides.** A flagged position still gets its normal exit advice
and still gets FORCE_EXIT at 15:15. Suppressing advice on a news day is the worst
available reading of "normal technical reasoning does not apply" — the one thing you
certainly still need when a stock is gapping on news is to be told to be flat.
`test_event_risk_never_changes_advice` freezes the clock and asserts the position cards
are byte-identical with and without an elevated flag, and a structural test asserts the
exit engine contains no reference to event risk at all.

**No sentiment, no direction.** Categories name a KIND of event: `RATING_CHANGE`, never
`RATING_UPGRADE`. `FORBIDDEN_DIRECTIONAL_TERMS` is checked against every category name
by a test. Classification matches NSE's own `desc`/`purpose` fields — controlled
vocabularies, so the keyword table is auditable against a finite list — and never the
free-text attachment summary, which would produce false positives nobody could review.
The table was built from vocabulary observed in the live feed, and
`unclassified_subjects()` reports what matched nothing, because a gap there silently
renders a symbol CLEAR.

**In the bulk table the badge is in a `<td>`, never on the `<tr>`.** A row-level
highlight is how a filter becomes a recommendation, and that holds whichever direction
it points in. The identical-row-classes assertion still stands; the header is now
`symbol | verdict | event risk | reason`.

**Collection is a separate scheduled job.** `python -m src.cli collect-announcements
[--repeat N --interval 900]`. The dashboard only reads the ledger, so an NSE outage
degrades the panel to STALE instead of hanging the page. Run it every 15 minutes through
the session — that tolerates one missed run inside the 30-minute limit.

### `clear-positions` deletes records; `close-position` does not

`python -m src.cli clear-positions --date D [--position-id a,b] --confirm`. Without
`--confirm` it lists what would go and exits 1, deleting nothing. It also drops the
matching runtime rows, so a cleared position cannot leave a trailing stop behind to be
resumed against something that no longer exists. Positions are decision records rather
than raw market data, so this does not violate the append-only rule on `data/raw`.

---

## 2.3 Module B as built — and why it does not yet earn its place

Full report: `reports/signal_evaluation.md`. DEV only, 2020-01-01..2024-11-26,
1212 sessions, 20 symbols, **n = 2,368 signals**. The six-year holdout is UNTOUCHED.

**It loses money, and it loses more than the baseline.** Mean net **−Rs 209/trade**
(95% CI [−230, −189]) against the "buy the highest-volume stock at 09:45" baseline's
−Rs 166 (95% CI [−187, −146]). Both are decisively loss-making; neither interval is
close to zero.

The damning number is not the net. **Module B is negative BEFORE costs
(gross −Rs 65k) while the baseline is positive before costs (gross +Rs 20k).** This
is not an edge eaten by friction — the selection is worse than "whatever traded most
this morning", and it trades twice as often to achieve that.

### The stop was fixed by construction, and it worked — the P&L did not follow

`reports/stop_calibration.md`. The 1.0-ATR stop sat at **1.02× the mean adverse
excursion** — inside the noise floor, so it exited on chatter whatever the signal was
worth. That is a design error, not a bad setting, and it was fixed by measurement
rather than by search:

Simulate a stop-less long at 51,223 screened entries, walk to the 15:15 square-off,
and for the 40,105 that reached T1 record the worst adverse excursion they had to
survive first. The **p75 of that distribution is 2.37 ATR** — three quarters of
winners never touch it. Targets re-derived from the stop to preserve the declared
1.5:1, and checked for reachability: 54.7% of entries reach the new T1, against a
median favourable excursion of 4.04 ATR.

| setting | old | new | source |
|---|---|---|---|
| `stop_atr_multiple` | 1.00 | **2.37** | p75 of survivable MAE, n=40,105 |
| `t1_atr_multiple` | 1.50 | **3.56** | stop × 1.5, preserving R:R |
| `t2_atr_multiple` | 3.00 | **7.11** | stop × 3.0 |
| `min_composite_score` | 0.350 | **0.594** | p90 of observed scores, n=4,891 |

**Result: the structural fix worked; the strategy still loses.**

| | before | after |
|---|---|---|
| STOP_HIT share | 97.5% | **77.7%** (PASS, threshold was 80%) |
| stop ÷ mean MAE | 1.02× | **1.78×** |
| mean R | −0.87 | **−0.39** |
| NO_SIGNAL sessions | 1.1% | **3.7%** |
| signals at 09:30 | 87.2% | **40.0%** |
| mean net/trade | −Rs 209 | **−Rs 208** |
| gross before costs | −Rs 65k | **−Rs 59k** |

Loss per unit of risk more than halved and the stop stopped being the default exit,
but net P&L barely moved and **gross is still negative**. That is the finding: the
stop was genuinely broken and is now genuinely fixed, and it was never the reason the
engine loses money. **The selection has no edge.** No amount of exit geometry rescues
a negative gross.

### Selectivity was a multiple-comparisons problem, and it is now fixed

Raising the score bar from p74 to p90 moved NO_SIGNAL from 1.1% to only 3.7%, because
a session signalled if ANY of 20 symbols cleared at ANY of 61 evaluations — ~1,220
draws a day. **A per-candidate bar cannot make sessions selective.**

`run_single_shot()` replaces that with ONE draw: evaluate once at a fixed decision
point, take only the day's single highest-scoring candidate, trade it only if it
clears the absolute bar, else NO_SIGNAL. If the top name is then rejected by a
downstream gate the session is NO_SIGNAL — it does **not** fall through to second best,
because walking the ranking is how one draw silently becomes several.

Measured once at two decision points (`reports/single_shot_evaluation.md`), neither
selected:

| | continuous | @09:45 | @10:15 |
|---|---|---|---|
| draws/session | ~1,220 | 1 | 1 |
| NO_SIGNAL | 3.7% | **31.2%** | **51.0%** |
| signals | 2,236 | 834 | 594 |
| mean GROSS/trade | −Rs 208 (net) | **−Rs 33** | **+Rs 42** |
| gross 95% CI | — | [−70, +4] | [−7, +91] |

### The answer on Module B

**Gross is now INCONCLUSIVE, not negative** — both intervals span zero, so the
pre-stated "gross is negative ⇒ no edge" rule does not strictly fire. What the sample
*can* distinguish, on identical sessions:

1. **The baseline's gross edge clears zero; Module B's does not.** On the same 834
   days, baseline gross +Rs 29/trade (CI [0.08, 58]) vs Module B −Rs 33. On the same
   594 days, +Rs 61 (CI [23, 100]) vs +Rs 42. Picking the best-scoring name is worse
   than taking whatever traded most that morning.
2. **The two decision points disagree on the SIGN of the gross edge** while overlapping
   on only 52.7% of traded days. A result whose sign flips on a 30-minute shift is not
   robust.
3. **Net is decisively negative for everything, engine and baseline alike.**

### The cost hurdle is the real constraint

Costs are **~Rs 182/trade = 0.182% of notional**. The mean favourable excursion over
the whole holding period is **0.489%**. A trade must therefore capture **~37% of the
best price it ever sees** just to break even, on positions stopped out ~80% of the
time and held ~16 minutes.

That arithmetic, not the selection rule, is what every intraday strategy on this
universe has to beat. It is why every net figure in every report here is negative
regardless of which rule produced it. **Before more selection work, the honest next
question is whether a ~16-minute holding period can clear 0.182% at all** — a longer
hold, a larger notional (the ₹20 brokerage cap stops scaling), or a different
instrument would each change the arithmetic more than any feature will.

**Do not nudge thresholds until the number improves — that is fitting to DEV.** Change
one thing, re-measure on DEV, keep the holdout for one evaluation at the end.

### CLOSED: these features carry no edge at any tested timescale

`reports/swing_evaluation.md`. The last hypothesis was that the same features might
predict on a horizon where friction is not the dominant term. Tested on 1, 3 and
5 trading-day holds, DEV 2020-01-01..2024-12-31, entry 15:15, delivery costs, stops
recalibrated fresh on each horizon, gaps modelled, excess measured over an
equal-weight panel hold on identical dates.

| horizon | trades | gross % | net % | **excess %** | 95% CI (effective n) | n_eff | MDE |
|---|---|---|---|---|---|---|---|
| 1d | 898 | +0.042 | −0.296 | **−0.120** | **[−0.208, −0.032]** | 898 | 0.125 |
| 3d | 898 | +0.171 | −0.168 | −0.089 | [−0.217, +0.039] | 300 | 0.183 |
| 5d | 898 | +0.253 | −0.085 | −0.070 | [−0.229, +0.096] | 180 | 0.233 |

**1d excess is decisively negative. 3d and 5d span zero and are underpowered — both
|means| sit below their own MDE.** No horizon clears zero. The pre-stated rule fired.

**Gross is positive at every horizon and all of it is beta.** +0.042 / +0.171 / +0.253
looks like an edge growing with the hold. Subtract an equal-weight hold of the same
panel over the same dates and it is negative everywhere. The strategy was long in a
market that went up. This is precisely why a naive comparator is mandatory on a
multi-day horizon.

Tested now at ~16 minutes (continuous and single-shot) and at 1, 3 and 5 days. Not one
produced an excess clearing zero. **The problem is the features, not the holding
period and not the friction.**

### CLOSED: F&O positioning features carry no edge either (2026-08-23)

`reports/fno_evaluation.md`. The remaining hypothesis was that the price/volume features
were simply the wrong INPUTS, and that F&O positioning data — open interest, the
OI-price divergence states, futures basis, India VIX, rollover — would carry what they
did not. It does not.

Pre-registered in `reports/.fno_preregistration.json` **before any P&L was computed**,
and the evaluation script refuses to run if the live constants have drifted from it.
One horizon (1 trading day), one decision point per session at 15:15, the single
best-scoring candidate, an absolute score bar of +0.25 fixed from the [-1, +1] scale
rather than read off a percentile of the data. DEV 2020-01-01..2024-12-31, 19 symbols,
1116 decision dates.

| arm | trades | gross % | panel comparator % | **excess %** | 95% CI (n_eff) | n_eff |
|---|---:|---:|---:|---:|---|---:|
| primary, bar +0.25 | 907 | +0.0022 | +0.090 | **−0.088** | **[−0.170, −0.006]** | 907 |
| power reference, no bar | 1114 | +0.0004 | +0.112 | **−0.112** | **[−0.185, −0.039]** | 1114 |

**Both intervals lie entirely below zero.** The pre-stated rule fired. Per that rule:
no sweep, no second horizon, no subset search, and **no holdout evaluation** — the swing
holdout (2025-01-01 onward) is still UNSPENT.

The power-reference arm was declared in advance precisely so a null could not hide
behind low power: it takes the day's best candidate every session, giving the largest
sample the design can produce. It is the more negative of the two.

Note the shape is the same one the swing experiment found: gross is roughly zero while
the equal-weight panel returned +0.09 to +0.11% over the same dates. The strategy did
not lose money by being wrong about direction; it lost by selecting worse than not
selecting at all.

**Do not re-litigate this by adding an intraday OI feature.** Per-minute OI does not
exist in this data — see below — so that is a data-acquisition project, not a tweak.

### F&O data facts established here that outlive the experiment

**OI is DAILY, and no amount of engineering changes that.** Fyers' history endpoint
returns six-field OHLCV with no open interest at any resolution; OI comes from the
end-of-day bhavcopy. India VIX comes from NSE's daily index-close file. There are no
intraday futures bars. Any request for a 1/5/15/30-MINUTE OI or basis feature is a
request for data this project does not have. Never resample a daily row into minutes.

**THREE discontinuities break a naive OI change, not one.** All three must yield None,
never zero:

1. **The roll.** RELIANCE's near-month OI moves **+292%** on its 2024-10-31 roll. Total
   OI summed over every listed expiry *is* continuous through a roll, which is the only
   reason 15- and 30-day windows are possible — a monthly contract does not live long
   enough for one to fit inside it.
2. **Corporate actions rescale OI itself.** NSE restates lot size at a split or bonus,
   so OI in units jumps by the action's factor: RELIANCE **+73.9%** near / **+99.2%**
   total on 2024-10-28, WIPRO **+88.9%** / **+90.2%** on 2024-12-03. Neither is a
   position build. This one is easy to miss because it is not the roll.
3. **The two sources are on DIFFERENT PRICE SCALES, silently.** Fyers spot is
   split/bonus ADJUSTED; bhavcopy futures are RAW. Pre-bonus RELIANCE futures ÷ stored
   spot is **2.007**; TATASTEEL's is **10.0**; KOTAKBANK sits at **5.0** and BAJFINANCE
   at **10.0** for the whole of DEV because their actions fall *after* it. A basis
   computed as `futures − spot` is then a 100% error that looks like nothing is wrong.
   `price_scale_factor` snaps the ratio to a candidate corporate-action factor and
   REFUSES when nothing fits. Price CHANGES are taken from adjusted spot, never raw
   futures.

**Demergers defeat that snap, and the tell is persistence, not size.** A demerger's
adjustment factor is a value ratio, not a simple rational, so it lands *inside* the
snap's 5% tolerance, is absorbed into a factor of 1.0, and re-emerges as a constant fake
premium. **ADANIENT's futures/spot ratio is 1.0350 in every one of the twenty quarters
from 2020Q1 to 2024Q4** — including 2023Q1, when the stock lost ~70% on Hindenburg. A
real basis cannot hold flat through that (SBIN's is 1.002 and moves). Do NOT widen
`CANDIDATE_FACTORS` to catch it: that destroys the 20% separation which makes the snap
unambiguous, after which a genuine 3% basis is silently snapped away.
`basis_is_plausible` refuses the basis features for any symbol whose MEDIAN basis
exceeds 1.5% of spot.

**Three India VIX files carry a US-order date stamp, and one destroyed a session.**
`ind_close_all_06042023.csv`, `..._10042023.csv` and `..._11042023.csv` write their
`Index Date` as MM-DD-YYYY while the rest of the archive uses DD-MM-YYYY. Keyed on the
internal date, the April file collided with the genuine 4-October file and **won**:
2023-10-04 took April's VIX of 12.27 instead of its own 11.66, and 2023-04-10 vanished.
1640 files became 1639 rows and nothing said so. `VixStore.load_all` now keys by the
FILENAME, which is the URL the session was requested at. A swap is only detectable when
the day is ≤12 and differs from the month, so there may be undetectable ones — keying by
filename makes that harmless rather than something to hunt for.

**The 1-day stop recalibrates to 0.302 daily ATR** on this panel, against the swing
experiment's independently derived 0.299. Two runs of the same method on overlapping
data agreeing to three decimal places is a useful check that the calibration is
deterministic and correctly wired.

### Things this experiment established that outlive it

**Delivery costs MORE than intraday, not less.** 0.338% vs 0.183% round trip on
Rs 1,00,000, *despite* zero brokerage: delivery STT is 0.1% on BOTH legs against
intraday's 0.025% sell-only (Rs 200 vs Rs 25). Never assume a longer hold is cheaper.
`src/costs.py` has `delivery_*` alongside the intraday functions; pick by holding
period, not by habit.

**Overnight gaps are material and must fill at the open.** 16.1% of 1d stops and ~10%
of 3d/5d stops gapped through, mean shortfall 0.79–0.85% of entry — comparable to the
whole stop distance. `simulate_swing` fills gapped stops at the OPEN. Filling at the
stop price would invent that money.

**Overlapping windows are not independent draws.** 898 trades at a 5-day hold is
180 effective observations, a 5x overstatement. `effective_n()` counts non-overlapping
date blocks; `mde_from()` must be fed that, never the trade count.

**Define a winner without circularity.** Calibrating a stop from "positions that
reached the target" is circular when the target is derived from the stop. Use profit
at the horizon. A first attempt did it the circular way and produced a 0.32 ATR stop
calibrated against a 1.5 ATR target that was never traded.

**Daily features leak through the cross-session route.** `assert_session_causal` is
run on all eleven daily features plus two deliberately leaky controls that it
correctly rejects. The bar-level poison harness cannot see this class at all.

### The look-ahead harness — build it before the features it guards

`src/analysis/lookahead.py`. Every feature is evaluated three ways at each timestamp:
with future bars present, truncated away, and **poisoned** (replaced by nonsense of
the same shape). Any disagreement is a leak. Poisoning is the sharper of the two:
truncation changes list length, so a feature keying off `len(bars)` can fail it
innocently or pass it guiltily.

Two limits worth knowing, both learned the hard way here:

* **A ratio leak can survive poisoning.** Scale numerator and denominator by the same
  factor and it cancels exactly. `tests/test_signal_leak.py` pins this.
* **Cross-session leaks are invisible to it.** The volume baseline leaks by being
  handed the wrong list of DATES; every bar in those dates is genuinely complete, so
  nothing about the current session changes. `assert_session_causal` is the only
  thing covering that class, and `trailing_sessions()` (strict `<`) is the single
  function standing between the profile and the bug.

Verified causal on real bars, not only synthetic: 56 feature-checks × 16 timestamps
× 4 symbols.

---

## 3. DATA

### 3.1 Broker

- **Primary: Fyers API v3**, free tier, Python SDK `fyers_apiv3`.
- **Secondary: Dhan** — later, not now. Keep broker access behind an interface in
  `src/data/` so a second provider can slot in, but do not build the abstraction
  speculatively before the second provider is real.

### 3.2 Authentication — the 2FA step is MANUAL

- OAuth + 2FA. The access token must be regenerated **daily, before pre-open**, per
  SEBI requirements.
- **The 2FA step is manual and must stay manual. Do not attempt to automate it.** No
  TOTP-secret storage, no headless-browser login, no OTP scraping from SMS or email.
  This is both a SEBI-compliance boundary and a security boundary.
- Design for the human in the loop: `scripts/` gets a token-refresh script that prints
  the auth URL, waits for the user to paste the redirect URL back, and stores the
  resulting token.
- Token storage: local file outside version control (`data/` or a user-profile path),
  never in the repo, never in code, never in a log line.
- Every API call path must handle "token expired" by failing with a clear
  *"run the daily token refresh"* message — never by retrying in a loop.

### 3.3 Credentials

- **Credentials NEVER appear in code.** Not as defaults, not in tests, not in
  docstrings, not in example snippets.
- `.env` holds real values and is gitignored. `.env.example` holds placeholder shapes
  and is committed.

> **`.env.example` is checked, not trusted (2026-08-27).** A real app id and a pasted
> authorisation URL were once appended to the bottom of it and sat there unnoticed —
> in the one file whose whole purpose is to be committable. `tests/test_env_example.py`
> now scans it for a URL with a query string, a credential-bearing parameter, a JWT
> shape, a Fyers app-id shape, and any long non-placeholder value, and is **verified by
> injection** against each shape.
>
> Two rules that file holds itself to. **A failure reports line numbers and rule names
> and never the matched value** — an assertion that echoes a credential into the test
> output has copied it somewhere new, and `test_a_failure_message_never_contains_the_matched_value`
> pins that. **It never reads the real `.env`**, which legitimately holds live values;
> `test_the_real_dotenv_is_never_read` asserts this module names only the example.
>
> Scan case-SENSITIVELY for the app-id shape. A case-insensitive first draft matched
> `bg-fuchsia-950` and produced 40 false positives in `web/index.html`. And keep the
> prose free of the literal parameter names it bans — an explanatory comment saying
> `generate-authcode` trips naive external scanners, the same way the bulk table's
> "not a ranking" prose tripped its own guard.
- Secrets are typed `SecretStr` in settings so they do not leak into reprs or logs.
- Never print, log, or include a token/secret in an error message or a commit.

### 3.4 Storage

- **Parquet files partitioned by date + symbol**, queried with **DuckDB**.
- **No database server.** Do not propose Postgres, SQLite-as-primary, Redis, or a
  message queue. The whole point is a zero-ops local store.
- Layout: `data/raw/` (as fetched, immutable), `data/processed/` (derived),
  `data/reference/` (holiday list, F&O universe as-of date, ban lists),
  `data/journal/` (module D), `data/positions/` (module C).
- Money columns are `decimal128(18, 4)`, never float. A float price column is how
  1000.55 comes back as 1000.5500000000001 and a reconciliation starts failing by a
  paisa.
- **DuckDB gotcha:** `asof` is a reserved word (ASOF JOIN). Quote it — `min("asof")`,
  not `min(asof)`, which is a parse error. Same for `first`/`last` as aliases.
- **DuckDB needs `pytz` installed** to materialise TIMESTAMPTZ columns, even though
  our own code never imports it. Do not prune it from requirements.
- Raw data is **append-only and immutable**. A correction is a new file plus a
  recorded reason, never an in-place edit — otherwise backtests silently change
  underneath you.
- `data/` is gitignored in its entirety.

### 3.5 Additional sources

Both of the F&O sources are **backfilled and on disk**; neither needs a broker token,
so both can be rebuilt on any machine without an account.

| source | module | script | state |
|---|---|---|---|
| NSE F&O daily **bhavcopy** — expiries, futures OHLC, **daily OI** | `src/data/bhavcopy.py` | `scripts/bhavcopy_backfill.py` | 1641 dates, 2020-01-01..2026-08-21, 0 duplicate (date, symbol, expiry) rows |
| **India VIX** daily, from NSE's index-close file | `src/data/india_vix.py` | `scripts/india_vix_backfill.py` | 1640 sessions, 2020-01-01..2026-08-19, 0 missing, 0 failed |
| **Corporate announcements + board-meeting calendar** | `src/data/announcements.py` | `python -m src.cli collect-announcements` | FORWARD-COLLECT ONLY. Feeds the ELEVATED_EVENT_RISK exclusion filter. **Never backfill it** — see §2.4. |
| **F&O ban list** | — | — | NOT BUILT. The ban gate is still a stub. |
| **Participant-wise OI** (free scrape) | — | — | NOT BUILT. |

India VIX is an INDEX, not a contract, so it is **absent from the F&O bhavcopy** on every
sampled date — do not go looking for it there. `scripts/futures_inventory.py` reports
what the bhavcopy actually contains; `reports/fno_data_inventory.md` is its output.

For scraped sources: be a polite client — identify honestly, rate-limit, cache
aggressively, and never hammer NSE endpoints. Persist the raw payload before parsing,
so a parser change can be replayed without re-fetching.

---

## 4. MARKET RULES

| Rule | Value |
|---|---|
| Regular session | **09:15 – 15:30 IST** |
| Pre-open | 09:00 – 09:08 IST (order collection; **read-only interest to us**) |
| Intraday square-off | **15:15 IST** |
| Trading days | Mon–Fri, minus NSE holidays |

Encoded in `src/market/calendar.py`. Always go through that module — never re-derive a
session time inline.

- **F&O ban period: skip the symbol entirely.** A symbol in ban has no new-position
  capacity; it must not be ranked, suggested, or entered. Check the ban list published
  the previous evening.
- **NSE holiday calendar must be checked before any session starts.** No exceptions —
  a run on a holiday should refuse to start, not produce empty results that look like
  `NO_TRADE`. Those two states must never be confused.
- **Circuit limits exist and a symbol can become untradeable mid-session.** Position
  Tracker must handle "cannot exit right now" as a real state and surface it plainly
  rather than repeating an exit suggestion that cannot be acted on.
- **Muhurat trading** (Diwali) is a special session on a day that is otherwise a
  holiday. It is *not* a normal trading day: `is_trading_day()` returns `False` for it.
  It is listed separately in the calendar module. Never include muhurat sessions in
  backtests or in daily statistics — the liquidity profile is not comparable.
- Expiry days behave differently (pin risk, gamma, volume distortion). Do not treat an
  expiry day as an ordinary day in any statistic without flagging it.

### 4.0 The calendar is tape-verified for 2020–2026

`HOLIDAY_DATA_VERIFIED` is True for every year 2020–2026, derived on 2026-08-21 from
daily-resolution candles for RELIANCE, HDFCBANK and INFY unioned across
2020-01-01 → 2026-02-15. A weekday with no candle for any of them is a day the
exchange did not run a regular session. The three never disagreed on a single day.

It reconciles exactly: **1515 regular sessions + 8 short sessions = 1523**, which is
precisely the set of days the tape shows traded. Nothing invented, nothing lost.

**Published lists are corroboration, never the source.** calendarlabs' 2020 page
disagreed with the tape on 11 dates; direct 1-minute probes confirmed the tape on all
11 — it lists 2020-03-06 as Holi when a full 375-bar session traded, and omits
2020-11-30 Guru Nanak Jayanti when the market was shut. Two 2026 sources agreed with
each other and the tape; one 2020 source was simply wrong. Trust the tape.

#### Two categories the weekend rule alone gets wrong

`NSE_SPECIAL_TRADING_DAYS` — **weekend days with a full 375-bar session.** Union
Budget Saturdays (2020-02-01, 2025-02-01), Budget Sunday 2026-02-01, and the
2024-01-20 special session. `is_trading_day` returns True for these. Without them the
weekend rule loses four full sessions with **no error and no EMPTY marker** — the
silent-loss failure mode again.

`NSE_SHORT_SESSIONS` — **ceremonial or test sessions, NOT trading days.** Six Diwali
muhurat sessions (~60 bars, 18:15–19:14 through 2024; NSE moved it to 13:45–14:45 in
2025) and two DR-test Saturdays (105 bars, 09:15–12:29). Four of the muhurat days fall
on **weekdays** (2021-11-04, 2022-10-24, 2024-11-01, 2025-10-21), so only an explicit
rule keeps them out — a weekday with a candle otherwise looks like a normal session.

When adding a year: derive it from the tape, classify anything anomalous by 1-minute
bar count (375 = full, ~60/105 = short), and only then set the verified flag.

### 4.1 What a wrong 2026 list cost, as a worked example

`HOLIDAY_DATA_VERIFIED[2026] = True` as of 2026-08-21. Verified two ways: against the
**exchange's own tape** for 2026-01-01 → 2026-08-20 (every weekday queried; zero
candles means the exchange did not trade), and against two independent published
lists for the full year. `TAPE_VERIFIED_THROUGH[2026]` records where tape evidence
stops — 2026-09-14 onward rests on published sources alone.

The previous model-recalled list had **four errors**, all of which had already
corrupted a backfill before anyone noticed:

| Date | Old list said | Truth | Damage |
|---|---|---|---|
| 2026-01-15 | *(absent)* | Holiday (civic elections) | outside the window, would have gone wrong later |
| 2026-03-03 | trading | Holiday (Holi) | 19 EMPTY entries |
| 2026-03-04 | Holiday (Holi) | **Traded** | 19 symbol-days silently missing |
| 2026-03-20 | Holiday (Id-Ul-Fitr — invented) | **Traded** | 19 symbol-days silently missing |
| 2026-05-27 | Holiday (Bakri Id) | **Traded** | 19 symbol-days silently missing |
| 2026-05-28 | trading | Holiday (Bakri Id) | 19 EMPTY entries |

Note the asymmetry, because it decides how hard you have to look. A day wrongly
marked a **holiday** is never requested, so it leaves **no trace at all** — the data
is just quietly absent. A day wrongly marked **trading** at least shows up as EMPTY.
Only the second kind is self-announcing; the first kind cost 57 symbol-days that
nothing in the pipeline would ever have flagged.

Years with no holiday data raise `UnknownHolidayYearError` — the calendar **fails
closed**. Keep it that way; do not add an "assume it's a trading day" fallback. When
adding a year, verify it the same way (`scripts/` has the probe pattern) and only
then set its `HOLIDAY_DATA_VERIFIED` flag.

---

## 5. CODE CONVENTIONS

- Python 3.11. Type hints on everything public.
- `src/` is the package root; `config/` holds settings; `tests/` mirrors `src/`.
- Import `IST`, `now_ist`, `ensure_ist` from `src.market.calendar`. Do not re-implement.
- Prefer explicit failure over silent fallback. In a trading system, a wrong number is
  far worse than a raised exception.
- No bare `except:`. No `except Exception: pass`.
- Money: never `float` for a price the user sees as currency in a total. Use `Decimal`
  where cents matter (P&L, costs); `float` is fine for indicators and ratios.
- Tests: `pytest`. Anything touching time must accept an injectable `asof`/`now`
  parameter so it can be tested deterministically. No `datetime.now()` inside logic.
- Every new time-series function needs (a) the data-availability docstring from §1.2
  and (b) a test that would fail if the boundary were off by one bar.

---

## 6. SESSION CHECKLIST FOR CLAUDE

Before you finish a task, verify:

- [ ] No broker order API is called anywhere.
- [ ] Every time-series function has a data-availability docstring.
- [ ] No naive datetimes. No `datetime.now()`. No `pytz` in our code.
- [ ] Every P&L figure has a net value beside its gross value.
- [ ] `NO_TRADE` / empty-candidate paths exist and are tested.
- [ ] No credentials in code, tests, logs, or docs.
- [ ] No database server introduced.
- [ ] Build order respected (C before A/B/D; ML only past the §2.1 gate).
- [ ] Unverified data (holidays, cost rates) still flagged as unverified.

**When in doubt, ask the user. This is real money.**

---

## 7. VALIDATION STATUS — read before quoting any number

**Six years of real data** (backfilled 2026-08-21): 2020-01-01 → 2026-08-19,
**32,932 symbol-days, 12,344,587 one-minute bars**, 80 quarantined, 7 empty.
20 symbols cover 1512/1515 sessions (99.8%) of the six-year window.

### Prices are SPLIT/BONUS-ADJUSTED — know what that means

Fyers serves history restated in today's terms. Verified: TATASTEEL shows ~₹95 in
July 2022 when it actually traded ~₹950 (1:10 split); RELIANCE ~₹1335 in Oct 2024
against a real ~₹2670 (1:1 bonus); WIPRO and TMPV likewise. A >20% discontinuity scan
across 21 symbols × 6 years found **exactly one** step, and it was the COVID crash
(AXISBANK −21.8% over 2020-03-20 → 2020-03-24), not a corporate action.

Consequences, in order of how likely they are to bite:

- **Percentage returns are correct.** Fixed-notional turnover is preserved, so P&L in
  percent, cost percentages and the ₹20-vs-0.03% brokerage cap all behave.
- **Absolute historical prices are NOT what you would have seen**, and neither are
  share counts. A ₹100k notional buys 1,052 "shares" of 2022 TATASTEEL in this data
  and 105 in reality. Never reason about lot size, tick size or share counts from
  historical bars.
- **It is look-ahead in the strict sense** (CLAUDE.md 1.2): the series was adjusted
  using knowledge of a future split. Benign for intraday direction — a split tells you
  nothing about the next 60 minutes — but it must be stated, not assumed away.
- Do NOT add an adjustment step. The data is already adjusted; adjusting again would
  corrupt it. `src/data/corporate_actions.py` reports and never adjusts, and a test
  asserts no function in it is named like an adjuster.

### The 80 remaining quarantines are 4 dates, and 3 of them are real market halts

| date | symbols | cause |
|---|---|---|
| 2020-03-13 | 20 | circuit-breaker halt, 09:21→10:21 (60 min) — COVID crash |
| 2020-03-23 | 20 | circuit-breaker halt, 09:58→10:57 (59 min) |
| 2022-03-07 | 20 | halt, 09:16→09:39 (23 min) |
| 2026-08-20 | 20 | vendor duplication with conflicting high/low/volume |

The first three are **correct data of a halted market**, quarantined by the
">5 min intrasession gap" rule working as specified. If a future analysis wants them,
whitelist those dates deliberately — do not loosen the gap rule, which is the only
thing catching genuine holes.

**The exit rules have still never been evaluated against it.**
`reports/momentum_analysis.md` says NO DATA YET until the analysis is actually run.
Do not quote a number from a synthetic replay as if it described the market.

### The panel and its holdout

`src/analysis/split.py` defines `PANEL_FULL_HISTORY`: 2020-01-01 → 2026-02-15,
**DEV 2020-01-01..2024-11-26 (1212 sessions), HOLDOUT 2024-11-27..2026-02-13 (303)**,
chronological 80/20. Claim file `reports/.holdout_claim_full_history.json`.
**UNSPENT as of 2026-08-21.**

`SPENT_REGIONS` excludes 2026-02-16 → 2026-08-19 entirely. That range's DEV was swept
and its holdout evaluated, so it can never be a holdout again — data that has been
looked at is development data forever. It has its own claim file
(`reports/.holdout_claim.json`) so the two panels cannot decide anything about each
other.

DEV spans three regimes (2020 crash 251 sessions, 2021-22 trending 494, 2023-24 467).
The HOLDOUT is 279 sessions of 2025-26 plus 24 of late 2024 — recent, but **largely
one regime**. It tests recency, not regime robustness. Say so when reporting it.

**TMCV is excluded by the coverage screen** (66 of 1515 sessions, 4.4%). It listed at
the Tata Motors demerger on 2025-11-12; there is no earlier history and none can be
manufactured. `TATAMOTORS` returns "Invalid symbol provided" for *every* historical
range, so splicing is impossible in principle, not merely unwise. TMPV is the renamed
continuing entity and carries history from 2017 — but be clear what that history is:
before the Oct-2025 demerger it is the COMBINED company, after it is passenger
vehicles only. One ticker, two economically different instruments.

### For the older 2026 H1 panel: use 2026-02-16 → 2026-08-19 (125 sessions)

**2026-08-20 is quarantined for 20 of 21 symbols** and must be excluded. Fyers
returns that session TWICE for every symbol — 750 candles for 375 minutes — and the
two copies disagree. HDFCBANK is the sole survivor because its disagreements were
confined to `open`; keeping its extra day would make 2026-08-20 an n=1
cross-section. Excluding the date gives a perfectly rectangular 21 × 125 = 2,625
panel.

This is a vendor defect, not a bug here: a single-day request for 2026-08-20 alone
reproduces the duplication, and no other date in 2026-01-01 → 2026-08-20 is affected.

### Data-quality rules learned the hard way

**Duplicate timestamps are deduplicated, not assumed away.** `dedupe_bars()` keeps
the first copy and *counts* disagreements. If any disagreement touches
`DECISION_FIELDS` (`high`, `low`, `close`, `volume`) the day is quarantined — picking
a winner would silently choose one version of the market. Disagreements confined to
`open` are tolerated because nothing reads `open`:
`test_no_indicator_or_rule_reads_bar_open` pins that premise and will fail if it ever
stops being true.

> Do not generalise from one symbol. RELIANCE's 2026-08-20 conflicts were
> `open`-only, which made the whole date look harmless. Across all 21 symbols the
> conflicts also hit `high`, `low` and `volume`.

**Never request a session that has not closed.** The first run asked for 2026-08-21
at 00:57 that morning and booked 19 EMPTY entries for a session that had not started
— indistinguishable from a holiday or a dead ticker. `last_completed_session()` now
clamps the range.

**Tickers die.** `TATAMOTORS` returns `Invalid symbol provided` (code -300): the Tata
Motors demerger replaced it with **TMPV** and **TMCV**, both carried in
`LIQUID_FNO_UNIVERSE`, both with history back beyond this dataset. `RETIRED_SYMBOLS`
maps dead tickers to successors so a stale config produces an explanation instead of
seven failed requests. Expect more of this — it is the survivorship problem in §1.2
arriving in person.

**An EMPTY day is a question, not a fact.** It can mean holiday, unopened session,
dead ticker, or vendor outage. Investigate before assuming.

**Out-of-session bars are TRIMMED, not fatal.** `trim_to_session()` drops prints
outside 09:15–15:30 and records the count; the day is still validated on what remains.
The validator used to reject the whole day, which cost 136 otherwise-complete
symbol-days across ten dates over a single vendor bar stamped 15:30. A boundary
artifact says nothing about whether 09:15–15:29 is sound.

**The ledger is append-only JSONL.** Rewriting it per symbol-day is quadratic: at
~33,000 settled days and ~6 MB it would be hundreds of GB of writes. `forget()` still
compacts, because forgetting is rare and deliberate.

**`SYMBOL_LISTED_FROM` skips pre-listing ranges.** Requesting TMCV for 2020 burned
four retries with exponential backoff on every chunk — many minutes for a ticker that
did not exist. A symbol that has not listed is not a transient failure.

---

## 8. WHAT THE REAL DATA SAID (2026-08-21)

Full report: `reports/momentum_analysis.md`. Panel 21 × 125 sessions
(2026-02-16 → 2026-08-19), chronological 80/20 split, DEV 100 / HOLDOUT 25 sessions,
2,100 DEV positions. **The holdout is SPENT** (`reports/.holdout_claim.json`,
config `trail=2.0x`).

### The dominant finding

**48% of positions exit on the first completed bar; 83% within 15 minutes.** Mean
gross P&L +Rs 2.39 against Rs 181.45 of costs → net −Rs 179. On a fixed-clock entry
the exit rules never let a position develop, so the aggregate measures the price of a
round trip, not rule quality. Every comparison in that report inherits this.

### Per-rule, on DEV

| rule | n | median hold | mean gross % | mean net Rs | mean MFE % |
|---|---|---|---|---|---|
| MOMENTUM_REVERSAL | 987 (47%) | **0 min** | −0.065 | −246 | 0.023 |
| STOP_HIT (ATR trail) | 604 (29%) | **16 min** | **+0.194** | **+11** | **0.492** |
| VWAP_LOSS | 509 (24%) | 1 min | −0.095 | −275 | 0.037 |

The ATR trail is the only rule with positive gross AND net. Momentum and VWAP fire
almost instantly and both lose.

### Answers to the questions that were asked

1. **Is momentum >70% of exits (trail dead code)?** No — **47%**. The trail sweep also
   moves net P&L by Rs 11,456 across widths, so it is not inert. **The synthetic-data
   suspicion did not replicate.**
2. **Is the 10:52 exit cluster real?** No — an **artifact of the fixture**. Real exits
   cluster in the first 15 minutes after entry.
3. **Is momentum cutting winners?** **No, and this is an adequately-powered null.**
   Forward 60-min return after a momentum exit is −0.009pp, 95% CI [−0.045, +0.027],
   n=987, MDE 0.052pp. Since MDE ≪ the ~0.18pp cost threshold, an economically
   meaningful effect would have been detected. It was not. The rule loses money by
   preventing positions, not by cutting winners.
4. **Holdout:** DEV-best `trail=2.0x` gave −176.30/position on DEV, −200.66 on
   HOLDOUT. **NOT CONFIRMED.** Both are cost-paying, not strategy results.

### What this does NOT license

Entry is a fixed 09:45 clock time, not a signal. **These numbers say nothing about
whether the system makes money**, and nothing about how the exit rules behave on a
position entered for a reason. Re-running this diagnostic after Module B exists will
be a genuinely different experiment — and will need fresh holdout data, since this one
is spent.

Do not tune `momentum_confirmation_bars` or `trail_atr_multiple` on these results. The
confirmation sweep spans 0.9% of the mean and every setting loses money; there is no
signal there to fit.

### Never present synthetic numbers as findings

A distribution, histogram, forward return, or sweep computed from `synthetic_session()`
describes the fixture's construction, not the market — and the output looks exactly
like a real result, which is what makes it dangerous. `scripts/momentum_analysis.py`
refuses to fabricate: an empty store yields a report that says so.

Use synthetic bars to prove machinery runs. Never to answer a question about the
market. If asked for the momentum analysis and the store is empty, say it cannot be
answered yet and name the two commands below.

### Getting real data (needs the user — the 2FA step is manual)

```powershell
python -m src.cli auth              # manual browser login + 2FA
python -m src.cli backfill --months 6
python scripts/momentum_analysis.py
```

`backfill` is resumable via `data/raw/_backfill_ledger.json`; interrupting it is safe.
Bad days are quarantined to `data/quarantine/bars/` with a `reason.json`, never
dropped and never stored as if clean. Check `backfill-status` for quarantined days and
look at them before drawing conclusions from the set.

### Statistical discipline

`src/analysis/momentum.py` enforces it structurally: `Stat` cannot be built without an
`n`, and `Stat.verdict()` returns NO CLAIM POSSIBLE below n=30 and DIRECTIONAL ONLY
below n=200. `minimum_detectable_effect()` gives the smallest effect a sample could
see — a measured effect below its MDE is not a small effect, it is not a measurement.
Report it that way.

`sweep_summary()` deliberately never recommends a value. Nine settings scored on one
sample gives nine in-sample maxima; separating those from real effects needs held-out
data.
