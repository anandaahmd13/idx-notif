"""Telegram delivery.

Formats alerts to match the target layout:

    🚨 ALERT PENGUMUMAN BEI

    Keyword: Penambahan Modal
    Emiten: PEGE

    Subject: <judul pengumuman>
    Time: <publish time>

    Link:
    <pdf url>

When a PDF is downloaded it is sent via sendDocument with the alert text as the
caption; otherwise the text is sent via sendMessage (link-only fallback).
Uses the Telegram Bot HTTP API directly — no heavy client dependency.
"""
from __future__ import annotations

import html
import logging
import time

import requests

from .config import TelegramConfig
from .filters import MatchResult
from .models import Announcement

log = logging.getLogger("idxbot.telegram")

API_ROOT = "https://api.telegram.org"
# Telegram caption cap is 1024 chars; keep headroom for the file.
MAX_CAPTION = 1024
MAX_MESSAGE = 4096
# Attempts per API call: transient network errors and 429s are retried with
# backoff so one hiccup doesn't drop an alert (announcements are marked seen
# before delivery, so a dropped send is lost forever).
MAX_ATTEMPTS = 3


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, timeout: int = 30):
        self._token = cfg.bot_token
        self._chat_id = cfg.chat_id
        self._timeout = timeout
        self._session = requests.Session()

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token}/{method}"

    def build_text(self, ann: Announcement, match: MatchResult) -> str:
        """Render the alert body as HTML (parse_mode=HTML)."""
        keyword = match.keyword or "-"
        link = ann.primary_link or "-"
        lines = [
            "🚨 <b>ALERT PENGUMUMAN BEI</b>",
            "",
            f"Keyword: {html.escape(keyword)}",
            f"Emiten: {html.escape(ann.emiten or '-')}",
            "",
            f"Subject: {html.escape(ann.title or '-')}",
            f"Time: {html.escape(ann.published or '-')}",
            "",
            "Link:",
            html.escape(link),
        ]
        return "\n".join(lines)

    def send(self, ann: Announcement, match: MatchResult, pdf_path: str | None) -> bool:
        """Send one alert. Returns True on success.

        If pdf_path is given, send the file with the text as caption; else send
        a plain text message. Falls back to a text message if file upload fails.
        """
        text = self.build_text(ann, match)

        if pdf_path:
            caption = text if len(text) <= MAX_CAPTION else text[: MAX_CAPTION - 1] + "…"
            try:
                if self._post_with_retry(
                    "sendDocument",
                    data={
                        "chat_id": self._chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    file_path=pdf_path,
                    file_name=_safe_name(ann),
                ):
                    return True
                log.warning("sendDocument failed, falling back to text message.")
            except OSError as exc:
                log.warning("Could not read PDF %s (%s); sending text only.", pdf_path, exc)

        return self._send_text(text)

    def _send_text(self, text: str) -> bool:
        body = text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"
        return self._post_with_retry(
            "sendMessage",
            data={
                "chat_id": self._chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def _post_with_retry(
        self,
        method: str,
        data: dict,
        file_path: str | None = None,
        file_name: str = "document.pdf",
    ) -> bool:
        """POST to the Bot API, retrying timeouts/connection errors and 429s.

        Honors Telegram's `retry_after` on 429. Other API errors (400 bad
        chat, 403 kicked, oversized file) are NOT retried — they won't heal.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if file_path:
                    with open(file_path, "rb") as fh:
                        resp = self._session.post(
                            self._url(method),
                            data=data,
                            files={"document": (file_name, fh, "application/pdf")},
                            timeout=self._timeout,
                        )
                else:
                    resp = self._session.post(
                        self._url(method), data=data, timeout=self._timeout
                    )
            except requests.RequestException as exc:
                log.warning(
                    "Telegram %s network error (attempt %d/%d): %s",
                    method, attempt, MAX_ATTEMPTS, exc,
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                continue

            if self._ok(resp, method):
                return True
            if resp.status_code == 429 and attempt < MAX_ATTEMPTS:
                retry_after = self._retry_after(resp)
                log.warning("Telegram rate limit; retrying in %ss.", retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            return False  # permanent API error; retrying won't help
        return False

    @staticmethod
    def _retry_after(resp: requests.Response) -> float:
        try:
            payload = resp.json()
            return min(float(payload["parameters"]["retry_after"]), 60.0)
        except (ValueError, KeyError, TypeError):
            return 5.0

    def send_startup_ping(self) -> bool:
        """Verify the token/chat wiring at boot with a small message."""
        return self._send_text("✅ IDX Alert Bot online — watching for announcements.")

    @staticmethod
    def _ok(resp: requests.Response, method: str) -> bool:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code == 200 and payload.get("ok"):
            return True
        log.error(
            "Telegram %s HTTP %s: %s",
            method,
            resp.status_code,
            payload.get("description", resp.text[:200]),
        )
        return False


def _safe_name(ann: Announcement) -> str:
    """Filename for the uploaded document, preferring IDX's own name."""
    if ann.attachments and ann.attachments[0].filename:
        return ann.attachments[0].filename
    stem = (ann.emiten or "idx").replace("/", "_")
    return f"{stem}.pdf"
