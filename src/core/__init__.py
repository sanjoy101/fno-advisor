"""Cross-cutting primitives shared by every module.

Planned:
    costs.py   -- the transaction cost model: brokerage, STT/CTT, exchange charge,
                  SEBI fee, GST, stamp duty, and MODELLED slippage. Charges BOTH
                  legs of a round trip. Options STT is on premium, not notional.
    money.py   -- Decimal money type, tick-size rounding, lot-size arithmetic.
    types.py   -- shared enums and result models, including the advice vocabulary:
                  SUGGEST_ENTRY / SUGGEST_EXIT / HOLD / NO_TRADE / NO_SIGNAL.
    logging.py -- structured logging that never emits a token or secret.

Hard rule enforced here: no function returns a bare gross P&L float. Results carry
{"gross_pnl", "costs", "net_pnl"} together. See CLAUDE.md 1.4.
"""
