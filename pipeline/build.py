#!/usr/bin/env python3
"""Build the Portfolio Cockpit dashboard (single self-contained HTML).

Reads:
  portfolio.json      - snapshot from Parqet (value, gains, xirr, holdings)
  research/<TICKER>.json - per-ticker news + catalysts + pulse (from research agents)

Writes:
  cockpit.html        - the dashboard (open directly or publish as Artifact)

Re-run daily after refreshing the source JSON to keep the dashboard current.
"""
import json, os, re, datetime, html

ROOT = os.path.dirname(os.path.abspath(__file__))

def load(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return json.load(f)

port = load("portfolio.json")
# portfolio.json liegt im OEFFENTLICHEN Repo und traegt seit 2026-09-09 keine Geldwerte
# mehr (nur Thesen, Links, Ticker). Alle Zahlen kommen aus der Engine (pp.json);
# die Nullen hier sind reine Absturzsicherung fuer einen Lauf ohne pp.json.
port.setdefault("cashValue", 0)
for _h in port.get("holdings", []):
    for _k in ("value", "shares", "price"): _h.setdefault(_k, 0)
_MONEY_TOP = ("totalValue","netGainUnrealized","unrealizedReturn","izf","ttwror",
              "realizedAllTime","chartYtd","ttwrorYtd","since2022")
_MONEY_H = ("costPrice","unrealizedReturn","totalGainNet","dayChange","prevClose")
def sanitized(p):
    """Kopie ohne Depotwerte — nur das darf ins Repo zurueckgeschrieben werden."""
    q = {k: v for k, v in p.items() if k not in _MONEY_TOP}
    q["cashValue"] = 0
    q["holdings"] = [dict({k: v for k, v in h.items() if k not in _MONEY_H},
                          value=0, shares=0, price=0) for h in p.get("holdings", [])]
    return q
try:
    pp = load("pp.json")
except Exception:
    pp = None

# Wire depot.xml-derived values (hourly-refreshed by pp_sync.py) into the snapshot:
# PP-exact returns (TTWROR/YTD/IZF - replaces ALL Parqet return figures),
# net cash (fully computed, EUR->USD) and the NVO option (Parqet doesn't track it).
if pp:
    try:
        fxr = list(load("fx_daily.json")["rates"].items())
        usd_per_eur = sorted(fxr)[-1][1]["USD"]
    except Exception:
        usd_per_eur = 1.14
    # PP-exact return engine values (verified vs PP app 2026-07-11)
    if pp.get("ttwrorSince2022") is not None:
        port["since2022"] = pp["ttwrorSince2022"]
    if pp.get("ttwrorYtd") is not None:
        port["ttwrorYtd"] = pp["ttwrorYtd"]
    if pp.get("izf") is not None:
        port["izf"] = pp["izf"]
    if pp.get("cashEur") is not None:
        cash_usd = round(pp["cashEur"] * usd_per_eur)
        port["cashValue"] = cash_usd
        for h in port["holdings"]:
            if h.get("assetType") == "cash":
                h["value"] = cash_usd
    # ---- holdings reconcile: POSITIONS come from the engine (depot.xml via pp.json);
    # portfolio.json only contributes presentation extras (thesis, links, pid).
    # New buys appear and sold positions disappear automatically — the snapshot can
    # never again show a stale position list (WGS-sold/RPID-missing incident 2026-07-16).
    if pp.get("holdings"):
        alias = {"PINK.V": "PINK"}
        secs = [x for x in pp["holdings"] if x.get("ticker") and len(x["ticker"]) <= 10]
        by_tk = {h["ticker"]: h for h in port["holdings"] if h.get("assetType") == "security"}
        rest = [h for h in port["holdings"] if h.get("assetType") != "security"]
        merged = []
        for x in secs:
            tk = alias.get(x["ticker"], x["ticker"])
            h = by_tk.get(tk)
            if h is None:   # brand-new position: sensible defaults, thesis fills in later
                nm = (x.get("name") or tk).title()
                h = {"pid": "pp_" + tk, "ticker": tk, "name": nm, "assetType": "security",
                     "thesis": "", "ySym": x["ticker"], "gquery": nm,
                     "links": {"stocktwits": "https://stocktwits.com/symbol/" + tk,
                               "x": "https://x.com/search?q=%24" + tk + "&f=live",
                               "finviz": "https://finviz.com/quote.ashx?t=" + tk}}
            h["shares"] = x.get("shares"); h["price"] = x.get("price")
            h["value"] = round(x.get("valueUsd") or x.get("value") or 0)
            if x.get("dayRet") is not None: h["dayChange"] = x["dayRet"]   # Engine-Tageswert
            if x.get("prevClose") is not None: h["prevClose"] = x["prevClose"]
            if x.get("unrealRet") is not None: h["unrealizedReturn"] = x["unrealRet"]
            if x.get("basisUsd"):
                h["totalGainNet"] = round((x.get("valueUsd") or 0) - x["basisUsd"] + (x.get("realizedUsd") or 0))
            if x.get("short"): h["short"] = True   # Leerverkauf: Stueck/Wert negativ, Badge im Client
            else: h.pop("short", None)               # kein Dauer-Rauschen in portfolio.json
            merged.append(h)
        cash_first = [h for h in rest if h.get("assetType") == "cash"]
        # options reconcile like securities: the engine (OCC-style tickers, e.g.
        # ABVX270115C00120000) is the source of truth — new contracts appear, sold
        # ones disappear (NVO-call-sold incident 2026-07-22)
        MON = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
        DASH = "\u2014"
        prev_opts = {h.get("occ") or h.get("ticker"): h for h in rest if h.get("assetType") != "cash"}
        options = []
        for x in pp["holdings"]:
            m = re.match(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$", x.get("ticker") or "")
            if not m or (x.get("shares") or 0) <= 0:
                continue
            und, ymd, cp = m.group(1), m.group(2), m.group(3)
            strike = int(m.group(4)) / 1000
            label = f"{MON[int(ymd[2:4])]}-20{ymd[0:2]} ${strike:g} {'Call' if cp == 'C' else 'Put'}"
            h = prev_opts.get(m.group(0)) or prev_opts.get(und)
            if h is None:   # brand-new contract: sensible defaults, thesis fills in later
                h = {"pid": "pp_" + m.group(0), "ticker": und, "assetType": "option",
                     "name": und + " " + DASH + " " + label, "thesis": "", "gquery": und,
                     "links": {"stocktwits": "https://stocktwits.com/symbol/" + und,
                               "x": "https://x.com/search?q=%24" + und + "&f=live",
                               "finviz": "https://finviz.com/quote.ashx?t=" + und}}
            else:           # keep the curated name prefix, refresh the contract label
                h["name"] = (h.get("name") or und).split(" " + DASH + " ")[0] + " " + DASH + " " + label
            h["occ"] = m.group(0)
            h["shares"] = x.get("shares"); h["price"] = x.get("price")
            h["value"] = round(x.get("valueUsd") or x.get("value") or 0)
            if x.get("dayRet") is not None: h["dayChange"] = x["dayRet"]   # Engine-Tageswert
            if x.get("prevClose") is not None: h["prevClose"] = x["prevClose"]
            if x.get("avgCost"): h["costPrice"] = x["avgCost"]
            if x.get("unrealRet") is not None: h["unrealizedReturn"] = x["unrealRet"]
            if x.get("basisUsd"):
                h["totalGainNet"] = round((x.get("valueUsd") or 0) - x["basisUsd"] + (x.get("realizedUsd") or 0))
            options.append(h)
        port["holdings"] = cash_first + sorted(merged, key=lambda h: -h["value"]) + options
        # persist ticker-set changes back to portfolio.json — ir_sweep/social_sync read
        # their ticker lists from the FILE, so new buys must land there too (only on
        # set changes, to avoid a value-churn diff in every minute commit)
        if set(by_tk) != {h["ticker"] for h in merged} or set(prev_opts) != {h["occ"] for h in options}:
            try:
                json.dump(sanitized(port), open(os.path.join(ROOT, "portfolio.json"), "w"),
                          ensure_ascii=False, indent=1)
            except Exception: pass
    port["totalValue"] = sum(h["value"] for h in port["holdings"])

# ---- SEC CIK auto-resolution (so a NEWLY bought US ticker auto-gets instant SEC alerts) ----
def sec_cik_map(holdings, extra=()):
    import subprocess, time
    f = os.path.join(ROOT, "sec_tickers.json")
    stale = (not os.path.exists(f)) or (time.time() - os.path.getmtime(f) > 7*86400)
    if stale:
        try:
            subprocess.run(["curl", "-s", "-m", "20", "-H",
                "User-Agent: PortfolioCockpit contact@portfolio-cockpit.app",
                "https://www.sec.gov/files/company_tickers.json", "-o", f], check=True)
            json.load(open(f))  # validate
        except Exception:
            pass
    try:
        raw = json.load(open(f))
    except Exception:
        return {}
    by_ticker = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
    out = {}
    for h in holdings:
        if h.get("assetType") != "cash":       # option too: watch the underlying's SEC filings
            cik = by_ticker.get((h["ticker"] or "").upper())
            if cik:
                out[h["ticker"]] = cik
    for tk in extra:                           # watchlist tickers get the same SEC alerts
        tk = (tk or "").upper()
        if tk and tk not in out and by_ticker.get(tk):
            out[tk] = by_ticker[tk]
    return out

def _watch_syms():
    try:
        return json.load(open(os.path.join(ROOT, "site_state.json"))).get("watchlist", [])
    except Exception:
        return []

sec_cik = sec_cik_map(port["holdings"], _watch_syms())

# compact ticker->CIK map as a STATIC site file (research tab resolves any US ticker;
# kept out of data.json to protect the Firestore budget)
def write_cik_map():
    try:
        raw = json.load(open(os.path.join(ROOT, "sec_tickers.json")))
        m = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
        json.dump(m, open(os.path.join(ROOT, "cik.json"), "w"), separators=(",", ":"))
    except Exception:
        pass
write_cik_map()

# ---- PRIVACY: restrict the security name map to ISINs that actually appear in the
# displayed (Parqet biotech) portfolio's activities. depot.xml also contains a separate
# dividend depot (Swissquote); its security names must NOT be exposed on the page. ----
def displayed_isins():
    import subprocess
    isins, off = set(), 0
    try:
        while off < 4000:
            r = subprocess.run(["curl", "-s", "-m", "20",
                f"https://api.parqet.com/v1/activities?portfolioIds={port.get('portfolioId','66e18c9426cf62020ccc7ee7')}&limit=100&offset={off}"],
                capture_output=True, text=True)
            j = json.loads(r.stdout or "{}")
            acts = j.get("activities", [])
            if not acts:
                break
            for a in acts:
                i = a.get("isin") or (a.get("asset") or {}).get("identifier")
                if i:
                    isins.add(i)
            off += len(acts)
            if off >= j.get("totalCount", 0):
                break
    except Exception:
        return None       # on failure, keep the full map (page still works)
    return isins or None

_shown_isins = displayed_isins()
# displayed brokers = the ports the CURRENT holdings sit in (IBKR/SQ); the private
# depot has its own port name and is never among them
_disp_ports = set()
for _h in (pp.get("holdings", []) if pp else []):
    _disp_ports.update((_h.get("byPort") or {}).keys())
# the Parqet-derived shown-set is frozen in the pre-retirement past — extend it with
# everything the displayed depot itself references, so post-Parqet buys stay visible
if _shown_isins is not None and pp:
    _shown_isins |= {h.get("isin") for h in pp.get("holdings", []) if h.get("isin")}
    _shown_isins |= {t.get("isin") for t in pp.get("trades", [])
                     if t.get("isin") and (not _disp_ports or t.get("port") in _disp_ports)}

# Persist the privacy filter INTO pp.json itself, so the copy that lands in the public
# repo's pipeline/ (and the cloud rebuild, which has no web egress to re-fetch) never
# carries the separate depots' security names. Only when the fetch succeeded.
if pp and _shown_isins is None and pp.get("securities"):
    # Parqet-free fallback (account retired 2026-07-22): keep only securities the
    # displayed portfolio itself references — holdings, closed trades, buy markers.
    # Payments are excluded on purpose: the private dividend depot pays through there.
    ref_isin = {h.get("isin") for h in pp.get("holdings", [])} | {t.get("isin") for t in pp.get("trades", [])}
    ref_tk = ({h.get("ticker") for h in pp.get("holdings", [])} | {t.get("ticker") for t in pp.get("trades", [])}
              | set((pp.get("buysByTicker") or {}).keys()))
    _shown_isins = {s.get("isin") for s in pp["securities"]
                    if s.get("isin") in ref_isin or s.get("ticker") in ref_tk}
if pp and _shown_isins is not None and pp.get("securities"):
    kept = [s for s in pp["securities"] if s.get("isin") in _shown_isins]
    if kept and len(kept) < len(pp["securities"]):
        pp["securities"] = kept
        json.dump(pp, open(os.path.join(ROOT, "pp.json"), "w"), ensure_ascii=False, indent=1)

# class-action / law-firm noise is never wanted — not as news, not as "catalysts"
import re
JUNK_RE = re.compile(
    r"class action|class-action|lead.plaintiff|shareholder (notice|alert|rights|deadline)|"
    r"reminds (investors|shareholders)|claimsfiler|faruqi|rosen law|pomerantz|bragar|kaskela|"
    r"levi\s*&|encourages investors|investigation (on behalf|of)|law offices|deadline alert|"
    r"securities fraud|lawsuit", re.I)
def _clean_research(r):
    r["news"] = [n for n in r.get("news", []) if not JUNK_RE.search(n.get("headline", "") + " " + n.get("summary", ""))]
    r["catalysts"] = [c for c in r.get("catalysts", [])
                      if not JUNK_RE.search(c.get("label", "") + " " + c.get("detail", ""))]
    return r

research = {}
rdir = os.path.join(ROOT, "research")
for fn in os.listdir(rdir):
    if fn.endswith(".json"):
        r = json.load(open(os.path.join(rdir, fn), encoding="utf-8"))
        research[r["ticker"]] = _clean_research(r)

# Live market data from Yahoo (fetched via browser). Map Yahoo symbol -> our ticker.
YMAP = {"PINK.V": "PINK"}
market = {}
ydir = os.path.join(ROOT, "yahoo")
if os.path.isdir(ydir):
    for fn in os.listdir(ydir):
        if fn.endswith(".json"):
            m = json.load(open(os.path.join(ydir, fn), encoding="utf-8"))
            tk = YMAP.get(m["symbol"], m["symbol"])
            market[tk] = m

# Yahoo symbol + news query per ticker (for the browser's live fetching)
YAHOO_SYM = {"PINK": "PINK.V"}
NEWS_QUERY = {
    "QURE": "uniQure", "WGS": "GeneDx", "CLPT": "ClearPoint Neuro",
    "NKTR": "Nektar Therapeutics", "DCTH": "Delcath", "NRXS": "Neuraxis",
    "PINK": "Perimeter Medical Imaging", "TENX": "Tenax Therapeutics",
    "NVO": "Novo Nordisk",   # option on NVO — covered like the underlying stock
}
# Currency of the live Yahoo quote (PINK.V trades in CAD; all others USD)
LIVE_CCY = {"PINK": "CAD"}

# ---- company logos: candidate URL chain per holding (client falls through on 404) ----
# FIRST choice: curated official logo from logos/<TICKER>.{png,jpg,svg} (scraped from the
# company's own website / TradingView), embedded as a data URI so it works everywhere
# (GitHub Pages AND the CSP-locked artifact). CDN chain only as fallback / for sold positions.
import base64
LOGO_DIR = os.path.join(ROOT, "logos")
def local_logo(tk):
    sym = tk.split()[0].upper().replace(".", "_")
    for ext in ("png", "jpg", "jpeg", "svg", "webp"):
        f = os.path.join(LOGO_DIR, f"{sym}.{ext}")
        if os.path.exists(f) and os.path.getsize(f) > 200:
            raw = open(f, "rb").read()
            mime = ("image/png" if raw[:4] == b"\x89PNG" else
                    "image/jpeg" if raw[:2] == b"\xff\xd8" else
                    "image/svg+xml" if raw.lstrip()[:1] == b"<" else "image/png")
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return None

# Parqet CDN covers nearly everything; overrides for the few it misses (micro caps).
LOGO_OVERRIDE = {
    "LXEO": ["https://www.lexeotx.com/wp-content/themes/lexeo/images/favicon.png"],
    "NRXS": ["https://www.google.com/s2/favicons?domain=neuraxis.com&sz=64"],
    "TENX": ["https://www.google.com/s2/favicons?domain=tenaxthera.com&sz=64"],   # Parqet logo is white-on-transparent (invisible)
    "NKTR": ["https://www.google.com/s2/favicons?domain=nektar.com&sz=64"],       # Parqet only has a generated "NE" letter tile
}
isin_by_ticker = {h["ticker"]: h["isin"] for h in (pp.get("holdings", []) if pp else [])
                  if h.get("isin")}
def logo_candidates(h):
    if h.get("assetType") == "cash":
        return []
    tk = h["ticker"]
    loc = local_logo(tk)
    if loc:
        return [loc]                        # curated official logo — no fallback needed
    if tk in LOGO_OVERRIDE:
        return LOGO_OVERRIDE[tk]
    out = []
    isin = isin_by_ticker.get(tk)
    if isin:
        out.append(f"https://assets.parqet.com/logos/isin/{isin}?format=png&size=64")
    sym = tk.split()[0].upper()            # "NVO $40C" -> NVO (option gets the Novo logo)
    out.append(f"https://assets.parqet.com/logos/symbol/{sym}?format=png&size=64")
    ir = (h.get("links") or {}).get("ir", "")
    if ir:
        dom = ir.split("//")[-1].split("/")[0]
        for pre in ("www.", "ir.", "investors.", "investor."):
            if dom.startswith(pre): dom = dom[len(pre):]
        out.append(f"https://www.google.com/s2/favicons?domain={dom}&sz=64")
    return out

# ---- PP per-holding numbers for the Holdings table (Rafael: "Daten unbedingt aus PP") ----
_OCC_RE = re.compile(r"^[A-Z.]{1,6}\d{6}[CP]\d{8}$")
def _pp_key(t):
    return t if _OCC_RE.match(t) else t.split(".")[0]   # PINK.V -> PINK; OCC tickers stay verbatim
pp_by_ticker = {_pp_key(x["ticker"]): x for x in (pp.get("holdings", []) if pp else [])}
for h in port["holdings"]:
    px = pp_by_ticker.get(h.get("occ") or h["ticker"])   # options match via their OCC ticker
    if px and h.get("assetType") != "cash":
        h["ppShares"] = px["shares"]
        h["ppAvgCost"] = px["avgCost"]          # native ccy (USD, PINK: CAD)
        h["ppBasisUsd"] = px.get("basisUsd")    # FIFO remaining cost basis
        h["ppBasisEur"] = px.get("basisEur")    # same basis at transaction-date FX
        h["ppRealizedUsd"] = px.get("realizedUsd")
        h["ppPrice"] = px["price"]
        h["ppCcy"] = px["ccy"]
        h["byPort"] = px.get("byPort")          # shares per broker (SQ / IBKR)

# ---- closed trades (FIFO round-trips) for the trading-record widget ----
# same privacy filter as activities; logos resolve via the trade's own ISIN
for tr in (pp.get("trades", []) if pp else []):
    if tr.get("isin") and tr["ticker"] not in isin_by_ticker:
        isin_by_ticker[tr["ticker"]] = tr["isin"]
# NOT filtered by displayed_isins: trades come from Rafael's own portfolio txs by
# construction, and dropping any would break "sum == verified realized gains"
pp_trades, trade_logos = [], {}
for tr in (pp.get("trades", []) if pp else []):
    pp_trades.append({k: v for k, v in tr.items() if k != "isin"})
    if tr["ticker"] not in trade_logos:          # one logo set per ticker, not per row
        trade_logos[tr["ticker"]] = logo_candidates(tr)

# ---- brand colours from the logos (chips in News / Social / Catalysts) ----
try:
    import logo_colors
    _logo_tks = ({h["ticker"] for h in port["holdings"] if h.get("assetType") != "cash"}
                 | set(trade_logos.keys())
                 | {n.get("ticker") for n in (research_news if "research_news" in dir() else [])})
    def _logo_urls(tk):
        return logo_candidates({"ticker": tk, "links": {}})
    _logo_colors = logo_colors.colors_for(_logo_tks, _logo_urls)
except Exception as e:
    print("logo colours skipped:", e); _logo_colors = {}

# ---- payments ledger for the Payments tab ----
# COMPLETE journal (compact arrays) — tiles/chart/div-by-security all aggregate
# client-side from this one source, so day-exact reporting periods stay exact
_pays = pp.get("payments", []) if pp else []
pay_journal = [[p["d"], p["k"], p["tk"], p["eur"], p["usd"]] for p in _pays]

# Attach research + market to each holding; compute allocation
total = port["totalValue"]
for h in port["holdings"]:
    h["alloc"] = round(100.0 * h["value"] / total, 1)
    h["logos"] = logo_candidates(h)
    if h["assetType"] == "option":   # Kursquelle = Engine (Bid/Ask-Mitte): der Chart-Feed
        h.pop("ySym", None)          # liefert nur den letzten Trade — beim illiquiden Kontrakt
                                     # tagealt (zeigte 30.90 vom eigenen Kauf, 2026-07-26)
        h["gquery"] = NEWS_QUERY.get(h["ticker"], (h.get("name") or h["ticker"]).split(" \u2014 ")[0])
        h["liveCcy"] = LIVE_CCY.get(h["ticker"], "USD")
    elif h["assetType"] != "cash":
        h["ySym"] = YAHOO_SYM.get(h["ticker"], h["ticker"])
        h["gquery"] = NEWS_QUERY.get(h["ticker"], h["name"])
        h["liveCcy"] = LIVE_CCY.get(h["ticker"], "USD")
    h["research"] = research.get(h["ticker"], {"news": [], "catalysts": [], "pulse": ""})
    m = market.get(h["ticker"])
    if m and m.get("price"):
        pc = m.get("prevClose") or m["price"]
        h["livePrice"] = m["price"]
        if h.get("dayChange") is None:   # Engine-Tageswert hat Vorrang (deckt sich mit der Today-Kachel)
            h["dayChange"] = round((m["price"] - pc) / pc * 100, 2) if pc else 0
        h["hi52"] = m.get("hi52"); h["lo52"] = m.get("lo52")
        h["spark"] = m.get("spark", [])
        if h.get("hi52") and h.get("lo52") and h["hi52"] > h["lo52"]:
            h["pos52"] = round((m["price"] - h["lo52"]) / (h["hi52"] - h["lo52"]) * 100, 1)

# Merge all upcoming catalysts across tickers for the timeline
merged = []
for h in port["holdings"]:
    for c in h["research"].get("catalysts", []):
        merged.append({**c, "ticker": h["ticker"]})

# Merge all news across tickers for the "Latest News" feed (newest first)
all_news = []
for h in port["holdings"]:
    for n in h["research"].get("news", []):
        all_news.append({**n, "ticker": h["ticker"]})
all_news.sort(key=lambda n: n.get("date", ""), reverse=True)

data = {
    # with the live-quote overlay the engine series ends TODAY — the header date
    # should say so instead of the depot-file date
    "asOf": (pp["series"][-1]["d"] if pp and pp.get("series") else port["asOf"]),
    # UTC! Mac (EEST) vs runner (UTC) stamps broke the client's freshness compare
    # -> updates were rejected for hours after every local build+push
    # stamp = DATA freshness (engine run time), NOT build-machine clock — a build from
    # a lagging data copy must never outrank fresher on-device/Firestore state
    "generated": (pp.get("generated") if pp else None) or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    "currency": port.get("currency", "EUR"),
    "portfolioId": port.get("portfolioId", "66e18c9426cf62020ccc7ee7"),
    "totalValue": total,
    "izf": port.get("izf", 0),
    "ttwror": port.get("ttwror", 0),
    "ttwrorYtd": port.get("ttwrorYtd", 0),
    "since2022": port.get("since2022"),
    "netGainUnrealized": port.get("netGainUnrealized", 0),
    "unrealizedReturn": port.get("unrealizedReturn", 0),
    "realizedAllTime": port.get("realizedAllTime", 0),
    # PP-exact capital gains (FIFO over depot.xml, USD at tx-date rates) — KPI tiles, not Parqet
    "ppRealizedUsd": pp.get("realizedUsd") if pp else None,
    "ppUnrealizedUsd": pp.get("unrealizedUsd") if pp else None,
    # engine-exact EUR twins + current FX for the currency toggle
    "ppRealizedEur": pp.get("realizedEur") if pp else None,
    "ppUnrealizedEur": pp.get("unrealizedEur") if pp else None,
    # net deposits since 2022 (PP "performance neutral transfers") — auto-updates on new deposits
    "ppNetContribUsd": pp.get("netContribUsd") if pp else None,
    "ppNetContribEur": pp.get("netContribEur") if pp else None,
    # PP Calculation rows (verified vs the app to the cent)
    "ppEarningsUsd": pp.get("earningsUsd") if pp else None, "ppEarningsEur": pp.get("earningsEur") if pp else None,
    "ppFeesUsd": pp.get("feesUsd") if pp else None, "ppFeesEur": pp.get("feesEur") if pp else None,
    "ppTaxesUsd": pp.get("taxesUsd") if pp else None, "ppTaxesEur": pp.get("taxesEur") if pp else None,
    # benchmark daily closes for the Performance tab (bench_fetch.py, cloud)
    "bench": (lambda: (json.load(open(os.path.join(ROOT, "bench.json")))
                       if os.path.exists(os.path.join(ROOT, "bench.json")) else {}))(),
    # watchlist quotes (separate from bench so they never appear as benchmark chips)
    "watch": (lambda: (json.load(open(os.path.join(ROOT, "watch.json")))
                       if os.path.exists(os.path.join(ROOT, "watch.json")) else {}))(),
    # sector ETFs with their 52-week band (bench_fetch.fetch_sectors, hourly)
    "sectors": (lambda: (json.load(open(os.path.join(ROOT, "sectors.json")))
                         if os.path.exists(os.path.join(ROOT, "sectors.json")) else {}))(),
    "watchMeta": (lambda: (json.load(open(os.path.join(ROOT, "watch_meta.json")))
                           if os.path.exists(os.path.join(ROOT, "watch_meta.json")) else {}))(),
    "fund": (lambda: (json.load(open(os.path.join(ROOT, "fund.json")))
                      if os.path.exists(os.path.join(ROOT, "fund.json")) else {}))(),
    "estimates": (lambda: (json.load(open(os.path.join(ROOT, "estimates.json")))
                           if os.path.exists(os.path.join(ROOT, "estimates.json")) else {}))(),
    # multi-year analyst consensus (Seeking Alpha via sa_fetch.py / seed scrape);
    # preferred over the 2-year Yahoo "estimates" when a ticker is present here
    "saEst": (lambda: (json.load(open(os.path.join(ROOT, "sa_estimates.json")))
                       if os.path.exists(os.path.join(ROOT, "sa_estimates.json")) else {}))(),
    "buysByTicker": pp.get("buysByTicker", {}) if pp else {},
    "usdPerEur": usd_per_eur if pp else 1.14,
    # PP net-worth curve (daily EUR value + cum TTWROR) — replaces Parqet's wrong chart
    "chartPP": pp.get("series", []) if pp else [],
    "trades": pp_trades,
    # Activities feed from the engine: trades + dividends/interest, shown ISINs only
    # Activities feed = trades + every cash-relevant booking (PP's transaction list):
    # dividends/interest received, interest charged, deposits, withdrawals. Interest
    # is one PP kind with a sign - split it so a charge reads as a debit, not "+".
    "acts": (lambda: (
        sorted(
          [a for a in (pp.get("acts", []) if pp else [])
             if (a.get("port") in _disp_ports if (a.get("port") and _disp_ports)
                 else (_shown_isins is None or a.get("isin") in _shown_isins or not a.get("isin")))]
          + [{"d": p["d"], "t": p["k"].upper(), "tk": p["tk"], "amt": p["usd"], "ccy": "USD"}
             for p in _pays if p["k"] == "dividend" and p.get("tk")
             and (_shown_isins is None or any(s.get("ticker") == p["tk"] for s in pp.get("securities", [])))]
          + [{"d": p["d"], "t": "INTEREST" if p["usd"] >= 0 else "INTEREST_CHARGE",
              "tk": p.get("tk"), "amt": abs(p["usd"]), "ccy": "USD"}
             for p in _pays if p["k"] == "interest" and abs(p.get("usd") or 0) >= 0.005]
          + [{"d": p["d"], "t": p["k"].upper(), "tk": None, "amt": abs(p["usd"]), "ccy": "USD"}
             for p in _pays if p["k"] in ("deposit", "withdrawal")],
          key=lambda a: a["d"], reverse=True)
    ))(),
    # the individual purchases still held per ticker (open FIFO lots)
    "lots": (pp.get("lotsByTicker", {}) if pp else {}),
    "tradeLogos": trade_logos,
    "payments": pay_journal,
    # security name map for Activities — ONLY ISINs shown in this portfolio (dividend
    # depot names filtered out; if the ISIN lookup failed, _shown_isins is None -> keep all)
    "secByIsin": {s["isin"]: {"tk": s["ticker"], "name": s["name"]}
                  for s in (pp.get("securities", []) if pp else [])
                  if _shown_isins is None or s["isin"] in _shown_isins},
    "isinByTicker": {h["ticker"]: h["isin"] for h in (pp.get("holdings", []) if pp else [])
                     if h.get("isin")},
    "cashValue": port["cashValue"],
    "cashPct": round(100.0 * port["cashValue"] / total, 1),
    "holdings": port["holdings"],
    "catalysts": merged,
    "latestNews": all_news,
    # brand colour per ticker, read out of the company's own logo (logo_colors.py):
    # the chips in News / Social / Catalysts carry the company's tone instead of an
    # invented one. Cached by logo source, so the minute loop costs no requests.
    "logoCol": (lambda: _logo_colors)(),
    # only the single field the template reads — NOT the whole pp.json (which contains
    # the full securities list of every depot; that would leak the separate depots' names)
    "pp": {"ppReferenceTtwror": pp.get("ppReferenceTtwror")} if pp else None,
    "secCik": sec_cik,
    "social": (lambda: (json.load(open(os.path.join(ROOT, "social.json"), encoding="utf-8"))
                        if os.path.exists(os.path.join(ROOT, "social.json")) else {}))(),
    # Code-Version der App (Hash von template.html): der Client vergleicht sie mit der
    # Version im Live-Push und laedt sich EINMAL selbst neu, wenn er veraltet laeuft —
    # iOS-Home-Screen-Apps behalten ihre Seite sonst tagelang (Vorfall 2026-07-29)
    "appBuild": __import__("hashlib").md5(
        open(os.path.join(ROOT, "template.html"), "rb").read()).hexdigest()[:10],
}

DATA_JSON = json.dumps(data, ensure_ascii=True)
# full snapshot for Firestore (firestore_sync.py push uploads it after the build)
with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
    f.write(DATA_JSON)

# ---- privacy: visibility lives in Firestore (portfolios/main.public), mirrored to
# site_state.json by `firestore_sync.py pull`. Public -> bake DATA into the page for
# instant load. Private -> bake NOTHING; the page fetches data from Firestore, which
# only serves it to the signed-in owner (enforced server-side by security rules).
def site_is_public():
    try:
        return json.load(open(os.path.join(ROOT, "site_state.json"))).get("public", True) is not False
    except Exception:
        return True

TEMPLATE = open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()

# ---- instance config (starter-kit): cockpit_config.json overrides the baked-in
# Rafael defaults, so a clone only edits ONE file. Absent/partial config = identical
# output to before (verified byte-identical without a config file).
_CFG_DEFAULTS = {
    "firebaseApiKey": "AIzaSyA8ycuNIjcLCmYpTV8IMJLLpW8S0Jiv4mA",
    "firebaseAuthDomain": "portfolio-cockpit-rg.firebaseapp.com",
    "firebaseProjectId": "portfolio-cockpit-rg",
    "portfolioName": "Mein Depot",
    "liveUrl": "https://gratzerr.github.io/depot/",
    "ogHash": "4bc2b77abbd6131c88235e7de77c55e13c8ed7a573ccdaec8e45a9b1dbcf93f7",
}
try:
    _icfg = {**_CFG_DEFAULTS, **json.load(open(os.path.join(ROOT, "cockpit_config.json")))}
except Exception:
    _icfg = dict(_CFG_DEFAULTS)
if _icfg != _CFG_DEFAULTS:
    TEMPLATE = (TEMPLATE
        .replace(_CFG_DEFAULTS["firebaseApiKey"], _icfg["firebaseApiKey"])
        .replace(_CFG_DEFAULTS["firebaseAuthDomain"], _icfg["firebaseAuthDomain"])
        .replace(_CFG_DEFAULTS["firebaseProjectId"], _icfg["firebaseProjectId"])
        .replace(_CFG_DEFAULTS["portfolioName"], _icfg["portfolioName"])
        .replace(_CFG_DEFAULTS["liveUrl"], _icfg["liveUrl"])
        .replace(_CFG_DEFAULTS["ogHash"], _icfg["ogHash"]))
# bake the CURRENT portfolio name (owner renames via settings -> Firestore -> site_state):
# otherwise every load flashes the old baked default before the config fetch lands
try:
    _live_name = json.load(open(os.path.join(ROOT, "site_state.json"))).get("name", "")
    _live_name = _live_name.replace('"', "").replace("\\", "").strip()
    if _live_name and _live_name != _icfg["portfolioName"]:
        TEMPLATE = TEMPLATE.replace(_icfg["portfolioName"], _live_name)
    if _live_name:   # keep the PWA manifest (home-screen label) in sync too
        _mf_path = (os.path.join(os.path.dirname(ROOT), "manifest.json")
                    if os.path.basename(ROOT) == "pipeline"
                    else os.path.join(ROOT, "site", "manifest.json"))
        try:
            _mf = json.load(open(_mf_path))
            if _mf.get("short_name") != _live_name:
                _mf["name"] = _mf["short_name"] = _live_name
                json.dump(_mf, open(_mf_path, "w"), indent=2)
        except Exception:
            pass
except Exception:
    pass
# Seit 2026-09-09 wird NIE mehr gebacken: die Seite liegt im oeffentlichen Repo, die
# Daten holt sie sich per Schluessel-Link (#k=...) aus Firestore. site_is_public()
# bleibt fuer die Sperrbildschirm-Logik erhalten, entscheidet aber nicht mehr ueber
# den Inhalt der Datei.
out = TEMPLATE.replace("/*__DATA__*/", "null")
mode = "keyed (no data in page — Firestore via #k= link)"
with open(os.path.join(ROOT, "cockpit.html"), "w", encoding="utf-8") as f:
    f.write(out)
print(f"Built cockpit.html  ({len(out):,} bytes)  as-of {data['asOf']}  holdings={len(data['holdings'])}  mode={mode}")
