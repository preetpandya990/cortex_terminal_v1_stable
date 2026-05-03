import { type NextRequest, NextResponse } from 'next/server';

// Paths that are accessible without authentication
const PUBLIC_PATHS = new Set(['/login']);

// Prefixes to skip entirely — Next.js internals and all API routes handle
// their own auth; middleware should never interfere with them.
const SKIP_PREFIXES = ['/_next/', '/api/', '/favicon.ico', '/icons/', '/images/'];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Short-circuit for static assets and API routes
  if (SKIP_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    return NextResponse.next();
  }

  // Cookie presence is used as a fast hint for redirect decisions only.
  // Cryptographic token validation happens inside AuthContext on the client
  // via the /api/auth/refresh silent-refresh flow.
  const hasSession = request.cookies.has('refresh_token');
  const isPublicPath = PUBLIC_PATHS.has(pathname);

  if (!hasSession && !isPublicPath) {
    const loginUrl = new URL('/login', request.url);
    // Preserve the intended destination so the login page can redirect back
    if (pathname !== '/') {
      loginUrl.searchParams.set('next', pathname);
    }
    return NextResponse.redirect(loginUrl);
  }

  if (hasSession && isPublicPath) {
    return NextResponse.redirect(new URL('/', request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
