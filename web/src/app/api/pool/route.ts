import { NextResponse } from "next/server";
import { withSession } from "@/lib/guard";
import { BrokerError, getPoolSnapshot } from "@/lib/cindral";

export async function GET() {
  const guard = await withSession();
  if ("response" in guard) {
    return guard.response;
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
