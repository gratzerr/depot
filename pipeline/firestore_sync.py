#!/usr/bin/env python3
"""Firestore bridge for the cockpit pipeline.
  pull : read portfolios/main -> site_state.json {name, public} (creates the doc on first run)
  push : upload data.json (built by build.py) into the doc's `data` field
Auth: owner OAuth token minted from the Firebase CLI login on this Mac (gtoken.py).
As project owner this bypasses security rules — visitors go through the rules."""
import json, os, sys, subprocess, datetime

def access_token():
    """Cloud runners use the service-account key (SA_KEY env or ./sa_key.json);
    the Mac falls back to the Firebase-CLI login (gtoken)."""
    sa = os.environ.get("SA_KEY") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "sa_key.json")
    if os.path.exists(sa):
        os.environ.setdefault("SA_KEY", sa)
        from sa_token import access_token as sa_at
        return sa_at()
    from gtoken import access_token as gt_at
    return gt_at()

ROOT = os.path.dirname(os.path.abspath(__file__))
# instance config (starter-kit): cockpit_config.json overrides the Rafael defaults
try:
    _icfg = json.load(open(os.path.join(ROOT, "cockpit_config.json")))
except Exception:
    _icfg = {}
_PROJECT = _icfg.get("firebaseProjectId", "portfolio-cockpit-rg")
# Dokument-ID = der Schluessel im Familien-Link (#k=...). Er kommt aus dem GitHub-Secret
# FS_DOC_ID und steht nirgends im Repo. Ohne Secret laeuft alles wie frueher auf "main".
DOC_ID = (os.environ.get("FS_DOC_ID") or _icfg.get("docId") or "main").strip()
_COLL = f"https://firestore.googleapis.com/v1/projects/{_PROJECT}/databases/(default)/documents/portfolios/"
DOC = _COLL + DOC_ID
# Ausgemusterte Schluessel (Config "retire": [...]): werden nach dem Push entwertet,
# damit ein alter Link nicht als zweite Tuer offen bleibt (Schluessel-Kuerzung 2026-09-09)
RETIRE = [str(x).strip() for x in (_icfg.get("retire") or []) if x and str(x).strip() != DOC_ID]
LEGACY = _COLL + "main"   # das alte, ratbare Dokument: bleibt als leerer, oeffentlicher
                          # Herzschlag (nur "updated") fuer den Waechter — ohne Daten
OWNER = _icfg.get("ownerEmail", "rafael.gratzer@gmail.com")

def req(method, url, body=None, tok=None):
    cmd = ["curl","-s","-X",method,url,"-H","Authorization: Bearer "+tok]
    if body is not None:
        cmd += ["-H","Content-Type: application/json","--data-binary","@-"]
        r = subprocess.run(cmd, input=json.dumps(body), capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(r.stdout or "{}")

def pull():
    tok = access_token()
    j = req("GET", DOC, tok=tok)
    # Anlegen bei NOT_FOUND — oder nachruesten, wenn das Dokument zwar existiert (ein
    # push kann es implizit erzeugen), aber die Sichtbarkeits-/Besitzerfelder fehlen:
    # ohne "public" verweigern die Regeln jedem Besucher das Lesen.
    if "fields" not in j or "public" not in j["fields"]:
        # ONLY create on a definitive NOT_FOUND — a transient API error must NEVER
        # recreate the doc with defaults (that reset Rafael's portfolio name on 2026-07-22)
        err = (j.get("error") or {})
        if "fields" not in j and err.get("status") != "NOT_FOUND" and err.get("code") != 404:
            print("firestore pull: transient error, keeping local state:", str(j)[:150]); return
        body = {"fields":{
            "owner":{"stringValue":OWNER},
            "name":{"stringValue":_icfg.get("portfolioName", "Rafael's Portfolio")},
            "public":{"booleanValue":True},
            "data":{"stringValue":""}}}
        # Umzug: Einstellungen (Name, Watchlist, Benchmarks, ...) vom alten Dokument
        # uebernehmen, damit der neue Schluessel nicht bei Null anfaengt
        if DOC_ID != "main":
            # Einstellungen vom Vorgaenger uebernehmen: zuerst ein ausgemusterter Schluessel
            # (hat den aktuellsten Stand), sonst das alte main
            for src in [_COLL + r for r in RETIRE] + [LEGACY]:
                old = (req("GET", src, tok=tok).get("fields") or {})
                keep = {k: v for k, v in old.items() if k in ("owner","name","public","hidden","benchmarks","watchlist","saReq")}
                if not keep.get("owner",{}).get("stringValue"): keep.pop("owner", None)   # schon entwertet
                if keep:
                    body["fields"].update(keep); print("firestore: migrating settings from previous document"); break
        # updateMask: nur diese Felder setzen — ein bereits vorhandenes dataz bleibt unangetastet
        mask = "&".join("updateMask.fieldPaths="+k for k in body["fields"])
        j = req("PATCH", DOC+"?"+mask, body, tok)
        if "fields" not in j:
            print("firestore pull: create failed:", str(j)[:200]); return
        print("firestore: keyed document created/seeded")
    f = j["fields"]
    bench = [v.get("stringValue","") for v in f.get("benchmarks",{}).get("arrayValue",{}).get("values",[]) if v.get("stringValue")]
    watch = [v.get("stringValue","") for v in f.get("watchlist",{}).get("arrayValue",{}).get("values",[]) if v.get("stringValue")]
    sareq = [v.get("stringValue","") for v in f.get("saReq",{}).get("arrayValue",{}).get("values",[]) if v.get("stringValue")]
    state = {"name": f.get("name",{}).get("stringValue","Rafael's Portfolio"),
             "public": f.get("public",{}).get("booleanValue", True),
             "benchmarks": bench, "watchlist": watch, "saReq": sareq}
    json.dump(state, open(os.path.join(ROOT,"site_state.json"),"w"))
    print(f"firestore pull: name={state['name']!r} public={state['public']}")

# Firestore rejects any document over 1 MiB, and the raw snapshot passed that line.
# Dropping data to fit was the wrong trade: a client then keeps whatever history it
# already had, so a device that had not fetched the new HTML showed current holdings
# next to yesterday's activity list. Gzip instead — the whole snapshot travels, four
# times smaller than before, and the phone downloads a quarter of the bytes.
MAX_DOC = 1_000_000

def push():
    import gzip, base64
    tok = access_token()
    data = open(os.path.join(ROOT,"data.json"), encoding="utf-8").read()
    packed = base64.b64encode(gzip.compress(data.encode("utf-8"), 6)).decode("ascii")
    # Seit dem Schluessel-Link (2026-09-09) backt die Seite KEINE Daten mehr ein — acts
    # (Trade-Historie) und social muessen daher mit. Gezippt bleibt der ganze Snapshot
    # bei ~400 KB, weit unter dem 1-MiB-Limit, an dem der ungezippte Push 2026-07-24
    # scheiterte. Nur noch kompakt serialisieren, nichts mehr wegwerfen.
    try:
        data = json.dumps(json.loads(data), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass
    # the old plain field is cleared in the SAME patch: it counts towards the 1 MiB
    # document limit even when nothing writes to it any more
    body = {"fields":{
        "dataz":{"stringValue":packed},
        "data":{"stringValue":""},
        "updated":{"stringValue":datetime.datetime.utcnow().isoformat()+"Z"}}}
    if len(packed) > MAX_DOC:
        print("firestore push: compressed snapshot still over 1 MiB — nothing pushed"); return
    j = req("PATCH", DOC+"?updateMask.fieldPaths=dataz&updateMask.fieldPaths=data&updateMask.fieldPaths=updated", body, tok)
    ok = "fields" in j
    print("firestore push:", "ok" if ok else ("FAILED "+str(j)[:200]),
          f"({len(data)//1024} KB raw -> {len(packed)//1024} KB gzipped)")
    # Herzschlag auf dem alten, ratbaren portfolios/main: KEINE Daten mehr, kein
    # Besitzer — nur "updated", damit der GitHub-Waechter die Frische weiterhin
    # anonym pruefen kann. #k=main zeigt damit nur noch den Sperrbildschirm.
    if ok and DOC_ID != "main":
        # Alte, im Geraet gecachte Seiten (iOS-Home-Screen!) fragen weiter main ab und
        # heilen sich nur, wenn sie dort eine ANDERE appBuild sehen. Ein komplett leeres
        # Dokument gab ihnen nichts -> sie blieben ewig auf dem eingebackenen Stand
        # (Vorfall 2026-09-09 16:30). Darum: nur die Versionsnummer, sonst nichts.
        try: ab = json.loads(data).get("appBuild", "")
        except Exception: ab = ""
        hb = {"fields":{"dataz":{"stringValue":""},
                        "data":{"stringValue": json.dumps({"appBuild": ab}) if ab else ""},
                        "owner":{"stringValue":""},
                        "updated":body["fields"]["updated"]}}
        j2 = req("PATCH", LEGACY+"?updateMask.fieldPaths=dataz&updateMask.fieldPaths=data&updateMask.fieldPaths=owner&updateMask.fieldPaths=updated", hb, tok)
        if "fields" not in j2: print("firestore: heartbeat on main FAILED", str(j2)[:120])
        # ausgemusterte Schluessel entwerten: keine Daten, kein Besitzer — greift effektiv einmal
        for r in RETIRE:
            f = (req("GET", _COLL + r, tok=tok).get("fields") or {})
            if f and (f.get("dataz",{}).get("stringValue") or f.get("owner",{}).get("stringValue")):
                j3 = req("PATCH", _COLL + r + "?updateMask.fieldPaths=dataz&updateMask.fieldPaths=data&updateMask.fieldPaths=owner",
                         {"fields":{"dataz":{"stringValue":""},"data":{"stringValue":""},"owner":{"stringValue":""}}}, tok)
                print("firestore: retired key blanked:", "ok" if "fields" in j3 else "FAILED "+str(j3)[:100])

if __name__ == "__main__":
    (pull if (sys.argv[1:2] or ["pull"])[0]=="pull" else push)()
