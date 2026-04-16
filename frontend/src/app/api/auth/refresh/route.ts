import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

/**
 * Token refresh endpoint
 * POST /api/auth/refresh
 * 
 * Forwards refresh token cookie to backend and returns new access token
 */
export async function POST(request: NextRequest) {
  try {
    // Get refresh token cookie from request
    const refreshToken = request.cookies.get('refresh_token')?.value;
    
    if (!refreshToken) {
      return NextResponse.json(
        { error: 'No refresh token provided' },
        { status: 401 }
      );
    }

    // Forward to backend with cookie
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/refresh`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Cookie': `refresh_token=${refreshToken}`,
      },
    });

    if (!response.ok) {
      const error = await response.json();
      return NextResponse.json(
        { error: error.detail || 'Token refresh failed' },
        { status: response.status }
      );
    }

    const data = await response.json();
    
    // Extract new refresh token cookie from backend
    const setCookieHeader = response.headers.get('set-cookie');
    
    // Create response with new access token
    const nextResponse = NextResponse.json({
      access_token: data.access_token,
      token_type: data.token_type,
      expires_in: data.expires_in,
    });

    // Forward new refresh token cookie
    if (setCookieHeader) {
      nextResponse.headers.set('set-cookie', setCookieHeader);
    }

    return nextResponse;
  } catch (error) {
    console.error('[API] Token refresh error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
