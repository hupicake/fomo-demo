# FOMO 技术设计（V2）

> 版本：2.3（内部文档版本；同步 P1-A GoalGraph 纵向切片及集中代码验收结果）
> 日期：2026-08-09
> 状态：设计与实施文档。P0「原生 Pi 基线」和 P1-A「GoalGraph / 多目标 / 持久 checkpoint 与恢复 / Goal 工作台投影」代码已落地；最新集中回归为后端 385 项、Web Vitest 134 项、Pi bridge 18 项、本地 Web Playwright 2/2，Web typecheck/build 与 Ruff 亦通过。真实本地固定 runner canary 已通过；10 次生命周期矩阵尚未完成、不能记为通过，也不作为当前笔试 Demo 的发布条件。同条件 A-B、P1-B Context OS 与公网 HTTPS 验收仍未完成，因此不得写成环境已验收或安全收敛（§15）。P2 Verified Reuse / Policy **尚未实现**。

## 1. 执行摘要

FOMO 不是"Pi + 模型 + 工作台"的组合，而是以 **Pi 为可替换执行内核**的 **Goal OS + Context OS + Verified Reuse Path**，并由 FOMO 持久控制面提供验证、版本与诚实性。阶段顺序仍是权威约束：

- **P0（已实施）：原生 Pi 基线。** 取消业务文件冻结与 allowedWritePaths；Base Snapshot 可修改；Pi 在沙箱内使用官方原生工具与全项目开发权限；容器内 QA hardening 与 telemetry 已落地。集中代码回归及真实固定 runner canary 已通过；10 次生命周期矩阵尚未完成且不能记为通过，但不阻塞当前笔试 Demo 发布；同条件 A-B 尚未完成。
- **P1-A（本轮已实施）：GoalGraph 纵向切片。** 服务端确定性多目标执行、非最终 Goal focused QA 与强制 full QA、verified checkpoint、崩溃恢复、usage/沙箱资源持久化、Goal 工作台投影。Context OS 不在此切片。
- **P1-B（后续）：Context OS。** Context Manifest/Capsule/Inspector、索引与 token/cache/provenance 投影。
- **P2（更后）：Verified Registry/Recipe、Policy 数据/评测/微调。** 更不在本轮实现。

**量化承诺**：FOMO 相对原生 harness 的差异必须能在**同模型、同沙箱、同需求、同一外部评测目标与验证器**下量化（§4）；供应商 benchmark 不是项目证据（§14）。

**表述规则**：全文严格区分"已实施（P0、P1-A）/ P1-B / P2 设计预览"，未实现能力一律不得写成已完成。

## 2. 采纳 / 暂不采纳与阶段总表

| # | 评审方向 | 决定 | 阶段与落点 |
| --- | --- | --- | --- |
| 1 | 产品定位：Goal OS + Context OS + Verified Reuse Path，Pi 为可替换执行内核 | **采纳** | P0 定位与量化（§1、§4）；P1/P2 预览（§8–§10） |
| 2 | Golden Starter → 可修改的已验证 Base Snapshot；冻结目标/质量门槛/证据而非文件 | **采纳（P0）** | §5、§7.1 |
| 3 | 沙箱内最大有用权限（/workspace 全量读写等），沙箱外零权限 | **采纳（P0 部分）** | §6.1–§6.3；egress deny 未实现，公共不可信部署为发布阻塞（§6.3、§16） |
| 4 | 自定义 RPC bridge 缩减为传输/观测/取消/资源预算层；不做业务 allowlist、不做 frozen BuildPlan enforcement | **采纳（P0，已实施）** | §6.4 |
| 5 | AcceptanceContract/BuildPlan 演进为可版本化 GoalGraph；同 session 多目标 turn；Goal Manager；claim ≠ verified | **采纳（P1-A，已实施）** | §8；P0 保留 BuildPlan 只读兼容（§7.2） |
| 6 | Context OS：索引 + Manifest + Capsule；Manifest 是建议；工作台 Context Inspector 与 token/cache/provenance 指标；源码优先，不先做图谱/向量库 | **采纳（P1-B，未实施）** | §9 |
| 7 | Verified Reuse Path：通用底座 + shadcn Registry 语义少量 block；Agent 可改可删；权威 QA 重跑；Reuse Manifest；provenance 由 FOMO installer/hash 记录 | **采纳（P2）** | §10 |
| 8 | 模型策略：不先微调主 Coding Model；Policy Model 数据沉淀；Reliable/Fast profile；显式记录、无静默 fallback | **采纳（P2 数据；provenance 自 P0 起）** | §14 |
| 9 | 保留并加强持久控制面、G/V 隔离、外部 QA、unverified preview、Git/版本/provenance、失败不伪造成功 | **采纳（延续基线）** | §3.1、§7 |
| 10 | 按纵向切片实施；不建微调流水线/大型向量库/500 组件库/第二套 Agent 框架/E2B/新 provider/CI/发布平台 | **采纳** | §3.3–§3.4、§13 |
| — | 微调主 Coding Model / 微调流水线 | **暂不采纳（P2+）** | §14 |
| — | 通用知识图谱、全量向量库 | **暂不采纳（P1+ 评估）** | §9.6 |
| — | 500 个组件库、第二套 Agent 框架、E2B/新 provider、CI/生产发布平台 | **暂不采纳** | §3.3 |
| — | MetaGPT/第二套 Agent 框架 | **已退役** | MetaGPT adapter/config/dependency 已删除；`native` SOP 仅保留为兼容路径（§13） |

## 3. 现状基线 / P0 交付 / 后续里程碑

### 3.1 当前基线（P0 + P1-A 已在代码中）

**执行链**：`WorkerRunner`（`worker/runner.py`，DB 租约轮询 + lease 心跳 + 过期 run 恢复 + 陈旧沙箱清理）→ `DirectPiOrchestrator`（`direct_pi/orchestrator.py`）→ `DirectPiSession`（`direct_pi/session.py`，单会话跨 planning/building/repairing）。

**fomo-pi-ds 包**（`fomo_pi_ds/`）：`invocation.py`（PiRequest/PiInvocation，FOMO_PI_* 环境契约，prompt 走 base64、密钥走进程环境、repr 脱敏；无任何业务工具策略）、`rpc.py`（PiBridgeStreamReducer，fail-closed JSONL 解码、public 事件白名单、剥离 thinking/reasoning）、`transport.py`（OpenSandboxPiTransport，前台 exec + interrupt 取消）、`gateway.py`（LiteLLMRunKeyClient，每 run 签发 opaque virtual key，终态 block）。

**Bridge**（`infra/opensandbox/fomo-pi-rpc-bridge.mjs`）：仅传输、事件/usage 观测、取消、总资源/静默/墙钟预算、脱敏、协议 fail-closed、session 复用；`--tools` 为官方 builtin `read,write,edit,bash,grep,find,ls`，无 allowedWritePaths、无 first-write/探索/路径循环 enforcement（§6.4）。

**编排**（`direct_pi/`）：P0 单目标路径仍可通过 feature flag 回退；默认开启的 P1-A 路径先冻结 GoalGraph，再由服务端按拓扑顺序逐目标执行。以下 G/V、审计和固定 runner 边界由两条路径共用：
- PREPARING：冻结 run_input + 固定 starter profile（base+crud+local-persistence）+ 签发 virtual key。
- PLANNING：planning cache 候选（输入指纹筛选 + 已验证 `build_plan`/`acceptance_contract` artifacts 组合 + 当前 parser 严格重验证，不依赖 public 截断消息）或模型产出 PlanningBundle（BuildPlan + AcceptanceContract，schema 严格校验；**BuildPlan 仅展示/咨询**，不驱动文件边界）。
- BUILDING：**一个完整 build turn** 由 Pi 在 /workspace 自主实施（可增删改任意项目文件）；随后 FOMO 用固定 runner 直接 `tsc --noEmit`，失败则一次同 session repair turn；通过后 checkpoint。
- 审计（`workspace.py`）：**保护性安全不变量**（路径规范、拒绝 `.env*`/`.git` 内部/符号链接/非普通文件/二进制/超限文件、changed 文件数预算、FOMO-owned 根 `tests/fomo-acceptance/**` 与 `tests/harness/**` 不可改、系统 .gitignore 不可改）；实际 diff（含修改 starter/config/package/lockfile 与删除）为真相。
- VERIFYING：干净 V 从相同 Base 种子重建 → 应用完整审计 diff → **FOMO 注入 `tests/fomo-acceptance/**`** → **初始 commit 后冻结 manifest（initial_files/initial_hashes 出自同一次 list），在候选进程启动前用 `HEAD == commit_sha` + `git status --porcelain=v1 --untracked-files=all` 为空校验绑定（经验证一致，非原子快照）** → `pnpm install --offline --frozen-lockfile --ignore-scripts` → 固定 runner 的 `next dev` 健康检查（发 `preview.available` unverified）→ 固定 runner `tsc --noEmit` → 按 GoalGraph 选择 QA scope → 重注入/恢复 FOMO-owned 测试并重验 hash → harness smoke → FOMO-owned 验收测试 → 全绿。非最终 Goal 默认走 **focused QA**：跳过 `next build`，只执行当前 Goal AC；**full QA** 保留 `next build` 并执行全部 verified Goal + 当前 Goal AC，且最终 Goal、项目级配置变更、跨 Goal 重复修改既往 Goal 文件、缺少 `goalChangedPathsByGoal` 的 legacy checkpoint、verified graph recovery 均强制 full。**`preview.verified` 不在 gate 后立即发出**：发布前必须通过最终一致性检查（live V 可见源文件 hash 与冻结快照一致）并成功创建版本后才发出。**typecheck/build/dev 均为 FOMO-owned 固定 runner：在只含 root-owned 目录的固定 PATH 下，直接执行 runtime cache 中 pnpm 生成的 `#!/bin/sh` 绝对 wrapper；wrapper 为 root-owned `0555`，并解析受信系统 Node。不信任模型可改的 package scripts，也不从候选 `node_modules/.bin` 解析。此为保证容器内 runner 完整性，不是 host-level anti-tamper。**
- REPAIRING：最小结构化 DiagnosticReport 回传同一 session；修复 turn 同样可修改完整候选（仍只在 G）。P1-A 在 session/进程崩溃或租约过期后不恢复孤儿进程，而是从最近 verified checkpoint 新建 G 与 Pi session，并按持久 usage 余额续跑（§7.4）。
- 发布：最终一致性检查（live V 源文件 hash == 冻结快照，失败即 fail closed、不创建版本）→ **最终 preview 健康复查**（dev server 中途退出或 URL 缺失即 fail closed）→ 已验证 OpenSandbox V 强制续期到有界 7 天（失败不发布）→ 显式 `git tag version/N <冻结commit_sha>` → `create_version(files=冻结 initial_files, commit_sha=冻结 commit_sha)` → 配置 `PUBLIC_PREVIEW_BASE_DOMAIN` 时仅在最终原子发布中把内部 endpoint 替换为 `https://<sandbox-id>.<domain>/` → 成功后才发 `preview.verified` → trace links → READY。

**契约**（`contracts.py`、`acceptance.py`）：AcceptanceContract DSL（criteria 1–8、tests 一对一、goto/click/fill/select/reload + visible/value/url 断言），`compile_acceptance` 确定性编译 FOMO-owned Playwright 测试（只在 V 注入）；`tests/generated/**` 仅为 G 内自检，永不作为证据。

**沙箱**（`sandbox/`）：SandboxProvider 抽象（base/fake/process/opensandbox）；OpenSandbox v0.2.2 + SDK v0.1.15。可选 `preview_gateway.py` 以独立 wildcard Host 代理数据库确认的当前 verified sandbox；浏览器不会拿到随机 `localhost` 宿主端口，`/_next/*` 等根路径资源保持同一 Preview Origin；网关剥离账户 Cookie、Authorization、forwarding header 与 OpenSandbox key，并只对 Provider 明确 404/410 收敛 `preview.expired`。**无 egress 策略开关**：本地 config.toml 没有受认证 dns+nft sidecar，任何看似 fail-closed 的 network-policy 都可被未认证 policy API 绕过，故不提供产品开关（§6.3）。

**持久化**（`persistence/`）：runs/leases/events（seq 单调）/artifacts/trace links/versions/planning cache/acceptance items；P1-A 增加 GoalGraph/nodes、checkpoint files/evidence、usage ledger、sandbox resources、租约 fencing 与 checkpoint 恢复。显式 Alembic migration 兼容既有 P0 SQLite；未知旧结构失败关闭。

**前端**（`apps/web/`）：workbench（project-workbench / role-timeline / GoalGraph panel / spec-proof / workspace / home-screen）、事件 reducer（未知事件默认容忍）；snapshot 与实时事件都消费服务端权威 GoalGraph 投影，P0 项目保持 `goalGraph: null`。

**配置**：`AGENT_FRAMEWORK=direct_pi` 默认；`DIRECT_PI_GOAL_GRAPH_ENABLED=true` 默认并可回退 P0；`run_max_*` 预算（wall/token/tool/spend，cacheRead 不计入新处理 token）；`PI_CONTEXT_WINDOW` 显式传入 bridge（默认 200000，作为 FOMO 统一逻辑上下文预算）；bridge 在每次运行的私有 `PI_CODING_AGENT_DIR` 写入受控 `settings.json`，固定启用 compaction，并设置 `reserveTokens=32768`、`keepRecentTokens=20000`；LiteLLM 别名 `fomo-pi-flash`（deepseek-v4-flash，thinking enabled）与 `fomo-pi-build`（gpt-5.5）。

**待 P1-B/P2 演进的对象**：Context Manifest/Capsule/Inspector（P1-B）、starter 目录（P2 收敛为通用底座 + registry）。

### 3.2 P0 交付边界（本轮已实施）

| 模块 | 改动（已实施） |
| --- | --- |
| `infra/opensandbox/fomo-pi-rpc-bridge.mjs` | 官方 builtin read/write/edit/bash/grep/find/ls；移除 PiToolPolicy/allowedWritePaths/探索/首写/路径循环 enforcement；保留传输/观测/取消/静默/墙钟/超时预算/脱敏/fail-closed/session 复用；context window 改为显式 env（默认 200000）；每次运行只在随机私有 `PI_CODING_AGENT_DIR` 写 root/runtime 受控 `settings.json`（compaction enabled、reserve 32768、keepRecent 20000），与持久 `sessions/` 分离且不含密钥；新增 telemetry（首工具/首个 edit/write 工具相对耗时 `firstEditOrWriteToolElapsedMs`（不含 bash 写入）、分工具计数、lastStopReason，随 tool 事件与 completed 落库） |
| `fomo_pi_ds/invocation.py` | 删除 PiToolPolicy 与 FOMO_PI_TOOL_POLICY 环境传递；新增 `context_window`、`activity_silence_seconds` 契约 |
| `direct_pi/session.py` | invoke 不再要求 allowed_write_paths；building/repairing turn 携带工具静默预算（120s）；run-total 预算不变 |
| `direct_pi/contracts.py` | 删除 `validate_plan_write_scope`；BuildPlan/AcceptanceContract schema 与 planning cache 保留（只读兼容） |
| `direct_pi/prompts.py` | planning prompt 明示 plan 仅咨询；build prompt = 完整项目自主实施（可改 package/config/starter），frozen 部分仅需求与验收；repair prompt 同；依赖约束提示（离线预取 store） |
| `direct_pi/orchestrator.py` | 删除 `compile_build_batches`/`interface_ledger`/计划 write scope；单 build turn + 直接 typecheck + 一次 repair + checkpoint；事件 `build.turn.started/completed`；`file.changed` 区分 modified/deleted；删除 build_plan_amendment 工件 |
| `direct_pi/workspace.py` | Base Snapshot 可修改；settle 审计：真实 changed/new 才进 diff、FOMO-owned 存在且未变则排除、`.env*` 拒绝、lockfile 允许 512KiB；`_seed` 按 version manifest 恢复并删除 starter 中已删除文件（跨 run 保留删除）；G 不含 `tests/fomo-acceptance/**`（V 注入）；typecheck 用固定 runner |
| `direct_pi/batching.py` | 删除（无其他引用） |
| `direct_pi/verification.py` | gate 在固定 root-owned PATH 下直接执行 runtime cache 中 root-owned `0555` 的 pnpm `#!/bin/sh` 绝对 wrapper；GoalGraph 非最终 focused / 强制 full QA；依赖安装 `--ignore-scripts`；runner 探测与 FOMO-owned 恢复/重验 gate；普通依赖失败分类为可修复；`preview.available/verified` 携带 `elapsedSeconds` |
| `config.py` / `.env.example` / `compose.yaml` | 新增 `PI_CONTEXT_WINDOW`；无 egress 策略开关（已撤回，见 §6.3） |
| `sandbox/opensandbox.py` / `sandbox/__init__.py` | 撤回 network-policy 假开关（无受认证 dns+nft sidecar，见 §6.3）；capabilities 恢复 `network_policy=False`；copy_starter 解引用 node_modules（候选可写、cache 只读） |
| 数据/事件 | 无新表；新事件 `build.turn.*`；`pi.tool.*`/`pi.completed` 增 telemetry 字段；旧事件全部保留 |
| 文档 | DESIGN.md（本文）、根 README、`services/control-plane/README.md` 同步 P0 状态 |

上述是 P0 当时的交付边界；其中 GoalGraph、对应 migration/API/前端面板已由本轮 P1-A 补齐。Context Engine、ts-morph/BM25、向量库、registry installer、benchmark runner 与微调流水线仍未实现。

### 3.3 P1-A GoalGraph 纵向切片（本轮已实施）

- GoalGraph schema 与服务端质量门槛；Goal Manager 按 `dependsOn` 确定性选择、activate/claim/retry/verify，模型不能自选目标，`claim ≠ verified`。
- 每个 Goal 的 acceptance ID/持久键/Playwright 路径隔离；非最终 Goal 默认 focused（跳过 `next build`，仅当前 Goal AC），最终 Goal、项目级配置变更、跨 Goal 重复文件、legacy checkpoint 无 `goalChangedPathsByGoal`、verified graph recovery 强制 full（保留 build，执行 verified + 当前 Goal AC）。
- 每个 verified Goal 原子保存完整候选 checkpoint、manifest 与证据；恢复时销毁孤儿沙箱、创建新 Pi session，并按持久 usage 余额从最近 verified checkpoint 继续。
- generation/verification sandbox resource 登记与确认清理；成功发布的当前 verified preview 保留，引用改变后回收。
- 发布事务原子提交 version/files、project head、verified trace、preview/summary 与 run succeeded；取消或失租零提交并可按 run+commit 幂等重试。
- GoalGraph snapshot/read projection 与实时事件携带权威 acceptance/evidence/terminal 状态；工作台展示 Goal 树、active/claimed/verified 与证据数量，P0 为空态兼容。
- 最新集中回归为后端 385 项、Web Vitest 134 项、Pi bridge 18 项、本地 Web Playwright 2/2，Web typecheck/build 与 Ruff 亦通过；真实本地固定 runner canary 已通过；10 次生命周期矩阵尚未完成且不能记为通过，但不作为当前笔试 Demo 的发布条件；Context OS、同条件 A-B 与公网 HTTPS 验收尚未完成（§15）。

### 3.4 后续里程碑

- **P1-B**：Context Manifest/Capsule/Inspector（§9）与源码优先索引。
- **P2**：Verified Registry/Recipe（§10）、Policy 数据/评测/微调（§14）。
- 其余暂不采纳项见 §2 表。

## 4. 产品定位与量化（P0）

### 4.1 定位

- Pi 是**可替换执行内核**：通过 fomo-pi-ds 契约嵌入 read/write/edit/bash 循环；FOMO 的全部语义（目标、上下文、证据、版本）不依赖 Pi 内部实现。
- P0 交付的是**同条件可测量的内核基线**：原生工具 + 可修改 Base + 可信 QA 边界 + telemetry；Goal/Context/Reuse 语义在 P1/P2 叠加并继续用同一基线量化。

### 4.2 对照方法（同条件 A/B）

- **同一外部评测目标与验证器**：local Pi、sandbox-native Pi（同一 OpenSandbox 镜像内裸 Pi CLI）、FOMO 三者共享同一需求文本、同一模型 profile、同一沙箱镜像、同一 FOMO-owned 验证器与验收判据，才能互相比较。
- 生产事件即可提供数据（§12.4），**不实现、不执行 benchmark runner**；A/B 执行属于集中验证矩阵的一部分，尚未执行，本轮只让事件可产出数据。

### 4.3 量化指标定义

| 指标 | 定义 |
| --- | --- |
| 目标完成率 | 验收目标中 verified 数 / 总数（P1 起按目标；P0 按 run 级 AC） |
| QA 首过率 | 首轮验证全绿的 run 数 / run 总数 |
| 修复轮数 | run 从首次验证失败到 verified 的轮次分布 |
| 新处理 token | input+output+cacheWrite（cacheRead 单列），按 turn/stage 汇总 |
| 工具节奏 | first tool、first edit/write tool 相对耗时（**不包含 bash 写入**，bridge 不解释工具语义）、分工具计数、finish reason |
| 首 preview 耗时 | run 开始到 `preview.available` 的 elapsedSeconds |
| cache 命中率 | cacheRead / (input+cacheRead) |
| 诚实性 | unverified→verified 仅发生在证据落库后；零"假成功"事件 |

## 5. Base Snapshot 与契约演进（P0 已实施，P1/P2 演进）

### 5.1 Base Snapshot（P0 已实施）

- Starter 仍为服务端固定的 base+crud+local-persistence 组合作为初始种子；**P0 起该快照可修改**：Pi 可增删改移普通 package/config/routes/app shell/components/ui/starter/普通测试文件；每个版本保存完整的已验证候选快照，后续 run 以该快照为种子。
- 由于 Base 可变，**每个新版本都在 V 中重跑全部权威 QA**，不存在"Base 已验证所以跳过"。
- P0 保留的安全不变量（settle 审计，非逐工具拦截）：路径规范且留在 workspace；**`.env`/`.env.*` 出现即拒绝**（无完整内容 secret scanner，仅路径拒绝 + 事件 redaction）；`.git/**` 为 G 内 checkpoint，排除出候选；拒绝符号链接/设备或非普通文件、changed/new 文件的二进制/超限检查（`pnpm-lock.yaml` 允许至持久化上限 512KiB）；changed 文件数预算；FOMO-owned 根（`tests/fomo-acceptance/**`、`tests/harness/**`，存在且 hash 未变则排除出 diff，增删改拒绝）与系统 .gitignore 不可改。

### 5.2 契约（P0 形态）

- **BuildPlan：仅展示/咨询**。schema、planning cache 与旧事件保留（只读兼容）；BUILDING 不再按计划文件分批、不校验 model-owned 路径、不把计划 paths 当 write scope；模型可按实现证据调整文件拓扑，但不得改变用户需求与冻结的 AcceptanceContract。
- **AcceptanceContract**：仍是本轮冻结的验收真相（criteria + 有界 DSL 测试）；逐 AC 证据只来自 V 内 FOMO-owned 测试。
- **P1 演进**：AcceptanceContract/BuildPlan 由可版本化 GoalGraph 取代（§8）；P0 的 AcceptanceContract 语义保留为 GoalGraph 的逐目标 acceptance 子集。

## 6. 执行内核：Pi 在沙箱内的权限与边界（P0）

### 6.1 沙箱内：最大有用权限（P0 已实施）

fomo-pi-ds 在生成沙箱 G 内拥有 `/workspace` **全量读写**：可增、删、移动项目文件（含 package.json、锁文件、config、starter 底座、路由、业务代码）；`pnpm install/add/remove`、dev server、Git 命令（bash 下 status/diff/log 与 commit/tag 等写操作均可执行，但 G 内**无权威发布语义**，候选 commit/tag 与版本只由 FOMO 在 V 内完成）、Playwright 自检（结果仅供参考）；不受业务路径白名单、不受 frozen BuildPlan 文件清单、不受"model-owned 根"限制。

### 6.2 沙箱外：零权限（P0 目标，部分待验证）

- 不得接触宿主机文件系统、Docker socket、其他项目、控制面 DB、MinIO/对象存储、LiteLLM 管理端点、外部 QA/版本记录。
- G 内 Git 命令（含 commit）可执行但**无权威发布语义**：Git 候选 commit/tag 与版本只由 FOMO 在 V 内完成；`tests/fomo-acceptance/**` 不进入 G（由 FOMO 在 V 注入）。
- 密钥策略：G 内只有每 run opaque virtual key；master/provider key 绝不进入 G。
- **诚实标注**：**没有 egress deny-by-default**。本地 config.toml 无受认证 `dns+nft` egress sidecar，未认证的 policy API 可被 workload 访问/改写，host-only rule 还会放行 host 网关其他端口——因此不提供看似 fail-closed 的网络策略开关。本地可信开发可用；公共不可信部署在受认证 dns+nft/credential proxy 与真实验证完成前是**发布阻塞**（§16）。

### 6.3 边界由什么提供

真正的安全边界来自基础设施，**不来自提示词或业务 allowlist**：

| 层 | 边界 | 状态 |
| --- | --- | --- |
| OpenSandbox 进程权限 | Pi 以受限沙箱用户运行（非 root）；bridge 仅管理生命周期；无 host 挂载、无 docker socket、无特权能力 | 已实施（镜像/Dockerfile 现状） |
| workspace 隔离 | /workspace 为该项目专用卷；沙箱外路径不可达；FOMO-owned 测试不进入 G | 已实施 |
| 网络与凭据策略 | 仅注入每 run opaque virtual key（TTL 兜底 + 终态 best-effort block）；**无 egress 白名单** | **发布阻塞**：公共不可信部署需受认证 dns+nft/credential proxy + 真实验证（§16）；本地可信开发不受影响 |
| FOMO settle 审计 | 结果检查（§5.1），不是逐工具拦截 | 已实施 |

### 6.4 RPC bridge 缩减（P0 已实施）

- **保留**：传输（prompt/密钥环境契约）、观测（public 事件流、脱敏、剥离 thinking）、取消（interrupt/grace）、资源预算（工具静默、墙钟/超时、stdout/stderr 上限）、fail-closed 协议（schemaVersion、生命周期、final state/stats）、session 复用。
- **移除**：`allowedWritePaths` 业务文件 allowlist、frozen BuildPlan enforcement、"protected starter/package/config 不可改"的桥接强制、探索调用/首写 deadline/路径循环 enforcement。
- bridge 不代理、不解释 Pi 原生 read/write/edit/bash 语义；它只是 FOMO 与 Pi 之间的**管道 + 仪表**。
- 上下文窗口：`FOMO_PI_CONTEXT_WINDOW` 显式传入，默认 200000，作为跨配置、调用与 bridge 一致的 FOMO 逻辑预算。bridge 在随机私有且运行结束即清理的 `PI_CODING_AGENT_DIR/settings.json` 中固定 `compaction.enabled=true`、`reserveTokens=32768`、`keepRecentTokens=20000`，因此约在 167232 tokens 进入压缩；设置文件不写入持久 session 目录，也不包含模型密钥。

### 6.5 可信 QA 边界（P0 已实施；性质为 hardening）

- 权威 gate 使用 **FOMO-owned 固定 runner**：单处 helper 为 G/V 设置只含 root-owned 目录的固定 PATH，并直接执行 runtime cache 中 pnpm 生成的 `#!/bin/sh` 绝对 wrapper；`tsc`、`next`、`playwright` wrapper 均为 root-owned `0555`，由固定 PATH 解析受信系统 Node。镜像内 PNPM_HOME、COREPACK_HOME、`/ms-playwright`、`/opt/fomo/pi`、runtime-cache 全部 root:root + a-w，候选可写 pnpm store 单独 chown node + `u+rwX`。**不从候选 `node_modules/.bin` 解析**，候选 package.json scripts 不参与任何 gate。依赖安装为 `pnpm install --offline --frozen-lockfile --ignore-scripts`（阻断候选 lifecycle scripts）。
- FOMO-owned acceptance/harness 在候选代码执行后、Playwright 前**重新注入/恢复并重验 hash**；G 内自检（含 `tests/generated/**`）永不作为发布证据。
- **诚实边界**：固定 runner cache 的 root-owned/read-only 隔离（与候选可写 store 无共享 inode，见 Dockerfile）只是**容器内 hardening**：候选 Next config/app 与测试仍运行在 V 的同一用户/进程边界内。发布一致性靠**gate 前冻结 V manifest/commit + 发布前源文件 hash 一致性检查**保证；但同用户候选进程对可写 acceptance/harness 做**短暂篡改并恢复的 adversarial race 没有解决**（TOCTOU/同用户 race）。这不是 host-level/cryptographic anti-tamper；真正外部 QA runner / 测试目录只读挂载仍是公共不可信部署的**发布阻塞项**。
- **依赖预取限制**：新依赖的 V 安装闭环未完成真实验证，本轮诚实限制到预取依赖（镜像内离线 store）；模型在 G 内 `pnpm add` 的新依赖若不在 store，V 的离线安装 gate 将失败并如实呈现，不声称任意 `pnpm add` 已可发布。
- **失败分类**：普通非零依赖安装属于可修复的 source/package 问题（diagnostic 回 repair）；仅 transport/timeout、固定 runner 缺失、FOMO-owned 恢复失败等才是 infrastructure failure（needs_attention）。

### 6.6 受控评审公网拓扑（配置已收口，尚未外网验收）

- **Origin 最小暴露**：named Cloudflare Tunnel 从宿主机主动出站；公网只看到 Cloudflare HTTPS，origin 不接收入站 Internet 连接。若改用传统 ingress，防火墙也只能开放 TLS 入口。OpenSandbox `8080`、LiteLLM `4000`、Docker 随机 Preview 端口（含 `40000-60000`）、PostgreSQL、Redis、MinIO 均不得公网直达。
- **不同站点**：工作台示例为 `app.example.com`，生成应用为 `<sandbox-id>.fomo-previews.example.net`，两者使用不同 eTLD+1。`/v1/*`/`/health` 与 Web 可由同一 named Tunnel 按 path 分流；wildcard Host 原样传给 Preview gateway，不得用固定 `httpHostHeader` 覆盖 UUID。Preview wildcard 需要匹配其实际深度的 TLS 证书，并配置全站 cache bypass，避免已过期 sandbox 被边缘缓存继续返回。
- **Gateway 最小凭据**：Compose 的 `preview-gateway` 不继承 API/worker 的全量环境，只获得 DB URL、OpenSandbox lifecycle URL/key、Preview domain、host override 与监听端口；不得获得 LiteLLM master/provider key、Redis、MinIO 或 AWS 凭据。
- **能力边界**：当前 Preview gateway 是有界的普通 HTTP request/response 代理，覆盖本 Demo 的 Next.js 页面与 `/_next/*`；不承诺 generated-app WebSocket、SSE 或任意流式传输。
- **存活边界**：成功发布只将 verification sandbox 续期一次，最长 7 天；评审期间必须保持 Docker/OpenSandbox/gateway/Tunnel 运行，并在到期前由受信运维续期或重跑。
- **证据边界**：内部 endpoint 健康、localhost Host gateway 与本机 Playwright 通过，均不等于公网完成。只有外部网络通过真实 DNS/TLS/Tunnel 完成账户登录、一次真实生成与 API SSE、最终 HTTPS Preview、`/_next/*`、交互及 reload 持久化，才可把在线链接标为已交付。

此拓扑只用于受控评审者访问，不改变 §6.2/§6.5 对匿名不可信公共生成和外部 QA 隔离的发布阻塞判断。可执行的无密钥 ingress 形状见 `deploy/cloudflared/config.example.yml`；Tunnel credentials 永不进入仓库。

## 7. 编排与状态机（P0 已实施）

### 7.1 阶段状态机（run 级）

```text
PREPARING → PLANNING → [BUILDING ⇄ REPAIRING] → VERIFYING ──┬─ 通过 → READY
                                                             └─ 失败/预算耗尽 → FAILED / NEEDS_ATTENTION
```

run 级阶段：PREPARING / PLANNING / BUILDING / VERIFYING / REPAIRING / READY / FAILED（NEEDS_ATTENTION 表示基础设施或验证受阻；**不保证有 preview**），由 FOMO 独占转换。

### 7.2 P0 单目标流程

1. PLANNING：模型产出 PlanningBundle；FOMO schema 校验并冻结；BuildPlan 落库仅作展示/咨询；`compile_acceptance` 生成 FOMO-owned 测试（暂存，不写入 G）。
2. BUILDING：**同一个 DirectPiSession 一个完整 build turn** 在 /workspace 自主实施（原生工具、全项目权限）；FOMO 直接 typecheck，失败则一次同 session repair turn；checkpoint。
3. 审计：以实际文件系统 diff 为真相（§5.1 不变量）；`file.changed`（modified/deleted）事件按审计结果发出。
4. VERIFYING：V 从相同 Base 种子 + 完整审计 diff + 注入 acceptance 重建；**创建初始 commit 后立即冻结 manifest/commit 快照**（此后不再二次 list 作为发布真相）；gate 顺序见 §6.5；dev server health 2xx 即 `preview.available`（unverified）。**保留语义（精确）**：一旦进入 repair，当前 V 即销毁、preview URL 清空并发 `preview.expired`，repair 期间**没有** preview；仅当修复轮耗尽且当时已有健康 preview 时才 best-effort 保留（NEEDS_ATTENTION 不保证有 preview）；infrastructure failure（runner/restore/超时）会清除 preview。
5. 全绿 → **最终一致性检查（live V 源文件 hash == 冻结快照，漂移即 fail closed）** → **最终 preview 健康复查（失败或 URL 缺失即 fail closed）** → 显式 tag 指向冻结 commit_sha → 版本只持久化冻结 manifest → 版本创建成功后才发 `preview.verified` → Publish/READY。
6. 失败 → REPAIRING：V 销毁并清空 preview（`preview.expired`）；最小结构化 DiagnosticReport 回传同一 session；修复 turn 可修改完整候选；达到 `max_repair_rounds` 或预算耗尽即停（**无 fingerprint 去重**）。

### 7.3 验证与 repair

- 证据只来自 V 内 FOMO-owned `tests/fomo-acceptance/**`；`tests/generated/**` 与 Pi 自检永不作为发布证据。
- DiagnosticReport 保持最小结构化（gate 状态、失败项、阻断问题、affectedFiles、修复建议、当前/最大轮次），不包含无筛选终端输出。

### 7.4 预算与恢复（P1-A 已补齐）

- run-total 非重置（墙钟/token/tool/spend，cacheRead 单列）+ per-command（超时/输出字节）；修复轮次不重置；耗尽即停（**无“预算耗尽后最终 V 验证”**：预算内未 verified 即诚实失败）。
- 持久真相：run_input、GoalGraph、逐目标 checkpoint files/manifest/evidence、usage ledger、sandbox resource、版本记录。可丢弃缓存：G/V、runtime snapshot、pi_sessions、transcript（重建前按 manifest hash 校验）。
- session 崩溃/租约过期时不 resume 孤儿进程、不以未审计 working tree 为真相：先确认清理登记的 G/V，再创建新 Pi session，从最近 verified checkpoint 重建；无有效 checkpoint 才从 Base 重建。
- provider 调用前 reserve request ID，完成后幂等 settle usage；即使取消/失租发生在 provider 返回后，已发生用量仍可结算。调用前预算达到上限（`>=`）即拒绝新 turn。
- deterministic command、verification、checkpoint 与 publish 之间均检查取消/租约栅栏；最终发布事务再次拒绝取消或失租，取消优先于 succeeded。

## 8. GoalGraph（P1-A 已实施）

默认 P1-A 路径以 GoalGraph 取代 frozen BuildPlan/AcceptanceContract 单合同作为目标真相；feature flag 关闭时仍可回退 P0：

- 冻结**用户目标、质量门槛与证据**，不冻结文件数量/拓扑/业务路径；每个目标含 productOutcome、qualityBar、用户可见纵向 goals、dependsOn、acceptance、status、revision/amendment/provenance。
- **Goal Manager**（控制面组件）按 dependsOn 拓扑序决定 active goal；每项目同一时刻一个 active goal、一个写执行者；模型不能自选目标。
- **claim ≠ verified**：模型"完成"声明只置 `claimed`；V 内 FOMO-owned 证据落库才 `verified`。
- **Goal acceptance adapter（P1 必须）**：P0 的 AcceptanceContract 语义作为单目标 acceptance 子集兼容接入；逐目标编译 FOMO-owned 测试。
- **GoalGraph QA scope（已实施）**：非最终 Goal 默认 focused，跳过 `next build` 且只执行当前 Goal AC；full 保留 build 并执行全部 verified Goal + 当前 Goal AC。最终 Goal、项目级配置变更、跨 Goal 重复修改既往 Goal 文件、legacy checkpoint 缺少 `goalChangedPathsByGoal`（影响范围 unknown）、verified graph recovery 一律 full。
- **完整候选 checkpoint/恢复（P1 必须）**：每 verified 目标持久化候选快照与证据，run/会话崩溃后从最近 verified 目标续跑，而非回到 run 起点。
- revision/amendment 必须携带 reason + provenance；禁止静默改写冻结内容。当前纵向切片固定 revision 1，后续显式 amendment API 尚未开放。

精简 schema 示例：

```json
{
  "schemaVersion": 1,
  "graphId": "0197c9...",
  "projectId": "0196a1...",
  "productOutcome": "可运行的深色 CRM 销售仪表盘：KPI、趋势图、交易表格、详情抽屉、手机布局",
  "qualityBar": {
    "gates": ["deps", "typecheck", "build", "harness-smoke", "per-goal-acceptance", "preview-health"],
    "mustAcceptance": "all",
    "releaseEvidence": "fomo_qa_only"
  },
  "goals": [
    {
      "goalId": "G-1",
      "title": "数据模型与本地持久化",
      "userVisible": true,
      "dependsOn": [],
      "acceptance": [
        {"acceptanceId": "AC-1", "priority": "must",
         "given": "打开仪表盘", "when": "新建一条交易", "then": "刷新后记录仍存在"}
      ],
      "status": "verified",
      "evidence": [
        {"kind": "fomo_qa_test", "ref": "tests/fomo-acceptance/ac-1.spec.ts",
         "status": "passed", "runId": "0197c9...", "recordedAt": "2026-08-09T09:00:00Z"}
      ]
    },
    {
      "goalId": "G-2",
      "title": "KPI 卡片与趋势图",
      "userVisible": true,
      "dependsOn": ["G-1"],
      "acceptance": [
        {"acceptanceId": "AC-2", "priority": "must",
         "given": "存在至少一条交易", "when": "访问首页", "then": "展示 KPI 与趋势图"}
      ],
      "status": "active",
      "evidence": []
    }
  ],
  "status": "active",
  "revision": 2,
  "amendments": [
    {"revision": 2, "reason": "用户追加手机布局要求", "provenance": "run:0198b2...", "at": "2026-08-09T10:00:00Z"}
  ],
  "provenance": {"createdBy": "run:0197c9...", "frozenAt": "2026-08-09T09:00:00Z", "supersedes": null}
}
```

目标级状态机：`pending → active → claimed → verified | superseded`，以及 `failed`（预算/轮次耗尽）。

## 9. Context OS（P1-B 设计预览，未实现）

- 项目索引（文件、package capabilities、Next route、TS symbol/import、测试、最近 diff、registry 使用）；**源码优先结构索引 + BM25，不先做通用知识图谱或全量向量库**。
- Context Manifest：选择与排除理由；**advisory，非读取 allowlist**（模型仍可自由读取 /workspace 任意文件）；稳定前缀 + provider-cache 友好排序。
- Context Capsule：已完成目标、决策、接口、changedFiles、证据、问题、下一目标、avoidRepeating；随目标更新并注入下一目标 prompt。
- 工作台 Context Inspector 与 token/cache/provenance 指标（P1 前端面板）。
- 刷新按目标增量（事件驱动），不整库重建；Manifest 由规则 + avoidRepeating 生成，不引入学习模型。

精简 schema 示例（Manifest 与 Capsule）：

```json
{
  "schemaVersion": 1,
  "runId": "0197c9...",
  "goalId": "G-2",
  "advisory": true,
  "selected": [
    {"itemId": "file:app/(generated)/composition.tsx", "type": "file", "reason": "当前目标入口，必读"},
    {"itemId": "symbol:useCrudCollection", "type": "ts_symbol", "reason": "G-1 交付接口，本目标消费"},
    {"itemId": "test:ac-1.spec.ts", "type": "test", "reason": "G-1 验收证据，防回归"}
  ],
  "excluded": [
    {"itemId": "dir:components/features/orders", "type": "dir", "reason": "G-3 范围，本轮不读"}
  ],
  "ordering": "stable-prefix:requirement|goal|starter|capsule|reuse|files|tests",
  "tokenEstimate": 18400
}
```

```json
{
  "schemaVersion": 1,
  "runId": "0197c9...",
  "goalId": "G-2",
  "completedGoals": [{"goalId": "G-1", "verifiedAt": "2026-08-09T09:10:00Z", "evidenceRefs": ["verification_evidence:0197c9-e1"]}],
  "decisions": [
    {"decision": "交易数据使用 local-persistence 的 useCrudCollection",
     "rationale": "复用 Base 能力，避免自造 storage",
     "source": "run:0197c9...", "appliesTo": ["file:lib/domain/sales.ts"]}
  ],
  "interfaces": [
    {"symbol": "useCrudCollection", "signature": "<T>() => { state, actions }",
     "exportedBy": "lib/domain/sales.ts", "consumedBy": ["app/(generated)/composition.tsx"]}
  ],
  "changedFiles": ["lib/domain/sales.ts", "app/(generated)/composition.tsx"],
  "evidence": [{"gate": "typecheck", "status": "passed", "ref": "run:0197c9..."}],
  "issues": [],
  "nextGoal": "G-3",
  "avoidRepeating": ["不要重定义 storage key", "不要改动 tests/fomo-acceptance/**", "图表库用 Base 已装版本"]
}
```

## 10. Verified Reuse Path（P2 设计预览，未实现）

- Starter 进一步收敛为通用底座；以 shadcn Registry 语义维护少量可验证 block/recipe：来源、commit/version、依赖、兼容性、测试、许可证。
- Agent 可自由修改/删除复用代码；**最终必须在当前 candidate 上重跑权威 QA**。
- **provenance 由 FOMO installer/hash 记录（P2 必须）**：安装 = FOMO 按 sourceRef 拷贝 + 记录文件 hash 与版本；`modified`/`removed` 状态由 FOMO 对照 hash 判定，**不由 Agent 自报**。
- 工作台 Reuse Manifest 投影（P2 前端面板）。

精简 schema 示例：

```json
{
  "schemaVersion": 1,
  "runId": "0197c9...",
  "entries": [
    {
      "blockId": "registry:shadcn-ui/table",
      "source": "shadcn/ui registry",
      "sourceRef": {"commit": "abc123", "version": "table@2.1.8"},
      "dependencies": ["@tanstack/react-table@8"],
      "compatibility": {"next": ">=14", "react": ">=18", "tailwind": ">=3.4"},
      "license": "MIT",
      "tests": ["tests/fomo-acceptance/ac-3.spec.ts"],
      "installedFiles": ["components/ui/table.tsx"],
      "fileHashes": {"components/ui/table.tsx": "sha256:..."},
      "modified": true,
      "verification": {"required": "authoritative-qa-on-candidate", "lastVerifiedIn": "run:0199d0...", "status": "pending"}
    }
  ]
}
```

## 11. 事件与数据契约（P0 + P1-A 已实施）

### 11.1 事件信封（沿用）

```text
id: 42
event: run.event
data: {"schemaVersion":1,"eventId":"019...","seq":42,"projectId":"...","runId":"...",
       "kind":"build.turn.completed","stage":"building","occurredAt":"2026-08-09T10:00:00Z",
       "payload":{...}}
```

`seq` 单 run 单调；先落库后 Redis 通知；SSE 断线按 `after` 回放。

### 11.2 P0 事件变化

- 新增：`build.turn.started`、`build.turn.completed`。
- `pi.tool.started` / `pi.tool.output` / `pi.tool.completed` 增 `elapsedMs`（相对 started 信封）；`pi.completed` 增 `telemetry`（firstToolElapsedMs、firstEditOrWriteToolElapsedMs、toolCounts、lastStopReason）；`pi.started` 增 `contextWindow`。
- `preview.available` / `preview.verified` 增 `elapsedSeconds`。
- `file.changed` 的 status 区分 `modified` / `deleted`。
- 保留全部既有事件。**拆开 legacy 语义**：`build.batch.*` 只做历史 run 回放，新 run 不再产生；`planning.cache_hit` 历史可回放且**新 run 仍可能产生**（planning cache 按输入指纹筛选 + 已验证 `build_plan`/`acceptance_contract` artifacts + 当前 parser 重验证，与 run 终态无关）。

### 11.3 数据

P0 没有新增表。P1-A 通过显式 Alembic migration 增加 GoalGraph/nodes、checkpoint/files/evidence、usage ledger 与 sandbox resources；既有 P0 SQLite 原位升级，未知结构失败关闭。`build_plan` / `acceptance_contract` 工件保留只读兼容与 P0 fallback，不删除历史数据。

P1-A 新增 `goal_graph.created/verified/failed/cancelled/superseded`、`goal.activated/claimed/verification_failed/resumed/verified/failed` 等生命周期事件。`goal.verified` 与 graph 终态事件携带有界完整 `goalGraph` 读投影，UI 不从测试事件自行推断 verified。

### 11.4 telemetry 契约

- A/B 数据来源全部是生产事件 + 既有 artifact（bridge stats、`pi.*` 事件、`verification.*`、`preview.*`），不新增 benchmark runner。
- 模型 profile/provider/model/thinking 继续进入 provenance（沿用 virtual key 与 `pi.started` 载荷）；`fallbackOccurred` 必须为 false 或显式事件，**禁止静默 fallback**（模型选择在 bridge 侧 fail-closed）。

## 12. 工作台投影

- **P0（现状）**：阶段/活动/工具时间线、spec-proof（AC 证据）、三态 preview、版本列表、代码与 diff 视图；未知事件默认容忍。
- **P1-A（已实施）**：Goal 面板（GoalGraph 树、active goal、claim/verified 徽标、证据数量）；snapshot 与实时 reducer 采用服务端权威投影，P0 `goalGraph: null` 为空态兼容。
- **P1-B（设计）**：Context Inspector（索引统计、Manifest 选择/排除理由、Capsule、token/cache/provenance 指标）。
- **P2 投影（设计）**：Reuse Manifest（来源/版本/许可证/installedFiles/modified/verification 状态）。

## 13. 迁移兼容

- **只读兼容（拆开 legacy 语义）**：旧 BuildPlan/AcceptanceContract 工件与 `acceptance_contracts` 表只读保留；**仅 `build.batch.*` 是 legacy-only**（只做历史 run 回放，新 run 不再产生，历史 UI 可标 legacy）。`planning.cache_hit` **不是 legacy-only**：历史可回放，且新 Direct Pi run 由 artifact cache 命中仍会产生——不得把二者并列称为 legacy，也不得要求 UI 给新的 cache_hit 事件标 legacy plan。**不删除、不改写任何历史数据**。
- **不再驱动新 run**：P0 起新 run 的 BUILDING 不再以计划文件为边界（计划仅咨询）；`batching.py` 已删除；planning cache 按 requirement/base_version/starter 指纹筛选 + 已验证 `build_plan`/`acceptance_contract` artifacts 组合 + 当前 parser 重验证（无独立 schemaVersion 字段；不回填不伪造）。
- **legacy native SOP**：`worker/runner.py` 仍保留 `native` 兼容分支（被引用，不删除）；它是**非默认可写兼容路径**（显式 `AGENT_FRAMEWORK=native` 时可写可运行），不是只读；P0 不为其新增功能。
- 数据变更全部走显式 Alembic migration（不依赖 `create_all` 幻觉）；P0 无数据变更，P1-A migration 原位兼容已知 P0 schema，并保留未知结构 fail-closed。

## 14. 模型策略

- **不微调主 Coding Model**（本轮与近期都不做）：先把 Goal/Context/Reuse 决策沉淀为**可评测的 Policy Model 数据**（P2 起），有评测基准后再评估微调。
- **Profile**：run 级显式选择 `reliable`（默认；发布链）或 `fast-experimental`（仅实验/对照，不进入发布链，P2 起表单化）；每个 stage 的模型与 thinking 显式记录，**禁止静默 fallback**——provider 失败即失败/显式重试，不换模型降级。
- **证据边界**：供应商 benchmark、模型能力自述不是项目证据；一切验收以 FOMO 自身对照实验与 QA 证据为准。

## 15. 评测矩阵与验收

> 最新集中回归为后端 385 项、Web Vitest 134 项、Pi bridge 18 项、本地 Web Playwright 2/2，Web typecheck/build 与 Ruff 亦通过。真实本地固定 runner canary 已通过；10 次生命周期矩阵尚未完成且不能记为通过，但不作为当前笔试 Demo 的发布条件；Context OS、同条件 A-B 与公网 HTTPS 验收仍未完成。以下环境项仍是最终验收判据。

| 证据层 | 当前结果 | 严格边界 |
| --- | --- | --- |
| 代码/协议回归 | 后端 385、Web Vitest 134、Pi bridge 18；typecheck/build/Ruff 通过 | 证明代码契约与受控 fake/integration 路径，不证明真实沙箱或网络 |
| Preview Gateway 代码测试 | 已包含在后端 385 项中并通过 | 证明 Host/verified resource/header 隔离/失效与大小边界；不经过 DNS、TLS、Tunnel |
| 真实本地运行 | OpenSandbox canary `succeeded / ready`；本地 Chrome 已验 direct Preview 与 Host Gateway 的交互/reload；Web Playwright 2/2 覆盖 workbench smoke 与账户/session 隔离 | 证明本机真实 OpenSandbox、数据库、浏览器及 Gateway 链路；所有入口仍是 localhost 或本地 Host routing |
| 公网 HTTPS | **尚未执行** | 只有外部网络通过真实 DNS/TLS/named Tunnel 完成登录、生成/SSE、最终 Preview、交互/reload 后才能标记在线交付 |

| 维度 | 度量 | P0 验收判据 |
| --- | --- | --- |
| 原生权限 | Pi 工具与文件操作 | G 内 Pi 可改 package/config/starter 且直接 typecheck 通过；无业务 allowlist 拦截；事件中无 write_scope_violation 类失败 |
| 可信 QA | gate 命令来源 | V 在固定 root-owned PATH 下直接执行 root-owned `0555` 的 pnpm `#!/bin/sh` 绝对 wrapper；候选 scripts 不参与 gate |
| 依赖限制 | 离线安装 | 预取依赖内变更可通过依赖 gate；store 外新依赖如实失败（不声称可发布） |
| 诚实性 | unverified/verified/ready 转换 | 植入失败场景（坏验收编译、运行时错误、gate 期间源文件漂移）不产生 preview.verified/ready 且不创建版本；preview.verified 只在最终一致性检查 + 版本创建成功后发出；unverified 仅在保留窗口内可见（repair 期间 V 销毁即清除，不承诺持续可见） |
| 权限 | 沙箱内/外接触面 | 无 host/docker socket/控制面接触；**标准事件路径不出现原始 key（verbatim/常见格式的 best-effort redaction）**；分块/编码/主动外传未解决，公共不可信发布依赖 credential proxy + egress；egress deny 仅在有服务端验证后声明 |
| Telemetry | 工具节奏/preview 耗时 | first tool、first edit/write tool（不含 bash 写入）、tool 计数、finish reason、token/cache、first preview 相对耗时均可从生产事件查询 |
| 对照 | 同模型同沙箱同需求、同一外部评测目标与验证器 | local Pi / sandbox-native Pi / FOMO 三者可共享同一验证器产出一致可比的报告（**代码契约验证通过；A-B 环境验证尚未执行**） |
| 迁移 | 历史数据完整性 | 旧 run 全部可读；新 run 事件不产生 build.batch.*；planning.cache_hit 可由新 run artifact cache 命中产生；无数据删除 |
| 可复现 | README 一键启动 | 新机器按 README 启动 Web/API/worker/PG/Redis/MinIO/LiteLLM/OpenSandbox（沿用基线） |

## 16. 风险与开放问题

| 风险 | 处理 |
| --- | --- |
| 无受认证 egress 隔离（无 dns+nft sidecar；policy API 未认证；host-only 规则放行网关其他端口） | 不提供看似 fail-closed 的网络策略开关；本地可信开发可用；公共不可信部署在受认证 dns+nft/credential proxy + 真实验证前为**发布阻塞** |
| 模型新增依赖超出预取 store | V 离线安装 gate 如实失败（needs_attention）；修复轮可移除依赖；发布路径限制到预取依赖 |
| 候选 scripts 被改坏 | gate 在固定 root-owned PATH 下直接执行 root-owned `0555` 的 pnpm `#!/bin/sh` 绝对 wrapper，不读 scripts；settle 审计只查安全不变量 |
| 凭据经事件/日志泄漏 | 标准事件路径 best-effort redaction（verbatim key/常见格式）；分块、编码或主动外传无法防，公共不可信发布依赖 authenticated egress + credential proxy（blocker） |
| preview 中途退出（dev server 在 gate 期间死亡） | 发布前最终 health recheck，失败即 fail closed、不创建版本、不发 preview.verified；健康检查是时间点检查，TOCTOU 仍为公共不可信发布 blocker |
| 模型删除/移动 Base 关键文件 | V 重建自相同种子 + 完整 diff，QA 全量重跑；版本可回滚 |
| gate 期间 V 源文件漂移（含候选进程短暂篡改并恢复的 TOCTOU/同用户 race） | gate 前冻结 manifest/commit；发布前 live V hash 必须等于冻结快照，否则 fail closed、不创建版本；同用户 adversarial race 未解决，外部 QA runner/只读测试挂载为公共不可信发布 blocker |
| 版本与 gate 输入不一致 | 版本只持久化冻结 initial_files 与冻结 commit_sha；tag 显式指向冻结 sha；不重新读取 gate 后的活 V |
| 多目标长链（P1）token 超预算 | P0 预算机制沿用；P1 引入逐目标 Capsule 与 tokenEstimate |
| 二进制资源文件无法经 FileChange 传输 | 审计拒绝二进制/超限文件（P0 限制，与传输契约一致；后续里程碑引入对象存储工件） |
| profile 漂移或静默降级 | bridge 模型选择 fail-closed + provenance 显式记录 |
| 旧数据兼容回归 | 只读兼容 + 无数据变更 + 显式 migration 纪律 |
| 开放问题 | 受认证 dns+nft/credential-proxy egress 的实现与验证；Goal acceptance adapter 的回归集策略；Capsule 压缩阈值——留待对应阶段 |

## 17. 参考资料

- [Pi coding-agent：RPC / security / models / containerization](https://github.com/earendil-works/pi/tree/v0.84.1/packages/coding-agent/docs)
- [OpenSandbox 官方仓库与架构](https://github.com/opensandbox-group/OpenSandbox)
- [shadcn/ui registry](https://ui.shadcn.com/docs/registry)
- [gVisor Docker runtime](https://gvisor.dev/docs/user_guide/quick_start/docker/)
- [DeepSeek API 文档](https://api-docs.deepseek.com/)
- 本仓库：`AGENTS.md`（协作与测试策略规则）、`README.md`、`compose.yaml`、`infra/opensandbox/fomo-pi-rpc-bridge.mjs`、`services/control-plane/src/fomo/direct_pi/`、`services/control-plane/src/fomo/fomo_pi_ds/`
