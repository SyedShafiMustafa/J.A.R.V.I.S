from agents.ollama_errors import OllamaTimeoutError, PlannerValidationError
from backend.bus import BackendBus
from backend.server import (
    JarvisBackendService,
    PHASE_ERROR,
    PHASE_IDLE,
    STATUS_ERROR,
    STATUS_IDLE,
)


class FakeAudio:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def wait(self):
        pass


class FakeMemory:
    def build_context(self, conversation_id, text):
        return [{"role": "user", "content": text}]

    def save_message(self, *args):
        pass

    def save_memory(self, *args):
        pass

    def maybe_summarize(self, *args):
        pass


class FakeSession:
    id = "test-session"
    conversation_id = None

    def note_user(self, text):
        self.user = text

    def note_reply(self, reply):
        self.reply = reply

    def note_decision(self, decision):
        self.decision = decision


class FailingBrain:
    def stream(self, messages):
        raise OllamaTimeoutError("technical timeout")
        yield


class RecoveringBrain:
    def stream(self, messages):
        yield "Recovered."


class FailingPlanner:
    def plan_action(self, text):
        raise PlannerValidationError("invalid plan")


class FakeLifecycle:
    shutdown_requested = False


class FakeOrchestrator:
    def __init__(self):
        self.planner = FailingPlanner()

    def plan_action(self, text):
        return self.planner.plan_action(text)


class ReplyDecision:
    kind = "reply"
    reply = "Hello."
    metadata = {}


class ReplyOrchestrator:
    def decide(self, text):
        return ReplyDecision()


def runtime(brain):
    return {
        "audio": FakeAudio(),
        "brain": brain,
        "memory": FakeMemory(),
        "session": FakeSession(),
    }


def test_brain_failure_is_observable_and_not_stuck_thinking():
    service = JarvisBackendService()
    current = runtime(FailingBrain())

    service._run_chat(current, "hello")

    snapshot = service.state.snapshot()
    assert snapshot["status"] == STATUS_ERROR
    assert snapshot["phase"] == PHASE_ERROR
    assert snapshot["error_message"] == "Ollama took too long to respond. Please try again."
    assert "technical" not in snapshot["error_message"]


def test_successful_request_recovers_after_previous_brain_failure():
    service = JarvisBackendService()
    failed = runtime(FailingBrain())
    service._run_chat(failed, "first")
    service.state.clear_error()

    recovered = runtime(RecoveringBrain())
    service._run_chat(recovered, "second")

    assert recovered["audio"].spoken == ["Recovered."]
    assert service.state.snapshot()["error_message"] is None


def test_planner_failure_leaves_error_phase_not_thinking():
    service = JarvisBackendService()
    audio = FakeAudio()
    runtime = {
        "audio": audio,
        "session": FakeSession(),
        "lifecycle": FakeLifecycle(),
        "tool_runner": None,
        "orchestrator": FakeOrchestrator(),
        "bus": BackendBus(),
        "memory": FakeMemory(),
    }

    service._run_action(runtime, "open YouTube")

    snapshot = service.state.snapshot()
    assert snapshot["phase"] == PHASE_ERROR
    assert snapshot["status"] == STATUS_ERROR
    assert snapshot["error_message"] == "I couldn't safely interpret that action plan."


def test_successful_text_command_returns_to_idle_phase():
    service = JarvisBackendService()
    current = {
        "audio": FakeAudio(),
        "bus": BackendBus(),
        "orchestrator": ReplyOrchestrator(),
        "session": FakeSession(),
    }
    service._runtime_builder = lambda **_: current

    status, payload = service.handle_command({"text": "hello"})

    assert status == 200
    assert payload == {"received": "hello"}
    snapshot = service.state.snapshot()
    assert snapshot["status"] == STATUS_IDLE
    assert snapshot["phase"] == PHASE_IDLE
