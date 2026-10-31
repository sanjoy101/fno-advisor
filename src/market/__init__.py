"""Market structure: trading calendar, session times, F&O ban list, circuit limits.

Implemented:
    calendar.py -- NSE trading calendar and session boundaries.

Planned (not yet built):
    ban_list.py -- F&O ban period membership. Symbols in ban are SKIPPED entirely,
        never ranked or suggested. Use the list published the previous evening.
    circuits.py -- circuit-limit state. A symbol can become untradeable mid-session;
        that is a real state the Position Tracker must surface, not retry through.
    universe.py -- the F&O universe as-of a date. Stored per-date to avoid
        survivorship bias (CLAUDE.md 1.2).
"""
