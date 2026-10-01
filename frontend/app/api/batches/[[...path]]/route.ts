import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path?: string[] }> }) {
  const { path = [] } = await context.params;
  const route = path.join("/");
  const allowed = /^(?:|catalog|validate|[a-f0-9]{64}(?:\/(?:submit|export|items(?:\/row-\d+(?:\/reviews)?)?))?)$/;
  if (!allowed.test(route)) return NextResponse.json({ detail: "Unknown batch route" }, { status: 404 });
  const host = request.headers.get("host") || "";
  const development = process.env.NODE_ENV !== "production" && !process.env.CONTAINER_APP_NAME && !process.env.IDENTITY_ENDPOINT && process.env.DOCINTEL_BATCH_DEV === "true" && /^(localhost|127\.0\.0\.1)(:\d+)?$/.test(host);
  const portalOrigin = development ? `http://${host}` : process.env.DOCINTEL_PORTAL_ORIGIN;
  if (!portalOrigin || (!development && !/^https:\/\/[^/]+$/.test(portalOrigin))) return NextResponse.json({ detail: "Trusted portal origin is not configured" }, { status: 503 });
  const origin = request.headers.get("origin");
  if ((origin && origin !== portalOrigin) || request.headers.get("sec-fetch-site") === "cross-site") {
    return NextResponse.json({ detail: "Same-origin batch access required" }, { status: 403 });
  }
  const base = process.env.DOCINTEL_BATCH_API_URL;
  if (!base) return NextResponse.json({ detail: "Batch backend is not configured" }, { status: 503 });
  try {
    const destination = new URL(base);
    if (destination.username || destination.password || destination.search || destination.hash || (!development && destination.protocol !== "https:") || (development && !["127.0.0.1", "localhost"].includes(destination.hostname))) throw new Error();
    const headers: Record<string, string> = {};
    if (development) headers["X-DocIntel-Development"] = "1";
    else {
      const token = await getToken({ req: request, secret: process.env.AUTH_SECRET, secureCookie: true });
      if (!token || typeof token.batchAccessToken !== "string" || typeof token.batchExpiresAt !== "number" || token.batchExpiresAt * 1000 <= Date.now()) {
        return NextResponse.json({ detail: "Sign in again to obtain an authorized batch API token" }, { status: 401 });
      }
      headers.Authorization = `Bearer ${token.batchAccessToken}`;
    }
    const contentType = request.headers.get("content-type");
    if (contentType) headers["Content-Type"] = contentType;
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (request.body) {
      const reader = request.body.getReader();
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        size += next.value.byteLength;
        if (size > 21 * 1024 * 1024) {
          await reader.cancel();
          return NextResponse.json({ detail: "Workbook request exceeds 21 MiB" }, { status: 413 });
        }
        chunks.push(next.value);
      }
    }
    const response = await fetch(`${base.replace(/\/$/, "")}${route ? "/" + route : ""}${request.nextUrl.search}`, { method: request.method, headers, body: chunks.length ? Buffer.concat(chunks) : undefined, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(120000) });
    return new NextResponse(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") || "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", ...(response.headers.get("content-disposition") ? { "Content-Disposition": 'attachment; filename="docintel-batch.xlsx"' } : {}) } });
  } catch {
    return NextResponse.json({ detail: "Batch backend or authentication unavailable; check approved server configuration" }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;