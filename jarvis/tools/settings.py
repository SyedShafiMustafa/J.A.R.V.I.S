"""
tools/settings.py
-----------------
§29 — hybrid Windows settings control.

Reusable setting abstraction over the existing JARVIS stack. No parallel
framework: the planner, router, TaskExecutor, LiveToolRunner,
PermissionEngine, experience memory and the visual/UI stack stay exactly
as they are — this module only adds *setting backends* behind two tools
(``get_setting`` / ``set_setting``).

Control strategy per setting (native first, honest last):

    1. READ the current state through a reliable native interface.
    2. CHANGE it through the same native interface.
    3. READ again and VERIFY the exact resulting state.
    4. Only when the native write is unavailable/unreliable, invoke the
       UI-automation fallback (existing visual/computer stack), then
       verify again through the native read.
    5. Otherwise report an honest failure — never a bare claim.

Every operation returns a ``SettingResult`` with ``method`` recorded as
``native`` / ``ui_automation`` / ``failed`` plus per-stage latencies, so
experience memory and the HUD can tell the paths apart.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_log = logging.getLogger("jarvis.settings")

# ---------------------------------------------------------------------------
# Abstraction
# ---------------------------------------------------------------------------

SUPPORTED_SETTINGS = ("dark_mode", "system_volume")

METHOD_NATIVE = "native"
METHOD_UI = "ui_automation"
METHOD_FAILED = "failed"

#: Volume writes verify within this tolerance (winmm quantizes to 65535).
VOLUME_VERIFY_TOLERANCE_PCT = 2
#: Relative up/down step for "turn the volume up/down".
VOLUME_NUDGE_PCT = 10


@dataclass
class SettingRequest:
    """What the caller wants. ``desired_value`` is setting-specific:

    - ``dark_mode``: ``"on"`` / ``"off"`` (also accepts True/False)
    - ``system_volume``: 0-100 int (also accepts numeric strings),
      ``"mute"`` / ``"unmute"`` / ``"up"`` / ``"down"``
    """

    setting: str
    desired_value: Any = None
    verification: bool = True
    fallback_allowed: bool = True


@dataclass
class SettingResult:
    """READ → CHANGE → VERIFY outcome. Never a bare claim."""

    success: bool
    verified: bool
    method: str = METHOD_FAILED
    previous_value: Any = None
    new_value: Any = None
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "verified": self.verified,
            "method": self.method,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "message": self.message,
            "evidence": dict(self.evidence),
            "retryable": self.retryable,
            "error": self.error,
        }


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


# ---------------------------------------------------------------------------
# Native backend: dark mode (registry Personalize + broadcast)
# ---------------------------------------------------------------------------
#
# JARVIS defines "dark mode" as BOTH theme components dark:
#   AppsUseLightTheme == 0 AND SystemUsesLightTheme == 0
# under HKCU\\...\\Themes\\Personalize. "Light mode" is both == 1.
# Mixed states are reported honestly as "mixed", never rounded to dark.

_PERSONALIZE_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
)
_APPS_KEY = "AppsUseLightTheme"
_SYSTEM_KEY = "SystemUsesLightTheme"


class DarkModeNative:
    """Native dark-mode read/write through the Personalize registry keys."""

    def available(self) -> bool:
        try:
            import winreg  # noqa: F401
        except Exception:
            return False
        return True

    def read(self) -> dict[str, Any]:
        """Return {ok, apps_light, system_light, dark, ...} (no throw)."""
        try:
            import winreg
        except Exception as exc:
            return {"ok": False, "reason": "native_unavailable",
                    "error": str(exc)[:200]}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                _PERSONALIZE_SUBKEY) as key:
                apps, _ = winreg.QueryValueEx(key, _APPS_KEY)
                system, _ = winreg.QueryValueEx(key, _SYSTEM_KEY)
            apps_light = bool(int(apps))
            system_light = bool(int(system))
            dark = not apps_light and not system_light
            return {"ok": True, "apps_light": apps_light,
                    "system_light": system_light, "dark": dark,
                    "mixed": apps_light != system_light}
        except FileNotFoundError as exc:
            return {"ok": False, "reason": "native_unavailable",
                    "error": f"personalize keys missing: {exc}"[:200]}
        except OSError as exc:
            return {"ok": False, "reason": "native_unavailable",
                    "error": str(exc)[:200]}

    def write(self, dark: bool) -> dict[str, Any]:
        """Set both theme components; broadcast so apps pick it up."""
        try:
            import winreg
        except Exception as exc:
            return {"ok": False, "reason": "native_unavailable",
                    "error": str(exc)[:200]}
        value = 0 if dark else 1
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                _PERSONALIZE_SUBKEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _APPS_KEY, 0, winreg.REG_DWORD,
                                  value)
                winreg.SetValueEx(key, _SYSTEM_KEY, 0, winreg.REG_DWORD,
                                  value)
        except OSError as exc:
            return {"ok": False, "reason": "native_write_failed",
                    "error": str(exc)[:200]}
        self._broadcast_theme_change()
        return {"ok": True}

    @staticmethod
    def _broadcast_theme_change() -> None:
        """Tell running apps the immersive color set changed (best effort)."""
        try:
            import ctypes
            from ctypes import wintypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x1A
            SMTO_ABORTIFHUNG = 0x0002
            result = wintypes.DWORD()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
                SMTO_ABORTIFHUNG, 2000, ctypes.byref(result))
        except Exception:
            _log.debug("theme broadcast failed", exc_info=True)


# ---------------------------------------------------------------------------
# Native backend: system volume (winmm waveOut + mixer mute)
# ---------------------------------------------------------------------------
#
# Master *output* volume only — never application or microphone volume:
# - level  via waveOutGetVolume / waveOutSetVolume (device 0, both channels)
# - mute   via the mixer MUTE control on the DST_SPEAKERS line
# Both are pure-ctypes system interfaces (no new dependencies).

_MMSYSERR_NOERROR = 0
_MIXER_OBJECTF_MIXER = 0
_MIXER_GETLINEINFOF_COMPONENTTYPE = 3
_MIXERLINE_COMPONENTTYPE_DST_SPEAKERS = 0x4
_MIXER_GETLINECONTROLSF_ONEBYTYPE = 2
_MIXERCONTROL_CONTROLTYPE_MUTE = 0x20010002
_MIXER_GETCONTROLDETAILSF_VALUE = 0


def _winmm():
    try:
        import ctypes
        return ctypes.windll.winmm
    except Exception:
        return None


class VolumeNative:
    """Native master-output volume/mute through winmm (ctypes only)."""

    def available(self) -> bool:
        winmm = _winmm()
        if winmm is None:
            return False
        try:
            import ctypes
            raw = ctypes.c_uint32()
            return winmm.waveOutGetVolume(0, ctypes.byref(raw)) == 0
        except Exception:
            return False

    # -- level ----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        winmm = _winmm()
        if winmm is None:
            return {"ok": False, "reason": "native_unavailable",
                    "error": "winmm missing"}
        try:
            import ctypes
            raw = ctypes.c_uint32()
            if winmm.waveOutGetVolume(0, ctypes.byref(raw)) != 0:
                return {"ok": False, "reason": "native_unavailable",
                        "error": "waveOutGetVolume failed"}
            left = raw.value & 0xFFFF
            right = (raw.value >> 16) & 0xFFFF
            pct = int(round(((left + right) / 2.0) / 655.35))
            mute = self._read_mute(winmm)
            out: dict[str, Any] = {"ok": True, "percent": pct,
                                   "left": left, "right": right}
            if mute.get("ok"):
                out["muted"] = mute["muted"]
            else:
                out["mute_error"] = mute.get("reason", "mute_unavailable")
            return out
        except Exception as exc:
            return {"ok": False, "reason": "native_unavailable",
                    "error": str(exc)[:200]}

    def write_percent(self, percent: int) -> dict[str, Any]:
        winmm = _winmm()
        if winmm is None:
            return {"ok": False, "reason": "native_unavailable",
                    "error": "winmm missing"}
        try:
            import ctypes
            v = max(0, min(100, int(percent)))
            raw = int(round(v * 655.35)) & 0xFFFF
            packed = raw | (raw << 16)
            if winmm.waveOutSetVolume(0, packed) != 0:
                return {"ok": False, "reason": "native_write_failed",
                        "error": "waveOutSetVolume failed"}
            return {"ok": True, "requested": v}
        except Exception as exc:
            return {"ok": False, "reason": "native_write_failed",
                    "error": str(exc)[:200]}

    # -- mute (mixer MUTE control on the speakers line) ------------------
    def _mixer_mute_control(self, winmm, for_set: bool = False):
        """Resolve the mute control id. Returns (ok, payload)."""
        import ctypes
        from ctypes import wintypes

        class MIXERLINE(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("dwDestination", wintypes.DWORD),
                ("dwSource", wintypes.DWORD),
                ("dwLineID", wintypes.DWORD),
                ("fdwLine", wintypes.DWORD),
                ("dwUser", wintypes.DWORD),
                ("dwComponentType", wintypes.DWORD),
                ("cChannels", wintypes.DWORD),
                ("cConnections", wintypes.DWORD),
                ("cControls", wintypes.DWORD),
                ("szShortName", ctypes.c_char * 16),
                ("szName", ctypes.c_char * 64),
                ("dwType", wintypes.DWORD),
                ("dwDeviceID", wintypes.DWORD),
                ("wMid", wintypes.WORD),
                ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT),
                ("szPname", ctypes.c_char * 32),
            ]

        class MIXERCONTROL(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("dwControlID", wintypes.DWORD),
                ("dwControlType", wintypes.DWORD),
                ("fdwControl", wintypes.DWORD),
                ("cMultipleItems", wintypes.DWORD),
                ("szShortName", ctypes.c_char * 16),
                ("szName", ctypes.c_char * 64),
                ("Bounds", wintypes.DWORD * 6),
                ("Metrics", wintypes.DWORD * 6),
            ]

        class MIXERLINECONTROLS(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("dwLineID", wintypes.DWORD),
                ("dwControl", wintypes.DWORD),
                ("cControls", wintypes.DWORD),
                ("cbmxctrl", wintypes.DWORD),
                ("pamxctrl", ctypes.c_void_p),
            ]

        hmx = wintypes.HANDLE()
        if winmm.mixerOpen(ctypes.byref(hmx), 0, 0, 0,
                           _MIXER_OBJECTF_MIXER) != _MMSYSERR_NOERROR:
            return False, {"reason": "mixer_open_failed"}
        try:
            line = MIXERLINE()
            line.cbStruct = ctypes.sizeof(MIXERLINE)
            line.dwComponentType = _MIXERLINE_COMPONENTTYPE_DST_SPEAKERS
            if winmm.mixerGetLineInfoA(
                    hmx, ctypes.byref(line),
                    _MIXER_GETLINEINFOF_COMPONENTTYPE) != 0:
                return False, {"reason": "mixer_line_missing"}
            ctrl = MIXERCONTROL()
            ctrl.cbStruct = ctypes.sizeof(MIXERCONTROL)
            mxlc = MIXERLINECONTROLS()
            mxlc.cbStruct = ctypes.sizeof(MIXERLINECONTROLS)
            mxlc.dwLineID = line.dwLineID
            mxlc.dwControl = _MIXERCONTROL_CONTROLTYPE_MUTE
            mxlc.cControls = 1
            mxlc.cbmxctrl = ctypes.sizeof(MIXERCONTROL)
            mxlc.pamxctrl = ctypes.cast(ctypes.byref(ctrl),
                                        ctypes.c_void_p).value
            if winmm.mixerGetLineControlsA(
                    hmx, ctypes.byref(mxlc),
                    _MIXER_GETLINECONTROLSF_ONEBYTYPE) != 0:
                return False, {"reason": "mute_control_missing"}
            return True, {"hmx": hmx, "control_id": ctrl.dwControlID,
                          "keep_open": for_set}
        except Exception as exc:
            try:
                winmm.mixerClose(hmx)
            except Exception:
                pass
            return False, {"reason": "mixer_error",
                           "error": str(exc)[:200]}

    def _read_mute(self, winmm) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        class MIXERCONTROLDETAILS(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("dwControlID", wintypes.DWORD),
                ("cChannels", wintypes.DWORD),
                ("item", wintypes.DWORD),
                ("cbDetails", wintypes.DWORD),
                ("paDetails", ctypes.c_void_p),
            ]

        class MIXERCONTROLDETAILS_BOOLEAN(ctypes.Structure):
            _fields_ = [("fValue", ctypes.c_long)]

        ok, payload = self._mixer_mute_control(winmm)
        if not ok:
            return {"ok": False, **payload}
        hmx = payload["hmx"]
        try:
            flag = MIXERCONTROLDETAILS_BOOLEAN()
            mxcd = MIXERCONTROLDETAILS()
            mxcd.cbStruct = ctypes.sizeof(MIXERCONTROLDETAILS)
            mxcd.dwControlID = payload["control_id"]
            mxcd.cChannels = 1
            mxcd.item = 0
            mxcd.cbDetails = ctypes.sizeof(
                MIXERCONTROLDETAILS_BOOLEAN)
            mxcd.paDetails = ctypes.cast(ctypes.byref(flag),
                                         ctypes.c_void_p).value
            if winmm.mixerGetControlDetailsA(
                    hmx, ctypes.byref(mxcd),
                    _MIXER_GETCONTROLDETAILSF_VALUE) != 0:
                return {"ok": False, "reason": "mute_read_failed"}
            return {"ok": True, "muted": bool(flag.fValue)}
        except Exception as exc:
            return {"ok": False, "reason": "mute_read_failed",
                    "error": str(exc)[:200]}
        finally:
            try:
                winmm.mixerClose(hmx)
            except Exception:
                pass

    def write_mute(self, muted: bool) -> dict[str, Any]:
        winmm = _winmm()
        if winmm is None:
            return {"ok": False, "reason": "native_unavailable",
                    "error": "winmm missing"}
        import ctypes
        from ctypes import wintypes

        class MIXERCONTROLDETAILS(ctypes.Structure):
            _fields_ = [
                ("cbStruct", wintypes.DWORD),
                ("dwControlID", wintypes.DWORD),
                ("cChannels", wintypes.DWORD),
                ("item", wintypes.DWORD),
                ("cbDetails", wintypes.DWORD),
                ("paDetails", ctypes.c_void_p),
            ]

        class MIXERCONTROLDETAILS_BOOLEAN(ctypes.Structure):
            _fields_ = [("fValue", ctypes.c_long)]

        ok, payload = self._mixer_mute_control(winmm, for_set=True)
        if not ok:
            return {"ok": False, **payload}
        hmx = payload["hmx"]
        try:
            flag = MIXERCONTROLDETAILS_BOOLEAN(1 if muted else 0)
            mxcd = MIXERCONTROLDETAILS()
            mxcd.cbStruct = ctypes.sizeof(MIXERCONTROLDETAILS)
            mxcd.dwControlID = payload["control_id"]
            mxcd.cChannels = 1
            mxcd.item = 0
            mxcd.cbDetails = ctypes.sizeof(
                MIXERCONTROLDETAILS_BOOLEAN)
            mxcd.paDetails = ctypes.cast(ctypes.byref(flag),
                                         ctypes.c_void_p).value
            if winmm.mixerSetControlDetails(
                    hmx, ctypes.byref(mxcd),
                    _MIXER_GETCONTROLDETAILSF_VALUE) != 0:
                return {"ok": False, "reason": "native_write_failed",
                        "error": "mixerSetControlDetails failed"}
            return {"ok": True, "requested_muted": bool(muted)}
        except Exception as exc:
            return {"ok": False, "reason": "native_write_failed",
                    "error": str(exc)[:200]}
        finally:
            try:
                winmm.mixerClose(hmx)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Settings registry / capability model (§12)
# ---------------------------------------------------------------------------

SETTINGS_REGISTRY: dict[str, dict[str, Any]] = {
    "dark_mode": {
        "label": "dark mode",
        "read": "HKCU Themes\\Personalize AppsUseLightTheme/SystemUsesLightTheme",
        "write": "registry write of both values + WM_SETTINGCHANGE broadcast",
        "native": "winreg + SendMessageTimeoutW",
        "fallback": "Settings app Colors page via visual stack",
        "verification": "re-read both values; dark == both zero",
        "permission_read": "harmless",
        "permission_write": "normal",
        "values": ["on", "off"],
        "limitations": [
            "changes app + system theme together, never one side alone",
            "a mixed pre-existing state is normalized to the requested side",
            "running apps refresh via broadcast; some need a restart",
        ],
    },
    "system_volume": {
        "label": "system volume",
        "read": "waveOutGetVolume (master output) + mixer mute flag",
        "write": "waveOutSetVolume / mixer mute control",
        "native": "winmm waveOut + mixer API (ctypes, no new deps)",
        "fallback": "media keys (volume up/down/mute) via computer stack",
        "verification": "re-read level within ±2, mute flag exact",
        "permission_read": "harmless",
        "permission_write": "normal",
        "values": ["0-100", "mute", "unmute", "up", "down"],
        "limitations": [
            "targets master output only, not per-app or microphone",
            "up/down move in 10-point steps",
        ],
    },
}


# ---------------------------------------------------------------------------
# Natural-language parsing (§8)
# ---------------------------------------------------------------------------
#
# Returns (tool, payload) for explicit OS-setting requests, else None.
# Explicit how-to questions ("How do I turn on dark mode?") return None so
# the brain explains instead of executing — checked FIRST.

_DARK_ON = (
    r"(turn|switch|put|set|change|enable|make)\b.{0,40}\b(dark\b.{0,10}\bmode|dark\b)",
    r"\bdark\s+mode\s+on\b",
    r"\benable\b.{0,30}\bdark",
)
_DARK_OFF = (
    r"(turn|switch|put|set|change|disable|make)\b.{0,40}\b(light\b.{0,10}\bmode|light\b)",
    r"\b(turn|switch)\s+off\b.{0,25}\bdark",
    r"\bdark\b.{0,15}\boff\b",
    r"\blight\s+mode\b",
    r"\bdisable\b.{0,30}\bdark",
)
_DARK_READ = (
    r"\bis\b.{0,30}\bdark\s+mode\b.{0,20}\b(on|off|enabled|disabled)\b",
    r"\bdark\s+mode\s+(status|state)\b",
    r"\bwhat\b.{0,20}\b(theme|mode)\b.{0,20}\b(am\s+i|is\s+it)\b",
)


def _parse_dark(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    low = text.lower()
    if "dark" not in low and "light mode" not in low and "light theme" not in low:
        return None
    for pattern in _DARK_READ:
        if re.search(pattern, low):
            return ("get_setting", {"setting": "dark_mode"})
    for pattern in _DARK_OFF:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "dark_mode",
                                    "value": "off"})
    for pattern in _DARK_ON:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "dark_mode",
                                    "value": "on"})
    # Bare mention with an action verb ("dark mode please") defaults to
    # nothing — the planner decides. Only explicit reads fall through:
    if re.search(r"\bdark\s+mode\b", low) and re.search(
            r"\b(is|status|state|on\?|enabled\?)\b", low):
        return ("get_setting", {"setting": "dark_mode"})
    return None


_VOLUME_SET = (
    r"(?:set|change|put|turn|adjust).{0,20}\bvolume\b.{0,10}\bto\b\s*(\d{1,3})",
    r"\bvolume\b\s*(\d{1,3})\s*%?",
    r"\bset\b.{0,10}\bvolume\b\s*(\d{1,3})",
)
_VOLUME_UP = (
    r"\b(volume\s+up|turn\s+(the\s+)?volume\s+up|increase\s+(the\s+)?volume|"
    r"raise\s+(the\s+)?volume|volume\s+louder)\b",
    r"\bturn\s+it\s+up\b.{0,20}\bvolume\b",
)
_VOLUME_DOWN = (
    r"\b(volume\s+down|turn\s+(the\s+)?volume\s+down|decrease\s+(the\s+)?volume|"
    r"lower\s+(the\s+)?volume|volume\s+(quieter|lower))\b",
)
_VOLUME_MUTE = (
    r"\bmute\b.{0,25}\bvolume\b",
    r"\bvolume\b.{0,10}\bmute\b",
    r"^\s*(mute|mute\s+it|mute\s+the\s+volume)\s*[.?!]?\s*$",
)
_VOLUME_UNMUTE = (
    r"\bunmute\b",
    r"\bun-?mute\b",
    r"\bmute\s+off\b",
)
_VOLUME_READ = (
    r"\bwhat(\'s| is)\b.{0,30}\bvolume\b",
    r"\bcurrent\b.{0,10}\bvolume\b",
    r"\bvolume\s+(level|status|state)\b",
    r"\bhow\s+(loud|high)\b.{0,20}\bvolume\b",
    r"\bis\b.{0,25}\bvolume\b.{0,20}\bmuted\b",
    r"\bis\s+it\s+muted\b",
)


def _parse_volume(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    low = text.lower()
    if "volume" not in low and "mute" not in low and "unmute" not in low:
        return None
    for pattern in _VOLUME_READ:
        if re.search(pattern, low):
            return ("get_setting", {"setting": "system_volume"})
    for pattern in _VOLUME_UNMUTE:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "system_volume",
                                    "value": "unmute"})
    for pattern in _VOLUME_MUTE:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "system_volume",
                                    "value": "mute"})
    for pattern in _VOLUME_UP:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "system_volume",
                                    "value": "up"})
    for pattern in _VOLUME_DOWN:
        if re.search(pattern, low):
            return ("set_setting", {"setting": "system_volume",
                                    "value": "down"})
    for pattern in _VOLUME_SET:
        match = re.search(pattern, low)
        if match:
            try:
                pct = int(match.group(1))
            except (IndexError, ValueError):
                continue
            if 0 <= pct <= 100:
                return ("set_setting", {"setting": "system_volume",
                                        "value": pct})
            return ("__invalid__", {"reason": "volume out of range",
                                    "value": pct})
    return None


def parse_settings_command(
        text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Map an explicit OS-setting utterance to (tool, payload).

    Returns None when the text is not a settings command — including
    explicit how-to questions, which must be explained, not executed.
    """
    if not (text or "").strip():
        return None
    try:
        from core.intent import is_explanation_request, strip_politeness
        if is_explanation_request(text):
            return None
        core = strip_politeness(text)
    except Exception:
        core = (text or "").lower()
    for parser in (_parse_dark, _parse_volume):
        try:
            hit = parser(core)
        except Exception:
            continue
        if hit is not None:
            return hit
    return None


# ---------------------------------------------------------------------------
# Controller: READ → CHANGE → VERIFY with fallback (§2, §6, §7, §10, §16)
# ---------------------------------------------------------------------------

class SettingsController:
    """Owns setting backends; UI fallback reuses the injected stacks."""

    def __init__(self, computer=None, visual=None, desktop=None,
                 dark: Optional[DarkModeNative] = None,
                 volume: Optional[VolumeNative] = None,
                 ui_fallback: Optional[Callable[..., dict]] = None):
        self.computer = computer
        self.visual = visual
        self.desktop = desktop
        self.dark = dark or DarkModeNative()
        self.volume = volume or VolumeNative()
        # Test seam: override the whole fallback step per setting.
        self._ui_fallback_override = ui_fallback

    # -- READ ------------------------------------------------------------
    def get_setting(self, setting: str) -> SettingResult:
        start = _now_ms()
        if setting not in SUPPORTED_SETTINGS:
            return SettingResult(
                False, False, METHOD_FAILED, None, None,
                f"I don't manage a setting called '{setting}'. "
                f"I handle dark mode and system volume.",
                {"setting": setting, "total_ms": _now_ms() - start},
                retryable=False, error="unsupported_setting")
        if setting == "dark_mode":
            state = self.dark.read()
            if not state.get("ok"):
                return SettingResult(
                    False, False, METHOD_FAILED, None, None,
                    "I couldn't read the theme state.",
                    {"setting": setting,
                     "total_ms": _now_ms() - start,
                     "reason": state.get("reason", "native_unavailable")},
                    error="verification_unavailable")
            dark = state["dark"]
            label = self._dark_label(state)
            return SettingResult(
                True, True, METHOD_NATIVE, label, label,
                f"Dark mode is {label}.",
                {"setting": setting, "total_ms": _now_ms() - start,
                 "apps_light": state["apps_light"],
                 "system_light": state["system_light"]},
                retryable=False)
        state = self.volume.read()
        if not state.get("ok"):
            return SettingResult(
                False, False, METHOD_FAILED, None, None,
                "I couldn't read the system volume.",
                {"setting": setting, "total_ms": _now_ms() - start,
                 "reason": state.get("reason", "native_unavailable")},
                error="verification_unavailable")
        label = self._volume_label(state)
        return SettingResult(
            True, True, METHOD_NATIVE, label, label,
            self._volume_read_message(state),
            {"setting": setting, "total_ms": _now_ms() - start,
             "percent": state["percent"],
             "muted": state.get("muted")},
            retryable=False)

    # -- CHANGE ----------------------------------------------------------
    def set_setting(self, setting: str, value: Any,
                    fallback_allowed: bool = True) -> SettingResult:
        start = _now_ms()
        if setting not in SUPPORTED_SETTINGS:
            return SettingResult(
                False, False, METHOD_FAILED, None, None,
                f"I don't manage a setting called '{setting}'.",
                {"setting": setting, "total_ms": _now_ms() - start},
                retryable=False, error="unsupported_setting")
        if setting == "dark_mode":
            norm = self._normalize_dark(value)
            if norm is None:
                return SettingResult(
                    False, False, METHOD_FAILED, None, None,
                    "Dark mode takes 'on' or 'off'.",
                    {"setting": setting,
                     "total_ms": _now_ms() - start},
                    retryable=False, error="invalid_value")
            return self._apply_dark(norm, fallback_allowed, start)
        norm = self._normalize_volume(value)
        if norm is None:
            return SettingResult(
                False, False, METHOD_FAILED, None, None,
                "Volume takes 0–100, mute, unmute, up or down.",
                {"setting": setting, "total_ms": _now_ms() - start},
                retryable=False, error="invalid_value")
        return self._apply_volume(norm, fallback_allowed, start)

    # -- dark mode --------------------------------------------------------
    @staticmethod
    def _normalize_dark(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        low = str(value or "").strip().lower()
        if low in ("on", "dark", "enable", "enabled", "true", "1"):
            return True
        if low in ("off", "light", "disable", "disabled", "false", "0"):
            return False
        return None

    @staticmethod
    def _dark_label(state: dict[str, Any]) -> str:
        if state.get("mixed"):
            return "mixed"
        return "on" if state.get("dark") else "off"

    def _apply_dark(self, dark: bool, fallback_allowed: bool,
                    start: float) -> SettingResult:
        evidence: dict[str, Any] = {"setting": "dark_mode",
                                    "requested": "on" if dark else "off"}
        t = _now_ms()
        prev = self.dark.read()
        evidence["read_ms"] = _now_ms() - t
        previous = self._dark_label(prev) if prev.get("ok") else None
        evidence["previous_value"] = previous

        request = SettingRequest("dark_mode", "on" if dark else "off",
                                 fallback_allowed=fallback_allowed)

        def _verified() -> Optional[str]:
            t2 = _now_ms()
            cur = self.dark.read()
            evidence["verify_ms"] = _now_ms() - t2
            if not cur.get("ok"):
                evidence["verify_error"] = cur.get("reason")
                return None
            if cur.get("dark") is dark and not cur.get("mixed"):
                return self._dark_label(cur)
            evidence["verify_mismatch"] = {
                "apps_light": cur.get("apps_light"),
                "system_light": cur.get("system_light")}
            return None

        # Already there: verify and report, no write needed.
        if prev.get("ok") and prev.get("dark") is dark \
                and not prev.get("mixed"):
            evidence.update(method=METHOD_NATIVE, native_ms=0.0,
                            fallback_used=False,
                            total_ms=_now_ms() - start)
            label = self._dark_label(prev)
            return SettingResult(
                True, True, METHOD_NATIVE, previous, label,
                f"Dark mode is already {label}.", evidence,
                retryable=False)

        # Native write → verify.
        t = _now_ms()
        wrote = self.dark.write(dark)
        evidence["native_ms"] = _now_ms() - t
        if wrote.get("ok"):
            label = _verified()
            if label is not None:
                evidence.update(method=METHOD_NATIVE,
                                fallback_used=False,
                                total_ms=_now_ms() - start)
                return SettingResult(
                    True, True, METHOD_NATIVE, previous, label,
                    f"Dark mode is {label}.", evidence,
                    retryable=False)
            evidence["native_verify_failed"] = True
        else:
            evidence["native_error"] = wrote.get("reason",
                                                 "native_write_failed")

        # Fallback: Settings Colors page through the visual stack.
        fallback_refused: Optional[str] = None
        if fallback_allowed and request.fallback_allowed:
            t = _now_ms()
            fb = self._ui_fallback_dark(dark)
            evidence["fallback_ms"] = _now_ms() - t
            evidence["fallback_used"] = True
            evidence["fallback_detail"] = fb.get("reason", "attempted")
            if fb.get("ok"):
                label = _verified()
                evidence["total_ms"] = _now_ms() - start
                if label is not None:
                    evidence["method"] = METHOD_UI
                    return SettingResult(
                        True, True, METHOD_UI, previous, label,
                        f"Dark mode is {label} (via Settings).",
                        evidence, retryable=False)
                evidence["method"] = METHOD_FAILED
                return SettingResult(
                    False, False, METHOD_FAILED, previous, None,
                    "I tried the Settings app but couldn't confirm "
                    "dark mode changed.", evidence,
                    error="verification_unavailable")
            fallback_refused = fb.get("reason", "fallback_failed")

        evidence.update(method=METHOD_FAILED, fallback_used=False,
                        total_ms=_now_ms() - start)
        if fallback_refused is not None:
            evidence["fallback_refused"] = fallback_refused
            return SettingResult(
                False, False, METHOD_FAILED, previous, None,
                "I couldn't change dark mode, and the Settings "
                "fallback wasn't usable either.", evidence,
                error="verification_unavailable")
        return SettingResult(
            False, False, METHOD_FAILED, previous, None,
            "I couldn't change dark mode.",
            evidence, error=evidence.get("native_error",
                                         "native_write_failed"))

    # -- volume ------------------------------------------------------------
    @staticmethod
    def _normalize_volume(value: Any) -> Optional[Any]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            pct = int(value)
            return pct if 0 <= pct <= 100 else "__range__"
        low = str(value or "").strip().lower().rstrip("%")
        if low in ("mute", "muted"):
            return "mute"
        if low in ("unmute", "unmuted", "un-mute"):
            return "unmute"
        if low in ("up", "louder", "increase"):
            return "up"
        if low in ("down", "quieter", "decrease", "lower"):
            return "down"
        if re.fullmatch(r"\d{1,3}", low or ""):
            pct = int(low)
            return pct if 0 <= pct <= 100 else "__range__"
        return None

    @staticmethod
    def _volume_label(state: dict[str, Any]) -> str:
        if state.get("muted"):
            return f"{state['percent']}% (muted)"
        return f"{state['percent']}%"

    @staticmethod
    def _volume_read_message(state: dict[str, Any]) -> str:
        if state.get("muted"):
            return (f"System volume is {state['percent']}% "
                    f"and muted.")
        return f"System volume is {state['percent']}%."

    def _volume_target(self, norm: Any,
                       current: dict[str, Any]) -> tuple[str, Any]:
        """Resolve up/down/relative words to (kind, absolute target)."""
        if norm == "up":
            return ("percent", min(100, current["percent"]
                                   + VOLUME_NUDGE_PCT))
        if norm == "down":
            return ("percent", max(0, current["percent"]
                                   - VOLUME_NUDGE_PCT))
        if norm in ("mute", "unmute"):
            return ("mute", norm == "mute")
        return ("percent", int(norm))

    def _apply_volume(self, norm: Any, fallback_allowed: bool,
                      start: float) -> SettingResult:
        evidence: dict[str, Any] = {"setting": "system_volume",
                                    "requested": norm}
        t = _now_ms()
        prev = self.volume.read()
        evidence["read_ms"] = _now_ms() - t
        if not prev.get("ok"):
            evidence.update(method=METHOD_FAILED,
                            total_ms=_now_ms() - start)
            return SettingResult(
                False, False, METHOD_FAILED, None, None,
                "I couldn't read the system volume.",
                evidence, error="verification_unavailable")
        previous = self._volume_label(prev)
        evidence["previous_value"] = previous

        if norm == "__range__":
            evidence.update(method=METHOD_FAILED,
                            total_ms=_now_ms() - start)
            return SettingResult(
                False, False, METHOD_FAILED, previous, previous,
                "Volume stays between 0 and 100.",
                evidence, retryable=False, error="invalid_value")

        kind, target = self._volume_target(norm, prev)
        evidence["target"] = target
        evidence["target_kind"] = kind

        def _verified() -> Optional[dict[str, Any]]:
            t2 = _now_ms()
            cur = self.volume.read()
            evidence["verify_ms"] = _now_ms() - t2
            if not cur.get("ok"):
                evidence["verify_error"] = cur.get("reason")
                return None
            if kind == "mute":
                if cur.get("muted") is target:
                    return cur
            elif abs(cur["percent"] - int(target)) \
                    <= VOLUME_VERIFY_TOLERANCE_PCT:
                # A level change while muted stays muted: say so.
                return cur
            evidence["verify_mismatch"] = {
                "percent": cur.get("percent"),
                "muted": cur.get("muted")}
            return None

        # Already there.
        if kind == "mute" and prev.get("muted") is target:
            evidence.update(method=METHOD_NATIVE, native_ms=0.0,
                            fallback_used=False,
                            total_ms=_now_ms() - start)
            return SettingResult(
                True, True, METHOD_NATIVE, previous, previous,
                self._volume_read_message(prev), evidence,
                retryable=False)
        if kind == "percent" and abs(prev["percent"] - int(target)) \
                <= VOLUME_VERIFY_TOLERANCE_PCT and not prev.get("muted"):
            evidence.update(method=METHOD_NATIVE, native_ms=0.0,
                            fallback_used=False,
                            total_ms=_now_ms() - start)
            return SettingResult(
                True, True, METHOD_NATIVE, previous, previous,
                self._volume_read_message(prev), evidence,
                retryable=False)

        # Native write → verify.
        t = _now_ms()
        if kind == "mute":
            wrote = self.volume.write_mute(bool(target))
        else:
            wrote = self.volume.write_percent(int(target))
            # Setting a level on a muted endpoint: unmute so the change
            # is audible, then verify both.
            if wrote.get("ok") and prev.get("muted"):
                un = self.volume.write_mute(False)
                evidence["unmuted_for_level_change"] = bool(
                    un.get("ok"))
        evidence["native_ms"] = _now_ms() - t
        if wrote.get("ok"):
            cur = _verified()
            if cur is not None:
                label = self._volume_label(cur)
                evidence.update(method=METHOD_NATIVE,
                                fallback_used=False,
                                total_ms=_now_ms() - start)
                verb = ("muted" if kind == "mute" and target
                        else "unmuted" if kind == "mute"
                        else f"set to {cur['percent']}%")
                return SettingResult(
                    True, True, METHOD_NATIVE, previous, label,
                    f"System volume {verb}.", evidence,
                    retryable=False)
            evidence["native_verify_failed"] = True
        else:
            evidence["native_error"] = wrote.get("reason",
                                                 "native_write_failed")

        # Fallback: media keys toward the target, then native re-read.
        fallback_refused: Optional[str] = None
        if fallback_allowed:
            t = _now_ms()
            fb = self._ui_fallback_volume(kind, target, prev)
            evidence["fallback_ms"] = _now_ms() - t
            evidence["fallback_used"] = True
            evidence["fallback_detail"] = fb.get("reason", "attempted")
            if fb.get("ok"):
                cur = _verified()
                evidence["total_ms"] = _now_ms() - start
                if cur is not None:
                    label = self._volume_label(cur)
                    evidence["method"] = METHOD_UI
                    return SettingResult(
                        True, True, METHOD_UI, previous, label,
                        f"System volume is {label} (via media keys).",
                        evidence, retryable=False)
                evidence["method"] = METHOD_FAILED
                return SettingResult(
                    False, False, METHOD_FAILED, previous, None,
                    "I tried the volume keys but couldn't confirm "
                    "the volume changed.", evidence,
                    error="verification_unavailable")
            fallback_refused = fb.get("reason", "fallback_failed")

        evidence.update(method=METHOD_FAILED, fallback_used=False,
                        total_ms=_now_ms() - start)
        if fallback_refused is not None:
            evidence["fallback_refused"] = fallback_refused
            return SettingResult(
                False, False, METHOD_FAILED, previous, None,
                "I couldn't change the system volume, and the "
                "media-key fallback wasn't usable either.", evidence,
                error="verification_unavailable")
        return SettingResult(
            False, False, METHOD_FAILED, previous, None,
            "I couldn't change the system volume.",
            evidence, error=evidence.get("native_error",
                                         "native_write_failed"))

    # -- UI automation fallback (§7: OBSERVE → LOCATE → ACT → OBSERVE → VERIFY)
    #    Verification always happens through the native read in the
    #    caller above — these helpers only ACT.

    def _ui_fallback_dark(self, dark: bool) -> dict[str, Any]:
        """Open the Colors settings page and choose the mode visually."""
        if self._ui_fallback_override is not None:
            try:
                return self._ui_fallback_override("dark_mode", dark)
            except Exception as exc:
                return {"ok": False, "reason": "fallback_failed",
                        "error": str(exc)[:200]}
        if self.visual is None or self.desktop is None:
            return {"ok": False, "reason": "fallback_unavailable",
                    "error": "no visual stack attached"}
        try:
            import os
            # OBSERVE: open the Colors page directly (no blind clicks).
            os.startfile("ms-settings:colors")  # noqa: S606
            time.sleep(1.5)
            want = "Dark" if dark else "Light"
            # LOCATE → ACT through the shared VisualAgent (refuses when
            # confidence is low — never a random click).
            res = self.visual.visual_click(
                f"the {want} option under Choose your mode",
                min_confidence=0.6)
            if not res.get("ok"):
                return {"ok": False,
                        "reason": res.get("reason",
                                          "fallback_failed")}
            return {"ok": True, "reason": "settings_page_click"}
        except Exception as exc:
            return {"ok": False, "reason": "fallback_failed",
                    "error": str(exc)[:200]}

    def _ui_fallback_volume(self, kind: str, target: Any,
                            current: dict[str, Any]) -> dict[str, Any]:
        """Nudge master volume with media keys toward the target."""
        if self._ui_fallback_override is not None:
            try:
                return self._ui_fallback_override("system_volume",
                                                  (kind, target))
            except Exception as exc:
                return {"ok": False, "reason": "fallback_failed",
                        "error": str(exc)[:200]}
        if self.computer is None:
            return {"ok": False, "reason": "fallback_unavailable",
                    "error": "no computer stack attached"}
        try:
            press = getattr(self.computer, "press", None)
            if not callable(press):
                return {"ok": False, "reason": "fallback_unavailable",
                        "error": "computer has no key press"}
            if kind == "mute":
                press("volumemute")
                return {"ok": True, "reason": "mute_key"}
            # Step toward the target in bounded key presses, then let
            # the caller verify through the native read.
            diff = int(target) - int(current.get("percent", 50))
            steps = min(10, max(1, abs(diff) // 5 + 1))
            key = "volumeup" if diff > 0 else "volumedown"
            if diff == 0:
                return {"ok": True, "reason": "already_at_target"}
            for _ in range(steps):
                press(key)
                time.sleep(0.15)
            return {"ok": True,
                    "reason": f"{steps}x {key}"}
        except Exception as exc:
            return {"ok": False, "reason": "fallback_failed",
                    "error": str(exc)[:200]}

    # -- experience memory (§11) ------------------------------------------
    @staticmethod
    def build_lesson(result: SettingResult) -> dict[str, str]:
        """Lesson triple for the existing experience-memory system.

        Recorded through the standard tool pipeline (scenario
        ``tool:get_setting`` / ``tool:set_setting``) — no screenshot data.
        """
        ev = result.evidence or {}
        setting = str(ev.get("setting", "setting"))
        if result.success:
            outcome = "success"
            lesson = (f"{setting} {result.method} "
                      f"{result.previous_value} -> {result.new_value}")
            if ev.get("fallback_used"):
                lesson += " (native write failed, fallback verified)"
            if ev.get("unmuted_for_level_change"):
                lesson += " (level change also unmuted)"
        elif result.error == "verification_unavailable":
            outcome = "failure"
            lesson = (f"{setting} unverifiable: "
                      f"{result.method}; never claim success")
        else:
            outcome = "failure"
            lesson = (f"{setting} {result.method} failed: "
                      f"{result.error or 'unknown'}")
        return {"scenario": f"tool:{setting}",
                "strategy": f"{setting}={ev.get('requested', '?')}",
                "outcome": outcome, "lesson": lesson[:300]}
