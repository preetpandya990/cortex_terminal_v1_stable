import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

export async function POST(_request: NextRequest) {
  // Hard block outside of local development. The backend enforces this too,
  // but defence-in-depth means neither layer trusts the other to catch it.
  if (process.env.NODE_ENV !== 'development') {
    return NextResponse.json({ error: 'Not Found' }, { status: 404 });
  }

  try {
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/dev-login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(
        { error: error.detail || 'Login failed' },
        { status: response.status },
      );
    }

    const data = await response.json();

    const nextResponse = NextResponse.json({
      access_token: data.access_token,
      token_type: data.token_type,
      expires_in: data.expires_in,
    });

    if (data.refresh_token) {
      // Session-only cookie — no maxAge/expires means the browser discards it
      // when the window closes. Dev bypass sessions must never outlive the
      // browser session; persistent dev cookies cause unintended cold-load
      // auto-login. Real user sessions (via /auth/login) use a 7-day cookie
      // controlled by the "remember me" preference on the login page.
      nextResponse.cookies.set('refresh_token', data.refresh_token, {
        httpOnly: true,
        secure: false, // localhost has no TLS in dev
        sameSite: 'strict',
        path: '/api/auth',
      });
    }

    return nextResponse;
  } catch (error) {
    console.error('[API] Dev login error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
