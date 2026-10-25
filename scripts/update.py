"""Rebuild data.json from positions.json and Yahoo Finance prices.

Runs in GitHub Actions every weekday after the market close (see
.github/workflows/update-data.yml). Anything it can't fetch is carried over
from the previous data.json, so one bad source never blanks the app.
"""
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSITIONS = ROOT / "positions.json"
DATA = ROOT / "data.json"
HISTORY_DAYS = 70
MAX_ARTICLES = 15


# ---------------------------------------------------------------- provider
class YahooProvider:
    """Thin wrapper so tests can swap in a fake provider."""

    def __init__(self):
        import yfinance as yf  # imported lazily so tests don't need it
        self.yf = yf
        self._t = {}

    def _ticker(self, sym):
        if sym not in self._t:
            self._t[sym] = self.yf.Ticker(sym)
        return self._t[sym]

    def history(self, sym, period="6mo"):
        df = self._ticker(sym).history(period=period, interval="1d", auto_adjust=False)
        out = []
        for idx, row in df.iterrows():
            c = row.get("Close")
            if c is None or (isinstance(c, float) and math.isnan(c)):
                continue
            out.append([idx.strftime("%Y-%m-%d"), round(float(c), 4)])
        return out

    def info(self, sym):
        try:
            return self._ticker(sym).info or {}
        except Exception:
            return {}

    def news(self, sym):
        try:
            return self._ticker(sym).news or []
        except Exception:
            return []

    def earnings_dates(self, sym):
        try:
            cal = self._ticker(sym).calendar
        except Exception:
            return []
        dates = []
        if isinstance(cal, dict):
            v = cal.get("Earnings Date") or []
            dates = v if isinstance(v, list) else [v]
        out = []
        for d in dates:
            try:
                out.append(d.strftime("%Y-%m-%d"))
            except Exception:
                pass
        return out


# ---------------------------------------------------------------- helpers
def occ(sym, expiry, opt_type, strike):
    """OCC option symbol, e.g. AMZN270416C00255000."""
    y, m, d = expiry.split("-")
    return f"{sym}{y[2:]}{m}{d}{'C' if opt_type == 'call' else 'P'}{int(round(strike * 1000)):08d}"


def safe(fn, default=None, label=""):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - we log and carry on
        print(f"warn: {label}: {e}", file=sys.stderr)
        return default


def days_between(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def close_on_or_before(hist, date):
    best = None
    for d, c in hist:
        if d <= date:
            best = c
        else:
            break
    return best


def fmt_money(v, cur="USD", nd=2):
    p = "US$" if cur == "USD" else "C$"
    return ("−" if v < 0 else "") + p + f"{abs(v):,.{nd}f}"


def pct(v, nd=1):
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):.{nd}f}%"


def norm_news(item, sym):
    """Handle both the old flat and the newer nested yfinance news shapes."""
    c = item.get("content") if isinstance(item.get("content"), dict) else item
    title = c.get("title")
    url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or c.get("link")
    src = (c.get("provider") or {}).get("displayName") or c.get("publisher") or "Yahoo Finance"
    date = None
    if c.get("pubDate"):
        date = str(c["pubDate"])[:10]
    elif c.get("providerPublishTime"):
        date = dt.datetime.utcfromtimestamp(int(c["providerPublishTime"])).strftime("%Y-%m-%d")
    summary = (c.get("summary") or c.get("description") or "").strip()
    if len(summary) > 220:
        summary = summary[:217].rsplit(" ", 1)[0] + "…"
    if not title or not url or not str(url).startswith("https://") or not date:
        return None
    return {"title": title, "src": src, "date": date, "url": url, "syms": [sym], "summary": summary}


# ---------------------------------------------------------------- build
def build(pos, prev, prov, today=None):
    today = today or dt.date.today().isoformat()
    prev_assets = {a["id"]: a for a in prev.get("assets", [])}
    notes = []

    # FX
    fx_hist = safe(lambda: prov.history("CAD=X", "6mo"), [], "fx")
    if len(fx_hist) >= 2:
        usdcad, usdcad_prev = fx_hist[-1][1], fx_hist[-2][1]
    else:
        usdcad = prev.get("fx", {}).get("usdcad", 1.4)
        usdcad_prev = prev.get("fx", {}).get("prev", usdcad)
        notes.append("FX carried over")
    fx_of = lambda c: usdcad if c == "USD" else 1.0  # noqa: E731

    assets, as_of = [], None
    hist_cache = {}
    for a in pos["assets"]:
        p = prev_assets.get(a["id"], {})
        out = {k: v for k, v in a.items() if k not in ("yahoo", "shortCalls", "markAsOf")}
        ysym = a["yahoo"]
        hist = hist_cache.get(ysym)
        if hist is None:
            hist = safe(lambda: prov.history(ysym, "6mo"), [], ysym) or []
            hist_cache[ysym] = hist
        if len(hist) >= 2:
            out["price"], out["prev"] = hist[-1][1], hist[-2][1]
            out["history"] = hist[-HISTORY_DAYS:]
            as_of = max(as_of or hist[-1][0], hist[-1][0])
        else:
            out["price"], out["prev"] = p.get("price"), p.get("prev")
            out["history"] = p.get("history", [])
            notes.append(f"{a['sym']} price carried over")
        qty = sum(x["qty"] for x in a["positions"])

        if a["kind"] == "option":
            code = occ(a["sym"], a["expiry"], a["optType"], a["strike"])
            oh = safe(lambda: prov.history(code, "1mo"), [], code) or []
            mult = 100 * a.get("contracts", 1)
            if len(oh) >= 2:
                out["mark"], out["markPrev"] = round(oh[-1][1] * mult, 2), round(oh[-2][1] * mult, 2)
            elif len(oh) == 1:
                out["mark"] = round(oh[-1][1] * mult, 2)
                out["markPrev"] = p.get("mark", out["mark"])
            else:
                out["mark"] = p.get("mark", a.get("mark") or 0)
                out["markPrev"] = out["mark"]
                notes.append(f"{a['sym']} option mark carried over")
            out["value"] = out["mark"]
            out["dayChange"] = round(out["mark"] - out["markPrev"], 2)
        else:
            price, prv = out["price"] or 0, out["prev"] or 0
            value, day = qty * price, qty * (price - prv)
            sc_total = 0.0
            for sc in a.get("shortCalls", []):
                code = occ(a["sym"], sc["expiry"], "call", sc["strike"])
                oh = safe(lambda: prov.history(code, "1mo"), [], code) or []
                if len(oh) >= 2:
                    m, mp = oh[-1][1] * 100 * sc["contracts"], oh[-2][1] * 100 * sc["contracts"]
                    value -= m
                    day -= m - mp
                    sc_total += m
            out["value"] = round(value, 2)
            out["dayChange"] = round(day, 2)
            if a.get("shortCalls"):
                out["shortCallMarks"] = round(sc_total, 2)
        out["valueCad"] = round(out["value"] * fx_of(a["cur"]), 2)

        # analyst view (stocks and option underlyings only)
        if a["kind"] != "etf":
            inf = safe(lambda: prov.info(ysym), {}, f"info {ysym}") or {}
            n = inf.get("numberOfAnalystOpinions")
            if n and inf.get("targetMeanPrice"):
                out["analyst"] = {
                    "rating": str(inf.get("recommendationKey", "")).replace("_", " ").title() or "—",
                    "n": int(n),
                    "avg": round(float(inf["targetMeanPrice"]), 2),
                    "low": round(float(inf.get("targetLowPrice") or inf["targetMeanPrice"]), 2),
                    "high": round(float(inf.get("targetHighPrice") or inf["targetMeanPrice"]), 2),
                    "asOf": today,
                    "src": f"https://finance.yahoo.com/quote/{ysym}/analysis/",
                }
            elif p.get("analyst"):
                out["analyst"] = p["analyst"]
        else:
            out["analyst"] = None

        # one-line note
        if out.get("price") and out.get("prev"):
            ch = (out["price"] / out["prev"] - 1) * 100
            base = close_on_or_before(out["history"], pos["baseDate"])
            since = f"; {pct((out['price'] / base - 1) * 100)} since {fmt_date(pos['baseDate'])}" if base else ""
            subj = a["sym"] if a["kind"] != "option" else f"{a['sym']} stock"
            out["note"] = f"{subj} {'rose' if ch > 0 else 'fell' if ch < 0 else 'was flat'} {abs(ch):.1f}% on the day{since}."
        assets.append(out)

    as_of = as_of or today

    # totals
    margin = pos.get("marginDebitUsd", 0)
    gross = sum(a["valueCad"] for a in assets)
    net = gross - margin * usdcad
    prev_net = 0.0
    for a in assets:
        fxp = usdcad_prev if a["cur"] == "USD" else 1.0
        prev_net += (a["value"] - a["dayChange"]) * fxp
    prev_net -= margin * usdcad_prev
    base_date = pos["baseDate"]
    base_fx = close_on_or_before(fx_hist, base_date) or usdcad
    base_val, base_ok = 0.0, True
    for a, src in zip(assets, pos["assets"]):
        if a["kind"] == "option":
            base_val += a["value"] * (base_fx if a["cur"] == "USD" else 1)  # held at today's mark
            continue
        b = close_on_or_before(a["history"], base_date)
        if b is None:
            base_ok = False
            break
        qty = sum(x["qty"] for x in src["positions"])
        base_val += (qty * b - a.get("shortCallMarks", 0)) * (base_fx if a["cur"] == "USD" else 1)
    base_val -= margin * base_fx
    vh = [v for v in prev.get("valueHistory", []) if v[0] != as_of] + [[as_of, round(net)]]
    vh = vh[-120:]
    since = round(net - base_val) if base_ok else round(net - vh[0][1])

    totals = {
        "netCad": round(net, 2), "dayCad": round(net - prev_net), "sinceBaseCad": since, "baseDate": base_date,
        "grossCad": round(gross, 2), "marginDebitUsd": margin,
        "wealthsimpleCad": pos.get("wealthsimple", {}).get("totalCad"), "wealthsimpleAsOf": pos.get("wealthsimple", {}).get("asOf"),
    }

    # market
    idx = []
    for i in pos.get("indexes", []):
        h = safe(lambda: prov.history(i["yahoo"], "1mo"), [], i["yahoo"]) or []
        if len(h) < 2:
            old = next((x for x in prev.get("market", {}).get("indexes", []) if x["name"] == i["name"]), None)
            if old:
                idx.append(old)
            continue
        c, pc = h[-1][1], h[-2][1]
        row = {"name": i["name"], "close": round(c, 2)}
        if i.get("unit"):
            row["unit"] = i["unit"]
        if i.get("unit") == "%":
            row["chgBp"] = round((c - pc) * 100)
        else:
            row["chgPct"] = round((c / pc - 1) * 100, 2)
        idx.append(row)
    movers = sorted([a for a in assets if a["kind"] != "option" and a.get("prev")],
                    key=lambda a: abs(a["price"] / a["prev"] - 1), reverse=True)
    parts = []
    for r in idx:
        if "chgPct" in r and r["name"] in ("S&P 500", "Nasdaq", "S&P/TSX"):
            parts.append(f"{r['name']} {pct(r['chgPct'])}")
    summary = (", ".join(parts) + "." if parts else "") + (
        f" Your biggest mover was {movers[0]['sym']} ({pct((movers[0]['price'] / movers[0]['prev'] - 1) * 100)})." if movers else "")
    market = {"date": as_of, "indexes": idx, "summary": summary.strip(),
              "src": "https://finance.yahoo.com/markets/"}

    # events
    events = []
    seen_syms = set()
    for a, src in zip(assets, pos["assets"]):
        if a["kind"] == "option":
            events.append({"date": a["expiry"], "title": f"{a['sym']} ${a['strike']:g} {a['optType']} expires", "sym": a["sym"]})
        for sc in src.get("shortCalls", []):
            events.append({"date": sc["expiry"], "title": f"{a['sym']} ${sc['strike']:g} short call expires ({sc['contracts']} contract)", "sym": a["sym"]})
        if a["kind"] != "etf" and a["sym"] not in seen_syms:
            seen_syms.add(a["sym"])
            for d in safe(lambda: prov.earnings_dates(src["yahoo"]), [], "earnings") or []:
                events.append({"date": d, "title": f"{a['sym']} earnings", "sym": a["sym"]})
    events = sorted({(e["date"], e["title"]): e for e in events if e["date"] >= today}.values(), key=lambda e: e["date"])

    # articles
    arts, seen = [], set()
    for a, src in zip(assets, pos["assets"]):
        for item in (safe(lambda: prov.news(src["yahoo"]), [], "news") or [])[:4]:
            n = norm_news(item, a["sym"])
            if not n:
                continue
            if n["url"] in seen:
                for x in arts:
                    if x["url"] == n["url"] and a["sym"] not in x["syms"]:
                        x["syms"].append(a["sym"])
                continue
            seen.add(n["url"])
            arts.append(n)
    if not arts:
        arts = prev.get("articles", [])
        notes.append("articles carried over")
    arts = sorted(arts, key=lambda x: x["date"], reverse=True)[:MAX_ARTICLES]

    watch = build_watch(assets, pos, events, gross, usdcad, usdcad_prev, today)

    return {
        "asOf": as_of,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "updateNote": "Prices from Yahoo Finance at the " + fmt_date(as_of) + " close; positions as of "
                      + fmt_date(pos.get("positionsAsOf", as_of)) + (". " + "; ".join(notes) + "." if notes else "."),
        "fx": {"usdcad": round(usdcad, 4), "prev": round(usdcad_prev, 4), "src": "https://finance.yahoo.com/quote/CAD=X/"},
        "totals": totals,
        "accounts": pos.get("accounts", []),
        "assets": assets,
        "valueHistory": vh,
        "market": market,
        "watch": watch,
        "events": events,
        "articles": arts,
        **({"sentiment": prev["sentiment"]} if prev.get("sentiment") else {}),
        **({"trending": prev["trending"]} if prev.get("trending") else {}),
        "sources": [{"label": "Prices: Yahoo Finance", "url": "https://finance.yahoo.com/"}],
    }


def fmt_date(s):
    d = dt.date.fromisoformat(s)
    return d.strftime("%b ") + str(d.day)


def build_watch(assets, pos, events, gross, fx, fx_prev, today):
    w = []
    by_id = {a["id"]: a for a in assets}
    for src in pos["assets"]:
        a = by_id[src["id"]]
        for sc in src.get("shortCalls", []):
            gap = sc["strike"] - (a["price"] or 0)
            dleft = days_between(today, sc["expiry"])
            if dleft < 0:
                continue
            shares = 100 * sc["contracts"]
            if gap <= 0:
                w.append({"level": "high", "sym": a["sym"], "title": f"{a['sym']} is above your ${sc['strike']:g} call strike ({fmt_date(sc['expiry'])})",
                          "body": f"{a['sym']} closed at ${a['price']:.2f}, ${-gap:.2f} over the strike with {dleft} days left. If it stays above ${sc['strike']:g} at expiry, {shares} shares are likely called away at ${sc['strike']:g}."})
            elif gap / sc["strike"] < 0.03:
                w.append({"level": "high", "sym": a["sym"], "title": f"{a['sym']} is ${gap:.2f} below your ${sc['strike']:g} call strike ({fmt_date(sc['expiry'])})",
                          "body": f"{dleft} days to expiry. A close above ${sc['strike']:g} at expiry would likely see {shares} shares called away, capping your upside."})
        if a["kind"] == "option":
            dleft = days_between(today, a["expiry"])
            itm = a["price"] >= a["strike"] if a["optType"] == "call" else a["price"] <= a["strike"]
            gap = abs(a["price"] - a["strike"])
            body = f"{a['sym']} ${a['price']:.2f} vs the ${a['strike']:g} strike, {dleft} days left. Marked at {fmt_money(a['mark'])}"
            body += f" vs {fmt_money(a['cost'])} cost." if a.get("cost") else "."
            w.append({"level": "med" if dleft < 45 else "low", "sym": a["sym"],
                      "title": f"{a['sym']} {a['optType']} is {'in' if itm else 'out of'} the money by ${gap:.2f}", "body": body})
    for e in events:
        if "earnings" in e["title"] and 0 <= days_between(today, e["date"]) <= 14:
            w.append({"level": "high", "sym": e["sym"], "title": f"{e['sym']} reports earnings {fmt_date(e['date'])}",
                      "body": f"In {days_between(today, e['date'])} days. Shares often move sharply after results."})
    if gross:
        top = max(assets, key=lambda a: a["valueCad"])
        share = top["valueCad"] / gross * 100
        if share >= 25:
            body = f"{top['sym']} is worth about C${top['valueCad'] / 1000:,.1f}k of C${gross / 1000:,.1f}k gross holdings."
            if pos.get("marginDebitUsd"):
                body += f" Your margin loan is about US${pos['marginDebitUsd'] / 1000:,.1f}k; rate rises increase its interest cost."
            w.append({"level": "med", "sym": top["sym"], "title": f"One holding is {share:.0f}% of your holdings", "body": body})
        usd = sum(a["valueCad"] for a in assets if a["cur"] == "USD") / gross * 100
        w.append({"level": "low", "sym": None, "title": f"About {usd:.0f}% of your holdings are in US dollars",
                  "body": f"USD/CAD moved from {fx_prev:.4f} to {fx:.4f}. A stronger loonie lowers their CAD value."})
    order = {"high": 0, "med": 1, "low": 2}
    return sorted(w, key=lambda x: order[x["level"]])[:7]


def main():
    pos = json.loads(POSITIONS.read_text(encoding="utf-8"))
    prev = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    data = build(pos, prev, YahooProvider())
    if not any(a.get("history") for a in data["assets"]):
        sys.exit("No prices fetched; leaving data.json unchanged.")
    DATA.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"data.json updated: as of {data['asOf']}, net C${data['totals']['netCad']:,.0f}, day {data['totals']['dayCad']:+,} CAD")


if __name__ == "__main__":
    main()
