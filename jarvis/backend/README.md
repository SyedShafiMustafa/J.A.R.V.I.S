# JARVIS Backend

This package is the internal backend layer for the JARVIS desktop assistant.

Its purpose is to give the runtime a cleaner shape than a single
script with lots of implicit state. The backend is meant to be:

- replaceable, piece by piece
- testable with fakes instead of real audio/desktop/LLM dependencies
- observable through a small event bus and structured logging
- robust against transient failure via retry and explicit error types

## What lives here

- `interfaces.py` — core backend contracts and fake implementations
- `models.py` — session and task state types
- `bus.py` — small in-memory event/observation bus
- `retry.py` — retry helper for transient failures
- `lifecycle.py` — startup/shutdown lifecycle and cancellation
- `tools.py` — tool definitions, registry, errors, dry-run support
- `validation.py` — startup validation for required config/assets
- `logging.py` — lightweight structured logging helpers
- `smoke.py` — runnable backend test suite using fakes

## The main ideas

### Contracts between layers

The backend defines explicit interfaces so modules do not reach directly
into each other:

- `AudioProvider` for wake, recording, STT, and TTS
- `ToolCall / ToolResult / ToolRunner` for tool execution
- `Orchestrator / OrchestratorDecision` for deciding what to do next

### Session and task state

A `Session` carries one conversation’s context.

A `Task` represents an in-progress action plan and its runtime status,
including start, complete, fail, and cancel transitions.

### Events

`BackendBus` is a simple publish/subscribe bus for backend observations.

Common events include:

- `wake.detected`
- `wake.listening`
- `audio.start`
- `audio.stop`
- `stt.ready`
- `tool.started`
- `tool.finished`
- `tool.failed`
- `session.started`
- `session.ended`
- `task.started`
- `task.completed`
- `task.failed`
- `task.cancelled`

Built-in observers include:

- `LoggingObserver` for stderr debugging output
- `ReplayObserver` for capturing events in tests or analysis

### Errors

The backend uses a small error taxonomy:

- `BackendError`
- `ConfigurationError`
- `TransientError`
- `PermanentError`

Only transient failures should be retried. Configuration and permanent
failures should fail fast and loudly.

### Retry

`retry()` supports:

- configurable attempts
- base delay and exponential backoff
- max delay
- optional jitter
- optional total timeout cap

It is meant for backend operations where a short pause and retry is
acceptable, not for indefinite retry loops.

### Tools

`tools.py` provides:

- `ToolDefinition` for tool metadata and input fields
- `ToolRegistry` for looking up tools by name
- `ToolError` for explicit failure reasons
- dry-run support for tools that can describe what they would do
  without executing

### Startup and lifecycle

`validation.py` checks required settings and local model paths before
the main loop starts.

`lifecycle.py` provides:

- running state
- shutdown request
- cleanup callbacks
- session end announcement

### Tests

`smoke.py` can be run directly:

```bash
python backend/smoke.py
```

It covers core backend contracts using fakes, and is intended to stay
lightweight and reusable as the backend grows.

## Current status

This backend is still evolving. Some parts of the existing runtime
remain close to their original shape, especially around audio and the
voice loop. The backend modules exist so those pieces can be improved
incrementally without making the whole app harder to understand.

## How to extend it

Good next steps are:

- make the voice loop use the orchestrator/session/tool contracts more
  completely
- add more tool metadata and better dry-run coverage
- add more instrumentation around wake, STT, TTS, and tool boundaries
- add more fake-based tests for the runtime adapters
