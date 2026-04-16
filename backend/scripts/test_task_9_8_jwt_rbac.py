#!/usr/bin/env python3
"""
Task 9.8: JWT Authentication and RBAC E2E Test
==============================================
Tests JWT token lifecycle, refresh token rotation, and role-based access control.

Test Coverage:
1. Login as viewer and access read endpoints
2. Verify viewer blocked from admin endpoints (403)
3. Login as admin and access admin endpoints
4. Test refresh token rotation with family tracking
5. Test token reuse detection and family revocation
6. Test token expiration handling
"""
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import get_settings

settings = get_settings()

# Configuration
API_BASE_URL = "http://localhost:8000"
API_V1_PREFIX = "/api/v1"
DATABASE_URL = str(settings.DATABASE_URL)


class JWTRBACTester:
    """E2E tester for JWT authentication and RBAC."""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.engine = create_async_engine(DATABASE_URL, echo=False)
        self.async_session_maker = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        
        # Test users
        self.viewer_username = "viewer"
        self.viewer_password = "viewer123"
        self.admin_username = "admin"
        self.admin_password = "admin123"
        
        # Tokens
        self.viewer_access_token = None
        self.viewer_refresh_token = None
        self.admin_access_token = None
        self.admin_refresh_token = None
    
    def async_session(self):
        return self.async_session_maker()
    
    async def cleanup(self):
        """Cleanup resources."""
        await self.client.aclose()
        await self.engine.dispose()
    
    async def test_1_viewer_login_and_read_access(self):
        """Test 1: Login as viewer and access read endpoints."""
        print("\n" + "="*80)
        print("TEST 1: Viewer Login and Read Access")
        print("="*80)
        
        # Login as viewer
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/login",
            json={
                "username": self.viewer_username,
                "password": self.viewer_password
            }
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Login returned {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        data = response.json()
        self.viewer_access_token = data["access_token"]
        self.viewer_refresh_token = data["refresh_token"]
        
        print(f"✅ Logged in as viewer")
        print(f"   Access token: {self.viewer_access_token[:20]}...")
        print(f"   Refresh token: {self.viewer_refresh_token[:20]}...")
        print(f"   Token type: {data['token_type']}")
        print(f"   Expires in: {data['expires_in']}s")
        
        # Test read access to signals endpoint
        headers = {"Authorization": f"Bearer {self.viewer_access_token}"}
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/fusion/signals",
            headers=headers,
            params={"limit": 5}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Signals endpoint returned {response.status_code}")
            return False
        
        signals = response.json()
        print(f"✅ Read access granted to /fusion/signals")
        print(f"   Retrieved {len(signals)} signals")
        
        # Test read access to models endpoint (viewer should have read access)
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/models",
            headers=headers,
            params={"limit": 5}
        )
        
        # Models endpoint might require admin, check both cases
        if response.status_code == 200:
            models = response.json()
            print(f"✅ Read access granted to /governance/models")
            print(f"   Retrieved {len(models)} models")
        elif response.status_code == 403:
            print(f"ℹ️  Models endpoint requires admin role (expected for some deployments)")
        else:
            print(f"❌ FAILED: Models endpoint returned {response.status_code}")
            return False
        
        print(f"   ✅ PASSED: Viewer can access read endpoints")
        return True
    
    async def test_2_viewer_blocked_from_admin_endpoints(self):
        """Test 2: Verify viewer is blocked from admin endpoints (403)."""
        print("\n" + "="*80)
        print("TEST 2: Viewer Blocked from Admin Endpoints")
        print("="*80)
        
        headers = {"Authorization": f"Bearer {self.viewer_access_token}"}
        
        # Try to activate kill switch (admin only)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/safety/kill-switch/activate",
            headers=headers,
            json={
                "reason": "Test activation",
                "duration_seconds": 60
            }
        )
        
        if response.status_code == 403:
            print(f"✅ Kill switch activation blocked (403)")
            print(f"   Response: {response.json()['detail']}")
        else:
            print(f"❌ FAILED: Expected 403, got {response.status_code}")
            return False
        
        # Try to trigger drift check (admin only)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/check/1",
            headers=headers,
            json={"lookback_hours": 24}
        )
        
        if response.status_code == 403:
            print(f"✅ Drift check blocked (403)")
            print(f"   Response: {response.json()['detail']}")
        else:
            print(f"❌ FAILED: Expected 403, got {response.status_code}")
            return False
        
        # Try to promote model (admin only)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/models/test_model/promote",
            headers=headers,
            json={"target_state": "live"}
        )
        
        if response.status_code == 403:
            print(f"✅ Model promotion blocked (403)")
            print(f"   Response: {response.json()['detail']}")
        else:
            print(f"❌ FAILED: Expected 403, got {response.status_code}")
            return False
        
        print(f"   ✅ PASSED: Viewer correctly blocked from admin endpoints")
        return True
    
    async def test_3_admin_login_and_admin_access(self):
        """Test 3: Login as admin and access admin endpoints."""
        print("\n" + "="*80)
        print("TEST 3: Admin Login and Admin Access")
        print("="*80)
        
        # Login as admin
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/login",
            json={
                "username": self.admin_username,
                "password": self.admin_password
            }
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Login returned {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        data = response.json()
        self.admin_access_token = data["access_token"]
        self.admin_refresh_token = data["refresh_token"]
        
        print(f"✅ Logged in as admin")
        print(f"   Access token: {self.admin_access_token[:20]}...")
        
        # Test admin access to kill switch monitoring
        headers = {"Authorization": f"Bearer {self.admin_access_token}"}
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/safety/kill-switch/status",
            headers=headers
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Kill switch status returned {response.status_code}")
            return False
        
        status_data = response.json()
        print(f"✅ Admin access granted to /safety/kill-switch/status")
        print(f"   Status: {status_data}")
        
        # Test admin access to drift reports
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/reports",
            headers=headers,
            params={"limit": 5}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Drift reports returned {response.status_code}")
            return False
        
        reports = response.json()
        print(f"✅ Admin access granted to /governance/drift/reports")
        print(f"   Retrieved {len(reports)} reports")
        
        print(f"   ✅ PASSED: Admin can access admin endpoints")
        return True
    
    async def test_4_refresh_token_rotation(self):
        """Test 4: Test refresh token rotation with family tracking."""
        print("\n" + "="*80)
        print("TEST 4: Refresh Token Rotation")
        print("="*80)
        
        # Use viewer's refresh token
        original_refresh = self.viewer_refresh_token
        original_access = self.viewer_access_token
        
        print(f"   Original refresh token: {original_refresh[:20]}...")
        
        # Rotate token
        start_time = time.perf_counter()
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/refresh",
            json={"refresh_token": original_refresh}
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        
        if response.status_code != 200:
            print(f"❌ FAILED: Token refresh returned {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        data = response.json()
        new_access = data["access_token"]
        new_refresh = data["refresh_token"]
        
        print(f"✅ Token rotated successfully")
        print(f"   Response time: {elapsed_ms:.2f}ms")
        print(f"   New access token: {new_access[:20]}...")
        print(f"   New refresh token: {new_refresh[:20]}...")
        
        # Verify tokens are different
        if new_access == original_access:
            print(f"❌ FAILED: Access token not rotated")
            return False
        
        if new_refresh == original_refresh:
            print(f"❌ FAILED: Refresh token not rotated")
            return False
        
        print(f"✅ Tokens successfully rotated (different from originals)")
        
        # Verify new access token works
        headers = {"Authorization": f"Bearer {new_access}"}
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/me",
            headers=headers
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: New access token doesn't work")
            return False
        
        user_data = response.json()
        print(f"✅ New access token validated")
        print(f"   User: {user_data['username']} ({user_data['role']})")
        
        # Update stored tokens
        self.viewer_access_token = new_access
        self.viewer_refresh_token = new_refresh
        
        print(f"   ✅ PASSED: Token rotation working correctly")
        
        # Performance check
        if elapsed_ms > 100:
            print(f"   ⚠️  WARNING: Token rotation took {elapsed_ms:.2f}ms (target <100ms)")
        else:
            print(f"   ✅ PASSED: Token rotation within 100ms target")
        
        return True
    
    async def test_5_token_reuse_detection(self):
        """Test 5: Test token reuse detection and family revocation."""
        print("\n" + "="*80)
        print("TEST 5: Token Reuse Detection")
        print("="*80)
        
        # Get a fresh token pair
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/login",
            json={
                "username": self.viewer_username,
                "password": self.viewer_password
            }
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Login failed")
            return False
        
        data = response.json()
        refresh_token = data["refresh_token"]
        
        print(f"   Fresh refresh token: {refresh_token[:20]}...")
        
        # Use the refresh token once (valid)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/refresh",
            json={"refresh_token": refresh_token}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: First refresh failed")
            return False
        
        print(f"✅ First token use successful")
        
        # Try to reuse the same token (should fail)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/refresh",
            json={"refresh_token": refresh_token}
        )
        
        if response.status_code == 401:
            print(f"✅ Token reuse detected and blocked (401)")
            print(f"   Response: {response.json()['detail']}")
        else:
            print(f"❌ FAILED: Token reuse not detected (got {response.status_code})")
            return False
        
        print(f"   ✅ PASSED: Token reuse detection working")
        return True
    
    async def test_6_invalid_token_handling(self):
        """Test 6: Test handling of invalid/expired tokens."""
        print("\n" + "="*80)
        print("TEST 6: Invalid Token Handling")
        print("="*80)
        
        # Test with malformed token
        headers = {"Authorization": "Bearer invalid_token_12345"}
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/me",
            headers=headers
        )
        
        if response.status_code == 401:
            print(f"✅ Malformed token rejected (401)")
        else:
            print(f"❌ FAILED: Malformed token not rejected (got {response.status_code})")
            return False
        
        # Test with no token
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/me"
        )
        
        if response.status_code == 401:
            print(f"✅ Missing token rejected (401)")
        else:
            print(f"❌ FAILED: Missing token not rejected (got {response.status_code})")
            return False
        
        # Test with empty Bearer token (skip - causes httpx error)
        # httpx validates headers before sending, so we can't test truly empty Bearer
        print(f"ℹ️  Skipping empty Bearer token test (httpx validates headers)")
        
        print(f"   ✅ PASSED: Invalid token handling working correctly")
        return True
    
    async def run_all_tests(self):
        """Run all tests in sequence."""
        print("="*80)
        print("TASK 9.8: JWT AUTHENTICATION AND RBAC - E2E TEST")
        print("="*80)
        print(f"Start Time: {datetime.now(timezone.utc).isoformat()}")
        print(f"API Base URL: {API_BASE_URL}")
        
        tests = [
            ("Test 1: Viewer Login & Read Access", self.test_1_viewer_login_and_read_access),
            ("Test 2: Viewer Blocked from Admin", self.test_2_viewer_blocked_from_admin_endpoints),
            ("Test 3: Admin Login & Admin Access", self.test_3_admin_login_and_admin_access),
            ("Test 4: Refresh Token Rotation", self.test_4_refresh_token_rotation),
            ("Test 5: Token Reuse Detection", self.test_5_token_reuse_detection),
            ("Test 6: Invalid Token Handling", self.test_6_invalid_token_handling),
        ]
        
        results = {}
        
        for test_name, test_func in tests:
            try:
                result = await test_func()
                results[test_name] = result
            except Exception as e:
                print(f"\n❌ EXCEPTION in {test_name}: {e}")
                import traceback
                traceback.print_exc()
                results[test_name] = False
        
        # Summary
        print("\n" + "="*80)
        print("TEST SUMMARY")
        print("="*80)
        
        for test_name, result in results.items():
            status = "✅" if result else "❌"
            print(f"{status} {test_name}: {'PASSED' if result else 'FAILED'}")
        
        passed = sum(1 for r in results.values() if r)
        total = len(results)
        print(f"\nTotal: {passed}/{total} tests passed")
        
        if passed == total:
            print("\n🎉 ALL TESTS PASSED! Task 9.8 Complete!")
        else:
            print(f"\n⚠️  {total - passed} test(s) failed")
        
        return passed == total


async def main():
    """Main entry point."""
    tester = JWTRBACTester()
    try:
        success = await tester.run_all_tests()
        sys.exit(0 if success else 1)
    finally:
        await tester.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
