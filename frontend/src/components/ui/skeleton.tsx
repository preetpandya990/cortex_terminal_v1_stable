/**
 * Skeleton Component
 * =================
 * Production-grade skeleton loading component for React 19 + Tailwind CSS v4.
 * 
 * Features:
 * - Animated shimmer effect
 * - Accessible (aria-busy, aria-label)
 * - Customizable dimensions
 * - Multiple variants (text, avatar, card, table)
 * - Composable for complex layouts
 * 
 * Best Practices (2026):
 * - Skeleton screens improve perceived performance by 20-30%
 * - Show content structure instead of spinners
 * - Match final content layout for smooth transition
 * - Use for initial loads (isPending), not background refetches
 * 
 * References:
 * - https://www.nngroup.com/articles/skeleton-screens/
 * - https://uxdesign.cc/what-you-should-know-about-skeleton-screens-a820c45a571a
 */

import { cn } from "@/lib/utils";

interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  /**
   * Variant of skeleton
   * - text: Single line of text
   * - avatar: Circular avatar
   * - card: Rectangular card
   * - custom: Use className for custom dimensions
   */
  variant?: "text" | "avatar" | "card" | "custom";
  
  /**
   * Width of skeleton (only for custom variant)
   */
  width?: string | number;
  
  /**
   * Height of skeleton (only for custom variant)
   */
  height?: string | number;
  
  /**
   * Accessible label for screen readers
   */
  "aria-label"?: string;
}

/**
 * Base Skeleton component with shimmer animation
 */
export function Skeleton({
  className,
  variant = "custom",
  width,
  height,
  "aria-label": ariaLabel = "Loading content",
  ...props
}: SkeletonProps) {
  const variantStyles = {
    text: "h-4 w-full rounded",
    avatar: "h-12 w-12 rounded-full",
    card: "h-32 w-full rounded-lg",
    custom: "",
  };

  const style: React.CSSProperties = {};
  if (width) style.width = typeof width === "number" ? `${width}px` : width;
  if (height) style.height = typeof height === "number" ? `${height}px` : height;

  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={ariaLabel}
      className={cn(
        "animate-pulse bg-muted",
        "relative overflow-hidden",
        // Shimmer effect
        "before:absolute before:inset-0",
        "before:-translate-x-full",
        "before:animate-[shimmer_2s_infinite]",
        "before:bg-gradient-to-r",
        "before:from-transparent before:via-white/10 before:to-transparent",
        variantStyles[variant],
        className
      )}
      style={style}
      {...props}
    />
  );
}

/**
 * Skeleton for text lines
 */
export function SkeletonText({
  lines = 3,
  className,
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)} aria-label={`Loading ${lines} lines of text`}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          variant="text"
          className={cn(
            // Last line is shorter for natural look
            i === lines - 1 && "w-4/5"
          )}
        />
      ))}
    </div>
  );
}

/**
 * Skeleton for avatar + text (user profile)
 */
export function SkeletonProfile({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-3", className)} aria-label="Loading profile">
      <Skeleton variant="avatar" />
      <div className="flex-1 space-y-2">
        <Skeleton variant="text" className="w-32" />
        <Skeleton variant="text" className="w-24 h-3" />
      </div>
    </div>
  );
}

/**
 * Skeleton for card component
 */
export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div
      className={cn("rounded-lg border bg-card p-6 space-y-4", className)}
      aria-label="Loading card"
    >
      {/* Header */}
      <div className="space-y-2">
        <Skeleton variant="text" className="w-3/4 h-6" />
        <Skeleton variant="text" className="w-1/2 h-4" />
      </div>
      
      {/* Content */}
      <SkeletonText lines={3} />
      
      {/* Footer */}
      <div className="flex gap-2 pt-2">
        <Skeleton className="h-9 w-20 rounded-md" />
        <Skeleton className="h-9 w-20 rounded-md" />
      </div>
    </div>
  );
}

/**
 * Skeleton for table rows
 */
export function SkeletonTable({
  rows = 5,
  columns = 4,
  className,
}: {
  rows?: number;
  columns?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-3", className)} aria-label={`Loading table with ${rows} rows`}>
      {/* Header */}
      <div className="flex gap-4 pb-2 border-b">
        {Array.from({ length: columns }).map((_, i) => (
          <Skeleton key={i} variant="text" className="h-4 flex-1" />
        ))}
      </div>
      
      {/* Rows */}
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex gap-4">
          {Array.from({ length: columns }).map((_, colIndex) => (
            <Skeleton key={colIndex} variant="text" className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * Skeleton for list items
 */
export function SkeletonList({
  items = 5,
  className,
}: {
  items?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-3", className)} aria-label={`Loading list with ${items} items`}>
      {Array.from({ length: items }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 p-3 rounded-lg border">
          <Skeleton variant="avatar" className="h-10 w-10" />
          <div className="flex-1 space-y-2">
            <Skeleton variant="text" className="w-3/4" />
            <Skeleton variant="text" className="w-1/2 h-3" />
          </div>
          <Skeleton className="h-8 w-16 rounded-md" />
        </div>
      ))}
    </div>
  );
}

/**
 * Skeleton for chart/graph
 */
export function SkeletonChart({ className }: { className?: string }) {
  return (
    <div
      className={cn("rounded-lg border bg-card p-6 space-y-4", className)}
      aria-label="Loading chart"
    >
      {/* Title */}
      <Skeleton variant="text" className="w-48 h-6" />
      
      {/* Chart area */}
      <div className="h-64 flex items-end gap-2">
        {Array.from({ length: 12 }).map((_, i) => (
          <Skeleton
            key={i}
            className="flex-1 rounded-t"
            style={{ height: `${Math.random() * 100}%` }}
          />
        ))}
      </div>
      
      {/* Legend */}
      <div className="flex gap-4 justify-center">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="flex items-center gap-2">
            <Skeleton className="h-3 w-3 rounded-full" />
            <Skeleton variant="text" className="w-16 h-3" />
          </div>
        ))}
      </div>
    </div>
  );
}
