# web/ — React dashboard

**Not scaffolded yet.** Placeholder only, by design: the backend build order puts
Module C (Position Tracker) first, and the UI should follow a working API rather
than lead it.

## Intended stack

- Vite + React + TypeScript
- Dev server proxying `/api` to the FastAPI backend on `127.0.0.1:8000`

## When it is time

```powershell
cd web
npm create vite@latest . -- --template react-ts
npm install
```

`web/node_modules/` and `web/dist/` are already gitignored.

## UI rules

These follow from CLAUDE.md and apply to every view:

- The dashboard **advises**. Label actions `Suggested entry` / `Suggested exit` /
  `Hold` — never a bare "Buy"/"Sell" button, and never anything that looks like it
  submits an order. There is no order-placement endpoint to call.
- **Never render a gross P&L without the net beside it.** Not in a card, not in a
  tooltip, not in a table cell.
- **`NO_TRADE` / `NO_SIGNAL` gets a real, deliberate empty state** — a clear "no
  qualifying setup today", not a blank panel and not a spinner that never resolves.
  An empty candidate list is a correct answer, and the UI should say so with the
  same confidence it shows a candidate.
- Distinguish **"market closed / holiday"** from **"no signal"**. They look
  identical in a naive UI and mean completely different things.
- Show every timestamp with an explicit **IST** label.
- Surface the unverified-data warnings (holiday list, cost rates) rather than
  hiding them.
