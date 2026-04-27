# Pagination Quick Reference

## TL;DR

Cursor-based pagination provides O(1) performance regardless of page depth. Offset-based pagination is deprecated but maintained for backward compatibility.

## Quick Comparison

| Feature | Cursor Mode | Offset Mode |
|---------|-------------|-------------|
| Performance | O(1) constant | O(n) linear |
| Deep pages | Fast (5ms) | Slow (100ms+) |
| Stability | Stable | Unstable |
| Total count | Estimated | Exact |
| Recommended | ✅ Yes | ❌ No (deprecated) |

## API Usage

### Cursor Pagination (Recommended)

```bash
# First page
GET /api/v1/trade-suggestions?limit=50

# Next page
GET /api/v1/trade-suggestions?limit=50&cursor={next_cursor}

# With filters
GET /api/v1/trade-suggestions?limit=50&cursor={cursor}&direction=BUY
```

**Response:**
```json
{
  "suggestions": [...],
  "next_cursor": "abc123...",
  "has_more": true,
  "limit": 50,
  "estimated_total": 1250
}
```

### Offset Pagination (Deprecated)

```bash
GET /api/v1/trade-suggestions?page=2&page_size=50
```

**Response:**
```json
{
  "suggestions": [...],
  "total": 1250,
  "page": 2,
  "page_size": 50,
  "has_more": true
}
```

## Client Examples

### JavaScript/TypeScript

```typescript
// Fetch all pages
async function fetchAll() {
  const all = [];
  let cursor = null;
  
  while (true) {
    const url = cursor 
      ? `/api/v1/trade-suggestions?limit=50&cursor=${cursor}`
      : `/api/v1/trade-suggestions?limit=50`;
    
    const res = await fetch(url, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    const data = await res.json();
    
    all.push(...data.suggestions);
    if (!data.has_more) break;
    cursor = data.next_cursor;
  }
  
  return all;
}
```

### React Query (Infinite Scroll)

```typescript
import { useInfiniteQuery } from '@tanstack/react-query';

function useSuggestions() {
  return useInfiniteQuery({
    queryKey: ['suggestions'],
    queryFn: async ({ pageParam }) => {
      const url = pageParam 
        ? `/api/v1/trade-suggestions?limit=50&cursor=${pageParam}`
        : `/api/v1/trade-suggestions?limit=50`;
      return (await fetch(url)).json();
    },
    getNextPageParam: (lastPage) => 
      lastPage.has_more ? lastPage.next_cursor : undefined,
    initialPageParam: null,
  });
}
```

### Python

```python
async def fetch_all(token: str):
    all_suggestions = []
    cursor = None
    
    async with httpx.AsyncClient() as client:
        while True:
            url = f"/api/v1/trade-suggestions?limit=50"
            if cursor:
                url += f"&cursor={cursor}"
            
            res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            data = res.json()
            
            all_suggestions.extend(data["suggestions"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
    
    return all_suggestions
```

## Performance

| Page | Offset Mode | Cursor Mode | Improvement |
|------|-------------|-------------|-------------|
| 1 | 5ms | 5ms | 1x |
| 10 | 15ms | 5ms | 3x |
| 100 | 100ms | 5ms | 20x |
| 1000 | 1000ms+ | 5ms | 200x+ |

## Response Headers

```
Link: <url>; rel="next"          # RFC 5988 pagination link
X-Cache-Status: HIT              # Cache status
X-RateLimit-Remaining: 28        # Rate limit info
```

## Query Parameters

| Parameter | Type | Default | Mode |
|-----------|------|---------|------|
| `cursor` | string | null | Cursor |
| `limit` | int | 50 | Cursor |
| `page` | int | null | Offset |
| `page_size` | int | null | Offset |
| `status` | enum | active | Both |
| `direction` | enum | null | Both |
| `symbol` | string | null | Both |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Duplicate records | Use cursor pagination |
| Slow deep pages | Use cursor pagination |
| Invalid cursor | Treat as first page |
| Inaccurate total | Expected (estimated count) |

## Migration

**Before (Offset):**
```typescript
const res = await fetch(`/api/v1/trade-suggestions?page=2&page_size=50`);
```

**After (Cursor):**
```typescript
// First page
const res1 = await fetch(`/api/v1/trade-suggestions?limit=50`);

// Second page
const res2 = await fetch(
  `/api/v1/trade-suggestions?limit=50&cursor=${res1.next_cursor}`
);
```

## Files

- **Module**: `backend/app/core/pagination.py`
- **Endpoint**: `backend/app/api/v1/trade_suggestions.py`
- **Tests**: `backend/tests/integration/test_cursor_pagination.py`
- **Full Guide**: `backend/PAGINATION_GUIDE.md`

---

**Enhancement**: 2.2 - Pagination Improvements  
**Status**: ✅ Complete  
**Last Updated**: 2026-04-23
