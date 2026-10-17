/**
 * The panel's store: the broker, behind the write boundary.
 *
 * Reads go through `resourceActions` rather than calling the broker directly, so the guard and the
 * declared columns sit in front of every served answer instead of only in front of the writes there
 * are not yet. A write is refused four times over, and the order is the point: a name outside the
 * exposed set, then the permission rule, then the definition's columns, then this store. The rule
 * is a read-only rule, so in production it is the second of those that stops a write, and the
 * store's own refusal is the last line rather than the first.
 */
import { cache } from "react";
import {
  AdminResourceNotExposedError,
  createAdminResourceActions,
  type AdminPersistenceAdapter,
} from "@/lib/admin-core";
import {
  BrokerError,
  getJob,
  getPoolSnapshot,
  type DeviceInfo,
  type JobInfo,
  type PoolSnapshot,
} from "@/lib/cindral";
import { requirePermission } from "@/lib/permissions";
import {
  DEVICE_RESOURCE,
  JOB_RESOURCE,
  exposedResource,
  resourceDefinitions,
} from "@/lib/resources";

/**
 * One broker read per request. The layout's build badge and the page beneath it each want the
 * snapshot, and the panel records the instant it read the broker as the clock every relative time
 * on the page is measured from, so two reads would be two instants to reconcile.
 */
export const readPoolSnapshot = cache((): Promise<PoolSnapshot> =>
  getPoolSnapshot(),
);

/**
 * A query field the panel's own, carrying a snapshot the caller has already read.
 *
 * The adapter's `query` argument is a map of fields to values by contract, so a field of this
 * panel's own naming is a legal thing to put there. Without it a page showing queue counters beside
 * device rows causes a second broker read, and the two halves of the page are two instants.
 */
const SNAPSHOT_FIELD = "cindralSnapshot";

function snapshotFrom(query: unknown): PoolSnapshot | undefined {
  if (!query || typeof query !== "object") {
    return undefined;
  }
  const value = (query as Record<string, unknown>)[SNAPSHOT_FIELD];
  return value && typeof value === "object" ? (value as PoolSnapshot) : undefined;
}

export class BrokerReadOnlyError extends Error {
  readonly operation: string;
  readonly resource: string;

  constructor(operation: string, resource: string) {
    super(
      `the panel holds the read-only pool token, so ${operation} on ${resource} has nowhere to go`,
    );
    this.name = "BrokerReadOnlyError";
    this.operation = operation;
    this.resource = resource;
  }
}

export const brokerPersistence: AdminPersistenceAdapter = {
  async read<T>(resource: string, id: string): Promise<T | null> {
    if (resource === DEVICE_RESOURCE) {
      const { devices } = await readPoolSnapshot();
      return (devices.find((device) => device.name === id) as T | undefined) ?? null;
    }
    if (resource === JOB_RESOURCE) {
      try {
        return (await getJob(id)) as T;
      } catch (error) {
        // the contract answers null for a row that is not there, which is a different
        // answer from a broker that could not be asked
        if (error instanceof BrokerError && error.message === "job not found") {
          return null;
        }
        throw error;
      }
    }
    throw new AdminResourceNotExposedError(resource);
  },

  async query<T>(resource: string, query?: Record<string, unknown>): Promise<T[]> {
    const snapshot = snapshotFrom(query) ?? (await readPoolSnapshot());
    if (resource === DEVICE_RESOURCE) {
      return snapshot.devices as T[];
    }
    if (resource === JOB_RESOURCE) {
      return snapshot.recent_jobs as T[];
    }
    throw new AdminResourceNotExposedError(resource);
  },

  create: async (resource) => {
    throw new BrokerReadOnlyError("create", resource);
  },
  update: async (resource) => {
    throw new BrokerReadOnlyError("update", resource);
  },
  delete: async (resource) => {
    throw new BrokerReadOnlyError("delete", resource);
  },
};

export const resourceActions = createAdminResourceActions({
  guard: requirePermission,
  persistence: brokerPersistence,
  expose: exposedResource,
  definitions: resourceDefinitions,
});

/**
 * One broker read, through the boundary, with the rows and the counters off that one snapshot.
 *
 * The snapshot goes into each query so a page showing queue counters beside device rows is not
 * showing two instants, and it is read here rather than left to the layout's badge so a route
 * handler, which has no render for the cache to memoise inside, reads the broker once too.
 */
export async function readPool(): Promise<{
  snapshot: PoolSnapshot;
  devices: DeviceInfo[];
  jobs: JobInfo[];
}> {
  const snapshot = await readPoolSnapshot();
  const [devices, jobs] = await Promise.all([
    resourceActions.query<DeviceInfo>(DEVICE_RESOURCE, { [SNAPSHOT_FIELD]: snapshot }),
    resourceActions.query<JobInfo>(JOB_RESOURCE, { [SNAPSHOT_FIELD]: snapshot }),
  ]);
  return { snapshot, devices, jobs };
}
