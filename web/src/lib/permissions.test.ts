import { describe, expect, it } from "vitest";
import {
  AdminPermissionDeniedError,
  AdminUnauthenticatedError,
  evaluateAdminPermission,
} from "@/lib/admin-core";
import { can, permissionRefusal } from "@/lib/permissions";
import { readDevices, readJobs } from "@/lib/resources";

/**
 * The panel's one rule, and the two shapes a refusal takes.
 *
 * `can` is what the nav filters on and what the store's guard asks, so a test that only ever
 * called it directly would miss the case that matters: a rule that answers differently for a
 * session it did not mint. Every "no" below is a case where a panel that had guessed would show a
 * control whose action then refused.
 */
const operator = { email: "operator@example.com", role: "operator" };

describe("can", () => {
  it("lets an operator read the two resources the panel has", () => {
    expect(can(operator, readDevices)).toBe(true);
    expect(can(operator, readJobs)).toBe(true);
  });

  it("grants no session nothing", () => {
    expect(can(null, readDevices)).toBe(false);
    expect(can(null, readJobs)).toBe(false);
  });

  it("grants a session with no role nothing", () => {
    // a role a request could name would be the whole of the authorization
    expect(can({ email: "operator@example.com" }, readDevices)).toBe(false);
    expect(can({ email: "operator@example.com", role: "" }, readDevices)).toBe(false);
  });

  it("grants a role this table does not name nothing", () => {
    for (const role of ["admin", "viewer", "Operator", "operator ", "superuser"]) {
      expect(can({ email: "a@b.c", role }, readDevices)).toBe(false);
    }
  });

  it("grants an operator no write, so the boundary refuses every one of them", () => {
    for (const permission of [
      "devices.create",
      "devices.update",
      "devices.delete",
      "jobs.create",
      "jobs.update",
      "jobs.delete",
    ]) {
      expect(can(operator, permission)).toBe(false);
    }
  });

  it("grants a permission it does not name, so a typo denies rather than allows", () => {
    expect(can(operator, "device.read")).toBe(false);
    expect(can(operator, "")).toBe(false);
    expect(can(operator, "devices")).toBe(false);
  });
});

describe("the rule behind the guard", () => {
  it("fails closed on a rule that throws", async () => {
    // a rule that cannot decide must not decide yes
    const allowed = await evaluateAdminPermission({
      rule: () => {
        throw new Error("the role store is down");
      },
      session: operator,
      permission: readDevices,
    });
    expect(allowed).toBe(false);
  });

  it("fails closed on a rule that answers something other than true", async () => {
    for (const answer of [undefined, null, 0, "", "yes", 1, {}]) {
      const allowed = await evaluateAdminPermission({
        rule: () => answer as unknown as boolean,
        session: operator,
        permission: readDevices,
      });
      expect(allowed).toBe(false);
    }
  });

  it("fails closed with no rule at all", async () => {
    // a host that wired the views and forgot the server has no authorization rather than a
    // mislaid permission, and that has to read as a refusal
    const allowed = await evaluateAdminPermission({
      session: operator,
      permission: readDevices,
    });
    expect(allowed).toBe(false);
  });

  it("decides the operator's reads and a visitor's the same way the guard does", async () => {
    for (const [session, permission, expected] of [
      [operator, readDevices, true],
      [operator, readJobs, true],
      [operator, "devices.create", false],
      [null, readDevices, false],
    ] as const) {
      expect(
        await evaluateAdminPermission({ rule: can, session, permission }),
      ).toBe(expected);
    }
  });
});

describe("permissionRefusal", () => {
  it("answers 401 for nobody signed in and 403 for a session that may not", async () => {
    const unauthenticated = permissionRefusal(
      readJobs,
      new AdminUnauthenticatedError(readJobs),
    );
    const refused = permissionRefusal(
      readJobs,
      new AdminPermissionDeniedError({
        permission: readJobs,
        session: { email: "a@b.c", role: "viewer" },
        reason: "denied",
      }),
    );

    expect(unauthenticated?.status).toBe(401);
    expect(refused?.status).toBe(403);
    expect(await refused?.json()).toEqual({ error: "this session may not jobs.read" });
    expect(await unauthenticated?.json()).toEqual({
      error: "the control panel requires a signed-in operator",
    });
  });

  it("passes an error that is not a refusal through untouched", () => {
    // a broker that is down is a 503, not a permission answer, and swallowing it here would
    // report an outage as an authorization failure
    expect(permissionRefusal(readJobs, new Error("the broker did not respond"))).toBeNull();
  });
});
