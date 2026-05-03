# Auth & Login — Bugs Found and Fixed

**Date:** 2026-05-02

---

## Bug 1 — FastAPI 204 Route AssertionError on Startup

**Symptom:** Backend crashed on startup with:
```
AssertionError: Status code 204 must not have a response body
```

**Root cause:** FastAPI ≥ 0.100 enforces that routes returning `204 No Content` must declare `response_class=Response` and explicitly return a `Response` object. The `delete_user` route used `-> None` with implicit `return`.

**Fix:** `backend/app/api/v1/admin_users.py`

```python
# Before
@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(...) -> None:
    ...  # implicit return

# After
from fastapi import Response
@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_user(...) -> Response:
    ...
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

---

## Bug 2 — `/admin/users` Returns 500 (Pydantic ORM Mode Missing)

**Symptom:** `GET /api/v1/admin/users` returned `500 Internal Server Error`.

**Root cause:** `UserResponse` Pydantic model was missing `model_config = ConfigDict(from_attributes=True)`. Without it, Pydantic v2 cannot serialize SQLAlchemy ORM objects — it expects plain dicts.

**Fix:** `backend/app/api/v1/auth.py`

```python
# Before
class UserResponse(BaseModel):
    id: int
    ...

# After
from pydantic import ConfigDict
class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ...
```

This also fixed the `/auth/me` endpoint which returns the same model.

---

## Bug 3 — Admin Nav Missing After Page Reload (Role Lost on Token Refresh)

**Symptom:** After any page reload or browser restart, the Admin nav link disappeared. Logging out and back in restored it.

**Root cause:** `create_refresh_token()` in `backend/app/core/security.py` accepted a `role` parameter but silently dropped it — it was never forwarded to `_create_token()` as `extra_claims`. So every refresh token was issued **without a `role` claim**.

When `rotate_refresh_token()` read the role back with `getattr(payload, "role", "viewer")`, the field existed in the Pydantic model (`role: str | None = None`) so it returned `None` instead of the default `"viewer"`. The new access token was then issued with `role: null`.

The frontend `decodeRole(accessToken)` returned `"viewer"` for `null`, so `isAdmin` was `false` after every refresh — even for admin users.

Login worked because the initial access token was created directly from `user.role` (correct). Only the **refreshed** access tokens were broken.

**Fix:** `backend/app/core/security.py`

```python
# Before — role silently dropped
def create_refresh_token(subject: str, family_id: str, role: str = "viewer") -> str:
    return _create_token(
        subject,
        "refresh",
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60,
        family_id=family_id,           # role never forwarded
    )

# After — role embedded in refresh token JWT
def create_refresh_token(subject: str, family_id: str, role: str = "viewer") -> str:
    return _create_token(
        subject,
        "refresh",
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60,
        family_id=family_id,
        extra_claims={"role": role},   # role survives rotation
    )
```

Also tightened the role read in `rotate_refresh_token`:

```python
# Before — getattr returns None (field exists but is null), not the default "viewer"
role = getattr(payload, "role", "viewer")

# After — None-safe fallback
role = payload.role or "viewer"
```

**Note:** After deploying this fix, existing refresh tokens in the wild still lack the `role` claim. Affected users must **log out and log back in** to get a new refresh token with the role embedded.

---

## Diagnosis Method for Bug 3

Decoded the live token payloads to compare login vs. refresh:

```bash
# Login — access token had role: "admin" ✓
# After refresh — access token had role: null ✗
# Refresh token — no role field at all ← root cause
```

The discrepancy between initial login (correct) and page reload (broken) was the key diagnostic signal — it pointed directly to the token rotation path.
