import type { ReactNode } from "react";

export type AdminSession = { email: string; role?: string; name?: string; id?: string };
export type AdminPermission = string;
export type AdminLoginCredentials = { email: string; password: string };
export type AdminLoginResult = { ok: true; session: AdminSession } | { ok: false; message: string };
export type AdminPermissionRule = (
  session: AdminSession,
  permission: AdminPermission,
  context?: { resourceId?: string },
) => boolean | Promise<boolean>;
export type AdminPermissionContext = { resourceId?: string };
export type AdminPermissionReason = "allowed" | "denied" | "no-session" | "no-rule" | "rule-failed";
export type AdminPermissionDecision = {
  permission: AdminPermission;
  context?: AdminPermissionContext;
  session: AdminSession | null;
  allowed: boolean;
  reason: AdminPermissionReason;
  cause?: unknown;
};
export type AdminPermissionGuard = (
  permission: AdminPermission,
  context?: AdminPermissionContext,
) => Promise<AdminSession>;
export type AdminSessionResolver = () => AdminSession | null | Promise<AdminSession | null>;
export type AdminResourceOperation = "read" | "create" | "update" | "delete";
export type AdminResourceColumn = { key: string; header: ReactNode };
export type AdminResourceField = { name: string; label: string };
export type AdminResourceDefinition = {
  resource: string;
  label: string;
  singularLabel?: string;
  path?: string;
  columns: AdminResourceColumn[];
  fields: AdminResourceField[];
  writable?: string[];
  permissions?: Partial<Record<AdminResourceOperation, AdminPermission>>;
};
export type AdminPersistenceAdapter = {
  read: <T = Record<string, unknown>>(resource: string, id: string) => Promise<T | null>;
  query: <T = Record<string, unknown>>(resource: string, query?: Record<string, unknown>) => Promise<T[]>;
  create: <T = Record<string, unknown>>(resource: string, value: unknown) => Promise<T>;
  update: <T = Record<string, unknown>>(resource: string, id: string, value: unknown) => Promise<T>;
  delete: (resource: string, id: string) => Promise<void>;
};
export type AdminResourceActions = {
  query: <T = Record<string, unknown>>(resource: string, query?: Record<string, unknown>) => Promise<T[]>;
  read: <T = Record<string, unknown>>(resource: string, id: string) => Promise<T | null>;
  create: <T = Record<string, unknown>>(resource: string, value: unknown) => Promise<T>;
  update: <T = Record<string, unknown>>(resource: string, id: string, value: unknown) => Promise<T>;
  delete: (resource: string, id: string) => Promise<void>;
};

export class AdminUnauthenticatedError extends Error {
  constructor(readonly permission: AdminPermission) {
    super(`No session may ${permission}`);
    this.name = "AdminUnauthenticatedError";
  }
}

export class AdminPermissionDeniedError extends Error {
  readonly permission: AdminPermission;
  readonly context?: AdminPermissionContext;
  readonly reason: AdminPermissionReason;
  readonly session: AdminSession | null;

  constructor(decision: Omit<AdminPermissionDecision, "allowed">) {
    super(`This session may not ${decision.permission}`);
    this.name = "AdminPermissionDeniedError";
    this.permission = decision.permission;
    this.context = decision.context;
    this.reason = decision.reason;
    this.session = decision.session;
  }
}

export class AdminSessionRequiredError extends Error {
  readonly reason: "no-session" | "session-failed";

  constructor(input: { reason: "no-session" | "session-failed"; loginHref: string; returnTo: string }) {
    super("A signed-in session is required");
    this.name = "AdminSessionRequiredError";
    this.reason = input.reason;
  }
}

export class AdminResourceNotExposedError extends Error {
  constructor(readonly resource: string) {
    super(`Resource is not exposed: ${resource}`);
    this.name = "AdminResourceNotExposedError";
  }
}

export class AdminResourceFieldError extends Error {
  readonly fields: readonly string[];
  readonly operation: AdminResourceOperation;

  constructor(input: { resource: string; operation: AdminResourceOperation; fields: readonly string[] }) {
    super(`${input.operation} ${input.resource} carries ${input.fields.join(", ")}`);
    this.name = "AdminResourceFieldError";
    this.fields = input.fields;
    this.operation = input.operation;
  }
}

export function defineAdminResource(definition: AdminResourceDefinition): AdminResourceDefinition {
  return definition;
}

export async function evaluateAdminPermission(input: {
  rule?: AdminPermissionRule;
  session: AdminSession | null;
  permission: AdminPermission;
  context?: AdminPermissionContext;
}): Promise<boolean> {
  if (!input.rule || !input.session) return false;
  try {
    return (await input.rule(input.session, input.permission, input.context)) === true;
  } catch {
    return false;
  }
}

export function createAdminPermissionGuard(options: {
  rule?: AdminPermissionRule;
  session: AdminSessionResolver;
  onError?: (cause: unknown) => void;
}): AdminPermissionGuard {
  return async (permission, context) => {
    let session: AdminSession | null;
    try {
      session = await options.session();
    } catch (cause) {
      options.onError?.(cause);
      throw new AdminUnauthenticatedError(permission);
    }
    if (!session) throw new AdminUnauthenticatedError(permission);
    let reason: AdminPermissionReason = options.rule ? "denied" : "no-rule";
    if (options.rule) {
      try {
        if ((await options.rule(session, permission, context)) === true) return session;
      } catch (cause) {
        options.onError?.(cause);
        reason = "rule-failed";
      }
    }
    throw new AdminPermissionDeniedError({ permission, context, session, reason });
  };
}

export function createAdminSessionGuard(options: {
  session: AdminSessionResolver;
  loginHref?: string;
  onUnauthenticated?: (input: {
    session: null;
    reason: "no-session" | "session-failed";
    loginHref: string;
    returnTo: string;
  }) => unknown;
  onError?: (cause: unknown) => void;
}): () => Promise<AdminSession> {
  return async () => {
    let session: AdminSession | null = null;
    let reason: "no-session" | "session-failed" = "no-session";
    try {
      session = await options.session();
    } catch (cause) {
      options.onError?.(cause);
      reason = "session-failed";
    }
    if (session) return session;
    const refusal = {
      session: null,
      reason,
      loginHref: options.loginHref ?? "/login",
      returnTo: "/",
    } as const;
    options.onUnauthenticated?.(refusal);
    throw new AdminSessionRequiredError(refusal);
  };
}

export function createAdminResourceActions(options: {
  guard: AdminPermissionGuard;
  persistence: AdminPersistenceAdapter;
  expose?: (resource: string) => boolean;
  definitions?: readonly AdminResourceDefinition[];
}): AdminResourceActions {
  const ensureExposed = (resource: string) => {
    if (options.expose && !options.expose(resource)) throw new AdminResourceNotExposedError(resource);
  };
  const authorize = async (resource: string, operation: AdminResourceOperation, id?: string) => {
    ensureExposed(resource);
    const definition = options.definitions?.find((item) => item.resource === resource);
    await options.guard(definition?.permissions?.[operation] ?? `${resource}.${operation}`, id ? { resourceId: id } : undefined);
    return definition;
  };
  const validate = (resource: string, operation: "create" | "update", value: unknown, definition?: AdminResourceDefinition) => {
    if (!definition || !value || typeof value !== "object" || Array.isArray(value)) return;
    const allowed = new Set([
      ...definition.columns.map((column) => column.key),
      ...(definition.writable ?? []),
    ]);
    const fields = Object.keys(value)
      .filter((key) =>
        operation === "update" ? key === "id" || !allowed.has(key) : key !== "id" && !allowed.has(key),
      )
      .sort();
    if (fields.length) throw new AdminResourceFieldError({ resource, operation, fields });
  };
  return {
    async query<T>(resource: string, query?: Record<string, unknown>) {
      await authorize(resource, "read");
      return options.persistence.query<T>(resource, query);
    },
    async read<T>(resource: string, id: string) {
      await authorize(resource, "read", id);
      return options.persistence.read<T>(resource, id);
    },
    async create<T>(resource: string, value: unknown) {
      const definition = await authorize(resource, "create");
      validate(resource, "create", value, definition);
      return options.persistence.create<T>(resource, value);
    },
    async update<T>(resource: string, id: string, value: unknown) {
      const definition = await authorize(resource, "update", id);
      validate(resource, "update", value, definition);
      return options.persistence.update<T>(resource, id, value);
    },
    async delete(resource: string, id: string) {
      await authorize(resource, "delete", id);
      return options.persistence.delete(resource, id);
    },
  };
}
