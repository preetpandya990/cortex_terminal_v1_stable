# Loading States Quick Reference

## TL;DR

Production-grade loading states for React 19 + Next.js 16 + TanStack Query v5. Skeleton screens improve perceived performance by 20-30%.

## Quick Start

### 1. Skeleton Screens

```tsx
import { SkeletonList, SkeletonCard, SkeletonTable } from '@/components/ui/skeleton';

// List
<SkeletonList items={5} />

// Card
<SkeletonCard />

// Table
<SkeletonTable rows={10} columns={5} />
```

### 2. Loading State Wrapper

```tsx
import { LoadingState } from '@/components/ui/loading-state';
import { SkeletonList } from '@/components/ui/skeleton';

const { data, isPending, isFetching, isError, error, refetch } = useQuery(...);

<LoadingState
  isPending={isPending}
  isFetching={isFetching}
  isError={isError}
  error={error}
  data={data}
  refetch={refetch}
  skeleton={<SkeletonList items={5} />}
  isEmpty={(data) => data.length === 0}
>
  {(data) => <YourComponent data={data} />}
</LoadingState>
```

### 3. Error Boundaries

```tsx
import { ErrorBoundary } from '@/components/ui/error-boundary';

<ErrorBoundary>
  <YourComponent />
</ErrorBoundary>
```

### 4. Toast Notifications

```tsx
import { useToast } from '@/components/ui/toast';

const { success, error } = useToast();

// Success
success('Operation completed');

// Error
error('Operation failed', 'Please try again');
```

## Component Cheat Sheet

| Component | Use Case | Import |
|-----------|----------|--------|
| `Skeleton` | Base skeleton | `@/components/ui/skeleton` |
| `SkeletonList` | List loading | `@/components/ui/skeleton` |
| `SkeletonCard` | Card loading | `@/components/ui/skeleton` |
| `SkeletonTable` | Table loading | `@/components/ui/skeleton` |
| `LoadingState` | All-in-one wrapper | `@/components/ui/loading-state` |
| `ErrorBoundary` | Error catching | `@/components/ui/error-boundary` |
| `useToast` | Notifications | `@/components/ui/toast` |

## TanStack Query States

| State | When | Use For |
|-------|------|---------|
| `isPending` | First fetch, no data | Show skeleton |
| `isFetching` | Any fetch (initial + background) | Show subtle indicator |
| `isLoading` | `isPending && isFetching` | Simpler check |

## Best Practices

### ✅ DO

```tsx
// Show skeleton only when no data
if (isPending && !data) {
  return <SkeletonList />;
}

// Show subtle indicator during refetch
if (isFetching && data) {
  return (
    <>
      <RefetchIndicator />
      <DataList data={data} />
    </>
  );
}

// Use error boundaries at route level
<ErrorBoundary>
  <PageContent />
</ErrorBoundary>

// Use toasts for transient feedback
success('Saved successfully');
```

### ❌ DON'T

```tsx
// Don't show skeleton during background refetch
if (isFetching) {
  return <SkeletonList />;
}

// Don't use toasts for critical errors
error('App crashed'); // Use error boundary instead

// Don't show blank screens
if (isPending) {
  return null; // Bad UX
}
```

## Common Patterns

### Pattern 1: List with All States

```tsx
<LoadingState
  {...queryResult}
  skeleton={<SkeletonList items={5} />}
  isEmpty={(data) => data.length === 0}
  emptyState={<EmptyState title="No data" />}
>
  {(data) => <List data={data} />}
</LoadingState>
```

### Pattern 2: Mutation with Toast

```tsx
const { success, error } = useToast();

const mutation = useMutation({
  mutationFn: updateData,
  onSuccess: () => success('Updated'),
  onError: (err) => error('Failed', err.message),
});
```

### Pattern 3: Button Loading

```tsx
<button disabled={mutation.isPending}>
  {mutation.isPending ? (
    <>
      <InlineLoader className="mr-2" />
      Saving...
    </>
  ) : (
    'Save'
  )}
</button>
```

## Files

- **Skeleton**: `frontend/src/components/ui/skeleton.tsx`
- **Error Boundary**: `frontend/src/components/ui/error-boundary.tsx`
- **Loading State**: `frontend/src/components/ui/loading-state.tsx`
- **Toast**: `frontend/src/components/ui/toast.tsx`
- **Providers**: `frontend/src/app/providers.tsx`
- **Full Guide**: `frontend/LOADING_STATES_GUIDE.md`

---

**Enhancement**: 2.3 - Frontend Loading States  
**Status**: ✅ Complete  
**Last Updated**: 2026-04-23
