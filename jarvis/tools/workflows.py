"""
tools/workflows.py
------------------
Milestone 4 — cross-application workflow engine.

Reusable mechanism for arbitrary tool combinations::

    OBSERVE APP A -> EXTRACT -> SWITCH TO APP B -> LOCATE -> ACT
      -> OBSERVE -> VERIFY -> CONTINUE

This is orchestration, not a parallel agent framework: every step runs
through the SAME ``TaskExecutor`` dispatch (planner schemas, permission
gates and verification contracts all apply), and every visual act goes
through the Milestone 3 ``VisualAgent`` observe/verify loop.

Hybrid priority is enforced by construction:

1. native Windows/API/tool (window focus, geometry, clipboard)
2. deterministic app interfaces (launcher, hotkeys)
3. OCR/UIA structure
4. visual interaction (locate -> act -> verify)
5. (broader vision reasoning stays out of scope by design)

Data handoff between steps is structured (clipboard / named extracts /
``{{steps.N.field}}`` templates) so raw screen text is never re-fed to
the LLM for reinterpretation.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from backend.interfaces import ToolResult

_log = logging.getLogger("jarvis.workflow")

MAX_STEPS = 50
DEFAULT_MAX_RETRIES = 1
TEXT_LIMIT = 2000

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _truncate(value: Any, limit: int = TEXT_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    return value


class WorkflowEngine:
    """Compose existing tools into verified cross-app workflows."""

    def __init__(self, step_runner: Callable[[dict[str, Any]], ToolResult],
                 visual: Any | None = None,
                 windows: Any | None = None,
                 computer: Any | None = None) -> None:
        self.step_runner = step_runner
        self.visual = visual
        self.windows = windows
        self.computer = computer

    # ------------------------------------------------------------------
    # Templating — structured handoff without LLM reinterpretation
    # ------------------------------------------------------------------

    def resolve_templates(self, payload: Any,
                          context: dict[str, Any]) -> Any:
        """Substitute ``{{clipboard}}`` / ``{{extracts.x}}`` /
        ``{{steps.0.data.text}}`` inside string payloads."""
        if isinstance(payload, str):
            def _one(match: re.Match) -> str:
                return str(self._lookup(match.group(1), context))
            return _TEMPLATE_RE.sub(_one, payload)
        if isinstance(payload, dict):
            return {k: self.resolve_templates(v, context)
                    for k, v in payload.items()}
        if isinstance(payload, list):
            return [self.resolve_templates(v, context) for v in payload]
        return payload

    @staticmethod
    def _lookup(path: str, context: dict[str, Any]) -> Any:
        if path == "clipboard":
            return context.get("clipboard", "")
        parts = path.split(".")
        node: Any = {"extracts": context.get("extracts", {}),
                     "steps": context.get("steps", {})}
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""
        return "" if node is None else node

    # ------------------------------------------------------------------
    # Handoff record (spec section 4 shape)
    # ------------------------------------------------------------------

    @staticmethod
    def build_handoff(source_app: str, extracted_data: Any,
                      destination_app: str, destination_target: str,
                      transformation: str | None = None,
                      verification: dict[str, Any] | None = None,
                      ) -> dict[str, Any]:
        return {
            "source_app": source_app,
            "extracted_data": _truncate(extracted_data),
            "destination_app": destination_app,
            "destination_target": destination_target,
            "transformation": transformation or "none",
            "verification": verification or {"verified": False},
        }

    # ------------------------------------------------------------------
    # Expectation checks — every step declares its postcondition
    # ------------------------------------------------------------------

    def check_expect(self, expect: dict[str, Any] | None,
                     result: ToolResult) -> dict[str, Any]:
        """Evaluate a step postcondition against fresh observation."""
        t0 = time.monotonic()
        if not expect:
            return {"verified": bool(result.success), "kind": "success",
                    "evidence": {}, "expect_ms": _ms(t0)}
        kind = expect.get("kind")
        if kind == "success":
            return {"verified": bool(result.success), "kind": kind,
                    "evidence": {}, "expect_ms": _ms(t0)}
        if kind in ("text_visible", "text_absent",
                    "window_active", "window_closed"):
            if self.visual is None:
                return {"verified": False, "kind": kind,
                        "evidence": {"reason": "no visual layer"},
                        "expect_ms": _ms(t0)}
            checked = self.visual.verify(
                {"kind": kind, "text": expect.get("text"),
                 "title": expect.get("title")})
            return {"verified": bool(checked.get("verified")),
                    "kind": kind,
                    "evidence": checked.get("evidence", {}),
                    "expect_ms": _ms(t0)}
        if kind == "clipboard_equals":
            want = str(expect.get("text") or "")
            got = self._clipboard_text()
            match = bool(want) and got.strip() == want.strip()
            return {"verified": match, "kind": kind,
                    "evidence": {"clipboard_chars": len(got),
                                 "expected_chars": len(want)},
                    "expect_ms": _ms(t0)}
        if kind == "clipboard_non_empty":
            got = self._clipboard_text()
            return {"verified": bool(got.strip()), "kind": kind,
                    "evidence": {"clipboard_chars": len(got)},
                    "expect_ms": _ms(t0)}
        if kind == "field_non_empty":
            field = str(expect.get("field") or "")
            data = result.data or {}
            present = field in data and data[field] not in (None, "")
            return {"verified": bool(present),
                    "kind": kind,
                    "evidence": {"field": field,
                                 "present": field in data},
                    "expect_ms": _ms(t0)}
        return {"verified": False, "kind": str(kind),
                "evidence": {"reason": "unknown expect kind"},
                "expect_ms": _ms(t0)}

    def _clipboard_text(self) -> str:
        try:
            return str(self.computer.get_clipboard() or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Run — observe/extract/switch/act/verify with bounded recovery
    # ------------------------------------------------------------------

    def run_workflow(self, goal: str, steps: list[dict[str, Any]],
                     max_retries: int = DEFAULT_MAX_RETRIES) -> ToolResult:
        t0 = time.monotonic()
        if not isinstance(goal, str) or not goal.strip():
            return ToolResult("run_workflow", False, "invalid goal",
                              {"started": False, "completed": False})
        if not isinstance(steps, list) or not steps \
                or len(steps) > MAX_STEPS:
            return ToolResult("run_workflow", False, "invalid steps",
                              {"started": False, "completed": False})
        try:
            retries = max(0, int(max_retries))
        except (TypeError, ValueError):
            retries = DEFAULT_MAX_RETRIES

        context: dict[str, Any] = {"clipboard": "", "extracts": {},
                                   "steps": {}}
        trail: list[dict[str, Any]] = []
        total_retries = 0

        for index, raw in enumerate(steps):
            if not isinstance(raw, dict) or not isinstance(
                    raw.get("tool"), str):
                return self._abort(goal, trail, context, total_retries,
                                   f"invalid step {index}", t0)
            tool = raw["tool"]
            payload = {k: v for k, v in raw.items()
                       if k not in ("tool", "save_as", "expect", "app")}
            payload = self.resolve_templates(payload, context)
            step = {"tool": tool, **payload}

            attempt = 0
            recovery: dict[str, Any] = {"strategy": "none", "ok": True}
            result: ToolResult | None = None
            checked: dict[str, Any] = {"verified": False}
            while True:
                try:
                    result = self.step_runner(step)
                except Exception as exc:
                    _log.warning("workflow step %s raised: %s", tool, exc)
                    result = ToolResult(tool, False, f"{tool} raised",
                                        {"started": True,
                                         "completed": False})
                self._harvest(tool, result, raw.get("save_as"), context)
                checked = self.check_expect(raw.get("expect"), result)
                if result.success and checked.get("verified"):
                    break
                if attempt >= retries:
                    break
                attempt += 1
                total_retries += 1
                # RECOVER: observe, reassess, refocus, retry.
                recovery = self._recover(raw)
            trail.append({
                "index": index, "tool": tool,
                "success": bool(result.success),
                "verified": bool(checked.get("verified")),
                "expect": checked.get("kind"),
                "evidence": _truncate(checked.get("evidence", {})),
                "recovery": recovery,
                "retries": attempt,
            })
            context["steps"][str(index)] = _truncate(result.data or {})
            if not result.success or not checked.get("verified"):
                return self._abort(
                    goal, trail, context, total_retries,
                    result.message or f"{tool} failed verification", t0)

        verified = all(s["verified"] for s in trail)
        return ToolResult(
            "run_workflow", True,
            f"workflow verified: {goal}" if verified
            else f"workflow completed unverified: {goal}",
            {"started": True, "completed": True, "verified": verified,
             "stages": trail, "extracts": context["extracts"],
             "retries": total_retries, "total_ms": _ms(t0)},
        )

    def _harvest(self, tool: str, result: ToolResult,
                 save_as: Any, context: dict[str, Any]) -> None:
        if not result.success:
            return
        data = result.data or {}
        if tool == "read_clipboard" and isinstance(data.get("text"), str):
            context["clipboard"] = data["text"]
        if isinstance(save_as, str) and save_as:
            context["extracts"][save_as] = _truncate(data)

    def _recover(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Observe -> reassess -> refocus -> (caller retries)."""
        app = raw.get("app")
        if not app or not isinstance(app, str) or self.windows is None:
            try:
                if self.windows is not None:
                    self.windows.list_windows()
            except Exception:
                pass
            return {"strategy": "re_observe", "ok": True}
        try:
            active = self.windows.active()
            title = (active.get("title") or "").lower()
            if app.lower() not in title:
                refocused = self.windows.focus(app)
                return {"strategy": "refocus",
                        "ok": bool(refocused.get("ok")),
                        "app": app}
            return {"strategy": "re_observe", "ok": True}
        except Exception as exc:
            return {"strategy": "refocus", "ok": False,
                    "error": str(exc)[:200]}

    @staticmethod
    def _abort(goal: str, trail: list[dict[str, Any]],
               context: dict[str, Any], retries: int,
               reason: str, t0: float) -> ToolResult:
        return ToolResult(
            "run_workflow", False, f"workflow failed: {reason}",
            {"started": True, "completed": False, "verified": False,
             "goal": goal, "stages": trail,
             "extracts": context["extracts"],
             "retries": retries, "total_ms": _ms(t0),
             "reason": reason},
        )

    # ------------------------------------------------------------------
    # Experience memory — workflow patterns, never raw dumps
    # ------------------------------------------------------------------

    @staticmethod
    def build_lesson(goal: str, result: ToolResult) -> dict[str, str]:
        data = result.data or {}
        trail = data.get("stages", []) if isinstance(data, dict) else []
        tools = ",".join(s.get("tool", "?") for s in trail)[:200]
        ok = bool(result.success and data.get("verified", False))
        if ok:
            lesson = (f"workflow [{tools}] verified in "
                      f"{data.get('retries', 0)} retries")
        else:
            lesson = (f"workflow [{tools}] failed: "
                      f"{data.get('reason', result.message)[:200]}")
        return {
            "scenario": f"workflow:{goal[:120]}",
            "strategy": tools,
            "outcome": "success" if ok else "failure",
            "lesson": lesson[:300],
        }

    def record_lesson(self, memory: Any, goal: str,
                      result: ToolResult) -> None:
        save = getattr(memory, "save_experience", None)
        if not callable(save):
            return
        try:
            save(**self.build_lesson(goal, result))
        except Exception:
            _log.debug("workflow lesson save failed", exc_info=True)
