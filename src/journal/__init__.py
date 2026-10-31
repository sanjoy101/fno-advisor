"""Module D -- Journal.  Build order: after C, A, B.

Logs EVERY signal -- taken and skipped -- plus the outcome.

Skipped signals matter as much as taken ones: without them there is no way to tell
whether the engine has edge or whether the user's discretion does. Record the
as-of timestamp, the feature values as they stood at decision time, the advice
given, what the user actually did, and the realised outcome gross AND net.

Append-only. A correction is a new row with a reason, never an in-place edit --
otherwise the historical record changes under every backtest.
"""
