"""
core/router.py

Fast-path command router: simple open/close application requests are
handled directly (launch + focus verification), everything else is
deferred to the planner / brain.

Intent precedence (explicit, in order):

1. on-screen GUI / menu intent  -> defer, never launch an app
   ("open the File menu", "close the Settings menu")
2. terminal / shell / script     -> defer
3. file-management batch work    -> defer (unless it is about windows)
4. multi-step computer use       -> defer
5. close application             -> handle
6. open application              -> handle (with launch + focus verify)
7. anything else                 -> defer

All matching is token- or phrase-based with word boundaries. Naive
substring tests (``"and" in text``, ``"cmd" in text``) previously
misrouted ordinary chat ("Thailand", "understand", "disclose") and,
worse, treated the File *menu* as a launchable app.
"""

import re

from tools.computer import ComputerController
from tools.desktop_control import DesktopController

desktop = DesktopController()
computer = ComputerController()

_FOCUS_TIMEOUT_S = 8.0


def _tokens(text: str) -> set[str]:
    """Lower-cased word tokens (``[a-z0-9]+``); substrings never match."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _has_phrase(text: str, phrase: str) -> bool:
    """Whole-phrase match with word boundaries (multi-word safe)."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def _strip_fillers(app: str, fillers: list[str]) -> str:
    for filler in fillers:
        app = re.sub(r"\b" + re.escape(filler) + r"\b", " ", app)
    app = re.sub(r"\s+", " ", app).strip(" ?.!").strip()
    return app


# Verbs that address something already on screen (a menu, dialog or
# control) rather than launching a program.
_MENU_VERBS = {"open", "close", "click", "press", "show", "hide",
               "expand", "collapse", "dismiss", "toggle", "select"}

# Words proving the request is about windows, not file batches.
_WINDOW_WORDS = {"window", "windows", "screen", "chrome", "notepad",
                 "browser", "app", "application", "maximize", "minimize",
                 "restore", "fullscreen", "snap", "left", "right", "monitor"}

# Terminal / shell deferral. Single words match as tokens, phrases match
# with boundaries — "cmd" must not fire inside ordinary words.
_TERMINAL_WORDS = {"terminal", "shell", "powershell", "bash", "script",
                   "command prompt", "command line", "the command"}
_TERMINAL_TOKENS = {"cmd", "python", "pip", "echo", "git"}

# File-batch deferral (organizer territory).
_FILE_WORDS = {"organize", "organise", "tidy", "cleanup", "declutter",
               "archive", "duplicate", "clean up", "sort", "arrange"}

# Multi-step computer-use deferral (planner territory). Token-matched so
# "and" no longer fires inside "Thailand"/"standard"/"command".
_COMPLEX_TOKENS = {"message", "text", "send", "write", "type", "search",
                   "youtube", "google", "click", "scroll", "press", "and"}

_CLOSE_WORDS = {"close", "quit", "exit"}
_OPEN_VERBS = ("open", "launch", "start", "bring up")

_FILLERS = [
    "can you", "could you", "would you", "please", "for me",
    "i want to", "i wanna", "let me",
    "open", "launch", "start", "bring up",
    "my", "the",
]

# After filler stripping, these name files/documents — not apps.
_FILE_HINTS = {"file", "files", "folder", "folders", "directory",
               "document", "pdf", "this", "that"}

# Launchable app aliases that must keep working through the fast path.
_APP_ALIASES = {
    "file explorer", "explorer", "chrome", "edge", "notepad",
    "vs code", "vscode", "whatsapp", "discord", "spotify",
}


def _is_menu_intent(text: str, tokens: set[str]) -> bool:
    """"open/close the File menu" addresses on-screen UI, not an app."""
    if "menu" not in tokens and "menubar" not in tokens:
        return False
    return bool(tokens & _MENU_VERBS)


def _is_terminal_request(text: str, tokens: set[str]) -> bool:
    low = text.lower()
    if any(_has_phrase(low, phrase) for phrase in _TERMINAL_WORDS):
        return True
    return bool(tokens & _TERMINAL_TOKENS)


def _is_file_batch_request(text: str, tokens: set[str]) -> bool:
    if tokens & _WINDOW_WORDS:
        return False
    low = text.lower()
    return any(_has_phrase(low, phrase) for phrase in _FILE_WORDS)


class CommandRouter:

    def route(self, text: str):
        original = text
        text = text.lower().strip()
        tokens = _tokens(text)

        # 1. On-screen GUI / menu intent: NEVER launch. The planner owns
        # menus via locate/visual_click/visual_menu — not open_app.
        if _is_menu_intent(text, tokens):
            return False, None

        # 2-4. Deferrals: shell, file batches, multi-step computer use.
        if _is_terminal_request(text, tokens):
            return False, None
        if _is_file_batch_request(text, tokens):
            return False, None
        if tokens & _COMPLEX_TOKENS:
            return False, None

        # 5. Close application (whole-word verbs only).
        if tokens & _CLOSE_WORDS:
            match = re.search(r"\b(close|quit|exit)\b", text)
            app = _strip_fillers(text[match.end():], [
                "can you", "could you", "would you", "please", "for me",
                "i want to", "i wanna", "let me",
                "close", "quit", "exit", "my", "the",
            ])
            if app:
                if desktop.close_app(app):
                    return True, f"Closed {app}."
                return True, f"I couldn't find an open app called '{app}'."

        # 6. Open application ("run"/"use" are not launches: "run a
        # workflow" or "use Chrome to search" belong to the planner).
        first_token = (text.split(" ", 1) + [""])[0]
        if first_token in ("open", "launch", "start") \
                or text.startswith("bring up"):
            app = _strip_fillers(text, _FILLERS)
            if not app:
                return False, None
            app_tokens = _tokens(app)
            # File/document intent goes to the planner (inspect/create),
            # unless it names a launchable app (File Explorer).
            if app not in _APP_ALIASES and (
                    app_tokens & _FILE_HINTS or "." in app):
                return False, None
            if not desktop.open_app(app):
                hint = ""
                if "menu" in _tokens(original):
                    hint = (" If you meant a menu bar item like the File "
                            "menu, just say 'open the File menu' and I'll "
                            "click it in the current window.")
                return True, f"I couldn't find an app called '{app}'.{hint}"
            # Launched: verify the window actually comes forward.
            if computer.wait_for_window(app, timeout=_FOCUS_TIMEOUT_S):
                return True, f"{app.title()} is now open."
            return (True,
                    f"{app.title()} is open, but I couldn't bring its "
                    f"window forward.")

        return False, None
