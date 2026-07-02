"""CLI entrypoint.

Usage:
    python -m idxbot            # run the polling loop
    python -m idxbot --check    # validate config + Telegram wiring, then exit
    python -m idxbot --once     # run a single poll cycle (useful for testing)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import Config, ConfigError
from .poller import Poller
from .telegram import TelegramNotifier


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="idxbot", description="IDX announcement alert bot.")
    parser.add_argument("--config", help="Path to config.yaml (overrides IDX_CONFIG env).")
    parser.add_argument("--check", action="store_true", help="Validate config + Telegram, then exit.")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit.")
    parser.add_argument("--no-ping", action="store_true", help="Skip the startup Telegram ping.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log = logging.getLogger("idxbot")

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    if args.check:
        notifier = TelegramNotifier(cfg.telegram)
        ok = notifier.send_startup_ping()
        log.info("Telegram check %s.", "passed" if ok else "FAILED")
        return 0 if ok else 1

    seen_path = os.getenv("IDX_SEEN_PATH", "seen.json")
    poller = Poller(cfg, seen_path=seen_path)

    if not args.no_ping:
        TelegramNotifier(cfg.telegram).send_startup_ping()

    if args.once:
        from .scraper import IdxScraper

        with IdxScraper(page_size=cfg.poll.page_size, lang=cfg.poll.lang) as scraper:
            poller._poll_once(scraper)  # single cycle for smoke-testing
        return 0

    try:
        poller.run_forever()
    except KeyboardInterrupt:
        log.info("Shutting down (interrupted).")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
