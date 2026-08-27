# TraceForge：可信编程智能体实施计划

## 1. 产品目标与边界

TraceForge 是一个本地 Web 编程智能体，核心卖点是“任务完成有证据”，而不是功能数量：

- 用户提交任务后，系统先生成可编辑的计划与验收条件；批准后自主读取、修改、执行并验证。
- 用户补充：我记得trae有个特别好用的功能，就是需求比较复杂的时候会向用户提问（问题+若干可选项），这个我希望能做一下
- Builder 负责实现，独立只读 Verifier 根据原始任务、代码 diff、命令结果和测试证据审查；不通过则反馈修复，最多两轮。
- 页面完整展示计划、工具调用、文件 diff、测试结果、错误恢复与最终证据，不展示模型隐藏思维链。
- 支持整次任务安全回滚、会话恢复、命令取消和模型异常重试。
- 明确不做：宠物、插件市场、IDE、云端部署、多用户、Windows、并行多智能体、浏览器操作和复杂代码编辑器。

技术栈固定为 Python 3.12、FastAPI、React、TypeScript、Vite；支持 macOS/Linux 和 OpenAI-compatible Chat Completions tool calling，不使用任何 agent 框架或托管代码执行工具。

## 2. 核心架构与行为

- 采用状态机：`created → planning → awaiting_plan_approval → executing → verifying → succeeded/failed`，另含 `awaiting_action_approval`、`cancelled`、`interrupted`、`rolled_back`。
- `ModelProvider` 只封装模型协议；消息历史、工具注册与调度、上下文压缩、审批、终止、错误恢复均自行实现。
- 内置工具固定为：
  - `list_files`：受限深度目录读取并自动忽略缓存、构建产物和 `.git`。
  - `read_file`：按行读取文本并限制单次内容。
  - `search_text`：优先使用 `rg`，缺失时回退 Python 搜索。
  - `apply_patch`：事务化应用 unified diff，并支持修改和删除文本文件。
  - `create_file`：仅创建不存在的文件。
  - `run_command`：接收 argv 数组，通过 `create_subprocess_exec` 执行，禁止 `shell=True`。
  - `update_plan`、`finish`：更新结构化计划和申请结束任务。
- 工作区在启动时通过参数固定；所有文件路径解析真实路径并拒绝越界、符号链接逃逸及 `.git` 写入。
- 用户批准计划后，计划内的文件操作和验收命令自动执行；未知、联网、安装依赖、外部写入及危险命令单独审批，`sudo` 和明显破坏系统的操作直接拒绝。
- 命令默认超时 120 秒，最长可批准至 600 秒；取消时终止整个进程组。保存输出最多 1 MiB，模型只接收首尾合计 16 KiB及退出状态。
- Agent 默认最多 30 个工具步骤；连续重复相同失败调用两次即进入恢复提示，不能无限循环。
- 上下文达到配置窗口约 70% 时压缩旧历史，始终保留原始任务、已批准计划、当前变更摘要、未解决错误和最近工具结果。
- 每个文件第一次修改前保存原始内容、权限和哈希。回滚只恢复仍与 Agent 最后写入哈希一致的文件；用户后来改动过的文件跳过并报告冲突。
- SQLite 保存运行、计划、事件、审批和验证报告；快照保存在系统用户数据目录。程序异常退出后任务标记为 `interrupted`，不会自动重放未确认命令，可选择恢复或回滚。
- Verifier 默认开启但创建任务时可关闭；它只有读/搜索权限。两轮修复后仍不通过则如实标记失败，不允许 Builder 自行宣称完成。

## 3. 公共接口与 Web 体验

- 启动命令：
  - `traceforge serve --workspace PATH --port 8765`
  - `traceforge demo`：复制随仓库提供的演示项目到临时目录并启动。
- 配置仅使用 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` 和可选 `TRACEFORGE_CONTEXT_LIMIT`；日志、API 响应和界面永不显示完整密钥。
- FastAPI 同源提供前端、REST 和 WebSocket，默认只绑定 `127.0.0.1`：
  - 创建、查询、取消和回滚运行。
  - 批准或要求修改计划。
  - 批准或拒绝高风险动作。
  - 按序号补取历史事件并通过 WebSocket 接收增量事件。
- 核心数据类型包括 `RunState`、`TaskPlan`、`AcceptanceCheck`、`ToolCall`、`ToolResult`、`RunEvent`、`ApprovalRequest` 和 `VerificationReport`；所有 REST、WebSocket、数据库事件共用同一套 Pydantic schema。
- 同一工作区只允许一个活动任务，第二个创建请求返回冲突；WebSocket 断线重连后从最后事件序号补齐。
- UI 固定为深色 coding mission control：
  - 左侧：历史任务与状态。
  - 中间：任务、计划审批、对话和错误恢复。
  - 右侧：Timeline、Diff、Checks、Verifier 四个页签。
  - 顶栏：工作区、模型、执行模式、步骤数和上下文用量。
  - 最终 Evidence Board 按验收条件列出通过状态、命令、退出码、修改文件和 Verifier 结论。
  - 始终提供 Stop 与 Rollback；审批卡片显示风险原因和将执行的精确动作。
- React 生产构建产物打包进 Python 包，使普通用户只需 Python 环境即可运行；前端开发和重建使用 pnpm。

## 4. 测试与验收

- 后端使用 Ruff、Mypy、Pytest、pytest-asyncio，核心模块覆盖率门槛 85%；前端使用 ESLint、TypeScript、Vitest。
- 单元测试覆盖模型输出解析、工具参数校验、状态转换、上下文压缩、重复调用检测、审批策略和 Verifier JSON 解析失败。
- 安全测试覆盖 `..` 越界、绝对路径、符号链接逃逸、`.git` 写入、危险命令、超时、进程组取消、巨量输出和密钥脱敏。
- 回滚测试覆盖已修改文件、新建文件、删除文件、权限恢复及用户二次修改冲突。
- 集成测试使用脚本化 FakeProvider，无需 API key 即可在 CI 完整跑通计划、审批、修改、测试失败、修复、验证和回滚流程。
- Playwright 完成一条浏览器端到端烟测：创建任务、批准计划、观察事件、处理命令审批、查看 Evidence Board、执行回滚。
- GitHub Actions 在 Ubuntu 运行完整测试，在 macOS 运行安装与启动烟测；真实模型测试只允许手动触发，不读取仓库 Secret 以外的凭据。
- 演示项目固定为一个小型 FastAPI 多租户用户服务：缓存 key 漏掉租户 ID，造成跨租户数据串读。演示任务要求定位并修复、保持 TTL 语义、补充回归测试并通过全部检查。
- 视频控制在约 110 秒：10 秒介绍、15 秒计划审批、45 秒加速执行、20 秒展示 Verifier/Evidence Board、20 秒讲解自研循环与安全设计。

## 5. 开发节奏与交付

- 8 月 27 日：建立 GitHub `traceforge` 公共仓库、工程骨架、规格、状态机和 Provider。
- 8 月 28 日：完成工具系统、路径安全、命令执行和 Builder 主循环。
- 8 月 29 日：完成上下文管理、SQLite 会话、快照回滚和中断恢复。
- 8 月 30 日：完成 Verifier、修复闭环、FastAPI API 和 WebSocket。
- 8 月 31 日：完成 React 工作台、审批、Diff、Checks 和 Evidence Board。
- 9 月 1 日：完成测试、CI、演示项目、架构文档、README 与视频。
- 9 月 2 日：仅处理阻断问题，完成干净 clone 验证、秘密扫描、视频/压缩包检查并提前冻结。
- 用户补充：按照你自己的节奏来就行，只要有commit history就ok，肯定没必要真干这么久

提交历史保持约十个主题明确的 commit：工程初始化、协议与状态、工具安全、Agent 循环、持久化回滚、Verifier、Web API、前端、测试评测、文档发布；不压缩或改写已推送历史。

默认采用 MIT License。仓库提供详细 `README.md` 和架构说明；提交用 `README.txt` 严格控制在 1000 汉字以内。最终 ZIP 仅包含以学生真实姓名命名的 `README.txt` 和 MP4，真实姓名、GitHub 账号及运行模型 API 凭据作为发布阶段用户输入，任何凭据都不进入仓库、历史、README 或视频。
