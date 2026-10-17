import {
  AdminBanner,
  AdminPageHeader,
  AdminStatCard,
  AdminSectionCard,
} from "@/components/admin";
import { Activity, CheckCircle2, Clock, Cpu, RotateCcw } from "lucide-react";
import { loadPool, poolFailureTitle } from "@/lib/load-pool";
import { STALL_ADVICE } from "@/lib/cindral";
import { formatDuration } from "@/lib/time";
import { DeviceTable, JobTable } from "./tables";
import { SnapshotControls } from "./snapshot-controls";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Pool Overview" />
        <AdminBanner
          tone={result.reason === "unauthorized" ? "error" : "warning"}
          title={poolFailureTitle(result.reason)}
          body={result.message}
        />
      </div>
    );
  }

  const { snapshot, devices, jobs, fetchedAt } = result;
  const pending = snapshot.queue_depth.pending;
  const oldest = snapshot.oldest_pending_age_seconds;
  const busy = snapshot.devices.filter((d) => d.busy).length;

  return (
    <div className="space-y-6">
      <AdminPageHeader
        title="Pool Overview"
        action={<SnapshotControls fetchedAt={fetchedAt} />}
      />

      {snapshot.stalled_reason && (
        <AdminBanner
          tone="warning"
          title="Queue needs attention"
          body={`${pending} job(s) waiting, oldest ${formatDuration(oldest)}: ${(STALL_ADVICE[snapshot.stalled_reason] ?? "the queue has not moved")}`}
        />
      )}

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
          detail={
            oldest > 0 ? `Oldest waiting ${formatDuration(oldest)}` : "Queue clear"
          }
          tone={pending > 0 ? "warning" : "neutral"}
        />
        <AdminStatCard
          icon={Cpu}
          label="Active Leases"
          value={String(snapshot.queue_depth.running)}
          detail={`${busy} active devices`}
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
          detail={
            snapshot.live_device_reclaim_count
              ? `${snapshot.live_device_reclaim_count} from a device that was still connected`
              : "Abandoned / expired recovery"
          }
          tone={snapshot.reclaim_count > 0 ? "warning" : "neutral"}
        />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <AdminSectionCard
          icon={Cpu}
          title="Device Status"
          description="Available runner hardware and active lease slots"
        >
          <DeviceTable devices={devices} />
        </AdminSectionCard>

        <AdminSectionCard
          icon={Activity}
          title="Recent Dispatches"
          description="Latest jobs submitted to the direct runner pool"
        >
          <JobTable jobs={jobs.slice(0, 5)} now={fetchedAt} />
        </AdminSectionCard>
      </div>
    </div>
  );
}
