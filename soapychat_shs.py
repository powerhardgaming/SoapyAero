"""
soapychat_shs.py — SoapyChat Desktop Notification Service
Runs silently in the background.
Connects to SoapyChat WebSocket and shows native desktop notifications
for new messages (excluding your own).
Theme colour is fetched from the server's /api/theme endpoint.
"""
import os, sys, json, time, threading, platform
import tkinter as tk

MUSIC_DIR   = os.path.join(os.path.expanduser("~"), "Music", "SoapytechHelperService")
CREDS_FILE  = os.path.join(MUSIC_DIR, "soapychat_creds.json")
CONFIG_FILE = os.path.join(MUSIC_DIR, "soapychat_shs_config.json")

# Default server — update this to your Render URL
DEFAULT_WS_URL   = "wss://soapychat.onrender.com/ws"
DEFAULT_HTTP_URL = "https://soapychat.onrender.com"

# ── Notification styling colours (overridden by server theme if available) ─
ACCENT  = "#7c5cfc"
BG      = "#13151b"
TEXT    = "#e8eaf0"
TEXT2   = "#9499a8"

# ── Load config ─────────────────────────────────────────────────────────────
def load_creds():
    if os.path.exists(CREDS_FILE):
        with open(CREDS_FILE) as f:
            return json.load(f)
    return {}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"ws_url": DEFAULT_WS_URL, "http_url": DEFAULT_HTTP_URL}

def save_config(c):
    with open(CONFIG_FILE, "w") as f:
        json.dump(c, f, indent=2)

# ── Fetch server theme accent colour ────────────────────────────────────────
def fetch_theme_accent(http_url):
    try:
        import requests
        r = requests.get(http_url + "/api/theme", timeout=5)
        if r.ok:
            data = r.json()
            return data.get("accent", ACCENT)
    except Exception:
        pass
    return ACCENT

# ── Tkinter toast notification ───────────────────────────────────────────────
class ToastNotification:
    """Small themed popup in the top-right corner, auto-dismisses after 5s."""

    def __init__(self, username, message, accent_color=ACCENT):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=BG)

        if platform.system() == "Windows":
            self.root.attributes("-transparentcolor", "")

        # Size & position: top-right corner
        w, h = 320, 80
        sw = self.root.winfo_screenwidth()
        x = sw - w - 16
        y = 16
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Border frame
        outer = tk.Frame(self.root, bg=accent_color, padx=2, pady=2)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG, padx=12, pady=8)
        inner.pack(fill="both", expand=True)

        # Accent top bar
        tk.Frame(inner, bg=accent_color, height=2).pack(fill="x", pady=(0,6))

        top_row = tk.Frame(inner, bg=BG)
        top_row.pack(fill="x")
        tk.Label(top_row, text="💬 SoapyChat", font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=accent_color).pack(side="left")
        tk.Label(top_row, text=time.strftime("%H:%M"),
                 font=("Segoe UI", 8), bg=BG, fg=TEXT2).pack(side="right")

        tk.Label(inner, text=f"{username}: {message}",
                 font=("Segoe UI", 10), bg=BG, fg=TEXT,
                 wraplength=280, justify="left", anchor="w").pack(fill="x", pady=(2,0))

        self.root.bind("<Button-1>", lambda e: self._dismiss())
        self._fade_in()
        self.root.after(5000, self._fade_out)

    def _fade_in(self):
        alpha = self.root.attributes("-alpha")
        if alpha < 0.95:
            self.root.attributes("-alpha", min(alpha + 0.08, 0.95))
            self.root.after(20, self._fade_in)

    def _fade_out(self):
        alpha = self.root.attributes("-alpha")
        if alpha > 0.0:
            self.root.attributes("-alpha", max(alpha - 0.07, 0.0))
            self.root.after(25, self._fade_out)
        else:
            self.root.destroy()

    def _dismiss(self):
        self.root.destroy()

    def show(self):
        self.root.mainloop()

def show_notification(username, message, accent):
    """Run notification in its own thread so it doesn't block WS listener."""
    def _run():
        t = ToastNotification(username, message, accent)
        t.show()
    threading.Thread(target=_run, daemon=True).start()

# ── WebSocket listener ───────────────────────────────────────────────────────
def run_listener(creds, config, accent):
    try:
        import websocket
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "websocket-client", "-q"])
        import websocket  # noqa: F811

    my_username = creds.get("username", "")
    ws_url = config.get("ws_url", DEFAULT_WS_URL)

    def on_open(ws):
        # Authenticate
        ws.send(json.dumps({
            "type": "auth",
            "username": creds.get("username", ""),
            "password": creds.get("password", ""),
        }))

    def on_message(ws, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        t = data.get("type", "")
        # Only show text/image messages from other users
        if t in ("message", "image", "audio", "video"):
            sender = data.get("name", "")
            if sender and sender != my_username:
                if t == "message":
                    body = data.get("content", "")[:80]
                elif t == "image":
                    body = "[sent an image]"
                elif t == "audio":
                    body = "[sent audio]"
                elif t == "video":
                    body = "[sent a video]"
                else:
                    body = "[message]"
                show_notification(sender, body, accent)

    def on_error(ws, err):
        pass  # silent

    def on_close(ws, *args):
        # Reconnect after 10s
        time.sleep(10)
        connect()

    def connect():
        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

    connect()

# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    creds  = load_creds()
    config = load_config()

    if not creds.get("username"):
        # No creds saved — exit silently (installer should have saved them)
        sys.exit(0)

    # Fetch theme accent from server
    accent = fetch_theme_accent(config.get("http_url", DEFAULT_HTTP_URL))

    # Run WS listener (blocks forever, handles reconnects)
    try:
        run_listener(creds, config, accent)
    except KeyboardInterrupt:
        pass
