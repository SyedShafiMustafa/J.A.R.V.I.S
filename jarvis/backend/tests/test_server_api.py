import json
import urllib.request
import pytest
import tempfile
import os

from backend.server import JarvisBackendService
from backend.interfaces import ToolResult
from core.permission import PermissionEngine, ConfirmationStore, PermissionLevel
from core.scheduler import TaskScheduler


def test_server_stop_api():
    service = JarvisBackendService(host="127.0.0.1", port=8991)
    service.start()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8991/api/stop",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert data.get("ok") is True
            assert data.get("stopped") is True
    finally:
        service.stop()


def test_server_confirm_and_pending_actions_api():
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()

    try:
        store = ConfirmationStore(db_path=db_file.name)
        action_id = store.create_pending_action(
            session_id="test-session",
            tool="system_shutdown",
            payload={"reason": "test"},
            permission_level=PermissionLevel.DESTRUCTIVE,
        )

        class _OkRunner:
            def run(self, call, task=None, confirmed=False):
                return ToolResult(call.tool, True, "ok", {"started": True, "completed": True})

        def mock_runtime_builder(*args, **kwargs):
            return {
                "confirmation_store": store,
                "tool_runner": _OkRunner(),
                "lifecycle": None,
            }

        service = JarvisBackendService(host="127.0.0.1", port=8992, runtime_builder=mock_runtime_builder)
        service.start()

        try:
            # Test GET /api/pending_actions
            req_get = urllib.request.Request("http://127.0.0.1:8992/api/pending_actions")
            with urllib.request.urlopen(req_get) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                assert resp.status == 200
                actions = data.get("pending_actions", [])
                assert len(actions) == 1
                assert actions[0]["id"] == action_id
                assert actions[0]["tool"] == "system_shutdown"

            # Test POST /api/confirm (approve)
            req_post = urllib.request.Request(
                "http://127.0.0.1:8992/api/confirm",
                data=json.dumps({"action_id": action_id, "approve": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_post) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                assert resp.status == 200
                assert data.get("action_id") == action_id
                assert data.get("approved") is True

            # Verify action is no longer pending
            action = store.get_pending_action(action_id)
            assert action["status"] in ("approved", "executed")
        finally:
            service.stop()
            store.close()
    finally:
        if os.path.exists(db_file.name):
            os.unlink(db_file.name)
