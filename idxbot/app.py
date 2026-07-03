"""Dashboard FastAPI — feed realtime (SSE) + statistik untuk bot IDX.

Menjalankan poll loop di thread daemon di dalam proses ini, jadi pengumuman
baru langsung di-push ke browser tanpa polling. Reuse Poller/scraper/state apa
adanya; menambah HistoryStore (SQLite) + EventBroadcaster (SSE).

Jalankan:  python -m idxbot.app       (atau via config dashboard.enabled)

Keamanan: Basic Auth dari env DASHBOARD_USER / DASHBOARD_PASS. Bila host bukan
loopback dan kredensial kosong, app menolak start (fail-safe, tidak diam-diam
terbuka ke publik).
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Config, ConfigError
from .db import HistoryStore
from .events import EventBroadcaster, format_sse
from .poller import Poller
from .stats import BotStats

log = logging.getLogger("idxbot.app")

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


def _render_dashboard(channel_title: str) -> str:
    """Render template satu-variabel via replace sederhana.

    Sengaja tanpa Jinja2: templatenya statis + satu placeholder, dan Jinja2
    LRUCache bentrok di Python 3.14. HTML escape agar aman dari injeksi.
    """
    import html

    tpl = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return tpl.replace("__CHANNEL_TITLE__", html.escape(channel_title or ""))


def _auth_credentials() -> tuple[str, str] | None:
    user = os.getenv("DASHBOARD_USER", "")
    pw = os.getenv("DASHBOARD_PASS", "")
    if user and pw:
        return user, pw
    return None


def create_app(cfg: Config) -> FastAPI:
    stats = BotStats()
    history = HistoryStore(cfg.dashboard.db_path)
    broadcaster = EventBroadcaster()
    stop_event = threading.Event()
    creds = _auth_credentials()

    # Fail-safe: menolak terbuka ke non-loopback tanpa autentikasi.
    if cfg.dashboard.host not in _LOOPBACK and creds is None:
        raise ConfigError(
            "Dashboard di-bind ke host publik tanpa kredensial. Set DASHBOARD_USER "
            "dan DASHBOARD_PASS di .env, atau bind ke 127.0.0.1."
        )

    security = HTTPBasic(auto_error=False)

    def require_auth(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> None:
        if creds is None:
            return  # loopback tanpa kredensial -> akses bebas (lihat fail-safe)
        user, pw = creds
        ok = (
            credentials is not None
            and secrets.compare_digest(credentials.username, user)
            and secrets.compare_digest(credentials.password, pw)
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        seen_path = os.getenv("IDX_SEEN_PATH", "seen.json")
        poller = Poller(
            cfg,
            seen_path=seen_path,
            stats=stats,
            recorder=history,
            broadcaster=broadcaster,
            stop_event=stop_event,
        )
        thread = threading.Thread(
            target=poller.run_forever, name="idxbot-poller", daemon=True
        )
        thread.start()
        log.info("Poller thread dimulai; dashboard siap.")
        try:
            yield
        finally:
            stop_event.set()  # minta loop berhenti; sleep interruptible bangun segera
            thread.join(timeout=15)
            history.close()
            log.info("Poller dihentikan; dashboard shutdown.")

    app = FastAPI(title="IDX Alert Dashboard", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index(_: None = Depends(require_auth)) -> HTMLResponse:
        return HTMLResponse(_render_dashboard(cfg.telegram.channel_title))

    @app.get("/api/status")
    def api_status(_: None = Depends(require_auth)) -> dict:
        snap = stats.snapshot()
        snap["total_recorded"] = history.count_total()
        snap["subscribers"] = broadcaster.subscriber_count
        return snap

    @app.get("/api/alerts")
    def api_alerts(_: None = Depends(require_auth)) -> dict:
        return {"alerts": history.recent(cfg.dashboard.recent_limit)}

    @app.get("/api/charts")
    def api_charts(_: None = Depends(require_auth)) -> dict:
        return {
            "top_emiten": history.top_emiten(10),
            "per_day": history.per_day(14),
        }

    @app.get("/healthz")
    def healthz() -> dict:
        # Tanpa auth: untuk uptime-monitor. Hanya status sehat/tidak.
        return {"healthy": stats.is_healthy()}

    @app.get("/events")
    def events(_: None = Depends(require_auth)) -> StreamingResponse:
        """Aliran SSE: alert baru di-push begitu terkirim."""
        q = broadcaster.subscribe()

        def gen():
            try:
                # Sapaan awal agar koneksi langsung terbuka di browser.
                yield format_sse("hello", {"ok": True})
                while True:
                    payload = q.get()  # blok sampai ada event
                    yield format_sse(payload["event"], payload["data"])
            finally:
                broadcaster.unsubscribe(q)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # cegah buffering di balik nginx
        }
        return StreamingResponse(
            gen(), media_type="text/event-stream", headers=headers
        )

    return app


def main() -> int:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        cfg = Config.load()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    try:
        app = create_app(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    uvicorn.run(app, host=cfg.dashboard.host, port=cfg.dashboard.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
