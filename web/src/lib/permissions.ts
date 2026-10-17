/**
 * The panel's one permission rule.
 *
 * A rendered nav item and a served action both come through here, so the two cannot disagree about
 * who may see what. The rule answers from the session `auth.ts` resolved off a signed cookie and
 * never from the request, so it has nothing to defend itself against, and the role it switches on
 * is a stored value no request can name.
 *
 * The panel is a single-operator surface today, so the table has one row. It is a table rather than
 * a constant because the second role is the change that would otherwise scatter these answers back
 * across the shell and the routes, which is the shape this file exists to prevent.
 *
 * Nothing here names a write. The panel's store has no write to make and the broker would refuse
 * one with the read-only pool token, but the rule grants nothing it was not asked to grant, so the
 * boundary refuses a write by permission before it reaches the column check or the store.
 */
import { NextResponse } from "next/server";
import { redirect } from "next/navigation";
import {
  AdminPermissionDeniedError,
  AdminUnauthenticatedError,
  createAdminPermissionGuard,
  createAdminSessionGuard,
  type AdminPermission,
  type AdminSession,
} from "@/lib/admin-core";
import { currentAdminSession } from "@/lib/auth";
import { readDevices, readJobs } from "@/lib/resources";

const LOGIN_HREF = "/login";

const UNAUTHENTICATED = "the control panel requires a signed-in operator";

const READS_BY_ROLE: Record<string, ReadonlySet<AdminPermission>> = {
  operator: new Set<AdminPermission>([readDevices, readJobs]),
};

/**
 * May this session perform this permission. Fails closed: no session denies, a role this table
 * does not name grants nothing, and only a literal `true` allows.
 */
export function can(
  session: AdminSession | null,
  permission: AdminPermission,
): boolean {
  if (!session) {
    return false;
  }
  return READS_BY_ROLE[session.role ?? ""]?.has(permission) === true;
}

/**
 * For a page: no session goes to the sign-in screen. A session that may not read a resource is
 * refused by the store's own guard below, which is the one decision a read and a write share, so a
 * page does not ask a second time and risk the two answering differently.
 */
export const requirePageSession = createAdminSessionGuard({
  session: currentAdminSession,
  loginHref: LOGIN_HREF,
  onUnauthenticated: ({ loginHref }) => redirect(loginHref),
});

/**
 * For a server action or a route handler: the same decision, refused by throwing the typed error,
 * so a caller cannot forget to check an answer it did not read. The store's guard is this one, so
 * a resource read on a page and a resource read in a route handler are decided the same way.
 */
export const requirePermission = createAdminPermissionGuard({
  rule: can,
  session: currentAdminSession,
});

/**
 * The response a refusal is, or null when the error is something else.
 *
 * A guard refuses by throwing, so a route catches one answer rather than checking a return value a
 * caller can forget to read. The two refusals stay distinct because a visitor who is not signed in
 * and a visitor who is not allowed are different problems with different fixes.
 */
export function permissionRefusal(
  permission: AdminPermission,
  error: unknown,
): NextResponse | null {
  if (error instanceof AdminUnauthenticatedError) {
    return NextResponse.json({ error: UNAUTHENTICATED }, { status: 401 });
  }
  if (error instanceof AdminPermissionDeniedError) {
    return NextResponse.json(
      { error: `this session may not ${permission}` },
      { status: 403 },
    );
  }
  return null;
}

/**
 * A view asks `can()` with the session this module resolves, which is the same rule and
 * the same server-resolved session the action behind it uses, so a control drawn from it
 * and the action it guards cannot disagree.
 *
 * The package also ships a `createAdminPermissionCheck` wrapper. It had no caller here:
 * the nav renders with `can()` directly, so keeping a second uncalled path was a second
 * way for the two to drift, which is what it existed to prevent.
 */
