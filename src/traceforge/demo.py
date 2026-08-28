# ruff: noqa: RUF001  # Chinese demo copy intentionally uses Chinese punctuation.

from __future__ import annotations

from traceforge.models import ToolCall
from traceforge.provider import ModelResponse, ScriptedProvider

DEMO_TASK = (
    "修复多租户缓存隔离问题，不能改变 TTL 或缓存命中行为。"
    "补充回归测试，并证明完整测试套件通过。"
)

_FIX_PATCH = """\
--- a/src/tenant_cache_api/cache.py
+++ b/src/tenant_cache_api/cache.py
@@ -17 +17 @@
-        self._entries: dict[str, CacheEntry[T]] = {}
+        self._entries: dict[tuple[str, str], CacheEntry[T]] = {}
@@ -27,8 +27,8 @@
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
    """Return the repeatable provider used by the zero-credential product demo."""
    check = ["python", "-m", "pytest", "-q"]
    return ScriptedProvider(
        [
            ModelResponse(
                content="我会先梳理这个小型服务及其现有测试边界。",
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
                                    "prompt": "这次修复需要保留到什么程度的 API 兼容性？",
                                    "options": [
                                        {
                                            "id": "preserve",
                                            "label": "保留公共 API",
                                            "description": (
                                                "保持现有调用签名，只隔离内部缓存键。"
                                            ),
                                            "recommended": True,
                                        },
                                        {
                                            "id": "redesign",
                                            "label": "重新设计缓存 API",
                                            "description": (
                                                "引入新的租户级键类型，并迁移调用方。"
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
                                "按租户和资料 ID 隔离缓存项，同时保留公共 API 和 TTL 行为。"
                            ),
                            "steps": [
                                {
                                    "id": "inspect",
                                    "title": "检查缓存实现",
                                    "description": "确认错误的缓存键和现有语义。",
                                },
                                {
                                    "id": "fix",
                                    "title": "使用租户级缓存键",
                                    "description": (
                                        "保持查找、过期和加载行为不变。"
                                    ),
                                },
                                {
                                    "id": "regression",
                                    "title": "补充跨租户回归测试",
                                    "description": "证明相同资料 ID 不会跨租户共享值。",
                                },
                            ],
                            "acceptance_checks": [
                                {
                                    "id": "pytest",
                                    "label": "所有缓存和 API 测试通过",
                                    "command": check,
                                }
                            ],
                            "impacted_files": [
                                "src/tenant_cache_api/cache.py",
                                "tests/test_tenant_isolation.py",
                            ],
                            "risks": [
                                "修改缓存键不能重置 TTL，也不能破坏同一租户内的缓存命中。"
                            ],
                        },
                    )
                ]
            ),
            ModelResponse(
                content="已批准的计划会刻意保持较小的变更范围。",
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
                    "实现和回归测试已经就位；接下来收集新鲜证据。"
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
                                "已按 (tenant_id, profile_id) 隔离缓存项，并补充跨租户回归测试。"
                            ),
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
                                "差异中的每次缓存读写都同时按租户和资料 ID 隔离。"
                                "新鲜测试覆盖隔离、缓存命中、过期和 HTTP API。"
                            ),
                            "findings": [],
                        },
                    )
                ]
            ),
        ],
        delay_seconds=0.45,
        repeat=True,
        streaming=True,
    )
