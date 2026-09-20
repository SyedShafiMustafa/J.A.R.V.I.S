import re
from tools.desktop_control import DesktopController

desktop = DesktopController()


class CommandRouter:

    def route(self, text: str):
        original = text
        text = text.lower().strip()

        # ==================================================
        # COMPLEX TASKS → LET THE PLANNER HANDLE THEM
        # ==================================================

        # ==================================================
        # TERMINAL / SCRIPT / SHELL → LET THE PLANNER HANDLE THEM
        # ==================================================
        # "run the command ...", "open a terminal and ...", "execute the
        # python script" are computer-use requests, not "open an app".
        # Deferring them keeps the fast app-open shortcut from swallowing a
        # real shell command as an application name.
        terminal_markers = [
            "terminal", "command prompt", "command line", " the command ",
            "shell", "cmd", "powershell", "bash", "script",
            "python ", "pip ", "echo ", "git ", "git status", "git diff",
            "git log", "git commit", "&&", "|",
        ]
        if any(marker in text for marker in terminal_markers):
            return False, None

        # File-management / duplicate requests are computer-use tasks, not
        # "open an app". Defer them so "organize my downloads" is never
        # swallowed by the app-open shortcut as an app called "my downloads".
        file_markers = [
            "organize", "organise", "sort", "tidy", "clean up", "cleanup",
            "declutter", "arrange", "archive", "duplicate",
        ]
        if any(marker in text for marker in file_markers):
            return False, None

        complex_words = [
            "message",
            "text",
            "send",
            "write",
            "type",
            "search",
            "youtube",
            "google",
            "click",
            "scroll",
            "press",
            "and"
        ]

        if any(word in text for word in complex_words):
            return False, None

        # ==================================================
        # CLOSE APPLICATION
        # ==================================================

        if any(w in text for w in ["close", "quit", "exit"]):

            app = re.sub(r".*?(close|quit|exit)", "", text)
            app = app.strip(" ?.!")

            if app:
                desktop.close_app(app)
                return True, f"Closing {app.title()}."

        # ==================================================
        # SIMPLE OPEN APPLICATION ONLY
        # ==================================================

        open_keywords = [
            "open",
            "launch",
            "start",
            "run",
            "bring up",
            "use",
        ]

        if any(text.startswith(k) for k in open_keywords):

            app = text

            fillers = [
                "can you",
                "could you",
                "would you",
                "please",
                "for me",
                "i want to",
                "i wanna",
                "let me",
                "open",
                "launch",
                "start",
                "run",
                "bring up",
                "use",
                "my",
                "the",
            ]

            for f in fillers:
                app = app.replace(f, "")

            app = app.strip(" ?.!")

            if desktop.open_app(app):
                return True, f"Opening {app.title()}."
            else:
                return True, f"I couldn't find {app.title()}."

        return False, None