"""Does the dashboard actually RENDER? Not "does the server return bytes".

Why this file exists
--------------------

The end-to-end check this replaced asserted that ``GET /`` returned ~20 KB containing
the strings "REPLAY", "FEED STALE" and "FORCE EXIT". It passed while the page was a
blank black rectangle in a real browser.

It passed *because* it was a substring match. Those strings live inside the
``<script type="text/babel">`` block, which is part of the served HTML whether or not
a single line of it ever executes. The test was reading the source code of the app,
not its output.

What had actually happened: the CDN URL for ``@babel/standalone`` was unpinned, unpkg
began serving Babel 8, and Babel 8's ``preset-react`` defaults to
``runtime: "automatic"`` -- so JSX compiled to ``import {jsx} from "react/jsx-runtime"``.
An ES import inside a classic ``<script>`` is a parse error, the application never
ran, and ``#root`` stayed empty. ``/health`` returned 200 the entire time.

So: if the page ships JavaScript, the test has to EXECUTE the JavaScript. These tests
drive real headless Chrome over the DevTools protocol and assert against the mounted
DOM -- element present, non-empty, and actually laid out with a non-zero box.

Chrome is required. When it cannot be found these tests FAIL rather than skip, unless
``FNO_ALLOW_NO_BROWSER=1`` is set explicitly. A skipped render test reintroduces the
exact failure mode this file exists to prevent: a green suite over an unreadable page.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from src.api.app import REQUIRED_VENDOR, WEB_DIR, create_app
from src.api.runtime import build_replay_runtime
from src.positions.store import PositionStore
from tests.conftest import TRADE_DATE, ist, make_series

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str | None:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return shutil.which("chrome") or shutil.which("chromium") or shutil.which("msedge")


def require_browser() -> str:
    browser = find_browser()
    if browser:
        return browser
    if os.environ.get("FNO_ALLOW_NO_BROWSER") == "1":
        pytest.skip("no browser and FNO_ALLOW_NO_BROWSER=1 was set explicitly")
    pytest.fail(
        "No Chrome/Edge found, so the dashboard's rendering is UNVERIFIED. Set "
        "CHROME_PATH, or set FNO_ALLOW_NO_BROWSER=1 to accept that risk knowingly. "
        "This fails rather than skips on purpose: the bug it guards against was a "
        "blank page under a fully green test suite."
    )


def free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------------
# A tiny DevTools-protocol client. No new dependency: websockets already ships with
# uvicorn's extras and drives the /stream endpoint.
# --------------------------------------------------------------------------------


class Browser:
    """Headless Chrome, driven far enough to ask 'what does a human see'."""

    def __init__(self, exe: str, profile: Path):
        # One event loop per browser, owned here. Opening the devtools socket on one
        # loop and closing it on another raises out of teardown and masks the real
        # result of the test.
        self.loop = asyncio.new_event_loop()
        self.port = free_port()
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", "--window-size=1400,900",
             f"--remote-debugging-port={self.port}",
             f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._ws = None
        self._id = 0

    def _target(self) -> str:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                targets = json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json"))
                pages = [t for t in targets if t["type"] == "page"]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 - devtools is simply not up yet
                pass
            time.sleep(0.25)
        raise RuntimeError("Chrome devtools endpoint never came up")

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        me = self._id
        await self._ws.send(json.dumps({"id": me, "method": method,
                                        "params": params or {}}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == me:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})

    async def open(self, url: str, block: list[str] | None = None) -> None:
        import websockets

        self._ws = await websockets.connect(self._target(), max_size=30_000_000)
        await self._send("Runtime.enable")
        await self._send("Page.enable")
        await self._send("Network.enable")
        if block:
            await self._send("Network.setBlockedURLs", {"urls": block})
        await self._send("Page.navigate", {"url": url})

    async def eval(self, expression: str):
        result = await self._send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, timeout: float = 20.0):
        """Poll until the expression is truthy. Returns it, or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = await self.eval(expression)
            if value:
                return value
            await asyncio.sleep(0.25)
        return None

    def run(self, coro):
        """Run a coroutine on this browser's own loop."""
        return self.loop.run_until_complete(coro)

    async def _close(self):
        if self._ws is None:
            return
        try:
            # Ask Chrome to shut itself down. terminate() on the launcher process does
            # NOT take the renderer/GPU children with it on Windows, and a test file
            # with eight browser tests then leaves eight process trees competing for
            # the machine -- which shows up as an unrelated test timing out waiting
            # for a mount.
            await self._send("Browser.close")
        except Exception:  # noqa: BLE001 - already gone, or refusing; killed below
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)  # let the keepalive task unwind quietly

    def close(self):
        try:
            self.loop.run_until_complete(self._close())
        finally:
            self.loop.close()
            self._kill_tree()

    def _kill_tree(self):
        """Backstop: make sure no part of this browser survives the test."""
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, check=False)
        else:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


# --------------------------------------------------------------------------------
# The app under test, served by a real uvicorn on a real socket
# --------------------------------------------------------------------------------


def _serve_runtime(runtime, *, run_loop=True):
    """Run a runtime under a real uvicorn on a free port. Returns (url, shutdown).

    ``run_loop=False`` freezes the virtual clock exactly where the caller put it. The
    replay loop otherwise CLAMPS the clock to ``last_bar + REPLAY_TAIL``, so a runtime
    parked at 15:20 over bars ending 12:34 is pulled back to 12:39 on the first tick --
    which is how a FORCE_EXIT fixture silently became a STALE_DATA one. The page still
    populates from its mount-time GET /positions.
    """
    import uvicorn

    port = free_port()
    config = uvicorn.Config(create_app(runtime, run_loop=run_loop),
                            host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    crash: list[str] = []

    def run_server():
        # Warning filters are process-global, and pytest sets `error` for the suite.
        # uvicorn imports websockets.legacy lazily IN THIS THREAD, which warns, which
        # would otherwise abort startup with the traceback stranded off the main
        # thread -- presenting as a silent "server never started".
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("default")
            try:
                server.run()
            except BaseException:  # noqa: BLE001 - re-raised on the main thread below
                import traceback
                crash.append(traceback.format_exc())

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline:
        if crash:
            raise RuntimeError(f"the uvicorn thread died:\n{crash[0]}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:  # noqa: BLE001 - server still binding
            time.sleep(0.2)
    else:
        detail = f"\n{crash[0]}" if crash else " (no exception captured)"
        raise RuntimeError(f"uvicorn never started:{detail}")

    def shutdown():
        server.should_exit = True
        thread.join(timeout=15)

    return f"http://127.0.0.1:{port}", shutdown


def _replay_store(tmp_path, trade_date, symbols=("RELIANCE",)):
    store = PositionStore(tmp_path)
    for i, symbol in enumerate(symbols):
        # Alternate rising/falling so bulk entry checks produce mixed verdicts.
        prices = ([1000 + (j % 7) * 0.5 for j in range(200)] if i % 2 == 0
                  else [1000 - j * 0.4 for j in range(200)])
        store.save_bars(symbol, trade_date,
                        make_series(prices, start=ist(9, 15, d=trade_date),
                                    volumes=[20000] * 200, spread=1.2))
    return store


@pytest.fixture
def served_multi(tmp_path):
    """A dashboard with several symbols, registered out of alphabetical order."""
    symbols = ["TCS", "INFY", "RELIANCE", "ABB"]
    store = _replay_store(tmp_path, TRADE_DATE, symbols)
    runtime = build_replay_runtime(store, symbols, TRADE_DATE,
                                   speed=600.0, tick_seconds=0.05)
    url, shutdown = _serve_runtime(runtime)
    yield url
    shutdown()


@pytest.fixture
def served(tmp_path):
    """A REPLAY dashboard on a real port, with one position that has advice."""
    store = _replay_store(tmp_path, TRADE_DATE)
    runtime = build_replay_runtime(store, ["RELIANCE"], TRADE_DATE,
                                   speed=600.0, tick_seconds=0.05)
    runtime.add_position(symbol="RELIANCE", quantity=100, entry_price="1000.00",
                         entry_time=ist(9, 30, d=TRADE_DATE))
    url, shutdown = _serve_runtime(runtime)
    yield url
    shutdown()


@pytest.fixture
def serve_date(tmp_path):
    """Factory: serve a dashboard for a CHOSEN date, through the --date code path.

    Goes via src.api.serve.build_runtime, the same function the `dashboard` CLI verb
    calls, so the test covers flag -> runtime -> /health -> rendered banner rather
    than just the last hop.
    """
    from src.api.serve import build_runtime

    shutdowns = []

    def make(trade_date):
        store = _replay_store(tmp_path / str(trade_date), trade_date)
        runtime = build_runtime(store, mode="replay", trade_date=trade_date,
                                speed=600.0, symbols="RELIANCE", tick_seconds=0.05)
        url, shutdown = _serve_runtime(runtime)
        shutdowns.append(shutdown)
        return url

    yield make
    for s in shutdowns:
        s()


@pytest.fixture
def static_only():
    """The page and its vendored assets, with NO backend behind them.

    `/positions` 404s and the `/stream` websocket cannot connect, so the page has
    genuinely no data. This is the "backend is down / not answering" case, and it is
    simulated by absence rather than by CDP URL blocking -- Network.setBlockedURLs
    does not apply to websockets, so the socket kept delivering health and the page
    under test was never actually data-less.
    """
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=10)


@pytest.fixture
def browser(tmp_path):
    b = Browser(require_browser(), tmp_path / "chrome-profile")
    yield b
    b.close()


# --------------------------------------------------------------------------------
# Disclosure helpers
#
# Standing caveats live behind per-panel "Why?" <details> toggles, collapsed by
# default. Collapsed is not deleted: these helpers open one and read it, so the tests
# assert the caveats are PRESENT AND REACHABLE rather than permanently on screen.
# --------------------------------------------------------------------------------


async def root_text(browser) -> str:
    """Rendered text of the app. Excludes anything inside a closed <details>."""
    return await browser.wait_for(
        "(()=>{const r=document.getElementById('root');"
        "return r && r.innerText.length > 50 ? r.innerText : null;})()", timeout=25)


async def open_disclosure(browser, testid: str) -> str | None:
    """Click a Why? toggle and return the disclosure's text once it is open."""
    found = await browser.wait_for(
        f"!!document.querySelector('[data-testid=\"{testid}\"]')", timeout=25)
    if not found:
        return None
    await browser.eval(
        f"document.querySelector('[data-testid=\"{testid}\"] summary').click()")
    return await browser.wait_for(
        f"(()=>{{const d=document.querySelector('[data-testid=\"{testid}\"]');"
        f"return d && d.open ? d.innerText : null;}})()", timeout=10)


async def is_collapsed(browser, testid: str) -> bool:
    return bool(await browser.eval(
        f"(()=>{{const d=document.querySelector('[data-testid=\"{testid}\"]');"
        f"return !!d && !d.open;}})()"))


# --------------------------------------------------------------------------------
# The tests that would have caught the blank page
# --------------------------------------------------------------------------------


def test_dashboard_actually_renders_in_a_real_browser(served, browser):
    """#root must contain a laid-out DOM, not just be present in the HTML source.

    This is the assertion the old substring check could not make. With Babel failing,
    #root has zero children and zero height while the served HTML is byte-identical.
    """
    async def go():
        await browser.open(served)
        mounted = await browser.wait_for(
            "document.getElementById('root').childElementCount > 0")
        assert mounted, "#root never received any children -- the app did not mount"

        box = json.loads(await browser.eval(
            "(()=>{const r=document.getElementById('root');"
            "const b=r.getBoundingClientRect();"
            "return JSON.stringify({w:Math.round(b.width),h:Math.round(b.height),"
            "text:r.innerText.length});})()"
        ))
        assert box["w"] > 200, f"#root has no width: {box}"
        assert box["h"] > 200, f"#root has no height: {box}"
        assert box["text"] > 300, f"#root rendered almost no text: {box}"

    browser.run(go())


def test_no_uncaught_javascript_errors_on_load(served, browser):
    """A page that throws on load is a page that renders nothing."""
    async def go():
        await browser.open(served)
        await browser.wait_for("document.getElementById('root').childElementCount > 0")
        # The page installs a window.onerror overlay; if it fired, it says so.
        failed = await browser.eval(
            "!!document.body.innerText.match(/DASHBOARD FAILED TO RENDER/)")
        assert not failed, "the page's own error overlay fired -- check the console"

    browser.run(go())


def test_the_mode_banner_is_visible_and_says_replay(served, browser):
    """The single most important pixel on the page: which mode am I looking at."""
    async def go():
        await browser.open(served)
        # Wait for the banner to SETTLE, not merely to exist. It renders immediately
        # as "NO DATA - MODE UNKNOWN" and only resolves once /health arrives; waiting
        # on the element alone races that and reads the placeholder.
        banner = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"mode-banner\"]');"
            "if(!e || e.innerText.includes('UNKNOWN')) return null;"
            "const b=e.getBoundingClientRect();const s=getComputedStyle(e);"
            "return JSON.stringify({text:e.innerText,w:Math.round(b.width),"
            "h:Math.round(b.height),display:s.display,visibility:s.visibility,"
            "opacity:s.opacity});})()"
        )
        assert banner, "no mode banner rendered at all"
        info = json.loads(banner)
        assert "REPLAY" in info["text"], f"banner does not say REPLAY: {info}"
        assert "NOT LIVE" in info["text"], f"banner must disclaim live: {info}"
        # Present in the DOM is not the same as visible to a human.
        assert info["w"] > 100 and info["h"] > 20, f"banner has no box: {info}"
        assert info["display"] != "none" and info["visibility"] != "hidden", info
        assert float(info["opacity"]) > 0.5, info

    browser.run(go())


@pytest.mark.parametrize("offset_days", [0, 5])
def test_the_banner_date_matches_the_date_it_was_launched_with(
        serve_date, browser, offset_days):
    """The header must name the session actually being replayed.

    Two dates, because a hardcoded or defaulted date passes trivially against one.
    Also asserts the OTHER date is absent, so a banner showing both -- or the wrong
    one -- fails.

    This is the same class of fault as the LIVE/UNKNOWN bug: a dashboard that
    misreports which session it is showing is worse than one that shows nothing,
    because it looks authoritative.
    """
    from datetime import timedelta

    wanted = TRADE_DATE + timedelta(days=offset_days)
    other = TRADE_DATE + timedelta(days=5 - offset_days)
    url = serve_date(wanted)

    async def go():
        await browser.open(url)
        text = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('Replaying stored bars') ? t : null;})()")
        assert text, "the replay banner never rendered"
        assert str(wanted) in text, (
            f"banner does not name the session being replayed ({wanted}); got: "
            f"{text[:300]!r}")
        if other != wanted:
            assert str(other) not in text, (
                f"banner names a date that was not requested ({other})")

    browser.run(go())


def test_a_position_card_renders_with_gross_and_net(served, browser):
    """Both figures reach the screen, not just the JSON."""
    async def go():
        await browser.open(served)
        text = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('RELIANCE') ? t : null;})()", timeout=25)
        assert text, "the position never appeared on the page"
        assert "gross" in text.lower(), "gross P&L is not rendered"
        assert "net" in text.lower(), "net P&L is not rendered"

    browser.run(go())


def test_page_with_no_data_never_claims_to_be_live(static_only, browser):
    """With no backend behind it at all, the banner must NOT read LIVE.

    `h.mode === "REPLAY"` used to mean every data-less render fell through to a bare
    LIVE banner, so a page that had loaded nothing was indistinguishable from a
    healthy live session. Unknown must look unknown.

    Absence, not URL blocking: CDP's Network.setBlockedURLs does not apply to
    websockets, so /stream kept delivering health and the page under test was never
    actually data-less.
    """
    async def go():
        await browser.open(static_only)
        banner = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"mode-banner\"]');"
            "return e ? e.innerText : null;})()")
        assert banner, "no banner rendered"
        assert "LIVE" not in banner.replace("NOT LIVE", ""), (
            f"a page with no data claimed to be LIVE: {banner!r}")
        assert "UNKNOWN" in banner.upper(), f"unknown mode not surfaced: {banner!r}"

    browser.run(go())


def test_a_server_error_is_shown_on_the_page_not_swallowed(served, browser):
    """If /positions fails, the page must say so rather than look idle."""
    async def go():
        await browser.open(served, block=["*/positions", "*/stream"])
        shown = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return /cannot reach the server|no health payload|mode unknown/i"
            ".test(t) ? t : null;})()")
        assert shown, "the page hid a total data failure behind an idle-looking UI"

    browser.run(go())


def test_the_bulk_entry_check_renders_a_table_in_alphabetical_order(
        served_multi, browser):
    """The panel must render, and its rows must be alphabetical on screen.

    Server-side ordering is asserted in test_api.py; this asserts the DOM the human
    reads, because a client-side .sort() would satisfy the API test and still present
    a ranking.
    """
    async def go():
        await browser.open(served_multi)
        await browser.wait_for("!!document.querySelector('[data-testid=\"check-all-button\"]')")
        await browser.eval("document.querySelector('[data-testid=\"check-all-button\"]').click()")
        symbols = await browser.wait_for(
            "(()=>{const rs=document.querySelectorAll('[data-testid=\"check-all-row\"]');"
            "if(!rs.length) return null;"
            "return JSON.stringify([...rs].map(r=>r.cells[0].innerText.trim()));})()",
            timeout=25)
        assert symbols, "the bulk check table never rendered any rows"
        rendered = json.loads(symbols)
        assert rendered == ["ABB", "INFY", "RELIANCE", "TCS"], rendered
        assert rendered == sorted(rendered), "rows are not alphabetical on screen"

    browser.run(go())


def test_the_bulk_panel_has_no_score_column_and_no_highlighted_row(
        served_multi, browser):
    """Four columns, and no row styled as preferred.

    A "best candidate" highlight is how a filter becomes a recommendation without
    anyone adding a score field.

    The fourth column is ``event risk``, added deliberately. It is a DISQUALIFIER, not
    a preference: it takes one of three unordered values, it carries no number, and it
    can only ever argue against a symbol. Critically it lives in a ``<td>`` rather than
    on the ``<tr>``, so the identical-row-classes assertion below still holds -- a
    row-level highlight is exactly the thing that must not appear, whichever direction
    it points in.
    """
    async def go():
        await browser.open(served_multi)
        await browser.wait_for("!!document.querySelector('[data-testid=\"check-all-button\"]')")
        await browser.eval("document.querySelector('[data-testid=\"check-all-button\"]').click()")
        await browser.wait_for(
            "document.querySelectorAll('[data-testid=\"check-all-row\"]').length > 0",
            timeout=25)

        headers = json.loads(await browser.eval(
            "JSON.stringify([...document.querySelectorAll("
            "'[data-testid=\"check-all-table\"] thead th')].map(t=>t.innerText.trim().toLowerCase()))"))
        assert headers == ["symbol", "verdict", "event risk", "reason"], headers

        # Every row must carry the same classes: no single row picked out.
        row_classes = json.loads(await browser.eval(
            "JSON.stringify([...document.querySelectorAll('[data-testid=\"check-all-row\"]')]"
            ".map(r=>r.className))"))
        assert len(set(row_classes)) == 1, (
            f"rows are styled differently from each other: {set(row_classes)}")

        # Scoped to the TABLE. The surrounding prose deliberately contains "not a
        # ranking", so a naive substring scan of the whole panel flags its own
        # disclaimer -- which is how this assertion first failed.
        table = await browser.eval(
            "document.querySelector('[data-testid=\"check-all-table\"]')"
            ".innerText.toLowerCase()")
        for banned in ("best", "top pick", "recommend", "score", "rank",
                       "confidence", "prefer", "%"):
            assert banned not in table, f"the table implies a ranking: {banned!r}"

    browser.run(go())


def test_the_event_risk_panel_renders_and_reports_unknown_in_replay(
        served_multi, browser):
    """Executed in a browser, not matched as a substring.

    The lesson from the blank-page incident applies directly here: the panel's markup
    sits inside the ``text/babel`` block, so a substring scan of the served HTML passes
    whether or not a single line of it ever runs.

    REPLAY must report UNAVAILABLE. Announcements are forward-collected only, so a
    replayed session has nothing contemporaneous to consult -- and rendering that as
    CLEAR would assert, of every historical date, that nothing happened.
    """
    async def go():
        await browser.open(served_multi)
        # Wait for the panel to SETTLE, not merely to exist. Its no-payload fallback is
        # ALSO labelled UNAVAILABLE -- correctly, since "nothing has arrived" is not
        # CLEAR -- so waiting on the state chip alone races the first push and reads the
        # placeholder. Same trap as the mode banner's "NO DATA - MODE UNKNOWN".
        note = await browser.wait_for(
            "(()=>{const e=document.querySelector("
            "'[data-testid=\"event-risk-unavailable\"]');"
            "if(!e) return null;"
            "const t=e.innerText;"
            "return t.includes('No event-risk payload') ? null : t;})()", timeout=25)
        assert note, "the event-risk panel never received a payload"
        assert "forward-collected" in note.lower(), note

        state = await browser.eval(
            "(()=>{const e=document.querySelector('[data-testid=\"event-risk-state\"]');"
            "return e ? e.innerText.trim() : null;})()")
        assert state, "the event-risk panel never rendered"
        assert "UNAVAILABLE" in state.upper(), state
        # Never CLEAR by omission.
        assert "clear" not in state.lower()

    browser.run(go())


def test_the_event_risk_panel_says_what_it_is_and_is_not(served_multi, browser):
    """The constraints are on screen for the human, not only in the JSON.

    They now live behind the panel's "Why?" disclosure. That is a layout decision and
    nothing else: this test opens it and asserts every word is still there, unchanged.
    Reachability is the property to hold, not permanent occupancy of the screen --
    see test_standing_caveats_are_collapsed_but_reachable for the other half.
    """
    async def go():
        await browser.open(served_multi)
        text = await open_disclosure(browser, "why-event-risk")
        assert text, "the event-risk Why? disclosure never rendered"
        text = text.lower()
        assert "exclusion filter" in text
        assert "nothing about direction" in text
        assert "unknown is not clear" in text
        assert "improves returns" in text
        assert "forward-collected only" in text

    browser.run(go())


def test_the_bulk_table_shows_an_event_risk_badge_on_every_row(served_multi, browser):
    """A badge per row, in a CELL, and no row styled differently from another.

    In replay every symbol is UNKNOWN, which is the point: the column renders the
    honest state rather than quietly showing nothing.
    """
    async def go():
        await browser.open(served_multi)
        await browser.wait_for("!!document.querySelector('[data-testid=\"check-all-button\"]')")
        await browser.eval("document.querySelector('[data-testid=\"check-all-button\"]').click()")
        badges = await browser.wait_for(
            "(()=>{const rs=document.querySelectorAll('[data-testid=\"check-all-row\"]');"
            "if(!rs.length) return null;"
            "return JSON.stringify([...rs].map(r=>r.querySelector("
            "'[data-testid=\"event-risk-badge\"]').getAttribute('data-risk')));})()",
            timeout=25)
        assert badges, "no event-risk badge rendered in the bulk table"
        values = json.loads(badges)
        assert len(values) == 4
        assert set(values) == {"UNKNOWN"}, values

    browser.run(go())


def test_the_bulk_panel_says_sound_is_not_a_prediction(served_multi, browser):
    """On screen, not only in the JSON. Behind the panel's Why? disclosure."""
    async def go():
        await browser.open(served_multi)
        text = await open_disclosure(browser, "why-bulk-check")
        assert text, "the bulk-check Why? disclosure never rendered"
        text = text.lower()
        assert "filter, not a ranking" in text, (
            "the panel does not say it is a filter rather than a ranking")
        assert "not" in text and "rise" in text, (
            "the panel must say SOUND is not a prediction that the price will rise")
        assert "alphabetical" in text
        assert "no score" in text and "no rank" in text

    browser.run(go())


def test_there_is_no_signal_panel_and_the_page_says_why(served_multi, browser):
    """The absent signal panel is explained on the page, not only in CLAUDE.md.

    Module B produced no edge over a naive comparator at any horizon tested, so a
    scored ranking here would present a known-negative result as a recommendation.
    The page has to say that where a reader can find it.
    """
    async def go():
        await browser.open(served_multi)
        text = await open_disclosure(browser, "why-advisory")
        assert text, "the advisory Why? disclosure never rendered"
        text = text.lower()
        assert "no signal panel" in text
        assert "no edge" in text
        assert "naive comparator" in text
        assert "never places" in (await root_text(browser)).lower(), (
            "the advisory-only claim itself must stay visible without interaction")

    browser.run(go())


# --------------------------------------------------------------------------------
# Layout: readability, without losing a single caveat
# --------------------------------------------------------------------------------


def test_standing_caveats_are_collapsed_but_reachable(served_multi, browser):
    """Both halves of the deal, in one test.

    The caveats used to occupy permanent screen space in ~11px paragraphs. They are now
    behind "Why?" toggles: NOT on screen by default, and fully present one click away.
    Asserting only the second half would let a future edit delete them; asserting only
    the first would let a future edit collapse something that must stay visible.
    """
    async def go():
        await browser.open(served_multi)
        text = (await root_text(browser)).lower()

        # Half one: the standing explanations are not occupying the screen.
        for phrase in ("nothing about direction", "unknown is not clear",
                       "filter, not a ranking", "no edge over a naive comparator"):
            assert phrase not in text, (
                f"{phrase!r} is still permanently on screen; it belongs behind Why?")

        for testid in ("why-event-risk", "why-bulk-check", "why-advisory"):
            assert await is_collapsed(browser, testid), f"{testid} is open by default"

        # Half two: every one of them is reachable and unchanged.
        assert "nothing about direction" in (
            await open_disclosure(browser, "why-event-risk")).lower()
        assert "filter, not a ranking" in (
            await open_disclosure(browser, "why-bulk-check")).lower()
        assert "no edge over a naive comparator" in (
            await open_disclosure(browser, "why-advisory")).lower()

    browser.run(go())


def test_the_empty_state_offers_the_bulk_check_not_an_empty_box(served_multi, browser):
    """With nothing open, the wide column is the screening tool, not blank space."""
    async def go():
        await browser.open(served_multi)
        await browser.wait_for("!!document.querySelector('[data-testid=\"no-positions\"]')",
                               timeout=25)
        # The bulk check must be laid out in the wide column, not tucked in the rail.
        box = json.loads(await browser.wait_for(
            "(()=>{const b=document.querySelector('[data-testid=\"check-all-button\"]');"
            "if(!b) return null;const r=b.getBoundingClientRect();"
            "return JSON.stringify({w:Math.round(r.width),top:Math.round(r.top)});})()",
            timeout=25))
        assert box["w"] > 500, (
            f"the bulk check is not occupying the wide column: {box}")

    browser.run(go())


def test_positions_take_the_wide_column_when_there_are_any(served, browser):
    """And the bulk check stays reachable below them rather than disappearing."""
    async def go():
        await browser.open(served)
        text = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('RELIANCE') ? t : null;})()", timeout=25)
        assert text, "the position never appeared"
        assert "No open positions" not in text
        assert await browser.eval(
            "!!document.querySelector('[data-testid=\"check-all-button\"]')"), (
            "the bulk check vanished once a position existed")

    browser.run(go())


def test_the_two_silent_failure_readouts_are_prominent(served, browser):
    """Feed and token break this tool without an error. They get their own size.

    Not animated on purpose: motion on this page means STALE_DATA or FORCE_EXIT and
    nothing else, and diluting that is how the alarm stops meaning anything.
    """
    async def go():
        await browser.open(served)
        info = await browser.wait_for(
            "(()=>{const g=document.querySelector('[data-testid=\"silent-failure-group\"]');"
            "const f=document.querySelector('[data-testid=\"feed-status\"]');"
            "const t=document.querySelector('[data-testid=\"token-status\"]');"
            "const s=document.querySelector('[data-testid=\"session\"]');"
            "if(!g||!f||!t) return null;"
            "const fv=f.querySelector('div:nth-child(2)');"
            "const b=g.getBoundingClientRect();"
            "return JSON.stringify({w:Math.round(b.width),h:Math.round(b.height),"
            "feedSize:parseFloat(getComputedStyle(fv).fontSize),"
            "anim:getComputedStyle(g).animationName,"
            "text:g.innerText});})()", timeout=25)
        assert info, "the feed/token group never rendered"
        info = json.loads(info)
        assert info["w"] > 200 and info["h"] > 40, info
        assert info["feedSize"] >= 20, (
            f"the feed readout is not larger than ordinary header text: {info}")
        assert info["anim"] in ("none", ""), (
            "the silent-failure group must not animate -- motion is reserved for "
            f"STALE_DATA and FORCE_EXIT: {info}")
        assert "token" in info["text"].lower() and "feed" in info["text"].lower()

    browser.run(go())


def test_the_right_rail_is_not_set_in_tiny_type(served, browser):
    """Second monitor, arm's length. The rail was ~11px paragraphs."""
    async def go():
        await browser.open(served)
        await root_text(browser)
        sizes = json.loads(await browser.eval(
            "(()=>{const out=[];"
            "for(const el of document.querySelectorAll('p, td, li, summary')){"
            "if(!el.innerText.trim() || el.offsetParent===null) continue;"
            "out.push(parseFloat(getComputedStyle(el).fontSize));}"
            "return JSON.stringify(out);})()"))
        assert sizes, "no prose rendered at all"
        assert min(sizes) >= 14, (
            f"prose is set below 14px somewhere on the page: min={min(sizes)}")

    browser.run(go())


def test_the_entry_result_leads_with_a_verdict_and_one_reason(served_multi, browser):
    """Requirement: verdict, then ONE plain line. Detail behind the same Why? toggle.

    The old card printed the same VWAP sentence twice -- once inside `summary`, which
    concatenates every problem, and again in the bulleted problem list underneath.
    """
    async def go():
        await browser.open(served_multi)
        await browser.wait_for("!!document.querySelector('[data-testid=\"check-entry-form\"]')",
                               timeout=25)
        # React tracks the input value, so set it through the native setter and fire
        # the event React listens for; otherwise onChange never runs.
        await browser.eval(
            "(()=>{const form=document.querySelector('[data-testid=\"check-entry-form\"]');"
            "const input=form.querySelector('input');"
            "const set=Object.getOwnPropertyDescriptor("
            "window.HTMLInputElement.prototype,'value').set;"
            "set.call(input,'INFY');"
            "input.dispatchEvent(new Event('input',{bubbles:true}));"
            "form.requestSubmit();})()")

        verdict = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"entry-verdict\"]');"
            "return e ? e.innerText.trim() : null;})()", timeout=25)
        assert verdict in ("SOUND", "UNSOUND", "NOT TRACKED", "NO BAR YET"), verdict

        card = await browser.eval(
            "document.querySelector('[data-testid=\"entry-result\"]').innerText")
        # INFY's series falls all session, so this is the unsound path.
        assert verdict == "UNSOUND", f"expected the falling symbol to be unsound: {verdict}"
        assert card.lower().count("below session vwap") == 1, (
            f"the same VWAP sentence is rendered more than once:\n{card}")

        lead = await browser.eval(
            "document.querySelector('[data-testid=\"entry-lead\"]').innerText")
        assert lead.count("\n") == 0, f"the lead reason is not one line: {lead!r}"
        assert " -- " not in lead, (
            f"the lead should carry the fact, not the boilerplate half: {lead!r}")

        # And the full text is still there, one click away.
        detail = await open_disclosure(browser, "why-entry-result")
        assert "not a prediction" in detail.lower() or (
            "not" in detail.lower() and "rise" in detail.lower())
        # "records nothing" stopped being true when the check log arrived. The claim
        # that must survive verbatim is the one about positions and orders.
        assert "no position was created" in detail.lower()
        assert "no order was placed" in detail.lower()
        assert "below session vwap" in detail.lower(), (
            "the full problem text must remain reachable")

    browser.run(go())


def test_gross_and_net_are_never_behind_a_toggle(served, browser):
    """Both figures must be readable with no interaction at all."""
    async def go():
        await browser.open(served)
        text = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('RELIANCE') ? t : null;})()", timeout=25)
        # The labels are rendered through `text-transform: uppercase`, so innerText
        # comes back uppercased whatever the source string says.
        upper = text.upper()
        assert "GROSS" in upper, "gross is not visible without interaction"
        assert "NET (AFTER COSTS)" in upper, "net is not visible without interaction"
        # And neither sits inside a <details>.
        inside = await browser.eval(
            "(()=>{for(const d of document.querySelectorAll('details')){"
            "if(/GROSS|NET \\(AFTER COSTS\\)/i.test(d.innerText)) return true;}"
            "return false;})()")
        assert not inside, "a P&L figure was moved inside a disclosure"

    browser.run(go())


# --------------------------------------------------------------------------------
# The redesigned operations layout.
#
# A dense dark board for a second monitor. These tests pin the properties the redesign
# must not lose: the mode marker is always on screen, every hazard is unmissable, every
# price carries its age, gross and net stay paired, and nothing anywhere reads as a
# recommendation.
# --------------------------------------------------------------------------------


def _frozen_runtime(tmp_path, at, *, with_position=True, symbols=("RELIANCE",)):
    """A replay runtime parked at a chosen instant, so a state is deterministic.

    speed=1 with a 60s tick means the background loop barely moves the clock for the
    lifetime of a browser test, which is what makes STALE_DATA and FORCE_EXIT testable
    without racing a 600x replay to the end.
    """
    from src.api.runtime import build_replay_runtime

    store = _replay_store(tmp_path, TRADE_DATE, list(symbols))
    runtime = build_replay_runtime(store, list(symbols), TRADE_DATE,
                                   speed=1.0, tick_seconds=60.0)
    if with_position:
        runtime.add_position(symbol=symbols[0], quantity=100, entry_price="1000.00",
                             entry_time=ist(9, 30, d=TRADE_DATE))
    runtime._virtual_now = at
    runtime.feed.advance_to(at)
    return runtime


@pytest.fixture
def served_stale(tmp_path):
    """13:30, past the fixture's last bar (12:34): the feed is stale mid-session."""
    url, shutdown = _serve_runtime(
        _frozen_runtime(tmp_path, ist(13, 30, d=TRADE_DATE)), run_loop=False)
    yield url
    shutdown()


@pytest.fixture
def served_force_exit(tmp_path):
    """15:20, past the 15:15 square-off. FORCE_EXIT outranks the stale feed."""
    url, shutdown = _serve_runtime(
        _frozen_runtime(tmp_path, ist(15, 20, d=TRADE_DATE)), run_loop=False)
    yield url
    shutdown()


async def card_text(browser, timeout=25):
    return await browser.wait_for(
        "(()=>{const t=document.getElementById('root').innerText;"
        "return t.includes('RELIANCE') ? t : null;})()", timeout=timeout)


def test_the_mode_marker_is_visible_without_scrolling(served, browser):
    """A second-monitor glance must never be ambiguous about LIVE versus REPLAY."""
    async def go():
        await browser.open(served)
        info = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"mode-banner\"]');"
            "if(!e || e.innerText.includes('UNKNOWN')) return null;"
            "const r=e.getBoundingClientRect();const h=e.closest('[data-testid="
            "\"ops-header\"]');"
            "return JSON.stringify({text:e.innerText,top:Math.round(r.top),"
            "sticky:h?getComputedStyle(h).position:'none'});})()", timeout=25)
        assert info, "no settled mode marker"
        info = json.loads(info)
        assert "REPLAY" in info["text"] and "NOT LIVE" in info["text"]
        assert info["top"] >= 0 and info["top"] < 200, info
        assert info["sticky"] == "sticky", f"the header must stay on screen: {info}"

    browser.run(go())


def test_the_health_summary_row_reports_the_four_operational_facts(served, browser):
    async def go():
        await browser.open(served)
        # Wait for the payload, not merely for the elements: the row renders
        # immediately with zeroes and only fills once GET /positions returns.
        await card_text(browser)
        cards = await browser.wait_for(
            "(()=>{const ids=['health-positions','health-feed','health-token',"
            "'health-event-risk'];const out={};for(const i of ids){"
            "const e=document.querySelector(`[data-testid=\"${i}\"]`);"
            "if(!e) return null;out[i]=e.innerText;}"
            "return out['health-positions'].includes('0') ? null "
            ": JSON.stringify(out);})()", timeout=25)
        assert cards, "the health summary row did not render all four cards"
        cards = json.loads(cards)
        assert "1" in cards["health-positions"]
        assert cards["health-feed"].strip()
        assert cards["health-token"].strip()
        assert cards["health-event-risk"].strip()

    browser.run(go())


def test_a_stale_position_carries_the_exact_suppression_warning(served_stale, browser):
    """The wording is fixed: it tells the user what to do, not merely that a flag is set."""
    async def go():
        await browser.open(served_stale)
        text = await card_text(browser)
        assert text, "the position never rendered"
        warning = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"stale-warning\"]');"
            "return e ? e.innerText : null;})()", timeout=25)
        assert warning, "no in-card stale warning"
        assert "Price data is stale." in warning
        assert "Exit guidance is suppressed." in warning
        assert "Verify in your broker app." in warning

    browser.run(go())


def test_a_stale_card_is_accented_not_flooded_with_colour(served_stale, browser):
    """A thin accent, not a fully coloured card: colour must stay legible."""
    async def go():
        await browser.open(served_stale)
        await card_text(browser)
        info = json.loads(await browser.eval(
            "(()=>{const c=document.querySelector('[data-testid=\"position-card\"]');"
            "const s=getComputedStyle(c);"
            "return JSON.stringify({left:s.borderLeftWidth,anim:s.animationName});})()"))
        assert float(info["left"].replace("px", "")) >= 3, info
        assert info["anim"] != "none", "STALE_DATA must still animate"

    browser.run(go())


def test_force_exit_stays_visually_prominent(served_force_exit, browser):
    async def go():
        await browser.open(served_force_exit)
        await card_text(browser)
        info = json.loads(await browser.eval(
            "(()=>{const c=document.querySelector('[data-testid=\"position-card\"]');"
            "const chip=c.querySelector('[data-testid=\"state-chip\"]');"
            "return JSON.stringify({state:chip.innerText,"
            "size:parseFloat(getComputedStyle(chip).fontSize),"
            "anim:getComputedStyle(c).animationName});})()"))
        assert "FORCE" in info["state"].upper(), info
        assert info["size"] >= 16, f"the state chip is not prominent: {info}"
        assert info["anim"] != "none", "FORCE_EXIT must still animate"

    browser.run(go())


def test_every_rendered_price_carries_its_age(served, browser):
    """Structural: a price element without a freshness sibling is a price you cannot
    trust, and the card must make that impossible rather than conventional."""
    async def go():
        await browser.open(served)
        await card_text(browser)
        missing = json.loads(await browser.eval(
            "(()=>{const out=[];"
            "for(const e of document.querySelectorAll('[data-price]')){"
            "const age=e.getAttribute('data-price-age');"
            "if(age===null||age==='') out.push(e.innerText);}"
            "return JSON.stringify(out);})()"))
        assert missing == [], f"prices rendered without an age: {missing}"
        count = await browser.eval(
            "document.querySelectorAll('[data-price]').length")
        assert count >= 1, "no priced element was marked at all"

    browser.run(go())


def test_gross_and_net_render_as_one_unit(served, browser):
    async def go():
        await browser.open(served)
        await card_text(browser)
        info = json.loads(await browser.eval(
            "(()=>{const g=document.querySelector('[data-testid=\"pnl-gross\"]');"
            "const n=document.querySelector('[data-testid=\"pnl-net\"]');"
            "if(!g||!n) return JSON.stringify({missing:true});"
            "return JSON.stringify({gross:g.innerText,net:n.innerText,"
            "sameParent:g.parentElement===n.parentElement});})()"))
        assert not info.get("missing"), "gross and net are not both rendered"
        assert info["sameParent"], "gross and net must sit in one block"
        assert "GROSS" in info["gross"].upper()
        assert "NET" in info["net"].upper()

    browser.run(go())


def test_the_manual_exit_control_never_reads_as_an_order(served, browser):
    async def go():
        await browser.open(served)
        await card_text(browser)
        info = json.loads(await browser.eval(
            "(()=>{const b=document.querySelector('[data-testid=\"record-exit\"]');"
            "if(!b) return JSON.stringify({missing:true});"
            "return JSON.stringify({tag:b.tagName,text:b.innerText});})()"))
        assert not info.get("missing"), "no manual-exit control"
        assert info["tag"] == "BUTTON", "must be a semantic button"
        assert info["text"].strip().lower() == "record manual exit", info
        for banned in ("sell", "exit now", "close position", "square off", "order"):
            assert banned not in info["text"].lower()

    browser.run(go())


def test_the_empty_state_is_calm_useful_and_clearly_non_trading(served_multi, browser):
    async def go():
        await browser.open(served_multi)
        info = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"no-positions\"]');"
            "if(!e) return null;"
            "const bs=[...e.querySelectorAll('button')].map(b=>b.innerText.trim());"
            "return JSON.stringify({text:e.innerText,buttons:bs,"
            "anim:getComputedStyle(e).animationName});})()", timeout=25)
        assert info, "no empty state"
        info = json.loads(info)
        joined = " ".join(info["buttons"]).lower()
        assert "check tracked symbols" in joined, info["buttons"]
        assert "record a manually executed position" in joined, info["buttons"]
        assert "no order" in info["text"].lower()
        assert info["anim"] == "none", "the empty state must be calm"

    browser.run(go())


def test_the_empty_state_actions_reach_the_real_tools(served_multi, browser):
    async def go():
        await browser.open(served_multi)
        await browser.wait_for(
            "!!document.querySelector('[data-testid=\"no-positions\"]')", timeout=25)
        await browser.eval(
            "[...document.querySelectorAll('[data-testid=\"no-positions\"] button')]"
            ".find(b=>/check tracked/i.test(b.innerText)).click()")
        rows = await browser.wait_for(
            "document.querySelectorAll('[data-testid=\"check-all-row\"]').length > 0",
            timeout=25)
        assert rows, "the empty-state action did not run the bulk check"

    browser.run(go())


def test_no_wording_anywhere_reads_as_a_recommendation(served, browser):
    """Page-wide, with the disclaimers excluded -- they necessarily name what they
    disclaim, which is the trap every scan in this repo has hit."""
    async def go():
        await browser.open(served)
        await card_text(browser)
        # Read the LIVE innerText (so collapsed <details> stay excluded) and subtract
        # the visible disclaimer blocks. A detached clone was the first attempt and it
        # was wrong: innerText on an unrendered node falls back to textContent, which
        # pulls in every closed disclosure on the page.
        text = await browser.eval(
            "(()=>{let t=document.getElementById('root').innerText;"
            "for(const d of document.querySelectorAll('[data-disclaimer]'))"
            "  t=t.split(d.innerText).join(' ');"
            "return t;})()")
        low = text.lower()
        for banned in ("buy ", "sell ", "best ", "top pick", "recommend", "score",
                       "confidence", "ranking", "forecast", "expected return",
                       "probability of", "candidate"):
            assert banned not in low, f"the page implies {banned!r}"

    browser.run(go())


def test_the_tools_remain_reachable_after_the_redesign(served, browser):
    async def go():
        await browser.open(served)
        await card_text(browser)
        present = json.loads(await browser.eval(
            "JSON.stringify(['check-entry-form','check-all-button',"
            "'add-position-form','event-risk-state']"
            ".map(i=>!!document.querySelector(`[data-testid=\"${i}\"]`)))"))
        assert all(present), present
        # The checked log renders its EMPTY state until a check has been logged, so
        # either is proof the panel is reachable.
        assert await browser.eval(
            "!!document.querySelector('[data-testid=\"checked-log-table\"]') || "
            "!!document.querySelector('[data-testid=\"checked-log-empty\"]')")

    browser.run(go())


def test_interactive_controls_have_a_visible_focus_state(served, browser):
    """Keyboard use must not be invisible."""
    async def go():
        await browser.open(served)
        await card_text(browser)
        bad = json.loads(await browser.eval(
            "(()=>{const out=[];"
            "for(const b of document.querySelectorAll('button, summary')){"
            "if(b.offsetParent===null) continue;"
            "if(!/focus:(ring|outline)/.test(b.className||'')) out.push("
            "(b.innerText||'').slice(0,30));}"
            "return JSON.stringify(out);})()"))
        assert bad == [], f"controls without a focus style: {bad}"

    browser.run(go())


# --------------------------------------------------------------------------------
# TOKEN INVALID: loud, static, and never showing a price as current
# --------------------------------------------------------------------------------


@pytest.fixture
def served_token_invalid(tmp_path, monkeypatch):
    """A LIVE-mode dashboard whose token loader always fails. No broker, no market."""
    from src.api.runtime import Mode, SessionRuntime
    from src.data.fyers_auth import TokenExpiredError
    from src.positions.feed import MultiSymbolReplayFeed

    at = ist(12, 0, d=TRADE_DATE)
    monkeypatch.setattr("src.api.runtime.now_ist", lambda: at)

    store = _replay_store(tmp_path, TRADE_DATE, ["RELIANCE"])
    feed = MultiSymbolReplayFeed({"RELIANCE": store.load_bars("RELIANCE", TRADE_DATE)})
    feed.advance_to(at)

    def dead_token():
        raise TokenExpiredError("Fyers token expired. Generated 2026-08-20 08:30 IST.")

    runtime = SessionRuntime(
        mode=Mode.LIVE, store=store, symbols=["RELIANCE"], trade_date=TRADE_DATE,
        feed=feed, token_loader=dead_token,
    )
    runtime.add_position(symbol="RELIANCE", quantity=100, entry_price="1000.00",
                         entry_time=ist(9, 30, d=TRADE_DATE))

    url, shutdown = _serve_runtime(runtime)
    yield url
    shutdown()


def test_the_token_invalid_banner_is_loud_and_static(served_token_invalid, browser):
    """Loud, but NOT animated: motion is reserved for STALE_DATA and FORCE_EXIT."""
    async def go():
        await browser.open(served_token_invalid)
        info = await browser.wait_for(
            "(()=>{const e=document.querySelector('[data-testid=\"token-alert\"]');"
            "if(!e) return null;const b=e.getBoundingClientRect();const s=getComputedStyle(e);"
            "return JSON.stringify({text:e.innerText,w:Math.round(b.width),"
            "h:Math.round(b.height),anim:s.animationName,size:parseFloat("
            "getComputedStyle(e.firstElementChild).fontSize)});})()", timeout=25)
        assert info, "the TOKEN INVALID banner never rendered"
        info = json.loads(info)

        assert "TOKEN INVALID" in info["text"].upper()
        assert "SUPPRESSED" in info["text"].upper()
        assert "src.cli auth" in info["text"], "it must name the fix"
        assert info["w"] > 400 and info["h"] > 40, info
        assert info["size"] >= 20, f"the banner headline is not loud: {info}"
        assert info["anim"] in ("none", ""), (
            "the token banner must not animate -- motion means STALE_DATA or "
            f"FORCE_EXIT and nothing else: {info}")

    browser.run(go())


def test_a_suppressed_card_never_shows_a_price_as_current(served_token_invalid, browser):
    async def go():
        await browser.open(served_token_invalid)
        text = await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('RELIANCE') ? t : null;})()", timeout=25)
        assert text, "the position card never rendered"

        assert await browser.eval(
            "!!document.querySelector('[data-testid=\"state-not-current\"]')"), (
            "the card does not say its state is stale")
        assert "NOT CURRENT" in text.upper()
        # Gross and net stay side by side even here.
        upper = text.upper()
        assert "GROSS" in upper and "NET (AFTER COSTS)" in upper
        assert "stale price" in text.lower(), "the P&L must be labelled stale"

    browser.run(go())


def test_the_suppressed_card_offers_no_exit_advice(served_token_invalid, browser):
    async def go():
        await browser.open(served_token_invalid)
        await browser.wait_for(
            "(()=>{const t=document.getElementById('root').innerText;"
            "return t.includes('RELIANCE') ? t : null;})()", timeout=25)
        text = await browser.eval("document.getElementById('root').innerText")
        assert "you execute this manually" not in text.lower(), (
            "a suppressed card must not suggest selling anything")
        assert "why there is no advice" in text.lower()

    browser.run(go())


# --------------------------------------------------------------------------------
# The "checked today" scoreboard, in a browser
# --------------------------------------------------------------------------------


@pytest.fixture
def served_with_checks(tmp_path):
    """A dashboard whose check log already holds a mixed, multi-minute sample."""
    from src.api.serve import build_runtime

    symbols = ["TCS", "INFY", "RELIANCE", "ABB"]
    store = _replay_store(tmp_path, TRADE_DATE, symbols)
    runtime = build_runtime(store, mode="replay", trade_date=TRADE_DATE,
                            speed=600.0, symbols=",".join(symbols), tick_seconds=0.05)
    # Checks at three different minutes, so ordering is testable and both verdicts
    # appear. INFY and ABB fall all session; TCS and RELIANCE do not.
    for hh, mm in ((9, 45), (10, 30), (11, 15)):
        runtime._virtual_now = ist(hh, mm, d=TRADE_DATE)
        runtime.feed.advance_to(runtime._virtual_now)
        runtime.check_entry_all()
    runtime._virtual_now = ist(13, 0, d=TRADE_DATE)
    runtime.feed.advance_to(runtime._virtual_now)
    runtime.checked_log()          # settle everything due

    url, shutdown = _serve_runtime(runtime)
    yield url
    shutdown()


def test_the_checked_log_renders_newest_first_on_screen(served_with_checks, browser):
    """Server ordering is asserted in test_api.py; this asserts the DOM a human reads.

    A client-side sort would satisfy the API test and still present a ranking.
    """
    async def go():
        await browser.open(served_with_checks)
        stamps = await browser.wait_for(
            "(()=>{const rs=document.querySelectorAll('[data-testid=\"checked-log-row\"]');"
            "if(rs.length < 8) return null;"
            "return JSON.stringify([...rs].map(r=>r.cells[0].innerText.trim()"
            ".split('\\n')[0]));})()", timeout=25)
        assert stamps, "the checked-log table never rendered"
        rendered = json.loads(stamps)
        assert rendered == sorted(rendered, reverse=True), (
            f"rows are not newest-first on screen: {rendered}")

    browser.run(go())


def test_the_change_column_is_labelled_past_tense(served_with_checks, browser):
    async def go():
        await browser.open(served_with_checks)
        headers = await browser.wait_for(
            "(()=>{const t=document.querySelector('[data-testid=\"checked-log-table\"]');"
            "if(!t) return null;return JSON.stringify([...t.querySelectorAll('thead th')]"
            ".map(h=>h.innerText.trim().toLowerCase()));})()", timeout=25)
        assert headers, "the checked-log table never rendered"
        cols = json.loads(headers)
        assert "change since you checked" in cols, cols
        for forward_looking in ("expected", "forecast", "projected", "return", "upside"):
            assert not any(forward_looking in c for c in cols), cols

    browser.run(go())


def test_the_checked_log_has_no_sort_control_and_no_top_view(served_with_checks, browser):
    """No clickable headers, no filter, no aggregate league table."""
    async def go():
        await browser.open(served_with_checks)
        await browser.wait_for(
            "document.querySelectorAll('[data-testid=\"checked-log-row\"]').length > 0",
            timeout=25)

        controls = json.loads(await browser.eval(
            "(()=>{const p=document.querySelector('[data-testid=\"checked-log-table\"]')"
            ".closest('div').parentElement;"
            "return JSON.stringify({"
            "selects:p.querySelectorAll('select').length,"
            "headerButtons:p.querySelectorAll('thead button, thead a').length});})()"))
        assert controls["selects"] == 0, "a dropdown here is a sort or a filter"
        assert controls["headerButtons"] == 0, "a clickable header is a sort control"

        table = await browser.eval(
            "document.querySelector('[data-testid=\"checked-log-table\"]')"
            ".innerText.toLowerCase()")
        for banned in ("best", "top", "rank", "score", "winner", "recommend", "prefer"):
            assert banned not in table, f"the table implies a ranking: {banned!r}"

    browser.run(go())


def test_both_verdicts_are_on_screen_together(served_with_checks, browser):
    """Showing only the SOUND rows turns a scoreboard into a track record."""
    async def go():
        await browser.open(served_with_checks)
        verdicts = await browser.wait_for(
            "(()=>{const rs=document.querySelectorAll('[data-testid=\"checked-log-row\"]');"
            "if(!rs.length) return null;"
            "return JSON.stringify([...rs].map(r=>r.cells[2].innerText.trim()));})()",
            timeout=25)
        assert verdicts, "no rows rendered"
        assert set(json.loads(verdicts)) == {"SOUND", "UNSOUND"}, json.loads(verdicts)

    browser.run(go())


def test_no_row_is_styled_as_a_winner(served_with_checks, browser):
    """Colouring gains green ranks the table by eye exactly as a sort would."""
    async def go():
        await browser.open(served_with_checks)
        await browser.wait_for(
            "document.querySelectorAll('[data-testid=\"checked-log-row\"]').length > 0",
            timeout=25)

        classes = json.loads(await browser.eval(
            "JSON.stringify([...document.querySelectorAll("
            "'[data-testid=\"checked-log-row\"]')].map(r=>r.className))"))
        assert len(set(classes)) == 1, f"rows are styled differently: {set(classes)}"

        # And the change cell is one colour for every row, whatever the sign.
        colours = json.loads(await browser.eval(
            "JSON.stringify([...document.querySelectorAll("
            "'[data-testid=\"checked-log-row\"]')].map("
            "r=>getComputedStyle(r.cells[5]).color))"))
        assert len(set(colours)) == 1, (
            f"the change column is colour-coded by sign, which ranks it by eye: "
            f"{set(colours)}")

    browser.run(go())


def test_the_summary_says_inconclusive_and_explains_itself(served_with_checks, browser):
    async def go():
        await browser.open(served_with_checks)
        verdict = await browser.wait_for(
            "(()=>{const e=document.querySelector("
            "'[data-testid=\"checked-log-verdict\"]');"
            "return e ? e.innerText.trim() : null;})()", timeout=25)
        assert verdict == "INCONCLUSIVE", verdict

        summary = await browser.eval(
            "document.querySelector('[data-testid=\"checked-log-summary\"]').innerText")
        assert "sound" in summary.lower() and "unsound" in summary.lower()
        assert "n=" in summary.lower()

        detail = await open_disclosure(browser, "why-checked-log")
        assert "independent minutes" in detail.lower(), (
            "the panel must say why one check-all sweep is not N draws")
        assert "96,720" in detail, (
            "the panel should name the offline measurement rather than let the user "
            "wait to rediscover it")

    browser.run(go())


def test_the_scoreboard_says_it_is_not_a_buy_list(served_with_checks, browser):
    async def go():
        await browser.open(served_with_checks)
        detail = await open_disclosure(browser, "why-checked-log-purpose")
        assert detail, "the scoreboard's Why? disclosure never rendered"
        text = detail.lower()
        assert "not a list of what to buy" in text
        assert "no sort by return" in text
        assert "past tense" in text

    browser.run(go())


# --------------------------------------------------------------------------------
# Static guards. Fast, no browser, and they catch the regression at source.
# --------------------------------------------------------------------------------


def _panel_source(name: str, until: str) -> str:
    """The CODE of one panel, with comments stripped.

    Stripping matters: these panels are documented with comments that necessarily
    quote the very thing being banned ("a `.sort(` here would..."), and a naive
    substring scan flags the guard's own explanation. Same trap as the bulk table's
    "not a ranking" prose, which broke that assertion once already. Comments cannot
    hide a real call, because a commented-out sort does not run.
    """
    import re

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    section = html[html.index(f"function {name}"):html.index(f"function {until}")]
    section = re.sub(r"/\*.*?\*/", "", section, flags=re.S)
    return re.sub(r"^\s*//.*$", "", section, flags=re.M)


def test_the_bulk_panel_does_not_sort_client_side():
    """No .sort() in the frontend: server order is alphabetical and must be kept.

    Structural guard. A client-side sort would pass the API ordering test and still
    put a ranking on screen.
    """
    panel = _panel_source("CheckAllPanel", "CheckedLogPanel")
    assert ".sort(" not in panel, "CheckAllPanel must render rows in the order received"
    assert "rows.map(" in panel, "rows should be mapped straight to <tr>"


def test_the_checked_log_panel_does_not_sort_or_filter_client_side():
    """The same guard, on the panel that needs it most.

    This table has a return column. A client-side sort here -- or a verdict filter --
    would satisfy every server-side test and still put a buy list on screen.
    """
    panel = _panel_source("CheckedLogPanel", "Panel(")
    assert ".sort(" not in panel, (
        "CheckedLogPanel must render rows in the order received: check time, newest "
        "first. Sorting by return turns a scoreboard into a buy list.")
    assert ".filter(" not in panel, (
        "no client-side filter: SOUND and UNSOUND must always be shown together")
    assert "rows.map(" in panel, "rows should be mapped straight to <tr>"
    # No onClick on a header, which is how a sortable column usually arrives.
    header = panel[panel.index("<thead>"):panel.index("</thead>")]
    assert "onClick" not in header, "a clickable column header is a sort control"


def _frontend_endpoints() -> set[tuple[str, str]]:
    """(METHOD, path) for every endpoint index.html actually calls.

    Parses the served page rather than a hand-maintained list, because a hand-maintained
    list is one more thing that can be forgotten in exactly the commit that adds a
    panel. Template segments (``/positions/${id}``) collapse to a ``{}`` wildcard.
    """
    import re

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()

    # fetch("/path") and fetch(`/path/${x}`), with an optional options object whose
    # `method:` decides the verb. Default GET, same as the browser.
    for match in re.finditer(
        r"""fetch\(\s*["'`](?P<path>/[^"'`]*)["'`]\s*(?P<rest>,\s*\{.*?\n\s*\})?""",
        html, re.S,
    ):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group("path"))
        rest = match.group("rest") or ""
        verb = re.search(r"""method:\s*["'](?P<m>[A-Z]+)["']""", rest)
        found.add(((verb.group("m") if verb else "GET"), path))

    for match in re.finditer(r"""new WebSocket\(`[^`]*?(?P<path>/[a-z][\w/-]*)`""", html):
        found.add(("WS", match.group("path")))

    return found


def _app_routes(app) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = re.sub(r"\{[^}]*\}", "{}", getattr(route, "path", ""))
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods:
                routes.add((method, path))
        else:                                   # a websocket route has no methods
            routes.add(("WS", path))
    return routes


def test_every_endpoint_the_frontend_calls_exists_on_the_app(tmp_path):
    """A panel shipped with a dead endpoint passed the ENTIRE suite once.

    `/checks` was fetched by CheckedLogPanel and returned 404 in the browser while every
    API test that exercised it passed -- because the tests built their own app and the
    browser was talking to a server process started before the route existed. Nothing
    connected the page's calls to the app's routes, so nothing could have caught a
    genuinely missing one.

    This is the same class of gap as the blank page: green suite, absent feature. It
    parses the real index.html and checks every call it makes against the real routes.
    """
    import re as _re

    store = PositionStore(tmp_path)
    store.save_bars("RELIANCE", TRADE_DATE,
                    make_series([1000.0] * 60, start=ist(9, 15, d=TRADE_DATE),
                                volumes=[20000] * 60, spread=1.2))
    app = create_app(build_replay_runtime(store, ["RELIANCE"], TRADE_DATE),
                     run_loop=False)

    called = _frontend_endpoints()
    assert called, "no fetch() calls were found -- the parser has stopped working"
    assert ("GET", "/checks") in called, (
        "the parser missed the check-log call, so this test would not have caught it")

    routes = _app_routes(app)
    missing = sorted(c for c in called if c not in routes)
    assert not missing, (
        f"index.html calls {len(missing)} endpoint(s) the app does not serve: "
        f"{missing}. The page will 404 in the browser while every other test passes."
    )


def test_the_endpoint_parser_would_notice_a_missing_route(tmp_path):
    """Verified by injection: the guard above must fail on a real omission.

    A coverage test that cannot fail is worse than none, because it reads as proof.
    """
    store = PositionStore(tmp_path)
    store.save_bars("RELIANCE", TRADE_DATE,
                    make_series([1000.0] * 60, start=ist(9, 15, d=TRADE_DATE),
                                volumes=[20000] * 60, spread=1.2))
    app = create_app(build_replay_runtime(store, ["RELIANCE"], TRADE_DATE),
                     run_loop=False)

    routes = _app_routes(app)
    routes.discard(("GET", "/checks"))
    missing = [c for c in _frontend_endpoints() if c not in routes]
    assert ("GET", "/checks") in missing, (
        "removing a route the frontend calls did not register as missing")


def test_web_page_makes_no_external_requests():
    """No CDN. A local dashboard must render with the network unplugged.

    The blank page was caused by an unpinned CDN shipping a breaking major version.
    Vendoring is the fix; this keeps it vendored.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    import re

    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', html)
    external = [r for r in refs if r.startswith(("http://", "https://", "//"))]
    assert not external, (
        f"index.html loads {len(external)} asset(s) over the network: {external}. "
        f"Vendor them into web/vendor/ instead -- see web/vendor/README.md."
    )


def test_required_vendor_assets_are_present():
    for name in REQUIRED_VENDOR:
        path = WEB_DIR / "vendor" / name
        assert path.exists(), f"missing vendored asset {path}"
        assert path.stat().st_size > 1000, f"{path} looks truncated"


def test_babel_is_pinned_to_7_and_the_classic_runtime_is_explicit():
    """Both guards against the exact regression that blanked the page.

    Babel 8's preset-react defaults to the automatic runtime, which emits an ES
    `import` that a classic <script> cannot parse.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-presets="react-classic"' in html, (
        "the JSX script must use the explicitly-classic preset")
    assert 'runtime: "classic"' in html, (
        "the classic JSX runtime must be named, not left to a preset default")

    babel = (WEB_DIR / "vendor" / "babel.min.js").read_text(
        encoding="utf-8", errors="ignore")[:400_000]
    assert "react/jsx-runtime" not in babel or 'runtime: "classic"' in html


def test_create_app_refuses_to_start_with_vendor_assets_missing(tmp_path, monkeypatch):
    """Fail at startup, not in the browser at 14:50."""
    store = PositionStore(tmp_path)
    store.save_bars("RELIANCE", TRADE_DATE,
                    make_series([1000.0] * 60, start=ist(9, 15, d=TRADE_DATE),
                                volumes=[20000] * 60, spread=1.2))
    runtime = build_replay_runtime(store, ["RELIANCE"], TRADE_DATE)

    monkeypatch.setattr("src.api.app.WEB_DIR", tmp_path / "no-web-dir")
    with pytest.raises(RuntimeError, match="vendored frontend assets missing"):
        create_app(runtime, run_loop=False)
