from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from traceforge.models import ProjectCandidate

MAX_PROJECT_CANDIDATES = 50
MAX_ROOT_ENTRIES = 2_000
MAX_PROJECT_ENTRIES = 500
MAX_TARGETED_PROJECT_NAMES = 32
MAX_INTENT_CLASSIFICATION_CHARS = 500

_IGNORED_EXACT = {
    ".cache",
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".traceforge-uv-venv",
    ".trae",
    ".venv",
    ".vscode",
    "__pycache__",
    "artifacts",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "output",
    "outputs",
    "target",
    "temp",
    "tmp",
    "vendor",
    "venv",
}
_IGNORED_PREFIXES = (".tmp", "_tmp", "tmp_", "temp_")

_EXACT_MARKERS = {
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "CMakeLists.txt": "CMake",
    "composer.json": "PHP",
    "Cargo.toml": "Rust",
    "deno.json": "Deno",
    "deno.jsonc": "Deno",
    "Gemfile": "Ruby",
    "go.mod": "Go",
    "mix.exs": "Elixir",
    "package.json": "Node.js",
    "Package.swift": "Swift",
    "pom.xml": "Maven",
    "pubspec.yaml": "Dart/Flutter",
    "pyproject.toml": "Python",
    "setup.cfg": "Python",
    "setup.py": "Python",
    "settings.gradle": "Gradle",
    "settings.gradle.kts": "Gradle",
}
_SUFFIX_MARKERS = {
    ".csproj": ".NET",
    ".sln": ".NET",
    ".xcodeproj": "Xcode",
}
_README_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt"}
_ASCII_DIRECT_CHILD_NAME = r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,118}[A-Za-z0-9_])?"
_QUOTED_DIRECT_CHILD_NAME = re.compile(
    r'"([^"\r\n]{1,120})"|\'([^\'\r\n]{1,120})\'|`([^`\r\n]{1,120})`|'
    r"“([^”\r\n]{1,120})”|\u300c([^」\r\n]{1,120})\u300d"
)
_CONTEXTUAL_DIRECT_CHILD_NAME = re.compile(
    rf"(?P<before>{_ASCII_DIRECT_CHILD_NAME})\s*(?:项目|工程|仓库|目录)|"
    rf"(?:project|repository|repo|directory)\s+(?:named|called)\s+"
    rf"(?P<named>{_ASCII_DIRECT_CHILD_NAME})|"
    rf"(?P<english_before>{_ASCII_DIRECT_CHILD_NAME})\s+"
    r"(?:project|repository|repo|directory)\b|"
    rf"(?:介绍|查看|分析|说明|切换到|换成)\s*"
    rf"(?P<chinese_action>{_ASCII_DIRECT_CHILD_NAME})(?=\s|$|[?\uff1f])|"
    rf"\b(?:describe|introduce|explain|analy[sz]e|switch\s+to)\s+"
    rf"(?:the\s+)?(?P<english_action>{_ASCII_DIRECT_CHILD_NAME})",
    re.IGNORECASE,
)
_TARGET_NAME_STOPWORDS = {
    "a",
    "an",
    "app",
    "codebase",
    "directory",
    "project",
    "projects",
    "repo",
    "repository",
    "that",
    "the",
    "this",
}
_GENERIC_CANDIDATE_NAMES = (_TARGET_NAME_STOPWORDS - {"app"}) | {
    "介绍",
    "目",
    "项",
    "项目",
}
_DOMAIN_COLLISION_CANDIDATE_NAMES = {
    "api",
    "log",
    "logs",
    "python",
    "react",
    "日志",
}

_OVERVIEW_SIGNALS = re.compile(
    r"介绍|概览|概述|讲讲|说明|解释|了解|看看|分析|梳理|干什么|做什么|是什么|"
    r"\b(?:introduce|describ(?:e|ing)|explain|analy[sz]e|overview|summari[sz]e|"
    r"understand)\b|\btell\s+me\s+about\b|\bwalk\s+me\s+through\b|"
    r"\b(?:what\s+(?:is|does))\b",
    re.IGNORECASE,
)
_COMPARISON_SIGNALS = re.compile(
    r"比较|对比|区别|差异|"
    r"\b(?:compare|comparison|differences?|vs\.?|versus)\b",
    re.IGNORECASE,
)
_PROJECT_SIGNALS = re.compile(
    r"项目|工程|仓库|代码库|\b(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b",
    re.IGNORECASE,
)
_STRONG_MUTATION_SIGNALS = re.compile(
    r"修改|修复|创建|新建|实现|添加|删除|迁移|重构|重命名|"
    r"(?<![A-Za-z0-9_])(?:fix|create|implement|add|delete|remove|migrate|"
    r"refactor)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CONTEXTUAL_MUTATION_SIGNALS = re.compile(
    r"编辑|改一下|改下|改|优化|开发|完善|写|生成|移动|复制|提交|"
    r"(?<![A-Za-z0-9_])(?:edit|change|optimize|develop|improve|write|generate|"
    r"rename|move|copy|commit)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_EXPLANATORY_MUTATION_SIGNALS = re.compile(
    r"(?:怎么|如何|怎样)(?:去)?\s*"
    r"(?:修改|修复|创建|新建|实现|添加|删除|迁移|重构|编辑|改一下|改下|改|优化|开发|完善|写|生成|重命名|移动|复制|提交)|"
    r"(?:修改|修复|创建|新建|实现|添加|删除|迁移|重构|编辑|改一下|改下|改|优化|开发|完善|写|生成|重命名|移动|复制|提交)(?:了)?\s*"
    r"(?:原理|方式|方法|流程|架构|历史|记录|规则|说明|写法|法|原因|细节|内容|结果|哪些|什么)|"
    r"\bhow\s+(?:[\w-]+\s+){0,5}"
    r"(?:fix|create|implement|add|delete|remove|migrate|refactor|edit|change|"
    r"optimize|develop|improve|write|generate|rename|move|copy|commit)\b|"
    r"\b(?:implementation|modifications?|changes?|fix(?:es|ed)?|migrations?|"
    r"refactoring|edits?|optimization|development|improvements?|writing|"
    r"generation|renaming|moves?|copies|commits?)\s+"
    r"(?:details?|history|records?|logs?|rationale|architecture|principles?)\b|"
    r"\bwhat\s+(?:was|were|has\s+been)\s+(?:fixed|changed|implemented)\b",
    re.IGNORECASE,
)
_CHAINED_MUTATION_SIGNALS = re.compile(
    r"(?:并|然后|接着|之后|再|最后|最终)\s*"
    r"(?:修改|修复|创建|新建|实现|添加|删除|迁移|重构|编辑|改一下|改下|改|优化|开发|完善|写|生成|重命名|移动|复制|提交)|"
    r"\b(?:and|then)\s+(?:fix|create|implement|add|delete|remove|migrate|refactor|"
    r"edit|change|optimize|develop|improve|write|generate|rename|move|copy|"
    r"commit)\b",
    re.IGNORECASE,
)
_LEADING_MUTATION_SIGNALS = re.compile(
    r"^\s*(?:(?:请|帮我|现在|直接)\s*)?"
    r"(?:修改|修复|创建|新建|实现|添加|删除|迁移|重构|编辑|改一下|改下|改|优化|开发|完善|写|生成|重命名|移动|复制|提交)|"
    r"^\s*(?:please\s+)?"
    r"(?:fix|create|implement|add|delete|remove|migrate|refactor|edit|change|"
    r"optimize|develop|improve|write|generate|rename|move|copy|commit)\b",
    re.IGNORECASE,
)
_PUNCTUATED_MUTATION_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:(?:请|帮我|现在|直接)\s*)?"
    r"(?:编辑|改一下|改下|改|优化|开发|完善|写|生成|移动|复制|提交)|"
    r"(?:[,;.!?]\s*)(?:(?:please|now|directly)\s+)?"
    r"(?:edit|change|optimize|develop|improve|write|generate|rename|move|copy|"
    r"commit)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_MUTATION_OBJECT_SIGNALS = re.compile(
    r"(?:编辑|优化|开发|完善|写|生成|移动|复制|提交)\s*"
    r"(?:一下|这个|该|一(?:个|份|段)|README|文件|代码|配置|文档|报告|模块|目录|依赖|改动|更改|性能|功能|服务|接口|应用|页面)|"
    r"(?:改一下|改下|改)\s*"
    r"(?:README|文件|代码|配置|文档|报告|模块|目录|依赖)|"
    r"(?<![A-Za-z0-9_])(?:edit|change|optimize|develop|improve|write|generate|"
    r"rename|move|copy|commit)\s+"
    r"(?:the|a|an|this|that|readme|files?|code|configuration|config|documents?|"
    r"reports?|modules?|directories|dependencies|changes?|performance|features?|"
    r"services?|interfaces?|applications?|pages?)\b",
    re.IGNORECASE,
)
_GENERIC_MIXED_CLAUSE_SIGNALS = re.compile(
    r"(?:并|然后|接着|之后|再|最后|最终)\s*\S+|"
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*(?:顺便|同时|顺手)\s*\S+|"
    r"\b(?:and|then|while)\s+\S+|[,;.!?]\s*also\s+\S+",
    re.IGNORECASE,
)
_READ_ONLY_MIXED_CLAUSE_SIGNALS = re.compile(
    r"再\s*(?:详细|深入)\s*(?:介绍|说明|解释|分析)|"
    r"(?:并|然后|接着|之后|再|最后|最终)\s*"
    r"(?:介绍|说明|解释|分析|深入分析|查看|阅读|搜索|总结|概述|概括|比较|对比|梳理|列出|给出|给我|说说|回答|指出|整理|告诉|重点讲|了解)|"
    r"\b(?:and|then)\s+(?:introduce|describe|explain|analy[sz]e|inspect|read|"
    r"search|summari[sz]e|compare|list|show|tell|highlight|give|focus)\b|"
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*(?:顺便|同时|顺手)\s*"
    r"(?:介绍|说明|解释|分析|查看|阅读|搜索|总结|概述|概括|比较|对比|梳理|列出|给出|给我|说说|回答|指出|整理|告诉|重点讲|了解)|"
    r"[,;.!?]\s*also\s+(?:introduce|describe|explain|analy[sz]e|inspect|read|"
    r"search|summari[sz]e|compare|list|show|tell|highlight|give|focus)\b|"
    r"\bwhile\s+(?:introducing|describing|explaining|analy[sz]ing|inspecting|"
    r"reading|searching|summarizing|comparing|listing|showing|telling|"
    r"highlighting|giving|focusing)\b",
    re.IGNORECASE,
)
_GENERIC_PUNCTUATED_IMPERATIVE_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:请|请你|帮我|麻烦|能否|可以|现在|直接)\s*\S+|"
    r"[,;.!?]\s*(?:please|can\s+you|could\s+you|now|directly)\s+\S+",
    re.IGNORECASE,
)
_READ_ONLY_PUNCTUATED_IMPERATIVE_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:请|请你|帮我|麻烦|能否|可以|现在|直接)\s*"
    r"(?:介绍|说明|解释|分析|查看|阅读|搜索|总结|概述|概括|比较|对比|梳理|列出|给出|给我|说说|回答|指出|整理|告诉|重点讲|了解)|"
    r"[,;.!?]\s*(?:please|can\s+you|could\s+you|now|directly)\s+"
    r"(?:introduce|describe|explain|analy[sz]e|inspect|read|search|summari[sz]e|"
    r"compare|list|show|tell|highlight|give|focus)\b",
    re.IGNORECASE,
)
_OPERATION_SIGNALS = re.compile(
    r"运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止|"
    r"\b(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop)\b",
    re.IGNORECASE,
)
_EXPLANATORY_OPERATION_SIGNALS = re.compile(
    r"(?:怎么|如何|怎样)(?:去)?\s*"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)|"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)\s*"
    r"(?:方式|方法|流程|架构|配置|原理|说明|概览|系统|指南|结果|历史|记录)|"
    r"\bhow\s+(?:[\w-]+\s+){0,5}"
    r"(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop)\b|"
    r"\b(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop|building|running|execution|deployment|publishing|"
    r"testing|startup|installation|updating|upgrading|compilation|restarting|"
    r"stopping)\s+"
    r"(?:method|process|workflow|architecture|configuration|setup|system|"
    r"results?|history|records?)\b",
    re.IGNORECASE,
)
_CHAINED_OPERATION_SIGNALS = re.compile(
    r"(?:并|然后|接着|之后|再|最后|最终|顺便|同时|顺手)\s*"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)|"
    r"\b(?:and|then|also|while)\s+(?:build|run|execute|deploy|publish|test|start|install|"
    r"update|upgrade|compile|restart|stop)\b",
    re.IGNORECASE,
)
_LEADING_OPERATION_SIGNALS = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦|能否|可以|现在|直接)\s*)?"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)|"
    r"^\s*(?:please\s+)?(?:build|run|execute|deploy|publish|test|start|install|"
    r"update|upgrade|compile|restart|stop)\b",
    re.IGNORECASE,
)
_PUNCTUATED_OPERATION_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:(?:请|请你|帮我|麻烦|能否|可以|现在|直接|顺便|同时|顺手)\s*)?"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)|"
    r"[,;.!?]\s*(?:(?:please|also|while|now|directly)\s+)?"
    r"(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop)\b",
    re.IGNORECASE,
)
_IMPERATIVE_OPERATION_SIGNALS = re.compile(
    r"(?:请|请你|帮我|麻烦|能否|可以|我要|我想|需要)\s*"
    r"(?:运行|执行|部署|发布|构建|测试|启动|安装|更新|升级|编译|重启|停止)|"
    r"\b(?:please|(?:i\s+)?(?:want|need)\s+to|can\s+you|could\s+you)\s+"
    r"(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop)\b",
    re.IGNORECASE,
)
_PROJECT_ARTIFACT_SIGNALS = re.compile(
    r"\b(?:readme|changelog|manifest|package\.json|pyproject\.toml|go\.mod|"
    r"cargo\.toml)\b|项目文档|入口文件|配置文件|代码结构",
    re.IGNORECASE,
)
_FOLLOWUP_REFERENCE_SIGNALS = re.compile(
    r"(?:这个|该)(?:项目|工程|仓库|代码库)|继续|再详细|深入|更多|"
    r"\b(?:this|that)\s+(?:project|repository|repo|codebase)\b|"
    r"\b(?:continue|more|further)\b",
    re.IGNORECASE,
)
_PROJECT_PRONOUN_SIGNALS = re.compile(r"它|\bit\b", re.IGNORECASE)
_PROJECT_DETAIL_SIGNALS = re.compile(
    r"数据库|依赖|架构|技术栈|主要功能|功能|语言|入口|"
    r"\b(?:database|dependencies|architecture|stack|features?|language|entry)\b",
    re.IGNORECASE,
)
_FOLLOWUP_CONTINUATION_SIGNALS = re.compile(
    r"继续(?:一下)?|再详细(?:一点|一些|些)?|深入|更多|"
    r"\b(?:continue|more|further)\b",
    re.IGNORECASE,
)
_FOLLOWUP_QUESTION_SIGNALS = re.compile(
    r"什么|哪些|哪|在哪|怎么样|为何|为什么|是否|吗|呢|数据库|依赖|架构|技术栈|"
    r"[?\uff1f]|\b(?:what|which|why|whether|database|dependencies|architecture|stack)\b",
    re.IGNORECASE,
)
_PROJECT_POSSESSIVE_DETAIL_SIGNALS = re.compile(
    r"(?:项目|工程|仓库|代码库)\s*的\s*\S|"
    r"\b(?:projects?|repositor(?:y|ies)|repos?|codebases?)['\u2019]s\s+\w",
    re.IGNORECASE,
)
_QUALIFIED_PROJECT_CONTEXT_SIGNALS = re.compile(
    r"(?>(?:介绍|概览|概述|讲讲|说明|解释|了解|看看|分析)(?:一下|下)?)\s*"
    r"(?!(?:(?:一个|一项|某个|某项|任意(?:一个|一项)?|任何(?:一个|一项)?|"
    r"随便(?:一个|一项)?)\s*)?(?:项目|工程|仓库|代码库)"
    r"(?:$|[\s,.!?\uff0c\u3002\uff01\uff1f]))"
    r"[^,;.!?\uff0c\u3002\uff1b\uff01\uff1f\r\n]{1,80}?"
    r"(?:项目|工程|仓库|代码库)|"
    r"(?>(?:describe|introduce|explain|analy[sz]e|summari[sz]e)\s+"
    r"(?:the\s+)?)"
    r"(?!(?:(?:a|an|any|some|one)\s+|one\s+of\s+the\s+)?"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b)"
    r"[A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+){0,8}\s+"
    r"(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_PURE_PROJECT_CONTINUATION_SIGNALS = re.compile(
    r"^\s*(?:继续(?:一下|说|说说|讲|讲讲|介绍)?|"
    r"接着(?:说|讲|介绍)|展开(?:说说|讲讲)|再(?:说说|讲讲)|"
    r"再详细(?:介绍|说明|解释|分析)?(?:一下|一点|一些|些)?|"
    r"再详细(?:说说|讲讲)|"
    r"更多(?:细节|信息)?|"
    r"(?:continue|more|further)(?:\s+(?:details?|explanation|analysis))?)"
    r"\s*(?:[.!?]|\u3002|\uff01|\uff1f)?\s*$",
    re.IGNORECASE,
)
_WHOLE_WORKSPACE_SCOPE_SIGNALS = re.compile(
    r"整个工作区|工作区整体|工作区根目录|"
    r"\b(?:whole|entire)\s+workspace\b",
    re.IGNORECASE,
)
_OTHER_PROJECT_SCOPE_SIGNALS = re.compile(
    r"(?:换个|换一个|换到另一个|切换到另一个)\s*"
    r"(?:项目|工程|仓库|代码库)|切换项目|"
    r"(?:其他|其它|别的|另一个|另一)\s*"
    r"(?:项目|工程|仓库|代码库)|"
    r"(?:另一个|另一)(?=$|[\s?\uff1f])|"
    r"\b(?:other|another)\s+(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b|"
    r"\banother\s+one\b|\b(?:switch\s+projects?|pick\s+another\s+project)\b",
    re.IGNORECASE,
)
_MULTI_PROJECT_SCOPE_SIGNALS = re.compile(
    r"(?:所有|全部|每个)\s*(?:项目|工程|仓库|代码库)|"
    r"(?:多个|这两个|这些|两个)\s*(?:项目|工程|仓库|代码库)|"
    r"项目\s*之间|(?:比较|对比)(?:一下)?\s*(?:这些|两个|多个)?\s*项目|"
    r"\b(?:all|every|multiple|two|these|both)\s+"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b|"
    r"\bthe\s+(?:projects|repositories|repos|codebases)\b|"
    r"\bbetween\s+(?:these|the|two)\s+"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b",
    re.IGNORECASE,
)
_EXPLICIT_PROJECT_SWITCH_SIGNALS = re.compile(
    r"换成|换到|切换到|\b(?:switch|change)\s+to\b",
    re.IGNORECASE,
)
_NEGATED_EXPLICIT_PROJECT_SWITCH_SIGNALS = re.compile(
    r"(?:不要|别)\s*(?:换成|换到|切换到)|"
    r"\b(?:do\s+not|don't|dont|never)\s+(?:switch|change)\s+to\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProjectInventory:
    candidates: tuple[ProjectCandidate, ...]
    root_is_project: bool
    root_markers: tuple[str, ...]
    root_identity: str | None
    complete: bool


def is_ignored_workspace_directory(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _IGNORED_EXACT or lowered.startswith(_IGNORED_PREFIXES)


def _bounded_intent_request(request: str) -> str:
    compact = request.strip()
    if len(compact) <= MAX_INTENT_CLASSIFICATION_CHARS:
        return compact
    # Preserve both the task framing and trailing action clause. Treating every long prompt as
    # unrelated lets a padded generic overview bypass host scope selection; scanning the whole
    # prompt would make every regex classifier depend on unbounded input.
    tail_length = MAX_INTENT_CLASSIFICATION_CHARS // 2
    head_length = MAX_INTENT_CLASSIFICATION_CHARS - tail_length - 1
    return compact[:head_length] + "\n" + compact[-tail_length:]


def discover_project_candidates(root: Path) -> ProjectInventory:
    """Discover stable direct-child project roots without reading file contents."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return ProjectInventory(
            candidates=(),
            root_is_project=False,
            root_markers=(),
            root_identity=None,
            complete=False,
        )
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OSError("workspace root is not a directory")
        root_markers, root_readme, root_complete = _project_markers_from_fd(root_fd)
        entry_names, entries_complete = _bounded_entry_names_from_fd(
            root_fd,
            MAX_ROOT_ENTRIES,
        )
        candidates: list[ProjectCandidate] = []
        complete = root_complete and entries_complete
        for name in entry_names:
            if len(candidates) >= MAX_PROJECT_CANDIDATES:
                complete = False
                break
            if _unsafe_project_name(name):
                complete = False
                continue
            if name.startswith(".") or is_ignored_workspace_directory(name):
                continue
            candidate = _lookup_direct_child_project_from_fd(root_fd, name)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda item: (item.path.casefold(), item.path))
        current_root = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(current_root.st_mode) or _directory_identity_token(
            current_root
        ) != _directory_identity_token(root_stat):
            raise OSError("workspace root changed during project discovery")
        return ProjectInventory(
            candidates=tuple(candidates),
            root_is_project=bool(root_markers),
            root_markers=tuple(
                [name for name, _kind in root_markers]
                + ([root_readme] if root_readme else [])
            )[:20],
            root_identity=_directory_identity_token(root_stat),
            complete=complete,
        )
    except OSError:
        return ProjectInventory(
            candidates=(),
            root_is_project=False,
            root_markers=(),
            root_identity=None,
            complete=False,
        )
    finally:
        os.close(root_fd)


def lookup_explicit_project_candidates(
    root: Path,
    request: str,
) -> tuple[ProjectCandidate, ...]:
    """Look up named direct-child projects without relying on a capped root scan."""

    compact = _bounded_intent_request(request)
    if not compact:
        return ()
    names = _targeted_project_names(compact)
    candidates: list[ProjectCandidate] = []
    for name in names[:MAX_TARGETED_PROJECT_NAMES]:
        candidate = _lookup_direct_child_project(root, name)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.path.casefold(), item.path))
    return tuple(candidates)


def lookup_project_candidate_by_name(
    root: Path,
    name: str,
) -> ProjectCandidate | None:
    """Safely revalidate one exact direct-child project name without scanning siblings."""

    return _lookup_direct_child_project(root, name)


def is_project_overview_request(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> bool:
    compact = _bounded_intent_request(request)
    if not compact:
        return False
    explicit = matching_candidates(compact, candidates)
    explicit_switch = is_explicit_project_switch_request(compact, candidates)
    switch_masked = _mask_project_switch_clauses(compact, candidates)
    intent_request = _mask_candidate_names(switch_masked, explicit)
    if _has_execution_intent(intent_request):
        return False
    negated_ids = {
        candidate.id
        for candidate in negated_project_switch_candidates(compact, candidates)
    }
    if negated_ids:
        explicit = [candidate for candidate in explicit if candidate.id not in negated_ids]
    if explicit_switch:
        return True
    if is_project_scope_reset_request(compact):
        return True
    if len(explicit) >= 2 and _COMPARISON_SIGNALS.search(compact):
        return True
    if not _OVERVIEW_SIGNALS.search(compact):
        return bool(explicit and _FOLLOWUP_QUESTION_SIGNALS.search(compact))
    return bool(
        _PROJECT_SIGNALS.search(compact)
        or _PROJECT_ARTIFACT_SIGNALS.search(compact)
        or _EXPLANATORY_MUTATION_SIGNALS.search(compact)
        or explicit
    )


def matching_candidates(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> list[ProjectCandidate]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for candidate in candidates:
        if _unsafe_project_name(candidate.path):
            continue
        candidate_spans: list[tuple[int, int]] = []
        for match in re.finditer(re.escape(candidate.path), request, re.IGNORECASE):
            start, end = match.span()
            if _candidate_occurrence_is_explicit(
                request,
                start,
                end,
                candidate.path,
            ):
                candidate_spans.append((start, end))
        if candidate_spans:
            spans[candidate.id] = candidate_spans
    return [
        candidate
        for candidate in candidates
        if candidate.id in spans
        and any(
            not any(
                other_start <= start
                and end <= other_end
                and (other_start, other_end) != (start, end)
                for other in candidates
                for other_start, other_end in spans.get(other.id, [])
            )
            for start, end in spans[candidate.id]
        )
    ]


def is_explicit_project_switch_request(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> bool:
    """Recognize a scope switch only when it names a verified project candidate."""

    return bool(positive_project_switch_candidates(request, candidates))


def positive_project_switch_candidates(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> list[ProjectCandidate]:
    """Return candidates targeted by a positive, candidate-local scope switch."""

    compact = _bounded_intent_request(request)
    if (
        not compact
        or not _EXPLICIT_PROJECT_SWITCH_SIGNALS.search(compact)
    ):
        return []
    positive: list[ProjectCandidate] = []
    for candidate in matching_candidates(compact, candidates):
        if any(
            not _switch_match_is_negated(compact, match.start())
            for match in _positive_project_switch_pattern(candidate).finditer(compact)
        ):
            positive.append(candidate)
    return positive


def is_negated_project_switch_request(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> bool:
    """Recognize a negated switch that names a verified candidate but must not select it."""

    return bool(negated_project_switch_candidates(request, candidates))


def negated_project_switch_candidates(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> list[ProjectCandidate]:
    """Return only candidates named as the target of a negated scope switch."""

    compact = _bounded_intent_request(request)
    if (
        not compact
        or not _NEGATED_EXPLICIT_PROJECT_SWITCH_SIGNALS.search(compact)
    ):
        return []
    negated: list[ProjectCandidate] = []
    for candidate in matching_candidates(compact, candidates):
        name = re.escape(candidate.path)
        if re.search(
            rf"(?:不要|别)\s*(?:换成|换到|切换到)\s*"
            rf"(?:项目\s*)?{name}(?:\s*(?:项目|工程|仓库|代码库))?|"
            rf"\b(?:do\s+not|don't|dont|never)\s+(?:switch|change)\s+to\s+"
            rf"(?:the\s+)?(?:project\s+)?{name}\b",
            compact,
            re.IGNORECASE,
        ):
            negated.append(candidate)
    return negated


def is_project_scope_followup_request(request: str) -> bool:
    """Recognize bounded, read-only references to a project selected in a prior turn."""

    compact = _bounded_intent_request(request)
    if not compact or _has_execution_intent(compact):
        return False
    if is_project_scope_reset_request(compact):
        return False
    overview = _OVERVIEW_SIGNALS.search(compact)
    project_context = _PROJECT_SIGNALS.search(compact)
    project_artifact = _PROJECT_ARTIFACT_SIGNALS.search(compact)
    project_reference = _FOLLOWUP_REFERENCE_SIGNALS.search(compact)
    project_pronoun = _PROJECT_PRONOUN_SIGNALS.search(compact)
    project_detail = _PROJECT_DETAIL_SIGNALS.search(compact)
    possessive_detail = _PROJECT_POSSESSIVE_DETAIL_SIGNALS.search(compact)
    qualified_project_context = _QUALIFIED_PROJECT_CONTEXT_SIGNALS.search(compact)
    external_detail_subject = bool(
        project_detail
        and _has_external_project_detail_subject(compact, project_detail.start())
    )
    question = _FOLLOWUP_QUESTION_SIGNALS.search(compact)
    if _PURE_PROJECT_CONTINUATION_SIGNALS.fullmatch(compact):
        return True
    # A generic project overview remains ambiguous even on an adjacent turn. Inheritance needs
    # a real backward reference or a qualified detail, not merely the word "project" plus a
    # generic overview/question signal.
    if project_context and (overview or question) and (
        project_reference
        or project_detail
        or project_artifact
        or possessive_detail
        or qualified_project_context
        or _READ_ONLY_MIXED_CLAUSE_SIGNALS.search(compact)
    ):
        return True
    if project_artifact and (overview or question):
        return True
    if _EXPLANATORY_MUTATION_SIGNALS.search(compact) and overview:
        return True
    if _OPERATION_SIGNALS.search(compact) and _EXPLANATORY_OPERATION_SIGNALS.search(
        compact
    ):
        return True
    if project_pronoun and project_detail:
        return True
    if overview and project_detail and _READ_ONLY_MIXED_CLAUSE_SIGNALS.search(compact):
        return True
    return bool(project_detail and question and not external_detail_subject)


def is_project_scope_reset_request(request: str) -> bool:
    """Recognize read-only requests that intentionally leave the prior project scope."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and not _has_execution_intent(compact)
        and (
            is_other_project_scope_request(compact)
            or _WHOLE_WORKSPACE_SCOPE_SIGNALS.search(compact)
            or _MULTI_PROJECT_SCOPE_SIGNALS.search(compact)
        )
    )


def is_other_project_scope_request(request: str) -> bool:
    """Recognize a read-only request to leave the current project for another one."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and not _has_execution_intent(compact)
        and _OTHER_PROJECT_SCOPE_SIGNALS.search(compact)
    )


def _word_character(value: str) -> bool:
    return bool(value) and (value.isalnum() or value == "_")


def _has_external_project_detail_subject(request: str, detail_start: int) -> bool:
    prefix = request[:detail_start].strip(" \t,;\uff0c\uff1b:\uff1a")
    if not prefix:
        return False
    if re.fullmatch(
        r"(?:有?哪些|用什么|什么|what|which|how\s+many)",
        prefix,
        re.IGNORECASE,
    ):
        return False
    if _PROJECT_SIGNALS.search(prefix) or _PROJECT_PRONOUN_SIGNALS.search(prefix):
        return False
    if re.search(r"\b[A-Za-z][A-Za-z0-9_.+-]*\b", prefix):
        return True
    return bool(re.search(r".{1,40}的(?:主要)?$", prefix))


def _candidate_occurrence_is_explicit(
    request: str,
    start: int,
    end: int,
    name: str,
) -> bool:
    before = request[start - 1] if start else ""
    after = request[end] if end < len(request) else ""
    has_initial_boundary = not (
        _word_character(before) and _word_character(request[start])
    )
    has_terminal_boundary = not (
        _word_character(after) and _word_character(request[end - 1])
    )
    has_boundary = has_initial_boundary and has_terminal_boundary
    before_text = request[:start]
    after_text = request[end:]
    quoted = bool(
        start
        and end < len(request)
        and (before, after)
        in {('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("「", "」")}
    )
    named = bool(
        re.search(r"(?:名为|叫|named|called)\s*$", before_text, re.IGNORECASE)
    )
    chinese_after_cue = re.match(
        r"^\s*(?:项目|工程|仓库|目录)"
        r"(?=$|[\s的和与及、,;\uff0c\uff1b.!?\u3002\uff01\uff1f])",
        after_text,
        re.IGNORECASE,
    )
    english_after_cue = re.match(
        r"^\s*(?:project|repository|repo|directory)\b",
        after_text,
        re.IGNORECASE,
    )
    after_cue = bool(chinese_after_cue or english_after_cue)
    directory_cue = bool(
        re.match(
            r"^\s*目录(?=$|[\s的和与及、,;\uff0c\uff1b.!?\u3002\uff01\uff1f])|"
            r"^\s*directory\b",
            after_text,
            re.IGNORECASE,
        )
    )
    comparison = bool(_COMPARISON_SIGNALS.search(request))
    comparison_before = re.search(
        r"(?:比较|对比|和|与|及|and|vs\.?|versus)\s*$",
        before_text,
        re.IGNORECASE,
    )
    comparison_after = re.match(
        r"^\s*(?:(?:和|与|及)|(?:and|vs\.?|versus)\b)",
        after_text,
        re.IGNORECASE,
    )
    comparison_target = bool(
        comparison
        and (
            (
                comparison_before
                and (comparison_after or has_terminal_boundary)
            )
            or (
                comparison_after
                and (comparison_before or has_initial_boundary)
            )
        )
    )
    before_cue = re.search(
        r"(?:介绍(?:一下)?(?:这个|该)?|查看|分析|说明|解释|切换到|换成)\s*$",
        before_text,
        re.IGNORECASE,
    )
    switch_cue = re.search(
        r"(?:切换到|换成|换到|(?:switch|change)\s+to)\s*"
        r"(?:the\s+)?(?:project\s+)?$",
        before_text,
        re.IGNORECASE,
    )
    preceding_project_cue = re.search(
        r"(?:项目|工程|仓库|目录|project|repository|repo|directory)"
        r"\s*(?:名为|叫|named|called)?\s*$",
        before_text,
        re.IGNORECASE,
    )
    if name.casefold() in _GENERIC_CANDIDATE_NAMES:
        return bool(
            (has_boundary and (quoted or named or directory_cue))
            or comparison_target
        )
    if name.casefold() in _DOMAIN_COLLISION_CANDIDATE_NAMES:
        return bool(
            quoted
            or named
            or switch_cue
            or after_cue
            or preceding_project_cue
            or comparison_target
        )
    if has_boundary:
        return True
    return bool(
        quoted
        or named
        or comparison_target
        or (before_cue and has_terminal_boundary)
        or (
            after_cue
            and (has_initial_boundary or before_cue or comparison_before)
        )
    )


def _mask_candidate_names(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> str:
    masked = list(request)
    for candidate in sorted(candidates, key=lambda item: len(item.path), reverse=True):
        for match in re.finditer(re.escape(candidate.path), request, re.IGNORECASE):
            index, end = match.span()
            if _candidate_occurrence_is_explicit(
                request,
                index,
                end,
                candidate.path,
            ):
                masked[index:end] = " " * (end - index)
    return "".join(masked)


def _positive_project_switch_pattern(candidate: ProjectCandidate) -> re.Pattern[str]:
    name = re.escape(candidate.path)
    return re.compile(
        rf"(?:换成|换到|切换到)\s*(?:项目\s*)?{name}"
        r"(?:\s*(?:项目|工程|仓库|代码库))?"
        r"(?=$|[\s,;\uff0c\uff1b.!?\u3002\uff01\uff1f])|"
        rf"\b(?:switch|change)\s+to\s+(?:the\s+)?(?:project\s+)?{name}"
        r"(?:\s+(?:project|repository|repo|codebase))?"
        r"(?=$|[\s,;.!?])",
        re.IGNORECASE,
    )


def _switch_match_is_negated(request: str, switch_start: int) -> bool:
    before = request[max(0, switch_start - 32) : switch_start]
    return bool(
        re.search(r"(?:不要|别)\s*$", before)
        or re.search(
            r"\b(?:do\s+not|don't|dont|never)\s*$",
            before,
            re.IGNORECASE,
        )
    )


def _mask_project_switch_clauses(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> str:
    masked = list(request)
    for candidate in candidates:
        if _unsafe_project_name(candidate.path):
            continue
        for match in _positive_project_switch_pattern(candidate).finditer(request):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _is_bare_explicit_project_switch(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    name = re.escape(candidate.path)
    chinese = re.compile(
        rf"^\s*(?:(?:请|帮我)\s*)?(?:换成|换到|切换到)\s*"
        rf"(?:项目\s*)?{name}(?:\s*(?:项目|工程|仓库|代码库))?"
        r"(?:\s*(?:看看|吧))?\s*(?:[.!?]|\u3002|\uff01|\uff1f)?\s*$",
        re.IGNORECASE,
    )
    english = re.compile(
        rf"^\s*(?:please\s+)?(?:switch|change)\s+to\s+(?:the\s+)?"
        rf"(?:project\s+)?{name}(?:\s+(?:project|repository|repo|codebase))?"
        r"(?:\s+please)?\s*[.!?]?\s*$",
        re.IGNORECASE,
    )
    return bool(chinese.fullmatch(request) or english.fullmatch(request))


def _has_execution_intent(request: str) -> bool:
    if _STRONG_MUTATION_SIGNALS.search(request) and (
        _CHAINED_MUTATION_SIGNALS.search(request)
        or _LEADING_MUTATION_SIGNALS.search(request)
        or not _EXPLANATORY_MUTATION_SIGNALS.search(request)
    ):
        return True
    if _CONTEXTUAL_MUTATION_SIGNALS.search(request) and (
        _CHAINED_MUTATION_SIGNALS.search(request)
        or _LEADING_MUTATION_SIGNALS.search(request)
        or _PUNCTUATED_MUTATION_SIGNALS.search(request)
        or (
            _CONTEXTUAL_MUTATION_OBJECT_SIGNALS.search(request)
            and not _EXPLANATORY_MUTATION_SIGNALS.search(request)
        )
    ):
        return True
    if _OVERVIEW_SIGNALS.search(request) and (
        (
            _GENERIC_MIXED_CLAUSE_SIGNALS.search(request)
            and not _READ_ONLY_MIXED_CLAUSE_SIGNALS.search(request)
        )
        or (
            _GENERIC_PUNCTUATED_IMPERATIVE_SIGNALS.search(request)
            and not _READ_ONLY_PUNCTUATED_IMPERATIVE_SIGNALS.search(request)
        )
    ):
        return True
    if not _OPERATION_SIGNALS.search(request):
        return False
    return bool(
        _CHAINED_OPERATION_SIGNALS.search(request)
        or _LEADING_OPERATION_SIGNALS.search(request)
        or _PUNCTUATED_OPERATION_SIGNALS.search(request)
        or _IMPERATIVE_OPERATION_SIGNALS.search(request)
    )


def _unsafe_project_name(name: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name)


def _directory_identity_token(identity: os.stat_result) -> str:
    return f"{identity.st_dev}:{identity.st_ino}:{identity.st_ctime_ns}"


def _lookup_direct_child_project(root: Path, name: str) -> ProjectCandidate | None:
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or _unsafe_project_name(name)
        or is_ignored_workspace_directory(name)
    ):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd: int | None = None
    try:
        root_fd = os.open(root, flags)
        return _lookup_direct_child_project_from_fd(root_fd, name)
    except OSError:
        return None
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _lookup_direct_child_project_from_fd(
    root_fd: int,
    name: str,
) -> ProjectCandidate | None:
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or _unsafe_project_name(name)
        or is_ignored_workspace_directory(name)
    ):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    project_fd: int | None = None
    try:
        project_fd = os.open(name, flags, dir_fd=root_fd)
        project_stat = os.fstat(project_fd)
        if not stat.S_ISDIR(project_stat.st_mode):
            return None
        markers, readme, _complete = _project_markers_from_fd(project_fd)
        if not markers:
            return None
        marker_names = [marker_name for marker_name, _kind in markers]
        kinds = list(dict.fromkeys(kind for _marker_name, kind in markers))
        evidence = (marker_names + ([readme] if readme else []))[:20]
        marker_summary = ", ".join(evidence[:4])
        if len(evidence) > 4:
            marker_summary += f" 等 {len(evidence)} 个根标记"
        description = f"{', '.join(kinds)} 项目 · {marker_summary}"[:400]
        return ProjectCandidate(
            id="project_" + hashlib.sha256(name.encode()).hexdigest()[:16],
            path=name,
            label=name[:120],
            description=description,
            markers=evidence,
            identity=_directory_identity_token(project_stat),
        )
    except OSError:
        return None
    finally:
        if project_fd is not None:
            os.close(project_fd)


def _targeted_project_names(request: str) -> list[str]:
    names: list[str] = []
    for match in _QUOTED_DIRECT_CHILD_NAME.finditer(request):
        name = next(group for group in match.groups() if group is not None)
        if _quoted_target_has_project_cue(request, match.start(), match.end()):
            names.append(name.strip())
    for match in _CONTEXTUAL_DIRECT_CHILD_NAME.finditer(request):
        name = next(group for group in match.groups() if group is not None)
        if name.casefold() not in _TARGET_NAME_STOPWORDS:
            names.append(name)
    safe_names: list[str] = []
    for name in dict.fromkeys(names):
        if (
            not name
            or len(name) > 120
            or name in {".", ".."}
            or name.startswith(".")
            or ".." in name
            or "/" in name
            or "\\" in name
            or _unsafe_project_name(name)
            or is_ignored_workspace_directory(name)
        ):
            continue
        safe_names.append(name)
        if len(safe_names) >= MAX_TARGETED_PROJECT_NAMES:
            break
    return safe_names


def _quoted_target_has_project_cue(request: str, start: int, end: int) -> bool:
    before = request[max(0, start - 80) : start]
    after = request[end : end + 32]
    return bool(
        re.search(
            r"(?:名为|叫|named|called|介绍(?:项目|工程|仓库|目录)?|"
            r"切换到|换成|(?:switch|change)\s+to|project|repository|repo|directory)"
            r"\s*$",
            before,
            re.IGNORECASE,
        )
        or re.match(
            r"^\s*(?:项目|工程|仓库|目录)"
            r"(?=$|[\s的和与及、,;\uff0c\uff1b.!?\u3002\uff01\uff1f])|"
            r"^\s*(?:project|repository|repo|directory)\b",
            after,
            re.IGNORECASE,
        )
    )


def _project_markers_from_fd(
    directory_fd: int,
) -> tuple[list[tuple[str, str]], str | None, bool]:
    markers: list[tuple[str, str]] = []
    for marker_name, kind in _EXACT_MARKERS.items():
        try:
            marker_stat = os.stat(
                marker_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            continue
        if stat.S_ISREG(marker_stat.st_mode):
            markers.append((marker_name, kind))
    entries: list[os.DirEntry[str]] = []
    complete = True
    with os.scandir(directory_fd) as scanner:
        for index, entry in enumerate(scanner):
            if index >= MAX_PROJECT_ENTRIES:
                complete = False
                break
            entries.append(entry)
    readme: str | None = None
    for entry in sorted(entries, key=lambda item: (item.name.casefold(), item.name)):
        try:
            if _unsafe_project_name(entry.name) or entry.is_symlink():
                continue
            if entry.name.casefold() in _README_NAMES and entry.is_file(
                follow_symlinks=False
            ):
                readme = entry.name
            if entry.name in _EXACT_MARKERS:
                continue
            for suffix, suffix_kind in _SUFFIX_MARKERS.items():
                if not entry.name.endswith(suffix):
                    continue
                if suffix != ".xcodeproj":
                    if entry.is_file(follow_symlinks=False):
                        markers.append((entry.name, suffix_kind))
                    break
                if not entry.is_dir(follow_symlinks=False):
                    break
                bundle_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    project_file = os.stat(
                        "project.pbxproj",
                        dir_fd=bundle_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISREG(project_file.st_mode):
                        markers.append(
                            (f"{entry.name}/project.pbxproj", suffix_kind)
                        )
                finally:
                    os.close(bundle_fd)
                break
        except OSError:
            continue
    return (
        sorted(markers, key=lambda item: (item[0].casefold(), item[0])),
        readme,
        complete,
    )


def _bounded_entry_names_from_fd(
    directory_fd: int,
    limit: int,
) -> tuple[list[str], bool]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as scanner:
            for index, entry in enumerate(scanner):
                if index >= limit:
                    return sorted(names, key=lambda name: (name.casefold(), name)), False
                names.append(entry.name)
    except OSError:
        return [], False
    return sorted(names, key=lambda name: (name.casefold(), name)), True


def _bounded_entries(directory: Path, limit: int) -> tuple[list[Path], bool]:
    try:
        entries: list[Path] = []
        with os.scandir(directory) as scanner:
            for index, entry in enumerate(scanner):
                if index >= limit:
                    return (
                        sorted(
                            entries,
                            key=lambda item: (item.name.casefold(), item.name),
                        ),
                        False,
                    )
                entries.append(Path(entry.path))
    except OSError:
        return [], False
    return sorted(entries, key=lambda item: (item.name.casefold(), item.name)), True
