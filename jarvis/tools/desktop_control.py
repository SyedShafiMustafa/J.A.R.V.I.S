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

        # 1. Desktop shortcut
        shortcut = self.find_app(app)
        if shortcut:
            os.startfile(shortcut)
            return True

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

        except Exception:
            pass

        print(f"App not found: {app}")
        return False

    # -------------------------------------------------
    # Close app
    # -------------------------------------------------

    def close_app(self, app):
        app = app.lower().strip()

        # 1. Direct guess: <app>.exe
        # Use an argument list (no shell) so app-controlled input can never
        # be interpreted as shell syntax.
        if _SAFE_NAME.match(app):
            try:
                subprocess.run(
                    ["taskkill", "/IM", f"{app}.exe", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
            except Exception:
                pass

        # 2. Fallback: scan real process names and kill any that match
        #    a word of the app name (handles "visual studio code" -> Code.exe)
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10
            )

            for line in result.stdout.splitlines():
                parts = line.strip().strip('"').split('","')

                if len(parts) < 2:
                    continue

                proc = parts[0].strip().lower()
                stem = proc.replace(".exe", "")

                for word in app.split():
                    if len(word) > 2 and word in stem:
                        try:
                            subprocess.run(
                                ["taskkill", "/IM", proc, "/F"],
                                capture_output=True,
                                text=True,
                                timeout=10
                            )
                        except Exception:
                            pass
                        return

        except Exception:
            pass

    # -------------------------------------------------
    # Browser tools
    # -------------------------------------------------

    def open_url(self, url):
        webbrowser.open(url)

    def open_youtube(self):
        webbrowser.open("https://www.youtube.com")

    def search_youtube(self, query):
        url = (
            "https://www.youtube.com/results?"
            f"search_query={quote_plus(query)}"
        )
        webbrowser.open(url)

    def search_google(self, query):
        url = (
            "https://www.google.com/search?"
            f"q={quote_plus(query)}"
        )
        webbrowser.open(url)