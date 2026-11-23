"""Local LLM (Ollama) helpers. Everything stays on this PC.

Used for two things only:
  1. News impact scoring - rate each non-routine DSE announcement from -2 (very negative) to +2
     (very positive). Stored in `news.ai_impact` and used as a model input ("AI news score"),
     which is tested like every other input group.
  2. Analyst note - a plain-language summary of a stock written from the numbers this tool has
     already computed (price, ranges, trade plan, technicals, fundamentals, recent news). The
     model is told not to invent numbers or give certainty about prices.

The LLM never produces the price ranges or signals itself.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime

import requests

from . import config, db

log = logging.getLogger(__name__)

NEWS_SYSTEM = """You rate Dhaka Stock Exchange company announcements for their likely effect on the share price over the next few weeks.
Rules: impact is an integer from -2 to 2.
0 = routine or procedural (meeting schedules, webcasts, disbursement done, information updates, elections, AGM notices).
+1/+2 = clearly good for earnings or value (profit growth, big new contract, expansion, upgrade, bonus/high dividend).
-1/-2 = clearly bad (losses, profit fall, no dividend, auditor qualification/emphasis of matter, production stop, legal/regulatory penalty, downgrade).
Reply in JSON with a one-sentence reason of at most 15 words."""

NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "integer", "enum": [-2, -1, 0, 1, 2]},
        "reason": {"type": "string", "maxLength": 160},
    },
    "required": ["impact", "reason"],
}

# routine notices that need no AI judgement
ROUTINE_SQL = ("(n.tags IS NULL OR n.tags NOT REGEXP 'board_meeting|record_date|agm|spot|price_limit') "
               "AND n.summary NOT LIKE '%%NAV%%' AND n.summary NOT LIKE '%%Resumption%%' AND n.summary NOT LIKE '%%Suspension%%'")


def _cfg() -> dict:
    c = config.load()
    sec = c["ollama"] if c.has_section("ollama") else {}
    return {
        "enabled": str(sec.get("enabled", "1")) == "1",
        "host": sec.get("host", "http://127.0.0.1:11434").rstrip("/"),
        "model": sec.get("model", "gemma3:4b"),
        "timeout": float(sec.get("timeout_seconds", "180")),
    }


def status() -> dict:
    cfg = _cfg()
    out = {"enabled": cfg["enabled"], "host": cfg["host"], "model": cfg["model"], "available": False, "models": []}
    if not cfg["enabled"]:
        return out
    try:
        r = requests.get(cfg["host"] + "/api/tags", timeout=4)
        names = [m["name"] for m in r.json().get("models", [])]
        out["models"] = names
        out["available"] = cfg["model"] in names
    except requests.RequestException as e:
        out["error"] = str(e)
    return out


def chat(messages: list[dict], schema: dict | None = None, max_tokens: int = 400, temperature: float = 0.0) -> str:
    cfg = _cfg()
    body = {"model": cfg["model"], "messages": messages, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens}}
    if schema:
        body["format"] = schema
    r = requests.post(cfg["host"] + "/api/chat", json=body, timeout=cfg["timeout"])
    r.raise_for_status()
    return r.json()["message"]["content"]


# ---------------------------------------------------------------------------- news scoring

def score_one(code: str, type_: str | None, summary: str, body: str) -> tuple[int, str]:
    content = chat(
        [{"role": "system", "content": NEWS_SYSTEM},
         {"role": "user", "content": f"{code} | {type_ or ''} | {summary}\n{(body or '')[:1200]}"}],
        schema=NEWS_SCHEMA, max_tokens=80,
    )
    data = json.loads(content)
    impact = max(-2, min(2, int(data.get("impact", 0))))
    return impact, str(data.get("reason", ""))[:300]


def pending_news(days: int = 120, limit: int = 2000) -> list[dict]:
    return db.query(
        "SELECT n.uid, n.code, n.type, n.summary, n.body FROM news n JOIN instruments i ON i.code=n.code "
        f"WHERE n.ai_impact IS NULL AND n.news_date >= CURDATE() - INTERVAL %s DAY AND i.asset_type IN ('EQ','MF') AND {ROUTINE_SQL} "
        "ORDER BY n.news_date DESC LIMIT %s",
        (days, limit),
    )


def score_news(days: int = 120, max_minutes: float = 600, progress=None) -> dict:
    """Score unscored, non-routine announcements (newest first) until done or out of time."""
    if not status()["available"]:
        return {"status": "skipped", "message": "Ollama or the model is not available"}
    items = pending_news(days)
    started, done, failed = time.time(), 0, 0
    for i, n in enumerate(items, 1):
        if (time.time() - started) / 60 > max_minutes:
            break
        try:
            impact, reason = score_one(n["code"], n["type"], n["summary"], n["body"])
            db.execute("UPDATE news SET ai_impact=%s, ai_reason=%s, ai_at=NOW() WHERE uid=%s", (impact, reason, n["uid"]))
            done += 1
        except Exception as e:
            failed += 1
            log.warning("AI news scoring failed for %s: %s", n["code"], e)
        if progress:
            progress(i, len(items), n["code"])
    # routine notices are neutral by definition - mark them so they are not re-checked
    db.execute(
        f"UPDATE news n SET n.ai_impact=0, n.ai_reason='Routine notice' WHERE n.ai_impact IS NULL AND NOT ({ROUTINE_SQL})"
    )
    left = len(items) - done - failed
    log.info("AI news scoring: %d scored, %d failed, %d left", done, failed, left)
    return {"status": "ok", "scored": done, "failed": failed, "left": max(0, left)}


# ---------------------------------------------------------------------------- analyst note

NOTE_SYSTEM = """You are a careful equity analyst writing for a private investor in Bangladesh (Dhaka Stock Exchange).
Write using ONLY the facts and numbers given. Never invent numbers, news or price targets.
Prices are uncertain: speak in probabilities and ranges, never promise gains.
Format (markdown, under 220 words):
**Summary** - 2 sentences.
**Positives** - up to 3 bullets.
**Risks** - up to 3 bullets.
**What to watch** - up to 3 bullets (levels, events).
**Plan** - one sentence restating the tool's trade plan and its historical win rate in plain words."""


def analyst_note(code: str, facts: dict) -> str:
    content = chat(
        [{"role": "system", "content": NOTE_SYSTEM},
         {"role": "user", "content": f"Stock {code}. Facts (JSON):\n{json.dumps(facts, default=str)[:6000]}"}],
        max_tokens=450, temperature=0.2,
    )
    return content.strip()


def cached_note(code: str, trade_date: date) -> dict | None:
    return db.query_one("SELECT note, model, created_at FROM ai_notes WHERE code=%s AND trade_date=%s", (code, trade_date))


def save_note(code: str, trade_date: date, note: str) -> None:
    db.execute(
        "INSERT INTO ai_notes (code, trade_date, model, note, created_at) VALUES (%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE model=VALUES(model), note=VALUES(note), created_at=VALUES(created_at)",
        (code, trade_date, _cfg()["model"], note, datetime.now()),
    )
