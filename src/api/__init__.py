"""FastAPI application serving the React dashboard in web/.

Binds 127.0.0.1 only -- this is a single-user local tool and must never be exposed
on a network interface.

Every route is a read or an advisory computation. There is no route that places,
modifies, or cancels an order, and none may be added.
"""
