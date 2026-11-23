"""Portfolio tracking with the average-cost method (as used by BD brokers' statements)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import db, settings


class PortfolioError(ValueError):
    pass


def latest_prices(codes: list[str]) -> dict[str, dict]:
    """Latest traded price per code: live quote if traded, otherwise last close."""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    out: dict[str, dict] = {}
    for q in db.query(
        f"SELECT code, trade_date, ltp, close, ycp, change_pct, volume FROM live_quotes WHERE code IN ({ph})", codes
    ):
        price = q["ltp"] if q["ltp"] else q["close"]
        if price:
            out[q["code"]] = {"price": price, "ycp": q["ycp"] or price, "change_pct": q["change_pct"], "as_of": q["trade_date"]}
    missing = [c for c in codes if c not in out]
    if missing:
        ph = ",".join(["%s"] * len(missing))
        for r in db.query(
            f"SELECT p.code, p.trade_date, p.close, p.ycp, p.change_pct FROM daily_prices p "
            f"JOIN (SELECT code, MAX(trade_date) d FROM daily_prices WHERE code IN ({ph}) GROUP BY code) m "
            f"ON m.code=p.code AND m.d=p.trade_date",
            missing,
        ):
            out[r["code"]] = {"price": r["close"], "ycp": r["ycp"] or r["close"], "change_pct": r["change_pct"], "as_of": r["trade_date"]}
    return out


def _positions() -> dict[str, dict]:
    return compute_positions(db.query("SELECT * FROM transactions ORDER BY trade_date, id"))


def compute_positions(transactions: list[dict]) -> dict[str, dict]:
    """Average-cost positions from transactions sorted by date. Buy fees are added to
    cost; sell fees reduce realised P/L."""
    pos: dict[str, dict] = {}
    for t in transactions:
        p = pos.setdefault(
            t["code"], {"code": t["code"], "qty": 0, "cost": 0.0, "realized": 0.0, "fees": 0.0, "first_buy": None}
        )
        qty, price, fee = int(t["qty"]), float(t["price"]), float(t["fee"] or 0)
        p["fees"] += fee
        if t["side"] == "BUY":
            p["cost"] += qty * price + fee
            p["qty"] += qty
            p["first_buy"] = p["first_buy"] or t["trade_date"]
        else:
            avg = p["cost"] / p["qty"] if p["qty"] else 0.0
            sold = min(qty, p["qty"])
            p["realized"] += sold * price - fee - avg * sold
            p["cost"] -= avg * sold
            p["qty"] -= sold
            if p["qty"] == 0:
                p["cost"] = 0.0
                p["first_buy"] = None
    return pos


def summary() -> dict:
    pos = _positions()
    open_codes = [c for c, p in pos.items() if p["qty"] > 0]
    prices = latest_prices(open_codes)
    meta = {}
    if open_codes:
        ph = ",".join(["%s"] * len(open_codes))
        meta = {r["code"]: r for r in db.query(f"SELECT code, name, sector, category FROM instruments WHERE code IN ({ph})", open_codes)}
    holdings = []
    for code in open_codes:
        p = pos[code]
        q = prices.get(code, {})
        price = q.get("price") or (p["cost"] / p["qty"])
        ycp = q.get("ycp") or price
        mv = p["qty"] * price
        holdings.append(
            {
                "code": code,
                "name": (meta.get(code) or {}).get("name"),
                "sector": (meta.get(code) or {}).get("sector"),
                "category": (meta.get(code) or {}).get("category"),
                "qty": p["qty"],
                "avg_cost": round(p["cost"] / p["qty"], 4),
                "cost": round(p["cost"], 2),
                "price": price,
                "change_pct": q.get("change_pct"),
                "market_value": round(mv, 2),
                "unrealized": round(mv - p["cost"], 2),
                "unrealized_pct": round((mv / p["cost"] - 1) * 100, 2) if p["cost"] else None,
                "day_change": round(p["qty"] * (price - ycp), 2),
                "realized": round(p["realized"], 2),
                "as_of": q.get("as_of"),
                "first_buy": p["first_buy"],
            }
        )
    total_mv = sum(h["market_value"] for h in holdings)
    total_cost = sum(h["cost"] for h in holdings)
    day_change = sum(h["day_change"] for h in holdings)
    for h in holdings:
        h["weight"] = round(h["market_value"] / total_mv * 100, 2) if total_mv else 0
    holdings.sort(key=lambda h: -h["market_value"])
    sectors: dict[str, float] = defaultdict(float)
    for h in holdings:
        sectors[h["sector"] or "Other"] += h["market_value"]
    realized_total = sum(p["realized"] for p in pos.values())
    prev_value = total_mv - day_change
    return {
        "holdings": holdings,
        "totals": {
            "market_value": round(total_mv, 2),
            "cost": round(total_cost, 2),
            "unrealized": round(total_mv - total_cost, 2),
            "unrealized_pct": round((total_mv / total_cost - 1) * 100, 2) if total_cost else None,
            "realized": round(realized_total, 2),
            "day_change": round(day_change, 2),
            "day_change_pct": round(day_change / prev_value * 100, 2) if prev_value else None,
            "fees": round(sum(p["fees"] for p in pos.values()), 2),
            "positions": len(holdings),
        },
        "sectors": sorted(
            ({"sector": s, "value": round(v, 2), "pct": round(v / total_mv * 100, 2) if total_mv else 0} for s, v in sectors.items()),
            key=lambda x: -x["value"],
        ),
        "closed": [
            {"code": c, "realized": round(p["realized"], 2)} for c, p in pos.items() if p["qty"] == 0 and p["realized"]
        ],
    }


def add_transaction(data: dict) -> int:
    code = str(data.get("code") or "").strip().upper()
    side = str(data.get("side") or "").strip().upper()
    try:
        qty = int(data.get("qty"))
        price = float(data.get("price"))
    except (TypeError, ValueError):
        raise PortfolioError("Quantity and price must be numbers")
    try:
        trade_date = date.fromisoformat(str(data.get("trade_date") or date.today().isoformat()))
    except ValueError:
        raise PortfolioError("Invalid date")
    if side not in ("BUY", "SELL"):
        raise PortfolioError("Side must be BUY or SELL")
    if qty <= 0 or price <= 0:
        raise PortfolioError("Quantity and price must be greater than zero")
    if not db.query_one("SELECT code FROM instruments WHERE code=%s", (code,)):
        raise PortfolioError(f"Unknown trading code: {code}")
    if side == "SELL":
        held = _positions().get(code, {}).get("qty", 0)
        if qty > held:
            raise PortfolioError(f"You hold only {held} shares of {code}")
    fee_raw = data.get("fee")
    if fee_raw in (None, ""):
        fee = round(qty * price * settings.get_float("commission_rate") / 100, 2)
    else:
        try:
            fee = float(fee_raw)
        except (TypeError, ValueError):
            raise PortfolioError("Fee must be a number")
    return db.execute(
        "INSERT INTO transactions (code, side, qty, price, fee, trade_date, note, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
        (code, side, qty, price, fee, trade_date, (data.get("note") or "")[:255]),
    )


def list_transactions(code: str | None = None) -> list[dict]:
    if code:
        return db.query("SELECT * FROM transactions WHERE code=%s ORDER BY trade_date DESC, id DESC", (code,))
    return db.query("SELECT * FROM transactions ORDER BY trade_date DESC, id DESC")


def delete_transaction(tx_id: int) -> None:
    db.execute("DELETE FROM transactions WHERE id=%s", (tx_id,))
