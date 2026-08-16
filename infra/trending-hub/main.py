"""
Seraphim Studio — Trending Hub.

One shared sync of Instagram account data (HikerAPI for any account, Meta
Graph API for the token owner's account) that every desktop install and every
VA's browser reads from. HikerAPI / Meta are called ONLY from here, on a
schedule, once — extra readers cost zero upstream requests. API keys live on
this box (settings table), never on client machines.

Auth: `Authorization: Bearer <token>`.
  admin  — everything (add/remove accounts, sync, settings, mint viewer tokens)
  viewer — read state, toggle favorites/pins, export CSV
Admin token is bootstrapped from /opt/trending-hub/admin_token at first start
(deploy.sh generates it). Viewer tokens are minted by the admin via the API.

Endpoints (all JSON unless noted):
  GET  /health                               no auth
  GET  /trending/state                       viewer  → full snapshot for clients
  POST /trending/accounts {handle, source}   admin   → add + backfill (background)
  DELETE /trending/accounts/{pk}             admin
  POST /trending/accounts/{pk}/pin {on}      viewer
  POST /trending/favorites {key, on}         viewer
  POST /trending/sync {pk?, deep?}           admin   → background sync (rate limited)
  GET  /trending/sync/status                 viewer
  GET  /trending/settings                    admin   (keys masked)
  PUT  /trending/settings {hikerKey?, metaToken?, cadence?}  admin
  POST /trending/settings/test {source}      admin
  GET  /trending/tokens                      admin
  POST /trending/tokens {name}               admin   → token shown once
  DELETE /trending/tokens/{id}               admin
  POST /trending/import {accounts,reels,snaps,favs}  admin  → merge client's local state
  GET  /trending/export.csv?pk=              viewer  (text/csv)
  GET  /trending/report.html                 no auth (page fetches /state with token)

Deployed to the droplet by infra/trending-hub/deploy.sh (venv, systemd
`trending-hub`, port 8790, ufw). Same host as the render server, separate
service — restarting one never touches the other.
"""
import json, os, secrets, sqlite3, threading, time, csv, io, hashlib
from typing import Optional
from pathlib import Path
from urllib import request as urlreq, parse as urlparse, error as urlerr
from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

HUB_VERSION = "1.2.1"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
BASE = Path(os.environ.get("TRENDING_HUB_DIR", "/opt/trending-hub"))
DB_PATH = BASE / "hub.db"
ADMIN_TOKEN_FILE = BASE / "admin_token"
REPORT_HTML = BASE / "report.html"

HIKER_BASE = "https://api.hikerapi.com"
META_BASE = "https://graph.instagram.com"
META_V = "/v23.0"
# Test hooks: point upstreams at a local mock server.
if os.environ.get("TRENDING_HUB_HIKER_BASE"): HIKER_BASE = os.environ["TRENDING_HUB_HIKER_BASE"]
if os.environ.get("TRENDING_HUB_META_BASE"): META_BASE = os.environ["TRENDING_HUB_META_BASE"]

app = FastAPI(title="Seraphim Studio Trending Hub")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------- storage

_db_lock = threading.RLock()

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    BASE.mkdir(parents=True, exist_ok=True)
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, hash TEXT UNIQUE, name TEXT, role TEXT,
            created REAL, last_used REAL, revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS accounts (pk TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS reels (pk TEXT, id TEXT, data TEXT, PRIMARY KEY (pk, id));
        CREATE TABLE IF NOT EXISTS snaps (pk TEXT, t REAL, followers INTEGER, seeded INTEGER DEFAULT 0, PRIMARY KEY (pk, t));
        CREATE TABLE IF NOT EXISTS favs (key TEXT PRIMARY KEY, created REAL);
        CREATE TABLE IF NOT EXISTS log (t REAL, actor TEXT, action TEXT, detail TEXT);
        """)
    # Bootstrap admin token from file (deploy.sh writes it).
    if ADMIN_TOKEN_FILE.exists():
        tok = ADMIN_TOKEN_FILE.read_text().strip()
        if tok:
            with _db_lock, db() as c:
                if not c.execute("SELECT 1 FROM tokens WHERE hash=?", (_hash(tok),)).fetchone():
                    c.execute("INSERT INTO tokens (id, hash, name, role, created) VALUES (?,?,?,?,?)",
                              (secrets.token_hex(6), _hash(tok), "admin", "admin", time.time()))

def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()

def get_setting(k, default=None):
    with _db_lock, db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return r["value"] if r else default

def set_setting(k, v):
    with _db_lock, db() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))

def log(actor, action, detail=""):
    with _db_lock, db() as c:
        c.execute("INSERT INTO log VALUES (?,?,?,?)", (time.time(), actor, action, detail[:500]))

# ---------------------------------------------------------------- auth

def auth(authorization: Optional[str], need: str = "viewer"):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    tok = authorization[7:].strip()
    with _db_lock, db() as c:
        row = c.execute("SELECT * FROM tokens WHERE hash=? AND revoked=0", (_hash(tok),)).fetchone()
        if not row:
            raise HTTPException(401, "Invalid or revoked token")
        c.execute("UPDATE tokens SET last_used=? WHERE id=?", (time.time(), row["id"]))
    if need == "admin" and row["role"] != "admin":
        raise HTTPException(403, "Admin token required")
    return {"id": row["id"], "name": row["name"], "role": row["role"]}

# ---------------------------------------------------------------- upstream HTTP

def _get_json(url, headers, params=None, timeout=30):
    if params:
        url = url + ("&" if "?" in url else "?") + urlparse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    # Cloudflare in front of api.hikerapi.com rejects urllib's default UA
    # (error 1010 "browser signature") — send a normal browser UA.
    req = urlreq.Request(url, headers={"accept": "application/json", "user-agent": UA, "accept-language": "en-US,en;q=0.9", **headers})
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urlerr.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        try:
            j = json.loads(body); msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error") or body
        except Exception:
            msg = body
        raise RuntimeError(f"HTTP {e.code}: {msg}")
    except urlerr.URLError as e:
        raise RuntimeError(f"network: {e.reason}")

def hiker(path, params):
    key = get_setting("hiker_key")
    if not key: raise RuntimeError("No HikerAPI key configured on hub")
    return _get_json(HIKER_BASE + path, {"x-access-key": key}, params)

def meta(path, params):
    tok = get_setting("meta_token")
    if not tok: raise RuntimeError("No Meta token configured on hub")
    return _get_json(META_BASE + path, {"authorization": "Bearer " + tok}, params)

# ---------------------------------------------------------------- normalizers (mirror the app's JS)

def fmt_num(n):
    try: n = float(n or 0)
    except Exception: n = 0
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K" if n < 1e4 else f"{n/1e3:.0f}K"
    return str(int(n))

def norm_hiker_user(j):
    u = (j.get("user") if isinstance(j, dict) else None) or ((j.get("response") or {}).get("user") if isinstance(j, dict) else None) or j or {}
    return {"pk": str(u.get("pk") or u.get("pk_id") or u.get("id") or ""), "username": u.get("username") or "",
            "fullName": u.get("full_name") or "", "followers": u.get("follower_count") or 0,
            "following": u.get("following_count") or 0, "mediaCount": u.get("media_count") or 0,
            "isPrivate": bool(u.get("is_private"))}

def hiker_items(j):
    if isinstance(j, list): return j
    if not isinstance(j, dict): return []
    return j.get("items") or (j.get("response") or {}).get("items") or j.get("medias") or []

def _hiker_media_urls(m):
    cand = ((m.get("image_versions2") or {}).get("candidates") or [None])[0]
    img = m.get("thumbnail_url") or (cand or {}).get("url") or m.get("display_url") or None
    vv = (m.get("video_versions") or [None])[0]
    vid = m.get("video_url") or ((vv or {}).get("url") if vv else None)
    return img, vid

def norm_hiker_clip(it, kind="reel"):
    """kind: 'reel' (from /user/clips — trial heuristic applies) or 'media' (from /user/medias)."""
    m = (it.get("media") if isinstance(it, dict) and isinstance(it.get("media"), dict) else it) or {}
    mt = m.get("media_type")            # 1 photo · 2 video · 8 carousel
    ptype = (m.get("product_type") or "").lower()
    thumb, video = _hiker_media_urls(m)
    typ = "reel"
    if kind == "media":
        if mt == 8: typ = "carousel"
        elif mt == 1: typ = "post"
        elif mt == 2 and ptype not in ("clips", "reel", "reels"): typ = "video"
    media = []
    if typ == "carousel":
        for c in m.get("carousel_media") or []:
            ci, cv = _hiker_media_urls(c)
            media.append({"img": ci, "video": cv})
        if not thumb and media: thumb = media[0]["img"]
    views = m.get("play_count") if m.get("play_count") is not None else (m.get("view_count") or 0)
    ig_views = m.get("ig_play_count") if m.get("ig_play_count") is not None else views
    fb = m.get("fb_play_count") if m.get("fb_play_count") is not None else max(0, (views or 0) - (ig_views or 0))
    return {"id": str(m.get("pk") or m.get("id") or ""), "code": m.get("code") or "", "type": typ,
            "ts": (m.get("taken_at") or 0) * 1000, "views": views or 0, "igViews": ig_views or 0,
            "fb": fb or 0, "likes": m.get("like_count") or 0,
            "comments": m.get("comment_count") or 0, "shares": m.get("reshare_count") or m.get("share_count") or 0,
            "saves": m.get("save_count"), "fbLikes": m.get("fb_like_count"), "fbComments": m.get("fb_comment_count"),
            "duration": m.get("video_duration") or 0, "inGrid": m.get("is_in_profile_grid"),
            "thumb": thumb, "videoUrl": video, "media": media,
            "trial": (kind == "reel" and not thumb), "source": "hiker",
            "caption": ((m.get("caption") or {}).get("text") if isinstance(m.get("caption"), dict) else m.get("caption_text")) or ""}

def norm_meta_user(j):
    j = j or {}
    return {"pk": "meta:" + str(j.get("id") or j.get("user_id") or ""), "username": j.get("username") or "",
            "fullName": j.get("name") or "", "followers": j.get("followers_count") or 0,
            "following": j.get("follows_count") or 0, "mediaCount": j.get("media_count") or 0, "isPrivate": False}

def norm_meta_media(m):
    m = m or {}
    perm = m.get("permalink") or ""
    code = [p for p in perm.split("/") if p][-1] if perm else ""
    ts = 0
    if m.get("timestamp"):
        try:
            from datetime import datetime
            ts = int(datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00").replace("+0000", "+00:00")).timestamp() * 1000)
        except Exception: ts = 0
    mpt, mty = m.get("media_product_type"), m.get("media_type")
    typ = "reel" if mpt == "REELS" else "carousel" if mty == "CAROUSEL_ALBUM" else "video" if mty == "VIDEO" else "post"
    media = []
    if typ == "carousel":
        for c in ((m.get("children") or {}).get("data") or []):
            media.append({"img": c.get("thumbnail_url") or c.get("media_url"), "video": c.get("media_url") if c.get("media_type") == "VIDEO" else None})
    is_video = typ in ("reel", "video")
    return {"id": str(m.get("id") or ""), "code": code, "permalink": perm, "ts": ts, "type": typ,
            "views": 0, "igViews": 0, "fb": 0, "likes": m.get("like_count") or 0, "comments": m.get("comments_count") or 0,
            "shares": 0, "saves": 0, "reach": 0, "watch": 0,
            "thumb": m.get("thumbnail_url") or (media[0]["img"] if media else None) or (None if is_video else m.get("media_url")),
            "videoUrl": m.get("media_url") if is_video else None, "media": media,
            "trial": False, "source": "meta", "caption": (m.get("caption") or "")[:300]}

def apply_insights(r, ins):
    for d in (ins or {}).get("data") or []:
        vals = d.get("values") or []
        v = vals[0].get("value") if vals else ((d.get("total_value") or {}).get("value") or 0)
        n = d.get("name")
        if n == "views": r["views"] = v; r["igViews"] = v
        elif n == "saved": r["saves"] = v
        elif n == "shares": r["shares"] = v
        elif n == "reach": r["reach"] = v
        elif n == "ig_reels_avg_watch_time": r["watch"] = v
    return r

# ---------------------------------------------------------------- state helpers

def load_accounts():
    with _db_lock, db() as c:
        return [json.loads(r["data"]) for r in c.execute("SELECT data FROM accounts")]

def save_account(a):
    with _db_lock, db() as c:
        c.execute("INSERT INTO accounts (pk, data) VALUES (?,?) ON CONFLICT(pk) DO UPDATE SET data=excluded.data", (a["pk"], json.dumps(a)))

def load_reels(pk):
    with _db_lock, db() as c:
        return [json.loads(r["data"]) for r in c.execute("SELECT data FROM reels WHERE pk=?", (pk,))]

def save_reels(pk, reels):
    with _db_lock, db() as c:
        c.executemany("INSERT INTO reels (pk, id, data) VALUES (?,?,?) ON CONFLICT(pk,id) DO UPDATE SET data=excluded.data",
                      [(pk, r["id"], json.dumps(r)) for r in reels])

def add_snap(pk, t, followers, seeded=0):
    with _db_lock, db() as c:
        c.execute("INSERT OR IGNORE INTO snaps (pk, t, followers, seeded) VALUES (?,?,?,?)", (pk, t, followers, seeded))

def load_snaps(pk):
    with _db_lock, db() as c:
        return [{"t": r["t"], "followers": r["followers"], "seeded": bool(r["seeded"])}
                for r in c.execute("SELECT t, followers, seeded FROM snaps WHERE pk=? ORDER BY t", (pk,))]

# ---------------------------------------------------------------- sync

_sync = {"busy": False, "status": "", "last": 0, "lastError": ""}
_sync_lock = threading.Lock()

def _status(msg):
    _sync["status"] = msg

def sync_hiker(acc, deep):
    pk = acc["pk"]
    _status(f"Syncing @{acc['username']}…")
    u = norm_hiker_user(hiker("/v2/user/by/username", {"username": acc["username"]}))
    if u["followers"]:
        acc.update({k: u[k] for k in ("followers", "following", "mediaCount", "isPrivate", "fullName")})
        snaps = load_snaps(pk)
        if not snaps or time.time() * 1000 - snaps[-1]["t"] > 3600e3:
            add_snap(pk, int(time.time() * 1000), u["followers"])
    page_id, got, pages = None, [], 0
    max_pages = 60 if deep else 2          # deep = full backfill (≈12 reels/page → 720 cap)
    while True:
        params = {"user_id": pk}
        if page_id: params["page_id"] = page_id
        cj = hiker("/v2/user/clips", params)
        items = hiker_items(cj)
        got.extend(r for r in (norm_hiker_clip(i) for i in items) if r["id"])
        page_id = (cj.get("next_page_id") if isinstance(cj, dict) else None) or ((cj.get("response") or {}).get("next_page_id") if isinstance(cj, dict) else None)
        pages += 1
        _status(f"Syncing @{acc['username']} — page {pages} ({len(got)} reels)…")
        if not page_id or pages >= max_pages or not items: break
    # Posts + carousels from the profile feed. Reels also appear here; the
    # clips pull above stays authoritative for reels (it's the one carrying the
    # trial signal), so reels coming back from /medias are skipped.
    reel_ids = {r["id"] for r in got}
    page_id, mpages = None, 0
    max_mpages = 30 if deep else 1
    try:
        while True:
            params = {"user_id": pk}
            if page_id: params["page_id"] = page_id
            mj = hiker("/v2/user/medias", params)
            items = hiker_items(mj)
            for r in (norm_hiker_clip(i, "media") for i in items):
                if r["id"] and r["id"] not in reel_ids and r["type"] != "reel":
                    got.append(r)
            page_id = (mj.get("next_page_id") if isinstance(mj, dict) else None) or ((mj.get("response") or {}).get("next_page_id") if isinstance(mj, dict) else None)
            mpages += 1
            _status(f"Syncing @{acc['username']} — posts page {mpages}…")
            if not page_id or mpages >= max_mpages or not items: break
    except Exception as e:
        log("sync", "medias_warn", f"@{acc['username']}: {e}")
    save_reels(pk, got)
    allr = load_reels(pk)
    acc["trialCount"] = sum(1 for r in allr if r.get("trial"))
    solid = [r for r in allr if not r.get("trial") and r.get("type", "reel") == "reel"]
    acc["avgViews"] = round(sum(r.get("views", 0) for r in solid) / len(solid)) if solid else 0
    acc["lastSync"] = int(time.time() * 1000)
    save_account(acc)
    return len(got), pages + mpages

def sync_meta(acc, deep):
    pk = acc["pk"]
    _status(f"Syncing @{acc['username']} via Meta…")
    u = norm_meta_user(meta(META_V + "/me", {"fields": "id,username,name,followers_count,follows_count,media_count"}))
    if u["username"]:
        acc.update({k: u[k] for k in ("username", "fullName", "followers", "following", "mediaCount")})
    now_ms = int(time.time() * 1000)
    snaps = load_snaps(pk)
    if u["followers"] and (not snaps or now_ms - snaps[-1]["t"] > 3600e3):
        add_snap(pk, now_ms, u["followers"])
    # Seed history from follower_count/day (new followers per day, ≤30d), walking back from today's total.
    try:
        fi = meta(META_V + "/me/insights", {"metric": "follower_count", "period": "day"})
        vals = sorted(((fi.get("data") or [{}])[0].get("values") or []), key=lambda v: v.get("end_time", ""))
        if vals and u["followers"]:
            from datetime import datetime
            have = {int(s["t"] // 864e5) for s in load_snaps(pk)}
            running = u["followers"]
            for v in reversed(vals):
                try: t = int(datetime.fromisoformat(v["end_time"].replace("+0000", "+00:00").replace("Z", "+00:00")).timestamp() * 1000) - 1
                except Exception: continue
                day = int(t // 864e5)
                if day not in have:
                    add_snap(pk, t, running, 1); have.add(day)
                running -= (v.get("value") or 0)
    except Exception:
        pass
    after, got, pages = None, [], 0
    max_pages = 12 if deep else 1
    while True:
        params = {"fields": "id,caption,media_product_type,media_type,permalink,timestamp,like_count,comments_count,thumbnail_url,media_url,children{media_type,media_url,thumbnail_url}", "limit": 50}
        if after: params["after"] = after
        mj = meta(META_V + "/me/media", params)
        items = mj.get("data") or []
        got.extend(r for r in (norm_meta_media(m) for m in items) if r["id"])
        paging = mj.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        pages += 1
        _status(f"Syncing @{acc['username']} via Meta — page {pages} ({len(got)} reels)…")
        if not after or pages >= max_pages: break
    prev = {r["id"]: r for r in load_reels(pk)}
    cap = 200 if deep else 40
    for i, r in enumerate(got):
        if i < cap:
            try:
                metrics = "views,reach,saved,shares,ig_reels_avg_watch_time" if r.get("type") == "reel" else "views,reach,saved,shares"
                apply_insights(r, meta(META_V + f"/{r['id']}/insights", {"metric": metrics}))
            except Exception:
                old = prev.get(r["id"]);
                if old: r.update({k: old.get(k, 0) for k in ("views", "igViews", "saves", "shares", "reach", "watch")})
            if i % 5 == 4: _status(f"Insights @{acc['username']} — {i+1}/{min(len(got), cap)}…")
        else:
            old = prev.get(r["id"])
            if old: r.update({k: old.get(k, 0) for k in ("views", "igViews", "saves", "shares", "reach", "watch")})
    save_reels(pk, got)
    allr = load_reels(pk)
    acc["trialCount"] = 0
    reels_only = [r for r in allr if r.get("type", "reel") == "reel"]
    acc["avgViews"] = round(sum(r.get("views", 0) for r in reels_only) / len(reels_only)) if reels_only else 0
    acc["lastSync"] = int(time.time() * 1000)
    save_account(acc)
    return len(got), pages

def run_sync(pks=None, deep=False, actor="scheduler"):
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync["busy"] = True; _sync["lastError"] = ""
    try:
        for acc in load_accounts():
            if pks and acc["pk"] not in pks: continue
            try:
                d = deep or not load_reels(acc["pk"])
                n, pages = (sync_meta if acc.get("source") == "meta" else sync_hiker)(acc, d)
                log(actor, "sync", f"@{acc['username']} {n} reels / {pages} pages deep={d}")
            except Exception as e:
                _sync["lastError"] = f"@{acc.get('username')}: {e}"
                log(actor, "sync_error", _sync["lastError"])
        _sync["last"] = int(time.time() * 1000)
        set_setting("last_sync", str(_sync["last"]))
    finally:
        _sync["busy"] = False; _sync["status"] = ""
        _sync_lock.release()
    return True

def _scheduler():
    while True:
        try:
            cad = get_setting("cadence", "24h")
            iv = {"6h": 6 * 3600, "12h": 12 * 3600, "24h": 24 * 3600}.get(cad)
            last = int(get_setting("last_sync", "0") or 0) / 1000
            if iv and time.time() - last > iv and load_accounts():
                run_sync(actor="scheduler")
        except Exception as e:
            log("scheduler", "error", str(e))
        time.sleep(300)

@app.on_event("startup")
def _startup():
    init_db()
    threading.Thread(target=_scheduler, daemon=True).start()

# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    return {"ok": True, "service": "trending-hub", "version": HUB_VERSION}

@app.get("/trending/state")
def state(authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "viewer")
    accounts = load_accounts()
    reels = {a["pk"]: load_reels(a["pk"]) for a in accounts}
    snaps = {a["pk"]: load_snaps(a["pk"]) for a in accounts}
    with _db_lock, db() as c:
        favs = [r["key"] for r in c.execute("SELECT key FROM favs ORDER BY created")]
    return {"role": who["role"], "tokenName": who["name"], "accounts": accounts, "reels": reels, "snaps": snaps,
            "favs": favs, "lastSync": int(get_setting("last_sync", "0") or 0), "cadence": get_setting("cadence", "24h"),
            "sources": {"hiker": bool(get_setting("hiker_key")), "meta": bool(get_setting("meta_token"))},
            "sync": {"busy": _sync["busy"], "status": _sync["status"], "lastError": _sync["lastError"]},
            "hubVersion": HUB_VERSION}

@app.get("/trending/sync/status")
def sync_status(authorization: Optional[str] = Header(default=None)):
    auth(authorization, "viewer")
    return {"busy": _sync["busy"], "status": _sync["status"], "last": _sync["last"], "lastError": _sync["lastError"]}

@app.post("/trending/sync")
async def sync_now(req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    body = await req.json() if req.headers.get("content-length", "0") not in ("0", "") else {}
    # `full` = complete backfill (all pages). Plain sync = newest pages only. The
    # old `deep` name is accepted but no longer means full — clicking SYNC NOW
    # repeatedly must not cost 30 pages a click.
    pk, deep, force = body.get("pk"), bool(body.get("full")), bool(body.get("force"))
    last = int(get_setting("last_sync", "0") or 0) / 1000
    if not force and not pk and time.time() - last < 600:
        raise HTTPException(429, "Synced less than 10 minutes ago — pass force=true to override")
    if _sync["busy"]:
        raise HTTPException(409, "A sync is already running")
    threading.Thread(target=run_sync, kwargs={"pks": [pk] if pk else None, "deep": deep, "actor": who["name"]}, daemon=True).start()
    return {"started": True}

@app.post("/trending/accounts")
async def add_account(req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    body = await req.json()
    source = body.get("source", "hiker")
    accounts = load_accounts()
    if source == "meta":
        if any(a.get("source") == "meta" for a in accounts): raise HTTPException(409, "Meta account already tracked")
        try: u = norm_meta_user(meta(META_V + "/me", {"fields": "id,username,name,followers_count,follows_count,media_count"}))
        except Exception as e: raise HTTPException(502, str(e))
        if not u["username"]: raise HTTPException(502, "Token has no Instagram account attached")
        acc = {**u, "source": "meta", "pinned": True, "mine": True}
    else:
        handle = (body.get("handle") or "").strip().lstrip("@")
        if not handle: raise HTTPException(400, "handle required")
        if any(a["username"].lower() == handle.lower() for a in accounts): raise HTTPException(409, "Already tracked")
        try: u = norm_hiker_user(hiker("/v2/user/by/username", {"username": handle}))
        except Exception as e: raise HTTPException(502, str(e))
        if not u["pk"]: raise HTTPException(404, "Account not found")
        acc = {**u, "username": u["username"] or handle, "source": "hiker", "pinned": not accounts, "mine": not accounts}
    acc.update({"addedAt": int(time.time() * 1000), "lastSync": 0, "avgViews": 0, "trialCount": 0})
    save_account(acc)
    log(who["name"], "add_account", f"@{acc['username']} via {source}")
    threading.Thread(target=run_sync, kwargs={"pks": [acc["pk"]], "deep": True, "actor": who["name"]}, daemon=True).start()
    return {"account": acc}

@app.delete("/trending/accounts/{pk}")
def del_account(pk: str, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    with _db_lock, db() as c:
        c.execute("DELETE FROM accounts WHERE pk=?", (pk,)); c.execute("DELETE FROM reels WHERE pk=?", (pk,))
        c.execute("DELETE FROM snaps WHERE pk=?", (pk,)); c.execute("DELETE FROM favs WHERE key LIKE ?", (pk + ":%",))
    log(who["name"], "del_account", pk)
    return {"ok": True}

@app.post("/trending/accounts/{pk}/pin")
async def pin_account(pk: str, req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "viewer")
    body = await req.json()
    accs = [a for a in load_accounts() if a["pk"] == pk]
    if not accs: raise HTTPException(404, "unknown account")
    accs[0]["pinned"] = bool(body.get("on")); save_account(accs[0])
    return {"ok": True, "pinned": accs[0]["pinned"]}

@app.post("/trending/favorites")
async def favorites(req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "viewer")
    body = await req.json()
    key = str(body.get("key") or "")
    if ":" not in key: raise HTTPException(400, "key must be pk:reelId")
    with _db_lock, db() as c:
        if body.get("on"): c.execute("INSERT OR IGNORE INTO favs (key, created) VALUES (?,?)", (key, time.time()))
        else: c.execute("DELETE FROM favs WHERE key=?", (key,))
        favs = [r["key"] for r in c.execute("SELECT key FROM favs ORDER BY created")]
    return {"favs": favs}

def _mask(s):
    return (s[:4] + "…" + s[-4:]) if s and len(s) > 10 else ("set" if s else "")

@app.get("/trending/settings")
def get_settings(authorization: Optional[str] = Header(default=None)):
    auth(authorization, "admin")
    return {"hikerKey": _mask(get_setting("hiker_key")), "metaToken": _mask(get_setting("meta_token")),
            "metaExpires": int(get_setting("meta_expires", "0") or 0), "cadence": get_setting("cadence", "24h")}

@app.put("/trending/settings")
async def put_settings(req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    body = await req.json()
    if "hikerKey" in body: set_setting("hiker_key", (body.get("hikerKey") or "").strip())
    if "metaToken" in body:
        set_setting("meta_token", (body.get("metaToken") or "").strip()); set_setting("meta_expires", "0")
    if body.get("cadence") in ("manual", "6h", "12h", "24h"): set_setting("cadence", body["cadence"])
    log(who["name"], "settings", ",".join(k for k in body.keys()))
    return get_settings(authorization)

@app.post("/trending/settings/test")
async def test_settings(req: Request, authorization: Optional[str] = Header(default=None)):
    auth(authorization, "admin")
    body = await req.json()
    try:
        if body.get("source") == "meta":
            u = norm_meta_user(meta(META_V + "/me", {"fields": "id,username,followers_count"}))
            return {"ok": True, "message": f"Meta token works — @{u['username']} · {fmt_num(u['followers'])} followers"}
        u = norm_hiker_user(hiker("/v2/user/by/username", {"username": "instagram"}))
        return {"ok": bool(u["pk"]), "message": "HikerAPI key works" if u["pk"] else "Reachable, unexpected payload"}
    except Exception as e:
        return {"ok": False, "message": str(e)}

@app.post("/trending/settings/refresh-meta")
def refresh_meta(authorization: Optional[str] = Header(default=None)):
    auth(authorization, "admin")
    tok = get_setting("meta_token")
    if not tok: raise HTTPException(400, "No Meta token")
    try:
        j = _get_json(META_BASE + "/refresh_access_token", {}, {"grant_type": "ig_refresh_token", "access_token": tok})
    except Exception as e:
        raise HTTPException(502, str(e))
    if not j.get("access_token"): raise HTTPException(502, "No token in refresh response")
    set_setting("meta_token", j["access_token"])
    exp = int(time.time() * 1000 + (j.get("expires_in") or 60 * 86400) * 1000)
    set_setting("meta_expires", str(exp))
    return {"ok": True, "metaExpires": exp}

@app.get("/trending/tokens")
def list_tokens(authorization: Optional[str] = Header(default=None)):
    auth(authorization, "admin")
    with _db_lock, db() as c:
        rows = c.execute("SELECT id, name, role, created, last_used, revoked FROM tokens ORDER BY created").fetchall()
    return {"tokens": [dict(r) for r in rows]}

@app.post("/trending/tokens")
async def mint_token(req: Request, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    body = await req.json()
    name = (body.get("name") or "").strip()[:40] or "viewer"
    tok = "sv_" + secrets.token_urlsafe(24)
    tid = secrets.token_hex(6)
    with _db_lock, db() as c:
        c.execute("INSERT INTO tokens (id, hash, name, role, created) VALUES (?,?,?,?,?)", (tid, _hash(tok), name, "viewer", time.time()))
    log(who["name"], "mint_token", name)
    return {"id": tid, "name": name, "role": "viewer", "token": tok}

@app.delete("/trending/tokens/{tid}")
def revoke_token(tid: str, authorization: Optional[str] = Header(default=None)):
    who = auth(authorization, "admin")
    with _db_lock, db() as c:
        row = c.execute("SELECT role, name FROM tokens WHERE id=?", (tid,)).fetchone()
        if not row: raise HTTPException(404, "unknown token")
        if row["role"] == "admin": raise HTTPException(400, "Cannot revoke the admin token here")
        c.execute("UPDATE tokens SET revoked=1 WHERE id=?", (tid,))
    log(who["name"], "revoke_token", row["name"])
    return {"ok": True}

@app.post("/trending/import")
async def import_state(req: Request, authorization: Optional[str] = Header(default=None)):
    """Merge a client's local trx state (accounts/reels/snaps/favs) so nobody re-pays for a backfill."""
    who = auth(authorization, "admin")
    body = await req.json()
    existing = {a["pk"]: a for a in load_accounts()}
    n_acc = n_reels = n_snaps = 0
    for a in body.get("accounts") or []:
        if not a.get("pk"): continue
        a.setdefault("source", "hiker")
        if a["pk"] not in existing:
            save_account(a); n_acc += 1
    for pk, reels in (body.get("reels") or {}).items():
        rs = [r for r in reels if r.get("id")]
        if rs: save_reels(pk, rs); n_reels += len(rs)
    for pk, snaps in (body.get("snaps") or {}).items():
        for s in snaps:
            if s.get("t") and s.get("followers") is not None:
                add_snap(pk, int(s["t"]), int(s["followers"]), 1 if s.get("seeded") else 0); n_snaps += 1
    with _db_lock, db() as c:
        for k in body.get("favs") or []:
            if ":" in k: c.execute("INSERT OR IGNORE INTO favs (key, created) VALUES (?,?)", (k, time.time()))
    # Refresh derived counters for imported accounts
    for a in load_accounts():
        allr = load_reels(a["pk"])
        if allr and not a.get("lastSync"):
            solid = [r for r in allr if not r.get("trial")]
            a["trialCount"] = sum(1 for r in allr if r.get("trial"))
            a["avgViews"] = round(sum(r.get("views", 0) for r in solid) / len(solid)) if solid else 0
            save_account(a)
    log(who["name"], "import", f"{n_acc} accounts, {n_reels} reels, {n_snaps} snaps")
    return {"accounts": n_acc, "reels": n_reels, "snaps": n_snaps}

@app.get("/trending/export.csv")
def export_csv(pk: str, authorization: Optional[str] = Header(default=None)):
    auth(authorization, "viewer")
    accs = [a for a in load_accounts() if a["pk"] == pk]
    if not accs: raise HTTPException(404, "unknown account")
    a = accs[0]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["handle", "source", "type", "reel_id", "date", "trial", "total_views", "ig_views", "fb_views", "likes", "fb_likes", "comments", "fb_comments", "shares", "saves", "reach", "avg_watch_s", "duration_s", "link"])
    from datetime import datetime, timezone
    for r in sorted(load_reels(pk), key=lambda r: -(r.get("views") or 0)):
        date = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if r.get("ts") else ""
        link = r.get("permalink") or (f"https://www.instagram.com/reel/{r['code']}/" if r.get("code") else "")
        w.writerow(["@" + a["username"], r.get("source", "hiker"), r.get("type", "reel"), r["id"], date, "TRIAL" if r.get("trial") else "",
                    r.get("views", 0), r.get("igViews", 0), r.get("fb", 0), r.get("likes", 0), r.get("fbLikes") if r.get("fbLikes") is not None else "",
                    r.get("comments", 0), r.get("fbComments") if r.get("fbComments") is not None else "", r.get("shares", 0),
                    r.get("saves") if r.get("saves") is not None else "", r.get("reach", ""), f"{r['watch']/1000:.1f}" if r.get("watch") else "",
                    f"{r['duration']:.1f}" if r.get("duration") else "", link])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{a["username"]}-reels.csv"'})

# ---- download proxy: CDN media URLs are cross-origin + signed, so `<a download>`
# can't save them. Clients ask for a short-lived signed hub link (auth), then the
# browser hits it with no headers and gets a real attachment streamed through.
def _dl_secret():
    sec = get_setting("dl_secret")
    if not sec:
        sec = secrets.token_hex(32); set_setting("dl_secret", sec)
    return sec

def _dl_sig(pk, mid, idx, exp):
    import hmac
    return hmac.new(_dl_secret().encode(), f"{pk}|{mid}|{idx}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]

@app.post("/trending/download-link")
async def download_link(req: Request, authorization: Optional[str] = Header(default=None)):
    auth(authorization, "viewer")
    body = await req.json()
    pk, mid, idx = str(body.get("pk") or ""), str(body.get("id") or ""), int(body.get("idx") or 0)
    r = next((x for x in load_reels(pk) if x["id"] == mid), None)
    if not r: raise HTTPException(404, "unknown media")
    exp = int(time.time()) + 900
    return {"url": f"/trending/dl/{urlparse.quote(pk, safe='')}/{urlparse.quote(mid, safe='')}?idx={idx}&exp={exp}&sig={_dl_sig(pk, mid, idx, exp)}", "expires": exp}

@app.get("/trending/dl/{pk}/{mid}")
def download(pk: str, mid: str, idx: int = 0, exp: int = 0, sig: str = ""):
    if exp < time.time() or sig != _dl_sig(pk, mid, idx, exp):
        raise HTTPException(403, "link expired — reopen the reel and download again")
    r = next((x for x in load_reels(pk) if x["id"] == mid), None)
    if not r: raise HTTPException(404, "unknown media")
    media = r.get("media") or []
    if r.get("type") == "carousel" and media and 0 <= idx < len(media):
        src = media[idx].get("video") or media[idx].get("img"); ext = "mp4" if media[idx].get("video") else "jpg"
    else:
        src = r.get("videoUrl") or r.get("thumb"); ext = "mp4" if r.get("videoUrl") else "jpg"
    if not src: raise HTTPException(404, "no media url stored — re-sync")
    try:
        up = urlreq.urlopen(urlreq.Request(src, headers={"user-agent": UA}), timeout=60)
    except urlerr.HTTPError as e:
        raise HTTPException(410, f"source link expired (HTTP {e.code}) — re-sync the account to refresh media links")
    except Exception as e:
        raise HTTPException(502, f"fetch failed: {e}")
    acc = next((a for a in load_accounts() if a["pk"] == pk), None)
    name = f"{(acc or {}).get('username', 'media')}-{r.get('code') or mid}{('-' + str(idx + 1)) if r.get('type') == 'carousel' else ''}.{ext}"
    def gen():
        while True:
            chunk = up.read(256 * 1024)
            if not chunk: break
            yield chunk
    from fastapi.responses import StreamingResponse
    ctype = up.headers.get("content-type") or ("video/mp4" if ext == "mp4" else "image/jpeg")
    return StreamingResponse(gen(), media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})

@app.get("/trending/report.html", response_class=HTMLResponse)
def report():
    if REPORT_HTML.exists():
        return HTMLResponse(REPORT_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
    return HTMLResponse("<h1>report.html not deployed</h1>", status_code=404)

@app.get("/trending/log")
def get_log(authorization: Optional[str] = Header(default=None)):
    auth(authorization, "admin")
    with _db_lock, db() as c:
        return {"log": [dict(r) for r in c.execute("SELECT * FROM log ORDER BY t DESC LIMIT 200")]}
