# scripts/ — operational scripts

Run by hand, not imported. Each one should be safe to re-run.

## Planned

- **`refresh_token.py`** — the daily Fyers auth. Prints the auth URL, waits for you
  to paste the redirect URL back after completing 2FA in the browser, exchanges it
  for an access token, caches it in `data/`.

  The 2FA step is **manual and stays manual** — SEBI compliance boundary and
  security boundary both. No TOTP secret is stored, no headless browser, no OTP
  scraping. Do not "improve" this into an automated login.

  Run before pre-open each trading day.

- **`check_session.py`** — pre-flight: is today a trading day, is the holiday list
  verified, is the token valid, is the ban list current. Refuses to green-light a
  session on a holiday rather than returning empty results that look like NO_TRADE.

- **`backfill_history.py`** — pull historical candles into `data/raw/`. Rate-limited,
  resumable, writes raw payloads before parsing.

- **`fetch_bhavcopy.py`**, **`fetch_ban_list.py`**, **`fetch_participant_oi.py`** —
  NSE sources. Polite clients: identify honestly, rate-limit, cache hard.

None of these call a broker order endpoint. None ever will.
