import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AdminResourceNotExposedError,
  AdminUnauthenticatedError,
} from "@/lib/admin-core";

const getPoolSnapshot = vi.fn();
const getJob = vi.fn();
const currentAdminSession = vi.fn();

vi.mock("@/lib/auth", () => ({
  currentAdminSession: () => currentAdminSession(),
}));

vi.mock("@/lib/cindral", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/cindral")>();
  return {
    ...actual,
    getPoolSnapshot: (...args: unknown[]) => getPoolSnapshot(...args),
    getJob: (...args: unknown[]) => getJob(...args),
  };
});

// The store decides on the server, so its reads are tested through the same
// boundary a request goes through rather than by calling the broker directly.
const { BrokerReadOnlyError, brokerPersistence, readPool } = await import(
  "@/lib/broker-store"
);
const { DEVICE_RESOURCE, JOB_RESOURCE } = await import("@/lib/resources");

const snapshot = {
  devices: [{ name: "device-alpha" }, { name: "device-beta" }],
  recent_jobs: [{ id: "run-1" }, { id: "run-2" }],
  queue_depth: { pending: 1, running: 0, success: 2, failure: 0 },
  expired_lease_count: 0,
  oldest_pending_age_seconds: 0,
  reclaim_count: 0,
  success_count: 2,
  failure_count: 0,
};

beforeEach(() => {
  getPoolSnapshot.mockReset().mockResolvedValue(snapshot);
  getJob.mockReset();
  currentAdminSession
    .mockReset()
    .mockResolvedValue({ email: "operator@example.com", role: "operator" });
});

describe("the read-only store", () => {
  it("answers the two resources the panel declares, from one broker read", async () => {
    // the counters a page shows beside these rows are off the same snapshot, so a device
    // count and a queue depth are never measured at two different instants
    const { snapshot: read, devices, jobs } = await readPool();
    expect(devices.map((d) => d.name)).toEqual(["device-alpha", "device-beta"]);
    expect(jobs.map((j) => j.id)).toEqual(["run-1", "run-2"]);
    expect(read.queue_depth.pending).toBe(1);
    expect(getPoolSnapshot).toHaveBeenCalledTimes(1);
  });

  it("reads a device by the name it is keyed on", async () => {
    await expect(
      brokerPersistence.read<{ name: string }>(DEVICE_RESOURCE, "device-beta"),
    ).resolves.toEqual({ name: "device-beta" });
    await expect(brokerPersistence.read(DEVICE_RESOURCE, "device-gamma")).resolves.toBeNull();
  });

  it("answers null for a run the broker does not hold", async () => {
    // the contract's null is a different answer from a broker that could not be asked, and
    // the API route turns it into a 404 rather than a 503
    const { BrokerError } = await import("@/lib/cindral");
    getJob.mockRejectedValue(new BrokerError("unavailable", "job not found"));
    await expect(brokerPersistence.read(JOB_RESOURCE, "run-9")).resolves.toBeNull();
  });

  it("lets a broker that is down be a failure, not a missing row", async () => {
    const { BrokerError } = await import("@/lib/cindral");
    getJob.mockRejectedValue(new BrokerError("unavailable", "the Cindral broker did not respond"));
    await expect(brokerPersistence.read(JOB_RESOURCE, "run-1")).rejects.toThrow(
      "the Cindral broker did not respond",
    );
  });

  it("refuses a resource name the panel does not expose", async () => {
    await expect(brokerPersistence.query("secrets")).rejects.toThrow(
      AdminResourceNotExposedError,
    );
    await expect(brokerPersistence.read("secrets", "x")).rejects.toThrow(
      AdminResourceNotExposedError,
    );
  });

  it("has no write of its own to make", async () => {
    // the last line behind the permission rule and the column check, and the only one that
    // says why: the panel holds the read-only pool token
    for (const call of [
      brokerPersistence.create(DEVICE_RESOURCE, { name: "device-gamma" }),
      brokerPersistence.update(DEVICE_RESOURCE, "device-alpha", { busy: false }),
      brokerPersistence.delete(JOB_RESOURCE, "run-1"),
    ]) {
      await expect(call).rejects.toThrow(BrokerReadOnlyError);
    }
    expect(getJob).not.toHaveBeenCalled();
  });

  it("refuses a read with no session before it reaches the broker", async () => {
    const { resourceActions } = await import("@/lib/broker-store");
    currentAdminSession.mockResolvedValue(null);
    await expect(resourceActions.query(DEVICE_RESOURCE)).rejects.toThrow(
      AdminUnauthenticatedError,
    );
    expect(getPoolSnapshot).not.toHaveBeenCalled();
  });

  it("refuses a read from a session the rule does not name", async () => {
    const { resourceActions } = await import("@/lib/broker-store");
    currentAdminSession.mockResolvedValue({ email: "a@b.c", role: "viewer" });
    await expect(resourceActions.read(JOB_RESOURCE, "run-1")).rejects.toThrow(
      /may not jobs\.read/,
    );
    expect(getPoolSnapshot).not.toHaveBeenCalled();
    expect(getJob).not.toHaveBeenCalled();
  });
});
