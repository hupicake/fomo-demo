import Link from "next/link";

export default function NotFound() {
  return (
    <main className="grid min-h-screen place-items-center p-6">
      <section className="max-w-md text-center">
        <p className="font-mono text-sm text-muted-foreground">404</p>
        <h1 className="mt-2 text-2xl font-semibold">找不到项目</h1>
        <p className="mt-2 text-sm text-muted-foreground">项目可能已过期，或当前账号无权访问。</p>
        <Link className="mt-5 inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground" href="/">
          返回首页
        </Link>
      </section>
    </main>
  );
}
