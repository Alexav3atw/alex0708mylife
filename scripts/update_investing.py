#!/usr/bin/env python3
"""Investing-portfolio updater for alex0708mylife.

Fetches the latest price and up to three recent headlines for each stock in
the watchlist and rewrites investing/index.html from the template. Runs in
GitHub Actions twice a day (see .github/workflows/investing.yml) and can also
be run locally:  python scripts/update_investing.py

Design rules, mirroring the space-news updater:
- Prices come from Yahoo Finance's chart API (free, no key); headlines come
  from Google News RSS and link back to their sources - nothing is rewritten.
- A per-stock cache (scripts/investing_cache.json) keeps the last good values:
  if one stock fails to fetch, its card shows the cached data with a "data of"
  note instead of disappearing. If every stock fails, the existing page is
  left untouched.
- The list order is the page order: alphabetical by company name.
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "investing_template.html"
OUTPUT = ROOT / "investing" / "index.html"
CACHE = ROOT / "scripts" / "investing_cache.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MAX_NEWS = 3
MAX_SCAN = 30  # how many feed items to look at while hunting for 3 good ones
SPARK_POINTS = 22
SLEEP = 0.3  # politeness delay between requests

# Auto-generated pages (Yahoo/TradingView "seasonal returns", bond profiles,
# "Estimates - EXCHANGE:SYMBOL") get indexed as news; drop those titles.
NEWS_JUNK = (
    "seasonal returns",
    "bond profile",
    "coupon and redemption",
    "number of employees",
    "employee count",
    "headcount",
    "stock price and chart",
    "stock price, news, quote",
    "analyst insights",
    "target price consensus",
    "gf score",
    ") cash flow",
    ") balance sheet",
    "| history",
    "stock forum",
    "still be a bargain",
    "- quote ",
    "estimates –",
    "– euronext:", "– lse:", "– nasdaq:", "– nyse:",
    "– xetra:", "– tlv:", "– hkex:", "– six:", "– tsx:",
    "– asx:", "– nzx:", "– krx:", "– jpx:", "– sse:",
    "– szse:", "– moex:", "– otc:", "– par:", "– bru:",
    "– ams:", "– mil:", "– mad:", "– lis:", "– vie:",
    "– sto:", "– hel:", "– osl:", "– cph:", "– icx:",
    "– wse:", "– bme:", "– bit:",
)


def junk_title(title):
    """True when a headline is an auto-generated page, not real news."""
    low = title.lower().replace("—", "–")  # normalise em-dash to en-dash
    return any(b in low for b in NEWS_JUNK)


def title_matches(title, query):
    """The headline must itself name the query. Google News fuzzy-matches
    loosely, so without this check a query like 'Ascencio shopping centres'
    can return any article about a shopping centre, and 'Sofina' can return
    news about the cosmetics brand of the same name. The anchor is the
    query's first word (or the full phrase if it appears verbatim)."""
    low = title.lower()
    if query.lower() in low:
        return True
    words = [w for w in re.split(r"\W+", query) if len(w) >= 2]
    if not words:
        return True
    return words[0] in low

# (Yahoo symbol, display name, one-line description, [Google News queries in
# English, tried in order]). List order = page order = alphabetical by name.
STOCKS = [
    ("AKER.OL", "Aker ASA", "Norwegian industrial investment group with holdings in energy, offshore services and marine industries.", ["Aker ASA", "Aker"]),
    ("ASCE.BR", "Ascencio SA", "Belgian real-estate investment trust (SIR) focused on out-of-town retail: retail parks and supermarkets in Belgium and France.", ["+Ascencio shopping centres"]),
    ("BPOST.BR", "bpost NV/SA", "Belgian postal operator: mail, parcels and e-commerce logistics in Belgium and abroad.", ["bpost", "bpost Belgium"]),
    ("BRK-B", "Berkshire Hathaway Inc.", "Warren Buffett's US holding company: insurance, railroads, energy and a large portfolio of listed companies.", ["Berkshire Hathaway", "Warren Buffett"]),
    ("BY6.VI", "BYD Company Limited", "Chinese maker of electric vehicles and batteries, among the world's largest EV producers.", ["BYD", "BYD Company"]),
    ("AI", "C3.ai", "US enterprise software company offering AI applications for industry and government.", ["C3.ai", "C3 AI"]),
    ("CPINV.BR", "Care Property Invest NV", "Belgian healthcare real-estate investor: care homes and housing with care in Belgium, the Netherlands, Spain and Ireland.", ["Care Property Invest"]),
    ("GTT.PA", "Gaztransport & Technigaz SA", "French engineering company: cryogenic membrane containment systems for LNG carriers and tanks.", ["Gaztransport Technigaz", "+GTT LNG"]),
    ("GBLB.BR", "Groupe Bruxelles Lambert SA", "Belgian investment holding company with stakes in listed companies such as SGS, adidas and Pernod Ricard.", ["Groupe Bruxelles Lambert", "GBL"]),
    ("INRG.MI", "iShares Global Clean Energy Transition UCITS ETF", "Exchange-traded fund tracking a global index of companies active in the clean-energy transition.", ["iShares Global Clean Energy"]),
    ("KBCA.BR", "KBC Ancora SA", "Belgian holding company whose core asset is its stake in KBC Group, the bank-insurer.", ["KBC Ancora", "KBC"]),
    ("NN.AS", "NN Group N.V.", "Dutch insurance and asset-management group, best known for the Nationale-Nederlanden brand.", ["NN Group"]),
    ("PNL.AS", "PostNL N.V.", "Dutch mail and parcel delivery company.", ["PostNL"]),
    ("SHELL.AS", "Shell plc", "British oil and gas major active across the whole energy chain, investing in the energy transition.", ["Shell plc", "Shell"]),
    ("SOF.BR", "Sofina S.A.", "Belgian family holding company (Boël) investing in private and listed growth companies across Europe, North America and Asia.", ["+Sofina invests", "+Sofina-backed", "Sofina Société Anonyme"]),
    ("WEB.BR", "Warehouses Estates Belgium S.C.A.", "Belgian property investment company (SIR) owning mainly out-of-town retail plus semi-industrial property, mostly in Wallonia.", ["Warehouses Estates Belgium", "+Warehouses Estates"]),
    ("3CP.DU", "Xiaomi Corp", "Chinese consumer-electronics company: smartphones, smart devices and a connected ecosystem.", ["Xiaomi"]),
    ("XIOR.BR", "Xior Student Housing NV", "Belgian real-estate investment trust specialising in student housing across Europe.", ["Xior", "Xior Student Housing"]),
]


def http_get(url):
    """Fetch a URL, return bytes or None on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
        print(f"  http error {url[:70]}: {exc}")
        return None


def yahoo_history(symbol):
    """Fetch ~3 months of daily closes plus the latest quote for one symbol.

    Returns {"price", "currency", "prev", "closes"} with closes as a list of
    (timestamp, close) pairs, or None if the fetch failed or is empty.
    """
    url_tpl = "https://{host}/v8/finance/chart/{sym}?range=3mo&interval=1d"
    raw = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        raw = http_get(url_tpl.format(host=host, sym=urllib.parse.quote(symbol)))
        if raw is not None:
            break
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            return None
        res = results[0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        quotes = (res.get("indicators") or {}).get("quote") or [{}]
        closes = [(t, c) for t, c in zip(ts, quotes[0].get("close") or []) if c is not None]
        if not closes:
            return None
        price = meta.get("regularMarketPrice") or closes[-1][1]
        # Yahoo's chartPreviousClose is stale for some tickers (it can sit at a
        # weeks-old value and fake a huge "daily" move). The series' own
        # second-to-last close is consistent with the rest of the data.
        prev = closes[-2][1] if len(closes) > 1 else meta.get("chartPreviousClose")
        if prev is None:
            prev = closes[-1][1]
        return {"price": price, "currency": meta.get("currency", ""),
                "prev": prev, "closes": closes}
    except Exception as exc:  # noqa: BLE001
        print(f"  parse error for {symbol}: {exc}")
        return None


def google_news(queries):
    """Fetch up to MAX_NEWS headlines (title, link, short date) from Google
    News RSS, trying each query in order until one yields results."""
    for query in queries:
        # A leading "+" marks an AND-query to send unquoted; anything else
        # with spaces is sent as an exact phrase so Google News does not
        # fuzzy-match wrong companies (e.g. "Sofina" also being a cosmetics
        # brand and a D&D character). when:30d = last 30 days only.
        unquoted = query.startswith("+")
        q0 = query[1:] if unquoted else query
        q = q0 if unquoted or " " not in q0 else f'"{q0}"'
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + "&hl=en-US&gl=US&ceid=US:en&when:30d")
        raw = http_get(url)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
            items = []
            seen = set()
            scanned = 0
            for item in root.findall(".//item"):
                scanned += 1
                if scanned > MAX_SCAN:
                    break
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                # Google News appends " - Source" to every title; drop it.
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]
                if (not title or not link or title in seen
                        or junk_title(title) or not title_matches(title, q0)):
                    continue
                seen.add(title)
                date = ""
                pub = item.findtext("pubDate") or ""
                try:
                    date = time.strftime("%d %b", time.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z"))
                except ValueError:
                    pass
                items.append({"t": title, "l": link, "d": date})
                if len(items) >= MAX_NEWS:
                    break
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            print(f"  news parse error for '{query}': {exc}")
    return []


def load_cache():
    """Return the per-stock cache dict, or {} if missing or unreadable."""
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - first run has no cache
        return {}


def save_cache(data):
    """Persist only fresh (non-stale) entries so stale values never accumulate."""
    fresh = {sym: entry for sym, entry in data.items() if not entry.get("stale")}
    if fresh:
        CACHE.write_text(json.dumps(fresh, ensure_ascii=False, indent=1), encoding="utf-8")


def fmt_num(n):
    """Belgian number format: dot thousands, comma decimals, 2 decimals."""
    return f"{n:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_pct(p):
    """Signed percentage in Belgian format, e.g. '+0,84 %'."""
    sign = "+" if p > 0.0005 else ("-" if p < -0.0005 else "")
    return f"{sign}{fmt_num(abs(p))} %"


def direction(p):
    """CSS class + arrow for a percentage: up / down / flat."""
    if p > 0.0005:
        return "up", "&#9650;"    # black up-pointing triangle
    if p < -0.0005:
        return "down", "&#9660;"  # black down-pointing triangle
    return "flat", "&#8226;"      # bullet


def sparkline(closes, change):
    """Tiny one-month line from the last SPARK_POINTS closes (flat floats, as
    stored in the cache), coloured by direction, with the numbers in the SVG
    title (native hover tooltip)."""
    vals = list(closes[-SPARK_POINTS:])
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    w, h = 220.0, 36.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = w * i / (n - 1)
        y = h - (v - lo) / (hi - lo) * h
        pts.append(f"{x:.1f},{y:.1f}")
    cls, _ = direction(change)
    title = f"1 month: {fmt_num(vals[0])} to {fmt_num(vals[-1])} ({fmt_pct(change)})"
    return (
        f'<svg class="sc-spark {cls}" viewBox="0 0 220 36" preserveAspectRatio="none" '
        f'role="img" aria-label="One-month price trend">'
        f"<title>{html.escape(title)}</title>"
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="currentColor" stroke-width="2" '
        f'vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def build_card(sym, entry):
    """Render one stock card from a fresh or cached entry."""
    name, tick, desc = sym, sym, ""
    for s, n, d, _ in STOCKS:
        if s == sym:
            name, tick, desc = n, s, d
            break
    price = entry["price"]
    ccy = entry.get("currency") or ""
    stale = entry.get("stale")
    stale_html = f' <span class="sc-stale">data of {entry.get("when", "")}</span>' if stale else ""

    day_cls, day_arrow = direction(entry["day"])
    wk_cls, wk_arrow = direction(entry["wk"])
    mo_cls, mo_arrow = direction(entry["mo"])

    eur_html = ""
    if ccy == "USD" and entry.get("usdeur"):
        eur_html = f'<span class="sc-eur">&asymp; &euro;{fmt_num(price / entry["usdeur"])}</span>'
    elif ccy == "NOK" and entry.get("nokeur"):
        eur_html = f'<span class="sc-eur">&asymp; &euro;{fmt_num(price / entry["nokeur"])}</span>'

    quote = " ".join(
        [f'<span class="sc-price">{fmt_num(price)} <span class="sc-ccy">{ccy}</span></span>',
         f'<span class="sc-day {day_cls}">{day_arrow} {fmt_pct(entry["day"])}</span>']
        + ([eur_html] if eur_html else [])
    )

    spark = sparkline(entry.get("closes", []), entry["mo"])

    if entry.get("news"):
        lis = []
        for item in entry["news"]:
            date = f'<span class="n-date">{item["d"]}</span> ' if item.get("d") else ""
            lis.append(
                f'      <li>{date}<a href="{item["l"]}" target="_blank" rel="noopener">'
                f'{html.escape(item["t"])}</a></li>'
            )
        news_html = '<ul class="sc-news">\n' + "\n".join(lis) + "\n    </ul>"
    else:
        news_html = '<div class="sc-none">No recent headlines.</div>'

    return f"""    <div class="stock-card">
      <h2 class="sc-name">{html.escape(name)} <span class="sc-tick">{tick}</span>{stale_html}</h2>
      <p class="sc-desc">{desc}</p>
      <div class="sc-quote">
        {quote}
      </div>
      <div class="sc-trends">
        <span class="tb {wk_cls}">1W {wk_arrow} {fmt_pct(entry["wk"])}</span>
        <span class="tb {mo_cls}">1M {mo_arrow} {fmt_pct(entry["mo"])}</span>
      </div>
      {spark}
      {news_html}
    </div>"""


def main():
    print("Fetching portfolio data...")
    cache = load_cache()
    fx = {}
    for pair, key in (("EURUSD=X", "usdeur"), ("EURNOK=X", "nokeur")):
        hist = yahoo_history(pair)
        if hist:
            fx[key] = hist["price"]
    print(f"  FX rates: USDEUR={fx.get('usdeur')}, NOKEUR={fx.get('nokeur')}")

    when = time.strftime("%d %b %Y, %H:%M UTC", time.gmtime())
    data = {}
    for sym, name, desc, queries in STOCKS:
        time.sleep(SLEEP)
        print(f"  {sym} ({name}) ...")
        entry = None
        try:
            hist = yahoo_history(sym)
            if hist is None:
                raise RuntimeError("no history from Yahoo")
            closes = hist["closes"]
            n = len(closes)
            last_close = closes[-1][1]
            day = (hist["price"] / hist["prev"] - 1.0) * 100.0 if hist["prev"] else 0.0
            wk = (last_close / closes[-6][1] - 1.0) * 100.0 if n >= 6 else \
                 (last_close / closes[0][1] - 1.0) * 100.0 if n >= 2 else 0.0
            mo = (last_close / closes[-22][1] - 1.0) * 100.0 if n >= 22 else wk
            news = google_news(queries)
            entry = {
                "price": hist["price"], "currency": hist["currency"],
                "day": day, "wk": wk, "mo": mo,
                "closes": [c for _, c in closes],
                "news": news, "when": when, "stale": False,
            }
        except Exception as exc:  # noqa: BLE001
            print(f"    failed ({exc}) - trying cache")
            entry = cache.get(sym)
            if entry:
                entry = dict(entry, stale=True)
        if entry:
            for key, val in fx.items():
                entry.setdefault(key, val)
            data[sym] = entry
            state = "stale cache" if entry.get("stale") else "fresh"
            print(f"    -> {state}, price {entry['price']} {entry['currency']}")

    if not data:
        print("No data for any stock - leaving the existing page untouched.")
        sys.exit(0)

    save_cache(data)

    # Page rows in the fixed alphabetical order.
    rows = [build_card(sym, data[sym]) for sym, _, _, _ in STOCKS if sym in data]

    # Summary tiles: best and worst mover of the day among fresh entries.
    movers = [d for d in data.values() if not d.get("stale")]
    best = max(movers, key=lambda d: d["day"]) if movers else None
    worst = min(movers, key=lambda d: d["day"]) if movers else None
    best_name = worst_name = ""
    for s, n, _, _ in STOCKS:
        if best and s in data and data[s] is best:
            best_name = n
        if worst and s in data and data[s] is worst:
            worst_name = n
    tiles = []
    if best is not None:
        cls, arrow = direction(best["day"])
        tiles.append(
            f'    <div class="s-tile"><div class="label">Best today</div>'
            f'<div class="value {cls}">{arrow} {fmt_pct(best["day"])}</div>'
            f'<div class="note">{html.escape(best_name)}</div></div>'
        )
    if worst is not None:
        cls, arrow = direction(worst["day"])
        tiles.append(
            f'    <div class="s-tile"><div class="label">Worst today</div>'
            f'<div class="value {cls}">{arrow} {fmt_pct(worst["day"])}</div>'
            f'<div class="note">{html.escape(worst_name)}</div></div>'
        )
    summary = "\n".join(tiles)

    stamp = time.strftime("%d %B %Y, %H:%M UTC", time.gmtime())
    template = TEMPLATE.read_text(encoding="utf-8")
    page = (template.replace("{{SUMMARY}}", summary)
                    .replace("{{ROWS}}", "\n".join(rows))
                    .replace("{{STAMP}}", stamp))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(rows)} stocks.")


if __name__ == "__main__":
    main()
