"""BD Stock Tracker command line.

    python run.py setup        # enter MySQL credentials, create the database + tables
    python run.py serve        # start the dashboard (http://127.0.0.1:8050) + live monitor
    python run.py daily        # end-of-day job (used by the Windows scheduled task)
    python run.py backfill     # download ~1 year of history for every stock
    python run.py fetch        # fetch the latest DSE snapshot once
    python run.py status       # show what is in the database
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from bdstock import config


def _progress(done: int, total: int, item: str = "") -> None:
    if sys.stdout is None:
        return
    width = 30
    filled = int(width * done / total) if total else width
    print(f"\r  [{'#' * filled}{'.' * (width - filled)}] {done}/{total} {item:<14}", end="", flush=True)
    if done >= total:
        print()


def cmd_setup(args: argparse.Namespace) -> None:
    from bdstock import db

    cp = config.load()
    print("MySQL connection (press Enter to keep the value in brackets)")
    for key, label in (("host", "Host"), ("port", "Port"), ("user", "User"), ("database", "Database name")):
        current = cp.get("mysql", key)
        value = input(f"  {label} [{current}]: ").strip()
        if value:
            cp.set("mysql", key, value)
    pw = getpass.getpass("  Password (hidden; Enter keeps the saved one): ")
    if pw:
        cp.set("mysql", "password", pw)
    config.save(cp)
    print(f"Saved {config.CONFIG_PATH}")
    db.init_db()
    print(f"Database '{cp.get('mysql', 'database')}' and tables are ready.")
    print("Next: python run.py daily   (first run downloads ~1 year of history, ~15-20 min)")
    print("Then: python run.py serve --open")


def cmd_init_db(args: argparse.Namespace) -> None:
    from bdstock import db

    db.init_db()
    print("Database and tables are ready.")


def cmd_serve(args: argparse.Namespace) -> None:
    from bdstock.web import serve

    serve(open_browser=args.open, monitor=not args.no_monitor)


def cmd_daily(args: argparse.Namespace) -> None:
    from bdstock import collector, db

    db.init_db()
    result = collector.run_daily(force=args.force, notify=not args.no_notify, progress=_progress)
    print(json.dumps(result, indent=2, default=str))


def cmd_backfill(args: argparse.Namespace) -> None:
    from bdstock import analysis, collector, db

    db.init_db()
    codes = [c.strip().upper() for c in args.codes.split(",")] if args.codes else None
    result = collector.backfill(codes=codes, delay=args.delay, progress=_progress)
    print(result["message"])
    last = db.scalar("SELECT MAX(trade_date) FROM daily_prices")
    if last:
        print(f"Analysed {analysis.analyze_all(last)} instruments for {last}")


def cmd_fetch(args: argparse.Namespace) -> None:
    from bdstock import collector, db

    db.init_db()
    snap = collector.refresh_live()
    dsex = next((i for i in snap.indices if i.get("key") == "DSEX"), {})
    print(f"Trade date {snap.trade_date} ({snap.trade_time}); market {'OPEN' if snap.is_open else 'closed'}")
    print(f"DSEX {dsex.get('value', 0):,.2f} ({dsex.get('percent', 0):+.2f}%); {len(snap.quotes)} instruments")


def cmd_status(args: argparse.Namespace) -> None:
    from bdstock import db

    db.init_db()
    h = db.query_one(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT code) AS codes, COUNT(DISTINCT trade_date) AS days, "
        "MIN(trade_date) AS first, MAX(trade_date) AS last FROM daily_prices"
    )
    print(f"Instruments:  {db.scalar('SELECT COUNT(*) FROM instruments')}")
    print(f"Price rows:   {h['n']:,} ({h['codes']} codes, {h['days']} days, {h['first']} .. {h['last']})")
    print(f"Signals for:  {db.scalar('SELECT MAX(trade_date) FROM signals')}")
    print(f"Last report:  {db.scalar('SELECT MAX(report_date) FROM daily_reports')}")
    print(f"Watchlist:    {db.scalar('SELECT COUNT(*) FROM watchlist')}   Alerts: {db.scalar('SELECT COUNT(*) FROM alerts WHERE active=1')}")
    print("Recent jobs:")
    for r in db.query("SELECT job, started_at, status, message FROM job_runs ORDER BY id DESC LIMIT 6"):
        print(f"  {r['started_at']}  {r['job']:<9} {r['status']:<8} {(r['message'] or '')[:90]}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="run.py", description="BD Stock Tracker - DSE analysis & daily monitor")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="configure MySQL and create tables").set_defaults(fn=cmd_setup)
    sub.add_parser("init-db", help="create database/tables using config.ini").set_defaults(fn=cmd_init_db)
    s = sub.add_parser("serve", help="run the dashboard and live monitor")
    s.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    s.add_argument("--no-monitor", action="store_true", help="do not poll DSE in the background")
    s.set_defaults(fn=cmd_serve)
    d = sub.add_parser("daily", help="end-of-day: store prices, analyse, alerts, report, notify")
    d.add_argument("--force", action="store_true", help="run even if the session has not finished")
    d.add_argument("--no-notify", action="store_true", help="do not send the summary notification")
    d.set_defaults(fn=cmd_daily)
    b = sub.add_parser("backfill", help="download ~1 year of daily history from DSE company pages")
    b.add_argument("--codes", help="comma-separated trading codes (default: all)")
    b.add_argument("--delay", type=float, default=None, help="seconds between requests")
    b.set_defaults(fn=cmd_backfill)
    sub.add_parser("fetch", help="fetch the latest snapshot once").set_defaults(fn=cmd_fetch)
    sub.add_parser("status", help="show database contents").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    config.setup_logging(console=True)
    args.fn(args)


if __name__ == "__main__":
    main()
