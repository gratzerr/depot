#!/usr/bin/env python3
"""Download the newest depot.xml the service account can see (= the PP folder the
owner shared with cockpit-actions@...). Skips the download when the file hasn't
changed since the last call (state kept next to the output file).
Usage: drive_fetch.py [outpath]"""
import json, os, subprocess, sys
from sa_token import access_token

ROOT=os.path.dirname(os.path.abspath(__file__))

def fetch_config(tok):
    """cockpit_config.json aus demselben Drive (fuer den Service-Account freigegeben):
    traegt die Firestore-Dokument-ID = den Schluessel des Familien-Links. So steht
    der Schluessel weder im Repo noch in einem GitHub-Secret. Fehlt die Datei,
    laeuft alles wie frueher (portfolios/main). Best effort — nie abbrechen."""
    try:
        q="name = 'cockpit_config.json' and trashed = false"
        r=subprocess.run(["curl","-s","-G","https://www.googleapis.com/drive/v3/files",
            "--data-urlencode",f"q={q}","--data-urlencode","fields=files(id,modifiedTime)",
            "--data-urlencode","orderBy=modifiedTime desc",
            "-H","Authorization: Bearer "+tok],capture_output=True,text=True)
        files=json.loads(r.stdout or "{}").get("files",[])
        if not files: return
        dst=os.path.join(ROOT,"cockpit_config.json")
        subprocess.run(["curl","-s","-o",dst,
            f"https://www.googleapis.com/drive/v3/files/{files[0]['id']}?alt=media",
            "-H","Authorization: Bearer "+tok],check=True)
        json.load(open(dst))          # muss gueltiges JSON sein, sonst weg damit
        print("drive_fetch: cockpit_config.json ok")
    except Exception as e:
        try: os.remove(os.path.join(ROOT,"cockpit_config.json"))
        except Exception: pass
        print("drive_fetch: no cockpit_config.json", str(e)[:80])

def main(out):
    tok=access_token()
    fetch_config(tok)
    q="name = 'depot.xml' and trashed = false"
    r=subprocess.run(["curl","-s","-G","https://www.googleapis.com/drive/v3/files",
        "--data-urlencode",f"q={q}",
        "--data-urlencode","fields=files(id,name,size,modifiedTime)",
        "--data-urlencode","orderBy=modifiedTime desc",
        "-H","Authorization: Bearer "+tok],capture_output=True,text=True)
    files=json.loads(r.stdout or "{}").get("files",[])
    files=[f for f in files if int(f.get("size",0))>1_000_000]
    if not files:
        raise SystemExit("drive_fetch: no depot.xml visible — folder not shared with the service account yet?")
    f=files[0]
    state=out+".mtime"
    last=open(state).read().strip() if os.path.exists(state) else ""
    if f["modifiedTime"]==last and os.path.exists(out):
        print(f"drive_fetch: unchanged ({f['modifiedTime']})"); return
    subprocess.run(["curl","-s","-o",out,
        f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media",
        "-H","Authorization: Bearer "+tok],check=True)
    open(state,"w").write(f["modifiedTime"])
    print(f"drive_fetch: {f['name']} {f['size']}B modified {f['modifiedTime']} -> {out}")

if __name__=="__main__":
    main(sys.argv[1] if len(sys.argv)>1 else "depot_cloud.xml")
