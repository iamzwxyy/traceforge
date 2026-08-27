from __future__ import annotations

from traceforge.models import ToolCall
from traceforge.provider import ModelResponse, ScriptedProvider

DEMO_TASK = (
    "Fix the multi-tenant cache isolation bug without changing TTL or cache-hit behavior. "
    "Add a regression test and prove the full test suite passes."
)

_FIX_PATCH = """\
--- a/src/tenant_cache_api/cache.py
+++ b/src/tenant_cache_api/cache.py
@@ -20 +20 @@
-        self._entries: dict[str, CacheEntry[T]] = {}
+        self._entries: dict[tuple[str, str], CacheEntry[T]] = {}
@@ -30,8 +30,8 @@
         \"\"\"Return a fresh cached profile or load it for the requesting tenant.\"\"\"
-        del tenant_id  # The cache currently forgets to include tenant scope.
+        cache_key = (tenant_id, profile_id)
         now = self._clock()
-        entry = self._entries.get(profile_id)
+        entry = self._entries.get(cache_key)
         if entry is not None and entry.expires_at > now:
             return entry.value
         value = loader()
-        self._entries[profile_id] = CacheEntry(value=value, expires_at=now + ttl_seconds)
+        self._entries[cache_key] = CacheEntry(value=value, expires_at=now + ttl_seconds)
"""

_REGRESSION_TEST = """\
from tenant_cache_api.cache import TenantTTLCache


def test_same_profile_id_is_isolated_between_tenants() -> None:
    cache: TenantTTLCache[str] = TenantTTLCache(clock=lambda: 10)

    assert cache.get_or_load("acme", "42", lambda: "Ada @ Acme") == "Ada @ Acme"
    assert cache.get_or_load("globex", "42", lambda: "Grace @ Globex") == "Grace @ Globex"
"""


def scripted_demo_provider() -> ScriptedProvider:
    """Return the one-run provider used by the zero-credential product demo."""
    check = ["python", "-m", "pytest", "-q"]
    return ScriptedProvider(
        [
            ModelResponse(
                content="I'll first map the small service and its current test boundary.",
                tool_calls=[
                    ToolCall(
                        id="demo-list",
                        name="list_files",
                        arguments={"path": ".", "max_depth": 4},
                    )
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-read-brief",
                        name="read_file",
                        arguments={"path": "README.md"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-question",
                        name="ask_questions",
                        arguments={
                            "questions": [
                                {
                                    "id": "compatibility",
                                    "prompt": "How much API compatibility should the fix preserve?",
                                    "options": [
                                        {
                                            "id": "preserve",
                                            "label": "Preserve public API",
                                            "description": (
                                                "Keep the current call signature and isolate only "
                                                "the internal cache key."
                                            ),
                                            "recommended": True,
                                        },
                                        {
                                            "id": "redesign",
                                            "label": "Redesign cache API",
                                            "description": (
                                                "Introduce a new tenant-scoped key type and "
                                                "migrate callers."
                                            ),
                                        },
                                    ],
                                }
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-plan",
                        name="submit_plan",
                        arguments={
                            "summary": (
                                "Scope cache entries by tenant and profile while preserving the "
                                "public API and TTL behavior."
                            ),
                            "steps": [
                                {
                                    "id": "inspect",
                                    "title": "Inspect the cache implementation",
                                    "description": "Confirm the faulty key and existing semantics.",
                                },
                                {
                                    "id": "fix",
                                    "title": "Use a tenant-scoped cache key",
                                    "description": (
                                        "Keep lookup, expiry, and loader behavior unchanged."
                                    ),
                                },
                                {
                                    "id": "regression",
                                    "title": "Add cross-tenant regression coverage",
                                    "description": "Prove equal profile IDs cannot share values.",
                                },
                            ],
                            "acceptance_checks": [
                                {
                                    "id": "pytest",
                                    "label": "All cache and API tests pass",
                                    "command": check,
                                }
                            ],
                            "risks": [
                                "Changing the key must not reset the TTL or defeat "
                                "same-tenant hits."
                            ],
                        },
                    )
                ]
            ),
            ModelResponse(
                content="The approved plan keeps the change deliberately narrow.",
                tool_calls=[
                    ToolCall(
                        id="demo-read-cache",
                        name="read_file",
                        arguments={"path": "src/tenant_cache_api/cache.py"},
                    ),
                    ToolCall(
                        id="demo-progress-inspect",
                        name="update_plan",
                        arguments={
                            "updates": [{"id": "inspect", "status": "completed"}]
                        },
                    ),
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-fix",
                        name="apply_patch",
                        arguments={"patch": _FIX_PATCH},
                    ),
                    ToolCall(
                        id="demo-progress-fix",
                        name="update_plan",
                        arguments={"updates": [{"id": "fix", "status": "completed"}]},
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-test",
                        name="create_file",
                        arguments={
                            "path": "tests/test_tenant_isolation.py",
                            "content": _REGRESSION_TEST,
                        },
                    ),
                    ToolCall(
                        id="demo-progress-regression",
                        name="update_plan",
                        arguments={
                            "updates": [{"id": "regression", "status": "completed"}]
                        },
                    ),
                ]
            ),
            ModelResponse(
                content=(
                    "The implementation and regression are in place; now I'll collect "
                    "fresh evidence."
                ),
                tool_calls=[
                    ToolCall(
                        id="demo-pytest",
                        name="run_command",
                        arguments={"argv": check},
                    )
                ],
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-finish",
                        name="finish",
                        arguments={
                            "summary": (
                                "Scoped cache entries by (tenant_id, profile_id) and added a "
                                "cross-tenant regression test."
                            ),
                            "evidence": [
                                "python -m pytest -q passed",
                                "existing hit and TTL tests remained green",
                            ],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="demo-verdict",
                        name="submit_verification",
                        arguments={
                            "verdict": "pass",
                            "summary": (
                                "The diff scopes every cache read and write by tenant plus "
                                "profile. "
                                "Fresh tests cover isolation, cache hits, expiry, and the HTTP API."
                            ),
                            "findings": [],
                        },
                    )
                ]
            ),
        ],
        delay_seconds=0.45,
    )
