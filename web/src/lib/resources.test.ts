import { describe, expect, it } from "vitest";
import {
  AdminResourceFieldError,
  AdminResourceNotExposedError,
  createAdminPermissionGuard,
  createAdminResourceActions,
  defineAdminResource,
  type AdminPersistenceAdapter,
  type AdminSession,
  type AdminPermissionRule,
} from "@/lib/admin-core";
import { can } from "@/lib/permissions";
import {
  DEVICE_RESOURCE,
  JOB_RESOURCE,
  deviceResource,
  exposedResource,
  jobResource,
  resourceDefinitions,
  readDevices,
  readJobs,
} from "@/lib/resources";
import type { DeviceInfo, JobInfo } from "@/lib/cindral";

/**
 * What the 0.5.1 write boundary does to this panel's records.
 *
 * The panel ships no write, so these are not tests of a path in use. They are the reason the
 * definitions exist: the boundary reads a definition's `columns` and refuses a write carrying a
 * key none of them name, so the set of keys a record may carry is a declaration in this repository
 * rather than whatever a caller sends. A test that stops being true when a definition changes is
 * the thing that makes a definition change deliberate.
 */
const operator: AdminSession = { email: "operator@example.com", role: "operator" };

function recordingStore() {
  const writes: Array<{ operation: string; resource: string; value: unknown }> = [];
  const persistence: AdminPersistenceAdapter = {
    read: async () => null,
    query: async () => [],
    create: async <T>(resource: string, value: unknown): Promise<T> => {
      writes.push({ operation: "create", resource, value });
      return value as T;
    },
    update: async <T>(resource: string, id: string, value: unknown): Promise<T> => {
      writes.push({ operation: "update", resource, value });
      return { id, ...(value as object) } as T;
    },
    delete: async (resource) => {
      writes.push({ operation: "delete", resource, value: null });
    },
  };
  return { persistence, writes };
}

function boundaryOver(
  session: AdminSession | null,
  store: AdminPersistenceAdapter,
  rule: AdminPermissionRule = can,
) {
  return createAdminResourceActions({
    guard: createAdminPermissionGuard({ rule, session: () => session }),
    persistence: store,
    expose: exposedResource,
    definitions: resourceDefinitions,
  });
}

/** The panel's own boundary, and the rule it actually runs behind. */
function boundary(session: AdminSession | null, store: AdminPersistenceAdapter) {
  return boundaryOver(session, store);
}

/**
 * The same boundary with the rule opened up, so a refusal here is the column check's and not the
 * permission's. The panel's own rule grants reads only, which is a layer in front of this one.
 */
const openRule = () => true;

/** A device carrying every key the panel's own type declares, and nothing invented. */
const fullDevice = {
  name: "device-alpha",
  status: "online",
  healthy: true,
  busy: true,
  labels: ["linux", "arm64"],
  current_lease: {
    job_id: "9f2c1a7b",
    repository: "example-org/cindral",
    sha: "0123456789abcdef",
    ref: "main",
    lease_expires: 1750000000,
    remaining_seconds: 42,
  },
  leases: [],
  agent_online: true,
  build: { version: "0.13.0", commit: "0123456789abcdef", built_at: "2026-01-01T00:00:00Z" },
  build_known: true,
  last_seen: 1749999900,
  max_concurrency: 2,
  active_jobs: 1,
  available_slots: 1,
} satisfies DeviceInfo;

/** A run carrying every key the panel's own type declares. */
const fullJob = {
  id: "9f2c1a7b4d5e6f70",
  repository: "example-org/cindral",
  sha: "0123456789abcdef",
  ref: "main",
  command: ["pnpm", "ci"],
  labels: ["linux"],
  timeout: 1800,
  status: "failure",
  device: "device-alpha",
  exit_code: 1,
  attempts: 2,
  status_sha: "fedcba9876543210",
  log: "step 1 failed",
  log_truncated: false,
  steps: [{ index: 1, name: "typecheck", output: "error TS1", completed: false }],
  last_error: null,
  created_at: 1749999000,
} satisfies JobInfo;

describe("the panel's declared columns", () => {
  it("names every key the panel's own device type carries", () => {
    // A key the broker sends and this file does not name would be refused by name on a
    // write, so a broker that grows a field breaks the build's tests rather than the panel
    const declared = new Set(deviceResource.columns.map((column) => column.key));
    expect([...Object.keys(fullDevice)].sort()).toEqual([...declared].sort());
  });

  it("agrees with the panel's own run type", () => {
    // `id` is the one key a record may carry that no definition names, and the boundary
    // decides it itself: writable on a create, refused on an update
    const declared = new Set([
      ...jobResource.columns.map((column) => column.key),
      "id",
    ]);
    expect([...Object.keys(fullJob)].sort()).toEqual([...declared].sort());
  });

  it("names the same set in the fields a form would carry", () => {
    // a write that came through a form is read from the fields, and one that did not is
    // held to the columns, so a name in one and not the other is two answers to one question
    expect(jobResource.fields.map((field) => field.name).sort()).toEqual(
      jobResource.columns.map((column) => column.key).sort(),
    );
    expect(deviceResource.fields.map((field) => field.name).sort()).toEqual(
      deviceResource.columns.map((column) => column.key).sort(),
    );
  });

  it("names no write-only column, because the broker is read through the pool token", () => {
    // `writable` is how a host declares a column its list does not show. There is none here,
    // and adding one without a store behind it would widen the boundary for nothing.
    expect(deviceResource.writable).toBeUndefined();
    expect(jobResource.writable).toBeUndefined();
  });
});

describe("the write boundary", () => {
  it("refuses a write carrying a key the definition does not name, by name", async () => {
    const { persistence, writes } = recordingStore();
    const actions = boundaryOver(operator, persistence, openRule);

    await expect(
      actions.create(DEVICE_RESOURCE, { ...fullDevice, is_privileged: true }),
    ).rejects.toThrow(AdminResourceFieldError);
    await expect(
      actions.create(DEVICE_RESOURCE, { ...fullDevice, is_privileged: true, role: "admin" }),
    ).rejects.toThrow(/is_privileged, role/);
    expect(writes).toHaveLength(0);
  });

  it("refuses the privilege columns the boundary was added for", async () => {
    // This is the shape the 0.5.1 changelog describes: a definition declaring two columns and a
    // write reaching the store with `role` and `is_privileged` on it. The device definition
    // names neither, so neither reaches a store.
    const twoColumns = defineAdminResource({
      resource: "widgets",
      label: "Widgets",
      columns: [
        { key: "name", header: "Name" },
        { key: "sku", header: "SKU" },
      ],
      fields: [{ name: "name", label: "Name" }, { name: "sku", label: "SKU" }],
    });
    const { persistence, writes } = recordingStore();
    const actions = createAdminResourceActions({
      guard: createAdminPermissionGuard({ rule: openRule, session: () => operator }),
      persistence,
      expose: () => true,
      definitions: [twoColumns],
    });

    await expect(
      actions.create("widgets", { name: "Widget", sku: "W-1", role: "admin", is_privileged: true }),
    ).rejects.toThrow(/is_privileged, role/);
    expect(writes).toHaveLength(0);
  });

  it("holds a write to the declared columns and lets the rest through", async () => {
    const { persistence, writes } = recordingStore();
    const actions = boundaryOver(operator, persistence, openRule);

    await actions.create(DEVICE_RESOURCE, fullDevice);
    expect(writes).toEqual([{ operation: "create", resource: DEVICE_RESOURCE, value: fullDevice }]);
  });

  it("accepts an id on a create and refuses one on an update", async () => {
    // a create is where a host generates a record's own key; an update names the record in the
    // route, so a value carrying an id is a request to move the row somewhere the route did not
    const { persistence, writes } = recordingStore();
    const actions = boundaryOver(operator, persistence, openRule);

    await actions.create(JOB_RESOURCE, { ...fullJob, id: "chosen-by-the-caller" });
    expect(writes).toHaveLength(1);

    await expect(
      actions.update(JOB_RESOURCE, fullJob.id, { ...fullJob, id: "moved" }),
    ).rejects.toThrow(/^update jobs carries id/);
    expect(writes).toHaveLength(1);
  });

  it("refuses a row-moving write on an update for a definition that declares its own key", async () => {
    // the hazard of declaring `id` as a column rather than leaving it to the boundary: the key
    // joins the writable set and an update may then name a different record than the route did.
    // cindral's run definition leaves it out for this reason.
    const { persistence, writes } = recordingStore();
    const actions = createAdminResourceActions({
      guard: createAdminPermissionGuard({ rule: openRule, session: () => operator }),
      persistence,
      expose: () => true,
      definitions: [
        defineAdminResource({
          resource: JOB_RESOURCE,
          label: "Runs",
          columns: [{ key: "id", header: "Run ID" }, { key: "repository", header: "Repository" }],
          fields: [{ name: "id", label: "Run ID" }, { name: "repository", label: "Repository" }],
        }),
      ],
    });

      // Under 0.5.1 this write went through: declaring `id` put it in the writable set, and an
      // update named a different record than the route did. 0.5.2 takes `id` off the writable set for
      // an update whether or not the definition declares it, so the row cannot be moved. The test
      // used to assert the hole; it now asserts the hole is closed, which is why this file pins
      // both shapes rather than only the one cindral happens to use.
      await expect(
        actions.update(JOB_RESOURCE, fullJob.id, { id: "moved", repository: "example-org/cindral" }),
      ).rejects.toBeInstanceOf(AdminResourceFieldError);
      expect(writes).toEqual([]);

    });
  it("refuses every write, because the one rule grants reads and nothing else", async () => {
    // the panel's permission table names `devices.read` and `jobs.read` and no write, so a
    // write is refused by the rule before the column check and before the store
    const { persistence, writes } = recordingStore();
    const actions = boundary(operator, persistence);

    await expect(actions.create(DEVICE_RESOURCE, fullDevice)).rejects.toThrow(
      /may not devices\.create/,
    );
    await expect(actions.update(DEVICE_RESOURCE, "device-alpha", fullDevice)).rejects.toThrow(
      /may not devices\.update/,
    );
    await expect(actions.delete(JOB_RESOURCE, fullJob.id)).rejects.toThrow(
      /may not jobs\.delete/,
    );
    expect(writes).toHaveLength(0);
  });

  it("refuses a write from nobody signed in, before it reaches a definition", async () => {
    const { persistence, writes } = recordingStore();
    const actions = boundary(null, persistence);

    await expect(actions.create(DEVICE_RESOURCE, fullDevice)).rejects.toThrow(
      /No session may devices\.create/,
    );
    expect(writes).toHaveLength(0);
  });

  it("refuses a resource the panel does not expose, whatever the session may do", async () => {
    // refused before the session is resolved, so the answer cannot be used to ask which names
    // are worth trying
    const { persistence } = recordingStore();
    const actions = boundaryOver(operator, persistence, openRule);

    await expect(actions.query("secrets")).rejects.toThrow(AdminResourceNotExposedError);
  });

  it("checks nothing for a resource no definition names", async () => {
    // The package's stated behaviour, and the limit of this adoption: the boundary is exactly
    // as wide as the declarations. A resource added without a definition reopens the column set
    // it has, so the declaration is the whole of the guarantee.
    const { persistence, writes } = recordingStore();
    const actions = createAdminResourceActions({
      guard: createAdminPermissionGuard({ rule: () => true, session: () => operator }),
      persistence,
      expose: () => true,
    });

    await actions.create("undeclared", { anything: "at all", role: "admin" });
    expect(writes).toEqual([
      { operation: "create", resource: "undeclared", value: { anything: "at all", role: "admin" } },
    ]);
  });

  it("refuses a write against a definition that declares no columns", async () => {
    // the opposite statement: a host that has said what it stores, and stored nothing
    const { persistence, writes } = recordingStore();
    const actions = createAdminResourceActions({
      guard: createAdminPermissionGuard({ rule: () => true, session: () => operator }),
      persistence,
      expose: () => true,
      definitions: [defineAdminResource({ resource: "blank", label: "Blank", columns: [], fields: [] })],
    });

    await expect(actions.create("blank", { name: "x" })).rejects.toThrow(AdminResourceFieldError);
    expect(writes).toHaveLength(0);
  });

  it("only checks a write's own top-level keys, so a declared column is a whole document", async () => {
    // `build` is one declared column, so anything inside it reaches a store unchecked. A host
    // that stores a nested object the browser fills in declares the object, not its shape.
    const { persistence, writes } = recordingStore();
    const actions = createAdminResourceActions({
      guard: createAdminPermissionGuard({ rule: () => true, session: () => operator }),
      persistence,
      expose: () => true,
      definitions: resourceDefinitions,
    });

    await actions.create(DEVICE_RESOURCE, { ...fullDevice, build: { privileged: true } });
    expect(writes).toHaveLength(1);
  });
});

describe("the reads that do happen", () => {
  it("grants the two reads the panel's pages make and nothing else", async () => {
    const { persistence } = recordingStore();
    const actions = boundary(operator, persistence);

    await expect(actions.query(DEVICE_RESOURCE)).resolves.toEqual([]);
    await expect(actions.read(JOB_RESOURCE, fullJob.id)).resolves.toBeNull();
    await expect(actions.read(DEVICE_RESOURCE, "device-alpha")).resolves.toBeNull();
  });

  it("asks for the permission the definitions and the rule share", async () => {
    expect(readDevices).toBe(`${DEVICE_RESOURCE}.read`);
    expect(readJobs).toBe(`${JOB_RESOURCE}.read`);
  });
});
