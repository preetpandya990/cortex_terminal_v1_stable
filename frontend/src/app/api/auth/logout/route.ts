import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Cookie attributes must exactly match those used when the cookie was set —
// the path in particular must align or the browser will not overwrite/expire it.
const REFRESH_COOKIE_PATH = '/api/auth';

function clearRefreshTokenCookie(response: NextResponse): void {
  response.cookies.set('refresh_token', '', {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'strict',
    path: REFRESH_COOKIE_PATH,
    maxAge: 0,
  });
}

export async function POST(request: NextRequest) {
  try {
    // Get tokens from request
    const refreshToken = request.cookies.get('refresh_token')?.value;
    const authHeader = request.headers.get('authorization');

    // Call backend logout
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/logout`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(authHeader && { 'Authorization': authHeader }),
        ...(refreshToken && { 'Cookie': `refresh_token=${refreshToken}` }),
      },
    });

    const nextResponse = NextResponse.json({ status: 'logged_out' });
    clearRefreshTokenCookie(nextResponse);
    return nextResponse;
  } catch (error) {
    console.error('[API] Logout error:', error);
    const nextResponse = NextResponse.json({ status: 'logged_out' });
    clearRefreshTokenCookie(nextResponse);
    return nextResponse;
  }
}
