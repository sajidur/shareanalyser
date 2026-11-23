"""MySQL access: connection pool, schema, and small query helpers."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterable

import mysql.connector
from mysql.connector import pooling

from . import config

_pool: pooling.MySQLConnectionPool | None = None
_pool_lock = threading.Lock()
_schema_ready = False

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS instruments (
        code VARCHAR(32) NOT NULL PRIMARY KEY,
        name VARCHAR(255) NULL,
        sector VARCHAR(64) NULL,
        category VARCHAR(8) NULL,
        board VARCHAR(16) NULL,
        asset_type VARCHAR(16) NULL,
        trailing_pe DOUBLE NULL,
        history_synced_at DATETIME NULL,
        updated_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS daily_prices (
        code VARCHAR(32) NOT NULL,
        trade_date DATE NOT NULL,
        open DOUBLE NULL, high DOUBLE NULL, low DOUBLE NULL, close DOUBLE NULL,
        ltp DOUBLE NULL, ycp DOUBLE NULL,
        volume BIGINT NOT NULL DEFAULT 0,
        value_mn DOUBLE NULL,
        trades INT NOT NULL DEFAULT 0,
        change_pct DOUBLE NULL,
        source VARCHAR(16) NULL,
        PRIMARY KEY (code, trade_date),
        KEY idx_prices_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS live_quotes (
        code VARCHAR(32) NOT NULL PRIMARY KEY,
        trade_date DATE NULL,
        ltp DOUBLE NULL, ycp DOUBLE NULL, open DOUBLE NULL, high DOUBLE NULL,
        low DOUBLE NULL, close DOUBLE NULL,
        volume BIGINT NOT NULL DEFAULT 0, value_mn DOUBLE NULL, trades INT NOT NULL DEFAULT 0,
        change_pct DOUBLE NULL,
        updated_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS index_daily (
        code VARCHAR(16) NOT NULL,
        trade_date DATE NOT NULL,
        value DOUBLE NULL, change_abs DOUBLE NULL, change_pct DOUBLE NULL,
        PRIMARY KEY (code, trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS market_daily (
        trade_date DATE NOT NULL PRIMARY KEY,
        turnover_mn DOUBLE NULL, volume BIGINT NULL, trades INT NULL,
        advanced INT NULL, declined INT NULL, unchanged INT NULL,
        market_cap DOUBLE NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS synced_dates (
        trade_date DATE NOT NULL PRIMARY KEY,
        source VARCHAR(16) NULL,
        synced_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS company_info (
        code VARCHAR(32) NOT NULL PRIMARY KEY,
        name VARCHAR(255) NULL,
        pe DOUBLE NULL, eps DOUBLE NULL, nav DOUBLE NULL, dividend_yield DOUBLE NULL,
        market_cap DOUBLE NULL, free_float DOUBLE NULL,
        week_high52 DOUBLE NULL, week_low52 DOUBLE NULL,
        face_value DOUBLE NULL, paid_up_capital DOUBLE NULL,
        listing_year INT NULL, year_end VARCHAR(32) NULL,
        extra_json MEDIUMTEXT NULL,
        updated_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS signals (
        code VARCHAR(32) NOT NULL,
        trade_date DATE NOT NULL,
        close DOUBLE NULL, change_pct DOUBLE NULL,
        score INT NULL, label VARCHAR(24) NULL,
        rsi DOUBLE NULL, macd_hist DOUBLE NULL,
        sma20 DOUBLE NULL, sma50 DOUBLE NULL, sma200 DOUBLE NULL,
        vol_ratio DOUBLE NULL, high52 DOUBLE NULL, low52 DOUBLE NULL,
        ret_1w DOUBLE NULL, ret_1m DOUBLE NULL, ret_3m DOUBLE NULL, ret_1y DOUBLE NULL,
        flags VARCHAR(512) NULL,
        reasons TEXT NULL,
        PRIMARY KEY (code, trade_date),
        KEY idx_signals_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS predictions (
        code VARCHAR(32) NOT NULL,
        trade_date DATE NOT NULL,
        horizon INT NOT NULL,
        close DOUBLE NULL,
        prob_up DOUBLE NULL,
        exp_ret DOUBLE NULL,
        sigma DOUBLE NULL,
        median DOUBLE NULL, low68 DOUBLE NULL, high68 DOUBLE NULL, low90 DOUBLE NULL, high90 DOUBLE NULL,
        atr DOUBLE NULL,
        liq_mn DOUBLE NULL,
        hist_up DOUBLE NULL,
        drivers TEXT NULL,
        source VARCHAR(16) NOT NULL DEFAULT 'live',
        PRIMARY KEY (code, trade_date, horizon),
        KEY idx_pred_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS prediction_checks (
        check_date DATE NOT NULL,
        horizon INT NOT NULL,
        source VARCHAR(16) NOT NULL,
        made_on DATE NULL,
        n INT NULL,
        in68 DOUBLE NULL, in90 DOUBLE NULL,
        dir_n INT NULL, dir_hits INT NULL,
        mae_pct DOUBLE NULL, bias_pct DOUBLE NULL,
        PRIMARY KEY (check_date, horizon, source)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS news (
        uid CHAR(40) NOT NULL PRIMARY KEY,
        code VARCHAR(32) NOT NULL,
        news_date DATE NOT NULL,
        news_time VARCHAR(8) NULL,
        type VARCHAR(32) NULL,
        summary VARCHAR(500) NULL,
        body TEXT NULL,
        tags VARCHAR(255) NULL,
        tone DOUBLE NULL,
        eps_growth DOUBLE NULL,
        record_date DATE NULL,
        source VARCHAR(16) NULL,
        fetched_at DATETIME NULL,
        KEY idx_news_code_date (code, news_date),
        KEY idx_news_date (news_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS market_events (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        event_date DATE NOT NULL,
        kind VARCHAR(16) NOT NULL,
        title VARCHAR(200) NOT NULL,
        impact TINYINT NULL,
        note VARCHAR(500) NULL,
        created_at DATETIME NULL,
        KEY idx_events_date (event_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS signal_log (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        signal_date DATE NOT NULL,
        code VARCHAR(32) NOT NULL,
        signal_type VARCHAR(16) NOT NULL,
        close_at_signal DOUBLE NULL,
        entry_limit DOUBLE NULL, stop DOUBLE NULL, t1 DOUBLE NULL, t2 DOUBLE NULL,
        exit_by DATE NULL,
        win_rate DOUBLE NULL, ev_pct DOUBLE NULL,
        status VARCHAR(12) NOT NULL DEFAULT 'pending',
        entry_date DATE NULL, entry_price DOUBLE NULL,
        exit_date DATE NULL, exit_price DOUBLE NULL,
        return_pct DOUBLE NULL, max_gain_pct DOUBLE NULL, max_loss_pct DOUBLE NULL, sessions INT NULL,
        UNIQUE KEY uq_signal (signal_date, code, signal_type),
        KEY idx_signal_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS ai_notes (
        code VARCHAR(32) NOT NULL,
        trade_date DATE NOT NULL,
        model VARCHAR(64) NULL,
        note MEDIUMTEXT NULL,
        created_at DATETIME NULL,
        PRIMARY KEY (code, trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS watchlist (
        code VARCHAR(32) NOT NULL PRIMARY KEY,
        note VARCHAR(255) NULL,
        added_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS transactions (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(32) NOT NULL,
        side VARCHAR(4) NOT NULL,
        qty INT NOT NULL,
        price DOUBLE NOT NULL,
        fee DOUBLE NOT NULL DEFAULT 0,
        trade_date DATE NOT NULL,
        note VARCHAR(255) NULL,
        created_at DATETIME NULL,
        KEY idx_tx_code (code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS alerts (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(32) NOT NULL,
        kind VARCHAR(24) NOT NULL,
        threshold DOUBLE NULL,
        note VARCHAR(255) NULL,
        repeat_daily TINYINT NOT NULL DEFAULT 1,
        active TINYINT NOT NULL DEFAULT 1,
        created_at DATETIME NULL,
        last_triggered_on DATE NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS alert_events (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        alert_id INT NULL,
        code VARCHAR(32) NOT NULL,
        kind VARCHAR(24) NOT NULL,
        message VARCHAR(512) NULL,
        value DOUBLE NULL,
        trade_date DATE NULL,
        triggered_at DATETIME NULL,
        KEY idx_events_time (triggered_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS daily_reports (
        report_date DATE NOT NULL PRIMARY KEY,
        data MEDIUMTEXT NULL,
        notified TINYINT NOT NULL DEFAULT 0,
        created_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS job_runs (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        job VARCHAR(32) NOT NULL,
        started_at DATETIME NULL,
        finished_at DATETIME NULL,
        status VARCHAR(16) NULL,
        message TEXT NULL,
        KEY idx_job_runs (job, started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS settings (
        k VARCHAR(64) NOT NULL PRIMARY KEY,
        v TEXT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS app_state (
        k VARCHAR(64) NOT NULL PRIMARY KEY,
        v MEDIUMTEXT NULL,
        updated_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


# Columns added after a table was first created: (table, column, column DDL)
MIGRATIONS = [
    ("predictions", "hist_up", "hist_up DOUBLE NULL AFTER liq_mn"),
    ("predictions", "source", "source VARCHAR(16) NOT NULL DEFAULT 'live' AFTER drivers"),
    ("predictions", "low50", "low50 DOUBLE NULL AFTER median"),
    ("predictions", "high50", "high50 DOUBLE NULL AFTER low50"),
    ("prediction_checks", "in50", "in50 DOUBLE NULL AFTER n"),
    ("prediction_checks", "width50", "width50 DOUBLE NULL AFTER bias_pct"),
    ("prediction_checks", "width68", "width68 DOUBLE NULL AFTER width50"),
    ("news", "ai_impact", "ai_impact TINYINT NULL AFTER record_date"),
    ("news", "ai_reason", "ai_reason VARCHAR(300) NULL AFTER ai_impact"),
    ("news", "ai_at", "ai_at DATETIME NULL AFTER ai_reason"),
    ("predictions", "low30", "low30 DOUBLE NULL AFTER median"),
    ("predictions", "high30", "high30 DOUBLE NULL AFTER low30"),
]


def _params(with_db: bool = True) -> dict[str, Any]:
    c = config.load()["mysql"]
    params: dict[str, Any] = dict(
        host=c.get("host"),
        port=c.getint("port"),
        user=c.get("user"),
        password=c.get("password"),
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        autocommit=True,
        connection_timeout=10,
    )
    if with_db:
        params["database"] = c.get("database")
    return params


def init_db() -> None:
    """Create the database (if needed) and all tables. Safe to run repeatedly."""
    global _schema_ready
    dbname = config.get("mysql", "database")
    if not dbname.replace("_", "").isalnum():
        raise ValueError(f"Invalid database name: {dbname!r}")
    cn = mysql.connector.connect(**_params(with_db=False))
    try:
        cur = cn.cursor()
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cur.execute(f"USE `{dbname}`")
        for stmt in SCHEMA:
            cur.execute(stmt)
        for table, column, ddl in MIGRATIONS:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (dbname, table, column),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE `{table}` ADD COLUMN {ddl}")
        cur.close()
    finally:
        cn.close()
    _schema_ready = True


def ensure_schema() -> None:
    if not _schema_ready:
        init_db()


def pool() -> pooling.MySQLConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            ensure_schema()
            _pool = pooling.MySQLConnectionPool(pool_name="bdstock", pool_size=16, **_params())
    return _pool


@contextmanager
def connection():
    cn = pool().get_connection()
    try:
        yield cn
    finally:
        cn.close()


def query(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    with connection() as cn:
        cur = cn.cursor(dictionary=True)
        cur.execute(sql, tuple(params or ()))
        rows = cur.fetchall()
        cur.close()
        return rows


def query_one(sql: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: Iterable[Any] | None = None) -> Any:
    with connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, tuple(params or ()))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None


def execute(sql: str, params: Iterable[Any] | None = None) -> int:
    """Run a statement; returns lastrowid for inserts, otherwise the affected row count."""
    with connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, tuple(params or ()))
        result = cur.lastrowid or cur.rowcount
        cur.close()
        return result


def executemany(sql: str, rows: list[tuple], chunk: int = 500) -> int:
    if not rows:
        return 0
    with connection() as cn:
        cur = cn.cursor()
        for i in range(0, len(rows), chunk):
            cur.executemany(sql, rows[i : i + chunk])
        cur.close()
    return len(rows)


@contextmanager
def named_lock(name: str, timeout: int = 0):
    """MySQL advisory lock so the scheduled task and the server never run a job twice."""
    with connection() as cn:
        cur = cn.cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (name, timeout))
        got = cur.fetchone()[0] == 1
        try:
            yield got
        finally:
            if got:
                cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
                cur.fetchone()
            cur.close()


# ---- key/value state shared between the server and the scheduled task -------------

def set_state(key: str, value: Any) -> None:
    execute(
        "INSERT INTO app_state (k, v, updated_at) VALUES (%s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
        (key, json.dumps(value, default=str)),
    )


def get_state(key: str, default: Any = None) -> Any:
    row = query_one("SELECT v FROM app_state WHERE k=%s", (key,))
    if not row or row["v"] is None:
        return default
    try:
        return json.loads(row["v"])
    except ValueError:
        return default


def job_start(job: str) -> int:
    return execute("INSERT INTO job_runs (job, started_at, status) VALUES (%s, NOW(), 'running')", (job,))


def job_finish(run_id: int, status: str, message: str = "") -> None:
    execute(
        "UPDATE job_runs SET finished_at=NOW(), status=%s, message=%s WHERE id=%s",
        (status, (message or "")[:4000], run_id),
    )
