import {
  AdminBanner,
  AdminPageHeader,
  AdminStatCard,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Activity, CheckCircle2, Clock, Cpu, RotateCcw } from "lucide-react";
import { jobStatusTone, type DeviceInfo, type JobInfo } from "@/lib/cindral";
import { loadPool } from "@/lib/load-pool";

export const dynamic = "force-dynamic";

const deviceColumns: AdminTableColumn<DeviceInfo>[] = [
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
        <AdminStatusPill tone="warning" label={`Leased: ${d.current_lease.repository}`} />
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

const jobColumns: AdminTableColumn<JobInfo>[] = [
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
    cell: (j) => <AdminStatusPill tone={jobStatusTone(j.status)} label={j.status} />,
  },
  {
    key: "device",
    header: "Runner Device",
    cell: (j) => <span className="text-sm">{j.device || "Unassigned"}</span>,
  },
];

export default async function OverviewPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Pool Overview" />
        <AdminBanner
          tone={result.reason === "unauthorized" ? "error" : "warning"}
          title={
            result.reason === "unauthorized"
              ? "Operator token required"
              : "Broker unavailable"
          }
          body={result.message}
        />
      </div>
    );
  }

  const { snapshot } = result;
  const pending = snapshot.queue_depth.pending;
  const oldest = snapshot.oldest_pending_age_seconds;

  return (
    <div className="space-y-6">
      <AdminPageHeader title="Pool Overview" />

      {snapshot.expired_lease_count > 0 && (
        <AdminBanner
          tone="warning"
          title="Expired leases awaiting reclaim"
          body={`${snapshot.expired_lease_count} running job(s) passed their lease and are queued for another device.`}
        />
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <AdminStatCard
          icon={Clock}
          label="Pending Queue"
          value={String(pending)}
          detail={oldest > 0 ? `Oldest waiting ${oldest}s` : "Queue clear"}
          tone={pending > 0 ? "warning" : "neutral"}
        />
        <AdminStatCard
          icon={Cpu}
          label="Active Leases"
          value={String(snapshot.queue_depth.running)}
          detail={`${snapshot.devices.filter((d) => d.busy).length} active devices`}
          tone={snapshot.queue_depth.running > 0 ? "success" : "neutral"}
        />
        <AdminStatCard
          icon={CheckCircle2}
          label="Completed Runs"
          value={String(snapshot.success_count + snapshot.failure_count)}
          detail={`${snapshot.success_count} passed, ${snapshot.failure_count} failed`}
        />
        <AdminStatCard
          icon={RotateCcw}
          label="Reclaimed Leases"
          value={String(snapshot.reclaim_count)}
          detail="Abandoned / expired recovery"
          tone={snapshot.reclaim_count > 0 ? "warning" : "neutral"}
        />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <AdminSectionCard
          icon={Cpu}
          title="Device Status"
          description="Available runner hardware and active lease slots"
        >
          <AdminTable
            columns={deviceColumns}
            rows={snapshot.devices}
            getKey={(d) => d.name}
            empty={
              <div className="p-4 text-center text-sm text-zinc-500">
                No devices configured
              </div>
            }
          />
        </AdminSectionCard>

        <AdminSectionCard
          icon={Activity}
          title="Recent Dispatches"
          description="Latest jobs submitted to the direct runner pool"
        >
          <AdminTable
            columns={jobColumns}
            rows={snapshot.recent_jobs.slice(0, 5)}
            getKey={(j) => j.id}
            empty={
              <div className="p-4 text-center text-sm text-zinc-500">
                No jobs recorded yet
              </div>
            }
          />
        </AdminSectionCard>
      </div>
    </div>
  );
}
