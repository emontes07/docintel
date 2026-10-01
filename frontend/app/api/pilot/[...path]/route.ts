import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const host = request.headers.get("host") || "";
  const origin = request.headers.get("origin");
  if (!/^(127\.0\.0\.1|localhost):\d+$/.test(host) ||
      (origin && origin !== `http://${host}`) ||
      request.headers.get("sec-fetch-site") === "cross-site" ||
      request.headers.get("x-docintel-local") !== "1") {
    return NextResponse.json({ detail: "Local same-origin access only" }, { status: 403 });
  }
  const { path } = await context.params;
  const route = path.join("/");
  if (!/^(catalog|runs|runs\/[a-f0-9-]{36}(\/(reviews|export))?)$/.test(route)) {
    return NextResponse.json({ detail: "Unknown pilot route" }, { status: 404 });
  }
  const backend = process.env.DOCINTEL_PILOT_API || "http://127.0.0.1:8011";
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(backend)) {
    return NextResponse.json({ detail: "Pilot backend must use loopback" }, { status: 503 });
  }
  try {
    const body = request.method === "POST" ? await request.text() : undefined;
    if (body && body.length > 16000) {
      return NextResponse.json({ detail: "Request too large" }, { status: 413 });
    }
    const response = await fetch(`${backend}/${route}`, {
      method: request.method,
      headers: { "Content-Type": "application/json", "X-DocIntel-Local": "1" },
      body, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000),
    });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        ...(response.headers.get("content-disposition") ? { "Content-Disposition": response.headers.get("content-disposition")! } : {}),
      },
    });
  } catch {
    return NextResponse.json({ detail: "Local backend unavailable. A submitted run may still be active; refresh before submitting again." }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;