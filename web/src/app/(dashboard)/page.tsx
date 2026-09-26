import {
  AdminBanner,
  AdminPageHeader,
  AdminStatCard,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Activity, Clock, Cpu, CheckCircle2, RotateCcw } from "lucide-react";
import { getPoolSnapshot, type DeviceInfo, type JobInfo } from "@/lib/cindral";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const snapshot = await getPoolSnapshot();

  const pendingCount = snapshot?.queue_depth?.pending ?? 0;
  const runningCount = snapshot?.queue_depth?.running ?? 0;
  const successCount = snapshot?.success_count ?? 0;
  const failureCount = snapshot?.failure_count ?? 0;
  const reclaimCount = snapshot?.reclaim_count ?? 0;
  const oldestPendingSec = snapshot?.oldest_pending_age_seconds ?? 0;
  const devices = snapshot?.devices ?? [];
  const recentJobs = snapshot?.recent_jobs ?? [];

  const deviceColumns: AdminTableColumn<DeviceInfo>[] = [
    {
      key: "name",
      header: "Device Name",
      cell: (d) => <span className="font-semibold text-zinc-900 dark:text-zinc-100">{d.name}</span>,
    },
    {
      key: "status",
      header: "Health & State",
      cell: (d) => {
        const tone = d.status === "online" && d.healthy ? "success" : "error";
        return <AdminStatusPill tone={tone} label={`${d.status} ${d.healthy ? "" : "(unhealthy)"}`} />;
      },
    },
    {
      key: "busy",
      header: "Allocation",
      cell: (d) => {
        if (d.current_lease) {
          return (
            <AdminStatusPill
              tone="warning"
              label={`Leased: ${d.current_lease.repository}`}
            />
          );
        }
        return <AdminStatusPill tone="neutral" label="Idle" />;
      },
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
      cell: (j) => <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">{j.id.slice(0, 8)}</span>,
    },
    {
      key: "repository",
      header: "Repository & Ref",
      cell: (j) => (
        <div>
          <div className="font-medium text-zinc-900 dark:text-zinc-100">{j.repository}</div>
          <div className="text-xs text-zinc-500 font-mono">{j.ref} @ {j.sha.slice(0, 7)}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (j) => {
        let tone: "success" | "warning" | "error" | "info" | "neutral" = "neutral";
        if (j.status === "success") tone = "success";
        else if (j.status === "failure") tone = "error";
        else if (j.status === "running") tone = "warning";
        else if (j.status === "pending") tone = "info";
        return <AdminStatusPill tone={tone} label={j.status} />;
      },
    },
    {
      key: "device",
      header: "Runner Device",
      cell: (j) => <span className="text-sm">{j.device || "Unassigned"}</span>,
    },
  ];

  return (
    <div className="space-y-6">
      <AdminPageHeader title="Pool Overview" />

      {!snapshot && (
        <AdminBanner
          tone="warning"
          title="Broker API unreachable"
          body="Could not fetch pool snapshot from the Cindral broker. Verify CINDRAL_API_URL and network connectivity."
        />
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <AdminStatCard
          icon={Clock}
          label="Pending Queue"
          value={String(pendingCount)}
          detail={oldestPendingSec > 0 ? `Oldest waiting ${oldestPendingSec}s` : "Queue clear"}
          tone={pendingCount > 0 ? "warning" : "neutral"}
        />
        <AdminStatCard
          icon={Cpu}
          label="Active Leases"
          value={String(runningCount)}
          detail={`${devices.filter((d) => d.busy).length} active devices`}
          tone={runningCount > 0 ? "success" : "neutral"}
        />
        <AdminStatCard
          icon={CheckCircle2}
          label="Completed Runs"
          value={String(successCount + failureCount)}
          detail={`${successCount} passed, ${failureCount} failed`}
          tone="neutral"
        />
        <AdminStatCard
          icon={RotateCcw}
          label="Reclaimed Leases"
          value={String(reclaimCount)}
          detail="Abandoned / expired recovery"
          tone={reclaimCount > 0 ? "warning" : "neutral"}
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
            rows={devices}
            getKey={(d) => d.name}
            empty={<div className="p-4 text-center text-sm text-zinc-500">No devices configured</div>}
          />
        </AdminSectionCard>

        <AdminSectionCard
          icon={Activity}
          title="Recent Dispatches"
          description="Latest jobs submitted to the direct runner pool"
        >
          <AdminTable
            columns={jobColumns}
            rows={recentJobs.slice(0, 5)}
            getKey={(j) => j.id}
            empty={<div className="p-4 text-center text-sm text-zinc-500">No jobs recorded yet</div>}
          />
        </AdminSectionCard>
      </div>
    </div>
  );
}
