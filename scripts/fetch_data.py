#!/usr/bin/env python3
"""Fetch upcoming economic releases, earnings, and stock splits into site/data/calendar.json.

Standard library only — no pip installs. Run locally with:  python scripts/fetch_data.py

Sources (no API key needed):
  - Economic calendar: Forex Factory weekly JSON feed (this week + next week)
  - Earnings + call timing (before open / after close): Nasdaq public calendar API
  - Stock splits: Nasdaq public calendar API
Optional fallback for earnings: Finnhub (set FINNHUB_API_KEY as a repo secret).

If a source fails, the previous data for that section is kept and marked stale,
so one flaky endpoint never blanks the dashboard.
"""
import json
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
def fetch_economic():
    events, got_first = [], False
    for i, url in enumerate(FF_URLS):
        try:
            rows = get_json(url)
        except urllib.error.HTTPError as e:
            if i > 0 and e.code == 404:  # next week's file isn't published yet
                log("  next-week economic feed not published yet")
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
                "type": "econ",
                "ts": dt.isoformat(),
                "date": dt.date().isoformat(),
                "title": (r.get("title") or "").strip(),
                "country": (r.get("country") or "").strip(),
                "impact": (r.get("impact") or "").strip(),
                "forecast": (r.get("forecast") or "").strip(),
                "previous": (r.get("previous") or "").strip(),
            })
        time.sleep(3)  # the feed asks for polite polling
    if not got_first:
        raise RuntimeError("economic feed returned nothing")
    seen, out = set(), []
    for e in events:
        k = (e["ts"], e["country"], e["title"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


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


# ---------------------------------------------------------------- main
SECTIONS = [
    ("economic", "econ", fetch_economic, "Forex Factory"),
    ("earnings", "earnings", fetch_earnings, "Nasdaq"),
    ("splits", "split", fetch_splits, "Nasdaq"),
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
