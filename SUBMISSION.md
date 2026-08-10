# FOMO Demo — 交付总结

FOMO 是一个本地运行的 AI Coding Agent 工作台：用户登录后输入产品需求并选择模型，控制面驱动 Pi 在 OpenSandbox 中规划、开发和修复，再由独立干净沙箱执行确定性验收，最终保存版本并提供可交互 Preview。

## 交付入口

- 源码：[github.com/hupicake/fomo-demo](https://github.com/hupicake/fomo-demo)
- 本地运行与架构说明：[README.md](README.md)
- 控制面运行手册：[services/control-plane/README.md](services/control-plane/README.md)
- 公网 Demo：尚未完成 Cloudflare 外网验收，因此不提供未经验证的临时链接

## 1. 实现思路与关键取舍

- 优先完成“需求 → 规划 → 沙箱开发 → 自动修复 → 独立验收 → 版本/Preview”的完整纵向链路，而不是继续堆叠外围页面。
- Pi 在生成沙箱 G 内拥有完整项目开发权限，不用业务文件白名单限制代码架构；`.env*`、路径、符号链接、FOMO 自有验收文件等安全边界仍由控制面保护。
- 当前 Goal 的受保护 Playwright 测试会提前放入 G，供 Agent 在交付前运行类型检查和浏览器自验；该结果只用于快速反馈。正式证据始终来自干净验证沙箱 V 中独立重新编译的验收套件。
- 模型选择采用服务端冻结的 Runtime Contract。浏览器只提交模型与思考强度，服务端固定实际别名、上下文和预算策略；未知、未启用或不兼容组合明确失败，不静默切换模型。
- 规划使用结构化虚拟表单；字段校验失败会把错误反馈给模型继续修正，而不是第一次格式错误就结束。产品提示词按产品经理方式描述用户、目标、完整流程、状态、边界和成功标准，验收条件是下限而不是功能上限。
- 页面展示公开进度、工具活动和闭集失败原因，不展示私有思维链。Context 占用明确标注为 turn 边界快照，不伪装成实时计数。

## 2. 当前完成程度

已完成：

- 注册、登录、退出、开发账号预填、HttpOnly Session 和跨账户项目隔离。
- GPT-5.6、GPT-5.5、DeepSeek Flash、Grok 4.5、Kimi K2.7 Code、Gemini 3.6 Flash、Gemini 3.1 Pro 的运行目录，以及按模型约束的思考强度选择。
- 分模型上下文：GPT 250K、DeepSeek 1M、Grok 500K、Kimi 262K、Gemini 250K；新运行不设累计 token 上限，但 Provider 上下文/输出、TPM、费用、沙箱生命周期、租约和取消仍是实际边界。
- GoalGraph 多目标规划、结构化输出纠错、同一 Pi Session 开发/修复、Verified Checkpoint 与 Worker 恢复。
- 工作日志、Goal 状态、Preview、Code、Terminal、Problems、Versions，以及模型/规划/协议/工作区/验收问题的可解释错误展示。
- 受保护的 G 内 Playwright 自验、V 内独立权威验收、版本快照和 Preview Gateway。
- 删除生产 Demo fixture、Guest 自动会话/项目认领、旧 starter v1 和无消费者的前端组件/状态链；登录成为默认入口。

尚未完成或尚未形成可信证据：

- 尚无稳定公网 URL，也没有从外部网络完成登录 → 生成/SSE → Preview → 交互 → 刷新保持的 Cloudflare 验收。
- 多模型同题能力矩阵未完成；Grok 已达到额度，DeepSeek 曾遇到流中断/超时，不能把“模型可配置”写成“所有模型已稳定交付”。
- 失败任务删除和终态任务原地续跑未实现；当前恢复覆盖 Worker/沙箱故障、Verified Checkpoint 和用户澄清续接。失败后应创建新运行。
- 项目列表仍以名称、状态和时间为主，没有独立的需求摘要字段；生成中的完整文件内容也不会实时从沙箱暴露到浏览器。
- Context Inspector、阶段记忆投影、Verified Reuse、同条件 A/B benchmark 尚未实现。
- 本地 Docker OpenSandbox 适合受控 Demo，不是面向匿名公网代码的强隔离环境；公网开放前仍需 egress、限流和成本保护。

## 3. 如果继续投入时间

P0 — 先保证可交付：

1. 固定一个已验证的默认模型路线（当前优先 GPT-5.5），完成一次真实端到端生成。
2. 配置 named Cloudflare Tunnel，并从外网验收登录、SSE、Preview 静态资源、交互和刷新保持。
3. 给失败任务提供“从最后 verified version 创建新运行”，而不是尝试恢复已经失效的模型进程。

P1 — 改善 Demo 体验：

1. 项目列表补充需求摘要、最近模型、运行阶段和失败原因。
2. 在 Goal 边界展示文件树/架构快照，并把心跳转成“当前动作 + 最后活动时间”，不伪造百分比。
3. 增加模型健康状态和显式的重试/切换模型动作。

P2 — 长期能力：

1. Context Manifest/Capsule、阶段记忆投影和增量索引。
2. Verified Reuse 与相同模型/沙箱条件的 A/B benchmark。
3. 面向公网匿名使用的强沙箱隔离、认证 egress、限流和费用治理。

## 截止版验证记录

- Web：Vitest 14 个文件、97 项通过；TypeScript 检查和 Next.js production build 通过。
- Bridge/启动探针：Node 23 项通过。
- 后端：一次全量回归为 443/447，通过后确认剩余 4 项均是旧测试夹具与新 Runtime Contract/checkpoint 规则不一致；同步夹具后这 4 项定向通过。最终全量复跑按交付要求在 96% 主动停止，停止前未出现新失败，因此不把它写成“447 项完整通过”。
- 全量 Ruff、Compose 配置解析和 `git diff --check` 通过。

以上记录只证明当前代码与本地受控环境，不等同于公网部署验收。
