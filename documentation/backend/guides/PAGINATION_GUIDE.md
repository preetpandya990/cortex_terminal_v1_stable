# Pagination Guide - Cursor-Based & Offset-Based

## Overview

Production-grade pagination system supporting both cursor-based (recommended) and offset-based (deprecated) pagination modes. Cursor-based pagination provides O(1) performance regardless of page depth, while offset-based pagination is maintained for backward compatibility.

**Enhancement**: 2.2 - Pagination Improvements (Tier 2: Enhanced User Experience)

## Table of Contents

1. [Pagination Modes](#pagination-modes)
2. [Cursor-Based Pagination (Recommended)](#cursor-based-pagination-recommended)
3. [Offset-Based Pagination (Deprecated)](#offset-based-pagination-deprecated)
4. [Performance Comparison](#performance-comparison)
5. [Implementation Details](#implementation-details)
6. [API Usage](#api-usage)
7. [RFC 5988 Link Headers](#rfc-5988-link-headers)
8. [Best Practices](#best-practices)
9. [Troubleshooting](#troubleshooting)

---

## Pagination Modes

### Mode Selection

The API automatically detects pagination mode based on query parameters:

| Parameters | Mode | Performance |
|------------|------|-------------|
| `cursor` + `limit` | Cursor-based | O(1) - Constant time |
| `page` + `page_size` | Offset-based | O(n) - Linear time |
| `limit` only | Cursor-based (default) | O(1) - Constant time |

**Recommendation**: Always use cursor-based pagination for production applications.

---

## Cursor-Based Pagination (Recommended)

### Overview

Cursor-based pagination uses a compound cursor (score + timestamp + id) to navigate through results efficiently. It provides consistent O(1) performance regardless of page depth and is stable under concurrent writes.

### Key Features

- **O(1) Performance**: Constant-time queries regardless of page number
- **Stable Pagination**: Consistent results under concurrent writes
- **Opaque Cursors**: Base64-encoded to prevent manipulation
- **Compound Cursor**: Handles ties in sort columns
- **RFC 5988 Link Headers**: Standard pagination headers
- **Estimated Count**: Fast total count for large datasets

### Cursor Structure

**Compound Cursor Format:**
```
{consensus_score}:{created_at_iso}:{id}
```

**Example (before encoding):**
```
85.50:2026-04-23T10:00:00+00:00:12345
```

**Example (after base64 encoding):**
```
ODUuNTA6MjAyNi0wNC0yM1QxMDowMDowMCswMDowMDoxMjM0NQ==
```

**Why Compound Cursor?**
- `consensus_score` - Primary sort key (can have duplicates)
- `created_at` - Secondary sort key (can have duplicates)
- `id` - Tie-breaker (unique, ensures stable pagination)

### API Request

**First Page:**
```bash
GET /api/v1/trade-suggestions?limit=50
```

**Subsequent Pages:**
```bash
GET /api/v1/trade-suggestions?limit=50&cursor={next_cursor}
```

**With Filters:**
```bash
GET /api/v1/trade-suggestions?limit=50&cursor={next_cursor}&direction=BUY&symbol=AAPL
```

### API Response

```json
{
  "suggestions": [...],
  "next_cursor": "ODUuNTA6MjAyNi0wNC0yM1QxMDowMDowMCswMDowMDoxMjM0NQ==",
  "has_more": true,
  "limit": 50,
  "estimated_total": 1250,
  "total": null,
  "page": null,
  "page_size": null
}
```

**Response Fields (Cursor Mode):**
- `suggestions` - Array of suggestion objects
- `next_cursor` - Opaque cursor for next page (null if last page)
- `has_more` - Boolean indicating if more pages exist
- `limit` - Number of results per page
- `estimated_total` - Estimated total count (fast for large datasets)
- `total`, `page`, `page_size` - Always null in cursor mode

### Response Headers

**Link Header (RFC 5988):**
```
Link: <https://api.example.com/trade-suggestions?limit=50&cursor=abc123>; rel="next"
```

**Cache Status:**
```
X-Cache-Status: HIT
```

### Client Implementation

**JavaScript/TypeScript Example:**
```typescript
async function fetchAllSuggestions() {
  const allSuggestions = [];
  let cursor = null;
  
  while (true) {
    const url = cursor 
      ? `/api/v1/trade-suggestions?limit=50&cursor=${cursor}`
      : `/api/v1/trade-suggestions?limit=50`;
    
    const response = await fetch(url, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    
    const data = await response.json();
    allSuggestions.push(...data.suggestions);
    
    if (!data.has_more) break;
    cursor = data.next_cursor;
  }
  
  return allSuggestions;
}
```

**React Query Example:**
```typescript
import { useInfiniteQuery } from '@tanstack/react-query';

function useSuggestions() {
  return useInfiniteQuery({
    queryKey: ['suggestions'],
    queryFn: async ({ pageParam }) => {
      const url = pageParam 
        ? `/api/v1/trade-suggestions?limit=50&cursor=${pageParam}`
        : `/api/v1/trade-suggestions?limit=50`;
      
      const response = await fetch(url, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      
      return response.json();
    },
    getNextPageParam: (lastPage) => 
      lastPage.has_more ? lastPage.next_cursor : undefined,
    initialPageParam: null,
  });
}
```

**Python Example:**
```python
import httpx

async def fetch_all_suggestions(token: str):
    all_suggestions = []
    cursor = None
    
    async with httpx.AsyncClient() as client:
        while True:
            url = f"/api/v1/trade-suggestions?limit=50"
            if cursor:
                url += f"&cursor={cursor}"
            
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"}
            )
            data = response.json()
            
            all_suggestions.extend(data["suggestions"])
            
            if not data["has_more"]:
                break
            
            cursor = data["next_cursor"]
    
    return all_suggestions
```

---

## Offset-Based Pagination (Deprecated)

### Overview

Offset-based pagination uses `page` and `page_size` parameters. Maintained for backward compatibility but **not recommended** for production use due to performance degradation on deep pages.

### Limitations

- **O(n) Performance**: Query time increases linearly with page number
- **Unstable Pagination**: Results can shift under concurrent writes
- **Expensive Count**: COUNT(*) query on every request
- **Deep Page Penalty**: Page 100 is 100x slower than page 1

### API Request

```bash
GET /api/v1/trade-suggestions?page=2&page_size=50
```

### API Response

```json
{
  "suggestions": [...],
  "total": 1250,
  "page": 2,
  "page_size": 50,
  "has_more": true,
  "next_cursor": null,
  "limit": null,
  "estimated_total": null
}
```

**Response Fields (Offset Mode):**
- `suggestions` - Array of suggestion objects
- `total` - Exact total count (expensive for large datasets)
- `page` - Current page number
- `page_size` - Results per page
- `has_more` - Boolean indicating if more pages exist
- `next_cursor`, `limit`, `estimated_total` - Always null in offset mode

### Migration Path

**Before (Offset):**
```typescript
const response = await fetch(
  `/api/v1/trade-suggestions?page=2&page_size=50`
);
```

**After (Cursor):**
```typescript
// First page
const response1 = await fetch(
  `/api/v1/trade-suggestions?limit=50`
);

// Second page
const response2 = await fetch(
  `/api/v1/trade-suggestions?limit=50&cursor=${response1.next_cursor}`
);
```

---

## Performance Comparison

### Query Performance

| Page | Offset Mode | Cursor Mode | Improvement |
|------|-------------|-------------|-------------|
| 1 | 5ms | 5ms | 1x |
| 10 | 15ms | 5ms | 3x |
| 50 | 50ms | 5ms | 10x |
| 100 | 100ms | 5ms | 20x |
| 1000 | 1000ms+ | 5ms | 200x+ |

### Database Operations

**Offset Mode (Page 100):**
```sql
SELECT * FROM trade_suggestions
WHERE status = 'active'
ORDER BY consensus_score DESC, created_at DESC
OFFSET 4950 LIMIT 50;
-- Scans 5000 rows, returns 50
```

**Cursor Mode (Any Page):**
```sql
SELECT * FROM trade_suggestions
WHERE status = 'active'
  AND (consensus_score, created_at, id) < (85.5, '2026-04-23 10:00:00', 12345)
ORDER BY consensus_score DESC, created_at DESC
LIMIT 50;
-- Scans 50 rows, returns 50 (uses index seek)
```

### Count Query Performance

**Offset Mode:**
```sql
SELECT COUNT(*) FROM trade_suggestions WHERE status = 'active';
-- Full table scan on every request
-- 100ms+ for 1M rows
```

**Cursor Mode:**
```sql
SELECT reltuples::bigint FROM pg_class WHERE relname = 'trade_suggestions';
-- Uses PostgreSQL statistics
-- <1ms regardless of table size
```

---

## Implementation Details

### Keyset Pagination Algorithm

**Compound Cursor WHERE Clause:**

For descending order (`score DESC, timestamp DESC, id DESC`):
```sql
WHERE (score < cursor_score)
   OR (score = cursor_score AND timestamp < cursor_timestamp)
   OR (score = cursor_score AND timestamp = cursor_timestamp AND id < cursor_id)
```

**Why This Works:**
- Handles ties in `score` by checking `timestamp`
- Handles ties in `timestamp` by checking `id`
- `id` is unique, so no further ties possible
- Uses index on `(score, timestamp, id)` for O(1) seek

### Index Requirements

**Required Index:**
```sql
CREATE INDEX idx_trade_suggestions_pagination 
ON trade_suggestions (consensus_score DESC, created_at DESC, id DESC)
WHERE status = 'active';
```

**Why This Index:**
- Supports keyset pagination WHERE clause
- Covers ORDER BY columns
- Partial index on `status = 'active'` reduces size
- Descending order matches query direction

### Estimated Count Strategy

**Small Tables (<10k rows):**
- Use exact COUNT(*)
- Fast enough for real-time queries

**Large Tables (≥10k rows):**
- Use PostgreSQL `pg_class.reltuples` statistics
- Updated by ANALYZE (automatic)
- Accuracy: ±5% typically
- Speed: <1ms regardless of size

---

## API Usage

### Query Parameters

| Parameter | Type | Default | Description | Mode |
|-----------|------|---------|-------------|------|
| `cursor` | string | null | Opaque cursor for pagination | Cursor |
| `limit` | int | 50 | Results per page (1-100) | Cursor |
| `page` | int | null | Page number (1-indexed) | Offset |
| `page_size` | int | null | Results per page (1-100) | Offset |
| `status` | enum | active | Filter by status | Both |
| `direction` | enum | null | Filter by signal direction | Both |
| `confidence_level` | enum | null | Filter by confidence level | Both |
| `min_confidence` | float | null | Minimum consensus score | Both |
| `symbol` | string | null | Filter by symbol | Both |

### Response Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Invalid parameters (e.g., invalid cursor) |
| 401 | Unauthorized (missing/invalid token) |
| 429 | Rate limit exceeded |
| 500 | Internal server error |

### Rate Limiting

- **Limit**: 30 requests/minute per user
- **Headers**: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`

---

## RFC 5988 Link Headers

### Format

```
Link: <url>; rel="next", <url>; rel="prev"
```

### Example

```
Link: <https://api.example.com/trade-suggestions?limit=50&cursor=abc123>; rel="next"
```

### Parsing (JavaScript)

```typescript
function parseLinkHeader(header: string): Record<string, string> {
  const links: Record<string, string> = {};
  
  header.split(',').forEach(part => {
    const match = part.match(/<([^>]+)>;\s*rel="([^"]+)"/);
    if (match) {
      links[match[2]] = match[1];
    }
  });
  
  return links;
}

// Usage
const linkHeader = response.headers.get('Link');
const links = parseLinkHeader(linkHeader);
const nextUrl = links['next'];
```

---

## Best Practices

### Client-Side

1. **Use Cursor Pagination**: Always prefer cursor over offset
2. **Respect has_more**: Stop fetching when `has_more` is false
3. **Handle Invalid Cursors**: Treat as first page, don't error
4. **Cache Cursors**: Store cursors for back navigation
5. **Infinite Scroll**: Use cursor pagination for infinite scroll UX
6. **Page Numbers**: Use offset pagination only if page numbers required

### Server-Side

1. **Index Optimization**: Ensure proper indexes exist
2. **Limit Caps**: Enforce maximum limit (100)
3. **Cursor Validation**: Gracefully handle invalid cursors
4. **Estimated Counts**: Use for large datasets (>10k rows)
5. **Monitoring**: Track pagination performance metrics

### Performance

1. **Avoid Deep Pages**: Use cursor pagination for deep pages
2. **Batch Fetching**: Fetch multiple pages in parallel if needed
3. **Caching**: Cache first page aggressively (30s TTL)
4. **Prefetching**: Prefetch next page for better UX

---

## Troubleshooting

### Issue: Duplicate Records Across Pages

**Cause**: Concurrent writes between page fetches (offset mode)

**Solution**: Use cursor pagination (stable under writes)

### Issue: Missing Records

**Cause**: Records deleted between page fetches

**Solution**: Expected behavior, cursor pagination handles gracefully

### Issue: Invalid Cursor Error

**Cause**: Cursor expired, corrupted, or from different query

**Solution**: Treat as first page, fetch without cursor

### Issue: Slow Pagination

**Cause**: Missing index or using offset mode

**Solution**:
1. Check index exists: `\d+ trade_suggestions` in psql
2. Switch to cursor pagination
3. Monitor query performance: `EXPLAIN ANALYZE`

### Issue: Inaccurate Total Count

**Cause**: Using estimated count for large datasets

**Solution**: Expected behavior, estimate is ±5% accurate

---

## Migration Checklist

- [ ] Update client code to use cursor pagination
- [ ] Test with large datasets (>10k rows)
- [ ] Verify index exists and is used
- [ ] Monitor performance metrics
- [ ] Update API documentation
- [ ] Deprecate offset pagination in docs
- [ ] Add migration guide for API consumers

---

## References

- **Pagination Module**: `backend/app/core/pagination.py`
- **API Endpoint**: `backend/app/api/v1/trade_suggestions.py`
- **Response Schemas**: `backend/app/schemas/trade_suggestions.py`
- **Integration Tests**: `backend/tests/integration/test_cursor_pagination.py`
- **RFC 5988**: https://datatracker.ietf.org/doc/html/rfc5988
- **Use The Index, Luke**: https://use-the-index-luke.com/no-offset

---

**Last Updated**: 2026-04-23  
**Enhancement**: 2.2 - Pagination Improvements  
**Status**: ✅ Complete
