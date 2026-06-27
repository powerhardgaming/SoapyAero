import os, json, threading, time, hashlib, uuid, secrets
import asyncio
import requests
from aiohttp import web

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
MAX_MESSAGES  = 20
MAX_CHARS     = 300
MAX_USERNAME  = 100
SOAPYCORE_VERSION = 5

UPDATE_LOG = [
    {"version":"5.1.0","date":"2026-06-12","notes":"SoCore5.1: Persistent sessions, group admins, kick/promote, bug fixes"},
    {"version":"5.0.0","date":"2026-06-01","notes":"SoCore5: Group chats, Gold theme, chunked media, typing indicators"},
    {"version":"4.0.0","date":"2026-05-01","notes":"SoapyCore 4: Video sending, Artemis theme, 100-char usernames"},
]

# ── Persistence ───────────────────────────────────────────────
USERS_FILE    = os.path.join(BASE_DIR, "users.json")
GROUPS_FILE   = os.path.join(BASE_DIR, "groups.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# users_db:    { username: password_hash }
# groups_db:   { group_id: { id, name, image, password_hash, creator, created_at,
#                            admins:[...], members:[...] } }
# sessions_db: { token: { username, created_at } }
users_db    = load_json(USERS_FILE,    {})
groups_db   = load_json(GROUPS_FILE,   {})
sessions_db = load_json(SESSIONS_FILE, {})

users_lock    = threading.Lock()
groups_lock   = threading.Lock()
sessions_lock = threading.Lock()

SESSION_TTL = 60 * 60 * 24 * 30   # 30 days

def clean_sessions():
    now = time.time()
    with sessions_lock:
        expired = [t for t, s in sessions_db.items() if now - s["created_at"] > SESSION_TTL]
        for t in expired:
            del sessions_db[t]
        if expired:
            save_json(SESSIONS_FILE, sessions_db)

def create_session(username):
    token = secrets.token_hex(32)
    with sessions_lock:
        sessions_db[token] = {"username": username, "created_at": int(time.time())}
        save_json(SESSIONS_FILE, sessions_db)
    return token

def verify_session(token):
    """Returns username or None."""
    if not token:
        return None
    with sessions_lock:
        s = sessions_db.get(token)
    if not s:
        return None
    if time.time() - s["created_at"] > SESSION_TTL:
        with sessions_lock:
            sessions_db.pop(token, None)
            save_json(SESSIONS_FILE, sessions_db)
        return None
    return s["username"]

def invalidate_session(token):
    with sessions_lock:
        sessions_db.pop(token, None)
        save_json(SESSIONS_FILE, sessions_db)

# ── GitHub ABG ────────────────────────────────────────────────
GH_TOKEN  = os.environ.get("GH_TOKEN", "")
GH_REPO   = os.environ.get("GH_REPO", "")
ABG_CACHE = {"url": None}

def get_abg_url():
    if not GH_TOKEN or not GH_REPO:
        return None
    for ext in ["png","jpg","jpeg","gif","webp"]:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GH_REPO}/contents/ABG.{ext}",
                headers={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"},
                timeout=8
            )
            if r.status_code == 200:
                return r.json().get("download_url")
        except Exception:
            pass
    return None

threading.Thread(target=lambda: ABG_CACHE.__setitem__("url", get_abg_url()), daemon=True).start()

# ── Runtime state ─────────────────────────────────────────────
clients       = set()     # all active WebSocket connections
user_map      = {}        # ws -> {"username": str, "room": str}
room_messages = {}        # room_id -> [msg, ...]
typing_state  = {}        # room_id -> {username: timestamp}

TYPING_TIMEOUT = 4

# ── Helpers ───────────────────────────────────────────────────
def get_room_clients(room_id):
    return [ws for ws, info in user_map.items() if info.get("room") == room_id]

def get_online_in_room(room_id):
    return [info["username"] for ws, info in user_map.items()
            if info.get("room") == room_id and info.get("username")]

async def broadcast(room_id, payload, exclude=None):
    dead = []
    for ws in list(get_room_clients(room_id)):
        if ws is exclude:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
        user_map.pop(ws, None)

async def broadcast_all(payload, exclude=None):
    dead = []
    for ws in list(clients):
        if ws is exclude:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
        user_map.pop(ws, None)

def store_message(room_id, payload):
    if room_id not in room_messages:
        room_messages[room_id] = []
    room_messages[room_id].append(payload)
    while len(room_messages[room_id]) > MAX_MESSAGES:
        room_messages[room_id].pop(0)

def public_group(g):
    """Return group dict safe to send to clients (no password hash)."""
    return {
        "id":          g["id"],
        "name":        g["name"],
        "image":       g.get("image",""),
        "creator":     g["creator"],
        "admins":      g.get("admins", []),
        "members":     g.get("members", []),
        "created_at":  g["created_at"],
        "has_password": bool(g.get("password_hash",""))
    }

def is_group_admin(group, username):
    """True if user is creator or in admins list."""
    return username == group["creator"] or username in group.get("admins", [])

# ── HTTP: static ──────────────────────────────────────────────
async def index(request):
    return web.FileResponse(os.path.join(BASE_DIR, "index.html"))

# ── HTTP: Auth ────────────────────────────────────────────────
async def auth_signup(request):
    body     = await request.json()
    username = body.get("username","").strip()
    password = body.get("password","")
    if not username or not password:
        return web.json_response({"ok":False,"error":"Fill all fields"})
    if len(username) < 2:
        return web.json_response({"ok":False,"error":"Username too short"})
    if len(username) > MAX_USERNAME:
        return web.json_response({"ok":False,"error":f"Username too long (max {MAX_USERNAME})"})
    if len(password) < 4:
        return web.json_response({"ok":False,"error":"Password too short (min 4)"})
    with users_lock:
        if username in users_db:
            return web.json_response({"ok":False,"error":"Username taken"})
        users_db[username] = hash_pw(password)
        save_json(USERS_FILE, users_db)
    token = create_session(username)
    return web.json_response({"ok":True,"username":username,"token":token})

async def auth_login(request):
    body     = await request.json()
    username = body.get("username","").strip()
    password = body.get("password","")
    with users_lock:
        stored = users_db.get(username)
    if not stored or stored != hash_pw(password):
        return web.json_response({"ok":False,"error":"Invalid username or password"})
    token = create_session(username)
    return web.json_response({"ok":True,"username":username,"token":token})

async def auth_token(request):
    """Verify a saved session token and return username."""
    body  = await request.json()
    token = body.get("token","")
    uname = verify_session(token)
    if not uname:
        return web.json_response({"ok":False,"error":"Session expired"})
    return web.json_response({"ok":True,"username":uname})

async def auth_signout(request):
    body  = await request.json()
    token = body.get("token","")
    invalidate_session(token)
    return web.json_response({"ok":True})

async def change_password(request):
    body     = await request.json()
    username = body.get("username","").strip()
    old_pw   = body.get("old_password","")
    new_pw   = body.get("new_password","")
    if not username or not old_pw or not new_pw:
        return web.json_response({"ok":False,"error":"Fill all fields"})
    if len(new_pw) < 4:
        return web.json_response({"ok":False,"error":"Password too short (min 4)"})
    with users_lock:
        stored = users_db.get(username)
        if not stored or stored != hash_pw(old_pw):
            return web.json_response({"ok":False,"error":"Current password incorrect"})
        users_db[username] = hash_pw(new_pw)
        save_json(USERS_FILE, users_db)
    return web.json_response({"ok":True})

async def change_username(request):
    body     = await request.json()
    old_name = body.get("old_username","").strip()
    new_name = body.get("new_username","").strip()
    password = body.get("password","")
    if not old_name or not new_name or not password:
        return web.json_response({"ok":False,"error":"Fill all fields"})
    if len(new_name) < 2:
        return web.json_response({"ok":False,"error":"Username too short"})
    if len(new_name) > MAX_USERNAME:
        return web.json_response({"ok":False,"error":f"Username too long (max {MAX_USERNAME})"})
    with users_lock:
        stored = users_db.get(old_name)
        if not stored or stored != hash_pw(password):
            return web.json_response({"ok":False,"error":"Password incorrect"})
        if new_name in users_db and new_name != old_name:
            return web.json_response({"ok":False,"error":"Username already taken"})
        pw_hash = users_db.pop(old_name)
        users_db[new_name] = pw_hash
        save_json(USERS_FILE, users_db)
    # Update all sessions for this user
    with sessions_lock:
        for s in sessions_db.values():
            if s["username"] == old_name:
                s["username"] = new_name
        save_json(SESSIONS_FILE, sessions_db)
    # Update creator/admin/member records in groups
    with groups_lock:
        changed = False
        for g in groups_db.values():
            if g["creator"] == old_name:
                g["creator"] = new_name; changed = True
            if old_name in g.get("admins",[]):
                g["admins"] = [new_name if u==old_name else u for u in g["admins"]]; changed = True
            if old_name in g.get("members",[]):
                g["members"] = [new_name if u==old_name else u for u in g["members"]]; changed = True
        if changed:
            save_json(GROUPS_FILE, groups_db)
    return web.json_response({"ok":True,"username":new_name})

# ── HTTP: Groups ──────────────────────────────────────────────
async def list_groups(request):
    with groups_lock:
        groups = list(groups_db.values())
    return web.json_response({"ok":True,"groups":[public_group(g) for g in groups]})

async def create_group(request):
    body     = await request.json()
    name     = body.get("name","").strip()
    password = body.get("password","")
    image    = body.get("image","")
    creator  = body.get("creator","").strip()
    if not name or not creator:
        return web.json_response({"ok":False,"error":"Name and creator required"})
    if len(name) > 60:
        return web.json_response({"ok":False,"error":"Name too long"})
    group_id = str(uuid.uuid4())[:8]
    group = {
        "id":            group_id,
        "name":          name,
        "image":         image,
        "password_hash": hash_pw(password) if password else "",
        "creator":       creator,
        "admins":        [],
        "members":       [creator],
        "created_at":    int(time.time()),
    }
    with groups_lock:
        groups_db[group_id] = group
        save_json(GROUPS_FILE, groups_db)
    await broadcast_all({"type":"group_created","group":public_group(group)})
    return web.json_response({"ok":True,"group_id":group_id})

async def join_group(request):
    body     = await request.json()
    group_id = body.get("group_id","")
    password = body.get("password","")
    username = body.get("username","").strip()
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if group.get("password_hash") and group["password_hash"] != hash_pw(password):
        return web.json_response({"ok":False,"error":"Wrong password"})
    # Add to members if not already
    with groups_lock:
        if username and username not in group.get("members",[]):
            group.setdefault("members",[]).append(username)
            save_json(GROUPS_FILE, groups_db)
    await broadcast_all({"type":"group_updated","group":public_group(group)})
    return web.json_response({"ok":True,"group":public_group(group)})

async def update_group(request):
    body     = await request.json()
    group_id = body.get("group_id","")
    username = body.get("username","").strip()
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if not is_group_admin(group, username):
        return web.json_response({"ok":False,"error":"Only admins can edit group settings"})
    if "name" in body:
        n = body["name"].strip()
        if not n or len(n) > 60:
            return web.json_response({"ok":False,"error":"Invalid name"})
        group["name"] = n
    if "password" in body:
        group["password_hash"] = hash_pw(body["password"]) if body["password"] else ""
    if "image" in body:
        group["image"] = body["image"]
    with groups_lock:
        groups_db[group_id] = group
        save_json(GROUPS_FILE, groups_db)
    await broadcast_all({"type":"group_updated","group":public_group(group)})
    return web.json_response({"ok":True})

async def delete_group(request):
    body     = await request.json()
    group_id = body.get("group_id","")
    username = body.get("username","").strip()
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if group["creator"] != username:
        return web.json_response({"ok":False,"error":"Only the creator can delete this group"})
    with groups_lock:
        del groups_db[group_id]
        save_json(GROUPS_FILE, groups_db)
    room_messages.pop(group_id, None)
    await broadcast_all({"type":"group_deleted","group_id":group_id})
    return web.json_response({"ok":True})

async def promote_member(request):
    """Creator promotes a member to admin."""
    body     = await request.json()
    group_id = body.get("group_id","")
    actor    = body.get("username","").strip()     # who is doing the action
    target   = body.get("target","").strip()       # who to promote
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if group["creator"] != actor:
        return web.json_response({"ok":False,"error":"Only the creator can promote members"})
    if target == group["creator"]:
        return web.json_response({"ok":False,"error":"Creator is already the owner"})
    admins = group.setdefault("admins", [])
    if target not in admins:
        admins.append(target)
    # Make sure they're also a member
    group.setdefault("members",[])
    if target not in group["members"]:
        group["members"].append(target)
    with groups_lock:
        save_json(GROUPS_FILE, groups_db)
    pg = public_group(group)
    await broadcast_all({"type":"group_updated","group":pg})
    # Notify the group room
    await broadcast(group_id, {"type":"system_msg","room":group_id,
        "content":f"{target} was promoted to admin by {actor}"})
    return web.json_response({"ok":True,"group":pg})

async def demote_member(request):
    """Creator demotes an admin back to member."""
    body     = await request.json()
    group_id = body.get("group_id","")
    actor    = body.get("username","").strip()
    target   = body.get("target","").strip()
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if group["creator"] != actor:
        return web.json_response({"ok":False,"error":"Only the creator can demote admins"})
    admins = group.get("admins",[])
    if target in admins:
        admins.remove(target)
    with groups_lock:
        save_json(GROUPS_FILE, groups_db)
    pg = public_group(group)
    await broadcast_all({"type":"group_updated","group":pg})
    await broadcast(group_id, {"type":"system_msg","room":group_id,
        "content":f"{target} was demoted to member by {actor}"})
    return web.json_response({"ok":True,"group":pg})

async def kick_member(request):
    """Creator or admin kicks a member."""
    body     = await request.json()
    group_id = body.get("group_id","")
    actor    = body.get("username","").strip()
    target   = body.get("target","").strip()
    with groups_lock:
        group = groups_db.get(group_id)
    if not group:
        return web.json_response({"ok":False,"error":"Group not found"})
    if not is_group_admin(group, actor):
        return web.json_response({"ok":False,"error":"Only admins can kick members"})
    if target == group["creator"]:
        return web.json_response({"ok":False,"error":"Cannot kick the group creator"})
    # Admins can't kick other admins (only creator can)
    if target in group.get("admins",[]) and actor != group["creator"]:
        return web.json_response({"ok":False,"error":"Only the creator can kick admins"})
    # Remove from members and admins
    members = group.get("members",[])
    admins  = group.get("admins",[])
    if target in members:  members.remove(target)
    if target in admins:   admins.remove(target)
    with groups_lock:
        save_json(GROUPS_FILE, groups_db)
    pg = public_group(group)
    await broadcast_all({"type":"group_updated","group":pg})
    # Tell the kicked user's WS to leave the room
    for ws, info in list(user_map.items()):
        if info.get("username") == target and info.get("room") == group_id:
            try:
                await ws.send_json({"type":"kicked","group_id":group_id,"by":actor})
            except Exception:
                pass
    await broadcast(group_id, {"type":"system_msg","room":group_id,
        "content":f"{target} was kicked by {actor}"})
    return web.json_response({"ok":True,"group":pg})

# ── HTTP: Chunked media upload ────────────────────────────────
# Videos are written to disk; images/audio stay in-memory base64.
# Video storage cap: 3 GB.  Oldest files are deleted first when over limit.

VIDEOS_DIR   = os.path.join(BASE_DIR, "videos")
VIDEO_CAP    = 3 * 1024 * 1024 * 1024   # 3 GB in bytes
os.makedirs(VIDEOS_DIR, exist_ok=True)

def _ext_for_mime(mime: str) -> str:
    return {
        "video/mp4":       ".mp4",
        "video/webm":      ".webm",
        "video/ogg":       ".ogv",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
    }.get(mime, ".mp4")

def enforce_video_cap():
    """Delete oldest video files until total size is under VIDEO_CAP."""
    try:
        files = []
        total = 0
        for fname in os.listdir(VIDEOS_DIR):
            fpath = os.path.join(VIDEOS_DIR, fname)
            if os.path.isfile(fpath):
                st = os.stat(fpath)
                files.append((st.st_mtime, st.st_size, fpath))
                total += st.st_size
        if total <= VIDEO_CAP:
            return
        # Sort oldest first
        files.sort(key=lambda x: x[0])
        for mtime, size, fpath in files:
            if total <= VIDEO_CAP:
                break
            try:
                os.remove(fpath)
                total -= size
                print(f"[video-gc] Removed {fpath} ({size//1024//1024} MB)", flush=True)
            except Exception as e:
                print(f"[video-gc] Failed to remove {fpath}: {e}", flush=True)
    except Exception as e:
        print(f"[video-gc] Error: {e}", flush=True)

pending_uploads = {}
uploads_lock    = threading.Lock()

async def upload_chunk(request):
    body       = await request.json()
    upload_id  = body.get("upload_id","")
    chunk_idx  = int(body.get("chunk_idx", 0))
    total      = int(body.get("total_chunks", 1))
    data       = body.get("data","")        # raw base64 (no data-URL prefix)
    media_type = body.get("media_type","image")
    room_id    = body.get("room_id","public")
    sender     = body.get("sender","?")
    mime       = body.get("mime","image/jpeg")

    with uploads_lock:
        if upload_id not in pending_uploads:
            pending_uploads[upload_id] = {
                "chunks": {}, "total": total, "type": media_type,
                "room": room_id, "sender": sender, "mime": mime,
            }
        pending_uploads[upload_id]["chunks"][chunk_idx] = data
        up = pending_uploads[upload_id]

        if len(up["chunks"]) < up["total"]:
            return web.json_response({"ok": True, "complete": False})

        # ── All chunks received ───────────────────────────────
        ordered = "".join(up["chunks"][i] for i in range(up["total"]))
        del pending_uploads[upload_id]

    # Decode and handle outside the lock
    import base64 as _b64
    raw_bytes = _b64.b64decode(ordered)

    if up["type"] == "video":
        # ── Write video to disk ───────────────────────────────
        ext      = _ext_for_mime(mime)
        fname    = f"{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}{ext}"
        fpath    = os.path.join(VIDEOS_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(raw_bytes)
        # Enforce 3 GB cap (runs in thread so we don't block the event loop)
        threading.Thread(target=enforce_video_cap, daemon=True).start()
        # Content is a URL path, not base64
        content = f"/videos/{fname}"
        payload = {
            "type":    "video",
            "name":    up["sender"],
            "content": content,      # URL served by the /videos/ route
            "room":    up["room"],
        }
    else:
        # ── Images / audio stay as base64 data-URLs ───────────
        import base64 as _b64x
        b64str  = _b64x.b64encode(raw_bytes).decode()
        content = f"data:{mime};base64,{b64str}"
        payload = {
            "type":    up["type"],
            "name":    up["sender"],
            "content": content,
            "room":    up["room"],
        }

    store_message(up["room"], payload)
    asyncio.ensure_future(broadcast(up["room"], payload))
    return web.json_response({"ok": True, "complete": True})

async def serve_video(request):
    """Serve a video file from the videos directory."""
    fname = request.match_info["filename"]
    # Prevent path traversal
    if "/" in fname or "\\" in fname or fname.startswith("."):
        raise web.HTTPForbidden()
    fpath = os.path.join(VIDEOS_DIR, fname)
    if not os.path.isfile(fpath):
        raise web.HTTPNotFound()
    ext  = os.path.splitext(fname)[1].lower()
    mime = {
        ".mp4":  "video/mp4",
        ".webm": "video/webm",
        ".ogv":  "video/ogg",
        ".mov":  "video/quicktime",
        ".avi":  "video/x-msvideo",
    }.get(ext, "video/mp4")
    return web.FileResponse(fpath, headers={"Content-Type": mime})

# ── HTTP: Export / Import ─────────────────────────────────────
BACKUP_PASSWORD = "ExpSoapy"

async def export_data(request):
    """
    POST /api/admin/export   { "password": "ExpSoapy" }
    Streams a ZIP containing users.json, groups.json, sessions.json,
    and every file in the videos/ directory.
    """
    import zipfile, io
    body = await request.json()
    if body.get("password","") != BACKUP_PASSWORD:
        return web.json_response({"ok":False,"error":"Wrong password"}, status=403)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # JSON data files
        for fname in ("users.json", "groups.json", "sessions.json", "cards.json"):
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, fname)

        # Video files
        if os.path.isdir(VIDEOS_DIR):
            for vname in os.listdir(VIDEOS_DIR):
                vpath = os.path.join(VIDEOS_DIR, vname)
                if os.path.isfile(vpath):
                    zf.write(vpath, os.path.join("videos", vname))

    buf.seek(0)
    ts = time.strftime("%Y%m%d_%H%M%S")
    return web.Response(
        body=buf.read(),
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="soapyaero_backup_{ts}.zip"'
        }
    )

async def import_data(request):
    """
    POST /api/admin/import   multipart: password field + backup.zip file
    Restores all JSON files and videos from the zip.
    Merges rather than wipes — existing users/groups not in the backup are kept,
    backup entries overwrite on conflict.
    """
    import zipfile, io
    reader = await request.multipart()

    password  = None
    zip_bytes = None

    async for part in reader:
        if part.name == "password":
            password = (await part.read()).decode("utf-8").strip()
        elif part.name == "file":
            zip_bytes = await part.read()

    if password != BACKUP_PASSWORD:
        return web.json_response({"ok":False,"error":"Wrong password"}, status=403)
    if not zip_bytes:
        return web.json_response({"ok":False,"error":"No file uploaded"}, status=400)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return web.json_response({"ok":False,"error":"Invalid zip file"}, status=400)

    restored = {"json": [], "videos": 0}

    with zf:
        for item in zf.namelist():
            # ── JSON data files ───────────────────────────────
            if item in ("users.json", "groups.json", "sessions.json", "cards.json"):
                try:
                    incoming = json.loads(zf.read(item).decode("utf-8"))
                except Exception:
                    continue

                dest = os.path.join(BASE_DIR, item)
                existing = load_json(dest, {})
                # Merge: backup wins on conflict
                existing.update(incoming)
                save_json(dest, existing)
                restored["json"].append(item)

                # Reload in-memory databases
                if item == "users.json":
                    with users_lock:
                        users_db.clear()
                        users_db.update(existing)
                elif item == "groups.json":
                    with groups_lock:
                        groups_db.clear()
                        groups_db.update(existing)
                elif item == "sessions.json":
                    with sessions_lock:
                        sessions_db.clear()
                        sessions_db.update(existing)
                elif item == "cards.json":
                    with cards_lock:
                        cards_db.clear()
                        cards_db.update(existing)

            # ── Video files ───────────────────────────────────
            elif item.startswith("videos/") and not item.endswith("/"):
                fname = os.path.basename(item)
                if not fname or fname.startswith("."):
                    continue
                dest = os.path.join(VIDEOS_DIR, fname)
                with open(dest, "wb") as f:
                    f.write(zf.read(item))
                restored["videos"] += 1

    # Broadcast updated group list to all connected clients
    with groups_lock:
        grps = list(groups_db.values())
    await broadcast_all({"type":"groups_list","groups":[public_group(g) for g in grps]})

    return web.json_response({
        "ok": True,
        "restored_json": restored["json"],
        "restored_videos": restored["videos"],
    })

# ── HTTP: Misc ────────────────────────────────────────────────
async def get_sounds(request):
    if not GH_TOKEN or not GH_REPO:
        return web.json_response({"ts":None,"send":None,"click":None})
    sounds = {}
    for name, key in [("TS.mp3","ts"),("Send.mp3","send"),("Click.mp3","click")]:
        try:
            r = requests.get(f"https://api.github.com/repos/{GH_REPO}/contents/{name}",
                headers={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"},timeout=6)
            sounds[key] = r.json().get("download_url") if r.status_code==200 else None
        except Exception:
            sounds[key] = None
    return web.json_response(sounds)

async def get_card_assets(request):
    """Return URLs for card sound and image files from the GitHub repo.
    Repo should contain: Rih.mp3, SoapPipe.mp3, BabyOil.mp3, Cheerio.mp3
                         Rih.png/jpg, SoapPipe.png/jpg, BabyOil.png/jpg, Cheerio.png/jpg
    """
    if not GH_TOKEN or not GH_REPO:
        return web.json_response({"sounds":{},"images":{}})
    sounds, images = {}, {}
    asset_map = {
        "sounds": [("Rih.mp3","rih"),("SoapPipe.mp3","soappipe"),("BabyOil.mp3","babyoil"),("Cheerio.mp3","cheerio")],
        "images": [("Rih.png","rih"),("Rih.jpg","rih"),
                   ("SoapPipe.png","soappipe"),("SoapPipe.jpg","soappipe"),
                   ("BabyOil.png","babyoil"),("BabyOil.jpg","babyoil"),
                   ("Cheerio.png","cheerio"),("Cheerio.jpg","cheerio")],
    }
    for fname, key in asset_map["sounds"]:
        try:
            r = requests.get(f"https://api.github.com/repos/{GH_REPO}/contents/{fname}",
                headers={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"},timeout=6)
            if r.status_code == 200:
                sounds[key] = r.json().get("download_url")
        except Exception:
            pass
    for fname, key in asset_map["images"]:
        if key in images:
            continue  # already found a match
        try:
            r = requests.get(f"https://api.github.com/repos/{GH_REPO}/contents/{fname}",
                headers={"Authorization":f"token {GH_TOKEN}","Accept":"application/vnd.github.v3+json"},timeout=6)
            if r.status_code == 200:
                images[key] = r.json().get("download_url")
        except Exception:
            pass
    return web.json_response({"sounds": sounds, "images": images})

async def get_abg(request):
    return web.json_response({"url": ABG_CACHE.get("url")})

async def get_update_log(request):
    return web.json_response({"updates": UPDATE_LOG})

async def get_theme_info(request):
    return web.json_response({"accent":"#c9a227","bg":"#0b0c0f","text":"#fff8e1","version":SOAPYCORE_VERSION})

async def verify_user(request):
    data = await request.json()
    uname = verify_session(data.get("token",""))
    if uname:
        return web.json_response({"ok":True,"username":uname})
    return web.json_response({"ok":False,"error":"Invalid session"}, status=401)

# ── WebSocket ─────────────────────────────────────────────────
async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=1_000_000)
    await ws.prepare(request)
    clients.add(ws)
    # Each connection gets its own info dict — username starts None
    user_info = {"username": None, "room": "public"}
    user_map[ws] = user_info

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            t    = data.get("type","")
            # Always read username fresh from user_info (not a stale local variable)
            username = user_info["username"]

            # ── Join ──────────────────────────────────────────
            if t == "join":
                uname = data.get("name","Anonymous")
                user_info["username"] = uname
                user_info["room"]     = "public"
                username = uname  # update local ref too

                await ws.send_json({"type":"history","messages":room_messages.get("public",[]),"room":"public"})

                with groups_lock:
                    grps = list(groups_db.values())
                await ws.send_json({"type":"groups_list","groups":[public_group(g) for g in grps]})

                # Announce to public room (exclude self so self doesn't see "X joined")
                await broadcast("public",{"type":"user_joined","name":uname,"room":"public"},exclude=ws)
                # Send online list (self is already in user_map so get_online_in_room includes us)
                online = get_online_in_room("public")
                await ws.send_json({"type":"online_list","room":"public","users":online})
                await broadcast("public",{"type":"online_list","room":"public","users":online},exclude=ws)

            # ── Switch room ───────────────────────────────────
            elif t == "switch_room":
                if username is None:
                    continue
                old_room = user_info["room"]
                new_room = data.get("room_id","public")
                if old_room == new_room:
                    continue

                # Leave old room — broadcast before updating room field
                await broadcast(old_room,{"type":"user_left","name":username,"room":old_room},exclude=ws)
                old_online = get_online_in_room(old_room)
                # Remove self from old count then broadcast
                await broadcast(old_room,{"type":"online_list","room":old_room,"users":old_online})

                user_info["room"] = new_room

                await ws.send_json({"type":"history","messages":room_messages.get(new_room,[]),"room":new_room})
                await broadcast(new_room,{"type":"user_joined","name":username,"room":new_room},exclude=ws)
                new_online = get_online_in_room(new_room)
                await ws.send_json({"type":"online_list","room":new_room,"users":new_online})
                await broadcast(new_room,{"type":"online_list","room":new_room,"users":new_online},exclude=ws)

            # ── Message ───────────────────────────────────────
            elif t == "message":
                if username is None:
                    continue
                room    = user_info["room"]
                content = data.get("content","")
                if len(content) > MAX_CHARS:
                    await ws.send_json({"type":"error","content":f"Max {MAX_CHARS} chars"})
                    continue
                if room in typing_state:
                    typing_state[room].pop(username, None)
                payload = {"type":"message","name":username,"content":content,"room":room}
                store_message(room, payload)
                await broadcast(room, payload)  # includes sender

            # ── Typing ────────────────────────────────────────
            elif t == "typing":
                if username is None:
                    continue
                room = user_info["room"]
                typing_state.setdefault(room,{})[username] = time.time()
                typers = [u for u, ts in typing_state[room].items()
                          if time.time()-ts < TYPING_TIMEOUT]
                await broadcast(room,{"type":"typing","room":room,"users":typers},exclude=ws)

            elif t == "stop_typing":
                if username is None:
                    continue
                room = user_info["room"]
                typing_state.get(room,{}).pop(username, None)
                typers = [u for u, ts in typing_state.get(room,{}).items()
                          if time.time()-ts < TYPING_TIMEOUT]
                await broadcast(room,{"type":"typing","room":room,"users":typers})

            # ── Audio ─────────────────────────────────────────
            elif t == "audio":
                if username is None:
                    continue
                room    = user_info["room"]
                content = data.get("content","")
                payload = {"type":"audio","name":username,"content":content,"room":room}
                store_message(room, payload)
                await broadcast(room, payload)  # includes sender

    finally:
        # Always use user_info here — local `username` may be stale
        uname = user_info.get("username")
        room  = user_info.get("room","public")

        clients.discard(ws)
        user_map.pop(ws, None)

        if uname:
            typing_state.get(room,{}).pop(uname, None)
            await broadcast(room,{"type":"user_left","name":uname,"room":room})
            online = get_online_in_room(room)
            await broadcast(room,{"type":"online_list","room":room,"users":online})

    return ws

# ── Typing cleanup ────────────────────────────────────────────
async def typing_cleanup(_app):
    while True:
        now = time.time()
        for room_id in list(typing_state.keys()):
            expired = [u for u, ts in list(typing_state[room_id].items())
                       if now-ts > TYPING_TIMEOUT]
            for u in expired:
                typing_state[room_id].pop(u, None)
        await asyncio.sleep(2)

# ══════════════════════════════════════════════════════════════
#  SOAPYCARDS SYSTEM
# ══════════════════════════════════════════════════════════════
import random as _random

CARDS_FILE = os.path.join(BASE_DIR, "cards.json")
cards_lock = threading.Lock()

# cards_db schema:
# {
#   username: {
#     "points":        int,
#     "collection":    [ {id, card, obtained_at}, ... ],
#     "last_draw":     float (epoch),        # 0 = never
#     "name_style":    { color, gradient_a, gradient_b, use_gradient } | null,
#     "card_cooldowns":{ card_name: last_used_epoch },
#   },
#   "__god_sessions__": { username: { activated_at, snapshot: {...} } }
# }
cards_db = load_json(CARDS_FILE, {})
cards_lock = threading.Lock()

CARD_DEFINITIONS = [
    {"id":"soapyrgb","name":"SoapyRGB", "rarity":"Common",   "chance":70,     "color":"#00ccff","emoji":"🌈","desc":"Customize your username color or gradient. Everyone sees it.",           "sell_price":10,  "cooldown":0},
    {"id":"rih",     "name":"Rih",       "rarity":"Rare",     "chance":50,     "color":"#ff66cc","emoji":"🌸","desc":"Plays the rizz sound for everyone online. 10s cooldown.",              "sell_price":40,  "cooldown":10},
    {"id":"soappipe","name":"Soap Pipe", "rarity":"Very Rare","chance":15,     "color":"#aaaaaa","emoji":"🔩","desc":"Plays metal pipe SFX and shows the pipe image for 10 seconds. 15s cd.","sell_price":120, "cooldown":15},
    {"id":"babyoil", "name":"BabyOil",   "rarity":"Legendary","chance":3,      "color":"#ffe066","emoji":"🍼","desc":"Shows the baby oil image on everyone's screen for 30 seconds. 60s cd.","sell_price":400, "cooldown":60},
    {"id":"cheerio", "name":"Cheerio",   "rarity":"Legendary","chance":1,      "color":"#ffcc44","emoji":"⭕","desc":"Floats a Cheerio on everyone's screen for 1 minute. 3s cooldown.",     "sell_price":600, "cooldown":3},
    {"id":"soapygod","name":"SoapyGod",  "rarity":"Ultrakill","chance":0.0001, "color":"#ff2222","emoji":"👁","desc":"24 hours of full server admin. One-time use, all effects revert.",     "sell_price":9999,"cooldown":0},
]
CARD_MAP = {c["id"]: c for c in CARD_DEFINITIONS}
DRAW_COOLDOWN     = 3600   # 1 hour between draws (0 for Limomani_3)
PROTECTED_USER    = "Limomani_3"
GOD_DURATION      = 86400  # 24 hours

def _ensure_user_cards(username):
    if username not in cards_db:
        cards_db[username] = {
            "points":        0,
            "collection":    [],
            "last_draw":     0,
            "name_style":    None,
            "card_cooldowns":{},
        }

def _save_cards():
    save_json(CARDS_FILE, cards_db)

def _draw_card():
    """Roll against each card's chance independently, highest-rarity wins."""
    won = []
    for card in CARD_DEFINITIONS:
        if _random.uniform(0, 100) < card["chance"]:
            won.append(card)
    if not won:
        # Guaranteed common if nothing hit
        won = [CARD_DEFINITIONS[0]]
    # Return rarest won card (lowest index = highest rarity = last in list)
    return sorted(won, key=lambda c: -CARD_DEFINITIONS.index(c))[0]

# ── Cards HTTP handlers ────────────────────────────────────────
async def cards_get(request):
    """GET /api/cards?username=X  — return user state + card definitions."""
    username = request.rel_url.query.get("username","")
    with cards_lock:
        _ensure_user_cards(username)
        data = dict(cards_db.get(username, {}))
        # Check god session
        god = cards_db.get("__god_sessions__",{}).get(username)
    now = time.time()
    time_until_draw = max(0, DRAW_COOLDOWN - (now - data.get("last_draw",0))) if username != PROTECTED_USER else 0
    return web.json_response({
        "ok":              True,
        "points":          data.get("points",0),
        "collection":      data.get("collection",[]),
        "name_style":      data.get("name_style", None),
        "card_cooldowns":  data.get("card_cooldowns",{}),
        "time_until_draw": int(time_until_draw),
        "can_draw":        time_until_draw == 0,
        "god_active":      bool(god and now - god["activated_at"] < GOD_DURATION),
        "god_expires":     god["activated_at"] + GOD_DURATION if god else None,
        "card_defs":       CARD_DEFINITIONS,
    })

async def cards_draw(request):
    """POST /api/cards/draw  { username }"""
    body     = await request.json()
    username = body.get("username","").strip()
    if not username:
        return web.json_response({"ok":False,"error":"No username"})
    now = time.time()
    with cards_lock:
        _ensure_user_cards(username)
        ud = cards_db[username]
        last = ud.get("last_draw", 0)
        if username != PROTECTED_USER and now - last < DRAW_COOLDOWN:
            remaining = int(DRAW_COOLDOWN - (now - last))
            return web.json_response({"ok":False,"error":f"Come back in {remaining}s","remaining":remaining})
        card = _draw_card()
        entry = {"id": str(uuid.uuid4())[:8], "card": card["id"], "obtained_at": int(now)}
        ud["collection"].append(entry)
        ud["last_draw"] = now
        _save_cards()
    return web.json_response({"ok":True,"card": card, "entry": entry})

async def cards_sell(request):
    """POST /api/cards/sell  { username, entry_id }"""
    body     = await request.json()
    username = body.get("username","").strip()
    entry_id = body.get("entry_id","")
    with cards_lock:
        _ensure_user_cards(username)
        ud   = cards_db[username]
        col  = ud.get("collection",[])
        entry = next((e for e in col if e["id"]==entry_id), None)
        if not entry:
            return web.json_response({"ok":False,"error":"Card not found"})
        card_def = CARD_MAP.get(entry["card"])
        if not card_def:
            return web.json_response({"ok":False,"error":"Unknown card"})
        col.remove(entry)
        ud["points"] = ud.get("points",0) + card_def["sell_price"]
        _save_cards()
    return web.json_response({"ok":True,"points":ud["points"],"earned":card_def["sell_price"]})

async def cards_give(request):
    """POST /api/cards/give  { from_username, to_username, entry_id }"""
    body        = await request.json()
    from_user   = body.get("from_username","").strip()
    to_user     = body.get("to_username","").strip()
    entry_id    = body.get("entry_id","")
    recover_pts = int(body.get("recover_points", 0))  # points paid to recover
    with cards_lock:
        _ensure_user_cards(from_user)
        _ensure_user_cards(to_user)
        sender   = cards_db[from_user]
        receiver = cards_db[to_user]
        col      = sender.get("collection",[])
        entry    = next((e for e in col if e["id"]==entry_id), None)
        if not entry:
            return web.json_response({"ok":False,"error":"Card not found in your collection"})
        card_def  = CARD_MAP.get(entry["card"])
        recover   = int(card_def["sell_price"] * 0.5)  # 50% of sell price to recover
        col.remove(entry)
        entry["given_by"]     = from_user
        entry["recover_cost"] = recover
        receiver.setdefault("collection",[]).append(entry)
        _save_cards()
    return web.json_response({"ok":True,"recover_cost":recover})

async def cards_recover(request):
    """POST /api/cards/recover  { username, entry_id }  — buy back a given card"""
    body     = await request.json()
    username = body.get("username","").strip()
    entry_id = body.get("entry_id","")
    with cards_lock:
        _ensure_user_cards(username)
        ud    = cards_db[username]
        col   = ud.get("collection",[])
        entry = next((e for e in col if e["id"]==entry_id), None)
        if not entry:
            return web.json_response({"ok":False,"error":"Card not found"})
        if "given_by" not in entry:
            return web.json_response({"ok":False,"error":"This card was not given — nothing to recover"})
        cost = entry.get("recover_cost", 0)
        if ud.get("points",0) < cost:
            return web.json_response({"ok":False,"error":f"Need {cost} points (you have {ud.get('points',0)})"})
        ud["points"] -= cost
        del entry["given_by"]
        del entry["recover_cost"]
        _save_cards()
    return web.json_response({"ok":True,"points":ud["points"]})

async def cards_use(request):
    """POST /api/cards/use  { username, entry_id, payload:{} }"""
    body     = await request.json()
    username = body.get("username","").strip()
    entry_id = body.get("entry_id","")
    payload  = body.get("payload", {})
    now      = time.time()

    with cards_lock:
        _ensure_user_cards(username)
        ud    = cards_db[username]
        col   = ud.get("collection",[])
        entry = next((e for e in col if e["id"]==entry_id), None)
        if not entry:
            return web.json_response({"ok":False,"error":"Card not found"})
        card_def = CARD_MAP.get(entry["card"])
        if not card_def:
            return web.json_response({"ok":False,"error":"Unknown card"})
        cid      = card_def["id"]
        cooldown = card_def.get("cooldown", 0)
        cds      = ud.setdefault("card_cooldowns", {})
        last_used = cds.get(cid, 0)
        if now - last_used < cooldown:
            remaining = int(cooldown - (now - last_used))
            return web.json_response({"ok":False,"error":f"Cooldown: {remaining}s remaining","remaining":remaining})

        # ── Card-specific logic ──────────────────────────────
        if cid == "soapyrgb":
            # Save name style; doesn't consume the card
            style = payload.get("style", {})
            ud["name_style"] = style
            _save_cards()
            # Broadcast to everyone so they update their name rendering
            await broadcast_all({"type":"card_name_style","username":username,"style":style})
            return web.json_response({"ok":True,"consumed":False})

        elif cid == "soapygod":
            # Check not already used
            god_sessions = cards_db.setdefault("__god_sessions__",{})
            existing = god_sessions.get(username)
            if existing and now - existing["activated_at"] < GOD_DURATION:
                return web.json_response({"ok":False,"error":"SoapyGod is already active"})
            if existing:
                return web.json_response({"ok":False,"error":"SoapyGod has already been used on this card"})
            # Snapshot current server params for revert
            snapshot = {
                "MAX_MESSAGES": MAX_MESSAGES,
                "MAX_CHARS":    MAX_CHARS,
                "groups":       {k: dict(v) for k,v in groups_db.items()},
                "users":        dict(users_db),
            }
            god_sessions[username] = {"activated_at": now, "snapshot": snapshot}
            col.remove(entry)   # consumed
            _save_cards()
            await broadcast_all({"type":"soapygod_activated","username":username,"expires":now+GOD_DURATION})
            return web.json_response({"ok":True,"consumed":True,"expires":now+GOD_DURATION})

        else:
            # Effect cards: broadcast effect, set cooldown, don't consume
            cds[cid] = now
            _save_cards()

        effect_payload = {"type":"card_effect","card":cid,"username":username,"payload":payload}

    # Broadcast effect outside lock
    await broadcast_all(effect_payload)
    return web.json_response({"ok":True,"consumed":False})

async def cards_set_name_style(request):
    """POST /api/cards/name_style  { username, style:{...} }"""
    body     = await request.json()
    username = body.get("username","").strip()
    style    = body.get("style", None)
    with cards_lock:
        _ensure_user_cards(username)
        cards_db[username]["name_style"] = style
        _save_cards()
    await broadcast_all({"type":"card_name_style","username":username,"style":style})
    return web.json_response({"ok":True})

async def cards_get_all_styles(request):
    """GET /api/cards/styles  — returns name styles for all users who have SoapyRGB active"""
    with cards_lock:
        styles = {u: d["name_style"] for u,d in cards_db.items()
                  if isinstance(d, dict) and d.get("name_style")}
    return web.json_response({"ok":True,"styles":styles})

# SoapyGod admin endpoints
def _god_log_action(actor, action_type, data):
    """Append an action to the god session log for full revert on expiry."""
    with cards_lock:
        god = cards_db.get("__god_sessions__",{}).get(actor)
        if not god:
            return
        god.setdefault("actions",[]).append({"type":action_type,"data":data,"at":int(time.time())})
        _save_cards()

async def god_kick(request):
    body   = await request.json()
    actor  = body.get("username","").strip()
    target = body.get("target","").strip()
    now    = time.time()
    with cards_lock:
        god = cards_db.get("__god_sessions__",{}).get(actor)
    if not god or now - god["activated_at"] >= GOD_DURATION:
        return web.json_response({"ok":False,"error":"No active SoapyGod session"})
    if target == PROTECTED_USER:
        return web.json_response({"ok":False,"error":f"Cannot kick {PROTECTED_USER}"})
    # Kick is ephemeral (no persistent state change) — just disconnect
    for ws, info in list(user_map.items()):
        if info.get("username") == target:
            try: await ws.send_json({"type":"god_kicked","by":actor})
            except: pass
    await broadcast_all({"type":"system_msg","room":"public","content":f"⚡ {target} was kicked by SoapyGod {actor}"})
    return web.json_response({"ok":True})

async def god_delete_account(request):
    body   = await request.json()
    actor  = body.get("username","").strip()
    target = body.get("target","").strip()
    now    = time.time()
    with cards_lock:
        god = cards_db.get("__god_sessions__",{}).get(actor)
    if not god or now - god["activated_at"] >= GOD_DURATION:
        return web.json_response({"ok":False,"error":"No active SoapyGod session"})
    if target == PROTECTED_USER:
        return web.json_response({"ok":False,"error":f"Cannot delete {PROTECTED_USER}"})
    pw_hash = None
    with users_lock:
        pw_hash = users_db.get(target)
        if target in users_db:
            del users_db[target]
            save_json(USERS_FILE, users_db)
    if pw_hash:
        # Log for revert: store the original password hash
        _god_log_action(actor, "delete_account", {"username": target, "pw_hash": pw_hash})
    for ws, info in list(user_map.items()):
        if info.get("username") == target:
            try: await ws.send_json({"type":"god_kicked","by":actor,"reason":"account_deleted"})
            except: pass
    await broadcast_all({"type":"system_msg","room":"public","content":f"⚡ Account '{target}' deleted by SoapyGod {actor}"})
    return web.json_response({"ok":True})

async def god_set_params(request):
    global MAX_MESSAGES, MAX_CHARS
    body  = await request.json()
    actor = body.get("username","").strip()
    now   = time.time()
    with cards_lock:
        god = cards_db.get("__god_sessions__",{}).get(actor)
    if not god or now - god["activated_at"] >= GOD_DURATION:
        return web.json_response({"ok":False,"error":"No active SoapyGod session"})
    old_msgs, old_chars = MAX_MESSAGES, MAX_CHARS
    if "max_messages" in body:
        MAX_MESSAGES = max(1, min(200, int(body["max_messages"])))
    if "max_chars" in body:
        MAX_CHARS = max(10, min(2000, int(body["max_chars"])))
    # Log for revert: record the previous values
    _god_log_action(actor, "set_params", {"old_max_messages": old_msgs, "old_max_chars": old_chars})
    await broadcast_all({"type":"server_params","max_chars":MAX_CHARS,"max_messages":MAX_MESSAGES})
    return web.json_response({"ok":True,"max_messages":MAX_MESSAGES,"max_chars":MAX_CHARS})

async def god_delete_group(request):
    body     = await request.json()
    actor    = body.get("username","").strip()
    group_id = body.get("group_id","")
    now      = time.time()
    with cards_lock:
        god = cards_db.get("__god_sessions__",{}).get(actor)
    if not god or now - god["activated_at"] >= GOD_DURATION:
        return web.json_response({"ok":False,"error":"No active SoapyGod session"})
    saved_group = None
    with groups_lock:
        if group_id not in groups_db:
            return web.json_response({"ok":False,"error":"Group not found"})
        saved_group = dict(groups_db[group_id])
        del groups_db[group_id]
        save_json(GROUPS_FILE, groups_db)
    room_messages.pop(group_id, None)
    # Log for revert
    _god_log_action(actor, "delete_group", {"group": saved_group})
    await broadcast_all({"type":"group_deleted","group_id":group_id})
    return web.json_response({"ok":True})

async def god_revert_check(_app):
    """Background task: auto-revert ALL SoapyGod actions after 24 hours."""
    global MAX_MESSAGES, MAX_CHARS
    while True:
        await asyncio.sleep(30)
        now = time.time()
        with cards_lock:
            god_sessions = cards_db.get("__god_sessions__", {})
            expired = [(u, dict(s)) for u, s in god_sessions.items()
                       if now - s["activated_at"] >= GOD_DURATION and "reverted" not in s]

        for username, session in expired:
            snap    = session.get("snapshot", {})
            actions = session.get("actions", [])

            # ── Revert each logged action in reverse order ──
            for action in reversed(actions):
                atype = action["type"]
                adata = action["data"]

                if atype == "delete_account":
                    # Restore the deleted account
                    uname = adata.get("username")
                    ph    = adata.get("pw_hash")
                    if uname and ph:
                        with users_lock:
                            if uname not in users_db:
                                users_db[uname] = ph
                        save_json(USERS_FILE, users_db)

                elif atype == "delete_group":
                    # Restore the deleted group
                    g = adata.get("group")
                    if g:
                        with groups_lock:
                            if g["id"] not in groups_db:
                                groups_db[g["id"]] = g
                        save_json(GROUPS_FILE, groups_db)

                elif atype == "set_params":
                    # Revert to the params that were in place before this change
                    # Only the FIRST set_params action's old values matter (original state)
                    pass  # handled below by snapshot

            # ── Revert params to pre-god snapshot ──────────
            MAX_MESSAGES = snap.get("MAX_MESSAGES", MAX_MESSAGES)
            MAX_CHARS    = snap.get("MAX_CHARS",    MAX_CHARS)

            # ── Restore any users/groups from snapshot not already restored ──
            snap_users = snap.get("users", {})
            with users_lock:
                for u, ph in snap_users.items():
                    if u not in users_db:
                        users_db[u] = ph
            save_json(USERS_FILE, users_db)

            snap_groups = snap.get("groups", {})
            with groups_lock:
                for gid, g in snap_groups.items():
                    if gid not in groups_db:
                        groups_db[gid] = g
            save_json(GROUPS_FILE, groups_db)

            # Mark reverted
            with cards_lock:
                cards_db["__god_sessions__"][username]["reverted"] = True
                _save_cards()

            await broadcast_all({
                "type":         "soapygod_expired",
                "username":     username,
                "max_chars":    MAX_CHARS,
                "max_messages": MAX_MESSAGES,
            })
            with groups_lock:
                grps = list(groups_db.values())
            await broadcast_all({"type":"groups_list","groups":[public_group(g) for g in grps]})
            print(f"[SoapyGod] Session for {username} expired and fully reverted ({len(actions)} actions).", flush=True)

async def on_startup(_app):
    asyncio.ensure_future(typing_cleanup(_app))
    asyncio.ensure_future(god_revert_check(_app))
    clean_sessions()
    # Ensure Limomani_3 premade account always exists
    with users_lock:
        if PROTECTED_USER not in users_db:
            users_db[PROTECTED_USER] = hash_pw("LM3R$")
            save_json(USERS_FILE, users_db)
            print(f"[Boot] Created protected account: {PROTECTED_USER}", flush=True)

# ── CORS middleware ───────────────────────────────────────────
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
        })
    r = await handler(request)
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

# ── App ───────────────────────────────────────────────────────
app = web.Application(
    client_max_size=30*1024*1024,
    middlewares=[cors_mw]
)
app.on_startup.append(on_startup)

app.router.add_get("/",                            index)
app.router.add_get("/ws",                          ws_handler)
app.router.add_post("/api/signup",                 auth_signup)
app.router.add_post("/api/login",                  auth_login)
app.router.add_post("/api/auth/token",             auth_token)
app.router.add_post("/api/auth/signout",           auth_signout)
app.router.add_post("/api/change_password",        change_password)
app.router.add_post("/api/change_username",        change_username)
app.router.add_get("/api/sounds",                  get_sounds)
app.router.add_get("/api/card_assets",             get_card_assets)
app.router.add_get("/api/abg",                     get_abg)
app.router.add_get("/api/updates",                 get_update_log)
app.router.add_get("/api/theme",                   get_theme_info)
app.router.add_post("/api/verify",                 verify_user)
app.router.add_get("/api/groups",                  list_groups)
app.router.add_post("/api/groups/create",          create_group)
app.router.add_post("/api/groups/join",            join_group)
app.router.add_post("/api/groups/update",          update_group)
app.router.add_post("/api/groups/delete",          delete_group)
app.router.add_post("/api/groups/promote",         promote_member)
app.router.add_post("/api/groups/demote",          demote_member)
app.router.add_post("/api/groups/kick",            kick_member)
app.router.add_post("/api/upload/chunk",           upload_chunk)
app.router.add_get("/videos/{filename}",           serve_video)
app.router.add_post("/api/admin/export",           export_data)
app.router.add_post("/api/admin/import",           import_data)
# Cards
app.router.add_get("/api/cards",                   cards_get)
app.router.add_post("/api/cards/draw",             cards_draw)
app.router.add_post("/api/cards/sell",             cards_sell)
app.router.add_post("/api/cards/give",             cards_give)
app.router.add_post("/api/cards/recover",          cards_recover)
app.router.add_post("/api/cards/use",              cards_use)
app.router.add_post("/api/cards/name_style",       cards_set_name_style)
app.router.add_get("/api/cards/styles",            cards_get_all_styles)
# SoapyGod
app.router.add_post("/api/god/kick",               god_kick)
app.router.add_post("/api/god/delete_account",     god_delete_account)
app.router.add_post("/api/god/params",             god_set_params)
app.router.add_post("/api/god/delete_group",       god_delete_group)

# ── Keep-alive ────────────────────────────────────────────────
SELF_URL = os.environ.get("SELF_URL","")
def keep_alive():
    while True:
        try:
            if SELF_URL: requests.get(SELF_URL, timeout=10)
        except Exception: pass
        time.sleep(360)
threading.Thread(target=keep_alive, daemon=True).start()

# ── Run ───────────────────────────────────────────────────────
port = int(os.environ.get("PORT", 8000))
print(f"[SoapyAero] Starting on port {port}", flush=True)
web.run_app(app, host="0.0.0.0", port=port, print=print)
