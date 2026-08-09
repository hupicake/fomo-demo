import type { ProjectSnapshot, RunPresentation } from "@/lib/contracts";
import { createRunPresentation } from "@/lib/events/reducer";

export const demoProjectId = "demo-library";

const demoGoalGraph: NonNullable<ProjectSnapshot["goalGraph"]> = {
  graphId: "demo-graph-library",
  runId: "demo-run-library",
  revision: 2,
  status: "completed",
  productOutcome: "读者可以检索并借还图书，管理员可以维护库存与读者状态。",
  activeGoalId: null,
  goals: [
    {
      goalId: "G-1",
      title: "建立目录、检索与分类浏览",
      userVisible: true,
      dependsOn: [],
      status: "verified",
      checkpointId: "demo-checkpoint-1",
      verifiedAt: "2026-08-07T10:55:00.000Z",
      acceptance: [
        { acceptanceId: "AC-01", title: "按书名、作者、ISBN 和分类检索", priority: "must", status: "passed" },
      ],
      evidenceCount: 3,
    },
    {
      goalId: "G-2",
      title: "实现借阅、归还与库存一致性",
      userVisible: true,
      dependsOn: ["G-1"],
      status: "verified",
      checkpointId: "demo-checkpoint-2",
      verifiedAt: "2026-08-07T11:12:00.000Z",
      acceptance: [
        { acceptanceId: "AC-02", title: "零库存时阻止借阅并给出反馈", priority: "must", status: "passed" },
        { acceptanceId: "AC-03", title: "归还后恢复库存并更新借阅状态", priority: "must", status: "passed" },
      ],
      evidenceCount: 5,
    },
    {
      goalId: "G-3",
      title: "交付可预览、可检查的管理页面",
      userVisible: true,
      dependsOn: ["G-1", "G-2"],
      status: "verified",
      checkpointId: "demo-checkpoint-3",
      verifiedAt: "2026-08-07T11:21:00.000Z",
      acceptance: [
        { acceptanceId: "AC-04", title: "Preview、Code、Terminal 与版本记录可用", priority: "must", status: "passed" },
      ],
      evidenceCount: 4,
    },
  ],
};

export const demoProjectSnapshot: ProjectSnapshot = {
  goalGraph: demoGoalGraph,
  project: {
    id: demoProjectId,
    name: "图书管理系统",
    status: "completed",
    updatedAt: "2026-08-07T11:22:00.000Z",
  },
  messages: [
    {
      id: "demo-request",
      role: "user",
      content: "设计一个图书管理系统，支持图书检索、分类、借阅/归还、读者管理、库存状态和管理后台。",
      createdAt: "2026-08-07T10:30:00.000Z",
    },
    {
      id: "demo-summary",
      role: "assistant",
      content:
        "## 图书管理系统已生成\n\nDirect Pi 已完成规划、实现与修复，FOMO 已执行独立验收。右侧可以查看**代码**、**验证记录**和版本证据；这个页面是明确标注的本地演示夹具，并不代表真实 sandbox 成功。",
      createdAt: "2026-08-07T11:22:00.000Z",
    },
  ],
  activeRun: {
    id: "demo-run-library",
    projectId: demoProjectId,
    status: "completed",
    phase: "ready",
    lastSeq: 28,
  },
  lastSeq: 28,
  events: [],
  files: [
    { path: "app/page.tsx", hash: "43b71ca", language: "tsx" },
    { path: "app/books/page.tsx", hash: "9230efa", language: "tsx" },
    { path: "components/books/book-table.tsx", hash: "17cca81", language: "tsx" },
    { path: "lib/books.ts", hash: "a9b5f2d", language: "ts" },
    { path: "tests/books.spec.ts", hash: "12b7a4e", language: "ts" },
  ],
  versions: [
    {
      id: "demo-v2",
      hash: "43b71ca",
      message: "feat: add book inventory and borrow flow",
      createdAt: "2026-08-07T11:22:00.000Z",
      status: "ready",
      files: [
        { path: "app/books/page.tsx", additions: 96, status: "modified" },
        { path: "components/books/book-table.tsx", additions: 142, status: "added" },
      ],
    },
    {
      id: "demo-v1",
      hash: "14b8efc",
      message: "chore: scaffold library workspace",
      createdAt: "2026-08-07T10:48:00.000Z",
      status: "ready",
    },
  ],
  trace: [
    {
      id: "AC-01",
      title: "管理员可以按书名、作者、ISBN 和分类搜索图书。",
      priority: "must",
      status: "passed",
      evidence: [
        { id: "spec-01", type: "design", label: "ProductSpec §2.1", status: "passed" },
        { id: "file-01", type: "file", label: "components/books/book-table.tsx", status: "passed" },
        { id: "test-01", type: "test", label: "searches by title and ISBN", status: "passed" },
      ],
    },
    {
      id: "AC-02",
      title: "借阅操作实时更新可借库存，并阻止库存为零的借阅。",
      priority: "must",
      status: "passed",
      evidence: [
        { id: "spec-02", type: "design", label: "TechnicalSpec §3.2", status: "passed" },
        { id: "file-02", type: "file", label: "lib/books.ts", status: "passed" },
        { id: "test-02", type: "test", label: "prevents borrowing unavailable book", status: "passed" },
      ],
    },
    {
      id: "AC-03",
      title: "读者可以看到当前借阅、到期日和归还入口。",
      priority: "must",
      status: "passed",
      evidence: [
        { id: "file-03", type: "file", label: "app/books/page.tsx", status: "passed" },
        { id: "shot-03", type: "screenshot", label: "borrowed-books.desktop.png", status: "passed" },
      ],
    },
  ],
  preview: { status: "demo", runId: "demo-run-library" },
};

export const demoFiles: Record<string, { content: string; hash: string; language: string }> = {
  "app/page.tsx": {
    hash: "43b71ca",
    language: "tsx",
    content: `import { BookTable } from "@/components/books/book-table";
import { getBooks } from "@/lib/books";

export default async function LibraryPage() {
  const books = await getBooks();
  return (
    <main className="mx-auto max-w-6xl p-8">
      <header className="mb-8 flex items-end justify-between">
        <div>
          <p className="text-sm text-muted-foreground">Northstar Library</p>
          <h1 className="text-3xl font-semibold">图书目录</h1>
        </div>
        <button className="rounded bg-slate-950 px-4 py-2 text-white">新增图书</button>
      </header>
      <BookTable books={books} />
    </main>
  );
}`,
  },
  "app/books/page.tsx": {
    hash: "9230efa",
    language: "tsx",
    content: `export default function LoansPage() {
  return (
    <section aria-labelledby="loans-title">
      <h1 id="loans-title">当前借阅</h1>
      {/* Reader, due date, overdue state and return action are listed here. */}
      <button type="button">归还图书</button>
    </section>
  );
}`,
  },
  "components/books/book-table.tsx": {
    hash: "17cca81",
    language: "tsx",
    content: `"use client";

import { useMemo, useState } from "react";

export function BookTable({ books }: { books: Book[] }) {
  const [query, setQuery] = useState("");
  const visibleBooks = useMemo(
    () => books.filter((book) => [book.title, book.author, book.isbn].join(" ").toLowerCase().includes(query.toLowerCase())),
    [books, query],
  );
  return <table aria-label="图书目录">{/* inventory rows */}</table>;
}`,
  },
  "lib/books.ts": {
    hash: "a9b5f2d",
    language: "ts",
    content: `export async function borrowBook(bookId: string, readerId: string) {
  const book = await db.books.find(bookId);
  if (book.availableCopies === 0) {
    throw new DomainError("BOOK_UNAVAILABLE");
  }
  return db.transaction(async () => {
    await db.loans.create({ bookId, readerId });
    return db.books.decrementAvailableCopies(bookId);
  });
}

export async function returnBook(loanId: string) {
  return db.transaction(async () => {
    const loan = await db.loans.markReturned(loanId);
    return db.books.incrementAvailableCopies(loan.bookId);
  });
}`,
  },
  "tests/books.spec.ts": {
    hash: "12b7a4e",
    language: "ts",
    content: `test("searches by title and ISBN", async () => {
  await expect(searchBooks("978-7")).resolves.toHaveLength(1);
});

test("prevents borrowing unavailable book", async () => {
  await expect(borrowBook("book-1", "reader-1")).rejects.toMatchObject({ code: "BOOK_UNAVAILABLE" });
});

test("returns a borrowed book and restores inventory", async () => {
  await expect(returnBook("loan-1")).resolves.toMatchObject({ availableCopies: 1 });
});`,
  },
};

export function createDemoRunPresentation(): RunPresentation {
  const state = createRunPresentation({
    projectId: demoProjectId,
    run: demoProjectSnapshot.activeRun,
    trace: demoProjectSnapshot.trace,
    versions: demoProjectSnapshot.versions,
    preview: demoProjectSnapshot.preview,
    goalGraph: demoGoalGraph,
  });
  return {
    ...state,
    contextUsage: {
      contextTokens: 68_420,
      contextWindow: 200_000,
      boundary: "turn_completed",
      capturedAt: "2026-08-07T11:21:00.000Z",
    },
    stages: {
      planning: { stage: "planning", status: "completed", title: "Plan", detail: "GoalGraph 已形成可执行目标与验收条件。", updatedAt: "2026-08-07T10:38:00.000Z" },
      building: { stage: "building", status: "completed", title: "Build", detail: "Coding Agent 已完成页面、数据流与交互实现。", updatedAt: "2026-08-07T11:12:00.000Z" },
      verifying: { stage: "verifying", status: "completed", title: "Verify", detail: "类型、构建与浏览器验收均通过。", updatedAt: "2026-08-07T11:21:00.000Z" },
      repairing: { stage: "repairing", status: "idle", title: "Repair" },
    },
    roles: {
      product_manager: {
        role: "product_manager",
        status: "completed",
        title: "需求与验收标准已对齐",
        detail: "定义图书检索、借阅、库存与读者管理的 must AC。",
        updatedAt: "2026-08-07T10:38:00.000Z",
      },
      architect: {
        role: "architect",
        status: "completed",
        title: "领域模型与事务边界已确定",
        detail: "借阅使用事务，库存变更与 loan 记录一致提交。",
        updatedAt: "2026-08-07T10:51:00.000Z",
      },
      engineer: {
        role: "engineer",
        status: "completed",
        title: "目录、库存和借阅流程已实现",
        detail: "新增 5 个文件，提交 43b71ca。",
        updatedAt: "2026-08-07T11:12:00.000Z",
      },
      reviewer: {
        role: "reviewer",
        status: "completed",
        title: "核心 AC 证据完整",
        detail: "类型检查、构建与 3 个浏览器验收用例通过。",
        updatedAt: "2026-08-07T11:21:00.000Z",
      },
    },
    artifacts: [
      {
        id: "build-plan-demo",
        kind: "build_plan",
        role: "pi",
        title: "Build Plan · 图书管理系统",
        markdown: `### 核心用户流\n\n1. 管理员搜索、筛选并维护图书目录。\n2. 读者借阅或归还图书，库存即时刷新。\n3. 管理员查看逾期和库存预警。\n\n### Must AC\n\n- [x] 多字段检索与分类筛选\n- [x] 不可借时给出明确反馈\n- [x] 借阅、归还、库存状态一致`,
      },
      {
        id: "acceptance-demo",
        kind: "acceptance_contract",
        role: "fomo",
        title: "Acceptance Contract · 可证明的借阅事务",
        markdown: `### 领域模型\n\n\`Book\`、\`Reader\`、\`Loan\` 以 \`availableCopies\` 建立库存约束。\n\n### 关键保障\n\n- 借阅和库存扣减在同一事务中完成。\n- API 返回 \`409 BOOK_UNAVAILABLE\`，前端不伪装成功。\n- Playwright 覆盖检索、借阅拦截与归还。`,
      },
    ],
    fileChanges: [
      { id: "file-1", path: "components/books/book-table.tsx", status: "added", additions: 142 },
      { id: "file-2", path: "lib/books.ts", status: "modified", additions: 38, deletions: 8 },
    ],
    commands: [
      {
        id: "cmd-typecheck",
        command: "pnpm typecheck && pnpm build",
        output: "$ pnpm typecheck\n✓ Typecheck passed\n$ pnpm build\n✓ Build completed\n",
        status: "completed",
        exitCode: 0,
      },
      {
        id: "cmd-e2e",
        command: "pnpm playwright test tests/books.spec.ts",
        output: "Running 3 tests using 1 worker\n  ✓ search by title and ISBN\n  ✓ prevents borrowing unavailable book\n  ✓ returns a borrowed book\n\n3 passed (4.1s)",
        status: "completed",
        exitCode: 0,
      },
    ],
    verifications: [
      { id: "verify-typecheck", name: "TypeScript typecheck", status: "passed", duration: 4200 },
      { id: "verify-build", name: "Production build", status: "passed", duration: 11100 },
      { id: "verify-e2e", name: "Core acceptance Playwright", status: "passed", duration: 4100 },
    ],
    problems: [],
    summaries: [demoProjectSnapshot.messages[1]?.content || ""],
    inputRequests: [
      {
        id: "demo-input-layout",
        runId: "demo-run-library",
        question: "管理工作台优先采用哪种信息密度？",
        choices: ["紧凑三栏", "宽松双栏"],
        allowFreeform: false,
        status: "answered",
        stage: "planning",
        createdAt: "2026-08-07T10:35:00.000Z",
        answeredAt: "2026-08-07T10:36:00.000Z",
        requestedSeq: 4,
        resolvedSeq: 5,
        answerMessageId: "demo-answer-layout",
      },
    ],
    worklog: [
      { id: "demo-log-01", kind: "system", status: "completed", title: "项目上下文已载入", detail: "已读取需求、现有目录和可复用组件。", stage: "planning", occurredAt: "2026-08-07T10:31:00.000Z", seq: 1 },
      { id: "demo-log-02", kind: "progress", status: "completed", title: "已梳理主要用户旅程", detail: "覆盖检索、分类、借阅、归还、读者管理和库存状态。", stage: "planning", occurredAt: "2026-08-07T10:34:00.000Z", seq: 2 },
      { id: "demo-log-03", kind: "goal", status: "completed", title: "GoalGraph 已建立", detail: "3 个交付目标和 4 条可验证标准已进入执行队列。", stage: "planning", occurredAt: "2026-08-07T10:38:00.000Z", seq: 3 },
      { id: "demo-log-04", kind: "tool", status: "completed", title: "检查现有项目结构", detail: "确认 Next.js 路由、组件目录、数据层和测试入口。", stage: "building", occurredAt: "2026-08-07T10:42:00.000Z", seq: 6 },
      { id: "demo-log-05", kind: "file", status: "completed", title: "实现图书目录页面", detail: "新增检索、分类筛选和库存状态呈现。", stage: "building", occurredAt: "2026-08-07T10:51:00.000Z", seq: 7 },
      { id: "demo-log-06", kind: "verification", status: "completed", title: "目录目标通过验证", detail: "检索和筛选行为满足 AC-01。", stage: "verifying", occurredAt: "2026-08-07T10:55:00.000Z", seq: 8 },
      { id: "demo-log-07", kind: "file", status: "completed", title: "实现借阅事务", detail: "借阅记录和库存扣减在同一事务内提交。", stage: "building", occurredAt: "2026-08-07T11:02:00.000Z", seq: 9 },
      { id: "demo-log-08", kind: "file", status: "completed", title: "实现归还流程", detail: "归还后同步借阅状态并恢复可借库存。", stage: "building", occurredAt: "2026-08-07T11:08:00.000Z", seq: 10 },
      { id: "demo-log-09", kind: "verification", status: "completed", title: "借还目标通过验证", detail: "零库存拦截与归还库存恢复均已通过。", stage: "verifying", occurredAt: "2026-08-07T11:12:00.000Z", seq: 11 },
      { id: "demo-log-10", kind: "tool", status: "completed", title: "运行 TypeScript 检查", detail: "pnpm typecheck 成功，无类型错误。", stage: "verifying", occurredAt: "2026-08-07T11:15:00.000Z", seq: 12 },
      { id: "demo-log-11", kind: "tool", status: "completed", title: "构建生产版本", detail: "pnpm build 成功完成。", stage: "verifying", occurredAt: "2026-08-07T11:17:00.000Z", seq: 13 },
      { id: "demo-log-12", kind: "verification", status: "completed", title: "运行浏览器验收", detail: "检索、借阅拦截和归还流程共 3 条用例通过。", stage: "verifying", occurredAt: "2026-08-07T11:20:00.000Z", seq: 14 },
      { id: "demo-log-13", kind: "system", status: "completed", title: "预览与版本已就绪", detail: "可以在右侧检查 Preview、Code、Terminal、Problems 和 Versions。", stage: "verifying", occurredAt: "2026-08-07T11:21:00.000Z", seq: 15 },
    ],
  };
}
