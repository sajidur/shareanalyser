"""User settings stored in MySQL (editable from the Settings page)."""
from __future__ import annotations

from . import db

DEFAULTS: dict[str, str] = {
    # Broker commission per side, in percent of trade value (typical BD brokers: 0.30-0.50%).
    "commission_rate": "0.40",
    # Daily % move treated as "near circuit-breaker limit".
    "circuit_pct": "9.5",
    "notify_toast": "1",
    "notify_telegram": "0",
    "telegram_token": "",
    "telegram_chat_id": "",
    "intraday_alerts": "1",
    "dashboard_url": "http://127.0.0.1:8050",
    # position sizing for trade plans
    "capital": "0",              # Tk available for trading (0 = do not size positions)
    "risk_pct": "1.0",           # max % of capital lost if a stop-loss is hit
    "max_position_pct": "20",    # max % of capital in one share
    "ai_daily_minutes": "10",    # time budget for AI news scoring in the end-of-day run
    "ai_profile_minutes": "10",  # time budget for AI company health checks in the end-of-day run
    # add your recorded political links (party / MP / minister ...) to the forecast; off by default
    # because the companies are chosen with hindsight
    "use_political_records": "0",
}


def get_all() -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.query("SELECT k, v FROM settings"):
        if row["k"] in DEFAULTS and row["v"] is not None:
            values[row["k"]] = row["v"]
    return values


def get(key: str) -> str:
    return get_all().get(key, DEFAULTS.get(key, ""))


def get_float(key: str) -> float:
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def update(values: dict[str, object]) -> dict[str, str]:
    rows = [(k, "" if v is None else str(v).strip()) for k, v in values.items() if k in DEFAULTS]
    db.executemany(
        "INSERT INTO settings (k, v) VALUES (%s, %s) ON DUPLICATE KEY UPDATE v=VALUES(v)", rows
    )
    return get_all()
