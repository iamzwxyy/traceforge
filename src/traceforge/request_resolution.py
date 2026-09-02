from __future__ import annotations

import re
from collections.abc import Sequence
from itertools import pairwise
from typing import Literal

from traceforge.models import ProjectCandidate, ProjectTarget, RequestResolution
from traceforge.project_scope import (
    has_advisory_action_intent,
    has_ambiguous_project_semantic_subject_intent,
    has_diagnostic_action_intent,
    has_execution_intent,
    has_governed_multiple_project_targets,
    has_governed_workspace_root_target,
    has_informational_action_intent,
    has_inspection_read_action_intent,
    has_mixed_adjacent_explicit_project_targets,
    has_mixed_current_other_project_targets,
    has_other_project_target_intent,
    has_overview_read_action_intent,
    has_read_action_intent,
    is_project_overview_request,
    is_project_scope_followup_request,
    mask_execution_action_words,
    matching_candidates,
    negated_project_switch_candidates,
    target_role_candidates,
)

_NON_WORKSPACE_CONVERSATION = re.compile(
    r"^\s*(?:"
    r"(?:你好|您好|嗨|哈喽|谢谢|感谢|再见|早上好|下午好|晚上好)"
    r"[?\uff1f!\uff01\u3002,.\uff0c\s]*|"
    r"(?:你是谁|你能做什么|你会做什么|你支持什么|有哪些能力|如何使用你|怎么使用你)"
    r"[?\uff1f!\uff01\u3002\s]*|"
    r"(?:(?:hi|hello|hey|thanks|thank\s+you|goodbye|good\s+(?:morning|afternoon|evening))"
    r"[!,.?\s]*|"
    r"(?:who\s+are\s+you|what\s+can\s+you\s+do|what\s+are\s+your\s+capabilities|"
    r"how\s+do\s+i\s+use\s+you)[?!.\s]*)"
    r")\s*$",
    re.IGNORECASE,
)
_CONTENT_LITERAL = re.compile(
    r"(?:\b(?:contain(?:s|ing)?|say(?:s|ing)?|reads?|literal|term|string|snippet|"
    r"quoted\s+text)\b|(?:包含|内容为|写着|显示|字符串|术语|原文|片段))"
    r"[^\r\n]{0,32}?(?P<literal>\"[^\"\r\n]{1,2000}\"|"
    r"“[^”\r\n]{1,2000}”|`[^`\r\n]{1,2000}`)",
    re.IGNORECASE,
)
_WORKSPACE_ANCHOR = re.compile(
    r"项目|工程|仓库|代码库|工作区|当前目录|这个目录|该目录|"
    r"\b(?:projects?|repositor(?:y|ies)|repos?|codebases?|workspaces?|"
    r"current\s+directory)\b",
    re.IGNORECASE,
)
_LOCAL_WORKSPACE_REFERENCE = re.compile(
    r"(?:这个|该|当前|本|上述|上面|刚才)(?:项目|工程|仓库|代码库)|"
    r"(?:整个|当前|这个|该|本|上述)(?:工作区|目录)|"
    r"\b(?:this|that|current|local|previous|above)\s+"
    r"(?:project|repository|repo|codebase|workspace|directory)\b|"
    r"\bcurrent\s+(?:working\s+)?directory\b",
    re.IGNORECASE,
)
_LOCAL_PROJECT_REFERENCE = re.compile(
    r"(?:这个|该|当前|本|上述|上面|刚才)"
    r"(?:[A-Za-z0-9_.+-]+|[一-鿿]{1,12})?\s*(?:项目|工程|仓库|代码库)|"
    r"\b(?:this|that|current|local|previous|above)\s+"
    r"(?:(?:[A-Za-z0-9_.+-]+)\s+){0,3}(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_WORKSPACE_READ_INTENT = re.compile(
    r"(?:检查|查看|检索|搜索|查找|扫描|审查|阅读|列出|梳理|定位|分析|排查)"
    r"(?:一下|下)?\s*(?:这个|该|当前)?\s*"
    r"[^\r\n]{0,24}?"
    r"(?:项目|工程|仓库|代码库|工作区|目录|依赖|代码|源码|文件|实现|逻辑|流程|"
    r"调用链|定义|引用|测试|配置|README|清单|入口)|"
    r"(?:依赖|代码|源码|文件|实现|逻辑|流程|调用链|定义|引用|测试|配置|README|"
    r"清单|入口)\s*"
    r"(?:检查|查看|检索|搜索|查找|扫描|审查|分析)|"
    r"\b(?:inspect|check|review|search|find|scan|read|list|analy[sz]e|locate|audit)\b"
    r"[^\r\n]{0,60}\b(?:projects?|repositor(?:y|ies)|repos?|codebases?|workspaces?|"
    r"directories|dependencies|source\s+code|code|files?|implementations?|tests?|"
    r"configurations?|configs?|readmes?|manifests?|entry\s+points?|flows?|logic|"
    r"definitions?|references?)\b",
    re.IGNORECASE,
)
_CONCRETE_WORKSPACE_SUBJECT = re.compile(
    r"项目|工程|仓库|代码库|依赖(?!注入|倒置)|"
    r"(?:源)?代码(?!\s*(?:示例|例子|样例|片段))|源码|文件|实现|逻辑|流程|调用链|"
    r"定义|引用|测试|配置|README|清单|入口|构建|编译|部署|服务|应用|程序(?!员)|脚本|模块|"
    r"函数|接口|端点|页面|功能|缺陷|漏洞|错误|问题|数据库表|数据表|模式|迁移|"
    r"\b(?:projects?|repositor(?:y|ies)|repos?|codebases?|dependencies|"
    r"source\s+code|code(?!\s+(?:examples?|samples?|snippets?))|files?|"
    r"implementations?|logic|flows?|call\s+chains?|definitions?|references?|"
    r"tests?|configurations?|configs?|readmes?|manifests?|entry\s+points?|"
    r"builds?|compilation|deployments?|services?|apps?|applications?|"
    r"programs?|scripts?|modules?|"
    r"functions?|APIs?|endpoints?|pages?|features?|bugs?|errors?|issues?|"
    r"database\s+tables?|"
    r"schemas?|migrations?)\b",
    re.IGNORECASE,
)
_DEFINITE_EXPLANATION_SUBJECT = re.compile(
    r"(?:这段|这份|这个|该|当前|本项目的)\s*"
    r"(?:代码|源码|文件|实现|逻辑|流程|调用链|配置|测试|依赖)|"
    r"^\s*(?:请|请你|帮我)?\s*(?:解释|说明|讲解)(?:一下|下)?\s*"
    r"(?:代码|源码)(?:\s*[.!?\u3002\uFF01\uFF1F])?\s*$|"
    r"\b(?:the|this|that|current|project['\u2019]s)\s+"
    r"(?:code|source\s+code|files?|implementation|logic|flow|call\s+chain|"
    r"configuration|config|tests?|dependencies)\b",
    re.IGNORECASE,
)
_DIAGNOSTIC_STATE = re.compile(
    r"失败|报错|错误|异常|没通过|未通过|不工作|卡住|超时|很慢|"
    r"\b(?:fail(?:ed|ing)?|errors?|broken|not\s+working|stuck|tim(?:ed|ing)\s+out|"
    r"too\s+slow)\b",
    re.IGNORECASE,
)
_DIAGNOSTIC_SUBJECT = re.compile(
    r"测试|构建|编译|安装|部署|启动|命令|代码|服务|应用|"
    r"\b(?:tests?|builds?|compilation|installation|deployments?|startup|commands?|"
    r"code|services?|apps?|applications?)\b",
    re.IGNORECASE,
)
_EXTERNAL_KNOWLEDGE_CONTEXT = re.compile(
    r"示例|例子|样例|教程|最佳实践|通用概念|一般来说|一般|通常|一般会|通常会|"
    r"\b(?:examples?|samples?|tutorials?|best\s+practices|concepts?|in\s+general|"
    r"usually|typically)\b",
    re.IGNORECASE,
)
_PROJECT_TOUR = re.compile(
    r"(?:带我|给我)(?:浏览|导览)(?:一下)?(?:这个|该|当前)?(?:项目|工程|仓库|代码库)|"
    r"\b(?:give|take)\s+me\s+(?:on\s+)?a\s+tour\s+of\s+(?:the\s+)?"
    r"(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_LOCATION_QUERY_SIGNAL = re.compile(
    r"哪里|哪儿|在哪|在哪里|位置|\bwhere\b",
    re.IGNORECASE,
)
_LOCAL_LOCATION_SUBJECT = re.compile(
    r"(?:项目|工程|仓库|代码库)(?:里|中|内)|代码入口|入口文件|测试(?:目录|文件)?|"
    r"模块|实现|定义|配置|"
    r"\b(?:the|this|that|current)\s+(?:tests?|files?|code|entry\s+point|modules?|"
    r"implementation|definition|configuration)\b|"
    r"\b(?:implemented|defined|located|declared|live)\b",
    re.IGNORECASE,
)
_CONTAINMENT_QUERY = re.compile(
    r"(?:项目|工程|仓库|代码库)(?:里|中|内)(?:有|包含)(?:哪些|什么)"
    r"(?:模块|文件|目录|测试|服务|应用|功能)",
    re.IGNORECASE,
)
_WORKSPACE_ARTIFACT = re.compile(
    r"(?:^|[\s`'\"])(?:README(?:\.[A-Za-z0-9]+)?|package\.json|pyproject\.toml|"
    r"go\.mod|Cargo\.toml|pom\.xml|build\.gradle(?:\.kts)?|CMakeLists\.txt)"
    r"(?:$|[\s`'\",.!?\uff0c\u3002\uff01\uff1f])|"
    r"(?:^|[\s`'\"])(?:src|app|lib|tests?)/[A-Za-z0-9_.\-/]+",
    re.IGNORECASE,
)
_LOCAL_FILE_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_@:/.-])(?:\./)?(?:[A-Za-z0-9_.-]+/){0,8}"
    r"(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*\."
    r"(?:c|cc|cfg|conf|cpp|css|csv|dart|ex|exs|go|gradle|graphql|h|hpp|html|ini|"
    r"java|js|json|jsx|kt|kts|lock|md|php|proto|py|rb|rs|rst|sh|sql|swift|toml|"
    r"ts|tsx|tsv|txt|xml|ya?ml|zsh)|CMakeLists\.txt|Dockerfile|Gemfile|Makefile)"
    r"(?![A-Za-z0-9_@.-])",
    re.IGNORECASE,
)
_ROOT_RELATIVE_REFERENCE = re.compile(
    r"(?:^|[\s`'\"])(?:\./)(?![./])(?=[A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_FILE_CONCEPT_CONTEXT = re.compile(
    r"语法|格式|规范|文件类型|扩展名|用途|一般用来|通常用来|"
    r"\b(?:syntax|format|specification|file\s+type|extension|convention|"
    r"used\s+for|typically\s+used|examples?|best\s+practices)\b",
    re.IGNORECASE,
)
_FILE_CONTENT_QUERY = re.compile(
    r"写了什么|包含什么|内容是什么|里面有什么|中有什么|"
    r"\bwhat\b[^\r\n]{0,32}\b(?:contains?|inside|in)\b|"
    r"\b(?:contains?|contents?|inside)\b",
    re.IGNORECASE,
)
_INLINE_SNIPPET = re.compile(
    r"```[\s\S]{1,12000}?```|"
    r"(?:这段|下面的|以下)(?:代码|片段)|"
    r"\b(?:this|following|below)\s+(?:code|snippet)\b[^\r\n]{0,80}`[^`\r\n]+`",
    re.IGNORECASE,
)
_CONTENT_CREATION_ACTION = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?"
    r"(?:写|创作|创建|新建|生成)|"
    r"^\s*(?:please\s+)?(?:write|compose|create|generate)\b",
    re.IGNORECASE,
)
_RESPONSE_OUTPUT_OBJECT = re.compile(
    r"项目(?:总结|概述|概览|报告|表格)|"
    r"(?:总结|概述|概览|报告|表格)[^\r\n]{0,40}(?:项目|工程|仓库|代码库)|"
    r"关于(?:这个|该|当前)?(?:项目|工程|仓库|代码库)的?"
    r"(?:总结|概述|概览|报告|表格)|"
    r"\b(?:project|repository|repo|codebase)\s+"
    r"(?:summary|overview|report|table)\b|"
    r"\b(?:summary|overview|report|table)\b[^\r\n]{0,48}"
    r"\b(?:of|about|for)\s+(?:the\s+|this\s+|that\s+|current\s+)?"
    r"(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_RESPONSE_OUTPUT_NOUN = re.compile(
    r"总结|概述|概览|报告|表格|\b(?:summary|overview|report|table)\b",
    re.IGNORECASE,
)
_FILESYSTEM_OUTPUT_DESTINATION = re.compile(
    r"保存到|写入|落盘|输出到|(?:文件|文档|页面)中|"
    r"(?:报告|总结|概述|概览)(?:文件|文档|页面)|"
    r"\b(?:save|store|persist|write)\b[^\r\n]{0,48}\b(?:to|into|as)\b|"
    r"\b(?:report|summary|overview)\s+(?:file|document|page)\b|"
    r"\b(?:file|document|page)\s+(?:for|with)\s+(?:the\s+)?"
    r"(?:report|summary|overview)\b",
    re.IGNORECASE,
)
_PROVIDED_TEXT_CORRECTION = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?"
    r"(?:修复|修正|改正)\s*(?:这个|该|下面)?\s*(?:句子|文本|文案)"
    r"(?:中|里的)?\s*(?:的)?(?:错字|拼写错误|语法错误)"
    r"\s*[.!?\u3002\uFF01\uFF1F]?\s*$|"
    r"^\s*(?:please\s+)?(?:fix|correct)\s+(?:a|the)?\s*"
    r"(?:typo|spelling|grammar)(?:\s+(?:error|mistake))?\s+"
    r"in\s+(?:this|the)\s+(?:sentence|text|copy)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_WHOLE_WORKSPACE_TARGET = re.compile(
    r"整个工作区|工作区整体|工作区根目录|(?:当前|这个|该|本|本地)(?:工作区|目录)|"
    r"全工作区|跨工作区|工作区范围内|当前工作目录|"
    r"\b(?:whole|entire|current|this|that|local)\s+workspace\b|"
    r"\b(?:the\s+)?workspace\s+root\b|"
    r"\b(?:current|this|that|local)\s+(?:working\s+)?directory\b|"
    r"\b(?:across|throughout)\s+the\s+workspace\b",
    re.IGNORECASE,
)
_MIXED_WORKSPACE_TARGET = re.compile(
    r"(?:比较|对比)[^\r\n]{0,120}(?:整个|当前|这个|该|本地)?(?:工作区|根目录)|"
    r"\bcompare\b[^\r\n]{0,120}\b(?:the\s+)?(?:whole\s+|entire\s+|current\s+)?"
    r"(?:workspace|workspace\s+root)\b|"
    r"(?:和|与|及|以及|、|&|\+)\s*(?:在|到|对)?\s*"
    r"(?:整个|当前|这个|该|本地)?(?:工作区|工作区根目录|根目录)|"
    r"\b(?:and|plus|with)\s+(?:(?:at|in|on|to)\s+)?(?:the\s+)?"
    r"(?:workspace\s+root|whole\s+workspace|entire\s+workspace)\b",
    re.IGNORECASE,
)
_WORKSPACE_CONTAINER_QUERY = re.compile(
    r"(?:列出|查看|展示|统计|盘点)(?:一下|下)?\s*"
    r"(?:当前|这个|该|整个)?(?:工作区|目录)(?:下|里|中)?\s*"
    r"(?:的)?(?:所有|全部|哪些|项目列表)?\s*(?:项目|工程|仓库)|"
    r"(?:当前|这个|该|整个)?(?:工作区|目录)(?:下|里|中)?"
    r"(?:有|包含)(?:哪些|什么|多少)(?:项目|工程|仓库)|"
    r"\b(?:list|show|enumerate|inventory|count)\b[^\r\n]{0,40}"
    r"\b(?:projects?|repositor(?:y|ies)|repos?)\b[^\r\n]{0,30}"
    r"\b(?:workspace|directory)\b|"
    r"\b(?:what|which|how\s+many)\s+(?:projects?|repositor(?:y|ies)|repos?)\b"
    r"[^\r\n]{0,30}\b(?:workspace|directory)\b",
    re.IGNORECASE,
)
_GREENFIELD_WORKSPACE_TARGET = re.compile(
    r"(?:创建|新建|搭建|初始化|生成)\s*(?:一个|一份|新的?)?\s*"
    r"(?:新\s*)?(?:项目|工程|仓库|代码库|应用|服务)|"
    r"\b(?:create|scaffold|initialize|bootstrap|generate|build)\b"
    r"[^\r\n]{0,24}\b(?:a|an)\s+new\s+"
    r"(?:project|repository|repo|codebase|app|application|service)\b",
    re.IGNORECASE,
)
_RAW_COMMAND_TARGET = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦|现在|直接)\s*)?"
    r"(?:运行|执行)\s*`?[A-Za-z0-9_./-]+(?:\s+[^\r\n]{0,160})?`?\s*$|"
    r"^\s*(?:please\s+)?(?:run|execute)\s+"
    r"(?!(?:the\s+)?(?:tests?|build|project|app|service|dependencies)\b)"
    r"`?[A-Za-z0-9_./-]+"
    r"(?:\s+[^\r\n]{0,160})?`?\s*$",
    re.IGNORECASE,
)
_MULTIPLE_TARGET = re.compile(
    r"(?:所有|全部|每个|多个|这些|这两个|两个)\s*"
    r"(?:(?:其他|其它|其余|剩余)\s*)?(?:项目|工程|仓库|代码库)|"
    r"项目\s*之间|(?:比较|对比)(?:一下)?\s*(?:这些|两个|多个)?\s*项目|"
    r"\b(?:all|every|multiple|these|both|two)\s+"
    r"(?:(?:other|remaining)\s+)?"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b|"
    r"\bbetween\s+(?:these|the|two)\s+"
    r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b",
    re.IGNORECASE,
)
_OTHER_TARGET = re.compile(
    r"(?:换个|换一个|换到另一个|切换到另一个|其他|其它|别的|另一个|另一)\s*"
    r"(?:项目|工程|仓库|代码库)|切换项目|"
    r"\b(?:other|another)\s+(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b|"
    r"\banother\s+one\b|\b(?:switch\s+projects?|pick\s+another\s+project)\b",
    re.IGNORECASE,
)
_PROJECT_DEICTIC_REFERENCE = re.compile(
    r"(?:这个|该|当前|刚才|上面|上述|同一个|同一|同个|同样的|"
    r"相同的?|刚才那个)"
    r"(?:[A-Za-z0-9_.+-]+|[一-鿿]{1,12})?\s*(?:项目|工程|仓库|代码库)|"
    r"\b(?:this|that|current|previous|above|the\s+same|same)\s+"
    r"(?:(?:[A-Za-z0-9_.+-]+)\s+){0,3}(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_PROJECT_PRONOUN_DETAIL = re.compile(
    r"它(?:的|里|中|用|有|是)[^\r\n]{0,40}"
    r"(?:依赖|代码|源码|文件|实现|测试|配置|架构|技术栈|入口|功能|模块|目录结构)|"
    r"(?:修复|修改|检查|查看|搜索|测试|构建|部署|运行|启动)[^\r\n]{0,24}它|"
    r"\bits\s+(?:dependencies|code|files?|implementation|tests?|configuration|"
    r"architecture|stack|entry\s+point|features?)\b|"
    r"\b(?:fix|change|inspect|check|review|search|test|build|deploy|run|start)"
    r"(?:\s+\w+){0,4}\s+it\b|"
    r"(?:解释|说明|分析|介绍|看看|查看)\s*它|它\s*(?:为什么|为何|怎么)|那?它呢|"
    r"\b(?:explain|describe|analy[sz]e|inspect|review)\s+it\b|"
    r"\bwhy\s+does\s+it\b|\bwhat\s+about\s+it\b|"
    r"\bwhat\s+does\s+it\s+do\b|\bhow\s+(?:is\s+it\s+organiz(?:ed|sed)|"
    r"does\s+it\s+work)\b|\bdoes\s+it\s+have\s+"
    r"(?:tests?|dependencies|modules?|features?|configuration|an?\s+entry\s+point)\b|"
    r"它(?:是)?(?:做什么|干什么)的?|它(?:是)?如何(?:组织|工作|运行)的?|"
    r"它有(?:哪些|什么)(?:模块|功能|依赖|测试|配置)|它的?目录结构|"
    r"\b(?:run|build|test|deploy|inspect|check|search|read)\b[^\r\n]{0,40}"
    r"\bthere\b|\bcontinue\s+with\s+it\b|\bkeep\s+working\s+on\s+it\b|"
    r"(?:继续|接着)(?:处理|做|检查|修改|修复)?(?:它|这个)",
    re.IGNORECASE,
)
_PROJECT_CONTINUATION = re.compile(
    r"^\s*(?:继续|接着|再|还是|然后)\s*"
    r"(?:修复|修改|检查|查看|搜索|测试|构建|部署|运行|启动|介绍|分析|阅读|查找|"
    r"梳理|说|讲|做|处理|试|完成|推进)(?:[\s\S]{0,80})$|"
    r"^\s*(?:continue|then|next|again)\s+"
    r"(?:fixing|changing|inspecting|checking|reviewing|searching|testing|building|"
    r"deploying|running|starting|describing|explaining|analy[sz]ing|reading|"
    r"finding|working)\b"
    r"[^\r\n]{0,80}$|"
    r"^\s*(?:继续(?:吧)?|接着|就这个|同一个|continue|same\s+one|more|further)\s*"
    r"[.!?\u3002\uff01\uff1f]?\s*$|"
    r"^\s*(?:再多说(?:一点|一些)?|再说(?:一点|一些)|告诉我更多|"
    r"tell\s+me\s+more)\s*[.!?\u3002\uFF01\uFF1F]?\s*$",
    re.IGNORECASE,
)
_UNQUALIFIED_PROJECT_READ_IMPERATIVE = re.compile(
    r"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?"
    r"(?:介绍|说明|解释|描述|分析|展示|看看)(?:一下|下)?\s*"
    r"(?:[A-Za-z0-9_.+-]+|[一-鿿]{1,12})?\s*(?:项目|工程|仓库|代码库)|"
    r"^\s*(?:please\s+)?(?:introduce|describe|explain|analy[sz]e|show)\s+"
    r"(?:the\s+)?(?:(?:[A-Za-z0-9_.+-]+)\s+){0,3}"
    r"(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_ACTION_ELLIPSIS = re.compile(
    r"^\s*(?:继续|接着|然后|再)\s*(?:完成|推进|处理|做)?"
    r"(?:一下|下)?(?:这个|该|上一个)?(?:任务|工作|事情)?"
    r"\s*[.!?\u3002\uff01\uff1f]?\s*$|"
    r"^\s*(?:continue|keep\s+going|carry\s+on|finish|complete)"
    r"(?:\s+(?:the|this|previous))?(?:\s+(?:task|work))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_OVERVIEW_EVIDENCE_INTENT = re.compile(
    r"介绍|概览|概述|讲讲|说明|了解|"
    r"解释(?:一下|下)?\s*(?:这个|该|当前|本)?\s*(?:项目|工程|仓库|代码库)"
    r"\s*[.!?\u3002\uFF01\uFF1F]?\s*$|"
    r"(?:项目|工程|仓库|代码库)[^\r\n]{0,30}(?:做什么|干什么|是什么)|"
    r"(?:分析|梳理)[^\r\n]{0,30}(?:架构|技术栈|代码结构)|"
    r"\bexplain\s+(?:the\s+|this\s+|that\s+|current\s+)?"
    r"(?:project|repository|repo|codebase)\s*[.!?]?\s*$|"
    r"\b(?:introduce|describe|overview|summari[sz]e|understand)\b|"
    r"\btell\s+me\s+about\b|\bwalk\s+me\s+through\b|"
    r"\b(?:give|take)\s+me\s+(?:on\s+)?a\s+tour\s+of\b|"
    r"\bwhat\s+(?:is|does)\b[^\r\n]{0,30}"
    r"(?:project|repository|repo|codebase)\b|"
    r"\banaly[sz]e\b[^\r\n]{0,30}\b(?:architecture|stack|code\s+structure)\b|"
    r"核心能力|主要功能|整体定位|主要用途|"
    r"主要解决[^\r\n]{0,20}问题|如何组织|目录结构|"
    r"\b(?:core\s+capabilit(?:y|ies)|main\s+(?:features?|purpose)|"
    r"overall\s+purpose|how\s+(?:is|the)\s+[^\r\n]{0,20}organiz(?:ed|ation))\b",
    re.IGNORECASE,
)
_PROJECT_PROPERTY = re.compile(
    r"测试覆盖率|测试结果|构建流程|构建结果|更新日志|"
    r"运行方式|部署流程|依赖|技术栈|入口|配置|数据库|"
    r"\b(?:test\s+(?:coverage|results?|reports?)|build\s+(?:process|flow|results?)|"
    r"change\s*log|changelog|dependencies|tech(?:nology)?\s+stack|entry\s+point|"
    r"configuration|database)\b",
    re.IGNORECASE,
)
_QUESTION_SIGNAL = re.compile(
    r"什么|哪些|哪|多少|如何|怎么|怎样|吗|呢|[?\uff1f]|"
    r"\b(?:what|which|how|where|why|whether|does|is|are)\b",
    re.IGNORECASE,
)
_LOCAL_PROPERTY_STATE_CUE = re.compile(
    r"这里|本地|当前|现有|已安装|已经安装|正在使用|使用了|配置了|"
    r"\b(?:here|locally|current|existing|installed|configured|in\s+use)\b|"
    r"\b(?:does|do)\s+(?:this|that|the\s+current)\s+"
    r"(?:project|repository|repo|codebase)\b",
    re.IGNORECASE,
)
_GENERAL_PROJECT_CONCEPT = re.compile(
    r"项目(?:管理|估算|估时|治理|方法论|生命周期|组合管理|经理职责)|"
    r"\bproject\s+(?:management|estimation|governance|methodolog(?:y|ies)|"
    r"life\s*cycle|portfolio\s+management)\b|"
    r"\b(?:open[- ]source|software|student|research|example|sample)\s+projects?\b|"
    r"\bprojects\s+(?:in\s+general|typically|usually)\b",
    re.IGNORECASE,
)
_DEFINITION_QUERY = re.compile(
    r"是什么意思|什么是|定义|概念|"
    r"\bwhat\s+(?:is|are)\b|\bwhat\s+does\b[^\r\n]{0,80}\bmean\b|"
    r"\b(?:define|explain)\b[^\r\n]{0,32}\b(?:concept|term|meaning)\b",
    re.IGNORECASE,
)
_SCOPE_TERM = re.compile(
    r"工作区根目录|整个工作区|所有项目|全部项目|另一个项目|其他项目|"
    r"\b(?:workspace\s+root|entire\s+workspace|whole\s+workspace|"
    r"all\s+projects?|another\s+project|other\s+projects?)\b",
    re.IGNORECASE,
)
_LOCAL_SCOPE_DEICTIC = re.compile(
    r"(?:这个|该|当前|本|本地)(?:工作区|目录|项目)|"
    r"\b(?:this|that|current|local)\s+(?:workspace|directory|project)\b",
    re.IGNORECASE,
)
_POTENTIAL_WORKSPACE_TASK_FRAME = re.compile(
    r"^\s*(?:please\s+)?(?:make|ensure|handle|address|resolve|enable|support|allow|"
    r"prevent)\b|"
    r"^\s*(?:i|we)\s+(?:need|want|would\s+like)\b|"
    r"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?"
    r"(?:让|确保|处理|解决|启用|支持|允许|避免)|"
    r"^\s*(?:我|我们)?\s*(?:需要|想要)",
    re.IGNORECASE,
)
_CHINESE_SOFTWARE_SUBJECT = (
    r"(?:登录|认证|授权|接口|端点|输入|输出|请求|响应|模式|支持|"
    r"功能|缺陷|问题|错误|异常|页面|服务|应用|程序|脚本|代码|文件|测试|"
    r"构建|依赖|数据库|配置|空值|边界)"
)
_ENGLISH_SOFTWARE_SUBJECT = (
    r"(?:login|auth(?:entication|orization)?|apis?|endpoints?|uuids?|inputs?|"
    r"outputs?|requests?|responses?|modes?|support|features?|bugs?|issues?|errors?|"
    r"pages?|services?|apps?|applications?|programs?|scripts?|code|files?|tests?|"
    r"builds?|dependencies|databases?|configs?|configuration|null|empty|edge\s+cases?)"
)
_POTENTIAL_SOFTWARE_OBJECT = re.compile(
    rf"{_CHINESE_SOFTWARE_SUBJECT}|\b{_ENGLISH_SOFTWARE_SUBJECT}\b",
    re.IGNORECASE,
)
_ENGLISH_RESULT_STATE_VERB = (
    r"(?:work|accept|support|handle|allow|return|reject|validate|process|pass|run|"
    r"start|stop|remain|stay|use|be)"
)
_ENGLISH_RESULT_STATE = (
    rf"(?:{_ENGLISH_RESULT_STATE_VERB}\b|working|accepting|supporting|handling|"
    r"passing|running|available|compatible|stable|secure)"
)
_CHINESE_RESULT_STATE = (
    r"(?:弄好|修好|改好|做好|处理好|跑通|恢复正常|正常工作|工作正常|"
    r"支持|接受|接收|处理|允许|返回|拒绝|校验|通过|运行|启动|停止|保持)"
)
_CAUSATIVE_RESULT_STATE_FRAME = re.compile(
    rf"^\s*(?:please\s+)?(?:"
    rf"get\s+(?:the\s+)?{_ENGLISH_SOFTWARE_SUBJECT}\s+"
    rf"(?:(?:to\s+)?{_ENGLISH_RESULT_STATE_VERB}|{_ENGLISH_RESULT_STATE})|"
    rf"(?:have|let)\s+(?:the\s+)?{_ENGLISH_SOFTWARE_SUBJECT}\s+"
    rf"{_ENGLISH_RESULT_STATE_VERB}|"
    rf"keep\s+(?:the\s+)?{_ENGLISH_SOFTWARE_SUBJECT}\s+{_ENGLISH_RESULT_STATE}"
    rf")\b|"
    rf"^\s*(?:(?:请|请你|帮我|麻烦)\s*)?把"
    rf"[^,;.!?\r\n]{{0,24}}{_CHINESE_SOFTWARE_SUBJECT}"
    rf"[^,;.!?\r\n]{{0,32}}{_CHINESE_RESULT_STATE}",
    re.IGNORECASE,
)
_OBLIGATION_RESULT_STATE_FRAME = re.compile(
    rf"^\s*(?:the\s+)?{_ENGLISH_SOFTWARE_SUBJECT}\s+"
    rf"(?:should|must|needs?\s+to|need\s+to|has\s+to|have\s+to|ought\s+to)\s+"
    rf"{_ENGLISH_RESULT_STATE}|"
    rf"^\s*{_CHINESE_SOFTWARE_SUBJECT}[^,;.!?\r\n]{{0,16}}"
    rf"(?:应该|必须|需要)(?:能够|可以)?\s*{_CHINESE_RESULT_STATE}",
    re.IGNORECASE,
)
_RESULT_STATE_QUESTION_FRAME = re.compile(
    r"[?\uFF1F]\s*$|(?:吗|呢)\s*[?\uFF1F]?\s*$|"
    r"^\s*(?:为什么|为何|如何|怎么|是否|应不应该|要不要)|"
    r"^\s*(?:why|how|what|which|should|must|does|do|is|are|can|could|would)\b",
    re.IGNORECASE,
)
_EPISTEMIC_COMPLEMENT = re.compile(
    r"(?:我|我们)?\s*(?:需要|想要|想)\s*"
    r"(?:一个|一份|一些)?\s*(?:解释|了解|理解|学习|知道|建议|指导)|"
    r"(?:我|我们)?\s*需要[^,;.!?\r\n]{0,40}(?:解释|建议|指导)|"
    r"(?:我想|我需要)?\s*理解[^,;.!?\r\n]{0,40}原理|"
    r"\b(?:i|we)\s+(?:need|want|would\s+like)\s+(?:an?\s+)?"
    r"(?:explanation|advice|guidance|information)\s+(?:of|on|about)\b|"
    r"\b(?:i|we)\s+(?:need|want|would\s+like)\s+to\s+"
    r"(?:understand|learn(?:\s+about)?|know(?:\s+how)?)\b",
    re.IGNORECASE,
)
type WorkKind = Literal["conversation", "read", "execute", "undetermined"]


def is_workspace_target_followup_request(request: str) -> bool:
    """Return whether the request reliably refers to the adjacent project target."""

    compact = _semantic_request_text(request).strip()
    if (
        not compact
        or has_governed_workspace_root_target(compact)
        or has_governed_multiple_project_targets(compact)
    ):
        return False
    if has_other_project_target_intent(compact):
        return False
    if _PROJECT_DEICTIC_REFERENCE.search(compact):
        return True
    if _is_general_project_concept(compact, []):
        return False
    adjacent_detail = bool(
        is_project_scope_followup_request(compact)
        and not _WORKSPACE_READ_INTENT.search(compact)
        and not _UNQUALIFIED_PROJECT_READ_IMPERATIVE.search(compact)
    )
    return bool(
        adjacent_detail
        or _PROJECT_DEICTIC_REFERENCE.search(compact)
        or _PROJECT_PRONOUN_DETAIL.search(compact)
        or _PROJECT_CONTINUATION.fullmatch(compact)
    )


def resolve_request(
    request: str,
    candidates: Sequence[ProjectCandidate],
    *,
    prior_target: ProjectTarget | None = None,
    root_candidate: ProjectCandidate | None = None,
    prior_resolution: RequestResolution | None = None,
) -> RequestResolution:
    """Resolve workspace dependence and project-target ambiguity for one request.

    This function performs no filesystem access and does not create a ``ProjectTarget``. A caller
    materializes a resolved target from the verified candidate inventory, or from the workspace
    root identity when ``target_reference`` is ``workspace``.
    """

    compact = request.strip()
    candidate_list = list(candidates)
    if root_candidate is not None and not any(
        candidate.path == "." for candidate in candidate_list
    ):
        candidate_list.insert(0, root_candidate)
    inline_only = _is_inline_only_request(compact)
    semantic_text = _semantic_request_text(compact)
    scope_text = semantic_text
    scope_definition = _is_scope_term_definition(scope_text)
    explicit = explicit_target_candidates(scope_text, candidate_list)
    ambiguous_semantic_subject = bool(
        len([candidate for candidate in candidate_list if candidate.path != "."]) > 1
        and not explicit
        and has_ambiguous_project_semantic_subject_intent(scope_text, candidate_list)
    )
    multiple_targets = has_governed_multiple_project_targets(scope_text)
    workspace_root_target = bool(
        _ROOT_RELATIVE_REFERENCE.search(scope_text)
        or has_governed_workspace_root_target(scope_text)
    )
    governed_file = _has_governed_local_file_reference(scope_text)
    other_target = has_other_project_target_intent(scope_text)
    mixed_current_other = bool(
        prior_target is not None
        and has_mixed_current_other_project_targets(scope_text)
    )
    mixed_adjacent_explicit = bool(
        prior_target is not None
        and has_mixed_adjacent_explicit_project_targets(scope_text, candidate_list)
    )
    adjacent_explicit = list(explicit)
    if mixed_adjacent_explicit:
        seen_ids = {candidate.id for candidate in adjacent_explicit}
        adjacent_explicit.extend(
            candidate
            for candidate in matching_candidates(scope_text, candidate_list)
            if candidate.id not in seen_ids
        )
    same_adjacent_explicit = bool(
        mixed_adjacent_explicit
        and len(adjacent_explicit) == 1
        and prior_target is not None
        and adjacent_explicit[0].path == prior_target.path
        and adjacent_explicit[0].identity == prior_target.identity
    )
    mixed_adjacent_explicit = bool(
        mixed_adjacent_explicit
        and not same_adjacent_explicit
        and adjacent_explicit
    )
    local_subject = bool(
        explicit
        or _LOCAL_WORKSPACE_REFERENCE.search(semantic_text)
        or governed_file
        or _CONCRETE_WORKSPACE_SUBJECT.search(semantic_text)
    )
    response_artifact = _is_response_artifact_request(
        semantic_text,
        has_explicit_target=bool(explicit),
    )
    explicit_file_mutation = bool(
        _LOCAL_FILE_REFERENCE.search(semantic_text)
        and (
            _CONTENT_CREATION_ACTION.search(semantic_text)
            or _FILESYSTEM_OUTPUT_DESTINATION.search(semantic_text)
        )
    )
    content_only = bool(
        not local_subject
        and (
            _CONTENT_CREATION_ACTION.search(semantic_text)
            or _PROVIDED_TEXT_CORRECTION.fullmatch(semantic_text)
        )
    )
    inherited_execution = bool(
        prior_target is not None
        and prior_resolution is not None
        and prior_resolution.work_kind == "execute"
        and _ACTION_ELLIPSIS.fullmatch(semantic_text)
    )
    epistemic = bool(_EPISTEMIC_COMPLEMENT.search(semantic_text))
    structured_result_state = _has_structured_result_state_task(semantic_text)
    execution = bool(
        not inline_only
        and not content_only
        and not response_artifact
        and not epistemic
        and not structured_result_state
        and (
            has_execution_intent(semantic_text, candidate_list)
            or inherited_execution
            or explicit_file_mutation
        )
    )
    advisory = has_advisory_action_intent(semantic_text)
    informational_action = has_informational_action_intent(semantic_text)
    undetermined_task = bool(
        not inline_only
        and not content_only
        and not execution
        and not advisory
        and not informational_action
        and not epistemic
        and _has_potential_workspace_task(semantic_text)
    )
    followup = bool(
        not inline_only
        and not scope_definition
        and prior_target is not None
        and (
            is_workspace_target_followup_request(scope_text)
            or inherited_execution
        )
    )
    general_concept = scope_definition or _is_general_project_concept(
        scope_text, explicit
    )
    project_read = bool(
        not inline_only
        and not execution
        and not general_concept
        and (
            not (
                _WORKSPACE_ARTIFACT.search(semantic_text)
                or _LOCAL_FILE_REFERENCE.search(semantic_text)
            )
            or governed_file
        )
        and (
            is_project_overview_request(semantic_text, explicit)
            or _PROJECT_TOUR.search(semantic_text)
        )
    )
    overview = bool(
        not inline_only
        and not execution
        and not general_concept
        and _OVERVIEW_EVIDENCE_INTENT.search(semantic_text)
        and (
            project_read
            or _LOCAL_PROJECT_REFERENCE.search(semantic_text)
            or followup
            or explicit
        )
    )
    property_query = _is_project_property_query(semantic_text)
    workspace_read = bool(
        not inline_only
        and not general_concept
        and (
            _LOCAL_WORKSPACE_REFERENCE.search(semantic_text)
            or _WORKSPACE_READ_INTENT.search(semantic_text)
            or project_read
            or _has_concrete_workspace_read_intent(semantic_text)
            or _has_concrete_workspace_location_query(semantic_text)
            or (advisory and _has_advisory_workspace_subject(semantic_text))
            or (
                informational_action
                and (
                    _WORKSPACE_ANCHOR.search(semantic_text)
                    or _CONCRETE_WORKSPACE_SUBJECT.search(semantic_text)
                )
            )
            or governed_file
            or response_artifact
            or property_query
            or ambiguous_semantic_subject
            or _WORKSPACE_CONTAINER_QUERY.search(semantic_text)
        )
    )
    bare_target = len(explicit) == 1 and _is_bare_candidate_selection(
        scope_text, explicit[0]
    )
    conversational = bool(_NON_WORKSPACE_CONVERSATION.fullmatch(compact))
    workspace_dependent = bool(
        not conversational
        and (
            execution
            or undetermined_task
            or overview
            or workspace_read
            or followup
            or bare_target
            or explicit
            or ambiguous_semantic_subject
            or same_adjacent_explicit
            or (
                not scope_definition
                and (
                    _WORKSPACE_CONTAINER_QUERY.search(scope_text)
                    or workspace_root_target
                    or multiple_targets
                    or mixed_current_other
                    or mixed_adjacent_explicit
                    or other_target
                )
            )
        )
    )
    if not workspace_dependent:
        return RequestResolution(
            work_kind="conversation",
            workspace_dependent=False,
            target_reference="none",
            target_status="not_required",
            ambiguity_dimensions=[],
            overview_required=False,
            reasons=["The request does not depend on files or commands in the workspace."],
        )

    work_kind: WorkKind = (
        "execute" if execution else "undetermined" if undetermined_task else "read"
    )
    if explicit and workspace_root_target and _MIXED_WORKSPACE_TARGET.search(
        scope_text
    ):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="multiple",
            target_status="unsupported",
            ambiguity_dimensions=["target", "scope"],
            overview_required=overview,
            reasons=[
                "The request requires both a verified project and the workspace root."
            ],
        )
    if same_adjacent_explicit:
        assert prior_target is not None
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="inherited",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=[
                f"The adjacent reference and explicit target both resolve to {prior_target.path}."
            ],
        )
    if multiple_targets or mixed_current_other or mixed_adjacent_explicit:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="multiple",
            target_status="unsupported",
            ambiguity_dimensions=["target", "scope"],
            overview_required=overview,
            reasons=["The request requires multiple project targets in one turn."],
        )
    if len(explicit) > 1 and _uses_alternative_target_choice(scope_text, explicit):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="unspecified",
            target_status="clarification_required",
            ambiguity_dimensions=["target"],
            overview_required=overview,
            reasons=["The request names several verified projects as alternatives."],
        )
    if len(explicit) > 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="multiple",
            target_status="unsupported",
            ambiguity_dimensions=["target", "scope"],
            overview_required=overview,
            reasons=["The request explicitly names multiple verified project targets."],
        )
    if len(explicit) == 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="explicit",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=[f"The request explicitly selects verified project {explicit[0].path}."],
        )
    if _ROOT_RELATIVE_REFERENCE.search(scope_text):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=["An explicit ./ path selects the workspace root."],
        )
    if _WORKSPACE_CONTAINER_QUERY.search(scope_text):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=["The request addresses the workspace container itself."],
        )
    if workspace_root_target:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=["The request explicitly selects the whole workspace."],
        )
    if execution and not followup and _RAW_COMMAND_TARGET.fullmatch(semantic_text):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=False,
            reasons=["An explicit raw command uses the workspace root as its working target."],
        )
    if execution and _GREENFIELD_WORKSPACE_TARGET.search(semantic_text):
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=False,
            reasons=["A greenfield project request targets the workspace root."],
        )
    if other_target:
        return _resolve_other_target(
            work_kind=work_kind,
            overview=overview,
            candidates=candidate_list,
            prior_target=prior_target,
        )
    if followup:
        if _prior_target_is_current(prior_target, candidate_list):
            assert prior_target is not None
            return RequestResolution(
                work_kind=work_kind,
                workspace_dependent=True,
                target_reference="inherited",
                target_status="resolved",
                ambiguity_dimensions=[],
                overview_required=overview,
                reasons=[f"The request reliably refers to adjacent target {prior_target.path}."],
            )
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="inherited",
            target_status="unsupported",
            ambiguity_dimensions=["target"],
            overview_required=overview,
            reasons=["The adjacent project target is no longer present in the verified inventory."],
        )
    if len(candidate_list) == 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="unspecified",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=[f"The workspace has one verified project target: {candidate_list[0].path}."],
        )
    if len(candidate_list) > 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="unspecified",
            target_status="clarification_required",
            ambiguity_dimensions=["target"],
            overview_required=overview,
            reasons=["The request needs one project target but the workspace has several."],
        )
    if execution or not overview:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="workspace",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=["No child project is present, so the workspace root is the target."],
        )
    return RequestResolution(
        work_kind=work_kind,
        workspace_dependent=True,
        target_reference="unspecified",
        target_status="unsupported",
        ambiguity_dimensions=["target"],
        overview_required=overview,
        reasons=["No verified project target is available for the requested overview."],
    )


def explicit_target_candidates(
    request: str,
    candidates: Sequence[ProjectCandidate],
) -> list[ProjectCandidate]:
    """Return verified names used in a target role, not merely as ordinary nouns."""

    semantic_text = _semantic_request_text(request)
    candidate_list = list(candidates)
    negated = {
        candidate.id
        for candidate in negated_project_switch_candidates(semantic_text, candidate_list)
    }
    return [
        candidate
        for candidate in target_role_candidates(semantic_text, candidate_list)
        if candidate.id not in negated and candidate.path != "."
    ]


def _is_general_project_concept(
    request: str,
    explicit: Sequence[ProjectCandidate],
) -> bool:
    if (
        explicit
        or _LOCAL_WORKSPACE_REFERENCE.search(request)
        or _PROJECT_DEICTIC_REFERENCE.search(request)
    ):
        return False
    if re.search(
        r"(?:这个|该|当前|本|上述)\s*(?:[A-Za-z0-9_.+-]+|[一-鿿]{1,12})\s*"
        r"(?:项目|工程|仓库|代码库)|"
        r"\b(?:the|this|that|current|local|previous|above)\s+"
        r"(?:[A-Za-z0-9_.+-]+\s+){1,3}(?:projects?|repositor(?:y|ies)|repos?|"
        r"codebases?)\b",
        request,
        re.IGNORECASE,
    ):
        return False
    if (
        _WORKSPACE_CONTAINER_QUERY.search(request)
        or _WHOLE_WORKSPACE_TARGET.search(request)
        or _MULTIPLE_TARGET.search(request)
        or _OTHER_TARGET.search(request)
    ):
        return False
    if _GENERAL_PROJECT_CONCEPT.search(request):
        return True
    modified = re.search(
        r"(?:介绍|说明|解释|分析|了解|讲讲)(?:一下|下)?\s*"
        r"(?P<modifier>[\w一-鿿.+-]{1,24})项目",
        request,
        re.IGNORECASE,
    )
    if modified is not None:
        return modified.group("modifier") not in {
            "一个",
            "一下",
            "下",
            "某个",
            "任意",
            "任何",
            "随便一个",
            "这个",
            "该",
            "当前",
            "本",
        }
    english_modified = re.search(
        r"\b(?:introduce|describe|explain|analy[sz]e|understand|summari[sz]e)\s+"
        r"(?:(?:the|a|an)\s+)?"
        r"(?P<modifier>[A-Za-z0-9_.+-]+(?:\s+[A-Za-z0-9_.+-]+){0,2})\s+"
        r"(?:projects?|repositor(?:y|ies)|repos?|codebases?)\b",
        request,
        re.IGNORECASE,
    )
    if english_modified is None:
        return False
    return english_modified.group("modifier").casefold() not in {
        "the",
        "a",
        "an",
        "this",
        "that",
        "current",
        "local",
        "previous",
        "above",
        "one",
        "any",
        "some",
    }


def _has_potential_workspace_task(request: str) -> bool:
    """Route result-state software requests without pretending their effect is certain."""

    potential_frame = bool(
        (
            _POTENTIAL_WORKSPACE_TASK_FRAME.search(request)
            and _POTENTIAL_SOFTWARE_OBJECT.search(request)
        )
        or _has_structured_result_state_task(request)
    )
    return bool(
        potential_frame
        and not _EPISTEMIC_COMPLEMENT.search(request)
        and not _EXTERNAL_KNOWLEDGE_CONTEXT.search(request)
        and not _DEFINITION_QUERY.search(request)
        and not _RESULT_STATE_QUESTION_FRAME.search(request)
    )


def _has_structured_result_state_task(request: str) -> bool:
    """Recognize a declarative/causative desired software state, not a question."""

    return bool(
        (
            _CAUSATIVE_RESULT_STATE_FRAME.search(request)
            or _OBLIGATION_RESULT_STATE_FRAME.search(request)
        )
        and not _EPISTEMIC_COMPLEMENT.search(request)
        and not _EXTERNAL_KNOWLEDGE_CONTEXT.search(request)
        and not _DEFINITION_QUERY.search(request)
        and not _RESULT_STATE_QUESTION_FRAME.search(request)
    )
def _is_scope_term_definition(request: str) -> bool:
    """Keep definitions of scope vocabulary separate from local scope selection."""

    return bool(
        _DEFINITION_QUERY.search(request)
        and _SCOPE_TERM.search(request)
        and not _LOCAL_SCOPE_DEICTIC.search(request)
    )


def _is_project_property_query(request: str) -> bool:
    match = _PROJECT_PROPERTY.search(request)
    if match is None or not _QUESTION_SIGNAL.search(request):
        return False
    return bool(
        _LOCAL_WORKSPACE_REFERENCE.search(request)
        or _LOCAL_PROPERTY_STATE_CUE.search(request)
        or re.search(
            r"(?:项目|工程|仓库|代码库)的|"
            r"\b(?:the|this|that|current)\s+"
            r"(?:project|repository|repo|codebase)['\u2019]s\b",
            request,
            re.IGNORECASE,
        )
    )


def _has_concrete_workspace_read_intent(request: str) -> bool:
    """Recognize compositional reads of local artifacts without enumerating full prompts."""

    local_context = bool(
        _LOCAL_WORKSPACE_REFERENCE.search(request) or _WORKSPACE_ANCHOR.search(request)
    )
    if _EXTERNAL_KNOWLEDGE_CONTEXT.search(request) and not local_context:
        return False
    subject = bool(_CONCRETE_WORKSPACE_SUBJECT.search(request))
    if not subject:
        return False
    if has_inspection_read_action_intent(request):
        return True
    if has_overview_read_action_intent(request) and (
        local_context or _DEFINITE_EXPLANATION_SUBJECT.search(request)
    ):
        return True
    if has_diagnostic_action_intent(request):
        return True
    return bool(
        _QUESTION_SIGNAL.search(request)
        and _DIAGNOSTIC_STATE.search(request)
        and _DIAGNOSTIC_SUBJECT.search(request)
    )


def _has_governed_local_file_reference(request: str) -> bool:
    """Require a local role for file-like text instead of treating its spelling as scope."""

    if not (
        _WORKSPACE_ARTIFACT.search(request) or _LOCAL_FILE_REFERENCE.search(request)
    ):
        return False
    if _ROOT_RELATIVE_REFERENCE.search(request) or _LOCAL_WORKSPACE_REFERENCE.search(
        request
    ):
        return True
    if _FILE_CONCEPT_CONTEXT.search(request) and has_overview_read_action_intent(
        request
    ):
        return False
    if _DEFINITION_QUERY.search(request) and not _FILE_CONTENT_QUERY.search(request):
        return False
    return bool(
        has_read_action_intent(request)
        or has_execution_intent(request)
        or _FILE_CONTENT_QUERY.search(request)
        or _LOCATION_QUERY_SIGNAL.search(request)
    )


def _has_concrete_workspace_location_query(request: str) -> bool:
    if _EXTERNAL_KNOWLEDGE_CONTEXT.search(request):
        return False
    if _CONTAINMENT_QUERY.search(request):
        return True
    return bool(
        _LOCATION_QUERY_SIGNAL.search(request)
        and _LOCAL_LOCATION_SUBJECT.search(request)
    )


def _has_advisory_workspace_subject(request: str) -> bool:
    """Find the object of a discussed action without treating its verb as that object."""

    masked = mask_execution_action_words(request)
    if (
        _WORKSPACE_ANCHOR.search(masked)
        or _CONCRETE_WORKSPACE_SUBJECT.search(masked)
        or _LOCAL_FILE_REFERENCE.search(masked)
    ):
        return True

    removed_spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, (original, replacement) in enumerate(zip(request, masked, strict=False)):
        removed = original != " " and replacement == " "
        if removed and start is None:
            start = index
        elif not removed and start is not None:
            removed_spans.append((start, index))
            start = None
    if start is not None:
        removed_spans.append((start, len(masked)))

    for subject in _CONCRETE_WORKSPACE_SUBJECT.finditer(request):
        if not re.search(r"[一-鿿]", subject.group()):
            continue
        containing = next(
            (
                span
                for span in removed_spans
                if span[0] <= subject.start() < span[1]
            ),
            None,
        )
        if containing is None:
            continue
        if containing[0] < subject.start() and re.fullmatch(
            r"[一-鿿]+",
            request[containing[0] : subject.start()],
        ):
            return True
        preceding = [span for span in removed_spans if span[1] <= subject.start()]
        if preceding and re.fullmatch(
            r"\s*(?:一下|下)?\s*",
            request[preceding[-1][1] : subject.start()],
        ):
            return True
    return False


def _is_inline_only_request(request: str) -> bool:
    if not _INLINE_SNIPPET.search(request):
        return False
    outside_inline = _semantic_request_text(request)
    return bool(
        not _WORKSPACE_ANCHOR.search(outside_inline)
        and not _WORKSPACE_ARTIFACT.search(outside_inline)
        and not _LOCAL_FILE_REFERENCE.search(outside_inline)
    )


def _semantic_request_text(request: str) -> str:
    """Mask user-supplied literals before classifying actions or project targets."""

    without_snippets = _INLINE_SNIPPET.sub(" ", request)
    masked = list(without_snippets)
    for match in _CONTENT_LITERAL.finditer(without_snippets):
        start, end = match.span("literal")
        masked[start:end] = " " * (end - start)
    return "".join(masked)


def _is_response_artifact_request(
    request: str,
    *,
    has_explicit_target: bool,
) -> bool:
    """Recognize a requested answer artifact without inventing an on-disk mutation."""

    return bool(
        _CONTENT_CREATION_ACTION.search(request)
        and (
            _RESPONSE_OUTPUT_OBJECT.search(request)
            or (has_explicit_target and _RESPONSE_OUTPUT_NOUN.search(request))
        )
        and not _LOCAL_FILE_REFERENCE.search(request)
        and not _FILESYSTEM_OUTPUT_DESTINATION.search(request)
        and not re.search(
            r"数据库(?:表|模式)|数据表|\b(?:database\s+tables?|schemas?)\b",
            request,
            re.IGNORECASE,
        )
    )


def _is_bare_candidate_selection(request: str, candidate: ProjectCandidate) -> bool:
    return bool(
        re.fullmatch(
            rf"\s*(?:项目\s*)?{re.escape(candidate.path)}(?:\s*(?:项目|工程|仓库))?"
            r"\s*[.!?\u3002\uff01\uff1f]?\s*",
            request,
            re.IGNORECASE,
        )
    )


def _uses_alternative_target_choice(
    request: str,
    candidates: Sequence[ProjectCandidate],
) -> bool:
    """Return whether verified targets are offered as alternatives, not a joint scope."""

    mentions: list[tuple[int, int, str]] = []
    for candidate in candidates:
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_.-]){re.escape(candidate.path)}"
            r"(?![A-Za-z0-9_.-])",
            request,
            re.IGNORECASE,
        ):
            mentions.append((match.start(), match.end(), candidate.id))
    mentions.sort()
    for (_, end, candidate_id), (start, _, next_candidate_id) in pairwise(mentions):
        if candidate_id == next_candidate_id:
            continue
        between = request[end:start]
        if re.search(
            r"\b(?:either\s+)?or\b|要么|或者|或",
            between,
            re.IGNORECASE,
        ):
            return True
    return False


def _prior_target_is_current(
    prior_target: ProjectTarget | None,
    candidates: list[ProjectCandidate],
) -> bool:
    if prior_target is None:
        return False
    return any(
        candidate.path == prior_target.path and candidate.identity == prior_target.identity
        for candidate in candidates
    )


def _resolve_other_target(
    *,
    work_kind: WorkKind,
    overview: bool,
    candidates: list[ProjectCandidate],
    prior_target: ProjectTarget | None,
) -> RequestResolution:
    if prior_target is None:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="other",
            target_status="unsupported",
            ambiguity_dimensions=["target"],
            overview_required=overview,
            reasons=["An 'other project' reference has no adjacent project target."],
        )
    alternatives = [candidate for candidate in candidates if candidate.path != prior_target.path]
    if len(alternatives) == 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="other",
            target_status="resolved",
            ambiguity_dimensions=[],
            overview_required=overview,
            reasons=[f"Exactly one other verified project is available: {alternatives[0].path}."],
        )
    if len(alternatives) > 1:
        return RequestResolution(
            work_kind=work_kind,
            workspace_dependent=True,
            target_reference="other",
            target_status="clarification_required",
            ambiguity_dimensions=["target"],
            overview_required=overview,
            reasons=["Several verified projects differ from the adjacent target."],
        )
    return RequestResolution(
        work_kind=work_kind,
        workspace_dependent=True,
        target_reference="other",
        target_status="unsupported",
        ambiguity_dimensions=["target"],
        overview_required=overview,
        reasons=["No other verified project is available."],
    )
