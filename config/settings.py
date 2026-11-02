"""Application settings, loaded from environment / .env via pydantic-settings.

Credentials NEVER live in code. They come from the environment or a gitignored
`.env`, and secrets are typed `SecretStr` so they cannot leak into a repr, a log
line, or a traceback.

Usage:
    from config.settings import get_settings
    settings = get_settings()
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FyersSettings(BaseSettings):
    """Fyers API v3 credentials and paths.

    The access token is regenerated MANUALLY every day before pre-open (SEBI
    requirement). The 2FA step is deliberately not automatable -- see CLAUDE.md
    section 3.2. Nothing here stores a TOTP secret.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="FYERS_",
        extra="ignore",
    )

    app_id: str = Field(description="Fyers app id, e.g. XXXXXXXXXX-100")
    secret: SecretStr = Field(description="Fyers app secret")
    redirect_uri: str = Field(description="OAuth redirect URI registered with Fyers")

    #: Data Socket plan cap. Standard defaults to 100 symbols per user; after a
    #: Prime upgrade, set FYERS_WEBSOCKET_SYMBOL_LIMIT=5000 in .env.
    websocket_symbol_limit: int = Field(default=100, ge=1, le=5000)

    #: Where the daily access token is cached. Outside version control, always.
    token_path: Path = PROJECT_ROOT / "data" / ".fyers_token.json"

    @field_validator("redirect_uri")
    @classmethod
    def _must_be_http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("FYERS_REDIRECT_URI must be an http(s) URL")
        return v


class CostSettings(BaseSettings):
    """Transaction cost model inputs.

    Every P&L figure in this system is reported gross AND net. There is no code path
    that reports gross alone -- see CLAUDE.md section 1.4.

    TODO(VERIFY): every rate below is an approximation and MUST be reconciled against
    the account holder's actual contract notes before any cost-dependent conclusion is
    presented as settled. Rates also change by circular. Options STT is charged on
    PREMIUM, not notional -- a classic and expensive modelling error.

    Reconcile with ``scripts/reconcile_costs.py``, which compares line by line and
    distinguishes a wrong RATE from a wrong ROUNDING RULE from a wrong GST BASE. Do
    not adjust a rate to make a total agree -- a right total from two offsetting
    errors is worse than a visible discrepancy, because it survives review.

    Note the split of responsibility, because it decides what a mismatch means:

      * **Statutory lines** (STT, exchange transaction charge, SEBI turnover fee,
        stamp duty, IPFT, GST) are set by the exchange and the government. They do
        NOT vary by broker, so a mismatch is this model's error.
      * **Brokerage** is a broker's own commercial term. The data feed here is Fyers,
        but the EXECUTION broker need not be, and :func:`src.costs._brokerage`
        hard-codes the Fyers *shape* -- ``min(flat fee, pct of turnover)``. A broker
        that charges a flat fee with no cap, or a percentage with no floor, needs a
        different function, not a different constant.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="COST_",
        extra="ignore",
    )

    rates_verified: bool = Field(
        default=False,
        description="Flip to True only after reconciling against real contract notes.",
    )

    # Brokerage
    brokerage_per_order: Decimal = Decimal("20")
    brokerage_pct_of_turnover: Decimal = Decimal("0.0003")  # whichever is lower
    # Statutory
    stt_equity_intraday_sell: Decimal = Decimal("0.00025")
    stt_futures_sell: Decimal = Decimal("0.0002")
    stt_options_sell_on_premium: Decimal = Decimal("0.001")
    exchange_txn_equity: Decimal = Decimal("0.0000297")
    exchange_txn_futures: Decimal = Decimal("0.0000173")
    exchange_txn_options_on_premium: Decimal = Decimal("0.0003503")
    sebi_turnover_fee: Decimal = Decimal("0.000001")  # Rs 10 per crore
    #: NSE Investor Protection Fund Trust levy, Rs 10 per crore. Not in the original
    #: brief's list; included because omitting a cost understates it, and understating
    #: cost is the dangerous direction. Set to 0 to disable.
    ipft_charge: Decimal = Decimal("0.000001")
    gst_rate: Decimal = Decimal("0.18")  # on brokerage + exchange + SEBI + IPFT
    stamp_duty_equity_buy: Decimal = Decimal("0.00003")
    stamp_duty_futures_buy: Decimal = Decimal("0.00002")
    stamp_duty_options_buy: Decimal = Decimal("0.00003")

    #: Slippage is MODELLED, never assumed zero. Conservative default in basis points
    #: per leg; a spread-aware model should replace this once quote data is stored.
    slippage_bps_per_leg: Decimal = Decimal("5")

    # ---- DELIVERY (CNC) equity: a multi-day hold is NOT intraday ------------
    # A position carried overnight settles as delivery, and the statutory charges are
    # materially different -- in the EXPENSIVE direction. Zero brokerage does not mean
    # zero cost.
    #
    # STT is the whole story: delivery pays 0.1% on BOTH legs (0.2% round trip) where
    # intraday pays 0.025% on the sell alone. That is 8x, and it swamps whatever
    # brokerage is saved.
    #
    # TODO(VERIFY): as with the intraday rates, these are approximations and must be
    # reconciled against real contract notes before any net figure is trusted.
    #: Delivery STT, charged on BOTH buy and sell.
    stt_delivery_each_side: Decimal = Decimal("0.001")
    #: Stamp duty on the buy leg, delivery rate (higher than intraday's 0.003%).
    stamp_duty_delivery_buy: Decimal = Decimal("0.00015")
    #: Depository participant charge per scrip per sell, a flat rupee amount rather
    #: than a percentage -- so it hurts small positions disproportionately.
    dp_charge_per_sell: Decimal = Decimal("13.50")
    #: Brokerage per delivery order.
    #: PROVENANCE: a WEB SEARCH, not a contract note and not a statement from the
    #: account holder. An earlier comment here claimed the account holder supplied it;
    #: that was wrong. A broker's advertised "zero delivery brokerage" routinely comes
    #: with a platform or DP fee that a marketing page omits and a contract note shows.
    #: Treat this as the least trustworthy number in this class. The note wins.
    brokerage_per_delivery_order: Decimal = Decimal("0")


class ExitEngineSettings(BaseSettings):
    """Module C exit-engine thresholds.

    Every number the state machine keys off lives here so it can be tuned from .env
    without touching code. Changing any of these changes historical replay results --
    record which config a replay ran under before comparing two runs.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="EXIT_",
        extra="ignore",
    )

    #: Profit in ATR multiples at which the stop is pulled up to breakeven.
    breakeven_atr_multiple: Decimal = Decimal("1.0")
    #: Once breakeven is armed, trail this many ATRs below the running session high.
    trail_atr_multiple: Decimal = Decimal("1.5")
    #: Target 1, in ATR multiples above entry, when no explicit target was recorded.
    t1_atr_multiple: Decimal = Decimal("1.5")
    #: Fraction of the position to book at Target 1.
    partial_exit_fraction: Decimal = Decimal("0.5")

    #: TIME_EXIT: minutes held before the decay test is applied at all.
    time_exit_minutes: int = 90
    #: TIME_EXIT: gross unrealised P&L must be inside +/- this percent.
    time_exit_pnl_pct_band: Decimal = Decimal("0.3")
    #: TIME_EXIT: price must be within this many ATRs of entry.
    time_exit_atr_multiple: Decimal = Decimal("0.5")

    #: Seconds without a tick before the feed is declared stale and advice suppressed.
    stale_feed_seconds: Decimal = Decimal("30")
    #: Volume ratio above which a VWAP loss counts as "on above-average volume".
    vwap_loss_volume_ratio: Decimal = Decimal("1.0")
    #: RSI level below which momentum is treated as having reversed.
    rsi_reversal_level: Decimal = Decimal("50")
    #: Consecutive completed bars the momentum condition must hold before EXIT_NOW
    #: fires. 1 = the original single-bar rule. Higher values demand persistence and
    #: cut whipsaw exits at the cost of responsiveness.
    #:
    #: Which value is right is EMPIRICAL -- see reports/momentum_analysis.md. Do not
    #: change this on intuition; change it on a swept result over real bars.
    momentum_confirmation_bars: int = 1

    #: Bars of history required before any price-based advice is emitted. ATR(14) and
    #: RSI(14) both need 15 bars; below this the engine says so rather than guessing.
    min_bars_for_advice: int = 15


class SignalEngineSettings(BaseSettings):
    """Module B thresholds and component weights.

    These values are DEFAULTS, not tuned parameters. They were chosen to be
    defensible before any evaluation was run, and they have not been fitted to any
    result. Tuning them requires the DEV/HOLDOUT protocol in src/analysis/split.py --
    changing one because it improved a number is exactly how a strategy gets fitted
    to a sample. See CLAUDE.md section 2.1.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="SIGNAL_",
        extra="ignore",
    )

    # ---- when the engine runs ---------------------------------------------
    #: First evaluation, once the 15-minute opening range has fully formed.
    first_signal_time: str = "09:30"
    #: Last evaluation. Past this there is too little session left for a target to be
    #: reached before the 15:15 square-off.
    last_signal_time: str = "14:30"
    #: Re-evaluate every this many minutes between those two.
    interval_minutes: int = 5

    # ---- how many ----------------------------------------------------------
    #: A CEILING, never a quota. Zero candidates is a normal, expected result.
    max_candidates: int = 2
    #: Composite score a candidate must clear. Below this, nothing is surfaced.
    #:
    #: DERIVED 2026-08-21, not swept: the 90th percentile of 4,891 observed composite
    #: scores on DEV. The previous 0.35 sat at the 74th percentile and let a quarter of
    #: the universe through, which with 20 symbols and 61 evaluations filled the
    #: 2-candidate budget on the first evaluation of almost every session.
    #: See reports/stop_calibration.md.
    min_composite_score: Decimal = Decimal("0.594")

    # ---- component weights (sum is normalised, so relative size is what matters) --
    weight_or_breakout: Decimal = Decimal("2.0")
    weight_or_hold: Decimal = Decimal("1.0")
    weight_ema_trend: Decimal = Decimal("1.5")
    weight_vwap_trend: Decimal = Decimal("1.5")
    weight_vwap_persistence: Decimal = Decimal("1.0")
    weight_rsi: Decimal = Decimal("1.5")
    weight_roc: Decimal = Decimal("1.0")
    weight_volume: Decimal = Decimal("2.0")
    weight_relative_strength: Decimal = Decimal("2.0")

    # ---- component shaping --------------------------------------------------
    #: Breakout of this many ATRs past the opening range scores full marks.
    breakout_atr_full: Decimal = Decimal("1.0")
    #: Consecutive bars holding the breakout for full marks.
    hold_bars_full: int = 3
    #: EMA9-over-EMA21 gap, in percent, that scores full marks.
    ema_gap_full_pct: Decimal = Decimal("0.30")
    #: Price-over-VWAP gap, in percent, that scores full marks.
    vwap_gap_full_pct: Decimal = Decimal("0.40")
    #: Consecutive bars above VWAP for full marks.
    vwap_bars_full: int = 5
    #: RSI band. Full marks in the middle; overbought is penalised, not rewarded --
    #: buying strength is not the same as buying exhaustion.
    rsi_ideal_low: Decimal = Decimal("55")
    rsi_ideal_high: Decimal = Decimal("68")
    rsi_overbought: Decimal = Decimal("78")
    #: Rate of change, in percent over the lookback, for full marks.
    roc_lookback_bars: int = 10
    roc_full_pct: Decimal = Decimal("0.50")
    #: Relative volume for full marks. 1.0 is an ordinary minute for that time of day.
    relative_volume_full: Decimal = Decimal("2.0")
    #: Trailing sessions used to build the time-of-day volume baseline.
    volume_profile_lookback: int = 20
    #: Outperformance of the panel median, in percentage points, for full marks.
    relative_strength_full_pct: Decimal = Decimal("0.50")

    # ---- levels --------------------------------------------------------------
    #: Entry zone half-width, in ATRs, around the last completed close.
    entry_zone_atr: Decimal = Decimal("0.25")

    # ---- stop and targets: DERIVED 2026-08-21, not swept --------------------
    #: Initial stop, in ATRs below the entry.
    #:
    #: The 75th percentile of the adverse excursion suffered by positions that went on
    #: to reach T1, measured over 40,105 such positions on DEV. Three quarters of
    #: winners never touch it. The previous 1.0 sat at 1.02x the MEAN adverse
    #: excursion -- inside the noise floor, so it exited on chatter regardless of
    #: whether the signal was any good. That was a design error, not a bad setting,
    #: and it is fixed by construction rather than by search.
    #: See reports/stop_calibration.md.
    stop_atr_multiple: Decimal = Decimal("2.37")
    #: Targets, in ATRs above the entry. Re-derived from the stop so the declared
    #: reward-to-risk (1.5 to T1, 3.0 to T2) is preserved exactly.
    #: Reachability was checked, not assumed: 54.7% of entries reach T1 and 27.9%
    #: reach T2 within the holding window, against a median favourable excursion of
    #: 4.04 ATR. A target beyond what the stock typically moves would mean the R:R is
    #: unachievable and the trade should not be taken at all.
    t1_atr_multiple: Decimal = Decimal("3.56")
    t2_atr_multiple: Decimal = Decimal("7.11")
    #: Minimum reward-to-risk against T1. Below this the trade is not worth the stop.
    min_risk_reward: Decimal = Decimal("1.2")


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Safety tripwire ---------------------------------------------------
    #: This system is ADVISORY ONLY and never places, modifies, or cancels an order.
    #: This flag exists as a tripwire, NOT as a feature switch. It is validated to
    #: reject True. Do not add a code path that would place an order if it flipped.
    allow_order_placement: bool = False

    # ---- Environment -------------------------------------------------------
    env: str = "local"
    log_level: str = "INFO"

    # ---- Paths -------------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"
    reference_dir: Path = PROJECT_ROOT / "data" / "reference"
    journal_dir: Path = PROJECT_ROOT / "data" / "journal"

    # ---- API ---------------------------------------------------------------
    api_host: str = "127.0.0.1"  # personal use: never bind 0.0.0.0
    api_port: int = 8000

    # ---- Risk defaults (Module A overrides these per session) --------------
    default_daily_target_pct: Decimal = Decimal("1.0")
    default_max_daily_loss_pct: Decimal = Decimal("1.0")
    #: Module B surfaces UP TO this many candidates. Zero is a valid, normal result;
    #: this is a ceiling, never a quota to fill. See CLAUDE.md section 1.5.
    max_candidates_per_day: int = 2

    @field_validator("allow_order_placement")
    @classmethod
    def _never_allow_orders(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "ALLOW_ORDER_PLACEMENT must be False. This system is advisory only "
                "and never places, modifies, or cancels orders. If you believe you "
                "need this, stop and re-read CLAUDE.md section 1.1."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached application settings."""
    return Settings()


@lru_cache(maxsize=1)
def get_fyers_settings() -> FyersSettings:
    """Cached Fyers credentials. Raises if .env is missing required keys."""
    return FyersSettings()


@lru_cache(maxsize=1)
def get_cost_settings() -> CostSettings:
    """Cached cost model inputs."""
    return CostSettings()


@lru_cache(maxsize=1)
def get_exit_engine_settings() -> ExitEngineSettings:
    """Cached exit-engine thresholds."""
    return ExitEngineSettings()


@lru_cache(maxsize=1)
def get_signal_engine_settings() -> SignalEngineSettings:
    """Cached signal-engine thresholds and weights."""
    return SignalEngineSettings()
