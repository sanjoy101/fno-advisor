# data/ — local Parquet store

Gitignored in its entirety. Nothing here is ever committed.

```
raw/         As fetched from the source. Append-only and IMMUTABLE.
             A correction is a NEW file plus a recorded reason, never an
             in-place edit -- otherwise backtests silently change underneath you.
processed/   Derived features and resampled bars. Regenerable from raw/.
reference/   Holiday lists, F&O ban lists, and the F&O universe AS-OF each date
             (stored per-date to avoid survivorship bias).
journal/     Module D: every signal taken and skipped, plus outcomes.
```

Partitioning: by **date + symbol**. Queried with DuckDB directly over the Parquet
tree. There is no database server and none should be introduced.

Timestamps are persisted **tz-aware UTC** and converted to IST on read — Parquet
round-trips UTC reliably, local-zone round-tripping is implementation-dependent.

The cached Fyers access token also lands here (`.fyers_token.json`). It is a
secret: never commit it, never log it, never paste it into an issue.
