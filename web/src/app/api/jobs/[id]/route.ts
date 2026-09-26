import { NextResponse } from "next/server";
import { withSession } from "@/lib/guard";
import { BrokerError, getJob } from "@/lib/cindral";

export async function GET(
  _request: Request,
  props: { params: Promise<{ id: string }> }
) {
  const guard = await withSession();
  if ("response" in guard) {
    return guard.response;
  }
  const { id } = await props.params;
  try {
    return NextResponse.json(await getJob(id));
  } catch (error) {
    if (error instanceof BrokerError) {
      const status =
        error.reason === "unauthorized"
          ? 502
          : error.message === "job not found"
            ? 404
            : 503;
      return NextResponse.json({ error: error.message }, { status });
    }
    return NextResponse.json({ error: "job lookup failed" }, { status: 503 });
  }
}
