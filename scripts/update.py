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


def xaus_gold():
    """Daily spot gold closes in US dollars from xaus.com's free public API (no key). Oldest first."""
    points = json.loads(fetch_text("https://xaus.com/api/v1/history"))["points"]
    rows = []
    for p in points:
        try:
            rows.append((str(p["d"])[:10], float(p["c"])))
        except (KeyError, ValueError, TypeError):
            continue
    rows.sort()
    if len(rows) < 30:
        raise ValueError("too few price rows")
    return rows


def yahoo_gold():
    """Backup: daily gold futures closes (GC=F) from Yahoo Finance's public chart feed."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?range=1y&interval=1d"
    result = json.loads(fetch_text(url))["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    rows = []
    for stamp, close in zip(result["timestamp"], closes):
        if close is None:
            continue
        day = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%d")
        rows.append((day, float(close)))
    if len(rows) < 30:
        raise ValueError("too few price rows")
    return rows


GOLD_SOURCES = (
    ("xaus.com spot gold", xaus_gold),
    ("Yahoo Finance gold futures", yahoo_gold),
)
MAX_PRICE_AGE_DAYS = 10
PRICE_RANGE = (500, 20000)  # a close outside this is a bad feed, not a gold price


def gold_prices(limit=300):
    """Tries each gold source in turn. Returns (rows, source name)."""
    errors = []
    for name, fn in GOLD_SOURCES:
        try:
            rows = fn()[-limit:]
            last_day = dt.date.fromisoformat(rows[-1][0])
            if (dt.date.today() - last_day).days > MAX_PRICE_AGE_DAYS:
                raise ValueError(f"latest price is from {rows[-1][0]}, too old")
            if not all(PRICE_RANGE[0] < v < PRICE_RANGE[1] for _, v in rows):
                raise ValueError("prices outside the believable range")
            return rows, name
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:80]}")
    raise ValueError(" | ".join(errors))


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


# Fed hike gauge. The gap between the 2-year Treasury yield and the current fed funds rate shows how
# many hikes (or cuts) the bond market expects over about two years. Change these numbers to retune it.
GAUGE_MIN, GAUGE_MAX = -0.50, 1.00   # ends of the dial, in percentage points of gap
GAUGE_CUTS = -0.25                   # gap at or below this = cuts priced
GAUGE_HOLD = 0.15                    # gap below this = on hold
GAUGE_STRONG = 0.50                  # gap at or above this = hikes strongly priced
GAUGE_EASING_OFF = 0.20              # 2-year yield this far under its 20-day high, and falling = easing off


def judge_fed_gauge(two_year, funds):
    """Rules: gap = 2-year yield minus effective fed funds rate. At or below -0.25 = cuts priced.
    Below +0.15 = on hold. +0.15 or more but the 2-year yield is 0.20+ under its 20-day high and falling
    over 5 days = easing off. +0.50 or more = hikes strongly priced. Otherwise hikes priced.
    One quarter-point hike is 0.25, so gap / 0.25 is a rough count of hikes."""
    if not two_year or not funds:
        return None
    y = [v for _, v in two_year]
    last2, d2 = y[-1], two_year[-1][0]
    rate, dff = funds[-1][1], funds[-1][0]
    gap = last2 - rate
    hi20 = max(y[-20:])
    off = hi20 - last2
    chg5 = last2 - y[-6] if len(y) > 5 else 0.0
    steps = gap / 0.25
    if gap <= GAUGE_CUTS:
        status, label = "cuts", "Cuts priced"
    elif gap < GAUGE_HOLD:
        status, label = "hold", "On hold"
    elif off >= GAUGE_EASING_OFF and chg5 < 0:
        status, label = "easing", "Hike pricing easing"
    elif gap >= GAUGE_STRONG:
        status, label = "strong", "Hikes strongly priced"
    else:
        status, label = "hikes", "Hikes priced"
    if gap >= GAUGE_HOLD:
        meaning = f"That is about {max(1, round(steps))} quarter-point hike{'s' if round(steps) > 1 else ''} priced in over the next two years."
    elif gap <= GAUGE_CUTS:
        meaning = f"That is about {max(1, round(-steps))} quarter-point cut{'s' if round(-steps) > 1 else ''} priced in over the next two years."
    else:
        meaning = "The market expects the Fed to stay roughly where it is."
    reading = (f"The 2-year Treasury yield was {last2:.2f}% on {d2} and the effective fed funds rate was "
               f"{rate:.2f}% on {dff}. The gap is {gap:+.2f} points. {meaning} The 2-year yield is "
               f"{off:.2f} points below its 20-day high and moved {chg5:+.2f} over the last 5 trading days.")
    return {"status": status, "label": label, "reading": reading, "gap": round(gap, 3), "steps": round(steps, 1),
            "twoYear": round(last2, 3), "twoYearHigh20": round(hi20, 3), "fundsRate": round(rate, 3),
            "chg5": round(chg5, 3), "min": GAUGE_MIN, "max": GAUGE_MAX,
            "zones": [GAUGE_CUTS, GAUGE_HOLD, GAUGE_STRONG], "updated": d2}


def clean_fedwatch(raw):
    """Passes the hand-typed FedWatch box through only if it is usable."""
    try:
        pct = float(raw["hike25"])
        if not 0 <= pct <= 100:
            return None
        return {"meeting": str(raw.get("meeting", "")), "hike25": round(pct, 1),
                "source": str(raw.get("source", "CME FedWatch"))[:80], "updated": str(raw.get("updated", ""))[:10]}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


# Blow-off check thresholds. Change these numbers to make the check stricter or looser.
BLOWOFF_STRETCH = 25.0   # percent above the 200-day average
BLOWOFF_SPEED = 15.0     # percent gained in 20 trading days
BLOWOFF_HOT_PCT = 3.0    # a "hot day" is a daily gain of at least this percent
BLOWOFF_HOT_DAYS = 3     # hot days needed within the last 10 trading days
BLOWOFF_REVERSAL = 8.0   # percent below the 20-day high that counts as a turn after a stretch


def judge_blowoff(rows):
    """Rules: count how many of three conditions are met (far above the 200-day average,
    gained a lot in 20 days, several big up days in the last 10). 0 = not present, 1 = early signs,
    2 or 3 = blow-off conditions. If gold was stretched in the last 20 days and has since dropped
    8% from its 20-day high, the status is 'possible top is in'. Needs about 220 daily closes."""
    vals = [v for _, v in rows]
    if len(vals) < 220:
        return None
    date, last = rows[-1][0], vals[-1]
    ma200 = sum(vals[-200:]) / 200
    stretch = (last / ma200 - 1) * 100
    peak_stretch = max(
        (vals[i] / (sum(vals[i - 199:i + 1]) / 200) - 1) * 100 for i in range(len(vals) - 20, len(vals))
    )
    gain20 = (last / vals[-21] - 1) * 100
    hot = sum(1 for i in range(len(vals) - 10, len(vals)) if (vals[i] / vals[i - 1] - 1) * 100 >= BLOWOFF_HOT_PCT)
    high20 = max(vals[-20:])
    off_high = (1 - last / high20) * 100

    checks = [
        {"id": "stretch", "label": "Distance above the 200-day average", "value": round(stretch, 1), "limit": BLOWOFF_STRETCH,
         "unit": "%", "hit": stretch >= BLOWOFF_STRETCH, "counts": True},
        {"id": "speed", "label": "Gain over the last 20 trading days", "value": round(gain20, 1), "limit": BLOWOFF_SPEED,
         "unit": "%", "hit": gain20 >= BLOWOFF_SPEED, "counts": True},
        {"id": "hot", "label": "Days up %g%% or more, of the last 10" % BLOWOFF_HOT_PCT, "value": hot, "limit": BLOWOFF_HOT_DAYS,
         "unit": "days", "hit": hot >= BLOWOFF_HOT_DAYS, "counts": True},
        {"id": "reversal", "label": "Drop from the 20-day high", "value": round(off_high, 1), "limit": BLOWOFF_REVERSAL,
         "unit": "%", "hit": off_high >= BLOWOFF_REVERSAL, "counts": False},
    ]
    count = sum(1 for c in checks if c["counts"] and c["hit"])
    base = (f"Gold closed at ${last:,.0f} on {date}, {abs(stretch):.1f}% "
            f"{'above' if stretch >= 0 else 'below'} its 200-day average of ${ma200:,.0f}.")
    if peak_stretch >= BLOWOFF_STRETCH and off_high >= BLOWOFF_REVERSAL:
        status, label = "reversal", "Possible top is in"
        why = (f"Within the last 20 days gold was up to {peak_stretch:.0f}% above its 200-day average, "
               f"and it has since fallen {off_high:.1f}% from its 20-day high. That is the pattern after a blow-off top.")
    elif count >= 2:
        status, label = "active", "Blow-off conditions"
        why = f"{count} of 3 blow-off conditions are met. The rally is stretched and moving fast."
    elif count == 1:
        status, label = "early", "Early signs"
        why = "1 of 3 blow-off conditions is met. Watch whether the others follow."
    else:
        status, label = "none", "Not present"
        why = "0 of 3 blow-off conditions are met."
    return {"status": status, "label": label, "reading": base + " " + why, "count": count,
            "checks": checks, "updated": date}


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
    two_year = attempt("2-year Treasury yield (FRED DGS2)", need_key("DGS2"))
    gold_result = attempt("Gold price (xaus.com, then Yahoo)", gold_prices)
    gold, gold_source = gold_result if gold_result else (None, "")
    headlines = attempt("News headlines (Google News)", news)
    fed_news = attempt("Fed releases (federalreserve.gov)", fed_releases)

    info = dict(manual.get("info", {}))
    record = info.get("record")
    series = {}
    if gold:
        last_date, last_close = gold[-1]
        info["price"] = round(last_close, 2)
        info["priceDate"] = last_date
        info["priceSource"] = gold_source
        low_close = min(gold, key=lambda r: r[1])
        if low_close[1] < float(info.get("low", 1e12)):
            info["low"] = round(low_close[1], 2)
            info["lowDate"] = low_close[0]
        series["gold"] = last_n(gold)
    if real_yield:
        series["real_yield"] = last_n(real_yield)
    if dollar:
        series["dollar"] = last_n(dollar)
    if two_year:
        series["two_year"] = last_n(two_year)

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
            if auto == "price" and gold_source:
                reading += f" Source: {gold_source}."
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
        "fedgauge": judge_fed_gauge(two_year, funds),
        "fedwatch": clean_fedwatch(manual.get("fedwatch")),
        "blowoff": judge_blowoff(gold) if gold else None,
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
