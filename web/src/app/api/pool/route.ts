import { NextResponse } from "next/server";
import { BrokerError } from "@/lib/cindral";
import { readPool } from "@/lib/broker-store";
import { readDevices } from "@/lib/resources";
import { permissionRefusal, requirePermission } from "@/lib/permissions";

export const dynamic = "force-dynamic";

/**
 * The snapshot, for a client that wants the whole thing.
 *
 * The two resources are read through the boundary, so the guard and the declared columns apply to
 * them, and the queue counters ride alongside from the same snapshot so the response keeps the
 * shape it has always had. Those counters are aggregates of the snapshot rather than a stored row,
 * so there is no definition to hold a write to them.
 */
export async function GET() {
  try {
    await requirePermission(readDevices);
    const { snapshot, devices, jobs } = await readPool();
    return NextResponse.json({ ...snapshot, devices, jobs });
  } catch (error) {
    const refusal = permissionRefusal(readDevices, error);
    if (refusal) {
      return refusal;
    }
    if (error instanceof BrokerError) {
      return NextResponse.json(
        { error: error.message },
        { status: error.reason === "unauthorized" ? 502 : 503 },
      );
    }
    return NextResponse.json({ error: "pool snapshot failed" }, { status: 503 });
  }
}
