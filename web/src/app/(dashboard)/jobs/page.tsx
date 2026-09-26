import { AdminBanner, AdminPageHeader, AdminSectionCard } from "@yesvus/helmdeck";
import { Activity } from "lucide-react";
import { loadPool } from "@/lib/load-pool";
import { JobTable } from "../tables";

export const dynamic = "force-dynamic";

export default async function JobsPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Queue & Run History" />
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

  return (
    <div className="space-y-6">
      <AdminPageHeader title="Queue & Run History" />
      <AdminSectionCard
        icon={Activity}
        title="Direct Execution Queue"
        description="Active leases, queued commits, and completed device runs"
      >
        <JobTable jobs={result.snapshot.recent_jobs} full />
      </AdminSectionCard>
    </div>
  );
}
