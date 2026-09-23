"""
core/intent.py

Action-vs-explanation intent model for J.A.R.V.I.S.

JARVIS is an operating agent, not a tutorial bot: an imperative request
that names an executable capability must EXECUTE (route to a tool), and
only an explicit request for instructions may be answered with a how-to.

The distinction is semantic and global (not per-command phrase hacks):

- grammatical mood first: explicit how-to / why / what-happens /
  capability questions are EXPLAIN, even when they name tools
  ("How do I maximize Chrome?", "Can Chrome be maximized?")
- otherwise an imperative verb heading the request (after polite
  wrappers are stripped) is ACT — including polite forms ("Please open
  Chrome.", "Can you move this file?")
- anything else is CHAT (normal conversation, no tools involved)

Matching is token-based with word boundaries throughout; naive
substring tests ("and" in text) previously turned ordinary chat like
"Thailand" or "I already read that" into tool tasks.
"""

from __future__ import annotations

import re

# Every currently executable capability family contributes its verbs.
# Deliberately NOT included: bare words with no tool behind them whose
# chat meaning dominates ("update", bare "status"/"log"/"read").
ACTION_VERBS = frozenset({
    # applications
    "open", "launch", "start", "close", "quit", "restart",
    "focus", "switch", "make",
    # windows
    "maximize", "maximized", "max", "minimize", "minimized",
    "restore", "snap", "arrange", "resize", "move", "rename",
    # visual computer use
    "screenshot", "inspect", "locate", "click", "drag", "scroll",
    "type", "press", "verify", "find",
    # filesystem
    "create", "make", "file", "folder", "directory", "delete",
    "remove", "copy", "list", "save", "archive",
    # terminal / dev / git
    "run", "execute", "terminal", "command", "shell", "git",
    "commit", "checkpoint", "checkout", "stage", "push", "pull",
    "python", "script", "test", "build", "lint", "debug", "fix",
    "install", "uninstall", "navigate",
    # messaging / media
    "play", "write", "message", "send",
    # organizing
    "organize", "organise", "sort", "tidy", "declutter",
    "duplicate", "clean", "cleanup",
    # scheduler
    "remind", "reminder", "reminders", "schedule", "scheduled",
    "alarm", "cancel",
    # cross-app / transfer
    "paste", "transfer", "extract", "transform",
    # generic operating verbs
    "set", "change", "turn", "enable", "disable", "stop", "put",
    "bring",
})

# Polite wrappers around an imperative core. Stripped before verb
# analysis so "Can you open Chrome?" reads as "open Chrome" (ACT),
# while the question form stays intact for the how-to check first.
_POLITE_PREFIXES = (
    r"^(?:please|kindly|hey jarvis[,\s]*|jarvis[,\s]*)\b[\s,]*",
    r"^(?:can|could|would|will)\s+you\b[\s,]*",
    r"^(?:would|do)\s+you\s+mind\b[\s,]*",
)

# Explicit requests for instruction. Checked BEFORE verbs so
# "How do I maximize Chrome?" explains even though "maximize" acts.
# A trailing "?" alone is NOT a signal ("Maximize Chrome?" still acts).
_EXPLAIN_PATTERNS = (
    r"^\s*how\s+(do|does|can|could|would|should|to)\b",
    r"\bteach\s+me\b",
    r"\bshow\s+me\s+how\b",
    r"\bwalk\s+me\s+through\b",
    r"\bexplain\b",
    r"\bgive\s+me\s+instructions\b",
    r"^\s*why\b",
    r"\bwhat\s+happens\s+if\b",
    r"\bcan\b[^?.!]{0,60}\bbe\s+\w+(ed|en)\b",
    r"\bis\s+it\s+possible\b",
    r"\bis\s+there\s+a\s+way\b",
)

# Bare repeat requests name no target; they still act (the planner gets
# the last action via experience lessons), never explain.
_REPEAT_PATTERNS = (
    r"^\s*(do|repeat)\s+(that|it|this)\s+again\s*[.?!]?\s*$",
    r"^\s*again\s*[.?!]?\s*$",
    r"^\s*one\s+more\s+time\s*[.?!]?\s*$",
)


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def strip_politeness(text: str) -> str:
    core = (text or "").strip()
    lowered = core.lower()
    for pattern in _POLITE_PREFIXES:
        stripped = re.sub(pattern, "", lowered, count=1).strip(" ,")
        if stripped != lowered:
            lowered = stripped
    # Trailing "for me / please" does not change the mood.
    lowered = re.sub(r"\bfor\s+me\s*[.?!]?\s*$", "", lowered).strip()
    lowered = re.sub(r"\bplease\s*[.?!]?\s*$", "", lowered).strip()
    return lowered


def is_explanation_request(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(pattern, low) for pattern in _EXPLAIN_PATTERNS)


def is_repeat_request(text: str) -> bool:
    low = (text or "").lower()
    return any(re.match(pattern, low) for pattern in _REPEAT_PATTERNS)


def classify(text: str) -> str:
    """Return ``"act"``, ``"explain"`` or ``"chat"`` for an utterance."""
    if not (text or "").strip():
        return "chat"
    if is_explanation_request(text):
        return "explain"
    if is_repeat_request(text):
        return "act"
    core = strip_politeness(text)
    words = re.findall(r"[a-z0-9]+", core)
    if words and words[0] in ACTION_VERBS:
        return "act"
    if tokens(core) & ACTION_VERBS:
        return "act"
    return "chat"


class IntentClassifier:
    """Trinary intent classifier (legacy name, current model)."""

    def classify(self, text: str):
        return classify(text)
