"""
Authentication Dependencies - User Retrieval and RBAC
"""
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.security import get_current_user_id, decode_token
from app.models.user import User


async def get_current_user(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get current authenticated user from database."""
    stmt = select(User).where(User.id == int(user_id))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )
    
    return user


def require_role(required_role: str):
    """
    Dependency factory for role-based access control.
    
    Usage:
        @router.post("/admin-only", dependencies=[Depends(require_role("admin"))])
    """
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        role_hierarchy = {
            "viewer": 0,
            "trader": 1,
            "admin": 2,
        }
        
        user_level = role_hierarchy.get(current_user.role, -1)
        required_level = role_hierarchy.get(required_role, 999)
        
        if user_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {required_role}"
            )
        
        return current_user
    
    return role_checker


def require_permission(permission: str):
    """
    Dependency factory for permission-based access control.
    
    Permissions by role:
    - viewer: read
    - trader: read, trade
    - admin: read, trade, admin
    """
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        permissions_map = {
            "viewer": ["read"],
            "trader": ["read", "trade"],
            "admin": ["read", "trade", "admin"],
        }
        
        user_permissions = permissions_map.get(current_user.role, [])
        
        if permission not in user_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}"
            )
        
        return current_user
    
    return permission_checker


# Type aliases for convenience
CurrentUser = Annotated[User, Depends(get_current_user)]
