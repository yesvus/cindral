import { timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";

const PANEL_TOKEN = process.env.CINDRAL_CONTROL_PANEL_TOKEN || "";

/**
 * The broker snapshot is bearer-protected, and this app holds that token
 * server-side. Without a check here, anyone who can reach the panel reads
 * device leases and CI logs, bypassing the broker gate entirely.
 */
export function panelAuthorized(request: Request): boolean {
  if (!PANEL_TOKEN) {
    return false;
  }
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Bearer ")) {
    return false;
  }
  const presented = Buffer.from(header.slice("Bearer ".length).trim());
  const expected = Buffer.from(PANEL_TOKEN);
  return (
    presented.length === expected.length && timingSafeEqual(presented, expected)
  );
}

export function unauthorized(): NextResponse {
  return NextResponse.json(
    { error: "the control panel requires a bearer token" },
    { status: 401 },
  );
}
