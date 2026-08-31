from __future__ import annotations

import hashlib

import pytest

from traceforge.models import ProjectCandidate, ProjectTarget
from traceforge.request_resolution import resolve_request


def _candidate(path: str, identity: str) -> ProjectCandidate:
    return ProjectCandidate(
        id="project_" + hashlib.sha256(path.encode()).hexdigest()[:16],
        path=path,
        label=path,
        description=f"Verified project {path}",
        markers=["README.md", "package.json"],
        identity=identity,
    )


ALPHA = _candidate("alpha", "1:2:3")
BETA = _candidate("beta", "1:3:4")
CANDIDATES = [ALPHA, BETA]
ALPHA_TARGET = ProjectTarget(
    path="alpha",
    label="alpha",
    markers=ALPHA.markers,
    selected_by="explicit",
    identity=ALPHA.identity,
)


@pytest.mark.parametrize(
    ("prompt", "work_kind"),
    [
        ("Inspect architecture.txt", "read"),
        ("只分析 architecture.txt, 不要修改", "read"),
        ("Create note.txt", "execute"),
        ("Write README.md", "execute"),
        ("更新 src/config.yaml", "execute"),
    ],
)
def test_safe_relative_file_objects_require_workspace_scope(
    prompt: str,
    work_kind: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == work_kind
    assert resolution.workspace_dependent is True
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Write an email to dev.py@example.com",
        "Explain https://example.com/example.py",
        "Review this snippet:\n```python\npath = 'architecture.txt'\n```",
        "Explain this snippet: `alpha/config`",
        "Review this snippet:\n```sh\ncd alpha/\n```",
        "Review this code: `in alpha`",
    ],
)
def test_file_like_text_outside_a_local_file_role_stays_unscoped(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Inspect this workspace",
        "Inspect the local workspace",
        "Inspect the current directory",
        "Analyze the workspace root",
        "Show files in this directory",
        "Read ./README.md",
        "Inspect ./pyproject.toml",
        "分析这个目录",
        "查看当前目录的README",
    ],
)
def test_explicit_workspace_and_directory_references_bind_the_root(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize("prompt", ["Fix ./README.md", "更新 ./config.yaml"])
def test_root_relative_file_mutations_bind_the_workspace_root(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "workspace"
    assert resolution.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "What does it do?",
        "How is it organized?",
        "How does it work?",
        "Does it have tests?",
        "On the same project, inspect config",
        "Run tests in the same project",
        "Run tests there",
        "Same one",
        "Continue with it",
        "Keep working on it",
        "它是做什么的?",
        "它是如何工作的?",
        "它有哪些模块?",
        "它的目录结构?",
        "还是同一个项目, 检查配置",
        "在刚才那个项目运行测试",
        "就这个",
        "继续吧",
    ],
)
def test_strong_adjacent_pronouns_inherit_only_a_current_verified_target(
    prompt: str,
) -> None:
    fresh = resolve_request(prompt, CANDIDATES)
    adjacent = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert fresh.target_reference != "inherited"
    assert adjacent.target_reference == "inherited"
    assert adjacent.target_status == "resolved"


@pytest.mark.parametrize(
    "prompt",
    [
        "Write a project summary",
        "Generate a report about the project",
        "Create a table for the repository",
        "写一份项目总结",
        "生成关于项目的报告",
    ],
)
def test_response_artifacts_read_the_project_without_authorizing_mutation(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Write the project summary to summary.md",
        "Create a project report file",
        "把项目总结写入 summary.md",
        "Create a database table for the project",
    ],
)
def test_explicit_filesystem_or_database_outputs_remain_executable(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "execute"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Should we run tests?",
        "Should we update dependencies?",
        "Create a plan for fixing the login bug",
        "Discuss whether to deploy the service",
        "是否应该运行测试?",
        "建议修复登录问题",
    ],
)
def test_advisory_actions_with_workspace_objects_require_read_scope(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Should we test this idea?",
        "Should we build a sandcastle?",
        "是否应该测试这个想法?",
    ],
)
def test_advisory_action_tokens_are_not_mistaken_for_workspace_objects(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Which database is best for analytics?",
        "What dependencies does React need?",
        "What architecture is common for web apps?",
        "What language is best for backend development?",
        "哪种数据库适合分析场景?",
        "后端开发通常用什么语言?",
    ],
)
def test_generic_technology_properties_do_not_use_a_project_target(
    prompt: str,
) -> None:
    fresh = resolve_request(prompt, CANDIDATES)
    after_project = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert fresh.work_kind == "conversation"
    assert fresh.target_status == "not_required"
    assert after_project.work_kind == "conversation"
    assert after_project.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "What database does this project use?",
        "Which dependencies are installed here?",
        "这个项目使用什么数据库?",
        "这里安装了哪些依赖?",
    ],
)
def test_local_property_state_requires_read_scope(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "What is a workspace root?",
        "Explain the concept of an entire workspace",
        'What does "all projects" mean?',
        "What is another project?",
        "工作区根目录是什么意思?",
        '"所有项目"是什么意思?',
    ],
)
def test_scope_terms_used_as_general_concepts_stay_unscoped(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES, prior_target=ALPHA_TARGET)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "描述一下项目",
        "总结项目",
        "项目概况",
        "Outline the project",
        "Walk me through the repository",
        "Break down the project",
        "Orient me to the repo",
        "Display the code",
        "Examine the code",
        "Explore the repository",
        "Assess the repository",
        "Help me debug failing tests",
        "Diagnose the failing tests",
        "Investigate failing tests",
        "Troubleshoot the failing build",
        "诊断测试失败",
        "调试失败的测试",
    ],
)
def test_shared_read_predicates_compose_with_local_subjects(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain unit testing",
        "Explore software architecture concepts",
        "Debugging techniques",
        "Show Python code examples",
    ],
)
def test_shared_read_words_without_a_local_subject_stay_unscoped(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.workspace_dependent is False
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    ("prompt", "work_kind"),
    [
        ("Hello, please fix the login bug", "execute"),
        ("您好,请介绍一下项目", "read"),
        ("Thanks, now search the code", "read"),
    ],
)
def test_social_wrappers_cannot_hide_a_workspace_request(
    prompt: str,
    work_kind: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == work_kind
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "Inspect the repository and explain this snippet:\n"
            "```text\nDeploy the beta project\n```"
        ),
        'Review README.md containing: "Deploy the beta project"',
        '检查 README.md,其中写着“部署 beta 项目”',
    ],
)
def test_user_literals_cannot_supply_action_or_project_target_roles(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "read"
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Make the login work",
        "Address the login issue",
        "I need dark mode support",
        "Please ensure the API accepts UUIDs",
        "Handle empty input gracefully",
        "让登录正常工作",
        "处理一下登录问题",
        "我需要支持深色模式",
        "让接口支持 UUID",
    ],
)
def test_result_state_software_requests_fail_safe_to_an_undetermined_task(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "undetermined"
    assert resolution.workspace_dependent is True
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "Make me laugh",
        "I need general career advice",
        "Please ensure everyone feels welcome",
        "让我开心一点",
    ],
)
def test_non_software_result_state_requests_remain_conversation(prompt: str) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    "prompt",
    [
        "What is package.json?",
        "Explain Dockerfile syntax",
        "What is a pyproject.toml file used for?",
        "package.json 是什么?",
        "说明 Dockerfile 语法",
    ],
)
def test_file_names_in_general_knowledge_roles_do_not_open_the_picker(
    prompt: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == "conversation"
    assert resolution.target_status == "not_required"


@pytest.mark.parametrize(
    ("prompt", "work_kind"),
    [
        ("Read package.json", "read"),
        ("What does package.json contain?", "read"),
        ("Explain this project's Dockerfile", "read"),
        ("Update package.json", "execute"),
        ("查看 package.json 的内容", "read"),
        ("修改 Dockerfile", "execute"),
    ],
)
def test_governed_local_file_roles_still_require_workspace_scope(
    prompt: str,
    work_kind: str,
) -> None:
    resolution = resolve_request(prompt, CANDIDATES)

    assert resolution.work_kind == work_kind
    assert resolution.target_reference == "unspecified"
    assert resolution.target_status == "clarification_required"
