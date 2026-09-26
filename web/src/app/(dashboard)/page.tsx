import {
  AdminBanner,
  AdminPageHeader,
  AdminStatCard,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Activity, CheckCircle2, Clock, Cpu, RotateCcw } from "lucide-react";
import { loadPool } from "@/lib/load-pool";
import { DeviceTable, JobTable } from "./tables";

export const dynamic = "force-dynamic";

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
              ? "Sign in required"
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
  const busy = snapshot.devices.filter((d) => d.busy).length;

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
          <DeviceTable devices={snapshot.devices} />
        </AdminSectionCard>

        <AdminSectionCard
          icon={Activity}
          title="Recent Dispatches"
          description="Latest jobs submitted to the direct runner pool"
        >
          <JobTable jobs={snapshot.recent_jobs.slice(0, 5)} />
        </AdminSectionCard>
      </div>
    </div>
  );
}
