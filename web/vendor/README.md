# Vendored frontend libraries

These are committed on purpose. **The dashboard must render with no network access.**

## Why

They used to be CDN `<script src="https://...">` tags. The Babel URL was unpinned:

```html
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
```

unpkg began serving **Babel 8**, whose `preset-react` changed its default from
`runtime: "classic"` to `runtime: "automatic"`. JSX started compiling to

```js
import { jsx as _jsx } from "react/jsx-runtime";
```

An ES `import` inside a classic `<script>` is a parse error. The entire application
script never executed, `#root` stayed empty, and `body { background: #0b0f14 }`
rendered a flat black page — while `/health` returned perfectly valid JSON.

No line of our code changed. A third party published a version and the dashboard went
blank. On a tool you look at while holding a position, that is the failure mode that
matters most.

## The pins

| File | Source | Version |
|---|---|---|
| `react.production.min.js` | `unpkg.com/react@18.3.1/umd/react.production.min.js` | 18.3.1 |
| `react-dom.production.min.js` | `unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js` | 18.3.1 |
| `babel.min.js` | `unpkg.com/@babel/standalone@7.26.10/babel.min.js` | **7.26.10 — must stay 7.x** |
| `tailwind.js` | `cdn.tailwindcss.com` (Play CDN, v3) | as fetched 2026-08-21 |

Storing the bytes *is* the pin. Do not replace these with CDN URLs.

## If you upgrade Babel to 8

`index.html` registers a `react-classic` preset that passes `{ runtime: "classic" }`
explicitly, so Babel 8 would also work. Both guards exist because either alone is one
edit away from being removed.

## What enforces this

- `test_web_page_makes_no_external_requests` — fails if any `src`/`href` in
  `index.html` points off-origin.
- `create_app()` raises at startup if any file here is missing, rather than serving a
  page that cannot render.
- `test_dashboard_actually_renders_in_a_real_browser` — runs headless Chrome and
  asserts the mounted DOM, so a substring match on the HTML can never again stand in
  for "a human can read this".
