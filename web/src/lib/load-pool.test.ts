import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminPermissionDeniedError } from "@/lib/admin-core";

const currentAdminSession = vi.fn();
const getPoolSnapshot = vi.fn();

vi.mock("@/lib/auth", () => ({
  currentAdminSession: () => currentAdminSession(),
}));
vi.mock("@/lib/broker-store", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/broker-store")>();
  return {
    ...actual,
    readPool: vi.fn(async () => {
      const snapshot = await getPoolSnapshot();
      return { snapshot, devices: [], jobs: [] };
    }),
  };
});
vi.mock("@/lib/cindral", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/cindral")>();
  return { ...actual, BrokerError: actual.BrokerError };
});

import { BrokerError } from "@/lib/cindral";
import { loadPool, poolFailureTitle } from "@/lib/load-pool";

describe("a page's view of why the pool did not load", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentAdminSession.mockResolvedValue({ email: "o@e.c", role: "operator" });
  });

  it("reports a permission refusal as a refusal, not as the broker being down", async () => {
    // The routes already told these two apart with permissionRefusal. A page did
    // not, so a session the rule denies was shown "Broker unavailable" and sent to
    // investigate the broker. Inert while there is one role, and the first thing
    // that breaks the day there are two.
    getPoolSnapshot.mockRejectedValue(
      new AdminPermissionDeniedError({
        permission: "devices.read",
        session: { email: "o@e.c", role: "viewer" },
        reason: "denied",
      }),
    );
    const result = await loadPool();
    expect(result).toMatchObject({ ok: false, reason: "forbidden" });
    expect(result.ok === false && result.reason).not.toBe("unavailable");
  });

  it("still reports a broker outage as an outage", async () => {
    getPoolSnapshot.mockRejectedValue(new BrokerError("unavailable", "no response"));
    const result = await loadPool();
    expect(result).toMatchObject({ ok: false, reason: "unavailable" });
  });

  it("reports an unauthenticated session before it asks the broker", async () => {
    currentAdminSession.mockResolvedValue(null);
    getPoolSnapshot.mockClear();
    const result = await loadPool();
    expect(result).toMatchObject({ ok: false, reason: "unauthorized" });
    expect(getPoolSnapshot).not.toHaveBeenCalled();
  });
});

describe("what a page says when the pool did not load", () => {
  it("names a refusal as a refusal rather than as the broker being down", () => {
    // The three pages render this string, so it is the only place a person sees the
    // distinction. It was untested when it was three copies of a ternary, which is
    // how the refusal came to be reported as an outage in the first place.
    expect(poolFailureTitle("forbidden")).toBe("Not permitted");
    expect(poolFailureTitle("unavailable")).toBe("Broker unavailable");
    expect(poolFailureTitle("unauthorized")).toBe("Sign in required");
  });
});
