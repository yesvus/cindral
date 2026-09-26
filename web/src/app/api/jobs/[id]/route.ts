import { NextResponse } from "next/server";
import { panelAuthorized, unauthorized } from "@/lib/auth";
import { BrokerError, getJob } from "@/lib/cindral";

export async function GET(
  request: Request,
  props: { params: Promise<{ id: string }> }
) {
  if (!panelAuthorized(request)) {
    return unauthorized();
  }
  const { id } = await props.params;
  try {
    return NextResponse.json(await getJob(id));
  } catch (error) {
    if (error instanceof BrokerError) {
      const status =
        error.reason === "unauthorized" ? 502 : error.message === "job not found" ? 404 : 503;
      return NextResponse.json({ error: error.message }, { status });
    }
    return NextResponse.json({ error: "job lookup failed" }, { status: 503 });
  }
}
