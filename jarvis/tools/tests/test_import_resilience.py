"""Import resilience: optional Windows-only deps must never break startup.

Regression test for the launch failure where the backend (running on the
system interpreter without pywinauto) died in ``build_live_runtime`` with
``No module named 'pywinauto'`` — so the mic never connected on launch.
Heavy Windows-only imports must stay lazy; importing the modules must
always succeed.
"""

import sys


def _block_windows_deps(monkeypatch):
    for name in ("pywinauto", "pywinauto.application", "win32gui",
                 "win32con", "win32process"):
        monkeypatch.setitem(sys.modules, name, None)
    # Force a fresh import under the blocked state.
    monkeypatch.delitem(sys.modules, "tools.ui_automation",
                        raising=False)


def test_ui_automation_imports_without_pywinauto(monkeypatch):
    _block_windows_deps(monkeypatch)
    import tools.ui_automation as uia_mod

    assert uia_mod.UIAutomation is not None
    detector = uia_mod.UIAutomation()
    # UIA calls fail cleanly (catchable), never at import time.
    try:
        detector.active_window()
        raised = False
    except ImportError:
        raised = True
    assert raised is True
    # The graceful helper degrades to None instead of raising.
    assert detector.foreground_title() is None
    assert detector.safe_inspect() == []


def test_executor_imports_without_pywinauto(monkeypatch):
    _block_windows_deps(monkeypatch)
    import tools.executor as executor_mod

    assert executor_mod.TaskExecutor is not None


def test_visual_agent_imports_without_pywinauto(monkeypatch):
    _block_windows_deps(monkeypatch)
    import tools.visual as visual_mod

    assert visual_mod.VisualAgent is not None
