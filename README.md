# fno-advisor

Personal intraday **decision-support** dashboard for Indian NSE F&O stocks.

> **Advisory only.** This system never places, modifies, or cancels an order. It
> reads market data and suggests; you execute manually in your broker app.
> Read [CLAUDE.md](CLAUDE.md) for the full rule set.

---

## Status

**Built:** market calendar, intraday + delivery cost models, Module C (Position Tracker
& Exit Advisory Engine) and its **web dashboard**, the Fyers live feed, Fyers OAuth,
resumable historical backfill, batch replay with MAE/MFE, the momentum diagnostic
pipeline, and Module B (Signal Engine — evaluated, and it does not earn its place).
**1575 tests pass** (`python -m pytest -q`, 2026-08-27, 338s, zero skips, exit 0).

**Data on disk:** 2020-01-01 → 2026-08-19. **32,932 symbol-days, 12.34M one-minute
bars**, 80 quarantined, 7 empty. Calendar tape-verified for 2020–2026.

**Analysis panel:** `PANEL_FULL_HISTORY` — 20 symbols × 1515 sessions, DEV
2020-01-01..2024-11-26 (1212), HOLDOUT 2024-11-27..2026-02-13 (303). **Holdout
unspent.** TMCV excluded (4.4% coverage — listed at the Tata Motors demerger).
Prices are split/bonus-adjusted: percentage returns are right, absolute historical
prices and share counts are not.

**Momentum analysis complete** — `reports/momentum_analysis.md`. Chronological 80/20
DEV/HOLDOUT split; **the holdout is spent**. Headline: 48% of positions exit on the
first bar after entry, so the exit rules never let a position develop on a fixed-clock
entry. The momentum rule is *not* cutting winners (adequately-powered null), and the
trail is *not* dead code (47% momentum share, sweep is not inert) — the synthetic-data
suspicion did not replicate. See CLAUDE.md §8.

**Use 2026-02-16 → 2026-08-19 (125 sessions) for analysis.** 2026-08-20 is
quarantined: Fyers returns that session twice for every symbol and the copies
disagree on `high`/`low`/`volume`. Excluding it gives a rectangular 21 × 125 panel.

**Module B was built and evaluated, and it failed.** Its feature set carries no edge
at any tested horizon — intraday continuous, intraday single-shot, 1-day, 3-day and
5-day. Gross excess over a naive comparator never clears zero, before costs. See
CLAUDE.md §2.3. **The dashboard therefore has no signal panel**, on purpose: showing a
scored ranking that failed evaluation would present a known-negative result as a
recommendation.

**Repository audit: `reports/audit_2026-08-27.md`.** A read-only, 20-section survey of
the codebase as it stood on 2026-08-27 — architecture, entry points, auth and token
handling, data/live/replay flows, staleness and forming-bar handling, features, the
(absent) model code, validation, costs, routes, frontend flow, existing protections,
weak points, and which files should and must not be changed. It records the test run
that preceded it and classifies every finding. Read it before planning work; it is the
inventory the phased plan is built on. Findings closed since: the credential-shaped
value in `.env.example`, and token-invalid handling (both below). Findings still open
include the unrun cost reconciliation, the stubbed F&O ban gate, and the absence of
version control.

**Module A (Morning Setup) is implemented and available from the CLI** —
`src/morning/setup.py`, 66 module tests plus 41 CLI tests:

```powershell
python -m src.cli morning --capital 100000 --target-pct 1.0 --max-loss-pct 1.0
python -m src.cli morning --capital 100000 --date 2026-08-20 --at 08:30
python -m src.cli morning --capital 100000 --json
```

`--target-pct` and `--max-loss-pct` fall back to config; `--capital` is **required and
never defaulted**. `--date` plans another session, `--at` fixes the clock so the answer
is deterministic, and `--json` emits the same result as a machine-readable payload.

It answers the questions that come before any position exists: is today a session,
where in it are we, what capital is available, what one round trip costs against that
capital, and whether the configured target survives that cost. Pure arithmetic over the
NSE calendar and the shared cost model.

**Advisory planning only.** It **does not predict returns, does not identify profitable
trades, and does not rank or even name a symbol** — there is no symbol, candidate,
ranking or score anywhere in it. `POSSIBLE` means only that the arithmetic does not
reject the plan; it is explicitly *not* a probability, and the output says so on the
same line. Nothing it prints is an order instruction, and no order is placed.
**Cost rates remain unverified**, so every feasibility figure is provisional and `READY`
is unreachable until `COST_RATES_VERIFIED=true`. Not yet wired to the dashboard or API.

The dashboard includes a transparent, descriptive intraday price/volume board (up to
20 symbols per direction), completed-candle structure charts, NIFTY/BANKNIFTY context,
sector participation and breadth map, a price-structure alert feed, and a decision
journal with setup/habit tags, net results by tag, and a P&L calendar. Rankings use
completed one-minute bars after skipping the first five opening bars when present;
mechanical zones use completed five-minute bars by default. A zone requires a close
through a confirmed swing, the last opposite-colour candle, and displacement proven by
a three-candle gap or a close at least one prior average candle range beyond the zone.
The dashboard reports retests and close invalidation. The board excludes OI because
the intraday candle feed does not provide it. Its zones and pivots are price
references, not verified institutional orders,
trade recommendations, or projected stop/target levels. VWAP is estimated from candle
typical prices and volume. The chart popup adds a four-part descriptive context check
(board, sector, breadth, and VWAP), not a signal. The playbook includes a user-entered
position-size and gross R:R calculator; the journal records a manual plan and compares
an optional daily loss cap with recorded closed fills only. Neither tool blocks or
places orders. No option-chain or Greeks feed is available.
Run `python -m src.cli refresh-universe` to download, validate, and save the dated
FYERS NSE F&O symbol master. Default LIVE scans use that snapshot while it is no more
than seven days old, otherwise they fall back to the maintained 21-name liquid subset
plus the two indices. The UI reports its actual coverage. Net journal P&L is
provisional until the cost model is reconciled against contract notes. ML remains
behind the gate in CLAUDE.md §2.1.

The live Fyers feed (`FyersLiveFeed`) has **50 tests**, all driving injected fakes. It
seeds history before subscribing, backfills gaps after a reconnect, and reports
staleness per symbol.

**Real-exchange status, stated precisely.** First live run: **2026-08-22, a Saturday,
with the market shut.** That is what makes the record below lopsided, and the
lopsidedness is the point.

| Verified against Fyers (2026-08-22) | Still UNVERIFIED — fakes only |
|---|---|
| Websocket connects to the real endpoint | **No real price tick has ever been parsed.** Zero ticks arrived; `parse_tick` has still only seen injected data |
| Authentication succeeds | Bar aggregation from live ticks |
| Subscription accepted for all 6 symbols | Staleness detection against a real feed going quiet |
| Control frames arrive in the expected shapes | `reconnect()` / `backfill_gap()` against a real disconnection |
| `seed_history()` classifies a closed market correctly | `seed_history()` with actual bars present |

**The tick path remains unverified, and must be described that way.** Do not compress
this into "the live feed has been tested against the real exchange" — that sentence
reads as covering the tick path, and the tick path is exactly what has not been tested.
A Monday session moves a row; it never assumes one. See CLAUDE.md §2.2a.

---

## Getting real data

The 2FA step is manual by design. Nothing automates it, and nothing will.

```powershell
# 1. Authenticate. Prints a URL; you log in and complete 2FA in a browser,
#    then paste the redirect URL back.
python -m src.cli auth
python -m src.cli auth --status      # is my token still good today?

# 2. Backfill 1-minute bars. RESUMABLE -- interrupting it is safe.
python -m src.cli backfill --months 6
python -m src.cli backfill-status    # what has been settled, what is quarantined

# 3. Run the diagnostic
python scripts/momentum_analysis.py
```

The token is valid only on the IST day it was generated — SEBI requires daily
regeneration, and that is enforced locally rather than trusted to the broker. Run
`auth` before pre-open each trading day.

Backfill defaults to 20 liquid F&O names over 6 months (~2,500 symbol-days). Days
failing validation — duplicate timestamps, gaps >5 min inside the session, OHLC
violations, negative volume — are **quarantined** to `data/quarantine/bars/` with a
`reason.json` beside them. Never dropped, never stored as if clean. Look at them.

## The momentum question

`reports/momentum_analysis.md` answers: **is the momentum exit rule cutting winners
early?** It measures the 60-minute forward return after every momentum exit. A
reliably positive value means the rule sold into continued upside and is costing
money.

It also reports the exit-rule distribution, exit-time histogram, and sweeps of
`trail_atr_multiple` and `momentum_confirmation_bars`.

Statistical discipline is enforced in code, not left to prose: a `Stat` cannot exist
without its `n`, verdicts read NO CLAIM POSSIBLE below n=30 and DIRECTIONAL ONLY below
n=200, and every null result is reported against the smallest effect the sample could
have detected. The sweep summary deliberately never says "use this value" — nine
settings scored on one sample is nine in-sample maxima.

---

## The dashboard

```powershell
# replay a stored session at 120x -- a whole day in under three minutes
python -m src.cli dashboard --date 2026-08-19 --speed 120
python -m src.cli dashboard --date 2026-08-19 --symbols RELIANCE,INFY

# live, only after the token has been refreshed for the day
python -m src.cli dashboard --mode live
```

Then open <http://127.0.0.1:8000>. Binds loopback only, and there is no
authentication on the page — do not bind it to a routable interface.

Mode, date and speed are **CLI flags**. Nothing reads them from the environment or
from config, and `--mode` defaults to `replay`: an env var that could flip a
dashboard to LIVE is a way to be looking at real money while believing you are in
replay. `python -m src.cli dashboard --help` lists the flags. Replay refuses to start
without a `--date` rather than serving a blank page that looks like a quiet market.

**It also refuses a port that is already serving**, naming the occupant's trade date.
Otherwise a leftover dashboard keeps the port, the new launch fails to bind, and the
browser shows the *older* session — different date, different positions — with
nothing on the page admitting it.

`scripts/serve_dashboard.py --replay <date>` / `--live` still works and is the same
code — both front doors call `src.api.serve.serve()`.

**REPLAY is the default and LIVE must be asked for explicitly.** The header says which
one you are looking at in a colour used nowhere else on the page — `▶ REPLAY — NOT
LIVE` versus `● LIVE`. Replaying 2024 while believing you are watching today is the
expensive mistake, so it is made hard to make.

What is on the page: one card per open position, each with its state, the reason
string in full, the price *with its age in seconds*, the stop, and gross **and** net
P&L side by side. A form to record a position you already bought. A `check-entry` box
that answers "should I have bought this" and records nothing. A bulk entry check
across every subscribed symbol. A feed-health strip.

**The bulk check is a filter, not a ranking.** It answers one question per symbol —
"is this entry obviously unsound right now?" — independently for each, and lists the
answers alphabetically. There is no score, no rank, no highlighted row, and no "best"
candidate; `SOUND` means only *not obviously unsound*, never *this will go up*. The
panel says so on screen. Module B ranked this universe and produced no edge at any
horizon tested, so a panel that started implying which symbol to prefer would be that
failed engine wearing a different hat. Tests enforce the shape: a recursive scan for
banned keys, an ordering assertion over out-of-order symbols with mixed verdicts, and
browser tests reading the rendered DOM.

- `STALE_DATA` and `FORCE_EXIT` pulse. Nothing else on the page moves, so motion means
  exactly one thing, and 15:15 square-off is unmissable from across a desk.
- No price is ever shown without its age. No gross figure is ever shown without net.
- Nothing is behind a hover, and no state lives in the browser — refreshing cannot
  resurrect a stale trailing stop.

**There is no signal panel, and that is deliberate.** Module B was evaluated and
failed at every horizon tested; a ranked list of "which stock will go up" would be a
known-negative result dressed as a recommendation. The page says so where the panel
would have been. See CLAUDE.md §2.4.

---

## Using Module C from the CLI

```powershell
# ask BEFORE you buy -- records nothing, exit 0 = sound, 2 = unsound
python -m src.cli check-entry --symbol RELIANCE
python -m src.cli check-entry --symbol TATASTEEL --date 2026-02-13 --at 13:30

# record a buy you already made in your broker app
python -m src.cli add-position --symbol RELIANCE --qty 100 --price 1018.00

# see what the engine would have said across a session
python -m src.cli replay --position-id RELIANCE-ab12cd34 --demo

# print advice as each 1-minute bar completes
python -m src.cli watch --replay-demo

# record what you actually did
python -m src.cli book-partial  --position-id RELIANCE-ab12cd34 --qty 50
python -m src.cli close-position --position-id RELIANCE-ab12cd34 --price 1031.40

# DELETE records (test entries, mistakes). Lists them and exits 1 without --confirm.
python -m src.cli clear-positions --date 2026-08-19
python -m src.cli clear-positions --date 2026-08-19 --confirm
python -m src.cli clear-positions --date 2026-08-19 --position-id RELIANCE-ab12cd34 --confirm

# what has the engine been saying?
python -m src.cli journal
python -m src.cli session      # trading day? how long to square-off?
```

`--demo` / `--replay-demo` drive a deterministic **synthetic** session so the loop can
be seen working before any real bars exist. It is a fixture, not market data — never
judge a rule by it.

`book-partial` and `close-position` **record** fills you already made. They do not
place anything. Nothing here does.

`clear-positions` is the only destructive verb: it **deletes the record**, where
`close-position` keeps it with an exit attached. It also drops the position's runtime
state so a cleared position cannot leave a trailing stop behind.

Recording an **exact** duplicate of an open position — same symbol, quantity, entry
price and entry time — is rejected (HTTP 409 from the API). Two identical cards look
like one position rendered twice, and the difference is whether you hold 100 shares or
200. Pass `allow_duplicate` if you really did fill twice at the same price and second.

### The six states

| State | Meaning |
|---|---|
| `HOLD` | trend intact, nothing to do |
| `TRAIL` | in profit past 1× ATR; stop ratcheted up, never down |
| `PARTIAL_EXIT` | Target 1 hit — book 50%, trail the rest (fires once) |
| `EXIT_NOW` | stop hit / VWAP lost on above-average volume / momentum reversed |
| `TIME_EXIT` | 90 min with the price going nowhere — the edge decayed |
| `FORCE_EXIT` | 15:15 IST square-off. Unconditional, beats everything |

Plus two states that are not exit advice:

`STALE_DATA` — the feed has gone quiet for >30s, so price-based advice is
**suppressed** and the last-known P&L is labelled rather than presented as current.
`FORCE_EXIT` still fires through a stale feed — the square-off deadline is a clock
fact and needs no price.

### An invalid token suppresses advice too — and it is NOT a state

An expired or missing broker token used to be *reported* in the header and never
*enforced*: the token was checked once at construction, so one dying mid-session left
advice flowing until the socket dropped and staleness fired ~30s later. It failed
closed indirectly, through a different mechanism than the one that knew the truth.

Suppression now happens at the **runtime boundary**, not in the state machine:

- **`PositionState` is unchanged** and there is no `TOKEN_INVALID` state. The exit
  engine stays credential-agnostic and pure — a test parses it with `ast`, strips every
  docstring, and asserts the remaining code never mentions a token at all.
- The token is **re-read before every evaluation**, deliberately not cached. A
  remembered bundle stays valid for its whole IST day, so a token deleted, replaced or
  revoked out of band would go unnoticed until midnight *while the dashboard kept
  advising*.
- While invalid: `evaluate()` is never called, **nothing is persisted** (the trail, the
  breakeven flag and the last state stay exactly as the last authorised evaluation left
  them), and the card shows that preserved state explicitly marked `NOT CURRENT`.
- **`FORCE_EXIT` still fires**, for the same reason it survives a stale feed. Its P&L is
  labelled stale or absent — never presented as live.
- `/health` carries a typed `feed_health` block: `token_valid`, `advice_suppressed`,
  `reason` (`TOKEN_INVALID`), `detail`. **`TOKEN_INVALID` is kept distinct from
  `STALE_DATA`** — a quiet socket and a lapsed credential have different causes and
  different fixes, and telling you to restart a feed that is working wastes the minutes
  that matter.
- On screen: a loud, **static** `ACCESS TOKEN INVALID — PRICE-BASED ADVICE IS
  SUPPRESSED` banner. Static on purpose; motion stays reserved for `STALE_DATA` and
  `FORCE_EXIT`.
- Recovery needs no restart — run `python -m src.cli auth` in another terminal and the
  next evaluation picks the new token up.

**REPLAY never reads a token**, and a test injects an exploding loader into a replay
runtime to prove the path is never taken.

`INVALID_ENTRY` — the exit condition was **already true when you bought**. Fires once,
at the first completed bar after entry, when momentum was already reversed, price was
already below session VWAP, or price was already through the stop. It reports that the
position should not exist; it is **not** a stop-loss and suppresses nothing — normal
advice resumes on the very next bar, because you may hold anyway and still need exit
guidance. Real example, ONGC on 2026-02-13:

```
13:31  INVALID_ENTRY  This position should not have been opened. Entry was unsound --
                      EMA9 268.30 was already below EMA21 268.54 and RSI14 was already
                      31.5 (below 50) -- momentum was against this trade before it was
                      opened. Diagnostic only -- this is not an exit instruction.
13:32  PARTIAL_EXIT   Target 1 reached: last 269.10 at or above T1 268.54...
13:33  TRAIL          In profit and trailing: session high 269.10, stop 268.66...
```

Every state carries a reason string with the actual numbers in it, so you can
disagree with it:

```
[10:52:00 IST] RELIANCE  EXIT_NOW
  reason : Momentum reversal: EMA9 1032.16 below EMA21 1036.02 and RSI14 31.4 below 50.
  price  : 1023.20
  stop   : 1018.94
  P&L    : unrealised: gross +520.00 (+0.51%) | costs 185.51 | net +334.49 (+0.33%)
  action : sell 100 (you, manually)
```

### Replay is a requirement, not a nicety

`replay_session()` reads no clock and no network, so the same bars plus the same
config produce byte-identical advice every run. Test it against past data before
trusting it live. It shows *what the engine would have said and when* — it is not a
backtest of profitability, which would need the entry rule and real fills too.

---

## Setup (Windows 11)

### 1. Python 3.11

This project targets **Python 3.11**. This machine currently has only **3.14**, which
will not work: several pinned dependencies have no cp314 wheels. `pydantic-core` in
particular falls back to compiling from Rust source and fails on `link.exe not found`
unless you have the Visual Studio C++ build tools. Install 3.11 rather than fighting
that.

Check what you have:

```powershell
py -0p
```

If 3.11 is not listed, install it:

```powershell
winget install Python.Python.3.11
```

Then re-open PowerShell and confirm `py -3.11 --version` prints `3.11.x`.

### 2. Virtual environment

From the project root (`C:\laragon\www\fno-advisor`):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 3. Dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Note `tzdata` in the list: Windows has no system IANA timezone database, so stdlib
`zoneinfo` cannot resolve `Asia/Kolkata` without it. Nothing imports it by name —
don't prune it as unused.

### 4. Credentials

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in `FYERS_APP_ID`, `FYERS_SECRET`, `FYERS_REDIRECT_URI` from
<https://myapi.fyers.in/dashboard>. The redirect URI in `.env` must match the one
registered in the Fyers app **exactly**, including the trailing path.

`.env` is gitignored. **Credentials never go in code.**

### 5. Run the tests

```powershell
pytest
```

1575 tests, zero skips. A selection of what they are actually pinning down:

| File | Covers |
|---|---|
| `test_calendar.py` | IST offset (guards the pytz `+05:53` LMT trap), holidays, session boundaries to the second, fail-closed on unknown years |
| `test_costs.py` | which charge lands on which leg, both legs always charged, net always worse than gross |
| `test_indicators.py` | hand-computable known values for each indicator, insufficient-history → `None`, the forming-bar exclusion |
| `test_exit_engine.py` | the state machine — stop never moves down, FORCE_EXIT overrides everything, stale feed suppresses advice, TIME_EXIT boundaries |
| `test_store.py` | restart survival, Decimal exactness, IST-aware round trip, DuckDB queries |
| `test_replay.py` | replay determinism, gap → staleness, bar aggregation never leaks the forming bar |
| `test_cli.py` | every verb, and that none of them sounds like or reaches an order API |
| `test_fyers_auth.py` | token never leaks into a repr/log, expiry enforced locally, each failure mode distinguishable, and no 2FA-automation capability exists |
| `test_backfill.py` | resumability after a crash, quarantine of bad days, rate limiting and backoff, request logging |
| `test_batch_replay.py` | MAE/MFE excursions exclude the exit bar, forward-return censoring, sweep machinery, and the `Stat` discipline layer |
| `test_momentum_report.py` | an empty store yields a no-data report, never fabricated findings |
| `test_lookahead.py` | the leak harness itself — that poisoning the future actually *does* change a leaky function's output, so a passing causal check means something |
| `test_live_feed.py` | history seeded before the socket opens, reconnect refetches the gap and merges over the partial bar, staleness is per-symbol, bars close on the clock and not on tick arrival |
| `test_api.py` | the replay feed satisfies the live feed's interface, replay runs on a virtual clock and never reveals a forming bar, every price carries an age, every P&L carries gross and net, no route reaches an order API, invalid-token suppression (re-read per evaluation, nothing persisted, FORCE_EXIT survives, distinct from STALE_DATA), and **no route and no part of the UI returns a signal, ranking or confidence score** |
| `test_feed_semantics.py` | `last_tick_at` means one thing in both modes — REPLAY and LIVE reach the same staleness verdict and the same `PositionState` on equivalent inputs, with the residual stored bars cannot express measured and bounded |
| `test_env_example.py` | `.env.example` carries no pasted auth URL, credential parameter, JWT, app-id shape or secret-shaped value; verified by injection, and a failure never prints the value it found |
| `test_web_render.py` | the page actually RENDERS in headless Chrome (not a substring match on the served HTML), caveats are collapsed but reachable, no ranking survives to the DOM, and the TOKEN INVALID banner is loud but not animated |

### 6. Git

Git is not installed on this machine. When you want version control:

```powershell
winget install Git.Git
```

Then re-open PowerShell and run `git init`. `.gitignore` is already in place, so
`.env` and `data/` are protected from the first commit.

---

## Daily routine

The Fyers access token expires daily and must be regenerated **before pre-open**
(SEBI requirement).

The 2FA step is **manual by design** — there is no TOTP secret stored anywhere and
no attempt to automate the login. `python -m src.cli auth` prints an auth URL, waits
for you to paste the redirect URL back, and caches the token in `data/`.

If any command fails with a token error, the fix is always: run the refresh, then
retry. Nothing retries a login automatically. The dashboard's health strip shows
token status in LIVE mode so you find out before the open, not at 09:20.

`--replay` needs no token at all, which is the other reason it is the default.

---

## Layout

```
CLAUDE.md          Rules every session inherits. Read this first.
config/            settings.py -- pydantic settings, secrets from .env
src/
  cli.py           <- CLI: check-entry, add-position, watch, replay, journal, session
  costs.py         <- intraday AND delivery cost models (gross AND net, both legs)
  market/
    calendar.py    <- NSE calendar, session times, square-off. Tape-verified 2020-2026
  positions/       <- Module C, BUILT
    models.py        Position, PositionRuntime, Bar, ExitAdvice, PositionState
    indicators.py    VWAP (session-anchored), ATR14, EMA9/21, RSI14, volume ratio
    exit_engine.py   the state machine -- pure, replay-driveable
    store.py         Parquet + DuckDB persistence, survives restart
    feed.py          ReplayFeed, MultiSymbolReplayFeed, BarAggregator
    live_feed.py     FyersLiveFeed -- injected socket/history; TICK PATH unverified live
    replay.py        historical session driver + batch replay
  analysis/
    momentum.py    <- exit diagnostics, forward returns, sweeps, power analysis
    lookahead.py   <- the leak harness: truncation + poisoning. Built before features
    split.py       <- DEV/HOLDOUT panels, one-shot HoldoutGuard, spent regions
  data/
    fyers_auth.py  <- OAuth + daily token cache; 2FA is MANUAL
    backfill.py    <- resumable 1-min backfill, validation, quarantine
  morning/         Module A -- Morning Setup  (planned)
  signals/         Module B -- Signal Engine. BUILT AND EVALUATED; carries no edge
  journal/         Module D -- manual decisions + exit-advice snapshots (partial)
  api/
    runtime.py     <- SessionRuntime; REPLAY and LIVE differ only in now()
    app.py         <- FastAPI, binds 127.0.0.1. No signals endpoint, deliberately
data/              Parquet store, gitignored, never committed
  quarantine/      bars that failed validation, with reasons. Inspect, do not ignore.
reports/           audit_2026-08-27.md, momentum_analysis.md, signal_evaluation.md,
                   entry_check_diagnostic.md, swing/fno evaluations, batch replay CSVs
                   .holdout_claim*.json -- which panels are spent. Do not edit.
scripts/           serve_dashboard.py + analysis and operational scripts
tests/             pytest suite, mirrors src/
web/
  index.html       <- the dashboard. React 18 UMD + Tailwind CDN, no build step
```

---

## Known unverified data

1. **Transaction cost rates** in `config/settings.py`. Approximations. Reconcile
   against your real Fyers contract notes and set `COST_RATES_VERIFIED=true`.
   No cost-dependent conclusion is trustworthy until then. It fails loudly rather
   than quietly — please keep it that way.

2. **The live Fyers feed's TICK PATH has never run against the real exchange.** Its 50
   tests drive injected fakes. The connection path — socket, auth, subscription,
   control frames, closed-market seeding — was verified on 2026-08-22, but the market
   was shut and **zero price ticks arrived**, so tick parsing, bar aggregation from
   live ticks, real-feed staleness and real reconnect/backfill are all still unverified.
   Watch a full session before trusting `--live`. See the table under **Status**.

3. **Transaction-cost reconciliation has not been run.** `scripts/reconcile_costs.py`
   exists; there is no reconciliation report in `reports/`. This is the same gap as (1),
   restated as an action.

**Resolved:** the NSE holiday calendar is no longer model-recalled. 2020–2026 are
derived from the exchange tape (`TAPE_VERIFIED_THROUGH` records how far each year is
covered), which turned up four errors in the original 2026 list plus weekend full
sessions and weekday muhurat days. See CLAUDE.md §4.0.

---

## Non-negotiables

- No order placement, ever. No broker order API is called.
- No look-ahead: every feature computable at or before its decision timestamp.
- All datetimes tz-aware IST (`Asia/Kolkata`), never naive.
- Every gross P&L is reported with its net beside it.
- `NO_TRADE` / `NO_SIGNAL` is always a valid output. There is no signal quota.
- A result that failed evaluation is not shown to the user as a recommendation. That
  is why the dashboard has no signal panel.
