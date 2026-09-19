import os
import re
import subprocess
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

# Values coming from LLM-generated plans are user- or model-controlled.
# Before interpolating an app name into a PowerShell script or using it
# to build a command line, require it to be a plain safe identifier.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._()-]+$")

# --- Known install locations -------------------------------------------------
# Checked before shortcut scanning because a real executable path can be
# launched *and verified*, unlike a fuzzy Start Menu match.
_KNOWN_EXES = {
    "chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ],
    "google chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ],
    "microsoft edge": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ],
    "notepad": [r"%SystemRoot%\System32\notepad.exe"],
    "explorer": [r"%SystemRoot%\explorer.exe"],
    "file explorer": [r"%SystemRoot%\explorer.exe"],
    "cmd": [r"%SystemRoot%\System32\cmd.exe"],
    "command prompt": [r"%SystemRoot%\System32\cmd.exe"],
    "vs code": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
    ],
    "visual studio code": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
    ],
    "vscode": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
    ],
}

# Expected process-name fragment used to *verify* a launch actually happened.
# Without verification the old code reported success whenever PowerShell
# exited 0 — which it did even when it launched the wrong program.
_APP_PROCESS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "explorer": "explorer",
    "file explorer": "explorer",
    "cmd": "cmd",
    "command prompt": "cmd",
    "vs code": "code",
    "visual studio code": "code",
    "vscode": "code",
}

_ALIASES = {
    "opera gx": "opera gx",
    "opera": "opera",
    "chrome": "google chrome",
    "google chrome": "google chrome",
    "edge": "microsoft edge",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "notepad": "notepad",
    "discord": "discord",
    "whatsapp": "whatsapp",
}


def _process_fragment(app: str) -> str:
    """Process-name fragment to look for when verifying a launch."""
    canonical = _ALIASES.get(app, app)
    if canonical in _APP_PROCESS:
        return _APP_PROCESS[canonical]
    return re.sub(r"[^a-z0-9]", "", canonical) or canonical


class DesktopController:

    def __init__(self):
        self.apps = self.scan_apps()

    # -------------------------------------------------
    # Scan Start Menu shortcuts
    # -------------------------------------------------

    def scan_apps(self):
        folders = [
            Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
            Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")),
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
        query = _ALIASES.get(query, query)

        # Exact match
        if query in self.apps:
            return self.apps[query]

        # Partial match: prefer the shortest name that contains the query so
        # "whatsapp" cannot win against an unrelated longer entry first.
        candidates = [(name, path) for name, path in self.apps.items() if query in name]
        if candidates:
            candidates.sort(key=lambda item: (len(item[0]), item[0]))
            return candidates[0][1]

        return None

    # -------------------------------------------------
    # Launch verification
    # -------------------------------------------------

    @staticmethod
    def _running_processes():
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        names = []
        for line in result.stdout.splitlines():
            parts = line.strip().strip('"').split('","')
            if parts and parts[0]:
                names.append(parts[0].strip().strip('"').lower())
        return names

    def _process_running(self, fragment: str) -> bool:
        fragment = fragment.lower()
        if not fragment:
            return False
        return any(fragment in name for name in self._running_processes())

    def _wait_for_process(self, fragment: str, timeout: float = 8.0) -> bool:
        """Poll until a matching process appears. Never claims success blindly."""
        deadline = time.monotonic() + timeout
        while True:
            if self._process_running(fragment):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    # -------------------------------------------------
    # Open any Windows app
    # -------------------------------------------------

    def open_app(self, app):
        if not isinstance(app, str):
            return False
        app = app.lower().strip()
        if not app or len(app) < 3 or not _SAFE_NAME.match(app):
            return False

        canonical = _ALIASES.get(app, app)
        fragment = _process_fragment(app)

        # 1. Known executable path (verified launch)
        for raw in _KNOWN_EXES.get(canonical, []):
            path = os.path.expandvars(raw)
            if not os.path.exists(path):
                continue
            try:
                subprocess.Popen([path])
            except OSError:
                continue
            if self._wait_for_process(fragment):
                return True

        # 2. Start Menu shortcut (verified launch)
        shortcut = self.find_app(app)
        if shortcut:
            try:
                os.startfile(shortcut)
            except OSError:
                pass
            else:
                if self._wait_for_process(fragment):
                    return True

        # 3. Microsoft Store / UWP app via its Start-app alias.
        # The query is passed through an ENVIRONMENT VARIABLE, not as a
        # trailing argument: `powershell -Command <script> <arg>` does not
        # populate $args, which previously made the filter match *every*
        # Start app and launch the alphabetically first one ("About Java").
        launched_name = self._launch_start_app(canonical)
        if launched_name is not None:
            if self._wait_for_process(fragment, timeout=10.0):
                return True
            # Start-Process was accepted but the window has not surfaced yet.
            # Report failure honestly rather than claiming a launch that
            # could not be confirmed.
            print(f"App launch unconfirmed: {canonical} -> {launched_name}")

        return False

    def _launch_start_app(self, query: str):
        """Launch a Start app by name. Returns the matched name, or None."""
        ps_script = (
            "$name = $env:JARVIS_APP_QUERY; "
            "$app = Get-StartApps | Where-Object { $_.Name -ieq $name } | Select-Object -First 1; "
            "if (-not $app) { "
            "  $app = Get-StartApps | Where-Object { $_.Name -like ('*' + $name + '*') } "
            "         | Sort-Object { $_.Name.Length } | Select-Object -First 1 "
            "} "
            "if ($app) { "
            "  Start-Process ('shell:AppsFolder\\' + $app.AppID); "
            "  Write-Output ('OK:' + $app.Name) "
            "} else { Write-Output 'NOMATCH' }"
        )
        env = dict(os.environ)
        env["JARVIS_APP_QUERY"] = query
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        output = (result.stdout or "").strip()
        if output.startswith("OK:"):
            return output[len("OK:"):].strip()
        return None

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
                timeout=10,
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
