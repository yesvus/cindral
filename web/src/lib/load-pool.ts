import { BrokerError, getPoolSnapshot, type PoolSnapshot } from "@/lib/cindral";
import { currentSessionEmail } from "@/lib/auth";

export type PoolResult =
  | { ok: true; snapshot: PoolSnapshot }
  | { ok: false; reason: "unauthorized" | "unavailable"; message: string };

export async function loadPool(): Promise<PoolResult> {
  if (!(await currentSessionEmail())) {
    return {
      ok: false,
      reason: "unauthorized",
      message: "Sign in to view the runner pool.",
    };
  }
  try {
    return { ok: true, snapshot: await getPoolSnapshot() };
  } catch (error) {
    if (error instanceof BrokerError) {
      return { ok: false, reason: error.reason, message: error.message };
    }
    return { ok: false, reason: "unavailable", message: "pool snapshot failed" };
  }
}
