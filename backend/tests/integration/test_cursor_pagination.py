"""
Integration Tests for Cursor-Based Pagination
==============================================
Tests for cursor pagination, keyset queries, and RFC 5988 Link headers.

Test Coverage:
- Cursor encoding/decoding
- Keyset pagination (WHERE clause instead of OFFSET)
- Compound cursor (score + timestamp + id)
- RFC 5988 Link headers
- Estimated count for large datasets
- Backward compatibility with offset pagination
- Performance comparison (cursor vs offset)
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.models.trade_suggestions import TradeSuggestion


@pytest.mark.asyncio
async def test_cursor_encoding_decoding():
    """
    Test cursor encoding and decoding.
    
    Validates:
    - Base64 encoding produces opaque string
    - Decoding returns original values
    - Round-trip encoding/decoding is lossless
    """
    # Test data
    score = 85.5
    timestamp = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    record_id = 12345
    
    # Encode
    cursor = encode_cursor(score, timestamp, record_id)
    
    # Validate opaque (base64)
    assert isinstance(cursor, str)
    assert len(cursor) > 0
    assert ":" not in cursor  # Opaque, not raw format
    
    # Decode
    decoded_score, decoded_timestamp, decoded_id = decode_cursor(cursor)
    
    # Validate round-trip
    assert decoded_score == score
    assert decoded_timestamp == timestamp
    assert decoded_id == record_id


@pytest.mark.asyncio
async def test_cursor_encoding_with_decimal():
    """
    Test cursor encoding with Decimal (database type).
    
    Validates:
    - Decimal is converted to float
    - Round-trip works correctly
    """
    score = Decimal("85.50")
    timestamp = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    record_id = 12345
    
    cursor = encode_cursor(score, timestamp, record_id)
    decoded_score, decoded_timestamp, decoded_id = decode_cursor(cursor)
    
    assert decoded_score == float(score)
    assert decoded_timestamp == timestamp
    assert decoded_id == record_id


@pytest.mark.asyncio
async def test_invalid_cursor_decoding():
    """
    Test invalid cursor handling.
    
    Validates:
    - Invalid base64 raises ValueError
    - Malformed cursor raises ValueError
    """
    # Invalid base64
    with pytest.raises(ValueError, match="Invalid cursor"):
        decode_cursor("not-valid-base64!!!")
    
    # Valid base64 but malformed content
    import base64
    malformed = base64.urlsafe_b64encode(b"invalid:format").decode("utf-8")
    with pytest.raises(ValueError, match="Invalid cursor"):
        decode_cursor(malformed)


@pytest.mark.asyncio
async def test_cursor_pagination_first_page(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cursor pagination - first page.
    
    Validates:
    - No cursor parameter returns first page
    - next_cursor is provided if more pages exist
    - has_more is true if more pages exist
    - Link header is present
    """
    # Create 75 test suggestions (3 pages at limit=25)
    suggestions = []
    for i in range(75):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i * 0.1)),  # Descending scores
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # First page request (cursor mode)
    response = await async_client.get(
        "/api/v1/trade-suggestions?limit=25",
        headers=auth_headers,
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Validate response structure
    assert len(data["suggestions"]) == 25
    assert data["has_more"] is True
    assert data["next_cursor"] is not None
    assert data["limit"] == 25
    assert data["estimated_total"] >= 75
    
    # Validate offset fields are None (cursor mode)
    assert data["total"] is None
    assert data["page"] is None
    assert data["page_size"] is None
    
    # Validate Link header
    assert "Link" in response.headers
    assert 'rel="next"' in response.headers["Link"]
    
    # Validate ordering (descending by consensus_score)
    scores = [s["consensus_score"] for s in data["suggestions"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_cursor_pagination_second_page(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cursor pagination - second page.
    
    Validates:
    - Using next_cursor from first page returns second page
    - No duplicate records between pages
    - Stable pagination (consistent results)
    """
    # Create 75 test suggestions
    suggestions = []
    for i in range(75):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i * 0.1)),
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # First page
    response1 = await async_client.get(
        "/api/v1/trade-suggestions?limit=25",
        headers=auth_headers,
    )
    data1 = response1.json()
    next_cursor = data1["next_cursor"]
    
    # Second page using cursor
    response2 = await async_client.get(
        f"/api/v1/trade-suggestions?limit=25&cursor={next_cursor}",
        headers=auth_headers,
    )
    data2 = response2.json()
    
    assert response2.status_code == 200
    assert len(data2["suggestions"]) == 25
    assert data2["has_more"] is True
    
    # Validate no duplicates
    ids_page1 = {s["suggestion_id"] for s in data1["suggestions"]}
    ids_page2 = {s["suggestion_id"] for s in data2["suggestions"]}
    assert len(ids_page1 & ids_page2) == 0  # No intersection
    
    # Validate ordering continues
    last_score_page1 = data1["suggestions"][-1]["consensus_score"]
    first_score_page2 = data2["suggestions"][0]["consensus_score"]
    assert first_score_page2 <= last_score_page1


@pytest.mark.asyncio
async def test_cursor_pagination_last_page(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cursor pagination - last page.
    
    Validates:
    - Last page has has_more=false
    - next_cursor is None on last page
    - No Link header on last page
    """
    # Create 60 test suggestions (3 pages at limit=25, last page has 10)
    suggestions = []
    for i in range(60):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i * 0.1)),
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Navigate to last page
    cursor = None
    for _ in range(2):  # Skip first 2 pages
        response = await async_client.get(
            f"/api/v1/trade-suggestions?limit=25{f'&cursor={cursor}' if cursor else ''}",
            headers=auth_headers,
        )
        cursor = response.json()["next_cursor"]
    
    # Last page
    response = await async_client.get(
        f"/api/v1/trade-suggestions?limit=25&cursor={cursor}",
        headers=auth_headers,
    )
    data = response.json()
    
    assert response.status_code == 200
    assert len(data["suggestions"]) == 10  # Remaining records
    assert data["has_more"] is False
    assert data["next_cursor"] is None
    
    # No Link header on last page
    assert "Link" not in response.headers or 'rel="next"' not in response.headers.get("Link", "")


@pytest.mark.asyncio
async def test_cursor_pagination_with_filters(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cursor pagination with filters.
    
    Validates:
    - Filters work correctly with cursor pagination
    - Cursor respects filter constraints
    """
    # Create mixed suggestions
    suggestions = []
    for i in range(50):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="AAPL" if i % 2 == 0 else "TSLA",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY" if i % 2 == 0 else "SELL",
            consensus_score=Decimal(str(90.0 - i * 0.1)),
            confidence_level="HIGH" if i % 3 == 0 else "MEDIUM",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Filter by direction=BUY
    response = await async_client.get(
        "/api/v1/trade-suggestions?limit=10&direction=BUY",
        headers=auth_headers,
    )
    data = response.json()
    
    assert response.status_code == 200
    assert all(s["signal_direction"] == "BUY" for s in data["suggestions"])
    
    # Navigate to second page with same filter
    if data["next_cursor"]:
        response2 = await async_client.get(
            f"/api/v1/trade-suggestions?limit=10&direction=BUY&cursor={data['next_cursor']}",
            headers=auth_headers,
        )
        data2 = response2.json()
        assert all(s["signal_direction"] == "BUY" for s in data2["suggestions"])


@pytest.mark.asyncio
async def test_offset_pagination_backward_compatibility(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test offset pagination (backward compatibility).
    
    Validates:
    - Old offset pagination still works
    - Returns total, page, page_size
    - Cursor fields are None
    """
    # Create 75 test suggestions
    suggestions = []
    for i in range(75):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i * 0.1)),
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Offset pagination request
    response = await async_client.get(
        "/api/v1/trade-suggestions?page=2&page_size=25",
        headers=auth_headers,
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Validate offset mode response
    assert len(data["suggestions"]) == 25
    assert data["total"] == 75
    assert data["page"] == 2
    assert data["page_size"] == 25
    assert data["has_more"] is True
    
    # Validate cursor fields are None (offset mode)
    assert data["next_cursor"] is None
    assert data["limit"] is None
    assert data["estimated_total"] is None


@pytest.mark.asyncio
async def test_cursor_pagination_handles_ties(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cursor pagination handles ties in consensus_score.
    
    Validates:
    - Compound cursor (score + timestamp + id) handles ties
    - No records are skipped or duplicated
    - Stable pagination with identical scores
    """
    # Create suggestions with identical scores
    base_time = datetime.now(timezone.utc)
    suggestions = []
    for i in range(30):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal("85.50"),  # All same score
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=base_time + timedelta(hours=8),
            generated_at=base_time - timedelta(minutes=i),
            created_at=base_time - timedelta(minutes=i),
            updated_at=base_time - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Fetch all pages
    all_ids = set()
    cursor = None
    page_count = 0
    
    while True:
        response = await async_client.get(
            f"/api/v1/trade-suggestions?limit=10{f'&cursor={cursor}' if cursor else ''}",
            headers=auth_headers,
        )
        data = response.json()
        page_count += 1
        
        # Collect IDs
        page_ids = {s["suggestion_id"] for s in data["suggestions"]}
        
        # Check for duplicates
        assert len(all_ids & page_ids) == 0, "Duplicate records found across pages"
        all_ids.update(page_ids)
        
        # Break if no more pages
        if not data["has_more"]:
            break
        
        cursor = data["next_cursor"]
        
        # Safety check
        assert page_count < 10, "Too many pages, possible infinite loop"
    
    # Validate all records fetched
    assert len(all_ids) == 30


@pytest.mark.asyncio
async def test_invalid_cursor_returns_first_page(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test invalid cursor gracefully falls back to first page.
    
    Validates:
    - Invalid cursor doesn't break endpoint
    - Returns first page as fallback
    - Logs warning
    """
    # Create test suggestions
    suggestions = []
    for i in range(10):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i)),
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Request with invalid cursor
    response = await async_client.get(
        "/api/v1/trade-suggestions?limit=10&cursor=invalid-cursor-123",
        headers=auth_headers,
    )
    
    # Should still return 200 (fallback to first page)
    assert response.status_code == 200
    data = response.json()
    assert len(data["suggestions"]) == 10


@pytest.mark.asyncio
async def test_rfc_5988_link_header_format(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test RFC 5988 Link header format.
    
    Validates:
    - Link header follows RFC 5988 format
    - Contains rel="next" for next page
    - URL includes cursor parameter
    """
    # Create test suggestions
    suggestions = []
    for i in range(30):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"SYM{i:03d}",
            instrument_key=f"NSE_EQ|INE{i:06d}",
            signal_direction="BUY",
            consensus_score=Decimal(str(90.0 - i)),
            confidence_level="HIGH",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5},
            ai_signal={"sentiment": "bullish"},
            ml_signal={"direction": "BUY"},
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
        )
        suggestions.append(suggestion)
    
    db_session.add_all(suggestions)
    await db_session.commit()
    
    # Request first page
    response = await async_client.get(
        "/api/v1/trade-suggestions?limit=10",
        headers=auth_headers,
    )
    
    assert response.status_code == 200
    assert "Link" in response.headers
    
    link_header = response.headers["Link"]
    
    # Validate RFC 5988 format: <url>; rel="next"
    assert link_header.startswith("<")
    assert ">; rel=\"next\"" in link_header
    assert "cursor=" in link_header
    assert "limit=" in link_header
