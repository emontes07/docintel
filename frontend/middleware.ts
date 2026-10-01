import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(request: NextRequest) {
  // Get the pathname of the request
  const path = request.nextUrl.pathname;
  const hosted = process.env.NODE_ENV === 'production' || Boolean(process.env.CONTAINER_APP_NAME || process.env.IDENTITY_ENDPOINT);
  if (hosted && path.startsWith('/api/') &&
      path !== '/api/auth' && !path.startsWith('/api/auth/') &&
      path !== '/api/batches' && !path.startsWith('/api/batches/')) {
    return NextResponse.json({ detail: 'Route unavailable in hosted batch mode' }, { status: 404, headers: { 'Cache-Control': 'no-store' } });
  }

  if (path === '/api/batches' || path.startsWith('/api/batches/')) {
    if (request.method === 'OPTIONS' || request.headers.get('sec-fetch-site') === 'cross-site') {
      return NextResponse.json({ detail: 'Same-origin batch access required' }, { status: 403 });
    }
    const response = NextResponse.next();
    response.headers.set('Cache-Control', 'no-store');
    return response;
  }

  if (path === '/api/pilot' || path.startsWith('/api/pilot/')) {
    const host = request.headers.get('host') || '';
    const origin = request.headers.get('origin');
    if (!/^(127\.0\.0\.1|localhost):\d+$/.test(host) ||
        (origin && origin !== `http://${host}`) ||
        request.headers.get('sec-fetch-site') === 'cross-site' ||
        request.method === 'OPTIONS') {
      return NextResponse.json({ detail: 'Local same-origin access only' }, { status: 403 });
    }
    const response = NextResponse.next();
    response.headers.set('Cache-Control', 'no-store');
    return response;
  }

  // Handle OPTIONS request for CORS preflight
  if (request.method === 'OPTIONS') {
    // Return response with CORS headers
    return new NextResponse(null, {
      status: 200,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Requested-With',
        'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Max-Age': '86400', // 24 hours
      },
    });
  }

  // For other requests, just proceed as normal
  const response = NextResponse.next();
  
  // Set CORS headers for API routes
  if (path.startsWith('/api/')) {
    response.headers.set('Access-Control-Allow-Origin', '*');
    response.headers.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
    response.headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With');
    response.headers.set('Access-Control-Allow-Credentials', 'true');
  }

  return response;
}

// Configure the paths where this middleware should run
export const config = {
  matcher: [
    // Apply to all API routes
    '/api/:path*',
  ],
}; 