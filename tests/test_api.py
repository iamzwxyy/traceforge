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
        assert waiting["context_tokens"] > 0
        assert waiting["context_tokens"] < waiting["context_limit"]
        assert waiting["context_limit"] == settings.context_limit

        decision = client.post(
            f"/api/runs/{run_id}/plan-decision", json={"decision": "approve"}
        )
        assert decision.status_code == 202
        completed = _wait_for_state(client, run_id, "succeeded")
        assert completed["verification"]["verdict"] == "inconclusive"

        events = client.get(f"/api/runs/{run_id}/events").json()
        assert events[-1]["type"] == "run.completed"

        proof = client.get(f"/api/runs/{run_id}/proof-pack")
        assert proof.status_code == 200
        assert proof.json()["proof_status"] == "checks_only"
        downloaded = client.get(f"/api/runs/{run_id}/proof-pack.md")
        assert downloaded.status_code == 200
        assert "attachment;" in downloaded.headers["content-disposition"]
        assert "TraceForge Proof Pack" in downloaded.text


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


def test_projects_direct_tasks_and_directory_browser(settings: Settings, tmp_path) -> None:
    provider = ScriptedProvider([_plan_response(), _plan_response()])
    existing_root = tmp_path / "existing-project"
    existing_root.mkdir()
    created_root = tmp_path / "created-project"
    direct_root = tmp_path / "direct-work"
    direct_root.mkdir()
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        browsed = client.get(
            "/api/filesystem/directories", params={"path": str(tmp_path)}
        )
        assert browsed.status_code == 200
        assert {entry["name"] for entry in browsed.json()["children"]} >= {
            "existing-project",
            "direct-work",
        }

        opened = client.post(
            "/api/projects",
            json={"name": "Existing", "root": str(existing_root)},
        )
        created = client.post(
            "/api/projects",
            json={
                "name": "Created",
                "root": str(created_root),
                "create_directory": True,
            },
        )
        assert opened.status_code == 201
        assert created.status_code == 201 and created_root.is_dir()
        assert len(client.get("/api/projects").json()) == 2

        direct = client.post(
            "/api/runs",
            json={"task": "Direct", "workspace": str(direct_root)},
        )
        assert direct.status_code == 201
        direct_run = _wait_for_state(client, direct.json()["id"], "awaiting_plan_approval")
        assert direct_run["project_id"] is None
        assert direct_run["workspace"] == str(direct_root.resolve())

        project = client.post(
            "/api/runs",
            json={"task": "Project", "project_id": opened.json()["id"]},
        )
        assert project.status_code == 201
        project_run = _wait_for_state(client, project.json()["id"], "awaiting_plan_approval")
        assert project_run["project_id"] == opened.json()["id"]
        assert project_run["workspace"] == str(existing_root.resolve())
        assert client.get("/api/status").json()["last_workspace"] == str(
            direct_root.resolve()
        )


def test_provider_config_uses_a_file_reference_without_returning_secret(
    settings: Settings, tmp_path
) -> None:
    credential = tmp_path / "provider.key"
    credential.write_text("credential-value\n")
    credential.chmod(0o600)
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        updated = client.put(
            "/api/provider",
            json={
                "model": "deepseek-v4-flash-vision-exp",
                "base_url": "https://api.deepseek.com",
                "credential_file": str(credential),
            },
        )
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["credential_source"] == "file"
        assert payload["credential_file"] == str(credential.resolve())
        assert payload["api_key_configured"] is True
        assert "credential-value" not in updated.text

        tested = client.post("/api/provider/test")
        assert tested.status_code == 200
        assert tested.json()["ok"] is True


def test_provider_config_rejects_loose_credential_permissions(
    settings: Settings, tmp_path
) -> None:
    credential = tmp_path / "provider.key"
    credential.write_text("probe\n")
    credential.chmod(0o644)
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        response = client.put(
            "/api/provider",
            json={
                "model": "model",
                "base_url": "https://provider.example/v1",
                "credential_file": str(credential),
            },
        )

        assert response.status_code == 422
        assert "chmod 600" in response.json()["detail"]
