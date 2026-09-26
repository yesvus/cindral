import { NextResponse } from "next/server";
import { panelAuthorized, unauthorized } from "@/lib/auth";
import { BrokerError, getPoolSnapshot } from "@/lib/cindral";

export async function GET(request: Request) {
  if (!panelAuthorized(request)) {
    return unauthorized();
  }
  try {
    return NextResponse.json(await getPoolSnapshot());
  } catch (error) {
    if (error instanceof BrokerError) {
      const status = error.reason === "unauthorized" ? 502 : 503;
      return NextResponse.json({ error: error.message }, { status });
    }
    return NextResponse.json({ error: "pool snapshot failed" }, { status: 503 });
  }
}
