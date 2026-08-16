import { NextRequest, NextResponse } from "next/server";

import { parseFunnelEvent } from "@/lib/funnel";
import { isSameOriginRequest } from "@/lib/http";

const MAX_BODY_BYTES = 256;

/**
 * Receive anonymous, first-party product-funnel counters.
 *
 * Deliberate omissions from the log are as important as what it contains: no IP,
 * user agent, request URL, referrer, cookie, account, market, order or persistent
 * browser identifier. Vercel may still retain ordinary request metadata in its
 * platform access logs; this application log adds none of it.
 */
export async function POST(request: NextRequest) {
  if (!isSameOriginRequest(request)) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }

  if (!request.headers.get("content-type")?.includes("application/json")) {
    return NextResponse.json({ error: "invalid content type" }, { status: 415 });
  }

  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload too large" }, { status: 413 });
  }

  let input: unknown;
  try {
    const raw = await request.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
      return NextResponse.json({ error: "payload too large" }, { status: 413 });
    }
    input = JSON.parse(raw) as unknown;
  } catch {
    return NextResponse.json({ error: "invalid event" }, { status: 400 });
  }

  const event = parseFunnelEvent(input);
  if (!event) {
    return NextResponse.json({ error: "invalid event" }, { status: 400 });
  }

  console.info(
    JSON.stringify({
      level: "info",
      event: `funnel.${event.name}`,
      source: event.source,
      ...(event.placement ? { placement: event.placement } : {}),
      environment: process.env.VERCEL_ENV ?? process.env.NODE_ENV ?? "unknown",
      recordedAt: new Date().toISOString(),
    }),
  );

  return new NextResponse(null, {
    status: 204,
    headers: { "cache-control": "no-store" },
  });
}
