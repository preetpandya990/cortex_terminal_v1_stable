# Auth — Login UI & Admin User Management

**Date:** 2026-05-02  
**Scope:** Full-stack — FastAPI backend + Next.js 16 App Router frontend

---

## Overview

This document covers two major features implemented together:

1. **Login UI** — world-class branded login page replacing the previous dev-only bypass button
2. **Admin User Management** — full CRUD interface for platform user accounts, accessible only to admins

---

## 1. Login UI

### Design

Split-panel layout:
- **Left (60%)** — dark branded panel (`bg-[#050d1a]`), dot-grid CSS background, radial glow accents, CortexMark SVG logo, four feature tiles describing the platform, footer
- **Right (40%)** — white form panel with identifier input, password field (show/hide toggle), error banner, submit button

Key UX decisions:
- Single `identifier` field accepts **username or email** — no separate fields
- No "Remember me" toggle — sessions are always 7-day persistent
- No public self-registration — user creation is admin-only
- Dev bypass button rendered only when `process.env.NODE_ENV === 'development'`
- Redirect-after-login: reads `?next=` param from `window.location.search` directly (avoids Next.js `useSearchParams` Suspense requirement)

Accessibility: ARIA roles, `role="alert"` on error banner, `focus-visible` rings throughout, keyboard-navigable.

### Files

| File | Change |
|------|--------|
| `frontend/src/app/login/page.tsx` | New — full login page with `BrandPanel` + `LoginForm` components |
| `frontend/src/app/api/auth/login/route.ts` | New — BFF proxy: `POST {identifier, password}` → backend, sets cookie at `path: '/'` |
| `frontend/src/middleware.ts` | New — Edge middleware for auth-gated routing |
| `frontend/src/components/AppHeader.tsx` | New — sticky nav with active-link highlight, `UserBadge`, admin nav link |
| `frontend/src/components/AppShell.tsx` | New — conditional layout wrapper (standalone vs. app layout) |
| `frontend/src/app/layout.tsx` | Revamped — stripped to `html + body + Providers + AppShell` only |
| `frontend/src/contexts/AuthContext.tsx` | Revamped — added `UserProfile` type, `user` state, `fetchUserProfile()` |
| `frontend/src/components/auth/AuthStatus.tsx` | Revamped — simplified status indicator |
| `frontend/src/components/auth/DevLoginButton.tsx` | Deleted — dead code, replaced by inline dev bypass in login page |

### Edge Middleware (`middleware.ts`)

```typescript
// Route guard logic
const PUBLIC_PATHS = new Set(['/login']);
const SKIP_PREFIXES = ['/_next/', '/api/', '/favicon.ico', '/icons/', '/images/'];

// Unauthenticated → redirect to /login?next=<path>
// Authenticated on /login → redirect to /
// Cookie presence only — no JWT verification at edge (Edge Runtime limitation)
```

The middleware reads the `refresh_token` httpOnly cookie to determine session presence. No JWT signature verification at edge — that happens on the backend for every API call.

### Auth Context changes

- Added `UserProfile` interface: `{id, username, email, full_name, role, is_active}`
- `user: UserProfile | null` state — populated asynchronously after token is acquired
- `fetchUserProfile(token)` — calls `/api/auth/me` BFF, sets `user` state
- `useEffect` watching `accessToken` → auto-fetches or clears profile
- `isAdmin` derived from JWT `role` claim via `decodeRole(accessToken)`, not from `user.role`

---

## 2. Backend Auth Fixes

### a. Username OR Email Login

`UserLogin.username` renamed to `UserLogin.identifier`. New helper `get_user_by_identifier()` tries username first, then email.

**File:** `backend/app/api/v1/auth.py`

```python
async def get_user_by_identifier(db, identifier):
    user = await get_user_by_username(db, identifier)
    if user is None:
        user = await get_user_by_email(db, identifier)
    return user
```

### b. Timing-Safe Login (CVE-2025-22234 pattern)

Previously the login route returned early with "user not found" before running bcrypt, leaking username validity via response time difference.

Fix: always run bcrypt against a dummy hash when the user is not found.

```python
_BCRYPT_ROUNDS: int = 10 if _settings.ENVIRONMENT == "development" else 12
_DUMMY_HASH: str = bcrypt.hashpw(b"cortex-timing-sentinel", bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()

# In login route:
user = await get_user_by_identifier(db, credentials.identifier)
candidate_hash = user.hashed_password if user is not None else _DUMMY_HASH
password_ok = verify_password(credentials.password, candidate_hash)
if user is None or not password_ok:
    raise HTTPException(401, "Incorrect credentials")  # generic message
```

### c. bcrypt Work Factor

Hardcoded `4` rounds replaced with env-aware constants (OWASP 2025 recommendation):
- Development: 10 rounds
- Production: 12 rounds

### d. Cookie Path Fix

Refresh token cookie was scoped to `path: '/api/auth'`, so the browser only sent it for requests under that path. The Edge Middleware at root couldn't read it.

Fixed in all three BFF auth routes:
- `frontend/src/app/api/auth/login/route.ts` — `path: '/'`
- `frontend/src/app/api/auth/refresh/route.ts` — `path: '/'`
- `frontend/src/app/api/auth/logout/route.ts` — `path: '/'`

---

## 3. Admin User Management

### Backend (`backend/app/api/v1/admin_users.py`)

New router registered at `/api/v1/admin`. All endpoints require the `AdminUserID` dependency (JWT-claim-only admin check, no extra DB round-trip).

#### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/users` | List all users with optional `?search=` substring filter |
| `POST` | `/admin/users` | Create a new user with explicit role |
| `PATCH` | `/admin/users/{id}` | Update role and/or active status |
| `DELETE` | `/admin/users/{id}` | Permanently delete user + cascade |

#### Safety Guards

**PATCH:**
- Cannot change your own role
- Cannot deactivate your own account
- Cannot demote the last active admin

**DELETE:**
- Cannot delete your own account
- Cannot delete the last active admin

#### Session Revocation

On role downgrade or deactivation, all active refresh token families for that user are immediately revoked in Redis. This prevents new access token issuance. In-flight access tokens expire naturally within `ACCESS_TOKEN_EXPIRE_MINUTES`.

```python
async def _revoke_all_sessions(user, db, redis):
    result = await db.execute(
        select(RefreshToken.token_family)
        .where(RefreshToken.user_id == user.id, RefreshToken.is_revoked.is_(False))
        .distinct()
    )
    for family in result.scalars().all():
        await revoke_token_family(family, redis)
```

### Frontend

#### Hook (`frontend/src/hooks/useAdminUsers.ts`)

TanStack Query v5 hooks for all operations. All mutations invalidate the shared `['admin-users']` query key.

```typescript
useAdminUsers(search?)    // GET with optional debounced search
useCreateUser()           // POST
useUpdateUser()           // PATCH { id, role?, is_active? }
useDeleteUser()           // DELETE by id
```

#### Page (`frontend/src/app/admin/users/page.tsx`)

- **Stats bar** — total users, breakdown by role (admin / trader / viewer)
- **Debounced search** — 300 ms via `useRef` + `setTimeout`
- **User table** — sortable by creation date, loading / error / empty states
- **Self-identification** — logged-in user's row shows a "you" badge; role select and status toggle are disabled for self
- **Role select** — inline dropdown per row, disabled for self
- **Status toggle** — `role="switch"` ARIA toggle, disabled for self; triggers session revocation on deactivation
- **Delete button** — appears on row hover, disabled for self, requires confirmation modal
- **Create User modal** — username, email, password, full name (optional), role select
- **Delete User modal** — shows username, requires explicit confirmation

#### Admin Layout (`frontend/src/app/admin/layout.tsx`)

Added "User Management" nav item pointing to `/admin/users` alongside existing Trade Audit and ML Governance items.

---

## 4. Role Hierarchy

```
viewer (0) → trader (1) → admin (2)
```

Defined in `frontend/src/lib/jwt.ts`:

```typescript
const ROLE_LEVEL: Record<UserRole, number> = { viewer: 0, trader: 1, admin: 2 };
export function hasMinimumRole(userRole, required): boolean {
  return (ROLE_LEVEL[userRole] ?? 0) >= (ROLE_LEVEL[required] ?? 999);
}
```

---

## 5. Key Architecture Notes

- **BFF pattern** — all auth calls go through Next.js API routes (`/api/auth/*`) which proxy to FastAPI. The browser never calls the backend directly.
- `isAdmin` in the frontend context is derived from the JWT `role` claim (decoded client-side, no signature verification), not from the `/me` profile response. This keeps role-gated UI instantaneous after token acquisition.
- The `AdminNavLink` component is placed in the root `AppHeader` and renders nothing for non-admins — no route-level guard needed in the header.
- Admin page-level guard is in `frontend/src/app/admin/layout.tsx` using `useAuth().isAdmin`.
