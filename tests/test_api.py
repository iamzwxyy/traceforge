from __future__ import annotations

import time

from fastapi.testclient import TestClient

from traceforge.api import create_app
from traceforge.config import Settings
from traceforge.models import ToolCall
from traceforge.provider import ModelResponse, ScriptedProvider


def _wait_for_state(client: TestClient, run_id: str, state: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["state"] == state:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"Run did not reach {state}")


def _plan_response() -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id="plan",
                name="submit_plan",
                arguments={
                    "summary": "Observe the workspace",
                    "steps": [{"id": "observe", "title": "Observe"}],
                    "acceptance_checks": [
                        {"id": "observed", "label": "Workspace was observed"}
                    ],
                },
            )
        ]
    )


def test_api_run_lifecycle_and_public_shape(settings: Settings) -> None:
    provider = ScriptedProvider(
        [
            _plan_response(),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Observed", "evidence": ["No mutation required"]},
                    )
                ]
            ),
        ]
    )
    app = create_app(settings, provider=provider)
    with TestClient(app) as client:
        response = client.post(
            "/api/runs", json={"task": "Observe this workspace", "verifier_enabled": False}
        )
        assert response.status_code == 201
        run_id = response.json()["id"]
        waiting = _wait_for_state(client, run_id, "awaiting_plan_approval")
        assert "messages" not in waiting

        decision = client.post(
            f"/api/runs/{run_id}/plan-decision", json={"decision": "approve"}
        )
        assert decision.status_code == 202
        completed = _wait_for_state(client, run_id, "succeeded")
        assert completed["verification"]["verdict"] == "inconclusive"

        events = client.get(f"/api/runs/{run_id}/events").json()
        assert events[-1]["type"] == "run.completed"


def test_api_rejects_second_active_run(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))
    with TestClient(app) as client:
        first = client.post("/api/runs", json={"task": "First"})
        assert first.status_code == 201
        run_id = first.json()["id"]
        _wait_for_state(client, run_id, "awaiting_plan_approval")

        second = client.post("/api/runs", json={"task": "Second"})
        assert second.status_code == 409
        assert "active" in second.json()["detail"]


def test_websocket_replays_persisted_events(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))
    with TestClient(app) as client:
        response = client.post("/api/runs", json={"task": "Observe"})
        run_id = response.json()["id"]
        _wait_for_state(client, run_id, "awaiting_plan_approval")

        with client.websocket_connect(f"/api/runs/{run_id}/events?after_seq=0") as websocket:
            first = websocket.receive_json()
            assert first["seq"] == 1
            assert first["type"] == "state.changed"

