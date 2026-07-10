from __future__ import annotations
import hashlib, json, logging, re, sqlite3, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "tukkerscout.db"
CONFIG_PATH = BASE_DIR / "config.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 TukkerScout/2.4"

CHECK_LOCK = threading.Lock()
STATUS = {"running":False,"last_started":None,"last_finished":None,"last_error":None,"next_check":None}

CATEGORY_RULES = [
 ("Transfer",["transfer","akkoord","belangstelling","bod","huur","vertrek","tekent","transfervrij","medische keuring"]),
 ("Blessure",["blessure","geblesseerd","revalidatie","hamstring","knie","enkel","niet inzetbaar"]),
 ("Contract",["contract","verlengt","verlenging","optie gelicht","verbintenis"]),
 ("Europa",["europa league","conference league","uefa","europese","voorronde","loting"]),
 ("Wedstrijd",["wedstrijd","opstelling","basiself","selectie","uitslag","oefenduel","voorbeschouwing","nabeschouwing"]),
 ("Interview",["zegt","vertelt","interview","reactie","spreekt","persconferentie"]),
 ("Clubnieuws",["directeur","staf","trainer","technische staf","clubleiding","bestuur"])
]

STOPWORDS={"de","het","een","en","van","voor","met","naar","bij","op","in","uit","is","zijn","dat","dit","die","als","om","te","na","over","fc","twente","tukkers","nieuws","krijgt","heeft","komt","kan","wordt","wil","weer","tegen","door"}

def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_DIR/"tukkerscout.log",encoding="utf-8"),logging.StreamHandler()])

def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def connect_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn=sqlite3.connect(DB_PATH,timeout=30,check_same_thread=False)
    conn.row_factory=sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS articles(
      id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL UNIQUE,
      found_at TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 3,urgency INTEGER NOT NULL DEFAULT 2,
      category TEXT NOT NULL DEFAULT 'Algemeen',fingerprint TEXT NOT NULL,is_baseline INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'Nieuw',is_favorite INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS source_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,checked_at TEXT NOT NULL,
      relevant_count INTEGER NOT NULL DEFAULT 0,new_count INTEGER NOT NULL DEFAULT 0,
      ok INTEGER NOT NULL DEFAULT 1,message TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS persons(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      role TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      follow INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn

def normalize_title(v): return re.sub(r"\s+"," ",v or "").strip()
def fingerprint(t): return hashlib.sha1(re.sub(r"[^a-z0-9]+"," ",t.lower()).strip().encode()).hexdigest()
def tokens(t): return {w for w in re.findall(r"[a-z0-9À-ÿ]+",t.lower()) if len(w)>2 and w not in STOPWORDS}

def classify(t):
    low=t.lower()
    for cat,words in CATEGORY_RULES:
        if any(w in low for w in words): return cat
    return "Algemeen"

def urgency(t,cat,p):
    low=t.lower()
    if any(x in low for x in ["officieel","akkoord","tekent","bevestigt","ernstige blessure","opstelling"]): return 5
    if cat in {"Transfer","Contract","Blessure","Europa"}: return min(5,max(3,p))
    if cat in {"Wedstrijd","Clubnieuws"}: return min(4,max(2,p-1))
    return min(3,max(1,p-2))

def domain_allowed(url,domains):
    host=(urlparse(url).hostname or "").lower()
    return any(host==d.lower() or host.endswith("."+d.lower()) for d in domains)

def person_terms():
    conn=connect_db()
    try:
        rows=conn.execute("SELECT name FROM persons WHERE active=1 AND follow=1 ORDER BY name").fetchall()
        terms=[]
        for row in rows:
            name=row["name"].strip()
            if not name: continue
            terms.append(name.lower())
            parts=[p for p in re.split(r"\s+",name) if p]
            if len(parts)>=2:
                surname=" ".join(parts[-2:]) if parts[-2].lower() in {"van","de","der","den","ten","ter"} else parts[-1]
                if len(surname)>=4:
                    terms.append(surname.lower())
        return sorted(set(terms))
    finally:
        conn.close()

def relevant(title,source,config):
    low=title.lower()
    if any(x.lower() in low for x in config.get("exclude_terms",[])): return False
    if source.get("source_scope") in {"club","fc_twente_page"}: return True
    if any(x.lower() in low for x in config.get("club_terms",[])): return True
    return any(term in low for term in person_terms())

def fetch_site(source,config):
    r=requests.get(source["url"],headers={"User-Agent":USER_AGENT,"Accept-Language":"nl-NL,nl;q=0.9"},timeout=int(config.get("request_timeout_seconds",15)))
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    now=datetime.now().astimezone().isoformat(timespec="seconds")
    found={}
    for link in soup.find_all("a",href=True):
        href=str(link.get("href","")).strip()
        if not href or href.startswith(("mailto:","javascript:","#")): continue
        absolute=urldefrag(urljoin(source["url"],href))[0]
        if not domain_allowed(absolute,source.get("allowed_domains",[])): continue
        path=urlparse(absolute).path
        parts=source.get("article_url_contains",[])
        if parts and not any(p in path for p in parts): continue
        title=normalize_title(link.get_text(" ",strip=True))
        if len(title)<8:
            img=link.find("img")
            if img: title=normalize_title(img.get("alt","") or img.get("title",""))
        if len(title)<8 or len(title)>260 or not relevant(title,source,config): continue
        cat=classify(title); pri=int(source.get("priority",3))
        found[absolute]={"source":source["name"],"title":title,"url":absolute,"found_at":now,
                         "priority":pri,"urgency":urgency(title,cat,pri),"category":cat,"fingerprint":fingerprint(title)}
    return list(found.values())

def has_history(conn,name):
    return conn.execute("SELECT COUNT(*) n FROM source_runs WHERE source=?",(name,)).fetchone()["n"]>0

def save_articles(conn,articles,baseline):
    new=[]
    for a in articles:
        try:
            conn.execute("""INSERT INTO articles(source,title,url,found_at,priority,urgency,category,fingerprint,is_baseline,status)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (a["source"],a["title"],a["url"],a["found_at"],a["priority"],a["urgency"],a["category"],a["fingerprint"],1 if baseline else 0,"Startbestand" if baseline else "Nieuw"))
            if not baseline:new.append(a)
        except sqlite3.IntegrityError: pass
    conn.commit(); return new

def record_run(conn,source,relevant_count,new_count,ok,message=""):
    conn.execute("INSERT INTO source_runs(source,checked_at,relevant_count,new_count,ok,message) VALUES(?,?,?,?,?,?)",
                 (source,datetime.now().astimezone().isoformat(timespec="seconds"),relevant_count,new_count,1 if ok else 0,message[:500]))
    conn.commit()

def run_check():
    if not CHECK_LOCK.acquire(blocking=False): return {"ok":False,"message":"Er loopt al een controle."}
    STATUS["running"]=True; STATUS["last_started"]=datetime.now().astimezone().isoformat(timespec="seconds")
    total_new=0
    try:
        config=load_config(); conn=connect_db()
        try:
            for source in config["sources"]:
                if not source.get("enabled",True): continue
                try:
                    arts=fetch_site(source,config)
                    baseline=(not has_history(conn,source["name"]) and config.get("first_run_silent",True))
                    new=save_articles(conn,arts,baseline)
                    record_run(conn,source["name"],len(arts),len(new),True)
                    total_new+=len(new)
                    logging.info("%s: %d relevant, %d nieuw.",source["name"],len(arts),len(new))
                except Exception as exc:
                    record_run(conn,source["name"],0,0,False,str(exc))
                    logging.warning("Bron %s tijdelijk overgeslagen: %s", source["name"], exc)
        finally: conn.close()
        STATUS["last_finished"]=datetime.now().astimezone().isoformat(timespec="seconds")
        return {"ok":True,"new":total_new}
    finally:
        STATUS["running"]=False; CHECK_LOCK.release()

def scheduler_loop(stop_event):
    while not stop_event.is_set():
        interval=max(1,int(load_config().get("check_interval_minutes",10)))
        run_check()
        STATUS["next_check"]=(datetime.now().astimezone()+timedelta(minutes=interval)).isoformat(timespec="seconds")
        stop_event.wait(interval*60)

def cutoff(hours): return (datetime.now().astimezone()-timedelta(hours=hours)).isoformat(timespec="seconds")

def group_similar(rows):
    groups=[]
    for row in rows:
        ts=tokens(row["title"]); placed=False
        for g in groups:
            union=ts|g["tokens"]; score=len(ts&g["tokens"])/len(union) if union else 0
            if score>=0.46:
                g["items"].append(row); g["tokens"]|=ts; g["urgency"]=max(g["urgency"],row["urgency"]); placed=True; break
        if not placed: groups.append({"items":[row],"tokens":set(ts),"urgency":row["urgency"]})
    out=[]
    for g in groups:
        items=sorted(g["items"],key=lambda x:(x["urgency"],x["priority"],x["id"]),reverse=True)
        out.append({"primary":items[0],"items":items,"source_count":len({x["source"] for x in items}),"urgency":g["urgency"]})
    return sorted(out,key=lambda g:(g["urgency"],g["primary"]["found_at"]),reverse=True)

def get_groups(hours=48):
    conn=connect_db()
    try:
        rows=conn.execute("SELECT * FROM articles WHERE found_at>=? AND is_baseline=0 ORDER BY urgency DESC,id DESC",(cutoff(hours),)).fetchall()
        return group_similar(rows)
    finally: conn.close()

def sources_status():
    config=load_config(); conn=connect_db(); out=[]
    try:
        for s in config["sources"]:
            last=conn.execute("SELECT * FROM source_runs WHERE source=? ORDER BY id DESC LIMIT 1",(s["name"],)).fetchone()
            out.append({"config":s,"last":last})
        return out
    finally: conn.close()

def list_persons(q="", role="", active="", follow=""):
    conn=connect_db()
    try:
        sql="SELECT * FROM persons WHERE 1=1"; params=[]
        if q: sql+=" AND lower(name) LIKE ?"; params.append(f"%{q.lower()}%")
        if role: sql+=" AND role=?"; params.append(role)
        if active!="": sql+=" AND active=?"; params.append(int(active))
        if follow!="": sql+=" AND follow=?"; params.append(int(follow))
        sql+=" ORDER BY role,name"
        return conn.execute(sql,params).fetchall()
    finally: conn.close()

def add_person(name,role,active,follow):
    name=normalize_title(name)
    if not name: raise ValueError("Naam ontbreekt.")
    conn=connect_db()
    try:
        conn.execute("INSERT INTO persons(name,role,active,follow) VALUES(?,?,?,?)",(name,role,1 if active else 0,1 if follow else 0))
        conn.commit()
    finally: conn.close()

def update_person(person_id,role,active,follow):
    conn=connect_db()
    try:
        conn.execute("UPDATE persons SET role=?,active=?,follow=? WHERE id=?",(role,1 if active else 0,1 if follow else 0,person_id))
        conn.commit()
    finally: conn.close()

def delete_person(person_id):
    conn=connect_db()
    try:
        conn.execute("DELETE FROM persons WHERE id=?",(person_id,))
        conn.commit()
    finally: conn.close()

def bulk_add_persons(text,default_role="Speler"):
    names=[]
    for line in text.splitlines():
        name=normalize_title(line)
        if name and name.lower() not in {"naam","speler","trainer","staf"}:
            names.append(name)
    conn=connect_db(); added=0; skipped=0
    try:
        for name in names:
            try:
                conn.execute("INSERT INTO persons(name,role,active,follow) VALUES(?,?,1,1)",(name,default_role))
                added+=1
            except sqlite3.IntegrityError:
                skipped+=1
        conn.commit()
    finally: conn.close()
    return added,skipped
