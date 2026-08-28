from __future__ import annotations

import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from traceforge.api import _choose_macos_directory, _open_workspace_directory, create_app
from traceforge.config import Settings
from traceforge.models import (
    ConversationTurn,
    ProviderConfig,
    RunRecord,
    RunState,
    ToolCall,
)
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


def _answer_response(content: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id="answer",
                name="respond_to_user",
                arguments={"content": content},
            )
        ]
    )


def test_api_represents_direct_answer_without_completion_evidence(
    settings: Settings,
) -> None:
    app = create_app(settings, provider=ScriptedProvider([_answer_response("你好!")]))

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "task": "你好",
                "mode": "agent",
                "create_direct_workspace": True,
            },
        )
        assert created.status_code == 201
        answered = _wait_for_state(client, created.json()["id"], "answered")

        assert answered["plan"] is None
        assert answered["clarification"] is None
        assert answered["verification"] is None
        assert answered["turns"][-1]["outcome"] == "answered"
        assert answered["turns"][-1]["summary"] == "你好!"
        assert client.get(f"/api/runs/{answered['id']}/diff").json() == {"diff": ""}
        assert client.get(f"/api/runs/{answered['id']}/proof-pack").status_code == 409
        assert client.get(f"/api/runs/{answered['id']}/proof-pack.md").status_code == 409
        assert client.post(f"/api/runs/{answered['id']}/rollback").status_code == 409


def test_instance_health_and_ipv6_origins_cover_rest_and_websocket(
    settings: Settings,
) -> None:
    instance_id = "test-instance-identity"
    config_fingerprint = "a" * 64
    app = create_app(
        settings,
        provider=ScriptedProvider([_answer_response("ok")]),
        instance_id=instance_id,
        instance_config_fingerprint=config_fingerprint,
    )
    ipv6_origin = "http://[::1]:8765"

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {
            "status": "ok",
            "version": app.version,
            "instance_id": instance_id,
        }
        created = client.post(
            "/api/runs",
            headers={"origin": ipv6_origin, "sec-fetch-site": "same-origin"},
            json={"task": "Answer", "create_direct_workspace": True},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        with client.websocket_connect(
            f"/api/runs/{run_id}/events?after_seq=0",
            headers={"origin": ipv6_origin},
        ) as websocket:
            assert websocket.receive_json()["seq"] == 1


def test_api_run_lifecycle_and_public_shape(settings: Settings) -> None:
    provider = ScriptedProvider(
        [
            _plan_response(),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Observed"},
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
        assert status_payload["limits"]["context"] == settings.context_limit
        assert status_payload["limits"]["context_source"] == "fallback"
        response = client.post(
            "/api/runs",
            json={
                "task": "Observe this workspace",
                "verifier_enabled": False,
                "mode": "plan",
            },
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

        plan_download = client.get(f"/api/runs/{run_id}/plan.md")
        assert plan_download.status_code == 200
        assert "attachment;" in plan_download.headers["content-disposition"]
        assert "# Implementation plan" in plan_download.text
        assert "## Validation" in plan_download.text

        events = client.get(f"/api/runs/{run_id}/events").json()
        assert events[-1]["type"] == "run.completed"

        proof = client.get(f"/api/runs/{run_id}/proof-pack")
        assert proof.status_code == 200
        assert proof.json()["proof_status"] == "checks_only"
        downloaded = client.get(f"/api/runs/{run_id}/proof-pack.md")
        assert downloaded.status_code == 200
        assert "attachment;" in downloaded.headers["content-disposition"]
        assert "TraceForge Proof Pack" in downloaded.text


def test_api_defaults_to_agent_mode_and_supports_same_task_follow_up(
    settings: Settings,
) -> None:
    provider = ScriptedProvider(
        [
            _plan_response(),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-1",
                        name="finish",
                        arguments={
                            "summary": "Observed the first request",
                        },
                    )
                ]
            ),
            _plan_response(),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={
                            "summary": "Observed the follow-up",
                        },
                    )
                ]
            ),
        ]
    )
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Observe this workspace", "verifier_enabled": False},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        assert created.json()["mode"] == "agent"
        assert created.json()["approval_mode"] == "automatic"
        assert created.json()["reasoning_effort"] == "auto"
        first = _wait_for_state(client, run_id, "succeeded")
        assert first["plan_gate"]["decision"] == "agent_continues"
        assert len(first["turns"]) == 1

        follow_up = client.post(
            f"/api/runs/{run_id}/turns",
            json={
                "prompt": "Check the edge case too",
                "approval_mode": "manual",
            },
        )
        assert follow_up.status_code == 200
        assert follow_up.json()["id"] == run_id
        assert follow_up.json()["approval_mode"] == "manual"
        second = _wait_for_state(client, run_id, "succeeded")
        assert len(second["turns"]) == 2
        assert second["turns"][1]["request"] == "Check the edge case too"
        assert second["turns"][0]["approval_mode"] == "automatic"
        assert second["turns"][1]["approval_mode"] == "manual"
        assert all(turn["reasoning_effort"] == "auto" for turn in second["turns"])
        assert all(turn["outcome"] == "succeeded" for turn in second["turns"])


def test_api_freezes_model_supported_reasoning_per_turn(settings: Settings) -> None:
    provider = ScriptedProvider(
        [_answer_response("first"), _answer_response("second")]
    )
    app = create_app(
        replace(settings, model="gpt-5.6-sol", base_url=None), provider=provider
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "First question", "reasoning_effort": "high"},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        _wait_for_state(client, run_id, "answered")

        follow_up = client.post(
            f"/api/runs/{run_id}/turns",
            json={"prompt": "Second question", "reasoning_effort": "low"},
        )
        assert follow_up.status_code == 200
        completed = _wait_for_state(client, run_id, "answered")

        assert completed["reasoning_effort"] == "low"
        assert [turn["reasoning_effort"] for turn in completed["turns"]] == [
            "high",
            "low",
        ]
        assert provider.reasoning_efforts == ["high", "low"]
        started = [
            event["payload"]
            for event in client.get(f"/api/runs/{run_id}/events").json()
            if event["type"] == "turn.started"
        ]
        assert [payload["reasoning_effort"] for payload in started] == ["high", "low"]


def test_api_rejects_unadvertised_reasoning_before_model_call(
    settings: Settings,
) -> None:
    provider = ScriptedProvider([_answer_response("must not run")])
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/runs",
            json={"task": "Do work", "reasoning_effort": "high"},
        )

        assert rejected.status_code == 422
        assert "exact model route" in rejected.json()["detail"]
        assert provider.requests == []


def test_api_validates_approval_modes(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_answer_response("ok")]))

    with TestClient(app) as client:
        invalid = client.post(
            "/api/runs",
            json={"task": "Answer", "approval_mode": "unbounded"},
        )
        assert invalid.status_code == 422

        full = client.post(
            "/api/runs",
            json={
                "task": "Answer",
                "approval_mode": "full_access",
                "create_direct_workspace": True,
            },
        )
        assert full.status_code == 201
        assert full.json()["approval_mode"] == "full_access"
        assert full.json()["turns"][0]["approval_mode"] == "full_access"


def test_api_rejects_second_active_run(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))
    with TestClient(app) as client:
        first = client.post(
            "/api/runs",
            json={"task": "First", "workspace": str(settings.workspace), "mode": "plan"},
        )
        assert first.status_code == 201
        run_id = first.json()["id"]
        _wait_for_state(client, run_id, "awaiting_plan_approval")

        second = client.post(
            "/api/runs", json={"task": "Second", "workspace": str(settings.workspace)}
        )
        assert second.status_code == 409
        assert "active" in second.json()["detail"]


def test_websocket_replays_persisted_events(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))
    with TestClient(app) as client:
        response = client.post("/api/runs", json={"task": "Observe", "mode": "plan"})
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
            json={"task": "Direct", "workspace": str(direct_root), "mode": "plan"},
        )
        assert direct.status_code == 201
        direct_run = _wait_for_state(client, direct.json()["id"], "awaiting_plan_approval")
        assert direct_run["project_id"] is None
        assert direct_run["workspace"] == str(direct_root.resolve())

        project = client.post(
            "/api/runs",
            json={
                "task": "Project",
                "project_id": opened.json()["id"],
                "mode": "plan",
            },
        )
        assert project.status_code == 201
        project_run = _wait_for_state(client, project.json()["id"], "awaiting_plan_approval")
        assert project_run["project_id"] == opened.json()["id"]
        assert project_run["workspace"] == str(existing_root.resolve())
        assert client.get("/api/status").json()["last_workspace"] == str(direct_root.resolve())


def test_direct_task_allocates_an_isolated_workspace(settings: Settings) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response(), _plan_response()]))

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Direct", "mode": "plan"},
        )

        assert created.status_code == 201
        run = _wait_for_state(client, created.json()["id"], "awaiting_plan_approval")
        workspace = Path(str(run["workspace"]))
        assert workspace.parent == settings.workspace.resolve()
        assert workspace.name.startswith("traceforge-task-")
        assert workspace.is_dir()
        assert run["project_id"] is None

        explicit = client.post(
            "/api/runs",
            json={"task": "Explicit direct", "create_direct_workspace": True, "mode": "plan"},
        )
        assert explicit.status_code == 201
        explicit_run = _wait_for_state(
            client, explicit.json()["id"], "awaiting_plan_approval"
        )
        explicit_workspace = Path(str(explicit_run["workspace"]))
        assert explicit_workspace.parent == settings.workspace.resolve()
        assert explicit_workspace != workspace
        assert explicit_workspace.stat().st_mode & 0o077 == 0

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
        for invalid_workspace in ("", "   "):
            invalid = client.post(
                "/api/runs", json={"task": "Invalid", "workspace": invalid_workspace}
            )
            assert invalid.status_code == 422
            assert "Workspace must not be empty" in invalid.text


def test_failed_direct_task_creation_removes_only_its_empty_directory(
    settings: Settings, monkeypatch
) -> None:
    retained = settings.workspace / "existing-user-directory"
    retained.mkdir()
    app = create_app(settings, provider=ScriptedProvider([]))

    async def fail_start(*_args, **_kwargs):
        raise ValueError("provider is unavailable")

    monkeypatch.setattr(app.state.runtime, "start_run", fail_start)

    with TestClient(app) as client:
        response = client.post("/api/runs", json={"task": "Cannot start"})

    assert response.status_code == 422
    assert list(settings.workspace.iterdir()) == [retained]


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
        unsafe_mode = client.post(
            "/api/runs",
            json={"task": suggested_task, "approval_mode": "full_access"},
        )
        assert unsafe_mode.status_code == 422
        assert "automatic approval" in unsafe_mode.json()["detail"]
        explicit_reasoning = client.post(
            "/api/runs",
            json={"task": suggested_task, "reasoning_effort": "high"},
        )
        assert explicit_reasoning.status_code == 422
        assert "model-default reasoning" in explicit_reasoning.json()["detail"]
        assert client.get("/api/runs").json() == []


def test_resume_preserves_effort_and_blocks_an_incompatible_new_route(
    settings: Settings,
) -> None:
    official = replace(settings, model="gpt-5.6-sol", base_url=None)
    app = create_app(official, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        run = RunRecord(
            id="reasoning-resume",
            task="Resume precisely",
            workspace=str(settings.workspace),
            state=RunState.INTERRUPTED,
            reasoning_effort="high",
            turns=[
                ConversationTurn(
                    index=1,
                    request="Resume precisely",
                    reasoning_effort="high",
                )
            ],
            interrupted_from=RunState.PLANNING,
        )
        app.state.storage.create_run(run)
        updated = client.put(
            "/api/provider",
            json={
                "model": "custom-model",
                "base_url": "https://provider.example/v1",
            },
        )
        assert updated.status_code == 200

        rejected = client.post("/api/runs/reasoning-resume/resume")

        assert rejected.status_code == 409
        assert "incompatible" in rejected.json()["detail"]
        persisted = client.get("/api/runs/reasoning-resume").json()
        assert persisted["state"] == "interrupted"
        assert persisted["reasoning_effort"] == "high"


def test_macos_directory_picker_reports_capability(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    recent = tmp_path / "recent"
    recent.mkdir()
    initial_paths: list[Path] = []
    app = create_app(settings, provider=ScriptedProvider([]))
    monkeypatch.setattr("traceforge.api.platform.system", lambda: "Darwin")

    def choose(initial: Path) -> str:
        initial_paths.append(initial)
        return str(selected)

    monkeypatch.setattr("traceforge.api._choose_macos_directory", choose)

    with TestClient(app) as client:
        client.app.state.storage.set_preference("last_workspace", str(recent))
        picked = client.post("/api/filesystem/choose-directory")
        assert picked.status_code == 200
        assert picked.json() == {"supported": True, "path": str(selected)}
        assert initial_paths == [recent.resolve()]

        client.app.state.storage.set_preference(
            "last_workspace", str(tmp_path / "removed-project")
        )
        fallback_to_home = client.post("/api/filesystem/choose-directory")
        assert fallback_to_home.status_code == 200
        assert initial_paths[-1] == Path.home().resolve()
        assert client.get("/api/status").json()["last_workspace"] == str(Path.home().resolve())

        monkeypatch.setattr("traceforge.api.Path.home", lambda: tmp_path / "missing-home")
        assert client.get("/api/status").json()["last_workspace"] == str(
            settings.workspace.resolve()
        )

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


def test_open_workspace_uses_fixed_argv_and_scrubbed_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr("traceforge.api.platform.system", lambda: "Darwin")
    monkeypatch.setenv("TRACEFORGE_TEST_API_KEY", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/must-not-leak.sock")
    monkeypatch.setenv("TRACEFORGE_TEST_PLAIN", "visible")

    def run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("traceforge.api.subprocess.run", run)

    result = _open_workspace_directory(settings.workspace)

    assert result == {"supported": True, "opened": True, "application": "Finder"}
    assert calls[0][0] == ["/usr/bin/open", "-R", str(settings.workspace.resolve())]
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert "TRACEFORGE_TEST_API_KEY" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["TRACEFORGE_TEST_PLAIN"] == "visible"
    assert calls[0][1]["timeout"] == 10


def test_open_workspace_handles_linux_capability_and_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("traceforge.api.platform.system", lambda: "Linux")
    monkeypatch.setattr("traceforge.api.shutil.which", lambda _name: "/usr/bin/xdg-open")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        "traceforge.api.subprocess.run",
        lambda arguments, **_kwargs: calls.append(arguments),
    )

    assert _open_workspace_directory(settings.workspace) == {
        "supported": False,
        "opened": False,
        "application": None,
    }
    assert calls == []

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("traceforge.api.shutil.which", lambda _name: None)
    assert _open_workspace_directory(settings.workspace)["supported"] is False

    workspace_launcher = settings.workspace / "xdg-open"
    workspace_launcher.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        "traceforge.api.shutil.which", lambda _name: str(workspace_launcher)
    )
    assert _open_workspace_directory(settings.workspace)["supported"] is False
    assert calls == []

    monkeypatch.setattr("traceforge.api.shutil.which", lambda _name: "/usr/bin/xdg-open")
    monkeypatch.setattr(
        "traceforge.api.subprocess.run",
        lambda arguments, **_kwargs: (
            calls.append(arguments)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    assert _open_workspace_directory(settings.workspace) == {
        "supported": True,
        "opened": True,
        "application": "file_manager",
    }
    assert calls[-1] == ["/usr/bin/xdg-open", str(settings.workspace.resolve())]

    monkeypatch.setattr(
        "traceforge.api.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="private"),
    )
    with pytest.raises(ValueError, match="could not open"):
        _open_workspace_directory(settings.workspace)


@pytest.mark.parametrize(
    "failure",
    [OSError("unavailable"), subprocess.TimeoutExpired("open", 10)],
)
def test_open_workspace_hides_launcher_exception_details(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    monkeypatch.setattr("traceforge.api.platform.system", lambda: "Darwin")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr("traceforge.api.subprocess.run", fail)
    with pytest.raises(ValueError, match="could not be opened") as captured:
        _open_workspace_directory(settings.workspace)
    assert "unavailable" not in str(captured.value)


def test_open_workspace_endpoint_is_run_scoped_and_rejects_cross_site_or_retargeting(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings, provider=ScriptedProvider([_plan_response()]))
    opened: list[Path] = []

    def open_directory(path: Path) -> dict[str, object]:
        opened.append(path)
        return {"supported": True, "opened": True, "application": "Finder"}

    monkeypatch.setattr("traceforge.api._open_workspace_directory", open_directory)

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "task": "Observe",
                "mode": "plan",
                "workspace": str(settings.workspace),
            },
        )
        run_id = created.json()["id"]
        _wait_for_state(client, run_id, "awaiting_plan_approval")

        response = client.post(f"/api/runs/{run_id}/open-workspace")
        assert response.status_code == 200
        assert response.json() == {
            "supported": True,
            "opened": True,
            "application": "Finder",
        }
        assert opened == [settings.workspace.resolve()]
        assert client.post("/api/runs/missing/open-workspace").status_code == 404

        denied = client.post(
            f"/api/runs/{run_id}/open-workspace",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert denied.status_code == 403
        assert len(opened) == 1

        original = settings.workspace.with_name("workspace-original")
        replacement = settings.workspace.with_name("workspace-replacement")
        settings.workspace.rename(original)
        replacement.mkdir()
        settings.workspace.symlink_to(replacement, target_is_directory=True)
        try:
            retargeted = client.post(f"/api/runs/{run_id}/open-workspace")
            assert retargeted.status_code == 422
            assert "original directory" in retargeted.json()["detail"]
            assert len(opened) == 1
        finally:
            settings.workspace.unlink()
            original.rename(settings.workspace)
            replacement.rmdir()


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
        assert payload["context_window"] is None
        assert payload["resolved_context_window"] == 1_000_000
        assert payload["context_window_source"] == "catalog"
        assert payload["supported_reasoning_efforts"] == [
            "auto",
            "none",
            "low",
            "high",
            "max",
        ]
        assert payload["default_reasoning_effort"] == "high"
        assert payload["reasoning_effort_source"] == "deepseek_catalog"
        assert payload["reasoning_effort_catalog_version"] == "2026-08-28"
        assert "credential-value" not in updated.text

        created = client.post(
            "/api/runs",
            json={
                "task": "Inspect with the resolved context snapshot",
                "verifier_enabled": False,
                "mode": "agent",
                "create_direct_workspace": True,
            },
        )
        assert created.status_code == 201
        assert created.json()["context_limit"] == 1_000_000

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
                "context_window": 240_000,
                "api_key": secret,
            },
        )

        assert updated.status_code == 200
        payload = updated.json()
        credential = Path(payload["credential_file"])
        assert payload["credential_source"] == "file"
        assert payload["api_key_configured"] is True
        assert payload["context_window"] == 240_000
        assert payload["resolved_context_window"] == 240_000
        assert payload["context_window_source"] == "configured"
        assert payload["supported_reasoning_efforts"] == ["auto"]
        assert payload["default_reasoning_effort"] is None
        assert payload["reasoning_effort_source"] == "provider_default"
        assert "api_key" not in payload
        assert secret not in updated.text
        assert credential.parent == (
            settings.data_dir / "provider-credentials"
        ).resolve()
        assert credential.parent.stat().st_mode & 0o777 == 0o700
        assert credential.read_text(encoding="utf-8") == f"{secret}\n"
        assert credential.stat().st_mode & 0o777 == 0o600

    for database_file in settings.data_dir.glob("traceforge.db*"):
        assert secret.encode() not in database_file.read_bytes()


def test_failed_provider_draft_probe_preserves_the_verified_saved_config(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_secret = "api-old-verified-secret"
    rejected_secret = "api-rejected-draft-secret"

    class ConditionalProbeProvider:
        def __init__(self, provider_settings) -> None:
            self.api_key = provider_settings.api_key

        async def complete(self, messages, tools=None) -> ModelResponse:
            assert messages and tools
            if self.api_key == rejected_secret:
                raise ProviderError(f"Rejected {rejected_secret}")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(
        "traceforge.runtime.OpenAICompatibleProvider", ConditionalProbeProvider
    )
    app = create_app(settings)

    with TestClient(app) as client:
        verified = client.post(
            "/api/provider/test",
            json={
                "model": "verified-model",
                "base_url": "https://verified.example/v1",
                "api_key": old_secret,
            },
        )
        assert verified.status_code == 200
        assert verified.json()["ok"] is True
        assert old_secret not in verified.text
        saved_before = verified.json()["provider"]
        credential_before = Path(saved_before["credential_file"])
        managed_directory = settings.data_dir / "provider-credentials"
        files_before = set(managed_directory.glob("provider-credential-*.key"))

        failed = client.post(
            "/api/provider/test",
            json={
                "model": "rejected-model",
                "base_url": "https://rejected.example/v1",
                "api_key": rejected_secret,
            },
        )

        assert failed.status_code == 200
        assert failed.json()["ok"] is False
        assert failed.json()["provider"] == saved_before
        assert rejected_secret not in failed.text
        assert client.get("/api/provider").json() == saved_before
        assert client.get("/api/status").json()["connection_verified"] is True
        assert credential_before.read_text(encoding="utf-8") == f"{old_secret}\n"
        assert set(managed_directory.glob("provider-credential-*.key")) == files_before

        saved_only = client.put(
            "/api/provider",
            json={
                "model": saved_before["model"],
                "base_url": saved_before["base_url"],
                "credential_file": saved_before["credential_file"],
            },
        )
        assert saved_only.status_code == 200
        assert saved_only.json()["connection_verified"] is False
        assert saved_only.json()["verified_at"] is None

    for database_file in settings.data_dir.glob("traceforge.db*"):
        assert rejected_secret.encode() not in database_file.read_bytes()


def test_failed_retest_of_current_provider_revokes_readiness(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    reject_probe = False

    class ConditionalProbeProvider:
        def __init__(self, _provider_settings) -> None:
            pass

        async def complete(self, messages, tools=None) -> ModelResponse:
            assert messages and tools
            if reject_probe:
                raise ProviderError("The saved provider is unavailable")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="report_connection",
                        arguments={"status": "ok"},
                    )
                ]
            )

    monkeypatch.setattr(
        "traceforge.runtime.OpenAICompatibleProvider", ConditionalProbeProvider
    )
    app = create_app(settings)
    saved_body = {
        "model": "saved-model",
        "base_url": "https://saved.example/v1",
        "credential_file": None,
        "context_window": None,
    }

    with TestClient(app) as client:
        verified = client.post("/api/provider/test", json=saved_body)
        assert verified.status_code == 200
        assert verified.json()["provider"]["connection_verified"] is True

        reject_probe = True
        failed_draft = client.post("/api/provider/test", json=saved_body)
        assert failed_draft.status_code == 200
        assert failed_draft.json()["ok"] is False
        assert failed_draft.json()["provider"]["connection_verified"] is False
        assert failed_draft.json()["provider"]["verified_at"] is None
        assert client.get("/api/status").json()["connection_verified"] is False

        reject_probe = False
        reverified = client.post("/api/provider/test", json=saved_body)
        assert reverified.json()["provider"]["connection_verified"] is True

        reject_probe = True
        failed_current = client.post("/api/provider/test")
        assert failed_current.status_code == 200
        assert failed_current.json()["ok"] is False
        assert failed_current.json()["provider"]["connection_verified"] is False
        assert failed_current.json()["provider"]["verified_at"] is None


def test_standard_runs_follow_ups_and_resumes_require_a_verified_connection(
    settings: Settings,
) -> None:
    app = create_app(settings)
    app.state.storage.create_run(
        RunRecord(
            id="unverified-terminal",
            task="Continue later",
            workspace=str(settings.workspace),
            state=RunState.SUCCEEDED,
        )
    )
    app.state.storage.create_run(
        RunRecord(
            id="unverified-interrupted",
            task="Resume later",
            workspace=str(settings.workspace),
            state=RunState.INTERRUPTED,
            interrupted_from=RunState.EXECUTING,
        )
    )

    with TestClient(app) as client:
        status_payload = client.get("/api/status").json()
        assert status_payload["api_key_configured"] is True
        assert status_payload["connection_verified"] is False

        created = client.post(
            "/api/runs",
            json={"task": "Must be gated", "create_direct_workspace": True},
        )
        followed = client.post(
            "/api/runs/unverified-terminal/turns",
            json={"prompt": "Continue"},
        )
        resumed = client.post("/api/runs/unverified-interrupted/resume")

        for response in (created, followed, resumed):
            assert response.status_code == 422
            assert "Test and verify the model connection" in response.json()["detail"]

        saved = client.put(
            "/api/provider",
            json={
                "model": settings.model,
                "base_url": settings.base_url,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["connection_verified"] is False
        assert saved.json()["verified_at"] is None


def test_fresh_failed_provider_probe_does_not_create_config_or_managed_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected_secret = "fresh-rejected-secret"

    class RejectingProbeProvider:
        def __init__(self, provider_settings) -> None:
            assert provider_settings.api_key == rejected_secret

        async def complete(self, messages, tools=None) -> ModelResponse:
            raise ProviderError(f"Rejected {rejected_secret}")

    monkeypatch.setattr(
        "traceforge.runtime.OpenAICompatibleProvider", RejectingProbeProvider
    )
    fresh = replace(settings, api_key="")
    app = create_app(fresh)

    with TestClient(app) as client:
        failed = client.post(
            "/api/provider/test",
            json={
                "model": "rejected-model",
                "base_url": "https://rejected.example/v1",
                "api_key": rejected_secret,
            },
        )

        assert failed.status_code == 200
        assert failed.json()["ok"] is False
        assert failed.json()["provider"]["model"] == settings.model
        assert failed.json()["provider"]["api_key_configured"] is False
        assert failed.json()["provider"]["connection_verified"] is False
        assert failed.json()["provider"]["verified_at"] is None
        assert rejected_secret not in failed.text
        assert list(
            (settings.data_dir / "provider-credentials").glob(
                "provider-credential-*.key"
            )
        ) == []

    with sqlite3.connect(settings.data_dir / "traceforge.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_config").fetchone() == (0,)
    for database_file in settings.data_dir.glob("traceforge.db*"):
        assert rejected_secret.encode() not in database_file.read_bytes()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.deepseek.com:bad/v1",
        "https://api.deepseek.com:99999/v1",
        "https://[api.deepseek.com/v1",
    ],
)
def test_provider_config_rejects_malformed_ports_and_hosts(
    settings: Settings, base_url: str
) -> None:
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        rejected = client.put(
            "/api/provider",
            json={"model": "deepseek-v4-pro", "base_url": base_url},
        )

        assert rejected.status_code == 422
        assert rejected.json()["detail"] == (
            "Base URL must be an absolute http:// or https:// URL"
        )


def test_legacy_malformed_provider_route_remains_safe_to_inspect(
    settings: Settings,
) -> None:
    app = create_app(settings, provider=ScriptedProvider([]))
    app.state.storage.save_provider_config(
        ProviderConfig(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com:bad/v1",
        )
    )

    with TestClient(app) as client:
        payload = client.get("/api/provider").json()

        assert payload["resolved_context_window"] == settings.context_limit
        assert payload["context_window_source"] == "fallback"
        assert payload["supported_reasoning_efforts"] == ["auto"]
        assert payload["reasoning_effort_source"] == "provider_default"


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
                            arguments={"summary": "Recovered"},
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
            json={
                "task": "Recover this run",
                "verifier_enabled": False,
                "mode": "plan",
            },
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
