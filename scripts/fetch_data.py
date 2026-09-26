#!/usr/bin/env python3
"""Fetch upcoming economic releases, earnings, stock splits, and IPOs into site/data/calendar.json.

Standard library only — no pip installs. Run locally with:  python scripts/fetch_data.py

Sources (no API key needed):
  - Economic calendar: Forex Factory (current week) + Nasdaq (next 3 weeks)
  - Earnings + call timing (before open / after close): Nasdaq public calendar API
  - Stock splits: Nasdaq public calendar API
  - Upcoming IPOs: Nasdaq public calendar API
Optional fallback for earnings: Finnhub (set FINNHUB_API_KEY as a repo secret).

If a source fails, the previous data for that section is kept and marked stale,
so one flaky endpoint never blanks the dashboard.
"""
import html
import json
import re
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data", "calendar.json")
ET = ZoneInfo("America/New_York")
EARNINGS_DAYS = int(os.environ.get("EARNINGS_DAYS", "21"))
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
PLAIN_HEADERS = {"User-Agent": "market-calendar-dashboard/1.0 (GitHub Actions)"}

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]


def log(msg):
    print(msg, flush=True)


def get_json(url, headers=None, retries=3, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or PLAIN_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404):
                raise
        except Exception as e:  # network error, timeout, bad JSON
            last = e
        time.sleep(2 * (attempt + 1))
    raise last


def money(v):
    """'$1,234.5' -> 1234.5, '($0.12)' -> -0.12, 'N/A' -> None."""
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def mdy(s):
    """'09/30/2026' -> '2026-09-30'."""
    try:
        return datetime.strptime(str(s).strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def today_et():
    return datetime.now(ET).date()


def weekdays(start, n_days):
    for i in range(n_days):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            yield d


# ---------------------------------------------------------------- economic
# Two sources: Forex Factory (current week, real impact ratings, reliable times)
# and Nasdaq (any date, so it covers the weeks Forex Factory hasn't published).
# Forex Factory wins wherever both cover the same days.

COUNTRY_CCY = {
    "united states": "USD", "us": "USD", "usa": "USD",
    "euro zone": "EUR", "eurozone": "EUR", "euro area": "EUR", "european union": "EUR",
    "germany": "EUR", "france": "EUR", "italy": "EUR", "spain": "EUR",
    "united kingdom": "GBP", "uk": "GBP", "japan": "JPY", "canada": "CAD",
    "australia": "AUD", "new zealand": "NZD", "switzerland": "CHF", "china": "CNY",
}
HIGH_KW = ["nonfarm", "non-farm", "unemployment rate", "cpi", "consumer price index",
           "pce", "interest rate decision", "fomc", "fed funds", "federal funds", "gdp",
           "retail sales", "ism manufacturing", "ism non-manufacturing", "ism services",
           "jolts", "average hourly earnings", "ppi", "producer price", "jobless claims",
           "powell", "employment change", "monetary policy statement", "rate statement"]
MED_KW = ["pmi", "consumer confidence", "consumer sentiment", "michigan", "durable goods",
          "housing starts", "building permits", "existing home", "new home", "pending home",
          "trade balance", "industrial production", "adp", "factory orders", "crude oil",
          "empire state", "philadelphia fed", "philly fed", "personal income",
          "personal spending", "beige book", "minutes", "business confidence", "zew", "ifo"]


def classify_impact(title):
    t = title.lower()
    if any(k in t for k in HIGH_KW):
        return "High"
    if any(k in t for k in MED_KW):
        return "Medium"
    return "Low"


def norm_title(t):
    return "".join(ch for ch in t.lower() if ch.isalnum())


def fetch_ff():
    events, got_first = [], False
    for i, url in enumerate(FF_URLS):
        try:
            rows = get_json(url)
        except urllib.error.HTTPError as e:
            if i > 0 and e.code == 404:
                continue
            raise
        if i == 0:
            got_first = True
        for r in rows or []:
            try:
                dt = datetime.fromisoformat(r["date"]).astimezone(ET)
            except (KeyError, TypeError, ValueError):
                continue
            events.append({
                "type": "econ", "src": "Forex Factory",
                "ts": dt.isoformat(), "date": dt.date().isoformat(),
                "title": (r.get("title") or "").strip(),
                "country": (r.get("country") or "").strip(),
                "impact": (r.get("impact") or "").strip(),
                "forecast": (r.get("forecast") or "").strip(),
                "previous": (r.get("previous") or "").strip(),
            })
        time.sleep(3)  # the feed asks for polite polling
    if not got_first:
        raise RuntimeError("Forex Factory feed returned nothing")
    return events


def parse_clock(s):
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", s or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, mi


def fetch_nasdaq_econ(start, n_days):
    out, days, failures = [], 0, 0
    for d in weekdays(start, n_days):
        days += 1
        try:
            j = get_json(f"https://api.nasdaq.com/api/calendar/economicevents?date={d.isoformat()}",
                         headers=NASDAQ_HEADERS)
        except Exception as e:
            failures += 1
            log(f"  nasdaq econ {d}: {e}")
            continue
        for r in ((j or {}).get("data") or {}).get("rows") or []:
            title = html.unescape((r.get("eventName") or "").strip())
            if not title:
                continue
            country = html.unescape((r.get("country") or "").strip())
            clock = parse_clock(r.get("gmt") or r.get("time") or "")
            utc = (datetime(d.year, d.month, d.day, clock[0], clock[1], tzinfo=timezone.utc)
                   if clock else None)
            out.append({
                "type": "econ", "src": "Nasdaq", "_utc": utc, "_day": d,
                "title": title,
                "country": COUNTRY_CCY.get(country.lower(), country),
                "impact": classify_impact(title),
                "forecast": html.unescape((r.get("consensus") or "").strip()),
                "previous": html.unescape((r.get("previous") or "").strip()),
            })
        time.sleep(1)
    if days and failures == days:
        raise RuntimeError("Nasdaq economic endpoint unreachable for every day")
    return out


def finish_nasdaq(rows, offset_min):
    """Apply the time-zone correction and give each row a final ts/date."""
    for e in rows:
        utc, d = e.pop("_utc"), e.pop("_day")
        if utc is None:
            e["ts"], e["date"] = None, d.isoformat()
        else:
            dt = (utc + timedelta(minutes=offset_min)).astimezone(ET)
            e["ts"], e["date"] = dt.isoformat(), dt.date().isoformat()
    return rows


def calibrate(ff, nq):
    """Nasdaq's time column is labelled GMT. Confirm that against events both
    sources list; if Nasdaq turns out to be in another zone, shift by the gap."""
    ff_by = {}
    for e in ff:
        ff_by.setdefault((e["country"], norm_title(e["title"])), []).append(e)
    gaps = []
    for e in nq:
        if e["_utc"] is None:
            continue
        for f in ff_by.get((e["country"], norm_title(e["title"])), []):
            gap = (datetime.fromisoformat(f["ts"]) - e["_utc"]).total_seconds() / 60
            if abs(gap) <= 14 * 60:
                gaps.append(int(round(gap / 30.0)) * 30)
    if not gaps:
        return 0
    best = max(set(gaps), key=gaps.count)
    log(f"  Nasdaq time offset from {len(gaps)} matched events: {best} min")
    return best


def fetch_economic():
    today = today_et()
    ff, nq, errors = None, None, []
    try:
        ff = fetch_ff()
        log(f"  Forex Factory: {len(ff)}")
    except Exception as e:
        errors.append(f"Forex Factory: {e}")
        log(f"  Forex Factory failed: {e}")

    start = today
    if ff:
        ff_min = min(date.fromisoformat(e["date"]) for e in ff)
        start = max(min(today, ff_min), today - timedelta(days=7))  # overlap for calibration
    try:
        nq = fetch_nasdaq_econ(start, (today - start).days + EARNINGS_DAYS)
        log(f"  Nasdaq: {len(nq)}")
    except Exception as e:
        errors.append(f"Nasdaq: {e}")
        log(f"  Nasdaq economic failed: {e}")

    if ff is None and nq is None:
        raise RuntimeError("; ".join(errors))
    if ff and nq:
        nq = finish_nasdaq(nq, calibrate(ff, nq))
        ff_min = min(e["date"] for e in ff)
        ff_max = max(e["date"] for e in ff)
        nq = [e for e in nq if not (ff_min <= e["date"] <= ff_max)]
        label = "Forex Factory + Nasdaq"
    elif nq is not None:
        nq = finish_nasdaq(nq, 0)
        label = "Nasdaq only (Forex Factory down)"
    else:
        label = "Forex Factory only (Nasdaq down)"

    seen, out = set(), []
    for e in (ff or []) + (nq or []):
        k = (e["date"], e["ts"], e["country"], e["title"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out, label


# ---------------------------------------------------------------- earnings
SESSION = {"time-pre-market": "BMO", "time-after-hours": "AMC"}


def fetch_earnings_nasdaq(start):
    out, days, failures = [], 0, 0
    for d in weekdays(start, EARNINGS_DAYS):
        days += 1
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
        try:
            j = get_json(url, headers=NASDAQ_HEADERS)
        except Exception as e:
            failures += 1
            log(f"  earnings {d}: {e}")
            continue
        rows = ((j or {}).get("data") or {}).get("rows") or []
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            out.append({
                "type": "earnings",
                "date": d.isoformat(),
                "symbol": sym,
                "name": (r.get("name") or "").strip(),
                "session": SESSION.get(r.get("time"), "TNS"),
                "market_cap": money(r.get("marketCap")),
                "eps_est": money(r.get("epsForecast")),
                "num_est": int(money(r.get("noOfEsts")) or 0) or None,
                "fiscal_q": (r.get("fiscalQuarterEnding") or "").strip(),
                "last_year_eps": money(r.get("lastYearEPS")),
                "rev_est": None,
            })
        time.sleep(1)
    if days and failures == days:
        raise RuntimeError("Nasdaq earnings endpoint unreachable for every day")
    return out


def fetch_earnings_finnhub(start):
    end = start + timedelta(days=EARNINGS_DAYS)
    url = (f"https://finnhub.io/api/v1/calendar/earnings?from={start.isoformat()}"
           f"&to={end.isoformat()}&token={FINNHUB_KEY}")
    j = get_json(url)
    hour = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}
    out = []
    for r in (j or {}).get("earningsCalendar") or []:
        if not r.get("symbol") or not r.get("date"):
            continue
        out.append({
            "type": "earnings",
            "date": r["date"],
            "symbol": r["symbol"].upper(),
            "name": "",
            "session": hour.get((r.get("hour") or "").lower(), "TNS"),
            "market_cap": None,
            "eps_est": r.get("epsEstimate"),
            "num_est": None,
            "fiscal_q": f"Q{r.get('quarter')} {r.get('year')}" if r.get("quarter") else "",
            "last_year_eps": None,
            "rev_est": r.get("revenueEstimate"),
        })
    return out


def fetch_earnings():
    start = today_et()
    try:
        return fetch_earnings_nasdaq(start), "Nasdaq"
    except Exception as e:
        if not FINNHUB_KEY:
            raise
        log(f"  Nasdaq failed ({e}); falling back to Finnhub")
        return fetch_earnings_finnhub(start), "Finnhub"


# ---------------------------------------------------------------- splits
def fetch_splits():
    j = get_json("https://api.nasdaq.com/api/calendar/splits", headers=NASDAQ_HEADERS)
    rows = ((j or {}).get("data") or {}).get("rows")
    if rows is None:
        raise RuntimeError("unexpected splits response shape")
    out = []
    for r in rows:
        d = mdy(r.get("executionDate"))
        if not d:
            continue
        out.append({
            "type": "split",
            "date": d,
            "symbol": (r.get("symbol") or "").strip().upper(),
            "name": (r.get("name") or "").strip(),
            "ratio": (r.get("ratio") or "").strip(),
            "announced": mdy(r.get("announcedDate")),
            "payable": mdy(r.get("payableDate")),
        })
    return out


# ---------------------------------------------------------------- IPOs
def fetch_ipos():
    """Upcoming IPOs for this month and next. Date = expected pricing date;
    shares usually start trading the next morning."""
    t = today_et()
    nxt = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
    out, seen, ok = [], set(), 0
    for m in (t, nxt):
        url = f"https://api.nasdaq.com/api/calendar/ipos?date={m.strftime('%Y-%m')}"
        try:
            j = get_json(url, headers=NASDAQ_HEADERS)
        except Exception as e:
            log(f"  ipos {m:%Y-%m}: {e}")
            continue
        ok += 1
        data = (j or {}).get("data") or {}
        rows = ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []
        for r in rows:
            d = mdy(r.get("expectedPriceDate"))
            sym = (r.get("proposedTickerSymbol") or "").strip().upper()
            if not d or not sym or (sym, d) in seen:
                continue
            seen.add((sym, d))
            out.append({
                "type": "ipo",
                "date": d,
                "symbol": sym,
                "name": (r.get("companyName") or "").strip(),
                "exchange": (r.get("proposedExchange") or "").strip(),
                "price_range": (r.get("proposedSharePrice") or "").strip(),
                "shares": (r.get("sharesOffered") or "").strip(),
                "deal_size": money(r.get("dollarValueOfSharesOffered")),
            })
        time.sleep(1)
    if ok == 0:
        raise RuntimeError("Nasdaq IPO endpoint unreachable")
    return out


# ---------------------------------------------------------------- main
SECTIONS = [
    ("economic", "econ", fetch_economic, "Forex Factory"),
    ("earnings", "earnings", fetch_earnings, "Nasdaq"),
    ("splits", "split", fetch_splits, "Nasdaq"),
    ("ipos", "ipo", fetch_ipos, "Nasdaq"),
]


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sources": {}, "events": []}


def main():
    prev = load_previous()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cutoff = today_et().isoformat()
    events, sources, ok_count = [], {}, 0

    for key, etype, fn, default_src in SECTIONS:
        log(f"Fetching {key}...")
        try:
            result = fn()
            rows, src = result if isinstance(result, tuple) else (result, default_src)
            rows = [e for e in rows if e["date"] >= cutoff]
            events.extend(rows)
            sources[key] = {"ok": True, "updated_at": now, "source": src,
                            "count": len(rows), "error": None}
            ok_count += 1
            log(f"  {len(rows)} {key} events")
        except Exception as e:
            kept = [x for x in prev.get("events", [])
                    if x.get("type") == etype and x.get("date", "") >= cutoff]
            events.extend(kept)
            old = prev.get("sources", {}).get(key, {})
            sources[key] = {"ok": False, "updated_at": old.get("updated_at"),
                            "source": old.get("source", default_src),
                            "count": len(kept), "error": str(e)[:300]}
            log(f"  FAILED: {e} (kept {len(kept)} previous)")

    events.sort(key=lambda e: (e["date"], e.get("ts") or "", e["type"],
                               -(e.get("market_cap") or 0), e.get("symbol") or ""))
    data = {"generated_at": now, "sources": sources, "events": events}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    log(f"Wrote {len(events)} events to {OUT}")
    if ok_count == 0:
        sys.exit("Every source failed — check the log above.")


if __name__ == "__main__":
    main()
