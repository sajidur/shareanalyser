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
    model = sec.get("model", "gemma3:4b")
    return {
        "enabled": str(sec.get("enabled", "1")) == "1",
        "host": sec.get("host", "http://127.0.0.1:11434").rstrip("/"),
        "model": model,
        # larger model for analysis text (notes, health checks, briefs); defaults to the same model
        "analysis_model": sec.get("analysis_model", "") or model,
        "timeout": float(sec.get("timeout_seconds", "180")),
    }


def status() -> dict:
    cfg = _cfg()
    out = {"enabled": cfg["enabled"], "host": cfg["host"], "model": cfg["model"], "analysis_model": cfg["analysis_model"],
           "available": False, "models": []}
    if not cfg["enabled"]:
        return out
    try:
        r = requests.get(cfg["host"] + "/api/tags", timeout=4)
        names = [m["name"] for m in r.json().get("models", [])]
        out["models"] = names
        out["available"] = cfg["model"] in names and cfg["analysis_model"] in names
    except requests.RequestException as e:
        out["error"] = str(e)
    return out


def chat(messages: list[dict], schema: dict | None = None, max_tokens: int = 400, temperature: float = 0.0,
         analysis: bool = False) -> str:
    cfg = _cfg()
    body = {"model": cfg["analysis_model"] if analysis else cfg["model"], "messages": messages, "stream": False,
            "think": False, "options": {"temperature": temperature, "num_predict": max_tokens}}
    if schema:
        body["format"] = schema
    # long analysis texts take 2-4 minutes on a CPU-only PC; give them more room than quick news ratings
    timeout = max(cfg["timeout"], 600.0) if analysis else cfg["timeout"]
    r = requests.post(cfg["host"] + "/api/chat", json=body, timeout=(5, timeout))
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
        max_tokens=450, temperature=0.2, analysis=True,
    )
    return content.strip()


# ---------------------------------------------------------------------------- company health check

PROFILE_SYSTEM = """You are a careful credit and equity analyst for the Dhaka Stock Exchange (DSE, Bangladesh).
From the company's own announcements and financial history given below, score each area from -2 (serious problems)
to +2 (clearly strong). 0 = normal / not enough information. Use ONLY the facts given; do not guess.
governance: auditor qualifications, emphasis of matter, going concern, regulator actions, board reconstitution,
  late AGM/financials, category Z (missed dividends/AGM) are negative.
financial_health: profit trend, losses, operating cash flow (NOCFPS), NAV trend.
growth: EPS and revenue trend, expansion, new capacity.
dividend_reliability: how consistently cash dividends were paid in recent years.
news_flow: overall tone of the last few months of announcements.
political_exposure: state / political risk visible in the filings. Negative: the central bank or regulator dissolved
  the board or appointed an administrator or observer, court orders hold up the AGM, dividend or control, shares or
  assets frozen, the bank was declared non-viable or its capital written off, or the company says political
  unrest hurt its business. Central bank / regulator control is NOT state ownership: it is negative (-2).
  0 = none of this appears. +1 only if the government owns shares (stated in the facts), there is no takeover
  and dividends are regular. Judge ONLY from the facts given.
  Never guess any person's political affiliation from names or from outside knowledge.
Give up to 3 short positives and 3 short risks (each under 15 words) and a 2-sentence summary.
Trust "plain_facts" for trends (e.g. whether EPS rose or fell). Never call a decline growth."""

PROFILE_AREAS = ("governance", "financial_health", "growth", "dividend_reliability", "news_flow", "political_exposure")
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "integer", "enum": [-2, -1, 0, 1, 2]} for k in PROFILE_AREAS},
        "positives": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "summary": {"type": "string"},
    },
    "required": [*PROFILE_AREAS, "positives", "risks", "summary"],
}


def profile_facts(code: str) -> dict:
    inst = db.query_one("SELECT code, name, sector, category FROM instruments WHERE code=%s", (code,)) or {}
    info = db.query_one("SELECT pe, eps, nav, dividend_yield, free_float, listing_year, year_end, extra_json "
                        "FROM company_info WHERE code=%s", (code,)) or {}
    extra = json.loads(info.pop("extra_json", None) or "{}")
    news_rows = db.query(
        "SELECT news_date, summary, LEFT(body, 260) AS body, tags, ai_impact FROM news WHERE code=%s "
        "AND summary NOT LIKE %s ORDER BY news_date DESC LIMIT 20", (code, "%NAV%"))
    eps = [(f.get("year"), f.get("epsBasic")) for f in (extra.get("multiYearFinancials") or []) if f.get("epsBasic") is not None][-5:]
    trend = []
    for (y0, e0), (y1, e1) in zip(eps, eps[1:]):
        word = "rose" if e1 > e0 else "fell" if e1 < e0 else "was unchanged"
        trend.append(f"EPS {word} from {e0} ({y0}) to {e1} ({y1})")
    divs = [(d.get("year"), d.get("cash")) for d in (extra.get("dividendHistory") or [])][:5]
    paid = [f"{y}: {c or 0}% cash" for y, c in divs]
    # political / state footprint found in the company's own filings (facts, not guesses)
    from . import bdfactors

    pol, pol_events = [], []
    govt = bdfactors.govt_holdings().get(code)
    if govt:
        pol.append(f"The government holds {govt:g}% of the shares.")
    for r in db.query("SELECT news_date, summary, LEFT(body, 3000) AS body FROM news WHERE code=%s "
                      "AND news_date >= CURDATE() - INTERVAL 2 YEAR ORDER BY news_date DESC LIMIT 200", (code,)):
        kinds = [k for k in bdfactors.classify_news(r["summary"] or "", r["body"] or "") if k in bdfactors.POLITICAL_KINDS]
        for k in kinds:
            pol_events.append((r["news_date"], k))
            pol.append(f"{r['news_date']:%d %b %Y}: {bdfactors.EVENT_TYPES[k][0]} ({r['summary']}).")
    pol = pol[:8] or ["No central bank / regulator takeover, court order or political disruption appears in the filings."]
    return {
        "_pol_events": pol_events,
        "plain_facts": trend + ([f"Cash dividends by year: {', '.join(paid)}"] if paid else []) + pol,
        "company": inst, "fundamentals": info,
        "eps_nav_history": [{k: f.get(k) for k in ("year", "epsBasic", "nav", "profit")} for f in (extra.get("multiYearFinancials") or [])][-6:],
        "dividends": (extra.get("dividendHistory") or [])[:6],
        "shareholding": (extra.get("sharePattern") or [])[-1:],
        "announcements": news_rows,
    }


def political_cap(events: list[tuple[date, str]], today: date | None = None) -> int:
    """Highest political / state score the filings allow. A small model can contradict the facts
    (it once scored a bank whose capital was written off +1), so the filings set a ceiling."""
    today = today or date.today()
    cap = 2
    for d, k in events:
        if k == "wipeout":
            cap = min(cap, -2)
        elif k == "state_control":
            cap = min(cap, -2 if (today - d).days <= 365 else -1)
        elif k in ("court_hold", "political_hit"):
            cap = min(cap, 0)
    return cap


def company_profile(code: str) -> dict:
    facts = profile_facts(code)
    cap = political_cap(facts.pop("_pol_events", []))
    content = chat(
        [{"role": "system", "content": PROFILE_SYSTEM},
         {"role": "user", "content": f"Company {code}. Facts (JSON):\n{json.dumps(facts, default=str)[:7000]}"}],
        schema=PROFILE_SCHEMA, max_tokens=420, analysis=True,
    )
    data = json.loads(content)
    for k in PROFILE_AREAS:
        data[k] = max(-2, min(2, int(data.get(k, 0))))
    if data["political_exposure"] > cap:
        data["political_capped"] = {"model_said": data["political_exposure"], "cap": cap}
        data["political_exposure"] = cap
    data["positives"] = [str(x)[:160] for x in (data.get("positives") or [])][:3]
    data["risks"] = [str(x)[:160] for x in (data.get("risks") or [])][:3]
    data["summary"] = str(data.get("summary", ""))[:600]
    data["overall"] = round(sum(data[k] for k in PROFILE_AREAS) / len(PROFILE_AREAS), 2)
    db.execute(
        "INSERT INTO ai_profiles (code, as_of, model, data, created_at) VALUES (%s, CURDATE(), %s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE as_of=VALUES(as_of), model=VALUES(model), data=VALUES(data), created_at=VALUES(created_at)",
        (code, _cfg()["analysis_model"], json.dumps(data)))
    return data


def get_profiles(codes: list[str] | None = None) -> dict[str, dict]:
    sql, params = "SELECT code, as_of, model, data, created_at FROM ai_profiles", []
    if codes:
        sql += f" WHERE code IN ({','.join(['%s'] * len(codes))})"
        params = codes
    out = {}
    for r in db.query(sql, params):
        try:
            out[r["code"]] = {**json.loads(r["data"]), "as_of": r["as_of"], "model": r["model"], "created_at": r["created_at"]}
        except ValueError:
            continue
    return out


def profile_queue() -> list[str]:
    """Who needs a (new) health check, most important first: holdings, watchlist, today's BUY /
    ACCUMULATE candidates, then liquid shares. Re-check when there is newer news or after 14 days."""
    from . import portfolio

    have = {r["code"]: r for r in db.query("SELECT code, as_of FROM ai_profiles")}
    last_news = {r["code"]: r["d"] for r in db.query("SELECT code, MAX(news_date) AS d FROM news GROUP BY code")}
    holdings = [h["code"] for h in portfolio.summary()["holdings"]]
    watch = [w["code"] for w in db.query("SELECT code FROM watchlist")]
    d = db.scalar("SELECT MAX(trade_date) FROM predictions")
    liquid = [r["code"] for r in db.query(
        "SELECT p.code FROM predictions p JOIN instruments i ON i.code=p.code WHERE p.trade_date=%s AND p.horizon=20 "
        "AND i.asset_type='EQ' ORDER BY p.liq_mn DESC", (d,))] if d else []
    sig = db.query("SELECT DISTINCT code FROM signal_log WHERE signal_date=%s", (d,)) if d else []
    order = holdings + watch + [r["code"] for r in sig] + liquid
    out, seen = [], set()
    for code in order:
        if code in seen:
            continue
        seen.add(code)
        p = have.get(code)
        stale = (not p or not p["as_of"] or (last_news.get(code) and last_news[code] > p["as_of"])
                 or (date.today() - p["as_of"]).days > 14)
        if stale:
            out.append(code)
    return out


def profile_batch(max_minutes: float = 10, codes: list[str] | None = None, progress=None) -> dict:
    if not status()["available"]:
        return {"status": "skipped", "message": "Ollama or the model is not available"}
    queue = codes or profile_queue()
    started, done, failed = time.time(), 0, 0
    for i, code in enumerate(queue, 1):
        if (time.time() - started) / 60 > max_minutes:
            break
        try:
            company_profile(code)
            done += 1
        except Exception as e:
            failed += 1
            log.warning("AI health check failed for %s: %s", code, e)
        if progress:
            progress(i, len(queue), code)
    return {"status": "ok", "profiled": done, "failed": failed, "left": max(0, len(queue) - done - failed)}


# ---------------------------------------------------------------------------- daily market brief / portfolio review

BRIEF_SYSTEM = """You write a short end-of-day market brief for a private investor on the Dhaka Stock Exchange.
Use ONLY the numbers and facts given. Do not predict prices or give certainty; mention risks plainly.
Markdown, under 200 words: **Market**, **Sectors**, **Movers**, **Bangladesh factors** (rumours, category changes,
regulator/government news, market regime), **Signals** (what the tool's rules flagged today and their track record)."""

REVIEW_SYSTEM = """You review a private investor's Dhaka Stock Exchange portfolio using ONLY the facts given
(holdings, the tool's signals and reasons, profit/loss, sector weights, AI health checks, market regime).
Be balanced and practical; never promise returns; do not invent numbers. Remind that past losses are sunk costs.
Markdown, under 300 words: **Overall**, **Biggest risks**, **Holding by holding** (one bullet each: code, the
tool signal in capitals, and its main reason in plain words), **Diversification**, **Questions to consider**
(2-3 bullets)."""


def brief_facts(rep: dict) -> list[str]:
    """Plain fact sentences for the market brief (small models misread raw JSON)."""
    from . import bdfactors

    f = []
    for k, v in (rep.get("indices") or {}).items():
        if v.get("value"):
            f.append(f"{k} closed at {v['value']:,.2f}, change {v.get('change') or 0:+.2f} points ({v.get('pct') or 0:+.2f}%).")
    m = rep.get("market") or {}
    if m.get("turnover_mn"):
        chg = m.get("turnover_change_pct")
        f.append(f"Turnover was Tk {m['turnover_mn'] / 10:,.1f} crore" + (f", {chg:+.1f}% versus the previous session." if chg is not None else "."))
    if m.get("advanced") is not None:
        f.append(f"{m['advanced']} shares rose, {m['declined']} fell and {m['unchanged']} were unchanged.")
    sec = rep.get("sectors") or []
    if sec:
        f.append("Best sectors by average change: " + ", ".join(f"{s['sector']} {s['avg_change']:+.2f}%" for s in sec[:3]) + ".")
        f.append("Worst sectors: " + ", ".join(f"{s['sector']} {s['avg_change']:+.2f}%" for s in sec[-3:]) + ".")
    if rep.get("top_gainers"):
        f.append("Top gainers: " + ", ".join(f"{x['code']} {x['change_pct']:+.1f}%" for x in rep["top_gainers"][:5]) + ".")
    if rep.get("top_losers"):
        f.append("Top losers: " + ", ".join(f"{x['code']} {x['change_pct']:+.1f}%" for x in rep["top_losers"][:5]) + ".")
    ts = rep.get("trade_signals") or {}
    regime = ts.get("regime") or ((db.get_state("bd_factors") or {}).get("regime_now") or {}).get("regime")
    if regime:
        f.append(f"The tool classifies the market regime as {regime}.")
    buys = [x["code"] for x in ts.get("buy", [])]
    if ts.get("counts") is not None:
        f.append(f"The tool's BUY rule flagged {ts['counts'].get('BUY', 0)} shares today"
                 + (f", led by {', '.join(buys[:5])}." if buys else "."))
    rec = ts.get("record") or {}
    if rec.get("trades"):
        f.append(f"In the past year that BUY rule averaged {rec['avg_trade']:+.2f}% per trade versus {rec['baseline_trade']:+.2f}% "
                 f"for random entries, and hit its stop-loss {rec['stop_rate']:.0f}% of the time.")
    fc = next((c for c in rep.get("forecast_check") or [] if c["horizon"] == 1), None)
    if fc:
        f.append(f"Of earlier next-session forecasts, {fc['in68']:.0f}% of shares closed inside their 68% range (target 68%).")
    counts: dict[str, int] = {}
    for n in db.query("SELECT summary, LEFT(body, 600) AS body FROM news WHERE news_date=%s", (rep.get("date"),)):
        for k in bdfactors.classify_news(n["summary"] or "", n["body"] or ""):
            counts[k] = counts.get(k, 0) + 1
    if counts:
        f.append("Bangladesh-specific announcements today: " + ", ".join(
            f"{v} {bdfactors.EVENT_TYPES[k][0].lower()}" for k, v in counts.items()) + ".")
    elif rep.get("news_count"):
        f.append(f"{rep['news_count']} company announcements today, none of them rumour, insider, category or regulator events.")
    return f


def market_brief(rep: dict) -> str:
    facts = brief_facts(rep)
    return chat([{"role": "system", "content": BRIEF_SYSTEM + "\nNever add numbers, causes or trends that are not in the facts."},
                 {"role": "user", "content": "Facts:\n- " + "\n- ".join(facts)}],
                max_tokens=380, temperature=0.1, analysis=True).strip()


def review_facts(pf: dict, plans: dict, profiles: dict) -> list[str]:
    """Plain fact sentences for the portfolio review, numbers pre-formatted (small models garble raw JSON)."""
    t = pf["totals"]
    f = [f"The portfolio has {len(pf['holdings'])} holdings. Market value Tk {t['market_value']:,.0f}, cost Tk {t['cost']:,.0f}, "
         f"unrealised profit/loss Tk {t['unrealized']:+,.0f} ({t['unrealized_pct']:+.1f}%)."]
    if pf.get("sectors"):
        f.append("Share of the portfolio by sector: " + "; ".join(f"{s['sector']} {s['pct']:.1f}%" for s in pf["sectors"]) + ".")
    for h in pf["holdings"]:
        p = plans.get(h["code"]) or {}
        reasons = [r for r in (p.get("reasons") or []) if not r.startswith("You are ")][:2]
        line = (f"{h['code']} ({h['sector']}, DSE category {h['category']}): {h['weight']:.1f}% of the portfolio, "
                f"{h['unrealized_pct']:+.1f}% versus cost. Tool signal: {p.get('signal') or 'none'}"
                + (f" because: {'; '.join(reasons)}." if reasons else "."))
        prof = profiles.get(h["code"])
        if prof:
            line += (" AI health check scores (-2 to +2): " + ", ".join(f"{k.replace('_', ' ')} {prof.get(k, 0):+d}" for k in PROFILE_AREAS if k in prof)
                     + f". Health check summary: {prof.get('summary', '')}")
        f.append(line)
    n = sum(1 for h in pf["holdings"] if h["code"] in profiles)
    f.append(f"AI health checks exist for {n} of the {len(pf['holdings'])} holdings"
             + (f" ({', '.join(h['code'] for h in pf['holdings'] if h['code'] in profiles)})." if n else "."))
    if any(h["category"] == "Z" for h in pf["holdings"]):
        f.append("DSE category Z is the weakest category: companies that paid no dividend, missed their AGM or have large losses.")
    regime = ((db.get_state("bd_factors") or {}).get("regime_now") or {}).get("regime")
    if regime:
        f.append(f"Current DSE market regime: {regime}.")
    return f


def portfolio_review() -> dict:
    from . import portfolio, tradeplan

    pf = portfolio.summary()
    if not pf["holdings"]:
        raise ValueError("No holdings to review")
    plans = {p["code"]: p for p in tradeplan.all_plans()["holdings"]}
    profiles = get_profiles([h["code"] for h in pf["holdings"]])
    facts = review_facts(pf, plans, profiles)
    text = chat([{"role": "system", "content": REVIEW_SYSTEM + "\nUse numbers exactly as written in the facts. "
                  "Never add numbers, causes or claims that are not in the facts."},
                 {"role": "user", "content": "Facts:\n- " + "\n- ".join(facts)}],
                max_tokens=520, temperature=0.1, analysis=True).strip()
    save_note("__PORTFOLIO__", date.today(), text)
    return cached_note("__PORTFOLIO__", date.today())


def cached_note(code: str, trade_date: date) -> dict | None:
    return db.query_one("SELECT note, model, created_at FROM ai_notes WHERE code=%s AND trade_date=%s", (code, trade_date))


def save_note(code: str, trade_date: date, note: str) -> None:
    db.execute(
        "INSERT INTO ai_notes (code, trade_date, model, note, created_at) VALUES (%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE model=VALUES(model), note=VALUES(note), created_at=VALUES(created_at)",
        (code, trade_date, _cfg()["analysis_model"], note, datetime.now()),
    )
