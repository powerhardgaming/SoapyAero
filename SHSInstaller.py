"""
SHSInstaller.py — Soapytech Helper Service Installer
Run with: python SHSInstaller.py
Requires Python 3.8+  |  pip install requests plyer pillow
"""
import os, sys, json, time, threading, subprocess, shutil, platform
import tkinter as tk
from tkinter import ttk, messagebox

# ── Config ─────────────────────────────────────────────────────────────────
GITHUB_RAW   = "https://raw.githubusercontent.com/Limomani-3/SHSInstaller/main"
REPO_FILES   = {
    "soapyupdater64":  "soapyupdater64.py",
    "soapychat_shs":   "soapychat_shs.py",
}
SERVICES = [
    {
        "id":    "soapychat",
        "label": "SoapyChat",
        "desc":  "Desktop notifications for SoapyChat messages",
        "file":  "soapychat_shs.py",
    },
]
MUSIC_DIR = os.path.join(os.path.expanduser("~"), "Music", "SoapytechHelperService")
CREDS_FILE = os.path.join(MUSIC_DIR, "soapychat_creds.json")

INFO_TEXT = (
    "SHS is a service that runs on your computer and as the name implies, "
    "it helps with all Soapy websites and apps that need it!\n"
    "It will auto update at startup."
)

# ── Colours (match SoapyCore default) ──────────────────────────────────────
BG      = "#0b0c0f"
BG2     = "#13151b"
BG3     = "#1e2028"
ACCENT  = "#7c5cfc"
ACCENT2 = "#5c3fdc"
TEXT    = "#e8eaf0"
TEXT2   = "#9499a8"
GREEN   = "#3ddc97"
RED     = "#ff5566"
PURPLE  = "#7c5cfc"

# ═══════════════════════════════════════════════════════════════════════════
class SHSInstaller(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SoapytechHelperService Installer")
        self.geometry("520x600")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._center()

        # Selected services
        self.checks = {}
        self.page = 0   # 0=select, 1=installing, 2=done

        self._build_header()
        self._build_select_page()

    def _center(self):
        self.update_idletasks()
        w, h = 520, 600
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ── Header ─────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self, bg=BG2, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚙", font=("Segoe UI Emoji", 32), bg=BG2, fg=ACCENT).pack()
        tk.Label(hdr, text="SoapytechHelperService", font=("Segoe UI", 15, "bold"),
                 bg=BG2, fg=TEXT).pack(pady=(4,2))
        tk.Label(hdr, text=INFO_TEXT, font=("Segoe UI", 9), bg=BG2, fg=TEXT2,
                 wraplength=460, justify="center").pack(padx=20)

    # ── Page 0: Service selection ───────────────────────────────────────────
    def _build_select_page(self):
        self.select_frame = tk.Frame(self, bg=BG)
        self.select_frame.pack(fill="both", expand=True, padx=28, pady=18)

        tk.Label(self.select_frame, text="Choose services to install:",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=TEXT).pack(anchor="w", pady=(0,10))

        for svc in SERVICES:
            row = tk.Frame(self.select_frame, bg=BG3, padx=14, pady=12,
                           highlightbackground=ACCENT, highlightthickness=1)
            row.pack(fill="x", pady=5)

            var = tk.BooleanVar(value=True)
            self.checks[svc["id"]] = var

            cb = tk.Checkbutton(row, text=svc["label"], variable=var,
                                font=("Segoe UI", 11, "bold"),
                                bg=BG3, fg=TEXT, selectcolor=ACCENT2,
                                activebackground=BG3, activeforeground=ACCENT,
                                cursor="hand2")
            cb.pack(anchor="w")
            tk.Label(row, text=svc["desc"], font=("Segoe UI", 9),
                     bg=BG3, fg=TEXT2).pack(anchor="w", padx=22)

        # SoapyChat extra: username + password
        self.chat_extra = tk.Frame(self.select_frame, bg=BG3, padx=14, pady=10,
                                   highlightbackground=ACCENT+"44", highlightthickness=1)
        self.chat_extra.pack(fill="x", pady=(0,5))
        tk.Label(self.chat_extra, text="SoapyChat credentials (for notifications):",
                 font=("Segoe UI", 9, "bold"), bg=BG3, fg=ACCENT).pack(anchor="w", pady=(0,6))

        self._labeled_entry(self.chat_extra, "Username", "user_entry")
        self._labeled_entry(self.chat_extra, "Password", "pw_entry", show="•")

        tk.Label(self.select_frame,
                 text="Scripts install to: ~/Music/SoapytechHelperService/",
                 font=("Segoe UI", 8), bg=BG, fg=TEXT2).pack(pady=(12,0))

        btn = tk.Button(self.select_frame, text="Install →",
                        font=("Segoe UI", 11, "bold"),
                        bg=ACCENT, fg="#fff", relief="flat", cursor="hand2",
                        activebackground=ACCENT2, activeforeground="#fff",
                        padx=24, pady=8, command=self._start_install)
        btn.pack(pady=14)

    def _labeled_entry(self, parent, label, attr, show=None):
        row = tk.Frame(parent, bg=BG3)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label+":", font=("Segoe UI", 9), bg=BG3,
                 fg=TEXT2, width=10, anchor="w").pack(side="left")
        kw = {"font":("Consolas",10),"bg":BG,"fg":TEXT,"relief":"flat",
              "insertbackground":ACCENT,"highlightbackground":ACCENT+"44",
              "highlightthickness":1}
        if show: kw["show"] = show
        e = tk.Entry(row, **kw)
        e.pack(side="left", fill="x", expand=True, padx=(4,0))
        setattr(self, attr, e)

    # ── Page 1: Installing ─────────────────────────────────────────────────
    def _start_install(self):
        selected = [s for s in SERVICES if self.checks[s["id"]].get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                                   "Select at least one service.", parent=self)
            return

        username = self.user_entry.get().strip()
        password = self.pw_entry.get().strip()
        if self.checks.get("soapychat", tk.BooleanVar()).get():
            if not username or not password:
                messagebox.showwarning("Credentials needed",
                    "Enter your SoapyChat username and password.", parent=self)
                return

        # Save creds
        os.makedirs(MUSIC_DIR, exist_ok=True)
        if username:
            with open(CREDS_FILE, "w") as f:
                json.dump({"username": username, "password": password}, f)

        self.select_frame.destroy()
        self._build_install_page(selected)

    def _build_install_page(self, selected):
        self.install_frame = tk.Frame(self, bg=BG)
        self.install_frame.pack(fill="both", expand=True, padx=28, pady=14)

        tk.Label(self.install_frame, text="Installing...",
                 font=("Segoe UI", 12, "bold"), bg=BG, fg=TEXT).pack(pady=(0,8))

        # Purple divider
        div = tk.Frame(self.install_frame, bg=PURPLE, height=2)
        div.pack(fill="x", pady=(0,10))

        tk.Label(self.install_frame, text="Actions", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=ACCENT).pack(anchor="w")

        # Log box
        log_outer = tk.Frame(self.install_frame, bg=BG2,
                             highlightbackground=ACCENT+"44", highlightthickness=1)
        log_outer.pack(fill="both", expand=True, pady=8)

        self.log = tk.Text(log_outer, bg=BG2, fg=TEXT2, font=("Consolas", 9),
                           relief="flat", state="disabled", wrap="word",
                           insertbackground=ACCENT)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log.tag_config("ok",     foreground=GREEN)
        self.log.tag_config("err",    foreground=RED)
        self.log.tag_config("head",   foreground=ACCENT)
        self.log.tag_config("plain",  foreground=TEXT2)

        self.finish_btn = tk.Button(self.install_frame, text="Finish",
                                    font=("Segoe UI", 11, "bold"),
                                    bg=BG3, fg=TEXT2, relief="flat",
                                    state="disabled", cursor="hand2",
                                    activebackground=GREEN, activeforeground="#fff",
                                    padx=24, pady=8, command=self._finish)
        self.finish_btn.pack(pady=10)

        threading.Thread(target=self._run_install, args=(selected,), daemon=True).start()

    def _log(self, msg, tag="plain"):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run_install(self, selected):
        try:
            import requests
        except ImportError:
            self._log("Installing requests...", "plain")
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "requests", "plyer", "pillow", "-q"])
            import importlib
            import requests  # noqa: F811

        os.makedirs(MUSIC_DIR, exist_ok=True)
        self._log("=== SoapytechHelperService Installer ===", "head")
        self._log(f"Install directory: {MUSIC_DIR}\n", "plain")

        # Step 1: soapyupdater64
        self._log("[ 1/2 ] Installing SoapyUpdater64...", "head")
        ok = self._download_file(requests, "soapyupdater64.py",
                                 os.path.join(MUSIC_DIR, "soapyupdater64.py"))
        if ok:
            self._log("  ✓ soapyupdater64.py installed", "ok")
        else:
            self._log("  ✗ Could not download soapyupdater64.py (will retry on next run)", "err")

        # Step 2: Selected services
        self._log(f"\n[ 2/2 ] Installing selected services...", "head")
        for svc in selected:
            self._log(f"  Installing {svc['label']}...", "plain")
            ok = self._download_file(requests, svc["file"],
                                     os.path.join(MUSIC_DIR, svc["file"]))
            if ok:
                self._log(f"  ✓ {svc['file']} installed", "ok")
            else:
                self._log(f"  ✗ {svc['file']} download failed", "err")

        # Step 3: Write manifest
        manifest = {
            "installed": [s["id"] for s in selected],
            "install_time": time.time(),
            "music_dir": MUSIC_DIR,
        }
        with open(os.path.join(MUSIC_DIR, "shs_manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        self._log("\n  ✓ Manifest written", "ok")

        # Step 4: Launch updater in background
        updater = os.path.join(MUSIC_DIR, "soapyupdater64.py")
        if os.path.exists(updater):
            self._log("\n  Starting SoapyUpdater64...", "plain")
            kwargs = {}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, updater], **kwargs)
            self._log("  ✓ SoapyUpdater64 running", "ok")

        self._log("\n=== Done! ===", "head")
        self._log(INFO_TEXT, "plain")

        # Enable finish
        self.after(0, self._enable_finish)

    def _download_file(self, requests, filename, dest):
        url = f"{GITHUB_RAW}/{filename}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            return True
        except Exception as e:
            self._log(f"    Error: {e}", "err")
            return False

    def _enable_finish(self):
        self.finish_btn.configure(state="normal", bg=GREEN, fg="#000",
                                  activebackground=ACCENT)

    def _finish(self):
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Check Python version
    if sys.version_info < (3, 8):
        print("SHSInstaller requires Python 3.8 or newer.")
        sys.exit(1)
    app = SHSInstaller()
    app.mainloop()
