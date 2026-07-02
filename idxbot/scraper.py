"""IDX scraper built on Playwright.

Why a real browser: www.idx.co.id is fronted by Cloudflare's bot challenge, so
a plain HTTP client gets a 403 "Just a moment..." interstitial. Playwright
drives real Chromium, which solves the JS challenge and earns a `cf_clearance`
cookie. We keep ONE persistent context alive and reuse it for both the
GetAnnouncement JSON call and the PDF downloads, because both endpoints sit
behind the same Cloudflare edge and share that clearance.

The JSON is fetched via `page.request` (an in-browser fetch that inherits the
page's cookies + TLS fingerprint) rather than a separate HTTP client, so it
looks like the same browser that passed the challenge.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:  # for type hints only; not required at runtime
    from playwright.sync_api import Browser, BrowserContext, Playwright

from .models import IDX_BASE, Announcement

log = logging.getLogger("idxbot.scraper")

# The page a human would load to view announcements. Visiting it triggers and
# clears the Cloudflare challenge before we call the JSON API.
WARMUP_URL = f"{IDX_BASE}/en/listed-companies/company-announcements/"
ANNOUNCEMENT_API = f"{IDX_BASE}/primary/ListedCompany/GetAnnouncement"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# cf_clearance typically lasts ~30 min; re-warm a bit earlier so we never poll
# with a stale cookie and eat a 403 mid-cycle.
CLEARANCE_TTL_SECONDS = 1500

# Titles Cloudflare uses while the challenge is still unsolved.
_CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser")


class ScrapeError(RuntimeError):
    """Raised when IDX cannot be reached or returns an unusable response."""


class IdxScraper:
    """Owns a persistent browser context; use as a context manager."""

    def __init__(self, page_size: int = 20, lang: str = "id", headless: bool = True):
        self._page_size = page_size
        self._lang = lang
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page = None  # persistent page kept on the IDX origin
        self._warmed_at: float = 0.0  # monotonic time of last successful warmup

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "IdxScraper":
        # Imported here so `import idxbot.scraper` (and thus the poller/CLI)
        # works without Playwright installed — only starting the browser needs it.
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._ctx = self._browser.new_context(
            user_agent=USER_AGENT,
            locale="id-ID",
            viewport={"width": 1366, "height": 768},
        )
        self._ctx.set_default_timeout(45_000)
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._page, self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- internal ----------------------------------------------------------
    def _warmup(self) -> None:
        """Load the announcements page so Cloudflare grants clearance, and keep
        that page open.

        We reuse this one page for every subsequent request so that all fetches
        run through Chromium's real network stack (correct TLS/HTTP fingerprint
        + the cf_clearance cookie). This is the whole reason a plain HTTP client
        — including Playwright's context.request — gets 403'd: Cloudflare
        fingerprints the connection, not just the cookie.
        """
        assert self._ctx is not None
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        from playwright.sync_api import Error as PlaywrightError

        try:
            if self._page is None or self._page.is_closed():
                self._page = self._ctx.new_page()
            self._page.goto(WARMUP_URL, wait_until="domcontentloaded")
            # Give Cloudflare's JS challenge time to run and redirect back.
            try:
                self._page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeoutError:
                pass  # networkidle is best-effort; clearance may already be set
        except PlaywrightError as exc:
            # Navigation/browser failure is transient from the poller's view;
            # surface it as ScrapeError so the loop retries instead of dying.
            self._page = None
            raise ScrapeError(f"Warmup navigation failed: {exc}") from exc

        # Poll the title until the challenge interstitial is gone (max ~15s).
        # networkidle alone can fire while "Just a moment..." is still up.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                title = (self._page.title() or "").lower()
            except PlaywrightError as exc:
                self._page = None
                raise ScrapeError(f"Warmup title check failed: {exc}") from exc
            if not any(marker in title for marker in _CHALLENGE_TITLES):
                self._warmed_at = time.monotonic()
                return
            self._page.wait_for_timeout(500)
        raise ScrapeError("Cloudflare challenge did not clear during warmup.")

    def reset_clearance(self) -> None:
        """Force a fresh Cloudflare warmup on the next fetch."""
        self._warmed_at = 0.0

    def _clearance_fresh(self) -> bool:
        return (
            self._warmed_at > 0
            and (time.monotonic() - self._warmed_at) < CLEARANCE_TTL_SECONDS
        )

    def _ensure_ready(self) -> None:
        if self._ctx is None:
            raise ScrapeError("Scraper used outside its context manager.")
        if self._page is None or self._page.is_closed() or not self._clearance_fresh():
            self._warmup()

    def _page_eval(self, script: str, arg) -> dict:
        """Run JS in the warmed page, converting Playwright crashes into
        ScrapeError so the poll loop can recover instead of dying."""
        from playwright.sync_api import Error as PlaywrightError

        try:
            result = self._page.evaluate(script, arg)
        except PlaywrightError as exc:
            # Page/renderer crashed or navigated away; rebuild on next call.
            self._page = None
            self.reset_clearance()
            raise ScrapeError(f"In-page fetch failed: {exc}") from exc
        if not isinstance(result, dict):
            raise ScrapeError("In-page fetch returned an unexpected result.")
        return result

    # -- public API --------------------------------------------------------
    def fetch_announcements(self) -> list[Announcement]:
        """Return the latest announcements, newest first.

        The request is issued from *inside* the warmed page via fetch(), so it
        uses Chromium's real network stack (right fingerprint + cf_clearance).
        """
        self._ensure_ready()

        params = {
            "indexFrom": "1",
            "pageSize": str(self._page_size),
            "dateFrom": "",
            "dateTo": "",
            "lang": self._lang,
            "keyword": "",
        }
        url = f"{ANNOUNCEMENT_API}?{urlencode(params)}"
        fetch_js = """async (u) => {
            const r = await fetch(u, {
                headers: {'X-Requested-With': 'XMLHttpRequest'},
                credentials: 'include',
            });
            return { status: r.status, body: await r.text() };
        }"""
        result = self._page_eval(fetch_js, url)

        status = result.get("status")
        if status in (403, 429, 503):
            # Clearance expired / rate limited: re-warm once and retry in the
            # same cycle so a stale cookie doesn't cost a whole poll interval.
            log.info("GetAnnouncement HTTP %s; re-warming clearance and retrying.", status)
            self.reset_clearance()
            self._ensure_ready()
            result = self._page_eval(fetch_js, url)
            status = result.get("status")
        if status != 200:
            if status in (403, 429, 503):
                self.reset_clearance()
            raise ScrapeError(f"IDX GetAnnouncement returned HTTP {status}.")

        try:
            data = json.loads(result.get("body") or "")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ScrapeError(f"IDX response was not JSON: {exc}") from exc

        rows = self._extract_rows(data)
        return [Announcement.from_idx_row(r) for r in rows]

    @staticmethod
    def _extract_rows(data: object) -> list[dict]:
        """Pull the item list out of IDX's response, tolerating shape changes."""
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("Items", "items", "Replies", "data", "Data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
        return []

    def download_pdf(self, url: str, dest_path: str, max_bytes: int) -> bool:
        """Download a PDF through the warmed page. Returns False if skipped.

        Uses in-page fetch() + arrayBuffer so the download shares the browser's
        Cloudflare clearance. Skips (returns False) when the file exceeds
        max_bytes or the response is not a PDF; raises ScrapeError on transport
        failure so the caller can still send a link-only alert.
        """
        self._ensure_ready()

        # Content-Length is checked inside the page so oversized files are
        # aborted before their bytes are pulled into Python at all.
        fetch_js = """async (arg) => {
            const r = await fetch(arg.url, { credentials: 'include' });
            if (r.status !== 200) return { status: r.status, b64: '', tooBig: false };
            const len = parseInt(r.headers.get('content-length') || '0', 10);
            if (len > arg.maxBytes) return { status: 200, b64: '', tooBig: true, size: len };
            const buf = await r.arrayBuffer();
            if (buf.byteLength > arg.maxBytes)
                return { status: 200, b64: '', tooBig: true, size: buf.byteLength };
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            return { status: r.status, b64: btoa(binary), tooBig: false };
        }"""
        arg = {"url": url, "maxBytes": max_bytes}
        result = self._page_eval(fetch_js, arg)

        status = result.get("status")
        if status in (403, 429, 503):
            log.info("PDF download HTTP %s; re-warming clearance and retrying.", status)
            self.reset_clearance()
            self._ensure_ready()
            result = self._page_eval(fetch_js, arg)
            status = result.get("status")
        if status != 200:
            if status in (403, 429, 503):
                self.reset_clearance()
            raise ScrapeError(f"PDF download returned HTTP {status}.")

        if result.get("tooBig"):
            log.info("PDF %s skipped: %s bytes > limit %d", url, result.get("size"), max_bytes)
            return False

        body = base64.b64decode(result.get("b64") or "")
        if not body:
            raise ScrapeError("PDF download returned an empty body.")
        # Guard against Cloudflare HTML sneaking through with a 200.
        if body[:5] != b"%PDF-":
            head = body[:512].lower()
            if b"<html" in head or b"<!doctype" in head:
                self.reset_clearance()
                raise ScrapeError("Expected PDF but got HTML (challenge?).")
            log.warning("PDF %s lacks %%PDF- magic; sending anyway.", url)

        with open(dest_path, "wb") as fh:
            fh.write(body)
        return True
