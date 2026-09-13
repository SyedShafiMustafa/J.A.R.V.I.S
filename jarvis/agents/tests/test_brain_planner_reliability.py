import json

import pytest
import requests

from agents.brain import JarvisBrain
from agents.ollama_errors import (
    OllamaMalformedResponseError,
    OllamaTimeoutError,
    OllamaUnavailableError,
    PlannerValidationError,
)
from agents.ollama_client import post_with_retries
from agents.planner import TaskPlanner


class FakeResponse:
    def __init__(self, *, lines=None, payload=None):
        self.lines = lines or []
        self.payload = payload
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return self.lines

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


def test_brain_successful_streaming_response(monkeypatch):
    response = FakeResponse(lines=[
        json.dumps({"message": {"content": "Hello there. "}}).encode(),
        json.dumps({"message": {"content": "How are you?"}}).encode(),
    ])
    calls = []
    monkeypatch.setattr(
        "agents.ollama_client.requests.post",
        lambda *args, **kwargs: calls.append(kwargs) or response,
    )

    result = list(JarvisBrain().stream([{"role": "user", "content": "Hi"}]))

    assert result == ["Hello there.", "How are you?"]
    assert calls[0]["timeout"] == 30.0
    assert response.closed


def test_brain_retries_transient_failure_then_recovers(monkeypatch):
    response = FakeResponse(lines=[b'{"message":{"content":"Recovered."}}'])
    attempts = [0]
    def post(*args, **kwargs):
        if attempts[0] == 0:
            attempts[0] += 1
            raise requests.ConnectionError("down")
        return response
    monkeypatch.setattr("agents.ollama_client.time.sleep", lambda _: None)
    monkeypatch.setattr("agents.ollama_client.requests.post", post)

    assert list(JarvisBrain().stream([{"role": "user", "content": "Hi"}])) == ["Recovered."]


def test_brain_unavailable_is_bounded(monkeypatch):
    monkeypatch.setattr("agents.ollama_client.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "agents.ollama_client.requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    with pytest.raises(OllamaUnavailableError):
        list(JarvisBrain().stream([{"role": "user", "content": "Hi"}]))


def test_brain_timeout_is_classified(monkeypatch):
    monkeypatch.setattr("agents.ollama_client.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "agents.ollama_client.requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("slow")),
    )

    with pytest.raises(OllamaTimeoutError):
        list(JarvisBrain().stream([{"role": "user", "content": "Hi"}]))


def planner_response(plan):
    return FakeResponse(payload={"message": {"content": json.dumps(plan)}})


def test_planner_accepts_valid_plan(monkeypatch):
    response = planner_response({
        "goal": "Open YouTube",
        "steps": [{"tool": "open_youtube"}],
    })
    monkeypatch.setattr("agents.ollama_client.requests.post", lambda *a, **k: response)
    assert TaskPlanner().create_plan("Open YouTube")["goal"] == "Open YouTube"


@pytest.mark.parametrize("plan", [
    {"goal": "x"},
    {"goal": "x", "steps": [{"tool": "unknown"}]},
    {"goal": "x", "steps": [{"tool": "type", "text": 4}]},
    {"goal": "x", "steps": [{"tool": "open_youtube", "url": "unsafe"}]},
    {"goal": "x", "steps": [{"tool": "click_text"}]},
])
def test_planner_rejects_invalid_shapes(monkeypatch, plan):
    response = planner_response(plan)
    monkeypatch.setattr("agents.ollama_client.requests.post", lambda *a, **k: response)
    with pytest.raises(PlannerValidationError):
        TaskPlanner().create_plan("do it")


def test_planner_rejects_malformed_json(monkeypatch):
    response = FakeResponse(payload={"message": {"content": "{not json"}})
    monkeypatch.setattr("agents.ollama_client.requests.post", lambda *a, **k: response)
    with pytest.raises(PlannerValidationError):
        TaskPlanner().create_plan("do it")


def test_planner_transport_failure_is_not_retried_as_validation(monkeypatch):
    monkeypatch.setattr("agents.ollama_client.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "agents.ollama_client.requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    with pytest.raises(OllamaUnavailableError):
        TaskPlanner().create_plan("do it")
