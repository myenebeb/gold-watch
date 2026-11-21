#!/usr/bin/env python3
"""Gold Watch updater.

Reads manual.json (your hand-edited notes), pulls free public data, works out
the signal statuses with simple written rules, and writes data.json.

Uses only the Python standard library, so nothing needs to be installed.
Every data source is wrapped so that one failing source never stops the rest.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANUAL = os.path.join(ROOT, "manual.json")
OUT = os.path.join(ROOT, "data.json")
UA = "Mozilla/5.0 (compatible; GoldWatch/1.0)"

NEWS_QUERIES = [
    ("Fed", "gold Fed rate hike when:3d"),
    ("Central banks", "central bank gold buying PBOC when:7d"),
    ("ETF flows", "gold ETF flows when:7d"),
    ("Geopolitics", "gold price war safe haven when:3d"),
    ("Macro", "gold price dollar yields when:2d"),
]
FED_FEED = "https://www.federalreserve.gov/feeds/press_monetary.xml"


# ---------------------------------------------------------------- fetching
def fetch_text(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fred_series(series_id, key, limit=90):
    """Returns [(date, value), ...] oldest first."""
    query = urllib.parse.urlencode({
        "series_id": series_id, "api_key": key, "file_type": "json",
        "sort_order": "desc", "limit": limit,
    })
    data = json.loads(fetch_text("https://api.stlouisfed.org/fred/series/observations?" + query))
    rows = []
    for obs in data.get("observations", []):
        try:
            rows.append((obs["date"], float(obs["value"])))
        except (KeyError, ValueError, TypeError):
            continue  # FRED uses "." for missing days
    rows.sort()
    if not rows:
        raise ValueError("no observations returned")
    return rows


def gold_prices(limit=120):
    """Daily gold closes in US dollars from Stooq's free CSV. Oldest first."""
    text = fetch_text("https://stooq.com/q/d/l/?s=xauusd&i=d")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        try:
            rows.append((rec["Date"], float(rec["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if len(rows) < 30:
        raise ValueError("too few price rows")
    return rows[-limit:]


def parse_rss(text):
    root = ET.fromstring(text)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        when = None
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            pass
        source = (item.findtext("source") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "when": when, "source": source})
    return items


def news(max_items=30):
    seen, out, ok_any, last_err = set(), [], False, None
    for cat, q in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            for it in parse_rss(fetch_text(url)):
                ok_any = True
                key = it["title"].lower()
                if key in seen:
                    continue
                seen.add(key)
                it["cat"] = cat
                out.append(it)
        except Exception as exc:  # one feed failing is fine
            last_err = exc
    if not ok_any:
        raise ValueError(f"no news feed answered ({last_err})")
    out.sort(key=lambda i: i["when"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return [{
        "title": i["title"], "link": i["link"], "cat": i["cat"], "source": i["source"],
        "when": i["when"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if i["when"] else "",
    } for i in out[:max_items]]


def fed_releases(max_items=5):
    items = parse_rss(fetch_text(FED_FEED))
    if not items:
        raise ValueError("empty feed")
    return [{
        "title": i["title"], "link": i["link"], "cat": "Fed", "source": "Federal Reserve",
        "when": i["when"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if i["when"] else "",
    } for i in items[:max_items]]


# ---------------------------------------------------------------- signal rules
def sma(vals, n):
    window = vals[-n:]
    return sum(window) / len(window)


def judge_real_yield(rows):
    """Rules: near its 20-day high = headwind. Fallen 0.30 points in 20 days = tailwind.
    Off the high by 0.10+ and still falling over 5 days = turning. Otherwise watching."""
    vals = [v for _, v in rows]
    last, date = vals[-1], rows[-1][0]
    hi20 = max(vals[-20:])
    chg5 = last - vals[-6] if len(vals) > 5 else 0.0
    chg20 = last - vals[-21] if len(vals) > 20 else 0.0
    if last >= hi20 - 0.03:
        status = "bearish"
    elif chg20 <= -0.30:
        status = "bullish"
    elif last <= hi20 - 0.10 and chg5 < 0:
        status = "turning"
    else:
        status = "neutral"
    reading = (f"The 10-year real yield was {last:.2f}% on {date}. Its 20-day high is {hi20:.2f}%, "
               f"and it moved {chg5:+.2f} points over the last 5 trading days.")
    return status, reading, date


def judge_dollar(rows):
    """Rules: within 0.3% of its 20-day high = headwind. Down 2% from the high = tailwind.
    Down 0.8%+ and still falling over 5 days = turning. Otherwise watching."""
    vals = [v for _, v in rows]
    last, date = vals[-1], rows[-1][0]
    hi20 = max(vals[-20:])
    off = (last / hi20 - 1) * 100
    chg5 = (last / vals[-6] - 1) * 100 if len(vals) > 5 else 0.0
    if off >= -0.3:
        status = "bearish"
    elif off <= -2.0:
        status = "bullish"
    elif off <= -0.8 and chg5 < 0:
        status = "turning"
    else:
        status = "neutral"
    reading = (f"The broad US dollar index was {last:.1f} on {date}, {abs(off):.1f}% below its 20-day high "
               f"and {chg5:+.1f}% over the last 5 trading days.")
    return status, reading, date


def judge_price(rows, record):
    """Rules: below both the 20-day and 50-day average, with the 20-day under the 50-day = headwind.
    Above both with the 20-day over the 50-day = tailwind. Back above the 20-day but 20-day still
    under the 50-day = turning. Anything else = watching."""
    vals = [v for _, v in rows]
    last, date = vals[-1], rows[-1][0]
    a20, a50 = sma(vals, 20), sma(vals, 50)
    if last < a20 and a20 < a50:
        status = "bearish"
    elif last > a20 and a20 > a50:
        status = "bullish"
    elif last > a20 and a20 < a50:
        status = "turning"
    else:
        status = "neutral"
    below = f" That is {round((1 - last / record) * 100)}% below the record near ${record:,.0f}." if record else ""
    reading = (f"Gold last closed at ${last:,.0f} on {date}. The 20-day average is ${a20:,.0f} and the "
               f"50-day average is ${a50:,.0f}.{below}")
    return status, reading, date


def last_n(rows, n=60):
    return [round(v, 4) for _, v in rows[-n:]]


# ---------------------------------------------------------------- main
def main():
    with open(MANUAL, encoding="utf-8") as fh:
        manual = json.load(fh)

    health = []

    def attempt(name, fn):
        try:
            result = fn()
            health.append({"source": name, "ok": True})
            return result
        except Exception as exc:
            health.append({"source": name, "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:140]}"})
            return None

    key = os.environ.get("FRED_API_KEY", "").strip()

    def need_key(series):
        def run():
            if not key:
                raise ValueError("FRED_API_KEY is not set")
            return fred_series(series, key)
        return run

    real_yield = attempt("Real yield (FRED DFII10)", need_key("DFII10"))
    dollar = attempt("Dollar index (FRED DTWEXBGS)", need_key("DTWEXBGS"))
    funds = attempt("Fed funds rate (FRED DFF)", need_key("DFF"))
    gold = attempt("Gold price (Stooq)", gold_prices)
    headlines = attempt("News headlines (Google News)", news)
    fed_news = attempt("Fed releases (federalreserve.gov)", fed_releases)

    info = dict(manual.get("info", {}))
    record = info.get("record")
    series = {}
    if gold:
        last_date, last_close = gold[-1]
        info["price"] = round(last_close, 2)
        info["priceDate"] = last_date
        low_close = min(gold, key=lambda r: r[1])
        if low_close[1] < float(info.get("low", 1e12)):
            info["low"] = round(low_close[1], 2)
            info["lowDate"] = low_close[0]
        series["gold"] = last_n(gold)
    if real_yield:
        series["real_yield"] = last_n(real_yield)
    if dollar:
        series["dollar"] = last_n(dollar)

    judges = {
        "real_yield": (real_yield, judge_real_yield),
        "dollar": (dollar, judge_dollar),
        "price": (gold, lambda rows: judge_price(rows, record)),
    }

    today = dt.date.today().isoformat()
    signals = []
    for sig in sorted(manual.get("signals", []), key=lambda s: s.get("order", 99)):
        out = {"id": sig["id"], "order": sig.get("order", 99), "name": sig["name"], "watch": sig.get("watch", "")}
        if sig.get("series") in series:
            out["series"] = sig["series"]
        auto = sig.get("auto")
        rows, judge = judges.get(auto, (None, None))
        if judge and rows:
            status, reading, when = judge(rows)
            out.update(status=status, reading=reading, updated=when, source="auto")
        elif auto == "fed" or "status" in sig:
            out.update(status=sig["status"], reading=sig["reading"], updated=sig.get("updated", ""), source="manual")
            if auto == "fed" and funds:
                out["reading"] += f" The effective fed funds rate was {funds[-1][1]:.2f}% on {funds[-1][0]}."
        else:
            fb = sig.get("fallback", {})
            out.update(status=fb.get("status", "neutral"),
                       reading=fb.get("reading", "Waiting for the first data run."),
                       updated=fb.get("updated", ""), source="fallback")
        signals.append(out)

    all_headlines, seen_titles = [], set()
    for group in (fed_news, headlines):
        for item in group or []:
            key = item["title"].lower()
            if key not in seen_titles:  # the same story can arrive from two feeds
                seen_titles.add(key)
                all_headlines.append(item)
    all_headlines.sort(key=lambda h: h.get("when", ""), reverse=True)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    previous_refresh = ""
    try:
        with open(OUT, encoding="utf-8") as fh:
            previous_refresh = json.load(fh).get("refreshed", "")
    except (OSError, ValueError):
        pass
    # "refreshed" moves only when at least one live source answered, so the page never claims fresh data it does not have.
    refreshed = now if any(h["ok"] for h in health) else previous_refresh

    data = {
        "generated": now,
        "refreshed": refreshed,
        "today": today,
        "health": health,
        "info": info,
        "cycle": manual.get("cycle", {}),
        "signals": signals,
        "events": manual.get("events", []),
        "headlines": all_headlines[:30],
        "series": series,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    ok = sum(1 for h in health if h["ok"])
    print(f"Wrote data.json. {ok} of {len(health)} data sources answered.")
    for h in health:
        if not h["ok"]:
            print(f"  - {h['source']}: {h.get('detail', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
