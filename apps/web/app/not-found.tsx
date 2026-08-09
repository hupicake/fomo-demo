import Link from "next/link";

export default function NotFound() {
  return (
    <main className="grid min-h-screen place-items-center p-6">
      <section className="max-w-md text-center">
        <p className="font-mono text-sm text-muted-foreground">404</p>
        <h1 className="mt-2 text-2xl font-semibold">Project not found</h1>
        <p className="mt-2 text-sm text-muted-foreground">The project may have expired, or this guest session does not have access.</p>
        <Link className="mt-5 inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground" href="/">
          Return home
        </Link>
      </section>
    </main>
  );
}
