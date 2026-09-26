import { NextResponse } from "next/server";
import { getPoolSnapshot } from "@/lib/cindral";

export async function GET() {
  const snapshot = await getPoolSnapshot();
  if (!snapshot) {
    return NextResponse.json({ error: "Failed to fetch pool snapshot" }, { status: 502 });
  }
  return NextResponse.json(snapshot);
}
