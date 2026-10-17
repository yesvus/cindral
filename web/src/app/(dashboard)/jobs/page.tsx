import { AdminBanner, AdminPageHeader, AdminSectionCard } from "@/components/admin";
import { Activity } from "lucide-react";
import { loadPool, poolFailureTitle } from "@/lib/load-pool";
import { SnapshotControls } from "../snapshot-controls";
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
          title={poolFailureTitle(result.reason)}
          body={result.message}
        />
      </div>
    );
  }

  const { jobs, fetchedAt } = result;

  return (
    <div className="space-y-6">
      <AdminPageHeader
        title="Queue & Run History"
        action={<SnapshotControls fetchedAt={fetchedAt} />}
      />
      <AdminSectionCard
        icon={Activity}
        title="Direct Execution Queue"
        description="Active leases, queued commits, and completed device runs"
      >
        <JobTable jobs={jobs} now={fetchedAt} full />
      </AdminSectionCard>
    </div>
  );
}
