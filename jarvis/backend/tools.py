"""
backend/tools.py

Structured tool definitions for the backend.

This gives each tool:
- a stable name
- an explicit input schema (as a runtime description, not a full
  schema engine)
- whether it supports dry-run
- an explicit failure reason type so errors are descriptive
- a clean contract for execution and dry-run validation

The point is not to build a heavy plugin system. It is to make
tool behavior easier to inspect, test, document, and later
replace without scattering tool contracts across the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Tool failure detail
# ---------------------------------------------------------------------------

@dataclass
class ToolError:
    """Explicit failure reason for a tool call."""

    tool: str
    reason: str
    detail: dict[str, Any] | None = None

    def to_result(self) -> "ToolResult":
        return ToolResult(
            tool=self.tool,
            success=False,
            message=self.reason,
            data={"error": self.reason, **(self.detail or {})},
        )


# ---------------------------------------------------------------------------
# Tool input/output contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDefinition:
    """Static description of one backend tool capability."""

    name: str
    description: str
    supports_dry_run: bool = False
    input_fields: list[dict[str, Any]] = field(default_factory=list)
    idempotent: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "supports_dry_run": self.supports_dry_run,
            "input_fields": list(self.input_fields),
            "idempotent": self.idempotent,
        }


@runtime_checkable
class ToolExecutor(Protocol):
    """Executes one tool by name with explicit input and optional dry-run."""

    def execute(
        self,
        tool: str,
        payload: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> ToolResult:
        ...


@dataclass
class ToolResult:
    """Structured outcome from executing or validating a tool."""

    tool: str
    success: bool
    message: str
    data: dict[str, Any] | None = None

    def as_error(self, reason: str) -> ToolResult:
        return ToolResult(
            tool=self.tool,
            success=False,
            message=reason,
            data={"error": reason, **(self.data or {})},
        )


# ---------------------------------------------------------------------------
# Simple runtime registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Single source of truth for tool metadata and dry-run validation."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_all(self) -> list[dict[str, Any]]:
        return [tool.describe() for tool in self._tools.values()]

    def validate_payload(self, name: str, payload: dict[str, Any]) -> ToolError | None:
        """Return a ToolError if the payload is invalid for this tool, else None."""
        definition = self._tools.get(name)
        if definition is None:
            return ToolError(
                tool=name,
                reason=f"Unknown tool: {name}",
            )

        if not definition.supports_dry_run:
            return ToolError(
                tool=name,
                reason=f"Dry run is not supported for tool: {name}",
            )

        missing: list[str] = []
        for field in definition.input_fields:
            if field.get("required") and field["name"] not in payload:
                missing.append(field["name"])

        if missing:
            return ToolError(
                tool=name,
                reason=f"Missing required fields: {', '.join(missing)}",
                detail={"missing": missing},
            )

        return None

    def describe_execution(
        self,
        name: str,
        payload: dict[str, Any],
        dry_run: bool = False,
    ) -> ToolResult:
        """Describe what executing this tool call would look like."""
        error = self.validate_payload(name, payload)
        if error is not None:
            return error.to_result()

        definition = self._tools[name]
        message = "Would execute {tool}".format(tool=name) if dry_run else "Ready to execute {tool}".format(tool=name)
        return ToolResult(
            tool=name,
            success=True,
            message=message,
            data={"definition": definition.describe(), "payload": dict(payload)},
        )


# ---------------------------------------------------------------------------
# Built-in tool catalog for the current backend
# ---------------------------------------------------------------------------

def build_default_tool_registry() -> ToolRegistry:
    """
    Describe the tools the backend currently uses.

    This is intentionally manual for now. It is meant to be the
    source of truth for tool metadata, not the execution logic.
    """
    registry = ToolRegistry()

    registry.register(ToolDefinition(
        name="open_app",
        description="Open a desktop application by name or alias",
        supports_dry_run=False,
        input_fields=[{"name": "app", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="wait_window",
        description="Wait for and focus a window by title",
        supports_dry_run=False,
        input_fields=[{"name": "title", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="close_app",
        description="Close an application by name or alias",
        supports_dry_run=False,
        input_fields=[{"name": "app", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="open_youtube",
        description="Open YouTube in the default browser",
        supports_dry_run=False,
        input_fields=[],
    ))
    registry.register(ToolDefinition(
        name="search_youtube",
        description="Search YouTube in the default browser",
        supports_dry_run=False,
        input_fields=[{"name": "query", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="search_google",
        description="Search Google in the default browser",
        supports_dry_run=False,
        input_fields=[{"name": "query", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="type",
        description="Type text into the focused field",
        supports_dry_run=True,
        input_fields=[{"name": "text", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="press",
        description="Press a keyboard key",
        supports_dry_run=True,
        input_fields=[{"name": "key", "type": "string", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="hotkey",
        description="Press a keyboard hotkey",
        supports_dry_run=True,
        input_fields=[{"name": "keys", "type": "array", "required": True}],
    ))
    registry.register(ToolDefinition(
        name="click_text",
        description="Click visible text or a semantic target on the screen",
        supports_dry_run=False,
        input_fields=[{"name": "text", "type": "string", "required": True}],
    ))

    return registry
