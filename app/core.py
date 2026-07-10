from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "tukkerscout.db"
CONFIG_PATH = BASE_DIR / "config.json"
X_CONFIG_PATH = DATA_DIR / "x_config.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36 TukkerScout/3.2"
)

CHECK_LOCK = threading.Lock()
STATUS = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_error": None,
}

CATEGORY_RULES = [
    ("Transfer", ["transfer", "akkoord", "belangstelling", "bod", "huur", "vertrek", "tekent", "transfervrij", "medische keuring"]),
    ("Blessure", ["blessure", "geblesseerd", "revalidatie", "hamstring", "knie", "enkel", "niet inzetbaar"]),
    ("Contract", ["contract", "verlengt", "verlenging", "optie gelicht", "verbintenis"]),
    ("Europa", ["europa league", "conference league", "uefa", "europese", "voorronde", "loting"]),
    ("Wedstrijd", ["wedstrijd", "opstelling", "basiself", "selectie", "uitslag", "oefenduel", "voorbeschouwing", "nabeschouwing"]),
    ("Interview", ["zegt", "vertelt", "interview", "reactie", "spreekt", "persconferentie"]),
    ("Clubnieuws", ["directeur", "staf", "trainer", "technische staf", "clubleiding", "bestuur"]),
]

STOPWORDS = {
    "de","het","een","en","van","voor","met","naar","bij","op","in","uit","is","zijn",
    "dat","dit","die","als","om","te","na","over","fc","twente","tukkers","nieuws",
    "krijgt","heeft","komt","kan","wordt","wil","weer","tegen","door"
}

GENERIC_NOISE_TITLES = {
    "nieuws", "meer nieuws", "laatste nieuws", "net binnen", "lees meer",
    "video", "videos", "podcast", "home", "sport", "voetbal", "clubs",
    "wedstrijden", "stand", "selectie", "programma", "uitslagen"
}

FOOTBALL_CONTEXT_TERMS = {
    "transfer", "contract", "blessure", "wedstrijd", "trainer", "speler",
    "selectie", "eredivisie", "knvb", "uefa", "europa", "oefenduel",
    "training", "opstelling", "doelpunt", "spits", "keeper", "verdediger",
    "middenvelder", "club", "technisch directeur", "schorsing"
}


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "tukkerscout.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_x_config():
    DATA_DIR.mkdir(exist_ok=True)
    if not X_CONFIG_PATH.exists():
        return {"bearer_token": "", "accounts": []}
    try:
        return json.loads(X_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"bearer_token": "", "accounts": []}


def save_x_config(bearer_token, accounts):
    DATA_DIR.mkdir(exist_ok=True)
    cleaned = []
    seen = set()
    for raw in accounts:
        handle = re.sub(r"[^A-Za-z0-9_]", "", raw.lstrip("@").strip())
        if handle and handle.lower() not in seen:
            cleaned.append(handle)
            seen.add(handle.lower())
    X_CONFIG_PATH.write_text(
        json.dumps(
            {"bearer_token": bearer_token.strip(), "accounts": cleaned},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def connect_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("""CREATE TABLE IF NOT EXISTS articles(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      title TEXT NOT NULL,
      url TEXT NOT NULL UNIQUE,
      found_at TEXT NOT NULL,
      published_at TEXT,
      priority INTEGER NOT NULL DEFAULT 3,
      urgency INTEGER NOT NULL DEFAULT 2,
      category TEXT NOT NULL DEFAULT 'Algemeen',
      fingerprint TEXT NOT NULL,
      is_baseline INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'Nieuw',
      is_favorite INTEGER NOT NULL DEFAULT 0,
      check_id INTEGER,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS source_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      check_id INTEGER,
      source TEXT NOT NULL,
      checked_at TEXT NOT NULL,
      relevant_count INTEGER NOT NULL DEFAULT 0,
      new_count INTEGER NOT NULL DEFAULT 0,
      ok INTEGER NOT NULL DEFAULT 1,
      message TEXT
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS persons(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      role TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      follow INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS checks(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      new_count INTEGER NOT NULL DEFAULT 0
    )""")

    # Migrate databases from 3.0 without deleting existing data.
    article_cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if "published_at" not in article_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN published_at TEXT")
    if "check_id" not in article_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN check_id INTEGER")
    if "match_reason" not in article_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN match_reason TEXT")
    if "reliability" not in article_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN reliability INTEGER NOT NULL DEFAULT 3")
    if "source_type" not in article_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN source_type TEXT NOT NULL DEFAULT 'Website'")

    run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(source_runs)")}
    if "check_id" not in run_cols:
        conn.execute("ALTER TABLE source_runs ADD COLUMN check_id INTEGER")

    conn.commit()
    return conn


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_title(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fingerprint(title):
    normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def tokens(title):
    return {
        word for word in re.findall(r"[a-z0-9À-ÿ]+", title.lower())
        if len(word) > 2 and word not in STOPWORDS
    }


def classify(title):
    lowered = title.lower()
    for category, words in CATEGORY_RULES:
        if any(word in lowered for word in words):
            return category
    return "Algemeen"


def urgency(title, category, priority):
    lowered = title.lower()
    if any(word in lowered for word in [
        "officieel", "akkoord", "tekent", "bevestigt",
        "ernstige blessure", "opstelling"
    ]):
        return 5
    if category in {"Transfer", "Contract", "Blessure", "Europa"}:
        return min(5, max(3, priority))
    if category in {"Wedstrijd", "Clubnieuws"}:
        return min(4, max(2, priority - 1))
    return min(3, max(1, priority - 2))


def domain_allowed(url, domains):
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == domain.lower() or host.endswith("." + domain.lower())
        for domain in domains
    )


def person_terms():
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT name FROM persons WHERE active=1 AND follow=1"
        ).fetchall()
    finally:
        conn.close()

    result = set()
    for row in rows:
        name = row["name"].strip()
        if not name:
            continue
        result.add(name.lower())
        parts = name.split()
        if len(parts) >= 2:
            surname = (
                " ".join(parts[-2:])
                if parts[-2].lower() in {"van", "de", "der", "den", "ten", "ter"}
                else parts[-1]
            )
            if len(surname) >= 4:
                result.add(surname.lower())
    return sorted(result)


def relevance_details(title, source, config):
    lowered = normalize_title(title).lower()

    if not lowered or lowered in GENERIC_NOISE_TITLES:
        return False, "Algemene navigatietekst"

    if any(term.lower() in lowered for term in config.get("exclude_terms", [])):
        return False, "Uitgesloten regionaal onderwerp"

    # Alleen de officiële clubsite mag een titel zonder club- of persoonsnaam leveren.
    if source.get("source_scope") == "club":
        return True, "Officiële FC Twente-bron"

    club_matches = [
        term for term in config.get("club_terms", [])
        if term.lower() in lowered
    ]
    if club_matches:
        return True, f"FC Twente-term: {club_matches[0]}"

    person_matches = [term for term in person_terms() if term in lowered]
    if person_matches:
        return True, f"Persoon: {person_matches[0]}"

    return False, "Geen FC Twente-term of gevolgde persoon"


def relevant(title, source, config):
    return relevance_details(title, source, config)[0]


def fetch_site(source, config):
    timeout = int(config.get("request_timeout_seconds", 15))
    response = requests.get(
        source["url"],
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl-NL,nl;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    found = {}

    for link in soup.find_all("a", href=True):
        href = str(link.get("href", "")).strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue

        absolute = urldefrag(urljoin(source["url"], href))[0]
        if not domain_allowed(absolute, source.get("allowed_domains", [])):
            continue

        path = urlparse(absolute).path
        parts = source.get("article_url_contains", [])
        if parts and not any(part in path for part in parts):
            continue

        title = normalize_title(link.get_text(" ", strip=True))
        if len(title) < 8:
            image = link.find("img")
            if image:
                title = normalize_title(
                    image.get("alt", "") or image.get("title", "")
                )

        if len(title) < 8 or len(title) > 260:
            continue
        is_relevant, match_reason = relevance_details(title, source, config)
        if not is_relevant:
            continue

        category = classify(title)
        priority = int(source.get("priority", 3))
        found[absolute] = {
            "source": source["name"],
            "title": title,
            "url": absolute,
            "found_at": now_iso(),
            "published_at": None,
            "priority": priority,
            "urgency": urgency(title, category, priority),
            "category": category,
            "fingerprint": fingerprint(title),
            "match_reason": match_reason,
            "reliability": int(source.get("reliability", priority)),
            "source_type": "Website",
        }

    return list(found.values())


def latest_finished_check_time(conn):
    row = conn.execute(
        "SELECT finished_at FROM checks "
        "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["finished_at"] if row else None


def fetch_x_posts(last_check_time):
    x_config = load_x_config()
    token = x_config.get("bearer_token", "").strip()
    accounts = x_config.get("accounts", [])

    if not token or not accounts:
        return [], "X is nog niet ingesteld."

    from_parts = [f"from:{handle}" for handle in accounts]
    query = "(" + " OR ".join(from_parts) + ") -is:retweet"

    params = {
        "query": query,
        "max_results": 100,
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }

    if last_check_time:
        last_dt = datetime.fromisoformat(last_check_time)
        oldest = datetime.now(timezone.utc) - timedelta(days=7)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        last_utc = last_dt.astimezone(timezone.utc)
        start = max(last_utc - timedelta(seconds=5), oldest)
        params["start_time"] = start.isoformat(timespec="seconds").replace("+00:00", "Z")

    response = requests.get(
        "https://api.x.com/2/tweets/search/recent",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    users = {
        user["id"]: user
        for user in payload.get("includes", {}).get("users", [])
    }

    posts = []
    for post in payload.get("data", []):
        user = users.get(post.get("author_id"), {})
        username = user.get("username", "unknown")
        text = normalize_title(post.get("text", ""))
        if not text:
            continue

        x_source = {"source_scope": "strict"}
        is_relevant, match_reason = relevance_details(text, x_source, load_config())
        if not is_relevant:
            continue

        category = classify(text)
        posts.append({
            "source": f"X · @{username}",
            "title": text,
            "url": f"https://x.com/{username}/status/{post['id']}",
            "found_at": now_iso(),
            "published_at": post.get("created_at"),
            "priority": 4,
            "urgency": urgency(text, category, 4),
            "category": category,
            "fingerprint": fingerprint(text),
            "match_reason": match_reason,
            "reliability": 4,
            "source_type": "X",
        })

    return posts, ""


def has_source_history(conn, source_name):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM source_runs WHERE source=?",
        (source_name,),
    ).fetchone()
    return bool(row and row["n"] > 0)


def save_articles(conn, articles, baseline, check_id):
    new_items = []
    for article in articles:
        try:
            conn.execute("""INSERT INTO articles(
                source,title,url,found_at,published_at,priority,urgency,
                category,fingerprint,is_baseline,status,check_id,
                match_reason,reliability,source_type
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                article["source"],
                article["title"],
                article["url"],
                article["found_at"],
                article.get("published_at"),
                article["priority"],
                article["urgency"],
                article["category"],
                article["fingerprint"],
                1 if baseline else 0,
                "Startbestand" if baseline else "Nieuw",
                check_id,
                article.get("match_reason", ""),
                int(article.get("reliability", article.get("priority", 3))),
                article.get("source_type", "Website"),
            ))
            if not baseline:
                new_items.append(article)
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return new_items


def record_run(conn, check_id, source, relevant_count, new_count, ok, message=""):
    conn.execute("""INSERT INTO source_runs(
        check_id,source,checked_at,relevant_count,new_count,ok,message
    ) VALUES(?,?,?,?,?,?,?)""", (
        check_id,
        source,
        now_iso(),
        relevant_count,
        new_count,
        1 if ok else 0,
        message[:500],
    ))
    conn.commit()


def run_check():
    if not CHECK_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "Er loopt al een nieuwsupdate."}

    STATUS["running"] = True
    STATUS["last_started"] = now_iso()
    STATUS["last_error"] = None

    conn = connect_db()
    check_id = conn.execute(
        "INSERT INTO checks(started_at,new_count) VALUES(?,0)",
        (STATUS["last_started"],),
    ).lastrowid
    conn.commit()

    total_new = 0
    results = []

    try:
        config = load_config()
        cleanup_irrelevant_articles()
        previous_check = latest_finished_check_time(conn)

        for source in config.get("sources", []):
            if not source.get("enabled", True):
                continue
            try:
                articles = fetch_site(source, config)
                baseline = (
                    not has_source_history(conn, source["name"])
                    and bool(config.get("first_run_silent", True))
                )
                new_items = save_articles(conn, articles, baseline, check_id)
                total_new += len(new_items)
                record_run(
                    conn, check_id, source["name"],
                    len(articles), len(new_items), True
                )
                results.append({
                    "source": source["name"],
                    "ok": True,
                    "new": len(new_items),
                })
            except requests.RequestException as exc:
                record_run(conn, check_id, source["name"], 0, 0, False, str(exc))
                results.append({
                    "source": source["name"],
                    "ok": False,
                    "message": str(exc),
                })

        # X is checked only when explicitly configured.
        try:
            x_posts, x_message = fetch_x_posts(previous_check)
            if x_message:
                results.append({"source": "X", "ok": False, "message": x_message})
            else:
                # First X use is baseline, so old posts do not trigger as new.
                x_baseline = not has_source_history(conn, "X")
                new_x = save_articles(conn, x_posts, x_baseline, check_id)
                total_new += len(new_x)
                record_run(conn, check_id, "X", len(x_posts), len(new_x), True)
                results.append({"source": "X", "ok": True, "new": len(new_x)})
        except requests.RequestException as exc:
            record_run(conn, check_id, "X", 0, 0, False, str(exc))
            results.append({"source": "X", "ok": False, "message": str(exc)})

        finished = now_iso()
        conn.execute(
            "UPDATE checks SET finished_at=?,new_count=? WHERE id=?",
            (finished, total_new, check_id),
        )
        conn.commit()
        STATUS["last_finished"] = finished

        return {
            "ok": True,
            "new": total_new,
            "check_id": check_id,
            "sources": results,
        }

    except Exception as exc:
        STATUS["last_error"] = str(exc)
        logging.exception("Nieuwsupdate mislukt.")
        return {"ok": False, "message": str(exc)}
    finally:
        conn.close()
        STATUS["running"] = False
        CHECK_LOCK.release()


def cleanup_irrelevant_articles():
    """Remove previously stored noise using the current strict filter."""
    config = load_config()
    source_map = {source["name"]: source for source in config.get("sources", [])}
    conn = connect_db()
    removed = 0
    try:
        rows = conn.execute("SELECT id,source,title FROM articles").fetchall()
        for row in rows:
            source_name = row["source"]
            if source_name.startswith("X ·"):
                source = {"source_scope": "strict"}
            else:
                source = source_map.get(source_name, {"source_scope": "strict"})
            keep, _ = relevance_details(row["title"], source, config)
            if not keep:
                conn.execute("DELETE FROM articles WHERE id=?", (row["id"],))
                removed += 1
        conn.commit()
    finally:
        conn.close()
    return removed


def latest_check_runs(check_id):
    if not check_id:
        return []
    conn = connect_db()
    try:
        return conn.execute(
            "SELECT * FROM source_runs WHERE check_id=? ORDER BY id",
            (check_id,),
        ).fetchall()
    finally:
        conn.close()


def latest_check():
    conn = connect_db()
    try:
        return conn.execute(
            "SELECT * FROM checks ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def cutoff(hours):
    return (
        datetime.now().astimezone() - timedelta(hours=hours)
    ).isoformat(timespec="seconds")


def group_similar(rows):
    groups = []
    for row in rows:
        row_tokens = tokens(row["title"])
        placed = False
        for group in groups:
            union = row_tokens | group["tokens"]
            score = len(row_tokens & group["tokens"]) / len(union) if union else 0
            if score >= 0.46:
                group["items"].append(row)
                group["tokens"] |= row_tokens
                group["urgency"] = max(group["urgency"], row["urgency"])
                placed = True
                break
        if not placed:
            groups.append({
                "items": [row],
                "tokens": set(row_tokens),
                "urgency": row["urgency"],
            })

    result = []
    for group in groups:
        items = sorted(
            group["items"],
            key=lambda item: (
                item["urgency"],
                item["published_at"] or item["found_at"],
                item["id"],
            ),
            reverse=True,
        )
        result.append({
            "primary": items[0],
            "items": items,
            "source_count": len({item["source"] for item in items}),
            "urgency": group["urgency"],
        })
    return sorted(
        result,
        key=lambda group: (
            group["urgency"],
            group["primary"]["published_at"] or group["primary"]["found_at"],
        ),
        reverse=True,
    )


def get_groups(hours=48, check_id=None):
    conn = connect_db()
    try:
        sql = "SELECT * FROM articles WHERE found_at>=?"
        params = [cutoff(hours)]
        if check_id is not None:
            sql += " AND check_id=? AND is_baseline=0"
            params.append(check_id)
        sql += " ORDER BY urgency DESC,id DESC"
        return group_similar(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def sources_status():
    config = load_config()
    conn = connect_db()
    output = []
    try:
        for source in config.get("sources", []):
            last = conn.execute(
                "SELECT * FROM source_runs WHERE source=? ORDER BY id DESC LIMIT 1",
                (source["name"],),
            ).fetchone()
            output.append({"config": source, "last": last})

        x_last = conn.execute(
            "SELECT * FROM source_runs WHERE source='X' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        x_cfg = load_x_config()
        output.append({
            "config": {
                "name": "X",
                "url": "https://x.com",
                "enabled": bool(x_cfg.get("bearer_token") and x_cfg.get("accounts")),
            },
            "last": x_last,
        })
    finally:
        conn.close()
    return output


def list_persons(q="", role="", active="", follow=""):
    conn = connect_db()
    try:
        sql = "SELECT * FROM persons WHERE 1=1"
        params = []
        if q:
            sql += " AND lower(name) LIKE ?"
            params.append(f"%{q.lower()}%")
        if role:
            sql += " AND role=?"
            params.append(role)
        if active != "":
            sql += " AND active=?"
            params.append(int(active))
        if follow != "":
            sql += " AND follow=?"
            params.append(int(follow))
        sql += " ORDER BY role,name"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def count_persons():
    conn = connect_db()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
    finally:
        conn.close()


def add_person(name, role, active, follow):
    name = normalize_title(name)
    if not name:
        raise ValueError("Naam ontbreekt.")
    conn = connect_db()
    try:
        conn.execute(
            "INSERT INTO persons(name,role,active,follow) VALUES(?,?,?,?)",
            (name, role, 1 if active else 0, 1 if follow else 0),
        )
        conn.commit()
    finally:
        conn.close()


def update_person(person_id, role, active, follow):
    conn = connect_db()
    try:
        conn.execute(
            "UPDATE persons SET role=?,active=?,follow=? WHERE id=?",
            (role, 1 if active else 0, 1 if follow else 0, person_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_person(person_id):
    conn = connect_db()
    try:
        conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
        conn.commit()
    finally:
        conn.close()


def parse_names(text):
    names = []
    seen = set()
    for line in text.splitlines():
        name = normalize_title(line)
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            names.append(name)
            seen.add(key)
    return names


def bulk_add_persons(text):
    names = parse_names(text)
    added = skipped = 0
    conn = connect_db()
    try:
        for name in names:
            try:
                conn.execute(
                    "INSERT INTO persons(name,role,active,follow) "
                    "VALUES(?,'Speler',1,1)",
                    (name,),
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return added, skipped


def compare_player_selection(text):
    incoming = parse_names(text)
    incoming_map = {name.casefold(): name for name in incoming}
    conn = connect_db()
    try:
        current = [
            row["name"] for row in conn.execute(
                "SELECT name FROM persons WHERE role='Speler' AND active=1"
            ).fetchall()
        ]
    finally:
        conn.close()
    current_map = {name.casefold(): name for name in current}
    return {
        "new": sorted(incoming_map[k] for k in incoming_map.keys() - current_map.keys()),
        "missing": sorted(current_map[k] for k in current_map.keys() - incoming_map.keys()),
        "unchanged": sorted(incoming_map[k] for k in incoming_map.keys() & current_map.keys()),
        "incoming": incoming,
    }


def apply_player_selection(text):
    result = compare_player_selection(text)
    incoming_keys = {name.casefold() for name in result["incoming"]}
    conn = connect_db()
    try:
        for name in result["new"]:
            try:
                conn.execute(
                    "INSERT INTO persons(name,role,active,follow) "
                    "VALUES(?,'Speler',1,1)",
                    (name,),
                )
            except sqlite3.IntegrityError:
                conn.execute(
                    "UPDATE persons SET role='Speler',active=1,follow=1 "
                    "WHERE lower(name)=lower(?)",
                    (name,),
                )
        for row in conn.execute(
            "SELECT id,name FROM persons WHERE role='Speler' AND active=1"
        ).fetchall():
            if row["name"].casefold() not in incoming_keys:
                conn.execute(
                    "UPDATE persons SET active=0 WHERE id=?",
                    (row["id"],),
                )
        conn.commit()
    finally:
        conn.close()
    return result
