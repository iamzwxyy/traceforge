from __future__ import annotations

import json
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
    ClarificationQuestion,
    ClarificationRequest,
    ConversationTurn,
    DecisionKind,
    EventType,
    ProviderConfig,
    QuestionOption,
    RunRecord,
    RunState,
    ToolCall,
    WorkspaceInstructionSnapshot,
)
from traceforge.provider import ModelResponse, ProviderError, ScriptedProvider
from traceforge.storage import Storage


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


def test_api_exposes_only_workspace_rule_provenance(
    settings: Settings,
) -> None:
    canary = "private-workspace-rule-canary-9813"
    (settings.workspace / "AGENTS.md").write_text(canary)
    app = create_app(
        settings,
        provider=ScriptedProvider(
            [
                _plan_response(),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"summary": "Reviewed"},
                        )
                    ]
                ),
            ]
        ),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "task": "Review",
                "workspace": str(settings.workspace),
                "verifier_enabled": False,
            },
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        _wait_for_state(client, run_id, "succeeded")

        events = client.get(f"/api/runs/{run_id}/events").json()
        websocket_events = []
        with client.websocket_connect(
            f"/api/runs/{run_id}/events?after_seq=0"
        ) as websocket:
            for _ in events:
                websocket_events.append(websocket.receive_json())
        proof_response = client.get(f"/api/runs/{run_id}/proof-pack")
        proof_markdown = client.get(f"/api/runs/{run_id}/proof-pack.md")
        assert proof_response.status_code == 200
        assert proof_markdown.status_code == 200

        public_payload = {
            "run": client.get(f"/api/runs/{run_id}").json(),
            "runs": client.get("/api/runs").json(),
            "events": events,
            "websocket_events": websocket_events,
            "proof": proof_response.json(),
            "proof_markdown": proof_markdown.text,
        }
        rendered = json.dumps(public_payload)
        assert canary not in rendered
        resolved = [
            event
            for event in public_payload["events"]
            if event["type"] == "workspace.instructions.resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0]["payload"]["sources"][0]["path"] == "AGENTS.md"
        assert resolved[0]["payload"]["content_private"] is True
        assert app.state.storage.get_workspace_instruction_snapshot(
            run_id, 1
        ).sources[0].content == canary


def test_api_rejects_unsafe_workspace_rules_before_creating_a_run(
    settings: Settings,
) -> None:
    (settings.workspace / "AGENTS.md").write_text("do not store sk-abcdefghijkl")
    app = create_app(settings, provider=ScriptedProvider([]))

    with TestClient(app) as client:
        rejected = client.post(
            "/api/runs",
            json={"task": "Review", "workspace": str(settings.workspace)},
        )

        assert rejected.status_code == 422
        assert "credential-like" in rejected.json()["detail"]
        assert client.get("/api/runs").json() == []


def test_api_rejects_duplicate_answers_for_one_clarification_question(
    settings: Settings,
) -> None:
    question = ModelResponse(
        tool_calls=[
            ToolCall(
                id="question",
                name="ask_questions",
                arguments={
                    "questions": [
                        {
                            "id": "scope",
                            "prompt": "Which scope?",
                            "options": [
                                {"id": "small", "label": "Small"},
                                {"id": "large", "label": "Large"},
                            ],
                        }
                    ]
                },
            )
        ]
    )
    app = create_app(
        settings,
        provider=ScriptedProvider([question, _answer_response("Scope recorded")]),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Clarify scope", "create_direct_workspace": True},
        )
        run_id = created.json()["id"]
        waiting = _wait_for_state(client, run_id, "awaiting_clarification")
        request_id = waiting["decision_request_id"]

        duplicate = client.post(
            f"/api/runs/{run_id}/answers",
            json={
                "request_id": request_id,
                "answers": [
                    {"question_id": "scope", "option_id": "small"},
                    {"question_id": "scope", "option_id": "large"},
                ],
            },
        )

        assert duplicate.status_code == 409
        assert "exactly once" in duplicate.json()["detail"]
        still_waiting = client.get(f"/api/runs/{run_id}").json()
        assert still_waiting["state"] == "awaiting_clarification"
        assert still_waiting["decision_request_id"] == request_id
        accepted = client.post(
            f"/api/runs/{run_id}/answers",
            json={
                "request_id": request_id,
                "answers": [{"question_id": "scope", "option_id": "small"}],
            },
        )
        assert accepted.status_code == 202
        assert _wait_for_state(client, run_id, "answered")["decision_request_id"] is None


def test_api_rollback_hides_and_expires_an_interrupted_decision(
    settings: Settings,
) -> None:
    app = create_app(settings, provider=ScriptedProvider([]))
    clarification = ClarificationRequest(
        questions=[
            ClarificationQuestion(
                id="scope",
                prompt="Which scope?",
                options=[
                    QuestionOption(id="small", label="Small"),
                    QuestionOption(id="large", label="Large"),
                ],
            )
        ]
    )
    run = RunRecord(
        id="rollback-interrupted-decision",
        task="Rollback the old question",
        workspace=str(settings.workspace),
        state=RunState.PLANNING,
        clarification=clarification,
    )
    app.state.storage.create_run(run)
    run.state = RunState.AWAITING_CLARIFICATION
    request_id = "question-before-rollback"
    app.state.storage.open_decision(
        run,
        previous_state=RunState.PLANNING,
        request_id=request_id,
        kind=DecisionKind.CLARIFICATION,
        turn_index=1,
        subject=clarification.model_dump(mode="json"),
        requested_event_type=EventType.CLARIFICATION_REQUESTED,
        requested_payload=clarification.model_dump(mode="json"),
    )
    interrupted = app.state.storage.get_run(run.id)
    interrupted.state = RunState.INTERRUPTED
    interrupted.interrupted_from = RunState.AWAITING_CLARIFICATION
    app.state.storage.save_run(interrupted)

    with TestClient(app) as client:
        rolled_back = client.post(f"/api/runs/{run.id}/rollback")
        assert rolled_back.status_code == 200
        public = client.get(f"/api/runs/{run.id}").json()
        assert public["state"] == "rolled_back"
        assert public["clarification"] is None
        assert public["decision_request_id"] is None
        assert public["decision_kind"] is None
        stale = client.post(
            f"/api/runs/{run.id}/answers",
            json={
                "request_id": request_id,
                "answers": [{"question_id": "scope", "option_id": "small"}],
            },
        )
        assert stale.status_code == 409
        assert "abandoned" in stale.json()["detail"]


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
        assert waiting["decision_kind"] == "plan"
        assert waiting["decision_request_id"]

        missing_identity = client.post(
            f"/api/runs/{run_id}/plan-decision", json={"decision": "approve"}
        )
        assert missing_identity.status_code == 422

        plan_request = {
            "request_id": waiting["decision_request_id"],
            "decision": "approve",
        }
        decision = client.post(
            f"/api/runs/{run_id}/plan-decision",
            json=plan_request,
        )
        assert decision.status_code == 202
        assert client.post(
            f"/api/runs/{run_id}/plan-decision", json=plan_request
        ).status_code == 202
        conflicting_retry = client.post(
            f"/api/runs/{run_id}/plan-decision",
            json={
                "request_id": waiting["decision_request_id"],
                "decision": "revise",
                "feedback": "Different response",
            },
        )
        assert conflicting_retry.status_code == 409
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


def test_api_follow_up_after_rollback_creates_a_linked_successor(
    settings: Settings,
) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="submit_plan",
                        arguments={
                            "summary": "Create one note",
                            "steps": [{"id": "create", "title": "Create note.txt"}],
                            "acceptance_checks": [
                                {"id": "review", "label": "Review note.txt"}
                            ],
                            "impacted_files": ["note.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="create",
                        name="create_file",
                        arguments={"path": "note.txt", "content": "agent\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish",
                        arguments={"summary": "Created note.txt"},
                    )
                ]
            ),
            _answer_response("The rolled-back work is available as history."),
        ]
    )
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "Create note.txt", "verifier_enabled": False},
        )
        first_id = created.json()["id"]
        _wait_for_state(client, first_id, "succeeded")
        rolled_back = client.post(f"/api/runs/{first_id}/rollback")
        assert rolled_back.status_code == 200

        continued = client.post(
            f"/api/runs/{first_id}/turns",
            json={"prompt": "Explain what was rolled back"},
        )

        assert continued.status_code == 200
        successor = continued.json()
        assert successor["id"] != first_id
        assert successor["parent_run_id"] == first_id
        assert successor["task"] == "Explain what was rolled back"
        assert client.get(f"/api/runs/{first_id}").json()["state"] == "rolled_back"
        completed = _wait_for_state(client, successor["id"], "answered")
        assert completed["parent_run_id"] == first_id
        parent = client.get(f"/api/runs/{first_id}").json()
        assert parent["successor_run_id"] == successor["id"]

        exact_retry = client.post(
            f"/api/runs/{first_id}/turns",
            json={"prompt": "Explain what was rolled back"},
        )
        assert exact_retry.status_code == 200
        assert exact_retry.json()["id"] == successor["id"]
        conflicting_retry = client.post(
            f"/api/runs/{first_id}/turns",
            json={"prompt": "Start a different continuation"},
        )
        assert conflicting_retry.status_code == 409
        assert "already continued" in conflicting_retry.json()["detail"]


def test_api_keeps_each_success_proof_immutable_across_later_turns(
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
                        arguments={"summary": "First success"},
                    )
                ]
            ),
            _plan_response(),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="finish-2",
                        name="finish",
                        arguments={"summary": "Second success"},
                    )
                ]
            ),
            _answer_response("Both successful turns remain available."),
        ]
    )
    app = create_app(settings, provider=provider)

    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"task": "First request", "verifier_enabled": False},
        )
        run_id = created.json()["id"]
        first_view = _wait_for_state(client, run_id, "succeeded")
        assert first_view["proof_turn_indexes"] == [1]
        first = client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 1}
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        first_json = first.json()
        assert first_json["schema_version"] == "traceforge.proof-pack.v2"
        assert first_json["run_id"] == run_id
        assert first_json["turn_index"] == 1
        assert first_json["scope"] == "cumulative_through_turn"
        assert first_json["event_through_seq"] == first_json["event_count"]
        assert len(first_json["artifact_sha256"]) == 64
        assert len(first_json["turns"]) == 1

        followed = client.post(
            f"/api/runs/{run_id}/turns",
            json={"prompt": "Second request"},
        )
        assert followed.status_code == 200
        second_view = _wait_for_state(client, run_id, "succeeded")
        assert second_view["proof_turn_indexes"] == [1, 2]
        second_json = client.get(f"/api/runs/{run_id}/proof-pack").json()

        assert len(second_json["turns"]) == 2
        assert second_json == client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 2}
        ).json()
        assert first_json == client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 1}
        ).json()
        assert first_json["event_count"] < second_json["event_count"]
        assert first_json["evidence_sha256"] != second_json["evidence_sha256"]

        answered = client.post(
            f"/api/runs/{run_id}/turns",
            json={"prompt": "Were both requests completed?"},
        )
        assert answered.status_code == 200
        answer_view = _wait_for_state(client, run_id, "answered")
        assert answer_view["proof_turn_indexes"] == [1, 2]

        assert client.get(f"/api/runs/{run_id}/proof-pack").json() == second_json
        assert client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 1}
        ).json() == first_json
        markdown = client.get(
            f"/api/runs/{run_id}/proof-pack.md", params={"turn_index": 1}
        )
        assert markdown.status_code == 200
        assert markdown.headers["cache-control"] == "no-store"
        assert "turn-1-proof-pack.md" in markdown.headers["content-disposition"]
        assert "First success" in markdown.text
        assert "Second success" not in markdown.text
        no_answer_proof = client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 3}
        )
        missing = client.get(
            f"/api/runs/{run_id}/proof-pack", params={"turn_index": 99}
        )
        assert no_answer_proof.status_code == 409
        assert missing.status_code == 404


def test_api_backfills_only_a_legacy_current_success(settings: Settings) -> None:
    database = settings.data_dir / "traceforge.db"
    legacy = Storage(database)
    current = RunRecord(
        id="legacy-current-success",
        task="Legacy current success",
        workspace=str(settings.workspace),
        state=RunState.SUCCEEDED,
        turns=[],
    )
    historical = RunRecord(
        id="legacy-historical-success",
        task="Legacy historical success",
        workspace=str(settings.workspace),
        state=RunState.ANSWERED,
        turns=[
            ConversationTurn(
                index=1,
                request="Legacy implementation",
                outcome="succeeded",
                summary="Old success",
            ),
            ConversationTurn(
                index=2,
                request="Legacy question",
                outcome="answered",
                summary="Old answer",
            ),
        ],
    )
    legacy.create_run(current)
    legacy.create_run(historical)
    legacy.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE proof_backfill_candidates")
        connection.execute("DROP TABLE proof_packs")
        connection.execute("DROP TABLE schema_migrations")

    app = create_app(replace(settings, api_key=""))

    with TestClient(app) as client:
        listed = client.get("/api/runs")
        assert listed.status_code == 200
        listed_by_id = {item["id"]: item for item in listed.json()}
        assert listed_by_id["legacy-current-success"]["proof_turn_indexes"] == [1]
        assert listed_by_id["legacy-historical-success"]["proof_turn_indexes"] == []

        backfilled = client.get("/api/runs/legacy-current-success/proof-pack")
        unavailable = client.get(
            "/api/runs/legacy-historical-success/proof-pack",
            params={"turn_index": 1},
        )

        assert backfilled.status_code == 200
        assert backfilled.json()["turn_index"] == 1
        assert backfilled.json()["turns"] == []
        assert app.state.storage.get_proof_pack("legacy-current-success", 1) is not None
        assert unavailable.status_code == 409
        assert "cannot be reconstructed" in unavailable.json()["detail"]
        assert app.state.storage.get_proof_pack("legacy-historical-success") is None


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


def test_rest_and_websocket_redact_legacy_credential_payloads(
    settings: Settings,
) -> None:
    configured = 'owner"secret\\tail\tsegment'
    protected = replace(settings, api_key=configured)
    app = create_app(protected, provider=ScriptedProvider([]))
    run = RunRecord(
        id="legacy-secret-output",
        task="Inspect safely",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
        interrupted_from=RunState.AWAITING_ACTION_APPROVAL,
    )
    app.state.storage.create_run(run)
    approval = {
        "id": "legacy-approval",
        "tool_call": {
            "id": "legacy-call",
            "name": "run_command",
            "arguments": {"argv": ["python", "-c", configured]},
        },
        "summary": "Legacy action",
        "reason": "Approval required",
        "risk": "elevated",
        "approval_mode": "automatic",
        "policy_decision": "ask",
        "sandbox_bypass_on_approve": False,
    }
    event_payload = {
        "id": "legacy-call",
        "name": "run_command",
        "arguments": {"argv": ["python", "-c", configured]},
    }
    with sqlite3.connect(settings.data_dir / "traceforge.db") as legacy_writer:
        legacy_writer.execute(
            "UPDATE runs SET pending_approval_json = ? WHERE id = ?",
            (json.dumps(approval, ensure_ascii=False), run.id),
        )
        legacy_writer.execute(
            """
            INSERT INTO events(run_id, seq, type, payload_json, created_at)
            VALUES (?, 1, ?, ?, '2026-08-28T00:00:00+00:00')
            """,
            (
                run.id,
                EventType.TOOL_REQUESTED.value,
                json.dumps(event_payload, ensure_ascii=False),
            ),
        )

    escaped = json.dumps(configured, ensure_ascii=False)[1:-1].encode()
    with TestClient(app) as client:
        run_response = client.get(f"/api/runs/{run.id}")
        event_response = client.get(f"/api/runs/{run.id}/events")
        for response in (run_response, event_response):
            assert response.status_code == 200
            assert configured.encode() not in response.content
            assert escaped not in response.content
        assert event_response.json()[0]["payload"]["arguments"]["argv"][-1] == "█" * 10

        with client.websocket_connect(f"/api/runs/{run.id}/events") as websocket:
            raw = websocket.receive_text().encode()
            assert configured.encode() not in raw
            assert escaped not in raw


def test_rest_and_websocket_json_do_not_synthesize_a_configured_key(
    settings: Settings,
) -> None:
    configured = 'foo", "start_line": 1'
    protected = replace(settings, api_key=configured)
    app = create_app(protected, provider=ScriptedProvider([]))
    run = RunRecord(
        id="structural-output",
        task="Inspect safely",
        workspace=str(settings.workspace),
        state=RunState.INTERRUPTED,
    )
    app.state.storage.create_run(run)
    app.state.storage.append_event(
        run.id,
        EventType.TOOL_REQUESTED,
        {"path": "foo", "start_line": 1},
    )

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run.id}/events")
        assert response.status_code == 200
        assert configured.encode() not in response.content
        assert response.json()[0]["payload"] == {"path": "foo", "start_line": 1}
        with client.websocket_connect(f"/api/runs/{run.id}/events") as websocket:
            raw = websocket.receive_text().encode()
            assert configured.encode() not in raw
            assert json.loads(raw)["payload"] == {"path": "foo", "start_line": 1}


@pytest.mark.parametrize(
    ("configured", "path", "expected_status"),
    [
        ('openapi":"3.1.0","info', "/api/openapi.json", 200),
        ('detail":"Not Found', "/api/missing-route", 404),
    ],
)
def test_framework_generated_rest_json_uses_the_public_credential_boundary(
    settings: Settings,
    configured: str,
    path: str,
    expected_status: int,
) -> None:
    app = create_app(
        replace(settings, api_key=configured),
        provider=ScriptedProvider([]),
    )

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == expected_status
    assert configured.encode() not in response.content
    assert isinstance(response.json(), dict)


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
        app.state.storage.create_run(
            run,
            instruction_snapshot=WorkspaceInstructionSnapshot.empty(),
        )
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
        assert payload["environment_credential_configured"] is True
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
        waiting = _wait_for_state(client, run_id, "awaiting_plan_approval")
        approved = client.post(
            f"/api/runs/{run_id}/plan-decision",
            json={
                "request_id": waiting["decision_request_id"],
                "decision": "approve",
            },
        )
        assert approved.status_code == 202
        completed = _wait_for_state(client, run_id, "succeeded")
        assert completed["error"] is None

        events = client.get(f"/api/runs/{run_id}/events").json()
        assert [event["type"] for event in events].count("model.retry") == 2
        assert any(event["type"] == "run.resumed" for event in events)
