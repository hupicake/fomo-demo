"use client";

export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="grid min-h-screen place-items-center bg-background p-6">
      <section className="max-w-md rounded-2xl border bg-card p-6 shadow-sm">
        <p className="text-sm font-medium text-destructive">FOMO workspace error</p>
        <h1 className="mt-2 text-xl font-semibold">The workbench could not render.</h1>
        <p className="mt-2 text-sm text-muted-foreground">Your project data was not changed. Retry this view to reconnect.</p>
        <button className="mt-5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground" onClick={reset} type="button">
          Retry workspace
        </button>
      </section>
    </main>
  );
}
