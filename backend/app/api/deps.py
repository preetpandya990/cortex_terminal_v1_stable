"""
API dependencies for FastAPI endpoints.
"""
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db as get_db_session
from app.core.security import get_current_user_id


# Re-export for convenience
get_db = get_db_session


async def get_current_user(user_id: Annotated[str, Depends(get_current_user_id)]) -> dict:
    """
    Get current authenticated user.
    
    Args:
        user_id: User ID from JWT token
        
    Returns:
        User dictionary with user_id
    """
    return {"user_id": user_id}
