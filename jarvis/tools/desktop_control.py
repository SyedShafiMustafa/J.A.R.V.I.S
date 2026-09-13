import os
import re
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

# Values coming from LLM-generated plans are user- or model-controlled.
# Before interpolating an app name into a PowerShell script or using it
# to build a command line, require it to be a plain safe identifier.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._()-]+$")


class DesktopController:

    def __init__(self):
        self.apps = self.scan_apps()

    # -------------------------------------------------
    # Scan Start Menu shortcuts
    # -------------------------------------------------

    def scan_apps(self):
        folders = [
            Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
            Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"))
        ]

        apps = {}

        for folder in folders:
            if not folder.exists():
                continue

            for file in folder.rglob("*.lnk"):
                apps[file.stem.lower()] = str(file)

        return apps

    # -------------------------------------------------
    # Find closest matching shortcut
    # -------------------------------------------------

    def find_app(self, query):
        query = query.lower().strip()

        aliases = {
            "opera gx": "opera gx",
            "opera": "opera",
            "chrome": "google chrome",
            "edge": "microsoft edge",
            "vs code": "visual studio code",
            "vscode": "visual studio code",
            "notepad": "notepad",
            "discord": "discord",
        }

        query = aliases.get(query, query)

        # Exact match
        if query in self.apps:
            return self.apps[query]

        # Partial match
        for name, path in self.apps.items():
            if query in name:
                return path

        return None

    # -------------------------------------------------
    # Open any Windows app
    # -------------------------------------------------

    def open_app(self, app):
        app = app.lower().strip()
        if not app or len(app) < 3 or not _SAFE_NAME.match(app):
            return False

        # 1. Desktop shortcut
        shortcut = self.find_app(app)
        if shortcut:
            try:
                os.startfile(shortcut)
                return True
            except OSError:
                return False

        # 2. Microsoft Store apps (WhatsApp, Spotify, etc.)
        # The app name is passed as a script argument ($args[0]) instead of
        # being interpolated into the script body, and it must match a safe
        # identifier pattern before it is used at all.
        if not _SAFE_NAME.match(app):
            print(f"App name rejected (unsafe characters): {app}")
            return False

        try:
            ps_script = (
                "$app = Get-StartApps | "
                "Where-Object { $_.Name -like ('*' + $args[0] + '*') } | "
                "Select-Object -First 1; "
                "if ($app) { Start-Process ('shell:AppsFolder\\' + $app.AppID) }"
            )

            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script, app],
                capture_output=True,
                text=True,
                timeout=15
            )

            # If PowerShell executed successfully, assume it launched
            if result.returncode == 0:
                return True

        except (OSError, subprocess.SubprocessError):
            return False

        return False

    # -------------------------------------------------
    # Close app
    # -------------------------------------------------

    def close_app(self, app):
        app = app.lower().strip()
        if not app or len(app) < 3 or not _SAFE_NAME.match(app):
            return False
        aliases = {
            "google chrome": "chrome",
            "microsoft edge": "msedge",
            "visual studio code": "code",
            "vs code": "code",
        }
        target = aliases.get(app, app).replace(" ", "")
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return False
            for line in result.stdout.splitlines():
                parts = line.strip().strip('"').split('","')
                if len(parts) < 2:
                    continue
                proc = parts[0].strip().lower()
                stem = proc.removesuffix(".exe")
                if stem != target:
                    continue
                killed = subprocess.run(
                    ["taskkill", "/IM", proc, "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return killed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        return False

    # -------------------------------------------------
    # Browser tools
    # -------------------------------------------------

    def open_url(self, url):
        return bool(url and webbrowser.open(url))

    def open_youtube(self):
        return self.open_url("https://www.youtube.com")

    def search_youtube(self, query):
        if not query or not query.strip():
            return False
        url = (
            "https://www.youtube.com/results?"
            f"search_query={quote_plus(query)}"
        )
        return self.open_url(url)

    def search_google(self, query):
        if not query or not query.strip():
            return False
        url = (
            "https://www.google.com/search?"
            f"q={quote_plus(query)}"
        )
        return self.open_url(url)