"use client";
import Link from "next/link";

// One column set per fact, shared by the overview and the detail pages, so a
// device or a job reads the same way wherever it appears.

import {
  AdminStatusPill,
  AdminTable,
  type AdminStatusTone,
  type AdminTableColumn,
} from "@/components/admin";
import { jobStatusTone, type DeviceInfo, type JobInfo } from "@/lib/cindral";
import { absoluteTime, formatCountdown } from "@/lib/time";
import { RelativeTime } from "./relative-time";

// a lease this close to lapsing needs a renewal from the agent, which is the
// countdown an operator actually watches
const LEASE_WARN_SECONDS = 60;

// a broker that predates the agent heartbeat reports neither field, and a
// column of "no agent" placeholders would read as a fault rather than as a
// broker that cannot answer the question yet
const reportsAgents = (devices: DeviceInfo[]) =>
  devices.some((d) => typeof d.agent_online === "boolean");

// AdminTable already pads and centres the empty cell, so a table's empty state
// carries wording only and every table lands on the same vertical rhythm
function TableEmpty({ children }: { children: string }) {
  return <p className="text-sm text-admin-text-muted">{children}</p>;
}

const LABEL_TAG_CLASS =
  "inline-block rounded bg-admin-surface-muted px-2 py-0.5 font-mono text-xs text-admin-text-secondary";

function deviceHealth(d: DeviceInfo): { tone: AdminStatusTone; label: string } {
  const healthy = d.status === "online" && d.healthy;
  return {
    tone: healthy ? "success" : "error",
    label: d.healthy ? d.status : `${d.status} (unhealthy)`,
  };
}

function deviceAgent(d: DeviceInfo): { tone: AdminStatusTone; label: string } | null {
  if (d.agent_online === undefined) {
    return null;
  }
  if (d.agent_online) {
    return { tone: "success", label: "Agent online" };
  }
  // an agent that dropped off mid-session wants different attention than one
  // that has never reported, and only the last-seen age tells them apart
  return d.last_seen
    ? { tone: "warning", label: "Agent not responding" }
    : { tone: "error", label: "No agent" };
}

// A device agent is a checkout on the machine, so the commit it reports is the
// only thing that makes its age visible: a device behind the broker claims a job
// and answers with code older than the job. "unknown" is deliberately a first
// class answer, because a device that does not say is the case to notice.
export function agentBuild(d: DeviceInfo): { label: string; title: string } {
  const commit = d.build?.commit;
  if (!d.build_known || !commit || commit === "unknown") {
    return { label: "unknown", title: "this device did not report a build" };
  }
  return {
    label: commit.slice(0, 8),
    title: `${d.build?.version ?? "no version"} · ${commit}`,
  };
}

function DeviceIdentityCell({ device }: { device: DeviceInfo }) {
  return (
    <div className="min-w-0">
      <div className="truncate font-semibold text-admin-text-primary">
        {device.name}
      </div>
      <div className="text-xs text-admin-text-muted">Node Runner</div>
      <div className="mt-1 flex flex-wrap gap-1">
        {device.labels.map((label) => (
          <span key={label} className={LABEL_TAG_CLASS}>
            {label}
          </span>
        ))}
        {device.labels.length === 0 && (
          <span className="text-xs text-admin-text-disabled">No labels</span>
        )}
      </div>
    </div>
  );
}

function DeviceStateCell({ device }: { device: DeviceInfo }) {
  const health = deviceHealth(device);
  const agent = deviceAgent(device);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <AdminStatusPill tone={health.tone} label={health.label} />
      {agent && <AdminStatusPill tone={agent.tone} label={agent.label} />}
    </div>
  );
}

function DeviceAllocationCell({ device }: { device: DeviceInfo }) {
  const lease = device.current_lease;
  if (!lease) {
    // a device can be occupied by a GitHub runner without holding a Cindral
    // lease, so busy is not the same as having a lease
    return (
      <AdminStatusPill
        tone={device.busy ? "warning" : "neutral"}
        label={device.busy ? "Busy (no Cindral lease)" : "Idle"}
      />
    );
  }
  // one badge, one subject, one meta line: a row stays a single record instead
  // of three ragged stacks
  return (
    <div className="min-w-0">
      <div className="flex min-w-0 items-center gap-2">
        <AdminStatusPill tone="warning" label="Leased" />
        <span className="truncate text-sm font-medium text-admin-text-primary">
          {lease.repository}
        </span>
      </div>
      <div className="mt-1 truncate font-mono text-xs text-admin-text-muted">
        {lease.job_id.slice(0, 8)}
        {device.leases.length > 1 && (
          <span className="font-sans font-medium text-admin-warning-text">
            {" · "}
            {device.leases.length} concurrent leases
          </span>
        )}
      </div>
    </div>
  );
}

function DeviceLeaseCell({ device }: { device: DeviceInfo }) {
  const lease = device.current_lease;
  if (!lease) {
    return <span className="text-xs text-admin-text-disabled">No lease</span>;
  }
  const { dateTime, label } = absoluteTime(lease.lease_expires);
  const expiring = lease.remaining_seconds <= LEASE_WARN_SECONDS;
  return (
    <time
      dateTime={dateTime}
      title={label}
      className={`block whitespace-nowrap text-xs tabular-nums ${
        expiring ? "font-medium text-admin-warning-text" : "text-admin-text-muted"
      }`}
    >
      {formatCountdown(lease.remaining_seconds)}
    </time>
  );
}

const DEVICE_COLUMNS: AdminTableColumn<DeviceInfo>[] = [
  {
    key: "name",
    header: "Device",
    width: "11rem",
    className: "max-w-[11rem]",
    cell: (d) => <DeviceIdentityCell device={d} />,
  },
  {
    key: "state",
    header: "State",
    width: "13rem",
    cell: (d) => <DeviceStateCell device={d} />,
  },
  {
    key: "allocation",
    header: "Allocation",
    width: "13rem",
    className: "max-w-[13rem]",
    cell: (d) => <DeviceAllocationCell device={d} />,
  },
  {
    key: "lease",
    header: "Lease Remaining",
    width: "8rem",
    align: "right",
    headerClassName: "whitespace-nowrap",
    cell: (d) => <DeviceLeaseCell device={d} />,
  },
];

export function DeviceTable({ devices }: { devices: DeviceInfo[] }) {
  return (
    <AdminTable
      caption="Runner devices in the pool"
      columns={DEVICE_COLUMNS}
      rows={devices}
      getKey={(d) => d.name}
      empty={<TableEmpty>No runner devices registered</TableEmpty>}
    />
  );
}

// a device whose agent is not connected reports no free count at all, because
// it takes no new work, so the cell explains the absence rather than repeating
// the limit it was last configured with
function slotPill(d: DeviceInfo): { tone: AdminStatusTone; label: string } {
  if (d.available_slots == null) {
    return {
      tone: "neutral",
      label: d.max_concurrency == null ? "Unknown" : "Not connected",
    };
  }
  return {
    tone: d.available_slots > 0 ? "success" : "warning",
    label: `${d.available_slots} of ${d.max_concurrency} free`,
  };
}

// the presence badge lives in the shared state cell, so these two columns carry
// only what the badge cannot: when the agent was last heard and how much room
// it left
function deviceAgentColumns(now: number): AdminTableColumn<DeviceInfo>[] {
  return [
    {
      key: "last_seen",
      header: "Last Seen",
      width: "8rem",
      align: "right",
      headerClassName: "whitespace-nowrap",
      cell: (d) =>
        typeof d.last_seen === "number" ? (
          <RelativeTime
            at={d.last_seen}
            now={now}
            className="whitespace-nowrap text-xs tabular-nums text-admin-text-muted"
          />
        ) : (
          <span className="text-xs text-admin-text-disabled">Never seen</span>
        ),
    },
    {
      key: "build",
      header: "Agent Build",
      width: "10rem",
      align: "right",
      headerClassName: "whitespace-nowrap",
      cell: (d) => {
        const build = agentBuild(d);
        if (build.label === "unknown") {
          return (
            <span className="text-xs text-admin-text-disabled" title={build.title}>
              {build.label}
            </span>
          );
        }
        return (
          <span className="font-mono text-xs text-admin-text-muted" title={build.title}>
            {build.label}
          </span>
        );
      },
    },
    {
      key: "slots",
      header: "Free Slots",
      width: "11rem",
      align: "right",
      headerClassName: "whitespace-nowrap",
      cell: (d) => {
        const slot = slotPill(d);
        return (
          <div className="flex items-center justify-end gap-2 whitespace-nowrap">
            <AdminStatusPill tone={slot.tone} label={slot.label} />
            <span className="text-xs tabular-nums text-admin-text-muted">
              {d.active_jobs ?? 0} running
            </span>
          </div>
        );
      },
    },
  ];
}

export function DeviceDetailTable({
  devices,
  now,
}: {
  devices: DeviceInfo[];
  now: number;
}) {
  const columns = reportsAgents(devices)
    ? [...DEVICE_COLUMNS, ...deviceAgentColumns(now)]
    : DEVICE_COLUMNS;
  return (
    <AdminTable
      caption="Runner devices, their leases, and their agent capacity"
      columns={columns}
      rows={devices}
      getKey={(d) => d.name}
      empty={<TableEmpty>No runner devices registered</TableEmpty>}
    />
  );
}

function jobColumns(jobs: JobInfo[], now: number): AdminTableColumn<JobInfo>[] {
  const columns: AdminTableColumn<JobInfo>[] = [
    {
      key: "id",
      header: "Job ID",
      width: "6rem",
      className: "max-w-[6rem]",
      cell: (j) => (
        <Link href={`/jobs/${encodeURIComponent(j.id)}`} className="font-mono text-xs text-admin-text-secondary underline hover:text-admin-text-primary">
          {j.id.slice(0, 8)}
        </Link>
      ),
    },
    {
      key: "repository",
      header: "Repository & Ref",
      width: "16rem",
      className: "max-w-[16rem]",
      cell: (j) => (
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-admin-text-primary">
            {j.repository}
          </div>
          <div className="truncate font-mono text-xs text-admin-text-muted">
            {j.ref} @ {j.sha.slice(0, 7)}
          </div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      width: "7rem",
      cell: (j) => (
        <AdminStatusPill tone={jobStatusTone(j.status)} label={j.status} />
      ),
    },
    {
      key: "device",
      header: "Runner Device",
      width: "10rem",
      className: "max-w-[10rem]",
      cell: (j) =>
        j.device ? (
          <span className="block truncate text-sm text-admin-text-primary">
            {j.device}
          </span>
        ) : (
          <span className="text-sm text-admin-text-disabled">Unassigned</span>
        ),
    },
    {
      key: "exit",
      header: "Exit",
      width: "5rem",
      align: "right",
      headerClassName: "whitespace-nowrap",
      cell: (j) =>
        j.exit_code == null ? (
          <span className="text-xs text-admin-text-disabled">-</span>
        ) : (
          <span
            className={`font-mono text-xs tabular-nums ${
              j.exit_code === 0
                ? "text-admin-text-muted"
                : "text-admin-danger-text"
            }`}
          >
            {j.exit_code}
          </span>
        ),
    },
  ];
  // a column of placeholders reads as a fault, so the dispatch time only
  // appears once the broker reports one
  if (jobs.some((j) => typeof j.created_at === "number")) {
    columns.push({
      key: "dispatched",
      header: "Dispatched",
      width: "8rem",
      align: "right",
      headerClassName: "whitespace-nowrap",
      cell: (j) =>
        typeof j.created_at === "number" ? (
          <RelativeTime
            at={j.created_at}
            now={now}
            className="whitespace-nowrap text-xs tabular-nums text-admin-text-muted"
          />
        ) : (
          <span className="text-xs text-admin-text-disabled">Unknown</span>
        ),
    });
  }
  return columns;
}

export function JobTable({
  jobs,
  now,
  full,
}: {
  jobs: JobInfo[];
  now: number;
  full?: boolean;
}) {
  const columns = full
    ? [...jobColumns(jobs, now), ...JOB_DETAIL_COLUMNS]
    : jobColumns(jobs, now);
  return (
    <AdminTable
      caption={full ? "Queue and run history" : "Recent dispatches"}
      columns={columns}
      rows={jobs}
      getKey={(j) => j.id}
      empty={<TableEmpty>No jobs recorded</TableEmpty>}
    />
  );
}

const JOB_DETAIL_COLUMNS: AdminTableColumn<JobInfo>[] = [
  {
    key: "attempts",
    header: "Attempts",
    width: "7rem",
    align: "right",
    headerClassName: "whitespace-nowrap",
    cell: (j) => (
      <span className="font-mono text-xs tabular-nums text-admin-text-secondary">
        {j.attempts}
      </span>
    ),
  },
  {
    key: "command",
    header: "Contract Command",
    width: "14rem",
    className: "max-w-[14rem]",
    cell: (j) => (
      <div className="truncate font-mono text-xs text-admin-text-secondary">
        {j.command.length > 0 ? j.command.join(" ") : "default ci"}
      </div>
    ),
  },
  {
    key: "log",
    header: "Log Output",
    width: "20rem",
    className: "max-w-[20rem]",
    cell: (j) =>
      <Link href={`/jobs/${encodeURIComponent(j.id)}`} className="text-xs text-admin-warning-text underline hover:text-admin-text-primary">
        {j.log ? "View run output" : "View run"}{j.log_truncated ? " (truncated)" : ""}
      </Link>,
  },
];
