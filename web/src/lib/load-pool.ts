import { AdminPermissionDeniedError } from "@/lib/admin-core";
import { BrokerError } from "@/lib/cindral";
import { readPool } from "@/lib/broker-store";
import { currentAdminSession } from "@/lib/auth";

export type PoolFailureReason = "unauthorized" | "forbidden" | "unavailable";

export type PoolResult =
  | {
      ok: true;
      /** the broker's own snapshot, for the counters derived from it */
      snapshot: Awaited<ReturnType<typeof readPool>>["snapshot"];
      devices: Awaited<ReturnType<typeof readPool>>["devices"];
      jobs: Awaited<ReturnType<typeof readPool>>["jobs"];
      fetchedAt: number;
    }
  | { ok: false; reason: PoolFailureReason; message: string };

/**
 * The pool, for a page.
 *
 * The two resources come back through the write boundary and the counters come from the same
 * snapshot, so a page cannot show a device count and a queue depth measured at two different
 * instants.
 */
export async function loadPool(): Promise<PoolResult> {
  if (!(await currentAdminSession())) {
    return {
      ok: false,
      reason: "unauthorized",
      message: "Sign in to view the runner pool.",
    };
  }
  try {
    const { snapshot, devices, jobs } = await readPool();
    // the snapshot carries no clock of its own, so the panel records when it
    // read the broker: every relative time on the page is measured from here
    return { ok: true, snapshot, devices, jobs, fetchedAt: Date.now() / 1000 };
  } catch (error) {
    // A refusal is not an outage. The routes already tell the two apart with
    // permissionRefusal, and the pages did not: an AdminPermissionDeniedError fell
    // through to "unavailable", so a session the rule denies was shown "Broker
    // unavailable" and sent to look at the broker. Inert with one role, live the
    // day there are two, and wrong the moment there is.
    if (error instanceof AdminPermissionDeniedError) {
      return { ok: false, reason: "forbidden", message: error.message };
    }
    if (error instanceof BrokerError) {
      return { ok: false, reason: error.reason, message: error.message };
    }
    return { ok: false, reason: "unavailable", message: "pool snapshot failed" };
  }
}

/**
 * What a page says when the pool did not load, for one reason.
 *
 * Shared by the three pages that render this banner, so the mapping is one testable
 * function rather than three copies that can drift. The refusal case is the one that
 * matters: it is a decision about the session, not about the broker, and a page that
 * calls it an outage sends an operator to investigate the wrong thing.
 */
export function poolFailureTitle(reason: PoolFailureReason): string {
  if (reason === "unauthorized") return "Sign in required";
  if (reason === "forbidden") return "Not permitted";
  return "Broker unavailable";
}
