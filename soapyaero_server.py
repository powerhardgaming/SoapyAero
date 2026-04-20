import os, json, threading, time, hashlib, uuid
import requests
from aiohttp import web

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
MAX_MESSAGES = 15
MAX_CHARS    = 300

# ── Users DB (simple JSON file) ───────────────────────────────
USERS_FILE = os.path.join(BASE_DIR, "users.json")

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}

def save_users(u):
    with open(USERS_FILE, "w") as f:
        json.dump(u, f, indent=2)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

users_db = load_users()
users_lock = threading.Lock()

# ── GitHub ABG sync ───────────────────────────────────────────
GH_TOKEN  = os.environ.get("GH_TOKEN", "")
GH_REPO   = os.environ.get("GH_REPO", "")
GH_BRANCH = os.environ.get("GH_BRANCH", "main")
ABG_CACHE = {"url": None, "etag": None}

def get_abg_url():
    """Try to get the ABG image from GitHub repo root."""
    if not GH_TOKEN or not GH_REPO:
        return None
    for ext in ["png","jpg","jpeg","gif","webp"]:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GH_REPO}/contents/ABG.{ext}",
                headers={"Authorization": f"token {GH_TOKEN}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=8
            )
            if r.status_code == 200:
                d = r.json()
                return d.get("download_url")
        except:
            pass
    return None

# Cache ABG URL at startup
def init_abg():
    url = get_abg_url()
    if url:
        ABG_CACHE["url"] = url
        print(f"[ABG] Found background: {url}")
    else:
        print("[ABG] No ABG image found in repo root")

threading.Thread(target=init_abg, daemon=True).start()

# ── HTTP routes ───────────────────────────────────────────────
async def index(request):
    return web.FileResponse(os.path.join(BASE_DIR, "index.html"))

async def auth_signup(request):
    body = await request.json()
    username = body.get("username","").strip()
    password = body.get("password","")
    if not username or not password:
        return web.json_response({"ok":False,"error":"Fill all fields"})
    if len(username) < 2:
        return web.json_response({"ok":False,"error":"Username too short"})
    if len(password) < 4:
        return web.json_response({"ok":False,"error":"Password too short (min 4)"})
    with users_lock:
        if username in users_db:
            return web.json_response({"ok":False,"error":"Username taken"})
        users_db[username] = hash_pw(password)
        save_users(users_db)
    return web.json_response({"ok":True,"username":username})

async def auth_login(request):
    body = await request.json()
    username = body.get("username","").strip()
    password = body.get("password","")
    with users_lock:
        stored = users_db.get(username)
    if not stored or stored != hash_pw(password):
        return web.json_response({"ok":False,"error":"Invalid username or password"})
    return web.json_response({"ok":True,"username":username})

async def get_abg(request):
    """Return the ABG background URL."""
    url = ABG_CACHE.get("url")
    return web.json_response({"url": url})

async def refresh_abg(request):
    """Force refresh ABG from GitHub."""
    url = get_abg_url()
    ABG_CACHE["url"] = url
    return web.json_response({"url": url})

# ── WebSocket ─────────────────────────────────────────────────
clients   = set()
users     = {}      # ws -> username
messages  = []
# Active screen shares: sharer_username -> {offer, viewers}
shares    = {}

async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=20_000_000)
    await ws.prepare(request)
    clients.add(ws)
    await ws.send_json({"type":"history","messages":messages})
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                t = data.get("type","")

                if t == "join":
                    users[ws] = data.get("name","Anonymous")

                elif t == "message":
                    content = data.get("content","")
                    if len(content) > MAX_CHARS:
                        await ws.send_json({"type":"error","content":f"Max {MAX_CHARS} chars"})
                        continue
                    payload = {"type":"message","name":users.get(ws,"?"),"content":content}
                    messages.append(payload)
                    while len(messages) > MAX_MESSAGES: messages.pop(0)
                    for c in set(clients): await c.send_json(payload)

                elif t in ("image","audio"):
                    payload = {"type":t,"name":users.get(ws,"?"),"content":data["content"]}
                    messages.append(payload)
                    while len(messages) > MAX_MESSAGES: messages.pop(0)
                    for c in set(clients): await c.send_json(payload)

                # ── Screen share signaling ──────────────────────
                elif t == "screenshare_start":
                    # Broadcaster announces they're sharing
                    sharer = users.get(ws,"?")
                    shares[sharer] = {"ws": ws}
                    payload = {
                        "type": "screenshare_announce",
                        "sharer": sharer,
                        "share_id": sharer
                    }
                    messages.append(payload)
                    while len(messages) > MAX_MESSAGES: messages.pop(0)
                    for c in set(clients): await c.send_json(payload)

                elif t == "screenshare_stop":
                    sharer = users.get(ws,"?")
                    shares.pop(sharer, None)
                    for c in set(clients):
                        await c.send_json({"type":"screenshare_ended","sharer":sharer})

                elif t == "screenshare_frame":
                    # Broadcaster sends a frame → relay to all viewers of this share
                    sharer = users.get(ws,"?")
                    frame  = data.get("frame","")
                    payload = {"type":"screenshare_frame","sharer":sharer,"frame":frame}
                    for c in set(clients):
                        if c != ws:
                            try: await c.send_json(payload)
                            except: pass

                elif t == "screenshare_join":
                    # Viewer wants to watch
                    sharer = data.get("sharer","")
                    viewer = users.get(ws,"?")
                    # Tell sharer someone joined
                    if sharer in shares and shares[sharer]["ws"] in clients:
                        await shares[sharer]["ws"].send_json({
                            "type":"screenshare_viewer_joined","viewer":viewer
                        })

    finally:
        clients.discard(ws)
        sharer = users.pop(ws, None)
        if sharer and sharer in shares:
            shares.pop(sharer)
            for c in set(clients):
                try: await c.send_json({"type":"screenshare_ended","sharer":sharer})
                except: pass
    return ws

# ── App ───────────────────────────────────────────────────────
app = web.Application(client_max_size=25*1024*1024)
app.router.add_get("/",                index)
app.router.add_get("/ws",              ws_handler)
app.router.add_post("/api/signup",     auth_signup)
app.router.add_post("/api/login",      auth_login)
app.router.add_get("/api/abg",         get_abg)
app.router.add_post("/api/abg/refresh",refresh_abg)

@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={"Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type","Access-Control-Allow-Methods":"GET,POST,OPTIONS"})
    r = await handler(request)
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r
app.middlewares.append(cors_mw)

# ── Keep alive ────────────────────────────────────────────────
SELF_URL = os.environ.get("SELF_URL","")
def keep_alive():
    while True:
        try:
            if SELF_URL: requests.get(SELF_URL, timeout=10)
        except: pass
        time.sleep(360)
threading.Thread(target=keep_alive, daemon=True).start()

port = int(os.environ.get("PORT", 8000))
web.run_app(app, host="0.0.0.0", port=port)
