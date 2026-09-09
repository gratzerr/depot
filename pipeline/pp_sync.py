#!/usr/bin/env python3
"""PP-exact performance engine over Portfolio Performance's depot.xml.

Replicates Portfolio Performance's own calculations (verified 2026-07-11 against
the PP app to the cent / heatmap decimal):
  - daily-chained TTWROR since 2022-01-01 (ClientIndex formula:
      delta = (V_t + outbound_t) / (V_{t-1} + inbound_t) - 1, calendar-daily)
  - yearly TTWROR slices (heatmap) + YTD + annualized
  - IZF (money-weighted XIRR, act/365, ClientIRRYield flow set)
  - per-account cash balances (fully computed - no anchor constants)
  - exact holdings + EUR values

Scope: UNFILTERED entire portfolio (all accounts + portfolios). Verified: this
is what Rafael's PP dashboard widgets show. The legacy SC EUR account nets to
0.00 and SQ NOK to 0 since Feb 2023, so today's values equal the IBKR+SQ
filter; only 2022/23 history differs (and matches the app this way:
2022 3.3%, 2023 11.8%).

Verification snapshot (2026-07-11, file quotes Jul 9/10):
  initial 2022-01-01 EUR 2,278.48 (app 2,278.51) | neutral transfers 666,772.19
  (app 666,771.93) | earnings/taxes to the cent | with the app's intraday final
  value the engine returns exactly TTWROR 434.25% / YTD 112.1% / IZF 76.01%.

XStream parsing rules (hard-won, do not simplify):
  - txs appear under SIX tags: portfolio-transaction/portfolioTransaction,
    account-transaction/accountTransaction, transactionFrom/transactionTo
    (transfer legs embedded in crossEntries). Dedup by <uuid>.
  - references are relative paths ("../..", "tag[N]" 1-indexed); resolve via
    parent map, follow chains.
  - owner: parent <transactions> under an account/portfolio def, or crossEntry
    sibling (account/portfolio/accountFrom/accountTo/portfolioFrom/portfolioTo).
  - untyped legs: buysell -> mirror of portfolioTransaction type;
    transfers -> transactionFrom=TRANSFER_OUT / transactionTo=TRANSFER_IN.
  - units: shares x1e8, amounts in cents, prices x1e8. GBX = GBP pence (/100).
  - security price lookup = PP Security.getSecurityPrice (latest-quote rules,
    first-price fallback before history).

Run before build.py. Refreshes fx_daily.json (ECB via frankfurter, multi-ccy).
"""
import xml.etree.ElementTree as ET, re, bisect, json, os, subprocess, datetime, sys, time
from collections import defaultdict

ROOT=os.path.dirname(os.path.abspath(__file__))
FXFILE=os.path.join(ROOT,"fx_daily.json")
START="2022-01-01"
FX_SYMBOLS="USD,CAD,GBP,DKK,JPY,NOK,CHF,SEK"

def find_depot():
    """Locate the live depot.xml robustly (survives moves/renames of the folder).
    Cloud runners (GitHub Actions) set DEPOT_XML to a freshly downloaded copy.
    Candidates: any depot.xml >5MB, not a backup, under CloudStorage/Desktop/Documents.
    Pick the most recently modified one that parses and contains the QURE ISIN."""
    import glob
    envp=os.environ.get("DEPOT_XML")
    if envp and os.path.exists(envp) and os.path.getsize(envp)>1_000_000:
        return envp
    known=[
        "/Users/rafaelgratzer/Library/CloudStorage/GoogleDrive-rafael.gratzer@gmail.com/My Drive/JSRG/2 Finances/2 Portfolio Performance/depot.xml",
        "/Users/rafaelgratzer/Desktop/depot.xml",
    ]
    hits=[f for f in known if os.path.exists(f) and os.path.getsize(f)>5_000_000]
    if hits:
        best=max(hits,key=os.path.getmtime)
        with open(best,encoding="utf-8",errors="replace") as fh:
            if "NL0010696654" in fh.read(): return best
    pats=[
        "/Users/rafaelgratzer/Library/CloudStorage/*/My Drive/**/depot.xml",
        "/Users/rafaelgratzer/Library/CloudStorage/*/Meine Ablage/**/depot.xml",
        "/Users/rafaelgratzer/Desktop/**/depot.xml",
        "/Users/rafaelgratzer/Documents/**/depot.xml",
    ]
    cands=[]
    for p in pats:
        for f in glob.glob(p, recursive=True):
            b=os.path.basename(f).lower()
            if "backup" in b or "alt" in b: continue
            try:
                if os.path.getsize(f)<5_000_000: continue
                cands.append((os.path.getmtime(f), f))
            except OSError: continue
    for _,f in sorted(cands, reverse=True):
        try:
            head=open(f,encoding="utf-8",errors="replace").read(200)
            if "<client>" in head:
                with open(f,encoding="utf-8",errors="replace") as fh:
                    if "NL0010696654" in fh.read():
                        return f
        except OSError: continue
    raise SystemExit("depot.xml not found in any known location")

DEPOT=find_depot()
print("using depot:",DEPOT)

# ---------------- FX (ECB daily, EUR base, multi-currency) ----------------
def refresh_fx():
    """Freshness comes from the CONTENT, never from the file's mtime: a CI checkout
    stamps every file with the current time, so the old mtime gate reported "fetched
    today" on every cloud run and the rates silently froze (they sat at 10 Jul 2026
    for six weeks, which quietly bent every EUR figure the engine produced)."""
    need=True
    latest_have=""
    if os.path.exists(FXFILE):
        try:
            doc=json.load(open(FXFILE)); sample=doc["rates"]
            latest_have=sorted(sample)[-1]
            latest=sample[latest_have]
            # ECB publishes on business days; anything older than the last workday is stale
            d=datetime.date.today()
            while d.weekday()>=5: d-=datetime.timedelta(days=1)
            need=(latest_have < d.isoformat()) or ("CAD" not in latest)
        except Exception:
            need=True
    if need:
        end=datetime.date.today().isoformat()
        try:
            tmp=FXFILE+".tmp"
            subprocess.run(["curl","-s","-m","30","-H","User-Agent: Mozilla/5.0",
                f"https://api.frankfurter.dev/v1/2021-10-01..{end}?base=EUR&symbols={FX_SYMBOLS}",
                "-o",tmp],check=True)
            fresh=json.load(open(tmp))["rates"]   # validate before replacing
            if sorted(fresh)[-1] < latest_have:   # never regress on a partial answer
                raise ValueError("fx feed older than the file we have")
            os.replace(tmp,FXFILE)
            print(f"fx refreshed: {latest_have or 'none'} -> {sorted(fresh)[-1]}")
        except Exception as e:
            print("fx refresh failed, keeping old rates:", e)
refresh_fx()
FXD=json.load(open(FXFILE))["rates"]
fx_dates=sorted(FXD)
def fx_rate(ccy,d):
    i=bisect.bisect_right(fx_dates,d)-1
    if i<0: i=0
    return FXD[fx_dates[i]].get(ccy) or 1.0
def to_eur(amount,ccy,d):
    if ccy=="EUR": return amount
    if ccy=="GBX": return amount/100.0/fx_rate("GBP",d)
    return amount/fx_rate(ccy,d)
def to_usd(amount,ccy,d):
    return to_eur(amount,ccy,d)*fx_rate("USD",d)

# ---------------- parse + canonical extraction ----------------
tree=ET.parse(DEPOT); root=tree.getroot()
parent={c:p for p in root.iter() for c in p}
STEP=re.compile(r'([\w\-\.]+)(?:\[(\d+)\])?$')
def _resolve(el,ref):
    cur=el
    for part in ref.split('/'):
        if part=='..': cur=parent.get(cur)
        elif part:
            m=STEP.match(part)
            if not m: return None
            kids=[k for k in cur if k.tag==m.group(1)]
            i=int(m.group(2) or 1)
            if len(kids)<i: return None
            cur=kids[i-1]
        if cur is None: return None
    return cur
def deref(el):
    n=0
    while el is not None and el.get("reference") and n<10:
        el=_resolve(el,el.get("reference")); n+=1
    return el

secs=root.find("securities").findall("security")
sec_index={id(s):i for i,s in enumerate(secs)}
SEC=[{"name":s.findtext("name"),"tk":s.findtext("tickerSymbol"),
      "isin":s.findtext("isin"),"ccy":s.findtext("currencyCode") or "EUR"} for s in secs]
PRICES=[];LATEST=[]
for s in secs:
    pr=sorted((p.get("t"),int(p.get("v"))) for p in s.findall("prices/price"))
    PRICES.append(([d for d,_ in pr],[v for _,v in pr]))
    la=s.find("latest")
    LATEST.append((la.get("t"),int(la.get("v"))) if (la is not None and la.get("v")) else None)
def sec_price(i,d):
    """PP Security.getSecurityPrice: prefer historic; latest only if newest."""
    ds,vs=PRICES[i]; la=LATEST[i]
    last=ds[-1] if ds else None
    if la is not None and (last is None or (d>=la[0] and la[0]>=last)):
        return la[1]/1e8
    if last is None: return 0.0
    if last<=d: return vs[-1]/1e8
    k=bisect.bisect_right(ds,d)-1
    if k<0: k=0
    return vs[k]/1e8

ACC_TAGS={"account","accountFrom","accountTo","referenceAccount"}
PORT_TAGS={"portfolio","portfolioFrom","portfolioTo"}
accounts={};portfolios={};el2acc={};el2port={}
for el in root.iter():
    if el.get("reference"): continue
    u=el.findtext("uuid")
    if not u: continue
    if el.tag in ACC_TAGS and el.find("currencyCode") is not None:
        accounts.setdefault(u,{"name":el.findtext("name"),"ccy":el.findtext("currencyCode")})
        el2acc[id(el)]=u
    elif el.tag in PORT_TAGS and el.find("referenceAccount") is not None:
        portfolios.setdefault(u,{"name":el.findtext("name")})
        el2port[id(el)]=u
def owner_uuid(el):
    if el is None: return None
    el=deref(el)
    return el.findtext("uuid") if el is not None else None

TX_TAGS={"account-transaction","accountTransaction","portfolio-transaction",
         "portfolioTransaction","transactionFrom","transactionTo"}
txs={}
for el in root.iter():
    if el.tag not in TX_TAGS or el.get("reference"): continue
    u=el.findtext("uuid")
    if not u or u in txs: continue
    par=parent.get(el)
    kind=None;owner=None;ce=None;legrole=None
    if par is not None and par.tag=="transactions":
        gp=parent.get(par)
        if id(gp) in el2acc: kind="acc";owner=el2acc[id(gp)]
        elif id(gp) in el2port: kind="port";owner=el2port[id(gp)]
    elif par is not None and par.tag=="crossEntry":
        ce=par;cls=par.get("class")
        if el.tag=="accountTransaction": kind="acc";owner=owner_uuid(par.find("account"))
        elif el.tag=="portfolioTransaction": kind="port";owner=owner_uuid(par.find("portfolio"))
        elif el.tag=="transactionFrom":
            legrole="from"
            kind="acc" if cls=="account-transfer" else "port"
            owner=owner_uuid(par.find("accountFrom" if kind=="acc" else "portfolioFrom"))
        elif el.tag=="transactionTo":
            legrole="to"
            kind="acc" if cls=="account-transfer" else "port"
            owner=owner_uuid(par.find("accountTo" if kind=="acc" else "portfolioTo"))
    if ce is None:
        cee=el.find("crossEntry")
        if cee is not None: ce=deref(cee)
    ty=el.findtext("type")
    if ty is None and ce is not None:
        cls=ce.get("class")
        if cls=="buysell":
            pt=deref(ce.find("portfolioTransaction"))
            if pt is not None: ty=pt.findtext("type")
        else:
            if legrole is None:
                tf=deref(ce.find("transactionFrom"))
                legrole="from" if (tf is not None and tf.findtext("uuid")==u) else "to"
            ty={"from":"TRANSFER_OUT","to":"TRANSFER_IN"}[legrole]
    sidx=None
    sref=el.find("security")
    if sref is not None:
        s=deref(sref)
        if s is not None: sidx=sec_index.get(id(s))
    amt=el.findtext("amount");sh=el.findtext("shares")
    fee=tax=0.0   # FEE/TAX units (cents, tx currency) — needed for PP-gross capital gains
    for un in el.findall("units/unit"):
        ua=un.find("amount")
        if ua is None: continue
        v=int(ua.get("amount") or 0)/100.0
        if un.get("type")=="FEE": fee+=v
        elif un.get("type")=="TAX": tax+=v
    txs[u]={"kind":kind,"owner":owner,"type":ty,"date":(el.findtext("date") or "")[:10],
            "ccy":el.findtext("currencyCode") or "EUR",
            "amount":int(amt)/100.0 if amt else 0.0,
            "fee":fee,"tax":tax,
            "shares":int(sh)/1e8 if sh else 0.0,"sec":sidx}
_bad=[t for t in txs.values() if t["type"] is None or t["owner"] is None]
if _bad:
    print(f"WARNING: {len(_bad)} unresolved tx legs — results may drift",file=sys.stderr)

# ---------------- event streams (unfiltered entire portfolio) ----------------
CREDIT={"DEPOSIT","DIVIDENDS","FEES_REFUND","INTEREST","SELL","TRANSFER_IN","TAX_REFUND"}
DEBIT={"REMOVAL","FEES","INTEREST_CHARGE","TAXES","TRANSFER_OUT","BUY"}
PADD={"BUY","DELIVERY_INBOUND","TRANSFER_IN"};PSUB={"SELL","DELIVERY_OUTBOUND","TRANSFER_OUT"}

# ---- LIVE QUOTE OVERLAY ----------------------------------------------------
# depot.xml quotes are only as fresh as the last time PP refreshed them on the Mac.
# The site must be current: overlay today's live prices (yfinance) onto LATEST for
# currently-held securities, so TODAY's valuation/TTWROR/YTD move with the market.
# History stays untouched (sec_price uses file history for all past days).
def us_eff_date():
    """Last US trading day with (at least partial) prices: weekends roll back to
    Friday; before ~market open (13:35 UTC) the previous trading day counts."""
    now=datetime.datetime.utcnow()
    d=now.date()
    if now.hour*60+now.minute < 13*60+35: d-=datetime.timedelta(days=1)
    while d.weekday()>=5: d-=datetime.timedelta(days=1)
    return d.isoformat()

PREV={}   # si -> Vortagesschluss desselben Kursanbieters wie der Live-Kurs

def live_overlay():
    net=defaultdict(float)
    for t in txs.values():
        if t["kind"]=="port" and t["sec"] is not None:
            if t["type"] in PADD: net[t["sec"]]+=t["shares"]
            elif t["type"] in PSUB: net[t["sec"]]-=t["shares"]
    held=[si for si,sh in net.items() if sh>1e-6]
    if not held: return
    try:
        import warnings; warnings.filterwarnings("ignore")
        import yfinance as yf
    except Exception:
        return
    eff=us_eff_date()
    ok=0
    # Letzte GUTE Optionskurse: die Optionsketten-Abfrage ist wackelig (Rate-Limits),
    # und ohne Cache fiel der Kurs dann auf den PP-Datei-Wert = Kaufpreis zurueck
    # (ABVX-Call sprang staendig zwischen 27.85 und 30.90, 2026-07-27)
    PREV.clear()   # Vortagesschluss AUS DERSELBEN Quelle wie der Live-Kurs
    OQ_PATH=os.path.join(ROOT,"opt_quotes.json")
    try: oq=json.load(open(OQ_PATH))
    except Exception: oq={}
    oq_dirty=False
    for si in held:
        tk=SEC[si]["tk"]
        if not tk: continue
        if len(tk)>10 and not re.match(r"^[A-Z.]{1,6}\d{6}[CP]\d{8}$", tk):
            continue   # Fremdformate: Datei-Kurse behalten. Echte OCC-Optionen werden
                       # live quotiert — PP kann sie nicht, sonst friert der Kurs am
                       # Kauftag ein (ABVX-Call stand 5 Tage auf 30.90, 2026-07-26)
        occ = len(tk)>10
        try:
            if occ:
                # OCC-Optionen: yfinance-history ist leer, rohes curl blockt Yahoo im
                # CI — die Optionskette (yf.option_chain) geht ueber yfinance-Cookies
                # und funktioniert dort. Kurs = Geld/Brief-Mitte wie beim Broker.
                m2=re.match(r"^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", tk)
                und,yy,mm,dd,cp,kk=m2.groups()
                px=0.0; qual=0   # 2 = Geld/Brief-Mitte, 1 = Last-Trade VON HEUTE, 0 = nichts
                for _try in range(3):
                    try:
                        oc=yf.Ticker(und).option_chain(f"20{yy}-{mm}-{dd}")
                        df=oc.calls if cp=="C" else oc.puts
                        row=df[abs(df["strike"]-int(kk)/1000.0)<1e-6]
                        if len(row):
                            r0=row.iloc[0]
                            bid=float(r0.get("bid") or 0); ask=float(r0.get("ask") or 0)
                            if bid>0 and ask>0:
                                px,qual=(bid+ask)/2.0,2
                            else:
                                # lastPrice ist der letzte GEHANDELTE Kurs — bei einem weit
                                # aus dem Geld liegenden Call ist der oft Tage alt. Nur
                                # nehmen, wenn der Trade wirklich von eff stammt, sonst
                                # kippt der Kurs nachts auf einen Altdruck zurueck
                                # (ABVX-Call: 21.25 -> 17.00 von vorgestern, 2026-09-09).
                                lp=float(r0.get("lastPrice") or 0)
                                ltd=str(r0.get("lastTradeDate") or "")[:10]
                                if lp>0 and re.match(r"^\d{4}-\d{2}-\d{2}$",ltd) and ltd>=eff:
                                    px,qual=lp,1
                        if qual: break
                    except Exception:
                        pass
                    time.sleep(1.5)
                c0=oq.get(tk)
                # Ein schlechterer Kurs darf einen besseren DESSELBEN Tages nie ersetzen:
                # nach Boersenschluss liefert die Kette haeufig bid=ask=0, der Altdruck
                # wuerde sonst die tagsueber ermittelte Mitte ueberschreiben.
                if qual and c0 and c0.get("d")==eff and qual<int(c0.get("q") or 1):
                    qual=0
                if qual:
                    if c0 and c0.get("d") and c0["d"]<eff and float(c0.get("px") or 0)>0:
                        PREV[si]=float(c0["px"])           # Stand von gestern = Vortagesschluss
                    elif c0 and c0.get("prev"):
                        PREV[si]=float(c0["prev"])
                    oq[tk]={"d":eff,"px":px,"q":qual,
                            "prev":PREV.get(si) or (c0 or {}).get("prev")}; oq_dirty=True
                else:
                    if not c0: continue      # Abruf gescheitert/nur Altdruck: letzten GUTEN
                    px=float(c0["px"])       # Kurs halten, nie auf den PP-Kaufpreis zurueck
                    # Vortagesschluss auch im Halte-Fall setzen, sonst rechnet die
                    # Tagesveraenderung gegen die eingefrorene Datei-Historie
                    if c0.get("d") and c0["d"]<eff and px>0: PREV[si]=px
                    elif c0.get("prev"): PREV[si]=float(c0["prev"])
                pairs=[(eff, px)]
            else:
                h2=yf.Ticker(tk).history(period="10d")["Close"]
                pairs=[(idx.strftime("%Y-%m-%d"), float(px)) for idx,px in h2.items()]
                pv=[p for d,p in sorted(pairs) if d<eff and p>0]
                if pv: PREV[si]=pv[-1]
                # Yahoo liefert fuer HEUTE keinen Balken (Datenstau wie am 2026-08-06:
                # alle Kurse standen auf dem Vortagesschluss, DCTH real ~15 vs. 12.63):
                # Stooq als unabhaengige Zweitquelle fuer den laufenden Kurs — nur
                # US-Plain-Ticker; PREV bleibt der offizielle Yahoo-Schlusskurs
                if not any(d>=eff for d,_ in pairs) and re.match(r"^[A-Z]{1,5}$", tk):
                    try:
                        rr=subprocess.run(["curl","-s","-m","12",
                            f"https://stooq.com/q/l/?s={tk.lower()}.us&f=sd2t2ohlcv&h&e=csv"],
                            capture_output=True,text=True)
                        rows=rr.stdout.strip().split("\n")
                        if len(rows)>=2:
                            c=rows[1].split(",")
                            sq_d, sq_px = c[1], float(c[6])
                            if sq_px>0 and sq_d>=eff:
                                pairs.append((eff, sq_px))
                    except Exception:
                        pass
        except Exception:
            continue
        if not pairs: continue
        pairs.sort()
        ds,vs=PRICES[si]
        last_file=ds[-1] if ds else "0000"
        for d,px in pairs:
            if d<=last_file or d>eff or px!=px or px<=0: continue
            ds.append(d); vs.append(int(px*1e8))
        lastq=pairs[-1][1]
        la=LATEST[si]
        if lastq>0 and (la is None or eff>=la[0]):
            LATEST[si]=(eff,int(lastq*1e8)); ok+=1
    if oq_dirty:
        try: json.dump(oq, open(OQ_PATH,"w"))
        except Exception: pass
    print(f"live overlay: {ok}/{len(held)} held securities quoted live (gap-filled to {eff})")
if os.environ.get("LIVE_QUOTES","1")=="1":
    live_overlay()
pos_ev=defaultdict(list);cash_ev=defaultdict(list)
inflow=defaultdict(float);outflow=defaultdict(float)
irr_flows=[]
# net contributions ("Performance neutral Transfers" in PP's Calculation tab):
# deposits + inbound deliveries minus removals + outbound deliveries, at tx-date FX
contrib_eur=0.0;contrib_usd=0.0
earn_eur=earn_usd=fees_eur=fees_usd=tax_eur=tax_usd=0.0   # PP Calculation rows (gross)
# per-day versions of the same rows, so the calc widget can report any sub-period
earn_day=defaultdict(lambda:[0.0,0.0])   # d -> [eur, usd]
fees_day=defaultdict(lambda:[0.0,0.0])
tax_day=defaultdict(lambda:[0.0,0.0])
def _bump(m,d,e,u): m[d][0]+=e;m[d][1]+=u
payments=[]   # payment ledger for the Payments tab (every cash-relevant booking since START)
def _pay(d,k,sec,e,u):
    tk=(SEC[sec]["tk"] or (SEC[sec]["name"] or "")[:10]) if sec is not None else None
    payments.append({"d":d,"k":k,"tk":tk,"eur":round(e,2),"usd":round(u,2)})
for t in txs.values():
    d=t["date"];ty=t["type"];a=t["amount"];ccy=t["ccy"]
    if d>START:
        fe,tx_=t.get("fee",0.0),t.get("tax",0.0)
        if fe: e=to_eur(fe,ccy,d);u=to_usd(fe,ccy,d);fees_eur+=e;fees_usd+=u;_bump(fees_day,d,e,u);_pay(d,'fee',t.get("sec"),e,u)
        if tx_: e=to_eur(tx_,ccy,d);u=to_usd(tx_,ccy,d);tax_eur+=e;tax_usd+=u;_bump(tax_day,d,e,u);_pay(d,'tax',t.get("sec"),e,u)
        if t["kind"]=="acc":
            if ty in ("DIVIDENDS","INTEREST"):
                g=a+tx_   # gross: PP zeigt Steuern separat
                e=to_eur(g,ccy,d);u=to_usd(g,ccy,d);earn_eur+=e;earn_usd+=u;_bump(earn_day,d,e,u)
                _pay(d,'dividend' if ty=="DIVIDENDS" else 'interest',t.get("sec"),e,u)
            elif ty=="INTEREST_CHARGE":
                e=to_eur(a,ccy,d);u=to_usd(a,ccy,d);earn_eur-=e;earn_usd-=u;_bump(earn_day,d,-e,-u)
                _pay(d,'interest',t.get("sec"),-e,-u)
            elif ty=="FEES":
                e=to_eur(a,ccy,d);u=to_usd(a,ccy,d);fees_eur+=e;fees_usd+=u;_bump(fees_day,d,e,u);_pay(d,'fee',t.get("sec"),e,u)
            elif ty=="FEES_REFUND":
                e=to_eur(a,ccy,d);u=to_usd(a,ccy,d);fees_eur-=e;fees_usd-=u;_bump(fees_day,d,-e,-u);_pay(d,'fee',t.get("sec"),-e,-u)
            elif ty=="TAXES":
                e=to_eur(a,ccy,d);u=to_usd(a,ccy,d);tax_eur+=e;tax_usd+=u;_bump(tax_day,d,e,u);_pay(d,'tax',t.get("sec"),e,u)
            elif ty=="TAX_REFUND":
                e=to_eur(a,ccy,d);u=to_usd(a,ccy,d);tax_eur-=e;tax_usd-=u;_bump(tax_day,d,-e,-u);_pay(d,'tax',t.get("sec"),-e,-u)
            elif ty=="DEPOSIT":
                _pay(d,'deposit',None,to_eur(a,ccy,d),to_usd(a,ccy,d))
            elif ty=="REMOVAL":
                _pay(d,'withdrawal',None,to_eur(a,ccy,d),to_usd(a,ccy,d))
        elif t["kind"]=="port":
            if ty=="DELIVERY_INBOUND": _pay(d,'deposit',t.get("sec"),to_eur(a,ccy,d),to_usd(a,ccy,d))
            elif ty=="DELIVERY_OUTBOUND": _pay(d,'withdrawal',t.get("sec"),to_eur(a,ccy,d),to_usd(a,ccy,d))
    if t["kind"]=="port":
        if ty in PADD: pos_ev[d].append((t["sec"],t["shares"]))
        elif ty in PSUB: pos_ev[d].append((t["sec"],-t["shares"]))
        if ty=="DELIVERY_INBOUND":
            inflow[d]+=to_eur(a,ccy,d);irr_flows.append((d,-to_eur(a,ccy,d)))
            if d>START: contrib_eur+=to_eur(a,ccy,d);contrib_usd+=to_usd(a,ccy,d)
        elif ty=="DELIVERY_OUTBOUND":
            outflow[d]+=to_eur(a,ccy,d);irr_flows.append((d,to_eur(a,ccy,d)))
            if d>START: contrib_eur-=to_eur(a,ccy,d);contrib_usd-=to_usd(a,ccy,d)
        elif ty=="TRANSFER_IN": irr_flows.append((d,-to_eur(a,ccy,d)))
        elif ty=="TRANSFER_OUT": irr_flows.append((d,to_eur(a,ccy,d)))
    else:
        if ty in CREDIT: cash_ev[d].append((t["owner"],a))
        elif ty in DEBIT: cash_ev[d].append((t["owner"],-a))
        if ty=="DEPOSIT":
            inflow[d]+=to_eur(a,ccy,d);irr_flows.append((d,-to_eur(a,ccy,d)))
            if d>START: contrib_eur+=to_eur(a,ccy,d);contrib_usd+=to_usd(a,ccy,d)
        elif ty=="REMOVAL":
            outflow[d]+=to_eur(a,ccy,d);irr_flows.append((d,to_eur(a,ccy,d)))
            if d>START: contrib_eur-=to_eur(a,ccy,d);contrib_usd-=to_usd(a,ccy,d)
        elif ty=="TRANSFER_IN": irr_flows.append((d,-to_eur(a,ccy,d)))
        elif ty=="TRANSFER_OUT": irr_flows.append((d,to_eur(a,ccy,d)))

# ---------------- daily total-wealth series + TTWROR chain ----------------
TODAY=datetime.date.today().isoformat()
END=us_eff_date() if os.environ.get("LIVE_QUOTES","1")=="1" else TODAY
shares=defaultdict(float);cash=defaultdict(float)
for d in sorted(set(list(pos_ev)+list(cash_ev))):
    if d>START: break
    for s,ds in pos_ev.get(d,[]): shares[s]+=ds
    for a,da in cash_ev.get(d,[]): cash[a]+=da
def valuation(d):
    tot=0.0
    for s,sh in shares.items():
        if abs(sh)>1e-9: tot+=to_eur(sh*sec_price(s,d),SEC[s]["ccy"],d)
    for a,c in cash.items():
        if abs(c)>1e-9: tot+=to_eur(c,accounts[a]["ccy"],d)
    return tot
V0=valuation(START)
open_sh=dict(shares)   # opening positions at START (basis for FIFO capital gains)
series={START:0.0};acc_ret=0.0;prevV=V0
cum_c_eur=cum_c_usd=0.0
daily=[{"d":START,"v":round(V0*fx_rate("USD",START)),"e":round(V0),"r":0.0,"c":0,"k":0}]   # PP net-worth curve: v=USD, e=EUR (daily FX)
day=datetime.date.fromisoformat(START)+datetime.timedelta(days=1)
endd=datetime.date.fromisoformat(END)
while day<=endd:
    d=day.isoformat()
    for s,ds in pos_ev.get(d,[]): shares[s]+=ds
    for a,da in cash_ev.get(d,[]): cash[a]+=da
    V=valuation(d)
    inb=inflow.get(d,0.0);outb=outflow.get(d,0.0)
    delta=0.0 if abs(prevV+inb)<1e-9 else (V+outb)/(prevV+inb)-1
    acc_ret=(acc_ret+1)*(delta+1)-1
    series[d]=acc_ret;prevV=V
    cum_c_eur+=inflow.get(d,0.0)-outflow.get(d,0.0)
    cum_c_usd+=(inflow.get(d,0.0)-outflow.get(d,0.0))*fx_rate("USD",d)
    daily.append({"d":d,"v":round(V*fx_rate("USD",d)),"e":round(V),"r":round(acc_ret,6),
                  "c":round(cum_c_usd),"k":round(cum_c_eur)})
    day+=datetime.timedelta(days=1)
Vend=prevV

def slice_ret(a,b):
    return ((1+series[b])/(1+series[a])-1)*100
yearly={}
prev=START
for y in range(2022,int(END[:4])+1):
    b=min(f"{y}-12-31",END)
    yearly[str(y)]=round(slice_ret(prev,b),1)
    prev=b
ytd=slice_ret(f"{int(END[:4])-1}-12-31",END)
ndays=(endd-datetime.date.fromisoformat(START)).days
annualized=((1+acc_ret)**(365.0/ndays)-1)*100

# ---------------- FIFO capital gains (PP Performance -> Calculation semantics, in EUR) --
# realized = SELL proceeds minus FIFO purchase basis (EUR at the respective tx dates);
# unrealized = current value of remaining lots minus their FIFO basis.
# Opening positions at START are valued at the START price (period basis, like PP).
# Inner portfolio transfers are neutral (skipped); deliveries move basis without realizing
# (PP books deliveries under "transfers", not capital gains).
from collections import deque
lots=defaultdict(deque)   # sec -> deque of [shares, basis_eur, basis_usd, buy_date]
for s,sh in open_sh.items():
    if sh>1e-9:
        v=sh*sec_price(s,START)
        lots[s].append([sh, to_eur(v,SEC[s]["ccy"],START), to_usd(v,SEC[s]["ccy"],START), START])
realized_eur=0.0;realized_usd=0.0
trades=[]   # every SELL as a closed FIFO round-trip (for the trading-record widget)
acts=[]     # raw trade journal for the Activities feed (Parqet retired 2026-07-22)
buys_by_sec=defaultdict(lambda: defaultdict(lambda:[0.0,0.0]))  # sec -> date -> [shares, cost_native] for chart markers
rz_usd_by_sec=defaultdict(float)   # per-security realized (USD) for the holdings table
rz_day=defaultdict(lambda:[0.0,0.0])   # d -> [eur, usd] realized that day (for period calc)
# same-date ordering: process adds (BUY/inbound) BEFORE removes, otherwise a same-day
# round-trip sells from an empty lot queue (full proceeds booked as "gain") and leaves
# a ghost lot behind (caused +99k realized / +20k unrealized drift vs the PP app)
ptx=sorted((t for t in txs.values() if t["kind"]=="port" and t["sec"] is not None and t["date"]>START),
           key=lambda t:(t["date"], 0 if t["type"] in ("BUY","DELIVERY_INBOUND") else 1))
for t in ptx:
    ty=t["type"];s=t["sec"];sh=t["shares"]
    if ty in ("TRANSFER_IN","TRANSFER_OUT"): continue     # internal moves, client-neutral
    # PP capital gains are GROSS: fees/taxes sit in their own rows of the Calculation tab.
    # BUY amount includes fees -> basis = amount - fee - tax; SELL amount is net -> proceeds + fee + tax.
    if ty=="BUY": g=t["amount"]-t["fee"]-t["tax"]
    elif ty=="SELL": g=t["amount"]+t["fee"]+t["tax"]
    else: g=t["amount"]
    eur=to_eur(g,t["ccy"],t["date"]);usd=to_usd(g,t["ccy"],t["date"])
    act_row=None
    if sh>1e-9 and ty in ("BUY","SELL","DELIVERY_INBOUND","DELIVERY_OUTBOUND"):
        act_row={"d":t["date"],"t":"BUY" if ty in ("BUY","DELIVERY_INBOUND") else "SELL",
                 "sec":s,"sh":round(sh,4),"px":round(g/sh,4),
                 "ccy":t["ccy"],"amt":round(g,2),
                 "port":(portfolios.get(t["owner"]) or {}).get("name")}
        acts.append(act_row)
    if ty in ("BUY","DELIVERY_INBOUND"):
        lots[s].append([sh,eur,usd,t["date"]])
        if sh>1e-9:
            b=buys_by_sec[s][t["date"]]; b[0]+=sh; b[1]+=(t["amount"]-t["fee"]-t["tax"])  # native cost ex-fee
    elif ty in ("SELL","DELIVERY_OUTBOUND"):
        rem=sh;basis=0.0;basis_u=0.0;q=lots[s]
        first_buy=None;wdays=0.0;taken=0.0
        sell_dt=datetime.date.fromisoformat(t["date"])
        while rem>1e-9 and q:
            l=q[0];take=min(l[0],rem)
            frac=take/l[0] if l[0]>1e-12 else 0.0
            basis+=l[1]*frac;l[1]-=l[1]*frac
            basis_u+=l[2]*frac;l[2]-=l[2]*frac
            if first_buy is None or l[3]<first_buy: first_buy=l[3]
            wdays+=take*(sell_dt-datetime.date.fromisoformat(l[3])).days
            taken+=take
            l[0]-=take;rem-=take
            if l[0]<=1e-9: q.popleft()
        if ty=="SELL":
            realized_eur+=eur-basis;realized_usd+=usd-basis_u
            rz_usd_by_sec[s]+=usd-basis_u
            if act_row is not None:      # Activities feed shows the gain of each single sell
                act_row["gEur"]=round(eur-basis,2); act_row["gUsd"]=round(usd-basis_u,2)
                act_row["ret"]=round((usd-basis_u)/basis_u*100,2) if basis_u>1e-6 else None
            rz_day[t["date"]][0]+=eur-basis;rz_day[t["date"]][1]+=usd-basis_u
            trades.append({"sec":s,
                "port":(portfolios.get(t["owner"]) or {}).get("name"),
                "buy":first_buy,"sell":t["date"],
                "days":round(wdays/taken) if taken>1e-9 else 0,
                "sh":round(sh,4),
                "proceedsUsd":round(usd),"basisUsd":round(basis_u),
                "proceedsEur":round(eur),"basisEur":round(basis),
                "gainUsd":round(usd-basis_u),"gainEur":round(eur-basis),
                "ret":round((usd-basis_u)/basis_u*100,2) if basis_u>1e-6 else None})
# attach cumulative Calculation rows to the daily series: q/Q realized, g/G earnings,
# f/F fees, t/T taxes (USD/EUR) — lets the calc widget report any reporting period
_re=_ru=_ge=_gu=_fe=_fu=_te=_tu=0.0
for p in daily:
    d=p["d"]
    if d in rz_day:   _re+=rz_day[d][0];  _ru+=rz_day[d][1]
    if d in earn_day: _ge+=earn_day[d][0];_gu+=earn_day[d][1]
    if d in fees_day: _fe+=fees_day[d][0];_fu+=fees_day[d][1]
    if d in tax_day:  _te+=tax_day[d][0]; _tu+=tax_day[d][1]
    p["q"]=round(_ru);p["Q"]=round(_re);p["g"]=round(_gu);p["G"]=round(_ge)
    p["f"]=round(_fu);p["F"]=round(_fe);p["t"]=round(_tu);p["T"]=round(_te)
assert abs(_ru-realized_usd)<1 and abs(_gu-earn_usd)<1 and abs(_fu-fees_usd)<1 and abs(_tu-tax_usd)<1, "daily cums drifted from verified totals"
unrealized_eur=0.0;unrealized_usd=0.0
for s,q in lots.items():
    rsh=sum(l[0] for l in q)
    if rsh<=1e-6: continue
    v=rsh*sec_price(s,END)
    unrealized_eur+=to_eur(v,SEC[s]["ccy"],END)-sum(l[1] for l in q)
    unrealized_usd+=to_usd(v,SEC[s]["ccy"],END)-sum(l[2] for l in q)

# ---------------- IZF (XIRR act/365) ----------------
flows=[(START,-V0)]+[(d,v) for d,v in irr_flows if START<d<=END]+[(END,Vend)]
flows.sort()
d0=datetime.date.fromisoformat(START)
fdays=[(datetime.date.fromisoformat(d)-d0).days for d,_ in flows]
fvals=[v for _,v in flows]
def npv(r): return sum(v/((1+r)**(dd/365.0)) for v,dd in zip(fvals,fdays))
izf=None
lo,hi=-0.95,50.0
flo,fhi=npv(lo),npv(hi)
if flo*fhi<0:
    for _ in range(200):
        mid=(lo+hi)/2;fm=npv(mid)
        if abs(fm)<1e-7: break
        if (fm<0)==(flo<0): lo,flo=mid,fm
        else: hi,fhi=mid,fm
    izf=(lo+hi)/2*100

def _day_ret(i,px_now):
    """Kurs heute vs. Vortagesschluss, in Prozent (None wenn unbekannt)."""
    try:
        p=PREV.get(i)
        if p and px_now: return round((px_now/p-1)*100,2)
        ds,vs=PRICES[i]
        prev=None
        for d,v in zip(reversed(ds),reversed(vs)):
            if d<END and v>0: prev=v/1e8; break
        if not prev or not px_now: return None
        return round((px_now/prev-1)*100,2)
    except Exception:
        return None

# ---------------- cash + holdings ----------------
cash_by_acc=[]
net_cash_eur=0.0
for a,c in sorted(cash.items(),key=lambda kv:-abs(kv[1])):
    if abs(c)>0.005:
        e=to_eur(c,accounts[a]["ccy"],END);net_cash_eur+=e
        cash_by_acc.append({"account":accounts[a]["name"],"ccy":accounts[a]["ccy"],
                            "balance":round(c,2),"eur":round(e,2)})
PP_CASH_EUR=round(net_cash_eur,2)

posagg={}
for t in txs.values():
    if t["kind"]!="port": continue
    a=posagg.setdefault(t["sec"],{"net":0.0,"buyAmt":0.0,"buySh":0.0})
    if t["type"] in PADD:
        a["net"]+=t["shares"]
        if t["type"]=="BUY": a["buyAmt"]+=t["amount"];a["buySh"]+=t["shares"]
    elif t["type"] in PSUB: a["net"]-=t["shares"]
# current shares per broker portfolio (SC / IBKR): replay ALL portfolio txs with
# their owner — transfer legs each carry their own portfolio, so moves between
# brokers net out correctly
sh_by_port=defaultdict(lambda:defaultdict(float))   # sec -> {portName: shares}
for t in txs.values():
    if t["kind"]=="port" and t["sec"] is not None:
        pn=(portfolios.get(t["owner"]) or {}).get("name") or "?"
        if t["type"] in PADD: sh_by_port[t["sec"]][pn]+=t["shares"]
        elif t["type"] in PSUB: sh_by_port[t["sec"]][pn]-=t["shares"]
holdings=[]
tot_sec_eur=0.0
for si,a in posagg.items():
    if si is None or a["net"]<=1e-4: continue
    s=SEC[si]
    px=sec_price(si,END)
    la=LATEST[si];ds,_=PRICES[si]
    pdate=(la[0] if la else None) or (ds[-1] if ds else None)
    avg=a["buyAmt"]/a["buySh"] if a["buySh"] else 0
    val=a["net"]*px
    veur=to_eur(val,s["ccy"],END)
    tot_sec_eur+=veur
    basis_usd=sum(l[2] for l in lots.get(si,[]))   # FIFO remaining cost basis (USD, tx-date FX)
    basis_eur=sum(l[1] for l in lots.get(si,[]))   # ...and the same basis in EUR, so an EUR
                                                   # page never mixes a USD cost into a EUR value
    byport={n:round(v,4) for n,v in sh_by_port.get(si,{}).items() if v>1e-6}
    holdings.append({"account":"ALL","ticker":s["tk"] or (s["name"] or "")[:10],"name":s["name"],
        "byPort":byport,
        "isin":s["isin"],"ccy":s["ccy"],"shares":round(a["net"],4),"price":px,
        "value":round(val,2),"valueEur":round(veur),
        "valueUsd":round(to_usd(val,s["ccy"],END)),
        "basisUsd":round(basis_usd),"basisEur":round(basis_eur),
        "realizedUsd":round(rz_usd_by_sec.get(si,0.0)),
        # entry price = FIFO REMAINING basis / shares (matches Parqet purchasePrice);
        # the naive all-buys average is wrong once some lots were sold. Non-USD (PINK/CAD)
        # keeps the buy average — no sells there, so both are identical.
        "avgCost":round(basis_usd/a["net"],4) if (s["ccy"]=="USD" and a["net"]) else round(avg,4),
        "unrealRet":(round((to_usd(val,s["ccy"],END)/basis_usd-1)*100,1) if basis_usd
                     else (round((px-avg)/avg*100,1) if avg else 0)),
        # Tagesveraenderung AUS DER ENGINE (Kurs heute vs. letzter Schlusskurs davor):
        # die Yahoo-Variante im Client lief auf eigenem Takt und widersprach dem
        # Gesamt-Tageswert der Kachel (Vorfall 2026-07-29)
        "dayRet":_day_ret(si,px),
        "prevClose":round(PREV[si],4) if PREV.get(si) else None,   # nachpruefbar: gegen welchen Kurs gerechnet wird
        "priceDate":pdate})
holdings.sort(key=lambda h:-h["valueEur"])

out={"fileDate":datetime.datetime.fromtimestamp(os.path.getmtime(DEPOT)).strftime("%Y-%m-%d %H:%M"),
     "generated":datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
     "engine":"pp-exact (verified vs PP app 2026-07-11)",
     "totalSecuritiesEur":round(tot_sec_eur),
     "cashEur":PP_CASH_EUR,
     "cashByAccount":cash_by_acc,
     "totalEur":round(tot_sec_eur+net_cash_eur),
     "ttwrorSince2022":round(acc_ret*100,2),
     "ttwrorYtd":round(ytd,2),
     "ttwrorAnnualized":round(annualized,2),
     "izf":round(izf,2) if izf is not None else None,
     "yearlyReturns":yearly,
     "realizedEur":round(realized_eur),
     "unrealizedEur":round(unrealized_eur),
     "realizedUsd":round(realized_usd),
     "unrealizedUsd":round(unrealized_usd),
     "netContribEur":round(contrib_eur),
     "netContribUsd":round(contrib_usd),
     "earningsEur":round(earn_eur,2),"earningsUsd":round(earn_usd),
     "feesEur":round(fees_eur,2),"feesUsd":round(fees_usd),
     "taxesEur":round(tax_eur,2),"taxesUsd":round(tax_usd),
     "series":daily,
     "payments":sorted(payments,key=lambda p:p["d"],reverse=True),
     # buy markers per ticker for the detail chart (held securities only, one entry
     # per day: [date, shares, avg price paid ex-fee in native ccy])
     "buysByTicker":{ (SEC[si]["tk"] or (SEC[si]["name"] or "")[:10]):
         [[d, round(v[0],2), round(v[1]/v[0],4)] for d,v in sorted(dm.items()) if v[0]>1e-9]
         for si,dm in buys_by_sec.items()
         if sum(l[0] for l in lots.get(si,[]))>1e-6 },
     # open FIFO lots per ticker: the individual purchases that make up the CURRENT
     # position (date, shares, remaining basis EUR/USD) - sums to the position's cost
     "lotsByTicker":{ (SEC[si]["tk"] or (SEC[si]["name"] or "")[:10]):
         [{"d":l[3],"sh":round(l[0],4),"eur":round(l[1],2),"usd":round(l[2],2)}
          for l in q if l[0]>1e-9]
         for si,q in lots.items() if sum(l[0] for l in q)>1e-6 },
     "ports":sorted({(portfolios.get(t["owner"]) or {}).get("name") for t in txs.values()
                     if t["kind"]=="port" and t["owner"]} - {None}),
     "acts":sorted(({**{k:v for k,v in a.items() if k!="sec"},
                     "tk":SEC[a["sec"]]["tk"] or (SEC[a["sec"]]["name"] or "")[:10],
                     "isin":SEC[a["sec"]]["isin"]}
                    for a in acts), key=lambda x:x["d"], reverse=True),
     "trades":sorted(({**{k:v for k,v in tr.items() if k!="sec"},
                       "ticker":SEC[tr["sec"]]["tk"] or (SEC[tr["sec"]]["name"] or "")[:10],
                       "name":SEC[tr["sec"]]["name"],"isin":SEC[tr["sec"]]["isin"]}
                      for tr in trades), key=lambda x:x["sell"], reverse=True),
     "securities":[{"isin":s["isin"],"ticker":s["tk"],"name":s["name"]}
                   for s in SEC if s["isin"]],
     # legacy keys kept for template compatibility (now = the exact numbers)
     "ppReferenceTtwror":round(acc_ret*100,2),
     "ppReferenceDate":datetime.date.today().isoformat(),
     "ttwrorApproxSince2022":round(acc_ret*100,2),
     "yearlyReturnsApprox":yearly,
     "holdings":holdings}
out["txmax"]=max((t["date"] for t in txs.values() if t.get("date")), default="")
# NEVER regress: a stale depot.xml (Drive lag, partial download) must not overwrite
# a pp.json that was built from a newer transaction state
try:
    _old=json.load(open(os.path.join(ROOT,"pp.json")))
    if (_old.get("txmax") or "") > out["txmax"]:
        raise SystemExit(f"pp_sync: depot.xml aelter (txmax {out['txmax']}) als pp.json ({_old['txmax']}) — NICHT ueberschrieben")
except SystemExit: raise
except Exception: pass
json.dump(out,open(os.path.join(ROOT,"pp.json"),"w"),ensure_ascii=False,indent=1)
# Actions-Logs oeffentlicher Repos sind oeffentlich (90 Tage): hier stehen NIE
# Betraege — nur Zaehler und Zeitstempel (Vorfall 2026-09-09: Depotwert, Cash und
# Gewinne standen minuetlich im Log)
print(f"pp.json written: {len(holdings)} holdings (txmax {out['txmax']})")
print(f"capital gains (FIFO since 2022) computed; net-worth series {len(daily)} days")
