"""Background work inside the web server: a job runner (with progress for the UI)
and the market monitor thread that refreshes prices while DSE is open, checks
intraday alerts, and starts the end-of-day run after the session closes."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from . import alerts, collector, config, news, settings

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, name: str, fn: Callable[..., Any], **kwargs: Any) -> bool:
        with self._lock:
            current = self._jobs.get(name)
            if current and current["running"]:
                return False
            state: dict[str, Any] = {
                "name": name, "running": True, "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None, "progress": None, "result": None, "error": None,
            }
            self._jobs[name] = state

        def progress(done: int, total: int, item: str = "") -> None:
            state["progress"] = {"done": done, "total": total, "item": item}

        def run() -> None:
            try:
                state["result"] = fn(progress=progress, **kwargs)
            except Exception as e:
                log.exception("Job %s failed", name)
                state["error"] = str(e)
            finally:
                state["running"] = False
                state["finished_at"] = datetime.now().isoformat(timespec="seconds")

        threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
        return True

    def status(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._jobs.items()}

    def running(self, name: str) -> bool:
        j = self._jobs.get(name)
        return bool(j and j["running"])


JOBS = JobRunner()


def _at(day, hhmm: str | None) -> datetime | None:
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":")[:2])
        return datetime(day.year, day.month, day.day, hh, mm)
    except (TypeError, ValueError):
        return None


class MarketMonitor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="market-monitor", daemon=True)
        self._stop_event = threading.Event()
        self.last_cycle: dict[str, Any] = {}
        self._news_at = datetime(2000, 1, 1)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("Market monitor started")
        while not self._stop_event.is_set():
            wait = 600.0
            try:
                wait = self.cycle()
            except Exception as e:
                log.warning("Monitor cycle failed: %s", e)
                self.last_cycle = {"at": datetime.now().isoformat(timespec="seconds"), "error": str(e)}
                wait = 300.0
            self._stop_event.wait(wait)

    def cycle(self) -> float:
        snap = collector.refresh_live()
        now = datetime.now()
        # the DSE news page shows only the newest ~50 disclosures: poll it through the day
        if (now - self._news_at).total_seconds() >= (600 if snap.is_open else 1800):
            try:
                news.fetch_latest()
                self._news_at = now
            except Exception as e:
                log.warning("News fetch failed: %s", e)
        info: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "trade_date": snap.trade_date.isoformat(), "open": snap.is_open}
        if snap.is_open:
            if settings.get("intraday_alerts") == "1":
                events = alerts.check_intraday(snap.trade_date)
                info["alerts"] = len(events)
            wait = max(1, config.getint("monitor", "intraday_interval_minutes")) * 60.0
        else:
            if (collector.eod_ready(snap) and not collector.eod_done(snap.trade_date)
                    and not JOBS.running("backfill") and not JOBS.running("predict")):
                if JOBS.start("daily", collector.run_daily):
                    info["started"] = "daily"
            wait = self._closed_wait(snap, now)
        info["next_in_s"] = int(wait)
        self.last_cycle = info
        return wait

    @staticmethod
    def _closed_wait(snap, now: datetime) -> float:
        s = snap.session or {}
        today = s.get("today") or {}
        if s.get("tradingDay") and str(today.get("date")) == now.date().isoformat():
            opening = _at(now.date(), today.get("opening") or s.get("opening"))
            if opening and now < opening:
                return min(max((opening - now).total_seconds() + 30, 60), 1800)
            if not collector.eod_ready(snap, now):
                return 300.0
        return 1800.0
