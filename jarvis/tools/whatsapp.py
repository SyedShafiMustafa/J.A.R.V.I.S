"""
tools/whatsapp.py

Dedicated WhatsApp messaging integration for J.A.R.V.I.S.

Provides end-to-end handling for parsing, opening, locating contact,
entering message, sending, verifying delivery, and returning structured results.
"""

from __future__ import annotations

import re
import time
import logging
from difflib import SequenceMatcher
from typing import Any

from backend.interfaces import ToolResult
from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision

_log = logging.getLogger("jarvis.whatsapp")


# Leading politeness / transcript artefacts stripped before parsing, so
# "Can you please open WhatsApp and text Mom hi?" parses like the bare request.
_LEADING_FILLERS = (
    r"^(?:hey|ok|okay|so|alright)[,\s]+jarvis[,\s]+",
    r"^jarvis[,\s]+",
    r"^(?:can|could|would|will)\s+you\s+please\s+",
    r"^(?:can|could|would|will)\s+you\s+",
    r"^please\s+",
    r"^i\s+(?:want|need)\s+you\s+to\s+",
    r"^i\s+(?:want|need)\s+to\s+",
    r"^go\s+ahead\s+and\s+",
    r"^(?:you|u|he|she)\s+",
)

_WHATSAPP_VERB = re.compile(r"\b(send|message|msg|text|whatsapp)\b", re.IGNORECASE)

# Grammar noise that can never be part of a contact name.
_RECIPIENT_NOISE = {
    "a", "an", "the", "to", "on", "whatsapp", "message", "msg", "me", "my",
    "and", "then", "that", "saying", "please", "for", "some",
}

# Words that almost always begin the *message* rather than the name, used to
# split "send Affan Bhaiyya this is a test" into a recipient and a message.
_MESSAGE_STARTERS = {
    "a", "an", "the", "this", "that", "it", "i", "we", "you", "they",
    "hello", "hi", "hey", "please", "thanks", "thank", "happy", "good",
    "message", "just", "sorry", "congrats", "congratulations",
}


def _similar(a: str, b: str) -> bool:
    """Cheap typo tolerance for contact names (STT often mangles one letter)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.85


# ── Name matching ───────────────────────────────────────────────────────────
#
# STT mangles names ("Mumma" -> "mamma", "Affan Bhaiyya" -> "a fan, bye ya")
# and WhatsApp itself resolves names fuzzily, so exact string equality is
# useless here. Matching is token based with a cheap phonetic key plus a typo
# ratio, and it always reports whether the match is confident enough to *send*
# to — an unsure match must ask the user instead of guessing.

_PHONETIC_GROUPS = (
    ("ph", "f"),
    ("ck", "k"),
    ("kh", "k"),
    ("gh", "g"),
    ("th", "t"),
    ("sh", "s"),
    ("q", "k"),
    ("z", "s"),
    ("x", "ks"),
)


def _phonetic(token: str) -> str:
    """Crude sound key: first letter + de-doubled consonants ("mamma"=="mumma")."""
    text = re.sub(r"[^a-z0-9]", "", (token or "").lower())
    if not text:
        return ""
    first, rest = text[0], text[1:]
    for a, b in _PHONETIC_GROUPS:
        rest = rest.replace(a, b)
    rest = re.sub(r"(.)\1+", r"\1", rest)
    rest = re.sub(r"[aeiou]", "", rest)
    return (first + rest)[:12]


def _token_similarity(a: str, b: str) -> float:
    """0..1 similarity for one name token."""
    a = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pa, pb = _phonetic(a), _phonetic(b)
    if len(pa) >= 2 and pa == pb:
        return 0.95
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= 0.85:
        return 0.9
    return round(ratio * 0.8, 3)


def name_tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if t]


def name_match(query: str, candidate: str) -> dict[str, Any]:
    """Score how well ``candidate`` names what the user asked for.

    ``confident`` means the query covers the candidate name and nearly all of
    the candidate is covered by the query — the bar for sending without asking.
    """
    q = name_tokens(query)
    c = name_tokens(candidate)
    empty = {"score": 0.0, "confident": False, "query_coverage": 0.0, "candidate_coverage": 0.0}
    if not q or not c:
        return empty

    if q == c:
        return {"score": 1.0, "confident": True, "query_coverage": 1.0, "candidate_coverage": 1.0}

    query_scores = [max(_token_similarity(t, u) for u in c) for t in q]
    candidate_scores = [max(_token_similarity(u, t) for t in q) for u in c]
    q_cov = sum(query_scores) / len(query_scores)
    c_cov = sum(candidate_scores) / len(candidate_scores)
    return {
        "score": round(0.7 * q_cov + 0.3 * c_cov, 3),
        "confident": q_cov >= 0.9 and c_cov >= 0.75,
        "query_coverage": round(q_cov, 3),
        "candidate_coverage": round(c_cov, 3),
    }


# ── Clarification replies ───────────────────────────────────────────────────

_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "one": 0, "1": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
}

_CANCEL_PHRASES = (
    "cancel", "never mind", "nevermind", "forget it", "leave it",
    "no thanks", "not that", "don't send", "dont send", "abort",
)

_AFFIRM_PHRASES = (
    "yes", "yeah", "yep", "yah", "haan", "han", "correct", "right",
    "that one", "that's the one", "thats the one", "ok", "okay", "sure",
    "go ahead", "confirm", "do it",
)

_CLARIFY_NEAR = 0.40


def dedupe_names(names: list[str]) -> list[str]:
    """Drop empty/duplicate candidates, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = " ".join(name_tokens(name)) or (name or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((name or "").strip())
    return out


def resolve_clarification_reply(text: str, options: list[str]) -> dict[str, Any] | None:
    """Interpret a user's answer to "which chat did you mean?".

    Returns ``{"cancelled": True}``, ``{"choice": <option>}``, or ``None`` when
    the utterance is not an answer to the question at all.
    """
    options = [o for o in (options or []) if o]
    low = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not low or not options:
        return None

    if any(phrase in low for phrase in _CANCEL_PHRASES):
        return {"cancelled": True}

    tokens = low.split()
    # Ordinals are only trusted on short answers ("the second one") so a long
    # new command containing "one" is not misread as a choice.
    if len(tokens) <= 5:
        for token in tokens:
            token = token.strip(".,!?")
            if token in _ORDINAL_WORDS:
                index = _ORDINAL_WORDS[token]
                if index < len(options):
                    return {"choice": options[index]}

    if len(options) == 1 and any(low == p or low.startswith(p) for p in _AFFIRM_PHRASES):
        return {"choice": options[0]}

    scored = sorted(
        ((name_match(text, option)["score"], option) for option in options),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.85 and best_score - runner_up >= 0.1:
        return {"choice": best}
    return None


def _strip_leading_filler(text: str) -> str:
    out = text.strip()
    changed = True
    while changed:
        changed = False
        for pattern in _LEADING_FILLERS:
            new = re.sub(pattern, "", out, count=1, flags=re.IGNORECASE).strip()
            if new != out:
                out = new
                changed = True
    return out


def parse_whatsapp_request(user_text: str) -> dict[str, str] | None:
    """Parse a recipient + message out of a natural-language WhatsApp request.

    Covers the phrasings people actually use, e.g.
    - "Send Ahmed 'reaching in 10 minutes' on WhatsApp"
    - "Can you send a message on whatsapp to Afan saying this is a test?"
    - "Send Mama a message on WhatsApp saying hello"
    - "Open WhatsApp and text Affan Bhaiyya a hello message"
    - "Message Ahmed Hello on WhatsApp"
    """
    if not user_text:
        return None
    raw = user_text.strip()
    if "whatsapp" not in raw.lower():
        # Require an explicit WhatsApp mention so unrelated "send X a mail"
        # style requests are not hijacked.
        return None

    body = _strip_leading_filler(raw)
    body = re.sub(
        r"^(?:open|launch|start)\s+whatsapp\s*(?:and|then|,)?\s*",
        "",
        body,
        flags=re.IGNORECASE,
    ).strip()
    if not body:
        return None

    message: str | None = None
    head = body

    # 1) double-quoted message
    m_dq = re.search(r"[\u201c\u201d\"]([^\"\u201c\u201d]{1,300})[\u201c\u201d\"]", body)
    if m_dq:
        message = m_dq.group(1).strip()
        head = body[: m_dq.start()]
    # 2) single-quoted message anchored at the end (tolerates apostrophes)
    if message is None:
        m_sq = re.search(r"'(.+?)'\s*(?:on\s+whatsapp)?\s*$", body, re.IGNORECASE)
        if m_sq:
            message = m_sq.group(1).strip()
            head = body[: m_sq.start()]
    # 3) "... saying <message>"
    if message is None:
        m_say = re.search(r"\bsaying\b\s+(.+)$", body, re.IGNORECASE)
        if m_say:
            message = m_say.group(1).strip()
            head = body[: m_say.start()]
    # 4) "... on whatsapp <message>" (only when real content follows)
    if message is None:
        m_on = re.search(r"\bon\s+whatsapp\b\s+(.+)$", body, re.IGNORECASE)
        if m_on:
            remainder = m_on.group(1).strip()
            if remainder and not remainder.lower().startswith("to "):
                message = remainder
                head = body[: m_on.start()]

    # "... saying hi on whatsapp": drop the trailing platform mention.
    if message is not None:
        message = re.sub(
            r"\s+on\s+whatsapp\s*$", "", message, flags=re.IGNORECASE
        ).strip()

    # Recipient sits between the verb and the message.
    verb = _WHATSAPP_VERB.search(head)
    tail = head[verb.end():] if verb else head
    tail = re.sub(r"\bon\s+whatsapp\b", " ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\bwhatsapp\b", " ", tail, flags=re.IGNORECASE)
    # "... a message" idiom: strip when trailing ("Mama a message") or leading
    # ("send a message to Mama"), but never from the middle so real content
    # like "a hello message" survives.
    tail = re.sub(r"\b(?:a|an|the)\s+message\b\s*$", " ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"^\s*(?:a|an|the)\s+message\b", " ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"^\s*message\b", " ", tail, flags=re.IGNORECASE)
    tail = re.sub(r"^\s*(?:to|for)\s+", "", tail, flags=re.IGNORECASE)
    tail = tail.strip(" ,.;:!?\"'")

    tokens = [t for t in tail.split() if t]
    while tokens and tokens[0].strip(".,!?").lower() in _RECIPIENT_NOISE:
        tokens.pop(0)

    if message is None:
        # No explicit delimiter ("text <name> <message>"): split where the
        # message plausibly starts, otherwise treat the first token as the name.
        split_at = None
        for i, tok in enumerate(tokens):
            if i >= 1 and tok.strip(".,!?").lower() in _MESSAGE_STARTERS:
                split_at = i
                break
        if split_at is not None:
            recipient = " ".join(tokens[:split_at])
            message = " ".join(tokens[split_at:])
        elif len(tokens) >= 2:
            recipient = tokens[0]
            message = " ".join(tokens[1:])
        else:
            return None
    else:
        recipient = " ".join(tokens)

    recipient = recipient.strip(" ,.;:!?\"'")
    message = (message or "").strip(" ,.;:!?\"'")
    if not recipient or not message:
        return None
    if len(recipient) < 2 or recipient.lower() in _RECIPIENT_NOISE:
        return None
    return {"recipient": recipient, "message": message}


class WhatsAppManager:
    """Manages WhatsApp Desktop interactions with multi-strategy automation and verification."""

    def __init__(
        self,
        desktop: DesktopController | None = None,
        computer: ComputerController | None = None,
        vision: ScreenVision | None = None,
    ) -> None:
        self.desktop = desktop or DesktopController()
        self.computer = computer or ComputerController()
        self.vision = vision or ScreenVision()
        # How long WhatsApp (a Store app, often cold-started) may take to reach
        # the foreground before we refuse to type anything.
        self.focus_timeout = 12.0

    def send_message(
        self,
        recipient: str,
        message: str,
        preferred: str | None = None,
    ) -> ToolResult:
        """
        Execute full WhatsApp messaging flow:
        1. Parse & validate inputs
        2. Open / focus WhatsApp Desktop
        3. Locate recipient conversation (Ctrl+F search)
        4. Enter message text
        5. Send message
        6. Verify execution
        """
        if not recipient or not recipient.strip():
            return ToolResult(
                "send_whatsapp",
                False,
                "Recipient name is required",
                {"started": False, "completed": False, "verified": False},
            )
        if not message or not message.strip():
            return ToolResult(
                "send_whatsapp",
                False,
                "Message content is required",
                {"started": False, "completed": False, "verified": False},
            )

        recipient = recipient.strip()
        message = message.strip()
        # `preferred` is the exact chat name the user picked when the request
        # was ambiguous; it targets that row instead of re-searching blind.
        target = (preferred or recipient).strip()

        _log.info("Starting WhatsApp message flow to '%s' (target=%r)", recipient, target)

        # 1. WhatsApp must actually be the ACTIVE window before any typing.
        #    A fixed sleep was not enough: during a cold start the keystrokes
        #    used to land in whatever application happened to be in front.
        if not self._ensure_focused():
            _log.info("WhatsApp never became active; aborting before typing")
            return ToolResult(
                "send_whatsapp",
                False,
                "I couldn't bring WhatsApp to the front, so I didn't type anything. "
                "Open WhatsApp once and ask me again.",
                {"started": True, "completed": False, "verified": False, "stage": "focus"},
            )

        # 2. Locate the conversation (clipboard paste is instant and unicode-safe).
        self.computer.hotkey("ctrl", "f")
        self.computer.hotkey("ctrl", "a")
        self.computer.press("backspace")
        self.computer.set_clipboard(target)
        self.computer.paste()
        # Remember the header BEFORE opening the result: the pane still shows
        # the previous chat, so waiting for it to change avoids acting on a
        # stale name.
        previous_header = self._read_header().strip()
        self.computer.press("enter")
        header = self._wait_for_header_change(previous_header, timeout=0.7)

        # 3. Confirm the OPENED conversation is actually the intended person
        # before touching the composer. Search is fuzzy: typing "Ahmed" can
        # open "Vigar Ahmed Munawar". The message body is never typed until
        # this passes.
        if not self._conversation_matches(header, target):
            outcome = self._resolve_recipient(target, header)

            if outcome["kind"] == "ask":
                _log.info(
                    "WhatsApp needs clarification for %r (header=%r) candidates=%s",
                    recipient, header, outcome["candidates"],
                )
                return ToolResult(
                    "send_whatsapp",
                    False,
                    outcome["question"],
                    {
                        "started": True,
                        "completed": False,
                        "verified": False,
                        "stage": "needs_clarification",
                        "needs_clarification": True,
                        "candidates": outcome["candidates"],
                        "recipient": recipient,
                        "message": message,
                        "header": header,
                    },
                )

            if outcome["kind"] == "refuse":
                _log.info(
                    "WhatsApp conversation guard refused recipient=%r header=%r",
                    recipient, header,
                )
                return ToolResult(
                    "send_whatsapp",
                    False,
                    outcome["question"],
                    {
                        "started": True,
                        "completed": False,
                        "verified": False,
                        "stage": "conversation_match",
                        "header": header,
                        "recipient": recipient,
                    },
                )

            target = outcome["target"]
            header = outcome["header"]

        # 4. Focus the message box, then enter the text.
        clicked_box = False
        try:
            clicked_box = self.vision.click_text("message_box")
        except Exception:
            clicked_box = False

        if not clicked_box:
            # Manual fallback click near bottom center of active window
            active_win = self.computer.get_active_window()
            if active_win and "whatsapp" in active_win.lower():
                self.computer.click()

        # Clipboard paste safely supports special characters, quotes, newlines.
        self.computer.set_clipboard(message)
        self.computer.paste()
        self._wait_for_composer(message, timeout=0.9)

        # Press Enter to send.
        self.computer.press("enter")

        # 5. Verification — only claim delivery on actual observed evidence,
        #    re-checked briefly so a slow render is not reported as failure.
        verified, evidence = self._poll_verified(message, timeout=1.2)

        _log.info(
            "WhatsApp flow to '%s' finished. verified=%s evidence=%s",
            target, verified, evidence,
        )

        if verified:
            return ToolResult(
                "send_whatsapp",
                True,
                f"Message sent to {target} on WhatsApp.",
                {
                    "started": True,
                    "completed": True,
                    "verified": True,
                    "evidence": evidence,
                    "recipient": target,
                    "message": message,
                },
            )

        # The keystrokes ran, but nothing on screen confirms the send.
        # Reporting failure (not fake success) is the honest outcome.
        return ToolResult(
            "send_whatsapp",
            False,
            f"I typed the message to {target}, but could not confirm it was sent.",
            {
                "started": True,
                "completed": False,
                "verified": False,
                "evidence": evidence,
                "recipient": target,
                "message": message,
            },
        )

    # ------------------------------------------------------------------
    # pacing — wait for the UI to reach the wanted state instead of
    # sleeping a fixed amount (faster when it is ready, correct when it is not)
    # ------------------------------------------------------------------

    @staticmethod
    def _wait_until(predicate, timeout: float, interval: float = 0.12):
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            value = predicate()
            if value or time.monotonic() >= deadline:
                return value
            time.sleep(interval)

    def _is_whatsapp_active(self) -> bool:
        try:
            window = self.computer.get_active_window()
        except Exception:
            return False
        return bool(window) and "whatsapp" in str(window).lower()

    def _ensure_focused(self, timeout: float | None = None) -> bool:
        """Bring WhatsApp to the front, launching it once if it is not open."""
        if timeout is None:
            timeout = self.focus_timeout
        if self._is_whatsapp_active():
            return True

        try:
            self.computer.focus_window("WhatsApp")
        except Exception:
            pass

        launched = False
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._is_whatsapp_active():
                return True
            if not launched:
                launched = True
                try:
                    self.desktop.open_app("whatsapp")
                except Exception:
                    _log.debug("whatsapp launch failed", exc_info=True)
            try:
                self.computer.focus_window("WhatsApp")
            except Exception:
                pass
            time.sleep(0.3)
        return self._is_whatsapp_active()

    # ------------------------------------------------------------------
    # recipient resolution
    # ------------------------------------------------------------------

    def _sidebar_rows(self) -> list[dict[str, Any]]:
        reader = getattr(self.vision, "read_sidebar_rows", None)
        if not callable(reader):
            return []
        try:
            rows = reader() or []
        except Exception:
            _log.debug("sidebar OCR failed", exc_info=True)
            return []
        return [r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip()]

    def _wait_for_sidebar(self, timeout: float) -> list[dict[str, Any]]:
        if not callable(getattr(self.vision, "read_sidebar_rows", None)):
            return []
        return self._wait_until(self._sidebar_rows, timeout) or []

    def _wait_for_header(self, timeout: float) -> str:
        return self._wait_until(lambda: self._read_header().strip(), timeout) or ""

    def _wait_for_header_change(self, previous: str, timeout: float) -> str:
        """Wait for the conversation header to stop showing the previous chat."""
        deadline = time.monotonic() + max(0.0, timeout)
        latest = self._read_header().strip()
        while latest == previous and time.monotonic() < deadline:
            time.sleep(0.12)
            latest = self._read_header().strip()
        return latest

    def _wait_for_composer(self, message: str, timeout: float) -> bool:
        reader = getattr(self.vision, "read_composer", None)
        if not callable(reader):
            return False
        needle = " ".join(message.lower().split())[:30]

        def _seen() -> bool:
            try:
                text = (reader() or "").lower()
            except Exception:
                return False
            return needle in text if needle else bool(text)

        return bool(self._wait_until(_seen, timeout))

    def _poll_verified(self, message: str, timeout: float) -> tuple[bool, str]:
        deadline = time.monotonic() + max(0.0, timeout)
        verified, evidence = self._verify_sent(message)
        while not verified and time.monotonic() < deadline:
            time.sleep(0.25)
            verified, evidence = self._verify_sent(message)
        return verified, evidence

    @staticmethod
    def _unique_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            key = " ".join(name_tokens(row["name"]))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    @classmethod
    def _near_rows(
        cls, rows: list[dict[str, Any]], query: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        scored = sorted(
            ((name_match(query, r["name"])["score"], r) for r in rows),
            key=lambda pair: pair[0],
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for score, row in scored:
            if score < _CLARIFY_NEAR:
                break
            out.append(row)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _join_names(names: list[str]) -> str:
        if len(names) == 1:
            return f"'{names[0]}'"
        if len(names) == 2:
            return f"'{names[0]}' or '{names[1]}'"
        return ", ".join(f"'{n}'" for n in names[:-1]) + f", or '{names[-1]}'"

    def _resolve_recipient(self, query: str, header: str) -> dict[str, Any]:
        """Decide what to do when the opened chat is not confidently the target.

        Search is fuzzy and WhatsApp happily shows several chats, contacts and
        groups whose names all partly match. Instead of guessing or flatly
        refusing, this opens the single unambiguous match or asks which one the
        user meant — never sending while unsure.
        """
        rows = self._drop_query_echo(self._unique_rows(self._sidebar_rows()), query)

        matches = [row for row in rows if self._conversation_matches(row["name"], query)]

        if len(matches) == 1:
            row = matches[0]
            self._click_row(row)
            opened = self._wait_for_header(timeout=1.2) or self._read_header()
            if self._conversation_matches(opened, row["name"]):
                _log.info("WhatsApp opened row %r (header was %r)", row["name"], header)
                return {"kind": "ok", "target": row["name"], "header": opened}

        if len(matches) >= 2:
            names = dedupe_names([r["name"] for r in matches])[:5]
            question = (
                f"I found {len(names)} WhatsApp chats that match '{query}': "
                f"{self._join_names(names)}. Which one should I message?"
            )
            return {"kind": "ask", "question": question, "candidates": names}

        near = self._near_rows(rows, query)
        if near:
            names = dedupe_names([r["name"] for r in near])[:3]
            question = (
                f"I couldn't find a WhatsApp chat called '{query}'. "
                f"Did you mean {self._join_names(names)}?"
            )
            return {"kind": "ask", "question": question, "candidates": names}

        found = " ".join(self._name_token_list(header)).title() or header.strip()
        if not found:
            return {
                "kind": "refuse",
                "question": (
                    f"I couldn't find a WhatsApp chat matching '{query}', so I "
                    "didn't send anything. Say the contact's saved name and "
                    "I'll try again."
                ),
            }
        return {
            "kind": "refuse",
            "question": (
                f"I opened a WhatsApp chat called '{found}', but you asked for "
                f"'{query}', so I didn't send anything. Say the contact's "
                "exact saved name and I'll try again."
            ),
        }

    def _drop_query_echo(
        self, rows: list[dict[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        """Ignore the search box's own text, which OCR reads as a top row.

        Without this the query itself looks like a perfect candidate at the top
        of the pane and can be mistaken for a matching chat.
        """
        if not rows:
            return rows
        needle = " ".join(name_tokens(query))
        if not needle:
            return rows
        try:
            bounds = self.vision.window_bounds()
        except Exception:
            bounds = None
        if not bounds:
            return rows
        _left, top, _width, height = bounds
        # The search field sits at the very top of the pane; real results start
        # below it. OCR glues nearby glyphs onto the box text, so match on the
        # query tokens being contained rather than on exact equality.
        cutoff = top + height * 0.18
        query_tokens = set(name_tokens(query))
        return [
            row for row in rows
            if not (
                row.get("y", 0) <= cutoff
                and query_tokens
                and query_tokens <= set(name_tokens(row.get("name", "")))
            )
        ]

    def _click_row(self, row: dict[str, Any]) -> None:
        try:
            self.computer.click(row.get("x"), row.get("y"))
        except Exception:
            _log.debug("sidebar row click failed", exc_info=True)

    def _read_header(self) -> str:
        """Best-effort OCR of the open conversation's header (contact name)."""
        read_header = getattr(self.vision, "read_header", None)
        if not callable(read_header):
            return ""
        try:
            return read_header() or ""
        except Exception:
            return ""

    # UI chrome that OCR picks up around the real contact name in the header
    # strip. Filtered out before comparing, so "WhatsApp  Shafi Ahmed" still
    # matches the recipient "Shafi Ahmed".
    _UI_NOISE = {
        "whatsapp", "chat", "chats", "search", "all", "unread", "favourites",
        "favorites", "archived", "wa", "meta", "ai", "menu", "back", "video",
        "call", "mute", "delete", "archive",
        # conversation-header hint text
        "click", "here", "for", "contact", "info", "to", "open", "profile",
        "you", "yourself", "message", "messages", "typing",
    }

    @staticmethod
    def _normalize_name(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    @classmethod
    def _name_tokens(cls, text: str) -> set[str]:
        return {
            tok for tok in cls._normalize_name(text).split()
            if tok and tok not in cls._UI_NOISE
        }

    @classmethod
    def _name_token_list(cls, text: str) -> list[str]:
        """Ordered, noise-filtered name tokens (order is meaningful here)."""
        return [
            tok for tok in cls._normalize_name(text).split()
            if tok and tok not in cls._UI_NOISE
        ]

    @classmethod
    def _conversation_matches(cls, header_text: str, recipient: str) -> bool:
        """True only when the header confidently names the intended recipient.

        Rules, safest first:
        - exact token-set equality (order-insensitive, UI noise removed);
        - the user gave a *fuller* name than the saved contact;
        - the user gave a full multi-token name fully present in the header;
        - the query and the header name each cover the other (typo/phonetic
          tolerant, so "mumma"/"Mama" and "afan"/"Affan" still match);
        - the header has one or two name tokens and the single token asked for
          is that name (e.g. "Mama" -> "Bhaiyya Mama"), with typo tolerance.

        A single token against a three-or-more-token header is never
        confident, which keeps the dangerous "Ahmed" -> "Vigar Ahmed Munawar"
        case from messaging the wrong person. A false negative (asking or
        refusing) is always safer than a false positive.
        """
        header = cls._name_tokens(header_text)
        target = cls._name_tokens(recipient)
        if not header or not target:
            return False
        if header == target:
            return True
        if header <= target:
            return True
        if target <= header and len(target) >= 2:
            return True
        # Directional similarity: the query must cover the candidate name and
        # the candidate must be nearly covered by the query, so a bare first
        # name never confidently matches a longer, different person.
        if name_match(recipient, header_text)["confident"]:
            return True

        if len(target) == 1:
            header_list = cls._name_token_list(header_text)
            only = next(iter(target))
            if len(header_list) == 1:
                # "mamma" -> "Mumma", "afan" -> "Affan"
                return _token_similarity(only, header_list[0]) >= 0.9
            if len(header_list) == 2:
                last = header_list[-1]
                if only == last or _similar(only, last):
                    return True
                return _token_similarity(only, last) >= 0.9
        return False

    def _verify_sent(self, message: str) -> tuple[bool, str]:
        """Return (verified, evidence) with the strongest signal available.

        A focused WhatsApp window alone is NOT delivery evidence. We only
        report success when the sent text is actually visible in the
        conversation (via OCR), or fail honestly when we cannot observe it.
        """
        active_window = self.computer.get_active_window()
        if not active_window or "whatsapp" not in active_window.lower():
            return False, "whatsapp_not_focused"

        needle = " ".join(message.lower().split())[:40]

        # If the text is still sitting in the input row, Enter did not send it.
        # (The composer is part of the full-screen OCR, so without this a typed
        # draft would read as a delivered message.)
        read_composer = getattr(self.vision, "read_composer", None)
        if callable(read_composer):
            try:
                composer = (read_composer() or "").lower()
            except Exception:
                composer = ""
            if needle and needle in composer:
                return False, "message_still_in_composer"

        read_screen = getattr(self.vision, "read_screen", None)
        if callable(read_screen):
            try:
                screen = read_screen() or {}
            except Exception:
                screen = {}
            text = (screen.get("text") or "").lower()
            if needle and needle in text:
                return True, "message_visible_in_conversation"

        return False, "no_visual_confirmation"
