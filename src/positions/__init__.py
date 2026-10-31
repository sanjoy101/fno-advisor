"""Module C -- Position Tracker.  *** BUILD THIS FIRST. ***

The user enters their manual buys; the system advises when to exit. Rule-based,
zero ML, useful on day one.

It must handle these states honestly:
  * approaching the 15:15 intraday square-off
  * the symbol has gone untradeable (circuit limit hit) -- say so plainly rather
    than repeating an exit suggestion that cannot be acted on
  * stale or missing quote data -- never infer a price

Every P&L it reports is gross AND net of costs. It emits advice
(SUGGEST_EXIT / HOLD), never an executed action, and it never calls a broker
order endpoint. See CLAUDE.md sections 1.1, 1.4 and 2.
"""
