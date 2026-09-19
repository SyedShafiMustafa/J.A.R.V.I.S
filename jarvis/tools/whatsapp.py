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
from typing import Any

from backend.interfaces import ToolResult
from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision

_log = logging.getLogger("jarvis.whatsapp")


def parse_whatsapp_request(user_text: str) -> dict[str, str] | None:
    """
    Parse recipient and message content from natural language requests.

    Examples:
    - "Send Ahmed 'I'm reaching in 10 minutes' on WhatsApp"
    - "Message Ahmed Hello on WhatsApp"
    - "Open WhatsApp and message Mom Happy Birthday"
    """
    text = user_text.strip()

    # Pattern 1: Double quotes
    m_dq = re.search(
        r"(?:send|message)\s+([A-Za-z0-9 _-]+?)\s+\"([^\"]+)\"(?:\s+on\s+whatsapp)?",
        text,
        re.IGNORECASE,
    )
    if m_dq:
        return {"recipient": m_dq.group(1).strip(), "message": m_dq.group(2).strip()}

    # Pattern 2: Single quotes
    m_sq = re.search(
        r"(?:send|message)\s+([A-Za-z0-9 _-]+?)\s+'(.+?)'(?:\s+on\s+whatsapp)?$",
        text,
        re.IGNORECASE,
    )
    if m_sq:
        return {"recipient": m_sq.group(1).strip(), "message": m_sq.group(2).strip()}

    # Pattern 2: Send <recipient> <message> on WhatsApp
    m2 = re.search(
        r"(?:send|message)\s+([A-Za-z0-9_-]+)\s+(.+?)\s+on\s+whatsapp",
        text,
        re.IGNORECASE,
    )
    if m2:
        return {"recipient": m2.group(1).strip(), "message": m2.group(2).strip()}

    # Pattern 3: WhatsApp [and] message <recipient> <message>
    m3 = re.search(
        r"whatsapp.*?(?:message|send)\s+([A-Za-z0-9_-]+)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if m3:
        return {"recipient": m3.group(1).strip(), "message": m3.group(2).strip()}

    return None


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

    def send_message(self, recipient: str, message: str) -> ToolResult:
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

        _log.info("Starting WhatsApp message flow to '%s'", recipient)

        # 1. Resolve & Open WhatsApp
        opened = self.desktop.open_app("whatsapp")
        if not opened:
            # Fallback check if WhatsApp window is already open
            if not self.computer.focus_window("WhatsApp"):
                return ToolResult(
                    "send_whatsapp",
                    False,
                    "Failed to open or find WhatsApp application",
                    {"started": True, "completed": False, "verified": False, "stage": "open_app"},
                )

        time.sleep(0.5)
        self.computer.focus_window("WhatsApp")
        time.sleep(0.3)

        # 2. Focus Search & locate recipient
        # Shortcut Ctrl+F or Ctrl+E focuses search box in WhatsApp Desktop
        self.computer.hotkey("ctrl", "f")
        time.sleep(0.2)
        # Clear existing search input if any
        self.computer.hotkey("ctrl", "a")
        self.computer.press("backspace")
        time.sleep(0.1)
        self.computer.type_text(recipient)
        time.sleep(0.5)
        self.computer.press("enter")
        time.sleep(0.8)

        # 2b. Confirm the OPENED conversation is actually the intended person
        # before touching the composer. Search is fuzzy: typing "Ahmed" can
        # open "Vigar Ahmed Munawar". We abort here rather than message the
        # wrong contact.
        header = self._read_header()
        if not self._conversation_matches(header, recipient):
            _log.info(
                "WhatsApp conversation guard rejected recipient=%r header=%r",
                recipient, header,
            )
            return ToolResult(
                "send_whatsapp",
                False,
                f"I couldn't confirm the WhatsApp conversation with {recipient}, so I didn't send anything.",
                {
                    "started": True,
                    "completed": False,
                    "verified": False,
                    "stage": "conversation_match",
                    "header": header,
                    "recipient": recipient,
                },
            )

        # 3. Focus Chat Message Box
        # Try semantic vision target "message_box", fallback to clicking bottom area
        clicked_box = False
        try:
            clicked_box = self.vision.click_text("message_box")
        except Exception:
            pass

        if not clicked_box:
            # Manual fallback click near bottom center of active window
            active_win = self.computer.get_active_window()
            if active_win and "whatsapp" in active_win.lower():
                self.computer.click()

        time.sleep(0.2)

        # 4. Enter & Send Message
        # Use clipboard paste to safely support special characters, quotes, and newlines
        self.computer.set_clipboard(message)
        time.sleep(0.1)
        self.computer.paste()
        time.sleep(0.3)

        # Press Enter to send
        self.computer.press("enter")
        time.sleep(0.5)

        # 5. Verification — only claim delivery on actual observed evidence.
        verified, evidence = self._verify_sent(message)

        _log.info(
            "WhatsApp flow to '%s' finished. verified=%s evidence=%s",
            recipient, verified, evidence,
        )

        if verified:
            return ToolResult(
                "send_whatsapp",
                True,
                f"Message sent to {recipient} on WhatsApp.",
                {
                    "started": True,
                    "completed": True,
                    "verified": True,
                    "evidence": evidence,
                    "recipient": recipient,
                    "message": message,
                },
            )

        # The keystrokes ran, but nothing on screen confirms the send.
        # Reporting failure (not fake success) is the honest outcome.
        return ToolResult(
            "send_whatsapp",
            False,
            f"I typed the message to {recipient}, but could not confirm it was sent.",
            {
                "started": True,
                "completed": False,
                "verified": False,
                "evidence": evidence,
                "recipient": recipient,
                "message": message,
            },
        )

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
    def _conversation_matches(cls, header_text: str, recipient: str) -> bool:
        """True only when the header names exactly the intended recipient.

        Token-set equality (order-insensitive, UI noise removed) is
        deliberate: a fuzzy substring match would happily accept
        "Vigar Ahmed Munawar" for "Ahmed", which would send to the wrong
        person. A false negative (refusing to send) is always safer than a
        false positive.
        """
        header = cls._name_tokens(header_text)
        target = cls._name_tokens(recipient)
        if not header or not target:
            return False
        return header == target

    def _verify_sent(self, message: str) -> tuple[bool, str]:
        """Return (verified, evidence) with the strongest signal available.

        A focused WhatsApp window alone is NOT delivery evidence. We only
        report success when the sent text is actually visible in the
        conversation (via OCR), or fail honestly when we cannot observe it.
        """
        active_window = self.computer.get_active_window()
        if not active_window or "whatsapp" not in active_window.lower():
            return False, "whatsapp_not_focused"

        read_screen = getattr(self.vision, "read_screen", None)
        if callable(read_screen):
            try:
                screen = read_screen() or {}
            except Exception:
                screen = {}
            text = (screen.get("text") or "").lower()
            needle = " ".join(message.lower().split())[:40]
            if needle and needle in text:
                return True, "message_visible_in_conversation"

        return False, "no_visual_confirmation"
