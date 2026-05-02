/**
 * JWT Payload Decoder
 * ====================
 * Client-side utility to read public claims from a JWT access token.
 * Does NOT verify the signature — that is the server's responsibility.
 * Safe to use for UI personalisation (role-gated nav, display name, etc.).
 */

export type UserRole = "viewer" | "trader" | "admin";

export interface JwtClaims {
  sub: string;
  role: UserRole;
  exp: number;
  iat: number;
  [key: string]: unknown;
}

/**
 * Base64url → Base64 → JSON.
 * Returns null on any parse error so callers don't need try/catch.
 */
export function decodeJwtPayload(token: string): JwtClaims | null {
  try {
    const segment = token.split(".")[1];
    if (!segment) return null;
    // Base64url → Base64
    const base64 = segment.replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join("")
    );
    return JSON.parse(json) as JwtClaims;
  } catch {
    return null;
  }
}

export function decodeRole(token: string | null): UserRole {
  if (!token) return "viewer";
  return decodeJwtPayload(token)?.role ?? "viewer";
}

/** Role hierarchy — higher number = more access. */
const ROLE_LEVEL: Record<UserRole, number> = {
  viewer: 0,
  trader: 1,
  admin: 2,
};

export function hasMinimumRole(userRole: UserRole, required: UserRole): boolean {
  return (ROLE_LEVEL[userRole] ?? 0) >= (ROLE_LEVEL[required] ?? 999);
}
