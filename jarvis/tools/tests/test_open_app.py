"""Regression tests for DesktopController.open_app.

The critical regression: the old launch used

    powershell -NoProfile -Command "<script>" <app>

which does NOT populate ``$args`` in PowerShell. The filter therefore became
``'**'``, matched *every* Start app, and ``Select-Object -First 1`` launched
the alphabetically first one — on this machine, the "About Java" window.
"""

import subprocess

from tools.desktop_control import DesktopController


def make_controller():
    controller = DesktopController.__new__(DesktopController)
    controller.apps = {}
    return controller


class _Result:
    returncode = 0
    stdout = "NOMATCH\n"
    stderr = ""


def test_missing_start_app_launches_nothing(monkeypatch):
    controller = make_controller()
    monkeypatch.setattr(controller, "_running_processes", lambda: [])
    popen_calls = []
    monkeypatch.setattr(
        "tools.desktop_control.subprocess.Popen",
        lambda *args, **kwargs: popen_calls.append(args),
    )
    monkeypatch.setattr("tools.desktop_control.subprocess.run", lambda *a, **k: _Result())

    assert controller.open_app("whatsapp") is False
    assert popen_calls == []


def test_start_app_query_is_passed_via_environment_not_positional_arg(monkeypatch):
    controller = make_controller()
    calls = []
    monkeypatch.setattr(
        "tools.desktop_control.subprocess.run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or _Result(),
    )

    assert controller._launch_start_app("whatsapp") is None

    cmd, kwargs = calls[0]
    assert cmd[:3] == ["powershell", "-NoProfile", "-Command"]
    # The app name must NOT be a trailing command argument (that was the bug).
    assert "whatsapp" not in cmd
    assert kwargs["env"]["JARVIS_APP_QUERY"] == "whatsapp"


def test_start_app_reports_matched_name_on_success(monkeypatch):
    controller = make_controller()

    class _Ok(_Result):
        stdout = "OK:WhatsApp\n"

    monkeypatch.setattr("tools.desktop_control.subprocess.run", lambda *a, **k: _Ok())
    assert controller._launch_start_app("whatsapp") == "WhatsApp"


def test_known_app_launches_and_is_verified(monkeypatch):
    controller = make_controller()
    monkeypatch.setattr("tools.desktop_control.os.path.exists", lambda path: True)
    popen_calls = []
    monkeypatch.setattr(
        "tools.desktop_control.subprocess.Popen",
        lambda cmd, **kwargs: popen_calls.append(cmd),
    )
    monkeypatch.setattr(controller, "_wait_for_process", lambda fragment, timeout=8.0: True)

    assert controller.open_app("notepad") is True
    assert popen_calls and "notepad.exe" in popen_calls[0][0].lower()


def test_unconfirmed_launch_is_not_reported_as_success(monkeypatch):
    controller = make_controller()
    monkeypatch.setattr("tools.desktop_control.os.path.exists", lambda path: True)
    monkeypatch.setattr("tools.desktop_control.subprocess.Popen", lambda *a, **k: None)
    monkeypatch.setattr(controller, "_wait_for_process", lambda fragment, timeout=8.0: False)
    monkeypatch.setattr(controller, "_launch_start_app", lambda query: None)

    assert controller.open_app("notepad") is False


def test_open_app_rejects_unsafe_and_short_names(monkeypatch):
    controller = make_controller()
    called = []
    monkeypatch.setattr(
        "tools.desktop_control.subprocess.run",
        lambda *a, **k: called.append(a) or _Result(),
    )
    assert controller.open_app("no; rm -rf") is False
    assert controller.open_app("a") is False
    assert controller.open_app("") is False
    assert controller.open_app(None) is False
    assert called == []


def test_find_app_prefers_shortest_partial_match():
    controller = make_controller()
    controller.apps = {
        "whatsapp": r"C:\sm\WhatsApp.lnk",
        "whatsapp beta": r"C:\sm\WhatsApp Beta.lnk",
    }
    assert controller.find_app("whatsapp") == r"C:\sm\WhatsApp.lnk"


def test_close_app_still_rejects_broad_targets(monkeypatch):
    controller = make_controller()
    called = []
    monkeypatch.setattr(
        "tools.desktop_control.subprocess.run",
        lambda *a, **k: called.append(a),
    )
    assert controller.close_app("a") is False
    assert called == []


def test_process_fragment_maps_aliases():
    from tools.desktop_control import _process_fragment

    assert _process_fragment("chrome") == "chrome"
    assert _process_fragment("vs code") == "code"
    assert _process_fragment("whatsapp") == "whatsapp"
