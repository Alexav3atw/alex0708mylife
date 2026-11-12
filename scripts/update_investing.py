#!/usr/bin/env python3
"""Investing-portfolio updater for alex0708mylife.

Fetches the latest price and up to three recent headlines for each stock in
the watchlist and rewrites investing/index.html from the template. Dutch
headlines come from De Tijd and De Aandeelhouder (Google News site-searches),
English ones from Google News. Runs in GitHub Actions six times a day, from
08:00 to 22:00 CET (see .github/workflows/investing.yml), and can also be
run locally:  python scripts/update_investing.py

Design rules, mirroring the space-news updater:
- Prices come from Yahoo Finance's chart API (free, no key); headlines come
  from Google News RSS and link back to their sources - nothing is rewritten.
- A per-stock cache (scripts/investing_cache.json) keeps the last good values:
  if one stock fails to fetch, its card shows the cached data with a "data of"
  note instead of disappearing. If every stock fails, the existing page is
  left untouched.
- The list order is the page order: alphabetical by company name.
- Each card shows the 200-day moving average (from the 1-year price history),
  and the page carries a Buffett-indicator section: Wilshire 5000 index /
  nominal US GDP, from Yahoo and FRED respectively (both keyless).
"""
import calendar
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
MAX_NL = 2  # at most this many Dutch headlines per stock; English fill the rest
NEWS_MAX_AGE_DAYS = 30  # drop older items; Google's when:30d filter is unreliable
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
    "grafieken en nieuws",  # De Tijd stock page, e.g. "Aker - Koersen, grafieken en nieuws | ISIN"
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
    return words[0].lower() in low

# (Yahoo symbol, display name, one-line description, [Google News queries in
# English, tried in order], [Dutch source queries: site:-searches on De Tijd
# and De Aandeelhouder, tried before the English ones]). List order = page
# order = alphabetical by name.
STOCKS = [
    ("AKER.OL", "Aker ASA", "Norwegian industrial investment group with holdings in energy, offshore services and marine industries.", ["Aker ASA", "Aker"], ["+Aker site:tijd.be", "+Aker site:deaandeelhouder.nl"]),
    ("ASCE.BR", "Ascencio SA", "Belgian real-estate investment trust (SIR) focused on out-of-town retail: retail parks and supermarkets in Belgium and France.", ["+Ascencio shopping centres"], ["+Ascencio site:tijd.be", "+Ascencio site:deaandeelhouder.nl"]),
    ("BPOST.BR", "bpost NV/SA", "Belgian postal operator: mail, parcels and e-commerce logistics in Belgium and abroad.", ["bpost", "bpost Belgium"], ["+bpost site:tijd.be", "+bpost site:deaandeelhouder.nl"]),
    ("BRK-B", "Berkshire Hathaway Inc.", "Warren Buffett's US holding company: insurance, railroads, energy and a large portfolio of listed companies.", ["Berkshire Hathaway", "Warren Buffett"], ["+Berkshire Hathaway site:tijd.be", "+Berkshire Hathaway site:deaandeelhouder.nl"]),
    ("BY6.VI", "BYD Company Limited", "Chinese maker of electric vehicles and batteries, among the world's largest EV producers.", ["BYD", "BYD Company"], ["+BYD site:tijd.be", "+BYD site:deaandeelhouder.nl"]),
    ("AI", "C3.ai", "US enterprise software company offering AI applications for industry and government.", ["C3.ai", "C3 AI"], ["+C3.ai site:tijd.be", "+C3.ai site:deaandeelhouder.nl"]),
    ("CPINV.BR", "Care Property Invest NV", "Belgian healthcare real-estate investor: care homes and housing with care in Belgium, the Netherlands, Spain and Ireland.", ["Care Property Invest"], ["+Care Property Invest site:tijd.be", "+Care Property Invest site:deaandeelhouder.nl"]),
    ("GTT.PA", "Gaztransport & Technigaz SA", "French engineering company: cryogenic membrane containment systems for LNG carriers and tanks.", ["Gaztransport Technigaz", "+GTT LNG"], ["+GTT site:tijd.be", "+GTT site:deaandeelhouder.nl"]),
    ("GBLB.BR", "Groupe Bruxelles Lambert SA", "Belgian investment holding company with stakes in listed companies such as SGS, adidas and Pernod Ricard.", ["Groupe Bruxelles Lambert", "GBL"], ["+GBL site:tijd.be", "+GBL site:deaandeelhouder.nl"]),
    ("INRG.MI", "iShares Global Clean Energy Transition UCITS ETF", "Exchange-traded fund tracking a global index of companies active in the clean-energy transition.", ["iShares Global Clean Energy"], ["+iShares Global Clean Energy site:tijd.be", "+iShares Global Clean Energy site:deaandeelhouder.nl"]),
    ("KBCA.BR", "KBC Ancora SA", "Belgian holding company whose core asset is its stake in KBC Group, the bank-insurer.", ["KBC Ancora", "KBC"], ["+KBC Ancora site:tijd.be", "+KBC Ancora site:deaandeelhouder.nl"]),
    ("NN.AS", "NN Group N.V.", "Dutch insurance and asset-management group, best known for the Nationale-Nederlanden brand.", ["NN Group"], ["+NN Group site:tijd.be", "+NN Group site:deaandeelhouder.nl"]),
    ("PNL.AS", "PostNL N.V.", "Dutch mail and parcel delivery company.", ["PostNL"], ["+PostNL site:tijd.be", "+PostNL site:deaandeelhouder.nl"]),
    ("SHELL.AS", "Shell plc", "British oil and gas major active across the whole energy chain, investing in the energy transition.", ["Shell plc", "Shell"], ["+Shell site:tijd.be", "+Shell site:deaandeelhouder.nl"]),
    ("SOF.BR", "Sofina S.A.", "Belgian family holding company (Boël) investing in private and listed growth companies across Europe, North America and Asia.", ["+Sofina invests", "+Sofina-backed", "Sofina Société Anonyme"], ["+Sofina site:tijd.be", "+Sofina site:deaandeelhouder.nl"]),
    ("WEB.BR", "Warehouses Estates Belgium S.C.A.", "Belgian property investment company (SIR) owning mainly out-of-town retail plus semi-industrial property, mostly in Wallonia.", ["Warehouses Estates Belgium", "+Warehouses Estates"], ["+Warehouses Estates site:tijd.be", "+Warehouses Estates site:deaandeelhouder.nl"]),
    ("3CP.DU", "Xiaomi Corp", "Chinese consumer-electronics company: smartphones, smart devices and a connected ecosystem.", ["Xiaomi"], ["+Xiaomi site:tijd.be", "+Xiaomi site:deaandeelhouder.nl"]),
    ("XIOR.BR", "Xior Student Housing NV", "Belgian real-estate investment trust specialising in student housing across Europe.", ["Xior", "Xior Student Housing"], ["+Xior site:tijd.be", "+Xior site:deaandeelhouder.nl"]),
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


def yahoo_history(symbol, span="3mo"):
    """Fetch daily closes plus the latest quote for one symbol.

    span is the Yahoo chart range ("3mo", "1y", "5y", "max"). Returns
    {"price", "currency", "prev", "closes"} with closes as a list of
    (timestamp, close) pairs, or None if the fetch failed or is empty.
    """
    url_tpl = "https://{host}/v8/finance/chart/{sym}?range={span}&interval=1d"
    raw = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        raw = http_get(url_tpl.format(host=host, sym=urllib.parse.quote(symbol), span=span))
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


# Dutch financial press searched per stock: Google News site:-searches for
# De Tijd (Belgian financial daily) and De Aandeelhouder (Dutch investor
# site). These queries come first in each stock's query list.
NL_DOMAINS = ("site:tijd.be", "site:deaandeelhouder.nl")


def google_news(queries):
    """Collect up to MAX_NEWS headlines, trying each query in order.
    Dutch site:-queries contribute at most MAX_NL headlines; the English
    queries fill the remaining slots up to MAX_NEWS."""
    items = []
    seen = set()
    for query in queries:
        if len(items) >= MAX_NEWS:
            break
        nl = any(d in query for d in NL_DOMAINS)
        if nl and sum(1 for i in items if i.get("nl")) >= MAX_NL:
            continue
        # A leading "+" marks an AND-query to send unquoted; anything else
        # with spaces is sent as an exact phrase so Google News does not
        # fuzzy-match wrong companies (e.g. "Sofina" also being a cosmetics
        # brand and a D&D character). when:30d = last 30 days only.
        unquoted = query.startswith("+")
        q0 = query[1:] if unquoted else query
        q = q0 if unquoted or " " not in q0 else f'"{q0}"'
        region = "&hl=nl-BE&gl=BE&ceid=BE:nl" if nl else "&hl=en-US&gl=US&ceid=US:en"
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
               + region + "&when:30d")
        raw = http_get(url)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
            scanned = 0
            for item in root.findall(".//item"):
                scanned += 1
                if scanned > MAX_SCAN:
                    break
                title = item.findtext("title") or ""
                link = item.findtext("link") or ""
                # Google News appends " - Source" to every title; drop it (the
                # separator can be a plain, en or em dash). Some De Tijd pages
                # already end in their own outlet name ("Sofina - De Tijd - De
                # Tijd"), so keep stripping while the tail matches <source>.
                source_name = (item.findtext("source") or "").strip().lower()
                while True:
                    stripped = False
                    for sep in (" - ", " – ", " — "):
                        if sep in title:
                            tail = title.rsplit(sep, 1)[1].strip().rstrip(".,;:!?").lower()
                            if tail == source_name or not source_name:
                                title = title.rsplit(sep, 1)[0]
                                stripped = True
                                break
                    if not stripped:
                        break
                # Company dossier pages surface as "<Name> - De Tijd - De
                # Tijd" and reduce to the bare name; a real headline always
                # has words beyond the company name.
                clean = re.sub(r"site:\S+", "", q0)
                name_words = {w.lower() for w in re.split(r"\W+", clean) if w}
                if name_words and all(w in name_words for w in title.lower().split()):
                    continue
                if (not title or not link or title in seen
                        or junk_title(title) or not title_matches(title, q0)):
                    continue
                date = ""
                pub = item.findtext("pubDate") or ""
                try:
                    ts = time.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                    if time.time() - calendar.timegm(ts) > NEWS_MAX_AGE_DAYS * 86400:
                        continue
                    date = time.strftime("%d %b", ts)
                except ValueError:
                    pass
                if nl and sum(1 for i in items if i.get("nl")) >= MAX_NL:
                    break
                seen.add(title)
                src = (item.findtext("source") or "") if nl else ""
                items.append({"t": title, "l": link, "d": date, "nl": nl, "s": src})
                if len(items) >= MAX_NEWS:
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"  news parse error for '{query}': {exc}")
    return items


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


# ---------------------------------------------------------------------------
# Buffett indicator - the total value of the US stock market as a share of
# US GDP, the gauge Warren Buffett called "the best single measure of where
# valuations stand". Two keyless sources make it computable:
#   - the Wilshire 5000 Full Cap index (Yahoo ^W5000): by construction a
#     close proxy for the total dollar value of all US-listed companies in
#     billions (the 1970 Wilshire index was exactly that figure);
#   - nominal US GDP, quarterly, billions, from FRED (St. Louis Fed CSV).
# Dividing index by GDP and multiplying by 100 gives the indicator in
# percent: above 100 % the market is worth more than one year of output.
# ---------------------------------------------------------------------------

W5000 = "^W5000"  # Wilshire 5000 Full Cap index on Yahoo
GDP_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDP"
DAILY_SPAN_DAYS = 252  # ~12 months of sessions for the daily chart
MONTHLY_SPAN = 60      # last 60 month-ends for the monthly chart
SIX_SPAN = 40          # last 40 half-years = 20 years for the six-monthly chart


def fred_gdp():
    """Quarterly nominal US GDP from FRED as [(ymd, billions)], or None.

    FRED's edge resets requests that carry a browser User-Agent (bot
    protection); a bare request with no UA header goes through."""
    raw = None
    for _ in range(3):
        req = urllib.request.Request(GDP_URL)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  GDP fetch error: {exc}")
            time.sleep(2)
    if raw is None:
        return None
    try:
        rows = []
        for line in raw.decode("utf-8", "replace").splitlines()[1:]:
            date, _, value = line.partition(",")
            value = value.strip()
            if value in ("", "."):
                continue
            rows.append((date, float(value)))
        return rows or None
    except Exception as exc:  # noqa: BLE001
        print(f"  GDP parse error: {exc}")
        return None


def w5000_series():
    """Wilshire 5000 closes: the daily 5-year series (for the daily and
    monthly charts) and the longest series Yahoo serves (for the six-monthly
    chart - on this index range=max comes back as ~quarterly points)."""
    daily = None
    hist = yahoo_history(W5000, "5y")
    if hist and len(hist["closes"]) > 250:
        daily = hist["closes"]
    longest = None
    hist = yahoo_history(W5000, "max")
    if hist and len(hist["closes"]) > 40:
        longest = hist["closes"]
    return daily, longest


def buffett_series(w5000, gdp):
    """Merge the daily index closes with stepped quarterly GDP into
    [(ymd, pct)] points: each day uses the latest GDP published so far."""
    pts, gi = [], 0
    for ts, close in w5000:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        while gi + 1 < len(gdp) and gdp[gi + 1][0] <= day:
            gi += 1
        g = gdp[gi][1]
        if g > 0:
            pts.append((day, close / g * 100.0))
    return pts


def group_last(points, keyfn):
    """Keep the last point of each consecutive group, in order."""
    out, cur = [], None
    for p in points:
        k = keyfn(p[0])
        if k != cur:
            out.append(p)
            cur = k
        else:
            out[-1] = p
    return out


def fetch_buffett(cache):
    """Compute the Buffett-indicator payload, or fall back to the cache.

    Returns (buff, stale); buff holds the three point series plus 20-year
    stats, stale says whether the numbers are yesterday's cached ones."""
    try:
        daily_w, long_w = w5000_series()
        gdp = fred_gdp()
        if not daily_w or not long_w or not gdp:
            raise RuntimeError("no W5000/GDP data")
        pts = buffett_series(daily_w, gdp)
        pts_long = buffett_series(long_w, gdp)
        if len(pts) < 250 or len(pts_long) < 40:
            raise RuntimeError("too few merged points")
        monthly = group_last(pts, lambda d: d[:7])[-MONTHLY_SPAN:]
        six = group_last(pts_long, lambda d: d[:4] + ("-H1" if d[5:7] <= "06" else "-H2"))[-SIX_SPAN:]
        six[-1] = pts[-1]  # freshest point for the current half-year
        stats = [v for _, v in six]
        buff = {
            "daily": pts[-DAILY_SPAN_DAYS:],
            "monthly": monthly,
            "six": six,
            "cur": pts[-1][1],
            "avg": sum(stats) / len(stats),
            "min": min(stats), "max": max(stats),
            "when": time.strftime("%d %b %Y, %H:%M UTC", time.gmtime()),
        }
        return buff, False
    except Exception as exc:  # noqa: BLE001
        print(f"  buffett indicator failed ({exc}) - trying cache")
        old = cache.get("~buffett")
        return (dict(old, stale=True), True) if old else (None, True)


def fmt_pct_axis(v):
    """Compact Belgian-format number for axis labels: '172,3'."""
    return f"{v:,.1f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def label_month(d):
    """'2025-11-04' -> 'Nov 2025'."""
    return time.strftime("%b %Y", time.strptime(d, "%Y-%m-%d"))


def svg_chart(points, w=300.0, h=170.0, ref=None, aria=""):
    """Dark-theme SVG line chart from (label, pct) points.

    One series, so no legend box: the heading above each chart names it.
    Returns markup with a polyline, min/mid/max value labels, first/middle/
    last date labels, a dashed reference line when `ref` falls inside the
    window, and native <title> tooltips on a sparse set of hover dots (the
    site ships no JavaScript, so hover help rides on browser tooltips)."""
    if len(points) < 2:
        return ""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = (hi - lo) * 0.10
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 38.0, 8.0, 10.0, 22.0
    iw, ih = w - left - right, h - top - bottom
    n = len(points)

    def xy(i, v):
        return left + iw * i / (n - 1), top + ih * (1.0 - (v - lo) / (hi - lo))

    parts = []
    for frac in (0.0, 0.5, 1.0):
        v = hi - (hi - lo) * frac
        y = top + ih * frac
        parts.append(f'<line class="b-gridline" x1="{left:.1f}" y1="{y:.1f}" '
                     f'x2="{left + iw:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="b-chart-label" x="{left - 6:.1f}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{fmt_pct_axis(v)}</text>')

    xlabs = [0, n - 1]
    if n >= 40:
        xlabs.insert(1, n // 2)
    for i in xlabs:
        x, _ = xy(i, vals[i])
        anchor = "start" if i == 0 else ("middle" if i != n - 1 else "end")
        parts.append(f'<text class="b-chart-label" x="{x:.1f}" y="{top + ih + 17:.1f}" '
                     f'text-anchor="{anchor}">{html.escape(points[i][0])}</text>')

    line = " ".join(f"{x:.1f},{y:.1f}"
                    for x, y in (xy(i, v) for i, v in enumerate(vals)))

    if ref is not None and lo <= ref <= hi:
        _, y = xy(0, ref)
        parts.append(f'<line class="b-ref" x1="{left:.1f}" y1="{y:.1f}" '
                     f'x2="{left + iw:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="b-chart-label" x="{left + iw + 3:.1f}" y="{y + 4:.1f}">'
                     f'{fmt_pct_axis(ref)} %</text>')

    dots = []
    step = 1 if n <= 70 else 5
    for i in range(0, n, step):
        x, y = xy(i, vals[i])
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="transparent">'
                    f'<title>{html.escape(points[i][0])}: {fmt_num(vals[i])} %</title></circle>')
    lx, ly = xy(n - 1, vals[-1])
    dots.append(f'<circle class="b-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="4">'
                f'<title>{html.escape(points[-1][0])}: {fmt_num(vals[-1])} %</title></circle>')

    return (f'<svg viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
            f'aria-label="{html.escape(aria)}">'
            f'<title>{html.escape(aria)}</title>'
            + "".join(parts)
            + f'<polyline class="b-line" points="{line}"/>'
            + "".join(dots) + "</svg>")


def build_buffett(buff):
    """Render the Buffett-indicator section: summary tiles, three charts and
    a data table - or a short note when no data is available at all."""
    if not buff:
        return ('<div class="buff-note">Buffett-indicator data is temporarily '
                'unavailable (Wilshire 5000 or GDP source did not respond). '
                'The rest of the page is unaffected.</div>')
    stale_html = (' <span class="b-stale">data of {}</span>'.format(buff.get("when", ""))
                  if buff.get("stale") else "")
    daily_l = [(label_month(d), v) for d, v in buff["daily"]]
    monthly_l = [(label_month(d), v) for d, v in buff["monthly"]]
    six_l = [(f"{k[5:]} {k[:4]}", v) for k, v in buff["six"]]

    cur, avg = buff["cur"], buff["avg"]
    diff = cur - avg
    d_cls, d_arrow = direction(diff)
    tiles = "\n".join([
        '    <div class="b-tile"><div class="label">Now</div>'
        f'<div class="value">{fmt_num(cur)} %</div>'
        '<div class="note">total US stock market vs US GDP</div></div>',
        '    <div class="b-tile"><div class="label">vs 20-year average</div>'
        f'<div class="value {d_cls}">{d_arrow} {fmt_pct_axis(abs(diff))} pts</div>'
        f'<div class="note">20-year average {fmt_num(avg)} %</div></div>',
        '    <div class="b-tile"><div class="label">20-year range</div>'
        f'<div class="value">{fmt_num(buff["min"])} % &ndash; {fmt_num(buff["max"])} %</div>'
        '<div class="note">six-monthly points</div></div>',
    ])

    charts = "\n".join([
        '    <div class="b-card"><h3>Daily &middot; last 12 months</h3>'
        + svg_chart(daily_l, aria=f"Buffett indicator, daily, {daily_l[0][0]} to {daily_l[-1][0]}")
        + "</div>",
        '    <div class="b-card"><h3>Monthly &middot; last 5 years</h3>'
        + svg_chart(monthly_l, aria=f"Buffett indicator, monthly, {monthly_l[0][0]} to {monthly_l[-1][0]}")
        + "</div>",
        '    <div class="b-card"><h3>Six-monthly &middot; last 20 years</h3>'
        + svg_chart(six_l, ref=100.0, aria=f"Buffett indicator, six-monthly, {six_l[0][0]} to {six_l[-1][0]}")
        + "</div>",
    ])

    rows = "\n".join(f"<tr><td>{lbl}</td><td>{fmt_num(v)} %</td></tr>"
                     for lbl, v in six_l)
    table = ('<details class="b-table"><summary>Data table: six-monthly points</summary>'
             '<table><thead><tr><th>Half-year</th><th>Indicator</th></tr></thead>'
             f'<tbody>\n{rows}\n</tbody></table></details>')

    return f"""  <h2>Buffett indicator{stale_html}</h2>
    <p class="b-intro">The classic gauge of overall market valuation: the total value of all US-listed companies (the Wilshire 5000 Full Cap index, which tracks that value in billions) divided by the country's yearly output (nominal GDP, quarterly). Above 100 % the stock market is worth more than a full year of US economic output &mdash; a rough &ldquo;is the market expensive or cheap?&rdquo; thermometer. Data: Yahoo Finance and FRED (St. Louis Fed), both without API keys.</p>
    <div class="b-tiles">
{tiles}
    </div>
    <div class="b-grid">
{charts}
    </div>
    {table}"""


def build_card(sym, entry):
    """Render one stock card from a fresh or cached entry."""
    name, tick, desc = sym, sym, ""
    for s, n, d, _, _ in STOCKS:
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

    ma_html = ""
    if entry.get("ma200"):
        rel = (price / entry["ma200"] - 1.0) * 100.0
        ma_cls, ma_arrow = direction(rel)
        ma_html = (
            f'<div class="sc-ma"><span class="sc-ma-label">200-day avg</span> '
            f'<span class="sc-ma-value">{fmt_num(entry["ma200"])} <span class="sc-ccy">{ccy}</span></span> '
            f'<span class="sc-ma-pos {ma_cls}">{ma_arrow} {fmt_pct(rel)} vs avg</span></div>'
        )

    if entry.get("news"):
        lis = []
        for item in entry["news"]:
            date = f'<span class="n-date">{item["d"]}</span> ' if item.get("d") else ""
            src = f'<span class="n-src">{html.escape(item["s"])}</span> ' if item.get("s") else ""
            lis.append(
                f'      <li>{date}{src}<a href="{item["l"]}" target="_blank" rel="noopener">'
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
      {ma_html}
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
    for sym, name, desc, queries, nl_queries in STOCKS:
        time.sleep(SLEEP)
        print(f"  {sym} ({name}) ...")
        entry = None
        try:
            hist = yahoo_history(sym, "1y")
            if hist is None:
                raise RuntimeError("no history from Yahoo")
            closes = hist["closes"]
            n = len(closes)
            last_close = closes[-1][1]
            day = (hist["price"] / hist["prev"] - 1.0) * 100.0 if hist["prev"] else 0.0
            wk = (last_close / closes[-6][1] - 1.0) * 100.0 if n >= 6 else \
                 (last_close / closes[0][1] - 1.0) * 100.0 if n >= 2 else 0.0
            mo = (last_close / closes[-22][1] - 1.0) * 100.0 if n >= 22 else wk
            # 200-day moving average: the classic long-term trend line.
            # range=1y gives ~252 sessions, so it needs 200 days of history;
            # very young listings simply show no average line.
            ma200 = (sum(c for _, c in closes[-200:]) / 200.0
                     if n >= 200 else None)
            news = google_news(nl_queries + queries)
            entry = {
                "price": hist["price"], "currency": hist["currency"],
                "day": day, "wk": wk, "mo": mo, "ma200": ma200,
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

    # Buffett indicator: market-wide gauge, cached under "~buffett" exactly
    # like a stock, so one failed fetch shows yesterday's numbers instead
    # of an empty section.
    time.sleep(SLEEP)
    buff, buff_stale = fetch_buffett(cache)
    if buff:
        buff = dict(buff, stale=buff_stale)
        data["~buffett"] = buff
        print(f"  ~buffett indicator: {buff['cur']:.2f} %"
              + (" (stale cache)" if buff_stale else ""))

    save_cache(data)

    # Page rows in the fixed alphabetical order.
    rows = [build_card(sym, data[sym]) for sym, _, _, _, _ in STOCKS if sym in data]

    # Summary tiles: best and worst mover of the day among fresh entries.
    # Only fresh stock entries count; "~buffett" has no "day" key and is
    # deliberately excluded from best/worst.
    movers = [d for d in data.values() if not d.get("stale") and d.get("day") is not None]
    best = max(movers, key=lambda d: d["day"]) if movers else None
    worst = min(movers, key=lambda d: d["day"]) if movers else None
    best_name = worst_name = ""
    for s, n, _, _, _ in STOCKS:
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
                    .replace("{{BUFFETT}}", build_buffett(data.get("~buffett")))
                    .replace("{{STAMP}}", stamp))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(rows)} stocks.")


if __name__ == "__main__":
    main()
