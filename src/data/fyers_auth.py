"""Fyers API v3 OAuth: token generation, caching, and expiry.

**The 2FA step is manual by design and stays manual.** No TOTP secret is stored, no
headless browser is driven, no OTP is scraped. This is both a SEBI compliance boundary
and a security boundary: automating it would mean holding a credential that can log
into a live trading account unattended. :meth:`FyersAuth.interactive_login` prints an
authorisation URL, waits for a human to complete login and 2FA in a browser, and takes
the redirect URL back by paste.

**Read-only.** This module obtains a token. It never calls an order endpoint, and the
token it produces is used only for quotes and historical candles.

**Failure is loud and specific.** A missing token, an expired token, and a corrupt
cache raise three different exceptions with three different instructions. Nothing here
retries a login -- an automatic retry loop against a broker's auth endpoint is how an
account gets locked.

Token lifetime
--------------
SEBI requires the access token to be regenerated daily. This module treats a token as
valid only on the IST calendar date it was generated, which enforces that rule locally
rather than trusting the broker to reject a stale one. A token made at 08:30 works all
day; the next morning it is expired and ``auth`` must be run again before pre-open.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from config.settings import FyersSettings
from src.market.calendar import IST, ensure_ist, now_ist

__all__ = [
    "TokenBundle",
    "FyersAuth",
    "AuthError",
    "TokenMissingError",
    "TokenExpiredError",
    "TokenCorruptError",
    "AuthCodeError",
    "extract_auth_code",
]


class AuthError(RuntimeError):
    """Base for every auth failure. Never caught-and-retried inside this module."""


class TokenMissingError(AuthError):
    """No token has been generated yet."""


class TokenExpiredError(AuthError):
    """The cached token is past its validity date and must be regenerated."""


class TokenCorruptError(AuthError):
    """The token cache exists but cannot be read."""


class AuthCodeError(AuthError):
    """The pasted redirect URL did not contain a usable auth code."""


@dataclass(frozen=True)
class TokenBundle:
    """A generated access token plus the metadata needed to know when it dies.

    ``__repr__`` and ``__str__`` are redacted. A token in a traceback or a log line is
    a live credential leaked to wherever that text ends up.
    """

    access_token: str
    app_id: str
    generated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", ensure_ist(self.generated_at))
        object.__setattr__(self, "expires_at", ensure_ist(self.expires_at))

    def __repr__(self) -> str:
        return (
            f"TokenBundle(app_id={self.app_id!r}, access_token='<redacted {len(self.access_token)} "
            f"chars>', generated_at={self.generated_at.isoformat()}, "
            f"expires_at={self.expires_at.isoformat()})"
        )

    __str__ = __repr__

    def is_valid(self, now: datetime | None = None) -> bool:
        """Valid only on its own IST generation date, and only before its expiry.

        The date check is what enforces daily regeneration. A token that the broker
        might still technically honour at 06:00 the next morning is treated as dead
        here, because SEBI expects a fresh one before pre-open.
        """
        now = ensure_ist(now or now_ist())
        return now.date() == self.generated_at.date() and now < self.expires_at

    def seconds_remaining(self, now: datetime | None = None) -> float:
        now = ensure_ist(now or now_ist())
        return max(0.0, (self.expires_at - now).total_seconds())

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "app_id": self.app_id,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TokenBundle":
        try:
            return cls(
                access_token=raw["access_token"],
                app_id=raw["app_id"],
                generated_at=datetime.fromisoformat(raw["generated_at"]),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenCorruptError(f"token cache is not readable: {exc}") from exc


class SessionLike(Protocol):
    """The slice of ``fyersModel.SessionModel`` this module uses."""

    def generate_authcode(self) -> str: ...
    def set_token(self, auth_code: str) -> None: ...
    def generate_token(self) -> dict: ...


def extract_auth_code(redirect_url: str) -> str:
    """Pull the auth code out of the URL the browser was redirected to.

    Accepts the whole pasted URL, or a bare code. Fyers returns ``?auth_code=...&s=ok``;
    an error comes back as ``?s=error&message=...``, which is surfaced rather than
    letting an empty code fail later with something unrelated.

    Raises:
        AuthCodeError: if the URL carries an error, or has no auth code in it.
    """
    value = redirect_url.strip().strip('"').strip("'")
    if not value:
        raise AuthCodeError("nothing pasted -- paste the full redirect URL from the browser")

    if "?" not in value and "://" not in value:
        return value  # a bare auth code

    query = parse_qs(urlparse(value).query)

    if query.get("s", [""])[0] == "error" or "error" in query:
        message = (
            query.get("message", [""])[0]
            or query.get("error_description", [""])[0]
            or query.get("error", [""])[0]
            or "unknown error"
        )
        raise AuthCodeError(
            f"Fyers returned an error instead of an auth code: {message}. "
            f"Check the app id, secret, and that the redirect URI registered with "
            f"Fyers matches FYERS_REDIRECT_URI exactly."
        )

    for key in ("auth_code", "authcode", "code"):
        if query.get(key, [""])[0]:
            return query[key][0]

    raise AuthCodeError(
        f"no auth_code in the pasted URL (query keys: {sorted(query) or 'none'}). "
        f"Paste the FULL URL from the browser address bar after login completes, "
        f"including everything after the '?'."
    )


class FyersAuth:
    """Generates, caches, and validates the daily Fyers access token."""

    def __init__(
        self,
        settings: FyersSettings,
        *,
        session_factory: Callable[[], SessionLike] | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._session_factory = session_factory
        self.token_path = Path(settings.token_path)

    # ---- session ---------------------------------------------------------------

    def _session(self) -> SessionLike:
        """Build the SDK session. Imported lazily so the rest of the app runs without it."""
        if self._session_factory is not None:
            return self._session_factory()
        try:
            from fyers_apiv3 import fyersModel  # noqa: PLC0415 - optional dependency
        except ImportError as exc:  # pragma: no cover - depends on install
            raise AuthError(
                "fyers_apiv3 is not installed. Run: pip install -r requirements.txt "
                "(it needs Python 3.11 -- there are no wheels for 3.13/3.14)."
            ) from exc
        return fyersModel.SessionModel(
            client_id=self.settings.app_id,
            secret_key=self.settings.secret.get_secret_value(),
            redirect_uri=self.settings.redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )

    def auth_url(self) -> str:
        """The URL the human opens in a browser to log in and complete 2FA."""
        return self._session().generate_authcode()

    def exchange(self, auth_code: str) -> TokenBundle:
        """Trade an auth code for an access token.

        Raises:
            AuthError: if the broker declines. Not retried -- a loop against an auth
                endpoint gets accounts locked.
        """
        session = self._session()
        session.set_token(auth_code)
        response = session.generate_token()

        token = (response or {}).get("access_token")
        if not token:
            code = (response or {}).get("code", "?")
            message = (response or {}).get("message", "no message")
            raise AuthError(
                f"Fyers did not return an access token (code {code}): {message}. "
                f"The auth code may have already been used or expired -- they are "
                f"single-use and short-lived. Run auth again for a fresh one."
            )

        generated = ensure_ist(self.clock())
        return TokenBundle(
            access_token=token,
            app_id=self.settings.app_id,
            generated_at=generated,
            expires_at=self.expiry_for(generated),
        )

    @staticmethod
    def expiry_for(generated_at: datetime) -> datetime:
        """End of the IST day the token was generated on.

        Deliberately conservative and local. Fyers may honour a token slightly past
        this; we do not, because the next session needs a fresh one anyway and an
        ambiguous token at 09:14 is worse than a definite failure at 08:00.
        """
        return datetime.combine(generated_at.date(), time(23, 59, 59), tzinfo=IST)

    # ---- cache -----------------------------------------------------------------

    def save(self, bundle: TokenBundle) -> Path:
        """Write the token to its cache file, owner-readable only where supported."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(bundle.to_dict(), indent=2), encoding="utf-8")
        try:
            os.chmod(self.token_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - Windows ACLs differ
            # Windows largely ignores POSIX modes. The file still lives under data/,
            # which is gitignored in full, so it cannot be committed by accident.
            pass
        return self.token_path

    def load(self) -> TokenBundle:
        """Load the cached token, or raise a specific, actionable error.

        Raises:
            TokenMissingError: never generated.
            TokenCorruptError: unreadable cache.
            TokenExpiredError: past its date.
        """
        if not self.token_path.exists():
            raise TokenMissingError(
                f"no Fyers token at {self.token_path}. Run:\n"
                f"    python -m src.cli auth\n"
                f"The 2FA step is manual -- you will be asked to log in in a browser."
            )
        try:
            raw = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TokenCorruptError(
                f"token cache at {self.token_path} is unreadable ({exc}). "
                f"Delete it and run: python -m src.cli auth"
            ) from exc

        bundle = TokenBundle.from_dict(raw)
        now = ensure_ist(self.clock())
        if not bundle.is_valid(now):
            raise TokenExpiredError(
                f"Fyers token expired. Generated {bundle.generated_at:%Y-%m-%d %H:%M} "
                f"IST, valid only that day; it is now {now:%Y-%m-%d %H:%M} IST. "
                f"SEBI requires daily regeneration. Run:\n"
                f"    python -m src.cli auth\n"
                f"Do this BEFORE pre-open (09:00 IST), not during the session."
            )
        if bundle.app_id != self.settings.app_id:
            raise TokenCorruptError(
                f"cached token belongs to app {bundle.app_id!r} but settings say "
                f"{self.settings.app_id!r}. Delete {self.token_path} and re-auth."
            )
        return bundle

    def load_if_valid(self) -> TokenBundle | None:
        """The cached token, or None if there isn't a usable one. Never raises."""
        try:
            return self.load()
        except AuthError:
            return None

    def status(self) -> str:
        """A one-line description of the token state, safe to print."""
        try:
            bundle = self.load()
        except AuthError as exc:
            return f"NO VALID TOKEN: {type(exc).__name__}: {str(exc).splitlines()[0]}"
        mins = bundle.seconds_remaining(self.clock()) / 60
        return (
            f"Token OK for app {bundle.app_id}, generated "
            f"{bundle.generated_at:%Y-%m-%d %H:%M} IST, {mins:.0f} min remaining."
        )

    # ---- the manual flow -------------------------------------------------------

    def interactive_login(
        self,
        *,
        prompt: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
    ) -> TokenBundle:
        """Run the manual OAuth + 2FA flow and cache the resulting token.

        Deliberately blocking and human-driven. ``prompt`` and ``out`` are injected so
        the flow can be tested without a terminal -- NOT so it can be automated. Do not
        wire a TOTP generator into ``prompt``.
        """
        url = self.auth_url()
        out("")
        out("=" * 72)
        out("  FYERS LOGIN -- the 2FA step is MANUAL by design")
        out("=" * 72)
        out("")
        out("  1. Open this URL in your browser:")
        out("")
        out(f"     {url}")
        out("")
        out("  2. Log in with your Fyers ID, then complete 2FA (PIN / OTP).")
        out("     This step is not automated and will not be. It is a SEBI")
        out("     compliance boundary and a security boundary.")
        out("")
        out("  3. The browser will redirect to your registered redirect URI.")
        out("     The page may fail to load -- that is fine and expected.")
        out("     Copy the FULL URL from the address bar, including everything")
        out("     after the '?'.")
        out("")
        pasted = prompt("  4. Paste the redirect URL here: ")

        auth_code = extract_auth_code(pasted)
        bundle = self.exchange(auth_code)
        path = self.save(bundle)

        out("")
        out(f"  Token saved to {path}")
        out(f"  Valid until {bundle.expires_at:%Y-%m-%d %H:%M:%S} IST "
            f"({bundle.seconds_remaining(self.clock()) / 3600:.1f} h)")
        out("  It dies at the end of today. Run this again tomorrow before pre-open.")
        out("")
        return bundle


def require_token(
    settings: FyersSettings,
    *,
    clock: Callable[[], datetime] = now_ist,
) -> TokenBundle:
    """A valid token, or a loud specific failure. The entry point for data code.

    Never retries and never silently falls back to an expired token. Callers should
    let the exception propagate to the user with its instructions intact.
    """
    return FyersAuth(settings, clock=clock).load()
