import { NextResponse } from "next/server";
import { BrokerError } from "@/lib/cindral";
import { resourceActions } from "@/lib/broker-store";
import { JOB_RESOURCE, readJobs } from "@/lib/resources";
import { permissionRefusal, requirePermission } from "@/lib/permissions";
import type { JobInfo } from "@/lib/cindral";

export async function GET(
  request: Request,
  props: { params: Promise<{ id: string }> }
) {
  const { id } = await props.params;
  if (request.headers.get("accept")?.includes("text/html")) {
    return new Response(null, {
      status: 303,
      headers: { Location: `/jobs/${encodeURIComponent(id)}` },
    });
  }
  try {
    await requirePermission(readJobs);
    const job = await resourceActions.read<JobInfo>(JOB_RESOURCE, id);
    if (!job) {
      return NextResponse.json({ error: "job not found" }, { status: 404 });
    }
    return NextResponse.json(job);
  } catch (error) {
    const refusal = permissionRefusal(readJobs, error);
    if (refusal) {
      return refusal;
    }
    if (error instanceof BrokerError) {
      return NextResponse.json(
        { error: error.message },
        { status: error.reason === "unauthorized" ? 502 : 503 },
      );
    }
    return NextResponse.json({ error: "job lookup failed" }, { status: 503 });
  }
}
