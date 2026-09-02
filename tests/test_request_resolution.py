from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from traceforge.models import (
    ConversationTurn,
    ProjectCandidate,
    ProjectScope,
    ProjectTarget,
    RequestResolution,
)
from traceforge.request_resolution import (
    explicit_target_candidates,
    is_workspace_target_followup_request,
    resolve_request,
)


def _project_id(path: str) -> str:
    return "project_" + hashlib.sha256(path.encode()).hexdigest()[:16]


def _candidate(path: str, identity: str) -> ProjectCandidate:
    return ProjectCandidate(
        id=_project_id(path),
        path=path,
        label=path,
        description=f"Verified project {path}",
        markers=["README.md", "package.json"],
        identity=identity,
    )


ALPHA = _candidate("alpha", "1:2:3")
BETA = _candidate("beta", "1:3:4")
GAMMA = _candidate("gamma", "1:4:5")
ROOT = ProjectCandidate(
    id=_project_id("."),
    path=".",
    label="workspace root",
    description="Verified workspace-root project",
    markers=["pyproject.toml"],
    identity="1:1:1",
)
CANDIDATES = [ALPHA, BETA]
ALPHA_TARGET = ProjectTarget(
    path="alpha",
    label="alpha",
    markers=ALPHA.markers,
    selected_by="explicit",
    identity=ALPHA.identity,
)


@pytest.mark.parametrize(
    ("prompt", "work_kind", "status", "reference", "overview"),
    [
        # Workspace-independent conversation, capability, knowledge, and inline-code requests.
        ("你好", "conversation", "not_required", "none", False),
        ("谢谢", "conversation", "not_required", "none", False),
        ("你是谁?", "conversation", "not_required", "none", False),
        ("你能做什么?", "conversation", "not_required", "none", False),
        ("Hello!", "conversation", "not_required", "none", False),
        ("What can you do?", "conversation", "not_required", "none", False),
        ("Python 的 GIL 是什么?", "conversation", "not_required", "none", False),
        ("解释依赖注入", "conversation", "not_required", "none", False),
        ("HTTP 404 是什么意思?", "conversation", "not_required", "none", False),
        ("React 的 virtual DOM 是什么?", "conversation", "not_required", "none", False),
        ("How does TCP congestion control work?", "conversation", "not_required", "none", False),
        ("What is Rust ownership?", "conversation", "not_required", "none", False),
        ("解释这段代码: `value += 1`", "conversation", "not_required", "none", False),
        (
            "Review this snippet:\n```python\nvalue += 1\n```",
            "conversation",
            "not_required",
            "none",
            False,
        ),
        ("分析这个日志", "conversation", "not_required", "none", False),
        ("What time is it?", "conversation", "not_required", "none", False),
        # Read-only workspace families all use the same target ambiguity policy.
        ("介绍一下项目", "read", "clarification_required", "unspecified", True),
        ("项目是做什么的?", "read", "clarification_required", "unspecified", True),
        ("检查项目依赖", "read", "clarification_required", "unspecified", False),
        ("搜索代码", "read", "clarification_required", "unspecified", False),
        ("查看代码结构", "read", "clarification_required", "unspecified", False),
        ("审查配置", "read", "clarification_required", "unspecified", False),
        ("定位登录实现", "read", "clarification_required", "unspecified", False),
        ("搜索认证逻辑", "read", "clarification_required", "unspecified", False),
        ("阅读 README", "read", "clarification_required", "unspecified", False),
        ("Describe the project", "read", "clarification_required", "unspecified", True),
        ("Inspect the repository", "read", "clarification_required", "unspecified", False),
        ("Check dependencies", "read", "clarification_required", "unspecified", False),
        ("Search the code", "read", "clarification_required", "unspecified", False),
        ("Find the login implementation", "read", "clarification_required", "unspecified", False),
        ("Search for authentication flow", "read", "clarification_required", "unspecified", False),
        ("Review the configuration", "read", "clarification_required", "unspecified", False),
        ("Read README.md", "read", "clarification_required", "unspecified", False),
        ("Analyze the project architecture", "read", "clarification_required", "unspecified", True),
        # Mutation and command families use the same target policy as reads.
        ("修复登录问题", "execute", "clarification_required", "unspecified", False),
        ("运行测试", "execute", "clarification_required", "unspecified", False),
        ("构建项目", "execute", "clarification_required", "unspecified", False),
        ("部署服务", "execute", "clarification_required", "unspecified", False),
        ("安装依赖", "execute", "clarification_required", "unspecified", False),
        ("更新依赖", "execute", "clarification_required", "unspecified", False),
        ("启动项目", "execute", "clarification_required", "unspecified", False),
        ("Fix the login bug", "execute", "clarification_required", "unspecified", False),
        ("Run the tests", "execute", "clarification_required", "unspecified", False),
        ("Build the project", "execute", "clarification_required", "unspecified", False),
        ("Deploy the service", "execute", "clarification_required", "unspecified", False),
        ("Install dependencies", "execute", "clarification_required", "unspecified", False),
        ("Update dependencies", "execute", "clarification_required", "unspecified", False),
        ("Start the app", "execute", "clarification_required", "unspecified", False),
        # Explicit verified candidates resolve identically across semantic families.
        ("介绍 alpha 项目", "read", "resolved", "explicit", True),
        ("检查 alpha 项目的依赖", "read", "resolved", "explicit", False),
        ("在 alpha 项目中搜索代码", "read", "resolved", "explicit", False),
        ("修复 alpha 项目的登录问题", "execute", "resolved", "explicit", False),
        ("运行 alpha 项目的测试", "execute", "resolved", "explicit", False),
        ("Describe the alpha project", "read", "resolved", "explicit", True),
        ("Inspect the alpha repository", "read", "resolved", "explicit", False),
        ("Search the alpha codebase", "read", "resolved", "explicit", False),
        ("Fix the alpha project login bug", "execute", "resolved", "explicit", False),
        ("Build the alpha project", "execute", "resolved", "explicit", False),
    ],
)
def test_request_resolution_corpus(
    prompt: str,
    work_kind: str,
    status: str,
    reference: str,
    overview: bool,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == work_kind
    assert resolution.target_status == status
    assert resolution.target_reference == reference
    assert resolution.overview_required is overview
    assert resolution.workspace_dependent is (work_kind != "conversation")
    assert ("target" in resolution.ambiguity_dimensions) is (
        status in {"clarification_required", "unsupported"}
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "继续修复它",
        "继续检查这个项目的依赖",
        "再详细介绍一下这个项目",
        "运行它的测试",
        "Continue fixing it",
        "Check this project's dependencies",
        "Run its tests",
        "Continue describing this repository",
    ],
)
def test_reliable_adjacent_references_inherit_the_verified_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert is_workspace_target_followup_request(prompt) is True
    assert resolution.workspace_dependent is True
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"
    assert resolution.reasons == ["The request reliably refers to adjacent target alpha."]


@pytest.mark.parametrize(
    "prompt",
    [
        "在这个项目运行测试",
        "在这个项目中运行测试",
        "Run tests in this project",
        "In this project run tests",
    ],
)
def test_target_prefixed_operations_remain_executable_followups(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下项目",
        "运行测试",
        "检查依赖",
        "Describe the project",
        "Run the tests",
        "Check dependencies",
    ],
)
def test_unqualified_requests_do_not_inherit_an_adjacent_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert is_workspace_target_followup_request(prompt) is False
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


def test_explicit_target_overrides_the_adjacent_target() -> None:
    resolution = resolve_request(
        "改为检查 beta 项目的依赖",
        CANDIDATES,
        prior_target=ALPHA_TARGET,
    )

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"
    assert resolution.reasons == ["The request explicitly selects verified project beta."]


def test_one_other_project_resolves_deterministically() -> None:
    resolution = resolve_request(
        "介绍另一个项目",
        CANDIDATES,
        prior_target=ALPHA_TARGET,
    )

    assert resolution.target_reference == "other"
    assert resolution.target_status == "resolved"
    assert resolution.reasons == ["Exactly one other verified project is available: beta."]


def test_several_other_projects_require_clarification() -> None:
    resolution = resolve_request(
        "检查另一个项目的依赖",
        [ALPHA, BETA, GAMMA],
        prior_target=ALPHA_TARGET,
    )

    assert resolution.target_reference == "other"
    assert resolution.target_status == "clarification_required"
    assert resolution.ambiguity_dimensions == ["target"]


@pytest.mark.parametrize(
    "prompt",
    [
        "比较 alpha 和 beta 项目",
        "介绍所有项目",
        "Run tests in all projects",
        "Compare both repositories",
    ],
)
def test_multi_target_requests_are_explicitly_unsupported(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"
    assert resolution.ambiguity_dimensions == ["target", "scope"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix alpha or beta",
        "Fix either alpha or beta",
        "修复alpha或beta",
    ],
)
def test_explicit_alternatives_require_a_choice_instead_of_becoming_joint_scope(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, [ALPHA, BETA, GAMMA])

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"
    assert resolution.ambiguity_dimensions == ["target"]
    assert [
        candidate.path
        for candidate in explicit_target_candidates(prompt, [ALPHA, BETA, GAMMA])
    ] == ["alpha", "beta"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain alpha architecture and beta dependencies",
        "介绍 alpha 的架构和 beta 的依赖",
    ],
)
def test_parallel_project_properties_remain_a_joint_multi_target_scope(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


@pytest.mark.parametrize(
    "prompt",
    [
        "Compare this project with beta",
        "Copy a file from this project to beta",
        "比较这个项目和 beta",
        "从这个项目复制文件到 beta",
    ],
)
def test_adjacent_anaphor_plus_a_different_verified_target_is_never_inherited(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


@pytest.mark.parametrize("prompt", ["Fix beta instead of alpha", "修复 beta 而不是 alpha"])
def test_non_selected_alternative_does_not_become_a_second_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"
    assert explicit_target_candidates(prompt, CANDIDATES) == [BETA]


@pytest.mark.parametrize("prompt", ["分析整个工作区", "Inspect the entire workspace"])
def test_explicit_whole_workspace_requests_resolve_to_the_root(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    ("prompt", "work_kind"),
    [
        ("创建一个新项目", "execute"),
        ("Build a new app", "execute"),
        ("搜索代码", "read"),
        ("Read README.md", "read"),
    ],
)
def test_empty_workspace_operations_resolve_to_the_workspace_root(
    prompt: str,
    work_kind: str,
) -> None:
    resolution = resolve_request(prompt, [])

    assert resolution.work_kind == work_kind
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize("prompt", ["创建一个新项目", "Build a new app"])
def test_greenfield_operations_use_the_root_even_when_child_projects_exist(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"
    assert resolution.reasons == ["A greenfield project request targets the workspace root."]


def test_project_overview_without_a_verified_target_is_unsupported() -> None:
    resolution = resolve_request("介绍一下项目", [])

    assert resolution.work_kind == "read"
    assert resolution.overview_required is True
    assert resolution.target_status == "unsupported"
    assert resolution.ambiguity_dimensions == ["target"]


def test_root_project_and_child_projects_remain_an_explicit_ambiguity() -> None:
    resolution = resolve_request(
        "介绍一下项目",
        CANDIDATES,
        root_candidate=ROOT,
    )

    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"
    assert resolution.ambiguity_dimensions == ["target"]


def test_workspace_root_is_automatic_only_when_it_is_the_sole_project() -> None:
    resolution = resolve_request("介绍一下项目", [], root_candidate=ROOT)

    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "resolved"


def test_replaced_workspace_root_is_not_inherited() -> None:
    prior_root = ProjectTarget(
        path=".",
        label="workspace root",
        markers=ROOT.markers,
        selected_by="explicit",
        identity="9:9:9",
    )
    resolution = resolve_request(
        "继续介绍这个项目",
        CANDIDATES,
        prior_target=prior_root,
        root_candidate=ROOT,
    )

    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "unsupported"


@pytest.mark.parametrize(
    "prompt",
    [
        "项目管理是什么?",
        "如何做项目估算?",
        "介绍开源项目的许可证类型",
        "What is project management?",
        "How do open-source projects choose licenses?",
    ],
)
def test_general_project_concepts_never_bind_or_inherit_workspace_targets(
    prompt: str,
) -> None:
    first = resolve_request(prompt, CANDIDATES)
    adjacent = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    for resolution in (first, adjacent):
        assert resolution.work_kind == "conversation"
        assert resolution.workspace_dependent is False
        assert resolution.target_reference == "none"
        assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "这个项目的测试覆盖率是多少?",
        "这个项目的构建流程是什么?",
        "这个项目的更新日志写了什么?",
        "这个项目的修复方案是什么?",
        "How do I run this project's tests?",
    ],
)
def test_explanations_and_project_properties_are_not_execution_requests(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "不要修改项目\uFF0C只介绍一下",
        "Do not edit this project; only describe it.",
    ],
)
def test_negated_mutations_preserve_read_only_project_overview_semantics(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.overview_required is True
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    ["运行 `pwd`", "执行 echo hello", "Run Python", "Execute `git status`"],
)
def test_explicit_raw_commands_use_the_workspace_root(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "当前目录下有哪些项目?",
        "列出工作区的所有仓库",
        "Search TODO across the workspace",
        "List projects in the workspace",
    ],
)
def test_workspace_container_queries_bind_the_root_instead_of_one_child(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "在整个工作区的所有项目运行测试",
        "Run tests in all projects across the workspace",
    ],
)
def test_whole_workspace_wording_cannot_hide_multi_target_cardinality(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


def test_candidate_name_used_as_an_ordinary_noun_is_not_silently_selected() -> None:
    server = _candidate("server", "2:1:1")
    web = _candidate("web", "2:2:2")

    ambiguous = resolve_request("Fix server response parsing", [server, web])
    explicit = resolve_request("Fix the server project response parser", [server, web])

    assert ambiguous.target_reference == "unspecified"
    assert ambiguous.target_status == "clarification_required"
    assert explicit.target_reference == "explicit"
    assert explicit.target_status == "resolved"


def test_action_ellipsis_inherits_only_a_prior_executable_request() -> None:
    executable = RequestResolution(
        work_kind="execute",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        reasons=["Explicit prior action."],
    )
    read_only = executable.model_copy(update={"work_kind": "read"})

    continued = resolve_request(
        "继续完成任务",
        CANDIDATES,
        prior_target=ALPHA_TARGET,
        prior_resolution=executable,
    )
    not_inherited_as_execution = resolve_request(
        "继续完成任务",
        CANDIDATES,
        prior_target=ALPHA_TARGET,
        prior_resolution=read_only,
    )

    assert continued.work_kind == "execute"
    assert continued.target_reference == "inherited"
    assert not_inherited_as_execution.work_kind == "read"


@pytest.mark.parametrize(
    "prompt",
    [
        "这个仓库主要解决什么问题?",
        "该项目的核心能力是什么?",
        "How is this repo organized?",
    ],
)
def test_whole_project_paraphrases_require_root_overview_evidence(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "inherited"
    assert resolution.overview_required is True


@pytest.mark.parametrize(
    "prompt",
    [
        "解释一下项目",
        "Explain the project",
        "看看代码",
        "Show me the code",
        "为什么测试失败?",
        "Why are the tests failing?",
        "Tell me where the tests live",
        "项目里有哪些模块?",
        "代码入口在哪里?",
        "认证是在哪里实现的?",
        "Where is authentication implemented?",
        "Give me a tour of the codebase",
    ],
)
def test_compositional_local_reads_require_a_project_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.workspace_dependent is True
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain unit testing",
        "Show Python code examples",
        "Why do tests matter?",
        "Where do tests usually live?",
        "项目一般有哪些模块?",
        "Describe alpha decay",
        "Summarize beta distribution",
    ],
)
def test_parallel_general_knowledge_phrasings_remain_unscoped(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    ("candidate_name", "prompt", "work_kind", "reference"),
    [
        ("run", "Run tests", "execute", "unspecified"),
        ("fix", "Fix login bug", "execute", "unspecified"),
        ("deploy", "Deploy service", "execute", "unspecified"),
        ("test", "Test alpha", "execute", "explicit"),
        ("python", "Run python --version", "execute", "workspace"),
        ("pytest", "Run pytest", "execute", "workspace"),
        ("npm", "Run npm --version", "execute", "workspace"),
    ],
)
def test_candidate_names_cannot_change_action_or_raw_command_semantics(
    candidate_name: str,
    prompt: str,
    work_kind: str,
    reference: str,
) -> None:
    baseline = resolve_request(prompt, CANDIDATES)
    expanded = resolve_request(
        prompt,
        [_candidate(candidate_name, f"9:{len(candidate_name)}:1"), *CANDIDATES],
    )

    assert expanded.work_kind == work_kind == baseline.work_kind
    assert expanded.target_reference == reference == baseline.target_reference
    assert expanded.target_status == baseline.target_status


@pytest.mark.parametrize(
    "prompt",
    [
        "Compare alpha and beta performance",
        "Compare alpha and beta's architecture",
        "比较 alpha 和 beta 的架构",
        "对比 alpha 与 beta 的依赖",
        "介绍 alpha、beta 项目的架构",
    ],
)
def test_shared_comparison_roles_never_narrow_to_the_last_candidate(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


@pytest.mark.parametrize(
    "prompt",
    [
        "Describe the alpha project and beta interactions",
        "介绍 alpha 项目和 beta 的交互",
    ],
)
def test_component_name_collisions_do_not_become_a_second_project_target(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"
    assert resolution.reasons == ["The request explicitly selects verified project alpha."]


@pytest.mark.parametrize(
    "prompt",
    [
        "Write a haiku",
        "Create a table",
        "Generate a UUID",
        "Write an email",
        "Create a joke",
        "Fix a typo in this sentence",
        "写一首诗",
        "创建一个表格",
        "生成随机数",
        "帮我写邮件",
    ],
)
def test_non_workspace_content_outputs_do_not_open_a_project_picker(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "帮我做个todolist程序",
        "帮我做一个todo程序",
        "做个待办清单程序",
        "帮我写个todolist程序",
        "帮我生成一个todo程序",
        "帮我写个待办脚本",
        "write me a todo program",
        "create a todo program",
        "build a todo script",
    ],
)
def test_software_creation_requests_are_workspace_dependent(prompt: str) -> None:
    """A request to build a program is workspace work, never a chat-only turn.

    Regression: the mutation lexicon lacked ``做`` and the software-subject
    lexicon lacked ``程序``/``脚本``/``program``/``script``, so creation phrasing
    such as "帮我做个todolist程序" fell through to a ``conversation`` result and
    the agent refused to touch a fully accessible workspace.
    """

    resolution = resolve_request(prompt, [])

    assert resolution.work_kind in {"execute", "undetermined"}
    assert resolution.workspace_dependent is True


@pytest.mark.parametrize(
    "prompt",
    ["Make me laugh", "does that make sense", "make sure it works"],
)
def test_english_make_idioms_are_not_forced_into_execution(prompt: str) -> None:
    """English ``make`` stays polysemous: idioms are not treated as file mutations."""

    resolution = resolve_request(prompt, [])

    assert resolution.work_kind != "execute"


@pytest.mark.parametrize(
    ("prompt", "expected_path"),
    [
        ("在alpha中搜索代码", "alpha"),
        ("检查alpha的依赖", "alpha"),
        ("运行alpha的测试", "alpha"),
        ("在前端中搜索代码", "前端"),
        ("检查前端的依赖", "前端"),
        ("运行前端的测试", "前端"),
    ],
)
def test_target_roles_do_not_require_spaces_around_verified_names(
    prompt: str,
    expected_path: str,
) -> None:
    candidates = (
        CANDIDATES
        if expected_path == "alpha"
        else [_candidate("前端", "8:1:1"), _candidate("后端", "8:2:2")]
    )
    resolution = resolve_request(prompt, candidates)

    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"
    assert expected_path in resolution.reasons[0]


@pytest.mark.parametrize(
    "prompt",
    [
        "Continue the task",
        "Keep going",
        "Carry on",
        "Finish the task",
        "Complete the work",
    ],
)
def test_action_ellipsis_inherits_the_adjacent_executable_target(prompt: str) -> None:
    prior = RequestResolution(
        work_kind="execute",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        reasons=["Prior executable target."],
    )

    resolution = resolve_request(
        prompt,
        CANDIDATES,
        prior_target=ALPHA_TARGET,
        prior_resolution=prior,
    )

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Run alpha tests",
        "Execute alpha tests",
        "In alpha run tests",
    ],
)
def test_targeted_operation_grammar_resolves_before_raw_command_fallback(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Should we fix the login bug?",
        "是否应该修复登录问题?",
        "给出修复登录问题的建议",
        "建议修复登录问题",
        "Give recommendations for fixing the login bug",
        "Create a plan for fixing the login bug",
        "Draft a proposal to update dependencies",
        "Discuss whether to update dependencies",
        "Evaluate whether we should deploy the service",
    ],
)
def test_advisory_actions_are_read_requests_not_execution_requests(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Please fix the login bug",
        "Update dependencies",
        "Deploy the service",
        "请修复登录问题",
    ],
)
def test_direct_action_requests_are_not_weakened_by_advisory_grammar(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "这个功能在哪里添加的?",
        "认证逻辑是在哪里实现的?",
    ],
)
def test_existing_action_location_queries_require_workspace_evidence(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Describe this backend project",
        "Describe the current backend project",
        "介绍这个后端项目",
        "介绍当前后端项目",
    ],
)
def test_modified_deictic_project_references_inherit_only_when_adjacent(
    prompt: str,
) -> None:
    fresh = resolve_request(prompt, CANDIDATES)
    adjacent = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert fresh.target_reference == "unspecified"
    assert fresh.target_status == "clarification_required"
    assert adjacent.target_reference == "inherited"
    assert adjacent.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Describe a backend project",
        "Describe backend projects",
        "介绍后端项目",
    ],
)
def test_indefinite_external_project_descriptors_remain_general(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "conversation"
    assert resolution.target_status == "not_required"


def test_replaced_adjacent_target_is_not_inherited() -> None:
    replaced_alpha = _candidate("alpha", "9:9:9")

    resolution = resolve_request(
        "继续修复它",
        [replaced_alpha, BETA],
        prior_target=ALPHA_TARGET,
    )

    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "unsupported"
    assert resolution.ambiguity_dimensions == ["target"]


def test_project_target_allows_an_empty_marker_set_for_a_new_workspace() -> None:
    target = ProjectTarget(
        path=".",
        label="new-workspace",
        markers=[],
        selected_by="automatic",
        identity="4:5:6",
    )

    assert target.path == "."
    assert target.markers == []


def test_conversation_turn_persists_a_consistent_request_resolution_and_target() -> None:
    resolution = RequestResolution(
        work_kind="execute",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        ambiguity_dimensions=[],
        overview_required=False,
        reasons=["The target is explicit."],
    )

    turn = ConversationTurn(
        index=1,
        request="Fix alpha",
        request_resolution=resolution,
        project_target=ALPHA_TARGET,
    )

    restored = ConversationTurn.model_validate_json(turn.model_dump_json())
    assert restored.request_resolution == resolution
    assert restored.project_target == ALPHA_TARGET


def test_conversation_turn_rejects_a_resolved_request_without_its_target() -> None:
    resolution = RequestResolution(
        work_kind="read",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        ambiguity_dimensions=[],
        overview_required=False,
        reasons=["The target is explicit."],
    )

    with pytest.raises(ValidationError, match="must persist its project target"):
        ConversationTurn(index=1, request="Inspect alpha", request_resolution=resolution)


@pytest.mark.parametrize(
    "fields",
    [
        {
            "work_kind": "execute",
            "workspace_dependent": False,
            "target_reference": "none",
            "target_status": "not_required",
        },
        {
            "work_kind": "conversation",
            "workspace_dependent": True,
            "target_reference": "workspace",
            "target_status": "resolved",
        },
        {
            "work_kind": "read",
            "workspace_dependent": True,
            "target_reference": "multiple",
            "target_status": "resolved",
        },
        {
            "work_kind": "read",
            "workspace_dependent": True,
            "target_reference": "explicit",
            "target_status": "clarification_required",
            "ambiguity_dimensions": ["target"],
        },
    ],
)
def test_request_resolution_rejects_contradictory_state_combinations(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RequestResolution(**fields, reasons=["Contradictory test state."])  # type: ignore[arg-type]


def test_conversation_turn_rejects_mismatched_project_evidence_scope() -> None:
    resolution = RequestResolution(
        work_kind="read",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        overview_required=True,
        reasons=["Explicit overview target."],
    )
    mismatched_scope = ProjectScope(
        path="beta",
        label="beta",
        markers=BETA.markers,
        selected_by="explicit",
        identity=BETA.identity,
    )

    with pytest.raises(ValidationError, match="must match its resolved target"):
        ConversationTurn(
            index=1,
            request="Describe alpha",
            request_resolution=resolution,
            project_target=ALPHA_TARGET,
            project_scope=mismatched_scope,
        )


def test_conversation_turn_requires_an_evidence_scope_for_a_resolved_overview() -> None:
    resolution = RequestResolution(
        work_kind="read",
        workspace_dependent=True,
        target_reference="explicit",
        target_status="resolved",
        overview_required=True,
        reasons=["Explicit overview target."],
    )

    with pytest.raises(ValidationError, match="requires an evidence scope"):
        ConversationTurn(
            index=1,
            request="Describe alpha",
            request_resolution=resolution,
            project_target=ALPHA_TARGET,
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍同一个项目",
        "介绍同一项目",
        "介绍同个项目",
        "介绍同样的项目",
        "介绍相同的项目",
        "介绍刚才那个项目",
        "继续介绍同一个项目",
    ],
)
def test_explicit_same_project_overview_inherits_adjacent_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain alpha architecture",
        "解释alpha架构",
        "Show alpha code",
        "Check alpha dependencies",
        "What database does alpha use?",
    ],
)
def test_low_collision_read_shorthand_resolves_verified_candidate(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    ("candidate", "prompt"),
    [
        (_candidate("python", "2:1:1"), "Explain Python architecture"),
        (_candidate("react", "2:1:2"), "Show React code"),
        (_candidate("app", "2:1:3"), "Check app dependencies"),
    ],
)
def test_collision_candidate_read_shorthand_remains_conservative(
    candidate: ProjectCandidate,
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, [candidate, *CANDIDATES])

    assert resolution.target_reference != "explicit"


@pytest.mark.parametrize(
    "prompt",
    [
        "Add feature X to alpha",
        "Add login support to alpha",
        "Apply patch to alpha",
        "Deploy to alpha",
        "Write code to alpha",
    ],
)
def test_generic_destination_slot_resolves_execution_target(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "explicit"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix alpha and update beta",
        "Fix alpha then update beta",
        "Fix alpha before update beta",
        "Fix alpha after update beta",
        "Fix alpha plus update beta",
        "Fix alpha and also update beta",
        "Fix alpha as well as update beta",
        "修复alpha并更新beta",
        "修复alpha然后更新beta",
        "修复alpha再更新beta",
        "修复alpha同时更新beta",
        "修复alpha\uff0c然后更新beta",
        "检查alpha后再修复beta",
        "Inspect alpha and review beta",
        "Inspect alpha before review beta",
        "Inspect alpha while review beta",
        "Inspect alpha before reviewing beta",
        "Inspect alpha while reviewing beta",
        "Fix alpha before updating beta",
    ],
)
def test_cross_predicate_target_union_is_never_silently_narrowed(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix alpha or update beta",
        "Inspect alpha or fix beta",
        "Read alpha or update beta",
        "检查alpha或修复beta",
        "Inspect alpha or review beta",
        "检查alpha或查看beta",
        "要么修复alpha\uFF0C要么更新beta",
    ],
)
def test_cross_predicate_alternatives_require_target_clarification(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"
    assert resolution.ambiguity_dimensions == ["target"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Inspect this project and alpha",
        "Compare this project with alpha",
        "Copy from this project to alpha",
        "检查这个项目和alpha",
        "Compare alpha with it",
        "比较alpha与它",
    ],
)
def test_adjacent_and_same_explicit_target_are_identity_deduplicated(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


def test_adjacent_and_different_explicit_target_remain_multiple() -> None:
    resolution = resolve_request(
        "Inspect this project and beta", CANDIDATES, prior_target=ALPHA_TARGET
    )

    assert resolution.target_reference == "multiple"
    assert resolution.target_status == "unsupported"


def test_chinese_source_to_adjacent_destination_is_an_execution_request() -> None:
    resolution = resolve_request(
        "从alpha复制到这个项目", CANDIDATES, prior_target=ALPHA_TARGET
    )

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "inherited"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "I need an explanation of dependency injection",
        "I need to understand database indexing",
        "I want to learn about unit testing",
        "I need advice on fixing login",
        "I need to know how authentication works",
        "我需要解释依赖注入",
        "我想学习单元测试",
        "我需要登录修复建议",
        "理解事务隔离原理",
    ],
)
def test_epistemic_complements_remain_conversation(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False


@pytest.mark.parametrize(
    "prompt",
    ["I need dark mode support", "我需要支持深色模式"],
)
def test_result_state_need_remains_an_undetermined_workspace_task(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "undetermined"
    assert resolution.workspace_dependent is True


@pytest.mark.parametrize(
    "prompt",
    [
        "What does alpha do?",
        "How does alpha work?",
        "alpha如何工作",
        "alpha能做什么",
    ],
)
def test_bare_semantic_candidate_subject_requires_project_clarification(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Get login working",
        "Please get authentication to work",
        "Have the API accept UUIDs",
        "Let the service handle empty input",
        "Keep tests passing",
        "The login should work",
        "The API must accept UUIDs",
        "Tests need to pass",
        "The service has to handle empty input",
        "The API ought to reject invalid requests",
        "把登录弄好",
        "请把接口改好",
        "登录应该正常工作",
        "接口必须接受 UUID",
        "测试需要通过",
    ],
)
def test_structured_result_state_frames_are_undetermined_workspace_tasks(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "undetermined"
    assert resolution.workspace_dependent is True
    assert resolution.target_reference == "unspecified"


@pytest.mark.parametrize(
    "prompt",
    [
        "How do I get login working?",
        "Should the login work?",
        "Why must the API accept UUIDs?",
        "I need advice on getting login working",
        "What does get login working mean?",
        "Login should work in general",
        "登录应该正常工作吗?",
        "为什么接口必须接受 UUID?",
        "我需要登录工作原理的解释",
    ],
)
def test_result_state_questions_and_epistemic_frames_remain_conversation(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
