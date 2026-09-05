import os
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus


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
        try:
            ps_script = f"""
            $app = Get-StartApps |
                Where-Object {{$_.Name -like '*{app}*'}} |
                Select-Object -First 1

            if ($app) {{
                Start-Process ("shell:AppsFolder\\" + $app.AppID)
            }}
            """

            result = subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True,
                text=True
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
        os.system(f'taskkill /IM "{app}.exe" /F >nul 2>&1')

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