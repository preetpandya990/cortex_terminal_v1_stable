import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

/**
 * Logout endpoint
 * POST /api/auth/logout
 */
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

    // Create response
    const nextResponse = NextResponse.json({ status: 'logged_out' });

    // Clear refresh token cookie
    nextResponse.cookies.delete('refresh_token');

    return nextResponse;
  } catch (error) {
    console.error('[API] Logout error:', error);
    // Even if backend fails, clear local cookies
    const nextResponse = NextResponse.json({ status: 'logged_out' });
    nextResponse.cookies.delete('refresh_token');
    return nextResponse;
  }
}
