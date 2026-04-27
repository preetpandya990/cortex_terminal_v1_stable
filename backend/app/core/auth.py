"""
Authentication Dependencies - User Retrieval and RBAC
"""
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.security import bearer_scheme, decode_token, get_current_user_id
from app.models.user import User

logger = logging.getLogger(__name__)


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


async def require_admin_role(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """
    Require admin role for endpoint access.
    
    Extracts role directly from JWT token for performance and security.
    Does not query database - uses cryptographically verified JWT claim.
    
    Returns:
        User ID (subject) from token
        
    Raises:
        HTTPException: 401 if no token, 403 if not admin
        
    Usage:
        @router.post("/admin-endpoint")
        async def admin_only(user_id: str = Depends(require_admin_role)):
            ...
    """
    # Extract token
    token = None
    if credentials:
        token = credentials.credentials
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Decode and validate token
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check role claim
    role = getattr(payload, "role", None)
    if role != "admin":
        logger.warning(
            "Admin access denied for user=%s role=%s endpoint=%s",
            payload.sub,
            role,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    
    logger.info(
        "Admin access granted: user=%s endpoint=%s method=%s",
        payload.sub,
        request.url.path,
        request.method,
    )
    
    return payload.sub


# Type aliases for convenience
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUserID = Annotated[str, Depends(require_admin_role)]
