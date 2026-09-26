"use client";

import {
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
} from "@yesvus/helmdeck";
import { jobStatusTone, type DeviceInfo, type JobInfo } from "@/lib/cindral";

const DEVICE_COLUMNS: AdminTableColumn<DeviceInfo>[] = [
  {
    key: "name",
    header: "Device Name",
    cell: (d) => (
      <span className="font-semibold text-zinc-900 dark:text-zinc-100">
        {d.name}
      </span>
    ),
  },
  {
    key: "status",
    header: "Health & State",
    cell: (d) => (
      <AdminStatusPill
        tone={d.status === "online" && d.healthy ? "success" : "error"}
        label={`${d.status}${d.healthy ? "" : " (unhealthy)"}`}
      />
    ),
  },
  {
    key: "allocation",
    header: "Allocation",
    cell: (d) =>
      d.current_lease ? (
        <AdminStatusPill
          tone="warning"
          label={`Leased: ${d.current_lease.repository}`}
        />
      ) : (
        <AdminStatusPill tone="neutral" label="Idle" />
      ),
  },
  {
    key: "labels",
    header: "Labels",
    cell: (d) => (
      <span className="text-xs text-zinc-500 dark:text-zinc-400">
        {d.labels.length > 0 ? d.labels.join(", ") : "None"}
      </span>
    ),
  },
];

const JOB_COLUMNS: AdminTableColumn<JobInfo>[] = [
  {
    key: "id",
    header: "Job ID",
    cell: (j) => (
      <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">
        {j.id.slice(0, 8)}
      </span>
    ),
  },
  {
    key: "repository",
    header: "Repository & Ref",
    cell: (j) => (
      <div>
        <div className="font-medium text-zinc-900 dark:text-zinc-100">
          {j.repository}
        </div>
        <div className="font-mono text-xs text-zinc-500">
          {j.ref} @ {j.sha.slice(0, 7)}
        </div>
      </div>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (j) => (
      <AdminStatusPill tone={jobStatusTone(j.status)} label={j.status} />
    ),
  },
  {
    key: "device",
    header: "Runner Device",
    cell: (j) => <span className="text-sm">{j.device || "Unassigned"}</span>,
  },
];

export function DeviceTable({ devices }: { devices: DeviceInfo[] }) {
  return (
    <AdminTable
      columns={DEVICE_COLUMNS}
      rows={devices}
      getKey={(d) => d.name}
      empty={
        <div className="p-4 text-center text-sm text-zinc-500">
          No devices configured
        </div>
      }
    />
  );
}

const DEVICE_DETAIL_COLUMNS: AdminTableColumn<DeviceInfo>[] = [
  {
    key: "name",
    header: "Device Name",
    cell: (d) => (
      <div>
        <div className="font-semibold text-zinc-900 dark:text-zinc-100">
          {d.name}
        </div>
        <div className="text-xs text-zinc-500">Node Runner</div>
      </div>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (d) => (
      <div className="flex items-center gap-2">
        <AdminStatusPill
          tone={d.status === "online" && d.healthy ? "success" : "error"}
          label={d.status}
        />
        {!d.healthy && <AdminStatusPill tone="warning" label="Degraded" />}
      </div>
    ),
  },
  {
    key: "labels",
    header: "Runner Labels",
    cell: (d) => (
      <div className="flex flex-wrap gap-1">
        {d.labels.map((label) => (
          <span
            key={label}
            className="inline-block rounded bg-zinc-100 px-2 py-0.5 font-mono text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
          >
            {label}
          </span>
        ))}
        {d.labels.length === 0 && (
          <span className="text-xs text-zinc-400">None</span>
        )}
      </div>
    ),
  },
  {
    key: "allocation",
    header: "Current Allocation",
    cell: (d) => {
      if (!d.current_lease) {
        // a device can be occupied by a GitHub runner without holding a
        // Cindral lease, so busy is not the same as having a lease
        return (
          <AdminStatusPill
            tone={d.busy ? "warning" : "neutral"}
            label={d.busy ? "Busy (no Cindral lease)" : "Idle"}
          />
        );
      }
      const lease = d.current_lease;
      return (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5">
            <AdminStatusPill tone="warning" label="Leased" />
            <span className="font-mono text-xs text-zinc-600 dark:text-zinc-300">
              {lease.repository}
            </span>
          </div>
          <div className="font-mono text-xs text-zinc-500">
            Job: {lease.job_id.slice(0, 8)} (
            {Math.round(lease.remaining_seconds)}s remaining)
          </div>
          {d.leases.length > 1 && (
            <div className="text-xs font-medium text-amber-600 dark:text-amber-400">
              {d.leases.length} concurrent leases on this device
            </div>
          )}
        </div>
      );
    },
  },
];

export function DeviceDetailTable({ devices }: { devices: DeviceInfo[] }) {
  return (
    <AdminTable
      columns={DEVICE_DETAIL_COLUMNS}
      rows={devices}
      getKey={(d) => d.name}
      empty={
        <div className="p-8 text-center text-sm text-zinc-500">
          No runner devices registered
        </div>
      }
    />
  );
}

export function JobTable({ jobs, full }: { jobs: JobInfo[]; full?: boolean }) {
  return (
    <AdminTable
      columns={full ? FULL_JOB_COLUMNS : JOB_COLUMNS}
      rows={jobs}
      getKey={(j) => j.id}
      empty={
        <div className="p-8 text-center text-sm text-zinc-500">
          No jobs currently recorded
        </div>
      }
    />
  );
}

const FULL_JOB_COLUMNS: AdminTableColumn<JobInfo>[] = [
  ...JOB_COLUMNS.slice(0, 4),
  {
    key: "command",
    header: "Contract Command",
    cell: (j) => (
      <div className="max-w-xs truncate font-mono text-xs text-zinc-600 dark:text-zinc-400">
        {j.command.length > 0 ? j.command.join(" ") : "default ci"}
      </div>
    ),
  },
  {
    key: "log",
    header: "Log Output",
    cell: (j) =>
      j.log ? (
        <details className="group text-xs">
          <summary className="cursor-pointer text-amber-600 hover:underline dark:text-amber-400">
            View log tail
            {j.log_truncated ? " (truncated)" : ""}
          </summary>
          <pre className="mt-2 max-h-48 max-w-lg overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-100">
            {j.log}
          </pre>
        </details>
      ) : (
        <span className="text-xs text-zinc-400">No output</span>
      ),
  },
];
