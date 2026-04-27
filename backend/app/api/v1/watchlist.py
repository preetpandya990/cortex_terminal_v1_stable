"""
Watchlist API Routes - Per-user stock watchlist management
"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.security import CurrentUserID, get_current_user_id
from app.models.watchlist import WatchlistItem

router = APIRouter()


# ── Request/Response Models ────────────────────────────────────────────────────
class WatchlistItemCreate(BaseModel):
    instrument_key: str = Field(..., min_length=1, max_length=100)
    trading_symbol: str = Field(..., min_length=1, max_length=50)
    name: str | None = None
    exchange: str | None = None


class WatchlistItemResponse(BaseModel):
    id: int
    instrument_key: str
    trading_symbol: str
    name: str | None
    exchange: str | None
    position: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WatchlistReorderRequest(BaseModel):
    item_id: int
    new_position: int = Field(..., ge=0)


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.get("/", response_model=list[WatchlistItemResponse])
async def get_watchlist(
    user_id: CurrentUserID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[WatchlistItem]:
    """Get user's watchlist items ordered by position."""
    stmt = (
        select(WatchlistItem)
        .where(WatchlistItem.user_id == int(user_id))
        .order_by(WatchlistItem.position)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/", response_model=WatchlistItemResponse, status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    item: WatchlistItemCreate,
    user_id: CurrentUserID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WatchlistItem:
    """Add a stock to user's watchlist."""
    user_id_int = int(user_id)
    
    # Check if already exists
    stmt = select(WatchlistItem).where(
        WatchlistItem.user_id == user_id_int,
        WatchlistItem.instrument_key == item.instrument_key
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stock already in watchlist"
        )
    
    # Get max position for this user
    stmt = select(WatchlistItem.position).where(
        WatchlistItem.user_id == user_id_int
    ).order_by(WatchlistItem.position.desc()).limit(1)
    result = await db.execute(stmt)
    max_position = result.scalar_one_or_none()
    next_position = (max_position + 1) if max_position is not None else 0
    
    # Create new watchlist item
    watchlist_item = WatchlistItem(
        user_id=user_id_int,
        instrument_key=item.instrument_key,
        trading_symbol=item.trading_symbol,
        name=item.name,
        exchange=item.exchange,
        position=next_position
    )
    
    db.add(watchlist_item)
    await db.commit()
    await db.refresh(watchlist_item)
    
    return watchlist_item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_watchlist(
    item_id: int,
    user_id: CurrentUserID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Remove a stock from user's watchlist."""
    # Verify ownership
    stmt = select(WatchlistItem).where(
        WatchlistItem.id == item_id,
        WatchlistItem.user_id == int(user_id)
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watchlist item not found"
        )
    
    # Delete item
    await db.delete(item)
    await db.commit()


@router.put("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_watchlist(
    reorder: WatchlistReorderRequest,
    user_id: CurrentUserID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Reorder watchlist items."""
    user_id_int = int(user_id)
    
    # Get all user's watchlist items
    stmt = (
        select(WatchlistItem)
        .where(WatchlistItem.user_id == user_id_int)
        .order_by(WatchlistItem.position)
    )
    result = await db.execute(stmt)
    items = list(result.scalars().all())
    
    # Find the item to move
    item_to_move = None
    old_position = None
    for i, item in enumerate(items):
        if item.id == reorder.item_id:
            item_to_move = item
            old_position = i
            break
    
    if item_to_move is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watchlist item not found"
        )
    
    # Validate new position
    if reorder.new_position < 0 or reorder.new_position >= len(items):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid position"
        )
    
    # Reorder items
    items.pop(old_position)
    items.insert(reorder.new_position, item_to_move)
    
    # Update positions in database
    for i, item in enumerate(items):
        item.position = i
    
    await db.commit()


@router.get("/check/{instrument_key}", response_model=dict)
async def check_in_watchlist(
    instrument_key: str,
    user_id: CurrentUserID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Check if a stock is in user's watchlist."""
    stmt = select(WatchlistItem).where(
        WatchlistItem.user_id == int(user_id),
        WatchlistItem.instrument_key == instrument_key
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    
    return {
        "in_watchlist": item is not None,
        "item_id": item.id if item else None
    }
