"""Module A -- Morning Setup.  BUILT (2026-08-27), pending review.

`setup.py` answers the questions that come before any position exists: is today a
session, where in it are we, what capital is available, what target and loss limit are
configured, what one round trip costs against that capital, and whether the configured
target survives that cost.

It is pure arithmetic over the NSE calendar and the shared cost model. It reads no
market data, no indicator, no model and no broker token, so it runs offline and in
replay identically. There is no symbol anywhere in it, and no ranking -- "which stock
will go up" is not a question this module is allowed to answer.

`NO_TRADE` is a first-class result. A holiday, a closed session, missing capital or a
loss budget too small to absorb a single round trip's costs all produce it, and nothing
here lowers a target or widens a risk limit to avoid it.
"""
