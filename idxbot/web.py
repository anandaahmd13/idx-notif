"""Halaman status/monitoring ringan.

Menjalankan HTTP server dari stdlib (`http.server`) di thread daemon, di dalam
proses bot yang sama, sehingga ia bisa membaca `BotStats` yang sedang di-update
oleh poll loop secara live. Tanpa dependensi eksternal (tidak ada Flask).

Endpoint:
  GET /            -> halaman HTML status (auto-refresh via fetch /api/status)
  GET /api/status  -> JSON snapshot metrik
  GET /healthz     -> 200 bila sehat, 503 bila tidak (untuk uptime-monitor/Docker)

Tanpa autentikasi: di VPS publik, bind ke 127.0.0.1 dan akses via SSH tunnel.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .stats import BotStats

log = logging.getLogger("idxbot.web")

_PAGE = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IDX Alert Bot — Status</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.3rem; }
  .badge { display: inline-block; padding: .2rem .6rem; border-radius: .4rem;
           font-weight: 600; color: #fff; }
  .ok { background: #16a34a; }
  .bad { background: #dc2626; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  td { padding: .4rem .2rem; border-bottom: 1px solid #8883; vertical-align: top; }
  td:first-child { color: #888; width: 45%; }
  .err { color: #dc2626; white-space: pre-wrap; word-break: break-word; }
  footer { margin-top: 1.5rem; color: #888; font-size: .85rem; }
</style>
</head>
<body>
  <h1>🚨 IDX Alert Bot <span id="badge" class="badge">…</span></h1>
  <table id="tbl"></table>
  <footer>Auto-refresh tiap 5 detik · <a href="/api/status">/api/status</a></footer>
<script>
const FIELDS = [
  ["uptime", "Uptime"],
  ["market_open", "Jam bursa"],
  ["current_interval_seconds", "Interval poll (dtk)"],
  ["poll_count", "Total poll"],
  ["total_alerts", "Total alert terkirim"],
  ["consecutive_failures", "Kegagalan beruntun"],
  ["retry_queue_size", "Antrian retry"],
  ["last_poll_at", "Poll terakhir"],
  ["last_success_at", "Sukses terakhir"],
  ["last_error", "Error terakhir"],
  ["last_error_at", "Waktu error"],
  ["started_at", "Mulai"],
];
function fmtUptime(s) {
  if (s == null) return "-";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60);
  return `${d}h ${h}j ${m}m`;
}
function fmtTime(v) {
  if (!v) return "-";
  try { return new Date(v).toLocaleString("id-ID"); } catch { return v; }
}
async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    const s = await r.json();
    const badge = document.getElementById("badge");
    badge.textContent = s.healthy ? "SEHAT" : "BERMASALAH";
    badge.className = "badge " + (s.healthy ? "ok" : "bad");
    const rows = FIELDS.map(([k, label]) => {
      let v = s[k];
      if (k === "uptime") v = fmtUptime(s.uptime_seconds);
      else if (k === "market_open") v = v === true ? "BUKA" : v === false ? "tutup" : "-";
      else if (k.endsWith("_at") || k === "started_at") v = fmtTime(v);
      else if (v == null) v = "-";
      const cls = k === "last_error" && v !== "-" ? ' class="err"' : "";
      return `<tr><td>${label}</td><td${cls}>${v}</td></tr>`;
    }).join("");
    document.getElementById("tbl").innerHTML = rows;
  } catch (e) {
    const badge = document.getElementById("badge");
    badge.textContent = "TAK TERHUBUNG";
    badge.className = "badge bad";
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def _make_handler(stats: BotStats):
    class StatusHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — required name
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                body = json.dumps(stats.snapshot()).encode("utf-8")
                self._send(200, body, "application/json")
            elif path == "/healthz":
                healthy = stats.is_healthy()
                self._send(
                    200 if healthy else 503,
                    b"ok" if healthy else b"unhealthy",
                    "text/plain",
                )
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, fmt: str, *args) -> None:
            # Redam access-log bawaan agar tidak membanjiri stdout bot.
            log.debug("web: " + fmt, *args)

    return StatusHandler


def start_status_server(stats: BotStats, host: str, port: int) -> ThreadingHTTPServer:
    """Start the status server on a daemon thread and return it.

    Daemon thread => tidak menahan proses saat bot berhenti (Ctrl+C).
    """
    server = ThreadingHTTPServer((host, port), _make_handler(stats))
    thread = threading.Thread(
        target=server.serve_forever, name="idxbot-web", daemon=True
    )
    thread.start()
    log.info("Halaman status aktif di http://%s:%d/", host, port)
    return server
