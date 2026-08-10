import { cn } from "@/lib/utils";

/**
 * A lightweight skeleton placeholder. Uses the muted token so it adapts to
 * light/dark themes automatically.
 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      data-slot="skeleton"
      {...props}
    />
  );
}

export { Skeleton };
