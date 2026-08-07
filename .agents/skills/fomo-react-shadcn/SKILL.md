---
name: fomo-react-shadcn
description: FOMO 内部 Coding Agent 的 React + shadcn/ui 产品界面生成基线。
---

# FOMO React + shadcn/ui

除非仓库已有更明确的约定，生成产品界面时默认使用 React + TypeScript、Tailwind CSS、shadcn/ui（Radix 基座）和 Lucide React。优先复用项目已有组件、token、别名和目录；shadcn 是复制到代码库的可维护源码，不是另一个运行时 UI 框架。此 skill 不调用 v0，也不需要 `V0_API_KEY`。

## 生成规则

- 先选成熟 primitive，再写业务组合；使用 `@/components/ui/*`、`cn()` 和主题 token（`bg-background`、`text-foreground`、`border-border` 等），不以任意 hex、全局样式覆盖或另一套组件库破坏一致性。
- 图标只用 Lucide，图标按钮必须有可访问名称；优先小而明确的 client boundary，重型编辑器/图表按需加载，独立请求并行，避免 barrel import、组件内定义组件和无意义的 memo。
- 业务组件采用命名导出：`export function FeaturePanel`；公开类型紧邻组件：`export type FeaturePanelProps`。props 显式、可组合且保持稳定：数据、受控值与 `onValueChange`，操作回调用语义化 `onCreate`/`onDelete(id)`；接受 `className`、`disabled` 和原生可访问性 props 时完整透传。领域类型集中定义，禁止 `any`、隐式形状和以数组下标作为 key。

## 页面映射

| 场景 | 默认组合 |
| --- | --- |
| CRUD | `Table` + `DropdownMenu` 行操作；`Sheet`/`Dialog` 编辑；删除用 `AlertDialog`；状态用 `Badge`。 |
| 仪表盘 | 少量指标 `Card`、筛选栏、`Table`/趋势内容；不要把每个信息块都包成同款卡片。 |
| 表单 | `Label` + `Input`/`Textarea`/`Select`/`Checkbox`/`Switch` + 明确帮助与校验信息；提交按钮有 pending 状态。 |
| 移动导航 | 桌面侧栏/顶栏 + 移动端 `Sheet`、`Button`、`Separator`；触控目标足够大，导航可关闭并保留当前项。 |

禁止在已有 primitive 时手写 raw `button`/`input`/`textarea`/`select`、无理由自研 Dialog/Menu/Select/Tooltip，或反复堆砌 `div rounded border p-*` 卡片壳。破坏性动作必须经 `AlertDialog` 确认。

## 状态与本地数据

每个异步或 CRUD 视图必须完整呈现：加载时用贴合布局的 `Skeleton` 并禁用重复提交；空态说明“这里为什么为空”并给出主操作；错误态使用 `Alert`、保留用户输入、提供可重试动作；成功/删除结果要有可感知反馈。不要只留控制台错误或空白区域。

仅用于非敏感本地草稿/偏好的 `localStorage` 必须版本化：key 使用 `fomo:<feature>:v<N>`，值含 `{ version, data }`；仅在客户端读取，安全 `JSON.parse` 后校验版本，迁移或丢弃无效旧数据。不得存储密钥、token 或服务端真相。

## 交付检查

- 所有交互支持键盘、焦点、label/aria 和窄屏；避免嵌套卡片、混乱半径、过度渐变与多个竞争性强调色。
- 若使用 Playwright：开发服务器绑定 `0.0.0.0`，浏览器访问 `http://127.0.0.1:<port>`；测试真实点击、表单校验、加载/空/错误态和移动导航，不以静态 helper 断言代替用户路径。
- 最终输出只交付可运行的完整代码与必要状态处理；复用成熟 primitive 的理由无需赘述，自研 primitive 必须有明确缺口说明。
