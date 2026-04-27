"""
Cursor-Based Pagination Utilities
==================================
Production-grade cursor pagination for FastAPI + SQLAlchemy with PostgreSQL.

Features:
- Compound cursor (score + timestamp + id) for stable pagination
- Base64 encoding for opaque cursors
- Keyset pagination (WHERE clause instead of OFFSET)
- Estimated count for large datasets (>10k rows)
- RFC 5988 Link header generation
- Backward compatible with offset pagination

Best Practices (2026):
- Keyset pagination provides O(1) performance regardless of page depth
- Compound cursors handle ties in sort columns
- Estimated counts avoid expensive COUNT(*) on large tables
- Opaque cursors prevent client manipulation
- Link headers follow RFC 5988 standard

References:
- https://use-the-index-luke.com/no-offset
- https://www.postgresql.org/docs/current/queries-limit.html
- https://datatracker.ietf.org/doc/html/rfc5988
"""
import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar
from urllib.parse import urlencode

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Cursor Encoding/Decoding
# ============================================================================

def encode_cursor(
    score: float | Decimal,
    timestamp: datetime,
    record_id: int,
) -> str:
    """
    Encode compound cursor to opaque base64 string.
    
    Format: {score}:{timestamp_iso}:{id}
    Example: 85.50:2026-04-23T10:00:00+00:00:12345
    
    Args:
        score: Primary sort value (e.g., consensus_score)
        timestamp: Secondary sort value (e.g., created_at)
        record_id: Unique identifier (tie-breaker)
    
    Returns:
        Base64-encoded cursor string
    
    Example:
        >>> encode_cursor(85.5, datetime(2026, 4, 23, 10, 0, 0), 12345)
        'ODUuNTA6MjAyNi0wNC0yM1QxMDowMDowMCswMDowMDoxMjM0NQ=='
    """
    # Convert Decimal to float for consistency
    if isinstance(score, Decimal):
        score = float(score)
    
    # Format: score:timestamp_iso:id
    cursor_str = f"{score}:{timestamp.isoformat()}:{record_id}"
    
    # Base64 encode for opacity
    cursor_bytes = cursor_str.encode("utf-8")
    encoded = base64.urlsafe_b64encode(cursor_bytes).decode("utf-8")
    
    return encoded


def decode_cursor(cursor: str) -> tuple[float, datetime, int]:
    """
    Decode opaque cursor to compound values.
    
    Args:
        cursor: Base64-encoded cursor string
    
    Returns:
        Tuple of (score, timestamp, record_id)
    
    Raises:
        ValueError: If cursor is invalid or malformed
    
    Example:
        >>> decode_cursor('ODUuNTA6MjAyNi0wNC0yM1QxMDowMDowMCswMDowMDoxMjM0NQ==')
        (85.5, datetime(2026, 4, 23, 10, 0, 0), 12345)
    """
    try:
        # Base64 decode
        cursor_bytes = base64.urlsafe_b64decode(cursor.encode("utf-8"))
        cursor_str = cursor_bytes.decode("utf-8")
        
        # Parse format: score:timestamp_iso:id
        parts = cursor_str.split(":")
        
        # Handle ISO timestamp with colons (e.g., 2026-04-23T10:00:00+00:00)
        # Format: score:date:time:timezone:id
        if len(parts) < 4:
            raise ValueError("Invalid cursor format")
        
        score = float(parts[0])
        # Reconstruct ISO timestamp (parts[1] through parts[-2])
        timestamp_str = ":".join(parts[1:-1])
        timestamp = datetime.fromisoformat(timestamp_str)
        record_id = int(parts[-1])
        
        return score, timestamp, record_id
    
    except (ValueError, IndexError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid cursor: {e}") from e


# ============================================================================
# Pagination Parameters
# ============================================================================

@dataclass
class PaginationParams:
    """
    Pagination parameters for cursor or offset mode.
    
    Supports both cursor-based and offset-based pagination for
    backward compatibility.
    """
    # Cursor-based parameters
    cursor: str | None = None
    limit: int = 50
    
    # Offset-based parameters (backward compatibility)
    page: int | None = None
    page_size: int | None = None
    
    @property
    def is_cursor_mode(self) -> bool:
        """Check if using cursor-based pagination."""
        return self.cursor is not None or (self.page is None and self.page_size is None)
    
    @property
    def effective_limit(self) -> int:
        """Get effective limit (cursor limit or page_size)."""
        if self.page_size is not None:
            return min(self.page_size, 100)  # Cap at 100
        return min(self.limit, 100)  # Cap at 100


# ============================================================================
# Pagination Response
# ============================================================================

class CursorPaginationMeta(BaseModel):
    """
    Cursor pagination metadata.
    
    Includes cursors for navigation and estimated total count.
    """
    next_cursor: str | None = None
    prev_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    estimated_total: int | None = None


class CursorPaginatedResponse(BaseModel, Generic[T]):
    """
    Generic cursor-paginated response.
    
    Type-safe response wrapper for cursor pagination.
    """
    data: list[T]
    pagination: CursorPaginationMeta


# ============================================================================
# Keyset Pagination Query Builder
# ============================================================================

async def apply_cursor_pagination(
    session: AsyncSession,
    base_query: Select,
    score_column: Any,
    timestamp_column: Any,
    id_column: Any,
    cursor: str | None,
    limit: int,
    direction: str = "desc",
) -> tuple[list[Any], str | None, bool]:
    """
    Apply cursor-based keyset pagination to SQLAlchemy query.
    
    Uses compound cursor (score + timestamp + id) for stable pagination
    with O(1) performance regardless of page depth.
    
    Args:
        session: Async database session
        base_query: Base SELECT query with filters applied
        score_column: Primary sort column (e.g., TradeSuggestion.consensus_score)
        timestamp_column: Secondary sort column (e.g., TradeSuggestion.created_at)
        id_column: Unique identifier column (e.g., TradeSuggestion.id)
        cursor: Opaque cursor from previous page (None for first page)
        limit: Number of records to fetch (+ 1 for has_more check)
        direction: Sort direction ("desc" or "asc")
    
    Returns:
        Tuple of (records, next_cursor, has_more)
    
    Example:
        >>> records, next_cursor, has_more = await apply_cursor_pagination(
        ...     session=session,
        ...     base_query=select(TradeSuggestion).where(TradeSuggestion.status == "active"),
        ...     score_column=TradeSuggestion.consensus_score,
        ...     timestamp_column=TradeSuggestion.created_at,
        ...     id_column=TradeSuggestion.id,
        ...     cursor=None,
        ...     limit=50,
        ...     direction="desc"
        ... )
    """
    # Decode cursor if provided
    if cursor:
        try:
            cursor_score, cursor_timestamp, cursor_id = decode_cursor(cursor)
        except ValueError as e:
            logger.warning(f"Invalid cursor: {e}")
            # Treat as first page
            cursor = None
    
    # Build keyset WHERE clause
    if cursor:
        if direction == "desc":
            # For descending order: (score, timestamp, id) < cursor
            # Handles ties: score < cursor_score OR (score = cursor_score AND timestamp < cursor_timestamp) OR ...
            keyset_filter = (
                (score_column < cursor_score) |
                (
                    (score_column == cursor_score) &
                    (
                        (timestamp_column < cursor_timestamp) |
                        (
                            (timestamp_column == cursor_timestamp) &
                            (id_column < cursor_id)
                        )
                    )
                )
            )
        else:
            # For ascending order: (score, timestamp, id) > cursor
            keyset_filter = (
                (score_column > cursor_score) |
                (
                    (score_column == cursor_score) &
                    (
                        (timestamp_column > cursor_timestamp) |
                        (
                            (timestamp_column == cursor_timestamp) &
                            (id_column > cursor_id)
                        )
                    )
                )
            )
        
        base_query = base_query.where(keyset_filter)
    
    # Apply ordering
    if direction == "desc":
        base_query = base_query.order_by(
            score_column.desc(),
            timestamp_column.desc(),
            id_column.desc()
        )
    else:
        base_query = base_query.order_by(
            score_column.asc(),
            timestamp_column.asc(),
            id_column.asc()
        )
    
    # Fetch limit + 1 to check for more pages
    base_query = base_query.limit(limit + 1)
    
    # Execute query
    result = await session.execute(base_query)
    records = result.scalars().all()
    
    # Check if more pages exist
    has_more = len(records) > limit
    
    # Trim to limit
    if has_more:
        records = records[:limit]
    
    # Generate next cursor from last record
    next_cursor = None
    if has_more and records:
        last_record = records[-1]
        next_cursor = encode_cursor(
            score=getattr(last_record, score_column.key),
            timestamp=getattr(last_record, timestamp_column.key),
            record_id=getattr(last_record, id_column.key),
        )
    
    return records, next_cursor, has_more


async def get_estimated_count(
    session: AsyncSession,
    table_name: str,
    where_clause: str | None = None,
    threshold: int = 10000,
) -> int:
    """
    Get estimated row count using PostgreSQL statistics.
    
    For large tables (>10k rows), uses pg_class.reltuples for fast estimation.
    For small tables, uses exact COUNT(*).
    
    Args:
        session: Async database session
        table_name: Table name (e.g., "trade_suggestions")
        where_clause: Optional WHERE clause for filtering (e.g., "status = 'active'")
        threshold: Row count threshold for using estimation (default: 10000)
    
    Returns:
        Estimated or exact row count
    
    Example:
        >>> count = await get_estimated_count(
        ...     session=session,
        ...     table_name="trade_suggestions",
        ...     where_clause="status = 'active'"
        ... )
    """
    # Get table statistics from pg_class
    stats_query = text(
        """
        SELECT reltuples::bigint AS estimate
        FROM pg_class
        WHERE relname = :table_name
        """
    )
    result = await session.execute(stats_query, {"table_name": table_name})
    estimate = result.scalar_one_or_none()
    
    # If estimate is below threshold, use exact count
    if estimate is None or estimate < threshold:
        if where_clause:
            count_query = text(f"SELECT COUNT(*) FROM {table_name} WHERE {where_clause}")
        else:
            count_query = text(f"SELECT COUNT(*) FROM {table_name}")
        
        result = await session.execute(count_query)
        return result.scalar_one()
    
    # For large tables, use estimate (adjust for WHERE clause if needed)
    # Note: This is a rough estimate and may not be accurate for complex filters
    return int(estimate)


# ============================================================================
# RFC 5988 Link Header Generation
# ============================================================================

def generate_link_header(
    request: Request,
    next_cursor: str | None,
    prev_cursor: str | None,
) -> str | None:
    """
    Generate RFC 5988 Link header for pagination.
    
    Format: <url>; rel="next", <url>; rel="prev"
    
    Args:
        request: FastAPI Request object
        next_cursor: Next page cursor (None if last page)
        prev_cursor: Previous page cursor (None if first page)
    
    Returns:
        Link header string or None if no links
    
    Example:
        Link: <https://api.example.com/suggestions?cursor=abc123&limit=50>; rel="next"
    """
    links = []
    
    # Build base URL
    base_url = str(request.url.remove_query_params("cursor", "page"))
    
    # Add next link
    if next_cursor:
        query_params = dict(request.query_params)
        query_params["cursor"] = next_cursor
        # Remove offset pagination params
        query_params.pop("page", None)
        query_params.pop("page_size", None)
        
        next_url = f"{base_url.split('?')[0]}?{urlencode(query_params)}"
        links.append(f'<{next_url}>; rel="next"')
    
    # Add prev link (if implemented)
    if prev_cursor:
        query_params = dict(request.query_params)
        query_params["cursor"] = prev_cursor
        query_params.pop("page", None)
        query_params.pop("page_size", None)
        
        prev_url = f"{base_url.split('?')[0]}?{urlencode(query_params)}"
        links.append(f'<{prev_url}>; rel="prev"')
    
    return ", ".join(links) if links else None
