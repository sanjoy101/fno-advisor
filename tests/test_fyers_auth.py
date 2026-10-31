"""Tests for Fyers OAuth token handling.

Three things must hold: the token never leaks into a repr or log, expiry is enforced
locally so daily regeneration actually happens, and every failure mode raises its own
exception with its own instructions rather than a generic one.

The SDK session is faked throughout -- no network, no credentials, no 2FA. There is
deliberately no test that automates the 2FA step, because that capability must not
exist.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from config.settings import FyersSettings
from src.data.fyers_auth import (
    AuthCodeError,
    AuthError,
    FyersAuth,
    TokenBundle,
    TokenCorruptError,
    TokenExpiredError,
    TokenMissingError,
    extract_auth_code,
    require_token,
)
from src.market.calendar import IST
from tests.conftest import ist

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake-token-value"


@pytest.fixture
def settings(tmp_path) -> FyersSettings:
    return FyersSettings(
        app_id="ABCD1234-100",
        secret=SecretStr("shhh-secret"),
        redirect_uri="http://127.0.0.1:8000/fyers/callback",
        token_path=tmp_path / ".fyers_token.json",
    )


class FakeSession:
    """Stands in for fyersModel.SessionModel."""

    def __init__(self, response=None, url="https://api.fyers.in/api/v3/generate-authcode?x=1"):
        self.response = response if response is not None else {"access_token": TOKEN}
        self.url = url
        self.auth_code: str | None = None

    def generate_authcode(self) -> str:
        return self.url

    def set_token(self, auth_code: str) -> None:
        self.auth_code = auth_code

    def generate_token(self) -> dict:
        return self.response


def make_auth(settings, session=None, now=None) -> FyersAuth:
    session = session or FakeSession()
    clock = (lambda: now) if now else (lambda: ist(8, 30))
    return FyersAuth(settings, session_factory=lambda: session, clock=clock)


# --------------------------------------------------------------------------------
# extract_auth_code
# --------------------------------------------------------------------------------


def test_extracts_auth_code_from_a_full_redirect_url():
    url = "http://127.0.0.1:8000/fyers/callback?s=ok&code=200&auth_code=ABC123&state=None"
    assert extract_auth_code(url) == "ABC123"


def test_accepts_a_bare_auth_code():
    assert extract_auth_code("ABC123") == "ABC123"


def test_strips_whitespace_and_quotes():
    assert extract_auth_code('  "ABC123"  ') == "ABC123"


@pytest.mark.parametrize("key", ["auth_code", "authcode", "code"])
def test_accepts_the_alternative_parameter_names(key):
    assert extract_auth_code(f"http://x/cb?{key}=ZZZ") == "ZZZ"


def test_surfaces_a_broker_error_rather_than_failing_later():
    url = "http://127.0.0.1:8000/fyers/callback?s=error&message=invalid+app+id"
    with pytest.raises(AuthCodeError, match="invalid app id"):
        extract_auth_code(url)


def test_error_mentions_the_redirect_uri_mismatch_as_a_likely_cause():
    with pytest.raises(AuthCodeError, match="redirect URI"):
        extract_auth_code("http://x/cb?s=error&message=bad")


def test_missing_auth_code_says_to_paste_the_full_url():
    with pytest.raises(AuthCodeError, match="FULL URL"):
        extract_auth_code("http://127.0.0.1:8000/fyers/callback?s=ok&state=None")


def test_empty_paste_is_rejected():
    with pytest.raises(AuthCodeError, match="nothing pasted"):
        extract_auth_code("   ")


# --------------------------------------------------------------------------------
# Secrecy
# --------------------------------------------------------------------------------


def test_token_is_redacted_in_repr():
    bundle = TokenBundle(TOKEN, "APP-100", ist(8, 30), ist(23, 59, 59))
    assert TOKEN not in repr(bundle)
    assert "redacted" in repr(bundle)


def test_token_is_redacted_in_str():
    bundle = TokenBundle(TOKEN, "APP-100", ist(8, 30), ist(23, 59, 59))
    assert TOKEN not in str(bundle)
    assert TOKEN not in f"{bundle}"


def test_status_line_never_contains_the_token(settings):
    auth = make_auth(settings)
    auth.save(auth.exchange("CODE"))
    assert TOKEN not in auth.status()


def test_secret_is_not_in_settings_repr(settings):
    assert "shhh-secret" not in repr(settings)


# --------------------------------------------------------------------------------
# Expiry -- enforcing daily regeneration
# --------------------------------------------------------------------------------


def test_token_is_valid_on_its_generation_day():
    bundle = TokenBundle(TOKEN, "A", ist(8, 30), FyersAuth.expiry_for(ist(8, 30)))
    assert bundle.is_valid(ist(15, 0)) is True


def test_token_is_invalid_the_next_morning():
    """SEBI wants a fresh token daily; a leftover must not be used."""
    generated = ist(8, 30)
    bundle = TokenBundle(TOKEN, "A", generated, FyersAuth.expiry_for(generated))
    assert bundle.is_valid(generated + timedelta(days=1)) is False


def test_token_is_invalid_past_its_expiry_instant():
    generated = ist(8, 30)
    bundle = TokenBundle(TOKEN, "A", generated, generated + timedelta(hours=1))
    assert bundle.is_valid(generated + timedelta(hours=2)) is False


def test_expiry_is_end_of_the_generation_day():
    expiry = FyersAuth.expiry_for(ist(8, 30))
    assert (expiry.hour, expiry.minute, expiry.second) == (23, 59, 59)
    assert expiry.date() == ist(8, 30).date()


def test_seconds_remaining_never_goes_negative():
    generated = ist(8, 30)
    bundle = TokenBundle(TOKEN, "A", generated, generated + timedelta(hours=1))
    assert bundle.seconds_remaining(generated + timedelta(hours=5)) == 0.0


def test_bundle_rejects_naive_datetimes():
    from src.market.calendar import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        TokenBundle(TOKEN, "A", datetime(2026, 8, 20, 8, 30), ist(23, 59))


# --------------------------------------------------------------------------------
# Exchange
# --------------------------------------------------------------------------------


def test_exchange_returns_a_bundle(settings):
    session = FakeSession()
    bundle = make_auth(settings, session).exchange("ABC123")
    assert bundle.access_token == TOKEN
    assert bundle.app_id == "ABCD1234-100"
    assert session.auth_code == "ABC123"


def test_exchange_raises_specifically_when_no_token_comes_back(settings):
    session = FakeSession(response={"code": -413, "message": "invalid auth code"})
    with pytest.raises(AuthError, match="invalid auth code"):
        make_auth(settings, session).exchange("STALE")


def test_exchange_failure_explains_that_auth_codes_are_single_use(settings):
    session = FakeSession(response={"code": -413, "message": "nope"})
    with pytest.raises(AuthError, match="single-use"):
        make_auth(settings, session).exchange("STALE")


def test_exchange_handles_an_empty_response(settings):
    with pytest.raises(AuthError):
        make_auth(settings, FakeSession(response={})).exchange("X")


def test_auth_url_comes_from_the_session(settings):
    session = FakeSession(url="https://example.test/login")
    assert make_auth(settings, session).auth_url() == "https://example.test/login"


# --------------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------------


def test_token_round_trips_through_the_cache(settings):
    auth = make_auth(settings)
    saved = auth.exchange("X")
    auth.save(saved)
    assert make_auth(settings).load() == saved


def test_cache_survives_a_restart(settings):
    make_auth(settings).save(make_auth(settings).exchange("X"))
    fresh = FyersAuth(settings, session_factory=lambda: FakeSession(), clock=lambda: ist(14, 0))
    assert fresh.load().access_token == TOKEN


def test_missing_token_names_the_command_to_run(settings):
    with pytest.raises(TokenMissingError, match="python -m src.cli auth"):
        make_auth(settings).load()


def test_expired_token_names_the_command_and_the_deadline(settings):
    auth = make_auth(settings)
    auth.save(auth.exchange("X"))
    tomorrow = FyersAuth(
        settings,
        session_factory=lambda: FakeSession(),
        clock=lambda: ist(8, 30) + timedelta(days=1),
    )
    with pytest.raises(TokenExpiredError) as exc:
        tomorrow.load()
    assert "python -m src.cli auth" in str(exc.value)
    assert "pre-open" in str(exc.value)


def test_corrupt_cache_raises_its_own_error(settings):
    Path(settings.token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.token_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(TokenCorruptError, match="unreadable"):
        make_auth(settings).load()


def test_cache_missing_a_field_raises_corrupt_not_missing(settings):
    Path(settings.token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.token_path).write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
    with pytest.raises(TokenCorruptError):
        make_auth(settings).load()


def test_token_for_a_different_app_is_rejected(settings, tmp_path):
    other = settings.model_copy(update={"app_id": "OTHER-100"})
    auth_other = make_auth(other)
    auth_other.save(auth_other.exchange("X"))
    with pytest.raises(TokenCorruptError, match="belongs to app"):
        make_auth(settings).load()


def test_the_three_failure_modes_are_distinguishable():
    """Each needs a different response from the user, so each gets its own type."""
    assert TokenMissingError is not TokenExpiredError is not TokenCorruptError
    for cls in (TokenMissingError, TokenExpiredError, TokenCorruptError, AuthCodeError):
        assert issubclass(cls, AuthError)


def test_load_if_valid_returns_none_instead_of_raising(settings):
    assert make_auth(settings).load_if_valid() is None


def test_load_if_valid_returns_the_bundle_when_good(settings):
    auth = make_auth(settings)
    auth.save(auth.exchange("X"))
    assert make_auth(settings).load_if_valid() is not None


def test_require_token_propagates_the_failure(settings):
    with pytest.raises(TokenMissingError):
        require_token(settings, clock=lambda: ist(8, 30))


def test_status_reports_no_token_without_raising(settings):
    assert "NO VALID TOKEN" in make_auth(settings).status()


def test_status_reports_time_remaining(settings):
    auth = make_auth(settings)
    auth.save(auth.exchange("X"))
    assert "min remaining" in make_auth(settings).status()


# --------------------------------------------------------------------------------
# The manual flow
# --------------------------------------------------------------------------------


def test_interactive_login_prints_the_url_and_takes_a_paste(settings):
    session = FakeSession(url="https://example.test/login")
    lines: list[str] = []
    bundle = make_auth(settings, session).interactive_login(
        prompt=lambda _: "http://127.0.0.1:8000/fyers/callback?auth_code=ABC&s=ok",
        out=lines.append,
    )
    text = "\n".join(lines)
    assert "https://example.test/login" in text
    assert bundle.access_token == TOKEN
    assert session.auth_code == "ABC"


def test_interactive_login_states_that_2fa_is_manual(settings):
    lines: list[str] = []
    make_auth(settings).interactive_login(prompt=lambda _: "auth_code_x", out=lines.append)
    text = "\n".join(lines).lower()
    assert "manual" in text
    assert "not automated" in text


def test_interactive_login_warns_the_redirect_page_may_fail_to_load(settings):
    lines: list[str] = []
    make_auth(settings).interactive_login(prompt=lambda _: "X", out=lines.append)
    assert "fail to load" in "\n".join(lines)


def test_interactive_login_saves_the_token(settings):
    make_auth(settings).interactive_login(prompt=lambda _: "X", out=lambda _: None)
    assert Path(settings.token_path).exists()


def test_interactive_login_never_prints_the_token(settings):
    lines: list[str] = []
    make_auth(settings).interactive_login(prompt=lambda _: "X", out=lines.append)
    assert TOKEN not in "\n".join(lines)


def test_interactive_login_says_to_rerun_tomorrow(settings):
    lines: list[str] = []
    make_auth(settings).interactive_login(prompt=lambda _: "X", out=lines.append)
    assert "tomorrow" in "\n".join(lines)


def test_bad_paste_fails_without_calling_the_broker(settings):
    session = FakeSession()
    with pytest.raises(AuthCodeError):
        make_auth(settings, session).interactive_login(
            prompt=lambda _: "http://cb?s=error&message=denied", out=lambda _: None
        )
    assert session.auth_code is None


# --------------------------------------------------------------------------------
# No automation of 2FA
# --------------------------------------------------------------------------------


def _code_identifiers(module) -> set[str]:
    """Every name used in real CODE -- imports, attributes, identifiers, literals.

    Docstrings and comments are excluded on purpose: this module's prose says "no TOTP
    secret is stored", and a naive substring scan would flag its own promise.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Skip docstrings; keep other string literals (e.g. a URL or a key name).
            if not _is_docstring(tree, node):
                names.add(node.value)
    return {n.lower() for n in names}


def _is_docstring(tree, node) -> bool:
    import ast

    for parent in ast.walk(tree):
        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(parent, "body", [])
            if body and isinstance(body[0], ast.Expr) and body[0].value is node:
                return True
    return False


def test_module_stores_no_totp_or_pin_credential():
    """The capability to log in unattended must not exist."""
    import src.data.fyers_auth as mod

    identifiers = _code_identifiers(mod)
    for token in ("totp", "pyotp", "selenium", "playwright", "webdriver", "otp_secret"):
        offenders = [n for n in identifiers if token in n]
        assert not offenders, f"{token} would automate the 2FA step: {offenders}"


def test_module_calls_no_order_api():
    import src.data.fyers_auth as mod

    identifiers = _code_identifiers(mod)
    for token in ("place_order", "modify_order", "cancel_order", "exit_positions"):
        assert not [n for n in identifiers if token in n]


def test_no_retry_loop_around_exchange(settings):
    """One attempt only. Retrying against an auth endpoint locks accounts."""
    calls = {"n": 0}

    class CountingSession(FakeSession):
        def generate_token(self):
            calls["n"] += 1
            return {"code": -413, "message": "bad"}

    with pytest.raises(AuthError):
        make_auth(settings, CountingSession()).exchange("X")
    assert calls["n"] == 1
