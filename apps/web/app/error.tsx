"use client";

export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="grid min-h-screen place-items-center bg-background p-6">
      <section className="max-w-md rounded-2xl border bg-card p-6 shadow-sm">
        <p className="text-sm font-medium text-destructive">工作台错误</p>
        <h1 className="mt-2 text-xl font-semibold">工作台无法渲染。</h1>
        <p className="mt-2 text-sm text-muted-foreground">你的项目数据没有变化。重试此视图以重新连接。</p>
        <button className="mt-5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground" onClick={reset} type="button">
          重试工作台
        </button>
      </section>
    </main>
  );
}
