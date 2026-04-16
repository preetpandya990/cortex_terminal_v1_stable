import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

/**
 * Dev-only login endpoint
 * POST /api/auth/dev-login
 */
export async function POST(request: NextRequest) {
  try {
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/dev-login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(
        { error: error.detail || 'Login failed' },
        { status: response.status }
      );
    }

    const data = await response.json();
    
    // Extract refresh token from backend response cookies
    const setCookieHeader = response.headers.get('set-cookie');
    
    // Create response with access token
    const nextResponse = NextResponse.json({
      access_token: data.access_token,
      token_type: data.token_type,
      expires_in: data.expires_in,
    });

    // Forward refresh token cookie from backend
    if (setCookieHeader) {
      nextResponse.headers.set('set-cookie', setCookieHeader);
    }

    return nextResponse;
  } catch (error) {
    console.error('[API] Dev login error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
