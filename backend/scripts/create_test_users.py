#!/usr/bin/env python3
"""
Create test users for development and testing.
"""
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.user import User
import bcrypt


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


async def create_test_users():
    """Create admin, trader, and viewer test users."""
    async with AsyncSessionLocal() as db:
        # Check if users already exist
        stmt = select(User).where(User.username == "admin")
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            print("✓ Test users already exist")
            return
        
        # Create admin user
        admin = User(
            username="admin",
            email="admin@cortex.local",
            hashed_password=hash_password("admin123"),
            full_name="Admin User",
            role="admin",
            is_active=True,
        )
        
        # Create trader user
        trader = User(
            username="trader",
            email="trader@cortex.local",
            hashed_password=hash_password("trader123"),
            full_name="Trader User",
            role="trader",
            is_active=True,
        )
        
        # Create viewer user
        viewer = User(
            username="viewer",
            email="viewer@cortex.local",
            hashed_password=hash_password("viewer123"),
            full_name="Viewer User",
            role="viewer",
            is_active=True,
        )
        
        db.add_all([admin, trader, viewer])
        await db.commit()
        
        print("✓ Created test users:")
        print("  - admin / admin123 (role: admin)")
        print("  - trader / trader123 (role: trader)")
        print("  - viewer / viewer123 (role: viewer)")


if __name__ == "__main__":
    asyncio.run(create_test_users())
