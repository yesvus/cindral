import { headers } from "next/headers";
import { BrokerError, getPoolSnapshot, type PoolSnapshot } from "@/lib/cindral";
import { panelAuthorized } from "@/lib/auth";

export type PoolResult =
  | { ok: true; snapshot: PoolSnapshot }
  | { ok: false; reason: "unauthorized" | "unavailable"; message: string };

/**
 * The panel holds the broker token server-side, so it must prove the caller is
 * an operator before any lease or CI log data is rendered.
 */
export async function loadPool(): Promise<PoolResult> {
  const request = new Request("http://panel.local", {
    headers: await headers(),
  });
  if (!panelAuthorized(request)) {
    return {
      ok: false,
      reason: "unauthorized",
      message: "This panel requires an operator bearer token.",
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
