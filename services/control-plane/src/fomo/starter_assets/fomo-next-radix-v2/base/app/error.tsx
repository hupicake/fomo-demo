"use client";

import { ErrorState } from "@/components/system/feedback";

export default function GlobalError({ reset }: Readonly<{ error: Error & { digest?: string }; reset: () => void }>) {
  return (
    <main className="grid min-h-screen place-items-center p-6">
      <ErrorState title="Something went wrong" onRetry={reset} retryLabel="Try again" />
    </main>
  );
}
