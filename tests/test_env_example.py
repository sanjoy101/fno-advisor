"""``.env.example`` must stay a file of placeholders.

Why this exists
---------------

A real Fyers client id and a pasted ``generate-authcode`` URL were once appended to the
bottom of ``.env.example`` and sat there unnoticed. The file's entire purpose is to be
committable, so anything real in it is exposed by design, and nothing was checking.

CLAUDE.md 3.3 says credentials never appear in code, tests, logs, or docs. This is the
check for the one file most likely to break that rule, because it is the file people
paste into while getting auth working.

Two rules this file follows itself
----------------------------------

**It never prints a matched value.** A failure reports the line number and the name of
the rule that fired, nothing else. An assertion that echoes the credential into the test
output has copied it somewhere new -- a terminal, a CI log, a bug report -- which is the
opposite of the point.

**It never reads the real ``.env``.** That file legitimately holds live values. It is
not scanned, not opened, and not asserted about; ``test_the_real_dotenv_is_never_read``
pins that this module names only the example.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

#: (rule name, compiled pattern, what it catches). Case-sensitive where the shape is
#: genuinely case-bearing, so a Tailwind class like `bg-fuchsia-950` cannot masquerade
#: as a Fyers app id -- a case-insensitive first draft of this scan produced 40 false
#: positives in web/index.html.
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "pasted-url-with-query",
        re.compile(r"https?://\S*\?\S*"),
        "a URL carrying a query string -- an authorisation URL pasted back in",
    ),
    (
        "credential-parameter",
        re.compile(r"(?i)\b(client_id|auth_code|authcode|access_token|refresh_token"
                   r"|appIdHash|secret_key|api_key)\s*="),
        "a credential-bearing parameter name",
    ),
    (
        "jwt-shaped",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
        "a JWT-shaped value -- an access token",
    ),
    (
        "fyers-app-id-shape",
        re.compile(r"\b[A-Z0-9]{6,}-\d{3}\b"),
        "a Fyers app-id shape such as ABCD123456-100",
    ),
)

#: A value is an obvious placeholder if it starts with one of these, or is empty.
PLACEHOLDER_PREFIXES = ("YOUR_", "<", "CHANGE", "REPLACE", "TODO", "...")

#: Keys whose value must ALWAYS be an obvious placeholder, whatever its length.
SECRET_KEY_WORDS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "APIKEY", "PRIVATE")

#: Above this, a non-placeholder value stops looking like config and starts looking
#: like a key. Query-less URLs are exempt: a redirect URI is legitimately long.
MAX_PLAIN_VALUE_LEN = 20


def is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("\"'")
    if not stripped:
        return True
    upper = stripped.upper()
    if upper.startswith(PLACEHOLDER_PREFIXES):
        return True
    # All-X / all-underscore masks such as XXXXXXXXXX are placeholders too.
    return bool(re.fullmatch(r"[Xx_\-<>.]+", stripped))


def is_queryless_url(value: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s?]+", value.strip()))


def scan(text: str) -> list[tuple[int, str, str]]:
    """(line number, rule name, description) for every credential-shaped hit.

    Returns locations and rule names. **Never returns the matched text** -- callers
    render this straight into an assertion message.
    """
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern, description in RULES:
            if pattern.search(line):
                findings.append((lineno, name, description))

        key, sep, value = line.partition("=")
        if not sep or line.lstrip().startswith("#"):
            continue
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        if any(word in key.upper() for word in SECRET_KEY_WORDS):
            if not is_placeholder(value):
                findings.append((
                    lineno, "secret-key-not-placeholder",
                    f"{key} must be an obvious placeholder such as YOUR_{key}",
                ))
        elif (len(value) > MAX_PLAIN_VALUE_LEN
                and not is_placeholder(value)
                and not is_queryless_url(value)):
            findings.append((
                lineno, "long-opaque-value",
                f"{key} holds a long non-placeholder value that may be a credential",
            ))
    return findings


def describe(findings) -> str:
    """A failure message carrying locations and rules only -- never the value."""
    lines = [f"  .env.example:{n}  [{rule}]  {why}" for n, rule, why in findings]
    return (
        f"{len(findings)} credential-shaped value(s) in .env.example:\n"
        + "\n".join(lines)
        + "\n\nThe matched text is deliberately NOT shown: printing it would copy the "
          "credential into the test output. Open the file at those line numbers, "
          "replace the value with a placeholder, and if it was ever real, rotate it."
    )


# --------------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------------


def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE} is missing"


def test_env_example_contains_nothing_credential_shaped():
    findings = scan(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert not findings, describe(findings)


def test_the_three_fyers_keys_are_present_and_are_placeholders():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1) for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    for key in ("FYERS_APP_ID", "FYERS_SECRET"):
        assert key in values, f"{key} is missing from .env.example"
        assert is_placeholder(values[key]), f"{key} is not an obvious placeholder"

    redirect = values.get("FYERS_REDIRECT_URI", "")
    assert redirect.startswith(("http://", "https://")), redirect
    assert "?" not in redirect, "the example redirect URI must carry no query string"


def test_no_totp_or_pin_secret_is_ever_added():
    """The 2FA step is manual and stays manual -- CLAUDE.md 3.2."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8").upper()
    for banned in ("TOTP_SECRET", "FYERS_PIN", "FYERS_TOTP", "FYERS_PASSWORD"):
        assert banned not in text, (
            f"{banned} would let something automate the login. That is a SEBI "
            f"compliance boundary and a security boundary.")


# --------------------------------------------------------------------------------
# Verified by injection. A scanner that cannot fail is not a check.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_line, expected_rule",
    [
        # The exact SHAPE that was in this file, with a synthetic id -- reproducing the
        # real one here would just move the credential into the test suite.
        ("https://api-t1.fyers.in/api/v3/generate-authcode?client_id=ABCD123456-100"
         "&redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fcallback&response_type=code",
         "pasted-url-with-query"),
        ("FYERS_APP_ID=ABCD123456-100", "fyers-app-id-shape"),
        ("FYERS_SECRET=k7Qw2mZp9Lr4Nc8Vt1Bx6Yd3Hs5Ja0Ge", "secret-key-not-placeholder"),
        ("FYERS_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaaaaaaaaaaaaaaaaaa",
         "jwt-shaped"),
        ("SOME_URL=https://example.com/cb?client_id=abc", "credential-parameter"),
        ("MY_VALUE=0123456789abcdef0123456789abcdef", "long-opaque-value"),
    ],
)
def test_the_scanner_catches_each_shape(bad_line, expected_rule):
    findings = scan(bad_line)
    assert expected_rule in {rule for _, rule, _ in findings}, (
        f"the scanner missed {expected_rule}: got {[r for _, r, _ in findings]}")


def test_the_scanner_catches_a_bad_line_appended_to_the_real_file():
    """End to end: the file passes, and the same file with one bad line appended fails."""
    clean = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not scan(clean)

    poisoned = clean + "\n https://api-t1.fyers.in/api/v3/generate-authcode?client_id=ZZZZ999999-100&state=None\n"
    findings = scan(poisoned)
    assert findings, "appending a pasted auth URL did not fail the scan"
    assert findings[0][0] == len(clean.splitlines()) + 2, "wrong line number reported"


def test_the_scanner_accepts_the_legitimate_placeholders():
    good = "\n".join([
        "# Create an app at https://myapi.fyers.in/dashboard to obtain these.",
        "FYERS_APP_ID=YOUR_FYERS_APP_ID",
        "FYERS_SECRET=YOUR_FYERS_SECRET",
        "FYERS_REDIRECT_URI=http://127.0.0.1:8080/callback",
        "# API_PORT=8000",
        "ENV=local",
    ])
    assert not scan(good), scan(good)


def test_a_failure_message_never_contains_the_matched_value():
    """The rule this module holds itself to."""
    secret = "k7Qw2mZp9Lr4Nc8Vt1Bx6Yd3Hs5Ja0Ge"
    message = describe(scan(f"FYERS_SECRET={secret}"))
    assert secret not in message, "the failure message leaked the value it found"
    assert "deliberately NOT shown" in message


# --------------------------------------------------------------------------------
# The real .env is never touched
# --------------------------------------------------------------------------------


def test_the_real_dotenv_is_never_read():
    """This module must name only the example file.

    The real .env legitimately holds live values. Reading it here would pull them into
    the test process and, on a failure, potentially into the output.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    assert '".env.example"' in source or "'.env.example'" in source
    assert not re.search(r'["\']\.env["\']', source), (
        "this module references the real .env -- it must only ever read .env.example")
