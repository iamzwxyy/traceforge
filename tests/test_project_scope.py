from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import traceforge.project_scope as project_scope_module
from traceforge.models import ProjectCandidate
from traceforge.project_scope import (
    discover_project_candidates,
    is_explicit_project_switch_request,
    is_negated_project_switch_request,
    is_other_project_scope_request,
    is_project_overview_request,
    is_project_scope_followup_request,
    is_project_scope_reset_request,
    lookup_explicit_project_candidates,
    lookup_project_candidate_by_name,
    matching_candidates,
    negated_project_switch_candidates,
    positive_project_switch_candidates,
)


def _project_id(path: str) -> str:
    return "project_" + hashlib.sha256(path.encode()).hexdigest()[:16]


def _candidate(path: str) -> ProjectCandidate:
    return ProjectCandidate(
        id=_project_id(path),
        path=path,
        label=path,
        description=f"Test project · {path}/package.json",
        markers=["package.json"],
        identity="1:2:3",
    )


def _create_project(root: Path, name: str, marker: str, *, readme: bool = True) -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / marker).write_text("{}\n" if marker == "package.json" else "module test\n")
    if readme:
        (project / "README.md").write_text(f"# {name}\n")
    return project


def test_discovery_matches_real_multi_project_workspace_shape(tmp_path: Path) -> None:
    root = tmp_path / "630"
    root.mkdir()
    (root / "generate_latency_report.js").write_text("export {};\n")
    (root / "latency_comparison_report.html").write_text("<html></html>\n")
    (root / "第一份_vs_第二份_译音延迟归因报告.html").write_text("<html></html>\n")

    _create_project(root, "eval_center_middleware", "go.mod")
    _create_project(root, "vc-asx", "package.json")
    _create_project(root, ".tmp_bytedmysql", "setup.py")
    _create_project(root, "_tmp_bytedmysql", "pyproject.toml")
    _create_project(root, ".idea", "package.json")
    _create_project(root, ".trae", "package.json")

    readme_only = root / "prompt_previews"
    readme_only.mkdir()
    (readme_only / "README.md").write_text("# Prompt previews\n")

    nested_only = root / "group"
    (nested_only / "nested_project").mkdir(parents=True)
    (nested_only / "nested_project" / "package.json").write_text("{}\n")

    outside = tmp_path / "outside_project"
    outside.mkdir()
    (outside / "pyproject.toml").write_text("[project]\nname = 'outside'\n")
    (root / "linked_project").symlink_to(outside, target_is_directory=True)

    inventory = discover_project_candidates(root)

    assert inventory.complete is True
    assert inventory.root_is_project is False
    assert [candidate.path for candidate in inventory.candidates] == [
        "eval_center_middleware",
        "vc-asx",
    ]
    assert inventory.candidates[0].markers == ["go.mod", "README.md"]
    assert inventory.candidates[1].markers == ["package.json", "README.md"]
    assert all(candidate.identity.count(":") == 2 for candidate in inventory.candidates)


def test_discovery_rejects_a_child_swapped_to_an_external_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = _create_project(root, "app", "package.json")
    outside = _create_project(tmp_path, "outside", "pyproject.toml")
    real_open = project_scope_module.os.open
    swapped = False

    def swap_before_child_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal swapped
        if path == "app" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            app.rename(root / "app-original")
            app.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(project_scope_module.os, "open", swap_before_child_open)

    inventory = discover_project_candidates(root)

    assert swapped is True
    assert inventory.candidates == ()
    assert "pyproject.toml" not in repr(inventory)


def test_discovery_fails_closed_when_workspace_root_changes_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "old-app", "package.json")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _create_project(replacement, "new-app", "pyproject.toml")
    real_markers = project_scope_module._project_markers_from_fd
    swapped = False

    def swap_after_root_marker_scan(
        directory_fd: int,
    ) -> tuple[list[tuple[str, str]], str | None, bool]:
        nonlocal swapped
        result = real_markers(directory_fd)
        if not swapped:
            swapped = True
            root.rename(tmp_path / "old-workspace")
            replacement.rename(root)
        return result

    monkeypatch.setattr(
        project_scope_module,
        "_project_markers_from_fd",
        swap_after_root_marker_scan,
    )

    inventory = discover_project_candidates(root)

    assert swapped is True
    assert inventory.complete is False
    assert inventory.candidates == ()
    assert inventory.root_identity is None


def test_discovery_rejects_same_root_inode_with_changed_ctime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "alpha", "package.json")
    original = root.stat(follow_symlinks=False)
    real_stat = project_scope_module.os.stat

    def report_new_root_generation(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        result = real_stat(path, *args, **kwargs)
        if path == root and kwargs.get("follow_symlinks") is False:
            return SimpleNamespace(
                st_mode=original.st_mode,
                st_dev=original.st_dev,
                st_ino=original.st_ino,
                st_ctime_ns=original.st_ctime_ns + 1,
            )
        return result

    monkeypatch.setattr(project_scope_module.os, "stat", report_new_root_generation)

    inventory = discover_project_candidates(root)

    assert inventory.complete is False
    assert inventory.candidates == ()
    assert inventory.root_identity is None


def test_discovery_sorting_and_ids_are_stable(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("zeta", "Beta", "alpha"):
        _create_project(root, name, "package.json", readme=False)

    first = discover_project_candidates(root)
    (root / "unrelated.txt").write_text("not a project marker\n")
    second = discover_project_candidates(root)

    expected_paths = ["alpha", "Beta", "zeta"]
    expected_ids = [_project_id(path) for path in expected_paths]
    assert [candidate.path for candidate in first.candidates] == expected_paths
    assert [candidate.id for candidate in first.candidates] == expected_ids
    assert [
        (candidate.path, candidate.id, candidate.markers)
        for candidate in second.candidates
    ] == [
        (candidate.path, candidate.id, candidate.markers)
        for candidate in first.candidates
    ]


def test_root_manifest_marks_workspace_root_as_project(tmp_path: Path) -> None:
    root = tmp_path / "single-root-project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'single-root-project'\nversion = '0.1.0'\n"
    )
    (root / "README.md").write_text("# Single root project\n")

    inventory = discover_project_candidates(root)

    assert inventory.complete is True
    assert inventory.root_is_project is True
    assert inventory.candidates == ()


def test_discovery_marks_inventory_incomplete_when_scan_cap_is_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "capped-workspace"
    root.mkdir()
    for index in range(3):
        (root / f"loose-{index}.txt").write_text("loose\n")
    monkeypatch.setattr(project_scope_module, "MAX_ROOT_ENTRIES", 2)

    inventory = discover_project_candidates(root)

    assert inventory.complete is False
    assert inventory.root_is_project is False


def test_targeted_lookup_finds_an_explicit_project_beyond_candidate_cap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(55):
        _create_project(root, f"project-{index:03d}", "package.json", readme=False)

    inventory = discover_project_candidates(root)
    targeted = lookup_explicit_project_candidates(root, "介绍 project-051 项目")

    assert inventory.complete is False
    assert "project-051" not in {candidate.path for candidate in inventory.candidates}
    assert [candidate.path for candidate in targeted] == ["project-051"]
    assert targeted[0].markers == ["package.json"]
    assert targeted[0].identity.count(":") == 2


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 ../outside",
        "介绍 ./project-001",
        "介绍 nested/project-001",
        "介绍 nested\\project-001",
    ],
)
def test_targeted_lookup_rejects_path_syntax(tmp_path: Path, prompt: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "project-001", "package.json")

    assert lookup_explicit_project_candidates(root, prompt) == ()


def test_targeted_lookup_rejects_symlinks_and_directories_without_markers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = _create_project(tmp_path, "outside", "package.json")
    (root / "linked-project").symlink_to(outside, target_is_directory=True)
    (root / "plain-directory").mkdir()

    assert lookup_explicit_project_candidates(root, "介绍 linked-project") == ()
    assert lookup_explicit_project_candidates(root, "介绍 plain-directory") == ()


def test_targeted_lookup_does_not_select_a_generic_common_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "project", "package.json")

    assert lookup_explicit_project_candidates(root, "Describe the project") == ()


def test_targeted_lookup_supports_quoted_unicode_and_spaces(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "中文 项目", "package.json")

    targeted = lookup_explicit_project_candidates(root, '介绍 "中文 项目"')

    assert [candidate.path for candidate in targeted] == ["中文 项目"]


def test_targeted_lookup_does_not_treat_an_unrelated_quote_as_a_project_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "hello world", "package.json")

    targeted = lookup_explicit_project_candidates(
        root,
        '介绍一下项目, README 标题 "hello world" 是什么意思?',
    )

    assert targeted == ()


def test_targeted_lookup_ignores_unsafe_name_but_keeps_safe_explicit_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "project-051", "package.json", readme=False)

    targeted = lookup_explicit_project_candidates(
        root,
        "介绍 ../outside 和 project-051 项目",
    )

    assert [candidate.path for candidate in targeted] == ["project-051"]


def test_targeted_lookup_probes_exact_markers_beyond_entry_scan_cap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = root / "large-project"
    project.mkdir()
    for index in range(project_scope_module.MAX_PROJECT_ENTRIES + 5):
        (project / f"loose-{index:04d}.txt").write_text("loose\n")
    (project / "package.json").write_text("{}\n")

    targeted = lookup_explicit_project_candidates(root, "介绍 large-project 项目")

    assert [candidate.path for candidate in targeted] == ["large-project"]
    assert targeted[0].markers == ["package.json"]


def test_exact_name_lookup_revalidates_a_direct_child_without_scanning_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "project-051", "package.json", readme=False)

    def reject_scandir(_path: object) -> object:
        raise AssertionError("exact lookup should not scan workspace siblings")

    real_scandir = project_scope_module.os.scandir

    def scoped_scandir(path: object) -> object:
        if path == root:
            return reject_scandir(path)
        return real_scandir(path)

    monkeypatch.setattr(project_scope_module.os, "scandir", scoped_scandir)

    candidate = lookup_project_candidate_by_name(root, "project-051")

    assert candidate is not None
    assert candidate.path == "project-051"
    assert candidate.markers == ["package.json"]


@pytest.mark.parametrize("name", ["../outside", "nested/project", ".hidden"])
def test_exact_name_lookup_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert lookup_project_candidate_by_name(root, name) is None


def test_discovery_skips_non_utf8_and_bidi_project_names_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bidi = root / "safe\u202eevil"
    bidi.mkdir()
    (bidi / "package.json").write_text("{}\n")

    inventory = discover_project_candidates(root)

    assert project_scope_module._unsafe_project_name("unsafe-\udcff") is True
    assert inventory.complete is False
    assert inventory.candidates == ()


def test_discovery_ignores_bidi_control_characters_in_marker_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "spoof\u202e.sln").write_text("Microsoft Visual Studio Solution File\n")
    child = _create_project(root, "safe-app", "package.json", readme=False)
    (child / "spoof\u202e.sln").write_text("Microsoft Visual Studio Solution File\n")

    inventory = discover_project_candidates(root)

    assert inventory.root_is_project is False
    assert [candidate.path for candidate in inventory.candidates] == ["safe-app"]
    assert inventory.candidates[0].markers == ["package.json"]


def test_bounded_discovery_does_not_use_listdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_project(root, "app", "package.json")

    def reject_listdir(_path: object) -> list[str]:
        raise AssertionError("bounded discovery must stream with scandir")

    monkeypatch.setattr(project_scope_module.os, "listdir", reject_listdir)

    inventory = discover_project_candidates(root)

    assert [candidate.path for candidate in inventory.candidates] == ["app"]


def test_bounded_entries_reads_at_most_cap_plus_one_and_closes_scandir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    class CountingScanner:
        def __init__(self) -> None:
            self.paths = [root / f"entry-{index}" for index in range(10)]
            self.index = 0
            self.next_calls = 0
            self.closed = False

        def __enter__(self) -> CountingScanner:
            return self

        def __exit__(self, *_args: object) -> None:
            self.closed = True

        def __iter__(self) -> CountingScanner:
            return self

        def __next__(self) -> object:
            if self.index >= len(self.paths):
                raise StopIteration
            path = self.paths[self.index]
            self.index += 1
            self.next_calls += 1
            return type("Entry", (), {"path": str(path)})()

    scanner = CountingScanner()
    monkeypatch.setattr(project_scope_module.os, "scandir", lambda _path: scanner)

    entries, complete = project_scope_module._bounded_entries(root, 2)

    assert [entry.name for entry in entries] == ["entry-0", "entry-1"]
    assert complete is False
    assert scanner.next_calls == 3
    assert scanner.closed is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下项目",
        "这个工程是做什么的?",
        "给我一个代码库概览",
        "Describe the project",
        "Help me understand this repository",
    ],
)
def test_project_overview_intent_recognizes_ambiguous_overviews(prompt: str) -> None:
    candidates = [_candidate("app"), _candidate("app2")]

    assert is_project_overview_request(prompt, candidates) is True
    assert matching_candidates(prompt, candidates) == []


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下项目",
        "项目是做什么的?",
        "分析一下项目",
        "介绍一个项目",
        "介绍某个项目",
        "介绍任意项目",
        "给我一个代码库概览",
        "Describe the project",
        "What is the project?",
        "What does the repository do?",
        "Describe a project",
        "Describe any project",
        "Explain one project",
        "Give me an overview of the repository",
    ],
)
def test_unqualified_project_overview_is_not_an_inheritable_followup(
    prompt: str,
) -> None:
    assert is_project_scope_followup_request(prompt) is False


def test_padded_project_overview_cannot_bypass_scope_classification() -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(
        "介绍一下项目 " + "x" * 1_000,
        candidates,
    ) is True
    assert is_project_overview_request(
        "介绍一下项目 " + "x" * 1_000 + " 并修改 README",
        candidates,
    ) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍并修改 app 项目",
        "先概述项目, 再创建一个新文件",
        "Explain and fix the app project",
        "Describe the repository and deploy it",
    ],
)
def test_project_overview_intent_excludes_implementation_requests(prompt: str) -> None:
    candidates = [_candidate("app"), _candidate("app2")]

    assert is_project_overview_request(prompt, candidates) is False


def test_explicit_candidate_matching_respects_project_name_boundaries() -> None:
    app = _candidate("app")
    app2 = _candidate("app2")
    candidates = [app, app2]

    assert matching_candidates("介绍 app2", candidates) == [app2]
    assert matching_candidates("介绍 app", candidates) == [app]
    assert matching_candidates("介绍app项目", candidates) == [app]
    assert matching_candidates("介绍 app20", candidates) == []
    assert is_project_overview_request("介绍 app2", candidates) is True
    assert is_project_overview_request("app2 用了什么数据库?", candidates) is True


def test_explicit_candidate_matching_prefers_the_longest_overlapping_name() -> None:
    api = _candidate("api")
    api_server = _candidate("api-server")
    app = _candidate("app")
    app_web = _candidate("app.web")

    assert matching_candidates("介绍 api-server 项目", [api, api_server]) == [api_server]
    assert matching_candidates("介绍 app.web 项目", [app, app_web]) == [app_web]
    assert matching_candidates("比较 api 和 api-server", [api, api_server]) == [
        api,
        api_server,
    ]


def test_generic_candidate_names_require_explicit_target_evidence() -> None:
    repo = _candidate("repo")
    project = _candidate("项目")
    alpha = _candidate("alpha")

    assert matching_candidates("Describe the repo", [repo, alpha]) == []
    assert matching_candidates("介绍一下项目", [project, alpha]) == []
    assert matching_candidates('Describe "repo"', [repo, alpha]) == [repo]
    assert matching_candidates("Describe project named repo", [repo, alpha]) == [repo]
    assert matching_candidates("Compare repo and alpha", [repo, alpha]) == [repo, alpha]


@pytest.mark.parametrize("name", ["项", "目", "介绍"])
def test_short_chinese_candidate_names_do_not_match_generic_overview(
    name: str,
) -> None:
    assert matching_candidates("介绍一下项目", [_candidate(name)]) == []


@pytest.mark.parametrize(
    "prompt",
    [
        "比较 app 和 app2",
        "对比 app 与 app2",
        "app 和 app2 有什么区别",
        "compare app and app2",
        "app vs app2",
        "app versus app2",
    ],
)
def test_project_comparison_of_two_explicit_candidates_enters_scope_route(
    prompt: str,
) -> None:
    candidates = [_candidate("app"), _candidate("app2")]

    assert matching_candidates(prompt, candidates) == candidates
    assert is_project_overview_request(prompt, candidates) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下项目怎么运行",
        "介绍如何运行项目",
        "介绍如何部署项目",
        "分析项目部署架构",
        "Describe the project build system",
        "Explain how to run the app project",
        "Explain how the app project should run",
    ],
)
def test_project_overview_treats_explanations_of_operations_as_read_only(prompt: str) -> None:
    assert is_project_overview_request(prompt, [_candidate("app")]) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍如何测试项目",
        "介绍项目的测试架构",
        "说明如何启动项目",
        "介绍项目的安装方式",
        "介绍项目的更新流程",
        "介绍如何升级项目",
        "分析项目的编译流程",
        "介绍项目的重启方式",
        "介绍项目的停止流程",
        "Explain how to test the project",
        "Describe the project testing architecture",
        "Explain how to install the project dependencies",
    ],
)
def test_new_operation_vocabulary_keeps_how_to_requests_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍项目的运行环境",
        "介绍项目的测试覆盖率",
        "介绍项目的构建工具",
        "介绍项目的安装依赖",
        "介绍项目的更新机制",
        "介绍项目的编译选项",
    ],
)
def test_operation_nouns_without_command_structure_remain_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "运行项目并介绍结果",
        "介绍项目并部署它",
        "介绍一下项目\uff0c运行测试",
        "介绍项目\uff0c最后部署它",
        "Describe the project and deploy it",
    ],
)
def test_project_overview_still_rejects_operation_commands(prompt: str) -> None:
    assert is_project_overview_request(prompt, [_candidate("app")]) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍项目并测试它",
        "介绍项目并启动服务",
        "介绍项目并安装依赖",
        "介绍项目并更新依赖",
        "介绍项目并升级依赖",
        "介绍项目并编译它",
        "介绍项目并重启服务",
        "介绍项目并停止服务",
        "Describe the project and test it",
        "Describe the project and start the service",
        "Describe the project and install dependencies",
        "Describe the project and update dependencies",
        "Describe the project and upgrade dependencies",
        "Describe the project and compile it",
        "Describe the project and restart the service",
        "Describe the project and stop the service",
    ],
)
def test_new_operation_vocabulary_rejects_mixed_execution_requests(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "action",
    [
        "编辑 README",
        "改一下配置",
        "写一个文件",
        "生成报告",
        "重命名入口文件",
        "移动模块",
        "复制配置",
        "提交代码",
        "优化代码",
        "开发功能",
        "完善文档",
        "edit the README",
        "change the configuration",
        "write a file",
        "generate a report",
        "rename the entry file",
        "move the module",
        "copy the configuration",
        "commit the code",
        "optimize the code",
        "develop a feature",
        "improve the documentation",
    ],
)
def test_extended_mutation_vocabulary_rejects_mixed_execution(action: str) -> None:
    prompt = (
        f"Describe the alpha project and {action}"
        if action.isascii()
        else f"介绍 alpha 项目并{action}"
    )
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 alpha 项目并改 README",
        "介绍 alpha 项目然后改代码",
        "介绍 alpha 项目\uff0c帮我改下配置",
        "改 README 后介绍 alpha 项目",
    ],
)
def test_contextual_change_verbs_are_executable_in_command_structures(
    prompt: str,
) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 alpha 项目并同步配置",
        "介绍 alpha 项目\uff0c请校准配置",
        "Describe the alpha project and synchronize the configuration",
        "Describe the alpha project; please calibrate the configuration",
    ],
)
def test_unknown_mixed_imperative_falls_open_to_the_planner(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 alpha 项目并修一下 bug",
        "介绍 alpha 项目并调整配置",
        "介绍 alpha 项目并清理代码",
    ],
)
def test_unlisted_but_structural_mutation_commands_fall_open(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍项目并分析架构",
        "Describe the project and explain its architecture",
    ],
)
def test_known_read_only_mixed_clauses_remain_in_project_scope(prompt: str) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "suffix",
    [
        "并给出主要功能",
        "并说说技术栈",
        "并回答它做什么",
        "并指出入口文件",
        "并整理目录结构",
        "然后告诉我怎么运行",
        "\uff0c并重点讲架构",
        "\uff0c帮我了解技术栈",
        " and highlight architecture",
        " and give key features",
        ", please focus on database",
    ],
)
def test_common_read_only_mixed_phrasings_remain_scoped(suffix: str) -> None:
    prompt = f"介绍 alpha 项目{suffix}"
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 alpha 项目\uff0c顺便同步配置",
        "介绍 alpha 项目\uff0c同时校准配置",
        "介绍 alpha 项目\uff0c顺手调整配置",
        "Describe alpha, also synchronize the config",
        "Describe alpha while calibrating the config",
        "Describe alpha; also calibrate config",
    ],
)
def test_discourse_markers_with_actions_fall_open_to_planner(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is False
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍 alpha 项目\uff0c顺便说说技术栈",
        "介绍 alpha 项目\uff0c同时介绍架构",
        "Describe alpha, also explain dependencies",
        "Describe alpha while explaining dependencies",
    ],
)
def test_discourse_markers_with_read_only_clauses_remain_scoped(
    prompt: str,
) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "启动服务",
        "安装依赖",
        "更新依赖",
        "Start the service",
        "Install dependencies",
        "Update dependencies",
    ],
)
def test_direct_operations_do_not_inherit_project_scope(prompt: str) -> None:
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("再详细介绍一下", True),
        ("它用了什么数据库?", True),
        ("这个项目如何部署?", True),
        ("继续修复它", False),
        ("运行测试", False),
    ],
)
def test_project_scope_followup_classifier(prompt: str, expected: bool) -> None:
    assert is_project_scope_followup_request(prompt) is expected


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍一下你自己",
        "讲讲 Python",
        "分析这个日志",
        "这个日志是什么?",
        "Tell me about yourself",
        "Explain this log",
        "What is this log?",
        "What time is it?",
        "Why is it raining?",
    ],
)
def test_unrelated_overview_words_do_not_inherit_project_scope(prompt: str) -> None:
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "再详细介绍一下",
        "它用了什么数据库?",
        "分析项目架构",
        "解释 README",
        "Explain the README",
    ],
)
def test_bounded_project_references_still_inherit_project_scope(prompt: str) -> None:
    assert is_project_scope_followup_request(prompt) is True


def test_bare_it_requires_a_project_detail_to_inherit_scope() -> None:
    assert is_project_scope_followup_request("What architecture does it use?") is True
    assert is_project_scope_followup_request("它有哪些依赖?") is True


@pytest.mark.parametrize(
    "prompt",
    [
        "继续",
        "再详细一点",
        "continue",
        "有哪些依赖?",
        "技术栈是什么?",
        "架构怎么样?",
        "数据库呢?",
        "主要功能是什么?",
        "用什么语言写的?",
        "入口在哪?",
    ],
)
def test_adjacent_project_continuations_and_details_inherit_scope(prompt: str) -> None:
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "继续, 今天天气怎么样?",
        "继续讲 Python?",
        "更多关于北京的信息?",
        "More about the weather?",
        "Continue, what time is it?",
    ],
)
def test_continuation_words_with_a_new_topic_do_not_inherit_scope(
    prompt: str,
) -> None:
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "Python 有哪些依赖?",
        "React 的架构怎么样?",
        "北京的主要语言是什么?",
    ],
)
def test_bare_project_details_with_an_external_subject_do_not_inherit(
    prompt: str,
) -> None:
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "Tell me about the project",
        "Walk me through this repo",
    ],
)
def test_more_common_project_overview_phrasings_are_recognized(prompt: str) -> None:
    assert is_project_overview_request(prompt, [_candidate("app")]) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍项目的实现原理",
        "分析项目的修改历史",
        "说明之前修复了哪些问题",
    ],
)
def test_mutation_words_in_explanatory_noun_contexts_remain_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍项目的编辑历史",
        "分析项目的改动记录",
        "介绍项目的提交历史",
        "分析项目的代码生成原理",
        "Describe the project edit history",
        "Analyze the project change records",
        "Describe the project commit history",
        "Explain the project code generation principles",
    ],
)
def test_extended_mutation_words_keep_explanatory_context_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍移动端项目",
        "介绍代码编辑器项目",
        "介绍生成式 AI 项目",
        "介绍写作助手项目",
        "介绍提交系统项目",
        "介绍复制工具项目",
    ],
)
def test_contextual_mutation_words_in_project_names_remain_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍查询优化器项目",
        "介绍移动开发工具项目",
        "介绍持续完善度分析项目",
        "Describe the developer tools project",
        "Describe the optimization toolkit project",
        "Describe the improvement tracker project",
    ],
)
def test_more_contextual_mutation_roots_in_names_remain_read_only(
    prompt: str,
) -> None:
    candidates = [_candidate("app")]

    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "name",
    [
        "测试平台",
        "部署工具",
        "发布平台",
        "启动器",
        "安装助手",
        "更新服务",
        "编译器",
        "运行时",
        "执行引擎",
        "升级中心",
        "test-runner",
        "update-service",
    ],
)
def test_operation_words_inside_verified_candidate_names_are_masked(name: str) -> None:
    candidate = _candidate(name)
    prompt = (
        f"Describe the {name} project"
        if name.isascii()
        else f"介绍{name}项目"
    )

    assert matching_candidates(prompt, [candidate]) == [candidate]
    assert is_project_overview_request(prompt, [candidate]) is True


@pytest.mark.parametrize(
    "name",
    [
        "代码修改器",
        "Bug修复助手",
        "项目创建工具",
        "接口实现平台",
        "插件添加器",
        "文件删除器",
        "数据库迁移工具",
        "代码重构助手",
        "重命名工具",
        "fix-cli",
        "create-app",
        "refactor-helper",
    ],
)
def test_strong_mutation_words_inside_verified_candidate_names_are_masked(
    name: str,
) -> None:
    candidate = _candidate(name)
    prompt = (
        f"Describe the {name} project"
        if name.isascii()
        else f"介绍{name}项目"
    )

    assert matching_candidates(prompt, [candidate]) == [candidate]
    assert is_project_overview_request(prompt, [candidate]) is True


def test_chinese_candidate_names_match_natural_project_cues_and_comparison() -> None:
    testing = _candidate("测试平台")
    deploy = _candidate("部署工具")

    assert matching_candidates("介绍一下测试平台项目", [testing, deploy]) == [testing]
    assert matching_candidates("比较测试平台和部署工具", [testing, deploy]) == [
        testing,
        deploy,
    ]


@pytest.mark.parametrize(
    ("name", "prompt"),
    [
        ("Python", "讲讲 Python"),
        ("Python", "Explain Python"),
        ("React", "介绍 React"),
        ("API", "Explain the API"),
        ("日志", "分析日志"),
    ],
)
def test_common_domain_names_are_not_silent_explicit_project_selections(
    name: str,
    prompt: str,
) -> None:
    candidate = _candidate(name)

    assert matching_candidates(prompt, [candidate]) == []
    assert is_project_overview_request(prompt, [candidate]) is False


def test_domain_name_with_a_project_cue_is_an_explicit_candidate() -> None:
    react = _candidate("React")

    assert matching_candidates("介绍 React 项目", [react]) == [react]
    assert is_project_overview_request("介绍 React 项目", [react]) is True


def test_candidate_masking_does_not_hide_a_real_mixed_action() -> None:
    candidate = _candidate("fix-cli")
    prompt = "Describe the fix-cli project and edit the README"

    assert matching_candidates(prompt, [candidate]) == [candidate]
    assert is_project_overview_request(prompt, [candidate]) is False


def test_test_result_is_a_read_only_project_explanation() -> None:
    prompt = "介绍项目的测试结果"

    assert is_project_overview_request(prompt, [_candidate("app")]) is True
    assert is_project_scope_followup_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "分析整个工作区",
        "介绍其他项目",
        "介绍另一个项目",
        "介绍所有项目",
        "analyze the entire workspace",
        "introduce another project",
        "describe all projects",
        "换个项目看看",
        "switch projects",
    ],
)
def test_scope_reset_requests_do_not_inherit_the_prior_project(prompt: str) -> None:
    candidates = [_candidate("app"), _candidate("app2")]

    assert is_project_scope_reset_request(prompt) is True
    assert is_project_scope_followup_request(prompt) is False
    assert is_project_overview_request(prompt, candidates) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍这个项目的所有依赖",
        "分析项目的整体架构",
        "它用了什么数据库?",
    ],
)
def test_scope_reset_classifier_does_not_capture_in_project_details(
    prompt: str,
) -> None:
    assert is_project_scope_reset_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍其他项目",
        "换个项目看看",
        "介绍另一个项目",
        "introduce another project",
        "show me another repository",
        "switch projects",
    ],
)
def test_other_project_scope_request_is_a_specific_reset_kind(prompt: str) -> None:
    assert is_other_project_scope_request(prompt) is True
    assert is_project_scope_reset_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "分析整个工作区",
        "介绍所有项目",
        "analyze the entire workspace",
        "describe all projects",
    ],
)
def test_workspace_wide_reset_is_not_an_other_project_request(prompt: str) -> None:
    assert is_other_project_scope_request(prompt) is False
    assert is_project_scope_reset_request(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "比较这些项目",
        "比较两个项目",
        "项目之间有什么区别",
        "对比一下项目",
        "compare the projects",
        "differences between these projects",
        "compare both repositories",
        "介绍多个项目",
        "介绍这两个项目",
    ],
)
def test_multi_project_requests_reset_single_project_scope(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_project_scope_reset_request(prompt) is True
    assert is_project_scope_followup_request(prompt) is False
    assert is_project_overview_request(prompt, candidates) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "换成 beta",
        "切换到 beta 项目",
        "switch to beta",
        "change to beta",
    ],
)
def test_explicit_candidate_switch_enters_project_scope_route(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert matching_candidates(prompt, candidates) == [candidates[1]]
    assert is_explicit_project_switch_request(prompt, candidates) is True
    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "换成 postgres",
        "switch to postgres",
        "不要换成 beta",
        "do not switch to beta",
    ],
)
def test_explicit_switch_requires_a_non_negated_verified_candidate(prompt: str) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert is_explicit_project_switch_request(prompt, candidates) is False
    assert is_project_overview_request(prompt, candidates) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "不要换成 beta\uff0c继续介绍这个项目",
        "别切换到 beta, 继续介绍该项目",
        "do not switch to beta; continue describing this project",
        "don't change to beta; continue describing this project",
    ],
)
def test_negated_candidate_switch_is_detected_without_selecting_it(
    prompt: str,
) -> None:
    candidates = [_candidate("alpha"), _candidate("beta")]

    assert matching_candidates(prompt, candidates) == [candidates[1]]
    assert is_negated_project_switch_request(prompt, candidates) is True
    assert is_explicit_project_switch_request(prompt, candidates) is False
    assert is_project_overview_request(prompt, candidates) is True
    assert is_project_scope_followup_request(prompt) is True


def test_negated_switch_candidates_are_filtered_per_candidate() -> None:
    alpha = _candidate("alpha")
    beta = _candidate("beta")
    candidates = [alpha, beta]
    prompt = "不要换成 beta\uff0c继续介绍 alpha 项目"

    assert matching_candidates(prompt, candidates) == candidates
    assert negated_project_switch_candidates(prompt, candidates) == [beta]
    assert is_project_overview_request(prompt, candidates) is True


def test_positive_switch_candidate_survives_a_different_negated_switch() -> None:
    alpha = _candidate("alpha")
    beta = _candidate("beta")
    candidates = [alpha, beta]
    prompt = "不要换成 alpha, 换成 beta"

    assert negated_project_switch_candidates(prompt, candidates) == [alpha]
    assert positive_project_switch_candidates(prompt, candidates) == [beta]
    assert is_explicit_project_switch_request(prompt, candidates) is True
    assert is_project_overview_request(prompt, candidates) is True


def test_xcode_bundle_uses_its_readable_project_file_as_evidence(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app = root / "ios-app"
    bundle = app / "TraceForge.xcodeproj"
    bundle.mkdir(parents=True)
    (bundle / "project.pbxproj").write_text("// !$*UTF8*$!\n")

    inventory = discover_project_candidates(root)

    assert [candidate.path for candidate in inventory.candidates] == ["ios-app"]
    assert inventory.candidates[0].markers == [
        "TraceForge.xcodeproj/project.pbxproj"
    ]
