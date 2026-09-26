# Market calendar

A self-updating dashboard of upcoming economic releases, earnings (with before-open / after-close call timing), and stock splits.

- **Updates itself:** a GitHub Action runs every 3 hours on weekdays (plus Sunday afternoon), pulls fresh data, commits `site/data/calendar.json`, and redeploys GitHub Pages.
- **No API keys, no servers, no trackers.** The page makes zero third-party requests — it only loads its own JSON. Your watchlist lives in your browser, never in the repo.
- **Fails safe:** if a source is down, the last good data for that section is kept and flagged as stale in the header.

## Sources
| Section | Source | Key needed |
|---|---|---|
| Economic calendar | Forex Factory weekly feed (this week + next week) | No |
| Earnings + call timing | Nasdaq public calendar (next 21 days) | No |
| Stock splits | Nasdaq public calendar | No |
| Earnings fallback | Finnhub, used only if Nasdaq fails | Optional `FINNHUB_API_KEY` secret |

Exact conference-call times aren't published by any free source; the BMO/AMC tag is the practical proxy (calls usually start within an hour of the release).

## Layout
```
site/index.html              dashboard (single file)
site/data/calendar.json      data, rewritten by the Action
scripts/fetch_data.py        fetcher, Python standard library only
.github/workflows/update.yml schedule + deploy
```

## Tweaks
- Schedule: edit the `cron` lines in `.github/workflows/update.yml` (times are UTC).
- Earnings look-ahead: set `EARNINGS_DAYS` in the workflow's `env` (default 21).
- Run locally: `python scripts/fetch_data.py`, then `cd site && python -m http.server` and open http://localhost:8000.
