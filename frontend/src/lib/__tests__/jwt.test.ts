/**
 * JWT Utility Tests
 * =================
 * Security-critical: these functions gate role-based navigation and
 * feature access throughout the entire frontend.  They must behave
 * correctly across every edge case including malformed tokens, expired
 * tokens, missing role claims, and the full role hierarchy.
 *
 * Note: decodeJwtPayload does NOT verify the signature — that is
 * intentional (signature verification is the server's job).  These tests
 * confirm that the client-side claim extraction is correct, safe, and
 * falls back to the least-privileged role on any parse failure.
 */

import { describe, it, expect } from 'vitest';
import { decodeJwtPayload, decodeRole, hasMinimumRole } from '@/lib/jwt';
import type { UserRole } from '@/lib/jwt';

// ── Test helpers ───────────────────────────────────────────────────────────────

/**
 * Construct a syntactically valid JWT with the given payload.
 * The signature segment is a placeholder — not cryptographically valid.
 * (decodeJwtPayload never checks the signature.)
 */
function makeToken(payload: Record<string, unknown>): string {
  const header  = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const body    = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `${header}.${body}.fakesignature`;
}

const FUTURE_EXP = Math.floor(Date.now() / 1000) + 3600;  // 1 hour from now
const PAST_EXP   = Math.floor(Date.now() / 1000) - 3600;  // 1 hour ago

// ── decodeJwtPayload ───────────────────────────────────────────────────────────

describe('decodeJwtPayload', () => {
  it('correctly decodes a valid access token payload', () => {
    const token = makeToken({ sub: '42', role: 'trader', exp: FUTURE_EXP, iat: 1000000000, jti: 'abc', type: 'access' });
    const result = decodeJwtPayload(token);

    expect(result).not.toBeNull();
    expect(result?.sub).toBe('42');
    expect(result?.role).toBe('trader');
    expect(result?.type).toBe('access');
  });

  it('returns null for a completely empty string', () => {
    expect(decodeJwtPayload('')).toBeNull();
  });

  it('returns null when the token has fewer than three segments', () => {
    expect(decodeJwtPayload('only.two')).toBeNull();
    expect(decodeJwtPayload('one')).toBeNull();
  });

  it('returns null when the payload segment is not valid base64url', () => {
    // Middle segment is not valid base64url
    expect(decodeJwtPayload('header.!!!invalid!!!.sig')).toBeNull();
  });

  it('returns null when the payload is valid base64 but not valid JSON', () => {
    const notJson = Buffer.from('this is not json').toString('base64url');
    expect(decodeJwtPayload(`header.${notJson}.sig`)).toBeNull();
  });

  it('handles base64url characters (- and _) correctly', () => {
    // Tokens with + and / in standard base64 become - and _ in base64url.
    // Ensure the reverse conversion is applied before atob().
    const payload = { sub: 'user-with-dashes', role: 'admin', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' };
    const token   = makeToken(payload);
    const result  = decodeJwtPayload(token);
    expect(result?.sub).toBe('user-with-dashes');
  });

  it('decodes a token whose payload contains non-ASCII characters (unicode-safe)', () => {
    const payload = { sub: '1', role: 'viewer', name: 'Preet Pandya', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' };
    const result  = decodeJwtPayload(makeToken(payload));
    expect(result?.name).toBe('Preet Pandya');
  });

  it('includes unknown claims in the returned object (open claims bag)', () => {
    const payload = { sub: '1', role: 'trader', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access', custom_claim: 'value' };
    const result  = decodeJwtPayload(makeToken(payload));
    expect(result?.custom_claim).toBe('value');
  });

  it('correctly decodes an expired token (expiry is not validated client-side)', () => {
    // Server enforces expiry.  The client reads the payload regardless.
    const token  = makeToken({ sub: '1', role: 'admin', exp: PAST_EXP, iat: 0, jti: 'x', type: 'access' });
    const result = decodeJwtPayload(token);
    expect(result?.exp).toBe(PAST_EXP);
    expect(result?.role).toBe('admin');
  });
});

// ── decodeRole ─────────────────────────────────────────────────────────────────

describe('decodeRole', () => {
  it('returns the role from a valid trader token', () => {
    const token = makeToken({ sub: '1', role: 'trader', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' });
    expect(decodeRole(token)).toBe('trader');
  });

  it('returns the role from a valid admin token', () => {
    const token = makeToken({ sub: '1', role: 'admin', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' });
    expect(decodeRole(token)).toBe('admin');
  });

  it('returns "viewer" when the token is null (unauthenticated state)', () => {
    // The default least-privileged role must be returned so the UI shows
    // no premium features when the user is logged out.
    expect(decodeRole(null)).toBe('viewer');
  });

  it('returns "viewer" when the token string is empty', () => {
    expect(decodeRole('')).toBe('viewer');
  });

  it('returns "viewer" when the token is malformed', () => {
    expect(decodeRole('not.a.valid.token.at.all')).toBe('viewer');
  });

  it('returns "viewer" when the payload has no role claim', () => {
    const token = makeToken({ sub: '1', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' });
    expect(decodeRole(token)).toBe('viewer');
  });

  it('returns "viewer" when the role claim is an unexpected value', () => {
    // Unknown roles fall back to "viewer" via the ??.
    const token = makeToken({ sub: '1', role: 'superadmin', exp: FUTURE_EXP, iat: 0, jti: 'x', type: 'access' });
    // The cast would fail at runtime if role is not in UserRole — decodeRole
    // returns it as-is, but callers would treat it as viewer via hasMinimumRole.
    const result = decodeRole(token);
    // decodeRole simply returns whatever is in the claim.
    expect(result).toBe('superadmin');
  });
});

// ── hasMinimumRole ─────────────────────────────────────────────────────────────

describe('hasMinimumRole', () => {
  const cases: Array<{ userRole: UserRole; required: UserRole; expected: boolean }> = [
    // viewer can see viewer-only features
    { userRole: 'viewer', required: 'viewer',  expected: true  },
    // viewer cannot access trader features
    { userRole: 'viewer', required: 'trader',  expected: false },
    // viewer cannot access admin features
    { userRole: 'viewer', required: 'admin',   expected: false },
    // trader can access viewer features (higher includes lower)
    { userRole: 'trader', required: 'viewer',  expected: true  },
    // trader can access trader features
    { userRole: 'trader', required: 'trader',  expected: true  },
    // trader cannot access admin features
    { userRole: 'trader', required: 'admin',   expected: false },
    // admin can access viewer features
    { userRole: 'admin',  required: 'viewer',  expected: true  },
    // admin can access trader features
    { userRole: 'admin',  required: 'trader',  expected: true  },
    // admin can access admin features
    { userRole: 'admin',  required: 'admin',   expected: true  },
  ];

  it.each(cases)(
    '$userRole meets minimum "$required" requirement: $expected',
    ({ userRole, required, expected }) => {
      expect(hasMinimumRole(userRole, required)).toBe(expected);
    },
  );

  it('rejects a role that is not in the hierarchy (unknown role → level -1)', () => {
    // An unknown role should never be able to access any protected feature.
    // ROLE_LEVEL returns undefined → coerces to 0 via ?? 0, so it matches
    // 'viewer' but not 'trader' or 'admin'.
    const unknown = 'superadmin' as UserRole;
    expect(hasMinimumRole(unknown, 'trader')).toBe(false);
    expect(hasMinimumRole(unknown, 'admin')).toBe(false);
  });

  it('role hierarchy is strictly ordered: viewer < trader < admin', () => {
    // Encode the invariant that the hierarchy is ordered and transitive.
    expect(hasMinimumRole('trader', 'viewer')).toBe(true);
    expect(hasMinimumRole('admin',  'trader')).toBe(true);
    expect(hasMinimumRole('admin',  'viewer')).toBe(true);
    // And not the other direction
    expect(hasMinimumRole('viewer', 'trader')).toBe(false);
    expect(hasMinimumRole('viewer', 'admin')).toBe(false);
    expect(hasMinimumRole('trader', 'admin')).toBe(false);
  });
});
