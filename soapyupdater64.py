"""
soapyupdater64.py — SoapyTech Auto-Updater
Runs at startup (launched by SHSInstaller).
Compares installed scripts against GitHub and replaces changed ones silently.
Shows a small tray-style window while running.
"""
import os, sys, json, time, hashlib, threading, subprocess, platform
import tkinter as tk
from tkinter import ttk

GITHUB_RAW  = "https://raw.githubusercontent.com/Limomani-3/SHSInstaller/main"
MUSIC_DIR   = os.path.join(os.path.expanduser("~"), "Music", "SoapytechHelperService")
MANIFEST    = os.path.join(MUSIC_DIR, "shs_manifest.json")
CHECK_EVERY = 60 * 60  # check every hour

BG     = "#0b0c0f"
BG2    = "#13151b"
ACCENT = "#7c5cfc"
TEXT   = "#e8eaf0"
TEXT2  = "#9499a8"
GREEN  = "#3ddc97"
RED    = "#ff5566"

UPDATABLE = {
    "soapyupdater64": "soapyupdater64.py",
    "soapychat":      "soapychat_shs.py",
}

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            return json.load(f)
    return {"installed": []}

class UpdaterWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SoapyUpdater64")
        self.geometry("320x200")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._minimize)

        tk.Label(self, text="SoapyUpdater64", font=("Segoe UI", 12, "bold"),
                 bg=BG, fg=ACCENT).pack(pady=(16,4))
        tk.Label(self, text="Keeping your Soapy scripts up to date",
                 font=("Segoe UI", 9), bg=BG, fg=TEXT2).pack()

        # Purple line
        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x", padx=20, pady=10)

        self.status_var = tk.StringVar(value="Starting up...")
        tk.Label(self, textvariable=self.status_var, font=("Consolas", 9),
                 bg=BG, fg=TEXT2, wraplength=280).pack(pady=4)

        self.last_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.last_var, font=("Consolas", 8),
                 bg=BG, fg=TEXT2+"88").pack()

        tk.Button(self, text="Minimize", font=("Segoe UI", 9),
                  bg=BG2, fg=TEXT2, relief="flat", cursor="hand2",
                  activebackground=ACCENT, activeforeground="#fff",
                  command=self._minimize).pack(pady=8)

        threading.Thread(target=self._update_loop, daemon=True).start()

    def _center(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - 320) // 2
        y = (self.winfo_screenheight() - 200) // 2
        self.geometry(f"320x200+{x}+{y}")

    def _minimize(self):
        self.withdraw()

    def _set_status(self, msg):
        self.after(0, lambda: self.status_var.set(msg))

    def _set_last(self, msg):
        self.after(0, lambda: self.last_var.set(msg))

    def _update_loop(self):
        try:
            import requests
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "requests", "-q"])
            import requests  # noqa: F811

        while True:
            self._set_status("Checking for updates...")
            manifest = load_manifest()
            installed = manifest.get("installed", [])
            updated = []

            for svc_id, filename in UPDATABLE.items():
                if svc_id != "soapyupdater64" and svc_id not in installed:
                    continue
                local_path = os.path.join(MUSIC_DIR, filename)
                url = f"{GITHUB_RAW}/{filename}"
                try:
                    r = requests.get(url, timeout=15)
                    r.raise_for_status()
                    remote_content = r.content
                    remote_hash = hashlib.sha256(remote_content).hexdigest()

                    if os.path.exists(local_path):
                        local_hash = sha256(local_path)
                        if local_hash == remote_hash:
                            continue  # up to date

                    # Write update
                    with open(local_path, "wb") as f:
                        f.write(remote_content)
                    updated.append(filename)
                    self._set_status(f"Updated: {filename}")
                    time.sleep(0.5)
                except Exception as e:
                    self._set_status(f"Could not check {filename}")

            now = time.strftime("%H:%M:%S")
            if updated:
                self._set_last(f"Last check: {now} | Updated: {', '.join(updated)}")
                self._set_status("All up to date ✓")
                # Restart soapychat_shs if it was updated
                if "soapychat_shs.py" in updated:
                    _restart_shs_script("soapychat_shs.py")
            else:
                self._set_last(f"Last check: {now} | No updates")
                self._set_status("All up to date ✓")

            time.sleep(CHECK_EVERY)

def _restart_shs_script(filename):
    path = os.path.join(MUSIC_DIR, filename)
    if not os.path.exists(path): return
    # Kill old instance (best-effort)
    # Then relaunch
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, path], **kwargs)

if __name__ == "__main__":
    app = UpdaterWindow()
    # Also launch installed background scripts silently
    manifest = load_manifest()
    for svc_id in manifest.get("installed", []):
        script = UPDATABLE.get(svc_id)
        if script:
            path = os.path.join(MUSIC_DIR, script)
            if os.path.exists(path):
                kwargs = {}
                if platform.system() == "Windows":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.Popen([sys.executable, path], **kwargs)
    app.mainloop()
