"""Data access: broker client, Parquet store, DuckDB queries, external sources.

Planned:
    fyers_client.py -- Fyers API v3 wrapper. READ-ONLY surface only: auth, profile,
        funds, positions, quotes, depth, history, symbol master. Order endpoints are
        never wrapped, not even unused (CLAUDE.md 1.1).
    auth.py       -- daily token lifecycle. The 2FA step is MANUAL and stays manual.
                     Token expiry fails loudly with "run the daily refresh"; never retries.
    store.py      -- Parquet read/write, partitioned by date + symbol. Timestamps are
                     persisted tz-aware UTC and converted to IST on read. Raw data is
                     append-only and immutable.
    queries.py    -- DuckDB over the Parquet tree. No database server, ever.
    bhavcopy.py   -- NSE daily bhavcopy. Publishes after the close on day D; available
                     for decisions on D+1 only.
    participant_oi.py -- participant-wise OI scrape. Publishes with a lag; same rule.

Scrapers: identify honestly, rate-limit, cache hard, persist the raw payload before
parsing so a parser fix can be replayed without re-fetching.
"""
