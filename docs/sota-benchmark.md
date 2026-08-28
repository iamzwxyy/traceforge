# TraceForge 源码对标与产品取舍

## 结论

TraceForge 不应复刻 Codex 或 DeepSeek Harness 的完整产品面。课程项目最值得借鉴的是它们已经验证过的执行底座：明确的任务生命周期、可恢复事件、读工具并发与写工具屏障、上下文压缩、沙箱和审批分层，以及真实应用级回归。

TraceForge 自己的主线应保持鲜明：**每次改动都能给出“计划 → 差异 → 新鲜检查 → 独立验证 → 可冲突回滚”的证据闭环**。这个能力比宠物、庞大插件市场或多智能体动画更能体现工程质量，也更适合作为答辩时可现场验证的亮点。产品上可将它命名为 **Proof Pack（交付证据包）**。

截至 2026-08-27，Phase A、默认 Agent / 可选计划模式、同任务多轮、Proof Pack、对话 +
折叠 Trace、故障恢复、OS 沙箱与固定质量语料均已落地；两条真实 DeepSeek 代表任务也已
脚本化并通过。当前工作重点转为可用性细节、完整回归和现场演示稳定性，而不是继续扩张功能面。

## 对标范围

本次结论固定在以下源码版本，避免把会持续变化的 `main` 当作稳定事实：

- DeepSeek Harness：[`b150a55`](https://github.com/deepseek-ai/deepseek-harness/tree/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e)
- OpenAI Codex：[`89650c6`](https://github.com/openai/codex/tree/89650c66f2f3ff0d028d3f5d6d0b187b2ed49be5)
- OpenAI 官方说明：[Codex App Server](https://developers.openai.com/codex/app-server)、[Sandbox](https://developers.openai.com/codex/sandboxing)

对标只覆盖与本项目直接相关的六个维度：执行循环、工具协议、上下文、审批与隔离、项目/任务模型、可观测性与测试。不把商业协作、插件生态、云执行、多智能体和装饰性功能列入 v1。

## 六维对标

| 维度 | SOTA 源码中的成熟做法 | TraceForge 现状 | 取舍 |
| --- | --- | --- | --- |
| 执行生命周期 | Codex 使用 Thread → Turn → Item，并流式发布 item/turn 事件；DeepSeek 将一次 turn 拆成多次 model step，持久事件承担恢复和投影 | Run 状态机清晰，规划、执行、验证、恢复均有显式状态；但 `messages_json` 仍是可变主记录 | 保留简单 Run 模型；补充更细的 step/item 事件和稳定投影，不为 v1 重写成完整事件溯源系统 |
| 工具调度 | DeepSeek 以“可并发调用 + 独占屏障”调度，执行可重叠但结果按模型顺序提交；Codex 用读写锁让并发工具共享读锁、独占工具占写锁 | 同一模型响应中的工具逐个执行，稳定但浪费只读检查时间 | 仅并发 `list_files`、`read_file`、`search_text`；写文件、命令、计划更新和完成动作都是屏障；结果仍按原 tool-call 顺序进入历史 |
| 上下文管理 | Codex 规范化 call/output 配对、截断工具输出并做可恢复 compaction；DeepSeek 把 runtime context 和 compaction 作为可持久替换事件 | 固定保留头部/尾部并生成确定性中段摘要，简单但长期任务会丢失关键因果 | 先做两级压缩：裁剪旧工具大输出，再生成结构化摘要；计划、文件改动、检查证据和未解决风险进入不可丢的 evidence ledger |
| 审批与隔离 | Codex 明确区分 plan mode、sandbox 与 approval：规划交互、技术边界和越界决策是三件事；DeepSeek 对审批失败关闭，并统一清理子进程环境中的凭据变量 | 默认 Agent、可选计划模式、路径/argv 策略、凭据清理均已分层；macOS Seatbelt / Linux Bubblewrap 独立适配，失败显式降级；完成后只读复核另成一层 | 保持各层边界和准确文案；不宣称抵御任意恶意本地代码，不把计划模式或复核包装成权限 |
| 项目与任务 | Codex 的 Project 是独立实体，Thread 可选 `project_id`；DeepSeek Workspace 使用规范路径对应稳定 ID，同时允许未分组 session | 已支持可空 `project_id`、直接任务、最近目录和可复用项目根目录，多工作区运行时按规范路径隔离 | 保持 Project 只是分组与稳定根目录，不引入远程 host、工作树 handoff 等平台复杂度 |
| 观测与测试 | DeepSeek 要求用户/模型可见变更同时有可无 key 回放快照，并用真实 API E2E 验证 provider；Codex 对 turn/item/tool/compaction 都有事件和历史投影 | 已有持久事件、断线续传、fake provider 全闭环、故障注入、固定质量语料、真实 DeepSeek 双场景、独立 verifier 与可下载 Proof Pack | 保留确定性 CI 与低频真实模型双层证据；真实 API 不进入日常 CI，避免密钥、成本和模型漂移造成不稳定 |

## 直接借鉴的源码机制

### 1. 并发只读工具，写操作保持屏障

DeepSeek Harness 的 [`tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/tool-calls.ts) 使用有界并发池，同时把独占工具作为屏障；Codex 的 [`parallel.rs`](https://github.com/openai/codex/blob/89650c66f2f3ff0d028d3f5d6d0b187b2ed49be5/codex-rs/core/src/tools/parallel.rs) 也把可并发工具与独占工具放进同一读写门。

TraceForge 只需要其中最小且安全的子集：三个读工具并发，任何可能改变文件、证据或控制状态的工具独占。并发数应有小上限，取消时要收敛已启动任务，并为未启动调用生成有序失败结果。

### 2. 项目是分组，任务可以不属于项目

Codex 的 [`project.rs`](https://github.com/openai/codex/blob/89650c66f2f3ff0d028d3f5d6d0b187b2ed49be5/codex-rs/app-server-protocol/src/protocol/v2/project.rs) 将项目定义为拥有名称、根目录和元数据的独立对象，线程只保存可选项目关联。DeepSeek 的 [`workspace` 类型](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/workspace/workspace/src/types.ts) 也把 workspace 注册与 session 本身分开。

TraceForge 应采用同样的关系而不是复制完整 API：

```text
Project (0..1) ─────< Run
  id                     project_id 可空
  name                   workspace_snapshot
  canonical_root         task / state / evidence
```

直接任务自动获得一个位于可见托管根目录下的独立工作目录，不要求先创建项目或选择路径；
已有代码通过应用内“添加项目”选择，项目内任务自动继承项目根目录。删除项目注册不得删除
真实目录或历史 Run，删除历史任务也不得连带删除用户产出的目录。

### 3. 上下文压缩不能破坏证据链

Codex 的 [`compact.rs`](https://github.com/openai/codex/blob/89650c66f2f3ff0d028d3f5d6d0b187b2ed49be5/codex-rs/core/src/compact.rs) 会生成替换历史并在上下文超限时继续回退；[`history.rs`](https://github.com/openai/codex/blob/89650c66f2f3ff0d028d3f5d6d0b187b2ed49be5/codex-rs/core/src/context_manager/history.rs) 维护工具调用/结果配对并限制工具输出。DeepSeek 则把压缩和动态运行时上下文保存为可投影事件。

TraceForge 不需要复制全部机制，但必须把以下信息从普通聊天历史中独立出来：已批准计划、实际变更文件、最新检查结果、verifier finding、剩余风险和当前修复轮次。压缩可以丢掉冗长输出，不能丢掉这些事实。

### 4. API key 不得隐式进入模型生成的子进程

DeepSeek 的 [`subprocess` 环境实现](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/subprocess/subprocess/src/index.ts) 会从 ambient env 中移除名称含 `KEY`、`PASSWORD`、`SECRET`、`TOKEN` 的变量，只有可信调用方显式加入的值才会进入子进程。

TraceForge 当前模型客户端与命令执行器同处一个服务进程，因此这是 P0 修复：provider 可以读取 API key，但模型生成的测试命令默认不应继承它。还需用对抗测试证明清理生效。

### 5. 测试用户真正看到的闭环

DeepSeek Harness 的 [`AGENTS.md`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/AGENTS.md) 要求非平凡的用户/模型可见改动具有可无 key 回放的 assembled snapshot；mock 单测与真实 API E2E 各自解决不同问题，不能相互替代。

TraceForge 已具备 fake-provider 闭环、固定产品质量语料和两条真实 DeepSeek 代表场景。
真实运行先后暴露了检查变体审批疲劳与结构化计划容错问题，两者都已形成回归测试；日常
回归保持无 key、确定性，发布前再运行真实模型脚本。

## 明确不复制的部分

- 不复制 DeepSeek Harness 的“everything is a plugin”架构。当前体量下会显著增加依赖注入、生命周期和配置复杂度，却不会提升答辩可证明的质量。
- 不复制 Codex 的完整 app-server 协议、远程 host、云执行、工作树 handoff 和组织级权限。这些属于成熟产品平台能力，不是本项目核心问题。
- 不做宠物、成就动画或无关的多智能体编排。视觉亮点服务于理解执行证据，而不是掩盖执行过程。
- 不把审批当沙箱，也不把关键词黑名单包装成强安全。UI 必须准确区分“策略允许”“用户批准”“OS 强制隔离”三种事实。
- 不为了看起来智能而让多个 agent 并发修改同一工作区。独立 verifier 保留，写入仍由单一 builder 负责。

## 推荐路线

### Phase A：安全与可用入口

1. 子进程环境凭据清理与对抗测试。
2. Provider 配置视图只展示模型、base URL、凭据来源和连接状态；永不把密钥值写入数据库或返回前端。
3. Project 表和 `Run.project_id?`；同时支持直接任务与项目内任务。
4. 首页重构为“快速开始 + 最近项目 + 最近任务”，任务工作台继续复用现有闭环。

### Phase B：质量与速度

1. 三个只读工具有界并发，写/命令/控制工具作为屏障，保证结果顺序。
2. Evidence ledger 与两级上下文压缩。
3. assembled transcript golden tests；真实模型代表场景脚本标准化。
4. Agent / Plan 双模式（已完成）：普通 Agent 默认继续；计划模式始终在实施前等待确认；两者都保留完整 Markdown 计划，越界写入和未知命令仍单独审批。

### Phase C：答辩亮点

1. Proof Pack（已完成）：一页展示需求、规范 Markdown 计划、动作审批、持久化最终 diff、执行命令、新鲜度、完成后只读复核 verdict、回滚状态与完整性摘要。
2. Evidence Timeline：每个结论可以追到原始工具事件，模型总结与机器证据采用不同视觉语言。
3. Failure Lab：内置一个会触发旧测试失效、verifier 退回修复、最终通过的样例；再展示冲突感知回滚拒绝覆盖用户修改。
4. OS 沙箱适配层（已完成）：macOS 使用 Seatbelt，Linux 探测非 setuid Bubblewrap；
   不可用时显式降级为 policy-only，用户单次越界批准记录为 bypass，并进入 Proof Pack。

## 已确定的产品决策

1. 计划审批不再按风险隐式切换：普通 Agent 默认不暂停，计划模式由用户显式开启并始终暂停。
2. OS 沙箱、动作审批、计划模式、完成后只读复核分别表达，不共用一个含糊的“安全开关”。
3. 项目/直接任务、provider 自检和同任务多轮优先于并发多 Agent、插件市场等平台扩张。
