/**
 * What the panel stores, declared once.
 *
 * A `devices` record and a `jobs` record are the broker's own payloads, so the column set here is
 * the payload's key set and nothing else. That matters because 0.5.1 holds a write to a
 * definition's declared columns: a write carrying a key none of these names is refused by name
 * rather than stored, so a resource this file describes incompletely fails a write instead of
 * quietly accepting one. The keys the broker may add later are the ones to add here first.
 *
 * A record's own key is the one key left undeclared, because the boundary handles it: `id` is
 * writable on a create, where a host may generate a key, and refused on an update, where the route
 * already named the record. Declaring `id` as a column would put it in the writable set and
 * reopen the row-moving write the package closes by leaving it out.
 */
import {
  defineAdminResource,
  type AdminPermission,
  type AdminResourceColumn,
  type AdminResourceField,
} from "@/lib/admin-core";

export const DEVICE_RESOURCE = "devices";
export const JOB_RESOURCE = "jobs";

/**
 * The form's fields, read off the columns.
 *
 * A write that came through a form is held to `fields` and one that did not is held to `columns`, so
 * the two naming the same set by hand is two answers to one question waiting to drift. The panel
 * draws its own cells rather than the generated form, so the fields are here to be a declaration the
 * boundary reads rather than a control set.
 */
function fieldsOf(columns: AdminResourceColumn[]): AdminResourceField[] {
  return columns.map((column) => ({
    name: column.key,
    label: typeof column.header === "string" ? column.header : column.key,
  }));
}

/** The names the write boundary and the guard ask, in the seam's own default shape. */
export const readDevices = `${DEVICE_RESOURCE}.read` as AdminPermission;
export const readJobs = `${JOB_RESOURCE}.read` as AdminPermission;

const DEVICE_COLUMNS: AdminResourceColumn[] = [
  { key: "name", header: "Device" },
  { key: "status", header: "State" },
  { key: "healthy", header: "Healthy" },
  { key: "busy", header: "Busy" },
  { key: "labels", header: "Labels" },
  { key: "current_lease", header: "Current lease" },
  { key: "leases", header: "Leases" },
  { key: "agent_online", header: "Agent" },
  { key: "build", header: "Agent build" },
  { key: "build_known", header: "Agent build known" },
  { key: "last_seen", header: "Last seen" },
  { key: "max_concurrency", header: "Max concurrency" },
  { key: "active_jobs", header: "Active jobs" },
  { key: "available_slots", header: "Available slots" },
];

const JOB_COLUMNS: AdminResourceColumn[] = [
  { key: "repository", header: "Repository" },
  { key: "sha", header: "Commit" },
  { key: "ref", header: "Ref" },
  { key: "command", header: "Command" },
  { key: "labels", header: "Labels" },
  { key: "timeout", header: "Timeout" },
  { key: "status", header: "Status" },
  { key: "device", header: "Device" },
  { key: "exit_code", header: "Exit" },
  { key: "attempts", header: "Attempts" },
  { key: "last_error", header: "Last error" },
  { key: "status_sha", header: "Status SHA" },
  { key: "log", header: "Log" },
  { key: "log_truncated", header: "Log truncated" },
  { key: "steps", header: "Steps" },
  { key: "created_at", header: "Dispatched" },
];

export const deviceResource = defineAdminResource({
  resource: DEVICE_RESOURCE,
  label: "Runner devices",
  singularLabel: "Runner device",
  path: "devices",
  columns: DEVICE_COLUMNS,
  fields: fieldsOf(DEVICE_COLUMNS),
});

export const jobResource = defineAdminResource({
  resource: JOB_RESOURCE,
  label: "Runs",
  singularLabel: "Run",
  path: "jobs",
  columns: JOB_COLUMNS,
  fields: fieldsOf(JOB_COLUMNS),
});

export const resourceDefinitions = [deviceResource, jobResource] as const;

/** A name outside this set is refused before the session is resolved. */
export function exposedResource(name: string): boolean {
  return name === DEVICE_RESOURCE || name === JOB_RESOURCE;
}
