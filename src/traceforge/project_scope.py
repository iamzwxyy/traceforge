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
    "app",
    "log",
    "logs",
    "node",
    "npm",
    "python",
    "pytest",
    "react",
    "uv",
    "日志",
}

_CHINESE_OVERVIEW_READ_ACTION = (
    r"(?:介绍|说明|解释|描述|总结|概括|讲解|解读|概述|梳理)"
)
_ENGLISH_OVERVIEW_READ_ACTION = (
    r"(?:introduce|describe|explain|summari[sz]e|outline|walk\s+(?:me\s+)?through|"
    r"break\s+down|orient\s+me\s+to)"
)
_CHINESE_INSPECTION_READ_ACTION = (
    r"(?:展示|查看|看看|检查|审查|查找|定位|搜索|阅读|分析|探索|评估)"
)
_ENGLISH_INSPECTION_READ_ACTION = (
    r"(?:show|display|inspect|review|check|find|look|locate|search|read|analy[sz]e|"
    r"examine|explore|assess)"
)
_CHINESE_DIAGNOSTIC_READ_ACTION = r"(?:诊断|调试|排查|调查)"
_ENGLISH_DIAGNOSTIC_READ_ACTION = (
    r"(?:debug|diagnose|investigate|troubleshoot)"
)
_OVERVIEW_NOUN_READ_SIGNAL = (
    r"(?:项目|工程|仓库|代码库)(?:概况|概览|概述|简介)|"
    r"\b(?:project|repository|repo|codebase)\s+(?:overview|summary|profile)\b"
)
_CHINESE_READ_ACTION = (
    rf"(?:{_CHINESE_OVERVIEW_READ_ACTION}|{_CHINESE_INSPECTION_READ_ACTION}|"
    rf"{_CHINESE_DIAGNOSTIC_READ_ACTION})"
)
_ENGLISH_READ_ACTION = (
    rf"(?:{_ENGLISH_OVERVIEW_READ_ACTION}|{_ENGLISH_INSPECTION_READ_ACTION}|"
    rf"{_ENGLISH_DIAGNOSTIC_READ_ACTION})"
)
_ENGLISH_READ_ACTION_INFLECTED = (
    r"(?:introducing|describing|explaining|summari[sz]ing|outlining|"
    r"walking\s+(?:me\s+)?through|breaking\s+down|orienting\s+me\s+to|"
    r"showing|displaying|inspecting|reviewing|checking|finding|looking|locating|"
    r"searching|reading|analy[sz]ing|examining|exploring|assessing|debugging|"
    r"diagnosing|investigating|troubleshooting)"
)

_OVERVIEW_SIGNALS = re.compile(
    rf"{_CHINESE_OVERVIEW_READ_ACTION}|概览|讲讲|了解|看看|分析|干什么|做什么|是什么|"
    rf"\b{_ENGLISH_OVERVIEW_READ_ACTION}\b|"
    r"\b(?:overview|understand|analy[sz]e)\b|\btell\s+me\s+about\b|"
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

# Keep action vocabulary in data-like regex fragments.  Intent grammars below compose these
# fragments with modality, clause position, object, and target-role syntax; an action word alone
# is deliberately not enough to decide whether a request authorizes workspace execution.
_CHINESE_STRONG_MUTATION_ACTION = r"(?:修改|修复|创建|新建|实现|添加|删除|迁移|重构|重命名)"
_CHINESE_CONTEXTUAL_MUTATION_ACTION = (
    r"(?:编辑|改一下|改下|改|优化|开发|完善|写入|写|保存(?:到)?|落盘|"
    r"生成|移动|复制|提交)"
)
_CHINESE_MUTATION_ACTION = (
    rf"(?:{_CHINESE_STRONG_MUTATION_ACTION}|{_CHINESE_CONTEXTUAL_MUTATION_ACTION})"
)
_CHINESE_OPERATION_ACTION = (
    r"(?:构建|运行|执行|部署|发布|测试|启动|安装|更新|升级|编译|重启|停止)"
)
_CHINESE_ACTION = rf"(?:{_CHINESE_MUTATION_ACTION}|{_CHINESE_OPERATION_ACTION})"
_CHINESE_DIRECT_PROJECT_ACTION = rf"(?!(?:运行|执行)){_CHINESE_ACTION}"

_ENGLISH_STRONG_MUTATION_ACTION = (
    r"(?:fix|create|implement|add|delete|remove|migrate|refactor)"
)
_ENGLISH_CONTEXTUAL_MUTATION_ACTION = (
    r"(?:edit|change|optimize|develop|improve|write|generate|rename|move|copy|"
    r"commit|apply|save|store|persist)"
)
_ENGLISH_MUTATION_ACTION = (
    rf"(?:{_ENGLISH_STRONG_MUTATION_ACTION}|{_ENGLISH_CONTEXTUAL_MUTATION_ACTION})"
)
_ENGLISH_OPERATION_ACTION = (
    r"(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop)"
)
_ENGLISH_ACTION = rf"(?:{_ENGLISH_MUTATION_ACTION}|{_ENGLISH_OPERATION_ACTION})"
_ENGLISH_DIRECT_PROJECT_ACTION = rf"(?!(?:run|execute)\b){_ENGLISH_ACTION}"
_ENGLISH_ACTION_INFLECTED = (
    r"(?:fix(?:ing)?|creat(?:e|ing)|implement(?:ing)?|add(?:ing)?|"
    r"delet(?:e|ing)|remov(?:e|ing)|migrat(?:e|ing)|refactor(?:ing)?|"
    r"edit(?:ing)?|chang(?:e|ing)|optimiz(?:e|ing)|develop(?:ing)?|"
    r"improv(?:e|ing)|writ(?:e|ing)|generat(?:e|ing)|renam(?:e|ing)|"
    r"mov(?:e|ing)|cop(?:y|ying)|commit(?:ting)?|appl(?:y|ying)|sav(?:e|ing)|"
    r"stor(?:e|ing)|persist(?:ing)?|build(?:ing)?|run(?:ning)?|"
    r"execut(?:e|ing)|deploy(?:ing)?|publish(?:ing)?|test(?:ing)?|"
    r"start(?:ing)?|install(?:ing)?|updat(?:e|ing)|upgrad(?:e|ing)|"
    r"compil(?:e|ing)|restart(?:ing)?|stop(?:ping)?)"
)
_ACTION_WORD_SIGNALS = re.compile(
    rf"{_CHINESE_ACTION}|"
    rf"(?<![A-Za-z0-9_]){_ENGLISH_ACTION_INFLECTED}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

_STRONG_MUTATION_SIGNALS = re.compile(
    rf"{_CHINESE_STRONG_MUTATION_ACTION}|"
    rf"(?<![A-Za-z0-9_]){_ENGLISH_STRONG_MUTATION_ACTION}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CONTEXTUAL_MUTATION_SIGNALS = re.compile(
    rf"{_CHINESE_CONTEXTUAL_MUTATION_ACTION}|"
    rf"(?<![A-Za-z0-9_]){_ENGLISH_CONTEXTUAL_MUTATION_ACTION}"
    rf"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_EXPLANATORY_MUTATION_SIGNALS = re.compile(
    r"(?:怎么|如何|怎样)(?:去)?\s*"
    rf"{_CHINESE_MUTATION_ACTION}|"
    rf"{_CHINESE_MUTATION_ACTION}(?:了)?\s*"
    r"(?:原理|方式|方法|方案|建议|计划|思路|策略|流程|架构|历史|记录|规则|说明|写法|法|原因|细节|内容|结果|哪些|什么)|"
    r"\bhow\s+(?:[\w-]+\s+){0,5}"
    rf"{_ENGLISH_MUTATION_ACTION}\b|"
    r"\b(?:implementation|modifications?|changes?|fix(?:es|ed)?|migrations?|"
    r"refactoring|edits?|optimization|development|improvements?|writing|"
    r"generation|renaming|moves?|copies|commits?)\s+"
    r"(?:details?|history|records?|logs?|rationale|architecture|principles?|"
    r"plans?|proposals?|suggestions?|strateg(?:y|ies)|approaches?)\b|"
    r"\bwhat\s+(?:was|were|has\s+been)\s+(?:fixed|changed|implemented)\b",
    re.IGNORECASE,
)
_CHAINED_MUTATION_SIGNALS = re.compile(
    r"(?:并|然后|接着|之后|再|最后|最终)\s*"
    rf"{_CHINESE_MUTATION_ACTION}|"
    rf"\b(?:and|then)\s+{_ENGLISH_MUTATION_ACTION}\b",
    re.IGNORECASE,
)
_LEADING_MUTATION_SIGNALS = re.compile(
    r"^\s*(?:(?:请|帮我|现在|直接)\s*)?"
    rf"{_CHINESE_MUTATION_ACTION}|"
    r"^\s*(?:please\s+)?"
    rf"{_ENGLISH_MUTATION_ACTION}\b",
    re.IGNORECASE,
)
_PUNCTUATED_MUTATION_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:(?:请|帮我|现在|直接)\s*)?"
    rf"{_CHINESE_MUTATION_ACTION}|"
    r"(?:[,;.!?]\s*)(?:(?:please|now|directly)\s+)?"
    rf"{_ENGLISH_MUTATION_ACTION}\b",
    re.IGNORECASE,
)
_IMPERATIVE_MUTATION_SIGNALS = re.compile(
    r"(?:请|请你|帮我|麻烦|能否|可以|我要|我想|需要)\s*"
    rf"{_CHINESE_MUTATION_ACTION}|"
    r"\b(?:please|(?:i\s+)?(?:want|need)\s+to|can\s+you|could\s+you)\s+"
    rf"{_ENGLISH_MUTATION_ACTION}\b",
    re.IGNORECASE,
)
_CONTEXTUAL_MUTATION_OBJECT_SIGNALS = re.compile(
    rf"{_CHINESE_CONTEXTUAL_MUTATION_ACTION}\s*"
    r"(?:一下|这个|该|一(?:个|份|段)|README|文件|代码|配置|文档|报告|模块|目录|依赖|改动|更改|性能|功能|服务|接口|应用|页面)|"
    r"(?:改一下|改下|改)\s*"
    r"(?:README|文件|代码|配置|文档|报告|模块|目录|依赖)|"
    rf"(?<![A-Za-z0-9_]){_ENGLISH_CONTEXTUAL_MUTATION_ACTION}\s+"
    r"(?:the|a|an|this|that|readme|files?|code|configuration|config|documents?|"
    r"reports?|modules?|directories|dependencies|changes?|performance|features?|"
    r"services?|interfaces?|applications?|pages?|patch|edits?)\b",
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
    r"highlighting|giving|focusing)\b"
    r"|(?:并|然后|接着|以及|和|与|及)\s*"
    r"(?:其|它的|各自的|[A-Za-z0-9_.-]+\s*的)?\s*"
    r"(?:性能|架构|依赖|依赖图|测试|覆盖率|代码|配置|实现|行为|功能|"
    r"技术栈|数据库|构建|部署|接口|交互)|"
    r"\b(?:and|plus)\s+(?:its|their|the|[A-Za-z0-9_.-]+['\u2019]s)?\s*"
    r"(?:performance|architecture|dependencies|dependency\s+graph|tests?|coverage|"
    r"code|configuration|implementation|behavior|features?|tech(?:nology)?\s+stack|"
    r"database|build|deployment|interfaces?|interactions?)\b",
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
_CHINESE_TARGET_ACTION = rf"(?:{_CHINESE_READ_ACTION}|{_CHINESE_ACTION})"
_ENGLISH_TARGET_ACTION = (
    rf"(?:{_ENGLISH_READ_ACTION}|{_ENGLISH_READ_ACTION_INFLECTED}|"
    rf"{_ENGLISH_ACTION_INFLECTED})"
)
_CHINESE_PROPERTY_OBJECT = (
    r"(?:架构|依赖|代码|源码|配置|测试|入口|入口文件|实现|README)"
)
_CHINESE_PROJECT_OBJECT = (
    rf"(?:{_CHINESE_PROPERTY_OBJECT}|服务|应用|项目|工程|仓库|代码库)"
)
_CHINESE_ACTION_TARGET_OBJECT = (
    rf"(?:{_CHINESE_PROPERTY_OBJECT}|登录|认证|授权|功能|问题|缺陷|接口|页面)"
)
_ENGLISH_PROJECT_OBJECT = (
    r"(?:architecture|dependencies|source\s+code|code|configs?|configuration|"
    r"tests?|entry\s+points?|implementations?|readmes?|services?|apps?|"
    r"applications?|projects?|repositor(?:y|ies)|repos?|codebases?)"
)
_GENERIC_PROJECT_OBJECT_NAME = (
    rf"(?:{_CHINESE_PROJECT_OBJECT}|{_CHINESE_ACTION_TARGET_OBJECT}|数据库|"
    rf"{_ENGLISH_PROJECT_OBJECT}|servers?|clients?|backends?|frontends?|databases?|web)"
)
_OPERATION_SIGNALS = re.compile(
    rf"{_CHINESE_OPERATION_ACTION}|\b{_ENGLISH_OPERATION_ACTION}\b",
    re.IGNORECASE,
)
_EXPLANATORY_OPERATION_SIGNALS = re.compile(
    r"(?:怎么|如何|怎样)(?:去)?\s*"
    rf"{_CHINESE_OPERATION_ACTION}|"
    rf"{_CHINESE_OPERATION_ACTION}\s*"
    r"(?:方式|方法|方案|流程|架构|配置|原理|说明|概览|系统|指南|结果|历史|记录|日志|报告|覆盖率|工具|环境|机制|选项)|"
    r"\bhow\s+(?:[\w-]+\s+){0,5}"
    rf"{_ENGLISH_OPERATION_ACTION}\b|"
    r"\b(?:build|run|execute|deploy|publish|test|start|install|update|upgrade|"
    r"compile|restart|stop|building|running|execution|deployment|publishing|"
    r"testing|startup|installation|updating|upgrading|compilation|restarting|"
    r"stopping)\s+"
    r"(?:method|process|workflow|architecture|configuration|setup|system|"
    r"results?|history|records?)\b",
    re.IGNORECASE,
)
_NEGATED_EXECUTION_SIGNALS = re.compile(
    r"(?:不要|别|无需|不需要|不用你?|禁止|我不想(?:让你)?)"
    r"\s*(?:再|直接)?\s*"
    rf"{_CHINESE_ACTION}|"
    r"\b(?:do\s+not|don't|dont|never|without)\s+"
    rf"{_ENGLISH_ACTION_INFLECTED}\b|"
    r"\b(?:no\s+need\s+to|(?:i|you|we)\s+(?:do\s+not|don't|dont)\s+"
    r"(?:need|want)(?:\s+you)?\s+to|(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+not\s+"
    r"asking\s+(?:you\s+)?to)\s+"
    rf"{_ENGLISH_ACTION_INFLECTED}\b",
    re.IGNORECASE,
)
_ADVISORY_ACTION_SIGNALS = re.compile(
    r"(?:是否|应不应该|要不要|是否应该|是否需要|可否考虑)\s*"
    rf"{_CHINESE_ACTION}|"
    rf"建议\s*(?:我们|你们?)?\s*{_CHINESE_ACTION}|"
    rf"(?:讨论|评估|分析|考虑)\s*是否[^\r\n]{{0,32}}{_CHINESE_ACTION}|"
    rf"{_CHINESE_MUTATION_ACTION}\s*"
    r"(?:方案|建议|计划|策略)|"
    r"(?:给出|提供|制定|讨论|评估|分析)[^\r\n]{0,48}"
    r"(?:建议|方案|计划|策略|风险)|"
    r"\b(?:should\s+(?:we|i)|would\s+you\s+recommend(?:\s+we)?|"
    r"do\s+you\s+recommend(?:\s+we)?|"
    r"is\s+it\s+advisable\s+to)\s+"
    rf"{_ENGLISH_ACTION}\b|"
    rf"\b(?:i|we)\s+(?:recommend|suggest)\s+(?:that\s+)?(?:we\s+)?"
    rf"{_ENGLISH_ACTION}\b|"
    r"\b(?:give|provide|draft|discuss|evaluate|assess)\b[^\r\n]{0,48}"
    r"\b(?:recommendations?|proposals?|plans?|strateg(?:y|ies)|risks?)\b|"
    r"\b(?:create|draft|prepare|write)\s+(?:a\s+)?(?:plan|proposal|strategy)\b"
    r"[^\r\n]{0,64}\b(?:for|to|about)\b[^\r\n]{0,64}"
    rf"\b{_ENGLISH_ACTION_INFLECTED}\b|"
    r"\b(?:discuss|evaluate|assess|analy[sz]e|consider)\s+whether\b"
    rf"[^\r\n]{{0,32}}\b{_ENGLISH_ACTION}\b",
    re.IGNORECASE,
)
_INFORMATIONAL_ACTION_QUERY = re.compile(
    r"(?:哪里|哪儿|在哪|在哪里)[^\r\n]{0,40}(?:实现|添加|定义|创建|生成)|"
    r"(?:实现|添加|定义|创建|生成)(?:的|了)?[^\r\n]{0,40}"
    r"(?:哪里|哪儿|在哪|在哪里)",
    re.IGNORECASE,
)
_CHAINED_OPERATION_SIGNALS = re.compile(
    r"(?:并|然后|接着|之后|再|最后|最终|顺便|同时|顺手)\s*"
    rf"{_CHINESE_OPERATION_ACTION}|"
    rf"\b(?:and|then|also|while)\s+{_ENGLISH_OPERATION_ACTION}\b",
    re.IGNORECASE,
)
_LEADING_OPERATION_SIGNALS = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦|能否|可以|现在|直接)\s*)?"
    rf"{_CHINESE_OPERATION_ACTION}|"
    rf"^\s*(?:please\s+)?{_ENGLISH_OPERATION_ACTION}\b",
    re.IGNORECASE,
)
_PUNCTUATED_OPERATION_SIGNALS = re.compile(
    r"(?:[,;.!?]|\uff0c|\u3002|\uff1b)\s*"
    r"(?:(?:请|请你|帮我|麻烦|能否|可以|现在|直接|顺便|同时|顺手)\s*)?"
    rf"{_CHINESE_OPERATION_ACTION}|"
    r"[,;.!?]\s*(?:(?:please|also|while|now|directly)\s+)?"
    rf"{_ENGLISH_OPERATION_ACTION}\b",
    re.IGNORECASE,
)
_IMPERATIVE_OPERATION_SIGNALS = re.compile(
    r"(?:请|请你|帮我|麻烦|能否|可以|我要|我想|需要)\s*"
    rf"{_CHINESE_OPERATION_ACTION}|"
    r"\b(?:please|(?:i\s+)?(?:want|need)\s+to|can\s+you|could\s+you)\s+"
    rf"{_ENGLISH_OPERATION_ACTION}\b",
    re.IGNORECASE,
)
_CHAINED_INFLECTED_ACTION_SIGNALS = re.compile(
    rf"(?:然后|接着|之后|再|最后|最终)\s*{_CHINESE_ACTION}|"
    rf"\b(?:then|next|before|after)\s+{_ENGLISH_ACTION_INFLECTED}\b",
    re.IGNORECASE,
)
_TARGET_PREFIXED_EXECUTION_SIGNALS = re.compile(
    r"^\s*(?:请|请你|帮我|麻烦)?\s*(?:在|对|为)\s*"
    r"(?:(?:这个|该|当前)\s*(?:项目|工程|仓库|代码库)|"
    r"(?:项目|工程|仓库|代码库)|它)(?:中|里|内|上面)?\s*"
    r"(?:请|请你|帮我|麻烦)?\s*"
    rf"{_CHINESE_OPERATION_ACTION}|"
    r"^\s*(?:in|on|for)\s+(?:this|that|the\s+current)\s+"
    r"(?:project|repository|repo|codebase)\s*[,]?\s*(?:please\s+)?"
    rf"{_ENGLISH_OPERATION_ACTION}\b|"
    r"^\s*(?:请|请你|帮我|麻烦)?\s*在\s*"
    r"(?:整个|全|当前)?工作区(?:的)?(?:所有|全部)?"
    r"(?:项目|工程|仓库)?(?:中|里|内|上)?\s*"
    rf"{_CHINESE_OPERATION_ACTION}|"
    r"^\s*(?:in|on|across|throughout)\s+(?:the\s+)?(?:whole\s+|entire\s+)?"
    r"workspace(?:'s)?(?:\s+(?:all|every)\s+(?:projects?|repositor(?:y|ies)|repos?))?"
    r"\s*[,]?\s*(?:please\s+)?"
    rf"{_ENGLISH_OPERATION_ACTION}\b|"
    r"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?从"
    r"[^,;.!?\r\n]{1,64}(?:复制|移动)"
    r"[^,;.!?\r\n]{0,64}(?:到|至)",
    re.IGNORECASE,
)
_NEGATED_ACTION_CLAUSE = re.compile(
    r"(?:^|[,;.!?\uFF0C\u3002\uFF01\uFF1F\uFF1B]\s*|"
    r"\b(?:but|then)\s+|(?:但是|但|然后)\s*)"
    r"(?P<body>(?:(?:请|请你|帮我|麻烦)\s*)?"
    r"(?:(?:不要|别|无需|不需要|不用你?|禁止|"
    r"我不想(?:让你)?)\s*(?:再|直接)?\s*|"
    r"(?:please\s+)?(?:do\s+not|don't|dont|never)\s+|"
    r"(?:no\s+need\s+to|(?:i|you|we)\s+(?:do\s+not|don't|dont)\s+"
    r"(?:need|want)(?:\s+you)?\s+to|(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+not\s+"
    r"asking\s+(?:you\s+)?to)\s+)"
    r"(?:(?![,;.!?\uFF0C\u3002\uFF01\uFF1F\uFF1B]|\b(?:but|then)\b|"
    r"但是|但|然后).)*)",
    re.IGNORECASE,
)
_NON_SELECTED_ALTERNATIVE_CLAUSE = re.compile(
    r"(?:\b(?:instead\s+of|rather\s+than)\b|(?:而不是|而非|不是))\s*"
    r"(?P<body>(?:(?![,;.!?\uFF0C\u3002\uFF01\uFF1F\uFF1B]|\b(?:but|then)\b|"
    r"但是|但|然后).)*)",
    re.IGNORECASE,
)
_LEADING_READ_ONLY_SIGNALS = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦|能否|可以)\s*)?"
    r"(?:(?:改为|改成|转为|换为)\s*)?"
    r"(?:检查|查看|检索|搜索|查找|扫描|审查|阅读|列出|梳理|定位|分析|排查)|"
    r"^\s*(?:please\s+)?(?:(?:instead\s+)?(?:switch|change)\s+to\s+)?"
    r"(?:inspect|check|review|search|find|scan|read|list|analy[sz]e|locate|audit)\b",
    re.IGNORECASE,
)
_READ_THEN_ACTION_SEPARATOR = re.compile(
    r"(?:并且?|然后|接着|之后|再|最后|最终|顺便|同时|顺手|"
    r"[,;.!?\uff0c\u3002\uff1b])\s*|"
    r"\b(?:and(?:\s+then)?|then|also|before|after)\b\s*",
    re.IGNORECASE,
)
_PROJECT_ARTIFACT_SIGNALS = re.compile(
    r"\b(?:readme|changelog|manifest|package\.json|pyproject\.toml|go\.mod|"
    r"cargo\.toml)\b|项目文档|入口文件|配置文件|代码结构",
    re.IGNORECASE,
)
_FOLLOWUP_REFERENCE_SIGNALS = re.compile(
    r"(?:这个|该|同一个|同一|同个|同样的|相同的|刚才(?:那个)?|上述|上面的)"
    r"(?:项目|工程|仓库|代码库)|"
    r"它的|继续|再详细|深入|更多|"
    r"\b(?:this|that|same|previous|above)\s+"
    r"(?:project|repository|repo|codebase)\b|\bits\b|"
    r"\b(?:continue|more|further)\b",
    re.IGNORECASE,
)
_PROJECT_PRONOUN_SIGNALS = re.compile(r"它|\bits?\b", re.IGNORECASE)
_PROJECT_PRONOUN_FOLLOWUP_SIGNALS = re.compile(
    r"它(?:的|里|中|用|有|是|能|如何|怎么)[^\r\n]{0,40}"
    r"(?:依赖|代码|源码|文件|实现|测试|配置|架构|技术栈|入口|功能|"
    r"数据库|模块|目录结构|目录|工作|做什么)|"
    r"\bits\s+(?:dependencies|code|files?|implementation|tests?|configuration|"
    r"architecture|stack|entry\s+point|features?|database|modules?)\b|"
    r"\b(?:what\s+does|how\s+does|how\s+is|does)\s+it\s+"
    r"(?:do|work|organized|have\s+tests?)\b|"
    r"\bwhat\s+(?:architecture|database|language|stack|dependencies|features?)\s+"
    r"does\s+it\s+(?:use|have|support)\b",
    re.IGNORECASE,
)
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
    r"(?:所有|全部|每个)\s*(?:其他|其余|别的)?\s*"
    r"(?:项目|工程|仓库|代码库)|"
    r"(?:多个|这两个|这些|两个)\s*(?:项目|工程|仓库|代码库)|"
    r"项目\s*之间|(?:比较|对比)(?:一下)?\s*(?:这些|两个|多个)?\s*项目|"
    r"\b(?:all|every|multiple|two|these|both)\s+(?:(?:other|remaining)\s+)?"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b|"
    r"\bthe\s+(?:projects|repositories|repos|codebases)\b|"
    r"\bbetween\s+(?:these|the|two)\s+"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b",
    re.IGNORECASE,
)
_MIXED_CURRENT_OTHER_SCOPE_SIGNALS = re.compile(
    r"(?:(?:这个|该|当前)项目|它)"
    r"[^\r\n]{0,80}(?:另一个|其他|别的)项目|"
    r"(?:从\s*)?(?:(?:这个|该|当前)项目|它)"
    r"[^\r\n]{0,80}(?:到|和|与|对比|比较)"
    r"[^\r\n]{0,40}(?:另一个|其他|别的)项目|"
    r"(?:\b(?:this|current)\s+(?:project|repository|repo|codebase)\b|\bit\b)"
    r"[^\r\n]{0,80}\b(?:another|other)\s+"
    r"(?:project|repository|repo|codebase)\b",
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
    explicit = target_role_candidates(compact, candidates)
    explicit_switch = is_explicit_project_switch_request(compact, candidates)
    switch_masked = _mask_project_switch_clauses(compact, candidates)
    detail_masked = _mask_candidate_nominal_details(switch_masked, candidates)
    intent_request = _mask_candidate_names(detail_masked, explicit)
    if _has_execution_intent(intent_request):
        return False
    if is_project_scope_followup_request(compact):
        return True
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
        # The synthetic workspace-root candidate participates in deterministic selection,
        # never lexical matching: treating "." as a name would match punctuation everywhere.
        if candidate.path == "." or _unsafe_project_name(candidate.path):
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


def candidate_has_target_role(request: str, candidate: ProjectCandidate) -> bool:
    """Return whether one verified-name mention is used as a project target."""

    return _candidate_has_strong_target_role(
        request, candidate
    ) or _candidate_has_detail_target_role(
        request, candidate
    ) or _candidate_has_semantic_subject_role(request, candidate)


def _mask_negated_action_clauses(request: str) -> str:
    """Remove negative action clauses before collecting positive target roles."""

    masked = list(request)
    for pattern in (_NEGATED_ACTION_CLAUSE, _NON_SELECTED_ALTERNATIVE_CLAUSE):
        for match in pattern.finditer(request):
            start, end = match.span("body")
            masked[start:end] = " " * (end - start)
    return "".join(masked)


def _candidate_has_strong_target_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    """Recognize explicit target syntax that is stronger than a possessive noun use."""

    name = re.escape(candidate.path)
    weak_action_target_allowed = not _candidate_has_weak_subject_collision(
        candidate.path
    )
    return bool(
        _candidate_has_targeted_execution_role(request, candidate)
        or _candidate_has_transfer_target_role(request, candidate)
        or re.fullmatch(
            rf"\s*[\"'`“「]{name}[\"'`”」]\s*"
            r"[.!?\u3002\uFF01\uFF1F]?\s*",
            request,
            re.IGNORECASE,
        )
        or re.search(rf"(?<![\w.-]){name}/", request, re.IGNORECASE)
        or re.search(
            rf"(?:项目|工程|仓库|目录)\s*(?:名为|叫)?\s*{name}"
            rf"(?=$|\s|的|中|里|内|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"{name}\s*(?:项目|工程|仓库|目录)|"
            rf"\b(?:project|repository|repo|codebase|directory)\s*"
            rf"(?:named|called)?\s*{name}\b|"
            rf"\b{name}\s*(?:project|repository|repo|codebase|directory)\b",
            request,
            re.IGNORECASE,
        )
        or re.search(
            rf"(?:切换到|换成)\s*(?:项目\s*)?{name}"
            rf"(?=$|\s|的|中|里|内|上|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"\b(?:switch\s+to|change\s+to)\s+"
            rf"(?:the\s+)?(?:project\s+)?{name}\b|"
            rf"\b(?:switch|change)\s+from\b[^,;.!?\r\n]{{0,48}}"
            rf"\bto\s+(?:the\s+)?(?:project\s+)?{name}\b",
            request,
            re.IGNORECASE,
        )
        or re.search(
            rf"^\s*(?:在|对)\s*(?:项目\s*)?{name}"
            rf"(?=$|\s|的|中|里|内|上|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"(?:{_CHINESE_INSPECTION_READ_ACTION}|{_CHINESE_DIAGNOSTIC_READ_ACTION}|"
            rf"{_CHINESE_ACTION})[^,;.!?\r\n]{{0,64}}(?:在|对)\s*"
            rf"(?:项目\s*)?{name}"
            rf"(?=$|\s|的|中|里|内|上|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"^\s*(?:in|inside|under|within|into|onto)\s+"
            rf"(?:the\s+)?(?:project\s+)?{name}\b|"
            rf"\b(?:{_ENGLISH_INSPECTION_READ_ACTION}|{_ENGLISH_DIAGNOSTIC_READ_ACTION}|"
            rf"{_ENGLISH_ACTION})\b[^,;.!?\r\n]{{0,64}}"
            rf"\b(?:in|inside|under|within|into|onto)\s+"
            rf"(?:the\s+)?(?:project\s+)?{name}\b",
            request,
            re.IGNORECASE,
        )
        or (
            weak_action_target_allowed
            and _candidate_has_action_target_role(request, candidate)
        )
    )


def _candidate_has_action_target_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    """Recognize a verified name filling an action's direct target slot."""

    name = re.escape(candidate.path)
    clause_start = (
        r"(?:^|[,;.!?\uFF0C\u3002\uFF01\uFF1F\uFF1B]\s*|"
        r"(?:和|与|或|或者|要么|并|并且|然后|接着|之后|后再|再|同时|最后|最终)\s*|"
        r"\b(?:and(?:\s+also)?|or|plus|as\s+well\s+as|then|next|before|after|while)\s+)"
    )
    connector = (
        r"(?:和|与|及|以及|、|或|或者|要么|并|并且|然后|后再|再|同时|,|&|\+|"
        r"\band(?:\s+also)?\b|\bplus\b|\bas\s+well\s+as\b|\bor\b|"
        r"\bthen\b|\bbefore\b|\bafter\b|\bwhile\b)"
    )
    transition = (
        r"(?:然后|接着|之后|后再|再|\bthen\b|\bbefore\b|\bafter\b|\bwhile\b)"
    )
    chinese = re.compile(
        rf"{clause_start}(?:(?:请|请你|帮我|麻烦|现在|直接|先)\s*)?"
        rf"{_CHINESE_READ_ACTION}(?:一下|下)?\s*(?:项目\s*)?{name}"
        rf"(?=\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]|{connector}|{transition}|"
        rf"(?:的\s*)?{_CHINESE_PROJECT_OBJECT}(?=\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]|"
        rf"{connector}|{transition}))|而不是|而非|不是))",
        re.IGNORECASE,
    )
    english = re.compile(
        rf"{clause_start}(?:(?:either|also)\s+)?(?:please\s+)?"
        rf"(?:{_ENGLISH_READ_ACTION}|{_ENGLISH_READ_ACTION_INFLECTED})\s+"
        rf"(?:either\s+)?(?:the\s+)?{name}"
        rf"(?=\s*(?:$|[,;.!?]|{connector}|{transition}|\bfor\b|"
        rf"{_ENGLISH_PROJECT_OBJECT}(?=\s*(?:$|[,;.!?]|{connector}|{transition}))|"
        r"\b(?:instead\s+of|rather\s+than)\b))",
        re.IGNORECASE,
    )
    return bool(chinese.search(request) or english.search(request))


def _candidate_has_transfer_target_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    """Treat both source and destination projects as required transfer scope."""

    name = re.escape(candidate.path)
    return bool(
        re.search(
            rf"从\s*(?:项目\s*)?{name}(?![A-Za-z0-9_.-])"
            rf"[^,;.!?\r\n]{{0,48}}"
            r"(?:复制|移动)|"
            rf"(?:复制|移动)\s*(?:项目\s*)?{name}"
            rf"(?![A-Za-z0-9_.-])[^,;.!?\r\n]{{0,48}}(?:到|至|进入)\s*|"
            rf"(?:复制|移动)[^,;.!?\r\n]{{0,48}}到\s*"
            rf"(?:项目\s*)?{name}(?![A-Za-z0-9_.-])|"
            rf"参考\s*(?:项目\s*)?{name}(?![A-Za-z0-9_.-])"
            rf"[^,;.!?\r\n]{{0,48}}"
            rf"{_CHINESE_MUTATION_ACTION}",
            request,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:copy|move)\b[^,;.!?\r\n]{{0,64}}\bfrom\s+"
            rf"(?:the\s+)?{name}\b|"
            rf"\b(?:copy|move)\s+(?:the\s+)?{name}\b"
            rf"[^,;.!?\r\n]{{0,48}}\b(?:to|into|onto)\b|"
            rf"\b(?:copy|move)\b[^,;.!?\r\n]{{0,64}}\b(?:to|into|onto)\s+"
            rf"(?:the\s+)?{name}\b",
            request,
            re.IGNORECASE,
        )
    )


def _candidate_has_targeted_execution_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    name = re.escape(candidate.path)
    connector = (
        r"(?:和|与|及|以及|、|或|或者|要么|并|并且|然后|后再|再|同时|,|\uFF0C|\uFF1B|&|\+|"
        r"\band(?:\s+also)?\b|\bplus\b|\bas\s+well\s+as\b|\bor\b|"
        r"\bthen\b|\bbefore\b|\bafter\b|\bwhile\b)"
    )
    strong_role = bool(
        re.search(
            rf"(?:在|对|为)\s*(?:项目\s*)?{name}"
            rf"(?:\s*(?:项目|工程|仓库))?(?:中|里|内|上)?\s*{_CHINESE_ACTION}|"
            rf"(?:在|对|为)\s*(?:项目\s*)?{name}\s*{connector}"
            rf"[^,;.!?\r\n]{{1,48}}(?:中|里|内|上)\s*{_CHINESE_ACTION}|"
            rf"{_CHINESE_ACTION}\s*(?:项目\s*)?{name}"
            rf"\s*(?:项目|工程|仓库)|"
            rf"{_CHINESE_ACTION}[^,;.!?\r\n]{{0,64}}(?:到|至|进入)\s*"
            rf"(?:项目\s*)?{name}(?![A-Za-z0-9_.-])",
            request,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:in|inside|within|for)\s+(?:the\s+)?(?:project\s+)?{name}\b"
            rf"(?:\s+(?:project|repository|repo|codebase))?\s*[,]?\s*"
            rf"{_ENGLISH_ACTION}\b|"
            rf"\b{_ENGLISH_ACTION}\s+(?:the\s+)?{name}\b"
            rf"\s+(?:project|repository|repo|codebase)\b|"
            rf"\b{_ENGLISH_ACTION}\b[^,;.!?\r\n]{{0,64}}"
            rf"\b(?:to|into|onto)\s+(?:the\s+)?(?:project\s+)?{name}\b",
            request,
            re.IGNORECASE,
        )
    )
    if strong_role:
        return True
    # An action-token directory name in an unqualified direct-object slot is ambiguous with the
    # predicate/object vocabulary itself.  Project/locative syntax above remains authoritative.
    if _candidate_has_weak_subject_collision(candidate.path):
        return False
    return bool(
        re.search(
            rf"{_CHINESE_ACTION}\s*(?:项目\s*)?{name}"
            rf"(?:(?:的)?\s*(?:测试|服务|应用|依赖)|\s*{connector})|"
            rf"(?:然后|接着|之后|再|先)?\s*{_CHINESE_ACTION}\s*"
            rf"(?:项目\s*)?{name}(?=\s*(?:[.!?\u3002\uFF01\uFF1F]?\s*$|"
            r"而不是|而非|不是))",
            request,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b{_ENGLISH_ACTION}\s+(?:either\s+)?(?:the\s+)?{name}\b"
            rf"(?:\s+(?:tests?|service|app|application|dependencies)|\s*{connector})|"
            rf"(?:^|[,;.!?]\s*|\b(?:and(?:\s+also)?|or|plus|as\s+well\s+as|"
            r"then|next|before|after|while)\s+)"
            rf"{_ENGLISH_ACTION_INFLECTED}\s+(?:either\s+)?(?:the\s+)?{name}\b"
            r"(?=\s*(?:[.!?]?\s*$|\b(?:instead\s+of|rather\s+than)\b))",
            request,
            re.IGNORECASE,
        )
    )


def _candidate_has_detail_target_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    """Recognize a sole verified subject through possessive or property syntax."""

    name = re.escape(candidate.path)
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_.-]){name}\s*(?:的|里|中)|"
            rf"\b{name}['\u2019]s\b|"
            rf"\b{_ENGLISH_PROJECT_OBJECT}\s+for\s+(?:the\s+)?{name}\b",
            request,
            re.IGNORECASE,
        )
    )


def _candidate_has_semantic_subject_role(
    request: str,
    candidate: ProjectCandidate,
) -> bool:
    """Recognize project-semantic questions with a verified name as subject."""

    if _candidate_has_weak_subject_collision(candidate.path):
        return False
    name = re.escape(candidate.path)
    return bool(
        re.search(
            rf"(?<![\w.-]){name}\s*"
            r"(?:用了|使用|采用|用|有)什么"
            r"(?:数据库|语言|依赖|技术栈)|"
            rf"\bwhat\s+(?:database|language|stack|dependencies|features?)\s+"
            rf"does\s+{name}\s+(?:use|have|support)\b|"
            rf"\bwhat\s+language\s+is\s+{name}\s+written\s+in\b",
            request,
            re.IGNORECASE,
        )
    )


def target_role_candidates(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> list[ProjectCandidate]:
    """Resolve verified-name mentions by role, including coordinated target lists."""

    positive_request = _mask_negated_action_clauses(request)
    lexical = matching_candidates(positive_request, candidates)
    comparison_visible = _comparison_candidate_mentions(
        positive_request, candidates
    )
    role_visible = [
        candidate
        for candidate in candidates
        if candidate.path != "."
        and not _unsafe_project_name(candidate.path)
        and candidate_has_target_role(positive_request, candidate)
    ]
    visible_ids = {
        candidate.id
        for candidate in [*lexical, *role_visible, *comparison_visible]
    }
    matched = [candidate for candidate in candidates if candidate.id in visible_ids]
    strong = [
        candidate
        for candidate in matched
        if _candidate_has_strong_target_role(positive_request, candidate)
    ]
    detail = [
        candidate
        for candidate in matched
        if _candidate_has_detail_target_role(positive_request, candidate)
    ]
    semantic = [
        candidate
        for candidate in matched
        if _candidate_has_semantic_subject_role(positive_request, candidate)
    ]
    primary = [*strong, *semantic]
    if not primary:
        primary = detail
    direct_ids = {candidate.id for candidate in primary}
    direct = [candidate for candidate in matched if candidate.id in direct_ids]
    selected_ids = {candidate.id for candidate in direct}
    if len(matched) > 1 and not selected_ids and (
        _ACTION_WORD_SIGNALS.search(positive_request)
        or _COMPARISON_SIGNALS.search(positive_request)
        or re.search(
            rf"{_CHINESE_READ_ACTION}|\b{_ENGLISH_READ_ACTION}\b",
            positive_request,
            re.IGNORECASE,
        )
    ):
        # Parallel property clauses have no single seed target (for example,
        # "explain alpha architecture and beta dependencies").  The shared action plus
        # coordinated verified-name roles is sufficient to classify the whole group, while a
        # bare mention such as "alpha and beta are Greek letters" remains non-workspace text.
        for index, candidate in enumerate(matched):
            for other in matched[index + 1 :]:
                if _candidate_shares_coordinated_target_group(
                    positive_request, candidate, other
                ):
                    selected_ids.update((candidate.id, other.id))
    changed = True
    while changed:
        changed = False
        selected = [candidate for candidate in matched if candidate.id in selected_ids]
        for candidate in matched:
            if candidate.id in selected_ids:
                continue
            if any(
                _candidate_shares_coordinated_target_group(
                    positive_request, candidate, other
                )
                for other in selected
            ):
                selected_ids.add(candidate.id)
                changed = True
    if len(matched) > 1 and _comparison_uses_candidate_group(
        positive_request, matched
    ):
        selected_ids.update(candidate.id for candidate in matched)
    return [candidate for candidate in matched if candidate.id in selected_ids]


def _comparison_candidate_mentions(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> list[ProjectCandidate]:
    """Expose candidate tokens in a comparison without relying on Unicode ``\b``."""

    if not _COMPARISON_SIGNALS.search(request):
        return []
    masked = list(request)
    for quoted in _QUOTED_DIRECT_CHILD_NAME.finditer(request):
        masked[quoted.start() : quoted.end()] = " " * (quoted.end() - quoted.start())
    visible = "".join(masked)
    comparison_clause = re.search(
        r"(?:比较|对比)[^,;.!?\r\n]{0,120}|"
        r"\b(?:compare|comparison|differences?|vs\.?|versus)\b"
        r"[^,;.!?\r\n]{0,120}",
        visible,
        re.IGNORECASE,
    )
    if comparison_clause is None:
        return []
    body = comparison_clause.group()
    return [
        candidate
        for candidate in candidates
        if candidate.path != "."
        and not _unsafe_project_name(candidate.path)
        and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(candidate.path)}"
            r"(?![A-Za-z0-9_.-])",
            body,
            re.IGNORECASE,
        )
    ]


def _candidate_shares_coordinated_target_group(
    request: str,
    candidate: ProjectCandidate,
    other: ProjectCandidate,
) -> bool:
    first = re.escape(candidate.path)
    second = re.escape(other.path)
    connector = (
        r"(?:\s*(?:和|与|及|以及|、|或|或者|,|&|\+|"
        r"\band\b|\bplus\b|\bor\b)\s*)"
    )
    target_noun = r"(?:项目|工程|仓库|代码库|projects?|repositories|repos?|codebases?)"
    property_noun = (
        r"(?:(?:的\s*)?(?:性能|架构|依赖|依赖图|测试|覆盖率|代码|配置|实现|行为|"
        r"功能|技术栈|数据库|构建|部署|接口|交互)|"
        r"(?:['\u2019]s\s+)?(?:performance|architecture|dependencies|dependency\s+graph|"
        r"tests?|coverage|code|configuration|implementation|behavior|features?|"
        r"tech(?:nology)?\s+stack|database|build|deployment|interfaces?|interactions?))"
    )
    shared_property_connector = (
        r"(?:\s*(?:和|与|及|以及|、|或|或者|,|&|\+|"
        r"\band\b|\bplus\b|\bor\b)\s*)"
    )
    for left, right in ((first, second), (second, first)):
        if re.search(
            rf"(?<![A-Za-z0-9_.-]){left}{connector}{right}\s*{target_noun}"
            rf"(?=$|\s|的|里|中|[,;.!?\uFF0C\u3002\uFF01\uFF1F])",
            request,
            re.IGNORECASE,
        ) or re.search(
            rf"{target_noun}\s*{left}{connector}{right}(?![A-Za-z0-9_.-])",
            request,
            re.IGNORECASE,
        ) or re.search(
            rf"(?<![A-Za-z0-9_.-]){left}{connector}{right}\s*{property_noun}",
            request,
            re.IGNORECASE,
        ) or re.search(
            rf"(?<![A-Za-z0-9_.-]){left}\s*{property_noun}"
            rf"{shared_property_connector}{right}\s*{property_noun}",
            request,
            re.IGNORECASE,
        ) or re.search(
            rf"(?<![A-Za-z0-9_.-]){left}{connector}{right}(?![A-Za-z0-9_.-])",
            request,
            re.IGNORECASE,
        ):
            return True
    return False


def _comparison_uses_candidate_group(
    request: str,
    candidates: list[ProjectCandidate],
) -> bool:
    if not _COMPARISON_SIGNALS.search(request):
        return False
    residual = request
    for candidate in sorted(candidates, key=lambda item: len(item.path), reverse=True):
        residual = re.sub(re.escape(candidate.path), " ", residual, flags=re.IGNORECASE)
    residual = _COMPARISON_SIGNALS.sub(" ", residual)
    residual = re.sub(
        r"\b(?:the|these|those|either|between|and|or|plus|with|to|projects?|repositories|repos?|"
        r"codebases?|differences?|what|are|is)\b|"
        r"(?:一下|这两个|两个|这些|项目|工程|仓库|代码库|和|与|及|以及|或|或者|、|有什么|什么)",
        " ",
        residual,
        flags=re.IGNORECASE,
    )
    residual = residual.strip(" \t,;:.!?\uFF0C\u3002\uFF01\uFF1F")
    if not residual:
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:的|其|各自|之间|和|与|及|以及|、)\s*)*"
            r"(?:性能|架构|依赖|依赖图|测试|覆盖率|代码|配置|实现|行为|功能|"
            r"技术栈|数据库|构建|部署|接口|交互)|"
            r"(?:(?:of|their|respective|and|plus|['\u2019]s)\s+)*"
            r"(?:performance|architecture|dependencies|dependency\s+graph|tests?|"
            r"coverage|code|configuration|implementation|behavior|features?|"
            r"tech(?:nology)?\s+stack|database|build|deployment|interfaces?|"
            r"interactions?)",
            residual,
            re.IGNORECASE,
        )
    )


def has_execution_intent(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate] = (),
) -> bool:
    """Classify executable intent while masking verified project names.

    The public wrapper lets request routing reuse the established mutation and command
    distinction without treating names such as ``test-runner`` as instructions.
    """

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    intent_compact = _mask_negated_action_clauses(
        _mask_discussed_action_clauses(compact)
    )
    if not intent_compact.strip():
        return False
    if any(
        _candidate_has_targeted_execution_role(intent_compact, candidate)
        for candidate in candidates
    ):
        return True
    explicit = target_role_candidates(intent_compact, candidates)
    switch_masked = _mask_project_switch_clauses(intent_compact, candidates)
    detail_masked = _mask_candidate_nominal_details(switch_masked, candidates)
    intent_request = _mask_candidate_names(detail_masked, explicit)
    if _TARGET_PREFIXED_EXECUTION_SIGNALS.search(intent_request):
        return True
    if _LEADING_READ_ONLY_SIGNALS.search(intent_request) and not any(
        _has_execution_intent(intent_request[separator.start() :])
        for separator in _READ_THEN_ACTION_SEPARATOR.finditer(intent_request)
    ):
        return False
    return _has_execution_intent(intent_request)


def _mask_discussed_action_clauses(request: str) -> str:
    """Blank advisory/informational clauses without weakening later imperatives."""

    masked = list(request)
    for clause in re.finditer(r"[^,;.!?\uFF0C\u3002\uFF01\uFF1F\uFF1B\r\n]+", request):
        text = clause.group()
        if _ADVISORY_ACTION_SIGNALS.search(text) or _INFORMATIONAL_ACTION_QUERY.search(
            text
        ):
            masked[clause.start() : clause.end()] = " " * (clause.end() - clause.start())
    return "".join(masked)


def has_advisory_action_intent(request: str) -> bool:
    """Return whether an action is being discussed as a choice rather than requested."""

    compact = _bounded_intent_request(request)
    return bool(compact and _ADVISORY_ACTION_SIGNALS.search(compact))


def has_read_action_intent(request: str) -> bool:
    """Return whether the request contains a shared read predicate."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and re.search(
            rf"{_CHINESE_READ_ACTION}|\b{_ENGLISH_READ_ACTION}\b|"
            rf"{_OVERVIEW_NOUN_READ_SIGNAL}",
            compact,
            re.IGNORECASE,
        )
    )


def has_overview_read_action_intent(request: str) -> bool:
    """Return whether the request contains a shared overview/explanation predicate."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and re.search(
            rf"{_CHINESE_OVERVIEW_READ_ACTION}|"
            rf"\b{_ENGLISH_OVERVIEW_READ_ACTION}\b|{_OVERVIEW_NOUN_READ_SIGNAL}",
            compact,
            re.IGNORECASE,
        )
    )


def has_inspection_read_action_intent(request: str) -> bool:
    """Return whether the request contains a shared inspect/search predicate."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and re.search(
            rf"{_CHINESE_INSPECTION_READ_ACTION}|"
            rf"\b{_ENGLISH_INSPECTION_READ_ACTION}\b",
            compact,
            re.IGNORECASE,
        )
    )


def mask_execution_action_words(request: str) -> str:
    """Blank shared mutation/operation tokens while preserving offsets and objects."""

    compact = _bounded_intent_request(request)
    masked = list(compact)
    for match in _ACTION_WORD_SIGNALS.finditer(compact):
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def has_diagnostic_action_intent(request: str) -> bool:
    """Return whether the request contains a shared diagnostic predicate."""

    compact = _bounded_intent_request(request)
    return bool(
        compact
        and re.search(
            rf"{_CHINESE_DIAGNOSTIC_READ_ACTION}|"
            rf"\b{_ENGLISH_DIAGNOSTIC_READ_ACTION}\b",
            compact,
            re.IGNORECASE,
        )
    )


def has_informational_action_intent(request: str) -> bool:
    """Return whether an action word describes existing code rather than authorizing work."""

    compact = _bounded_intent_request(request)
    return bool(compact and _INFORMATIONAL_ACTION_QUERY.search(compact))


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
    project_context = _PROJECT_SIGNALS.search(compact)
    project_artifact = _PROJECT_ARTIFACT_SIGNALS.search(compact)
    project_reference = _FOLLOWUP_REFERENCE_SIGNALS.search(compact)
    project_pronoun = _PROJECT_PRONOUN_SIGNALS.search(compact)
    project_detail = _PROJECT_DETAIL_SIGNALS.search(compact)
    possessive_detail = _PROJECT_POSSESSIVE_DETAIL_SIGNALS.search(compact)
    qualified_project_context = _QUALIFIED_PROJECT_CONTEXT_SIGNALS.search(compact)
    if _PURE_PROJECT_CONTINUATION_SIGNALS.fullmatch(compact):
        return True
    # A generic detail/question remains ambiguous even on an adjacent turn.  Inheritance needs
    # an explicit backward reference; a topic word such as "database" or "architecture" is not
    # evidence that the user means the previously selected project.
    if project_reference and (
        project_context
        or project_artifact
        or project_detail
        or possessive_detail
        or qualified_project_context
    ):
        return True
    if project_pronoun and _PROJECT_PRONOUN_FOLLOWUP_SIGNALS.search(compact):
        return True
    return False


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


def has_multiple_project_scope_intent(request: str) -> bool:
    """Return whether a quantified request independently targets several projects."""

    compact = _bounded_intent_request(request)
    return bool(compact and _MULTI_PROJECT_SCOPE_SIGNALS.search(compact))


def has_governed_multiple_project_targets(request: str) -> bool:
    """Recognize quantified project sets only when they fill a target slot."""

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    masked = list(compact)
    for quote in _QUOTED_DIRECT_CHILD_NAME.finditer(compact):
        masked[quote.start() : quote.end()] = " " * (quote.end() - quote.start())
    visible = "".join(masked)
    chinese_set = (
        r"(?:所有|全部|每个|两个|这两个|这些|多个)\s*"
        r"(?:其他|其余)?\s*(?:项目|工程|仓库|代码库)"
    )
    english_set = (
        r"(?:all|every|both|two|these|multiple)\s+(?:(?:other|remaining)\s+)?"
        r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)"
    )
    return bool(
        re.search(
            rf"{_CHINESE_TARGET_ACTION}(?:一下|下)?\s*{chinese_set}|"
            rf"(?:在|遍历|跨)越?\s*{chinese_set}(?:中|里|内|上)?|"
            rf"(?:在|遍历|跨越)\s*(?:整个|当前|这个|该)?"
            rf"工作区(?:的)?\s*{chinese_set}(?:中|里|内|上)?\s*"
            rf"{_CHINESE_TARGET_ACTION}|"
            rf"{_CHINESE_TARGET_ACTION}[^,;.!?\r\n]{{0,48}}"
            rf"(?:和|与|及|以及|、)\s*{chinese_set}|"
            rf"(?:比较|对比)[^,;.!?\r\n]{{0,48}}{chinese_set}|"
            rf"\b{_ENGLISH_TARGET_ACTION}\b\s+(?:the\s+)?{english_set}\b|"
            rf"\b(?:in|across|throughout)\s+(?:the\s+)?{english_set}\b|"
            rf"\b{_ENGLISH_TARGET_ACTION}\b[^,;.!?\r\n]{{0,48}}"
            rf"\b(?:and|plus)\s+(?:the\s+)?{english_set}\b|"
            rf"\bcompare\b[^,;.!?\r\n]{{0,48}}{english_set}\b",
            visible,
            re.IGNORECASE,
        )
    )


def has_governed_workspace_root_target(request: str) -> bool:
    """Recognize workspace-root phrases only when governed as a local target."""

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    masked = list(compact)
    for quote in _QUOTED_DIRECT_CHILD_NAME.finditer(compact):
        masked[quote.start() : quote.end()] = " " * (quote.end() - quote.start())
    visible = "".join(masked)
    chinese_root = (
        r"(?:(?:这个|该|当前|本|整个|全部)?(?:工作区|目录)|"
        r"工作区根目录)"
    )
    english_root = (
        r"(?:(?:(?:this|the\s+current|current|local|whole|entire)\s+)?"
        r"(?:workspace|directory)|(?:the\s+)?workspace\s+root)"
    )
    return bool(
        re.search(
            rf"{_CHINESE_TARGET_ACTION}(?:一下|下)?\s*{chinese_root}|"
            rf"(?:在|遍历|跨越)\s*{chinese_root}(?:中|里|内|上)?|"
            rf"\b{_ENGLISH_TARGET_ACTION}\b\s+(?:the\s+)?{english_root}\b|"
            rf"\b(?:in|inside|within|across|throughout)\s+(?:the\s+)?"
            rf"{english_root}\b",
            visible,
            re.IGNORECASE,
        )
    )


def has_other_project_target_intent(request: str) -> bool:
    """Recognize a governed singular other-project target, not a general concept."""

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    masked = list(compact)
    for quote in _QUOTED_DIRECT_CHILD_NAME.finditer(compact):
        masked[quote.start() : quote.end()] = " " * (quote.end() - quote.start())
    visible = "".join(masked)
    chinese_other = r"(?:另一个|另一|别的|其他|其它)(?:项目|工程|仓库|代码库)"
    english_other = (
        r"(?:another|the\s+other)\s+"
        r"(?:project|repository|repo|codebase)"
    )
    return bool(
        re.fullmatch(
            rf"\s*(?:{chinese_other}|{english_other}|another\s+one)\s*"
            r"[.!?\u3002\uFF01\uFF1F]?\s*",
            visible,
            re.IGNORECASE,
        )
        or re.search(
            rf"{_CHINESE_TARGET_ACTION}(?:一下|下)?\s*{chinese_other}|"
            rf"(?:切换|换到|选择)\s*{chinese_other}|"
            rf"\b{_ENGLISH_TARGET_ACTION}\b\s+(?:the\s+)?{english_other}\b|"
            rf"\b(?:switch|change|select|choose|pick)\s+(?:to\s+)?"
            rf"(?:the\s+)?{english_other}\b",
            visible,
            re.IGNORECASE,
        )
    )


def has_mixed_current_other_project_targets(request: str) -> bool:
    """Return whether adjacent/current and other project references share one request."""

    compact = _bounded_intent_request(request)
    return bool(compact and _MIXED_CURRENT_OTHER_SCOPE_SIGNALS.search(compact))


def has_mixed_adjacent_explicit_project_targets(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> bool:
    """Recognize one adjacent anaphoric target combined with a verified target."""

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    # A candidate coordinated with an anaphor is itself a target even when the name does not
    # repeat the governing predicate ("inspect this project and beta").  Accept either a normal
    # target slot or a verified lexical occurrence here; the anaphor + governing relation below
    # supplies the otherwise missing role evidence.
    if not (
        target_role_candidates(compact, candidates)
        or matching_candidates(compact, candidates)
    ):
        return False
    anaphor = re.search(
        r"(?:这个|该|当前|同一个)(?:项目|工程|仓库|代码库)|"
        r"它(?:的)?|"
        r"\b(?:this|current|same)\s+(?:project|repository|repo|codebase)\b|"
        r"\bits?\b",
        compact,
        re.IGNORECASE,
    )
    if anaphor is None:
        return False
    return bool(
        _COMPARISON_SIGNALS.search(compact)
        or re.search(
            r"(?:和|与|及|以及|、|从|到|复制|移动|先|再|然后|接着)|"
            r"\b(?:and|plus|with|to|from|copy|move|then|before|after)\b",
            compact,
            re.IGNORECASE,
        )
    )


def has_ambiguous_project_semantic_subject_intent(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> bool:
    """Recognize a bare candidate in project-semantic syntax without selecting it."""

    compact = _bounded_intent_request(request)
    if not compact:
        return False
    masked = list(compact)
    for quoted in _QUOTED_DIRECT_CHILD_NAME.finditer(compact):
        masked[quoted.start() : quoted.end()] = " " * (quoted.end() - quoted.start())
    visible = "".join(masked)
    for candidate in candidates:
        if candidate.path == "." or _unsafe_project_name(candidate.path):
            continue
        name = re.escape(candidate.path)
        if re.search(
            rf"(?<![A-Za-z0-9_.-]){name}\s*(?:是做什么的|能做什么|如何工作|"
            r"怎么工作|支持哪些功能|怎么组织|如何组织)|"
            rf"\b(?:what\s+(?:does|can)|how\s+(?:does|is))\s+{name}\s+"
            r"(?:do|work|organized)\b",
            visible,
            re.IGNORECASE,
        ):
            return True
    return False


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
    # CJK target connectors are Unicode word characters, so ``isalnum`` alone would make a
    # no-space target list such as ``alpha或beta`` look like one identifier.  Treat only the
    # grammar's coordination punctuation as a boundary; arbitrary adjacent CJK text still cannot
    # turn a candidate substring into a project selection.
    target_connectors = frozenset("或和与及、\uff0c\uff1b")
    has_initial_boundary = not (
        _word_character(before) and _word_character(request[start])
    ) or before in target_connectors
    has_terminal_boundary = not (
        _word_character(after) and _word_character(request[end - 1])
    ) or after in target_connectors
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
            has_boundary
            or quoted
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


def _mask_candidate_nominal_details(
    request: str,
    candidates: tuple[ProjectCandidate, ...] | list[ProjectCandidate],
) -> str:
    """Mask verified component-like names in read-only coordinated noun phrases."""

    masked = list(request)
    property_noun = (
        r"(?:性能|架构|依赖|依赖图|测试|覆盖率|代码|配置|实现|行为|功能|技术栈|"
        r"数据库|构建|部署|接口|交互|performance|architecture|dependencies|"
        r"dependency\s+graph|tests?|coverage|code|configuration|implementation|"
        r"behavior|features?|tech(?:nology)?\s+stack|database|build|deployment|"
        r"interfaces?|interactions?)"
    )
    for candidate in candidates:
        name = candidate.path
        if (
            candidate.path == "."
            or _unsafe_project_name(name)
            or _STRONG_MUTATION_SIGNALS.fullmatch(name)
            or _CONTEXTUAL_MUTATION_SIGNALS.fullmatch(name)
            or _OPERATION_SIGNALS.fullmatch(name)
        ):
            continue
        pattern = re.compile(
            rf"(?:和|与|及|以及|、|\band\b|\bplus\b)\s*"
            rf"(?P<name>{re.escape(name)})(?:\s*的|['\u2019]s)?\s+{property_noun}",
            re.IGNORECASE,
        )
        for match in pattern.finditer(request):
            start, end = match.span("name")
            masked[start:end] = " " * (end - start)
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
    request = _NEGATED_EXECUTION_SIGNALS.sub(" ", request)
    if _CHAINED_INFLECTED_ACTION_SIGNALS.search(request):
        return True
    strong_mutation_frame = bool(
        _CHAINED_MUTATION_SIGNALS.search(request)
        or _LEADING_MUTATION_SIGNALS.search(request)
        or _PUNCTUATED_MUTATION_SIGNALS.search(request)
        or _IMPERATIVE_MUTATION_SIGNALS.search(request)
    )
    if (
        _STRONG_MUTATION_SIGNALS.search(request)
        and strong_mutation_frame
        and (
            _CHAINED_MUTATION_SIGNALS.search(request)
            or not _EXPLANATORY_MUTATION_SIGNALS.search(request)
        )
    ):
        return True
    if _CONTEXTUAL_MUTATION_SIGNALS.search(request) and (
        _CHAINED_MUTATION_SIGNALS.search(request)
        or _LEADING_MUTATION_SIGNALS.search(request)
        or _PUNCTUATED_MUTATION_SIGNALS.search(request)
        or _IMPERATIVE_MUTATION_SIGNALS.search(request)
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
    if (
        _EXPLANATORY_OPERATION_SIGNALS.search(request)
        and not _CHAINED_OPERATION_SIGNALS.search(request)
        and not _PUNCTUATED_OPERATION_SIGNALS.search(request)
    ):
        return False
    return bool(
        _CHAINED_OPERATION_SIGNALS.search(request)
        or _LEADING_OPERATION_SIGNALS.search(request)
        or _PUNCTUATED_OPERATION_SIGNALS.search(request)
        or _IMPERATIVE_OPERATION_SIGNALS.search(request)
    )


def _candidate_name_is_action(name: str) -> bool:
    return bool(
        _STRONG_MUTATION_SIGNALS.fullmatch(name)
        or _CONTEXTUAL_MUTATION_SIGNALS.fullmatch(name)
        or _OPERATION_SIGNALS.fullmatch(name)
    )


def _candidate_has_weak_subject_collision(name: str) -> bool:
    """Return whether a bare name is also shared action/object vocabulary."""

    return bool(
        name.casefold() in _DOMAIN_COLLISION_CANDIDATE_NAMES
        or _candidate_name_is_action(name)
        or re.fullmatch(_GENERIC_PROJECT_OBJECT_NAME, name, re.IGNORECASE)
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
    strong_names: list[str] = []
    stripped_request = request.strip()
    for match in _QUOTED_DIRECT_CHILD_NAME.finditer(request):
        name = next(group for group in match.groups() if group is not None)
        if _quoted_target_has_project_cue(
            request, match.start(), match.end()
        ) or re.fullmatch(
            rf"[\"'`“「]{re.escape(name)}[\"'`”」]",
            stripped_request,
        ):
            strong_names.append(name.strip())

    direct_name = _ASCII_DIRECT_CHILD_NAME
    cjk_name = r"[一-鿿][一-鿿0-9_.+-]{0,23}"
    target_connector = r"(?:和|与|及|以及|、|或|或者|要么|&|\+|and|plus|or)"
    strong_slot_patterns = (
        re.compile(
            rf"(?:在|对)\s*(?:项目\s*)?(?P<name>{direct_name}|{cjk_name})"
            r"(?=$|\s|的|中|里|内|上|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"\b(?:in|inside|under|within|into|onto)\s+(?:the\s+)?"
            rf"(?:project\s+)?(?P<english_name>{direct_name})\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{_CHINESE_ACTION}[^,;.!?\r\n]{{0,48}}(?:到|至|进入)\s*"
            rf"(?:项目\s*)?(?P<name>{direct_name}|{cjk_name})"
            r"(?=$|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"\b{_ENGLISH_ACTION}\b[^,;.!?\r\n]{{0,64}}"
            rf"\b(?:to|into|onto)\s+(?:the\s+)?(?:project\s+)?"
            rf"(?P<english_name>{direct_name})(?=\s*(?:$|[,;.!?]))",
            re.IGNORECASE,
        ),
    )
    for pattern in strong_slot_patterns:
        for match in pattern.finditer(request):
            name = next(group for group in match.groups() if group is not None)
            strong_names.append(name)

    weak_patterns = (
        re.compile(
            rf"(?:{_CHINESE_READ_ACTION}|比较|对比)(?:一下|下)?\s*"
            r"(?:项目\s*)?(?P<first_name>[一-鿿][一-鿿0-9_.+-]{0,23}?)"
            r"\s*(?:和|与|及|以及|、|或|或者)\s*"
            r"(?P<second_name>[一-鿿][一-鿿0-9_.+-]{0,23})(?="
            rf"\s*(?:的\s*)?{_CHINESE_PROPERTY_OBJECT}"
            r"\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]))",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{_CHINESE_READ_ACTION}(?:一下|下)?\s*(?:项目\s*)?"
            rf"(?P<name>{direct_name})(?=\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]|"
            rf"{target_connector}))|"
            rf"{_CHINESE_ACTION}\s*(?:项目\s*)?(?P<action_name>{direct_name})"
            rf"(?=\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]|{target_connector}|"
            rf"(?:的\s*)?{_CHINESE_PROJECT_OBJECT}))",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b{_ENGLISH_READ_ACTION}\s+(?:either\s+)?(?:the\s+)?"
            rf"(?P<property_name>{direct_name})(?=\s+{_ENGLISH_PROJECT_OBJECT}"
            rf"\s*(?:$|[,;.!?]|{target_connector}))|"
            rf"\b{_ENGLISH_READ_ACTION}\s+(?:either\s+)?(?:the\s+)?"
            rf"(?P<name>{direct_name})(?=\s*(?:$|[,;.!?]|{target_connector}))|"
            rf"\b{_ENGLISH_ACTION}\s+(?:either\s+)?(?:the\s+)?"
            rf"(?P<action_name>{direct_name})(?=\s*(?:$|[,;.!?]|{target_connector}|"
            rf"{_ENGLISH_PROJECT_OBJECT}))|"
            rf"\b{_ENGLISH_ACTION}\b[^,;.!?\r\n]{{0,64}}"
            rf"\b(?:to|into|onto)\s+(?:the\s+)?(?:project\s+)?"
            rf"(?P<destination_name>{direct_name})(?=\s*(?:$|[,;.!?]|"
            rf"{target_connector}))",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:在|对)\s*(?:项目\s*)?(?P<name>{direct_name})"
            r"(?=$|\s|的|中|里|内|上|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"\b(?:in|inside|under|within|into|onto)\s+(?:the\s+)?"
            rf"(?:project\s+)?(?P<english_name>{direct_name})\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:在|对)\s*(?:项目\s*)?"
            r"(?P<name>[一-鿿][一-鿿0-9_.+-]{0,23}?)(?:中|里|内|上)|"
            rf"{_CHINESE_READ_ACTION}(?:一下|下)?\s*(?:项目\s*)?"
            r"(?P<possessive_name>[一-鿿][一-鿿0-9_.+-]{0,23}?)(?="
            rf"的\s*{_CHINESE_PROJECT_OBJECT})|"
            rf"{_CHINESE_READ_ACTION}(?:一下|下)?\s*(?:项目\s*)?"
            r"(?P<read_object_name>[一-鿿][一-鿿0-9_.+-]{0,23})(?="
            rf"\s*(?:的\s*)?{_CHINESE_PROPERTY_OBJECT}"
            r"\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]))|"
            rf"{_CHINESE_READ_ACTION}(?:一下|下)?\s*(?:项目\s*)?"
            r"(?P<chinese_name>[一-鿿][一-鿿0-9_.+-]{0,23}?)(?="
            r"$|[,;.!?\uFF0C\u3002\uFF01\uFF1F])|"
            rf"{_CHINESE_ACTION}\s*(?:项目\s*)?"
            r"(?P<action_object_name>[一-鿿][一-鿿0-9_.+-]{0,23})(?="
            rf"\s*(?:的\s*)?{_CHINESE_ACTION_TARGET_OBJECT}"
            r"\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]))|"
            rf"{_CHINESE_ACTION}\s*(?:项目\s*)?"
            r"(?P<direct_action_name>[一-鿿][一-鿿0-9_.+-]{0,23}?)(?="
            rf"\s*(?:$|[,;.!?\uFF0C\u3002\uFF01\uFF1F]|{target_connector}))|"
            rf"{_CHINESE_ACTION}[^,;.!?\r\n]{{0,48}}(?:到|至|进入)\s*"
            r"(?:项目\s*)?(?P<destination_name>[一-鿿]"
            r"[一-鿿0-9_.+-]{0,23})(?=$|[,;.!?\uFF0C\u3002\uFF01\uFF1F])",
            re.IGNORECASE,
        ),
    )
    weak_names: list[str] = []
    for pattern in weak_patterns:
        for match in pattern.finditer(request):
            weak_names.extend(group for group in match.groups() if group is not None)

    strong_patterns = (
        re.compile(
            rf"(?P<name>{direct_name})\s+(?:project|repository|repo|directory)\b|"
            rf"\b(?:project|repository|repo|directory)\s+(?:named|called)\s+"
            rf"(?P<named>{direct_name})\b|"
            rf"\b(?:switch|change)\s+to\s+(?:the\s+)?(?:project\s+)?"
            rf"(?P<switched>{direct_name})\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<name>{direct_name}|[一-鿿][一-鿿0-9_.+-]{{0,23}}?)\s*"
            r"(?:项目|工程|仓库|目录)|"
            rf"(?:项目|工程|仓库|目录)\s*(?:名为|叫)\s*"
            rf"(?P<named>{direct_name}|[一-鿿][一-鿿0-9_.+-]{{0,23}})|"
            rf"(?:切换到|换成|换到)\s*(?:项目\s*)?"
            rf"(?P<switched>{direct_name}|[一-鿿][一-鿿0-9_.+-]{{0,23}})",
            re.IGNORECASE,
        ),
    )
    for pattern in strong_patterns:
        for match in pattern.finditer(request):
            name = next(group for group in match.groups() if group is not None)
            lowered = name.casefold()
            grammar_word = bool(
                re.fullmatch(_ENGLISH_READ_ACTION, name, re.IGNORECASE)
                or re.fullmatch(_CHINESE_READ_ACTION, name, re.IGNORECASE)
            )
            if (
                lowered not in _GENERIC_CANDIDATE_NAMES
                and lowered not in {"for", "from", "in", "into", "on", "to"}
                and not grammar_word
            ):
                strong_names.append(name)

    weak_stopwords = _TARGET_NAME_STOPWORDS | _DOMAIN_COLLISION_CANDIDATE_NAMES | {
        "all",
        "code",
        "dependencies",
        "dependency",
        "entire",
        "for",
        "in",
        "service",
        "test",
        "tests",
        "代码",
        "依赖",
        "架构",
        "登录",
        "配置",
        "测试",
    }
    names = [
        *strong_names,
        *(
            name
            for name in weak_names
            if name.casefold() not in weak_stopwords
            and not _candidate_name_is_action(name)
        ),
    ]
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
            r"(?:项目|工程|仓库|目录)\s*(?:名为|叫)|"
            r"(?:project|repository|repo|directory)\s+(?:named|called)|"
            r"(?:介绍(?:项目|工程|仓库|目录)?|切换到|换成|"
            r"(?:switch|change)\s+to|project|repository|repo|directory)"
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
