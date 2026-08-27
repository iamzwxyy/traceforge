from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from traceforge.api import _choose_macos_directory, create_app
from traceforge.config import Settings
from traceforge.models import ToolCall
from traceforge.provider import ModelResponse, ProviderError, ScriptedProvider


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
                    "acceptance_checks": [{"id": "observed", "label": "Workspace was observed"}],
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
        status_payload = client.get("/api/status").json()
        assert status_payload["sandbox"]["backend"] in {"seatbelt", "bubblewrap", "none"}
        assert isinstance(status_payload["sandbox"]["enforced"], bool)
        assert status_payload["sandbox"]["detail"]
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

        decision = client.post(f"/api/runs/{run_id}/plan-decision", json={"decision": "approve"})
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
        browsed = client.get("/api/filesystem/directories", params={"path": str(tmp_path)})
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
        assert client.get("/api/status").json()["last_workspace"] == str(direct_root.resolve())


def test_direct_task_allocates_an_isolated_workspace(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Direct", "create_direct_workspace": True},
        )

        assert created.status_code == 201
        run = _wait_for_state(client, created.json()["id"], "awaiting_plan_approval")
        workspace = Path(str(run["workspace"]))
        assert workspace.parent == settings.workspace.resolve()
        assert workspace.name.startswith("traceforge-task-")
        assert workspace.is_dir()
        assert run["project_id"] is None

        ambiguous = client.post(
            "/api/runs",
            json={
                "task": "Ambiguous",
                "workspace": str(settings.workspace),
                "create_direct_workspace": True,
            },
        )
        assert ambiguous.status_code == 422
        assert "Choose a project" in ambiguous.text


def test_fixed_demo_rejects_unrelated_tasks(settings: Settings) -> None:
    suggested_task = "Build the deterministic demo"
    app = create_app(
        replace(settings, demo_mode=True, suggested_task=suggested_task),
        provider=ScriptedProvider([]),
    )

    with TestClient(app) as client:
        assert client.get("/api/status").json()["mode"] == "demo"

        rejected = client.post("/api/runs", json={"task": "你好"})
        assert rejected.status_code == 422
        assert "fixed demo" in rejected.json()["detail"]

        retargeted = client.post(
            "/api/runs",
            json={"task": suggested_task, "workspace": str(settings.workspace.parent)},
        )
        assert retargeted.status_code == 422
        assert "disposable demo workspace" in retargeted.json()["detail"]
        assert client.get("/api/runs").json() == []


def test_macos_directory_picker_reports_capability(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    app = create_app(settings, provider=ScriptedProvider([]))
    monkeypatch.setattr("traceforge.api.platform.system", lambda: "Darwin")
    monkeypatch.setattr("traceforge.api._choose_macos_directory", lambda _initial: str(selected))

    with TestClient(app) as client:
        picked = client.post("/api/filesystem/choose-directory")
        assert picked.status_code == 200
        assert picked.json() == {"supported": True, "path": str(selected)}

        monkeypatch.setattr("traceforge.api.platform.system", lambda: "Linux")
        fallback = client.post("/api/filesystem/choose-directory")
        assert fallback.json() == {"supported": False, "path": None}


def test_macos_directory_picker_normalizes_success_and_cancel(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    calls: list[list[str]] = []

    def choose(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=f"{selected}/\n", stderr="")

    monkeypatch.setattr("traceforge.api.subprocess.run", choose)
    assert _choose_macos_directory(settings.workspace) == str(selected.resolve())
    assert calls[0][0] == "osascript"
    assert calls[0][-1] == str(settings.workspace.resolve())

    monkeypatch.setattr(
        "traceforge.api.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="User canceled. (-128)"
        ),
    )
    assert _choose_macos_directory(settings.workspace) is None

    monkeypatch.setattr(
        "traceforge.api.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="automation unavailable"
        ),
    )
    with pytest.raises(ValueError, match="did not return"):
        _choose_macos_directory(settings.workspace)


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


def test_provider_config_stores_direct_api_key_in_owner_only_file(
    settings: Settings,
) -> None:
    secret = "tf-test-direct-key-4f392a"
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        updated = client.put(
            "/api/provider",
            json={
                "model": "direct-key-model",
                "base_url": "https://provider.example/v1",
                "credential_file": None,
                "api_key": secret,
            },
        )

        assert updated.status_code == 200
        payload = updated.json()
        credential = Path(payload["credential_file"])
        assert payload["credential_source"] == "file"
        assert payload["api_key_configured"] is True
        assert "api_key" not in payload
        assert secret not in updated.text
        assert credential.parent == settings.data_dir.resolve()
        assert credential.read_text(encoding="utf-8") == f"{secret}\n"
        assert credential.stat().st_mode & 0o777 == 0o600

    for database_file in settings.data_dir.glob("traceforge.db*"):
        assert secret.encode() not in database_file.read_bytes()


def test_provider_config_rejects_multiple_credential_sources(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        response = client.put(
            "/api/provider",
            json={
                "model": "model",
                "credential_file": "/tmp/provider.key",
                "api_key": "must-not-be-reflected",
            },
        )

        assert response.status_code == 422
        assert "either an API key or a credential file" in response.text
        assert "must-not-be-reflected" not in response.text


def test_provider_config_rejects_loose_credential_permissions(settings: Settings, tmp_path) -> None:
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


def test_api_allows_provider_repair_then_resume_after_transient_outage(
    settings: Settings,
) -> None:
    class RecoveringProvider:
        def __init__(self) -> None:
            self.outcomes = [
                ProviderError("offline", retryable=True, category="connection"),
                ProviderError("offline", retryable=True, category="connection"),
                ProviderError("offline", retryable=True, category="connection"),
                _plan_response(),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Recovered", "evidence": ["plan"]},
                        )
                    ]
                ),
            ]

        async def complete(self, messages, tools=None) -> ModelResponse:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, ProviderError):
                raise outcome
            return outcome

    app = create_app(
        replace(settings, model_retry_delay=0),
        provider=RecoveringProvider(),
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Recover this run", "verifier_enabled": False},
        )
        run_id = created.json()["id"]
        interrupted = _wait_for_state(client, run_id, "interrupted")
        assert "preserved" in interrupted["error"]

        updated = client.put(
            "/api/provider",
            json={
                "model": "repaired-model",
                "base_url": "https://provider.example/v1",
                "credential_file": None,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["model"] == "repaired-model"

        resumed = client.post(f"/api/runs/{run_id}/resume")
        assert resumed.status_code == 200
        _wait_for_state(client, run_id, "awaiting_plan_approval")
        approved = client.post(f"/api/runs/{run_id}/plan-decision", json={"decision": "approve"})
        assert approved.status_code == 202
        completed = _wait_for_state(client, run_id, "succeeded")
        assert completed["error"] is None

        events = client.get(f"/api/runs/{run_id}/events").json()
        assert [event["type"] for event in events].count("model.retry") == 2
        assert any(event["type"] == "run.resumed" for event in events)
