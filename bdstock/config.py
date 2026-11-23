"""Configuration loaded from config.ini (MySQL connection, server, monitor options)."""
from __future__ import annotations

import configparser
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("BDSTOCK_CONFIG", BASE_DIR / "config.ini"))
LOG_DIR = BASE_DIR / "logs"

DEFAULTS = {
    "mysql": {
        "host": "127.0.0.1",
        "port": "3306",
        "user": "root",
        "password": "",
        "database": "bd_stock",
    },
    "server": {"host": "127.0.0.1", "port": "8050"},
    "monitor": {
        # How often prices are refreshed while the market is open (server running).
        "intraday_interval_minutes": "5",
        # End-of-day analysis runs this many minutes after DSE's post-closing session.
        "eod_delay_minutes": "10",
        # Pause between company-page requests during history backfill (be polite to DSE).
        "backfill_delay_seconds": "0.8",
    },
    "dse": {"base_url": "https://www.dse.com.bd", "timeout_seconds": "30"},
}

_cache: configparser.ConfigParser | None = None


def load(reload: bool = False) -> configparser.ConfigParser:
    global _cache
    if _cache is None or reload:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if CONFIG_PATH.exists():
            cp.read(CONFIG_PATH, encoding="utf-8")
        _cache = cp
    return _cache


def save(cp: configparser.ConfigParser) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)
    load(reload=True)


def get(section: str, key: str) -> str:
    return load().get(section, key)


def getint(section: str, key: str) -> int:
    return load().getint(section, key)


def getfloat(section: str, key: str) -> float:
    return load().getfloat(section, key)


def setup_logging(console: bool = True, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_bdstock_configured", False):
        return
    LOG_DIR.mkdir(exist_ok=True)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "bdstock.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stderr is not None:  # pythonw has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    logging.getLogger("mysql.connector").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    root._bdstock_configured = True  # type: ignore[attr-defined]
